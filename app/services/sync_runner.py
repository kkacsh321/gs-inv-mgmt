from __future__ import annotations

import time
from datetime import datetime, UTC, timedelta

from sqlalchemy import select

from app.config import settings
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.repository import InventoryRepository
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.sync_jobs import execute_sync_job, is_sync_job_enabled
from app.utils.time import utcnow_naive


def _log(message: str) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"[sync-runner] {stamp} {message}", flush=True)


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


def run_once() -> None:
    _run_ebay_orders_pull_import()
    _run_governance_snapshot_schedule()


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
