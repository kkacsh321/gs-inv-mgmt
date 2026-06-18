from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select

from app.db.models import AuditLog, IntegrationQueueJob
from app.services.business_agents import (
    BUSINESS_CHAT_ROOM_AGENT_ORDER,
    build_business_chat_room_roster,
    detect_business_agent_mentions,
    get_business_agent,
    resolve_business_agent_key,
)
from app.services.workflow_contracts import (
    apply_ai_agent_question_answers,
    build_ai_agent_apply_plan,
    build_ai_agent_draft_payload,
    extract_ai_agent_draft_payload,
    parse_ai_agent_answer_prompt,
)
from app.utils.time import utcnow_naive


BUSINESS_CHAT_ENTITY_TYPE = "business_chat_room"
DEFAULT_BUSINESS_ROOM_KEY = "goldenstackers_business"
ALL_BUSINESS_ROOM_AGENT_KEYS = tuple(BUSINESS_CHAT_ROOM_AGENT_ORDER)
BUSINESS_ROOM_WRITE_ACTION_WORDS = (
    "apply",
    "archive",
    "change",
    "create",
    "delete",
    "draft listing",
    "intake product",
    "list this",
    "make listing",
    "post",
    "publish",
    "revise",
    "send",
    "submit",
    "sync",
    "update",
)
BUSINESS_ROOM_ACTION_ROUTES: dict[str, dict[str, str]] = {
    "intake": {
        "label": "Inventory Intake",
        "agent_key": "kurt_intake_agent",
        "recommended_workflow": "inventory_intake_wizard",
        "next_step": "Review/create an intake draft with lot, source, cost-basis, quantity, and media evidence.",
    },
    "listing": {
        "label": "Listing Draft",
        "agent_key": "murdock_listing_agent",
        "recommended_workflow": "listing_wizard",
        "next_step": "Review/create a listing draft with title, eBay category, condition, specifics, media, price, and publish blockers.",
    },
    "accounting": {
        "label": "Accounting Review",
        "agent_key": "goldie_accountant_agent",
        "recommended_workflow": "goldie",
        "next_step": "Review accounting evidence and prepare corrections for normal approval-gated accounting workflows.",
    },
    "pricing": {
        "label": "Research/Pricing",
        "agent_key": "research_pricing_agent",
        "recommended_workflow": "tools_comp",
        "next_step": "Run comp/pricing evidence review before applying listing price or margin assumptions.",
    },
    "business_monitor": {
        "label": "Business Monitor",
        "agent_key": "business_monitor_agent",
        "recommended_workflow": "operations_home",
        "next_step": "Review operational priority/status and route to the relevant workspace.",
    },
    "general": {
        "label": "General Business Action",
        "agent_key": "business_monitor_agent",
        "recommended_workflow": "business_chat_room",
        "next_step": "Clarify the target workflow before applying any changes.",
    },
}
BUSINESS_ROOM_WORKFLOW_PAGE_PATHS: dict[str, str] = {
    "inventory_intake_wizard": "pages/23_Inventory_Intake_Wizard.py",
    "listing_wizard": "pages/26_Listing_Wizard.py",
    "goldie": "pages/28_Goldie.py",
    "tools_comp": "pages/06_Tools.py",
    "operations_home": "pages/00_Operations_Home.py",
    "business_chat_room": "pages/19_Business_Chat_Room.py",
}


def _room_entity_id(room_key: str) -> int:
    digest = hashlib.sha256(str(room_key or DEFAULT_BUSINESS_ROOM_KEY).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % 2_000_000_000


def normalize_business_room_key(room_key: str = "") -> str:
    return "_".join(str(room_key or DEFAULT_BUSINESS_ROOM_KEY).strip().lower().split()) or DEFAULT_BUSINESS_ROOM_KEY


def business_room_workflow_page_path(workflow_key: str) -> str:
    return BUSINESS_ROOM_WORKFLOW_PAGE_PATHS.get(str(workflow_key or "").strip().lower(), "")


def build_business_room_attachment_evidence_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    rows: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        stored_ref = item.get("stored_ref") if isinstance(item.get("stored_ref"), dict) else {}
        row = {
            "filename": str(item.get("filename") or "").strip(),
            "kind": str(item.get("kind") or "").strip(),
            "content_type": str(item.get("content_type") or "").strip(),
            "size_bytes": int(item.get("size_bytes") or 0),
            "stored_as": str(stored_ref.get("entity_type") or "").strip(),
            "stored_id": int(stored_ref.get("entity_id") or 0),
            "error": str(item.get("error") or "").strip(),
        }
        if row["filename"] or row["kind"] or row["stored_id"] or row["error"]:
            rows.append(row)
    return rows


def build_business_room_operator_answer_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    def _normalize_display_answer(field: str, value: Any) -> Any:
        normalized_field = str(field or "").strip().lower()
        if normalized_field not in {"item_specifics", "specifics", "aspects", "ebay_aspects"} or not isinstance(value, str):
            return value
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

    rows: list[dict[str, Any]] = []
    draft_contract = payload.get("draft_contract") if isinstance(payload.get("draft_contract"), dict) else {}
    if isinstance(draft_contract.get("operator_answers"), list):
        rows.extend([dict(row) for row in draft_contract.get("operator_answers", []) if isinstance(row, dict)])
    if isinstance(payload.get("operator_answers"), list):
        rows.extend([dict(row) for row in payload.get("operator_answers", []) if isinstance(row, dict)])
    out: list[dict[str, Any]] = []
    by_answer: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        field = str(row.get("field") or "").strip()
        raw_answer = _normalize_display_answer(field, row.get("answer", row.get("value", "")))
        if isinstance(raw_answer, (dict, list)):
            answer = json.dumps(raw_answer, sort_keys=True, separators=(",", ":"))
        else:
            answer = str(raw_answer).strip()
        source = str(row.get("source") or "").strip()
        actor = str(row.get("actor") or "").strip()
        key = (field, answer)
        if not field or not answer:
            continue
        existing = by_answer.get(key)
        candidate = {
            "field": field,
            "answer": answer,
            "source": source,
            "actor": actor,
        }
        if existing is None or (
            (not existing.get("source") and source) or (not existing.get("actor") and actor)
        ):
            by_answer[key] = candidate
    return list(by_answer.values())


def infer_business_room_attachment_kind(content_type: str, filename: str = "") -> str:
    normalized_type = str(content_type or "").strip().lower()
    normalized_name = str(filename or "").strip().lower()
    if normalized_type.startswith("image/"):
        return "image"
    if normalized_type.startswith("video/") or normalized_name.endswith((".mov", ".mp4", ".m4v", ".webm")):
        return "video"
    if normalized_type == "application/pdf" or normalized_name.endswith(".pdf"):
        return "pdf"
    if normalized_type.startswith("text/") or normalized_name.endswith((".csv", ".txt", ".md", ".json")):
        return "text"
    return "document"


def is_business_room_write_intent(message: str) -> bool:
    text = f" {str(message or '').strip().lower()} "
    if not text.strip():
        return False
    if parse_ai_agent_answer_prompt(message):
        return False
    return any(f" {word} " in text or word in text for word in BUSINESS_ROOM_WRITE_ACTION_WORDS)


def build_business_room_agent_answer_evidence(
    *,
    message: str,
    actor: str,
    source: str = "business_chat_room",
) -> dict[str, Any]:
    parsed = parse_ai_agent_answer_prompt(message)
    if not parsed:
        return {}
    agent_key = resolve_business_agent_key(str(parsed.get("agent") or "")) or ""
    agent = get_business_agent(agent_key) if agent_key else None
    return {
        "type": "ai_agent_answer",
        "agent": str(parsed.get("agent") or "").strip().lower(),
        "agent_key": agent_key,
        "agent_label": agent.label if agent else "",
        "field": str(parsed.get("field") or "").strip(),
        "answer": str(parsed.get("answer") or "").strip(),
        "target_queue_job_id": int(parsed.get("target_queue_job_id") or 0),
        "target_draft_id": int(parsed.get("target_draft_id") or 0),
        "actor": str(actor or "").strip(),
        "source": str(source or "business_chat_room").strip(),
        "captured_at_utc": utcnow_naive().isoformat(timespec="seconds"),
        "write_executed": False,
    }


def _workflow_key_for_agent_answer(agent_key: str) -> str:
    normalized = str(agent_key or "").strip().lower()
    if normalized == "kurt_intake_agent":
        return "inventory_intake_wizard"
    if normalized == "murdock_listing_agent":
        return "listing_wizard"
    return ""


def _answer_agent_name_for_workflow(workflow_key: str, review_card: dict[str, Any] | None = None) -> str:
    agent_key = str((review_card or {}).get("agent_key") or "").strip().lower()
    if agent_key:
        agent = get_business_agent(agent_key)
        if agent:
            return agent.name.lower()
    normalized = str(workflow_key or "").strip().lower()
    if normalized == "inventory_intake_wizard":
        return "kurt"
    if normalized == "listing_wizard":
        return "murdock"
    return "agent"


def _business_room_answer_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(row.get("agent_key") or row.get("agent") or "").strip().lower(),
        re.sub(r"[^a-zA-Z0-9_]+", "_", str(row.get("field") or "").strip().lower()).strip("_"),
        json.dumps(row.get("answer", row.get("value")), sort_keys=True, separators=(",", ":"), default=str),
        str(row.get("actor") or "").strip(),
        str(row.get("source") or "").strip(),
        str(row.get("target_queue_job_id") or row.get("target_draft_id") or "").strip(),
    )


def _dedupe_business_room_answer_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _business_room_answer_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def build_business_room_answer_command_suggestions(
    handoff: dict[str, Any],
    *,
    review_card: dict[str, Any] | None = None,
    max_suggestions: int = 3,
) -> list[str]:
    card = review_card if isinstance(review_card, dict) else build_business_room_handoff_review_card(handoff)
    workflow_key = str(card.get("workflow_key") or handoff.get("workflow_key") or "").strip().lower()
    agent_name = _answer_agent_name_for_workflow(workflow_key, card)
    queue_job_id = 0
    draft_id = 0
    try:
        queue_job_id = max(0, int(handoff.get("queue_job_id") or 0))
    except Exception:
        queue_job_id = 0
    try:
        draft_id = max(0, int(handoff.get("id") or 0))
    except Exception:
        draft_id = 0
    target = f"handoff {queue_job_id}" if queue_job_id else (f"draft {draft_id}" if draft_id else "")
    questions = [
        row
        for row in card.get("missing_questions", [])
        if isinstance(row, dict) and str(row.get("field") or row.get("question") or "").strip()
    ]

    def _placeholder_for_field(field: str) -> str:
        normalized = str(field or "").strip().lower()
        if normalized in {"quantity", "qty"}:
            return "20"
        if normalized in {"product_id", "product_link"}:
            return "123"
        if normalized in {"title", "listing_title"}:
            return "eBay-safe title"
        if normalized in {"description", "description_html", "listing_description"}:
            return "<p>Buyer-facing description.</p>"
        if normalized in {"category", "category_id", "ebay_category_id"}:
            return "261"
        if normalized in {"condition", "condition_id", "ebay_condition_id"}:
            return "3000"
        if normalized in {"main_image_id", "media", "image_id", "primary_image_id"}:
            return "media_asset_id"
        if normalized in {"item_specifics", "specifics", "aspects", "ebay_aspects"}:
            return "Brand=Golden Stackers; Fineness=0.999"
        if "cost" in normalized or "price" in normalized or "total" in normalized:
            return "0.00"
        return "VALUE"

    suggestions: list[str] = []
    for row in questions[: max(1, min(int(max_suggestions or 3), 10))]:
        field = str(row.get("field") or row.get("question") or "field").strip()
        field = re.sub(r"[^a-zA-Z0-9_]+", " ", field).strip().replace(" ", "_").lower() or "field"
        placeholder = _placeholder_for_field(field)
        command_parts = [agent_name, "answer"]
        if target:
            command_parts.append(target)
        command_parts.append(f"{field}: {placeholder}")
        suggestions.append(" ".join(command_parts))
    cost_guardrail = card.get("cost_basis_guardrail") if isinstance(card.get("cost_basis_guardrail"), dict) else {}
    if (
        cost_guardrail.get("ambiguous_acquisition_cost")
        and len(suggestions) < max(1, min(int(max_suggestions or 3), 10))
    ):
        for field in ("product_unit_cost", "lot_landed_total", "assignment_landed_cost"):
            if len(suggestions) >= max(1, min(int(max_suggestions or 3), 10)):
                break
            command_parts = [agent_name, "answer"]
            if target:
                command_parts.append(target)
            command_parts.append(f"{field}: 0.00")
            command = " ".join(command_parts)
            if command not in suggestions:
                suggestions.append(command)
    if workflow_key == "listing_wizard" and len(suggestions) < max(1, min(int(max_suggestions or 3), 10)):
        readiness_answer_fields = {
            "product_link": "product_id",
            "title": "title",
            "description": "description_html",
            "category": "category_id",
            "condition": "condition_id",
            "price": "suggested_price",
            "media": "main_image_id",
            "item_specifics": "item_specifics",
        }
        existing_fields = {
            re.sub(r"[^a-zA-Z0-9_]+", " ", str(item).strip()).strip().replace(" ", "_").lower()
            for item in re.findall(r"\banswer\b(?:\s+(?:handoff|draft)\s+\d+)?\s+([a-zA-Z0-9_ ]+):", "\n".join(suggestions))
        }
        for row in card.get("listing_readiness_checks", []):
            if len(suggestions) >= max(1, min(int(max_suggestions or 3), 10)):
                break
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status not in {"blocker", "review"}:
                continue
            field = readiness_answer_fields.get(str(row.get("key") or "").strip().lower(), "")
            if not field or field in existing_fields:
                continue
            placeholder = _placeholder_for_field(field)
            command_parts = [agent_name, "answer"]
            if target:
                command_parts.append(target)
            command_parts.append(f"{field}: {placeholder}")
            command = " ".join(command_parts)
            if command not in suggestions:
                suggestions.append(command)
                existing_fields.add(field)
    return suggestions


def apply_business_room_agent_answer_to_latest_handoff(
    repo: Any,
    *,
    environment: str,
    username: str,
    answer_evidence: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    if not isinstance(answer_evidence, dict) or not answer_evidence.get("field"):
        return {"applied": False, "reason": "missing_answer_evidence"}
    agent_key = str(answer_evidence.get("agent_key") or "").strip()
    if not agent_key:
        agent_key = resolve_business_agent_key(str(answer_evidence.get("agent") or ""))
    workflow_key = _workflow_key_for_agent_answer(agent_key)
    if not workflow_key:
        return {"applied": False, "reason": "unsupported_agent"}
    target_queue_job_id = 0
    target_draft_id = 0
    try:
        target_queue_job_id = max(0, int(answer_evidence.get("target_queue_job_id") or 0))
    except Exception:
        target_queue_job_id = 0
    try:
        target_draft_id = max(0, int(answer_evidence.get("target_draft_id") or 0))
    except Exception:
        target_draft_id = 0
    handoffs = list_business_room_workflow_handoffs(
        repo,
        environment=environment,
        workflow_key=workflow_key,
        username=username,
        limit=50 if (target_queue_job_id or target_draft_id) else 1,
    )
    if target_queue_job_id:
        handoffs = [row for row in handoffs if int(row.get("queue_job_id") or 0) == target_queue_job_id]
    if target_draft_id:
        handoffs = [row for row in handoffs if int(row.get("id") or 0) == target_draft_id]
    if not handoffs:
        return {
            "applied": False,
            "reason": "target_handoff_not_found" if (target_queue_job_id or target_draft_id) else "no_active_handoff",
            "workflow_key": workflow_key,
            "target_queue_job_id": target_queue_job_id,
            "target_draft_id": target_draft_id,
        }
    handoff = handoffs[0]
    payload = dict(handoff.get("payload") or {})
    draft_contract = payload.get("draft_contract") if isinstance(payload.get("draft_contract"), dict) else {}
    if not draft_contract:
        return {
            "applied": False,
            "reason": "handoff_missing_draft_contract",
            "workflow_key": workflow_key,
            "scope_key": str(handoff.get("scope_key") or ""),
        }
    updated_contract = apply_ai_agent_question_answers(
        draft_contract,
        [answer_evidence],
        actor=actor,
        source=str(answer_evidence.get("source") or "business_chat_room"),
    )
    payload["draft_contract"] = updated_contract
    payload["apply_plan"] = build_ai_agent_apply_plan(updated_contract)
    operator_answers = payload.get("operator_answers") if isinstance(payload.get("operator_answers"), list) else []
    payload["operator_answers"] = _dedupe_business_room_answer_rows(list(operator_answers) + [dict(answer_evidence)])
    payload["updated_at_utc"] = utcnow_naive().isoformat(timespec="seconds")
    payload["status"] = str(payload.get("status") or "handoff_pending_review")
    row = repo.save_workflow_draft(
        environment=environment,
        workflow_key=workflow_key,
        username=username,
        scope_key=str(handoff.get("scope_key") or ""),
        draft_payload=payload,
        schema_version="business_room_action_handoff_v1",
        status="active",
        last_step="business_room_answer_applied",
        actor=actor,
    )
    return {
        "applied": True,
        "reason": "applied_to_latest_handoff",
        "workflow_key": workflow_key,
        "scope_key": str(handoff.get("scope_key") or ""),
        "draft_id": int(getattr(row, "id", 0) or handoff.get("id") or 0),
        "apply_plan": payload["apply_plan"],
    }


def business_room_action_id(message: str, *, actor: str, room_key: str = DEFAULT_BUSINESS_ROOM_KEY) -> str:
    seed = {
        "room_key": normalize_business_room_key(room_key),
        "actor": str(actor or "").strip().lower(),
        "message": str(message or "").strip()[:4000],
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return f"business_room_action:{seed['room_key']}:{digest}"


def classify_business_room_action_route(
    *,
    message: str,
    directed_to: list[str] | None = None,
) -> dict[str, Any]:
    text = f" {str(message or '').strip().lower()} "
    directed = [resolve_business_agent_key(item) or str(item or "").strip().lower() for item in (directed_to or [])]
    if "kurt_intake_agent" in directed:
        route_key = "intake"
    elif "murdock_listing_agent" in directed:
        route_key = "listing"
    elif "goldie_accountant_agent" in directed:
        route_key = "accounting"
    elif "research_pricing_agent" in directed:
        route_key = "pricing"
    elif "business_monitor_agent" in directed:
        route_key = "business_monitor"
    elif any(token in text for token in (" listing", " ebay", " auction", " title", " description", " publish", " revise")):
        route_key = "listing"
    elif any(token in text for token in (" intake", " inventory", " product", " lot", " purchase", " invoice", " source")):
        route_key = "intake"
    elif any(token in text for token in (" accounting", " cogs", " cost basis", " tax", " profit", " margin")):
        route_key = "accounting"
    elif any(token in text for token in (" comp", " pricing", " price", " research", " market", " sold")):
        route_key = "pricing"
    elif any(token in text for token in (" status", " priority", " backlog", " sync", " health")):
        route_key = "business_monitor"
    else:
        route_key = "general"
    route = dict(BUSINESS_ROOM_ACTION_ROUTES[route_key])
    route["route_key"] = route_key
    route["directed_to"] = list(dict.fromkeys(item for item in directed if item))
    return route


def _infer_intake_category_from_prompt(message: str) -> tuple[str, float]:
    text = f" {str(message or '').strip().lower()} "
    if any(token in text for token in (" coin", " coins", " morgan", " peace dollar", " quarter", " dime", " nickel")):
        return "coins", 0.65
    if any(token in text for token in (" bullion", " silver", " gold", " copper", " bar", " round", " ingot")):
        return "bullion", 0.65
    if any(token in text for token in (" antique", " vintage")):
        return "antiques", 0.55
    if any(token in text for token in (" collectible", " commemorative", " set")):
        return "collectibles", 0.55
    return "other", 0.35


def build_business_room_action_draft_contract(
    *,
    message: str,
    action_route: dict[str, Any],
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    route_key = str(action_route.get("route_key") or "").strip().lower()
    prompt = str(message or "").strip()
    attachment_count = len(attachments or [])
    if route_key == "listing":
        product_id = _first_entity_id(prompt, "product")
        listing_id = _first_entity_id(prompt, "listing")
        title = _first_quoted_text(prompt)
        fields = [
            {
                "key": "product_id",
                "label": "Product ID",
                "value": product_id or "",
                "confidence": 0.85 if product_id else 0.0,
                "source": "business_room_prompt_hint",
                "missing_reason": "product_id is needed to apply listing suggestions",
            },
            {
                "key": "listing_id",
                "label": "Listing ID",
                "value": listing_id or "",
                "confidence": 0.75 if listing_id else 0.0,
                "source": "business_room_prompt_hint",
            },
            {
                "key": "title",
                "label": "eBay Title",
                "value": title,
                "confidence": 0.65 if title else 0.0,
                "source": "business_room_prompt_hint",
            },
            {
                "key": "description_html",
                "label": "eBay Description HTML",
                "value": "",
                "confidence": 0.0,
                "source": "pending_murdock_review",
                "missing_reason": "Murdock or the Listing Wizard must generate/review eBay-safe description copy",
            },
        ]
        return build_ai_agent_draft_payload(
            agent_key="murdock_listing_agent",
            draft_type="listing",
            operator_request=prompt,
            fields=fields,
            context={
                "source": "business_chat_room",
                "route": dict(action_route),
                "attachment_count": attachment_count,
            },
            warnings=[
                "Room-origin listing drafts are suggestions until reviewed in Listing Wizard.",
                "Review title, description, condition, category, item specifics, media, and price before publishing.",
            ],
            proposed_actions=[
                {
                    "action": "review_listing_wizard_handoff",
                    "requires_approval": True,
                    "target_product_id": product_id or None,
                    "target_listing_id": listing_id or None,
                }
            ],
            source_refs=[{"kind": "business_chat_room_prompt"}],
            approval_required=True,
        )
    if route_key == "intake":
        title = _first_quoted_text(prompt)
        quantity = _first_quantity(prompt)
        cost = _first_money_amount(prompt)
        category, category_confidence = _infer_intake_category_from_prompt(prompt)
        fields = [
            {
                "key": "title",
                "label": "Title",
                "value": title,
                "confidence": 0.65 if title else 0.0,
                "source": "business_room_prompt_hint",
            },
            {
                "key": "category",
                "label": "Category",
                "value": category,
                "confidence": category_confidence,
                "source": "business_room_prompt_hint",
            },
            {
                "key": "quantity",
                "label": "Quantity",
                "value": quantity or "",
                "confidence": 0.8 if quantity else 0.0,
                "source": "business_room_prompt_hint",
            },
            {
                "key": "acquisition_cost",
                "label": "Acquisition Cost",
                "value": cost,
                "confidence": 0.75 if cost else 0.0,
                "source": "business_room_prompt_hint",
                "missing_reason": "cost basis is required before accounting close",
            },
        ]
        return build_ai_agent_draft_payload(
            agent_key="kurt_intake_agent",
            draft_type="intake",
            operator_request=prompt,
            fields=fields,
            context={
                "source": "business_chat_room",
                "route": dict(action_route),
                "attachment_count": attachment_count,
            },
            warnings=[
                "Room-origin intake drafts require operator review for lot/source/cost-basis evidence before submit.",
            ],
            proposed_actions=[
                {
                    "action": "review_inventory_intake_handoff",
                    "requires_approval": True,
                }
            ],
            source_refs=[{"kind": "business_chat_room_prompt"}],
            approval_required=True,
        )
    return {}


def queue_business_room_action_request(
    repo: Any,
    *,
    room_key: str = DEFAULT_BUSINESS_ROOM_KEY,
    message: str,
    actor: str,
    user_role: str = "",
    directed_to: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    source_message_id: int = 0,
    environment: str = "local",
) -> dict[str, Any]:
    action_route = classify_business_room_action_route(message=message, directed_to=directed_to)
    draft_contract = build_business_room_action_draft_contract(
        message=message,
        action_route=action_route,
        attachments=list(attachments or []),
    )
    payload = {
        "idempotency_key": business_room_action_id(message, actor=actor, room_key=room_key),
        "received_at": utcnow_naive().isoformat(timespec="seconds"),
        "source": "business_chat_room",
        "room_key": normalize_business_room_key(room_key),
        "source_message_id": int(source_message_id or 0),
        "intent": "business_room_write_action_request",
        "prompt": str(message or "").strip()[:4000],
        "requester": {"username": actor, "role": str(user_role or "").strip()},
        "directed_to": list(directed_to or []),
        "action_route": action_route,
        "attachments": list(attachments or []),
        "approval": {
            "required": True,
            "status": "pending",
            "requested_at": utcnow_naive().isoformat(timespec="seconds"),
            "requested_by": actor,
        },
        "execution": {
            "status": "blocked_pending_approval",
            "note": "Business Chat Room action requests require human approval and workflow-specific executors before writes run.",
        },
    }
    if draft_contract:
        payload["draft_contract"] = draft_contract
        payload["apply_plan"] = build_ai_agent_apply_plan(draft_contract)
    queued = repo.create_integration_queue_job(
        environment=environment,
        integration="business_chat_room",
        action="write_action_request",
        payload_json=json.dumps(payload, sort_keys=True),
        requested_by=actor,
        max_retries=0,
        actor=actor,
    )
    queue_job_id = int(getattr(queued, "id", 0) or 0)
    if hasattr(repo, "update_integration_queue_job"):
        repo.update_integration_queue_job(
            queue_job_id,
            {
                "status": "blocked",
                "last_error": "Awaiting human approval for Business Chat Room write-action request.",
            },
            actor=actor,
        )
    return {
        "queue_job_id": queue_job_id,
        "status": "pending_approval",
        "integration": "business_chat_room",
        "action": "write_action_request",
        "idempotency_key": payload["idempotency_key"],
        "action_route": action_route,
        "draft_signature": str((draft_contract or {}).get("signature") or ""),
        "has_draft_contract": bool(draft_contract),
    }


def list_business_room_action_requests(
    repo: Any,
    *,
    environment: str,
    statuses: set[str] | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    rows = repo.list_integration_queue_jobs(
        environment=environment,
        integration="business_chat_room",
        statuses=statuses or {"blocked", "queued", "running", "failed"},
        limit=max(1, min(int(limit or 25), 200)),
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(str(getattr(row, "payload_json", "") or "{}"))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
        prompt = str(payload.get("prompt") or "").strip()
        approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
        action_route = payload.get("action_route") if isinstance(payload.get("action_route"), dict) else {}
        workflow = str(action_route.get("recommended_workflow") or "").strip()
        output.append(
            {
                "id": int(getattr(row, "id", 0) or 0),
                "status": str(getattr(row, "status", "") or "").strip(),
                "requested_by": str(getattr(row, "requested_by", "") or payload.get("requester", {}).get("username", "")),
                "prompt": prompt[:240],
                "approval_status": str(approval.get("status") or "").strip(),
                "directed_to": list(payload.get("directed_to") or []) if isinstance(payload.get("directed_to"), list) else [],
                "source_message_id": int(payload.get("source_message_id") or 0),
                "attachment_count": len(payload.get("attachments") or []) if isinstance(payload.get("attachments"), list) else 0,
                "route": str(action_route.get("route_key") or ""),
                "route_label": str(action_route.get("label") or action_route.get("route_key") or ""),
                "workflow": workflow,
                "workflow_page": business_room_workflow_page_path(workflow),
                "next_step": str(action_route.get("next_step") or ""),
                "has_draft_contract": isinstance(payload.get("draft_contract"), dict) and bool(payload.get("draft_contract")),
                "draft_signature": str((payload.get("draft_contract") or {}).get("signature") or "")[:16],
                "payload": payload,
                "last_error": str(getattr(row, "last_error", "") or "").strip()[:240],
                "next_attempt_at": getattr(row, "next_attempt_at", None),
                "created_at": getattr(row, "created_at", None),
            }
        )
    return output


def transition_business_room_action_request(
    repo: Any,
    *,
    queue_job_id: int,
    transition: str,
    actor: str,
) -> dict[str, Any]:
    normalized_transition = str(transition or "").strip().lower()
    payload: dict[str, Any] = {}
    row_before_update: Any = None
    if hasattr(repo, "db"):
        try:
            row_before_update = repo.db.get(IntegrationQueueJob, int(queue_job_id))
            parsed = json.loads(str(getattr(row_before_update, "payload_json", "") or "{}"))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
    if normalized_transition == "approve":
        updates = {
            "status": "queued",
            "last_error": "Approved from Business Chat Room; awaiting workflow-specific executor.",
        }
        if payload:
            approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
            approval.update(
                {
                    "status": "approved",
                    "approved_at": utcnow_naive().isoformat(timespec="seconds"),
                    "approved_by": actor,
                }
            )
            payload["approval"] = approval
            payload["execution"] = {
                **(payload.get("execution") if isinstance(payload.get("execution"), dict) else {}),
                "status": "queued_after_human_approval",
            }
            updates["payload_json"] = json.dumps(payload, sort_keys=True)
    elif normalized_transition == "cancel":
        updates = {
            "status": "cancelled",
            "last_error": "Cancelled from Business Chat Room.",
        }
        if payload:
            approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
            approval.update(
                {
                    "status": "cancelled",
                    "cancelled_at": utcnow_naive().isoformat(timespec="seconds"),
                    "cancelled_by": actor,
                }
            )
            payload["approval"] = approval
            payload["execution"] = {
                **(payload.get("execution") if isinstance(payload.get("execution"), dict) else {}),
                "status": "cancelled_by_human",
            }
            updates["payload_json"] = json.dumps(payload, sort_keys=True)
    else:
        raise ValueError(f"Unsupported Business Chat Room action transition `{transition}`.")
    row = repo.update_integration_queue_job(int(queue_job_id), updates, actor=actor)
    approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
    return {
        "queue_job_id": int(queue_job_id),
        "transition": normalized_transition,
        "status": str(getattr(row, "status", updates["status"]) or updates["status"]),
        "last_error": str(getattr(row, "last_error", updates["last_error"]) or updates["last_error"]),
        "approval_status": str(approval.get("status") or ""),
    }


def build_business_room_action_draft_payload(
    *,
    queue_job_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    action_route = payload.get("action_route") if isinstance(payload.get("action_route"), dict) else {}
    requester = payload.get("requester") if isinstance(payload.get("requester"), dict) else {}
    draft_contract = payload.get("draft_contract") if isinstance(payload.get("draft_contract"), dict) else {}
    apply_plan = payload.get("apply_plan") if isinstance(payload.get("apply_plan"), dict) else {}
    answer_rows: list[dict[str, Any]] = []
    if isinstance(payload.get("ai_agent_answer"), dict):
        answer_rows.append(dict(payload.get("ai_agent_answer") or {}))
    if isinstance(payload.get("operator_answers"), list):
        answer_rows.extend([dict(row) for row in payload.get("operator_answers", []) if isinstance(row, dict)])
    answer_rows = _dedupe_business_room_answer_rows(answer_rows)
    if draft_contract and answer_rows:
        draft_contract = apply_ai_agent_question_answers(
            draft_contract,
            answer_rows,
            actor=str(requester.get("username") or payload.get("requested_by") or "operator").strip(),
            source=str(payload.get("source") or "business_chat_room").strip() or "business_chat_room",
        )
        apply_plan = build_ai_agent_apply_plan(draft_contract)
    draft_payload = {
        "source": "business_chat_room",
        "schema": "business_room_action_handoff_v1",
        "queue_job_id": int(queue_job_id or 0),
        "room_key": str(payload.get("room_key") or DEFAULT_BUSINESS_ROOM_KEY),
        "source_message_id": int(payload.get("source_message_id") or 0),
        "prompt": str(payload.get("prompt") or "").strip(),
        "requester": dict(requester),
        "directed_to": list(payload.get("directed_to") or []) if isinstance(payload.get("directed_to"), list) else [],
        "attachments": list(payload.get("attachments") or []) if isinstance(payload.get("attachments"), list) else [],
        "action_route": dict(action_route),
        "approval": payload.get("approval") if isinstance(payload.get("approval"), dict) else {},
        "created_at_utc": utcnow_naive().isoformat(timespec="seconds"),
        "status": "handoff_pending_review",
    }
    if draft_contract:
        draft_payload["draft_contract"] = dict(draft_contract)
    if apply_plan:
        draft_payload["apply_plan"] = dict(apply_plan)
    if answer_rows:
        draft_payload["operator_answers"] = answer_rows
    return draft_payload


def save_business_room_action_workflow_draft(
    repo: Any,
    *,
    environment: str,
    queue_job_id: int,
    payload: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    action_route = payload.get("action_route") if isinstance(payload.get("action_route"), dict) else {}
    workflow_key = str(action_route.get("recommended_workflow") or "business_chat_room").strip().lower()
    requester = payload.get("requester") if isinstance(payload.get("requester"), dict) else {}
    username = str(requester.get("username") or actor or "system").strip() or "system"
    scope_key = f"business_chat_room:{int(queue_job_id or 0)}"
    draft_payload = build_business_room_action_draft_payload(queue_job_id=queue_job_id, payload=payload)
    row = repo.save_workflow_draft(
        environment=environment,
        workflow_key=workflow_key,
        username=username,
        scope_key=scope_key,
        draft_payload=draft_payload,
        schema_version="business_room_action_handoff_v1",
        status="active",
        last_step="business_room_handoff",
        actor=actor,
    )
    return {
        "draft_id": int(getattr(row, "id", 0) or 0),
        "workflow_key": workflow_key,
        "username": username,
        "scope_key": scope_key,
    }


def list_business_room_workflow_handoffs(
    repo: Any,
    *,
    environment: str,
    workflow_key: str,
    username: str = "",
    limit: int = 25,
) -> list[dict[str, Any]]:
    rows = repo.list_workflow_drafts(
        environment=environment,
        workflow_key=workflow_key,
        username=username,
        active_only=True,
        limit=max(1, min(int(limit or 25), 200)),
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        scope_key = str(getattr(row, "scope_key", "") or "").strip()
        if not scope_key.startswith("business_chat_room:"):
            continue
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(str(getattr(row, "draft_json", "") or "{}"))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
        action_route = payload.get("action_route") if isinstance(payload.get("action_route"), dict) else {}
        requester = payload.get("requester") if isinstance(payload.get("requester"), dict) else {}
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        try:
            queue_job_id = int(payload.get("queue_job_id") or scope_key.rsplit(":", 1)[-1] or 0)
        except Exception:
            queue_job_id = 0
        output.append(
            {
                "id": int(getattr(row, "id", 0) or 0),
                "scope_key": scope_key,
                "queue_job_id": queue_job_id,
                "workflow_key": str(getattr(row, "workflow_key", "") or workflow_key).strip(),
                "status": str(payload.get("status") or getattr(row, "status", "") or "").strip(),
                "prompt": str(payload.get("prompt") or "").strip(),
                "requester": str(requester.get("username") or getattr(row, "username", "") or "").strip(),
                "route": str(action_route.get("route_key") or "").strip(),
                "route_label": str(action_route.get("label") or action_route.get("route_key") or "").strip(),
                "next_step": str(action_route.get("next_step") or "").strip(),
                "directed_to": list(payload.get("directed_to") or []) if isinstance(payload.get("directed_to"), list) else [],
                "attachment_count": len(attachments),
                "source_message_id": int(payload.get("source_message_id") or 0),
                "created_at": getattr(row, "created_at", None),
                "updated_at": getattr(row, "updated_at", None),
                "payload": payload,
            }
        )
        if len(output) >= max(1, min(int(limit or 25), 200)):
            break
    return output


def list_business_room_active_workflow_handoffs(
    repo: Any,
    *,
    environment: str,
    username: str = "",
    workflow_keys: list[str] | None = None,
    limit_per_workflow: int = 10,
) -> list[dict[str, Any]]:
    keys = workflow_keys or [
        key for key in BUSINESS_ROOM_WORKFLOW_PAGE_PATHS.keys() if key != "business_chat_room"
    ]
    output: list[dict[str, Any]] = []
    for workflow_key in keys:
        rows = list_business_room_workflow_handoffs(
            repo,
            environment=environment,
            workflow_key=workflow_key,
            username=username,
            limit=limit_per_workflow,
        )
        for row in rows:
            enriched = dict(row)
            enriched["workflow_page"] = business_room_workflow_page_path(str(row.get("workflow_key") or workflow_key))
            output.append(enriched)
    output.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return output


def _agent_key_for_business_room_row(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    draft_contract = payload.get("draft_contract") if isinstance(payload.get("draft_contract"), dict) else {}
    agent_key = resolve_business_agent_key(str(draft_contract.get("agent_key") or ""))
    if agent_key:
        return agent_key
    action_route = (
        row.get("action_route")
        if isinstance(row.get("action_route"), dict)
        else payload.get("action_route")
        if isinstance(payload.get("action_route"), dict)
        else {}
    )
    agent_key = resolve_business_agent_key(str(action_route.get("agent_key") or ""))
    if agent_key:
        return agent_key
    route_key = str(row.get("route") or action_route.get("route_key") or "").strip().lower()
    route = BUSINESS_ROOM_ACTION_ROUTES.get(route_key) if route_key else None
    if route:
        agent_key = resolve_business_agent_key(str(route.get("agent_key") or ""))
        if agent_key:
            return agent_key
    directed_to = row.get("directed_to") if isinstance(row.get("directed_to"), list) else payload.get("directed_to")
    if isinstance(directed_to, list):
        for item in directed_to:
            agent_key = resolve_business_agent_key(str(item or ""))
            if agent_key:
                return agent_key
    workflow_key = str(row.get("workflow") or row.get("workflow_key") or action_route.get("recommended_workflow") or "").strip().lower()
    workflow_agent_map = {
        "inventory_intake_wizard": "kurt_intake_agent",
        "listing_wizard": "murdock_listing_agent",
        "goldie": "goldie_accountant_agent",
        "tools_comp": "research_pricing_agent",
        "operations_home": "business_monitor_agent",
    }
    return workflow_agent_map.get(workflow_key, "business_monitor_agent")


def _draft_contract_missing_question_count(payload: dict[str, Any]) -> int:
    draft_contract = payload.get("draft_contract") if isinstance(payload.get("draft_contract"), dict) else {}
    missing_questions = (
        draft_contract.get("missing_questions")
        if isinstance(draft_contract.get("missing_questions"), list)
        else []
    )
    return len([row for row in missing_questions if isinstance(row, dict)])


def build_business_room_agent_workload_summary(
    *,
    action_rows: list[dict[str, Any]],
    handoff_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for roster_row in build_business_chat_room_roster():
        agent_key = str(roster_row.get("key") or "").strip()
        summary[agent_key] = {
            "agent_key": agent_key,
            "agent": str(roster_row.get("name") or "").strip(),
            "label": str(roster_row.get("label") or "").strip(),
            "pending_action_count": 0,
            "queued_action_count": 0,
            "failed_action_count": 0,
            "active_handoff_count": 0,
            "missing_question_count": 0,
            "operator_answer_count": 0,
            "attachment_count": 0,
            "next_answer_commands": [],
            "latest_prompt": "",
            "latest_workflow": "",
            "attention": "idle",
        }

    def _touch_latest(agent_summary: dict[str, Any], row: dict[str, Any]) -> None:
        prompt = str(row.get("prompt") or "").strip()
        if prompt and not agent_summary.get("latest_prompt"):
            agent_summary["latest_prompt"] = prompt[:160]
        workflow = str(row.get("workflow") or row.get("workflow_key") or "").strip()
        if workflow and not agent_summary.get("latest_workflow"):
            agent_summary["latest_workflow"] = workflow

    for row in action_rows or []:
        if not isinstance(row, dict):
            continue
        agent_key = _agent_key_for_business_room_row(row)
        agent_summary = summary.setdefault(
            agent_key,
            {
                "agent_key": agent_key,
                "agent": agent_key,
                "label": agent_key,
                "pending_action_count": 0,
                "queued_action_count": 0,
                "failed_action_count": 0,
                "active_handoff_count": 0,
                "missing_question_count": 0,
                "operator_answer_count": 0,
                "attachment_count": 0,
                "next_answer_commands": [],
                "latest_prompt": "",
                "latest_workflow": "",
                "attention": "idle",
            },
        )
        status = str(row.get("status") or "").strip().lower()
        approval_status = str(row.get("approval_status") or "").strip().lower()
        if status == "blocked" or approval_status == "pending":
            agent_summary["pending_action_count"] += 1
        elif status in {"queued", "running"}:
            agent_summary["queued_action_count"] += 1
        elif status == "failed":
            agent_summary["failed_action_count"] += 1
        agent_summary["attachment_count"] += int(row.get("attachment_count") or 0)
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        agent_summary["missing_question_count"] += _draft_contract_missing_question_count(payload)
        agent_summary["operator_answer_count"] += len(build_business_room_operator_answer_rows(payload))
        if _draft_contract_missing_question_count(payload):
            suggestion_row = dict(row)
            suggestion_row["workflow_key"] = str(
                row.get("workflow")
                or row.get("workflow_key")
                or payload.get("workflow_key")
                or payload.get("recommended_workflow")
                or ""
            )
            suggestion_row["queue_job_id"] = int(row.get("queue_job_id") or row.get("id") or 0)
            suggestions = build_business_room_answer_command_suggestions(
                suggestion_row,
                max_suggestions=3,
            )
            existing_commands = agent_summary["next_answer_commands"]
            for command in suggestions:
                if command not in existing_commands and len(existing_commands) < 5:
                    existing_commands.append(command)
        _touch_latest(agent_summary, row)

    for row in handoff_rows or []:
        if not isinstance(row, dict):
            continue
        agent_key = _agent_key_for_business_room_row(row)
        agent_summary = summary.setdefault(
            agent_key,
            {
                "agent_key": agent_key,
                "agent": agent_key,
                "label": agent_key,
                "pending_action_count": 0,
                "queued_action_count": 0,
                "failed_action_count": 0,
                "active_handoff_count": 0,
                "missing_question_count": 0,
                "operator_answer_count": 0,
                "attachment_count": 0,
                "next_answer_commands": [],
                "latest_prompt": "",
                "latest_workflow": "",
                "attention": "idle",
            },
        )
        agent_summary["active_handoff_count"] += 1
        agent_summary["attachment_count"] += int(row.get("attachment_count") or 0)
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        agent_summary["missing_question_count"] += _draft_contract_missing_question_count(payload)
        agent_summary["operator_answer_count"] += len(build_business_room_operator_answer_rows(payload))
        if _draft_contract_missing_question_count(payload):
            suggestions = build_business_room_answer_command_suggestions(
                row,
                max_suggestions=3,
            )
            existing_commands = agent_summary["next_answer_commands"]
            for command in suggestions:
                if command not in existing_commands and len(existing_commands) < 5:
                    existing_commands.append(command)
        _touch_latest(agent_summary, row)

    for row in summary.values():
        if int(row["failed_action_count"]):
            row["attention"] = "failed_action"
        elif int(row["pending_action_count"]):
            row["attention"] = "pending_approval"
        elif int(row["missing_question_count"]):
            row["attention"] = "needs_answers"
        elif int(row["active_handoff_count"]):
            row["attention"] = "active_handoff"
        elif int(row["queued_action_count"]):
            row["attention"] = "queued"
    ordered = [summary[key] for key in BUSINESS_CHAT_ROOM_AGENT_ORDER if key in summary]
    return ordered


def build_business_room_agent_activity_summary(
    *,
    recent_messages: list[dict[str, Any]],
    action_rows: list[dict[str, Any]],
    handoff_rows: list[dict[str, Any]],
    max_items_per_agent: int = 5,
) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for roster_row in build_business_chat_room_roster():
        agent_key = str(roster_row.get("key") or "").strip()
        summary[agent_key] = {
            "agent_key": agent_key,
            "agent": str(roster_row.get("name") or "").strip(),
            "label": str(roster_row.get("label") or "").strip(),
            "activity": [],
        }

    max_items = max(1, min(int(max_items_per_agent or 5), 20))
    sequence = 0

    def _add(agent_key: str, item: dict[str, Any]) -> None:
        nonlocal sequence
        resolved = resolve_business_agent_key(agent_key) or str(agent_key or "").strip().lower()
        if resolved not in summary:
            return
        sequence += 1
        payload = dict(item)
        payload["_sequence"] = sequence
        summary[resolved]["activity"].append(payload)

    for message in recent_messages or []:
        if not isinstance(message, dict):
            continue
        sender_key = resolve_business_agent_key(str(message.get("sender_key") or ""))
        directed_to = [
            resolve_business_agent_key(str(item or "")) or str(item or "").strip().lower()
            for item in (message.get("directed_to") or [])
            if str(item or "").strip()
        ]
        message_text = str(message.get("message") or "").strip()
        title = str(message.get("sender_label") or message.get("sender_key") or "Room message").strip()
        base_item = {
            "kind": "message",
            "status": str(message.get("source") or "").strip(),
            "title": title,
            "detail": message_text[:180],
            "created_at": message.get("created_at") or message.get("created_at_utc") or "",
        }
        if sender_key:
            _add(sender_key, {**base_item, "direction": "from_agent"})
        for target_key in directed_to:
            if target_key in ALL_BUSINESS_ROOM_AGENT_KEYS and target_key != sender_key:
                _add(target_key, {**base_item, "direction": "to_agent"})

    for row in action_rows or []:
        if not isinstance(row, dict):
            continue
        agent_key = _agent_key_for_business_room_row(row)
        _add(
            agent_key,
            {
                "kind": "action_request",
                "status": str(row.get("approval_status") or row.get("status") or "").strip(),
                "title": f"Action request #{int(row.get('id') or 0)}",
                "detail": str(row.get("prompt") or row.get("next_step") or "").strip()[:180],
                "created_at": row.get("created_at") or "",
            },
        )

    for row in handoff_rows or []:
        if not isinstance(row, dict):
            continue
        agent_key = _agent_key_for_business_room_row(row)
        workflow_key = str(row.get("workflow_key") or "").strip()
        _add(
            agent_key,
            {
                "kind": "workflow_handoff",
                "status": str(row.get("status") or "active").strip(),
                "title": f"Handoff #{int(row.get('id') or 0)}" + (f" -> {workflow_key}" if workflow_key else ""),
                "detail": str(row.get("prompt") or row.get("next_step") or "").strip()[:180],
                "created_at": row.get("updated_at") or row.get("created_at") or "",
            },
        )

    rows: list[dict[str, Any]] = []
    for agent_key in BUSINESS_CHAT_ROOM_AGENT_ORDER:
        row = summary.get(agent_key)
        if not row:
            continue
        activity = sorted(
            [item for item in row.get("activity", []) if isinstance(item, dict)],
            key=lambda item: (str(item.get("created_at") or ""), int(item.get("_sequence") or 0)),
            reverse=True,
        )[:max_items]
        cleaned = []
        for item in activity:
            next_item = dict(item)
            next_item.pop("_sequence", None)
            cleaned.append(next_item)
        next_row = dict(row)
        next_row["activity"] = cleaned
        next_row["activity_count"] = len(cleaned)
        rows.append(next_row)
    return rows


def build_business_room_agent_focus_summary(
    *,
    agent_key: str,
    workload_rows: list[dict[str, Any]],
    activity_rows: list[dict[str, Any]],
    customer_rollup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_key = resolve_business_agent_key(agent_key) or str(agent_key or "").strip().lower()
    roster_by_key = {
        str(row.get("key") or "").strip(): row
        for row in build_business_chat_room_roster()
        if str(row.get("key") or "").strip()
    }
    roster_row = roster_by_key.get(resolved_key, {})
    workload = next(
        (row for row in workload_rows or [] if str(row.get("agent_key") or "") == resolved_key),
        {},
    )
    activity = next(
        (row for row in activity_rows or [] if str(row.get("agent_key") or "") == resolved_key),
        {},
    )
    next_commands = (
        workload.get("next_answer_commands")
        if isinstance(workload.get("next_answer_commands"), list)
        else []
    )
    activity_items = activity.get("activity") if isinstance(activity.get("activity"), list) else []
    attention = str(workload.get("attention") or "idle").strip()
    latest_prompt = str(workload.get("latest_prompt") or "").strip()
    prompt_context = latest_prompt or "the current room workload"

    base_prompts_by_agent: dict[str, list[str]] = {
        "kurt_intake_agent": [
            "Kurt, review the latest intake handoff and list the missing product, lot, quantity, and cost-basis confirmations.",
            "Kurt, turn these attachments and notes into an intake draft with confidence and missing questions.",
        ],
        "murdock_listing_agent": [
            "Murdock, review the latest listing handoff and produce an eBay-ready title, HTML description direction, and publish blockers.",
            "Murdock, check category, condition, item specifics, media, price, and policy readiness for the latest listing draft.",
        ],
        "goldie_accountant_agent": [
            "Goldie, review current accounting exceptions and tell me which cost-basis issue to fix first.",
            "Goldie, summarize profit-basis risk, tax assumptions, and close-readiness blockers from the latest evidence.",
        ],
        "research_pricing_agent": [
            "Scout, run pricing research for the latest listing candidate and separate sold evidence from active/listed evidence.",
            "Scout, estimate list price, breakeven, and confidence for the latest product using comps and fee assumptions.",
        ],
        "business_monitor_agent": [
            "Atlas, summarize the current business-room workload and identify the next three operational priorities.",
            "Atlas, review sync, orders, listings, inventory, and accounting signals and route work to the right specialist.",
        ],
    }
    suggested_prompts = list(base_prompts_by_agent.get(resolved_key, []))
    customer_context = customer_rollup if isinstance(customer_rollup, dict) else {}
    if customer_context.get("available") and resolved_key in {"business_monitor_agent", "goldie_accountant_agent"}:
        repeat_count = int(customer_context.get("repeat_buyer_count") or 0)
        dormant_count = int(customer_context.get("dormant_90d_count") or 0)
        noted_count = int(customer_context.get("customers_with_internal_notes") or 0)
        if resolved_key == "business_monitor_agent" and (repeat_count or dormant_count):
            suggested_prompts.insert(
                0,
                "Atlas, review repeat-buyer and dormant-customer context and recommend customer follow-up priorities.",
            )
        elif resolved_key == "goldie_accountant_agent" and (repeat_count or noted_count):
            suggested_prompts.insert(
                0,
                "Goldie, review customer/repeat-buyer context for accounting or tax-sensitive follow-up risks, using note-presence only.",
            )
    if attention == "pending_approval":
        suggested_prompts.insert(0, f"{roster_row.get('name') or resolved_key}, review pending approvals for {prompt_context}.")
    elif attention == "needs_answers":
        suggested_prompts.insert(0, f"{roster_row.get('name') or resolved_key}, restate the missing confirmations for {prompt_context}.")
    elif attention == "failed_action":
        suggested_prompts.insert(0, f"{roster_row.get('name') or resolved_key}, explain the failed action and safest retry path for {prompt_context}.")
    elif attention == "active_handoff":
        suggested_prompts.insert(0, f"{roster_row.get('name') or resolved_key}, summarize the active handoff and what the operator should do next.")
    elif attention == "queued":
        suggested_prompts.insert(0, f"{roster_row.get('name') or resolved_key}, check queued work and identify anything that needs human attention.")

    return {
        "agent_key": resolved_key,
        "agent": str(roster_row.get("name") or workload.get("agent") or activity.get("agent") or resolved_key).strip(),
        "label": str(roster_row.get("label") or workload.get("label") or activity.get("label") or resolved_key).strip(),
        "role": str(roster_row.get("role") or "").strip(),
        "write_capable": bool(roster_row.get("write_capable")),
        "domains": list(roster_row.get("domains") or []),
        "attention": attention,
        "pending_action_count": int(workload.get("pending_action_count") or 0),
        "queued_action_count": int(workload.get("queued_action_count") or 0),
        "failed_action_count": int(workload.get("failed_action_count") or 0),
        "active_handoff_count": int(workload.get("active_handoff_count") or 0),
        "missing_question_count": int(workload.get("missing_question_count") or 0),
        "operator_answer_count": int(workload.get("operator_answer_count") or 0),
        "attachment_count": int(workload.get("attachment_count") or 0),
        "latest_prompt": latest_prompt,
        "latest_workflow": str(workload.get("latest_workflow") or "").strip(),
        "next_answer_commands": [str(item) for item in next_commands if str(item).strip()][:5],
        "suggested_prompts": [str(item) for item in suggested_prompts if str(item).strip()][:5],
        "activity": [dict(item) for item in activity_items if isinstance(item, dict)][:10],
    }


def build_business_room_agent_prompt_board(
    *,
    workload_rows: list[dict[str, Any]],
    activity_rows: list[dict[str, Any]],
    customer_rollup: dict[str, Any] | None = None,
    prompts_per_agent: int = 2,
) -> list[dict[str, Any]]:
    max_prompts = max(1, min(int(prompts_per_agent or 2), 5))
    rows: list[dict[str, Any]] = []
    for agent_key in BUSINESS_CHAT_ROOM_AGENT_ORDER:
        focus = build_business_room_agent_focus_summary(
            agent_key=agent_key,
            workload_rows=workload_rows,
            activity_rows=activity_rows,
            customer_rollup=customer_rollup,
        )
        prompts = [
            str(prompt)
            for prompt in focus.get("suggested_prompts", [])
            if str(prompt).strip()
        ][:max_prompts]
        rows.append(
            {
                "agent_key": focus.get("agent_key"),
                "agent": focus.get("agent"),
                "attention": focus.get("attention"),
                "prompts": prompts,
            }
        )
    return rows


def build_business_room_standup_brief(
    *,
    workload_rows: list[dict[str, Any]],
    prompt_board_rows: list[dict[str, Any]],
    coordination_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    totals = {
        "pending_approvals": 0,
        "queued": 0,
        "failed": 0,
        "active_handoffs": 0,
        "missing_questions": 0,
        "captured_answers": 0,
        "attachments": 0,
    }
    attention_order = {
        "failed_action": 0,
        "pending_approval": 1,
        "needs_answers": 2,
        "active_handoff": 3,
        "queued": 4,
        "idle": 5,
    }
    active_agents: list[dict[str, Any]] = []
    for row in workload_rows or []:
        if not isinstance(row, dict):
            continue
        totals["pending_approvals"] += int(row.get("pending_action_count") or 0)
        totals["queued"] += int(row.get("queued_action_count") or 0)
        totals["failed"] += int(row.get("failed_action_count") or 0)
        totals["active_handoffs"] += int(row.get("active_handoff_count") or 0)
        totals["missing_questions"] += int(row.get("missing_question_count") or 0)
        totals["captured_answers"] += int(row.get("operator_answer_count") or 0)
        totals["attachments"] += int(row.get("attachment_count") or 0)
        if str(row.get("attention") or "idle") != "idle":
            active_agents.append(
                {
                    "agent_key": row.get("agent_key"),
                    "agent": row.get("agent"),
                    "attention": row.get("attention"),
                    "latest_prompt": row.get("latest_prompt"),
                }
            )

    active_agents.sort(
        key=lambda row: (
            attention_order.get(str(row.get("attention") or "idle"), 99),
            str(row.get("agent") or ""),
        )
    )
    prompt_by_agent = {
        str(row.get("agent_key") or ""): row
        for row in (prompt_board_rows or [])
        if isinstance(row, dict) and str(row.get("agent_key") or "")
    }
    coordination_candidates = sorted(
        [row for row in (coordination_rows or []) if isinstance(row, dict) and str(row.get("prompt") or "").strip()],
        key=lambda row: (int(row.get("priority") or 99), str(row.get("target_agent") or "")),
    )
    next_prompt = ""
    recommended_kind = "agent_prompt"
    if coordination_candidates:
        next_prompt = str(coordination_candidates[0].get("prompt") or "").strip()
        recommended_kind = "coordination"
    if active_agents:
        first_agent_key = str(active_agents[0].get("agent_key") or "")
        prompts = prompt_by_agent.get(first_agent_key, {}).get("prompts", [])
        if not next_prompt and isinstance(prompts, list) and prompts:
            next_prompt = str(prompts[0])
    if not next_prompt:
        for row in prompt_board_rows or []:
            prompts = row.get("prompts") if isinstance(row, dict) else []
            if isinstance(prompts, list) and prompts:
                next_prompt = str(prompts[0])
                break

    if totals["failed"]:
        status = "failed_action"
    elif totals["pending_approvals"]:
        status = "pending_approval"
    elif totals["missing_questions"]:
        status = "needs_answers"
    elif totals["active_handoffs"]:
        status = "active_handoff"
    elif totals["queued"]:
        status = "queued"
    else:
        status = "idle"

    summary = (
        f"{len(active_agents)} active agent(s); "
        f"{totals['pending_approvals']} approval(s), "
        f"{totals['active_handoffs']} handoff(s), "
        f"{totals['missing_questions']} question(s), "
        f"{totals['failed']} failed action(s)."
    )
    return {
        "status": status,
        "summary": summary,
        "totals": totals,
        "active_agents": active_agents[:5],
        "recommended_prompt": next_prompt,
        "recommended_prompt_kind": recommended_kind if next_prompt else "",
        "coordination_count": len(coordination_candidates),
    }


def build_business_room_coordination_suggestions(
    *,
    workload_rows: list[dict[str, Any]],
    customer_rollup: dict[str, Any] | None = None,
    max_suggestions: int = 5,
) -> list[dict[str, Any]]:
    max_rows = max(1, min(int(max_suggestions or 5), 10))
    by_agent = {
        str(row.get("agent_key") or ""): row
        for row in workload_rows or []
        if isinstance(row, dict) and str(row.get("agent_key") or "")
    }

    def _row(agent_key: str) -> dict[str, Any]:
        return by_agent.get(agent_key, {})

    def _active(agent_key: str) -> bool:
        row = _row(agent_key)
        return bool(row) and str(row.get("attention") or "idle") != "idle"

    def _prompt_context(agent_key: str, fallback: str) -> str:
        return str(_row(agent_key).get("latest_prompt") or fallback).strip()

    suggestions: list[dict[str, Any]] = []

    def _add(
        *,
        source_agent_key: str,
        target_agent_key: str,
        reason: str,
        prompt: str,
        priority: int,
    ) -> None:
        source = get_business_agent(source_agent_key)
        target = get_business_agent(target_agent_key)
        key = (source_agent_key, target_agent_key, prompt)
        if any((row.get("source_agent_key"), row.get("target_agent_key"), row.get("prompt")) == key for row in suggestions):
            return
        suggestions.append(
            {
                "source_agent_key": source_agent_key,
                "source_agent": source.name if source else source_agent_key,
                "target_agent_key": target_agent_key,
                "target_agent": target.name if target else target_agent_key,
                "reason": reason,
                "prompt": prompt,
                "priority": int(priority),
            }
        )

    murdock = _row("murdock_listing_agent")
    if _active("murdock_listing_agent"):
        context = _prompt_context("murdock_listing_agent", "the active listing handoff")
        _add(
            source_agent_key="murdock_listing_agent",
            target_agent_key="research_pricing_agent",
            reason="Listing work should have pricing/comps evidence before price or margin is trusted.",
            prompt=f"Scout, comp {context} and return sold evidence, active-market evidence, breakeven, and confidence for Murdock.",
            priority=20 if int(murdock.get("missing_question_count") or 0) else 40,
        )

    kurt = _row("kurt_intake_agent")
    if _active("kurt_intake_agent"):
        context = _prompt_context("kurt_intake_agent", "the active intake handoff")
        if int(kurt.get("missing_question_count") or 0) or any(
            token in context.lower() for token in ("cost", "basis", "lot", "landed", "assignment")
        ):
            _add(
                source_agent_key="kurt_intake_agent",
                target_agent_key="goldie_accountant_agent",
                reason="Intake work has cost/lot evidence risk that should stay aligned with accounting close rules.",
                prompt=f"Goldie, review Kurt's intake context for {context} and identify cost-basis, lot-total, or assignment evidence questions before apply.",
                priority=15,
            )

    goldie = _row("goldie_accountant_agent")
    if _active("goldie_accountant_agent") and int(goldie.get("missing_question_count") or 0):
        _add(
            source_agent_key="goldie_accountant_agent",
            target_agent_key="kurt_intake_agent",
            reason="Accounting questions often need source, lot, or product intake evidence to resolve.",
            prompt="Kurt, review Goldie's open cost-basis questions and identify the source documents, lot assignments, or product fields needed to answer them.",
            priority=25,
        )

    if any(int(_row(agent_key).get("failed_action_count") or 0) for agent_key in by_agent):
        _add(
            source_agent_key="business_monitor_agent",
            target_agent_key="business_monitor_agent",
            reason="Failed agent-room actions should be triaged before more work is queued.",
            prompt="Atlas, review failed Business Chat Room actions and propose the safest retry or cancellation path.",
            priority=5,
        )
    elif any(int(_row(agent_key).get("pending_action_count") or 0) for agent_key in by_agent):
        _add(
            source_agent_key="business_monitor_agent",
            target_agent_key="business_monitor_agent",
            reason="Pending approvals are the next human bottleneck.",
            prompt="Atlas, summarize pending Business Chat Room approvals and recommend which one the operator should approve, cancel, or route first.",
            priority=30,
        )

    customer_context = customer_rollup if isinstance(customer_rollup, dict) else {}
    if customer_context.get("available"):
        repeat_count = int(customer_context.get("repeat_buyer_count") or 0)
        dormant_count = int(customer_context.get("dormant_90d_count") or 0)
        noted_count = int(customer_context.get("customers_with_internal_notes") or 0)
        if repeat_count or dormant_count:
            _add(
                source_agent_key="business_monitor_agent",
                target_agent_key="business_monitor_agent",
                reason="Customer rollups show repeat-buyer or dormant-buyer follow-up opportunities.",
                prompt=(
                    "Atlas, review customer context "
                    f"({repeat_count} repeat buyer(s), {dormant_count} dormant 90d+ customer(s)) "
                    "and recommend follow-up priorities for the operator."
                ),
                priority=35,
            )
        if repeat_count and noted_count:
            _add(
                source_agent_key="business_monitor_agent",
                target_agent_key="goldie_accountant_agent",
                reason="Repeat buyers with internal-note presence can affect follow-up, tax/accounting context, and close review boundaries.",
                prompt=(
                    "Goldie, review repeat-buyer context with internal-note presence using note flags only; "
                    "identify accounting, tax, or policy review risks before customer follow-up."
                ),
                priority=45,
            )

    suggestions.sort(key=lambda row: (int(row.get("priority") or 99), str(row.get("target_agent") or "")))
    return suggestions[:max_rows]


def mark_business_room_workflow_handoff_reviewed(
    repo: Any,
    *,
    environment: str,
    workflow_key: str,
    username: str,
    handoff: dict[str, Any],
    actor: str,
    source: str = "workflow",
) -> dict[str, Any]:
    resolved_workflow = str(workflow_key or handoff.get("workflow_key") or "").strip()
    scope_key = str(handoff.get("scope_key") or "").strip()
    if not resolved_workflow or not scope_key:
        raise ValueError("Business Chat Room handoff review requires workflow_key and scope_key.")
    payload = handoff.get("payload") if isinstance(handoff.get("payload"), dict) else {}
    draft_contract = payload.get("draft_contract") if isinstance(payload.get("draft_contract"), dict) else {}
    fields = draft_contract.get("fields") if isinstance(draft_contract.get("fields"), list) else []
    cleared = repo.clear_workflow_draft(
        environment=environment,
        workflow_key=resolved_workflow,
        username=username,
        scope_key=scope_key,
        actor=actor,
        reason=f"business_room_handoff_reviewed_from_{source}",
    )
    event = repo.append_workflow_event(
        environment=environment,
        workflow_key=resolved_workflow,
        username=username,
        scope_key=scope_key,
        action=f"review_business_room_handoff_from_{source}",
        status="ok" if cleared else "not_found",
        message="Operator marked Business Chat Room workflow handoff reviewed.",
        payload={
            "handoff_draft_id": int(handoff.get("id") or 0),
            "queue_job_id": int(handoff.get("queue_job_id") or 0),
            "source_message_id": int(handoff.get("source_message_id") or 0),
            "workflow_key": resolved_workflow,
            "route": str(handoff.get("route") or ""),
            "draft_signature": str(draft_contract.get("signature") or ""),
            "field_count": len(fields),
            "cleared": bool(cleared),
            "source": str(source or "workflow").strip(),
        },
        draft_id=int(handoff.get("id") or 0),
        actor=actor,
    )
    return {
        "cleared": bool(cleared),
        "workflow_key": resolved_workflow,
        "scope_key": scope_key,
        "event_id": int(getattr(event, "id", 0) or 0),
    }


def _first_quoted_text(value: str) -> str:
    match = re.search(r"[\"']([^\"']{4,160})[\"']", str(value or ""))
    return str(match.group(1)).strip() if match else ""


def _first_money_amount(value: str) -> str:
    match = re.search(r"(?:\$|cost(?:\s+is|\s*=|\s*:)?\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", str(value or ""), re.I)
    return str(match.group(1)).replace(",", "").strip() if match else ""


def _first_money_for_terms(value: str, terms: tuple[str, ...]) -> str:
    text = str(value or "")
    money = r"\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    for term in terms:
        term_pattern = re.escape(term).replace(r"\ ", r"\s+")
        patterns = [
            rf"\b{term_pattern}\b\s*(?:is|=|:|of|at|for)?\s*{money}",
            rf"{money}\s*(?:for|as|in)?\s*\b{term_pattern}\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                groups = [group for group in match.groups() if group]
                if groups:
                    return str(groups[-1]).replace(",", "").strip()
    return ""


def _first_quantity(value: str) -> int:
    match = re.search(r"(?:qty|quantity|x)\s*(?:=|:|is)?\s*([0-9]{1,5})\b", str(value or ""), re.I)
    if not match:
        match = re.search(r"\b([0-9]{1,5})\s*(?:pcs|pieces|coins|bars|items|units)\b", str(value or ""), re.I)
    try:
        return max(0, int(match.group(1))) if match else 0
    except Exception:
        return 0


def _first_entity_id(value: str, entity: str) -> int:
    pattern = rf"\b{re.escape(entity)}\s*(?:id|#|number|num)?\s*[:#]?\s*([0-9]{{1,10}})\b"
    match = re.search(pattern, str(value or ""), re.I)
    try:
        return max(0, int(match.group(1))) if match else 0
    except Exception:
        return 0


def _build_cost_basis_guardrail(field_values: dict[str, Any], prompt: str) -> dict[str, Any]:
    whole_lot_cost = _first_money_for_terms(
        prompt,
        (
            "whole lot landed cost",
            "whole lot cost",
            "lot landed total",
            "lot landed cost",
            "lot total",
            "lot cost",
        ),
    )
    product_unit_cost = _first_money_for_terms(
        prompt,
        (
            "product unit cost",
            "unit product cost",
            "unit cost",
            "per unit cost",
            "each cost",
            "product cost",
        ),
    )
    assignment_cost = _first_money_for_terms(
        prompt,
        (
            "assignment landed cost",
            "assignment cost",
            "allocated cost",
            "allocated unit cost",
        ),
    )
    if whole_lot_cost and not field_values.get("lot_landed_total"):
        field_values["lot_landed_total"] = whole_lot_cost
    if product_unit_cost and not field_values.get("product_unit_cost"):
        field_values["product_unit_cost"] = product_unit_cost
    if assignment_cost and not field_values.get("assignment_landed_cost"):
        field_values["assignment_landed_cost"] = assignment_cost

    explicit_keys = [
        key
        for key in ("product_unit_cost", "lot_landed_total", "assignment_landed_cost")
        if field_values.get(key) not in (None, "")
    ]
    acquisition_cost = str(field_values.get("acquisition_cost") or "").strip()
    ambiguous = bool(acquisition_cost and not explicit_keys)
    basis_type = "unknown"
    if explicit_keys:
        basis_type = "+".join(explicit_keys)
    elif ambiguous:
        basis_type = "ambiguous_acquisition_cost"
    return {
        "basis_type": basis_type,
        "explicit_cost_keys": explicit_keys,
        "ambiguous_acquisition_cost": ambiguous,
        "requires_confirmation": ambiguous or not (explicit_keys or acquisition_cost),
        "review_note": (
            "Confirm whether the cost is product unit cost, whole-lot landed cost, assignment cost, or unknown before applying."
            if ambiguous
            else "No cost evidence detected; intake should ask for product unit cost, whole-lot landed cost, or assignment cost."
            if not (explicit_keys or acquisition_cost)
            else "Cost evidence includes explicit basis context."
        ),
    }


def _build_listing_readiness_checks(field_values: dict[str, Any], handoff: dict[str, Any]) -> list[dict[str, Any]]:
    def _present(*keys: str) -> bool:
        return any(str(field_values.get(key) or "").strip() for key in keys)

    attachment_count = 0
    try:
        attachment_count = max(0, int(handoff.get("attachment_count") or 0))
    except Exception:
        attachment_count = 0
    checks = [
        {
            "key": "product_link",
            "label": "Product Link",
            "status": "ok" if _present("product_id", "listing_id") else "blocker",
            "message": (
                "Product or listing reference is present."
                if _present("product_id", "listing_id")
                else "Select a product or existing listing before applying Murdock suggestions."
            ),
        },
        {
            "key": "title",
            "label": "Title",
            "status": "ok" if _present("title", "suggested_title") else "blocker",
            "message": "Title evidence is present." if _present("title", "suggested_title") else "Generate or confirm an eBay-safe title.",
        },
        {
            "key": "description",
            "label": "Description",
            "status": "ok" if _present("description_html", "description", "listing_description", "suggested_details") else "blocker",
            "message": (
                "Description evidence is present."
                if _present("description_html", "description", "listing_description", "suggested_details")
                else "Generate/review buyer-facing description copy before publish."
            ),
        },
        {
            "key": "category",
            "label": "Category",
            "status": "ok" if _present("category_id", "ebay_category_id", "category") else "review",
            "message": (
                "Category evidence is present."
                if _present("category_id", "ebay_category_id", "category")
                else "Confirm eBay category so required specifics and condition policy can be checked."
            ),
        },
        {
            "key": "condition",
            "label": "Condition",
            "status": "ok" if _present("condition_id", "condition") else "review",
            "message": (
                "Condition evidence is present."
                if _present("condition_id", "condition")
                else "Confirm condition is valid for the selected eBay category."
            ),
        },
        {
            "key": "price",
            "label": "Price / Economics",
            "status": "ok" if _present("price", "suggested_price", "start_price", "buy_it_now_price") else "review",
            "message": (
                "Price evidence is present."
                if _present("price", "suggested_price", "start_price", "buy_it_now_price")
                else "Run comps/fee/breakeven review before listing."
            ),
        },
        {
            "key": "media",
            "label": "Media",
            "status": "ok" if attachment_count > 0 or _present("main_image_id", "main_image", "image_ids") else "review",
            "message": (
                "Media evidence is present."
                if attachment_count > 0 or _present("main_image_id", "main_image", "image_ids")
                else "Confirm main image and media readiness before publish."
            ),
        },
        {
            "key": "item_specifics",
            "label": "Item Specifics",
            "status": "ok" if _present("item_specifics", "aspects", "ebay_aspects") else "review",
            "message": (
                "Item-specific evidence is present."
                if _present("item_specifics", "aspects", "ebay_aspects")
                else "Fetch/review required eBay item specifics for the category."
            ),
        },
    ]
    return checks


def build_business_room_handoff_review_card(
    handoff: dict[str, Any],
    *,
    workflow_key: str = "",
) -> dict[str, Any]:
    payload = handoff.get("payload") if isinstance(handoff.get("payload"), dict) else {}
    prompt = str(payload.get("prompt") or handoff.get("prompt") or "").strip()
    normalized_workflow = str(workflow_key or handoff.get("workflow_key") or "").strip().lower()
    draft_contract = (
        payload.get("draft_contract")
        if isinstance(payload.get("draft_contract"), dict)
        else payload.get("ai_draft_contract") if isinstance(payload.get("ai_draft_contract"), dict) else {}
    )
    extracted = extract_ai_agent_draft_payload(draft_contract)
    field_values = dict(extracted.get("field_values") or {}) if extracted.get("is_contract") else {}
    prompt_title = _first_quoted_text(prompt)
    if prompt_title and not field_values.get("title"):
        field_values["title"] = prompt_title

    if normalized_workflow == "listing_wizard":
        product_id = _first_entity_id(prompt, "product")
        listing_id = _first_entity_id(prompt, "listing")
        if product_id and not field_values.get("product_id"):
            field_values["product_id"] = product_id
        if listing_id and not field_values.get("listing_id"):
            field_values["listing_id"] = listing_id
        listing_readiness_checks = _build_listing_readiness_checks(field_values, handoff)
        draft_type = "listing"
    elif normalized_workflow == "inventory_intake_wizard":
        quantity = _first_quantity(prompt)
        cost = _first_money_amount(prompt)
        if quantity and not field_values.get("quantity"):
            field_values["quantity"] = quantity
        if cost and not field_values.get("acquisition_cost"):
            field_values["acquisition_cost"] = cost
        cost_basis_guardrail = _build_cost_basis_guardrail(field_values, prompt)
        listing_readiness_checks = []
        draft_type = "intake"
    else:
        cost_basis_guardrail = {}
        listing_readiness_checks = []
        draft_type = str(extracted.get("draft_type") or "").strip() or "general"
    if normalized_workflow != "inventory_intake_wizard":
        cost_basis_guardrail = {}
    if normalized_workflow != "listing_wizard":
        listing_readiness_checks = []

    fields = [
        row
        for row in (extracted.get("fields") or [])
        if isinstance(row, dict) and str(row.get("key") or "").strip()
    ]
    existing_field_keys = {str(row.get("key") or "").strip() for row in fields if isinstance(row, dict)}
    prompt_hint_fields = [
            {
                "key": key,
                "label": str(key).replace("_", " ").title(),
                "value": value,
                "confidence": 0.55,
                "source": "business_room_prompt_hint",
            }
            for key, value in field_values.items()
            if value not in (None, "") and key not in existing_field_keys
    ]
    if fields:
        fields.extend(prompt_hint_fields)
    else:
        fields = prompt_hint_fields
    warnings = list(extracted.get("warnings") or [])
    if not warnings:
        warnings = ["Review room handoff hints before applying; prompt-derived fields are not authoritative."]
    if cost_basis_guardrail.get("ambiguous_acquisition_cost"):
        warnings.append(
            "Cost basis is ambiguous: confirm whether the amount is product unit cost, whole-lot landed cost, or assignment-level cost."
        )
    return {
        "workflow_key": normalized_workflow,
        "draft_type": draft_type,
        "agent_key": str(extracted.get("agent_key") or "").strip(),
        "signature": str(extracted.get("signature") or "").strip(),
        "prompt": prompt,
        "field_values": field_values,
        "fields": fields,
        "cost_basis_guardrail": cost_basis_guardrail,
        "listing_readiness_checks": listing_readiness_checks,
        "missing_questions": list(extracted.get("missing_questions") or []),
        "proposed_actions": list(extracted.get("proposed_actions") or []),
        "warnings": warnings,
        "source": "ai_agent_draft_contract" if extracted.get("is_contract") else "business_room_prompt_hint",
    }


def plan_business_room_agent_responses(
    *,
    message: str,
    directed_to: list[str] | None = None,
    max_agents: int = 3,
) -> list[str]:
    explicit_targets = [resolve_business_agent_key(item) or str(item or "").strip().lower() for item in (directed_to or [])]
    normalized_explicit = [item for item in dict.fromkeys(explicit_targets) if item in ALL_BUSINESS_ROOM_AGENT_KEYS]
    if normalized_explicit:
        return normalized_explicit[: max(1, min(int(max_agents or 3), len(ALL_BUSINESS_ROOM_AGENT_KEYS)))]

    text = f" {str(message or '').strip().lower()} "
    if any(token in text for token in (" everyone", " everybody", " all agents", " team ", " room ")):
        return list(ALL_BUSINESS_ROOM_AGENT_KEYS[: max(1, min(int(max_agents or 3), len(ALL_BUSINESS_ROOM_AGENT_KEYS)))])

    planned: list[str] = []
    mentioned = detect_business_agent_mentions(text)
    planned.extend(mentioned)
    keyword_routes: list[tuple[tuple[str, ...], str]] = [
        (("intake", "inventory", "purchase", "lot", "source", "photo", "picture", "invoice"), "kurt_intake_agent"),
        (("listing", "draft", "ebay", "auction", "title", "description", "specifics"), "murdock_listing_agent"),
        (("comp", "price", "pricing", "research", "market", "sold", "dealer"), "research_pricing_agent"),
        (("accounting", "cogs", "cost basis", "tax", "profit", "margin", "goldie"), "goldie_accountant_agent"),
        (("status", "priority", "backlog", "business", "sync", "health"), "business_monitor_agent"),
    ]
    for tokens, agent_key in keyword_routes:
        if any(token in text for token in tokens):
            planned.append(agent_key)
    if not planned:
        planned.append("business_monitor_agent")
    deduped = [item for item in dict.fromkeys(planned) if item in ALL_BUSINESS_ROOM_AGENT_KEYS]
    return deduped[: max(1, min(int(max_agents or 3), len(ALL_BUSINESS_ROOM_AGENT_KEYS)))]


def infer_business_room_reply_targets(
    *,
    reply_text: str,
    human_key: str = "",
    sender_agent_key: str = "",
) -> list[str]:
    targets: list[str] = []
    if str(human_key or "").strip():
        targets.append(str(human_key or "").strip().lower())
    sender_key = resolve_business_agent_key(sender_agent_key) or str(sender_agent_key or "").strip().lower()
    for agent_key in detect_business_agent_mentions(reply_text):
        if agent_key != sender_key:
            targets.append(agent_key)

    text = f" {str(reply_text or '').strip().lower()} "
    handoff_routes: list[tuple[tuple[str, ...], str]] = [
        (("scout", "comp", "pricing", "sold comparable", "market evidence"), "research_pricing_agent"),
        (("goldie", "accounting", "cost basis", "cogs", "tax"), "goldie_accountant_agent"),
        (("murdock", "listing", "description", "ebay draft"), "murdock_listing_agent"),
        (("kurt", "intake", "lot", "inventory", "source"), "kurt_intake_agent"),
        (("atlas", "business priority", "backlog", "status"), "business_monitor_agent"),
    ]
    for tokens, agent_key in handoff_routes:
        if agent_key == sender_key:
            continue
        if any(token in text for token in tokens):
            targets.append(agent_key)
    return list(dict.fromkeys(targets))


def plan_business_room_followup_agents(
    *,
    reply_text: str,
    sender_agent_key: str,
    already_responded: list[str] | None = None,
    max_agents: int = 5,
) -> list[str]:
    sender_key = resolve_business_agent_key(sender_agent_key) or str(sender_agent_key or "").strip().lower()
    responded = {
        resolve_business_agent_key(item) or str(item or "").strip().lower()
        for item in (already_responded or [])
        if str(item or "").strip()
    }
    responded.add(sender_key)
    target_keys = infer_business_room_reply_targets(
        reply_text=reply_text,
        human_key="",
        sender_agent_key=sender_key,
    )
    remaining_slots = max(0, min(int(max_agents or 5), len(ALL_BUSINESS_ROOM_AGENT_KEYS)) - len(responded))
    if remaining_slots <= 0:
        return []
    return [
        key
        for key in target_keys
        if key in ALL_BUSINESS_ROOM_AGENT_KEYS and key not in responded
    ][:remaining_slots]


def build_business_room_message_payload(
    *,
    room_key: str = DEFAULT_BUSINESS_ROOM_KEY,
    sender_type: str,
    sender_key: str,
    sender_label: str = "",
    message: str,
    thread_key: str = "",
    directed_to: list[str] | None = None,
    source: str = "app",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_sender_type = str(sender_type or "user").strip().lower()
    normalized_sender_key = str(sender_key or "").strip().lower() or normalized_sender_type
    agent = get_business_agent(normalized_sender_key)
    resolved_label = str(sender_label or "").strip()
    if not resolved_label and agent is not None:
        resolved_label = agent.label
    if not resolved_label:
        resolved_label = normalized_sender_key
    if directed_to is None:
        directed_agent_keys = detect_business_agent_mentions(message)
    else:
        directed_agent_keys = []
        for item in directed_to:
            resolved = resolve_business_agent_key(str(item or ""))
            normalized_item = resolved or str(item or "").strip().lower()
            normalized_item = "_".join(normalized_item.replace("-", "_").split())
            if normalized_item:
                directed_agent_keys.append(normalized_item)
    deduped_directed_to = list(dict.fromkeys(directed_agent_keys))
    return {
        "room_key": normalize_business_room_key(room_key),
        "thread_key": str(thread_key or "").strip(),
        "sender_type": normalized_sender_type,
        "sender_key": normalized_sender_key,
        "sender_label": resolved_label,
        "message": str(message or "").strip(),
        "directed_to": deduped_directed_to,
        "source": str(source or "app").strip().lower() or "app",
        "metadata": dict(metadata or {}),
        "created_at_utc": utcnow_naive().isoformat(),
    }


def record_business_room_message(
    repo: Any,
    *,
    room_key: str = DEFAULT_BUSINESS_ROOM_KEY,
    sender_type: str,
    sender_key: str,
    sender_label: str = "",
    message: str,
    thread_key: str = "",
    directed_to: list[str] | None = None,
    source: str = "app",
    metadata: dict[str, Any] | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    payload = build_business_room_message_payload(
        room_key=room_key,
        sender_type=sender_type,
        sender_key=sender_key,
        sender_label=sender_label,
        message=message,
        thread_key=thread_key,
        directed_to=directed_to,
        source=source,
        metadata=metadata,
    )
    if not payload["message"]:
        raise ValueError("Business Chat Room message is required.")
    row = repo.record_audit_event(
        entity_type=BUSINESS_CHAT_ENTITY_TYPE,
        entity_id=_room_entity_id(payload["room_key"]),
        action="message",
        actor=(actor or str(payload["sender_key"] or "system")).strip() or "system",
        changes={"after": payload},
    )
    payload["id"] = int(getattr(row, "id", 0) or 0)
    payload["created_at"] = getattr(row, "created_at", None)
    return payload


def record_business_room_turn(
    repo: Any,
    *,
    room_key: str = DEFAULT_BUSINESS_ROOM_KEY,
    user_key: str,
    user_label: str = "",
    agent_key: str,
    agent_label: str = "",
    user_message: str,
    agent_message: str,
    thread_key: str = "",
    source: str = "ask_goldenstackers",
    metadata: dict[str, Any] | None = None,
    actor: str = "system",
) -> list[dict[str, Any]]:
    resolved_thread_key = str(thread_key or "").strip()
    if not resolved_thread_key:
        seed = {
            "room_key": normalize_business_room_key(room_key),
            "user_key": str(user_key or "").strip().lower(),
            "agent_key": str(agent_key or "").strip().lower(),
            "user_message": str(user_message or "").strip()[:1000],
            "agent_message": str(agent_message or "").strip()[:1000],
        }
        resolved_thread_key = "ask:" + hashlib.sha256(
            json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
    base_metadata = dict(metadata or {})
    base_metadata["turn_thread_key"] = resolved_thread_key
    rows: list[dict[str, Any]] = []
    if str(user_message or "").strip():
        rows.append(
            record_business_room_message(
                repo,
                room_key=room_key,
                sender_type="user",
                sender_key=user_key,
                sender_label=user_label,
                message=str(user_message or "").strip(),
                thread_key=resolved_thread_key,
                directed_to=[agent_key] if agent_key else [],
                source=source,
                metadata={**base_metadata, "turn_role": "user"},
                actor=actor or user_key or "system",
            )
        )
    if str(agent_message or "").strip():
        rows.append(
            record_business_room_message(
                repo,
                room_key=room_key,
                sender_type="agent",
                sender_key=agent_key,
                sender_label=agent_label,
                message=str(agent_message or "").strip(),
                thread_key=resolved_thread_key,
                directed_to=[user_key] if user_key else [],
                source=source,
                metadata={**base_metadata, "turn_role": "agent"},
                actor=agent_key or actor or "system",
            )
        )
    return rows


def list_business_room_messages(
    repo: Any,
    *,
    room_key: str = DEFAULT_BUSINESS_ROOM_KEY,
    limit: int = 100,
    thread_key: str = "",
) -> list[dict[str, Any]]:
    normalized_room = normalize_business_room_key(room_key)
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.entity_type == BUSINESS_CHAT_ENTITY_TYPE,
            AuditLog.entity_id == _room_entity_id(normalized_room),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    rows = repo.db.scalars(stmt).all()
    output: list[dict[str, Any]] = []
    thread_filter = str(thread_key or "").strip()
    for row in rows:
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(str(row.changes_json or "{}"))
            if isinstance(parsed, dict):
                after = parsed.get("after")
                if isinstance(after, dict):
                    payload = after
        except Exception:
            payload = {}
        if not payload:
            continue
        if thread_filter and str(payload.get("thread_key") or "").strip() != thread_filter:
            continue
        payload["id"] = int(row.id)
        payload["created_at"] = row.created_at
        output.append(payload)
    return output


def _business_room_customer_rollup(repo: Any, *, limit: int = 5) -> dict[str, Any]:
    if not hasattr(repo, "list_customers"):
        return {
            "available": False,
            "customer_count": 0,
            "repeat_buyer_count": 0,
            "customers_with_internal_notes": 0,
            "dormant_90d_count": 0,
            "top_repeat_buyers": [],
            "top_dormant_customers": [],
        }
    try:
        customers = list(repo.list_customers())
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc)[:240],
            "customer_count": 0,
            "repeat_buyer_count": 0,
            "customers_with_internal_notes": 0,
            "dormant_90d_count": 0,
            "top_repeat_buyers": [],
            "top_dormant_customers": [],
        }

    now = utcnow_naive()

    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    def _days_since(value: Any) -> int | None:
        if value is None:
            return None
        try:
            delta = now - value
        except Exception:
            return None
        return max(0, int(delta.days))

    repeat_buyers = [row for row in customers if bool(getattr(row, "is_repeat_buyer", False))]
    noted_customers = [row for row in customers if str(getattr(row, "notes", "") or "").strip()]
    dormant_customers = [
        row
        for row in customers
        if (_days_since(getattr(row, "last_order_at", None)) or 0) >= 90
        and int(getattr(row, "order_count", 0) or 0) > 0
    ]
    top_repeat_buyers = sorted(
        repeat_buyers,
        key=lambda row: (_safe_float(getattr(row, "total_spend", 0)), int(getattr(row, "order_count", 0) or 0)),
        reverse=True,
    )[: max(1, min(int(limit or 5), 10))]
    top_dormant_customers = sorted(
        dormant_customers,
        key=lambda row: (
            _days_since(getattr(row, "last_order_at", None)) or 0,
            _safe_float(getattr(row, "total_spend", 0)),
        ),
        reverse=True,
    )[: max(1, min(int(limit or 5), 10))]

    def _customer_summary(row: Any) -> dict[str, Any]:
        return {
            "customer_id": int(getattr(row, "id", 0) or 0),
            "identity": (
                str(getattr(row, "ebay_username", "") or "").strip()
                or str(getattr(row, "display_name", "") or "").strip()
                or str(getattr(row, "primary_email", "") or "").strip()
                or f"customer#{int(getattr(row, 'id', 0) or 0)}"
            ),
            "order_count": int(getattr(row, "order_count", 0) or 0),
            "total_spend": round(_safe_float(getattr(row, "total_spend", 0)), 2),
            "has_internal_notes": bool(str(getattr(row, "notes", "") or "").strip()),
            "days_since_last_order": _days_since(getattr(row, "last_order_at", None)),
        }

    return {
        "available": True,
        "customer_count": len(customers),
        "repeat_buyer_count": len(repeat_buyers),
        "customers_with_internal_notes": len(noted_customers),
        "dormant_90d_count": len(dormant_customers),
        "top_repeat_buyers": [_customer_summary(row) for row in top_repeat_buyers],
        "top_dormant_customers": [_customer_summary(row) for row in top_dormant_customers],
    }


def build_business_room_context_snapshot(repo: Any, *, room_key: str = DEFAULT_BUSINESS_ROOM_KEY, limit: int = 25) -> dict[str, Any]:
    messages = list_business_room_messages(repo, room_key=room_key, limit=limit)
    return {
        "room_key": normalize_business_room_key(room_key),
        "roster": build_business_chat_room_roster(),
        "recent_messages": list(reversed(messages)),
        "message_count": len(messages),
        "customer_rollup": _business_room_customer_rollup(repo),
    }
