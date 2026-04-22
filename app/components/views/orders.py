from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import (
    build_listing_options,
    build_product_options,
    format_ebay_sync_note_for_customer,
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


ORDER_STATUS_OPTIONS = [
    "draft",
    "not_shipped",
    "packaging",
    "paid",
    "shipped",
    "delivered",
    "cancelled",
    "refunded",
]


def render_orders(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Orders")
    st.caption("Capture multi-item orders and optionally create linked sale records per line.")
    render_help_panel(
        section_title="Orders",
        goal="Represent multi-line marketplace orders and keep line-level item detail.",
        steps=[
            "Select marketplace and order metadata, then add one or more line items.",
            "Use product/listing line links for traceability and inventory reporting.",
            "Optionally auto-create `sales` records from order lines.",
            "Update order status as fulfillment progresses.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    products = repo.list_products()
    listings = repo.list_listings()
    product_opts = build_product_options(products, include_none=False, include_id=True)
    listing_opts = build_listing_options(listings, include_none=True, include_id=True)

    with st.form("create_order_form", clear_on_submit=True):
        marketplace = st.selectbox("Marketplace", MARKETPLACES, key="order_marketplace")
        external_order_id = st.text_input("External Order ID (optional)")
        order_status = st.selectbox(
            "Order Status",
            ORDER_STATUS_OPTIONS,
            index=ORDER_STATUS_OPTIONS.index("not_shipped"),
        )
        sold_date = st.date_input("Order Date", value=utc_today())
        c1, c2 = st.columns(2)
        with c1:
            order_fees = st.number_input("Order-Level Fees", min_value=0.0, value=0.0, step=1.0)
        with c2:
            order_shipping = st.number_input("Order-Level Shipping Cost", min_value=0.0, value=0.0, step=1.0)
        notes = st.text_area("Order Notes")
        actor = st.text_input(
            "Actor (for audit log)",
            value=user.username,
            key="order_actor",
            disabled=True,
        )
        line_count = st.number_input("Line Item Count", min_value=1, max_value=10, value=1, step=1)
        create_sales = st.checkbox("Also create Sales records from line items", value=False)

        line_items: list[dict] = []
        for idx in range(int(line_count)):
            st.markdown(f"**Line {idx + 1}**")
            l1, l2, l3, l4, l5 = st.columns([3, 3, 1, 1, 1])
            with l1:
                product_key = st.selectbox(
                    "Product",
                    list(product_opts.keys()),
                    key=f"order_line_product_{idx}",
                )
            with l2:
                listing_key = st.selectbox(
                    "Listing (Optional)",
                    list(listing_opts.keys()),
                    key=f"order_line_listing_{idx}",
                )
            with l3:
                quantity = st.number_input(
                    "Qty",
                    min_value=1,
                    value=1,
                    step=1,
                    key=f"order_line_qty_{idx}",
                )
            with l4:
                unit_price = st.number_input(
                    "Unit Price",
                    min_value=0.0,
                    value=0.0,
                    step=1.0,
                    key=f"order_line_unit_price_{idx}",
                )
            with l5:
                line_fees = st.number_input(
                    "Line Fees",
                    min_value=0.0,
                    value=0.0,
                    step=1.0,
                    key=f"order_line_fees_{idx}",
                )
            line_items.append(
                {
                    "product_id": product_opts[product_key],
                    "listing_id": listing_opts[listing_key],
                    "quantity": int(quantity),
                    "unit_price": to_decimal(unit_price),
                    "line_fees": to_decimal(line_fees),
                    "line_shipping": to_decimal(0),
                    "notes": "",
                }
            )

        if st.form_submit_button("Create Order"):
            if not ensure_permission(user, "create", "Create Order"):
                st.stop()
            try:
                created_order = repo.create_order(
                    marketplace=marketplace,
                    external_order_id=external_order_id.strip(),
                    order_status=order_status,
                    sold_at=datetime.combine(sold_date, datetime.min.time()),
                    fees=to_decimal(order_fees),
                    shipping_cost=to_decimal(order_shipping),
                    notes=notes.strip(),
                    items=line_items,
                    actor=actor,
                )
                st.success(f"Order #{created_order.id} created.")
                if create_sales:
                    created_sales = 0
                    for item in line_items:
                        quantity = int(item["quantity"])
                        sold_price = to_decimal(float(item["unit_price"]) * quantity)
                        repo.create_sale(
                            marketplace=marketplace,
                            sold_price=sold_price,
                            fees=item["line_fees"],
                            shipping_cost=item["line_shipping"],
                            quantity_sold=quantity,
                            order_id=created_order.id,
                            product_id=item["product_id"],
                            listing_id=item["listing_id"],
                            external_order_id=created_order.external_order_id,
                            sold_at=datetime.combine(sold_date, datetime.min.time()),
                            actor=user.username,
                        )
                        created_sales += 1
                    st.success(f"Created {created_sales} linked sale record(s).")
            except IntegrityError:
                repo.db.rollback()
                st.error("Order create failed (possibly duplicate marketplace/external order ID).")
            except ValueError as exc:
                st.error(str(exc))

    orders = repo.list_orders()
    if not orders:
        st.info("No orders yet.")
        return

    order_rows = [
        {
            "id": o.id,
            "marketplace": o.marketplace,
            "external_order_id": o.external_order_id,
            "status": o.order_status,
            "sold_at": iso_or_none(o.sold_at),
            "subtotal_amount": float(o.subtotal_amount),
            "fees": float(o.fees),
            "shipping_cost": float(o.shipping_cost),
            "shipping_label_cost": float(getattr(o, "shipping_label_cost", 0) or 0),
            "shipping_delta": float(o.shipping_cost or 0) - float(getattr(o, "shipping_label_cost", 0) or 0),
            "shipping_label_currency": str(getattr(o, "shipping_label_currency", "") or "USD"),
            "shipping_provider": str(getattr(o, "shipping_provider", "") or ""),
            "shipping_service": str(getattr(o, "shipping_service", "") or ""),
            "tracking_number": str(getattr(o, "tracking_number", "") or ""),
            "tracking_status": str(getattr(o, "tracking_status", "") or ""),
            "shipped_at": iso_or_none(getattr(o, "shipped_at", None)),
            "delivered_at": iso_or_none(getattr(o, "delivered_at", None)),
            "total_amount": float(o.total_amount),
            "item_count": len(o.items),
        }
        for o in orders
    ]
    st.markdown("### Order Filters")
    order_marketplace_options = sorted(
        {str(row["marketplace"]) for row in order_rows if row.get("marketplace")}
    )
    order_status_options = sorted({str(row["status"]) for row in order_rows if row.get("status")})
    normalized_marketplace_filters = normalize_multiselect_values(
        st.session_state.get("orders_filter_marketplaces"),
        order_marketplace_options,
    )
    normalized_status_filters = normalize_multiselect_values(
        st.session_state.get("orders_filter_status"),
        order_status_options,
    )
    if list(st.session_state.get("orders_filter_marketplaces") or []) != normalized_marketplace_filters:
        st.session_state["orders_filter_marketplaces"] = normalized_marketplace_filters
    if list(st.session_state.get("orders_filter_status") or []) != normalized_status_filters:
        st.session_state["orders_filter_status"] = normalized_status_filters
    f1, f2, f3 = st.columns(3)
    with f1:
        order_filter_query = st.text_input("Search External Order ID", key="orders_filter_query")
    with f2:
        order_filter_marketplaces = st.multiselect(
            "Marketplace",
            options=order_marketplace_options,
            key="orders_filter_marketplaces",
        )
    with f3:
        order_filter_status = st.multiselect(
            "Status",
            options=order_status_options,
            key="orders_filter_status",
        )
    effective_filter = render_saved_filter_bar(
        repo=repo,
        scope="orders",
        username=user.username,
        current_filters={
            "query": order_filter_query,
            "marketplaces": order_filter_marketplaces,
            "statuses": order_filter_status,
        },
    )
    q = str(effective_filter.get("query") or "").strip().lower()
    marketplaces = {
        str(v).strip().lower() for v in (effective_filter.get("marketplaces") or []) if str(v).strip()
    }
    statuses = {str(v).strip().lower() for v in (effective_filter.get("statuses") or []) if str(v).strip()}
    filtered_rows = []
    for row in order_rows:
        if q and q not in str(row.get("external_order_id") or "").lower():
            continue
        if marketplaces and str(row.get("marketplace") or "").strip().lower() not in marketplaces:
            continue
        if statuses and str(row.get("status") or "").strip().lower() not in statuses:
            continue
        filtered_rows.append(row)

    filtered_df = pd.DataFrame(filtered_rows)
    st.markdown("### Orders Table + Side Panel")
    table_col, panel_col = st.columns([2, 1])
    with table_col:
        render_table_toolbar(
            df=filtered_df,
            section_key="orders_table",
            export_basename="orders_filtered",
            active_filters={
                "query": q,
                "marketplaces": sorted(marketplaces),
                "statuses": sorted(statuses),
            },
        )
        st.dataframe(filtered_df, use_container_width=True)
        render_standard_row_actions(
            repo,
            entity_type="order",
            rows=filtered_rows,
            id_field="id",
            title="Order Row Actions",
        )

    with panel_col:
        st.markdown("#### Order Detail/Edit")
        if not filtered_rows:
            st.info("No filtered orders available.")
        else:
            orders_by_id = {o.id: o for o in orders}
            select_options = {
                f"#{row['id']} | {row['marketplace']} | {row['external_order_id']}": int(row["id"])
                for row in filtered_rows
            }
            selected_label = st.selectbox(
                "Select Order",
                options=list(select_options.keys()),
                key="orders_side_panel_select",
            )
            selected_order = orders_by_id[select_options[selected_label]]
            st.markdown("##### Sales/Orders Copilot")
            st.caption("AI triage for order mismatches, fulfillment risk, and refund/return guidance.")
            if st.button("Analyze Selected Order", key=f"orders_copilot_analyze_{selected_order.id}"):
                if not ensure_permission(user, "ai_comp_use", "Use Orders Copilot"):
                    st.stop()
                try:
                    order_items = list(selected_order.items or [])
                    order_context = {
                        "order_id": int(selected_order.id),
                        "marketplace": str(selected_order.marketplace or ""),
                        "external_order_id": str(selected_order.external_order_id or ""),
                        "order_status": str(selected_order.order_status or ""),
                        "subtotal_amount": float(selected_order.subtotal_amount or 0),
                        "fees": float(selected_order.fees or 0),
                        "shipping_cost": float(selected_order.shipping_cost or 0),
                        "shipping_label_cost": float(getattr(selected_order, "shipping_label_cost", 0) or 0),
                        "shipping_delta": float(selected_order.shipping_cost or 0)
                        - float(getattr(selected_order, "shipping_label_cost", 0) or 0),
                        "total_amount": float(selected_order.total_amount or 0),
                        "notes": str(selected_order.notes or ""),
                        "item_count": len(order_items),
                        "items": [
                            {
                                "order_item_id": int(i.id),
                                "product_id": int(i.product_id) if i.product_id is not None else None,
                                "listing_id": int(i.listing_id) if i.listing_id is not None else None,
                                "quantity": int(i.quantity or 0),
                                "unit_price": float(i.unit_price or 0),
                                "line_total": float(i.line_total or 0),
                                "line_fees": float(i.line_fees or 0),
                                "line_shipping": float(i.line_shipping or 0),
                            }
                            for i in order_items[:20]
                        ],
                    }
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
                        query=f"Order triage for order #{int(selected_order.id)}",
                        ebay_rows=[],
                        web_rows=[],
                        spot_context={"order": order_context},
                        system_message=system_message,
                        instruction=instruction,
                    )
                    st.session_state[f"orders_copilot_raw_{selected_order.id}"] = str(result.text or "").strip()
                    st.success("Orders copilot analysis complete.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Orders copilot analysis failed: {exc}")
            raw_key = f"orders_copilot_raw_{selected_order.id}"
            raw_val = str(st.session_state.get(raw_key) or "").strip()
            if raw_val:
                with st.expander("Orders Copilot Result", expanded=False):
                    st.code(raw_val, language="json")

            st.markdown("##### Document Draft")
            od1, od2 = st.columns([2, 1])
            with od1:
                orders_doc_type = st.selectbox(
                    "Document Type",
                    options=["invoice", "receipt"],
                    index=0,
                    key=f"orders_documents_doc_type_{selected_order.id}",
                )
            with od2:
                if st.button(
                    "Open in Documents",
                    key=f"orders_to_documents_{selected_order.id}",
                ):
                    handoff_to_documents_draft(
                        source_type="Order",
                        source_id=int(selected_order.id),
                        doc_type=orders_doc_type,
                        handoff_from="orders",
                        repo=repo,
                        actor=user.username,
                    )
            with st.form("orders_side_panel_edit_form"):
                op1, op2 = st.columns(2)
                with op1:
                    edit_marketplace = st.selectbox(
                        "Marketplace",
                        MARKETPLACES,
                        index=MARKETPLACES.index(selected_order.marketplace)
                        if selected_order.marketplace in MARKETPLACES
                        else 0,
                    )
                    edit_external_order_id = st.text_input(
                        "External Order ID",
                        value=selected_order.external_order_id or "",
                    )
                    edit_status = st.selectbox(
                        "Status",
                        ORDER_STATUS_OPTIONS,
                        index=ORDER_STATUS_OPTIONS.index(selected_order.order_status)
                        if selected_order.order_status in set(ORDER_STATUS_OPTIONS)
                        else 0,
                    )
                    sold_default = selected_order.sold_at.date() if selected_order.sold_at else utc_today()
                    edit_sold_date = st.date_input("Order Date", value=sold_default)
                with op2:
                    edit_fees = st.number_input(
                        "Fees",
                        min_value=0.0,
                        value=float(selected_order.fees or 0),
                        step=1.0,
                    )
                    edit_shipping_cost = st.number_input(
                        "Shipping Charged to Buyer",
                        min_value=0.0,
                        value=float(selected_order.shipping_cost or 0),
                        step=1.0,
                    )
                    edit_shipping_label_cost = st.number_input(
                        "Actual Shipping Label Spend (Internal)",
                        min_value=0.0,
                        value=float(getattr(selected_order, "shipping_label_cost", 0) or 0),
                        step=1.0,
                    )
                    edit_subtotal = st.number_input(
                        "Subtotal",
                        min_value=0.0,
                        value=float(selected_order.subtotal_amount or 0),
                        step=1.0,
                    )
                    edit_total = st.number_input(
                        "Total",
                        min_value=0.0,
                        value=float(selected_order.total_amount or 0),
                        step=1.0,
                    )
                sh1, sh2 = st.columns(2)
                with sh1:
                    edit_shipping_provider = st.text_input(
                        "Shipping Provider",
                        value=str(getattr(selected_order, "shipping_provider", "") or ""),
                    )
                    edit_shipping_service = st.text_input(
                        "Shipping Service",
                        value=str(getattr(selected_order, "shipping_service", "") or ""),
                    )
                    edit_tracking_number = st.text_input(
                        "Tracking Number",
                        value=str(getattr(selected_order, "tracking_number", "") or ""),
                    )
                    edit_tracking_status = st.text_input(
                        "Tracking Status",
                        value=str(getattr(selected_order, "tracking_status", "") or ""),
                    )
                with sh2:
                    existing_shipped_at = getattr(selected_order, "shipped_at", None)
                    existing_delivered_at = getattr(selected_order, "delivered_at", None)
                    shipped_enabled = st.checkbox(
                        "Set Shipped Date",
                        value=existing_shipped_at is not None,
                    )
                    edit_shipped_date = st.date_input(
                        "Shipped Date",
                        value=(existing_shipped_at.date() if existing_shipped_at is not None else utc_today()),
                        disabled=not shipped_enabled,
                    )
                    delivered_enabled = st.checkbox(
                        "Set Delivered Date",
                        value=existing_delivered_at is not None,
                    )
                    edit_delivered_date = st.date_input(
                        "Delivered Date",
                        value=(existing_delivered_at.date() if existing_delivered_at is not None else utc_today()),
                        disabled=not delivered_enabled,
                    )
                edit_notes = st.text_area("Notes", value=selected_order.notes or "")
                formatted_sync_note = format_ebay_sync_note_for_customer(selected_order.notes or "")
                if formatted_sync_note and formatted_sync_note != str(selected_order.notes or "").strip():
                    st.caption("Parsed eBay sync note (customer/shipping)")
                    st.code(formatted_sync_note, language="text")
                save_side_panel = st.form_submit_button("Save Order Changes")

            if save_side_panel:
                if not ensure_permission(user, "update", "Update Order"):
                    st.stop()
                try:
                    repo.update_order(
                        selected_order.id,
                        {
                            "marketplace": edit_marketplace,
                            "external_order_id": edit_external_order_id.strip(),
                            "order_status": edit_status,
                            "sold_at": datetime.combine(edit_sold_date, datetime.min.time()),
                            "fees": to_decimal(edit_fees),
                            "shipping_cost": to_decimal(edit_shipping_cost),
                            "shipping_label_cost": to_decimal(edit_shipping_label_cost),
                            "shipping_label_currency": str(
                                getattr(selected_order, "shipping_label_currency", "USD") or "USD"
                            ).strip().upper(),
                            "shipping_provider": edit_shipping_provider.strip(),
                            "shipping_service": edit_shipping_service.strip(),
                            "tracking_number": edit_tracking_number.strip(),
                            "tracking_status": edit_tracking_status.strip(),
                            "shipped_at": (
                                datetime.combine(edit_shipped_date, datetime.min.time())
                                if shipped_enabled
                                else None
                            ),
                            "delivered_at": (
                                datetime.combine(edit_delivered_date, datetime.min.time())
                                if delivered_enabled
                                else None
                            ),
                            "subtotal_amount": to_decimal(edit_subtotal),
                            "total_amount": to_decimal(edit_total),
                            "notes": edit_notes.strip(),
                        },
                        actor=user.username,
                    )
                    st.success("Order updated.")
                    st.rerun()
                except (ValueError, IntegrityError) as exc:
                    repo.db.rollback()
                    st.error(str(exc))

    st.caption("Quick status updates are available from the side-panel edit form above.")

    load_order_items_table = st.checkbox(
        "Load Order Items Table (slower)",
        value=False,
        key="orders_load_items_table",
    )
    if load_order_items_table:
        order_items = repo.list_order_items()
        if order_items:
            st.markdown("### Order Items")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": i.id,
                            "order_id": i.order_id,
                            "product_id": i.product_id,
                            "listing_id": i.listing_id,
                            "sku": i.product.sku if i.product else None,
                            "quantity": i.quantity,
                            "unit_price": float(i.unit_price),
                            "line_total": float(i.line_total),
                            "line_fees": float(i.line_fees),
                            "line_shipping": float(i.line_shipping),
                        }
                        for i in order_items
                    ]
                ),
                use_container_width=True,
            )
        else:
            st.caption("No order items found.")
    else:
        st.caption("Enable order-item table loading to query and render all order lines.")
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="orders",
        section_title="Workspace Feedback: Orders",
    )
