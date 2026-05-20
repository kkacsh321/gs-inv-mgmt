from datetime import datetime
import hashlib
import json

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import iso_or_none, normalize_multiselect_values, to_decimal_or_none
from app.components.views.shared import (
    generate_sku,
    render_help_panel,
    render_media_capture_inputs,
    render_media_file_actions,
    render_media_gallery,
    render_table_toolbar,
    upload_media_for_listing,
)
from app.components.views.entity_ops import (
    render_saved_filter_bar,
    render_standard_row_actions,
)
from app.components.views.workspace_shell import render_workspace_feedback
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.ai_text import normalize_ai_text
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_str
from app.services.validation import ValidationService, ValidationError
from app.utils.time import utc_today, utcnow_naive


def _coin_reference_summary(ref) -> str:
    if ref is None:
        return ""
    year_start = getattr(ref, "year_start", None)
    year_end = getattr(ref, "year_end", None)
    years = ""
    if year_start and year_end:
        years = f"{int(year_start)}-{int(year_end)}"
    elif year_start:
        years = str(int(year_start))
    composition = str(getattr(ref, "composition", "") or "").strip()
    metal_type = str(getattr(ref, "metal_type", "") or "").strip()
    denomination = str(getattr(ref, "denomination", "") or "").strip()
    series = str(getattr(ref, "series", "") or "").strip()
    pieces = [
        p
        for p in [
            f"{getattr(ref, 'coin_name', '')}".strip(),
            f"Series: {series}" if series else "",
            f"Denomination: {denomination}" if denomination else "",
            f"Years: {years}" if years else "",
            f"Metal: {metal_type}" if metal_type else "",
            f"Composition: {composition}" if composition else "",
        ]
        if p
    ]
    return " | ".join(pieces)


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
        snippet = text[first : last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _validate_product_create_inputs(
    sku: str,
    title: str,
    ebay_purchase: bool,
    ebay_purchase_item_id: str,
) -> str | None:
    if not str(sku or "").strip() or not str(title or "").strip():
        return "SKU and title are required."
    if bool(ebay_purchase) and not str(ebay_purchase_item_id or "").strip():
        return "eBay Purchase Item ID is required when Purchased On eBay is enabled."
    return None


def _validate_product_edit_ebay_inputs(
    ebay_purchase: bool,
    ebay_purchase_item_id: str,
) -> str | None:
    if bool(ebay_purchase) and not str(ebay_purchase_item_id or "").strip():
        return "eBay Purchase Item ID is required when Purchased On eBay is enabled."
    return None


def _product_ebay_fields_disabled(*, ebay_purchase: bool, context: str) -> bool:
    # Products page keeps eBay fields editable in both create and side-panel edit flows.
    # Validation enforces required Item ID only when Purchased On eBay is enabled.
    _ = context
    _ = ebay_purchase
    return False


def render_products(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    st.subheader("Products")
    render_help_panel(
        section_title="Products",
        goal="Create and maintain inventory items with SKU, cost basis, shipping attributes, and media.",
        steps=[
            "Generate or enter a unique SKU, then enter title/category/core item details.",
            "Capture acquired date, cost, and lot assignment to keep purchase traceability.",
            "Upload product photos/videos now or later from Product Media Manager.",
            "Fill package dimensions/weight to support downstream shipping labels and rates.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )
    lots = repo.list_purchase_lots()
    coin_refs = repo.list_coin_references(active_only=True, limit=5000)
    coin_ref_options = {"None": None}
    coin_ref_options.update(
        {
            f"#{row.id} | {row.coin_name} | {row.country} | {row.series}": row
            for row in coin_refs
        }
    )

    for key, default_value in {
        "product_title_input": "",
        "product_category_input": "bullion",
        "product_description_input": "",
        "product_metal_type_input": "",
        "product_weight_oz_input": 0.0,
        "product_acquisition_cost_input": 0.0,
        "product_acquisition_tax_paid_input": 0.0,
        "product_acquisition_shipping_paid_input": 0.0,
        "product_acquisition_handling_paid_input": 0.0,
        "product_product_cost_input": 0.0,
        "product_qty_input": 1,
        "product_ebay_purchase_input": False,
        "product_ebay_purchase_item_id_input": "",
        "product_ebay_purchase_url_input": "",
        "product_package_weight_oz_input": 0.0,
        "product_package_length_in_input": 0.0,
        "product_package_width_in_input": 0.0,
        "product_package_height_in_input": 0.0,
    }.items():
        if key not in st.session_state:
            st.session_state[key] = default_value

    st.markdown("### Coin Catalog Assisted Intake")
    ai1, ai2 = st.columns([2, 1])
    with ai1:
        selected_coin_ref_key = st.selectbox(
            "Coin Reference (optional)",
            options=list(coin_ref_options.keys()),
            key="create_product_coin_reference_select",
            help="Link product to a coin catalog item for better consistency and downstream listing/AI workflows.",
        )
    with ai2:
        st.write("")
        st.write("")
        apply_coin_ref_defaults = st.button("Apply Coin Ref Defaults", key="apply_coin_ref_defaults_btn")
    selected_coin_ref = coin_ref_options.get(selected_coin_ref_key)
    if selected_coin_ref is not None:
        st.caption(_coin_reference_summary(selected_coin_ref))
    if apply_coin_ref_defaults and selected_coin_ref is not None:
        st.session_state["product_title_input"] = str(selected_coin_ref.coin_name or "").strip()
        st.session_state["product_category_input"] = "coins"
        st.session_state["product_metal_type_input"] = str(selected_coin_ref.metal_type or "").strip()
        ref_weight_oz = float(selected_coin_ref.asw_oz or 0)
        if ref_weight_oz <= 0 and selected_coin_ref.weight_grams is not None:
            ref_weight_oz = float(selected_coin_ref.weight_grams) / 31.1034768
        st.session_state["product_weight_oz_input"] = max(0.0, ref_weight_oz)
        st.session_state["product_description_input"] = _coin_reference_summary(selected_coin_ref)
        st.success("Applied coin reference defaults to product create form.")

    st.markdown("### Product Copilot (AI Suggestions)")
    ai_seed_prompt = st.text_area(
        "AI Seed Prompt (optional)",
        key="product_ai_seed_prompt",
        help="Use plain language like: 'optimize title for eBay bullion buyer search'.",
    )
    if st.button("Generate Product AI Suggestions", key="product_generate_ai_suggestions_btn"):
        if not ensure_permission(user, "ai_comp_use", "Generate Product AI Suggestions"):
            st.stop()
        try:
            system_message = get_runtime_str(
                repo,
                "comp_llm_system_message",
                "You are a resale inventory assistant. Return concise outputs.",
            ).strip()
            instruction = (
                "Return ONLY JSON with keys: "
                "`suggested_title`, `suggested_category`, `suggested_description`, "
                "`suggested_metal_type`, `suggested_weight_oz`, `suggested_ai_description`. "
                "Use categories only from: bullion, coins, collectibles, antiques, other. "
                "Keep text concise and operational."
            )
            context_parts = [
                str(ai_seed_prompt or "").strip(),
                str(st.session_state.get("product_title_input") or "").strip(),
                str(st.session_state.get("product_description_input") or "").strip(),
                str(st.session_state.get("product_metal_type_input") or "").strip(),
            ]
            if selected_coin_ref is not None:
                context_parts.append(_coin_reference_summary(selected_coin_ref))
            query_text = " | ".join([p for p in context_parts if p]).strip() or "Suggest product intake defaults"
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
                st.warning("AI output was not valid JSON. Stored raw output in AI Description field.")
                st.session_state["product_description_input"] = str(result.text or "").strip()
            else:
                title_val = str(payload.get("suggested_title") or "").strip()
                category_val = str(payload.get("suggested_category") or "").strip().lower()
                description_val = str(payload.get("suggested_description") or "").strip()
                metal_val = str(payload.get("suggested_metal_type") or "").strip()
                ai_desc_val = str(payload.get("suggested_ai_description") or "").strip()
                weight_raw = str(payload.get("suggested_weight_oz") or "").strip()
                if title_val:
                    st.session_state["product_title_input"] = title_val
                if category_val in {"bullion", "coins", "collectibles", "antiques", "other"}:
                    st.session_state["product_category_input"] = category_val
                if description_val:
                    st.session_state["product_description_input"] = description_val
                if metal_val:
                    st.session_state["product_metal_type_input"] = metal_val
                if ai_desc_val:
                    st.session_state["product_description_input"] = (
                        st.session_state.get("product_description_input", "").strip()
                        + ("\n\n" if st.session_state.get("product_description_input") else "")
                        + f"AI Notes:\n{ai_desc_val}"
                    )
                try:
                    weight_val = float(weight_raw) if weight_raw else 0.0
                    if weight_val > 0:
                        st.session_state["product_weight_oz_input"] = weight_val
                except Exception:
                    pass
                st.session_state["product_ai_suggestion_raw"] = str(result.text or "").strip()
                st.success("AI suggestions applied to product create defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"AI suggestion generation failed: {exc}")

    raw_product_ai = str(st.session_state.get("product_ai_suggestion_raw") or "").strip()
    if raw_product_ai:
        with st.expander("Last Product Copilot Payload", expanded=False):
            st.code(raw_product_ai, language="json")

    st.markdown("### SKU Generator")
    inventory_classes = ["sellable", "raw_material", "supply"]
    sku_seed_col1, sku_seed_col2, sku_seed_col3 = st.columns([1, 1, 1])
    with sku_seed_col1:
        sku_category_seed = st.selectbox(
            "Category Seed",
            ["bullion", "coins", "collectibles", "antiques", "other"],
            key="sku_seed_category",
        )
    with sku_seed_col2:
        sku_metal_seed = st.text_input("Metal Seed", value="silver", key="sku_seed_metal")
    with sku_seed_col3:
        st.write("")
        st.write("")
        if st.button("Generate SKU"):
            st.session_state["product_sku_input"] = generate_sku(
                sku_category_seed,
                sku_metal_seed,
            )

    if "product_sku_input" not in st.session_state:
        st.session_state["product_sku_input"] = ""

    st.markdown("### Optional Initial Product Media")
    product_uploaded_by = st.text_input("Uploaded By", value="employee", key="product_uploaded_by")
    product_files = render_media_capture_inputs(
        key_prefix="create_product_media",
        upload_label="Product Photos/Videos (optional)",
        allow_enhanced=True,
    )

    with st.form("create_product_form", clear_on_submit=True):
        sku = st.text_input("SKU", key="product_sku_input", help="Internal unique SKU used across all channels.")
        title = st.text_input("Title", key="product_title_input")
        category = st.selectbox(
            "Category",
            ["bullion", "coins", "collectibles", "antiques", "other"],
            index=["bullion", "coins", "collectibles", "antiques", "other"].index(
                str(st.session_state.get("product_category_input") or "bullion")
            )
            if str(st.session_state.get("product_category_input") or "bullion") in {
                "bullion",
                "coins",
                "collectibles",
                "antiques",
                "other",
            }
            else 0,
            key="product_category_input",
        )
        inventory_class = st.selectbox(
            "Inventory Class",
            options=inventory_classes,
            index=0,
            help="Use `raw_material`/`supply` for inputs that may later be converted into sellable SKUs.",
        )
        description = st.text_area("Description", key="product_description_input")
        metal_type = st.text_input(
            "Metal Type",
            placeholder="gold / silver / platinum / etc",
            key="product_metal_type_input",
        )
        create_coin_ref_id = (
            int(getattr(selected_coin_ref, "id", 0))
            if selected_coin_ref is not None
            else None
        )
        acquired_date = st.date_input("Acquired Date", value=utc_today())
        lot_options = {"None": None, **{f"{lot.lot_code} | {lot.vendor}": lot.id for lot in lots}}
        lot_key = st.selectbox(
            "Purchase Lot (Optional)",
            list(lot_options.keys()),
            help="Assign this product to a lot for purchase tracking.",
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            weight_oz = st.number_input(
                "Weight (oz)",
                min_value=0.0,
                step=0.01,
                key="product_weight_oz_input",
            )
        with c2:
            acquisition_cost = st.number_input(
                "Acquisition Cost",
                min_value=0.0,
                step=1.0,
                key="product_acquisition_cost_input",
            )
        with c3:
            qty = st.number_input(
                "Quantity",
                min_value=0,
                step=1,
                key="product_qty_input",
            )
        acquisition_tax_paid = st.number_input(
            "Acquisition Tax Paid",
            min_value=0.0,
            step=1.0,
            key="product_acquisition_tax_paid_input",
            help="Track purchase tax separately from acquisition cost.",
        )
        acq1, acq2 = st.columns(2)
        with acq1:
            acquisition_shipping_paid = st.number_input(
                "Acquisition Shipping Paid",
                min_value=0.0,
                step=1.0,
                key="product_acquisition_shipping_paid_input",
            )
        with acq2:
            acquisition_handling_paid = st.number_input(
                "Acquisition Handling Paid",
                min_value=0.0,
                step=1.0,
                key="product_acquisition_handling_paid_input",
            )
        product_cost = st.number_input(
            "Product Cost",
            min_value=0.0,
            step=1.0,
            key="product_product_cost_input",
            help="Direct per-product cost used for margin tracking and quick pricing decisions.",
        )
        ebay_purchase = st.checkbox(
            "Purchased On eBay",
            value=False,
            key="product_ebay_purchase_input",
        )
        ebay_purchase_item_id = st.text_input(
            "eBay Purchase Item ID",
            value="",
            key="product_ebay_purchase_item_id_input",
            disabled=_product_ebay_fields_disabled(ebay_purchase=bool(ebay_purchase), context="create"),
        )
        ebay_purchase_url = st.text_input(
            "eBay Purchase Link",
            value="",
            key="product_ebay_purchase_url_input",
            disabled=_product_ebay_fields_disabled(ebay_purchase=bool(ebay_purchase), context="create"),
        )
        if not ebay_purchase:
            st.caption("Tip: enable `Purchased On eBay` to require Item ID validation on create.")
        st.markdown("#### Shipping Package Details")
        sp1, sp2, sp3, sp4 = st.columns(4)
        with sp1:
            package_weight_oz = st.number_input(
                "Package Weight (oz)",
                min_value=0.0,
                step=0.01,
                key="product_package_weight_oz_input",
            )
        with sp2:
            package_length_in = st.number_input(
                "Length (in)",
                min_value=0.0,
                step=0.1,
                key="product_package_length_in_input",
            )
        with sp3:
            package_width_in = st.number_input(
                "Width (in)",
                min_value=0.0,
                step=0.1,
                key="product_package_width_in_input",
            )
        with sp4:
            package_height_in = st.number_input(
                "Height (in)",
                min_value=0.0,
                step=0.1,
                key="product_package_height_in_input",
            )

        if st.form_submit_button("Create Product"):
            if not ensure_permission(user, "create", "Create Product"):
                st.stop()
            create_validation_error = _validate_product_create_inputs(
                sku=sku,
                title=title,
                ebay_purchase=bool(ebay_purchase),
                ebay_purchase_item_id=ebay_purchase_item_id,
            )
            if create_validation_error:
                st.error(create_validation_error)
            else:
                try:
                    created_product = repo.create_product(
                        sku=sku.strip(),
                        title=title.strip(),
                        category=category,
                        inventory_class=inventory_class,
                        description=description.strip(),
                        metal_type=metal_type.strip(),
                        weight_oz=to_decimal_or_none(weight_oz),
                        package_weight_oz=to_decimal_or_none(package_weight_oz),
                        package_length_in=to_decimal_or_none(package_length_in),
                        package_width_in=to_decimal_or_none(package_width_in),
                        package_height_in=to_decimal_or_none(package_height_in),
                        acquisition_cost=to_decimal_or_none(acquisition_cost),
                        acquisition_tax_paid=to_decimal_or_none(acquisition_tax_paid),
                        acquisition_shipping_paid=to_decimal_or_none(acquisition_shipping_paid),
                        acquisition_handling_paid=to_decimal_or_none(acquisition_handling_paid),
                        product_cost=to_decimal_or_none(product_cost),
                        current_quantity=int(qty),
                        ebay_purchase=bool(ebay_purchase),
                        ebay_purchase_item_id=ebay_purchase_item_id.strip(),
                        ebay_purchase_url=ebay_purchase_url.strip(),
                        coin_reference_id=create_coin_ref_id,
                        acquired_at=datetime.combine(acquired_date, datetime.min.time()),
                        lot_id=lot_options[lot_key],
                        actor=user.username,
                    )
                    st.success("Product created.")
                    if product_files:
                        if not storage.enabled:
                            st.warning(
                                "Product created, but media upload skipped because S3 storage is not configured."
                            )
                        else:
                            uploaded, errors = upload_media_for_listing(
                                repo=repo,
                                storage=storage,
                                listing_id=None,
                                product_id=created_product.id,
                                uploaded_files=product_files,
                                uploaded_by=product_uploaded_by,
                            )
                            if uploaded:
                                st.success(f"Uploaded {uploaded} media file(s) to product.")
                            for error in errors:
                                st.error(f"Upload failed: {error}")
                except IntegrityError:
                    repo.db.rollback()
                    st.error("SKU must be unique. Use a different SKU.")

    products = repo.list_products()
    if not products:
        st.info("No products yet.")
        return

    product_rows = [
        {
            "id": p.id,
            "sku": p.sku,
            "title": p.title,
            "category": p.category,
            "inventory_class": str(getattr(p, "inventory_class", "sellable") or "sellable"),
            "metal": p.metal_type,
            "weight_oz": float(p.weight_oz) if p.weight_oz is not None else None,
            "pkg_weight_oz": float(p.package_weight_oz) if p.package_weight_oz is not None else None,
            "length_in": float(p.package_length_in) if p.package_length_in is not None else None,
            "width_in": float(p.package_width_in) if p.package_width_in is not None else None,
            "height_in": float(p.package_height_in) if p.package_height_in is not None else None,
            "acquisition_cost": float(p.acquisition_cost) if p.acquisition_cost is not None else None,
            "acquisition_tax_paid": float(getattr(p, "acquisition_tax_paid", None)) if getattr(p, "acquisition_tax_paid", None) is not None else None,
            "acquisition_shipping_paid": float(getattr(p, "acquisition_shipping_paid", None)) if getattr(p, "acquisition_shipping_paid", None) is not None else None,
            "acquisition_handling_paid": float(getattr(p, "acquisition_handling_paid", None)) if getattr(p, "acquisition_handling_paid", None) is not None else None,
            "landed_unit_cost": (
                (
                    float(p.acquisition_cost or 0)
                    + float(getattr(p, "acquisition_tax_paid", 0) or 0)
                    + float(getattr(p, "acquisition_shipping_paid", 0) or 0)
                    + float(getattr(p, "acquisition_handling_paid", 0) or 0)
                )
                if (
                    p.acquisition_cost is not None
                    or getattr(p, "acquisition_tax_paid", None) is not None
                    or getattr(p, "acquisition_shipping_paid", None) is not None
                    or getattr(p, "acquisition_handling_paid", None) is not None
                )
                else None
            ),
            "landed_on_hand_value": (
                (
                    (
                        float(p.acquisition_cost or 0)
                        + float(getattr(p, "acquisition_tax_paid", 0) or 0)
                        + float(getattr(p, "acquisition_shipping_paid", 0) or 0)
                        + float(getattr(p, "acquisition_handling_paid", 0) or 0)
                    )
                    * int(p.current_quantity or 0)
                )
                if (
                    p.acquisition_cost is not None
                    or getattr(p, "acquisition_tax_paid", None) is not None
                    or getattr(p, "acquisition_shipping_paid", None) is not None
                    or getattr(p, "acquisition_handling_paid", None) is not None
                )
                else None
            ),
            "product_cost": float(getattr(p, "product_cost", None)) if getattr(p, "product_cost", None) is not None else None,
            "ebay_purchase": bool(getattr(p, "ebay_purchase", False)),
            "ebay_purchase_item_id": str(getattr(p, "ebay_purchase_item_id", "") or ""),
            "ebay_purchase_url": str(getattr(p, "ebay_purchase_url", "") or ""),
            "qty": p.current_quantity,
            "acquired_at": iso_or_none(p.acquired_at),
            "status": p.status,
            "coin_ref_id": p.coin_reference_id,
            "ai_graded": bool(getattr(p, "ai_graded", False)),
            "ai_comped": bool(str(getattr(p, "ai_comp", "") or "").strip()),
            "media_count": len(p.media_assets),
        }
        for p in products
    ]
    st.markdown("### Product Filters")
    product_category_options = sorted({str(row["category"]) for row in product_rows if row.get("category")})
    product_status_options = sorted({str(row["status"]) for row in product_rows if row.get("status")})
    product_inventory_class_options = sorted(
        {str(row["inventory_class"]) for row in product_rows if row.get("inventory_class")}
    )
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        product_filter_query = st.text_input("Search SKU/Title", key="products_filter_query")
    with f2:
        product_filter_categories = st.multiselect(
            "Category",
            options=product_category_options,
            key="products_filter_categories",
        )
    with f3:
        product_filter_status = st.multiselect(
            "Status",
            options=product_status_options,
            key="products_filter_status",
        )
    with f4:
        product_filter_inventory_classes = st.multiselect(
            "Inventory Class",
            options=product_inventory_class_options,
            key="products_filter_inventory_classes",
        )
    product_filter_include_archived = st.checkbox(
        "Include Archived",
        value=False,
        key="products_filter_include_archived",
        help="Show archived products in table and detail selection.",
    )
    product_filter_categories = normalize_multiselect_values(
        product_filter_categories,
        product_category_options,
    )
    product_filter_status = normalize_multiselect_values(
        product_filter_status,
        product_status_options,
    )
    product_filter_inventory_classes = normalize_multiselect_values(
        product_filter_inventory_classes,
        product_inventory_class_options,
    )
    effective_filter = render_saved_filter_bar(
        repo=repo,
        scope="products",
        username=user.username,
        current_filters={
            "query": product_filter_query,
            "categories": product_filter_categories,
            "statuses": product_filter_status,
            "inventory_classes": product_filter_inventory_classes,
            "include_archived": bool(product_filter_include_archived),
        },
    )
    q = str(effective_filter.get("query") or "").strip().lower()
    categories = {str(v).strip().lower() for v in (effective_filter.get("categories") or []) if str(v).strip()}
    statuses = {str(v).strip().lower() for v in (effective_filter.get("statuses") or []) if str(v).strip()}
    inventory_class_filter = {
        str(v).strip().lower() for v in (effective_filter.get("inventory_classes") or []) if str(v).strip()
    }
    include_archived = bool(effective_filter.get("include_archived"))
    filtered_rows = []
    for row in product_rows:
        if q and q not in str(row.get("sku") or "").lower() and q not in str(row.get("title") or "").lower():
            continue
        if categories and str(row.get("category") or "").strip().lower() not in categories:
            continue
        if statuses and str(row.get("status") or "").strip().lower() not in statuses:
            continue
        if inventory_class_filter and str(row.get("inventory_class") or "").strip().lower() not in inventory_class_filter:
            continue
        if not include_archived and str(row.get("status") or "").strip().lower() == "archived":
            continue
        filtered_rows.append(row)

    filtered_df = pd.DataFrame(filtered_rows)
    st.markdown("### Product Table")
    table_col = st.container()
    panel_col = st.container()
    with table_col:
        render_table_toolbar(
            df=filtered_df,
            section_key="products_table",
            export_basename="products_filtered",
            active_filters={
                "query": q,
                "categories": sorted(categories),
                "statuses": sorted(statuses),
                "inventory_classes": sorted(inventory_class_filter),
                "include_archived": bool(include_archived),
            },
        )
        st.dataframe(filtered_df, use_container_width=True)
        render_standard_row_actions(
            repo,
            entity_type="product",
            rows=filtered_rows,
            id_field="id",
            title="Product Row Actions",
        )

    with panel_col:
        st.markdown("### Product Detail/Edit")
        if not filtered_rows:
            st.info("No filtered products available.")
        else:
            product_index = {p.id: p for p in products}
            select_options = {
                f"#{row['id']} | {row['sku']} | {row['title']}": int(row["id"]) for row in filtered_rows
            }
            selected_label = st.selectbox(
                "Select Product",
                options=list(select_options.keys()),
                key="products_side_panel_select",
            )
            selected_product = product_index[select_options[selected_label]]
            side_lot_options = {"None": None, **{f"{lot.lot_code} | {lot.vendor}": lot.id for lot in lots}}
            st.caption(f"Current SKU: `{selected_product.sku}`")
            st.page_link("pages/06_Tools.py", label="Open AI Tools (Coin/Comp)")
            cp1, cp2, cp3 = st.columns(3)
            with cp1:
                open_comp_for_product = st.button(
                    "Comp Tool: Product",
                    key=f"product_comp_open_{selected_product.id}",
                    help="Open Comp Tool with this product pre-selected.",
                )
            with cp2:
                open_comp_with_context = st.button(
                    "Comp Tool: AI Context",
                    key=f"product_comp_ai_open_{selected_product.id}",
                    help="Open Comp Tool with manual query enriched by product AI/coin context.",
                )
            with cp3:
                open_comp_photo_mode = st.button(
                    "Comp Tool: Photo Mode",
                    key=f"product_comp_photo_open_{selected_product.id}",
                    help="Open Comp Tool in Image/File Hint mode with product hints prefilled.",
                )
            if open_comp_for_product or open_comp_with_context or open_comp_photo_mode:
                st.session_state["comp_prefill_product_id"] = int(selected_product.id)
                st.session_state["comp_prefill_source_mode"] = (
                    "Image/File Hint"
                    if open_comp_photo_mode
                    else ("Manual Title/Description" if open_comp_with_context else "Inventory Item")
                )
                st.session_state["comp_prefill_query"] = " ".join(
                    [
                        str(selected_product.title or "").strip(),
                        str(selected_product.metal_type or "").strip(),
                    ]
                ).strip()
                if open_comp_with_context:
                    st.session_state["comp_prefill_manual_title"] = str(selected_product.title or "").strip()
                    st.session_state["comp_prefill_manual_desc"] = "\n\n".join(
                        [
                            str(selected_product.description or "").strip(),
                            str(selected_product.ai_description or "").strip(),
                            str(selected_product.ai_grading_description or "").strip(),
                        ]
                    ).strip()
                elif open_comp_photo_mode:
                    st.session_state["comp_prefill_manual_title"] = str(selected_product.title or "").strip()
                    st.session_state["comp_prefill_manual_desc"] = str(selected_product.description or "").strip()
                st.session_state["comp_prefill_origin"] = f"product:{int(selected_product.id)}"
                st.switch_page("pages/06_Tools.py")

            with st.form("products_side_panel_edit_form"):
                ep1, ep2 = st.columns(2)
                with ep1:
                    edit_title = st.text_input("Title", value=selected_product.title)
                    edit_category = st.selectbox(
                        "Category",
                        ["bullion", "coins", "collectibles", "antiques", "other"],
                        index=["bullion", "coins", "collectibles", "antiques", "other"].index(selected_product.category)
                        if selected_product.category in {"bullion", "coins", "collectibles", "antiques", "other"}
                        else 4,
                    )
                    edit_inventory_class = st.selectbox(
                        "Inventory Class",
                        inventory_classes,
                        index=inventory_classes.index(
                            str(getattr(selected_product, "inventory_class", "sellable") or "sellable")
                        )
                        if str(getattr(selected_product, "inventory_class", "sellable") or "sellable")
                        in set(inventory_classes)
                        else 0,
                    )
                    edit_status = st.text_input("Status", value=selected_product.status or "active")
                    edit_qty = st.number_input(
                        "Quantity",
                        min_value=0,
                        value=int(selected_product.current_quantity or 0),
                        step=1,
                    )
                    edit_acquisition_cost = st.number_input(
                        "Acquisition Cost",
                        min_value=0.0,
                        value=float(selected_product.acquisition_cost or 0),
                        step=1.0,
                    )
                    edit_acquisition_tax_paid = st.number_input(
                        "Acquisition Tax Paid",
                        min_value=0.0,
                        value=float(getattr(selected_product, "acquisition_tax_paid", 0) or 0),
                        step=1.0,
                    )
                    edit_acquisition_shipping_paid = st.number_input(
                        "Acquisition Shipping Paid",
                        min_value=0.0,
                        value=float(getattr(selected_product, "acquisition_shipping_paid", 0) or 0),
                        step=1.0,
                    )
                    edit_acquisition_handling_paid = st.number_input(
                        "Acquisition Handling Paid",
                        min_value=0.0,
                        value=float(getattr(selected_product, "acquisition_handling_paid", 0) or 0),
                        step=1.0,
                    )
                    edit_product_cost = st.number_input(
                        "Product Cost",
                        min_value=0.0,
                        value=float(getattr(selected_product, "product_cost", 0) or 0),
                        step=1.0,
                    )
                    edit_ebay_purchase = st.checkbox(
                        "Purchased On eBay",
                        value=bool(getattr(selected_product, "ebay_purchase", False)),
                    )
                    edit_ebay_purchase_item_id = st.text_input(
                        "eBay Purchase Item ID",
                        value=str(getattr(selected_product, "ebay_purchase_item_id", "") or ""),
                        disabled=_product_ebay_fields_disabled(
                            ebay_purchase=bool(edit_ebay_purchase),
                            context="edit",
                        ),
                    )
                    edit_ebay_purchase_url = st.text_input(
                        "eBay Purchase Link",
                        value=str(getattr(selected_product, "ebay_purchase_url", "") or ""),
                        disabled=_product_ebay_fields_disabled(
                            ebay_purchase=bool(edit_ebay_purchase),
                            context="edit",
                        ),
                    )
                    if not edit_ebay_purchase:
                        st.caption("Tip: enable `Purchased On eBay` to require Item ID validation on save.")
                    current_coin_ref_id = int(selected_product.coin_reference_id or 0)
                    coin_ref_edit_labels = list(coin_ref_options.keys())
                    default_coin_ref_index = 0
                    for idx, label in enumerate(coin_ref_edit_labels):
                        row = coin_ref_options.get(label)
                        if row is not None and int(getattr(row, "id", 0)) == current_coin_ref_id:
                            default_coin_ref_index = idx
                            break
                    edit_coin_ref_key = st.selectbox(
                        "Coin Reference",
                        options=coin_ref_edit_labels,
                        index=default_coin_ref_index,
                        help="Link to coin catalog reference for cleaner intake/listing workflows.",
                    )
                with ep2:
                    edit_metal_type = st.text_input("Metal Type", value=selected_product.metal_type or "")
                    edit_weight_oz = st.number_input(
                        "Weight (oz)",
                        min_value=0.0,
                        value=float(selected_product.weight_oz or 0),
                        step=0.01,
                    )
                    edit_pkg_weight_oz = st.number_input(
                        "Package Weight (oz)",
                        min_value=0.0,
                        value=float(selected_product.package_weight_oz or 0),
                        step=0.01,
                    )
                    edit_length = st.number_input(
                        "Length (in)",
                        min_value=0.0,
                        value=float(selected_product.package_length_in or 0),
                        step=0.1,
                    )
                    edit_width = st.number_input(
                        "Width (in)",
                        min_value=0.0,
                        value=float(selected_product.package_width_in or 0),
                        step=0.1,
                    )
                    edit_height = st.number_input(
                        "Height (in)",
                        min_value=0.0,
                        value=float(selected_product.package_height_in or 0),
                        step=0.1,
                    )
                edit_description = st.text_area("Description", value=selected_product.description or "")
                ai1, ai2 = st.columns(2)
                with ai1:
                    edit_ai_graded = st.checkbox(
                        "AI_GRADED",
                        value=bool(getattr(selected_product, "ai_graded", False)),
                    )
                with ai2:
                    st.caption("Use this flag when AI grading has been applied/reviewed.")
                edit_ai_grading_description = st.text_area(
                    "AI Grading Description",
                    value=getattr(selected_product, "ai_grading_description", "") or "",
                )
                edit_ai_description = st.text_area(
                    "AI Description",
                    value=getattr(selected_product, "ai_description", "") or "",
                )
                edit_ai_comp = st.text_area(
                    "AI Comp",
                    value=getattr(selected_product, "ai_comp", "") or "",
                )
                selected_edit_coin_ref = coin_ref_options.get(edit_coin_ref_key)
                if selected_edit_coin_ref is not None:
                    st.caption(f"Linked Coin Ref: {_coin_reference_summary(selected_edit_coin_ref)}")
                apply_last_grade = st.checkbox("Apply last Coin Grader result from this session", value=False)
                apply_last_identifier = st.checkbox("Apply last Coin Identifier result from this session", value=False)
                apply_last_comp = st.checkbox("Apply last AI Comp summary from this session", value=False)
                save_side_panel = st.form_submit_button("Save Product Changes")

            if save_side_panel:
                if not ensure_permission(user, "update", "Update Product"):
                    st.stop()
                try:
                    ai_grading_value = edit_ai_grading_description.strip()
                    ai_description_value = edit_ai_description.strip()
                    ai_comp_value = edit_ai_comp.strip()
                    if apply_last_grade:
                        ai_grading_value = normalize_ai_text(
                            str(st.session_state.get("coin_grader_last_result") or "").strip()
                        ) or ai_grading_value
                        edit_ai_graded = True
                    if apply_last_identifier:
                        ai_description_value = normalize_ai_text(
                            str(st.session_state.get("coin_identifier_last_result") or "").strip()
                        ) or ai_description_value
                    if apply_last_comp:
                        ai_comp_value = normalize_ai_text(
                            str(st.session_state.get("comp_last_ai_summary") or "").strip()
                        ) or ai_comp_value
                    edit_validation_error = _validate_product_edit_ebay_inputs(
                        ebay_purchase=bool(edit_ebay_purchase),
                        ebay_purchase_item_id=edit_ebay_purchase_item_id,
                    )
                    if edit_validation_error:
                        raise ValueError(edit_validation_error)
                    repo.update_product(
                        selected_product.id,
                        {
                            "title": edit_title.strip(),
                            "category": edit_category,
                            "inventory_class": edit_inventory_class,
                            "status": edit_status.strip() or "active",
                            "current_quantity": int(edit_qty),
                            "acquisition_cost": to_decimal_or_none(edit_acquisition_cost),
                            "acquisition_tax_paid": to_decimal_or_none(edit_acquisition_tax_paid),
                            "acquisition_shipping_paid": to_decimal_or_none(edit_acquisition_shipping_paid),
                            "acquisition_handling_paid": to_decimal_or_none(edit_acquisition_handling_paid),
                            "product_cost": to_decimal_or_none(edit_product_cost),
                            "ebay_purchase": bool(edit_ebay_purchase),
                            "ebay_purchase_item_id": edit_ebay_purchase_item_id.strip(),
                            "ebay_purchase_url": edit_ebay_purchase_url.strip(),
                            "coin_reference_id": (
                                int(getattr(selected_edit_coin_ref, "id", 0))
                                if selected_edit_coin_ref is not None
                                else None
                            ),
                            "metal_type": edit_metal_type.strip(),
                            "weight_oz": to_decimal_or_none(edit_weight_oz),
                            "package_weight_oz": to_decimal_or_none(edit_pkg_weight_oz),
                            "package_length_in": to_decimal_or_none(edit_length),
                            "package_width_in": to_decimal_or_none(edit_width),
                            "package_height_in": to_decimal_or_none(edit_height),
                            "description": edit_description.strip(),
                            "ai_graded": bool(edit_ai_graded),
                            "ai_grading_description": normalize_ai_text(ai_grading_value),
                            "ai_description": normalize_ai_text(ai_description_value),
                            "ai_comp": normalize_ai_text(ai_comp_value),
                        },
                        actor=user.username,
                    )
                    st.success("Product updated.")
                    st.rerun()
                except (ValueError, ValidationError, IntegrityError) as exc:
                    repo.db.rollback()
                    st.error(str(exc))

            st.markdown("#### Product Lifecycle")
            product_is_archived = str(getattr(selected_product, "status", "") or "").strip().lower() == "archived"
            active_product_listing_count = sum(
                1
                for row in repo.list_listings()
                if int(getattr(row, "product_id", 0) or 0) == int(selected_product.id)
                and str(getattr(row, "listing_status", "") or "").strip().lower() == "active"
            )
            if product_is_archived:
                st.info("This product is archived.")
                if st.button(
                    "Restore Product",
                    key=f"restore_product_btn_{selected_product.id}",
                    use_container_width=True,
                ):
                    if not ensure_permission(user, "update", "Restore Product"):
                        st.stop()
                    try:
                        repo.restore_product(int(selected_product.id), actor=user.username)
                        st.success(f"Restored product #{int(selected_product.id)}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to restore product: {exc}")
            else:
                if active_product_listing_count > 0:
                    st.warning(
                        f"{active_product_listing_count} active listing(s) are linked to this product. "
                        "Archive is blocked unless forced."
                    )
                force_archive = st.checkbox(
                    "Force archive even with active listings",
                    value=False,
                    key=f"force_archive_product_{selected_product.id}",
                    disabled=active_product_listing_count <= 0,
                )
                if st.button(
                    "Archive Product",
                    key=f"archive_product_btn_{selected_product.id}",
                    use_container_width=True,
                ):
                    if not ensure_permission(user, "update", "Archive Product"):
                        st.stop()
                    try:
                        repo.archive_product(
                            int(selected_product.id),
                            actor=user.username,
                            force=bool(force_archive),
                        )
                        st.success(f"Archived product #{int(selected_product.id)}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to archive product: {exc}")

            st.markdown("#### Link Product To Purchase Lot")
            if not lots:
                st.info("No purchase lots found. Create a lot first from the Lots page.")
            else:
                with st.form(f"product_assign_lot_form_{selected_product.id}"):
                    assign_lot_key = st.selectbox(
                        "Purchase Lot",
                        options=list(side_lot_options.keys()),
                        key=f"assign_lot_key_{selected_product.id}",
                        help="Create an assignment record linking this product to a purchase lot.",
                    )
                    la1, la2, la3, la4 = st.columns(4)
                    with la1:
                        assign_qty = st.number_input(
                            "Qty Acquired",
                            min_value=1,
                            value=1,
                            step=1,
                            key=f"assign_lot_qty_{selected_product.id}",
                        )
                    with la2:
                        assign_unit_cost = st.number_input(
                            "Unit Cost",
                            min_value=0.0,
                            value=float(selected_product.acquisition_cost or 0),
                            step=0.01,
                            key=f"assign_lot_unit_cost_{selected_product.id}",
                        )
                    with la3:
                        assign_unit_tax = st.number_input(
                            "Unit Tax Paid",
                            min_value=0.0,
                            value=0.0,
                            step=0.01,
                            key=f"assign_lot_unit_tax_{selected_product.id}",
                        )
                    with la4:
                        assign_acquired_date = st.date_input(
                            "Acquired Date",
                            value=utc_today(),
                            key=f"assign_lot_acquired_date_{selected_product.id}",
                        )
                    lb1, lb2 = st.columns(2)
                    with lb1:
                        assign_allocated_cost = st.number_input(
                            "Allocated Lot Cost Total",
                            min_value=0.0,
                            value=0.0,
                            step=0.01,
                            key=f"assign_lot_allocated_cost_{selected_product.id}",
                            help="Optional total dollar share for this product/quantity in a mixed lot.",
                        )
                    with lb2:
                        assign_allocation_weight = st.number_input(
                            "Allocation Weight",
                            min_value=0.0,
                            value=0.0,
                            step=0.01,
                            key=f"assign_lot_allocation_weight_{selected_product.id}",
                            help="Optional proportional share used to split whole-lot cost across mixed products.",
                        )
                    assign_submit = st.form_submit_button("Assign To Lot")

                if assign_submit:
                    if not ensure_permission(user, "update", "Assign Product To Lot"):
                        st.stop()
                    chosen_lot_id = side_lot_options.get(assign_lot_key)
                    if not chosen_lot_id:
                        st.error("Select a purchase lot to assign.")
                    else:
                        try:
                            repo.assign_product_to_lot(
                                product_id=int(selected_product.id),
                                lot_id=int(chosen_lot_id),
                                quantity_acquired=int(assign_qty),
                                unit_cost=to_decimal_or_none(assign_unit_cost),
                                unit_tax_paid=to_decimal_or_none(assign_unit_tax),
                                allocated_cost=to_decimal_or_none(assign_allocated_cost),
                                allocation_weight=to_decimal_or_none(assign_allocation_weight),
                                acquired_at=datetime.combine(assign_acquired_date, datetime.min.time()),
                            )
                            st.success("Product linked to purchase lot.")
                            st.rerun()
                        except (ValueError, ValidationError, IntegrityError) as exc:
                            repo.db.rollback()
                            st.error(str(exc))

            st.markdown("#### Convert Inventory To New SKU")
            st.caption(
                "Use when raw material/supply inventory should become a different sellable SKU "
                "(example: silver shot -> multiple small bars/rounds)."
            )
            with st.form(f"product_convert_form_{selected_product.id}"):
                cv1, cv2 = st.columns(2)
                with cv1:
                    convert_source_qty = st.number_input(
                        "Source Quantity Used",
                        min_value=1,
                        value=1,
                        step=1,
                        key=f"convert_source_qty_{selected_product.id}",
                    )
                    convert_target_qty = st.number_input(
                        "Target Quantity Created",
                        min_value=1,
                        value=1,
                        step=1,
                        key=f"convert_target_qty_{selected_product.id}",
                    )
                    convert_target_sku = st.text_input(
                        "Target SKU",
                        value=generate_sku(str(selected_product.category or "other"), str(selected_product.metal_type or "mixed")),
                        key=f"convert_target_sku_{selected_product.id}",
                    )
                    convert_target_title = st.text_input(
                        "Target Title",
                        value=f"{selected_product.title} (Converted)",
                        key=f"convert_target_title_{selected_product.id}",
                    )
                with cv2:
                    convert_target_category = st.selectbox(
                        "Target Category",
                        ["bullion", "coins", "collectibles", "antiques", "other"],
                        index=["bullion", "coins", "collectibles", "antiques", "other"].index(
                            str(selected_product.category or "other")
                        )
                        if str(selected_product.category or "other")
                        in {"bullion", "coins", "collectibles", "antiques", "other"}
                        else 4,
                        key=f"convert_target_category_{selected_product.id}",
                    )
                    convert_target_inventory_class = st.selectbox(
                        "Target Inventory Class",
                        options=inventory_classes,
                        index=0,
                        key=f"convert_target_inventory_class_{selected_product.id}",
                    )
                    convert_target_weight_oz = st.number_input(
                        "Target Unit Weight (oz)",
                        min_value=0.0,
                        value=float(selected_product.weight_oz or 0),
                        step=0.01,
                        key=f"convert_target_weight_oz_{selected_product.id}",
                    )
                    convert_lot_key = st.selectbox(
                        "Purchase Lot (Optional)",
                        options=list(side_lot_options.keys()),
                        key=f"convert_lot_{selected_product.id}",
                    )
                convert_manual_cost = st.checkbox(
                    "Override Target Unit Cost",
                    value=False,
                    key=f"convert_manual_cost_{selected_product.id}",
                )
                convert_target_unit_cost = st.number_input(
                    "Target Unit Cost",
                    min_value=0.0,
                    value=float(
                        (
                            (
                                float(
                                    (
                                        selected_product.product_cost
                                        if selected_product.product_cost is not None
                                        else (selected_product.acquisition_cost or 0)
                                    )
                                )
                                * int(convert_source_qty)
                            )
                            / max(1, int(convert_target_qty))
                        )
                    ),
                    step=0.01,
                    key=f"convert_target_unit_cost_{selected_product.id}",
                    disabled=not convert_manual_cost,
                )
                convert_target_description = st.text_area(
                    "Target Description",
                    value=selected_product.description or "",
                    key=f"convert_target_description_{selected_product.id}",
                )
                convert_notes = st.text_area(
                    "Conversion Notes",
                    value="",
                    key=f"convert_notes_{selected_product.id}",
                )
                convert_submit = st.form_submit_button("Convert To New Product")

            if convert_submit:
                if not ensure_permission(user, "create", "Convert Inventory To New Product"):
                    st.stop()
                try:
                    created_target = repo.convert_inventory_to_product(
                        source_product_id=int(selected_product.id),
                        source_quantity_used=int(convert_source_qty),
                        target_sku=convert_target_sku.strip(),
                        target_title=convert_target_title.strip(),
                        target_category=convert_target_category,
                        target_inventory_class=convert_target_inventory_class,
                        target_description=convert_target_description.strip(),
                        target_metal_type=str(selected_product.metal_type or "").strip(),
                        target_weight_oz=to_decimal_or_none(convert_target_weight_oz),
                        target_quantity_created=int(convert_target_qty),
                        target_unit_cost=(
                            to_decimal_or_none(convert_target_unit_cost) if bool(convert_manual_cost) else None
                        ),
                        acquired_at=utcnow_naive(),
                        lot_id=side_lot_options.get(convert_lot_key),
                        notes=convert_notes.strip(),
                        actor=user.username,
                    )
                    st.success(
                        f"Converted inventory from #{selected_product.id} to new product "
                        f"#{created_target.id} ({created_target.sku})."
                    )
                    st.rerun()
                except (ValueError, ValidationError, IntegrityError) as exc:
                    repo.db.rollback()
                    st.error(str(exc))

            st.markdown("#### Bulk Multi-Target Conversion")
            st.caption(
                "Convert one source inventory quantity into multiple target SKUs in one operation "
                "(example: 20 oz shot -> 10x 1oz bars + 20x 0.5oz rounds)."
            )
            with st.form(f"product_bulk_convert_form_{selected_product.id}"):
                bcv1, bcv2, bcv3 = st.columns(3)
                with bcv1:
                    bulk_source_qty = st.number_input(
                        "Total Source Quantity Used",
                        min_value=1,
                        value=1,
                        step=1,
                        key=f"bulk_convert_source_qty_{selected_product.id}",
                    )
                with bcv2:
                    bulk_target_count = st.number_input(
                        "Number of Target SKUs",
                        min_value=2,
                        max_value=10,
                        value=2,
                        step=1,
                        key=f"bulk_convert_target_count_{selected_product.id}",
                    )
                with bcv3:
                    bulk_lot_key = st.selectbox(
                        "Purchase Lot (Optional)",
                        options=list(side_lot_options.keys()),
                        key=f"bulk_convert_lot_{selected_product.id}",
                    )
                bulk_targets: list[dict[str, object]] = []
                for idx in range(int(bulk_target_count)):
                    st.markdown(f"Target {idx + 1}")
                    t1, t2, t3, t4 = st.columns(4)
                    with t1:
                        t_sku = st.text_input(
                            "SKU",
                            value=generate_sku(
                                str(selected_product.category or "other"),
                                str(selected_product.metal_type or "mixed"),
                            ),
                            key=f"bulk_convert_sku_{selected_product.id}_{idx}",
                        )
                    with t2:
                        t_title = st.text_input(
                            "Title",
                            value=f"{selected_product.title} (Part {idx + 1})",
                            key=f"bulk_convert_title_{selected_product.id}_{idx}",
                        )
                    with t3:
                        t_qty = st.number_input(
                            "Quantity Created",
                            min_value=1,
                            value=1,
                            step=1,
                            key=f"bulk_convert_qty_{selected_product.id}_{idx}",
                        )
                    with t4:
                        t_category = st.selectbox(
                            "Category",
                            ["bullion", "coins", "collectibles", "antiques", "other"],
                            index=["bullion", "coins", "collectibles", "antiques", "other"].index(
                                str(selected_product.category or "other")
                            )
                            if str(selected_product.category or "other")
                            in {"bullion", "coins", "collectibles", "antiques", "other"}
                            else 4,
                            key=f"bulk_convert_category_{selected_product.id}_{idx}",
                        )
                    t5, t6, t7 = st.columns(3)
                    with t5:
                        t_inventory_class = st.selectbox(
                            "Inventory Class",
                            options=inventory_classes,
                            index=0,
                            key=f"bulk_convert_inventory_class_{selected_product.id}_{idx}",
                        )
                    with t6:
                        t_weight_oz = st.number_input(
                            "Unit Weight (oz)",
                            min_value=0.0,
                            value=float(selected_product.weight_oz or 0),
                            step=0.01,
                            key=f"bulk_convert_weight_{selected_product.id}_{idx}",
                        )
                    with t7:
                        t_use_manual_cost = st.checkbox(
                            "Manual Unit Cost",
                            value=False,
                            key=f"bulk_convert_use_manual_cost_{selected_product.id}_{idx}",
                        )
                    t_unit_cost = st.number_input(
                        "Unit Cost",
                        min_value=0.0,
                        value=float(
                            selected_product.product_cost
                            if selected_product.product_cost is not None
                            else (selected_product.acquisition_cost or 0)
                        ),
                        step=0.01,
                        key=f"bulk_convert_unit_cost_{selected_product.id}_{idx}",
                        disabled=not t_use_manual_cost,
                    )
                    t_description = st.text_area(
                        "Description",
                        value=selected_product.description or "",
                        key=f"bulk_convert_description_{selected_product.id}_{idx}",
                    )
                    bulk_targets.append(
                        {
                            "sku": t_sku.strip(),
                            "title": t_title.strip(),
                            "category": t_category,
                            "inventory_class": t_inventory_class,
                            "description": t_description.strip(),
                            "metal_type": str(selected_product.metal_type or "").strip(),
                            "quantity_created": int(t_qty),
                            "weight_oz": to_decimal_or_none(t_weight_oz),
                            "unit_cost": (to_decimal_or_none(t_unit_cost) if t_use_manual_cost else None),
                        }
                    )
                    st.divider()
                bulk_notes = st.text_area(
                    "Bulk Conversion Notes",
                    value="",
                    key=f"bulk_convert_notes_{selected_product.id}",
                )
                bulk_convert_submit = st.form_submit_button("Run Bulk Conversion")

            if bulk_convert_submit:
                if not ensure_permission(user, "create", "Run Bulk Product Conversion"):
                    st.stop()
                try:
                    created_targets = repo.convert_inventory_to_multiple_products(
                        source_product_id=int(selected_product.id),
                        source_quantity_used=int(bulk_source_qty),
                        targets=bulk_targets,
                        acquired_at=utcnow_naive(),
                        lot_id=side_lot_options.get(bulk_lot_key),
                        notes=bulk_notes.strip(),
                        actor=user.username,
                    )
                    st.success(
                        f"Bulk conversion complete. Created {len(created_targets)} products: "
                        + ", ".join(f"#{int(p.id)} {p.sku}" for p in created_targets)
                    )
                    st.rerun()
                except (ValueError, ValidationError, IntegrityError) as exc:
                    repo.db.rollback()
                    st.error(str(exc))

            st.markdown("#### Quick eBay Draft (Smart)")
            linked_coin_ref = None
            if selected_product.coin_reference_id is not None:
                linked_coin_ref = next(
                    (row for row in coin_refs if int(row.id) == int(selected_product.coin_reference_id)),
                    None,
                )
            if linked_coin_ref is not None:
                st.caption(f"Using linked coin reference: {_coin_reference_summary(linked_coin_ref)}")
            qe1, qe2, qe3 = st.columns(3)
            with qe1:
                quick_price_markup_pct = st.number_input(
                    "Markup %",
                    min_value=0.0,
                    value=20.0,
                    step=1.0,
                    key=f"quick_ebay_markup_{selected_product.id}",
                    help="Applied over acquisition cost for draft price estimate.",
                )
            with qe2:
                quick_qty = st.number_input(
                    "Draft Qty",
                    min_value=1,
                    value=max(1, int(selected_product.current_quantity or 1)),
                    step=1,
                    key=f"quick_ebay_qty_{selected_product.id}",
                )
            with qe3:
                include_ai_notes = st.checkbox(
                    "Include AI Notes",
                    value=True,
                    key=f"quick_ebay_include_ai_{selected_product.id}",
                )
            attach_unassigned_media = st.checkbox(
                "Attach unassigned product media to this new listing",
                value=True,
                key=f"quick_ebay_attach_media_{selected_product.id}",
            )
            quick_create_draft = st.button(
                "Create Smart eBay Draft",
                key=f"quick_ebay_create_btn_{selected_product.id}",
            )
            if quick_create_draft:
                if not ensure_permission(user, "create", "Create Smart eBay Draft"):
                    st.stop()
                try:
                    acquisition_cost = float(selected_product.acquisition_cost or 0.0)
                    suggested_price = round(acquisition_cost * (1.0 + (float(quick_price_markup_pct) / 100.0)), 2)
                    if suggested_price <= 0:
                        suggested_price = 0.01
                    year_text = ""
                    if linked_coin_ref is not None:
                        if linked_coin_ref.year_start and linked_coin_ref.year_end:
                            year_text = f"{int(linked_coin_ref.year_start)}-{int(linked_coin_ref.year_end)}"
                        elif linked_coin_ref.year_start:
                            year_text = str(int(linked_coin_ref.year_start))
                    quick_title = selected_product.title.strip()
                    if linked_coin_ref is not None:
                        quick_title = " ".join(
                            [
                                str(linked_coin_ref.coin_name or "").strip(),
                                f"({year_text})" if year_text else "",
                                str(linked_coin_ref.denomination or "").strip(),
                            ]
                        ).strip() or quick_title
                    detail_sections: list[str] = []
                    if linked_coin_ref is not None:
                        detail_sections.append(
                            "\n".join(
                                [
                                    "Coin Reference Context:",
                                    f"- Name: {linked_coin_ref.coin_name}",
                                    f"- Country: {linked_coin_ref.country}",
                                    f"- Series: {linked_coin_ref.series}",
                                    f"- Denomination: {linked_coin_ref.denomination}",
                                    f"- Metal: {linked_coin_ref.metal_type}",
                                    f"- KM: {linked_coin_ref.km_number}",
                                    f"- PCGS: {linked_coin_ref.pcgs_no}",
                                    f"- NGC: {linked_coin_ref.ngc_id}",
                                ]
                            ).strip()
                        )
                    if include_ai_notes:
                        ai_desc = str(getattr(selected_product, "ai_description", "") or "").strip()
                        ai_grade = str(getattr(selected_product, "ai_grading_description", "") or "").strip()
                        if ai_desc:
                            detail_sections.append(f"AI Description:\n{ai_desc}")
                        if ai_grade:
                            detail_sections.append(f"AI Grading Notes:\n{ai_grade}")
                    detail_sections.append(
                        json.dumps(
                            {
                                "source": "product_side_panel_quick_draft",
                                "product_id": int(selected_product.id),
                                "coin_reference_id": int(selected_product.coin_reference_id)
                                if selected_product.coin_reference_id is not None
                                else None,
                                "created_by": user.username,
                            },
                            indent=2,
                        )
                    )
                    created_listing = repo.create_listing(
                        product_id=int(selected_product.id),
                        marketplace="ebay",
                        listing_title=quick_title,
                        listing_price=to_decimal_or_none(suggested_price) or to_decimal_or_none(0.01),
                        quantity_listed=int(quick_qty),
                        external_listing_id="",
                        marketplace_url="",
                        marketplace_details="\n\n".join([d for d in detail_sections if d]).strip(),
                        listing_status="draft",
                        listed_at=utcnow_naive(),
                        actor=user.username,
                    )
                    attached = 0
                    if attach_unassigned_media:
                        product_media_rows = repo.list_media_assets_for_product(int(selected_product.id))
                        for media in product_media_rows:
                            if media.listing_id is not None:
                                continue
                            repo.update_media_asset(
                                int(media.id),
                                {"listing_id": int(created_listing.id)},
                                actor=user.username,
                            )
                            attached += 1
                    st.success(
                        f"Created smart eBay draft listing #{created_listing.id} from product #{selected_product.id}. "
                        f"Attached media: {attached}."
                    )
                    st.rerun()
                except (ValueError, ValidationError, IntegrityError) as exc:
                    repo.db.rollback()
                    st.error(str(exc))

            st.markdown("#### Repurchase / Restock Existing SKU")
            with st.form(f"product_repurchase_form_{selected_product.id}"):
                rp1, rp2, rp3 = st.columns(3)
                with rp1:
                    repurchase_qty = st.number_input(
                        "Repurchase Quantity",
                        min_value=1,
                        value=1,
                        step=1,
                        key=f"repurchase_qty_{selected_product.id}",
                    )
                    repurchase_unit_cost = st.number_input(
                        "Repurchase Unit Cost",
                        min_value=0.0,
                        value=float(selected_product.acquisition_cost or 0),
                        step=0.01,
                        key=f"repurchase_unit_cost_{selected_product.id}",
                    )
                    repurchase_unit_product_cost = st.number_input(
                        "Repurchase Unit Product Cost",
                        min_value=0.0,
                        value=float(getattr(selected_product, "product_cost", 0) or 0),
                        step=0.01,
                        key=f"repurchase_unit_product_cost_{selected_product.id}",
                        help="Unit direct product cost used in margin calculations.",
                    )
                with rp2:
                    repurchase_unit_tax = st.number_input(
                        "Repurchase Unit Tax Paid",
                        min_value=0.0,
                        value=float(getattr(selected_product, "acquisition_tax_paid", 0) or 0),
                        step=0.01,
                        key=f"repurchase_unit_tax_{selected_product.id}",
                    )
                    repurchase_unit_shipping = st.number_input(
                        "Repurchase Unit Shipping Paid",
                        min_value=0.0,
                        value=float(getattr(selected_product, "acquisition_shipping_paid", 0) or 0),
                        step=0.01,
                        key=f"repurchase_unit_shipping_{selected_product.id}",
                    )
                    repurchase_unit_handling = st.number_input(
                        "Repurchase Unit Handling Paid",
                        min_value=0.0,
                        value=float(getattr(selected_product, "acquisition_handling_paid", 0) or 0),
                        step=0.01,
                        key=f"repurchase_unit_handling_{selected_product.id}",
                    )
                with rp3:
                    repurchase_date = st.date_input(
                        "Repurchase Date",
                        value=utc_today(),
                        key=f"repurchase_date_{selected_product.id}",
                    )
                    repurchase_lot_key = st.selectbox(
                        "Purchase Lot (Optional)",
                        options=list(side_lot_options.keys()),
                        key=f"repurchase_lot_{selected_product.id}",
                    )
                    repurchase_doc_kind = st.selectbox(
                        "Repurchase Document Kind",
                        options=["incoming_invoice", "purchase_order", "receipt", "other"],
                        key=f"repurchase_doc_kind_{selected_product.id}",
                    )
                    repurchase_doc_file = st.file_uploader(
                        "Attach Repurchase Invoice/Receipt (optional)",
                        type=["pdf", "png", "jpg", "jpeg", "webp"],
                        key=f"repurchase_doc_file_{selected_product.id}",
                    )
                repurchase_notes = st.text_area(
                    "Repurchase Notes",
                    value="",
                    key=f"repurchase_notes_{selected_product.id}",
                )
                repurchase_submit = st.form_submit_button("Record Repurchase")

            if repurchase_submit:
                if not ensure_permission(user, "update", "Record Product Repurchase"):
                    st.stop()
                try:
                    repo.record_product_repurchase(
                        product_id=selected_product.id,
                        quantity_acquired=int(repurchase_qty),
                        unit_cost=to_decimal_or_none(repurchase_unit_cost),
                        unit_tax_paid=to_decimal_or_none(repurchase_unit_tax),
                        unit_shipping_paid=to_decimal_or_none(repurchase_unit_shipping),
                        unit_handling_paid=to_decimal_or_none(repurchase_unit_handling),
                        unit_product_cost=to_decimal_or_none(repurchase_unit_product_cost),
                        acquired_at=datetime.combine(repurchase_date, datetime.min.time()),
                        lot_id=side_lot_options.get(repurchase_lot_key),
                        notes=repurchase_notes.strip(),
                        actor=user.username,
                    )
                    repurchase_doc_message = ""
                    if repurchase_doc_file is not None:
                        if not storage.enabled:
                            repurchase_doc_message = " Repurchase recorded, but document upload skipped (S3 not configured)."
                        else:
                            file_name = str(getattr(repurchase_doc_file, "name", "") or "").strip() or "repurchase_document.bin"
                            file_bytes = bytes(repurchase_doc_file.getvalue() or b"")
                            if not file_bytes:
                                repurchase_doc_message = " Repurchase recorded, but attached document was empty."
                            else:
                                content_type = str(getattr(repurchase_doc_file, "type", "") or "").strip() or "application/octet-stream"
                                upload_result = storage.upload_file(
                                    file_name=file_name,
                                    file_bytes=file_bytes,
                                    content_type=content_type,
                                )
                                created_doc = repo.create_purchase_document(
                                    document_kind=str(repurchase_doc_kind or "incoming_invoice").strip().lower() or "incoming_invoice",
                                    title=f"Repurchase {selected_product.sku} {repurchase_date.isoformat()}",
                                    original_filename=file_name,
                                    content_type=content_type,
                                    size_bytes=len(file_bytes),
                                    content_sha256=hashlib.sha256(file_bytes).hexdigest(),
                                    s3_bucket=upload_result.bucket,
                                    s3_key=upload_result.key,
                                    s3_url=upload_result.url,
                                    lot_id=side_lot_options.get(repurchase_lot_key),
                                    product_id=int(selected_product.id),
                                    ai_extracted_json="{}",
                                    ai_summary="",
                                    uploaded_by=user.username,
                                    actor=user.username,
                                )
                                repurchase_doc_message = f" Attached purchase document #{int(created_doc.id)}."
                    st.success("Repurchase recorded with lot/movement tracking." + repurchase_doc_message)
                    st.rerun()
                except (ValueError, ValidationError, IntegrityError) as exc:
                    repo.db.rollback()
                    st.error(str(exc))

            st.markdown("#### Batch Buy/Sell Operator (Repeated SKU Cycles)")
            batch_ref_key = f"product_cycle_batch_ref_{selected_product.id}"
            if batch_ref_key not in st.session_state:
                st.session_state[batch_ref_key] = (
                    f"BATCH-{selected_product.sku}-{utcnow_naive().strftime('%Y%m%d-%H%M%S')}"
                )
            batch_ref = st.text_input(
                "Batch Reference",
                key=batch_ref_key,
                help="Shared reference for linking fast receive/sell operations for this SKU cycle.",
            ).strip()
            op_receive_tab, op_sell_tab = st.tabs(["Batch Receive", "Batch Sale"])

            with op_receive_tab:
                with st.form(f"product_batch_receive_form_{selected_product.id}"):
                    br1, br2 = st.columns(2)
                    with br1:
                        batch_receive_qty = st.number_input(
                            "Receive Quantity",
                            min_value=1,
                            value=1,
                            step=1,
                            key=f"batch_receive_qty_{selected_product.id}",
                        )
                        batch_receive_unit_cost = st.number_input(
                            "Receive Unit Cost",
                            min_value=0.0,
                            value=float(selected_product.acquisition_cost or 0),
                            step=0.01,
                            key=f"batch_receive_unit_cost_{selected_product.id}",
                        )
                    with br2:
                        batch_receive_date = st.date_input(
                            "Receive Date",
                            value=utc_today(),
                            key=f"batch_receive_date_{selected_product.id}",
                        )
                        batch_receive_lot_key = st.selectbox(
                            "Purchase Lot (Optional)",
                            options=list(side_lot_options.keys()),
                            key=f"batch_receive_lot_{selected_product.id}",
                        )
                    batch_receive_submit = st.form_submit_button("Record Batch Receive")

                if batch_receive_submit:
                    if not ensure_permission(user, "update", "Batch Receive SKU"):
                        st.stop()
                    try:
                        notes = f"[batch_ref:{batch_ref}] operator receive"
                        repo.record_product_repurchase(
                            product_id=selected_product.id,
                            quantity_acquired=int(batch_receive_qty),
                            unit_cost=to_decimal_or_none(batch_receive_unit_cost),
                            unit_tax_paid=to_decimal_or_none(float(getattr(selected_product, "acquisition_tax_paid", 0) or 0)),
                            unit_shipping_paid=to_decimal_or_none(float(getattr(selected_product, "acquisition_shipping_paid", 0) or 0)),
                            unit_handling_paid=to_decimal_or_none(float(getattr(selected_product, "acquisition_handling_paid", 0) or 0)),
                            unit_product_cost=to_decimal_or_none(float(getattr(selected_product, "product_cost", 0) or 0)),
                            acquired_at=datetime.combine(batch_receive_date, datetime.min.time()),
                            lot_id=side_lot_options.get(batch_receive_lot_key),
                            notes=notes,
                            actor=user.username,
                        )
                        st.success("Batch receive recorded.")
                        st.rerun()
                    except (ValueError, ValidationError, IntegrityError) as exc:
                        repo.db.rollback()
                        st.error(str(exc))

            with op_sell_tab:
                listing_rows_for_product = [
                    row for row in repo.list_listings() if int(row.product_id or 0) == int(selected_product.id)
                ]
                listing_options = {"None": None}
                listing_options.update(
                    {
                        f"#{row.id} | {(row.marketplace or '').upper()} | {row.listing_title}": row.id
                        for row in listing_rows_for_product
                    }
                )
                with st.form(f"product_batch_sale_form_{selected_product.id}"):
                    bs1, bs2, bs3 = st.columns(3)
                    with bs1:
                        batch_sale_marketplace = st.selectbox(
                            "Marketplace",
                            ["ebay", "facebook", "craigslist", "whatnot", "shopify", "other"],
                            key=f"batch_sale_marketplace_{selected_product.id}",
                        )
                        batch_sale_qty = st.number_input(
                            "Quantity Sold",
                            min_value=1,
                            value=1,
                            step=1,
                            key=f"batch_sale_qty_{selected_product.id}",
                        )
                    with bs2:
                        batch_sale_gross = st.number_input(
                            "Gross Sale Amount",
                            min_value=0.0,
                            value=0.0,
                            step=0.01,
                            key=f"batch_sale_gross_{selected_product.id}",
                        )
                        batch_sale_fees = st.number_input(
                            "Fees",
                            min_value=0.0,
                            value=0.0,
                            step=0.01,
                            key=f"batch_sale_fees_{selected_product.id}",
                        )
                    with bs3:
                        batch_sale_shipping = st.number_input(
                            "Shipping Cost",
                            min_value=0.0,
                            value=0.0,
                            step=0.01,
                            key=f"batch_sale_shipping_{selected_product.id}",
                        )
                        batch_sale_date = st.date_input(
                            "Sold Date",
                            value=utc_today(),
                            key=f"batch_sale_date_{selected_product.id}",
                        )
                    batch_sale_external_order = st.text_input(
                        "External Order ID (Optional)",
                        key=f"batch_sale_external_order_{selected_product.id}",
                        help="If empty, batch reference is used for internal linkage.",
                    )
                    batch_sale_listing_key = st.selectbox(
                        "Link to Listing (Optional)",
                        options=list(listing_options.keys()),
                        key=f"batch_sale_listing_{selected_product.id}",
                    )
                    batch_sale_submit = st.form_submit_button("Record Batch Sale")

                if batch_sale_submit:
                    if not ensure_permission(user, "create", "Batch Sale SKU"):
                        st.stop()
                    on_hand = int(selected_product.current_quantity or 0)
                    if int(batch_sale_qty) > on_hand:
                        st.error(f"Cannot sell {int(batch_sale_qty)}; only {on_hand} on hand.")
                    else:
                        try:
                            repo.create_sale(
                                marketplace=batch_sale_marketplace.strip().lower(),
                                sold_price=to_decimal_or_none(batch_sale_gross) or to_decimal_or_none(0),
                                fees=to_decimal_or_none(batch_sale_fees) or to_decimal_or_none(0),
                                shipping_cost=to_decimal_or_none(batch_sale_shipping) or to_decimal_or_none(0),
                                quantity_sold=int(batch_sale_qty),
                                product_id=selected_product.id,
                                listing_id=listing_options.get(batch_sale_listing_key),
                                external_order_id=(
                                    (batch_sale_external_order or "").strip() or batch_ref
                                ),
                                sold_at=datetime.combine(batch_sale_date, datetime.min.time()),
                                actor=user.username,
                            )
                            st.success("Batch sale recorded and inventory decremented.")
                            st.rerun()
                        except (ValueError, ValidationError, IntegrityError) as exc:
                            repo.db.rollback()
                            st.error(str(exc))

            st.markdown("#### Inventory Lifecycle Snapshot")
            product_assignments = [
                row for row in repo.list_product_lot_assignments() if int(row.product_id) == int(selected_product.id)
            ]
            product_sales = [row for row in repo.list_sales() if (row.product_id or 0) == selected_product.id]
            product_movements = [
                row
                for row in repo.list_inventory_movements(limit=5000)
                if (row.product_id or 0) == selected_product.id
            ]
            total_acquired = sum(int(row.quantity_acquired or 0) for row in product_assignments)
            total_sold = sum(int(row.quantity_sold or 0) for row in product_sales)
            avg_unit_cost = (
                (
                    sum(float(row.unit_cost or 0) * int(row.quantity_acquired or 0) for row in product_assignments)
                    / total_acquired
                )
                if total_acquired > 0
                else 0.0
            )
            avg_unit_landed_cost = (
                (
                    sum(
                        (
                            float(row.unit_cost or 0)
                            + float(getattr(row, "unit_tax_paid", 0) or 0)
                            + float(getattr(row, "unit_shipping_paid", 0) or 0)
                            + float(getattr(row, "unit_handling_paid", 0) or 0)
                        )
                        * int(row.quantity_acquired or 0)
                        for row in product_assignments
                    )
                    / total_acquired
                )
                if total_acquired > 0
                else 0.0
            )
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Acquired", total_acquired)
            m2.metric("Total Sold", total_sold)
            m3.metric("On Hand", int(selected_product.current_quantity or 0))
            m4.metric("Avg Buy Cost", f"${avg_unit_cost:,.2f}")
            st.caption(f"Avg Landed Cost (cost+tax+shipping+handling): `${avg_unit_landed_cost:,.2f}`")

            if product_assignments:
                st.caption("Recent Lot Assignments")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "assignment_id": row.id,
                                "lot_id": row.lot_id,
                                "qty_acquired": row.quantity_acquired,
                                "unit_cost": float(row.unit_cost) if row.unit_cost is not None else None,
                                "unit_tax_paid": float(row.unit_tax_paid) if getattr(row, "unit_tax_paid", None) is not None else None,
                                "unit_shipping_paid": float(row.unit_shipping_paid) if getattr(row, "unit_shipping_paid", None) is not None else None,
                                "unit_handling_paid": float(row.unit_handling_paid) if getattr(row, "unit_handling_paid", None) is not None else None,
                                "allocated_cost": float(row.allocated_cost) if row.allocated_cost is not None else None,
                                "allocation_weight": float(row.allocation_weight) if getattr(row, "allocation_weight", None) is not None else None,
                                "allocated_tax_paid": float(row.allocated_tax_paid) if getattr(row, "allocated_tax_paid", None) is not None else None,
                                "allocated_shipping_paid": float(row.allocated_shipping_paid) if getattr(row, "allocated_shipping_paid", None) is not None else None,
                                "allocated_handling_paid": float(row.allocated_handling_paid) if getattr(row, "allocated_handling_paid", None) is not None else None,
                                "acquired_at": iso_or_none(row.acquired_at),
                            }
                            for row in product_assignments[:15]
                        ]
                    ),
                    use_container_width=True,
                )
            if product_movements:
                st.caption("Recent Inventory Movements")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "movement_id": row.id,
                                "movement_type": row.movement_type,
                                "qty_delta": row.quantity_delta,
                                "before": row.quantity_before,
                                "after": row.quantity_after,
                                "unit_cost": float(row.unit_cost) if row.unit_cost is not None else None,
                                "reference_type": row.reference_type,
                                "reference_id": row.reference_id,
                                "occurred_at": iso_or_none(row.occurred_at),
                            }
                            for row in product_movements[:20]
                        ]
                    ),
                    use_container_width=True,
                )
    st.markdown("### Product Media Manager")
    product_map = {f"{p.sku} | {p.title} | #{p.id}": p for p in products}
    selected_product_key = st.selectbox("Choose Product", list(product_map.keys()), key="manage_product_key")
    selected_product = product_map[selected_product_key]

    media_uploaded_by = st.text_input("Uploaded By", value="employee", key="product_media_by")
    more_files = render_media_capture_inputs(
        key_prefix="manage_product_media",
        upload_label="Add More Photos/Videos",
        allow_enhanced=True,
    )
    submit_media = st.button("Upload Media To Product", key="product_media_upload_submit")

    if submit_media:
        if not ensure_permission(user, "create", "Upload Product Media"):
            st.stop()
        if not more_files:
            st.error("Select at least one file.")
        elif not storage.enabled:
            st.error("S3 storage is not configured.")
        else:
            uploaded, errors = upload_media_for_listing(
                repo=repo,
                storage=storage,
                listing_id=None,
                product_id=selected_product.id,
                uploaded_files=more_files,
                uploaded_by=media_uploaded_by,
            )
            if uploaded:
                st.success(f"Uploaded {uploaded} media file(s).")
            for error in errors:
                st.error(f"Upload failed: {error}")

    product_media = repo.list_media_assets_for_product(selected_product.id)
    if not product_media:
        st.info("No media currently attached to this product.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": m.id,
                        "type": m.media_type,
                        "filename": m.original_filename,
                        "content_type": m.content_type,
                        "size_bytes": m.size_bytes,
                        "url": m.s3_url,
                    }
                    for m in product_media
                ]
            ),
            use_container_width=True,
        )
        render_media_gallery(
            product_media,
            section_title="Product Media Preview Gallery",
            columns=3,
            storage=storage,
        )
        render_media_file_actions(
            product_media,
            storage=storage,
            key_prefix=f"product_media_file_actions_{selected_product.id}",
            section_title="Product Media File Access",
            repo=repo,
            actor=user.username,
            user=user,
        )

    st.markdown("### Create eBay Listing From Product")
    source_product_key = st.selectbox(
        "Source Product",
        list(product_map.keys()),
        key="create_ebay_listing_source_product",
    )
    source_product = product_map[source_product_key]
    source_product_media = repo.list_media_assets_for_product(source_product.id)
    source_media_map = {
        f"#{m.id} | {m.media_type} | {m.original_filename}": m for m in source_product_media
    }

    with st.form("create_ebay_listing_from_product_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            listing_title = st.text_input("Listing Title", value=source_product.title)
        with c2:
            listing_price = st.number_input(
                "Listing Price",
                min_value=0.0,
                value=float(source_product.acquisition_cost or 0.0),
                step=1.0,
            )
        with c3:
            listing_qty = st.number_input(
                "Quantity Listed",
                min_value=1,
                value=max(1, int(source_product.current_quantity or 1)),
                step=1,
            )
        d1, d2 = st.columns(2)
        with d1:
            listing_status = st.selectbox("Listing Status", ["draft", "active", "ended", "sold"], index=0)
        with d2:
            listed_date = st.date_input("Listed Date", value=utc_today(), key="create_ebay_listed_date")
        external_listing_id = st.text_input("External Listing ID (optional)")
        marketplace_url = st.text_input("Marketplace URL (optional)")
        marketplace_details = st.text_area("Marketplace Details (optional)")

        if source_media_map:
            selected_media_labels = st.multiselect(
                "Attach Product Media",
                list(source_media_map.keys()),
                help="Selected media assets will be linked to the newly created listing.",
            )
            allow_reassign = st.checkbox(
                "Allow Reassigning Media Already Linked To Another Listing",
                value=False,
            )
        else:
            st.info("No product media available to attach.")
            selected_media_labels = []
            allow_reassign = False

        submit_create_from_product = st.form_submit_button("Create eBay Listing")

    if submit_create_from_product:
        if not ensure_permission(user, "create", "Create Listing"):
            st.stop()
        if not listing_title.strip():
            st.error("Listing title is required.")
        else:
            try:
                price_value = to_decimal_or_none(listing_price)
                if price_value is None:
                    price_value = to_decimal_or_none(0.0)
                ValidationService.validate_listing_workflow(
                    listing_title=listing_title.strip(),
                    listing_price=price_value,
                    quantity_listed=int(listing_qty),
                    listing_status=listing_status,
                    media_count=len(selected_media_labels),
                    external_listing_id=external_listing_id.strip(),
                    marketplace_url=marketplace_url.strip(),
                )
                created_listing = repo.create_listing(
                    product_id=source_product.id,
                    marketplace="ebay",
                    listing_title=listing_title.strip(),
                    listing_price=price_value,
                    quantity_listed=int(listing_qty),
                    external_listing_id=external_listing_id.strip(),
                    marketplace_url=marketplace_url.strip(),
                    marketplace_details=marketplace_details.strip(),
                    listing_status=listing_status,
                    listed_at=datetime.combine(listed_date, datetime.min.time()),
                    actor=user.username,
                )

                attached = 0
                skipped = 0
                for label in selected_media_labels:
                    media = source_media_map[label]
                    if (
                        media.listing_id is not None
                        and media.listing_id != created_listing.id
                        and not allow_reassign
                    ):
                        skipped += 1
                        continue
                    repo.update_media_asset(
                        media.id,
                        {"listing_id": created_listing.id},
                        actor=user.username,
                    )
                    attached += 1

                st.success(
                    f"Created eBay listing #{created_listing.id} from product #{source_product.id}. "
                    f"Media attached={attached}, skipped={skipped}."
                )
                st.rerun()
            except (ValueError, ValidationError) as exc:
                st.error(str(exc))

    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="products",
        section_title="Workspace Feedback: Products",
    )
