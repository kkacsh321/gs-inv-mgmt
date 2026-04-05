from datetime import datetime
from decimal import Decimal
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
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary, execute_multimodal_task
from app.services.ebay import EbayClient
from app.services.ai_text import (
    coin_grader_structured_to_text,
    normalize_ai_text,
    parse_coin_grader_structured,
)
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_str
from app.utils.time import utc_today, utcnow_naive


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


def render_inventory_intake_wizard(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
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
            st.image(stored_starter_bytes, caption="Buffered starter image", use_container_width=True)
        if st.button("Clear Buffered Starter Image", key="inv_intake_clear_starter_buffer"):
            st.session_state.pop("inv_intake_starter_image_bytes", None)
            st.session_state.pop("inv_intake_starter_image_name", None)
            st.session_state.pop("inv_intake_starter_image_type", None)
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
                if title_val:
                    st.session_state["inv_intake_default_title"] = title_val
                if category_val in {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}:
                    st.session_state["inv_intake_default_category"] = category_val
                if item_type_val in {"precious_metal", "collectible", "antique", "general_merchandise", "other"}:
                    st.session_state["inv_intake_default_item_type"] = item_type_val
                if metal_type_val:
                    st.session_state["inv_intake_default_metal_type"] = metal_type_val
                if description_val:
                    st.session_state["inv_intake_default_description"] = description_val
                if ai_desc_val:
                    st.session_state["inv_intake_default_ai_description"] = ai_desc_val
                if comp_query_val:
                    st.session_state["inv_intake_ai_seed_prompt"] = comp_query_val
                st.success("Image analysis applied to intake defaults.")
            st.session_state["inv_intake_image_start_raw"] = str(result.text or "").strip()
            st.rerun()
        except Exception as exc:
            st.error(f"Starter image analysis failed: {exc}")

    raw_image_start = str(st.session_state.get("inv_intake_image_start_raw") or "").strip()
    if raw_image_start:
        with st.expander("Last Starter Image Analysis", expanded=False):
            st.code(raw_image_start, language="json")

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
                if title_val:
                    st.session_state["inv_intake_default_title"] = title_val
                if category_val in {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}:
                    st.session_state["inv_intake_default_category"] = category_val
                if description_val:
                    st.session_state["inv_intake_default_description"] = description_val
                if ai_desc_val:
                    st.session_state["inv_intake_default_ai_description"] = ai_desc_val
                st.session_state["inv_intake_ai_suggestion_raw"] = result.text
                st.success("AI suggestions generated and applied to wizard defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"AI suggestion generation failed: {exc}")

    raw_ai = str(st.session_state.get("inv_intake_ai_suggestion_raw") or "").strip()
    if raw_ai:
        with st.expander("Last AI Suggestion Payload", expanded=False):
            st.code(raw_ai, language="json")

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
    include_ai_images_on_submit = st.checkbox(
        "Include AI assist images in intake media on submit",
        value=True,
        key="inv_intake_include_ai_images_on_submit",
    )
    buffered_ai_images = st.session_state.get("inv_intake_ai_buffered_media") or []
    if buffered_ai_images:
        st.caption(f"Buffered AI assist images: {len(buffered_ai_images)}")
        if st.button("Clear Buffered AI Assist Images", key="inv_intake_clear_ai_buffered"):
            st.session_state.pop("inv_intake_ai_buffered_media", None)
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
                    context={"source": "inventory_intake_wizard"},
                )
                payload = _try_extract_json_object(result.text)
                normalized = normalize_ai_text(result.text)
                st.session_state["coin_identifier_last_result"] = normalized
                st.session_state["inv_intake_default_ai_description"] = normalized
                if isinstance(payload, dict):
                    title_val = str(payload.get("coin_name") or "").strip()
                    metal_val = str(payload.get("metal") or "").strip()
                    notes_val = str(payload.get("notes") or "").strip()
                    if title_val:
                        st.session_state["inv_intake_default_title"] = title_val
                    if metal_val:
                        st.session_state["inv_intake_default_metal_type"] = metal_val
                    if notes_val:
                        st.session_state["inv_intake_default_description"] = notes_val
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
                    context={"source": "inventory_intake_wizard"},
                )
                structured_grade = parse_coin_grader_structured(result.text)
                normalized_grade = (
                    coin_grader_structured_to_text(structured_grade)
                    if structured_grade
                    else normalize_ai_text(result.text)
                )
                st.session_state["coin_grader_last_result"] = normalized_grade
                st.session_state["coin_grader_last_structured"] = structured_grade
                st.success("Grader completed and applied to last grader output.")
            except Exception as exc:
                st.error(f"Grader failed: {exc}")

    last_structured_grade = st.session_state.get("coin_grader_last_structured") or {}
    if last_structured_grade:
        with st.expander("Last Grader Structured Result", expanded=False):
            st.json(last_structured_grade)
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
                client = EbayClient()
                if client.is_configured():
                    try:
                        ebay_rows = client.find_completed_items(
                            keywords=query,
                            sold_only=True,
                            entries_per_page=25,
                            page_number=1,
                        )
                    except Exception:
                        ebay_rows = client.find_completed_items(
                            keywords=query,
                            sold_only=False,
                            entries_per_page=25,
                            page_number=1,
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
                )
                normalized_comp = normalize_ai_text(comp_result.text)
                st.session_state["comp_last_ai_summary"] = normalized_comp
                st.session_state["inv_intake_default_ai_comp"] = normalized_comp
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
    existing_product_media_ids = render_existing_media_attach_selector(
        repo=repo,
        key_prefix="inventory_intake_existing_product_media",
        section_title="Attach Existing Media To New Product (Optional)",
        help_text="Bulk-link already uploaded media assets to the product created by this wizard.",
    )
    existing_listing_media_ids = render_existing_media_attach_selector(
        repo=repo,
        key_prefix="inventory_intake_existing_listing_media",
        section_title="Attach Existing Media To New Draft Listing (Optional)",
        help_text="Bulk-link existing media assets to the draft listing created by this wizard.",
    )

    with st.form("inventory_intake_wizard_form", clear_on_submit=False):
        st.markdown("### 1) Item + Inventory")
        c1, c2, c3 = st.columns(3)
        with c1:
            category = st.selectbox(
                "Category",
                ["bullion", "collectibles", "antiques", "coins", "normal_goods", "other"],
                index=["bullion", "collectibles", "antiques", "coins", "normal_goods", "other"].index(ai_default_category),
            )
        with c2:
            inventory_class = st.selectbox(
                "Inventory Class",
                ["sellable", "raw_material", "supply"],
                index=0,
                help="Choose `raw_material` or `supply` for stock that may be transformed before sale.",
            )
        with c3:
            item_type = st.selectbox(
                "Item Type",
                ["precious_metal", "collectible", "antique", "general_merchandise", "other"],
                index=["precious_metal", "collectible", "antique", "general_merchandise", "other"].index(
                    ai_default_item_type
                ),
            )
        metal_type = st.text_input("Metal Type (optional)", value=ai_default_metal_type)

        s1, s2, s3 = st.columns(3)
        with s1:
            sku_seed_category = st.text_input("SKU Category Seed", value=category)
        with s2:
            sku_seed_type = st.text_input("SKU Type Seed", value=item_type)
        with s3:
            sku = st.text_input("SKU", value=generate_sku(sku_seed_category, sku_seed_type))

        title = st.text_input("Product Title", value=ai_default_title)
        description = st.text_area("Product Description", value=ai_default_description)
        q1, q2, q3 = st.columns(3)
        with q1:
            quantity = st.number_input("Quantity", min_value=0, value=1, step=1)
        with q2:
            acquisition_cost = st.number_input("Unit Acquisition Cost", min_value=0.0, value=0.0, step=0.01)
        with q3:
            weight_oz = st.number_input("Weight (oz)", min_value=0.0, value=0.0, step=0.01)
        acquisition_tax_paid = st.number_input("Unit Acquisition Tax Paid", min_value=0.0, value=0.0, step=0.01)
        sh1, sh2 = st.columns(2)
        with sh1:
            acquisition_shipping_paid = st.number_input("Unit Acquisition Shipping Paid", min_value=0.0, value=0.0, step=0.01)
        with sh2:
            acquisition_handling_paid = st.number_input("Unit Acquisition Handling Paid", min_value=0.0, value=0.0, step=0.01)
        product_cost = st.number_input("Product Cost", min_value=0.0, value=0.0, step=0.01)
        ebay_purchase = st.checkbox("Purchased On eBay", value=False)
        ebay_purchase_item_id = st.text_input(
            "eBay Purchase Item ID",
            value="",
            disabled=not ebay_purchase,
        )
        ebay_purchase_url = st.text_input(
            "eBay Purchase Link",
            value="",
            disabled=not ebay_purchase,
        )

        p1, p2, p3, p4 = st.columns(4)
        with p1:
            package_weight_oz = st.number_input("Pkg Weight (oz)", min_value=0.0, value=0.0, step=0.01)
        with p2:
            package_length_in = st.number_input("Length (in)", min_value=0.0, value=0.0, step=0.1)
        with p3:
            package_width_in = st.number_input("Width (in)", min_value=0.0, value=0.0, step=0.1)
        with p4:
            package_height_in = st.number_input("Height (in)", min_value=0.0, value=0.0, step=0.1)

        acquired_date = st.date_input("Acquired Date", value=utc_today())

        st.markdown("### 2) Source + Lot")
        source_key = st.selectbox("Source (optional)", options=list(source_options.keys()))
        lot_key = st.selectbox("Existing Purchase Lot (optional)", options=list(lot_options.keys()))
        create_new_lot = st.checkbox("Create New Purchase Lot Inline", value=False)

        new_lot_code = ""
        new_lot_vendor = ""
        new_lot_total_cost = 0.0
        new_lot_total_tax_paid = 0.0
        new_lot_total_shipping_paid = 0.0
        new_lot_total_handling_paid = 0.0
        new_lot_notes = ""
        if create_new_lot:
            l1, l2 = st.columns(2)
            with l1:
                new_lot_code = st.text_input("New Lot Code")
            with l2:
                new_lot_vendor = st.text_input("New Lot Vendor")
            new_lot_total_cost = st.number_input("New Lot Total Cost", min_value=0.0, value=0.0, step=0.01)
            new_lot_total_tax_paid = st.number_input("New Lot Total Tax Paid", min_value=0.0, value=0.0, step=0.01)
            new_lot_total_shipping_paid = st.number_input("New Lot Total Shipping Paid", min_value=0.0, value=0.0, step=0.01)
            new_lot_total_handling_paid = st.number_input("New Lot Total Handling Paid", min_value=0.0, value=0.0, step=0.01)
            new_lot_notes = st.text_area("New Lot Notes", value="")

        st.markdown("### 4) Optional AI Assist")
        apply_last_comp_summary = st.checkbox("Apply latest AI Comp summary to AI Comp", value=True)
        apply_last_coin_identifier = st.checkbox("Apply latest Coin Identifier result to AI Description", value=False)
        apply_last_coin_grader = st.checkbox("Apply latest Coin Grader result", value=False)
        ai_graded = st.checkbox("AI_GRADED", value=False)
        ai_grading_description = st.text_area("AI Grading Description", value="")
        ai_description = st.text_area("AI Description", value=ai_default_ai_description)
        ai_comp = st.text_area("AI Comp", value=ai_default_ai_comp)

        st.markdown("### 5) Optional Draft Listing Handoff")
        create_draft_listing = st.checkbox("Create Draft Listing after product creation", value=True)
        l4, l5, l6 = st.columns(3)
        with l4:
            draft_marketplace = st.selectbox("Draft Marketplace", ["ebay", "facebook_marketplace", "whatnot", "craigslist", "shopify", "local"], index=0)
        with l5:
            draft_markup_pct = st.number_input("Draft Markup %", min_value=0.0, value=20.0, step=1.0)
        with l6:
            draft_qty = st.number_input("Draft Listing Qty", min_value=1, value=1, step=1)
        attach_uploaded_media_to_listing = st.checkbox("Attach uploaded media to draft listing", value=True)

        submit = st.form_submit_button("Run Inventory Intake Wizard")

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
        if existing_product_media_ids:
            media_map = {int(row.id): row for row in repo.list_media_assets()}
            attached_count = 0
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
                try:
                    repo.update_media_asset(
                        int(media_id),
                        {"product_id": int(created_product.id)},
                        actor=user.username,
                    )
                    attached_count += 1
                except Exception as exc:
                    attach_errors.append(f"#{int(media_id)}: {exc}")
            if attached_count:
                st.success(f"Attached {attached_count} existing media item(s) to product.")
            for msg in attach_errors:
                st.warning(f"Existing media attach skipped: {msg}")

        created_listing_id: int | None = None
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
                for media_row in repo.list_media_assets_for_product(int(created_product.id)):
                    if media_row.listing_id is not None:
                        continue
                    repo.update_media_asset(
                        int(media_row.id),
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
                media_map = {int(row.id): row for row in repo.list_media_assets()}
                attached_listing_count = 0
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
                    try:
                        repo.update_media_asset(
                            int(media_id),
                            {"product_id": int(created_product.id), "listing_id": int(created_listing.id)},
                            actor=user.username,
                        )
                        attached_listing_count += 1
                    except Exception as exc:
                        attached_listing_errors.append(f"#{int(media_id)}: {exc}")
                if attached_listing_count:
                    st.success(f"Attached {attached_listing_count} existing media item(s) to draft listing.")
                for msg in attached_listing_errors:
                    st.warning(f"Existing listing-media attach skipped: {msg}")
        elif existing_listing_media_ids:
            st.info("Draft listing was not created, so existing listing-media attachments were skipped.")

        success_msg = f"Created product #{created_product.id}"
        if selected_lot_id:
            success_msg += f" assigned to lot #{selected_lot_id}"
        if created_listing_id is not None:
            success_msg += f" and draft listing #{created_listing_id}"
        success_msg += "."
        st.success(success_msg)
        if uploaded_count:
            st.success(f"Uploaded {uploaded_count} media file(s) to product.")
        for err in uploaded_errors:
            st.error(f"Media upload failed: {err}")

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
