from datetime import datetime
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from uuid import uuid4

import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import to_decimal_or_none
from app.components.views.shared import (
    generate_sku,
    render_help_panel,
    render_existing_media_attach_selector,
    render_media_capture_inputs,
    upload_media_for_listing,
)
from app.components.views.entity_ops import render_entity_timeline
from app.components.views.workspace_shell import render_workspace_feedback
from app.config import settings
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary, execute_multimodal_task
from app.services.ai_quality import is_weak_intake_text, is_weak_listing_title, load_ai_quality_policy
from app.services.business_chat_room import (
    build_business_room_answer_command_suggestions,
    build_business_room_attachment_evidence_rows,
    build_business_room_handoff_review_card,
    build_business_room_operator_answer_rows,
    list_business_room_workflow_handoffs,
    mark_business_room_workflow_handoff_reviewed,
)
from app.services.ebay import EbayClient
from app.services.purchase_doc_extraction import extract_with_textract_best_effort, merge_llm_and_textract
from app.services.ai_text import (
    coin_grader_structured_to_text,
    normalize_ai_text,
    parse_coin_grader_structured,
)
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_bool, get_runtime_str
from app.utils.time import utc_today, utcnow_naive

INVENTORY_INTAKE_WORKFLOW_KEY = "inventory_intake_wizard"
INVENTORY_INTAKE_WORKFLOW_SCOPE = "default"
INVENTORY_INTAKE_DRAFT_SESSION_KEYS = [
    "inv_intake_default_category",
    "inv_intake_default_title",
    "inv_intake_default_description",
    "inv_intake_default_ai_description",
    "inv_intake_default_ai_comp",
    "inv_intake_default_item_type",
    "inv_intake_default_metal_type",
    "inv_intake_starter_image_bytes",
    "inv_intake_starter_image_name",
    "inv_intake_starter_image_type",
    "inv_intake_include_starter_image_upload",
    "inv_intake_image_start_raw",
    "inv_intake_ai_seed_prompt",
    "inv_intake_ai_suggestion_raw",
    "inv_intake_ai_hint",
    "inv_intake_ai_buffered_media",
    "inv_intake_include_ai_images_on_submit",
    "inv_intake_uploaded_by",
    "coin_identifier_last_result",
    "coin_grader_last_result",
    "coin_grader_last_structured",
    "comp_last_ai_summary",
    "inv_intake_form_category",
    "inv_intake_form_inventory_class",
    "inv_intake_form_item_type",
    "inv_intake_form_metal_type",
    "inv_intake_form_sku_seed_category",
    "inv_intake_form_sku_seed_type",
    "inv_intake_form_sku",
    "inv_intake_form_title",
    "inv_intake_form_description",
    "inv_intake_form_quantity",
    "inv_intake_form_acquisition_cost",
    "inv_intake_form_weight_oz",
    "inv_intake_form_acquisition_tax_paid",
    "inv_intake_form_acquisition_shipping_paid",
    "inv_intake_form_acquisition_handling_paid",
    "inv_intake_form_product_cost",
    "inv_intake_form_ebay_purchase",
    "inv_intake_form_ebay_purchase_item_id",
    "inv_intake_form_ebay_purchase_url",
    "inv_intake_form_package_weight_oz",
    "inv_intake_form_package_length_in",
    "inv_intake_form_package_width_in",
    "inv_intake_form_package_height_in",
    "inv_intake_form_acquired_date",
    "inv_intake_form_source_key",
    "inv_intake_form_lot_key",
    "inv_intake_form_create_new_lot",
    "inv_intake_form_new_lot_code",
    "inv_intake_form_new_lot_vendor",
    "inv_intake_form_new_lot_total_cost",
    "inv_intake_form_new_lot_total_tax_paid",
    "inv_intake_form_new_lot_total_shipping_paid",
    "inv_intake_form_new_lot_total_handling_paid",
    "inv_intake_form_new_lot_expected_total_quantity",
    "inv_intake_form_new_lot_notes",
    "inv_intake_form_purchase_doc_kind",
    "inv_intake_form_purchase_doc_title",
    "inv_intake_form_purchase_doc_run_ai_extract",
    "inv_intake_form_purchase_doc_extraction_mode",
    "inv_intake_form_apply_last_comp_summary",
    "inv_intake_form_apply_last_coin_identifier",
    "inv_intake_form_apply_last_coin_grader",
    "inv_intake_form_ai_graded",
    "inv_intake_form_ai_grading_description",
    "inv_intake_form_ai_description",
    "inv_intake_form_ai_comp",
    "inv_intake_form_create_draft_listing",
    "inv_intake_form_draft_marketplace",
    "inv_intake_form_draft_markup_pct",
    "inv_intake_form_draft_qty",
    "inv_intake_form_attach_uploaded_media_to_listing",
    "inventory_intake_existing_product_media_search_text",
    "inventory_intake_existing_product_media_media_type_filter",
    "inventory_intake_existing_product_media_only_unlinked",
    "inventory_intake_existing_product_media_selected_labels",
    "inventory_intake_existing_listing_media_search_text",
    "inventory_intake_existing_listing_media_media_type_filter",
    "inventory_intake_existing_listing_media_only_unlinked",
    "inventory_intake_existing_listing_media_selected_labels",
    "inventory_intake_media_capture_mode",
    "inventory_intake_listing_media_capture_mode",
    "inv_intake_starter_image_buffered",
    "inv_intake_starter_image_size_bytes",
    "inv_intake_ai_buffered_media_count",
]

INVENTORY_INTAKE_SESSION_DEFAULTS = {
    "inv_intake_default_category": "bullion",
    "inv_intake_default_title": "",
    "inv_intake_default_description": "",
    "inv_intake_default_ai_description": "",
    "inv_intake_default_ai_comp": "",
    "inv_intake_default_item_type": "precious_metal",
    "inv_intake_default_metal_type": "",
    "inv_intake_starter_image_bytes": None,
    "inv_intake_starter_image_name": "",
    "inv_intake_starter_image_type": "",
    "inv_intake_include_starter_image_upload": True,
    "inv_intake_image_start_raw": "",
    "inv_intake_ai_seed_prompt": "",
    "inv_intake_ai_suggestion_raw": "",
    "inv_intake_ai_hint": "",
    "inv_intake_ai_buffered_media": [],
    "inv_intake_include_ai_images_on_submit": True,
    "inv_intake_uploaded_by": "",
    "coin_identifier_last_result": "",
    "coin_grader_last_result": "",
    "coin_grader_last_structured": {},
    "comp_last_ai_summary": "",
    "inv_intake_form_category": "bullion",
    "inv_intake_form_inventory_class": "sellable",
    "inv_intake_form_item_type": "precious_metal",
    "inv_intake_form_metal_type": "",
    "inv_intake_form_sku_seed_category": "bullion",
    "inv_intake_form_sku_seed_type": "precious_metal",
    "inv_intake_form_sku": "",
    "inv_intake_form_title": "",
    "inv_intake_form_description": "",
    "inv_intake_form_quantity": 1,
    "inv_intake_form_acquisition_cost": 0.0,
    "inv_intake_form_weight_oz": 0.0,
    "inv_intake_form_acquisition_tax_paid": 0.0,
    "inv_intake_form_acquisition_shipping_paid": 0.0,
    "inv_intake_form_acquisition_handling_paid": 0.0,
    "inv_intake_form_product_cost": 0.0,
    "inv_intake_form_ebay_purchase": False,
    "inv_intake_form_ebay_purchase_item_id": "",
    "inv_intake_form_ebay_purchase_url": "",
    "inv_intake_form_package_weight_oz": 0.0,
    "inv_intake_form_package_length_in": 0.0,
    "inv_intake_form_package_width_in": 0.0,
    "inv_intake_form_package_height_in": 0.0,
    "inv_intake_form_acquired_date": None,
    "inv_intake_form_source_key": "None",
    "inv_intake_form_lot_key": "None",
    "inv_intake_form_create_new_lot": False,
    "inv_intake_form_new_lot_code": "",
    "inv_intake_form_new_lot_vendor": "",
    "inv_intake_form_new_lot_total_cost": 0.0,
    "inv_intake_form_new_lot_total_tax_paid": 0.0,
    "inv_intake_form_new_lot_total_shipping_paid": 0.0,
    "inv_intake_form_new_lot_total_handling_paid": 0.0,
    "inv_intake_form_new_lot_expected_total_quantity": 0,
    "inv_intake_form_new_lot_notes": "",
    "inv_intake_form_purchase_doc_kind": "incoming_invoice",
    "inv_intake_form_purchase_doc_title": "",
    "inv_intake_form_purchase_doc_run_ai_extract": True,
    "inv_intake_form_purchase_doc_extraction_mode": "llm",
    "inv_intake_form_apply_last_comp_summary": True,
    "inv_intake_form_apply_last_coin_identifier": False,
    "inv_intake_form_apply_last_coin_grader": False,
    "inv_intake_form_ai_graded": False,
    "inv_intake_form_ai_grading_description": "",
    "inv_intake_form_ai_description": "",
    "inv_intake_form_ai_comp": "",
    "inv_intake_form_create_draft_listing": True,
    "inv_intake_form_draft_marketplace": "ebay",
    "inv_intake_form_draft_markup_pct": 20.0,
    "inv_intake_form_draft_qty": 1,
    "inv_intake_form_attach_uploaded_media_to_listing": True,
    "inventory_intake_existing_product_media_search_text": "",
    "inventory_intake_existing_product_media_media_type_filter": "all",
    "inventory_intake_existing_product_media_only_unlinked": True,
    "inventory_intake_existing_product_media_selected_labels": [],
    "inventory_intake_existing_listing_media_search_text": "",
    "inventory_intake_existing_listing_media_media_type_filter": "all",
    "inventory_intake_existing_listing_media_only_unlinked": True,
    "inventory_intake_existing_listing_media_selected_labels": [],
    "inventory_intake_media_capture_mode": "Basic",
    "inventory_intake_listing_media_capture_mode": "Basic",
    "inv_intake_starter_image_buffered": False,
    "inv_intake_starter_image_size_bytes": 0,
    "inv_intake_ai_buffered_media_count": 0,
}


class _BufferedUploadFile:
    def __init__(self, *, name: str, content_type: str, data: bytes) -> None:
        self.name = str(name or "starter_image.jpg")
        self.type = str(content_type or "application/octet-stream")
        self._data = bytes(data or b"")

    def read(self) -> bytes:
        return self._data


def _try_extract_json_object(raw_text: str) -> dict:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        snippet = text[first:last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(BytesIO(file_bytes))
        chunks: list[str] = []
        for page in reader.pages[:10]:
            text = str(page.extract_text() or "").strip()
            if text:
                chunks.append(text)
        return "\n\n".join(chunks).strip()
    except Exception:
        return ""


def _extract_decimal_candidate(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in {".", "-"})
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def _derive_lot_item_subtotal(payload: dict, total: float | None) -> float | None:
    subtotal = _extract_decimal_candidate(payload.get("subtotal"))
    if subtotal is not None:
        return subtotal
    if total is None:
        return None
    components = sum(
        value or 0.0
        for value in (
            _extract_decimal_candidate(payload.get("tax")),
            _extract_decimal_candidate(payload.get("shipping")),
            _extract_decimal_candidate(payload.get("handling")),
        )
    )
    if components <= 0:
        return total
    derived = round(float(total) - float(components), 2)
    return derived if derived >= 0 else total


def _extract_invoice_date_candidate(value: object):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _build_purchase_lot_updates_from_payload(payload: dict) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(payload, dict):
        return {}, {}
    ai_vendor = str(payload.get("vendor_name") or "").strip()
    ai_invoice_date = _extract_invoice_date_candidate(payload.get("invoice_date"))
    ai_total = _extract_decimal_candidate(payload.get("total"))
    ai_subtotal = _derive_lot_item_subtotal(payload, ai_total)
    ai_tax = _extract_decimal_candidate(payload.get("tax"))
    ai_shipping = _extract_decimal_candidate(payload.get("shipping"))
    ai_handling = _extract_decimal_candidate(payload.get("handling"))
    apply_updates: dict[str, object] = {}
    if ai_vendor:
        apply_updates["vendor"] = ai_vendor
    if ai_invoice_date is not None:
        apply_updates["purchase_date"] = datetime.combine(ai_invoice_date, datetime.min.time())
    if ai_subtotal is not None:
        apply_updates["total_cost"] = to_decimal_or_none(ai_subtotal)
    if ai_tax is not None:
        apply_updates["total_tax_paid"] = to_decimal_or_none(ai_tax)
    if ai_shipping is not None:
        apply_updates["total_shipping_paid"] = to_decimal_or_none(ai_shipping)
    if ai_handling is not None:
        apply_updates["total_handling_paid"] = to_decimal_or_none(ai_handling)
    candidates = {
        "vendor": ai_vendor,
        "invoice_date": ai_invoice_date.isoformat() if ai_invoice_date else "n/a",
        "subtotal": ai_subtotal if ai_subtotal is not None else "n/a",
        "total": ai_total if ai_total is not None else "n/a",
        "tax": ai_tax if ai_tax is not None else "n/a",
        "shipping": ai_shipping if ai_shipping is not None else "n/a",
        "handling": ai_handling if ai_handling is not None else "n/a",
    }
    return apply_updates, candidates


def _apply_inventory_intake_ai_defaults_to_form_state(
    *,
    force_identifier: bool,
    force_grader: bool,
    force_comp: bool,
) -> None:
    default_title = str(st.session_state.get("inv_intake_default_title") or "").strip()
    default_metal = str(st.session_state.get("inv_intake_default_metal_type") or "").strip()
    default_description = str(st.session_state.get("inv_intake_default_description") or "").strip()
    default_ai_description = str(st.session_state.get("inv_intake_default_ai_description") or "").strip()
    default_ai_comp = str(st.session_state.get("inv_intake_default_ai_comp") or "").strip()
    last_grader = normalize_ai_text(str(st.session_state.get("coin_grader_last_result") or "").strip())

    if default_title and (force_identifier or not str(st.session_state.get("inv_intake_form_title") or "").strip()):
        st.session_state["inv_intake_form_title"] = default_title
    if default_metal and (force_identifier or not str(st.session_state.get("inv_intake_form_metal_type") or "").strip()):
        st.session_state["inv_intake_form_metal_type"] = default_metal
    if default_description and (
        force_identifier or not str(st.session_state.get("inv_intake_form_description") or "").strip()
    ):
        st.session_state["inv_intake_form_description"] = default_description
    if default_ai_description and (
        force_identifier or not str(st.session_state.get("inv_intake_form_ai_description") or "").strip()
    ):
        st.session_state["inv_intake_form_ai_description"] = default_ai_description

    if last_grader and (
        force_grader or not str(st.session_state.get("inv_intake_form_ai_grading_description") or "").strip()
    ):
        st.session_state["inv_intake_form_ai_grading_description"] = last_grader
    if force_grader and last_grader:
        st.session_state["inv_intake_form_ai_graded"] = True

    if default_ai_comp and (force_comp or not str(st.session_state.get("inv_intake_form_ai_comp") or "").strip()):
        st.session_state["inv_intake_form_ai_comp"] = default_ai_comp


def _normalize_inventory_grader_output(*, raw_result_text: str, structured_grade: dict[str, object]) -> str:
    normalized_grade = (
        coin_grader_structured_to_text(structured_grade)
        if structured_grade
        else normalize_ai_text(raw_result_text)
    )
    if not str(normalized_grade or "").strip():
        normalized_grade = normalize_ai_text(
            raw_result_text,
            preferred_keys=("estimated_grade_range", "recommendation_rationale", "notes"),
        )
    return str(normalized_grade or "").strip()


def _buffer_inventory_ai_images(*, primary, secondary) -> None:
    buffered: list[dict] = []
    for idx, uploaded in enumerate([primary, secondary], start=1):
        if uploaded is None:
            continue
        content_type = str(getattr(uploaded, "type", "") or "image/jpeg").strip() or "image/jpeg"
        ext = "jpg"
        if "/" in content_type:
            ext_candidate = str(content_type.split("/", 1)[1] or "jpg").strip().lower()
            if ext_candidate.isalnum():
                ext = ext_candidate
        name = str(getattr(uploaded, "name", "") or "").strip()
        if not name:
            label = "primary" if idx == 1 else "secondary"
            name = f"inventory_ai_{label}_{uuid4().hex[:8]}.{ext}"
        data = uploaded.getvalue()
        if not data:
            continue
        buffered.append({"name": name, "content_type": content_type, "data": data})
    st.session_state["inv_intake_ai_buffered_media"] = buffered


def _render_ebay_finding_status(*, key_prefix: str) -> None:
    cooldown_remaining = int(EbayClient.finding_rate_limit_cooldown_remaining_seconds() or 0)
    finding_diag = EbayClient.finding_call_snapshot(window_seconds=600) or {}
    finding_last_error = EbayClient.finding_last_error() or {}
    calls_in_window = int(finding_diag.get("count") or 0)
    by_source = finding_diag.get("by_source") or {}
    if isinstance(by_source, dict) and by_source:
        by_source_caption = ", ".join(f"{src}:{int(count)}" for src, count in sorted(by_source.items()))
    else:
        by_source_caption = "none"

    st.caption(
        "eBay Finding activity (last 10m): "
        f"{calls_in_window} call(s) | sources: {by_source_caption}"
    )
    if cooldown_remaining > 0:
        st.warning(
            "Local eBay Finding cooldown is active for this app process "
            f"({cooldown_remaining}s remaining). "
            "This is usually set after eBay returns a remote rate-limit/quota error."
        )
        if finding_last_error:
            last_type = str(finding_last_error.get("type") or "").strip() or "unknown"
            probe_interval = int(finding_last_error.get("probe_interval_seconds") or 0)
            probe_hint = f" | probe interval={probe_interval}s" if probe_interval > 0 else ""
            st.caption(f"Last Finding error type: `{last_type}`{probe_hint}")
        if st.button("Clear Local eBay Finding Cooldown", key=f"{key_prefix}_clear_finding_cooldown"):
            EbayClient.clear_finding_rate_limit_cooldown()
            st.success("Cleared local eBay Finding cooldown for this app process.")
            st.rerun()


def _inventory_intake_parse_draft_json(raw: str) -> dict:
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _inventory_intake_apply_draft_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    for key in INVENTORY_INTAKE_DRAFT_SESSION_KEYS:
        if key in payload:
            st.session_state[key] = payload.get(key)


def _inventory_intake_set_state_default(key: str) -> None:
    default = INVENTORY_INTAKE_SESSION_DEFAULTS.get(key, "")
    if key == "inv_intake_form_acquired_date" and default is None:
        default = utc_today()
    if isinstance(default, (list, dict)):
        default = default.copy()
    st.session_state[key] = default


def _inventory_intake_clear_draft_session_state() -> None:
    for key in INVENTORY_INTAKE_DRAFT_SESSION_KEYS:
        _inventory_intake_set_state_default(key)
    st.session_state["inventory_intake_last_autosave_signature"] = ""
    st.session_state["inventory_intake_last_draft_id"] = 0


def _inventory_intake_take_flag(key: str) -> bool:
    value = bool(st.session_state.get(key, False))
    st.session_state[key] = False
    return value


def _inventory_intake_build_draft_payload() -> dict:
    # Uploaded/captured media binaries are intentionally not persisted in workflow drafts.
    non_persisted_binary_keys = {
        "inv_intake_starter_image_bytes",
        "inv_intake_ai_buffered_media",
    }
    state: dict[str, object] = {}
    for key in INVENTORY_INTAKE_DRAFT_SESSION_KEYS:
        if key in non_persisted_binary_keys:
            continue
        if key in st.session_state:
            state[key] = st.session_state.get(key)
    if "inv_intake_starter_image_bytes" in st.session_state:
        try:
            state["inv_intake_starter_image_buffered"] = bool(st.session_state.get("inv_intake_starter_image_bytes"))
            state["inv_intake_starter_image_size_bytes"] = int(len(st.session_state.get("inv_intake_starter_image_bytes") or b""))
        except Exception:
            state["inv_intake_starter_image_buffered"] = False
            state["inv_intake_starter_image_size_bytes"] = 0
    if "inv_intake_ai_buffered_media" in st.session_state:
        try:
            buffered = list(st.session_state.get("inv_intake_ai_buffered_media") or [])
            state["inv_intake_ai_buffered_media_count"] = int(len(buffered))
        except Exception:
            state["inv_intake_ai_buffered_media_count"] = 0
    return state


def _inventory_intake_draft_signature(payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _render_inventory_intake_business_room_handoffs(repo: InventoryRepository, *, username: str) -> None:
    handoffs = list_business_room_workflow_handoffs(
        repo,
        environment=settings.app_env,
        workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
        username=username,
        limit=20,
    )
    loaded = st.session_state.get("inventory_intake_business_room_handoff_context")
    if isinstance(loaded, dict) and str(loaded.get("prompt") or "").strip():
        st.info(
            "Loaded Business Chat Room handoff context: "
            + str(loaded.get("prompt") or "").strip()[:180]
        )
    if not handoffs:
        st.caption("No Business Chat Room inventory handoffs are waiting for this user.")
        return

    with st.expander(f"Business Chat Room Handoffs ({len(handoffs)})", expanded=False):
        st.caption(
            "Approved room requests routed to Inventory Intake Wizard appear here. Loading one adds its prompt to the intake AI seed; it does not create inventory by itself."
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
        st.dataframe(table_rows, use_container_width=True, hide_index=True)
        options = {
            f"#{row['id']} | job {row['queue_job_id']} | {(row['prompt'] or 'No prompt')[:80]}": row
            for row in handoffs
        }
        selected_label = st.selectbox(
            "Select handoff",
            options=list(options.keys()),
            key="inventory_intake_business_room_handoff_select",
        )
        selected = options[selected_label]
        review_card = build_business_room_handoff_review_card(
            selected,
            workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
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
            key="inventory_intake_business_room_handoff_prompt_preview",
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
        if st.button("Load Handoff Context", key="inventory_intake_load_business_room_handoff_btn"):
            payload = dict(selected.get("payload") or {})
            prompt = str(payload.get("prompt") or selected.get("prompt") or "").strip()
            st.session_state["inventory_intake_business_room_handoff_context"] = payload
            st.session_state["inventory_intake_business_room_review_card"] = review_card
            field_values = dict(review_card.get("field_values") or {})
            title = str(field_values.get("title") or "").strip()
            if title:
                st.session_state["inv_intake_default_title"] = title
                if not str(st.session_state.get("inv_intake_form_title") or "").strip():
                    st.session_state["inv_intake_form_title"] = title
            category = str(field_values.get("category") or "").strip()
            if category in {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}:
                st.session_state["inv_intake_default_category"] = category
                st.session_state["inv_intake_form_category"] = category
            description = str(field_values.get("description") or field_values.get("notes") or "").strip()
            if description:
                st.session_state["inv_intake_default_description"] = description
                if not str(st.session_state.get("inv_intake_form_description") or "").strip():
                    st.session_state["inv_intake_form_description"] = description
            quantity = field_values.get("quantity")
            if quantity not in (None, ""):
                try:
                    st.session_state["inv_intake_form_quantity"] = max(0, int(quantity))
                except Exception:
                    pass
            acquisition_cost = field_values.get("acquisition_cost")
            if acquisition_cost not in (None, ""):
                try:
                    st.session_state["inv_intake_form_acquisition_cost"] = float(acquisition_cost)
                except Exception:
                    pass
            metal_type = str(field_values.get("metal_type") or field_values.get("material") or "").strip()
            if metal_type:
                st.session_state["inv_intake_default_metal_type"] = metal_type
                st.session_state["inv_intake_form_metal_type"] = metal_type
            if prompt:
                existing_seed = str(st.session_state.get("inv_intake_ai_seed_prompt") or "").strip()
                handoff_block = (
                    "\n\nBusiness Chat Room handoff context:\n"
                    + prompt
                    + "\nUse this as operator intent and evidence context for inventory intake."
                )
                if "Business Chat Room handoff context:" not in existing_seed:
                    st.session_state["inv_intake_ai_seed_prompt"] = (existing_seed + handoff_block).strip()
            repo.append_workflow_event(
                environment=settings.app_env,
                workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                username=username,
                scope_key=str(selected.get("scope_key") or INVENTORY_INTAKE_WORKFLOW_SCOPE),
                action="load_business_room_handoff",
                status="ok",
                message="Operator loaded Business Chat Room handoff context into Inventory Intake Wizard session.",
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
            st.success("Loaded handoff context into this Inventory Intake Wizard session.")
            st.rerun()
        if st.button("Mark Handoff Reviewed", key="inventory_intake_reviewed_business_room_handoff_btn"):
            scope_key = str(selected.get("scope_key") or "").strip()
            if not scope_key:
                st.warning("Selected handoff is missing a scope key.")
            else:
                mark_business_room_workflow_handoff_reviewed(
                    repo,
                    environment=settings.app_env,
                    workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                    username=username,
                    actor=username,
                    source="inventory_intake_wizard",
                    handoff=selected,
                )
                st.success("Marked Business Chat Room handoff reviewed.")
                st.rerun()


def render_inventory_intake_wizard(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    quality_policy = load_ai_quality_policy(repo)
    pending_resume_payload = st.session_state.get("inventory_intake_resume_payload")
    st.session_state["inventory_intake_resume_payload"] = None
    if isinstance(pending_resume_payload, dict):
        _inventory_intake_apply_draft_payload(pending_resume_payload)
        st.session_state["inventory_intake_draft_flash"] = "Resumed saved draft."
    saved_draft = repo.load_workflow_draft(
        environment=settings.app_env,
        workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
        username=user.username,
        scope_key=INVENTORY_INTAKE_WORKFLOW_SCOPE,
        active_only=True,
    )
    saved_payload: dict = {}
    if saved_draft is not None:
        saved_payload = _inventory_intake_parse_draft_json(str(saved_draft.draft_json or "{}"))
    st.subheader("Inventory Intake Wizard")
    render_help_panel(
        section_title="Inventory Intake Wizard",
        goal="Intake any incoming inventory type and optionally hand off directly to draft listing workflow.",
        steps=[
            "Capture item category and inventory details.",
            "Assign source/lot context (or create a new lot inline).",
            "Upload media once for product/listing reuse.",
            "Optionally create a draft listing in the same flow.",
        ],
        roadmap_phase="v0.6 GS-V06-006 Inventory Intake Wizard (Generalized)",
    )
    st.page_link("pages/06_Tools.py", label="Open AI Tools (Comp / Coin / Chat)")
    draft_flash = str(st.session_state.get("inventory_intake_draft_flash") or "").strip()
    st.session_state["inventory_intake_draft_flash"] = ""
    if draft_flash:
        st.success(draft_flash)
    starter_buffered = bool(st.session_state.get("inv_intake_starter_image_buffered"))
    starter_size = int(st.session_state.get("inv_intake_starter_image_size_bytes") or 0)
    ai_buffered_count = int(st.session_state.get("inv_intake_ai_buffered_media_count") or 0)
    lost_starter = starter_buffered and not st.session_state.get("inv_intake_starter_image_bytes")
    lost_ai = ai_buffered_count > 0 and not st.session_state.get("inv_intake_ai_buffered_media")
    if lost_starter or lost_ai:
        lost_parts: list[str] = []
        if lost_starter:
            lost_parts.append(f"starter image ({starter_size} bytes)")
        if lost_ai:
            lost_parts.append(f"{ai_buffered_count} AI assist image(s)")
        st.warning(
            "Resumed draft references buffered local media that is not available after restart: "
            + ", ".join(lost_parts)
            + ". Reattach files before submit if needed."
        )
    dc1, dc2, dc3 = st.columns([1, 1, 1])
    with dc1:
        if st.button("Save Draft", key="inventory_intake_save_draft_btn"):
            payload = _inventory_intake_build_draft_payload()
            row = repo.save_workflow_draft(
                environment=settings.app_env,
                workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                username=user.username,
                scope_key=INVENTORY_INTAKE_WORKFLOW_SCOPE,
                draft_payload=payload,
                schema_version="v1",
                status="active",
                last_step="intake",
                actor=user.username,
            )
            repo.append_workflow_event(
                environment=settings.app_env,
                workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                username=user.username,
                scope_key=INVENTORY_INTAKE_WORKFLOW_SCOPE,
                action="save_draft",
                status="ok",
                message="Operator saved inventory intake draft.",
                payload={"draft_id": int(getattr(row, "id", 0) or 0)},
                draft_id=int(getattr(row, "id", 0) or 0),
                actor=user.username,
            )
            st.session_state["inventory_intake_last_autosave_signature"] = _inventory_intake_draft_signature(payload)
            st.session_state["inventory_intake_draft_flash"] = "Saved draft."
            st.rerun()
    with dc2:
        if st.button("Resume Draft", key="inventory_intake_resume_draft_btn"):
            resumed = repo.resume_latest_workflow_draft(
                environment=settings.app_env,
                workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                username=user.username,
                active_only=True,
            )
            payload = saved_payload
            if resumed is not None:
                payload = _inventory_intake_parse_draft_json(str(resumed.draft_json or "{}"))
            st.session_state["inventory_intake_resume_payload"] = payload
            repo.append_workflow_event(
                environment=settings.app_env,
                workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                username=user.username,
                scope_key=INVENTORY_INTAKE_WORKFLOW_SCOPE,
                action="resume_draft",
                status="ok",
                message="Operator resumed inventory intake draft.",
                payload={"draft_id": int(getattr(resumed, "id", 0) or getattr(saved_draft, "id", 0) or 0)},
                draft_id=int(getattr(resumed, "id", 0) or getattr(saved_draft, "id", 0) or 0),
                actor=user.username,
            )
            st.rerun()
    with dc3:
        if st.button("Clear Draft", key="inventory_intake_clear_draft_btn"):
            repo.clear_workflow_draft(
                environment=settings.app_env,
                workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
                username=user.username,
                scope_key=INVENTORY_INTAKE_WORKFLOW_SCOPE,
                actor=user.username,
                reason="operator_reset",
            )
            _inventory_intake_clear_draft_session_state()
            st.session_state["inventory_intake_draft_flash"] = "Cleared draft."
            st.rerun()
    _render_inventory_intake_business_room_handoffs(repo, username=user.username)
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="inventory_intake_wizard",
        section_title="Workspace Feedback",
    )

    sources = repo.list_inventory_sources(active_only=True)
    source_options = {"None": None, **{f"#{row.id} | {row.name} | {row.source_type}": row.id for row in sources}}
    lots = repo.list_purchase_lots()
    lot_options = {"None": None, **{f"{row.lot_code} | {row.vendor}": row.id for row in lots}}

    ai_default_category = str(st.session_state.get("inv_intake_default_category") or "bullion")
    if ai_default_category not in {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}:
        ai_default_category = "bullion"
    ai_default_title = str(st.session_state.get("inv_intake_default_title") or "").strip()
    ai_default_description = str(st.session_state.get("inv_intake_default_description") or "").strip()
    ai_default_ai_description = str(st.session_state.get("inv_intake_default_ai_description") or "").strip()
    ai_default_ai_comp = str(st.session_state.get("inv_intake_default_ai_comp") or "").strip()
    ai_default_item_type = str(st.session_state.get("inv_intake_default_item_type") or "precious_metal").strip()
    if ai_default_item_type not in {"precious_metal", "collectible", "antique", "general_merchandise", "other"}:
        ai_default_item_type = "precious_metal"
    ai_default_metal_type = str(st.session_state.get("inv_intake_default_metal_type") or "").strip()

    st.markdown("### Intake Start (Optional Image)")
    st.caption("Start intake from a photo/image; AI can prefill title/category/type/description before form entry.")
    with st.expander("Camera (Starter Image)", expanded=False):
        seed_camera_image = st.camera_input(
            "Take Starter Photo (optional)",
            key="inv_intake_seed_camera_image",
        )
    seed_uploaded_image = st.file_uploader(
        "Or Upload Starter Image (optional)",
        type=["jpg", "jpeg", "png", "webp", "gif"],
        key="inv_intake_seed_uploaded_image",
    )
    seed_image_hint = st.text_input(
        "Image Context Hint (optional)",
        key="inv_intake_seed_image_hint",
        placeholder="Example: antique brass pocket watch / sterling flatware / 1 oz silver bar",
    )
    include_starter_image_upload = st.checkbox(
        "Include starter image in intake media upload on submit",
        value=True,
        key="inv_intake_include_starter_image_upload",
        help="When enabled, the analyzed starter image is also saved as product/listing media during intake submit.",
    )
    stored_starter_bytes = st.session_state.get("inv_intake_starter_image_bytes")
    stored_starter_name = str(st.session_state.get("inv_intake_starter_image_name") or "").strip()
    stored_starter_type = str(st.session_state.get("inv_intake_starter_image_type") or "").strip()
    if stored_starter_bytes:
        st.caption(
            f"Starter image buffered for upload: `{stored_starter_name or 'starter_image.jpg'}` "
            f"({len(stored_starter_bytes)} bytes)"
        )
        if str(stored_starter_type).startswith("image/"):
            try:
                st.image(stored_starter_bytes, caption="Buffered starter image", use_container_width=True)
            except Exception:
                st.caption("Starter image preview unavailable (invalid image bytes).")
        if st.button("Clear Buffered Starter Image", key="inv_intake_clear_starter_buffer"):
            st.session_state["inv_intake_starter_image_bytes"] = None
            st.session_state["inv_intake_starter_image_name"] = ""
            st.session_state["inv_intake_starter_image_type"] = ""
            st.session_state["inv_intake_starter_image_buffered"] = False
            st.session_state["inv_intake_starter_image_size_bytes"] = 0
            st.success("Cleared buffered starter image.")
            st.rerun()
    if st.button("Analyze Starter Image For Intake Defaults", key="inv_intake_image_start_analyze"):
        if not ensure_permission(user, "ai_comp_use", "Analyze Intake Starter Image"):
            st.stop()
        chosen_image = seed_camera_image or seed_uploaded_image
        if chosen_image is None:
            st.error("Add a starter image first (camera photo or uploaded image).")
            st.stop()
        try:
            system_message = get_runtime_str(
                repo,
                "comp_llm_system_message",
                "You are a resale inventory assistant. Return concise outputs.",
            ).strip()
            instruction = (
                "Analyze the image and return ONLY JSON with keys: "
                "`suggested_title`, `suggested_category`, `suggested_item_type`, "
                "`suggested_metal_type`, `suggested_description`, `suggested_ai_description`, "
                "`comp_search_query`. "
                "Use categories only from: bullion, collectibles, antiques, coins, normal_goods, other. "
                "Use item types only from: precious_metal, collectible, antique, general_merchandise, other. "
                "Keep descriptions concise and listing-usable."
            )
            hint = str(seed_image_hint or "").strip()
            if hint:
                instruction += f"\nContext hint from operator: {hint}"
            image_bytes = chosen_image.getvalue()
            image_content_type = str(getattr(chosen_image, "type", "") or "image/jpeg").strip() or "image/jpeg"
            ext = "jpg"
            if "/" in image_content_type:
                ext = str(image_content_type.split("/", 1)[1] or "jpg").strip().lower()
                if not ext.isalnum():
                    ext = "jpg"
            chosen_name = str(getattr(chosen_image, "name", "") or "").strip()
            if not chosen_name:
                chosen_name = f"intake_starter_{uuid4().hex[:8]}.{ext}"
            st.session_state["inv_intake_starter_image_bytes"] = image_bytes
            st.session_state["inv_intake_starter_image_name"] = chosen_name
            st.session_state["inv_intake_starter_image_type"] = image_content_type
            result = execute_multimodal_task(
                repo,
                tool_name="inventory_intake_image_start",
                system_message=system_message,
                instruction=instruction,
                image_bytes=image_bytes,
                image_content_type=image_content_type,
                max_output_tokens_override=900,
                workflow="intake",
                context={"hint": hint},
            )
            payload = _try_extract_json_object(result.text)
            if not payload:
                st.warning("Image analysis output was not valid JSON. See raw response below.")
            else:
                title_val = str(payload.get("suggested_title") or "").strip()
                category_val = str(payload.get("suggested_category") or "").strip().lower()
                item_type_val = str(payload.get("suggested_item_type") or "").strip().lower()
                metal_type_val = str(payload.get("suggested_metal_type") or "").strip()
                description_val = str(payload.get("suggested_description") or "").strip()
                ai_desc_val = str(payload.get("suggested_ai_description") or "").strip()
                comp_query_val = str(payload.get("comp_search_query") or "").strip()
                if title_val and not is_weak_listing_title(title_val, policy=quality_policy):
                    st.session_state["inv_intake_default_title"] = title_val
                if category_val in {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}:
                    st.session_state["inv_intake_default_category"] = category_val
                if item_type_val in {"precious_metal", "collectible", "antique", "general_merchandise", "other"}:
                    st.session_state["inv_intake_default_item_type"] = item_type_val
                if metal_type_val:
                    st.session_state["inv_intake_default_metal_type"] = metal_type_val
                if description_val and not is_weak_intake_text(description_val, policy=quality_policy):
                    st.session_state["inv_intake_default_description"] = description_val
                if ai_desc_val and not is_weak_intake_text(ai_desc_val, policy=quality_policy):
                    st.session_state["inv_intake_default_ai_description"] = ai_desc_val
                if comp_query_val:
                    st.session_state["inv_intake_ai_seed_prompt"] = comp_query_val
                st.success("Image analysis applied to intake defaults.")
            st.session_state["inv_intake_image_start_raw"] = str(result.text or "").strip()
            st.rerun()
        except Exception as exc:
            st.error(f"Starter image analysis failed: {exc}")

    raw_image_start = str(st.session_state.get("inv_intake_image_start_raw") or "").strip()
    show_ai_diagnostics = st.checkbox(
        "Load AI Diagnostic Panels (slower)",
        value=False,
        key="inv_intake_load_ai_diagnostics",
        help="Enable raw AI payload/structured-debug panels for troubleshooting.",
    )
    render_full_ai_payloads = False
    if show_ai_diagnostics:
        render_full_ai_payloads = st.checkbox(
            "Render Full AI Payloads (slowest)",
            value=False,
            key="inv_intake_render_full_ai_payloads",
            help="When disabled, diagnostics show compact summaries instead of full JSON/code payloads.",
        )
    if raw_image_start and show_ai_diagnostics:
        with st.expander("Last Starter Image Analysis", expanded=False):
            if render_full_ai_payloads:
                st.code(raw_image_start, language="json")
            else:
                st.caption(f"Starter analysis payload captured ({len(raw_image_start)} chars).")

    st.markdown("### AI Suggestion Helper")
    ai_seed_prompt = st.text_area(
        "AI Seed Prompt (optional)",
        key="inv_intake_ai_seed_prompt",
        help="Add brand/style/context hints for suggested title/category/description.",
    )
    if st.button("Generate AI Suggestions", key="inv_intake_generate_ai_suggestions"):
        if not ensure_permission(user, "ai_comp_use", "Generate Intake AI Suggestions"):
            st.stop()
        try:
            system_message = get_runtime_str(
                repo,
                "comp_llm_system_message",
                "You are a resale inventory assistant. Return concise outputs.",
            ).strip()
            instruction = (
                "Return ONLY JSON with keys: "
                "`suggested_title`, `suggested_category`, `suggested_description`, `suggested_ai_description`. "
                "Use categories only from: bullion, collectibles, antiques, coins, normal_goods, other. "
                "Keep descriptions concise and listing-usable."
            )
            query_parts = [
                str(ai_seed_prompt or "").strip(),
                str(ai_default_title or "").strip(),
                str(ai_default_description or "").strip(),
            ]
            query_text = " | ".join([p for p in query_parts if p]).strip() or "General inventory intake suggestion"
            result = execute_comp_summary(
                repo,
                query=query_text,
                ebay_rows=[],
                web_rows=[],
                spot_context={},
                system_message=system_message,
                instruction=instruction,
                workflow="intake",
            )
            payload = _try_extract_json_object(result.text)
            if not payload:
                st.warning("AI output was not valid JSON. Showing raw text in AI Description default.")
                st.session_state["inv_intake_default_ai_description"] = str(result.text or "").strip()
            else:
                title_val = str(payload.get("suggested_title") or "").strip()
                category_val = str(payload.get("suggested_category") or "").strip().lower()
                description_val = str(payload.get("suggested_description") or "").strip()
                ai_desc_val = str(payload.get("suggested_ai_description") or "").strip()
                if title_val and not is_weak_listing_title(title_val, policy=quality_policy):
                    st.session_state["inv_intake_default_title"] = title_val
                if category_val in {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}:
                    st.session_state["inv_intake_default_category"] = category_val
                if description_val and not is_weak_intake_text(description_val, policy=quality_policy):
                    st.session_state["inv_intake_default_description"] = description_val
                if ai_desc_val and not is_weak_intake_text(ai_desc_val, policy=quality_policy):
                    st.session_state["inv_intake_default_ai_description"] = ai_desc_val
                st.session_state["inv_intake_ai_suggestion_raw"] = result.text
                st.success("AI suggestions generated and applied to wizard defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"AI suggestion generation failed: {exc}")

    raw_ai = str(st.session_state.get("inv_intake_ai_suggestion_raw") or "").strip()
    if raw_ai and show_ai_diagnostics:
        with st.expander("Last AI Suggestion Payload", expanded=False):
            if render_full_ai_payloads:
                st.code(raw_ai, language="json")
            else:
                st.caption(f"AI suggestion payload captured ({len(raw_ai)} chars).")

    st.markdown("### Wizard AI Assist (Run Directly Here)")
    st.caption("Run Identifier, Grader, and Comp before submit to prefill intake fields.")
    ai_hint = st.text_input(
        "AI Hint (optional)",
        key="inv_intake_ai_hint",
        placeholder="Example: 1 oz silver bar, toned condition, vintage hallmark",
    )
    ai_image_upload = st.file_uploader(
        "Upload Item Image (AI Assist)",
        type=["jpg", "jpeg", "png", "webp"],
        key="inv_intake_ai_image_upload",
    )
    with st.expander("Camera (AI Assist Primary)", expanded=False):
        ai_camera_image = st.camera_input(
            "Capture Item Image (AI Assist)",
            key="inv_intake_ai_camera_image",
        )
    ai_reverse_upload = st.file_uploader(
        "Upload Additional Image (optional)",
        type=["jpg", "jpeg", "png", "webp"],
        key="inv_intake_ai_reverse_upload",
    )
    with st.expander("Camera (AI Assist Additional)", expanded=False):
        ai_reverse_camera = st.camera_input(
            "Capture Additional Image (optional)",
            key="inv_intake_ai_reverse_camera",
        )
    ai_image = ai_camera_image or ai_image_upload
    ai_reverse = ai_reverse_camera or ai_reverse_upload
    ai_c1, ai_c2, ai_c3 = st.columns(3)
    run_identifier = ai_c1.button("Run Identifier", key="inv_intake_run_identifier")
    run_grader = ai_c2.button("Run Grader", key="inv_intake_run_grader")
    run_comp = ai_c3.button("Run Comp", key="inv_intake_run_comp")
    _render_ebay_finding_status(key_prefix="inv_intake")
    include_ai_images_on_submit = st.checkbox(
        "Include AI assist images in intake media on submit",
        value=True,
        key="inv_intake_include_ai_images_on_submit",
    )
    buffered_ai_images = st.session_state.get("inv_intake_ai_buffered_media") or []
    if buffered_ai_images:
        st.caption(f"Buffered AI assist images: {len(buffered_ai_images)}")
        if st.button("Clear Buffered AI Assist Images", key="inv_intake_clear_ai_buffered"):
            st.session_state["inv_intake_ai_buffered_media"] = []
            st.session_state["inv_intake_ai_buffered_media_count"] = 0
            st.success("Cleared buffered AI assist images.")
            st.rerun()

    if run_identifier:
        if not ensure_permission(user, "ai_coin_identify", "Run Identifier (Inventory Wizard)"):
            st.stop()
        if ai_image is None and not str(ai_hint or "").strip():
            st.error("Provide an image or hint to run identifier.")
        else:
            try:
                _buffer_inventory_ai_images(primary=ai_image, secondary=ai_reverse)
                system_message = get_runtime_str(
                    repo,
                    "coin_identifier_system_message",
                    "You are a careful identifier. Prefer precision and state uncertainty clearly.",
                ).strip()
                instruction = get_runtime_str(
                    repo,
                    "coin_identifier_instruction_template",
                    (
                        "Identify the item from image and notes. "
                        "Respond as strict JSON object with keys: "
                        "coin_name, possible_country_or_mint, year_or_period, denomination, metal, "
                        "confidence, search_keywords, notes."
                    ),
                ).strip()
                image_bytes = ai_image.getvalue() if ai_image is not None else b""
                image_type = str(getattr(ai_image, "type", "") or "image/jpeg")
                reverse_bytes = ai_reverse.getvalue() if ai_reverse is not None else None
                reverse_type = str(getattr(ai_reverse, "type", "") or "image/jpeg") if ai_reverse is not None else "image/jpeg"
                result = execute_multimodal_task(
                    repo,
                    tool_name="inventory_identifier_wizard",
                    system_message=system_message,
                    instruction=f"{instruction}\nUser hint: {str(ai_hint or '').strip() or '(none)'}",
                    image_bytes=image_bytes if image_bytes else b"",
                    image_content_type=image_type,
                    additional_images=[(reverse_bytes, reverse_type)] if reverse_bytes else [],
                    workflow="intake",
                    context={"source": "inventory_intake_wizard"},
                )
                payload = _try_extract_json_object(result.text)
                normalized = normalize_ai_text(result.text)
                st.session_state["coin_identifier_last_result"] = normalized
                st.session_state["inv_intake_default_ai_description"] = normalized
                if isinstance(payload, dict):
                    title_val = str(
                        payload.get("coin_name")
                        or payload.get("title")
                        or payload.get("item_title")
                        or payload.get("item_name")
                        or payload.get("name")
                        or ""
                    ).strip()
                    metal_val = str(
                        payload.get("metal")
                        or payload.get("metal_type")
                        or payload.get("composition")
                        or ""
                    ).strip()
                    notes_val = str(
                        payload.get("notes")
                        or payload.get("description")
                        or payload.get("details")
                        or payload.get("summary")
                        or ""
                    ).strip()
                    if title_val:
                        st.session_state["inv_intake_default_title"] = title_val
                    if metal_val:
                        st.session_state["inv_intake_default_metal_type"] = metal_val
                    if notes_val:
                        st.session_state["inv_intake_default_description"] = notes_val
                if normalized and not str(st.session_state.get("inv_intake_default_description") or "").strip():
                    if not is_weak_intake_text(normalized, policy=quality_policy):
                        st.session_state["inv_intake_default_description"] = normalized
                st.session_state["inv_intake_force_apply_identifier_prefill"] = True
                st.success("Identifier completed and intake defaults updated.")
                st.rerun()
            except Exception as exc:
                st.error(f"Identifier failed: {exc}")

    if run_grader:
        if not ensure_permission(user, "ai_coin_grade", "Run Grader (Inventory Wizard)"):
            st.stop()
        if ai_image is None:
            st.error("Provide an image to run grader.")
        else:
            try:
                _buffer_inventory_ai_images(primary=ai_image, secondary=ai_reverse)
                system_message = get_runtime_str(
                    repo,
                    "coin_grader_system_message",
                    "You are a conservative grading assistant.",
                ).strip()
                instruction = get_runtime_str(
                    repo,
                    "coin_grader_instruction_template",
                    "Estimate item condition and return practical grading notes.",
                ).strip()
                result = execute_multimodal_task(
                    repo,
                    tool_name="inventory_grader_wizard",
                    system_message=system_message,
                    instruction=f"{instruction}\nUser hint: {str(ai_hint or '').strip() or '(none)'}",
                    image_bytes=ai_image.getvalue(),
                    image_content_type=str(getattr(ai_image, "type", "") or "image/jpeg"),
                    additional_images=[(ai_reverse.getvalue(), str(getattr(ai_reverse, "type", "") or "image/jpeg"))]
                    if ai_reverse is not None
                    else [],
                    workflow="intake",
                    context={"source": "inventory_intake_wizard"},
                )
                structured_grade = parse_coin_grader_structured(result.text)
                normalized_grade = _normalize_inventory_grader_output(
                    raw_result_text=result.text,
                    structured_grade=structured_grade,
                )
                st.session_state["coin_grader_last_result"] = normalized_grade
                st.session_state["coin_grader_last_structured"] = structured_grade
                st.session_state["inv_intake_force_apply_grader_prefill"] = True
                if normalized_grade:
                    # Keep wizard fields in sync immediately after the grader call.
                    st.session_state["inv_intake_form_ai_grading_description"] = normalized_grade
                    st.session_state["inv_intake_form_ai_graded"] = True
                st.success("Grader completed and applied to last grader output.")
                st.rerun()
            except Exception as exc:
                st.error(f"Grader failed: {exc}")

    last_structured_grade = st.session_state.get("coin_grader_last_structured") or {}
    if last_structured_grade and show_ai_diagnostics:
        with st.expander("Last Grader Structured Result", expanded=False):
            if render_full_ai_payloads:
                st.json(last_structured_grade)
            else:
                st.caption(
                    "Structured grader payload captured. Enable `Render Full AI Payloads (slowest)` to view full JSON."
                )
            g1, g2, g3 = st.columns(3)
            with g1:
                st.caption(
                    f"Estimated Grade Range: `{str(last_structured_grade.get('estimated_grade_range') or '').strip() or 'n/a'}`"
                )
            with g2:
                st.caption(
                    f"Recommendation: `{str(last_structured_grade.get('submit_for_professional_grading') or '').strip() or 'n/a'}`"
                )
            with g3:
                st.caption(
                    f"Net Upside (USD): `{str(last_structured_grade.get('estimated_net_upside_usd') or '').strip() or 'n/a'}`"
                )

    if run_comp:
        if not ensure_permission(user, "ai_comp_use", "Run Comp (Inventory Wizard)"):
            st.stop()
        query = " ".join(
            [
                str(ai_hint or "").strip(),
                str(st.session_state.get("inv_intake_default_title") or "").strip(),
                str(st.session_state.get("inv_intake_default_metal_type") or "").strip(),
                str(st.session_state.get("inv_intake_default_description") or "").strip(),
            ]
        ).strip()
        if not query:
            st.error("Add a hint or run identifier first so a query can be built.")
        else:
            try:
                _buffer_inventory_ai_images(primary=ai_image, secondary=ai_reverse)
                ebay_rows: list[dict] = []
                rate_limited_note = ""
                client = EbayClient()
                if client.is_configured():
                    comp_outcome = client.find_completed_items_with_fallback(
                        keywords=query,
                        sold_only=True,
                        entries_per_page=25,
                        page_number=1,
                        source="inventory_intake_wizard_primary",
                        auto_broaden=True,
                        allow_html_fallback=True,
                    )
                    ebay_rows = list(comp_outcome.get("rows") or [])
                    rate_limited_note = str(comp_outcome.get("rate_limited_note") or "").strip()
                if rate_limited_note:
                    st.warning(
                        "eBay Finding API is rate-limited. Continuing with fallback comp sources. "
                        f"Details: {rate_limited_note}"
                    )
                comp_result = execute_comp_summary(
                    repo,
                    query=query,
                    ebay_rows=ebay_rows,
                    web_rows=[],
                    spot_context={},
                    system_message=get_runtime_str(
                        repo,
                        "comp_llm_system_message",
                        "You are a conservative resale pricing assistant.",
                    ).strip(),
                    instruction=get_runtime_str(
                        repo,
                        "comp_llm_instruction",
                        "Summarize comp pricing and provide a practical suggested range.",
                    ).strip(),
                    workflow="intake",
                )
                normalized_comp = normalize_ai_text(comp_result.text)
                st.session_state["comp_last_ai_summary"] = normalized_comp
                st.session_state["inv_intake_default_ai_comp"] = normalized_comp
                st.session_state["inv_intake_force_apply_comp_prefill"] = True
                st.success("Comp completed and AI Comp default updated.")
                st.rerun()
            except Exception as exc:
                st.error(f"Comp failed: {exc}")

    st.markdown("### 3) Optional Media Upload")
    uploaded_by = st.text_input("Uploaded By", value=user.username, key="inv_intake_uploaded_by")
    intake_media = render_media_capture_inputs(
        key_prefix="inventory_intake_media",
        upload_label="Product Photos/Videos (optional)",
        allow_enhanced=True,
    )
    listing_media = render_media_capture_inputs(
        key_prefix="inventory_intake_listing_media",
        upload_label="Draft Listing Photos/Videos (optional, multiple images + video supported)",
        allow_enhanced=True,
    )
    existing_media_rows_shared = None
    if bool(st.session_state.get("inventory_intake_existing_product_media_load_media")) or bool(
        st.session_state.get("inventory_intake_existing_listing_media_load_media")
    ):
        existing_media_rows_shared = repo.list_media_assets(limit=300)
    existing_product_media_ids = render_existing_media_attach_selector(
        repo=repo,
        key_prefix="inventory_intake_existing_product_media",
        section_title="Attach Existing Media To New Product (Optional)",
        help_text="Bulk-link already uploaded media assets to the product created by this wizard.",
        defer_load=True,
        preloaded_rows=existing_media_rows_shared,
    )
    existing_listing_media_ids = render_existing_media_attach_selector(
        repo=repo,
        key_prefix="inventory_intake_existing_listing_media",
        section_title="Attach Existing Media To New Draft Listing (Optional)",
        help_text="Bulk-link existing media assets to the draft listing created by this wizard.",
        defer_load=True,
        preloaded_rows=existing_media_rows_shared,
    )

    with st.form("inventory_intake_wizard_form", clear_on_submit=False):
        force_identifier = _inventory_intake_take_flag("inv_intake_force_apply_identifier_prefill")
        force_grader = _inventory_intake_take_flag("inv_intake_force_apply_grader_prefill")
        force_comp = _inventory_intake_take_flag("inv_intake_force_apply_comp_prefill")
        _apply_inventory_intake_ai_defaults_to_form_state(
            force_identifier=force_identifier,
            force_grader=force_grader,
            force_comp=force_comp,
        )
        st.session_state.setdefault("inv_intake_form_category", ai_default_category)
        st.session_state.setdefault("inv_intake_form_inventory_class", "sellable")
        st.session_state.setdefault("inv_intake_form_item_type", ai_default_item_type)
        st.session_state.setdefault("inv_intake_form_metal_type", ai_default_metal_type)
        st.session_state.setdefault("inv_intake_form_sku_seed_category", ai_default_category)
        st.session_state.setdefault("inv_intake_form_sku_seed_type", ai_default_item_type)
        generated_sku = generate_sku(
            str(st.session_state.get("inv_intake_form_sku_seed_category") or ai_default_category),
            str(st.session_state.get("inv_intake_form_sku_seed_type") or ai_default_item_type),
        )
        st.session_state.setdefault("inv_intake_form_sku", generated_sku)
        st.session_state.setdefault("inv_intake_form_title", ai_default_title)
        st.session_state.setdefault("inv_intake_form_description", ai_default_description)
        st.session_state.setdefault("inv_intake_form_quantity", 1)
        st.session_state.setdefault("inv_intake_form_acquisition_cost", 0.0)
        st.session_state.setdefault("inv_intake_form_weight_oz", 0.0)
        st.session_state.setdefault("inv_intake_form_acquisition_tax_paid", 0.0)
        st.session_state.setdefault("inv_intake_form_acquisition_shipping_paid", 0.0)
        st.session_state.setdefault("inv_intake_form_acquisition_handling_paid", 0.0)
        st.session_state.setdefault("inv_intake_form_product_cost", 0.0)
        st.session_state.setdefault("inv_intake_form_ebay_purchase", False)
        st.session_state.setdefault("inv_intake_form_ebay_purchase_item_id", "")
        st.session_state.setdefault("inv_intake_form_ebay_purchase_url", "")
        st.session_state.setdefault("inv_intake_form_package_weight_oz", 0.0)
        st.session_state.setdefault("inv_intake_form_package_length_in", 0.0)
        st.session_state.setdefault("inv_intake_form_package_width_in", 0.0)
        st.session_state.setdefault("inv_intake_form_package_height_in", 0.0)
        st.session_state.setdefault("inv_intake_form_acquired_date", utc_today())
        source_default = str(st.session_state.get("inv_intake_form_source_key") or "None")
        if source_default not in source_options:
            st.session_state["inv_intake_form_source_key"] = "None"
        lot_default = str(st.session_state.get("inv_intake_form_lot_key") or "None")
        if lot_default not in lot_options:
            st.session_state["inv_intake_form_lot_key"] = "None"
        st.session_state.setdefault("inv_intake_form_create_new_lot", False)
        st.session_state.setdefault("inv_intake_form_new_lot_code", "")
        st.session_state.setdefault("inv_intake_form_new_lot_vendor", "")
        st.session_state.setdefault("inv_intake_form_new_lot_total_cost", 0.0)
        st.session_state.setdefault("inv_intake_form_new_lot_total_tax_paid", 0.0)
        st.session_state.setdefault("inv_intake_form_new_lot_total_shipping_paid", 0.0)
        st.session_state.setdefault("inv_intake_form_new_lot_total_handling_paid", 0.0)
        st.session_state.setdefault("inv_intake_form_new_lot_expected_total_quantity", 0)
        st.session_state.setdefault("inv_intake_form_new_lot_notes", "")
        st.session_state.setdefault("inv_intake_form_purchase_doc_kind", "incoming_invoice")
        st.session_state.setdefault("inv_intake_form_purchase_doc_title", "")
        st.session_state.setdefault("inv_intake_form_purchase_doc_run_ai_extract", True)
        textract_enabled = get_runtime_bool(repo, "purchase_doc_textract_enabled", True)
        if textract_enabled:
            st.session_state.setdefault("inv_intake_form_purchase_doc_extraction_mode", "both")
        else:
            st.session_state.setdefault("inv_intake_form_purchase_doc_extraction_mode", "llm")
        st.session_state.setdefault("inv_intake_form_apply_last_comp_summary", True)
        st.session_state.setdefault("inv_intake_form_apply_last_coin_identifier", False)
        st.session_state.setdefault("inv_intake_form_apply_last_coin_grader", False)
        st.session_state.setdefault("inv_intake_form_ai_graded", False)
        st.session_state.setdefault("inv_intake_form_ai_grading_description", "")
        st.session_state.setdefault("inv_intake_form_ai_description", ai_default_ai_description)
        st.session_state.setdefault("inv_intake_form_ai_comp", ai_default_ai_comp)
        st.session_state.setdefault("inv_intake_form_create_draft_listing", True)
        st.session_state.setdefault("inv_intake_form_draft_marketplace", "ebay")
        st.session_state.setdefault("inv_intake_form_draft_markup_pct", 20.0)
        st.session_state.setdefault("inv_intake_form_draft_qty", 1)
        st.session_state.setdefault("inv_intake_form_attach_uploaded_media_to_listing", True)

        st.markdown("### 1) Item + Inventory")
        c1, c2, c3 = st.columns(3)
        with c1:
            category = st.selectbox(
                "Category",
                ["bullion", "collectibles", "antiques", "coins", "normal_goods", "other"],
                key="inv_intake_form_category",
            )
        with c2:
            inventory_class = st.selectbox(
                "Inventory Class",
                ["sellable", "raw_material", "supply"],
                key="inv_intake_form_inventory_class",
                help="Choose `raw_material` or `supply` for stock that may be transformed before sale.",
            )
        with c3:
            item_type = st.selectbox(
                "Item Type",
                ["precious_metal", "collectible", "antique", "general_merchandise", "other"],
                key="inv_intake_form_item_type",
            )
        metal_type = st.text_input("Metal Type (optional)", key="inv_intake_form_metal_type")

        s1, s2, s3 = st.columns(3)
        with s1:
            sku_seed_category = st.text_input("SKU Category Seed", key="inv_intake_form_sku_seed_category")
        with s2:
            sku_seed_type = st.text_input("SKU Type Seed", key="inv_intake_form_sku_seed_type")
        with s3:
            generated_sku = generate_sku(sku_seed_category, sku_seed_type)
            if not str(st.session_state.get("inv_intake_form_sku") or "").strip():
                st.session_state["inv_intake_form_sku"] = generated_sku
            sku = st.text_input("SKU", key="inv_intake_form_sku")

        title = st.text_input("Product Title", key="inv_intake_form_title")
        description = st.text_area("Product Description", key="inv_intake_form_description")
        q1, q2, q3 = st.columns(3)
        with q1:
            quantity = st.number_input("Quantity", min_value=0, step=1, key="inv_intake_form_quantity")
        with q2:
            acquisition_cost = st.number_input("Unit Acquisition Cost", min_value=0.0, step=0.01, key="inv_intake_form_acquisition_cost")
        with q3:
            weight_oz = st.number_input("Weight (oz)", min_value=0.0, step=0.01, key="inv_intake_form_weight_oz")
        acquisition_tax_paid = st.number_input("Unit Acquisition Tax Paid", min_value=0.0, step=0.01, key="inv_intake_form_acquisition_tax_paid")
        sh1, sh2 = st.columns(2)
        with sh1:
            acquisition_shipping_paid = st.number_input("Unit Acquisition Shipping Paid", min_value=0.0, step=0.01, key="inv_intake_form_acquisition_shipping_paid")
        with sh2:
            acquisition_handling_paid = st.number_input("Unit Acquisition Handling Paid", min_value=0.0, step=0.01, key="inv_intake_form_acquisition_handling_paid")
        product_cost = st.number_input("Product Cost", min_value=0.0, step=0.01, key="inv_intake_form_product_cost")
        ebay_purchase = st.checkbox("Purchased On eBay", key="inv_intake_form_ebay_purchase")
        st.caption("If enabled, `eBay Purchase Item ID` is required at submit.")
        ebay_purchase_item_id = st.text_input(
            "eBay Purchase Item ID",
            key="inv_intake_form_ebay_purchase_item_id",
            help="Required when Purchased On eBay is enabled.",
        )
        ebay_purchase_url = st.text_input(
            "eBay Purchase Link",
            key="inv_intake_form_ebay_purchase_url",
            help="Optional, but recommended for traceability.",
        )
        if not ebay_purchase:
            st.caption("`Purchased On eBay` is off; these fields are optional and only validated when enabled.")

        p1, p2, p3, p4 = st.columns(4)
        with p1:
            package_weight_oz = st.number_input("Pkg Weight (oz)", min_value=0.0, step=0.01, key="inv_intake_form_package_weight_oz")
        with p2:
            package_length_in = st.number_input("Length (in)", min_value=0.0, step=0.1, key="inv_intake_form_package_length_in")
        with p3:
            package_width_in = st.number_input("Width (in)", min_value=0.0, step=0.1, key="inv_intake_form_package_width_in")
        with p4:
            package_height_in = st.number_input("Height (in)", min_value=0.0, step=0.1, key="inv_intake_form_package_height_in")

        acquired_date = st.date_input("Acquired Date", key="inv_intake_form_acquired_date")

        st.markdown("### 2) Source + Lot")
        source_key = st.selectbox("Source (optional)", options=list(source_options.keys()), key="inv_intake_form_source_key")
        lot_key = st.selectbox("Existing Purchase Lot (optional)", options=list(lot_options.keys()), key="inv_intake_form_lot_key")
        create_new_lot = st.checkbox("Create New Purchase Lot Inline", key="inv_intake_form_create_new_lot")

        new_lot_code = ""
        new_lot_vendor = ""
        new_lot_total_cost = 0.0
        new_lot_total_tax_paid = 0.0
        new_lot_total_shipping_paid = 0.0
        new_lot_total_handling_paid = 0.0
        new_lot_expected_total_quantity = 0
        new_lot_notes = ""
        if create_new_lot:
            l1, l2 = st.columns(2)
            with l1:
                new_lot_code = st.text_input("New Lot Code", key="inv_intake_form_new_lot_code")
            with l2:
                new_lot_vendor = st.text_input("New Lot Vendor", key="inv_intake_form_new_lot_vendor")
            new_lot_total_cost = st.number_input(
                "New Lot Item Subtotal (before tax/shipping/fees)",
                min_value=0.0,
                step=0.01,
                key="inv_intake_form_new_lot_total_cost",
                help="Enter the item subtotal only. Do not enter the order total here when tax/shipping/fees are entered below.",
            )
            new_lot_total_tax_paid = st.number_input("New Lot Total Tax Paid", min_value=0.0, step=0.01, key="inv_intake_form_new_lot_total_tax_paid")
            new_lot_total_shipping_paid = st.number_input("New Lot Total Shipping Paid", min_value=0.0, step=0.01, key="inv_intake_form_new_lot_total_shipping_paid")
            new_lot_total_handling_paid = st.number_input("New Lot Total Handling Paid", min_value=0.0, step=0.01, key="inv_intake_form_new_lot_total_handling_paid")
            new_lot_expected_total_quantity = st.number_input(
                "New Lot Expected Total Quantity",
                min_value=0,
                step=1,
                key="inv_intake_form_new_lot_expected_total_quantity",
                help="Optional. Use when this lot's cost covers products that have not all been checked in yet.",
            )
            new_lot_notes = st.text_area("New Lot Notes", key="inv_intake_form_new_lot_notes")

        st.markdown("### 3) Incoming Purchase Invoice / Document (Optional)")
        d1, d2 = st.columns(2)
        with d1:
            purchase_doc_kind = st.selectbox(
                "Document Kind",
                options=["incoming_invoice", "purchase_order", "receipt", "other"],
                key="inv_intake_form_purchase_doc_kind",
            )
            purchase_doc_title = st.text_input(
                "Document Title (Optional)",
                key="inv_intake_form_purchase_doc_title",
                placeholder="Example: APMEX Invoice #12345",
            )
        with d2:
            purchase_doc_run_ai_extract = st.checkbox(
                "Run AI extraction",
                key="inv_intake_form_purchase_doc_run_ai_extract",
                help="Extract structured fields from the uploaded purchase document.",
            )
            purchase_doc_extraction_mode_options = ["llm"]
            if textract_enabled:
                purchase_doc_extraction_mode_options.extend(["textract", "both"])
            existing_mode = str(st.session_state.get("inv_intake_form_purchase_doc_extraction_mode") or "").strip().lower()
            if existing_mode not in purchase_doc_extraction_mode_options:
                st.session_state["inv_intake_form_purchase_doc_extraction_mode"] = (
                    "both" if textract_enabled else "llm"
                )
            purchase_doc_extraction_mode = st.selectbox(
                "Extraction Mode",
                options=purchase_doc_extraction_mode_options,
                format_func=lambda mode: {
                    "llm": "LLM Multimodal",
                    "textract": "AWS Textract",
                    "both": "Both (merge)",
                }.get(str(mode), str(mode)),
                key="inv_intake_form_purchase_doc_extraction_mode",
                disabled=not purchase_doc_run_ai_extract,
            )
        purchase_doc_file = st.file_uploader(
            "Upload Purchase Document (PDF/Image)",
            type=["pdf", "png", "jpg", "jpeg", "webp"],
            key="inv_intake_form_purchase_doc_file",
            help="Linked automatically to the created product and selected lot/source.",
        )
        with st.expander("Camera (Purchase Document)", expanded=False):
            purchase_doc_camera = st.camera_input(
                "Or Take Picture",
                key="inv_intake_form_purchase_doc_camera",
            )

        st.markdown("### 4) Optional AI Assist")
        apply_last_comp_summary = st.checkbox("Apply latest AI Comp summary to AI Comp", key="inv_intake_form_apply_last_comp_summary")
        apply_last_coin_identifier = st.checkbox("Apply latest Coin Identifier result to AI Description", key="inv_intake_form_apply_last_coin_identifier")
        apply_last_coin_grader = st.checkbox("Apply latest Coin Grader result", key="inv_intake_form_apply_last_coin_grader")
        ai_graded = st.checkbox("AI_GRADED", key="inv_intake_form_ai_graded")
        ai_grading_description = st.text_area("AI Grading Description", key="inv_intake_form_ai_grading_description")
        ai_description = st.text_area("AI Description", key="inv_intake_form_ai_description")
        ai_comp = st.text_area("AI Comp", key="inv_intake_form_ai_comp")

        st.markdown("### 5) Optional Draft Listing Handoff")
        create_draft_listing = st.checkbox("Create Draft Listing after product creation", key="inv_intake_form_create_draft_listing")
        l4, l5, l6 = st.columns(3)
        with l4:
            draft_marketplace = st.selectbox("Draft Marketplace", ["ebay", "facebook_marketplace", "whatnot", "craigslist", "shopify", "local"], key="inv_intake_form_draft_marketplace")
        with l5:
            draft_markup_pct = st.number_input("Draft Markup %", min_value=0.0, step=1.0, key="inv_intake_form_draft_markup_pct")
        with l6:
            draft_qty = st.number_input("Draft Listing Qty", min_value=1, step=1, key="inv_intake_form_draft_qty")
        attach_uploaded_media_to_listing = st.checkbox("Attach uploaded media to draft listing", key="inv_intake_form_attach_uploaded_media_to_listing")

        submit = st.form_submit_button("Run Inventory Intake Wizard")

    autosave_payload = _inventory_intake_build_draft_payload()
    autosave_signature = _inventory_intake_draft_signature(autosave_payload)
    previous_signature = str(st.session_state.get("inventory_intake_last_autosave_signature") or "").strip()
    if autosave_signature != previous_signature:
        row = repo.save_workflow_draft(
            environment=settings.app_env,
            workflow_key=INVENTORY_INTAKE_WORKFLOW_KEY,
            username=user.username,
            scope_key=INVENTORY_INTAKE_WORKFLOW_SCOPE,
            draft_payload=autosave_payload,
            schema_version="v1",
            status="active",
            last_step="intake_autosave",
            actor=user.username,
        )
        st.session_state["inventory_intake_last_autosave_signature"] = autosave_signature
        st.session_state["inventory_intake_last_draft_id"] = int(getattr(row, "id", 0) or 0)

    if not submit:
        return
    if not ensure_permission(user, "create", "Run Inventory Intake Wizard"):
        st.stop()
    if not sku.strip():
        st.error("SKU is required.")
        st.stop()
    if not title.strip():
        st.error("Product title is required.")
        st.stop()
    if quantity < 0:
        st.error("Quantity cannot be negative.")
        st.stop()
    if ebay_purchase and not ebay_purchase_item_id.strip():
        st.error("eBay Purchase Item ID is required when Purchased On eBay is enabled.")
        st.stop()

    if apply_last_comp_summary:
        ai_comp = normalize_ai_text(
            str(st.session_state.get("comp_last_ai_summary") or "").strip()
        ) or ai_comp
    if apply_last_coin_identifier:
        ai_description = normalize_ai_text(
            str(st.session_state.get("coin_identifier_last_result") or "").strip()
        ) or ai_description
    if apply_last_coin_grader:
        ai_grading_description = normalize_ai_text(
            str(st.session_state.get("coin_grader_last_result") or "").strip()
        ) or ai_grading_description
        if ai_grading_description:
            ai_graded = True

    selected_source_id = source_options.get(source_key)
    selected_lot_id = lot_options.get(lot_key)

    try:
        if create_new_lot:
            if not new_lot_code.strip():
                st.error("New lot code is required when creating a lot inline.")
                st.stop()
            created_lot = repo.create_purchase_lot(
                lot_code=new_lot_code.strip(),
                vendor=(new_lot_vendor.strip() or "unknown"),
                purchase_date=datetime.combine(acquired_date, datetime.min.time()),
                total_cost=to_decimal_or_none(new_lot_total_cost),
                total_tax_paid=to_decimal_or_none(new_lot_total_tax_paid),
                total_shipping_paid=to_decimal_or_none(new_lot_total_shipping_paid),
                total_handling_paid=to_decimal_or_none(new_lot_total_handling_paid),
                expected_total_quantity=int(new_lot_expected_total_quantity or 0) or None,
                notes=new_lot_notes.strip(),
                source_id=selected_source_id,
                ebay_purchase=bool(ebay_purchase),
                ebay_purchase_item_id=ebay_purchase_item_id.strip(),
                ebay_purchase_url=ebay_purchase_url.strip(),
            )
            selected_lot_id = int(created_lot.id)

        created_product = repo.create_product(
            sku=sku.strip(),
            title=title.strip(),
            category=category.strip(),
            inventory_class=inventory_class.strip(),
            description=description.strip(),
            metal_type=metal_type.strip(),
            weight_oz=to_decimal_or_none(weight_oz),
            acquisition_cost=to_decimal_or_none(acquisition_cost),
            acquisition_tax_paid=to_decimal_or_none(acquisition_tax_paid),
            acquisition_shipping_paid=to_decimal_or_none(acquisition_shipping_paid),
            acquisition_handling_paid=to_decimal_or_none(acquisition_handling_paid),
            current_quantity=int(quantity),
            product_cost=to_decimal_or_none(product_cost),
            ebay_purchase=bool(ebay_purchase),
            ebay_purchase_item_id=ebay_purchase_item_id.strip(),
            ebay_purchase_url=ebay_purchase_url.strip(),
            package_weight_oz=to_decimal_or_none(package_weight_oz),
            package_length_in=to_decimal_or_none(package_length_in),
            package_width_in=to_decimal_or_none(package_width_in),
            package_height_in=to_decimal_or_none(package_height_in),
            acquired_at=datetime.combine(acquired_date, datetime.min.time()),
            lot_id=selected_lot_id,
            actor=user.username,
        )
        repo.update_product(
            int(created_product.id),
            {
                "ai_graded": bool(ai_graded),
                "ai_grading_description": normalize_ai_text(ai_grading_description.strip()),
                "ai_description": normalize_ai_text(ai_description.strip()),
                "ai_comp": normalize_ai_text(ai_comp.strip()),
            },
            actor=user.username,
        )
        repo.record_audit_event(
            entity_type="product",
            entity_id=int(created_product.id),
            action="intake_wizard_handoff",
            actor=user.username,
            changes={
                "wizard": {
                    "category": category.strip(),
                    "item_type": item_type.strip(),
                    "source_id": selected_source_id,
                    "lot_id": selected_lot_id,
                    "create_draft_listing": bool(create_draft_listing),
                }
            },
        )

        uploaded_count = 0
        uploaded_errors: list[str] = []
        media_to_upload = list(intake_media or [])
        if include_starter_image_upload and st.session_state.get("inv_intake_starter_image_bytes"):
            media_to_upload.append(
                _BufferedUploadFile(
                    name=str(st.session_state.get("inv_intake_starter_image_name") or "starter_image.jpg"),
                    content_type=str(st.session_state.get("inv_intake_starter_image_type") or "image/jpeg"),
                    data=bytes(st.session_state.get("inv_intake_starter_image_bytes") or b""),
                )
            )
        if include_ai_images_on_submit:
            for row in list(st.session_state.get("inv_intake_ai_buffered_media") or []):
                media_to_upload.append(
                    _BufferedUploadFile(
                        name=str(row.get("name") or "inventory_ai_image.jpg"),
                        content_type=str(row.get("content_type") or "image/jpeg"),
                        data=bytes(row.get("data") or b""),
                    )
                )
        if media_to_upload:
            if not storage.enabled:
                st.warning("Product created but media upload skipped (S3 storage not configured).")
            else:
                uploaded_count, uploaded_errors = upload_media_for_listing(
                    repo=repo,
                    storage=storage,
                    listing_id=None,
                    product_id=int(created_product.id),
                    uploaded_files=media_to_upload,
                    uploaded_by=(uploaded_by or user.username).strip() or user.username,
                )
        media_map: dict[int, object] = {}
        if existing_product_media_ids or existing_listing_media_ids:
            selected_media_ids = [
                int(v)
                for v in (list(existing_product_media_ids or []) + list(existing_listing_media_ids or []))
                if int(v) > 0
            ]
            media_map = {
                int(row.id): row
                for row in repo.list_media_assets_by_ids(selected_media_ids, include_archived=True)
            }
        if existing_product_media_ids:
            attach_candidate_ids: list[int] = []
            attach_errors: list[str] = []
            for media_id in existing_product_media_ids:
                row = media_map.get(int(media_id))
                if row is None:
                    attach_errors.append(f"#{int(media_id)} not found.")
                    continue
                if row.product_id not in {None, int(created_product.id)}:
                    attach_errors.append(
                        f"#{int(media_id)} already linked to product #{int(row.product_id)}."
                    )
                    continue
                attach_candidate_ids.append(int(media_id))
            attached_count = 0
            if attach_candidate_ids:
                try:
                    result = repo.bulk_update_media_assets(
                        attach_candidate_ids,
                        {"product_id": int(created_product.id)},
                        actor=user.username,
                    )
                    attached_count = len(result.get("updated_ids") or [])
                    for missing_id in result.get("missing_ids") or []:
                        attach_errors.append(f"#{int(missing_id)} not found.")
                except Exception as exc:
                    attach_errors.append(str(exc))
            if attached_count:
                st.success(f"Attached {attached_count} existing media item(s) to product.")
            for msg in attach_errors:
                st.warning(f"Existing media attach skipped: {msg}")

        created_listing_id: int | None = None
        created_purchase_document_id: int | None = None
        created_purchase_document_preview: dict | None = None
        auto_applied_purchase_lot_id: int | None = None
        auto_applied_purchase_lot_field_count = 0
        selected_purchase_doc = purchase_doc_camera if purchase_doc_camera is not None else purchase_doc_file
        if selected_purchase_doc is not None:
            try:
                file_name = str(getattr(selected_purchase_doc, "name", "") or "").strip() or "purchase_document.bin"
                file_bytes = bytes(selected_purchase_doc.getvalue() or b"")
                if not file_bytes:
                    raise ValueError("Uploaded purchase document is empty.")
                content_type = (
                    str(getattr(selected_purchase_doc, "type", "") or "").strip()
                    or "application/octet-stream"
                )
                if not storage.enabled:
                    st.warning(
                        "Product created, but purchase document upload was skipped "
                        "(S3 storage is not configured)."
                    )
                else:
                    upload_result = storage.upload_file(
                        file_name=file_name,
                        file_bytes=file_bytes,
                        content_type=content_type,
                    )
                    sha256 = hashlib.sha256(file_bytes).hexdigest()
                    ai_payload: dict = {}
                    ai_summary = ""
                    llm_summary = ""
                    textract_summary = ""
                    textract_error = ""
                    if purchase_doc_run_ai_extract:
                        if purchase_doc_extraction_mode in {"llm", "both"}:
                            default_sys = get_runtime_str(
                                repo,
                                "purchase_doc_ai_system_message",
                                "You extract structured data from purchase invoices and receipts. "
                                "Return strict JSON only.",
                            )
                            default_instruction = get_runtime_str(
                                repo,
                                "purchase_doc_ai_instruction",
                                "Extract fields as JSON: vendor_name, invoice_number, invoice_date, due_date, "
                                "line_items[{description, quantity, unit_price, line_total}], subtotal, tax, "
                                "shipping, handling, total, currency, payment_method, account_reference, notes, confidence.",
                            )
                            instruction = default_instruction
                            image_bytes = None
                            image_content_type = "image/jpeg"
                            if content_type.startswith("image/"):
                                image_bytes = file_bytes
                                image_content_type = content_type
                            elif content_type == "application/pdf":
                                pdf_text = _extract_pdf_text(file_bytes)
                                instruction = (
                                    default_instruction
                                    + "\n\nThis upload is a PDF purchase document. Extract from this OCR/text content:\n\n"
                                    + (pdf_text[:12000] if pdf_text else "[no extractable text found]")
                                    + "\n\nIf text is limited, return best-effort partial JSON and indicate low confidence."
                                )
                            else:
                                instruction = (
                                    default_instruction
                                    + "\n\nAnalyze the provided file context and return best-effort JSON."
                                )
                            ai_result = execute_multimodal_task(
                                repo,
                                tool_name="purchase_invoice_extractor",
                                system_message=default_sys,
                                instruction=instruction,
                                image_bytes=image_bytes,
                                image_content_type=image_content_type,
                                additional_images=None,
                                workflow="intake",
                                context={
                                    "doc_kind": purchase_doc_kind,
                                    "file_name": file_name,
                                    "content_type": content_type,
                                },
                            )
                            llm_summary = str(ai_result.text or "").strip()
                            ai_payload = _try_extract_json_object(llm_summary)
                        if purchase_doc_extraction_mode in {"textract", "both"}:
                            textract_payload, textract_summary, textract_error = extract_with_textract_best_effort(
                                file_bytes=file_bytes,
                                content_type=content_type,
                            )
                            if purchase_doc_extraction_mode == "textract":
                                ai_payload = textract_payload
                            elif not textract_error:
                                ai_payload = merge_llm_and_textract(ai_payload, textract_payload)
                        if purchase_doc_extraction_mode == "llm":
                            ai_summary = llm_summary
                        elif purchase_doc_extraction_mode == "textract":
                            ai_summary = textract_summary
                        else:
                            ai_summary = json.dumps(
                                {
                                    "provider": "llm+aws_textract",
                                    "llm_summary": llm_summary,
                                    "textract_summary": textract_summary,
                                    "merged_payload": ai_payload,
                                },
                                indent=2,
                            )
                    created_purchase_document = repo.create_purchase_document(
                        document_kind=purchase_doc_kind,
                        title=(purchase_doc_title or "").strip() or file_name,
                        original_filename=file_name,
                        content_type=content_type,
                        size_bytes=len(file_bytes),
                        content_sha256=sha256,
                        s3_bucket=upload_result.bucket,
                        s3_key=upload_result.key,
                        s3_url=upload_result.url,
                        lot_id=selected_lot_id,
                        product_id=int(created_product.id),
                        source_id=selected_source_id,
                        ai_extracted_json=json.dumps(ai_payload) if ai_payload else "{}",
                        ai_summary=ai_summary,
                        uploaded_by=(uploaded_by or user.username).strip() or user.username,
                        actor=user.username,
                    )
                    created_purchase_document_id = int(created_purchase_document.id)
                    created_purchase_document_preview = {
                        "id": created_purchase_document_id,
                        "kind": str(purchase_doc_kind or "").strip(),
                        "title": (purchase_doc_title or "").strip() or file_name,
                        "file_name": file_name,
                        "content_type": content_type,
                        "size_bytes": int(len(file_bytes)),
                        "lot_id": int(selected_lot_id) if selected_lot_id is not None else None,
                        "product_id": int(created_product.id),
                        "source_id": int(selected_source_id) if selected_source_id is not None else None,
                        "extraction_mode": str(purchase_doc_extraction_mode or "").strip()
                        if bool(purchase_doc_run_ai_extract)
                        else "disabled",
                        "ai_payload": ai_payload if isinstance(ai_payload, dict) else {},
                        "ai_summary": str(ai_summary or "").strip(),
                        "textract_error": textract_error,
                    }
                    if textract_error:
                        st.warning(
                            "Purchase document was stored, but Textract extraction was skipped/failed: "
                            f"{textract_error}"
                        )
                    auto_apply_enabled = bool(
                        get_runtime_bool(repo, "purchase_doc_auto_apply_linked_lot_fields", False)
                    )
                    if auto_apply_enabled and selected_lot_id is not None and isinstance(ai_payload, dict):
                        auto_updates, _ = _build_purchase_lot_updates_from_payload(ai_payload)
                        if auto_updates:
                            repo.update_purchase_lot(
                                int(selected_lot_id),
                                auto_updates,
                                actor=user.username,
                            )
                            repo.record_audit_event(
                                entity_type="purchase_document",
                                entity_id=int(created_purchase_document.id),
                                action="auto_apply_extracted_fields_to_lot",
                                actor=user.username,
                                changes={
                                    "workflow": "inventory_intake_wizard",
                                    "mode": "auto",
                                    "lot_id": int(selected_lot_id),
                                    "applied_fields": sorted(auto_updates.keys()),
                                },
                            )
                            auto_applied_purchase_lot_id = int(selected_lot_id)
                            auto_applied_purchase_lot_field_count = len(auto_updates)
            except Exception as exc:
                st.error(f"Unable to store purchase document: {exc}")

        if create_draft_listing:
            draft_price = float(acquisition_cost) * (1.0 + float(draft_markup_pct) / 100.0)
            if draft_price <= 0:
                draft_price = 0.01
            created_listing = repo.create_listing(
                product_id=int(created_product.id),
                marketplace=draft_marketplace.strip(),
                listing_title=title.strip(),
                listing_price=Decimal(str(round(draft_price, 2))),
                quantity_listed=max(1, int(draft_qty)),
                marketplace_details=f"Intake wizard generated. source_id={selected_source_id or ''} lot_id={selected_lot_id or ''}",
                listing_status="draft",
                listed_at=utcnow_naive(),
                actor=user.username,
            )
            created_listing_id = int(created_listing.id)
            repo.record_audit_event(
                entity_type="listing",
                entity_id=created_listing_id,
                action="intake_wizard_created_draft",
                actor=user.username,
                changes={
                    "wizard": {
                        "from_product_id": int(created_product.id),
                        "marketplace": draft_marketplace.strip(),
                        "markup_pct": float(draft_markup_pct),
                        "draft_qty": int(draft_qty),
                    }
                },
            )
            if attach_uploaded_media_to_listing:
                unlinked_ids = repo.list_unlinked_product_media_ids(int(created_product.id))
                if unlinked_ids:
                    repo.bulk_update_media_assets(
                        unlinked_ids,
                        {"listing_id": int(created_listing.id)},
                        actor=user.username,
                    )
            if listing_media:
                if not storage.enabled:
                    st.warning("Draft listing created, but listing media upload skipped (S3 not configured).")
                else:
                    listing_uploaded, listing_upload_errors = upload_media_for_listing(
                        repo=repo,
                        storage=storage,
                        listing_id=int(created_listing.id),
                        product_id=int(created_product.id),
                        uploaded_files=listing_media,
                        uploaded_by=(uploaded_by or user.username).strip() or user.username,
                    )
                    if listing_uploaded:
                        st.success(f"Uploaded {listing_uploaded} listing media file(s).")
                    for err in listing_upload_errors:
                        st.error(f"Listing media upload failed: {err}")
            if existing_listing_media_ids:
                listing_attach_candidate_ids: list[int] = []
                attached_listing_errors: list[str] = []
                for media_id in existing_listing_media_ids:
                    row = media_map.get(int(media_id))
                    if row is None:
                        attached_listing_errors.append(f"#{int(media_id)} not found.")
                        continue
                    if row.listing_id not in {None, int(created_listing.id)}:
                        attached_listing_errors.append(
                            f"#{int(media_id)} already linked to listing #{int(row.listing_id)}."
                        )
                        continue
                    if row.product_id not in {None, int(created_product.id)}:
                        attached_listing_errors.append(
                            f"#{int(media_id)} linked to product #{int(row.product_id)} (cannot reassign)."
                        )
                        continue
                    listing_attach_candidate_ids.append(int(media_id))
                attached_listing_count = 0
                if listing_attach_candidate_ids:
                    try:
                        result = repo.bulk_update_media_assets(
                            listing_attach_candidate_ids,
                            {"product_id": int(created_product.id), "listing_id": int(created_listing.id)},
                            actor=user.username,
                        )
                        attached_listing_count = len(result.get("updated_ids") or [])
                        for missing_id in result.get("missing_ids") or []:
                            attached_listing_errors.append(f"#{int(missing_id)} not found.")
                    except Exception as exc:
                        attached_listing_errors.append(str(exc))
                if attached_listing_count:
                    st.success(f"Attached {attached_listing_count} existing media item(s) to draft listing.")
                for msg in attached_listing_errors:
                    st.warning(f"Existing listing-media attach skipped: {msg}")
        elif existing_listing_media_ids:
            st.info("Draft listing was not created, so existing listing-media attachments were skipped.")

        success_msg = f"Created product #{created_product.id}"
        if selected_lot_id:
            success_msg += f" assigned to lot #{selected_lot_id}"
        if created_purchase_document_id is not None:
            success_msg += f" with purchase document #{created_purchase_document_id}"
        if created_listing_id is not None:
            success_msg += f" and draft listing #{created_listing_id}"
        success_msg += "."
        st.success(success_msg)
        if auto_applied_purchase_lot_id is not None and auto_applied_purchase_lot_field_count > 0:
            st.success(
                "Auto-applied extracted invoice fields to "
                f"lot #{int(auto_applied_purchase_lot_id)} "
                f"({int(auto_applied_purchase_lot_field_count)} field(s))."
            )
        if uploaded_count:
            st.success(f"Uploaded {uploaded_count} media file(s) to product.")
        for err in uploaded_errors:
            st.error(f"Media upload failed: {err}")
        if created_purchase_document_preview is not None:
            st.markdown("### Last Purchase Document Extract")
            pd1, pd2, pd3 = st.columns(3)
            pd1.metric("Document ID", int(created_purchase_document_preview.get("id") or 0))
            pd2.metric("Kind", str(created_purchase_document_preview.get("kind") or ""))
            pd3.metric("Extraction", str(created_purchase_document_preview.get("extraction_mode") or ""))
            st.caption(
                f"File: `{str(created_purchase_document_preview.get('file_name') or '')}`"
                f" ({int(created_purchase_document_preview.get('size_bytes') or 0)} bytes)"
            )
            show_purchase_extract_details = st.checkbox(
                "Load Detailed Purchase Extract View (slower)",
                value=False,
                key="inv_intake_load_purchase_extract_details",
                help="Enable detailed AI summary/field table/JSON rendering for the last uploaded purchase document.",
            )
            summary_text = str(created_purchase_document_preview.get("ai_summary") or "").strip()
            parsed_payload = created_purchase_document_preview.get("ai_payload") or {}
            if summary_text and show_purchase_extract_details:
                with st.expander("AI Summary", expanded=False):
                    st.code(summary_text)
            if isinstance(parsed_payload, dict) and parsed_payload and show_purchase_extract_details:
                preview_fields: list[dict] = []
                for key in [
                    "vendor_name",
                    "invoice_number",
                    "invoice_date",
                    "due_date",
                    "subtotal",
                    "tax",
                    "shipping",
                    "handling",
                    "total",
                    "currency",
                    "payment_method",
                    "confidence",
                ]:
                    value = parsed_payload.get(key)
                    if value is None:
                        continue
                    if isinstance(value, str) and not value.strip():
                        continue
                    if isinstance(value, (list, dict)) and not value:
                        continue
                    if isinstance(value, (int, float)) and float(value) == 0.0:
                        continue
                    preview_fields.append({"field": key, "value": value})
                if preview_fields:
                    st.dataframe(preview_fields, use_container_width=True, hide_index=True)
                with st.expander("Full Extracted JSON", expanded=False):
                    st.json(parsed_payload)
            if isinstance(parsed_payload, dict) and parsed_payload:
                linked_lot_id_raw = created_purchase_document_preview.get("lot_id")
                linked_lot_id = int(linked_lot_id_raw) if linked_lot_id_raw is not None else None
                st.markdown("#### Apply Extracted Accounting Fields")
                if linked_lot_id is None:
                    st.caption(
                        "No purchase lot is linked to this document, so extracted totals are stored but not yet "
                        "applied to normalized lot accounting fields."
                    )
                else:
                    apply_updates, candidates = _build_purchase_lot_updates_from_payload(parsed_payload)
                    st.caption(
                        "Detected candidates: "
                        + ", ".join(
                            [
                                f"vendor={str(candidates.get('vendor') or 'n/a')}",
                                f"invoice_date={str(candidates.get('invoice_date') or 'n/a')}",
                                f"total={str(candidates.get('total') or 'n/a')}",
                                f"tax={str(candidates.get('tax') or 'n/a')}",
                                f"shipping={str(candidates.get('shipping') or 'n/a')}",
                                f"handling={str(candidates.get('handling') or 'n/a')}",
                            ]
                        )
                    )
                    if st.button(
                        "Apply Extracted Fields To Linked Lot",
                        key=f"inv_intake_apply_ai_to_lot_{int(created_purchase_document_preview.get('id') or 0)}",
                        disabled=not bool(apply_updates),
                    ):
                        try:
                            repo.update_purchase_lot(
                                int(linked_lot_id),
                                apply_updates,
                                actor=user.username,
                            )
                            doc_id = int(created_purchase_document_preview.get("id") or 0)
                            if doc_id > 0:
                                repo.record_audit_event(
                                    entity_type="purchase_document",
                                    entity_id=doc_id,
                                    action="manual_apply_extracted_fields_to_lot",
                                    actor=user.username,
                                    changes={
                                        "workflow": "inventory_intake_wizard",
                                        "mode": "manual",
                                        "lot_id": int(linked_lot_id),
                                        "applied_fields": sorted(apply_updates.keys()),
                                    },
                                )
                            st.success(
                                f"Applied extracted accounting fields to linked lot #{int(linked_lot_id)}."
                            )
                            st.rerun()
                        except Exception as exc:
                            repo.db.rollback()
                            st.error(f"Unable to apply extracted fields to lot: {exc}")

        st.markdown("### Next Actions")
        a1, a2, a3, a4 = st.columns(4)
        with a1:
            if st.button("Open Products", key="inv_intake_open_products", use_container_width=True):
                if hasattr(st, "switch_page"):
                    st.switch_page("pages/02_Products.py")
        with a2:
            if st.button("Open Listings", key="inv_intake_open_listings", use_container_width=True):
                if hasattr(st, "switch_page"):
                    st.switch_page("pages/03_Listings.py")
        with a3:
            if st.button("Open Search & Edit", key="inv_intake_open_search_edit", use_container_width=True):
                if hasattr(st, "switch_page"):
                    st.switch_page("pages/10_Search_Edit.py")
        with a4:
            if st.button("Run Another Intake", key="inv_intake_run_another", use_container_width=True):
                st.rerun()

        st.markdown("### Result Timeline")
        product_tab_label = f"Product #{int(created_product.id)}"
        if created_listing_id is not None:
            listing_tab_label = f"Listing #{int(created_listing_id)}"
            tab_product, tab_listing = st.tabs([product_tab_label, listing_tab_label])
            with tab_product:
                render_entity_timeline(
                    repo,
                    entity_type="product",
                    entity_id=int(created_product.id),
                    title=f"Product #{int(created_product.id)} Timeline",
                )
            with tab_listing:
                render_entity_timeline(
                    repo,
                    entity_type="listing",
                    entity_id=int(created_listing_id),
                    title=f"Listing #{int(created_listing_id)} Timeline",
                )
        else:
            render_entity_timeline(
                repo,
                entity_type="product",
                entity_id=int(created_product.id),
                title=f"Product #{int(created_product.id)} Timeline",
            )
    except Exception as exc:
        repo.db.rollback()
        st.error(f"Inventory intake wizard failed: {exc}")
