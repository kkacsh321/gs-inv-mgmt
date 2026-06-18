from __future__ import annotations

import hashlib
import json
from typing import Any

import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.views.shared import render_help_panel
from app.config import settings
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_multimodal_task
from app.services.business_agents import get_business_agent
from app.services.business_chat_room import (
    DEFAULT_BUSINESS_ROOM_KEY,
    build_business_room_attachment_evidence_rows,
    build_business_room_handoff_review_card,
    build_business_room_agent_answer_evidence,
    build_business_room_agent_activity_summary,
    build_business_room_agent_focus_summary,
    build_business_room_agent_prompt_board,
    build_business_room_answer_command_suggestions,
    build_business_room_agent_workload_summary,
    build_business_room_coordination_suggestions,
    build_business_room_operator_answer_rows,
    build_business_room_standup_brief,
    business_room_workflow_page_path,
    build_business_room_context_snapshot,
    classify_business_room_action_route,
    infer_business_room_attachment_kind,
    infer_business_room_reply_targets,
    is_business_room_write_intent,
    apply_business_room_agent_answer_to_latest_handoff,
    list_business_room_action_requests,
    list_business_room_active_workflow_handoffs,
    mark_business_room_workflow_handoff_reviewed,
    plan_business_room_followup_agents,
    plan_business_room_agent_responses,
    queue_business_room_action_request,
    record_business_room_message,
    transition_business_room_action_request,
)
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_bool, get_runtime_int
from app.utils.time import utcnow_naive


_DRAFT_TYPE_WORKFLOW_KEYS = {
    "intake": "inventory_intake_wizard",
    "listing": "listing_wizard",
}


def _business_room_review_field_rows(review_card: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(review_card, dict):
        return []
    rows: list[dict[str, Any]] = []
    for row in review_card.get("fields", []):
        if not isinstance(row, dict) or not str(row.get("key") or "").strip():
            continue
        rows.append(
            {
                "field": row.get("key"),
                "value": row.get("value"),
                "confidence": row.get("confidence"),
                "source": row.get("source"),
            }
        )
    return rows


def _safe_file_bytes(uploaded_file: Any) -> bytes:
    try:
        return uploaded_file.getvalue()
    except Exception:
        try:
            return uploaded_file.read()
        except Exception:
            return b""


def _attachment_preview(uploaded_file: Any, *, stored_ref: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    file_name = str(getattr(uploaded_file, "name", "") or "upload.bin").strip() or "upload.bin"
    content_type = str(getattr(uploaded_file, "type", "") or "application/octet-stream").strip()
    file_bytes = _safe_file_bytes(uploaded_file)
    return {
        "filename": file_name,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "sha256": hashlib.sha256(file_bytes).hexdigest() if file_bytes else "",
        "kind": infer_business_room_attachment_kind(content_type, file_name),
        "stored_ref": dict(stored_ref or {}),
        "error": str(error or "").strip(),
    }


def _store_business_room_uploads(
    repo: InventoryRepository,
    uploaded_files: list[Any],
    *,
    uploaded_by: str,
) -> tuple[list[dict[str, Any]], list[tuple[bytes, str]]]:
    if not uploaded_files:
        return [], []

    storage = MediaStorageService()
    image_inputs: list[tuple[bytes, str]] = []
    attachments: list[dict[str, Any]] = []
    if not storage.enabled:
        for uploaded_file in uploaded_files:
            file_bytes = _safe_file_bytes(uploaded_file)
            content_type = str(getattr(uploaded_file, "type", "") or "application/octet-stream").strip()
            if content_type.startswith("image/") and file_bytes:
                image_inputs.append((file_bytes, content_type))
            attachments.append(
                _attachment_preview(
                    uploaded_file,
                    error="Media storage is not configured; file was available to this chat turn but not persisted.",
                )
            )
        return attachments, image_inputs

    storage.ensure_bucket()
    for uploaded_file in uploaded_files:
        file_bytes = _safe_file_bytes(uploaded_file)
        file_name = str(getattr(uploaded_file, "name", "") or "upload.bin").strip() or "upload.bin"
        content_type = str(getattr(uploaded_file, "type", "") or "application/octet-stream").strip()
        kind = infer_business_room_attachment_kind(content_type, file_name)
        if kind == "image" and file_bytes:
            image_inputs.append((file_bytes, content_type))
        try:
            upload = storage.upload_file(
                file_name=file_name,
                file_bytes=file_bytes,
                content_type=content_type,
            )
            if kind in {"image", "video"}:
                media = repo.create_media_asset(
                    media_type=kind,
                    original_filename=file_name,
                    content_type=upload.content_type,
                    size_bytes=upload.size_bytes,
                    s3_bucket=upload.bucket,
                    s3_key=upload.key,
                    s3_url=upload.url,
                    uploaded_by=uploaded_by,
                )
                stored_ref = {"entity_type": "media_asset", "entity_id": int(getattr(media, "id", 0) or 0)}
            else:
                doc = repo.create_purchase_document(
                    document_kind="business_chat_attachment",
                    title=file_name,
                    original_filename=file_name,
                    content_type=upload.content_type,
                    size_bytes=upload.size_bytes,
                    content_sha256=hashlib.sha256(file_bytes).hexdigest(),
                    s3_bucket=upload.bucket,
                    s3_key=upload.key,
                    s3_url=upload.url,
                    uploaded_by=uploaded_by,
                    actor=uploaded_by,
                )
                stored_ref = {"entity_type": "purchase_document", "entity_id": int(getattr(doc, "id", 0) or 0)}
            attachments.append(_attachment_preview(uploaded_file, stored_ref=stored_ref))
        except Exception as exc:
            attachments.append(_attachment_preview(uploaded_file, error=str(exc)))
    return attachments, image_inputs


def _format_attachment_summary(attachments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in attachments:
        status = "stored" if item.get("stored_ref") else ("not persisted" if item.get("error") else "attached")
        lines.append(
            f"- {item.get('filename')} ({item.get('kind')}, {int(item.get('size_bytes') or 0)} bytes): {status}"
        )
    return "\n".join(lines)


def _business_room_pending_upload_rows(uploaded_files: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for uploaded_file in uploaded_files or []:
        filename = str(getattr(uploaded_file, "name", "") or "").strip()
        content_type = str(getattr(uploaded_file, "type", "") or "").strip()
        size_bytes = int(getattr(uploaded_file, "size", 0) or 0)
        rows.append(
            {
                "filename": filename or "(unnamed)",
                "kind": infer_business_room_attachment_kind(content_type, filename),
                "content_type": content_type,
                "size_bytes": size_bytes,
            }
        )
    return rows


def _render_business_room_attachment_evidence(
    payload: dict[str, Any],
    *,
    caption: str = "Attachment evidence",
) -> bool:
    attachment_rows = build_business_room_attachment_evidence_rows(payload)
    if not attachment_rows:
        return False
    st.caption(caption)
    st.dataframe(attachment_rows, use_container_width=True, hide_index=True)
    return True


def _business_room_customer_context_rows(customer_rollup: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(customer_rollup, dict) or not customer_rollup.get("available"):
        return []
    rows: list[dict[str, Any]] = []
    for kind, source_rows in (
        ("repeat_buyer", customer_rollup.get("top_repeat_buyers", [])),
        ("dormant", customer_rollup.get("top_dormant_customers", [])),
    ):
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "kind": kind,
                    "customer_id": int(row.get("customer_id") or 0),
                    "identity": str(row.get("identity") or "").strip(),
                    "orders": int(row.get("order_count") or 0),
                    "lifetime_spend": float(row.get("total_spend") or 0),
                    "has_internal_notes": bool(row.get("has_internal_notes")),
                    "days_since_last_order": row.get("days_since_last_order"),
                }
            )
    return rows


def _business_room_customer_context_prompts(customer_rollup: dict[str, Any]) -> list[str]:
    if not isinstance(customer_rollup, dict) or not customer_rollup.get("available"):
        return []
    repeat_count = int(customer_rollup.get("repeat_buyer_count") or 0)
    dormant_count = int(customer_rollup.get("dormant_90d_count") or 0)
    noted_count = int(customer_rollup.get("customers_with_internal_notes") or 0)
    prompts: list[str] = []
    if repeat_count or dormant_count:
        prompts.append(
            "Atlas, review repeat-buyer and dormant-customer context and recommend customer follow-up priorities."
        )
    if repeat_count and noted_count:
        prompts.append(
            "Goldie, review customer/repeat-buyer context for accounting or tax-sensitive follow-up risks, using note-presence only."
        )
    return prompts


def _business_room_prompt_action_rows(prompts: list[Any], *, limit: int = 3) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for prompt in prompts:
        text = str(prompt or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        label = text.replace("\n", " ")
        if len(label) > 80:
            label = label[:77].rstrip() + "..."
        rows.append({"label": label, "prompt": text})
        if len(rows) >= max(0, int(limit or 0)):
            break
    return rows


def _business_room_prepared_prompt_status(prompt: str) -> dict[str, Any]:
    text = str(prompt or "").strip()
    if not text:
        return {"has_prompt": False, "write_intent": False, "message": "No prepared prompt."}
    write_intent = is_business_room_write_intent(text)
    route = classify_business_room_action_route(message=text, directed_to=[]) if write_intent else {}
    if write_intent:
        route_label = str(route.get("label") or route.get("route_key") or "Action").strip()
        workflow = str(route.get("recommended_workflow") or "business_chat_room").strip()
        return {
            "has_prompt": True,
            "write_intent": True,
            "route": route,
            "status": "approval_required",
            "message": (
                "This prepared prompt looks like a write/action request. Sending it will create a "
                f"human-approval queue request before any workflow can apply changes. Route: {route_label} -> {workflow}."
            ),
        }
    return {
        "has_prompt": True,
        "write_intent": False,
        "route": {},
        "status": "read_only",
        "message": "This prepared prompt looks read-only and will be sent as a normal room message.",
    }


def _business_room_prepared_status_caption(status: dict[str, Any]) -> str:
    if not isinstance(status, dict):
        return ""
    status_key = str(status.get("status") or "").strip()
    if not status_key:
        return ""
    route = status.get("route") if isinstance(status.get("route"), dict) else {}
    if route:
        route_label = str(route.get("label") or route.get("route_key") or "Action").strip()
        workflow = str(route.get("recommended_workflow") or "business_chat_room").strip()
        return f"Prepared status: `{status_key}` | route `{route_label}` -> `{workflow}`"
    return f"Prepared status: `{status_key}`"


def _business_room_prepared_source_caption(source: dict[str, Any]) -> str:
    if not isinstance(source, dict):
        return ""
    source_label = str(source.get("source_label") or "").strip()
    if not source_label:
        return ""
    prompt_label = str(source.get("prompt_label") or "").strip()
    if prompt_label:
        return f"Prepared from: `{source_label}` | `{prompt_label}`"
    return f"Prepared from: `{source_label}`"


def _render_business_room_prompt_actions(
    prompts: list[Any],
    *,
    key_prefix: str,
    pending_prompt_key: str,
    pending_prompt_meta_key: str = "",
    source_label: str = "",
    limit: int = 3,
) -> None:
    action_rows = _business_room_prompt_action_rows(prompts, limit=limit)
    if not action_rows:
        return
    cols = st.columns(len(action_rows))
    for idx, row in enumerate(action_rows):
        if cols[idx].button(
            f"Use: {row['label']}",
            key=f"{key_prefix}::use_prompt::{idx}",
            help="Copy this suggestion into the prepared room prompt box for review before sending.",
        ):
            st.session_state[pending_prompt_key] = row["prompt"]
            if pending_prompt_meta_key:
                st.session_state[pending_prompt_meta_key] = {
                    "source_label": source_label or key_prefix,
                    "source_key": key_prefix,
                    "prompt_label": row["label"],
                }
            st.rerun()


def _render_draft_contract_card(
    *,
    message: dict[str, Any],
    metadata: dict[str, Any],
    pending_prompt_key: str = "",
    pending_prompt_meta_key: str = "",
    source_label: str = "",
    key_prefix: str = "business_chat_draft_card",
) -> None:
    draft_contract = metadata.get("draft_contract") if isinstance(metadata.get("draft_contract"), dict) else {}
    if not draft_contract:
        return
    draft_type = str(draft_contract.get("draft_type") or "").strip().lower()
    workflow_key = _DRAFT_TYPE_WORKFLOW_KEYS.get(draft_type, "")
    review_card = build_business_room_handoff_review_card(
        {
            "id": int(metadata.get("workflow_draft_id") or metadata.get("draft_id") or 0),
            "queue_job_id": int(metadata.get("queue_job_id") or 0),
            "workflow_key": workflow_key,
            "prompt": str(message.get("message") or ""),
            "payload": {
                "prompt": str(message.get("message") or ""),
                "draft_contract": draft_contract,
            },
        },
        workflow_key=workflow_key,
    )
    fields = [row for row in review_card.get("fields", []) if isinstance(row, dict)]
    st.caption(
        "AI draft contract"
        + (f" | `{draft_type}`" if draft_type else "")
        + (f" | signature `{review_card.get('signature')}`" if review_card.get("signature") else "")
    )
    if fields:
        st.dataframe(
            [
                {
                    "field": row.get("key"),
                    "value": row.get("value"),
                    "confidence": row.get("confidence"),
                    "source": row.get("source"),
                }
                for row in fields
            ],
            use_container_width=True,
            hide_index=True,
        )
    missing_questions = [row for row in review_card.get("missing_questions", []) if isinstance(row, dict)]
    if missing_questions:
        st.caption(
            "Missing confirmations: "
            + "; ".join(str(row.get("question") or row.get("field") or "") for row in missing_questions[:5])
        )
    answer_suggestions = build_business_room_answer_command_suggestions(
        {
            "id": int(metadata.get("workflow_draft_id") or metadata.get("draft_id") or 0),
            "queue_job_id": int(metadata.get("queue_job_id") or 0),
            "workflow_key": workflow_key,
            "prompt": str(message.get("message") or ""),
            "payload": {"draft_contract": draft_contract},
        },
        review_card=review_card,
        max_suggestions=8,
    )
    if answer_suggestions:
        st.caption("Reply with one of:")
        st.code("\n".join(answer_suggestions), language="text")
        if pending_prompt_key:
            _render_business_room_prompt_actions(
                answer_suggestions,
                key_prefix=f"{key_prefix}::answer_commands",
                pending_prompt_key=pending_prompt_key,
                pending_prompt_meta_key=pending_prompt_meta_key,
                source_label=source_label or "Draft Card Answer Commands",
                limit=3,
            )
    operator_answers = [
        row for row in draft_contract.get("operator_answers", []) if isinstance(row, dict)
    ]
    if operator_answers:
        st.dataframe(
            [
                {
                    "field": row.get("field"),
                    "answer": row.get("value"),
                    "source": row.get("source"),
                    "actor": row.get("actor"),
                }
                for row in operator_answers[:10]
            ],
            use_container_width=True,
            hide_index=True,
        )
    warnings = [str(item) for item in review_card.get("warnings", []) if str(item).strip()]
    if warnings:
        st.caption("Review notes: " + " | ".join(warnings[:3]))
    apply_plan = metadata.get("apply_plan") if isinstance(metadata.get("apply_plan"), dict) else {}
    if apply_plan:
        st.caption(
            "Apply plan: "
            f"`{apply_plan.get('status') or 'unknown'}`"
            + (f" ({apply_plan.get('reason')})" if apply_plan.get("reason") else "")
        )
    workflow_page = business_room_workflow_page_path(workflow_key)
    if workflow_page:
        st.page_link(workflow_page, label=f"Open {workflow_key}")


def _build_agent_instruction(
    *,
    agent_key: str,
    user_message: str,
    snapshot: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> tuple[str, str, str]:
    agent = get_business_agent(agent_key)
    label = str(agent.label if agent else agent_key)
    system_message = str(agent.system_message if agent else "You are a GoldenStackers business specialist.")
    recent_lines: list[str] = []
    for msg in (snapshot.get("recent_messages") or [])[-12:]:
        if not isinstance(msg, dict):
            continue
        recent_lines.append(
            f"{msg.get('sender_label') or msg.get('sender_key')}: {str(msg.get('message') or '')[:600]}"
        )
    customer_rollup = snapshot.get("customer_rollup") if isinstance(snapshot.get("customer_rollup"), dict) else {}
    customer_lines: list[str] = []
    if customer_rollup.get("available"):
        customer_lines.extend(
            [
                f"Customers: {int(customer_rollup.get('customer_count') or 0)}",
                f"Repeat buyers: {int(customer_rollup.get('repeat_buyer_count') or 0)}",
                f"Customers with internal notes: {int(customer_rollup.get('customers_with_internal_notes') or 0)}",
                f"Dormant 90d+ customers: {int(customer_rollup.get('dormant_90d_count') or 0)}",
            ]
        )
        top_repeat_buyers = [
            row for row in customer_rollup.get("top_repeat_buyers", []) if isinstance(row, dict)
        ]
        if top_repeat_buyers:
            customer_lines.append("Top repeat buyers:")
            for row in top_repeat_buyers[:5]:
                customer_lines.append(
                    "- "
                    f"{row.get('identity') or 'customer'}: "
                    f"{int(row.get('order_count') or 0)} order(s), "
                    f"${float(row.get('total_spend') or 0):.2f} lifetime, "
                    f"notes {'yes' if row.get('has_internal_notes') else 'no'}, "
                    f"last order {row.get('days_since_last_order')}d ago"
                )
        top_dormant_customers = [
            row for row in customer_rollup.get("top_dormant_customers", []) if isinstance(row, dict)
        ]
        if top_dormant_customers:
            customer_lines.append("Top dormant customers:")
            for row in top_dormant_customers[:5]:
                customer_lines.append(
                    "- "
                    f"{row.get('identity') or 'customer'}: "
                    f"{int(row.get('order_count') or 0)} order(s), "
                    f"${float(row.get('total_spend') or 0):.2f} lifetime, "
                    f"notes {'yes' if row.get('has_internal_notes') else 'no'}, "
                    f"last order {row.get('days_since_last_order')}d ago"
                )
        customer_lines.append("Internal customer note bodies are private operator records and are not included here.")
    elif customer_rollup.get("error"):
        customer_lines.append(f"Customer rollup unavailable: {customer_rollup.get('error')}")
    else:
        customer_lines.append("Customer rollup unavailable.")
    attachment_text = _format_attachment_summary(attachments) if attachments else "No files attached."
    instruction = (
        f"{agent.chat_instruction if agent else 'Respond concisely with business guidance.'}\n\n"
        "You are in the GoldenStackers Business Chat Room with the human operator and other agents. "
        "Only speak when you have relevant work, a question, a risk, or a handoff. Do not claim a write was "
        "performed. If a write is needed, ask for approval and name the app workflow that should apply it.\n\n"
        f"Human message:\n{user_message}\n\n"
        f"Attachments:\n{attachment_text}\n\n"
        "Customer context:\n" + "\n".join(customer_lines) + "\n\n"
        "Recent room context:\n" + ("\n".join(recent_lines) if recent_lines else "(none)")
    )
    return label, system_message, instruction


def _run_agent_reply(
    repo: InventoryRepository,
    *,
    agent_key: str,
    user_message: str,
    snapshot: dict[str, Any],
    attachments: list[dict[str, Any]],
    image_inputs: list[tuple[bytes, str]],
) -> tuple[str, dict[str, Any]]:
    label, system_message, instruction = _build_agent_instruction(
        agent_key=agent_key,
        user_message=user_message,
        snapshot=snapshot,
        attachments=attachments,
    )
    try:
        first_image = image_inputs[0][0] if image_inputs else None
        first_image_type = image_inputs[0][1] if image_inputs else "image/jpeg"
        result = execute_multimodal_task(
            repo,
            tool_name=f"business_chat_room_{agent_key}",
            system_message=system_message,
            instruction=instruction,
            image_bytes=first_image,
            image_content_type=first_image_type,
            additional_images=image_inputs[1:],
            max_output_tokens_override=900,
            context={"agent_key": agent_key, "room_key": DEFAULT_BUSINESS_ROOM_KEY},
            workflow="chat",
        )
        return str(result.text or "").strip(), {"agent_label": label, "ai_citation": result.citation}
    except Exception as exc:
        return (
            f"{label} could not generate an AI reply right now: `{str(exc)[:240]}`",
            {"agent_label": label, "ai_error": str(exc)[:500]},
        )


def render_business_chat_room(repo: InventoryRepository) -> None:
    user = current_user()
    st.title("Business Chat Room")
    render_help_panel(
        section_title="Business Chat Room",
        goal="Coordinate GoldenStackers work with humans, Goldy, and specialist agents in one live app-native chat.",
        steps=[
            "Post a message like `Kurt intake these photos`, `Murdock draft a listing`, or `Goldie review cost basis`.",
            "Attach images, videos, PDFs, CSVs, or notes when the agents need evidence.",
            "Answer agent questions with targeted replies like `kurt answer handoff 88 quantity: 20` or `murdock answer draft #321 condition_id: 3000`.",
            "Agents can suggest drafts, questions, and handoffs; writes remain approval-gated in the app workflows.",
        ],
        roadmap_phase="GS-V10-022 AI/Slack Wizard Simplification",
    )

    if not ensure_permission(user, "read", "Use Business Chat Room"):
        st.stop()
    if not ensure_permission(user, "ai_chat_use", "Use Business Chat Room AI"):
        st.stop()
    if not get_runtime_bool(repo, "business_chat_room_enabled", True):
        st.info("Business Chat Room is disabled by runtime setting.")
        return

    env_key = str(settings.app_env or "local").strip().lower()
    max_agent_replies = max(1, min(5, get_runtime_int(repo, "business_chat_room_max_agent_replies", 5)))
    ai_replies_enabled = get_runtime_bool(repo, "business_chat_room_ai_replies_enabled", True)
    pending_prompt_key = f"business_chat_pending_prompt::{env_key}::{user.username}"
    pending_prompt_meta_key = f"business_chat_pending_prompt_meta::{env_key}::{user.username}"

    with st.sidebar:
        st.caption("Business Chat Room")
        st.checkbox(
            "Agent replies",
            value=ai_replies_enabled,
            disabled=True,
            help="Controlled by business_chat_room_ai_replies_enabled.",
        )
        st.caption(f"Max agent replies per turn: `{max_agent_replies}`")

    snapshot = build_business_room_context_snapshot(
        repo,
        room_key=DEFAULT_BUSINESS_ROOM_KEY,
        limit=get_runtime_int(repo, "business_chat_room_recent_message_limit", 100),
    )
    roster = snapshot.get("roster") if isinstance(snapshot, dict) else []
    with st.expander("Room Roster", expanded=False):
        st.dataframe(roster, use_container_width=True, hide_index=True)

    customer_rollup = snapshot.get("customer_rollup") if isinstance(snapshot, dict) and isinstance(snapshot.get("customer_rollup"), dict) else {}
    with st.expander("Customer Context", expanded=False):
        if customer_rollup.get("available"):
            cols = st.columns(4)
            cols[0].metric("Customers", int(customer_rollup.get("customer_count") or 0))
            cols[1].metric("Repeat Buyers", int(customer_rollup.get("repeat_buyer_count") or 0))
            cols[2].metric("With Notes", int(customer_rollup.get("customers_with_internal_notes") or 0))
            cols[3].metric("Dormant 90d+", int(customer_rollup.get("dormant_90d_count") or 0))
            customer_rows = _business_room_customer_context_rows(customer_rollup)
            if customer_rows:
                st.caption("Top repeat and dormant customer summaries. Internal note bodies are not included in room agent context.")
                st.dataframe(customer_rows, use_container_width=True, hide_index=True)
            else:
                st.caption("No repeat-buyer rollups available.")
            st.page_link("pages/29_Customers.py", label="Open Customers")
            customer_prompts = _business_room_customer_context_prompts(customer_rollup)
            if customer_prompts:
                st.caption("Suggested room prompts")
                st.code("\n".join(customer_prompts), language="text")
                _render_business_room_prompt_actions(
                    customer_prompts,
                    key_prefix=f"business_chat_customer_prompts::{env_key}",
                    pending_prompt_key=pending_prompt_key,
                    pending_prompt_meta_key=pending_prompt_meta_key,
                    source_label="Customer Context",
                    limit=2,
                )
        elif customer_rollup.get("error"):
            st.warning(f"Customer context unavailable: {customer_rollup.get('error')}")
        else:
            st.caption("Customer context unavailable.")

    with st.expander("Pending Room Action Requests", expanded=False):
        try:
            action_rows = list_business_room_action_requests(
                repo,
                environment=env_key,
                statuses={"blocked", "queued", "failed"},
                limit=25,
            )
        except Exception as exc:
            action_rows = []
            st.warning(f"Could not load room action requests: {exc}")
        if action_rows:
            st.dataframe(action_rows, use_container_width=True, hide_index=True)
            action_options = {
                f"#{row['id']} | {row.get('route_label') or row.get('route') or 'action'} -> {row.get('workflow') or 'workflow'} | {str(row.get('prompt') or '')[:80]}": row
                for row in action_rows
            }
            selected_action_label = st.selectbox(
                "Selected request",
                options=list(action_options.keys()),
                key=f"business_chat_selected_request::{env_key}",
            )
            selected_action = action_options[selected_action_label]
            selected_request_id = int(selected_action.get("id") or 0)
            st.text_area(
                "Request Prompt",
                value=str(selected_action.get("prompt") or ""),
                height=100,
                disabled=True,
                key=f"business_chat_selected_prompt::{env_key}",
            )
            route_label = str(selected_action.get("route_label") or selected_action.get("route") or "Action").strip()
            workflow = str(selected_action.get("workflow") or "").strip()
            next_step = str(selected_action.get("next_step") or "").strip()
            st.caption(f"Route: `{route_label}` -> `{workflow or 'unknown'}`")
            if next_step:
                st.caption(f"Next step: {next_step}")
            workflow_page = str(selected_action.get("workflow_page") or business_room_workflow_page_path(workflow)).strip()
            if workflow_page:
                st.page_link(workflow_page, label=f"Open {workflow or route_label}")
            selected_payload = selected_action.get("payload") if isinstance(selected_action.get("payload"), dict) else {}
            if selected_payload.get("draft_contract"):
                _render_draft_contract_card(
                    message={"message": str(selected_action.get("prompt") or "")},
                    metadata={
                        "draft_contract": selected_payload.get("draft_contract"),
                        "apply_plan": selected_payload.get("apply_plan") if isinstance(selected_payload.get("apply_plan"), dict) else {},
                    },
                )
            a1, a2 = st.columns(2)
            with a1:
                if st.button("Approve Selected Request", key=f"business_chat_approve_request::{env_key}"):
                    if int(selected_request_id or 0) <= 0:
                        st.warning("Enter a request ID to approve.")
                    else:
                        try:
                            transition_business_room_action_request(
                                repo,
                                queue_job_id=int(selected_request_id),
                                transition="approve",
                                actor=user.username,
                            )
                            record_business_room_message(
                                repo,
                                room_key=DEFAULT_BUSINESS_ROOM_KEY,
                                sender_type="system",
                                sender_key="business_chat_room",
                                sender_label="Business Chat Room",
                                message=(
                                    f"Approved room action request `#{int(selected_request_id)}` for queued processing.\n\n"
                                    f"Route: `{route_label}` -> `{workflow or 'unknown'}`.\n"
                                    + (f"Next step: {next_step}\n" if next_step else "")
                                    + "Workflow-specific executors and normal app safeguards still apply."
                                ),
                                directed_to=[user.username],
                                source="business_chat_room",
                                metadata={
                                    "scope_env": env_key,
                                    "approved_queue_job_id": int(selected_request_id),
                                    "action_route": {
                                        "label": route_label,
                                        "recommended_workflow": workflow,
                                        "next_step": next_step,
                                    },
                                },
                                actor="business_chat_room",
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not approve request: {exc}")
            with a2:
                if st.button("Cancel Selected Request", key=f"business_chat_cancel_request::{env_key}"):
                    if int(selected_request_id or 0) <= 0:
                        st.warning("Enter a request ID to cancel.")
                    else:
                        try:
                            transition_business_room_action_request(
                                repo,
                                queue_job_id=int(selected_request_id),
                                transition="cancel",
                                actor=user.username,
                            )
                            record_business_room_message(
                                repo,
                                room_key=DEFAULT_BUSINESS_ROOM_KEY,
                                sender_type="system",
                                sender_key="business_chat_room",
                                sender_label="Business Chat Room",
                                message=f"Cancelled room action request `#{int(selected_request_id)}`.",
                                directed_to=[user.username],
                                source="business_chat_room",
                                metadata={"scope_env": env_key, "cancelled_queue_job_id": int(selected_request_id)},
                                actor="business_chat_room",
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not cancel request: {exc}")
        else:
            st.caption("No pending room action requests.")

    with st.expander("Active Room Workflow Handoffs", expanded=False):
        try:
            handoff_rows = list_business_room_active_workflow_handoffs(
                repo,
                environment=env_key,
                username=user.username,
                workflow_keys=["inventory_intake_wizard", "listing_wizard", "goldie", "tools_comp", "operations_home"],
                limit_per_workflow=10,
            )
        except Exception as exc:
            handoff_rows = []
            st.warning(f"Could not load workflow handoffs: {exc}")
        if handoff_rows:
            st.dataframe(
                [
                    {
                        "draft_id": row.get("id"),
                        "workflow": row.get("workflow_key"),
                        "queue_job_id": row.get("queue_job_id"),
                        "route": row.get("route_label") or row.get("route"),
                        "attachments": row.get("attachment_count"),
                        "prompt": str(row.get("prompt") or "")[:160],
                        "updated_at": row.get("updated_at"),
                    }
                    for row in handoff_rows
                ],
                use_container_width=True,
                hide_index=True,
            )
            workflow_counts: dict[str, int] = {}
            for row in handoff_rows:
                workflow_key = str(row.get("workflow_key") or "").strip()
                workflow_counts[workflow_key] = workflow_counts.get(workflow_key, 0) + 1
            st.caption(
                "Open handoffs by workflow: "
                + ", ".join(f"`{key}`={value}" for key, value in sorted(workflow_counts.items()) if key)
            )
            handoff_options = {
                f"#{row['id']} | {row.get('workflow_key')} | {(row.get('prompt') or 'No prompt')[:80]}": row
                for row in handoff_rows
            }
            selected_handoff_label = st.selectbox(
                "Selected handoff",
                options=list(handoff_options.keys()),
                key=f"business_chat_active_handoff_select::{env_key}",
            )
            selected_handoff = handoff_options[selected_handoff_label]
            review_card = build_business_room_handoff_review_card(
                selected_handoff,
                workflow_key=str(selected_handoff.get("workflow_key") or ""),
            )
            st.text_area(
                "Handoff Prompt",
                value=str(selected_handoff.get("prompt") or ""),
                height=100,
                disabled=True,
                key=f"business_chat_active_handoff_prompt::{env_key}",
            )
            selected_payload = (
                selected_handoff.get("payload")
                if isinstance(selected_handoff.get("payload"), dict)
                else {}
            )
            if selected_payload.get("draft_contract"):
                _render_draft_contract_card(
                    message={"message": str(selected_handoff.get("prompt") or "")},
                    metadata={
                        "draft_contract": selected_payload.get("draft_contract"),
                        "apply_plan": (
                            selected_payload.get("apply_plan")
                            if isinstance(selected_payload.get("apply_plan"), dict)
                            else {}
                        ),
                    },
                    pending_prompt_key=pending_prompt_key,
                    pending_prompt_meta_key=pending_prompt_meta_key,
                    source_label="Active Handoff Draft Card",
                    key_prefix=f"business_chat_active_handoff_draft::{env_key}::{selected_handoff.get('id')}",
                )
            operator_answer_rows = build_business_room_operator_answer_rows(selected_payload)
            if operator_answer_rows:
                st.caption("Captured operator answers")
                st.dataframe(
                    operator_answer_rows[:10],
                    use_container_width=True,
                    hide_index=True,
                )
            review_field_rows = _business_room_review_field_rows(review_card)
            if review_field_rows and not selected_payload.get("draft_contract"):
                st.caption("Review-card fields")
                st.dataframe(
                    review_field_rows,
                    use_container_width=True,
                    hide_index=True,
                )
            _render_business_room_attachment_evidence(selected_payload)
            answer_suggestions = build_business_room_answer_command_suggestions(
                selected_handoff,
                review_card=review_card,
                max_suggestions=8,
            )
            if answer_suggestions:
                st.caption("Targeted answer commands")
                st.code("\n".join(answer_suggestions), language="text")
                _render_business_room_prompt_actions(
                    answer_suggestions,
                    key_prefix=f"business_chat_handoff_answers::{env_key}::{selected_handoff.get('id')}",
                    pending_prompt_key=pending_prompt_key,
                    pending_prompt_meta_key=pending_prompt_meta_key,
                    source_label="Active Handoff Answer Commands",
                    limit=3,
                )
            cost_guardrail = (
                review_card.get("cost_basis_guardrail")
                if isinstance(review_card.get("cost_basis_guardrail"), dict)
                else {}
            )
            if cost_guardrail.get("review_note"):
                st.caption(
                    "Cost basis: "
                    f"`{cost_guardrail.get('basis_type') or 'unknown'}` - "
                    + str(cost_guardrail.get("review_note") or "")
                )
            readiness_checks = [
                row
                for row in review_card.get("listing_readiness_checks", [])
                if isinstance(row, dict) and str(row.get("label") or "").strip()
            ]
            if readiness_checks:
                st.dataframe(
                    [
                        {
                            "check": row.get("label"),
                            "status": row.get("status"),
                            "message": row.get("message"),
                        }
                        for row in readiness_checks
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            if selected_handoff.get("workflow_page"):
                st.page_link(
                    str(selected_handoff.get("workflow_page") or ""),
                    label=f"Open {selected_handoff.get('workflow_key')}",
                )
            if st.button("Mark Selected Handoff Reviewed", key=f"business_chat_review_handoff::{env_key}"):
                workflow_key = str(selected_handoff.get("workflow_key") or "").strip()
                scope_key = str(selected_handoff.get("scope_key") or "").strip()
                if not workflow_key or not scope_key:
                    st.warning("Selected handoff is missing workflow or scope metadata.")
                else:
                    try:
                        review_result = mark_business_room_workflow_handoff_reviewed(
                            repo,
                            environment=env_key,
                            workflow_key=workflow_key,
                            username=user.username,
                            actor=user.username,
                            source="room",
                            handoff=selected_handoff,
                        )
                        record_business_room_message(
                            repo,
                            room_key=DEFAULT_BUSINESS_ROOM_KEY,
                            sender_type="system",
                            sender_key="business_chat_room",
                            sender_label="Business Chat Room",
                            message=(
                                f"Marked workflow handoff `#{int(selected_handoff.get('id') or 0)}` reviewed "
                                f"for `{workflow_key}` from the room."
                            ),
                            directed_to=[user.username],
                            source="business_chat_room",
                            metadata={
                                "scope_env": env_key,
                                "reviewed_handoff_draft_id": int(selected_handoff.get("id") or 0),
                                "queue_job_id": int(selected_handoff.get("queue_job_id") or 0),
                                "workflow_key": workflow_key,
                                "cleared": bool(review_result.get("cleared")),
                                "workflow_event_id": int(review_result.get("event_id") or 0),
                            },
                            actor="business_chat_room",
                        )
                        st.success("Marked workflow handoff reviewed.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not mark handoff reviewed: {exc}")
        else:
            st.caption("No active room workflow handoffs for this user.")

    with st.expander("Agent Workload", expanded=False):
        agent_workload_rows = build_business_room_agent_workload_summary(
            action_rows=action_rows,
            handoff_rows=handoff_rows,
        )
        st.dataframe(
            [
                {
                    "agent": row.get("agent"),
                    "attention": row.get("attention"),
                    "pending_approvals": row.get("pending_action_count"),
                    "queued": row.get("queued_action_count"),
                    "failed": row.get("failed_action_count"),
                    "handoffs": row.get("active_handoff_count"),
                    "missing_questions": row.get("missing_question_count"),
                    "answers": row.get("operator_answer_count"),
                    "attachments": row.get("attachment_count"),
                    "workflow": row.get("latest_workflow"),
                    "latest_prompt": row.get("latest_prompt"),
                }
                for row in agent_workload_rows
            ],
            use_container_width=True,
            hide_index=True,
        )
        attention_rows = [row for row in agent_workload_rows if row.get("attention") != "idle"]
        if attention_rows:
            st.caption(
                "Needs attention: "
                + ", ".join(f"`{row.get('agent')}`={row.get('attention')}" for row in attention_rows)
            )
        else:
            st.caption("No agent handoffs or approvals currently need attention.")
        answer_command_rows = [
            row
            for row in agent_workload_rows
            if isinstance(row.get("next_answer_commands"), list) and row.get("next_answer_commands")
        ]
        if answer_command_rows:
            st.caption("Next answer commands")
            for row in answer_command_rows:
                st.markdown(f"**{row.get('agent') or row.get('agent_key')}**")
                commands = [str(command) for command in row.get("next_answer_commands", [])[:5]]
                st.code("\n".join(commands), language="text")
                _render_business_room_prompt_actions(
                    commands,
                    key_prefix=f"business_chat_workload_answers::{env_key}::{row.get('agent_key') or row.get('agent')}",
                    pending_prompt_key=pending_prompt_key,
                    pending_prompt_meta_key=pending_prompt_meta_key,
                    source_label="Agent Workload Answer Commands",
                    limit=3,
                )

    recent_messages = snapshot.get("recent_messages") if isinstance(snapshot, dict) else []
    with st.expander("Agent Activity", expanded=False):
        agent_activity_rows = build_business_room_agent_activity_summary(
            recent_messages=recent_messages if isinstance(recent_messages, list) else [],
            action_rows=action_rows,
            handoff_rows=handoff_rows,
            max_items_per_agent=5,
        )
        for row in agent_activity_rows:
            activity = row.get("activity") if isinstance(row.get("activity"), list) else []
            if not activity:
                continue
            st.markdown(f"**{row.get('agent') or row.get('agent_key')}**")
            st.dataframe(
                [
                    {
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "title": item.get("title"),
                        "detail": item.get("detail"),
                    }
                    for item in activity
                    if isinstance(item, dict)
                ],
                use_container_width=True,
                hide_index=True,
            )
        if not any(row.get("activity_count") for row in agent_activity_rows):
            st.caption("No recent agent activity yet.")
    with st.expander("Room Prompt Board", expanded=False):
        prompt_board_rows = build_business_room_agent_prompt_board(
            workload_rows=agent_workload_rows,
            activity_rows=agent_activity_rows,
            customer_rollup=customer_rollup,
            prompts_per_agent=2,
        )
        for row in prompt_board_rows:
            prompts = row.get("prompts") if isinstance(row.get("prompts"), list) else []
            if not prompts:
                continue
            st.markdown(f"**{row.get('agent') or row.get('agent_key')}** `{row.get('attention') or 'idle'}`")
            st.code("\n".join(str(prompt) for prompt in prompts), language="text")
            _render_business_room_prompt_actions(
                prompts,
                key_prefix=f"business_chat_prompt_board::{env_key}::{row.get('agent_key') or row.get('agent')}",
                pending_prompt_key=pending_prompt_key,
                pending_prompt_meta_key=pending_prompt_meta_key,
                source_label="Room Prompt Board",
                limit=2,
            )
    coordination_rows = build_business_room_coordination_suggestions(
        workload_rows=agent_workload_rows,
        customer_rollup=customer_rollup,
        max_suggestions=5,
    )
    with st.expander("Room Standup", expanded=False):
        standup_brief = build_business_room_standup_brief(
            workload_rows=agent_workload_rows,
            prompt_board_rows=prompt_board_rows,
            coordination_rows=coordination_rows,
        )
        st.caption(f"Status: `{standup_brief.get('status') or 'idle'}`")
        st.write(str(standup_brief.get("summary") or ""))
        totals = standup_brief.get("totals") if isinstance(standup_brief.get("totals"), dict) else {}
        cols = st.columns(5)
        cols[0].metric("Approvals", int(totals.get("pending_approvals") or 0))
        cols[1].metric("Handoffs", int(totals.get("active_handoffs") or 0))
        cols[2].metric("Questions", int(totals.get("missing_questions") or 0))
        cols[3].metric("Queued", int(totals.get("queued") or 0))
        cols[4].metric("Failed", int(totals.get("failed") or 0))
        active_agents = (
            standup_brief.get("active_agents")
            if isinstance(standup_brief.get("active_agents"), list)
            else []
        )
        if active_agents:
            st.caption("Active agents")
            st.dataframe(
                [
                    {
                        "agent": row.get("agent"),
                        "attention": row.get("attention"),
                        "latest_prompt": row.get("latest_prompt"),
                    }
                    for row in active_agents
                    if isinstance(row, dict)
                ],
                use_container_width=True,
                hide_index=True,
            )
        if standup_brief.get("recommended_prompt"):
            kind = str(standup_brief.get("recommended_prompt_kind") or "agent_prompt")
            st.caption(f"Recommended next room prompt ({kind})")
            st.code(str(standup_brief.get("recommended_prompt") or ""), language="text")
            _render_business_room_prompt_actions(
                [standup_brief.get("recommended_prompt")],
                key_prefix=f"business_chat_standup::{env_key}",
                pending_prompt_key=pending_prompt_key,
                pending_prompt_meta_key=pending_prompt_meta_key,
                source_label="Room Standup",
                limit=1,
            )
    with st.expander("Agent Coordination Suggestions", expanded=False):
        if coordination_rows:
            for row in coordination_rows:
                st.markdown(
                    f"**{row.get('source_agent')} -> {row.get('target_agent')}** "
                    f"`priority {int(row.get('priority') or 0)}`"
                )
                st.caption(str(row.get("reason") or ""))
                st.code(str(row.get("prompt") or ""), language="text")
                _render_business_room_prompt_actions(
                    [row.get("prompt")],
                    key_prefix=(
                        f"business_chat_coordination::{env_key}::"
                        f"{row.get('source_agent')}::{row.get('target_agent')}"
                    ),
                    pending_prompt_key=pending_prompt_key,
                    pending_prompt_meta_key=pending_prompt_meta_key,
                    source_label="Agent Coordination",
                    limit=1,
                )
        else:
            st.caption("No cross-agent coordination suggestions right now.")
    with st.expander("Agent Focus", expanded=False):
        focus_options = [
            f"{row.get('agent') or row.get('agent_key')} - {row.get('attention') or 'idle'}"
            for row in agent_workload_rows
        ]
        focus_key_by_label = {
            label: str(row.get("agent_key") or "")
            for label, row in zip(focus_options, agent_workload_rows)
        }
        selected_focus_label = st.selectbox(
            "Focus Agent",
            focus_options,
            key=f"business_chat_focus_agent::{env_key}",
        )
        focus_summary = build_business_room_agent_focus_summary(
            agent_key=focus_key_by_label.get(selected_focus_label, ""),
            workload_rows=agent_workload_rows,
            activity_rows=agent_activity_rows,
            customer_rollup=customer_rollup,
        )
        st.caption(
            f"{focus_summary.get('label')} | role={focus_summary.get('role') or 'n/a'} | "
            f"attention={focus_summary.get('attention')}"
        )
        cols = st.columns(6)
        cols[0].metric("Approvals", int(focus_summary.get("pending_action_count") or 0))
        cols[1].metric("Queued", int(focus_summary.get("queued_action_count") or 0))
        cols[2].metric("Failed", int(focus_summary.get("failed_action_count") or 0))
        cols[3].metric("Handoffs", int(focus_summary.get("active_handoff_count") or 0))
        cols[4].metric("Questions", int(focus_summary.get("missing_question_count") or 0))
        cols[5].metric("Answers", int(focus_summary.get("operator_answer_count") or 0))
        if focus_summary.get("latest_prompt"):
            st.caption("Latest prompt")
            st.write(str(focus_summary.get("latest_prompt") or ""))
        if focus_summary.get("next_answer_commands"):
            st.caption("Next answer commands")
            focus_answer_commands = [str(command) for command in focus_summary.get("next_answer_commands", [])]
            st.code("\n".join(focus_answer_commands), language="text")
            _render_business_room_prompt_actions(
                focus_answer_commands,
                key_prefix=f"business_chat_focus_answers::{env_key}::{focus_summary.get('agent_key') or 'agent'}",
                pending_prompt_key=pending_prompt_key,
                pending_prompt_meta_key=pending_prompt_meta_key,
                source_label="Agent Focus Answer Commands",
                limit=3,
            )
        if focus_summary.get("suggested_prompts"):
            st.caption("Suggested room prompts")
            st.code("\n".join(focus_summary.get("suggested_prompts", [])), language="text")
            _render_business_room_prompt_actions(
                focus_summary.get("suggested_prompts", []),
                key_prefix=f"business_chat_focus_prompts::{env_key}::{focus_summary.get('agent_key') or 'agent'}",
                pending_prompt_key=pending_prompt_key,
                pending_prompt_meta_key=pending_prompt_meta_key,
                source_label="Agent Focus",
                limit=3,
            )
        focus_activity = focus_summary.get("activity") if isinstance(focus_summary.get("activity"), list) else []
        if focus_activity:
            st.caption("Recent activity")
            st.dataframe(
                [
                    {
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "title": item.get("title"),
                        "detail": item.get("detail"),
                    }
                    for item in focus_activity
                    if isinstance(item, dict)
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No recent activity for this agent.")
    for msg in recent_messages[-60:]:
        if not isinstance(msg, dict):
            continue
        sender_type = str(msg.get("sender_type") or "agent").strip().lower()
        chat_role = "user" if sender_type == "user" else "assistant"
        with st.chat_message(chat_role):
            st.markdown(f"**{msg.get('sender_label') or msg.get('sender_key') or 'unknown'}**")
            directed_to = [str(item) for item in (msg.get("directed_to") or []) if str(item).strip()]
            if directed_to:
                st.caption("To: `" + "`, `".join(directed_to) + "`")
            st.markdown(str(msg.get("message") or ""))
            metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            prepared_source = (
                metadata.get("prepared_prompt_source")
                if isinstance(metadata.get("prepared_prompt_source"), dict)
                else {}
            )
            prepared_source_caption = _business_room_prepared_source_caption(prepared_source)
            if metadata.get("prepared_prompt") and prepared_source_caption:
                st.caption(prepared_source_caption)
            prepared_status = (
                metadata.get("prepared_prompt_status")
                if isinstance(metadata.get("prepared_prompt_status"), dict)
                else {}
            )
            prepared_status_caption = _business_room_prepared_status_caption(prepared_status)
            if metadata.get("prepared_prompt") and prepared_status_caption:
                st.caption(prepared_status_caption)
            attachments = metadata.get("attachments") if isinstance(metadata, dict) else []
            if attachments:
                rendered_attachments = _render_business_room_attachment_evidence(
                    {"attachments": attachments},
                    caption="Attachments",
                )
                if not rendered_attachments:
                    st.code(json.dumps(attachments, indent=2, default=str), language="json")
            _render_draft_contract_card(
                message=msg,
                metadata=metadata,
                pending_prompt_key=pending_prompt_key,
                pending_prompt_meta_key=pending_prompt_meta_key,
                source_label="Message Draft Card",
                key_prefix=f"business_chat_message_draft::{env_key}::{msg.get('id') or 'message'}",
            )
            action_route = metadata.get("action_route") if isinstance(metadata, dict) else {}
            if isinstance(action_route, dict) and action_route.get("recommended_workflow"):
                st.caption(
                    "Action route: "
                    f"`{action_route.get('label') or action_route.get('route_key')}` -> "
                    f"`{action_route.get('recommended_workflow')}`"
                )

    uploaded_files = st.file_uploader(
        "Attach evidence for the next message",
        accept_multiple_files=True,
        key=f"business_chat_uploads::{env_key}::{user.username}",
        help="Images can be sent to multimodal agents; videos/PDFs/documents are stored as evidence metadata.",
    )
    pending_upload_rows = _business_room_pending_upload_rows(list(uploaded_files or []))

    prepared_prompt = ""
    prepared_prompt_meta: dict[str, Any] = {}
    pending_prompt = str(st.session_state.get(pending_prompt_key) or "").strip()
    pending_prompt_meta = (
        st.session_state.get(pending_prompt_meta_key)
        if isinstance(st.session_state.get(pending_prompt_meta_key), dict)
        else {}
    )
    if pending_prompt:
        st.divider()
        st.caption("Prepared room prompt")
        if pending_prompt_meta.get("source_label"):
            st.caption(
                "Source: "
                f"`{pending_prompt_meta.get('source_label')}`"
                + (
                    f" | `{pending_prompt_meta.get('prompt_label')}`"
                    if pending_prompt_meta.get("prompt_label")
                    else ""
                )
            )
        prepared_prompt = st.text_area(
            "Review or edit before sending",
            value=pending_prompt,
            key=(
                f"business_chat_prepared_prompt::{env_key}::{user.username}::"
                f"{hashlib.sha1(pending_prompt.encode('utf-8')).hexdigest()[:10]}"
            ),
            height=100,
        )
        prepared_status = _business_room_prepared_prompt_status(prepared_prompt)
        if prepared_status.get("write_intent"):
            st.warning(str(prepared_status.get("message") or "This will require approval before changes can apply."))
        else:
            st.info(str(prepared_status.get("message") or "This will be sent as a normal room message."))
        if pending_upload_rows:
            st.caption("Attachments that will be sent with this prepared prompt")
            st.dataframe(pending_upload_rows, use_container_width=True, hide_index=True)
        else:
            st.caption("No attachments selected for this prepared prompt.")
        prompt_cols = st.columns([1, 1, 4])
        send_prepared = prompt_cols[0].button(
            "Send prepared prompt",
            key=f"business_chat_send_prepared::{env_key}::{user.username}",
        )
        clear_prepared = prompt_cols[1].button(
            "Clear",
            key=f"business_chat_clear_prepared::{env_key}::{user.username}",
        )
        if clear_prepared:
            st.session_state[pending_prompt_key] = ""
            st.session_state[pending_prompt_meta_key] = {}
            st.rerun()
        if send_prepared:
            prepared_prompt_meta = dict(pending_prompt_meta)
        else:
            prepared_prompt = ""
    prompt = prepared_prompt.strip() or (st.chat_input("Message the Business Chat Room...") or "").strip()
    if not prompt:
        return
    prepared_prompt_status = _business_room_prepared_prompt_status(prompt) if prepared_prompt else {}
    if prepared_prompt:
        st.session_state[pending_prompt_key] = ""
        st.session_state[pending_prompt_meta_key] = {}

    attachments, image_inputs = _store_business_room_uploads(
        repo,
        list(uploaded_files or []),
        uploaded_by=user.username,
    )
    answer_evidence = build_business_room_agent_answer_evidence(
        message=prompt,
        actor=user.username,
        source="business_chat_room",
    )
    write_intent = is_business_room_write_intent(prompt)
    initial_action_route = (
        classify_business_room_action_route(message=prompt, directed_to=[]) if write_intent else {}
    )
    human_msg = record_business_room_message(
        repo,
        room_key=DEFAULT_BUSINESS_ROOM_KEY,
        sender_type="user",
        sender_key=user.username,
        sender_label=user.username,
        message=prompt,
        source="business_chat_room",
        metadata={
            "scope_env": env_key,
            "user_role": user.role,
            "attachments": attachments,
            "attachment_count": len(attachments),
            "write_intent": write_intent,
            "ai_agent_answer": answer_evidence,
            "action_route": initial_action_route,
            "prepared_prompt": bool(prepared_prompt),
            "prepared_prompt_source": prepared_prompt_meta,
            "prepared_prompt_status": prepared_prompt_status,
            "created_at_utc": utcnow_naive().isoformat(),
        },
        actor=user.username,
    )
    if answer_evidence:
        answer_apply_result = apply_business_room_agent_answer_to_latest_handoff(
            repo,
            environment=env_key,
            username=user.username,
            answer_evidence=answer_evidence,
            actor=user.username,
        )
        record_business_room_message(
            repo,
            room_key=DEFAULT_BUSINESS_ROOM_KEY,
            sender_type="system",
            sender_key="business_chat_room",
            sender_label="Business Chat Room",
            message=(
                "Captured agent answer evidence. "
                f"Agent: `{answer_evidence.get('agent_label') or answer_evidence.get('agent') or 'agent'}`. "
                f"Field: `{answer_evidence.get('field')}`. "
                + (
                    f"Updated active `{answer_apply_result.get('workflow_key')}` handoff draft evidence. "
                    if answer_apply_result.get("applied")
                    else "No active matching handoff draft was updated. "
                )
                + "No product, listing, inventory, accounting, or integration write was executed."
            ),
            thread_key=str(human_msg.get("thread_key") or human_msg.get("id") or ""),
            directed_to=[user.username],
            source="business_chat_room",
            metadata={
                "scope_env": env_key,
                "responding_to_message_id": int(human_msg.get("id") or 0),
                "ai_agent_answer": answer_evidence,
                "answer_apply_result": answer_apply_result,
                "write_executed": False,
            },
            actor="business_chat_room",
        )
        st.rerun()

    agent_keys = plan_business_room_agent_responses(
        message=prompt,
        directed_to=human_msg.get("directed_to") if isinstance(human_msg, dict) else [],
        max_agents=max_agent_replies,
    )
    approval_result: dict[str, Any] = {}
    approval_error = ""
    if (
        write_intent
        and get_runtime_bool(repo, "business_chat_room_write_actions_require_approval", True)
    ):
        try:
            approval_result = queue_business_room_action_request(
                repo,
                room_key=DEFAULT_BUSINESS_ROOM_KEY,
                message=prompt,
                actor=user.username,
                user_role=user.role,
                directed_to=human_msg.get("directed_to") if isinstance(human_msg, dict) else [],
                attachments=attachments,
                source_message_id=int(human_msg.get("id") or 0),
                environment=env_key,
            )
            route = (
                approval_result.get("action_route")
                if isinstance(approval_result.get("action_route"), dict)
                else initial_action_route
            )
            route_label = str(route.get("label") or route.get("route_key") or "Action").strip()
            route_workflow = str(route.get("recommended_workflow") or "business_chat_room").strip()
            route_next_step = str(route.get("next_step") or "").strip()
            record_business_room_message(
                repo,
                room_key=DEFAULT_BUSINESS_ROOM_KEY,
                sender_type="system",
                sender_key="business_chat_room",
                sender_label="Business Chat Room",
                message=(
                    "Write/action request captured for human approval: "
                    f"`#{approval_result.get('queue_job_id')}`.\n\n"
                    f"Route: `{route_label}` -> `{route_workflow}`.\n"
                    + (f"Next step: {route_next_step}\n" if route_next_step else "")
                    + "No inventory, listing, order, accounting, or integration records were changed by chat."
                ),
                thread_key=str(human_msg.get("thread_key") or human_msg.get("id") or ""),
                directed_to=[user.username],
                source="business_chat_room",
                metadata={
                    "scope_env": env_key,
                    "responding_to_message_id": int(human_msg.get("id") or 0),
                    "approval_result": approval_result,
                    "action_route": route,
                },
                actor="business_chat_room",
            )
        except Exception as exc:
            approval_error = str(exc)[:500]
    if ai_replies_enabled:
        fresh_snapshot = build_business_room_context_snapshot(repo, room_key=DEFAULT_BUSINESS_ROOM_KEY, limit=40)
        reply_queue = list(agent_keys)
        responded_agents: list[str] = []
        while reply_queue and len(responded_agents) < max_agent_replies:
            agent_key = reply_queue.pop(0)
            if agent_key in responded_agents:
                continue
            reply, reply_meta = _run_agent_reply(
                repo,
                agent_key=agent_key,
                user_message=prompt,
                snapshot=fresh_snapshot,
                attachments=attachments,
                image_inputs=image_inputs,
            )
            if not reply:
                continue
            agent = get_business_agent(agent_key)
            reply_targets = infer_business_room_reply_targets(
                reply_text=reply,
                human_key=user.username,
                sender_agent_key=agent_key,
            )
            record_business_room_message(
                repo,
                room_key=DEFAULT_BUSINESS_ROOM_KEY,
                sender_type="agent",
                sender_key=agent_key,
                sender_label=str(agent.label if agent else agent_key),
                message=reply,
                thread_key=str(human_msg.get("thread_key") or human_msg.get("id") or ""),
                directed_to=reply_targets,
                source="business_chat_room_ai",
                metadata={
                    "scope_env": env_key,
                    "responding_to_message_id": int(human_msg.get("id") or 0),
                    "agent_route": agent_keys,
                    "agent_reply_order": len(responded_agents) + 1,
                    "approval_result": approval_result,
                    "approval_error": approval_error,
                    **reply_meta,
                },
                actor=agent_key,
            )
            responded_agents.append(agent_key)
            for followup_agent_key in plan_business_room_followup_agents(
                reply_text=reply,
                sender_agent_key=agent_key,
                already_responded=responded_agents + reply_queue,
                max_agents=max_agent_replies,
            ):
                if followup_agent_key not in reply_queue and followup_agent_key not in responded_agents:
                    reply_queue.append(followup_agent_key)
    st.rerun()
