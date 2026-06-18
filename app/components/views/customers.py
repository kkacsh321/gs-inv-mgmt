from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import iso_or_none
from app.components.views.entity_ops import render_standard_row_actions
from app.components.views.shared import render_help_panel, render_table_toolbar
from app.repository import InventoryRepository


def _days_since_datetime(value, *, now: datetime | None = None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    observed = value
    if observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0, int((current.date() - observed.date()).days))


def _customer_follow_up_status(order_count: int, days_since_last_order: int | None) -> str:
    if int(order_count or 0) <= 0 or days_since_last_order is None:
        return "No orders"
    if days_since_last_order < 30:
        return "Recent"
    if days_since_last_order < 90:
        return "Warm"
    return "Dormant 90d+"


def _customer_contact_summary(row: dict) -> str:
    parts = [
        str(row.get("display_name") or row.get("shipping_name") or row.get("ebay_username") or "").strip(),
        str(row.get("primary_email") or "").strip(),
        str(row.get("shipping_address") or "").strip(),
    ]
    return " | ".join(part for part in parts if part)


def _filter_order_rows(rows: list[dict], *, query: str = "", statuses: list[str] | None = None) -> list[dict]:
    query_lower = str(query or "").strip().lower()
    status_set = {str(status).strip().lower() for status in (statuses or []) if str(status).strip()}
    filtered: list[dict] = []
    for row in rows:
        if status_set and str(row.get("status") or "").strip().lower() not in status_set:
            continue
        haystack = " ".join(
            str(row.get(field) or "")
            for field in (
                "marketplace",
                "external_order_id",
                "status",
                "sold_at",
                "items",
                "shipping_service",
                "tracking_status",
                "tracking_number",
                "ship_to",
            )
        ).lower()
        if query_lower and query_lower not in haystack:
            continue
        filtered.append(row)
    return filtered


def _customer_row(customer) -> dict:
    notes = str(getattr(customer, "notes", "") or "").strip()
    order_count = int(getattr(customer, "order_count", 0) or 0)
    days_since_last_order = _days_since_datetime(getattr(customer, "last_order_at", None))
    shipping_address_parts = [
        str(getattr(customer, "shipping_address_line1", "") or "").strip(),
        str(getattr(customer, "shipping_address_line2", "") or "").strip(),
        ", ".join(
            part
            for part in [
                str(getattr(customer, "shipping_city", "") or "").strip(),
                str(getattr(customer, "shipping_state", "") or "").strip(),
                str(getattr(customer, "shipping_postal_code", "") or "").strip(),
            ]
            if part
        ),
        str(getattr(customer, "shipping_country", "") or "").strip(),
    ]
    return {
        "id": int(getattr(customer, "id", 0) or 0),
        "marketplace": str(getattr(customer, "marketplace", "") or ""),
        "ebay_username": str(getattr(customer, "ebay_username", "") or ""),
        "display_name": str(getattr(customer, "display_name", "") or ""),
        "primary_email": str(getattr(customer, "primary_email", "") or ""),
        "shipping_name": str(getattr(customer, "shipping_name", "") or ""),
        "shipping_address_line1": str(getattr(customer, "shipping_address_line1", "") or ""),
        "shipping_address_line2": str(getattr(customer, "shipping_address_line2", "") or ""),
        "shipping_city": str(getattr(customer, "shipping_city", "") or ""),
        "shipping_state": str(getattr(customer, "shipping_state", "") or ""),
        "shipping_postal_code": str(getattr(customer, "shipping_postal_code", "") or ""),
        "shipping_country": str(getattr(customer, "shipping_country", "") or ""),
        "shipping_address": " | ".join(part for part in shipping_address_parts if part),
        "order_count": order_count,
        "total_spend": float(getattr(customer, "total_spend", 0) or 0),
        "is_repeat_buyer": bool(getattr(customer, "is_repeat_buyer", False)),
        "follow_up_status": _customer_follow_up_status(order_count, days_since_last_order),
        "days_since_last_order": days_since_last_order,
        "has_internal_notes": bool(notes),
        "notes_preview": notes[:180],
        "first_order_at": iso_or_none(getattr(customer, "first_order_at", None)),
        "last_order_at": iso_or_none(getattr(customer, "last_order_at", None)),
    }


def _order_items_summary(order, *, max_items: int = 3) -> str:
    parts: list[str] = []
    for item in list(getattr(order, "items", []) or [])[: max(1, int(max_items or 3))]:
        qty = int(getattr(item, "quantity", 0) or 0)
        product = getattr(item, "product", None)
        listing = getattr(item, "listing", None)
        label = (
            str(getattr(product, "sku", "") or "").strip()
            or str(getattr(product, "title", "") or "").strip()
            or str(getattr(listing, "listing_title", "") or "").strip()
            or f"item#{int(getattr(item, 'id', 0) or 0)}"
        )
        if qty > 1:
            label = f"{qty}x {label}"
        parts.append(label)
    total_items = len(list(getattr(order, "items", []) or []))
    if total_items > len(parts):
        parts.append(f"+{total_items - len(parts)} more")
    return "; ".join(parts)


def _order_row(order) -> dict:
    return {
        "id": int(getattr(order, "id", 0) or 0),
        "marketplace": str(getattr(order, "marketplace", "") or ""),
        "external_order_id": str(getattr(order, "external_order_id", "") or ""),
        "status": str(getattr(order, "order_status", "") or ""),
        "sold_at": iso_or_none(getattr(order, "sold_at", None)),
        "subtotal_amount": float(getattr(order, "subtotal_amount", 0) or 0),
        "shipping_charged": float(getattr(order, "shipping_cost", 0) or 0),
        "label_spend": float(getattr(order, "shipping_label_cost", 0) or 0),
        "total_amount": float(getattr(order, "total_amount", 0) or 0),
        "item_count": len(list(getattr(order, "items", []) or [])),
        "items": _order_items_summary(order),
        "shipping_service": str(getattr(order, "shipping_service", "") or ""),
        "tracking_status": str(getattr(order, "tracking_status", "") or ""),
        "tracking_number": str(getattr(order, "tracking_number", "") or ""),
        "ship_to": ", ".join(
            part
            for part in [
                str(getattr(order, "ship_to_city", "") or "").strip(),
                str(getattr(order, "ship_to_state", "") or "").strip(),
                str(getattr(order, "ship_to_postal_code", "") or "").strip(),
                str(getattr(order, "ship_to_country", "") or "").strip(),
            ]
            if part
        ),
    }


def render_customers(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Customers")
    st.caption("Lookup marketplace customers, repeat buyers, lifetime spend, and past order status.")
    render_help_panel(
        section_title="Customers",
        goal="See buyer rollups from marketplace orders and quickly inspect past purchases.",
        steps=[
            "Search by eBay username, name, email, city, state, postal code, or marketplace.",
            "Use repeat-buyer and marketplace filters to focus customer follow-up.",
            "Open a customer in the detail panel to review purchase history and fulfillment status.",
            "Run the backfill action if historical orders are not linked yet.",
        ],
        roadmap_phase="v1.0 Customer Intelligence",
    )

    c_backfill, c_note = st.columns([1, 3])
    with c_backfill:
        if st.button("Backfill Customers", key="customers_backfill_btn"):
            if not ensure_permission(user, "update", "Backfill Customers"):
                st.stop()
            try:
                result = repo.backfill_customers_from_orders(actor=user.username)
                st.success(
                    f"Backfilled {int(result.get('created_customers') or 0)} customer(s); "
                    f"linked {int(result.get('linked_orders') or 0)} order(s)."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Customer backfill failed: {exc}")
    with c_note:
        st.caption("Customer records are created from order buyer identity; eBay username is preferred when available.")

    customers = repo.list_customers()
    if not customers:
        st.info("No customers yet. Import orders or run backfill after orders exist.")
        return

    rows = [_customer_row(customer) for customer in customers]
    marketplace_options = sorted({row["marketplace"] for row in rows if row["marketplace"]})
    f1, f2, f3, f4, f5 = st.columns([2, 1, 1, 1, 1])
    with f1:
        query = st.text_input("Search Customers", key="customers_search_query")
    with f2:
        selected_marketplaces = st.multiselect(
            "Marketplace",
            options=marketplace_options,
            key="customers_marketplace_filter",
        )
    with f3:
        repeat_filter = st.selectbox(
            "Buyer Type",
            options=["All", "Repeat buyers", "First observed"],
            key="customers_repeat_filter",
        )
    with f4:
        notes_filter = st.selectbox(
            "Notes",
            options=["All", "Has notes", "No notes"],
            key="customers_notes_filter",
        )
    with f5:
        follow_up_filter = st.selectbox(
            "Follow-Up",
            options=["All", "Recent", "Warm", "Dormant 90d+", "No orders"],
            key="customers_follow_up_filter",
        )

    query_lower = str(query or "").strip().lower()
    marketplace_set = {str(v).strip().lower() for v in selected_marketplaces if str(v).strip()}
    filtered_rows: list[dict] = []
    for row in rows:
        haystack = " ".join(
            str(row.get(field) or "")
            for field in (
                "marketplace",
                "ebay_username",
                "display_name",
                "primary_email",
                "shipping_name",
                "shipping_city",
                "shipping_state",
                "shipping_postal_code",
                "shipping_country",
                "shipping_address",
                "follow_up_status",
                "notes_preview",
            )
        ).lower()
        if query_lower and query_lower not in haystack:
            continue
        if marketplace_set and str(row.get("marketplace") or "").strip().lower() not in marketplace_set:
            continue
        if repeat_filter == "Repeat buyers" and not bool(row.get("is_repeat_buyer")):
            continue
        if repeat_filter == "First observed" and bool(row.get("is_repeat_buyer")):
            continue
        if notes_filter == "Has notes" and not bool(row.get("has_internal_notes")):
            continue
        if notes_filter == "No notes" and bool(row.get("has_internal_notes")):
            continue
        if follow_up_filter != "All" and str(row.get("follow_up_status") or "") != follow_up_filter:
            continue
        filtered_rows.append(row)

    total_customers = len(filtered_rows)
    repeat_customers = sum(1 for row in filtered_rows if bool(row.get("is_repeat_buyer")))
    total_orders = sum(int(row.get("order_count") or 0) for row in filtered_rows)
    total_spend = sum(float(row.get("total_spend") or 0) for row in filtered_rows)
    dormant_customers = sum(1 for row in filtered_rows if row.get("follow_up_status") == "Dormant 90d+")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Customers", total_customers)
    m2.metric("Repeat Buyers", repeat_customers)
    m3.metric("Dormant 90d+", dormant_customers)
    m4.metric("Lifetime Spend", f"${total_spend:,.2f}")
    st.caption(f"Filtered order count: {total_orders}")

    st.markdown("### Customer Directory")
    table_col, detail_col = st.columns([2, 1])
    with table_col:
        df = pd.DataFrame(filtered_rows)
        render_table_toolbar(
            df=df,
            section_key="customers_table",
            export_basename="customers_filtered",
            active_filters={
                "query": query_lower,
                "marketplaces": sorted(marketplace_set),
                "buyer_type": repeat_filter,
                "notes": notes_filter,
                "follow_up": follow_up_filter,
            },
        )
        st.dataframe(df, use_container_width=True, hide_index=True)
        render_standard_row_actions(
            repo,
            entity_type="customer",
            rows=filtered_rows,
            id_field="id",
            title="Customer Row Actions",
        )

    with detail_col:
        st.markdown("#### Customer Detail")
        if not filtered_rows:
            st.info("No matching customers.")
            return
        customer_by_id = {int(customer.id): customer for customer in customers}
        options = {
            (
                f"#{row['id']} | "
                f"{row.get('ebay_username') or row.get('display_name') or row.get('primary_email') or 'Customer'} "
                f"| {int(row.get('order_count') or 0)} order(s)"
            ): int(row["id"])
            for row in filtered_rows
        }
        selected_label = st.selectbox(
            "Select Customer",
            options=list(options.keys()),
            key="customers_detail_select",
        )
        selected_customer = customer_by_id.get(options[selected_label])
        if selected_customer is None:
            st.warning("Selected customer no longer exists.")
            return
        selected_row = _customer_row(selected_customer)
        c1, c2 = st.columns(2)
        c1.metric("Repeat Buyer", "Yes" if selected_row["is_repeat_buyer"] else "No")
        c2.metric("Orders", selected_row["order_count"])
        st.metric("Lifetime Spend", f"${selected_row['total_spend']:,.2f}")
        st.caption(
            f"Follow-up: {selected_row['follow_up_status']}"
            + (
                f" ({selected_row['days_since_last_order']} day(s) since last order)"
                if selected_row["days_since_last_order"] is not None
                else ""
            )
        )
        st.caption(
            " | ".join(
                part
                for part in [
                    selected_row["ebay_username"],
                    selected_row["display_name"],
                    selected_row["primary_email"],
                    selected_row["shipping_postal_code"],
                ]
                if part
            )
        )
        contact_summary = _customer_contact_summary(selected_row)
        if contact_summary:
            st.caption(f"Contact: {contact_summary}")
        with st.form(f"customers_notes_form_{int(selected_customer.id)}"):
            internal_notes = st.text_area(
                "Internal Customer Notes",
                value=str(getattr(selected_customer, "notes", "") or ""),
                height=140,
                help="Internal-only notes for customer support, follow-up, preferences, or repeat-buyer context.",
            )
            if st.form_submit_button("Save Customer Notes"):
                if not ensure_permission(user, "update", "Update Customer Notes"):
                    st.stop()
                try:
                    repo.update_customer(
                        int(selected_customer.id),
                        {"notes": internal_notes},
                        actor=user.username,
                    )
                    st.success("Customer notes saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Customer notes save failed: {exc}")
        st.markdown("##### Past Purchases")
        orders = repo.list_orders_for_customer(int(selected_customer.id))
        order_rows = [_order_row(order) for order in orders]
        if not order_rows:
            st.info("No linked orders for this customer yet.")
        else:
            order_status_options = sorted({str(row.get("status") or "") for row in order_rows if row.get("status")})
            of1, of2 = st.columns([2, 1])
            with of1:
                order_query = st.text_input(
                    "Search Purchases",
                    key=f"customers_order_search_{int(selected_customer.id)}",
                    placeholder="Order ID, item, tracking, destination...",
                )
            with of2:
                selected_statuses = st.multiselect(
                    "Order Status",
                    options=order_status_options,
                    key=f"customers_order_status_filter_{int(selected_customer.id)}",
                )
            filtered_order_rows = _filter_order_rows(
                order_rows,
                query=order_query,
                statuses=list(selected_statuses),
            )
            if not filtered_order_rows:
                st.info("No linked orders match the selected purchase filters.")
                return
            orders_df = pd.DataFrame(filtered_order_rows)
            status_rows = (
                orders_df.groupby("status", dropna=False)
                .agg(order_count=("id", "count"), total_amount=("total_amount", "sum"))
                .reset_index()
                .sort_values(["order_count", "total_amount"], ascending=[False, False])
                .to_dict("records")
            )
            if status_rows:
                st.caption("Status breakdown")
                st.dataframe(pd.DataFrame(status_rows), use_container_width=True, hide_index=True)
            st.dataframe(orders_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Customer Orders CSV",
                data=orders_df.to_csv(index=False).encode("utf-8"),
                file_name=f"customer_{int(selected_customer.id)}_orders.csv",
                mime="text/csv",
                key=f"customers_orders_csv_{int(selected_customer.id)}",
            )
