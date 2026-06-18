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


def _order_actuals_by_id(repo: InventoryRepository, orders: list) -> dict[int, dict]:
    sold_dates = [o.sold_at for o in orders if getattr(o, "sold_at", None) is not None]
    if not sold_dates or not hasattr(repo, "report_orders_rows"):
        return {}
    try:
        rows = repo.report_orders_rows(start_dt=min(sold_dates), end_dt=max(sold_dates))
    except Exception:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
        return {}
    return {
        int(row.get("order_id") or row.get("id") or 0): row
        for row in rows
        if int(row.get("order_id") or row.get("id") or 0) > 0
    }


def _order_field_shipping_delta(order) -> float:
    return float(getattr(order, "shipping_cost", 0.0) or 0.0) - float(
        getattr(order, "shipping_label_cost", 0.0) or 0.0
    )


def _order_field_net_before_cogs(order) -> float:
    return (
        float(getattr(order, "subtotal_amount", 0.0) or 0.0)
        + float(getattr(order, "shipping_cost", 0.0) or 0.0)
        - float(getattr(order, "fees", 0.0) or 0.0)
        - float(getattr(order, "shipping_label_cost", 0.0) or 0.0)
    )


def _order_actual_context(order, actuals: dict | None = None) -> dict[str, object]:
    actual = actuals or {}
    return {
        "actual_fee": float(actual.get("actual_fee", float(getattr(order, "fees", 0.0) or 0.0)) or 0.0),
        "actual_fee_source": str(actual.get("actual_fee_source", "order_fees_field") or ""),
        "actual_shipping_label_cost": float(
            actual.get(
                "actual_shipping_label_cost",
                float(getattr(order, "shipping_label_cost", 0.0) or 0.0),
            )
            or 0.0
        ),
        "actual_shipping_delta": float(
            actual.get(
                "actual_shipping_delta_charged_minus_label",
                _order_field_shipping_delta(order),
            )
            or 0.0
        ),
        "actual_shipping_source": str(
            actual.get("actual_shipping_source", "order_shipping_label_field") or ""
        ),
        "actual_net_before_cogs": float(
            actual.get(
                "actual_net_before_cogs",
                _order_field_net_before_cogs(order),
            )
            or 0.0
        ),
        "actual_net_source": "order_actuals_rollup" if actual else "order_fields_fallback",
    }


def _customer_notes_preview(customer, *, max_chars: int = 220) -> str:
    notes = str(getattr(customer, "notes", "") or "").strip()
    if not notes:
        return ""
    limit = max(20, int(max_chars or 220))
    if len(notes) <= limit:
        return notes
    return notes[: max(0, limit - 3)].rstrip() + "..."


def _customer_identity_summary(customer) -> str:
    if customer is None:
        return ""
    parts = [
        str(getattr(customer, "ebay_username", "") or "").strip(),
        str(getattr(customer, "display_name", "") or "").strip(),
        str(getattr(customer, "primary_email", "") or "").strip(),
        str(getattr(customer, "shipping_postal_code", "") or "").strip(),
    ]
    return " | ".join(part for part in parts if part)


def _customer_order_context(customer) -> dict[str, object]:
    if customer is None:
        return {
            "customer_id": None,
            "repeat_buyer": False,
            "order_count": 0,
            "total_spend": 0.0,
            "identity_summary": "",
            "has_internal_notes": False,
            "notes_preview": "",
        }
    notes_preview = _customer_notes_preview(customer)
    return {
        "customer_id": int(getattr(customer, "id", 0) or 0),
        "repeat_buyer": bool(getattr(customer, "is_repeat_buyer", False)),
        "order_count": int(getattr(customer, "order_count", 0) or 0),
        "total_spend": float(getattr(customer, "total_spend", 0) or 0),
        "identity_summary": _customer_identity_summary(customer),
        "has_internal_notes": bool(notes_preview),
        "notes_preview": notes_preview,
    }


def _order_customer_context(order, customer_by_id: dict[int, object]) -> dict[str, object]:
    return _customer_order_context(customer_by_id.get(int(getattr(order, "customer_id", 0) or 0)))


def _filter_order_rows(
    rows: list[dict],
    *,
    query: str = "",
    marketplaces: list[str] | None = None,
    statuses: list[str] | None = None,
    buyer_type: str = "All",
    customer_notes: str = "All",
) -> list[dict]:
    q = str(query or "").strip().lower()
    marketplace_set = {str(v).strip().lower() for v in (marketplaces or []) if str(v).strip()}
    status_set = {str(v).strip().lower() for v in (statuses or []) if str(v).strip()}
    buyer_type = str(buyer_type or "All")
    customer_notes = str(customer_notes or "All")
    filtered: list[dict] = []
    for row in rows:
        haystack = " ".join(
            str(row.get(field) or "")
            for field in (
                "external_order_id",
                "buyer_username",
                "buyer_name",
                "buyer_email",
                "customer_notes_preview",
            )
        ).lower()
        if q and q not in haystack:
            continue
        if marketplace_set and str(row.get("marketplace") or "").strip().lower() not in marketplace_set:
            continue
        if status_set and str(row.get("status") or "").strip().lower() not in status_set:
            continue
        if buyer_type == "Repeat buyers" and not bool(row.get("repeat_buyer")):
            continue
        if buyer_type == "First observed" and bool(row.get("repeat_buyer")):
            continue
        if customer_notes == "Has notes" and not bool(row.get("customer_has_internal_notes")):
            continue
        if customer_notes == "No notes" and bool(row.get("customer_has_internal_notes")):
            continue
        filtered.append(row)
    return filtered


def _update_linked_customer_notes(repo: InventoryRepository, customer_id: int, notes: str, *, actor: str):
    return repo.update_customer(int(customer_id), {"notes": str(notes or "")}, actor=actor)


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
        st.markdown("**Customer / Ship-To**")
        b1, b2, b3 = st.columns(3)
        with b1:
            buyer_username = st.text_input("Buyer Username")
            buyer_name = st.text_input("Buyer / Ship-To Name")
        with b2:
            buyer_email = st.text_input("Buyer Email")
            ship_to_city = st.text_input("Ship-To City")
        with b3:
            ship_to_state = st.text_input("Ship-To State")
            ship_to_postal_code = st.text_input("Ship-To Postal Code")
            ship_to_country = st.text_input("Ship-To Country", value="US")
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
                    buyer_username=buyer_username,
                    buyer_name=buyer_name,
                    buyer_email=buyer_email,
                    ship_to_city=ship_to_city,
                    ship_to_state=ship_to_state,
                    ship_to_postal_code=ship_to_postal_code,
                    ship_to_country=ship_to_country,
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

    customers = repo.list_customers() if hasattr(repo, "list_customers") else []
    customer_by_id = {int(c.id): c for c in customers}
    if st.button("Backfill Customers From Existing Orders", key="orders_backfill_customers_btn"):
        if not ensure_permission(user, "update", "Backfill Customers"):
            st.stop()
        try:
            result = repo.backfill_customers_from_orders(actor=user.username)
            st.success(
                "Customer backfill complete: "
                f"{int(result.get('created_customers') or 0)} customer(s) created, "
                f"{int(result.get('linked_orders') or 0)} order(s) linked."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Customer backfill failed: {exc}")

    orders = repo.list_orders()
    if not orders:
        st.info("No orders yet.")
        return

    actuals_by_order_id = _order_actuals_by_id(repo, orders)
    order_rows = [
        {
            "id": o.id,
            "marketplace": o.marketplace,
            "external_order_id": o.external_order_id,
            "status": o.order_status,
            "sold_at": iso_or_none(o.sold_at),
            "customer_id": int(getattr(o, "customer_id", 0) or 0),
            "buyer_username": str(getattr(o, "buyer_username", "") or ""),
            "buyer_name": str(getattr(o, "buyer_name", "") or ""),
            "buyer_email": str(getattr(o, "buyer_email", "") or ""),
            "repeat_buyer": customer_context["repeat_buyer"],
            "customer_order_count": customer_context["order_count"],
            "customer_total_spend": customer_context["total_spend"],
            "customer_has_internal_notes": customer_context["has_internal_notes"],
            "customer_notes_preview": customer_context["notes_preview"],
            "ship_to_city": str(getattr(o, "ship_to_city", "") or ""),
            "ship_to_state": str(getattr(o, "ship_to_state", "") or ""),
            "ship_to_postal_code": str(getattr(o, "ship_to_postal_code", "") or ""),
            "ship_to_country": str(getattr(o, "ship_to_country", "") or ""),
            "subtotal_amount": float(o.subtotal_amount),
            "fees": float(o.fees),
            "actual_fee": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))["actual_fee"],
            "actual_fee_source": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))["actual_fee_source"],
            "shipping_cost": float(o.shipping_cost),
            "shipping_label_cost": float(getattr(o, "shipping_label_cost", 0) or 0),
            "actual_shipping_label_cost": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))[
                "actual_shipping_label_cost"
            ],
            "shipping_delta": _order_field_shipping_delta(o),
            "actual_shipping_delta": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))[
                "actual_shipping_delta"
            ],
            "actual_shipping_source": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))[
                "actual_shipping_source"
            ],
            "actual_net_before_cogs": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))[
                "actual_net_before_cogs"
            ],
            "actual_net_source": _order_actual_context(o, actuals_by_order_id.get(int(o.id), {}))[
                "actual_net_source"
            ],
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
        for customer_context in [_order_customer_context(o, customer_by_id)]
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
    f1, f2, f3, f4, f5 = st.columns([2, 1, 1, 1, 1])
    with f1:
        order_filter_query = st.text_input("Search Order / Buyer", key="orders_filter_query")
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
    with f4:
        order_filter_buyer_type = st.selectbox(
            "Buyer Type",
            options=["All", "Repeat buyers", "First observed"],
            key="orders_filter_buyer_type",
        )
    with f5:
        order_filter_customer_notes = st.selectbox(
            "Customer Notes",
            options=["All", "Has notes", "No notes"],
            key="orders_filter_customer_notes",
        )
    effective_filter = render_saved_filter_bar(
        repo=repo,
        scope="orders",
        username=user.username,
        current_filters={
            "query": order_filter_query,
            "marketplaces": order_filter_marketplaces,
            "statuses": order_filter_status,
            "buyer_type": order_filter_buyer_type,
            "customer_notes": order_filter_customer_notes,
        },
    )
    q = str(effective_filter.get("query") or "").strip().lower()
    marketplaces = [
        str(v).strip() for v in (effective_filter.get("marketplaces") or []) if str(v).strip()
    ]
    statuses = [str(v).strip() for v in (effective_filter.get("statuses") or []) if str(v).strip()]
    buyer_type_filter = str(effective_filter.get("buyer_type") or "All")
    customer_notes_filter = str(effective_filter.get("customer_notes") or "All")
    filtered_rows = _filter_order_rows(
        order_rows,
        query=q,
        marketplaces=marketplaces,
        statuses=statuses,
        buyer_type=buyer_type_filter,
        customer_notes=customer_notes_filter,
    )

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
                "marketplaces": sorted({str(v).strip().lower() for v in marketplaces if str(v).strip()}),
                "statuses": sorted({str(v).strip().lower() for v in statuses if str(v).strip()}),
                "buyer_type": buyer_type_filter,
                "customer_notes": customer_notes_filter,
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
            selected_actuals = actuals_by_order_id.get(int(selected_order.id), {})
            selected_customer = customer_by_id.get(int(getattr(selected_order, "customer_id", 0) or 0))
            selected_customer_context = _customer_order_context(selected_customer)
            if selected_customer is not None:
                st.markdown("##### Customer")
                c1, c2 = st.columns(2)
                with c1:
                    st.metric(
                        "Repeat Buyer",
                        "Yes" if bool(getattr(selected_customer, "is_repeat_buyer", False)) else "No",
                    )
                with c2:
                    st.metric("Orders", int(getattr(selected_customer, "order_count", 0) or 0))
                st.caption(
                    str(selected_customer_context.get("identity_summary") or "")
                )
                st.caption(
                    f"Lifetime spend: ${float(selected_customer_context.get('total_spend') or 0):,.2f}"
                )
                if bool(selected_customer_context.get("has_internal_notes")):
                    with st.expander("Internal Customer Notes", expanded=False):
                        st.write(str(selected_customer_context.get("notes_preview") or ""))
                with st.expander("Edit Internal Customer Notes", expanded=False):
                    with st.form(f"orders_customer_notes_form_{int(selected_customer.id)}"):
                        customer_notes = st.text_area(
                            "Internal Customer Notes",
                            value=str(getattr(selected_customer, "notes", "") or ""),
                            height=120,
                            help="Internal-only notes saved on the linked customer record.",
                        )
                        if st.form_submit_button("Save Customer Notes"):
                            if not ensure_permission(user, "update", "Update Customer Notes"):
                                st.stop()
                            try:
                                _update_linked_customer_notes(
                                    repo,
                                    int(selected_customer.id),
                                    customer_notes,
                                    actor=user.username,
                                )
                                st.success("Customer notes saved.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Customer notes save failed: {exc}")
            elif str(getattr(selected_order, "buyer_username", "") or getattr(selected_order, "buyer_email", "") or "").strip():
                st.caption("Customer details exist on this order but have not been linked. Run customer backfill.")
            st.markdown("##### Sales/Orders Copilot")
            st.caption("AI triage for order mismatches, fulfillment risk, and refund/return guidance.")
            if st.button("Analyze Selected Order", key=f"orders_copilot_analyze_{selected_order.id}"):
                if not ensure_permission(user, "ai_comp_use", "Use Orders Copilot"):
                    st.stop()
                try:
                    order_items = list(selected_order.items or [])
                    selected_actual_context = _order_actual_context(selected_order, selected_actuals)
                    order_context = {
                        "order_id": int(selected_order.id),
                        "marketplace": str(selected_order.marketplace or ""),
                        "external_order_id": str(selected_order.external_order_id or ""),
                        "order_status": str(selected_order.order_status or ""),
                        "subtotal_amount": float(selected_order.subtotal_amount or 0),
                        "fees": float(selected_order.fees or 0),
                        "actual_fee": float(selected_actual_context.get("actual_fee") or 0),
                        "actual_fee_source": str(selected_actual_context.get("actual_fee_source") or ""),
                        "shipping_cost": float(selected_order.shipping_cost or 0),
                        "shipping_label_cost": float(getattr(selected_order, "shipping_label_cost", 0) or 0),
                        "actual_shipping_label_cost": float(
                            selected_actual_context.get("actual_shipping_label_cost") or 0
                        ),
                        "shipping_delta": _order_field_shipping_delta(selected_order),
                        "actual_shipping_delta": float(selected_actual_context.get("actual_shipping_delta") or 0),
                        "actual_shipping_source": str(selected_actual_context.get("actual_shipping_source") or ""),
                        "actual_net_before_cogs": float(
                            selected_actual_context.get("actual_net_before_cogs") or 0
                        ),
                        "actual_net_source": str(selected_actual_context.get("actual_net_source") or ""),
                        "customer": selected_customer_context,
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
