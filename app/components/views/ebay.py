import streamlit as st
import pandas as pd
from datetime import datetime

from app.auth import current_user, ensure_permission
from app.components.views.ebay_context import render_active_ebay_context_banner
from app.components.views.shared import render_help_panel
from app.config import settings
from app.repository import InventoryRepository
from app.services.ebay import EbayClient
from app.services.runtime_settings import get_runtime_str
from app.services.sync_jobs import execute_sync_job, is_sync_job_enabled


def render_ebay(client: EbayClient, repo: InventoryRepository) -> None:
    user = current_user()
    ebay_pull_enabled = is_sync_job_enabled("ebay_orders_pull_import", repo=repo)
    workspace_token = str(st.session_state.get("ebay_workspace_access_token") or "").strip()
    default_token = (
        workspace_token
        or get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    )
    if "ebay_verify_access_token" not in st.session_state:
        st.session_state["ebay_verify_access_token"] = default_token
    if "ebay_pull_access_token" not in st.session_state:
        st.session_state["ebay_pull_access_token"] = default_token

    # One-click profile apply (shared with eBay Workspace and eBay Ops).
    profile_map = st.session_state.get("ebay_workspace_saved_profiles") or {}
    if profile_map:
        st.markdown("### Account Profile Quick Switch")
        labels = ["None"] + sorted(profile_map.keys())
        selected_profile = st.selectbox(
            "Profile",
            options=labels,
            key="ebay_integration_profile_quick_select",
        )
        if st.button(
            "Apply Profile to Integration + Ops",
            key="ebay_integration_profile_apply_btn",
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
    st.subheader("eBay Integration (Phase 1)")
    st.caption("Start with eBay OAuth and account checks, then extend to listing/order sync.")
    render_help_panel(
        section_title="eBay Integration",
        goal="Authorize eBay access and verify API connectivity before sync workflows.",
        steps=[
            "Confirm eBay credentials and environment variables are set for the active environment.",
            "Use Authorize eBay Account, then paste the returned auth code for token exchange.",
            "Test account privileges with a valid access token to confirm API readiness.",
            "Run pull/import to upsert eBay orders into local orders/items/sales.",
        ],
        roadmap_phase="v0.3 Channel Sync + Accounting Readiness",
    )
    render_active_ebay_context_banner(section_title="eBay Integration")

    if not client.is_configured():
        st.warning(
            "eBay credentials are not configured. Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_RU_NAME in .env."
        )
        return
    st.caption(
        "Job toggle: `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED="
        f"{'true' if ebay_pull_enabled else 'false'}`"
    )

    st.markdown("### eBay Operations Dashboard")
    ebay_listings = [l for l in repo.list_listings() if (l.marketplace or "").strip().lower() == "ebay"]
    sync_runs = repo.list_sync_runs(provider="ebay", limit=200)
    pending_publish = [
        l for l in ebay_listings
        if (l.listing_status or "").strip().lower() == "draft" and not (l.external_listing_id or "").strip()
    ]
    active_linked = [
        l for l in ebay_listings
        if (l.listing_status or "").strip().lower() == "active" and (l.external_listing_id or "").strip()
    ]
    ended_rows = [l for l in ebay_listings if (l.listing_status or "").strip().lower() == "ended"]
    failed_syncs = [r for r in sync_runs if (r.status or "").strip().lower() in {"failed", "partial"}]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Drafts Pending Publish", len(pending_publish))
    m2.metric("Active eBay Listings", len(active_linked))
    m3.metric("Ended eBay Listings", len(ended_rows))
    m4.metric("Failed/Partial Sync Runs", len(failed_syncs))

    if pending_publish:
        st.caption("Top pending publish drafts:")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "listing_id": l.id,
                        "title": l.listing_title,
                        "product_id": l.product_id,
                        "price": float(l.listing_price),
                        "qty": l.quantity_listed,
                    }
                    for l in pending_publish[:20]
                ]
            ),
            use_container_width=True,
        )

    st.write("Environment:", settings.ebay_environment)
    if (settings.ebay_environment or "").strip().lower() != "production":
        st.info(
            "Sandbox mode is intended for OAuth/API smoke tests. Seller-policy and publish flows may be limited "
            "unless the sandbox seller account is fully onboarded."
        )
    st.link_button("1) Authorize eBay Account", client.authorize_url())

    st.markdown("**2) Exchange OAuth code for token**")
    oauth_code = st.text_input("Paste eBay auth code")

    if st.button("Exchange Code"):
        try:
            token_payload = client.exchange_code_for_tokens(oauth_code.strip())
            st.success("Token exchange successful.")
            st.json(token_payload)
        except Exception as exc:
            st.error(f"Token exchange failed: {exc}")

    st.markdown("**3) Validate account privilege using access token**")
    access_token = st.text_area(
        "Paste access token",
        height=130,
        help="Defaults to `EBAY_USER_ACCESS_TOKEN` if set.",
        key="ebay_verify_access_token",
    )

    if st.button("Check eBay Account Privileges"):
        try:
            data = client.get_account_privileges(access_token.strip())
            st.success("eBay API call succeeded.")
            st.json(data)
        except Exception as exc:
            st.error(f"eBay API call failed: {exc}")

    st.markdown("**4) Pull recent eBay orders and import into local records**")
    if not ebay_pull_enabled:
        st.warning("`ebay_orders_pull_import` is disabled by configuration.")
    with st.form("ebay_pull_orders_import_form"):
        c1, c2 = st.columns(2)
        with c1:
            pull_limit = st.number_input("Order fetch limit", min_value=1, max_value=200, value=25)
        with c2:
            pull_offset = st.number_input("Order offset", min_value=0, value=0)
        pull_token = st.text_area(
            "Access token for order pull",
            height=120,
            key="ebay_pull_access_token",
            help="Defaults to `EBAY_USER_ACCESS_TOKEN` if set.",
        )
        pull_submit = st.form_submit_button(
            "Pull + Import Orders",
            disabled=not ebay_pull_enabled,
            help="Enable `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED=true` to run this job.",
        )

    if pull_submit:
        if not ensure_permission(user, "create", "Run eBay Sync Pull"):
            return
        if not ebay_pull_enabled:
            st.error("`ebay_orders_pull_import` is disabled by configuration.")
            return
        try:
            token_to_use = pull_token.strip() or default_token
            result = execute_sync_job(
                repo,
                job_name="ebay_orders_pull_import",
                access_token=token_to_use,
                actor=user.username,
                limit=int(pull_limit),
                offset=int(pull_offset),
                client=client,
            )
            st.success(
                f"Import completed with status `{result['status']}`. "
                f"processed={result['processed']}, created={result['created']}, "
                f"updated={result['updated']}, failed={result['failed']}."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "run_id": result["run_id"],
                            "line_items_with_listing_link": result["line_items_with_listing_link"],
                            "line_items_unmapped_sku": result["line_items_unmapped_sku"],
                            "auto_listings_created": result["auto_listings_created"],
                        }
                    ]
                ),
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"eBay pull import failed: {exc}")
