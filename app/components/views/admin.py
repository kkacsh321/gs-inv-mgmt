from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlparse
import json
import re
from collections import Counter
import zipfile
from io import BytesIO
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import delete, text
from sqlalchemy import select

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.auth import DEFAULT_PERMISSIONS, auth_debug_snapshot, current_user, ensure_permission
from app.components.views.shared import handoff_to_documents_draft, render_help_panel
from app.components.views.ebay import render_ebay_connection_status_card
from app.components.views.system_health import render_system_health
from app.config import settings
from app.db.migrate import downgrade as migrate_downgrade
from app.db.migrate import upgrade as migrate_upgrade
from app.db.models import (
    AIProviderConfig,
    AuditLog,
    CoinAIRun,
    DocumentTemplateProfile,
    IntegrationQueueJob,
    InventoryMovement,
    InventorySource,
    MarketplaceListing,
    MediaAsset,
    Order,
    OrderItem,
    Product,
    ProductLotAssignment,
    PurchaseLot,
    ReturnRecord,
    Sale,
    SavedFilterProfile,
    ShippingPreset,
    WorkflowDraft,
    WorkflowEvent,
)
from app.db.seed import seed_dev_data
from app.repository import InventoryRepository
from app.services.db_backup import (
    create_backup_dump,
    download_backup_from_s3,
    list_backups_in_s3,
    pg_tools_status,
    restore_dump_file,
    s3_backup_enabled,
    upload_backup_to_s3,
)
from app.services.config_health import health_state, required_env_keys, required_runtime_keys
from app.services.env_manager import (
    SENSITIVE_ENV_KEYS,
    ensure_env_defaults,
    is_editable_env_key,
    mask_env_value,
    read_process_env_values,
    read_env_file,
    upsert_env_key,
    uses_env_file,
)
from app.services.ebay import EbayClient
from app.services.coin_reference_sources import (
    resolve_paid_coin_source_adapter,
    resolve_paid_coin_source_config,
)
from app.services.grading_standards import (
    CURATED_COMP_BASELINE,
    CURATED_GRADING_BASELINE,
    build_coin_grading_rules_context_from_web,
    build_comp_rules_context_from_web,
    clear_standards_snapshot_cache,
    fetch_standards_snapshot,
)
from app.services.llm_runtime import (
    DEFAULT_COMP_INSTRUCTION,
    DEFAULT_COMP_SYSTEM_MESSAGE,
    LLMRuntimeConfig,
    fetch_available_models,
    validate_llm_runtime_config,
)
from app.services.sync_jobs import is_sync_job_enabled, sync_job_catalog
from app.services.runtime_settings import (
    get_runtime_bool,
    get_runtime_float,
    get_runtime_int,
    get_runtime_str,
    get_runtime_value,
)
from app.services.ai_prompt_registry import (
    active_prompt_version,
    create_prompt_version,
    list_prompt_versions,
    restore_prompt_version,
)
from app.services.integration_automation import preview_rule_impact, simulate_rule_evaluation_for_job
from app.services.integration_queue import (
    process_due_google_queue_jobs,
    process_due_integration_queue_jobs,
    process_integration_queue_job,
)
from app.services.slack_ops_bot import approve_slack_ops_queue_job
from app.services.shipping_labels import purchase_shipping_label
from app.services.slack_notify import build_slack_alert_text, dispatch_slack_alert, send_slack_message
from app.services.notification_outbox import (
    cleanup_notification_outbox_retention,
    process_due_notification_outbox,
)
from app.services.lifecycle_retention import cleanup_lifecycle_retention
from app.components.views.tools import DEFAULT_COMP_DEALER_DOMAINS
from app.components.views.listing_wizard import (
    DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE,
    DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT,
    DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE,
)
from app.utils.time import utcnow_naive


def _audit_changes(row: AuditLog) -> dict:
    try:
        payload = json.loads(str(getattr(row, "changes_json", "") or "{}"))
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        return payload
    return {}


def _build_business_status_context(repo: InventoryRepository, *, days: int = 1) -> dict[str, Any]:
    now = utcnow_naive()
    since = now - timedelta(days=max(1, int(days)))
    metrics = repo.dashboard_metrics()
    products = repo.list_products()
    listings = repo.list_listings()
    sales = repo.list_sales()
    orders = repo.list_orders()

    sales_window = [s for s in sales if getattr(s, "sold_at", None) and s.sold_at >= since]
    gross_window = float(sum(float(getattr(s, "sold_price", 0.0) or 0.0) for s in sales_window))
    fees_window = float(sum(float(getattr(s, "fees", 0.0) or 0.0) for s in sales_window))
    shipping_window = float(sum(float(getattr(s, "shipping_cost", 0.0) or 0.0) for s in sales_window))
    net_window = gross_window - fees_window - shipping_window

    low_stock = [p for p in products if int(getattr(p, "current_quantity", 0) or 0) <= 1]
    draft_listings = [l for l in listings if str(getattr(l, "listing_status", "") or "").strip().lower() == "draft"]
    active_listings = [l for l in listings if str(getattr(l, "listing_status", "") or "").strip().lower() == "active"]
    unlisted_products = [p for p in products if not bool(getattr(p, "listing_id", None))]
    orders_window = [o for o in orders if getattr(o, "order_date", None) and o.order_date >= since]

    return {
        "env": settings.app_env,
        "window_days": int(days),
        "as_of_utc": now.isoformat(timespec="seconds"),
        "product_count": int(metrics.get("product_count", len(products))),
        "listing_count": int(metrics.get("listing_count", len(listings))),
        "active_count": int(len(active_listings)),
        "draft_count": int(len(draft_listings)),
        "unlisted_count": int(len(unlisted_products)),
        "low_stock_count": int(len(low_stock)),
        "sale_count": int(metrics.get("sale_count", len(sales))),
        "sales_window_count": int(len(sales_window)),
        "gross_window": f"{gross_window:,.2f}",
        "net_window": f"{net_window:,.2f}",
        "order_count": int(len(orders)),
        "orders_window_count": int(len(orders_window)),
        "inventory_cost": f"{float(metrics.get('inventory_cost', 0.0)):,.2f}",
    }


def _slack_ops_queue_snapshot(rows: list[Any], *, now: datetime | None = None) -> dict[str, Any]:
    now_dt = now or utcnow_naive()
    normalized_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    pending_approval_ages_hours: list[float] = []
    for row in rows or []:
        status = str(getattr(row, "status", "") or "").strip().lower()
        status_counts[status] += 1
        payload = {}
        try:
            payload_raw = json.loads(str(getattr(row, "payload_json", "") or "{}"))
            if isinstance(payload_raw, dict):
                payload = payload_raw
        except Exception:
            payload = {}
        approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
        approval_required = bool(approval.get("required", False))
        approval_status = str(approval.get("status") or "").strip().lower()
        requested_at_raw = str(approval.get("requested_at") or "").strip()
        requested_at = None
        try:
            if requested_at_raw:
                requested_at = datetime.fromisoformat(requested_at_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            requested_at = None
        age_hours = None
        if status == "blocked" and approval_required and approval_status == "pending" and requested_at is not None:
            age_hours = max(0.0, (now_dt - requested_at).total_seconds() / 3600.0)
            pending_approval_ages_hours.append(age_hours)
        normalized_rows.append(
            {
                "id": int(getattr(row, "id", 0) or 0),
                "action": str(getattr(row, "action", "") or ""),
                "status": status,
                "retry_count": int(getattr(row, "retry_count", 0) or 0),
                "max_retries": int(getattr(row, "max_retries", 0) or 0),
                "next_attempt_at": getattr(row, "next_attempt_at", None),
                "requested_by": str(getattr(row, "requested_by", "") or ""),
                "created_at": getattr(row, "created_at", None),
                "last_error": str(getattr(row, "last_error", "") or ""),
                "intent": str(((payload.get("command") or {}).get("intent") or "")).strip().lower(),
                "approval_required": approval_required,
                "approval_status": approval_status,
                "approval_requested_at": requested_at_raw,
                "approval_requested_by": str(approval.get("requested_by") or "").strip(),
                "approval_approved_at": str(approval.get("approved_at") or "").strip(),
                "approval_approved_by": str(approval.get("approved_by") or "").strip(),
                "pending_approval_age_hours": None if age_hours is None else round(float(age_hours), 2),
            }
        )
    pending_count = int(sum(1 for row in normalized_rows if row["status"] == "blocked" and row["approval_required"] and row["approval_status"] == "pending"))
    return {
        "rows": normalized_rows,
        "total_count": int(len(normalized_rows)),
        "queued_count": int(status_counts.get("queued", 0)),
        "running_count": int(status_counts.get("running", 0)),
        "blocked_count": int(status_counts.get("blocked", 0)),
        "success_count": int(status_counts.get("success", 0)),
        "failed_count": int(status_counts.get("failed", 0)),
        "pending_approval_count": pending_count,
        "pending_approval_avg_hours": round(sum(pending_approval_ages_hours) / len(pending_approval_ages_hours), 2) if pending_approval_ages_hours else 0.0,
        "pending_approval_max_hours": round(max(pending_approval_ages_hours), 2) if pending_approval_ages_hours else 0.0,
    }


def _summarize_ai_quality_metrics(
    ai_metric_rows: list[tuple[Any, Any, Any, Any]],
    *,
    workflow_filter: str = "all",
) -> dict[str, Any]:
    apply_events = 0
    outcome_events = 0
    accepted_as_is_count = 0
    edited_count = 0
    workflow_totals: dict[str, dict[str, int]] = {}
    version_totals: dict[str, dict[str, int]] = {}
    daily_totals: dict[str, dict[str, int]] = {}
    workflow_daily_totals: dict[str, dict[str, int]] = {}
    edited_field_totals_by_workflow: dict[str, Counter[str]] = {}
    recent_rows: list[dict[str, str]] = []

    wf_filter = str(workflow_filter or "all").strip().lower()

    for created_at, actor, action, changes_json in ai_metric_rows or []:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        workflow_name = str(payload.get("workflow") or "").strip().lower() or "unknown"
        if wf_filter != "all" and workflow_name != wf_filter:
            continue

        action_name = str(action or "").strip().lower()
        if action_name == "listing_wizard_apply":
            apply_events += 1
            continue
        if action_name != "listing_wizard_outcome":
            continue

        outcome_events += 1
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
        accepted_as_is = bool(outcome.get("accepted_as_is"))
        if accepted_as_is:
            accepted_as_is_count += 1
        else:
            edited_count += 1

        wf_row = workflow_totals.setdefault(workflow_name, {"accepted_as_is": 0, "edited": 0, "total": 0})
        wf_row["total"] += 1
        if accepted_as_is:
            wf_row["accepted_as_is"] += 1
        else:
            wf_row["edited"] += 1

        acceptance = payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else {}
        prompt_version = str(acceptance.get("prompt_version_id") or "").strip() or "(unversioned)"
        pv_row = version_totals.setdefault(prompt_version, {"accepted_as_is": 0, "edited": 0, "total": 0})
        pv_row["total"] += 1
        if accepted_as_is:
            pv_row["accepted_as_is"] += 1
        else:
            pv_row["edited"] += 1

        created_dt = created_at if isinstance(created_at, datetime) else None
        if created_dt is not None:
            date_key = created_dt.date().isoformat()
            day_row = daily_totals.setdefault(date_key, {"accepted_as_is": 0, "edited": 0, "total": 0})
            wf_day_key = f"{workflow_name}::{date_key}"
            wf_day_row = workflow_daily_totals.setdefault(
                wf_day_key,
                {"workflow": workflow_name, "date": date_key, "accepted_as_is": 0, "edited": 0, "total": 0},
            )
            day_row["total"] += 1
            wf_day_row["total"] += 1
            if accepted_as_is:
                day_row["accepted_as_is"] += 1
                wf_day_row["accepted_as_is"] += 1
            else:
                day_row["edited"] += 1
                wf_day_row["edited"] += 1

        edited_fields = outcome.get("edited_fields") if isinstance(outcome.get("edited_fields"), list) else []
        if edited_fields:
            wf_counter = edited_field_totals_by_workflow.setdefault(workflow_name, Counter())
            for item in edited_fields:
                key = str(item or "").strip()
                if key:
                    wf_counter[key] += 1
        recent_rows.append(
            {
                "created_at": str(created_at or ""),
                "actor": str(actor or ""),
                "workflow": workflow_name,
                "prompt_version_id": prompt_version,
                "accepted_as_is": str(accepted_as_is),
                "edited_fields": ", ".join(str(x) for x in edited_fields[:8]),
            }
        )

    daily_rows = []
    for date_key in sorted(daily_totals.keys()):
        item = daily_totals[date_key]
        total = int(item.get("total") or 0)
        accepted = int(item.get("accepted_as_is") or 0)
        edited = int(item.get("edited") or 0)
        daily_rows.append(
            {
                "date": date_key,
                "total": total,
                "accepted_as_is": accepted,
                "edited": edited,
                "accept_rate_pct": round((float(accepted) / float(total) * 100.0) if total else 0.0, 2),
            }
        )
    workflow_daily_rows = []
    for key in sorted(workflow_daily_totals.keys()):
        item = workflow_daily_totals[key]
        total = int(item.get("total") or 0)
        accepted = int(item.get("accepted_as_is") or 0)
        edited = int(item.get("edited") or 0)
        workflow_daily_rows.append(
            {
                "workflow": str(item.get("workflow") or ""),
                "date": str(item.get("date") or ""),
                "total": total,
                "accepted_as_is": accepted,
                "edited": edited,
                "accept_rate_pct": round((float(accepted) / float(total) * 100.0) if total else 0.0, 2),
            }
        )

    edited_fields_top_rows = []
    for workflow_name, counter in edited_field_totals_by_workflow.items():
        for field_name, count in counter.most_common():
            edited_fields_top_rows.append(
                {
                    "workflow": workflow_name,
                    "field": field_name,
                    "edit_count": int(count),
                }
            )

    return {
        "apply_events": int(apply_events),
        "outcome_events": int(outcome_events),
        "accepted_as_is_count": int(accepted_as_is_count),
        "edited_count": int(edited_count),
        "workflow_totals": workflow_totals,
        "version_totals": version_totals,
        "daily_rows": daily_rows,
        "workflow_daily_rows": workflow_daily_rows,
        "recent_rows": recent_rows,
        "edited_fields_top_rows": edited_fields_top_rows,
    }


def _build_normalized_fee_coverage_admin_summary(
    repo: InventoryRepository,
    *,
    lookback_weeks: int,
    threshold_percent: float,
    min_consecutive_weeks: int,
) -> dict[str, Any]:
    now = utcnow_naive()
    start_dt = now - timedelta(days=max(2, int(lookback_weeks)) * 7)
    if not hasattr(repo, "report_ebay_fee_reconciliation_rows"):
        return {
            "error": "reconciliation_not_supported",
            "triggered": False,
            "latest_week_start": "",
            "latest_week_coverage_pct": 0.0,
            "consecutive_below": 0,
            "weekly_rows": [],
        }

    rows = repo.report_ebay_fee_reconciliation_rows(start_dt=start_dt, end_dt=now)
    weekly: dict[str, dict[str, int]] = {}
    for row in rows or []:
        sold_at_raw = str(row.get("sold_at") or "").strip()
        source = str(row.get("actual_fee_source") or "").strip().lower()
        if not sold_at_raw:
            continue
        try:
            sold_at_dt = datetime.fromisoformat(sold_at_raw.replace("Z", "+00:00"))
        except Exception:
            try:
                sold_at_dt = datetime.fromisoformat(sold_at_raw)
            except Exception:
                continue
        week_start = (sold_at_dt.date() - timedelta(days=sold_at_dt.weekday())).isoformat()
        bucket = weekly.setdefault(week_start, {"total": 0, "normalized": 0})
        bucket["total"] += 1
        if source == "normalized_order_finance_entries_marketplace_fee_sum":
            bucket["normalized"] += 1

    weekly_rows: list[dict[str, Any]] = []
    for week_start in sorted(weekly.keys()):
        total = int(weekly[week_start]["total"])
        normalized = int(weekly[week_start]["normalized"])
        coverage = (float(normalized) / float(total) * 100.0) if total > 0 else 0.0
        weekly_rows.append(
            {
                "week_start": week_start,
                "total_sales": total,
                "normalized_sales": normalized,
                "coverage_pct": round(coverage, 2),
            }
        )

    consecutive_below = 0
    for week_row in reversed(weekly_rows):
        if float(week_row.get("coverage_pct") or 0.0) < float(threshold_percent):
            consecutive_below += 1
        else:
            break
    triggered = bool(consecutive_below >= max(1, int(min_consecutive_weeks)))
    latest_week = weekly_rows[-1] if weekly_rows else {}
    return {
        "triggered": triggered,
        "latest_week_start": str(latest_week.get("week_start") or ""),
        "latest_week_coverage_pct": float(latest_week.get("coverage_pct") or 0.0),
        "latest_week_total_sales": int(latest_week.get("total_sales") or 0),
        "consecutive_below": int(consecutive_below),
        "threshold_percent": float(threshold_percent),
        "min_consecutive_weeks": int(min_consecutive_weeks),
        "weekly_rows": weekly_rows,
    }


def _all_permission_options() -> list[str]:
    options = set()
    for perms in DEFAULT_PERMISSIONS.values():
        options.update(perms)
    return sorted(options)


def _workspace_parity_specs() -> list[dict]:
    return [
        {
            "workflow": "eBay listing lifecycle",
            "legacy_surface": "Listings + eBay Ops",
            "unified_surface": "eBay Workspace",
            "required_permission": "bulk_update",
            "audit_entity_types": ["listing", "sync_run", "sync_error"],
            "audit_actions": ["update", "retry", "resolve_error"],
            "task_completion_workflows": ["ebay_workspace", "listings"],
        },
        {
            "workflow": "Shipping queue bulk updates",
            "legacy_surface": "Shipping",
            "unified_surface": "Fulfillment Ops (planned)",
            "required_permission": "bulk_update",
            "audit_entity_types": ["sale", "shipping_preset", "sync_run"],
            "audit_actions": ["update", "create"],
            "task_completion_workflows": ["shipping"],
        },
        {
            "workflow": "Sync failure triage/retry",
            "legacy_surface": "Sync",
            "unified_surface": "Sync Ops (planned)",
            "required_permission": "create",
            "audit_entity_types": ["sync_run", "sync_error"],
            "audit_actions": ["create", "update", "resolve_error"],
            "task_completion_workflows": ["sync"],
        },
        {
            "workflow": "Runtime/env configuration updates",
            "legacy_surface": "Admin tabs",
            "unified_surface": "Admin controls",
            "required_permission": "manage_settings",
            "audit_entity_types": ["runtime_setting", "app_user", "role_permission"],
            "audit_actions": ["create", "update", "delete"],
            "task_completion_workflows": [],
        },
        {
            "workflow": "AI-assisted operational tools",
            "legacy_surface": "Tools",
            "unified_surface": "Workspace-integrated tools",
            "required_permission": "ai_comp_use",
            "audit_entity_types": ["coin_ai_run", "workspace_feedback", "navigation"],
            "audit_actions": ["create", "submit", "page_view"],
            "task_completion_workflows": ["operations_home"],
        },
    ]


def _get_current_db_revision(repo: InventoryRepository) -> str:
    try:
        value = repo.db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        return str(value)
    except Exception:
        return "unknown (alembic_version not found)"


def _migration_history_rows() -> list[dict[str, str]]:
    project_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(project_root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    head_revision = script.get_current_head()
    rows: list[dict[str, str]] = []
    for rev in script.walk_revisions(base="base", head="heads"):
        down = rev.down_revision
        if isinstance(down, tuple):
            down_label = ", ".join(str(item) for item in down)
        else:
            down_label = str(down or "")
        rows.append(
            {
                "revision": rev.revision,
                "down_revision": down_label,
                "message": rev.doc or "",
                "is_head": "yes" if rev.revision == head_revision else "",
            }
        )
    return rows


def _seed_mode_label(mode: str) -> str:
    if mode == "append_only":
        return "Append seed data (no wipe)"
    if mode == "wipe_seed_tables_then_seed":
        return "Wipe seed tables then seed"
    return "Wipe operational data then seed (empty-db style)"


def _mask_secret(value: str, visible: int = 4) -> str:
    clean = (value or "").strip()
    if not clean:
        return "(not set)"
    if len(clean) <= visible:
        return "*" * len(clean)
    return f"{'*' * max(3, len(clean) - visible)}{clean[-visible:]}"


def _normalize_comp_dealer_domains_csv(value: str) -> tuple[str, list[str]]:
    tokens = str(value or "").replace("\n", ",").split(",")
    out: list[str] = []
    for token in tokens:
        clean = token.strip().lower()
        if not clean:
            continue
        if clean.startswith("https://") or clean.startswith("http://"):
            clean = (urlparse(clean).netloc or clean).lower()
        clean = clean.lstrip("www.")
        if not clean:
            continue
        if "/" in clean:
            clean = clean.split("/")[0].strip()
        if "." not in clean:
            continue
        if clean not in out:
            out.append(clean)
    return ",".join(out), out


def _parse_iso_naive(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _ebay_token_auto_refresh_diagnostics(repo: InventoryRepository) -> dict[str, Any]:
    now = utcnow_naive().replace(microsecond=0)
    interval_hours = max(
        1,
        min(72, int(get_runtime_int(repo, "ebay_user_token_auto_refresh_interval_hours", 12))),
    )
    min_ttl_minutes = max(
        5,
        min(240, int(get_runtime_int(repo, "ebay_user_token_auto_refresh_min_ttl_minutes", 45))),
    )
    failure_cooldown_minutes = max(
        1,
        min(
            24 * 60,
            int(get_runtime_int(repo, "ebay_user_token_auto_refresh_failure_cooldown_minutes", 30)),
        ),
    )
    refreshed_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_refreshed_at", ""))
    expires_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_expires_at", ""))
    failed_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_refresh_failed_at", ""))
    last_error = str(get_runtime_str(repo, "ebay_user_access_token_refresh_last_error", "") or "").strip()

    expires_in_minutes: int | None = None
    if expires_at is not None:
        expires_in_minutes = int((expires_at - now).total_seconds() // 60)
    next_refresh_due_at = (
        (refreshed_at + timedelta(hours=int(interval_hours))).replace(microsecond=0)
        if refreshed_at is not None
        else None
    )
    failure_cooldown_until = (
        (failed_at + timedelta(minutes=int(failure_cooldown_minutes))).replace(microsecond=0)
        if failed_at is not None
        else None
    )
    failure_cooldown_active = bool(
        failure_cooldown_until is not None and now < failure_cooldown_until
    )
    return {
        "now": now.isoformat(timespec="seconds"),
        "interval_hours": int(interval_hours),
        "min_ttl_minutes": int(min_ttl_minutes),
        "failure_cooldown_minutes": int(failure_cooldown_minutes),
        "refreshed_at": refreshed_at.isoformat(timespec="seconds") if refreshed_at else "",
        "expires_at": expires_at.isoformat(timespec="seconds") if expires_at else "",
        "expires_in_minutes": expires_in_minutes,
        "next_refresh_due_at": next_refresh_due_at.isoformat(timespec="seconds") if next_refresh_due_at else "",
        "failed_at": failed_at.isoformat(timespec="seconds") if failed_at else "",
        "failure_cooldown_until": (
            failure_cooldown_until.isoformat(timespec="seconds")
            if failure_cooldown_until
            else ""
        ),
        "failure_cooldown_active": bool(failure_cooldown_active),
        "last_error": last_error,
    }


def _clear_ebay_token_refresh_failure_state(repo: InventoryRepository, *, actor: str) -> None:
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key="ebay_user_access_token_refresh_failed_at",
        value="",
        value_type="str",
        description="Timestamp when eBay user token auto-refresh most recently failed.",
        actor=actor,
    )
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key="ebay_user_access_token_refresh_last_error",
        value="",
        value_type="str",
        description="Last eBay user token auto-refresh error message.",
        actor=actor,
    )


def _ebay_finding_recommended_runtime_settings() -> list[tuple[str, str, str, str]]:
    return [
        (
            "comp_ebay_max_calls_per_run",
            "3",
            "int",
            "Legacy Finding guardrail (inactive in primary comp path).",
        ),
        (
            "comp_ebay_max_calls_per_10m",
            "12",
            "int",
            "Legacy Finding rolling-window guardrail (inactive in primary comp path).",
        ),
        (
            "ebay_finding_rate_limit_cooldown_seconds",
            "600",
            "int",
            "Legacy Finding cooldown setting (inactive in primary comp path).",
        ),
        (
            "ebay_finding_rate_limit_severe_cooldown_seconds",
            "3600",
            "int",
            "Legacy Finding severe cooldown setting (inactive in primary comp path).",
        ),
        (
            "ebay_finding_rate_limit_probe_interval_seconds",
            "120",
            "int",
            "Legacy Finding cooldown probe interval (inactive in primary comp path).",
        ),
    ]


def _runtime_setting_seed_defaults() -> list[dict[str, str]]:
    return [
        {
            "key": "app_build_version",
            "value": settings.app_build_version,
            "value_type": "str",
            "description": "Application build version identifier (for deployment traceability).",
        },
        {
            "key": "app_build_sha",
            "value": settings.app_build_sha,
            "value_type": "str",
            "description": "Application build git SHA (for deployment traceability).",
        },
        {
            "key": "auth_query_token_fallback_enabled",
            "value": "true" if getattr(settings, "app_auth_query_token_fallback_enabled", True) else "false",
            "value_type": "bool",
            "description": "Enable URL `?auth=` remember-token fallback when cookie session persistence is unavailable.",
        },
        {
            "key": "comp_web_fallback_enabled",
            "value": "true" if settings.comp_web_fallback_enabled else "false",
            "value_type": "bool",
            "description": "Default web fallback behavior for Comp Tool when eBay comps are empty.",
        },
        {
            "key": "comp_ebay_max_calls_per_run",
            "value": "3",
            "value_type": "int",
            "description": "Legacy Finding guardrail (inactive in primary comp path).",
        },
        {
            "key": "comp_ebay_max_calls_per_10m",
            "value": "12",
            "value_type": "int",
            "description": "Legacy Finding rolling-window guardrail (inactive in primary comp path).",
        },
        {
            "key": "ebay_finding_rate_limit_cooldown_seconds",
            "value": str(int(getattr(settings, "ebay_finding_rate_limit_cooldown_seconds", 600))),
            "value_type": "int",
            "description": "Legacy Finding cooldown setting (inactive in primary comp path).",
        },
        {
            "key": "ebay_finding_rate_limit_probe_interval_seconds",
            "value": str(int(getattr(settings, "ebay_finding_rate_limit_probe_interval_seconds", 120))),
            "value_type": "int",
            "description": "Legacy Finding cooldown probe interval (inactive in primary comp path).",
        },
        {
            "key": "ebay_allow_sandbox_seller_ops",
            "value": "true" if settings.ebay_allow_sandbox_seller_ops else "false",
            "value_type": "bool",
            "description": "Allow seller operations in sandbox environment.",
        },
        {
            "key": "ebay_require_runbook_for_bulk_ops",
            "value": "false",
            "value_type": "bool",
            "description": "Require eBay Workspace runbook completion before bulk eBay Ops actions are enabled.",
        },
        {
            "key": "ebay_marketplace_id",
            "value": settings.ebay_marketplace_id,
            "value_type": "str",
            "description": "Default marketplace ID for eBay operations.",
        },
        {
            "key": "ebay_currency",
            "value": settings.ebay_currency,
            "value_type": "str",
            "description": "Default eBay listing currency.",
        },
        {
            "key": "ebay_content_language",
            "value": settings.ebay_content_language,
            "value_type": "str",
            "description": "Default eBay content language.",
        },
        {
            "key": "ebay_merchant_location_key",
            "value": settings.ebay_merchant_location_key,
            "value_type": "str",
            "description": "Default eBay merchant location key.",
        },
        {
            "key": "ebay_payment_policy_id",
            "value": settings.ebay_payment_policy_id,
            "value_type": "str",
            "description": "Default eBay payment policy ID.",
        },
        {
            "key": "ebay_fulfillment_policy_id",
            "value": settings.ebay_fulfillment_policy_id,
            "value_type": "str",
            "description": "Default eBay fulfillment policy ID.",
        },
        {
            "key": "ebay_return_policy_id",
            "value": settings.ebay_return_policy_id,
            "value_type": "str",
            "description": "Default eBay return policy ID.",
        },
        {
            "key": "ebay_category_id",
            "value": "",
            "value_type": "str",
            "description": "Default eBay category ID used by workspace/listing publish defaults.",
        },
        {
            "key": "ebay_listing_format_default",
            "value": "FIXED_PRICE",
            "value_type": "str",
            "description": "Default eBay listing format (`FIXED_PRICE` or `AUCTION`).",
        },
        {
            "key": "ebay_best_offer_default",
            "value": "false",
            "value_type": "bool",
            "description": "Default Best Offer toggle for fixed-price eBay listings.",
        },
        {
            "key": "ebay_auction_duration_default",
            "value": "DAYS_7",
            "value_type": "str",
            "description": "Default auction duration for eBay listing workflows.",
        },
        {
            "key": "ebay_auction_start_default",
            "value": "1.0",
            "value_type": "float",
            "description": "Default auction start price for eBay listing workflows.",
        },
        {
            "key": "ebay_auction_reserve_default",
            "value": "0.0",
            "value_type": "float",
            "description": "Default auction reserve price for eBay listing workflows.",
        },
        {
            "key": "ebay_auction_buy_now_default",
            "value": "0.0",
            "value_type": "float",
            "description": "Default auction Buy It Now price for eBay listing workflows.",
        },
        {
            "key": "ebay_workspace_store_profiles_json",
            "value": "{}",
            "value_type": "str",
            "description": "Persisted eBay workspace store/policy/listing-format profiles.",
        },
        {
            "key": "ebay_workspace_default_store_profile",
            "value": "",
            "value_type": "str",
            "description": "Default eBay workspace store profile alias loaded on workspace start.",
        },
        {
            "key": "ebay_auth_accepted_url",
            "value": settings.ebay_auth_accepted_url_effective,
            "value_type": "str",
            "description": "eBay developer portal accepted callback URL.",
        },
        {
            "key": "ebay_auth_declined_url",
            "value": settings.ebay_auth_declined_url_effective,
            "value_type": "str",
            "description": "eBay developer portal declined callback URL.",
        },
        {
            "key": "ebay_user_access_token",
            "value": settings.ebay_user_access_token,
            "value_type": "str",
            "description": "Default eBay user access token used in forms.",
        },
        {
            "key": "ebay_user_refresh_token",
            "value": settings.ebay_user_refresh_token,
            "value_type": "str",
            "description": "Default eBay user refresh token used for access token renewal.",
        },
        {
            "key": "ebay_user_token_auto_refresh_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable proactive eBay user token refresh in sync runner.",
        },
        {
            "key": "ebay_user_token_auto_refresh_interval_hours",
            "value": "12",
            "value_type": "int",
            "description": "Fallback max hours between proactive eBay user token refresh attempts.",
        },
        {
            "key": "ebay_user_token_auto_refresh_min_ttl_minutes",
            "value": "45",
            "value_type": "int",
            "description": "Refresh eBay user token when remaining TTL drops below this many minutes.",
        },
        {
            "key": "ebay_user_token_auto_refresh_failure_cooldown_minutes",
            "value": "30",
            "value_type": "int",
            "description": "Cooldown after eBay token auto-refresh failure before retrying again.",
        },
        {
            "key": "spot_price_provider",
            "value": settings.spot_price_provider,
            "value_type": "str",
            "description": "Spot provider (`yahoo_finance` or `metals_api`).",
        },
        {
            "key": "metals_api_base_url",
            "value": settings.metals_api_base_url,
            "value_type": "str",
            "description": "Metals API base URL runtime override.",
        },
        {
            "key": "metals_api_key",
            "value": settings.metals_api_key,
            "value_type": "str",
            "description": "Metals API key runtime override.",
        },
        {
            "key": "yahoo_finance_base_url",
            "value": settings.yahoo_finance_base_url,
            "value_type": "str",
            "description": "Yahoo chart base URL runtime override.",
        },
        {
            "key": "yahoo_symbol_gold",
            "value": settings.yahoo_symbol_gold,
            "value_type": "str",
            "description": "Yahoo symbol for gold spot.",
        },
        {
            "key": "yahoo_symbol_silver",
            "value": settings.yahoo_symbol_silver,
            "value_type": "str",
            "description": "Yahoo symbol for silver spot.",
        },
        {
            "key": "yahoo_symbol_platinum",
            "value": settings.yahoo_symbol_platinum,
            "value_type": "str",
            "description": "Yahoo symbol for platinum spot.",
        },
        {
            "key": "sync_job_ebay_orders_pull_import_enabled",
            "value": "true" if settings.sync_job_ebay_orders_pull_import_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable eBay order pull/import job.",
        },
        {
            "key": "sync_job_ebay_orders_pull_import_limit",
            "value": str(settings.sync_job_ebay_orders_pull_import_limit),
            "value_type": "int",
            "description": "Default limit for eBay pull/import worker runs.",
        },
        {
            "key": "sync_job_ebay_orders_pull_import_offset",
            "value": str(settings.sync_job_ebay_orders_pull_import_offset),
            "value_type": "int",
            "description": "Default offset for eBay pull/import worker runs.",
        },
        {
            "key": "sync_job_ebay_shipping_tracking_push_enabled",
            "value": "true" if settings.sync_job_ebay_shipping_tracking_push_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable eBay tracking push job.",
        },
        {
            "key": "sync_job_ebay_connection_health_check_enabled",
            "value": "true"
            if getattr(settings, "sync_job_ebay_connection_health_check_enabled", True)
            else "false",
            "value_type": "bool",
            "description": "Enable/disable scheduled eBay connection health-check job.",
        },
        {
            "key": "sync_job_ebay_connection_health_check_interval_minutes",
            "value": str(int(getattr(settings, "sync_job_ebay_connection_health_check_interval_minutes", 30))),
            "value_type": "int",
            "description": "Minimum minutes between scheduled eBay connection health checks.",
        },
        {
            "key": "sync_job_quickbooks_export_enabled",
            "value": "true" if settings.sync_job_quickbooks_export_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable QuickBooks export job scaffold.",
        },
        {
            "key": "sync_job_shopify_orders_pull_enabled",
            "value": "true" if settings.sync_job_shopify_orders_pull_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable Shopify pull job scaffold.",
        },
        {
            "key": "sync_job_shopify_orders_pull_shop_domain",
            "value": settings.sync_job_shopify_orders_pull_shop_domain,
            "value_type": "str",
            "description": "Default Shopify shop domain for pull jobs (my-shop.myshopify.com).",
        },
        {
            "key": "sync_job_shopify_orders_pull_access_token",
            "value": settings.sync_job_shopify_orders_pull_access_token,
            "value_type": "str",
            "description": "Default Shopify Admin API access token for pull jobs.",
        },
        {
            "key": "sync_job_shopify_orders_pull_limit",
            "value": str(settings.sync_job_shopify_orders_pull_limit),
            "value_type": "int",
            "description": "Default fetch limit for Shopify pull jobs.",
        },
        {
            "key": "sync_job_shopify_orders_pull_offset",
            "value": str(settings.sync_job_shopify_orders_pull_offset),
            "value_type": "int",
            "description": "Default fetch offset for Shopify pull jobs.",
        },
        {
            "key": "governance_snapshot_runner_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable/disable scheduled governance snapshot creation in sync runner.",
        },
        {
            "key": "governance_snapshot_interval_hours",
            "value": "24",
            "value_type": "int",
            "description": "Minimum hours between sync-runner governance snapshot events.",
        },
        {
            "key": "governance_snapshot_lookback_days",
            "value": "30",
            "value_type": "int",
            "description": "Lookback window for scheduled governance snapshot event counts.",
        },
        {
            "key": "governance_snapshot_max_rows_per_scope",
            "value": "2000",
            "value_type": "int",
            "description": "Max rows per governance scope sampled in scheduled snapshots.",
        },
        {
            "key": "backup_policy_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable scheduled backup policy reporting/tracking for this environment.",
        },
        {
            "key": "backup_policy_runner_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable scheduled backup execution by sync-runner.",
        },
        {
            "key": "backup_policy_schedule_timezone",
            "value": "America/Denver",
            "value_type": "str",
            "description": "IANA timezone used for scheduled backup execution.",
        },
        {
            "key": "backup_policy_schedule_local_time",
            "value": "02:00",
            "value_type": "str",
            "description": "Local-time HH:MM used for daily scheduled backup execution.",
        },
        {
            "key": "backup_policy_cadence_hours",
            "value": "24",
            "value_type": "int",
            "description": "Expected backup cadence in hours for compliance and readiness checks.",
        },
        {
            "key": "backup_policy_retention_days",
            "value": "30",
            "value_type": "int",
            "description": "Expected backup retention window in days.",
        },
        {
            "key": "backup_policy_upload_to_s3",
            "value": "true",
            "value_type": "bool",
            "description": "Whether backups should be uploaded to S3 by policy.",
        },
        {
            "key": "backup_restore_drill_interval_days",
            "value": "30",
            "value_type": "int",
            "description": "Maximum target days between successful restore drills.",
        },
        {
            "key": "backup_restore_rto_target_minutes",
            "value": "60",
            "value_type": "int",
            "description": "Target restore recovery-time objective (minutes) used for drill evidence.",
        },
        {
            "key": "backup_policy_owner",
            "value": "",
            "value_type": "str",
            "description": "Primary owner/team accountable for backup policy and drill execution.",
        },
        {
            "key": "comp_llm_system_message",
            "value": DEFAULT_COMP_SYSTEM_MESSAGE,
            "value_type": "str",
            "description": "System message for AI comp synthesis prompts.",
        },
        {
            "key": "comp_llm_instruction_template",
            "value": DEFAULT_COMP_INSTRUCTION,
            "value_type": "str",
            "description": "Instruction template for AI comp synthesis prompts.",
        },
        {
            "key": "listing_wizard_ai_system_message",
            "value": DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE,
            "value_type": "str",
            "description": "System message for Listing Wizard AI draft suggestions.",
        },
        {
            "key": "listing_wizard_ai_instruction_template",
            "value": DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE,
            "value_type": "str",
            "description": "Instruction template for Listing Wizard AI draft suggestions.",
        },
        {
            "key": "listing_wizard_ai_seed_default",
            "value": DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT,
            "value_type": "str",
            "description": "Default seed prompt pre-filled in Listing Wizard AI Draft Assist.",
        },
        {
            "key": "listing_wizard_ai_include_quick_comp_context",
            "value": "true",
            "value_type": "bool",
            "description": "When true, Listing Wizard AI tries to include quick eBay sold-comp and spot context.",
        },
        {
            "key": "listing_wizard_ai_quick_comp_limit",
            "value": "8",
            "value_type": "int",
            "description": "Max eBay sold-comp rows fetched for Listing Wizard AI quick pricing context.",
        },
        {
            "key": "purchase_doc_auto_apply_linked_lot_fields",
            "value": "false",
            "value_type": "bool",
            "description": "When true, inventory intake auto-applies extracted purchase-document fields to the linked lot accounting fields.",
        },
        {
            "key": "comp_reference_rules_context",
            "value": (
                "For comp analysis, prioritize sold comparables and clearly separate certified vs raw coins. "
                "When certified comps are present, compare within same grading service tier (PCGS/NGC/ANACS/ICG) "
                "and nearby grade bands; avoid mixing unlike grade populations without an explicit adjustment note. "
                "Call out when outliers, altered/cleaned coins, or weak title matches may distort pricing."
            ),
            "value_type": "str",
            "description": "Supplemental grading/comps rule context appended to comp prompts.",
        },
        {
            "key": "coin_grading_rules_context",
            "value": (
                "Use major third-party grading standards as reference context (PCGS, NGC, ANACS, ICG). "
                "Evaluate wear/friction, luster, strike quality, surface preservation, eye appeal, toning, "
                "and cleaning/damage indicators. Grade conservatively when uncertain."
            ),
            "value_type": "str",
            "description": "Supplemental grading standards context appended to grader prompts.",
        },
        {
            "key": "comp_web_fallback_limit",
            "value": "20",
            "value_type": "int",
            "description": "Default max web fallback result rows evaluated in Comp Tool.",
        },
        {
            "key": "comp_web_detail_fetch_limit",
            "value": "20",
            "value_type": "int",
            "description": "Default max web fallback links opened for detailed on-page price extraction.",
        },
        {
            "key": "documents_handoff_governance_review_mode",
            "value": "false",
            "value_type": "bool",
            "description": "When true, Admin governance clear-audit and preset-audit share one date preset/range.",
        },
        {
            "key": "listing_review_two_person_required",
            "value": "false",
            "value_type": "bool",
            "description": "Require a different user than reviewer when setting listing to active on configured channels.",
        },
        {
            "key": "listing_review_two_person_channels_csv",
            "value": "ebay",
            "value_type": "str",
            "description": "Comma-separated marketplaces where two-person review policy applies.",
        },
        {
            "key": "comp_dealer_domains_csv",
            "value": ",".join(DEFAULT_COMP_DEALER_DOMAINS),
            "value_type": "str",
            "description": "Comma-separated dealer domains used for comp parser/domain weighting.",
        },
        {
            "key": "coin_ref_paid_source_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable optional paid coin-reference source adapter contract (disabled by default).",
        },
        {
            "key": "coin_ref_paid_source_provider",
            "value": "none",
            "value_type": "str",
            "description": "Paid source provider key (`none`, `greysheet`).",
        },
        {
            "key": "coin_ref_paid_source_base_url",
            "value": "",
            "value_type": "str",
            "description": "Paid source API base URL (if licensed/in use).",
        },
        {
            "key": "coin_ref_paid_source_api_key",
            "value": "",
            "value_type": "str",
            "description": "Paid source API key/token (if licensed/in use).",
        },
        {
            "key": "coin_ref_paid_source_license_ack",
            "value": "false",
            "value_type": "bool",
            "description": "Set true only after legal/licensing approval for paid source usage.",
        },
        {
            "key": "coin_ref_paid_source_allow_prod",
            "value": "false",
            "value_type": "bool",
            "description": "Allow paid source usage in production environment (separate guardrail).",
        },
        {
            "key": "ai_voice_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable/disable voice features in AI chat/copilot surfaces.",
        },
        {
            "key": "ai_voice_stt_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable microphone speech-to-text prompt capture in AI chat.",
        },
        {
            "key": "ai_voice_tts_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable text-to-speech playback for AI responses.",
        },
        {
            "key": "ai_voice_provider",
            "value": "openai",
            "value_type": "str",
            "description": "Voice provider identifier (`openai` or `localai`).",
        },
        {
            "key": "ai_voice_base_url",
            "value": (settings.comp_llm_base_url or "https://api.openai.com/v1").strip().rstrip("/"),
            "value_type": "str",
            "description": "Voice provider base URL.",
        },
        {
            "key": "ai_voice_api_key",
            "value": settings.openai_api_key or "",
            "value_type": "str",
            "description": "Voice provider API key/token.",
        },
        {
            "key": "ai_voice_stt_model",
            "value": "gpt-4o-mini-transcribe",
            "value_type": "str",
            "description": "Speech-to-text model id.",
        },
        {
            "key": "ai_voice_stt_language",
            "value": "",
            "value_type": "str",
            "description": "Optional speech-to-text language hint (for example `en`).",
        },
        {
            "key": "ai_voice_tts_model",
            "value": "gpt-4o-mini-tts",
            "value_type": "str",
            "description": "Text-to-speech model id.",
        },
        {
            "key": "ai_voice_tts_voice",
            "value": "alloy",
            "value_type": "str",
            "description": "Text-to-speech voice id.",
        },
        {
            "key": "ai_voice_tts_response_format",
            "value": "mp3",
            "value_type": "str",
            "description": "Text-to-speech response format (`mp3` or `wav`).",
        },
        {
            "key": "ai_voice_timeout_seconds",
            "value": "45",
            "value_type": "int",
            "description": "Voice API timeout seconds.",
        },
        {
            "key": "ai_voice_tts_max_chars",
            "value": "1400",
            "value_type": "int",
            "description": "Maximum response chars to synthesize for one TTS call.",
        },
        {
            "key": "ai_domain_chat_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Ask GoldenStackers chat.",
        },
        {
            "key": "ai_domain_comp_tool_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Comp Tool features.",
        },
        {
            "key": "ai_domain_coin_grader_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Coin Grader features.",
        },
        {
            "key": "ai_domain_coin_identifier_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Coin Identifier features.",
        },
        {
            "key": "chat_ai_refine_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable/disable orchestration-backed AI refinement pass for Ask GoldenStackers responses.",
        },
        {
            "key": "chat_ai_refine_system_message",
            "value": (
                "You are GoldenStackers' read-only operations copilot. "
                "Preserve factual values from the provided draft answer and citations."
            ),
            "value_type": "str",
            "description": "System message used for Ask GoldenStackers AI refinement pass.",
        },
        {
            "key": "chat_ai_refine_instruction",
            "value": (
                "Rewrite the draft answer for clarity and operator usefulness. "
                "Do not invent values. Keep output concise markdown with short bullets."
            ),
            "value_type": "str",
            "description": "Instruction template used for Ask GoldenStackers AI refinement pass.",
        },
        {
            "key": "chat_mask_sensitive_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Master toggle for masking sensitive values in Ask GoldenStackers responses.",
        },
        {
            "key": "chat_mask_email_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Mask email addresses in Ask GoldenStackers responses.",
        },
        {
            "key": "chat_mask_phone_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Mask phone numbers in Ask GoldenStackers responses.",
        },
        {
            "key": "chat_mask_tracking_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Mask tracking numbers in Ask GoldenStackers responses.",
        },
        {
            "key": "ai_fallback_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable AI runtime fallback across active provider profiles.",
        },
        {
            "key": "ai_fallback_max_profiles",
            "value": "3",
            "value_type": "int",
            "description": "Maximum number of active AI runtime profiles to attempt per request.",
        },
        {
            "key": "ai_quality_title_min_words",
            "value": "3",
            "value_type": "int",
            "description": "Minimum words required before applying AI-suggested listing title text.",
        },
        {
            "key": "ai_quality_title_min_chars",
            "value": "12",
            "value_type": "int",
            "description": "Minimum chars required before applying AI-suggested listing title text.",
        },
        {
            "key": "ai_quality_listing_details_min_words",
            "value": "28",
            "value_type": "int",
            "description": "Minimum words required before applying AI-suggested listing details.",
        },
        {
            "key": "ai_quality_listing_details_min_chars",
            "value": "180",
            "value_type": "int",
            "description": "Minimum chars required before applying AI-suggested listing details.",
        },
        {
            "key": "ai_quality_intake_min_words",
            "value": "8",
            "value_type": "int",
            "description": "Minimum words required before applying AI-suggested intake text.",
        },
        {
            "key": "ai_quality_intake_min_chars",
            "value": "40",
            "value_type": "int",
            "description": "Minimum chars required before applying AI-suggested intake text.",
        },
        {
            "key": "ai_quality_forbidden_terms_csv",
            "value": "guaranteed profit,guaranteed return,risk-free,no risk,investment advice,financial advice",
            "value_type": "str",
            "description": "Comma-separated blocked words/phrases that prevent AI suggestion auto-apply.",
        },
        {
            "key": "ai_prompt_active_version_comp",
            "value": "",
            "value_type": "str",
            "description": "Active prompt registry version id for comp workflow.",
        },
        {
            "key": "ai_prompt_active_version_listing",
            "value": "",
            "value_type": "str",
            "description": "Active prompt registry version id for listing workflow.",
        },
        {
            "key": "ai_prompt_registry_comp_json",
            "value": "[]",
            "value_type": "json",
            "description": "Prompt registry history rows for comp workflow.",
        },
        {
            "key": "ai_prompt_registry_listing_json",
            "value": "[]",
            "value_type": "json",
            "description": "Prompt registry history rows for listing workflow.",
        },
        {
            "key": "google_integration_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Master toggle for Google Workspace integration features (Gmail/Calendar/Drive).",
        },
        {
            "key": "google_oauth_client_id",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth client ID for this environment.",
        },
        {
            "key": "google_oauth_client_secret",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth client secret for this environment.",
        },
        {
            "key": "google_oauth_redirect_uri",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth redirect URI for this environment.",
        },
        {
            "key": "google_workspace_scopes_csv",
            "value": "https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/drive.file",
            "value_type": "str",
            "description": "Comma-separated Google OAuth scopes requested by the app.",
        },
        {
            "key": "google_oauth_access_token",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth access token for API calls (runtime-managed credential).",
        },
        {
            "key": "google_oauth_refresh_token",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth refresh token for future token refresh flow.",
        },
        {
            "key": "google_default_sender_email",
            "value": "sales@goldenstackers.com",
            "value_type": "str",
            "description": "Default sender email used for Gmail invoice/receipt workflows.",
        },
        {
            "key": "google_drive_root_folder_id",
            "value": "",
            "value_type": "str",
            "description": "Optional default Google Drive folder ID for exports/uploads.",
        },
        {
            "key": "google_default_calendar_id",
            "value": "primary",
            "value_type": "str",
            "description": "Default Google Calendar ID for follow-up event creation.",
        },
        {
            "key": "google_default_timezone",
            "value": "America/Denver",
            "value_type": "str",
            "description": "Default timezone for Google Calendar event scheduling.",
        },
        {
            "key": "app_default_timezone",
            "value": "America/Denver",
            "value_type": "str",
            "description": "Default app timezone for local display/scheduling defaults.",
        },
        {
            "key": "google_http_timeout_seconds",
            "value": "30",
            "value_type": "int",
            "description": "Timeout for Google API HTTP requests.",
        },
        {
            "key": "google_queue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Google integration retry queue for failed actions.",
        },
        {
            "key": "google_queue_max_retries",
            "value": "5",
            "value_type": "int",
            "description": "Maximum retry attempts per queued Google integration action.",
        },
        {
            "key": "google_queue_backoff_base_seconds",
            "value": "120",
            "value_type": "int",
            "description": "Base backoff seconds for exponential retry scheduling.",
        },
        {
            "key": "google_queue_backoff_max_seconds",
            "value": "3600",
            "value_type": "int",
            "description": "Maximum backoff seconds for queued retries.",
        },
        {
            "key": "shipping_queue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable shipping integration retry queue execution.",
        },
        {
            "key": "shipping_queue_max_retries",
            "value": "5",
            "value_type": "int",
            "description": "Default max retries for queued shipping label purchase jobs.",
        },
        {
            "key": "shipping_queue_backoff_base_seconds",
            "value": "60",
            "value_type": "int",
            "description": "Base backoff seconds for shipping queue retry scheduling.",
        },
        {
            "key": "shipping_queue_backoff_max_seconds",
            "value": "3600",
            "value_type": "int",
            "description": "Maximum backoff seconds for shipping queue retries.",
        },
        {
            "key": "shipping_label_purchase_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable shipping label purchase queue actions.",
        },
        {
            "key": "shipping_label_live_provider_calls_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Guardrail toggle for live external label-purchase API calls.",
        },
        {
            "key": "shipping_label_provider_pirateship_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Pirate Ship as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_ebay_shipping_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable eBay Shipping as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_usps_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable USPS as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_ups_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable UPS as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_fedex_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable FedEx as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_other_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable generic/other shipping label provider jobs.",
        },
        {
            "key": "shipping_label_pirateship_mode",
            "value": "mock",
            "value_type": "str",
            "description": "Pirate Ship adapter mode (`mock` or `api`) for live-provider execution path.",
        },
        {
            "key": "shipping_label_pirateship_base_url",
            "value": "",
            "value_type": "str",
            "description": "Pirate Ship adapter base URL for API mode.",
        },
        {
            "key": "shipping_label_pirateship_api_key",
            "value": "",
            "value_type": "str",
            "description": "Pirate Ship adapter API key/token for API mode.",
        },
        {
            "key": "shipping_label_pirateship_endpoint_path",
            "value": "/v1/labels/purchase",
            "value_type": "str",
            "description": "Pirate Ship adapter endpoint path (joined with base URL).",
        },
        {
            "key": "shipping_label_pirateship_auth_scheme",
            "value": "bearer",
            "value_type": "str",
            "description": "Pirate Ship auth scheme (`bearer` or `token`).",
        },
        {
            "key": "shipping_label_pirateship_timeout_seconds",
            "value": "20",
            "value_type": "int",
            "description": "Pirate Ship API timeout seconds for live mode.",
        },
        {
            "key": "invoicing_tax_jurisdiction",
            "value": "Golden, Colorado",
            "value_type": "str",
            "description": "Default jurisdiction label for invoice/receipt tax display.",
        },
        {
            "key": "invoicing_tax_rate_percent_default",
            "value": "7.50",
            "value_type": "str",
            "description": "Default local sales-tax rate percent used by Documents/Reports tax calculators (Golden, CO local profile).",
        },
        {
            "key": "invoicing_tax_shipping_taxable_default",
            "value": "false",
            "value_type": "bool",
            "description": "Default toggle for whether shipping is taxable in Documents tax calculator.",
        },
        {
            "key": "invoicing_tax_exempt_categories_csv",
            "value": "bullion,coins",
            "value_type": "str",
            "description": "Comma-separated product categories treated as tax-exempt in auto tax mode.",
        },
        {
            "key": "marketplace_facilitator_channels_csv",
            "value": "ebay",
            "value_type": "str",
            "description": "Comma-separated marketplaces that collect/remit sales tax as facilitator channels (excluded by default in local tax-liability report scope).",
        },
        {
            "key": "slack_notifications_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Master toggle for Slack notifications.",
        },
        {
            "key": "slack_bot_token",
            "value": "",
            "value_type": "str",
            "description": "Slack Bot OAuth token used for posting notifications.",
        },
        {
            "key": "slack_signing_secret",
            "value": "",
            "value_type": "str",
            "description": "Slack signing secret for future interactive/event verification.",
        },
        {
            "key": "slack_default_channel",
            "value": "",
            "value_type": "str",
            "description": "Default Slack channel for operational notifications (for example #ops-alerts).",
        },
        {
            "key": "slack_notify_sync_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications for sync failures/partial runs.",
        },
        {
            "key": "slack_notify_shipping_exceptions",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications for shipping exceptions.",
        },
        {
            "key": "slack_notify_order_imports",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications when new eBay orders are imported.",
        },
        {
            "key": "slack_notify_daily_summary",
            "value": "false",
            "value_type": "bool",
            "description": "Send one daily Slack operational summary message.",
        },
        {
            "key": "slack_daily_report_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable sync-runner scheduled daily operations report delivery.",
        },
        {
            "key": "slack_daily_report_timezone",
            "value": "America/Denver",
            "value_type": "str",
            "description": "IANA timezone used by daily ops report scheduler.",
        },
        {
            "key": "slack_daily_report_local_time",
            "value": "08:00",
            "value_type": "str",
            "description": "Local HH:MM trigger for daily ops report scheduler.",
        },
        {
            "key": "slack_daily_report_channel",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for scheduled daily ops report.",
        },
        {
            "key": "slack_daily_report_normalized_fee_coverage_lookback_weeks",
            "value": "8",
            "value_type": "int",
            "description": "Lookback window in weeks for normalized eBay fee-source coverage health in daily ops reports.",
        },
        {
            "key": "slack_daily_report_normalized_fee_coverage_threshold_pct",
            "value": "80",
            "value_type": "float",
            "description": "Minimum weekly normalized fee-source coverage percent before daily-report health alerting triggers.",
        },
        {
            "key": "slack_daily_report_normalized_fee_coverage_consecutive_weeks",
            "value": "2",
            "value_type": "int",
            "description": "Number of consecutive below-threshold weeks required before daily-report fee coverage alert is triggered.",
        },
        {
            "key": "slack_notify_backup_success",
            "value": "false",
            "value_type": "bool",
            "description": "Send Slack notification when scheduled backup succeeds.",
        },
        {
            "key": "slack_notify_backup_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notification when scheduled backup fails.",
        },
        {
            "key": "slack_channel_backup_events",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for backup success/failure notifications.",
        },
        {
            "key": "slack_channel_business_reports",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for manual business status report notifications.",
        },
        {
            "key": "slack_channel_order_imports",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for new eBay order import notifications.",
        },
        {
            "key": "slack_template_backup_success",
            "value": ":white_check_mark: *GoldenStackers* scheduled DB backup completed\n- Env: `{env}`\n- File: `{file_name}`\n- Size: `{size_bytes}` bytes\n- Uploaded to S3: `{uploaded_to_s3}`\n- S3 Key: `{s3_key}`\n- Local Time: `{local_time}`",
            "value_type": "str",
            "description": "Template for successful scheduled backup notifications.",
        },
        {
            "key": "slack_template_backup_failure",
            "value": ":x: *GoldenStackers* scheduled DB backup failed\n- Env: `{env}`\n- Error: `{error}`\n- Local Time: `{local_time}`",
            "value_type": "str",
            "description": "Template for failed scheduled backup notifications.",
        },
        {
            "key": "slack_template_business_status_report",
            "value": ":bar_chart: *GoldenStackers Business Status* (`{env}`)\n- Window: `{window_days}` day(s)\n- Sales: `{sales_window_count}` | Gross: `${gross_window}` | Net: `${net_window}`\n- Orders: `{orders_window_count}`\n- Listings: `{listing_count}` (active `{active_count}`, draft `{draft_count}`)\n- Low stock: `{low_stock_count}` | Unlisted: `{unlisted_count}`\n- As of UTC: `{as_of_utc}`",
            "value_type": "str",
            "description": "Template for manual business status report notifications.",
        },
        {
            "key": "slack_template_inventory_risk_report",
            "value": ":package: *GoldenStackers Inventory Risk* (`{env}`)\n- Low stock items: `{low_stock_count}`\n- Unlisted products: `{unlisted_count}`\n- Draft listings: `{draft_count}`\n- Active listings: `{active_count}`\n- Inventory cost basis: `${inventory_cost}`\n- As of UTC: `{as_of_utc}`",
            "value_type": "str",
            "description": "Template for manual inventory risk report notifications.",
        },
        {
            "key": "slack_template_order_imported",
            "value": ":package: *New eBay order imported*\n- Env: `{env}`\n- Order: `{order_id}`\n- Buyer: `{buyer}`\n- Status: `{status}`\n- Total: `${total}` (shipping `${shipping}`, tax `${tax}`)\n- Items: `{line_item_count}`\n- Shipping service: `{shipping_service}`\n- Ship to: `{shipping_address}`\n- Created: `{created_at}`",
            "value_type": "str",
            "description": "Template for new eBay order imported notifications.",
        },
        {
            "key": "slack_notify_google_queue_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications when Google integration queue jobs hit terminal failure.",
        },
        {
            "key": "slack_notify_integration_queue_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications when any integration queue job hits terminal failure.",
        },
        {
            "key": "slack_notify_ebay_oauth_refresh_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications when sync-runner eBay OAuth auto-refresh fails.",
        },
        {
            "key": "slack_notify_parity_decisions",
            "value": "false",
            "value_type": "bool",
            "description": "Send Slack notifications when workspace parity release decisions are recorded.",
        },
        {
            "key": "slack_notify_followup_overdue",
            "value": "false",
            "value_type": "bool",
            "description": "Allow sending Slack notifications for overdue workspace rollout follow-up tasks.",
        },
        {
            "key": "slack_notify_system_health_critical",
            "value": "false",
            "value_type": "bool",
            "description": "Send Slack notifications when System Health critical-signal thresholds are breached.",
        },
        {
            "key": "slack_daily_summary_cron",
            "value": "0 16 * * *",
            "value_type": "str",
            "description": "Cron expression for daily summary schedule (UTC).",
        },
        {
            "key": "notification_route_sync_failures",
            "value": "slack",
            "value_type": "str",
            "description": "Notification route for sync failure events (`slack`, `email`, `both`, `disabled`).",
        },
        {
            "key": "notification_route_daily_report",
            "value": "slack",
            "value_type": "str",
            "description": "Notification route for daily report events (`slack`, `email`, `both`, `disabled`).",
        },
        {
            "key": "notification_route_backup_events",
            "value": "slack",
            "value_type": "str",
            "description": "Notification route for backup success/failure events (`slack`, `email`, `both`, `disabled`).",
        },
        {
            "key": "notification_route_system_health_critical",
            "value": "slack",
            "value_type": "str",
            "description": "Notification route for system-health critical events (`slack`, `email`, `both`, `disabled`).",
        },
        {
            "key": "notification_route_business_reports",
            "value": "slack",
            "value_type": "str",
            "description": "Notification route for manual business status reports (`slack`, `email`, `both`, `disabled`).",
        },
        {
            "key": "notification_email_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable notification email delivery pipeline (future Google/email integration).",
        },
        {
            "key": "notification_email_recipients_csv",
            "value": "",
            "value_type": "str",
            "description": "Default comma-separated notification email recipients (future email delivery).",
        },
        {
            "key": "slack_http_timeout_seconds",
            "value": "15",
            "value_type": "int",
            "description": "Timeout for Slack API requests.",
        },
        {
            "key": "slack_queue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Slack delivery retry queue on post failures.",
        },
        {
            "key": "slack_queue_max_retries",
            "value": "5",
            "value_type": "int",
            "description": "Maximum retry attempts per queued Slack delivery.",
        },
        {
            "key": "slack_queue_backoff_base_seconds",
            "value": "60",
            "value_type": "int",
            "description": "Base backoff seconds for Slack retry queue scheduling.",
        },
        {
            "key": "slack_queue_backoff_max_seconds",
            "value": "3600",
            "value_type": "int",
            "description": "Maximum backoff seconds for Slack retry queue scheduling.",
        },
        {
            "key": "integration_automation_dry_run_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "When true, automation rules are evaluated/logged but rule effects are not persisted.",
        },
        {
            "key": "integration_automation_execute_approval_required_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "When true, rules marked requires_approval may auto-apply in execution engine.",
        },
        {
            "key": "health_queue_execute_exceptions_warn_24h",
            "value": "1",
            "value_type": "int",
            "description": "System Health warning threshold (24h) for queue execute exceptions.",
        },
        {
            "key": "health_queue_execute_exceptions_critical_24h",
            "value": "5",
            "value_type": "int",
            "description": "System Health critical threshold (24h) for queue execute exceptions.",
        },
        {
            "key": "health_terminal_queue_failures_warn_24h",
            "value": "1",
            "value_type": "int",
            "description": "System Health warning threshold (24h) for terminal integration queue failures.",
        },
        {
            "key": "health_terminal_queue_failures_critical_24h",
            "value": "3",
            "value_type": "int",
            "description": "System Health critical threshold (24h) for terminal integration queue failures.",
        },
        {
            "key": "health_integration_warnings_warn_24h",
            "value": "10",
            "value_type": "int",
            "description": "System Health warning threshold (24h) for integration warning events.",
        },
        {
            "key": "health_integration_warnings_critical_24h",
            "value": "30",
            "value_type": "int",
            "description": "System Health critical threshold (24h) for integration warning events.",
        },
        {
            "key": "runbook_queue_execute_exceptions_url",
            "value": "",
            "value_type": "str",
            "description": "Runbook URL for queue execute exception remediation.",
        },
        {
            "key": "runbook_terminal_queue_failures_url",
            "value": "",
            "value_type": "str",
            "description": "Runbook URL for terminal queue failure remediation.",
        },
        {
            "key": "runbook_integration_warnings_url",
            "value": "",
            "value_type": "str",
            "description": "Runbook URL for elevated integration warning remediation.",
        },
        {
            "key": "go_live_readiness_weight_checklist_gap_pct",
            "value": "40",
            "value_type": "int",
            "description": "Weight (percent) applied to checklist completion gap in go-live readiness score.",
        },
        {
            "key": "go_live_readiness_weight_env_missing",
            "value": "5",
            "value_type": "int",
            "description": "Per-key penalty for missing required environment keys in readiness score.",
        },
        {
            "key": "go_live_readiness_weight_runtime_missing",
            "value": "5",
            "value_type": "int",
            "description": "Per-key penalty for missing/inactive required runtime keys in readiness score.",
        },
        {
            "key": "go_live_readiness_weight_terminal_queue_failure",
            "value": "10",
            "value_type": "int",
            "description": "Per-failure penalty for terminal queue failures (24h), capped by max setting.",
        },
        {
            "key": "go_live_readiness_weight_queue_execute_exception",
            "value": "5",
            "value_type": "int",
            "description": "Per-exception penalty for queue execute exceptions (24h), capped by max setting.",
        },
        {
            "key": "go_live_readiness_penalty_terminal_queue_failure_max",
            "value": "30",
            "value_type": "int",
            "description": "Maximum total penalty applied for terminal queue failures.",
        },
        {
            "key": "go_live_readiness_penalty_queue_execute_exception_max",
            "value": "20",
            "value_type": "int",
            "description": "Maximum total penalty applied for queue execute exceptions.",
        },
        {
            "key": "go_live_readiness_penalty_integration_warnings_warn",
            "value": "10",
            "value_type": "int",
            "description": "Penalty applied when 24h integration warnings exceed warn threshold.",
        },
        {
            "key": "go_live_readiness_penalty_integration_warnings_critical",
            "value": "20",
            "value_type": "int",
            "description": "Penalty applied when 24h integration warnings exceed critical threshold.",
        },
        {
            "key": "go_live_readiness_threshold_green",
            "value": "85",
            "value_type": "int",
            "description": "Minimum readiness score for GREEN state.",
        },
        {
            "key": "go_live_readiness_threshold_yellow",
            "value": "65",
            "value_type": "int",
            "description": "Minimum readiness score for YELLOW state (below this is RED).",
        },
        {
            "key": "health_auto_alert_critical_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable automatic System Health critical-signal alert dispatch.",
        },
        {
            "key": "health_auto_alert_cooldown_minutes",
            "value": "60",
            "value_type": "int",
            "description": "Cooldown minutes before repeating identical System Health critical alerts.",
        },
        {
            "key": "slack_channel_sync_failures",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for sync failure alerts.",
        },
        {
            "key": "slack_channel_google_queue_failures",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for Google queue failure alerts.",
        },
        {
            "key": "slack_channel_integration_queue_failures",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for generic integration queue failure alerts.",
        },
        {
            "key": "slack_channel_parity_decision",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for parity release decision alerts.",
        },
        {
            "key": "slack_channel_followup_overdue",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for overdue rollout follow-up alerts.",
        },
        {
            "key": "slack_channel_warning",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for warning-severity alerts.",
        },
        {
            "key": "slack_channel_error",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for error-severity alerts.",
        },
        {
            "key": "slack_channel_critical",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for critical-severity alerts.",
        },
        {
            "key": "slack_channel_system_health_critical",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for System Health critical alerts.",
        },
        {
            "key": "slack_template_sync_failures",
            "value": (
                ":warning: *GoldenStackers* sync run `{job_name}` `{status}`\n"
                "- Env: `{env}`\n"
                "- Run: `#{run_id}`\n"
                "- Processed: `{processed}`\n"
                "- Failed: `{failed}`\n"
                "- Actor: `{actor}`"
            ),
            "value_type": "str",
            "description": "Template for sync failure/partial Slack alerts.",
        },
        {
            "key": "slack_template_google_queue_failures",
            "value": (
                ":warning: *GoldenStackers* Google queue job failed permanently\n"
                "- Env: `{env}`\n"
                "- Job: `#{job_id}` `{action}`\n"
                "- Retries: `{retry_count}/{max_retries}`\n"
                "- Error: `{error}`"
            ),
            "value_type": "str",
            "description": "Template for terminal Google queue failure Slack alerts.",
        },
        {
            "key": "slack_template_integration_queue_failures",
            "value": (
                ":warning: *GoldenStackers* integration queue job failed permanently\n"
                "- Env: `{env}`\n"
                "- Integration: `{integration}`\n"
                "- Job: `#{job_id}` `{action}`\n"
                "- Retries: `{retry_count}/{max_retries}`\n"
                "- Error: `{error}`"
            ),
            "value_type": "str",
            "description": "Template for terminal integration queue failure Slack alerts.",
        },
        {
            "key": "slack_template_parity_decision",
            "value": (
                ":clipboard: *GoldenStackers* parity release decision `{decision}`\n"
                "- Env: `{env}`\n"
                "- Snapshot: `#{snapshot_id}`\n"
                "- Actor: `{actor}`\n"
                "- Note: `{note}`"
            ),
            "value_type": "str",
            "description": "Template for workspace parity release decision alerts.",
        },
        {
            "key": "slack_template_followup_overdue",
            "value": (
                ":rotating_light: *GoldenStackers* rollout follow-up overdue\n"
                "- Env: `{env}`\n"
                "- Task: `{task_key}`\n"
                "- Title: `{title}`\n"
                "- Owner: `{owner}`\n"
                "- Due: `{due_date}`\n"
                "- Priority: `{priority}`"
            ),
            "value_type": "str",
            "description": "Template for overdue workspace rollout follow-up alerts.",
        },
        {
            "key": "slack_template_system_health_critical",
            "value": (
                ":rotating_light: *GoldenStackers* System Health critical signals detected\n"
                "- Env: `{env}`\n"
                "- Critical Signals: `{critical_signals}`\n"
                "- Queue Execute Exceptions: `{queue_execute_exceptions}`\n"
                "- Terminal Queue Failures: `{terminal_queue_failures}`\n"
                "- Integration Warnings: `{integration_warnings}`"
            ),
            "value_type": "str",
            "description": "Template for System Health critical threshold alerts.",
        },
        {
            "key": "ux_workspace_ebay_enabled",
            "value": "true" if settings.ux_workspace_ebay_enabled else "false",
            "value_type": "bool",
            "description": "Enable consolidated eBay Workspace UX controls and defaults.",
        },
        {
            "key": "ux_workspace_inventory_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Inventory workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_workspace_fulfillment_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Fulfillment workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_workspace_sync_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Sync workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_workspace_revenue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Revenue workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_listings_auto_photo_comp_review_preset",
            "value": "false",
            "value_type": "bool",
            "description": "Auto-apply Listings Photo-Comp Review Queue preset once per user session.",
        },
        {
            "key": "ux_navigation_mode",
            "value": "unified",
            "value_type": "str",
            "description": "Navigation rollout mode (`unified` or `legacy`).",
        },
        {
            "key": "ux_navigation_telemetry_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable navigation telemetry audit events (page views/switches) for IA tuning.",
        },
        {
            "key": "ux_role_default_landing_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable role-based default landing redirect from Home page.",
        },
        {
            "key": "ux_navigation_window_start_iso",
            "value": "",
            "value_type": "str",
            "description": "Optional telemetry window lower-bound timestamp (ISO UTC) for nav analytics.",
        },
        {
            "key": "ux_readiness_weight_permission_gap",
            "value": "12",
            "value_type": "int",
            "description": "Readiness score penalty per permission gap workflow.",
        },
        {
            "key": "ux_readiness_weight_audit_gap",
            "value": "8",
            "value_type": "int",
            "description": "Readiness score penalty per missing-audit-evidence workflow.",
        },
        {
            "key": "ux_readiness_weight_overdue_followup",
            "value": "5",
            "value_type": "int",
            "description": "Readiness score penalty per overdue open follow-up task.",
        },
        {
            "key": "ux_readiness_weight_task_gap",
            "value": "6",
            "value_type": "int",
            "description": "Readiness score penalty per workflow missing task-completion evidence.",
        },
        {
            "key": "ux_readiness_penalty_rejected_decision",
            "value": "25",
            "value_type": "int",
            "description": "Readiness score penalty when latest release decision is rejected.",
        },
        {
            "key": "ux_readiness_penalty_missing_decision",
            "value": "10",
            "value_type": "int",
            "description": "Readiness score penalty when no latest approved/rejected decision is present.",
        },
        {
            "key": "ux_parity_min_task_completion_events",
            "value": "1",
            "value_type": "int",
            "description": "Minimum workspace task-completion events required in parity lookback window per workflow.",
        },
    ]


def _build_env_coverage_rows(env_values: dict[str, str], recommended_defaults: dict[str, str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    keys = sorted(set(recommended_defaults.keys()) | set(env_values.keys()))
    for key in keys:
        current = str(env_values.get(key, ""))
        recommended = str(recommended_defaults.get(key, ""))
        present = key in env_values
        is_empty = present and not current.strip()
        if not present:
            status = "missing"
        elif is_empty:
            status = "empty"
        elif key in recommended_defaults and current.strip() == recommended.strip():
            status = "default"
        else:
            status = "set"
        rows.append(
            {
                "key": key,
                "status": status,
                "tracked": bool(key in recommended_defaults),
                "present_in_env": bool(present),
                "editable": bool(is_editable_env_key(key)),
                "is_sensitive": bool(key in SENSITIVE_ENV_KEYS),
                "current_value": mask_env_value(key, current),
                "recommended_default": mask_env_value(key, recommended),
                "current_raw_len": len(current),
            }
        )
    return rows


def _apply_slack_channel_presets(repo: InventoryRepository, *, actor: str, env_name: str) -> int:
    env = str(env_name or settings.app_env or "local").strip().lower()
    defaults = {
        "slack_default_channel": f"#gs-{env}-ops",
        "slack_channel_sync_failures": f"#gs-{env}-sync",
        "slack_channel_order_imports": f"#gs-{env}-orders",
        "slack_channel_google_queue_failures": f"#gs-{env}-integrations",
        "slack_channel_warning": f"#gs-{env}-warn",
        "slack_channel_error": f"#gs-{env}-error",
        "slack_channel_critical": f"#gs-{env}-critical",
    }
    updated = 0
    for key, value in defaults.items():
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key=key,
            value=value,
            value_type="str",
            description="Auto-applied Slack channel preset.",
            is_active=True,
            actor=actor,
        )
        updated += 1
    return updated


def _health_label_and_emoji(ratio: float) -> tuple[str, str]:
    state = health_state(ratio)
    if state == "healthy":
        return "healthy", "green"
    if state == "warning":
        return "warning", "orange"
    return "critical", "red"


def _apply_required_env_defaults(
    *,
    env_path: str,
    required_keys: set[str],
    env_values: dict[str, str],
    recommended_defaults: dict[str, str],
) -> int:
    updated = 0
    for key in sorted(required_keys):
        current = str(env_values.get(key, ""))
        needs_fix = key not in env_values or not current.strip()
        if not needs_fix:
            continue
        if key not in recommended_defaults:
            continue
        upsert_env_key(env_path, key, str(recommended_defaults.get(key, "")))
        updated += 1
    return updated


def _apply_all_env_defaults(
    *,
    env_path: str,
    env_values: dict[str, str],
    recommended_defaults: dict[str, str],
) -> int:
    updated = 0
    for key in sorted(recommended_defaults.keys()):
        current = str(env_values.get(key, ""))
        needs_fix = key not in env_values or not current.strip()
        if not needs_fix:
            continue
        upsert_env_key(env_path, key, str(recommended_defaults.get(key, "")))
        updated += 1
    return updated


def _apply_required_runtime_defaults(
    *,
    repo: InventoryRepository,
    actor: str,
    required_keys: set[str],
    runtime_rows: list,
    seed_defaults: list[dict[str, str]],
) -> int:
    by_key = {str(row.key): row for row in runtime_rows}
    defaults_by_key = {str(item["key"]): item for item in seed_defaults}
    updated = 0
    for key in sorted(required_keys):
        default_item = defaults_by_key.get(key)
        if default_item is None:
            continue
        row = by_key.get(key)
        if row is None:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(default_item["value"]),
                value_type=str(default_item["value_type"]),
                description=str(default_item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
            continue
        if not bool(row.is_active):
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(row.value or default_item["value"]),
                value_type=str(row.value_type or default_item["value_type"]),
                description=str(row.description or default_item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
    return updated


def _apply_all_runtime_defaults(
    *,
    repo: InventoryRepository,
    actor: str,
    runtime_rows: list,
    seed_defaults: list[dict[str, str]],
) -> int:
    by_key = {str(row.key): row for row in runtime_rows}
    updated = 0
    for item in seed_defaults:
        key = str(item.get("key") or "")
        if not key:
            continue
        row = by_key.get(key)
        if row is None:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(item.get("value") or ""),
                value_type=str(item.get("value_type") or "str"),
                description=str(item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
            continue
        if not bool(row.is_active):
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(row.value or item.get("value") or ""),
                value_type=str(row.value_type or item.get("value_type") or "str"),
                description=str(row.description or item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
    return updated


def _build_runtime_coverage_rows(runtime_rows: list, seed_defaults: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {str(row.key): row for row in runtime_rows}
    tracked_keys: set[str] = set()
    rows: list[dict[str, str]] = []
    for item in seed_defaults:
        key = str(item.get("key") or "")
        tracked_keys.add(key)
        expected_value = str(item.get("value") or "")
        expected_type = str(item.get("value_type") or "str")
        row = by_key.get(key)
        if row is None:
            status = "missing"
            current_value = ""
            current_type = expected_type
            is_active = False
            updated_by = ""
            updated_at = ""
        else:
            current_value = str(row.value or "")
            current_type = str(row.value_type or "")
            is_active = bool(row.is_active)
            updated_by = str(row.updated_by or "")
            updated_at = row.updated_at.isoformat() if row.updated_at else ""
            if not is_active:
                status = "inactive"
            elif current_value.strip() == expected_value.strip() and current_type == expected_type:
                status = "default"
            else:
                status = "overridden"
        rows.append(
            {
                "key": key,
                "status": status,
                "expected_type": expected_type,
                "current_type": current_type,
                "expected_default": expected_value,
                "current_value": current_value,
                "is_active": bool(is_active),
                "updated_by": updated_by,
                "updated_at": updated_at,
                "description": str(item.get("description") or ""),
            }
        )
    for key, row in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        if key in tracked_keys:
            continue
        rows.append(
            {
                "key": key,
                "status": "custom_untracked",
                "expected_type": "",
                "current_type": str(row.value_type or ""),
                "expected_default": "",
                "current_value": str(row.value or ""),
                "is_active": bool(row.is_active),
                "updated_by": str(row.updated_by or ""),
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                "description": str(row.description or ""),
            }
        )
    return rows


def _seed_missing_runtime_defaults(repo: InventoryRepository, *, actor: str, seed_defaults: list[dict[str, str]]) -> int:
    seeded = 0
    for item in seed_defaults:
        try:
            existing = repo.get_runtime_setting(
                environment=settings.app_env,
                key=item["key"],
                active_only=False,
            )
            if existing is not None:
                continue
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=item["key"],
                value=item["value"],
                value_type=item["value_type"],
                description=item["description"],
                is_active=True,
                actor=actor,
            )
            seeded += 1
        except Exception:
            continue
    return seeded


def _render_comp_dealer_domains_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Comp Dealer Domains")
    st.caption(
        "Manage dealer domains used by Comp Tool parser confidence and domain-specific extraction."
    )
    current_domain_setting = repo.get_runtime_setting(
        environment=settings.app_env,
        key="comp_dealer_domains_csv",
        active_only=False,
    )
    current_domain_value = (
        (current_domain_setting.value if current_domain_setting is not None else ",".join(DEFAULT_COMP_DEALER_DOMAINS))
        or ",".join(DEFAULT_COMP_DEALER_DOMAINS)
    )
    normalized_preview_csv, normalized_preview = _normalize_comp_dealer_domains_csv(current_domain_value)
    st.caption(f"Current normalized domains: {len(normalized_preview)}")
    with st.form("admin_comp_dealer_domains_form"):
        comp_domains_text = st.text_area(
            "Dealer Domains (comma-separated)",
            value=current_domain_value,
            height=120,
            help="Example: apmex.com,jmbullion.com,sdbullion.com",
        )
        normalized_input_csv, normalized_input = _normalize_comp_dealer_domains_csv(comp_domains_text)
        st.caption(f"Normalized preview ({len(normalized_input)}): {', '.join(normalized_input[:20])}")
        if len(normalized_input) > 20:
            st.caption(f"... and {len(normalized_input) - 20} more")
        comp_domains_is_active = st.checkbox(
            "Active",
            value=bool(current_domain_setting.is_active) if current_domain_setting is not None else True,
        )
        cd1, cd2 = st.columns(2)
        with cd1:
            comp_domains_save = st.form_submit_button("Save Dealer Domains")
        with cd2:
            comp_domains_reset = st.form_submit_button("Reset To Defaults")
    if comp_domains_save:
        if not normalized_input:
            st.error("Provide at least one valid domain (example: apmex.com).")
            return
        try:
            row = repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="comp_dealer_domains_csv",
                value=normalized_input_csv,
                value_type="str",
                description="Comma-separated dealer domains used for comp parser/domain weighting.",
                is_active=bool(comp_domains_is_active),
                actor=user.username,
            )
            st.success(f"Saved `{row.key}`.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save dealer domains: {exc}")
    if comp_domains_reset:
        try:
            row = repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="comp_dealer_domains_csv",
                value=",".join(DEFAULT_COMP_DEALER_DOMAINS),
                value_type="str",
                description="Comma-separated dealer domains used for comp parser/domain weighting.",
                is_active=True,
                actor=user.username,
            )
            st.success(f"Reset `{row.key}` to defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to reset dealer domains: {exc}")


def _render_comp_photo_retry_telemetry(repo: InventoryRepository, user) -> None:
    st.markdown("### Photo-Comp Retry Telemetry")
    st.caption(
        "Review recent photo-comp retry strategy outcomes (coverage, no-result rate, and strategy effectiveness)."
    )
    c1, c2 = st.columns(2)
    with c1:
        lookback_days = st.number_input(
            "Lookback Days",
            min_value=1,
            max_value=365,
            value=14,
            step=1,
            key="admin_comp_photo_retry_lookback_days",
        )
    with c2:
        max_rows = st.number_input(
            "Max Rows",
            min_value=20,
            max_value=5000,
            value=500,
            step=20,
            key="admin_comp_photo_retry_max_rows",
        )
    cutoff = utcnow_naive() - timedelta(days=int(lookback_days))
    logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "comp_photo_retry",
            AuditLog.action == "run",
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()
    parsed_rows: list[dict] = []
    for log in logs:
        try:
            payload = json.loads(log.changes_json or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        parsed_rows.append(
            {
                "time": log.created_at,
                "actor": str(log.actor or ""),
                "strategy": str(payload.get("strategy") or ""),
                "run_label": str(payload.get("run_label") or ""),
                "query": str(payload.get("query") or ""),
                "coverage_pct": float(payload.get("coverage_pct") or 0.0),
                "rows_total": int(payload.get("rows_total") or 0),
                "rows_priced": int(payload.get("rows_priced") or 0),
                "rows_missing_price": int(payload.get("rows_missing_price") or 0),
                "web_rows_total": int(payload.get("web_rows_total") or 0),
                "web_rows_priced": int(payload.get("web_rows_priced") or 0),
                "web_rows_missing_price": int(payload.get("web_rows_missing_price") or 0),
                "result": str(payload.get("result") or ""),
                "raw_payload": payload,
            }
        )
    if not parsed_rows:
        st.info("No `comp_photo_retry` telemetry events found in selected lookback.")
        return

    strategy_values = sorted(
        {str(row.get("strategy") or "").strip().lower() for row in parsed_rows if str(row.get("strategy") or "").strip()}
    )
    selected_strategies = st.multiselect(
        "Strategy Filter",
        options=strategy_values,
        default=strategy_values,
        key="admin_comp_photo_retry_strategy_filter",
    )
    filtered_rows = [
        row
        for row in parsed_rows
        if not selected_strategies
        or str(row.get("strategy") or "").strip().lower() in set(selected_strategies)
    ]
    if not filtered_rows:
        st.info("No telemetry rows match selected strategy filters.")
        return

    total_runs = len(filtered_rows)
    no_rows_runs = sum(1 for row in filtered_rows if str(row.get("result") or "").strip().lower() == "no_rows")
    avg_coverage = sum(float(row.get("coverage_pct") or 0.0) for row in filtered_rows) / max(1, total_runs)
    no_rows_rate = (float(no_rows_runs) / float(total_runs) * 100.0) if total_runs else 0.0
    strategy_df = (
        pd.DataFrame(filtered_rows)
        .groupby(["strategy"], dropna=False)
        .agg(
            runs=("strategy", "count"),
            avg_coverage_pct=("coverage_pct", "mean"),
            no_rows_runs=("result", lambda s: int((s.fillna("").astype(str).str.lower() == "no_rows").sum())),
        )
        .reset_index()
        .sort_values(["runs", "avg_coverage_pct"], ascending=[False, False])
    )
    if not strategy_df.empty:
        strategy_df["no_rows_rate_pct"] = strategy_df.apply(
            lambda r: (float(r["no_rows_runs"]) / float(max(1, int(r["runs"]))) * 100.0), axis=1
        )
        best_row = strategy_df.sort_values(["avg_coverage_pct", "runs"], ascending=[False, False]).iloc[0]
        best_strategy = str(best_row.get("strategy") or "")
        best_strategy_coverage = float(best_row.get("avg_coverage_pct") or 0.0)
    else:
        best_strategy = ""
        best_strategy_coverage = 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Runs", int(total_runs))
    m2.metric("No-Result Rate", f"{no_rows_rate:.1f}%")
    m3.metric("Avg Coverage", f"{avg_coverage:.1f}%")
    m4.metric("Top Strategy (Coverage)", f"{best_strategy or '-'} ({best_strategy_coverage:.1f}%)")

    st.caption("Strategy performance summary")
    st.dataframe(strategy_df, use_container_width=True)

    st.markdown("#### Strategy Trends")
    trend_df = pd.DataFrame(filtered_rows).copy()
    if not trend_df.empty:
        trend_df["date"] = pd.to_datetime(trend_df["time"], errors="coerce").dt.date
        coverage_trend = (
            trend_df.groupby(["date", "strategy"], dropna=False)["coverage_pct"]
            .mean()
            .reset_index()
            .sort_values(["date", "strategy"], ascending=[True, True])
        )
        if not coverage_trend.empty:
            coverage_pivot = (
                coverage_trend.pivot(index="date", columns="strategy", values="coverage_pct")
                .sort_index()
            )
            st.caption("Average coverage % by strategy over time")
            st.line_chart(coverage_pivot, use_container_width=True)
        no_result_trend = (
            trend_df.assign(
                no_result=trend_df["result"].fillna("").astype(str).str.lower().eq("no_rows").astype(int)
            )
            .groupby(["date", "strategy"], dropna=False)
            .agg(no_result_rate_pct=("no_result", "mean"))
            .reset_index()
            .sort_values(["date", "strategy"], ascending=[True, True])
        )
        if not no_result_trend.empty:
            no_result_trend["no_result_rate_pct"] = no_result_trend["no_result_rate_pct"] * 100.0
            no_result_pivot = (
                no_result_trend.pivot(index="date", columns="strategy", values="no_result_rate_pct")
                .sort_index()
            )
            st.caption("No-result rate % by strategy over time")
            st.line_chart(no_result_pivot, use_container_width=True)

    st.markdown("#### Top Missing-Price Domains")
    domain_miss_counter: Counter[str] = Counter()
    domain_priced_counter: Counter[str] = Counter()
    for row in filtered_rows:
        payload = row.get("raw_payload") or {}
        try:
            missing_pairs = json.loads(str(payload.get("top_missing_domains_json") or "[]"))
        except Exception:
            missing_pairs = []
        try:
            priced_pairs = json.loads(str(payload.get("top_priced_domains_json") or "[]"))
        except Exception:
            priced_pairs = []
        if isinstance(missing_pairs, list):
            for pair in missing_pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                domain = str(pair[0] or "").strip().lower()
                if not domain:
                    continue
                try:
                    domain_miss_counter[domain] += int(pair[1] or 0)
                except Exception:
                    continue
        if isinstance(priced_pairs, list):
            for pair in priced_pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                domain = str(pair[0] or "").strip().lower()
                if not domain:
                    continue
                try:
                    domain_priced_counter[domain] += int(pair[1] or 0)
                except Exception:
                    continue
    domain_rows: list[dict[str, Any]] = []
    for domain, miss_count in domain_miss_counter.most_common(30):
        priced_count = int(domain_priced_counter.get(domain, 0))
        total = int(miss_count + priced_count)
        miss_rate = (float(miss_count) / float(max(1, total))) * 100.0
        domain_rows.append(
            {
                "domain": domain,
                "missing_price_rows": int(miss_count),
                "priced_rows": int(priced_count),
                "total_rows": int(total),
                "missing_rate_pct": float(round(miss_rate, 2)),
            }
        )
    if domain_rows:
        domain_df = pd.DataFrame(domain_rows).sort_values(
            ["missing_price_rows", "missing_rate_pct"], ascending=[False, False]
        )
        st.dataframe(domain_df, use_container_width=True)
        top10 = domain_df.head(10).set_index("domain")
        st.caption("Top 10 missing-price domains")
        st.bar_chart(top10["missing_price_rows"], use_container_width=True)

        st.markdown("#### Dealer-Domain Recommendations")
        current_domain_setting = repo.get_runtime_setting(
            environment=settings.app_env,
            key="comp_dealer_domains_csv",
            active_only=False,
        )
        current_domain_csv = (
            str(current_domain_setting.value or "").strip()
            if current_domain_setting is not None
            else ",".join(DEFAULT_COMP_DEALER_DOMAINS)
        )
        _, current_domains = _normalize_comp_dealer_domains_csv(current_domain_csv)
        current_domain_set = {d.strip().lower() for d in current_domains if d.strip()}

        r1, r2, r3 = st.columns(3)
        with r1:
            min_obs = st.number_input(
                "Min Domain Observations",
                min_value=1,
                max_value=200,
                value=5,
                step=1,
                key="admin_comp_domain_rec_min_obs",
            )
        with r2:
            add_max_missing_rate = st.number_input(
                "Add If Missing Rate ≤ %",
                min_value=0.0,
                max_value=100.0,
                value=40.0,
                step=1.0,
                key="admin_comp_domain_rec_add_max_missing",
            )
        with r3:
            remove_min_missing_rate = st.number_input(
                "Remove If Missing Rate ≥ %",
                min_value=0.0,
                max_value=100.0,
                value=80.0,
                step=1.0,
                key="admin_comp_domain_rec_remove_min_missing",
            )

        recommended_add: list[str] = []
        recommended_remove: list[str] = []
        for _, row in domain_df.iterrows():
            domain = str(row.get("domain") or "").strip().lower()
            if not domain:
                continue
            total_rows = int(row.get("total_rows") or 0)
            priced_rows = int(row.get("priced_rows") or 0)
            miss_rate = float(row.get("missing_rate_pct") or 0.0)
            if total_rows < int(min_obs):
                continue
            if domain not in current_domain_set and priced_rows > 0 and miss_rate <= float(add_max_missing_rate):
                recommended_add.append(domain)
            if domain in current_domain_set and priced_rows == 0 and miss_rate >= float(remove_min_missing_rate):
                recommended_remove.append(domain)

        st.caption(f"Current configured dealer domains: {len(current_domains)}")
        a1, a2 = st.columns(2)
        with a1:
            st.caption(f"Recommended Add ({len(recommended_add)})")
            st.code(", ".join(recommended_add) if recommended_add else "(none)")
        with a2:
            st.caption(f"Recommended Remove ({len(recommended_remove)})")
            st.code(", ".join(recommended_remove) if recommended_remove else "(none)")

        merged_domain_list = sorted((current_domain_set | set(recommended_add)) - set(recommended_remove))
        st.caption(f"Preview configured domains after apply: {len(merged_domain_list)}")
        st.code(", ".join(merged_domain_list) if merged_domain_list else "(empty)")

        st.markdown("##### Dry-Run Change Preview")
        preview_mode = st.radio(
            "Preview Mode",
            options=["Add Only", "Remove Only", "Add + Remove"],
            horizontal=True,
            key="admin_comp_domain_preview_mode",
        )
        if preview_mode == "Add Only":
            preview_updated_domains = sorted(current_domain_set | set(recommended_add))
            preview_add = sorted(set(recommended_add) - current_domain_set)
            preview_remove = []
        elif preview_mode == "Remove Only":
            preview_updated_domains = sorted(current_domain_set - set(recommended_remove))
            preview_add = []
            preview_remove = sorted(current_domain_set & set(recommended_remove))
        else:
            preview_updated_domains = sorted((current_domain_set | set(recommended_add)) - set(recommended_remove))
            preview_add = sorted(set(recommended_add) - current_domain_set)
            preview_remove = sorted(current_domain_set & set(recommended_remove))
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Current Domains", int(len(current_domain_set)))
        d2.metric("Will Add", int(len(preview_add)))
        d3.metric("Will Remove", int(len(preview_remove)))
        d4.metric("Result Domains", int(len(preview_updated_domains)))
        preview_rows: list[dict[str, str]] = []
        for domain in preview_add:
            preview_rows.append({"domain": domain, "change": "add"})
        for domain in preview_remove:
            preview_rows.append({"domain": domain, "change": "remove"})
        if preview_rows:
            st.dataframe(pd.DataFrame(preview_rows).sort_values(["change", "domain"]), use_container_width=True)
        else:
            st.caption("No domain changes for this preview mode.")

        ap1, ap2, ap3 = st.columns(3)
        with ap1:
            apply_add = st.button(
                "Apply Add Recommendations",
                key="admin_comp_domain_apply_add_btn",
                disabled=not bool(recommended_add),
            )
        with ap2:
            apply_remove = st.button(
                "Apply Remove Recommendations",
                key="admin_comp_domain_apply_remove_btn",
                disabled=not bool(recommended_remove),
            )
        with ap3:
            apply_all = st.button(
                "Apply Add + Remove",
                key="admin_comp_domain_apply_all_btn",
                disabled=not bool(recommended_add or recommended_remove),
            )

        if apply_add or apply_remove or apply_all:
            if apply_add:
                updated_domains = sorted(current_domain_set | set(recommended_add))
            elif apply_remove:
                updated_domains = sorted(current_domain_set - set(recommended_remove))
            else:
                updated_domains = sorted((current_domain_set | set(recommended_add)) - set(recommended_remove))
            if not updated_domains:
                st.error("Apply blocked: result would leave dealer-domain config empty.")
            else:
                try:
                    row = repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="comp_dealer_domains_csv",
                        value=",".join(updated_domains),
                        value_type="str",
                        description="Comma-separated dealer domains used for comp parser/domain weighting.",
                        is_active=bool(current_domain_setting.is_active) if current_domain_setting is not None else True,
                        actor=user.username,
                    )
                    action_label = (
                        "add-only"
                        if apply_add
                        else ("remove-only" if apply_remove else "add+remove")
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="comp_domain_recommendation",
                            entity_id=None,
                            action="apply",
                            actor=user.username,
                            changes={
                                "mode": action_label,
                                "current_count": int(len(current_domain_set)),
                                "updated_count": int(len(updated_domains)),
                                "recommended_add": list(recommended_add),
                                "recommended_remove": list(recommended_remove),
                                "applied_add": sorted(list(set(updated_domains) - current_domain_set)),
                                "applied_remove": sorted(list(current_domain_set - set(updated_domains))),
                                "before_domains_csv": ",".join(sorted(current_domain_set)),
                                "after_domains_csv": ",".join(sorted(updated_domains)),
                            },
                        )
                    except Exception:
                        pass
                    st.success(
                        f"Applied dealer-domain recommendations ({action_label}). "
                        f"`{row.key}` now has {len(updated_domains)} domains."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply dealer-domain recommendations: {exc}")

        st.markdown("##### Recommendation Apply History")
        hist1, hist2 = st.columns(2)
        with hist1:
            history_lookback_days = st.number_input(
                "History Lookback Days",
                min_value=1,
                max_value=365,
                value=30,
                step=1,
                key="admin_comp_domain_rec_history_lookback_days",
            )
        with hist2:
            history_limit = st.number_input(
                "History Max Rows",
                min_value=20,
                max_value=2000,
                value=200,
                step=20,
                key="admin_comp_domain_rec_history_limit",
            )
        history_cutoff = utcnow_naive() - timedelta(days=int(history_lookback_days))
        history_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "comp_domain_recommendation",
                AuditLog.action == "apply",
                AuditLog.created_at >= history_cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(history_limit))
        ).all()
        history_rows: list[dict[str, Any]] = []
        for log in history_logs:
            try:
                payload = json.loads(log.changes_json or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            history_rows.append(
                {
                    "audit_id": int(log.id),
                    "time": log.created_at,
                    "actor": str(log.actor or ""),
                    "mode": str(payload.get("mode") or ""),
                    "current_count": int(payload.get("current_count") or 0),
                    "updated_count": int(payload.get("updated_count") or 0),
                    "recommended_add_count": len(payload.get("recommended_add") or []),
                    "recommended_remove_count": len(payload.get("recommended_remove") or []),
                    "applied_add_count": len(payload.get("applied_add") or []),
                    "applied_remove_count": len(payload.get("applied_remove") or []),
                    "applied_add_csv": ",".join(payload.get("applied_add") or []),
                    "applied_remove_csv": ",".join(payload.get("applied_remove") or []),
                    "before_domains_csv": str(payload.get("before_domains_csv") or ""),
                    "after_domains_csv": str(payload.get("after_domains_csv") or ""),
                }
            )
        if history_rows:
            history_df = pd.DataFrame(history_rows).sort_values(["time"], ascending=[False])
            st.dataframe(history_df, use_container_width=True)
            row_map = {
                f"#{int(row['audit_id'])} | {str(row['time'])} | {str(row['actor'])} | {str(row['mode'])}": row
                for row in history_rows
            }
            selected_history_key = st.selectbox(
                "Undo Target",
                options=list(row_map.keys()),
                key="admin_comp_domain_rec_undo_target",
            )
            if st.button("Undo Selected Apply (Restore Before Set)", key="admin_comp_domain_rec_undo_btn"):
                selected_row = row_map.get(selected_history_key) or {}
                before_csv = str(selected_row.get("before_domains_csv") or "").strip()
                if not before_csv:
                    try:
                        repo.record_audit_event(
                            entity_type="comp_domain_recommendation",
                            entity_id=None,
                            action="undo_failed",
                            actor=user.username,
                            changes={
                                "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                "reason": "missing_before_snapshot",
                            },
                        )
                    except Exception:
                        pass
                    st.error(
                        "Undo is unavailable for this event because full before-domain snapshot "
                        "was not recorded yet."
                    )
                else:
                    _, normalized_before = _normalize_comp_dealer_domains_csv(before_csv)
                    if not normalized_before:
                        try:
                            repo.record_audit_event(
                                entity_type="comp_domain_recommendation",
                                entity_id=None,
                                action="undo_failed",
                                actor=user.username,
                                changes={
                                    "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                    "reason": "empty_normalized_before_snapshot",
                                },
                            )
                        except Exception:
                            pass
                        st.error("Undo blocked: before snapshot resolved to empty domain list.")
                    else:
                        try:
                            row = repo.upsert_runtime_setting(
                                environment=settings.app_env,
                                key="comp_dealer_domains_csv",
                                value=",".join(normalized_before),
                                value_type="str",
                                description="Comma-separated dealer domains used for comp parser/domain weighting.",
                                is_active=bool(current_domain_setting.is_active) if current_domain_setting is not None else True,
                                actor=user.username,
                            )
                            try:
                                repo.record_audit_event(
                                    entity_type="comp_domain_recommendation",
                                    entity_id=None,
                                    action="undo",
                                    actor=user.username,
                                    changes={
                                        "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                        "restored_domain_count": int(len(normalized_before)),
                                        "restored_domains_csv": ",".join(normalized_before),
                                    },
                                )
                            except Exception:
                                pass
                            st.success(
                                f"Undo complete. Restored `{row.key}` to "
                                f"{len(normalized_before)} domains from selected apply event."
                            )
                            st.rerun()
                        except Exception as exc:
                            try:
                                repo.record_audit_event(
                                    entity_type="comp_domain_recommendation",
                                    entity_id=None,
                                    action="undo_failed",
                                    actor=user.username,
                                    changes={
                                        "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                        "reason": "exception",
                                        "error": str(exc),
                                    },
                                )
                            except Exception:
                                pass
                            st.error(f"Unable to undo selected apply event: {exc}")
            st.download_button(
                "Download Recommendation Apply History CSV",
                data=history_df.to_csv(index=False).encode("utf-8"),
                file_name=(
                    f"comp_domain_recommendation_apply_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                ),
                mime="text/csv",
                key="admin_comp_domain_rec_history_csv_btn",
            )

            st.markdown("##### Undo Telemetry")
            undo_logs = repo.db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "comp_domain_recommendation",
                    AuditLog.action.in_(["undo", "undo_failed"]),
                    AuditLog.created_at >= history_cutoff,
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(int(history_limit))
            ).all()
            undo_rows: list[dict[str, Any]] = []
            for log in undo_logs:
                try:
                    payload = json.loads(log.changes_json or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                except Exception:
                    payload = {}
                undo_rows.append(
                    {
                        "time": log.created_at,
                        "actor": str(log.actor or ""),
                        "action": str(log.action or ""),
                        "source_apply_audit_id": int(payload.get("source_apply_audit_id") or 0),
                        "reason": str(payload.get("reason") or ""),
                        "restored_domain_count": int(payload.get("restored_domain_count") or 0),
                        "error": str(payload.get("error") or ""),
                    }
                )
            if undo_rows:
                undo_total = len(undo_rows)
                undo_success = sum(1 for row in undo_rows if str(row.get("action") or "") == "undo")
                undo_failed = sum(1 for row in undo_rows if str(row.get("action") or "") == "undo_failed")
                undo_success_rate = (float(undo_success) / float(max(1, undo_total))) * 100.0
                u1, u2, u3 = st.columns(3)
                u1.metric("Undo Attempts", int(undo_total))
                u2.metric("Undo Success", int(undo_success))
                u3.metric("Undo Success Rate", f"{undo_success_rate:.1f}%")
                undo_df = pd.DataFrame(undo_rows).sort_values(["time"], ascending=[False])
                actor_summary = (
                    undo_df.groupby(["actor", "action"], dropna=False)
                    .size()
                    .reset_index(name="events")
                    .sort_values(["events"], ascending=[False])
                )
                st.caption("Undo events by actor/action")
                st.dataframe(actor_summary, use_container_width=True)
                st.caption("Recent undo events")
                st.dataframe(undo_df.head(50), use_container_width=True)
                st.download_button(
                    "Download Undo Telemetry CSV",
                    data=undo_df.to_csv(index=False).encode("utf-8"),
                    file_name=(
                        f"comp_domain_recommendation_undo_{settings.app_env}_"
                        f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                    ),
                    mime="text/csv",
                    key="admin_comp_domain_rec_undo_csv_btn",
                )
            else:
                st.caption("No undo events found for the selected lookback window.")

            st.markdown("##### Governance Bundle Export")
            bundle_buffer = BytesIO()
            with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                strategy_export_df = strategy_df.copy()
                strategy_export_df.insert(0, "environment", settings.app_env)
                strategy_export_df.insert(1, "lookback_days", int(lookback_days))
                strategy_export_df.insert(2, "strategy_filter_csv", ",".join(selected_strategies))
                bundle_zip.writestr(
                    "photo_comp_strategy_performance.csv",
                    strategy_export_df.to_csv(index=False),
                )
                domain_export_df = domain_df.copy()
                domain_export_df.insert(0, "environment", settings.app_env)
                domain_export_df.insert(1, "lookback_days", int(lookback_days))
                bundle_zip.writestr(
                    "photo_comp_domain_leaderboard.csv",
                    domain_export_df.to_csv(index=False),
                )
                history_export_df = history_df.copy()
                history_export_df.insert(0, "environment", settings.app_env)
                history_export_df.insert(1, "history_lookback_days", int(history_lookback_days))
                bundle_zip.writestr(
                    "domain_recommendation_apply_history.csv",
                    history_export_df.to_csv(index=False),
                )
                if undo_rows:
                    undo_export_df = undo_df.copy()
                    undo_export_df.insert(0, "environment", settings.app_env)
                    undo_export_df.insert(1, "history_lookback_days", int(history_lookback_days))
                    bundle_zip.writestr(
                        "domain_recommendation_undo_telemetry.csv",
                        undo_export_df.to_csv(index=False),
                    )
                recommendation_summary_df = pd.DataFrame(
                    [
                        {
                            "environment": settings.app_env,
                            "min_domain_observations": int(min_obs),
                            "add_max_missing_rate_pct": float(add_max_missing_rate),
                            "remove_min_missing_rate_pct": float(remove_min_missing_rate),
                            "current_domain_count": int(len(current_domains)),
                            "recommended_add_count": int(len(recommended_add)),
                            "recommended_remove_count": int(len(recommended_remove)),
                            "preview_mode": str(preview_mode),
                            "preview_add_count": int(len(preview_add)),
                            "preview_remove_count": int(len(preview_remove)),
                            "preview_result_domain_count": int(len(preview_updated_domains)),
                            "generated_at_utc": utcnow_naive().isoformat(),
                        }
                    ]
                )
                bundle_zip.writestr(
                    "domain_recommendation_summary.csv",
                    recommendation_summary_df.to_csv(index=False),
                )
            bundle_buffer.seek(0)
            st.download_button(
                "Export Photo-Comp Governance Bundle (ZIP)",
                data=bundle_buffer.getvalue(),
                file_name=(
                    f"photo_comp_governance_bundle_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
                ),
                mime="application/zip",
                key="admin_comp_domain_rec_governance_bundle_zip_btn",
            )
        else:
            st.caption("No recommendation-apply history found for the selected lookback window.")
    else:
        st.info("No domain-level miss telemetry available yet. Run photo-comp searches with web fallback.")

    st.markdown("#### Promote Strategy To Default Retry Preset")
    promote_min_runs = st.number_input(
        "Minimum Runs Required For Promotion",
        min_value=1,
        max_value=200,
        value=5,
        step=1,
        key="admin_comp_retry_promote_min_runs",
        help="Guardrail: block promotion until the selected best strategy has at least this many runs.",
    )
    promotable_rows = [row for row in filtered_rows if str(row.get("result") or "").strip().lower() != "no_rows"]
    if promotable_rows:
        best_row = sorted(
            promotable_rows,
            key=lambda r: (float(r.get("coverage_pct") or 0.0), int(r.get("rows_total") or 0)),
            reverse=True,
        )[0]
        suggested_strategy = str(best_row.get("strategy") or "").strip() or "manual"
        suggested_name = f"Auto {suggested_strategy} ({utcnow_naive().strftime('%Y-%m-%d')})"
        strategy_subset = [
            row for row in filtered_rows if str(row.get("strategy") or "").strip().lower() == suggested_strategy.lower()
        ]
        strategy_runs = len(strategy_subset)
        strategy_no_rows_runs = sum(
            1
            for row in strategy_subset
            if str(row.get("result") or "").strip().lower() == "no_rows"
        )
        strategy_no_rows_rate = (
            float(strategy_no_rows_runs) / float(max(1, strategy_runs)) * 100.0
            if strategy_runs
            else 0.0
        )
        confidence_label = "low"
        if strategy_runs >= int(promote_min_runs) and strategy_no_rows_rate <= 25.0:
            confidence_label = "high"
        elif strategy_runs >= max(2, int(promote_min_runs // 2)) and strategy_no_rows_rate <= 40.0:
            confidence_label = "medium"
        p1, p2, p3 = st.columns(3)
        with p1:
            promote_name = st.text_input(
                "Preset Name",
                value=suggested_name,
                key="admin_comp_retry_promote_name",
            )
        with p2:
            promote_shared = st.checkbox(
                "Team-shared",
                value=False,
                key="admin_comp_retry_promote_shared",
            )
        with p3:
            st.caption(
                f"Suggested from strategy `{suggested_strategy}` "
                f"(coverage {float(best_row.get('coverage_pct') or 0.0):.1f}%)."
            )
            st.caption(
                f"Sample confidence: `{confidence_label}` "
                f"(runs={int(strategy_runs)}, no-result-rate={strategy_no_rows_rate:.1f}%)."
            )
            promote_clicked = st.button(
                "Promote Best To Default",
                key="admin_comp_retry_promote_btn",
                use_container_width=True,
            )
        if promote_clicked:
            resolved_name = str(promote_name or "").strip()
            if not resolved_name:
                st.error("Preset name is required.")
            elif int(strategy_runs) < int(promote_min_runs):
                st.error(
                    f"Promotion blocked: strategy `{suggested_strategy}` has {int(strategy_runs)} runs, "
                    f"below minimum required {int(promote_min_runs)}."
                )
            else:
                payload_src = dict(best_row.get("raw_payload") or {})
                preset_payload = {
                    "sold_only": bool(payload_src.get("sold_only")),
                    "auto_broaden": bool(payload_src.get("auto_broaden")),
                    "use_web_fallback": bool(payload_src.get("used_web_fallback")),
                    "use_ai_summary": bool(payload_src.get("used_ai_summary")),
                    "web_fallback_limit": int(payload_src.get("web_fallback_limit") or 20),
                    "web_detail_fetch_limit": int(payload_src.get("web_detail_fetch_limit") or 20),
                    "min_web_confidence": str(payload_src.get("min_web_confidence") or "any"),
                    "min_web_confidence_score": float(payload_src.get("min_web_confidence_score") or 0.0),
                    "parser_source_filter": list(payload_src.get("parser_source_filter") or []),
                    "domain_include_raw": str(payload_src.get("domain_include_raw") or ""),
                    "domain_exclude_raw": str(payload_src.get("domain_exclude_raw") or ""),
                }
                try:
                    repo.upsert_saved_filter_profile(
                        environment=settings.app_env,
                        username=user.username,
                        scope="tools_photo_comp_retry",
                        name=resolved_name,
                        filter_json=json.dumps(preset_payload),
                        is_shared=bool(promote_shared),
                        is_default=True,
                        is_active=True,
                        actor=user.username,
                    )
                    st.success(
                        f"Promoted `{resolved_name}` as default photo-comp retry preset "
                        f"from strategy `{suggested_strategy}`."
                    )
                except Exception as exc:
                    st.error(f"Unable to promote strategy to default preset: {exc}")
    else:
        st.info("No successful (priced) retry runs available to promote yet.")

    rows_df = pd.DataFrame(filtered_rows).sort_values(["time"], ascending=[False])
    st.caption("Recent retry telemetry")
    st.dataframe(rows_df.head(50), use_container_width=True)
    csv_bytes = rows_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download Retry Telemetry CSV",
        data=csv_bytes,
        file_name=f"comp_photo_retry_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key="admin_comp_photo_retry_csv_btn",
    )


def _render_listing_review_policy_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Listing Review Policy")
    st.caption("Configure optional two-person review requirement for selected marketplaces.")
    required_row = repo.get_runtime_setting(
        environment=settings.app_env,
        key="listing_review_two_person_required",
        active_only=False,
    )
    channels_row = repo.get_runtime_setting(
        environment=settings.app_env,
        key="listing_review_two_person_channels_csv",
        active_only=False,
    )
    required_value = str(getattr(required_row, "value", "false") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    channels_value = str(getattr(channels_row, "value", "ebay") or "ebay")
    with st.form("admin_listing_review_policy_form"):
        policy_required = st.checkbox(
            "Require two-person approval before active status",
            value=required_value,
        )
        policy_channels = st.text_input(
            "Policy Marketplaces (comma-separated)",
            value=channels_value,
            help="Example: ebay,facebook,whatnot",
        )
        save_policy = st.form_submit_button("Save Review Policy")
    if save_policy:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="listing_review_two_person_required",
                value="true" if bool(policy_required) else "false",
                value_type="bool",
                description="Require a different user than reviewer when setting listing to active on configured channels.",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="listing_review_two_person_channels_csv",
                value=(policy_channels or "").strip() or "ebay",
                value_type="str",
                description="Comma-separated marketplaces where two-person review policy applies.",
                is_active=True,
                actor=user.username,
            )
            st.success("Listing review policy saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save listing review policy: {exc}")


def _render_coin_paid_source_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Coin Paid Source Adapter")
    st.caption(
        "Optional paid-source contract for coin reference ingestion (for example Greysheet). "
        "Use only with approved licensing."
    )

    cfg = resolve_paid_coin_source_config(repo)
    adapter = resolve_paid_coin_source_adapter(repo)
    issues = adapter.validate()

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "enabled": bool(cfg.enabled),
                    "provider": cfg.provider,
                    "base_url": cfg.base_url,
                    "api_key_configured": bool(cfg.api_key),
                    "license_acknowledged": bool(cfg.license_acknowledged),
                    "allow_prod": bool(cfg.allow_prod),
                    "environment": settings.app_env,
                }
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    if issues:
        st.warning("Current validation issues:")
        for issue in issues:
            st.write(f"- {issue}")
    else:
        st.success("Current paid-source configuration is valid.")

    with st.form("admin_coin_paid_source_form"):
        p1, p2 = st.columns(2)
        with p1:
            enabled = st.checkbox("Enable paid source adapter", value=bool(cfg.enabled))
        with p2:
            provider = st.selectbox(
                "Provider",
                options=["none", "greysheet"],
                index=1 if cfg.provider == "greysheet" else 0,
            )
        base_url = st.text_input("Base URL", value=cfg.base_url)
        api_key = st.text_input(
            "API Key/Token",
            value="",
            type="password",
            help="Leave blank to keep existing stored key.",
        )
        c1, c2 = st.columns(2)
        with c1:
            license_ack = st.checkbox(
                "I confirm we have required paid-source license approval",
                value=bool(cfg.license_acknowledged),
            )
        with c2:
            allow_prod = st.checkbox(
                "Allow in production environment",
                value=bool(cfg.allow_prod),
                help="Keep off for local/dev validation unless approved for production usage.",
            )
        a1, a2 = st.columns(2)
        with a1:
            save_paid = st.form_submit_button("Save Paid Source Settings")
        with a2:
            disable_paid = st.form_submit_button("Disable + Reset To Safe Defaults")

    if save_paid:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_enabled",
                value="true" if bool(enabled) else "false",
                value_type="bool",
                description="Enable optional paid coin-reference source adapter contract (disabled by default).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_provider",
                value=str(provider or "none").strip().lower() or "none",
                value_type="str",
                description="Paid source provider key (`none`, `greysheet`).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_base_url",
                value=(base_url or "").strip(),
                value_type="str",
                description="Paid source API base URL (if licensed/in use).",
                is_active=True,
                actor=user.username,
            )
            if (api_key or "").strip():
                api_key_value = api_key.strip()
            else:
                existing_key_row = repo.get_runtime_setting(
                    environment=settings.app_env,
                    key="coin_ref_paid_source_api_key",
                    active_only=False,
                )
                api_key_value = str(getattr(existing_key_row, "value", "") or "")
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_api_key",
                value=api_key_value,
                value_type="str",
                description="Paid source API key/token (if licensed/in use).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_license_ack",
                value="true" if bool(license_ack) else "false",
                value_type="bool",
                description="Set true only after legal/licensing approval for paid source usage.",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_allow_prod",
                value="true" if bool(allow_prod) else "false",
                value_type="bool",
                description="Allow paid source usage in production environment (separate guardrail).",
                is_active=True,
                actor=user.username,
            )
            st.success("Paid source adapter settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save paid source settings: {exc}")

    if disable_paid:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_enabled",
                value="false",
                value_type="bool",
                description="Enable optional paid coin-reference source adapter contract (disabled by default).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_provider",
                value="none",
                value_type="str",
                description="Paid source provider key (`none`, `greysheet`).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_license_ack",
                value="false",
                value_type="bool",
                description="Set true only after legal/licensing approval for paid source usage.",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_allow_prod",
                value="false",
                value_type="bool",
                description="Allow paid source usage in production environment (separate guardrail).",
                is_active=True,
                actor=user.username,
            )
            st.success("Paid source adapter reset to safe defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to reset paid source settings: {exc}")


def _render_voice_runtime_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Voice Runtime (STT/TTS)")
    st.caption("Configure speech-to-text and text-to-speech behavior for Ask GoldenStackers.")
    defaults = {
        "ai_voice_enabled": "false",
        "ai_voice_stt_enabled": "true",
        "ai_voice_tts_enabled": "false",
        "ai_voice_provider": "openai",
        "ai_voice_base_url": (settings.comp_llm_base_url or "https://api.openai.com/v1").strip().rstrip("/"),
        "ai_voice_api_key": settings.openai_api_key or "",
        "ai_voice_stt_model": "gpt-4o-mini-transcribe",
        "ai_voice_stt_language": "",
        "ai_voice_tts_model": "gpt-4o-mini-tts",
        "ai_voice_tts_voice": "alloy",
        "ai_voice_tts_response_format": "mp3",
        "ai_voice_timeout_seconds": "45",
        "ai_voice_tts_max_chars": "1400",
    }

    def _get_value(key: str) -> str:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=False)
        if row is None:
            return defaults[key]
        return str(row.value or "")

    with st.form("admin_voice_runtime_form"):
        v1, v2, v3 = st.columns(3)
        with v1:
            voice_enabled = st.checkbox(
                "Enable voice features",
                value=_get_value("ai_voice_enabled").strip().lower() in {"1", "true", "yes", "on"},
            )
        with v2:
            voice_stt_enabled = st.checkbox(
                "Enable STT (mic -> text)",
                value=_get_value("ai_voice_stt_enabled").strip().lower() in {"1", "true", "yes", "on"},
            )
        with v3:
            voice_tts_enabled = st.checkbox(
                "Enable TTS (assistant speech)",
                value=_get_value("ai_voice_tts_enabled").strip().lower() in {"1", "true", "yes", "on"},
            )
        p1, p2 = st.columns(2)
        with p1:
            voice_provider = st.selectbox(
                "Voice Provider",
                options=["openai", "localai"],
                index=0 if _get_value("ai_voice_provider").strip().lower() != "localai" else 1,
            )
        with p2:
            voice_base_url = st.text_input("Voice Base URL", value=_get_value("ai_voice_base_url"))
        voice_api_key = st.text_input(
            "Voice API Key/Token",
            value="",
            type="password",
            help="Leave blank to keep existing stored key.",
        )
        m1, m2, m3 = st.columns(3)
        with m1:
            voice_stt_model = st.text_input("STT Model", value=_get_value("ai_voice_stt_model"))
        with m2:
            voice_tts_model = st.text_input("TTS Model", value=_get_value("ai_voice_tts_model"))
        with m3:
            voice_tts_voice = st.text_input("TTS Voice", value=_get_value("ai_voice_tts_voice"))
        n1, n2, n3 = st.columns(3)
        with n1:
            voice_stt_language = st.text_input("STT Language (optional)", value=_get_value("ai_voice_stt_language"))
        with n2:
            voice_tts_format = st.selectbox(
                "TTS Format",
                options=["mp3", "wav"],
                index=0 if _get_value("ai_voice_tts_response_format").strip().lower() != "wav" else 1,
            )
        with n3:
            voice_timeout = st.number_input(
                "Voice Timeout Seconds",
                min_value=5,
                max_value=300,
                value=max(5, int(_get_value("ai_voice_timeout_seconds") or "45")),
                step=5,
            )
        voice_tts_max_chars = st.number_input(
            "TTS Max Chars per Response",
            min_value=200,
            max_value=8000,
            value=max(200, int(_get_value("ai_voice_tts_max_chars") or "1400")),
            step=100,
        )
        save_voice_runtime = st.form_submit_button("Save Voice Runtime Settings")
    st.caption(
        "LocalAI is supported when it exposes OpenAI-compatible audio endpoints "
        "(`.../audio/transcriptions`, `.../audio/speech`) and matching STT/TTS models."
    )
    if save_voice_runtime:
        try:
            upserts = [
                ("ai_voice_enabled", "true" if voice_enabled else "false", "bool", "Enable/disable voice features."),
                (
                    "ai_voice_stt_enabled",
                    "true" if voice_stt_enabled else "false",
                    "bool",
                    "Enable speech-to-text input in chat.",
                ),
                (
                    "ai_voice_tts_enabled",
                    "true" if voice_tts_enabled else "false",
                    "bool",
                    "Enable text-to-speech playback for assistant responses.",
                ),
                ("ai_voice_provider", voice_provider.strip().lower(), "str", "Voice provider id."),
                ("ai_voice_base_url", voice_base_url.strip().rstrip("/"), "str", "Voice provider base URL."),
                ("ai_voice_stt_model", voice_stt_model.strip(), "str", "Speech-to-text model."),
                ("ai_voice_stt_language", voice_stt_language.strip(), "str", "Optional STT language hint."),
                ("ai_voice_tts_model", voice_tts_model.strip(), "str", "Text-to-speech model."),
                ("ai_voice_tts_voice", voice_tts_voice.strip(), "str", "Text-to-speech voice id."),
                ("ai_voice_tts_response_format", voice_tts_format.strip().lower(), "str", "TTS response format."),
                ("ai_voice_timeout_seconds", str(int(voice_timeout)), "int", "Voice request timeout seconds."),
                ("ai_voice_tts_max_chars", str(int(voice_tts_max_chars)), "int", "Max chars for TTS synthesis."),
            ]
            if voice_api_key.strip():
                upserts.append(("ai_voice_api_key", voice_api_key.strip(), "str", "Voice provider API key/token."))
            for key, value, value_type, desc in upserts:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key=key,
                    value=value,
                    value_type=value_type,
                    description=desc,
                    is_active=True,
                    actor=user.username,
                )
            st.success("Voice runtime settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save voice runtime settings: {exc}")


def _render_ai_domain_toggles_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### AI Domain Toggles")
    st.caption("Enable/disable AI features by domain without redeploying.")

    def _is_enabled(key: str, default: bool = True) -> bool:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=False)
        if row is None:
            return bool(default)
        return str(row.value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    with st.form("admin_ai_domain_toggles_form"):
        d1, d2 = st.columns(2)
        with d1:
            chat_enabled = st.checkbox(
                "Ask GoldenStackers Chat",
                value=_is_enabled("ai_domain_chat_enabled", True),
            )
            comp_enabled = st.checkbox(
                "Comp Tool",
                value=_is_enabled("ai_domain_comp_tool_enabled", True),
            )
        with d2:
            grader_enabled = st.checkbox(
                "Coin Grader",
                value=_is_enabled("ai_domain_coin_grader_enabled", True),
            )
            identifier_enabled = st.checkbox(
                "Coin Identifier",
                value=_is_enabled("ai_domain_coin_identifier_enabled", True),
            )
        save_domains = st.form_submit_button("Save AI Domain Toggles")
    if save_domains:
        try:
            toggles = [
                ("ai_domain_chat_enabled", chat_enabled, "Enable/disable Ask GoldenStackers chat."),
                ("ai_domain_comp_tool_enabled", comp_enabled, "Enable/disable Comp Tool features."),
                ("ai_domain_coin_grader_enabled", grader_enabled, "Enable/disable Coin Grader features."),
                ("ai_domain_coin_identifier_enabled", identifier_enabled, "Enable/disable Coin Identifier features."),
            ]
            for key, enabled, desc in toggles:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key=key,
                    value="true" if bool(enabled) else "false",
                    value_type="bool",
                    description=desc,
                    is_active=True,
                    actor=user.username,
                )
            st.success("AI domain toggles saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save AI domain toggles: {exc}")


def _runtime_setting_audit_history(repo: InventoryRepository, setting_id: int) -> list[dict]:
    rows = repo.list_audit_logs_for_entity(
        entity_type="runtime_setting",
        entity_id=setting_id,
        limit=500,
    )
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.changes_json or "{}")
        except Exception:
            payload = {}
        value_before = None
        value_after = None
        if isinstance(payload, dict):
            if isinstance(payload.get("value"), dict):
                value_before = payload.get("value", {}).get("before")
                value_after = payload.get("value", {}).get("after")
            # Backward-compatible fallback if create payload later includes value under `after`.
            after_obj = payload.get("after") if isinstance(payload.get("after"), dict) else {}
            if value_after is None and "value" in after_obj:
                value_after = after_obj.get("value")
        out.append(
            {
                "audit_id": int(row.id),
                "created_at": row.created_at,
                "actor": row.actor,
                "action": row.action,
                "value_before": "" if value_before is None else str(value_before),
                "value_after": "" if value_after is None else str(value_after),
                "raw_changes": payload,
            }
        )
    return out


def _wipe_operational_data(
    repo: InventoryRepository,
    *,
    include_shipping_presets: bool,
    include_document_templates: bool,
    include_audit_logs: bool,
) -> dict[str, int]:
    targets: list[type] = [
        ReturnRecord,
        Sale,
        OrderItem,
        Order,
        MediaAsset,
        MarketplaceListing,
        ProductLotAssignment,
        InventoryMovement,
        Product,
        PurchaseLot,
        InventorySource,
    ]
    if include_shipping_presets:
        targets.append(ShippingPreset)
    if include_document_templates:
        targets.append(DocumentTemplateProfile)
    if include_audit_logs:
        targets.append(AuditLog)

    counts: dict[str, int] = {}
    for model in targets:
        deleted = repo.db.execute(delete(model)).rowcount or 0
        counts[model.__tablename__] = int(deleted)
    repo.db.commit()
    return counts


def _governance_snapshot_counts(
    repo: InventoryRepository,
    *,
    lookback_days: int,
    max_rows: int,
) -> dict[str, int]:
    cutoff = utcnow_naive() - timedelta(days=max(1, int(lookback_days)))
    capped_rows = max(100, min(10000, int(max_rows)))
    nav_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "navigation",
                AuditLog.action.in_(["workspace_handoff_applied", "workspace_handoff_cleared"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    feedback_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_feedback",
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    parity_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type.in_(["workspace_parity", "workspace_parity_decision", "workspace_followup"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    comp_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type.in_(["comp_photo_retry", "comp_domain_recommendation"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    return {
        "handoff_events": int(nav_count),
        "workspace_feedback_events": int(feedback_count),
        "parity_followup_events": int(parity_count),
        "photo_comp_events": int(comp_count),
    }


def _record_governance_snapshot_event(
    repo: InventoryRepository,
    *,
    actor: str,
    lookback_days: int,
    max_rows: int,
    source: str,
    download_intent: bool = False,
) -> dict[str, int]:
    counts = _governance_snapshot_counts(repo, lookback_days=int(lookback_days), max_rows=int(max_rows))
    snapshot_time = utcnow_naive()
    repo.record_audit_event(
        entity_type="governance_export",
        entity_id=None,
        action="snapshot",
        actor=actor,
        changes={
            "environment": settings.app_env,
            "recorded_at": snapshot_time.isoformat(timespec="seconds"),
            "source": str(source or "").strip() or "admin",
            "scheduled": False,
            "lookback_days": int(lookback_days),
            "max_rows_per_scope": int(max_rows),
            "counts": counts,
            "download_intent": bool(download_intent),
        },
    )
    return counts


def _render_governance_exports_hub(repo: InventoryRepository, user) -> None:
    st.markdown("### Governance Exports")
    st.caption(
        "Centralized export hub for operations governance artifacts across handoffs, workspace feedback, parity/follow-ups, and photo-comp tuning."
    )
    c1, c2 = st.columns(2)
    with c1:
        lookback_days = st.number_input(
            "Lookback Days",
            min_value=1,
            max_value=365,
            value=30,
            step=1,
            key="admin_governance_export_hub_lookback_days",
        )
    with c2:
        max_rows = st.number_input(
            "Max Rows Per Scope",
            min_value=100,
            max_value=10000,
            value=2000,
            step=100,
            key="admin_governance_export_hub_max_rows",
        )

    cutoff = utcnow_naive() - timedelta(days=int(lookback_days))
    load_governance_event_exports = st.checkbox(
        "Load Governance Event Exports (slower)",
        value=False,
        key="admin_governance_export_hub_load_events",
        help="Defers handoff/feedback/parity/photo-comp audit-log export reads until requested.",
    )
    nav_logs: list[AuditLog] = []
    feedback_logs: list[AuditLog] = []
    parity_logs: list[AuditLog] = []
    comp_logs: list[AuditLog] = []
    if load_governance_event_exports:
        nav_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "navigation",
                AuditLog.action.in_(["workspace_handoff_applied", "workspace_handoff_cleared"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(max_rows))
        ).all()
        feedback_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_feedback",
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(max_rows))
        ).all()
        parity_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type.in_(["workspace_parity", "workspace_parity_decision", "workspace_followup"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(max_rows))
        ).all()
        comp_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type.in_(["comp_photo_retry", "comp_domain_recommendation"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(max_rows))
        ).all()
    else:
        st.caption("Governance event exports are skipped by default. Enable `Load Governance Event Exports (slower)` to fetch them.")

    def _rows(logs: list[AuditLog]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in logs:
            payload = _audit_changes(row)
            out.append(
                {
                    "id": int(row.id),
                    "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "entity_type": str(row.entity_type or ""),
                    "action": str(row.action or ""),
                    "actor": str(row.actor or ""),
                    "entity_id": int(row.entity_id) if row.entity_id is not None else "",
                    "payload_json": json.dumps(payload, ensure_ascii=True)[:2000],
                }
            )
        return out

    nav_df = pd.DataFrame(_rows(nav_logs))
    feedback_df = pd.DataFrame(_rows(feedback_logs))
    parity_df = pd.DataFrame(_rows(parity_logs))
    comp_df = pd.DataFrame(_rows(comp_logs))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Handoff Events", int(len(nav_df)))
    m2.metric("Workspace Feedback Events", int(len(feedback_df)))
    m3.metric("Parity/Follow-up Events", int(len(parity_df)))
    m4.metric("Photo-Comp Governance Events", int(len(comp_df)))

    with st.expander("Preview Export Coverage", expanded=False):
        p1, p2 = st.columns(2)
        with p1:
            st.caption("Handoff events")
            st.dataframe(nav_df.head(20), use_container_width=True, hide_index=True)
        with p2:
            st.caption("Workspace feedback")
            st.dataframe(feedback_df.head(20), use_container_width=True, hide_index=True)
        p3, p4 = st.columns(2)
        with p3:
            st.caption("Parity + follow-up")
            st.dataframe(parity_df.head(20), use_container_width=True, hide_index=True)
        with p4:
            st.caption("Photo-comp governance")
            st.dataframe(comp_df.head(20), use_container_width=True, hide_index=True)

    all_bundle_buffer = BytesIO()
    with zipfile.ZipFile(all_bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
        metadata_df = pd.DataFrame(
            [
                {
                    "environment": settings.app_env,
                    "lookback_days": int(lookback_days),
                    "max_rows_per_scope": int(max_rows),
                    "generated_at_utc": utcnow_naive().isoformat(),
                    "generated_by": str(user.username or ""),
                    "handoff_events": int(len(nav_df)),
                    "workspace_feedback_events": int(len(feedback_df)),
                    "parity_followup_events": int(len(parity_df)),
                    "photo_comp_events": int(len(comp_df)),
                }
            ]
        )
        bundle_zip.writestr("governance_metadata.csv", metadata_df.to_csv(index=False))
        bundle_zip.writestr("handoff_events.csv", nav_df.to_csv(index=False))
        bundle_zip.writestr("workspace_feedback_events.csv", feedback_df.to_csv(index=False))
        bundle_zip.writestr("parity_followup_events.csv", parity_df.to_csv(index=False))
        bundle_zip.writestr("photo_comp_governance_events.csv", comp_df.to_csv(index=False))
    all_bundle_buffer.seek(0)
    st.download_button(
        "Export All Governance Bundles (ZIP)",
        data=all_bundle_buffer.getvalue(),
        file_name=(
            f"governance_exports_bundle_{settings.app_env}_"
            f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
        ),
        mime="application/zip",
        key="admin_governance_export_hub_all_zip_btn",
    )

    split1, split2, split3, split4 = st.columns(4)
    with split1:
        st.download_button(
            "Download Handoff CSV",
            data=nav_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_handoff_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_handoff_csv_btn",
        )
    with split2:
        st.download_button(
            "Download Feedback CSV",
            data=feedback_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_feedback_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_feedback_csv_btn",
        )
    with split3:
        st.download_button(
            "Download Parity CSV",
            data=parity_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_parity_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_parity_csv_btn",
        )
    with split4:
        st.download_button(
            "Download Photo-Comp CSV",
            data=comp_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_photo_comp_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_comp_csv_btn",
        )

    st.markdown("#### Workflow State Governance")
    st.caption(
        "Review DB-backed workflow drafts/events and run retention cleanup for stale workflow state records."
    )
    wf1, wf2, wf3, wf4 = st.columns(4)
    with wf1:
        workflow_filter = st.text_input(
            "Workflow Key Filter (optional)",
            value="",
            key="admin_workflow_state_filter_workflow_key",
            placeholder="listing_wizard / ebay_workspace_setup",
        ).strip().lower()
    with wf2:
        workflow_user_filter = st.text_input(
            "Username Filter (optional)",
            value="",
            key="admin_workflow_state_filter_username",
            placeholder="admin",
        ).strip()
    with wf3:
        workflow_draft_limit = st.number_input(
            "Draft/Event Row Limit",
            min_value=50,
            max_value=5000,
            value=1000,
            step=50,
            key="admin_workflow_state_row_limit",
        )
    with wf4:
        workflow_active_only = st.checkbox(
            "Active Drafts Only",
            value=False,
            key="admin_workflow_state_active_only",
        )
    load_workflow_events = st.checkbox(
        "Load Workflow Events (slower)",
        value=False,
        key="admin_workflow_state_load_events",
        help="Defers workflow event-history queries until explicitly requested.",
    )

    workflow_drafts = repo.list_workflow_drafts(
        environment=settings.app_env,
        workflow_key=workflow_filter,
        username=workflow_user_filter,
        active_only=bool(workflow_active_only),
        limit=int(workflow_draft_limit),
    )
    workflow_events = []
    if load_workflow_events:
        if workflow_filter:
            workflow_events = repo.list_workflow_events(
                environment=settings.app_env,
                workflow_key=workflow_filter,
                username=workflow_user_filter,
                limit=int(workflow_draft_limit),
            )
        elif workflow_user_filter:
            # If no workflow_key is supplied, pull common workflow streams for this user.
            merged_events: list[WorkflowEvent] = []
            for wf_key in [
                "listing_wizard",
                "listings_ebay_publish",
                "ebay_workspace_setup",
                "coin_intake_wizard",
                "inventory_intake_wizard",
            ]:
                merged_events.extend(
                    repo.list_workflow_events(
                        environment=settings.app_env,
                        workflow_key=wf_key,
                        username=workflow_user_filter,
                        limit=max(50, int(workflow_draft_limit) // 5),
                    )
                )
            merged_events.sort(
                key=lambda row: (getattr(row, "created_at", datetime.min), int(getattr(row, "id", 0))),
                reverse=True,
            )
            workflow_events = merged_events[: int(workflow_draft_limit)]
    else:
        st.caption("Workflow events are skipped by default. Enable `Load Workflow Events (slower)` to fetch them.")

    workflow_draft_rows: list[dict[str, Any]] = []
    for row in workflow_drafts:
        payload = {}
        try:
            payload = json.loads(str(getattr(row, "draft_json", "") or "{}"))
        except Exception:
            payload = {}
        workflow_draft_rows.append(
            {
                "id": int(row.id),
                "workflow_key": str(row.workflow_key or ""),
                "username": str(row.username or ""),
                "scope_key": str(row.scope_key or ""),
                "status": str(row.status or ""),
                "is_active": bool(row.is_active),
                "autosave_count": int(row.autosave_count or 0),
                "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else "",
                "resumed_at": row.resumed_at.isoformat(timespec="seconds") if row.resumed_at else "",
                "expires_at": row.expires_at.isoformat(timespec="seconds") if row.expires_at else "",
                "payload_preview": json.dumps(payload, ensure_ascii=True)[:500],
            }
        )
    workflow_event_rows: list[dict[str, Any]] = []
    for row in workflow_events:
        payload = {}
        try:
            payload = json.loads(str(getattr(row, "payload_json", "") or "{}"))
        except Exception:
            payload = {}
        workflow_event_rows.append(
            {
                "id": int(row.id),
                "draft_id": int(row.draft_id) if row.draft_id is not None else "",
                "workflow_key": str(row.workflow_key or ""),
                "username": str(row.username or ""),
                "scope_key": str(row.scope_key or ""),
                "action": str(row.action or ""),
                "status": str(row.status or ""),
                "created_by": str(row.created_by or ""),
                "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                "message": str(row.message or "")[:200],
                "payload_preview": json.dumps(payload, ensure_ascii=True)[:500],
            }
        )

    workflow_drafts_df = pd.DataFrame(workflow_draft_rows)
    workflow_events_df = pd.DataFrame(workflow_event_rows)

    wm1, wm2 = st.columns(2)
    wm1.metric("Workflow Draft Rows", int(len(workflow_drafts_df)))
    wm2.metric("Workflow Event Rows", int(len(workflow_events_df)))

    with st.expander("Preview Workflow State Coverage", expanded=False):
        pd1, pd2 = st.columns(2)
        with pd1:
            st.caption("Workflow Drafts")
            st.dataframe(workflow_drafts_df.head(40), use_container_width=True, hide_index=True)
        with pd2:
            st.caption("Workflow Events")
            st.dataframe(workflow_events_df.head(40), use_container_width=True, hide_index=True)

    wd1, wd2 = st.columns(2)
    with wd1:
        st.download_button(
            "Download Workflow Drafts CSV",
            data=workflow_drafts_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_workflow_drafts_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_workflow_drafts_csv_btn",
        )
    with wd2:
        st.download_button(
            "Download Workflow Events CSV",
            data=workflow_events_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_workflow_events_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_workflow_events_csv_btn",
        )

    st.markdown("##### Workflow State Retention Cleanup")
    rc1, rc2, rc3 = st.columns(3)
    with rc1:
        cleanup_draft_days = st.number_input(
            "Draft Retention Days",
            min_value=1,
            max_value=3650,
            value=30,
            step=1,
            key="admin_workflow_state_cleanup_draft_days",
        )
    with rc2:
        cleanup_event_days = st.number_input(
            "Event Retention Days",
            min_value=1,
            max_value=3650,
            value=90,
            step=1,
            key="admin_workflow_state_cleanup_event_days",
        )
    with rc3:
        run_cleanup = st.button(
            "Run Workflow Cleanup Now",
            key="admin_workflow_state_cleanup_run_btn",
            use_container_width=True,
        )
    if run_cleanup:
        try:
            cleanup_result = repo.cleanup_workflow_state(
                environment=settings.app_env,
                draft_retention_days=int(cleanup_draft_days),
                event_retention_days=int(cleanup_event_days),
                actor=user.username,
            )
            st.success(
                "Workflow cleanup complete: "
                f"deleted_stale_drafts={int(cleanup_result.get('deleted_stale_drafts', 0))}, "
                f"deleted_events_for_stale_drafts={int(cleanup_result.get('deleted_events_for_stale_drafts', 0))}, "
                f"deleted_old_events={int(cleanup_result.get('deleted_old_events', 0))}."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Workflow cleanup failed: {exc}")

    st.markdown("#### Go-Live Evidence Pack")
    st.caption(
        "One-click bundle for release readiness review: governance exports, alert evidence, queue snapshot, and checklist snapshot."
    )
    load_go_live_diagnostics = st.checkbox(
        "Load Go-Live Diagnostics Tables (slower)",
        value=False,
        key="admin_go_live_load_diagnostics",
        help="Defers heavy alert/validation/queue/history diagnostics reads until explicitly requested.",
    )
    load_go_live_full_history = st.checkbox(
        "Load Full Go-Live Sign-Off History (slowest)",
        value=False,
        key="admin_go_live_load_full_history",
        help="When disabled, history tables use a smaller recent window for faster default load.",
    )
    if not load_go_live_diagnostics:
        st.caption(
            "Diagnostics tables are deferred. Enable `Load Go-Live Diagnostics Tables (slower)` for full evidence counts/history."
        )
    if not load_go_live_full_history:
        st.caption("Sign-off/history tables are using recent-window mode. Enable full-history toggle for deeper exports.")
    signoff_history_limit = 500 if load_go_live_full_history else 50
    restore_drill_history_limit = 1000 if load_go_live_full_history else 100
    go_live_section_signoff_limit = 2000 if load_go_live_full_history else 200
    legal_signoff_limit = 1200 if load_go_live_full_history else 120
    dr_checklist_limit = 1000 if load_go_live_full_history else 120
    now_ts = utcnow_naive()
    try:
        alembic_version = str(repo.db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one())
    except Exception:
        alembic_version = "unknown"
    env_values = read_env_file(".env")
    env_defaults = read_env_file(".env.example")
    req_env = required_env_keys()
    env_missing = [
        key
        for key in sorted(req_env)
        if key not in env_values or not str(env_values.get(key, "")).strip()
    ]
    runtime_rows = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
    by_key = {str(row.key): row for row in runtime_rows}
    req_runtime = required_runtime_keys()
    runtime_missing = [
        key
        for key in sorted(req_runtime)
        if key not in by_key or not bool(getattr(by_key[key], "is_active", False))
    ]
    untracked_env_keys = sorted([k for k in env_values.keys() if k not in env_defaults])

    integration_event_rows_30d = []
    if load_go_live_diagnostics:
        integration_event_rows_30d = repo.db.execute(
            text(
                """
                SELECT created_at, actor, action, changes_json
                FROM audit_logs
                WHERE entity_type = 'integration_event'
                  AND created_at >= :since
                ORDER BY created_at DESC
                LIMIT 4000
                """
            ),
            {"since": now_ts - timedelta(days=30)},
        ).all()
    alert_window_start = now_ts - timedelta(days=7)
    alert_rows_raw = [
        row
        for row in integration_event_rows_30d
        if getattr(row, "__len__", lambda: 0)() >= 1
        and row[0] is not None
        and row[0] >= alert_window_start
    ]
    critical_alert_evidence_rows: list[dict[str, Any]] = []
    for created_at, actor, action, changes_json in alert_rows_raw:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        integration_name = str(after.get("integration") or "").strip().lower()
        action_name = str(after.get("action") or action or "").strip().lower()
        status = str(after.get("status") or "").strip().lower()
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        is_health_alert = (
            integration_name == "system_health"
            and action_name in {"critical_signal_alert", "critical_signal_alert_manual"}
        )
        is_slack_dispatch = (
            integration_name == "slack"
            and action_name == "dispatch_system_health_critical"
        )
        if not (is_health_alert or is_slack_dispatch):
            continue
        critical_alert_evidence_rows.append(
            {
                "created_at": str(created_at or ""),
                "actor": str(actor or ""),
                "integration": integration_name,
                "action": action_name,
                "status": status,
                "critical_signals": ", ".join(str(x) for x in (details.get("critical_signals") or [])),
                "channel": str(details.get("channel") or ""),
                "queue_job_id": str(details.get("queue_job_id") or ""),
                "dispatch_mode": (
                    "queued"
                    if bool(details.get("queued")) or str(status) == "queued"
                    else ("sent" if str(status) == "success" else str(status or ""))
                ),
                "error": str(details.get("error") or "")[:200],
            }
        )
    critical_alert_evidence_df = pd.DataFrame(critical_alert_evidence_rows)

    provider_validation_rows_raw = list(integration_event_rows_30d)
    provider_validation_rows: list[dict[str, Any]] = []
    for created_at, actor, action, changes_json in provider_validation_rows_raw:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        if str(after.get("integration") or "").strip().lower() != "shipping_provider_validation":
            continue
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        provider_validation_rows.append(
            {
                "created_at": str(created_at or ""),
                "actor": str(actor or ""),
                "action": str(after.get("action") or action or ""),
                "status": str(after.get("status") or ""),
                "target_env": str(details.get("target_env") or ""),
                "provider": str(details.get("provider") or ""),
                "sale_id": details.get("sale_id"),
                "queue_job_id": details.get("queue_job_id"),
                "queue_status": str(details.get("queue_status") or ""),
                "label_id": str(details.get("label_id") or ""),
                "tracking_number": str(details.get("tracking_number") or ""),
                "message": str(details.get("message") or ""),
                "validation_notes": str(details.get("validation_notes") or ""),
                "error": str(details.get("error") or "")[:220],
            }
        )
    provider_validation_df = pd.DataFrame(provider_validation_rows)
    provider_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "shipping_provider_validation_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    provider_signoff_rows: list[dict[str, Any]] = []
    latest_signoff_by_env: dict[str, dict[str, Any]] = {}
    for row in provider_signoff_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        provider_signoff_rows.append(entry)
        if target_env and target_env not in latest_signoff_by_env:
            latest_signoff_by_env[target_env] = entry
    provider_signoff_df = pd.DataFrame(provider_signoff_rows)
    provider_signoff_dev_status = str((latest_signoff_by_env.get("dev") or {}).get("status") or "")
    provider_signoff_prod_status = str((latest_signoff_by_env.get("prod") or {}).get("status") or "")
    provider_signoff_dev_ready = provider_signoff_dev_status == "approved"
    provider_signoff_prod_ready = provider_signoff_prod_status == "approved"

    health_calibration_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "system_health_calibration_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    health_calibration_rows: list[dict[str, Any]] = []
    latest_health_calibration_by_env: dict[str, dict[str, Any]] = {}
    for row in health_calibration_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        health_calibration_rows.append(entry)
        if target_env and target_env not in latest_health_calibration_by_env:
            latest_health_calibration_by_env[target_env] = entry
    health_calibration_df = pd.DataFrame(health_calibration_rows)
    health_calibration_dev_status = str((latest_health_calibration_by_env.get("dev") or {}).get("status") or "")
    health_calibration_prod_status = str((latest_health_calibration_by_env.get("prod") or {}).get("status") or "")
    health_calibration_dev_ready = health_calibration_dev_status == "approved"
    health_calibration_prod_ready = health_calibration_prod_status == "approved"

    health_alert_routing_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "system_health_alert_routing_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    health_alert_routing_rows: list[dict[str, Any]] = []
    latest_health_alert_routing_by_env: dict[str, dict[str, Any]] = {}
    for row in health_alert_routing_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "channel_routing_confirmed": bool(payload.get("channel_routing_confirmed")),
            "owner_confirmed": bool(payload.get("owner_confirmed")),
            "escalation_confirmed": bool(payload.get("escalation_confirmed")),
            "runbook_confirmed": bool(payload.get("runbook_confirmed")),
            "notes": str(payload.get("notes") or "")[:220],
        }
        health_alert_routing_rows.append(entry)
        if target_env and target_env not in latest_health_alert_routing_by_env:
            latest_health_alert_routing_by_env[target_env] = entry
    health_alert_routing_df = pd.DataFrame(health_alert_routing_rows)
    health_alert_routing_dev_status = str((latest_health_alert_routing_by_env.get("dev") or {}).get("status") or "")
    health_alert_routing_prod_status = str((latest_health_alert_routing_by_env.get("prod") or {}).get("status") or "")
    health_alert_routing_dev_ready = health_alert_routing_dev_status == "approved"
    health_alert_routing_prod_ready = health_alert_routing_prod_status == "approved"

    automation_hardening_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "integration_automation_hardening_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    automation_hardening_rows: list[dict[str, Any]] = []
    latest_automation_hardening_by_env: dict[str, dict[str, Any]] = {}
    for row in automation_hardening_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "guardrails_verified": bool(payload.get("guardrails_verified")),
            "approval_policy_reviewed": bool(payload.get("approval_policy_reviewed")),
            "runbook_signed_off": bool(payload.get("runbook_signed_off")),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        automation_hardening_rows.append(entry)
        if target_env and target_env not in latest_automation_hardening_by_env:
            latest_automation_hardening_by_env[target_env] = entry
    automation_hardening_df = pd.DataFrame(automation_hardening_rows)
    automation_hardening_dev_status = str((latest_automation_hardening_by_env.get("dev") or {}).get("status") or "")
    automation_hardening_prod_status = str((latest_automation_hardening_by_env.get("prod") or {}).get("status") or "")
    automation_hardening_dev_ready = automation_hardening_dev_status == "approved"
    automation_hardening_prod_ready = automation_hardening_prod_status == "approved"

    fee_calibration_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "ebay_fee_calibration_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    fee_calibration_rows: list[dict[str, Any]] = []
    latest_fee_calibration_by_env: dict[str, dict[str, Any]] = {}
    for row in fee_calibration_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "sample_order_count": int(payload.get("sample_order_count") or 0),
            "assumption_snapshot": str(payload.get("assumption_snapshot") or ""),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        fee_calibration_rows.append(entry)
        if target_env and target_env not in latest_fee_calibration_by_env:
            latest_fee_calibration_by_env[target_env] = entry
    fee_calibration_df = pd.DataFrame(fee_calibration_rows)
    fee_calibration_dev_status = str((latest_fee_calibration_by_env.get("dev") or {}).get("status") or "")
    fee_calibration_prod_status = str((latest_fee_calibration_by_env.get("prod") or {}).get("status") or "")
    fee_calibration_dev_ready = fee_calibration_dev_status == "approved"
    fee_calibration_prod_ready = fee_calibration_prod_status == "approved"

    economics_threshold_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "economics_threshold_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    economics_threshold_rows: list[dict[str, Any]] = []
    latest_economics_threshold_by_env: dict[str, dict[str, Any]] = {}
    for row in economics_threshold_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "min_actual_margin_alert_pct": float(payload.get("min_actual_margin_alert_pct") or 0.0),
            "max_avg_fee_variance_alert_usd": float(payload.get("max_avg_fee_variance_alert_usd") or 0.0),
            "min_group_sales_for_alert": int(payload.get("min_group_sales_for_alert") or 0),
            "assumption_snapshot": str(payload.get("assumption_snapshot") or ""),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        economics_threshold_rows.append(entry)
        if target_env and target_env not in latest_economics_threshold_by_env:
            latest_economics_threshold_by_env[target_env] = entry
    economics_threshold_df = pd.DataFrame(economics_threshold_rows)
    economics_threshold_dev_status = str((latest_economics_threshold_by_env.get("dev") or {}).get("status") or "")
    economics_threshold_prod_status = str((latest_economics_threshold_by_env.get("prod") or {}).get("status") or "")
    economics_threshold_dev_ready = economics_threshold_dev_status == "approved"
    economics_threshold_prod_ready = economics_threshold_prod_status == "approved"

    lifecycle_retention_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "lifecycle_retention_policy_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(signoff_history_limit)
    ).all()
    lifecycle_retention_signoff_rows: list[dict[str, Any]] = []
    latest_lifecycle_retention_signoff_by_env: dict[str, dict[str, Any]] = {}
    for row in lifecycle_retention_signoff_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "cleanup_enabled": bool(payload.get("cleanup_enabled")),
            "cleanup_timezone": str(payload.get("cleanup_timezone") or ""),
            "cleanup_local_time": str(payload.get("cleanup_local_time") or ""),
            "retain_days_media": int(payload.get("retain_days_media") or 0),
            "retain_days_listing": int(payload.get("retain_days_listing") or 0),
            "retain_days_lot": int(payload.get("retain_days_lot") or 0),
            "retain_days_product": int(payload.get("retain_days_product") or 0),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        lifecycle_retention_signoff_rows.append(entry)
        if target_env and target_env not in latest_lifecycle_retention_signoff_by_env:
            latest_lifecycle_retention_signoff_by_env[target_env] = entry
    lifecycle_retention_signoff_df = pd.DataFrame(lifecycle_retention_signoff_rows)
    lifecycle_retention_signoff_dev_status = str(
        (latest_lifecycle_retention_signoff_by_env.get("dev") or {}).get("status") or ""
    )
    lifecycle_retention_signoff_prod_status = str(
        (latest_lifecycle_retention_signoff_by_env.get("prod") or {}).get("status") or ""
    )
    lifecycle_retention_signoff_dev_ready = lifecycle_retention_signoff_dev_status == "approved"
    lifecycle_retention_signoff_prod_ready = lifecycle_retention_signoff_prod_status == "approved"

    restore_drill_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "backup_restore_drill")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(restore_drill_history_limit)
    ).all()
    restore_drill_rows: list[dict[str, Any]] = []
    for row in restore_drill_logs:
        payload = _audit_changes(row)
        created_at = row.created_at
        result = str(payload.get("result") or "").strip().lower()
        duration_minutes = payload.get("duration_minutes")
        rto_target_minutes = payload.get("rto_target_minutes")
        duration_int = int(duration_minutes) if str(duration_minutes or "").isdigit() else None
        rto_target_int = int(rto_target_minutes) if str(rto_target_minutes or "").isdigit() else None
        restore_drill_rows.append(
            {
                "id": int(row.id),
                "recorded_at_utc": created_at.isoformat(timespec="seconds") if created_at else "",
                "actor": str(row.actor or ""),
                "target_env": str(payload.get("target_env") or ""),
                "drill_date": str(payload.get("drill_date") or ""),
                "result": result,
                "source_type": str(payload.get("source_type") or ""),
                "source_ref": str(payload.get("source_ref") or ""),
                "duration_minutes": duration_int,
                "rto_target_minutes": rto_target_int,
                "rto_met": payload.get("rto_met"),
                "notes": str(payload.get("notes") or "")[:220],
            }
        )
    restore_drill_df = pd.DataFrame(restore_drill_rows)
    restore_drill_180d_count = 0
    restore_drill_180d_pass_count = 0
    restore_drill_last_at = ""
    restore_drill_last_result = ""
    restore_drill_last_pass_age_days: int | None = None
    since_restore_window = now_ts - timedelta(days=180)
    for row in restore_drill_logs:
        if row.created_at is None:
            continue
        if row.created_at >= since_restore_window:
            restore_drill_180d_count += 1
            payload = _audit_changes(row)
            if str(payload.get("result") or "").strip().lower() == "pass":
                restore_drill_180d_pass_count += 1
    if restore_drill_logs:
        latest_row = restore_drill_logs[0]
        latest_payload = _audit_changes(latest_row)
        restore_drill_last_at = latest_row.created_at.isoformat(timespec="seconds") if latest_row.created_at else ""
        restore_drill_last_result = str(latest_payload.get("result") or "").strip().lower()
    for row in restore_drill_logs:
        payload = _audit_changes(row)
        if str(payload.get("result") or "").strip().lower() != "pass":
            continue
        if row.created_at is None:
            continue
        restore_drill_last_pass_age_days = max(0, (now_ts - row.created_at).days)
        break

    go_live_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "go_live_section_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(go_live_section_signoff_limit)
    ).all()
    go_live_signoff_rows: list[dict[str, Any]] = []
    latest_go_live_signoff_by_key: dict[str, dict[str, Any]] = {}
    for row in go_live_signoff_logs:
        payload = _audit_changes(row)
        section_key = str(payload.get("section_key") or "").strip()
        item_key = str(payload.get("item_key") or "").strip()
        composite_key = f"{section_key}::{item_key}" if section_key and item_key else ""
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "section_key": section_key,
            "item_key": item_key,
            "status": str(payload.get("status") or "").strip().lower(),
            "owner": str(payload.get("owner") or ""),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "signoff_date": str(payload.get("signoff_date") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        go_live_signoff_rows.append(entry)
        if composite_key and composite_key not in latest_go_live_signoff_by_key:
            latest_go_live_signoff_by_key[composite_key] = entry
    go_live_signoff_df = pd.DataFrame(go_live_signoff_rows)
    go_live_signoff_total = len(latest_go_live_signoff_by_key)
    go_live_signoff_approved = sum(
        1 for row in latest_go_live_signoff_by_key.values() if str(row.get("status") or "").strip().lower() == "approved"
    )
    legal_policy_catalog: list[tuple[str, str]] = [
        ("tax_treatment", "Tax treatment policy"),
        ("record_retention", "Invoice/receipt retention policy"),
        ("marketplace_policy", "Marketplace policy conformance"),
        ("privacy_data_handling", "Privacy/data handling policy"),
        ("financial_posting_role_controls", "Financial posting role controls"),
        ("legal_accounting_reviewer", "Legal/accounting reviewer sign-off"),
    ]
    legal_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "commerce_legal_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(legal_signoff_limit)
    ).all()
    legal_signoff_rows: list[dict[str, Any]] = []
    latest_legal_signoff_by_key: dict[str, dict[str, Any]] = {}
    for row in legal_signoff_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        policy_key = str(payload.get("policy_key") or "").strip().lower()
        composite_key = f"{target_env}::{policy_key}" if target_env and policy_key else ""
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "policy_key": policy_key,
            "policy_label": str(payload.get("policy_label") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "owner": str(payload.get("owner") or ""),
            "signoff_date": str(payload.get("signoff_date") or ""),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        legal_signoff_rows.append(entry)
        if composite_key and composite_key not in latest_legal_signoff_by_key:
            latest_legal_signoff_by_key[composite_key] = entry
    legal_signoff_df = pd.DataFrame(legal_signoff_rows)
    legal_signoff_total = len(latest_legal_signoff_by_key)
    legal_signoff_approved = sum(
        1 for row in latest_legal_signoff_by_key.values() if str(row.get("status") or "").strip().lower() == "approved"
    )
    legal_required_total_prod = len(legal_policy_catalog)
    legal_approved_prod = sum(
        1
        for policy_key, _label in legal_policy_catalog
        if str(
            (latest_legal_signoff_by_key.get(f"prod::{policy_key}") or {}).get("status") or ""
        ).strip().lower()
        == "approved"
    )
    legal_ready_prod = legal_required_total_prod > 0 and legal_approved_prod >= legal_required_total_prod

    dr_checklist_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "backup_dr_checklist")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(dr_checklist_limit)
    ).all()
    dr_checklist_rows: list[dict[str, Any]] = []
    for row in dr_checklist_logs:
        payload = _audit_changes(row)
        dr_checklist_rows.append(
            {
                "id": int(row.id),
                "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                "actor": str(row.actor or ""),
                "target_env": str(payload.get("target_env") or ""),
                "owner": str(payload.get("owner") or ""),
                "evidence_link": str(payload.get("evidence_link") or ""),
                "completed_count": payload.get("completed_count"),
                "total_count": payload.get("total_count"),
                "completion_percent": payload.get("completion_percent"),
            }
        )
    dr_checklist_df = pd.DataFrame(dr_checklist_rows)
    dr_checklist_180d_count = 0
    dr_checklist_latest_completion_pct: float | None = None
    since_checklist_window = now_ts - timedelta(days=180)
    for row in dr_checklist_logs:
        if row.created_at is None:
            continue
        if row.created_at >= since_checklist_window:
            dr_checklist_180d_count += 1
    if dr_checklist_logs:
        latest_payload = _audit_changes(dr_checklist_logs[0])
        try:
            dr_checklist_latest_completion_pct = float(latest_payload.get("completion_percent"))
        except Exception:
            dr_checklist_latest_completion_pct = None

    window_24h_start = now_ts - timedelta(hours=24)
    integration_event_24h_rows = [
        row
        for row in integration_event_rows_30d
        if getattr(row, "__len__", lambda: 0)() >= 1
        and row[0] is not None
        and row[0] >= window_24h_start
    ]
    signal_counts = {"queue_execute_exceptions": 0, "terminal_queue_failures": 0, "integration_warnings": 0}
    signal_samples: list[dict[str, Any]] = []
    for created_at, actor, action, changes_json in integration_event_24h_rows:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        status = str(after.get("status") or "").strip().lower()
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        if action and str(action).endswith("_execute_exception"):
            signal_counts["queue_execute_exceptions"] += 1
        if status == "failed":
            signal_counts["terminal_queue_failures"] += 1
        if status == "warning":
            signal_counts["integration_warnings"] += 1
        if status in {"error", "failed", "warning"} and len(signal_samples) < 200:
            signal_samples.append(
                {
                    "created_at": str(created_at or ""),
                    "actor": str(actor or ""),
                    "action": str(action or ""),
                    "status": status,
                    "integration": str(after.get("integration") or ""),
                    "error": str(details.get("error") or "")[:200],
                }
            )
    signal_counts_df = pd.DataFrame(
        [
            {"metric": "queue_execute_exceptions_24h", "count": int(signal_counts["queue_execute_exceptions"])},
            {"metric": "terminal_queue_failures_24h", "count": int(signal_counts["terminal_queue_failures"])},
            {"metric": "integration_warnings_24h", "count": int(signal_counts["integration_warnings"])},
        ]
    )
    signal_samples_df = pd.DataFrame(signal_samples)

    queue_rows = []
    if load_go_live_diagnostics:
        queue_rows = repo.db.scalars(
            select(IntegrationQueueJob)
            .where(IntegrationQueueJob.environment == settings.app_env)
            .order_by(IntegrationQueueJob.next_attempt_at.asc(), IntegrationQueueJob.id.desc())
            .limit(5000)
        ).all()
    queue_df = pd.DataFrame(
        [
            {
                "id": int(row.id),
                "environment": str(row.environment or ""),
                "integration": str(row.integration or ""),
                "action": str(row.action or ""),
                "status": str(row.status or ""),
                "retry_count": int(row.retry_count or 0),
                "max_retries": int(row.max_retries or 0),
                "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
                "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
                "completed_at": row.completed_at.isoformat() if row.completed_at else "",
                "last_error": str(row.last_error or "")[:500],
                "requested_by": str(row.requested_by or ""),
                "updated_by": str(row.updated_by or ""),
            }
            for row in queue_rows
        ]
    )

    missing_required_df = pd.DataFrame(
        [
            {"type": "required_env_missing", "key": key}
            for key in env_missing
        ]
        + [
            {"type": "required_runtime_missing_or_inactive", "key": key}
            for key in runtime_missing
        ]
    )
    env_untracked_df = pd.DataFrame([{"key": key} for key in untracked_env_keys])
    try:
        checklist_text = Path("GO_LIVE_CHECKLIST.md").read_text(encoding="utf-8")
    except Exception:
        checklist_text = "Unable to read GO_LIVE_CHECKLIST.md from workspace."
    checklist_match_rows = re.findall(r"^\s*-\s*\[( |~|x)\]\s+(.*)$", checklist_text, flags=re.MULTILINE)
    checklist_total = len(checklist_match_rows)
    checklist_done = sum(1 for state, _label in checklist_match_rows if str(state).lower() == "x")
    checklist_in_progress = sum(1 for state, _label in checklist_match_rows if str(state) == "~")
    checklist_not_started = sum(1 for state, _label in checklist_match_rows if str(state) == " ")
    checklist_completion_pct = (
        (float(checklist_done) / float(checklist_total) * 100.0) if checklist_total > 0 else 0.0
    )
    fee_calibration_required_envs = {"dev", "prod"}
    fee_calibration_approved_envs = {
        env_name
        for env_name in fee_calibration_required_envs
        if str((latest_fee_calibration_by_env.get(env_name) or {}).get("status") or "").strip().lower() == "approved"
    }
    fee_calibration_missing_envs = sorted(list(fee_calibration_required_envs - fee_calibration_approved_envs))
    economics_threshold_required_envs = {"dev", "prod"}
    economics_threshold_approved_envs = {
        env_name
        for env_name in economics_threshold_required_envs
        if str((latest_economics_threshold_by_env.get(env_name) or {}).get("status") or "").strip().lower()
        == "approved"
    }
    economics_threshold_missing_envs = sorted(list(economics_threshold_required_envs - economics_threshold_approved_envs))
    lifecycle_retention_required_envs = {"dev", "prod"}
    lifecycle_retention_approved_envs = {
        env_name
        for env_name in lifecycle_retention_required_envs
        if str((latest_lifecycle_retention_signoff_by_env.get(env_name) or {}).get("status") or "").strip().lower()
        == "approved"
    }
    lifecycle_retention_missing_envs = sorted(
        list(lifecycle_retention_required_envs - lifecycle_retention_approved_envs)
    )
    checklist_status_df = pd.DataFrame(
        [
            {
                "total_items": int(checklist_total),
                "done_items": int(checklist_done),
                "in_progress_items": int(checklist_in_progress),
                "not_started_items": int(checklist_not_started),
                "completion_percent": round(float(checklist_completion_pct), 2),
                "fee_calibration_required_env_count": int(len(fee_calibration_required_envs)),
                "fee_calibration_approved_env_count": int(len(fee_calibration_approved_envs)),
                "fee_calibration_missing_env_count": int(len(fee_calibration_missing_envs)),
                "fee_calibration_missing_envs": ",".join(fee_calibration_missing_envs),
                "economics_threshold_signoff_required_env_count": int(len(economics_threshold_required_envs)),
                "economics_threshold_signoff_approved_env_count": int(len(economics_threshold_approved_envs)),
                "economics_threshold_signoff_missing_env_count": int(len(economics_threshold_missing_envs)),
                "economics_threshold_signoff_missing_envs": ",".join(economics_threshold_missing_envs),
                "lifecycle_retention_signoff_required_env_count": int(len(lifecycle_retention_required_envs)),
                "lifecycle_retention_signoff_approved_env_count": int(len(lifecycle_retention_approved_envs)),
                "lifecycle_retention_signoff_missing_env_count": int(len(lifecycle_retention_missing_envs)),
                "lifecycle_retention_signoff_missing_envs": ",".join(lifecycle_retention_missing_envs),
            }
        ]
    )

    gl1, gl2, gl3, gl4 = st.columns(4)
    gl1.metric("Checklist Total", int(checklist_total))
    gl2.metric("Checklist Done", int(checklist_done))
    gl3.metric("Checklist In Progress", int(checklist_in_progress))
    gl4.metric("Checklist Completion", f"{checklist_completion_pct:.1f}%")
    gd1, gd2, gd3 = st.columns(3)
    gd1.metric("Restore Drills (180d)", int(restore_drill_180d_count))
    gd2.metric("Restore Drill Passes (180d)", int(restore_drill_180d_pass_count))
    gd3.metric("Latest Pass Age (days)", "n/a" if restore_drill_last_pass_age_days is None else int(restore_drill_last_pass_age_days))
    gs1, gs2 = st.columns(2)
    gs1.metric("Go-Live Sign-Off Items", int(go_live_signoff_total))
    gs2.metric("Go-Live Sign-Off Approved", int(go_live_signoff_approved))
    gls1, gls2 = st.columns(2)
    gls1.metric("Legal Sign-Off Items", int(legal_signoff_total))
    gls2.metric("Legal Sign-Off Approved", int(legal_signoff_approved))
    gls3, gls4 = st.columns(2)
    gls3.metric("Legal Approved (Prod)", f"{int(legal_approved_prod)}/{int(legal_required_total_prod)}")
    gls4.metric("Legal Ready (Prod)", "yes" if legal_ready_prod else "no")
    pv1, pv2 = st.columns(2)
    pv1.metric("Validation Sign-Off Dev", provider_signoff_dev_status or "missing")
    pv2.metric("Validation Sign-Off Prod", provider_signoff_prod_status or "missing")
    ph1, ph2 = st.columns(2)
    ph1.metric("Health Calibration Dev", health_calibration_dev_status or "missing")
    ph2.metric("Health Calibration Prod", health_calibration_prod_status or "missing")
    pr1, pr2 = st.columns(2)
    pr1.metric("Alert Routing Dev", health_alert_routing_dev_status or "missing")
    pr2.metric("Alert Routing Prod", health_alert_routing_prod_status or "missing")
    pa1, pa2 = st.columns(2)
    pa1.metric("Automation Hardening Dev", automation_hardening_dev_status or "missing")
    pa2.metric("Automation Hardening Prod", automation_hardening_prod_status or "missing")
    pf1, pf2 = st.columns(2)
    pf1.metric("Fee Calibration Dev", fee_calibration_dev_status or "missing")
    pf2.metric("Fee Calibration Prod", fee_calibration_prod_status or "missing")
    pet1, pet2 = st.columns(2)
    pet1.metric("Economics Thresholds Dev", economics_threshold_dev_status or "missing")
    pet2.metric("Economics Thresholds Prod", economics_threshold_prod_status or "missing")
    pl1, pl2 = st.columns(2)
    pl1.metric("Lifecycle Retention Dev", lifecycle_retention_signoff_dev_status or "missing")
    pl2.metric("Lifecycle Retention Prod", lifecycle_retention_signoff_prod_status or "missing")
    if fee_calibration_missing_envs:
        st.warning(
            "Fee calibration sign-off missing for: "
            + ", ".join(fee_calibration_missing_envs)
            + ". This now reduces go-live readiness score."
        )
    if economics_threshold_missing_envs:
        st.warning(
            "Economics threshold sign-off missing for: "
            + ", ".join(economics_threshold_missing_envs)
            + ". This now reduces go-live readiness score."
        )
    if lifecycle_retention_missing_envs:
        st.warning(
            "Lifecycle retention policy sign-off missing for: "
            + ", ".join(lifecycle_retention_missing_envs)
            + ". This now reduces go-live readiness score."
        )
    with st.expander("Readiness Scoring Config", expanded=False):
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            score_weight_checklist_gap_pct = st.number_input(
                "Checklist Gap Weight %",
                min_value=0,
                max_value=100,
                value=max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_weight_checklist_gap_pct", 40)))),
                step=1,
                key="admin_go_live_score_weight_checklist_gap_pct",
            )
            score_weight_env_missing = st.number_input(
                "Per Env Missing Penalty",
                min_value=0,
                max_value=50,
                value=max(0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_env_missing", 5)))),
                step=1,
                key="admin_go_live_score_weight_env_missing",
            )
            score_weight_runtime_missing = st.number_input(
                "Per Runtime Missing Penalty",
                min_value=0,
                max_value=50,
                value=max(0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_runtime_missing", 5)))),
                step=1,
                key="admin_go_live_score_weight_runtime_missing",
            )
            score_weight_terminal_queue_failure = st.number_input(
                "Per Terminal Failure Penalty",
                min_value=0,
                max_value=50,
                value=max(
                    0,
                    min(50, int(get_runtime_int(repo, "go_live_readiness_weight_terminal_queue_failure", 10))),
                ),
                step=1,
                key="admin_go_live_score_weight_terminal_queue_failure",
            )
        with sc2:
            score_weight_queue_execute_exception = st.number_input(
                "Per Execute Exception Penalty",
                min_value=0,
                max_value=50,
                value=max(
                    0,
                    min(50, int(get_runtime_int(repo, "go_live_readiness_weight_queue_execute_exception", 5))),
                ),
                step=1,
                key="admin_go_live_score_weight_queue_execute_exception",
            )
            score_penalty_terminal_queue_failure_max = st.number_input(
                "Terminal Failure Penalty Cap",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_terminal_queue_failure_max", 30))),
                ),
                step=1,
                key="admin_go_live_score_penalty_terminal_queue_failure_max",
            )
            score_penalty_queue_execute_exception_max = st.number_input(
                "Execute Exception Penalty Cap",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_queue_execute_exception_max", 20))),
                ),
                step=1,
                key="admin_go_live_score_penalty_queue_execute_exception_max",
            )
            score_penalty_integration_warnings_warn = st.number_input(
                "Warnings Penalty (Warn)",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_warn", 10))),
                ),
                step=1,
                key="admin_go_live_score_penalty_warnings_warn",
            )
        with sc3:
            score_penalty_integration_warnings_critical = st.number_input(
                "Warnings Penalty (Critical)",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(
                        100,
                        int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_critical", 20)),
                    ),
                ),
                step=1,
                key="admin_go_live_score_penalty_warnings_critical",
            )
            score_threshold_green = st.number_input(
                "Green Threshold",
                min_value=0,
                max_value=100,
                value=max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_green", 85)))),
                step=1,
                key="admin_go_live_score_threshold_green",
            )
            score_threshold_yellow = st.number_input(
                "Yellow Threshold",
                min_value=0,
                max_value=100,
                value=max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_yellow", 65)))),
                step=1,
                key="admin_go_live_score_threshold_yellow",
            )
        if st.button("Save Readiness Scoring Config", key="admin_go_live_score_config_save_btn"):
            try:
                updates = [
                    ("go_live_readiness_weight_checklist_gap_pct", str(int(score_weight_checklist_gap_pct))),
                    ("go_live_readiness_weight_env_missing", str(int(score_weight_env_missing))),
                    ("go_live_readiness_weight_runtime_missing", str(int(score_weight_runtime_missing))),
                    ("go_live_readiness_weight_terminal_queue_failure", str(int(score_weight_terminal_queue_failure))),
                    ("go_live_readiness_weight_queue_execute_exception", str(int(score_weight_queue_execute_exception))),
                    (
                        "go_live_readiness_penalty_terminal_queue_failure_max",
                        str(int(score_penalty_terminal_queue_failure_max)),
                    ),
                    (
                        "go_live_readiness_penalty_queue_execute_exception_max",
                        str(int(score_penalty_queue_execute_exception_max)),
                    ),
                    (
                        "go_live_readiness_penalty_integration_warnings_warn",
                        str(int(score_penalty_integration_warnings_warn)),
                    ),
                    (
                        "go_live_readiness_penalty_integration_warnings_critical",
                        str(int(score_penalty_integration_warnings_critical)),
                    ),
                    ("go_live_readiness_threshold_green", str(int(score_threshold_green))),
                    ("go_live_readiness_threshold_yellow", str(int(score_threshold_yellow))),
                ]
                for key, value in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type="int",
                        description="Go-live readiness scoring configuration.",
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Readiness scoring configuration saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save readiness scoring configuration: {exc}")
    readiness_score = 100.0
    readiness_weight_checklist_gap_pct = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_weight_checklist_gap_pct", 40)))
    )
    readiness_weight_env_missing = max(0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_env_missing", 5))))
    readiness_weight_runtime_missing = max(
        0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_runtime_missing", 5)))
    )
    readiness_weight_terminal_queue_failure = max(
        0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_terminal_queue_failure", 10)))
    )
    readiness_weight_queue_execute_exception = max(
        0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_queue_execute_exception", 5)))
    )
    readiness_penalty_terminal_queue_failure_max = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_terminal_queue_failure_max", 30)))
    )
    readiness_penalty_queue_execute_exception_max = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_queue_execute_exception_max", 20)))
    )
    readiness_penalty_integration_warnings_warn = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_warn", 10)))
    )
    readiness_penalty_integration_warnings_critical = max(
        0,
        min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_critical", 20))),
    )
    warnings_warn_threshold = max(1, int(get_runtime_int(repo, "health_integration_warnings_warn_24h", 10)))
    warnings_critical_threshold = max(
        warnings_warn_threshold,
        int(get_runtime_int(repo, "health_integration_warnings_critical_24h", 30)),
    )
    readiness_threshold_green = max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_green", 85))))
    readiness_threshold_yellow = max(
        0,
        min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_yellow", 65))),
    )
    readiness_score -= max(
        0.0,
        (100.0 - float(checklist_completion_pct)) * (float(readiness_weight_checklist_gap_pct) / 100.0),
    )
    readiness_score -= float(len(env_missing)) * float(readiness_weight_env_missing)
    readiness_score -= float(len(fee_calibration_missing_envs)) * float(readiness_weight_env_missing)
    readiness_score -= float(len(economics_threshold_missing_envs)) * float(readiness_weight_env_missing)
    readiness_score -= float(len(lifecycle_retention_missing_envs)) * float(readiness_weight_env_missing)
    readiness_score -= float(len(runtime_missing)) * float(readiness_weight_runtime_missing)
    readiness_score -= min(
        float(readiness_penalty_terminal_queue_failure_max),
        float(signal_counts["terminal_queue_failures"]) * float(readiness_weight_terminal_queue_failure),
    )
    readiness_score -= min(
        float(readiness_penalty_queue_execute_exception_max),
        float(signal_counts["queue_execute_exceptions"]) * float(readiness_weight_queue_execute_exception),
    )
    warning_count = int(signal_counts["integration_warnings"])
    if warning_count >= int(warnings_critical_threshold):
        readiness_score -= float(readiness_penalty_integration_warnings_critical)
    elif warning_count >= int(warnings_warn_threshold):
        readiness_score -= float(readiness_penalty_integration_warnings_warn)
    readiness_score = max(0.0, min(100.0, readiness_score))
    if readiness_score >= float(readiness_threshold_green):
        readiness_state = "green"
    elif readiness_score >= float(readiness_threshold_yellow):
        readiness_state = "yellow"
    else:
        readiness_state = "red"
    gr1, gr2 = st.columns(2)
    gr1.metric("Go-Live Readiness Score", f"{readiness_score:.1f}")
    gr2.metric("Go-Live Readiness State", readiness_state.upper())

    go_live_summary_df = pd.DataFrame(
        [
            {
                "environment": settings.app_env,
                "generated_at_utc": now_ts.isoformat(),
                "generated_by": str(user.username or ""),
                "alembic_version": alembic_version,
                "required_env_missing_count": int(len(env_missing)),
                "fee_calibration_signoff_missing_env_count": int(len(fee_calibration_missing_envs)),
                "fee_calibration_signoff_missing_envs": ",".join(fee_calibration_missing_envs),
                "economics_threshold_signoff_missing_env_count": int(len(economics_threshold_missing_envs)),
                "economics_threshold_signoff_missing_envs": ",".join(economics_threshold_missing_envs),
                "lifecycle_retention_signoff_missing_env_count": int(len(lifecycle_retention_missing_envs)),
                "lifecycle_retention_signoff_missing_envs": ",".join(lifecycle_retention_missing_envs),
                "required_runtime_missing_count": int(len(runtime_missing)),
                "env_untracked_count": int(len(untracked_env_keys)),
                "queue_job_count": int(len(queue_df)),
                "critical_alert_evidence_7d_count": int(len(critical_alert_evidence_df)),
                "provider_validation_runs_30d_count": int(len(provider_validation_df)),
                "provider_validation_signoff_dev_status": provider_signoff_dev_status,
                "provider_validation_signoff_prod_status": provider_signoff_prod_status,
                "provider_validation_signoff_dev_ready": bool(provider_signoff_dev_ready),
                "provider_validation_signoff_prod_ready": bool(provider_signoff_prod_ready),
                "health_calibration_signoff_dev_status": health_calibration_dev_status,
                "health_calibration_signoff_prod_status": health_calibration_prod_status,
                "health_calibration_signoff_dev_ready": bool(health_calibration_dev_ready),
                "health_calibration_signoff_prod_ready": bool(health_calibration_prod_ready),
                "health_alert_routing_signoff_dev_status": health_alert_routing_dev_status,
                "health_alert_routing_signoff_prod_status": health_alert_routing_prod_status,
                "health_alert_routing_signoff_dev_ready": bool(health_alert_routing_dev_ready),
                "health_alert_routing_signoff_prod_ready": bool(health_alert_routing_prod_ready),
                "automation_hardening_signoff_dev_status": automation_hardening_dev_status,
                "automation_hardening_signoff_prod_status": automation_hardening_prod_status,
                "automation_hardening_signoff_dev_ready": bool(automation_hardening_dev_ready),
                "automation_hardening_signoff_prod_ready": bool(automation_hardening_prod_ready),
                "fee_calibration_signoff_dev_status": fee_calibration_dev_status,
                "fee_calibration_signoff_prod_status": fee_calibration_prod_status,
                "fee_calibration_signoff_dev_ready": bool(fee_calibration_dev_ready),
                "fee_calibration_signoff_prod_ready": bool(fee_calibration_prod_ready),
                "economics_threshold_signoff_dev_status": economics_threshold_dev_status,
                "economics_threshold_signoff_prod_status": economics_threshold_prod_status,
                "economics_threshold_signoff_dev_ready": bool(economics_threshold_dev_ready),
                "economics_threshold_signoff_prod_ready": bool(economics_threshold_prod_ready),
                "lifecycle_retention_signoff_dev_status": lifecycle_retention_signoff_dev_status,
                "lifecycle_retention_signoff_prod_status": lifecycle_retention_signoff_prod_status,
                "lifecycle_retention_signoff_dev_ready": bool(lifecycle_retention_signoff_dev_ready),
                "lifecycle_retention_signoff_prod_ready": bool(lifecycle_retention_signoff_prod_ready),
                "restore_drills_180d_count": int(restore_drill_180d_count),
                "restore_drills_180d_pass_count": int(restore_drill_180d_pass_count),
                "restore_drill_last_at_utc": restore_drill_last_at,
                "restore_drill_last_result": restore_drill_last_result,
                "restore_drill_last_pass_age_days": restore_drill_last_pass_age_days,
                "go_live_signoff_items_total": int(go_live_signoff_total),
                "go_live_signoff_items_approved": int(go_live_signoff_approved),
                "legal_signoff_items_total": int(legal_signoff_total),
                "legal_signoff_items_approved": int(legal_signoff_approved),
                "legal_signoff_prod_approved_count": int(legal_approved_prod),
                "legal_signoff_prod_required_count": int(legal_required_total_prod),
                "legal_signoff_prod_ready": bool(legal_ready_prod),
                "dr_checklist_snapshots_180d_count": int(dr_checklist_180d_count),
                "dr_checklist_latest_completion_percent": dr_checklist_latest_completion_pct,
                "queue_execute_exceptions_24h": int(signal_counts["queue_execute_exceptions"]),
                "terminal_queue_failures_24h": int(signal_counts["terminal_queue_failures"]),
                "integration_warnings_24h": int(signal_counts["integration_warnings"]),
                "checklist_total_items": int(checklist_total),
                "checklist_done_items": int(checklist_done),
                "checklist_in_progress_items": int(checklist_in_progress),
                "checklist_not_started_items": int(checklist_not_started),
                "checklist_completion_percent": round(float(checklist_completion_pct), 2),
                "go_live_readiness_score": round(float(readiness_score), 2),
                "go_live_readiness_state": readiness_state,
            }
        ]
    )

    st.caption(
        "Evidence pack generation is on-demand to reduce Admin rerun latency. "
        "Generate when needed, then download."
    )
    pack_state_key = "admin_go_live_evidence_pack_state"
    if st.button(
        "Prepare Go-Live Evidence Pack (ZIP)",
        key="admin_go_live_evidence_pack_prepare_btn",
        help="Build and cache a fresh evidence bundle for download.",
    ):
        go_live_pack_buffer = BytesIO()
        with zipfile.ZipFile(go_live_pack_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as pack_zip:
            pack_zip.writestr("go_live_summary.csv", go_live_summary_df.to_csv(index=False))
            pack_zip.writestr("governance_metadata.csv", pd.DataFrame(
                [{
                    "environment": settings.app_env,
                    "lookback_days": int(lookback_days),
                    "max_rows_per_scope": int(max_rows),
                    "generated_at_utc": now_ts.isoformat(),
                    "generated_by": str(user.username or ""),
                }]
            ).to_csv(index=False))
            pack_zip.writestr("governance_handoff_events.csv", nav_df.to_csv(index=False))
            pack_zip.writestr("governance_workspace_feedback.csv", feedback_df.to_csv(index=False))
            pack_zip.writestr("governance_parity_followup.csv", parity_df.to_csv(index=False))
            pack_zip.writestr("governance_photo_comp.csv", comp_df.to_csv(index=False))
            pack_zip.writestr("critical_alert_evidence_7d.csv", critical_alert_evidence_df.to_csv(index=False))
            pack_zip.writestr("shipping_provider_validation_30d.csv", provider_validation_df.to_csv(index=False))
            pack_zip.writestr("shipping_provider_validation_signoffs.csv", provider_signoff_df.to_csv(index=False))
            pack_zip.writestr("system_health_calibration_signoffs.csv", health_calibration_df.to_csv(index=False))
            pack_zip.writestr("system_health_alert_routing_signoffs.csv", health_alert_routing_df.to_csv(index=False))
            pack_zip.writestr("integration_automation_hardening_signoffs.csv", automation_hardening_df.to_csv(index=False))
            pack_zip.writestr("ebay_fee_calibration_signoffs.csv", fee_calibration_df.to_csv(index=False))
            pack_zip.writestr("economics_threshold_signoffs.csv", economics_threshold_df.to_csv(index=False))
            pack_zip.writestr(
                "lifecycle_retention_policy_signoffs.csv",
                lifecycle_retention_signoff_df.to_csv(index=False),
            )
            pack_zip.writestr("backup_restore_drills.csv", restore_drill_df.to_csv(index=False))
            pack_zip.writestr("backup_dr_checklist_snapshots.csv", dr_checklist_df.to_csv(index=False))
            pack_zip.writestr("go_live_section_signoffs.csv", go_live_signoff_df.to_csv(index=False))
            pack_zip.writestr("commerce_legal_signoffs.csv", legal_signoff_df.to_csv(index=False))
            pack_zip.writestr("integration_error_signal_counts_24h.csv", signal_counts_df.to_csv(index=False))
            pack_zip.writestr("integration_error_signal_samples_24h.csv", signal_samples_df.to_csv(index=False))
            pack_zip.writestr("integration_queue_snapshot.csv", queue_df.to_csv(index=False))
            pack_zip.writestr("config_missing_required.csv", missing_required_df.to_csv(index=False))
            pack_zip.writestr("config_env_untracked_keys.csv", env_untracked_df.to_csv(index=False))
            pack_zip.writestr("go_live_checklist_status.csv", checklist_status_df.to_csv(index=False))
            pack_zip.writestr("GO_LIVE_CHECKLIST_snapshot.md", checklist_text)
        go_live_pack_buffer.seek(0)
        st.session_state[pack_state_key] = {
            "generated_at_utc": utcnow_naive().isoformat(timespec="seconds"),
            "file_name": f"go_live_evidence_pack_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.zip",
            "bytes": go_live_pack_buffer.getvalue(),
        }
        st.success("Go-live evidence pack prepared.")

    pack_state = st.session_state.get(pack_state_key)
    if isinstance(pack_state, dict) and pack_state.get("bytes"):
        st.caption(
            "Prepared at UTC: "
            f"{str(pack_state.get('generated_at_utc') or '')}"
        )
        st.download_button(
            "Download Go-Live Evidence Pack (ZIP)",
            data=pack_state.get("bytes"),
            file_name=str(pack_state.get("file_name") or f"go_live_evidence_pack_{settings.app_env}.zip"),
            mime="application/zip",
            key="admin_go_live_evidence_pack_zip_btn",
        )
    if st.button(
        "Record Evidence Capture Event",
        key="admin_go_live_evidence_capture_event_btn",
        help="Create an audit-stamped event that evidence was captured for this environment.",
    ):
        try:
            repo.record_audit_event(
                entity_type="go_live_evidence",
                entity_id=None,
                action="capture",
                actor=user.username,
                changes={
                    "environment": settings.app_env,
                    "captured_at_utc": utcnow_naive().isoformat(),
                    "captured_by": str(user.username or ""),
                    "alembic_version": alembic_version,
                    "required_env_missing_count": int(len(env_missing)),
                    "fee_calibration_signoff_missing_env_count": int(len(fee_calibration_missing_envs)),
                    "fee_calibration_signoff_missing_envs": ",".join(fee_calibration_missing_envs),
                    "economics_threshold_signoff_missing_env_count": int(len(economics_threshold_missing_envs)),
                    "economics_threshold_signoff_missing_envs": ",".join(economics_threshold_missing_envs),
                    "lifecycle_retention_signoff_missing_env_count": int(len(lifecycle_retention_missing_envs)),
                    "lifecycle_retention_signoff_missing_envs": ",".join(lifecycle_retention_missing_envs),
                    "required_runtime_missing_count": int(len(runtime_missing)),
                    "queue_job_count": int(len(queue_df)),
                    "critical_alert_evidence_7d_count": int(len(critical_alert_evidence_df)),
                    "provider_validation_runs_30d_count": int(len(provider_validation_df)),
                    "provider_validation_signoff_dev_status": provider_signoff_dev_status,
                    "provider_validation_signoff_prod_status": provider_signoff_prod_status,
                    "provider_validation_signoff_dev_ready": bool(provider_signoff_dev_ready),
                    "provider_validation_signoff_prod_ready": bool(provider_signoff_prod_ready),
                    "health_calibration_signoff_dev_status": health_calibration_dev_status,
                    "health_calibration_signoff_prod_status": health_calibration_prod_status,
                    "health_calibration_signoff_dev_ready": bool(health_calibration_dev_ready),
                    "health_calibration_signoff_prod_ready": bool(health_calibration_prod_ready),
                    "health_alert_routing_signoff_dev_status": health_alert_routing_dev_status,
                    "health_alert_routing_signoff_prod_status": health_alert_routing_prod_status,
                    "health_alert_routing_signoff_dev_ready": bool(health_alert_routing_dev_ready),
                    "health_alert_routing_signoff_prod_ready": bool(health_alert_routing_prod_ready),
                    "automation_hardening_signoff_dev_status": automation_hardening_dev_status,
                    "automation_hardening_signoff_prod_status": automation_hardening_prod_status,
                    "automation_hardening_signoff_dev_ready": bool(automation_hardening_dev_ready),
                    "automation_hardening_signoff_prod_ready": bool(automation_hardening_prod_ready),
                    "fee_calibration_signoff_dev_status": fee_calibration_dev_status,
                    "fee_calibration_signoff_prod_status": fee_calibration_prod_status,
                    "fee_calibration_signoff_dev_ready": bool(fee_calibration_dev_ready),
                    "fee_calibration_signoff_prod_ready": bool(fee_calibration_prod_ready),
                    "economics_threshold_signoff_dev_status": economics_threshold_dev_status,
                    "economics_threshold_signoff_prod_status": economics_threshold_prod_status,
                    "economics_threshold_signoff_dev_ready": bool(economics_threshold_dev_ready),
                    "economics_threshold_signoff_prod_ready": bool(economics_threshold_prod_ready),
                    "lifecycle_retention_signoff_dev_status": lifecycle_retention_signoff_dev_status,
                    "lifecycle_retention_signoff_prod_status": lifecycle_retention_signoff_prod_status,
                    "lifecycle_retention_signoff_dev_ready": bool(lifecycle_retention_signoff_dev_ready),
                    "lifecycle_retention_signoff_prod_ready": bool(lifecycle_retention_signoff_prod_ready),
                    "restore_drills_180d_count": int(restore_drill_180d_count),
                    "restore_drills_180d_pass_count": int(restore_drill_180d_pass_count),
                    "restore_drill_last_at_utc": restore_drill_last_at,
                    "restore_drill_last_result": restore_drill_last_result,
                    "restore_drill_last_pass_age_days": restore_drill_last_pass_age_days,
                    "go_live_signoff_items_total": int(go_live_signoff_total),
                    "go_live_signoff_items_approved": int(go_live_signoff_approved),
                    "legal_signoff_items_total": int(legal_signoff_total),
                    "legal_signoff_items_approved": int(legal_signoff_approved),
                    "legal_signoff_prod_approved_count": int(legal_approved_prod),
                    "legal_signoff_prod_required_count": int(legal_required_total_prod),
                    "legal_signoff_prod_ready": bool(legal_ready_prod),
                    "dr_checklist_snapshots_180d_count": int(dr_checklist_180d_count),
                    "dr_checklist_latest_completion_percent": dr_checklist_latest_completion_pct,
                    "queue_execute_exceptions_24h": int(signal_counts["queue_execute_exceptions"]),
                    "terminal_queue_failures_24h": int(signal_counts["terminal_queue_failures"]),
                    "integration_warnings_24h": int(signal_counts["integration_warnings"]),
                    "checklist_total_items": int(checklist_total),
                    "checklist_done_items": int(checklist_done),
                    "checklist_in_progress_items": int(checklist_in_progress),
                    "checklist_not_started_items": int(checklist_not_started),
                    "checklist_completion_percent": round(float(checklist_completion_pct), 2),
                    "go_live_readiness_score": round(float(readiness_score), 2),
                    "go_live_readiness_state": readiness_state,
                },
            )
            st.success("Go-live evidence capture event recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record go-live evidence capture event: {exc}")

    recent_capture_logs = []
    if load_go_live_diagnostics:
        recent_capture_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "go_live_evidence")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(20)
        ).all()
    else:
        st.caption("Recent evidence-capture history is deferred until diagnostics loading is enabled.")
    if recent_capture_logs:
        capture_rows: list[dict[str, Any]] = []
        for row in recent_capture_logs:
            payload = _audit_changes(row)
            capture_rows.append(
                {
                    "id": int(row.id),
                    "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "action": str(row.action or ""),
                    "env": str(payload.get("environment") or ""),
                    "checklist_completion_percent": payload.get("checklist_completion_percent"),
                    "go_live_readiness_score": payload.get("go_live_readiness_score"),
                    "go_live_readiness_state": payload.get("go_live_readiness_state"),
                    "required_env_missing_count": payload.get("required_env_missing_count"),
                    "fee_calibration_signoff_missing_env_count": payload.get("fee_calibration_signoff_missing_env_count"),
                    "fee_calibration_signoff_missing_envs": payload.get("fee_calibration_signoff_missing_envs"),
                    "economics_threshold_signoff_missing_env_count": payload.get(
                        "economics_threshold_signoff_missing_env_count"
                    ),
                    "economics_threshold_signoff_missing_envs": payload.get(
                        "economics_threshold_signoff_missing_envs"
                    ),
                    "lifecycle_retention_signoff_missing_env_count": payload.get(
                        "lifecycle_retention_signoff_missing_env_count"
                    ),
                    "lifecycle_retention_signoff_missing_envs": payload.get(
                        "lifecycle_retention_signoff_missing_envs"
                    ),
                    "required_runtime_missing_count": payload.get("required_runtime_missing_count"),
                    "provider_validation_signoff_dev_status": payload.get("provider_validation_signoff_dev_status"),
                    "provider_validation_signoff_prod_status": payload.get("provider_validation_signoff_prod_status"),
                    "health_calibration_signoff_dev_status": payload.get("health_calibration_signoff_dev_status"),
                    "health_calibration_signoff_prod_status": payload.get("health_calibration_signoff_prod_status"),
                    "health_alert_routing_signoff_dev_status": payload.get("health_alert_routing_signoff_dev_status"),
                    "health_alert_routing_signoff_prod_status": payload.get("health_alert_routing_signoff_prod_status"),
                    "automation_hardening_signoff_dev_status": payload.get("automation_hardening_signoff_dev_status"),
                    "automation_hardening_signoff_prod_status": payload.get("automation_hardening_signoff_prod_status"),
                    "fee_calibration_signoff_dev_status": payload.get("fee_calibration_signoff_dev_status"),
                    "fee_calibration_signoff_prod_status": payload.get("fee_calibration_signoff_prod_status"),
                    "economics_threshold_signoff_dev_status": payload.get("economics_threshold_signoff_dev_status"),
                    "economics_threshold_signoff_prod_status": payload.get("economics_threshold_signoff_prod_status"),
                    "lifecycle_retention_signoff_dev_status": payload.get("lifecycle_retention_signoff_dev_status"),
                    "lifecycle_retention_signoff_prod_status": payload.get("lifecycle_retention_signoff_prod_status"),
                    "restore_drills_180d_count": payload.get("restore_drills_180d_count"),
                    "restore_drill_last_result": payload.get("restore_drill_last_result"),
                    "go_live_signoff_items_total": payload.get("go_live_signoff_items_total"),
                    "go_live_signoff_items_approved": payload.get("go_live_signoff_items_approved"),
                    "legal_signoff_items_total": payload.get("legal_signoff_items_total"),
                    "legal_signoff_items_approved": payload.get("legal_signoff_items_approved"),
                    "legal_signoff_prod_approved_count": payload.get("legal_signoff_prod_approved_count"),
                    "legal_signoff_prod_required_count": payload.get("legal_signoff_prod_required_count"),
                    "legal_signoff_prod_ready": payload.get("legal_signoff_prod_ready"),
                    "dr_checklist_snapshots_180d_count": payload.get("dr_checklist_snapshots_180d_count"),
                    "dr_checklist_latest_completion_percent": payload.get("dr_checklist_latest_completion_percent"),
                    "queue_execute_exceptions_24h": payload.get("queue_execute_exceptions_24h"),
                    "terminal_queue_failures_24h": payload.get("terminal_queue_failures_24h"),
                }
            )
        st.caption("Recent Evidence Capture Events")
        st.dataframe(pd.DataFrame(capture_rows), use_container_width=True, hide_index=True)

    st.markdown("#### eBay Fee Calibration Sign-Off Tracker")
    st.caption(
        "Capture finance calibration acceptance for eBay fee assumptions (owner/date/evidence) per environment."
    )
    fee_coverage_rows: list[dict[str, Any]] = []
    for target_env in ["dev", "prod"]:
        latest = latest_fee_calibration_by_env.get(target_env) or {}
        fee_coverage_rows.append(
            {
                "environment": target_env,
                "status": str(latest.get("status") or "missing"),
                "owner": str(latest.get("owner") or ""),
                "signoff_date": str(latest.get("signoff_date") or ""),
                "sample_order_count": int(latest.get("sample_order_count") or 0),
                "assumption_snapshot": str(latest.get("assumption_snapshot") or ""),
                "evidence_link": str(latest.get("evidence_link") or ""),
            }
        )
    fee_coverage_df = pd.DataFrame(fee_coverage_rows)
    st.dataframe(fee_coverage_df, use_container_width=True, hide_index=True)
    fes1, fes2 = st.columns([2, 1])
    with fes1:
        seed_fee_target = st.selectbox(
            "Seed Missing Fee Sign-Off Rows For",
            options=["prod", "dev", "dev+prod"],
            index=0,
            key="admin_ebay_fee_calibration_signoff_seed_target",
        )
    with fes2:
        if st.button("Seed Missing Fee Sign-Off Items", key="admin_ebay_fee_calibration_signoff_seed_btn"):
            target_envs = ["dev", "prod"] if seed_fee_target == "dev+prod" else [seed_fee_target]
            seeded_count = 0
            try:
                for target_env in target_envs:
                    if target_env in latest_fee_calibration_by_env:
                        continue
                    repo.record_audit_event(
                        entity_type="ebay_fee_calibration_signoff",
                        entity_id=None,
                        action="seed_missing",
                        actor=user.username,
                        changes={
                            "target_env": str(target_env or "").strip().lower(),
                            "signoff_date": str(utcnow_naive().date().isoformat()),
                            "owner": str(user.username or "").strip(),
                            "status": "needs_followup",
                            "sample_order_count": 0,
                            "assumption_snapshot": (
                                f"final_value_rate_percent={get_runtime_float(repo, 'ebay_fee_estimate_final_value_rate_percent', 13.25):.4f}, "
                                f"payment_rate_percent={get_runtime_float(repo, 'ebay_fee_estimate_payment_processing_rate_percent', 2.9):.4f}, "
                                f"per_order_fixed_usd={get_runtime_float(repo, 'ebay_fee_estimate_fixed_fee_per_order_usd', 0.30):.4f}"
                            ),
                            "evidence_link": "",
                            "notes": "Auto-seeded missing fee calibration sign-off row from Admin tracker.",
                            "seeded": True,
                        },
                    )
                    seeded_count += 1
                if seeded_count <= 0:
                    st.info("No missing fee calibration sign-off rows to seed for the selected target.")
                else:
                    st.success(f"Seeded {seeded_count} missing fee calibration sign-off row(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to seed fee calibration sign-off rows: {exc}")
    feq1, feq2 = st.columns(2)
    with feq1:
        quick_fee_env = st.selectbox(
            "Quick Approve Env",
            options=["prod", "dev"],
            index=0,
            key="admin_ebay_fee_calibration_signoff_quick_env",
        )
    with feq2:
        if st.button("Quick Mark Approved", key="admin_ebay_fee_calibration_signoff_quick_approve_btn"):
            try:
                repo.record_audit_event(
                    entity_type="ebay_fee_calibration_signoff",
                    entity_id=None,
                    action="quick_approve",
                    actor=user.username,
                    changes={
                        "target_env": str(quick_fee_env or "").strip().lower(),
                        "signoff_date": str(utcnow_naive().date().isoformat()),
                        "owner": str(user.username or "").strip(),
                        "status": "approved",
                        "sample_order_count": 0,
                        "assumption_snapshot": (
                            f"final_value_rate_percent={get_runtime_float(repo, 'ebay_fee_estimate_final_value_rate_percent', 13.25):.4f}, "
                            f"payment_rate_percent={get_runtime_float(repo, 'ebay_fee_estimate_payment_processing_rate_percent', 2.9):.4f}, "
                            f"per_order_fixed_usd={get_runtime_float(repo, 'ebay_fee_estimate_fixed_fee_per_order_usd', 0.30):.4f}"
                        ),
                        "evidence_link": "",
                        "notes": "Quick approved from fee calibration tracker.",
                        "quick_action": True,
                    },
                )
                st.success("Fee calibration sign-off quick-approved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to quick-approve fee calibration sign-off: {exc}")
    with st.form("admin_ebay_fee_calibration_signoff_form"):
        ef1, ef2 = st.columns(2)
        with ef1:
            fee_signoff_target_env = st.selectbox(
                "Environment",
                options=["dev", "prod"],
                index=1,
                key="admin_ebay_fee_calibration_signoff_target_env",
            )
            fee_signoff_date = st.date_input(
                "Sign-Off Date",
                value=utcnow_naive().date(),
                key="admin_ebay_fee_calibration_signoff_date",
            )
            fee_signoff_owner = st.text_input(
                "Owner",
                value=str(user.username or ""),
                key="admin_ebay_fee_calibration_signoff_owner",
            )
        with ef2:
            fee_signoff_status = st.selectbox(
                "Status",
                options=["approved", "blocked", "needs_followup"],
                index=0,
                key="admin_ebay_fee_calibration_signoff_status",
            )
            fee_signoff_sample_order_count = st.number_input(
                "Sample Order Count",
                min_value=0,
                max_value=10000,
                value=5,
                step=1,
                key="admin_ebay_fee_calibration_signoff_sample_order_count",
                help="How many production orders were used for calibration evidence.",
            )
            fee_signoff_evidence_link = st.text_input(
                "Evidence Link",
                placeholder="report/export/ticket URL",
                key="admin_ebay_fee_calibration_signoff_evidence_link",
            )
        fee_signoff_assumption_snapshot = st.text_input(
            "Assumption Snapshot",
            value=(
                f"final_value_rate_percent={get_runtime_float(repo, 'ebay_fee_estimate_final_value_rate_percent', 13.25):.4f}, "
                f"payment_rate_percent={get_runtime_float(repo, 'ebay_fee_estimate_payment_processing_rate_percent', 2.9):.4f}, "
                f"per_order_fixed_usd={get_runtime_float(repo, 'ebay_fee_estimate_fixed_fee_per_order_usd', 0.30):.4f}"
            ),
            key="admin_ebay_fee_calibration_signoff_assumption_snapshot",
        )
        fee_signoff_notes = st.text_area(
            "Notes",
            placeholder="Calibration rationale, confidence, and any residual risk.",
            key="admin_ebay_fee_calibration_signoff_notes",
        )
        save_fee_signoff = st.form_submit_button("Record Fee Calibration Sign-Off")
    if save_fee_signoff:
        try:
            repo.record_audit_event(
                entity_type="ebay_fee_calibration_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "target_env": str(fee_signoff_target_env or "").strip().lower(),
                    "signoff_date": str(fee_signoff_date.isoformat()),
                    "owner": str(fee_signoff_owner or "").strip(),
                    "status": str(fee_signoff_status or "").strip().lower(),
                    "sample_order_count": int(fee_signoff_sample_order_count or 0),
                    "assumption_snapshot": str(fee_signoff_assumption_snapshot or "").strip(),
                    "evidence_link": str(fee_signoff_evidence_link or "").strip(),
                    "notes": str(fee_signoff_notes or "").strip(),
                },
            )
            st.success("Fee calibration sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record fee calibration sign-off: {exc}")

    if not fee_calibration_df.empty:
        st.dataframe(fee_calibration_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Fee Calibration Sign-Off CSV",
            data=fee_calibration_df.to_csv(index=False).encode("utf-8"),
            file_name=f"ebay_fee_calibration_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_ebay_fee_calibration_signoff_download_csv_btn",
        )
    else:
        st.caption("No fee calibration sign-off records yet.")

    st.markdown("#### Economics Threshold Sign-Off Tracker")
    st.caption(
        "Capture finance/operator acceptance of Economics Intelligence alert thresholds and assumptions per environment."
    )
    economics_coverage_rows: list[dict[str, Any]] = []
    for target_env in ["dev", "prod"]:
        latest = latest_economics_threshold_by_env.get(target_env) or {}
        economics_coverage_rows.append(
            {
                "environment": target_env,
                "status": str(latest.get("status") or "missing"),
                "owner": str(latest.get("owner") or ""),
                "signoff_date": str(latest.get("signoff_date") or ""),
                "min_actual_margin_alert_pct": float(latest.get("min_actual_margin_alert_pct") or 0.0),
                "max_avg_fee_variance_alert_usd": float(latest.get("max_avg_fee_variance_alert_usd") or 0.0),
                "min_group_sales_for_alert": int(latest.get("min_group_sales_for_alert") or 0),
                "assumption_snapshot": str(latest.get("assumption_snapshot") or ""),
                "evidence_link": str(latest.get("evidence_link") or ""),
            }
        )
    economics_coverage_df = pd.DataFrame(economics_coverage_rows)
    st.dataframe(economics_coverage_df, use_container_width=True, hide_index=True)
    ets1, ets2 = st.columns([2, 1])
    with ets1:
        economics_seed_target = st.selectbox(
            "Seed Missing Economics Threshold Rows For",
            options=["prod", "dev", "dev+prod"],
            index=0,
            key="admin_economics_threshold_signoff_seed_target",
        )
    with ets2:
        if st.button("Seed Missing Economics Threshold Sign-Off Items", key="admin_economics_threshold_signoff_seed_btn"):
            target_envs = ["dev", "prod"] if economics_seed_target == "dev+prod" else [economics_seed_target]
            seeded_count = 0
            try:
                for target_env in target_envs:
                    if target_env in latest_economics_threshold_by_env:
                        continue
                    repo.record_audit_event(
                        entity_type="economics_threshold_signoff",
                        entity_id=None,
                        action="seed_missing",
                        actor=user.username,
                        changes={
                            "target_env": str(target_env or "").strip().lower(),
                            "signoff_date": str(utcnow_naive().date().isoformat()),
                            "owner": str(user.username or "").strip(),
                            "status": "needs_followup",
                            "min_actual_margin_alert_pct": 5.0,
                            "max_avg_fee_variance_alert_usd": 3.0,
                            "min_group_sales_for_alert": 3,
                            "assumption_snapshot": "defaults=min_margin_pct:5.0,max_avg_fee_var_usd:3.0,min_group_sales:3",
                            "evidence_link": "",
                            "notes": "Auto-seeded missing economics threshold sign-off row from Admin tracker.",
                            "seeded": True,
                        },
                    )
                    seeded_count += 1
                if seeded_count <= 0:
                    st.info("No missing economics threshold sign-off rows to seed for the selected target.")
                else:
                    st.success(f"Seeded {seeded_count} missing economics threshold sign-off row(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to seed economics threshold sign-off rows: {exc}")
    etq1, etq2 = st.columns(2)
    with etq1:
        quick_economics_env = st.selectbox(
            "Quick Approve Economics Env",
            options=["prod", "dev"],
            index=0,
            key="admin_economics_threshold_signoff_quick_env",
        )
    with etq2:
        if st.button("Quick Mark Economics Threshold Approved", key="admin_economics_threshold_signoff_quick_approve_btn"):
            try:
                repo.record_audit_event(
                    entity_type="economics_threshold_signoff",
                    entity_id=None,
                    action="quick_approve",
                    actor=user.username,
                    changes={
                        "target_env": str(quick_economics_env or "").strip().lower(),
                        "signoff_date": str(utcnow_naive().date().isoformat()),
                        "owner": str(user.username or "").strip(),
                        "status": "approved",
                        "min_actual_margin_alert_pct": 5.0,
                        "max_avg_fee_variance_alert_usd": 3.0,
                        "min_group_sales_for_alert": 3,
                        "assumption_snapshot": "defaults=min_margin_pct:5.0,max_avg_fee_var_usd:3.0,min_group_sales:3",
                        "evidence_link": "",
                        "notes": "Quick approved from economics threshold sign-off tracker.",
                        "quick_action": True,
                    },
                )
                st.success("Economics threshold sign-off quick-approved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to quick-approve economics threshold sign-off: {exc}")
    with st.form("admin_economics_threshold_signoff_form"):
        etf1, etf2 = st.columns(2)
        with etf1:
            economics_signoff_target_env = st.selectbox(
                "Environment",
                options=["dev", "prod"],
                index=1,
                key="admin_economics_threshold_signoff_target_env",
            )
            economics_signoff_date = st.date_input(
                "Sign-Off Date",
                value=utcnow_naive().date(),
                key="admin_economics_threshold_signoff_date",
            )
            economics_signoff_owner = st.text_input(
                "Owner",
                value=str(user.username or ""),
                key="admin_economics_threshold_signoff_owner",
            )
            economics_signoff_status = st.selectbox(
                "Status",
                options=["approved", "blocked", "needs_followup"],
                index=0,
                key="admin_economics_threshold_signoff_status",
            )
            economics_signoff_evidence_link = st.text_input(
                "Evidence Link",
                placeholder="report/export/ticket URL",
                key="admin_economics_threshold_signoff_evidence_link",
            )
        with etf2:
            economics_signoff_min_margin_alert_pct = st.number_input(
                "Min Actual Margin % Alert",
                min_value=-100.0,
                max_value=100.0,
                value=5.0,
                step=0.5,
                key="admin_economics_threshold_signoff_min_margin_alert_pct",
            )
            economics_signoff_max_fee_variance_alert_usd = st.number_input(
                "Max Avg Fee Variance Alert ($)",
                min_value=0.0,
                value=3.0,
                step=0.25,
                key="admin_economics_threshold_signoff_max_fee_variance_alert_usd",
            )
            economics_signoff_min_group_sales_for_alert = st.number_input(
                "Min Sales per Group for Alert",
                min_value=1,
                max_value=10000,
                value=3,
                step=1,
                key="admin_economics_threshold_signoff_min_group_sales_for_alert",
            )
        economics_signoff_assumption_snapshot = st.text_input(
            "Assumption Snapshot",
            value=(
                f"min_margin_pct={float(economics_signoff_min_margin_alert_pct):.2f},"
                f"max_avg_fee_var_usd={float(economics_signoff_max_fee_variance_alert_usd):.2f},"
                f"min_group_sales={int(economics_signoff_min_group_sales_for_alert)}"
            ),
            key="admin_economics_threshold_signoff_assumption_snapshot",
        )
        economics_signoff_notes = st.text_area(
            "Notes",
            placeholder="Threshold rationale, confidence, and any residual risk.",
            key="admin_economics_threshold_signoff_notes",
        )
        save_economics_signoff = st.form_submit_button("Record Economics Threshold Sign-Off")
    if save_economics_signoff:
        try:
            repo.record_audit_event(
                entity_type="economics_threshold_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "target_env": str(economics_signoff_target_env or "").strip().lower(),
                    "signoff_date": str(economics_signoff_date.isoformat()),
                    "owner": str(economics_signoff_owner or "").strip(),
                    "status": str(economics_signoff_status or "").strip().lower(),
                    "min_actual_margin_alert_pct": float(economics_signoff_min_margin_alert_pct or 0.0),
                    "max_avg_fee_variance_alert_usd": float(economics_signoff_max_fee_variance_alert_usd or 0.0),
                    "min_group_sales_for_alert": int(economics_signoff_min_group_sales_for_alert or 0),
                    "assumption_snapshot": str(economics_signoff_assumption_snapshot or "").strip(),
                    "evidence_link": str(economics_signoff_evidence_link or "").strip(),
                    "notes": str(economics_signoff_notes or "").strip(),
                },
            )
            st.success("Economics threshold sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record economics threshold sign-off: {exc}")
    if not economics_threshold_df.empty:
        st.dataframe(economics_threshold_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Economics Threshold Sign-Off CSV",
            data=economics_threshold_df.to_csv(index=False).encode("utf-8"),
            file_name=f"economics_threshold_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_economics_threshold_signoff_download_csv_btn",
        )
    else:
        st.caption("No economics threshold sign-off records yet.")

    st.markdown("#### Lifecycle Retention Policy Sign-Off Tracker")
    st.caption(
        "Capture retention-policy acceptance per environment (owner/date/evidence) using current cleanup scheduler and retain-day settings."
    )
    lifecycle_coverage_rows: list[dict[str, Any]] = []
    for target_env in ["dev", "prod"]:
        latest = latest_lifecycle_retention_signoff_by_env.get(target_env) or {}
        lifecycle_coverage_rows.append(
            {
                "environment": target_env,
                "status": str(latest.get("status") or "missing"),
                "owner": str(latest.get("owner") or ""),
                "signoff_date": str(latest.get("signoff_date") or ""),
                "cleanup_enabled": bool(latest.get("cleanup_enabled")),
                "cleanup_timezone": str(latest.get("cleanup_timezone") or ""),
                "cleanup_local_time": str(latest.get("cleanup_local_time") or ""),
                "retain_days_media": int(latest.get("retain_days_media") or 0),
                "retain_days_listing": int(latest.get("retain_days_listing") or 0),
                "retain_days_lot": int(latest.get("retain_days_lot") or 0),
                "retain_days_product": int(latest.get("retain_days_product") or 0),
                "evidence_link": str(latest.get("evidence_link") or ""),
            }
        )
    st.dataframe(pd.DataFrame(lifecycle_coverage_rows), use_container_width=True, hide_index=True)
    lcs1, lcs2 = st.columns([2, 1])
    with lcs1:
        lifecycle_seed_target = st.selectbox(
            "Seed Missing Lifecycle Policy Rows For",
            options=["prod", "dev", "dev+prod"],
            index=0,
            key="admin_lifecycle_retention_signoff_seed_target",
        )
    with lcs2:
        if st.button("Seed Missing Lifecycle Policy Sign-Off Items", key="admin_lifecycle_retention_signoff_seed_btn"):
            target_envs = ["dev", "prod"] if lifecycle_seed_target == "dev+prod" else [lifecycle_seed_target]
            seeded_count = 0
            try:
                for target_env in target_envs:
                    if target_env in latest_lifecycle_retention_signoff_by_env:
                        continue
                    repo.record_audit_event(
                        entity_type="lifecycle_retention_policy_signoff",
                        entity_id=None,
                        action="seed_missing",
                        actor=user.username,
                        changes={
                            "target_env": str(target_env or "").strip().lower(),
                            "signoff_date": str(utcnow_naive().date().isoformat()),
                            "owner": str(user.username or "").strip(),
                            "status": "needs_followup",
                            "cleanup_enabled": bool(get_runtime_bool(repo, "lifecycle_archive_cleanup_enabled", False)),
                            "cleanup_timezone": str(
                                get_runtime_value(
                                    repo,
                                    "lifecycle_archive_cleanup_timezone",
                                    str(get_runtime_value(repo, "app_default_timezone", "America/Denver")),
                                )
                            ),
                            "cleanup_local_time": str(
                                get_runtime_value(repo, "lifecycle_archive_cleanup_local_time", "03:45")
                            ),
                            "retain_days_media": int(
                                get_runtime_int(repo, "lifecycle_media_archive_retain_days", 180)
                            ),
                            "retain_days_listing": int(
                                get_runtime_int(repo, "lifecycle_listing_archive_retain_days", 365)
                            ),
                            "retain_days_lot": int(get_runtime_int(repo, "lifecycle_lot_archive_retain_days", 365)),
                            "retain_days_product": int(
                                get_runtime_int(repo, "lifecycle_product_archive_retain_days", 365)
                            ),
                            "evidence_link": "",
                            "notes": "Auto-seeded missing lifecycle retention policy sign-off row from Admin tracker.",
                            "seeded": True,
                        },
                    )
                    seeded_count += 1
                if seeded_count <= 0:
                    st.info("No missing lifecycle retention policy sign-off rows to seed for the selected target.")
                else:
                    st.success(f"Seeded {seeded_count} missing lifecycle retention policy sign-off row(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to seed lifecycle retention policy sign-off rows: {exc}")
    lcq1, lcq2 = st.columns(2)
    with lcq1:
        quick_lifecycle_env = st.selectbox(
            "Quick Approve Lifecycle Env",
            options=["prod", "dev"],
            index=0,
            key="admin_lifecycle_retention_signoff_quick_env",
        )
    with lcq2:
        if st.button("Quick Mark Lifecycle Policy Approved", key="admin_lifecycle_retention_signoff_quick_approve_btn"):
            try:
                repo.record_audit_event(
                    entity_type="lifecycle_retention_policy_signoff",
                    entity_id=None,
                    action="quick_approve",
                    actor=user.username,
                    changes={
                        "target_env": str(quick_lifecycle_env or "").strip().lower(),
                        "signoff_date": str(utcnow_naive().date().isoformat()),
                        "owner": str(user.username or "").strip(),
                        "status": "approved",
                        "cleanup_enabled": bool(get_runtime_bool(repo, "lifecycle_archive_cleanup_enabled", False)),
                        "cleanup_timezone": str(
                            get_runtime_value(
                                repo,
                                "lifecycle_archive_cleanup_timezone",
                                str(get_runtime_value(repo, "app_default_timezone", "America/Denver")),
                            )
                        ),
                        "cleanup_local_time": str(get_runtime_value(repo, "lifecycle_archive_cleanup_local_time", "03:45")),
                        "retain_days_media": int(get_runtime_int(repo, "lifecycle_media_archive_retain_days", 180)),
                        "retain_days_listing": int(get_runtime_int(repo, "lifecycle_listing_archive_retain_days", 365)),
                        "retain_days_lot": int(get_runtime_int(repo, "lifecycle_lot_archive_retain_days", 365)),
                        "retain_days_product": int(get_runtime_int(repo, "lifecycle_product_archive_retain_days", 365)),
                        "evidence_link": "",
                        "notes": "Quick approved from lifecycle retention policy sign-off tracker.",
                        "quick_action": True,
                    },
                )
                st.success("Lifecycle retention policy sign-off quick-approved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to quick-approve lifecycle retention policy sign-off: {exc}")
    with st.form("admin_lifecycle_retention_signoff_form"):
        lcf1, lcf2 = st.columns(2)
        with lcf1:
            lifecycle_signoff_target_env = st.selectbox(
                "Environment",
                options=["dev", "prod"],
                index=1,
                key="admin_lifecycle_retention_signoff_target_env",
            )
            lifecycle_signoff_date = st.date_input(
                "Sign-Off Date",
                value=utcnow_naive().date(),
                key="admin_lifecycle_retention_signoff_date",
            )
            lifecycle_signoff_owner = st.text_input(
                "Owner",
                value=str(user.username or ""),
                key="admin_lifecycle_retention_signoff_owner",
            )
            lifecycle_signoff_status = st.selectbox(
                "Status",
                options=["approved", "blocked", "needs_followup"],
                index=0,
                key="admin_lifecycle_retention_signoff_status",
            )
            lifecycle_signoff_evidence_link = st.text_input(
                "Evidence Link",
                placeholder="policy/runbook/export URL",
                key="admin_lifecycle_retention_signoff_evidence_link",
            )
        with lcf2:
            lifecycle_signoff_cleanup_enabled = st.checkbox(
                "Cleanup Enabled",
                value=bool(get_runtime_bool(repo, "lifecycle_archive_cleanup_enabled", False)),
                key="admin_lifecycle_retention_signoff_cleanup_enabled",
            )
            lifecycle_signoff_cleanup_timezone = st.text_input(
                "Cleanup Timezone",
                value=str(
                    get_runtime_value(
                        repo,
                        "lifecycle_archive_cleanup_timezone",
                        str(get_runtime_value(repo, "app_default_timezone", "America/Denver")),
                    )
                ),
                key="admin_lifecycle_retention_signoff_cleanup_timezone",
            )
            lifecycle_signoff_cleanup_local_time = st.text_input(
                "Cleanup Local Time (HH:MM)",
                value=str(get_runtime_value(repo, "lifecycle_archive_cleanup_local_time", "03:45")),
                key="admin_lifecycle_retention_signoff_cleanup_local_time",
            )
            lifecycle_signoff_retain_days_media = st.number_input(
                "Retain Days Media",
                min_value=1,
                max_value=3650,
                value=max(1, min(3650, int(get_runtime_int(repo, "lifecycle_media_archive_retain_days", 180)))),
                step=1,
                key="admin_lifecycle_retention_signoff_retain_days_media",
            )
            lifecycle_signoff_retain_days_listing = st.number_input(
                "Retain Days Listings",
                min_value=1,
                max_value=3650,
                value=max(1, min(3650, int(get_runtime_int(repo, "lifecycle_listing_archive_retain_days", 365)))),
                step=1,
                key="admin_lifecycle_retention_signoff_retain_days_listing",
            )
            lifecycle_signoff_retain_days_lot = st.number_input(
                "Retain Days Lots",
                min_value=1,
                max_value=3650,
                value=max(1, min(3650, int(get_runtime_int(repo, "lifecycle_lot_archive_retain_days", 365)))),
                step=1,
                key="admin_lifecycle_retention_signoff_retain_days_lot",
            )
            lifecycle_signoff_retain_days_product = st.number_input(
                "Retain Days Products",
                min_value=1,
                max_value=3650,
                value=max(1, min(3650, int(get_runtime_int(repo, "lifecycle_product_archive_retain_days", 365)))),
                step=1,
                key="admin_lifecycle_retention_signoff_retain_days_product",
            )
        lifecycle_signoff_notes = st.text_area(
            "Notes",
            placeholder="Policy rationale, risk acceptance, and follow-up items.",
            key="admin_lifecycle_retention_signoff_notes",
        )
        save_lifecycle_signoff = st.form_submit_button("Record Lifecycle Retention Policy Sign-Off")
    if save_lifecycle_signoff:
        try:
            repo.record_audit_event(
                entity_type="lifecycle_retention_policy_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "target_env": str(lifecycle_signoff_target_env or "").strip().lower(),
                    "signoff_date": str(lifecycle_signoff_date.isoformat()),
                    "owner": str(lifecycle_signoff_owner or "").strip(),
                    "status": str(lifecycle_signoff_status or "").strip().lower(),
                    "cleanup_enabled": bool(lifecycle_signoff_cleanup_enabled),
                    "cleanup_timezone": str(lifecycle_signoff_cleanup_timezone or "").strip(),
                    "cleanup_local_time": str(lifecycle_signoff_cleanup_local_time or "").strip(),
                    "retain_days_media": int(lifecycle_signoff_retain_days_media or 0),
                    "retain_days_listing": int(lifecycle_signoff_retain_days_listing or 0),
                    "retain_days_lot": int(lifecycle_signoff_retain_days_lot or 0),
                    "retain_days_product": int(lifecycle_signoff_retain_days_product or 0),
                    "evidence_link": str(lifecycle_signoff_evidence_link or "").strip(),
                    "notes": str(lifecycle_signoff_notes or "").strip(),
                },
            )
            st.success("Lifecycle retention policy sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record lifecycle retention policy sign-off: {exc}")
    if not lifecycle_retention_signoff_df.empty:
        st.dataframe(lifecycle_retention_signoff_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Lifecycle Retention Policy Sign-Off CSV",
            data=lifecycle_retention_signoff_df.to_csv(index=False).encode("utf-8"),
            file_name=f"lifecycle_retention_policy_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_lifecycle_retention_signoff_download_csv_btn",
        )
    else:
        st.caption("No lifecycle retention policy sign-off records yet.")

    st.markdown("#### Go-Live Section Sign-Off Tracker")
    st.caption(
        "Track owner/date/evidence completion per checklist item. Latest status per item is included in the evidence pack."
    )
    checklist_item_options: list[tuple[str, str, str]] = []
    for _state, raw_label in checklist_match_rows:
        label = str(raw_label or "").strip()
        if not label:
            continue
        section_key = "general"
        item_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:120] or "item"
        if ")" in label and label.split(")", 1)[0].strip().isdigit():
            section_key = f"section_{label.split(')', 1)[0].strip()}"
        checklist_item_options.append((label, section_key, item_key))
    option_map = {f"{label} [{section_key}:{item_key}]": (section_key, item_key, label) for label, section_key, item_key in checklist_item_options}
    default_option = next(iter(option_map.keys()), "manual [general:manual_item]")

    with st.form("admin_go_live_section_signoff_form"):
        gsf1, gsf2 = st.columns(2)
        with gsf1:
            selected_item = st.selectbox(
                "Checklist Item",
                options=list(option_map.keys()) if option_map else [default_option],
                index=0,
            )
            default_section_key, default_item_key, default_label = option_map.get(
                selected_item, ("general", "manual_item", "manual")
            )
            signoff_section_key = st.text_input("Section Key", value=default_section_key)
            signoff_item_key = st.text_input("Item Key", value=default_item_key)
            signoff_label = st.text_input("Item Label", value=default_label)
        with gsf2:
            signoff_status = st.selectbox("Status", options=["approved", "blocked", "needs_followup"], index=0)
            signoff_owner = st.text_input("Owner", value=str(user.username or ""))
            signoff_date = st.date_input("Sign-Off Date", value=utcnow_naive().date())
            signoff_evidence_link = st.text_input("Evidence Link", placeholder="ticket/runbook/artifact URL")
        signoff_notes = st.text_area("Notes", placeholder="Any follow-up or context.")
        save_section_signoff = st.form_submit_button("Record Checklist Item Sign-Off")
    if save_section_signoff:
        try:
            repo.record_audit_event(
                entity_type="go_live_section_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "section_key": str(signoff_section_key or "").strip().lower(),
                    "item_key": str(signoff_item_key or "").strip().lower(),
                    "item_label": str(signoff_label or "").strip(),
                    "status": str(signoff_status or "").strip().lower(),
                    "owner": str(signoff_owner or "").strip(),
                    "signoff_date": str(signoff_date.isoformat()),
                    "evidence_link": str(signoff_evidence_link or "").strip(),
                    "notes": str(signoff_notes or "").strip(),
                },
            )
            st.success("Checklist item sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record checklist item sign-off: {exc}")

    if not go_live_signoff_df.empty:
        st.dataframe(go_live_signoff_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Go-Live Sign-Off CSV",
            data=go_live_signoff_df.to_csv(index=False).encode("utf-8"),
            file_name=f"go_live_section_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_go_live_section_signoff_download_csv_btn",
        )
    else:
        st.caption("No checklist item sign-offs recorded yet.")

    st.markdown("#### Commerce Legal Sign-Off Tracker")
    st.caption(
        "Capture policy-level legal/compliance approvals (owner/date/evidence) for tax, retention, and marketplace readiness."
    )
    legal_coverage_rows: list[dict[str, Any]] = []
    for target_env in ["dev", "prod"]:
        for policy_key, policy_label in legal_policy_catalog:
            latest = latest_legal_signoff_by_key.get(f"{target_env}::{policy_key}") or {}
            legal_coverage_rows.append(
                {
                    "environment": target_env,
                    "policy_key": policy_key,
                    "policy_label": policy_label,
                    "status": str(latest.get("status") or "missing"),
                    "owner": str(latest.get("owner") or ""),
                    "signoff_date": str(latest.get("signoff_date") or ""),
                    "evidence_link": str(latest.get("evidence_link") or ""),
                }
            )
    legal_coverage_df = pd.DataFrame(legal_coverage_rows)
    st.dataframe(legal_coverage_df, use_container_width=True, hide_index=True)
    ls1, ls2 = st.columns([2, 1])
    with ls1:
        seed_target = st.selectbox(
            "Seed Missing Policy Rows For",
            options=["prod", "dev", "dev+prod"],
            index=0,
            key="admin_commerce_legal_signoff_seed_target",
        )
    with ls2:
        if st.button("Seed Missing Legal Sign-Off Items", key="admin_commerce_legal_signoff_seed_btn"):
            target_envs = ["dev", "prod"] if seed_target == "dev+prod" else [seed_target]
            seeded_count = 0
            try:
                for target_env in target_envs:
                    for policy_key, policy_label in legal_policy_catalog:
                        composite_key = f"{target_env}::{policy_key}"
                        if composite_key in latest_legal_signoff_by_key:
                            continue
                        repo.record_audit_event(
                            entity_type="commerce_legal_signoff",
                            entity_id=None,
                            action="seed_missing",
                            actor=user.username,
                            changes={
                                "target_env": target_env,
                                "policy_key": str(policy_key or "").strip().lower(),
                                "policy_label": str(policy_label or "").strip(),
                                "status": "needs_followup",
                                "owner": str(user.username or "").strip(),
                                "signoff_date": str(utcnow_naive().date().isoformat()),
                                "evidence_link": "",
                                "notes": "Auto-seeded missing legal sign-off item from Admin tracker.",
                                "seeded": True,
                            },
                        )
                        seeded_count += 1
                if seeded_count <= 0:
                    st.info("No missing legal sign-off items to seed for the selected target.")
                else:
                    st.success(f"Seeded {seeded_count} missing legal sign-off item(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to seed legal sign-off items: {exc}")
    qa1, qa2, qa3 = st.columns(3)
    with qa1:
        quick_env = st.selectbox(
            "Quick Approve Env",
            options=["prod", "dev"],
            index=0,
            key="admin_commerce_legal_signoff_quick_env",
        )
    with qa2:
        quick_policy = st.selectbox(
            "Quick Approve Policy",
            options=[f"{label} [{key}]" for key, label in legal_policy_catalog],
            index=0,
            key="admin_commerce_legal_signoff_quick_policy",
        )
        quick_policy_key = str(quick_policy.rsplit("[", 1)[-1].rstrip("]")).strip().lower()
        quick_policy_label = next(
            (label for key, label in legal_policy_catalog if key == quick_policy_key),
            quick_policy,
        )
    with qa3:
        if st.button("Quick Mark Approved", key="admin_commerce_legal_signoff_quick_approve_btn"):
            try:
                repo.record_audit_event(
                    entity_type="commerce_legal_signoff",
                    entity_id=None,
                    action="quick_approve",
                    actor=user.username,
                    changes={
                        "target_env": str(quick_env or "").strip().lower(),
                        "policy_key": str(quick_policy_key or "").strip().lower(),
                        "policy_label": str(quick_policy_label or "").strip(),
                        "status": "approved",
                        "owner": str(user.username or "").strip(),
                        "signoff_date": str(utcnow_naive().date().isoformat()),
                        "evidence_link": "",
                        "notes": "Quick approved from Commerce Legal Sign-Off coverage table.",
                        "quick_action": True,
                    },
                )
                st.success("Legal sign-off quick-approved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to quick-approve legal sign-off: {exc}")

    legal_policy_options = {
        f"{label} [{key}]": (key, label)
        for key, label in legal_policy_catalog
    }
    with st.form("admin_commerce_legal_signoff_form"):
        lsf1, lsf2 = st.columns(2)
        with lsf1:
            selected_policy = st.selectbox(
                "Policy",
                options=list(legal_policy_options.keys()),
                index=0,
            )
            selected_policy_key, selected_policy_label = legal_policy_options.get(
                selected_policy,
                ("tax_treatment", "Tax treatment policy"),
            )
            legal_target_env = st.selectbox("Environment", options=["dev", "prod"], index=1)
            legal_signoff_date = st.date_input("Sign-Off Date", value=utcnow_naive().date())
            legal_owner = st.text_input("Owner", value=str(user.username or ""))
        with lsf2:
            legal_status = st.selectbox("Status", options=["approved", "blocked", "needs_followup"], index=0)
            legal_evidence_link = st.text_input("Evidence Link", placeholder="ticket/runbook/artifact URL")
            legal_policy_notes = st.text_area("Notes", placeholder="Policy assumptions, reviewer comments, open risks.")
        legal_record_submit = st.form_submit_button("Record Commerce Legal Sign-Off")
    if legal_record_submit:
        try:
            repo.record_audit_event(
                entity_type="commerce_legal_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "target_env": str(legal_target_env or "").strip().lower(),
                    "policy_key": str(selected_policy_key or "").strip().lower(),
                    "policy_label": str(selected_policy_label or "").strip(),
                    "status": str(legal_status or "").strip().lower(),
                    "owner": str(legal_owner or "").strip(),
                    "signoff_date": str(legal_signoff_date.isoformat()),
                    "evidence_link": str(legal_evidence_link or "").strip(),
                    "notes": str(legal_policy_notes or "").strip(),
                },
            )
            st.success("Commerce legal sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record commerce legal sign-off: {exc}")

    if not legal_signoff_df.empty:
        st.dataframe(legal_signoff_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Commerce Legal Sign-Off CSV",
            data=legal_signoff_df.to_csv(index=False).encode("utf-8"),
            file_name=f"commerce_legal_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_commerce_legal_signoff_download_csv_btn",
        )
    else:
        st.caption("No commerce legal sign-offs recorded yet.")

    st.markdown("#### Governance Snapshot Runner")
    st.caption(
        "Record a point-in-time governance snapshot event for audit/review cadence. "
        "This does not enqueue background jobs yet; it captures current counts and scope config."
    )
    g1, g2 = st.columns(2)
    with g1:
        if st.button(
            "Record Governance Snapshot Event",
            key="admin_governance_export_hub_record_snapshot_btn",
            use_container_width=True,
        ):
            try:
                counts = _record_governance_snapshot_event(
                    repo,
                    actor=user.username,
                    lookback_days=int(lookback_days),
                    max_rows=int(max_rows),
                    source="admin_governance_exports",
                    download_intent=False,
                )
                st.success(
                    "Governance snapshot recorded. "
                    f"handoff={counts['handoff_events']} "
                    f"feedback={counts['workspace_feedback_events']} "
                    f"parity={counts['parity_followup_events']} "
                    f"comp={counts['photo_comp_events']}"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record governance snapshot: {exc}")
    with g2:
        if st.button(
            "Record + Download Combined Bundle",
            key="admin_governance_export_hub_record_and_download_btn",
            use_container_width=True,
        ):
            try:
                counts = _record_governance_snapshot_event(
                    repo,
                    actor=user.username,
                    lookback_days=int(lookback_days),
                    max_rows=int(max_rows),
                    source="admin_governance_exports",
                    download_intent=True,
                )
                st.success(
                    "Governance snapshot recorded. "
                    f"handoff={counts['handoff_events']} "
                    f"feedback={counts['workspace_feedback_events']} "
                    f"parity={counts['parity_followup_events']} "
                    f"comp={counts['photo_comp_events']}. "
                    "Use the bundle download button above."
                )
            except Exception as exc:
                st.error(f"Unable to record governance snapshot: {exc}")

    st.markdown("#### Recent Governance Snapshots")
    load_snapshot_history = st.checkbox(
        "Load Governance Snapshot History (slower)",
        value=False,
        key="admin_governance_export_hub_load_snapshot_history",
        help="Defers governance snapshot-history query unless explicitly requested.",
    )
    if not load_snapshot_history:
        st.caption(
            "Governance snapshot history is deferred. Enable "
            "`Load Governance Snapshot History (slower)` to query it."
        )
    snapshot_logs = (
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "governance_export",
                AuditLog.action == "snapshot",
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(max_rows))
        ).all()
        if load_snapshot_history
        else []
    )
    snapshot_rows: list[dict[str, Any]] = []
    for row in snapshot_logs:
        payload = _audit_changes(row)
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        snapshot_rows.append(
            {
                "id": int(row.id),
                "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                "actor": str(row.actor or ""),
                "environment": str(payload.get("environment") or settings.app_env),
                "lookback_days": int(payload.get("lookback_days") or 0),
                "max_rows_per_scope": int(payload.get("max_rows_per_scope") or 0),
                "source": str(payload.get("source") or "unknown"),
                "scheduled": bool(payload.get("scheduled") or False),
                "handoff_events": int(counts.get("handoff_events") or 0),
                "workspace_feedback_events": int(counts.get("workspace_feedback_events") or 0),
                "parity_followup_events": int(counts.get("parity_followup_events") or 0),
                "photo_comp_events": int(counts.get("photo_comp_events") or 0),
                "download_intent": bool(payload.get("download_intent") or False),
            }
        )
    if snapshot_rows:
        snapshot_df = pd.DataFrame(snapshot_rows)
        source_options = sorted(
            {
                str(v).strip()
                for v in snapshot_df["source"].dropna().tolist()
                if str(v).strip()
            }
        )
        sf1, sf2 = st.columns(2)
        with sf1:
            selected_sources = st.multiselect(
                "Source Filter",
                options=source_options,
                default=source_options,
                key="admin_governance_snapshot_source_filter",
            )
        with sf2:
            scheduled_filter = st.selectbox(
                "Scheduled Filter",
                options=["all", "scheduled_only", "manual_only"],
                index=0,
                key="admin_governance_snapshot_scheduled_filter",
            )
        filtered_snapshot_df = snapshot_df
        if selected_sources:
            filtered_snapshot_df = filtered_snapshot_df[filtered_snapshot_df["source"].astype(str).isin(selected_sources)]
        if scheduled_filter == "scheduled_only":
            filtered_snapshot_df = filtered_snapshot_df[filtered_snapshot_df["scheduled"] == True]  # noqa: E712
        elif scheduled_filter == "manual_only":
            filtered_snapshot_df = filtered_snapshot_df[filtered_snapshot_df["scheduled"] == False]  # noqa: E712
        st.dataframe(filtered_snapshot_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Governance Snapshot History CSV",
            data=filtered_snapshot_df.to_csv(index=False).encode("utf-8"),
            file_name=(
                f"governance_snapshot_history_{settings.app_env}_"
                f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
            ),
            mime="text/csv",
            key="admin_governance_export_hub_snapshot_history_csv_btn",
        )
    else:
        st.caption("No governance snapshot events in selected lookback window.")


def render_admin(repo: InventoryRepository) -> None:
    user = current_user()
    users = repo.list_app_users(active_only=False)
    st.subheader("Admin")
    env_source_label = "Local .env" if uses_env_file(settings.app_env) else "K8s Process Env"
    env_source_color = "green" if uses_env_file(settings.app_env) else "blue"
    st.caption(f"Env Source: :{env_source_color}[{env_source_label}]")
    st.caption("Manage app users, roles, and permissions.")
    render_help_panel(
        section_title="Admin",
        goal="Control user-role assignments and permission policies in one place.",
        steps=[
            "Create or update users and assign role memberships.",
            "Set role-to-permission mappings for viewer/ops/admin workflows.",
            "Keep at least one admin role active for governance continuity.",
            "All changes are written to audit log with signed-in identity.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    st.caption(f"Signed in as `{user.username}` ({user.role}).")
    if users and not ensure_permission(user, "manage_settings", "Admin Access"):
        st.info("Admin access is required to manage users and permissions.")
        return

    st.markdown("### Config Health Summary")
    env_file_mode = uses_env_file(settings.app_env)
    env_defaults_summary = read_env_file(".env.example")
    tracked_env_keys = set(env_defaults_summary.keys())
    env_values_summary = (
        read_env_file(".env")
        if env_file_mode
        else read_process_env_values(tracked_keys=tracked_env_keys, include_untracked_editable=True)
    )
    required_env = required_env_keys()
    missing_env_required = [
        key
        for key in sorted(required_env)
        if key not in env_values_summary or not str(env_values_summary.get(key, "")).strip()
    ]
    untracked_env_keys = sorted([k for k in env_values_summary.keys() if k not in env_defaults_summary])
    env_missing_or_empty_all = [
        key
        for key in sorted(env_defaults_summary.keys())
        if key not in env_values_summary or not str(env_values_summary.get(key, "")).strip()
    ]
    env_required_total = max(1, len(required_env))
    env_required_ok = env_required_total - len(missing_env_required)

    runtime_seed_defaults_summary = _runtime_setting_seed_defaults()
    required_runtime = required_runtime_keys()
    try:
        runtime_rows_summary = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
    except Exception:
        runtime_rows_summary = []
    runtime_by_key_summary = {str(row.key): row for row in runtime_rows_summary}
    missing_runtime_required = [
        key
        for key in sorted(required_runtime)
        if key not in runtime_by_key_summary or not bool(getattr(runtime_by_key_summary[key], "is_active", False))
    ]
    runtime_missing_or_inactive_all = [
        str(item.get("key") or "")
        for item in runtime_seed_defaults_summary
        if (
            str(item.get("key") or "") not in runtime_by_key_summary
            or not bool(getattr(runtime_by_key_summary[str(item.get("key") or "")], "is_active", False))
        )
    ]
    runtime_custom_untracked_keys = sorted(
        [key for key in runtime_by_key_summary.keys() if key not in {str(item.get("key") or "") for item in runtime_seed_defaults_summary}]
    )
    runtime_required_total = max(1, len(required_runtime))
    runtime_required_ok = runtime_required_total - len(missing_runtime_required)

    h1, h2, h3, h4, h5, h6, h7, h8 = st.columns(8)
    h1.metric("Env Required OK", f"{env_required_ok}/{env_required_total}")
    h2.metric("Runtime Required OK", f"{runtime_required_ok}/{runtime_required_total}")
    h3.metric("Env Required Missing", f"{len(missing_env_required)}")
    h4.metric("Runtime Required Missing/Inactive", f"{len(missing_runtime_required)}")
    h5.metric("Env Missing/Empty (All)", f"{len(env_missing_or_empty_all)}")
    h6.metric("Runtime Missing/Inactive (All)", f"{len(runtime_missing_or_inactive_all)}")
    h7.metric("Env Untracked Keys", f"{len(untracked_env_keys)}")
    h8.metric("Runtime Untracked Keys", f"{len(runtime_custom_untracked_keys)}")

    config_health_snapshot = {
        "environment": settings.app_env,
        "generated_at_utc": utcnow_naive().isoformat(),
        "required": {
            "env": {
                "ok": env_required_ok,
                "total": env_required_total,
                "missing_or_empty": missing_env_required,
            },
            "runtime": {
                "ok": runtime_required_ok,
                "total": runtime_required_total,
                "missing_or_inactive": missing_runtime_required,
            },
        },
        "all_tracked": {
            "env_missing_or_empty_count": len(env_missing_or_empty_all),
            "env_missing_or_empty_keys": env_missing_or_empty_all[:200],
            "env_untracked_count": len(untracked_env_keys),
            "env_untracked_keys": untracked_env_keys[:200],
            "runtime_missing_or_inactive_count": len(runtime_missing_or_inactive_all),
            "runtime_missing_or_inactive_keys": runtime_missing_or_inactive_all[:200],
            "runtime_untracked_count": len(runtime_custom_untracked_keys),
            "runtime_untracked_keys": runtime_custom_untracked_keys[:200],
        },
    }
    st.download_button(
        "Download Config Health Snapshot (JSON)",
        data=json.dumps(config_health_snapshot, indent=2).encode("utf-8"),
        file_name=f"config_health_snapshot_{settings.app_env}.json",
        mime="application/json",
        key="admin_config_health_snapshot_download",
    )

    hf1, hf2 = st.columns(2)
    with hf1:
        if st.button(
            "Auto-Fix Required Env Keys",
            key="admin_top_autofix_required_env_btn",
            disabled=(len(missing_env_required) == 0 or not env_file_mode),
        ):
            try:
                fixed = _apply_required_env_defaults(
                    env_path=".env",
                    required_keys=required_env,
                    env_values=env_values_summary,
                    recommended_defaults=env_defaults_summary,
                )
                if fixed:
                    st.success(f"Auto-fixed {fixed} required env key(s).")
                else:
                    st.info("No required env keys were auto-fixed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to auto-fix required env keys: {exc}")
    if not env_file_mode:
        st.caption(
            "Env auto-fix is disabled for non-local environments. "
            "Update Kubernetes Secret/ConfigMap values and redeploy/restart workloads."
        )
    with hf2:
        if st.button(
            "Auto-Fix Required Runtime Keys",
            key="admin_top_autofix_required_runtime_btn",
            disabled=(len(missing_runtime_required) == 0),
        ):
            try:
                fixed = _apply_required_runtime_defaults(
                    repo=repo,
                    actor=user.username,
                    required_keys=required_runtime,
                    runtime_rows=runtime_rows_summary,
                    seed_defaults=runtime_seed_defaults_summary,
                )
                if fixed:
                    st.success(f"Auto-fixed {fixed} required runtime key(s).")
                else:
                    st.info("No required runtime keys were auto-fixed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to auto-fix required runtime keys: {exc}")
    bf1, bf2 = st.columns(2)
    with bf1:
        if st.button(
            "Apply Missing + Empty Env Defaults",
            key="admin_top_apply_all_env_defaults_btn",
            disabled=(len(env_missing_or_empty_all) == 0 or not env_file_mode),
        ):
            try:
                fixed = _apply_all_env_defaults(
                    env_path=".env",
                    env_values=env_values_summary,
                    recommended_defaults=env_defaults_summary,
                )
                if fixed:
                    st.success(f"Applied {fixed} env default value(s).")
                else:
                    st.info("No env defaults were applied.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to apply env defaults: {exc}")
    with bf2:
        if st.button(
            "Apply Missing + Inactive Runtime Defaults",
            key="admin_top_apply_all_runtime_defaults_btn",
            disabled=(len(runtime_missing_or_inactive_all) == 0),
        ):
            try:
                fixed = _apply_all_runtime_defaults(
                    repo=repo,
                    actor=user.username,
                    runtime_rows=runtime_rows_summary,
                    seed_defaults=runtime_seed_defaults_summary,
                )
                if fixed:
                    st.success(f"Applied {fixed} runtime default update(s).")
                else:
                    st.info("No runtime defaults were applied.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to apply runtime defaults: {exc}")
    st.caption(
        "Detailed coverage, exports, and bulk default actions are available in `Env Config` and `Runtime Settings` tabs."
    )

    (
        tab_users,
        tab_perms,
        tab_migrations,
        tab_maintenance,
        tab_backups,
        tab_ebay_verify,
        tab_ai_runtime,
        tab_env_config,
        tab_runtime_settings,
        tab_integrations,
        tab_comp_config,
        tab_saved_filters,
        tab_sync_jobs,
        tab_governance_exports,
        tab_system_health,
    ) = st.tabs(
        [
            "Users",
            "Role Permissions",
            "Migrations",
            "Maintenance",
            "Backups",
            "eBay Verify",
            "AI Runtime",
            "Env Config",
            "Runtime Settings",
            "Integrations",
            "Comp Config",
            "Saved Filters",
            "Sync Jobs",
            "Governance Exports",
            "System Health",
        ]
    )

    with tab_users:
        st.markdown("### User Directory")
        with st.expander("Auth Session Debug", expanded=False):
            snapshot = auth_debug_snapshot()
            st.caption("Use this to verify remember-token/session restore behavior across restarts.")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Session Authenticated", "yes" if bool(snapshot.get("auth_authenticated_session")) else "no")
            m2.metric("Remember Enabled", "yes" if bool(snapshot.get("auth_remember_enabled_session")) else "no")
            m3.metric("Query Token Present", "yes" if bool(snapshot.get("query_token_present")) else "no")
            m4.metric("Query Token Valid", "yes" if bool(snapshot.get("query_token_valid")) else "no")
            if not bool(snapshot.get("query_token_present")) and bool(snapshot.get("auth_remember_enabled_session")):
                st.warning(
                    "Remember is enabled but URL query token is missing. "
                    "Navigate once after sign-in or re-sign-in with Remember enabled to mint token."
                )
            st.dataframe(
                pd.DataFrame([snapshot]),
                use_container_width=True,
                hide_index=True,
            )
        if not users:
            st.warning("No app users found. Bootstrap the first admin account.")
            with st.form("bootstrap_first_admin_form"):
                b1, b2, b3 = st.columns(3)
                with b1:
                    bootstrap_username = st.text_input("Admin Username", value="admin")
                with b2:
                    bootstrap_display_name = st.text_input("Display Name", value="Administrator")
                with b3:
                    bootstrap_email = st.text_input("Email", value="")
                bp1, bp2 = st.columns(2)
                with bp1:
                    bootstrap_password = st.text_input("Admin Password", type="password")
                with bp2:
                    bootstrap_password_confirm = st.text_input("Confirm Password", type="password")
                bootstrap_submit = st.form_submit_button("Bootstrap First Admin User")
            if bootstrap_submit:
                if not bootstrap_username.strip():
                    st.error("Admin username is required.")
                elif bootstrap_password != bootstrap_password_confirm:
                    st.error("Passwords do not match.")
                else:
                    try:
                        row = repo.upsert_app_user(
                            username=bootstrap_username.strip(),
                            role="admin",
                            display_name=bootstrap_display_name.strip(),
                            email=bootstrap_email.strip(),
                            password=bootstrap_password,
                            is_active=True,
                            actor=user.username,
                        )
                        if not repo.list_role_permissions():
                            for role_name, perms in DEFAULT_PERMISSIONS.items():
                                repo.set_role_permissions(role_name, set(perms), actor=user.username)
                        st.success(f"Bootstrapped admin user `{row.username}`.")
                    except ValueError as exc:
                        st.error(str(exc))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": u.id,
                        "username": u.username,
                        "display_name": u.display_name,
                        "email": u.email,
                        "role": u.role,
                        "password_set": bool(u.password_hash),
                        "is_active": u.is_active,
                    }
                    for u in users
                ]
            ),
            use_container_width=True,
        )

        st.markdown("### Add/Update User")
        with st.form("admin_upsert_user_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                username = st.text_input("Username")
            with c2:
                role = st.selectbox("Role", sorted(DEFAULT_PERMISSIONS.keys()))
            with c3:
                is_active = st.checkbox("Active", value=True)
            d1, d2 = st.columns(2)
            with d1:
                display_name = st.text_input("Display Name")
            with d2:
                email = st.text_input("Email")
            password = st.text_input("Password (Required for new users, min 8 chars)", type="password")
            submit = st.form_submit_button("Save User")

        if submit:
            try:
                existing_usernames = {u.username for u in users}
                is_new_user = username.strip() not in existing_usernames
                if is_new_user and not password.strip():
                    st.error("Password is required when creating a new user.")
                else:
                    row = repo.upsert_app_user(
                        username=username.strip(),
                        role=role,
                        display_name=display_name.strip(),
                        email=email.strip(),
                        password=password,
                        is_active=is_active,
                        actor=user.username,
                    )
                    st.success(f"Saved user `{row.username}`.")
            except ValueError as exc:
                st.error(str(exc))

        if users:
            st.markdown("### Edit Existing User")
            user_map = {f"#{u.id} | {u.username}": u for u in users}
            selected_key = st.selectbox("Select User", list(user_map.keys()))
            selected = user_map[selected_key]
            with st.form("admin_edit_user_form"):
                e1, e2, e3 = st.columns(3)
                with e1:
                    edit_role = st.selectbox(
                        "Role",
                        sorted(DEFAULT_PERMISSIONS.keys()),
                        index=sorted(DEFAULT_PERMISSIONS.keys()).index(selected.role)
                        if selected.role in DEFAULT_PERMISSIONS
                        else 0,
                    )
                with e2:
                    edit_active = st.checkbox("Active", value=selected.is_active)
                with e3:
                    edit_display_name = st.text_input("Display Name", value=selected.display_name)
                edit_email = st.text_input("Email", value=selected.email)
                edit_password = st.text_input("Reset Password (Optional)", type="password")
                update_submit = st.form_submit_button("Update User")

            if update_submit:
                try:
                    repo.update_app_user(
                        selected.id,
                        {
                            "role": edit_role,
                            "is_active": edit_active,
                            "display_name": edit_display_name.strip(),
                            "email": edit_email.strip(),
                        },
                        actor=user.username,
                    )
                    if edit_password.strip():
                        repo.set_app_user_password(selected.id, edit_password, actor=user.username)
                    st.success("User updated.")
                except ValueError as exc:
                    st.error(str(exc))

    with tab_perms:
        st.markdown("### Role Permission Matrix")
        permission_map = repo.list_role_permissions()
        role_names = sorted(set(DEFAULT_PERMISSIONS.keys()) | set(permission_map.keys()))
        all_permissions = _all_permission_options()

        matrix_rows = []
        for role_name in role_names:
            effective = permission_map.get(role_name, DEFAULT_PERMISSIONS.get(role_name, set()))
            row = {"role": role_name}
            for perm in all_permissions:
                row[perm] = perm in effective
            matrix_rows.append(row)
        st.dataframe(pd.DataFrame(matrix_rows), use_container_width=True)

        st.markdown("### Edit Role Permissions")
        selected_role = st.selectbox("Role", role_names)
        selected_current = permission_map.get(
            selected_role,
            DEFAULT_PERMISSIONS.get(selected_role, set()),
        )
        with st.form("admin_role_permissions_form"):
            selected_permissions = st.multiselect(
                "Permissions",
                all_permissions,
                default=sorted(selected_current),
            )
            save = st.form_submit_button("Save Role Permissions")

        if save:
            repo.set_role_permissions(selected_role, set(selected_permissions), actor=user.username)
            st.success(f"Updated permissions for role `{selected_role}`.")

    with tab_migrations:
        st.markdown("### Database Migrations")
        st.caption("Inspect Alembic revision status and run targeted upgrades.")
        if not users:
            st.info("Bootstrap the first admin user before running migrations from the UI.")
        else:
            current_rev = _get_current_db_revision(repo)
            st.metric("Current DB Revision", current_rev)

            try:
                history_rows = _migration_history_rows()
            except Exception as exc:
                history_rows = []
                st.error(f"Unable to read migration history: {exc}")

            if history_rows:
                st.dataframe(pd.DataFrame(history_rows), use_container_width=True)
                target_options = ["head"] + [row["revision"] for row in history_rows]
            else:
                target_options = ["head"]

            with st.form("admin_migration_upgrade_form"):
                target_revision = st.selectbox(
                    "Upgrade Target Revision",
                    options=target_options,
                    help="Choose `head` for latest, or choose a specific revision ID.",
                )
                confirm_upgrade = st.checkbox("I understand this changes DB schema.")
                run_upgrade = st.form_submit_button("Run Upgrade")

            if run_upgrade:
                if not confirm_upgrade:
                    st.error("Confirm schema-change acknowledgement first.")
                else:
                    try:
                        migrate_upgrade(target_revision)
                        st.success(f"Migration upgrade completed to `{target_revision}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Migration failed: {exc}")

            st.markdown("### Rollback / Downgrade")
            if settings.app_env.lower() == "prod":
                st.warning("Rollback is disabled from UI in `APP_ENV=prod`. Use controlled ops runbook.")
            else:
                with st.form("admin_migration_downgrade_form"):
                    downgrade_target = st.selectbox(
                        "Downgrade Target Revision",
                        options=["-1", "base"] + [row["revision"] for row in history_rows],
                        help="`-1` rolls back one step. `base` rolls back all migrations.",
                    )
                    confirm_downgrade = st.checkbox(
                        "I understand rollback can break app behavior and may require data recovery."
                    )
                    run_downgrade = st.form_submit_button("Run Downgrade")

                if run_downgrade:
                    if not confirm_downgrade:
                        st.error("Confirm rollback acknowledgement first.")
                    else:
                        try:
                            migrate_downgrade(downgrade_target)
                            st.success(f"Migration downgrade completed to `{downgrade_target}`.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Downgrade failed: {exc}")

    with tab_maintenance:
        st.markdown("### Data Seeding")
        st.caption("Seed development fixtures for local/dev environments.")
        if settings.app_env.lower() == "prod":
            st.warning("Seeding is disabled in `APP_ENV=prod`.")
        else:
            with st.form("admin_seed_form"):
                seed_mode = st.radio(
                    "Seed Mode",
                    options=["append_only", "wipe_seed_tables_then_seed", "wipe_operational_then_seed"],
                    format_func=_seed_mode_label,
                )
                confirm_seed = st.checkbox("I understand this modifies data.")
                run_seed = st.form_submit_button("Run Seed")

            if run_seed:
                if not confirm_seed:
                    st.error("Confirm seed acknowledgement first.")
                else:
                    try:
                        if seed_mode == "wipe_operational_then_seed":
                            _wipe_operational_data(
                                repo,
                                include_shipping_presets=False,
                                include_document_templates=False,
                                include_audit_logs=False,
                            )
                        counts = seed_dev_data(wipe=(seed_mode == "wipe_seed_tables_then_seed"))
                        st.success(
                            "Seed complete: "
                            f"lots={counts['lots']}, products={counts['products']}, assignments={counts['assignments']}, "
                            f"listings={counts['listings']}, sales={counts['sales']}, media={counts['media']}"
                        )
                    except Exception as exc:
                        st.error(f"Seed failed: {exc}")

        st.markdown("### Operational Data Reset")
        st.caption("Wipe operational tables while keeping app users and role permissions.")
        if settings.app_env.lower() == "prod":
            st.warning("Operational reset is disabled in `APP_ENV=prod`.")
        else:
            with st.form("admin_wipe_operational_form"):
                include_shipping_presets = st.checkbox("Also wipe shipping presets", value=False)
                include_document_templates = st.checkbox("Also wipe document templates", value=False)
                include_audit_logs = st.checkbox("Also wipe audit logs", value=False)
                wipe_phrase = st.text_input("Type WIPE to confirm")
                run_wipe = st.form_submit_button("Wipe Operational Data")

            if run_wipe:
                if wipe_phrase.strip() != "WIPE":
                    st.error("Type `WIPE` exactly to confirm.")
                else:
                    try:
                        deleted_counts = _wipe_operational_data(
                            repo,
                            include_shipping_presets=include_shipping_presets,
                            include_document_templates=include_document_templates,
                            include_audit_logs=include_audit_logs,
                        )
                        deleted_summary = ", ".join(
                            f"{table}={count}" for table, count in sorted(deleted_counts.items())
                        )
                        st.success(f"Operational data wipe completed. Deleted rows: {deleted_summary}")
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Operational data wipe failed: {exc}")

    with tab_backups:
        st.markdown("### Database Backups")
        st.caption("Create SQL dumps, upload to S3, and run guarded restores.")
        policy_enabled = get_runtime_bool(repo, "backup_policy_enabled", False)
        policy_upload_to_s3 = get_runtime_bool(repo, "backup_policy_upload_to_s3", True)
        policy_cadence_hours = max(1, int(get_runtime_int(repo, "backup_policy_cadence_hours", 24)))
        policy_retention_days = max(1, int(get_runtime_int(repo, "backup_policy_retention_days", 30)))
        policy_drill_interval_days = max(1, int(get_runtime_int(repo, "backup_restore_drill_interval_days", 30)))
        policy_rto_target_minutes = max(1, int(get_runtime_int(repo, "backup_restore_rto_target_minutes", 60)))
        policy_owner = get_runtime_str(repo, "backup_policy_owner", "").strip()
        policy_runner_enabled = get_runtime_bool(repo, "backup_policy_runner_enabled", False)
        policy_schedule_timezone = get_runtime_str(repo, "backup_policy_schedule_timezone", "America/Denver").strip()
        policy_schedule_local_time = get_runtime_str(repo, "backup_policy_schedule_local_time", "02:00").strip()

        st.markdown("### Backup Policy")
        st.caption("Environment-scoped policy settings for cadence, retention, and restore-drill objectives.")
        with st.form("admin_backup_policy_form"):
            bp1, bp2 = st.columns(2)
            with bp1:
                backup_policy_enabled = st.checkbox(
                    "Backup Policy Enabled",
                    value=bool(policy_enabled),
                    key="admin_backup_policy_enabled",
                )
                backup_policy_upload_to_s3 = st.checkbox(
                    "Policy Requires S3 Upload",
                    value=bool(policy_upload_to_s3),
                    key="admin_backup_policy_upload_to_s3",
                )
                backup_policy_owner = st.text_input(
                    "Policy Owner",
                    value=policy_owner,
                    placeholder="ops@goldenstackers.com or on-call team",
                    key="admin_backup_policy_owner",
                )
                backup_policy_runner_enabled = st.checkbox(
                    "Enable Scheduled Backup Runner",
                    value=bool(policy_runner_enabled),
                    key="admin_backup_policy_runner_enabled",
                    help="When enabled, sync-runner executes one scheduled backup per local day.",
                )
            with bp2:
                backup_policy_cadence_hours = st.number_input(
                    "Backup Cadence (hours)",
                    min_value=1,
                    max_value=720,
                    value=int(policy_cadence_hours),
                    step=1,
                    key="admin_backup_policy_cadence_hours",
                )
                backup_policy_retention_days = st.number_input(
                    "Retention (days)",
                    min_value=1,
                    max_value=3650,
                    value=int(policy_retention_days),
                    step=1,
                    key="admin_backup_policy_retention_days",
                )
                backup_restore_drill_interval_days = st.number_input(
                    "Restore Drill Interval (days)",
                    min_value=1,
                    max_value=365,
                    value=int(policy_drill_interval_days),
                    step=1,
                    key="admin_backup_restore_drill_interval_days",
                )
                backup_restore_rto_target_minutes = st.number_input(
                    "Restore RTO Target (minutes)",
                    min_value=1,
                    max_value=10080,
                    value=int(policy_rto_target_minutes),
                    step=1,
                    key="admin_backup_restore_rto_target_minutes",
                )
                backup_policy_schedule_timezone = st.text_input(
                    "Backup Schedule Timezone",
                    value=policy_schedule_timezone or "America/Denver",
                    key="admin_backup_policy_schedule_timezone",
                    help="IANA timezone, for example America/Denver or UTC.",
                )
                backup_policy_schedule_local_time = st.text_input(
                    "Backup Schedule Local Time (HH:MM)",
                    value=policy_schedule_local_time or "02:00",
                    key="admin_backup_policy_schedule_local_time",
                    help="24-hour local time in the selected timezone.",
                )
            save_backup_policy = st.form_submit_button("Save Backup Policy")

        if save_backup_policy:
            try:
                updates = [
                    ("backup_policy_enabled", "true" if backup_policy_enabled else "false", "bool"),
                    ("backup_policy_upload_to_s3", "true" if backup_policy_upload_to_s3 else "false", "bool"),
                    ("backup_policy_owner", str(backup_policy_owner or "").strip(), "str"),
                    ("backup_policy_cadence_hours", str(int(backup_policy_cadence_hours)), "int"),
                    ("backup_policy_retention_days", str(int(backup_policy_retention_days)), "int"),
                    ("backup_restore_drill_interval_days", str(int(backup_restore_drill_interval_days)), "int"),
                    ("backup_restore_rto_target_minutes", str(int(backup_restore_rto_target_minutes)), "int"),
                    ("backup_policy_runner_enabled", "true" if backup_policy_runner_enabled else "false", "bool"),
                    ("backup_policy_schedule_timezone", str(backup_policy_schedule_timezone or "").strip() or "America/Denver", "str"),
                    ("backup_policy_schedule_local_time", str(backup_policy_schedule_local_time or "").strip() or "02:00", "str"),
                ]
                descriptions = {
                    "backup_policy_enabled": "Enable scheduled backup policy reporting/tracking for this environment.",
                    "backup_policy_upload_to_s3": "Whether backups should be uploaded to S3 by policy.",
                    "backup_policy_owner": "Primary owner/team accountable for backup policy and drill execution.",
                    "backup_policy_cadence_hours": "Expected backup cadence in hours for compliance and readiness checks.",
                    "backup_policy_retention_days": "Expected backup retention window in days.",
                    "backup_restore_drill_interval_days": "Maximum target days between successful restore drills.",
                    "backup_restore_rto_target_minutes": "Target restore recovery-time objective (minutes) used for drill evidence.",
                    "backup_policy_runner_enabled": "Enable scheduled backup execution by sync-runner.",
                    "backup_policy_schedule_timezone": "IANA timezone used for scheduled backup execution.",
                    "backup_policy_schedule_local_time": "Local-time HH:MM used for daily scheduled backup execution.",
                }
                for key, value, value_type in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=descriptions.get(key, "Backup policy runtime setting."),
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Backup policy settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save backup policy settings: {exc}")

        tools = pg_tools_status()
        tool_df = pd.DataFrame(
            [
                {"tool": "pg_dump", "available": tools["pg_dump"]},
                {"tool": "psql", "available": tools["psql"]},
            ]
        )
        st.dataframe(tool_df, use_container_width=True)
        if not tools["pg_dump"]:
            st.warning("`pg_dump` not available in app runtime. Install PostgreSQL client tools in image.")

        s3_enabled = s3_backup_enabled()
        st.caption(f"S3 backup target: `{settings.s3_bucket}` ({'enabled' if s3_enabled else 'disabled'})")

        st.markdown("### Create Backup")
        with st.form("admin_backup_create_form"):
            include_drop = st.checkbox("Include DROP statements (`--clean --if-exists`)", value=True)
            upload_to_s3 = st.checkbox("Upload backup to S3 after dump", value=s3_enabled, disabled=not s3_enabled)
            run_backup = st.form_submit_button("Create Backup Dump")

        if run_backup:
            try:
                backup = create_backup_dump(include_drop_statements=include_drop)
                backup_bytes = backup.file_path.read_bytes()
                st.session_state["admin_backup_name"] = backup.file_name
                st.session_state["admin_backup_bytes"] = backup_bytes
                st.success(f"Backup created: `{backup.file_name}` ({backup.size_bytes} bytes).")
                if upload_to_s3:
                    key = upload_backup_to_s3(backup.file_path)
                    st.success(f"Uploaded to S3 key `{key}`.")
            except Exception as exc:
                st.error(f"Backup failed: {exc}")

        backup_name = st.session_state.get("admin_backup_name")
        backup_bytes = st.session_state.get("admin_backup_bytes")
        if backup_name and backup_bytes:
            st.download_button(
                "Download Last Backup",
                data=backup_bytes,
                file_name=backup_name,
                mime="application/sql",
                key="admin_backup_download",
            )

        st.markdown("### S3 Backups")
        if s3_enabled:
            refresh = st.button("Refresh S3 Backup List")
            if refresh or "admin_backup_s3_rows" not in st.session_state:
                try:
                    st.session_state["admin_backup_s3_rows"] = list_backups_in_s3()
                except Exception as exc:
                    st.error(f"Unable to list S3 backups: {exc}")
                    st.session_state["admin_backup_s3_rows"] = []
            s3_rows = st.session_state.get("admin_backup_s3_rows", [])
            if s3_rows:
                st.dataframe(pd.DataFrame(s3_rows), use_container_width=True)
            else:
                st.info("No backups found in S3 prefix.")
        else:
            st.info("Enable S3 configuration to use backup upload/list/restore from bucket.")

        st.markdown("### Restore")
        st.caption("Restore is blocked in prod. Prefer maintenance window and app downtime.")
        if settings.app_env.lower() == "prod":
            st.warning("Restore is disabled in `APP_ENV=prod`.")
        else:
            restore_source = st.radio(
                "Restore Source",
                options=["upload_sql_file", "s3_backup_key"],
                format_func=lambda x: "Upload SQL file" if x == "upload_sql_file" else "S3 backup key",
            )

            uploaded_restore = None
            selected_s3_key = ""
            if restore_source == "upload_sql_file":
                uploaded_restore = st.file_uploader("SQL Dump File", type=["sql"])
            else:
                s3_rows = st.session_state.get("admin_backup_s3_rows", [])
                key_options = [row.get("key", "") for row in s3_rows if row.get("key")]
                if key_options:
                    selected_s3_key = st.selectbox("S3 Backup Key", options=key_options)
                else:
                    st.info("Refresh S3 backup list above to select a key.")

            with st.form("admin_backup_restore_form"):
                confirm_restore = st.checkbox("I understand restore can overwrite data and interrupt the app.")
                restore_phrase = st.text_input("Type RESTORE to confirm")
                run_restore = st.form_submit_button("Run Restore")

            if run_restore:
                if not confirm_restore or restore_phrase.strip() != "RESTORE":
                    st.error("Confirm restore acknowledgement and type `RESTORE` exactly.")
                else:
                    try:
                        repo.db.rollback()
                        if restore_source == "upload_sql_file":
                            if uploaded_restore is None:
                                raise RuntimeError("Upload a SQL file to restore.")
                            temp_path = Path(f"/tmp/{uploaded_restore.name}")
                            temp_path.write_bytes(uploaded_restore.getvalue())
                            restore_dump_file(temp_path)
                        else:
                            if not selected_s3_key:
                                raise RuntimeError("Select an S3 backup key to restore.")
                            downloaded = download_backup_from_s3(selected_s3_key)
                            restore_dump_file(downloaded)
                        st.success("Restore completed. Reloading app state.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Restore failed: {exc}")

        st.markdown("### Restore Drill Evidence")
        st.caption("Track restore drills with outcome, duration, and source details for disaster recovery auditability.")
        recent_drill_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "backup_restore_drill")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        drill_rows: list[dict[str, Any]] = []
        for row in recent_drill_logs:
            payload = _audit_changes(row)
            result = str(payload.get("result") or "").strip().lower()
            duration_minutes = payload.get("duration_minutes")
            duration_minutes_int = int(duration_minutes) if str(duration_minutes or "").isdigit() else None
            drill_rows.append(
                {
                    "id": int(row.id),
                    "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": str(payload.get("target_env") or settings.app_env),
                    "drill_date": str(payload.get("drill_date") or ""),
                    "result": result,
                    "source_type": str(payload.get("source_type") or ""),
                    "source_ref": str(payload.get("source_ref") or ""),
                    "duration_minutes": duration_minutes_int,
                    "rto_target_minutes": payload.get("rto_target_minutes"),
                    "rto_met": payload.get("rto_met"),
                    "notes": str(payload.get("notes") or "")[:240],
                }
            )
        drill_df = pd.DataFrame(drill_rows)
        latest_pass_days: int | None = None
        if not drill_df.empty:
            pass_rows = drill_df[drill_df["result"].astype(str) == "pass"]
            if not pass_rows.empty:
                latest_pass_iso = str(pass_rows.iloc[0].get("recorded_at_utc") or "").strip()
                try:
                    latest_pass_ts = datetime.fromisoformat(latest_pass_iso)
                    latest_pass_days = max(0, (utcnow_naive() - latest_pass_ts).days)
                except Exception:
                    latest_pass_days = None
        d1, d2, d3 = st.columns(3)
        d1.metric("Restore Drill Events", int(len(drill_df)))
        d2.metric("Latest Pass Age (days)", "n/a" if latest_pass_days is None else int(latest_pass_days))
        d3.metric("Drill SLA (days)", int(policy_drill_interval_days))
        if latest_pass_days is not None and latest_pass_days > int(policy_drill_interval_days):
            st.warning(
                f"Restore drill SLA breached: last passing drill is {latest_pass_days} days old "
                f"(target <= {int(policy_drill_interval_days)} days)."
            )

        with st.form("admin_backup_restore_drill_event_form"):
            r1, r2 = st.columns(2)
            with r1:
                drill_date = st.date_input("Drill Date", value=datetime.now().date())
                drill_result = st.selectbox("Result", options=["pass", "partial", "fail"], index=0)
                drill_target_env = st.text_input("Target Environment", value=settings.app_env)
                drill_source_type = st.selectbox(
                    "Restore Source Type",
                    options=["s3_backup_key", "upload_sql_file", "local_file", "other"],
                    index=0,
                )
            with r2:
                drill_source_ref = st.text_input("Restore Source Reference", placeholder="s3://bucket/key or filename")
                drill_duration_minutes = st.number_input(
                    "Restore Duration (minutes)",
                    min_value=0,
                    max_value=10080,
                    value=0,
                    step=1,
                )
                drill_rto_target_minutes = st.number_input(
                    "RTO Target (minutes)",
                    min_value=1,
                    max_value=10080,
                    value=int(policy_rto_target_minutes),
                    step=1,
                )
            drill_notes = st.text_area(
                "Notes / Recovery Evidence",
                placeholder="What was restored, validation checks performed, issues, follow-ups.",
            )
            record_drill_event = st.form_submit_button("Record Restore Drill Event")
        if record_drill_event:
            try:
                duration_value = int(drill_duration_minutes)
                rto_target_value = int(drill_rto_target_minutes)
                repo.record_audit_event(
                    entity_type="backup_restore_drill",
                    entity_id=None,
                    action="record",
                    actor=user.username,
                    changes={
                        "target_env": str(drill_target_env or settings.app_env).strip(),
                        "drill_date": str(drill_date.isoformat()),
                        "result": str(drill_result or "").strip().lower(),
                        "source_type": str(drill_source_type or "").strip(),
                        "source_ref": str(drill_source_ref or "").strip(),
                        "duration_minutes": duration_value,
                        "rto_target_minutes": rto_target_value,
                        "rto_met": bool(duration_value <= rto_target_value),
                        "notes": str(drill_notes or "").strip(),
                    },
                )
                st.success("Restore drill event recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record restore drill event: {exc}")

        if drill_df.empty:
            st.caption("No restore drill evidence recorded yet.")
        else:
            st.dataframe(drill_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Restore Drill Evidence CSV",
                data=drill_df.to_csv(index=False).encode("utf-8"),
                file_name=f"backup_restore_drills_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_backup_restore_drills_download",
            )

        st.markdown("### DR Checklist + SLA Reporting")
        st.caption("Record environment-specific DR checklist snapshots and view restore-drill SLA coverage by environment.")
        with st.form("admin_backup_dr_checklist_form"):
            c1, c2 = st.columns(2)
            with c1:
                checklist_target_env = st.selectbox(
                    "Checklist Target Environment",
                    options=["local", "dev", "prod", settings.app_env],
                    index=0,
                )
                checklist_owner = st.text_input(
                    "Checklist Owner",
                    placeholder="name/team",
                )
                checklist_evidence_link = st.text_input(
                    "Evidence Link",
                    placeholder="runbook link, ticket, or artifact URL",
                )
            with c2:
                item_policy_reviewed = st.checkbox("Backup policy reviewed for target environment", value=True)
                item_recent_backup_verified = st.checkbox("Recent backup artifact verified", value=True)
                item_restore_drill_within_sla = st.checkbox("Restore drill is within SLA window", value=True)
                item_restore_validation_smoke = st.checkbox("Post-restore validation smoke tests completed", value=True)
                item_rto_documented = st.checkbox("RTO/RPO notes documented", value=True)
            checklist_notes = st.text_area(
                "Checklist Notes",
                placeholder="Open gaps, owners, remediation ETA, and sign-off comments.",
            )
            record_dr_checklist = st.form_submit_button("Record DR Checklist Snapshot")
        if record_dr_checklist:
            try:
                items = {
                    "backup_policy_reviewed": bool(item_policy_reviewed),
                    "recent_backup_verified": bool(item_recent_backup_verified),
                    "restore_drill_within_sla": bool(item_restore_drill_within_sla),
                    "restore_validation_smoke_tests": bool(item_restore_validation_smoke),
                    "rto_rpo_documented": bool(item_rto_documented),
                }
                completed_count = sum(1 for v in items.values() if bool(v))
                total_count = len(items)
                completion_pct = round((float(completed_count) / float(total_count) * 100.0), 2) if total_count else 0.0
                repo.record_audit_event(
                    entity_type="backup_dr_checklist",
                    entity_id=None,
                    action="snapshot",
                    actor=user.username,
                    changes={
                        "target_env": str(checklist_target_env or settings.app_env).strip().lower(),
                        "owner": str(checklist_owner or "").strip(),
                        "evidence_link": str(checklist_evidence_link or "").strip(),
                        "items": items,
                        "completed_count": int(completed_count),
                        "total_count": int(total_count),
                        "completion_percent": float(completion_pct),
                        "notes": str(checklist_notes or "").strip(),
                    },
                )
                st.success("DR checklist snapshot recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record DR checklist snapshot: {exc}")

        if not drill_df.empty:
            sla_df = drill_df.copy()
            sla_df["target_env"] = sla_df["target_env"].astype(str).str.strip().replace("", settings.app_env)
            sla_df["result"] = sla_df["result"].astype(str).str.strip().str.lower()
            env_rows: list[dict[str, Any]] = []
            for env_name in sorted({str(v).strip() for v in sla_df["target_env"].tolist() if str(v).strip()}):
                env_df = sla_df[sla_df["target_env"] == env_name].copy()
                pass_df = env_df[env_df["result"] == "pass"].copy()
                latest_event = str(env_df["recorded_at_utc"].iloc[0] or "") if not env_df.empty else ""
                latest_pass = str(pass_df["recorded_at_utc"].iloc[0] or "") if not pass_df.empty else ""
                latest_pass_age_days: int | None = None
                if latest_pass:
                    try:
                        latest_pass_ts = datetime.fromisoformat(latest_pass)
                        latest_pass_age_days = max(0, (utcnow_naive() - latest_pass_ts).days)
                    except Exception:
                        latest_pass_age_days = None
                pass_count = int(len(pass_df))
                total_count = int(len(env_df))
                pass_rate_pct = round((float(pass_count) / float(total_count) * 100.0), 2) if total_count > 0 else 0.0
                sla_status = (
                    "breach"
                    if (latest_pass_age_days is None or latest_pass_age_days > int(policy_drill_interval_days))
                    else "ok"
                )
                env_rows.append(
                    {
                        "target_env": env_name,
                        "drills_total": total_count,
                        "drills_pass": pass_count,
                        "pass_rate_percent": pass_rate_pct,
                        "latest_event_at_utc": latest_event,
                        "latest_pass_at_utc": latest_pass,
                        "latest_pass_age_days": latest_pass_age_days,
                        "sla_target_days": int(policy_drill_interval_days),
                        "sla_status": sla_status,
                    }
                )
            if env_rows:
                env_sla_df = pd.DataFrame(env_rows).sort_values(["sla_status", "target_env"], ascending=[True, True])
                st.caption("Restore Drill SLA by Environment")
                st.dataframe(env_sla_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download DR SLA Report CSV",
                    data=env_sla_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"backup_restore_sla_{settings.app_env}.csv",
                    mime="text/csv",
                    key="admin_backup_restore_sla_download",
                )

        checklist_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "backup_dr_checklist")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(100)
        ).all()
        checklist_rows: list[dict[str, Any]] = []
        for row in checklist_logs:
            payload = _audit_changes(row)
            items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
            checklist_rows.append(
                {
                    "id": int(row.id),
                    "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": str(payload.get("target_env") or ""),
                    "owner": str(payload.get("owner") or ""),
                    "evidence_link": str(payload.get("evidence_link") or ""),
                    "completed_count": payload.get("completed_count"),
                    "total_count": payload.get("total_count"),
                    "completion_percent": payload.get("completion_percent"),
                    "backup_policy_reviewed": items.get("backup_policy_reviewed"),
                    "recent_backup_verified": items.get("recent_backup_verified"),
                    "restore_drill_within_sla": items.get("restore_drill_within_sla"),
                    "restore_validation_smoke_tests": items.get("restore_validation_smoke_tests"),
                    "rto_rpo_documented": items.get("rto_rpo_documented"),
                    "notes": str(payload.get("notes") or "")[:220],
                }
            )
        checklist_df = pd.DataFrame(checklist_rows)
        if checklist_df.empty:
            st.caption("No DR checklist snapshots recorded yet.")
        else:
            st.caption("Recent DR Checklist Snapshots")
            st.dataframe(checklist_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download DR Checklist Snapshots CSV",
                data=checklist_df.to_csv(index=False).encode("utf-8"),
                file_name=f"backup_dr_checklist_snapshots_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_backup_dr_checklist_download",
            )

    with tab_ebay_verify:
        st.markdown("### eBay API Verification")
        st.caption("Validate eBay credentials and confirm token/API calls succeed from this runtime.")
        st.markdown("### Connection Status")
        render_ebay_connection_status_card(repo)
        token_diag = _ebay_token_auto_refresh_diagnostics(repo)
        st.markdown("#### Token Auto-Refresh Diagnostics")
        d1, d2, d3, d4 = st.columns(4)
        with d1:
            st.metric("Refresh Interval (h)", int(token_diag.get("interval_hours") or 0))
        with d2:
            st.metric("Min TTL Trigger (min)", int(token_diag.get("min_ttl_minutes") or 0))
        with d3:
            exp_val = token_diag.get("expires_in_minutes")
            st.metric("Access Token Expires In (min)", exp_val if exp_val is not None else "(unknown)")
        with d4:
            st.metric(
                "Failure Cooldown Active",
                "yes" if bool(token_diag.get("failure_cooldown_active")) else "no",
            )
        if bool(token_diag.get("failure_cooldown_active")):
            st.warning(
                "Auto-refresh failure cooldown is active until "
                f"`{str(token_diag.get('failure_cooldown_until') or '(unknown)')}`."
            )
        td1, td2 = st.columns([1, 3])
        with td1:
            clear_refresh_failure_state = st.button(
                "Clear Refresh Failure State",
                key="admin_ebay_clear_refresh_failure_state_btn",
                use_container_width=True,
            )
        with td2:
            st.caption(
                "Use after fixing credentials/scopes to clear cooldown/error state and allow immediate retry."
            )
        if clear_refresh_failure_state:
            if ensure_permission(user, "update", "Clear eBay OAuth Refresh Failure State"):
                try:
                    _clear_ebay_token_refresh_failure_state(repo, actor=user.username)
                    st.success("Cleared eBay token refresh failure/cooldown state.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear refresh failure state: {exc}")
        diag_rows = [
            {"field": "Now", "value": str(token_diag.get("now") or "")},
            {"field": "Refreshed At", "value": str(token_diag.get("refreshed_at") or "")},
            {"field": "Next Refresh Due At", "value": str(token_diag.get("next_refresh_due_at") or "")},
            {"field": "Access Token Expires At", "value": str(token_diag.get("expires_at") or "")},
            {"field": "Last Refresh Failed At", "value": str(token_diag.get("failed_at") or "")},
            {"field": "Failure Cooldown Until", "value": str(token_diag.get("failure_cooldown_until") or "")},
            {"field": "Last Refresh Error", "value": str(token_diag.get("last_error") or "")},
        ]
        st.dataframe(pd.DataFrame(diag_rows), use_container_width=True, hide_index=True)
        client = EbayClient()
        ebay_auth_accepted_url = get_runtime_str(
            repo,
            "ebay_auth_accepted_url",
            settings.ebay_auth_accepted_url_effective,
        ).strip()
        ebay_auth_declined_url = get_runtime_str(
            repo,
            "ebay_auth_declined_url",
            settings.ebay_auth_declined_url_effective,
        ).strip()

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "field": "Environment",
                        "value": settings.ebay_environment,
                    },
                    {
                        "field": "Client ID",
                        "value": _mask_secret(settings.ebay_client_id),
                    },
                    {
                        "field": "Client Secret",
                        "value": _mask_secret(settings.ebay_client_secret),
                    },
                    {
                        "field": "RU Name",
                        "value": settings.ebay_ru_name or "(not set)",
                    },
                    {
                        "field": "Auth Accepted URL",
                        "value": ebay_auth_accepted_url or "(not set)",
                    },
                    {
                        "field": "Auth Declined URL",
                        "value": ebay_auth_declined_url or "(not set)",
                    },
                    {
                        "field": "User Access Token",
                        "value": "set" if settings.ebay_user_access_token.strip() else "(not set)",
                    },
                    {
                        "field": "User Refresh Token",
                        "value": "set"
                        if get_runtime_str(
                            repo,
                            "ebay_user_refresh_token",
                            settings.ebay_user_refresh_token,
                        ).strip()
                        else "(not set)",
                    },
                    {
                        "field": "Configured",
                        "value": "yes" if client.is_configured() else "no",
                    },
                    {
                        "field": "Auth Query Fallback",
                        "value": "enabled"
                        if get_runtime_bool(
                            repo,
                            "auth_query_token_fallback_enabled",
                            getattr(settings, "app_auth_query_token_fallback_enabled", True),
                        )
                        else "disabled",
                    },
                ]
            ),
            use_container_width=True,
        )
        expected_callback_host = {
            "prod": "inventory.goldenstackers.com",
            "production": "inventory.goldenstackers.com",
            "dev": "dev-inventory.goldenstackers.com",
            "development": "dev-inventory.goldenstackers.com",
            "staging": "dev-inventory.goldenstackers.com",
            "local": "localhost:8501",
        }.get((settings.app_env or "").strip().lower(), "")
        accepted_parsed = urlparse(ebay_auth_accepted_url or "")
        declined_parsed = urlparse(ebay_auth_declined_url or "")
        accepted_path = (accepted_parsed.path or "").strip()
        declined_path = (declined_parsed.path or "").strip()
        accepted_host = (accepted_parsed.netloc or "").strip()
        declined_host = (declined_parsed.netloc or "").strip()
        readiness_checks = [
            {
                "check": "Client ID present",
                "status": "pass" if bool(settings.ebay_client_id.strip()) else "fail",
                "details": "Configured" if settings.ebay_client_id.strip() else "Missing EBAY_CLIENT_ID",
            },
            {
                "check": "Client Secret present",
                "status": "pass" if bool(settings.ebay_client_secret.strip()) else "fail",
                "details": "Configured" if settings.ebay_client_secret.strip() else "Missing EBAY_CLIENT_SECRET",
            },
            {
                "check": "RU Name present",
                "status": "pass" if bool(settings.ebay_ru_name.strip()) else "fail",
                "details": settings.ebay_ru_name.strip() if settings.ebay_ru_name.strip() else "Missing EBAY_RU_NAME",
            },
            {
                "check": "Accepted callback URL set",
                "status": "pass" if bool(ebay_auth_accepted_url) else "fail",
                "details": ebay_auth_accepted_url or "(not set)",
            },
            {
                "check": "Declined callback URL set",
                "status": "pass" if bool(ebay_auth_declined_url) else "fail",
                "details": ebay_auth_declined_url or "(not set)",
            },
            {
                "check": "Accepted callback path",
                "status": "pass" if accepted_path == "/eBay_Workspace" else "warn",
                "details": accepted_path or "(missing path)",
            },
            {
                "check": "Declined callback path",
                "status": "pass" if declined_path == "/eBay_Workspace" else "warn",
                "details": declined_path or "(missing path)",
            },
            {
                "check": "Accepted/Declined URL consistency",
                "status": "pass" if ebay_auth_accepted_url == ebay_auth_declined_url else "warn",
                "details": "Same URL" if ebay_auth_accepted_url == ebay_auth_declined_url else "URLs differ",
            },
            {
                "check": "Callback host matches environment",
                "status": (
                    "pass"
                    if (
                        expected_callback_host
                        and accepted_host == expected_callback_host
                        and declined_host == expected_callback_host
                    )
                    else "warn"
                ),
                "details": (
                    f"accepted={accepted_host or '(missing)'} | declined={declined_host or '(missing)'} | "
                    f"expected={expected_callback_host or '(unknown)'}"
                ),
            },
            {
                "check": "Client config complete",
                "status": "pass" if client.is_configured() else "fail",
                "details": "Ready for OAuth/app-token calls." if client.is_configured() else "Missing required keyset values.",
            },
        ]
        readiness_df = pd.DataFrame(readiness_checks)
        pass_count = int((readiness_df["status"] == "pass").sum())
        warn_count = int((readiness_df["status"] == "warn").sum())
        fail_count = int((readiness_df["status"] == "fail").sum())
        st.markdown("#### OAuth Readiness Check")
        r1, r2, r3 = st.columns(3)
        r1.metric("Pass", pass_count)
        r2.metric("Warn", warn_count)
        r3.metric("Fail", fail_count)
        st.dataframe(readiness_df, use_container_width=True, hide_index=True)
        if fail_count > 0:
            st.error("Resolve failed checks before starting OAuth authorization.")
        elif warn_count > 0:
            st.warning("OAuth can proceed, but warning checks may cause callback or session issues.")
        else:
            st.success("OAuth readiness checks passed.")

        load_recent_verification_feedback = st.checkbox(
            "Load Recent Verification Feedback (slower)",
            value=False,
            key="admin_ebay_verify_load_recent_feedback",
            help="Defers eBay verification audit-history query unless explicitly requested.",
        )
        recent_verify_feedback: list[dict[str, Any]] = []
        if not load_recent_verification_feedback:
            st.caption(
                "Recent verification feedback is deferred. Enable "
                "`Load Recent Verification Feedback (slower)` to query audit history."
            )
        else:
            try:
                audit_rows = repo.list_audit_logs(limit=500)
                for row in audit_rows:
                    if str(getattr(row, "entity_type", "") or "").strip().lower() != "ebay_verify":
                        continue
                    payload = _audit_changes(row)
                    recent_verify_feedback.append(
                        {
                            "at": row.created_at,
                            "actor": str(getattr(row, "actor", "") or "").strip(),
                            "action": str(getattr(row, "action", "") or "").strip(),
                            "status": str(payload.get("status") or "").strip(),
                            "resolved_user": str(payload.get("resolved_user") or "").strip(),
                            "seller_registered": bool(payload.get("seller_registered"))
                            if payload.get("seller_registered") is not None
                            else "",
                            "message": str(payload.get("message") or "").strip(),
                        }
                    )
                    if len(recent_verify_feedback) >= 50:
                        break
            except Exception:
                recent_verify_feedback = []
            if recent_verify_feedback:
                st.caption("Recent Verification Feedback")
                st.dataframe(pd.DataFrame(recent_verify_feedback), use_container_width=True, hide_index=True)
            else:
                st.caption("Recent Verification Feedback: no verification events recorded yet.")

        if not client.is_configured():
            st.warning("Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_RU_NAME before running verification.")
        else:
            st.caption("Step 1: Validate app keys using client-credentials token grant.")
            with st.form("admin_ebay_app_token_verify_form"):
                scope = st.selectbox(
                    "Scope",
                    options=EbayClient.SCOPES,
                    index=0,
                    help="Use base `api_scope` first for key validation.",
                )
                run_app_verify = st.form_submit_button("Verify App Token")

            if run_app_verify:
                try:
                    token_payload = client.fetch_application_token(scopes=[scope])
                    st.success("App token request succeeded. eBay keys are valid for this environment.")
                    try:
                        repo.record_audit_event(
                            entity_type="ebay_verify",
                            entity_id=None,
                            action="app_token_verify",
                            actor=user.username,
                            changes={
                                "status": "success",
                                "scope": str(scope or "").strip(),
                                "expires_in": token_payload.get("expires_in"),
                                "message": "App token request succeeded.",
                            },
                        )
                    except Exception:
                        pass
                    st.json(
                        {
                            "token_type": token_payload.get("token_type"),
                            "expires_in": token_payload.get("expires_in"),
                            "scope": token_payload.get("scope"),
                        }
                    )
                except Exception as exc:
                    st.error(f"App token verification failed: {exc}")
                    try:
                        repo.record_audit_event(
                            entity_type="ebay_verify",
                            entity_id=None,
                            action="app_token_verify",
                            actor=user.username,
                            changes={
                                "status": "error",
                                "scope": str(scope or "").strip(),
                                "message": str(exc),
                            },
                        )
                    except Exception:
                        pass

            st.caption("Step 2: Optional user-token API check (Sell Account privileges endpoint).")
            pending_access = str(st.session_state.pop("admin_ebay_verify_user_token_pending", "") or "").strip()
            if pending_access:
                st.session_state["admin_ebay_verify_user_token"] = pending_access
            pending_refresh = str(st.session_state.pop("admin_ebay_verify_refresh_token_pending", "") or "").strip()
            if pending_refresh:
                st.session_state["admin_ebay_verify_refresh_token"] = pending_refresh
            if "admin_ebay_verify_user_token" not in st.session_state:
                st.session_state["admin_ebay_verify_user_token"] = get_runtime_str(
                    repo,
                    "ebay_user_access_token",
                    settings.ebay_user_access_token,
                )
            if "admin_ebay_verify_refresh_token" not in st.session_state:
                st.session_state["admin_ebay_verify_refresh_token"] = get_runtime_str(
                    repo,
                    "ebay_user_refresh_token",
                    settings.ebay_user_refresh_token,
                )
            verify_user_token = st.text_area(
                "Access Token",
                height=120,
                key="admin_ebay_verify_user_token",
                help="Paste a user OAuth access token from eBay OAuth code exchange.",
            )
            verify_refresh_token = st.text_area(
                "Refresh Token (optional but recommended)",
                height=120,
                key="admin_ebay_verify_refresh_token",
                help="Used to auto-renew short-lived access tokens when they expire.",
            )
            if st.button("Verify User Token API Access", key="admin_ebay_verify_user_token_button"):
                if not verify_user_token.strip():
                    st.error("Paste an access token first.")
                else:
                    try:
                        token_text = verify_user_token.strip()
                        claims = client.decode_access_token_claims(token_text)
                        privileges = client.get_account_privileges(token_text)
                        identity_payload: dict[str, Any] = {}
                        identity_error = ""
                        try:
                            identity_payload = client.get_identity_user(token_text)
                        except Exception as exc:
                            identity_error = str(exc)
                        username_hint = (
                            str(
                                claims.get("preferred_username")
                                or claims.get("username")
                                or claims.get("user_name")
                                or claims.get("sub")
                                or ""
                            ).strip()
                        )
                        if not username_hint:
                            username_hint = str(
                                identity_payload.get("username")
                                or identity_payload.get("userId")
                                or identity_payload.get("userID")
                                or identity_payload.get("individualAccount", {}).get("email")
                                or ""
                            ).strip()
                        seller_registration_completed = bool(privileges.get("sellerRegistrationCompleted"))
                        token_scope = str(claims.get("scope") or "").strip()
                        token_exp = claims.get("exp")
                        token_iat = claims.get("iat")

                        st.success("User token API call succeeded.")
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Resolved User", username_hint or "(unknown)")
                        c2.metric(
                            "Seller Registered",
                            "yes" if seller_registration_completed else "no",
                        )
                        c3.metric("Token Scope Present", "yes" if token_scope else "no")
                        c4.metric("JWT Claims Parsed", "yes" if claims else "no")

                        feedback_rows = [
                            {
                                "check": "Token parse",
                                "status": "pass" if claims else "warn",
                                "details": "JWT claims parsed successfully."
                                if claims
                                else "Could not parse JWT claims; token may be opaque or non-JWT.",
                            },
                            {
                                "check": "User identity",
                                "status": "pass" if username_hint else "warn",
                                "details": username_hint
                                if username_hint
                                else "No username/sub claim found in token payload.",
                            },
                            {
                                "check": "Privileges endpoint",
                                "status": "pass",
                                "details": "Sell Account privileges endpoint returned successfully.",
                            },
                            {
                                "check": "Identity endpoint",
                                "status": "pass" if identity_payload else ("warn" if identity_error else "info"),
                                "details": (
                                    f"Resolved via Identity API: {username_hint or '(no username field)'}"
                                    if identity_payload
                                    else (
                                        f"Identity API check did not return user details: {identity_error}"
                                        if identity_error
                                        else "Identity API check not run."
                                    )
                                ),
                            },
                            {
                                "check": "Seller registration",
                                "status": "pass" if seller_registration_completed else "warn",
                                "details": "sellerRegistrationCompleted=true"
                                if seller_registration_completed
                                else "sellerRegistrationCompleted=false (seller operations may be blocked).",
                            },
                            {
                                "check": "Token timing",
                                "status": "info",
                                "details": f"iat={token_iat or '(missing)'}, exp={token_exp or '(missing)'}",
                            },
                        ]
                        st.caption("Verification Feedback")
                        st.dataframe(pd.DataFrame(feedback_rows), use_container_width=True, hide_index=True)
                        if not seller_registration_completed:
                            st.warning(
                                "Seller registration is not completed for this token/account. "
                                "Publishing/policy operations may fail until onboarding is complete."
                            )
                        try:
                            repo.upsert_runtime_setting(
                                environment=settings.app_env,
                                key="ebay_user_access_token",
                                value=token_text,
                                value_type="str",
                                description="Default eBay user access token used by verification and sync jobs.",
                                actor=user.username,
                            )
                            if verify_refresh_token.strip():
                                repo.upsert_runtime_setting(
                                    environment=settings.app_env,
                                    key="ebay_user_refresh_token",
                                    value=verify_refresh_token.strip(),
                                    value_type="str",
                                    description="Default eBay user refresh token used to renew access tokens.",
                                    actor=user.username,
                                )
                            st.caption("Verified tokens were persisted to runtime settings for this environment.")
                        except Exception:
                            pass
                        try:
                            repo.record_audit_event(
                                entity_type="ebay_verify",
                                entity_id=None,
                                action="user_token_verify",
                                actor=user.username,
                                changes={
                                    "status": "success",
                                    "resolved_user": username_hint,
                                    "seller_registered": seller_registration_completed,
                                    "token_scope_present": bool(token_scope),
                                    "claims_parsed": bool(claims),
                                    "identity_resolved": bool(identity_payload),
                                    "identity_error": identity_error,
                                    "message": "User token privileges check succeeded.",
                                },
                            )
                        except Exception:
                            pass
                        st.json(privileges)
                        with st.expander("Decoded Token Claims", expanded=False):
                            st.json(claims or {"note": "No decodable JWT claims found."})
                        with st.expander("Identity API Response", expanded=False):
                            if identity_payload:
                                st.json(identity_payload)
                            else:
                                st.json(
                                    {
                                        "note": "Identity endpoint did not return payload.",
                                        "error": identity_error or "",
                                    }
                                )
                    except Exception as exc:
                        refreshed_retry_ok = False
                        refresh_error = ""
                        refreshed_access = ""
                        if verify_refresh_token.strip():
                            try:
                                refreshed = client.refresh_user_token(verify_refresh_token.strip())
                                refreshed_access = str(refreshed.get("access_token") or "").strip()
                                rotated_refresh = str(refreshed.get("refresh_token") or "").strip()
                                expires_in = int(refreshed.get("expires_in") or 0)
                                if refreshed_access:
                                    repo.upsert_runtime_setting(
                                        environment=settings.app_env,
                                        key="ebay_user_access_token",
                                        value=refreshed_access,
                                        value_type="str",
                                        description="Default eBay user access token used in forms.",
                                        actor=user.username,
                                    )
                                    if rotated_refresh:
                                        repo.upsert_runtime_setting(
                                            environment=settings.app_env,
                                            key="ebay_user_refresh_token",
                                            value=rotated_refresh,
                                            value_type="str",
                                            description="Default eBay user refresh token used for access token renewal.",
                                            actor=user.username,
                                        )
                                    if expires_in > 0:
                                        repo.upsert_runtime_setting(
                                            environment=settings.app_env,
                                            key="ebay_user_access_token_expires_at",
                                            value=(
                                                utcnow_naive() + timedelta(seconds=max(0, expires_in - 120))
                                            ).isoformat(timespec="seconds"),
                                            value_type="str",
                                            description="Best-effort expiry timestamp for current eBay user access token.",
                                            actor=user.username,
                                        )
                                    st.session_state["admin_ebay_verify_user_token_pending"] = refreshed_access
                                    if rotated_refresh:
                                        st.session_state["admin_ebay_verify_refresh_token_pending"] = rotated_refresh
                                    retry_privileges = client.get_account_privileges(refreshed_access)
                                    refreshed_retry_ok = True
                                    st.success("Token was expired; refreshed successfully and verification now passed.")
                                    st.json(retry_privileges)
                                    st.rerun()
                            except Exception as refresh_exc:
                                refresh_error = str(refresh_exc)
                        if not refreshed_retry_ok:
                            st.error(f"User token verification failed: {exc}")
                            if verify_refresh_token.strip():
                                st.warning(f"Refresh attempt failed: {refresh_error or 'unknown error'}")
                        try:
                            repo.record_audit_event(
                                entity_type="ebay_verify",
                                entity_id=None,
                                action="user_token_verify",
                                actor=user.username,
                                changes={
                                    "status": "success" if refreshed_retry_ok else "error",
                                    "message": (
                                        "Recovered by refresh token retry."
                                        if refreshed_retry_ok
                                        else str(exc)
                                    ),
                                    "refresh_attempted": bool(verify_refresh_token.strip()),
                                    "refresh_error": refresh_error,
                                },
                            )
                        except Exception:
                            pass

    with tab_ai_runtime:
        st.markdown("### AI Provider Runtime")
        st.caption(
            "Configure OpenAI/LocalAI runtime profiles in DB. "
            "Comp Tool uses the default active profile for this environment."
        )
        try:
            ai_rows = repo.list_ai_provider_configs(environment=settings.app_env, active_only=False)
        except Exception as exc:
            st.error(
                "AI provider config table is not available yet. "
                "Run database migrations first (`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            ai_rows = []

        if ai_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "environment": row.environment,
                            "name": row.name,
                            "provider": row.provider,
                            "model": row.model,
                            "multimodal_model": row.multimodal_model,
                            "base_url": row.base_url,
                            "endpoint_type": row.endpoint_type,
                            "api_key": _mask_secret(row.api_key),
                            "temperature": float(row.temperature),
                            "max_output_tokens": row.max_output_tokens,
                            "timeout_seconds": row.timeout_seconds,
                            "is_default": bool(row.is_default),
                            "is_active": bool(row.is_active),
                        }
                        for row in ai_rows
                    ]
                ),
                use_container_width=True,
            )
            st.markdown("### Bulk Max Output Tokens Upgrade")
            st.caption(
                "One-click helper to raise all AI runtime profiles below a target token limit. "
                "Use Dry Run first to preview impacted profiles."
            )
            with st.form("admin_ai_bulk_token_upgrade_form", clear_on_submit=False):
                b1, b2, b3 = st.columns(3)
                with b1:
                    bulk_target_tokens = st.number_input(
                        "Target Max Output Tokens",
                        min_value=1,
                        max_value=16000,
                        value=16000,
                        step=100,
                    )
                with b2:
                    bulk_include_inactive = st.checkbox("Include Inactive Profiles", value=True)
                with b3:
                    bulk_dry_run = st.checkbox("Dry Run (no writes)", value=True)
                bulk_apply = st.form_submit_button("Apply To Profiles Below Target")
            if bulk_apply:
                try:
                    scoped_rows = (
                        ai_rows
                        if bulk_include_inactive
                        else [row for row in ai_rows if bool(getattr(row, "is_active", False))]
                    )
                    target_int = int(bulk_target_tokens)
                    candidates = [
                        row
                        for row in scoped_rows
                        if int(getattr(row, "max_output_tokens", 0) or 0) < target_int
                    ]
                    if not candidates:
                        st.info("No profiles are below the selected target.")
                    elif bulk_dry_run:
                        st.info(
                            f"Dry run: {len(candidates)} profile(s) would be updated to `{target_int}` max output tokens."
                        )
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "id": int(row.id),
                                        "name": str(row.name or ""),
                                        "provider": str(row.provider or ""),
                                        "is_active": bool(row.is_active),
                                        "before_max_output_tokens": int(getattr(row, "max_output_tokens", 0) or 0),
                                        "after_max_output_tokens": target_int,
                                    }
                                    for row in candidates
                                ]
                            ),
                            use_container_width=True,
                        )
                    else:
                        updated = 0
                        failures: list[str] = []
                        for row in candidates:
                            try:
                                repo.update_ai_provider_config(
                                    int(row.id),
                                    {"max_output_tokens": target_int},
                                    actor=user.username,
                                )
                                updated += 1
                            except Exception as row_exc:
                                failures.append(
                                    f"#{int(row.id)} {str(row.name or '')}: {str(row_exc)}"
                                )
                        if updated > 0:
                            st.success(
                                f"Updated {updated} profile(s) to `{target_int}` max output tokens."
                            )
                        if failures:
                            st.warning(
                                "Some profiles failed to update:\n" + "\n".join(f"- {msg}" for msg in failures[:20])
                            )
                        st.rerun()
                except Exception as exc:
                    st.error(f"Bulk token upgrade failed: {exc}")
        else:
            st.info("No AI runtime profiles found for this environment.")

        st.markdown("### Add/Update AI Runtime Profile")
        create_models_state_key = "admin_ai_create_model_options"
        create_model_options = list(st.session_state.get(create_models_state_key) or [])
        create_model_choice_options = ["(manual entry)"] + create_model_options
        st.caption(f"Endpoint model options loaded: `{len(create_model_options)}`")
        with st.form("admin_ai_runtime_upsert_form", clear_on_submit=False):
            c1, c2, c3 = st.columns(3)
            with c1:
                profile_name = st.text_input("Profile Name", value="default-openai")
            with c2:
                provider = st.selectbox("Provider", options=["openai", "localai"], index=0)
            with c3:
                endpoint_type = st.selectbox(
                    "Endpoint Type",
                    options=["responses", "chat_completions"],
                    index=0,
                    help="Use `responses` for OpenAI responses API. LocalAI usually uses `chat_completions`.",
                )
            d1, d2 = st.columns(2)
            with d1:
                selected_create_model = st.selectbox(
                    "Model (from endpoint)",
                    options=create_model_choice_options,
                    index=0,
                    help="Use Query /models to populate this list, or use manual entry.",
                )
            with d2:
                base_url = st.text_input("Base URL", value="https://api.openai.com/v1")
            create_model_manual = st.text_input(
                "Model (manual override)",
                value="gpt-4o-mini",
                help="If set, this value is used instead of the dropdown.",
            )
            selected_create_mm_model = st.selectbox(
                "Multimodal Model (from endpoint)",
                options=create_model_choice_options,
                index=0,
                help="Optional vision model for image/camera tools.",
            )
            create_mm_model_manual = st.text_input(
                "Multimodal Model (manual override, optional)",
                value="",
                help="Leave blank to use Text Model.",
            )
            api_key = st.text_input(
                "API Key / Token (optional for LocalAI)",
                value="",
                type="password",
                help="Stored in DB for runtime use. When updating an existing profile, blank keeps current key.",
            )
            e1, e2, e3 = st.columns(3)
            with e1:
                temperature = st.number_input("Temperature", min_value=0.0, max_value=2.0, value=0.2, step=0.1)
            with e2:
                max_output_tokens = st.number_input(
                    "Max Output Tokens", min_value=1, max_value=16000, value=16000, step=100
                )
            with e3:
                timeout_seconds = st.number_input(
                    "Timeout Seconds", min_value=5, max_value=300, value=60, step=5
                )
            notes = st.text_area("Notes", value="")
            f1, f2 = st.columns(2)
            with f1:
                is_default = st.checkbox("Set default for this environment", value=False)
            with f2:
                is_active = st.checkbox("Active", value=True)
            s1, s2 = st.columns(2)
            with s1:
                query_models_create = st.form_submit_button("Query /models")
            with s2:
                save_profile = st.form_submit_button("Save AI Runtime Profile")

        if query_models_create:
            try:
                token_for_query = (api_key or "").strip()
                if provider == "openai" and not token_for_query:
                    st.error("Provide API key/token to query OpenAI `/models`.")
                else:
                    loaded = fetch_available_models(
                        base_url=(base_url or "").strip(),
                        api_key=token_for_query,
                        timeout_seconds=int(timeout_seconds),
                    )
                    st.session_state[create_models_state_key] = loaded
                    st.success(f"Loaded {len(loaded)} model(s) from endpoint.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Unable to load models from endpoint: {exc}")

        if save_profile:
            try:
                resolved_model = (create_model_manual or "").strip()
                if not resolved_model and selected_create_model != "(manual entry)":
                    resolved_model = selected_create_model
                resolved_mm_model = (create_mm_model_manual or "").strip()
                if not resolved_mm_model and selected_create_mm_model != "(manual entry)":
                    resolved_mm_model = selected_create_mm_model
                if not resolved_mm_model:
                    resolved_mm_model = resolved_model
                if not resolved_model:
                    st.error("Text model is required. Select from dropdown or provide manual override.")
                else:
                    row = repo.upsert_ai_provider_config(
                        environment=settings.app_env,
                        name=profile_name.strip(),
                        provider=provider,
                        model=resolved_model,
                        multimodal_model=resolved_mm_model,
                        base_url=base_url.strip(),
                        endpoint_type=endpoint_type,
                        api_key=api_key.strip(),
                        temperature=Decimal(str(temperature)),
                        max_output_tokens=int(max_output_tokens),
                        timeout_seconds=int(timeout_seconds),
                        notes=notes.strip(),
                        is_default=bool(is_default),
                        is_active=bool(is_active),
                        actor=user.username,
                    )
                    st.success(f"Saved AI runtime profile `{row.name}`.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Unable to save profile: {exc}")

        if ai_rows:
            st.markdown("### Manage Existing Profile")
            profile_map = {
                f"#{row.id} | {row.name} | {row.provider} | default={row.is_default} | active={row.is_active}": row
                for row in ai_rows
            }
            selected_key = st.selectbox("Profile", options=list(profile_map.keys()), key="admin_ai_profile_select")
            selected_profile = profile_map[selected_key]

            selected_id_state_key = "admin_ai_edit_selected_profile_id"
            selected_id = int(selected_profile.id)

            def _load_selected_ai_profile_into_state() -> None:
                st.session_state["admin_ai_edit_name"] = (selected_profile.name or "").strip()
                st.session_state["admin_ai_edit_provider"] = (selected_profile.provider or "openai").strip().lower()
                st.session_state["admin_ai_edit_endpoint"] = (
                    selected_profile.endpoint_type or "responses"
                ).strip().lower()
                st.session_state["admin_ai_edit_model"] = (selected_profile.model or "").strip()
                st.session_state["admin_ai_edit_mm_model"] = (
                    (selected_profile.multimodal_model or "").strip()
                    or (selected_profile.model or "").strip()
                )
                st.session_state["admin_ai_edit_base_url"] = (selected_profile.base_url or "").strip()
                st.session_state["admin_ai_edit_api_key"] = ""
                st.session_state["admin_ai_edit_temp"] = float(selected_profile.temperature)
                st.session_state["admin_ai_edit_max_tokens"] = int(selected_profile.max_output_tokens)
                st.session_state["admin_ai_edit_timeout"] = int(selected_profile.timeout_seconds)
                st.session_state["admin_ai_edit_notes"] = (selected_profile.notes or "").strip()
                st.session_state["admin_ai_edit_default"] = bool(selected_profile.is_default)
                st.session_state["admin_ai_edit_active"] = bool(selected_profile.is_active)
                st.session_state[selected_id_state_key] = selected_id

            previous_selected_id = int(st.session_state.get(selected_id_state_key) or 0)
            if previous_selected_id != selected_id:
                _load_selected_ai_profile_into_state()

            st.caption("Edit all fields on the selected profile. Leave API key blank to keep existing key.")
            edit_models_state_key = f"admin_ai_edit_model_options_{selected_profile.id}"
            edit_model_options = list(st.session_state.get(edit_models_state_key) or [])
            default_edit_model = str(st.session_state.get("admin_ai_edit_model") or "").strip()
            default_edit_mm_model = str(st.session_state.get("admin_ai_edit_mm_model") or "").strip()
            if default_edit_model and default_edit_model not in edit_model_options:
                edit_model_options.append(default_edit_model)
            if default_edit_mm_model and default_edit_mm_model not in edit_model_options:
                edit_model_options.append(default_edit_mm_model)
            edit_model_options = sorted({m for m in edit_model_options if m})
            edit_model_choice_options = ["(manual entry)"] + edit_model_options
            st.caption(f"Endpoint model options loaded: `{len(edit_model_options)}`")

            with st.form(f"admin_ai_edit_form_{selected_profile.id}"):
                h1, h2, h3 = st.columns(3)
                with h1:
                    edit_name = st.text_input(
                        "Profile Name",
                        key="admin_ai_edit_name",
                    )
                with h2:
                    edit_provider = st.selectbox(
                        "Provider",
                        options=["openai", "localai"],
                        key="admin_ai_edit_provider",
                    )
                with h3:
                    edit_endpoint = st.selectbox(
                        "Endpoint Type",
                        options=["responses", "chat_completions"],
                        key="admin_ai_edit_endpoint",
                    )

                i1, i2 = st.columns(2)
                with i1:
                    edit_model_pick = st.selectbox(
                        "Text Model (from endpoint)",
                        options=edit_model_choice_options,
                        index=edit_model_choice_options.index(default_edit_model)
                        if default_edit_model in edit_model_choice_options
                        else 0,
                    )
                with i2:
                    edit_multimodal_model_pick = st.selectbox(
                        "Multimodal Model (from endpoint)",
                        options=edit_model_choice_options,
                        index=edit_model_choice_options.index(default_edit_mm_model)
                        if default_edit_mm_model in edit_model_choice_options
                        else 0,
                    )
                edit_model = st.text_input(
                    "Text Model (manual override)",
                    key="admin_ai_edit_model",
                    help="If set, this value is used instead of the dropdown.",
                )
                edit_multimodal_model = st.text_input(
                    "Multimodal Model (manual override)",
                    key="admin_ai_edit_mm_model",
                    help="Leave blank to use Text Model.",
                )

                edit_base_url = st.text_input(
                    "Base URL",
                    key="admin_ai_edit_base_url",
                )
                edit_api_key = st.text_input(
                    "API Key / Token (optional)",
                    type="password",
                    help="Leave blank to keep current API key.",
                    key="admin_ai_edit_api_key",
                )

                j1, j2, j3 = st.columns(3)
                with j1:
                    edit_temperature = st.number_input(
                        "Temperature",
                        min_value=0.0,
                        max_value=2.0,
                        step=0.1,
                        key="admin_ai_edit_temp",
                    )
                with j2:
                    edit_max_output_tokens = st.number_input(
                        "Max Output Tokens",
                        min_value=1,
                        max_value=16000,
                        step=50,
                        key="admin_ai_edit_max_tokens",
                    )
                with j3:
                    edit_timeout_seconds = st.number_input(
                        "Timeout Seconds",
                        min_value=5,
                        max_value=300,
                        step=5,
                        key="admin_ai_edit_timeout",
                    )

                edit_notes = st.text_area(
                    "Notes",
                    key="admin_ai_edit_notes",
                )
                k1, k2 = st.columns(2)
                with k1:
                    edit_is_default = st.checkbox(
                        "Set default for this environment",
                        key="admin_ai_edit_default",
                    )
                with k2:
                    edit_is_active = st.checkbox(
                        "Active",
                        key="admin_ai_edit_active",
                    )
                q1, q2 = st.columns(2)
                with q1:
                    query_models_edit = st.form_submit_button("Query /models For Selected")
                with q2:
                    save_existing_profile = st.form_submit_button("Save Changes to Selected Profile")

            if query_models_edit:
                try:
                    token_for_query = (edit_api_key or "").strip() or str(selected_profile.api_key or "").strip()
                    if edit_provider == "openai" and not token_for_query:
                        st.error("Provide API key/token to query OpenAI `/models`.")
                    else:
                        loaded = fetch_available_models(
                            base_url=(edit_base_url or "").strip(),
                            api_key=token_for_query,
                            timeout_seconds=int(edit_timeout_seconds),
                        )
                        st.session_state[edit_models_state_key] = loaded
                        st.success(f"Loaded {len(loaded)} model(s) for selected profile.")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Unable to load models from endpoint: {exc}")

            if save_existing_profile:
                try:
                    resolved_edit_model = (edit_model or "").strip()
                    if not resolved_edit_model and edit_model_pick != "(manual entry)":
                        resolved_edit_model = edit_model_pick
                    resolved_edit_mm_model = (edit_multimodal_model or "").strip()
                    if not resolved_edit_mm_model and edit_multimodal_model_pick != "(manual entry)":
                        resolved_edit_mm_model = edit_multimodal_model_pick
                    if not resolved_edit_mm_model:
                        resolved_edit_mm_model = resolved_edit_model
                    if not resolved_edit_model:
                        st.error("Text model is required. Select from dropdown or provide manual override.")
                    else:
                        updates = {
                            "name": edit_name.strip(),
                            "provider": edit_provider,
                            "endpoint_type": edit_endpoint,
                            "model": resolved_edit_model,
                            "multimodal_model": resolved_edit_mm_model,
                            "base_url": edit_base_url.strip().rstrip("/"),
                            "temperature": Decimal(str(edit_temperature)),
                            "max_output_tokens": int(edit_max_output_tokens),
                            "timeout_seconds": int(edit_timeout_seconds),
                            "notes": edit_notes.strip(),
                            "is_default": bool(edit_is_default),
                            "is_active": bool(edit_is_active),
                        }
                        if edit_api_key.strip():
                            updates["api_key"] = edit_api_key.strip()
                        repo.update_ai_provider_config(selected_profile.id, updates, actor=user.username)
                        st.success("Selected AI runtime profile updated.")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update selected profile: {exc}")

            if st.button("Test Selected Profile", key=f"admin_ai_test_profile_{selected_profile.id}"):
                try:
                    test_payload = validate_llm_runtime_config(
                        LLMRuntimeConfig(
                            source="db",
                            enabled=bool(selected_profile.is_active),
                            provider=(selected_profile.provider or "openai").strip().lower(),
                            model=(selected_profile.model or "").strip(),
                            multimodal_model=((selected_profile.multimodal_model or "").strip() or (selected_profile.model or "").strip()),
                            base_url=(selected_profile.base_url or "").strip().rstrip("/"),
                            endpoint_type=(selected_profile.endpoint_type or "responses").strip().lower(),
                            api_key=(selected_profile.api_key or "").strip(),
                            temperature=float(selected_profile.temperature),
                            max_output_tokens=int(selected_profile.max_output_tokens),
                            timeout_seconds=int(selected_profile.timeout_seconds),
                        )
                    )
                    st.success("AI runtime test succeeded.")
                    st.json(test_payload)
                except Exception as exc:
                    st.error(f"AI runtime test failed: {exc}")

            with st.form(f"admin_ai_delete_form_{selected_profile.id}"):
                confirm_delete_ai = st.checkbox("I understand this deletes the selected profile.")
                delete_phrase_ai = st.text_input("Type DELETE to confirm")
                delete_submit_ai = st.form_submit_button("Delete Selected Profile")
            if delete_submit_ai:
                if not confirm_delete_ai or delete_phrase_ai.strip() != "DELETE":
                    st.error("Confirm deletion and type `DELETE` exactly.")
                else:
                    try:
                        repo.delete_ai_provider_config_by_id(config_id=selected_profile.id, actor=user.username)
                        st.success("AI runtime profile deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to delete profile: {exc}")

        st.divider()
        _render_voice_runtime_editor(repo, user)

        st.divider()
        _render_ai_domain_toggles_editor(repo, user)

        st.divider()
        st.markdown("### Comp AI Prompt Templates")
        st.caption(
            "Edit the prompt instruction and system message used by Comp Tool AI summaries. "
            "Changes apply immediately (runtime settings with env/default fallback)."
        )
        current_system_message_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="comp_llm_system_message",
            active_only=False,
        )
        current_instruction_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="comp_llm_instruction_template",
            active_only=False,
        )
        current_system_message = (
            (current_system_message_row.value if current_system_message_row else DEFAULT_COMP_SYSTEM_MESSAGE) or ""
        )
        current_instruction = (
            (current_instruction_row.value if current_instruction_row else DEFAULT_COMP_INSTRUCTION) or ""
        )

        with st.form("admin_comp_prompt_templates_form"):
            edited_system_message = st.text_area(
                "System Message",
                value=current_system_message,
                height=100,
            )
            edited_instruction = st.text_area(
                "Instruction Template",
                value=current_instruction,
                height=220,
            )
            csave1, csave2 = st.columns(2)
            with csave1:
                save_prompt_templates = st.form_submit_button("Save Prompt Templates")
            with csave2:
                reset_prompt_templates = st.form_submit_button("Reset to App Defaults")

        if save_prompt_templates:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_system_message",
                    value=(edited_system_message or "").strip() or DEFAULT_COMP_SYSTEM_MESSAGE,
                    value_type="str",
                    description="System message for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_instruction_template",
                    value=(edited_instruction or "").strip() or DEFAULT_COMP_INSTRUCTION,
                    value_type="str",
                    description="Instruction template for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Comp AI prompt templates saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save prompt templates: {exc}")

        if reset_prompt_templates:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_system_message",
                    value=DEFAULT_COMP_SYSTEM_MESSAGE,
                    value_type="str",
                    description="System message for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_instruction_template",
                    value=DEFAULT_COMP_INSTRUCTION,
                    value_type="str",
                    description="Instruction template for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Comp AI prompt templates reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to reset prompt templates: {exc}")

        st.divider()
        st.markdown("### Listing Wizard AI Prompt Templates")
        st.caption(
            "Edit the prompt instruction and system message used by Listing Wizard AI draft suggestions. "
            "Changes apply immediately (runtime settings with env/default fallback)."
        )
        wizard_system_message_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="listing_wizard_ai_system_message",
            active_only=False,
        )
        wizard_instruction_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="listing_wizard_ai_instruction_template",
            active_only=False,
        )
        wizard_seed_default_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="listing_wizard_ai_seed_default",
            active_only=False,
        )
        wizard_system_message = (
            (wizard_system_message_row.value if wizard_system_message_row else DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE)
            or ""
        )
        wizard_instruction = (
            (wizard_instruction_row.value if wizard_instruction_row else DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE)
            or ""
        )
        wizard_seed_default = (
            (wizard_seed_default_row.value if wizard_seed_default_row else DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT)
            or ""
        )

        with st.form("admin_listing_wizard_prompt_templates_form"):
            edited_wizard_system_message = st.text_area(
                "Listing Wizard System Message",
                value=wizard_system_message,
                height=130,
            )
            edited_wizard_seed_default = st.text_area(
                "Listing Wizard Seed Prompt (Default)",
                value=wizard_seed_default,
                height=120,
            )
            edited_wizard_instruction = st.text_area(
                "Listing Wizard Instruction Template",
                value=wizard_instruction,
                height=200,
            )
            wsave1, wsave2 = st.columns(2)
            with wsave1:
                save_wizard_prompt_templates = st.form_submit_button("Save Listing Wizard Prompts")
            with wsave2:
                reset_wizard_prompt_templates = st.form_submit_button("Reset Listing Wizard Prompts")

        if save_wizard_prompt_templates:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="listing_wizard_ai_system_message",
                    value=(edited_wizard_system_message or "").strip() or DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE,
                    value_type="str",
                    description="System message for Listing Wizard AI draft suggestions.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="listing_wizard_ai_instruction_template",
                    value=(edited_wizard_instruction or "").strip() or DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE,
                    value_type="str",
                    description="Instruction template for Listing Wizard AI draft suggestions.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="listing_wizard_ai_seed_default",
                    value=(edited_wizard_seed_default or "").strip() or DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT,
                    value_type="str",
                    description="Default seed prompt pre-filled in Listing Wizard AI Draft Assist.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Listing Wizard AI prompt templates saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save Listing Wizard prompts: {exc}")

        if reset_wizard_prompt_templates:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="listing_wizard_ai_system_message",
                    value=DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE,
                    value_type="str",
                    description="System message for Listing Wizard AI draft suggestions.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="listing_wizard_ai_instruction_template",
                    value=DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE,
                    value_type="str",
                    description="Instruction template for Listing Wizard AI draft suggestions.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="listing_wizard_ai_seed_default",
                    value=DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT,
                    value_type="str",
                    description="Default seed prompt pre-filled in Listing Wizard AI Draft Assist.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Listing Wizard AI prompt templates reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to reset Listing Wizard prompts: {exc}")

        st.divider()
        st.markdown("### AI Prompt Version Registry + Rollback")
        st.caption(
            "Capture versioned prompt snapshots for `comp` and `listing` workflows, restore prior versions, "
            "and pin the active version id used for telemetry."
        )
        registry_workflow = st.selectbox(
            "Prompt Workflow",
            options=["listing", "comp"],
            index=0,
            key="admin_ai_prompt_registry_workflow",
        )
        try:
            active_version_id = active_prompt_version(repo, registry_workflow)
            version_rows = list_prompt_versions(repo, registry_workflow, limit=120)
        except Exception:
            active_version_id = ""
            version_rows = []
        st.caption(
            f"Active version: `{active_version_id or 'none'}` | stored versions: `{len(version_rows)}`"
        )
        version_labels: list[str] = []
        version_lookup: dict[str, str] = {}
        for row in version_rows:
            vid = str(row.get("version_id") or "").strip()
            created_at = str(row.get("created_at") or "").strip()
            created_by = str(row.get("created_by") or "").strip()
            note = str(row.get("note") or "").strip()
            label = f"{vid} | {created_at} | {created_by}" + (f" | {note[:60]}" if note else "")
            version_labels.append(label)
            version_lookup[label] = vid
        selected_version_label = st.selectbox(
            "Stored Versions",
            options=["(none)", *version_labels],
            index=0,
            key="admin_ai_prompt_registry_selected_version",
        )
        selected_version_id = version_lookup.get(selected_version_label, "")
        note_key = f"admin_ai_prompt_registry_note_{registry_workflow}"
        version_note = st.text_input(
            "Snapshot Note (optional)",
            value=str(st.session_state.get(note_key) or "").strip(),
            key=note_key,
            placeholder="e.g. tuned for bullion listings before weekend batch",
        )
        pr1, pr2, pr3 = st.columns(3)
        with pr1:
            if st.button("Save Current as New Version", key="admin_ai_prompt_registry_save_btn"):
                try:
                    created = create_prompt_version(
                        repo,
                        registry_workflow,
                        actor=user.username,
                        note=version_note,
                        set_active=True,
                    )
                    st.success(f"Saved prompt version `{created.get('version_id')}` and set active.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save prompt version: {exc}")
        with pr2:
            if st.button("Restore Selected Version", key="admin_ai_prompt_registry_restore_btn"):
                if not selected_version_id:
                    st.warning("Select a stored version first.")
                else:
                    try:
                        restored = restore_prompt_version(
                            repo,
                            registry_workflow,
                            version_id=selected_version_id,
                            actor=user.username,
                            set_active=True,
                        )
                        if restored is None:
                            st.warning("Selected prompt version was not found.")
                        else:
                            st.success(f"Restored prompt version `{selected_version_id}`.")
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to restore prompt version: {exc}")
        with pr3:
            if st.button("Set Selected Active (No Restore)", key="admin_ai_prompt_registry_set_active_btn"):
                if not selected_version_id:
                    st.warning("Select a stored version first.")
                else:
                    try:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=f"ai_prompt_active_version_{registry_workflow}",
                            value=selected_version_id,
                            value_type="str",
                            description=f"Active prompt registry version id for `{registry_workflow}` workflow.",
                            is_active=True,
                            actor=user.username,
                        )
                        st.success(f"Active version set to `{selected_version_id}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to set active prompt version: {exc}")
        if version_rows:
            preview_rows: list[dict[str, str]] = []
            for row in version_rows[:30]:
                prompt_values = row.get("prompt_values") if isinstance(row.get("prompt_values"), dict) else {}
                preview_rows.append(
                    {
                        "version_id": str(row.get("version_id") or ""),
                        "created_at": str(row.get("created_at") or ""),
                        "created_by": str(row.get("created_by") or ""),
                        "note": str(row.get("note") or "")[:140],
                        "keys": ", ".join(sorted(str(k) for k in prompt_values.keys()))[:180],
                    }
                )
            st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### AI Quality Metrics")
        st.caption(
            "Operational telemetry for AI suggestion acceptance/edit outcomes. "
            "Use this to monitor prompt quality and version effectiveness."
        )
        am1, am2 = st.columns(2)
        with am1:
            lookback_days = int(
                st.number_input(
                    "Lookback Days",
                    min_value=1,
                    max_value=180,
                    value=14,
                    step=1,
                    key="admin_ai_metrics_lookback_days",
                )
            )
        with am2:
            workflow_filter = st.selectbox(
                "Workflow Filter",
                options=["all", "listing_wizard", "listing", "comp", "intake"],
                index=0,
                key="admin_ai_metrics_workflow_filter",
            )
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)
        ai_metric_rows: list[tuple] = []
        try:
            ai_metric_rows = repo.db.execute(
                text(
                    """
                    SELECT created_at, actor, action, changes_json
                    FROM audit_logs
                    WHERE entity_type = 'ai_prompt_acceptance'
                      AND created_at >= :since
                    ORDER BY created_at DESC
                    LIMIT 5000
                    """
                ),
                {"since": since},
            ).all()
        except Exception as exc:
            try:
                repo.db.rollback()
            except Exception:
                pass
            st.warning(f"AI quality metrics query failed; recovered DB session. {exc}")
            ai_metric_rows = []

        metrics_summary = _summarize_ai_quality_metrics(
            ai_metric_rows,
            workflow_filter=workflow_filter,
        )
        apply_events = int(metrics_summary.get("apply_events") or 0)
        outcome_events = int(metrics_summary.get("outcome_events") or 0)
        accepted_as_is_count = int(metrics_summary.get("accepted_as_is_count") or 0)
        edited_count = int(metrics_summary.get("edited_count") or 0)
        workflow_totals = metrics_summary.get("workflow_totals") or {}
        version_totals = metrics_summary.get("version_totals") or {}
        daily_rows = metrics_summary.get("daily_rows") or []
        workflow_daily_rows = metrics_summary.get("workflow_daily_rows") or []
        recent_rows = metrics_summary.get("recent_rows") or []
        edited_fields_top_rows = metrics_summary.get("edited_fields_top_rows") or []

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("AI Apply Events", int(apply_events))
        m2.metric("AI Outcome Events", int(outcome_events))
        m3.metric("Accepted As-Is", int(accepted_as_is_count))
        accept_rate = (accepted_as_is_count / outcome_events * 100.0) if outcome_events else 0.0
        m4.metric("Acceptance Rate", f"{accept_rate:,.1f}%")
        if outcome_events:
            m3.caption(f"Edited: {int(edited_count)}")

        if daily_rows:
            daily_df = pd.DataFrame(daily_rows)
            st.markdown("#### Acceptance Trend (Daily)")
            st.line_chart(
                daily_df.set_index("date")[["accept_rate_pct"]],
                height=220,
                use_container_width=True,
            )
            st.dataframe(daily_df, use_container_width=True, hide_index=True)

        if workflow_totals:
            wf_df = pd.DataFrame(
                [
                    {
                        "workflow": key,
                        "total": int(values["total"]),
                        "accepted_as_is": int(values["accepted_as_is"]),
                        "edited": int(values["edited"]),
                        "accept_rate_pct": round(
                            (float(values["accepted_as_is"]) / float(values["total"]) * 100.0)
                            if values["total"]
                            else 0.0,
                            2,
                        ),
                    }
                    for key, values in workflow_totals.items()
                ]
            ).sort_values(by=["total", "accept_rate_pct"], ascending=[False, False])
            st.markdown("#### Outcome by Workflow")
            st.dataframe(wf_df, use_container_width=True, hide_index=True)
            if workflow_daily_rows:
                st.markdown("#### Workflow Daily Drilldown")
                workflow_daily_df = pd.DataFrame(workflow_daily_rows)
                workflow_options = sorted(
                    {
                        str(row.get("workflow") or "").strip()
                        for row in workflow_daily_rows
                        if str(row.get("workflow") or "").strip()
                    }
                )
                if workflow_options:
                    drilldown_workflow = st.selectbox(
                        "Workflow Drilldown",
                        options=workflow_options,
                        index=0,
                        key="admin_ai_metrics_workflow_drilldown",
                    )
                    drilldown_df = workflow_daily_df[
                        workflow_daily_df["workflow"] == str(drilldown_workflow or "").strip()
                    ].sort_values(by=["date"], ascending=True, kind="stable")
                    st.line_chart(
                        drilldown_df.set_index("date")[["accept_rate_pct"]],
                        height=220,
                        use_container_width=True,
                    )
                    st.dataframe(drilldown_df, use_container_width=True, hide_index=True)

                    if edited_fields_top_rows:
                        edited_fields_df = pd.DataFrame(edited_fields_top_rows)
                        edited_fields_df = edited_fields_df[
                            edited_fields_df["workflow"] == str(drilldown_workflow or "").strip()
                        ].sort_values(by=["edit_count", "field"], ascending=[False, True], kind="stable")
                        if not edited_fields_df.empty:
                            st.markdown("#### Top Edited Fields (Workflow)")
                            st.dataframe(
                                edited_fields_df.head(30),
                                use_container_width=True,
                                hide_index=True,
                            )

        if version_totals:
            ver_df = pd.DataFrame(
                [
                    {
                        "prompt_version_id": key,
                        "total": int(values["total"]),
                        "accepted_as_is": int(values["accepted_as_is"]),
                        "edited": int(values["edited"]),
                        "accept_rate_pct": round(
                            (float(values["accepted_as_is"]) / float(values["total"]) * 100.0)
                            if values["total"]
                            else 0.0,
                            2,
                        ),
                    }
                    for key, values in version_totals.items()
                ]
            ).sort_values(by=["total", "accept_rate_pct"], ascending=[False, False])
            st.markdown("#### Outcome by Prompt Version")
            st.dataframe(ver_df.head(40), use_container_width=True, hide_index=True)

        if recent_rows:
            recent_df = pd.DataFrame(recent_rows[:80])
            st.markdown("#### Recent AI Outcomes")
            st.dataframe(recent_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download AI Quality Metrics CSV",
                data=recent_df.to_csv(index=False),
                file_name=f"ai_quality_metrics_{settings.app_env}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv",
                mime="text/csv",
                key="admin_ai_quality_metrics_csv_download",
            )
        else:
            st.caption("No AI outcome telemetry rows found for current filters.")

        st.divider()
        st.markdown("### Workflow AI Profile Routing")
        st.caption(
            "Optionally pin specific workflows to a preferred AI Runtime profile. "
            "Leave blank to use the default/fallback chain ordering."
        )
        profile_rows = repo.list_ai_provider_configs(environment=settings.app_env, active_only=True)
        profile_choices = [{"label": "Default chain order (no workflow override)", "value": ""}]
        for row in profile_rows:
            profile_choices.append(
                {
                    "label": (
                        f"#{int(row.id)} | {str(row.name or '').strip()} "
                        f"({str(row.provider or '').strip().lower()} / {str(row.model or '').strip()})"
                    ),
                    "value": str(int(row.id)),
                }
            )

        def _workflow_profile_current(key: str) -> str:
            row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=False)
            return str(row.value if row is not None else "").strip()

        def _workflow_profile_index(current: str) -> int:
            for idx, choice in enumerate(profile_choices):
                if str(choice.get("value") or "") == str(current or ""):
                    return idx
            return 0

        current_listing_profile = _workflow_profile_current("ai_workflow_profile_listing")
        current_intake_profile = _workflow_profile_current("ai_workflow_profile_intake")
        current_comp_profile = _workflow_profile_current("ai_workflow_profile_comp")
        current_risk_profile = _workflow_profile_current("ai_workflow_profile_risk")
        profile_labels = [str(choice["label"]) for choice in profile_choices]

        with st.form("admin_ai_workflow_profile_routing_form"):
            wf1, wf2 = st.columns(2)
            with wf1:
                listing_label = st.selectbox(
                    "Listing Workflow Profile",
                    options=profile_labels,
                    index=_workflow_profile_index(current_listing_profile),
                    key="admin_ai_workflow_profile_listing",
                )
                comp_label = st.selectbox(
                    "Comp Workflow Profile",
                    options=profile_labels,
                    index=_workflow_profile_index(current_comp_profile),
                    key="admin_ai_workflow_profile_comp",
                )
            with wf2:
                intake_label = st.selectbox(
                    "Intake Workflow Profile",
                    options=profile_labels,
                    index=_workflow_profile_index(current_intake_profile),
                    key="admin_ai_workflow_profile_intake",
                )
                risk_label = st.selectbox(
                    "Risk Workflow Profile",
                    options=profile_labels,
                    index=_workflow_profile_index(current_risk_profile),
                    key="admin_ai_workflow_profile_risk",
                )
            sr1, sr2 = st.columns(2)
            with sr1:
                save_workflow_routing = st.form_submit_button("Save Workflow Profile Routing")
            with sr2:
                clear_workflow_routing = st.form_submit_button("Clear Workflow Profile Routing")

        if save_workflow_routing:
            try:
                label_to_value = {str(choice["label"]): str(choice["value"]) for choice in profile_choices}
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ai_workflow_profile_listing",
                    value=label_to_value.get(str(listing_label), ""),
                    value_type="str",
                    description="Preferred AI runtime profile id for listing workflow calls (blank uses default chain order).",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ai_workflow_profile_intake",
                    value=label_to_value.get(str(intake_label), ""),
                    value_type="str",
                    description="Preferred AI runtime profile id for intake workflow calls (blank uses default chain order).",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ai_workflow_profile_comp",
                    value=label_to_value.get(str(comp_label), ""),
                    value_type="str",
                    description="Preferred AI runtime profile id for comp workflow calls (blank uses default chain order).",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ai_workflow_profile_risk",
                    value=label_to_value.get(str(risk_label), ""),
                    value_type="str",
                    description="Preferred AI runtime profile id for risk workflow calls (blank uses default chain order).",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Workflow AI profile routing saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save workflow AI profile routing: {exc}")

        if clear_workflow_routing:
            try:
                for runtime_key, runtime_desc in [
                    (
                        "ai_workflow_profile_listing",
                        "Preferred AI runtime profile id for listing workflow calls (blank uses default chain order).",
                    ),
                    (
                        "ai_workflow_profile_intake",
                        "Preferred AI runtime profile id for intake workflow calls (blank uses default chain order).",
                    ),
                    (
                        "ai_workflow_profile_comp",
                        "Preferred AI runtime profile id for comp workflow calls (blank uses default chain order).",
                    ),
                    (
                        "ai_workflow_profile_risk",
                        "Preferred AI runtime profile id for risk workflow calls (blank uses default chain order).",
                    ),
                ]:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=runtime_key,
                        value="",
                        value_type="str",
                        description=runtime_desc,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Workflow AI profile routing cleared.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to clear workflow AI profile routing: {exc}")

        st.divider()
        st.markdown("### AI Apply-Time Quality Gates")
        st.caption(
            "Control minimum AI output quality thresholds and policy-blocked phrases used before applying "
            "AI suggestions in listing and intake workflows."
        )
        ai_quality_defaults = {
            "title_min_words": 3,
            "title_min_chars": 12,
            "details_min_words": 28,
            "details_min_chars": 180,
            "intake_min_words": 8,
            "intake_min_chars": 40,
            "forbidden_terms_csv": "guaranteed profit,guaranteed return,risk-free,no risk,investment advice,financial advice",
        }
        quality_title_min_words = int(
            get_runtime_int(repo, "ai_quality_title_min_words", ai_quality_defaults["title_min_words"])
        )
        quality_title_min_chars = int(
            get_runtime_int(repo, "ai_quality_title_min_chars", ai_quality_defaults["title_min_chars"])
        )
        quality_details_min_words = int(
            get_runtime_int(
                repo,
                "ai_quality_listing_details_min_words",
                ai_quality_defaults["details_min_words"],
            )
        )
        quality_details_min_chars = int(
            get_runtime_int(
                repo,
                "ai_quality_listing_details_min_chars",
                ai_quality_defaults["details_min_chars"],
            )
        )
        quality_intake_min_words = int(
            get_runtime_int(repo, "ai_quality_intake_min_words", ai_quality_defaults["intake_min_words"])
        )
        quality_intake_min_chars = int(
            get_runtime_int(repo, "ai_quality_intake_min_chars", ai_quality_defaults["intake_min_chars"])
        )
        quality_forbidden_terms = str(
            get_runtime_str(
                repo,
                "ai_quality_forbidden_terms_csv",
                ai_quality_defaults["forbidden_terms_csv"],
            )
            or ""
        ).strip()
        with st.form("admin_ai_quality_gate_form"):
            q1, q2 = st.columns(2)
            with q1:
                input_title_min_words = st.number_input(
                    "Listing title min words",
                    min_value=1,
                    max_value=20,
                    step=1,
                    value=quality_title_min_words,
                    key="admin_ai_quality_title_min_words",
                )
                input_title_min_chars = st.number_input(
                    "Listing title min chars",
                    min_value=5,
                    max_value=120,
                    step=1,
                    value=quality_title_min_chars,
                    key="admin_ai_quality_title_min_chars",
                )
                input_details_min_words = st.number_input(
                    "Listing details min words",
                    min_value=10,
                    max_value=400,
                    step=1,
                    value=quality_details_min_words,
                    key="admin_ai_quality_details_min_words",
                )
            with q2:
                input_details_min_chars = st.number_input(
                    "Listing details min chars",
                    min_value=50,
                    max_value=8000,
                    step=10,
                    value=quality_details_min_chars,
                    key="admin_ai_quality_details_min_chars",
                )
                input_intake_min_words = st.number_input(
                    "Intake text min words",
                    min_value=3,
                    max_value=200,
                    step=1,
                    value=quality_intake_min_words,
                    key="admin_ai_quality_intake_min_words",
                )
                input_intake_min_chars = st.number_input(
                    "Intake text min chars",
                    min_value=20,
                    max_value=4000,
                    step=10,
                    value=quality_intake_min_chars,
                    key="admin_ai_quality_intake_min_chars",
                )
            input_forbidden_terms = st.text_area(
                "Policy blocked terms/phrases (comma/newline separated)",
                value=quality_forbidden_terms,
                key="admin_ai_quality_forbidden_terms",
                help="If AI suggestions contain these terms, they are blocked from auto-apply.",
            )
            qsave1, qsave2 = st.columns(2)
            with qsave1:
                save_quality_gates = st.form_submit_button("Save AI Quality Gates")
            with qsave2:
                reset_quality_gates = st.form_submit_button("Reset AI Quality Gates to Defaults")

        if save_quality_gates:
            try:
                quality_rows = [
                    ("ai_quality_title_min_words", str(int(input_title_min_words or 3)), "int"),
                    ("ai_quality_title_min_chars", str(int(input_title_min_chars or 12)), "int"),
                    ("ai_quality_listing_details_min_words", str(int(input_details_min_words or 28)), "int"),
                    ("ai_quality_listing_details_min_chars", str(int(input_details_min_chars or 180)), "int"),
                    ("ai_quality_intake_min_words", str(int(input_intake_min_words or 8)), "int"),
                    ("ai_quality_intake_min_chars", str(int(input_intake_min_chars or 40)), "int"),
                    ("ai_quality_forbidden_terms_csv", str(input_forbidden_terms or "").strip(), "str"),
                ]
                for key, value, value_type in quality_rows:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description="AI apply-time quality gate setting.",
                        is_active=True,
                        actor=user.username,
                    )
                st.success("AI quality gates saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save AI quality gates: {exc}")

        if reset_quality_gates:
            try:
                default_rows = [
                    ("ai_quality_title_min_words", str(ai_quality_defaults["title_min_words"]), "int"),
                    ("ai_quality_title_min_chars", str(ai_quality_defaults["title_min_chars"]), "int"),
                    ("ai_quality_listing_details_min_words", str(ai_quality_defaults["details_min_words"]), "int"),
                    ("ai_quality_listing_details_min_chars", str(ai_quality_defaults["details_min_chars"]), "int"),
                    ("ai_quality_intake_min_words", str(ai_quality_defaults["intake_min_words"]), "int"),
                    ("ai_quality_intake_min_chars", str(ai_quality_defaults["intake_min_chars"]), "int"),
                    ("ai_quality_forbidden_terms_csv", str(ai_quality_defaults["forbidden_terms_csv"]), "str"),
                ]
                for key, value, value_type in default_rows:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description="AI apply-time quality gate setting.",
                        is_active=True,
                        actor=user.username,
                    )
                st.success("AI quality gates reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to reset AI quality gates: {exc}")

        st.divider()
        st.markdown("### Ask GoldenStackers AI Refinement")
        st.caption(
            "Optional post-processing pass for chat answers using AI orchestration fallback profiles. "
            "Read-only chat guardrails still apply."
        )
        chat_refine_enabled_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="chat_ai_refine_enabled",
            active_only=False,
        )
        chat_refine_system_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="chat_ai_refine_system_message",
            active_only=False,
        )
        chat_refine_instruction_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="chat_ai_refine_instruction",
            active_only=False,
        )
        chat_refine_enabled_default = (
            str(chat_refine_enabled_row.value if chat_refine_enabled_row is not None else "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on", "y"}
        )
        chat_refine_system_default = (
            (chat_refine_system_row.value if chat_refine_system_row is not None else "")
            or (
                "You are GoldenStackers' read-only operations copilot. "
                "Preserve factual values from the provided draft answer and citations."
            )
        )
        chat_refine_instruction_default = (
            (chat_refine_instruction_row.value if chat_refine_instruction_row is not None else "")
            or (
                "Rewrite the draft answer for clarity and operator usefulness. "
                "Do not invent values. Keep output concise markdown with short bullets."
            )
        )
        with st.form("admin_chat_refine_form"):
            edited_chat_refine_enabled = st.checkbox(
                "Enable AI refinement for Ask GoldenStackers",
                value=bool(chat_refine_enabled_default),
            )
            edited_chat_refine_system = st.text_area(
                "Chat Refine System Message",
                value=str(chat_refine_system_default),
                height=110,
            )
            edited_chat_refine_instruction = st.text_area(
                "Chat Refine Instruction Template",
                value=str(chat_refine_instruction_default),
                height=200,
            )
            rc1, rc2 = st.columns(2)
            with rc1:
                save_chat_refine = st.form_submit_button("Save Chat Refinement Settings")
            with rc2:
                reset_chat_refine = st.form_submit_button("Reset Chat Refinement Defaults")

        if save_chat_refine:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_enabled",
                    value="true" if edited_chat_refine_enabled else "false",
                    value_type="bool",
                    description="Enable/disable orchestration-backed AI refinement pass for Ask GoldenStackers responses.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_system_message",
                    value=(edited_chat_refine_system or "").strip()
                    or (
                        "You are GoldenStackers' read-only operations copilot. "
                        "Preserve factual values from the provided draft answer and citations."
                    ),
                    value_type="str",
                    description="System message used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_instruction",
                    value=(edited_chat_refine_instruction or "").strip()
                    or (
                        "Rewrite the draft answer for clarity and operator usefulness. "
                        "Do not invent values. Keep output concise markdown with short bullets."
                    ),
                    value_type="str",
                    description="Instruction template used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Chat AI refinement settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save chat refinement settings: {exc}")

        if reset_chat_refine:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_enabled",
                    value="false",
                    value_type="bool",
                    description="Enable/disable orchestration-backed AI refinement pass for Ask GoldenStackers responses.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_system_message",
                    value=(
                        "You are GoldenStackers' read-only operations copilot. "
                        "Preserve factual values from the provided draft answer and citations."
                    ),
                    value_type="str",
                    description="System message used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_instruction",
                    value=(
                        "Rewrite the draft answer for clarity and operator usefulness. "
                        "Do not invent values. Keep output concise markdown with short bullets."
                    ),
                    value_type="str",
                    description="Instruction template used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Chat AI refinement settings reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to reset chat refinement settings: {exc}")

        st.divider()
        st.markdown("### Web-Fetched Grading/Comp Standards")
        st.caption(
            "Fetch public grading-reference signals (PCGS/NGC/ANACS/ICG pages) and store generated context "
            "into runtime settings. Values remain fully editable after save."
        )
        standards_snapshot = fetch_standards_snapshot()
        snapshot_sources = standards_snapshot.get("sources") or {}
        source_rows = []
        for key, payload in snapshot_sources.items():
            source_rows.append(
                {
                    "source": key,
                    "reachable": bool(payload.get("ok")),
                    "url": str(payload.get("url") or ""),
                }
            )
        if source_rows:
            st.dataframe(pd.DataFrame(source_rows), use_container_width=True)
        st.caption(f"Last standards snapshot UTC: `{str(standards_snapshot.get('checked_at_utc') or '')}`")
        with st.expander("Preview Generated Context From Current Snapshot", expanded=False):
            try:
                st.markdown("**Research Baseline (Curated, editable)**")
                st.text_area(
                    "Curated comp baseline",
                    value=CURATED_COMP_BASELINE,
                    height=180,
                    disabled=True,
                    key="admin_comp_rules_curated_baseline_preview",
                )
                st.text_area(
                    "Curated grading baseline",
                    value=CURATED_GRADING_BASELINE,
                    height=220,
                    disabled=True,
                    key="admin_grading_rules_curated_baseline_preview",
                )
                preview_comp = build_comp_rules_context_from_web()
                preview_grade = build_coin_grading_rules_context_from_web()
                st.markdown("**Comp Rules Context Preview**")
                st.text_area(
                    "comp_reference_rules_context (preview)",
                    value=preview_comp,
                    height=220,
                    disabled=True,
                    key="admin_comp_rules_context_preview",
                )
                st.markdown("**Coin Grading Rules Context Preview**")
                st.text_area(
                    "coin_grading_rules_context (preview)",
                    value=preview_grade,
                    height=260,
                    disabled=True,
                    key="admin_coin_grading_rules_context_preview",
                )
            except Exception as exc:
                st.error(f"Unable to build standards preview: {exc}")
        sfc1, sfc2 = st.columns([1, 2])
        with sfc1:
            refresh_snapshot = st.button(
                "Refresh Web Snapshot",
                key="admin_refresh_grading_web_snapshot",
            )
        with sfc2:
            apply_web_defaults = st.button(
                "Apply Web-Fetched Standards To Runtime",
                key="admin_apply_web_fetched_standards",
                use_container_width=True,
            )
        if refresh_snapshot:
            try:
                clear_standards_snapshot_cache()
                _ = fetch_standards_snapshot()
                st.success("Standards web snapshot refreshed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to refresh standards snapshot: {exc}")
        if apply_web_defaults:
            try:
                clear_standards_snapshot_cache()
                comp_context = build_comp_rules_context_from_web()
                grading_context = build_coin_grading_rules_context_from_web()
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_reference_rules_context",
                    value=str(comp_context or "").strip(),
                    value_type="str",
                    description="Supplemental grading/comps rule context appended to comp prompts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="coin_grading_rules_context",
                    value=str(grading_context or "").strip(),
                    value_type="str",
                    description="Supplemental grading standards context appended to grader prompts.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Saved web-fetched standards context into runtime settings.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to apply web-fetched standards: {exc}")

        st.divider()
        st.markdown("### Prompt Template Versioning & Rollback")
        st.caption(
            "Review prompt/system-template change history from audit logs and rollback a template key to a prior value."
        )
        template_key_options = [
            "comp_llm_system_message",
            "comp_llm_instruction_template",
            "comp_reference_rules_context",
            "coin_grader_system_message",
            "coin_grader_instruction_template",
            "coin_grading_rules_context",
            "coin_identifier_system_message",
            "coin_identifier_instruction_template",
            "chat_ai_refine_system_message",
            "chat_ai_refine_instruction",
        ]
        selected_template_key = st.selectbox(
            "Template Key",
            options=template_key_options,
            key="admin_prompt_versioning_key",
        )
        template_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key=selected_template_key,
            active_only=False,
        )
        if template_row is None:
            st.info("No runtime setting row exists yet for this template key in current environment.")
            current_value = ""
            current_description = "Prompt/system template runtime value."
            current_type = "str"
            history_rows: list[dict] = []
        else:
            current_value = str(template_row.value or "")
            current_description = str(template_row.description or "Prompt/system template runtime value.")
            current_type = str(template_row.value_type or "str")
            history_rows = _runtime_setting_audit_history(repo, int(template_row.id))

        st.caption(f"Current value length: `{len(current_value)}` chars")
        if history_rows:
            history_df = pd.DataFrame(
                [
                    {
                        "audit_id": row["audit_id"],
                        "created_at": row["created_at"],
                        "actor": row["actor"],
                        "action": row["action"],
                        "value_before_preview": (row["value_before"] or "")[:120],
                        "value_after_preview": (row["value_after"] or "")[:120],
                    }
                    for row in history_rows
                ]
            )
            st.dataframe(history_df, use_container_width=True)

            history_map = {
                (
                    f"#{row['audit_id']} | {row['created_at']} | {row['actor']} | {row['action']} | "
                    f"before_len={len(row.get('value_before') or '')} | after_len={len(row.get('value_after') or '')}"
                ): row
                for row in history_rows
            }
            selected_version_key = st.selectbox(
                "Select Version Event",
                options=list(history_map.keys()),
                key="admin_prompt_version_event_select",
            )
            selected_version = history_map[selected_version_key]
            before_value = str(selected_version.get("value_before") or "")
            after_value = str(selected_version.get("value_after") or "")

            v1, v2 = st.columns(2)
            with v1:
                st.text_area(
                    "Selected Before Value",
                    value=before_value,
                    height=180,
                    disabled=True,
                    key="admin_prompt_selected_before_preview",
                )
            with v2:
                st.text_area(
                    "Selected After Value",
                    value=after_value,
                    height=180,
                    disabled=True,
                    key="admin_prompt_selected_after_preview",
                )

            rb1, rb2 = st.columns(2)
            with rb1:
                rollback_before = st.button("Rollback To Selected Before Value", key="admin_prompt_rollback_before")
            with rb2:
                rollback_after = st.button("Rollback To Selected After Value", key="admin_prompt_rollback_after")

            if rollback_before or rollback_after:
                target_value = before_value if rollback_before else after_value
                if not target_value:
                    st.error("Selected rollback target value is empty for this event.")
                else:
                    try:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=selected_template_key,
                            value=target_value,
                            value_type=current_type,
                            description=current_description,
                            is_active=True,
                            actor=user.username,
                        )
                        st.success(f"Rolled back `{selected_template_key}` successfully.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Rollback failed: {exc}")
        else:
            st.info("No audit history available yet for this template key.")

        st.divider()
        st.markdown("### AI Usage Telemetry")
        telemetry_window = st.selectbox(
            "Telemetry Window",
            options=["last_24h", "last_7d", "last_30d"],
            index=1,
            key="admin_ai_telemetry_window",
        )
        days_map = {"last_24h": 1, "last_7d": 7, "last_30d": 30}
        since_utc = utcnow_naive() - timedelta(days=int(days_map.get(telemetry_window, 7)))

        chat_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "ai_chat",
                AuditLog.created_at >= since_utc,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(5000)
        ).all()
        chat_rows: list[dict] = []
        for row in chat_logs:
            try:
                payload = json.loads(row.changes_json or "{}")
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            meta = after.get("metadata") if isinstance(after.get("metadata"), dict) else {}
            chat_rows.append(
                {
                    "created_at": row.created_at,
                    "actor": row.actor,
                    "intent": str(after.get("intent") or ""),
                    "elapsed_ms": int(after.get("elapsed_ms") or 0),
                    "denied": bool(after.get("denied")),
                    "input_mode": str(meta.get("input_mode") or ""),
                    "voice_provider": str(meta.get("voice_provider") or ""),
                    "voice_stt_model": str(meta.get("voice_stt_model") or ""),
                    "voice_tts_model": str(meta.get("voice_tts_model") or ""),
                    "ai_refined": bool(meta.get("ai_refined")),
                    "ai_refine_provider": str(meta.get("ai_refine_provider") or ""),
                    "ai_refine_text_model": str(meta.get("ai_refine_text_model") or ""),
                    "ai_refine_endpoint_type": str(meta.get("ai_refine_endpoint_type") or ""),
                }
            )

        chat_df = pd.DataFrame(chat_rows)
        chat_query_df = chat_df[chat_df["intent"] != "tts_playback_generated"] if not chat_df.empty else chat_df
        safe_failures = int((chat_query_df["intent"] == "safe_failure").sum()) if not chat_query_df.empty else 0
        denied_count = int(chat_query_df["denied"].sum()) if not chat_query_df.empty else 0
        avg_latency = float(chat_query_df["elapsed_ms"].mean()) if not chat_query_df.empty else 0.0
        p95_latency = (
            float(chat_query_df["elapsed_ms"].quantile(0.95))
            if not chat_query_df.empty and len(chat_query_df) >= 2
            else avg_latency
        )
        voice_prompt_count = int((chat_query_df["input_mode"] == "voice_stt").sum()) if not chat_query_df.empty else 0
        tts_event_count = int((chat_df["intent"] == "tts_playback_generated").sum()) if not chat_df.empty else 0
        refined_count = int(chat_query_df["ai_refined"].sum()) if not chat_query_df.empty else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("AI Chat Queries", f"{len(chat_query_df)}")
        m2.metric("Denied Queries", f"{denied_count}")
        m3.metric("Safe Failures", f"{safe_failures}")
        m4.metric("Voice STT Prompts", f"{voice_prompt_count}")
        n1, n2, n3, n4 = st.columns(4)
        n1.metric("Avg Latency (ms)", f"{avg_latency:,.0f}")
        n2.metric("P95 Latency (ms)", f"{p95_latency:,.0f}")
        n3.metric("TTS Events", f"{tts_event_count}")
        n4.metric("AI Refined", f"{refined_count}")

        if not chat_query_df.empty:
            intent_counts = (
                chat_query_df.groupby("intent", dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            user_counts = (
                chat_query_df.groupby("actor", dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            voice_provider_counts = (
                chat_df[chat_df["voice_provider"] != ""]
                .groupby("voice_provider", dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                if not chat_df.empty
                else pd.DataFrame(columns=["voice_provider", "count"])
            )
            refine_model_counts = (
                chat_query_df[chat_query_df["ai_refine_provider"] != ""]
                .groupby(["ai_refine_provider", "ai_refine_text_model", "ai_refine_endpoint_type"], dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                if not chat_query_df.empty
                else pd.DataFrame(columns=["ai_refine_provider", "ai_refine_text_model", "ai_refine_endpoint_type", "count"])
            )
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown("#### Top Intents")
                st.dataframe(intent_counts.head(20), use_container_width=True)
            with c2:
                st.markdown("#### Top Users")
                st.dataframe(user_counts.head(20), use_container_width=True)
            with c3:
                st.markdown("#### Voice Provider Usage")
                st.dataframe(voice_provider_counts.head(20), use_container_width=True)
            with c4:
                st.markdown("#### AI Refinement Usage")
                st.dataframe(refine_model_counts.head(20), use_container_width=True)
        else:
            st.info("No AI chat telemetry rows in selected window.")

        coin_runs = repo.db.scalars(
            select(CoinAIRun)
            .where(
                CoinAIRun.environment == settings.app_env,
                CoinAIRun.created_at >= since_utc,
            )
            .order_by(CoinAIRun.created_at.desc())
            .limit(5000)
        ).all()
        if coin_runs:
            coin_df = pd.DataFrame(
                [
                    {
                        "created_at": row.created_at,
                        "tool_name": row.tool_name,
                        "username": row.username,
                        "product_id": row.product_id,
                        "listing_id": row.listing_id,
                    }
                    for row in coin_runs
                ]
            )
            st.markdown("#### Coin AI Tool Usage")
            st.dataframe(
                coin_df.groupby("tool_name", dropna=False).size().reset_index(name="count").sort_values(
                    "count", ascending=False
                ),
                use_container_width=True,
            )
        else:
            st.info("No coin AI runs in selected window.")

        profile_rows = repo.db.scalars(
            select(AIProviderConfig).where(AIProviderConfig.environment == settings.app_env)
        ).all()
        if profile_rows:
            profile_df = pd.DataFrame(
                [
                    {
                        "provider": row.provider,
                        "model": row.model,
                        "multimodal_model": row.multimodal_model,
                        "is_default": bool(row.is_default),
                        "is_active": bool(row.is_active),
                    }
                    for row in profile_rows
                ]
            )
            st.markdown("#### Configured Provider/Model Profiles")
            st.dataframe(profile_df, use_container_width=True)

    with tab_env_config:
        env_file_mode = uses_env_file(settings.app_env)
        st.markdown("### Environment Variables")
        if env_file_mode:
            st.caption(
                "Local mode: view and update `.env` values from Admin. "
                "Edits apply to the file immediately, but process-level env values require container restart to take effect."
            )
        else:
            st.caption(
                "Cluster mode: showing process environment values from the running container. "
                "Edits must be done via Kubernetes Secrets/ConfigMaps and applied by rollout/restart."
            )
        env_path = ".env"
        recommended_env_defaults = read_env_file(".env.example")
        tracked_env_keys = set(recommended_env_defaults.keys())
        env_values = (
            read_env_file(env_path)
            if env_file_mode
            else read_process_env_values(tracked_keys=tracked_env_keys, include_untracked_editable=True)
        )
        env_coverage_rows = _build_env_coverage_rows(env_values, recommended_env_defaults)
        if env_coverage_rows:
            env_cov_df = pd.DataFrame(env_coverage_rows)
            st.markdown("### Config Coverage (Env)")
            req_env = required_env_keys()
            env_required_issue_df = env_cov_df[
                env_cov_df["key"].isin(req_env) & env_cov_df["status"].isin(["missing", "empty"])
            ]
            untracked_env_df = env_cov_df[
                (env_cov_df["present_in_env"] == True) & (env_cov_df["tracked"] == False)
            ]
            env_ok_count = int(env_cov_df["status"].isin(["default", "set"]).sum())
            env_total_count = max(1, int(len(env_cov_df)))
            env_health_ratio = env_ok_count / env_total_count
            env_health_label, env_health_color = _health_label_and_emoji(env_health_ratio)
            st.markdown(
                f"**Env Config Health:** :{env_health_color}[{env_health_label.upper()}] "
                f"(`{env_ok_count}/{env_total_count}` = `{env_health_ratio * 100:.1f}%`)"
            )
            if not env_required_issue_df.empty:
                st.error(
                    "Config Health Warning: required env keys are missing/empty. "
                    "Fix these before relying on the environment."
                )
                if st.button(
                    "Auto-Fix Required Env Keys From .env.example",
                    key="admin_env_autofix_required_btn",
                    disabled=not env_file_mode,
                ):
                    try:
                        fixed = _apply_required_env_defaults(
                            env_path=env_path,
                            required_keys=req_env,
                            env_values=env_values,
                            recommended_defaults=recommended_env_defaults,
                        )
                        if fixed:
                            st.success(f"Auto-fixed {fixed} required env key(s).")
                        else:
                            st.info("No required env keys were auto-fixed.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to auto-fix required env keys: {exc}")
                st.dataframe(
                    env_required_issue_df[["key", "status", "current_value", "recommended_default"]],
                    use_container_width=True,
                )
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Tracked Keys", f"{len(env_cov_df)}")
            c2.metric("Missing", f"{int((env_cov_df['status'] == 'missing').sum())}")
            c3.metric("Empty", f"{int((env_cov_df['status'] == 'empty').sum())}")
            c4.metric("Default/Set", f"{int((env_cov_df['status'].isin(['default', 'set'])).sum())}")
            c5, _ = st.columns(2)
            c5.metric("Untracked Env Keys", f"{int(len(untracked_env_df))}")
            missing_or_empty_env_count = int(env_cov_df["status"].isin(["missing", "empty"]).sum())
            if st.button(
                "Apply Missing + Empty Env Defaults Now",
                key="admin_env_apply_all_defaults_btn",
                disabled=(missing_or_empty_env_count == 0 or not env_file_mode),
            ):
                try:
                    fixed = _apply_all_env_defaults(
                        env_path=env_path,
                        env_values=env_values,
                        recommended_defaults=recommended_env_defaults,
                    )
                    if fixed:
                        st.success(f"Applied {fixed} env default value(s).")
                    else:
                        st.info("No env defaults were applied.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply env defaults: {exc}")
            st.download_button(
                "Download Env Coverage CSV",
                data=env_cov_df.to_csv(index=False).encode("utf-8"),
                file_name=f"env_coverage_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_env_coverage_download_csv",
            )
            st.dataframe(
                env_cov_df[
                    [
                        "key",
                        "status",
                        "tracked",
                        "present_in_env",
                        "editable",
                        "is_sensitive",
                        "current_value",
                        "recommended_default",
                    ]
                ],
                use_container_width=True,
            )
            st.caption("Feature-flag-like env keys (bool/toggle domains):")
            flag_df = env_cov_df[
                env_cov_df["key"].str.contains("ENABLED|FEATURE|TOGGLE|_REQUIRED|ALLOW|OVERRIDE", regex=True)
            ]
            if not flag_df.empty:
                st.dataframe(
                    flag_df[["key", "status", "current_value", "recommended_default"]],
                    use_container_width=True,
                )
            else:
                st.info("No feature-flag-like env keys detected in tracked set.")
            if not untracked_env_df.empty:
                source_label = "`.env`" if env_file_mode else "running process environment"
                st.caption(
                    f"Untracked env keys (present in {source_label}, not defined in `.env.example`):"
                )
                st.dataframe(
                    untracked_env_df[["key", "current_value", "editable", "is_sensitive"]],
                    use_container_width=True,
                )
        if not env_values:
            if env_file_mode:
                st.warning("`.env` file was not found or has no key/value pairs.")
            else:
                st.warning("No relevant process environment keys detected for tracked/editable domains.")
        else:
            env_rows = []
            for key in sorted(env_values.keys()):
                raw_value = env_values.get(key, "")
                env_rows.append(
                    {
                        "key": key,
                        "value": mask_env_value(key, raw_value),
                        "editable": bool(is_editable_env_key(key)),
                    }
                )
            st.dataframe(pd.DataFrame(env_rows), use_container_width=True)

        editable_keys = [k for k in sorted(env_values.keys()) if is_editable_env_key(k)]
        st.markdown("### Edit Environment Value")
        if not env_file_mode:
            st.info(
                "Editing env vars in Admin is disabled outside local mode. "
                "Use Kubernetes Secret/ConfigMap updates for Development/Production."
            )
        if not editable_keys:
            st.info("No editable keys detected.")
        elif env_file_mode:
            selected_key = st.selectbox("Key", options=editable_keys, key="admin_env_edit_key")
            existing_value = env_values.get(selected_key, "")
            with st.form("admin_env_edit_form"):
                new_value = st.text_input(
                    "Value",
                    value=existing_value,
                    type="password" if "SECRET" in selected_key or "TOKEN" in selected_key or "KEY" in selected_key else "default",
                )
                save_env_value = st.form_submit_button("Save to .env")
            if save_env_value:
                try:
                    upsert_env_key(env_path, selected_key, new_value)
                    st.success(f"Updated `{selected_key}` in `.env`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update `.env`: {exc}")

        if env_file_mode:
            st.markdown("### Add New `.env` Key")
            with st.form("admin_env_add_form"):
                add_key = st.text_input("New Key")
                add_value = st.text_input("New Value")
                add_submit = st.form_submit_button("Add Key")
            if add_submit:
                normalized = (add_key or "").strip().upper()
                if not normalized:
                    st.error("Key is required.")
                elif not is_editable_env_key(normalized):
                    st.error("This key prefix is not editable from Admin.")
                else:
                    try:
                        upsert_env_key(env_path, normalized, add_value)
                        st.success(f"Added `{normalized}` to `.env`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to add `.env` key: {exc}")

            st.markdown("### Sync Missing Recommended Keys")
            st.caption("Adds missing keys from `.env.example` without overwriting current values.")
            if st.button("Add Missing Recommended Keys", key="admin_env_sync_defaults_btn"):
                try:
                    added = ensure_env_defaults(env_path, recommended_env_defaults)
                    if added:
                        st.success(f"Added {len(added)} missing keys to `.env`.")
                    else:
                        st.info("`.env` already contains all keys from `.env.example`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to sync defaults: {exc}")

    with tab_runtime_settings:
        st.markdown("### Runtime Settings (DB)")
        st.caption(
            "These settings are environment-scoped and can override selected env-based defaults at runtime."
        )
        try:
            runtime_rows = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
        except Exception as exc:
            st.error(
                "Runtime settings table is not available yet. "
                "Run database migrations first (`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            runtime_rows = []

        runtime_seed_defaults = _runtime_setting_seed_defaults()
        runtime_cov_rows = _build_runtime_coverage_rows(runtime_rows, runtime_seed_defaults)
        if runtime_cov_rows:
            runtime_cov_df = pd.DataFrame(runtime_cov_rows)
            st.markdown("### Config Coverage (Runtime Settings)")
            req_runtime = required_runtime_keys()
            runtime_required_issue_df = runtime_cov_df[
                runtime_cov_df["key"].isin(req_runtime) & runtime_cov_df["status"].isin(["missing", "inactive"])
            ]
            runtime_ok_count = int(runtime_cov_df["status"].isin(["default", "overridden"]).sum())
            runtime_total_count = max(1, int(len(runtime_cov_df)))
            runtime_health_ratio = runtime_ok_count / runtime_total_count
            runtime_health_label, runtime_health_color = _health_label_and_emoji(runtime_health_ratio)
            st.markdown(
                f"**Runtime Config Health:** :{runtime_health_color}[{runtime_health_label.upper()}] "
                f"(`{runtime_ok_count}/{runtime_total_count}` = `{runtime_health_ratio * 100:.1f}%`)"
            )
            if not runtime_required_issue_df.empty:
                st.error(
                    "Config Health Warning: required runtime keys are missing/inactive. "
                    "Use `Apply All Missing Runtime Defaults Now` and activate required keys."
                )
                if st.button(
                    "Auto-Fix Required Runtime Keys",
                    key="admin_runtime_autofix_required_btn",
                ):
                    try:
                        fixed = _apply_required_runtime_defaults(
                            repo=repo,
                            actor=user.username,
                            required_keys=req_runtime,
                            runtime_rows=runtime_rows,
                            seed_defaults=runtime_seed_defaults,
                        )
                        if fixed:
                            st.success(f"Auto-fixed {fixed} required runtime key(s).")
                        else:
                            st.info("No required runtime keys were auto-fixed.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to auto-fix required runtime keys: {exc}")
                st.dataframe(
                    runtime_required_issue_df[["key", "status", "current_value", "expected_default", "is_active"]],
                    use_container_width=True,
                )
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Tracked Runtime Keys", f"{len(runtime_cov_df)}")
            r2.metric("Missing", f"{int((runtime_cov_df['status'] == 'missing').sum())}")
            r3.metric("Inactive", f"{int((runtime_cov_df['status'] == 'inactive').sum())}")
            r4.metric("Overridden", f"{int((runtime_cov_df['status'] == 'overridden').sum())}")
            r5, _ = st.columns(2)
            r5.metric("Custom Untracked", f"{int((runtime_cov_df['status'] == 'custom_untracked').sum())}")
            missing_runtime_count = int((runtime_cov_df["status"] == "missing").sum())
            inactive_runtime_count = int((runtime_cov_df["status"] == "inactive").sum())
            if st.button(
                "Apply Missing + Inactive Runtime Defaults Now",
                key="admin_runtime_apply_all_defaults_btn",
                disabled=((missing_runtime_count + inactive_runtime_count) == 0),
            ):
                try:
                    fixed = _apply_all_runtime_defaults(
                        repo=repo,
                        actor=user.username,
                        runtime_rows=runtime_rows,
                        seed_defaults=runtime_seed_defaults,
                    )
                    if fixed:
                        st.success(f"Applied {fixed} runtime default update(s).")
                    else:
                        st.info("No runtime defaults were applied.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply runtime defaults: {exc}")
            st.download_button(
                "Download Runtime Coverage CSV",
                data=runtime_cov_df.to_csv(index=False).encode("utf-8"),
                file_name=f"runtime_coverage_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_runtime_coverage_download_csv",
            )
            if st.button(
                "Apply All Missing Runtime Defaults Now",
                key="admin_runtime_apply_missing_defaults_btn",
                disabled=(missing_runtime_count == 0),
            ):
                seeded_now = _seed_missing_runtime_defaults(
                    repo,
                    actor=user.username,
                    seed_defaults=runtime_seed_defaults,
                )
                if seeded_now:
                    st.success(f"Applied {seeded_now} missing runtime default(s).")
                else:
                    st.info("No missing runtime defaults were applied.")
                st.rerun()
            st.dataframe(
                runtime_cov_df[
                    [
                        "key",
                        "status",
                        "expected_type",
                        "current_type",
                        "is_active",
                        "current_value",
                        "expected_default",
                        "updated_by",
                        "updated_at",
                    ]
                ],
                use_container_width=True,
            )
            st.caption("Feature-flag-like runtime keys (bool/toggle domains):")
            runtime_flag_df = runtime_cov_df[
                runtime_cov_df["key"].str.contains("enabled|feature|toggle|required|allow|override", case=False, regex=True)
            ]
            if not runtime_flag_df.empty:
                st.dataframe(
                    runtime_flag_df[
                        ["key", "status", "is_active", "current_value", "expected_default", "description"]
                    ],
                    use_container_width=True,
                )
            else:
                st.info("No feature-flag-like runtime keys detected in tracked set.")

        if runtime_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "environment": row.environment,
                            "key": row.key,
                            "value": row.value,
                            "value_type": row.value_type,
                            "is_active": bool(row.is_active),
                            "updated_by": row.updated_by,
                            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                        }
                        for row in runtime_rows
                    ]
                ),
                use_container_width=True,
            )
        else:
            st.info("No runtime settings found for this environment.")
        st.info("Dealer-domain parser settings now live in the `Comp Config` tab.")

        st.markdown("### Documents Handoff History (Team/Admin)")
        is_admin_user = str(user.role or "").strip().lower() == "admin"
        handoff_prefix = "documents_recent_handoffs_json__"
        handoff_setting_rows = [
            row for row in runtime_rows if str(row.key or "").strip().lower().startswith(handoff_prefix)
        ]
        if not handoff_setting_rows:
            st.caption("No persisted Documents handoff history keys found yet.")
        else:
            parsed_rows: list[dict] = []
            for setting_row in handoff_setting_rows:
                key_raw = str(setting_row.key or "").strip()
                username = key_raw[len(handoff_prefix) :].strip() if key_raw.lower().startswith(handoff_prefix) else ""
                try:
                    payload = json.loads(str(setting_row.value or "[]"))
                except Exception:
                    payload = []
                if not isinstance(payload, list):
                    continue
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    parsed_rows.append(
                        {
                            "username": username or "(unknown)",
                            "at": str(item.get("at") or ""),
                            "source_type": str(item.get("source_type") or ""),
                            "source_id": int(item.get("source_id") or 0),
                            "doc_type": str(item.get("doc_type") or "invoice"),
                            "handoff_from": str(item.get("handoff_from") or ""),
                            "setting_id": int(setting_row.id),
                            "setting_key": key_raw,
                        }
                    )
            if not is_admin_user:
                parsed_rows = [
                    row for row in parsed_rows if str(row.get("username") or "").strip().lower() == str(user.username).strip().lower()
                ]
            if not parsed_rows:
                st.caption("No handoff history rows available for your scope.")
            else:
                user_options = sorted({str(row.get("username") or "") for row in parsed_rows if str(row.get("username") or "").strip()})
                type_options = sorted({str(row.get("source_type") or "") for row in parsed_rows if str(row.get("source_type") or "").strip()})
                doc_options = sorted({str(row.get("doc_type") or "") for row in parsed_rows if str(row.get("doc_type") or "").strip()})
                h1, h2, h3 = st.columns(3)
                with h1:
                    if is_admin_user:
                        selected_users = st.multiselect(
                            "Filter User",
                            options=user_options,
                            default=[],
                            key="admin_documents_handoff_users_filter",
                        )
                    else:
                        selected_users = [str(user.username).strip().lower()]
                        st.text_input(
                            "Filter User",
                            value=str(user.username).strip().lower(),
                            disabled=True,
                            key="admin_documents_handoff_users_readonly",
                        )
                with h2:
                    selected_types = st.multiselect(
                        "Filter Source Type",
                        options=type_options,
                        default=[],
                        key="admin_documents_handoff_types_filter",
                    )
                with h3:
                    selected_doc_types = st.multiselect(
                        "Filter Doc Type",
                        options=doc_options,
                        default=[],
                        key="admin_documents_handoff_doc_types_filter",
                    )
                filtered_handoffs = []
                for row in parsed_rows:
                    if selected_users and str(row.get("username") or "") not in selected_users:
                        continue
                    if selected_types and str(row.get("source_type") or "") not in selected_types:
                        continue
                    if selected_doc_types and str(row.get("doc_type") or "") not in selected_doc_types:
                        continue
                    filtered_handoffs.append(row)
                filtered_handoffs = sorted(
                    filtered_handoffs,
                    key=lambda row: str(row.get("at") or ""),
                    reverse=True,
                )
                st.caption(f"Rows: {len(filtered_handoffs)}")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "username": row.get("username"),
                                "at": row.get("at"),
                                "source_type": row.get("source_type"),
                                "source_id": row.get("source_id"),
                                "doc_type": row.get("doc_type"),
                                "handoff_from": row.get("handoff_from"),
                            }
                            for row in filtered_handoffs
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
                if filtered_handoffs:
                    action_map = {
                        (
                            f"{row.get('at')} | {row.get('username')} | "
                            f"{row.get('source_type')} #{int(row.get('source_id') or 0)} | "
                            f"{row.get('doc_type')} | {row.get('handoff_from')}"
                        ): row
                        for row in filtered_handoffs[:200]
                    }
                    a1, a2, a3 = st.columns([3, 1, 1])
                    with a1:
                        selected_handoff_action_label = st.selectbox(
                            "Select Handoff Context",
                            options=list(action_map.keys()),
                            key="admin_documents_handoff_action_pick",
                        )
                    selected_handoff_action = action_map[selected_handoff_action_label]
                    reason_code_options = [
                        "user_request",
                        "privacy_cleanup",
                        "policy_enforcement",
                        "data_quality_reset",
                        "security_incident",
                        "other",
                    ]
                    r1, r2 = st.columns([1, 2])
                    with r1:
                        clear_reason_code = st.selectbox(
                            "Clear Reason Code",
                            options=reason_code_options,
                            index=0,
                            key="admin_documents_handoff_clear_reason_code",
                            help="Standardized reason classification for governance reporting.",
                        )
                    with r2:
                        clear_reason_note = st.text_input(
                            "Clear Reason Note (optional)",
                            value="",
                            key="admin_documents_handoff_clear_reason_note",
                            help="Optional supporting context. Required for `other`.",
                        ).strip()
                    with a2:
                        if st.button("Open in Documents", key="admin_documents_handoff_open_btn"):
                            handoff_to_documents_draft(
                                source_type=str(selected_handoff_action.get("source_type") or ""),
                                source_id=int(selected_handoff_action.get("source_id") or 0),
                                doc_type=str(selected_handoff_action.get("doc_type") or "invoice"),
                                handoff_from="admin_documents_handoffs",
                                repo=repo,
                                actor=user.username,
                            )
                    with a3:
                        clear_label = "Clear User History" if is_admin_user else "Clear My History"
                        if st.button(clear_label, key="admin_documents_handoff_clear_user_btn"):
                            username_to_clear = str(selected_handoff_action.get("username") or "").strip().lower()
                            if not username_to_clear:
                                st.error("Cannot determine username for selected row.")
                            elif not is_admin_user and username_to_clear != str(user.username).strip().lower():
                                st.error("You can only clear your own handoff history.")
                            elif is_admin_user and username_to_clear != str(user.username).strip().lower() and not str(clear_reason_code).strip():
                                st.error("Clear reason code is required when clearing another user's history.")
                            elif str(clear_reason_code).strip().lower() == "other" and not clear_reason_note:
                                st.error("Reason note is required when reason code is `other`.")
                            else:
                                key_to_clear = f"{handoff_prefix}{username_to_clear}"
                                try:
                                    repo.upsert_runtime_setting(
                                        environment=settings.app_env,
                                        key=key_to_clear,
                                        value="[]",
                                        value_type="str",
                                        description="Recent Documents handoff contexts (per-user) for quick reopen.",
                                        is_active=True,
                                        actor=user.username,
                                    )
                                    try:
                                        repo.record_audit_event(
                                            entity_type="documents_handoff_history",
                                            entity_id=None,
                                            action="clear_history",
                                            actor=user.username,
                                            changes={
                                                "scope": "admin" if is_admin_user else "self",
                                                "target_user": username_to_clear,
                                                "environment": settings.app_env,
                                                "reason_code": str(clear_reason_code).strip().lower(),
                                                "reason_note": clear_reason_note,
                                                "reason": (
                                                    f"{str(clear_reason_code).strip().lower()}: {clear_reason_note}"
                                                    if clear_reason_note
                                                    else str(clear_reason_code).strip().lower()
                                                ),
                                            },
                                        )
                                    except Exception:
                                        pass
                                    st.success(f"Cleared handoff history for `{username_to_clear}`.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Unable to clear user history: {exc}")

        st.markdown("### Documents Handoff Clear Audit Summary")
        personal_preset_store_key = (
            f"documents_handoff_clear_audit_presets_json__{str(user.username).strip().lower()}"
        )
        shared_preset_store_key = "documents_handoff_clear_audit_presets_json__shared"

        def _load_preset_map(store_key: str) -> dict[str, dict]:
            row = next((r for r in runtime_rows if str(r.key or "").strip() == store_key), None)
            if row is None:
                return {}
            raw = str(row.value or "").strip()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return {str(k): v for k, v in parsed.items() if isinstance(v, dict)}
            except Exception:
                return {}
            return {}

        personal_presets = _load_preset_map(personal_preset_store_key)
        shared_presets = _load_preset_map(shared_preset_store_key)
        shared_default_key_store = "documents_handoff_clear_audit_default_shared_preset"
        shared_default_name = ""
        shared_default_row = next(
            (r for r in runtime_rows if str(r.key or "").strip() == shared_default_key_store),
            None,
        )
        if shared_default_row is not None:
            shared_default_name = str(shared_default_row.value or "").strip()

        def _apply_governance_preset(payload: dict) -> None:
            st.session_state["admin_documents_handoff_clear_audit_date_preset"] = str(
                payload.get("date_preset") or "Last 30d"
            )
            from_raw = str(payload.get("from_date") or "").strip()
            to_raw = str(payload.get("to_date") or "").strip()
            try:
                if from_raw:
                    st.session_state["admin_documents_handoff_clear_audit_from_date"] = datetime.fromisoformat(from_raw).date()
            except Exception:
                pass
            try:
                if to_raw:
                    st.session_state["admin_documents_handoff_clear_audit_to_date"] = datetime.fromisoformat(to_raw).date()
            except Exception:
                pass
            st.session_state["admin_documents_handoff_clear_audit_reason_filter"] = list(
                payload.get("reason_codes") or []
            )
            st.session_state["admin_documents_handoff_clear_audit_scope_filter"] = list(
                payload.get("scopes") or []
            )
            try:
                st.session_state["admin_documents_handoff_clear_audit_limit"] = int(
                    payload.get("lookback_limit") or 1000
                )
            except Exception:
                st.session_state["admin_documents_handoff_clear_audit_limit"] = 1000

        if not bool(st.session_state.get("admin_documents_handoff_default_shared_loaded")):
            default_payload = shared_presets.get(shared_default_name) if shared_default_name else None
            if isinstance(default_payload, dict):
                _apply_governance_preset(default_payload)
            st.session_state["admin_documents_handoff_default_shared_loaded"] = True

        st.markdown("#### Saved Governance Views")
        pg0, pg1, pg2, pg3 = st.columns([1, 2, 2, 2])
        with pg0:
            preset_scope = st.selectbox(
                "Preset Scope",
                options=["My Presets", "Shared Presets"],
                index=0,
                key="admin_documents_handoff_governance_preset_scope",
            )
        active_presets = personal_presets if preset_scope == "My Presets" else shared_presets
        active_store_key = personal_preset_store_key if preset_scope == "My Presets" else shared_preset_store_key
        active_description = (
            "Per-user saved governance views for Documents handoff clear-audit review."
            if preset_scope == "My Presets"
            else "Team-shared governance views for Documents handoff clear-audit review."
        )
        with pg1:
            selected_governance_preset = st.selectbox(
                "Saved Preset",
                options=["None"] + sorted(active_presets.keys()),
                key="admin_documents_handoff_governance_preset_select",
            )
        with pg2:
            new_governance_preset_name = st.text_input(
                "Preset Name",
                value="",
                key="admin_documents_handoff_governance_preset_name",
            ).strip()
        with pg3:
            st.caption("Save current filters/date settings as a reusable preset.")
            save_governance_preset = st.button(
                "Save Preset",
                key="admin_documents_handoff_governance_preset_save_btn",
            )
        lp1, lp2 = st.columns(2)
        with lp1:
            load_governance_preset = st.button(
                "Load Preset",
                key="admin_documents_handoff_governance_preset_load_btn",
            )
        with lp2:
            delete_governance_preset = st.button(
                "Delete Preset",
                key="admin_documents_handoff_governance_preset_delete_btn",
            )
        sp1, sp2 = st.columns(2)
        with sp1:
            set_team_default = st.button(
                "Set as Team Default",
                key="admin_documents_handoff_governance_set_default_btn",
                disabled=not (is_admin_user and preset_scope == "Shared Presets" and selected_governance_preset != "None"),
            )
        with sp2:
            clear_team_default = st.button(
                "Clear Team Default",
                key="admin_documents_handoff_governance_clear_default_btn",
                disabled=not is_admin_user,
            )
        st.caption(f"Current Team Default: `{shared_default_name or '(none)'}`")

        if save_governance_preset:
            if not new_governance_preset_name:
                st.error("Preset name is required.")
            elif preset_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can save shared presets.")
            else:
                active_presets[new_governance_preset_name] = {
                    "date_preset": str(st.session_state.get("admin_documents_handoff_clear_audit_date_preset") or "Last 30d"),
                    "from_date": str(st.session_state.get("admin_documents_handoff_clear_audit_from_date") or ""),
                    "to_date": str(st.session_state.get("admin_documents_handoff_clear_audit_to_date") or ""),
                    "reason_codes": list(st.session_state.get("admin_documents_handoff_clear_audit_reason_filter") or []),
                    "scopes": list(st.session_state.get("admin_documents_handoff_clear_audit_scope_filter") or []),
                    "lookback_limit": int(st.session_state.get("admin_documents_handoff_clear_audit_limit") or 1000),
                }
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_store_key,
                        value=json.dumps(active_presets),
                        value_type="str",
                        description=active_description,
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="save",
                            actor=user.username,
                            changes={
                                "scope": "shared" if preset_scope == "Shared Presets" else "personal",
                                "preset_name": new_governance_preset_name,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Saved preset `{new_governance_preset_name}` ({preset_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save preset: {exc}")

        if load_governance_preset:
            if selected_governance_preset == "None":
                st.error("Select a preset first.")
            else:
                payload = active_presets.get(selected_governance_preset) or {}
                _apply_governance_preset(payload)
                try:
                    repo.record_audit_event(
                        entity_type="documents_handoff_governance_preset",
                        entity_id=None,
                        action="load",
                        actor=user.username,
                        changes={
                            "scope": "shared" if preset_scope == "Shared Presets" else "personal",
                            "preset_name": selected_governance_preset,
                            "environment": settings.app_env,
                        },
                    )
                except Exception:
                    pass
                st.success(f"Loaded preset `{selected_governance_preset}` ({preset_scope}).")
                st.rerun()

        if delete_governance_preset:
            if selected_governance_preset == "None":
                st.error("Select a preset first.")
            elif preset_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can delete shared presets.")
            else:
                active_presets.pop(selected_governance_preset, None)
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_store_key,
                        value=json.dumps(active_presets),
                        value_type="str",
                        description=active_description,
                        is_active=True,
                        actor=user.username,
                    )
                    if preset_scope == "Shared Presets" and selected_governance_preset == shared_default_name:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=shared_default_key_store,
                            value="",
                            value_type="str",
                            description="Default team-shared governance preset for Documents handoff clear-audit panel.",
                            is_active=True,
                            actor=user.username,
                        )
                    st.session_state["admin_documents_handoff_default_shared_loaded"] = False
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="delete",
                            actor=user.username,
                            changes={
                                "scope": "shared" if preset_scope == "Shared Presets" else "personal",
                                "preset_name": selected_governance_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Deleted preset `{selected_governance_preset}` ({preset_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete preset: {exc}")

        if set_team_default:
            if not is_admin_user:
                st.error("Only admins can set team default.")
            elif preset_scope != "Shared Presets":
                st.error("Switch to Shared Presets to set team default.")
            elif selected_governance_preset == "None":
                st.error("Select a shared preset first.")
            elif selected_governance_preset not in shared_presets:
                st.error("Selected shared preset was not found.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=shared_default_key_store,
                        value=selected_governance_preset,
                        value_type="str",
                        description="Default team-shared governance preset for Documents handoff clear-audit panel.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["admin_documents_handoff_default_shared_loaded"] = False
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="set_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "preset_name": selected_governance_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Set team default to `{selected_governance_preset}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set team default: {exc}")

        if clear_team_default:
            if not is_admin_user:
                st.error("Only admins can clear team default.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=shared_default_key_store,
                        value="",
                        value_type="str",
                        description="Default team-shared governance preset for Documents handoff clear-audit panel.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["admin_documents_handoff_default_shared_loaded"] = False
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="clear_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success("Cleared team default preset.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear team default: {exc}")

        governance_review_mode_default = get_runtime_bool(
            repo,
            "documents_handoff_governance_review_mode",
            False,
        )
        governance_review_mode = st.checkbox(
            "Governance Review Mode (Shared Date Window)",
            value=bool(governance_review_mode_default),
            key="admin_documents_handoff_governance_review_mode",
            help=(
                "When enabled, both Governance Preset Audit and Clear Audit use the same date preset/range "
                "to simplify recurring reviews."
            ),
        )
        if st.button("Save Governance Review Preference", key="admin_documents_handoff_governance_review_mode_save"):
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="documents_handoff_governance_review_mode",
                    value="true" if governance_review_mode else "false",
                    value_type="bool",
                    description="When true, Admin governance clear-audit and preset-audit share one date preset/range.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Saved governance review preference.")
            except Exception as exc:
                st.error(f"Unable to save governance review preference: {exc}")

        st.markdown("#### Saved Governance Date Windows")
        window_personal_store_key = (
            f"documents_handoff_governance_window_presets_json__{str(user.username).strip().lower()}"
        )
        window_shared_store_key = "documents_handoff_governance_window_presets_json__shared"
        window_default_shared_key = "documents_handoff_governance_window_default_shared_preset"
        window_personal_presets = _load_preset_map(window_personal_store_key)
        window_shared_presets = _load_preset_map(window_shared_store_key)
        window_default_name = ""
        window_default_row = next(
            (r for r in runtime_rows if str(r.key or "").strip() == window_default_shared_key),
            None,
        )
        if window_default_row is not None:
            window_default_name = str(window_default_row.value or "").strip()

        def _apply_governance_window(payload: dict) -> None:
            date_preset = str(payload.get("date_preset") or "Last 30d")
            st.session_state["admin_documents_handoff_governance_shared_date_preset"] = date_preset
            st.session_state["admin_documents_handoff_governance_preset_audit_date_preset"] = date_preset
            st.session_state["admin_documents_handoff_clear_audit_date_preset"] = date_preset
            from_raw = str(payload.get("from_date") or "").strip()
            to_raw = str(payload.get("to_date") or "").strip()
            from_date = st.session_state.get("admin_documents_handoff_governance_shared_from_date")
            to_date = st.session_state.get("admin_documents_handoff_governance_shared_to_date")
            try:
                if from_raw:
                    from_date = datetime.fromisoformat(from_raw).date()
            except Exception:
                pass
            try:
                if to_raw:
                    to_date = datetime.fromisoformat(to_raw).date()
            except Exception:
                pass
            st.session_state["admin_documents_handoff_governance_shared_from_date"] = from_date
            st.session_state["admin_documents_handoff_governance_shared_to_date"] = to_date
            st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = from_date
            st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = to_date
            st.session_state["admin_documents_handoff_clear_audit_from_date"] = from_date
            st.session_state["admin_documents_handoff_clear_audit_to_date"] = to_date

        wp0, wp1, wp2, wp3 = st.columns([1, 2, 2, 2])
        with wp0:
            window_scope = st.selectbox(
                "Window Scope",
                options=["My Presets", "Shared Presets"],
                index=0,
                key="admin_documents_handoff_governance_window_scope",
            )
        active_window_presets = window_personal_presets if window_scope == "My Presets" else window_shared_presets
        active_window_store_key = window_personal_store_key if window_scope == "My Presets" else window_shared_store_key
        active_window_description = (
            "Per-user governance shared-date window presets."
            if window_scope == "My Presets"
            else "Team-shared governance shared-date window presets."
        )
        with wp1:
            selected_window_preset = st.selectbox(
                "Saved Window",
                options=["None"] + sorted(active_window_presets.keys()),
                key="admin_documents_handoff_governance_window_select",
            )
        with wp2:
            new_window_preset_name = st.text_input(
                "Window Name",
                value="",
                key="admin_documents_handoff_governance_window_name",
            ).strip()
        with wp3:
            save_window_preset = st.button(
                "Save Window Preset",
                key="admin_documents_handoff_governance_window_save_btn",
            )

        wl1, wl2 = st.columns(2)
        with wl1:
            load_window_preset = st.button(
                "Load Window Preset",
                key="admin_documents_handoff_governance_window_load_btn",
            )
        with wl2:
            delete_window_preset = st.button(
                "Delete Window Preset",
                key="admin_documents_handoff_governance_window_delete_btn",
            )
        wd1, wd2 = st.columns(2)
        with wd1:
            set_window_team_default = st.button(
                "Set Window Team Default",
                key="admin_documents_handoff_governance_window_set_default_btn",
                disabled=not (is_admin_user and window_scope == "Shared Presets" and selected_window_preset != "None"),
            )
        with wd2:
            clear_window_team_default = st.button(
                "Clear Window Team Default",
                key="admin_documents_handoff_governance_window_clear_default_btn",
                disabled=not is_admin_user,
            )
        st.caption(f"Current Window Team Default: `{window_default_name or '(none)'}`")

        if save_window_preset:
            if not new_window_preset_name:
                st.error("Window preset name is required.")
            elif window_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can save shared window presets.")
            else:
                active_window_presets[new_window_preset_name] = {
                    "date_preset": str(st.session_state.get("admin_documents_handoff_governance_shared_date_preset") or "Last 30d"),
                    "from_date": str(st.session_state.get("admin_documents_handoff_governance_shared_from_date") or ""),
                    "to_date": str(st.session_state.get("admin_documents_handoff_governance_shared_to_date") or ""),
                }
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_window_store_key,
                        value=json.dumps(active_window_presets),
                        value_type="str",
                        description=active_window_description,
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_save",
                            actor=user.username,
                            changes={
                                "scope": "shared" if window_scope == "Shared Presets" else "personal",
                                "preset_name": new_window_preset_name,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Saved window preset `{new_window_preset_name}` ({window_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save window preset: {exc}")

        if load_window_preset:
            if selected_window_preset == "None":
                st.error("Select a window preset first.")
            else:
                _apply_governance_window(active_window_presets.get(selected_window_preset) or {})
                try:
                    repo.record_audit_event(
                        entity_type="documents_handoff_governance_preset",
                        entity_id=None,
                        action="window_load",
                        actor=user.username,
                        changes={
                            "scope": "shared" if window_scope == "Shared Presets" else "personal",
                            "preset_name": selected_window_preset,
                            "environment": settings.app_env,
                        },
                    )
                except Exception:
                    pass
                st.success(f"Loaded window preset `{selected_window_preset}` ({window_scope}).")
                st.rerun()

        if delete_window_preset:
            if selected_window_preset == "None":
                st.error("Select a window preset first.")
            elif window_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can delete shared window presets.")
            else:
                active_window_presets.pop(selected_window_preset, None)
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_window_store_key,
                        value=json.dumps(active_window_presets),
                        value_type="str",
                        description=active_window_description,
                        is_active=True,
                        actor=user.username,
                    )
                    if window_scope == "Shared Presets" and selected_window_preset == window_default_name:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=window_default_shared_key,
                            value="",
                            value_type="str",
                            description="Default team-shared governance date-window preset.",
                            is_active=True,
                            actor=user.username,
                        )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_delete",
                            actor=user.username,
                            changes={
                                "scope": "shared" if window_scope == "Shared Presets" else "personal",
                                "preset_name": selected_window_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Deleted window preset `{selected_window_preset}` ({window_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete window preset: {exc}")

        if set_window_team_default:
            if not is_admin_user:
                st.error("Only admins can set window team default.")
            elif window_scope != "Shared Presets":
                st.error("Switch to Shared Presets to set team default.")
            elif selected_window_preset == "None":
                st.error("Select a shared window preset first.")
            elif selected_window_preset not in window_shared_presets:
                st.error("Selected shared window preset was not found.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=window_default_shared_key,
                        value=selected_window_preset,
                        value_type="str",
                        description="Default team-shared governance date-window preset.",
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_set_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "preset_name": selected_window_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Set window team default to `{selected_window_preset}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set window team default: {exc}")

        if clear_window_team_default:
            if not is_admin_user:
                st.error("Only admins can clear window team default.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=window_default_shared_key,
                        value="",
                        value_type="str",
                        description="Default team-shared governance date-window preset.",
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_clear_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success("Cleared window team default preset.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear window team default: {exc}")

        if (
            governance_review_mode
            and not bool(st.session_state.get("admin_documents_handoff_window_default_shared_loaded"))
            and window_default_name
            and isinstance(window_shared_presets.get(window_default_name), dict)
        ):
            _apply_governance_window(window_shared_presets.get(window_default_name) or {})
            st.session_state["admin_documents_handoff_window_default_shared_loaded"] = True
        if not governance_review_mode:
            st.session_state["admin_documents_handoff_window_default_shared_loaded"] = False
        shared_from_date = None
        shared_to_date = None
        shared_invalid_range = False
        if governance_review_mode:
            st.caption("Shared date controls for both governance audit sections.")
            shared_preset_options = ["Last 7d", "Last 30d", "This Month", "Custom"]
            shared_preset = st.selectbox(
                "Governance Date Preset",
                options=shared_preset_options,
                index=1,
                key="admin_documents_handoff_governance_shared_date_preset",
            )
            shared_today = utcnow_naive().date()
            if shared_preset == "Last 7d":
                st.session_state["admin_documents_handoff_governance_shared_from_date"] = shared_today - timedelta(
                    days=6
                )
                st.session_state["admin_documents_handoff_governance_shared_to_date"] = shared_today
            elif shared_preset == "Last 30d":
                st.session_state["admin_documents_handoff_governance_shared_from_date"] = shared_today - timedelta(
                    days=29
                )
                st.session_state["admin_documents_handoff_governance_shared_to_date"] = shared_today
            elif shared_preset == "This Month":
                st.session_state["admin_documents_handoff_governance_shared_from_date"] = shared_today.replace(day=1)
                st.session_state["admin_documents_handoff_governance_shared_to_date"] = shared_today
            sd1, sd2 = st.columns(2)
            with sd1:
                shared_from_date = st.date_input(
                    "Governance From Date",
                    key="admin_documents_handoff_governance_shared_from_date",
                )
            with sd2:
                shared_to_date = st.date_input(
                    "Governance To Date",
                    key="admin_documents_handoff_governance_shared_to_date",
                )
            shared_invalid_range = shared_from_date > shared_to_date
            if shared_invalid_range:
                st.error("Governance From Date must be on or before Governance To Date.")

            # Keep section-specific state in sync for consistency across panel widgets/export actions.
            st.session_state["admin_documents_handoff_governance_preset_audit_date_preset"] = shared_preset
            st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = shared_from_date
            st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = shared_to_date
            st.session_state["admin_documents_handoff_clear_audit_date_preset"] = shared_preset
            st.session_state["admin_documents_handoff_clear_audit_from_date"] = shared_from_date
            st.session_state["admin_documents_handoff_clear_audit_to_date"] = shared_to_date

        st.markdown("#### Governance Preset Audit Summary")
        preset_audit_limit = st.number_input(
            "Preset Audit Lookback Rows",
            min_value=100,
            max_value=5000,
            value=500,
            step=100,
            key="admin_documents_handoff_governance_preset_audit_limit",
        )
        if governance_review_mode:
            st.caption("Using shared governance date controls above.")
            preset_audit_from_date = shared_from_date
            preset_audit_to_date = shared_to_date
            invalid_preset_audit_range = shared_invalid_range
        else:
            preset_audit_preset_options = ["Last 7d", "Last 30d", "This Month", "Custom"]
            preset_audit_preset = st.selectbox(
                "Preset Audit Date Preset",
                options=preset_audit_preset_options,
                index=1,
                key="admin_documents_handoff_governance_preset_audit_date_preset",
            )
            preset_audit_today = utcnow_naive().date()
            if preset_audit_preset == "Last 7d":
                st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = (
                    preset_audit_today - timedelta(days=6)
                )
                st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = preset_audit_today
            elif preset_audit_preset == "Last 30d":
                st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = (
                    preset_audit_today - timedelta(days=29)
                )
                st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = preset_audit_today
            elif preset_audit_preset == "This Month":
                st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = (
                    preset_audit_today.replace(day=1)
                )
                st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = preset_audit_today
            prd1, prd2 = st.columns(2)
            with prd1:
                preset_audit_from_date = st.date_input(
                    "Preset Audit From Date",
                    key="admin_documents_handoff_governance_preset_audit_from_date",
                )
            with prd2:
                preset_audit_to_date = st.date_input(
                    "Preset Audit To Date",
                    key="admin_documents_handoff_governance_preset_audit_to_date",
                )
            invalid_preset_audit_range = preset_audit_from_date > preset_audit_to_date
        if invalid_preset_audit_range:
            st.error("Preset audit From Date must be on or before To Date.")
        load_preset_audit_summary = st.checkbox(
            "Load Preset Audit Summary (slower)",
            value=False,
            key="admin_documents_handoff_load_preset_audit_summary",
            help="Defers preset-governance audit-history query unless explicitly requested.",
        )
        if not load_preset_audit_summary:
            st.caption(
                "Preset audit summary is deferred. Enable `Load Preset Audit Summary (slower)` to query audit history."
            )
        preset_audit_logs = (
            repo.list_audit_logs(limit=int(preset_audit_limit))
            if load_preset_audit_summary
            else []
        )
        preset_event_rows: list[dict] = []
        for row in preset_audit_logs:
            if str(row.entity_type or "").strip().lower() != "documents_handoff_governance_preset":
                continue
            event_date = None
            try:
                event_date = row.created_at.date() if row.created_at is not None else None
            except Exception:
                event_date = None
            if invalid_preset_audit_range:
                continue
            if event_date is not None:
                if event_date < preset_audit_from_date or event_date > preset_audit_to_date:
                    continue
            changes_obj: dict = {}
            try:
                parsed_changes = json.loads(str(row.changes_json or "{}"))
                if isinstance(parsed_changes, dict):
                    changes_obj = parsed_changes
            except Exception:
                changes_obj = {}
            preset_event_rows.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "created_date": event_date.isoformat() if event_date is not None else "",
                    "actor": str(row.actor or "").strip().lower(),
                    "action": str(row.action or "").strip().lower(),
                    "scope": str(changes_obj.get("scope") or "").strip().lower(),
                    "preset_name": str(changes_obj.get("preset_name") or "").strip(),
                    "environment": str(changes_obj.get("environment") or "").strip().lower(),
                }
            )
        if not is_admin_user:
            username_scope = str(user.username).strip().lower()
            preset_event_rows = [row for row in preset_event_rows if row.get("actor") == username_scope]
        if not preset_event_rows:
            st.caption("No governance preset audit events found in selected lookback.")
        else:
            preset_df = pd.DataFrame(preset_event_rows).sort_values("created_at", ascending=False)
            pa1, pa2, pa3 = st.columns(3)
            pa1.metric("Preset Events", int(len(preset_df)))
            pa2.metric("Actors", int(preset_df["actor"].nunique()))
            pa3.metric("Actions", int(preset_df["action"].nunique()))
            by_action_df = (
                preset_df.groupby(["action"], as_index=False)
                .size()
                .rename(columns={"size": "events"})
                .sort_values("events", ascending=False)
            )
            by_actor_df = (
                preset_df.groupby(["actor"], as_index=False)
                .size()
                .rename(columns={"size": "events"})
                .sort_values("events", ascending=False)
            )
            pca, pcb = st.columns(2)
            with pca:
                st.caption("By Action")
                st.dataframe(by_action_df, use_container_width=True, hide_index=True)
            with pcb:
                st.caption("By Actor")
                st.dataframe(by_actor_df, use_container_width=True, hide_index=True)
            st.caption("Recent preset events")
            st.dataframe(preset_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Preset Audit CSV",
                data=preset_df.to_csv(index=False).encode("utf-8"),
                file_name=f"documents_handoff_governance_preset_audit_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_documents_handoff_governance_preset_audit_download",
            )

        clear_audit_limit = st.number_input(
            "Clear Audit Lookback Rows",
            min_value=100,
            max_value=5000,
            value=1000,
            step=100,
            key="admin_documents_handoff_clear_audit_limit",
        )
        if governance_review_mode:
            st.caption("Using shared governance date controls above.")
            clear_audit_from_date = shared_from_date
            clear_audit_to_date = shared_to_date
            invalid_clear_audit_range = shared_invalid_range
        else:
            preset_options = ["Last 7d", "Last 30d", "This Month", "Custom"]
            preset = st.selectbox(
                "Date Preset",
                options=preset_options,
                index=1,
                key="admin_documents_handoff_clear_audit_date_preset",
            )
            today_local = utcnow_naive().date()
            if preset == "Last 7d":
                st.session_state["admin_documents_handoff_clear_audit_from_date"] = today_local - timedelta(days=6)
                st.session_state["admin_documents_handoff_clear_audit_to_date"] = today_local
            elif preset == "Last 30d":
                st.session_state["admin_documents_handoff_clear_audit_from_date"] = today_local - timedelta(days=29)
                st.session_state["admin_documents_handoff_clear_audit_to_date"] = today_local
            elif preset == "This Month":
                st.session_state["admin_documents_handoff_clear_audit_from_date"] = today_local.replace(day=1)
                st.session_state["admin_documents_handoff_clear_audit_to_date"] = today_local
            dr1, dr2 = st.columns(2)
            with dr1:
                clear_audit_from_date = st.date_input(
                    "From Date",
                    key="admin_documents_handoff_clear_audit_from_date",
                )
            with dr2:
                clear_audit_to_date = st.date_input(
                    "To Date",
                    key="admin_documents_handoff_clear_audit_to_date",
                )
            invalid_clear_audit_range = clear_audit_from_date > clear_audit_to_date
        if invalid_clear_audit_range:
            st.error("From Date must be on or before To Date.")
        load_clear_audit_summary = st.checkbox(
            "Load Clear Audit Summary (slower)",
            value=False,
            key="admin_documents_handoff_load_clear_audit_summary",
            help="Defers clear-history audit query unless explicitly requested.",
        )
        if not load_clear_audit_summary:
            st.caption(
                "Clear audit summary is deferred. Enable `Load Clear Audit Summary (slower)` to query audit history."
            )
        audit_rows = repo.list_audit_logs(limit=int(clear_audit_limit)) if load_clear_audit_summary else []
        clear_rows: list[dict] = []
        for row in audit_rows:
            if str(row.entity_type or "").strip().lower() != "documents_handoff_history":
                continue
            if str(row.action or "").strip().lower() != "clear_history":
                continue
            changes_obj: dict = {}
            try:
                parsed_changes = json.loads(str(row.changes_json or "{}"))
                if isinstance(parsed_changes, dict):
                    changes_obj = parsed_changes
            except Exception:
                changes_obj = {}
            created_at_iso = row.created_at.isoformat() if row.created_at else ""
            created_at_date = None
            try:
                created_at_date = row.created_at.date() if row.created_at is not None else None
            except Exception:
                created_at_date = None
            if invalid_clear_audit_range:
                continue
            if created_at_date is not None and not invalid_clear_audit_range:
                if created_at_date < clear_audit_from_date or created_at_date > clear_audit_to_date:
                    continue
            clear_rows.append(
                {
                    "created_at": created_at_iso,
                    "created_date": created_at_date.isoformat() if created_at_date is not None else "",
                    "actor": str(row.actor or "").strip().lower(),
                    "scope": str(changes_obj.get("scope") or "").strip().lower(),
                    "target_user": str(changes_obj.get("target_user") or "").strip().lower(),
                    "environment": str(changes_obj.get("environment") or "").strip().lower(),
                    "reason_code": str(changes_obj.get("reason_code") or "").strip().lower(),
                    "reason_note": str(changes_obj.get("reason_note") or "").strip(),
                    "reason": str(changes_obj.get("reason") or "").strip(),
                }
            )
        if not is_admin_user:
            username_scope = str(user.username).strip().lower()
            clear_rows = [
                row
                for row in clear_rows
                if row.get("actor") == username_scope or row.get("target_user") == username_scope
            ]
        if not clear_rows:
            st.caption("No handoff clear audit events found in the selected lookback.")
        else:
            clear_df_all = pd.DataFrame(clear_rows).sort_values("created_at", ascending=False)
            reason_filter_options = sorted(
                {
                    str(value).strip()
                    for value in clear_df_all["reason_code"].fillna("").tolist()
                    if str(value).strip()
                }
            )
            scope_filter_options = sorted(
                {
                    str(value).strip()
                    for value in clear_df_all["scope"].fillna("").tolist()
                    if str(value).strip()
                }
            )
            rf1, rf2 = st.columns(2)
            with rf1:
                selected_reason_codes = st.multiselect(
                    "Filter Reason Code",
                    options=reason_filter_options,
                    default=[],
                    key="admin_documents_handoff_clear_audit_reason_filter",
                )
            with rf2:
                selected_scopes = st.multiselect(
                    "Filter Scope",
                    options=scope_filter_options,
                    default=[],
                    key="admin_documents_handoff_clear_audit_scope_filter",
                )
            clear_df = clear_df_all.copy()
            if selected_reason_codes:
                clear_df = clear_df[
                    clear_df["reason_code"].astype(str).isin([str(v) for v in selected_reason_codes])
                ]
            if selected_scopes:
                clear_df = clear_df[
                    clear_df["scope"].astype(str).isin([str(v) for v in selected_scopes])
                ]
            clear_df = clear_df.sort_values("created_at", ascending=False)
            if clear_df.empty:
                st.caption("No clear events for selected reason/scope filters.")
            else:
                ca1, ca2, ca3 = st.columns(3)
                ca1.metric("Clear Events", int(len(clear_df)))
                ca2.metric("Actors", int(clear_df["actor"].nunique()))
                ca3.metric("Targets", int(clear_df["target_user"].nunique()))
                by_actor_df = (
                    clear_df.groupby(["actor"], as_index=False)
                    .size()
                    .rename(columns={"size": "clear_events"})
                    .sort_values("clear_events", ascending=False)
                )
                by_target_df = (
                    clear_df.groupby(["target_user"], as_index=False)
                    .size()
                    .rename(columns={"size": "targeted_events"})
                    .sort_values("targeted_events", ascending=False)
                )
                by_reason_df = (
                    clear_df.assign(
                        reason_code=clear_df["reason_code"]
                        .astype(str)
                        .str.strip()
                        .replace("", "(unspecified)")
                    )
                    .groupby(["reason_code"], as_index=False)
                    .size()
                    .rename(columns={"size": "events"})
                    .sort_values("events", ascending=False)
                )
                cxa, cxb = st.columns(2)
                with cxa:
                    st.caption("By Actor")
                    st.dataframe(by_actor_df, use_container_width=True, hide_index=True)
                with cxb:
                    st.caption("By Target User")
                    st.dataframe(by_target_df, use_container_width=True, hide_index=True)
                st.caption("By Reason Code")
                st.dataframe(by_reason_df, use_container_width=True, hide_index=True)
                if not by_reason_df.empty:
                    st.bar_chart(by_reason_df.set_index("reason_code")["events"], use_container_width=True)
                st.caption("Recent clear events")
                st.dataframe(clear_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Clear Audit CSV",
                    data=clear_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"documents_handoff_clear_audit_{settings.app_env}.csv",
                    mime="text/csv",
                    key="admin_documents_handoff_clear_audit_download",
                )
                bundle_buffer = BytesIO()
                with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                    bundle_zip.writestr("clear_events_raw.csv", clear_df.to_csv(index=False))
                    bundle_zip.writestr("clear_events_by_actor.csv", by_actor_df.to_csv(index=False))
                    bundle_zip.writestr("clear_events_by_target.csv", by_target_df.to_csv(index=False))
                    bundle_zip.writestr("clear_events_by_reason_code.csv", by_reason_df.to_csv(index=False))
                bundle_buffer.seek(0)
                st.download_button(
                    "Export Governance Bundle (ZIP)",
                    data=bundle_buffer.getvalue(),
                    file_name=f"documents_handoff_governance_bundle_{settings.app_env}.zip",
                    mime="application/zip",
                    key="admin_documents_handoff_governance_bundle_download",
                )

        st.markdown("### Purchase Document -> Lot Apply Audit")
        pdla1, pdla2 = st.columns(2)
        with pdla1:
            purchase_doc_apply_from = st.date_input(
                "From Date",
                value=(utcnow_naive().date() - timedelta(days=29)),
                key="admin_purchase_doc_lot_apply_from_date",
            )
        with pdla2:
            purchase_doc_apply_to = st.date_input(
                "To Date",
                value=utcnow_naive().date(),
                key="admin_purchase_doc_lot_apply_to_date",
            )
        if purchase_doc_apply_from > purchase_doc_apply_to:
            st.error("Purchase-document lot-apply audit From Date must be on or before To Date.")
        purchase_doc_apply_limit = st.number_input(
            "Purchase-Document Audit Lookback Rows",
            min_value=100,
            max_value=5000,
            value=1000,
            step=100,
            key="admin_purchase_doc_lot_apply_audit_limit",
        )
        load_purchase_doc_apply_audit = st.checkbox(
            "Load Purchase-Document Lot-Apply Audit (slower)",
            value=False,
            key="admin_load_purchase_doc_lot_apply_audit",
            help="Defers audit-log query unless explicitly requested.",
        )
        if not load_purchase_doc_apply_audit:
            st.caption(
                "Purchase-document lot-apply audit is deferred. Enable "
                "`Load Purchase-Document Lot-Apply Audit (slower)` to query events."
            )
        else:
            audit_rows = repo.list_audit_logs(limit=int(purchase_doc_apply_limit))
            event_rows: list[dict] = []
            for row in audit_rows:
                if str(getattr(row, "entity_type", "") or "").strip().lower() != "purchase_document":
                    continue
                action = str(getattr(row, "action", "") or "").strip().lower()
                if action not in {"auto_apply_extracted_fields_to_lot", "manual_apply_extracted_fields_to_lot"}:
                    continue
                created_dt = getattr(row, "created_at", None)
                if created_dt is None:
                    continue
                created_date = created_dt.date()
                if created_date < purchase_doc_apply_from or created_date > purchase_doc_apply_to:
                    continue
                payload: dict = {}
                try:
                    parsed = json.loads(str(getattr(row, "changes_json", "") or "{}"))
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:
                    payload = {}
                applied_fields_raw = payload.get("applied_fields")
                if isinstance(applied_fields_raw, list):
                    applied_fields = [str(v).strip() for v in applied_fields_raw if str(v).strip()]
                else:
                    applied_fields = []
                event_rows.append(
                    {
                        "created_at": created_dt.isoformat(),
                        "actor": str(getattr(row, "actor", "") or "").strip().lower(),
                        "action": action,
                        "mode": str(payload.get("mode") or "").strip().lower(),
                        "workflow": str(payload.get("workflow") or "").strip(),
                        "purchase_document_id": int(getattr(row, "entity_id", 0) or 0),
                        "lot_id": int(payload.get("lot_id") or 0) if payload.get("lot_id") is not None else 0,
                        "applied_field_count": int(len(applied_fields)),
                        "applied_fields": ", ".join(applied_fields),
                    }
                )
            if not event_rows:
                st.caption("No purchase-document lot-apply audit events found in selected lookback.")
            else:
                events_df = pd.DataFrame(event_rows).sort_values("created_at", ascending=False)
                auto_count = int(
                    (events_df["action"].astype(str) == "auto_apply_extracted_fields_to_lot").sum()
                )
                manual_count = int(
                    (events_df["action"].astype(str) == "manual_apply_extracted_fields_to_lot").sum()
                )
                qa1, qa2, qa3, qa4 = st.columns(4)
                qa1.metric("Events", int(len(events_df)))
                qa2.metric("Auto", auto_count)
                qa3.metric("Manual", manual_count)
                qa4.metric("Distinct Lots", int(events_df["lot_id"].replace(0, pd.NA).dropna().nunique()))
                st.dataframe(events_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Purchase-Document Lot-Apply Audit CSV",
                    data=events_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"purchase_document_lot_apply_audit_admin_{settings.app_env}.csv",
                    mime="text/csv",
                    key="admin_purchase_doc_lot_apply_audit_download",
                )

        st.markdown("### Seed Recommended Runtime Keys")
        if st.button("Seed Defaults From Current Env", key="admin_runtime_seed_btn"):
            seeded = _seed_missing_runtime_defaults(
                repo,
                actor=user.username,
                seed_defaults=runtime_seed_defaults,
            )
            if seeded:
                st.success(f"Seeded {seeded} runtime settings.")
                st.rerun()
            else:
                st.info("No new runtime settings were seeded.")

        st.markdown("### Add/Update Runtime Setting")
        with st.form("admin_runtime_upsert_form"):
            c1, c2 = st.columns(2)
            with c1:
                runtime_key = st.text_input("Setting Key", value="")
            with c2:
                runtime_type = st.selectbox("Value Type", options=["str", "int", "float", "bool", "json"], index=0)
            runtime_value = st.text_area("Value")
            runtime_description = st.text_input("Description")
            runtime_active = st.checkbox("Active", value=True)
            runtime_submit = st.form_submit_button("Save Runtime Setting")
        if runtime_submit:
            try:
                row = repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key=(runtime_key or "").strip(),
                    value=(runtime_value or "").strip(),
                    value_type=runtime_type,
                    description=(runtime_description or "").strip(),
                    is_active=bool(runtime_active),
                    actor=user.username,
                )
                st.success(f"Saved runtime setting `{row.key}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save runtime setting: {exc}")

        if runtime_rows:
            st.markdown("### Manage Existing Runtime Setting")
            runtime_map = {
                f"#{row.id} | {row.key} | type={row.value_type} | active={row.is_active}": row
                for row in runtime_rows
            }
            selected_runtime_key = st.selectbox(
                "Select Runtime Setting",
                options=list(runtime_map.keys()),
                key="admin_runtime_setting_select",
            )
            selected_runtime = runtime_map[selected_runtime_key]

            with st.form("admin_runtime_update_selected_form"):
                selected_value = st.text_area("Value", value=selected_runtime.value)
                selected_type = st.selectbox(
                    "Value Type",
                    options=["str", "int", "float", "bool", "json"],
                    index=["str", "int", "float", "bool", "json"].index(selected_runtime.value_type)
                    if selected_runtime.value_type in {"str", "int", "float", "bool", "json"}
                    else 0,
                )
                selected_desc = st.text_input("Description", value=selected_runtime.description)
                selected_active = st.checkbox("Active", value=bool(selected_runtime.is_active))
                update_selected_runtime = st.form_submit_button("Update Selected")
            if update_selected_runtime:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=selected_runtime.key,
                        value=selected_value,
                        value_type=selected_type,
                        description=selected_desc,
                        is_active=bool(selected_active),
                        actor=user.username,
                    )
                    st.success("Runtime setting updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update runtime setting: {exc}")

            with st.form("admin_runtime_delete_form"):
                confirm_runtime_delete = st.checkbox("I understand this deletes the selected runtime setting.")
                runtime_delete_phrase = st.text_input("Type DELETE to confirm")
                runtime_delete_submit = st.form_submit_button("Delete Selected Runtime Setting")
            if runtime_delete_submit:
                if not confirm_runtime_delete or runtime_delete_phrase.strip() != "DELETE":
                    st.error("Confirm deletion and type `DELETE` exactly.")
                else:
                    try:
                        repo.delete_runtime_setting_by_id(
                            setting_id=selected_runtime.id,
                            actor=user.username,
                        )
                        st.success("Runtime setting deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to delete runtime setting: {exc}")

    with tab_integrations:
        st.markdown("### Integrations (Google + Slack)")
        st.caption(
            "Environment-scoped integration runtime settings. "
            "These are foundation controls for GS-V05-007 (Google) and Slack notifications."
        )
        runtime_map = {str(row.key): row for row in repo.list_runtime_settings(environment=settings.app_env, active_only=False)}

        def _rv(key: str, default: str = "") -> str:
            row = runtime_map.get(key)
            if row is None:
                return default
            return str(row.value or default)

        def _rb(key: str, default: bool = False) -> bool:
            return _rv(key, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}

        st.markdown("#### Integrations Performance Controls")
        ic1, ic2, ic3, ic4 = st.columns(4)
        with ic1:
            load_shipping_validation_events = st.checkbox(
                "Load Shipping Validation Events",
                value=False,
                key="admin_integrations_load_shipping_validation_events",
            )
        with ic2:
            load_shipping_adapter_events = st.checkbox(
                "Load Shipping Adapter Events",
                value=False,
                key="admin_integrations_load_shipping_adapter_events",
            )
        with ic3:
            load_automation_engine_events = st.checkbox(
                "Load Automation Engine Events",
                value=False,
                key="admin_integrations_load_automation_engine_events",
            )
        with ic4:
            load_slack_delivery_events = st.checkbox(
                "Load Slack Delivery Events",
                value=False,
                key="admin_integrations_load_slack_delivery_events",
            )
        ic5, ic6 = st.columns(2)
        with ic5:
            load_slack_queue_jobs = st.checkbox(
                "Load Slack Queue Jobs",
                value=False,
                key="admin_integrations_load_slack_queue_jobs",
            )
        with ic6:
            load_google_queue_jobs = st.checkbox(
                "Load Google Queue Jobs",
                value=False,
                key="admin_integrations_load_google_queue_jobs",
            )
        load_integrations_signoff_history = st.checkbox(
            "Load Integrations Sign-Off History",
            value=False,
            key="admin_integrations_load_signoff_history",
            help="Defers sign-off history audit-log reads until explicitly requested.",
        )
        _integration_event_cache: dict[tuple[int, int], list[Any]] = {}

        def _integration_event_rows(lookback_days: int, limit: int) -> list[Any]:
            key = (int(lookback_days), int(limit))
            cached = _integration_event_cache.get(key)
            if cached is not None:
                return cached
            rows = repo.db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "integration_event",
                    AuditLog.created_at >= (utcnow_naive() - timedelta(days=int(lookback_days))),
                )
                .order_by(AuditLog.created_at.desc())
                .limit(int(limit))
            ).all()
            _integration_event_cache[key] = rows
            return rows

        st.markdown("#### Google Workspace")
        with st.form("admin_google_integration_form"):
            g1, g2 = st.columns(2)
            with g1:
                google_enabled = st.checkbox(
                    "Enable Google Integration",
                    value=_rb("google_integration_enabled", False),
                    help="Master toggle for Gmail/Calendar/Drive integration features.",
                )
                google_client_id = st.text_input(
                    "Google OAuth Client ID",
                    value=_rv("google_oauth_client_id", ""),
                )
                google_redirect_uri = st.text_input(
                    "Google OAuth Redirect URI",
                    value=_rv("google_oauth_redirect_uri", ""),
                )
                google_scopes = st.text_area(
                    "Google Scopes CSV",
                    value=_rv(
                        "google_workspace_scopes_csv",
                        "https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/drive.file",
                    ),
                    height=90,
                )
            with g2:
                google_client_secret = st.text_input(
                    "Google OAuth Client Secret",
                    value=_rv("google_oauth_client_secret", ""),
                    type="password",
                )
                google_access_token = st.text_input(
                    "Google Access Token",
                    value=_rv("google_oauth_access_token", ""),
                    type="password",
                )
                google_refresh_token = st.text_input(
                    "Google Refresh Token (Optional)",
                    value=_rv("google_oauth_refresh_token", ""),
                    type="password",
                )
                google_sender = st.text_input(
                    "Default Sender Email",
                    value=_rv("google_default_sender_email", "sales@goldenstackers.com"),
                )
                google_drive_root = st.text_input(
                    "Default Drive Folder ID (Optional)",
                    value=_rv("google_drive_root_folder_id", ""),
                )
                google_calendar_id = st.text_input(
                    "Default Calendar ID",
                    value=_rv("google_default_calendar_id", "primary"),
                )
                google_timezone = st.text_input(
                    "Default Time Zone",
                    value=_rv("google_default_timezone", "America/Denver"),
                )
                google_timeout = st.number_input(
                    "Google HTTP Timeout Seconds",
                    min_value=5,
                    max_value=120,
                    value=max(5, min(120, int(_rv("google_http_timeout_seconds", "30") or "30"))),
                    step=1,
                )
                google_queue_enabled = st.checkbox(
                    "Enable Google Retry Queue",
                    value=_rb("google_queue_enabled", True),
                )
                google_queue_max_retries = st.number_input(
                    "Google Queue Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(_rv("google_queue_max_retries", "5") or "5"))),
                    step=1,
                )
                google_backoff_base = st.number_input(
                    "Queue Backoff Base Seconds",
                    min_value=5,
                    max_value=3600,
                    value=max(5, min(3600, int(_rv("google_queue_backoff_base_seconds", "120") or "120"))),
                    step=5,
                )
                google_backoff_max = st.number_input(
                    "Queue Backoff Max Seconds",
                    min_value=5,
                    max_value=86400,
                    value=max(5, min(86400, int(_rv("google_queue_backoff_max_seconds", "3600") or "3600"))),
                    step=30,
                )
            save_google = st.form_submit_button("Save Google Integration Settings")
        if save_google:
            try:
                updates = [
                    ("google_integration_enabled", "true" if google_enabled else "false", "bool", "Master toggle for Google Workspace integration features (Gmail/Calendar/Drive)."),
                    ("google_oauth_client_id", google_client_id.strip(), "str", "Google OAuth client ID for this environment."),
                    ("google_oauth_client_secret", google_client_secret.strip(), "str", "Google OAuth client secret for this environment."),
                    ("google_oauth_redirect_uri", google_redirect_uri.strip(), "str", "Google OAuth redirect URI for this environment."),
                    ("google_workspace_scopes_csv", google_scopes.strip(), "str", "Comma-separated Google OAuth scopes requested by the app."),
                    ("google_oauth_access_token", google_access_token.strip(), "str", "Google OAuth access token for API calls (runtime-managed credential)."),
                    ("google_oauth_refresh_token", google_refresh_token.strip(), "str", "Google OAuth refresh token for future token refresh flow."),
                    ("google_default_sender_email", google_sender.strip(), "str", "Default sender email used for Gmail invoice/receipt workflows."),
                    ("google_drive_root_folder_id", google_drive_root.strip(), "str", "Optional default Google Drive folder ID for exports/uploads."),
                    ("google_default_calendar_id", google_calendar_id.strip() or "primary", "str", "Default Google Calendar ID for follow-up event creation."),
                    ("google_default_timezone", google_timezone.strip() or "America/Denver", "str", "Default timezone for Google Calendar event scheduling."),
                    ("google_http_timeout_seconds", str(int(google_timeout)), "int", "Timeout for Google API HTTP requests."),
                    ("google_queue_enabled", "true" if google_queue_enabled else "false", "bool", "Enable/disable Google integration retry queue for failed actions."),
                    ("google_queue_max_retries", str(int(google_queue_max_retries)), "int", "Maximum retry attempts per queued Google integration action."),
                    ("google_queue_backoff_base_seconds", str(int(google_backoff_base)), "int", "Base backoff seconds for exponential retry scheduling."),
                    ("google_queue_backoff_max_seconds", str(int(google_backoff_max)), "int", "Maximum backoff seconds for queued retries."),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Google integration settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save Google integration settings: {exc}")

        st.divider()
        st.markdown("#### Shipping Queue Controls")
        with st.form("admin_shipping_queue_settings_form"):
            sq1, sq2 = st.columns(2)
            with sq1:
                shipping_queue_enabled = st.checkbox(
                    "Enable Shipping Queue",
                    value=_rb("shipping_queue_enabled", True),
                )
                shipping_label_purchase_enabled = st.checkbox(
                    "Enable Label Purchase Actions",
                    value=_rb("shipping_label_purchase_enabled", True),
                )
                shipping_label_live_provider_calls_enabled = st.checkbox(
                    "Enable Live Provider Calls",
                    value=_rb("shipping_label_live_provider_calls_enabled", False),
                    help="Keep disabled until provider adapters are fully wired and validated.",
                )
                shipping_queue_max_retries = st.number_input(
                    "Shipping Queue Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(_rv("shipping_queue_max_retries", "5") or "5"))),
                    step=1,
                )
                shipping_queue_backoff_base = st.number_input(
                    "Shipping Queue Backoff Base Seconds",
                    min_value=5,
                    max_value=3600,
                    value=max(5, min(3600, int(_rv("shipping_queue_backoff_base_seconds", "60") or "60"))),
                    step=5,
                )
                shipping_queue_backoff_max = st.number_input(
                    "Shipping Queue Backoff Max Seconds",
                    min_value=5,
                    max_value=86400,
                    value=max(5, min(86400, int(_rv("shipping_queue_backoff_max_seconds", "3600") or "3600"))),
                    step=30,
                )
            with sq2:
                shipping_label_provider_pirateship_enabled = st.checkbox(
                    "Provider: Pirate Ship",
                    value=_rb("shipping_label_provider_pirateship_enabled", True),
                )
                shipping_label_pirateship_mode = st.selectbox(
                    "Pirate Ship Adapter Mode",
                    options=["mock", "api"],
                    index=0 if (_rv("shipping_label_pirateship_mode", "mock").strip().lower() != "api") else 1,
                    help="`mock` returns generated artifacts locally. `api` calls configured Pirate Ship endpoint.",
                )
                shipping_label_pirateship_base_url = st.text_input(
                    "Pirate Ship Base URL",
                    value=_rv("shipping_label_pirateship_base_url", ""),
                )
                shipping_label_pirateship_api_key = st.text_input(
                    "Pirate Ship API Key",
                    value=_rv("shipping_label_pirateship_api_key", ""),
                    type="password",
                )
                shipping_label_pirateship_endpoint_path = st.text_input(
                    "Pirate Ship Endpoint Path",
                    value=_rv("shipping_label_pirateship_endpoint_path", "/v1/labels/purchase"),
                )
                shipping_label_pirateship_auth_scheme = st.selectbox(
                    "Pirate Ship Auth Scheme",
                    options=["bearer", "token"],
                    index=0 if (_rv("shipping_label_pirateship_auth_scheme", "bearer").strip().lower() != "token") else 1,
                )
                shipping_label_pirateship_timeout_seconds = st.number_input(
                    "Pirate Ship Timeout Seconds",
                    min_value=5,
                    max_value=120,
                    value=max(5, min(120, int(_rv("shipping_label_pirateship_timeout_seconds", "20") or "20"))),
                    step=1,
                )
                shipping_label_provider_ebay_shipping_enabled = st.checkbox(
                    "Provider: eBay Shipping",
                    value=_rb("shipping_label_provider_ebay_shipping_enabled", True),
                )
                shipping_label_provider_usps_enabled = st.checkbox(
                    "Provider: USPS",
                    value=_rb("shipping_label_provider_usps_enabled", True),
                )
                shipping_label_provider_ups_enabled = st.checkbox(
                    "Provider: UPS",
                    value=_rb("shipping_label_provider_ups_enabled", True),
                )
                shipping_label_provider_fedex_enabled = st.checkbox(
                    "Provider: FedEx",
                    value=_rb("shipping_label_provider_fedex_enabled", True),
                )
                shipping_label_provider_other_enabled = st.checkbox(
                    "Provider: Other",
                    value=_rb("shipping_label_provider_other_enabled", True),
                )
            save_shipping_queue = st.form_submit_button("Save Shipping Queue Settings")
        if save_shipping_queue:
            try:
                updates = [
                    ("shipping_queue_enabled", "true" if shipping_queue_enabled else "false", "bool", "Enable/disable shipping integration retry queue execution."),
                    ("shipping_queue_max_retries", str(int(shipping_queue_max_retries)), "int", "Default max retries for queued shipping label purchase jobs."),
                    ("shipping_queue_backoff_base_seconds", str(int(shipping_queue_backoff_base)), "int", "Base backoff seconds for shipping queue retry scheduling."),
                    ("shipping_queue_backoff_max_seconds", str(int(shipping_queue_backoff_max)), "int", "Maximum backoff seconds for shipping queue retries."),
                    ("shipping_label_purchase_enabled", "true" if shipping_label_purchase_enabled else "false", "bool", "Enable/disable shipping label purchase queue actions."),
                    ("shipping_label_live_provider_calls_enabled", "true" if shipping_label_live_provider_calls_enabled else "false", "bool", "Guardrail toggle for live external label-purchase API calls."),
                    ("shipping_label_provider_pirateship_enabled", "true" if shipping_label_provider_pirateship_enabled else "false", "bool", "Enable/disable Pirate Ship as a shipping label provider."),
                    ("shipping_label_pirateship_mode", shipping_label_pirateship_mode.strip().lower(), "str", "Pirate Ship adapter mode (`mock` or `api`) for live-provider execution path."),
                    ("shipping_label_pirateship_base_url", shipping_label_pirateship_base_url.strip(), "str", "Pirate Ship adapter base URL for API mode."),
                    ("shipping_label_pirateship_api_key", shipping_label_pirateship_api_key.strip(), "str", "Pirate Ship adapter API key/token for API mode."),
                    ("shipping_label_pirateship_endpoint_path", shipping_label_pirateship_endpoint_path.strip() or "/v1/labels/purchase", "str", "Pirate Ship adapter endpoint path (joined with base URL)."),
                    ("shipping_label_pirateship_auth_scheme", shipping_label_pirateship_auth_scheme.strip().lower() or "bearer", "str", "Pirate Ship auth scheme (`bearer` or `token`)."),
                    ("shipping_label_pirateship_timeout_seconds", str(int(shipping_label_pirateship_timeout_seconds)), "int", "Pirate Ship API timeout seconds for live mode."),
                    ("shipping_label_provider_ebay_shipping_enabled", "true" if shipping_label_provider_ebay_shipping_enabled else "false", "bool", "Enable/disable eBay Shipping as a shipping label provider."),
                    ("shipping_label_provider_usps_enabled", "true" if shipping_label_provider_usps_enabled else "false", "bool", "Enable/disable USPS as a shipping label provider."),
                    ("shipping_label_provider_ups_enabled", "true" if shipping_label_provider_ups_enabled else "false", "bool", "Enable/disable UPS as a shipping label provider."),
                    ("shipping_label_provider_fedex_enabled", "true" if shipping_label_provider_fedex_enabled else "false", "bool", "Enable/disable FedEx as a shipping label provider."),
                    ("shipping_label_provider_other_enabled", "true" if shipping_label_provider_other_enabled else "false", "bool", "Enable/disable generic/other shipping label provider jobs."),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Shipping queue settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save shipping queue settings: {exc}")

        with st.form("admin_shipping_label_adapter_test_form"):
            st.markdown("#### Test Pirate Ship Adapter")
            st.caption("Runs a non-queue test call using current runtime config (`mock` or `api`).")
            tp1, tp2, tp3 = st.columns(3)
            with tp1:
                test_sale_id = st.number_input(
                    "Test Sale ID",
                    min_value=1,
                    max_value=10_000_000,
                    value=1,
                    step=1,
                )
            with tp2:
                test_service = st.text_input("Test Service", value="Ground Advantage")
            with tp3:
                test_package = st.text_input("Test Package Type", value="small_box")
            tq1, tq2, tq3 = st.columns(3)
            with tq1:
                test_tracking = st.text_input("Test Tracking (optional)", value="")
            with tq2:
                test_cost = st.number_input(
                    "Test Label Cost (optional)",
                    min_value=0.0,
                    value=0.0,
                    step=0.01,
                )
            with tq3:
                test_currency = st.text_input("Test Currency", value="USD")
            test_submit = st.form_submit_button("Run Pirate Ship Adapter Test")
        if test_submit:
            try:
                payload = {
                    "sale_id": int(test_sale_id),
                    "shipping_provider": "pirateship",
                    "shipping_service": test_service.strip(),
                    "shipping_package_type": test_package.strip(),
                    "tracking_number": test_tracking.strip(),
                    "shipping_label_cost": float(test_cost) if float(test_cost) > 0 else None,
                    "shipping_label_currency": (test_currency or "USD").strip() or "USD",
                }
                result = purchase_shipping_label(repo, provider="pirateship", payload=payload)
                st.success(
                    f"Adapter test succeeded. label_id={result.label_id}, tracking={result.tracking_number or 'n/a'}"
                )
                st.json(
                    {
                        "label_id": result.label_id,
                        "label_url": result.label_url,
                        "label_cost": result.label_cost,
                        "label_currency": result.label_currency,
                        "tracking_number": result.tracking_number,
                        "provider_payload": result.provider_payload or {},
                    }
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="shipping_label_adapter",
                    action="pirateship_test",
                    status="success",
                    details={
                        "mode": _rv("shipping_label_pirateship_mode", "mock").strip().lower(),
                        "sale_id": int(test_sale_id),
                        "label_id": result.label_id,
                    },
                )
            except Exception as exc:
                st.error(f"Adapter test failed: {exc}")
                try:
                    repo.log_integration_event(
                        actor=user.username,
                        integration="shipping_label_adapter",
                        action="pirateship_test",
                        status="failed",
                        details={
                            "mode": _rv("shipping_label_pirateship_mode", "mock").strip().lower(),
                            "sale_id": int(test_sale_id),
                            "error": str(exc)[:500],
                        },
                    )
                except Exception:
                    pass

        with st.form("admin_shipping_live_provider_validation_form"):
            st.markdown("#### Live Provider Validation Run")
            st.caption(
                "Guided Dev/Prod evidence run for real provider execution. "
                "This may purchase a real label if live calls are enabled."
            )
            lv1, lv2, lv3 = st.columns(3)
            with lv1:
                validation_target_env = st.selectbox(
                    "Validation Target Env",
                    options=["local", "dev", "prod"],
                    index=0 if settings.app_env == "local" else (1 if settings.app_env == "dev" else 2),
                )
            with lv2:
                validation_provider = st.selectbox(
                    "Provider",
                    options=["pirateship", "ebay_shipping", "usps", "ups", "fedex", "other"],
                    index=0,
                )
            with lv3:
                validation_sale_id = st.number_input(
                    "Sale ID",
                    min_value=1,
                    max_value=10_000_000,
                    value=1,
                    step=1,
                )
            lv4, lv5 = st.columns(2)
            with lv4:
                validation_service = st.text_input("Service", value="Ground Advantage")
            with lv5:
                validation_package = st.text_input("Package Type", value="small_box")
            validation_notes = st.text_area(
                "Validation Notes",
                value="",
                height=90,
                help="Include any test context for evidence/sign-off.",
            )
            validation_confirm_live = st.checkbox(
                "I confirm this run may purchase a real shipping label.",
                value=False,
            )
            validation_submit = st.form_submit_button("Run Live Provider Validation Now")
        if validation_submit:
            try:
                live_calls_enabled = bool(_rb("shipping_label_live_provider_calls_enabled", False))
                pirateship_mode = _rv("shipping_label_pirateship_mode", "mock").strip().lower()
                if not validation_confirm_live:
                    raise ValueError("Confirm live purchase acknowledgement before running validation.")
                if not live_calls_enabled:
                    raise ValueError(
                        "Live provider calls are disabled (`shipping_label_live_provider_calls_enabled=false`)."
                    )
                if validation_provider == "pirateship" and pirateship_mode != "api":
                    raise ValueError(
                        "Pirate Ship validation requires adapter mode `api` (current mode is not api)."
                    )
                provider_enabled_key = f"shipping_label_provider_{validation_provider}_enabled"
                if not bool(_rb(provider_enabled_key, True)):
                    raise ValueError(f"Provider is disabled by runtime setting `{provider_enabled_key}`.")
                validation_payload = {
                    "sale_id": int(validation_sale_id),
                    "shipping_provider": str(validation_provider),
                    "shipping_service": str(validation_service or "").strip(),
                    "shipping_package_type": str(validation_package or "").strip(),
                    "shipping_label_currency": "USD",
                    "validation_target_env": str(validation_target_env),
                    "validation_notes": str(validation_notes or "").strip(),
                    "dry_run": False,
                }
                queued = repo.create_integration_queue_job(
                    environment=settings.app_env,
                    integration="shipping",
                    action="purchase_label",
                    payload_json=json.dumps(validation_payload),
                    requested_by=user.username,
                    max_retries=0,
                    actor=user.username,
                )
                ok, msg = process_integration_queue_job(
                    repo,
                    job_id=int(queued.id),
                    actor=user.username,
                )
                queue_row = repo.db.get(IntegrationQueueJob, int(queued.id))
                sale_row = repo.db.get(Sale, int(validation_sale_id))
                details = {
                    "target_env": str(validation_target_env),
                    "runtime_env": settings.app_env,
                    "provider": str(validation_provider),
                    "sale_id": int(validation_sale_id),
                    "queue_job_id": int(queued.id),
                    "queue_status": str(getattr(queue_row, "status", "") or ""),
                    "message": str(msg or ""),
                    "live_calls_enabled": bool(live_calls_enabled),
                    "pirateship_mode": str(pirateship_mode),
                    "validation_notes": str(validation_notes or "").strip(),
                }
                if sale_row is not None:
                    details.update(
                        {
                            "label_id": str(getattr(sale_row, "shipping_label_id", "") or ""),
                            "label_url": str(getattr(sale_row, "shipping_label_url", "") or ""),
                            "label_cost": (
                                float(getattr(sale_row, "shipping_label_cost"))
                                if getattr(sale_row, "shipping_label_cost", None) is not None
                                else None
                            ),
                            "label_currency": str(getattr(sale_row, "shipping_label_currency", "") or ""),
                            "tracking_number": str(getattr(sale_row, "tracking_number", "") or ""),
                            "tracking_status": str(getattr(sale_row, "tracking_status", "") or ""),
                        }
                    )
                repo.log_integration_event(
                    actor=user.username,
                    integration="shipping_provider_validation",
                    action="live_validation_run",
                    status="success" if ok else "failed",
                    details=details,
                )
                if ok:
                    st.success(f"Live provider validation succeeded. queue_job_id={queued.id}")
                else:
                    st.error(f"Live provider validation failed: {msg}")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to run live provider validation: {exc}")
                try:
                    repo.log_integration_event(
                        actor=user.username,
                        integration="shipping_provider_validation",
                        action="live_validation_run",
                        status="failed",
                        details={
                            "target_env": str(validation_target_env),
                            "runtime_env": settings.app_env,
                            "provider": str(validation_provider),
                            "sale_id": int(validation_sale_id),
                            "validation_notes": str(validation_notes or "").strip(),
                            "error": str(exc)[:500],
                        },
                    )
                except Exception:
                    pass

        st.markdown("#### Recent Live Provider Validation Runs")
        if not load_shipping_validation_events:
            st.caption("Live provider validation events are deferred. Enable above to load.")
        else:
            validation_rows = _integration_event_rows(lookback_days=30, limit=1000)
            validation_events: list[dict[str, Any]] = []
            for row in validation_rows:
                try:
                    payload = json.loads(row.changes_json or "{}")
                except Exception:
                    payload = {}
                after = payload.get("after") if isinstance(payload, dict) else {}
                if not isinstance(after, dict):
                    after = {}
                if str(after.get("integration") or "").strip().lower() != "shipping_provider_validation":
                    continue
                details = after.get("details") if isinstance(after.get("details"), dict) else {}
                validation_events.append(
                    {
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                        "actor": str(row.actor or ""),
                        "status": str(after.get("status") or ""),
                        "target_env": str(details.get("target_env") or ""),
                        "provider": str(details.get("provider") or ""),
                        "sale_id": details.get("sale_id"),
                        "queue_job_id": details.get("queue_job_id"),
                        "queue_status": str(details.get("queue_status") or ""),
                        "label_id": str(details.get("label_id") or ""),
                        "tracking_number": str(details.get("tracking_number") or ""),
                        "message": str(details.get("message") or ""),
                        "notes": str(details.get("validation_notes") or ""),
                        "error": str(details.get("error") or "")[:220],
                    }
                )
            if validation_events:
                validation_df = pd.DataFrame(validation_events)
                st.dataframe(validation_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Live Provider Validation CSV",
                    data=validation_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"shipping_provider_validation_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    key="admin_shipping_provider_validation_download_csv_btn",
                )
            else:
                st.caption("No live provider validation runs in last 30 days.")

        st.markdown("#### Validation Sign-Off (Dev/Prod)")
        st.caption(
            "Record explicit sign-off evidence for live-provider validation per environment to close go-live requirements."
        )
        with st.form("admin_shipping_provider_validation_signoff_form"):
            sv1, sv2 = st.columns(2)
            with sv1:
                signoff_target_env = st.selectbox(
                    "Sign-Off Environment",
                    options=["dev", "prod"],
                    index=0,
                    key="admin_shipping_provider_validation_signoff_target_env",
                )
                signoff_date = st.date_input(
                    "Sign-Off Date",
                    value=utcnow_naive().date(),
                    key="admin_shipping_provider_validation_signoff_date",
                )
                signoff_owner = st.text_input(
                    "Owner",
                    value=str(user.username or ""),
                    key="admin_shipping_provider_validation_signoff_owner",
                )
            with sv2:
                signoff_status = st.selectbox(
                    "Sign-Off Status",
                    options=["approved", "blocked", "needs_followup"],
                    index=0,
                    key="admin_shipping_provider_validation_signoff_status",
                )
                signoff_evidence_link = st.text_input(
                    "Evidence Link",
                    placeholder="ticket/runbook/artifact URL",
                    key="admin_shipping_provider_validation_signoff_evidence_link",
                )
            signoff_notes = st.text_area(
                "Sign-Off Notes",
                placeholder="What was validated, rollback path, outstanding risks.",
                key="admin_shipping_provider_validation_signoff_notes",
            )
            create_signoff = st.form_submit_button("Record Validation Sign-Off")
        if create_signoff:
            try:
                repo.record_audit_event(
                    entity_type="shipping_provider_validation_signoff",
                    entity_id=None,
                    action="record",
                    actor=user.username,
                    changes={
                        "target_env": str(signoff_target_env or "").strip().lower(),
                        "signoff_date": str(signoff_date.isoformat()),
                        "owner": str(signoff_owner or "").strip(),
                        "status": str(signoff_status or "").strip().lower(),
                        "evidence_link": str(signoff_evidence_link or "").strip(),
                        "notes": str(signoff_notes or "").strip(),
                    },
                )
                st.success("Validation sign-off recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record validation sign-off: {exc}")

        signoff_logs: list[Any] = []
        if not load_integrations_signoff_history:
            st.caption("Validation sign-off history is deferred. Enable `Load Integrations Sign-Off History` to load.")
        else:
            signoff_logs = repo.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "shipping_provider_validation_signoff")
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(200)
            ).all()
        signoff_rows: list[dict[str, Any]] = []
        for row in signoff_logs:
            payload = _audit_changes(row)
            signoff_rows.append(
                {
                    "recorded_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": str(payload.get("target_env") or ""),
                    "signoff_date": str(payload.get("signoff_date") or ""),
                    "owner": str(payload.get("owner") or ""),
                    "status": str(payload.get("status") or ""),
                    "evidence_link": str(payload.get("evidence_link") or ""),
                    "notes": str(payload.get("notes") or "")[:220],
                }
            )
        if signoff_rows:
            signoff_df = pd.DataFrame(signoff_rows)
            st.dataframe(signoff_df, use_container_width=True, hide_index=True)
            latest_status_by_env: dict[str, str] = {}
            for env in ["dev", "prod"]:
                latest = next(
                    (
                        row
                        for row in signoff_rows
                        if str(row.get("target_env") or "").strip().lower() == env
                    ),
                    None,
                )
                latest_status_by_env[env] = str((latest or {}).get("status") or "")
            s1, s2 = st.columns(2)
            s1.metric("Dev Sign-Off", latest_status_by_env.get("dev") or "missing")
            s2.metric("Prod Sign-Off", latest_status_by_env.get("prod") or "missing")
            st.download_button(
                "Download Validation Sign-Off CSV",
                data=signoff_df.to_csv(index=False).encode("utf-8"),
                file_name=f"shipping_provider_validation_signoff_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_shipping_provider_validation_signoff_download_csv_btn",
            )
        elif load_integrations_signoff_history:
            st.caption("No validation sign-off records yet.")

        st.markdown("#### Recent Shipping Adapter Test Events")
        if not load_shipping_adapter_events:
            st.caption("Shipping adapter event history is deferred. Enable above to load.")
        else:
            shipping_adapter_rows = _integration_event_rows(lookback_days=14, limit=500)
            shipping_adapter_events: list[dict[str, str]] = []
            for row in shipping_adapter_rows:
                try:
                    payload = json.loads(row.changes_json or "{}")
                except Exception:
                    payload = {}
                after = payload.get("after") if isinstance(payload, dict) else {}
                if not isinstance(after, dict):
                    after = {}
                integration_name = str(after.get("integration") or "").strip().lower()
                if integration_name != "shipping_label_adapter":
                    continue
                details = after.get("details") if isinstance(after.get("details"), dict) else {}
                shipping_adapter_events.append(
                    {
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                        "actor": row.actor,
                        "action": str(after.get("action") or row.action or ""),
                        "status": str(after.get("status") or ""),
                        "mode": str(details.get("mode") or ""),
                        "sale_id": str(details.get("sale_id") or ""),
                        "label_id": str(details.get("label_id") or ""),
                        "error": str(details.get("error") or "")[:220],
                    }
                )
            if shipping_adapter_events:
                st.dataframe(pd.DataFrame(shipping_adapter_events), use_container_width=True, hide_index=True)
            else:
                st.caption("No shipping adapter test events in last 14 days.")

        st.markdown("#### Integration Automation Rules (Preview)")
        st.caption(
            "Define environment-scoped rule records (conditions/effects JSON) with audit trail. "
            "Execution engine wiring is the next step."
        )
        with st.form("admin_integration_automation_runtime_form"):
            ia1, ia2 = st.columns(2)
            with ia1:
                integration_automation_dry_run_enabled = st.checkbox(
                    "Automation Dry-Run Mode",
                    value=_rb("integration_automation_dry_run_enabled", True),
                    help="When enabled, matched rules are logged but updates/block effects are not persisted.",
                )
            with ia2:
                integration_automation_execute_approval_required_enabled = st.checkbox(
                    "Allow Requires-Approval Rules To Execute",
                    value=_rb("integration_automation_execute_approval_required_enabled", False),
                    help="When disabled, requires_approval rules are logged as approval-gated only.",
                )
            save_automation_runtime = st.form_submit_button("Save Automation Runtime Settings")
        if save_automation_runtime:
            try:
                updates = [
                    (
                        "integration_automation_dry_run_enabled",
                        "true" if integration_automation_dry_run_enabled else "false",
                        "bool",
                        "When true, automation rules are evaluated/logged but rule effects are not persisted.",
                    ),
                    (
                        "integration_automation_execute_approval_required_enabled",
                        "true" if integration_automation_execute_approval_required_enabled else "false",
                        "bool",
                        "When true, rules marked requires_approval may auto-apply in execution engine.",
                    ),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Automation runtime settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save automation runtime settings: {exc}")

        with st.form("admin_create_integration_automation_rule_form", clear_on_submit=True):
            ar1, ar2, ar3, ar4 = st.columns(4)
            with ar1:
                rule_name = st.text_input("Rule Name", value="")
            with ar2:
                rule_integration = st.selectbox("Integration", options=["shipping", "google", "slack"], index=0)
            with ar3:
                rule_action = st.text_input("Action", value="purchase_label")
            with ar4:
                rule_trigger_status = st.selectbox(
                    "Trigger Status",
                    options=["queued", "running", "failed", "success"],
                    index=0,
                )
            rb1, rb2 = st.columns(2)
            with rb1:
                rule_requires_approval = st.checkbox("Requires Approval", value=True)
            with rb2:
                rule_is_active = st.checkbox("Active", value=True)
            rule_conditions_json = st.text_area(
                "Conditions JSON",
                value='{"all":[{"field":"payload.shipping_provider","op":"eq","value":"pirateship"}]}',
                height=120,
            )
            rule_effect_json = st.text_area(
                "Effect JSON",
                value='{"type":"queue_update","set":{"priority":"high"}}',
                height=120,
            )
            create_rule_submit = st.form_submit_button("Create Automation Rule")
        if create_rule_submit:
            try:
                repo.create_integration_automation_rule(
                    environment=settings.app_env,
                    integration=rule_integration,
                    action=rule_action,
                    name=rule_name,
                    trigger_status=rule_trigger_status,
                    conditions_json=rule_conditions_json,
                    effect_json=rule_effect_json,
                    requires_approval=rule_requires_approval,
                    is_active=rule_is_active,
                    actor=user.username,
                )
                st.success("Automation rule created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to create automation rule: {exc}")

        automation_rules = repo.list_integration_automation_rules(
            environment=settings.app_env,
            active_only=False,
            limit=500,
        )
        if automation_rules:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": r.id,
                            "integration": r.integration,
                            "action": r.action,
                            "name": r.name,
                            "trigger_status": r.trigger_status,
                            "requires_approval": r.requires_approval,
                            "is_active": r.is_active,
                            "created_by": r.created_by,
                            "updated_by": r.updated_by,
                            "created_at": r.created_at.isoformat() if r.created_at else "",
                            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                        }
                        for r in automation_rules
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            rule_options = {f"#{r.id} | {r.integration}:{r.action} | {r.name}": r for r in automation_rules}
            selected_rule_key = st.selectbox(
                "Edit/Delete Rule",
                options=list(rule_options.keys()),
                key="admin_integration_automation_rule_selected",
            )
            selected_rule = rule_options[selected_rule_key]

            with st.expander("Rule Impact Preview", expanded=False):
                st.caption(
                    "Estimate current queue-job matches for this rule using its integration/action/trigger/conditions."
                )
                p1, p2 = st.columns(2)
                with p1:
                    preview_scan_limit = st.number_input(
                        "Scan Limit",
                        min_value=25,
                        max_value=5000,
                        value=1000,
                        step=25,
                        key=f"admin_rule_preview_scan_limit_{selected_rule.id}",
                        help="Maximum queue jobs scanned in this environment for impact estimate.",
                    )
                with p2:
                    preview_sample_limit = st.number_input(
                        "Sample Rows",
                        min_value=5,
                        max_value=200,
                        value=25,
                        step=5,
                        key=f"admin_rule_preview_sample_limit_{selected_rule.id}",
                    )
                if st.button("Run Impact Preview", key=f"admin_rule_preview_btn_{selected_rule.id}"):
                    try:
                        preview = preview_rule_impact(
                            repo,
                            environment=settings.app_env,
                            integration=str(selected_rule.integration or ""),
                            action=str(selected_rule.action or ""),
                            trigger_status=str(selected_rule.trigger_status or ""),
                            conditions_json=str(selected_rule.conditions_json or "{}"),
                            scan_limit=int(preview_scan_limit),
                            sample_limit=int(preview_sample_limit),
                        )
                        st.success("Rule impact preview complete.")
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Candidates", int(preview.get("candidate_jobs") or 0))
                        m2.metric("Matched", int(preview.get("matched_jobs") or 0))
                        m3.metric("Match Rate", f"{(float(preview.get('match_rate') or 0.0) * 100):.1f}%")
                        m4.metric("Payload Parse Errors", int(preview.get("payload_parse_errors") or 0))
                        sample_rows = preview.get("samples") or []
                        if sample_rows:
                            st.dataframe(pd.DataFrame(sample_rows), use_container_width=True, hide_index=True)
                        else:
                            st.caption("No matching jobs found in current queue snapshot.")
                    except Exception as exc:
                        st.error(f"Unable to preview rule impact: {exc}")

            with st.form("admin_edit_integration_automation_rule_form"):
                er1, er2, er3, er4 = st.columns(4)
                with er1:
                    edit_rule_name = st.text_input("Rule Name", value=selected_rule.name)
                with er2:
                    edit_rule_integration = st.selectbox(
                        "Integration",
                        options=["shipping", "google", "slack"],
                        index=max(0, ["shipping", "google", "slack"].index(selected_rule.integration))
                        if selected_rule.integration in {"shipping", "google", "slack"}
                        else 0,
                    )
                with er3:
                    edit_rule_action = st.text_input("Action", value=selected_rule.action)
                with er4:
                    edit_rule_trigger_status = st.selectbox(
                        "Trigger Status",
                        options=["queued", "running", "failed", "success"],
                        index=max(0, ["queued", "running", "failed", "success"].index(selected_rule.trigger_status))
                        if selected_rule.trigger_status in {"queued", "running", "failed", "success"}
                        else 0,
                    )
                eb1, eb2 = st.columns(2)
                with eb1:
                    edit_rule_requires_approval = st.checkbox(
                        "Requires Approval",
                        value=bool(selected_rule.requires_approval),
                    )
                with eb2:
                    edit_rule_is_active = st.checkbox("Active", value=bool(selected_rule.is_active))
                edit_rule_conditions_json = st.text_area(
                    "Conditions JSON",
                    value=selected_rule.conditions_json or "{}",
                    height=120,
                )
                edit_rule_effect_json = st.text_area(
                    "Effect JSON",
                    value=selected_rule.effect_json or "{}",
                    height=120,
                )
                e1, e2 = st.columns(2)
                with e1:
                    update_rule_submit = st.form_submit_button("Save Rule")
                with e2:
                    delete_rule_submit = st.form_submit_button("Delete Rule")
            if update_rule_submit:
                try:
                    repo.update_integration_automation_rule(
                        selected_rule.id,
                        {
                            "name": edit_rule_name.strip(),
                            "integration": edit_rule_integration.strip().lower(),
                            "action": edit_rule_action.strip().lower(),
                            "trigger_status": edit_rule_trigger_status.strip().lower(),
                            "conditions_json": (edit_rule_conditions_json or "{}").strip() or "{}",
                            "effect_json": (edit_rule_effect_json or "{}").strip() or "{}",
                            "requires_approval": bool(edit_rule_requires_approval),
                            "is_active": bool(edit_rule_is_active),
                        },
                        actor=user.username,
                    )
                    st.success("Automation rule updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update automation rule: {exc}")
            if delete_rule_submit:
                try:
                    repo.delete_integration_automation_rule(
                        rule_id=selected_rule.id,
                        actor=user.username,
                    )
                    st.success("Automation rule deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete automation rule: {exc}")
        else:
            st.caption("No integration automation rules configured yet.")

        st.markdown("#### Automation Approvals")
        approval_rule_rows = [
            row
            for row in repo.list_integration_automation_rules(
                environment=settings.app_env,
                active_only=True,
                limit=500,
            )
            if bool(getattr(row, "requires_approval", False))
        ]
        if approval_rule_rows:
            rule_opt_map = {
                f"#{r.id} | {r.integration}:{r.action} | {r.name}": r
                for r in approval_rule_rows
            }
            with st.form("admin_create_automation_approval_form", clear_on_submit=True):
                selected_approval_rule_key = st.selectbox(
                    "Rule",
                    options=list(rule_opt_map.keys()),
                )
                ap1, ap2, ap3 = st.columns(3)
                with ap1:
                    queue_job_id_input = st.number_input(
                        "Queue Job ID (optional, 0=any)",
                        min_value=0,
                        max_value=10_000_000,
                        value=0,
                        step=1,
                    )
                with ap2:
                    expires_in_hours = st.number_input(
                        "Expires In Hours (0=never)",
                        min_value=0,
                        max_value=24 * 365,
                        value=24,
                        step=1,
                    )
                with ap3:
                    approval_actor = st.text_input("Approved By", value=user.username)
                approval_notes = st.text_area("Approval Notes", value="", height=90)
                create_approval_submit = st.form_submit_button("Create Approval")
            if create_approval_submit:
                try:
                    selected_rule = rule_opt_map[selected_approval_rule_key]
                    expires_at = (
                        utcnow_naive() + timedelta(hours=int(expires_in_hours))
                        if int(expires_in_hours) > 0
                        else None
                    )
                    repo.create_integration_automation_approval(
                        environment=settings.app_env,
                        rule_id=int(selected_rule.id),
                        queue_job_id=int(queue_job_id_input) if int(queue_job_id_input) > 0 else None,
                        notes=approval_notes.strip(),
                        approved_by=approval_actor.strip() or user.username,
                        approved_at=utcnow_naive(),
                        expires_at=expires_at,
                        actor=user.username,
                    )
                    st.success("Automation approval created.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to create automation approval: {exc}")
        else:
            st.caption("No active rules requiring approval.")

        approvals = repo.list_integration_automation_approvals(
            environment=settings.app_env,
            active_only=False,
            limit=500,
        )
        if approvals:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": a.id,
                            "rule_id": a.rule_id,
                            "queue_job_id": a.queue_job_id,
                            "status": a.status,
                            "is_active": a.is_active,
                            "approved_by": a.approved_by,
                            "approved_at": a.approved_at.isoformat() if a.approved_at else "",
                            "expires_at": a.expires_at.isoformat() if a.expires_at else "",
                            "notes": a.notes,
                        }
                        for a in approvals
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            active_approval_map = {
                f"#{a.id} | rule={a.rule_id} | job={a.queue_job_id or 'any'} | {a.status}": a
                for a in approvals
                if bool(a.is_active) and str(a.status or "").strip().lower() == "approved"
            }
            if active_approval_map:
                revoke_key = st.selectbox(
                    "Revoke Active Approval",
                    options=list(active_approval_map.keys()),
                    key="admin_revoke_automation_approval_key",
                )
                if st.button("Revoke Approval", key="admin_revoke_automation_approval_btn"):
                    try:
                        repo.revoke_integration_automation_approval(
                            approval_id=int(active_approval_map[revoke_key].id),
                            actor=user.username,
                        )
                        st.success("Approval revoked.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to revoke approval: {exc}")
        else:
            st.caption("No automation approvals yet.")

        st.markdown("#### Recent Automation Engine Events")
        automation_events: list[dict[str, Any]] = []
        if not load_automation_engine_events:
            st.caption("Automation engine event history is deferred. Enable above to load.")
        else:
            automation_audit_rows = _integration_event_rows(lookback_days=14, limit=500)
            for row in automation_audit_rows:
                try:
                    payload = json.loads(row.changes_json or "{}")
                except Exception:
                    payload = {}
                after = payload.get("after") if isinstance(payload, dict) else {}
                if not isinstance(after, dict):
                    after = {}
                integration_name = str(after.get("integration") or "").strip().lower()
                if integration_name != "integration_automation":
                    continue
                details = after.get("details") if isinstance(after.get("details"), dict) else {}
                automation_events.append(
                    {
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                        "actor": row.actor,
                        "action": str(after.get("action") or row.action or ""),
                        "status": str(after.get("status") or ""),
                        "job_id": details.get("job_id"),
                        "integration": details.get("integration"),
                        "action_name": details.get("action_name"),
                        "trigger_status": details.get("trigger_status"),
                        "dry_run": details.get("dry_run"),
                        "blocked": details.get("blocked"),
                        "matched_rules": len(details.get("matched_rule_ids") or []),
                        "applied_rules": len(details.get("applied_rule_ids") or []),
                        "approval_gated_rules": len(details.get("approval_gated_rule_ids") or []),
                        "blocked_reason": str(details.get("blocked_reason") or "")[:160],
                        "matched_rule_ids": details.get("matched_rule_ids") or [],
                        "approval_gated_rule_ids": details.get("approval_gated_rule_ids") or [],
                        "details_raw": details,
                    }
                )
            if automation_events:
                st.dataframe(pd.DataFrame(automation_events), use_container_width=True, hide_index=True)
            else:
                st.caption("No automation engine events in last 14 days.")

        triage_candidates = [
            row
            for row in automation_events
            if bool(row.get("blocked"))
            or int(row.get("approval_gated_rules") or 0) > 0
            or str(row.get("status") or "").strip().lower() == "failed"
        ]
        st.markdown("#### Automation Failure Triage")
        if not triage_candidates:
            st.caption("No blocked/approval-gated automation events to triage.")
        else:
            triage_map = {
                (
                    f"{row.get('created_at') or ''} | job={row.get('job_id') or 'n/a'} | "
                    f"blocked={row.get('blocked')} | gated={row.get('approval_gated_rules')}"
                ): row
                for row in triage_candidates
            }
            selected_triage_key = st.selectbox(
                "Select Automation Event",
                options=list(triage_map.keys()),
                key="admin_automation_triage_event_key",
            )
            selected_triage = triage_map[selected_triage_key]
            st.json(selected_triage.get("details_raw") or {})
            with st.expander("Replay Rule Simulation (Read-Only)", expanded=False):
                st.caption(
                    "Re-evaluate this queue job against current automation rules to compare with historical event outcome."
                )
                sim_job_id = int(selected_triage.get("job_id") or 0)
                sim_state_key = "admin_automation_triage_last_simulation"
                sim_include_inactive = st.checkbox(
                    "Include inactive rules in simulation",
                    value=False,
                    key="admin_automation_triage_sim_include_inactive",
                )
                if st.button("Run Replay Simulation", key="admin_automation_triage_simulate_btn"):
                    try:
                        if sim_job_id <= 0:
                            st.error("Selected event does not contain a queue job id.")
                        else:
                            simulation = simulate_rule_evaluation_for_job(
                                repo,
                                environment=settings.app_env,
                                job_id=sim_job_id,
                                trigger_status=str(selected_triage.get("trigger_status") or ""),
                                include_inactive=bool(sim_include_inactive),
                            )
                            st.session_state[sim_state_key] = simulation
                    except Exception as exc:
                        st.error(f"Unable to run replay simulation: {exc}")
                simulation = st.session_state.get(sim_state_key)
                simulation_for_selected = (
                    isinstance(simulation, dict) and int(simulation.get("job_id") or 0) == sim_job_id
                )
                if simulation_for_selected:
                    s1, s2, s3 = st.columns(3)
                    s1.metric("Rules Considered", int(simulation.get("rules_considered") or 0))
                    s2.metric("Matched Now", int(simulation.get("matched_rules") or 0))
                    s3.metric("Would Apply Now", int(simulation.get("would_apply_rules") or 0))
                    st.caption(
                        f"Approval-gated now: {int(simulation.get('approval_gated_rules') or 0)}"
                    )
                    sim_rows = simulation.get("rows") or []
                    if sim_rows:
                        st.dataframe(pd.DataFrame(sim_rows), use_container_width=True, hide_index=True)
                    else:
                        st.caption("No rules considered for this job context.")

                    blocked_now = any(
                        bool(row.get("would_apply")) and str(row.get("effect_type") or "") == "block_execute"
                        for row in sim_rows
                    )
                    historical = {
                        "matched_rules": int(selected_triage.get("matched_rules") or 0),
                        "applied_rules": int(selected_triage.get("applied_rules") or 0),
                        "approval_gated_rules": int(selected_triage.get("approval_gated_rules") or 0),
                        "blocked": bool(selected_triage.get("blocked")),
                    }
                    replay_now = {
                        "matched_rules": int(simulation.get("matched_rules") or 0),
                        "applied_rules": int(simulation.get("would_apply_rules") or 0),
                        "approval_gated_rules": int(simulation.get("approval_gated_rules") or 0),
                        "blocked": bool(blocked_now),
                    }
                    drift_rows: list[dict[str, Any]] = []
                    for metric in ("matched_rules", "applied_rules", "approval_gated_rules"):
                        before = int(historical.get(metric) or 0)
                        now = int(replay_now.get(metric) or 0)
                        drift_rows.append(
                            {
                                "metric": metric,
                                "historical": before,
                                "replay_now": now,
                                "delta": now - before,
                            }
                        )
                    drift_rows.append(
                        {
                            "metric": "blocked",
                            "historical": historical["blocked"],
                            "replay_now": replay_now["blocked"],
                            "delta": "changed" if historical["blocked"] != replay_now["blocked"] else "same",
                        }
                    )
                    st.markdown("##### Drift Check")
                    st.dataframe(pd.DataFrame(drift_rows), use_container_width=True, hide_index=True)
                    drift_detected = any(
                        (
                            (row.get("metric") == "blocked" and str(row.get("delta")) == "changed")
                            or (
                                row.get("metric") != "blocked"
                                and int(row.get("delta") or 0) != 0
                            )
                        )
                        for row in drift_rows
                    )
                    if drift_detected:
                        st.warning(
                            "Replay differs from historical outcome. Rule set, approvals, or queue context likely changed."
                        )
                    else:
                        st.success("Replay matches historical outcome for key automation metrics.")
                    if st.button("Log Drift Event", key="admin_automation_triage_log_drift_btn"):
                        try:
                            repo.log_integration_event(
                                actor=user.username,
                                integration="integration_automation",
                                action="drift_detected" if drift_detected else "drift_clear",
                                status="warning" if drift_detected else "success",
                                details={
                                    "job_id": int(sim_job_id),
                                    "trigger_status": str(selected_triage.get("trigger_status") or ""),
                                    "historical": historical,
                                    "replay_now": replay_now,
                                    "drift_detected": bool(drift_detected),
                                },
                            )
                            st.success("Drift event logged.")
                        except Exception as exc:
                            st.error(f"Unable to log drift event: {exc}")

                    sim_approval_hours = st.number_input(
                        "Simulation Approval TTL Hours",
                        min_value=0,
                        max_value=24 * 365,
                        value=24,
                        step=1,
                        key="admin_automation_triage_sim_approval_hours",
                    )
                    if st.button("Approve Gated From Simulation", key="admin_automation_triage_sim_approve_btn"):
                        try:
                            created_count = 0
                            for sim_row in sim_rows:
                                if not bool(sim_row.get("approval_gated")):
                                    continue
                                try:
                                    rule_id = int(sim_row.get("rule_id") or 0)
                                except Exception:
                                    continue
                                if rule_id <= 0:
                                    continue
                                if repo.has_active_integration_automation_approval(
                                    environment=settings.app_env,
                                    rule_id=rule_id,
                                    queue_job_id=sim_job_id if sim_job_id > 0 else None,
                                    as_of=utcnow_naive(),
                                ):
                                    continue
                                expires_at = (
                                    utcnow_naive() + timedelta(hours=int(sim_approval_hours))
                                    if int(sim_approval_hours) > 0
                                    else None
                                )
                                repo.create_integration_automation_approval(
                                    environment=settings.app_env,
                                    rule_id=rule_id,
                                    queue_job_id=sim_job_id if sim_job_id > 0 else None,
                                    notes="Created from replay simulation quick action.",
                                    approved_by=user.username,
                                    approved_at=utcnow_naive(),
                                    expires_at=expires_at,
                                    actor=user.username,
                                )
                                created_count += 1
                            st.success(f"Created {created_count} approval record(s) from simulation.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to create approvals from simulation: {exc}")
            t1, t2, t3 = st.columns(3)
            with t1:
                approval_hours = st.number_input(
                    "Approval TTL Hours",
                    min_value=0,
                    max_value=24 * 365,
                    value=24,
                    step=1,
                    key="admin_automation_triage_approval_hours",
                )
                if st.button("Approve Gated Rules", key="admin_automation_triage_approve_btn"):
                    try:
                        created_count = 0
                        queue_job_id = selected_triage.get("job_id")
                        for rule_id_raw in selected_triage.get("approval_gated_rule_ids") or []:
                            try:
                                rule_id = int(rule_id_raw)
                            except Exception:
                                continue
                            if repo.has_active_integration_automation_approval(
                                environment=settings.app_env,
                                rule_id=rule_id,
                                queue_job_id=int(queue_job_id) if queue_job_id else None,
                                as_of=utcnow_naive(),
                            ):
                                continue
                            expires_at = (
                                utcnow_naive() + timedelta(hours=int(approval_hours))
                                if int(approval_hours) > 0
                                else None
                            )
                            repo.create_integration_automation_approval(
                                environment=settings.app_env,
                                rule_id=rule_id,
                                queue_job_id=int(queue_job_id) if queue_job_id else None,
                                notes="Created from automation triage quick action.",
                                approved_by=user.username,
                                approved_at=utcnow_naive(),
                                expires_at=expires_at,
                                actor=user.username,
                            )
                            created_count += 1
                        st.success(f"Created {created_count} approval record(s).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to approve gated rules: {exc}")
            with t2:
                if st.button("Retry Job Now", key="admin_automation_triage_retry_btn"):
                    try:
                        job_id = int(selected_triage.get("job_id") or 0)
                        if job_id <= 0:
                            st.error("Selected event does not contain a queue job id.")
                        else:
                            repo.update_integration_queue_job(
                                job_id,
                                {
                                    "status": "queued",
                                    "next_attempt_at": utcnow_naive(),
                                },
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=job_id,
                                actor=user.username,
                            )
                            if ok:
                                st.success("Retry succeeded.")
                            else:
                                st.warning(f"Retry completed with failure: {msg}")
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to retry selected job: {exc}")
            with t3:
                if st.button("Disable Matched Rules", key="admin_automation_triage_disable_rules_btn"):
                    try:
                        disabled = 0
                        for rule_id_raw in selected_triage.get("matched_rule_ids") or []:
                            try:
                                rule_id = int(rule_id_raw)
                            except Exception:
                                continue
                            repo.update_integration_automation_rule(
                                rule_id,
                                {"is_active": False},
                                actor=user.username,
                            )
                            disabled += 1
                        st.success(f"Disabled {disabled} rule(s).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to disable matched rules: {exc}")

        st.markdown("#### Automation Hardening Sign-Off (Dev/Prod)")
        st.caption(
            "Record explicit production-hardening acceptance for automation guardrails, approval policy, and runbook readiness."
        )
        with st.form("admin_automation_hardening_signoff_form"):
            ah1, ah2 = st.columns(2)
            with ah1:
                hardening_target_env = st.selectbox(
                    "Sign-Off Environment",
                    options=["dev", "prod"],
                    index=0,
                    key="admin_automation_hardening_signoff_target_env",
                )
                hardening_date = st.date_input(
                    "Sign-Off Date",
                    value=utcnow_naive().date(),
                    key="admin_automation_hardening_signoff_date",
                )
                hardening_owner = st.text_input(
                    "Owner",
                    value=str(user.username or ""),
                    key="admin_automation_hardening_signoff_owner",
                )
            with ah2:
                hardening_status = st.selectbox(
                    "Sign-Off Status",
                    options=["approved", "blocked", "needs_followup"],
                    index=0,
                    key="admin_automation_hardening_signoff_status",
                )
                hardening_evidence_link = st.text_input(
                    "Evidence Link",
                    placeholder="runbook/ticket/review link",
                    key="admin_automation_hardening_signoff_evidence_link",
                )
            hh1, hh2, hh3 = st.columns(3)
            with hh1:
                hardening_guardrails_verified = st.checkbox("Guardrails verified", value=True)
            with hh2:
                hardening_approval_policy_reviewed = st.checkbox("Approval policy reviewed", value=True)
            with hh3:
                hardening_runbook_signed_off = st.checkbox("Runbook signed off", value=True)
            hardening_notes = st.text_area(
                "Hardening Notes",
                placeholder="Summary of checks, residual risks, and actions.",
                key="admin_automation_hardening_signoff_notes",
            )
            save_hardening_signoff = st.form_submit_button("Record Hardening Sign-Off")
        if save_hardening_signoff:
            try:
                repo.record_audit_event(
                    entity_type="integration_automation_hardening_signoff",
                    entity_id=None,
                    action="record",
                    actor=user.username,
                    changes={
                        "target_env": str(hardening_target_env or "").strip().lower(),
                        "signoff_date": str(hardening_date.isoformat()),
                        "owner": str(hardening_owner or "").strip(),
                        "status": str(hardening_status or "").strip().lower(),
                        "evidence_link": str(hardening_evidence_link or "").strip(),
                        "guardrails_verified": bool(hardening_guardrails_verified),
                        "approval_policy_reviewed": bool(hardening_approval_policy_reviewed),
                        "runbook_signed_off": bool(hardening_runbook_signed_off),
                        "notes": str(hardening_notes or "").strip(),
                    },
                )
                st.success("Automation hardening sign-off recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record automation hardening sign-off: {exc}")

        hardening_logs: list[Any] = []
        if not load_integrations_signoff_history:
            st.caption("Automation hardening sign-off history is deferred. Enable `Load Integrations Sign-Off History` to load.")
        else:
            hardening_logs = repo.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "integration_automation_hardening_signoff")
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(200)
            ).all()
        hardening_rows: list[dict[str, Any]] = []
        latest_hardening_by_env: dict[str, str] = {}
        for row in hardening_logs:
            payload = _audit_changes(row)
            target_env = str(payload.get("target_env") or "").strip().lower()
            status = str(payload.get("status") or "").strip().lower()
            hardening_rows.append(
                {
                    "recorded_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": target_env,
                    "signoff_date": str(payload.get("signoff_date") or ""),
                    "owner": str(payload.get("owner") or ""),
                    "status": status,
                    "guardrails_verified": bool(payload.get("guardrails_verified")),
                    "approval_policy_reviewed": bool(payload.get("approval_policy_reviewed")),
                    "runbook_signed_off": bool(payload.get("runbook_signed_off")),
                    "evidence_link": str(payload.get("evidence_link") or ""),
                    "notes": str(payload.get("notes") or "")[:220],
                }
            )
            if target_env and target_env not in latest_hardening_by_env:
                latest_hardening_by_env[target_env] = status
        if hardening_rows:
            hardening_df = pd.DataFrame(hardening_rows)
            h1, h2 = st.columns(2)
            h1.metric("Automation Hardening Dev", latest_hardening_by_env.get("dev") or "missing")
            h2.metric("Automation Hardening Prod", latest_hardening_by_env.get("prod") or "missing")
            st.dataframe(hardening_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Hardening Sign-Off CSV",
                data=hardening_df.to_csv(index=False).encode("utf-8"),
                file_name=f"integration_automation_hardening_signoff_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_automation_hardening_signoff_download_csv_btn",
            )
        elif load_integrations_signoff_history:
            st.caption("No automation hardening sign-off records yet.")

        st.divider()
        st.markdown("#### Slack Notifications")
        p1, p2 = st.columns(2)
        with p1:
            if st.button("Apply Recommended Channel Presets (Current Env)", key="admin_slack_apply_env_presets_btn"):
                try:
                    updated = _apply_slack_channel_presets(
                        repo,
                        actor=user.username,
                        env_name=settings.app_env,
                    )
                    st.success(f"Applied {updated} Slack channel preset key(s) for env `{settings.app_env}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply Slack channel presets: {exc}")
        with p2:
            st.caption("Presets seed channels like `#gs-<env>-ops`, `#gs-<env>-sync`, `#gs-<env>-error`.")
        with st.form("admin_slack_integration_form"):
            s1, s2 = st.columns(2)
            with s1:
                slack_enabled = st.checkbox(
                    "Enable Slack Notifications",
                    value=_rb("slack_notifications_enabled", False),
                )
                slack_default_channel = st.text_input(
                    "Default Slack Channel",
                    value=_rv("slack_default_channel", ""),
                    help="Example: #ops-alerts",
                )
                slack_notify_sync = st.checkbox(
                    "Notify Sync Failures",
                    value=_rb("slack_notify_sync_failures", True),
                )
                slack_notify_shipping = st.checkbox(
                    "Notify Shipping Exceptions",
                    value=_rb("slack_notify_shipping_exceptions", True),
                )
                slack_notify_order_imports = st.checkbox(
                    "Notify eBay Order Imports",
                    value=_rb("slack_notify_order_imports", True),
                    help="Send a Slack message when a new eBay order is imported into local Orders/Sales.",
                )
                slack_notify_daily = st.checkbox(
                    "Send Daily Summary",
                    value=_rb("slack_notify_daily_summary", False),
                )
                app_default_timezone = st.text_input(
                    "App Default Timezone",
                    value=_rv("app_default_timezone", settings.app_default_timezone or "America/Denver"),
                    help="Global default timezone for app display/scheduling defaults.",
                )
                slack_daily_report_enabled = st.checkbox(
                    "Enable Daily Ops Report Scheduler",
                    value=_rb("slack_daily_report_enabled", _rb("slack_notify_daily_summary", False)),
                    help="Runs once per local day in sync-runner and sends inventory/sales/listing/order summary to Slack.",
                )
                slack_notify_backup_success = st.checkbox(
                    "Notify Backup Success",
                    value=_rb("slack_notify_backup_success", False),
                )
                slack_notify_backup_failures = st.checkbox(
                    "Notify Backup Failures",
                    value=_rb("slack_notify_backup_failures", True),
                )
                slack_notify_queue_failures = st.checkbox(
                    "Notify Google Queue Failures",
                    value=_rb("slack_notify_google_queue_failures", True),
                )
                slack_notify_integration_queue_failures = st.checkbox(
                    "Notify Integration Queue Failures",
                    value=_rb("slack_notify_integration_queue_failures", True),
                )
                slack_notify_ebay_oauth_refresh_failures = st.checkbox(
                    "Notify eBay OAuth Refresh Failures",
                    value=_rb("slack_notify_ebay_oauth_refresh_failures", True),
                    help="Send Slack alerts when sync-runner eBay token auto-refresh fails.",
                )
                slack_notify_parity_decisions = st.checkbox(
                    "Notify Parity Decisions",
                    value=_rb("slack_notify_parity_decisions", False),
                )
                slack_notify_followup_overdue = st.checkbox(
                    "Notify Follow-up Overdue",
                    value=_rb("slack_notify_followup_overdue", False),
                )
                slack_notify_system_health_critical = st.checkbox(
                    "Notify System Health Critical",
                    value=_rb("slack_notify_system_health_critical", False),
                )
                slack_daily_cron = st.text_input(
                    "Daily Summary Cron (UTC)",
                    value=_rv("slack_daily_summary_cron", "0 16 * * *"),
                )
                slack_daily_report_timezone = st.text_input(
                    "Daily Ops Report Timezone",
                    value=_rv("slack_daily_report_timezone", "America/Denver"),
                    help="IANA timezone for daily ops report schedule.",
                )
                slack_daily_report_local_time = st.text_input(
                    "Daily Ops Report Local Time (HH:MM)",
                    value=_rv("slack_daily_report_local_time", "08:00"),
                    help="24-hour local time for daily ops report in selected timezone.",
                )
                slack_daily_report_fee_lookback_weeks = st.number_input(
                    "Daily Report Fee Coverage Lookback (weeks)",
                    min_value=2,
                    max_value=52,
                    value=max(
                        2,
                        min(
                            52,
                            int(_rv("slack_daily_report_normalized_fee_coverage_lookback_weeks", "8") or "8"),
                        ),
                    ),
                    step=1,
                    help="Weeks of reconciliation data used for normalized fee-source coverage health checks.",
                )
                try:
                    _fee_threshold_default = float(
                        _rv("slack_daily_report_normalized_fee_coverage_threshold_pct", "80") or "80"
                    )
                except Exception:
                    _fee_threshold_default = 80.0
                slack_daily_report_fee_threshold_pct = st.number_input(
                    "Daily Report Fee Coverage Threshold (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=max(0.0, min(100.0, float(_fee_threshold_default))),
                    step=0.5,
                    help="Alert threshold for weekly normalized fee-source coverage percentage.",
                )
                slack_daily_report_fee_consecutive_weeks = st.number_input(
                    "Daily Report Fee Coverage Consecutive Weeks",
                    min_value=1,
                    max_value=12,
                    value=max(
                        1,
                        min(
                            12,
                            int(
                                _rv(
                                    "slack_daily_report_normalized_fee_coverage_consecutive_weeks",
                                    "2",
                                )
                                or "2"
                            ),
                        ),
                    ),
                    step=1,
                    help="Consecutive below-threshold weeks required before posting a coverage alert line.",
                )
                slack_timeout = st.number_input(
                    "Slack HTTP Timeout Seconds",
                    min_value=3,
                    max_value=60,
                    value=max(3, min(60, int(_rv("slack_http_timeout_seconds", "15") or "15"))),
                    step=1,
                )
                slack_queue_enabled = st.checkbox(
                    "Enable Slack Retry Queue",
                    value=_rb("slack_queue_enabled", True),
                )
                slack_queue_max_retries = st.number_input(
                    "Slack Queue Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(_rv("slack_queue_max_retries", "5") or "5"))),
                    step=1,
                )
                slack_queue_backoff_base = st.number_input(
                    "Slack Queue Backoff Base Seconds",
                    min_value=5,
                    max_value=3600,
                    value=max(5, min(3600, int(_rv("slack_queue_backoff_base_seconds", "60") or "60"))),
                    step=5,
                )
                slack_queue_backoff_max = st.number_input(
                    "Slack Queue Backoff Max Seconds",
                    min_value=5,
                    max_value=86400,
                    value=max(5, min(86400, int(_rv("slack_queue_backoff_max_seconds", "3600") or "3600"))),
                    step=30,
                )
                health_auto_alert_critical_enabled = st.checkbox(
                    "Auto-Alert Health Critical Signals",
                    value=_rb("health_auto_alert_critical_enabled", False),
                    help="When enabled, System Health can auto-send Slack critical alerts on threshold breach.",
                )
                health_auto_alert_cooldown_minutes = st.number_input(
                    "Health Critical Alert Cooldown Minutes",
                    min_value=5,
                    max_value=24 * 60,
                    value=max(5, min(24 * 60, int(_rv("health_auto_alert_cooldown_minutes", "60") or "60"))),
                    step=5,
                )
                slack_ops_runner_enabled = st.checkbox(
                    "Enable Slack Ops Socket Mode Runner",
                    value=_rb("slack_ops_runner_enabled", False),
                    help="Runs dedicated Slack Socket Mode worker for app mentions/commands.",
                )
                slack_ops_enabled = st.checkbox(
                    "Enable Slack Ops Command Ingest",
                    value=_rb("slack_ops_enabled", True),
                    help="Global kill switch for Slack command ingestion.",
                )
                slack_ops_write_actions_require_approval = st.checkbox(
                    "Slack Ops: Require Approval For Write Actions",
                    value=_rb("slack_ops_write_actions_require_approval", True),
                )
                slack_ops_ai_assist_enabled = st.checkbox(
                    "Slack Ops: Enable AI Assist",
                    value=_rb("slack_ops_ai_assist_enabled", True),
                )
                slack_ops_ai_auto_reply_enabled = st.checkbox(
                    "Slack Ops: Enable AI Auto-Reply",
                    value=_rb("slack_ops_ai_auto_reply_enabled", False),
                    help="Posts AI summary responses back to Slack thread/channel after queue execution.",
                )
                slack_ops_process_queue_enabled = st.checkbox(
                    "Auto-Process Slack Ops Queue",
                    value=_rb("slack_ops_process_queue_enabled", True),
                    help="Allows Slack Ops runner to execute due slack_ops queue jobs automatically.",
                )
                slack_ops_process_queue_limit = st.number_input(
                    "Slack Ops Queue Process Limit",
                    min_value=1,
                    max_value=200,
                    value=max(1, min(200, int(_rv("slack_ops_process_queue_limit", "25") or "25"))),
                    step=1,
                )
                slack_ops_poll_interval_seconds = st.number_input(
                    "Slack Ops Poll Interval Seconds",
                    min_value=2,
                    max_value=300,
                    value=max(2, min(300, int(_rv("slack_ops_poll_interval_seconds", "5") or "5"))),
                    step=1,
                )
                slack_ops_default_role = st.selectbox(
                    "Slack Ops Default Role",
                    options=["viewer", "ops", "admin"],
                    index=["viewer", "ops", "admin"].index(
                        str(_rv("slack_ops_default_role", "ops") or "ops").strip().lower()
                        if str(_rv("slack_ops_default_role", "ops") or "ops").strip().lower() in {"viewer", "ops", "admin"}
                        else "ops"
                    ),
                    help="Fallback app role for Slack users not mapped explicitly.",
                )
            with s2:
                slack_bot_token = st.text_input(
                    "Slack Bot Token",
                    value=_rv("slack_bot_token", ""),
                    type="password",
                )
                slack_app_token = st.text_input(
                    "Slack App Token (Socket Mode)",
                    value=_rv("slack_app_token", ""),
                    type="password",
                    help="Starts with `xapp-`; required for inbound Slack bot conversation via Socket Mode.",
                )
                slack_bot_user_id = st.text_input(
                    "Slack Bot User ID (optional)",
                    value=_rv("slack_bot_user_id", ""),
                    help="Optional `U...` bot ID for mention stripping; auto-detected if empty.",
                )
                slack_signing_secret = st.text_input(
                    "Slack Signing Secret",
                    value=_rv("slack_signing_secret", ""),
                    type="password",
                )
                slack_ops_command_prefix = st.text_input(
                    "Slack Ops Command Prefix (optional)",
                    value=_rv("slack_ops_command_prefix", ""),
                    help="Optional prefix after mention, e.g. `gs` so `@bot gs comp ...`.",
                )
                slack_ops_user_role_map = st.text_area(
                    "Slack Ops User Role Map",
                    value=_rv("slack_ops_user_role_map", ""),
                    height=90,
                    help="Comma-separated `slackUserOrName:role` entries, e.g. `U123:admin,keith:ops`.",
                )
                slack_ops_allowed_channels = st.text_input(
                    "Slack Ops Allowed Channels (CSV)",
                    value=_rv("slack_ops_allowed_channels", ""),
                    help="Comma-separated channel IDs or names allowed to use Slack Ops (empty = unrestricted).",
                )
                slack_ops_allowed_users = st.text_input(
                    "Slack Ops Allowed Users (CSV)",
                    value=_rv("slack_ops_allowed_users", ""),
                    help="Comma-separated Slack user IDs/usernames allowed to use Slack Ops (empty = unrestricted).",
                )
                slack_channel_sync_failures = st.text_input(
                    "Channel Override: Sync Failures",
                    value=_rv("slack_channel_sync_failures", ""),
                    help="Optional: route sync failure alerts to this channel.",
                )
                slack_channel_google_queue_failures = st.text_input(
                    "Channel Override: Google Queue Failures",
                    value=_rv("slack_channel_google_queue_failures", ""),
                )
                slack_channel_integration_queue_failures = st.text_input(
                    "Channel Override: Integration Queue Failures",
                    value=_rv("slack_channel_integration_queue_failures", ""),
                )
                slack_channel_parity_decision = st.text_input(
                    "Channel Override: Parity Decisions",
                    value=_rv("slack_channel_parity_decision", ""),
                )
                slack_channel_followup_overdue = st.text_input(
                    "Channel Override: Follow-up Overdue",
                    value=_rv("slack_channel_followup_overdue", ""),
                )
                slack_channel_warning = st.text_input(
                    "Channel Override: Warning Severity",
                    value=_rv("slack_channel_warning", ""),
                )
                slack_channel_error = st.text_input(
                    "Channel Override: Error Severity",
                    value=_rv("slack_channel_error", ""),
                )
                slack_channel_critical = st.text_input(
                    "Channel Override: Critical Severity",
                    value=_rv("slack_channel_critical", ""),
                )
                slack_channel_system_health_critical = st.text_input(
                    "Channel Override: System Health Critical",
                    value=_rv("slack_channel_system_health_critical", ""),
                )
                slack_daily_report_channel = st.text_input(
                    "Channel Override: Daily Ops Report",
                    value=_rv("slack_daily_report_channel", ""),
                    help="Optional dedicated channel for scheduled daily ops report.",
                )
                slack_channel_backup_events = st.text_input(
                    "Channel Override: Backup Events",
                    value=_rv("slack_channel_backup_events", ""),
                    help="Optional dedicated channel for scheduled backup success/failure notifications.",
                )
                slack_channel_business_reports = st.text_input(
                    "Channel Override: Business Reports",
                    value=_rv("slack_channel_business_reports", ""),
                    help="Optional dedicated channel for manual business status report sends.",
                )
                slack_channel_order_imports = st.text_input(
                    "Channel Override: eBay Order Imports",
                    value=_rv("slack_channel_order_imports", ""),
                    help="Optional dedicated channel for new eBay order import notifications.",
                )
                slack_template_sync_failures = st.text_area(
                    "Template: Sync Failures",
                    value=_rv(
                        "slack_template_sync_failures",
                        (
                            ":warning: *GoldenStackers* sync run `{job_name}` `{status}`\n"
                            "- Env: `{env}`\n"
                            "- Run: `#{run_id}`\n"
                            "- Processed: `{processed}`\n"
                            "- Failed: `{failed}`\n"
                            "- Actor: `{actor}`"
                        ),
                    ),
                    height=140,
                )
                slack_template_google_queue_failures = st.text_area(
                    "Template: Google Queue Failures",
                    value=_rv(
                        "slack_template_google_queue_failures",
                        (
                            ":warning: *GoldenStackers* Google queue job failed permanently\n"
                            "- Env: `{env}`\n"
                            "- Job: `#{job_id}` `{action}`\n"
                            "- Retries: `{retry_count}/{max_retries}`\n"
                            "- Error: `{error}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_integration_queue_failures = st.text_area(
                    "Template: Integration Queue Failures",
                    value=_rv(
                        "slack_template_integration_queue_failures",
                        (
                            ":warning: *GoldenStackers* integration queue job failed permanently\n"
                            "- Env: `{env}`\n"
                            "- Integration: `{integration}`\n"
                            "- Job: `#{job_id}` `{action}`\n"
                            "- Retries: `{retry_count}/{max_retries}`\n"
                            "- Error: `{error}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_parity_decision = st.text_area(
                    "Template: Parity Decision",
                    value=_rv(
                        "slack_template_parity_decision",
                        (
                            ":clipboard: *GoldenStackers* parity release decision `{decision}`\n"
                            "- Env: `{env}`\n"
                            "- Snapshot: `#{snapshot_id}`\n"
                            "- Actor: `{actor}`\n"
                            "- Note: `{note}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_followup_overdue = st.text_area(
                    "Template: Follow-up Overdue",
                    value=_rv(
                        "slack_template_followup_overdue",
                        (
                            ":rotating_light: *GoldenStackers* rollout follow-up overdue\n"
                            "- Env: `{env}`\n"
                            "- Task: `{task_key}`\n"
                            "- Title: `{title}`\n"
                            "- Owner: `{owner}`\n"
                            "- Due: `{due_date}`\n"
                            "- Priority: `{priority}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_system_health_critical = st.text_area(
                    "Template: System Health Critical",
                    value=_rv(
                        "slack_template_system_health_critical",
                        (
                            ":rotating_light: *GoldenStackers* System Health critical signals detected\n"
                            "- Env: `{env}`\n"
                            "- Critical Signals: `{critical_signals}`\n"
                            "- Queue Execute Exceptions: `{queue_execute_exceptions}`\n"
                            "- Terminal Queue Failures: `{terminal_queue_failures}`\n"
                            "- Integration Warnings: `{integration_warnings}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_backup_success = st.text_area(
                    "Template: Backup Success",
                    value=_rv(
                        "slack_template_backup_success",
                        (
                            ":white_check_mark: *GoldenStackers* scheduled DB backup completed\n"
                            "- Env: `{env}`\n"
                            "- File: `{file_name}`\n"
                            "- Size: `{size_bytes}` bytes\n"
                            "- Uploaded to S3: `{uploaded_to_s3}`\n"
                            "- S3 Key: `{s3_key}`\n"
                            "- Local Time: `{local_time}`"
                        ),
                    ),
                    height=120,
                )
                slack_template_backup_failure = st.text_area(
                    "Template: Backup Failure",
                    value=_rv(
                        "slack_template_backup_failure",
                        (
                            ":x: *GoldenStackers* scheduled DB backup failed\n"
                            "- Env: `{env}`\n"
                            "- Error: `{error}`\n"
                            "- Local Time: `{local_time}`"
                        ),
                    ),
                    height=120,
                )
                slack_template_business_status_report = st.text_area(
                    "Template: Business Status Report",
                    value=_rv(
                        "slack_template_business_status_report",
                        (
                            ":bar_chart: *GoldenStackers Business Status* (`{env}`)\n"
                            "- Window: `{window_days}` day(s)\n"
                            "- Sales: `{sales_window_count}` | Gross: `${gross_window}` | Net: `${net_window}`\n"
                            "- Orders: `{orders_window_count}`\n"
                            "- Listings: `{listing_count}` (active `{active_count}`, draft `{draft_count}`)\n"
                            "- Low stock: `{low_stock_count}` | Unlisted: `{unlisted_count}`\n"
                            "- As of UTC: `{as_of_utc}`"
                        ),
                    ),
                    height=120,
                )
                slack_template_inventory_risk_report = st.text_area(
                    "Template: Inventory Risk Report",
                    value=_rv(
                        "slack_template_inventory_risk_report",
                        (
                            ":package: *GoldenStackers Inventory Risk* (`{env}`)\n"
                            "- Low stock items: `{low_stock_count}`\n"
                            "- Unlisted products: `{unlisted_count}`\n"
                            "- Draft listings: `{draft_count}`\n"
                            "- Active listings: `{active_count}`\n"
                            "- Inventory cost basis: `${inventory_cost}`\n"
                            "- As of UTC: `{as_of_utc}`"
                        ),
                    ),
                    height=120,
                )
                slack_template_order_imported = st.text_area(
                    "Template: eBay Order Imported",
                    value=_rv(
                        "slack_template_order_imported",
                        (
                            ":package: *New eBay order imported*\n"
                            "- Env: `{env}`\n"
                            "- Order: `{order_id}`\n"
                            "- Buyer: `{buyer}`\n"
                            "- Status: `{status}`\n"
                            "- Total: `${total}` (shipping `${shipping}`, tax `${tax}`)\n"
                            "- Items: `{line_item_count}`\n"
                            "- Shipping service: `{shipping_service}`\n"
                            "- Ship to: `{shipping_address}`\n"
                            "- Created: `{created_at}`"
                        ),
                    ),
                    height=140,
                )
            st.markdown("##### Fee Coverage Health (Daily Report Input)")
            st.caption(
                "Live view of normalized eBay fee-source coverage that feeds daily-report alerting thresholds."
            )
            try:
                fee_health = _build_normalized_fee_coverage_admin_summary(
                    repo,
                    lookback_weeks=int(slack_daily_report_fee_lookback_weeks),
                    threshold_percent=float(slack_daily_report_fee_threshold_pct),
                    min_consecutive_weeks=int(slack_daily_report_fee_consecutive_weeks),
                )
                f1, f2, f3 = st.columns(3)
                f1.metric(
                    "Latest Week Coverage",
                    f"{float(fee_health.get('latest_week_coverage_pct') or 0.0):.2f}%",
                )
                f2.metric(
                    "Consecutive Below Threshold",
                    int(fee_health.get("consecutive_below") or 0),
                )
                f3.metric(
                    "Alert Triggered",
                    "yes" if bool(fee_health.get("triggered")) else "no",
                )
                if bool(fee_health.get("triggered")):
                    st.warning(
                        "Coverage alert would trigger under current settings."
                        f" Threshold={float(fee_health.get('threshold_percent') or 0.0):.2f}%"
                        f", required_weeks={int(fee_health.get('min_consecutive_weeks') or 0)}."
                    )
                weekly_rows = fee_health.get("weekly_rows") or []
                if weekly_rows:
                    st.dataframe(pd.DataFrame(weekly_rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("No reconciliation rows found in lookback window.")
            except Exception as exc:
                st.warning(f"Unable to compute fee coverage health preview: {exc}")
            save_slack = st.form_submit_button("Save Slack Notification Settings")
        if save_slack:
            try:
                updates = [
                    ("slack_notifications_enabled", "true" if slack_enabled else "false", "bool", "Master toggle for Slack notifications."),
                    ("slack_default_channel", slack_default_channel.strip(), "str", "Default Slack channel for operational notifications (for example #ops-alerts)."),
                    ("slack_notify_sync_failures", "true" if slack_notify_sync else "false", "bool", "Send Slack notifications for sync failures/partial runs."),
                    ("slack_notify_shipping_exceptions", "true" if slack_notify_shipping else "false", "bool", "Send Slack notifications for shipping exceptions."),
                    ("slack_notify_order_imports", "true" if slack_notify_order_imports else "false", "bool", "Send Slack notifications when new eBay orders are imported."),
                    ("slack_notify_daily_summary", "true" if slack_notify_daily else "false", "bool", "Send one daily Slack operational summary message."),
                    ("app_default_timezone", app_default_timezone.strip() or "America/Denver", "str", "Global default timezone for app display/scheduling defaults."),
                    ("slack_daily_report_enabled", "true" if slack_daily_report_enabled else "false", "bool", "Enable sync-runner scheduled daily operations report delivery."),
                    ("slack_daily_report_timezone", slack_daily_report_timezone.strip() or "America/Denver", "str", "IANA timezone used by daily ops report scheduler."),
                    ("slack_daily_report_local_time", slack_daily_report_local_time.strip() or "08:00", "str", "Local HH:MM trigger for daily ops report scheduler."),
                    ("slack_daily_report_channel", slack_daily_report_channel.strip(), "str", "Optional channel override for scheduled daily ops report."),
                    (
                        "slack_daily_report_normalized_fee_coverage_lookback_weeks",
                        str(int(slack_daily_report_fee_lookback_weeks)),
                        "int",
                        "Lookback window in weeks for normalized eBay fee-source coverage health in daily ops reports.",
                    ),
                    (
                        "slack_daily_report_normalized_fee_coverage_threshold_pct",
                        str(float(slack_daily_report_fee_threshold_pct)),
                        "float",
                        "Minimum weekly normalized fee-source coverage percent before daily-report health alerting triggers.",
                    ),
                    (
                        "slack_daily_report_normalized_fee_coverage_consecutive_weeks",
                        str(int(slack_daily_report_fee_consecutive_weeks)),
                        "int",
                        "Number of consecutive below-threshold weeks required before daily-report fee coverage alert is triggered.",
                    ),
                    ("slack_notify_backup_success", "true" if slack_notify_backup_success else "false", "bool", "Send Slack notification on scheduled backup success."),
                    ("slack_notify_backup_failures", "true" if slack_notify_backup_failures else "false", "bool", "Send Slack notification on scheduled backup failure."),
                    ("slack_channel_backup_events", slack_channel_backup_events.strip(), "str", "Optional channel override for backup success/failure notifications."),
                    ("slack_channel_business_reports", slack_channel_business_reports.strip(), "str", "Optional channel override for manual business status report notifications."),
                    ("slack_channel_order_imports", slack_channel_order_imports.strip(), "str", "Optional channel override for new eBay order import notifications."),
                    ("slack_notify_google_queue_failures", "true" if slack_notify_queue_failures else "false", "bool", "Send Slack notifications when Google integration queue jobs hit terminal failure."),
                    ("slack_notify_integration_queue_failures", "true" if slack_notify_integration_queue_failures else "false", "bool", "Send Slack notifications when any integration queue job hits terminal failure."),
                    ("slack_notify_ebay_oauth_refresh_failures", "true" if slack_notify_ebay_oauth_refresh_failures else "false", "bool", "Send Slack notifications when sync-runner eBay OAuth auto-refresh fails."),
                    ("slack_notify_parity_decisions", "true" if slack_notify_parity_decisions else "false", "bool", "Send Slack notifications when workspace parity release decisions are recorded."),
                    ("slack_notify_followup_overdue", "true" if slack_notify_followup_overdue else "false", "bool", "Allow sending Slack notifications for overdue workspace rollout follow-up tasks."),
                    ("slack_notify_system_health_critical", "true" if slack_notify_system_health_critical else "false", "bool", "Send Slack notifications when System Health critical-signal thresholds are breached."),
                    ("slack_daily_summary_cron", slack_daily_cron.strip(), "str", "Cron expression for daily summary schedule (UTC)."),
                    ("slack_http_timeout_seconds", str(int(slack_timeout)), "int", "Timeout for Slack API requests."),
                    ("slack_queue_enabled", "true" if slack_queue_enabled else "false", "bool", "Enable/disable Slack delivery retry queue on post failures."),
                    ("slack_queue_max_retries", str(int(slack_queue_max_retries)), "int", "Maximum retry attempts per queued Slack delivery."),
                    ("slack_queue_backoff_base_seconds", str(int(slack_queue_backoff_base)), "int", "Base backoff seconds for Slack retry queue scheduling."),
                    ("slack_queue_backoff_max_seconds", str(int(slack_queue_backoff_max)), "int", "Maximum backoff seconds for Slack retry queue scheduling."),
                    ("health_auto_alert_critical_enabled", "true" if health_auto_alert_critical_enabled else "false", "bool", "Enable automatic System Health critical-signal alert dispatch."),
                    ("health_auto_alert_cooldown_minutes", str(int(health_auto_alert_cooldown_minutes)), "int", "Cooldown minutes before repeating identical System Health critical alerts."),
                    ("slack_ops_runner_enabled", "true" if slack_ops_runner_enabled else "false", "bool", "Enable Slack Socket Mode inbound command runner."),
                    ("slack_ops_enabled", "true" if slack_ops_enabled else "false", "bool", "Global enable/disable for Slack ops command ingestion."),
                    ("slack_ops_write_actions_require_approval", "true" if slack_ops_write_actions_require_approval else "false", "bool", "Require in-app approval for write-intent Slack ops actions."),
                    ("slack_ops_ai_assist_enabled", "true" if slack_ops_ai_assist_enabled else "false", "bool", "Enable AI assist for Slack ops intake/comp intents."),
                    ("slack_ops_ai_auto_reply_enabled", "true" if slack_ops_ai_auto_reply_enabled else "false", "bool", "Post AI summaries back to Slack thread/channel for supported intents."),
                    ("slack_ops_process_queue_enabled", "true" if slack_ops_process_queue_enabled else "false", "bool", "Allow Slack Ops runner to auto-process due slack_ops queue jobs."),
                    ("slack_ops_process_queue_limit", str(int(slack_ops_process_queue_limit)), "int", "Max due slack_ops queue jobs to process per runner tick."),
                    ("slack_ops_poll_interval_seconds", str(int(slack_ops_poll_interval_seconds)), "int", "Slack Ops runner tick interval in seconds."),
                    ("slack_ops_default_role", str(slack_ops_default_role).strip().lower(), "str", "Fallback app role for Slack users not explicitly mapped."),
                    ("slack_bot_token", slack_bot_token.strip(), "str", "Slack Bot OAuth token used for posting notifications."),
                    ("slack_app_token", slack_app_token.strip(), "str", "Slack App-level token for Socket Mode inbound event handling."),
                    ("slack_bot_user_id", slack_bot_user_id.strip(), "str", "Optional Slack bot user ID (`U...`) for mention stripping."),
                    ("slack_signing_secret", slack_signing_secret.strip(), "str", "Slack signing secret for future interactive/event verification."),
                    ("slack_ops_command_prefix", slack_ops_command_prefix.strip(), "str", "Optional command prefix required after mention in Slack ops requests."),
                    ("slack_ops_user_role_map", slack_ops_user_role_map.strip(), "str", "Comma-separated Slack user-role map (`user_or_id:role`)."),
                    ("slack_ops_allowed_channels", slack_ops_allowed_channels.strip(), "str", "Comma-separated Slack channels allowed for Slack Ops requests."),
                    ("slack_ops_allowed_users", slack_ops_allowed_users.strip(), "str", "Comma-separated Slack users allowed for Slack Ops requests."),
                    ("slack_channel_sync_failures", slack_channel_sync_failures.strip(), "str", "Optional channel override for sync failure alerts."),
                    ("slack_channel_google_queue_failures", slack_channel_google_queue_failures.strip(), "str", "Optional channel override for Google queue failure alerts."),
                    ("slack_channel_integration_queue_failures", slack_channel_integration_queue_failures.strip(), "str", "Optional channel override for integration queue failure alerts."),
                    ("slack_channel_parity_decision", slack_channel_parity_decision.strip(), "str", "Optional channel override for parity release decision alerts."),
                    ("slack_channel_followup_overdue", slack_channel_followup_overdue.strip(), "str", "Optional channel override for overdue rollout follow-up alerts."),
                    ("slack_channel_warning", slack_channel_warning.strip(), "str", "Optional channel override for warning-severity alerts."),
                    ("slack_channel_error", slack_channel_error.strip(), "str", "Optional channel override for error-severity alerts."),
                    ("slack_channel_critical", slack_channel_critical.strip(), "str", "Optional channel override for critical-severity alerts."),
                    ("slack_channel_system_health_critical", slack_channel_system_health_critical.strip(), "str", "Optional channel override for System Health critical alerts."),
                    ("slack_template_sync_failures", slack_template_sync_failures.strip(), "str", "Template for sync failure/partial Slack alerts."),
                    ("slack_template_google_queue_failures", slack_template_google_queue_failures.strip(), "str", "Template for terminal Google queue failure Slack alerts."),
                    ("slack_template_integration_queue_failures", slack_template_integration_queue_failures.strip(), "str", "Template for terminal integration queue failure Slack alerts."),
                    ("slack_template_parity_decision", slack_template_parity_decision.strip(), "str", "Template for workspace parity release decision alerts."),
                    ("slack_template_followup_overdue", slack_template_followup_overdue.strip(), "str", "Template for overdue workspace rollout follow-up alerts."),
                    ("slack_template_system_health_critical", slack_template_system_health_critical.strip(), "str", "Template for System Health critical threshold alerts."),
                    ("slack_template_backup_success", slack_template_backup_success.strip(), "str", "Template for successful scheduled backup notifications."),
                    ("slack_template_backup_failure", slack_template_backup_failure.strip(), "str", "Template for failed scheduled backup notifications."),
                    ("slack_template_business_status_report", slack_template_business_status_report.strip(), "str", "Template for manual business status report notifications."),
                    ("slack_template_inventory_risk_report", slack_template_inventory_risk_report.strip(), "str", "Template for manual inventory risk report notifications."),
                    ("slack_template_order_imported", slack_template_order_imported.strip(), "str", "Template for new eBay order imported notifications."),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Slack notification settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save Slack notification settings: {exc}")

        st.markdown("#### Notification Routing")
        st.caption("Choose delivery route per event type. Email routing is scoped and stored now; send pipeline will be enabled in a future milestone.")
        route_options = ["slack", "email", "both", "disabled"]

        def _route_index(value: str) -> int:
            raw = str(value or "").strip().lower()
            if raw in route_options:
                return route_options.index(raw)
            return 0

        with st.form("admin_notification_routing_form"):
            r1, r2 = st.columns(2)
            with r1:
                route_sync_failures = st.selectbox(
                    "Route: Sync Failures",
                    options=route_options,
                    index=_route_index(_rv("notification_route_sync_failures", "slack")),
                )
                route_daily_report = st.selectbox(
                    "Route: Daily Report",
                    options=route_options,
                    index=_route_index(_rv("notification_route_daily_report", "slack")),
                )
                route_backup_events = st.selectbox(
                    "Route: Backup Events",
                    options=route_options,
                    index=_route_index(_rv("notification_route_backup_events", "slack")),
                )
            with r2:
                route_system_health_critical = st.selectbox(
                    "Route: System Health Critical",
                    options=route_options,
                    index=_route_index(_rv("notification_route_system_health_critical", "slack")),
                )
                route_business_reports = st.selectbox(
                    "Route: Business Reports",
                    options=route_options,
                    index=_route_index(_rv("notification_route_business_reports", "slack")),
                )
                notification_email_enabled = st.checkbox(
                    "Enable Notification Email Pipeline (future)",
                    value=_rb("notification_email_enabled", False),
                )
                notification_email_recipients_csv = st.text_input(
                    "Notification Email Recipients (CSV, future)",
                    value=_rv("notification_email_recipients_csv", ""),
                    placeholder="ops@goldenstackers.com, owner@goldenstackers.com",
                )
            save_notification_routing = st.form_submit_button("Save Notification Routing")
        if save_notification_routing:
            try:
                route_updates = [
                    ("notification_route_sync_failures", route_sync_failures, "str", "Notification route for sync failure events."),
                    ("notification_route_daily_report", route_daily_report, "str", "Notification route for daily report events."),
                    ("notification_route_backup_events", route_backup_events, "str", "Notification route for backup events."),
                    ("notification_route_system_health_critical", route_system_health_critical, "str", "Notification route for system-health critical events."),
                    ("notification_route_business_reports", route_business_reports, "str", "Notification route for manual business reports."),
                    ("notification_email_enabled", "true" if notification_email_enabled else "false", "bool", "Enable notification email pipeline."),
                    ("notification_email_recipients_csv", notification_email_recipients_csv.strip(), "str", "Default notification email recipients (CSV)."),
                ]
                for key, value, value_type, description in route_updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=str(value or "").strip(),
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Notification routing settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save notification routing settings: {exc}")

        st.markdown("#### Notification Outbox Controls")
        st.caption(
            "Configure retry/backoff/retention behavior for queued notifications and run manual outbox processing/cleanup."
        )
        with st.form("admin_notification_outbox_controls_form"):
            o1, o2, o3 = st.columns(3)
            with o1:
                outbox_runner_enabled = st.checkbox(
                    "Enable Outbox Runner",
                    value=_rb("notification_outbox_runner_enabled", False),
                )
                outbox_runner_limit = st.number_input(
                    "Runner Batch Limit",
                    min_value=1,
                    max_value=500,
                    value=max(1, min(500, int(_rv("notification_outbox_runner_limit", "50") or "50"))),
                    step=1,
                )
                outbox_backoff_base = st.number_input(
                    "Backoff Base Seconds",
                    min_value=10,
                    max_value=3600,
                    value=max(
                        10,
                        min(3600, int(_rv("notification_outbox_backoff_base_seconds", "60") or "60")),
                    ),
                    step=10,
                )
                outbox_backoff_max = st.number_input(
                    "Backoff Max Seconds",
                    min_value=10,
                    max_value=86400,
                    value=max(
                        10,
                        min(86400, int(_rv("notification_outbox_backoff_max_seconds", "3600") or "3600")),
                    ),
                    step=30,
                )
            with o2:
                outbox_retain_sent_days = st.number_input(
                    "Retention (Sent Days)",
                    min_value=1,
                    max_value=3650,
                    value=max(
                        1,
                        min(3650, int(_rv("notification_outbox_retain_sent_days", "14") or "14")),
                    ),
                    step=1,
                )
                outbox_retain_failed_days = st.number_input(
                    "Retention (Failed Days)",
                    min_value=1,
                    max_value=3650,
                    value=max(
                        1,
                        min(3650, int(_rv("notification_outbox_retain_failed_days", "30") or "30")),
                    ),
                    step=1,
                )
                outbox_cleanup_enabled = st.checkbox(
                    "Enable Daily Cleanup",
                    value=_rb("notification_outbox_cleanup_enabled", True),
                )
                outbox_cleanup_timezone = st.text_input(
                    "Cleanup Timezone",
                    value=_rv("notification_outbox_cleanup_timezone", _rv("app_default_timezone", "America/Denver")),
                )
                outbox_cleanup_local_time = st.text_input(
                    "Cleanup Local Time (HH:MM)",
                    value=_rv("notification_outbox_cleanup_local_time", "03:15"),
                )
            with o3:
                st.caption("Manual Actions")
                run_now_limit = st.number_input(
                    "Run-Now Limit",
                    min_value=1,
                    max_value=500,
                    value=max(1, min(500, int(_rv("notification_outbox_runner_limit", "50") or "50"))),
                    step=1,
                    key="admin_notification_outbox_run_now_limit",
                )
                run_now = st.form_submit_button("Run Outbox Now")
                cleanup_now = st.form_submit_button("Run Cleanup Now")
                save_outbox_controls = st.form_submit_button("Save Outbox Controls")
        if save_outbox_controls:
            try:
                outbox_updates = [
                    ("notification_outbox_runner_enabled", "true" if outbox_runner_enabled else "false", "bool", "Enable sync-runner outbox processor."),
                    ("notification_outbox_runner_limit", str(int(outbox_runner_limit)), "int", "Max queued outbox rows to process per sync-runner pass."),
                    ("notification_outbox_backoff_base_seconds", str(int(outbox_backoff_base)), "int", "Base retry backoff seconds for notification outbox."),
                    ("notification_outbox_backoff_max_seconds", str(int(outbox_backoff_max)), "int", "Max retry backoff seconds for notification outbox."),
                    ("notification_outbox_retain_sent_days", str(int(outbox_retain_sent_days)), "int", "Retention window for sent outbox rows."),
                    ("notification_outbox_retain_failed_days", str(int(outbox_retain_failed_days)), "int", "Retention window for failed outbox rows."),
                    ("notification_outbox_cleanup_enabled", "true" if outbox_cleanup_enabled else "false", "bool", "Enable daily outbox retention cleanup."),
                    ("notification_outbox_cleanup_timezone", str(outbox_cleanup_timezone or "").strip() or "America/Denver", "str", "IANA timezone for outbox cleanup schedule."),
                    ("notification_outbox_cleanup_local_time", str(outbox_cleanup_local_time or "").strip() or "03:15", "str", "Local HH:MM time for outbox cleanup schedule."),
                ]
                for key, value, value_type, description in outbox_updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Notification outbox controls saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save outbox controls: {exc}")
        if run_now:
            try:
                result = process_due_notification_outbox(
                    repo,
                    environment=settings.app_env,
                    actor=user.username,
                    limit=int(run_now_limit),
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="notification_outbox",
                    action="manual_process_due",
                    status="success",
                    details={
                        "due": int(result.get("due") or 0),
                        "sent": int(result.get("sent") or 0),
                        "failed": int(result.get("failed") or 0),
                        "limit": int(run_now_limit),
                    },
                )
                st.success(
                    f"Outbox run completed: due={int(result.get('due') or 0)} "
                    f"sent={int(result.get('sent') or 0)} failed={int(result.get('failed') or 0)}"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to run outbox now: {exc}")
        if cleanup_now:
            try:
                result = cleanup_notification_outbox_retention(
                    repo,
                    environment=settings.app_env,
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="notification_outbox",
                    action="manual_cleanup",
                    status="success",
                    details={
                        "deleted_total": int(result.get("deleted_total") or 0),
                        "deleted_sent": int(result.get("deleted_sent") or 0),
                        "deleted_failed": int(result.get("deleted_failed") or 0),
                    },
                )
                st.success(
                    f"Cleanup completed: deleted_total={int(result.get('deleted_total') or 0)} "
                    f"(sent={int(result.get('deleted_sent') or 0)}, failed={int(result.get('deleted_failed') or 0)})"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to run outbox cleanup now: {exc}")

        try:
            outbox_preview_rows = repo.list_notification_outbox(
                environment=settings.app_env,
                statuses={"queued", "retrying", "processing", "failed", "sent"},
                limit=100,
            )
        except Exception:
            outbox_preview_rows = []
        if outbox_preview_rows:
            status_counts = Counter(str(getattr(row, "status", "") or "").strip().lower() for row in outbox_preview_rows)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Outbox Queued", int(status_counts.get("queued", 0)))
            m2.metric("Outbox Retrying", int(status_counts.get("retrying", 0)))
            m3.metric("Outbox Failed", int(status_counts.get("failed", 0)))
            m4.metric("Outbox Sent", int(status_counts.get("sent", 0)))
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": int(getattr(row, "id", 0) or 0),
                            "status": str(getattr(row, "status", "") or ""),
                            "channel": str(getattr(row, "channel", "") or ""),
                            "event_type": str(getattr(row, "event_type", "") or ""),
                            "attempt_count": int(getattr(row, "attempt_count", 0) or 0),
                            "max_attempts": int(getattr(row, "max_attempts", 0) or 0),
                            "next_attempt_at": getattr(row, "next_attempt_at", None),
                            "last_error": str(getattr(row, "last_error", "") or "")[:220],
                        }
                        for row in outbox_preview_rows[:50]
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No notification outbox rows found.")

        st.markdown("#### Lifecycle Archive Retention Controls")
        st.caption(
            "Configure archived-record retention windows for media/listings/lots/products and run manual cleanup."
        )
        if "admin_lifecycle_cleanup_last_result" not in st.session_state:
            st.session_state["admin_lifecycle_cleanup_last_result"] = None
        if "admin_lifecycle_cleanup_last_error" not in st.session_state:
            st.session_state["admin_lifecycle_cleanup_last_error"] = ""
        if "admin_lifecycle_cleanup_last_ran_at" not in st.session_state:
            st.session_state["admin_lifecycle_cleanup_last_ran_at"] = ""
        with st.form("admin_lifecycle_retention_controls_form"):
            lc1, lc2, lc3 = st.columns(3)
            with lc1:
                lifecycle_cleanup_enabled = st.checkbox(
                    "Enable Daily Lifecycle Cleanup",
                    value=_rb("lifecycle_archive_cleanup_enabled", False),
                )
                lifecycle_cleanup_timezone = st.text_input(
                    "Lifecycle Cleanup Timezone",
                    value=_rv("lifecycle_archive_cleanup_timezone", _rv("app_default_timezone", "America/Denver")),
                )
                lifecycle_cleanup_local_time = st.text_input(
                    "Lifecycle Cleanup Local Time (HH:MM)",
                    value=_rv("lifecycle_archive_cleanup_local_time", "03:45"),
                )
            with lc2:
                lifecycle_media_retain_days = st.number_input(
                    "Media Archive Retain Days",
                    min_value=1,
                    max_value=3650,
                    value=max(1, min(3650, int(_rv("lifecycle_media_archive_retain_days", "180") or "180"))),
                    step=1,
                )
                lifecycle_listing_retain_days = st.number_input(
                    "Listing Archive Retain Days",
                    min_value=1,
                    max_value=3650,
                    value=max(1, min(3650, int(_rv("lifecycle_listing_archive_retain_days", "365") or "365"))),
                    step=1,
                )
            with lc3:
                lifecycle_lot_retain_days = st.number_input(
                    "Lot Archive Retain Days",
                    min_value=1,
                    max_value=3650,
                    value=max(1, min(3650, int(_rv("lifecycle_lot_archive_retain_days", "365") or "365"))),
                    step=1,
                )
                lifecycle_product_retain_days = st.number_input(
                    "Product Archive Retain Days",
                    min_value=1,
                    max_value=3650,
                    value=max(1, min(3650, int(_rv("lifecycle_product_archive_retain_days", "365") or "365"))),
                    step=1,
                )
                st.caption("Manual Actions")
                lifecycle_run_now = st.form_submit_button("Run Lifecycle Cleanup Now")
                lifecycle_save_controls = st.form_submit_button("Save Lifecycle Controls")
        if lifecycle_save_controls:
            try:
                lifecycle_updates = [
                    (
                        "lifecycle_archive_cleanup_enabled",
                        "true" if lifecycle_cleanup_enabled else "false",
                        "bool",
                        "Enable daily archived-record lifecycle cleanup.",
                    ),
                    (
                        "lifecycle_archive_cleanup_timezone",
                        str(lifecycle_cleanup_timezone or "").strip() or "America/Denver",
                        "str",
                        "IANA timezone for lifecycle cleanup schedule.",
                    ),
                    (
                        "lifecycle_archive_cleanup_local_time",
                        str(lifecycle_cleanup_local_time or "").strip() or "03:45",
                        "str",
                        "Local HH:MM time for lifecycle cleanup schedule.",
                    ),
                    (
                        "lifecycle_media_archive_retain_days",
                        str(int(lifecycle_media_retain_days)),
                        "int",
                        "Retention window for archived media assets.",
                    ),
                    (
                        "lifecycle_listing_archive_retain_days",
                        str(int(lifecycle_listing_retain_days)),
                        "int",
                        "Retention window for archived marketplace listings.",
                    ),
                    (
                        "lifecycle_lot_archive_retain_days",
                        str(int(lifecycle_lot_retain_days)),
                        "int",
                        "Retention window for archived purchase lots.",
                    ),
                    (
                        "lifecycle_product_archive_retain_days",
                        str(int(lifecycle_product_retain_days)),
                        "int",
                        "Retention window for archived products.",
                    ),
                ]
                for key, value, value_type, description in lifecycle_updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Lifecycle retention controls saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save lifecycle retention controls: {exc}")
        if lifecycle_run_now:
            try:
                result = cleanup_lifecycle_retention(
                    repo,
                    actor=user.username,
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="lifecycle_retention",
                    action="manual_cleanup",
                    status="success",
                    details={
                        "retain_days_media": int(result.get("retain_days_media") or 0),
                        "retain_days_listing": int(result.get("retain_days_listing") or 0),
                        "retain_days_lot": int(result.get("retain_days_lot") or 0),
                        "retain_days_product": int(result.get("retain_days_product") or 0),
                        "deleted_archived_media": int(result.get("deleted_archived_media") or 0),
                        "deleted_archived_listings": int(result.get("deleted_archived_listings") or 0),
                        "deleted_archived_lots": int(result.get("deleted_archived_lots") or 0),
                        "deleted_archived_products": int(result.get("deleted_archived_products") or 0),
                        "skipped_listings_with_dependencies": int(
                            result.get("skipped_listings_with_dependencies") or 0
                        ),
                        "skipped_lots_with_dependencies": int(
                            result.get("skipped_lots_with_dependencies") or 0
                        ),
                        "skipped_products_with_dependencies": int(
                            result.get("skipped_products_with_dependencies") or 0
                        ),
                    },
                )
                st.session_state["admin_lifecycle_cleanup_last_result"] = {
                    **result,
                    "status": "success",
                    "actor": user.username,
                    "env": settings.app_env,
                }
                st.session_state["admin_lifecycle_cleanup_last_error"] = ""
                st.session_state["admin_lifecycle_cleanup_last_ran_at"] = utcnow_naive().isoformat(timespec="seconds")
                st.success(
                    "Lifecycle cleanup complete: "
                    f"deleted_media={int(result.get('deleted_archived_media') or 0)}, "
                    f"deleted_listings={int(result.get('deleted_archived_listings') or 0)}, "
                    f"deleted_lots={int(result.get('deleted_archived_lots') or 0)}, "
                    f"deleted_products={int(result.get('deleted_archived_products') or 0)}, "
                    f"skipped_with_dependencies="
                    f"{int(result.get('skipped_listings_with_dependencies') or 0) + int(result.get('skipped_lots_with_dependencies') or 0) + int(result.get('skipped_products_with_dependencies') or 0)}"
                )
                st.rerun()
            except Exception as exc:
                st.session_state["admin_lifecycle_cleanup_last_result"] = None
                st.session_state["admin_lifecycle_cleanup_last_error"] = str(exc)
                st.session_state["admin_lifecycle_cleanup_last_ran_at"] = utcnow_naive().isoformat(timespec="seconds")
                st.error(f"Unable to run lifecycle cleanup now: {exc}")
        last_cleanup_result = st.session_state.get("admin_lifecycle_cleanup_last_result")
        last_cleanup_error = str(st.session_state.get("admin_lifecycle_cleanup_last_error") or "").strip()
        last_cleanup_ran_at = str(st.session_state.get("admin_lifecycle_cleanup_last_ran_at") or "").strip()
        if last_cleanup_result or last_cleanup_error:
            st.markdown("##### Last Lifecycle Cleanup Run")
            if last_cleanup_ran_at:
                st.caption(f"Last run at (UTC): {last_cleanup_ran_at}")
            if last_cleanup_result:
                st.code(
                    json.dumps(last_cleanup_result, indent=2),
                    language="json",
                )
            elif last_cleanup_error:
                st.error(f"Last cleanup error: {last_cleanup_error}")

        st.markdown("#### Business Status Reports")
        st.caption("Send operational status snapshots to Slack on demand.")
        route_business_reports_now = str(_rv("notification_route_business_reports", "slack") or "slack").strip().lower()
        if route_business_reports_now in {"disabled", "email"}:
            st.info(
                "Business report route is set to "
                f"`{route_business_reports_now}`. Switch route to `slack` or `both` to send Slack reports."
            )
        br1, br2, br3 = st.columns(3)
        send_daily_business_report = br1.button("Send Daily Business Snapshot", key="admin_send_daily_business_snapshot_btn")
        send_weekly_business_report = br2.button("Send Weekly Business Summary", key="admin_send_weekly_business_summary_btn")
        send_inventory_risk_report = br3.button("Send Inventory Risk Snapshot", key="admin_send_inventory_risk_snapshot_btn")

        if send_daily_business_report or send_weekly_business_report or send_inventory_risk_report:
            if route_business_reports_now not in {"slack", "both"}:
                st.error("Slack delivery is disabled by routing for business reports.")
            else:
                try:
                    if send_weekly_business_report:
                        context = _build_business_status_context(repo, days=7)
                        event_type = "business_status_report"
                        default_template = (
                            ":bar_chart: *GoldenStackers Weekly Business Summary* (`{env}`)\n"
                            "- Window: `{window_days}` day(s)\n"
                            "- Sales: `{sales_window_count}` | Gross: `${gross_window}` | Net: `${net_window}`\n"
                            "- Orders: `{orders_window_count}`\n"
                            "- Inventory cost basis: `${inventory_cost}`\n"
                            "- Listings: `{listing_count}` (active `{active_count}`, draft `{draft_count}`)\n"
                            "- Low stock items: `{low_stock_count}` | Unlisted products: `{unlisted_count}`\n"
                            "- As of UTC: `{as_of_utc}`"
                        )
                    elif send_inventory_risk_report:
                        context = _build_business_status_context(repo, days=7)
                        event_type = "inventory_risk_report"
                        default_template = (
                            ":package: *GoldenStackers Inventory Risk Snapshot* (`{env}`)\n"
                            "- Low stock items: `{low_stock_count}`\n"
                            "- Unlisted products: `{unlisted_count}`\n"
                            "- Draft listings: `{draft_count}`\n"
                            "- Active listings: `{active_count}`\n"
                            "- Inventory cost basis: `${inventory_cost}`\n"
                            "- Sales (last `{window_days}`d): `{sales_window_count}` (net `${net_window}`)\n"
                            "- As of UTC: `{as_of_utc}`"
                        )
                    else:
                        context = _build_business_status_context(repo, days=1)
                        event_type = "business_status_report"
                        default_template = (
                            ":clipboard: *GoldenStackers Daily Business Snapshot* (`{env}`)\n"
                            "- Sales 24h: `{sales_window_count}` | Gross: `${gross_window}` | Net: `${net_window}`\n"
                            "- Orders 24h: `{orders_window_count}`\n"
                            "- Products: `{product_count}` | Listings: `{listing_count}`\n"
                            "- Draft listings: `{draft_count}` | Low stock: `{low_stock_count}`\n"
                            "- Inventory cost basis: `${inventory_cost}`\n"
                            "- As of UTC: `{as_of_utc}`"
                        )
                    text = build_slack_alert_text(
                        repo,
                        event_type=event_type,
                        default_template=default_template,
                        context=context,
                    )
                    dispatch_result = dispatch_slack_alert(
                        repo,
                        actor=user.username,
                        text=text,
                        event_type=event_type,
                        severity="info",
                        override_channel=str(_rv("slack_channel_business_reports", "") or "").strip(),
                    )
                    st.success(
                        f"Business status report dispatch result: `{dispatch_result.get('status', 'unknown')}` "
                        f"(channel: `{dispatch_result.get('channel', '')}`)"
                    )
                except Exception as exc:
                    st.error(f"Unable to send business status report: {exc}")

        st.markdown("#### Notification Dry-Run Preview")
        st.caption("Preview resolved Slack payload text and target channel before send.")
        preview_options = [
            "Daily Business Snapshot",
            "Weekly Business Summary",
            "Inventory Risk Snapshot",
        ]
        preview_choice = st.selectbox(
            "Preview Report Type",
            options=preview_options,
            index=0,
            key="admin_business_report_preview_choice",
        )
        preview_days = 1
        preview_event_type = "business_status_report"
        preview_default_template = (
            ":clipboard: *GoldenStackers Daily Business Snapshot* (`{env}`)\n"
            "- Sales 24h: `{sales_window_count}` | Gross: `${gross_window}` | Net: `${net_window}`\n"
            "- Orders 24h: `{orders_window_count}`\n"
            "- Products: `{product_count}` | Listings: `{listing_count}`\n"
            "- Draft listings: `{draft_count}` | Low stock: `{low_stock_count}`\n"
            "- Inventory cost basis: `${inventory_cost}`\n"
            "- As of UTC: `{as_of_utc}`"
        )
        if preview_choice == "Weekly Business Summary":
            preview_days = 7
            preview_default_template = (
                ":bar_chart: *GoldenStackers Weekly Business Summary* (`{env}`)\n"
                "- Window: `{window_days}` day(s)\n"
                "- Sales: `{sales_window_count}` | Gross: `${gross_window}` | Net: `${net_window}`\n"
                "- Orders: `{orders_window_count}`\n"
                "- Inventory cost basis: `${inventory_cost}`\n"
                "- Listings: `{listing_count}` (active `{active_count}`, draft `{draft_count}`)\n"
                "- Low stock items: `{low_stock_count}` | Unlisted products: `{unlisted_count}`\n"
                "- As of UTC: `{as_of_utc}`"
            )
        elif preview_choice == "Inventory Risk Snapshot":
            preview_days = 7
            preview_event_type = "inventory_risk_report"
            preview_default_template = (
                ":package: *GoldenStackers Inventory Risk Snapshot* (`{env}`)\n"
                "- Low stock items: `{low_stock_count}`\n"
                "- Unlisted products: `{unlisted_count}`\n"
                "- Draft listings: `{draft_count}`\n"
                "- Active listings: `{active_count}`\n"
                "- Inventory cost basis: `${inventory_cost}`\n"
                "- Sales (last `{window_days}`d): `{sales_window_count}` (net `${net_window}`)\n"
                "- As of UTC: `{as_of_utc}`"
            )
        preview_context = _build_business_status_context(repo, days=preview_days)
        preview_text = build_slack_alert_text(
            repo,
            event_type=preview_event_type,
            default_template=preview_default_template,
            context=preview_context,
        )
        preview_channel = str(_rv("slack_channel_business_reports", "") or "").strip()
        if not preview_channel:
            preview_channel = str(_rv("slack_default_channel", "") or "").strip()
        st.code(
            (
                f"route={route_business_reports_now or 'slack'}\n"
                f"event_type={preview_event_type}\n"
                f"channel={preview_channel or '(unresolved)'}\n\n"
                f"{preview_text}"
            ),
            language="text",
        )
        st.text_area(
            "Payload Text (copy)",
            value=preview_text,
            height=180,
            key="admin_business_report_preview_payload_text",
            help="Copy this rendered payload text directly for review or external notes.",
        )
        pv1, pv2 = st.columns(2)
        send_preview_now = pv1.button(
            "Send This Preview Now",
            key="admin_send_business_report_preview_now_btn",
        )
        pv2.download_button(
            "Copy/Download Payload (.txt)",
            data=preview_text.encode("utf-8"),
            file_name=f"business_report_preview_{preview_event_type}_{settings.app_env}.txt",
            mime="text/plain",
            key="admin_download_business_report_preview_payload_btn",
        )
        if send_preview_now:
            if route_business_reports_now not in {"slack", "both"}:
                st.error("Slack delivery is disabled by routing for business reports.")
            elif not preview_channel:
                st.error("No Slack channel resolved for preview send.")
            else:
                try:
                    dispatch_result = dispatch_slack_alert(
                        repo,
                        actor=user.username,
                        text=preview_text,
                        event_type=preview_event_type,
                        severity="info",
                        override_channel=preview_channel,
                    )
                    st.success(
                        f"Preview dispatch result: `{dispatch_result.get('status', 'unknown')}` "
                        f"(channel: `{dispatch_result.get('channel', '')}`)"
                    )
                except Exception as exc:
                    st.error(f"Unable to send preview payload: {exc}")
        if not preview_channel:
            st.warning("No Slack channel resolved. Set `slack_channel_business_reports` or `slack_default_channel`.")

        with st.form("admin_slack_test_send_form"):
            test_channel = st.text_input("Test Channel (optional, default uses slack_default_channel)", value="")
            test_message = st.text_area(
                "Test Message",
                value=f"GoldenStackers Slack test from `{settings.app_env}` at `{utcnow_naive().isoformat()}`.",
                height=100,
            )
            send_test_slack = st.form_submit_button("Send Test Slack Message")
        if send_test_slack:
            try:
                result = send_slack_message(
                    repo,
                    text=test_message.strip(),
                    channel=test_channel.strip(),
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="slack",
                    action="test_send",
                    status="success",
                    details={
                        "channel": result.get("channel", ""),
                        "ts": result.get("ts", ""),
                        "env": settings.app_env,
                    },
                )
                st.success(f"Slack test sent to `{result.get('channel', '')}`.")
            except Exception as exc:
                try:
                    repo.log_integration_event(
                        actor=user.username,
                        integration="slack",
                        action="test_send",
                        status="failed",
                        details={"error": str(exc), "env": settings.app_env},
                    )
                except Exception:
                    pass
                st.error(f"Slack test send failed: {exc}")

        st.markdown("#### Test eBay Order Import Alert")
        test_order_channel = str(_rv("slack_channel_order_imports", "") or "").strip()
        if not test_order_channel:
            test_order_channel = str(_rv("slack_default_channel", "") or "").strip()
        sample_order_context = {
            "env": settings.app_env,
            "order_id": "TEST-EBAY-ORDER-001",
            "buyer": "sample-buyer",
            "status": "not_shipped",
            "total": "149.99",
            "shipping": "9.99",
            "tax": "5.21",
            "line_item_count": 2,
            "shipping_service": "USPS Ground Advantage",
            "shipping_address": "Golden, CO, US",
            "created_at": utcnow_naive().isoformat(timespec="seconds"),
        }
        sample_order_text = build_slack_alert_text(
            repo,
            event_type="order_imported",
            default_template=(
                ":package: *New eBay order imported*\n"
                "- Env: `{env}`\n"
                "- Order: `{order_id}`\n"
                "- Buyer: `{buyer}`\n"
                "- Status: `{status}`\n"
                "- Total: `${total}` (shipping `${shipping}`, tax `${tax}`)\n"
                "- Items: `{line_item_count}`\n"
                "- Shipping service: `{shipping_service}`\n"
                "- Ship to: `{shipping_address}`\n"
                "- Created: `{created_at}`"
            ),
            context=sample_order_context,
        )
        st.code(
            (
                f"event_type=order_imported\n"
                f"channel={test_order_channel or '(unresolved)'}\n\n"
                f"{sample_order_text}"
            ),
            language="text",
        )
        if st.button("Send Test eBay Order Import Alert", key="admin_send_test_order_import_alert_btn"):
            if not test_order_channel:
                st.error("No Slack channel resolved. Set `slack_channel_order_imports` or `slack_default_channel`.")
            else:
                try:
                    dispatch_result = dispatch_slack_alert(
                        repo,
                        actor=user.username,
                        event_type="order_imported",
                        severity="info",
                        text=sample_order_text,
                        override_channel=test_order_channel,
                    )
                    st.success(
                        f"Test order-import alert dispatched: `{dispatch_result.get('status', 'unknown')}` "
                        f"(channel: `{dispatch_result.get('channel', '')}`)"
                    )
                except Exception as exc:
                    st.error(f"Unable to send test order-import alert: {exc}")

        st.markdown("#### Recent Slack Delivery Events")
        if not load_slack_delivery_events:
            st.caption("Slack delivery event history is deferred. Enable above to load.")
        else:
            slack_audit_rows = _integration_event_rows(lookback_days=14, limit=500)
            slack_events: list[dict[str, str]] = []
            for row in slack_audit_rows:
                try:
                    payload = json.loads(row.changes_json or "{}")
                except Exception:
                    payload = {}
                after = payload.get("after") if isinstance(payload, dict) else {}
                if not isinstance(after, dict):
                    after = {}
                integration_name = str(after.get("integration") or "").strip().lower()
                if not integration_name.startswith("slack"):
                    continue
                details = after.get("details") if isinstance(after.get("details"), dict) else {}
                slack_events.append(
                    {
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                        "actor": row.actor,
                        "integration": integration_name,
                        "action": str(after.get("action") or row.action or ""),
                        "status": str(after.get("status") or ""),
                        "channel": str(details.get("channel") or ""),
                        "ts": str(details.get("ts") or ""),
                        "error": str(details.get("error") or "")[:220],
                    }
                )
            if slack_events:
                st.dataframe(pd.DataFrame(slack_events), use_container_width=True)
            else:
                st.info("No Slack integration events in last 14 days.")

        st.markdown("#### Slack Retry Queue")
        if not load_slack_queue_jobs:
            slack_queue_rows = []
            st.caption("Slack queue table hydration is deferred. Enable above to load.")
        else:
            try:
                slack_queue_rows = repo.list_integration_queue_jobs(
                    environment=settings.app_env,
                    integration="slack",
                    statuses={"queued", "running", "failed", "success"},
                    limit=500,
                )
            except Exception as exc:
                st.error(
                    "Integration queue table is not available yet. "
                    "Run database migrations first (`docker compose run --rm migrate`)."
                )
                st.caption(f"Details: {exc}")
                slack_queue_rows = []
        if slack_queue_rows:
            slack_queue_df = pd.DataFrame(
                [
                    {
                        "id": row.id,
                        "action": row.action,
                        "status": row.status,
                        "retry_count": int(row.retry_count or 0),
                        "max_retries": int(row.max_retries or 0),
                        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
                        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
                        "completed_at": row.completed_at.isoformat() if row.completed_at else "",
                        "requested_by": row.requested_by,
                        "last_error": str(row.last_error or "")[:250],
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                    }
                    for row in slack_queue_rows
                ]
            )
            st.dataframe(slack_queue_df, use_container_width=True)
            sq1, sq2 = st.columns(2)
            with sq1:
                if st.button("Run Due Slack Queue Jobs Now", key="admin_slack_queue_run_due_btn"):
                    try:
                        summary = process_due_integration_queue_jobs(
                            repo,
                            integration="slack",
                            actor=user.username,
                            limit=20,
                        )
                        st.success(
                            f"Processed {summary['processed']} Slack queue job(s): "
                            f"{summary['success']} success, {summary['queued']} re-queued, {summary['failed']} failed."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to run due Slack queue jobs: {exc}")
            with sq2:
                slack_failed_only = [row for row in slack_queue_rows if str(row.status or "").lower() == "failed"]
                if slack_failed_only:
                    row_map = {
                        f"#{row.id} | {row.action} | retry {row.retry_count}/{row.max_retries}": row
                        for row in slack_failed_only
                    }
                    selected_key = st.selectbox(
                        "Retry Failed Slack Job",
                        options=list(row_map.keys()),
                        key="admin_slack_queue_retry_failed_select",
                    )
                    if st.button("Retry Selected Slack Job Now", key="admin_slack_queue_retry_failed_btn"):
                        selected_row = row_map[selected_key]
                        try:
                            repo.update_integration_queue_job(
                                int(selected_row.id),
                                {"status": "queued", "next_attempt_at": utcnow_naive()},
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=int(selected_row.id),
                                actor=user.username,
                            )
                            if ok:
                                st.success(f"Retry succeeded for Slack queue job #{selected_row.id}. {msg}")
                            else:
                                st.warning(f"Retry did not complete successfully for Slack queue job #{selected_row.id}. {msg}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to retry selected Slack queue job: {exc}")
                else:
                    st.info("No failed Slack queue jobs available.")
        elif load_slack_queue_jobs:
            st.info("No Slack queue jobs found for this environment.")

        st.markdown("#### Slack Ops Queue (Bot)")
        st.caption(
            "Slack AI Ops command queue status with pending-approval visibility and direct triage controls."
        )
        if not load_slack_queue_jobs:
            slack_ops_queue_rows = []
            st.caption("Slack Ops queue hydration is deferred. Enable above to load.")
        else:
            try:
                slack_ops_queue_rows = repo.list_integration_queue_jobs(
                    environment=settings.app_env,
                    integration="slack_ops",
                    statuses={"queued", "running", "blocked", "failed", "success"},
                    limit=500,
                )
            except Exception as exc:
                st.error(
                    "Slack Ops queue table is not available yet. "
                    "Run database migrations first (`docker compose run --rm migrate`)."
                )
                st.caption(f"Details: {exc}")
                slack_ops_queue_rows = []
        if slack_ops_queue_rows:
            snapshot = _slack_ops_queue_snapshot(slack_ops_queue_rows, now=utcnow_naive())
            so1, so2, so3, so4, so5, so6 = st.columns(6)
            so1.metric("Total", int(snapshot["total_count"]))
            so2.metric("Queued", int(snapshot["queued_count"]))
            so3.metric("Running", int(snapshot["running_count"]))
            so4.metric("Blocked", int(snapshot["blocked_count"]))
            so5.metric("Success", int(snapshot["success_count"]))
            so6.metric("Failed", int(snapshot["failed_count"]))
            so7, so8, so9 = st.columns(3)
            so7.metric("Pending Approvals", int(snapshot["pending_approval_count"]))
            so8.metric("Approval SLA Avg (h)", f"{float(snapshot['pending_approval_avg_hours']):.2f}")
            so9.metric("Approval SLA Max (h)", f"{float(snapshot['pending_approval_max_hours']):.2f}")

            slack_ops_df = pd.DataFrame(
                [
                    {
                        "id": row["id"],
                        "intent": row["intent"],
                        "action": row["action"],
                        "status": row["status"],
                        "retry_count": row["retry_count"],
                        "max_retries": row["max_retries"],
                        "requested_by": row["requested_by"],
                        "approval_required": row["approval_required"],
                        "approval_status": row["approval_status"],
                        "approval_requested_at": row["approval_requested_at"],
                        "approval_requested_by": row["approval_requested_by"],
                        "approval_approved_at": row["approval_approved_at"],
                        "approval_approved_by": row["approval_approved_by"],
                        "pending_approval_age_hours": row["pending_approval_age_hours"],
                        "next_attempt_at": row["next_attempt_at"].isoformat() if row["next_attempt_at"] else "",
                        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
                        "last_error": str(row["last_error"] or "")[:250],
                    }
                    for row in snapshot["rows"]
                ]
            )
            st.dataframe(slack_ops_df, use_container_width=True)
            soq1, soq2, soq3 = st.columns(3)
            with soq1:
                if st.button("Run Due Slack Ops Jobs Now", key="admin_slack_ops_queue_run_due_btn"):
                    try:
                        summary = process_due_integration_queue_jobs(
                            repo,
                            integration="slack_ops",
                            actor=user.username,
                            limit=20,
                        )
                        st.success(
                            f"Processed {summary['processed']} Slack Ops job(s): "
                            f"{summary['success']} success, {summary['queued']} re-queued, {summary['failed']} failed."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to run due Slack Ops queue jobs: {exc}")
            with soq2:
                slack_ops_failed_only = [row for row in snapshot["rows"] if str(row["status"] or "").lower() == "failed"]
                if slack_ops_failed_only:
                    row_map = {
                        f"#{row['id']} | {row['intent']} | retry {row['retry_count']}/{row['max_retries']}": row
                        for row in slack_ops_failed_only
                    }
                    selected_key = st.selectbox(
                        "Retry Failed Slack Ops Job",
                        options=list(row_map.keys()),
                        key="admin_slack_ops_queue_retry_failed_select",
                    )
                    if st.button("Retry Selected Slack Ops Job", key="admin_slack_ops_queue_retry_failed_btn"):
                        selected_row = row_map[selected_key]
                        try:
                            repo.update_integration_queue_job(
                                int(selected_row["id"]),
                                {"status": "queued", "next_attempt_at": utcnow_naive()},
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=int(selected_row["id"]),
                                actor=user.username,
                            )
                            if ok:
                                st.success(f"Retry succeeded for Slack Ops queue job #{selected_row['id']}. {msg}")
                            else:
                                st.warning(
                                    f"Retry did not complete successfully for Slack Ops queue job #{selected_row['id']}. {msg}"
                                )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to retry selected Slack Ops queue job: {exc}")
                else:
                    st.info("No failed Slack Ops queue jobs available.")
            with soq3:
                slack_ops_pending = [
                    row
                    for row in snapshot["rows"]
                    if str(row["status"] or "").lower() == "blocked"
                    and bool(row["approval_required"])
                    and str(row["approval_status"] or "").lower() == "pending"
                ]
                if slack_ops_pending:
                    pending_map = {
                        f"#{row['id']} | {row['intent']} | requested_by={row['approval_requested_by'] or row['requested_by']}": row
                        for row in slack_ops_pending
                    }
                    selected_pending_key = st.selectbox(
                        "Approve Pending Slack Ops Job",
                        options=list(pending_map.keys()),
                        key="admin_slack_ops_queue_approve_select",
                    )
                    if st.button("Approve Selected Slack Ops Job", key="admin_slack_ops_queue_approve_btn"):
                        selected_row = pending_map[selected_pending_key]
                        try:
                            outcome = approve_slack_ops_queue_job(
                                repo,
                                queue_job_id=int(selected_row["id"]),
                                approver_username=user.username,
                                approver_role=str(getattr(user, "role", "") or ""),
                                actor=user.username,
                            )
                            if str(outcome.get("status") or "").lower() == "approved":
                                st.success(f"Approved Slack Ops queue job #{selected_row['id']}.")
                            else:
                                st.warning(f"Approval not applied: {outcome}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to approve selected Slack Ops queue job: {exc}")
                else:
                    st.info("No pending Slack Ops approvals.")
        elif load_slack_queue_jobs:
            st.info("No Slack Ops queue jobs found for this environment.")

        st.divider()
        st.markdown("#### Google Retry Queue")
        st.caption("Durable retry queue for failed Google Gmail/Calendar/Drive actions with backoff scheduling.")
        if not load_google_queue_jobs:
            queue_rows = []
            st.caption("Google queue table hydration is deferred. Enable above to load.")
        else:
            try:
                queue_rows = repo.list_integration_queue_jobs(
                    environment=settings.app_env,
                    integration="google",
                    statuses={"queued", "running", "failed", "success"},
                    limit=500,
                )
            except Exception as exc:
                st.error(
                    "Integration queue table is not available yet. "
                    "Run database migrations first (`docker compose run --rm migrate`)."
                )
                st.caption(f"Details: {exc}")
                queue_rows = []
        if queue_rows:
            queue_df = pd.DataFrame(
                [
                    {
                        "id": row.id,
                        "integration": row.integration,
                        "action": row.action,
                        "status": row.status,
                        "retry_count": int(row.retry_count or 0),
                        "max_retries": int(row.max_retries or 0),
                        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
                        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
                        "completed_at": row.completed_at.isoformat() if row.completed_at else "",
                        "requested_by": row.requested_by,
                        "last_error": str(row.last_error or "")[:250],
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                    }
                    for row in queue_rows
                ]
            )
            st.dataframe(queue_df, use_container_width=True)
            q1, q2 = st.columns(2)
            with q1:
                if st.button("Run Due Queue Jobs Now", key="admin_google_queue_run_due_btn"):
                    try:
                        summary = process_due_google_queue_jobs(repo, actor=user.username, limit=20)
                        st.success(
                            f"Processed {summary['processed']} due job(s): "
                            f"{summary['success']} success, {summary['queued']} re-queued, {summary['failed']} failed."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to run due queue jobs: {exc}")
            with q2:
                failed_only = [row for row in queue_rows if str(row.status or "").lower() == "failed"]
                if failed_only:
                    row_map = {f"#{row.id} | {row.action} | retry {row.retry_count}/{row.max_retries}": row for row in failed_only}
                    selected_retry_key = st.selectbox(
                        "Retry Failed Job",
                        options=list(row_map.keys()),
                        key="admin_google_queue_retry_failed_select",
                    )
                    if st.button("Retry Selected Failed Job Now", key="admin_google_queue_retry_failed_btn"):
                        selected_row = row_map[selected_retry_key]
                        try:
                            repo.update_integration_queue_job(
                                int(selected_row.id),
                                {
                                    "status": "queued",
                                    "next_attempt_at": utcnow_naive(),
                                },
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=int(selected_row.id),
                                actor=user.username,
                            )
                            if ok:
                                st.success(f"Retry succeeded for job #{selected_row.id}. {msg}")
                            else:
                                st.warning(f"Retry did not complete successfully for job #{selected_row.id}. {msg}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to retry selected job: {exc}")
                else:
                    st.info("No failed queue jobs available.")
        elif load_google_queue_jobs:
            st.info("No Google queue jobs found for this environment.")

    with tab_comp_config:
        _render_listing_review_policy_editor(repo, user)
        st.divider()
        _render_comp_dealer_domains_editor(repo, user)
        st.divider()
        _render_comp_photo_retry_telemetry(repo, user)
        st.divider()
        _render_coin_paid_source_editor(repo, user)

    with tab_saved_filters:
        st.markdown("### Saved Filter Governance")
        st.caption(
            "Transfer ownership of team-shared filters and delete shared filters when needed."
        )
        blocker_scopes = {"listings_blocker_followups", "operations_home_blocker_followups"}
        try:
            all_filter_rows = repo.db.scalars(
                select(SavedFilterProfile)
                .where(SavedFilterProfile.environment == settings.app_env)
                .order_by(
                    SavedFilterProfile.scope.asc(),
                    SavedFilterProfile.is_shared.desc(),
                    SavedFilterProfile.name.asc(),
                )
            ).all()
        except Exception as exc:
            st.error(
                "Saved filter table is not available yet. Run database migrations first "
                "(`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            all_filter_rows = []
        scope_values = sorted(
            {
                str(row.scope or "").strip().lower()
                for row in all_filter_rows
                if str(row.scope or "").strip()
            }
        )
        selected_scopes = st.multiselect(
            "Scope Filter",
            options=scope_values,
            default=scope_values,
            key="admin_saved_filters_scope_filter",
        )
        only_blocker_scopes = st.checkbox(
            "Only blocker preset scopes",
            value=False,
            key="admin_saved_filters_only_blocker_scopes",
            help="Focus governance view on `listings_blocker_followups` and `operations_home_blocker_followups`.",
        )
        st.markdown("#### Scope Presets")
        sp1, sp2, sp3, sp4 = st.columns(4)
        with sp1:
            if st.button("All", key="admin_saved_filters_scope_preset_all", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = list(scope_values)
                st.session_state["admin_saved_filters_only_blocker_scopes"] = False
                st.rerun()
        with sp2:
            if st.button("Blocker Presets", key="admin_saved_filters_scope_preset_blocker", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = [
                    s for s in scope_values if s in blocker_scopes
                ]
                st.session_state["admin_saved_filters_only_blocker_scopes"] = True
                st.rerun()
        with sp3:
            if st.button("Listings", key="admin_saved_filters_scope_preset_listings", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = [
                    s for s in scope_values if "listing" in s
                ]
                st.session_state["admin_saved_filters_only_blocker_scopes"] = False
                st.rerun()
        with sp4:
            if st.button("Operations Home", key="admin_saved_filters_scope_preset_operations_home", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = [
                    s for s in scope_values if "operations_home" in s
                ]
                st.session_state["admin_saved_filters_only_blocker_scopes"] = False
                st.rerun()
        owner_filter_value = st.selectbox(
            "Ownership Filter",
            options=[
                "all",
                "my_owned",
                "shared_owned_by_me",
                "shared_not_owned_by_me",
            ],
            index=0,
            key="admin_saved_filters_owner_filter",
            help="Focus by ownership to speed transfer/delete governance actions.",
        )
        only_default_presets = st.checkbox(
            "Only default presets",
            value=False,
            key="admin_saved_filters_only_default",
            help="Show only saved filters currently marked as default.",
        )
        st.markdown("#### Ownership Presets")
        op1, op2, op3 = st.columns(3)
        with op1:
            if st.button("My Owned", key="admin_saved_filters_owner_preset_my_owned", use_container_width=True):
                st.session_state["admin_saved_filters_owner_filter"] = "my_owned"
                st.rerun()
        with op2:
            if st.button(
                "Shared Owned By Me",
                key="admin_saved_filters_owner_preset_shared_mine",
                use_container_width=True,
            ):
                st.session_state["admin_saved_filters_owner_filter"] = "shared_owned_by_me"
                st.rerun()
        with op3:
            if st.button(
                "Shared Not Owned By Me",
                key="admin_saved_filters_owner_preset_shared_not_mine",
                use_container_width=True,
            ):
                st.session_state["admin_saved_filters_owner_filter"] = "shared_not_owned_by_me"
                st.rerun()
        if st.button(
            "Reset Governance Filters",
            key="admin_saved_filters_reset_filters_btn",
            use_container_width=True,
        ):
            st.session_state["admin_saved_filters_scope_filter"] = list(scope_values)
            st.session_state["admin_saved_filters_only_blocker_scopes"] = False
            st.session_state["admin_saved_filters_owner_filter"] = "all"
            st.session_state["admin_saved_filters_only_default"] = False
            st.rerun()
        filtered_rows = [
            row
            for row in all_filter_rows
            if (
                (not selected_scopes or str(row.scope or "").strip().lower() in {str(s).strip().lower() for s in selected_scopes})
                and (not only_blocker_scopes or str(row.scope or "").strip().lower() in blocker_scopes)
                and (
                    owner_filter_value == "all"
                    or (
                        owner_filter_value == "my_owned"
                        and str(row.username or "").strip() == str(user.username or "").strip()
                    )
                    or (
                        owner_filter_value == "shared_owned_by_me"
                        and bool(row.is_shared)
                        and str(row.username or "").strip() == str(user.username or "").strip()
                    )
                    or (
                        owner_filter_value == "shared_not_owned_by_me"
                        and bool(row.is_shared)
                        and str(row.username or "").strip() != str(user.username or "").strip()
                    )
                )
                and (not only_default_presets or bool(row.is_default))
            )
        ]
        active_scope_count = int(len(selected_scopes or []))
        owner_mode_label = {
            "all": "All",
            "my_owned": "My Owned",
            "shared_owned_by_me": "Shared By Me",
            "shared_not_owned_by_me": "Shared Not Mine",
        }.get(str(owner_filter_value or "all"), str(owner_filter_value or "all"))
        st.markdown("#### Governance Filter State")
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("Active Scope Count", active_scope_count)
        g2.metric("Owner Mode", owner_mode_label)
        g3.metric("Blocker Focus", "On" if bool(only_blocker_scopes) else "Off")
        g4.metric("Defaults Only", "On" if bool(only_default_presets) else "Off")
        g5, g6 = st.columns(2)
        g5.metric("Visible Rows", int(len(filtered_rows)))
        g6.metric("Visible Defaults", int(len([row for row in filtered_rows if bool(row.is_default)])))
        shared_rows = [row for row in filtered_rows if bool(row.is_shared)]

        if filtered_rows:
            blocker_rows = [
                row for row in filtered_rows if str(row.scope or "").strip().lower() in blocker_scopes
            ]
            sfm1, sfm2, sfm3 = st.columns(3)
            sfm1.metric("Visible Saved Filters", int(len(filtered_rows)))
            sfm2.metric("Visible Shared Filters", int(len(shared_rows)))
            sfm3.metric("Visible Blocker Presets", int(len(blocker_rows)))
            filtered_rows_export = [
                {
                    "id": row.id,
                    "environment": row.environment,
                    "scope": row.scope,
                    "name": row.name,
                    "owner": row.username,
                    "is_shared": bool(row.is_shared),
                    "is_default": bool(row.is_default),
                    "is_active": bool(row.is_active),
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
                for row in filtered_rows
            ]
            st.dataframe(
                pd.DataFrame(
                    filtered_rows_export
                ),
                use_container_width=True,
            )
            st.download_button(
                "Download Filtered Saved Filters CSV",
                data=pd.DataFrame(filtered_rows_export).to_csv(index=False).encode("utf-8"),
                file_name=f"admin_saved_filters_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_saved_filters_filtered_csv_btn",
            )
            st.markdown("#### Filtered Governance Breakdown")
            b1, b2 = st.columns(2)
            filtered_df = pd.DataFrame(filtered_rows_export)
            with b1:
                owner_summary = (
                    filtered_df.groupby(["owner"], dropna=False)
                    .size()
                    .reset_index(name="count")
                    .sort_values(["count", "owner"], ascending=[False, True])
                )
                st.caption("By Owner")
                st.dataframe(owner_summary, use_container_width=True, hide_index=True)
            with b2:
                scope_summary = (
                    filtered_df.groupby(["scope"], dropna=False)
                    .size()
                    .reset_index(name="count")
                    .sort_values(["count", "scope"], ascending=[False, True])
                )
                st.caption("By Scope")
                st.dataframe(scope_summary, use_container_width=True, hide_index=True)
        else:
            st.info("No saved filters found for the current scope filter.")

        if not shared_rows:
            st.info("No team-shared filters available for transfer/delete actions.")
        else:
            shared_map = {
                f"#{row.id} | {row.scope} | {row.name} | owner={row.username}": row
                for row in shared_rows
            }
            selected_key = st.selectbox(
                "Select Shared Filter",
                options=list(shared_map.keys()),
                key="admin_saved_filter_shared_select",
            )
            selected_row = shared_map[selected_key]

            st.markdown("### Transfer Shared Filter Ownership")
            user_options = sorted({u.username for u in users if u.is_active})
            if not user_options:
                st.warning("No active users available as transfer targets.")
            else:
                target_owner = st.selectbox(
                    "New Owner",
                    options=user_options,
                    index=user_options.index(selected_row.username)
                    if selected_row.username in user_options
                    else 0,
                    key="admin_saved_filter_new_owner",
                )
                if st.button("Transfer Ownership", key="admin_saved_filter_transfer_btn"):
                    try:
                        repo.transfer_shared_filter_ownership(
                            profile_id=selected_row.id,
                            new_username=target_owner,
                            actor=user.username,
                        )
                        st.success(f"Transferred filter #{selected_row.id} to `{target_owner}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Ownership transfer failed: {exc}")

            st.markdown("### Delete Shared Filter")
            with st.form("admin_delete_shared_filter_form"):
                confirm_delete = st.checkbox("I understand this permanently deletes the shared filter.")
                phrase = st.text_input("Type DELETE to confirm")
                delete_submit = st.form_submit_button("Delete Selected Shared Filter")
            if delete_submit:
                if not confirm_delete or phrase.strip() != "DELETE":
                    st.error("Confirm deletion and type `DELETE` exactly.")
                else:
                    try:
                        repo.delete_shared_filter_profile_by_id(
                            profile_id=selected_row.id,
                            actor=user.username,
                        )
                        st.success(f"Deleted shared filter #{selected_row.id}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Delete failed: {exc}")

    with tab_sync_jobs:
        st.markdown("### Sync Job Controls")
        st.caption(
            "Sync jobs now resolve from Runtime Settings (DB) first, with env fallback. "
            "Runtime changes apply immediately; env changes apply on restart."
        )

        env_map = {
            "ebay_orders_pull_import": "SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED",
            "ebay_shipping_tracking_push": "SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED",
            "ebay_connection_health_check": "SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_ENABLED",
            "quickbooks_export": "SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED",
            "shopify_orders_pull": "SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED",
        }
        job_rows = []
        for row in sync_job_catalog(repo):
            job_name = str(row.get("job_name") or "")
            job_rows.append(
                {
                    "job_name": job_name,
                    "provider": row.get("provider"),
                    "direction": row.get("direction"),
                    "implemented": bool(row.get("implemented")),
                    "enabled": bool(is_sync_job_enabled(job_name, repo=repo)),
                    "env_var": env_map.get(job_name, ""),
                }
            )
        st.dataframe(pd.DataFrame(job_rows), use_container_width=True)

        st.markdown("### Desired Toggle Values")
        st.caption("Save Runtime Settings for live behavior and/or generate `.env` snippet for deployment fallback.")
        with st.form("admin_sync_jobs_env_snippet_form"):
            desired_orders_pull = st.checkbox(
                "Enable eBay orders pull/import",
                value=bool(is_sync_job_enabled("ebay_orders_pull_import", repo=repo)),
            )
            desired_tracking_push = st.checkbox(
                "Enable eBay shipping tracking push",
                value=bool(is_sync_job_enabled("ebay_shipping_tracking_push", repo=repo)),
            )
            desired_connection_health_check = st.checkbox(
                "Enable eBay connection health check",
                value=bool(is_sync_job_enabled("ebay_connection_health_check", repo=repo)),
            )
            desired_connection_health_check_interval_minutes = st.number_input(
                "eBay connection health check interval (minutes)",
                min_value=5,
                max_value=24 * 60,
                value=max(
                    5,
                    min(
                        24 * 60,
                        get_runtime_int(
                            repo,
                            "sync_job_ebay_connection_health_check_interval_minutes",
                            int(getattr(settings, "sync_job_ebay_connection_health_check_interval_minutes", 30)),
                        ),
                    ),
                ),
                step=5,
            )
            desired_quickbooks_export = st.checkbox(
                "Enable QuickBooks export (future job)",
                value=bool(is_sync_job_enabled("quickbooks_export", repo=repo)),
            )
            desired_shopify_pull = st.checkbox(
                "Enable Shopify orders pull (future job)",
                value=bool(is_sync_job_enabled("shopify_orders_pull", repo=repo)),
            )
            st.markdown("#### Governance Snapshot Scheduler")
            desired_governance_snapshot_runner = st.checkbox(
                "Enable scheduled governance snapshots (sync worker)",
                value=get_runtime_bool(repo, "governance_snapshot_runner_enabled", False),
                help="When enabled, sync worker records governance snapshot audit events on an interval.",
            )
            desired_governance_snapshot_interval_hours = st.number_input(
                "Snapshot Interval Hours",
                min_value=1,
                max_value=24 * 30,
                value=max(1, min(24 * 30, get_runtime_int(repo, "governance_snapshot_interval_hours", 24))),
                step=1,
            )
            desired_governance_snapshot_lookback_days = st.number_input(
                "Snapshot Lookback Days",
                min_value=1,
                max_value=365,
                value=max(1, min(365, get_runtime_int(repo, "governance_snapshot_lookback_days", 30))),
                step=1,
            )
            desired_governance_snapshot_max_rows = st.number_input(
                "Snapshot Max Rows Per Scope",
                min_value=100,
                max_value=10000,
                value=max(100, min(10000, get_runtime_int(repo, "governance_snapshot_max_rows_per_scope", 2000))),
                step=100,
            )
            save_runtime_toggles = st.form_submit_button("Save Runtime Toggles")
            generate_snippet = st.form_submit_button("Generate Env Snippet")

        if save_runtime_toggles:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_ebay_orders_pull_import_enabled",
                    value="true" if desired_orders_pull else "false",
                    value_type="bool",
                    description="Enable/disable eBay orders pull/import job.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_ebay_shipping_tracking_push_enabled",
                    value="true" if desired_tracking_push else "false",
                    value_type="bool",
                    description="Enable/disable eBay shipping tracking push job.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_ebay_connection_health_check_enabled",
                    value="true" if desired_connection_health_check else "false",
                    value_type="bool",
                    description="Enable/disable eBay connection health-check job.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_ebay_connection_health_check_interval_minutes",
                    value=str(int(desired_connection_health_check_interval_minutes)),
                    value_type="int",
                    description="Minimum minutes between scheduled eBay connection health checks.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_quickbooks_export_enabled",
                    value="true" if desired_quickbooks_export else "false",
                    value_type="bool",
                    description="Enable/disable QuickBooks export job scaffold.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_shopify_orders_pull_enabled",
                    value="true" if desired_shopify_pull else "false",
                    value_type="bool",
                    description="Enable/disable Shopify pull job scaffold.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_runner_enabled",
                    value="true" if desired_governance_snapshot_runner else "false",
                    value_type="bool",
                    description="Enable/disable scheduled governance snapshot creation in sync runner.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_interval_hours",
                    value=str(int(desired_governance_snapshot_interval_hours)),
                    value_type="int",
                    description="Minimum hours between sync-runner governance snapshot events.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_lookback_days",
                    value=str(int(desired_governance_snapshot_lookback_days)),
                    value_type="int",
                    description="Lookback window for scheduled governance snapshot event counts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_max_rows_per_scope",
                    value=str(int(desired_governance_snapshot_max_rows)),
                    value_type="int",
                    description="Max rows per governance scope sampled in scheduled snapshots.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Runtime sync toggles saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save runtime sync toggles: {exc}")

        if generate_snippet:
            snippet = (
                f"SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED={'true' if desired_orders_pull else 'false'}\n"
                f"SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED={'true' if desired_tracking_push else 'false'}\n"
                f"SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_ENABLED={'true' if desired_connection_health_check else 'false'}\n"
                f"SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_INTERVAL_MINUTES={int(desired_connection_health_check_interval_minutes)}\n"
                f"SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED={'true' if desired_quickbooks_export else 'false'}\n"
                f"SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED={'true' if desired_shopify_pull else 'false'}\n"
                "# Governance snapshot scheduler is runtime-only (DB-backed):\n"
                f"# governance_snapshot_runner_enabled={'true' if desired_governance_snapshot_runner else 'false'}\n"
                f"# governance_snapshot_interval_hours={int(desired_governance_snapshot_interval_hours)}\n"
                f"# governance_snapshot_lookback_days={int(desired_governance_snapshot_lookback_days)}\n"
                f"# governance_snapshot_max_rows_per_scope={int(desired_governance_snapshot_max_rows)}"
            )
            st.code(snippet, language="bash")

        st.caption(
            "Legacy eBay Finding controls have been removed from this Admin view. "
            "Comp workflows use sold-results HTML and web fallback sources."
        )

        st.markdown("### Governance Snapshot Scheduler Status")
        worker_snapshots = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "governance_export",
                AuditLog.action == "snapshot",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        latest_worker_snapshot = None
        latest_manual_snapshot = None
        for row in worker_snapshots:
            payload = _audit_changes(row)
            source = str(payload.get("source") or "").strip().lower()
            if source == "sync_runner":
                latest_worker_snapshot = row
            elif source in {"admin_sync_jobs", "admin_governance_exports"} and latest_manual_snapshot is None:
                latest_manual_snapshot = row
            if latest_worker_snapshot is not None and latest_manual_snapshot is not None:
                break
        now_ts = utcnow_naive()
        interval_hours = int(desired_governance_snapshot_interval_hours)
        next_due_at = None
        if latest_worker_snapshot is not None and latest_worker_snapshot.created_at is not None:
            next_due_at = latest_worker_snapshot.created_at + timedelta(hours=interval_hours)
        is_overdue = bool(next_due_at is not None and next_due_at <= now_ts)
        scheduler_enabled = bool(desired_governance_snapshot_runner)
        ss1, ss2, ss3, ss4, ss5 = st.columns(5)
        ss1.metric("Scheduler Enabled", "yes" if scheduler_enabled else "no")
        ss2.metric(
            "Last Worker Snapshot",
            latest_worker_snapshot.created_at.isoformat(timespec="seconds")
            if latest_worker_snapshot and latest_worker_snapshot.created_at
            else "none",
        )
        ss3.metric(
            "Last Manual Snapshot",
            latest_manual_snapshot.created_at.isoformat(timespec="seconds")
            if latest_manual_snapshot and latest_manual_snapshot.created_at
            else "none",
        )
        ss4.metric(
            "Next Due",
            next_due_at.isoformat(timespec="seconds") if next_due_at is not None else "on first run",
        )
        ss5.metric("Due Status", "overdue" if is_overdue else ("scheduled" if next_due_at is not None else "pending"))
        if latest_worker_snapshot is not None and latest_manual_snapshot is not None and latest_worker_snapshot.created_at and latest_manual_snapshot.created_at:
            lag_seconds = int((latest_worker_snapshot.created_at - latest_manual_snapshot.created_at).total_seconds())
            st.caption(
                "Worker vs manual snapshot recency delta (seconds): "
                f"{lag_seconds:+d} (positive means worker snapshot is newer)."
            )
        if scheduler_enabled and is_overdue:
            st.warning("Governance snapshot scheduler is overdue. Check sync worker health/logs.")
        elif scheduler_enabled:
            st.caption("Governance snapshot scheduler is active and waiting for next due interval.")
        else:
            st.caption("Governance snapshot scheduler is disabled.")
        cutoff_7d = now_ts - timedelta(days=7)
        cutoff_30d = now_ts - timedelta(days=30)
        source_counts_7d: Counter[str] = Counter()
        source_counts_30d: Counter[str] = Counter()
        source_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "governance_export",
                AuditLog.action == "snapshot",
                AuditLog.created_at >= cutoff_30d,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(5000)
        ).all()
        for row in source_logs:
            payload = _audit_changes(row)
            source = str(payload.get("source") or "unknown").strip().lower() or "unknown"
            created_at = row.created_at
            if created_at is None:
                continue
            source_counts_30d[source] += 1
            if created_at >= cutoff_7d:
                source_counts_7d[source] += 1
        source_keys = sorted(set(source_counts_30d.keys()) | set(source_counts_7d.keys()))
        if source_keys:
            source_breakdown_df = pd.DataFrame(
                [
                    {
                        "source": source,
                        "last_7_days": int(source_counts_7d.get(source, 0)),
                        "last_30_days": int(source_counts_30d.get(source, 0)),
                    }
                    for source in source_keys
                ]
            ).sort_values(["last_30_days", "source"], ascending=[False, True])
            st.caption("Snapshot Source Breakdown")
            st.dataframe(source_breakdown_df, use_container_width=True, hide_index=True)
        else:
            st.caption("No governance snapshot source activity in last 30 days.")

        st.markdown("#### Cadence Health")
        worker_7d = int(source_counts_7d.get("sync_runner", 0))
        worker_30d = int(source_counts_30d.get("sync_runner", 0))
        expected_7d = max(1, int(round((7 * 24) / max(1, interval_hours))))
        expected_30d = max(1, int(round((30 * 24) / max(1, interval_hours))))
        completion_7d = float(worker_7d) / float(expected_7d)
        completion_30d = float(worker_30d) / float(expected_30d)
        cadence_ratio = min(completion_7d, completion_30d)
        if not scheduler_enabled:
            cadence_state = "disabled"
        elif cadence_ratio >= 0.9:
            cadence_state = "green"
        elif cadence_ratio >= 0.5:
            cadence_state = "yellow"
        else:
            cadence_state = "red"
        ch1, ch2, ch3, ch4 = st.columns(4)
        ch1.metric("Cadence State", cadence_state.upper())
        ch2.metric("Worker Snapshots 7d", f"{worker_7d}/{expected_7d}")
        ch3.metric("Worker Snapshots 30d", f"{worker_30d}/{expected_30d}")
        ch4.metric("Cadence Ratio", f"{cadence_ratio * 100:.1f}%")
        if cadence_state == "green":
            st.success("Cadence health is GREEN based on expected worker snapshot frequency.")
        elif cadence_state == "yellow":
            st.warning("Cadence health is YELLOW. Worker snapshots are below expected target.")
        elif cadence_state == "red":
            st.error("Cadence health is RED. Worker snapshots are significantly below expected target.")
        else:
            st.caption("Cadence health is DISABLED because scheduler toggle is off.")
        if cadence_state == "red":
            followup_logs = repo.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "workspace_followup")
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(2000)
            ).all()
            cadence_created: dict[str, dict[str, Any]] = {}
            cadence_resolved: set[str] = set()
            for row in followup_logs:
                payload = _audit_changes(row)
                task_key = str(payload.get("task_key") or "").strip()
                workflow = str(payload.get("workflow") or "").strip().lower()
                action = str(row.action or "").strip().lower()
                if workflow != "governance_snapshot_cadence" or not task_key:
                    continue
                if action == "create" and task_key not in cadence_created:
                    cadence_created[task_key] = payload
                if action in {"resolve", "closed"}:
                    cadence_resolved.add(task_key)
            open_cadence_tasks = [
                payload for key, payload in cadence_created.items() if key not in cadence_resolved
            ]
            st.markdown("##### Cadence Blocker Follow-up")
            if open_cadence_tasks:
                st.warning(
                    f"Open cadence follow-up tasks detected: {len(open_cadence_tasks)}. "
                    "Resolve existing task(s) before creating another."
                )
            cf1, cf2, cf3 = st.columns(3)
            with cf1:
                cadence_followup_owner = st.text_input(
                    "Cadence Follow-up Owner",
                    value=user.username,
                    key="admin_sync_jobs_cadence_followup_owner",
                )
            with cf2:
                cadence_followup_priority = st.selectbox(
                    "Cadence Follow-up Priority",
                    options=["critical", "high", "medium", "low"],
                    index=1,
                    key="admin_sync_jobs_cadence_followup_priority",
                )
            with cf3:
                cadence_followup_due_days = st.number_input(
                    "Cadence Follow-up Due (days)",
                    min_value=0,
                    max_value=30,
                    value=1,
                    step=1,
                    key="admin_sync_jobs_cadence_followup_due_days",
                )
            if st.button(
                "Create Cadence Follow-up Task",
                key="admin_sync_jobs_create_cadence_followup_btn",
                disabled=bool(open_cadence_tasks),
            ):
                try:
                    task_key = f"gov-cadence-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                    due_date = (utcnow_naive() + timedelta(days=int(cadence_followup_due_days))).date().isoformat()
                    repo.record_audit_event(
                        entity_type="workspace_followup",
                        entity_id=None,
                        action="create",
                        actor=user.username,
                        changes={
                            "task_key": task_key,
                            "workflow": "governance_snapshot_cadence",
                            "title": "Governance snapshot cadence below threshold",
                            "owner": str(cadence_followup_owner or user.username).strip() or user.username,
                            "priority": str(cadence_followup_priority).strip().lower(),
                            "due_date": due_date,
                            "status": "open",
                            "environment": settings.app_env,
                            "note": (
                                f"Cadence red. interval_hours={interval_hours}, "
                                f"worker_7d={worker_7d}/{expected_7d}, "
                                f"worker_30d={worker_30d}/{expected_30d}, "
                                f"ratio={cadence_ratio * 100:.1f}%"
                            ),
                        },
                    )
                    st.success(f"Created cadence follow-up task `{task_key}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to create cadence follow-up task: {exc}")

        st.markdown("### Governance Snapshot Actions")
        st.caption("Run governance snapshot now using current scheduler settings, without waiting for sync-worker interval.")
        if st.button("Run Governance Snapshot Now", key="admin_sync_jobs_run_governance_snapshot_now_btn"):
            try:
                counts = _record_governance_snapshot_event(
                    repo,
                    actor=user.username,
                    lookback_days=int(desired_governance_snapshot_lookback_days),
                    max_rows=int(desired_governance_snapshot_max_rows),
                    source="admin_sync_jobs",
                    download_intent=False,
                )
                st.success(
                    "Governance snapshot recorded from Sync Jobs. "
                    f"handoff={counts['handoff_events']} "
                    f"feedback={counts['workspace_feedback_events']} "
                    f"parity={counts['parity_followup_events']} "
                    f"comp={counts['photo_comp_events']}"
                )
            except Exception as exc:
                st.error(f"Unable to run governance snapshot: {exc}")

    with tab_governance_exports:
        _render_governance_exports_hub(repo, user)

    with tab_system_health:
        render_system_health(repo)
        st.markdown("### UX Navigation Controls")
        runtime_map_nav = {
            str(row.key): row for row in repo.list_runtime_settings(environment=settings.app_env, active_only=False)
        }

        def _rb_nav(key: str, default: bool) -> bool:
            row = runtime_map_nav.get(key)
            if row is None:
                return default
            return str(row.value or "").strip().lower() in {"1", "true", "yes", "on"}

        current_nav_mode = str((runtime_map_nav.get("ux_navigation_mode").value if runtime_map_nav.get("ux_navigation_mode") else "unified") or "unified").strip().lower()
        if current_nav_mode not in {"unified", "legacy"}:
            current_nav_mode = "unified"
        current_window_start_raw = str(
            (runtime_map_nav.get("ux_navigation_window_start_iso").value if runtime_map_nav.get("ux_navigation_window_start_iso") else "")
            or ""
        ).strip()

        with st.form("admin_nav_controls_form"):
            nc1, nc2, nc3 = st.columns(3)
            with nc1:
                nav_mode = st.selectbox(
                    "Navigation Mode",
                    options=["unified", "legacy"],
                    index=0 if current_nav_mode == "unified" else 1,
                    help="`unified`: pinned pages + role default landing. `legacy`: classic sidebar behavior.",
                )
                nav_telemetry_enabled = st.checkbox(
                    "Enable Navigation Telemetry",
                    value=_rb_nav("ux_navigation_telemetry_enabled", True),
                )
            with nc2:
                role_default_landing_enabled = st.checkbox(
                    "Enable Role Default Landing",
                    value=_rb_nav("ux_role_default_landing_enabled", True),
                )
                workspace_ebay_enabled = st.checkbox(
                    "Enable eBay Workspace Group",
                    value=_rb_nav("ux_workspace_ebay_enabled", True),
                )
                workspace_inventory_enabled = st.checkbox(
                    "Enable Inventory Workspace Group",
                    value=_rb_nav("ux_workspace_inventory_enabled", True),
                )
            with nc3:
                workspace_fulfillment_enabled = st.checkbox(
                    "Enable Fulfillment Workspace Group",
                    value=_rb_nav("ux_workspace_fulfillment_enabled", True),
                )
                workspace_sync_enabled = st.checkbox(
                    "Enable Sync Workspace Group",
                    value=_rb_nav("ux_workspace_sync_enabled", True),
                )
                workspace_revenue_enabled = st.checkbox(
                    "Enable Revenue Workspace Group",
                    value=_rb_nav("ux_workspace_revenue_enabled", True),
                )
                listings_auto_photo_comp_review_preset = st.checkbox(
                    "Auto-Apply Listings Photo-Comp Queue",
                    value=_rb_nav("ux_listings_auto_photo_comp_review_preset", False),
                    help="When enabled, Listings auto-loads the Photo-Comp Review Queue preset once per signed-in session.",
                )
                ebay_require_runbook_for_bulk_ops = st.checkbox(
                    "Require eBay Runbook For Bulk Ops",
                    value=_rb_nav("ebay_require_runbook_for_bulk_ops", False),
                    help="When enabled, eBay Ops bulk controls are disabled until the eBay Workspace runbook checklist is complete.",
                )
                st.text_input(
                    "Telemetry Window Start (ISO, optional)",
                    value=current_window_start_raw,
                    disabled=True,
                    help="Set by action buttons below to isolate baseline windows.",
                )
            save_nav_controls = st.form_submit_button("Save Navigation Controls")

        if save_nav_controls:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_navigation_mode",
                    value=nav_mode,
                    value_type="str",
                    description="Navigation rollout mode (`unified` or `legacy`).",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_navigation_telemetry_enabled",
                    value="true" if nav_telemetry_enabled else "false",
                    value_type="bool",
                    description="Enable navigation telemetry audit events.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_role_default_landing_enabled",
                    value="true" if role_default_landing_enabled else "false",
                    value_type="bool",
                    description="Enable role-based default landing redirect from Home page.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_ebay_enabled",
                    value="true" if workspace_ebay_enabled else "false",
                    value_type="bool",
                    description="Enable eBay workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_inventory_enabled",
                    value="true" if workspace_inventory_enabled else "false",
                    value_type="bool",
                    description="Enable Inventory workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_fulfillment_enabled",
                    value="true" if workspace_fulfillment_enabled else "false",
                    value_type="bool",
                    description="Enable Fulfillment workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_sync_enabled",
                    value="true" if workspace_sync_enabled else "false",
                    value_type="bool",
                    description="Enable Sync workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_revenue_enabled",
                    value="true" if workspace_revenue_enabled else "false",
                    value_type="bool",
                    description="Enable Revenue workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_listings_auto_photo_comp_review_preset",
                    value="true" if listings_auto_photo_comp_review_preset else "false",
                    value_type="bool",
                    description="Auto-apply Listings Photo-Comp Review Queue preset once per user session.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ebay_require_runbook_for_bulk_ops",
                    value="true" if ebay_require_runbook_for_bulk_ops else "false",
                    value_type="bool",
                    description="Require eBay Workspace runbook completion before bulk eBay Ops actions are enabled.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Navigation controls saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save navigation controls: {exc}")

        wc1, wc2 = st.columns(2)
        with wc1:
            if st.button("Start New Telemetry Window Now", key="admin_nav_window_start_now_btn"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ux_navigation_window_start_iso",
                        value=utcnow_naive().isoformat(timespec="seconds"),
                        value_type="str",
                        description="Optional telemetry window lower-bound timestamp (ISO UTC) for nav analytics.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Telemetry window start set to current time.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set telemetry window start: {exc}")
        with wc2:
            if st.button("Clear Telemetry Window Marker", key="admin_nav_window_clear_btn"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ux_navigation_window_start_iso",
                        value="",
                        value_type="str",
                        description="Optional telemetry window lower-bound timestamp (ISO UTC) for nav analytics.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Telemetry window marker cleared.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear telemetry window marker: {exc}")

        st.markdown("### Navigation Telemetry")
        st.caption("Derived from audit events (`entity_type=navigation`) to tune IA and workflow grouping.")
        load_navigation_telemetry = st.checkbox(
            "Load Navigation Telemetry (slower)",
            value=False,
            key="admin_load_navigation_telemetry",
        )
        window_start_dt = None
        if current_window_start_raw:
            try:
                window_start_dt = datetime.fromisoformat(current_window_start_raw)
            except Exception:
                window_start_dt = None
        if not load_navigation_telemetry:
            st.caption("Navigation telemetry is skipped by default. Enable `Load Navigation Telemetry (slower)` to fetch it.")
        else:
            nav_query = select(AuditLog).where(AuditLog.entity_type == "navigation")
            if window_start_dt is not None:
                nav_query = nav_query.where(AuditLog.created_at >= window_start_dt)
                st.caption(f"Telemetry window start: `{window_start_dt.isoformat(timespec='seconds')}`")
            nav_events = repo.db.scalars(nav_query.order_by(AuditLog.created_at.desc()).limit(1500)).all()
            if not nav_events:
                st.info("No navigation telemetry events recorded yet.")
            else:
                page_view_counter: Counter[str] = Counter()
                switch_counter: Counter[str] = Counter()
                bounce_counter: Counter[str] = Counter()
                handoff_applied_counter: Counter[str] = Counter()
                handoff_cleared_counter: Counter[str] = Counter()
                nav_payload_by_id: dict[int, dict[str, Any]] = {}
                event_rows: list[dict] = []
                for row in nav_events:
                    payload = _audit_changes(row)
                    nav_payload_by_id[int(row.id)] = payload if isinstance(payload, dict) else {}
                    action = str(row.action or "").strip().lower()
                    if action == "page_view":
                        page_key = str(payload.get("page") or payload.get("page_title") or "unknown")
                        page_view_counter[page_key] += 1
                    elif action == "page_switch":
                        from_page = str(payload.get("from_page") or "").strip()
                        to_page = str(payload.get("to_page") or "").strip()
                        if from_page or to_page:
                            edge = f"{from_page or '?'} -> {to_page or '?'}"
                            switch_counter[edge] += 1
                            try:
                                delta = float(payload.get("seconds_since_last_page") or 0.0)
                            except Exception:
                                delta = 0.0
                            if delta > 0.0 and delta < 10.0:
                                bounce_counter[edge] += 1
                    elif action == "workspace_handoff_applied":
                        target = str(payload.get("to") or payload.get("target") or "unknown").strip().lower() or "unknown"
                        handoff_applied_counter[target] += 1
                    elif action == "workspace_handoff_cleared":
                        target = str(payload.get("target") or payload.get("to") or "unknown").strip().lower() or "unknown"
                        handoff_cleared_counter[target] += 1
                    event_rows.append(
                        {
                            "id": row.id,
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "actor": row.actor,
                            "action": row.action,
                            "changes": json.dumps(payload)[:400],
                        }
                    )
    
                n1, n2, n3 = st.columns(3)
                n1.metric("Total Nav Events", len(nav_events))
                n2.metric("Unique Page Views", len(page_view_counter))
                n3.metric("Unique Switch Paths", len(switch_counter))
    
                top_pages_df = pd.DataFrame(
                    [{"page": page, "views": count} for page, count in page_view_counter.most_common(20)]
                )
                top_switch_df = pd.DataFrame(
                    [
                        {"switch_path": path, "count": count, "bounce_lt_10s": int(bounce_counter.get(path, 0))}
                        for path, count in switch_counter.most_common(20)
                    ]
                )
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("#### Most Visited Pages")
                    if top_pages_df.empty:
                        st.caption("No page-view telemetry yet.")
                    else:
                        st.dataframe(top_pages_df, use_container_width=True, hide_index=True)
                with c2:
                    st.markdown("#### Most Common Switch Paths")
                    if top_switch_df.empty:
                        st.caption("No page-switch telemetry yet.")
                    else:
                        st.dataframe(top_switch_df, use_container_width=True, hide_index=True)
    
                st.markdown("#### Handoff Telemetry")
                total_handoff_applied = int(sum(handoff_applied_counter.values()))
                total_handoff_cleared = int(sum(handoff_cleared_counter.values()))
                clear_rate = (float(total_handoff_cleared) / float(total_handoff_applied)) if total_handoff_applied else 0.0
                handoff_df_for_export = pd.DataFrame()
                h1, h2, h3 = st.columns(3)
                h1.metric("Handoff Applied", total_handoff_applied)
                h2.metric("Handoff Cleared", total_handoff_cleared)
                h3.metric("Handoff Clear Rate", f"{clear_rate * 100:.1f}%")
                handoff_targets = sorted(set(handoff_applied_counter.keys()) | set(handoff_cleared_counter.keys()))
                if handoff_targets:
                    handoff_df = pd.DataFrame(
                        [
                            {
                                "target": target,
                                "applied_count": int(handoff_applied_counter.get(target, 0)),
                                "cleared_count": int(handoff_cleared_counter.get(target, 0)),
                                "clear_rate": round(
                                    (
                                        float(handoff_cleared_counter.get(target, 0))
                                        / float(handoff_applied_counter.get(target, 0))
                                    )
                                    if int(handoff_applied_counter.get(target, 0)) > 0
                                    else 0.0,
                                    4,
                                ),
                            }
                            for target in handoff_targets
                        ]
                    ).sort_values(by=["applied_count", "target"], ascending=[False, True])
                    handoff_df_for_export = handoff_df.copy()
                    st.dataframe(handoff_df, use_container_width=True, hide_index=True)
                else:
                    st.caption("No workspace handoff telemetry recorded yet.")
                handoff_event_rows: list[dict[str, Any]] = []
                for row in nav_events:
                    if str(row.action or "").strip().lower() not in {
                        "workspace_handoff_applied",
                        "workspace_handoff_cleared",
                    }:
                        continue
                    payload = nav_payload_by_id.get(int(row.id), {})
                    handoff_event_rows.append(
                        {
                            "id": row.id,
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "actor": str(row.actor or ""),
                            "action": str(row.action or ""),
                            "target": str(payload.get("target", "") or payload.get("to", "") or "unknown")
                            .strip()
                            .lower(),
                            "summary": json.dumps(payload)[:220],
                        }
                    )
                with st.expander("Recent Handoff Events", expanded=False):
                    if not handoff_event_rows:
                        st.caption("No handoff events recorded in this telemetry window.")
                    else:
                        handoff_events_df = pd.DataFrame(handoff_event_rows)
                        hf1, hf2, hf3 = st.columns(3)
                        with hf1:
                            action_filter = st.multiselect(
                                "Action",
                                options=sorted(handoff_events_df["action"].dropna().unique().tolist()),
                                default=[],
                                key="admin_handoff_events_action_filter",
                            )
                        with hf2:
                            target_filter = st.multiselect(
                                "Target",
                                options=sorted(handoff_events_df["target"].dropna().unique().tolist()),
                                default=[],
                                key="admin_handoff_events_target_filter",
                            )
                        with hf3:
                            actor_filter = st.multiselect(
                                "Actor",
                                options=sorted(handoff_events_df["actor"].dropna().unique().tolist()),
                                default=[],
                                key="admin_handoff_events_actor_filter",
                            )
                        filtered_handoff_df = handoff_events_df
                        if action_filter:
                            filtered_handoff_df = filtered_handoff_df[
                                filtered_handoff_df["action"].isin(action_filter)
                            ]
                        if target_filter:
                            filtered_handoff_df = filtered_handoff_df[
                                filtered_handoff_df["target"].isin(target_filter)
                            ]
                        if actor_filter:
                            filtered_handoff_df = filtered_handoff_df[
                                filtered_handoff_df["actor"].isin(actor_filter)
                            ]
                        top_actor = ""
                        top_target = ""
                        most_cleared_target = ""
                        if not filtered_handoff_df.empty:
                            actor_counts = (
                                filtered_handoff_df["actor"]
                                .fillna("")
                                .astype(str)
                                .str.strip()
                                .loc[lambda s: s != ""]
                                .value_counts()
                            )
                            if not actor_counts.empty:
                                top_actor = str(actor_counts.index[0])
                            target_counts = (
                                filtered_handoff_df["target"]
                                .fillna("")
                                .astype(str)
                                .str.strip()
                                .loc[lambda s: s != ""]
                                .value_counts()
                            )
                            if not target_counts.empty:
                                top_target = str(target_counts.index[0])
                            cleared_df = filtered_handoff_df[
                                filtered_handoff_df["action"].astype(str).str.lower() == "workspace_handoff_cleared"
                            ]
                            if not cleared_df.empty:
                                cleared_counts = (
                                    cleared_df["target"]
                                    .fillna("")
                                    .astype(str)
                                    .str.strip()
                                    .loc[lambda s: s != ""]
                                    .value_counts()
                                )
                                if not cleared_counts.empty:
                                    most_cleared_target = str(cleared_counts.index[0])
                        k1, k2, k3 = st.columns(3)
                        k1.metric("Top Actor", top_actor or "n/a")
                        k2.metric("Top Target", top_target or "n/a")
                        k3.metric("Most Cleared Target", most_cleared_target or "n/a")
                        st.download_button(
                            "Download Handoff Events CSV",
                            data=filtered_handoff_df.to_csv(index=False).encode("utf-8"),
                            file_name=f"handoff_events_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                            mime="text/csv",
                            key="admin_handoff_events_csv_btn",
                        )
                        bundle_buffer = BytesIO()
                        with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                            handoff_kpis_df = pd.DataFrame(
                                [
                                    {
                                        "environment": settings.app_env,
                                        "total_handoff_applied": int(total_handoff_applied),
                                        "total_handoff_cleared": int(total_handoff_cleared),
                                        "handoff_clear_rate_pct": round(float(clear_rate) * 100.0, 2),
                                        "top_actor": top_actor or "",
                                        "top_target": top_target or "",
                                        "most_cleared_target": most_cleared_target or "",
                                        "generated_at_utc": utcnow_naive().isoformat(),
                                    }
                                ]
                            )
                            bundle_zip.writestr("handoff_kpis.csv", handoff_kpis_df.to_csv(index=False))
                            if not handoff_df_for_export.empty:
                                agg_df = handoff_df_for_export.copy()
                                agg_df.insert(0, "environment", settings.app_env)
                                bundle_zip.writestr(
                                    "handoff_target_aggregate.csv",
                                    agg_df.to_csv(index=False),
                                )
                            bundle_zip.writestr(
                                "handoff_events_filtered.csv",
                                filtered_handoff_df.to_csv(index=False),
                            )
                            full_events_df = handoff_events_df.copy()
                            full_events_df.insert(0, "environment", settings.app_env)
                            bundle_zip.writestr(
                                "handoff_events_full_window.csv",
                                full_events_df.to_csv(index=False),
                            )
                        bundle_buffer.seek(0)
                        st.download_button(
                            "Export Handoff Governance Bundle (ZIP)",
                            data=bundle_buffer.getvalue(),
                            file_name=f"handoff_governance_bundle_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip",
                            mime="application/zip",
                            key="admin_handoff_events_bundle_zip_btn",
                        )
                        st.dataframe(
                            filtered_handoff_df.head(200),
                            use_container_width=True,
                            hide_index=True,
                        )
    
                with st.expander("Recent Navigation Events", expanded=False):
                    st.dataframe(pd.DataFrame(event_rows[:200]), use_container_width=True, hide_index=True)
    
                st.markdown("#### Workflow Baseline Metrics")
                switch_events: list[dict] = []
                for row in nav_events:
                    payload = _audit_changes(row)
                    if str(row.action or "").strip().lower() != "page_switch":
                        continue
                    try:
                        delta = float(payload.get("seconds_since_last_page") or 0.0)
                    except Exception:
                        delta = 0.0
                    switch_events.append(
                        {
                            "actor": str(row.actor or "unknown"),
                            "created_at": row.created_at,
                            "from_page": str(payload.get("from_page") or "").strip().lower(),
                            "to_page": str(payload.get("to_page") or "").strip().lower(),
                            "delta_s": delta,
                        }
                    )
    
                if not switch_events:
                    st.caption("No page-switch telemetry yet for baseline metrics.")
                else:
                    def _median(values: list[float]) -> float:
                        vals = sorted(v for v in values if v >= 0)
                        if not vals:
                            return 0.0
                        n = len(vals)
                        mid = n // 2
                        if n % 2 == 1:
                            return float(vals[mid])
                        return float((vals[mid - 1] + vals[mid]) / 2.0)
    
                    all_deltas = [float(r["delta_s"]) for r in switch_events if float(r["delta_s"]) > 0]
                    bounce_count = len([v for v in all_deltas if v < 10.0])
                    bounce_rate = (bounce_count / len(all_deltas)) if all_deltas else 0.0
    
                    # Session click-depth: per actor, new session starts after 30m inactivity.
                    sessions: list[int] = []
                    by_actor: dict[str, list[dict]] = {}
                    for row in switch_events:
                        by_actor.setdefault(str(row["actor"]), []).append(row)
                    for actor_rows in by_actor.values():
                        actor_rows_sorted = sorted(actor_rows, key=lambda r: r["created_at"] or utcnow_naive())
                        session_count = 0
                        prev_ts = None
                        for ev in actor_rows_sorted:
                            ts = ev["created_at"]
                            if ts is None:
                                continue
                            if prev_ts is None or (ts - prev_ts).total_seconds() > 1800:
                                if session_count > 0:
                                    sessions.append(session_count)
                                session_count = 1
                            else:
                                session_count += 1
                            prev_ts = ts
                        if session_count > 0:
                            sessions.append(session_count)
    
                    def _workflow_median(pairs: set[tuple[str, str]]) -> float:
                        vals = [
                            float(r["delta_s"])
                            for r in switch_events
                            if (str(r["from_page"]), str(r["to_page"])) in pairs and float(r["delta_s"]) > 0
                        ]
                        return _median(vals)
    
                    listing_pairs = {
                        ("operations_home", "listings"),
                        ("products", "listings"),
                        ("inventory_intake_wizard", "listings"),
                        ("listings", "ebay_workspace"),
                    }
                    fulfillment_pairs = {
                        ("operations_home", "shipping"),
                        ("orders", "shipping"),
                        ("sales", "shipping"),
                        ("shipping", "orders"),
                    }
                    reconcile_pairs = {
                        ("shipping", "sync"),
                        ("sales", "reports"),
                        ("sync", "reports"),
                        ("reports", "documents"),
                    }
    
                    b1, b2, b3, b4 = st.columns(4)
                    b1.metric("Median Switch Latency (s)", f"{_median(all_deltas):.1f}")
                    b2.metric("Bounce Rate (<10s)", f"{bounce_rate * 100:.1f}%")
                    b3.metric("Median Click-Depth / Session", f"{_median([float(v) for v in sessions]):.1f}")
                    b4.metric("Switch Events (window)", len(switch_events))
    
                    wf_df = pd.DataFrame(
                        [
                            {
                                "workflow": "Listing handoff",
                                "median_transition_seconds": round(_workflow_median(listing_pairs), 2),
                            },
                            {
                                "workflow": "Fulfillment handoff",
                                "median_transition_seconds": round(_workflow_median(fulfillment_pairs), 2),
                            },
                            {
                                "workflow": "Reconcile handoff",
                                "median_transition_seconds": round(_workflow_median(reconcile_pairs), 2),
                            },
                        ]
                    )
                    st.dataframe(wf_df, use_container_width=True, hide_index=True)
                    baseline_summary_df = pd.DataFrame(
                        [
                            {"metric": "median_switch_latency_seconds", "value": round(_median(all_deltas), 4)},
                            {"metric": "bounce_rate_lt_10s", "value": round(bounce_rate, 6)},
                            {"metric": "median_click_depth_per_session", "value": round(_median([float(v) for v in sessions]), 4)},
                            {"metric": "switch_event_count", "value": int(len(switch_events))},
                            {"metric": "window_start", "value": window_start_dt.isoformat(timespec="seconds") if window_start_dt else ""},
                            {"metric": "window_end", "value": utcnow_naive().isoformat(timespec="seconds")},
                        ]
                    )
                    combined_baseline_export = pd.concat(
                        [
                            baseline_summary_df.assign(section="summary"),
                            wf_df.rename(columns={"median_transition_seconds": "value"}).assign(section="workflow"),
                        ],
                        ignore_index=True,
                    )
                    ex1, ex2 = st.columns(2)
                    with ex1:
                        st.download_button(
                            "Download Baseline Metrics CSV",
                            data=combined_baseline_export.to_csv(index=False).encode("utf-8"),
                            file_name=f"ux_baseline_metrics_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                            mime="text/csv",
                            key="admin_nav_baseline_metrics_csv_btn",
                        )
                    with ex2:
                        if st.button("Record Baseline Snapshot Event", key="admin_nav_baseline_snapshot_btn"):
                            try:
                                repo.record_audit_event(
                                    entity_type="navigation_baseline",
                                    entity_id=None,
                                    action="snapshot",
                                    actor=user.username,
                                    changes={
                                        "environment": settings.app_env,
                                        "recorded_at": utcnow_naive().isoformat(timespec="seconds"),
                                        "summary": baseline_summary_df.to_dict(orient="records"),
                                        "workflow_handoffs": wf_df.to_dict(orient="records"),
                                    },
                                )
                                st.success("Baseline metrics snapshot recorded.")
                            except Exception as exc:
                                st.error(f"Unable to record baseline snapshot: {exc}")
    
        st.markdown("### Workspace Feedback Insights")
        st.caption("Aggregated from audit events (`entity_type=workspace_feedback`) to prioritize UX fixes.")
        load_workspace_feedback_insights = st.checkbox(
            "Load Workspace Feedback Insights (slower)",
            value=False,
            key="admin_load_workspace_feedback_insights",
        )
        if not load_workspace_feedback_insights:
            st.caption(
                "Workspace feedback insights are skipped by default. "
                "Enable `Load Workspace Feedback Insights (slower)` to fetch them."
            )
        else:
            feedback_lookback_days = st.number_input(
                "Feedback Lookback Window (days)",
                min_value=1,
                max_value=365,
                value=30,
                step=1,
                key="admin_workspace_feedback_lookback_days",
            )
            feedback_since_dt = utcnow_naive() - timedelta(days=int(feedback_lookback_days))
            if window_start_dt is not None and window_start_dt > feedback_since_dt:
                feedback_since_dt = window_start_dt
            feedback_rows = repo.db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "workspace_feedback",
                    AuditLog.created_at >= feedback_since_dt,
                )
                .order_by(AuditLog.created_at.desc())
                .limit(2000)
            ).all()
            if not feedback_rows:
                st.caption("No workspace feedback events in the selected window.")
            else:
                feedback_counter: Counter[str] = Counter()
                sentiment_counter: Counter[str] = Counter()
                flattened_feedback_rows: list[dict] = []
                for row in feedback_rows:
                    payload = _audit_changes(row)
                    workspace = str(payload.get("workspace") or "unknown").strip().lower() or "unknown"
                    sentiment = str(payload.get("sentiment") or "unknown").strip().lower() or "unknown"
                    note = str(payload.get("note") or "").strip()
                    feedback_counter[workspace] += 1
                    sentiment_counter[sentiment] += 1
                    flattened_feedback_rows.append(
                        {
                            "id": int(row.id),
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "actor": str(row.actor or ""),
                            "workspace": workspace,
                            "sentiment": sentiment,
                            "note": note,
                        }
                    )
    
                total_feedback = len(flattened_feedback_rows)
                down_count = int(sentiment_counter.get("down", 0))
                up_count = int(sentiment_counter.get("up", 0))
                down_rate = (down_count / total_feedback) if total_feedback else 0.0
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Feedback Events", total_feedback)
                f2.metric("Needs Improvement", down_count)
                f3.metric("Helpful", up_count)
                f4.metric("Needs Improvement Rate", f"{down_rate * 100:.1f}%")
    
                by_workspace_df = pd.DataFrame(
                    [
                        {
                            "workspace": workspace,
                            "feedback_count": count,
                            "needs_improvement": int(
                                len(
                                    [
                                        r
                                        for r in flattened_feedback_rows
                                        if r["workspace"] == workspace and r["sentiment"] == "down"
                                    ]
                                )
                            ),
                        }
                        for workspace, count in feedback_counter.most_common(50)
                    ]
                )
                if not by_workspace_df.empty:
                    by_workspace_df["needs_improvement_rate"] = by_workspace_df.apply(
                        lambda r: round(
                            float(r["needs_improvement"]) / float(r["feedback_count"])
                            if float(r["feedback_count"]) > 0
                            else 0.0,
                            4,
                        ),
                        axis=1,
                    )
                by_sentiment_df = pd.DataFrame(
                    [{"sentiment": k, "count": v} for k, v in sentiment_counter.items()]
                ).sort_values(by="count", ascending=False)
                fc1, fc2 = st.columns(2)
                with fc1:
                    st.markdown("#### Feedback by Workspace")
                    st.dataframe(by_workspace_df, use_container_width=True, hide_index=True)
                with fc2:
                    st.markdown("#### Feedback by Sentiment")
                    st.dataframe(by_sentiment_df, use_container_width=True, hide_index=True)
    
                notes_only_df = pd.DataFrame([r for r in flattened_feedback_rows if r.get("note")]).sort_values(
                    by="created_at", ascending=False
                )
                with st.expander("Recent Feedback Notes", expanded=False):
                    if notes_only_df.empty:
                        st.caption("No note text submitted in the selected window.")
                    else:
                        st.dataframe(notes_only_df.head(300), use_container_width=True, hide_index=True)
                        note_options = {
                            (
                                f"#{int(r['id'])} | {r['workspace']} | {r['sentiment']} | "
                                f"{str(r['created_at'])[:19]} | {str(r['note'])[:70]}"
                            ): r
                            for _, r in notes_only_df.head(300).iterrows()
                        }
                        selected_feedback_label = st.selectbox(
                            "Create follow-up from feedback note",
                            options=["None"] + list(note_options.keys()),
                            key="admin_workspace_feedback_followup_select",
                        )
                        followup_owner = st.text_input(
                            "Follow-up Owner",
                            value=user.username,
                            key="admin_workspace_feedback_followup_owner",
                        )
                        followup_priority = st.selectbox(
                            "Follow-up Priority",
                            options=["high", "medium", "low"],
                            index=1,
                            key="admin_workspace_feedback_followup_priority",
                        )
                        followup_due_days = st.number_input(
                            "Due in Days",
                            min_value=0,
                            max_value=60,
                            value=7,
                            step=1,
                            key="admin_workspace_feedback_followup_due_days",
                        )
                        if st.button(
                            "Create Follow-up Task From Selected Feedback",
                            key="admin_workspace_feedback_create_followup_btn",
                            disabled=selected_feedback_label == "None",
                        ):
                            selected_row = note_options.get(selected_feedback_label)
                            if not selected_row:
                                st.error("Select a feedback note first.")
                            else:
                                try:
                                    task_id = f"wf-feedback-{int(selected_row['id'])}-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                                    due_date = (utcnow_naive() + timedelta(days=int(followup_due_days))).date()
                                    workspace = str(selected_row.get("workspace") or "unknown").strip().lower()
                                    sentiment = str(selected_row.get("sentiment") or "unknown").strip().lower()
                                    note = str(selected_row.get("note") or "").strip()
                                    repo.record_audit_event(
                                        entity_type="workspace_followup",
                                        entity_id=None,
                                        action="create",
                                        actor=user.username,
                                        changes={
                                            "task_id": task_id,
                                            "workflow": f"feedback:{workspace}",
                                            "title": f"[feedback/{sentiment}] {workspace} UX follow-up",
                                            "owner": str(followup_owner or user.username).strip() or user.username,
                                            "priority": str(followup_priority).strip().lower(),
                                            "due_date": due_date.isoformat(),
                                            "note": note,
                                            "source_feedback_id": int(selected_row["id"]),
                                            "source_workspace": workspace,
                                            "source_sentiment": sentiment,
                                        },
                                    )
                                    st.success(f"Created follow-up task `{task_id}`.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Unable to create follow-up task from feedback: {exc}")
                st.download_button(
                    "Download Workspace Feedback CSV",
                    data=pd.DataFrame(flattened_feedback_rows).to_csv(index=False).encode("utf-8"),
                    file_name=f"workspace_feedback_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    key="admin_workspace_feedback_csv_btn",
                )
                feedback_bundle_buffer = BytesIO()
                with zipfile.ZipFile(feedback_bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                    feedback_events_df = pd.DataFrame(flattened_feedback_rows)
                    feedback_events_df.insert(0, "environment", settings.app_env)
                    feedback_events_df.insert(1, "lookback_days", int(feedback_lookback_days))
                    bundle_zip.writestr("workspace_feedback_events.csv", feedback_events_df.to_csv(index=False))
                    workspace_export_df = by_workspace_df.copy()
                    workspace_export_df.insert(0, "environment", settings.app_env)
                    bundle_zip.writestr("workspace_feedback_by_workspace.csv", workspace_export_df.to_csv(index=False))
                    sentiment_export_df = by_sentiment_df.copy()
                    sentiment_export_df.insert(0, "environment", settings.app_env)
                    bundle_zip.writestr("workspace_feedback_by_sentiment.csv", sentiment_export_df.to_csv(index=False))
                    notes_export_df = notes_only_df.copy()
                    if not notes_export_df.empty:
                        notes_export_df.insert(0, "environment", settings.app_env)
                    bundle_zip.writestr("workspace_feedback_notes.csv", notes_export_df.to_csv(index=False))
                    summary_export_df = pd.DataFrame(
                        [
                            {
                                "environment": settings.app_env,
                                "lookback_days": int(feedback_lookback_days),
                                "feedback_events": int(total_feedback),
                                "needs_improvement_count": int(down_count),
                                "helpful_count": int(up_count),
                                "needs_improvement_rate_pct": round(float(down_rate) * 100.0, 2),
                                "generated_at_utc": utcnow_naive().isoformat(),
                            }
                        ]
                    )
                    bundle_zip.writestr("workspace_feedback_summary.csv", summary_export_df.to_csv(index=False))
                feedback_bundle_buffer.seek(0)
                st.download_button(
                    "Export Workspace Feedback Governance Bundle (ZIP)",
                    data=feedback_bundle_buffer.getvalue(),
                    file_name=(
                        f"workspace_feedback_governance_bundle_{settings.app_env}_"
                        f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
                    ),
                    mime="application/zip",
                    key="admin_workspace_feedback_bundle_zip_btn",
                )

        st.markdown("### Workspace Rollout Parity Checker")
        st.caption(
            "Validates permission coverage and recent audit evidence across legacy vs unified workflow contracts."
        )
        lookback_days = st.number_input(
            "Audit Lookback Window (days)",
            min_value=1,
            max_value=180,
            value=30,
            step=1,
            key="admin_parity_lookback_days",
        )
        current_min_task_events = max(0, int(get_runtime_int(repo, "ux_parity_min_task_completion_events", 1)))
        min_task_events_input = st.number_input(
            "Minimum Task-Completion Events Per Workflow",
            min_value=0,
            max_value=100,
            value=int(current_min_task_events),
            step=1,
            key="admin_parity_min_task_events",
            help="If >0, workflows with configured task telemetry must meet this threshold in the lookback window.",
        )
        if int(min_task_events_input) != int(current_min_task_events):
            if st.button("Save Task-Completion Threshold", key="admin_parity_save_min_task_events"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ux_parity_min_task_completion_events",
                        value=str(int(min_task_events_input)),
                        value_type="int",
                        description="Minimum workspace task-completion events required in parity lookback window per workflow.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Task-completion threshold saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save threshold: {exc}")
        load_parity_audit_evidence = st.checkbox(
            "Load Parity Audit Evidence (slower)",
            value=False,
            key="admin_parity_load_audit_evidence",
            help="Defers the large audit-log scan used for parity evidence and task-completion counters.",
        )
        since_dt = utcnow_naive() - timedelta(days=int(lookback_days))
        parity_specs = _workspace_parity_specs()
        role_permission_map = st.session_state.get("auth_role_permissions", DEFAULT_PERMISSIONS)
        if load_parity_audit_evidence:
            audit_rows = repo.db.scalars(
                select(AuditLog).where(AuditLog.created_at >= since_dt).order_by(AuditLog.created_at.desc()).limit(5000)
            ).all()
        else:
            audit_rows = []
            st.caption(
                "Audit evidence loading is deferred. Enable `Load Parity Audit Evidence (slower)` for full audit/task gap checks."
            )
        min_task_events = int(min_task_events_input)
        task_completion_counts: Counter[str] = Counter()
        for row in audit_rows:
            if str(row.entity_type or "").strip().lower() != "workspace_task_completion":
                continue
            if str(row.action or "").strip().lower() != "complete":
                continue
            payload = _audit_changes(row)
            workflow_key = str(payload.get("workflow") or "").strip().lower()
            if workflow_key:
                task_completion_counts[workflow_key] += 1

        parity_rows: list[dict] = []
        for spec in parity_specs:
            required_permission = str(spec.get("required_permission") or "").strip()
            viewer_ok = required_permission in set(role_permission_map.get("viewer", set()))
            ops_ok = required_permission in set(role_permission_map.get("ops", set()))
            admin_ok = True  # admin is super-role by policy

            entity_types = {str(v).strip().lower() for v in spec.get("audit_entity_types", []) if str(v).strip()}
            actions = {str(v).strip().lower() for v in spec.get("audit_actions", []) if str(v).strip()}
            observed = False if load_parity_audit_evidence else True
            observed_count = 0
            for row in audit_rows:
                row_entity = str(row.entity_type or "").strip().lower()
                row_action = str(row.action or "").strip().lower()
                if (not entity_types or row_entity in entity_types) and (not actions or row_action in actions):
                    observed = True
                    observed_count += 1
            task_workflow_keys = [
                str(v or "").strip().lower()
                for v in spec.get("task_completion_workflows", [])
                if str(v or "").strip()
            ]
            task_count = (
                sum(int(task_completion_counts.get(k, 0)) for k in task_workflow_keys)
                if task_workflow_keys
                else 0
            )
            task_observed = True if not task_workflow_keys else task_count >= int(min_task_events)

            parity_rows.append(
                {
                    "workflow": spec.get("workflow"),
                    "legacy_surface": spec.get("legacy_surface"),
                    "unified_surface": spec.get("unified_surface"),
                    "required_permission": required_permission,
                    "viewer_has_permission": bool(viewer_ok),
                    "ops_has_permission": bool(ops_ok),
                    "admin_has_permission": bool(admin_ok),
                    "audit_check_enabled": bool(load_parity_audit_evidence),
                    "audit_observed_in_window": bool(observed),
                    "audit_match_count": int(observed_count),
                    "task_completion_required": bool(task_workflow_keys),
                    "task_completion_events": int(task_count),
                    "task_completion_observed": bool(task_observed),
                }
            )

        parity_df = pd.DataFrame(parity_rows)
        st.dataframe(parity_df, use_container_width=True, hide_index=True)
        permission_gap_df = parity_df[
            (~parity_df["ops_has_permission"]) | (~parity_df["admin_has_permission"])
        ]
        audit_gap_df = parity_df[
            (parity_df["audit_check_enabled"] == True) & (~parity_df["audit_observed_in_window"])
        ]
        task_gap_df = parity_df[
            (parity_df["task_completion_required"] == True)
            & (parity_df["task_completion_observed"] == False)
        ]
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Parity Workflows", len(parity_df))
        p2.metric("Permission Gaps", len(permission_gap_df))
        p3.metric("No Audit Evidence", len(audit_gap_df))
        p4.metric("No Task Completion Evidence", len(task_gap_df))
        open_followups_count = 0
        overdue_followups_count = 0
        followup_snapshot_rows: list[AuditLog] = []
        if load_parity_audit_evidence:
            followup_snapshot_rows = repo.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "workspace_followup")
                .order_by(AuditLog.created_at.desc())
                .limit(1000)
            ).all()
        if followup_snapshot_rows:
            created_by_key: dict[str, AuditLog] = {}
            created_payload_by_key: dict[str, dict[str, Any]] = {}
            resolved_keys: set[str] = set()
            for row in followup_snapshot_rows:
                payload = _audit_changes(row)
                task_key = str(payload.get("task_key") or "").strip()
                if not task_key:
                    continue
                action = str(row.action or "").strip().lower()
                if action == "create" and task_key not in created_by_key:
                    created_by_key[task_key] = row
                    created_payload_by_key[task_key] = payload if isinstance(payload, dict) else {}
                if action in {"resolve", "closed"}:
                    resolved_keys.add(task_key)
            today = utcnow_naive().date()
            for task_key, row in created_by_key.items():
                if task_key in resolved_keys:
                    continue
                open_followups_count += 1
                payload = created_payload_by_key.get(task_key, {})
                due_raw = str(payload.get("due_date") or "").strip()
                if due_raw:
                    try:
                        due_dt = datetime.fromisoformat(due_raw).date()
                        if due_dt < today:
                            overdue_followups_count += 1
                    except Exception:
                        pass

        latest_decision_row_for_score = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_parity_decision",
                AuditLog.action == "decision",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        ).first()
        latest_decision_value = (
            str(((latest_decision_row_for_score.changes or {}).get("decision") or "")).strip().lower()
            if latest_decision_row_for_score
            else ""
        )
        weight_permission_gap = max(
            0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_permission_gap", 12)))
        )
        weight_audit_gap = max(0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_audit_gap", 8)))
        )
        weight_overdue = max(
            0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_overdue_followup", 5)))
        )
        weight_task_gap = max(0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_task_gap", 6))))
        penalty_rejected = max(
            0, min(100, int(get_runtime_int(repo, "ux_readiness_penalty_rejected_decision", 25)))
        )
        penalty_missing_decision = max(
            0, min(100, int(get_runtime_int(repo, "ux_readiness_penalty_missing_decision", 10)))
        )
        score = 100
        score -= min(40, int(len(permission_gap_df) * weight_permission_gap))
        score -= min(30, int(len(audit_gap_df) * weight_audit_gap))
        score -= min(20, int(overdue_followups_count * weight_overdue))
        score -= min(20, int(len(task_gap_df) * weight_task_gap))
        if latest_decision_value == "rejected":
            score -= penalty_rejected
        if latest_decision_value not in {"approved", "rejected"}:
            score -= penalty_missing_decision
        score = max(0, min(100, score))
        readiness = "green" if score >= 85 else "yellow" if score >= 65 else "red"
        r1, r2, r3 = st.columns(3)
        r1.metric("Rollout Readiness Score", f"{score}/100")
        r2.metric("Open Follow-ups", open_followups_count)
        r3.metric("Overdue Follow-ups", overdue_followups_count)
        if readiness == "green":
            st.success("Readiness status: GREEN")
        elif readiness == "yellow":
            st.warning("Readiness status: YELLOW")
        else:
            st.error("Readiness status: RED")
        with st.expander("Readiness Score Weights", expanded=False):
            st.caption("Tune score strictness per environment.")
            preset_map = {
                "conservative": {
                    "ux_readiness_weight_permission_gap": 16,
                    "ux_readiness_weight_audit_gap": 12,
                    "ux_readiness_weight_overdue_followup": 8,
                    "ux_readiness_weight_task_gap": 10,
                    "ux_readiness_penalty_rejected_decision": 35,
                    "ux_readiness_penalty_missing_decision": 15,
                },
                "balanced": {
                    "ux_readiness_weight_permission_gap": 12,
                    "ux_readiness_weight_audit_gap": 8,
                    "ux_readiness_weight_overdue_followup": 5,
                    "ux_readiness_weight_task_gap": 6,
                    "ux_readiness_penalty_rejected_decision": 25,
                    "ux_readiness_penalty_missing_decision": 10,
                },
                "aggressive": {
                    "ux_readiness_weight_permission_gap": 8,
                    "ux_readiness_weight_audit_gap": 5,
                    "ux_readiness_weight_overdue_followup": 3,
                    "ux_readiness_weight_task_gap": 3,
                    "ux_readiness_penalty_rejected_decision": 15,
                    "ux_readiness_penalty_missing_decision": 5,
                },
            }
            current_weights = {
                "ux_readiness_weight_permission_gap": int(weight_permission_gap),
                "ux_readiness_weight_audit_gap": int(weight_audit_gap),
                "ux_readiness_weight_overdue_followup": int(weight_overdue),
                "ux_readiness_weight_task_gap": int(weight_task_gap),
                "ux_readiness_penalty_rejected_decision": int(penalty_rejected),
                "ux_readiness_penalty_missing_decision": int(penalty_missing_decision),
            }
            current_preset_name = "custom"
            for preset_name, preset_values in preset_map.items():
                if all(int(current_weights.get(k, -1)) == int(v) for k, v in preset_values.items()):
                    current_preset_name = preset_name
                    break
            st.caption(f"Current preset match: `{current_preset_name}`")
            st.caption("Preset profiles")
            pcol1, pcol2, pcol3 = st.columns(3)
            with pcol1:
                apply_conservative = st.button(
                    "Apply Conservative",
                    key="admin_readiness_preset_conservative",
                    use_container_width=True,
                )
            with pcol2:
                apply_balanced = st.button(
                    "Apply Balanced",
                    key="admin_readiness_preset_balanced",
                    use_container_width=True,
                )
            with pcol3:
                apply_aggressive = st.button(
                    "Apply Aggressive",
                    key="admin_readiness_preset_aggressive",
                    use_container_width=True,
                )
            selected_preset = ""
            if apply_conservative:
                selected_preset = "conservative"
            elif apply_balanced:
                selected_preset = "balanced"
            elif apply_aggressive:
                selected_preset = "aggressive"
            if selected_preset:
                try:
                    for key, value in preset_map[selected_preset].items():
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=key,
                            value=str(int(value)),
                            value_type="int",
                            description=f"Readiness scoring preset `{selected_preset}` value.",
                            is_active=True,
                            actor=user.username,
                        )
                    st.success(f"Applied `{selected_preset}` readiness preset.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply readiness preset: {exc}")

            with st.form("admin_readiness_weights_form"):
                w1, w2, w3 = st.columns(3)
                with w1:
                    edit_weight_permission_gap = st.number_input(
                        "Penalty per Permission Gap",
                        min_value=0,
                        max_value=50,
                        value=int(weight_permission_gap),
                        step=1,
                    )
                    edit_weight_audit_gap = st.number_input(
                        "Penalty per Audit Gap",
                        min_value=0,
                        max_value=50,
                        value=int(weight_audit_gap),
                        step=1,
                    )
                with w2:
                    edit_weight_overdue = st.number_input(
                        "Penalty per Overdue Follow-up",
                        min_value=0,
                        max_value=50,
                        value=int(weight_overdue),
                        step=1,
                    )
                    edit_weight_task_gap = st.number_input(
                        "Penalty per Task-Completion Gap",
                        min_value=0,
                        max_value=50,
                        value=int(weight_task_gap),
                        step=1,
                    )
                    edit_penalty_rejected = st.number_input(
                        "Penalty: Rejected Decision",
                        min_value=0,
                        max_value=100,
                        value=int(penalty_rejected),
                        step=1,
                    )
                with w3:
                    edit_penalty_missing_decision = st.number_input(
                        "Penalty: Missing Decision",
                        min_value=0,
                        max_value=100,
                        value=int(penalty_missing_decision),
                        step=1,
                    )
                save_weights = st.form_submit_button("Save Readiness Weights")
            if save_weights:
                try:
                    weight_updates = [
                        (
                            "ux_readiness_weight_permission_gap",
                            str(int(edit_weight_permission_gap)),
                            "Penalty per permission gap workflow.",
                        ),
                        (
                            "ux_readiness_weight_audit_gap",
                            str(int(edit_weight_audit_gap)),
                            "Penalty per missing-audit-evidence workflow.",
                        ),
                        (
                            "ux_readiness_weight_overdue_followup",
                            str(int(edit_weight_overdue)),
                            "Penalty per overdue open follow-up task.",
                        ),
                        (
                            "ux_readiness_weight_task_gap",
                            str(int(edit_weight_task_gap)),
                            "Penalty per workflow missing task-completion evidence.",
                        ),
                        (
                            "ux_readiness_penalty_rejected_decision",
                            str(int(edit_penalty_rejected)),
                            "Penalty when latest release decision is rejected.",
                        ),
                        (
                            "ux_readiness_penalty_missing_decision",
                            str(int(edit_penalty_missing_decision)),
                            "Penalty when no latest approved/rejected decision exists.",
                        ),
                    ]
                    for key, value, desc in weight_updates:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=key,
                            value=value,
                            value_type="int",
                            description=desc,
                            is_active=True,
                            actor=user.username,
                        )
                    st.success("Readiness score weights saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save readiness weights: {exc}")
        if not permission_gap_df.empty:
            st.warning("Permission parity gaps detected for one or more workflows.")
            st.dataframe(
                permission_gap_df[
                    [
                        "workflow",
                        "required_permission",
                        "viewer_has_permission",
                        "ops_has_permission",
                        "admin_has_permission",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
        if not audit_gap_df.empty:
            st.info(
                "Some workflows have no recent audit evidence in this window. Run workflow smoke tests before cutover."
            )
            st.dataframe(
                audit_gap_df[["workflow", "legacy_surface", "unified_surface", "required_permission"]],
                use_container_width=True,
                hide_index=True,
            )
        if not task_gap_df.empty:
            st.info(
                f"Some workflows are missing task-completion evidence (threshold={int(min_task_events)} event(s))."
            )
            st.dataframe(
                task_gap_df[["workflow", "task_completion_events", "task_completion_observed"]],
                use_container_width=True,
                hide_index=True,
            )
        s1, s2 = st.columns(2)
        with s1:
            st.download_button(
                "Download Parity Snapshot CSV",
                data=parity_df.to_csv(index=False).encode("utf-8"),
                file_name=(
                    f"workspace_parity_snapshot_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                ),
                mime="text/csv",
                key="admin_parity_snapshot_csv_btn",
            )
        with s2:
            if st.button("Record Parity Snapshot Event", key="admin_record_parity_snapshot_btn"):
                try:
                    snapshot_ts = utcnow_naive()
                    repo.record_audit_event(
                        entity_type="workspace_parity",
                        entity_id=None,
                        action="snapshot",
                        actor=user.username,
                        changes={
                            "environment": settings.app_env,
                            "recorded_at": snapshot_ts.isoformat(timespec="seconds"),
                            "lookback_days": int(lookback_days),
                            "since": since_dt.isoformat(timespec="seconds"),
                            "workflow_count": int(len(parity_df)),
                            "permission_gap_count": int(len(permission_gap_df)),
                            "audit_gap_count": int(len(audit_gap_df)),
                            "workflows": parity_df.to_dict(orient="records"),
                        },
                    )
                    st.success("Workspace parity snapshot recorded to audit log.")
                except Exception as exc:
                    st.error(f"Unable to record parity snapshot: {exc}")

        load_parity_history = st.checkbox(
            "Load Parity History + Follow-up Tables (slower)",
            value=False,
            key="admin_parity_load_history",
            help="Defers recent snapshots, release decision history, and follow-up task table hydration.",
        )
        recent_df = pd.DataFrame()
        st.markdown("#### Recent Parity Snapshots")
        parity_snapshot_rows: list[AuditLog] = []
        if load_parity_history:
            parity_snapshot_rows = repo.db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "workspace_parity",
                    AuditLog.action == "snapshot",
                )
                .order_by(AuditLog.created_at.desc())
                .limit(30)
            ).all()
        else:
            st.caption("Parity history loading is deferred. Enable the load toggle above to hydrate history tables.")
        if not parity_snapshot_rows:
            st.caption("No parity snapshots recorded yet.")
        else:
            recent_rows: list[dict] = []
            snapshot_payload_by_id: dict[int, dict] = {}
            for row in parity_snapshot_rows:
                payload = _audit_changes(row)
                snapshot_payload_by_id[int(row.id)] = payload if isinstance(payload, dict) else {}
                workflows = payload.get("workflows") if isinstance(payload, dict) else []
                recent_rows.append(
                    {
                        "id": row.id,
                        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                        "actor": row.actor,
                        "environment": str(payload.get("environment") or ""),
                        "lookback_days": int(payload.get("lookback_days") or 0),
                        "workflow_count": int(payload.get("workflow_count") or 0),
                        "permission_gap_count": int(payload.get("permission_gap_count") or 0),
                        "audit_gap_count": int(payload.get("audit_gap_count") or 0),
                        "workflows_json_len": len(workflows) if isinstance(workflows, list) else 0,
                    }
                )
            recent_df = pd.DataFrame(recent_rows)
            st.dataframe(recent_df, use_container_width=True, hide_index=True)

            snapshot_option_map = {
                (
                    f"#{r['id']} | {r['created_at']} | env={r['environment']} | "
                    f"perm_gap={r['permission_gap_count']} | audit_gap={r['audit_gap_count']}"
                ): r["id"]
                for r in recent_rows
            }
            selected_snapshot_label = st.selectbox(
                "Inspect Snapshot",
                options=list(snapshot_option_map.keys()),
                key="admin_parity_snapshot_inspect_select",
            )
            selected_snapshot_id = int(snapshot_option_map[selected_snapshot_label])
            selected_snapshot = next((r for r in parity_snapshot_rows if int(r.id) == selected_snapshot_id), None)
            if selected_snapshot is not None:
                payload = selected_snapshot.changes if isinstance(selected_snapshot.changes, dict) else {}
                workflow_rows = payload.get("workflows") if isinstance(payload, dict) else []
                if isinstance(workflow_rows, list) and workflow_rows:
                    st.caption("Snapshot workflow details")
                    st.dataframe(pd.DataFrame(workflow_rows), use_container_width=True, hide_index=True)
                with st.expander("Snapshot Raw Payload", expanded=False):
                    st.json(payload)

                st.markdown("#### Release Decision")
                with st.form("admin_parity_release_decision_form"):
                    d1, d2 = st.columns([1, 2])
                    with d1:
                        decision = st.selectbox(
                            "Decision",
                            options=["approved", "rejected"],
                            key="admin_parity_release_decision_value",
                        )
                    with d2:
                        decision_note = st.text_input(
                            "Decision Note (optional)",
                            key="admin_parity_release_decision_note",
                            placeholder="Reason, blocker, or follow-up action.",
                        )
                    submit_decision = st.form_submit_button("Record Release Decision")
                if submit_decision:
                    try:
                        repo.record_audit_event(
                            entity_type="workspace_parity_decision",
                            entity_id=int(selected_snapshot.id),
                            action="decision",
                            actor=user.username,
                            changes={
                                "snapshot_id": int(selected_snapshot.id),
                                "decision": decision,
                                "note": (decision_note or "").strip(),
                                "environment": settings.app_env,
                                "snapshot_created_at": selected_snapshot.created_at.isoformat(timespec="seconds")
                                if selected_snapshot.created_at
                                else "",
                            },
                        )
                        if get_runtime_bool(repo, "slack_notify_parity_decisions", False):
                            text = build_slack_alert_text(
                                repo,
                                event_type="parity_decision",
                                default_template=(
                                    ":clipboard: *GoldenStackers* parity release decision `{decision}`\n"
                                    "- Env: `{env}`\n"
                                    "- Snapshot: `#{snapshot_id}`\n"
                                    "- Actor: `{actor}`\n"
                                    "- Note: `{note}`"
                                ),
                                context={
                                    "decision": decision,
                                    "snapshot_id": int(selected_snapshot.id),
                                    "actor": user.username,
                                    "note": (decision_note or "").strip() or "(none)",
                                },
                            )
                            dispatch_slack_alert(
                                repo,
                                actor=user.username,
                                text=text,
                                event_type="parity_decision",
                                severity="warning" if decision == "rejected" else "info",
                            )
                        st.success(f"Recorded release decision `{decision}` for snapshot #{selected_snapshot.id}.")
                    except Exception as exc:
                        st.error(f"Unable to record release decision: {exc}")

                st.markdown("#### Create Follow-up Task")
                with st.form("admin_parity_followup_create_form"):
                    f1, f2 = st.columns(2)
                    with f1:
                        followup_title = st.text_input(
                            "Task Title",
                            key="admin_parity_followup_title",
                            placeholder="Example: fix missing shipping parity evidence",
                        )
                        followup_owner = st.text_input(
                            "Owner",
                            key="admin_parity_followup_owner",
                            value=user.username,
                        )
                    with f2:
                        followup_due_date = st.date_input(
                            "Due Date (optional)",
                            key="admin_parity_followup_due_date",
                            value=utcnow_naive().date(),
                        )
                        followup_priority = st.selectbox(
                            "Priority",
                            options=["low", "medium", "high", "critical"],
                            index=1,
                            key="admin_parity_followup_priority",
                        )
                    followup_note = st.text_area(
                        "Task Notes (optional)",
                        key="admin_parity_followup_note",
                        placeholder="Context, acceptance criteria, or links.",
                    )
                    submit_followup = st.form_submit_button("Create Follow-up Task")
                if submit_followup:
                    if not str(followup_title or "").strip():
                        st.error("Task title is required.")
                    else:
                        try:
                            task_key = f"snapshot-{int(selected_snapshot.id)}-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                            repo.record_audit_event(
                                entity_type="workspace_followup",
                                entity_id=int(selected_snapshot.id),
                                action="create",
                                actor=user.username,
                                changes={
                                    "task_key": task_key,
                                    "snapshot_id": int(selected_snapshot.id),
                                    "title": str(followup_title).strip(),
                                    "owner": str(followup_owner).strip() or user.username,
                                    "priority": str(followup_priority).strip().lower(),
                                    "due_date": followup_due_date.isoformat() if followup_due_date else "",
                                    "note": str(followup_note or "").strip(),
                                    "status": "open",
                                    "environment": settings.app_env,
                                },
                            )
                            st.success(f"Follow-up task created (`{task_key}`).")
                        except Exception as exc:
                            st.error(f"Unable to create follow-up task: {exc}")

            st.markdown("#### Compare Two Snapshots")
            if len(recent_rows) < 2:
                st.caption("Need at least 2 snapshots to compare.")
            else:
                snapshot_labels = [
                    (
                        f"#{r['id']} | {r['created_at']} | env={r['environment']} | "
                        f"perm_gap={r['permission_gap_count']} | audit_gap={r['audit_gap_count']}"
                    )
                    for r in recent_rows
                ]
                label_to_id = {label: int(recent_rows[idx]["id"]) for idx, label in enumerate(snapshot_labels)}
                dc1, dc2 = st.columns(2)
                with dc1:
                    baseline_label = st.selectbox(
                        "Baseline Snapshot",
                        options=snapshot_labels,
                        index=min(1, len(snapshot_labels) - 1),
                        key="admin_parity_compare_baseline",
                    )
                with dc2:
                    compare_label = st.selectbox(
                        "Compare Snapshot",
                        options=snapshot_labels,
                        index=0,
                        key="admin_parity_compare_target",
                    )
                baseline_id = label_to_id.get(baseline_label)
                compare_id = label_to_id.get(compare_label)
                if baseline_id == compare_id:
                    st.caption("Select two different snapshots to compute deltas.")
                else:
                    base_payload = snapshot_payload_by_id.get(int(baseline_id or 0), {})
                    cmp_payload = snapshot_payload_by_id.get(int(compare_id or 0), {})
                    base_perm_gap = int(base_payload.get("permission_gap_count") or 0)
                    cmp_perm_gap = int(cmp_payload.get("permission_gap_count") or 0)
                    base_audit_gap = int(base_payload.get("audit_gap_count") or 0)
                    cmp_audit_gap = int(cmp_payload.get("audit_gap_count") or 0)
                    dd1, dd2 = st.columns(2)
                    dd1.metric(
                        "Permission Gap Delta",
                        f"{cmp_perm_gap - base_perm_gap:+d}",
                        help=f"Baseline={base_perm_gap}, Compare={cmp_perm_gap}",
                    )
                    dd2.metric(
                        "Audit Gap Delta",
                        f"{cmp_audit_gap - base_audit_gap:+d}",
                        help=f"Baseline={base_audit_gap}, Compare={cmp_audit_gap}",
                    )

                    base_workflows = base_payload.get("workflows") if isinstance(base_payload, dict) else []
                    cmp_workflows = cmp_payload.get("workflows") if isinstance(cmp_payload, dict) else []
                    base_map = {
                        str(row.get("workflow") or ""): row
                        for row in base_workflows
                        if isinstance(row, dict) and str(row.get("workflow") or "").strip()
                    }
                    cmp_map = {
                        str(row.get("workflow") or ""): row
                        for row in cmp_workflows
                        if isinstance(row, dict) and str(row.get("workflow") or "").strip()
                    }
                    all_workflows = sorted(set(base_map.keys()) | set(cmp_map.keys()))
                    diff_rows: list[dict] = []
                    for wf in all_workflows:
                        b = base_map.get(wf, {})
                        c = cmp_map.get(wf, {})
                        b_perm_ok = bool(b.get("ops_has_permission", False)) and bool(
                            b.get("admin_has_permission", False)
                        )
                        c_perm_ok = bool(c.get("ops_has_permission", False)) and bool(
                            c.get("admin_has_permission", False)
                        )
                        b_audit_ok = bool(b.get("audit_observed_in_window", False))
                        c_audit_ok = bool(c.get("audit_observed_in_window", False))
                        diff_rows.append(
                            {
                                "workflow": wf,
                                "perm_ok_baseline": b_perm_ok,
                                "perm_ok_compare": c_perm_ok,
                                "perm_changed": b_perm_ok != c_perm_ok,
                                "audit_ok_baseline": b_audit_ok,
                                "audit_ok_compare": c_audit_ok,
                                "audit_changed": b_audit_ok != c_audit_ok,
                                "audit_match_count_delta": int(c.get("audit_match_count") or 0)
                                - int(b.get("audit_match_count") or 0),
                            }
                        )
                    if diff_rows:
                        diff_df = pd.DataFrame(diff_rows)
                        st.dataframe(diff_df, use_container_width=True, hide_index=True)

            decisions_df = pd.DataFrame()
            st.markdown("#### Recent Release Decisions")
            decision_rows: list[AuditLog] = []
            if load_parity_history:
                decision_rows = repo.db.scalars(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "workspace_parity_decision",
                        AuditLog.action == "decision",
                    )
                    .order_by(AuditLog.created_at.desc())
                    .limit(50)
                ).all()
            decision_payload_by_id: dict[int, dict[str, Any]] = {}
            for row in decision_rows:
                decision_payload_by_id[int(row.id)] = _audit_changes(row)
            latest_snapshot_row = parity_snapshot_rows[0] if parity_snapshot_rows else None
            latest_decision_row = decision_rows[0] if decision_rows else None
            latest_approved_row = next(
                (
                    row
                    for row in decision_rows
                    if str((decision_payload_by_id.get(int(row.id), {}).get("decision") or "")).strip().lower()
                    == "approved"
                ),
                None,
            )
            status_cols = st.columns(3)
            with status_cols[0]:
                latest_decision = (
                    str((decision_payload_by_id.get(int(latest_decision_row.id), {}).get("decision") or "none"))
                    .strip()
                    .lower()
                    if latest_decision_row
                    else "none"
                )
                st.metric("Latest Decision", latest_decision)
            with status_cols[1]:
                st.metric(
                    "Latest Approved Snapshot",
                    f"#{latest_approved_row.entity_id}" if latest_approved_row and latest_approved_row.entity_id else "none",
                )
            with status_cols[2]:
                stale_days = 999
                if latest_approved_row and latest_approved_row.created_at:
                    stale_days = int((utcnow_naive() - latest_approved_row.created_at).days)
                st.metric("Approved Snapshot Age (days)", "n/a" if stale_days == 999 else str(stale_days))

            if latest_decision_row is None:
                st.warning("No release decision recorded yet. Capture a parity decision before cutover.")
            else:
                latest_decision_payload = decision_payload_by_id.get(int(latest_decision_row.id), {})
                latest_decision = str((latest_decision_payload.get("decision") or "")).strip().lower()
                decision_note = str((latest_decision_payload.get("note") or "")).strip()
                if latest_decision == "rejected":
                    st.error(
                        "Latest parity release decision is `rejected`. Resolve parity gaps and record a new decision before cutover."
                    )
                elif latest_decision == "approved":
                    st.success("Latest parity release decision is `approved`.")
                else:
                    st.info(f"Latest parity release decision: `{latest_decision}`")
                if decision_note:
                    st.caption(f"Latest decision note: {decision_note}")

            if latest_snapshot_row and latest_decision_row:
                latest_snapshot_id = int(latest_snapshot_row.id)
                latest_decision_snapshot_id = int(latest_decision_row.entity_id or 0)
                if latest_decision_snapshot_id != latest_snapshot_id:
                    st.warning(
                        "Latest snapshot does not have a matching release decision yet. "
                        "Record decision on current snapshot to keep go/no-go state current."
                    )

            if latest_approved_row and latest_approved_row.created_at:
                approved_age_days = int((utcnow_naive() - latest_approved_row.created_at).days)
                if approved_age_days >= 14:
                    st.warning(
                        f"Latest approved snapshot is {approved_age_days} day(s) old. "
                        "Re-run parity checks before release."
                    )

            if not decision_rows:
                st.caption("No release decisions recorded yet.")
            else:
                decision_table_rows: list[dict[str, Any]] = []
                for row in decision_rows:
                    payload = decision_payload_by_id.get(int(row.id), {})
                    decision_table_rows.append(
                        {
                            "id": row.id,
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "actor": row.actor,
                            "snapshot_id": row.entity_id,
                            "decision": payload.get("decision", ""),
                            "note": payload.get("note", ""),
                            "environment": payload.get("environment", ""),
                        }
                    )
                decisions_df = pd.DataFrame(decision_table_rows)
                st.dataframe(decisions_df, use_container_width=True, hide_index=True)

            display_df = pd.DataFrame()
            st.markdown("#### Follow-up Tasks")
            followup_rows: list[AuditLog] = []
            if load_parity_history:
                followup_rows = repo.db.scalars(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "workspace_followup")
                    .order_by(AuditLog.created_at.desc())
                    .limit(500)
                ).all()
            if not followup_rows:
                st.caption("No follow-up tasks recorded yet.")
            else:
                created_by_key: dict[str, AuditLog] = {}
                created_payload_by_key: dict[str, dict[str, Any]] = {}
                resolved_keys: set[str] = set()
                overdue_alerted_keys: set[str] = set()
                for row in followup_rows:
                    payload = _audit_changes(row)
                    task_key = str(payload.get("task_key") or "").strip()
                    if not task_key:
                        continue
                    action = str(row.action or "").strip().lower()
                    if action == "create" and task_key not in created_by_key:
                        created_by_key[task_key] = row
                        created_payload_by_key[task_key] = payload if isinstance(payload, dict) else {}
                    if action in {"resolve", "closed"}:
                        resolved_keys.add(task_key)
                    if action == "overdue_alert":
                        overdue_alerted_keys.add(task_key)

                open_task_rows: list[dict] = []
                today = utcnow_naive().date()
                priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}
                for task_key, row in created_by_key.items():
                    payload = created_payload_by_key.get(task_key, {})
                    is_open = task_key not in resolved_keys
                    due_raw = str(payload.get("due_date") or "").strip()
                    due_dt = None
                    if due_raw:
                        try:
                            due_dt = datetime.fromisoformat(due_raw).date()
                        except Exception:
                            due_dt = None
                    due_in_days = (due_dt - today).days if due_dt is not None else None
                    sla_status = "none"
                    if is_open:
                        if due_in_days is None:
                            sla_status = "no_due_date"
                        elif due_in_days < 0:
                            sla_status = "overdue"
                        elif due_in_days <= 2:
                            sla_status = "due_soon"
                        else:
                            sla_status = "on_track"
                    open_task_rows.append(
                        {
                            "task_key": task_key,
                            "status": "open" if is_open else "resolved",
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "created_by": row.actor,
                            "snapshot_id": payload.get("snapshot_id", row.entity_id),
                            "title": payload.get("title", ""),
                            "owner": payload.get("owner", ""),
                            "priority": payload.get("priority", ""),
                            "due_date": payload.get("due_date", ""),
                            "due_in_days": due_in_days if due_in_days is not None else "",
                            "sla_status": sla_status,
                            "note": payload.get("note", ""),
                            "_priority_rank": priority_rank.get(str(payload.get("priority") or "").strip().lower(), 4),
                        }
                    )
                open_task_rows.sort(
                    key=lambda r: (
                        0 if str(r.get("status")) == "open" else 1,
                        0
                        if str(r.get("sla_status") or "") == "overdue"
                        else 1 if str(r.get("sla_status") or "") == "due_soon" else 2,
                        int(r.get("_priority_rank") or 4),
                        str(r.get("due_date") or ""),
                    )
                )
                status_opts = sorted({str(r.get("status") or "") for r in open_task_rows if str(r.get("status") or "")})
                owner_opts = sorted({str(r.get("owner") or "") for r in open_task_rows if str(r.get("owner") or "")})
                priority_opts = sorted(
                    {str(r.get("priority") or "") for r in open_task_rows if str(r.get("priority") or "")}
                )
                f1, f2, f3 = st.columns(3)
                with f1:
                    status_filter = st.multiselect(
                        "Status Filter",
                        options=status_opts,
                        default=["open"] if "open" in status_opts else status_opts,
                        key="admin_parity_followup_status_filter",
                    )
                with f2:
                    owner_filter = st.multiselect(
                        "Owner Filter",
                        options=owner_opts,
                        default=owner_opts,
                        key="admin_parity_followup_owner_filter",
                    )
                with f3:
                    priority_filter = st.multiselect(
                        "Priority Filter",
                        options=priority_opts,
                        default=priority_opts,
                        key="admin_parity_followup_priority_filter",
                    )
                filtered_followups = [
                    row
                    for row in open_task_rows
                    if (not status_filter or str(row.get("status") or "") in set(status_filter))
                    and (not owner_filter or str(row.get("owner") or "") in set(owner_filter))
                    and (not priority_filter or str(row.get("priority") or "") in set(priority_filter))
                ]
                followups_df = pd.DataFrame(filtered_followups)
                if not followups_df.empty:
                    m1, m2, m3 = st.columns(3)
                    m1.metric(
                        "Open Tasks",
                        int((followups_df["status"] == "open").sum()),
                    )
                    m2.metric(
                        "Overdue",
                        int(((followups_df["status"] == "open") & (followups_df["sla_status"] == "overdue")).sum()),
                    )
                    m3.metric(
                        "Due Soon (<=2d)",
                        int(((followups_df["status"] == "open") & (followups_df["sla_status"] == "due_soon")).sum()),
                    )
                display_df = followups_df.drop(columns=["_priority_rank"], errors="ignore")
                st.dataframe(display_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Follow-up Tasks CSV",
                    data=display_df.to_csv(index=False).encode("utf-8"),
                    file_name=(
                        f"workspace_followup_tasks_{settings.app_env}_"
                        f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                    ),
                    mime="text/csv",
                    key="admin_parity_followup_csv_btn",
                    disabled=display_df.empty,
                )

                open_rows = [r for r in open_task_rows if str(r.get("status")) == "open"]
                if open_rows:
                    overdue_open_rows = [
                        r for r in open_rows if str(r.get("sla_status") or "") == "overdue"
                    ]
                    if overdue_open_rows:
                        send_overdue_btn = st.button(
                            "Send Overdue Alerts Now",
                            key="admin_parity_followup_send_overdue_btn",
                            help="Sends alerts for overdue tasks that have not yet received an overdue alert event.",
                        )
                        if send_overdue_btn:
                            if not get_runtime_bool(repo, "slack_notify_followup_overdue", False):
                                st.warning(
                                    "Overdue Slack alerts are disabled (`slack_notify_followup_overdue=false`)."
                                )
                            else:
                                sent_count = 0
                                queued_count = 0
                                skipped_count = 0
                                for task in overdue_open_rows:
                                    task_key = str(task.get("task_key") or "").strip()
                                    if not task_key or task_key in overdue_alerted_keys:
                                        skipped_count += 1
                                        continue
                                    try:
                                        text = build_slack_alert_text(
                                            repo,
                                            event_type="followup_overdue",
                                            default_template=(
                                                ":rotating_light: *GoldenStackers* rollout follow-up overdue\n"
                                                "- Env: `{env}`\n"
                                                "- Task: `{task_key}`\n"
                                                "- Title: `{title}`\n"
                                                "- Owner: `{owner}`\n"
                                                "- Due: `{due_date}`\n"
                                                "- Priority: `{priority}`"
                                            ),
                                            context={
                                                "task_key": task_key,
                                                "title": str(task.get("title") or ""),
                                                "owner": str(task.get("owner") or ""),
                                                "due_date": str(task.get("due_date") or ""),
                                                "priority": str(task.get("priority") or ""),
                                            },
                                        )
                                        dispatch_result = dispatch_slack_alert(
                                            repo,
                                            actor=user.username,
                                            text=text,
                                            event_type="followup_overdue",
                                            severity="warning",
                                        )
                                        repo.record_audit_event(
                                            entity_type="workspace_followup",
                                            entity_id=int(task.get("snapshot_id") or 0) or None,
                                            action="overdue_alert",
                                            actor=user.username,
                                            changes={
                                                "task_key": task_key,
                                                "status": str(dispatch_result.get("status") or ""),
                                                "queue_job_id": dispatch_result.get("queue_job_id"),
                                                "channel": dispatch_result.get("channel", ""),
                                                "environment": settings.app_env,
                                            },
                                        )
                                        if str(dispatch_result.get("status") or "") == "queued":
                                            queued_count += 1
                                        else:
                                            sent_count += 1
                                    except Exception:
                                        skipped_count += 1
                                st.success(
                                    f"Overdue alerts processed. sent={sent_count}, queued={queued_count}, skipped={skipped_count}."
                                )
                                st.rerun()
                    open_map = {
                        f"{r['task_key']} | owner={r['owner']} | priority={r['priority']} | due={r['due_date']}": r
                        for r in open_rows
                    }
                    selected_followup_label = st.selectbox(
                        "Resolve Follow-up Task",
                        options=list(open_map.keys()),
                        key="admin_parity_followup_resolve_select",
                    )
                    resolve_note = st.text_input(
                        "Resolution Note (optional)",
                        key="admin_parity_followup_resolve_note",
                        placeholder="What changed to close this blocker?",
                    )
                    if st.button("Mark Follow-up Resolved", key="admin_parity_followup_resolve_btn"):
                        try:
                            selected_task = open_map[selected_followup_label]
                            repo.record_audit_event(
                                entity_type="workspace_followup",
                                entity_id=int(selected_task.get("snapshot_id") or 0) or None,
                                action="resolve",
                                actor=user.username,
                                changes={
                                    "task_key": selected_task.get("task_key"),
                                    "resolution_note": str(resolve_note or "").strip(),
                                    "resolved_at": utcnow_naive().isoformat(timespec="seconds"),
                                    "status": "resolved",
                                    "environment": settings.app_env,
                                },
                            )
                            st.success(f"Marked follow-up `{selected_task.get('task_key')}` as resolved.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to resolve follow-up: {exc}")

            st.markdown("#### Parity Governance Bundle")
            parity_bundle_cache_key = f"admin_parity_governance_bundle_{settings.app_env}"
            if st.button(
                "Prepare Parity Governance Bundle",
                key="admin_parity_governance_bundle_prepare_btn",
                help="Builds ZIP once and caches it for download in this session.",
            ):
                try:
                    parity_bundle_buffer = BytesIO()
                    generated_at = utcnow_naive()
                    with zipfile.ZipFile(parity_bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                        readiness_summary_df = pd.DataFrame(
                            [
                                {
                                    "environment": settings.app_env,
                                    "lookback_days": int(lookback_days),
                                    "readiness_score": int(score),
                                    "readiness_status": str(readiness),
                                    "permission_gap_count": int(len(permission_gap_df)),
                                    "audit_gap_count": int(len(audit_gap_df)),
                                    "open_followups_count": int(open_followups_count),
                                    "overdue_followups_count": int(overdue_followups_count),
                                    "generated_at_utc": generated_at.isoformat(),
                                }
                            ]
                        )
                        bundle_zip.writestr("parity_readiness_summary.csv", readiness_summary_df.to_csv(index=False))
                        parity_export_df = parity_df.copy()
                        parity_export_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr("parity_workflows.csv", parity_export_df.to_csv(index=False))
                        perm_gap_export_df = permission_gap_df.copy()
                        if not perm_gap_export_df.empty:
                            perm_gap_export_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr("parity_permission_gaps.csv", perm_gap_export_df.to_csv(index=False))
                        audit_gap_export_df = audit_gap_df.copy()
                        if not audit_gap_export_df.empty:
                            audit_gap_export_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr("parity_audit_gaps.csv", audit_gap_export_df.to_csv(index=False))
                        snapshot_export_df = recent_df.copy()
                        if not snapshot_export_df.empty:
                            snapshot_export_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr("parity_recent_snapshots.csv", snapshot_export_df.to_csv(index=False))
                        decision_export_df = decisions_df.copy()
                        if not decision_export_df.empty:
                            decision_export_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr("parity_release_decisions.csv", decision_export_df.to_csv(index=False))
                        followup_export_df = display_df.copy()
                        if not followup_export_df.empty:
                            followup_export_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr("parity_followup_tasks_filtered.csv", followup_export_df.to_csv(index=False))
                    parity_bundle_buffer.seek(0)
                    st.session_state[parity_bundle_cache_key] = {
                        "bytes": parity_bundle_buffer.getvalue(),
                        "generated_at": generated_at.isoformat(timespec="seconds"),
                        "file_name": (
                            f"parity_governance_bundle_{settings.app_env}_"
                            f"{generated_at.strftime('%Y%m%d_%H%M%S')}.zip"
                        ),
                    }
                    st.success("Parity governance bundle prepared.")
                except Exception as exc:
                    st.error(f"Unable to prepare parity governance bundle: {exc}")
            parity_bundle_cached = st.session_state.get(parity_bundle_cache_key) or {}
            parity_bundle_bytes = parity_bundle_cached.get("bytes")
            if parity_bundle_bytes:
                st.caption(f"Prepared at {parity_bundle_cached.get('generated_at', '')} UTC.")
                st.download_button(
                    "Export Parity Governance Bundle (ZIP)",
                    data=parity_bundle_bytes,
                    file_name=str(parity_bundle_cached.get("file_name") or "parity_governance_bundle.zip"),
                    mime="application/zip",
                    key="admin_parity_governance_bundle_zip_btn",
                )
            else:
                st.caption("Prepare the parity governance bundle to enable download.")
