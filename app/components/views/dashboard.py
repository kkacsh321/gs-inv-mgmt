import streamlit as st

from app.repository import InventoryRepository
from app.components.views.shared import as_money, render_help_panel

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

    col1, col2, col3 = st.columns(3)
    col1.metric("Products", metrics["product_count"])
    col2.metric("Active Listings", metrics["listing_count"])
    col3.metric("Sales Records", metrics["sale_count"])

    col4, col5, col6 = st.columns(3)
    col4.metric("Inventory Cost Basis", as_money(metrics["inventory_cost"]))
    col5.metric("Gross Sales", as_money(metrics["gross_sales"]))
    col6.metric("Net Sales", as_money(metrics["net_sales"]))

