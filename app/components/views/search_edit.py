from datetime import datetime

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

    tab_products, tab_listings, tab_sales, tab_media, tab_audit = st.tabs(
        ["Products", "Listings", "Sales", "Media", "Audit Log"]
    )

    with tab_products:
        products = repo.list_products()
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

        st.dataframe(
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
            ),
            use_container_width=True,
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
                    disabled=not ebay_purchase,
                )
                ebay_purchase_url = st.text_input(
                    "eBay Purchase Link",
                    value=str(getattr(selected, "ebay_purchase_url", "") or ""),
                    disabled=not ebay_purchase,
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
        listings = repo.list_listings()
        products = repo.list_products()
        product_map = build_product_options(products, include_none=False, include_id=True)
        q = st.text_input("Search Listings", key="search_listings")
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

        st.dataframe(
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
                    }
                    for l in filtered
                ]
            ),
            use_container_width=True,
        )

        if filtered:
            listing_map = {f"#{l.id} | {l.marketplace} | {l.listing_title}": l for l in filtered}
            selected_key = st.selectbox("Select Listing to Edit", list(listing_map.keys()), key="edit_listing_key")
            selected = listing_map[selected_key]
            listing_related_sales = [
                sale
                for sale in repo.list_sales()
                if sale.listing_id is not None and int(sale.listing_id) == int(selected.id)
            ]
            listing_related_order_items = [
                item
                for item in repo.list_order_items()
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
            listing_order_index = {int(order.id): order for order in repo.list_orders()}
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
                    ["draft", "active", "ended"],
                    index=["draft", "active", "ended"].index(selected.listing_status),
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
        else:
            st.info("No matching listings.")

    with tab_sales:
        sales = repo.list_sales()
        products = repo.list_products()
        listings = repo.list_listings()
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

        st.dataframe(
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
            ),
            use_container_width=True,
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

    with tab_media:
        media_items = repo.list_media_assets()
        products = repo.list_products()
        listings = repo.list_listings()
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

        st.dataframe(
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
                    }
                    for m in filtered
                ]
            ),
            use_container_width=True,
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
        else:
            st.info("No matching media.")

    with tab_audit:
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

        st.dataframe(
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
            ),
            use_container_width=True,
        )
