"""Compatibility layer for view renderers.

Primary implementations now live in `app.components.views.*`.
This module re-exports them so existing imports keep working.
"""

from app.components.views import (
    MARKETPLACES,
    MEDIA_UPLOAD_TYPES,
    as_money,
    dataframe_to_xlsx_bytes,
    generate_sku,
    infer_media_type,
    pretty_json,
    render_admin,
    render_customers,
    render_dashboard,
    render_documents,
    render_ebay,
    render_inventory_movements,
    render_listings,
    render_lots,
    render_media,
    render_orders,
    render_products,
    render_reports,
    render_returns,
    render_sales,
    render_search_edit,
    render_shipping,
    render_sources,
    render_tools,
    upload_media_for_listing,
)
