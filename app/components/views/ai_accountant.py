from __future__ import annotations

import csv
import json
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from typing import Any
from zoneinfo import ZoneInfo
import zipfile

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.auth import current_user, ensure_permission
from app.config import settings
from app.db.models import AuditLog
from app.repository import InventoryRepository
from app.components.views.shared import render_help_panel
from app.services.ai_orchestration import execute_comp_summary
from app.services.llm_runtime import describe_llm_runtime_chain
from app.services.notification_outbox import process_notification_outbox_row
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.utils.time import utcnow_naive


ACCOUNTING_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


AI_ACCOUNTANT_RECOMMENDED_RUNTIME_SETTINGS: tuple[dict[str, str], ...] = (
    {
        "key": "ai_accountant_monitor_enabled",
        "value": "true",
        "value_type": "bool",
        "description": "Enable the scheduled AI Accountant monitor.",
    },
    {
        "key": "ai_accountant_monitor_schedule_mode",
        "value": "interval",
        "value_type": "str",
        "description": "Run the AI Accountant monitor on an interval cadence.",
    },
    {
        "key": "ai_accountant_monitor_interval_hours",
        "value": "6",
        "value_type": "int",
        "description": "Run the AI Accountant monitor every six hours.",
    },
    {
        "key": "ai_accountant_monitor_timezone",
        "value": "America/Denver",
        "value_type": "str",
        "description": "IANA timezone used by the scheduled AI Accountant monitor.",
    },
    {
        "key": "ai_accountant_monitor_local_time",
        "value": "08:30",
        "value_type": "str",
        "description": "Local HH:MM trigger for daily AI Accountant monitor mode.",
    },
    {
        "key": "ai_accountant_monitor_lookback_days",
        "value": "30",
        "value_type": "int",
        "description": "Lookback window in days for scheduled AI Accountant monitor checks.",
    },
    {
        "key": "ai_accountant_monitor_min_severity",
        "value": "P1",
        "value_type": "str",
        "description": "Queue AI Accountant monitor alerts for P1 and higher findings.",
    },
    {
        "key": "ai_accountant_monitor_slack_enabled",
        "value": "true",
        "value_type": "bool",
        "description": "Queue Slack alerts for AI Accountant monitor findings.",
    },
    {
        "key": "notification_route_ai_accountant_monitor",
        "value": "slack",
        "value_type": "str",
        "description": "Route scheduled AI Accountant monitor alerts to Slack by default.",
    },
    {
        "key": "ai_accountant_monitor_record_empty",
        "value": "false",
        "value_type": "bool",
        "description": "Avoid recording empty AI Accountant monitor runs by default.",
    },
    {
        "key": "ai_accountant_monitor_llm_review_enabled",
        "value": "true",
        "value_type": "bool",
        "description": "Run scheduled read-only LLM reviews for AI Accountant monitor findings.",
    },
    {
        "key": "ai_accountant_monitor_review_max_rows",
        "value": "25",
        "value_type": "int",
        "description": "Maximum monitor rows sent to scheduled AI Accountant LLM reviews.",
    },
    {
        "key": "ai_accountant_monitor_review_max_exception_rows",
        "value": "25",
        "value_type": "int",
        "description": "Maximum accounting exception rows sent to scheduled AI Accountant LLM reviews.",
    },
    {
        "key": "ai_accountant_chat_ai_enabled",
        "value": "true",
        "value_type": "bool",
        "description": "Enable the AI Accountant identity for accounting and tax chat answers.",
    },
    {
        "key": "ai_accountant_web_research_enabled",
        "value": "true",
        "value_type": "bool",
        "description": "Enable external web-research context for AI Accountant tax/accounting questions.",
    },
    {
        "key": "ai_accountant_web_research_limit",
        "value": "5",
        "value_type": "int",
        "description": "Maximum external web-search rows attached to AI Accountant research context.",
    },
    {
        "key": "ai_accountant_web_research_timeout_seconds",
        "value": "10",
        "value_type": "int",
        "description": "HTTP timeout for AI Accountant external web research.",
    },
)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _is_valid_timezone_name(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        ZoneInfo(raw)
    except Exception:
        return False
    return True


def _monitor_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("severity") or ""),
            str(row.get("exception_type") or row.get("task_type") or ""),
            str(row.get("entity_type") or ""),
            str(row.get("entity_id") or ""),
            str(row.get("reference") or ""),
        ]
    )


def build_ai_accountant_monitor_rows(
    exception_rows: list[dict[str, Any]],
    *,
    dashboard_metrics: dict[str, Any] | None = None,
    max_rows: int = 200,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in exception_rows or []:
        severity = str(row.get("severity") or "P2").strip().upper() or "P2"
        exception_type = str(row.get("exception_type") or "accounting_review").strip()
        entity_type = str(row.get("entity_type") or "").strip()
        entity_id = _safe_int(row.get("entity_id"))
        rows.append(
            {
                "severity": severity,
                "task_type": exception_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "sku": str(row.get("sku") or ""),
                "reference": str(row.get("reference") or ""),
                "amount": row.get("amount"),
                "details": str(row.get("details") or "").strip(),
                "occurred_at": str(row.get("occurred_at") or ""),
                "recommended_action": _recommended_accounting_action(exception_type),
                "source": "accounting_exception_queue",
            }
        )

    metrics = dashboard_metrics or {}
    profit_basis = str(metrics.get("sales_30d_profit_basis_status") or "").strip().lower()
    review_count = _safe_int(metrics.get("sales_30d_cogs_review_count"))
    bundle_sale_count = _safe_int(metrics.get("sales_30d_bundle_sale_count"))
    bundle_inventory_units_sold = _safe_int(metrics.get("sales_30d_bundle_inventory_units_sold"))
    returns_count = _safe_int(metrics.get("returns_30d_count"))
    returns_refund_total = _safe_float(metrics.get("returns_30d_refund_total"))
    returns_cogs_reversal = _safe_float(metrics.get("returns_30d_cogs_reversal"))
    returns_profit_impact = _safe_float(metrics.get("returns_30d_profit_impact"))
    profit_before_returns = _safe_float(metrics.get("sales_30d_profit_before_returns"))
    estimated_profit_after_returns = _safe_float(metrics.get("sales_30d_est_profit"))
    if profit_basis in {"review_needed", "partial_lot_estimate"} or review_count > 0:
        bundle_note = (
            f" Bundle accounting detected {bundle_sale_count} sale(s) consuming "
            f"{bundle_inventory_units_sold} inventory unit(s)."
            if bundle_sale_count > 0
            else ""
        )
        rows.append(
            {
                "severity": "P1" if profit_basis == "review_needed" else "P2",
                "task_type": "dashboard_profit_basis_review",
                "entity_type": "dashboard",
                "entity_id": 0,
                "sku": "",
                "reference": "30d_profit_after_returns",
                "amount": estimated_profit_after_returns,
                "details": (
                    f"Dashboard 30-day profit basis status is `{profit_basis or 'unknown'}`; "
                    f"{review_count} sale(s) need COGS basis review. "
                    f"Profit before returns ${profit_before_returns:,.2f}; "
                    f"estimated profit after returns ${estimated_profit_after_returns:,.2f}."
                    f"{bundle_note}"
                ),
                "occurred_at": "",
                "recommended_action": (
                    "Review sold COGS source mix. For partial or mixed lots, set expected lot quantity, "
                    "allocation weights, or assignment-level costs before trusting profit. "
                    "For bundle listings, confirm component quantities and FIFO component COGS evidence."
                ),
                "source": "dashboard_live_metrics",
            }
        )
    if returns_count > 0 and (
        abs(returns_refund_total) > 0.005
        or abs(returns_cogs_reversal) > 0.005
        or abs(returns_profit_impact) > 0.005
    ):
        rows.append(
            {
                "severity": "P2",
                "task_type": "dashboard_return_profit_impact_review",
                "entity_type": "dashboard",
                "entity_id": 0,
                "sku": "",
                "reference": "30d_return_profit_impact",
                "amount": returns_profit_impact,
                "details": (
                    f"Dashboard return impact includes {returns_count} return(s), "
                    f"refund total ${returns_refund_total:,.2f}, "
                    f"COGS reversal ${returns_cogs_reversal:,.2f}, "
                    f"profit impact ${returns_profit_impact:,.2f}."
                ),
                "occurred_at": "",
                "recommended_action": _recommended_accounting_action("dashboard_return_profit_impact_review"),
                "source": "dashboard_live_metrics",
            }
        )

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        deduped.setdefault(_monitor_key(row), row)
    return sorted(
        deduped.values(),
        key=lambda row: (
            ACCOUNTING_SEVERITY_ORDER.get(str(row.get("severity") or ""), 99),
            str(row.get("task_type") or ""),
            str(row.get("occurred_at") or ""),
            _safe_int(row.get("entity_id")),
        ),
    )[: max(1, int(max_rows or 200))]


def _recommended_accounting_action(exception_type: str) -> str:
    key = str(exception_type or "").strip().lower()
    if key in {"missing_cost_basis", "blank_lot_assignment_without_lot_total"}:
        return "Add product landed cost, lot landed total, or product-lot assignment cost evidence."
    if key in {"lot_equal_fallback_review_needed", "lot_allocation_pending_check_in"}:
        return "Set expected lot quantity, allocation weights, or explicit assignment costs for the lot."
    if key in {"missing_shipping_label_spend", "unmatched_shipping_label_finance_entry"}:
        return "Import/link shipping-label finance entries to the matching order or sale."
    if key in {"missing_fee_evidence", "fee_source_fallback"}:
        return "Import/link marketplace fee evidence from normalized order finance entries."
    if key == "nonpositive_margin":
        return "Review sale price, fees, label spend, returns, and FIFO COGS basis before close sign-off."
    if key == "dashboard_return_profit_impact_review":
        return "Review refund totals, returned listing/inventory units, restock status, and COGS reversal evidence."
    if key == "missing_product_link":
        return "Link the sale to the correct product so FIFO COGS can be proven."
    if key == "active_bundle_listing_stock_shortage":
        return "Reduce/end the active bundle listing quantity or restock the short bundle component inventory."
    if key == "active_bundle_component_overcommitted":
        return "Reduce/end overlapping active bundle listings or restock the overcommitted component inventory."
    if key in {"lot_overallocated", "lot_underallocated"}:
        return "Reconcile lot landed total against explicit assignment landed totals."
    return "Review source evidence and resolve before accounting close sign-off."


def build_ai_accountant_message(
    rows: list[dict[str, Any]],
    *,
    period_label: str,
    max_items: int = 6,
) -> str:
    p0 = sum(1 for row in rows if str(row.get("severity") or "").upper() == "P0")
    p1 = sum(1 for row in rows if str(row.get("severity") or "").upper() == "P1")
    p2 = sum(1 for row in rows if str(row.get("severity") or "").upper() == "P2")
    lines = [
        f"AI Accountant monitor for {period_label}: {len(rows)} item(s) need review.",
        f"Severity mix: P0={p0}, P1={p1}, P2={p2}.",
    ]
    for row in rows[: max(1, int(max_items or 6))]:
        lines.append(
            "- "
            f"[{str(row.get('severity') or 'P2')}] {str(row.get('task_type') or 'accounting_review')} "
            f"{str(row.get('entity_type') or '')}#{str(row.get('entity_id') or '')}: "
            f"{str(row.get('recommended_action') or row.get('details') or '').strip()}"
        )
    if len(rows) > max_items:
        lines.append(f"- Plus {len(rows) - max_items} more item(s) in the AI Accountant workspace.")
    return "\n".join(lines)


def _audit_row_to_message(row: AuditLog) -> dict[str, Any]:
    changes = row.changes if hasattr(row, "changes") else {}
    automated_review = changes.get("automated_review") if isinstance(changes.get("automated_review"), dict) else {}
    return {
        "created_at": getattr(row, "created_at", None),
        "actor": getattr(row, "actor", ""),
        "message": str(changes.get("message") or ""),
        "period": str(changes.get("period") or ""),
        "item_count": _safe_int(changes.get("item_count")),
        "min_severity": str(changes.get("min_severity") or "").strip(),
        "requested_min_severity": str(changes.get("requested_min_severity") or "").strip(),
        "min_severity_fallback_applied": bool(changes.get("min_severity_fallback_applied")),
        "slack_outbox_id": changes.get("slack_outbox_id"),
        "review_enabled": bool(automated_review.get("enabled")),
        "review_status": (
            "unavailable"
            if str(automated_review.get("error") or "").strip()
            else ("completed" if str(automated_review.get("answer_hash_sha256") or "").strip() else "not_run")
        ),
        "review_compact_retry": bool(automated_review.get("compact_retry")),
        "review_monitor_rows": _safe_int(automated_review.get("monitor_rows")),
        "review_exception_rows": _safe_int(automated_review.get("exception_rows")),
        "review_fifo_evidence_rows": _safe_int(automated_review.get("sale_fifo_cogs_evidence_rows")),
        "review_rows_omitted": _safe_int(automated_review.get("monitor_rows_omitted"))
        + _safe_int(automated_review.get("exception_rows_omitted"))
        + _safe_int(automated_review.get("sale_fifo_cogs_evidence_rows_omitted")),
        "review_hash": str(automated_review.get("answer_hash_sha256") or "").strip()[:12],
        "review_prompt_hash": str(automated_review.get("prompt_hash_sha256") or "").strip()[:12],
        "review_data_scope_hash": str(automated_review.get("data_scope_hash_sha256") or "").strip()[:12],
        "review_error": str(automated_review.get("error") or "").strip()[:220],
        "review_runtime_route": str(automated_review.get("runtime_chain_brief") or "").strip()[:220],
    }


def summarize_ai_accountant_message_thresholds(messages: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(messages or [])
    fallback_rows = [row for row in rows if row.get("min_severity_fallback_applied")]
    if not fallback_rows:
        return {
            "fallback_count": 0,
            "latest_requested_min_severity": "",
            "latest_effective_min_severity": "",
            "warning": "",
        }
    latest = fallback_rows[0]
    requested = str(latest.get("requested_min_severity") or "").strip().upper()
    effective = str(latest.get("min_severity") or "").strip().upper()
    return {
        "fallback_count": len(fallback_rows),
        "latest_requested_min_severity": requested,
        "latest_effective_min_severity": effective,
        "warning": (
            "Recent AI Accountant monitor evidence used the severity fallback "
            f"({requested or 'blank'} -> {effective or 'P1'}). Review "
            "`ai_accountant_monitor_min_severity` in Admin runtime settings."
        ),
    }


def _audit_changes(row: Any) -> dict[str, Any]:
    if hasattr(row, "changes"):
        changes = getattr(row, "changes")
        if isinstance(changes, dict):
            return changes
    raw = str(getattr(row, "changes_json", "") or "{}")
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ai_accountant_review_outcome_row(row: Any) -> dict[str, Any] | None:
    payload = _audit_changes(row)
    after = payload.get("after", {}) if isinstance(payload, dict) else {}
    if not isinstance(after, dict):
        return None
    metadata = after.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    event_type = str(metadata.get("event_type") or "").strip().lower()
    review_type = str(metadata.get("review_type") or "").strip().lower()
    intent = str(after.get("intent") or "").strip().lower()
    if event_type != "ai_accountant_review_outcome" and review_type != "ai_accountant_review":
        if intent != "ai_accountant_review_outcome":
            return None
    data_scope = metadata.get("data_scope") if isinstance(metadata.get("data_scope"), dict) else {}
    row_counts = data_scope.get("row_counts") if isinstance(data_scope.get("row_counts"), dict) else {}
    return {
        "recorded_at": getattr(row, "created_at", None),
        "actor": str(getattr(row, "actor", "") or ""),
        "outcome": str(metadata.get("outcome") or "").strip().lower(),
        "surface": str(metadata.get("surface") or "").strip(),
        "monitor_rows": _safe_int(row_counts.get("monitor_rows")),
        "exception_rows": _safe_int(row_counts.get("accounting_exception_rows")),
        "fifo_evidence_rows": _safe_int(row_counts.get("sale_fifo_cogs_evidence_rows")),
        "evidence_packet_hash_sha256": str(metadata.get("evidence_packet_hash_sha256") or "").strip(),
        "evidence_packet_monitor_rows": _safe_int(
            (metadata.get("evidence_packet_row_counts") or {}).get("monitor_rows")
            if isinstance(metadata.get("evidence_packet_row_counts"), dict)
            else 0
        ),
        "evidence_packet_exception_rows": _safe_int(
            (metadata.get("evidence_packet_row_counts") or {}).get("accounting_exception_rows")
            if isinstance(metadata.get("evidence_packet_row_counts"), dict)
            else 0
        ),
        "evidence_packet_fifo_rows": _safe_int(
            (metadata.get("evidence_packet_row_counts") or {}).get("sale_fifo_cogs_evidence")
            if isinstance(metadata.get("evidence_packet_row_counts"), dict)
            else 0
        ),
        "evidence_packet_integrity_status": str(
            metadata.get("evidence_packet_integrity_status") or ""
        ).strip(),
        "evidence_packet_integrity_errors": "; ".join(
            str(item) for item in (metadata.get("evidence_packet_integrity_errors") or [])
        )[:500],
        "evidence_packet_integrity_error_count": _safe_int(
            metadata.get("evidence_packet_integrity_error_count")
        ),
        "evidence_packet_manifest_status": str(
            metadata.get("evidence_packet_manifest_status") or ""
        ).strip(),
        "evidence_packet_manifest_rows": _safe_int(metadata.get("evidence_packet_manifest_row_count")),
        "evidence_packet_manifest_expected_rows": _safe_int(
            metadata.get("evidence_packet_manifest_expected_row_count")
        ),
        "evidence_packet_action_summary_task_counts": json.dumps(
            metadata.get("evidence_packet_action_summary_task_counts") or {},
            sort_keys=True,
            default=str,
        )[:500],
        "prompt_hash_sha256": str(metadata.get("prompt_hash_sha256") or "").strip(),
        "data_scope_hash_sha256": str(metadata.get("data_scope_hash_sha256") or "").strip(),
        "answer_hash_sha256": str(metadata.get("answer_hash_sha256") or "").strip(),
        "answer_preview": str(after.get("answer_preview") or "")[:220],
    }


def list_ai_accountant_review_outcomes(repo: InventoryRepository, *, limit: int = 25) -> list[dict[str, Any]]:
    db = getattr(repo, "db", None)
    if db is None:
        return []
    try:
        rows = db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "ai_chat")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, int(limit or 25)))
        ).all()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        return []
    outcomes = [_ai_accountant_review_outcome_row(row) for row in rows]
    return [row for row in outcomes if row is not None]


def summarize_ai_accountant_review_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    latest = next((row for row in outcomes or [] if str(row.get("outcome") or "").strip()), None)
    if not latest:
        return {
            "latest_outcome": "none",
            "status": "No review outcome recorded",
            "needs_followup": False,
            "packet_needs_review": False,
            "recorded_at": "",
            "actor": "",
        }
    outcome = str(latest.get("outcome") or "").strip().lower()
    needs_followup = outcome in {"edited", "rejected"}
    integrity_status = str(latest.get("evidence_packet_integrity_status") or "").strip().lower()
    manifest_status = str(latest.get("evidence_packet_manifest_status") or "").strip().lower()
    integrity_error_count = _safe_int(latest.get("evidence_packet_integrity_error_count"))
    manifest_rows = _safe_int(latest.get("evidence_packet_manifest_rows"))
    expected_manifest_rows = _safe_int(latest.get("evidence_packet_manifest_expected_rows"))
    packet_needs_review = (
        integrity_status == "review_needed"
        or manifest_status == "review_needed"
        or integrity_error_count > 0
        or (expected_manifest_rows > 0 and manifest_rows != expected_manifest_rows)
    )
    if outcome == "accepted":
        status = "Latest AI Accountant review was accepted."
    elif outcome == "edited":
        status = "Latest AI Accountant review needs edits before close sign-off."
    elif outcome == "rejected":
        status = "Latest AI Accountant review was rejected and needs follow-up."
    else:
        status = f"Latest AI Accountant review outcome is `{outcome or 'unknown'}`."
    return {
        "latest_outcome": outcome or "unknown",
        "status": status,
        "needs_followup": needs_followup,
        "packet_needs_review": packet_needs_review,
        "packet_status": integrity_status or "unknown",
        "packet_manifest_status": manifest_status or "unknown",
        "packet_integrity_error_count": integrity_error_count,
        "recorded_at": latest.get("recorded_at") or "",
        "actor": str(latest.get("actor") or ""),
        "answer_hash_sha256": str(latest.get("answer_hash_sha256") or ""),
    }


def build_ai_accountant_action_summary(rows: list[dict[str, Any]], *, max_rows: int = 12) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        action = str(row.get("recommended_action") or row.get("details") or "").strip()
        if not action:
            action = "Review source evidence and resolve before accounting close sign-off."
        task_type = str(row.get("task_type") or row.get("exception_type") or "accounting_review").strip()
        key = f"{task_type}|{action}"
        bucket = grouped.setdefault(
            key,
            {
                "task_type": task_type,
                "recommended_action": action,
                "item_count": 0,
                "P0": 0,
                "P1": 0,
                "P2": 0,
                "sample_reference": "",
            },
        )
        severity = str(row.get("severity") or "P2").strip().upper() or "P2"
        bucket["item_count"] = _safe_int(bucket.get("item_count")) + 1
        if severity in {"P0", "P1", "P2"}:
            bucket[severity] = _safe_int(bucket.get(severity)) + 1
        if not bucket.get("sample_reference"):
            sample_parts = [
                str(row.get("entity_type") or "").strip(),
                str(row.get("entity_id") or "").strip(),
                str(row.get("reference") or "").strip(),
                str(row.get("sku") or "").strip(),
            ]
            bucket["sample_reference"] = " ".join(part for part in sample_parts if part).strip()

    return sorted(
        grouped.values(),
        key=lambda row: (
            -_safe_int(row.get("P0")),
            -_safe_int(row.get("P1")),
            -_safe_int(row.get("item_count")),
            str(row.get("task_type") or ""),
        ),
    )[: max(1, int(max_rows or 12))]


def build_ai_accountant_packet_review_action_rows(review_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    summary = dict(review_summary or {})
    if not summary.get("packet_needs_review"):
        return []
    return [
        {
            "severity": "P0",
            "task_type": "evidence_packet_integrity_review",
            "entity_type": "ai_accountant_evidence_packet",
            "entity_id": str(summary.get("answer_hash_sha256") or "")[:12],
            "reference": (
                f"integrity={summary.get('packet_status') or 'unknown'}; "
                f"manifest={summary.get('packet_manifest_status') or 'unknown'}; "
                f"errors={_safe_int(summary.get('packet_integrity_error_count'))}"
            ),
            "recommended_action": (
                "Regenerate or inspect the AI Accountant evidence packet before trusting this review outcome."
            ),
        }
    ]


def summarize_monitor_run_result(result: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(result or {})
    if not payload:
        return {
            "has_result": False,
            "status": "No monitor run recorded in this session.",
            "severity": "info",
            "details": "",
        }
    item_count = _safe_int(payload.get("item_count"))
    actionable_count = _safe_int(payload.get("actionable_count"))
    audit_id = _safe_int(payload.get("audit_id"))
    slack_outbox_id = _safe_int(payload.get("slack_outbox_id"))
    review_error = str(payload.get("review_error") or "").strip()
    review_hash = str(payload.get("review_hash") or "").strip()
    review_enabled = bool(payload.get("review_enabled"))
    details = (
        f"items={item_count} actionable={actionable_count} "
        f"audit={audit_id or 'none'} slack_outbox={slack_outbox_id or 'none'}"
    )
    if review_enabled and review_hash:
        details += f" review_hash={review_hash[:12]}"
    if review_error:
        return {
            "has_result": True,
            "status": f"Last monitor run completed, but automated review was unavailable: {review_error[:300]}",
            "severity": "warning",
            "details": details,
        }
    return {
        "has_result": True,
        "status": "Last monitor run completed.",
        "severity": "success",
        "details": details,
    }


def build_ai_accountant_runtime_summary(repo: InventoryRepository) -> list[dict[str, Any]]:
    schedule_mode = str(get_runtime_str(repo, "ai_accountant_monitor_schedule_mode", "interval") or "interval").strip()
    interval_hours = _safe_int(get_runtime_int(repo, "ai_accountant_monitor_interval_hours", 6))
    lookback_days = _safe_int(get_runtime_int(repo, "ai_accountant_monitor_lookback_days", 30))
    default_timezone = str(getattr(settings, "app_default_timezone", "America/Denver") or "America/Denver").strip()
    monitor_timezone = str(get_runtime_str(repo, "ai_accountant_monitor_timezone", default_timezone) or default_timezone).strip()
    local_time = str(get_runtime_str(repo, "ai_accountant_monitor_local_time", "08:30") or "08:30").strip()
    cadence = f"every {max(1, interval_hours)}h" if schedule_mode.lower() != "daily" else f"daily at {local_time}"
    web_limit_configured = _safe_int(get_runtime_int(repo, "ai_accountant_web_research_limit", 5))
    web_timeout_configured = _safe_int(get_runtime_int(repo, "ai_accountant_web_research_timeout_seconds", 10))
    web_limit_effective = max(1, web_limit_configured)
    web_timeout_effective = max(2, web_timeout_configured)
    monitor_min_severity = str(get_runtime_str(repo, "ai_accountant_monitor_min_severity", "P1") or "P1").strip()
    monitor_notification_route = str(
        get_runtime_str(repo, "notification_route_ai_accountant_monitor", "slack") or "slack"
    ).strip().lower()
    monitor_slack_channel = str(get_runtime_str(repo, "ai_accountant_monitor_channel", "") or "").strip()
    slack_notifications_enabled = get_runtime_bool(repo, "slack_notifications_enabled", True)
    slack_bot_token = str(get_runtime_str(repo, "slack_bot_token", "") or "").strip()
    slack_default_channel = str(get_runtime_str(repo, "slack_default_channel", "") or "").strip()
    return [
        {
            "setting": "Scheduled Monitor",
            "status": "enabled" if get_runtime_bool(repo, "ai_accountant_monitor_enabled", True) else "disabled",
            "value": cadence,
            "runtime_key": "ai_accountant_monitor_enabled",
            "schedule_mode": schedule_mode.lower() or "interval",
            "configured_interval_hours": interval_hours,
            "configured_lookback_days": lookback_days,
            "configured_timezone": monitor_timezone,
            "configured_local_time": local_time,
        },
        {
            "setting": "Slack Alerts",
            "status": "enabled" if get_runtime_bool(repo, "ai_accountant_monitor_slack_enabled", True) else "disabled",
            "value": monitor_slack_channel or "(default channel)",
            "runtime_key": "ai_accountant_monitor_slack_enabled",
            "configured_route": monitor_notification_route or "slack",
        },
        {
            "setting": "Slack Delivery",
            "status": "enabled" if slack_notifications_enabled else "disabled",
            "value": (
                f"token={'present' if slack_bot_token else 'missing'}; "
                f"default_channel={'present' if slack_default_channel else 'missing'}"
            ),
            "runtime_key": "slack_notifications_enabled",
            "configured_token_present": bool(slack_bot_token),
            "configured_default_channel_present": bool(slack_default_channel),
            "configured_monitor_channel_present": bool(monitor_slack_channel),
        },
        {
            "setting": "Automated LLM Review",
            "status": "enabled"
            if get_runtime_bool(repo, "ai_accountant_monitor_llm_review_enabled", True)
            else "disabled",
            "value": monitor_min_severity,
            "runtime_key": "ai_accountant_monitor_llm_review_enabled",
            "configured_min_severity": monitor_min_severity,
        },
        {
            "setting": "Interactive Chat",
            "status": "enabled" if get_runtime_bool(repo, "ai_accountant_chat_ai_enabled", True) else "disabled",
            "value": "AI Accountant identity prompt",
            "runtime_key": "ai_accountant_chat_ai_enabled",
        },
        {
            "setting": "External Web Research",
            "status": "enabled" if get_runtime_bool(repo, "ai_accountant_web_research_enabled", True) else "disabled",
            "value": f"limit {web_limit_effective}; timeout {web_timeout_effective}s",
            "runtime_key": "ai_accountant_web_research_enabled",
            "configured_limit": web_limit_configured,
            "configured_timeout_seconds": web_timeout_configured,
        },
    ]


def build_ai_accountant_runtime_chain_rows(repo: InventoryRepository) -> list[dict[str, Any]]:
    return describe_llm_runtime_chain(repo, workflow="accounting")


def run_ai_accountant_runtime_smoke_test(repo: InventoryRepository) -> dict[str, Any]:
    try:
        result = execute_comp_summary(
            repo,
            query="AI Accountant runtime smoke test. Reply with short JSON confirming runtime availability.",
            ebay_rows=[],
            web_rows=[],
            spot_context={
                "workflow": "accounting",
                "scope": "runtime_smoke_test",
                "instructions": "Do not analyze business data; only confirm the LLM route can respond.",
            },
            system_message=(
                "You are GoldenStackers' read-only AI Accountant runtime health check. "
                "Return a concise JSON object only."
            ),
            instruction='Return ONLY JSON: {"status":"ok","note":"runtime available"}.',
            workflow="accounting",
        )
        citation = dict(result.citation or {})
        fallback_errors = list(citation.get("fallback_errors") or [])
        return {
            "status": "ok",
            "provider": str(citation.get("provider") or "").strip(),
            "model": str(citation.get("text_model") or "").strip(),
            "endpoint_type": str(citation.get("endpoint_type") or "").strip(),
            "source": str(citation.get("source") or "").strip(),
            "fallback_attempts": int(citation.get("fallback_attempts") or 0),
            "fallback_errors": " | ".join(str(err) for err in fallback_errors)[:700],
            "text_preview": str(result.text or "").strip()[:500],
        }
    except Exception as exc:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
        return {
            "status": "failed",
            "provider": "",
            "model": "",
            "endpoint_type": "",
            "source": "",
            "fallback_attempts": 0,
            "fallback_errors": "",
            "text_preview": "",
            "error": str(exc)[:1000],
        }


def build_ai_accountant_setup_checks(runtime_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(runtime_rows or [])
    by_setting = {str(row.get("setting") or ""): row for row in rows}

    def _status(setting: str) -> str:
        return str((by_setting.get(setting) or {}).get("status") or "").strip().lower()

    scheduled_row = by_setting.get("Scheduled Monitor") or {}
    scheduled_status = _status("Scheduled Monitor")
    schedule_mode = str(scheduled_row.get("schedule_mode") or "").strip().lower()
    configured_timezone = str(scheduled_row.get("configured_timezone") or "").strip()
    configured_local_time = str(scheduled_row.get("configured_local_time") or "").strip()
    has_interval = "configured_interval_hours" in scheduled_row
    configured_interval_hours = _safe_int(scheduled_row.get("configured_interval_hours"))
    has_lookback = "configured_lookback_days" in scheduled_row
    configured_lookback_days = _safe_int(scheduled_row.get("configured_lookback_days"))
    scheduled_monitor_status = "pass"
    scheduled_monitor_details = "Enable `ai_accountant_monitor_enabled` so the accountant watches the business automatically."
    if scheduled_status != "enabled":
        scheduled_monitor_status = "warn"
    elif schedule_mode not in {"", "interval", "daily"}:
        scheduled_monitor_status = "warn"
        scheduled_monitor_details = "Set `ai_accountant_monitor_schedule_mode` to `interval` or `daily`."
    elif schedule_mode in {"", "interval"} and has_interval and configured_interval_hours < 1:
        scheduled_monitor_status = "warn"
        scheduled_monitor_details = "Set `ai_accountant_monitor_interval_hours` to at least 1."
    elif has_lookback and configured_lookback_days < 1:
        scheduled_monitor_status = "warn"
        scheduled_monitor_details = "Set `ai_accountant_monitor_lookback_days` to at least 1."
    elif configured_timezone and not _is_valid_timezone_name(configured_timezone):
        scheduled_monitor_status = "warn"
        scheduled_monitor_details = "Set `ai_accountant_monitor_timezone` to a valid IANA timezone."
    elif schedule_mode == "daily" and configured_local_time and not _HHMM_RE.match(configured_local_time):
        scheduled_monitor_status = "warn"
        scheduled_monitor_details = "Set `ai_accountant_monitor_local_time` to `HH:MM` in 24-hour time."

    review_row = by_setting.get("Automated LLM Review") or {}
    review_status = _status("Automated LLM Review")
    review_min_severity = str(
        review_row.get("configured_min_severity")
        or review_row.get("value")
        or ""
    ).strip().upper()
    automated_review_status = "pass"
    automated_review_details = "Enable `ai_accountant_monitor_llm_review_enabled` for scheduled expert review notes."
    if review_status != "enabled":
        automated_review_status = "warn"
    elif review_min_severity and review_min_severity not in {"P0", "P1", "P2"}:
        automated_review_status = "warn"
        automated_review_details = "Set `ai_accountant_monitor_min_severity` to `P0`, `P1`, or `P2`."

    web_row = by_setting.get("External Web Research") or {}
    web_status = _status("External Web Research")
    has_web_limit = "configured_limit" in web_row
    has_web_timeout = "configured_timeout_seconds" in web_row
    web_limit = _safe_int(web_row.get("configured_limit"))
    web_timeout = _safe_int(web_row.get("configured_timeout_seconds"))
    web_research_status = "pass"
    web_research_details = "`ai_accountant_web_research_enabled` lets the accountant fetch external context for tax/accounting questions."
    if web_status != "enabled":
        web_research_status = "warn"
    elif has_web_limit and web_limit < 1:
        web_research_status = "warn"
        web_research_details = "Set `ai_accountant_web_research_limit` to at least 1."
    elif has_web_timeout and web_timeout < 2:
        web_research_status = "warn"
        web_research_details = "Set `ai_accountant_web_research_timeout_seconds` to at least 2."

    slack_row = by_setting.get("Slack Alerts") or {}
    slack_status = _status("Slack Alerts")
    slack_route = str(slack_row.get("configured_route") or "slack").strip().lower()
    slack_allows_outbox = slack_route in {"", "slack", "both", "all"}
    slack_alert_status = "pass"
    slack_alert_details = "Slack notification route is enabled for scheduled AI Accountant monitor alerts."
    if slack_status != "enabled":
        slack_alert_status = "warn"
        slack_alert_details = "Enable `ai_accountant_monitor_slack_enabled` for out-of-app follow-up alerts."
    elif not slack_allows_outbox:
        slack_alert_status = "warn"
        slack_alert_details = "Set `notification_route_ai_accountant_monitor` to `slack`, `both`, or `all`."

    delivery_row = by_setting.get("Slack Delivery") or {}
    slack_delivery_status = "pass"
    slack_delivery_details = "Slack delivery is configured with a bot token and a target channel."
    if delivery_row:
        if str(delivery_row.get("status") or "").strip().lower() != "enabled":
            slack_delivery_status = "warn"
            slack_delivery_details = "Enable `slack_notifications_enabled` so queued AI Accountant alerts can dispatch."
        elif not bool(delivery_row.get("configured_token_present")):
            slack_delivery_status = "warn"
            slack_delivery_details = "Set `slack_bot_token`; AI Accountant Slack alerts will retry until a bot token exists."
        elif not (
            bool(delivery_row.get("configured_default_channel_present"))
            or bool(delivery_row.get("configured_monitor_channel_present"))
        ):
            slack_delivery_status = "warn"
            slack_delivery_details = "Set `slack_default_channel` or `ai_accountant_monitor_channel` for AI Accountant alerts."

    checks = [
        {
            "check": "scheduled_monitor_enabled",
            "status": scheduled_monitor_status,
            "details": scheduled_monitor_details,
        },
        {
            "check": "slack_alerts_enabled",
            "status": slack_alert_status,
            "details": slack_alert_details,
        },
        {
            "check": "slack_delivery_configured",
            "status": slack_delivery_status,
            "details": slack_delivery_details,
        },
        {
            "check": "automated_llm_review_enabled",
            "status": automated_review_status,
            "details": automated_review_details,
        },
        {
            "check": "interactive_chat_enabled",
            "status": "pass" if _status("Interactive Chat") == "enabled" else "warn",
            "details": "Enable `ai_accountant_chat_ai_enabled` for accountant identity in Ask/Slack answers.",
        },
        {
            "check": "external_web_research_review",
            "status": web_research_status,
            "details": web_research_details,
        },
    ]
    return checks


def build_ai_accountant_outbox_delivery_rows(
    repo: InventoryRepository,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    list_outbox = getattr(repo, "list_notification_outbox", None)
    if not callable(list_outbox):
        return []
    rows = list_outbox(
        environment=settings.app_env,
        channel="slack",
        limit=max(1, min(100, int(limit) * 4)),
    )
    delivery_rows: list[dict[str, Any]] = []
    for row in rows:
        event_type = str(getattr(row, "event_type", "") or "").strip()
        if event_type != "ai_accountant_monitor":
            continue
        payload_raw = str(getattr(row, "payload_json", "") or "{}")
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        text_preview = str(payload.get("text") or "").strip().replace("\n", " ")
        delivery_rows.append(
            {
                "id": int(getattr(row, "id", 0) or 0),
                "status": str(getattr(row, "status", "") or "").strip(),
                "attempt_count": int(getattr(row, "attempt_count", 0) or 0),
                "max_attempts": int(getattr(row, "max_attempts", 0) or 0),
                "next_attempt_at": getattr(row, "next_attempt_at", None),
                "last_attempt_at": getattr(row, "last_attempt_at", None),
                "dispatched_at": getattr(row, "dispatched_at", None),
                "target_channel": str(payload.get("channel") or "").strip() or "(default)",
                "last_error": str(getattr(row, "last_error", "") or "").strip()[:500],
                "text_preview": text_preview[:180],
            }
        )
        if len(delivery_rows) >= max(1, int(limit)):
            break
    return delivery_rows


def summarize_ai_accountant_outbox_delivery_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total": 0,
        "queued": 0,
        "retrying": 0,
        "failed": 0,
        "sent": 0,
        "due": 0,
        "blocked": 0,
        "latest_error": "",
    }
    now = utcnow_naive()
    for row in rows or []:
        summary["total"] += 1
        status = str(row.get("status") or "").strip().lower()
        if status in {"queued", "retrying", "failed", "sent"}:
            summary[status] += 1
        if status in {"retrying", "failed"}:
            summary["blocked"] += 1
        next_attempt_at = row.get("next_attempt_at")
        if status in {"queued", "retrying"} and (
            next_attempt_at is None or (_coerce_naive_datetime(next_attempt_at) or now) <= now
        ):
            summary["due"] += 1
        last_error = str(row.get("last_error") or "").strip()
        if last_error and not summary["latest_error"]:
            summary["latest_error"] = last_error[:300]
    return summary


def _coerce_naive_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def process_due_ai_accountant_outbox_rows(
    repo: InventoryRepository,
    *,
    actor: str,
    limit: int = 10,
) -> dict[str, Any]:
    list_outbox = getattr(repo, "list_notification_outbox", None)
    if not callable(list_outbox):
        return {"attempted": 0, "sent": 0, "failed": 0, "skipped": 0, "messages": ["Notification outbox is unavailable."]}
    rows = list_outbox(
        environment=settings.app_env,
        channel="slack",
        statuses={"queued", "retrying"},
        due_before=utcnow_naive(),
        limit=max(1, min(100, int(limit) * 4)),
    )
    attempted = 0
    sent = 0
    failed = 0
    skipped = 0
    messages: list[str] = []
    for row in rows:
        if str(getattr(row, "event_type", "") or "").strip() != "ai_accountant_monitor":
            skipped += 1
            continue
        attempted += 1
        ok, message = process_notification_outbox_row(
            repo,
            outbox_id=int(getattr(row, "id", 0) or 0),
            actor=(actor or "system").strip() or "system",
        )
        if ok:
            sent += 1
        else:
            failed += 1
        if message:
            messages.append(str(message)[:300])
        if attempted >= max(1, int(limit)):
            break
    return {
        "attempted": attempted,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "messages": messages[:5],
    }


def apply_ai_accountant_recommended_runtime_settings(
    repo: InventoryRepository,
    *,
    actor: str,
) -> list[dict[str, str]]:
    applied: list[dict[str, str]] = []
    for setting in AI_ACCOUNTANT_RECOMMENDED_RUNTIME_SETTINGS:
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key=setting["key"],
            value=setting["value"],
            value_type=setting["value_type"],
            description=setting["description"],
            is_active=True,
            actor=actor,
        )
        applied.append(dict(setting))
    return applied


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    return pd.DataFrame(rows or []).to_csv(index=False).encode("utf-8")


def _ai_accountant_period_slug(period_label: str) -> str:
    return (
        str(period_label or "ai-accountant")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace(":", "")
        .replace("/", "-")
    )


def build_ai_accountant_answer_followup_status_counts(
    answer_rows: list[dict[str, Any]] | None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in answer_rows or []:
        status = str(row.get("followup_status") or "unreviewed").strip().lower() or "unreviewed"
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def build_ai_accountant_evidence_summary(
    *,
    period_label: str,
    action_summary: list[dict[str, Any]],
    monitor_rows: list[dict[str, Any]],
    exception_rows: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]],
    review_outcomes: list[dict[str, Any]],
    answer_rows: list[dict[str, Any]] | None = None,
    answer_followup_rows: list[dict[str, Any]] | None = None,
    review_hash_index: list[dict[str, Any]] | None = None,
    dashboard_metrics: dict[str, Any] | None = None,
    sale_fifo_cogs_evidence_rows: list[dict[str, Any]] | None = None,
    artifact_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    hashes = dict(artifact_hashes or {})
    artifact_names = sorted(hashes)
    action_summary_task_counts: dict[str, int] = {}
    for row in action_summary or []:
        task_type = str(row.get("task_type") or "unknown").strip() or "unknown"
        action_summary_task_counts[task_type] = action_summary_task_counts.get(task_type, 0) + 1
    return {
        "packet_schema_version": "ai_accountant_evidence_packet_v1",
        "period_label": str(period_label or "").strip(),
        "row_counts": {
            "action_summary": int(len(action_summary or [])),
            "monitor_rows": int(len(monitor_rows or [])),
            "accounting_exception_rows": int(len(exception_rows or [])),
            "messages": int(len(messages or [])),
            "review_outcomes": int(len(review_outcomes or [])),
            "answers": int(len(answer_rows or [])),
            "answer_followups": int(len(answer_followup_rows or [])),
            "review_hash_index": int(len(review_hash_index or [])),
            "sale_fifo_cogs_evidence": int(len(sale_fifo_cogs_evidence_rows or [])),
        },
        "action_summary_task_counts": dict(sorted(action_summary_task_counts.items())),
        "answer_followup_status_counts": build_ai_accountant_answer_followup_status_counts(answer_rows),
        "dashboard_profit_basis_status": (dashboard_metrics or {}).get("sales_30d_profit_basis_status"),
        "artifact_count": int(len(artifact_names)),
        "artifact_names": artifact_names,
        "artifact_hashes_sha256": hashes,
        "evidence_hash_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def build_ai_accountant_review_hash_index(
    *,
    messages: list[dict[str, Any]],
    review_outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in messages or []:
        rows.append(
            {
                "source": "ai_accountant_messages",
                "recorded_at": row.get("created_at"),
                "actor": row.get("actor"),
                "status_or_outcome": row.get("review_status"),
                "answer_hash_sha256": row.get("review_hash"),
                "prompt_hash_sha256": row.get("review_prompt_hash"),
                "data_scope_hash_sha256": row.get("review_data_scope_hash"),
                "evidence_packet_hash_sha256": row.get("evidence_packet_hash_sha256"),
                "monitor_rows": row.get("review_monitor_rows"),
                "exception_rows": row.get("review_exception_rows"),
                "fifo_evidence_rows": row.get("review_fifo_evidence_rows"),
                "evidence_packet_integrity_status": row.get("evidence_packet_integrity_status"),
                "evidence_packet_integrity_error_count": row.get(
                    "evidence_packet_integrity_error_count"
                ),
                "evidence_packet_manifest_status": row.get("evidence_packet_manifest_status"),
                "evidence_packet_manifest_rows": row.get("evidence_packet_manifest_rows"),
                "evidence_packet_manifest_expected_rows": row.get(
                    "evidence_packet_manifest_expected_rows"
                ),
                "evidence_packet_action_summary_task_counts": row.get(
                    "evidence_packet_action_summary_task_counts"
                ),
            }
        )
    for row in review_outcomes or []:
        rows.append(
            {
                "source": "ai_accountant_review_outcomes",
                "recorded_at": row.get("recorded_at"),
                "actor": row.get("actor"),
                "status_or_outcome": row.get("outcome"),
                "answer_hash_sha256": row.get("answer_hash_sha256"),
                "prompt_hash_sha256": row.get("prompt_hash_sha256"),
                "data_scope_hash_sha256": row.get("data_scope_hash_sha256"),
                "evidence_packet_hash_sha256": row.get("evidence_packet_hash_sha256"),
                "monitor_rows": row.get("monitor_rows"),
                "exception_rows": row.get("exception_rows"),
                "fifo_evidence_rows": row.get("fifo_evidence_rows"),
                "evidence_packet_integrity_status": row.get("evidence_packet_integrity_status"),
                "evidence_packet_integrity_error_count": row.get(
                    "evidence_packet_integrity_error_count"
                ),
                "evidence_packet_manifest_status": row.get("evidence_packet_manifest_status"),
                "evidence_packet_manifest_rows": row.get("evidence_packet_manifest_rows"),
                "evidence_packet_manifest_expected_rows": row.get(
                    "evidence_packet_manifest_expected_rows"
                ),
                "evidence_packet_action_summary_task_counts": row.get(
                    "evidence_packet_action_summary_task_counts"
                ),
            }
        )
    return rows


def build_ai_accountant_evidence_zip(
    *,
    period_label: str,
    action_summary: list[dict[str, Any]],
    monitor_rows: list[dict[str, Any]],
    exception_rows: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]],
    review_outcomes: list[dict[str, Any]],
    answer_rows: list[dict[str, Any]] | None = None,
    answer_followup_rows: list[dict[str, Any]] | None = None,
    dashboard_metrics: dict[str, Any] | None = None,
    sale_fifo_cogs_evidence_rows: list[dict[str, Any]] | None = None,
) -> bytes:
    period_slug = _ai_accountant_period_slug(period_label)
    review_hash_index = build_ai_accountant_review_hash_index(
        messages=messages,
        review_outcomes=review_outcomes,
    )
    files: dict[str, bytes] = {
        f"{period_slug}/ai_accountant_action_summary.csv": _csv_bytes(action_summary),
        f"{period_slug}/ai_accountant_monitor_rows.csv": _csv_bytes(monitor_rows),
        f"{period_slug}/accounting_exception_queue.csv": _csv_bytes(exception_rows or []),
        f"{period_slug}/ai_accountant_messages.csv": _csv_bytes(messages),
        f"{period_slug}/ai_accountant_review_outcomes.csv": _csv_bytes(review_outcomes),
        f"{period_slug}/ai_accountant_answers.csv": _csv_bytes(answer_rows or []),
        f"{period_slug}/ai_accountant_answer_followups.csv": _csv_bytes(answer_followup_rows or []),
        f"{period_slug}/ai_accountant_review_hash_index.csv": _csv_bytes(review_hash_index),
        f"{period_slug}/sale_fifo_cogs_evidence.csv": _csv_bytes(sale_fifo_cogs_evidence_rows or []),
        f"{period_slug}/dashboard_profit_basis.json": json.dumps(
            dashboard_metrics or {},
            sort_keys=True,
            default=str,
            indent=2,
        ).encode("utf-8"),
    }
    artifact_hashes = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    evidence_summary = build_ai_accountant_evidence_summary(
        period_label=period_label,
        action_summary=action_summary,
        monitor_rows=monitor_rows,
        exception_rows=exception_rows,
        messages=messages,
        review_outcomes=review_outcomes,
        answer_rows=answer_rows,
        answer_followup_rows=answer_followup_rows,
        review_hash_index=review_hash_index,
        dashboard_metrics=dashboard_metrics,
        sale_fifo_cogs_evidence_rows=sale_fifo_cogs_evidence_rows,
        artifact_hashes=artifact_hashes,
    )
    files[f"{period_slug}/evidence_summary.json"] = json.dumps(
        evidence_summary,
        sort_keys=True,
        default=str,
        indent=2,
    ).encode("utf-8")
    manifest_rows = [
        {
            "artifact": name,
            "byte_count": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(files.items())
    ]
    files[f"{period_slug}/manifest.csv"] = _csv_bytes(manifest_rows)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    return buffer.getvalue()


def read_ai_accountant_evidence_zip_summary(packet: bytes, *, period_label: str) -> dict[str, Any]:
    if not packet:
        return {}
    period_slug = _ai_accountant_period_slug(period_label)
    summary_name = f"{period_slug}/evidence_summary.json"
    manifest_name = f"{period_slug}/manifest.csv"
    try:
        with zipfile.ZipFile(BytesIO(packet), "r") as archive:
            payload = json.loads(archive.read(summary_name))
            if not isinstance(payload, dict):
                return {}
            zip_names = set(archive.namelist())
            manifest_entries: dict[str, dict[str, str]] = {}
            if manifest_name in zip_names:
                manifest_text = archive.read(manifest_name).decode("utf-8")
                for row in csv.DictReader(StringIO(manifest_text)):
                    artifact_name = str(row.get("artifact") or "").strip()
                    if artifact_name:
                        manifest_entries[artifact_name] = {
                            "byte_count": str(row.get("byte_count") or "").strip(),
                            "sha256": str(row.get("sha256") or "").strip(),
                        }
            declared_hashes_raw = payload.get("artifact_hashes_sha256")
            declared_hashes = declared_hashes_raw if isinstance(declared_hashes_raw, dict) else {}
            declared_hashes = {str(k): str(v) for k, v in declared_hashes.items()}
            declared_names = sorted(str(name) for name in (payload.get("artifact_names") or []))
            expected_names = sorted(declared_hashes)
            errors: list[str] = []
            if not manifest_entries:
                errors.append("missing_manifest")
            else:
                expected_manifest_names = sorted(name for name in zip_names if name != manifest_name)
                manifest_names = sorted(manifest_entries)
                if manifest_names != expected_manifest_names:
                    errors.append("manifest_artifacts_mismatch")
                for name in expected_manifest_names:
                    content = archive.read(name)
                    manifest_row = manifest_entries.get(name) or {}
                    if str(len(content)) != str(manifest_row.get("byte_count") or ""):
                        errors.append(f"manifest_byte_count_mismatch:{name}")
                    if hashlib.sha256(content).hexdigest() != str(manifest_row.get("sha256") or ""):
                        errors.append(f"manifest_hash_mismatch:{name}")
            if declared_names and declared_names != expected_names:
                errors.append("artifact_names_mismatch")
            if _safe_int(payload.get("artifact_count")) != len(expected_names):
                errors.append("artifact_count_mismatch")
            actual_artifact_hashes: dict[str, str] = {}
            for name, expected_hash in declared_hashes.items():
                if name not in zip_names:
                    errors.append(f"missing_artifact:{name}")
                    continue
                actual_hash = hashlib.sha256(archive.read(name)).hexdigest()
                actual_artifact_hashes[name] = actual_hash
                if actual_hash != expected_hash:
                    errors.append(f"artifact_hash_mismatch:{name}")
            extra_artifacts = sorted(
                name
                for name in zip_names
                if name not in declared_hashes
                and name not in {summary_name, manifest_name}
            )
            if extra_artifacts:
                errors.append("undeclared_artifacts:" + ",".join(extra_artifacts))
            expected_evidence_hash = hashlib.sha256(
                json.dumps(declared_hashes, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if str(payload.get("evidence_hash_sha256") or "") != expected_evidence_hash:
                errors.append("evidence_hash_mismatch")
            payload["packet_integrity_status"] = "review_needed" if errors else "verified"
            payload["packet_integrity_errors"] = errors
            payload["packet_integrity_error_count"] = int(len(errors))
            payload["packet_verified_artifact_count"] = len(actual_artifact_hashes)
            payload["packet_zip_artifact_count"] = len(
                [name for name in zip_names if name not in {summary_name, manifest_name}]
            )
            payload["packet_manifest_status"] = (
                "verified"
                if manifest_entries
                and not any(
                    error == "missing_manifest" or error.startswith("manifest_")
                    for error in errors
                )
                else "review_needed"
            )
            payload["packet_manifest_row_count"] = len(manifest_entries)
            payload["packet_manifest_expected_row_count"] = len(
                [name for name in zip_names if name != manifest_name]
            )
    except Exception:
        return {}
    return payload


def build_deterministic_ai_accountant_review(
    *,
    monitor_rows: list[dict[str, Any]],
    action_summary: list[dict[str, Any]],
    dashboard_metrics: dict[str, Any] | None = None,
    llm_error: str = "",
) -> str:
    severity_counts = {
        "P0": sum(1 for row in monitor_rows or [] if str(row.get("severity") or "").upper() == "P0"),
        "P1": sum(1 for row in monitor_rows or [] if str(row.get("severity") or "").upper() == "P1"),
        "P2": sum(1 for row in monitor_rows or [] if str(row.get("severity") or "").upper() == "P2"),
    }
    profit_status = str((dashboard_metrics or {}).get("sales_30d_profit_basis_status") or "").strip()
    close_status = (
        f"Review needed: {len(monitor_rows or [])} monitor item(s), "
        f"P0={severity_counts['P0']}, P1={severity_counts['P1']}, P2={severity_counts['P2']}."
        if monitor_rows
        else "No monitor items found in the selected window."
    )
    if llm_error:
        close_status += " LLM review unavailable; deterministic evidence fallback was used."
    cost_basis_findings = [
        (
            f"{row.get('task_type')}: {row.get('item_count')} item(s), "
            f"P0={row.get('P0')}, P1={row.get('P1')}, P2={row.get('P2')}. "
            f"{row.get('recommended_action')}"
        )
        for row in (action_summary or [])[:8]
    ]
    recommended_actions = [str(row.get("recommended_action") or "").strip() for row in (action_summary or [])[:8]]
    recommended_actions = [action for idx, action in enumerate(recommended_actions) if action and action not in recommended_actions[:idx]]
    if not recommended_actions and monitor_rows:
        recommended_actions = ["Review source evidence and resolve monitor items before close sign-off."]
    payload = {
        "close_status": close_status,
        "profit_basis_notes": [
            f"Dashboard 30-day profit basis status: {profit_status or 'unknown'}.",
            f"COGS review count: {_safe_int((dashboard_metrics or {}).get('sales_30d_cogs_review_count'))}.",
            (
                "Dashboard 30-day profit before returns: "
                f"${_safe_float((dashboard_metrics or {}).get('sales_30d_profit_before_returns')):,.2f}; "
                "estimated profit after returns: "
                f"${_safe_float((dashboard_metrics or {}).get('sales_30d_est_profit')):,.2f}; "
                "sales net after returns: "
                f"${_safe_float((dashboard_metrics or {}).get('sales_30d_net_after_returns')):,.2f}."
            ),
            (
                "Return impact: "
                f"{_safe_int((dashboard_metrics or {}).get('returns_30d_count'))} return(s), "
                f"refunds ${_safe_float((dashboard_metrics or {}).get('returns_30d_refund_total')):,.2f}, "
                f"COGS reversal ${_safe_float((dashboard_metrics or {}).get('returns_30d_cogs_reversal')):,.2f}, "
                f"profit impact ${_safe_float((dashboard_metrics or {}).get('returns_30d_profit_impact')):,.2f}."
            ),
        ],
        "cost_basis_findings": cost_basis_findings,
        "message_followups": [
            "This review was generated from deterministic monitor evidence because the LLM runtime was unavailable."
        ]
        if llm_error
        else [],
        "recommended_human_actions": recommended_actions,
        "unsupported_tax_or_legal_items": [
            "Tax/legal filing or remittance conclusions require accountant/tax-advisor validation."
        ],
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def execute_ai_accountant_workspace_review(
    repo: InventoryRepository,
    *,
    prompt: str,
    system_message: str,
    instruction: str,
    period_label: str,
    start_date: str,
    end_date: str,
    monitor_rows: list[dict[str, Any]],
    exception_rows: list[dict[str, Any]],
    dashboard_metrics: dict[str, Any],
) -> dict[str, Any]:
    max_monitor_rows = max(5, min(50, int(get_runtime_int(repo, "ai_accountant_monitor_review_max_rows", 25))))
    max_exception_rows = max(
        5,
        min(50, int(get_runtime_int(repo, "ai_accountant_monitor_review_max_exception_rows", 25))),
    )
    max_fifo_evidence_rows = max(
        5,
        min(100, int(get_runtime_int(repo, "ai_accountant_monitor_review_max_fifo_evidence_rows", 25))),
    )
    sale_fifo_cogs_evidence_rows: list[dict[str, Any]] = []
    try:
        review_start_dt = datetime.combine(datetime.fromisoformat(start_date).date(), datetime.min.time())
        review_end_dt = datetime.combine(datetime.fromisoformat(end_date).date(), datetime.max.time())
        sale_fifo_cogs_evidence_rows = build_sale_fifo_cogs_evidence_rows(
            repo,
            start_dt=review_start_dt,
            end_dt=review_end_dt,
            max_rows=max_fifo_evidence_rows,
        )
    except Exception:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
    context_kwargs = {
        "period_label": period_label,
        "start_date": start_date,
        "end_date": end_date,
        "monitor_rows": monitor_rows,
        "exception_rows": exception_rows,
        "dashboard_metrics": dashboard_metrics,
        "sale_fifo_cogs_evidence_rows": sale_fifo_cogs_evidence_rows,
    }
    context = build_ai_accountant_review_context(
        **context_kwargs,
        max_monitor_rows=max_monitor_rows,
        max_exception_rows=max_exception_rows,
        max_sale_fifo_cogs_evidence_rows=max_fifo_evidence_rows,
    )
    errors: list[str] = []
    compact_retry = False
    try:
        result = execute_comp_summary(
            repo,
            query=prompt,
            ebay_rows=[],
            web_rows=[],
            spot_context=context,
            system_message=system_message,
            instruction=instruction,
            workflow="accounting",
        )
        return {
            "text": str(result.text or "").strip(),
            "result": result,
            "context": context,
            "error": "",
            "compact_retry": False,
        }
    except Exception as exc:
        errors.append(f"default_context: {str(exc)[:350]}")
        compact_retry = True
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
    context = build_ai_accountant_review_context(
        **context_kwargs,
        max_monitor_rows=5,
        max_exception_rows=5,
        max_sale_fifo_cogs_evidence_rows=5,
    )
    try:
        result = execute_comp_summary(
            repo,
            query=f"{prompt} (compact retry)",
            ebay_rows=[],
            web_rows=[],
            spot_context=context,
            system_message=system_message,
            instruction=instruction,
            workflow="accounting",
        )
        return {
            "text": str(result.text or "").strip(),
            "result": result,
            "context": context,
            "error": " | ".join(errors),
            "compact_retry": compact_retry,
        }
    except Exception as exc:
        errors.append(f"compact_context: {str(exc)[:350]}")
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
    return {
        "text": "",
        "result": None,
        "context": context,
        "error": " | ".join(errors)[:700],
        "compact_retry": compact_retry,
    }


def list_ai_accountant_messages(repo: InventoryRepository, *, limit: int = 25) -> list[dict[str, Any]]:
    db = getattr(repo, "db", None)
    if db is None:
        return []
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "ai_accountant_message")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(max(1, int(limit or 25)))
    ).all()
    return [_audit_row_to_message(row) for row in rows]


def record_ai_accountant_message(
    repo: InventoryRepository,
    *,
    actor: str,
    message: str,
    period_label: str,
    rows: list[dict[str, Any]],
    slack_outbox_id: int | None = None,
    min_severity: str = "",
    requested_min_severity: str = "",
) -> Any:
    effective_min_severity = str(min_severity or "").strip().upper()
    raw_requested_min_severity = str(requested_min_severity or effective_min_severity).strip().upper()
    return repo.record_audit_event(
        entity_type="ai_accountant_message",
        entity_id=None,
        action="create",
        actor=actor,
        changes={
            "message": str(message or "").strip(),
            "period": str(period_label or "").strip(),
            "item_count": len(rows),
            "severity_counts": {
                "P0": sum(1 for row in rows if str(row.get("severity") or "").upper() == "P0"),
                "P1": sum(1 for row in rows if str(row.get("severity") or "").upper() == "P1"),
                "P2": sum(1 for row in rows if str(row.get("severity") or "").upper() == "P2"),
            },
            "sample_items": rows[:10],
            "slack_outbox_id": slack_outbox_id,
            "min_severity": effective_min_severity,
            "requested_min_severity": raw_requested_min_severity,
            "min_severity_fallback_applied": bool(
                raw_requested_min_severity and raw_requested_min_severity != effective_min_severity
            ),
        },
    )


def enqueue_ai_accountant_slack_message(
    repo: InventoryRepository,
    *,
    actor: str,
    message: str,
    period_label: str,
    dedupe_key: str = "",
    channel: str = "",
) -> Any:
    payload = {
        "text": str(message or "").strip(),
        "channel": str(channel or "").strip(),
        "event_type": "ai_accountant_monitor",
        "severity": "warning",
    }
    digest = hashlib.sha256(str(message or "").encode("utf-8")).hexdigest()[:16]
    resolved_dedupe = dedupe_key or f"ai_accountant_monitor:{settings.app_env}:{period_label}:{digest}"
    return repo.enqueue_notification_outbox(
        environment=settings.app_env,
        channel="slack",
        event_type="ai_accountant_monitor",
        entity_type="ai_accountant_message",
        entity_id="",
        dedupe_key=resolved_dedupe,
        payload_json=json.dumps(payload, sort_keys=True),
        requested_by=actor,
        actor=actor,
    )


from app.services.ai_accountant_monitor import (  # noqa: E402
    annotate_ai_accountant_question_rows,
    build_ai_accountant_review_context,
    build_ai_accountant_review_metadata,
    build_ai_accountant_message,
    build_ai_accountant_monitor_rows,
    build_ai_accountant_question_rows,
    build_sale_fifo_cogs_evidence_rows,
    enqueue_ai_accountant_slack_message,
    list_ai_accountant_answer_followups,
    list_ai_accountant_answers,
    record_ai_accountant_answer_followup,
    record_ai_accountant_review_outcome,
    record_ai_accountant_message,
    run_ai_accountant_monitor,
)


def _parse_ai_accountant_json_sections(raw_payload: str | None, section_keys: list[str]) -> dict[str, list[str]]:
    raw = str(raw_payload or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    if not raw.startswith("{"):
        start_idx = raw.find("{")
        end_idx = raw.rfind("}")
        if start_idx >= 0 and end_idx > start_idx:
            raw = raw[start_idx : end_idx + 1].strip()
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    sections: dict[str, list[str]] = {}
    for key in section_keys:
        value = payload.get(key)
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
        elif value is None:
            items = []
        else:
            text = str(value).strip()
            items = [text] if text else []
        if items:
            sections[key] = items
    return sections


def _render_ai_accountant_json_sections(raw_payload: str | None) -> bool:
    section_labels = [
        ("close_status", "Close Status"),
        ("profit_basis_notes", "Profit Basis Notes"),
        ("cost_basis_findings", "Cost Basis Findings"),
        ("message_followups", "Message Follow-Ups"),
        ("recommended_human_actions", "Recommended Human Actions"),
        ("unsupported_tax_or_legal_items", "Unsupported Tax or Legal Items"),
    ]
    sections = _parse_ai_accountant_json_sections(raw_payload, [key for key, _label in section_labels])
    if not sections:
        return False
    for key, label in section_labels:
        items = sections.get(key) or []
        if not items:
            continue
        st.markdown(f"**{label}**")
        for item in items:
            st.markdown(f"- {item}")
    return True


def render_ai_accountant(repo: InventoryRepository) -> None:
    st.subheader("AI Accountant")
    render_help_panel(
        section_title="AI Accountant",
        goal="Monitor accounting cleanup items, leave in-app review notes, and queue Slack follow-ups.",
        steps=[
            "Review exception severity and recommended accounting cleanup actions.",
            "Record an in-app AI Accountant message for audit and close-review follow-up.",
            "Queue Slack alerts when accounting cleanup needs operator attention outside the app.",
        ],
        roadmap_phase="GS-V10-020 Accounting Verification + AI Accountant",
    )
    user = current_user()
    if not ensure_permission(user, "ai_accountant_use", "Use AI Accountant"):
        st.stop()

    today = utcnow_naive().date()
    default_start = today - timedelta(days=30)
    c1, c2 = st.columns(2)
    from_date = c1.date_input("From", value=default_start, key="ai_accountant_from")
    to_date = c2.date_input("To", value=today, key="ai_accountant_to")
    start_dt = datetime.combine(from_date, datetime.min.time())
    end_dt = datetime.combine(to_date, datetime.max.time())
    period_label = f"{from_date.isoformat()} to {to_date.isoformat()}"

    exception_rows: list[dict[str, Any]] = []
    dashboard_metrics: dict[str, Any] = {}
    sale_fifo_cogs_evidence_rows: list[dict[str, Any]] = []
    try:
        exception_rows = list(repo.report_accounting_exception_rows(start_dt=start_dt, end_dt=end_dt) or [])
    except Exception as exc:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
        st.warning(f"Accounting exception monitor could not load: {exc}")
    try:
        dashboard_metrics = dict(repo.dashboard_live_metrics(now=end_dt, include_fee_type_breakdown=False) or {})
    except Exception:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
        dashboard_metrics = {}
    try:
        sale_fifo_cogs_evidence_rows = build_sale_fifo_cogs_evidence_rows(
            repo,
            start_dt=start_dt,
            end_dt=end_dt,
            max_rows=max(25, min(500, int(get_runtime_int(repo, "ai_accountant_evidence_packet_fifo_rows", 200)))),
        )
    except Exception:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
        sale_fifo_cogs_evidence_rows = []

    review_outcomes = list_ai_accountant_review_outcomes(repo)
    answer_rows = list_ai_accountant_answers(repo)
    answer_followup_rows = list_ai_accountant_answer_followups(repo)
    monitor_rows = build_ai_accountant_monitor_rows(
        exception_rows,
        dashboard_metrics=dashboard_metrics,
        review_outcome_rows=review_outcomes,
        answer_rows=answer_rows,
    )
    p0 = sum(1 for row in monitor_rows if str(row.get("severity") or "").upper() == "P0")
    p1 = sum(1 for row in monitor_rows if str(row.get("severity") or "").upper() == "P1")
    p2 = sum(1 for row in monitor_rows if str(row.get("severity") or "").upper() == "P2")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Open Items", len(monitor_rows))
    m2.metric("P0", p0)
    m3.metric("P1", p1)
    m4.metric("P2", p2)

    review_summary = summarize_ai_accountant_review_outcomes(review_outcomes)
    if review_summary["latest_outcome"] != "none":
        status_detail = " | ".join(
            part
            for part in [
                str(review_summary.get("actor") or "").strip(),
                str(review_summary.get("recorded_at") or "").strip(),
            ]
            if part
        )
        if review_summary["needs_followup"]:
            st.warning(f"{review_summary['status']} {status_detail}".strip())
        elif review_summary.get("packet_needs_review"):
            st.warning(
                (
                    f"{review_summary['status']} Evidence packet needs review: "
                    f"integrity={review_summary.get('packet_status')}, "
                    f"manifest={review_summary.get('packet_manifest_status')}, "
                    f"errors={review_summary.get('packet_integrity_error_count')}. "
                    f"{status_detail}"
                ).strip()
            )
        else:
            st.success(f"{review_summary['status']} {status_detail}".strip())
    else:
        st.info("No AI Accountant review outcome has been recorded yet.")

    packet_action_rows = build_ai_accountant_packet_review_action_rows(review_summary)
    action_input_rows = [*packet_action_rows, *list(monitor_rows or [])]
    if action_input_rows:
        st.warning("AI Accountant found accounting cleanup items that should be resolved before close sign-off.")
        action_summary = build_ai_accountant_action_summary(action_input_rows)
        if action_summary:
            st.markdown("### Action Summary")
            st.dataframe(pd.DataFrame(action_summary), hide_index=True, use_container_width=True)
        question_rows = annotate_ai_accountant_question_rows(
            build_ai_accountant_question_rows(action_input_rows, max_rows=12),
            answer_rows,
        )
        if question_rows:
            st.markdown("### Questions To Answer")
            answered_count = sum(
                1 for row in question_rows if str(row.get("answer_status") or "") in {"answered", "applied"}
            )
            st.caption(
                "Use these prompts in Ask GoldenStackers or Slack so the AI Accountant can connect your answer "
                "back to the open accounting issue. The app stays read-only; corrections still need normal workflow edits. "
                f"{answered_count} of {len(question_rows)} visible question(s) already have recorded answer evidence."
            )
            st.dataframe(pd.DataFrame(question_rows), hide_index=True, use_container_width=True)
        if monitor_rows:
            st.dataframe(pd.DataFrame(monitor_rows), hide_index=True, use_container_width=True)
    else:
        action_summary = []
        question_rows = []
        st.success("No accounting monitor items found for the selected window.")

    st.markdown("### Monitor Automation")
    runtime_summary = build_ai_accountant_runtime_summary(repo)
    st.dataframe(pd.DataFrame(runtime_summary), hide_index=True, use_container_width=True)
    setup_checks = build_ai_accountant_setup_checks(runtime_summary)
    warn_checks = [row for row in setup_checks if str(row.get("status") or "").lower() == "warn"]
    applied_defaults = st.session_state.pop("ai_accountant_applied_runtime_defaults", [])
    if applied_defaults:
        st.success(
            "Applied AI Accountant automation defaults: "
            + ", ".join(str(row.get("key") or "") for row in applied_defaults)
        )
    if warn_checks:
        st.warning(
            "AI Accountant setup has warning(s): "
            + ", ".join(str(row.get("check") or "") for row in warn_checks[:4])
        )
    else:
        st.success("AI Accountant automation setup is ready.")
    with st.expander("Setup Checks", expanded=bool(warn_checks)):
        st.dataframe(pd.DataFrame(setup_checks), hide_index=True, use_container_width=True)
    runtime_chain_rows = build_ai_accountant_runtime_chain_rows(repo)
    runtime_chain_blocked = any(str(row.get("status") or "").lower() == "error" for row in runtime_chain_rows)
    with st.expander("Accounting AI Runtime Chain", expanded=runtime_chain_blocked):
        st.dataframe(pd.DataFrame(runtime_chain_rows), hide_index=True, use_container_width=True)
        st.caption(
            "This is the sanitized fallback order used by AI Accountant scheduled reviews, manual reviews, and smoke tests."
        )
        if st.button("Run AI Accountant LLM Smoke Test", key="ai_accountant_runtime_smoke_test_btn"):
            smoke_result = run_ai_accountant_runtime_smoke_test(repo)
            st.session_state["ai_accountant_runtime_smoke_test_result"] = smoke_result
            st.rerun()
        smoke_result = st.session_state.get("ai_accountant_runtime_smoke_test_result")
        if isinstance(smoke_result, dict) and smoke_result:
            if smoke_result.get("status") == "ok":
                st.success(
                    "AI Accountant LLM route responded: "
                    f"{smoke_result.get('provider')}/{smoke_result.get('model')}"
                )
                if smoke_result.get("fallback_attempts"):
                    st.caption(
                        f"Fallback attempts before success: {smoke_result.get('fallback_attempts')}. "
                        f"{smoke_result.get('fallback_errors') or ''}"
                    )
                if smoke_result.get("text_preview"):
                    st.code(str(smoke_result.get("text_preview") or ""), language="json")
            else:
                st.error(f"AI Accountant LLM route failed: {smoke_result.get('error') or 'unknown error'}")
    try:
        outbox_rows = build_ai_accountant_outbox_delivery_rows(repo, limit=10)
    except Exception as exc:
        db = getattr(repo, "db", None)
        if db is not None:
            db.rollback()
        outbox_rows = []
        st.warning(f"AI Accountant Slack delivery status could not load: {exc}")
    if outbox_rows:
        outbox_summary = summarize_ai_accountant_outbox_delivery_rows(outbox_rows)
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Slack Delivery Rows", int(outbox_summary["total"]))
        d2.metric("Due", int(outbox_summary["due"]))
        d3.metric("Retrying/Failed", int(outbox_summary["blocked"]))
        d4.metric("Sent", int(outbox_summary["sent"]))
        blocking_outbox = [
            row
            for row in outbox_rows
            if str(row.get("status") or "").strip().lower() in {"retrying", "failed"}
        ]
        if blocking_outbox:
            st.warning("Recent AI Accountant Slack delivery rows need attention.")
            if outbox_summary.get("latest_error"):
                st.caption(f"Latest delivery error: {outbox_summary['latest_error']}")
        with st.expander("Recent Slack Delivery", expanded=bool(blocking_outbox)):
            st.dataframe(pd.DataFrame(outbox_rows), hide_index=True, use_container_width=True)
            if st.button("Process Due AI Accountant Slack Deliveries", key="ai_accountant_process_due_outbox_btn"):
                try:
                    outbox_result = process_due_ai_accountant_outbox_rows(repo, actor=user.username, limit=10)
                    st.success(
                        "Processed due AI Accountant Slack delivery rows: "
                        f"attempted={outbox_result['attempted']} sent={outbox_result['sent']} "
                        f"failed={outbox_result['failed']}"
                    )
                    if outbox_result.get("messages"):
                        st.caption("Recent delivery result: " + " | ".join(outbox_result["messages"][:2]))
                    st.rerun()
                except Exception as exc:
                    db = getattr(repo, "db", None)
                    if db is not None:
                        db.rollback()
                    st.error(f"Could not process due AI Accountant Slack deliveries: {exc}")
    if warn_checks:
        if st.button("Enable Recommended Automation Defaults", key="ai_accountant_enable_defaults_btn"):
            try:
                applied = apply_ai_accountant_recommended_runtime_settings(repo, actor=user.username)
                st.session_state["ai_accountant_applied_runtime_defaults"] = applied
                st.rerun()
            except Exception as exc:
                db = getattr(repo, "db", None)
                if db is not None:
                    db.rollback()
                st.error(f"Could not apply AI Accountant automation defaults: {exc}")
        st.caption(
            "Recommended defaults enable the six-hour monitor, Slack alert queue, scheduled LLM review, accountant chat, and external web research context."
        )
    st.caption(
        "Runs the same monitor path used by sync-runner now, including in-app message recording, optional Slack outbox queueing, and optional scheduled LLM review runtime settings."
    )
    auto_cols = st.columns(4)
    with auto_cols[0]:
        run_now_min_severity = st.selectbox(
            "Alert Severity",
            options=["P0", "P1", "P2"],
            index=1,
            key="ai_accountant_run_now_min_severity",
        )
    with auto_cols[1]:
        run_now_slack = st.checkbox(
            "Queue Slack",
            value=False,
            key="ai_accountant_run_now_slack_enabled",
        )
    with auto_cols[2]:
        run_now_record_empty = st.checkbox(
            "Record Empty Run",
            value=False,
            key="ai_accountant_run_now_record_empty",
        )
    with auto_cols[3]:
        run_now_channel = st.text_input(
            "Slack Channel",
            value="",
            key="ai_accountant_run_now_slack_channel",
            help="Optional. Uses Slack routing/default channel if blank.",
        )
    if st.button("Run AI Accountant Monitor Now", key="ai_accountant_run_monitor_now_btn"):
        try:
            lookback_days = max(1, int((to_date - from_date).days) + 1)
            result = run_ai_accountant_monitor(
                repo,
                actor=user.username,
                now=end_dt,
                lookback_days=lookback_days,
                min_severity=str(run_now_min_severity or "P1"),
                slack_enabled=bool(run_now_slack),
                slack_channel=str(run_now_channel or "").strip(),
                record_when_empty=bool(run_now_record_empty),
            )
            st.session_state["ai_accountant_last_monitor_run_result"] = result
            st.rerun()
        except Exception as exc:
            db = getattr(repo, "db", None)
            if db is not None and hasattr(db, "rollback"):
                db.rollback()
            st.error(f"AI Accountant monitor run failed: {exc}")
    last_monitor_result = st.session_state.get("ai_accountant_last_monitor_run_result")
    if isinstance(last_monitor_result, dict) and last_monitor_result:
        monitor_run_summary = summarize_monitor_run_result(last_monitor_result)
        status_text = " ".join(
            part
            for part in [
                str(monitor_run_summary.get("status") or "").strip(),
                str(monitor_run_summary.get("details") or "").strip(),
            ]
            if part
        )
        if monitor_run_summary.get("severity") == "warning":
            st.warning(status_text)
        elif monitor_run_summary.get("severity") == "success":
            st.success(status_text)
        else:
            st.info(status_text)
        with st.expander("Last Monitor Run Result", expanded=False):
            st.json(last_monitor_result)

    st.markdown("### AI Review")
    st.caption(
        "Runs a read-only LLM review over the monitor, exception queue, and dashboard profit-basis evidence."
    )
    if st.button("Run AI Accountant Review", key="ai_accountant_run_review_btn"):
        try:
            started = time.perf_counter()
            system_message = get_runtime_str(
                repo,
                "accountant_llm_system_message",
                (
                    "You are GoldenStackers' read-only AI Accountant. Cite source tables/rows, "
                    "label estimated versus actual values, and never provide tax/legal conclusions."
                ),
            ).strip()
            instruction = (
                "Return ONLY JSON with keys: `close_status`, `profit_basis_notes`, "
                "`cost_basis_findings`, `message_followups`, `recommended_human_actions`, "
                "`unsupported_tax_or_legal_items`. Values must be concise arrays or strings. "
                "Review monitor rows, accounting exceptions, and dashboard profit-basis evidence. "
                "Explicitly call out missing or fallback cost basis, mixed-lot allocation review, "
                "fee/label evidence gaps, and any contradiction that should block close sign-off. "
                "Do not propose direct writes; draft only human-review recommendations. "
                "Use tax evidence only to identify advisor-review needs, not filing or legal conclusions."
            )
            prompt = "AI Accountant workspace monitor review"
            review_result = execute_ai_accountant_workspace_review(
                repo,
                prompt=prompt,
                system_message=system_message,
                instruction=instruction,
                period_label=period_label,
                start_date=from_date.isoformat(),
                end_date=to_date.isoformat(),
                monitor_rows=monitor_rows,
                exception_rows=exception_rows,
                dashboard_metrics=dashboard_metrics,
            )
            result = review_result.get("result")
            context = dict(review_result.get("context") or {})
            review_text = str(review_result.get("text") or "").strip()
            deterministic_fallback_error = str(review_result.get("error") or "").strip()
            if not review_text:
                review_text = build_deterministic_ai_accountant_review(
                    monitor_rows=monitor_rows,
                    action_summary=action_summary,
                    dashboard_metrics=dashboard_metrics,
                    llm_error=deterministic_fallback_error,
                )
            metadata = build_ai_accountant_review_metadata(
                surface="ai_accountant_workspace",
                prompt=prompt,
                system_message=system_message,
                instruction=instruction,
                context=context,
                citation=dict(getattr(result, "citation", {}) or {})
                if result is not None
                else {
                    "tool_name": "deterministic_ai_accountant_review",
                    "source": "local_fallback",
                    "fallback_errors": [deterministic_fallback_error],
                },
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            metadata["elapsed_ms"] = elapsed_ms
            metadata["compact_retry"] = bool(review_result.get("compact_retry"))
            review_packet_summary = read_ai_accountant_evidence_zip_summary(
                build_ai_accountant_evidence_zip(
                    period_label=period_label,
                    action_summary=action_summary,
                    monitor_rows=monitor_rows,
                    exception_rows=exception_rows,
                    messages=list_ai_accountant_messages(repo),
                    review_outcomes=review_outcomes,
                    answer_rows=answer_rows,
                    answer_followup_rows=answer_followup_rows,
                    dashboard_metrics=dashboard_metrics,
                    sale_fifo_cogs_evidence_rows=sale_fifo_cogs_evidence_rows,
                ),
                period_label=period_label,
            )
            if review_packet_summary:
                metadata["evidence_packet_hash_sha256"] = str(
                    review_packet_summary.get("evidence_hash_sha256") or ""
                )
                metadata["evidence_packet_row_counts"] = dict(review_packet_summary.get("row_counts") or {})
                metadata["evidence_packet_artifact_hashes_sha256"] = dict(
                    review_packet_summary.get("artifact_hashes_sha256") or {}
                )
                metadata["evidence_packet_integrity_status"] = str(
                    review_packet_summary.get("packet_integrity_status") or ""
                )
                metadata["evidence_packet_integrity_errors"] = list(
                    review_packet_summary.get("packet_integrity_errors") or []
                )
                metadata["evidence_packet_integrity_error_count"] = _safe_int(
                    review_packet_summary.get("packet_integrity_error_count")
                )
                metadata["evidence_packet_manifest_status"] = str(
                    review_packet_summary.get("packet_manifest_status") or ""
                )
                metadata["evidence_packet_manifest_row_count"] = _safe_int(
                    review_packet_summary.get("packet_manifest_row_count")
                )
                metadata["evidence_packet_manifest_expected_row_count"] = _safe_int(
                    review_packet_summary.get("packet_manifest_expected_row_count")
                )
                metadata["evidence_packet_action_summary_task_counts"] = dict(
                    review_packet_summary.get("action_summary_task_counts") or {}
                )
            if deterministic_fallback_error:
                metadata["runtime_fallback"] = "deterministic_monitor_review"
                metadata["llm_error"] = deterministic_fallback_error[:1000]
            st.session_state["ai_accountant_review_raw"] = review_text
            st.session_state["ai_accountant_review_metadata"] = metadata
            if hasattr(repo, "log_ai_chat_interaction"):
                try:
                    repo.log_ai_chat_interaction(
                        actor=user.username,
                        prompt=prompt,
                        intent="ai_accountant_workspace_review",
                        allowed_domains=["accounting", "reports", "sales", "orders", "inventory", "tax"],
                        citations=[
                            {
                                "table": "accounting_exception_queue",
                                "filters": f"from={from_date.isoformat()};to={to_date.isoformat()}",
                                "rows_considered": int(len(exception_rows)),
                                "as_of_utc": datetime.now(timezone.utc).isoformat(),
                            },
                            {
                                "table": "ai_accountant_monitor_rows",
                                "filters": f"from={from_date.isoformat()};to={to_date.isoformat()}",
                                "rows_considered": int(len(monitor_rows)),
                                "as_of_utc": datetime.now(timezone.utc).isoformat(),
                            },
                            {
                                "table": "dashboard_live_metrics",
                                "filters": "profit_basis=30d",
                                "rows_considered": 1 if dashboard_metrics else 0,
                                "as_of_utc": datetime.now(timezone.utc).isoformat(),
                            },
                            {
                                "table": "sale_fifo_cogs_evidence",
                                "filters": f"from={from_date.isoformat()};to={to_date.isoformat()}",
                                "rows_considered": int(
                                    context.get("sale_fifo_cogs_evidence_summary", {}).get("row_count") or 0
                                ),
                                "as_of_utc": datetime.now(timezone.utc).isoformat(),
                            },
                        ],
                        answer_preview=review_text,
                        denied=False,
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                except Exception:
                    pass
            if deterministic_fallback_error:
                st.warning("AI Accountant LLM runtime failed; deterministic monitor review was generated instead.")
            else:
                st.success("AI Accountant review complete.")
            st.rerun()
        except Exception as exc:
            db = getattr(repo, "db", None)
            if db is not None and hasattr(db, "rollback"):
                db.rollback()
            st.error(f"AI Accountant review failed: {exc}")

    raw_review = str(st.session_state.get("ai_accountant_review_raw") or "").strip()
    if raw_review:
        with st.expander("AI Accountant Review Result", expanded=False):
            if not _render_ai_accountant_json_sections(raw_review):
                st.write(raw_review)
            st.code(raw_review, language="json")
            f1, f2, f3 = st.columns(3)
            if f1.button("Accept Review", key="ai_accountant_review_accept_btn"):
                record_ai_accountant_review_outcome(
                    repo,
                    actor=user.username,
                    outcome="accepted",
                    answer_text=raw_review,
                    review_metadata=st.session_state.get("ai_accountant_review_metadata") or {},
                )
                st.success("AI Accountant review acceptance recorded.")
                st.rerun()
            if f2.button("Needs Edits", key="ai_accountant_review_edit_btn"):
                record_ai_accountant_review_outcome(
                    repo,
                    actor=user.username,
                    outcome="edited",
                    answer_text=raw_review,
                    review_metadata=st.session_state.get("ai_accountant_review_metadata") or {},
                )
                st.success("AI Accountant review edit outcome recorded.")
                st.rerun()
            if f3.button("Reject Review", key="ai_accountant_review_reject_btn"):
                record_ai_accountant_review_outcome(
                    repo,
                    actor=user.username,
                    outcome="rejected",
                    answer_text=raw_review,
                    review_metadata=st.session_state.get("ai_accountant_review_metadata") or {},
                )
                st.success("AI Accountant review rejection recorded.")
                st.rerun()

    message = build_ai_accountant_message(
        monitor_rows,
        period_label=period_label,
        answer_rows=answer_rows,
    )
    st.markdown("### Message Draft")
    edited_message = st.text_area("AI Accountant message", value=message, height=220)
    alert_channel = st.text_input("Slack channel override", value="", help="Optional. Uses Slack routing/default channel if blank.")

    a1, a2 = st.columns(2)
    if a1.button("Record In-App Message", disabled=not edited_message.strip()):
        record_ai_accountant_message(
            repo,
            actor=user.username,
            message=edited_message,
            period_label=period_label,
            rows=monitor_rows,
        )
        st.success("AI Accountant message recorded in the app.")
        st.rerun()
    if a2.button("Queue Slack Message", disabled=not edited_message.strip()):
        outbox = enqueue_ai_accountant_slack_message(
            repo,
            actor=user.username,
            message=edited_message,
            period_label=period_label,
            channel=alert_channel,
        )
        record_ai_accountant_message(
            repo,
            actor=user.username,
            message=edited_message,
            period_label=period_label,
            rows=monitor_rows,
            slack_outbox_id=int(getattr(outbox, "id", 0) or 0) or None,
        )
        st.success(f"Slack message queued in notification outbox #{int(getattr(outbox, 'id', 0) or 0)}.")
        st.rerun()

    st.markdown("### Recent AI Accountant Messages")
    messages = list_ai_accountant_messages(repo)
    if messages:
        threshold_summary = summarize_ai_accountant_message_thresholds(messages)
        if threshold_summary["warning"]:
            st.warning(threshold_summary["warning"])
        st.dataframe(pd.DataFrame(messages), hide_index=True, use_container_width=True)
    else:
        st.caption("No AI Accountant messages recorded yet.")

    st.markdown("### Recent AI Review Outcomes")
    if review_outcomes:
        st.dataframe(pd.DataFrame(review_outcomes), hide_index=True, use_container_width=True)
    else:
        st.caption("No AI Accountant review outcomes recorded yet.")

    st.markdown("### Recent AI Accountant Answers")
    if answer_rows:
        st.caption(
            "Operator answers captured from Ask or Slack prompts. These are evidence for follow-up, not automatic corrections."
        )
        answer_followup_status_counts = build_ai_accountant_answer_followup_status_counts(answer_rows)
        unresolved_answer_count = _safe_int(answer_followup_status_counts.get("needs_more_info")) + _safe_int(
            answer_followup_status_counts.get("obsolete")
        )
        if unresolved_answer_count:
            st.warning(
                f"{unresolved_answer_count} AI Accountant answer(s) still need replacement evidence or follow-up."
            )
        if answer_followup_status_counts:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"followup_status": key, "answer_count": value}
                        for key, value in sorted(answer_followup_status_counts.items())
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
        st.dataframe(pd.DataFrame(answer_rows), hide_index=True, use_container_width=True)
        answer_options = [
            (
                f"{str(row.get('task_type') or 'answer')} {str(row.get('reference') or '')} "
                f"({str(row.get('answer_hash_sha256') or '')[:12]})"
            )
            for row in answer_rows
        ]
        selected_answer_label = st.selectbox(
            "Answer follow-up",
            options=answer_options,
            key="ai_accountant_answer_followup_select",
            help="Mark whether the answer has been applied through the normal correction workflow.",
        )
        selected_answer_idx = answer_options.index(selected_answer_label) if selected_answer_label in answer_options else 0
        selected_answer = answer_rows[selected_answer_idx]
        f1, f2 = st.columns([1, 2])
        with f1:
            answer_followup_outcome = st.selectbox(
                "Follow-up status",
                options=["applied", "needs_more_info", "obsolete"],
                key="ai_accountant_answer_followup_status",
            )
        with f2:
            answer_followup_notes = st.text_input(
                "Follow-up notes",
                value="",
                key="ai_accountant_answer_followup_notes",
                placeholder="Example: product cost updated from lot assignment #39",
            )
        if st.button("Record Answer Follow-Up", key="ai_accountant_answer_followup_btn"):
            record_ai_accountant_answer_followup(
                repo,
                actor=user.username,
                answer_hash_sha256=str(selected_answer.get("answer_hash_sha256") or ""),
                outcome=str(answer_followup_outcome or ""),
                notes=str(answer_followup_notes or ""),
            )
            st.success("AI Accountant answer follow-up recorded.")
            st.rerun()
    else:
        st.caption("No AI Accountant operator answers recorded yet.")

    evidence_zip = build_ai_accountant_evidence_zip(
        period_label=period_label,
        action_summary=action_summary,
        monitor_rows=monitor_rows,
        exception_rows=exception_rows,
        messages=messages,
        review_outcomes=review_outcomes,
        answer_rows=answer_rows,
        answer_followup_rows=answer_followup_rows,
        dashboard_metrics=dashboard_metrics,
        sale_fifo_cogs_evidence_rows=sale_fifo_cogs_evidence_rows,
    )
    evidence_summary = read_ai_accountant_evidence_zip_summary(evidence_zip, period_label=period_label)
    st.markdown("### Evidence Packet Summary")
    summary_counts = evidence_summary.get("row_counts", {}) if isinstance(evidence_summary, dict) else {}
    e1, e2, e3, e4, e5, e6, e7, e8 = st.columns(8)
    e1.metric("Monitor Rows", _safe_int(summary_counts.get("monitor_rows")))
    e2.metric("FIFO Evidence Rows", _safe_int(summary_counts.get("sale_fifo_cogs_evidence")))
    e3.metric("Exception Rows", _safe_int(summary_counts.get("accounting_exception_rows")))
    e4.metric("Review Outcomes", _safe_int(summary_counts.get("review_outcomes")))
    e5.metric("Answers", _safe_int(summary_counts.get("answers")))
    e6.metric("Answer Follow-Ups", _safe_int(summary_counts.get("answer_followups")))
    e7.metric("Hash Index Rows", _safe_int(summary_counts.get("review_hash_index")))
    e8.metric("Artifacts", _safe_int(evidence_summary.get("artifact_count")))
    v1, v2, v3, v4 = st.columns(4)
    v1.metric("Integrity Errors", _safe_int(evidence_summary.get("packet_integrity_error_count")))
    v2.metric("Verified Artifacts", _safe_int(evidence_summary.get("packet_verified_artifact_count")))
    v3.metric("Manifest Rows", _safe_int(evidence_summary.get("packet_manifest_row_count")))
    v4.metric("Expected Manifest Rows", _safe_int(evidence_summary.get("packet_manifest_expected_row_count")))
    st.caption(f"Evidence hash: {str(evidence_summary.get('evidence_hash_sha256') or '')}")
    integrity_status = str(evidence_summary.get("packet_integrity_status") or "").strip()
    if integrity_status == "verified":
        manifest_status = str(evidence_summary.get("packet_manifest_status") or "").strip()
        if manifest_status == "verified":
            st.success("Evidence packet integrity and manifest verified.")
        else:
            st.warning("Evidence packet integrity verified, but manifest verification needs review.")
    elif integrity_status:
        st.warning(
            "Evidence packet integrity needs review: "
            + ", ".join(str(item) for item in evidence_summary.get("packet_integrity_errors") or [])
        )
    action_task_counts = evidence_summary.get("action_summary_task_counts")
    if isinstance(action_task_counts, dict) and action_task_counts:
        with st.expander("Evidence Packet Action Task Counts", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {"task_type": str(task_type), "row_count": _safe_int(count)}
                        for task_type, count in sorted(action_task_counts.items())
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
    answer_status_counts = evidence_summary.get("answer_followup_status_counts")
    if isinstance(answer_status_counts, dict) and answer_status_counts:
        with st.expander("Evidence Packet Answer Status Counts", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {"followup_status": key, "answer_count": value}
                        for key, value in sorted(answer_status_counts.items())
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
    st.download_button(
        "Download AI Accountant Evidence Packet",
        data=evidence_zip,
        file_name=f"ai_accountant_evidence_{from_date.isoformat()}_{to_date.isoformat()}.zip",
        mime="application/zip",
        key="ai_accountant_evidence_zip_download",
    )
