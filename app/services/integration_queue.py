import base64
import json
import traceback
from datetime import timedelta
from typing import Any

from app.config import settings
from app.db.models import IntegrationQueueJob, Sale
from app.services.google_workspace import (
    create_calendar_event,
    resolve_google_workspace_config,
    send_gmail_message,
    upload_drive_file,
)
from app.services.runtime_settings import get_runtime_bool, get_runtime_int
from app.services.integration_automation import evaluate_and_apply_rules_for_job
from app.services.shipping_labels import purchase_shipping_label
from app.services.slack_notify import (
    build_slack_alert_text,
    dispatch_slack_alert,
    resolve_slack_notify_config,
    send_slack_message,
)
from app.utils.time import utcnow_naive


def _calc_backoff_seconds(repo: Any, retry_count: int, *, integration: str) -> int:
    normalized = str(integration or "").strip().lower()
    if normalized == "slack":
        base_key = "slack_queue_backoff_base_seconds"
        max_key = "slack_queue_backoff_max_seconds"
        base_default = 60
        max_default = 3600
    elif normalized == "shipping":
        base_key = "shipping_queue_backoff_base_seconds"
        max_key = "shipping_queue_backoff_max_seconds"
        base_default = 60
        max_default = 3600
    else:
        base_key = "google_queue_backoff_base_seconds"
        max_key = "google_queue_backoff_max_seconds"
        base_default = 120
        max_default = 3600
    base_seconds = max(5, min(3600, get_runtime_int(repo, base_key, base_default)))
    max_seconds = max(base_seconds, min(86400, get_runtime_int(repo, max_key, max_default)))
    seconds = base_seconds * (2 ** max(0, int(retry_count)))
    return min(max_seconds, seconds)


def _capture_queue_execute_exception(
    repo: Any,
    *,
    actor: str,
    job: IntegrationQueueJob,
    exc: Exception,
) -> str:
    message = str(exc)[:2000] or exc.__class__.__name__
    try:
        repo.log_integration_event(
            actor=actor,
            integration=f"{job.integration}_queue",
            action=f"{job.action}_execute_exception",
            status="error",
            details={
                "queue_job_id": int(job.id),
                "retry_count": int(job.retry_count or 0),
                "error": message[:500],
                "exception_type": exc.__class__.__name__,
                "traceback": traceback.format_exc(limit=25)[:4000],
            },
        )
    except Exception:
        pass
    return message


def _emit_terminal_queue_failure_alert(
    repo: Any,
    *,
    actor: str,
    job: IntegrationQueueJob,
    retry_count: int,
    error_text: str,
) -> None:
    try:
        slack_cfg = resolve_slack_notify_config(repo)
        if not slack_cfg.enabled:
            return
        enabled_general = get_runtime_bool(repo, "slack_notify_integration_queue_failures", True)
        enabled_google_legacy = get_runtime_bool(repo, "slack_notify_google_queue_failures", True)
        if str(job.integration or "").strip().lower() == "google":
            if not (enabled_general or enabled_google_legacy):
                return
        elif not enabled_general:
            return
        alert_text = build_slack_alert_text(
            repo,
            event_type="integration_queue_failures",
            default_template=(
                ":warning: *GoldenStackers* integration queue job failed permanently\n"
                "- Env: `{env}`\n"
                "- Integration: `{integration}`\n"
                "- Job: `#{job_id}` `{action}`\n"
                "- Retries: `{retry_count}/{max_retries}`\n"
                "- Error: `{error}`"
            ),
            context={
                "env": settings.app_env,
                "integration": str(job.integration or ""),
                "job_id": int(job.id),
                "action": str(job.action or ""),
                "retry_count": int(retry_count),
                "max_retries": int(job.max_retries or 0),
                "error": str(error_text or "")[:280],
            },
        )
        dispatch_slack_alert(
            repo,
            actor=actor,
            event_type="integration_queue_failures",
            severity="error",
            text=alert_text,
        )
    except Exception:
        pass


def execute_integration_queue_job(repo: Any, job: Any, *, actor: str) -> tuple[bool, str]:
    integration = str(getattr(job, "integration", "") or "").strip().lower()
    action = str(getattr(job, "action", "") or "").strip().lower()
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(str(getattr(job, "payload_json", "") or "{}"))
    except Exception:
        payload = {}

    if integration != "google":
        if integration == "slack":
            if action != "post_message":
                return False, f"Unsupported slack action `{action}`."
            send_slack_message(
                repo,
                text=str(payload.get("text") or ""),
                channel=str(payload.get("channel") or ""),
            )
            return True, "Slack post completed."
        if integration == "shipping":
            if action != "purchase_label":
                return False, f"Unsupported shipping action `{action}`."
            if not get_runtime_bool(repo, "shipping_queue_enabled", True):
                return False, "Shipping queue is disabled by runtime setting."
            if not get_runtime_bool(repo, "shipping_label_purchase_enabled", True):
                return False, "Shipping label purchase is disabled by runtime setting."
            sale_id_raw = payload.get("sale_id")
            try:
                sale_id = int(sale_id_raw)
            except Exception:
                return False, "Missing/invalid `sale_id` payload."
            sale = repo.db.get(Sale, sale_id)
            if sale is None:
                return False, f"Sale `{sale_id}` not found."

            dry_run = bool(payload.get("dry_run", False))
            provider = str(payload.get("shipping_provider") or "").strip()
            provider_key = provider.replace(" ", "_").lower() or "other"
            provider_enabled_key = f"shipping_label_provider_{provider_key}_enabled"
            if not get_runtime_bool(repo, provider_enabled_key, True):
                return False, f"Shipping label provider `{provider or 'other'}` is disabled by runtime setting."
            service = str(payload.get("shipping_service") or "").strip()
            package_type = str(payload.get("shipping_package_type") or "").strip()
            tracking_number = str(payload.get("tracking_number") or "").strip()
            label_id = str(payload.get("shipping_label_id") or "").strip()
            label_url = str(payload.get("shipping_label_url") or "").strip()
            label_currency = str(payload.get("shipping_label_currency") or "USD").strip() or "USD"
            label_cost_raw = payload.get("shipping_label_cost")
            label_cost = None
            if label_cost_raw not in (None, ""):
                try:
                    label_cost = float(label_cost_raw)
                except Exception:
                    label_cost = None
            live_provider_calls = get_runtime_bool(repo, "shipping_label_live_provider_calls_enabled", False)

            if dry_run:
                return True, "Shipping label dry-run completed (no sale fields updated)."
            if live_provider_calls:
                provider_result = purchase_shipping_label(repo, provider=provider, payload=payload)
                if not label_id:
                    label_id = str(provider_result.label_id or "").strip()
                if not label_url:
                    label_url = str(provider_result.label_url or "").strip()
                if provider_result.label_cost is not None:
                    label_cost = float(provider_result.label_cost)
                if provider_result.label_currency:
                    label_currency = str(provider_result.label_currency).strip() or label_currency
                if not tracking_number:
                    tracking_number = str(provider_result.tracking_number or "").strip()

            updates: dict[str, Any] = {}
            if provider:
                updates["shipping_provider"] = provider
            if service:
                updates["shipping_service"] = service
            if package_type:
                updates["shipping_package_type"] = package_type
            if tracking_number:
                updates["tracking_number"] = tracking_number
            current_status = str(getattr(sale, "tracking_status", "") or "").strip().lower()
            if current_status in {"", "label_created"}:
                updates["tracking_status"] = "label_created"
            if not label_id:
                provider_token = provider.replace(" ", "_").lower() or "carrier"
                label_id = f"{provider_token}-LBL-{int(job.id)}-{int(sale.id)}"
            if not label_url:
                label_url = f"https://labels.goldenstackers.local/{provider or 'carrier'}/{label_id}.pdf"
            updates["shipping_label_id"] = label_id
            updates["shipping_label_url"] = label_url
            updates["shipping_label_currency"] = label_currency
            updates["shipping_label_purchased_at"] = utcnow_naive()
            updates["shipping_label_cost"] = label_cost
            if updates:
                repo.update_sale(int(sale.id), updates, actor=actor)
            return (
                True,
                "Shipping label purchase completed."
                if live_provider_calls
                else "Shipping label purchase scaffold completed.",
            )
        return False, f"Unsupported integration `{integration}`."

    cfg = resolve_google_workspace_config(repo)
    if action == "gmail_send_document_email":
        send_gmail_message(
            config=cfg,
            to_email=str(payload.get("to_email") or ""),
            subject=str(payload.get("subject") or ""),
            body_html=str(payload.get("body_html") or ""),
            body_text=str(payload.get("body_text") or ""),
        )
        return True, "Gmail send completed."

    if action == "calendar_create_event":
        create_calendar_event(
            config=cfg,
            summary=str(payload.get("summary") or ""),
            start_iso=str(payload.get("start_iso") or ""),
            end_iso=str(payload.get("end_iso") or ""),
            description=str(payload.get("description") or ""),
            timezone=str(payload.get("timezone") or cfg.default_timezone or "America/Denver"),
            calendar_id=str(payload.get("calendar_id") or cfg.default_calendar_id or "primary"),
        )
        return True, "Calendar event created."

    if action == "drive_upload_artifact":
        file_b64 = str(payload.get("file_b64") or "")
        if not file_b64:
            return False, "Missing `file_b64` payload."
        file_bytes = base64.b64decode(file_b64)
        upload_drive_file(
            config=cfg,
            file_name=str(payload.get("file_name") or ""),
            file_bytes=file_bytes,
            mime_type=str(payload.get("mime_type") or "application/octet-stream"),
            folder_id=str(payload.get("folder_id") or ""),
        )
        return True, "Drive upload completed."

    return False, f"Unsupported integration action `{action}`."


def process_integration_queue_job(repo: Any, *, job_id: int, actor: str) -> tuple[bool, str]:
    job = repo.db.get(IntegrationQueueJob, int(job_id))
    if job is None:
        raise ValueError(f"Integration queue job {job_id} not found.")

    now = utcnow_naive()
    repo.update_integration_queue_job(
        int(job.id),
        {
            "status": "running",
            "last_attempt_at": now,
        },
        actor=actor,
    )
    job = repo.db.get(IntegrationQueueJob, int(job.id))

    try:
        ok, message = execute_integration_queue_job(repo, job, actor=actor)
    except Exception as exc:
        ok, message = False, _capture_queue_execute_exception(repo, actor=actor, job=job, exc=exc)

    if ok:
        repo.update_integration_queue_job(
            int(job.id),
            {
                "status": "success",
                "completed_at": utcnow_naive(),
                "last_error": "",
            },
            actor=actor,
        )
        repo.log_integration_event(
            actor=actor,
            integration=f"{job.integration}_queue",
            action=f"{job.action}_retry_execute",
            status="success",
            details={"queue_job_id": int(job.id), "retry_count": int(job.retry_count or 0)},
        )
        return True, message

    next_retry = int(job.retry_count or 0) + 1
    exceeded = next_retry > int(job.max_retries or 0)
    if exceeded:
        repo.update_integration_queue_job(
            int(job.id),
            {
                "status": "failed",
                "retry_count": next_retry,
                "last_error": message[:2000],
            },
            actor=actor,
        )
        repo.log_integration_event(
            actor=actor,
            integration=f"{job.integration}_queue",
            action=f"{job.action}_retry_execute",
            status="failed",
            details={"queue_job_id": int(job.id), "retry_count": next_retry, "error": message[:500]},
        )
        _emit_terminal_queue_failure_alert(
            repo,
            actor=actor,
            job=job,
            retry_count=next_retry,
            error_text=message,
        )
        return False, message

    backoff_seconds = _calc_backoff_seconds(repo, next_retry, integration=str(job.integration or ""))
    repo.update_integration_queue_job(
        int(job.id),
        {
            "status": "queued",
            "retry_count": next_retry,
            "next_attempt_at": utcnow_naive() + timedelta(seconds=backoff_seconds),
            "last_error": message[:2000],
        },
        actor=actor,
    )
    repo.log_integration_event(
        actor=actor,
        integration=f"{job.integration}_queue",
        action=f"{job.action}_retry_execute",
        status="queued",
        details={
            "queue_job_id": int(job.id),
            "retry_count": next_retry,
            "next_attempt_in_seconds": backoff_seconds,
            "error": message[:500],
        },
    )
    return False, message


def process_due_google_queue_jobs(repo: Any, *, actor: str, limit: int = 10) -> dict[str, int]:
    return process_due_integration_queue_jobs(
        repo,
        integration="google",
        actor=actor,
        limit=limit,
    )


def process_due_integration_queue_jobs(
    repo: Any,
    *,
    integration: str,
    actor: str,
    limit: int = 10,
) -> dict[str, int]:
    jobs = repo.list_integration_queue_jobs(
        environment=settings.app_env,
        integration=str(integration or "").strip().lower(),
        statuses={"queued"},
        limit=max(1, min(int(limit), 100)),
    )
    now = utcnow_naive()
    due = [row for row in jobs if row.next_attempt_at is None or row.next_attempt_at <= now]
    summary = {
        "processed": 0,
        "success": 0,
        "queued": 0,
        "failed": 0,
        "blocked": 0,
        "rules_matched": 0,
        "rules_applied": 0,
        "rules_approval_gated": 0,
    }
    for row in due:
        refreshed_job = repo.db.get(IntegrationQueueJob, int(row.id))
        if refreshed_job is None:
            continue
        rule_result = evaluate_and_apply_rules_for_job(
            repo,
            job=refreshed_job,
            actor=actor,
            trigger_status="queued",
        )
        summary["rules_matched"] += len(rule_result.get("matched_rule_ids") or [])
        summary["rules_applied"] += len(rule_result.get("applied_rule_ids") or [])
        summary["rules_approval_gated"] += len(rule_result.get("approval_gated_rule_ids") or [])
        if bool(rule_result.get("blocked")):
            summary["blocked"] += 1
            try:
                repo.update_integration_queue_job(
                    int(refreshed_job.id),
                    {
                        "last_error": str(rule_result.get("blocked_reason") or "Blocked by automation rule.")[:2000],
                    },
                    actor=actor,
                )
            except Exception:
                pass
            continue

        ok, _ = process_integration_queue_job(repo, job_id=int(row.id), actor=actor)
        summary["processed"] += 1
        if ok:
            summary["success"] += 1
        else:
            refreshed = repo.db.get(IntegrationQueueJob, int(row.id))
            status = str(getattr(refreshed, "status", "") or "").strip().lower()
            if status == "queued":
                summary["queued"] += 1
            else:
                summary["failed"] += 1
    return summary
