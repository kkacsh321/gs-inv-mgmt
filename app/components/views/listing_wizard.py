import json
import hashlib
from html import escape
import imghdr
import re
import statistics
import time

import requests
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from streamlit.errors import StreamlitAPIException

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import build_product_options, to_decimal
from app.components.views.shared import (
    load_media_bytes,
    render_media_capture_inputs,
    safe_switch_page,
    upload_media_for_listing,
)
from app.config import settings
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary, execute_multimodal_task
from app.services.ai_quality import (
    find_forbidden_terms,
    is_weak_listing_details as _is_weak_details_shared,
    is_weak_listing_title,
    load_ai_quality_policy,
)
from app.services.business_chat_room import (
    build_business_room_answer_command_suggestions,
    build_business_room_attachment_evidence_rows,
    build_business_room_handoff_review_card,
    build_business_room_operator_answer_rows,
    list_business_room_workflow_handoffs,
    mark_business_room_workflow_handoff_reviewed,
)
from app.services.ebay_aspects import (
    merge_ebay_aspects_defaults,
    missing_required_ebay_aspects,
    normalize_ebay_category_aspect_rows,
)
from app.services.ebay import (
    EBAY_DEFAULT_INVENTORY_CONDITIONS,
    EBAY_MAX_CONDITION_DESCRIPTION_CHARS,
    EBAY_MAX_INVENTORY_DESCRIPTION_CHARS,
    EbayClient,
    build_ebay_inventory_item_sku,
    ebay_condition_label,
    normalize_ebay_condition_policy_rows,
)
from app.services.ebay_fee_estimator import (
    calculate_expected_net_score,
    estimate_ebay_fees,
    resolve_product_known_unit_cost,
)
from app.services.listing_readiness import evaluate_ebay_readiness
from app.services.llm_runtime import resolve_comp_llm_runtime_chain
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.spot_price import SpotPriceService
from app.services.video_processing import (
    is_ebay_video_upload_candidate,
    is_mov_video_media,
    is_mp4_video_media,
    mp4_filename_for_media,
    transcode_mov_to_mp4,
)
from app.services.workflow_contracts import build_listing_draft_payload, extract_listing_draft_payload
from app.utils.time import utcnow_naive

EBAY_TITLE_MAX_CHARS = 80

DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE = (
    "You are Golden Stackers' expert eBay listing copywriter and compliance reviewer. "
    "Write buyer-facing listings that feel premium, trustworthy, energetic, and specific without hype that cannot be proven. "
    "You are fluent in coins, bullion, precious metals, collectibles, antiques, handmade display pieces, and general resale goods. "
    "Use a warm collector-focused voice, clear sections, scannable bullets, and accurate condition language. "
    "Never invent certifications, grades, precious-metal content, mintage, handmade/original claims, brand affiliation, or scarcity. "
    "Only say handmade, made by Golden Stackers, limited edition, COA included, or made in Colorado when the provided facts support it. "
    "Avoid prohibited or risky marketplace claims, investment promises, medical claims, keyword spam, and unsupported guarantees."
)

DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT = (
    "Write an eBay-ready listing for the selected item. Make it engaging, polished, and buyer-focused. "
    "Use a strong opening, collector/stacker appeal when relevant, accurate specs, condition notes, what's included, "
    "shipping/service reassurance, and a concise reason the item stands out. "
    "Keep claims conservative and evidence-based. Do not claim Golden Stackers made the item unless the product facts say so."
)

DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE = (
    "Return ONLY JSON with keys: "
    "suggested_title, suggested_details, suggested_price, suggested_marketplace_details, "
    "suggested_price_low, suggested_price_high, "
    "best_offer_enabled, best_offer_auto_accept, best_offer_minimum, risk_summary.\n\n"
    "For suggested_details, write 350-900 words when enough facts exist. Use plain text with clear section headings and bullet lines. "
    "Recommended structure: strong title-style opening, short hook paragraph, Item Highlights, Condition & Notes, What's Included, "
    "Display/Use or Collector Appeal when relevant, Shipping & Service, and About Golden Stackers only when appropriate. "
    "Use excitement and personality, but do not fabricate facts. If the item is a third-party product, describe it as offered/sourced/listed by Golden Stackers, not made by us. "
    "For coins with numerical grades, only include the numerical grade when the input clearly says it is certified by PCGS, NGC, ANACS, ICG, CAC, or another approved grading company. "
    "Do not include policy-unsafe words, investment guarantees, bullion return promises, or unsupported authenticity claims."
)

LISTING_WIZARD_WORKFLOW_KEY = "listing_wizard"
LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT = "default"
LISTING_WIZARD_DRAFT_SESSION_KEYS = [
    "listing_wizard_title",
    "listing_wizard_details",
    "listing_wizard_mode",
    "listing_wizard_price",
    "listing_wizard_quantity",
    "listing_wizard_bundle_enabled",
    "listing_wizard_bundle_primary_qty",
    "listing_wizard_bundle_extra_product_labels",
    "listing_wizard_category_id",
    "listing_wizard_store_category_names",
    "listing_wizard_auction_duration",
    "listing_wizard_auction_start",
    "listing_wizard_auction_reserve",
    "listing_wizard_auction_bin",
    "listing_wizard_offer_enabled",
    "listing_wizard_offer_auto_accept",
    "listing_wizard_offer_minimum",
    "listing_wizard_volume_pricing_json",
    "listing_wizard_volume_discount_buy2",
    "listing_wizard_volume_discount_buy3",
    "listing_wizard_volume_discount_buy4",
    "listing_wizard_include_volume_pricing_in_details",
    "listing_wizard_subtitle",
    "listing_wizard_condition_description",
    "listing_wizard_aspects_json",
    "listing_wizard_ebay_merchant_location_key",
    "listing_wizard_ebay_payment_policy_id",
    "listing_wizard_ebay_fulfillment_policy_id",
    "listing_wizard_ebay_return_policy_id",
    "listing_wizard_ebay_marketplace_id",
    "listing_wizard_ebay_currency",
    "listing_wizard_ebay_content_language",
    "listing_wizard_ebay_condition",
    "listing_wizard_ebay_use_eps_images",
    "listing_wizard_ebay_upload_video",
    "listing_wizard_primary_image_ref",
    "listing_wizard_shipping_service",
    "listing_wizard_handling_days",
    "listing_wizard_shipping_cost",
    "listing_wizard_package_weight_oz",
    "listing_wizard_package_length_in",
    "listing_wizard_package_width_in",
    "listing_wizard_package_height_in",
    "listing_wizard_estimated_buyer_shipping",
    "listing_wizard_estimated_promoted_rate",
    "listing_wizard_estimated_local_shipping_cost_per_item",
    "listing_wizard_direct_post_mode",
    "listing_wizard_ebay_dependency_preflight_result",
    "listing_wizard_preflight_blocker_count",
    "listing_wizard_preflight_warning_count",
    "listing_wizard_risk_summary",
    "listing_wizard_ai_suggestions",
    "listing_wizard_ai_diagnostics",
    "listing_wizard_ai_acceptance",
    "listing_wizard_ai_comp_evidence",
    "listing_wizard_ai_has_run",
    "listing_wizard_ai_seed",
    "listing_wizard_seed_signature",
    "listing_wizard_seed_title",
    "listing_wizard_seed_details",
    "listing_wizard_seed_condition_description",
    "listing_wizard_category_query",
    "listing_wizard_category_query_seed_product_id",
    "listing_wizard_category_suggestions",
]


def _parse_product_id(option_label: str | None) -> int | None:
    label = str(option_label or "").strip()
    if not label or not label.startswith("#"):
        return None
    try:
        return int(label.split("|", 1)[0].replace("#", "").strip())
    except Exception:
        return None


def _wizard_option_for_product_id(options: dict[str, int], product_id: int | None) -> str | None:
    if not product_id:
        return None
    for label, pid in options.items():
        try:
            if int(pid) == int(product_id):
                return str(label)
        except Exception:
            continue
    return None


def _merge_product_rows(*groups: list[object]) -> list[object]:
    merged: list[object] = []
    seen: set[int] = set()
    for group in groups:
        for row in group or []:
            try:
                product_id = int(getattr(row, "id", 0) or 0)
            except Exception:
                product_id = 0
            if product_id <= 0 or product_id in seen:
                continue
            seen.add(product_id)
            merged.append(row)
    return merged


def _load_listing_wizard_product_rows(
    repo: InventoryRepository,
    *,
    search_query: str = "",
    selected_product_id: int | None = None,
    recent_limit: int = 75,
    search_limit: int = 100,
) -> list[object]:
    recent_rows = list(repo.list_products(limit=max(1, int(recent_limit or 75))) or [])
    query = str(search_query or "").strip()
    search_rows: list[object] = []
    if query:
        search_rows = list(
            repo.list_products(
                search_query=query,
                limit=max(1, int(search_limit or 100)),
            )
            or []
        )
    selected_rows: list[object] = []
    if selected_product_id:
        try:
            selected_id = int(selected_product_id or 0)
        except Exception:
            selected_id = 0
        if selected_id > 0:
            selected_rows = list(repo.list_products(product_ids=[selected_id], limit=1) or [])
    return _merge_product_rows(selected_rows, search_rows, recent_rows)


def _wizard_option_for_template_id(template_lookup: dict[str, object], template_id: int | None) -> str | None:
    if not template_id:
        return None
    for label, row in (template_lookup or {}).items():
        if row is None:
            continue
        try:
            if int(getattr(row, "id", 0) or 0) == int(template_id):
                return str(label)
        except Exception:
            continue
    return None


def _wizard_apply_draft_payload_to_session(payload: dict) -> None:
    parsed = extract_listing_draft_payload(payload, state_keys=LISTING_WIZARD_DRAFT_SESSION_KEYS)
    state = parsed.get("state")
    if not isinstance(state, dict):
        return
    deferred_updates: dict[str, object] = {}
    for key in LISTING_WIZARD_DRAFT_SESSION_KEYS:
        if key in state:
            try:
                st.session_state[key] = state.get(key)
            except StreamlitAPIException:
                deferred_updates[key] = state.get(key)
    if deferred_updates:
        _wizard_queue_pending_field_updates(deferred_updates)
        prior_flash = str(st.session_state.get("listing_wizard_apply_flash") or "").strip()
        deferred_msg = "Some draft fields were deferred and will apply on the next rerun."
        st.session_state["listing_wizard_apply_flash"] = (
            f"{prior_flash} {deferred_msg}".strip() if prior_flash else deferred_msg
        )


def _wizard_build_draft_payload(
    *,
    selected_product_id: int | None,
    selected_template_id: int | None,
) -> dict:
    state: dict[str, object] = {}
    for key in LISTING_WIZARD_DRAFT_SESSION_KEYS:
        if key in st.session_state:
            state[key] = st.session_state.get(key)
    return build_listing_draft_payload(
        state=state,
        context={
            "selected_product_id": int(selected_product_id or 0),
            "selected_template_id": int(selected_template_id or 0),
        },
    )


def _wizard_draft_signature(payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _wizard_clear_local_draft_state() -> None:
    clear_keys = set(LISTING_WIZARD_DRAFT_SESSION_KEYS)
    clear_keys.update(
        {
            "listing_wizard_product",
            "listing_wizard_template",
            "listing_wizard_pending_field_updates",
            "listing_wizard_seed_signature",
            "listing_wizard_seed_title",
            "listing_wizard_seed_details",
            "listing_wizard_last_autosave_signature",
            "listing_wizard_last_autosave_at",
            "listing_wizard_last_draft_id",
            "listing_wizard_ai_suggestions",
            "listing_wizard_ai_diagnostics",
            "listing_wizard_ai_acceptance",
            "listing_wizard_ai_comp_evidence",
            "listing_wizard_ai_has_run",
            "listing_wizard_ai_show_debug_panels",
            "listing_wizard_resume_applied_once",
            "listing_wizard_resume_payload",
        }
    )
    for key in clear_keys:
        st.session_state.pop(key, None)


def _wizard_promote_direct_post_retry_metadata(publish_meta: dict, context: dict | None) -> dict:
    updated = dict(publish_meta or {})
    context_obj = context if isinstance(context, dict) else {}
    for key in ("inventory_sku", "product_sku", "offer_id"):
        value = str(context_obj.get(key) or "").strip()
        if value:
            updated[key] = value
    return updated


def _wizard_normalize_store_category_names(values: object) -> list[str]:
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    else:
        raw_values = []
    names: list[str] = []
    for value in raw_values:
        clean = str(value or "").strip()
        if clean and clean not in names:
            names.append(clean)
        if len(names) >= 2:
            break
    return names


def _wizard_store_category_options(repo: InventoryRepository, *, marketplace_id: str) -> list[str]:
    rows = repo.list_ebay_store_categories(
        environment=settings.app_env,
        marketplace_id=str(marketplace_id or "EBAY_US").strip() or "EBAY_US",
        active_only=True,
    )
    return [
        str(getattr(row, "category_path", "") or "").strip()
        for row in rows
        if str(getattr(row, "category_path", "") or "").strip()
    ]


def _render_listing_wizard_business_room_handoffs(repo: InventoryRepository, *, username: str) -> None:
    handoffs = list_business_room_workflow_handoffs(
        repo,
        environment=settings.app_env,
        workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
        username=username,
        limit=20,
    )
    loaded = st.session_state.get("listing_wizard_business_room_handoff_context")
    if isinstance(loaded, dict) and str(loaded.get("prompt") or "").strip():
        st.info(
            "Loaded Business Chat Room handoff context: "
            + str(loaded.get("prompt") or "").strip()[:180]
        )
    if not handoffs:
        st.caption("No Business Chat Room listing handoffs are waiting for this user.")
        return

    with st.expander(f"Business Chat Room Handoffs ({len(handoffs)})", expanded=False):
        st.caption(
            "Approved room requests routed to Listing Wizard appear here. Loading one adds its prompt to this session's AI draft context; it does not publish or create a listing by itself."
        )
        table_rows = [
            {
                "draft_id": row["id"],
                "queue_job_id": row["queue_job_id"],
                "route": row["route_label"] or row["route"],
                "requester": row["requester"],
                "attachments": row["attachment_count"],
                "prompt": row["prompt"][:160],
                "updated_at": row["updated_at"],
            }
            for row in handoffs
        ]
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
        options = {
            f"#{row['id']} | job {row['queue_job_id']} | {(row['prompt'] or 'No prompt')[:80]}": row
            for row in handoffs
        }
        selected_label = st.selectbox(
            "Select handoff",
            options=list(options.keys()),
            key="listing_wizard_business_room_handoff_select",
        )
        selected = options[selected_label]
        review_card = build_business_room_handoff_review_card(
            selected,
            workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
        )
        if review_card.get("fields"):
            st.dataframe(
                [
                    {
                        "field": row.get("key"),
                        "value": row.get("value"),
                        "confidence": row.get("confidence"),
                        "source": row.get("source"),
                    }
                    for row in review_card.get("fields", [])
                    if isinstance(row, dict)
                ],
                use_container_width=True,
                hide_index=True,
            )
        for warning in review_card.get("warnings", [])[:3]:
            st.caption(f"Review note: {warning}")
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
        missing_questions = [
            row
            for row in review_card.get("missing_questions", [])
            if isinstance(row, dict) and str(row.get("question") or row.get("field") or "").strip()
        ]
        if missing_questions:
            st.caption(
                "Missing confirmations: "
                + "; ".join(str(row.get("question") or row.get("field") or "") for row in missing_questions[:5])
            )
        answer_suggestions = build_business_room_answer_command_suggestions(
            selected,
            review_card=review_card,
            max_suggestions=8,
        )
        if answer_suggestions:
            st.caption("Reply in Business Chat Room or Slack with one of:")
            st.code("\n".join(answer_suggestions), language="text")
        selected_payload = selected.get("payload") if isinstance(selected.get("payload"), dict) else {}
        operator_answers = build_business_room_operator_answer_rows(selected_payload)
        if operator_answers:
            st.dataframe(
                operator_answers[:10],
                use_container_width=True,
                hide_index=True,
            )
        apply_plan = selected_payload.get("apply_plan") if isinstance(selected_payload.get("apply_plan"), dict) else {}
        if apply_plan:
            st.caption(
                "Apply plan: "
                f"`{apply_plan.get('status') or 'unknown'}`"
                + (f" ({apply_plan.get('reason')})" if apply_plan.get("reason") else "")
            )
        proposed_actions = [
            row
            for row in review_card.get("proposed_actions", [])
            if isinstance(row, dict) and str(row.get("action") or "").strip()
        ]
        if proposed_actions:
            st.caption("Proposed actions: " + ", ".join(str(row.get("action") or "") for row in proposed_actions[:5]))
        st.text_area(
            "Handoff Prompt",
            value=str(selected.get("prompt") or ""),
            height=120,
            disabled=True,
            key="listing_wizard_business_room_handoff_prompt_preview",
        )
        next_step = str(selected.get("next_step") or "").strip()
        if next_step:
            st.caption(f"Recommended next step: {next_step}")
        attachment_rows = build_business_room_attachment_evidence_rows(selected_payload)
        if attachment_rows:
            st.caption("Attachment evidence")
            st.dataframe(attachment_rows, use_container_width=True, hide_index=True)
        elif int(selected.get("attachment_count") or 0) > 0:
            st.caption(f"Attachment evidence included: {int(selected.get('attachment_count') or 0)} file(s).")
        if st.button("Load Handoff Context", key="listing_wizard_load_business_room_handoff_btn"):
            payload = dict(selected.get("payload") or {})
            prompt = str(payload.get("prompt") or selected.get("prompt") or "").strip()
            st.session_state["listing_wizard_business_room_handoff_context"] = payload
            st.session_state["listing_wizard_business_room_review_card"] = review_card
            field_values = dict(review_card.get("field_values") or {})
            product_id = field_values.get("product_id")
            if product_id:
                st.session_state["listing_wizard_product_search"] = str(product_id)
            title = str(field_values.get("title") or "").strip()
            if title and not str(st.session_state.get("listing_wizard_title") or "").strip():
                st.session_state["listing_wizard_title"] = title[:EBAY_TITLE_MAX_CHARS].rstrip(" -_,.;:")
            description = str(
                field_values.get("description_html")
                or field_values.get("description")
                or field_values.get("listing_description")
                or ""
            ).strip()
            if description and not str(st.session_state.get("listing_wizard_details") or "").strip():
                st.session_state["listing_wizard_details"] = description
            if prompt:
                existing_seed = str(st.session_state.get("listing_wizard_ai_seed") or "").strip()
                handoff_block = (
                    "\n\nBusiness Chat Room handoff context:\n"
                    + prompt
                    + "\nUse this as operator intent and evidence context, but keep listing claims evidence-based."
                )
                if "Business Chat Room handoff context:" not in existing_seed:
                    st.session_state["listing_wizard_ai_seed"] = (existing_seed + handoff_block).strip()
            repo.append_workflow_event(
                environment=settings.app_env,
                workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                username=username,
                scope_key=str(selected.get("scope_key") or LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT),
                action="load_business_room_handoff",
                status="ok",
                message="Operator loaded Business Chat Room handoff context into Listing Wizard session.",
                payload={
                    "handoff_draft_id": int(selected.get("id") or 0),
                    "queue_job_id": int(selected.get("queue_job_id") or 0),
                    "source_message_id": int(selected.get("source_message_id") or 0),
                    "prompt_loaded": bool(prompt),
                    "field_count": len(review_card.get("fields") or []),
                    "draft_signature": str(review_card.get("signature") or ""),
                    "route": str(selected.get("route") or ""),
                },
                draft_id=int(selected.get("id") or 0),
                actor=username,
            )
            st.success("Loaded handoff context into this Listing Wizard session.")
            st.rerun()
        if st.button("Mark Handoff Reviewed", key="listing_wizard_reviewed_business_room_handoff_btn"):
            scope_key = str(selected.get("scope_key") or "").strip()
            if not scope_key:
                st.warning("Selected handoff is missing a scope key.")
            else:
                mark_business_room_workflow_handoff_reviewed(
                    repo,
                    environment=settings.app_env,
                    workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                    username=username,
                    actor=username,
                    source="listing_wizard",
                    handoff=selected,
                )
                st.success("Marked Business Chat Room handoff reviewed.")
                st.rerun()


def _category_query_seed(*, title: str, category: str = "", metal_type: str = "", sku: str = "") -> str:
    parts: list[str] = []
    for raw in [title, category, metal_type]:
        value = str(raw or "").strip()
        if value:
            parts.append(value)
    if not parts and str(sku or "").strip():
        parts.append(str(sku or "").strip())
    return " ".join(parts).strip()


def _try_json(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}
    # Common model output shape: fenced JSON block.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = str(fenced.group(1) or "").strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    # Fallback: parse first plausible JSON object substring.
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        snippet = raw[first : last + 1]
        try:
            parsed = json.loads(snippet)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_ai_suggestion_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    normalized = dict(payload)
    # Keep booleans strict and numeric fields safely coercible.
    normalized["best_offer_enabled"] = bool(payload.get("best_offer_enabled", False))
    for key in ("suggested_price", "suggested_price_low", "suggested_price_high", "best_offer_minimum"):
        normalized[key] = _safe_price_float(payload.get(key), 0.0)
    auto_accept_raw = payload.get("best_offer_auto_accept")
    if isinstance(auto_accept_raw, bool):
        normalized["best_offer_auto_accept"] = 0.0 if not auto_accept_raw else _safe_price_float(payload.get("suggested_price"), 0.0)
    else:
        normalized["best_offer_auto_accept"] = _safe_price_float(auto_accept_raw, 0.0)
    normalized["suggested_title"] = str(payload.get("suggested_title") or "").strip()
    normalized["suggested_details"] = str(
        payload.get("suggested_details")
        or payload.get("suggested_description")
        or payload.get("description")
        or payload.get("details")
        or ""
    ).strip()
    normalized["suggested_marketplace_details"] = str(payload.get("suggested_marketplace_details") or "").strip()
    normalized["risk_summary"] = str(payload.get("risk_summary") or "").strip()
    return normalized


def _resolve_ai_suggested_details(payload: dict, *, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return str(fallback or "").strip()
    for key in ("suggested_details", "suggested_description", "description", "details"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    marketplace_details = str(payload.get("suggested_marketplace_details") or "").strip()
    normalized = marketplace_details.lower().strip(" .,:;!?")
    # Ignore channel labels that are not actual listing body text.
    if normalized in {"ebay", "ebay.com", "marketplace", "online marketplace"}:
        return str(fallback or "").strip()
    if marketplace_details and len(marketplace_details) >= 12:
        return marketplace_details
    return str(fallback or "").strip()


def _is_weak_listing_details(text: str, *, policy=None) -> bool:
    return _is_weak_details_shared(text, policy=policy)


def _build_fallback_ebay_listing_details(product, *, title: str = "", existing_description: str = "") -> str:
    product_title = str(title or getattr(product, "title", "") or "").strip()
    category = str(getattr(product, "category", "") or "").strip()
    metal_type = str(getattr(product, "metal_type", "") or "").strip()
    weight_oz = str(getattr(product, "weight_oz", "") or "").strip()
    sku = str(getattr(product, "sku", "") or "").strip()
    base_description = str(existing_description or getattr(product, "description", "") or "").strip()

    highlights: list[str] = []
    if category:
        highlights.append(f"Category: {category}")
    if metal_type:
        highlights.append(f"Metal/Material: {metal_type}")
    if weight_oz:
        highlights.append(f"Weight: {weight_oz} oz")
    if sku:
        highlights.append(f"Internal SKU: {sku}")

    specs_block = "\n".join([f"- {line}" for line in highlights]) if highlights else "- See photos for complete item details."
    base_copy = (
        base_description
        if base_description
        else "Please review all photos carefully for condition details and design specifics before purchase."
    )
    return (
        f"{product_title or 'Item Listing'}\n\n"
        "You are purchasing exactly the item shown. This listing is prepared for eBay buyers and written to be clear, accurate, and easy to review.\n\n"
        "Item highlights:\n"
        f"{specs_block}\n\n"
        f"Description:\n{base_copy}\n\n"
        "Condition & notes:\n"
        "- Pre-owned/collectible condition unless explicitly marked otherwise.\n"
        "- Minor handling marks, tone/patina, or normal storage wear may be present.\n"
        "- Photos are part of the description and represent the actual item.\n\n"
        "Shipping & service:\n"
        "- Packaged carefully and shipped promptly.\n"
        "- Combined shipping may be available for multiple items when applicable.\n"
        "- Contact us with any questions before purchase."
    ).strip()


def _product_ai_grading_description(product) -> str:
    if product is None:
        return ""
    return str(getattr(product, "ai_grading_description", "") or "").strip()


def _with_ai_grading_notes(details: str, *, grading_description: str) -> str:
    base = str(details or "").strip()
    grading = str(grading_description or "").strip()
    if not grading:
        return base
    marker = "AI Grading Notes:"
    if marker.lower() in base.lower():
        return base
    block = f"{marker}\n{grading}"
    return f"{base}\n\n{block}" if base else block


def _ai_grading_prefill_status(*, current_value: str, default_value: str) -> str:
    current = str(current_value or "").strip()
    default = str(default_value or "").strip()
    if not default:
        return ""
    if current == default:
        return "AI Grading Description is prefilled from the selected product record."
    if current:
        return "AI Grading Description has been edited from the selected product default."
    return "Selected product has AI grading text available."


def _safe_price_float(value: object, default: float = 0.0) -> float:
    raw = str(value or "").strip()
    if not raw:
        return float(default)
    cleaned = raw.replace("$", "").replace(",", "").strip()
    if " to " in cleaned.lower():
        cleaned = cleaned.lower().split(" to ", 1)[0].strip()
    if "-" in cleaned and cleaned.count("-") == 1 and not cleaned.startswith("-"):
        left, _right = cleaned.split("-", 1)
        if left.strip():
            cleaned = left.strip()
    try:
        return float(to_decimal(cleaned))
    except Exception:
        try:
            return float(cleaned)
        except Exception:
            return float(default)


def _known_unit_cost(product: object | None) -> float:
    try:
        return round(float(resolve_product_known_unit_cost(product)), 2)
    except Exception:
        return 0.0


def _bundle_component(product: object, quantity_per_listing: int) -> dict[str, object]:
    units = max(1, int(quantity_per_listing or 1))
    return {
        "product_id": int(getattr(product, "id", 0) or 0),
        "sku": str(getattr(product, "sku", "") or "").strip(),
        "title": str(getattr(product, "title", "") or "").strip(),
        "quantity_per_listing": units,
        "current_quantity": int(getattr(product, "current_quantity", 0) or 0),
    }


def _bundle_expected_unit_cost(bundle_metadata: dict, product_by_id: dict[int, object]) -> float:
    if not bool((bundle_metadata or {}).get("enabled")):
        return 0.0
    total = 0.0
    for component in list((bundle_metadata or {}).get("components") or []):
        if not isinstance(component, dict):
            continue
        try:
            product_id = int(component.get("product_id") or 0)
            qty = max(1, int(component.get("quantity_per_listing") or 1))
        except Exception:
            continue
        total += _known_unit_cost(product_by_id.get(product_id)) * qty
    return round(max(0.0, total), 2)


def _expected_net_score(
    *,
    fee_estimate: dict,
    quantity: int,
    known_unit_cost: float,
    estimated_local_shipping_cost_per_item: float,
) -> dict[str, float | str]:
    return calculate_expected_net_score(
        fee_estimate=fee_estimate,
        quantity=quantity,
        known_unit_cost=known_unit_cost,
        estimated_local_shipping_cost_per_item=estimated_local_shipping_cost_per_item,
    )


def _build_listing_bundle_metadata(
    *,
    enabled: bool,
    primary_product: object | None,
    units_per_listing: int,
    available_lots: int,
    additional_components: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    lots = max(1, int(available_lots or 1))
    if not enabled or primary_product is None:
        return {
            "enabled": False,
            "components": [],
            "units_per_listing_total": 1,
            "available_lots": lots,
            "inventory_units_committed": lots,
        }
    product_id = int(getattr(primary_product, "id", 0) or 0)
    component = _bundle_component(primary_product, units_per_listing)
    components = [component]
    for extra in list(additional_components or []):
        if not isinstance(extra, dict):
            continue
        try:
            extra_product_id = int(extra.get("product_id") or 0)
            extra_units = max(1, int(extra.get("quantity_per_listing") or 1))
        except Exception:
            continue
        if extra_product_id <= 0 or extra_product_id == product_id:
            continue
        components.append(
            {
                "product_id": extra_product_id,
                "sku": str(extra.get("sku") or "").strip(),
                "title": str(extra.get("title") or "").strip(),
                "quantity_per_listing": extra_units,
                "current_quantity": max(0, int(extra.get("current_quantity") or 0)),
            }
        )
    units_total = sum(max(1, int(row.get("quantity_per_listing") or 1)) for row in components)
    return {
        "enabled": True,
        "kind": "mixed_product_bundle" if len(components) > 1 else "single_product_lot",
        "primary_product_id": product_id,
        "components": components,
        "units_per_listing_total": units_total,
        "available_lots": lots,
        "inventory_units_committed": units_total * lots,
    }


def _render_ebay_preflight_card(result: dict, *, title: str = "eBay Dependency Preflight") -> None:
    payload = result if isinstance(result, dict) else {}
    blockers = list(payload.get("blockers") or [])
    warnings = list(payload.get("warnings") or [])
    checks = list(payload.get("checks") or [])
    checked_at = str(payload.get("checked_at") or "").strip()
    pass_count = int(sum(1 for row in checks if bool((row or {}).get("ok"))))
    fail_count = int(sum(1 for row in checks if not bool((row or {}).get("ok"))))
    warn_count = int(len(warnings))

    st.markdown(f"#### {title}")
    if checked_at:
        st.caption(f"Last checked: {checked_at}")
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Pass", pass_count)
    with m2:
        st.metric("Warn", warn_count)
    with m3:
        st.metric("Fail", fail_count)
    if blockers:
        st.error("Blockers: " + " | ".join(blockers[:3]))
    elif warnings:
        st.warning("Warnings: " + " | ".join(warnings[:3]))
    else:
        st.success("All dependency checks passed.")
    if checks:
        st.dataframe(pd.DataFrame(checks), use_container_width=True, hide_index=True)


def _suggested_price_band(payload: dict) -> tuple[float, float, float]:
    price = _safe_price_float(payload.get("suggested_price"), 0.0)
    low = _safe_price_float(payload.get("suggested_price_low"), 0.0)
    high = _safe_price_float(payload.get("suggested_price_high"), 0.0)

    if low > 0 and high > 0 and high < low:
        low, high = high, low

    if price <= 0 and low > 0 and high > 0:
        price = (low + high) / 2.0

    if (low <= 0 or high <= 0) and price > 0:
        low = low if low > 0 else (price * 0.9)
        high = high if high > 0 else (price * 1.1)

    if low <= 0:
        low = 0.0
    if high <= 0:
        high = 0.0
    if price <= 0 and low > 0 and high > 0:
        price = (low + high) / 2.0

    return (round(price, 2), round(low, 2), round(high, 2))


def _wizard_build_ebay_offer_payload(
    *,
    sku: str,
    marketplace_id: str,
    format_type: str,
    listing_qty: int,
    category_id: str,
    merchant_location_key: str,
    listing_description: str,
    listing_duration: str,
    payment_policy_id: str,
    fulfillment_policy_id: str,
    return_policy_id: str,
    currency: str,
    fixed_price: float,
    best_offer_enabled: bool,
    best_offer_auto_accept: float,
    best_offer_minimum: float,
    auction_start_price: float,
    auction_reserve_price: float,
    auction_buy_now_price: float,
    store_category_names: list[str] | None = None,
) -> dict:
    fmt = str(format_type or "FIXED_PRICE").strip().upper()
    if fmt not in {"FIXED_PRICE", "AUCTION"}:
        fmt = "FIXED_PRICE"

    payload: dict[str, object] = {
        "sku": str(sku or "").strip(),
        "marketplaceId": str(marketplace_id or "").strip(),
        "format": fmt,
        "categoryId": str(category_id or "").strip(),
        "merchantLocationKey": str(merchant_location_key or "").strip(),
        "listingDescription": str(listing_description or "").strip(),
        "listingDuration": str(listing_duration or "").strip().upper(),
        "listingPolicies": {
            "paymentPolicyId": str(payment_policy_id or "").strip(),
            "fulfillmentPolicyId": str(fulfillment_policy_id or "").strip(),
            "returnPolicyId": str(return_policy_id or "").strip(),
        },
        "pricingSummary": {},
    }
    if fmt != "AUCTION":
        payload["availableQuantity"] = max(1, int(listing_qty or 1))
    store_paths = [
        str(path or "").strip()
        for path in (store_category_names or [])
        if str(path or "").strip()
    ][:2]
    if store_paths:
        payload["storeCategoryNames"] = store_paths

    pricing_summary = payload["pricingSummary"]
    if not isinstance(pricing_summary, dict):
        pricing_summary = {}
        payload["pricingSummary"] = pricing_summary

    currency_value = str(currency or "").strip()
    if fmt == "FIXED_PRICE":
        pricing_summary["price"] = {
            "value": str(round(float(fixed_price or 0.0), 2)),
            "currency": currency_value,
        }
        listing_policies = payload["listingPolicies"]
        if isinstance(listing_policies, dict):
            if bool(best_offer_enabled):
                best_offer_terms: dict[str, object] = {"bestOfferEnabled": True}
                if float(best_offer_auto_accept or 0.0) > 0:
                    best_offer_terms["autoAcceptPrice"] = {
                        "value": str(round(float(best_offer_auto_accept or 0.0), 2)),
                        "currency": currency_value,
                    }
                if float(best_offer_minimum or 0.0) > 0:
                    best_offer_terms["autoDeclinePrice"] = {
                        "value": str(round(float(best_offer_minimum or 0.0), 2)),
                        "currency": currency_value,
                    }
                listing_policies["bestOfferTerms"] = best_offer_terms
            else:
                listing_policies["bestOfferTerms"] = {"bestOfferEnabled": False}
    else:
        pricing_summary["auctionStartPrice"] = {
            "value": str(round(float(auction_start_price or 0.0), 2)),
            "currency": currency_value,
        }
        if float(auction_reserve_price or 0.0) > 0:
            pricing_summary["auctionReservePrice"] = {
                "value": str(round(float(auction_reserve_price or 0.0), 2)),
                "currency": currency_value,
            }
        if float(auction_buy_now_price or 0.0) > 0:
            pricing_summary["price"] = {
                "value": str(round(float(auction_buy_now_price or 0.0), 2)),
                "currency": currency_value,
            }

    return payload


def _uploaded_file_bytes(uploaded_file) -> bytes:
    if hasattr(uploaded_file, "getvalue"):
        return uploaded_file.getvalue()
    return uploaded_file.read()


def _resolve_vision_mime(payload: bytes, content_type: str) -> str:
    blob = payload or b""
    if not blob:
        return ""
    ctype = str(content_type or "").strip().lower()
    if ";" in ctype:
        ctype = ctype.split(";", 1)[0].strip()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype.startswith("image/svg"):
        return ""
    detected = str(imghdr.what(None, h=blob[:2048]) or "").strip().lower()
    detected_mime = {
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(detected, "")
    if detected_mime:
        return detected_mime
    if ctype in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        return ctype
    return ""


def _is_supported_vision_image(payload: bytes, content_type: str) -> bool:
    return bool(_resolve_vision_mime(payload, content_type))


def _sanitize_preview_html(value: str) -> str:
    html = str(value or "").strip()
    if not html:
        return ""
    sanitized = re.sub(r"(?is)<\s*script[^>]*>.*?<\s*/\s*script\s*>", "", html)
    sanitized = re.sub(r"(?is)<\s*style[^>]*>.*?<\s*/\s*style\s*>", "", sanitized)
    sanitized = re.sub(
        r"(?i)\s+on[a-z0-9_-]+\s*=\s*(\".*?\"|\'.*?\'|[^\s>]+)",
        "",
        sanitized,
    )
    sanitized = re.sub(r"(?i)javascript\s*:", "", sanitized)
    return sanitized.strip()


def _looks_like_listing_html(value: str) -> bool:
    return bool(re.search(r"(?is)<\s*(p|br|div|ul|ol|li|h[1-6]|strong|b|em|span|table|section)\b", str(value or "")))


def _plain_text_listing_to_html(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    blocks = re.split(r"\n\s*\n+", text)
    html_parts: list[str] = []
    pending_bullets: list[str] = []

    def _flush_bullets() -> None:
        if not pending_bullets:
            return
        items = "".join(f"<li>{escape(item.strip())}</li>" for item in pending_bullets if item.strip())
        if items:
            html_parts.append(f"<ul>{items}</ul>")
        pending_bullets.clear()

    heading_pattern = re.compile(r"^[A-Z][A-Za-z0-9 &'/.:-]{2,80}$")
    bullet_pattern = re.compile(r"^\s*(?:[-*•]|[0-9]+[.)])\s+(.+?)\s*$")
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if len(lines) == 1:
            raw_line = lines[0].strip()
            bullet_match = bullet_pattern.match(raw_line)
            if bullet_match:
                pending_bullets.append(bullet_match.group(1).strip())
                continue
            _flush_bullets()
            if heading_pattern.match(raw_line) and len(raw_line.split()) <= 8 and not raw_line.endswith("."):
                html_parts.append(f"<h3>{escape(raw_line)}</h3>")
            else:
                html_parts.append(f"<p>{escape(raw_line)}</p>")
            continue
        _flush_bullets()
        paragraph_lines: list[str] = []
        for line in lines:
            bullet_match = bullet_pattern.match(line)
            if bullet_match:
                if paragraph_lines:
                    html_parts.append(f"<p>{escape(' '.join(paragraph_lines).strip())}</p>")
                    paragraph_lines = []
                pending_bullets.append(bullet_match.group(1).strip())
            else:
                _flush_bullets()
                paragraph_lines.append(line.strip())
        if paragraph_lines:
            html_parts.append(f"<p>{escape(' '.join(paragraph_lines).strip())}</p>")
    _flush_bullets()
    return "\n".join(html_parts).strip()


def _format_listing_description_for_ebay(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if _looks_like_listing_html(raw):
        return _sanitize_preview_html(raw)
    return _sanitize_preview_html(_plain_text_listing_to_html(raw))


def _wizard_normalize_aspects_payload(raw_text: str) -> dict[str, list[str]]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in parsed.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, list):
            vals = [str(item).strip() for item in value if str(item).strip()]
        else:
            single = str(value or "").strip()
            vals = [single] if single else []
        if vals:
            out[name] = vals
    return out


def _wizard_norm_aspect_name(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _wizard_category_aspect_table_rows(
    category_aspects: list[dict[str, object]],
    existing_aspects: dict[str, list[str]],
) -> list[dict[str, object]]:
    existing_keys = {
        _wizard_norm_aspect_name(key): values for key, values in (existing_aspects or {}).items()
    }
    rows: list[dict[str, object]] = []
    for row in normalize_ebay_category_aspect_rows(category_aspects):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        values = row.get("values") or []
        current_values = existing_keys.get(_wizard_norm_aspect_name(name)) or []
        rows.append(
            {
                "aspect": name,
                "required": "yes" if bool(row.get("required")) else "no",
                "status": "filled" if current_values else "missing",
                "usage": str(row.get("usage") or ""),
                "mode": str(row.get("mode") or ""),
                "allowed_values": ", ".join(str(value) for value in values[:8]),
            }
        )
    return rows


def _wizard_category_aspect_input_key(prefix: str, aspect_name: str) -> str:
    digest = hashlib.sha1(str(aspect_name or "").strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _wizard_condition_option_labels(
    condition_rows: list[dict[str, object]],
    current_condition: str = "",
) -> dict[str, str]:
    rows = condition_rows if isinstance(condition_rows, list) else []
    labels: dict[str, str] = {}
    for row in rows:
        condition = str((row or {}).get("condition") or "").strip().upper()
        if not condition:
            continue
        label = str((row or {}).get("label") or "").strip() or ebay_condition_label(condition)
        condition_id = str((row or {}).get("condition_id") or "").strip()
        labels[condition] = f"{label} ({condition})" + (f" / eBay ID {condition_id}" if condition_id else "")
    if not labels:
        labels = {condition: ebay_condition_label(condition) for condition in EBAY_DEFAULT_INVENTORY_CONDITIONS}
    current = str(current_condition or "").strip().upper()
    if current and current not in labels:
        labels[current] = f"{ebay_condition_label(current)} ({current}) - not in loaded category policy"
    return labels


def _wizard_condition_options(condition_rows: list[dict[str, object]], current_condition: str = "") -> list[str]:
    labels = _wizard_condition_option_labels(condition_rows, current_condition)
    ordered = [str((row or {}).get("condition") or "").strip().upper() for row in (condition_rows or [])]
    options = [condition for condition in ordered if condition in labels]
    if not options:
        options = [condition for condition in EBAY_DEFAULT_INVENTORY_CONDITIONS if condition in labels]
    current = str(current_condition or "").strip().upper()
    if current and current not in options:
        options.append(current)
    return options


def _wizard_is_condition_valid_for_loaded_policy(condition_rows: list[dict[str, object]], condition: str) -> bool:
    if not condition_rows:
        return True
    allowed = {str((row or {}).get("condition") or "").strip().upper() for row in condition_rows}
    return str(condition or "").strip().upper() in allowed


def _wizard_order_media_rows_for_primary(media_rows: list[object], primary_ref: str) -> list[object]:
    rows = list(media_rows or [])
    ref = str(primary_ref or "").strip()
    if not rows or not ref:
        return rows
    if ref.startswith("media:"):
        try:
            primary_id = int(ref.split(":", 1)[1])
        except Exception:
            primary_id = 0
        if primary_id > 0:
            return sorted(rows, key=lambda row: 0 if int(getattr(row, "id", 0) or 0) == primary_id else 1)
    if ref.startswith("upload:"):
        filename = ref.split(":", 1)[1].strip()
        if filename:
            return sorted(
                rows,
                key=lambda row: (
                    0 if str(getattr(row, "original_filename", "") or "").strip() == filename else 1
                ),
            )
    return rows


def _wizard_primary_image_metadata(media_rows: list[object], primary_ref: str) -> dict[str, object]:
    rows = list(media_rows or [])
    primary_media = rows[0] if rows else None
    ref = str(primary_ref or "").strip()
    return {
        "primary_image_ref": ref,
        "primary_image_media_id": int(getattr(primary_media, "id", 0) or 0)
        if primary_media is not None
        else 0,
        "primary_image_filename": str(getattr(primary_media, "original_filename", "") or "").strip()
        if primary_media is not None
        else "",
    }


def _wizard_ebay_image_url_from_result(result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    direct = str(result.get("imageUrl") or "").strip()
    if direct:
        return direct
    nested = result.get("image")
    if isinstance(nested, dict):
        return str(nested.get("imageUrl") or "").strip()
    return ""


def _wizard_is_transient_ebay_media_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in [
            " 500 ",
            " 502 ",
            " 503 ",
            " 504 ",
            "internal server error",
            "service unavailable",
            "gateway",
            "timed out",
            "timeout",
            "temporarily",
        ]
    )


def _wizard_create_eps_image_with_retry(
    *,
    ebay: EbayClient,
    access_token: str,
    media,
    storage,
    max_attempts: int = 3,
) -> tuple[str, dict]:
    original_url = str(getattr(media, "s3_url", "") or "").strip()
    filename = str(getattr(media, "original_filename", "") or "image.jpg").strip() or "image.jpg"
    content_type = str(getattr(media, "content_type", "") or "image/jpeg").strip() or "image/jpeg"
    errors: list[str] = []

    image_bytes, image_content_type, load_err = load_media_bytes(media, storage=storage)
    if image_bytes is not None:
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                image_result = ebay.create_image_from_file(
                    access_token=access_token,
                    file_bytes=image_bytes,
                    filename=filename,
                    content_type=image_content_type or content_type,
                )
                eps_url = _wizard_ebay_image_url_from_result(image_result)
                if eps_url:
                    return eps_url, {
                        "media_asset_id": int(getattr(media, "id", 0) or 0),
                        "filename": filename,
                        "source_url": original_url,
                        "eps_url": eps_url,
                        "mode": "file_upload",
                        "attempts": attempt,
                    }
                raise RuntimeError("No imageUrl returned from eBay Media API file upload.")
            except Exception as exc:
                errors.append(f"file-upload attempt {attempt}: {exc}")
                if attempt >= max_attempts or not _wizard_is_transient_ebay_media_error(exc):
                    break
                time.sleep(0.5 * attempt)
    else:
        errors.append(load_err or "media bytes unavailable")

    if original_url.startswith("https://"):
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                image_result = ebay.create_image_from_url(access_token=access_token, image_url=original_url)
                eps_url = _wizard_ebay_image_url_from_result(image_result)
                if eps_url:
                    return eps_url, {
                        "media_asset_id": int(getattr(media, "id", 0) or 0),
                        "filename": filename,
                        "source_url": original_url,
                        "eps_url": eps_url,
                        "mode": "url_import",
                        "attempts": attempt,
                    }
                raise RuntimeError("No imageUrl returned from eBay Media API URL import.")
            except Exception as exc:
                errors.append(f"url-import attempt {attempt}: {exc}")
                if attempt >= max_attempts or not _wizard_is_transient_ebay_media_error(exc):
                    break
                time.sleep(0.5 * attempt)
    else:
        errors.append("URL import unavailable because media has no public HTTPS URL.")

    raise RuntimeError("eBay EPS image hosting failed; direct/self-hosted image fallback is disabled. " + " | ".join(errors[:6]))


def _wizard_is_mp4_video_media(media) -> bool:
    return is_mp4_video_media(media)


def _wizard_select_ebay_video_media(media_rows: list[object]) -> object | None:
    for media in list(media_rows or []):
        if str(getattr(media, "media_type", "") or "").strip().lower() != "video":
            continue
        if is_ebay_video_upload_candidate(media):
            return media
    return None


def _wizard_video_upload_warning(upload_video_to_ebay: bool, video_rows: list[object]) -> str:
    if not bool(upload_video_to_ebay):
        return ""
    rows = list(video_rows or [])
    if not rows:
        return (
            "Video upload was enabled, but no video media was linked to the created listing draft. "
            "Attach/select an MP4 or MOV listing video and retry."
        )
    if _wizard_select_ebay_video_media(rows) is None:
        return (
            "Attached listing video media exists, but no supported MP4/MOV video was found for eBay upload. "
            "Attach/select an MP4 video or MOV/QuickTime video and retry."
        )
    return ""


def _wizard_upload_ebay_video_with_retry(
    *,
    ebay: EbayClient,
    access_token: str,
    media,
    storage,
    listing_title: str,
    max_attempts: int = 3,
    status_checks: int = 30,
    status_sleep_seconds: float = 3.0,
) -> tuple[str, dict]:
    if not is_ebay_video_upload_candidate(media):
        raise RuntimeError("Only MP4 or MOV video upload is currently supported for eBay video attach.")

    original_filename = str(getattr(media, "original_filename", "") or "listing-video.mp4").strip()
    filename = mp4_filename_for_media(media)
    content_type = str(getattr(media, "content_type", "") or "video/mp4").strip() or "video/mp4"
    video_bytes, video_content_type, load_err = load_media_bytes(media, storage=storage)
    if video_bytes is None:
        raise RuntimeError(load_err or "Video bytes unavailable.")
    converted_from = ""
    if is_mov_video_media(media):
        video_bytes = transcode_mov_to_mp4(video_bytes, filename=original_filename or "listing-video.mov")
        video_content_type = "video/mp4"
        content_type = "video/mp4"
        converted_from = "mov"

    errors: list[str] = []
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            video_id = ebay.create_video(
                access_token=access_token,
                title=filename,
                size_bytes=len(video_bytes),
                description=str(listing_title or filename).strip(),
            )
            ebay.upload_video(
                access_token=access_token,
                video_id=video_id,
                file_bytes=video_bytes,
            )
            final_status = ""
            for _ in range(max(1, int(status_checks))):
                video_state = ebay.get_video(access_token=access_token, video_id=video_id)
                final_status = str(video_state.get("status") or "").upper()
                if final_status == "LIVE":
                    return video_id, {
                        "media_asset_id": int(getattr(media, "id", 0) or 0),
                        "filename": filename,
                        "original_filename": original_filename,
                        "video_id": video_id,
                        "status": final_status,
                        "attempts": attempt,
                        "converted_from": converted_from,
                    }
                if final_status in {"PROCESSING_FAILED", "BLOCKED"}:
                    raise RuntimeError(f"Video status reached terminal failure state: {final_status}")
                time.sleep(float(status_sleep_seconds))
            raise RuntimeError(
                "Video upload did not reach LIVE status within timeout. "
                f"Last status: {final_status or 'unknown'}"
            )
        except Exception as exc:
            errors.append(f"attempt {attempt}: {exc}")
            if attempt >= max_attempts or not _wizard_is_transient_ebay_media_error(exc):
                break
            time.sleep(0.5 * attempt)
    raise RuntimeError("eBay video upload failed. " + " | ".join(errors[:5]))


def _wizard_verify_inventory_video_ids(
    *,
    ebay: EbayClient,
    access_token: str,
    sku: str,
    expected_video_ids: list[str],
    content_language: str = "en-US",
    max_attempts: int = 3,
    sleep_seconds: float = 1.0,
) -> dict:
    expected = [str(value or "").strip() for value in expected_video_ids if str(value or "").strip()]
    if not expected:
        return {"verified": True, "actual_video_ids": []}
    last_payload: dict = {}
    for attempt in range(1, max(1, int(max_attempts or 1)) + 1):
        payload = ebay.get_inventory_item(
            access_token=access_token,
            sku=str(sku or "").strip(),
            content_language=content_language,
        )
        last_payload = payload if isinstance(payload, dict) else {}
        product_payload = last_payload.get("product") if isinstance(last_payload.get("product"), dict) else {}
        actual = [str(value or "").strip() for value in product_payload.get("videoIds") or [] if str(value or "").strip()]
        if all(video_id in actual for video_id in expected):
            return {"verified": True, "actual_video_ids": actual, "attempts": attempt}
        if attempt < max(1, int(max_attempts or 1)):
            time.sleep(max(0.0, float(sleep_seconds or 0.0)))
    product_payload = last_payload.get("product") if isinstance(last_payload.get("product"), dict) else {}
    actual = [str(value or "").strip() for value in product_payload.get("videoIds") or [] if str(value or "").strip()]
    raise RuntimeError(
        "eBay inventory item did not retain listing videoIds after update. "
        f"expected={expected}; actual={actual or []}"
    )


def _wizard_is_ebay_inventory_internal_error(exc: Exception) -> bool:
    text = str(exc or "")
    lowered = text.lower()
    return (
        "errorid\":25001" in lowered
        or "core inventory service internal error" in lowered
        or ("api_inventory" in lowered and "system error" in lowered)
    )


def _wizard_create_or_replace_inventory_item_with_fallback(
    *,
    ebay: EbayClient,
    access_token: str,
    sku: str,
    payload: dict,
    content_language: str,
    preserve_video_ids: bool = False,
) -> tuple[bool, str]:
    try:
        ebay.create_or_replace_inventory_item(
            access_token=access_token,
            sku=sku,
            payload=payload,
            content_language=content_language,
        )
        return False, ""
    except Exception as exc:
        if not _wizard_is_ebay_inventory_internal_error(exc):
            raise
        fallback_payload = dict(payload)
        if isinstance(fallback_payload.get("product"), dict):
            fallback_product = dict(fallback_payload["product"])
            if not preserve_video_ids:
                fallback_product.pop("videoIds", None)
            fallback_payload["product"] = fallback_product
        fallback_payload.pop("packageWeightAndSize", None)
        time.sleep(1.0)
        ebay.create_or_replace_inventory_item(
            access_token=access_token,
            sku=sku,
            payload=fallback_payload,
            content_language=content_language,
        )
        return True, str(exc)


def _wizard_verify_trading_listing_video_ids(
    *,
    ebay: EbayClient,
    access_token: str,
    listing_id: str,
    expected_video_ids: list[str],
    marketplace_id: str = "EBAY_US",
) -> dict:
    expected = [str(value or "").strip() for value in expected_video_ids if str(value or "").strip()]
    if not expected:
        return {"verified": True, "actual_video_ids": []}
    result = ebay.get_trading_item_video_ids(
        access_token=access_token,
        item_id=str(listing_id or "").strip(),
        marketplace_id=marketplace_id,
    )
    actual = [str(value or "").strip() for value in result.get("video_ids") or [] if str(value or "").strip()]
    if all(video_id in actual for video_id in expected):
        return {**result, "verified": True, "actual_video_ids": actual}
    raise RuntimeError(
        "Trading GetItem did not return the expected listing video ID. "
        f"listing_id={listing_id}; expected={expected}; actual={actual or []}"
    )


def _wizard_normalize_volume_pricing_tiers(
    raw_text: str,
    *,
    base_price: float = 0.0,
) -> tuple[list[dict[str, float | int]], list[str]]:
    text = str(raw_text or "").strip()
    if not text:
        return [], []
    try:
        parsed = json.loads(text)
    except Exception:
        return [], ["Volume pricing tiers must be valid JSON."]
    if not isinstance(parsed, list):
        return [], ["Volume pricing tiers must be a JSON array."]
    tiers: list[dict[str, float | int]] = []
    errors: list[str] = []
    for idx, row in enumerate(parsed, start=1):
        if not isinstance(row, dict):
            errors.append(f"Tier #{idx} must be an object with `min_qty` and `price`.")
            continue
        min_qty_raw = row.get("min_qty", row.get("qty"))
        price_raw = row.get("price", row.get("unit_price"))
        percent_raw = row.get("percent_off", row.get("discount_percent"))
        try:
            min_qty = int(float(min_qty_raw))
        except Exception:
            errors.append(f"Tier #{idx} has invalid min_qty.")
            continue
        price: float | None = None
        percent_off: float | None = None
        if price_raw is not None and str(price_raw).strip() != "":
            try:
                price = float(price_raw)
            except Exception:
                errors.append(f"Tier #{idx} has invalid price.")
                continue
        elif percent_raw is not None and str(percent_raw).strip() != "":
            try:
                percent_off = float(percent_raw)
            except Exception:
                errors.append(f"Tier #{idx} has invalid percent_off.")
                continue
            if percent_off <= 0 or percent_off >= 100:
                errors.append(f"Tier #{idx} percent_off must be > 0 and < 100.")
                continue
            if float(base_price or 0.0) <= 0:
                errors.append(
                    f"Tier #{idx} uses percent_off but base listing price is missing/invalid."
                )
                continue
            price = round(float(base_price) * (1.0 - (percent_off / 100.0)), 2)
        else:
            errors.append(f"Tier #{idx} must include either `price` or `percent_off`.")
            continue
        if min_qty < 2:
            errors.append(f"Tier #{idx} min_qty must be >= 2.")
            continue
        if float(price or 0.0) <= 0:
            errors.append(f"Tier #{idx} price must be > 0.")
            continue
        tier_row: dict[str, float | int] = {"min_qty": min_qty, "price": round(float(price), 2)}
        if percent_off is not None:
            tier_row["percent_off"] = round(float(percent_off), 2)
        tiers.append(tier_row)
    if errors:
        return [], errors
    tiers = sorted(tiers, key=lambda row: int(row["min_qty"]))
    seen: set[int] = set()
    deduped: list[dict[str, float | int]] = []
    for row in tiers:
        qty = int(row["min_qty"])
        if qty in seen:
            errors.append(f"Duplicate min_qty tier `{qty}` is not allowed.")
            continue
        seen.add(qty)
        deduped.append(row)
    if len(deduped) > 10:
        errors.append("Volume pricing supports at most 10 tiers.")
    if errors:
        return [], errors
    return deduped, []


def _wizard_volume_pricing_description_block(tiers: list[dict[str, float | int]]) -> str:
    rows = []
    for row in tiers:
        qty = int(row.get("min_qty") or 0)
        price = float(row.get("price") or 0.0)
        percent = float(row.get("percent_off") or 0.0)
        if qty >= 2 and price > 0:
            if percent > 0:
                rows.append(f"<li>Buy {qty}+ save {percent:g}% (${price:,.2f} each)</li>")
            else:
                rows.append(f"<li>Buy {qty}+ for ${price:,.2f} each</li>")
    if not rows:
        return ""
    return (
        "<h3>Volume Discount Pricing</h3>"
        "<p>Quantity discounts available:</p>"
        "<ul>"
        + "".join(rows)
        + "</ul>"
    )


def _wizard_volume_pricing_json_from_discount_controls(
    *,
    buy2_percent: float = 0.0,
    buy3_percent: float = 0.0,
    buy4_percent: float = 0.0,
) -> str:
    tiers: list[dict[str, float | int]] = []
    for qty, percent in (
        (2, float(buy2_percent or 0.0)),
        (3, float(buy3_percent or 0.0)),
        (4, float(buy4_percent or 0.0)),
    ):
        if percent > 0:
            tiers.append({"min_qty": qty, "percent_off": round(percent, 2)})
    return json.dumps(tiers, indent=2) if tiers else ""


def _wizard_volume_pricing_discount_controls_from_tiers(
    tiers: list[dict[str, float | int]] | None,
) -> tuple[float, float, float]:
    buy2 = 0.0
    buy3 = 0.0
    buy4 = 0.0
    for row in tiers or []:
        try:
            qty = int(float((row or {}).get("min_qty") or 0))
            percent = float((row or {}).get("percent_off") or 0.0)
        except Exception:
            continue
        if percent <= 0:
            continue
        if qty == 2:
            buy2 = percent
        elif qty == 3:
            buy3 = percent
        elif qty == 4:
            buy4 = percent
    return (buy2, buy3, buy4)


def _wizard_external_listing_id_owner(
    repo: InventoryRepository,
    *,
    marketplace: str,
    external_listing_id: str,
    exclude_listing_id: int,
) -> int | None:
    ext_id = str(external_listing_id or "").strip()
    if not ext_id:
        return None
    return repo.find_listing_owner_by_external_id(
        marketplace=marketplace,
        external_listing_id=ext_id,
        exclude_listing_id=int(exclude_listing_id),
    )


def _wizard_maybe_add_package_data(payload: dict, product) -> None:
    try:
        weight = float(
            st.session_state.get("listing_wizard_package_weight_oz")
            or getattr(product, "package_weight_oz", 0.0)
            or 0.0
        )
    except Exception:
        weight = 0.0
    try:
        length = float(
            st.session_state.get("listing_wizard_package_length_in")
            or getattr(product, "package_length_in", 0.0)
            or 0.0
        )
    except Exception:
        length = 0.0
    try:
        width = float(
            st.session_state.get("listing_wizard_package_width_in")
            or getattr(product, "package_width_in", 0.0)
            or 0.0
        )
    except Exception:
        width = 0.0
    try:
        height = float(
            st.session_state.get("listing_wizard_package_height_in")
            or getattr(product, "package_height_in", 0.0)
            or 0.0
        )
    except Exception:
        height = 0.0

    if weight <= 0 and (length <= 0 or width <= 0 or height <= 0):
        return

    package: dict = {}
    if weight > 0:
        package["weight"] = {"value": weight, "unit": "OUNCE"}
    if length > 0 and width > 0 and height > 0:
        package["dimensions"] = {
            "length": length,
            "width": width,
            "height": height,
            "unit": "INCH",
        }
    if package:
        payload["packageWeightAndSize"] = package


def _collect_uploaded_media_context(uploaded_files) -> tuple[list[tuple[bytes, str]], list[dict]]:
    uploaded_images: list[tuple[bytes, str]] = []
    uploaded_videos: list[dict] = []
    for f in uploaded_files or []:
        ctype = str(getattr(f, "type", "") or "").strip().lower()
        fname = str(getattr(f, "name", "") or "").strip()
        payload = _uploaded_file_bytes(f)
        if ctype.startswith("image/"):
            resolved_type = _resolve_vision_mime(payload, ctype or "image/jpeg")
            if resolved_type:
                uploaded_images.append((payload, resolved_type))
            continue
        if ctype.startswith("video/"):
            uploaded_videos.append(
                {
                    "filename": fname,
                    "content_type": ctype,
                    "size_bytes": len(payload or b""),
                }
            )
    return uploaded_images, uploaded_videos


def _load_product_media_context(repo: InventoryRepository, storage: MediaStorageService, product_id: int, limit: int = 3):
    rows = repo.list_media_assets_for_product(int(product_id))
    image_rows = [row for row in rows if str(row.media_type or "").strip().lower() == "image"]
    video_rows = [row for row in rows if str(row.media_type or "").strip().lower() == "video"]
    images: list[tuple[bytes, str]] = []
    for row in image_rows[: max(1, int(limit))]:
        content_type = str(row.content_type or "image/jpeg").strip() or "image/jpeg"
        try:
            if storage is not None and storage.enabled and row.s3_bucket and row.s3_key:
                blob, resolved_ct = storage.get_object_bytes(row.s3_bucket, row.s3_key)
                resolved_content_type = resolved_ct or content_type
                normalized_mime = _resolve_vision_mime(blob, resolved_content_type)
                if normalized_mime:
                    images.append((blob, normalized_mime))
                continue
        except Exception:
            pass
        if row.s3_url:
            try:
                response = requests.get(str(row.s3_url), timeout=20)
                response.raise_for_status()
                resolved_ct = response.headers.get("Content-Type") or content_type
                normalized_mime = _resolve_vision_mime(response.content, resolved_ct)
                if normalized_mime:
                    images.append((response.content, normalized_mime))
            except Exception:
                continue
    videos: list[dict] = []
    for row in video_rows:
        videos.append(
            {
                "id": int(row.id),
                "filename": str(row.original_filename or "").strip(),
                "content_type": str(row.content_type or "").strip(),
                "size_bytes": int(row.size_bytes or 0),
                "url": str(row.s3_url or "").strip(),
            }
        )
    return {"images": images, "videos": videos, "image_count": len(image_rows), "video_count": len(video_rows)}


def _load_selected_product_media_context(
    repo: InventoryRepository,
    storage: MediaStorageService,
    product_id: int,
    selected_media_ids: list[int],
    limit: int = 3,
):
    rows = repo.list_media_assets_for_product(int(product_id))
    selected_set = {int(v) for v in (selected_media_ids or [])}
    filtered_rows = [row for row in rows if int(row.id) in selected_set]
    image_rows = [row for row in filtered_rows if str(row.media_type or "").strip().lower() == "image"]
    video_rows = [row for row in filtered_rows if str(row.media_type or "").strip().lower() == "video"]

    images: list[tuple[bytes, str]] = []
    for row in image_rows[: max(1, int(limit))]:
        content_type = str(row.content_type or "image/jpeg").strip() or "image/jpeg"
        try:
            if storage is not None and storage.enabled and row.s3_bucket and row.s3_key:
                blob, resolved_ct = storage.get_object_bytes(row.s3_bucket, row.s3_key)
                resolved_content_type = resolved_ct or content_type
                normalized_mime = _resolve_vision_mime(blob, resolved_content_type)
                if normalized_mime:
                    images.append((blob, normalized_mime))
                continue
        except Exception:
            pass
        if row.s3_url:
            try:
                response = requests.get(str(row.s3_url), timeout=20)
                response.raise_for_status()
                resolved_ct = response.headers.get("Content-Type") or content_type
                normalized_mime = _resolve_vision_mime(response.content, resolved_ct)
                if normalized_mime:
                    images.append((response.content, normalized_mime))
            except Exception:
                continue

    videos: list[dict] = []
    for row in video_rows:
        videos.append(
            {
                "id": int(row.id),
                "filename": str(row.original_filename or "").strip(),
                "content_type": str(row.content_type or "").strip(),
                "size_bytes": int(row.size_bytes or 0),
                "url": str(row.s3_url or "").strip(),
            }
        )
    return {"images": images, "videos": videos, "image_count": len(image_rows), "video_count": len(video_rows)}


def _listing_mode_defaults(mode: str) -> dict:
    resolved = str(mode or "buy_it_now").strip().lower()
    if resolved == "store_30":
        return {
            "format_type": "FIXED_PRICE",
            "listing_duration": "DAYS_30",
            "auction_start_price": 0.0,
            "auction_reserve_price": 0.0,
            "auction_buy_now_price": 0.0,
        }
    if resolved == "auction":
        return {
            "format_type": "AUCTION",
            "listing_duration": "DAYS_5",
            "auction_start_price": 1.0,
            "auction_reserve_price": 0.0,
            "auction_buy_now_price": 0.0,
        }
    if resolved == "auction_plus_bin":
        return {
            "format_type": "AUCTION",
            "listing_duration": "DAYS_5",
            "auction_start_price": 1.0,
            "auction_reserve_price": 0.0,
            "auction_buy_now_price": 1.0,
        }
    return {
        "format_type": "FIXED_PRICE",
        "listing_duration": "GTC",
        "auction_start_price": 0.0,
        "auction_reserve_price": 0.0,
        "auction_buy_now_price": 0.0,
    }


def _build_wizard_risk_summary(
    *,
    ai_risk_summary: str,
    preflight,
    format_type: str,
    offers_enabled: bool,
) -> dict:
    blockers = list(getattr(preflight, "blockers", []) or [])
    warnings = list(getattr(preflight, "warnings", []) or [])
    score = int(getattr(preflight, "score", 0) or 0)
    format_resolved = str(format_type or "").strip().upper()

    level = "low"
    if blockers or score < 70:
        level = "high"
    elif warnings or score < 85 or format_resolved == "AUCTION" or offers_enabled:
        level = "medium"

    highlights: list[str] = []
    if blockers:
        highlights.append(f"{len(blockers)} blocker(s) must be resolved before publish.")
    if warnings:
        highlights.append(f"{len(warnings)} warning(s) should be reviewed before publish.")
    if format_resolved == "AUCTION":
        highlights.append("Auction mode needs tighter pricing and duration validation.")
    if offers_enabled:
        highlights.append("Best-offer thresholds should be checked against floor margin.")
    if ai_risk_summary:
        highlights.append(f"AI risk note: {ai_risk_summary}")
    if not highlights:
        highlights.append("No major publish risks detected in current wizard state.")

    return {
        "level": level,
        "score": score,
        "highlights": highlights,
        "ai_risk_summary": ai_risk_summary,
    }


def _handoff_to_listings_review(*, listing_title: str, sku: str) -> None:
    st.session_state["listings_filter_query"] = str(sku or listing_title or "").strip()
    st.session_state["listings_filter_marketplaces"] = ["ebay"]
    st.session_state["listings_filter_status"] = ["draft"]
    st.session_state["listings_filter_origin"] = "all"
    st.session_state["listings_readiness_filter"] = "blocked"
    st.session_state["listings_readiness_format_filter"] = "all"
    st.session_state["listings_readiness_blocker_reason_filter"] = "all"
    st.session_state["listings_readiness_warning_reason_filter"] = "all"


def _wizard_set_text_fields(
    *,
    title: str,
    details: str,
    price: float | None = None,
    condition_description: str | None = None,
) -> None:
    st.session_state["listing_wizard_title"] = str(title or "").strip()
    st.session_state["listing_wizard_details"] = str(details or "").strip()
    if price is not None:
        try:
            st.session_state["listing_wizard_price"] = float(price)
        except Exception:
            pass
    if condition_description is not None:
        st.session_state["listing_wizard_condition_description"] = str(condition_description or "").strip()


def _wizard_reset_inputs_for_context(
    *,
    default_title: str,
    default_details: str,
    default_price: float,
    default_condition_description: str = "",
) -> None:
    st.session_state["listing_wizard_title"] = str(default_title or "").strip()
    st.session_state["listing_wizard_details"] = str(default_details or "").strip()
    st.session_state["listing_wizard_price"] = float(default_price or 0.0)
    st.session_state["listing_wizard_mode"] = "buy_it_now"
    st.session_state["listing_wizard_auction_duration"] = "DAYS_5"
    st.session_state["listing_wizard_auction_start"] = 1.0
    st.session_state["listing_wizard_auction_reserve"] = 0.0
    st.session_state["listing_wizard_auction_bin"] = 0.0
    st.session_state["listing_wizard_offer_enabled"] = False
    st.session_state["listing_wizard_offer_auto_accept"] = 0.0
    st.session_state["listing_wizard_offer_minimum"] = 0.0
    st.session_state["listing_wizard_volume_pricing_json"] = ""
    st.session_state["listing_wizard_volume_discount_buy2"] = 0.0
    st.session_state["listing_wizard_volume_discount_buy3"] = 0.0
    st.session_state["listing_wizard_volume_discount_buy4"] = 0.0
    st.session_state["listing_wizard_include_volume_pricing_in_details"] = False
    st.session_state["listing_wizard_subtitle"] = ""
    st.session_state["listing_wizard_condition_description"] = str(default_condition_description or "").strip()
    st.session_state["listing_wizard_aspects_json"] = ""
    st.session_state.pop("listing_wizard_ai_suggestions", None)
    st.session_state.pop("listing_wizard_ai_diagnostics", None)
    st.session_state.pop("listing_wizard_ai_acceptance", None)
    st.session_state.pop("listing_wizard_ai_comp_evidence", None)
    st.session_state.pop("listing_wizard_ai_has_run", None)
    st.session_state.pop("listing_wizard_ai_show_debug_panels", None)


def _wizard_trim_title_to_ebay_max() -> None:
    raw = str(st.session_state.get("listing_wizard_title") or "").strip()
    if not raw:
        return
    if len(raw) <= EBAY_TITLE_MAX_CHARS:
        return
    st.session_state["listing_wizard_title"] = raw[:EBAY_TITLE_MAX_CHARS].rstrip(" -_,.;:")


def _wizard_append_details_text(block_text: str) -> None:
    block = str(block_text or "").strip()
    if not block:
        return
    existing = str(st.session_state.get("listing_wizard_details") or "").strip()
    if block in existing:
        return
    if existing:
        st.session_state["listing_wizard_details"] = f"{existing}\n\n{block}"
    else:
        st.session_state["listing_wizard_details"] = block


def _wizard_estimated_media_count(
    repo: InventoryRepository,
    product_id: int | None,
    uploaded_files,
) -> int:
    existing = 0
    if product_id:
        rows = repo.list_media_assets_for_product(int(product_id))
        existing = len([r for r in rows if str(r.media_type or "").strip().lower() in {"image", "video"}])
    uploads = len(uploaded_files or [])
    return int(existing + uploads)


def _wizard_apply_pending_field_updates() -> None:
    pending = st.session_state.pop("listing_wizard_pending_field_updates", None)
    if not isinstance(pending, dict):
        return
    allowed_keys = {
        "listing_wizard_title",
        "listing_wizard_details",
        "listing_wizard_price",
        "listing_wizard_offer_enabled",
        "listing_wizard_offer_auto_accept",
        "listing_wizard_offer_minimum",
        "listing_wizard_volume_pricing_json",
        "listing_wizard_category_id",
        "listing_wizard_subtitle",
        "listing_wizard_condition_description",
        "listing_wizard_aspects_json",
    }
    deferred_updates: dict[str, object] = {}
    for key, value in pending.items():
        if key in allowed_keys:
            try:
                st.session_state[key] = value
            except StreamlitAPIException:
                # Streamlit can reject widget-key writes in the same rerun after
                # widget instantiation; defer and retry on next rerun.
                deferred_updates[key] = value
    if deferred_updates:
        st.session_state["listing_wizard_pending_field_updates"] = _wizard_merge_pending_field_updates(
            st.session_state.get("listing_wizard_pending_field_updates"),
            deferred_updates,
        )
        prior_flash = str(st.session_state.get("listing_wizard_apply_flash") or "").strip()
        deferred_msg = "Some field updates were deferred and will apply on the next rerun."
        st.session_state["listing_wizard_apply_flash"] = (
            f"{prior_flash} {deferred_msg}".strip() if prior_flash else deferred_msg
        )


def _wizard_merge_pending_field_updates(current: dict | None, updates: dict | None) -> dict:
    merged: dict = {}
    if isinstance(current, dict):
        merged.update(current)
    if isinstance(updates, dict):
        for key, value in updates.items():
            resolved = str(key or "").strip()
            if resolved:
                merged[resolved] = value
    return merged


def _wizard_queue_pending_field_updates(updates: dict | None) -> None:
    st.session_state["listing_wizard_pending_field_updates"] = _wizard_merge_pending_field_updates(
        st.session_state.get("listing_wizard_pending_field_updates"),
        updates,
    )


def _existing_ebay_listings_for_product(repo: InventoryRepository, product_id: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for listing in repo.list_listings_for_product(int(product_id), marketplace="ebay", limit=50):
        rows.append(
            {
                "id": int(listing.id),
                "status": str(listing.listing_status or "").strip().lower(),
                "title": str(listing.listing_title or "").strip(),
                "external_listing_id": str(listing.external_listing_id or "").strip(),
                "url": str(listing.marketplace_url or "").strip(),
                "created_at": str(listing.created_at or ""),
            }
        )
    return rows


def _safe_table_df(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows or [])
    if frame.empty:
        return frame
    for col in frame.columns:
        if frame[col].dtype == "object":
            frame[col] = frame[col].map(
                lambda v: json.dumps(v, ensure_ascii=True, default=str)
                if isinstance(v, (dict, list, tuple, set))
                else ("" if v is None else str(v))
            )
    return frame


def _wizard_category_options(
    *,
    repo: InventoryRepository,
    username: str,
    runtime_category_id: str,
    cached_suggestions: list[dict],
) -> tuple[list[str], dict[str, str]]:
    labels: list[str] = []
    label_to_id: dict[str, str] = {}

    def _add_option(category_id: str, label: str) -> None:
        resolved_id = str(category_id or "").strip()
        resolved_label = str(label or "").strip()
        if not resolved_id or not resolved_label:
            return
        if resolved_label in label_to_id:
            return
        labels.append(resolved_label)
        label_to_id[resolved_label] = resolved_id

    if runtime_category_id:
        _add_option(runtime_category_id, f"{runtime_category_id} - Runtime Default")

    try:
        presets = repo.list_ebay_publish_presets(
            environment=settings.app_env,
            username=username,
            active_only=True,
        )
    except Exception:
        presets = []
    for row in presets:
        category_id = str(getattr(row, "category_id", "") or "").strip()
        if not category_id:
            continue
        preset_name = str(getattr(row, "name", "") or "").strip() or f"Preset #{int(getattr(row, 'id', 0) or 0)}"
        default_tag = " (default)" if bool(getattr(row, "is_default", False)) else ""
        _add_option(category_id, f"{category_id} - Preset: {preset_name}{default_tag}")

    for row in cached_suggestions:
        if not isinstance(row, dict):
            continue
        category_id = str(row.get("category_id") or "").strip()
        category_name = str(row.get("category_name") or "").strip()
        path = str(row.get("path") or "").strip()
        label = (
            f"{category_id} - {path}"
            if path
            else (f"{category_id} - {category_name}" if category_name else f"{category_id} - Suggested")
        )
        _add_option(category_id, label)

    return labels, label_to_id


def _wizard_shipping_profiles_key(username: str) -> str:
    return f"listing_wizard_shipping_profiles_json__{str(username or '').strip().lower()}"


def _wizard_shipping_default_key(username: str) -> str:
    return f"listing_wizard_shipping_profile_default__{str(username or '').strip().lower()}"


def _load_wizard_shipping_profiles(repo: InventoryRepository, username: str) -> dict[str, dict]:
    raw = str(get_runtime_str(repo, _wizard_shipping_profiles_key(username), "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in parsed.items():
        name = str(key or "").strip()
        if not name or not isinstance(value, dict):
            continue
        out[name] = {
            "shipping_service": str(value.get("shipping_service") or "").strip(),
            "handling_days": int(value.get("handling_days") or 0),
            "shipping_cost": float(value.get("shipping_cost") or 0.0),
            "package_weight_oz": float(value.get("package_weight_oz") or 0.0),
            "package_length_in": float(value.get("package_length_in") or 0.0),
            "package_width_in": float(value.get("package_width_in") or 0.0),
            "package_height_in": float(value.get("package_height_in") or 0.0),
        }
    return out


def _save_wizard_shipping_profiles(
    repo: InventoryRepository,
    *,
    username: str,
    actor: str,
    profiles: dict[str, dict],
) -> None:
    normalized: dict[str, dict] = {}
    for key, value in (profiles or {}).items():
        name = str(key or "").strip()
        if not name or not isinstance(value, dict):
            continue
        normalized[name] = {
            "shipping_service": str(value.get("shipping_service") or "").strip(),
            "handling_days": int(value.get("handling_days") or 0),
            "shipping_cost": float(value.get("shipping_cost") or 0.0),
            "package_weight_oz": float(value.get("package_weight_oz") or 0.0),
            "package_length_in": float(value.get("package_length_in") or 0.0),
            "package_width_in": float(value.get("package_width_in") or 0.0),
            "package_height_in": float(value.get("package_height_in") or 0.0),
        }
    repo.upsert_runtime_setting(
        key=_wizard_shipping_profiles_key(username),
        value=json.dumps(normalized, ensure_ascii=True, sort_keys=True),
        value_type="json",
        environment=settings.app_env,
        description="Listing Wizard shipping profile presets (per user).",
        is_active=True,
        actor=actor,
    )


def _set_wizard_shipping_default_profile(
    repo: InventoryRepository,
    *,
    username: str,
    actor: str,
    profile_name: str,
) -> None:
    repo.upsert_runtime_setting(
        key=_wizard_shipping_default_key(username),
        value=str(profile_name or "").strip(),
        value_type="str",
        environment=settings.app_env,
        description="Default Listing Wizard shipping profile name (per user).",
        is_active=True,
        actor=actor,
    )


def _wizard_ebay_post_profiles_key(username: str) -> str:
    return f"listing_wizard_ebay_post_profiles_json__{str(username or '').strip().lower()}"


def _wizard_ebay_post_default_key(username: str) -> str:
    return f"listing_wizard_ebay_post_profile_default__{str(username or '').strip().lower()}"


def _load_wizard_ebay_post_profiles(repo: InventoryRepository, username: str) -> dict[str, dict]:
    raw = str(get_runtime_str(repo, _wizard_ebay_post_profiles_key(username), "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in parsed.items():
        name = str(key or "").strip()
        if not name or not isinstance(value, dict):
            continue
        out[name] = {
            "merchant_location_key": str(value.get("merchant_location_key") or "").strip(),
            "payment_policy_id": str(value.get("payment_policy_id") or "").strip(),
            "fulfillment_policy_id": str(value.get("fulfillment_policy_id") or "").strip(),
            "return_policy_id": str(value.get("return_policy_id") or "").strip(),
            "marketplace_id": str(value.get("marketplace_id") or "EBAY_US").strip() or "EBAY_US",
            "currency": str(value.get("currency") or "USD").strip() or "USD",
            "content_language": str(value.get("content_language") or "en-US").strip() or "en-US",
            "condition": str(value.get("condition") or "NEW").strip().upper() or "NEW",
            "use_eps_images": bool(value.get("use_eps_images")),
            "category_id": str(value.get("category_id") or "").strip(),
        }
    return out


def _save_wizard_ebay_post_profiles(
    repo: InventoryRepository,
    *,
    username: str,
    actor: str,
    profiles: dict[str, dict],
) -> None:
    normalized: dict[str, dict] = {}
    for key, value in (profiles or {}).items():
        name = str(key or "").strip()
        if not name or not isinstance(value, dict):
            continue
        normalized[name] = {
            "merchant_location_key": str(value.get("merchant_location_key") or "").strip(),
            "payment_policy_id": str(value.get("payment_policy_id") or "").strip(),
            "fulfillment_policy_id": str(value.get("fulfillment_policy_id") or "").strip(),
            "return_policy_id": str(value.get("return_policy_id") or "").strip(),
            "marketplace_id": str(value.get("marketplace_id") or "EBAY_US").strip() or "EBAY_US",
            "currency": str(value.get("currency") or "USD").strip() or "USD",
            "content_language": str(value.get("content_language") or "en-US").strip() or "en-US",
            "condition": str(value.get("condition") or "NEW").strip().upper() or "NEW",
            "use_eps_images": bool(value.get("use_eps_images")),
            "category_id": str(value.get("category_id") or "").strip(),
        }
    repo.upsert_runtime_setting(
        key=_wizard_ebay_post_profiles_key(username),
        value=json.dumps(normalized, ensure_ascii=True, sort_keys=True),
        value_type="json",
        environment=settings.app_env,
        description="Listing Wizard eBay direct-post profiles (per user).",
        is_active=True,
        actor=actor,
    )


def _set_wizard_ebay_post_default_profile(
    repo: InventoryRepository,
    *,
    username: str,
    actor: str,
    profile_name: str,
) -> None:
    repo.upsert_runtime_setting(
        key=_wizard_ebay_post_default_key(username),
        value=str(profile_name or "").strip(),
        value_type="str",
        environment=settings.app_env,
        description="Default Listing Wizard eBay direct-post profile (per user).",
        is_active=True,
        actor=actor,
    )


def render_listing_wizard(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    if not user:
        st.warning("Sign in required.")
        return

    st.markdown("## Listing Wizard")
    st.caption("Product -> Template -> Policy/Format -> Review -> Publish")
    st.caption(
        "Recommended flow: 1) Product  2) Template  3) Mode/Pricing  4) Offers  5) Media  "
        "6) AI Assist (optional)  7) Preflight  8) Preview  9) Create Draft"
    )
    quality_policy = load_ai_quality_policy(repo)
    product_media_rows_cache: dict[int, list[object]] = {}

    def _product_media_rows_cached(product_id: int) -> list[object]:
        resolved_product_id = int(product_id or 0)
        if resolved_product_id <= 0:
            return []
        if resolved_product_id not in product_media_rows_cache:
            product_media_rows_cache[resolved_product_id] = list(
                repo.list_media_assets_for_product(resolved_product_id)
            )
        return list(product_media_rows_cache.get(resolved_product_id) or [])

    pending_resume_payload = st.session_state.pop("listing_wizard_resume_payload", None)
    if isinstance(pending_resume_payload, dict):
        pending_parsed = extract_listing_draft_payload(
            pending_resume_payload,
            state_keys=LISTING_WIZARD_DRAFT_SESSION_KEYS,
            context_keys=["seed_signature", "seed_title", "seed_details"],
        )
        pending_state = pending_parsed.get("state") if isinstance(pending_parsed, dict) else {}
        pending_context = pending_parsed.get("context") if isinstance(pending_parsed, dict) else {}
        _wizard_apply_draft_payload_to_session(pending_resume_payload)
        st.session_state["listing_wizard_resume_applied_once"] = True
        st.session_state["listing_wizard_apply_flash"] = "Resumed saved workflow draft."
        st.session_state["listing_wizard_seed_signature"] = str(
            (pending_state or {}).get("seed_signature")
            or (pending_context or {}).get("seed_signature")
            or pending_resume_payload.get("seed_signature")
            or ""
        ).strip()
        st.session_state["listing_wizard_seed_title"] = str(
            (pending_state or {}).get("seed_title")
            or (pending_context or {}).get("seed_title")
            or pending_resume_payload.get("seed_title")
            or ""
        ).strip()
        st.session_state["listing_wizard_seed_details"] = str(
            (pending_state or {}).get("seed_details")
            or (pending_context or {}).get("seed_details")
            or pending_resume_payload.get("seed_details")
            or ""
        ).strip()

    saved_draft = repo.load_workflow_draft(
        environment=settings.app_env,
        workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
        username=user.username,
        scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
        active_only=True,
    )
    saved_payload: dict = {}
    if saved_draft is not None:
        try:
            parsed = json.loads(str(saved_draft.draft_json or "{}"))
            if isinstance(parsed, dict):
                saved_payload = parsed
        except Exception:
            saved_payload = {}
    saved_parsed_payload = extract_listing_draft_payload(
        saved_payload,
        state_keys=LISTING_WIZARD_DRAFT_SESSION_KEYS,
        context_keys=["selected_product_id", "selected_template_id"],
    )
    saved_context = (
        saved_parsed_payload.get("context")
        if isinstance(saved_parsed_payload, dict) and isinstance(saved_parsed_payload.get("context"), dict)
        else {}
    )
    if saved_draft is not None:
        draft_updated = str(getattr(saved_draft, "updated_at", "") or "").strip()
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            st.caption(
                "Saved draft available"
                + (f" (last updated {draft_updated})" if draft_updated else "")
            )
        with c2:
            if st.button("Resume Saved Draft", key="listing_wizard_resume_saved_draft_btn"):
                resumed = repo.resume_latest_workflow_draft(
                    environment=settings.app_env,
                    workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                    username=user.username,
                    active_only=True,
                )
                resumed_payload: dict = {}
                if resumed is not None:
                    try:
                        parsed = json.loads(str(resumed.draft_json or "{}"))
                        if isinstance(parsed, dict):
                            resumed_payload = parsed
                    except Exception:
                        resumed_payload = {}
                st.session_state["listing_wizard_resume_payload"] = resumed_payload or saved_payload
                repo.append_workflow_event(
                    environment=settings.app_env,
                    workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                    username=user.username,
                    scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                    action="resume_draft",
                    status="ok",
                    message="Operator resumed saved draft.",
                    payload={"draft_id": int(getattr(resumed, "id", 0) or getattr(saved_draft, "id", 0) or 0)},
                    draft_id=int(getattr(resumed, "id", 0) or getattr(saved_draft, "id", 0) or 0),
                    actor=user.username,
                )
                st.rerun()
        with c3:
            if st.button("Reset Saved Draft", key="listing_wizard_reset_saved_draft_btn"):
                repo.clear_workflow_draft(
                    environment=settings.app_env,
                    workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                    username=user.username,
                    scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                    actor=user.username,
                    reason="operator_reset",
                )
                st.session_state["listing_wizard_resume_applied_once"] = False
                st.session_state["listing_wizard_apply_flash"] = "Cleared saved workflow draft."
                st.rerun()

    _render_listing_wizard_business_room_handoffs(repo, username=user.username)

    st.markdown("### Step 1 of 9: Select Product")
    force_restore_from_resume = bool(st.session_state.pop("listing_wizard_resume_applied_once", False))
    saved_product_id = int(saved_context.get("selected_product_id") or saved_payload.get("selected_product_id") or 0)
    current_product_id = _parse_product_id(st.session_state.get("listing_wizard_product"))
    selected_product_id_for_load = int(current_product_id or saved_product_id or 0) or None
    recent_product_limit = max(
        10,
        min(250, int(get_runtime_int(repo, "listing_wizard_recent_product_limit", 75))),
    )
    if "listing_wizard_product_search" not in st.session_state:
        st.session_state["listing_wizard_product_search"] = ""
    product_search_query = st.text_input(
        "Search Products",
        key="listing_wizard_product_search",
        placeholder="Search by SKU, title, category, material, or product ID",
    )
    products = _load_listing_wizard_product_rows(
        repo,
        search_query=product_search_query,
        selected_product_id=selected_product_id_for_load,
        recent_limit=recent_product_limit,
        search_limit=100,
    )
    if not products:
        st.info("Create at least one product before using Listing Wizard.")
        return
    product_by_id = {int(p.id): p for p in products}
    product_options = build_product_options(products, include_none=False, include_id=True)
    restored_product_label = _wizard_option_for_product_id(product_options, saved_product_id)
    if force_restore_from_resume and restored_product_label:
        st.session_state["listing_wizard_product"] = restored_product_label
    elif "listing_wizard_product" not in st.session_state and restored_product_label:
        st.session_state["listing_wizard_product"] = restored_product_label
    elif st.session_state.get("listing_wizard_product") not in product_options and product_options:
        current_label = _wizard_option_for_product_id(product_options, current_product_id)
        st.session_state["listing_wizard_product"] = current_label or next(iter(product_options.keys()))
    st.caption(
        f"Dropdown is capped to the most recent {recent_product_limit} product(s)"
        + (" plus search matches." if str(product_search_query or "").strip() else ".")
    )
    selected_product_option = st.selectbox(
        "Product",
        options=list(product_options.keys()),
        key="listing_wizard_product",
    )
    product_id = product_options.get(selected_product_option)
    if product_id is None:
        product_id = _parse_product_id(selected_product_option)
    selected_product = product_by_id.get(int(product_id)) if product_id else None
    has_existing_conflict = False
    allow_duplicate_listing = False
    if selected_product is not None:
        existing_ebay = _existing_ebay_listings_for_product(repo, int(selected_product.id))
        draft_or_active = [
            row
            for row in existing_ebay
            if str(row.get("status") or "") in {"draft", "active", "published", "reviewed"}
        ]
        if draft_or_active:
            has_existing_conflict = True
            st.warning(
                f"This product already has {len(draft_or_active)} draft/active eBay listing(s). "
                "Review existing rows before creating another."
            )
            allow_duplicate_listing = st.checkbox(
                "Allow duplicate draft creation for this product",
                value=False,
                key="listing_wizard_allow_duplicate_listing",
                help="Enable only when you intentionally need another listing for the same product.",
            )
            with st.expander("Existing eBay Listings For This Product", expanded=False):
                st.dataframe(_safe_table_df(draft_or_active), use_container_width=True, hide_index=True)
                if st.button("Open Listings Review Queue", key="listing_wizard_existing_open_listings_btn"):
                    _handoff_to_listings_review(
                        listing_title=str(selected_product.title or "").strip(),
                        sku=str(selected_product.sku or "").strip(),
                    )
                    safe_switch_page(
                        "pages/03_Listings.py",
                        error_prefix="Open Listings failed",
                        info_message="Open Listings from the sidebar.",
                    )

    st.markdown("### Step 2 of 9: Apply Template (Optional)")
    template_rows = repo.list_ebay_listing_template_profiles(
        environment=settings.app_env,
        username=user.username,
        include_shared=True,
        active_only=True,
    )
    template_lookup: dict[str, object] = {"None": None}
    for row in template_rows:
        label = f"{row.name} [{'Shared' if bool(row.is_shared) else 'Mine'}]"
        if label in template_lookup:
            label = f"{label} #{row.id}"
        template_lookup[label] = row
    restored_template_label = _wizard_option_for_template_id(
        template_lookup,
        int(saved_context.get("selected_template_id") or saved_payload.get("selected_template_id") or 0),
    )
    if force_restore_from_resume and restored_template_label:
        st.session_state["listing_wizard_template"] = restored_template_label
    elif "listing_wizard_template" not in st.session_state and restored_template_label:
        st.session_state["listing_wizard_template"] = restored_template_label
    selected_template_label = st.selectbox(
        "Template",
        options=list(template_lookup.keys()),
        key="listing_wizard_template",
    )
    selected_template_row = template_lookup.get(selected_template_label)
    selected_template_id = int(getattr(selected_template_row, "id", 0) or 0) if selected_template_row is not None else 0
    selected_product_id = int(product_id or 0)

    st.markdown("### Workflow Draft")
    wd1, wd2, wd3 = st.columns([1, 1, 2])
    with wd1:
        if st.button("Save Workflow Draft", key="listing_wizard_save_workflow_draft_btn"):
            payload = _wizard_build_draft_payload(
                selected_product_id=selected_product_id,
                selected_template_id=selected_template_id,
            )
            row = repo.save_workflow_draft(
                environment=settings.app_env,
                workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                username=user.username,
                scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                draft_payload=payload,
                status="active",
                last_step="step9",
                actor=user.username,
            )
            repo.append_workflow_event(
                environment=settings.app_env,
                workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                username=user.username,
                scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                action="save_draft",
                status="ok",
                message="Operator manually saved wizard draft.",
                payload={"draft_id": int(row.id)},
                draft_id=int(row.id),
                actor=user.username,
            )
            st.session_state["listing_wizard_last_autosave_signature"] = _wizard_draft_signature(payload)
            st.session_state["listing_wizard_last_autosave_at"] = utcnow_naive().isoformat()
            st.session_state["listing_wizard_last_draft_id"] = int(row.id)
            st.success("Workflow draft saved.")
    with wd2:
        if st.button("Clear Workflow Draft", key="listing_wizard_clear_workflow_draft_btn"):
            row = repo.load_workflow_draft(
                environment=settings.app_env,
                workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                username=user.username,
                scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                active_only=True,
            )
            cleared = repo.clear_workflow_draft(
                environment=settings.app_env,
                workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                username=user.username,
                scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                actor=user.username,
                reason="operator_clear_workflow",
            )
            if cleared:
                repo.append_workflow_event(
                    environment=settings.app_env,
                    workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
                    username=user.username,
                    scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
                    action="clear_draft",
                    status="ok",
                    message="Operator cleared workflow draft.",
                    payload={"draft_id": int(getattr(row, "id", 0) or 0)},
                    draft_id=int(getattr(row, "id", 0) or 0),
                    actor=user.username,
                )
            _wizard_clear_local_draft_state()
            st.rerun()
    with wd3:
        last_autosave_at = str(st.session_state.get("listing_wizard_last_autosave_at") or "").strip()
        if last_autosave_at:
            st.caption(f"Last autosave: {last_autosave_at}")
        elif saved_draft is not None:
            st.caption("Draft loaded from DB. Autosave will update on next state change.")
        else:
            st.caption("No draft autosave yet in this session.")

    product_title_default = str(getattr(selected_product, "title", "") or "").strip() if selected_product is not None else ""
    product_details_default = (
        str(getattr(selected_product, "description", "") or "").strip() if selected_product is not None else ""
    )
    product_condition_description_default = _product_ai_grading_description(selected_product)
    product_details_default = _with_ai_grading_notes(
        product_details_default,
        grading_description=product_condition_description_default,
    )
    default_title = product_title_default
    default_details = product_details_default
    default_condition_description = product_condition_description_default
    default_price = 0.0
    runtime_category_id = str(get_runtime_str(repo, "ebay_category_id", "") or "").strip()
    template_row = selected_template_row
    if template_row is not None:
        template_title = str(template_row.listing_title_template or "").strip()
        template_details = str(template_row.marketplace_details_template or "").strip()
        default_title = template_title or product_title_default
        default_details = template_details or product_details_default
        default_price = float(template_row.listing_price_default or 0.0)

    st.markdown("### Wizard Status")
    ws1, ws2, ws3, ws4 = st.columns(4)
    ws1.metric(
        "Product",
        (
            str(getattr(selected_product, "sku", "") or "").strip()
            or f"#{int(getattr(selected_product, 'id', 0) or 0)}"
            if selected_product is not None
            else "-"
        ),
    )
    ws2.metric("Template", "None" if template_row is None else str(getattr(template_row, "name", "") or "-"))
    ws3.metric(
        "Mode",
        {
            "buy_it_now": "Buy It Now",
            "auction": "Auction",
            "auction_plus_bin": "Auction+BIN",
            "store_30": "Store 30d",
        }.get(str(st.session_state.get("listing_wizard_mode") or "buy_it_now"), "Buy It Now"),
    )
    ws4.metric(
        "Preflight",
        (
            f"{int(st.session_state.get('listing_wizard_preflight_blocker_count') or 0)} blocker(s)"
            if int(st.session_state.get("listing_wizard_preflight_blocker_count") or 0) > 0
            else (
                f"{int(st.session_state.get('listing_wizard_preflight_warning_count') or 0)} warning(s)"
                if int(st.session_state.get("listing_wizard_preflight_warning_count") or 0) > 0
                else "ready"
            )
        ),
    )

    seed_signature = f"{int(product_id) if product_id else 0}:{int(getattr(template_row, 'id', 0) or 0)}"
    last_seed_signature = str(st.session_state.get("listing_wizard_seed_signature") or "").strip()
    if last_seed_signature != seed_signature:
        # Prevent stale AI/media state from prior product/template context.
        st.session_state.pop("listing_wizard_ai_suggestions", None)
        st.session_state.pop("listing_wizard_ai_diagnostics", None)
        st.session_state.pop("listing_wizard_ai_acceptance", None)
        st.session_state.pop("listing_wizard_ai_comp_evidence", None)
        st.session_state.pop("listing_wizard_ai_has_run", None)
        st.session_state.pop("listing_wizard_ai_show_debug_panels", None)
        st.session_state.pop("listing_wizard_existing_media_select", None)
        st.session_state.pop("listing_wizard_show_selected_media_preview", None)

        previous_seed_title = str(st.session_state.get("listing_wizard_seed_title") or "").strip()
        previous_seed_details = str(st.session_state.get("listing_wizard_seed_details") or "").strip()
        previous_seed_condition_description = str(
            st.session_state.get("listing_wizard_seed_condition_description") or ""
        ).strip()
        current_title = str(st.session_state.get("listing_wizard_title") or "").strip()
        current_details = str(st.session_state.get("listing_wizard_details") or "").strip()
        current_condition_description = str(st.session_state.get("listing_wizard_condition_description") or "").strip()
        should_update_title = (
            "listing_wizard_title" not in st.session_state
            or not current_title
            or current_title == previous_seed_title
        )
        should_update_details = (
            "listing_wizard_details" not in st.session_state
            or not current_details
            or current_details == previous_seed_details
        )
        should_update_condition_description = (
            "listing_wizard_condition_description" not in st.session_state
            or not current_condition_description
            or current_condition_description == previous_seed_condition_description
        )
        if should_update_title:
            st.session_state["listing_wizard_title"] = default_title
        if should_update_details:
            st.session_state["listing_wizard_details"] = default_details
        if should_update_condition_description:
            st.session_state["listing_wizard_condition_description"] = default_condition_description
        st.session_state["listing_wizard_seed_signature"] = seed_signature
        st.session_state["listing_wizard_seed_title"] = default_title
        st.session_state["listing_wizard_seed_details"] = default_details
        st.session_state["listing_wizard_seed_condition_description"] = default_condition_description

    if "listing_wizard_category_id" not in st.session_state:
        st.session_state["listing_wizard_category_id"] = runtime_category_id
    if "listing_wizard_subtitle" not in st.session_state:
        st.session_state["listing_wizard_subtitle"] = ""
    if "listing_wizard_condition_description" not in st.session_state:
        st.session_state["listing_wizard_condition_description"] = default_condition_description
    if "listing_wizard_aspects_json" not in st.session_state:
        st.session_state["listing_wizard_aspects_json"] = ""

    _wizard_apply_pending_field_updates()
    apply_msg = str(st.session_state.pop("listing_wizard_apply_flash", "") or "").strip()
    if apply_msg:
        st.success(apply_msg)
    if bool(st.session_state.pop("listing_wizard_apply_title_trimmed_flash", False)):
        st.info("AI title was trimmed to eBay 80-character max.")

    st.markdown("### Step 3 of 9: Listing Mode + Pricing")
    listing_mode = st.selectbox(
        "Listing Mode",
        options=["buy_it_now", "auction", "auction_plus_bin", "store_30"],
        format_func=lambda v: {
            "buy_it_now": "Buy It Now (Fixed)",
            "auction": "Auction",
            "auction_plus_bin": "Auction + Buy It Now",
            "store_30": "Store Listing (30 days)",
        }.get(v, v),
        key="listing_wizard_mode",
    )
    defaults = _listing_mode_defaults(listing_mode)
    format_type = defaults["format_type"]

    col1, col2 = st.columns(2)
    with col1:
        listing_title = st.text_input(
            "Listing Title",
            key="listing_wizard_title",
        )
        title_len = len(str(listing_title or "").strip())
        t1, t2 = st.columns([2, 1])
        with t1:
            st.caption(f"Title length: {title_len}/{EBAY_TITLE_MAX_CHARS}")
            if title_len > EBAY_TITLE_MAX_CHARS:
                st.warning(f"eBay title limit is {EBAY_TITLE_MAX_CHARS} characters. Shorten before creating draft.")
        with t2:
            st.button(
                "Trim to 80",
                key="listing_wizard_trim_title_btn",
                on_click=_wizard_trim_title_to_ebay_max,
                disabled=(title_len <= EBAY_TITLE_MAX_CHARS),
                help="Trim title to eBay max length.",
            )
    with col2:
        listing_price = st.number_input(
            "Listing Price",
            min_value=0.0,
            step=0.01,
            value=float(default_price),
            key="listing_wizard_price",
        )
    listing_qty = 1
    if listing_mode in {"buy_it_now", "store_30"}:
        listing_qty = int(
            st.number_input(
                "Quantity to List",
                min_value=1,
                step=1,
                value=int(st.session_state.get("listing_wizard_quantity") or 1),
                key="listing_wizard_quantity",
                help="Available quantity for Buy It Now / Store listings.",
            )
        )
    else:
        st.session_state["listing_wizard_quantity"] = 1
        st.caption("Quantity is fixed to 1 for auction listing formats.")
    current_stock_qty = int(getattr(selected_product, "current_quantity", 0) or 0) if selected_product is not None else 0
    bundle_enabled = bool(
        st.checkbox(
            "This listing is a product lot / bundle",
            value=bool(st.session_state.get("listing_wizard_bundle_enabled")),
            key="listing_wizard_bundle_enabled",
            help=(
                "Use when one marketplace listing unit contains multiple inventory units, "
                "for example one eBay listing for a lot of 10 coins."
            ),
        )
    )
    bundle_primary_qty = 1
    bundle_inventory_overcommit = False
    bundle_metadata = _build_listing_bundle_metadata(
        enabled=False,
        primary_product=selected_product,
        units_per_listing=1,
        available_lots=int(listing_qty),
    )
    if bundle_enabled:
        default_bundle_qty = max(1, min(max(1, current_stock_qty), int(st.session_state.get("listing_wizard_bundle_primary_qty") or 1)))
        bundle_primary_qty = int(
            st.number_input(
                "Units of selected product per listing",
                min_value=1,
                max_value=max(1, current_stock_qty),
                step=1,
                value=default_bundle_qty,
                key="listing_wizard_bundle_primary_qty",
                help="How many units of the selected product are included in each single marketplace listing unit.",
            )
        )
        bundle_extra_options = {
            label: pid
            for label, pid in product_options.items()
            if int(pid or 0) != int(product_id or 0)
        }
        st.session_state["listing_wizard_bundle_extra_product_labels"] = [
            label
            for label in list(st.session_state.get("listing_wizard_bundle_extra_product_labels") or [])
            if label in bundle_extra_options
        ]
        bundle_extra_labels = st.multiselect(
            "Additional products in this bundle",
            options=list(bundle_extra_options.keys()),
            key="listing_wizard_bundle_extra_product_labels",
            help=(
                "Optional. Search products above to make more products available here, "
                "then choose any extra products included in each bundle listing."
            ),
        )
        additional_bundle_components: list[dict[str, object]] = []
        for extra_label in bundle_extra_labels:
            extra_product_id = int(bundle_extra_options.get(extra_label) or 0)
            extra_product = product_by_id.get(extra_product_id)
            if extra_product is None:
                continue
            extra_stock_qty = int(getattr(extra_product, "current_quantity", 0) or 0)
            extra_qty = int(
                st.number_input(
                    f"Units of {getattr(extra_product, 'sku', '') or extra_product_id} per listing",
                    min_value=1,
                    max_value=max(1, extra_stock_qty),
                    value=max(
                        1,
                        min(
                            max(1, extra_stock_qty),
                            int(st.session_state.get(f"listing_wizard_bundle_extra_qty_{extra_product_id}") or 1),
                        ),
                    ),
                    step=1,
                    key=f"listing_wizard_bundle_extra_qty_{extra_product_id}",
                    help="How many units of this additional product are included in each bundle listing.",
                )
            )
            additional_bundle_components.append(_bundle_component(extra_product, extra_qty))
        bundle_metadata = _build_listing_bundle_metadata(
            enabled=True,
            primary_product=selected_product,
            units_per_listing=int(bundle_primary_qty),
            available_lots=int(listing_qty),
            additional_components=additional_bundle_components,
        )
        committed_units = int(bundle_metadata.get("inventory_units_committed") or 0)
        overcommitted_components: list[str] = []
        for component in list(bundle_metadata.get("components") or []):
            if not isinstance(component, dict):
                continue
            units_needed = max(1, int(component.get("quantity_per_listing") or 1)) * int(listing_qty)
            stock_qty = max(0, int(component.get("current_quantity") or 0))
            if units_needed > stock_qty:
                overcommitted_components.append(
                    f"{component.get('sku') or component.get('product_id')}: needs {units_needed}, stock {stock_qty}"
                )
        bundle_inventory_overcommit = bool(overcommitted_components)
        composition_bits = [
            f"{row.get('sku') or row.get('product_id')} x {int(row.get('quantity_per_listing') or 1)}"
            for row in list(bundle_metadata.get("components") or [])
            if isinstance(row, dict)
        ]
        st.caption(
            f"Bundle composition per listing: {', '.join(composition_bits)}. "
            f"{int(listing_qty)} available lot(s) commits {committed_units} total inventory unit(s)."
        )
        if bundle_inventory_overcommit:
            st.warning(
                "This bundle quantity exceeds current stock: "
                + " | ".join(overcommitted_components)
                + ". Reduce units per listing or available lot quantity before creating/publishing."
            )

    st.markdown("#### Estimated eBay Fees (Pricing Assist)")
    ef1, ef2 = st.columns(2)
    with ef1:
        estimated_buyer_shipping = float(
            st.number_input(
                "Estimated Buyer-Paid Shipping (USD)",
                min_value=0.0,
                step=0.01,
                value=float(st.session_state.get("listing_wizard_estimated_buyer_shipping") or 0.0),
                key="listing_wizard_estimated_buyer_shipping",
                help="For pricing guidance only. This is not the local shipping cost field.",
            )
        )
    with ef2:
        estimated_promoted_rate = float(
            st.number_input(
                "Estimated Promoted Listing Rate (%)",
                min_value=0.0,
                max_value=100.0,
                step=0.1,
                value=float(st.session_state.get("listing_wizard_estimated_promoted_rate") or 0.0),
                key="listing_wizard_estimated_promoted_rate",
                help="Override for ad-rate impact on estimated fees.",
            )
        )
    estimated_local_shipping_cost_per_item = float(
        st.number_input(
            "Estimated Local Fulfillment Cost per Item (USD)",
            min_value=0.0,
            step=0.01,
            value=float(st.session_state.get("listing_wizard_estimated_local_shipping_cost_per_item") or 0.0),
            key="listing_wizard_estimated_local_shipping_cost_per_item",
            help="Your estimated shipping/packing label spend per sold item for expected-net scoring.",
        )
    )
    fee_estimate_raw_price = (
        st.session_state.get("listing_wizard_auction_start")
        if format_type == "AUCTION"
        else listing_price
    )
    fee_estimate_unit_price = _safe_price_float(fee_estimate_raw_price, 0.0)
    wizard_fee_estimate = estimate_ebay_fees(
        repo,
        unit_price=fee_estimate_unit_price,
        quantity=int(listing_qty),
        buyer_paid_shipping=estimated_buyer_shipping,
        promoted_rate_percent=estimated_promoted_rate,
    )
    em1, em2, em3, em4 = st.columns(4)
    with em1:
        st.metric("Est Gross", f"${float(wizard_fee_estimate.get('gross_total') or 0.0):,.2f}")
    with em2:
        st.metric("Est Fees", f"${float(wizard_fee_estimate.get('estimated_total_fees') or 0.0):,.2f}")
    with em3:
        st.metric(
            "Est Net Payout",
            f"${float(wizard_fee_estimate.get('estimated_net_payout_before_shipping_cost') or 0.0):,.2f}",
        )
    with em4:
        st.metric(
            "Fee %",
            f"{float(wizard_fee_estimate.get('estimated_fee_percent_of_gross') or 0.0):.2f}%",
        )
    st.caption(
        "Estimate assumptions: "
        f"final value {float(wizard_fee_estimate.get('final_value_rate_percent') or 0.0):.2f}% + "
        f"${float(wizard_fee_estimate.get('final_value_fixed_usd') or 0.0):,.2f}, "
        f"payment {float(wizard_fee_estimate.get('payment_rate_percent') or 0.0):.2f}% + "
        f"${float(wizard_fee_estimate.get('payment_fixed_usd') or 0.0):,.2f}, "
        f"promoted {float(wizard_fee_estimate.get('promoted_rate_percent') or 0.0):.2f}%."
    )
    known_unit_cost = _known_unit_cost(selected_product)
    expected_net_unit_cost = (
        _bundle_expected_unit_cost(bundle_metadata, product_by_id)
        if bundle_enabled
        else known_unit_cost
    )
    expected_net = _expected_net_score(
        fee_estimate=wizard_fee_estimate,
        quantity=int(listing_qty),
        known_unit_cost=float(expected_net_unit_cost or 0.0),
        estimated_local_shipping_cost_per_item=float(estimated_local_shipping_cost_per_item or 0.0),
    )
    st.markdown("##### Expected Net Score (Pre-Publish)")
    en1, en2, en3, en4, en5 = st.columns(5)
    with en1:
        st.metric("Known Cost / Listing Unit", f"${float(expected_net_unit_cost or 0.0):,.2f}")
    with en2:
        st.metric("Est COGS Total", f"${float(expected_net.get('known_cogs_total') or 0.0):,.2f}")
    with en3:
        st.metric("Expected Net", f"${float(expected_net.get('expected_net') or 0.0):,.2f}")
    with en4:
        st.metric("Expected Margin %", f"{float(expected_net.get('expected_margin_pct_of_gross') or 0.0):.2f}%")
    with en5:
        st.metric("Breakeven Listing", f"${float(expected_net.get('breakeven_listing_price') or 0.0):,.2f}")
    st.caption(
        "Expected-net score: "
        f"`{str(expected_net.get('score') or '').upper()}` | "
        "formula = est net payout - local fulfillment cost - known COGS. "
        f"Price cushion vs breakeven: ${float(expected_net.get('price_cushion') or 0.0):,.2f}."
    )

    st.markdown("#### eBay Category")
    category_suggestions = st.session_state.get("listing_wizard_category_suggestions")
    if not isinstance(category_suggestions, list):
        category_suggestions = []
        st.session_state["listing_wizard_category_suggestions"] = category_suggestions
    category_labels, category_label_to_id = _wizard_category_options(
        repo=repo,
        username=user.username,
        runtime_category_id=runtime_category_id,
        cached_suggestions=category_suggestions,
    )
    seed_product_id = int(getattr(selected_product, "id", 0) or 0) if selected_product is not None else 0
    current_category_id = str(st.session_state.get("listing_wizard_category_id") or "").strip()
    last_category_id = str(st.session_state.get("listing_wizard_last_category_id") or "").strip()
    last_category_product_id = int(st.session_state.get("listing_wizard_last_category_product_id") or 0)
    last_category_matches_product = not seed_product_id or not last_category_product_id or last_category_product_id == seed_product_id
    if not current_category_id and last_category_id and last_category_matches_product:
        current_category_id = last_category_id
        st.session_state["listing_wizard_category_id"] = last_category_id
    current_category_label = next(
        (label for label, cid in category_label_to_id.items() if cid == current_category_id),
        "__manual__",
    )
    selected_category_label = st.selectbox(
        "Category Source",
        options=["__manual__", *category_labels],
        index=(0 if current_category_label == "__manual__" else (["__manual__", *category_labels].index(current_category_label))),
        format_func=lambda value: "Manual Entry" if value == "__manual__" else value,
        key="listing_wizard_category_select",
    )
    if selected_category_label != "__manual__":
        selected_id = str(category_label_to_id.get(selected_category_label) or current_category_id).strip()
        if selected_id and selected_id != str(st.session_state.get("listing_wizard_category_id") or "").strip():
            st.session_state["listing_wizard_last_category_id"] = selected_id
            st.session_state["listing_wizard_last_category_product_id"] = seed_product_id
            _wizard_queue_pending_field_updates({"listing_wizard_category_id": selected_id})
            st.rerun()
    selected_category_id = st.text_input(
        "eBay Category ID",
        key="listing_wizard_category_id",
        help="Required for publish preflight. Select from dropdown or enter manually.",
    ).strip()
    if selected_category_id:
        st.session_state["listing_wizard_last_category_id"] = selected_category_id
        st.session_state["listing_wizard_last_category_product_id"] = seed_product_id
    else:
        selected_category_id = (
            str(st.session_state.get("listing_wizard_last_category_id") or "").strip()
            if last_category_matches_product
            else ""
        )
    wizard_store_category_marketplace_id = str(
        st.session_state.get("listing_wizard_ebay_marketplace_id")
        or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
        or "EBAY_US"
    ).strip()
    wizard_store_category_options = _wizard_store_category_options(
        repo,
        marketplace_id=wizard_store_category_marketplace_id,
    )
    current_store_category_names = _wizard_normalize_store_category_names(
        st.session_state.get("listing_wizard_store_category_names")
    )
    wizard_store_category_default = [
        path for path in current_store_category_names if path in wizard_store_category_options
    ]
    if current_store_category_names != wizard_store_category_default:
        st.session_state["listing_wizard_store_category_names"] = wizard_store_category_default
    selected_store_category_names = st.multiselect(
        "eBay Store Categories (optional)",
        options=wizard_store_category_options,
        default=wizard_store_category_default,
        max_selections=2,
        key="listing_wizard_store_category_names",
        help="Optional. eBay store category full paths; eBay allows up to two per offer.",
    )
    selected_store_category_names = _wizard_normalize_store_category_names(selected_store_category_names)
    if not wizard_store_category_options:
        st.caption("No saved eBay store categories yet. Add them from Listings > eBay Store Categories.")
    seed_query = _category_query_seed(
        title=str(getattr(selected_product, "title", "") or "").strip() if selected_product is not None else "",
        category=str(getattr(selected_product, "category", "") or "").strip() if selected_product is not None else "",
        metal_type=str(getattr(selected_product, "metal_type", "") or "").strip() if selected_product is not None else "",
        sku=str(getattr(selected_product, "sku", "") or "").strip() if selected_product is not None else "",
    )
    if "listing_wizard_category_query_seed_product_id" not in st.session_state:
        st.session_state["listing_wizard_category_query_seed_product_id"] = seed_product_id
    if (
        "listing_wizard_category_query" not in st.session_state
        or int(st.session_state.get("listing_wizard_category_query_seed_product_id") or 0) != seed_product_id
    ):
        st.session_state["listing_wizard_category_query"] = seed_query
        st.session_state["listing_wizard_category_query_seed_product_id"] = seed_product_id

    cat_q1, cat_q2, cat_q3 = st.columns([3, 1, 1])
    with cat_q1:
        category_query = st.text_input(
            "Find eBay categories",
            key="listing_wizard_category_query",
            help="Enter keywords and fetch category suggestions from eBay Taxonomy API.",
        ).strip()
    with cat_q2:
        st.write("")
        st.write("")
        if st.button("Fetch Suggestions", key="listing_wizard_fetch_category_btn"):
            access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
            marketplace_id = str(
                st.session_state.get("listing_wizard_ebay_marketplace_id")
                or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
                or "EBAY_US"
            ).strip()
            if not category_query:
                st.warning("Enter a query to fetch category suggestions.")
            else:
                try:
                    cached = repo.list_cached_ebay_category_suggestions(
                        environment=settings.app_env,
                        marketplace_id=marketplace_id,
                        query=category_query,
                        limit=20,
                    )
                    if cached:
                        fresh = list(cached)
                        st.session_state["listing_wizard_category_suggestions"] = fresh
                        st.success(f"Loaded {len(fresh)} cached category suggestion(s).")
                    else:
                        if not access_token:
                            st.warning("Missing eBay user access token. Set it in Admin eBay Verify first.")
                            st.stop()
                        client = EbayClient()
                        fresh = client.get_category_suggestions(
                            access_token=access_token,
                            query=category_query,
                            marketplace_id=marketplace_id,
                            limit=20,
                        )
                        st.session_state["listing_wizard_category_suggestions"] = list(fresh or [])
                        if fresh:
                            repo.cache_ebay_category_suggestions(
                                environment=settings.app_env,
                                marketplace_id=marketplace_id,
                                query=category_query,
                                suggestions=fresh,
                                actor=user.username,
                            )
                        st.success(f"Loaded {len(fresh)} category suggestion(s) from eBay.")
                    if fresh:
                        next_category_id = str((fresh[0] or {}).get("category_id") or selected_category_id).strip()
                        if next_category_id:
                            st.session_state["listing_wizard_last_category_id"] = next_category_id
                            st.session_state["listing_wizard_last_category_product_id"] = seed_product_id
                            _wizard_queue_pending_field_updates({"listing_wizard_category_id": next_category_id})
                    else:
                        st.info("No category suggestions found for that query.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category fetch failed: {exc}")
    with cat_q3:
        st.write("")
        st.write("")
        if st.button("Refresh from eBay", key="listing_wizard_refresh_category_btn"):
            access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
            marketplace_id = str(
                st.session_state.get("listing_wizard_ebay_marketplace_id")
                or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
                or "EBAY_US"
            ).strip()
            if not category_query:
                st.warning("Enter a query to fetch category suggestions.")
            elif not access_token:
                st.warning("Missing eBay user access token. Set it in Admin eBay Verify first.")
            else:
                try:
                    client = EbayClient()
                    fresh = client.get_category_suggestions(
                        access_token=access_token,
                        query=category_query,
                        marketplace_id=marketplace_id,
                        limit=20,
                    )
                    st.session_state["listing_wizard_category_suggestions"] = list(fresh or [])
                    if fresh:
                        repo.cache_ebay_category_suggestions(
                            environment=settings.app_env,
                            marketplace_id=marketplace_id,
                            query=category_query,
                            suggestions=fresh,
                            actor=user.username,
                        )
                        next_category_id = str((fresh[0] or {}).get("category_id") or selected_category_id).strip()
                        if next_category_id:
                            st.session_state["listing_wizard_last_category_id"] = next_category_id
                            st.session_state["listing_wizard_last_category_product_id"] = seed_product_id
                            _wizard_queue_pending_field_updates({"listing_wizard_category_id": next_category_id})
                        st.success(f"Refreshed {len(fresh)} category suggestion(s) from eBay.")
                    else:
                        st.info("No category suggestions found for that query.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category refresh failed: {exc}")
    if not selected_category_id:
        st.caption("Tip: choose a category above or fetch suggestions to satisfy preflight.")
    category_aspect_rows = st.session_state.get("listing_wizard_category_aspect_rows")
    if not isinstance(category_aspect_rows, list):
        category_aspect_rows = []
    aspect_cache_marketplace_id = str(
        st.session_state.get("listing_wizard_ebay_marketplace_id")
        or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
        or "EBAY_US"
    ).strip()
    aspect_cache_signature = f"{aspect_cache_marketplace_id.upper()}:{selected_category_id}"
    if selected_category_id and st.session_state.get("listing_wizard_category_aspect_signature") != aspect_cache_signature:
        category_aspect_rows = []
        st.session_state["listing_wizard_category_aspect_rows"] = category_aspect_rows
        st.session_state["listing_wizard_category_aspect_signature"] = aspect_cache_signature
    elif not selected_category_id and category_aspect_rows:
        category_aspect_rows = []
        st.session_state["listing_wizard_category_aspect_rows"] = []
        st.session_state["listing_wizard_category_aspect_signature"] = ""
    load_aspects_col, refresh_aspects_col, aspect_status_col = st.columns([1, 1, 2])
    with load_aspects_col:
        if st.button("Load Required Item Specifics", key="listing_wizard_load_required_aspects_btn"):
            marketplace_id = str(
                st.session_state.get("listing_wizard_ebay_marketplace_id")
                or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
                or "EBAY_US"
            ).strip()
            if not selected_category_id:
                st.warning("Select or enter an eBay category ID first.")
            else:
                cached = repo.get_cached_ebay_category_aspects(
                    environment=settings.app_env,
                    marketplace_id=marketplace_id,
                    category_id=selected_category_id,
                )
                if cached:
                    category_aspect_rows = normalize_ebay_category_aspect_rows(cached.get("aspects") or [])
                    st.session_state["listing_wizard_category_aspect_rows"] = category_aspect_rows
                    st.session_state["listing_wizard_category_aspect_signature"] = aspect_cache_signature
                    st.success(
                        f"Loaded {len(category_aspect_rows)} cached category item specific(s)."
                    )
                else:
                    access_token = get_runtime_str(
                        repo, "ebay_user_access_token", settings.ebay_user_access_token
                    ).strip()
                    if not access_token:
                        st.warning("Missing eBay user access token. Set it in Admin eBay Verify first.")
                        st.stop()
                    try:
                        client = EbayClient()
                        raw_aspects = client.get_item_aspects_for_category(
                            access_token=access_token,
                            category_id=selected_category_id,
                            marketplace_id=marketplace_id,
                        )
                        category_aspect_rows = normalize_ebay_category_aspect_rows(raw_aspects)
                        repo.cache_ebay_category_aspects(
                            environment=settings.app_env,
                            marketplace_id=marketplace_id,
                            category_id=selected_category_id,
                            aspects=category_aspect_rows,
                            actor=user.username,
                        )
                        st.session_state["listing_wizard_category_aspect_rows"] = category_aspect_rows
                        st.session_state["listing_wizard_category_aspect_signature"] = aspect_cache_signature
                        required_count = sum(1 for row in category_aspect_rows if bool((row or {}).get("required")))
                        st.success(
                            f"Loaded {len(category_aspect_rows)} category item specific(s) from eBay; "
                            f"{required_count} required."
                        )
                    except Exception as exc:
                        st.error(f"Required item specifics fetch failed: {exc}")
    with refresh_aspects_col:
        if st.button("Refresh Required Item Specifics", key="listing_wizard_refresh_required_aspects_btn"):
            access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
            marketplace_id = str(
                st.session_state.get("listing_wizard_ebay_marketplace_id")
                or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
                or "EBAY_US"
            ).strip()
            if not selected_category_id:
                st.warning("Select or enter an eBay category ID first.")
            elif not access_token:
                st.warning("Missing eBay user access token. Set it in Admin eBay Verify first.")
            else:
                try:
                    client = EbayClient()
                    raw_aspects = client.get_item_aspects_for_category(
                        access_token=access_token,
                        category_id=selected_category_id,
                        marketplace_id=marketplace_id,
                    )
                    category_aspect_rows = normalize_ebay_category_aspect_rows(raw_aspects)
                    repo.cache_ebay_category_aspects(
                        environment=settings.app_env,
                        marketplace_id=marketplace_id,
                        category_id=selected_category_id,
                        aspects=category_aspect_rows,
                        actor=user.username,
                    )
                    st.session_state["listing_wizard_category_aspect_rows"] = category_aspect_rows
                    st.session_state["listing_wizard_category_aspect_signature"] = aspect_cache_signature
                    required_count = sum(1 for row in category_aspect_rows if bool((row or {}).get("required")))
                    st.success(
                        f"Refreshed {len(category_aspect_rows)} category item specific(s) from eBay; "
                        f"{required_count} required."
                    )
                except Exception as exc:
                    st.error(f"Required item specifics fetch failed: {exc}")
    with aspect_status_col:
        if category_aspect_rows:
            required_count = sum(1 for row in category_aspect_rows if bool((row or {}).get("required")))
            st.caption(
                f"eBay category aspects loaded for `{selected_category_id or '(no category)'}`: "
                f"{required_count} required of {len(category_aspect_rows)} total."
            )
    local_category_aspects_cache: dict[tuple[str, str], list[dict]] = {}

    def _wizard_required_specific_blockers(*, category_id_value: str, marketplace_id_value: str) -> list[str]:
        category_id_clean = str(category_id_value or "").strip()
        marketplace_id_clean = str(marketplace_id_value or aspect_cache_marketplace_id or "EBAY_US").strip()
        if not category_id_clean:
            return []
        cache_key = (marketplace_id_clean.upper(), category_id_clean)
        if cache_key not in local_category_aspects_cache:
            cached = repo.get_cached_ebay_category_aspects(
                environment=settings.app_env,
                marketplace_id=marketplace_id_clean,
                category_id=category_id_clean,
            )
            local_category_aspects_cache[cache_key] = (
                normalize_ebay_category_aspect_rows((cached or {}).get("aspects") or [])
                if cached
                else []
            )
        rows = list(local_category_aspects_cache.get(cache_key) or [])
        if not rows:
            return []
        aspects_payload = _wizard_normalize_aspects_payload(
            st.session_state.get("listing_wizard_aspects_json") or ""
        )
        return [
            f"Missing required eBay item specific: {str((row or {}).get('name') or '').strip()}"
            for row in missing_required_ebay_aspects(rows, aspects_payload)
            if str((row or {}).get("name") or "").strip()
        ]

    category_condition_rows = st.session_state.get("listing_wizard_category_condition_rows")
    if not isinstance(category_condition_rows, list):
        category_condition_rows = []
    condition_policy_marketplace_id = str(
        st.session_state.get("listing_wizard_ebay_marketplace_id")
        or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
        or "EBAY_US"
    ).strip()
    condition_policy_signature = f"{condition_policy_marketplace_id.upper()}:{selected_category_id}"
    if (
        selected_category_id
        and st.session_state.get("listing_wizard_category_condition_signature") != condition_policy_signature
    ):
        category_condition_rows = []
        st.session_state["listing_wizard_category_condition_rows"] = []
        st.session_state["listing_wizard_category_condition_signature"] = condition_policy_signature
    elif not selected_category_id and category_condition_rows:
        category_condition_rows = []
        st.session_state["listing_wizard_category_condition_rows"] = []
        st.session_state["listing_wizard_category_condition_signature"] = ""

    cond_col1, cond_col2, cond_col3 = st.columns([1, 1, 2])
    with cond_col1:
        if st.button("Load Category Conditions", key="listing_wizard_load_conditions_btn"):
            access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
            if not selected_category_id:
                st.warning("Select or enter an eBay category ID first.")
            elif not access_token:
                st.warning("Missing eBay user access token. Set it in Admin eBay Verify first.")
            else:
                try:
                    client = EbayClient()
                    policies = client.get_item_condition_policies(
                        access_token=access_token,
                        category_id=selected_category_id,
                        marketplace_id=condition_policy_marketplace_id,
                    )
                    category_condition_rows = normalize_ebay_condition_policy_rows(
                        policies,
                        category_id=selected_category_id,
                    )
                    st.session_state["listing_wizard_category_condition_rows"] = category_condition_rows
                    st.session_state["listing_wizard_category_condition_signature"] = condition_policy_signature
                    current_condition = str(st.session_state.get("listing_wizard_ebay_condition") or "").strip().upper()
                    if category_condition_rows and not _wizard_is_condition_valid_for_loaded_policy(
                        category_condition_rows,
                        current_condition,
                    ):
                        st.session_state["listing_wizard_ebay_condition"] = str(category_condition_rows[0]["condition"])
                    st.success(f"Loaded {len(category_condition_rows)} eBay category condition option(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category condition fetch failed: {exc}")
    with cond_col2:
        if st.button("Refresh Category Conditions", key="listing_wizard_refresh_conditions_btn"):
            access_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
            if not selected_category_id:
                st.warning("Select or enter an eBay category ID first.")
            elif not access_token:
                st.warning("Missing eBay user access token. Set it in Admin eBay Verify first.")
            else:
                try:
                    client = EbayClient()
                    policies = client.get_item_condition_policies(
                        access_token=access_token,
                        category_id=selected_category_id,
                        marketplace_id=condition_policy_marketplace_id,
                    )
                    category_condition_rows = normalize_ebay_condition_policy_rows(
                        policies,
                        category_id=selected_category_id,
                    )
                    st.session_state["listing_wizard_category_condition_rows"] = category_condition_rows
                    st.session_state["listing_wizard_category_condition_signature"] = condition_policy_signature
                    current_condition = str(st.session_state.get("listing_wizard_ebay_condition") or "").strip().upper()
                    if category_condition_rows and not _wizard_is_condition_valid_for_loaded_policy(
                        category_condition_rows,
                        current_condition,
                    ):
                        st.session_state["listing_wizard_ebay_condition"] = str(category_condition_rows[0]["condition"])
                    st.success(f"Refreshed {len(category_condition_rows)} eBay category condition option(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category condition refresh failed: {exc}")
    with cond_col3:
        if category_condition_rows:
            st.caption(
                f"eBay category conditions loaded for `{selected_category_id}`: "
                + ", ".join(
                    f"{row.get('label')} ({row.get('condition')})"
                    for row in category_condition_rows[:6]
                )
            )
        else:
            st.caption(
                "Category-specific condition policy not loaded yet. Load it before direct eBay post to avoid "
                "eBay 25021 condition/category mismatches."
            )
    current_wizard_condition = str(st.session_state.get("listing_wizard_ebay_condition") or "NEW").strip().upper()
    wizard_condition_options = _wizard_condition_options(category_condition_rows, current_wizard_condition)
    wizard_condition_labels = _wizard_condition_option_labels(category_condition_rows, current_wizard_condition)

    listing_details = st.text_area(
        "Listing Details",
        key="listing_wizard_details",
        height=180,
    )
    st.markdown("#### Item Specifics Defaults")
    load_compact_aspects_defaults = st.checkbox(
        "Load Suggested Item Specific Defaults (slower)",
        value=False,
        key="listing_wizard_load_compact_aspect_defaults",
        help="Defers default item-specific merge/table rendering while editing basic listing details.",
    )
    if not load_compact_aspects_defaults:
        st.caption(
            "Suggested item-specific defaults are deferred. Enable the checkbox above to preview/apply defaults."
        )
    else:
        compact_aspects_preview = _wizard_normalize_aspects_payload(
            st.session_state.get("listing_wizard_aspects_json") or ""
        )
        compact_defaults_payload, _compact_injected = merge_ebay_aspects_defaults(
            category=str(getattr(selected_product, "category", "") or "").strip(),
            metal_type=str(getattr(selected_product, "metal_type", "") or "").strip(),
            title=str(listing_title or getattr(selected_product, "title", "") or "").strip(),
            weight_oz=getattr(selected_product, "weight_oz", None),
            existing_aspects=compact_aspects_preview,
        )
        compact_default_only_payload = {
            key: values
            for key, values in compact_defaults_payload.items()
            if key not in compact_aspects_preview
        }
        if compact_default_only_payload:
            st.caption("Suggested bullion/coin defaults:")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"aspect": k, "values": ", ".join(v)}
                        for k, v in sorted(compact_default_only_payload.items(), key=lambda kv: kv[0].lower())
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            if st.button(
                "Apply Suggested Default Aspects",
                key="listing_wizard_aspect_apply_defaults_compact_btn",
            ):
                _wizard_queue_pending_field_updates(
                    {"listing_wizard_aspects_json": json.dumps(compact_defaults_payload, indent=2)}
                )
                st.session_state["listing_wizard_apply_flash"] = (
                    "Applied default bullion/coin item specifics: "
                    + ", ".join(sorted(compact_default_only_payload.keys(), key=str.lower))
                )
                st.rerun()
        else:
            st.caption("Default item specifics are already applied or no defaults were detected.")

    with st.expander("Advanced eBay Fields + Item Specifics (Optional)", expanded=True):
        st.text_input(
            "Subtitle",
            key="listing_wizard_subtitle",
            help="Optional eBay subtitle-style field where supported.",
        )
        st.text_area(
            "Condition Description",
            key="listing_wizard_condition_description",
            height=80,
            help=f"Optional extra condition details for buyers. eBay limit: {EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters.",
        )
        condition_description_len = len(str(st.session_state.get("listing_wizard_condition_description") or ""))
        if condition_description_len > EBAY_MAX_CONDITION_DESCRIPTION_CHARS:
            st.error(
                "Condition Description is too long for eBay: "
                f"{condition_description_len}/{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters."
            )
        else:
            st.caption(
                f"Condition Description: {condition_description_len}/{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters."
            )
        grading_prefill_msg = _ai_grading_prefill_status(
            current_value=st.session_state.get("listing_wizard_condition_description"),
            default_value=default_condition_description,
        )
        if grading_prefill_msg:
            st.caption(grading_prefill_msg)
        st.text_area(
            "Item Specifics (JSON object)",
            key="listing_wizard_aspects_json",
            height=120,
            help='Example: {"Brand":["US Mint"],"Metal":["Silver"],"Fineness":["0.999"]}',
        )
        aspects_preview = _wizard_normalize_aspects_payload(
            st.session_state.get("listing_wizard_aspects_json") or ""
        )
        defaults_preview_payload, _defaults_injected_keys = merge_ebay_aspects_defaults(
            category=str(getattr(selected_product, "category", "") or "").strip(),
            metal_type=str(getattr(selected_product, "metal_type", "") or "").strip(),
            title=str(listing_title or getattr(selected_product, "title", "") or "").strip(),
            weight_oz=getattr(selected_product, "weight_oz", None),
            existing_aspects=aspects_preview,
        )
        default_only_payload = {
            key: values
            for key, values in defaults_preview_payload.items()
            if key not in aspects_preview
        }
        st.caption("Item Specifics Builder")
        if default_only_payload:
            st.caption("Suggested bullion/coin defaults:")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"aspect": k, "values": ", ".join(v)}
                        for k, v in sorted(default_only_payload.items(), key=lambda kv: kv[0].lower())
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            if st.button("Apply Suggested Default Aspects", key="listing_wizard_aspect_apply_defaults_btn"):
                _wizard_queue_pending_field_updates(
                    {"listing_wizard_aspects_json": json.dumps(defaults_preview_payload, indent=2)}
                )
                st.session_state["listing_wizard_apply_flash"] = (
                    "Applied default bullion/coin item specifics: "
                    + ", ".join(sorted(default_only_payload.keys(), key=str.lower))
                )
                st.rerun()
        if aspects_preview:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"aspect": k, "values": ", ".join(v)}
                        for k, v in sorted(aspects_preview.items(), key=lambda kv: kv[0].lower())
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No parsed item specifics yet.")

        if category_aspect_rows:
            table_rows = _wizard_category_aspect_table_rows(category_aspect_rows, aspects_preview)
            required_table_rows = [row for row in table_rows if row.get("required") == "yes"]
            missing_required_rows = missing_required_ebay_aspects(category_aspect_rows, aspects_preview)
            if required_table_rows:
                st.caption("Required item specifics from selected eBay category:")
                st.dataframe(
                    pd.DataFrame(required_table_rows),
                    use_container_width=True,
                    hide_index=True,
                )
            if missing_required_rows:
                st.warning(
                    "Missing required item specifics: "
                    + ", ".join(str(row.get("name") or "") for row in missing_required_rows)
                )
                missing_labels = [str(row.get("name") or "").strip() for row in missing_required_rows]
                selected_missing = st.selectbox(
                    "Required Specific",
                    options=missing_labels,
                    key="listing_wizard_required_aspect_select",
                )
                selected_missing_row = next(
                    (
                        row
                        for row in missing_required_rows
                        if str(row.get("name") or "").strip() == selected_missing
                    ),
                    {},
                )
                allowed_values = [
                    str(value or "").strip()
                    for value in (selected_missing_row.get("values") or [])
                    if str(value or "").strip()
                ]
                if allowed_values:
                    required_value = st.selectbox(
                        "Required Specific Value",
                        options=[""] + allowed_values,
                        key=_wizard_category_aspect_input_key(
                            "listing_wizard_required_aspect_value", selected_missing
                        ),
                    )
                else:
                    required_value = st.text_input(
                        "Required Specific Value",
                        key=_wizard_category_aspect_input_key(
                            "listing_wizard_required_aspect_value", selected_missing
                        ),
                    )
                if st.button("Apply Required Specific", key="listing_wizard_apply_required_aspect_btn"):
                    if not selected_missing:
                        st.warning("Select a required item specific first.")
                    elif not str(required_value or "").strip():
                        st.warning("Enter a value before applying.")
                    else:
                        next_payload = dict(aspects_preview)
                        next_payload[selected_missing] = [str(required_value).strip()]
                        _wizard_queue_pending_field_updates(
                            {"listing_wizard_aspects_json": json.dumps(next_payload, indent=2)}
                        )
                        st.session_state["listing_wizard_apply_flash"] = (
                            f"Applied required item specific `{selected_missing}`."
                        )
                        st.rerun()
            elif required_table_rows:
                st.success("All loaded required item specifics are filled.")

        asp1, asp2 = st.columns(2)
        with asp1:
            aspect_name_input = st.text_input(
                "Aspect Name",
                key="listing_wizard_aspect_name_input",
                placeholder="e.g. Brand",
            ).strip()
        with asp2:
            aspect_values_input = st.text_input(
                "Aspect Values (comma-separated)",
                key="listing_wizard_aspect_values_input",
                placeholder="e.g. US Mint",
            ).strip()
        ab1, ab2, ab3 = st.columns(3)
        with ab1:
            if st.button("Add/Update Aspect", key="listing_wizard_aspect_add_btn"):
                if not aspect_name_input:
                    st.warning("Enter an aspect name first.")
                else:
                    next_payload = dict(aspects_preview)
                    values = [v.strip() for v in aspect_values_input.split(",") if v.strip()]
                    if not values:
                        st.warning("Enter at least one aspect value.")
                    else:
                        next_payload[aspect_name_input] = values
                        _wizard_queue_pending_field_updates(
                            {"listing_wizard_aspects_json": json.dumps(next_payload, indent=2)}
                        )
                        st.session_state["listing_wizard_apply_flash"] = f"Aspect `{aspect_name_input}` updated."
                        st.rerun()
        with ab2:
            remove_options = ["(select)"] + sorted(aspects_preview.keys(), key=lambda v: v.lower())
            remove_name = st.selectbox(
                "Remove Aspect",
                options=remove_options,
                key="listing_wizard_aspect_remove_select",
            )
            if st.button("Remove Selected", key="listing_wizard_aspect_remove_btn"):
                if remove_name == "(select)":
                    st.info("Select an aspect to remove.")
                else:
                    next_payload = dict(aspects_preview)
                    next_payload.pop(remove_name, None)
                    _wizard_queue_pending_field_updates(
                        {
                            "listing_wizard_aspects_json": json.dumps(next_payload, indent=2)
                            if next_payload
                            else ""
                        }
                    )
                    st.session_state["listing_wizard_apply_flash"] = f"Removed aspect `{remove_name}`."
                    st.rerun()
        with ab3:
            if st.button("Clear All Aspects", key="listing_wizard_aspect_clear_btn"):
                _wizard_queue_pending_field_updates({"listing_wizard_aspects_json": ""})
                st.session_state["listing_wizard_apply_flash"] = "Cleared item specifics."
                st.rerun()
    spec_lines: list[str] = []
    if selected_product is not None:
        sku = str(selected_product.sku or "").strip()
        category = str(selected_product.category or "").strip()
        metal = str(selected_product.metal_type or "").strip()
        weight_oz = str(selected_product.weight_oz or "").strip()
        if sku:
            spec_lines.append(f"SKU: {sku}")
        if category:
            spec_lines.append(f"Category: {category}")
        if metal:
            spec_lines.append(f"Metal: {metal}")
        if weight_oz:
            spec_lines.append(f"Weight: {weight_oz} oz")
    key_specs_block = "\n".join(spec_lines).strip()

    r1, r2, r3 = st.columns(3)
    with r1:
        st.button(
            "Reapply Product/Template Defaults",
            key="listing_wizard_reapply_defaults_btn",
            on_click=_wizard_set_text_fields,
            kwargs={
                "title": default_title,
                "details": default_details,
                "price": float(default_price),
                "condition_description": default_condition_description,
            },
            help="Resets title/details/price from current product + selected template defaults.",
        )
    with r2:
        st.button(
            "Clear Title + Details",
            key="listing_wizard_clear_text_btn",
            on_click=_wizard_set_text_fields,
            kwargs={
                "title": "",
                "details": "",
            },
        )
    with r3:
        st.button(
            "Append Product Key Specs",
            key="listing_wizard_append_specs_btn",
            on_click=_wizard_append_details_text,
            kwargs={"block_text": key_specs_block},
            disabled=not bool(key_specs_block),
            help="Adds SKU/category/metal/weight lines to listing details.",
        )
    with st.expander("Advanced Reset", expanded=False):
        st.button(
            "Reset Wizard Inputs",
            key="listing_wizard_reset_inputs_btn",
            on_click=_wizard_reset_inputs_for_context,
            kwargs={
                "default_title": default_title,
                "default_details": default_details,
                "default_price": float(default_price),
                "default_condition_description": default_condition_description,
            },
            help="Resets title/details/pricing/mode/offer controls and clears AI suggestion state.",
        )

    auction_duration = "GTC" if format_type == "FIXED_PRICE" else st.selectbox(
        "Auction Duration",
        options=["DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"],
        index=2,
        key="listing_wizard_auction_duration",
    )
    if listing_mode == "store_30":
        auction_duration = "DAYS_30"

    c3, c4, c5 = st.columns(3)
    with c3:
        auction_start = st.number_input(
            "Auction Start Price",
            min_value=0.0,
            step=0.01,
            value=float(defaults["auction_start_price"]),
            key="listing_wizard_auction_start",
            disabled=format_type != "AUCTION",
        )
    with c4:
        auction_reserve = st.number_input(
            "Auction Reserve Price (Optional)",
            min_value=0.0,
            step=0.01,
            value=float(defaults["auction_reserve_price"]),
            key="listing_wizard_auction_reserve",
            disabled=format_type != "AUCTION",
        )
    with c5:
        auction_bin = st.number_input(
            "Auction Buy It Now (Optional)",
            min_value=0.0,
            step=0.01,
            value=float(defaults["auction_buy_now_price"]),
            key="listing_wizard_auction_bin",
            disabled=format_type != "AUCTION",
        )

    st.markdown("### Step 4 of 9: Offer Controls")
    best_offer_enabled = st.checkbox("Accept Offers", value=False, key="listing_wizard_offer_enabled")
    co1, co2 = st.columns(2)
    with co1:
        best_offer_auto_accept = st.number_input(
            "Auto-Accept Offer >=",
            min_value=0.0,
            step=0.01,
            value=0.0,
            key="listing_wizard_offer_auto_accept",
            disabled=not best_offer_enabled,
        )
    with co2:
        best_offer_minimum = st.number_input(
            "Auto-Decline Offer <",
            min_value=0.0,
            step=0.01,
            value=0.0,
            key="listing_wizard_offer_minimum",
            disabled=not best_offer_enabled,
        )
    effective_offer_ceiling = float(auction_start or 0.0) if format_type == "AUCTION" else float(listing_price or 0.0)
    offer_rules_valid = True
    if bool(best_offer_enabled):
        if float(best_offer_auto_accept or 0.0) > 0 and float(best_offer_minimum or 0.0) > 0:
            if float(best_offer_minimum or 0.0) > float(best_offer_auto_accept or 0.0):
                offer_rules_valid = False
                st.warning("Offer rule check: auto-decline minimum cannot be greater than auto-accept.")
        if effective_offer_ceiling > 0:
            if float(best_offer_auto_accept or 0.0) > effective_offer_ceiling:
                offer_rules_valid = False
                st.warning("Offer rule check: auto-accept cannot exceed listing price/start price.")
            if float(best_offer_minimum or 0.0) > effective_offer_ceiling:
                offer_rules_valid = False
                st.warning("Offer rule check: auto-decline minimum cannot exceed listing price/start price.")

    st.markdown("#### Volume Pricing Tiers (Optional)")
    st.caption(
        "Store quantity discount tiers in draft metadata for eBay handoff/review."
    )
    if st.button("Load Discount Builder from JSON", key="listing_wizard_load_volume_discount_builder_btn"):
        loaded_tiers, _loaded_errors = _wizard_normalize_volume_pricing_tiers(
            st.session_state.get("listing_wizard_volume_pricing_json") or "",
            base_price=float(listing_price or 0.0)
            if str(format_type or "").strip().upper() == "FIXED_PRICE"
            else 0.0,
        )
        buy2, buy3, buy4 = _wizard_volume_pricing_discount_controls_from_tiers(loaded_tiers)
        st.session_state["listing_wizard_volume_discount_buy2"] = float(buy2)
        st.session_state["listing_wizard_volume_discount_buy3"] = float(buy3)
        st.session_state["listing_wizard_volume_discount_buy4"] = float(buy4)
        st.rerun()
    vp1, vp2, vp3 = st.columns(3)
    with vp1:
        st.number_input(
            "Buy 2 and save (%)",
            min_value=0.0,
            max_value=95.0,
            step=1.0,
            key="listing_wizard_volume_discount_buy2",
        )
    with vp2:
        st.number_input(
            "Buy 3 and save (%)",
            min_value=0.0,
            max_value=95.0,
            step=1.0,
            key="listing_wizard_volume_discount_buy3",
        )
    with vp3:
        st.number_input(
            "Buy 4+ and save (%)",
            min_value=0.0,
            max_value=95.0,
            step=1.0,
            key="listing_wizard_volume_discount_buy4",
        )
    vpb1, vpb2 = st.columns(2)
    with vpb1:
        if st.button("Apply Discount Builder", key="listing_wizard_apply_volume_discount_builder_btn"):
            st.session_state["listing_wizard_volume_pricing_json"] = _wizard_volume_pricing_json_from_discount_controls(
                buy2_percent=float(st.session_state.get("listing_wizard_volume_discount_buy2") or 0.0),
                buy3_percent=float(st.session_state.get("listing_wizard_volume_discount_buy3") or 0.0),
                buy4_percent=float(st.session_state.get("listing_wizard_volume_discount_buy4") or 0.0),
            )
            st.rerun()
    with vpb2:
        if st.button("Clear Volume Pricing", key="listing_wizard_clear_volume_pricing_btn"):
            st.session_state["listing_wizard_volume_pricing_json"] = ""
            st.rerun()
    st.text_area(
        "Volume Pricing JSON",
        key="listing_wizard_volume_pricing_json",
        height=100,
        placeholder='[{"min_qty": 2, "percent_off": 2}, {"min_qty": 3, "percent_off": 3}, {"min_qty": 5, "price": 4.60}]',
    )
    st.checkbox(
        "Append volume pricing block to listing details",
        key="listing_wizard_include_volume_pricing_in_details",
        help="Adds a buyer-visible quantity discount section to listing details text.",
    )
    volume_pricing_tiers, volume_pricing_errors = _wizard_normalize_volume_pricing_tiers(
        st.session_state.get("listing_wizard_volume_pricing_json") or "",
        base_price=float(listing_price or 0.0) if str(format_type or "").strip().upper() == "FIXED_PRICE" else 0.0,
    )
    if volume_pricing_tiers:
        st.caption(
            "Parsed tiers: "
            + " | ".join(
                [f"qty>={int(t['min_qty'])}: ${float(t['price']):,.2f}" for t in volume_pricing_tiers]
            )
        )
    if volume_pricing_errors:
        st.warning("Volume pricing validation: " + " | ".join(volume_pricing_errors[:5]))

    st.markdown("### Step 5 of 9: Images / Videos")
    listing_uploaded_by = st.text_input("Uploaded By", value=user.username, key="listing_wizard_uploaded_by")
    wizard_files = render_media_capture_inputs(
        key_prefix="listing_wizard_media",
        upload_label="Listing Photos/Videos (optional)",
        allow_enhanced=True,
    )
    selected_existing_media_ids: list[int] = []
    primary_image_options: dict[str, str] = {"Auto": ""}
    if selected_product is not None:
        product_media_rows = _product_media_rows_cached(int(selected_product.id))
        attachable_rows = sorted(
            [row for row in product_media_rows if row.listing_id in {None, 0}],
            key=lambda row: int(row.id),
            reverse=True,
        )
        with st.expander("Use Existing Product Media (no duplicate upload)", expanded=False):
            if not attachable_rows:
                st.caption("No existing product media available to attach.")
            else:
                st.caption(
                    "Select existing product media to link to this listing draft. "
                    "This reuses the same stored file and does not duplicate S3 objects."
                )
                media_options: dict[str, int] = {
                    (
                        f"#{int(row.id)} | {str(row.media_type or '').strip() or '-'} | "
                        f"{str(row.original_filename or '').strip() or '-'}"
                    ): int(row.id)
                    for row in attachable_rows
                }
                default_labels = list(media_options.keys())
                selected_labels = st.multiselect(
                    "Attach existing product media",
                    options=list(media_options.keys()),
                    default=default_labels,
                    key="listing_wizard_existing_media_select",
                )
                selected_existing_media_ids = [media_options[label] for label in selected_labels if label in media_options]
                selected_existing_image_labels = {
                    label: f"media:{media_options[label]}"
                    for label in selected_labels
                    if label in media_options
                    and str(
                        next(
                            (
                                row.media_type
                                for row in attachable_rows
                                if int(row.id) == int(media_options[label])
                            ),
                            "",
                        )
                        or ""
                    ).strip().lower()
                    == "image"
                }
                primary_image_options.update(selected_existing_image_labels)
                st.caption(
                    f"Selected existing media: {len(selected_existing_media_ids)} / {len(attachable_rows)}"
                )
                if selected_existing_media_ids:
                    show_selected_preview = st.checkbox(
                        "Show selected media preview",
                        value=False,
                        key="listing_wizard_show_selected_media_preview",
                    )
                    if show_selected_preview:
                        selected_map = {int(row.id): row for row in attachable_rows}
                        preview_rows = [selected_map[mid] for mid in selected_existing_media_ids if int(mid) in selected_map]
                        preview_cols = st.columns(3)
                        for idx, media_row in enumerate(preview_rows[:9]):
                            with preview_cols[idx % 3]:
                                st.caption(
                                    f"#{int(media_row.id)} • {str(media_row.media_type or '').strip()} • "
                                    f"{str(media_row.original_filename or '').strip()}"
                                )
                                media_type = str(media_row.media_type or "").strip().lower()
                                if media_type == "image":
                                    media_bytes, _preview_content_type, load_err = load_media_bytes(
                                        media_row,
                                        storage=storage,
                                    )
                                    if media_bytes is not None:
                                        try:
                                            st.image(media_bytes, use_container_width=True)
                                        except Exception:
                                            st.caption("Image preview unavailable (invalid image bytes).")
                                    else:
                                        st.caption("Image preview unavailable from private storage.")
                                        if str(load_err or "").strip():
                                            st.caption(f"Load warning: {str(load_err).strip()[:240]}")
                                elif media_type == "video":
                                    media_bytes, preview_content_type, load_err = load_media_bytes(
                                        media_row,
                                        storage=storage,
                                    )
                                    if media_bytes is not None:
                                        st.video(media_bytes, format=preview_content_type)
                                    else:
                                        st.caption("Video preview unavailable from private storage.")
                                        if str(load_err or "").strip():
                                            st.caption(f"Load warning: {str(load_err).strip()[:240]}")
                                else:
                                    st.caption("Unsupported preview type.")
    uploaded_image_options = {
        f"Upload | {str(getattr(file, 'name', '') or '').strip()}": (
            f"upload:{str(getattr(file, 'name', '') or '').strip()}"
        )
        for file in (wizard_files or [])
        if str(getattr(file, "type", "") or "").strip().lower().startswith("image/")
        and str(getattr(file, "name", "") or "").strip()
    }
    primary_image_options.update(uploaded_image_options)
    if len(primary_image_options) > 1:
        current_primary_ref = str(st.session_state.get("listing_wizard_primary_image_ref") or "").strip()
        primary_labels = list(primary_image_options.keys())
        current_primary_label = next(
            (label for label, ref in primary_image_options.items() if ref == current_primary_ref),
            "Auto",
        )
        primary_label = st.selectbox(
            "Main eBay Image",
            options=primary_labels,
            index=primary_labels.index(current_primary_label) if current_primary_label in primary_labels else 0,
            key="listing_wizard_primary_image_select",
        )
        st.session_state["listing_wizard_primary_image_ref"] = primary_image_options.get(primary_label, "")

    st.markdown("### Step 6 of 9: AI Draft Assist")
    fallback_enabled = bool(get_runtime_bool(repo, "ai_fallback_enabled", True))
    fallback_max_profiles = max(1, int(get_runtime_int(repo, "ai_fallback_max_profiles", 3)))
    try:
        ai_chain = resolve_comp_llm_runtime_chain(repo)
    except Exception:
        ai_chain = []
    ai_primary = ai_chain[0] if ai_chain else None
    ai_additional = max(0, len(ai_chain) - 1)
    st.caption(
        "AI runtime: "
        f"primary_provider={str(getattr(ai_primary, 'provider', '') or 'n/a')} | "
        f"primary_model={str(getattr(ai_primary, 'model', '') or 'n/a')} | "
        f"source={str(getattr(ai_primary, 'source', '') or 'n/a')} | "
        f"fallback_enabled={'yes' if fallback_enabled else 'no'} | "
        f"profiles_loaded={len(ai_chain)} (max={fallback_max_profiles}) | "
        f"additional_fallback_profiles={ai_additional}"
    )
    if not ai_chain:
        st.warning(
            "No executable AI runtime profiles detected. Configure AI Runtime profiles in Admin "
            "or environment defaults before using AI draft assist."
        )
    elif not fallback_enabled:
        st.info("AI fallback is disabled in runtime settings. Only the primary profile will be attempted.")

    use_selected_media_for_ai = st.checkbox(
        "Use selected existing media for AI draft context",
        value=True,
        key="listing_wizard_ai_use_selected_media",
        help="When enabled, AI uses the media selected above instead of arbitrary product media.",
    )
    ai_seed_default = get_runtime_str(
        repo,
        "listing_wizard_ai_seed_default",
        DEFAULT_LISTING_WIZARD_AI_SEED_PROMPT,
    ).strip() or get_runtime_str(
        repo,
        "listing_wizard_ai_instruction_template",
        DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE,
    ).strip()
    ai_seed_prev = str(st.session_state.get("listing_wizard_ai_seed_default_prev") or "").strip()
    ai_seed_current = str(st.session_state.get("listing_wizard_ai_seed") or "").strip()
    if (
        "listing_wizard_ai_seed" not in st.session_state
        or not ai_seed_current
        or ai_seed_current == ai_seed_prev
    ):
        st.session_state["listing_wizard_ai_seed"] = ai_seed_default
    st.session_state["listing_wizard_ai_seed_default_prev"] = ai_seed_default
    with st.expander("Advanced AI Prompt Controls", expanded=False):
        ai_seed = st.text_area(
            "AI Seed Prompt",
            key="listing_wizard_ai_seed",
            help="Example: write concise SEO-friendly title/details for eBay and suggest a conservative price.",
        )
    if st.button("Auto-Generate Title/Details/Pricing with AI", key="listing_wizard_ai_generate"):
        if not ensure_permission(user, "ai_comp_use", "Generate Listing Wizard AI Suggestions"):
            st.stop()
        if selected_product is None:
            st.warning("Select a product first.")
        else:
            ai_diag: dict[str, object] = {
                "attempted_modes": [],
                "used_runtime": {},
                "fallback_errors": [],
                "errors": [],
            }
            system_message = get_runtime_str(
                repo,
                "listing_wizard_ai_system_message",
                DEFAULT_LISTING_WIZARD_AI_SYSTEM_MESSAGE,
            ).strip()
            prompt = get_runtime_str(
                repo,
                "listing_wizard_ai_instruction_template",
                DEFAULT_LISTING_WIZARD_AI_INSTRUCTION_TEMPLATE,
            )
            query_parts = [
                str(ai_seed or "").strip(),
                f"SKU: {selected_product.sku}",
                f"Title: {selected_product.title}",
                f"Category: {selected_product.category}",
                f"Material: {selected_product.metal_type}",
                f"Weight oz: {selected_product.weight_oz}",
                f"Description: {selected_product.description}",
            ]
            query = "\n".join([p for p in query_parts if p]).strip()
            ebay_rows: list[dict] = []
            spot_context: dict = {}
            quick_comp_summary: dict[str, object] = {}

            include_quick_comp_context = str(
                get_runtime_str(repo, "listing_wizard_ai_include_quick_comp_context", "true")
            ).strip().lower() in {"1", "true", "yes", "y", "on"}
            if include_quick_comp_context:
                quick_comp_query = str(selected_product.title or listing_title or "").strip()
                quick_comp_limit = max(
                    1,
                    min(
                        20,
                        int(get_runtime_int(repo, "listing_wizard_ai_quick_comp_limit", 8)),
                    ),
                )
                quick_comp_rate_limited_note = ""
                if quick_comp_query:
                    try:
                        ebay = EbayClient(environment=settings.app_env)
                        if ebay.is_configured():
                            comp_outcome = ebay.find_completed_items_with_fallback(
                                keywords=quick_comp_query,
                                sold_only=True,
                                entries_per_page=quick_comp_limit,
                                source="listing_wizard_ai",
                                auto_broaden=True,
                                allow_html_fallback=True,
                            )
                            ebay_rows = list(comp_outcome.get("rows") or [])
                            quick_comp_rate_limited_note = str(comp_outcome.get("rate_limited_note") or "").strip()
                    except RuntimeError as exc:
                        if str(exc).startswith("EBAY_FINDING_RATE_LIMITED"):
                            quick_comp_rate_limited_note = str(exc)
                            ebay_rows = []
                        else:
                            ebay_rows = []
                    except Exception:
                        ebay_rows = []
                if ebay_rows:
                    totals = [
                        float(row.get("total_price") or 0.0)
                        for row in ebay_rows
                        if float(row.get("total_price") or 0.0) > 0
                    ]
                    if totals:
                        try:
                            med_total = float(statistics.median(totals))
                        except Exception:
                            med_total = 0.0
                        avg_total = float(sum(totals) / len(totals)) if totals else 0.0
                        quick_comp_summary = {
                            "count": int(len(ebay_rows)),
                            "priced_count": int(len(totals)),
                            "median_total": float(med_total),
                            "avg_total": float(avg_total),
                            "min_total": float(min(totals)) if totals else 0.0,
                            "max_total": float(max(totals)) if totals else 0.0,
                            "query": quick_comp_query,
                        }
                        query += (
                            "\n\nQuick sold-comp context (eBay): "
                            f"{len(ebay_rows)} row(s), median_total=${med_total:,.2f}, "
                            f"avg_total=${avg_total:,.2f}."
                        )
                    else:
                        query += f"\n\nQuick sold-comp context (eBay): {len(ebay_rows)} row(s), no positive totals parsed."
                else:
                    if quick_comp_rate_limited_note:
                        quick_comp_summary["rate_limited"] = True
                        quick_comp_summary["rate_limited_note"] = quick_comp_rate_limited_note
                        query += "\n\nQuick sold-comp context (eBay): temporarily rate-limited; proceeding without eBay sold rows."
                    else:
                        query += "\n\nQuick sold-comp context (eBay): unavailable/empty."

                try:
                    spot_service = SpotPriceService(repo=repo)
                    if spot_service.is_configured():
                        quotes = spot_service.latest_quotes()
                        spot_context["quotes_usd_per_troy_oz"] = {
                            k: float(v.usd_per_troy_oz) for k, v in quotes.items()
                        }
                        any_quote = next(iter(quotes.values())) if quotes else None
                        if any_quote is not None:
                            spot_context["as_of"] = any_quote.as_of.isoformat()
                            spot_context["source"] = any_quote.source
                except Exception as exc:
                    spot_context["fetch_error"] = str(exc)
                metal_hint = str(selected_product.metal_type or "").strip().lower()
                if metal_hint:
                    spot_context["detected_metal"] = metal_hint
                if spot_context:
                    quick_comp_summary["spot_source"] = str(spot_context.get("source") or "").strip()
                    quick_comp_summary["spot_as_of"] = str(spot_context.get("as_of") or "").strip()
                    quick_comp_summary["spot_detected_metal"] = str(spot_context.get("detected_metal") or "").strip()
                    quick_comp_summary["spot_quotes"] = dict(spot_context.get("quotes_usd_per_troy_oz") or {})
                    query += "\nSpot context: " + json.dumps(spot_context)

            uploaded_images, uploaded_videos = _collect_uploaded_media_context(wizard_files)
            max_product_images = max(1, min(8, int(get_runtime_int(repo, "listing_wizard_ai_max_product_images", 3))))
            max_ai_images_total = max(1, min(8, int(get_runtime_int(repo, "listing_wizard_ai_max_images_total", 4))))
            if use_selected_media_for_ai and selected_existing_media_ids:
                product_media_ctx = _load_selected_product_media_context(
                    repo,
                    storage,
                    int(selected_product.id),
                    selected_existing_media_ids,
                    limit=max_product_images,
                )
            else:
                product_media_ctx = _load_product_media_context(
                    repo,
                    storage,
                    int(selected_product.id),
                    limit=max_product_images,
                )
            product_images = list(product_media_ctx.get("images") or [])
            product_videos = list(product_media_ctx.get("videos") or [])
            image_inputs = (uploaded_images + product_images)[:max_ai_images_total]
            st.caption(
                "AI media inputs: "
                f"{len(image_inputs)} image(s) sent to multimodal "
                f"(uploaded={len(uploaded_images)}, product={len(product_images)}, cap={max_ai_images_total}); "
                f"video context: uploaded={len(uploaded_videos)}, product={len(product_videos)}."
            )
            query += (
                "\n\nAttached media context:\n"
                f"- Uploaded images in this run: {len(uploaded_images)}\n"
                f"- Uploaded videos in this run: {len(uploaded_videos)}\n"
                f"- Product attached images: {int(product_media_ctx.get('image_count') or 0)}\n"
                f"- Product attached videos: {int(product_media_ctx.get('video_count') or 0)}"
            )
            if uploaded_videos:
                query += "\n- Uploaded video files: " + ", ".join(
                    [str(v.get("filename") or "").strip() for v in uploaded_videos[:5] if str(v.get("filename") or "").strip()]
                )
            if product_videos:
                query += "\n- Product video files: " + ", ".join(
                    [str(v.get("filename") or "").strip() for v in product_videos[:5] if str(v.get("filename") or "").strip()]
                )

            payload = {}
            if image_inputs:
                ai_diag["attempted_modes"] = [*list(ai_diag.get("attempted_modes") or []), "multimodal"]
                try:
                    primary_bytes, primary_content_type = image_inputs[0]
                    additional_images = image_inputs[1:] if len(image_inputs) > 1 else []
                    mm_attempts: list[tuple[str, str, list[tuple[bytes, str]], dict[str, object]]] = [
                        (
                            "listing_full_images",
                            "listing",
                            additional_images,
                            {"workflow": "listing_wizard", "product_id": int(selected_product.id)},
                        )
                    ]
                    if additional_images:
                        mm_attempts.append(
                            (
                                "listing_primary_only",
                                "listing",
                                [],
                                {
                                    "workflow": "listing_wizard",
                                    "product_id": int(selected_product.id),
                                    "fallback_from_attempt": "listing_full_images",
                                },
                            )
                        )
                    mm_attempts.append(
                        (
                            "comp_primary_only",
                            "comp",
                            [],
                            {
                                "workflow": "listing_wizard",
                                "product_id": int(selected_product.id),
                                "fallback_from_workflow": "listing",
                            },
                        )
                    )

                    mm_result = None
                    mm_workflow = "listing"
                    mm_attempt = ""
                    last_mm_exc: Exception | None = None
                    for attempt_label, attempt_workflow, attempt_additional_images, attempt_context in mm_attempts:
                        try:
                            mm_result = execute_multimodal_task(
                                repo,
                                tool_name="listing_wizard_ai_draft",
                                system_message=system_message,
                                instruction=prompt + "\n\nProduct context:\n" + query,
                                image_bytes=primary_bytes,
                                image_content_type=primary_content_type,
                                additional_images=attempt_additional_images,
                                workflow=attempt_workflow,
                                context=attempt_context,
                            )
                            mm_workflow = attempt_workflow
                            mm_attempt = attempt_label
                            break
                        except Exception as attempt_exc:
                            last_mm_exc = attempt_exc
                            ai_diag["errors"] = [
                                *list(ai_diag.get("errors") or []),
                                f"multimodal {attempt_label}: {attempt_exc}",
                            ]
                    if mm_result is None:
                        raise RuntimeError(str(last_mm_exc or "unknown multimodal error"))
                    ai_diag["used_runtime"] = {
                        "mode": "multimodal",
                        "workflow": mm_workflow,
                        "attempt": mm_attempt,
                        "provider": mm_result.used_config.provider,
                        "source": mm_result.used_config.source,
                        "model": mm_result.used_config.multimodal_model or mm_result.used_config.model,
                        "endpoint_type": mm_result.used_config.endpoint_type,
                    }
                    ai_diag["fallback_errors"] = list(mm_result.fallback_errors or [])
                    payload = _normalize_ai_suggestion_payload(_try_json(mm_result.text))
                    if not payload:
                        st.warning("AI multimodal output was not valid JSON. Showing raw output.")
                        st.code(mm_result.text)
                except Exception as exc:
                    ai_diag["errors"] = [*list(ai_diag.get("errors") or []), f"multimodal: {exc}"]
                    st.info(
                        "Multimodal AI draft is temporarily unavailable; continuing with text-only draft generation. "
                        f"Reason: {exc}"
                    )

            if not payload:
                ai_diag["attempted_modes"] = [*list(ai_diag.get("attempted_modes") or []), "text"]
                try:
                    txt_result = execute_comp_summary(
                        repo,
                        query=query,
                        ebay_rows=ebay_rows,
                        web_rows=[],
                        spot_context=spot_context,
                        system_message=system_message,
                        instruction=prompt,
                        workflow="listing",
                    )
                    ai_diag["used_runtime"] = {
                        "mode": "text",
                        "provider": txt_result.used_config.provider,
                        "source": txt_result.used_config.source,
                        "model": txt_result.used_config.model,
                        "endpoint_type": txt_result.used_config.endpoint_type,
                    }
                    ai_diag["fallback_errors"] = list(txt_result.fallback_errors or [])
                    payload = _normalize_ai_suggestion_payload(_try_json(txt_result.text))
                    if not payload:
                        st.warning("AI text output was not valid JSON. Showing raw output.")
                        st.code(txt_result.text)
                except Exception as exc:
                    ai_diag["errors"] = [*list(ai_diag.get("errors") or []), f"text: {exc}"]
                    st.error(f"AI draft generation failed. {exc}")

            st.session_state["listing_wizard_ai_diagnostics"] = ai_diag
            st.session_state["listing_wizard_ai_comp_evidence"] = quick_comp_summary
            st.session_state["listing_wizard_ai_has_run"] = True
            if payload:
                st.session_state["listing_wizard_ai_suggestions"] = payload
                st.success("AI suggestions generated. Review and apply selected fields below.")
                st.rerun()

    ai_diag_snapshot = st.session_state.get("listing_wizard_ai_diagnostics")
    ai_comp_evidence = st.session_state.get("listing_wizard_ai_comp_evidence")
    ai_has_run = bool(st.session_state.get("listing_wizard_ai_has_run"))
    show_debug_panels = False
    if ai_has_run and (isinstance(ai_diag_snapshot, dict) or isinstance(ai_comp_evidence, dict)):
        show_debug_panels = st.checkbox(
            "Show AI troubleshooting panels",
            value=False,
            key="listing_wizard_ai_show_debug_panels",
            help="Shows runtime diagnostics and quick pricing evidence from the most recent AI run.",
        )
    if show_debug_panels and isinstance(ai_diag_snapshot, dict) and (
        ai_diag_snapshot.get("used_runtime")
        or ai_diag_snapshot.get("errors")
        or ai_diag_snapshot.get("fallback_errors")
    ):
        with st.expander("AI Runtime Diagnostics", expanded=False):
            used_runtime = ai_diag_snapshot.get("used_runtime") or {}
            u1, u2, u3, u4 = st.columns(4)
            u1.metric("Mode", str(used_runtime.get("mode") or "n/a"))
            u2.metric("Provider", str(used_runtime.get("provider") or "n/a"))
            u3.metric("Model", str(used_runtime.get("model") or "n/a"))
            u4.metric("Fallback Errors", str(len(list(ai_diag_snapshot.get("fallback_errors") or []))))
            st.caption(
                "Source: "
                f"{str(used_runtime.get('source') or 'n/a')} | Endpoint: "
                f"{str(used_runtime.get('endpoint_type') or 'n/a')} | "
                f"Attempted modes: {', '.join(list(ai_diag_snapshot.get('attempted_modes') or [])) or 'n/a'}"
            )
            fallback_errors = list(ai_diag_snapshot.get("fallback_errors") or [])
            if fallback_errors:
                st.warning("Fallback attempts:\n- " + "\n- ".join([str(v) for v in fallback_errors]))
            hard_errors = list(ai_diag_snapshot.get("errors") or [])
            if hard_errors:
                st.error("Run errors:\n- " + "\n- ".join([str(v) for v in hard_errors]))
    if show_debug_panels and isinstance(ai_comp_evidence, dict) and ai_comp_evidence:
        with st.expander("Pricing Evidence (Quick Context)", expanded=False):
            e1, e2, e3, e4 = st.columns(4)
            e1.metric("Comp Rows", str(int(ai_comp_evidence.get("count") or 0)))
            e2.metric("Median Total", f"${float(ai_comp_evidence.get('median_total') or 0):,.2f}")
            e3.metric("Average Total", f"${float(ai_comp_evidence.get('avg_total') or 0):,.2f}")
            e4.metric(
                "Range",
                (
                    f"${float(ai_comp_evidence.get('min_total') or 0):,.2f} - "
                    f"${float(ai_comp_evidence.get('max_total') or 0):,.2f}"
                ),
            )
            spot_source = str(ai_comp_evidence.get("spot_source") or "").strip()
            if spot_source:
                st.caption(
                    "Spot context: "
                    f"source={spot_source} | as_of={str(ai_comp_evidence.get('spot_as_of') or '').strip() or '-'} | "
                    f"metal_hint={str(ai_comp_evidence.get('spot_detected_metal') or '').strip() or '-'}"
                )
            if ai_comp_evidence.get("query"):
                st.caption(f"Quick comp query: {ai_comp_evidence.get('query')}")

    ai_suggestions = st.session_state.get("listing_wizard_ai_suggestions")
    if isinstance(ai_suggestions, dict) and ai_suggestions:
        st.markdown("#### AI Suggestions Review")
        risk_summary = str(ai_suggestions.get("risk_summary") or "").strip()
        if risk_summary:
            st.info(f"AI risk summary: {risk_summary}")
        suggested_price, suggested_price_low, suggested_price_high = _suggested_price_band(ai_suggestions)
        pb1, pb2, pb3 = st.columns(3)
        pb1.metric("AI Suggested Price", f"${suggested_price:,.2f}" if suggested_price > 0 else "-")
        pb2.metric("AI Price Low", f"${suggested_price_low:,.2f}" if suggested_price_low > 0 else "-")
        pb3.metric("AI Price High", f"${suggested_price_high:,.2f}" if suggested_price_high > 0 else "-")
        a1, a2, a3 = st.columns(3)
        with a1:
            apply_title = st.checkbox("Apply Suggested Title", value=True, key="listing_wizard_ai_apply_title")
        with a2:
            apply_details = st.checkbox("Apply Suggested Details", value=True, key="listing_wizard_ai_apply_details")
        with a3:
            apply_price_offer = st.checkbox("Apply Price/Offers", value=True, key="listing_wizard_ai_apply_price_offer")
        price_apply_mode = st.selectbox(
            "Price Apply Mode",
            options=["mid", "low", "high"],
            index=0,
            format_func=lambda v: {"mid": "Mid (Balanced)", "low": "Low (Conservative)", "high": "High (Aggressive)"}.get(v, v),
            key="listing_wizard_ai_price_apply_mode",
            disabled=not apply_price_offer,
            help="Choose which point in the AI price band should be applied to listing price.",
        )
        apply_only_empty = st.checkbox(
            "Apply Only To Empty Fields",
            value=False,
            key="listing_wizard_ai_apply_only_empty_fields",
            help="When enabled, AI suggestions are applied only where the current field is blank/zero.",
        )

        compact_title = str(ai_suggestions.get("suggested_title") or "").strip()
        compact_details_raw = _resolve_ai_suggested_details(ai_suggestions, fallback="")
        used_details_fallback = _is_weak_listing_details(compact_details_raw, policy=quality_policy)
        compact_details = (
            _build_fallback_ebay_listing_details(
                selected_product,
                title=compact_title or listing_title,
                existing_description=listing_details,
            )
            if used_details_fallback
            else compact_details_raw
        )
        cs1, cs2 = st.columns(2)
        with cs1:
            st.caption("Suggested Title Preview")
            st.write(compact_title[:120] + ("..." if len(compact_title) > 120 else "") if compact_title else "-")
            st.caption("Suggested Offer")
            st.write(
                (
                    f"Enabled={bool(ai_suggestions.get('best_offer_enabled'))} | "
                    f"AutoAccept=${float(_safe_price_float(ai_suggestions.get('best_offer_auto_accept'), 0.0)):,.2f} | "
                    f"Min=${float(_safe_price_float(ai_suggestions.get('best_offer_minimum'), 0.0)):,.2f}"
                )
            )
        with cs2:
            st.caption("Suggested Details Preview")
            if used_details_fallback:
                st.caption("AI details were too short; showing enriched eBay-ready fallback copy.")
            st.write(compact_details[:220] + ("..." if len(compact_details) > 220 else "") if compact_details else "-")

        show_full_ai = st.checkbox(
            "Show full AI suggestion payload",
            value=False,
            key="listing_wizard_ai_show_full_payload",
        )
        if show_full_ai:
            st.code(
                json.dumps(
                    {
                        "suggested_title": str(ai_suggestions.get("suggested_title") or ""),
                        "suggested_details": compact_details,
                        "suggested_marketplace_details": str(ai_suggestions.get("suggested_marketplace_details") or ""),
                        "suggested_price": suggested_price,
                        "suggested_price_low": suggested_price_low,
                        "suggested_price_high": suggested_price_high,
                        "best_offer_enabled": bool(ai_suggestions.get("best_offer_enabled")),
                        "best_offer_auto_accept": _safe_price_float(ai_suggestions.get("best_offer_auto_accept"), 0.0),
                        "best_offer_minimum": _safe_price_float(ai_suggestions.get("best_offer_minimum"), 0.0),
                        "risk_summary": risk_summary,
                    },
                    indent=2,
                )
            )

        aa1, aa2 = st.columns(2)
        with aa1:
            if st.button("Apply Selected AI Suggestions", key="listing_wizard_ai_apply_selected_btn"):
                title_trimmed_on_apply = False
                weak_ai_title = False
                pending_updates: dict[str, object] = {}
                missing_ai_details = False
                blocked_terms: list[str] = []
                blocked_detail_terms: list[str] = []
                if apply_title:
                    current_title = str(st.session_state.get("listing_wizard_title") or "").strip()
                    if not apply_only_empty or not current_title:
                        next_title = str(
                            ai_suggestions.get("suggested_title")
                            or ai_suggestions.get("title")
                            or ai_suggestions.get("suggested_listing_title")
                            or listing_title
                        ).strip()
                        blocked_terms = find_forbidden_terms(next_title, policy=quality_policy)
                        if is_weak_listing_title(next_title, policy=quality_policy):
                            weak_ai_title = True
                            next_title = ""
                        if len(next_title) > EBAY_TITLE_MAX_CHARS:
                            next_title = next_title[:EBAY_TITLE_MAX_CHARS].rstrip(" -_,.;:")
                            title_trimmed_on_apply = True
                        if next_title:
                            pending_updates["listing_wizard_title"] = next_title
                if apply_details:
                    current_details = str(st.session_state.get("listing_wizard_details") or "").strip()
                    if not apply_only_empty or not current_details:
                        resolved_details = _resolve_ai_suggested_details(ai_suggestions, fallback="")
                        blocked_detail_terms = find_forbidden_terms(
                            resolved_details,
                            policy=quality_policy,
                        )
                        if resolved_details and not _is_weak_listing_details(
                            resolved_details,
                            policy=quality_policy,
                        ):
                            pending_updates["listing_wizard_details"] = resolved_details
                        elif resolved_details:
                            pending_updates["listing_wizard_details"] = _build_fallback_ebay_listing_details(
                                selected_product,
                                title=compact_title or listing_title,
                                existing_description=listing_details,
                            )
                        else:
                            pending_updates["listing_wizard_details"] = _build_fallback_ebay_listing_details(
                                selected_product,
                                title=compact_title or listing_title,
                                existing_description=listing_details,
                            )
                            missing_ai_details = True
                    current_subtitle = str(st.session_state.get("listing_wizard_subtitle") or "").strip()
                    next_subtitle = str(
                        ai_suggestions.get("subtitle")
                        or ai_suggestions.get("suggested_subtitle")
                        or ""
                    ).strip()
                    if next_subtitle and (not apply_only_empty or not current_subtitle):
                        pending_updates["listing_wizard_subtitle"] = next_subtitle
                    current_condition_desc = str(st.session_state.get("listing_wizard_condition_description") or "").strip()
                    next_condition_desc = str(
                        ai_suggestions.get("condition_description")
                        or ai_suggestions.get("suggested_condition_description")
                        or ai_suggestions.get("ai_grading_description")
                        or ai_suggestions.get("grading_description")
                        or ""
                    ).strip()
                    if next_condition_desc and (not apply_only_empty or not current_condition_desc):
                        pending_updates["listing_wizard_condition_description"] = next_condition_desc
                    current_aspects_json = str(st.session_state.get("listing_wizard_aspects_json") or "").strip()
                    next_aspects = ai_suggestions.get("aspects")
                    if isinstance(next_aspects, dict):
                        next_aspects_json = json.dumps(next_aspects, indent=2)
                        if not apply_only_empty or not current_aspects_json:
                            pending_updates["listing_wizard_aspects_json"] = next_aspects_json
                if apply_price_offer:
                    try:
                        current_price = float(st.session_state.get("listing_wizard_price") or 0.0)
                        resolved_price = suggested_price if suggested_price > 0 else 0.0
                        if str(price_apply_mode or "mid").strip().lower() == "low" and suggested_price_low > 0:
                            resolved_price = float(suggested_price_low)
                        elif str(price_apply_mode or "mid").strip().lower() == "high" and suggested_price_high > 0:
                            resolved_price = float(suggested_price_high)
                        elif suggested_price > 0:
                            resolved_price = float(suggested_price)
                        if resolved_price > 0 and (not apply_only_empty or current_price <= 0.0):
                            pending_updates["listing_wizard_price"] = float(resolved_price)
                    except Exception:
                        pass
                    if "best_offer_enabled" in ai_suggestions:
                        current_offer_enabled = bool(
                            st.session_state.get("listing_wizard_offer_enabled") or False
                        )
                        if not apply_only_empty or not current_offer_enabled:
                            pending_updates["listing_wizard_offer_enabled"] = bool(
                                ai_suggestions.get("best_offer_enabled")
                            )
                    try:
                        current_auto_accept = float(
                            st.session_state.get("listing_wizard_offer_auto_accept") or 0.0
                        )
                        current_minimum = float(
                            st.session_state.get("listing_wizard_offer_minimum") or 0.0
                        )
                        if ai_suggestions.get("best_offer_auto_accept") is not None:
                            if not apply_only_empty or current_auto_accept <= 0.0:
                                pending_updates["listing_wizard_offer_auto_accept"] = float(
                                    to_decimal(ai_suggestions.get("best_offer_auto_accept"))
                                )
                        if ai_suggestions.get("best_offer_minimum") is not None:
                            if not apply_only_empty or current_minimum <= 0.0:
                                pending_updates["listing_wizard_offer_minimum"] = float(
                                    to_decimal(ai_suggestions.get("best_offer_minimum"))
                                )
                    except Exception:
                        pass
                if pending_updates:
                    st.session_state["listing_wizard_pending_field_updates"] = pending_updates
                    apply_flash = "Applied selected AI fields."
                    if missing_ai_details and apply_details:
                        apply_flash += " (No usable AI details were returned; applied enriched fallback details.)"
                    if weak_ai_title and apply_title:
                        apply_flash += " (AI title was weak/generic or violated policy; kept existing title.)"
                    if apply_title and blocked_terms:
                        apply_flash += f" (Blocked title term(s): {', '.join(blocked_terms[:4])})"
                    if apply_details and blocked_detail_terms:
                        apply_flash += f" (Blocked detail term(s): {', '.join(blocked_detail_terms[:4])}; fallback copy applied.)"
                else:
                    if missing_ai_details and apply_details:
                        apply_flash = "No usable AI details were returned; generated fallback details."
                    elif weak_ai_title and apply_title:
                        apply_flash = "AI title was too weak/generic or violated policy; no title update applied."
                    else:
                        apply_flash = "No AI field updates applied (suggestions may be empty or fields already populated)."
                snapshot = {
                    "title": str(
                        pending_updates.get(
                            "listing_wizard_title",
                            st.session_state.get("listing_wizard_title") or "",
                        )
                    ).strip(),
                    "details": str(
                        pending_updates.get(
                            "listing_wizard_details",
                            st.session_state.get("listing_wizard_details") or "",
                        )
                    ).strip(),
                    "price": float(
                        pending_updates.get(
                            "listing_wizard_price",
                            st.session_state.get("listing_wizard_price") or 0.0,
                        )
                        or 0.0
                    ),
                    "offer_enabled": bool(
                        pending_updates.get(
                            "listing_wizard_offer_enabled",
                            st.session_state.get("listing_wizard_offer_enabled") or False,
                        )
                    ),
                    "offer_auto_accept": float(
                        pending_updates.get(
                            "listing_wizard_offer_auto_accept",
                            st.session_state.get("listing_wizard_offer_auto_accept") or 0.0,
                        )
                        or 0.0
                    ),
                    "offer_minimum": float(
                        pending_updates.get(
                            "listing_wizard_offer_minimum",
                            st.session_state.get("listing_wizard_offer_minimum") or 0.0,
                        )
                        or 0.0
                    ),
                }
                st.session_state["listing_wizard_ai_acceptance"] = {
                    "accepted_at": utcnow_naive().isoformat(),
                    "accepted_by": user.username,
                    "prompt_version_id": str(
                        get_runtime_str(repo, "ai_prompt_active_version_listing", "") or ""
                    ).strip(),
                    "apply_only_empty": bool(apply_only_empty),
                    "applied_fields": {
                        "title": bool(apply_title),
                        "details": bool(apply_details),
                        "price_offer": bool(apply_price_offer),
                        "price_apply_mode": str(price_apply_mode or "mid"),
                        "title_trimmed_to_ebay_max": bool(title_trimmed_on_apply),
                    },
                    "runtime": dict(st.session_state.get("listing_wizard_ai_diagnostics") or {}),
                    "snapshot": snapshot,
                }
                try:
                    repo.record_audit_event(
                        entity_type="ai_prompt_acceptance",
                        entity_id=None,
                        action="listing_wizard_apply",
                        actor=user.username,
                        changes={
                            "workflow": "listing_wizard",
                            "accepted": True,
                            "apply_flash": apply_flash,
                            "acceptance": st.session_state["listing_wizard_ai_acceptance"],
                        },
                    )
                except Exception:
                    pass
                st.session_state["listing_wizard_apply_flash"] = apply_flash
                st.session_state["listing_wizard_apply_title_trimmed_flash"] = bool(title_trimmed_on_apply)
                st.rerun()
        with aa2:
            if st.button("Clear AI Suggestions", key="listing_wizard_ai_clear_btn"):
                st.session_state.pop("listing_wizard_ai_suggestions", None)
                st.session_state.pop("listing_wizard_ai_diagnostics", None)
                st.session_state.pop("listing_wizard_ai_acceptance", None)
                st.session_state.pop("listing_wizard_ai_comp_evidence", None)
                st.session_state.pop("listing_wizard_ai_has_run", None)
                st.session_state.pop("listing_wizard_ai_show_debug_panels", None)
                st.rerun()
    else:
        st.caption(
            "Next recommended action: run `Auto-Generate Title/Details/Pricing with AI` "
            "(optional), then continue to preflight."
        )

    st.markdown("### Step 7 of 9: Readiness Preflight")
    st.markdown("#### Direct eBay Post Settings (Optional)")
    default_marketplace_id = str(
        get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id) or "EBAY_US"
    ).strip() or "EBAY_US"
    default_currency = str(get_runtime_str(repo, "ebay_currency", settings.ebay_currency) or "USD").strip() or "USD"
    default_content_language = str(
        get_runtime_str(repo, "ebay_content_language", settings.ebay_content_language) or "en-US"
    ).strip() or "en-US"
    st.session_state.setdefault(
        "listing_wizard_ebay_access_token",
        str(get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token) or "").strip(),
    )
    st.session_state.setdefault(
        "listing_wizard_ebay_merchant_location_key",
        str(get_runtime_str(repo, "ebay_merchant_location_key", settings.ebay_merchant_location_key) or "").strip(),
    )
    st.session_state.setdefault(
        "listing_wizard_ebay_payment_policy_id",
        str(get_runtime_str(repo, "ebay_payment_policy_id", settings.ebay_payment_policy_id) or "").strip(),
    )
    st.session_state.setdefault(
        "listing_wizard_ebay_fulfillment_policy_id",
        str(get_runtime_str(repo, "ebay_fulfillment_policy_id", settings.ebay_fulfillment_policy_id) or "").strip(),
    )
    st.session_state.setdefault(
        "listing_wizard_ebay_return_policy_id",
        str(get_runtime_str(repo, "ebay_return_policy_id", settings.ebay_return_policy_id) or "").strip(),
    )
    st.session_state.setdefault("listing_wizard_ebay_marketplace_id", default_marketplace_id)
    st.session_state.setdefault("listing_wizard_ebay_currency", default_currency)
    st.session_state.setdefault("listing_wizard_ebay_content_language", default_content_language)
    st.session_state.setdefault("listing_wizard_ebay_condition", "NEW")
    st.session_state.setdefault("listing_wizard_ebay_use_eps_images", True)
    st.session_state.setdefault("listing_wizard_ebay_upload_video", True)
    st.session_state.setdefault("listing_wizard_include_volume_pricing_in_details", False)
    st.session_state.setdefault("listing_wizard_volume_discount_buy2", 0.0)
    st.session_state.setdefault("listing_wizard_volume_discount_buy3", 0.0)
    st.session_state.setdefault("listing_wizard_volume_discount_buy4", 0.0)
    ebay_post_profiles = _load_wizard_ebay_post_profiles(repo, user.username)
    default_ebay_post_profile = str(
        get_runtime_str(repo, _wizard_ebay_post_default_key(user.username), "") or ""
    ).strip()
    ebay_profile_names = sorted(ebay_post_profiles.keys(), key=lambda v: v.lower())
    if default_ebay_post_profile and default_ebay_post_profile in ebay_post_profiles:
        ebay_profile_names = [default_ebay_post_profile] + [
            n for n in ebay_profile_names if n != default_ebay_post_profile
        ]
    ebay_profile_options = ["(none)", *ebay_profile_names]
    selected_ebay_profile = st.selectbox(
        "Wizard eBay Post Profile",
        options=ebay_profile_options,
        index=(
            ebay_profile_options.index(default_ebay_post_profile)
            if default_ebay_post_profile in ebay_profile_options
            else 0
        ),
        key="listing_wizard_ebay_post_profile_selected",
        help="Save/load direct-post defaults for this wizard (does not store access token).",
    )
    ep1, ep2, ep3, ep4 = st.columns(4)
    with ep1:
        if st.button("Apply eBay Profile", key="listing_wizard_ebay_profile_apply_btn"):
            chosen = str(selected_ebay_profile or "").strip()
            if chosen and chosen != "(none)" and chosen in ebay_post_profiles:
                payload = ebay_post_profiles[chosen]
                st.session_state["listing_wizard_ebay_merchant_location_key"] = str(
                    payload.get("merchant_location_key") or ""
                ).strip()
                st.session_state["listing_wizard_ebay_payment_policy_id"] = str(
                    payload.get("payment_policy_id") or ""
                ).strip()
                st.session_state["listing_wizard_ebay_fulfillment_policy_id"] = str(
                    payload.get("fulfillment_policy_id") or ""
                ).strip()
                st.session_state["listing_wizard_ebay_return_policy_id"] = str(
                    payload.get("return_policy_id") or ""
                ).strip()
                st.session_state["listing_wizard_ebay_marketplace_id"] = str(
                    payload.get("marketplace_id") or "EBAY_US"
                ).strip()
                st.session_state["listing_wizard_ebay_currency"] = str(payload.get("currency") or "USD").strip()
                st.session_state["listing_wizard_ebay_content_language"] = str(
                    payload.get("content_language") or "en-US"
                ).strip()
                st.session_state["listing_wizard_ebay_condition"] = str(payload.get("condition") or "NEW").strip()
                st.session_state["listing_wizard_ebay_use_eps_images"] = bool(payload.get("use_eps_images"))
                st.session_state["listing_wizard_ebay_upload_video"] = bool(payload.get("upload_video_to_ebay", True))
                saved_cat = str(payload.get("category_id") or "").strip()
                if saved_cat:
                    _wizard_queue_pending_field_updates({"listing_wizard_category_id": saved_cat})
                st.session_state["listing_wizard_store_category_names"] = _wizard_normalize_store_category_names(
                    payload.get("store_category_names")
                )
                st.success(f"Applied wizard eBay post profile `{chosen}`.")
                st.rerun()
            else:
                st.info("Select a saved profile first.")
    with ep2:
        if st.button("Set Default eBay Profile", key="listing_wizard_ebay_profile_set_default_btn"):
            chosen = str(selected_ebay_profile or "").strip()
            if chosen and chosen != "(none)" and chosen in ebay_post_profiles:
                _set_wizard_ebay_post_default_profile(
                    repo,
                    username=user.username,
                    actor=user.username,
                    profile_name=chosen,
                )
                st.success(f"Default eBay post profile set to `{chosen}`.")
                st.rerun()
            else:
                st.info("Select a saved profile first.")
    with ep3:
        ebay_profile_name_input = st.text_input(
            "eBay Profile Name",
            value=str(st.session_state.get("listing_wizard_ebay_profile_name") or "").strip(),
            key="listing_wizard_ebay_profile_name",
            placeholder="e.g. Coins Main Store",
        ).strip()
        if st.button("Save Current eBay Settings", key="listing_wizard_ebay_profile_save_btn"):
            if not ebay_profile_name_input:
                st.warning("Enter a profile name to save.")
            else:
                next_profiles = dict(ebay_post_profiles)
                next_profiles[ebay_profile_name_input] = {
                    "merchant_location_key": str(
                        st.session_state.get("listing_wizard_ebay_merchant_location_key") or ""
                    ).strip(),
                    "payment_policy_id": str(st.session_state.get("listing_wizard_ebay_payment_policy_id") or "").strip(),
                    "fulfillment_policy_id": str(
                        st.session_state.get("listing_wizard_ebay_fulfillment_policy_id") or ""
                    ).strip(),
                    "return_policy_id": str(st.session_state.get("listing_wizard_ebay_return_policy_id") or "").strip(),
                    "marketplace_id": str(st.session_state.get("listing_wizard_ebay_marketplace_id") or "EBAY_US").strip(),
                    "currency": str(st.session_state.get("listing_wizard_ebay_currency") or "USD").strip(),
                    "content_language": str(
                        st.session_state.get("listing_wizard_ebay_content_language") or "en-US"
                    ).strip(),
                    "condition": str(st.session_state.get("listing_wizard_ebay_condition") or "NEW").strip().upper(),
                    "use_eps_images": bool(st.session_state.get("listing_wizard_ebay_use_eps_images")),
                    "upload_video_to_ebay": bool(st.session_state.get("listing_wizard_ebay_upload_video")),
                    "category_id": str(st.session_state.get("listing_wizard_category_id") or "").strip(),
                    "store_category_names": _wizard_normalize_store_category_names(
                        st.session_state.get("listing_wizard_store_category_names")
                    ),
                }
                _save_wizard_ebay_post_profiles(
                    repo,
                    username=user.username,
                    actor=user.username,
                    profiles=next_profiles,
                )
                st.success(f"Saved wizard eBay post profile `{ebay_profile_name_input}`.")
                st.rerun()
    with ep4:
        if st.button("Delete eBay Profile", key="listing_wizard_ebay_profile_delete_btn"):
            chosen = str(selected_ebay_profile or "").strip()
            if chosen and chosen != "(none)" and chosen in ebay_post_profiles:
                next_profiles = dict(ebay_post_profiles)
                next_profiles.pop(chosen, None)
                _save_wizard_ebay_post_profiles(
                    repo,
                    username=user.username,
                    actor=user.username,
                    profiles=next_profiles,
                )
                if default_ebay_post_profile == chosen:
                    _set_wizard_ebay_post_default_profile(
                        repo,
                        username=user.username,
                        actor=user.username,
                        profile_name="",
                    )
                st.success(f"Deleted wizard eBay post profile `{chosen}`.")
                st.rerun()
            else:
                st.info("Select a saved profile to delete.")
    st.caption("Access token is intentionally excluded from saved wizard eBay post profiles.")

    ps1, ps2 = st.columns(2)
    with ps1:
        st.text_input(
            "Merchant Location Key",
            key="listing_wizard_ebay_merchant_location_key",
            help="Required for direct wizard publish.",
        )
        st.text_input(
            "Payment Policy ID",
            key="listing_wizard_ebay_payment_policy_id",
            help="Required for direct wizard publish.",
        )
        st.text_input(
            "Fulfillment Policy ID",
            key="listing_wizard_ebay_fulfillment_policy_id",
            help="Required for direct wizard publish.",
        )
        st.text_input(
            "Return Policy ID",
            key="listing_wizard_ebay_return_policy_id",
            help="Required for direct wizard publish.",
        )
    with ps2:
        st.text_input("Marketplace ID", key="listing_wizard_ebay_marketplace_id")
        st.text_input("Currency", key="listing_wizard_ebay_currency")
        st.text_input("Content Language", key="listing_wizard_ebay_content_language")
        st.selectbox(
            "Condition",
            options=wizard_condition_options,
            key="listing_wizard_ebay_condition",
            format_func=lambda value: wizard_condition_labels.get(str(value), str(value)),
        )
        st.checkbox(
            "Use eBay EPS image import",
            key="listing_wizard_ebay_use_eps_images",
            help="When enabled, imports image URLs into eBay EPS before publish.",
        )
        st.checkbox(
            "Upload first MP4/MOV video to eBay",
            key="listing_wizard_ebay_upload_video",
            help="When direct posting, uploads the first supported listing video to eBay Media. MOV/QuickTime files are converted to MP4 before upload.",
        )
    st.text_area(
        "eBay User Access Token",
        key="listing_wizard_ebay_access_token",
        height=100,
        help="Optional. If blank, runtime token is used.",
    )

    preflight_merchant_location_key = str(st.session_state.get("listing_wizard_ebay_merchant_location_key") or "").strip()
    preflight_payment_policy_id = str(st.session_state.get("listing_wizard_ebay_payment_policy_id") or "").strip()
    preflight_fulfillment_policy_id = str(
        st.session_state.get("listing_wizard_ebay_fulfillment_policy_id") or ""
    ).strip()
    preflight_return_policy_id = str(st.session_state.get("listing_wizard_ebay_return_policy_id") or "").strip()
    media_count_est = _wizard_estimated_media_count(
        repo=repo,
        product_id=(int(selected_product.id) if selected_product is not None else None),
        uploaded_files=wizard_files,
    )
    media_count_est = int(media_count_est + len(selected_existing_media_ids))
    preflight_details_for_readiness = str(listing_details or "").strip()
    if bool(st.session_state.get("listing_wizard_include_volume_pricing_in_details")) and volume_pricing_tiers:
        volume_block = _wizard_volume_pricing_description_block(volume_pricing_tiers)
        if volume_block and volume_block not in preflight_details_for_readiness:
            preflight_details_for_readiness = (
                f"{preflight_details_for_readiness}\n\n{volume_block}"
                if preflight_details_for_readiness
                else volume_block
            )
    preflight = evaluate_ebay_readiness(
        listing_title=str(listing_title or "").strip(),
        listing_price=float(listing_price or 0.0),
        auction_start_price=float(auction_start or 0.0),
        auction_reserve_price=float(auction_reserve or 0.0),
        auction_buy_now_price=float(auction_bin or 0.0),
        quantity_listed=int(listing_qty),
        listing_status="draft",
        format_type=str(format_type).strip().upper(),
        listing_duration=str(auction_duration or "").strip().upper(),
        media_count=int(media_count_est),
        category_id=str(selected_category_id or "").strip(),
        merchant_location_key=preflight_merchant_location_key,
        payment_policy_id=preflight_payment_policy_id,
        fulfillment_policy_id=preflight_fulfillment_policy_id,
        return_policy_id=preflight_return_policy_id,
        aspects=_wizard_normalize_aspects_payload(st.session_state.get("listing_wizard_aspects_json") or ""),
        category_aspects=category_aspect_rows,
        condition=str(st.session_state.get("listing_wizard_ebay_condition") or "").strip().upper(),
        condition_description=str(st.session_state.get("listing_wizard_condition_description") or ""),
        listing_description=_format_listing_description_for_ebay(preflight_details_for_readiness),
        category_conditions=category_condition_rows,
    )
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Status", preflight.status)
    p2.metric("Score", preflight.score)
    p3.metric("Blockers", len(preflight.blockers))
    p4.metric("Warnings", len(preflight.warnings))
    if preflight.blockers:
        st.warning("Preflight blockers: " + " | ".join(preflight.blockers))
    elif preflight.warnings:
        st.info("Preflight warnings: " + " | ".join(preflight.warnings))
    st.session_state["listing_wizard_preflight_blocker_count"] = int(len(preflight.blockers))
    st.session_state["listing_wizard_preflight_warning_count"] = int(len(preflight.warnings))
    ai_risk_summary_text = (
        str((ai_suggestions or {}).get("risk_summary") or "").strip()
        if isinstance(ai_suggestions, dict)
        else ""
    )
    risk_summary_snapshot = _build_wizard_risk_summary(
        ai_risk_summary=ai_risk_summary_text,
        preflight=preflight,
        format_type=str(format_type or ""),
        offers_enabled=bool(best_offer_enabled),
    )
    st.session_state["listing_wizard_risk_summary"] = risk_summary_snapshot
    st.markdown("#### AI + Readiness Risk Summary")
    r1, r2 = st.columns(2)
    r1.metric("Risk Level", str(risk_summary_snapshot.get("level", "low")).upper())
    r2.metric("Readiness Score", str(int(risk_summary_snapshot.get("score") or 0)))
    highlights = [str(v) for v in list(risk_summary_snapshot.get("highlights") or []) if str(v).strip()]
    if str(risk_summary_snapshot.get("level") or "") == "high":
        st.error(" ".join(highlights[:2]) if highlights else "High publish risk detected.")
    elif str(risk_summary_snapshot.get("level") or "") == "medium":
        st.warning(" ".join(highlights[:2]) if highlights else "Moderate publish risk detected.")
    else:
        st.success(" ".join(highlights[:1]) if highlights else "Risk profile looks good.")
    if len(highlights) > 1:
        for line in highlights[1:]:
            st.caption(f"- {line}")

    step_checks = [
        {
            "step": "1) Product selected",
            "status": "ready" if selected_product is not None else "blocked",
            "detail": "A source product is required for listing draft creation.",
        },
        {
            "step": "3) Listing title",
            "status": (
                "ready"
                if (bool(str(listing_title or "").strip()) and len(str(listing_title or "").strip()) <= EBAY_TITLE_MAX_CHARS)
                else "blocked"
            ),
            "detail": f"Title must be non-empty and <= {EBAY_TITLE_MAX_CHARS} characters.",
        },
        {
            "step": "3) Pricing inputs",
            "status": (
                "ready"
                if (
                    float(listing_price or 0.0) >= 0.0
                    and (
                        format_type != "AUCTION"
                        or (
                            float(auction_start or 0.0) > 0.0
                            and (float(auction_reserve or 0.0) <= 0.0 or float(auction_reserve or 0.0) >= float(auction_start or 0.0))
                            and (float(auction_bin or 0.0) <= 0.0 or float(auction_bin or 0.0) >= float(auction_start or 0.0))
                        )
                    )
                )
                else "blocked"
            ),
            "detail": "Auction start must be > 0; reserve/BIN cannot be lower than start.",
        },
        {
            "step": "4) Offer controls",
            "status": "ready" if offer_rules_valid else "blocked",
            "detail": "If offers are enabled: minimum <= auto-accept, both <= effective listing price/start.",
        },
        {
            "step": "7) eBay policy preflight",
            "status": "ready" if not preflight.blockers else "blocked",
            "detail": "Readiness score, policy IDs, and format constraints are validated here.",
        },
    ]
    ready_count = len([r for r in step_checks if r["status"] == "ready"])
    s1, s2 = st.columns(2)
    s1.metric("Wizard Checks Ready", f"{ready_count}/{len(step_checks)}")
    s2.metric("Wizard Checks Blocked", str(len(step_checks) - ready_count))
    st.progress(float(ready_count) / float(max(1, len(step_checks))))
    st.dataframe(_safe_table_df(step_checks), use_container_width=True, hide_index=True)
    if preflight.blockers:
        st.warning("Next recommended action: resolve preflight blockers before creating a draft.")
    elif preflight.warnings:
        st.info("Next recommended action: review warnings, then preview and create draft.")
    else:
        st.success("Next recommended action: preview listing, then create draft.")

    st.markdown("### Step 8 of 9: Preview Listing")
    preview_auto_expand = bool(not preflight.blockers and not preflight.warnings)
    with st.expander("Preview Listing Draft", expanded=preview_auto_expand):
        pv1, pv2, pv3, pv4 = st.columns(4)
        pv1.markdown("**Title**")
        pv1.write(str(listing_title or "").strip() or "-")
        pv2.metric("Price", f"${float(listing_price or 0.0):,.2f}")
        pv3.metric("Mode", str(listing_mode or "").strip() or "-")
        pv4.metric("Quantity", str(int(listing_qty)))
        st.caption(
            f"Format: {str(format_type or '').strip().upper()} | "
            f"Duration: {str(auction_duration or '').strip().upper()} | "
            f"Offers: {'enabled' if bool(best_offer_enabled) else 'disabled'}"
        )
        if bool(best_offer_enabled):
            st.caption(
                f"Offer auto-accept: ${float(best_offer_auto_accept or 0.0):,.2f} | "
                f"Offer minimum: ${float(best_offer_minimum or 0.0):,.2f}"
            )
        if format_type == "AUCTION":
            st.caption(
                f"Auction start: ${float(auction_start or 0.0):,.2f} | "
                f"Reserve: ${float(auction_reserve or 0.0):,.2f} | "
                f"BIN: ${float(auction_bin or 0.0):,.2f}"
            )
        st.markdown("**Details Preview**")
        details_preview = str(listing_details or "").strip()
        if details_preview:
            preview_mode = st.selectbox(
                "Details Preview Mode",
                options=["Rendered HTML", "Raw Source"],
                index=0,
                key="listing_wizard_details_preview_mode",
            )
            if preview_mode == "Rendered HTML":
                rendered = _sanitize_preview_html(details_preview)
                if rendered:
                    preview_html = (
                        "<div style='background:#fff;color:#111;padding:12px;"
                        "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
                        "line-height:1.45;'>"
                        f"{rendered}"
                        "</div>"
                    )
                    components.html(preview_html, height=320, scrolling=True)
                else:
                    st.caption("No safe HTML content to render.")
            else:
                st.code(details_preview[:12000], language="html")
        else:
            st.caption("No details entered yet.")
        st.caption(
            f"Media selected: existing={len(selected_existing_media_ids)} | "
            f"new_uploads={len(wizard_files or [])} | "
            f"estimated_total={media_count_est}"
        )

        st.markdown("**Shipping Details**")
        shipping_profiles = _load_wizard_shipping_profiles(repo, user.username)
        default_shipping_profile = str(
            get_runtime_str(repo, _wizard_shipping_default_key(user.username), "") or ""
        ).strip()
        shipping_profile_names = sorted(shipping_profiles.keys(), key=lambda v: v.lower())
        if default_shipping_profile and default_shipping_profile in shipping_profiles:
            shipping_profile_names = [default_shipping_profile] + [
                n for n in shipping_profile_names if n != default_shipping_profile
            ]
        profile_options = ["(none)", *shipping_profile_names]
        profile_selected = st.selectbox(
            "Shipping Profile",
            options=profile_options,
            index=(profile_options.index(default_shipping_profile) if default_shipping_profile in profile_options else 0),
            key="listing_wizard_shipping_profile_selected",
            help="Save/load per-user shipping defaults for this wizard.",
        )
        p1, p2, p3, p4 = st.columns(4)
        with p1:
            if st.button("Apply Profile", key="listing_wizard_shipping_profile_apply_btn"):
                selected_name = str(profile_selected or "").strip()
                if selected_name and selected_name != "(none)" and selected_name in shipping_profiles:
                    selected_payload = shipping_profiles[selected_name]
                    st.session_state["listing_wizard_shipping_service"] = str(
                        selected_payload.get("shipping_service") or ""
                    ).strip()
                    st.session_state["listing_wizard_handling_days"] = int(selected_payload.get("handling_days") or 0)
                    st.session_state["listing_wizard_shipping_cost"] = float(selected_payload.get("shipping_cost") or 0.0)
                    st.session_state["listing_wizard_package_weight_oz"] = float(
                        selected_payload.get("package_weight_oz") or 0.0
                    )
                    st.session_state["listing_wizard_package_length_in"] = float(
                        selected_payload.get("package_length_in") or 0.0
                    )
                    st.session_state["listing_wizard_package_width_in"] = float(
                        selected_payload.get("package_width_in") or 0.0
                    )
                    st.session_state["listing_wizard_package_height_in"] = float(
                        selected_payload.get("package_height_in") or 0.0
                    )
                    st.success(f"Applied shipping profile `{selected_name}`.")
                    st.rerun()
                else:
                    st.info("Select a saved profile to apply.")
        with p2:
            if st.button("Set Default", key="listing_wizard_shipping_profile_set_default_btn"):
                selected_name = str(profile_selected or "").strip()
                if selected_name and selected_name != "(none)" and selected_name in shipping_profiles:
                    _set_wizard_shipping_default_profile(
                        repo,
                        username=user.username,
                        actor=user.username,
                        profile_name=selected_name,
                    )
                    st.success(f"Default shipping profile set to `{selected_name}`.")
                    st.rerun()
                else:
                    st.info("Select a saved profile first.")
        with p3:
            profile_name_input = st.text_input(
                "Profile Name",
                value=str(st.session_state.get("listing_wizard_shipping_profile_name") or "").strip(),
                key="listing_wizard_shipping_profile_name",
                placeholder="e.g. USPS Ground 1-day handling",
            ).strip()
            if st.button("Save Current", key="listing_wizard_shipping_profile_save_btn"):
                if not profile_name_input:
                    st.warning("Enter a profile name to save.")
                else:
                    next_profiles = dict(shipping_profiles)
                    next_profiles[profile_name_input] = {
                        "shipping_service": str(
                            st.session_state.get("listing_wizard_shipping_service") or "USPS Ground Advantage"
                        ).strip(),
                        "handling_days": int(st.session_state.get("listing_wizard_handling_days") or 3),
                        "shipping_cost": float(st.session_state.get("listing_wizard_shipping_cost") or 0.0),
                        "package_weight_oz": float(
                            st.session_state.get("listing_wizard_package_weight_oz")
                            or float(getattr(selected_product, "weight_oz", 0.0) or 0.0)
                        ),
                        "package_length_in": float(
                            st.session_state.get("listing_wizard_package_length_in")
                            or float(getattr(selected_product, "package_length_in", 0.0) or 0.0)
                        ),
                        "package_width_in": float(
                            st.session_state.get("listing_wizard_package_width_in")
                            or float(getattr(selected_product, "package_width_in", 0.0) or 0.0)
                        ),
                        "package_height_in": float(
                            st.session_state.get("listing_wizard_package_height_in")
                            or float(getattr(selected_product, "package_height_in", 0.0) or 0.0)
                        ),
                    }
                    _save_wizard_shipping_profiles(
                        repo,
                        username=user.username,
                        actor=user.username,
                        profiles=next_profiles,
                    )
                    st.success(f"Saved shipping profile `{profile_name_input}`.")
                    st.rerun()
        with p4:
            if st.button("Delete Profile", key="listing_wizard_shipping_profile_delete_btn"):
                selected_name = str(profile_selected or "").strip()
                if selected_name and selected_name != "(none)" and selected_name in shipping_profiles:
                    next_profiles = dict(shipping_profiles)
                    next_profiles.pop(selected_name, None)
                    _save_wizard_shipping_profiles(
                        repo,
                        username=user.username,
                        actor=user.username,
                        profiles=next_profiles,
                    )
                    if default_shipping_profile == selected_name:
                        _set_wizard_shipping_default_profile(
                            repo,
                            username=user.username,
                            actor=user.username,
                            profile_name="",
                        )
                    st.success(f"Deleted shipping profile `{selected_name}`.")
                    st.rerun()
                else:
                    st.info("Select a saved profile to delete.")

        sh1, sh2, sh3 = st.columns(3)
        with sh1:
            shipping_service = st.text_input(
                "Shipping Service",
                value=str(st.session_state.get("listing_wizard_shipping_service") or "USPS Ground Advantage"),
                key="listing_wizard_shipping_service",
                help="Display/service hint for draft metadata.",
            ).strip()
        with sh2:
            handling_days = int(
                st.number_input(
                    "Handling Days",
                    min_value=0,
                    step=1,
                    value=int(st.session_state.get("listing_wizard_handling_days") or 3),
                    key="listing_wizard_handling_days",
                )
            )
        with sh3:
            shipping_cost = float(
                st.number_input(
                    "Shipping Cost",
                    min_value=0.0,
                    step=0.01,
                    value=float(st.session_state.get("listing_wizard_shipping_cost") or 0.0),
                    key="listing_wizard_shipping_cost",
                )
            )
        package_weight_oz = float(
            st.number_input(
                "Package Weight (oz)",
                min_value=0.0,
                step=0.1,
                value=float(
                    st.session_state.get("listing_wizard_package_weight_oz")
                    or float(getattr(selected_product, "weight_oz", 0.0) or 0.0)
                ),
                key="listing_wizard_package_weight_oz",
            )
        )
        dim1, dim2, dim3 = st.columns(3)
        with dim1:
            package_length_in = float(
                st.number_input(
                    "Package Length (in)",
                    min_value=0.0,
                    step=0.1,
                    value=float(
                        st.session_state.get("listing_wizard_package_length_in")
                        or float(getattr(selected_product, "package_length_in", 0.0) or 0.0)
                    ),
                    key="listing_wizard_package_length_in",
                )
            )
        with dim2:
            package_width_in = float(
                st.number_input(
                    "Package Width (in)",
                    min_value=0.0,
                    step=0.1,
                    value=float(
                        st.session_state.get("listing_wizard_package_width_in")
                        or float(getattr(selected_product, "package_width_in", 0.0) or 0.0)
                    ),
                    key="listing_wizard_package_width_in",
                )
            )
        with dim3:
            package_height_in = float(
                st.number_input(
                    "Package Height (in)",
                    min_value=0.0,
                    step=0.1,
                    value=float(
                        st.session_state.get("listing_wizard_package_height_in")
                        or float(getattr(selected_product, "package_height_in", 0.0) or 0.0)
                    ),
                    key="listing_wizard_package_height_in",
                )
            )
        st.caption(
            f"Shipping preview: {shipping_service or '-'} | handling={handling_days} day(s) | "
            f"cost=${shipping_cost:,.2f} | pkg_weight_oz={package_weight_oz:,.2f} | "
            f"dims={package_length_in:,.2f}x{package_width_in:,.2f}x{package_height_in:,.2f} in"
        )
        bsh1, bsh2 = st.columns(2)
        with bsh1:
            if st.button("Apply As Shared eBay Shipping Defaults", key="listing_wizard_apply_shared_shipping_defaults_btn"):
                try:
                    runtime_updates = [
                        ("ebay_shipping_service_default", str(shipping_service or "").strip(), "str"),
                        ("ebay_handling_days_default", str(int(handling_days or 0)), "int"),
                        ("ebay_shipping_cost_default", str(float(shipping_cost or 0.0)), "float"),
                        ("ebay_package_weight_oz_default", str(float(package_weight_oz or 0.0)), "float"),
                        ("ebay_package_length_in_default", str(float(package_length_in or 0.0)), "float"),
                        ("ebay_package_width_in_default", str(float(package_width_in or 0.0)), "float"),
                        ("ebay_package_height_in_default", str(float(package_height_in or 0.0)), "float"),
                    ]
                    for key, value, value_type in runtime_updates:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=str(key),
                            value=str(value or "").strip(),
                            value_type=value_type,
                            description=f"Listing Wizard shared default for `{key}`.",
                            is_active=True,
                            actor=user.username,
                        )
                    st.session_state["ebay_workspace_store_shipping_service_input"] = str(shipping_service or "").strip()
                    st.session_state["ebay_workspace_store_handling_days_input"] = int(handling_days or 0)
                    st.session_state["ebay_workspace_store_shipping_cost_input"] = float(shipping_cost or 0.0)
                    st.session_state["ebay_workspace_store_package_weight_oz_input"] = float(package_weight_oz or 0.0)
                    st.session_state["ebay_workspace_store_package_length_in_input"] = float(package_length_in or 0.0)
                    st.session_state["ebay_workspace_store_package_width_in_input"] = float(package_width_in or 0.0)
                    st.session_state["ebay_workspace_store_package_height_in_input"] = float(package_height_in or 0.0)
                    st.success("Applied shared eBay shipping defaults for Listings + eBay Workspace.")
                except Exception as exc:
                    st.error(f"Unable to apply shared shipping defaults: {exc}")
        with bsh2:
            st.caption(
                "Bridge action: writes runtime defaults and preloads matching eBay Workspace shipping fields."
            )
    if preflight.blockers:
        st.caption("Next recommended action: return to prior steps and clear blockers.")
    else:
        st.caption("Next recommended action: continue to `Create Draft Listing`.")

    st.markdown("### Step 9 of 9: Create Draft / Optional Direct eBay Post")
    last_listing_id = int(st.session_state.get("listing_wizard_last_created_id") or 0)
    last_listing_sku = str(st.session_state.get("listing_wizard_last_created_sku") or "").strip()
    last_listing_title = str(st.session_state.get("listing_wizard_last_created_title") or "").strip()
    duplicate_guard_blocked = bool(has_existing_conflict and not allow_duplicate_listing)
    create_block_reasons: list[str] = []
    if selected_product is None:
        create_block_reasons.append("Select a product first.")
    if not str(listing_title or "").strip():
        create_block_reasons.append("Listing title is required.")
    if len(str(listing_title or "").strip()) > EBAY_TITLE_MAX_CHARS:
        create_block_reasons.append(f"Listing title exceeds eBay {EBAY_TITLE_MAX_CHARS}-character limit.")
    if bool(preflight.blockers):
        create_block_reasons.append("Resolve preflight blockers.")
    if duplicate_guard_blocked:
        create_block_reasons.append(
            "Existing draft/active eBay listings detected for this product. Enable duplicate override to continue."
        )
    if bundle_inventory_overcommit:
        create_block_reasons.append("Lot/bundle composition exceeds selected product stock.")
    create_disabled = bool(create_block_reasons)
    stay_on_wizard_after_create = st.checkbox(
        "Stay on Wizard after Create",
        value=False,
        key="listing_wizard_stay_after_create",
        help="When enabled, keeps you on this page after draft creation so you can use post-create actions.",
    )
    post_to_ebay_now = st.checkbox(
        "Post to eBay Immediately (single listing, non-batch)",
        value=False,
        key="listing_wizard_post_to_ebay_now",
        help="Creates draft locally, then immediately posts this listing to eBay from the wizard.",
    )
    direct_post_mode = st.selectbox(
        "Direct eBay Post Mode",
        options=["Save Unpublished Offer (API Draft)", "Publish Live Listing"],
        index=0,
        key="listing_wizard_direct_post_mode",
        help=(
            "Unpublished offer mode creates/updates an Inventory API offer only (not a Seller Hub compose draft). "
            "Publish mode creates/updates offer and publishes live."
        ),
        disabled=not post_to_ebay_now,
    )
    if post_to_ebay_now:
        st.caption(
            "API Draft note: this stores an unpublished offer (`offer_id`) for QA/review, "
            "then you can publish later."
        )
    if post_to_ebay_now:
        pf1, pf2 = st.columns([1, 2])
        with pf1:
            run_ebay_dependency_preflight = st.button(
                "Run eBay Dependency Preflight",
                key="listing_wizard_run_ebay_dependency_preflight_btn",
            )
        with pf2:
            st.caption(
                "Runs API checks for merchant location, policies, and category before direct post."
            )
        if run_ebay_dependency_preflight:
            token_to_use = str(st.session_state.get("listing_wizard_ebay_access_token") or "").strip() or str(
                get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token) or ""
            ).strip()
            marketplace_id = str(
                st.session_state.get("listing_wizard_ebay_marketplace_id") or default_marketplace_id
            ).strip()
            merchant_location_key = str(st.session_state.get("listing_wizard_ebay_merchant_location_key") or "").strip()
            payment_policy_id = str(st.session_state.get("listing_wizard_ebay_payment_policy_id") or "").strip()
            fulfillment_policy_id = str(st.session_state.get("listing_wizard_ebay_fulfillment_policy_id") or "").strip()
            return_policy_id = str(st.session_state.get("listing_wizard_ebay_return_policy_id") or "").strip()
            result_payload: dict
            try:
                ebay = EbayClient()
                if not ebay.is_configured():
                    result_payload = {
                        "blockers": ["eBay app credentials are not configured."],
                        "warnings": [],
                        "checks": [],
                    }
                elif not token_to_use:
                    result_payload = {
                        "blockers": ["User access token is required."],
                        "warnings": [],
                        "checks": [],
                    }
                else:
                    local_specific_blockers = _wizard_required_specific_blockers(
                        category_id_value=str(selected_category_id or "").strip(),
                        marketplace_id_value=marketplace_id,
                    )
                    if local_specific_blockers:
                        result_payload = {
                            "blockers": local_specific_blockers,
                            "warnings": [],
                            "checks": [],
                        }
                    else:
                        result_payload = ebay.verify_publish_dependencies(
                            access_token=token_to_use,
                            marketplace_id=marketplace_id,
                            category_id=str(selected_category_id or "").strip(),
                            merchant_location_key=merchant_location_key,
                            payment_policy_id=payment_policy_id,
                            fulfillment_policy_id=fulfillment_policy_id,
                            return_policy_id=return_policy_id,
                            format_type=str(format_type or "FIXED_PRICE").strip().upper(),
                            auction_buy_now_price=float(auction_bin or 0.0),
                            condition=str(
                                st.session_state.get("listing_wizard_ebay_condition") or ""
                            ).strip().upper(),
                        )
            except Exception as exc:
                result_payload = {
                    "blockers": [],
                    "warnings": [f"Preflight execution error: {exc}"],
                    "checks": [],
                }
            result_payload["checked_at"] = utcnow_naive().isoformat()
            st.session_state["listing_wizard_ebay_dependency_preflight_result"] = result_payload
            if list(result_payload.get("blockers") or []):
                st.error("eBay dependency preflight found blockers.")
            elif list(result_payload.get("warnings") or []):
                st.warning("eBay dependency preflight completed with warnings.")
            else:
                st.success("eBay dependency preflight passed.")
    preflight_card_payload = st.session_state.get("listing_wizard_ebay_dependency_preflight_result")
    if isinstance(preflight_card_payload, dict):
        _render_ebay_preflight_card(preflight_card_payload)
    if create_block_reasons:
        st.caption("Create is disabled until the following are resolved:")
        for reason in create_block_reasons:
            st.caption(f"- {reason}")
    if st.button(
        "Create Draft Listing",
        key="listing_wizard_create_draft_btn",
        disabled=create_disabled,
    ):
        if not ensure_permission(user, "create", "Create Listing Draft"):
            st.stop()
        if preflight.blockers:
            st.error("Resolve preflight blockers before creating draft listing.")
            st.stop()
        if selected_product is None:
            st.error("Select a product first.")
            st.stop()
        if has_existing_conflict and not allow_duplicate_listing:
            st.error(
                "Duplicate listing guard: existing draft/active eBay listing detected for this product. "
                "Review existing rows or enable override to continue."
            )
            st.stop()
        if not str(listing_title or "").strip():
            st.error("Listing title is required.")
            st.stop()
        if len(str(listing_title or "").strip()) > EBAY_TITLE_MAX_CHARS:
            st.error(f"Listing title exceeds eBay limit ({EBAY_TITLE_MAX_CHARS} chars).")
            st.stop()
        if float(listing_price or 0) < 0:
            st.error("Listing price must be non-negative.")
            st.stop()
        if format_type == "AUCTION":
            if float(auction_start or 0) <= 0:
                st.error("Auction start price must be greater than 0.")
                st.stop()
            if float(auction_reserve or 0) > 0 and float(auction_reserve or 0) < float(auction_start or 0):
                st.error("Auction reserve cannot be lower than start price.")
                st.stop()
            if float(auction_bin or 0) > 0 and float(auction_bin or 0) < float(auction_start or 0):
                st.error("Auction Buy It Now cannot be lower than start price.")
                st.stop()
        if bool(best_offer_enabled):
            if float(best_offer_auto_accept or 0.0) > 0 and float(best_offer_minimum or 0.0) > 0:
                if float(best_offer_minimum or 0.0) > float(best_offer_auto_accept or 0.0):
                    st.error("Offer minimum cannot exceed auto-accept value.")
                    st.stop()
            if effective_offer_ceiling > 0:
                if float(best_offer_auto_accept or 0.0) > effective_offer_ceiling:
                    st.error("Offer auto-accept cannot exceed listing price/start price.")
                    st.stop()
                if float(best_offer_minimum or 0.0) > effective_offer_ceiling:
                    st.error("Offer minimum cannot exceed listing price/start price.")
                    st.stop()
        if volume_pricing_errors:
            st.error("Volume pricing is invalid: " + " | ".join(volume_pricing_errors[:5]))
            st.stop()
        if volume_pricing_tiers and str(format_type or "").strip().upper() != "FIXED_PRICE":
            st.error("Volume pricing tiers are only supported for fixed-price modes.")
            st.stop()
        if volume_pricing_tiers and float(listing_price or 0.0) > 0:
            invalid_tiers = [t for t in volume_pricing_tiers if float(t.get("price") or 0.0) > float(listing_price or 0.0)]
            if invalid_tiers:
                st.error("Volume tier prices cannot exceed listing price.")
                st.stop()
        effective_details_for_save = str(listing_details or "").strip()
        if bool(st.session_state.get("listing_wizard_include_volume_pricing_in_details")) and volume_pricing_tiers:
            volume_block = _wizard_volume_pricing_description_block(volume_pricing_tiers)
            if volume_block and volume_block not in effective_details_for_save:
                effective_details_for_save = (
                    f"{effective_details_for_save}\n\n{volume_block}"
                    if effective_details_for_save
                    else volume_block
                )

        ebay_publish_payload = {
            "format_type": str(format_type).strip().upper(),
            "listing_duration": str(auction_duration).strip().upper(),
            "category_id": str(selected_category_id or "").strip(),
            "store_category_names": selected_store_category_names,
            "merchant_location_key": str(st.session_state.get("listing_wizard_ebay_merchant_location_key") or "").strip(),
            "payment_policy_id": str(get_runtime_str(repo, "ebay_payment_policy_id", "") or "").strip(),
            "fulfillment_policy_id": str(get_runtime_str(repo, "ebay_fulfillment_policy_id", "") or "").strip(),
            "return_policy_id": str(get_runtime_str(repo, "ebay_return_policy_id", "") or "").strip(),
            "best_offer_enabled": bool(best_offer_enabled),
            "best_offer_auto_accept": float(best_offer_auto_accept or 0),
            "best_offer_minimum": float(best_offer_minimum or 0),
            "volume_pricing_tiers": volume_pricing_tiers,
            "volume_pricing_json": str(st.session_state.get("listing_wizard_volume_pricing_json") or "").strip(),
            "quantity": int(listing_qty),
            "bundle": bundle_metadata,
            "auction_start_price": float(auction_start or 0),
            "auction_reserve_price": float(auction_reserve or 0),
            "auction_buy_now_price": float(auction_bin or 0),
            "shipping_service": str(st.session_state.get("listing_wizard_shipping_service") or "").strip(),
            "handling_days": int(st.session_state.get("listing_wizard_handling_days") or 0),
            "shipping_cost": float(st.session_state.get("listing_wizard_shipping_cost") or 0.0),
            "estimated_buyer_paid_shipping": float(
                st.session_state.get("listing_wizard_estimated_buyer_shipping") or 0.0
            ),
            "estimated_promoted_rate_percent": float(
                st.session_state.get("listing_wizard_estimated_promoted_rate") or 0.0
            ),
            "package_weight_oz": float(st.session_state.get("listing_wizard_package_weight_oz") or 0.0),
            "package_length_in": float(st.session_state.get("listing_wizard_package_length_in") or 0.0),
            "package_width_in": float(st.session_state.get("listing_wizard_package_width_in") or 0.0),
            "package_height_in": float(st.session_state.get("listing_wizard_package_height_in") or 0.0),
            "subtitle": str(st.session_state.get("listing_wizard_subtitle") or "").strip(),
            "condition_description": str(
                st.session_state.get("listing_wizard_condition_description") or ""
            ).strip(),
            "aspects_json": str(st.session_state.get("listing_wizard_aspects_json") or "").strip(),
            "aspects": _wizard_normalize_aspects_payload(st.session_state.get("listing_wizard_aspects_json") or ""),
            "primary_image_ref": str(st.session_state.get("listing_wizard_primary_image_ref") or "").strip(),
            "upload_video_to_ebay": bool(st.session_state.get("listing_wizard_ebay_upload_video")),
            "fee_estimate": estimate_ebay_fees(
                repo,
                unit_price=(
                    float(auction_start or 0.0)
                    if str(format_type or "").strip().upper() == "AUCTION"
                    else float(listing_price or 0.0)
                ),
                quantity=int(listing_qty or 1),
                buyer_paid_shipping=float(st.session_state.get("listing_wizard_estimated_buyer_shipping") or 0.0),
                promoted_rate_percent=float(st.session_state.get("listing_wizard_estimated_promoted_rate") or 0.0),
            ),
        }
        draft_details_payload = {
            "notes": effective_details_for_save,
            "subtitle": str(st.session_state.get("listing_wizard_subtitle") or "").strip(),
            "condition_description": str(
                st.session_state.get("listing_wizard_condition_description") or ""
            ).strip(),
            "aspects_json": str(st.session_state.get("listing_wizard_aspects_json") or "").strip(),
            "aspects": _wizard_normalize_aspects_payload(st.session_state.get("listing_wizard_aspects_json") or ""),
            "ebay_publish": ebay_publish_payload,
            "bundle": bundle_metadata,
            "wizard_mode": str(listing_mode),
            "wizard_created_by": user.username,
        }
        ai_suggestions_payload = st.session_state.get("listing_wizard_ai_suggestions")
        ai_diag_payload = st.session_state.get("listing_wizard_ai_diagnostics")
        ai_acceptance_payload = st.session_state.get("listing_wizard_ai_acceptance")
        risk_summary_payload = st.session_state.get("listing_wizard_risk_summary")
        ai_outcome_payload: dict[str, object] = {}
        if isinstance(ai_acceptance_payload, dict):
            snapshot_payload = (
                ai_acceptance_payload.get("snapshot")
                if isinstance(ai_acceptance_payload.get("snapshot"), dict)
                else {}
            )
            applied_fields_payload = (
                ai_acceptance_payload.get("applied_fields")
                if isinstance(ai_acceptance_payload.get("applied_fields"), dict)
                else {}
            )
            edited_fields: list[str] = []
            if bool(applied_fields_payload.get("title")):
                if str(snapshot_payload.get("title") or "").strip() != str(listing_title or "").strip():
                    edited_fields.append("title")
            if bool(applied_fields_payload.get("details")):
                if str(snapshot_payload.get("details") or "").strip() != str(effective_details_for_save or "").strip():
                    edited_fields.append("details")
            if bool(applied_fields_payload.get("price_offer")):
                snapshot_price = float(snapshot_payload.get("price") or 0.0)
                final_price = float(auction_start if format_type == "AUCTION" else listing_price)
                if abs(snapshot_price - final_price) > 0.0001:
                    edited_fields.append("price")
                if bool(snapshot_payload.get("offer_enabled")) != bool(best_offer_enabled):
                    edited_fields.append("offer_enabled")
                if abs(float(snapshot_payload.get("offer_auto_accept") or 0.0) - float(best_offer_auto_accept or 0.0)) > 0.0001:
                    edited_fields.append("offer_auto_accept")
                if abs(float(snapshot_payload.get("offer_minimum") or 0.0) - float(best_offer_minimum or 0.0)) > 0.0001:
                    edited_fields.append("offer_minimum")
            ai_outcome_payload = {
                "evaluated_at": utcnow_naive().isoformat(),
                "edited_fields": edited_fields,
                "accepted_as_is": len(edited_fields) == 0,
            }
        if (
            isinstance(ai_suggestions_payload, dict)
            or isinstance(ai_diag_payload, dict)
            or isinstance(ai_acceptance_payload, dict)
            or isinstance(risk_summary_payload, dict)
        ):
            draft_details_payload["ai_draft"] = {
                "suggestions": ai_suggestions_payload if isinstance(ai_suggestions_payload, dict) else {},
                "diagnostics": ai_diag_payload if isinstance(ai_diag_payload, dict) else {},
                "acceptance": ai_acceptance_payload if isinstance(ai_acceptance_payload, dict) else {},
                "risk_summary": risk_summary_payload if isinstance(risk_summary_payload, dict) else {},
                "outcome": ai_outcome_payload,
            }
        created = repo.create_listing(
            product_id=int(selected_product.id),
            marketplace="ebay",
            listing_title=str(listing_title).strip(),
            listing_price=to_decimal(auction_start if format_type == "AUCTION" else listing_price),
            quantity_listed=int(listing_qty),
            marketplace_details=json.dumps(draft_details_payload),
            listing_status="draft",
            actor=user.username,
        )
        repo.update_listing(
            int(created.id),
            {
                "format_type": str(format_type).strip().upper(),
                "listing_duration": str(auction_duration).strip().upper(),
                "listing_price": to_decimal(auction_start if format_type == "AUCTION" else listing_price),
            },
            actor=user.username,
        )
        if isinstance(ai_acceptance_payload, dict):
            try:
                repo.record_audit_event(
                    entity_type="ai_prompt_acceptance",
                    entity_id=int(created.id),
                    action="listing_wizard_outcome",
                    actor=user.username,
                    changes={
                        "workflow": "listing_wizard",
                        "listing_id": int(created.id),
                        "product_id": int(selected_product.id),
                        "acceptance": ai_acceptance_payload,
                        "outcome": ai_outcome_payload,
                    },
                )
            except Exception:
                pass

        uploaded_count = 0
        upload_errors: list[str] = []
        if wizard_files:
            uploaded_count, upload_errors = upload_media_for_listing(
                repo=repo,
                storage=storage,
                listing_id=int(created.id),
                product_id=int(selected_product.id),
                uploaded_files=wizard_files,
                uploaded_by=listing_uploaded_by,
            )
        linked_existing = 0
        linked_existing_errors: list[str] = []
        attach_lookup = {
            int(row.id): row
            for row in _product_media_rows_cached(int(selected_product.id))
        }
        for media_id in selected_existing_media_ids:
            row = attach_lookup.get(int(media_id))
            if row is None:
                linked_existing_errors.append(
                    f"Media #{int(media_id)} is unavailable for attach (already linked or missing)."
                )
                continue
            if row.product_id not in {None, int(selected_product.id)}:
                linked_existing_errors.append(
                    f"Media #{int(media_id)} is linked to a different product and was skipped."
                )
                continue
            if row.listing_id not in {None, 0, int(created.id)}:
                try:
                    repo.create_media_asset(
                        product_id=int(selected_product.id),
                        listing_id=int(created.id),
                        media_type=str(row.media_type or "").strip() or "image",
                        original_filename=str(row.original_filename or "").strip() or f"media-{int(row.id)}",
                        content_type=str(row.content_type or "").strip() or "application/octet-stream",
                        size_bytes=int(row.size_bytes or 0),
                        s3_bucket=str(row.s3_bucket or "").strip() or None,
                        s3_key=str(row.s3_key or "").strip() or None,
                        s3_url=str(row.s3_url or "").strip() or None,
                        uploaded_by=user.username,
                    )
                    linked_existing += 1
                    continue
                except Exception as exc:
                    linked_existing_errors.append(
                        f"Media #{int(media_id)} already linked to listing #{int(row.listing_id)} and clone failed: {exc}"
                    )
                    continue
            if row.listing_id == int(created.id):
                linked_existing += 1
                continue
            try:
                repo.update_media_asset(
                    int(media_id),
                    {"product_id": int(selected_product.id), "listing_id": int(created.id)},
                    actor=user.username,
                )
                linked_existing += 1
            except Exception as exc:
                linked_existing_errors.append(f"Media #{int(media_id)} failed to link: {exc}")

        st.success(f"Created listing draft #{created.id}.")
        if uploaded_count:
            st.success(f"Uploaded {uploaded_count} media file(s) to listing draft.")
        if linked_existing:
            st.success(f"Linked {linked_existing} existing product media file(s) to listing draft.")
        if upload_errors:
            st.error("Some media uploads failed: " + " | ".join(upload_errors))
        if linked_existing_errors:
            st.error("Some existing media links failed: " + " | ".join(linked_existing_errors))

        direct_post_failed = False
        direct_post_error = ""
        if post_to_ebay_now:
            ebay_inventory_sku = build_ebay_inventory_item_sku(
                str(selected_product.sku or "").strip(),
                listing_id=int(created.id),
            )
            token_to_use = str(st.session_state.get("listing_wizard_ebay_access_token") or "").strip() or str(
                get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token) or ""
            ).strip()
            merchant_location_key = str(st.session_state.get("listing_wizard_ebay_merchant_location_key") or "").strip()
            payment_policy_id = str(st.session_state.get("listing_wizard_ebay_payment_policy_id") or "").strip()
            fulfillment_policy_id = str(st.session_state.get("listing_wizard_ebay_fulfillment_policy_id") or "").strip()
            return_policy_id = str(st.session_state.get("listing_wizard_ebay_return_policy_id") or "").strip()
            marketplace_id = str(st.session_state.get("listing_wizard_ebay_marketplace_id") or default_marketplace_id).strip()
            currency = str(st.session_state.get("listing_wizard_ebay_currency") or default_currency).strip()
            content_language = str(
                st.session_state.get("listing_wizard_ebay_content_language") or default_content_language
            ).strip()
            condition = str(st.session_state.get("listing_wizard_ebay_condition") or "NEW").strip().upper() or "NEW"
            use_eps_images = bool(st.session_state.get("listing_wizard_ebay_use_eps_images"))
            upload_video_to_ebay = bool(st.session_state.get("listing_wizard_ebay_upload_video"))

            if not token_to_use:
                st.error("Direct eBay post skipped: missing user access token.")
            elif not selected_category_id:
                st.error("Direct eBay post skipped: missing eBay category ID.")
            elif not merchant_location_key:
                st.error("Direct eBay post skipped: missing merchant location key.")
            elif not payment_policy_id or not fulfillment_policy_id or not return_policy_id:
                st.error("Direct eBay post skipped: missing payment/fulfillment/return policy IDs.")
            elif category_condition_rows and not _wizard_is_condition_valid_for_loaded_policy(
                category_condition_rows,
                condition,
            ):
                st.error(
                    f"Direct eBay post skipped: condition `{condition}` is not valid for eBay category "
                    f"`{selected_category_id}`. Load/refresh Category Conditions and choose a returned option."
                )
            else:
                direct_post_stage = "init"
                direct_post_context: dict[str, object] = {
                    "mode": str(direct_post_mode or "").strip(),
                    "format": str(format_type or "FIXED_PRICE").strip().upper(),
                    "marketplace_id": marketplace_id,
                    "category_id": str(selected_category_id or "").strip(),
                    "merchant_location_key": merchant_location_key,
                    "payment_policy_id": payment_policy_id,
                    "fulfillment_policy_id": fulfillment_policy_id,
                    "return_policy_id": return_policy_id,
                    "inventory_sku": ebay_inventory_sku,
                    "product_sku": str(selected_product.sku or "").strip(),
                    "store_category_names": selected_store_category_names,
                    "use_eps_images": bool(use_eps_images),
                    "upload_video_to_ebay": bool(upload_video_to_ebay),
                    "listing_qty": int(listing_qty or 1),
                }
                try:
                    direct_post_stage = "ebay_client_init"
                    ebay = EbayClient()
                    if not ebay.is_configured():
                        raise RuntimeError("eBay app credentials are not configured.")
                    effective_condition_rows = list(category_condition_rows or [])
                    if not effective_condition_rows and selected_category_id and token_to_use:
                        direct_post_stage = "load_category_condition_policy"
                        policies = ebay.get_item_condition_policies(
                            access_token=token_to_use,
                            category_id=selected_category_id,
                            marketplace_id=marketplace_id,
                        )
                        effective_condition_rows = normalize_ebay_condition_policy_rows(
                            policies,
                            category_id=selected_category_id,
                        )
                        if effective_condition_rows:
                            st.session_state["listing_wizard_category_condition_rows"] = effective_condition_rows
                            st.session_state["listing_wizard_category_condition_signature"] = (
                                f"{marketplace_id.upper()}:{selected_category_id}"
                            )
                    if effective_condition_rows and not _wizard_is_condition_valid_for_loaded_policy(
                        effective_condition_rows,
                        condition,
                    ):
                        raise RuntimeError(
                            f"Selected condition `{condition}` is not valid for eBay category `{selected_category_id}`. "
                            "Load/refresh Category Conditions and choose one of the returned options."
                        )
                    local_specific_blockers = _wizard_required_specific_blockers(
                        category_id_value=str(selected_category_id or "").strip(),
                        marketplace_id_value=marketplace_id,
                    )
                    if local_specific_blockers:
                        raise RuntimeError(
                            "Direct eBay post blocked by required item specifics: "
                            + " | ".join(local_specific_blockers[:3])
                        )
                    direct_post_stage = "resolve_merchant_location"
                    effective_merchant_location_key = ebay.resolve_merchant_location_key(
                        access_token=token_to_use,
                        merchant_location_key=merchant_location_key,
                    )
                    direct_post_context["resolved_merchant_location_key"] = str(
                        effective_merchant_location_key or ""
                    ).strip()
                    direct_post_stage = "verify_publish_dependencies"
                    preflight_result = ebay.verify_publish_dependencies(
                        access_token=token_to_use,
                        marketplace_id=marketplace_id,
                        category_id=selected_category_id,
                        merchant_location_key=effective_merchant_location_key,
                        payment_policy_id=payment_policy_id,
                        fulfillment_policy_id=fulfillment_policy_id,
                        return_policy_id=return_policy_id,
                        format_type=str(format_type or "FIXED_PRICE").strip().upper(),
                        auction_buy_now_price=float(auction_bin or 0.0),
                        condition=condition,
                    )
                    preflight_blockers = list(preflight_result.get("blockers") or [])
                    preflight_warnings = list(preflight_result.get("warnings") or [])
                    direct_post_context["preflight_blockers"] = preflight_blockers[:5]
                    direct_post_context["preflight_warnings"] = preflight_warnings[:5]
                    if preflight_warnings:
                        st.warning("Direct eBay post preflight warnings: " + " | ".join(preflight_warnings[:3]))
                    if preflight_blockers:
                        raise RuntimeError(
                            "Direct eBay post blocked by dependency preflight: "
                            + " | ".join(preflight_blockers[:3])
                        )
                    direct_post_stage = "load_listing_media"
                    listing_media_rows = repo.list_media_assets_for_listing(int(created.id))
                    image_rows = [
                        m for m in listing_media_rows if str(m.media_type or "").strip().lower() == "image"
                    ]
                    video_rows = [
                        m for m in listing_media_rows if str(m.media_type or "").strip().lower() == "video"
                    ]
                    image_rows = _wizard_order_media_rows_for_primary(
                        image_rows,
                        str(st.session_state.get("listing_wizard_primary_image_ref") or ""),
                    )
                    primary_image_meta = _wizard_primary_image_metadata(
                        image_rows,
                        str(st.session_state.get("listing_wizard_primary_image_ref") or ""),
                    )
                    image_urls: list[str] = []
                    eps_uploads: list[dict] = []
                    eps_upload_errors: list[str] = []
                    for media in image_rows:
                        original_url = str(media.s3_url or "").strip()
                        if use_eps_images:
                            try:
                                eps_url, eps_meta = _wizard_create_eps_image_with_retry(
                                    ebay=ebay,
                                    access_token=token_to_use,
                                    media=media,
                                    storage=storage,
                                )
                                image_urls.append(eps_url)
                                eps_uploads.append(eps_meta)
                            except Exception as exc:
                                eps_upload_errors.append(f"{str(media.original_filename or '').strip()}: {exc}")
                        else:
                            if original_url and original_url.startswith("https://"):
                                image_urls.append(original_url)

                    image_source_mode = "ebay_eps" if use_eps_images else "direct_https_urls"
                    if use_eps_images and eps_upload_errors:
                        raise RuntimeError(
                            "eBay EPS image hosting failed for one or more selected images. "
                            "Direct/self-hosted image fallback is disabled. "
                            + " | ".join(eps_upload_errors[:5])
                        )
                    if not image_urls:
                        raise RuntimeError(
                            "No publishable images found on the created draft. "
                            "Attach at least one image and retry."
                        )
                    if len(image_urls) > 24:
                        image_urls = image_urls[:24]

                    video_ids: list[str] = []
                    uploaded_video_info: dict | None = None
                    skipped_video_count = 0
                    video_warning = _wizard_video_upload_warning(upload_video_to_ebay, video_rows)
                    if video_warning:
                        direct_post_context["video_warning"] = video_warning
                        st.warning(video_warning + " Continuing without listing video.")
                    if upload_video_to_ebay and video_rows:
                        selected_video = _wizard_select_ebay_video_media(video_rows)
                        skipped_video_count = int(len(video_rows) - (1 if selected_video is not None else 0))
                        if selected_video is not None:
                            direct_post_stage = "upload_video"
                            video_id, uploaded_video_info = _wizard_upload_ebay_video_with_retry(
                                ebay=ebay,
                                access_token=token_to_use,
                                media=selected_video,
                                storage=storage,
                                listing_title=str(listing_title or selected_product.title or "").strip(),
                            )
                            video_ids = [video_id]
                            direct_post_context["video_id"] = video_id
                            direct_post_context["video_media_asset_id"] = int(
                                getattr(selected_video, "id", 0) or 0
                            )
                            if skipped_video_count > 0:
                                st.info(
                                    "eBay supports one video per listing; attached the first supported video and skipped "
                                    f"{skipped_video_count} additional video file(s)."
                                )

                    effective_listing_description = _format_listing_description_for_ebay(effective_details_for_save)
                    if not effective_listing_description:
                        raise RuntimeError("Listing details are empty after sanitization.")
                    if len(effective_listing_description) > EBAY_MAX_INVENTORY_DESCRIPTION_CHARS:
                        raise RuntimeError(
                            "Direct eBay post blocked: eBay listing description must be "
                            f"{EBAY_MAX_INVENTORY_DESCRIPTION_CHARS} characters or fewer "
                            f"(currently {len(effective_listing_description)})."
                        )

                    direct_post_stage = "create_or_replace_inventory_item"
                    inventory_payload = {
                        "availability": {"shipToLocationAvailability": {"quantity": int(listing_qty)}},
                        "condition": condition,
                        "product": {
                            "title": str(listing_title or "").strip(),
                            "description": effective_listing_description,
                            "imageUrls": image_urls,
                        },
                    }
                    subtitle = str(st.session_state.get("listing_wizard_subtitle") or "").strip()
                    condition_description = str(
                        st.session_state.get("listing_wizard_condition_description") or ""
                    ).strip()
                    aspects_payload = _wizard_normalize_aspects_payload(
                        st.session_state.get("listing_wizard_aspects_json") or ""
                    )
                    effective_aspects_payload, injected_aspect_keys = merge_ebay_aspects_defaults(
                        category=str(selected_product.category or "").strip(),
                        metal_type=str(selected_product.metal_type or "").strip(),
                        title=str(listing_title or selected_product.title or "").strip(),
                        weight_oz=selected_product.weight_oz,
                        existing_aspects=aspects_payload,
                    )
                    if injected_aspect_keys:
                        st.info(
                            "Auto-filled eBay item specifics defaults for bullion/coin listing: "
                            + ", ".join(injected_aspect_keys)
                        )
                    if subtitle:
                        inventory_payload["product"]["subtitle"] = subtitle
                    if condition_description:
                        inventory_payload["conditionDescription"] = condition_description
                    if effective_aspects_payload:
                        inventory_payload["product"]["aspects"] = effective_aspects_payload
                    if video_ids:
                        inventory_payload["product"]["videoIds"] = video_ids
                    _wizard_maybe_add_package_data(inventory_payload, selected_product)

                    offer_payload = _wizard_build_ebay_offer_payload(
                        sku=ebay_inventory_sku,
                        marketplace_id=marketplace_id,
                        format_type=str(format_type or "FIXED_PRICE").strip().upper(),
                        listing_qty=int(listing_qty or 1),
                        category_id=selected_category_id,
                        merchant_location_key=effective_merchant_location_key,
                        listing_description=effective_listing_description,
                        listing_duration=str(auction_duration or "").strip().upper(),
                        payment_policy_id=payment_policy_id,
                        fulfillment_policy_id=fulfillment_policy_id,
                        return_policy_id=return_policy_id,
                        currency=currency,
                        fixed_price=float(listing_price or 0.0),
                        best_offer_enabled=bool(best_offer_enabled),
                        best_offer_auto_accept=float(best_offer_auto_accept or 0.0),
                        best_offer_minimum=float(best_offer_minimum or 0.0),
                        auction_start_price=float(auction_start or 0.0),
                        auction_reserve_price=float(auction_reserve or 0.0),
                        auction_buy_now_price=float(auction_bin or 0.0),
                        store_category_names=selected_store_category_names,
                    )
                    if str(format_type or "FIXED_PRICE").strip().upper() == "FIXED_PRICE" and volume_pricing_tiers:
                        st.info(
                            "Volume pricing tiers were stored in listing metadata for eBay handoff/review. "
                            "Direct API apply is not enabled yet in this flow."
                        )

                    direct_post_stage = "upsert_inventory"
                    fell_back_inventory, inventory_error = _wizard_create_or_replace_inventory_item_with_fallback(
                        ebay=ebay,
                        access_token=token_to_use,
                        sku=ebay_inventory_sku,
                        payload=inventory_payload,
                        content_language=content_language,
                        preserve_video_ids=bool(video_ids),
                    )
                    if fell_back_inventory:
                        st.warning(
                            "eBay inventory upsert hit a transient Core Inventory Service error and "
                            "succeeded after retrying with a simpler payload."
                        )
                    inventory_video_verification: dict[str, object] = {}
                    post_offer_video_verification: dict[str, object] = {}
                    post_publish_video_verification: dict[str, object] = {}
                    trading_listing_video_verification: dict[str, object] = {}
                    if video_ids:
                        direct_post_stage = "verify_inventory_video_ids"
                        inventory_video_verification = _wizard_verify_inventory_video_ids(
                            ebay=ebay,
                            access_token=token_to_use,
                            sku=ebay_inventory_sku,
                            expected_video_ids=video_ids,
                            content_language=content_language,
                        )
                        direct_post_context["inventory_video_ids_verified"] = bool(
                            inventory_video_verification.get("verified")
                        )
                        direct_post_context["inventory_video_ids"] = list(
                            inventory_video_verification.get("actual_video_ids") or []
                        )
                    direct_post_stage = "create_offer"
                    offer_result = ebay.create_offer(
                        access_token=token_to_use,
                        payload=offer_payload,
                        content_language=content_language,
                    )
                    offer_id = str(offer_result.get("offerId") or "").strip()
                    if not offer_id:
                        raise RuntimeError(f"eBay createOffer missing offerId. payload={offer_result}")
                    run_publish_live = str(direct_post_mode or "").strip().lower() == "publish live listing"
                    direct_post_context["offer_id"] = offer_id
                    direct_post_context["run_publish_live"] = bool(run_publish_live)
                    if video_ids:
                        direct_post_stage = "verify_post_offer_inventory_video_ids"
                        post_offer_video_verification = _wizard_verify_inventory_video_ids(
                            ebay=ebay,
                            access_token=token_to_use,
                            sku=ebay_inventory_sku,
                            expected_video_ids=video_ids,
                            content_language=content_language,
                        )
                        direct_post_context["post_offer_inventory_video_ids_verified"] = bool(
                            post_offer_video_verification.get("verified")
                        )
                        direct_post_context["post_offer_inventory_video_ids"] = list(
                            post_offer_video_verification.get("actual_video_ids") or []
                        )
                    publish_result = {}
                    listing_id = ""
                    listing_url = ""
                    if run_publish_live:
                        direct_post_stage = "publish_offer"
                        publish_result = ebay.publish_offer(
                            access_token=token_to_use,
                            offer_id=offer_id,
                            inventory_sku=ebay_inventory_sku,
                            content_language=content_language,
                        )
                        listing_id = str(publish_result.get("listingId") or "").strip()
                        if not listing_id:
                            raise RuntimeError(f"eBay publishOffer missing listingId. payload={publish_result}")
                        listing_url = ebay.listing_url_for_id(listing_id)
                        if video_ids:
                            direct_post_stage = "verify_post_publish_inventory_video_ids"
                            post_publish_video_verification = _wizard_verify_inventory_video_ids(
                                ebay=ebay,
                                access_token=token_to_use,
                                sku=ebay_inventory_sku,
                                expected_video_ids=video_ids,
                                content_language=content_language,
                            )
                            direct_post_context["post_publish_inventory_video_ids_verified"] = bool(
                                post_publish_video_verification.get("verified")
                            )
                            direct_post_context["post_publish_inventory_video_ids"] = list(
                                post_publish_video_verification.get("actual_video_ids") or []
                            )
                            direct_post_stage = "verify_trading_listing_video_ids"
                            trading_listing_video_verification = _wizard_verify_trading_listing_video_ids(
                                ebay=ebay,
                                access_token=token_to_use,
                                listing_id=listing_id,
                                expected_video_ids=video_ids,
                                marketplace_id=marketplace_id,
                            )
                            direct_post_context["trading_listing_video_ids_verified"] = bool(
                                trading_listing_video_verification.get("verified")
                            )
                            direct_post_context["trading_listing_video_ids"] = list(
                                trading_listing_video_verification.get("actual_video_ids") or []
                            )
                    offer_status = "PUBLISHED" if run_publish_live else ""
                    try:
                        offer_lookup = ebay.get_offer(access_token=token_to_use, offer_id=offer_id)
                        offer_status = str(offer_lookup.get("status") or offer_status or "").strip().upper()
                        if not listing_id:
                            listing_id = str(offer_lookup.get("listingId") or "").strip()
                            if listing_id and not listing_url:
                                listing_url = ebay.listing_url_for_id(listing_id)
                    except Exception:
                        pass

                    direct_post_stage = "update_local_listing"
                    details_obj = {
                        "notes": effective_details_for_save,
                        "ebay_publish": {
                            "format": str(format_type or "FIXED_PRICE").strip().upper(),
                            "listing_duration": str(auction_duration or "").strip().upper(),
                            "best_offer_enabled": bool(best_offer_enabled)
                            if str(format_type or "FIXED_PRICE").strip().upper() == "FIXED_PRICE"
                            else False,
                            "best_offer_auto_accept": float(best_offer_auto_accept or 0.0)
                            if str(format_type or "FIXED_PRICE").strip().upper() == "FIXED_PRICE"
                            else 0.0,
                            "best_offer_minimum": float(best_offer_minimum or 0.0)
                            if str(format_type or "FIXED_PRICE").strip().upper() == "FIXED_PRICE"
                            else 0.0,
                            "volume_pricing_tiers": volume_pricing_tiers
                            if str(format_type or "FIXED_PRICE").strip().upper() == "FIXED_PRICE"
                            else [],
                            "volume_pricing_json": str(
                                st.session_state.get("listing_wizard_volume_pricing_json") or ""
                            ).strip()
                            if str(format_type or "FIXED_PRICE").strip().upper() == "FIXED_PRICE"
                            else "",
                            "auction_start_price": float(auction_start or 0.0)
                            if str(format_type or "FIXED_PRICE").strip().upper() == "AUCTION"
                            else 0.0,
                            "auction_reserve_price": float(auction_reserve or 0.0)
                            if str(format_type or "FIXED_PRICE").strip().upper() == "AUCTION"
                            else 0.0,
                            "auction_buy_now_price": float(auction_bin or 0.0)
                            if str(format_type or "FIXED_PRICE").strip().upper() == "AUCTION"
                            else 0.0,
                            "offer_id": offer_id,
                            "inventory_sku": ebay_inventory_sku,
                            "product_sku": str(selected_product.sku or "").strip(),
                            "offer_status": str(offer_status or ("PUBLISHED" if run_publish_live else "UNPUBLISHED")).strip(),
                            "direct_post_last_error": "",
                            "direct_post_last_error_at": "",
                            "direct_post_last_error_stage": "",
                            "direct_post_last_context": {},
                            "marketplace_id": marketplace_id,
                            "store_category_names": selected_store_category_names,
                            "published_at": utcnow_naive().isoformat(),
                            "image_source": image_source_mode,
                            "image_count": len(image_urls),
                            **primary_image_meta,
                            "eps_uploads": eps_uploads,
                            "upload_video_to_ebay": bool(upload_video_to_ebay),
                            "video_attached": bool(video_ids),
                            "video_warning": video_warning,
                            "video_upload": uploaded_video_info or {},
                            "inventory_video_verification": inventory_video_verification,
                            "post_offer_video_verification": post_offer_video_verification,
                            "post_publish_video_verification": post_publish_video_verification,
                            "trading_listing_video_verification": trading_listing_video_verification,
                            "skipped_video_count": int(skipped_video_count),
                            "quantity": int(listing_qty),
                            "shipping_service": str(st.session_state.get("listing_wizard_shipping_service") or "").strip(),
                            "handling_days": int(st.session_state.get("listing_wizard_handling_days") or 0),
                            "shipping_cost": float(st.session_state.get("listing_wizard_shipping_cost") or 0.0),
                            "package_weight_oz": float(st.session_state.get("listing_wizard_package_weight_oz") or 0.0),
                            "package_length_in": float(st.session_state.get("listing_wizard_package_length_in") or 0.0),
                            "package_width_in": float(st.session_state.get("listing_wizard_package_width_in") or 0.0),
                            "package_height_in": float(st.session_state.get("listing_wizard_package_height_in") or 0.0),
                            "subtitle": subtitle,
                            "condition_description": condition_description,
                            "aspects_json": str(st.session_state.get("listing_wizard_aspects_json") or "").strip(),
                            "aspects": effective_aspects_payload,
                        },
                        "wizard_mode": str(listing_mode),
                        "wizard_created_by": user.username,
                    }
                    update_payload = {
                        "marketplace_details": json.dumps(details_obj, indent=2),
                        "quantity_listed": int(listing_qty),
                    }
                    if run_publish_live:
                        owner_listing_id = _wizard_external_listing_id_owner(
                            repo,
                            marketplace="ebay",
                            external_listing_id=listing_id,
                            exclude_listing_id=int(created.id),
                        )
                        update_payload.update(
                            {
                                "external_listing_id": (
                                    listing_id if owner_listing_id is None else str(getattr(created, "external_listing_id", "") or "").strip()
                                ),
                                "marketplace_url": (
                                    listing_url if owner_listing_id is None else str(getattr(created, "marketplace_url", "") or "").strip()
                                ),
                                "listing_status": "active" if owner_listing_id is None else "draft",
                                "review_status": "approved" if owner_listing_id is None else str(getattr(created, "review_status", "pending") or "pending").strip().lower(),
                                "reviewed_by": user.username if owner_listing_id is None else str(getattr(created, "reviewed_by", "") or "").strip(),
                                "reviewed_at": utcnow_naive() if owner_listing_id is None else getattr(created, "reviewed_at", None),
                            }
                        )
                    repo.update_listing(
                        int(created.id),
                        update_payload,
                        actor=user.username,
                    )
                    refreshed_listing = repo.get_listing(int(created.id))
                    if run_publish_live:
                        if owner_listing_id is not None:
                            st.warning(
                                f"eBay listingId `{listing_id}` is already linked to local listing #{owner_listing_id}; "
                                "kept this new local listing as draft to avoid linking wrong IDs."
                            )
                        st.success(f"Posted directly to eBay from wizard. listing_id={listing_id}, offer_id={offer_id}")
                        st.link_button("Open eBay Listing", listing_url)
                        stored_external_id = str(
                            getattr(refreshed_listing, "external_listing_id", "") or ""
                        ).strip()
                        stored_url = str(getattr(refreshed_listing, "marketplace_url", "") or "").strip()
                        stored_status = str(getattr(refreshed_listing, "listing_status", "") or "").strip().lower()
                        stored_review = str(getattr(refreshed_listing, "review_status", "") or "").strip().lower()
                        st.markdown("#### eBay Sync Integrity Check")
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "local_listing_id": int(created.id),
                                        "expected_ebay_item_id": str(listing_id or "").strip(),
                                        "stored_ebay_item_id": stored_external_id,
                                        "stored_ebay_url": stored_url,
                                        "stored_listing_status": stored_status,
                                        "stored_review_status": stored_review,
                                    }
                                ]
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )
                        mismatch_fields: list[str] = []
                        if owner_listing_id is None and str(listing_id or "").strip() and stored_external_id != str(listing_id or "").strip():
                            mismatch_fields.append("external_listing_id")
                        if owner_listing_id is None and str(listing_url or "").strip() and stored_url != str(listing_url or "").strip():
                            mismatch_fields.append("marketplace_url")
                        if owner_listing_id is None and stored_status != "active":
                            mismatch_fields.append("listing_status")
                        if owner_listing_id is None and stored_review != "approved":
                            mismatch_fields.append("review_status")
                        if mismatch_fields:
                            st.warning(
                                "Post-publish sync mismatch detected: "
                                + ", ".join(mismatch_fields)
                                + ". Use Listings to repair/sync this row."
                            )
                        else:
                            st.success("Post-publish sync check passed.")
                    else:
                        st.success(
                            f"Created unpublished eBay offer (API draft) from wizard. offer_id={offer_id}. "
                            "Publish later from Listings/eBay Workspace."
                        )
                    st.session_state["ebay_pub_manage_offer_id"] = offer_id
                    st.page_link("pages/03_Listings.py", label="Open Listings and Inspect This Offer")
                except Exception as exc:
                    direct_post_failed = True
                    direct_post_error = str(exc)
                    st.error(f"Direct eBay post failed: {direct_post_error}")
                    try:
                        existing_row = repo.get_listing(int(created.id))
                        details_obj: dict = {}
                        raw_details = str(getattr(existing_row, "marketplace_details", "") or "").strip()
                        if raw_details:
                            try:
                                parsed = json.loads(raw_details)
                                if isinstance(parsed, dict):
                                    details_obj = parsed
                            except Exception:
                                details_obj = {"notes": raw_details}
                        publish_meta = details_obj.get("ebay_publish")
                        if not isinstance(publish_meta, dict):
                            publish_meta = {}
                        publish_meta = _wizard_promote_direct_post_retry_metadata(
                            publish_meta,
                            direct_post_context,
                        )
                        publish_meta["direct_post_last_error"] = direct_post_error
                        publish_meta["direct_post_last_error_at"] = utcnow_naive().isoformat()
                        publish_meta["direct_post_last_error_stage"] = str(direct_post_stage or "").strip()
                        publish_meta["direct_post_last_context"] = direct_post_context
                        details_obj["ebay_publish"] = publish_meta
                        repo.update_listing(
                            int(created.id),
                            {"marketplace_details": json.dumps(details_obj, indent=2)},
                            actor=user.username,
                        )
                    except Exception:
                        pass
        st.session_state["listing_wizard_last_created_id"] = int(created.id)
        st.session_state["listing_wizard_last_created_sku"] = str(selected_product.sku or "").strip()
        st.session_state["listing_wizard_last_created_title"] = str(listing_title).strip()
        # Keep in-run summary state aligned with the newly created draft.
        last_listing_id = int(created.id)
        last_listing_sku = str(selected_product.sku or "").strip()
        last_listing_title = str(listing_title).strip()
        st.session_state.pop("listing_wizard_ai_suggestions", None)
        st.session_state.pop("listing_wizard_ai_diagnostics", None)
        st.session_state.pop("listing_wizard_ai_acceptance", None)
        st.session_state.pop("listing_wizard_ai_comp_evidence", None)
        st.session_state.pop("listing_wizard_existing_media_select", None)
        st.session_state.pop("listing_wizard_show_selected_media_preview", None)
        if post_to_ebay_now and direct_post_failed:
            st.warning(
                "Kept you on Listing Wizard because direct eBay post failed. "
                "Local draft was created and error details were saved in draft marketplace metadata."
            )
        elif stay_on_wizard_after_create:
            st.rerun()
        else:
            _handoff_to_listings_review(
                listing_title=str(listing_title).strip(),
                sku=str(selected_product.sku or "").strip(),
            )
            safe_switch_page(
                "pages/03_Listings.py",
                error_prefix="Open Listings failed",
                info_message="Open Listings from the sidebar.",
            )

    if create_disabled:
        st.caption("Next recommended action: resolve disabled reasons above, then create draft.")

    if last_listing_id > 0:
        st.info(f"Last draft created from wizard: #{last_listing_id} | {last_listing_sku} | {last_listing_title}")
        created_listing = repo.get_listing(int(last_listing_id))
        last_direct_error = ""
        last_direct_error_at = ""
        last_direct_video_diagnostics: dict[str, object] = {}
        if created_listing is not None:
            raw_details = str(getattr(created_listing, "marketplace_details", "") or "").strip()
            if raw_details:
                try:
                    parsed = json.loads(raw_details)
                    if isinstance(parsed, dict):
                        publish_meta = parsed.get("ebay_publish")
                        if isinstance(publish_meta, dict):
                            last_direct_error = str(publish_meta.get("direct_post_last_error") or "").strip()
                            last_direct_error_at = str(
                                publish_meta.get("direct_post_last_error_at") or ""
                            ).strip()
                            last_direct_error_stage = str(
                                publish_meta.get("direct_post_last_error_stage") or ""
                            ).strip()
                            last_direct_error_context = publish_meta.get("direct_post_last_context")
                            if not isinstance(last_direct_error_context, dict):
                                last_direct_error_context = {}
                            video_diag_keys = [
                                "upload_video_to_ebay",
                                "video_attached",
                                "video_upload",
                                "inventory_video_verification",
                                "post_offer_video_verification",
                                "post_publish_video_verification",
                                "trading_listing_video_verification",
                                "skipped_video_count",
                            ]
                            last_direct_video_diagnostics = {
                                key: publish_meta.get(key) for key in video_diag_keys if key in publish_meta
                            }
                except Exception:
                    pass
        ebay_review_url = str(getattr(created_listing, "marketplace_url", "") or "").strip()
        if not ebay_review_url and created_listing is not None:
            external_id = str(getattr(created_listing, "external_listing_id", "") or "").strip()
            if external_id:
                try:
                    ebay_review_url = EbayClient(environment=settings.app_env).listing_url_for_id(external_id)
                except Exception:
                    ebay_review_url = ""
        n1, n2, n3 = st.columns(3)
        with n1:
            if st.button("Open Listings Review Queue", key="listing_wizard_open_listings_review_btn"):
                _handoff_to_listings_review(listing_title=last_listing_title, sku=last_listing_sku)
                safe_switch_page(
                    "pages/03_Listings.py",
                    error_prefix="Open Listings failed",
                    info_message="Open Listings from the sidebar.",
                )
        with n2:
            st.page_link("pages/03_Listings.py", label="Open Listings")
        with n3:
            if ebay_review_url:
                st.link_button("Open Draft on eBay", url=ebay_review_url)
            else:
                st.caption("No eBay draft URL yet. Publish from Listings to generate external draft/listing link.")
        if last_direct_error:
            stamp = f" ({last_direct_error_at})" if last_direct_error_at else ""
            stage_text = f" | stage={last_direct_error_stage}" if str(last_direct_error_stage or "").strip() else ""
            st.warning(f"Last direct eBay post error{stamp}{stage_text}: {last_direct_error}")
            if last_direct_error_context:
                with st.expander("Last Direct Post Diagnostics", expanded=False):
                    st.json(last_direct_error_context)
        if last_direct_video_diagnostics:
            direct_video_warning = str(last_direct_video_diagnostics.get("video_warning") or "").strip()
            if direct_video_warning:
                st.warning(direct_video_warning)
            with st.expander("Last Direct Post Video Diagnostics", expanded=False):
                st.caption(
                    "Shows whether eBay Media upload completed and whether Inventory retained `product.videoIds` "
                    "after inventory upsert, offer create/update, and live publish."
                )
                st.json(last_direct_video_diagnostics)
        st.success("Next recommended action: review the draft in Listings or open on eBay when URL is available.")

    autosave_payload = _wizard_build_draft_payload(
        selected_product_id=selected_product_id,
        selected_template_id=selected_template_id,
    )
    autosave_signature = _wizard_draft_signature(autosave_payload)
    previous_signature = str(st.session_state.get("listing_wizard_last_autosave_signature") or "").strip()
    if autosave_signature != previous_signature:
        autosave_row = repo.save_workflow_draft(
            environment=settings.app_env,
            workflow_key=LISTING_WIZARD_WORKFLOW_KEY,
            username=user.username,
            scope_key=LISTING_WIZARD_WORKFLOW_SCOPE_DEFAULT,
            draft_payload=autosave_payload,
            status="active",
            last_step="step9",
            actor=user.username,
        )
        st.session_state["listing_wizard_last_autosave_signature"] = autosave_signature
        st.session_state["listing_wizard_last_autosave_at"] = utcnow_naive().isoformat()
        st.session_state["listing_wizard_last_draft_id"] = int(autosave_row.id)

    st.caption("You can publish directly here, or use Listings for additional review/revise/batch operations.")
