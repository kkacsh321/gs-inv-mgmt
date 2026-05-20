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
    load_profit_basis_audit = st.checkbox(
        "Load Profit Basis Audit (slower)",
        value=False,
        key="dashboard_load_profit_basis_audit",
        help="Shows recent sale-level net, FIFO COGS, COGS source, and before-return profit evidence.",
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
        profit_before_returns_30d = float(
            live.get("sales_30d_profit_before_returns")
            if live.get("sales_30d_profit_before_returns") is not None
            else net_30d - float(live.get("sales_30d_est_cogs") or 0.0)
        )
        shipping_30d = float(live.get("sales_30d_shipping_charged") or 0.0)
        shipping_label_spend_30d = float(live.get("sales_30d_shipping_label_spend") or 0.0)
        shipping_delta_30d = float(live.get("sales_30d_shipping_delta") or 0.0)
        returns_30d_count = int(live.get("returns_30d_count") or 0)
        returns_refund_total_30d = float(live.get("returns_30d_refund_total") or 0.0)
        returns_cogs_reversal_30d = float(live.get("returns_30d_cogs_reversal") or 0.0)
        returns_profit_impact_30d = float(live.get("returns_30d_profit_impact") or 0.0)
        net_after_returns_30d = float(live.get("sales_30d_net_after_returns") or net_30d)
        orders_7d_count = int(live.get("orders_7d_count") or 0)
        orders_30d_count = int(live.get("orders_30d_count") or 0)
        sales_7d_count = int(live.get("sales_7d_count") or 0)
        sales_30d_count = int(live.get("sales_30d_count") or 0)
        shipped_30d = int(live.get("orders_30d_shipped") or 0)
        not_shipped_30d = int(live.get("orders_30d_not_shipped") or 0)
        ebay_fees_30d_total = float(live.get("ebay_fees_30d_total") or 0.0)
        ebay_fee_type_breakdown_30d = dict(live.get("ebay_fee_type_breakdown_30d") or {})
        cogs_source_counts_30d = dict(live.get("sales_30d_cogs_source_counts") or {})
        cogs_review_count_30d = int(live.get("sales_30d_cogs_review_count") or 0)
        profit_basis_status_30d = str(live.get("sales_30d_profit_basis_status") or "ok").strip().lower()
        bundle_sale_count_30d = int(live.get("sales_30d_bundle_sale_count") or 0)
        bundle_inventory_units_sold_30d = int(live.get("sales_30d_bundle_inventory_units_sold") or 0)
    else:
        all_sales = repo.list_sales() if hasattr(repo, "list_sales") else []
        all_orders = repo.list_orders() if hasattr(repo, "list_orders") else []
        sales_7d = [s for s in all_sales if s.sold_at is not None and window_7d <= s.sold_at <= now]
        sales_30d = [s for s in all_sales if s.sold_at is not None and window_30d <= s.sold_at <= now]
        orders_7d = [o for o in all_orders if o.sold_at is not None and window_7d <= o.sold_at <= now]
        orders_30d = [o for o in all_orders if o.sold_at is not None and window_30d <= o.sold_at <= now]
        actual_30d_rows = []
        actual_7d_rows = []
        if hasattr(repo, "report_sales_actual_econ_rows"):
            try:
                actual_30d_rows = list(repo.report_sales_actual_econ_rows(start_dt=window_30d, end_dt=now) or [])
                actual_7d_rows = [
                    row
                    for row in actual_30d_rows
                    if next(
                        (
                            s.sold_at
                            for s in sales_7d
                            if int(getattr(s, "id", 0) or 0) == int(row.get("sale_id") or 0)
                        ),
                        None,
                    )
                    is not None
                ]
            except Exception:
                db = getattr(repo, "db", None)
                if db is not None and hasattr(db, "rollback"):
                    db.rollback()
                actual_30d_rows = []
                actual_7d_rows = []

        gross_7d = (
            sum(float(row.get("sold_price") or 0.0) for row in actual_7d_rows)
            if actual_7d_rows
            else sum(float(s.sold_price or 0.0) for s in sales_7d)
        )
        if actual_7d_rows:
            net_7d = sum(float(row.get("net_before_cogs_actual") or 0.0) for row in actual_7d_rows)
        else:
            fees_7d = sum(float(s.fees or 0.0) for s in sales_7d)
            shipping_7d = sum(float(s.shipping_cost or 0.0) for s in sales_7d)
            label_spend_7d = sum(float(getattr(s, "shipping_label_cost", 0.0) or 0.0) for s in sales_7d)
            net_7d = gross_7d + shipping_7d - fees_7d - label_spend_7d
        gross_30d = sum(float(s.sold_price or 0.0) for s in sales_30d)
        fees_30d = sum(float(s.fees or 0.0) for s in sales_30d)
        shipping_30d = sum(float(s.shipping_cost or 0.0) for s in sales_30d)

        sales_shipping_label_spend_30d = sum(float(getattr(s, "shipping_label_cost", 0.0) or 0.0) for s in sales_30d)
        order_shipping_charged_30d = sum(float(getattr(o, "shipping_cost", 0.0) or 0.0) for o in orders_30d)
        order_shipping_label_spend_30d = sum(float(getattr(o, "shipping_label_cost", 0.0) or 0.0) for o in orders_30d)
        if order_shipping_charged_30d > 0 or order_shipping_label_spend_30d > 0:
            shipping_30d = order_shipping_charged_30d
            shipping_label_spend_30d = order_shipping_label_spend_30d
        else:
            shipping_label_spend_30d = sales_shipping_label_spend_30d
        net_30d = gross_30d + shipping_30d - fees_30d - shipping_label_spend_30d
        if actual_30d_rows:
            gross_30d = sum(float(row.get("sold_price") or 0.0) for row in actual_30d_rows)
            fees_30d = sum(float(row.get("allocated_fee_actual") or 0.0) for row in actual_30d_rows)
            shipping_30d = sum(float(row.get("allocated_shipping_charged") or 0.0) for row in actual_30d_rows)
            shipping_label_spend_30d = sum(
                float(row.get("allocated_shipping_actual") or 0.0) for row in actual_30d_rows
            )
            net_30d = sum(float(row.get("net_before_cogs_actual") or 0.0) for row in actual_30d_rows)
        est_cogs_30d = sum(
            (
                (
                    (
                        float(s.product.acquisition_cost or 0.0)
                        + float(getattr(s.product, "acquisition_tax_paid", 0.0) or 0.0)
                        + float(getattr(s.product, "acquisition_shipping_paid", 0.0) or 0.0)
                        + float(getattr(s.product, "acquisition_handling_paid", 0.0) or 0.0)
                    )
                    or float(getattr(s.product, "product_cost", 0.0) or 0.0)
                )
                * int(s.quantity_sold or 0)
            )
            if getattr(s, "product", None) is not None
            else 0.0
            for s in sales_30d
        )
        profit_before_returns_30d = net_30d - est_cogs_30d
        est_profit_30d = profit_before_returns_30d
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
        cogs_source_counts_30d = {}
        cogs_review_count_30d = 0
        profit_basis_status_30d = "ok"
        bundle_sale_count_30d = 0
        bundle_inventory_units_sold_30d = 0
        returns_30d_count = 0
        returns_refund_total_30d = 0.0
        returns_cogs_reversal_30d = 0.0
        returns_profit_impact_30d = 0.0
        net_after_returns_30d = net_30d

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
    b5.metric("Est Profit After Returns (30d)", as_money(est_profit_30d))
    b6.metric("Profit Before Returns (30d)", as_money(profit_before_returns_30d))
    b7.metric("Shipping Charged (30d)", as_money(shipping_30d))
    b8.metric("Label Spend (30d)", as_money(shipping_label_spend_30d))

    b9, b10, b11, b12, b13 = st.columns(5)
    b9.metric("Shipping Delta (30d)", as_money(shipping_delta_30d))
    b10.metric("Sales Count (7d)", f"{sales_7d_count}")
    b11.metric("Sales Count (30d)", f"{sales_30d_count}")
    b12.metric("Orders Shipped (30d)", f"{shipped_30d}")
    b13.metric("Orders Not Shipped (30d)", f"{not_shipped_30d}")

    if returns_30d_count > 0:
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Returns (30d)", f"{returns_30d_count}")
        r2.metric("Refunds (30d)", as_money(returns_refund_total_30d))
        r3.metric("Return COGS Reversal (30d)", as_money(returns_cogs_reversal_30d))
        r4.metric("Return Profit Impact (30d)", as_money(returns_profit_impact_30d))

    if bundle_sale_count_30d > 0:
        b13, b14, _b15, _b16 = st.columns(4)
        b13.metric("Bundle Sales (30d)", f"{bundle_sale_count_30d}")
        b14.metric("Bundle Inventory Units Sold (30d)", f"{bundle_inventory_units_sold_30d}")

    st.caption(
        "Estimated profit uses sale net (`gross + shipping charged - fees - label spend`) minus COGS, "
        "then applies return profit impact (`-refunds - returned fees/shipping + returned COGS reversal`). "
        "When available, COGS uses time-aware FIFO lot costs; otherwise it falls back to product landed cost "
        "(`acquisition + tax + inbound shipping + handling`) and then `product_cost`. "
        "Use Reports for detailed reconciliation and close packet evidence."
    )
    if returns_30d_count > 0:
        st.caption(
            f"Returns adjusted 30d profit from {as_money(profit_before_returns_30d)} "
            f"to {as_money(est_profit_30d)}; sales net after returns is {as_money(net_after_returns_30d)}."
        )
    if bundle_sale_count_30d > 0:
        st.caption(
            f"Bundle accounting detected {bundle_sale_count_30d} bundle sale(s) in the last 30 days, "
            f"representing {bundle_inventory_units_sold_30d} inventory unit(s) consumed by bundle components."
        )
    if cogs_source_counts_30d:
        cogs_source_labels = ", ".join(
            f"{source}: {count}"
            for source, count in sorted(cogs_source_counts_30d.items())
            if int(count or 0) > 0
        )
        if cogs_source_labels:
            st.caption(f"COGS source mix (30d sales): {cogs_source_labels}.")
    if profit_basis_status_30d == "review_needed":
        st.warning(
            "Estimated profit needs cost-basis review: "
            f"{cogs_review_count_30d} sale(s) used equal-fallback, mixed, or missing COGS basis. "
            "For lots that are not fully checked in, set expected lot quantity, allocation weights, "
            "or assignment-level costs so sold COGS is not overstated."
        )
    elif profit_basis_status_30d == "partial_lot_estimate":
        st.caption(
            "Estimated profit includes partial-lot COGS based on expected lot quantity. "
            "Finalize lot check-in/allocation before close sign-off."
        )

    if load_profit_basis_audit:
        st.markdown("### Profit Basis Audit (30d)")
        st.caption(
            "Sale-level dashboard profit evidence. Negative rows usually mean the sale net was lower than FIFO COGS, "
            "or the cost source needs review."
        )
        if hasattr(repo, "dashboard_profit_basis_rows"):
            try:
                profit_basis_rows = repo.dashboard_profit_basis_rows(now=now, limit=50)
            except Exception as exc:
                db = getattr(repo, "db", None)
                if db is not None and hasattr(db, "rollback"):
                    db.rollback()
                profit_basis_rows = []
                st.warning(f"Profit basis audit could not be loaded: {exc}")
            if profit_basis_rows:
                profit_basis_df = pd.DataFrame(profit_basis_rows)
                st.dataframe(profit_basis_df, use_container_width=True, hide_index=True)
                negative_count = int((profit_basis_df["profit_before_returns"] < 0).sum())
                review_count = int(
                    profit_basis_df["fifo_cost_source"]
                    .astype(str)
                    .isin({"lot_equal_quantity_fallback", "missing_cost_basis", "mixed_fifo_cost"})
                    .sum()
                )
                if negative_count or review_count:
                    st.warning(
                        f"Profit basis audit found {negative_count} negative before-return sale row(s) "
                        f"and {review_count} row(s) with review-needed COGS source."
                    )
            else:
                st.info("No dashboard profit-basis rows found for the last 30 days.")
        else:
            st.info("This repository does not expose dashboard profit-basis rows yet.")

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
