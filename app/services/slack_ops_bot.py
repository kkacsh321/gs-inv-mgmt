from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.db.models import IntegrationQueueJob
from app.services.ai_accountant_monitor import parse_ai_accountant_answer_prompt, record_ai_accountant_answer
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.workflow_contracts import parse_ai_agent_answer_prompt
from app.utils.time import utcnow_naive


SUPPORTED_INTENTS: tuple[str, ...] = (
    "intake",
    "listing",
    "comp",
    "accountant",
    "accounting",
    "tax",
    "ai-accountant",
    "goldie",
    "kurt",
    "murdock",
    "customer",
    "customers",
    "repeat-buyer",
    "repeat-buyers",
    "repeat_buyers",
    "business",
    "business-room",
    "status",
    "operations",
)
INTENT_ALIASES: dict[str, str] = {
    "kurt": "intake",
    "inventory-intake": "intake",
    "inventory_intake": "intake",
    "murdock": "listing",
    "listings": "listing",
    "draft-listing": "listing",
    "draft_listing": "listing",
    "accounting": "accountant",
    "tax": "accountant",
    "ai-accountant": "accountant",
    "ai_accountant": "accountant",
    "aiaccountant": "accountant",
    "goldie": "accountant",
    "customers": "customer",
    "repeat-buyer": "customer",
    "repeat_buyers": "customer",
    "repeat-buyers": "customer",
    "business": "status",
    "business-room": "status",
    "business_room": "status",
}

# Canonical command contract for Slack-origin operator requests.
# `write_intent=True` means downstream execution would mutate state and
# requires elevated operator capability.
COMMAND_ALLOWLIST: dict[str, dict[str, Any]] = {
    "intake": {"intent": "intake", "write_intent": True},
    "listing": {"intent": "listing", "write_intent": True},
    "comp": {"intent": "comp", "write_intent": False},
    "accountant": {"intent": "accountant", "write_intent": False},
    "customer": {"intent": "customer", "write_intent": False},
    "status": {"intent": "status", "write_intent": False},
    "operations": {"intent": "operations", "write_intent": True},
}

ROLE_INTENT_ALLOWLIST: dict[str, set[str]] = {
    "admin": {"intake", "listing", "comp", "accountant", "customer", "status", "operations"},
    "ops": {"intake", "listing", "comp", "accountant", "customer", "status", "operations"},
    "viewer": {"comp", "status"},
}

QUEUE_INTEGRATION = "slack_ops"
QUEUE_ACTION = "command_ingest"


def build_slack_ops_help_text() -> str:
    return "\n".join(
        [
            "GoldenStackers Slack Ops commands:",
            "- `kurt ...` or `intake ...` starts an approval-gated inventory intake draft.",
            "- `murdock ...`, `listing ...`, or `draft-listing ...` starts an approval-gated listing draft.",
            "- `comp ...` runs read-only pricing/comparable research.",
            "- `goldie ...`, `accountant ...`, `accounting ...`, or `tax ...` asks Goldie read-only accounting/tax questions.",
            "- `customer ...`, `customers ...`, or `repeat-buyer ...` asks read-only customer/repeat-buyer questions.",
            "- `status ...` or `business-room ...` asks for read-only business status.",
            "- Answer handoffs with `kurt answer handoff 88 quantity: 20` or `murdock answer draft #321 condition_id: 3000`.",
            "- Answer-only commands are evidence capture; they do not create products, listings, or publish by themselves.",
        ]
    )


@dataclass(frozen=True)
class SlackCommandEnvelope:
    environment: str
    source: str
    team_id: str
    channel_id: str
    channel_name: str
    thread_ts: str
    message_ts: str
    slack_user_id: str
    slack_username: str
    app_username: str
    app_role: str
    command_text: str
    command_name: str
    intent: str
    args: list[str]
    files: list[dict[str, str]]
    idempotency_key: str
    raw_payload: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "source": self.source,
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "thread_ts": self.thread_ts,
            "message_ts": self.message_ts,
            "slack_user_id": self.slack_user_id,
            "slack_username": self.slack_username,
            "app_username": self.app_username,
            "app_role": self.app_role,
            "command_text": self.command_text,
            "command_name": self.command_name,
            "intent": self.intent,
            "args": list(self.args),
            "files": list(self.files),
            "idempotency_key": self.idempotency_key,
            "raw_payload": dict(self.raw_payload),
        }


def _normalized_role(value: str) -> str:
    role = str(value or "").strip().lower()
    return role if role in ROLE_INTENT_ALLOWLIST else "viewer"


def _canonical_command_text(raw_text: str) -> str:
    return " ".join(str(raw_text or "").strip().split())


def _intent_and_args(raw_text: str) -> tuple[str, list[str]]:
    normalized = _canonical_command_text(raw_text)
    if not normalized:
        return "", []
    parts = normalized.split(" ")
    raw_intent = str(parts[0] or "").strip().lower()
    intent = INTENT_ALIASES.get(raw_intent, raw_intent)
    args = [str(v).strip() for v in parts[1:] if str(v).strip()]
    return intent, args


def _normalize_files(raw_files: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(raw_files, list):
        return rows
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "mimetype": str(item.get("mimetype") or "").strip(),
                "url_private": str(item.get("url_private") or "").strip(),
                "url_private_download": str(item.get("url_private_download") or "").strip(),
            }
        )
    return rows


def build_slack_command_envelope(payload: dict[str, Any], *, default_env: str = "local") -> SlackCommandEnvelope:
    raw_payload = dict(payload or {})
    command_text = _canonical_command_text(str(raw_payload.get("text") or ""))
    intent, args = _intent_and_args(command_text)
    command_name = str(raw_payload.get("command") or "").strip().lower()
    team_id = str(raw_payload.get("team_id") or "").strip()
    channel_id = str(raw_payload.get("channel_id") or "").strip()
    channel_name = str(raw_payload.get("channel_name") or "").strip()
    thread_ts = str(raw_payload.get("thread_ts") or raw_payload.get("message_ts") or "").strip()
    message_ts = str(raw_payload.get("message_ts") or raw_payload.get("ts") or "").strip()
    slack_user_id = str(raw_payload.get("user_id") or "").strip()
    slack_username = str(raw_payload.get("user_name") or "").strip()
    app_username = str(raw_payload.get("app_username") or slack_username or "").strip().lower()
    app_role = _normalized_role(str(raw_payload.get("app_role") or "viewer"))
    files = _normalize_files(raw_payload.get("files"))

    idem_base = {
        "team_id": team_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "message_ts": message_ts,
        "slack_user_id": slack_user_id,
        "command_name": command_name,
        "intent": intent,
        "args": args,
        "files": files,
    }
    idempotency_key = hashlib.sha256(
        json.dumps(idem_base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return SlackCommandEnvelope(
        environment=str(raw_payload.get("environment") or default_env).strip().lower() or "local",
        source="slack_ops_bot",
        team_id=team_id,
        channel_id=channel_id,
        channel_name=channel_name,
        thread_ts=thread_ts,
        message_ts=message_ts,
        slack_user_id=slack_user_id,
        slack_username=slack_username,
        app_username=app_username,
        app_role=app_role,
        command_text=command_text,
        command_name=command_name,
        intent=intent,
        args=args,
        files=files,
        idempotency_key=idempotency_key,
        raw_payload=raw_payload,
    )


def is_intent_allowed_for_role(*, intent: str, role: str) -> bool:
    normalized_intent = str(intent or "").strip().lower()
    normalized_role = _normalized_role(role)
    return normalized_intent in ROLE_INTENT_ALLOWLIST.get(normalized_role, set())


def route_slack_command_request(
    repo: Any,
    *,
    envelope: SlackCommandEnvelope,
    actor: str = "slack_bot",
) -> dict[str, Any]:
    intent_cfg = COMMAND_ALLOWLIST.get(envelope.intent)
    if intent_cfg is None:
        outcome = {
            "status": "rejected",
            "reason": "unsupported_intent",
            "intent": envelope.intent,
            "idempotency_key": envelope.idempotency_key,
            "supported_intents": list(SUPPORTED_INTENTS),
            "help_text": build_slack_ops_help_text(),
        }
        repo.record_audit_event(
            entity_type="slack_ops_command",
            entity_id=_audit_entity_id_from_idempotency(envelope.idempotency_key),
            action="rejected",
            actor=actor,
            changes={"after": {"outcome": outcome, "envelope": envelope.to_payload()}},
        )
        return outcome

    if not is_intent_allowed_for_role(intent=envelope.intent, role=envelope.app_role):
        outcome = {
            "status": "denied",
            "reason": "role_not_allowed",
            "intent": envelope.intent,
            "role": envelope.app_role,
            "idempotency_key": envelope.idempotency_key,
            "help_text": build_slack_ops_help_text(),
        }
        repo.record_audit_event(
            entity_type="slack_ops_command",
            entity_id=_audit_entity_id_from_idempotency(envelope.idempotency_key),
            action="denied",
            actor=actor,
            changes={"after": {"outcome": outcome, "envelope": envelope.to_payload()}},
        )
        return outcome

    outcome = {
        "status": "accepted",
        "intent": str(intent_cfg.get("intent") or envelope.intent),
        "write_intent": bool(intent_cfg.get("write_intent", False)),
        "idempotency_key": envelope.idempotency_key,
        "route": f"slack_ops:{str(intent_cfg.get('intent') or envelope.intent)}",
    }
    repo.record_audit_event(
        entity_type="slack_ops_command",
        entity_id=_audit_entity_id_from_idempotency(envelope.idempotency_key),
        action="accepted",
        actor=actor,
        changes={"after": {"outcome": outcome, "envelope": envelope.to_payload()}},
    )
    try:
        repo.log_integration_event(
            actor=actor,
            integration="slack_ops",
            action="command_routed",
            status="success",
            details={
                "idempotency_key": envelope.idempotency_key,
                "intent": outcome["intent"],
                "role": envelope.app_role,
                "channel_id": envelope.channel_id,
                "thread_ts": envelope.thread_ts,
                "write_intent": bool(outcome["write_intent"]),
            },
        )
    except Exception:
        pass
    return outcome


def _audit_entity_id_from_idempotency(idempotency_key: str) -> int:
    raw = str(idempotency_key or "").strip().lower()
    if not raw:
        return 0
    try:
        return int(raw[:12], 16) % 2_000_000_000
    except Exception:
        return 0


def _safe_json_loads(payload_json: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(payload_json or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _csv_normalized_set(raw: str) -> set[str]:
    parts = []
    for token in str(raw or "").replace("\n", ",").split(","):
        value = str(token or "").strip().lower()
        if value:
            parts.append(value)
    return set(parts)


def _is_slack_ops_allowed(
    repo: Any,
    *,
    envelope: SlackCommandEnvelope,
) -> tuple[bool, str]:
    if not get_runtime_bool(repo, "slack_ops_enabled", True):
        return False, "slack_ops_disabled"
    if envelope.intent:
        if not get_runtime_bool(repo, f"slack_ops_intent_{envelope.intent}_enabled", True):
            return False, f"intent_disabled:{envelope.intent}"
    allowed_channels = _csv_normalized_set(get_runtime_str(repo, "slack_ops_allowed_channels", ""))
    if allowed_channels:
        by_id = str(envelope.channel_id or "").strip().lower()
        by_name = str(envelope.channel_name or "").strip().lower()
        if by_id not in allowed_channels and by_name not in allowed_channels:
            return False, "channel_not_allowed"
    allowed_users = _csv_normalized_set(get_runtime_str(repo, "slack_ops_allowed_users", ""))
    if allowed_users:
        candidates = {
            str(envelope.app_username or "").strip().lower(),
            str(envelope.slack_username or "").strip().lower(),
            str(envelope.slack_user_id or "").strip().lower(),
        }
        candidates.discard("")
        if not candidates.intersection(allowed_users):
            return False, "user_not_allowed"
    window_minutes = max(1, min(1440, int(get_runtime_int(repo, "slack_ops_rate_limit_window_minutes", 15))))
    max_requests = max(1, min(5000, int(get_runtime_int(repo, "slack_ops_rate_limit_max_requests", 50))))
    existing = repo.list_integration_queue_jobs(
        environment=envelope.environment,
        integration=QUEUE_INTEGRATION,
        statuses={"queued", "running", "blocked", "failed", "success"},
        limit=max_requests * 4,
    )
    cutoff = utcnow_naive() - timedelta(minutes=window_minutes)
    recent = 0
    for row in existing:
        created_at = getattr(row, "created_at", None)
        if created_at is None or created_at >= cutoff:
            recent += 1
    if recent >= max_requests:
        return False, "rate_limited"
    return True, ""


def _find_duplicate_slack_command_job(
    repo: Any,
    *,
    environment: str,
    idempotency_key: str,
    limit: int = 500,
) -> Any | None:
    rows = repo.list_integration_queue_jobs(
        environment=environment,
        integration=QUEUE_INTEGRATION,
        statuses={"queued", "running", "success", "failed", "blocked"},
        limit=max(25, min(int(limit), 2000)),
    )
    target = str(idempotency_key or "").strip().lower()
    if not target:
        return None
    for row in rows:
        payload = _safe_json_loads(getattr(row, "payload_json", "{}"))
        row_key = (
            str(payload.get("idempotency_key") or "").strip().lower()
            or str(((payload.get("command") or {}).get("idempotency_key") or "")).strip().lower()
        )
        if row_key == target:
            return row
    return None


def ingest_slack_command_request(
    repo: Any,
    *,
    payload: dict[str, Any],
    actor: str = "slack_bot",
    default_env: str = "local",
    max_retries: int = 2,
) -> dict[str, Any]:
    envelope = build_slack_command_envelope(payload, default_env=default_env)
    routed = route_slack_command_request(repo, envelope=envelope, actor=actor)
    if str(routed.get("status") or "").strip().lower() != "accepted":
        return {
            "status": str(routed.get("status") or "rejected"),
            "queued": False,
            "route_result": dict(routed),
            "idempotency_key": envelope.idempotency_key,
        }

    allowed, guard_reason = _is_slack_ops_allowed(repo, envelope=envelope)
    if not allowed:
        outcome = {
            "status": "rejected",
            "reason": guard_reason,
            "queued": False,
            "route_result": dict(routed),
            "idempotency_key": envelope.idempotency_key,
        }
        repo.record_audit_event(
            entity_type="slack_ops_command",
            entity_id=_audit_entity_id_from_idempotency(envelope.idempotency_key),
            action="rejected",
            actor=actor,
            changes={"after": outcome},
        )
        try:
            repo.log_integration_event(
                actor=actor,
                integration=QUEUE_INTEGRATION,
                action="command_ingest_rejected",
                status="skipped",
                details={
                    "idempotency_key": envelope.idempotency_key,
                    "reason": guard_reason,
                    "intent": envelope.intent,
                    "channel_id": envelope.channel_id,
                },
            )
        except Exception:
            pass
        return outcome

    duplicate = _find_duplicate_slack_command_job(
        repo,
        environment=envelope.environment,
        idempotency_key=envelope.idempotency_key,
    )
    if duplicate is not None:
        queue_job_id = int(getattr(duplicate, "id", 0) or 0)
        result = {
            "status": "duplicate",
            "queued": False,
            "idempotency_key": envelope.idempotency_key,
            "queue_job_id": queue_job_id,
            "queue_status": str(getattr(duplicate, "status", "") or "").strip().lower(),
            "route_result": dict(routed),
        }
        repo.record_audit_event(
            entity_type="slack_ops_command",
            entity_id=_audit_entity_id_from_idempotency(envelope.idempotency_key),
            action="deduped",
            actor=actor,
            changes={"after": result},
        )
        try:
            repo.log_integration_event(
                actor=actor,
                integration=QUEUE_INTEGRATION,
                action="command_ingest_duplicate",
                status="skipped",
                details={
                    "idempotency_key": envelope.idempotency_key,
                    "queue_job_id": queue_job_id,
                    "intent": envelope.intent,
                    "channel_id": envelope.channel_id,
                    "thread_ts": envelope.thread_ts,
                },
            )
        except Exception:
            pass
        return result

    approval_required = bool(routed.get("write_intent", False)) and bool(
        get_runtime_bool(repo, "slack_ops_write_actions_require_approval", True)
    )
    accountant_answer = None
    if envelope.intent == "accountant":
        accountant_answer = parse_ai_accountant_answer_prompt(envelope.command_text)
        if accountant_answer:
            record_ai_accountant_answer(
                repo,
                actor=envelope.app_username or envelope.slack_username or actor,
                prompt=envelope.command_text,
                source="slack",
            )
    ai_agent_answer = parse_ai_agent_answer_prompt(envelope.command_text)
    queue_payload = {
        "idempotency_key": envelope.idempotency_key,
        "received_at": utcnow_naive().isoformat(timespec="seconds"),
        "command": envelope.to_payload(),
        "route": dict(routed),
        "approval": {
            "required": approval_required,
            "status": "pending" if approval_required else "not_required",
            "requested_at": utcnow_naive().isoformat(timespec="seconds"),
            "requested_by": envelope.app_username or envelope.slack_username or actor,
        },
        "request_context": {
            "source": "slack",
            "team_id": envelope.team_id,
            "channel_id": envelope.channel_id,
            "channel_name": envelope.channel_name,
            "thread_ts": envelope.thread_ts,
            "message_ts": envelope.message_ts,
            "slack_user_id": envelope.slack_user_id,
            "slack_username": envelope.slack_username,
            "app_username": envelope.app_username,
            "app_role": envelope.app_role,
            "command_text": envelope.command_text,
        },
    }
    if accountant_answer:
        queue_payload["command"]["ai_accountant_answer"] = accountant_answer
    if ai_agent_answer:
        queue_payload["command"]["ai_agent_answer"] = ai_agent_answer
    queued = repo.create_integration_queue_job(
        environment=envelope.environment,
        integration=QUEUE_INTEGRATION,
        action=QUEUE_ACTION,
        payload_json=json.dumps(queue_payload, sort_keys=True),
        requested_by=envelope.app_username or envelope.slack_username or actor,
        max_retries=max(0, int(max_retries)),
        actor=actor,
    )
    queue_job_id = int(getattr(queued, "id", 0) or 0)
    if approval_required:
        try:
            repo.update_integration_queue_job(
                queue_job_id,
                {
                    "status": "blocked",
                    "last_error": "Awaiting approval for write-intent Slack command.",
                },
                actor=actor,
            )
        except Exception:
            pass
    result = {
        "status": "pending_approval" if approval_required else "queued",
        "queued": not approval_required,
        "queue_job_id": queue_job_id,
        "idempotency_key": envelope.idempotency_key,
        "intent": envelope.intent,
        "approval_required": approval_required,
        "route_result": dict(routed),
    }
    repo.record_audit_event(
        entity_type="slack_ops_command",
        entity_id=_audit_entity_id_from_idempotency(envelope.idempotency_key),
        action="approval_requested" if approval_required else "queued",
        actor=actor,
        changes={"after": result},
    )
    try:
        repo.log_integration_event(
            actor=actor,
            integration=QUEUE_INTEGRATION,
            action="command_ingest_pending_approval" if approval_required else "command_ingest_queued",
            status="blocked" if approval_required else "queued",
            details={
                "queue_job_id": queue_job_id,
                "idempotency_key": envelope.idempotency_key,
                "intent": envelope.intent,
                "role": envelope.app_role,
                "channel_id": envelope.channel_id,
                "thread_ts": envelope.thread_ts,
                "approval_required": approval_required,
            },
        )
    except Exception:
        pass
    return result


def approve_slack_ops_queue_job(
    repo: Any,
    *,
    queue_job_id: int,
    approver_username: str,
    approver_role: str,
    actor: str = "slack_bot",
) -> dict[str, Any]:
    normalized_role = _normalized_role(approver_role)
    if normalized_role not in {"admin", "ops"}:
        return {
            "status": "denied",
            "reason": "role_not_allowed",
            "queue_job_id": int(queue_job_id),
        }
    job = repo.db.get(IntegrationQueueJob, int(queue_job_id))
    if job is None:
        return {"status": "not_found", "queue_job_id": int(queue_job_id)}
    if str(getattr(job, "integration", "") or "").strip().lower() != QUEUE_INTEGRATION:
        return {"status": "invalid_job", "reason": "wrong_integration", "queue_job_id": int(queue_job_id)}
    if str(getattr(job, "action", "") or "").strip().lower() != QUEUE_ACTION:
        return {"status": "invalid_job", "reason": "wrong_action", "queue_job_id": int(queue_job_id)}
    payload = _safe_json_loads(getattr(job, "payload_json", "{}"))
    approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
    if not bool(approval.get("required", False)):
        return {"status": "not_required", "queue_job_id": int(queue_job_id)}
    if str(approval.get("status") or "").strip().lower() == "approved":
        return {"status": "already_approved", "queue_job_id": int(queue_job_id)}

    approval["status"] = "approved"
    approval["approved_at"] = utcnow_naive().isoformat(timespec="seconds")
    approval["approved_by"] = str(approver_username or "").strip() or "unknown"
    approval["approver_role"] = normalized_role
    payload["approval"] = approval
    repo.update_integration_queue_job(
        int(queue_job_id),
        {
            "payload_json": json.dumps(payload, sort_keys=True),
            "status": "queued",
            "last_error": "",
            "next_attempt_at": utcnow_naive(),
        },
        actor=actor,
    )
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    audit_id = _audit_entity_id_from_idempotency(idempotency_key)
    outcome = {
        "status": "approved",
        "queue_job_id": int(queue_job_id),
        "idempotency_key": idempotency_key,
        "approver_username": approval["approved_by"],
        "approver_role": normalized_role,
    }
    repo.record_audit_event(
        entity_type="slack_ops_command",
        entity_id=audit_id,
        action="approved",
        actor=actor,
        changes={"after": outcome},
    )
    try:
        repo.log_integration_event(
            actor=actor,
            integration=QUEUE_INTEGRATION,
            action="command_ingest_approved",
            status="queued",
            details=outcome,
        )
    except Exception:
        pass
    return outcome
