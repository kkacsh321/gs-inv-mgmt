from datetime import datetime
from decimal import Decimal
import json
from uuid import uuid4

import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import to_decimal_or_none
from app.components.views.shared import (
    generate_sku,
    render_existing_media_attach_selector,
    render_help_panel,
    render_media_capture_inputs,
    upload_media_for_listing,
)
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
        self.name = str(name or "ai_image.jpg")
        self.type = str(content_type or "application/octet-stream")
        self._data = bytes(data or b"")

    def read(self) -> bytes:
        return self._data


def _coin_ref_summary(ref) -> str:
    if ref is None:
        return ""
    year_start = getattr(ref, "year_start", None)
    year_end = getattr(ref, "year_end", None)
    years = ""
    if year_start and year_end:
        years = f"{int(year_start)}-{int(year_end)}"
    elif year_start:
        years = str(int(year_start))
    return " | ".join(
        [
            str(getattr(ref, "coin_name", "") or "").strip(),
            str(getattr(ref, "country", "") or "").strip(),
            str(getattr(ref, "denomination", "") or "").strip(),
            str(getattr(ref, "series", "") or "").strip(),
            years,
            str(getattr(ref, "metal_type", "") or "").strip(),
        ]
    ).strip(" |")


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


def _buffer_coin_ai_images(*, primary, secondary) -> None:
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
            label = "obverse" if idx == 1 else "reverse"
            name = f"coin_ai_{label}_{uuid4().hex[:8]}.{ext}"
        data = uploaded.getvalue()
        if not data:
            continue
        buffered.append({"name": name, "content_type": content_type, "data": data})
    st.session_state["coin_intake_ai_buffered_media"] = buffered


def render_coin_intake_wizard(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    st.subheader("Coin Intake Wizard")
    render_help_panel(
        section_title="Coin Intake Wizard",
        goal="Create inventory from coin reference + AI context, then optionally create an eBay draft in one flow.",
        steps=[
            "Select an existing coin reference record (optional but recommended).",
            "Apply optional AI outputs from recent Tool runs into product fields.",
            "Create the product and upload media once.",
            "Optionally generate a draft eBay listing immediately from the same intake.",
        ],
        roadmap_phase="v0.4 UX Consolidation + Central Operations Hub",
    )
    st.page_link("pages/06_Tools.py", label="Open AI Tools (Coin Grader / Identifier / Comp)")
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="coin_intake_wizard",
        section_title="Workspace Feedback",
    )

    coin_refs = repo.list_coin_references(active_only=True, limit=5000)
    coin_ref_options = {"None": None}
    coin_ref_options.update(
        {f"#{row.id} | {row.coin_name} | {row.country} | {row.series}": row for row in coin_refs}
    )
    lots = repo.list_purchase_lots()
    lot_options = {"None": None, **{f"{lot.lot_code} | {lot.vendor}": lot.id for lot in lots}}

    st.markdown("### 3) Optional Media Upload")
    uploaded_by = st.text_input("Uploaded By", value=user.username, key="coin_intake_uploaded_by")
    intake_media = render_media_capture_inputs(
        key_prefix="coin_intake_media",
        upload_label="Product Photos/Videos (optional)",
        allow_enhanced=True,
    )
    listing_media = render_media_capture_inputs(
        key_prefix="coin_intake_listing_media",
        upload_label="Draft Listing Photos/Videos (optional, multiple images + video supported)",
        allow_enhanced=True,
    )
    st.caption(
        "Tip: Product media and listing media support multiple photos plus a video upload. "
        "When draft listing handoff is enabled, selected media can be attached automatically."
    )
    existing_product_media_ids = render_existing_media_attach_selector(
        repo=repo,
        key_prefix="coin_intake_existing_product_media",
        section_title="Attach Existing Media To New Product (Optional)",
        help_text="Bulk-link already uploaded media assets to the product created by this wizard.",
    )
    existing_listing_media_ids = render_existing_media_attach_selector(
        repo=repo,
        key_prefix="coin_intake_existing_listing_media",
        section_title="Attach Existing Media To New Draft Listing (Optional)",
        help_text="Bulk-link existing media assets to the draft listing created by this wizard.",
    )

    st.markdown("### Wizard AI Assist (Run Directly Here)")
    ai_hint = st.text_input(
        "AI Hint (optional)",
        key="coin_intake_ai_hint",
        placeholder="e.g., 1921 Morgan silver dollar, worn condition",
    )
    ai_image_upload = st.file_uploader(
        "Upload Coin Image (AI Assist)",
        type=["jpg", "jpeg", "png", "webp"],
        key="coin_intake_ai_image_upload",
    )
    with st.expander("Camera (AI Assist Obverse)", expanded=False):
        ai_camera_image = st.camera_input(
            "Capture Coin Image (AI Assist)",
            key="coin_intake_ai_camera_image",
        )
    ai_reverse_upload = st.file_uploader(
        "Upload Reverse Image (optional)",
        type=["jpg", "jpeg", "png", "webp"],
        key="coin_intake_ai_reverse_upload",
    )
    with st.expander("Camera (AI Assist Reverse)", expanded=False):
        ai_reverse_camera = st.camera_input(
            "Capture Reverse Image (optional)",
            key="coin_intake_ai_reverse_camera",
        )
    ai_image = ai_camera_image or ai_image_upload
    ai_reverse = ai_reverse_camera or ai_reverse_upload
    r1, r2, r3 = st.columns(3)
    run_identifier = r1.button("Run Identifier", key="coin_intake_run_identifier")
    run_grader = r2.button("Run Grader", key="coin_intake_run_grader")
    run_comp = r3.button("Run Comp", key="coin_intake_run_comp")
    include_ai_images_on_submit = st.checkbox(
        "Include AI assist obverse/reverse images in intake media on submit",
        value=True,
        key="coin_intake_include_ai_images_on_submit",
    )
    buffered_ai_images = st.session_state.get("coin_intake_ai_buffered_media") or []
    if buffered_ai_images:
        st.caption(f"Buffered AI assist images: {len(buffered_ai_images)}")
        if st.button("Clear Buffered AI Assist Images", key="coin_intake_clear_ai_buffered"):
            st.session_state.pop("coin_intake_ai_buffered_media", None)
            st.success("Cleared buffered AI assist images.")
            st.rerun()

    if run_identifier:
        if not ensure_permission(user, "ai_coin_identify", "Run Coin Identifier (Wizard)"):
            st.stop()
        if ai_image is None and not str(ai_hint or "").strip():
            st.error("Provide an image or hint to run identifier.")
        else:
            try:
                _buffer_coin_ai_images(primary=ai_image, secondary=ai_reverse)
                system_message = get_runtime_str(
                    repo,
                    "coin_identifier_system_message",
                    "You are a careful numismatic identifier. Prefer precision and state uncertainty clearly.",
                ).strip()
                instruction = get_runtime_str(
                    repo,
                    "coin_identifier_instruction_template",
                    (
                        "Identify the coin from image and notes. "
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
                    tool_name="coin_identifier_wizard",
                    system_message=system_message,
                    instruction=f"{instruction}\nUser hint: {str(ai_hint or '').strip() or '(none)'}",
                    image_bytes=image_bytes if image_bytes else b"",
                    image_content_type=image_type,
                    additional_images=[(reverse_bytes, reverse_type)] if reverse_bytes else [],
                    context={"source": "coin_intake_wizard"},
                )
                payload = _try_extract_json_object(result.text)
                normalized = normalize_ai_text(result.text)
                st.session_state["coin_identifier_last_result"] = normalized
                st.session_state["coin_intake_prefill_ai_description"] = normalized
                if isinstance(payload, dict):
                    name = str(payload.get("coin_name") or "").strip()
                    metal = str(payload.get("metal") or "").strip()
                    notes = str(payload.get("notes") or "").strip()
                    if name:
                        st.session_state["coin_intake_prefill_title"] = name
                    if metal:
                        st.session_state["coin_intake_prefill_metal"] = metal
                    if notes:
                        st.session_state["coin_intake_prefill_description"] = notes
                st.success("Identifier completed and wizard prefill updated.")
            except Exception as exc:
                st.error(f"Identifier failed: {exc}")

    if run_grader:
        if not ensure_permission(user, "ai_coin_grade", "Run Coin Grader (Wizard)"):
            st.stop()
        if ai_image is None:
            st.error("Provide an image to run grader.")
        else:
            try:
                _buffer_coin_ai_images(primary=ai_image, secondary=ai_reverse)
                system_message = get_runtime_str(
                    repo,
                    "coin_grader_system_message",
                    "You are a conservative coin grading assistant.",
                ).strip()
                instruction = get_runtime_str(
                    repo,
                    "coin_grader_instruction_template",
                    "Estimate coin grade and return practical grading notes.",
                ).strip()
                result = execute_multimodal_task(
                    repo,
                    tool_name="coin_grader_wizard",
                    system_message=system_message,
                    instruction=f"{instruction}\nUser hint: {str(ai_hint or '').strip() or '(none)'}",
                    image_bytes=ai_image.getvalue(),
                    image_content_type=str(getattr(ai_image, "type", "") or "image/jpeg"),
                    additional_images=[(ai_reverse.getvalue(), str(getattr(ai_reverse, "type", "") or "image/jpeg"))]
                    if ai_reverse is not None
                    else [],
                    context={"source": "coin_intake_wizard"},
                )
                structured_grade = parse_coin_grader_structured(result.text)
                normalized_grade = (
                    coin_grader_structured_to_text(structured_grade)
                    if structured_grade
                    else normalize_ai_text(result.text)
                )
                st.session_state["coin_grader_last_result"] = normalized_grade
                st.session_state["coin_grader_last_structured"] = structured_grade
                st.session_state["coin_intake_prefill_ai_grading"] = normalized_grade
                st.success("Grader completed and wizard prefill updated.")
            except Exception as exc:
                st.error(f"Grader failed: {exc}")

    last_structured_grade = st.session_state.get("coin_grader_last_structured") or {}
    if last_structured_grade:
        with st.expander("Last Grader Structured Result", expanded=False):
            st.json(last_structured_grade)
            c1, c2, c3 = st.columns(3)
            with c1:
                st.caption(
                    f"Estimated Grade Range: `{str(last_structured_grade.get('estimated_grade_range') or '').strip() or 'n/a'}`"
                )
            with c2:
                st.caption(
                    f"Recommendation: `{str(last_structured_grade.get('submit_for_professional_grading') or '').strip() or 'n/a'}`"
                )
            with c3:
                st.caption(
                    f"Net Upside (USD): `{str(last_structured_grade.get('estimated_net_upside_usd') or '').strip() or 'n/a'}`"
                )

    if run_comp:
        if not ensure_permission(user, "ai_comp_use", "Run Comp (Wizard)"):
            st.stop()
        query = " ".join(
            [
                str(ai_hint or "").strip(),
                str(st.session_state.get("coin_intake_prefill_title") or "").strip(),
                str(st.session_state.get("coin_intake_prefill_metal") or "").strip(),
            ]
        ).strip()
        if not query:
            st.error("Add a hint or run identifier first so a query can be built.")
        else:
            try:
                _buffer_coin_ai_images(primary=ai_image, secondary=ai_reverse)
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
                st.session_state["coin_intake_prefill_ai_comp"] = normalized_comp
                st.success("Comp completed and wizard prefill updated.")
            except Exception as exc:
                st.error(f"Comp failed: {exc}")

    with st.form("coin_intake_wizard_form", clear_on_submit=False):
        st.markdown("### 1) Reference + AI Context")
        ref_key = st.selectbox("Coin Reference", options=list(coin_ref_options.keys()))
        selected_ref = coin_ref_options.get(ref_key)
        if selected_ref is not None:
            st.caption(f"Selected reference: {_coin_ref_summary(selected_ref)}")
        apply_ai_identifier = st.checkbox("Apply latest Coin Identifier result to description", value=True)
        apply_ai_grader = st.checkbox("Apply latest Coin Grader result to grading fields", value=True)
        apply_ai_comp = st.checkbox("Apply latest AI Comp summary to AI Comp", value=False)

        st.markdown("### 2) Product / Inventory")
        p1, p2, p3 = st.columns(3)
        with p1:
            sku_seed_category = st.selectbox("SKU Category Seed", ["coins", "bullion", "collectibles", "antiques", "other"])
        with p2:
            sku_seed_metal = st.text_input(
                "SKU Metal Seed",
                value=str(getattr(selected_ref, "metal_type", "") or "coin"),
            )
        with p3:
            generated_sku = generate_sku(sku_seed_category, sku_seed_metal)
            sku = st.text_input("SKU", value=generated_sku)

        product_title_default = str(
            st.session_state.get("coin_intake_prefill_title")
            or getattr(selected_ref, "coin_name", "")
            or ""
        ).strip()
        product_title = st.text_input("Product Title", value=product_title_default)
        category_default = "coins"
        if str(getattr(selected_ref, "metal_type", "") or "").strip().lower() in {"gold", "silver", "platinum", "palladium", "copper"}:
            category_default = "bullion"
        category = st.selectbox(
            "Category",
            ["coins", "bullion", "collectibles", "antiques", "other"],
            index=["coins", "bullion", "collectibles", "antiques", "other"].index(category_default),
        )
        inventory_class = st.selectbox(
            "Inventory Class",
            ["sellable", "raw_material", "supply"],
            index=0,
        )
        metal_type = st.text_input(
            "Metal Type",
            value=str(
                st.session_state.get("coin_intake_prefill_metal")
                or getattr(selected_ref, "metal_type", "")
                or ""
            ).strip(),
        )
        ref_weight_oz = 0.0
        if selected_ref is not None:
            ref_weight_oz = float(selected_ref.asw_oz or 0.0)
            if ref_weight_oz <= 0 and selected_ref.weight_grams is not None:
                ref_weight_oz = float(selected_ref.weight_grams) / 31.1034768
        w1, w2, w3 = st.columns(3)
        with w1:
            weight_oz = st.number_input("Weight (oz)", min_value=0.0, value=max(0.0, ref_weight_oz), step=0.01)
        with w2:
            acquisition_cost = st.number_input("Acquisition Cost", min_value=0.0, value=0.0, step=0.01)
        with w3:
            current_qty = st.number_input("Quantity", min_value=0, value=1, step=1)
        acquisition_tax_paid = st.number_input("Acquisition Tax Paid", min_value=0.0, value=0.0, step=0.01)
        ctax1, ctax2 = st.columns(2)
        with ctax1:
            acquisition_shipping_paid = st.number_input("Acquisition Shipping Paid", min_value=0.0, value=0.0, step=0.01)
        with ctax2:
            acquisition_handling_paid = st.number_input("Acquisition Handling Paid", min_value=0.0, value=0.0, step=0.01)
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

        acquired_date = st.date_input("Acquired Date", value=utc_today())
        lot_key = st.selectbox("Purchase Lot (optional)", options=list(lot_options.keys()))
        product_description = st.text_area(
            "Product Description",
            value=str(
                st.session_state.get("coin_intake_prefill_description")
                or (_coin_ref_summary(selected_ref) if selected_ref is not None else "")
            ),
        )
        ai_graded = st.checkbox("AI_GRADED", value=False)
        ai_grading_description = st.text_area(
            "AI Grading Description",
            value=str(st.session_state.get("coin_intake_prefill_ai_grading") or ""),
        )
        ai_description = st.text_area(
            "AI Description",
            value=str(st.session_state.get("coin_intake_prefill_ai_description") or ""),
        )
        ai_comp = st.text_area(
            "AI Comp",
            value=str(st.session_state.get("coin_intake_prefill_ai_comp") or ""),
        )

        st.markdown("### 4) Optional Draft eBay Listing")
        create_ebay_draft = st.checkbox("Create draft eBay listing after product is created", value=True)
        l1, l2 = st.columns(2)
        with l1:
            draft_markup_pct = st.number_input("Draft Markup %", min_value=0.0, value=20.0, step=1.0)
        with l2:
            draft_qty = st.number_input("Draft Listing Qty", min_value=1, value=1, step=1)
        attach_uploaded_media_to_listing = st.checkbox("Attach uploaded media to draft listing", value=True)
        include_ai_in_listing_details = st.checkbox("Include AI fields in listing details", value=True)

        submit = st.form_submit_button("Run Intake Wizard")

    if not submit:
        return

    if not ensure_permission(user, "create", "Run Coin Intake Wizard"):
        st.stop()
    if ebay_purchase and not ebay_purchase_item_id.strip():
        st.error("eBay Purchase Item ID is required when Purchased On eBay is enabled.")
        st.stop()

    if apply_ai_identifier:
        ai_description = normalize_ai_text(
            str(st.session_state.get("coin_identifier_last_result") or "").strip()
        ) or ai_description
    if apply_ai_grader:
        ai_grading_description = normalize_ai_text(
            str(st.session_state.get("coin_grader_last_result") or "").strip()
        ) or ai_grading_description
        if ai_grading_description:
            ai_graded = True
    if apply_ai_comp:
        ai_comp = normalize_ai_text(
            str(st.session_state.get("comp_last_ai_summary") or "").strip()
        ) or ai_comp

    try:
        created_product = repo.create_product(
            sku=sku.strip(),
            title=product_title.strip(),
            category=category,
            inventory_class=inventory_class,
            description=product_description.strip(),
            metal_type=metal_type.strip(),
            weight_oz=to_decimal_or_none(weight_oz),
            acquisition_cost=to_decimal_or_none(acquisition_cost),
            acquisition_tax_paid=to_decimal_or_none(acquisition_tax_paid),
            acquisition_shipping_paid=to_decimal_or_none(acquisition_shipping_paid),
            acquisition_handling_paid=to_decimal_or_none(acquisition_handling_paid),
            current_quantity=int(current_qty),
            product_cost=to_decimal_or_none(product_cost),
            ebay_purchase=bool(ebay_purchase),
            ebay_purchase_item_id=ebay_purchase_item_id.strip(),
            ebay_purchase_url=ebay_purchase_url.strip(),
            coin_reference_id=(int(selected_ref.id) if selected_ref is not None else None),
            acquired_at=datetime.combine(acquired_date, datetime.min.time()),
            lot_id=lot_options.get(lot_key),
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

        uploaded_count = 0
        uploaded_errors: list[str] = []
        media_to_upload = list(intake_media or [])
        if include_ai_images_on_submit:
            for row in list(st.session_state.get("coin_intake_ai_buffered_media") or []):
                media_to_upload.append(
                    _BufferedUploadFile(
                        name=str(row.get("name") or "coin_ai_image.jpg"),
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
                    attach_errors.append(f"#{int(media_id)} already linked to product #{int(row.product_id)}.")
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
        if create_ebay_draft:
            draft_price = float(acquisition_cost) * (1.0 + float(draft_markup_pct) / 100.0)
            if draft_price <= 0:
                draft_price = 0.01
            listing_title = product_title.strip() or created_product.title
            listing_details_parts = []
            if selected_ref is not None:
                listing_details_parts.append(f"Coin Reference: {_coin_ref_summary(selected_ref)}")
            if include_ai_in_listing_details:
                if ai_description.strip():
                    listing_details_parts.append(f"AI Description:\n{ai_description.strip()}")
                if ai_grading_description.strip():
                    listing_details_parts.append(f"AI Grading Notes:\n{ai_grading_description.strip()}")
            created_listing = repo.create_listing(
                product_id=int(created_product.id),
                marketplace="ebay",
                listing_title=listing_title,
                listing_price=Decimal(str(round(draft_price, 2))),
                quantity_listed=max(1, int(draft_qty)),
                marketplace_details="\n\n".join(listing_details_parts).strip(),
                listing_status="draft",
                listed_at=utcnow_naive(),
                actor=user.username,
            )
            created_listing_id = int(created_listing.id)
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
        if created_listing_id is not None:
            success_msg += f" and draft eBay listing #{created_listing_id}"
        success_msg += "."
        st.success(success_msg)
        if uploaded_count:
            st.success(f"Uploaded {uploaded_count} media file(s) to product.")
        for err in uploaded_errors:
            st.error(f"Media upload failed: {err}")
    except Exception as exc:
        repo.db.rollback()
        st.error(f"Coin intake wizard failed: {exc}")
