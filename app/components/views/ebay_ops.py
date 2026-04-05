import json
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import to_decimal
from app.components.views.ebay_context import render_active_ebay_context_banner
from app.components.views.shared import render_ebay_push_history, render_help_panel, render_table_toolbar
from app.components.views.entity_ops import render_entity_timeline
from app.components.views.workspace_shell import (
    normalize_status_semantic,
    render_ebay_command_rail,
    render_workspace_empty_state,
)
from app.config import settings
from app.db.models import MarketplaceListing, Product
from app.repository import InventoryRepository
from app.services.ebay import EbayClient
from app.services.runtime_settings import get_runtime_bool, get_runtime_str
from app.utils.time import utc_today


def _parse_offer_id(details: str) -> str:
    raw = (details or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            ebay_publish = parsed.get("ebay_publish") or {}
            if isinstance(ebay_publish, dict):
                return str(ebay_publish.get("offer_id") or "").strip()
    except Exception:
        return ""
    return ""


def _parse_details_obj(details: str) -> dict:
    raw = (details or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return {"notes": raw}
    return {}


def _merge_defaults_into_listing_details(listing_details: str, defaults: dict) -> str:
    data = _parse_details_obj(listing_details)
    data["ebay_ops_defaults"] = defaults
    return json.dumps(data, indent=2)


def _resolve_offer_id(
    client: EbayClient,
    access_token: str,
    listing,
    sku: str,
    offers_cache: dict[str, list[dict]],
) -> str:
    known = _parse_offer_id(listing.marketplace_details)
    if known:
        return known

    if not sku:
        return ""
    offers = offers_cache.get(sku)
    if offers is None:
        payload = client.get_offers(access_token=access_token, sku=sku)
        offers = payload.get("offers") or []
        offers_cache[sku] = offers

    listing_id = (listing.external_listing_id or "").strip()
    for offer in offers:
        if listing_id and str(offer.get("listingId") or "").strip() == listing_id:
            return str(offer.get("offerId") or "").strip()
    if len(offers) == 1:
        return str(offers[0].get("offerId") or "").strip()
    return ""


def _listings_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=["listing_id", "sku", "title", "status", "external_listing_id", "price", "qty", "offer_id_guess"]
        )
    return pd.DataFrame(rows)


def _policy_label(row: dict, id_field: str = "paymentPolicyId") -> str:
    policy_id = str(row.get(id_field) or "").strip()
    name = str(row.get("name") or row.get("policyName") or "").strip()
    category_types = row.get("categoryTypes") or []
    category_name = ""
    if isinstance(category_types, list) and category_types:
        first = category_types[0] or {}
        category_name = str(first.get("name") or "").strip()
    default_flag = "default" if bool(row.get("default")) else ""
    parts = [policy_id, name, category_name, default_flag]
    return " | ".join([p for p in parts if p]) or policy_id


def _location_label(row: dict) -> str:
    key = str(row.get("merchantLocationKey") or "").strip()
    location = row.get("location") or {}
    address = location.get("address") or {}
    city = str(address.get("city") or "").strip()
    state = str(address.get("stateOrProvince") or "").strip()
    country = str(address.get("country") or "").strip()
    status = str(row.get("status") or "").strip()
    parts = [key, city, state, country, status]
    return " | ".join([p for p in parts if p]) or key


def _filtered_ebay_listing_rows(
    repo: InventoryRepository,
    *,
    status_filter: list[str],
    linked_only: bool,
    query: str,
    use_date_filter: bool,
    listed_date_range,
) -> list[tuple[MarketplaceListing, str]]:
    all_rows = [l for l in repo.list_listings() if (l.marketplace or "").strip().lower() == "ebay"]
    qv = (query or "").strip().lower()
    listed_from = None
    listed_to = None
    if use_date_filter:
        if isinstance(listed_date_range, tuple) and len(listed_date_range) == 2:
            listed_from = listed_date_range[0]
            listed_to = listed_date_range[1]
        elif listed_date_range is not None:
            listed_from = listed_date_range
            listed_to = listed_date_range

    filtered: list[tuple[MarketplaceListing, str]] = []
    status_set = {str(s or "").strip().lower() for s in (status_filter or []) if str(s or "").strip()}
    for listing in all_rows:
        product = repo.db.get(Product, listing.product_id)
        sku = (product.sku if product else "").strip()
        listing_status = (listing.listing_status or "").strip().lower()
        if status_set and listing_status not in status_set:
            continue
        if linked_only and not (listing.external_listing_id or "").strip():
            continue
        if qv and qv not in (listing.listing_title or "").lower() and qv not in sku.lower() and qv not in (
            listing.external_listing_id or ""
        ).lower():
            continue
        if use_date_filter:
            listed_at = listing.listed_at.date() if listing.listed_at is not None else None
            if listed_at is None:
                continue
            if listed_from and listed_at < listed_from:
                continue
            if listed_to and listed_at > listed_to:
                continue
        filtered.append((listing, sku))
    return filtered


def _render_listing_side_panel(
    repo: InventoryRepository,
    *,
    listing: MarketplaceListing,
    sku: str,
    panel_key_prefix: str,
) -> None:
    product = repo.db.get(Product, listing.product_id)
    tab_detail, tab_sync_lineage, tab_timeline = st.tabs(["Detail", "Sync Lineage", "Timeline"])

    with tab_detail:
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Listing ID", listing.id)
        d2.metric("Status", str(listing.listing_status or ""))
        d3.metric("Review", str(listing.review_status or ""))
        d4.metric("Qty", int(listing.quantity_listed or 0))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "listing_id": listing.id,
                        "product_id": listing.product_id,
                        "sku": sku,
                        "product_title": product.title if product else "",
                        "marketplace": listing.marketplace,
                        "external_listing_id": listing.external_listing_id,
                        "marketplace_url": listing.marketplace_url,
                        "listing_title": listing.listing_title,
                        "listing_price": float(listing.listing_price),
                        "review_status": listing.review_status,
                        "reviewed_at": listing.reviewed_at,
                        "reviewed_by": listing.reviewed_by,
                        "listed_at": listing.listed_at,
                        "created_at": listing.created_at,
                        "updated_at": listing.updated_at,
                    }
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("Listing Details Payload (JSON)", expanded=False):
            details_obj = _parse_details_obj(listing.marketplace_details)
            if details_obj:
                st.json(details_obj)
            else:
                st.caption("No marketplace details payload stored.")
        listing_media = repo.list_media_assets_for_listing(listing.id)
        st.caption(f"Linked media assets: {len(listing_media)}")
        if listing_media:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "media_id": m.id,
                            "type": m.media_type,
                            "filename": m.original_filename,
                            "content_type": m.content_type,
                            "size_bytes": m.size_bytes,
                            "url": m.s3_url,
                            "uploaded_by": m.uploaded_by,
                            "created_at": m.created_at,
                        }
                        for m in listing_media
                    ]
                ),
                use_container_width=True,
            )

    with tab_sync_lineage:
        listing_events = repo.list_sync_events_for_entity(
            entity_type="listing",
            entity_id=listing.id,
            limit=500,
        )
        if not listing_events:
            render_workspace_empty_state(
                title="Listing Sync Lineage",
                detail="No sync lineage found for this listing.",
            )
        else:
            run_ids = sorted({int(e.sync_run_id) for e in listing_events if e.sync_run_id})
            run_index = {r.id: r for r in repo.list_sync_runs(provider="ebay", limit=500)}
            unresolved_by_run: dict[int, int] = {}
            for run_id in run_ids:
                run_errors = repo.list_sync_errors(run_id, limit=500)
                unresolved_by_run[run_id] = sum(1 for err in run_errors if err.resolved_at is None)
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "event_id": e.id,
                            "sync_run_id": e.sync_run_id,
                            "provider": (run_index.get(e.sync_run_id).provider if run_index.get(e.sync_run_id) else ""),
                            "job_name": (run_index.get(e.sync_run_id).job_name if run_index.get(e.sync_run_id) else ""),
                            "run_status": (run_index.get(e.sync_run_id).status if run_index.get(e.sync_run_id) else ""),
                            "run_status_semantic": normalize_status_semantic(
                                (run_index.get(e.sync_run_id).status if run_index.get(e.sync_run_id) else "")
                            ),
                            "retry_count": (run_index.get(e.sync_run_id).retry_count if run_index.get(e.sync_run_id) else ""),
                            "unresolved_errors": unresolved_by_run.get(int(e.sync_run_id or 0), 0),
                            "action": e.action,
                            "status": e.status,
                            "status_semantic": normalize_status_semantic(e.status),
                            "message": e.message,
                            "created_at": e.created_at,
                        }
                        for e in listing_events
                    ]
                ),
                use_container_width=True,
            )

    with tab_timeline:
        render_entity_timeline(
            repo,
            entity_type="listing",
            entity_id=listing.id,
            title=f"Listing #{listing.id} Timeline",
            key_prefix=f"{panel_key_prefix}_{listing.id}",
        )


def render_ebay_ops(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("eBay Ops")
    st.caption("Centralized daily eBay listing operations with filters and bulk actions.")
    render_help_panel(
        section_title="eBay Ops",
        goal="Run end/relist/revise operations in bulk for eBay-linked listings.",
        steps=[
            "Filter eBay listings by status and keyword.",
            "Select rows, then run bulk action: end or relist.",
            "Queue selected rows for revise, then execute revise queue in one run.",
            "Use this page for operational management after listing creation/publish.",
        ],
        roadmap_phase="v0.3 Channel Sync + Accounting Readiness",
    )
    render_active_ebay_context_banner(section_title="eBay Ops")
    if not bool(st.session_state.get("ebay_workspace_runbook_ready")):
        st.warning(
            "Runbook checklist is not complete in eBay Workspace. Complete it before high-impact bulk operations."
        )

    client = EbayClient()
    if not client.is_configured():
        st.warning("eBay credentials are not configured.")
        return
    allow_sandbox_ops = get_runtime_bool(
        repo,
        "ebay_allow_sandbox_seller_ops",
        bool(settings.ebay_allow_sandbox_seller_ops),
    )
    sandbox_seller_ops_blocked = client.environment != "production" and not allow_sandbox_ops
    require_runbook_for_bulk_ops = get_runtime_bool(
        repo,
        "ebay_require_runbook_for_bulk_ops",
        False,
    )
    runbook_ready = bool(st.session_state.get("ebay_workspace_runbook_ready"))
    runbook_bulk_ops_blocked = bool(require_runbook_for_bulk_ops and not runbook_ready)
    bulk_ops_blocked = bool(sandbox_seller_ops_blocked or runbook_bulk_ops_blocked)
    if sandbox_seller_ops_blocked:
        st.warning(
            "Sandbox mode detected. Seller operation controls are disabled by default because sandbox onboarding and "
            "Business Policy APIs are unreliable. Set `EBAY_ALLOW_SANDBOX_SELLER_OPS=true` to override."
        )
    if runbook_bulk_ops_blocked:
        st.warning(
            "Bulk operation guard is enabled (`ebay_require_runbook_for_bulk_ops=true`) and runbook is not complete."
        )

    workspace_token = str(st.session_state.get("ebay_workspace_access_token") or "").strip()
    default_token = (
        workspace_token
        or get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    )
    if "ebay_ops_access_token" not in st.session_state:
        st.session_state["ebay_ops_access_token"] = default_token
    if "ebay_ops_status_filter" not in st.session_state:
        st.session_state["ebay_ops_status_filter"] = ["draft", "active", "ended"]
    if "ebay_ops_linked_only" not in st.session_state:
        st.session_state["ebay_ops_linked_only"] = False
    if "ebay_ops_search_query" not in st.session_state:
        st.session_state["ebay_ops_search_query"] = ""
    if "ebay_ops_use_date_filter" not in st.session_state:
        st.session_state["ebay_ops_use_date_filter"] = bool(st.session_state.get("ebay_workspace_use_date_filter"))
    if "ebay_ops_listed_date_range" not in st.session_state:
        st.session_state["ebay_ops_listed_date_range"] = st.session_state.get(
            "ebay_workspace_listed_date_range",
            (utc_today() - timedelta(days=30), utc_today()),
        )
    default_marketplace_id = get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id).strip()
    default_content_language = get_runtime_str(
        repo,
        "ebay_content_language",
        settings.ebay_content_language,
    ).strip()
    default_currency = get_runtime_str(repo, "ebay_currency", settings.ebay_currency).strip()
    default_merchant_location = get_runtime_str(
        repo,
        "ebay_merchant_location_key",
        settings.ebay_merchant_location_key,
    ).strip()
    default_payment_policy = get_runtime_str(
        repo,
        "ebay_payment_policy_id",
        settings.ebay_payment_policy_id,
    ).strip()
    default_fulfillment_policy = get_runtime_str(
        repo,
        "ebay_fulfillment_policy_id",
        settings.ebay_fulfillment_policy_id,
    ).strip()
    default_return_policy = get_runtime_str(
        repo,
        "ebay_return_policy_id",
        settings.ebay_return_policy_id,
    ).strip()

    profile_map = st.session_state.get("ebay_workspace_saved_profiles") or {}
    if profile_map:
        st.markdown("### Account Profile Quick Switch")
        profile_labels = ["None"] + sorted(profile_map.keys())
        selected_profile = st.selectbox(
            "Profile",
            options=profile_labels,
            key="ebay_ops_profile_quick_select",
        )
        if st.button(
            "Apply Profile to Integration + Ops",
            key="ebay_ops_profile_apply_btn",
            disabled=selected_profile == "None",
        ):
            payload = profile_map.get(selected_profile) or {}
            token_val = str(payload.get("access_token") or "").strip()
            if token_val:
                st.session_state["ebay_workspace_access_token"] = token_val
                st.session_state["ebay_ops_access_token"] = token_val
                st.session_state["ebay_verify_access_token"] = token_val
                st.session_state["ebay_pull_access_token"] = token_val
            st.session_state["ebay_workspace_status_filter"] = list(
                payload.get("status_filter") or ["draft", "active", "ended"]
            )
            st.session_state["ebay_workspace_linked_only"] = bool(payload.get("linked_only"))
            st.session_state["ebay_workspace_search"] = str(payload.get("search") or "").strip()
            st.session_state["ebay_workspace_use_date_filter"] = bool(payload.get("use_date_filter"))
            stored_range = payload.get("listed_date_range")
            if isinstance(stored_range, list) and len(stored_range) == 2:
                try:
                    start_date = datetime.fromisoformat(str(stored_range[0])).date()
                    end_date = datetime.fromisoformat(str(stored_range[1])).date()
                    st.session_state["ebay_workspace_listed_date_range"] = (start_date, end_date)
                    st.session_state["ebay_ops_listed_date_range"] = (start_date, end_date)
                except Exception:
                    pass
            st.session_state["ebay_ops_status_filter"] = list(st.session_state["ebay_workspace_status_filter"])
            st.session_state["ebay_ops_linked_only"] = bool(st.session_state["ebay_workspace_linked_only"])
            st.session_state["ebay_ops_search_query"] = str(st.session_state["ebay_workspace_search"])
            st.session_state["ebay_ops_use_date_filter"] = bool(st.session_state["ebay_workspace_use_date_filter"])
            st.session_state["ebay_workspace_active_profile"] = selected_profile
            st.success(f"Applied profile `{selected_profile}`.")
            st.rerun()

    token = st.text_area(
        "User Access Token",
        height=110,
        help="Defaults to `EBAY_USER_ACCESS_TOKEN`.",
        key="ebay_ops_access_token",
    ).strip()
    if not token:
        st.info("Provide an eBay user access token to run operations.")
        return

    st.markdown("### Shared Listing Filter Bar")
    status_filter = st.multiselect(
        "Status Filter",
        options=["draft", "active", "ended"],
        key="ebay_ops_status_filter",
        help="Shared across Local Ops and eBay API Listings tabs.",
    )
    f1, f2, f3 = st.columns([1, 1, 2])
    with f1:
        linked_only = st.checkbox(
            "Only linked listings (external listing ID present)",
            key="ebay_ops_linked_only",
        )
    with f2:
        use_date_filter = st.checkbox(
            "Use listed date range",
            key="ebay_ops_use_date_filter",
        )
    with f3:
        listed_date_range = st.date_input(
            "Listed Date Range",
            key="ebay_ops_listed_date_range",
            help="Filters listings by local listed_at date.",
        )
    q = st.text_input(
        "Search (title, SKU, external listing ID)",
        key="ebay_ops_search_query",
    )
    filtered = _filtered_ebay_listing_rows(
        repo,
        status_filter=status_filter,
        linked_only=bool(linked_only),
        query=q,
        use_date_filter=bool(use_date_filter),
        listed_date_range=listed_date_range,
    )

    tab_local_ops, tab_api_listings, tab_policies = st.tabs(
        ["Local Ops", "eBay API Listings", "Policies & Locations"]
    )

    with tab_local_ops:
        qv = q.strip().lower()
        listed_from = None
        listed_to = None
        if use_date_filter:
            if isinstance(listed_date_range, tuple) and len(listed_date_range) == 2:
                listed_from = listed_date_range[0]
                listed_to = listed_date_range[1]
            elif listed_date_range is not None:
                listed_from = listed_date_range
                listed_to = listed_date_range

        preview_rows = []
        select_map: dict[str, tuple] = {}
        for listing, sku in filtered:
            offer_guess = _parse_offer_id(listing.marketplace_details)
            label = f"#{listing.id} | {sku or 'NO-SKU'} | {listing.listing_title}"
            select_map[label] = (listing, sku)
            preview_rows.append(
                {
                    "listing_id": listing.id,
                    "sku": sku,
                    "title": listing.listing_title,
                    "status": listing.listing_status,
                    "status_semantic": normalize_status_semantic(listing.listing_status),
                    "external_listing_id": listing.external_listing_id,
                    "price": float(listing.listing_price),
                    "qty": int(listing.quantity_listed),
                    "offer_id_guess": offer_guess,
                }
            )

        preview_df = _listings_frame(preview_rows)
        if preview_df.empty:
            render_workspace_empty_state(
                title="Local Ops",
                detail="No eBay listings match the current filters.",
            )
        render_table_toolbar(
            df=preview_df,
            section_key="ebay_ops_local_listings",
            export_basename="ebay_ops_local_listings",
            active_filters={
                "status": status_filter,
                "linked_only": "true" if linked_only else "",
                "query": qv,
                "use_listed_date_range": "true" if use_date_filter else "",
                "listed_from": listed_from.isoformat() if listed_from else "",
                "listed_to": listed_to.isoformat() if listed_to else "",
            },
        )
        st.dataframe(preview_df, use_container_width=True)
        selected_labels = st.multiselect(
            "Select Listings For Bulk Ops",
            options=list(select_map.keys()),
            key="ebay_ops_selected_labels",
        )
        selected = [select_map[label] for label in selected_labels if label in select_map]

        offers_cache: dict[str, list[dict]] = {}
        sync_runs = repo.list_sync_runs(provider="ebay", limit=200)
        sync_run_options = {
            f"#{r.id} | {r.job_name} | {r.status} | {r.started_at}": r.id
            for r in sync_runs
        }
        rail = render_ebay_command_rail(
            key_prefix="ebay_ops_cmd",
            selected_count=len(selected),
            sandbox_seller_ops_blocked=bulk_ops_blocked,
            sync_run_options=sync_run_options,
        )

        if rail.open_sync_page and hasattr(st, "switch_page"):
            st.switch_page("pages/14_Sync.py")
        if rail.clear_revise_queue:
            st.session_state["ebay_ops_revise_queue_ids"] = []
            st.success("Cleared revise queue.")
            st.rerun()
        if rail.run_add_selected_to_revise:
            queue_ids: list[int] = st.session_state.get("ebay_ops_revise_queue_ids", [])
            queue_ids = sorted(set(queue_ids + [row[0].id for row in selected]))
            st.session_state["ebay_ops_revise_queue_ids"] = queue_ids
            st.success(f"Queued {len(selected)} listing(s) for revise.")
            st.rerun()
        if rail.run_remove_selected_from_revise:
            queue_ids = st.session_state.get("ebay_ops_revise_queue_ids", [])
            selected_ids = {row[0].id for row in selected}
            queue_ids = [item for item in queue_ids if item not in selected_ids]
            st.session_state["ebay_ops_revise_queue_ids"] = queue_ids
            st.success(f"Unqueued {len(selected_ids)} selected listing(s).")
            st.rerun()

        if rail.run_end_selected or rail.run_relist_selected:
            if not ensure_permission(user, "bulk_update", "Bulk eBay Listing Action"):
                st.stop()
            action = "end" if rail.run_end_selected else "relist"
            success = 0
            errors: list[dict] = []
            for listing, sku in selected:
                try:
                    offer_id = _resolve_offer_id(client, token, listing, sku, offers_cache)
                    if not offer_id:
                        raise RuntimeError("Offer ID not found.")
                    if action == "end":
                        client.withdraw_offer(access_token=token, offer_id=offer_id)
                        repo.update_listing(listing.id, {"listing_status": "ended"}, actor=user.username)
                    else:
                        result = client.publish_offer(access_token=token, offer_id=offer_id)
                        listing_id = str(result.get("listingId") or "").strip()
                        updates = {"listing_status": "active"}
                        if listing_id:
                            updates["external_listing_id"] = listing_id
                            updates["marketplace_url"] = client.listing_url_for_id(listing_id)
                        repo.update_listing(listing.id, updates, actor=user.username)
                    success += 1
                except Exception as exc:
                    errors.append({"listing_id": listing.id, "title": listing.listing_title, "error": str(exc)})
            st.success(f"Command rail `{action}` complete. success={success}, failed={len(errors)}")
            if errors:
                st.dataframe(pd.DataFrame(errors), use_container_width=True)
            st.rerun()

        if rail.retry_run_now and rail.selected_sync_run_id:
            if not ensure_permission(user, "bulk_update", "Retry Sync Run"):
                st.stop()
            try:
                retry_row = repo.retry_sync_run(int(rail.selected_sync_run_id), actor=user.username)
                st.success(f"Created retry run #{retry_row.id} for source run #{rail.selected_sync_run_id}.")
            except Exception as exc:
                st.error(f"Retry failed: {exc}")

        if rail.resolve_run_errors and rail.selected_sync_run_id:
            if not ensure_permission(user, "bulk_update", "Resolve Sync Errors"):
                st.stop()
            try:
                run_errors = repo.list_sync_errors(int(rail.selected_sync_run_id), limit=1000)
                unresolved = [err for err in run_errors if err.resolved_at is None]
                resolved = 0
                for err in unresolved:
                    repo.resolve_sync_error(err.id, actor=user.username)
                    resolved += 1
                st.success(f"Resolved {resolved} unresolved error(s) for run #{rail.selected_sync_run_id}.")
            except Exception as exc:
                st.error(f"Resolve errors failed: {exc}")

        st.markdown("### Revise Queue Overrides")
        o1, o2 = st.columns(2)
        with o1:
            override_qty = st.number_input(
                "Override Quantity (0 = keep listing quantity)",
                min_value=0,
                value=0,
                step=1,
                key="ebay_ops_override_qty",
            )
        with o2:
            override_price = st.number_input(
                "Override Price (0 = keep listing price)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="ebay_ops_override_price",
            )

        st.markdown("### Listing Side Panel")
        side_panel_label = st.selectbox(
            "Select listing for detail/timeline",
            options=["None"] + list(select_map.keys()),
            key="ebay_ops_side_panel_listing_label",
        )
        if side_panel_label != "None":
            panel_listing, panel_sku = select_map[side_panel_label]
            _render_listing_side_panel(
                repo,
                listing=panel_listing,
                sku=panel_sku,
                panel_key_prefix="ebay_ops_local_panel",
            )

        st.markdown("### Bulk Category/Policy Assignment Helper")
        with st.form("ebay_ops_bulk_category_policy_form"):
            c1, c2, c3 = st.columns(3)
            with c1:
                bulk_category_id = st.text_input("Category ID", key="ebay_ops_bulk_category_id")
            with c2:
                bulk_marketplace_id = st.text_input(
                    "Marketplace ID",
                    value=default_marketplace_id,
                    key="ebay_ops_bulk_marketplace_id",
                )
            with c3:
                bulk_content_language = st.text_input(
                    "Content Language",
                    value=default_content_language,
                    key="ebay_ops_bulk_content_language",
                )
            p1, p2, p3 = st.columns(3)
            with p1:
                bulk_merchant_location_key = st.text_input(
                    "Merchant Location Key",
                    value=default_merchant_location,
                    key="ebay_ops_bulk_merchant_location_key",
                )
            with p2:
                bulk_payment_policy_id = st.text_input(
                    "Payment Policy ID",
                    value=default_payment_policy,
                    key="ebay_ops_bulk_payment_policy_id",
                )
            with p3:
                bulk_fulfillment_policy_id = st.text_input(
                    "Fulfillment Policy ID",
                    value=default_fulfillment_policy,
                    key="ebay_ops_bulk_fulfillment_policy_id",
                )
            b1, b2, b3 = st.columns(3)
            with b1:
                bulk_return_policy_id = st.text_input(
                    "Return Policy ID",
                    value=default_return_policy,
                    key="ebay_ops_bulk_return_policy_id",
                )
            with b2:
                bulk_format = st.selectbox("Format", options=["FIXED_PRICE", "AUCTION"], key="ebay_ops_bulk_format")
            with b3:
                bulk_duration = st.text_input("Listing Duration", value="GTC", key="ebay_ops_bulk_duration")
            push_updates_now = st.checkbox(
                "Push updates to eBay offers now (requires linked offer resolution)",
                value=False,
                key="ebay_ops_bulk_push_now",
            )
            apply_bulk_defaults = st.form_submit_button(
                "Apply To Selected Listings",
                disabled=bulk_ops_blocked,
            )

        if apply_bulk_defaults:
            if not ensure_permission(user, "bulk_update", "Bulk Category/Policy Assignment"):
                st.stop()
            if not selected:
                st.error("Select at least one listing.")
                st.stop()
            if not bulk_category_id.strip():
                st.error("Category ID is required.")
                st.stop()
            if not bulk_merchant_location_key.strip():
                st.error("Merchant Location Key is required.")
                st.stop()
            if not bulk_payment_policy_id.strip() or not bulk_fulfillment_policy_id.strip() or not bulk_return_policy_id.strip():
                st.error("Payment, fulfillment, and return policy IDs are required.")
                st.stop()

            defaults_payload = {
                "category_id": bulk_category_id.strip(),
                "marketplace_id": bulk_marketplace_id.strip() or default_marketplace_id,
                "content_language": bulk_content_language.strip() or default_content_language,
                "merchant_location_key": bulk_merchant_location_key.strip(),
                "payment_policy_id": bulk_payment_policy_id.strip(),
                "fulfillment_policy_id": bulk_fulfillment_policy_id.strip(),
                "return_policy_id": bulk_return_policy_id.strip(),
                "format": (bulk_format or "FIXED_PRICE").strip().upper(),
                "listing_duration": (bulk_duration or "GTC").strip().upper(),
            }

            success = 0
            push_success = 0
            errors: list[dict] = []
            for listing, sku in selected:
                try:
                    merged_details = _merge_defaults_into_listing_details(listing.marketplace_details, defaults_payload)
                    repo.update_listing(listing.id, {"marketplace_details": merged_details}, actor=user.username)
                    success += 1

                    if push_updates_now:
                        offer_id = _resolve_offer_id(client, token, listing, sku, offers_cache)
                        if not offer_id:
                            raise RuntimeError("Offer ID not found for push update.")
                        current_offer = client.get_offer(access_token=token, offer_id=offer_id)
                        update_payload = {}
                        for key in [
                            "sku",
                            "marketplaceId",
                            "format",
                            "availableQuantity",
                            "categoryId",
                            "merchantLocationKey",
                            "listingDescription",
                            "listingDuration",
                            "listingPolicies",
                            "pricingSummary",
                        ]:
                            if key in current_offer:
                                update_payload[key] = current_offer[key]
                        update_payload["marketplaceId"] = defaults_payload["marketplace_id"]
                        update_payload["format"] = defaults_payload["format"]
                        update_payload["categoryId"] = defaults_payload["category_id"]
                        update_payload["merchantLocationKey"] = defaults_payload["merchant_location_key"]
                        update_payload["listingDuration"] = defaults_payload["listing_duration"]
                        update_payload["listingPolicies"] = {
                            "paymentPolicyId": defaults_payload["payment_policy_id"],
                            "fulfillmentPolicyId": defaults_payload["fulfillment_policy_id"],
                            "returnPolicyId": defaults_payload["return_policy_id"],
                        }
                        client.update_offer(
                            access_token=token,
                            offer_id=offer_id,
                            payload=update_payload,
                            content_language=defaults_payload["content_language"],
                        )
                        push_success += 1
                except Exception as exc:
                    errors.append({"listing_id": listing.id, "title": listing.listing_title, "error": str(exc)})

            st.success(
                f"Bulk defaults applied to {success} listing(s). "
                f"eBay push updates={push_success}."
            )
            if errors:
                st.dataframe(pd.DataFrame(errors), use_container_width=True)
            st.rerun()

        st.markdown("### Bulk Revise Queue")
        queue_ids: list[int] = st.session_state.get("ebay_ops_revise_queue_ids", [])

        queue_rows = []
        for listing_id in queue_ids:
            listing = next((row[0] for row in filtered if row[0].id == listing_id), None)
            if listing is None:
                listing = repo.db.get(MarketplaceListing, listing_id)
            if listing is None:
                continue
            product = repo.db.get(Product, listing.product_id)
            queue_rows.append(
                {
                    "listing_id": listing.id,
                    "sku": (product.sku if product else ""),
                    "title": listing.listing_title,
                    "status": listing.listing_status,
                    "status_semantic": normalize_status_semantic(listing.listing_status),
                    "price": float(listing.listing_price),
                    "qty": int(listing.quantity_listed),
                }
            )
        queue_df = _listings_frame(queue_rows)
        if queue_df.empty:
            render_workspace_empty_state(
                title="Bulk Revise Queue",
                detail="Revise queue is empty.",
            )
        render_table_toolbar(
            df=queue_df,
            section_key="ebay_ops_revise_queue",
            export_basename="ebay_ops_revise_queue",
            active_filters={"queue": "revise"},
        )
        st.dataframe(queue_df, use_container_width=True)
        if rail.run_revise_queue:
            if not ensure_permission(user, "bulk_update", "Run Revise Queue"):
                st.stop()
            if not queue_ids:
                st.error("Revise queue is empty.")
                st.stop()

            success = 0
            errors: list[dict] = []
            for listing_id in queue_ids:
                listing = repo.db.get(MarketplaceListing, listing_id)
                if listing is None:
                    errors.append({"listing_id": listing_id, "error": "Listing not found."})
                    continue
                product = repo.db.get(Product, listing.product_id)
                sku = (product.sku if product else "").strip()
                try:
                    offer_id = _resolve_offer_id(client, token, listing, sku, offers_cache)
                    if not offer_id:
                        raise RuntimeError("Offer ID not found.")
                    current_offer = client.get_offer(access_token=token, offer_id=offer_id)
                    revise_payload = {}
                    for key in [
                        "sku",
                        "marketplaceId",
                        "format",
                        "availableQuantity",
                        "categoryId",
                        "merchantLocationKey",
                        "listingDescription",
                        "listingDuration",
                        "listingPolicies",
                        "pricingSummary",
                    ]:
                        if key in current_offer:
                            revise_payload[key] = current_offer[key]

                    qty_value = int(override_qty) if int(override_qty) > 0 else int(listing.quantity_listed)
                    revise_payload["availableQuantity"] = qty_value
                    pricing = revise_payload.get("pricingSummary") or {}
                    revise_payload["pricingSummary"] = pricing
                    currency = (
                        ((pricing.get("price") or {}).get("currency"))
                        or ((pricing.get("auctionStartPrice") or {}).get("currency"))
                        or default_currency
                    )
                    target_price = float(override_price) if float(override_price) > 0 else float(listing.listing_price)
                    if (revise_payload.get("format") or "").upper() == "FIXED_PRICE":
                        pricing["price"] = {"value": str(round(target_price, 2)), "currency": currency}
                    else:
                        pricing["auctionStartPrice"] = {"value": str(round(target_price, 2)), "currency": currency}

                    client.update_offer(
                        access_token=token,
                        offer_id=offer_id,
                        payload=revise_payload,
                        content_language=default_content_language,
                    )
                    repo.update_listing(
                        listing.id,
                        {
                            "quantity_listed": qty_value,
                            "listing_price": to_decimal(target_price),
                            "listing_status": "active",
                        },
                        actor=user.username,
                    )
                    success += 1
                except Exception as exc:
                    errors.append({"listing_id": listing.id, "title": listing.listing_title, "error": str(exc)})

            st.success(f"Revise queue complete. success={success}, failed={len(errors)}")
            if errors:
                st.dataframe(pd.DataFrame(errors), use_container_width=True)
            if success > 0:
                st.session_state["ebay_ops_revise_queue_ids"] = []
                st.rerun()

    with tab_api_listings:
        st.markdown("### Live Listings From eBay API")
        st.caption(
            "Pulls current eBay offer/listing status using Inventory API `getOffers` by local SKU mapping."
            " Uses the shared filter bar above."
        )
        include_unlinked = st.checkbox(
            "Include local listings without external listing ID",
            value=False,
            key="ebay_ops_api_include_unlinked",
        )
        st.caption(f"Shared-filter listing candidates: {len(filtered)}")
        if st.button("Refresh From eBay API", key="ebay_ops_refresh_api"):
            rows = []
            errors = []
            cache: dict[str, list[dict]] = {}
            for listing, sku in filtered:
                if not include_unlinked and not (listing.external_listing_id or "").strip():
                    continue
                if not sku:
                    continue
                try:
                    offers = cache.get(sku)
                    if offers is None:
                        payload = client.get_offers(access_token=token, sku=sku)
                        offers = payload.get("offers") or []
                        cache[sku] = offers
                    if not offers:
                        rows.append(
                            {
                                "local_listing_id": listing.id,
                                "sku": sku,
                                "local_status": listing.listing_status,
                                "local_external_listing_id": listing.external_listing_id,
                                "offer_id": "",
                                "ebay_listing_id": "",
                                "offer_status": "NO_OFFER_FOUND",
                                "format": "",
                                "available_quantity": "",
                                "price": "",
                            }
                        )
                        continue

                    local_ext = (listing.external_listing_id or "").strip()
                    matched = offers
                    if local_ext:
                        exact = [o for o in offers if str(o.get("listingId") or "").strip() == local_ext]
                        if exact:
                            matched = exact
                    for offer in matched:
                        price_obj = (offer.get("pricingSummary") or {}).get("price") or {}
                        auction_obj = (offer.get("pricingSummary") or {}).get("auctionStartPrice") or {}
                        price_val = price_obj.get("value") or auction_obj.get("value") or ""
                        currency = price_obj.get("currency") or auction_obj.get("currency") or ""
                        rows.append(
                            {
                                "local_listing_id": listing.id,
                                "sku": sku,
                                "local_status": listing.listing_status,
                                "local_external_listing_id": listing.external_listing_id,
                                "offer_id": offer.get("offerId"),
                                "ebay_listing_id": offer.get("listingId"),
                                "offer_status": offer.get("status"),
                                "format": offer.get("format"),
                                "available_quantity": offer.get("availableQuantity"),
                                "price": f"{price_val} {currency}".strip(),
                            }
                        )
                except Exception as exc:
                    errors.append({"local_listing_id": listing.id, "sku": sku, "error": str(exc)})

            st.session_state["ebay_ops_api_rows"] = rows
            st.session_state["ebay_ops_api_errors"] = errors

        api_rows = st.session_state.get("ebay_ops_api_rows", [])
        api_errors = st.session_state.get("ebay_ops_api_errors", [])
        if api_rows:
            st.dataframe(pd.DataFrame(api_rows), use_container_width=True)
            api_side_options = {
                f"#{int(row.get('local_listing_id') or 0)} | {row.get('sku') or ''} | "
                f"{row.get('offer_id') or row.get('ebay_listing_id') or 'no-offer'}": row
                for row in api_rows
                if int(row.get("local_listing_id") or 0) > 0
            }
            if api_side_options:
                st.markdown("### API Listing Side Panel")
                selected_api_side = st.selectbox(
                    "Select API row for detail/timeline",
                    options=["None"] + list(api_side_options.keys()),
                    key="ebay_ops_api_side_panel_label",
                )
                if selected_api_side != "None":
                    selected_row = api_side_options[selected_api_side]
                    listing_id = int(selected_row.get("local_listing_id") or 0)
                    panel_listing = repo.db.get(MarketplaceListing, listing_id)
                    if panel_listing is not None:
                        _render_listing_side_panel(
                            repo,
                            listing=panel_listing,
                            sku=str(selected_row.get("sku") or "").strip(),
                            panel_key_prefix="ebay_ops_api_panel",
                        )
                    else:
                        st.info("Selected local listing no longer exists.")
        else:
            st.info("No API rows loaded yet. Click `Refresh From eBay API`.")
        if api_errors:
            st.warning(f"API fetch errors: {len(api_errors)}")
            st.dataframe(pd.DataFrame(api_errors), use_container_width=True)

    with tab_policies:
        st.markdown("### Merchant Locations and Policies")
        st.caption(
            "Manage usable eBay merchant locations and payment/fulfillment/return policy IDs for ops flows."
        )
        marketplace_id = st.text_input(
            "Marketplace ID",
            value=st.session_state.get("ebay_ops_bulk_marketplace_id", default_marketplace_id),
            key="ebay_ops_policy_marketplace_id",
        )
        if st.button(
            "Refresh Policies & Locations",
            key="ebay_ops_refresh_policies",
            disabled=sandbox_seller_ops_blocked,
        ):
            refresh_errors: list[str] = []
            target_marketplace = marketplace_id.strip() or default_marketplace_id

            try:
                st.session_state["ebay_ops_locations_rows"] = client.list_inventory_locations(access_token=token)
            except Exception as exc:
                st.session_state["ebay_ops_locations_rows"] = []
                refresh_errors.append(f"Locations: {exc}")

            try:
                st.session_state["ebay_ops_payment_policy_rows"] = client.list_payment_policies(
                    access_token=token,
                    marketplace_id=target_marketplace,
                )
            except Exception as exc:
                st.session_state["ebay_ops_payment_policy_rows"] = []
                refresh_errors.append(f"Payment policies: {exc}")

            try:
                st.session_state["ebay_ops_fulfillment_policy_rows"] = client.list_fulfillment_policies(
                    access_token=token,
                    marketplace_id=target_marketplace,
                )
            except Exception as exc:
                st.session_state["ebay_ops_fulfillment_policy_rows"] = []
                refresh_errors.append(f"Fulfillment policies: {exc}")

            try:
                st.session_state["ebay_ops_return_policy_rows"] = client.list_return_policies(
                    access_token=token,
                    marketplace_id=target_marketplace,
                )
            except Exception as exc:
                st.session_state["ebay_ops_return_policy_rows"] = []
                refresh_errors.append(f"Return policies: {exc}")

            if refresh_errors:
                st.warning("Policies/locations refresh completed with errors.")
                for msg in refresh_errors:
                    st.error(msg)
            else:
                st.success("Policies and locations refreshed.")

        locations_rows = st.session_state.get("ebay_ops_locations_rows", [])
        payment_rows = st.session_state.get("ebay_ops_payment_policy_rows", [])
        fulfillment_rows = st.session_state.get("ebay_ops_fulfillment_policy_rows", [])
        return_rows = st.session_state.get("ebay_ops_return_policy_rows", [])

        lcol, pcol = st.columns(2)
        with lcol:
            st.markdown("#### Merchant Locations")
            st.dataframe(pd.DataFrame(locations_rows), use_container_width=True)
        with pcol:
            st.markdown("#### Payment Policies")
            st.dataframe(pd.DataFrame(payment_rows), use_container_width=True)

        fcol, rcol = st.columns(2)
        with fcol:
            st.markdown("#### Fulfillment Policies")
            st.dataframe(pd.DataFrame(fulfillment_rows), use_container_width=True)
        with rcol:
            st.markdown("#### Return Policies")
            st.dataframe(pd.DataFrame(return_rows), use_container_width=True)

        location_options = {"None": ""}
        for row in locations_rows:
            location_options[_location_label(row)] = str(row.get("merchantLocationKey") or "").strip()
        payment_options = {"None": ""}
        for row in payment_rows:
            payment_options[_policy_label(row, "paymentPolicyId")] = str(row.get("paymentPolicyId") or "").strip()
        fulfillment_options = {"None": ""}
        for row in fulfillment_rows:
            fulfillment_options[_policy_label(row, "fulfillmentPolicyId")] = str(
                row.get("fulfillmentPolicyId") or ""
            ).strip()
        return_options = {"None": ""}
        for row in return_rows:
            return_options[_policy_label(row, "returnPolicyId")] = str(row.get("returnPolicyId") or "").strip()

        st.markdown("#### Apply To eBay Ops Bulk Defaults")
        a1, a2 = st.columns(2)
        with a1:
            selected_location_label = st.selectbox(
                "Merchant Location",
                options=list(location_options.keys()),
                key="ebay_ops_select_location_label",
            )
            selected_payment_label = st.selectbox(
                "Payment Policy",
                options=list(payment_options.keys()),
                key="ebay_ops_select_payment_label",
            )
        with a2:
            selected_fulfillment_label = st.selectbox(
                "Fulfillment Policy",
                options=list(fulfillment_options.keys()),
                key="ebay_ops_select_fulfillment_label",
            )
            selected_return_label = st.selectbox(
                "Return Policy",
                options=list(return_options.keys()),
                key="ebay_ops_select_return_label",
            )

        if st.button(
            "Apply Selected Policies/Location To Local Ops Defaults",
            key="ebay_ops_apply_policies",
            disabled=sandbox_seller_ops_blocked,
        ):
            st.session_state["ebay_ops_bulk_marketplace_id"] = marketplace_id.strip() or default_marketplace_id
            chosen_location = location_options.get(selected_location_label, "")
            chosen_payment = payment_options.get(selected_payment_label, "")
            chosen_fulfillment = fulfillment_options.get(selected_fulfillment_label, "")
            chosen_return = return_options.get(selected_return_label, "")
            if chosen_location:
                st.session_state["ebay_ops_bulk_merchant_location_key"] = chosen_location
            if chosen_payment:
                st.session_state["ebay_ops_bulk_payment_policy_id"] = chosen_payment
            if chosen_fulfillment:
                st.session_state["ebay_ops_bulk_fulfillment_policy_id"] = chosen_fulfillment
            if chosen_return:
                st.session_state["ebay_ops_bulk_return_policy_id"] = chosen_return
            st.success("Applied selected policy/location values to Local Ops bulk helper defaults.")

    st.divider()
    render_ebay_push_history(
        repo,
        section_title="eBay Push History",
        key_prefix="ebay_ops_push_history",
        actor=user.username,
        user=user,
    )
