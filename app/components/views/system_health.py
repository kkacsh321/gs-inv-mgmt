from __future__ import annotations

import os
import platform
import shutil
import json
from datetime import datetime, timedelta, timezone

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
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.slack_notify import (
    build_slack_alert_text,
    check_slack_connectivity,
    dispatch_slack_alert,
    resolve_slack_notify_config,
)
from app.services.spot_price import SpotPriceService

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

    st.caption(f"As of UTC: {datetime.now(timezone.utc).isoformat()} | env=`{settings.app_env}`")
    refresh = st.button("Refresh Health Snapshot", key="system_health_refresh")
    if refresh:
        st.rerun()

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

    try:
        repo.db.execute(text("SELECT 1"))
        service_rows.append(_status_row("Database", "ok", "SELECT 1 succeeded"))
    except Exception as exc:
        service_rows.append(_status_row("Database", "error", str(exc)))

    try:
        version_row = repo.db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        if version_row:
            service_rows.append(_status_row("Migrations", "ok", f"alembic_version={version_row[0]}"))
        else:
            service_rows.append(_status_row("Migrations", "warn", "No alembic version row found"))
    except Exception as exc:
        service_rows.append(_status_row("Migrations", "error", str(exc)))

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

    st.markdown("### Error Signals (24h)")
    integration_error_rows: list[dict] = []
    try:
        recent_events = repo.db.execute(
            text(
                """
                SELECT created_at, actor, action, changes_json
                FROM audit_logs
                WHERE entity_type = 'integration_event'
                  AND created_at >= :since
                ORDER BY created_at DESC
                LIMIT 2000
                """
            ),
            {"since": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)},
        ).all()
    except Exception:
        recent_events = []
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
        cooldown_since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=int(auto_alert_cooldown_minutes)
        )
        recently_alerted = False
        try:
            recent_health_alerts = repo.db.execute(
                text(
                    """
                    SELECT changes_json
                    FROM audit_logs
                    WHERE entity_type = 'integration_event'
                      AND created_at >= :since
                    ORDER BY created_at DESC
                    LIMIT 200
                    """
                ),
                {"since": cooldown_since},
            ).all()
            for (changes_json,) in recent_health_alerts:
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
        except Exception:
            recently_alerted = False

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

    st.markdown("#### Critical Alert Evidence (Recent)")
    try:
        recent_alert_rows = repo.db.execute(
            text(
                """
                SELECT created_at, actor, action, changes_json
                FROM audit_logs
                WHERE entity_type = 'integration_event'
                  AND created_at >= :since
                ORDER BY created_at DESC
                LIMIT 800
                """
            ),
            {"since": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)},
        ).all()
    except Exception:
        recent_alert_rows = []
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

    calibration_logs = repo.db.execute(
        text(
            """
            SELECT created_at, actor, changes_json
            FROM audit_logs
            WHERE entity_type = 'system_health_calibration_signoff'
            ORDER BY created_at DESC
            LIMIT 300
            """
        )
    ).all()
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

    alert_signoff_logs = repo.db.execute(
        text(
            """
            SELECT created_at, actor, changes_json
            FROM audit_logs
            WHERE entity_type = 'system_health_alert_routing_signoff'
            ORDER BY created_at DESC
            LIMIT 300
            """
        )
    ).all()
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
    if ebay.is_configured():
        base_detail = f"env={ebay.environment} client_credentials=present ru_name=present"
        if ebay_token:
            base_detail += " user_token=present"
        else:
            base_detail += " user_token=missing"
        integration_rows.append(_status_row("eBay API", "ok", base_detail))
    else:
        integration_rows.append(_status_row("eBay API", "warn", "Client credentials incomplete"))

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
    unresolved_errors_count = 0
    try:
        unresolved_errors_count = int(
            repo.db.execute(
                text("SELECT COUNT(*) FROM sync_errors WHERE resolved_at IS NULL")
            ).scalar_one()
        )
    except Exception:
        unresolved_errors_count = 0

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
