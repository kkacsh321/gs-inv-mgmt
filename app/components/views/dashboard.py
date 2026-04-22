import streamlit as st
from datetime import timedelta
import pandas as pd

from app.repository import InventoryRepository
from app.components.views.shared import as_money, render_help_panel
from app.utils.time import utcnow_naive

def render_dashboard(repo: InventoryRepository) -> None:
    st.subheader("Dashboard")
    render_help_panel(
        section_title="Dashboard",
        goal="See current inventory, listing, and sales performance at a glance.",
        steps=[
            "Review counts for products, active listings, and sales records.",
            "Use inventory cost, gross sales, and net sales metrics to spot operational issues.",
            "Use this page as the daily health check before working in detailed pages.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )
    metrics = repo.dashboard_metrics()
    now = utcnow_naive()
    window_7d = now - timedelta(days=7)
    window_30d = now - timedelta(days=30)
    load_fee_breakdown = st.checkbox(
        "Load eBay Fee Attribution Breakdown (slower)",
        value=False,
        key="dashboard_load_fee_breakdown",
        help="Defers grouped fee-type aggregation unless explicitly requested.",
    )

    use_rollup = hasattr(repo, "dashboard_live_metrics")
    if use_rollup:
        try:
            live_payload = repo.dashboard_live_metrics(
                now=now,
                include_fee_type_breakdown=bool(load_fee_breakdown),
            )
        except TypeError:
            # Backward compatibility for older mocked/test signatures.
            live_payload = repo.dashboard_live_metrics(now=now)
        live = dict(live_payload or {})
        gross_7d = float(live.get("sales_7d_gross") or 0.0)
        net_7d = float(live.get("sales_7d_net") or 0.0)
        gross_30d = float(live.get("sales_30d_gross") or 0.0)
        net_30d = float(live.get("sales_30d_net") or 0.0)
        est_profit_30d = float(live.get("sales_30d_est_profit") or 0.0)
        shipping_30d = float(live.get("sales_30d_shipping_charged") or 0.0)
        shipping_label_spend_30d = float(live.get("sales_30d_shipping_label_spend") or 0.0)
        shipping_delta_30d = float(live.get("sales_30d_shipping_delta") or 0.0)
        orders_7d_count = int(live.get("orders_7d_count") or 0)
        orders_30d_count = int(live.get("orders_30d_count") or 0)
        sales_7d_count = int(live.get("sales_7d_count") or 0)
        sales_30d_count = int(live.get("sales_30d_count") or 0)
        shipped_30d = int(live.get("orders_30d_shipped") or 0)
        not_shipped_30d = int(live.get("orders_30d_not_shipped") or 0)
        ebay_fees_30d_total = float(live.get("ebay_fees_30d_total") or 0.0)
        ebay_fee_type_breakdown_30d = dict(live.get("ebay_fee_type_breakdown_30d") or {})
    else:
        all_sales = repo.list_sales() if hasattr(repo, "list_sales") else []
        all_orders = repo.list_orders() if hasattr(repo, "list_orders") else []
        sales_7d = [s for s in all_sales if s.sold_at is not None and s.sold_at >= window_7d]
        sales_30d = [s for s in all_sales if s.sold_at is not None and s.sold_at >= window_30d]
        orders_7d = [o for o in all_orders if o.sold_at is not None and o.sold_at >= window_7d]
        orders_30d = [o for o in all_orders if o.sold_at is not None and o.sold_at >= window_30d]

        gross_7d = sum(float(s.sold_price or 0.0) for s in sales_7d)
        fees_7d = sum(float(s.fees or 0.0) for s in sales_7d)
        shipping_7d = sum(float(s.shipping_cost or 0.0) for s in sales_7d)
        net_7d = gross_7d - fees_7d - shipping_7d
        gross_30d = sum(float(s.sold_price or 0.0) for s in sales_30d)
        fees_30d = sum(float(s.fees or 0.0) for s in sales_30d)
        shipping_30d = sum(float(s.shipping_cost or 0.0) for s in sales_30d)
        net_30d = gross_30d - fees_30d - shipping_30d

        est_cogs_30d = sum(
            (
                (
                    float(s.product.acquisition_cost or 0.0)
                    + float(getattr(s.product, "acquisition_tax_paid", 0.0) or 0.0)
                    + float(getattr(s.product, "acquisition_shipping_paid", 0.0) or 0.0)
                    + float(getattr(s.product, "acquisition_handling_paid", 0.0) or 0.0)
                )
                * int(s.quantity_sold or 0)
            )
            if getattr(s, "product", None) is not None
            else 0.0
            for s in sales_30d
        )
        est_profit_30d = net_30d - est_cogs_30d
        sales_shipping_label_spend_30d = sum(float(getattr(s, "shipping_label_cost", 0.0) or 0.0) for s in sales_30d)
        order_shipping_charged_30d = sum(float(getattr(o, "shipping_cost", 0.0) or 0.0) for o in orders_30d)
        order_shipping_label_spend_30d = sum(float(getattr(o, "shipping_label_cost", 0.0) or 0.0) for o in orders_30d)
        if order_shipping_charged_30d > 0 or order_shipping_label_spend_30d > 0:
            shipping_30d = order_shipping_charged_30d
            shipping_label_spend_30d = order_shipping_label_spend_30d
        else:
            shipping_label_spend_30d = sales_shipping_label_spend_30d
        shipping_delta_30d = shipping_30d - shipping_label_spend_30d
        shipped_30d = sum(
            1 for o in orders_30d if str(o.order_status or "").strip().lower() in {"shipped", "delivered"}
        )
        not_shipped_30d = sum(
            1
            for o in orders_30d
            if str(o.order_status or "").strip().lower() not in {"shipped", "delivered", "cancelled", "refunded"}
        )
        orders_7d_count = len(orders_7d)
        orders_30d_count = len(orders_30d)
        sales_7d_count = len(sales_7d)
        sales_30d_count = len(sales_30d)
        ebay_fees_30d_total = float(fees_30d)
        ebay_fee_type_breakdown_30d = {}

    col1, col2, col3 = st.columns(3)
    col1.metric("Products", metrics["product_count"])
    col2.metric("Active Listings", metrics["listing_count"])
    col3.metric("Sales Records", metrics["sale_count"])

    col4, col5, col6 = st.columns(3)
    col4.metric("Inventory Cost Basis", as_money(metrics["inventory_cost"]))
    col5.metric("Gross Sales", as_money(metrics["gross_sales"]))
    col6.metric("Net Sales", as_money(metrics["net_sales"]))

    st.markdown("### Live Business Metrics")
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Orders (7d)", f"{orders_7d_count}")
    b2.metric("Orders (30d)", f"{orders_30d_count}")
    b3.metric("Sales Gross (30d)", as_money(gross_30d))
    b4.metric("Sales Net (30d)", as_money(net_30d))

    b5, b6, b7, b8 = st.columns(4)
    b5.metric("Est Profit (30d)", as_money(est_profit_30d))
    b6.metric("Shipping Charged (30d)", as_money(shipping_30d))
    b7.metric("Label Spend (30d)", as_money(shipping_label_spend_30d))
    b8.metric("Shipping Delta (30d)", as_money(shipping_delta_30d))

    b9, b10, b11, b12 = st.columns(4)
    b9.metric("Sales Count (7d)", f"{sales_7d_count}")
    b10.metric("Sales Count (30d)", f"{sales_30d_count}")
    b11.metric("Orders Shipped (30d)", f"{shipped_30d}")
    b12.metric("Orders Not Shipped (30d)", f"{not_shipped_30d}")

    st.caption(
        "Estimated profit uses sale net (`gross - fees - shipping charged`) minus landed-cost estimate "
        "(`acquisition + tax + shipping + handling`). "
        "Use Reports for audited FIFO/lot COGS and detailed reconciliation."
    )

    st.markdown("### eBay Fee Attribution (30d)")
    f1, f2 = st.columns(2)
    f1.metric("eBay Fees (30d)", as_money(ebay_fees_30d_total))
    f2.metric("Fee Types Seen", f"{len([k for k, v in ebay_fee_type_breakdown_30d.items() if float(v or 0) > 0])}")
    if not load_fee_breakdown:
        st.caption(
            "Fee-type breakdown is deferred. Enable `Load eBay Fee Attribution Breakdown (slower)` to run grouped fee analytics."
        )
    elif ebay_fee_type_breakdown_30d:
        fee_rows = [
            {"fee_type": k, "amount": float(v or 0.0)}
            for k, v in ebay_fee_type_breakdown_30d.items()
            if float(v or 0.0) > 0
        ]
        if fee_rows:
            fee_df = pd.DataFrame(fee_rows).sort_values("amount", ascending=False)
            fee_df["amount"] = fee_df["amount"].map(lambda v: round(float(v or 0), 2))
            st.dataframe(fee_df, use_container_width=True, hide_index=True)
    else:
        st.info("No normalized eBay fee attribution found in the last 30 days yet.")
