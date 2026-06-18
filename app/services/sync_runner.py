from __future__ import annotations

import time
from datetime import datetime, UTC, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.repository import InventoryRepository
from app.services.runtime_settings import get_runtime_bool, get_runtime_float, get_runtime_int, get_runtime_str
from app.services.accounting_cogs import (
    cogs_evidence_split as build_cogs_evidence_split,
    format_cogs_evidence_split,
    net_before_cogs as accounting_net_before_cogs,
    profit_after_returns as accounting_profit_after_returns,
    profit_before_returns as accounting_profit_before_returns,
    return_refund_total,
    returns_profit_impact as accounting_returns_profit_impact,
)
from app.services.slack_notify import build_slack_alert_text, dispatch_slack_alert
from app.services.notification_outbox import (
    cleanup_notification_outbox_retention,
    process_due_notification_outbox,
)
from app.services.ai_accountant_identity import AI_ACCOUNTANT_LABEL
from app.services.ai_accountant_monitor import run_ai_accountant_monitor
from app.services.lifecycle_retention import cleanup_lifecycle_retention
from app.services.sync_jobs import execute_sync_job, is_sync_job_enabled, maybe_auto_refresh_ebay_user_token
from app.utils.time import utcnow_naive


def _log(message: str) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"[sync-runner] {stamp} {message}", flush=True)


def _safe_float(value) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _product_default_landed_unit_cost(product) -> float:
    landed = (
        _safe_float(getattr(product, "acquisition_cost", None))
        + _safe_float(getattr(product, "acquisition_tax_paid", None))
        + _safe_float(getattr(product, "acquisition_shipping_paid", None))
        + _safe_float(getattr(product, "acquisition_handling_paid", None))
    )
    if landed > 0:
        return landed
    return _safe_float(getattr(product, "product_cost", None))


def _format_cogs_source_mix(source_totals: dict[str, float], source_counts: dict[str, int]) -> str:
    if not source_totals:
        return ""
    parts = [
        f"{source} ${float(total):,.2f}/{int(source_counts.get(source, 0))} sale(s)"
        for source, total in sorted(source_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        if float(total or 0.0) > 0 or int(source_counts.get(source, 0)) > 0
    ]
    return "; ".join(parts)


def _run_ebay_orders_pull_import() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not is_sync_job_enabled("ebay_orders_pull_import", repo=repo):
            _log("Job `ebay_orders_pull_import` disabled by configuration.")
            return
        token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
        if not token:
            _log("Skipping `ebay_orders_pull_import` (missing eBay access token).")
            return
        result = execute_sync_job(
            repo,
            job_name="ebay_orders_pull_import",
            access_token=token,
            actor=settings.sync_runner_actor,
            limit=max(
                1,
                get_runtime_int(
                    repo,
                    "sync_job_ebay_orders_pull_import_limit",
                    int(settings.sync_job_ebay_orders_pull_import_limit),
                ),
            ),
            offset=max(
                0,
                get_runtime_int(
                    repo,
                    "sync_job_ebay_orders_pull_import_offset",
                    int(settings.sync_job_ebay_orders_pull_import_offset),
                ),
            ),
        )
        _log(
            "Completed `ebay_orders_pull_import`: "
            f"run_id={result['run_id']} status={result['status']} "
            f"processed={result['processed']} created={result['created']} "
            f"updated={result['updated']} failed={result['failed']}"
        )
    except Exception as exc:
        _log(f"Job `ebay_orders_pull_import` failed: {exc}")
    finally:
        db.close()


def _run_ebay_connection_health_check() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not is_sync_job_enabled("ebay_connection_health_check", repo=repo):
            _log("Job `ebay_connection_health_check` disabled by configuration.")
            return

        interval_minutes = max(
            5,
            int(
                get_runtime_int(
                    repo,
                    "sync_job_ebay_connection_health_check_interval_minutes",
                    int(getattr(settings, "sync_job_ebay_connection_health_check_interval_minutes", 30)),
                )
            ),
        )
        now = utcnow_naive()
        existing = [
            row
            for row in repo.list_sync_runs(provider="ebay", limit=500)
            if str(getattr(row, "job_name", "") or "").strip().lower() == "ebay_connection_health_check"
        ]
        latest = existing[0] if existing else None
        latest_at = getattr(latest, "completed_at", None) or getattr(latest, "started_at", None)
        if latest_at is not None:
            due_at = latest_at + timedelta(minutes=int(interval_minutes))
            if due_at > now:
                _log(
                    "Skipping `ebay_connection_health_check` (not due): "
                    f"next_due={due_at.isoformat(timespec='seconds')}"
                )
                return

        token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
        result = execute_sync_job(
            repo,
            job_name="ebay_connection_health_check",
            access_token=token,
            actor=settings.sync_runner_actor,
        )
        _log(
            "Completed `ebay_connection_health_check`: "
            f"run_id={result['run_id']} status={result['status']} "
            f"processed={result['processed']} failed={result['failed']}"
        )
    except Exception as exc:
        _log(f"Job `ebay_connection_health_check` failed: {exc}")
    finally:
        db.close()


def _run_ebay_store_categories_sync() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not is_sync_job_enabled("ebay_store_categories_sync", repo=repo):
            _log("Job `ebay_store_categories_sync` disabled by configuration.")
            return

        interval_hours = max(
            1,
            int(
                get_runtime_int(
                    repo,
                    "sync_job_ebay_store_categories_sync_interval_hours",
                    int(getattr(settings, "sync_job_ebay_store_categories_sync_interval_hours", 24)),
                )
            ),
        )
        now = utcnow_naive()
        existing = [
            row
            for row in repo.list_sync_runs(provider="ebay", limit=500)
            if str(getattr(row, "job_name", "") or "").strip().lower() == "ebay_store_categories_sync"
        ]
        latest = existing[0] if existing else None
        latest_at = getattr(latest, "completed_at", None) or getattr(latest, "started_at", None)
        if latest_at is not None:
            due_at = latest_at + timedelta(hours=int(interval_hours))
            if due_at > now:
                _log(
                    "Skipping `ebay_store_categories_sync` (not due): "
                    f"next_due={due_at.isoformat(timespec='seconds')} interval_hours={interval_hours}"
                )
                return

        token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
        if not token:
            _log("Skipping `ebay_store_categories_sync` (missing eBay access token).")
            return
        result = execute_sync_job(
            repo,
            job_name="ebay_store_categories_sync",
            access_token=token,
            actor=settings.sync_runner_actor,
            marketplace_id=str(settings.ebay_marketplace_id or "EBAY_US").strip() or "EBAY_US",
            deactivate_missing=get_runtime_bool(
                repo,
                "sync_job_ebay_store_categories_sync_deactivate_missing",
                bool(getattr(settings, "sync_job_ebay_store_categories_sync_deactivate_missing", False)),
            ),
        )
        _log(
            "Completed `ebay_store_categories_sync`: "
            f"run_id={result['run_id']} status={result['status']} "
            f"processed={result['processed']} updated={result['updated']} "
            f"missing={result.get('missing', 0)} deactivated={result.get('deactivated', 0)} "
            f"failed={result['failed']}"
        )
    except Exception as exc:
        _log(f"Job `ebay_store_categories_sync` failed: {exc}")
    finally:
        db.close()


def _run_ebay_token_auto_refresh() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        result = maybe_auto_refresh_ebay_user_token(
            repo,
            actor=settings.sync_runner_actor,
            force=False,
        )
        status = str(result.get("status") or "").strip().lower()
        if status == "refreshed":
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="ebay_oauth",
                action="auto_refresh",
                status="success",
                details={
                    "reason": str(result.get("reason") or "").strip(),
                    "expires_at": str(result.get("expires_at") or "").strip(),
                },
            )
            _log(
                "Auto-refreshed eBay user token: "
                f"reason={result.get('reason') or 'n/a'} "
                f"expires_at={result.get('expires_at') or '(unknown)'}"
            )
        elif status == "failed":
            reason = str(result.get("reason") or "").strip()
            transient_network = reason == "transient_network_unavailable"
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="ebay_oauth",
                action="auto_refresh",
                status="warning" if transient_network else "error",
                details={
                    "reason": reason,
                    "error": str(result.get("error") or "").strip()[:500],
                },
            )
            if (not transient_network) and get_runtime_bool(repo, "slack_notify_ebay_oauth_refresh_failures", True):
                try:
                    alert_text = build_slack_alert_text(
                        repo,
                        event_type="sync_failures",
                        default_template=(
                            ":warning: *GoldenStackers* eBay OAuth auto-refresh failed\n"
                            "- Env: `{env}`\n"
                            "- Reason: `{reason}`\n"
                            "- Error: `{error}`"
                        ),
                        context={
                            "reason": str(result.get("reason") or "").strip(),
                            "error": str(result.get("error") or "").strip()[:300],
                        },
                    )
                    dispatch_slack_alert(
                        repo,
                        actor=settings.sync_runner_actor,
                        text=alert_text,
                        event_type="sync_failures",
                        severity="warning",
                    )
                except Exception:
                    pass
            if transient_network:
                _log(
                    "Auto-refresh eBay user token skipped by transient network/DNS failure: "
                    f"{result.get('error') or 'unknown error'}"
                )
            else:
                _log(f"Auto-refresh eBay user token failed: {result.get('error') or 'unknown error'}")
        elif status == "skipped" and str(result.get("reason") or "").strip().lower() == "failure_cooldown_active":
            _log(
                "Skipping eBay user token auto-refresh (failure cooldown active): "
                f"retry_at={str(result.get('retry_at') or '').strip() or '(unknown)'}"
            )
    except Exception as exc:
        _log(f"Auto-refresh eBay user token failed: {exc}")
    finally:
        db.close()


def _run_governance_snapshot_schedule() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "governance_snapshot_runner_enabled", False):
            _log("Governance snapshot runner disabled by runtime setting.")
            return

        interval_hours = max(1, min(24 * 30, int(get_runtime_int(repo, "governance_snapshot_interval_hours", 24))))
        lookback_days = max(1, min(365, int(get_runtime_int(repo, "governance_snapshot_lookback_days", 30))))
        max_rows = max(100, min(10000, int(get_runtime_int(repo, "governance_snapshot_max_rows_per_scope", 2000))))
        now = utcnow_naive()

        recent_snapshots = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "governance_export",
                AuditLog.action == "snapshot",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        latest_worker_snapshot = None
        for row in recent_snapshots:
            payload = row.changes if isinstance(row.changes, dict) else {}
            if str(payload.get("source") or "").strip().lower() == "sync_runner":
                latest_worker_snapshot = row
                break
        if latest_worker_snapshot is not None and latest_worker_snapshot.created_at is not None:
            due_at = latest_worker_snapshot.created_at + timedelta(hours=int(interval_hours))
            if due_at > now:
                _log(
                    "Skipping governance snapshot (not due): "
                    f"next_due={due_at.isoformat(timespec='seconds')} interval_hours={interval_hours}"
                )
                return

        cutoff = now - timedelta(days=int(lookback_days))
        nav_count = int(
            repo.db.query(AuditLog)
            .filter(
                AuditLog.entity_type == "navigation",
                AuditLog.action.in_(["workspace_handoff_applied", "workspace_handoff_cleared"]),
                AuditLog.created_at >= cutoff,
            )
            .limit(int(max_rows))
            .count()
        )
        feedback_count = int(
            repo.db.query(AuditLog)
            .filter(
                AuditLog.entity_type == "workspace_feedback",
                AuditLog.created_at >= cutoff,
            )
            .limit(int(max_rows))
            .count()
        )
        parity_count = int(
            repo.db.query(AuditLog)
            .filter(
                AuditLog.entity_type.in_(["workspace_parity", "workspace_parity_decision", "workspace_followup"]),
                AuditLog.created_at >= cutoff,
            )
            .limit(int(max_rows))
            .count()
        )
        comp_count = int(
            repo.db.query(AuditLog)
            .filter(
                AuditLog.entity_type.in_(["comp_photo_retry", "comp_domain_recommendation"]),
                AuditLog.created_at >= cutoff,
            )
            .limit(int(max_rows))
            .count()
        )

        repo.record_audit_event(
            entity_type="governance_export",
            entity_id=None,
            action="snapshot",
            actor=settings.sync_runner_actor,
            changes={
                "environment": settings.app_env,
                "recorded_at": now.isoformat(timespec="seconds"),
                "source": "sync_runner",
                "scheduled": True,
                "interval_hours": int(interval_hours),
                "lookback_days": int(lookback_days),
                "max_rows_per_scope": int(max_rows),
                "counts": {
                    "handoff_events": int(nav_count),
                    "workspace_feedback_events": int(feedback_count),
                    "parity_followup_events": int(parity_count),
                    "photo_comp_events": int(comp_count),
                },
            },
        )
        _log(
            "Recorded scheduled governance snapshot: "
            f"handoff={nav_count} feedback={feedback_count} parity={parity_count} comp={comp_count}"
        )
    except Exception as exc:
        _log(f"Governance snapshot scheduler failed: {exc}")
    finally:
        db.close()


def _resolve_schedule_timezone(repo: InventoryRepository, *, key: str, default_tz: str) -> ZoneInfo:
    tz_name = str(get_runtime_str(repo, key, default_tz) or default_tz).strip() or default_tz
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _app_default_timezone(repo: InventoryRepository) -> str:
    fallback_tz = str(getattr(settings, "app_default_timezone", "America/Denver") or "America/Denver").strip() or "America/Denver"
    return str(
        get_runtime_str(
            repo,
            "app_default_timezone",
            fallback_tz,
        )
        or fallback_tz
        or "America/Denver"
    ).strip() or "America/Denver"


def _notification_route_allows_slack(repo: InventoryRepository, *, route_key: str, default_route: str = "slack") -> bool:
    route = str(get_runtime_str(repo, route_key, default_route) or default_route).strip().lower()
    if route in {"disabled", "off", "none", "email"}:
        return False
    return route in {"slack", "both", "all", ""}


def _parse_schedule_local_time(value: str, *, fallback_hour: int, fallback_minute: int) -> tuple[int, int]:
    raw = str(value or "").strip()
    if ":" not in raw:
        return fallback_hour, fallback_minute
    left, right = raw.split(":", 1)
    try:
        hour = max(0, min(23, int(left)))
        minute = max(0, min(59, int(right)))
        return hour, minute
    except Exception:
        return fallback_hour, fallback_minute


def _is_daily_job_due(
    repo: InventoryRepository,
    *,
    key_prefix: str,
    timezone_key: str,
    local_time_key: str,
    default_timezone: str,
    default_local_time: str,
) -> tuple[bool, datetime, str]:
    tz = _resolve_schedule_timezone(repo, key=timezone_key, default_tz=default_timezone)
    local_now = datetime.now(tz)
    default_hour, default_minute = _parse_schedule_local_time(
        default_local_time,
        fallback_hour=2,
        fallback_minute=0,
    )
    scheduled_hour, scheduled_minute = _parse_schedule_local_time(
        get_runtime_str(repo, local_time_key, default_local_time),
        fallback_hour=default_hour,
        fallback_minute=default_minute,
    )
    if (local_now.hour, local_now.minute) < (scheduled_hour, scheduled_minute):
        return False, local_now, local_now.date().isoformat()
    local_date = local_now.date().isoformat()
    last_attempt_date = str(
        get_runtime_str(repo, f"{key_prefix}_last_attempt_local_date", "")
        or ""
    ).strip()
    return bool(last_attempt_date != local_date), local_now, local_date


def _is_interval_job_due(
    repo: InventoryRepository,
    *,
    key_prefix: str,
    timezone_key: str,
    interval_hours_key: str,
    default_timezone: str,
    default_interval_hours: int,
) -> tuple[bool, datetime, str]:
    tz = _resolve_schedule_timezone(repo, key=timezone_key, default_tz=default_timezone)
    local_now = datetime.now(tz)
    interval_hours = max(
        1,
        min(24, int(get_runtime_int(repo, interval_hours_key, int(default_interval_hours or 6)))),
    )
    last_attempt_raw = str(get_runtime_str(repo, f"{key_prefix}_last_attempt_at", "") or "").strip()
    if last_attempt_raw:
        try:
            last_attempt = datetime.fromisoformat(last_attempt_raw)
            if last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=tz)
            due_at = last_attempt.astimezone(tz) + timedelta(hours=interval_hours)
            if due_at > local_now:
                return False, local_now, local_now.date().isoformat()
        except Exception:
            pass
    return True, local_now, local_now.date().isoformat()


def _parse_daily_cron_hhmm_utc(cron_expr: str, *, default_hour: int, default_minute: int) -> tuple[int, int]:
    raw = str(cron_expr or "").strip()
    parts = raw.split()
    if len(parts) < 2:
        return default_hour, default_minute
    minute_raw, hour_raw = parts[0].strip(), parts[1].strip()
    if minute_raw == "*" or hour_raw == "*":
        return default_hour, default_minute
    try:
        minute = max(0, min(59, int(minute_raw)))
        hour = max(0, min(23, int(hour_raw)))
        return hour, minute
    except Exception:
        return default_hour, default_minute


def _normalized_fee_source_coverage_health(
    rows: list[dict],
    *,
    threshold_percent: float,
    min_consecutive_weeks: int,
) -> dict:
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
    for row in reversed(weekly_rows):
        if float(row["coverage_pct"]) < float(threshold_percent):
            consecutive_below += 1
        else:
            break
    triggered = bool(consecutive_below >= max(1, int(min_consecutive_weeks)))
    latest_week = weekly_rows[-1] if weekly_rows else {"week_start": "", "coverage_pct": 0.0, "total_sales": 0}
    return {
        "triggered": triggered,
        "threshold_percent": float(threshold_percent),
        "min_consecutive_weeks": int(min_consecutive_weeks),
        "consecutive_below": int(consecutive_below),
        "latest_week_start": str(latest_week.get("week_start") or ""),
        "latest_week_coverage_pct": float(latest_week.get("coverage_pct") or 0.0),
        "latest_week_total_sales": int(latest_week.get("total_sales") or 0),
        "weekly_rows": weekly_rows,
    }


def _mark_daily_job_attempt(
    repo: InventoryRepository,
    *,
    key_prefix: str,
    actor: str,
    local_date: str,
    success: bool,
) -> None:
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key=f"{key_prefix}_last_attempt_local_date",
        value=str(local_date or "").strip(),
        value_type="str",
        description=f"Last local-date attempt marker for `{key_prefix}` scheduler.",
        is_active=True,
        actor=actor,
    )
    if success:
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key=f"{key_prefix}_last_success_local_date",
            value=str(local_date or "").strip(),
            value_type="str",
            description=f"Last local-date success marker for `{key_prefix}` scheduler.",
            is_active=True,
            actor=actor,
        )


def _mark_interval_job_attempt(
    repo: InventoryRepository,
    *,
    key_prefix: str,
    actor: str,
    local_now: datetime,
    success: bool,
) -> None:
    stamp = local_now.isoformat(timespec="seconds")
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key=f"{key_prefix}_last_attempt_at",
        value=stamp,
        value_type="str",
        description=f"Last local timestamp attempt marker for `{key_prefix}` interval scheduler.",
        is_active=True,
        actor=actor,
    )
    if success:
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key=f"{key_prefix}_last_success_at",
            value=stamp,
            value_type="str",
            description=f"Last local timestamp success marker for `{key_prefix}` interval scheduler.",
            is_active=True,
            actor=actor,
        )


def _backup_create_dump(*, include_drop_statements: bool):
    from app.services.db_backup import create_backup_dump

    return create_backup_dump(include_drop_statements=include_drop_statements)


def _backup_s3_enabled() -> bool:
    from app.services.db_backup import s3_backup_enabled

    return bool(s3_backup_enabled())


def _backup_upload_to_s3(file_path):
    from app.services.db_backup import upload_backup_to_s3

    return upload_backup_to_s3(file_path)


def _run_scheduled_db_backup() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "backup_policy_enabled", False):
            _log("Scheduled DB backup disabled: `backup_policy_enabled=false`.")
            return
        if not get_runtime_bool(repo, "backup_policy_runner_enabled", False):
            _log("Scheduled DB backup runner disabled: `backup_policy_runner_enabled=false`.")
            return

        due, local_now, local_date = _is_daily_job_due(
            repo,
            key_prefix="backup_policy_schedule",
            timezone_key="backup_policy_schedule_timezone",
            local_time_key="backup_policy_schedule_local_time",
            default_timezone=_app_default_timezone(repo),
            default_local_time="02:00",
        )
        if not due:
            _log(
                "Skipping scheduled DB backup (not due): "
                f"local_now={local_now.isoformat(timespec='seconds')}"
            )
            return

        actor = settings.sync_runner_actor
        include_drop = get_runtime_bool(repo, "backup_policy_include_drop_statements", True)
        upload_to_s3 = get_runtime_bool(repo, "backup_policy_upload_to_s3", True)

        key = ""
        backup = _backup_create_dump(include_drop_statements=include_drop)
        if upload_to_s3:
            if not _backup_s3_enabled():
                raise RuntimeError("S3 upload requested by policy but S3 backup is not configured.")
            key = _backup_upload_to_s3(backup.file_path)

        _mark_daily_job_attempt(
            repo,
            key_prefix="backup_policy_schedule",
            actor=actor,
            local_date=local_date,
            success=True,
        )
        repo.log_integration_event(
            actor=actor,
            integration="backup",
            action="scheduled_db_backup",
            status="success",
            details={
                "file_name": str(backup.file_name),
                "size_bytes": int(backup.size_bytes),
                "uploaded_to_s3": bool(upload_to_s3),
                "s3_key": str(key or ""),
                "local_time": local_now.isoformat(timespec="seconds"),
            },
        )
        if get_runtime_bool(repo, "slack_notify_backup_success", False) and _notification_route_allows_slack(
            repo,
            route_key="notification_route_backup_events",
            default_route="slack",
        ):
            backup_success_text = build_slack_alert_text(
                repo,
                event_type="backup_success",
                default_template=(
                    ":white_check_mark: *GoldenStackers* scheduled DB backup completed\n"
                    "- Env: `{env}`\n"
                    "- File: `{file_name}`\n"
                    "- Size: `{size_bytes}` bytes\n"
                    "- Uploaded to S3: `{uploaded_to_s3}`\n"
                    "- S3 Key: `{s3_key}`\n"
                    "- Local Time: `{local_time}`"
                ),
                context={
                    "env": settings.app_env,
                    "file_name": str(backup.file_name),
                    "size_bytes": int(backup.size_bytes),
                    "uploaded_to_s3": bool(upload_to_s3),
                    "s3_key": str(key or ""),
                    "local_time": local_now.isoformat(timespec="seconds"),
                },
            )
            dispatch_slack_alert(
                repo,
                actor=settings.sync_runner_actor,
                text=backup_success_text,
                event_type="backup_success",
                severity="info",
                override_channel=str(get_runtime_str(repo, "slack_channel_backup_events", "") or "").strip(),
            )
        _log(
            "Completed scheduled DB backup: "
            f"file={backup.file_name} size={backup.size_bytes} uploaded_to_s3={bool(upload_to_s3)} key={key or '-'}"
        )
    except Exception as exc:
        try:
            repo = InventoryRepository(db)
            due, local_now, local_date = _is_daily_job_due(
                repo,
                key_prefix="backup_policy_schedule",
                timezone_key="backup_policy_schedule_timezone",
                local_time_key="backup_policy_schedule_local_time",
                default_timezone=_app_default_timezone(repo),
                default_local_time="02:00",
            )
            _mark_daily_job_attempt(
                repo,
                key_prefix="backup_policy_schedule",
                actor=settings.sync_runner_actor,
                local_date=local_date,
                success=False,
            )
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="backup",
                action="scheduled_db_backup",
                status="error",
                details={
                    "error": str(exc)[:500],
                    "local_time": local_now.isoformat(timespec="seconds"),
                },
            )
            if get_runtime_bool(repo, "slack_notify_backup_failures", True) and _notification_route_allows_slack(
                repo,
                route_key="notification_route_backup_events",
                default_route="slack",
            ):
                backup_error_text = build_slack_alert_text(
                    repo,
                    event_type="backup_failure",
                    default_template=(
                        ":x: *GoldenStackers* scheduled DB backup failed\n"
                        "- Env: `{env}`\n"
                        "- Error: `{error}`\n"
                        "- Local Time: `{local_time}`"
                    ),
                    context={
                        "env": settings.app_env,
                        "error": str(exc)[:300],
                        "local_time": local_now.isoformat(timespec="seconds"),
                    },
                )
                dispatch_slack_alert(
                    repo,
                    actor=settings.sync_runner_actor,
                    text=backup_error_text,
                    event_type="backup_failure",
                    severity="error",
                    override_channel=str(get_runtime_str(repo, "slack_channel_backup_events", "") or "").strip(),
                )
        except Exception:
            pass
        _log(f"Scheduled DB backup failed: {exc}")
    finally:
        db.close()


def _run_daily_slack_report() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        enabled = bool(
            get_runtime_bool(
                repo,
                "slack_daily_report_enabled",
                get_runtime_bool(repo, "slack_notify_daily_summary", False),
            )
        )
        if not enabled:
            _log("Daily Slack report disabled: `slack_daily_report_enabled=false`.")
            return

        local_schedule_value = str(get_runtime_str(repo, "slack_daily_report_local_time", "") or "").strip()
        if local_schedule_value:
            due, local_now, local_date = _is_daily_job_due(
                repo,
                key_prefix="slack_daily_report",
                timezone_key="slack_daily_report_timezone",
                local_time_key="slack_daily_report_local_time",
                default_timezone=_app_default_timezone(repo),
                default_local_time="08:00",
            )
        else:
            cron_expr = str(get_runtime_str(repo, "slack_daily_summary_cron", "0 16 * * *") or "0 16 * * *").strip()
            hour_utc, minute_utc = _parse_daily_cron_hhmm_utc(
                cron_expr,
                default_hour=16,
                default_minute=0,
            )
            due, local_now, local_date = _is_daily_job_due(
                repo,
                key_prefix="slack_daily_report",
                timezone_key="slack_daily_report_timezone",
                local_time_key="slack_daily_report_local_time",
                default_timezone="UTC",
                default_local_time=f"{hour_utc:02d}:{minute_utc:02d}",
            )
        if not due:
            _log(
                "Skipping daily Slack report (not due): "
                f"local_now={local_now.isoformat(timespec='seconds')}"
            )
            return

        now = utcnow_naive()
        since = now - timedelta(hours=24)
        metrics = repo.dashboard_metrics()
        sales = repo.list_sales()
        products = repo.list_products()
        listings = repo.list_listings()
        orders = repo.list_orders()

        sales_24h = [s for s in sales if getattr(s, "sold_at", None) and s.sold_at >= since]
        default_unit_cost_by_product = {
            int(getattr(product, "id")): _product_default_landed_unit_cost(product)
            for product in products
            if getattr(product, "id", None) is not None
        }
        fifo_unit_cost_by_sale: dict[int, float] = {}
        fifo_unit_cost_source_by_sale: dict[int, str] = {}
        if hasattr(repo, "report_sale_unit_cost_maps"):
            try:
                maps_payload = repo.report_sale_unit_cost_maps(
                    end_dt=now,
                    default_unit_cost_by_product=default_unit_cost_by_product,
                ) or {}
                fifo_unit_cost_by_sale = {
                    int(k): _safe_float(v)
                    for k, v in dict(maps_payload.get("fifo_unit_cost_by_sale") or {}).items()
                }
                fifo_unit_cost_source_by_sale = {
                    int(k): str(v or "").strip() or "unknown"
                    for k, v in dict(maps_payload.get("fifo_unit_cost_source_by_sale") or {}).items()
                }
            except Exception:
                db = getattr(repo, "db", None)
                if db is not None and hasattr(db, "rollback"):
                    db.rollback()
                fifo_unit_cost_by_sale = {}
                fifo_unit_cost_source_by_sale = {}
        cogs_24h = 0.0
        cogs_source_totals: dict[str, float] = {}
        cogs_source_counts: dict[str, int] = {}
        for sale in sales_24h:
            sale_id = getattr(sale, "id", None)
            product_id = int(getattr(sale, "product_id", 0) or 0)
            quantity = int(getattr(sale, "quantity_sold", 0) or 0)
            if sale_id is not None and int(sale_id) in fifo_unit_cost_by_sale:
                unit_cost = _safe_float(fifo_unit_cost_by_sale.get(int(sale_id)))
                cost_source = fifo_unit_cost_source_by_sale.get(int(sale_id), "unknown")
            else:
                unit_cost = _safe_float(default_unit_cost_by_product.get(product_id, 0.0))
                cost_source = "product_default_landed_cost" if unit_cost > 0 else "missing_cost_basis"
            sale_cogs = unit_cost * quantity
            cogs_24h += sale_cogs
            cogs_source_totals[cost_source] = cogs_source_totals.get(cost_source, 0.0) + sale_cogs
            cogs_source_counts[cost_source] = cogs_source_counts.get(cost_source, 0) + 1
        cogs_source_mix = _format_cogs_source_mix(cogs_source_totals, cogs_source_counts)
        cogs_evidence_split = build_cogs_evidence_split(cogs_source_totals, cogs_source_counts)
        cogs_evidence_split_text = format_cogs_evidence_split(cogs_evidence_split)
        cogs_source_mix_line = ""
        if cogs_evidence_split_text:
            cogs_source_mix_line += f"- COGS evidence split 24h: {cogs_evidence_split_text}\n"
        if cogs_source_mix:
            cogs_source_mix_line += f"- COGS source mix 24h: {cogs_source_mix}\n"
        actual_24h_rows = []
        if hasattr(repo, "report_sales_actual_econ_rows"):
            try:
                actual_24h_rows = list(repo.report_sales_actual_econ_rows(start_dt=since, end_dt=now) or [])
            except Exception:
                db = getattr(repo, "db", None)
                if db is not None and hasattr(db, "rollback"):
                    db.rollback()
                actual_24h_rows = []
        if actual_24h_rows:
            gross_24h = float(sum(float(row.get("sold_price") or 0.0) for row in actual_24h_rows))
            net_24h = float(sum(float(row.get("net_before_cogs_actual") or 0.0) for row in actual_24h_rows))
        else:
            gross_24h = float(sum(float(getattr(s, "sold_price", 0.0) or 0.0) for s in sales_24h))
            fees_24h = float(sum(float(getattr(s, "fees", 0.0) or 0.0) for s in sales_24h))
            shipping_24h = float(sum(float(getattr(s, "shipping_cost", 0.0) or 0.0) for s in sales_24h))
            label_spend_24h = float(
                sum(float(getattr(s, "shipping_label_cost", 0.0) or 0.0) for s in sales_24h)
            )
            net_24h = accounting_net_before_cogs(
                gross=gross_24h,
                shipping_charged=shipping_24h,
                fees=fees_24h,
                label_spend=label_spend_24h,
            )

        returns_24h_rows = []
        if hasattr(repo, "report_returns_rows"):
            try:
                returns_24h_rows = list(repo.report_returns_rows(start_dt=since, end_dt=now) or [])
            except Exception:
                db = getattr(repo, "db", None)
                if db is not None and hasattr(db, "rollback"):
                    db.rollback()
                returns_24h_rows = []
        returns_24h_count = len(returns_24h_rows)
        returns_refund_24h = round(
            sum(
                return_refund_total(
                    refund_amount=row.get("refund_amount"),
                    refund_fees=row.get("refund_fees"),
                    refund_shipping=row.get("refund_shipping"),
                )
                for row in returns_24h_rows
            ),
            2,
        )
        returns_cogs_reversal_24h = 0.0
        for row in returns_24h_rows:
            sale_id = row.get("sale_id")
            product_id = int(row.get("product_id") or 0)
            return_qty = max(1, int(row.get("quantity") or 1))
            if sale_id is not None and int(sale_id) in fifo_unit_cost_by_sale:
                returns_cogs_reversal_24h += _safe_float(fifo_unit_cost_by_sale.get(int(sale_id))) * return_qty
            elif product_id > 0:
                returns_cogs_reversal_24h += _safe_float(default_unit_cost_by_product.get(product_id)) * return_qty
        returns_cogs_reversal_24h = round(float(returns_cogs_reversal_24h), 2)
        returns_profit_impact_24h = accounting_returns_profit_impact(
            refund_total=returns_refund_24h,
            cogs_reversal=returns_cogs_reversal_24h,
        )
        profit_before_returns_24h = accounting_profit_before_returns(
            net_before_cogs_amount=net_24h,
            cogs=cogs_24h,
        )
        net_after_returns_24h = round(net_24h - returns_refund_24h, 2)
        estimated_profit_24h = accounting_profit_after_returns(
            profit_before_returns_amount=profit_before_returns_24h,
            returns_profit_impact_amount=returns_profit_impact_24h,
        )

        draft_listings = [
            l for l in listings if str(getattr(l, "listing_status", "") or "").strip().lower() == "draft"
        ]
        active_listings = [
            l for l in listings if str(getattr(l, "listing_status", "") or "").strip().lower() == "active"
        ]
        in_stock_products = [p for p in products if int(getattr(p, "current_quantity", 0) or 0) > 0]
        orders_24h = [o for o in orders if getattr(o, "order_date", None) and o.order_date >= since]
        normalized_fee_coverage_health = {
            "triggered": False,
            "threshold_percent": 0.0,
            "min_consecutive_weeks": 0,
            "consecutive_below": 0,
            "latest_week_start": "",
            "latest_week_coverage_pct": 0.0,
            "latest_week_total_sales": 0,
            "weekly_rows": [],
        }
        normalized_fee_coverage_error = ""
        try:
            lookback_weeks = max(
                2,
                int(get_runtime_int(repo, "slack_daily_report_normalized_fee_coverage_lookback_weeks", 8)),
            )
            threshold_percent = max(
                0.0,
                min(
                    100.0,
                    float(
                        get_runtime_float(
                            repo,
                            "slack_daily_report_normalized_fee_coverage_threshold_pct",
                            80.0,
                        )
                    ),
                ),
            )
            min_consecutive_weeks = max(
                1,
                int(get_runtime_int(repo, "slack_daily_report_normalized_fee_coverage_consecutive_weeks", 2)),
            )
            lookback_start = now - timedelta(days=int(lookback_weeks * 7))
            reconciliation_rows = repo.report_ebay_fee_reconciliation_rows(
                start_dt=lookback_start,
                end_dt=now,
            )
            normalized_fee_coverage_health = _normalized_fee_source_coverage_health(
                reconciliation_rows,
                threshold_percent=threshold_percent,
                min_consecutive_weeks=min_consecutive_weeks,
            )
        except Exception as health_exc:
            normalized_fee_coverage_error = str(health_exc)[:250]

        fee_coverage_alert_line = ""
        if bool(normalized_fee_coverage_health.get("triggered")):
            fee_coverage_alert_line = (
                "- :warning: Normalized fee coverage alert: {latest_week_coverage}% for week {latest_week_start} "
                "({consecutive_below}/{min_consecutive_weeks} weeks below {threshold_percent}%)\n"
            )

        default_template = (
            "*GoldenStackers Daily Ops Report* ({env})\n"
            "Date: {local_date}\n"
            "- Products: {product_count} (in stock: {in_stock_count})\n"
            "- Listings: {listing_count} (active: {active_count}, draft: {draft_count})\n"
            "- Sales total: {sale_count} | last 24h: {sales_24h_count}\n"
            "- Gross sales 24h: ${gross_24h}\n"
            "- Net sales 24h: ${net_24h}\n"
            "- Estimated COGS 24h: ${cogs_24h}\n"
            "- Profit before returns 24h: ${profit_before_returns_24h}\n"
            "- Return impact 24h: {returns_24h_count} return(s), refunds ${returns_refund_24h}, "
            "COGS reversal ${returns_cogs_reversal_24h}, profit impact ${returns_profit_impact_24h}\n"
            "- Estimated profit 24h after returns: ${estimated_profit_24h}\n"
            "{cogs_source_mix_line}"
            "- Orders total: {order_count} | last 24h: {orders_24h_count}\n"
            "- Inventory cost basis (snapshot): ${inventory_cost}\n"
            "{fee_coverage_alert_line}"
        )
        text = build_slack_alert_text(
            repo,
            event_type="daily_report",
            default_template=default_template,
            context={
                "env": settings.app_env,
                "local_date": local_date,
                "product_count": int(metrics.get("product_count", 0)),
                "in_stock_count": int(len(in_stock_products)),
                "listing_count": int(metrics.get("listing_count", 0)),
                "active_count": int(len(active_listings)),
                "draft_count": int(len(draft_listings)),
                "sale_count": int(metrics.get("sale_count", 0)),
                "sales_24h_count": int(len(sales_24h)),
                "gross_24h": f"{gross_24h:,.2f}",
                "net_24h": f"{net_24h:,.2f}",
                "cogs_24h": f"{cogs_24h:,.2f}",
                "profit_before_returns_24h": f"{profit_before_returns_24h:,.2f}",
                "returns_24h_count": int(returns_24h_count),
                "returns_refund_24h": f"{returns_refund_24h:,.2f}",
                "returns_cogs_reversal_24h": f"{returns_cogs_reversal_24h:,.2f}",
                "returns_profit_impact_24h": f"{returns_profit_impact_24h:,.2f}",
                "net_after_returns_24h": f"{net_after_returns_24h:,.2f}",
                "estimated_profit_24h": f"{estimated_profit_24h:,.2f}",
                "cogs_source_mix": cogs_source_mix,
                "cogs_source_mix_line": cogs_source_mix_line,
                "cogs_evidence_split": cogs_evidence_split,
                "cogs_evidence_split_text": cogs_evidence_split_text,
                "order_count": int(len(orders)),
                "orders_24h_count": int(len(orders_24h)),
                "inventory_cost": f"{float(metrics.get('inventory_cost', 0.0)):,.2f}",
                "fee_coverage_alert_line": fee_coverage_alert_line,
                "latest_week_coverage": f"{float(normalized_fee_coverage_health.get('latest_week_coverage_pct', 0.0)):.2f}",
                "latest_week_start": str(normalized_fee_coverage_health.get("latest_week_start") or "-"),
                "consecutive_below": int(normalized_fee_coverage_health.get("consecutive_below") or 0),
                "min_consecutive_weeks": int(normalized_fee_coverage_health.get("min_consecutive_weeks") or 0),
                "threshold_percent": f"{float(normalized_fee_coverage_health.get('threshold_percent', 0.0)):.2f}",
            },
        )
        override_channel = str(get_runtime_str(repo, "slack_daily_report_channel", "") or "").strip()
        if _notification_route_allows_slack(repo, route_key="notification_route_daily_report", default_route="slack"):
            dispatch = dispatch_slack_alert(
                repo,
                actor=settings.sync_runner_actor,
                text=text,
                event_type="daily_report",
                severity="info",
                override_channel=override_channel,
            )
        else:
            dispatch = {"status": "skipped_route", "channel": "", "ts": ""}
        _mark_daily_job_attempt(
            repo,
            key_prefix="slack_daily_report",
            actor=settings.sync_runner_actor,
            local_date=local_date,
            success=True,
        )
        repo.log_integration_event(
            actor=settings.sync_runner_actor,
            integration="slack",
            action="daily_report",
            status="success",
            details={
                "dispatch_status": str(dispatch.get("status") or ""),
                "channel": str(dispatch.get("channel") or override_channel),
                "local_time": local_now.isoformat(timespec="seconds"),
                "sales_24h_count": int(len(sales_24h)),
                "gross_24h": float(gross_24h),
                "net_24h": float(net_24h),
                "cogs_24h": float(cogs_24h),
                "profit_before_returns_24h": float(profit_before_returns_24h),
                "returns_24h_count": int(returns_24h_count),
                "returns_refund_24h": float(returns_refund_24h),
                "returns_cogs_reversal_24h": float(returns_cogs_reversal_24h),
                "returns_profit_impact_24h": float(returns_profit_impact_24h),
                "net_after_returns_24h": float(net_after_returns_24h),
                "estimated_profit_24h": float(estimated_profit_24h),
                "cogs_source_mix": cogs_source_mix,
                "cogs_source_totals": {
                    source: round(float(total), 2) for source, total in sorted(cogs_source_totals.items())
                },
                "cogs_evidence_split": cogs_evidence_split,
                "normalized_fee_coverage_triggered": bool(normalized_fee_coverage_health.get("triggered")),
                "normalized_fee_coverage_latest_week_start": str(
                    normalized_fee_coverage_health.get("latest_week_start") or ""
                ),
                "normalized_fee_coverage_latest_week_pct": float(
                    normalized_fee_coverage_health.get("latest_week_coverage_pct") or 0.0
                ),
                "normalized_fee_coverage_consecutive_below": int(
                    normalized_fee_coverage_health.get("consecutive_below") or 0
                ),
                "normalized_fee_coverage_threshold_pct": float(
                    normalized_fee_coverage_health.get("threshold_percent") or 0.0
                ),
                "normalized_fee_coverage_error": normalized_fee_coverage_error,
            },
        )
        _log(
            "Sent daily Slack report: "
            f"status={dispatch.get('status')} channel={dispatch.get('channel') or override_channel or '-'}"
        )
    except Exception as exc:
        try:
            repo = InventoryRepository(db)
            due, local_now, local_date = _is_daily_job_due(
                repo,
                key_prefix="slack_daily_report",
                timezone_key="slack_daily_report_timezone",
                local_time_key="slack_daily_report_local_time",
                default_timezone=_app_default_timezone(repo),
                default_local_time="08:00",
            )
            _mark_daily_job_attempt(
                repo,
                key_prefix="slack_daily_report",
                actor=settings.sync_runner_actor,
                local_date=local_date,
                success=False,
            )
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="slack",
                action="daily_report",
                status="error",
                details={
                    "error": str(exc)[:500],
                    "local_time": local_now.isoformat(timespec="seconds"),
                },
            )
        except Exception:
            pass
        _log(f"Daily Slack report failed: {exc}")
    finally:
        db.close()


def _run_notification_outbox() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "notification_outbox_runner_enabled", True):
            return
        limit = max(1, min(500, int(get_runtime_int(repo, "notification_outbox_runner_limit", 50))))
        result = process_due_notification_outbox(
            repo,
            environment=settings.app_env,
            actor=settings.sync_runner_actor,
            limit=limit,
        )
        repo.log_integration_event(
            actor=settings.sync_runner_actor,
            integration="notification_outbox",
            action="process_due",
            status="success",
            details={
                "due": int(result.get("due") or 0),
                "sent": int(result.get("sent") or 0),
                "failed": int(result.get("failed") or 0),
                "limit": int(limit),
            },
        )
    except Exception as exc:
        try:
            repo = InventoryRepository(db)
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="notification_outbox",
                action="process_due",
                status="error",
                details={"error": str(exc)[:500]},
            )
        except Exception:
            pass
        _log(f"Notification outbox processing failed: {exc}")
    finally:
        db.close()


def _run_notification_outbox_cleanup() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "notification_outbox_cleanup_enabled", True):
            return
        due, local_now, local_date = _is_daily_job_due(
            repo,
            key_prefix="notification_outbox_cleanup",
            timezone_key="notification_outbox_cleanup_timezone",
            local_time_key="notification_outbox_cleanup_local_time",
            default_timezone=_app_default_timezone(repo),
            default_local_time="03:15",
        )
        if not due:
            return
        result = cleanup_notification_outbox_retention(
            repo,
            environment=settings.app_env,
        )
        _mark_daily_job_attempt(
            repo,
            key_prefix="notification_outbox_cleanup",
            actor=settings.sync_runner_actor,
            local_date=local_date,
            success=True,
        )
        repo.log_integration_event(
            actor=settings.sync_runner_actor,
            integration="notification_outbox",
            action="cleanup",
            status="success",
            details={
                "deleted_total": int(result.get("deleted_total") or 0),
                "deleted_sent": int(result.get("deleted_sent") or 0),
                "deleted_failed": int(result.get("deleted_failed") or 0),
                "local_time": local_now.isoformat(timespec="seconds"),
            },
        )
    except Exception as exc:
        try:
            repo = InventoryRepository(db)
            _mark_daily_job_attempt(
                repo,
                key_prefix="notification_outbox_cleanup",
                actor=settings.sync_runner_actor,
                local_date=datetime.now(UTC).date(),
                success=False,
            )
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="notification_outbox",
                action="cleanup",
                status="error",
                details={"error": str(exc)[:500]},
            )
        except Exception:
            pass
        _log(f"Notification outbox cleanup failed: {exc}")
    finally:
        db.close()


def _run_ai_accountant_monitor_schedule() -> None:
    db = SessionLocal()
    schedule_mode = "interval"
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "ai_accountant_monitor_enabled", True):
            _log(f"{AI_ACCOUNTANT_LABEL} monitor disabled: `ai_accountant_monitor_enabled=false`.")
            return
        schedule_mode = str(get_runtime_str(repo, "ai_accountant_monitor_schedule_mode", "interval") or "interval").strip().lower()
        if schedule_mode == "daily":
            due, local_now, local_date = _is_daily_job_due(
                repo,
                key_prefix="ai_accountant_monitor",
                timezone_key="ai_accountant_monitor_timezone",
                local_time_key="ai_accountant_monitor_local_time",
                default_timezone=_app_default_timezone(repo),
                default_local_time="08:30",
            )
        else:
            due, local_now, local_date = _is_interval_job_due(
                repo,
                key_prefix="ai_accountant_monitor",
                timezone_key="ai_accountant_monitor_timezone",
                interval_hours_key="ai_accountant_monitor_interval_hours",
                default_timezone=_app_default_timezone(repo),
                default_interval_hours=6,
            )
        if not due:
            _log(
                f"Skipping {AI_ACCOUNTANT_LABEL} monitor (not due): "
                f"local_now={local_now.isoformat(timespec='seconds')}"
            )
            return
        result = run_ai_accountant_monitor(
            repo,
            actor=settings.sync_runner_actor,
            lookback_days=max(1, int(get_runtime_int(repo, "ai_accountant_monitor_lookback_days", 30))),
            min_severity=str(get_runtime_str(repo, "ai_accountant_monitor_min_severity", "P1") or "P1"),
            slack_enabled=(
                get_runtime_bool(repo, "ai_accountant_monitor_slack_enabled", True)
                and _notification_route_allows_slack(
                    repo,
                    route_key="notification_route_ai_accountant_monitor",
                    default_route="slack",
                )
            ),
            slack_channel=str(get_runtime_str(repo, "ai_accountant_monitor_channel", "") or "").strip(),
            record_when_empty=get_runtime_bool(repo, "ai_accountant_monitor_record_empty", False),
        )
        if schedule_mode == "daily":
            _mark_daily_job_attempt(
                repo,
                key_prefix="ai_accountant_monitor",
                actor=settings.sync_runner_actor,
                local_date=local_date,
                success=True,
            )
        else:
            _mark_interval_job_attempt(
                repo,
                key_prefix="ai_accountant_monitor",
                actor=settings.sync_runner_actor,
                local_now=local_now,
                success=True,
            )
        repo.log_integration_event(
            actor=settings.sync_runner_actor,
            integration="ai_accountant",
            action="monitor",
            status="success",
            details={
                "local_time": local_now.isoformat(timespec="seconds"),
                "item_count": int(result.get("item_count") or 0),
                "actionable_count": int(result.get("actionable_count") or 0),
                "audit_id": result.get("audit_id"),
                "slack_outbox_id": result.get("slack_outbox_id"),
                "period_label": str(result.get("period_label") or ""),
                "schedule_mode": schedule_mode,
                "min_severity": str(result.get("min_severity") or ""),
                "requested_min_severity": str(result.get("requested_min_severity") or ""),
                "review_enabled": bool(result.get("review_enabled")),
                "review_status": (
                    "unavailable"
                    if str(result.get("review_error") or "").strip()
                    else ("completed" if str(result.get("review_hash") or "").strip() else "not_run")
                ),
                "review_hash": str(result.get("review_hash") or "")[:12],
                "review_error": str(result.get("review_error") or "")[:500],
                "review_compact_retry": bool(result.get("review_compact_retry")),
                "review_runtime_route": str(result.get("review_runtime_route") or "")[:500],
            },
        )
        _log(
            f"Completed {AI_ACCOUNTANT_LABEL} monitor: "
            f"items={result.get('item_count')} actionable={result.get('actionable_count')} "
            f"audit_id={result.get('audit_id') or '-'} slack_outbox_id={result.get('slack_outbox_id') or '-'} "
            f"review_status="
            f"{'unavailable' if str(result.get('review_error') or '').strip() else ('completed' if str(result.get('review_hash') or '').strip() else 'not_run')}"
        )
    except Exception as exc:
        try:
            repo = InventoryRepository(db)
            if schedule_mode == "daily":
                _due, local_now, local_date = _is_daily_job_due(
                    repo,
                    key_prefix="ai_accountant_monitor",
                    timezone_key="ai_accountant_monitor_timezone",
                    local_time_key="ai_accountant_monitor_local_time",
                    default_timezone=_app_default_timezone(repo),
                    default_local_time="08:30",
                )
                _mark_daily_job_attempt(
                    repo,
                    key_prefix="ai_accountant_monitor",
                    actor=settings.sync_runner_actor,
                    local_date=local_date,
                    success=False,
                )
            else:
                _due, local_now, _local_date = _is_interval_job_due(
                    repo,
                    key_prefix="ai_accountant_monitor",
                    timezone_key="ai_accountant_monitor_timezone",
                    interval_hours_key="ai_accountant_monitor_interval_hours",
                    default_timezone=_app_default_timezone(repo),
                    default_interval_hours=6,
                )
                _mark_interval_job_attempt(
                    repo,
                    key_prefix="ai_accountant_monitor",
                    actor=settings.sync_runner_actor,
                    local_now=local_now,
                    success=False,
                )
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="ai_accountant",
                action="monitor",
                status="error",
                details={
                    "error": str(exc)[:500],
                    "local_time": local_now.isoformat(timespec="seconds"),
                    "schedule_mode": schedule_mode,
                },
            )
        except Exception:
            pass
        _log(f"{AI_ACCOUNTANT_LABEL} monitor failed: {exc}")
    finally:
        db.close()


def _run_lifecycle_archive_cleanup() -> None:
    db = SessionLocal()
    try:
        repo = InventoryRepository(db)
        if not get_runtime_bool(repo, "lifecycle_archive_cleanup_enabled", False):
            return
        due, local_now, local_date = _is_daily_job_due(
            repo,
            key_prefix="lifecycle_archive_cleanup",
            timezone_key="lifecycle_archive_cleanup_timezone",
            local_time_key="lifecycle_archive_cleanup_local_time",
            default_timezone=_app_default_timezone(repo),
            default_local_time="03:45",
        )
        if not due:
            return
        result = cleanup_lifecycle_retention(
            repo,
            actor=settings.sync_runner_actor,
        )
        _mark_daily_job_attempt(
            repo,
            key_prefix="lifecycle_archive_cleanup",
            actor=settings.sync_runner_actor,
            local_date=local_date,
            success=True,
        )
        repo.log_integration_event(
            actor=settings.sync_runner_actor,
            integration="lifecycle_retention",
            action="cleanup",
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
                "skipped_lots_with_dependencies": int(result.get("skipped_lots_with_dependencies") or 0),
                "skipped_products_with_dependencies": int(
                    result.get("skipped_products_with_dependencies") or 0
                ),
                "local_time": local_now.isoformat(timespec="seconds"),
            },
        )
    except Exception as exc:
        try:
            repo = InventoryRepository(db)
            _mark_daily_job_attempt(
                repo,
                key_prefix="lifecycle_archive_cleanup",
                actor=settings.sync_runner_actor,
                local_date=datetime.now(UTC).date(),
                success=False,
            )
            repo.log_integration_event(
                actor=settings.sync_runner_actor,
                integration="lifecycle_retention",
                action="cleanup",
                status="error",
                details={"error": str(exc)[:500]},
            )
        except Exception:
            pass
        _log(f"Lifecycle archive cleanup failed: {exc}")
    finally:
        db.close()


def run_once() -> None:
    _run_ebay_token_auto_refresh()
    _run_ebay_connection_health_check()
    _run_ebay_store_categories_sync()
    _run_ebay_orders_pull_import()
    _run_governance_snapshot_schedule()
    _run_scheduled_db_backup()
    _run_daily_slack_report()
    _run_ai_accountant_monitor_schedule()
    _run_notification_outbox()
    _run_notification_outbox_cleanup()
    _run_lifecycle_archive_cleanup()


def run_forever() -> None:
    if not settings.sync_runner_enabled:
        _log("SYNC_RUNNER_ENABLED=false; exiting without starting scheduler.")
        return

    interval = max(30, int(settings.sync_runner_interval_seconds))
    _log(
        "Starting scheduler loop: "
        f"interval={interval}s actor={settings.sync_runner_actor} "
        f"run_once={settings.sync_runner_run_once}"
    )

    while True:
        run_once()
        if settings.sync_runner_run_once:
            _log("SYNC_RUNNER_RUN_ONCE=true; exiting after one pass.")
            break
        _log(f"Sleeping {interval}s before next pass.")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
