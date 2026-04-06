from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.repository import InventoryRepository
from app.services.runtime_settings import get_runtime_str
from app.utils.time import utcnow_naive


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def summarize_ebay_connection_status(repo: InventoryRepository) -> dict[str, Any]:
    now = utcnow_naive()
    access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    refresh_token = get_runtime_str(repo, "ebay_user_refresh_token", settings.ebay_user_refresh_token).strip()
    expires_at_raw = get_runtime_str(repo, "ebay_user_access_token_expires_at", "").strip()
    expires_at = _parse_iso(expires_at_raw)

    latest_verify_success = None
    latest_verify_error = None
    try:
        for row in repo.list_audit_logs(limit=500):
            if str(getattr(row, "entity_type", "") or "").strip().lower() != "ebay_verify":
                continue
            payload = getattr(row, "changes", None)
            if not isinstance(payload, dict):
                payload = {}
            status = str(payload.get("status") or "").strip().lower()
            if status == "success" and latest_verify_success is None:
                latest_verify_success = {
                    "at": getattr(row, "created_at", None),
                    "actor": str(getattr(row, "actor", "") or "").strip(),
                    "resolved_user": str(payload.get("resolved_user") or "").strip(),
                    "seller_registered": bool(payload.get("seller_registered"))
                    if payload.get("seller_registered") is not None
                    else None,
                    "message": str(payload.get("message") or "").strip(),
                }
            if status == "error" and latest_verify_error is None:
                latest_verify_error = {
                    "at": getattr(row, "created_at", None),
                    "actor": str(getattr(row, "actor", "") or "").strip(),
                    "message": str(payload.get("message") or "").strip(),
                }
            if latest_verify_success is not None and latest_verify_error is not None:
                break
    except Exception:
        latest_verify_success = None
        latest_verify_error = None

    health_runs = [
        row
        for row in repo.list_sync_runs(provider="ebay", limit=500)
        if str(getattr(row, "job_name", "") or "").strip().lower() == "ebay_connection_health_check"
    ]
    latest_health_run = health_runs[0] if health_runs else None
    latest_health_status = str(getattr(latest_health_run, "status", "") or "").strip().lower()
    latest_health_completed_at = getattr(latest_health_run, "completed_at", None)

    is_access_token_expired = False
    expires_in_minutes: int | None = None
    if expires_at is not None:
        delta = expires_at - now
        expires_in_minutes = int(delta.total_seconds() // 60)
        is_access_token_expired = expires_in_minutes <= 0

    health_interval_minutes = 30
    try:
        from app.services.runtime_settings import get_runtime_int

        health_interval_minutes = max(
            5,
            int(
                get_runtime_int(
                    repo,
                    "sync_job_ebay_connection_health_check_interval_minutes",
                    int(getattr(settings, "sync_job_ebay_connection_health_check_interval_minutes", 30)),
                )
            ),
        )
    except Exception:
        health_interval_minutes = 30

    health_stale = True
    if latest_health_completed_at is not None:
        health_stale = (now - latest_health_completed_at).total_seconds() > float(health_interval_minutes * 60 * 2)

    return {
        "token_present": bool(access_token),
        "refresh_token_present": bool(refresh_token),
        "token_expires_at": expires_at,
        "token_expires_in_minutes": expires_in_minutes,
        "token_expired": bool(is_access_token_expired),
        "latest_verify_success": latest_verify_success,
        "latest_verify_error": latest_verify_error,
        "latest_health_run_id": int(getattr(latest_health_run, "id", 0) or 0),
        "latest_health_status": latest_health_status or "",
        "latest_health_completed_at": latest_health_completed_at,
        "latest_health_notes": str(getattr(latest_health_run, "notes", "") or "").strip(),
        "health_interval_minutes": int(health_interval_minutes),
        "health_stale": bool(health_stale),
    }

