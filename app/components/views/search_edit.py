from datetime import datetime
import json

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import (
    build_listing_options,
    build_product_options,
    iso_or_none,
    key_for_value,
    to_decimal,
    to_decimal_or_none,
)
from app.components.views.shared import (
    MARKETPLACES,
    handoff_to_documents_draft,
    pretty_json,
    render_help_panel,
)
from app.repository import InventoryRepository
from app.services.ai_text import normalize_ai_text
from app.services.validation import ValidationService, ValidationError
from app.utils.time import utcnow_naive


def _build_lot_assignment_rows(assignments: list, product_index: dict[int, object]) -> dict[int, list[dict]]:
    lot_assignment_rows: dict[int, list[dict]] = {}
    for assignment in assignments:
        lot_id_val = int(assignment.lot_id)
        prod = product_index.get(int(assignment.product_id))
        lot_assignment_rows.setdefault(lot_id_val, []).append(
            {
                "assignment_id": int(assignment.id),
                "product_id": int(assignment.product_id),
                "sku": str(getattr(prod, "sku", "") or ""),
                "product_title": str(getattr(prod, "title", "") or ""),
                "quantity_acquired": int(assignment.quantity_acquired or 0),
                "unit_cost": float(assignment.unit_cost) if assignment.unit_cost is not None else None,
                "unit_tax_paid": float(assignment.unit_tax_paid) if assignment.unit_tax_paid is not None else None,
                "unit_shipping_paid": float(assignment.unit_shipping_paid) if assignment.unit_shipping_paid is not None else None,
                "unit_handling_paid": float(assignment.unit_handling_paid) if assignment.unit_handling_paid is not None else None,
                "allocated_cost": float(assignment.allocated_cost) if assignment.allocated_cost is not None else None,
                "allocation_weight": float(assignment.allocation_weight) if getattr(assignment, "allocation_weight", None) is not None else None,
                "allocated_tax_paid": float(assignment.allocated_tax_paid) if assignment.allocated_tax_paid is not None else None,
                "allocated_shipping_paid": float(assignment.allocated_shipping_paid) if assignment.allocated_shipping_paid is not None else None,
                "allocated_handling_paid": float(assignment.allocated_handling_paid) if assignment.allocated_handling_paid is not None else None,
                "acquired_at": iso_or_none(assignment.acquired_at),
            }
        )
    return lot_assignment_rows


def _build_lot_table_rows(filtered_lots: list, lot_assignment_rows: dict[int, list[dict]]) -> list[dict]:
    return [
        {
            "id": int(lot.id),
            "lot_code": str(lot.lot_code or ""),
            "source_id": lot.source_id,
            "source_name": (lot.source.name if lot.source else ""),
            "vendor": str(lot.vendor or ""),
            "purchase_date": iso_or_none(lot.purchase_date),
            "total_cost": float(lot.total_cost) if lot.total_cost is not None else None,
            "total_tax_paid": float(getattr(lot, "total_tax_paid", None))
            if getattr(lot, "total_tax_paid", None) is not None
            else None,
            "total_shipping_paid": float(getattr(lot, "total_shipping_paid", None))
            if getattr(lot, "total_shipping_paid", None) is not None
            else None,
            "total_handling_paid": float(getattr(lot, "total_handling_paid", None))
            if getattr(lot, "total_handling_paid", None) is not None
            else None,
            "expected_total_quantity": int(getattr(lot, "expected_total_quantity", 0) or 0) or None,
            "ebay_purchase": bool(getattr(lot, "ebay_purchase", False)),
            "ebay_purchase_item_id": str(getattr(lot, "ebay_purchase_item_id", "") or ""),
            "ebay_purchase_url": str(getattr(lot, "ebay_purchase_url", "") or ""),
            "archived": bool(_lot_is_archived(lot)),
            "attached_products_count": len(lot_assignment_rows.get(int(lot.id), [])),
            "attached_products": ", ".join(
                sorted(
                    {
                        str(row.get("sku") or "").strip() or f"product#{int(row.get('product_id') or 0)}"
                        for row in lot_assignment_rows.get(int(lot.id), [])
                    }
                )
            ),
            "notes": str(lot.notes or ""),
        }
        for lot in filtered_lots
    ]


def _validate_lot_update_inputs(lot_code: str, ebay_purchase: bool, ebay_purchase_item_id: str) -> str | None:
    if not str(lot_code or "").strip():
        return "Lot code is required."
    if bool(ebay_purchase) and not str(ebay_purchase_item_id or "").strip():
        return "eBay Purchase Item ID is required when Purchased On eBay is enabled."
    return None


def _listing_is_archived(listing) -> bool:
    raw = str(getattr(listing, "marketplace_details", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    lifecycle = parsed.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return bool(lifecycle.get("archived"))


def _lot_is_archived(lot) -> bool:
    raw = str(getattr(lot, "notes", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    lifecycle = parsed.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return bool(lifecycle.get("archived"))


def _build_lot_update_payload(
    *,
    source_id: int | None,
    lot_code: str,
    vendor: str,
    purchase_date,
    total_cost: float,
    total_tax_paid: float,
    total_shipping_paid: float,
    total_handling_paid: float,
    expected_total_quantity: int,
    ebay_purchase: bool,
    ebay_purchase_item_id: str,
    ebay_purchase_url: str,
    notes: str,
) -> dict:
    return {
        "lot_code": str(lot_code or "").strip(),
        "source_id": source_id,
        "vendor": str(vendor or "").strip(),
        "purchase_date": datetime.combine(purchase_date, datetime.min.time()),
        "total_cost": to_decimal_or_none(total_cost),
        "total_tax_paid": to_decimal_or_none(total_tax_paid),
        "total_shipping_paid": to_decimal_or_none(total_shipping_paid),
        "total_handling_paid": to_decimal_or_none(total_handling_paid),
        "expected_total_quantity": int(expected_total_quantity or 0) or None,
        "ebay_purchase": bool(ebay_purchase),
        "ebay_purchase_item_id": str(ebay_purchase_item_id or "").strip(),
        "ebay_purchase_url": str(ebay_purchase_url or "").strip(),
        "notes": str(notes or "").strip(),
    }


def render_search_edit(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Search, Edit, and Audit")
    st.caption("Search existing records, edit values, and review audit history.")
    render_help_panel(
        section_title="Search, Edit, and Audit",
        goal="Find existing records quickly, apply corrections, and keep change history visible.",
        steps=[
            "Use tab-specific search fields to narrow products, listings, sales, and media.",
            "Edit only verified fields and save changes to write audit logs with actor attribution.",
            "Review Audit Log tab for before/after style change details in JSON.",
            "Use this page for cleanup and corrections, not high-volume record creation.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )
    actor = user.username
    st.caption(f"Signed in as `{user.username}` ({user.role}). Update actions use this identity in audit logs.")

    se1, se2 = st.columns(2)
    with se1:
        search_edit_render_full_tables = st.checkbox(
            "Render Full Tables",
            value=False,
            key="search_edit_render_full_tables",
            help="When off, tables render as previews for faster page responsiveness.",
        )
    with se2:
        search_edit_preview_row_limit = st.number_input(
            "Preview Row Limit",
            min_value=10,
            max_value=500,
            value=100,
            step=10,
            key="search_edit_preview_row_limit",
            help="Rows shown per table when full-table rendering is disabled.",
        )

    _cache: dict[str, object] = {}

    def _get_cached(key: str, loader):
        if key not in _cache:
            _cache[key] = loader()
        return _cache[key]

    def _products():
        return _get_cached("products", repo.list_products)

    def _listings():
        return _get_cached("listings", repo.list_listings)

    def _sales():
        return _get_cached("sales", repo.list_sales)

    def _orders():
        return _get_cached("orders", repo.list_orders)

    def _order_items():
        return _get_cached("order_items", repo.list_order_items)

    def _lots():
        return _get_cached("lots", repo.list_purchase_lots)

    def _sources():
        return _get_cached("sources", lambda: repo.list_inventory_sources(active_only=False))

    def _lot_assignments():
        return _get_cached("lot_assignments", repo.list_product_lot_assignments)

    def _media_assets(include_archived: bool):
        return _get_cached(
            f"media_assets_{int(bool(include_archived))}",
            lambda: repo.list_media_assets(include_archived=bool(include_archived)),
        )

    tab_products, tab_listings, tab_sales, tab_lots, tab_media, tab_audit = st.tabs(
        ["Products", "Listings", "Sales", "Lots", "Media", "Audit Log"]
    )

    def _render_df_with_preview(df: pd.DataFrame, *, hide_index: bool = False) -> None:
        if bool(search_edit_render_full_tables):
            st.dataframe(df, use_container_width=True, hide_index=hide_index)
            return
        preview_limit = int(search_edit_preview_row_limit)
        total_rows = int(len(df.index))
        preview_df = df.head(preview_limit)
        st.dataframe(preview_df, use_container_width=True, hide_index=hide_index)
        if total_rows > preview_limit:
            st.caption(
                f"Showing preview rows: {preview_limit} / {total_rows}. "
                "Enable `Render Full Tables` to load all rows."
            )

    with tab_products:
        products = _products()
        q = st.text_input("Search Products", key="search_products")
        q_lower = q.strip().lower()
        filtered = [
            p
            for p in products
            if not q_lower
            or q_lower in p.sku.lower()
            or q_lower in p.title.lower()
            or q_lower in (p.category or "").lower()
            or q_lower in (p.metal_type or "").lower()
        ]

        _render_df_with_preview(
            pd.DataFrame(
                [
                    {
                        "id": p.id,
                        "sku": p.sku,
                        "title": p.title,
                        "category": p.category,
                        "metal_type": p.metal_type,
                        "weight_oz": float(p.weight_oz) if p.weight_oz is not None else None,
                        "pkg_weight_oz": float(p.package_weight_oz) if p.package_weight_oz is not None else None,
                        "length_in": float(p.package_length_in) if p.package_length_in is not None else None,
                        "width_in": float(p.package_width_in) if p.package_width_in is not None else None,
                        "height_in": float(p.package_height_in) if p.package_height_in is not None else None,
                        "acquisition_cost": float(p.acquisition_cost) if p.acquisition_cost is not None else None,
                        "acquisition_tax_paid": float(getattr(p, "acquisition_tax_paid", 0.0) or 0.0),
                        "acquisition_shipping_paid": float(getattr(p, "acquisition_shipping_paid", 0.0) or 0.0),
                        "acquisition_handling_paid": float(getattr(p, "acquisition_handling_paid", 0.0) or 0.0),
                        "landed_unit_cost": (
                            float(p.acquisition_cost or 0.0)
                            + float(getattr(p, "acquisition_tax_paid", 0.0) or 0.0)
                            + float(getattr(p, "acquisition_shipping_paid", 0.0) or 0.0)
                            + float(getattr(p, "acquisition_handling_paid", 0.0) or 0.0)
                        ),
                        "landed_on_hand_value": (
                            (
                                float(p.acquisition_cost or 0.0)
                                + float(getattr(p, "acquisition_tax_paid", 0.0) or 0.0)
                                + float(getattr(p, "acquisition_shipping_paid", 0.0) or 0.0)
                                + float(getattr(p, "acquisition_handling_paid", 0.0) or 0.0)
                            )
                            * int(p.current_quantity or 0)
                        ),
                        "product_cost": float(getattr(p, "product_cost", None)) if getattr(p, "product_cost", None) is not None else None,
                        "ebay_purchase": bool(getattr(p, "ebay_purchase", False)),
                        "ebay_purchase_item_id": str(getattr(p, "ebay_purchase_item_id", "") or ""),
                        "ebay_purchase_url": str(getattr(p, "ebay_purchase_url", "") or ""),
                        "qty": p.current_quantity,
                        "acquired_at": iso_or_none(p.acquired_at),
                        "status": p.status,
                    }
                    for p in filtered
                ]
            )
        )

        if filtered:
            product_map = {f"#{p.id} | {p.sku} | {p.title}": p for p in filtered}
            selected_key = st.selectbox("Select Product to Edit", list(product_map.keys()), key="edit_product_key")
            selected = product_map[selected_key]

            with st.form("edit_product_form"):
                title = st.text_input("Title", value=selected.title)
                category = st.selectbox(
                    "Category",
                    ["bullion", "coins", "collectibles", "antiques", "other"],
                    index=["bullion", "coins", "collectibles", "antiques", "other"].index(selected.category)
                    if selected.category in ["bullion", "coins", "collectibles", "antiques", "other"]
                    else 4,
                )
                description = st.text_area("Description", value=selected.description or "")
                metal_type = st.text_input("Metal Type", value=selected.metal_type or "")
                weight_oz = st.number_input(
                    "Weight (oz)", min_value=0.0, value=float(selected.weight_oz or 0.0), step=0.01
                )
                sw1, sw2, sw3, sw4 = st.columns(4)
                with sw1:
                    package_weight_oz = st.number_input(
                        "Package Weight (oz)",
                        min_value=0.0,
                        value=float(selected.package_weight_oz or 0.0),
                        step=0.01,
                    )
                with sw2:
                    package_length_in = st.number_input(
                        "Length (in)", min_value=0.0, value=float(selected.package_length_in or 0.0), step=0.1
                    )
                with sw3:
                    package_width_in = st.number_input(
                        "Width (in)", min_value=0.0, value=float(selected.package_width_in or 0.0), step=0.1
                    )
                with sw4:
                    package_height_in = st.number_input(
                        "Height (in)", min_value=0.0, value=float(selected.package_height_in or 0.0), step=0.1
                    )
                acquisition_cost = st.number_input(
                    "Acquisition Cost", min_value=0.0, value=float(selected.acquisition_cost or 0.0), step=1.0
                )
                acquisition_tax_paid = st.number_input(
                    "Acquisition Tax Paid",
                    min_value=0.0,
                    value=float(getattr(selected, "acquisition_tax_paid", 0.0) or 0.0),
                    step=1.0,
                )
                acquisition_shipping_paid = st.number_input(
                    "Acquisition Shipping Paid",
                    min_value=0.0,
                    value=float(getattr(selected, "acquisition_shipping_paid", 0.0) or 0.0),
                    step=1.0,
                )
                acquisition_handling_paid = st.number_input(
                    "Acquisition Handling Paid",
                    min_value=0.0,
                    value=float(getattr(selected, "acquisition_handling_paid", 0.0) or 0.0),
                    step=1.0,
                )
                product_cost = st.number_input(
                    "Product Cost",
                    min_value=0.0,
                    value=float(getattr(selected, "product_cost", 0.0) or 0.0),
                    step=1.0,
                )
                ebay_purchase = st.checkbox(
                    "Purchased On eBay",
                    value=bool(getattr(selected, "ebay_purchase", False)),
                )
                ebay_purchase_item_id = st.text_input(
                    "eBay Purchase Item ID",
                    value=str(getattr(selected, "ebay_purchase_item_id", "") or ""),
                )
                ebay_purchase_url = st.text_input(
                    "eBay Purchase Link",
                    value=str(getattr(selected, "ebay_purchase_url", "") or ""),
                )
                qty = st.number_input("Quantity", min_value=0, value=int(selected.current_quantity), step=1)
                acquired_date = st.date_input(
                    "Acquired Date",
                    value=(selected.acquired_at or utcnow_naive()).date(),
                    key="edit_product_acquired_date",
                )
                status = st.selectbox(
                    "Status",
                    ["active", "archived"],
                    index=0 if selected.status == "active" else 1,
                )
                ai_comp = st.text_area(
                    "AI Comp",
                    value=str(getattr(selected, "ai_comp", "") or ""),
                    key="edit_product_ai_comp",
                )
                submit = st.form_submit_button("Save Product Changes")

            if submit:
                if not ensure_permission(user, "update", "Update Product"):
                    st.stop()
                try:
                    if ebay_purchase and not ebay_purchase_item_id.strip():
                        st.error("eBay Purchase Item ID is required when Purchased On eBay is enabled.")
                        st.stop()
                    repo.update_product(
                        selected.id,
                        {
                            "title": title.strip(),
                            "category": category,
                            "description": description.strip(),
                            "metal_type": metal_type.strip(),
                            "weight_oz": to_decimal_or_none(weight_oz),
                            "package_weight_oz": to_decimal_or_none(package_weight_oz),
                            "package_length_in": to_decimal_or_none(package_length_in),
                            "package_width_in": to_decimal_or_none(package_width_in),
                            "package_height_in": to_decimal_or_none(package_height_in),
                            "acquisition_cost": to_decimal_or_none(acquisition_cost),
                            "acquisition_tax_paid": to_decimal_or_none(acquisition_tax_paid),
                            "acquisition_shipping_paid": to_decimal_or_none(acquisition_shipping_paid),
                            "acquisition_handling_paid": to_decimal_or_none(acquisition_handling_paid),
                            "product_cost": to_decimal_or_none(product_cost),
                            "ebay_purchase": bool(ebay_purchase),
                            "ebay_purchase_item_id": ebay_purchase_item_id.strip(),
                            "ebay_purchase_url": ebay_purchase_url.strip(),
                            "current_quantity": int(qty),
                            "acquired_at": datetime.combine(acquired_date, datetime.min.time()),
                            "status": status,
                            "ai_comp": normalize_ai_text(ai_comp.strip()),
                        },
                        actor=actor,
                    )
                    st.success("Product updated.")
                except IntegrityError:
                    repo.db.rollback()
                    st.error("Update failed due to data constraints (possibly duplicate value).")
                except ValueError as exc:
                    st.error(str(exc))
        else:
            st.info("No matching products.")

    with tab_listings:
        listings = _listings()
        products = _products()
        product_map = build_product_options(products, include_none=False, include_id=True)
        q = st.text_input("Search Listings", key="search_listings")
        include_archived_listings = st.checkbox(
            "Include Archived Listings",
            value=False,
            key="search_edit_listings_include_archived",
        )
        q_lower = q.strip().lower()
        filtered = [
            l
            for l in listings
            if not q_lower
            or q_lower in (l.marketplace or "").lower()
            or q_lower in (l.listing_title or "").lower()
            or q_lower in (l.external_listing_id or "").lower()
            or q_lower in (l.marketplace_url or "").lower()
            or q_lower in (l.marketplace_details or "").lower()
            or q_lower in (l.product.sku.lower() if l.product else "")
        ]
        if not include_archived_listings:
            filtered = [l for l in filtered if not _listing_is_archived(l)]

        _render_df_with_preview(
            pd.DataFrame(
                [
                    {
                        "id": l.id,
                        "product_id": l.product_id,
                        "marketplace": l.marketplace,
                        "external_listing_id": l.external_listing_id,
                        "marketplace_url": l.marketplace_url,
                        "listing_title": l.listing_title,
                        "listing_price": float(l.listing_price),
                        "quantity_listed": l.quantity_listed,
                        "listed_at": iso_or_none(l.listed_at),
                        "status": l.listing_status,
                        "archived": bool(_listing_is_archived(l)),
                    }
                    for l in filtered
                ]
            )
        )

        if filtered:
            listing_map = {f"#{l.id} | {l.marketplace} | {l.listing_title}": l for l in filtered}
            selected_key = st.selectbox("Select Listing to Edit", list(listing_map.keys()), key="edit_listing_key")
            selected = listing_map[selected_key]
            listing_related_sales = [
                sale
                for sale in _sales()
                if sale.listing_id is not None and int(sale.listing_id) == int(selected.id)
            ]
            listing_related_order_items = [
                item
                for item in _order_items()
                if item.listing_id is not None and int(item.listing_id) == int(selected.id)
            ]
            listing_related_order_ids = {
                int(sale.order_id)
                for sale in listing_related_sales
                if sale.order_id is not None
            } | {
                int(item.order_id)
                for item in listing_related_order_items
                if item.order_id is not None
            }
            listing_order_index = {int(order.id): order for order in _orders()}
            listing_related_orders = [
                listing_order_index[oid]
                for oid in sorted(listing_related_order_ids)
                if int(oid) in listing_order_index
            ]
            st.markdown("##### Document Draft")
            ld1, ld2 = st.columns([2, 1])
            with ld1:
                listing_doc_type = st.selectbox(
                    "Document Type",
                    options=["invoice", "receipt"],
                    index=0,
                    key=f"search_edit_listing_doc_type_{selected.id}",
                )
            listing_source_options: dict[str, tuple[str, int]] = {}
            for sale in sorted(
                listing_related_sales,
                key=lambda s: (s.sold_at or datetime.min, s.id),
                reverse=True,
            ):
                sale_label = (
                    f"Sale #{int(sale.id)} | {str(sale.marketplace or '').strip()} | "
                    f"{str(sale.external_order_id or '').strip() or 'no-ext-id'} | "
                    f"gross=${float(sale.sold_price or 0):,.2f}"
                )
                listing_source_options[sale_label] = ("Sale", int(sale.id))
            for order in sorted(
                listing_related_orders,
                key=lambda o: (o.sold_at or datetime.min, o.id),
                reverse=True,
            ):
                order_label = (
                    f"Order #{int(order.id)} | {str(order.marketplace or '').strip()} | "
                    f"{str(order.external_order_id or '').strip() or 'no-ext-id'} | "
                    f"total=${float(order.total_amount or 0):,.2f}"
                )
                if order_label not in listing_source_options:
                    listing_source_options[order_label] = ("Order", int(order.id))
            if listing_source_options:
                selected_source_label = st.selectbox(
                    "Related Sale/Order Source",
                    options=list(listing_source_options.keys()),
                    key=f"search_edit_listing_doc_source_{selected.id}",
                )
                with ld2:
                    if st.button(
                        "Open in Documents",
                        key=f"search_edit_listing_to_documents_{selected.id}",
                    ):
                        src_type, src_id = listing_source_options[selected_source_label]
                        handoff_to_documents_draft(
                            source_type=src_type,
                            source_id=int(src_id),
                            doc_type=listing_doc_type,
                            handoff_from="search_edit_listings",
                            repo=repo,
                            actor=actor,
                        )
            else:
                st.caption("No related sales/orders found for this listing.")
            default_product_key = key_for_value(product_map, selected.product_id, "")
            product_keys = list(product_map.keys())
            default_idx = product_keys.index(default_product_key) if default_product_key in product_keys else 0

            with st.form("edit_listing_form"):
                product_key = st.selectbox("Product", product_keys, index=default_idx)
                marketplace = st.selectbox(
                    "Marketplace",
                    MARKETPLACES,
                    index=MARKETPLACES.index(selected.marketplace)
                    if selected.marketplace in MARKETPLACES
                    else 0,
                )
                listing_title = st.text_input("Listing Title", value=selected.listing_title)
                listing_price = st.number_input(
                    "Listing Price", min_value=0.0, value=float(selected.listing_price), step=1.0
                )
                quantity_listed = st.number_input(
                    "Quantity Listed", min_value=1, value=int(selected.quantity_listed), step=1
                )
                listing_status = st.selectbox(
                    "Listing Status",
                    ["draft", "active", "ended", "sold"],
                    index=["draft", "active", "ended", "sold"].index(selected.listing_status),
                )
                external_listing_id = st.text_input("External Listing ID", value=selected.external_listing_id or "")
                marketplace_url = st.text_input("Marketplace URL", value=selected.marketplace_url or "")
                marketplace_details = st.text_area("Marketplace Details", value=selected.marketplace_details or "")
                listed_date = st.date_input(
                    "Listed Date",
                    value=(selected.listed_at or utcnow_naive()).date(),
                    key="edit_listing_listed_date",
                )
                submit = st.form_submit_button("Save Listing Changes")

            if submit:
                if not ensure_permission(user, "update", "Update Listing"):
                    st.stop()
                try:
                    current_media = repo.list_media_assets_for_listing(selected.id)
                    ValidationService.validate_listing_workflow(
                        listing_title=listing_title.strip(),
                        listing_price=to_decimal(listing_price),
                        quantity_listed=int(quantity_listed),
                        listing_status=listing_status,
                        media_count=len(current_media),
                        external_listing_id=external_listing_id.strip(),
                        marketplace_url=marketplace_url.strip(),
                    )
                    repo.update_listing(
                        selected.id,
                        {
                            "product_id": product_map[product_key],
                            "marketplace": marketplace,
                            "listing_title": listing_title.strip(),
                            "listing_price": to_decimal(listing_price),
                            "quantity_listed": int(quantity_listed),
                            "listing_status": listing_status,
                            "external_listing_id": external_listing_id.strip(),
                            "marketplace_url": marketplace_url.strip(),
                            "marketplace_details": marketplace_details.strip(),
                            "listed_at": datetime.combine(listed_date, datetime.min.time()),
                        },
                        actor=actor,
                    )
                    st.success("Listing updated.")
                except IntegrityError:
                    repo.db.rollback()
                    st.error("Update failed due to data constraints (possibly duplicate marketplace/external ID).")
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))
            st.markdown("##### Listing Lifecycle")
            listing_archived = bool(_listing_is_archived(selected))
            if listing_archived:
                st.info("Listing is archived.")
                if st.button(
                    "Restore Listing",
                    key=f"search_edit_restore_listing_{selected.id}",
                ):
                    if not ensure_permission(user, "update", "Restore Listing"):
                        st.stop()
                    try:
                        repo.restore_listing(int(selected.id), actor=actor)
                        st.success("Listing restored.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            else:
                archive_reason = st.text_input(
                    "Archive Reason (optional)",
                    value="",
                    key=f"search_edit_archive_listing_reason_{selected.id}",
                )
                if st.button(
                    "Archive Listing",
                    key=f"search_edit_archive_listing_{selected.id}",
                ):
                    if not ensure_permission(user, "update", "Archive Listing"):
                        st.stop()
                    try:
                        repo.archive_listing(
                            int(selected.id),
                            actor=actor,
                            reason=str(archive_reason or "").strip(),
                        )
                        st.success("Listing archived.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        else:
            st.info("No matching listings.")

    with tab_sales:
        sales = _sales()
        products = _products()
        listings = _listings()
        product_opts = build_product_options(products, include_none=True, include_id=True)
        listing_opts = build_listing_options(listings, include_none=True, include_id=True)
        q = st.text_input("Search Sales", key="search_sales")
        q_lower = q.strip().lower()
        filtered = [
            s
            for s in sales
            if not q_lower
            or q_lower in (s.marketplace or "").lower()
            or q_lower in (s.external_order_id or "").lower()
            or q_lower in (s.tracking_number or "").lower()
            or q_lower in (s.tracking_status or "").lower()
            or q_lower in (s.shipping_provider or "").lower()
            or q_lower in (s.shipping_exception_code or "").lower()
            or q_lower in (s.shipping_exception_notes or "").lower()
            or q_lower in (s.product.sku.lower() if s.product else "")
        ]

        _render_df_with_preview(
            pd.DataFrame(
                [
                    {
                        "id": s.id,
                        "marketplace": s.marketplace,
                        "product_id": s.product_id,
                        "listing_id": s.listing_id,
                        "external_order_id": s.external_order_id,
                        "shipping_provider": s.shipping_provider,
                        "shipping_service": s.shipping_service,
                        "shipping_package_type": s.shipping_package_type,
                        "tracking_number": s.tracking_number,
                        "tracking_status": s.tracking_status,
                        "shipping_exception_code": s.shipping_exception_code,
                        "shipping_exception_action": s.shipping_exception_action,
                        "shipping_exception_resolved_at": iso_or_none(s.shipping_exception_resolved_at),
                        "shipment_exported_at": iso_or_none(s.shipment_exported_at),
                        "sold_price": float(s.sold_price),
                        "fees": float(s.fees),
                        "shipping_cost": float(s.shipping_cost),
                        "quantity_sold": s.quantity_sold,
                        "sold_at": iso_or_none(s.sold_at),
                        "shipped_at": iso_or_none(s.shipped_at),
                        "delivered_at": iso_or_none(s.delivered_at),
                    }
                    for s in filtered
                ]
            )
        )

        if filtered:
            sale_map = {f"#{s.id} | {s.marketplace} | {s.external_order_id or 'no-order-id'}": s for s in filtered}
            selected_key = st.selectbox("Select Sale to Edit", list(sale_map.keys()), key="edit_sale_key")
            selected = sale_map[selected_key]
            st.markdown("##### Document Draft")
            sd1, sd2 = st.columns([2, 1])
            with sd1:
                sale_doc_type = st.selectbox(
                    "Document Type",
                    options=["invoice", "receipt"],
                    index=0,
                    key=f"search_edit_sale_doc_type_{selected.id}",
                )
            with sd2:
                if st.button(
                    "Open in Documents",
                    key=f"search_edit_sale_to_documents_{selected.id}",
                ):
                    handoff_to_documents_draft(
                        source_type="Sale",
                        source_id=int(selected.id),
                        doc_type=sale_doc_type,
                        handoff_from="search_edit_sales",
                        repo=repo,
                        actor=actor,
                    )
            default_product = key_for_value(product_opts, selected.product_id, "None")
            default_listing = key_for_value(listing_opts, selected.listing_id, "None")

            with st.form("edit_sale_form"):
                marketplace = st.selectbox(
                    "Marketplace",
                    MARKETPLACES,
                    index=MARKETPLACES.index(selected.marketplace)
                    if selected.marketplace in MARKETPLACES
                    else 0,
                )
                product_key = st.selectbox(
                    "Product (Optional)",
                    list(product_opts.keys()),
                    index=list(product_opts.keys()).index(default_product),
                )
                listing_key = st.selectbox(
                    "Listing (Optional)",
                    list(listing_opts.keys()),
                    index=list(listing_opts.keys()).index(default_listing),
                )
                sold_price = st.number_input("Sold Price", min_value=0.0, value=float(selected.sold_price), step=1.0)
                fees = st.number_input("Fees", min_value=0.0, value=float(selected.fees), step=1.0)
                shipping_cost = st.number_input(
                    "Shipping Cost", min_value=0.0, value=float(selected.shipping_cost), step=1.0
                )
                quantity_sold = st.number_input(
                    "Quantity Sold", min_value=1, value=int(selected.quantity_sold), step=1
                )
                external_order_id = st.text_input("External Order ID", value=selected.external_order_id or "")
                shipping_provider = st.selectbox(
                    "Shipping Provider",
                    ["", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"],
                    index=(
                        ["", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"].index(
                            selected.shipping_provider
                        )
                        if selected.shipping_provider in ["", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"]
                        else 0
                    ),
                )
                shipping_service = st.text_input("Shipping Service", value=selected.shipping_service or "")
                shipping_package_type = st.text_input(
                    "Package Type",
                    value=selected.shipping_package_type or "",
                )
                tracking_number = st.text_input("Tracking Number", value=selected.tracking_number or "")
                tracking_status = st.selectbox(
                    "Tracking Status",
                    ["", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"],
                    index=(
                        ["", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"].index(
                            selected.tracking_status
                        )
                        if selected.tracking_status in ["", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"]
                        else 0
                    ),
                )
                shipping_exception_code = st.text_input(
                    "Exception Code",
                    value=selected.shipping_exception_code or "",
                )
                shipping_exception_action = st.selectbox(
                    "Exception Action",
                    ["", "contact_buyer", "carrier_claim_opened", "refund_issued", "replacement_shipped", "monitoring", "other"],
                    index=(
                        ["", "contact_buyer", "carrier_claim_opened", "refund_issued", "replacement_shipped", "monitoring", "other"].index(
                            selected.shipping_exception_action
                        )
                        if selected.shipping_exception_action in [
                            "",
                            "contact_buyer",
                            "carrier_claim_opened",
                            "refund_issued",
                            "replacement_shipped",
                            "monitoring",
                            "other",
                        ]
                        else 0
                    ),
                )
                shipping_exception_notes = st.text_area(
                    "Exception Notes",
                    value=selected.shipping_exception_notes or "",
                )
                sold_date = st.date_input(
                    "Sold Date",
                    value=(selected.sold_at or utcnow_naive()).date(),
                    key="edit_sale_sold_date",
                )
                d1, d2 = st.columns(2)
                with d1:
                    shipped_enabled = st.checkbox("Has Shipped Date", value=selected.shipped_at is not None)
                    shipped_date = st.date_input(
                        "Shipped Date",
                        value=(selected.shipped_at or utcnow_naive()).date(),
                        disabled=not shipped_enabled,
                        key="edit_sale_shipped_date",
                    )
                with d2:
                    delivered_enabled = st.checkbox("Has Delivered Date", value=selected.delivered_at is not None)
                    delivered_date = st.date_input(
                        "Delivered Date",
                        value=(selected.delivered_at or utcnow_naive()).date(),
                        disabled=not delivered_enabled,
                        key="edit_sale_delivered_date",
                    )
                submit = st.form_submit_button("Save Sale Changes")

            if submit:
                if not ensure_permission(user, "update", "Update Sale"):
                    st.stop()
                try:
                    repo.update_sale(
                        selected.id,
                        {
                            "marketplace": marketplace,
                            "product_id": product_opts[product_key],
                            "listing_id": listing_opts[listing_key],
                            "sold_price": to_decimal(sold_price),
                            "fees": to_decimal(fees),
                            "shipping_cost": to_decimal(shipping_cost),
                            "shipping_provider": shipping_provider.strip(),
                            "shipping_service": shipping_service.strip(),
                            "shipping_package_type": shipping_package_type.strip(),
                            "tracking_number": tracking_number.strip(),
                            "tracking_status": tracking_status.strip(),
                            "shipping_exception_code": shipping_exception_code.strip(),
                            "shipping_exception_action": shipping_exception_action.strip(),
                            "shipping_exception_notes": shipping_exception_notes.strip(),
                            "shipping_exception_resolved_at": (
                                datetime.combine(delivered_date, datetime.min.time())
                                if tracking_status == "delivered"
                                else None
                            ),
                            "shipping_exception_resolved_by": actor.strip() if tracking_status == "delivered" else "",
                            "quantity_sold": int(quantity_sold),
                            "external_order_id": external_order_id.strip(),
                            "shipped_at": datetime.combine(shipped_date, datetime.min.time()) if shipped_enabled else None,
                            "delivered_at": datetime.combine(delivered_date, datetime.min.time()) if delivered_enabled else None,
                            "sold_at": datetime.combine(sold_date, datetime.min.time()),
                        },
                        actor=actor,
                    )
                    st.success("Sale updated.")
                except ValueError as exc:
                    st.error(str(exc))
        else:
            st.info("No matching sales.")

    with tab_lots:
        lots = _lots()
        sources = _sources()
        products = _products()
        assignments = _lot_assignments()
        product_index = {int(p.id): p for p in products}
        lot_assignment_rows = _build_lot_assignment_rows(assignments, product_index)
        source_options: dict[str, int | None] = {"None (one-off/manual)": None}
        for s in sources:
            source_options[f"#{int(s.id)} | {s.name} ({s.source_type})"] = int(s.id)
        q = st.text_input("Search Lots", key="search_lots")
        include_archived_lots = st.checkbox(
            "Include Archived Lots",
            value=False,
            key="search_edit_lots_include_archived",
        )
        q_lower = q.strip().lower()
        filtered_lots = [
            lot
            for lot in lots
            if not q_lower
            or q_lower in str(lot.lot_code or "").lower()
            or q_lower in str(lot.vendor or "").lower()
            or q_lower in str(lot.notes or "").lower()
            or q_lower in str(getattr(lot, "ebay_purchase_item_id", "") or "").lower()
            or q_lower in str(getattr(lot, "ebay_purchase_url", "") or "").lower()
        ]
        if not include_archived_lots:
            filtered_lots = [lot for lot in filtered_lots if not _lot_is_archived(lot)]

        _render_df_with_preview(pd.DataFrame(_build_lot_table_rows(filtered_lots, lot_assignment_rows)))

        if filtered_lots:
            lot_map = {
                f"#{int(lot.id)} | {str(lot.lot_code or '')} | {str(lot.vendor or '')}": lot
                for lot in filtered_lots
            }
            selected_key = st.selectbox("Select Lot to Edit", list(lot_map.keys()), key="edit_lot_key")
            selected = lot_map[selected_key]
            selected_lot_assignments = lot_assignment_rows.get(int(selected.id), [])
            st.markdown("##### Attached Products")
            if selected_lot_assignments:
                _render_df_with_preview(
                    pd.DataFrame(selected_lot_assignments),
                    hide_index=True,
                )
            else:
                st.caption("No products are currently attached to this lot.")

            default_source_key = "None (one-off/manual)"
            for label, source_id in source_options.items():
                if source_id == selected.source_id:
                    default_source_key = label
                    break
            source_labels = list(source_options.keys())
            default_source_idx = source_labels.index(default_source_key)

            with st.form("edit_lot_form"):
                lot_code = st.text_input("Lot Code", value=str(selected.lot_code or "").strip())
                source_key = st.selectbox("Common Source (Optional)", source_labels, index=default_source_idx)
                vendor = st.text_input("Vendor Override / One-Off Source (Optional)", value=str(selected.vendor or ""))
                purchase_date = st.date_input(
                    "Purchase Date",
                    value=(selected.purchase_date or utcnow_naive()).date(),
                    key="edit_lot_purchase_date",
                )
                total_cost = st.number_input(
                    "Lot Item Subtotal (before tax/shipping/fees)",
                    min_value=0.0,
                    value=float(selected.total_cost or 0.0),
                    step=1.0,
                    help=(
                        "Enter the item subtotal only. Do not enter the order total here when "
                        "tax/shipping/fees are entered separately."
                    ),
                )
                total_tax_paid = st.number_input(
                    "Total Lot Tax Paid",
                    min_value=0.0,
                    value=float(getattr(selected, "total_tax_paid", 0.0) or 0.0),
                    step=1.0,
                )
                total_shipping_paid = st.number_input(
                    "Total Lot Shipping Paid",
                    min_value=0.0,
                    value=float(getattr(selected, "total_shipping_paid", 0.0) or 0.0),
                    step=1.0,
                )
                total_handling_paid = st.number_input(
                    "Total Lot Handling Paid",
                    min_value=0.0,
                    value=float(getattr(selected, "total_handling_paid", 0.0) or 0.0),
                    step=1.0,
                )
                expected_total_quantity = st.number_input(
                    "Expected Total Lot Quantity",
                    min_value=0,
                    value=int(getattr(selected, "expected_total_quantity", 0) or 0),
                    step=1,
                    help="Optional. Use when whole-lot cost should account for items not checked in yet.",
                )
                ebay_purchase = st.checkbox(
                    "Purchased On eBay",
                    value=bool(getattr(selected, "ebay_purchase", False)),
                )
                ebay_purchase_item_id = st.text_input(
                    "eBay Purchase Item ID",
                    value=str(getattr(selected, "ebay_purchase_item_id", "") or ""),
                )
                ebay_purchase_url = st.text_input(
                    "eBay Purchase Link",
                    value=str(getattr(selected, "ebay_purchase_url", "") or ""),
                )
                notes = st.text_area("Notes", value=str(selected.notes or ""))
                submit = st.form_submit_button("Save Lot Changes")

            if submit:
                if not ensure_permission(user, "update", "Update Purchase Lot"):
                    st.stop()
                try:
                    lot_update_error = _validate_lot_update_inputs(
                        lot_code=lot_code,
                        ebay_purchase=bool(ebay_purchase),
                        ebay_purchase_item_id=ebay_purchase_item_id,
                    )
                    if lot_update_error:
                        st.error(lot_update_error)
                        st.stop()
                    repo.update_purchase_lot(
                        selected.id,
                        _build_lot_update_payload(
                            source_id=source_options[source_key],
                            lot_code=lot_code,
                            vendor=vendor,
                            purchase_date=purchase_date,
                            total_cost=total_cost,
                            total_tax_paid=total_tax_paid,
                            total_shipping_paid=total_shipping_paid,
                            total_handling_paid=total_handling_paid,
                            expected_total_quantity=int(expected_total_quantity or 0),
                            ebay_purchase=bool(ebay_purchase),
                            ebay_purchase_item_id=ebay_purchase_item_id,
                            ebay_purchase_url=ebay_purchase_url,
                            notes=notes,
                        ),
                        actor=actor,
                    )
                    st.success("Lot updated.")
                except IntegrityError:
                    repo.db.rollback()
                    st.error("Update failed due to data constraints (possibly duplicate lot code).")
                except ValueError as exc:
                    st.error(str(exc))

            st.markdown("##### Lot Lifecycle")
            lot_archived = bool(_lot_is_archived(selected))
            if lot_archived:
                st.info("Lot is archived.")
                if st.button(
                    "Restore Lot",
                    key=f"search_edit_restore_lot_{selected.id}",
                ):
                    if not ensure_permission(user, "update", "Restore Purchase Lot"):
                        st.stop()
                    try:
                        repo.restore_purchase_lot(int(selected.id), actor=actor)
                        st.success("Lot restored.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            else:
                blockers = repo.get_purchase_lot_archive_blockers(int(selected.id))
                blockers_total = sum(int(v or 0) for v in blockers.values())
                if blockers_total > 0:
                    st.warning(
                        "Archive preflight: linked records detected "
                        f"(assignments={int(blockers.get('product_assignments', 0))}, "
                        f"documents={int(blockers.get('purchase_documents', 0))}, "
                        f"active_products={int(blockers.get('active_products', 0))}, "
                        f"active_listings={int(blockers.get('active_listings', 0))})."
                    )
                force_archive_lot = st.checkbox(
                    "Force archive lot despite linked records",
                    value=False,
                    key=f"search_edit_force_archive_lot_{selected.id}",
                    disabled=blockers_total <= 0,
                )
                archive_reason = st.text_input(
                    "Archive Reason (optional)",
                    value="",
                    key=f"search_edit_archive_lot_reason_{selected.id}",
                )
                if st.button(
                    "Archive Lot",
                    key=f"search_edit_archive_lot_{selected.id}",
                ):
                    if not ensure_permission(user, "update", "Archive Purchase Lot"):
                        st.stop()
                    try:
                        repo.archive_purchase_lot(
                            int(selected.id),
                            actor=actor,
                            reason=str(archive_reason or "").strip(),
                            force=bool(force_archive_lot),
                        )
                        st.success("Lot archived.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        else:
            st.info("No matching lots.")

    with tab_media:
        include_archived_media = st.checkbox(
            "Include Archived Media",
            value=False,
            key="search_edit_media_include_archived",
        )
        media_items = _media_assets(bool(include_archived_media))
        products = _products()
        listings = _listings()
        product_opts = build_product_options(products, include_none=True, include_id=True)
        listing_opts = build_listing_options(listings, include_none=True, include_id=True)
        q = st.text_input("Search Media", key="search_media")
        q_lower = q.strip().lower()
        filtered = [
            m
            for m in media_items
            if not q_lower
            or q_lower in (m.original_filename or "").lower()
            or q_lower in (m.s3_key or "").lower()
            or q_lower in (m.s3_url or "").lower()
        ]

        _render_df_with_preview(
            pd.DataFrame(
                [
                    {
                        "id": m.id,
                        "media_type": m.media_type,
                        "filename": m.original_filename,
                        "product_id": m.product_id,
                        "listing_id": m.listing_id,
                        "uploaded_by": m.uploaded_by,
                        "s3_key": m.s3_key,
                        "archived": bool(getattr(m, "is_archived", False)),
                    }
                    for m in filtered
                ]
            )
        )

        if filtered:
            media_map = {f"#{m.id} | {m.media_type} | {m.original_filename}": m for m in filtered}
            selected_key = st.selectbox("Select Media to Edit", list(media_map.keys()), key="edit_media_key")
            selected = media_map[selected_key]
            default_product = key_for_value(product_opts, selected.product_id, "None")
            default_listing = key_for_value(listing_opts, selected.listing_id, "None")

            with st.form("edit_media_form"):
                media_type = st.selectbox(
                    "Media Type",
                    ["image", "video", "other"],
                    index=["image", "video", "other"].index(selected.media_type),
                )
                product_key = st.selectbox(
                    "Associate Product",
                    list(product_opts.keys()),
                    index=list(product_opts.keys()).index(default_product),
                )
                listing_key = st.selectbox(
                    "Associate Listing",
                    list(listing_opts.keys()),
                    index=list(listing_opts.keys()).index(default_listing),
                )
                uploaded_by = st.text_input("Uploaded By", value=selected.uploaded_by)
                submit = st.form_submit_button("Save Media Changes")

            if submit:
                if not ensure_permission(user, "update", "Update Media"):
                    st.stop()
                repo.update_media_asset(
                    selected.id,
                    {
                        "media_type": media_type,
                        "product_id": product_opts[product_key],
                        "listing_id": listing_opts[listing_key],
                        "uploaded_by": uploaded_by.strip() or "employee",
                    },
                    actor=actor,
                )
                st.success("Media updated.")

            st.markdown("##### Media Lifecycle")
            media_archived = bool(getattr(selected, "is_archived", False))
            if media_archived:
                st.info("Media is archived.")
                if st.button("Restore Media", key=f"search_edit_restore_media_{selected.id}"):
                    if not ensure_permission(user, "update", "Restore Media"):
                        st.stop()
                    try:
                        repo.restore_media_asset(int(selected.id), actor=actor)
                        st.success("Media restored.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            else:
                blockers = repo.get_media_asset_archive_blockers(int(selected.id))
                blockers_total = sum(int(v or 0) for v in blockers.values())
                if blockers_total > 0:
                    st.warning(
                        "Archive preflight: active listing context detected "
                        f"(linked_listing_active={int(blockers.get('linked_listing_active', 0))}, "
                        f"linked_product_active_listings={int(blockers.get('linked_product_active_listings', 0))})."
                    )
                force_archive_media = st.checkbox(
                    "Force archive media despite active listing links",
                    value=False,
                    key=f"search_edit_force_archive_media_{selected.id}",
                    disabled=blockers_total <= 0,
                )
                if st.button("Archive Media", key=f"search_edit_archive_media_{selected.id}"):
                    if not ensure_permission(user, "update", "Archive Media"):
                        st.stop()
                    try:
                        repo.archive_media_asset(int(selected.id), actor=actor, force=bool(force_archive_media))
                        st.success("Media archived.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        else:
            st.info("No matching media.")

    with tab_audit:
        load_audit_history = st.checkbox(
            "Load Audit History (slower)",
            value=False,
            key="search_edit_load_audit_logs",
        )
        if not load_audit_history:
            st.caption("Enable audit history loading to query and render recent change logs.")
        else:
            logs = repo.list_audit_logs(limit=500)
            entity_filter = st.selectbox(
                "Entity Type Filter",
                [
                    "all",
                    "product",
                    "listing",
                    "sale",
                    "order",
                    "order_item",
                    "return",
                    "media_asset",
                    "purchase_lot",
                    "product_lot_assignment",
                    "inventory_source",
                    "shipping_preset",
                    "document_template_profile",
                ],
                key="audit_entity_filter",
            )
            action_filter = st.selectbox("Action Filter", ["all", "create", "update"], key="audit_action_filter")
            q = st.text_input("Search Actor / Changes", key="audit_search")
            q_lower = q.strip().lower()

            filtered_logs = [
                log
                for log in logs
                if (entity_filter == "all" or log.entity_type == entity_filter)
                and (action_filter == "all" or log.action == action_filter)
                and (
                    not q_lower
                    or q_lower in (log.actor or "").lower()
                    or q_lower in (log.changes_json or "").lower()
                )
            ]

            _render_df_with_preview(
                pd.DataFrame(
                    [
                        {
                            "id": log.id,
                            "created_at": iso_or_none(log.created_at),
                            "entity_type": log.entity_type,
                            "entity_id": log.entity_id,
                            "action": log.action,
                            "actor": log.actor,
                            "changes": pretty_json(log.changes_json),
                        }
                        for log in filtered_logs
                    ]
                )
            )
