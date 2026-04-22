from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.config import settings
from app.services.runtime_settings import get_runtime_int
from app.services.slack_notify import send_slack_message
from app.utils.time import utcnow_naive


def _calc_backoff_seconds(repo: Any, retry_count: int) -> int:
    base_seconds = max(
        10,
        min(3600, int(get_runtime_int(repo, "notification_outbox_backoff_base_seconds", 60))),
    )
    max_seconds = max(
        base_seconds,
        min(86400, int(get_runtime_int(repo, "notification_outbox_backoff_max_seconds", 3600))),
    )
    delay = base_seconds * (2 ** max(0, int(retry_count)))
    return min(max_seconds, delay)


def _deliver_outbox_row(repo: Any, row: Any) -> tuple[bool, str]:
    channel = str(getattr(row, "channel", "") or "").strip().lower()
    payload_raw = str(getattr(row, "payload_json", "") or "{}")
    try:
        payload = json.loads(payload_raw)
    except Exception:
        payload = {}

    if channel == "slack":
        text = str(payload.get("text") or "").strip()
        target_channel = str(payload.get("channel") or "").strip()
        if not text:
            return False, "Missing slack payload text."
        try:
            send_slack_message(repo, text=text, channel=target_channel)
            return True, "Delivered to Slack."
        except Exception as exc:
            return False, str(exc) or "Slack delivery failed."

    if channel == "email":
        return False, "Email outbox dispatch is not implemented yet."

    return False, f"Unsupported notification channel `{channel}`."


def process_notification_outbox_row(
    repo: Any,
    *,
    outbox_id: int,
    actor: str = "system",
) -> tuple[bool, str]:
    row = None
    get_row = getattr(repo, "get_notification_outbox", None)
    if callable(get_row):
        row = get_row(int(outbox_id), environment=settings.app_env)
    if row is None:
        rows = repo.list_notification_outbox(
            environment=settings.app_env,
            statuses={"queued", "retrying", "processing", "failed", "sent"},
            limit=1000,
        )
        row = next((r for r in rows if int(getattr(r, "id", 0) or 0) == int(outbox_id)), None)
    if row is None:
        return False, "Outbox row not found."

    status = str(getattr(row, "status", "") or "").strip().lower()
    now = utcnow_naive()
    if status == "sent":
        return True, "Already sent."
    next_attempt_at = getattr(row, "next_attempt_at", None)
    if next_attempt_at is not None and next_attempt_at > now and status not in {"processing"}:
        return False, "Not due yet."

    repo.update_notification_outbox(
        int(row.id),
        {
            "status": "processing",
            "locked_by": (actor or "system").strip() or "system",
            "locked_at": now,
        },
        actor=actor,
    )

    ok, message = _deliver_outbox_row(repo, row)
    if ok:
        repo.update_notification_outbox(
            int(row.id),
            {
                "status": "sent",
                "attempt_count": int(getattr(row, "attempt_count", 0) or 0) + 1,
                "last_attempt_at": now,
                "dispatched_at": now,
                "last_error": "",
                "locked_by": "",
                "locked_at": None,
            },
            actor=actor,
        )
        return True, message

    attempts = int(getattr(row, "attempt_count", 0) or 0) + 1
    max_attempts = max(1, int(getattr(row, "max_attempts", 6) or 6))
    terminal = attempts >= max_attempts
    updates = {
        "status": "failed" if terminal else "retrying",
        "attempt_count": attempts,
        "last_attempt_at": now,
        "last_error": str(message or "")[:1000],
        "locked_by": "",
        "locked_at": None,
    }
    if not terminal:
        updates["next_attempt_at"] = now + timedelta(seconds=_calc_backoff_seconds(repo, attempts))
    repo.update_notification_outbox(int(row.id), updates, actor=actor)
    return False, str(message or "")


def process_due_notification_outbox(
    repo: Any,
    *,
    environment: str,
    actor: str = "system",
    limit: int = 50,
) -> dict[str, int]:
    now = utcnow_naive()
    rows = repo.list_notification_outbox(
        environment=environment,
        statuses={"queued", "retrying"},
        due_before=now,
        limit=max(1, int(limit)),
    )
    sent = 0
    failed = 0
    for row in rows:
        ok, _ = process_notification_outbox_row(repo, outbox_id=int(row.id), actor=actor)
        if ok:
            sent += 1
        else:
            failed += 1
    return {
        "due": len(rows),
        "sent": sent,
        "failed": failed,
    }


def cleanup_notification_outbox_retention(
    repo: Any,
    *,
    environment: str,
) -> dict[str, int]:
    retain_sent_days = max(1, int(get_runtime_int(repo, "notification_outbox_retain_sent_days", 14)))
    retain_failed_days = max(1, int(get_runtime_int(repo, "notification_outbox_retain_failed_days", 30)))
    return repo.cleanup_notification_outbox(
        environment=environment,
        retain_sent_days=retain_sent_days,
        retain_failed_days=retain_failed_days,
        actor="sync_runner",
    )
