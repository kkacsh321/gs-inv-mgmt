from datetime import datetime

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import build_product_options, iso_or_none, to_decimal
from app.components.views.shared import MARKETPLACES, render_help_panel
from app.repository import InventoryRepository
from app.utils.time import utc_today


def render_returns(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Returns")
    st.caption("Record return/refund outcomes and optionally restock inventory.")
    render_help_panel(
        section_title="Returns",
        goal="Track refunds and disposition while keeping inventory and financial records consistent.",
        steps=[
            "Create a return linked to sale/order/product where available.",
            "Set disposition and restock flag based on inspection outcome.",
            "Use status updates to track requested, received, processed, closed.",
            "Restock actions automatically create inventory movement events.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    sales = repo.list_sales()
    orders = repo.list_orders()
    products = repo.list_products()

    sale_opts = {"None": None, **{f"#{s.id} | {s.marketplace} | {s.external_order_id or 'no-order-id'}": s.id for s in sales}}
    order_opts = {"None": None, **{f"#{o.id} | {o.marketplace} | {o.external_order_id}": o.id for o in orders}}
    product_opts = build_product_options(products, include_none=True, include_id=True)

    with st.form("create_return_form", clear_on_submit=True):
        marketplace = st.selectbox("Marketplace", MARKETPLACES)
        sale_key = st.selectbox("Sale (Optional)", list(sale_opts.keys()))
        order_key = st.selectbox("Order (Optional)", list(order_opts.keys()))
        product_key = st.selectbox("Product (Optional)", list(product_opts.keys()))
        external_return_id = st.text_input("External Return ID (Optional)")
        return_status = st.selectbox(
            "Return Status",
            ["requested", "received", "processed", "closed", "cancelled"],
        )
        disposition = st.selectbox(
            "Disposition",
            ["pending", "restock", "damaged", "scrap", "return_to_vendor", "other"],
        )
        reason = st.text_input("Reason", placeholder="not as described / damaged / buyer changed mind / etc")
        c1, c2, c3 = st.columns(3)
        with c1:
            quantity = st.number_input("Quantity", min_value=1, value=1, step=1)
        with c2:
            refund_amount = st.number_input("Refund Amount", min_value=0.0, value=0.0, step=1.0)
        with c3:
            refund_fees = st.number_input("Refund Fees", min_value=0.0, value=0.0, step=1.0)
        refund_shipping = st.number_input("Refund Shipping", min_value=0.0, value=0.0, step=1.0)
        returned_date = st.date_input("Returned Date", value=utc_today())
        processed_enabled = st.checkbox("Has Processed Date", value=False)
        processed_date = st.date_input("Processed Date", value=utc_today(), disabled=not processed_enabled)
        restocked = st.checkbox("Restocked Back Into Inventory", value=False)
        notes = st.text_area("Notes")
        actor = user.username

        if st.form_submit_button("Create Return"):
            if not ensure_permission(user, "create", "Create Return"):
                st.stop()
            repo.create_return(
                marketplace=marketplace,
                sale_id=sale_opts[sale_key],
                order_id=order_opts[order_key],
                product_id=product_opts[product_key],
                external_return_id=external_return_id.strip(),
                return_status=return_status,
                reason=reason.strip(),
                disposition=disposition,
                quantity=int(quantity),
                refund_amount=to_decimal(refund_amount),
                refund_fees=to_decimal(refund_fees),
                refund_shipping=to_decimal(refund_shipping),
                restocked=restocked,
                returned_at=datetime.combine(returned_date, datetime.min.time()),
                processed_at=datetime.combine(processed_date, datetime.min.time()) if processed_enabled else None,
                notes=notes.strip(),
                actor=actor,
            )
            st.success("Return created.")

    returns = repo.list_returns()
    if not returns:
        st.info("No returns yet.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": r.id,
                    "marketplace": r.marketplace,
                    "external_return_id": r.external_return_id,
                    "sale_id": r.sale_id,
                    "order_id": r.order_id,
                    "product_id": r.product_id,
                    "sku": r.product.sku if r.product else None,
                    "status": r.return_status,
                    "disposition": r.disposition,
                    "reason": r.reason,
                    "qty": r.quantity,
                    "refund_amount": float(r.refund_amount),
                    "refund_fees": float(r.refund_fees),
                    "refund_shipping": float(r.refund_shipping),
                    "restocked": r.restocked,
                    "returned_at": iso_or_none(r.returned_at),
                    "processed_at": iso_or_none(r.processed_at),
                }
                for r in returns
            ]
        ),
        use_container_width=True,
    )

    st.markdown("### Update Return")
    actor = user.username
    return_map = {f"#{r.id} | {r.marketplace} | {r.external_return_id or 'no-id'}": r for r in returns}
    selected_key = st.selectbox("Select Return", list(return_map.keys()))
    selected = return_map[selected_key]
    with st.form("update_return_form"):
        return_status = st.selectbox(
            "Status",
            ["requested", "received", "processed", "closed", "cancelled"],
            index=["requested", "received", "processed", "closed", "cancelled"].index(selected.return_status)
            if selected.return_status in ["requested", "received", "processed", "closed", "cancelled"]
            else 0,
        )
        disposition = st.selectbox(
            "Disposition",
            ["pending", "restock", "damaged", "scrap", "return_to_vendor", "other"],
            index=["pending", "restock", "damaged", "scrap", "return_to_vendor", "other"].index(selected.disposition)
            if selected.disposition in ["pending", "restock", "damaged", "scrap", "return_to_vendor", "other"]
            else 0,
        )
        quantity = st.number_input("Quantity", min_value=1, value=int(selected.quantity), step=1)
        refund_amount = st.number_input("Refund Amount", min_value=0.0, value=float(selected.refund_amount), step=1.0)
        refund_fees = st.number_input("Refund Fees", min_value=0.0, value=float(selected.refund_fees), step=1.0)
        refund_shipping = st.number_input(
            "Refund Shipping",
            min_value=0.0,
            value=float(selected.refund_shipping),
            step=1.0,
        )
        restocked = st.checkbox("Restocked", value=selected.restocked)
        processed_enabled = st.checkbox("Has Processed Date", value=selected.processed_at is not None)
        processed_date = st.date_input(
            "Processed Date",
            value=(selected.processed_at or datetime.combine(utc_today(), datetime.min.time())).date(),
            disabled=not processed_enabled,
        )
        notes = st.text_area("Notes", value=selected.notes or "")
        submit = st.form_submit_button("Save Return Changes")
    if submit:
        if not ensure_permission(user, "update", "Update Return"):
            st.stop()
        repo.update_return(
            selected.id,
            {
                "return_status": return_status,
                "disposition": disposition,
                "quantity": int(quantity),
                "refund_amount": to_decimal(refund_amount),
                "refund_fees": to_decimal(refund_fees),
                "refund_shipping": to_decimal(refund_shipping),
                "restocked": bool(restocked),
                "processed_at": datetime.combine(processed_date, datetime.min.time()) if processed_enabled else None,
                "notes": notes.strip(),
            },
            actor=actor,
        )
        st.success("Return updated.")
