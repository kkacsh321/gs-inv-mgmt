from datetime import datetime

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import (
    build_listing_options,
    build_product_options,
    iso_or_none,
    normalize_multiselect_values,
    to_decimal,
)
from app.components.views.shared import (
    MARKETPLACES,
    handoff_to_documents_draft,
    render_help_panel,
    render_table_toolbar,
)
from app.components.views.entity_ops import (
    render_saved_filter_bar,
    render_standard_row_actions,
)
from app.components.views.workspace_shell import render_workspace_feedback
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.runtime_settings import get_runtime_str
from app.utils.time import utc_today

def render_sales(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Sales")
    render_help_panel(
        section_title="Sales",
        goal="Record completed sales with marketplace fees, shipping cost, and tracking details.",
        steps=[
            "Select marketplace and optionally link product/listing for traceability.",
            "Enter sold price, fees, and shipping cost to preserve net margin data.",
            "Capture provider/service/tracking and optional shipped/delivered dates.",
            "Record each transaction once to keep reports and QuickBooks exports clean.",
        ],
        roadmap_phase="v0.3 Channel Sync + Accounting Readiness",
    )
    products = repo.list_products()
    listings = repo.list_listings()
    orders = repo.list_orders()

    with st.form("create_sale_form", clear_on_submit=True):
        product_opts = build_product_options(products, include_none=True, include_id=False)
        listing_opts = build_listing_options(listings, include_none=True, include_id=True)
        order_opts = {"None": None, **{f"#{o.id} | {o.marketplace} | {o.external_order_id}": o.id for o in orders}}

        marketplace = st.selectbox("Marketplace", MARKETPLACES)
        order_key = st.selectbox("Order (Optional)", list(order_opts.keys()))
        product_key = st.selectbox("Product (Optional)", list(product_opts.keys()))
        listing_key = st.selectbox("Listing (Optional)", list(listing_opts.keys()))

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            sold_price = st.number_input("Sold Price", min_value=0.0, value=0.0, step=1.0)
        with c2:
            fees = st.number_input("Fees", min_value=0.0, value=0.0, step=1.0)
        with c3:
            shipping_cost = st.number_input("Shipping", min_value=0.0, value=0.0, step=1.0)
        with c4:
            quantity_sold = st.number_input("Quantity Sold", min_value=1, value=1, step=1)

        external_order_id = st.text_input("External Order ID")
        sh1, sh2 = st.columns(2)
        with sh1:
            shipping_provider = st.selectbox(
                "Shipping Provider",
                ["", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"],
            )
            shipping_service = st.text_input("Shipping Service", placeholder="Ground Advantage, Priority, etc.")
            shipping_package_type = st.text_input("Package Type", placeholder="small_box, padded_mailer, etc.")
        with sh2:
            tracking_number = st.text_input("Tracking Number")
            tracking_status = st.selectbox(
                "Tracking Status",
                ["", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"],
            )
        sold_date = st.date_input("Sold Date", value=utc_today())
        d1, d2 = st.columns(2)
        with d1:
            shipped_enabled = st.checkbox("Has Shipped Date", value=False)
            shipped_date = st.date_input("Shipped Date", value=utc_today(), disabled=not shipped_enabled)
        with d2:
            delivered_enabled = st.checkbox("Has Delivered Date", value=False)
            delivered_date = st.date_input(
                "Delivered Date", value=utc_today(), disabled=not delivered_enabled
            )

        if st.form_submit_button("Record Sale"):
            if not ensure_permission(user, "create", "Record Sale"):
                st.stop()
            try:
                repo.create_sale(
                    marketplace=marketplace,
                    sold_price=to_decimal(sold_price),
                    fees=to_decimal(fees),
                    shipping_cost=to_decimal(shipping_cost),
                    shipping_provider=shipping_provider.strip(),
                    shipping_service=shipping_service.strip(),
                    shipping_package_type=shipping_package_type.strip(),
                    tracking_number=tracking_number.strip(),
                    tracking_status=tracking_status.strip(),
                    quantity_sold=int(quantity_sold),
                    order_id=order_opts[order_key],
                    product_id=product_opts[product_key],
                    listing_id=listing_opts[listing_key],
                    external_order_id=external_order_id.strip(),
                    shipped_at=datetime.combine(shipped_date, datetime.min.time()) if shipped_enabled else None,
                    delivered_at=datetime.combine(delivered_date, datetime.min.time()) if delivered_enabled else None,
                    sold_at=datetime.combine(sold_date, datetime.min.time()),
                    actor=user.username,
                )
                st.success("Sale recorded.")
            except ValueError as exc:
                st.error(str(exc))

    sales = repo.list_sales()
    sale_rows = [
        {
            "id": s.id,
            "marketplace": s.marketplace,
            "order_id": s.order_id,
            "product_id": s.product_id,
            "listing_id": s.listing_id,
            "external_order_id": s.external_order_id,
            "shipping_provider": s.shipping_provider,
            "shipping_service": s.shipping_service,
            "shipping_package_type": s.shipping_package_type,
            "tracking_number": s.tracking_number,
            "tracking_status": s.tracking_status,
            "shipping_exception_code": s.shipping_exception_code,
            "shipment_exported_at": iso_or_none(s.shipment_exported_at),
            "sold_price": float(s.sold_price),
            "fees": float(s.fees),
            "shipping_cost": float(s.shipping_cost),
            "qty": s.quantity_sold,
            "sold_at": iso_or_none(s.sold_at),
            "shipped_at": iso_or_none(s.shipped_at),
            "delivered_at": iso_or_none(s.delivered_at),
            "net": float(s.sold_price - s.fees - s.shipping_cost),
        }
        for s in sales
    ]
    st.markdown("### Sales Filters")
    sales_marketplace_options = sorted(
        {str(row["marketplace"]) for row in sale_rows if row.get("marketplace")}
    )
    sales_tracking_status_options = sorted(
        {str(row["tracking_status"]) for row in sale_rows if row.get("tracking_status")}
    )
    st.session_state["sales_filter_marketplaces"] = normalize_multiselect_values(
        st.session_state.get("sales_filter_marketplaces"),
        sales_marketplace_options,
    )
    st.session_state["sales_filter_tracking_status"] = normalize_multiselect_values(
        st.session_state.get("sales_filter_tracking_status"),
        sales_tracking_status_options,
    )
    f1, f2, f3 = st.columns(3)
    with f1:
        sale_filter_query = st.text_input("Search External Order / Tracking", key="sales_filter_query")
    with f2:
        sale_filter_marketplaces = st.multiselect(
            "Marketplace",
            options=sales_marketplace_options,
            default=[],
            key="sales_filter_marketplaces",
        )
    with f3:
        sale_filter_tracking_status = st.multiselect(
            "Tracking Status",
            options=sales_tracking_status_options,
            default=[],
            key="sales_filter_tracking_status",
        )
    effective_filter = render_saved_filter_bar(
        repo=repo,
        scope="sales",
        username=user.username,
        current_filters={
            "query": sale_filter_query,
            "marketplaces": sale_filter_marketplaces,
            "tracking_statuses": sale_filter_tracking_status,
        },
    )
    q = str(effective_filter.get("query") or "").strip().lower()
    marketplaces = {
        str(v).strip().lower() for v in (effective_filter.get("marketplaces") or []) if str(v).strip()
    }
    tracking_statuses = {
        str(v).strip().lower() for v in (effective_filter.get("tracking_statuses") or []) if str(v).strip()
    }
    filtered_rows = []
    for row in sale_rows:
        if q and q not in str(row.get("external_order_id") or "").lower() and q not in str(row.get("tracking_number") or "").lower():
            continue
        if marketplaces and str(row.get("marketplace") or "").strip().lower() not in marketplaces:
            continue
        if tracking_statuses and str(row.get("tracking_status") or "").strip().lower() not in tracking_statuses:
            continue
        filtered_rows.append(row)

    filtered_df = pd.DataFrame(filtered_rows)
    st.markdown("### Sales Table + Side Panel")
    table_col, panel_col = st.columns([2, 1])
    with table_col:
        render_table_toolbar(
            df=filtered_df,
            section_key="sales_table",
            export_basename="sales_filtered",
            active_filters={
                "query": q,
                "marketplaces": sorted(marketplaces),
                "tracking_statuses": sorted(tracking_statuses),
            },
        )
        st.dataframe(filtered_df, use_container_width=True)
        render_standard_row_actions(
            repo,
            entity_type="sale",
            rows=filtered_rows,
            id_field="id",
            title="Sale Row Actions",
        )

    with panel_col:
        st.markdown("#### Sale Detail/Edit")
        if not filtered_rows:
            st.info("No filtered sales available.")
        else:
            sales_by_id = {s.id: s for s in sales}
            select_options = {
                f"#{row['id']} | {row['marketplace']} | {row['external_order_id'] or 'no-ext-id'}": int(row["id"])
                for row in filtered_rows
            }
            prefill_sale_id = st.session_state.get("sales_prefill_sale_id")
            selected_index = 0
            if prefill_sale_id is not None:
                option_values = list(select_options.values())
                if int(prefill_sale_id) in option_values:
                    selected_index = option_values.index(int(prefill_sale_id))
            selected_label = st.selectbox(
                "Select Sale",
                options=list(select_options.keys()),
                index=selected_index,
                key="sales_side_panel_select",
            )
            if prefill_sale_id is not None:
                st.session_state.pop("sales_prefill_sale_id", None)
            selected_sale = sales_by_id[select_options[selected_label]]
            linked_order = None
            if selected_sale.order_id is not None:
                linked_order = next((o for o in orders if int(o.id) == int(selected_sale.order_id)), None)

            st.markdown("##### Sales/Orders Copilot")
            st.caption("AI triage for sale/order mismatches, shipping risk, and refund guidance.")
            if st.button("Analyze Selected Sale", key=f"sales_copilot_analyze_{selected_sale.id}"):
                if not ensure_permission(user, "ai_comp_use", "Use Sales Copilot"):
                    st.stop()
                try:
                    sale_context = {
                        "sale_id": int(selected_sale.id),
                        "marketplace": str(selected_sale.marketplace or ""),
                        "order_id": int(selected_sale.order_id) if selected_sale.order_id is not None else None,
                        "product_id": int(selected_sale.product_id) if selected_sale.product_id is not None else None,
                        "listing_id": int(selected_sale.listing_id) if selected_sale.listing_id is not None else None,
                        "external_order_id": str(selected_sale.external_order_id or ""),
                        "sold_price": float(selected_sale.sold_price or 0),
                        "fees": float(selected_sale.fees or 0),
                        "shipping_cost": float(selected_sale.shipping_cost or 0),
                        "quantity_sold": int(selected_sale.quantity_sold or 0),
                        "tracking_number": str(selected_sale.tracking_number or ""),
                        "tracking_status": str(selected_sale.tracking_status or ""),
                        "shipping_exception_code": str(selected_sale.shipping_exception_code or ""),
                        "shipped_at": iso_or_none(selected_sale.shipped_at),
                        "delivered_at": iso_or_none(selected_sale.delivered_at),
                    }
                    order_context = (
                        {
                            "id": int(linked_order.id),
                            "status": str(linked_order.order_status or ""),
                            "subtotal_amount": float(linked_order.subtotal_amount or 0),
                            "fees": float(linked_order.fees or 0),
                            "shipping_cost": float(linked_order.shipping_cost or 0),
                            "total_amount": float(linked_order.total_amount or 0),
                            "item_count": len(linked_order.items or []),
                        }
                        if linked_order is not None
                        else {}
                    )
                    system_message = get_runtime_str(
                        repo,
                        "comp_llm_system_message",
                        "You are an operations copilot for marketplace reselling workflows.",
                    ).strip()
                    instruction = (
                        "Return ONLY JSON with keys: `triage_summary`, `recommended_actions`, "
                        "`risk_flags`, `possible_data_issues`, `refund_return_guidance`. "
                        "`recommended_actions`, `risk_flags`, and `possible_data_issues` must be arrays of short strings."
                    )
                    result = execute_comp_summary(
                        repo,
                        query=f"Sales triage for sale #{int(selected_sale.id)}",
                        ebay_rows=[],
                        web_rows=[],
                        spot_context={"sale": sale_context, "linked_order": order_context},
                        system_message=system_message,
                        instruction=instruction,
                    )
                    st.session_state[f"sales_copilot_raw_{selected_sale.id}"] = str(result.text or "").strip()
                    st.success("Sales copilot analysis complete.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Sales copilot analysis failed: {exc}")
            raw_key = f"sales_copilot_raw_{selected_sale.id}"
            raw_val = str(st.session_state.get(raw_key) or "").strip()
            if raw_val:
                with st.expander("Sales Copilot Result", expanded=False):
                    st.code(raw_val, language="json")

            st.markdown("##### Document Draft")
            sd1, sd2 = st.columns([2, 1])
            with sd1:
                sales_doc_type = st.selectbox(
                    "Document Type",
                    options=["invoice", "receipt"],
                    index=0,
                    key=f"sales_documents_doc_type_{selected_sale.id}",
                )
            with sd2:
                if st.button(
                    "Open in Documents",
                    key=f"sales_to_documents_{selected_sale.id}",
                ):
                    handoff_to_documents_draft(
                        source_type="Sale",
                        source_id=int(selected_sale.id),
                        doc_type=sales_doc_type,
                        handoff_from="sales",
                        repo=repo,
                        actor=user.username,
                    )

            with st.form("sales_side_panel_edit_form"):
                sp1, sp2 = st.columns(2)
                with sp1:
                    edit_marketplace = st.selectbox(
                        "Marketplace",
                        MARKETPLACES,
                        index=MARKETPLACES.index(selected_sale.marketplace)
                        if selected_sale.marketplace in MARKETPLACES
                        else 0,
                    )
                    edit_external_order_id = st.text_input(
                        "External Order ID",
                        value=selected_sale.external_order_id or "",
                    )
                    edit_sold_price = st.number_input(
                        "Sold Price",
                        min_value=0.0,
                        value=float(selected_sale.sold_price or 0),
                        step=1.0,
                    )
                    edit_fees = st.number_input(
                        "Fees",
                        min_value=0.0,
                        value=float(selected_sale.fees or 0),
                        step=1.0,
                    )
                    edit_shipping_cost = st.number_input(
                        "Shipping Cost",
                        min_value=0.0,
                        value=float(selected_sale.shipping_cost or 0),
                        step=1.0,
                    )
                with sp2:
                    edit_tracking_number = st.text_input("Tracking Number", value=selected_sale.tracking_number or "")
                    edit_tracking_status = st.selectbox(
                        "Tracking Status",
                        ["", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"],
                        index=["", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"].index(
                            selected_sale.tracking_status
                        )
                        if selected_sale.tracking_status in {"", "label_created", "in_transit", "out_for_delivery", "delivered", "exception"}
                        else 0,
                    )
                    edit_shipping_provider = st.selectbox(
                        "Shipping Provider",
                        ["", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"],
                        index=["", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"].index(
                            selected_sale.shipping_provider
                        )
                        if selected_sale.shipping_provider in {"", "ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"}
                        else 0,
                    )
                    edit_shipping_service = st.text_input(
                        "Shipping Service",
                        value=selected_sale.shipping_service or "",
                    )
                    edit_shipping_package_type = st.text_input(
                        "Package Type",
                        value=selected_sale.shipping_package_type or "",
                    )
                d1, d2 = st.columns(2)
                with d1:
                    shipped_enabled = st.checkbox("Has Shipped Date", value=bool(selected_sale.shipped_at))
                    shipped_default = selected_sale.shipped_at.date() if selected_sale.shipped_at else utc_today()
                    shipped_date = st.date_input("Shipped Date", value=shipped_default, disabled=not shipped_enabled)
                with d2:
                    delivered_enabled = st.checkbox("Has Delivered Date", value=bool(selected_sale.delivered_at))
                    delivered_default = selected_sale.delivered_at.date() if selected_sale.delivered_at else utc_today()
                    delivered_date = st.date_input("Delivered Date", value=delivered_default, disabled=not delivered_enabled)
                save_side_panel = st.form_submit_button("Save Sale Changes")

            if save_side_panel:
                if not ensure_permission(user, "update", "Update Sale"):
                    st.stop()
                try:
                    repo.update_sale(
                        selected_sale.id,
                        {
                            "marketplace": edit_marketplace,
                            "external_order_id": edit_external_order_id.strip(),
                            "sold_price": to_decimal(edit_sold_price),
                            "fees": to_decimal(edit_fees),
                            "shipping_cost": to_decimal(edit_shipping_cost),
                            "tracking_number": edit_tracking_number.strip(),
                            "tracking_status": edit_tracking_status.strip(),
                            "shipping_provider": edit_shipping_provider.strip(),
                            "shipping_service": edit_shipping_service.strip(),
                            "shipping_package_type": edit_shipping_package_type.strip(),
                            "shipped_at": datetime.combine(shipped_date, datetime.min.time()) if shipped_enabled else None,
                            "delivered_at": datetime.combine(delivered_date, datetime.min.time()) if delivered_enabled else None,
                        },
                        actor=user.username,
                    )
                    st.success("Sale updated.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="sales",
        section_title="Workspace Feedback: Sales",
    )
