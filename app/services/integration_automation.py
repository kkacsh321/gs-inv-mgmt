from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.db.models import IntegrationQueueJob
from app.services.runtime_settings import get_runtime_bool
from app.utils.time import utcnow_naive


def _get_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in str(path or "").split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except Exception:
                return None
        else:
            return None
    return current


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _condition_match(context: dict[str, Any], condition: dict[str, Any]) -> bool:
    field = str(condition.get("field") or "").strip()
    op = str(condition.get("op") or "eq").strip().lower()
    expected = condition.get("value")
    actual = _get_path(context, field) if field else None
    if op == "eq":
        return str(actual) == str(expected)
    if op == "neq":
        return str(actual) != str(expected)
    if op == "contains":
        return str(expected) in str(actual or "")
    if op in {"gt", "gte", "lt", "lte"}:
        av = _to_float(actual)
        ev = _to_float(expected)
        if av is None or ev is None:
            return False
        if op == "gt":
            return av > ev
        if op == "gte":
            return av >= ev
        if op == "lt":
            return av < ev
        return av <= ev
    if op == "in":
        if isinstance(expected, list):
            return str(actual) in {str(x) for x in expected}
        return False
    return False


def _rule_match(context: dict[str, Any], conditions_json: str) -> bool:
    raw = (conditions_json or "{}").strip() or "{}"
    try:
        spec = json.loads(raw)
    except Exception:
        return False
    if not isinstance(spec, dict):
        return False
    if not spec:
        return True
    all_conditions = spec.get("all")
    any_conditions = spec.get("any")
    if isinstance(all_conditions, list) and all_conditions:
        return all(_condition_match(context, cond) for cond in all_conditions if isinstance(cond, dict))
    if isinstance(any_conditions, list) and any_conditions:
        return any(_condition_match(context, cond) for cond in any_conditions if isinstance(cond, dict))
    return True


def _allowed_queue_update(effect_set: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "status" in effect_set:
        status = str(effect_set.get("status") or "").strip().lower()
        if status in {"queued", "running", "failed", "success"}:
            updates["status"] = status
    if "max_retries" in effect_set:
        try:
            updates["max_retries"] = max(0, int(effect_set.get("max_retries")))
        except Exception:
            pass
    if "retry_count" in effect_set:
        try:
            updates["retry_count"] = max(0, int(effect_set.get("retry_count")))
        except Exception:
            pass
    if "next_attempt_in_seconds" in effect_set:
        try:
            delay = int(effect_set.get("next_attempt_in_seconds"))
            updates["next_attempt_at"] = utcnow_naive() + timedelta(seconds=max(0, delay))
        except Exception:
            pass
    if "last_error" in effect_set:
        updates["last_error"] = str(effect_set.get("last_error") or "")[:2000]
    return updates


def evaluate_and_apply_rules_for_job(
    repo: Any,
    *,
    job: IntegrationQueueJob,
    actor: str,
    trigger_status: str,
) -> dict[str, Any]:
    rules = repo.list_integration_automation_rules(
        environment=str(getattr(job, "environment", "") or "").strip().lower(),
        integration=str(getattr(job, "integration", "") or "").strip().lower(),
        action=str(getattr(job, "action", "") or "").strip().lower(),
        active_only=True,
        limit=200,
    )
    try:
        payload = json.loads(str(getattr(job, "payload_json", "") or "{}"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    context = {
        "job": {
            "id": int(getattr(job, "id", 0) or 0),
            "environment": str(getattr(job, "environment", "") or ""),
            "integration": str(getattr(job, "integration", "") or ""),
            "action": str(getattr(job, "action", "") or ""),
            "status": str(getattr(job, "status", "") or ""),
            "retry_count": int(getattr(job, "retry_count", 0) or 0),
            "max_retries": int(getattr(job, "max_retries", 0) or 0),
            "requested_by": str(getattr(job, "requested_by", "") or ""),
        },
        "payload": payload,
    }
    execute_approval_required = get_runtime_bool(
        repo,
        "integration_automation_execute_approval_required_enabled",
        False,
    )
    dry_run = get_runtime_bool(repo, "integration_automation_dry_run_enabled", True)
    matched_rule_ids: list[int] = []
    approval_gated_rule_ids: list[int] = []
    applied_rule_ids: list[int] = []
    aggregate_updates: dict[str, Any] = {}
    blocked = False
    blocked_reason = ""
    for rule in rules:
        if str(getattr(rule, "trigger_status", "") or "").strip().lower() != str(trigger_status or "").strip().lower():
            continue
        if not _rule_match(context, str(getattr(rule, "conditions_json", "") or "{}")):
            continue
        matched_rule_ids.append(int(rule.id))
        if bool(getattr(rule, "requires_approval", False)):
            if not execute_approval_required:
                approval_gated_rule_ids.append(int(rule.id))
                continue
            has_approval = repo.has_active_integration_automation_approval(
                environment=str(getattr(job, "environment", "") or "").strip().lower(),
                rule_id=int(rule.id),
                queue_job_id=int(getattr(job, "id", 0) or 0),
                as_of=utcnow_naive(),
            )
            if not has_approval:
                approval_gated_rule_ids.append(int(rule.id))
                continue
        try:
            effect_spec = json.loads(str(getattr(rule, "effect_json", "") or "{}"))
        except Exception:
            effect_spec = {}
        if not isinstance(effect_spec, dict):
            continue
        effect_type = str(effect_spec.get("type") or "").strip().lower()
        if effect_type == "block_execute":
            blocked = True
            blocked_reason = str(effect_spec.get("reason") or f"Blocked by rule {rule.id}").strip()
            applied_rule_ids.append(int(rule.id))
            continue
        if effect_type == "queue_update":
            effect_set = effect_spec.get("set") if isinstance(effect_spec.get("set"), dict) else {}
            updates = _allowed_queue_update(effect_set)
            if updates:
                aggregate_updates.update(updates)
                applied_rule_ids.append(int(rule.id))
            continue

    if aggregate_updates and not dry_run:
        repo.update_integration_queue_job(
            int(job.id),
            aggregate_updates,
            actor=actor,
        )
    try:
        repo.log_integration_event(
            actor=actor,
            integration="integration_automation",
            action="evaluate_rules",
            status="success",
            details={
                "job_id": int(job.id),
                "integration": str(job.integration or ""),
                "action_name": str(job.action or ""),
                "trigger_status": str(trigger_status or ""),
                "matched_rule_ids": matched_rule_ids,
                "approval_gated_rule_ids": approval_gated_rule_ids,
                "applied_rule_ids": applied_rule_ids,
                "dry_run": bool(dry_run),
                "updates": aggregate_updates,
                "blocked": bool(blocked),
                "blocked_reason": blocked_reason,
            },
        )
    except Exception:
        pass
    return {
        "matched_rule_ids": matched_rule_ids,
        "approval_gated_rule_ids": approval_gated_rule_ids,
        "applied_rule_ids": applied_rule_ids,
        "dry_run": bool(dry_run),
        "updates": aggregate_updates,
        "blocked": bool(blocked),
        "blocked_reason": blocked_reason,
    }


def preview_rule_impact(
    repo: Any,
    *,
    environment: str,
    integration: str,
    action: str,
    trigger_status: str,
    conditions_json: str,
    scan_limit: int = 1000,
    sample_limit: int = 25,
) -> dict[str, Any]:
    """Estimate how many current queue jobs match a rule before enabling/saving it."""
    normalized_status = str(trigger_status or "").strip().lower()
    statuses: set[str] | None = {normalized_status} if normalized_status else None
    jobs = repo.list_integration_queue_jobs(
        environment=(environment or "").strip().lower(),
        integration=(integration or "").strip().lower() or None,
        statuses=statuses,
        limit=max(1, int(scan_limit)),
    )
    normalized_action = str(action or "").strip().lower()
    if normalized_action:
        jobs = [j for j in jobs if str(getattr(j, "action", "") or "").strip().lower() == normalized_action]

    matched_jobs: list[IntegrationQueueJob] = []
    payload_parse_errors = 0
    for job in jobs:
        try:
            payload = json.loads(str(getattr(job, "payload_json", "") or "{}"))
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload_parse_errors += 1
            payload = {}
        context = {
            "job": {
                "id": int(getattr(job, "id", 0) or 0),
                "environment": str(getattr(job, "environment", "") or ""),
                "integration": str(getattr(job, "integration", "") or ""),
                "action": str(getattr(job, "action", "") or ""),
                "status": str(getattr(job, "status", "") or ""),
                "retry_count": int(getattr(job, "retry_count", 0) or 0),
                "max_retries": int(getattr(job, "max_retries", 0) or 0),
                "requested_by": str(getattr(job, "requested_by", "") or ""),
            },
            "payload": payload,
        }
        if _rule_match(context, str(conditions_json or "{}")):
            matched_jobs.append(job)

    samples: list[dict[str, Any]] = []
    for row in matched_jobs[: max(1, int(sample_limit))]:
        samples.append(
            {
                "job_id": int(getattr(row, "id", 0) or 0),
                "status": str(getattr(row, "status", "") or ""),
                "integration": str(getattr(row, "integration", "") or ""),
                "action": str(getattr(row, "action", "") or ""),
                "requested_by": str(getattr(row, "requested_by", "") or ""),
                "retry_count": int(getattr(row, "retry_count", 0) or 0),
                "max_retries": int(getattr(row, "max_retries", 0) or 0),
                "next_attempt_at": (
                    getattr(row, "next_attempt_at", None).isoformat()
                    if getattr(row, "next_attempt_at", None)
                    else ""
                ),
                "updated_at": (
                    getattr(row, "updated_at", None).isoformat() if getattr(row, "updated_at", None) else ""
                ),
            }
        )

    return {
        "candidate_jobs": len(jobs),
        "matched_jobs": len(matched_jobs),
        "match_rate": (float(len(matched_jobs)) / float(len(jobs))) if jobs else 0.0,
        "payload_parse_errors": int(payload_parse_errors),
        "samples": samples,
    }


def simulate_rule_evaluation_for_job(
    repo: Any,
    *,
    environment: str,
    job_id: int,
    trigger_status: str,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Replay rule matching for one queue job without applying any effects."""
    job = repo.db.get(IntegrationQueueJob, int(job_id))
    if job is None:
        raise ValueError(f"Integration queue job {job_id} not found.")
    try:
        payload = json.loads(str(getattr(job, "payload_json", "") or "{}"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    context = {
        "job": {
            "id": int(getattr(job, "id", 0) or 0),
            "environment": str(getattr(job, "environment", "") or ""),
            "integration": str(getattr(job, "integration", "") or ""),
            "action": str(getattr(job, "action", "") or ""),
            "status": str(getattr(job, "status", "") or ""),
            "retry_count": int(getattr(job, "retry_count", 0) or 0),
            "max_retries": int(getattr(job, "max_retries", 0) or 0),
            "requested_by": str(getattr(job, "requested_by", "") or ""),
        },
        "payload": payload,
    }
    normalized_trigger = str(trigger_status or "").strip().lower()
    rules = repo.list_integration_automation_rules(
        environment=(environment or "").strip().lower(),
        integration=str(getattr(job, "integration", "") or "").strip().lower(),
        action=str(getattr(job, "action", "") or "").strip().lower(),
        active_only=not bool(include_inactive),
        limit=500,
    )
    execute_approval_required = get_runtime_bool(
        repo,
        "integration_automation_execute_approval_required_enabled",
        False,
    )
    as_of = utcnow_naive()
    rows: list[dict[str, Any]] = []
    for rule in rules:
        rule_trigger = str(getattr(rule, "trigger_status", "") or "").strip().lower()
        trigger_matches = rule_trigger == normalized_trigger if normalized_trigger else True
        conditions_match = _rule_match(context, str(getattr(rule, "conditions_json", "") or "{}"))
        matched = bool(trigger_matches and conditions_match)
        try:
            effect_spec = json.loads(str(getattr(rule, "effect_json", "") or "{}"))
        except Exception:
            effect_spec = {}
        if not isinstance(effect_spec, dict):
            effect_spec = {}
        effect_type = str(effect_spec.get("type") or "").strip().lower()
        requires_approval = bool(getattr(rule, "requires_approval", False))
        has_approval = False
        approval_gated = False
        if matched and requires_approval:
            if execute_approval_required:
                has_approval = repo.has_active_integration_automation_approval(
                    environment=(environment or "").strip().lower(),
                    rule_id=int(rule.id),
                    queue_job_id=int(job.id),
                    as_of=as_of,
                )
                approval_gated = not has_approval
            else:
                approval_gated = True
        would_apply = bool(
            matched
            and (not requires_approval or (execute_approval_required and has_approval))
        )
        rows.append(
            {
                "rule_id": int(rule.id),
                "rule_name": str(getattr(rule, "name", "") or ""),
                "is_active": bool(getattr(rule, "is_active", False)),
                "trigger_status": rule_trigger,
                "trigger_matches": bool(trigger_matches),
                "conditions_match": bool(conditions_match),
                "matched": matched,
                "requires_approval": requires_approval,
                "approval_gated": approval_gated,
                "has_active_approval": has_approval,
                "effect_type": effect_type,
                "would_apply": would_apply,
                "effect_json": str(getattr(rule, "effect_json", "") or "{}"),
            }
        )
    matched_count = sum(1 for row in rows if bool(row.get("matched")))
    apply_count = sum(1 for row in rows if bool(row.get("would_apply")))
    gated_count = sum(1 for row in rows if bool(row.get("approval_gated")))
    return {
        "job_id": int(job.id),
        "integration": str(getattr(job, "integration", "") or ""),
        "action": str(getattr(job, "action", "") or ""),
        "trigger_status": normalized_trigger,
        "rules_considered": len(rows),
        "matched_rules": matched_count,
        "would_apply_rules": apply_count,
        "approval_gated_rules": gated_count,
        "rows": rows,
    }
