from datetime import datetime
import json

import pandas as pd
import streamlit as st

from app.auth import current_user
from app.components.views.shared import as_money, render_help_panel, safe_switch_page
from app.components.views.entity_ops import render_saved_filter_bar, render_standard_row_actions
from app.components.views.workspace_shell import (
    normalize_status_semantic,
    render_workspace_feedback,
    render_workspace_task_completion,
    render_status_semantic_legend,
    render_workspace_empty_state,
)
from app.config import settings
from app.repository import InventoryRepository
from app.utils.time import utc_today, utcnow_naive


def _status(value: str) -> str:
    return (value or "").strip().lower()


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def _page_button(label: str, page_path: str, *, key: str) -> None:
    if st.button(label, key=key, use_container_width=True):
        if hasattr(st, "switch_page"):
            safe_switch_page(page_path)
        else:
            st.info(f"Open `{page_path}` from the sidebar.")


def _age_hours(value: datetime | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - value).total_seconds() / 3600.0)


def _sla_label(hours: float | None, *, warn: float, critical: float) -> str:
    if hours is None:
        return "n/a"
    if hours >= critical:
        return f"{hours:.1f}h (critical)"
    if hours >= warn:
        return f"{hours:.1f}h (warn)"
    return f"{hours:.1f}h (ok)"


def _photo_comp_created_listing_ids(repo: InventoryRepository, limit: int = 5000) -> set[int]:
    rows = repo.list_audit_logs(limit=max(1, int(limit)))
    ids: set[int] = set()
    for row in rows:
        if str(getattr(row, "entity_type", "") or "").strip().lower() != "navigation":
            continue
        if str(getattr(row, "action", "") or "").strip().lower() != "photo_comp_product_draft_created":
            continue
        raw = str(getattr(row, "changes_json", "") or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        listing_ids = payload.get("draft_listing_ids")
        if isinstance(listing_ids, list):
            for value in listing_ids:
                try:
                    ids.add(int(value))
                except Exception:
                    continue
    return ids


def _listings_blocker_followup_rows(repo: InventoryRepository, limit: int = 2000) -> list[dict]:
    rows = repo.list_audit_logs(limit=max(1, int(limit)))
    today = utc_today()
    task_state_map: dict[str, dict] = {}
    for event in sorted(
        rows,
        key=lambda r: (getattr(r, "changed_at", None) or datetime.min),
        reverse=True,
    ):
        if str(getattr(event, "entity_type", "") or "").strip().lower() != "workspace_followup":
            continue
        raw_changes = str(getattr(event, "changes_json", "") or "").strip()
        if not raw_changes:
            continue
        try:
            payload = json.loads(raw_changes)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        workflow = str(payload.get("workflow") or "").strip().lower()
        title_val = str(payload.get("title") or "").strip().lower()
        if workflow != "listings_readiness:blocker" and "[listings/readiness]" not in title_val:
            continue
        task_key = str(payload.get("task_key") or payload.get("task_id") or "").strip()
        if not task_key:
            continue
        due_date_raw = str(payload.get("due_date") or "").strip()
        due_date_obj = None
        if due_date_raw:
            try:
                due_date_obj = datetime.fromisoformat(due_date_raw).date()
            except Exception:
                due_date_obj = None
        due_in_days = (due_date_obj - today).days if due_date_obj is not None else None
        sla_status = "none"
        if due_in_days is not None:
            if due_in_days < 0:
                sla_status = "overdue"
            elif due_in_days <= 2:
                sla_status = "due_soon"
            else:
                sla_status = "on_track"
        changed_at = getattr(event, "changed_at", None)
        existing = task_state_map.get(task_key)
        if existing is None:
            task_state_map[task_key] = {
                "task_key": task_key,
                "title": str(payload.get("title") or "").strip(),
                "blocker_reason": str(payload.get("blocker_reason") or "").strip(),
                "owner": str(payload.get("owner") or "").strip(),
                "priority": str(payload.get("priority") or "").strip().lower(),
                "due_date": due_date_raw,
                "due_in_days": due_in_days,
                "sla_status": sla_status,
                "status": str(payload.get("status") or "").strip().lower()
                or ("resolved" if str(getattr(event, "action", "")).strip().lower() == "resolve" else "open"),
                "created_at": _format_dt(changed_at),
                "last_updated_at": _format_dt(changed_at),
                "last_action": str(getattr(event, "action", "") or "").strip().lower(),
                "last_actor": str(getattr(event, "changed_by", "") or "").strip(),
            }
        else:
            existing["last_updated_at"] = _format_dt(changed_at)
            existing["last_action"] = str(getattr(event, "action", "") or "").strip().lower()
            existing["last_actor"] = str(getattr(event, "changed_by", "") or "").strip()
            if due_in_days is not None:
                existing["due_in_days"] = due_in_days
                existing["sla_status"] = sla_status
            if str(getattr(event, "action", "") or "").strip().lower() == "resolve":
                existing["status"] = "resolved"
            elif str(payload.get("status") or "").strip():
                existing["status"] = str(payload.get("status") or "").strip().lower()
    return sorted(
        list(task_state_map.values()),
        key=lambda row: (
            0 if str(row.get("sla_status") or "") == "overdue" else (
                1 if str(row.get("sla_status") or "") == "due_soon" else 2
            ),
            int(row.get("due_in_days")) if isinstance(row.get("due_in_days"), int) else 9999,
            str(row.get("last_updated_at") or ""),
            str(row.get("task_key") or ""),
        ),
        reverse=False,
    )


def _governance_cadence_followup_rows(repo: InventoryRepository, limit: int = 2000) -> list[dict]:
    rows = repo.list_audit_logs(limit=max(1, int(limit)))
    today = utc_today()
    task_state_map: dict[str, dict] = {}
    for event in sorted(
        rows,
        key=lambda r: (getattr(r, "changed_at", None) or datetime.min),
        reverse=True,
    ):
        if str(getattr(event, "entity_type", "") or "").strip().lower() != "workspace_followup":
            continue
        raw_changes = str(getattr(event, "changes_json", "") or "").strip()
        if not raw_changes:
            continue
        try:
            payload = json.loads(raw_changes)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        workflow = str(payload.get("workflow") or "").strip().lower()
        if workflow != "governance_snapshot_cadence":
            continue
        task_key = str(payload.get("task_key") or payload.get("task_id") or "").strip()
        if not task_key:
            continue
        due_date_raw = str(payload.get("due_date") or "").strip()
        due_date_obj = None
        if due_date_raw:
            try:
                due_date_obj = datetime.fromisoformat(due_date_raw).date()
            except Exception:
                due_date_obj = None
        due_in_days = (due_date_obj - today).days if due_date_obj is not None else None
        sla_status = "none"
        if due_in_days is not None:
            if due_in_days < 0:
                sla_status = "overdue"
            elif due_in_days <= 2:
                sla_status = "due_soon"
            else:
                sla_status = "on_track"
        changed_at = getattr(event, "changed_at", None)
        existing = task_state_map.get(task_key)
        if existing is None:
            task_state_map[task_key] = {
                "task_key": task_key,
                "title": str(payload.get("title") or "").strip(),
                "owner": str(payload.get("owner") or "").strip(),
                "priority": str(payload.get("priority") or "").strip().lower(),
                "due_date": due_date_raw,
                "due_in_days": due_in_days,
                "sla_status": sla_status,
                "status": str(payload.get("status") or "").strip().lower()
                or ("resolved" if str(getattr(event, "action", "")).strip().lower() == "resolve" else "open"),
                "created_at": _format_dt(changed_at),
                "last_updated_at": _format_dt(changed_at),
                "last_action": str(getattr(event, "action", "") or "").strip().lower(),
                "last_actor": str(getattr(event, "changed_by", "") or "").strip(),
                "note": str(payload.get("note") or "").strip(),
            }
        else:
            existing["last_updated_at"] = _format_dt(changed_at)
            existing["last_action"] = str(getattr(event, "action", "") or "").strip().lower()
            existing["last_actor"] = str(getattr(event, "changed_by", "") or "").strip()
            if due_in_days is not None:
                existing["due_in_days"] = due_in_days
                existing["sla_status"] = sla_status
            if str(getattr(event, "action", "") or "").strip().lower() == "resolve":
                existing["status"] = "resolved"
            elif str(payload.get("status") or "").strip():
                existing["status"] = str(payload.get("status") or "").strip().lower()
    return sorted(
        list(task_state_map.values()),
        key=lambda row: (
            0 if str(row.get("sla_status") or "") == "overdue" else (
                1 if str(row.get("sla_status") or "") == "due_soon" else 2
            ),
            int(row.get("due_in_days")) if isinstance(row.get("due_in_days"), int) else 9999,
            str(row.get("last_updated_at") or ""),
            str(row.get("task_key") or ""),
        ),
        reverse=False,
    )


def _listing_format_hint(row, *, default_format_type: str, default_auction_duration: str) -> str:
    if (row.marketplace or "").strip().lower() != "ebay":
        return ""
    publish_meta = {}
    raw_details = str(row.marketplace_details or "").strip()
    if raw_details:
        try:
            details_obj = json.loads(raw_details)
            if isinstance(details_obj, dict):
                publish_meta = details_obj.get("ebay_publish")
                if not isinstance(publish_meta, dict):
                    publish_meta = details_obj
        except Exception:
            publish_meta = {}
    format_type = str(
        publish_meta.get("format")
        or publish_meta.get("format_type")
        or default_format_type
        or "FIXED_PRICE"
    ).strip().upper()
    if format_type not in {"FIXED_PRICE", "AUCTION"}:
        format_type = "FIXED_PRICE"

    def _num(value, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(fallback)

    auction_duration = str(
        publish_meta.get("listing_duration")
        or ("GTC" if format_type == "FIXED_PRICE" else default_auction_duration)
    ).strip().upper()
    auction_start_price = _num(publish_meta.get("auction_start_price"), _num(row.listing_price, 0.0))
    auction_reserve_price = _num(publish_meta.get("auction_reserve_price"), 0.0)
    auction_buy_now_price = _num(publish_meta.get("auction_buy_now_price"), 0.0)
    format_hints: list[str] = []
    if format_type == "FIXED_PRICE":
        if _num(row.listing_price, 0.0) <= 0:
            format_hints.append("Fixed Missing BIN")
    else:
        if auction_start_price <= 0:
            format_hints.append("Auction Missing Start")
        if auction_duration not in {"DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"}:
            format_hints.append("Auction Missing Duration")
        if auction_reserve_price > 0 and auction_reserve_price < auction_start_price:
            format_hints.append("Reserve < Start")
        if auction_buy_now_price > 0 and auction_buy_now_price < auction_start_price:
            format_hints.append("BIN < Start")
    return "; ".join(format_hints)


def _action_rows_for_role(role: str) -> list[tuple[str, str, str]]:
    if role == "admin":
        return [
            ("Listings: Publish Queue", "pages/03_Listings.py", "create listing, publish/revise/end"),
            ("Shipping: Needs Label", "pages/11_Shipping.py", "print/export shipment batches"),
            ("Sync: Failed Runs", "pages/18_Sync.py", "retry failed marketplace sync jobs"),
            ("Admin: Migrations + Backups", "pages/17_Admin.py", "schema, maintenance, backups"),
            ("Reports: Accounting Export", "pages/09_Reports.py", "sales/shipping/refund exports"),
            ("Search & Edit", "pages/10_Search_Edit.py", "cross-entity corrections + audit trail"),
        ]
    if role == "ops":
        return [
            ("Listings: Publish Queue", "pages/03_Listings.py", "create listing, publish/revise/end"),
            ("eBay Workspace", "pages/22_eBay_Workspace.py", "integration + bulk end/relist/revise + policies"),
            ("Shipping: Needs Label", "pages/11_Shipping.py", "print/export shipment batches"),
            ("Orders", "pages/14_Orders.py", "review incoming marketplace orders"),
            ("Customers", "pages/29_Customers.py", "repeat buyer lookup + purchase history"),
            ("Sales", "pages/04_Sales.py", "tracking and shipment updates"),
            ("Sync: Failed Runs", "pages/18_Sync.py", "retry failed marketplace sync jobs"),
        ]
    return [
        ("Dashboard", "pages/01_Dashboard.py", "high-level inventory and sales health"),
        ("Reports", "pages/09_Reports.py", "read-only exports and snapshots"),
        ("Search & Edit", "pages/10_Search_Edit.py", "audit-focused record lookup"),
    ]


def render_operations_home(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Operations Home")
    st.caption("Central command view for daily inventory, listing, shipping, sync, and accounting queues.")
    render_help_panel(
        section_title="Operations Home",
        goal="Triage the highest-impact operational queues from one place.",
        steps=[
            "Start with inventory intake and eBay listing workflow queues.",
            "Then process shipping backlog and sync failures.",
            "Resolve sync failures before end-of-day reconciliation.",
            "Work accounting exceptions before export/sync handoff.",
            "Use role-based quick actions to jump into execution pages.",
        ],
        roadmap_phase="v0.4 GS-V04-001 Operations Home Command Center",
    )

    products = repo.list_products()
    listings = repo.list_listings()
    sales = repo.list_sales()
    orders = repo.list_orders()
    sync_runs = repo.list_sync_runs(limit=200)

    marketplace_options = sorted(
        {
            (row.marketplace or "").strip().lower()
            for row in listings + sales + orders
            if (row.marketplace or "").strip()
        }
    )
    provider_options = sorted({(row.provider or "").strip().lower() for row in sync_runs if (row.provider or "").strip()})
    queue_view_options = [
        "All Queues",
        "Inventory Intake",
        "eBay Listing Workflow",
        "eBay Format Fix Queue",
        "Listings Blocker Follow-ups",
        "Governance Cadence Follow-ups",
        "Photo-Comp Review Queue",
        "Shipping Queue",
        "Sync Failures",
        "Accounting Exceptions",
    ]
    current_marketplace_state = st.session_state.get("ops_home_marketplace_filter", marketplace_options)
    if not isinstance(current_marketplace_state, list):
        current_marketplace_state = marketplace_options
    current_provider_state = st.session_state.get("ops_home_sync_provider_filter", provider_options)
    if not isinstance(current_provider_state, list):
        current_provider_state = provider_options
    current_queue_view_state = str(st.session_state.get("ops_home_queue_view", "All Queues") or "All Queues")
    if current_queue_view_state not in queue_view_options:
        current_queue_view_state = "All Queues"
    current_filter_state = {
        "marketplaces": [str(item).strip().lower() for item in current_marketplace_state if str(item).strip()],
        "sync_providers": [str(item).strip().lower() for item in current_provider_state if str(item).strip()],
        "queue_view": current_queue_view_state,
    }
    effective_filters = render_saved_filter_bar(
        repo=repo,
        scope="operations_home",
        username=user.username,
        role=user.role,
        allow_role_shared=True,
        current_filters=current_filter_state,
    )

    effective_marketplaces = effective_filters.get("marketplaces", marketplace_options)
    if not isinstance(effective_marketplaces, list):
        effective_marketplaces = marketplace_options
    selected_marketplace_defaults = [
        m for m in [str(item).strip().lower() for item in effective_marketplaces] if m in set(marketplace_options)
    ] or marketplace_options
    selected_marketplaces = st.multiselect(
        "Marketplace Filter",
        options=marketplace_options,
        default=selected_marketplace_defaults,
        key="ops_home_marketplace_filter",
    )
    selected_marketplaces_set = {m.strip().lower() for m in selected_marketplaces}

    def _keep_marketplace(value: str | None) -> bool:
        if not selected_marketplaces_set:
            return True
        return (value or "").strip().lower() in selected_marketplaces_set

    listings = [row for row in listings if _keep_marketplace(row.marketplace)]
    sales = [row for row in sales if _keep_marketplace(row.marketplace)]
    orders = [row for row in orders if _keep_marketplace(row.marketplace)]

    effective_providers = effective_filters.get("sync_providers", provider_options)
    if not isinstance(effective_providers, list):
        effective_providers = provider_options
    selected_provider_defaults = [
        p for p in [str(item).strip().lower() for item in effective_providers] if p in set(provider_options)
    ] or provider_options
    selected_providers = st.multiselect(
        "Sync Provider Filter",
        options=provider_options,
        default=selected_provider_defaults,
        key="ops_home_sync_provider_filter",
    )
    effective_queue_view = str(effective_filters.get("queue_view", "All Queues") or "All Queues").strip()
    if effective_queue_view not in queue_view_options:
        effective_queue_view = "All Queues"
    queue_view = st.selectbox(
        "Queue View",
        options=queue_view_options,
        index=queue_view_options.index(effective_queue_view),
        key="ops_home_queue_view",
        help="Use queue view presets to focus daily work by role/team.",
    )
    selected_providers_set = {p.strip().lower() for p in selected_providers}
    if selected_providers_set:
        sync_runs = [row for row in sync_runs if (row.provider or "").strip().lower() in selected_providers_set]

    active_listing_product_ids = {
        row.product_id for row in listings if _status(row.listing_status) == "active"
    }
    draft_listings = [row for row in listings if _status(row.listing_status) == "draft"]
    needs_listing_products = [
        row for row in products if int(row.current_quantity or 0) > 0 and row.id not in active_listing_product_ids
    ]
    low_stock_products = [row for row in products if int(row.current_quantity or 0) <= 1]

    delivered_like = {"delivered"}
    exception_like = {"exception", "delivery_exception", "failed", "failed_attempt", "returned_to_sender"}
    in_transit_like = {"in_transit", "label_created", "shipped"}
    needs_label_like = {"", "pending", "not_shipped", "needs_label", "ready_for_label"}

    needs_shipment_sales = []
    in_transit_sales = []
    exception_sales = []
    for row in sales:
        state = _status(row.tracking_status)
        if state in delivered_like:
            continue
        if state in exception_like:
            exception_sales.append(row)
            continue
        if state in in_transit_like:
            in_transit_sales.append(row)
            continue
        if state in needs_label_like:
            needs_shipment_sales.append(row)
            continue
        needs_shipment_sales.append(row)

    failed_status = {"failed", "partial"}
    recent_failed_sync = [row for row in sync_runs if _status(row.status) in failed_status]
    ebay_draft_listings = [
        row
        for row in listings
        if (row.marketplace or "").strip().lower() == "ebay" and _status(row.listing_status) == "draft"
    ]
    default_format_type = "FIXED_PRICE"
    default_auction_duration = "DAYS_7"
    try:
        from app.runtime_settings import get_runtime_str

        default_format_type = (
            get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE").strip().upper() or "FIXED_PRICE"
        )
        default_auction_duration = (
            get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7").strip().upper() or "DAYS_7"
        )
    except Exception:
        pass
    format_fix_listings = [
        row
        for row in listings
        if (row.marketplace or "").strip().lower() == "ebay"
        and str(_listing_format_hint(row, default_format_type=default_format_type, default_auction_duration=default_auction_duration)).strip()
    ]
    blocker_followup_rows = _listings_blocker_followup_rows(repo)
    open_blocker_followups = [
        row for row in blocker_followup_rows if str(row.get("status") or "").strip().lower() == "open"
    ]
    governance_cadence_rows = _governance_cadence_followup_rows(repo)
    open_governance_cadence_followups = [
        row for row in governance_cadence_rows if str(row.get("status") or "").strip().lower() == "open"
    ]
    photo_comp_listing_ids = _photo_comp_created_listing_ids(repo)
    photo_comp_pending_review_listings = [
        row
        for row in ebay_draft_listings
        if int(row.id) in photo_comp_listing_ids
        and _status(getattr(row, "review_status", "pending")) != "approved"
    ]
    ebay_active_listings = [
        row
        for row in listings
        if (row.marketplace or "").strip().lower() == "ebay" and _status(row.listing_status) == "active"
    ]
    ebay_needs_shipping_sales = [
        row for row in needs_shipment_sales if (row.marketplace or "").strip().lower() == "ebay"
    ]
    ebay_exception_sales = [
        row for row in exception_sales if (row.marketplace or "").strip().lower() == "ebay"
    ]

    accounting_exception_rows = []
    for row in sales:
        reasons: list[str] = []
        if row.order_id is None:
            reasons.append("missing_order_link")
        if row.listing_id is None:
            reasons.append("missing_listing_link")
        if (row.marketplace or "").strip() and not (row.external_order_id or "").strip():
            reasons.append("missing_external_order_id")
        if reasons:
            accounting_exception_rows.append(
                {
                    "sale_id": row.id,
                    "sold_at": _format_dt(row.sold_at),
                    "marketplace": row.marketplace,
                    "product_id": row.product_id,
                    "order_id": row.order_id,
                    "listing_id": row.listing_id,
                    "external_order_id": row.external_order_id,
                    "reasons": ", ".join(reasons),
                }
            )

    for row in orders:
        ext = (row.external_order_id or "").strip().lower()
        if ext.startswith("internal-"):
            accounting_exception_rows.append(
                {
                    "sale_id": "",
                    "sold_at": _format_dt(row.sold_at),
                    "marketplace": row.marketplace,
                    "product_id": "",
                    "order_id": row.id,
                    "listing_id": "",
                    "external_order_id": row.external_order_id,
                    "reasons": "internal_order_id_needs_channel_reference",
                }
            )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Needs Listing", len(needs_listing_products))
    m2.metric("Needs Shipment", len(needs_shipment_sales))
    m3.metric("Sync Failures", len(recent_failed_sync))
    m4.metric("Accounting Exceptions", len(accounting_exception_rows))

    m5, m6, m7, m8 = st.columns(4)
    m5.metric("Draft Listings", len(draft_listings))
    m6.metric("In Transit", len(in_transit_sales))
    m7.metric("Delivery Exceptions", len(exception_sales))
    m8.metric("Low Stock (<=1)", len(low_stock_products))
    m9, m10 = st.columns(2)
    m9.metric("Photo-Comp Pending Review", len(photo_comp_pending_review_listings))
    with m10:
        if st.button(
            "Open Photo-Comp Review Queue",
            key="ops_home_open_photo_comp_review_queue_btn",
            use_container_width=True,
        ):
            st.session_state["listings_filter_query"] = ""
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = ["draft"]
            st.session_state["listings_filter_origin"] = "photo_comp_draft"
            st.session_state["workspace_handoff_from"] = "operations_home"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "operations_home",
                        "to": "listings",
                        "preset": "photo_comp_review_queue",
                        "marketplaces": ["ebay"],
                        "statuses": ["draft"],
                        "origin": "photo_comp_draft",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/03_Listings.py")
    m11, m12 = st.columns(2)
    m11.metric("Format Fix Needed", len(format_fix_listings))
    with m12:
        if st.button(
            "Open Format Fix Queue",
            key="ops_home_open_format_fix_queue_btn",
            use_container_width=True,
        ):
            st.session_state["listings_filter_query"] = ""
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = ["draft", "active"]
            st.session_state["listings_filter_origin"] = "all"
            st.session_state["listings_filter_format_issue_only"] = True
            st.session_state["workspace_handoff_from"] = "operations_home"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "operations_home",
                        "to": "listings",
                        "preset": "format_fix_queue",
                        "marketplaces": ["ebay"],
                        "statuses": ["draft", "active"],
                        "origin": "all",
                        "format_issue_only": True,
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/03_Listings.py")
    m13, m14, m15 = st.columns(3)
    m13.metric("Open Blocker Follow-ups", len(open_blocker_followups))
    with m14:
        if st.button(
            "Open Listings Blocker Tasks",
            key="ops_home_open_listings_blocker_tasks_btn",
            use_container_width=True,
        ):
            st.session_state["listings_readiness_filter"] = "blocked"
            st.session_state["workspace_handoff_from"] = "operations_home"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "operations_home",
                        "to": "listings",
                        "preset": "listings_blocker_followups",
                        "readiness_filter": "blocked",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/03_Listings.py")
    with m15:
        st.metric("Governance Cadence Follow-ups", len(open_governance_cadence_followups))
        if st.button(
            "Open Governance Cadence Tasks",
            key="ops_home_open_governance_cadence_tasks_btn",
            use_container_width=True,
        ):
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/17_Admin.py")

    metrics = repo.dashboard_metrics()
    st.caption(
        f"Inventory Cost: {as_money(metrics['inventory_cost'])} | "
        f"Gross Sales: {as_money(metrics['gross_sales'])} | Net Sales: {as_money(metrics['net_sales'])}"
    )

    st.markdown("### Core Workflow: Inventory -> eBay -> Shipping")
    workflow_rows = [
        {
            "stage": "Inventory On Hand",
            "count": len([p for p in products if int(p.current_quantity or 0) > 0]),
            "semantic": "done",
            "focus": "Items available to list",
            "target_page": "Products",
            "path": "pages/02_Products.py",
        },
        {
            "stage": "Needs Listing",
            "count": len(needs_listing_products),
            "semantic": "needs_action",
            "focus": "In-stock items not yet active on a listing",
            "target_page": "Listings",
            "path": "pages/03_Listings.py",
        },
        {
            "stage": "eBay Draft Listings",
            "count": len(ebay_draft_listings),
            "semantic": "needs_action",
            "focus": "Drafts waiting for publish",
            "target_page": "Listings",
            "path": "pages/03_Listings.py",
        },
        {
            "stage": "eBay Active Listings",
            "count": len(ebay_active_listings),
            "semantic": "in_progress",
            "focus": "Live offers/listings",
            "target_page": "eBay Workspace",
            "path": "pages/22_eBay_Workspace.py",
        },
        {
            "stage": "eBay Needs Shipment",
            "count": len(ebay_needs_shipping_sales),
            "semantic": "needs_action",
            "focus": "Orders requiring label/ship updates",
            "target_page": "Shipping",
            "path": "pages/11_Shipping.py",
        },
        {
            "stage": "eBay Delivery Exceptions",
            "count": len(ebay_exception_sales),
            "semantic": "blocked",
            "focus": "Tracking and delivery issues",
            "target_page": "Shipping",
            "path": "pages/11_Shipping.py",
        },
    ]
    st.dataframe(pd.DataFrame(workflow_rows), use_container_width=True)
    wf_cols = st.columns(3)
    for idx, row in enumerate(workflow_rows):
        with wf_cols[idx % 3]:
            _page_button(
                f"{row['stage']}: Open {row['target_page']}",
                row["path"],
                key=f"ops_home_workflow_router_{idx}",
            )

    st.markdown("### SLA Watch")
    now = utcnow_naive()
    oldest_needs_shipment = min((row.sold_at for row in needs_shipment_sales if row.sold_at), default=None)
    oldest_failed_sync = min((row.started_at for row in recent_failed_sync if row.started_at), default=None)
    oldest_needs_listing = min((row.acquired_at for row in needs_listing_products if row.acquired_at), default=None)
    oldest_ebay_draft = min((row.listed_at for row in ebay_draft_listings if row.listed_at), default=None)

    needs_shipment_age = _age_hours(oldest_needs_shipment, now=now)
    failed_sync_age = _age_hours(oldest_failed_sync, now=now)
    needs_listing_age = _age_hours(oldest_needs_listing, now=now)
    draft_age = _age_hours(oldest_ebay_draft, now=now)

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Oldest Needs Shipment", _sla_label(needs_shipment_age, warn=8, critical=24))
    s2.metric("Oldest Failed Sync", _sla_label(failed_sync_age, warn=1, critical=4))
    s3.metric("Oldest Needs Listing", _sla_label(needs_listing_age, warn=24, critical=72))
    s4.metric("Oldest eBay Draft", _sla_label(draft_age, warn=24, critical=72))

    st.markdown("### Queue Router")
    render_status_semantic_legend(title="Queue Semantics")
    queue_router_rows = [
        {
            "queue": "Needs Listing",
            "semantic": "needs_action",
            "open_count": len(needs_listing_products),
            "target_page": "Listings",
            "path": "pages/03_Listings.py",
        },
        {
            "queue": "Photo-Comp Pending Review",
            "semantic": "needs_action",
            "open_count": len(photo_comp_pending_review_listings),
            "target_page": "Listings",
            "path": "pages/03_Listings.py",
        },
        {
            "queue": "eBay Format Fix Queue",
            "semantic": "needs_action",
            "open_count": len(format_fix_listings),
            "target_page": "Listings",
            "path": "pages/03_Listings.py",
        },
        {
            "queue": "Listings Blocker Follow-ups",
            "semantic": "blocked",
            "open_count": len(open_blocker_followups),
            "target_page": "Listings",
            "path": "pages/03_Listings.py",
        },
        {
            "queue": "Governance Cadence Follow-ups",
            "semantic": "blocked",
            "open_count": len(open_governance_cadence_followups),
            "target_page": "Admin",
            "path": "pages/17_Admin.py",
        },
        {
            "queue": "Needs Shipment",
            "semantic": "needs_action",
            "open_count": len(needs_shipment_sales),
            "target_page": "Shipping",
            "path": "pages/11_Shipping.py",
        },
        {
            "queue": "Sync Failures",
            "semantic": "blocked",
            "open_count": len(recent_failed_sync),
            "target_page": "Sync",
            "path": "pages/18_Sync.py",
        },
        {
            "queue": "Accounting Exceptions",
            "semantic": "blocked",
            "open_count": len(accounting_exception_rows),
            "target_page": "Reports",
            "path": "pages/09_Reports.py",
        },
    ]
    st.dataframe(pd.DataFrame(queue_router_rows), use_container_width=True)
    qr_cols = st.columns(len(queue_router_rows))
    for idx, row in enumerate(queue_router_rows):
        with qr_cols[idx]:
            _page_button(
                f"Open {row['target_page']}",
                row["path"],
                key=f"ops_home_queue_router_{idx}",
            )

    st.markdown("### Quick Actions")
    st.caption(f"Role-aware action set for `{user.username}` ({user.role}).")
    actions = _action_rows_for_role(user.role)
    cols = st.columns(3)
    for idx, (label, page_path, description) in enumerate(actions):
        with cols[idx % 3]:
            _page_button(label, page_path, key=f"ops_home_nav_{idx}")
            st.caption(description)

    def _render_inventory_intake() -> None:
        if not needs_listing_products:
            render_workspace_empty_state(
                title="Inventory Intake Queue",
                detail="No products currently waiting for listing.",
            )
        else:
            queue_rows = [
                {
                    "id": row.id,
                    "product_id": row.id,
                    "sku": row.sku,
                    "title": row.title,
                    "category": row.category,
                    "qty_on_hand": int(row.current_quantity or 0),
                    "acquired_at": _format_dt(row.acquired_at),
                }
                for row in needs_listing_products[:200]
            ]
            st.dataframe(pd.DataFrame(queue_rows), use_container_width=True)
            render_standard_row_actions(
                repo,
                entity_type="product",
                rows=queue_rows,
                id_field="id",
                title="Needs Listing Actions",
            )

    def _render_ebay_listing_workflow() -> None:
        listing_rows = [
            {
                "id": row.id,
                "listing_id": row.id,
                "product_id": row.product_id,
                "marketplace": row.marketplace,
                "title": row.listing_title,
                    "status": row.listing_status,
                    "status_semantic": normalize_status_semantic(row.listing_status),
                    "external_listing_id": row.external_listing_id,
                    "marketplace_url": row.marketplace_url,
                    "qty": int(row.quantity_listed or 0),
                "price": float(row.listing_price or 0),
                "listed_at": _format_dt(row.listed_at),
            }
            for row in sorted(
                [r for r in listings if (r.marketplace or "").strip().lower() == "ebay"],
                key=lambda r: (str(r.listing_status or ""), str(r.listing_title or "")),
            )[:300]
        ]
        if not listing_rows:
            render_workspace_empty_state(
                title="eBay Listing Workflow",
                detail="No eBay listings found in current filter scope.",
            )
        else:
            st.dataframe(pd.DataFrame(listing_rows), use_container_width=True)
            render_standard_row_actions(
                repo,
                entity_type="listing",
                rows=listing_rows,
                id_field="id",
                title="eBay Listing Workflow Actions",
            )

    def _render_shipping_queue() -> None:
        if not needs_shipment_sales and not in_transit_sales and not exception_sales:
            render_workspace_empty_state(
                title="Shipping Queue",
                detail="No open shipment workload.",
            )
        else:
            top_rows = needs_shipment_sales[:100] + exception_sales[:100] + in_transit_sales[:100]
            queue_rows = [
                {
                    "id": row.id,
                    "sale_id": row.id,
                    "sold_at": _format_dt(row.sold_at),
                    "marketplace": row.marketplace,
                    "product_id": row.product_id,
                    "order_id": row.order_id,
                    "tracking_status": row.tracking_status,
                    "tracking_semantic": normalize_status_semantic(row.tracking_status),
                    "tracking_number": row.tracking_number,
                    "shipping_provider": row.shipping_provider,
                    "title": f"{row.marketplace} sale",
                }
                for row in top_rows
            ]
            st.dataframe(pd.DataFrame(queue_rows), use_container_width=True)
            render_standard_row_actions(
                repo,
                entity_type="sale",
                rows=queue_rows,
                id_field="id",
                title="Shipment Queue Actions",
            )

    def _render_photo_comp_review_queue() -> None:
        queue_rows = [
            {
                "id": row.id,
                "listing_id": row.id,
                "product_id": row.product_id,
                "marketplace": row.marketplace,
                "title": row.listing_title,
                "status": row.listing_status,
                "review_status": getattr(row, "review_status", "pending"),
                "price": float(row.listing_price or 0),
                "qty": int(row.quantity_listed or 0),
                "listed_at": _format_dt(row.listed_at),
            }
            for row in sorted(
                photo_comp_pending_review_listings,
                key=lambda r: (r.listed_at or utcnow_naive()),
            )[:300]
        ]
        if not queue_rows:
            render_workspace_empty_state(
                title="Photo-Comp Review Queue",
                detail="No photo-comp draft listings pending review.",
            )
        else:
            st.dataframe(pd.DataFrame(queue_rows), use_container_width=True)
            render_standard_row_actions(
                repo,
                entity_type="listing",
                rows=queue_rows,
                id_field="id",
                title="Photo-Comp Review Actions",
                search_edit_page="pages/03_Listings.py",
                edit_action_label="Open Listings Queue",
                edit_action_caption="Use Listings page for review and publish workflow.",
            )

    def _render_format_fix_queue() -> None:
        queue_rows = [
            {
                "id": row.id,
                "listing_id": row.id,
                "product_id": row.product_id,
                "marketplace": row.marketplace,
                "title": row.listing_title,
                "status": row.listing_status,
                "review_status": getattr(row, "review_status", "pending"),
                "format_hint": _listing_format_hint(
                    row,
                    default_format_type=default_format_type,
                    default_auction_duration=default_auction_duration,
                ),
                "price": float(row.listing_price or 0),
                "qty": int(row.quantity_listed or 0),
                "listed_at": _format_dt(row.listed_at),
            }
            for row in sorted(
                format_fix_listings,
                key=lambda r: (str(r.listing_status or ""), r.listed_at or utcnow_naive()),
            )[:300]
        ]
        if not queue_rows:
            render_workspace_empty_state(
                title="eBay Format Fix Queue",
                detail="No eBay listings with format setup issues in current filter scope.",
            )
        else:
            st.dataframe(pd.DataFrame(queue_rows), use_container_width=True)
            render_standard_row_actions(
                repo,
                entity_type="listing",
                rows=queue_rows,
                id_field="id",
                title="Format Fix Actions",
                search_edit_page="pages/03_Listings.py",
                edit_action_label="Open Listings Queue",
                edit_action_caption="Use Listings page with 'Format Issue Only' to fix and publish.",
            )

    def _render_listings_blocker_followups_queue() -> None:
        queue_rows = [
            {
                "task_key": row.get("task_key"),
                "title": row.get("title"),
                "blocker_reason": row.get("blocker_reason"),
                "owner": row.get("owner"),
                "priority": row.get("priority"),
                "status": row.get("status"),
                "sla_status": row.get("sla_status"),
                "due_in_days": row.get("due_in_days"),
                "due_date": row.get("due_date"),
                "last_action": row.get("last_action"),
                "last_actor": row.get("last_actor"),
                "last_updated_at": row.get("last_updated_at"),
            }
            for row in blocker_followup_rows[:300]
        ]
        if not queue_rows:
            render_workspace_empty_state(
                title="Listings Blocker Follow-ups",
                detail="No listings blocker follow-up tasks found.",
            )
        else:
            st.markdown("##### Saved Queue Presets")
            preset_scope = "operations_home_blocker_followups"
            preset_map: dict[str, tuple[object, dict]] = {}
            try:
                preset_rows = repo.list_saved_filter_profiles(
                    environment=settings.app_env,
                    scope=preset_scope,
                    username=user.username,
                    include_shared=True,
                    active_only=True,
                )
            except Exception:
                preset_rows = []
            for row in preset_rows:
                try:
                    parsed = json.loads(str(row.filter_json or "{}"))
                    if not isinstance(parsed, dict):
                        parsed = {}
                except Exception:
                    parsed = {}
                visibility = "Shared" if bool(row.is_shared) else "Mine"
                default_tag = " | Default" if bool(row.is_default) else ""
                owner_tag = f" | Owner:{row.username}" if bool(row.is_shared) else ""
                label = f"{row.name} [{visibility}{default_tag}{owner_tag}]"
                if label in preset_map:
                    label = f"{label} #{row.id}"
                preset_map[label] = (row, parsed)
            default_loaded_key = f"{preset_scope}_default_loaded_{settings.app_env}_{user.username}"
            if default_loaded_key not in st.session_state:
                st.session_state[default_loaded_key] = False
            own_default_label = None
            shared_default_label = None
            for label, (row_obj, _) in preset_map.items():
                if not bool(getattr(row_obj, "is_default", False)):
                    continue
                if str(getattr(row_obj, "username", "")).strip() == user.username.strip() and not bool(
                    getattr(row_obj, "is_shared", False)
                ):
                    own_default_label = label
                    break
                if bool(getattr(row_obj, "is_shared", False)) and shared_default_label is None:
                    shared_default_label = label
            default_label = own_default_label or shared_default_label
            if default_label and not bool(st.session_state.get(default_loaded_key)):
                default_payload = preset_map.get(default_label, (None, {}))[1]
                st.session_state["ops_home_blocker_followup_status_filter"] = str(default_payload.get("status") or "all")
                st.session_state["ops_home_blocker_followup_owner_filter"] = str(default_payload.get("owner") or "all")
                st.session_state["ops_home_blocker_followup_priority_filter"] = str(default_payload.get("priority") or "all")
                st.session_state["ops_home_blocker_followup_sla_filter"] = str(default_payload.get("sla_status") or "all")
                st.session_state[default_loaded_key] = True
                st.rerun()
            preset_labels = ["None"] + sorted(preset_map.keys())
            ps1, ps2, ps3, ps4 = st.columns(4)
            with ps1:
                selected_queue_preset = st.selectbox(
                    "Queue Preset",
                    options=preset_labels,
                    key="ops_home_blocker_followup_preset_select",
                )
            with ps2:
                if st.button("Apply Queue Preset", key="ops_home_blocker_followup_preset_apply"):
                    if selected_queue_preset == "None":
                        st.info("Select a queue preset first.")
                    else:
                        payload = preset_map.get(selected_queue_preset, (None, {}))[1]
                        st.session_state["ops_home_blocker_followup_status_filter"] = str(payload.get("status") or "all")
                        st.session_state["ops_home_blocker_followup_owner_filter"] = str(payload.get("owner") or "all")
                        st.session_state["ops_home_blocker_followup_priority_filter"] = str(payload.get("priority") or "all")
                        st.session_state["ops_home_blocker_followup_sla_filter"] = str(payload.get("sla_status") or "all")
                        st.success(f"Applied queue preset `{selected_queue_preset}`.")
                        st.rerun()
            with ps3:
                with st.form("ops_home_blocker_followup_preset_save_form"):
                    preset_name = st.text_input("Save Current As", key="ops_home_blocker_followup_preset_name")
                    preset_shared = st.checkbox("Team-shared", value=False, key="ops_home_blocker_followup_preset_shared")
                    preset_default = st.checkbox(
                        "Set as default",
                        value=False,
                        key="ops_home_blocker_followup_preset_default",
                    )
                    save_queue_preset = st.form_submit_button("Save Queue Preset")
                if save_queue_preset:
                    normalized_name = str(preset_name or "").strip()
                    if not normalized_name:
                        st.error("Preset name is required.")
                    else:
                        payload = {
                            "status": str(st.session_state.get("ops_home_blocker_followup_status_filter") or "all"),
                            "owner": str(st.session_state.get("ops_home_blocker_followup_owner_filter") or "all"),
                            "priority": str(st.session_state.get("ops_home_blocker_followup_priority_filter") or "all"),
                            "sla_status": str(st.session_state.get("ops_home_blocker_followup_sla_filter") or "all"),
                        }
                        try:
                            repo.upsert_saved_filter_profile(
                                environment=settings.app_env,
                                username=user.username,
                                scope=preset_scope,
                                name=normalized_name,
                                filter_json=json.dumps(payload),
                                is_shared=bool(preset_shared),
                                is_default=bool(preset_default),
                                is_active=True,
                                actor=user.username,
                            )
                            st.success(f"Saved queue preset `{normalized_name}`.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to save queue preset: {exc}")
            with ps4:
                if st.button("Set Default Preset", key="ops_home_blocker_followup_preset_set_default"):
                    if selected_queue_preset == "None":
                        st.info("Select a queue preset first.")
                    else:
                        row = preset_map.get(selected_queue_preset, (None, {}))[0]
                        payload = preset_map.get(selected_queue_preset, (None, {}))[1]
                        if row is None:
                            st.error("Preset not found.")
                        elif str(row.username or "").strip() != user.username:
                            st.error("Only the preset owner can set it as default.")
                        else:
                            try:
                                repo.upsert_saved_filter_profile(
                                    environment=settings.app_env,
                                    username=user.username,
                                    scope=preset_scope,
                                    name=str(row.name or "").strip(),
                                    filter_json=json.dumps(payload),
                                    is_shared=bool(row.is_shared),
                                    is_default=True,
                                    is_active=bool(row.is_active),
                                    actor=user.username,
                                )
                                st.session_state[default_loaded_key] = True
                                st.success(f"Set default queue preset `{row.name}`.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Unable to set default preset: {exc}")
                if st.button("Clear Default Preset", key="ops_home_blocker_followup_preset_clear_default"):
                    if selected_queue_preset == "None":
                        st.info("Select a queue preset first.")
                    else:
                        row = preset_map.get(selected_queue_preset, (None, {}))[0]
                        payload = preset_map.get(selected_queue_preset, (None, {}))[1]
                        if row is None:
                            st.error("Preset not found.")
                        elif str(row.username or "").strip() != user.username:
                            st.error("Only the preset owner can clear default.")
                        else:
                            try:
                                repo.upsert_saved_filter_profile(
                                    environment=settings.app_env,
                                    username=user.username,
                                    scope=preset_scope,
                                    name=str(row.name or "").strip(),
                                    filter_json=json.dumps(payload),
                                    is_shared=bool(row.is_shared),
                                    is_default=False,
                                    is_active=bool(row.is_active),
                                    actor=user.username,
                                )
                                st.success(f"Cleared default flag for `{row.name}`.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Unable to clear default preset: {exc}")
                if st.button("Delete Queue Preset", key="ops_home_blocker_followup_preset_delete"):
                    if selected_queue_preset == "None":
                        st.info("Select a queue preset first.")
                    else:
                        row = preset_map.get(selected_queue_preset, (None, {}))[0]
                        if row is None:
                            st.error("Preset not found.")
                        elif str(row.username or "").strip() != user.username:
                            st.error("Only the preset owner can delete it.")
                        else:
                            try:
                                repo.delete_saved_filter_profile_by_id(profile_id=row.id, actor=user.username)
                                st.success(f"Deleted queue preset `{selected_queue_preset}`.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Unable to delete queue preset: {exc}")
            status_options = sorted({str(row.get("status") or "").strip().lower() for row in queue_rows if str(row.get("status") or "").strip()})
            owner_options = sorted({str(row.get("owner") or "").strip() for row in queue_rows if str(row.get("owner") or "").strip()})
            priority_options = sorted({str(row.get("priority") or "").strip().lower() for row in queue_rows if str(row.get("priority") or "").strip()})
            sla_options = sorted({str(row.get("sla_status") or "").strip().lower() for row in queue_rows if str(row.get("sla_status") or "").strip()})
            st.markdown("##### Queue Filter Presets")
            qp1, qp2, qp3, qp4 = st.columns(4)
            with qp1:
                if st.button(
                    "Overdue Critical",
                    key="ops_home_blocker_followup_preset_overdue_critical",
                    use_container_width=True,
                ):
                    st.session_state["ops_home_blocker_followup_status_filter"] = (
                        "open" if "open" in status_options else "all"
                    )
                    st.session_state["ops_home_blocker_followup_owner_filter"] = "all"
                    st.session_state["ops_home_blocker_followup_priority_filter"] = (
                        "critical" if "critical" in priority_options else "all"
                    )
                    st.session_state["ops_home_blocker_followup_sla_filter"] = (
                        "overdue" if "overdue" in sla_options else "all"
                    )
                    st.rerun()
            with qp2:
                if st.button(
                    "My Open",
                    key="ops_home_blocker_followup_preset_my_open",
                    use_container_width=True,
                ):
                    owner_default = user.username if user.username in owner_options else "all"
                    st.session_state["ops_home_blocker_followup_status_filter"] = (
                        "open" if "open" in status_options else "all"
                    )
                    st.session_state["ops_home_blocker_followup_owner_filter"] = owner_default
                    st.session_state["ops_home_blocker_followup_priority_filter"] = "all"
                    st.session_state["ops_home_blocker_followup_sla_filter"] = "all"
                    st.rerun()
            with qp3:
                if st.button(
                    "High Priority Open",
                    key="ops_home_blocker_followup_preset_high_open",
                    use_container_width=True,
                ):
                    st.session_state["ops_home_blocker_followup_status_filter"] = (
                        "open" if "open" in status_options else "all"
                    )
                    st.session_state["ops_home_blocker_followup_owner_filter"] = "all"
                    st.session_state["ops_home_blocker_followup_priority_filter"] = (
                        "high" if "high" in priority_options else "all"
                    )
                    st.session_state["ops_home_blocker_followup_sla_filter"] = "all"
                    st.rerun()
            with qp4:
                if st.button(
                    "Reset Queue Filters",
                    key="ops_home_blocker_followup_preset_reset",
                    use_container_width=True,
                ):
                    st.session_state["ops_home_blocker_followup_status_filter"] = "all"
                    st.session_state["ops_home_blocker_followup_owner_filter"] = "all"
                    st.session_state["ops_home_blocker_followup_priority_filter"] = "all"
                    st.session_state["ops_home_blocker_followup_sla_filter"] = "all"
                    st.rerun()
            fq1, fq2, fq3, fq4 = st.columns(4)
            with fq1:
                status_filter = st.selectbox(
                    "Status Filter",
                    options=["all"] + status_options,
                    index=0,
                    key="ops_home_blocker_followup_status_filter",
                )
            with fq2:
                owner_filter = st.selectbox(
                    "Owner Filter",
                    options=["all"] + owner_options,
                    index=0,
                    key="ops_home_blocker_followup_owner_filter",
                )
            with fq3:
                priority_filter = st.selectbox(
                    "Priority Filter",
                    options=["all"] + priority_options,
                    index=0,
                    key="ops_home_blocker_followup_priority_filter",
                )
            with fq4:
                sla_filter = st.selectbox(
                    "SLA Filter",
                    options=["all"] + sla_options,
                    index=0,
                    key="ops_home_blocker_followup_sla_filter",
                )

            filtered_queue_rows = []
            for row in queue_rows:
                row_status = str(row.get("status") or "").strip().lower()
                row_owner = str(row.get("owner") or "").strip()
                row_priority = str(row.get("priority") or "").strip().lower()
                row_sla = str(row.get("sla_status") or "").strip().lower()
                if status_filter != "all" and row_status != status_filter:
                    continue
                if owner_filter != "all" and row_owner != owner_filter:
                    continue
                if priority_filter != "all" and row_priority != priority_filter:
                    continue
                if sla_filter != "all" and row_sla != sla_filter:
                    continue
                filtered_queue_rows.append(row)

            open_rows = [
                row for row in filtered_queue_rows if str(row.get("status") or "").strip().lower() == "open"
            ]
            due_soon_rows = [
                row
                for row in open_rows
                if str(row.get("sla_status") or "").strip().lower() == "due_soon"
            ]
            overdue_rows = [
                row
                for row in open_rows
                if str(row.get("sla_status") or "").strip().lower() == "overdue"
            ]
            q1, q2, q3 = st.columns(3)
            q1.metric("Open Follow-ups", int(len(open_rows)))
            q2.metric("Due Soon", int(len(due_soon_rows)))
            q3.metric("Overdue", int(len(overdue_rows)))
            st.dataframe(pd.DataFrame(filtered_queue_rows), use_container_width=True)
            if st.button(
                "Open Listings Blocker Workflow",
                key="ops_home_open_listings_blocker_workflow_btn",
                use_container_width=True,
            ):
                st.session_state["listings_readiness_filter"] = "blocked"
                st.session_state["workspace_handoff_from"] = "operations_home"
                st.session_state["workspace_handoff_target"] = "listings"
                if hasattr(st, "switch_page"):
                    safe_switch_page("pages/03_Listings.py")

    def _render_governance_cadence_followups_queue() -> None:
        queue_rows = [
            {
                "task_key": row.get("task_key"),
                "title": row.get("title"),
                "owner": row.get("owner"),
                "priority": row.get("priority"),
                "status": row.get("status"),
                "sla_status": row.get("sla_status"),
                "due_in_days": row.get("due_in_days"),
                "due_date": row.get("due_date"),
                "last_action": row.get("last_action"),
                "last_actor": row.get("last_actor"),
                "last_updated_at": row.get("last_updated_at"),
                "note": row.get("note"),
            }
            for row in governance_cadence_rows[:300]
        ]
        if not queue_rows:
            render_workspace_empty_state(
                title="Governance Cadence Follow-ups",
                detail="No governance cadence follow-up tasks found.",
            )
            return
        open_rows = [row for row in queue_rows if str(row.get("status") or "").strip().lower() == "open"]
        due_soon_rows = [
            row for row in open_rows if str(row.get("sla_status") or "").strip().lower() == "due_soon"
        ]
        overdue_rows = [
            row for row in open_rows if str(row.get("sla_status") or "").strip().lower() == "overdue"
        ]
        q1, q2, q3 = st.columns(3)
        q1.metric("Open Follow-ups", int(len(open_rows)))
        q2.metric("Due Soon", int(len(due_soon_rows)))
        q3.metric("Overdue", int(len(overdue_rows)))
        st.dataframe(pd.DataFrame(queue_rows), use_container_width=True)
        if open_rows:
            row_map = {
                (
                    f"{str(row.get('task_key') or '')} | owner={str(row.get('owner') or '')} | "
                    f"priority={str(row.get('priority') or '')} | due={str(row.get('due_date') or '')}"
                ): row
                for row in open_rows
            }
            selected_key = st.selectbox(
                "Resolve Cadence Follow-up",
                options=list(row_map.keys()),
                key="ops_home_cadence_followup_resolve_select",
            )
            resolution_note = st.text_input(
                "Resolution Note (optional)",
                key="ops_home_cadence_followup_resolve_note",
                placeholder="What changed to restore cadence?",
            )
            if st.button(
                "Mark Cadence Follow-up Resolved",
                key="ops_home_cadence_followup_resolve_btn",
                use_container_width=True,
            ):
                try:
                    selected = row_map.get(selected_key) or {}
                    repo.record_audit_event(
                        entity_type="workspace_followup",
                        entity_id=None,
                        action="resolve",
                        actor=user.username,
                        changes={
                            "task_key": str(selected.get("task_key") or ""),
                            "workflow": "governance_snapshot_cadence",
                            "resolution_note": str(resolution_note or "").strip(),
                            "resolved_at": utcnow_naive().isoformat(timespec="seconds"),
                            "status": "resolved",
                            "environment": settings.app_env,
                        },
                    )
                    st.success(f"Resolved cadence follow-up `{str(selected.get('task_key') or '')}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to resolve cadence follow-up: {exc}")
        if st.button(
            "Open Admin Sync Jobs",
            key="ops_home_open_admin_sync_jobs_from_cadence_queue_btn",
            use_container_width=True,
        ):
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/17_Admin.py")

    def _render_sync_failures() -> None:
        if not recent_failed_sync:
            render_workspace_empty_state(
                title="Sync Failures Queue",
                detail="No failed/partial sync runs in current window.",
            )
        else:
            queue_rows = [
                {
                    "id": row.id,
                    "run_id": row.id,
                    "title": f"{row.provider}:{row.job_name}",
                    "provider": row.provider,
                    "job_name": row.job_name,
                    "status": row.status,
                    "status_semantic": normalize_status_semantic(row.status),
                    "retry_count": row.retry_count,
                    "line_items_with_listing_link": row.line_items_with_listing_link,
                    "line_items_unmapped_sku": row.line_items_unmapped_sku,
                    "auto_listings_created": row.auto_listings_created,
                    "started_at": _format_dt(row.started_at),
                    "completed_at": _format_dt(row.completed_at),
                }
                for row in recent_failed_sync[:200]
            ]
            st.dataframe(
                pd.DataFrame(queue_rows),
                use_container_width=True,
            )
            render_standard_row_actions(
                repo,
                entity_type="sync_run",
                rows=queue_rows,
                id_field="id",
                title="Sync Failure Actions",
                search_edit_page="pages/18_Sync.py",
                edit_action_label="Open Sync Queue",
                edit_action_caption="Use Sync page to retry failed runs and resolve errors.",
            )

    def _render_accounting_exceptions() -> None:
        if not accounting_exception_rows:
            render_workspace_empty_state(
                title="Accounting Exceptions Queue",
                detail="No accounting linkage exceptions detected.",
            )
        else:
            st.dataframe(pd.DataFrame(accounting_exception_rows[:300]), use_container_width=True)
            order_rows = [
                {
                    "id": row["order_id"],
                    "title": row["external_order_id"] or f"order-{row['order_id']}",
                    "marketplace": row["marketplace"],
                }
                for row in accounting_exception_rows[:300]
                if row.get("order_id")
            ]
            if order_rows:
                render_standard_row_actions(
                    repo,
                    entity_type="order",
                    rows=order_rows,
                    id_field="id",
                    title="Accounting Exception Order Actions",
                )

    queue_renderers = {
        "Inventory Intake": _render_inventory_intake,
        "eBay Listing Workflow": _render_ebay_listing_workflow,
        "eBay Format Fix Queue": _render_format_fix_queue,
        "Listings Blocker Follow-ups": _render_listings_blocker_followups_queue,
        "Governance Cadence Follow-ups": _render_governance_cadence_followups_queue,
        "Photo-Comp Review Queue": _render_photo_comp_review_queue,
        "Shipping Queue": _render_shipping_queue,
        "Sync Failures": _render_sync_failures,
        "Accounting Exceptions": _render_accounting_exceptions,
    }
    if queue_view == "All Queues":
        queue_labels = list(queue_renderers.keys())
        queue_tabs = st.tabs(queue_labels)
        for idx, label in enumerate(queue_labels):
            with queue_tabs[idx]:
                queue_renderers[label]()
    else:
        st.markdown(f"### {queue_view}")
        renderer = queue_renderers.get(queue_view)
        if renderer is not None:
            renderer()

    st.caption(
        "v0.4 in progress: inventory + eBay-first command-center UX with clear workflow stages and SLA watch."
    )
    st.divider()
    render_workspace_task_completion(
        repo=repo,
        actor=user.username,
        workflow_key="operations_home",
        section_title="Workflow Completion: Operations Home",
        tasks=[
            ("Routed inventory intake queue", "inventory_intake_routed"),
            ("Reviewed eBay listing blockers", "ebay_blockers_reviewed"),
            ("Cleared sync/shipping exception queue", "ops_exception_queue_cleared"),
        ],
    )
    st.divider()
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="operations_home",
        section_title="Operations Home Feedback",
    )
