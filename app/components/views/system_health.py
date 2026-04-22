from __future__ import annotations

import os
import platform
import shutil
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import text

from app.auth import current_user, ensure_permission
from app.components.views.shared import render_help_panel
from app.config import settings
from app.services.config_health import health_state, required_env_keys, required_runtime_keys
from app.services.ebay import EbayClient
from app.services.env_manager import read_env_file
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_bool, get_runtime_float, get_runtime_int, get_runtime_str
from app.services.slack_notify import (
    build_slack_alert_text,
    check_slack_connectivity,
    dispatch_slack_alert,
    resolve_slack_notify_config,
)
from app.services.spot_price import SpotPriceService
from app.utils.time import app_now, app_timezone_name


_PERF_PROBE_BUDGETS_MS_DEFAULTS: dict[str, float] = {
    "dashboard_metrics": 220.0,
    "dashboard_live_metrics": 180.0,
    "report_shipping_economics_summary": 280.0,
    "report_shipping_economics_rows": 650.0,
    "report_tax_estimate_detail_rows": 750.0,
    "report_ebay_fee_reconciliation_rows_extended": 900.0,
    "list_products": 500.0,
    "list_listings": 500.0,
    "list_orders": 500.0,
    "list_integration_queue_jobs_slack": 700.0,
    "list_integration_queue_jobs_google": 700.0,
    "integration_event_rows_shared_14d": 850.0,
    "integration_event_rows_shipping_validation_30d": 1000.0,
}


def _probe_budget_ms(repo, probe_name: str) -> float:
    normalized = str(probe_name or "").strip().lower()
    default = float(_PERF_PROBE_BUDGETS_MS_DEFAULTS.get(normalized, 500.0))
    key = f"perf_budget_{normalized}_ms"
    return float(get_runtime_float(repo, key, default))


def _normalize_page_baseline_rows(repo, rows: list[dict] | None) -> list[dict]:
    normalized_rows: list[dict] = []
    for row in rows or []:
        probe_name = str((row or {}).get("probe_name") or "").strip()
        elapsed_ms = float((row or {}).get("elapsed_ms") or 0.0)
        budget_ms = _probe_budget_ms(repo, probe_name)
        normalized_rows.append(
            {
                **dict(row or {}),
                "budget_ms": round(float(budget_ms), 3),
                "over_budget": bool(elapsed_ms > float(budget_ms)),
            }
        )
    return normalized_rows


def _page_baseline_summary(rows: list[dict] | None) -> dict[str, float | int]:
    normalized = list(rows or [])
    if not normalized:
        return {
            "total_count": 0,
            "over_budget_count": 0,
            "worst_elapsed_ms": 0.0,
        }
    over_budget_count = int(sum(1 for row in normalized if bool(row.get("over_budget"))))
    worst_elapsed_ms = float(max(float((row or {}).get("elapsed_ms") or 0.0) for row in normalized))
    return {
        "total_count": int(len(normalized)),
        "over_budget_count": over_budget_count,
        "worst_elapsed_ms": worst_elapsed_ms,
    }


def _safe_select_all(repo, sql: str, *, label: str, params: dict | None = None) -> list[tuple]:
    try:
        return list(repo.db.execute(text(sql), params or {}).all())
    except Exception as exc:
        try:
            repo.db.rollback()
        except Exception:
            pass
        st.warning(f"{label} query failed; recovered DB session. {exc}")
        return []


def _safe_select_first(repo, sql: str, *, label: str, params: dict | None = None):
    try:
        return repo.db.execute(text(sql), params or {}).first()
    except Exception as exc:
        try:
            repo.db.rollback()
        except Exception:
            pass
        st.warning(f"{label} query failed; recovered DB session. {exc}")
        return None


def _safe_scalar_one(repo, sql: str, *, label: str, params: dict | None = None, default=0):
    try:
        return repo.db.execute(text(sql), params or {}).scalar_one()
    except Exception as exc:
        try:
            repo.db.rollback()
        except Exception:
            pass
        st.warning(f"{label} query failed; recovered DB session. {exc}")
        return default


def _read_proc_meminfo() -> tuple[int | None, int | None]:
    mem_total_kb = None
    mem_available_kb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available_kb = int(line.split()[1])
    except Exception:
        return None, None
    return mem_total_kb, mem_available_kb


def _read_proc_rss_kb() -> int | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _fmt_gb_from_kb(kb: int | None) -> str:
    if kb is None:
        return "n/a"
    return f"{(kb / 1024 / 1024):,.2f} GB"


def _status_row(name: str, status: str, details: str) -> dict:
    return {"component": name, "status": status, "details": details}


def _slack_ops_health_snapshot(
    rows: list[Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or utcnow_naive()
    status_counts: dict[str, int] = {"queued": 0, "running": 0, "blocked": 0, "failed": 0, "success": 0}
    due_count = 0
    pending_approval_count = 0
    pending_approval_ages_hours: list[float] = []
    for row in rows or []:
        status = str(getattr(row, "status", "") or "").strip().lower()
        if status in status_counts:
            status_counts[status] += 1
        next_attempt_at = getattr(row, "next_attempt_at", None)
        if status == "queued" and (next_attempt_at is None or next_attempt_at <= now_dt):
            due_count += 1

        payload = {}
        try:
            payload_raw = json.loads(str(getattr(row, "payload_json", "") or "{}"))
            if isinstance(payload_raw, dict):
                payload = payload_raw
        except Exception:
            payload = {}
        approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
        if status == "blocked" and bool(approval.get("required")) and str(approval.get("status") or "").strip().lower() == "pending":
            pending_approval_count += 1
            requested_at_raw = str(approval.get("requested_at") or "").strip()
            try:
                if requested_at_raw:
                    requested_at = datetime.fromisoformat(requested_at_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                    pending_approval_ages_hours.append(max(0.0, (now_dt - requested_at).total_seconds() / 3600.0))
            except Exception:
                pass
    return {
        "total_count": int(len(rows or [])),
        "due_count": int(due_count),
        "queued_count": int(status_counts["queued"]),
        "running_count": int(status_counts["running"]),
        "blocked_count": int(status_counts["blocked"]),
        "success_count": int(status_counts["success"]),
        "failed_count": int(status_counts["failed"]),
        "pending_approval_count": int(pending_approval_count),
        "pending_approval_avg_hours": round(sum(pending_approval_ages_hours) / len(pending_approval_ages_hours), 2)
        if pending_approval_ages_hours
        else 0.0,
        "pending_approval_max_hours": round(max(pending_approval_ages_hours), 2) if pending_approval_ages_hours else 0.0,
    }


def _rollup_explain_failures(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows or []:
        error_raw = str((row or {}).get("error") or "").strip()
        if not error_raw:
            continue
        failures.append(
            {
                "rollup_name": str((row or {}).get("rollup_name") or ""),
                "error": error_raw,
                "elapsed_ms": float((row or {}).get("elapsed_ms") or 0.0),
                "sample_limit": int((row or {}).get("sample_limit") or 0),
            }
        )
    return failures


def _rollup_explain_skips(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    skips: list[dict[str, Any]] = []
    for row in rows or []:
        if not bool((row or {}).get("skipped")):
            continue
        skips.append(
            {
                "rollup_name": str((row or {}).get("rollup_name") or ""),
                "skip_reason": str((row or {}).get("skip_reason") or "").strip(),
                "sample_limit": int((row or {}).get("sample_limit") or 0),
            }
        )
    return skips


def _parse_iso_naive(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _ebay_oauth_auto_refresh_snapshot(repo, now_utc_naive: datetime) -> dict:
    enabled = bool(get_runtime_bool(repo, "ebay_user_token_auto_refresh_enabled", True))
    interval_hours = max(1, min(72, int(get_runtime_int(repo, "ebay_user_token_auto_refresh_interval_hours", 12))))
    min_ttl_minutes = max(5, min(240, int(get_runtime_int(repo, "ebay_user_token_auto_refresh_min_ttl_minutes", 45))))
    cooldown_minutes = max(
        1,
        min(
            24 * 60,
            int(get_runtime_int(repo, "ebay_user_token_auto_refresh_failure_cooldown_minutes", 30)),
        ),
    )

    access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    refresh_token = get_runtime_str(repo, "ebay_user_refresh_token", settings.ebay_user_refresh_token).strip()
    expires_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_expires_at", "").strip())
    refreshed_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_refreshed_at", "").strip())
    failed_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_refresh_failed_at", "").strip())
    last_error = str(get_runtime_str(repo, "ebay_user_access_token_refresh_last_error", "") or "").strip()

    due_reasons: list[str] = []
    if not access_token:
        due_reasons.append("missing_access_token")
    if expires_at is None and refreshed_at is None:
        due_reasons.append("missing_expiry_metadata")
    if expires_at is not None and (expires_at - now_utc_naive) <= timedelta(minutes=min_ttl_minutes):
        due_reasons.append("min_ttl_window")
    if refreshed_at is not None and (now_utc_naive - refreshed_at) >= timedelta(hours=interval_hours):
        due_reasons.append("interval_elapsed")

    next_due_at = None
    due_candidates: list[datetime] = []
    if refreshed_at is not None:
        due_candidates.append(refreshed_at + timedelta(hours=interval_hours))
    if expires_at is not None:
        due_candidates.append(expires_at - timedelta(minutes=min_ttl_minutes))
    if due_candidates:
        next_due_at = min(due_candidates)

    retry_at = None
    cooldown_active = False
    if failed_at is not None:
        retry_at = failed_at + timedelta(minutes=cooldown_minutes)
        cooldown_active = now_utc_naive < retry_at

    expires_in_minutes = None
    if expires_at is not None:
        expires_in_minutes = int((expires_at - now_utc_naive).total_seconds() // 60)

    if not enabled:
        status = "warn"
    elif not refresh_token:
        status = "error"
    elif cooldown_active:
        status = "error"
    elif due_reasons:
        status = "warn"
    else:
        status = "ok"

    return {
        "status": status,
        "enabled": enabled,
        "access_token_present": bool(access_token),
        "refresh_token_present": bool(refresh_token),
        "expires_at": expires_at,
        "expires_in_minutes": expires_in_minutes,
        "refreshed_at": refreshed_at,
        "failed_at": failed_at,
        "retry_at": retry_at,
        "cooldown_active": cooldown_active,
        "last_error": last_error,
        "due_reasons": due_reasons,
        "next_due_at": next_due_at,
        "interval_hours": interval_hours,
        "min_ttl_minutes": min_ttl_minutes,
        "cooldown_minutes": cooldown_minutes,
    }


def _normalized_fee_coverage_health_snapshot(
    repo,
    *,
    lookback_weeks: int,
    threshold_percent: float,
    min_consecutive_weeks: int,
) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start_dt = now - timedelta(days=max(2, int(lookback_weeks)) * 7)
    if not hasattr(repo, "report_ebay_fee_reconciliation_rows"):
        return {
            "triggered": False,
            "latest_week_start": "",
            "latest_week_coverage_pct": 0.0,
            "consecutive_below": 0,
            "weekly_rows": [],
            "error": "reconciliation_not_supported",
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

    weekly_rows: list[dict] = []
    for week_start in sorted(weekly.keys()):
        total = int(weekly[week_start]["total"])
        normalized = int(weekly[week_start]["normalized"])
        coverage_pct = (float(normalized) / float(total) * 100.0) if total > 0 else 0.0
        weekly_rows.append(
            {
                "week_start": week_start,
                "total_sales": total,
                "normalized_sales": normalized,
                "coverage_pct": round(coverage_pct, 2),
            }
        )

    consecutive_below = 0
    for week_row in reversed(weekly_rows):
        if float(week_row.get("coverage_pct") or 0.0) < float(threshold_percent):
            consecutive_below += 1
        else:
            break
    latest_week = weekly_rows[-1] if weekly_rows else {}
    return {
        "triggered": bool(consecutive_below >= max(1, int(min_consecutive_weeks))),
        "latest_week_start": str(latest_week.get("week_start") or ""),
        "latest_week_coverage_pct": float(latest_week.get("coverage_pct") or 0.0),
        "consecutive_below": int(consecutive_below),
        "threshold_percent": float(threshold_percent),
        "min_consecutive_weeks": int(min_consecutive_weeks),
        "weekly_rows": weekly_rows,
    }


def render_system_health(repo) -> None:
    user = current_user()
    st.subheader("System Health")
    render_help_panel(
        section_title="System Health",
        goal="Monitor app runtime, database, storage, integrations, and worker signals from one page.",
        steps=[
            "Review system runtime metrics (CPU/load, memory, disk).",
            "Verify DB/migration/app service checks are healthy.",
            "Validate integration readiness for eBay/S3/spot/AI runtime/sync runner.",
            "Use live integration check buttons when validating credentials/connectivity.",
        ],
        roadmap_phase="v0.5 AI Operations Copilot + Data Chat",
    )

    if not ensure_permission(user, "read", "View System Health"):
        st.stop()

    tz_name = get_runtime_str(repo, "app_default_timezone", app_timezone_name()).strip() or app_timezone_name()
    try:
        from zoneinfo import ZoneInfo

        local_now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        local_now = app_now()
        tz_name = app_timezone_name()
    st.caption(
        f"As of local ({tz_name}): {local_now.isoformat(timespec='seconds')} | "
        f"UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')} | env=`{settings.app_env}`"
    )
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    refresh = st.button("Refresh Health Snapshot", key="system_health_refresh")
    if refresh:
        st.rerun()

    integration_event_audit_cache: dict[tuple[str, int], list[tuple]] = {}

    def _integration_event_audit_rows(*, since: datetime, limit: int, label: str) -> list[tuple]:
        cache_key = (str(since.isoformat()), int(limit))
        cached_rows = integration_event_audit_cache.get(cache_key)
        if cached_rows is not None:
            return list(cached_rows)
        rows = _safe_select_all(
            repo,
            """
            SELECT created_at, actor, action, changes_json
            FROM audit_logs
            WHERE entity_type = 'integration_event'
              AND created_at >= :since
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            label=label,
            params={"since": since, "limit": int(limit)},
        )
        integration_event_audit_cache[cache_key] = list(rows)
        return list(rows)

    st.markdown("### Runtime")
    cpu_count = os.cpu_count() or 0
    load_avg = None
    try:
        load_avg = os.getloadavg()
    except Exception:
        load_avg = None
    mem_total_kb, mem_avail_kb = _read_proc_meminfo()
    rss_kb = _read_proc_rss_kb()
    root_disk = shutil.disk_usage("/")

    rt1, rt2, rt3, rt4 = st.columns(4)
    rt1.metric("CPU Cores", int(cpu_count))
    rt2.metric(
        "Load Avg (1m)",
        f"{load_avg[0]:.2f}" if load_avg else "n/a",
    )
    rt3.metric("Host Mem Total", _fmt_gb_from_kb(mem_total_kb))
    rt4.metric("Process RSS", _fmt_gb_from_kb(rss_kb))
    rt5, rt6 = st.columns(2)
    rt5.metric("Host Mem Available", _fmt_gb_from_kb(mem_avail_kb))
    rt6.metric("Root Disk Free", f"{root_disk.free / 1024 / 1024 / 1024:,.2f} GB")

    st.caption(
        f"Platform: `{platform.platform()}` | Python `{platform.python_version()}` | "
        f"App name: `{settings.app_name}`"
    )
    build_version = get_runtime_str(repo, "app_build_version", settings.app_build_version).strip() or "unknown"
    build_sha = get_runtime_str(repo, "app_build_sha", settings.app_build_sha).strip() or "unknown"
    build_sha_short = build_sha if build_sha in {"unknown", "n/a"} else build_sha[:12]
    rb1, rb2 = st.columns(2)
    rb1.metric("Build Version", build_version)
    rb2.metric("Build SHA", build_sha_short)

    st.markdown("### Service Checks")
    service_rows: list[dict] = []
    service_rows.append(
        _status_row(
            "Build Metadata",
            "ok" if build_version != "unknown" and build_sha != "unknown" else "warn",
            f"version={build_version} sha={build_sha}",
        )
    )

    db_ping = _safe_select_first(repo, "SELECT 1", label="Database ping")
    if db_ping is not None:
        service_rows.append(_status_row("Database", "ok", "SELECT 1 succeeded"))
    else:
        service_rows.append(_status_row("Database", "error", "SELECT 1 failed"))

    version_row = _safe_select_first(
        repo,
        "SELECT version_num FROM alembic_version LIMIT 1",
        label="Migration version",
    )
    if version_row:
        service_rows.append(_status_row("Migrations", "ok", f"alembic_version={version_row[0]}"))
    else:
        service_rows.append(_status_row("Migrations", "warn", "No alembic version row found"))

    sync_runner_enabled = get_runtime_bool(repo, "sync_runner_enabled", bool(settings.sync_runner_enabled))
    service_rows.append(
        _status_row(
            "Sync Runner",
            "ok" if sync_runner_enabled else "warn",
            (
                f"enabled={sync_runner_enabled} interval={settings.sync_runner_interval_seconds}s "
                f"actor={settings.sync_runner_actor}"
            ),
        )
    )

    st.dataframe(pd.DataFrame(service_rows), use_container_width=True)

    st.markdown("### DB Rollup Latency Baseline")
    st.caption("Run an on-demand baseline for dashboard/report DB rollup queries over a selected date window.")
    rollup_col1, rollup_col2, rollup_col3, rollup_col4 = st.columns(4)
    default_start = (local_now - timedelta(days=30)).date()
    default_end = local_now.date()
    with rollup_col1:
        rollup_start_date = st.date_input(
            "Rollup Start Date",
            value=default_start,
            key="system_health_rollup_start_date",
        )
    with rollup_col2:
        rollup_end_date = st.date_input(
            "Rollup End Date",
            value=default_end,
            key="system_health_rollup_end_date",
        )
    with rollup_col3:
        rollup_tax_rate = float(
            st.number_input(
                "Tax Rate %",
                min_value=0.0,
                max_value=30.0,
                value=7.5,
                step=0.1,
                key="system_health_rollup_tax_rate",
            )
        )
    with rollup_col4:
        rollup_shipping_taxable = bool(
            st.checkbox(
                "Shipping Taxable",
                value=True,
                key="system_health_rollup_shipping_taxable",
            )
        )
    rollup_explain_sample_limit = int(
        st.number_input(
            "Rollup EXPLAIN Sample Limit",
            min_value=100,
            max_value=10000,
            value=2000,
            step=100,
            key="system_health_rollup_explain_sample_limit",
            help="Row sample limit for heavy EXPLAIN baseline queries.",
        )
    )
    if rollup_start_date > rollup_end_date:
        st.warning("Rollup baseline date range is invalid: start date is after end date.")
    elif hasattr(repo, "collect_rollup_latency_baseline"):
        if st.button("Run Rollup Baseline", key="system_health_rollup_run_baseline"):
            start_dt = datetime(
                rollup_start_date.year,
                rollup_start_date.month,
                rollup_start_date.day,
                0,
                0,
                0,
            )
            end_dt = datetime(
                rollup_end_date.year,
                rollup_end_date.month,
                rollup_end_date.day,
                23,
                59,
                59,
            )
            try:
                baseline_rows = repo.collect_rollup_latency_baseline(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    tax_rate_percent=rollup_tax_rate,
                    shipping_taxable=rollup_shipping_taxable,
                )
                st.session_state["system_health_rollup_baseline_rows"] = baseline_rows
                st.success(f"Captured rollup baseline rows: {len(baseline_rows)}")
            except Exception as exc:
                st.error(f"Rollup baseline capture failed: {exc}")
        baseline_rows = st.session_state.get("system_health_rollup_baseline_rows")
        if isinstance(baseline_rows, list) and baseline_rows:
            baseline_df = pd.DataFrame(baseline_rows)
            st.dataframe(baseline_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Rollup Baseline CSV",
                data=baseline_df.to_csv(index=False),
                file_name=f"rollup_latency_baseline_{settings.app_env}.csv",
                mime="text/csv",
                key="system_health_rollup_baseline_download",
            )
        else:
            st.caption("No rollup baseline captured yet in this session.")

        if hasattr(repo, "collect_rollup_explain_baseline"):
            if st.button("Run Rollup EXPLAIN Snapshot", key="system_health_rollup_run_explain_snapshot"):
                start_dt = datetime(
                    rollup_start_date.year,
                    rollup_start_date.month,
                    rollup_start_date.day,
                    0,
                    0,
                    0,
                )
                end_dt = datetime(
                    rollup_end_date.year,
                    rollup_end_date.month,
                    rollup_end_date.day,
                    23,
                    59,
                    59,
                )
                try:
                    explain_rows = repo.collect_rollup_explain_baseline(
                        start_dt=start_dt,
                        end_dt=end_dt,
                        sample_limit=int(rollup_explain_sample_limit),
                    )
                    st.session_state["system_health_rollup_explain_rows"] = explain_rows
                    explain_failures = sum(1 for row in explain_rows if str(row.get("error") or "").strip())
                    if explain_failures:
                        failure_names = [
                            str(row.get("rollup_name") or "").strip()
                            for row in explain_rows
                            if str(row.get("error") or "").strip()
                        ]
                        failure_names = [name for name in failure_names if name]
                        failure_hint = ", ".join(failure_names[:5]) if failure_names else "unknown probe"
                        if len(failure_names) > 5:
                            failure_hint = f"{failure_hint}, ..."
                        st.warning(
                            f"Captured rollup EXPLAIN rows: {len(explain_rows)} "
                            f"(with {explain_failures} probe error(s): {failure_hint})."
                        )
                    else:
                        st.success(f"Captured rollup EXPLAIN rows: {len(explain_rows)}")
                    try:
                        execution_values = [float(row.get("execution_ms") or 0.0) for row in explain_rows]
                        max_execution_ms = max(execution_values) if execution_values else 0.0
                        avg_execution_ms = (
                            float(sum(execution_values) / len(execution_values))
                            if execution_values
                            else 0.0
                        )
                        slowest_row = (
                            max(explain_rows, key=lambda row: float(row.get("execution_ms") or 0.0))
                            if explain_rows
                            else {}
                        )
                        repo.log_integration_event(
                            actor=user.username,
                            integration="system_health",
                            action="rollup_explain_baseline_snapshot",
                            status="success",
                            details={
                                "environment": settings.app_env,
                                "window_start": start_dt.isoformat(),
                                "window_end": end_dt.isoformat(),
                                "rows_captured": int(len(explain_rows)),
                                "sample_limit": int(rollup_explain_sample_limit),
                                "avg_execution_ms": round(avg_execution_ms, 3),
                                "max_execution_ms": round(max_execution_ms, 3),
                                "slowest_rollup_name": str(slowest_row.get("rollup_name") or ""),
                            },
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    try:
                        repo.db.rollback()
                    except Exception:
                        pass
                    st.error(f"Rollup EXPLAIN snapshot failed: {exc}")
            explain_rows = st.session_state.get("system_health_rollup_explain_rows")
            if isinstance(explain_rows, list) and explain_rows:
                explain_failures = _rollup_explain_failures(explain_rows)
                explain_skips = _rollup_explain_skips(explain_rows)
                if explain_failures:
                    st.markdown("#### EXPLAIN Probe Failures")
                    f1, f2 = st.columns(2)
                    f1.metric("Failed Probes", int(len(explain_failures)))
                    f2.metric(
                        "Failure Rate",
                        f"{(100.0 * float(len(explain_failures)) / float(len(explain_rows))):.1f}%",
                    )
                    st.dataframe(
                        pd.DataFrame(explain_failures),
                        use_container_width=True,
                        hide_index=True,
                    )
                    st.caption("Inspect/resolve failed probes before using this snapshot as full performance evidence.")
                if explain_skips:
                    st.markdown("#### EXPLAIN Probe Skips")
                    st.info(
                        f"{len(explain_skips)} probe(s) were intentionally skipped due to missing optional tables in this environment."
                    )
                    st.dataframe(
                        pd.DataFrame(explain_skips),
                        use_container_width=True,
                        hide_index=True,
                    )
                explain_df = pd.DataFrame(explain_rows)
                preview_cols = [
                    col
                    for col in [
                        "rollup_name",
                        "elapsed_ms",
                        "planning_ms",
                        "execution_ms",
                        "plan_lines",
                        "sample_limit",
                        "window_start",
                        "window_end",
                        "error",
                        "skipped",
                        "skip_reason",
                    ]
                    if col in explain_df.columns
                ]
                st.dataframe(explain_df[preview_cols], use_container_width=True, hide_index=True)
                selected_rollup = st.selectbox(
                    "Inspect EXPLAIN Plan",
                    options=[str(row.get("rollup_name") or "") for row in explain_rows],
                    index=next(
                        (
                            idx
                            for idx, row in enumerate(explain_rows)
                            if str(row.get("error") or "").strip()
                        ),
                        0,
                    ),
                    key="system_health_rollup_explain_selected_rollup",
                )
                selected_plan = next(
                    (
                        str(row.get("plan_text") or "")
                        for row in explain_rows
                        if str(row.get("rollup_name") or "") == str(selected_rollup or "")
                    ),
                    "",
                )
                if selected_plan:
                    st.text_area(
                        "EXPLAIN Plan (text)",
                        value=selected_plan,
                        height=280,
                        key="system_health_rollup_explain_plan_text_view",
                    )
                st.download_button(
                    "Download Rollup EXPLAIN CSV",
                    data=explain_df.to_csv(index=False),
                    file_name=f"rollup_explain_baseline_{settings.app_env}.csv",
                    mime="text/csv",
                    key="system_health_rollup_explain_download",
                )
            else:
                st.caption("No rollup EXPLAIN snapshot captured yet in this session.")

            st.markdown("#### Recent Rollup EXPLAIN Snapshots (14d)")
            explain_since = now_utc_naive - timedelta(days=14)
            explain_audit_rows = _integration_event_audit_rows(
                since=explain_since,
                limit=1000,
                label="Rollup EXPLAIN snapshots",
            )
            recent_explain_rows: list[dict] = []
            for created_at, actor, _audit_action, changes_json in explain_audit_rows:
                try:
                    payload = json.loads(str(changes_json or "{}"))
                except Exception:
                    payload = {}
                after = payload.get("after") if isinstance(payload, dict) else {}
                if not isinstance(after, dict):
                    continue
                if str(after.get("integration") or "").strip().lower() != "system_health":
                    continue
                if str(after.get("action") or "").strip().lower() != "rollup_explain_baseline_snapshot":
                    continue
                if str(after.get("environment") or "").strip() != settings.app_env:
                    continue
                recent_explain_rows.append(
                    {
                        "created_at": created_at,
                        "actor": str(actor or "").strip(),
                        "window_start": str(after.get("window_start") or ""),
                        "window_end": str(after.get("window_end") or ""),
                        "rows_captured": int(after.get("rows_captured") or 0),
                        "sample_limit": int(after.get("sample_limit") or 0),
                        "avg_execution_ms": float(after.get("avg_execution_ms") or 0.0),
                        "max_execution_ms": float(after.get("max_execution_ms") or 0.0),
                        "slowest_rollup_name": str(after.get("slowest_rollup_name") or ""),
                    }
                )
            if recent_explain_rows:
                recent_explain_df = pd.DataFrame(recent_explain_rows)
                recent_explain_df = recent_explain_df.sort_values(
                    "created_at",
                    ascending=False,
                    kind="stable",
                )
                top_slowest = recent_explain_df.sort_values(
                    "max_execution_ms",
                    ascending=False,
                    kind="stable",
                ).head(1)
                s1, s2, s3 = st.columns(3)
                s1.metric("Snapshots (14d)", int(len(recent_explain_df.index)))
                s2.metric(
                    "Worst Max Exec (ms)",
                    f"{float(recent_explain_df['max_execution_ms'].max()):,.1f}",
                )
                s3.metric(
                    "Top Slowest Rollup",
                    str(top_slowest.iloc[0]["slowest_rollup_name"]) if not top_slowest.empty else "n/a",
                )
                st.dataframe(recent_explain_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Recent Rollup EXPLAIN Snapshot CSV",
                    data=recent_explain_df.to_csv(index=False),
                    file_name=f"rollup_explain_snapshot_history_{settings.app_env}.csv",
                    mime="text/csv",
                    key="system_health_rollup_explain_snapshot_history_download",
                )
            else:
                st.caption("No recent rollup EXPLAIN snapshots recorded in audit history.")
    else:
        st.caption("Repository does not expose rollup baseline diagnostics in this environment.")

    st.markdown("### Page/Read Latency Baseline")
    st.caption(
        "Run an on-demand baseline for page-critical repository reads (dashboard/list/report probes). "
        "Enable heavy probes only when you want full-load and extended-report reference timings. "
        "Integration probes are optional and target Admin Integrations queue/event surfaces."
    )
    include_heavy_list_reads = st.checkbox(
        "Include heavy probes (products/listings/orders + extended report reconciliation)",
        value=False,
        key="system_health_page_baseline_include_heavy_reads",
    )
    include_integrations_reads = st.checkbox(
        "Include integration probes (Slack/Google queues + integration-event history reads)",
        value=False,
        key="system_health_page_baseline_include_integrations_reads",
    )
    if rollup_start_date > rollup_end_date:
        st.warning("Page/read baseline date range is invalid: start date is after end date.")
    elif hasattr(repo, "collect_page_latency_baseline"):
        if st.button("Run Page/Read Baseline", key="system_health_page_run_baseline"):
            start_dt = datetime(
                rollup_start_date.year,
                rollup_start_date.month,
                rollup_start_date.day,
                0,
                0,
                0,
            )
            end_dt = datetime(
                rollup_end_date.year,
                rollup_end_date.month,
                rollup_end_date.day,
                23,
                59,
                59,
            )
            try:
                baseline_rows = repo.collect_page_latency_baseline(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    tax_rate_percent=rollup_tax_rate,
                    shipping_taxable=rollup_shipping_taxable,
                    include_heavy_list_reads=bool(include_heavy_list_reads),
                    include_integrations_reads=bool(include_integrations_reads),
                )
                st.session_state["system_health_page_baseline_rows"] = baseline_rows
                st.success(f"Captured page/read baseline rows: {len(baseline_rows)}")
                try:
                    max_elapsed_ms = (
                        max(float(row.get("elapsed_ms") or 0.0) for row in baseline_rows)
                        if baseline_rows
                        else 0.0
                    )
                    avg_elapsed_ms = (
                        float(sum(float(row.get("elapsed_ms") or 0.0) for row in baseline_rows) / len(baseline_rows))
                        if baseline_rows
                        else 0.0
                    )
                    repo.log_integration_event(
                        actor=user.username,
                        integration="system_health",
                        action="page_latency_baseline_snapshot",
                        status="success",
                        details={
                            "environment": settings.app_env,
                            "window_start": start_dt.isoformat(),
                            "window_end": end_dt.isoformat(),
                            "rows_captured": int(len(baseline_rows)),
                            "include_heavy_list_reads": bool(include_heavy_list_reads),
                            "include_integrations_reads": bool(include_integrations_reads),
                            "avg_elapsed_ms": round(avg_elapsed_ms, 3),
                            "max_elapsed_ms": round(max_elapsed_ms, 3),
                        },
                    )
                except Exception:
                    pass
            except Exception as exc:
                st.error(f"Page/read baseline capture failed: {exc}")
        page_baseline_rows = st.session_state.get("system_health_page_baseline_rows")
        if isinstance(page_baseline_rows, list) and page_baseline_rows:
            normalized_rows = _normalize_page_baseline_rows(repo, page_baseline_rows)
            page_baseline_df = pd.DataFrame(normalized_rows)
            if "elapsed_ms" in page_baseline_df.columns:
                page_baseline_df = page_baseline_df.sort_values("elapsed_ms", ascending=False, kind="stable")

            summary = _page_baseline_summary(normalized_rows)
            over_budget_count = int(summary.get("over_budget_count") or 0)
            total_count = int(summary.get("total_count") or 0)
            p1, p2, p3 = st.columns(3)
            p1.metric("Probes Captured", total_count)
            p2.metric("Over Budget", over_budget_count)
            p3.metric(
                "Worst Probe (ms)",
                f"{float(summary.get('worst_elapsed_ms') or 0.0):,.1f}",
            )
            integration_probe_mask = page_baseline_df["probe_name"].astype(str).str.startswith(
                ("list_integration_queue_jobs_", "integration_event_rows_")
            ) if "probe_name" in page_baseline_df.columns else pd.Series([], dtype=bool)
            integration_probe_count = int(integration_probe_mask.sum()) if len(integration_probe_mask.index) else 0
            if integration_probe_count > 0:
                integration_df = page_baseline_df[integration_probe_mask]
                i1, i2, i3 = st.columns(3)
                i1.metric("Integration Probes", integration_probe_count)
                i2.metric(
                    "Integration Over Budget",
                    int(integration_df["over_budget"].sum()) if "over_budget" in integration_df.columns else 0,
                )
                i3.metric(
                    "Worst Integration Probe (ms)",
                    f"{float(integration_df['elapsed_ms'].max()):,.1f}"
                    if not integration_df.empty and "elapsed_ms" in integration_df.columns
                    else "0.0",
                )
            if over_budget_count > 0:
                st.warning(
                    f"{over_budget_count} probe(s) exceeded configured latency budget. "
                    "Tune query/render paths or adjust realistic budgets via runtime keys "
                    "(`perf_budget_<probe_name>_ms`)."
                )
            with st.expander("Slowest Probes (Top 5)", expanded=False):
                slowest_df = page_baseline_df.head(5)[
                    [c for c in ["probe_name", "elapsed_ms", "budget_ms", "over_budget", "result_count"] if c in page_baseline_df.columns]
                ]
                st.dataframe(slowest_df, use_container_width=True, hide_index=True)
            st.dataframe(page_baseline_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Page/Read Baseline CSV",
                data=page_baseline_df.to_csv(index=False),
                file_name=f"page_latency_baseline_{settings.app_env}.csv",
                mime="text/csv",
                key="system_health_page_baseline_download",
            )
        else:
            st.caption("No page/read baseline captured yet in this session.")

        st.markdown("#### Recent Page/Read Baseline Snapshots (14d)")
        baseline_since = now_utc_naive - timedelta(days=14)
        baseline_audit_rows = _integration_event_audit_rows(
            since=baseline_since,
            limit=1000,
            label="Page/read baseline snapshots",
        )
        snapshot_rows: list[dict] = []
        for created_at, actor, _audit_action, changes_json in baseline_audit_rows:
            try:
                payload = json.loads(str(changes_json or "{}"))
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                continue
            if str(after.get("integration") or "").strip().lower() != "system_health":
                continue
            if str(after.get("action") or "").strip().lower() != "page_latency_baseline_snapshot":
                continue
            if str(after.get("environment") or "").strip() != settings.app_env:
                continue
            details = after.get("details") if isinstance(after.get("details"), dict) else {}
            snapshot_rows.append(
                {
                    "captured_at": str(created_at or ""),
                    "actor": str(actor or ""),
                    "status": str(after.get("status") or ""),
                    "rows_captured": int(details.get("rows_captured") or 0),
                    "avg_elapsed_ms": round(float(details.get("avg_elapsed_ms") or 0.0), 3),
                    "max_elapsed_ms": round(float(details.get("max_elapsed_ms") or 0.0), 3),
                    "include_heavy_list_reads": bool(details.get("include_heavy_list_reads")),
                    "window_start": str(details.get("window_start") or ""),
                    "window_end": str(details.get("window_end") or ""),
                }
            )
        if snapshot_rows:
            snapshots_df = pd.DataFrame(snapshot_rows)
            s1, s2, s3 = st.columns(3)
            s1.metric("Snapshots (14d)", int(len(snapshots_df.index)))
            s2.metric("Latest Avg Elapsed (ms)", f"{float(snapshots_df.iloc[0]['avg_elapsed_ms']):,.1f}")
            s3.metric("Latest Max Elapsed (ms)", f"{float(snapshots_df.iloc[0]['max_elapsed_ms']):,.1f}")
            st.dataframe(snapshots_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Snapshot History CSV",
                data=snapshots_df.to_csv(index=False),
                file_name=f"page_latency_snapshots_{settings.app_env}.csv",
                mime="text/csv",
                key="system_health_page_baseline_snapshot_download",
            )
        else:
            st.caption("No page/read baseline snapshots recorded yet in last 14 days.")
    else:
        st.caption("Repository does not expose page/read baseline diagnostics in this environment.")

    st.markdown("### Notification Outbox")
    try:
        outbox_rows = repo.list_notification_outbox(
            environment=settings.app_env,
            statuses={"queued", "retrying", "processing", "failed", "sent"},
            limit=500,
        )
    except Exception:
        outbox_rows = []
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    status_counts: dict[str, int] = {"queued": 0, "retrying": 0, "processing": 0, "failed": 0, "sent": 0}
    due_count = 0
    for row in outbox_rows:
        status = str(getattr(row, "status", "") or "").strip().lower()
        if status in status_counts:
            status_counts[status] += 1
        next_attempt_at = getattr(row, "next_attempt_at", None)
        if status in {"queued", "retrying"} and (next_attempt_at is None or next_attempt_at <= now_naive):
            due_count += 1
    no1, no2, no3, no4 = st.columns(4)
    no1.metric("Outbox Due", int(due_count))
    no2.metric("Outbox Retrying", int(status_counts["retrying"]))
    no3.metric("Outbox Failed", int(status_counts["failed"]))
    no4.metric("Outbox Sent (window)", int(status_counts["sent"]))
    if outbox_rows:
        preview_rows = []
        for row in outbox_rows[:50]:
            preview_rows.append(
                {
                    "id": int(getattr(row, "id", 0) or 0),
                    "status": str(getattr(row, "status", "") or ""),
                    "channel": str(getattr(row, "channel", "") or ""),
                    "event_type": str(getattr(row, "event_type", "") or ""),
                    "attempt_count": int(getattr(row, "attempt_count", 0) or 0),
                    "max_attempts": int(getattr(row, "max_attempts", 0) or 0),
                    "next_attempt_at": str(getattr(row, "next_attempt_at", "") or ""),
                    "last_error": str(getattr(row, "last_error", "") or "")[:140],
                }
            )
        st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No notification outbox rows found for current environment.")

    st.markdown("#### Outbox Runner Activity")
    outbox_since = now_utc_naive - timedelta(days=14)
    outbox_audit_rows = _integration_event_audit_rows(
        since=outbox_since,
        limit=2000,
        label="Outbox runner activity",
    )
    outbox_activity_rows: list[tuple] = []
    for created_at, actor, _audit_action, changes_json in outbox_audit_rows:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        if str(after.get("environment") or "").strip() != settings.app_env:
            continue
        if str(after.get("integration") or "").strip() != "notification_outbox":
            continue
        action = str(after.get("action") or "").strip()
        if action not in {"process_due", "cleanup", "manual_process_due", "manual_cleanup"}:
            continue
        status = str(after.get("status") or "").strip()
        outbox_activity_rows.append((created_at, actor, action, status, changes_json))
    outbox_activity_rows.sort(key=lambda row: str(row[0] or ""), reverse=True)
    outbox_activity_rows = outbox_activity_rows[:200]
    latest_by_action: dict[str, dict[str, str]] = {}
    for created_at, actor, action, status, changes_json in outbox_activity_rows:
        action_key = str(action or "").strip().lower()
        if action_key in latest_by_action:
            continue
        try:
            details_payload = json.loads(str(changes_json or "{}"))
        except Exception:
            details_payload = {}
        details = details_payload if isinstance(details_payload, dict) else {}
        latest_by_action[action_key] = {
            "created_at": str(created_at or ""),
            "actor": str(actor or ""),
            "status": str(status or ""),
            "details": json.dumps(details, ensure_ascii=False)[:220] if details else "",
        }
    process_run = (
        latest_by_action.get("manual_process_due")
        or latest_by_action.get("process_due")
        or {}
    )
    cleanup_run = (
        latest_by_action.get("manual_cleanup")
        or latest_by_action.get("cleanup")
        or {}
    )
    ra1, ra2 = st.columns(2)
    with ra1:
        st.metric("Last Outbox Process Run", str(process_run.get("created_at") or "never"))
        if process_run:
            st.caption(
                f"status={process_run.get('status') or ''} actor={process_run.get('actor') or ''}"
            )
            if process_run.get("details"):
                st.code(str(process_run.get("details") or ""), language="json")
    with ra2:
        st.metric("Last Outbox Cleanup Run", str(cleanup_run.get("created_at") or "never"))
        if cleanup_run:
            st.caption(
                f"status={cleanup_run.get('status') or ''} actor={cleanup_run.get('actor') or ''}"
            )
            if cleanup_run.get("details"):
                st.code(str(cleanup_run.get("details") or ""), language="json")

    st.markdown("### Slack Ops Queue Health")
    try:
        slack_ops_rows = repo.list_integration_queue_jobs(
            environment=settings.app_env,
            integration="slack_ops",
            statuses={"queued", "running", "blocked", "failed", "success"},
            limit=500,
        )
    except Exception:
        slack_ops_rows = []
    slack_ops_snapshot = _slack_ops_health_snapshot(slack_ops_rows, now=now_naive)
    so1, so2, so3, so4, so5, so6 = st.columns(6)
    so1.metric("Slack Ops Total", int(slack_ops_snapshot["total_count"]))
    so2.metric("Slack Ops Due", int(slack_ops_snapshot["due_count"]))
    so3.metric("Slack Ops Blocked", int(slack_ops_snapshot["blocked_count"]))
    so4.metric("Slack Ops Failed", int(slack_ops_snapshot["failed_count"]))
    so5.metric("Pending Approvals", int(slack_ops_snapshot["pending_approval_count"]))
    so6.metric("Approval SLA Max (h)", f"{float(slack_ops_snapshot['pending_approval_max_hours']):.2f}")

    slack_ops_events_24h = _integration_event_audit_rows(
        since=now_utc_naive - timedelta(hours=24),
        limit=2000,
        label="Slack ops 24h integration events",
    )
    slack_ops_event_totals = {"success": 0, "queued": 0, "failed": 0, "rejected": 0}
    for _created_at, _actor, _audit_action, changes_json in slack_ops_events_24h:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        if str(after.get("environment") or "").strip() != settings.app_env:
            continue
        if str(after.get("integration") or "").strip().lower() != "slack_ops":
            continue
        status = str(after.get("status") or "").strip().lower()
        action_name = str(after.get("action") or "").strip().lower()
        if status == "success":
            slack_ops_event_totals["success"] += 1
        elif status == "queued":
            slack_ops_event_totals["queued"] += 1
        elif status in {"failed", "error"}:
            slack_ops_event_totals["failed"] += 1
        if action_name.endswith("_rejected"):
            slack_ops_event_totals["rejected"] += 1
    se1, se2, se3, se4 = st.columns(4)
    se1.metric("Slack Ops Events 24h: Success", int(slack_ops_event_totals["success"]))
    se2.metric("Slack Ops Events 24h: Queued", int(slack_ops_event_totals["queued"]))
    se3.metric("Slack Ops Events 24h: Failed", int(slack_ops_event_totals["failed"]))
    se4.metric("Slack Ops Events 24h: Rejected", int(slack_ops_event_totals["rejected"]))

    if slack_ops_rows:
        preview_rows: list[dict[str, Any]] = []
        for row in slack_ops_rows[:50]:
            payload = {}
            try:
                payload_raw = json.loads(str(getattr(row, "payload_json", "") or "{}"))
                if isinstance(payload_raw, dict):
                    payload = payload_raw
            except Exception:
                payload = {}
            command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
            approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
            preview_rows.append(
                {
                    "id": int(getattr(row, "id", 0) or 0),
                    "status": str(getattr(row, "status", "") or ""),
                    "intent": str(command.get("intent") or ""),
                    "requested_by": str(getattr(row, "requested_by", "") or ""),
                    "approval_status": str(approval.get("status") or ""),
                    "next_attempt_at": str(getattr(row, "next_attempt_at", "") or ""),
                    "last_error": str(getattr(row, "last_error", "") or "")[:140],
                }
            )
        st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No Slack Ops queue rows found for current environment.")

    st.markdown("### Error Signals (24h)")
    integration_error_rows: list[dict] = []
    recent_events_since = now_utc_naive - timedelta(hours=24)
    recent_events = _integration_event_audit_rows(
        since=recent_events_since,
        limit=2000,
        label="Integration event errors (24h)",
    )
    queue_exec_exceptions = 0
    terminal_failures = 0
    warnings = 0
    recent_error_samples: list[dict[str, str]] = []
    for created_at, actor, action, changes_json in recent_events:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            after = {}
        status = str(after.get("status") or "").strip().lower()
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        if status == "warning":
            warnings += 1
        if status == "failed":
            terminal_failures += 1
        if action and str(action).endswith("_execute_exception"):
            queue_exec_exceptions += 1
        if status in {"error", "failed", "warning"} and len(recent_error_samples) < 15:
            recent_error_samples.append(
                {
                    "created_at": str(created_at or ""),
                    "actor": str(actor or ""),
                    "action": str(action or ""),
                    "status": status,
                    "integration": str(after.get("integration") or ""),
                    "error": str(details.get("error") or "")[:180],
                }
            )
    es1, es2, es3 = st.columns(3)
    es1.metric("Queue Execute Exceptions", queue_exec_exceptions)
    es2.metric("Terminal Queue Failures", terminal_failures)
    es3.metric("Integration Warnings", warnings)
    qex_warn = max(1, get_runtime_int(repo, "health_queue_execute_exceptions_warn_24h", 1))
    qex_critical = max(qex_warn, get_runtime_int(repo, "health_queue_execute_exceptions_critical_24h", 5))
    tqf_warn = max(1, get_runtime_int(repo, "health_terminal_queue_failures_warn_24h", 1))
    tqf_critical = max(tqf_warn, get_runtime_int(repo, "health_terminal_queue_failures_critical_24h", 3))
    iw_warn = max(1, get_runtime_int(repo, "health_integration_warnings_warn_24h", 10))
    iw_critical = max(iw_warn, get_runtime_int(repo, "health_integration_warnings_critical_24h", 30))
    runbook_qex = get_runtime_str(repo, "runbook_queue_execute_exceptions_url", "").strip()
    runbook_tqf = get_runtime_str(repo, "runbook_terminal_queue_failures_url", "").strip()
    runbook_iw = get_runtime_str(repo, "runbook_integration_warnings_url", "").strip()

    def _threshold_state(value: int, warn_threshold: int, critical_threshold: int) -> str:
        if value >= critical_threshold:
            return "error"
        if value >= warn_threshold:
            return "warn"
        return "ok"

    queue_execute_state = _threshold_state(queue_exec_exceptions, qex_warn, qex_critical)
    terminal_failure_state = _threshold_state(terminal_failures, tqf_warn, tqf_critical)
    integration_warning_state = _threshold_state(warnings, iw_warn, iw_critical)

    integration_error_rows.append(
        _status_row(
            "Queue Execute Exceptions (24h)",
            queue_execute_state,
            (
                f"count={queue_exec_exceptions} warn>={qex_warn} critical>={qex_critical} "
                f"runbook={runbook_qex or 'not_set'}"
            ),
        )
    )
    integration_error_rows.append(
        _status_row(
            "Terminal Queue Failures (24h)",
            terminal_failure_state,
            (
                f"count={terminal_failures} warn>={tqf_warn} critical>={tqf_critical} "
                f"runbook={runbook_tqf or 'not_set'}"
            ),
        )
    )
    integration_error_rows.append(
        _status_row(
            "Integration Warnings (24h)",
            integration_warning_state,
            (
                f"count={warnings} warn>={iw_warn} critical>={iw_critical} "
                f"runbook={runbook_iw or 'not_set'}"
            ),
        )
    )
    st.dataframe(pd.DataFrame(integration_error_rows), use_container_width=True, hide_index=True)
    if recent_error_samples:
        st.caption("Recent integration warnings/errors")
        st.dataframe(pd.DataFrame(recent_error_samples), use_container_width=True, hide_index=True)
    else:
        st.caption("No integration warnings/errors in last 24h.")

    st.markdown("#### eBay Fee Coverage Health")
    st.caption(
        "Operational view of normalized eBay fee-source coverage used by daily Slack report alerting."
    )
    fee_lookback_weeks = max(
        2,
        int(get_runtime_int(repo, "slack_daily_report_normalized_fee_coverage_lookback_weeks", 8)),
    )
    fee_threshold_pct = max(
        0.0,
        min(
            100.0,
            float(get_runtime_float(repo, "slack_daily_report_normalized_fee_coverage_threshold_pct", 80.0)),
        ),
    )
    fee_consecutive_weeks = max(
        1,
        int(get_runtime_int(repo, "slack_daily_report_normalized_fee_coverage_consecutive_weeks", 2)),
    )
    try:
        fee_health = _normalized_fee_coverage_health_snapshot(
            repo,
            lookback_weeks=fee_lookback_weeks,
            threshold_percent=fee_threshold_pct,
            min_consecutive_weeks=fee_consecutive_weeks,
        )
        fh1, fh2, fh3, fh4 = st.columns(4)
        fh1.metric("Latest Coverage", f"{float(fee_health.get('latest_week_coverage_pct') or 0.0):.2f}%")
        fh2.metric("Week Start", str(fee_health.get("latest_week_start") or "-"))
        fh3.metric("Consecutive Below", int(fee_health.get("consecutive_below") or 0))
        fh4.metric("Alert", "yes" if bool(fee_health.get("triggered")) else "no")
        if bool(fee_health.get("triggered")):
            st.warning(
                "Fee coverage alert condition is active: "
                f"threshold={fee_threshold_pct:.2f}% over {fee_consecutive_weeks} consecutive week(s)."
            )
        weekly_rows = fee_health.get("weekly_rows") or []
        if weekly_rows:
            st.dataframe(pd.DataFrame(weekly_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No eBay fee reconciliation rows found for configured lookback window.")
    except Exception as exc:
        st.warning(f"Unable to compute eBay fee coverage health: {exc}")

    st.markdown("#### Remediation Runbooks")
    rb_cols = st.columns(3)
    with rb_cols[0]:
        if runbook_qex:
            st.link_button("Queue Execute Exceptions Runbook", runbook_qex)
        else:
            st.caption("Queue execute runbook URL not configured.")
    with rb_cols[1]:
        if runbook_tqf:
            st.link_button("Terminal Queue Failures Runbook", runbook_tqf)
        else:
            st.caption("Terminal failure runbook URL not configured.")
    with rb_cols[2]:
        if runbook_iw:
            st.link_button("Integration Warnings Runbook", runbook_iw)
        else:
            st.caption("Integration warnings runbook URL not configured.")

    critical_signals: list[str] = []
    if queue_execute_state == "error":
        critical_signals.append("queue_execute_exceptions")
    if terminal_failure_state == "error":
        critical_signals.append("terminal_queue_failures")
    if integration_warning_state == "error":
        critical_signals.append("integration_warnings")

    st.markdown("#### Critical Alert Validation")
    st.caption(
        "Use this to validate Slack routing/template for System Health critical alerts before go-live."
    )
    if st.button("Send Critical Health Alert Now", key="system_health_send_critical_alert_now_btn"):
        try:
            preview_signals = critical_signals or ["manual_validation"]
            alert_text = build_slack_alert_text(
                repo,
                event_type="system_health_critical",
                default_template=(
                    ":rotating_light: *GoldenStackers* System Health critical signals detected\n"
                    "- Env: `{env}`\n"
                    "- Critical Signals: `{critical_signals}`\n"
                    "- Queue Execute Exceptions: `{queue_execute_exceptions}`\n"
                    "- Terminal Queue Failures: `{terminal_queue_failures}`\n"
                    "- Integration Warnings: `{integration_warnings}`"
                ),
                context={
                    "env": settings.app_env,
                    "critical_signals": ", ".join(preview_signals),
                    "queue_execute_exceptions": queue_exec_exceptions,
                    "terminal_queue_failures": terminal_failures,
                    "integration_warnings": warnings,
                },
            )
            result = dispatch_slack_alert(
                repo,
                actor=user.username,
                event_type="system_health_critical",
                severity="critical",
                text=alert_text,
            )
            repo.log_integration_event(
                actor=user.username,
                integration="system_health",
                action="critical_signal_alert_manual",
                status="success" if str(result.get("status") or "") == "sent" else "queued",
                details={
                    "critical_signals": preview_signals,
                    "queue_execute_exceptions": int(queue_exec_exceptions),
                    "terminal_queue_failures": int(terminal_failures),
                    "integration_warnings": int(warnings),
                    "dispatch_result": result,
                },
            )
            if str(result.get("status") or "") == "sent":
                st.success(f"Critical health alert sent to `{result.get('channel', '')}`.")
            else:
                st.warning(f"Alert queued for delivery (queue_job_id={result.get('queue_job_id')}).")
        except Exception as exc:
            st.error(f"Unable to send critical health alert: {exc}")

    auto_alert_enabled = get_runtime_bool(repo, "health_auto_alert_critical_enabled", False)
    auto_alert_cooldown_minutes = max(
        5,
        min(24 * 60, get_runtime_int(repo, "health_auto_alert_cooldown_minutes", 60)),
    )
    notify_health_critical = get_runtime_bool(repo, "slack_notify_system_health_critical", False)
    if critical_signals and auto_alert_enabled and notify_health_critical:
        cooldown_since = now_utc_naive - timedelta(
            minutes=int(auto_alert_cooldown_minutes)
        )
        recently_alerted = False
        recent_health_alerts = _integration_event_audit_rows(
            since=cooldown_since,
            limit=200,
            label="Recent health-alert cooldown checks",
        )
        for _created_at, _actor, _audit_action, changes_json in recent_health_alerts:
            try:
                payload = json.loads(str(changes_json or "{}"))
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                continue
            if str(after.get("integration") or "").strip().lower() != "system_health":
                continue
            if str(after.get("action") or "").strip().lower() != "critical_signal_alert":
                continue
            details = after.get("details") if isinstance(after.get("details"), dict) else {}
            prior = {str(x) for x in (details.get("critical_signals") or [])}
            if prior == set(critical_signals):
                recently_alerted = True
                break

        if not recently_alerted:
            try:
                alert_text = build_slack_alert_text(
                    repo,
                    event_type="system_health_critical",
                    default_template=(
                        ":rotating_light: *GoldenStackers* System Health critical signals detected\n"
                        "- Env: `{env}`\n"
                        "- Critical Signals: `{critical_signals}`\n"
                        "- Queue Execute Exceptions: `{queue_execute_exceptions}`\n"
                        "- Terminal Queue Failures: `{terminal_queue_failures}`\n"
                        "- Integration Warnings: `{integration_warnings}`"
                    ),
                    context={
                        "env": settings.app_env,
                        "critical_signals": ", ".join(critical_signals),
                        "queue_execute_exceptions": queue_exec_exceptions,
                        "terminal_queue_failures": terminal_failures,
                        "integration_warnings": warnings,
                    },
                )
                dispatch_slack_alert(
                    repo,
                    actor=user.username,
                    event_type="system_health_critical",
                    severity="critical",
                    text=alert_text,
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="system_health",
                    action="critical_signal_alert",
                    status="warning",
                    details={
                        "critical_signals": critical_signals,
                        "cooldown_minutes": int(auto_alert_cooldown_minutes),
                        "queue_execute_exceptions": int(queue_exec_exceptions),
                        "terminal_queue_failures": int(terminal_failures),
                        "integration_warnings": int(warnings),
                    },
                )
            except Exception:
                pass

    load_health_audit_history = st.checkbox(
        "Load Critical Alert / Sign-Off History (slower)",
        value=False,
        key="system_health_load_audit_history",
        help="Defers heavy audit-log history queries until explicitly requested.",
    )

    st.markdown("#### Critical Alert Evidence (Recent)")
    if not load_health_audit_history:
        st.caption("Enable `Load Critical Alert / Sign-Off History` to query recent alert evidence.")
    else:
        recent_alert_since = now_utc_naive - timedelta(days=7)
        recent_alert_rows = _integration_event_audit_rows(
            since=recent_alert_since,
            limit=800,
            label="Critical alert evidence",
        )
        alert_evidence: list[dict[str, str]] = []
        for created_at, actor, action, changes_json in recent_alert_rows:
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
            alert_evidence.append(
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
                    "error": str(details.get("error") or "")[:180],
                }
            )
        if alert_evidence:
            alert_evidence_df = pd.DataFrame(alert_evidence[:200])
            st.dataframe(alert_evidence_df.head(100), use_container_width=True, hide_index=True)
            st.download_button(
                "Download Critical Alert Evidence CSV",
                data=alert_evidence_df.to_csv(index=False),
                file_name=f"critical_alert_evidence_{settings.app_env}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv",
                mime="text/csv",
                key="system_health_download_critical_alert_evidence_csv",
            )
        else:
            st.caption("No System Health critical alert evidence in last 7 days.")

    st.markdown("#### Threshold/Scoring Calibration Sign-Off")
    st.caption(
        "Record explicit environment sign-off after calibrating System Health thresholds and go-live readiness scoring."
    )
    with st.form("system_health_calibration_signoff_form"):
        sf1, sf2 = st.columns(2)
        with sf1:
            signoff_target_env = st.selectbox(
                "Sign-Off Environment",
                options=["dev", "prod"],
                index=0,
                key="system_health_calibration_signoff_target_env",
            )
            signoff_date = st.date_input(
                "Sign-Off Date",
                value=datetime.now(timezone.utc).date(),
                key="system_health_calibration_signoff_date",
            )
            signoff_owner = st.text_input(
                "Owner",
                value=str(user.username or ""),
                key="system_health_calibration_signoff_owner",
            )
        with sf2:
            signoff_status = st.selectbox(
                "Sign-Off Status",
                options=["approved", "blocked", "needs_followup"],
                index=0,
                key="system_health_calibration_signoff_status",
            )
            signoff_evidence_link = st.text_input(
                "Evidence Link",
                placeholder="runbook/ticket/dashboard link",
                key="system_health_calibration_signoff_evidence_link",
            )
        signoff_notes = st.text_area(
            "Sign-Off Notes",
            placeholder="Threshold changes, observed traffic window, alert owner confirmation.",
            key="system_health_calibration_signoff_notes",
        )
        save_signoff = st.form_submit_button("Record Calibration Sign-Off")
    if save_signoff:
        try:
            repo.record_audit_event(
                entity_type="system_health_calibration_signoff",
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
                    "queue_execute_thresholds": {
                        "warn": int(qex_warn),
                        "critical": int(qex_critical),
                    },
                    "terminal_failure_thresholds": {
                        "warn": int(tqf_warn),
                        "critical": int(tqf_critical),
                    },
                    "integration_warning_thresholds": {
                        "warn": int(iw_warn),
                        "critical": int(iw_critical),
                    },
                    "readiness_thresholds": {
                        "green": int(get_runtime_int(repo, "go_live_readiness_threshold_green", 85)),
                        "yellow": int(get_runtime_int(repo, "go_live_readiness_threshold_yellow", 65)),
                    },
                },
            )
            st.success("System Health calibration sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record calibration sign-off: {exc}")

    if not load_health_audit_history:
        st.caption("Enable `Load Critical Alert / Sign-Off History` to query calibration sign-off history.")
    else:
        calibration_logs = _safe_select_all(
            repo,
            """
            SELECT created_at, actor, changes_json
            FROM audit_logs
            WHERE entity_type = 'system_health_calibration_signoff'
            ORDER BY created_at DESC
            LIMIT 300
            """,
            label="Calibration sign-off audit",
        )
        calibration_rows: list[dict[str, str]] = []
        latest_status_by_env: dict[str, str] = {}
        for created_at, actor, changes_json in calibration_logs:
            try:
                payload = json.loads(str(changes_json or "{}"))
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            target_env = str(after.get("target_env") or "").strip().lower()
            status = str(after.get("status") or "").strip().lower()
            row = {
                "recorded_at": str(created_at or ""),
                "actor": str(actor or ""),
                "target_env": target_env,
                "signoff_date": str(after.get("signoff_date") or ""),
                "owner": str(after.get("owner") or ""),
                "status": status,
                "evidence_link": str(after.get("evidence_link") or ""),
                "notes": str(after.get("notes") or "")[:220],
            }
            calibration_rows.append(row)
            if target_env and target_env not in latest_status_by_env:
                latest_status_by_env[target_env] = status

        if calibration_rows:
            calibration_df = pd.DataFrame(calibration_rows)
            c1, c2 = st.columns(2)
            c1.metric("Dev Calibration Sign-Off", latest_status_by_env.get("dev") or "missing")
            c2.metric("Prod Calibration Sign-Off", latest_status_by_env.get("prod") or "missing")
            st.dataframe(calibration_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Calibration Sign-Off CSV",
                data=calibration_df.to_csv(index=False),
                file_name=f"system_health_calibration_signoff_{settings.app_env}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv",
                mime="text/csv",
                key="system_health_download_calibration_signoff_csv",
            )
        else:
            st.caption("No calibration sign-off records yet.")

    st.markdown("#### Alert Routing Acceptance Sign-Off")
    st.caption(
        "Record environment-level acceptance for alert routing ownership, channel targets, and escalation/runbook path."
    )
    with st.form("system_health_alert_routing_signoff_form"):
        ar1, ar2 = st.columns(2)
        with ar1:
            alert_target_env = st.selectbox(
                "Sign-Off Environment",
                options=["dev", "prod"],
                index=0,
                key="system_health_alert_routing_signoff_target_env",
            )
            alert_signoff_date = st.date_input(
                "Sign-Off Date",
                value=datetime.now(timezone.utc).date(),
                key="system_health_alert_routing_signoff_date",
            )
            alert_owner = st.text_input(
                "Owner",
                value=str(user.username or ""),
                key="system_health_alert_routing_signoff_owner",
            )
        with ar2:
            alert_status = st.selectbox(
                "Sign-Off Status",
                options=["approved", "blocked", "needs_followup"],
                index=0,
                key="system_health_alert_routing_signoff_status",
            )
            alert_evidence_link = st.text_input(
                "Evidence Link",
                placeholder="ticket/runbook/chatops thread",
                key="system_health_alert_routing_signoff_evidence_link",
            )
        ar3, ar4 = st.columns(2)
        with ar3:
            alert_channel_confirmed = st.checkbox("Slack channel routing confirmed", value=True)
            alert_owner_confirmed = st.checkbox("On-call/owner confirmed", value=True)
        with ar4:
            alert_escalation_confirmed = st.checkbox("Escalation path tested", value=True)
            alert_runbook_confirmed = st.checkbox("Runbook link validated", value=True)
        alert_notes = st.text_area(
            "Alert Routing Notes",
            placeholder="Who owns alerts, where they route, escalation timing, and unresolved gaps.",
            key="system_health_alert_routing_signoff_notes",
        )
        save_alert_signoff = st.form_submit_button("Record Alert Routing Sign-Off")
    if save_alert_signoff:
        try:
            repo.record_audit_event(
                entity_type="system_health_alert_routing_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "target_env": str(alert_target_env or "").strip().lower(),
                    "signoff_date": str(alert_signoff_date.isoformat()),
                    "owner": str(alert_owner or "").strip(),
                    "status": str(alert_status or "").strip().lower(),
                    "evidence_link": str(alert_evidence_link or "").strip(),
                    "channel_routing_confirmed": bool(alert_channel_confirmed),
                    "owner_confirmed": bool(alert_owner_confirmed),
                    "escalation_confirmed": bool(alert_escalation_confirmed),
                    "runbook_confirmed": bool(alert_runbook_confirmed),
                    "notes": str(alert_notes or "").strip(),
                },
            )
            st.success("Alert routing sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record alert routing sign-off: {exc}")

    if not load_health_audit_history:
        st.caption("Enable `Load Critical Alert / Sign-Off History` to query alert routing sign-off history.")
    else:
        alert_signoff_logs = _safe_select_all(
            repo,
            """
            SELECT created_at, actor, changes_json
            FROM audit_logs
            WHERE entity_type = 'system_health_alert_routing_signoff'
            ORDER BY created_at DESC
            LIMIT 300
            """,
            label="Alert routing sign-off audit",
        )
        alert_signoff_rows: list[dict[str, str]] = []
        latest_alert_status_by_env: dict[str, str] = {}
        for created_at, actor, changes_json in alert_signoff_logs:
            try:
                payload = json.loads(str(changes_json or "{}"))
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            target_env = str(after.get("target_env") or "").strip().lower()
            status = str(after.get("status") or "").strip().lower()
            row = {
                "recorded_at": str(created_at or ""),
                "actor": str(actor or ""),
                "target_env": target_env,
                "signoff_date": str(after.get("signoff_date") or ""),
                "owner": str(after.get("owner") or ""),
                "status": status,
                "evidence_link": str(after.get("evidence_link") or ""),
                "channel_routing_confirmed": str(bool(after.get("channel_routing_confirmed"))),
                "owner_confirmed": str(bool(after.get("owner_confirmed"))),
                "escalation_confirmed": str(bool(after.get("escalation_confirmed"))),
                "runbook_confirmed": str(bool(after.get("runbook_confirmed"))),
                "notes": str(after.get("notes") or "")[:220],
            }
            alert_signoff_rows.append(row)
            if target_env and target_env not in latest_alert_status_by_env:
                latest_alert_status_by_env[target_env] = status
        if alert_signoff_rows:
            alert_signoff_df = pd.DataFrame(alert_signoff_rows)
            a1, a2 = st.columns(2)
            a1.metric("Dev Alert Routing Sign-Off", latest_alert_status_by_env.get("dev") or "missing")
            a2.metric("Prod Alert Routing Sign-Off", latest_alert_status_by_env.get("prod") or "missing")
            st.dataframe(alert_signoff_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Alert Routing Sign-Off CSV",
                data=alert_signoff_df.to_csv(index=False),
                file_name=f"system_health_alert_routing_signoff_{settings.app_env}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv",
                mime="text/csv",
                key="system_health_download_alert_routing_signoff_csv",
            )
        else:
            st.caption("No alert routing sign-off records yet.")

    st.markdown("### Config Health")
    env_values = read_env_file(".env")
    env_defaults = read_env_file(".env.example")
    req_env = required_env_keys()
    untracked_env_keys = sorted([k for k in env_values.keys() if k not in env_defaults])
    env_missing_or_empty = [
        key
        for key in sorted(req_env)
        if key not in env_values or not str(env_values.get(key, "")).strip()
    ]
    env_ok = len(req_env) - len(env_missing_or_empty)
    env_total = max(1, len(req_env))
    env_ratio = env_ok / env_total
    env_state = health_state(env_ratio)

    runtime_rows = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
    by_key = {str(row.key): row for row in runtime_rows}
    req_runtime = required_runtime_keys()
    seed_keys = {
        "comp_web_fallback_enabled",
        "app_build_version",
        "app_build_sha",
        "ebay_allow_sandbox_seller_ops",
        "ebay_marketplace_id",
        "ebay_currency",
        "ebay_content_language",
        "ebay_merchant_location_key",
        "ebay_payment_policy_id",
        "ebay_fulfillment_policy_id",
        "ebay_return_policy_id",
        "ebay_user_access_token",
        "ebay_user_refresh_token",
        "ebay_user_access_token_expires_at",
        "ebay_user_access_token_refreshed_at",
        "ebay_user_access_token_refresh_failed_at",
        "ebay_user_access_token_refresh_last_error",
        "ebay_user_token_auto_refresh_enabled",
        "ebay_user_token_auto_refresh_interval_hours",
        "ebay_user_token_auto_refresh_min_ttl_minutes",
        "ebay_user_token_auto_refresh_failure_cooldown_minutes",
        "spot_price_provider",
        "metals_api_base_url",
        "metals_api_key",
        "yahoo_finance_base_url",
        "yahoo_symbol_gold",
        "yahoo_symbol_silver",
        "yahoo_symbol_platinum",
        "sync_job_ebay_orders_pull_import_enabled",
        "sync_job_ebay_orders_pull_import_limit",
        "sync_job_ebay_orders_pull_import_offset",
        "sync_job_ebay_shipping_tracking_push_enabled",
        "sync_job_ebay_connection_health_check_enabled",
        "sync_job_ebay_connection_health_check_interval_minutes",
        "sync_job_quickbooks_export_enabled",
        "sync_job_shopify_orders_pull_enabled",
        "comp_llm_system_message",
        "comp_llm_instruction_template",
        "comp_web_fallback_limit",
        "comp_web_detail_fetch_limit",
        "listing_review_two_person_required",
        "listing_review_two_person_channels_csv",
        "comp_dealer_domains_csv",
        "ai_voice_enabled",
        "ai_voice_stt_enabled",
        "ai_voice_tts_enabled",
        "ai_voice_provider",
        "ai_voice_base_url",
        "ai_voice_api_key",
        "ai_voice_stt_model",
        "ai_voice_stt_language",
        "ai_voice_tts_model",
        "ai_voice_tts_voice",
        "ai_voice_tts_response_format",
        "ai_voice_timeout_seconds",
        "ai_voice_tts_max_chars",
        "ai_domain_chat_enabled",
        "ai_domain_comp_tool_enabled",
        "ai_domain_coin_grader_enabled",
        "ai_domain_coin_identifier_enabled",
        "chat_ai_refine_enabled",
        "chat_ai_refine_system_message",
        "chat_ai_refine_instruction",
        "ai_fallback_enabled",
        "ai_fallback_max_profiles",
        "health_queue_execute_exceptions_warn_24h",
        "health_queue_execute_exceptions_critical_24h",
        "health_terminal_queue_failures_warn_24h",
        "health_terminal_queue_failures_critical_24h",
        "health_integration_warnings_warn_24h",
        "health_integration_warnings_critical_24h",
        "runbook_queue_execute_exceptions_url",
        "runbook_terminal_queue_failures_url",
        "runbook_integration_warnings_url",
        "go_live_readiness_weight_checklist_gap_pct",
        "go_live_readiness_weight_env_missing",
        "go_live_readiness_weight_runtime_missing",
        "go_live_readiness_weight_terminal_queue_failure",
        "go_live_readiness_weight_queue_execute_exception",
        "go_live_readiness_penalty_terminal_queue_failure_max",
        "go_live_readiness_penalty_queue_execute_exception_max",
        "go_live_readiness_penalty_integration_warnings_warn",
        "go_live_readiness_penalty_integration_warnings_critical",
        "go_live_readiness_threshold_green",
        "go_live_readiness_threshold_yellow",
        "health_auto_alert_critical_enabled",
        "health_auto_alert_cooldown_minutes",
        "slack_notify_system_health_critical",
        "slack_notify_ebay_oauth_refresh_failures",
        "slack_channel_system_health_critical",
        "slack_template_system_health_critical",
    }
    runtime_untracked_keys = sorted([key for key in by_key.keys() if key not in seed_keys])
    runtime_missing_or_inactive = [
        key
        for key in sorted(req_runtime)
        if key not in by_key or not bool(getattr(by_key[key], "is_active", False))
    ]
    runtime_ok = len(req_runtime) - len(runtime_missing_or_inactive)
    runtime_total = max(1, len(req_runtime))
    runtime_ratio = runtime_ok / runtime_total
    runtime_state = health_state(runtime_ratio)

    h1, h2 = st.columns(2)
    h1.metric("Env Required Health", f"{env_ok}/{env_total} ({env_ratio * 100:.0f}%)")
    h2.metric("Runtime Required Health", f"{runtime_ok}/{runtime_total} ({runtime_ratio * 100:.0f}%)")
    h3, h4 = st.columns(2)
    h3.metric("Env Untracked Keys", len(untracked_env_keys))
    h4.metric("Runtime Untracked Keys", len(runtime_untracked_keys))
    config_rows = [
        _status_row(
            "Env Required Keys",
            "ok" if not env_missing_or_empty else ("warn" if env_state == "warning" else "error"),
            (
                f"state={env_state} missing_or_empty={len(env_missing_or_empty)} "
                f"keys={', '.join(env_missing_or_empty[:8]) if env_missing_or_empty else 'none'}"
            ),
        ),
        _status_row(
            "Env Drift (Untracked Keys)",
            "warn" if untracked_env_keys else "ok",
            (
                f"untracked={len(untracked_env_keys)} "
                f"keys={', '.join(untracked_env_keys[:8]) if untracked_env_keys else 'none'}"
            ),
        ),
        _status_row(
            "Runtime Drift (Untracked Keys)",
            "warn" if runtime_untracked_keys else "ok",
            (
                f"untracked={len(runtime_untracked_keys)} "
                f"keys={', '.join(runtime_untracked_keys[:8]) if runtime_untracked_keys else 'none'}"
            ),
        ),
        _status_row(
            "Runtime Required Keys",
            "ok" if not runtime_missing_or_inactive else ("warn" if runtime_state == "warning" else "error"),
            (
                f"state={runtime_state} missing_or_inactive={len(runtime_missing_or_inactive)} "
                f"keys={', '.join(runtime_missing_or_inactive[:8]) if runtime_missing_or_inactive else 'none'}"
            ),
        ),
    ]
    st.dataframe(pd.DataFrame(config_rows), use_container_width=True)
    if env_missing_or_empty:
        st.caption("Tip: Env tab in Admin supports auto-fixing missing/empty keys from `.env.example`.")
    if runtime_missing_or_inactive:
        st.caption("Tip: Runtime tab in Admin supports one-click default application/activation for missing runtime keys.")

    st.markdown("### Integration Checks")
    integration_rows: list[dict] = []

    storage = MediaStorageService()
    if not storage.enabled:
        integration_rows.append(_status_row("S3 Storage", "warn", "Not configured (`S3_BUCKET` missing or provider not s3)"))
    else:
        try:
            if storage.client is None:
                raise RuntimeError("S3 client is not initialized.")
            storage.client.head_bucket(Bucket=storage.bucket)
            integration_rows.append(_status_row("S3 Storage", "ok", f"bucket={storage.bucket} reachable"))
        except Exception as exc:
            integration_rows.append(_status_row("S3 Storage", "error", str(exc)))

    ebay = EbayClient()
    ebay_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    ebay_refresh_snapshot = _ebay_oauth_auto_refresh_snapshot(
        repo,
        datetime.now(timezone.utc).replace(tzinfo=None),
    )
    if ebay.is_configured():
        base_detail = f"env={ebay.environment} client_credentials=present ru_name=present"
        if ebay_token:
            base_detail += " user_token=present"
        else:
            base_detail += " user_token=missing"
        integration_rows.append(_status_row("eBay API", "ok", base_detail))
    else:
        integration_rows.append(_status_row("eBay API", "warn", "Client credentials incomplete"))

    oauth_detail_parts = [
        f"enabled={ebay_refresh_snapshot.get('enabled')}",
        f"refresh_token={'present' if ebay_refresh_snapshot.get('refresh_token_present') else 'missing'}",
        f"expires_in_min={ebay_refresh_snapshot.get('expires_in_minutes') if ebay_refresh_snapshot.get('expires_in_minutes') is not None else 'unknown'}",
        f"due_reasons={','.join(ebay_refresh_snapshot.get('due_reasons') or []) or 'none'}",
    ]
    if ebay_refresh_snapshot.get("cooldown_active"):
        oauth_detail_parts.append(
            f"cooldown_until={getattr(ebay_refresh_snapshot.get('retry_at'), 'isoformat', lambda: '')()}"
        )
    integration_rows.append(
        _status_row(
            "eBay OAuth Auto-Refresh",
            str(ebay_refresh_snapshot.get("status") or "warn"),
            " ".join(oauth_detail_parts),
        )
    )

    spot = SpotPriceService(repo)
    integration_rows.append(
        _status_row(
            "Spot Price",
            "ok" if spot.is_configured() else "warn",
            f"provider={spot.provider} configured={spot.is_configured()}",
        )
    )

    ai_rows = repo.list_ai_provider_configs(environment=settings.app_env, active_only=False)
    ai_active = [row for row in ai_rows if bool(row.is_active)]
    ai_default = next((row for row in ai_active if bool(row.is_default)), None)
    integration_rows.append(
        _status_row(
            "AI Runtime Profiles",
            "ok" if ai_active else "warn",
            f"total={len(ai_rows)} active={len(ai_active)} default={(ai_default.name if ai_default else 'none')}",
        )
    )

    slack_cfg = resolve_slack_notify_config(repo)
    slack_status = "warn"
    slack_details = (
        f"enabled={slack_cfg.enabled} "
        f"token={'present' if bool(slack_cfg.bot_token) else 'missing'} "
        f"default_channel={'present' if bool(slack_cfg.default_channel) else 'missing'}"
    )
    if slack_cfg.enabled and slack_cfg.bot_token and slack_cfg.default_channel:
        slack_status = "ok"
    integration_rows.append(_status_row("Slack Notifications", slack_status, slack_details))

    # Scope signal for future workspace integrations; not yet implemented.
    google_flags = [
        ("Gmail", bool(os.getenv("GOOGLE_GMAIL_ENABLED", "").strip())),
        ("Calendar", bool(os.getenv("GOOGLE_CALENDAR_ENABLED", "").strip())),
        ("Drive", bool(os.getenv("GOOGLE_DRIVE_ENABLED", "").strip())),
    ]
    enabled_google = [name for name, enabled in google_flags if enabled]
    integration_rows.append(
        _status_row(
            "Google Workspace (planned)",
            "warn",
            (
                f"enabled_flags={','.join(enabled_google) if enabled_google else 'none'} "
                "(integration scaffold not implemented yet)"
            ),
        )
    )

    st.dataframe(pd.DataFrame(integration_rows), use_container_width=True)
    st.caption("eBay OAuth Auto-Refresh")
    er1, er2, er3, er4 = st.columns(4)
    er1.metric(
        "Status",
        str(ebay_refresh_snapshot.get("status") or "unknown").upper(),
    )
    er2.metric(
        "Next Due",
        (
            getattr(ebay_refresh_snapshot.get("next_due_at"), "isoformat", lambda: "now")(
                timespec="seconds"
            )
            if ebay_refresh_snapshot.get("next_due_at") is not None
            else "now"
        ),
    )
    er3.metric(
        "Token Expiry",
        (
            f"{int(ebay_refresh_snapshot.get('expires_in_minutes'))} min"
            if ebay_refresh_snapshot.get("expires_in_minutes") is not None
            else "unknown"
        ),
    )
    er4.metric(
        "Failure Cooldown",
        (
            "active"
            if bool(ebay_refresh_snapshot.get("cooldown_active"))
            else "clear"
        ),
    )
    if ebay_refresh_snapshot.get("last_error"):
        st.caption(f"Last refresh error: {str(ebay_refresh_snapshot.get('last_error'))[:300]}")

    st.markdown("### Worker + Queue Diagnostics")
    stale_running_seconds = 1800
    try:
        stale_running_seconds = int(
            max(
                60,
                min(
                    86400,
                    int(get_runtime_str(repo, "health_stale_running_seconds", "1800") or "1800"),
                ),
            )
        )
    except Exception:
        stale_running_seconds = 1800

    sync_runs = repo.list_sync_runs(limit=1000)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    queued_runs = [r for r in sync_runs if (r.status or "").strip().lower() == "queued"]
    running_runs = [r for r in sync_runs if (r.status or "").strip().lower() == "running"]
    failed_runs = [r for r in sync_runs if (r.status or "").strip().lower() in {"failed", "partial"}]
    stale_running_runs = [
        r
        for r in running_runs
        if r.started_at is not None and (now_utc - r.started_at) > timedelta(seconds=stale_running_seconds)
    ]
    unresolved_errors_count = int(
        _safe_scalar_one(
            repo,
            "SELECT COUNT(*) FROM sync_errors WHERE resolved_at IS NULL",
            label="Unresolved sync error count",
            default=0,
        )
        or 0
    )

    latest_run = sync_runs[0] if sync_runs else None
    latest_run_started = latest_run.started_at if latest_run is not None else None
    latest_run_status = (latest_run.status or "n/a") if latest_run is not None else "n/a"

    q1, q2, q3, q4, q5 = st.columns(5)
    q1.metric("Queued Runs", len(queued_runs))
    q2.metric("Running Runs", len(running_runs))
    q3.metric("Failed/Partial Runs", len(failed_runs))
    q4.metric("Unresolved Sync Errors", unresolved_errors_count)
    q5.metric("Stale Running Runs", len(stale_running_runs))

    worker_rows: list[dict] = []
    worker_rows.append(
        _status_row(
            "Sync Queue",
            "ok" if len(stale_running_runs) == 0 else "warn",
            (
                f"queued={len(queued_runs)} running={len(running_runs)} failed_partial={len(failed_runs)} "
                f"unresolved_errors={unresolved_errors_count}"
            ),
        )
    )
    if latest_run is not None:
        worker_rows.append(
            _status_row(
                "Latest Sync Run",
                "ok" if str(latest_run_status).lower() not in {"failed", "partial"} else "warn",
                (
                    f"id={latest_run.id} provider={latest_run.provider} job={latest_run.job_name} "
                    f"status={latest_run_status} started_at={latest_run_started}"
                ),
            )
        )
    if stale_running_runs:
        for row in stale_running_runs[:10]:
            age_seconds = int(max(0, (now_utc - row.started_at).total_seconds())) if row.started_at else -1
            worker_rows.append(
                _status_row(
                    "Stale Running Sync",
                    "warn",
                    (
                        f"id={row.id} provider={row.provider} job={row.job_name} "
                        f"started_at={row.started_at} age_seconds={age_seconds}"
                    ),
                )
            )
    else:
        worker_rows.append(
            _status_row(
                "Stale Running Sync",
                "ok",
                f"none detected (threshold={stale_running_seconds}s)",
            )
        )
    st.dataframe(pd.DataFrame(worker_rows), use_container_width=True)

    if sync_runs:
        st.caption("Recent Sync Runs")
        recent_df = pd.DataFrame(
            [
                {
                    "id": row.id,
                    "provider": row.provider,
                    "job_name": row.job_name,
                    "status": row.status,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                    "retry_count": row.retry_count,
                    "records_processed": row.records_processed,
                    "records_failed": row.records_failed,
                }
                for row in sync_runs[:20]
            ]
        )
        st.dataframe(recent_df, use_container_width=True)

    st.markdown("### Live Checks (Manual)")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Run eBay Live Token Check", key="system_health_ebay_live_check"):
            if not ebay_token:
                st.error("No eBay user token configured.")
            elif not ebay.is_configured():
                st.error("eBay app credentials are not fully configured.")
            else:
                try:
                    payload = ebay.get_account_privileges(ebay_token)
                    st.success("eBay live token check succeeded.")
                    st.json(payload)
                except Exception as exc:
                    st.error(f"eBay live check failed: {exc}")
    with c2:
        if st.button("Run Spot Quote Check", key="system_health_spot_live_check"):
            if not spot.is_configured():
                st.error("Spot provider is not configured.")
            else:
                try:
                    quotes = spot.latest_quotes()
                    st.success(f"Spot quote check succeeded for metals: {', '.join(sorted(quotes.keys()))}")
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "metal": quote.metal,
                                    "usd_per_troy_oz": quote.usd_per_troy_oz,
                                    "as_of": quote.as_of.isoformat(),
                                    "source": quote.source,
                                }
                                for quote in quotes.values()
                            ]
                        ),
                        use_container_width=True,
                    )
                except Exception as exc:
                    st.error(f"Spot quote check failed: {exc}")
    with c3:
        if st.button("Run Slack Connectivity Check", key="system_health_slack_live_check"):
            result = check_slack_connectivity(repo)
            if bool(result.get("ok")):
                st.success("Slack connectivity check succeeded.")
            else:
                st.error(f"Slack connectivity check failed: {result.get('reason')}")
            st.json(result)
