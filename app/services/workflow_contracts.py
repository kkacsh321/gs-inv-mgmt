from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

LISTING_DRAFT_CONTRACT_TYPE = "listing_draft"
LISTING_DRAFT_CONTRACT_VERSION = 1
AI_AGENT_DRAFT_CONTRACT_TYPE = "ai_agent_draft"
AI_AGENT_DRAFT_CONTRACT_VERSION = 1

AI_AGENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "intake": ("title", "category", "quantity"),
    "listing": ("product_id", "title", "description_html"),
}


def build_listing_draft_payload(
    *,
    state: Mapping[str, object] | None = None,
    context: Mapping[str, object] | None = None,
    signature: str = "",
) -> dict[str, object]:
    state_obj = dict(state or {})
    context_obj = dict(context or {})
    return {
        "contract": {
            "type": LISTING_DRAFT_CONTRACT_TYPE,
            "version": LISTING_DRAFT_CONTRACT_VERSION,
        },
        "signature": str(signature or "").strip(),
        "context": context_obj,
        "state": state_obj,
    }


def extract_listing_draft_payload(
    payload: Mapping[str, object] | None,
    *,
    state_keys: Iterable[str] | None = None,
    context_keys: Iterable[str] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        return {
            "is_contract": False,
            "contract_version": 0,
            "signature": "",
            "state": {},
            "context": {},
        }

    raw_contract = payload.get("contract")
    contract_obj = raw_contract if isinstance(raw_contract, Mapping) else {}
    is_contract = str(contract_obj.get("type") or "").strip() == LISTING_DRAFT_CONTRACT_TYPE
    try:
        contract_version = int(contract_obj.get("version") or 0)
    except Exception:
        contract_version = 0
    signature = str(payload.get("signature") or "").strip()

    allowed_state_keys = {str(key) for key in (state_keys or []) if str(key or "").strip()}
    allowed_context_keys = {str(key) for key in (context_keys or []) if str(key or "").strip()}

    nested_state = payload.get("state")
    if isinstance(nested_state, Mapping):
        state_source = nested_state
    else:
        state_source = payload
    if allowed_state_keys:
        state_obj = {str(key): state_source.get(str(key)) for key in allowed_state_keys if str(key) in state_source}
    else:
        state_obj = dict(state_source)

    nested_context = payload.get("context")
    if isinstance(nested_context, Mapping):
        context_source = nested_context
    else:
        context_source = payload
    if allowed_context_keys:
        context_obj = {
            str(key): context_source.get(str(key))
            for key in allowed_context_keys
            if str(key) in context_source
        }
    else:
        context_obj = dict(context_source)

    if not signature:
        signature = str(context_obj.get("listing_signature") or payload.get("listing_signature") or "").strip()

    return {
        "is_contract": is_contract,
        "contract_version": contract_version,
        "signature": signature,
        "state": state_obj,
        "context": context_obj,
    }


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_confidence(raw_value: object) -> float:
    try:
        value = float(raw_value)
    except Exception:
        value = 0.0
    if value > 1.0:
        value = value / 100.0
    return round(max(0.0, min(1.0, value)), 4)


def _normalize_field(field: Mapping[str, object]) -> dict[str, object]:
    key = str(field.get("key") or field.get("field") or "").strip()
    value = field.get("value")
    source = str(field.get("source") or "").strip()
    warnings = field.get("warnings")
    evidence = field.get("evidence")
    missing_reason = str(field.get("missing_reason") or "").strip()
    return {
        "key": key,
        "label": str(field.get("label") or key.replace("_", " ").title()).strip(),
        "value": value,
        "confidence": _normalize_confidence(field.get("confidence", 0.0)),
        "source": source,
        "evidence": list(evidence) if isinstance(evidence, list) else ([] if evidence in (None, "") else [evidence]),
        "warnings": list(warnings) if isinstance(warnings, list) else ([] if warnings in (None, "") else [warnings]),
        "missing_reason": missing_reason,
        "apply_path": str(field.get("apply_path") or key).strip(),
    }


def _field_by_key(fields: Iterable[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    out: dict[str, Mapping[str, object]] = {}
    for field in fields:
        key = str(field.get("key") or "").strip()
        if key:
            out[key] = field
    return out


def build_ai_agent_missing_questions(
    *,
    draft_type: str,
    fields: Iterable[Mapping[str, object]],
    required_fields: Iterable[str] | None = None,
) -> list[dict[str, object]]:
    normalized_type = str(draft_type or "").strip().lower()
    required = tuple(required_fields or AI_AGENT_REQUIRED_FIELDS.get(normalized_type, ()))
    by_key = _field_by_key(fields)
    questions: list[dict[str, object]] = []
    for key in required:
        field = by_key.get(str(key))
        value = field.get("value") if isinstance(field, Mapping) else None
        missing = value is None or str(value).strip() == ""
        confidence = _normalize_confidence(field.get("confidence", 0.0)) if isinstance(field, Mapping) else 0.0
        if missing or confidence < 0.5:
            label = str(field.get("label") or str(key).replace("_", " ").title()).strip() if isinstance(field, Mapping) else str(key).replace("_", " ").title()
            questions.append(
                {
                    "field": str(key),
                    "question": f"Confirm {label}.",
                    "reason": (
                        str(field.get("missing_reason") or "missing_or_low_confidence").strip()
                        if isinstance(field, Mapping)
                        else "missing_required_field"
                    ),
                    "confidence": confidence,
                    "blocking": True,
                }
            )
    return questions


def build_ai_agent_draft_payload(
    *,
    agent_key: str,
    draft_type: str,
    operator_request: str = "",
    fields: Iterable[Mapping[str, object]] | None = None,
    context: Mapping[str, object] | None = None,
    warnings: Iterable[object] | None = None,
    missing_questions: Iterable[Mapping[str, object]] | None = None,
    proposed_actions: Iterable[Mapping[str, object]] | None = None,
    source_refs: Iterable[Mapping[str, object]] | None = None,
    approval_required: bool = True,
) -> dict[str, object]:
    normalized_fields = [
        field for field in (_normalize_field(item) for item in (fields or [])) if str(field.get("key") or "").strip()
    ]
    normalized_type = str(draft_type or "").strip().lower() or "general"
    generated_questions = build_ai_agent_missing_questions(draft_type=normalized_type, fields=normalized_fields)
    question_rows = list(missing_questions or []) or generated_questions
    warning_rows = [str(item).strip() for item in (warnings or []) if str(item).strip()]
    action_rows = [dict(item) for item in (proposed_actions or []) if isinstance(item, Mapping)]
    context_obj = dict(context or {})
    payload_body = {
        "agent_key": str(agent_key or "").strip().lower(),
        "draft_type": normalized_type,
        "operator_request": str(operator_request or "").strip(),
        "fields": normalized_fields,
        "context": context_obj,
        "warnings": warning_rows,
        "missing_questions": [dict(item) for item in question_rows if isinstance(item, Mapping)],
        "proposed_actions": action_rows,
        "source_refs": [dict(item) for item in (source_refs or []) if isinstance(item, Mapping)],
        "approval": {
            "required": bool(approval_required),
            "status": "pending" if approval_required else "not_required",
        },
    }
    return {
        "contract": {
            "type": AI_AGENT_DRAFT_CONTRACT_TYPE,
            "version": AI_AGENT_DRAFT_CONTRACT_VERSION,
        },
        "signature": _stable_hash(payload_body),
        **payload_body,
    }


def extract_ai_agent_draft_payload(payload: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        return {
            "is_contract": False,
            "contract_version": 0,
            "agent_key": "",
            "draft_type": "",
            "fields": [],
            "missing_questions": [],
            "proposed_actions": [],
            "approval": {},
            "signature": "",
        }
    contract = payload.get("contract") if isinstance(payload.get("contract"), Mapping) else {}
    is_contract = str(contract.get("type") or "").strip() == AI_AGENT_DRAFT_CONTRACT_TYPE
    try:
        version = int(contract.get("version") or 0)
    except Exception:
        version = 0
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    missing_questions = payload.get("missing_questions") if isinstance(payload.get("missing_questions"), list) else []
    proposed_actions = payload.get("proposed_actions") if isinstance(payload.get("proposed_actions"), list) else []
    approval = payload.get("approval") if isinstance(payload.get("approval"), Mapping) else {}
    return {
        "is_contract": is_contract,
        "contract_version": version,
        "agent_key": str(payload.get("agent_key") or "").strip().lower(),
        "draft_type": str(payload.get("draft_type") or "").strip().lower(),
        "fields": [dict(item) for item in fields if isinstance(item, Mapping)],
        "field_values": {
            str(item.get("key")): item.get("value")
            for item in fields
            if isinstance(item, Mapping) and str(item.get("key") or "").strip()
        },
        "missing_questions": [dict(item) for item in missing_questions if isinstance(item, Mapping)],
        "proposed_actions": [dict(item) for item in proposed_actions if isinstance(item, Mapping)],
        "approval": dict(approval),
        "warnings": list(payload.get("warnings")) if isinstance(payload.get("warnings"), list) else [],
        "context": dict(payload.get("context")) if isinstance(payload.get("context"), Mapping) else {},
        "signature": str(payload.get("signature") or "").strip(),
    }


def parse_ai_agent_answer_prompt(text: str) -> dict[str, object]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    match = re.match(
        r"^(?P<agent>kurt|murdock|goldie|scout|atlas|goldy|agent)\s+answer\s+(?P<field>[a-zA-Z0-9_ .#-]{2,80})\s*:\s*(?P<answer>.+?)\s*$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        match = re.match(
            r"^answer\s+(?P<field>[a-zA-Z0-9_ .#-]{2,80})\s*:\s*(?P<answer>.+?)\s*$",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if not match:
        return {}
    raw_field = str(match.group("field") or "").strip()
    target_queue_job_id = 0
    target_draft_id = 0
    target_match = re.match(
        r"^(?P<target_kind>handoff|queue|job|draft)\s*#?\s*(?P<target_id>[0-9]{1,10})\s+(?P<field>.+?)$",
        raw_field,
        flags=re.IGNORECASE,
    )
    if target_match:
        raw_field = str(target_match.group("field") or "").strip()
        try:
            target_id = max(0, int(target_match.group("target_id") or 0))
        except Exception:
            target_id = 0
        target_kind = str(target_match.group("target_kind") or "").strip().lower()
        if target_kind == "draft":
            target_draft_id = target_id
        else:
            target_queue_job_id = target_id
    field = re.sub(r"[^a-zA-Z0-9_]+", "_", raw_field.lower()).strip("_")
    answer = str(match.group("answer") or "").strip()
    if not field or not answer:
        return {}
    agent = str(match.groupdict().get("agent") or "").strip().lower()
    return {
        "agent": agent,
        "field": field,
        "answer": answer,
        "target_queue_job_id": target_queue_job_id,
        "target_draft_id": target_draft_id,
    }


def apply_ai_agent_question_answers(
    payload: Mapping[str, object] | None,
    answers: Iterable[Mapping[str, object]],
    *,
    actor: str = "",
    source: str = "operator_answer",
) -> dict[str, object]:
    extracted = extract_ai_agent_draft_payload(payload)
    if not extracted["is_contract"]:
        return dict(payload or {})

    def _normalize_answer_value(field: str, value: object) -> object:
        normalized_field = str(field or "").strip().lower()
        if normalized_field in {"item_specifics", "specifics", "aspects", "ebay_aspects"} and isinstance(value, str):
            raw = value.strip()
            if raw.startswith(("{", "[")):
                try:
                    parsed = json.loads(raw)
                except Exception:
                    return value
                if isinstance(parsed, (dict, list)):
                    return parsed
            if "=" in raw or ":" in raw:
                parsed_pairs: dict[str, str] = {}
                for part in re.split(r"\s*[;,]\s*", raw):
                    if not part.strip():
                        continue
                    if "=" in part:
                        key, item_value = part.split("=", 1)
                    elif ":" in part:
                        key, item_value = part.split(":", 1)
                    else:
                        continue
                    key = key.strip().strip('"').strip("'")
                    item_value = item_value.strip().strip('"').strip("'")
                    if key and item_value:
                        parsed_pairs[key] = item_value
                if parsed_pairs:
                    return parsed_pairs
        return value

    def _answer_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
        field_value = re.sub(
            r"[^a-zA-Z0-9_]+",
            "_",
            str(row.get("field") or "").strip().lower(),
        ).strip("_")
        raw_value = row.get("answer", row.get("value"))
        return (
            field_value,
            json.dumps(raw_value, sort_keys=True, separators=(",", ":"), default=str),
            str(row.get("actor") or actor or "").strip(),
            str(row.get("source") or source or "operator_answer").strip(),
        )

    answer_rows: list[dict[str, object]] = []
    seen_input_answers: set[tuple[str, str, str, str]] = set()
    for answer in answers:
        if not isinstance(answer, Mapping):
            continue
        field = re.sub(r"[^a-zA-Z0-9_]+", "_", str(answer.get("field") or "").strip().lower()).strip("_")
        value = _normalize_answer_value(field, answer.get("answer", answer.get("value")))
        row = {
            "field": field,
            "value": value,
            "actor": str(actor or "").strip(),
            "source": str(answer.get("source") or source or "operator_answer").strip(),
        }
        row_key = _answer_key(row)
        if field and value not in (None, "") and row_key not in seen_input_answers:
            seen_input_answers.add(row_key)
            answer_rows.append(row)
    if not answer_rows:
        return dict(payload or {})

    out = dict(payload or {})
    existing_fields = [dict(row) for row in extracted["fields"]]
    fields_by_key = {str(row.get("key") or "").strip().lower(): row for row in existing_fields}
    for answer in answer_rows:
        field_key = str(answer["field"])
        row = fields_by_key.get(field_key)
        if row is None:
            row = {
                "key": field_key,
                "label": field_key.replace("_", " ").title(),
                "value": answer["value"],
                "confidence": 0.8,
                "source": answer["source"],
                "evidence": [],
                "warnings": [],
                "missing_reason": "",
                "apply_path": field_key,
            }
            existing_fields.append(row)
            fields_by_key[field_key] = row
        else:
            row["value"] = answer["value"]
            row["confidence"] = max(_normalize_confidence(row.get("confidence", 0.0)), 0.8)
            row["source"] = answer["source"]
            evidence = row.get("evidence") if isinstance(row.get("evidence"), list) else []
            evidence_row = {
                "kind": "operator_answer",
                "field": field_key,
                "value": answer["value"],
                "actor": answer["actor"],
                "source": answer["source"],
            }
            evidence_key = json.dumps(evidence_row, sort_keys=True, separators=(",", ":"), default=str)
            existing_evidence_keys = {
                json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                for item in evidence
                if isinstance(item, Mapping)
            }
            if evidence_key not in existing_evidence_keys:
                evidence.append(evidence_row)
            row["evidence"] = evidence
            row["missing_reason"] = ""

    previous_answers = out.get("operator_answers") if isinstance(out.get("operator_answers"), list) else []
    operator_answers = [dict(row) for row in previous_answers if isinstance(row, Mapping)]
    existing_answer_keys = {_answer_key(row) for row in operator_answers}
    for answer in answer_rows:
        answer_key = _answer_key(answer)
        if answer_key not in existing_answer_keys:
            operator_answers.append(answer)
            existing_answer_keys.add(answer_key)

    answered_fields = {str(row["field"]) for row in answer_rows}
    remaining_questions = [
        dict(row)
        for row in extracted["missing_questions"]
        if str(row.get("field") or "").strip().lower() not in answered_fields
    ]
    out["fields"] = existing_fields
    out["missing_questions"] = remaining_questions
    out["operator_answers"] = operator_answers
    out["approval"] = dict(extracted["approval"])
    if out["approval"].get("status") == "approved":
        out["approval"]["status"] = "pending"
        out["approval"]["reason"] = "operator_answers_changed_draft"
    out["signature"] = _stable_hash(
        {
            "agent_key": extracted["agent_key"],
            "draft_type": extracted["draft_type"],
            "operator_request": str(out.get("operator_request") or ""),
            "fields": existing_fields,
            "context": dict(out.get("context")) if isinstance(out.get("context"), Mapping) else {},
            "warnings": list(out.get("warnings")) if isinstance(out.get("warnings"), list) else [],
            "missing_questions": remaining_questions,
            "proposed_actions": extracted["proposed_actions"],
            "source_refs": list(out.get("source_refs")) if isinstance(out.get("source_refs"), list) else [],
            "approval": out["approval"],
            "operator_answers": out["operator_answers"],
        }
    )
    return out


def build_ai_agent_apply_plan(payload: Mapping[str, object] | None) -> dict[str, object]:
    extracted = extract_ai_agent_draft_payload(payload)
    missing = extracted["missing_questions"]
    approval = extracted["approval"]
    approval_required = bool(approval.get("required", True))
    approval_status = str(approval.get("status") or ("pending" if approval_required else "not_required")).strip().lower()
    blocking_missing = [row for row in missing if bool(row.get("blocking", True))]
    safe_to_apply = bool(extracted["is_contract"]) and not blocking_missing and (
        not approval_required or approval_status == "approved"
    )
    status = "ready" if safe_to_apply else "blocked"
    if blocking_missing:
        reason = "missing_required_confirmations"
    elif approval_required and approval_status != "approved":
        reason = "pending_human_approval"
    elif not extracted["is_contract"]:
        reason = "invalid_contract"
    else:
        reason = "ready"
    return {
        "status": status,
        "reason": reason,
        "safe_to_apply": safe_to_apply,
        "agent_key": extracted["agent_key"],
        "draft_type": extracted["draft_type"],
        "actions": extracted["proposed_actions"],
        "missing_questions": blocking_missing,
        "approval_required": approval_required,
        "approval_status": approval_status,
        "signature": extracted["signature"],
    }
