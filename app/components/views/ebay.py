import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import secrets
import requests
import hashlib
from urllib.parse import parse_qs, urlparse

from app.auth import current_user, ensure_permission
from app.components.views.ebay_context import render_active_ebay_context_banner
from app.components.views.shared import render_help_panel
from app.config import settings
from app.repository import InventoryRepository
from app.services.ebay import EbayClient
from app.services.ebay_health import summarize_ebay_connection_status
from app.services.runtime_settings import get_runtime_str
from app.services.sync_jobs import execute_sync_job, is_sync_job_enabled
from app.utils.time import utcnow_naive


_PENDING_INPUT_UPDATES_KEY = "ebay_pending_input_updates"


def _queue_input_update(key: str, value: object) -> None:
    pending = st.session_state.get(_PENDING_INPUT_UPDATES_KEY)
    if not isinstance(pending, dict):
        pending = {}
    pending[str(key)] = value
    st.session_state[_PENDING_INPUT_UPDATES_KEY] = pending


def _apply_pending_input_updates() -> None:
    pending = st.session_state.pop(_PENDING_INPUT_UPDATES_KEY, None)
    if not isinstance(pending, dict):
        return
    for key, value in pending.items():
        st.session_state[str(key)] = value


def _read_query_param(name: str) -> str:
    params = getattr(st, "query_params", None)
    if params is None:
        return ""
    try:
        value = params.get(name, "")
    except Exception:
        return ""
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def _clear_oauth_query_params() -> None:
    params = getattr(st, "query_params", None)
    if params is not None:
        for key in ("code", "state", "error", "error_description"):
            try:
                if key in params:
                    del params[key]
            except Exception:
                pass


def _exchange_and_store_user_tokens(
    *,
    client: EbayClient,
    repo: InventoryRepository,
    actor: str,
    oauth_code: str,
) -> dict:
    token_payload = client.exchange_code_for_tokens(oauth_code.strip())
    exchanged_access = str(token_payload.get("access_token") or "").strip()
    exchanged_refresh = str(token_payload.get("refresh_token") or "").strip()
    expires_in = int(token_payload.get("expires_in") or 0)
    if exchanged_access:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_user_access_token",
                value=exchanged_access,
                value_type="str",
                description="Default eBay user access token used in forms.",
                actor=actor,
            )
            if exchanged_refresh:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ebay_user_refresh_token",
                    value=exchanged_refresh,
                    value_type="str",
                    description="Default eBay user refresh token used for access token renewal.",
                    actor=actor,
                )
            if expires_in > 0:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ebay_user_access_token_expires_at",
                    value=(
                        utcnow_naive().replace(microsecond=0) + timedelta(seconds=max(0, expires_in - 120))
                    ).isoformat(timespec="seconds"),
                    value_type="str",
                    description="Best-effort expiry timestamp for current eBay user access token.",
                    actor=actor,
                )
        except Exception:
            pass
        st.session_state["ebay_workspace_access_token"] = exchanged_access
        st.session_state["ebay_ops_access_token"] = exchanged_access
        st.session_state["ebay_verify_access_token"] = exchanged_access
        st.session_state["ebay_pull_access_token"] = exchanged_access
    return token_payload


def render_ebay_connection_status_card(repo: InventoryRepository) -> None:
    status = summarize_ebay_connection_status(repo)
    latest_verify = status.get("latest_verify_success") or {}
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Token Present", "yes" if status.get("token_present") else "no")
    c2.metric("Refresh Token", "yes" if status.get("refresh_token_present") else "no")
    c3.metric(
        "Token Expiry",
        (
            f"{int(status.get('token_expires_in_minutes'))} min"
            if status.get("token_expires_in_minutes") is not None
            else "unknown"
        ),
    )
    c4.metric("Health Status", str(status.get("latest_health_status") or "(none)").lower())
    c5.metric("Health Stale", "yes" if status.get("health_stale") else "no")

    resolved_user = str(latest_verify.get("resolved_user") or "").strip()
    verify_actor = str(latest_verify.get("actor") or "").strip()
    verify_at = latest_verify.get("at")
    verify_at_text = verify_at.isoformat(timespec="seconds") if verify_at else "(never)"
    health_at = status.get("latest_health_completed_at")
    health_at_text = health_at.isoformat(timespec="seconds") if health_at else "(never)"
    st.caption(
        "Last known good: "
        f"user={resolved_user or '(unknown)'} | "
        f"verified_at={verify_at_text} | verify_actor={verify_actor or '(unknown)'} | "
        f"health_run=#{int(status.get('latest_health_run_id') or 0)} | health_at={health_at_text}"
    )
    latest_error = status.get("latest_verify_error") or {}
    err_msg = str(latest_error.get("message") or "").strip()
    if err_msg:
        st.caption(f"Last verify error: {err_msg[:220]}")
    notes = str(status.get("latest_health_notes") or "").strip()
    if notes:
        st.caption(f"Latest health notes: {notes[:280]}")


def render_ebay(client: EbayClient, repo: InventoryRepository) -> None:
    _apply_pending_input_updates()
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
                payload.get("status_filter") or ["draft", "active", "ended", "sold"]
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
    st.markdown("### eBay Connection Health")
    render_ebay_connection_status_card(repo)

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
    sold_rows = [l for l in ebay_listings if (l.listing_status or "").strip().lower() == "sold"]
    failed_syncs = [r for r in sync_runs if (r.status or "").strip().lower() in {"failed", "partial"}]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Drafts Pending Publish", len(pending_publish))
    m2.metric("Active eBay Listings", len(active_linked))
    m3.metric("Ended/Sold eBay Listings", len(ended_rows) + len(sold_rows))
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
    accepted_url = get_runtime_str(
        repo,
        "ebay_auth_accepted_url",
        settings.ebay_auth_accepted_url_effective,
    ).strip()
    declined_url = get_runtime_str(
        repo,
        "ebay_auth_declined_url",
        settings.ebay_auth_declined_url_effective,
    ).strip()
    st.caption("eBay Developer Portal callback URLs (accept/decline):")
    st.code(
        f"Accepted: {accepted_url or '(not set)'}\nDeclined: {declined_url or '(not set)'}",
        language="text",
    )
    if (settings.ebay_environment or "").strip().lower() != "production":
        st.info(
            "Sandbox mode is intended for OAuth/API smoke tests. Seller-policy and publish flows may be limited "
            "unless the sandbox seller account is fully onboarded."
        )
    oauth_state_existing = str(st.session_state.get("ebay_oauth_state") or "").strip()
    had_saved_oauth_state = bool(oauth_state_existing)
    oauth_state = oauth_state_existing
    if not had_saved_oauth_state:
        oauth_state = secrets.token_urlsafe(16)
        st.session_state["ebay_oauth_state"] = oauth_state
    auth_url = client.authorize_url(state=oauth_state)
    exchange_client_prefix = ((settings.ebay_client_id or "").strip()[:8] + "...") if settings.ebay_client_id else "(missing)"
    try:
        parsed_auth_qs = parse_qs(urlparse(auth_url).query)
        auth_client_id = str((parsed_auth_qs.get("client_id") or [""])[0] or "").strip()
    except Exception:
        auth_client_id = ""
    authorize_client_prefix = (auth_client_id[:8] + "...") if auth_client_id else "(missing)"
    st.caption(
        "OAuth client diagnostics: "
        f"authorize_client_id={authorize_client_prefix} | "
        f"exchange_client_id={exchange_client_prefix} | "
        f"ru_name={settings.ebay_ru_name or '(missing)'}"
    )
    if authorize_client_prefix != exchange_client_prefix:
        st.warning(
            "Client ID mismatch detected between authorize URL and token exchange config. "
            "Update runtime/env eBay credentials so both paths use the same keyset."
        )
    st.markdown(
        (
            f"<a href='{auth_url}' target='_self' "
            "style='display:inline-block;padding:0.55rem 0.9rem;border-radius:0.5rem;"
            "border:1px solid rgba(250,204,21,0.7);text-decoration:none;font-weight:600;'>"
            "1) Authorize eBay Account"
            "</a>"
        ),
        unsafe_allow_html=True,
    )
    st.caption("After eBay redirects back, code exchange runs automatically in this app.")

    st.markdown("**2) Exchange OAuth code for token**")
    oauth_code_from_query = _read_query_param("code")
    oauth_state_from_query = _read_query_param("state")
    oauth_error = _read_query_param("error")
    oauth_error_desc = _read_query_param("error_description")
    oauth_expires_in = _read_query_param("expires_in")
    if oauth_code_from_query:
        st.session_state["ebay_oauth_last_code"] = oauth_code_from_query
        st.session_state["ebay_oauth_last_code_seen_at"] = utcnow_naive().isoformat(timespec="seconds")
        st.session_state["ebay_oauth_last_code_sha"] = hashlib.sha256(
            oauth_code_from_query.encode("utf-8")
        ).hexdigest()[:12]
        st.session_state["ebay_oauth_last_code_pending_exchange"] = True
        st.session_state["ebay_oauth_auto_attempted_code"] = ""
        if oauth_expires_in:
            st.session_state["ebay_oauth_last_code_expires_in"] = oauth_expires_in
    if oauth_code_from_query:
        st.caption(
            "Callback code detected from query. "
            f"fingerprint=`{st.session_state.get('ebay_oauth_last_code_sha')}` "
            f"seen_at=`{st.session_state.get('ebay_oauth_last_code_seen_at')}`"
        )
    elif st.session_state.get("ebay_oauth_last_code"):
        st.warning(
            "No OAuth `code` is currently in URL query params. "
            "Input is intentionally blank to avoid reusing stale one-time codes."
        )
        st.caption(
            "Last captured code fingerprint="
            f"`{st.session_state.get('ebay_oauth_last_code_sha') or '(unknown)'}` "
            f"seen_at=`{st.session_state.get('ebay_oauth_last_code_seen_at') or '(unknown)'}`"
        )
    if oauth_state_from_query:
        st.session_state["ebay_oauth_last_state"] = oauth_state_from_query
    cached_code = str(st.session_state.get("ebay_oauth_last_code") or "").strip()
    cached_pending = bool(st.session_state.get("ebay_oauth_last_code_pending_exchange"))
    cached_seen_raw = str(st.session_state.get("ebay_oauth_last_code_seen_at") or "").strip()
    cached_is_fresh = False
    if cached_seen_raw:
        try:
            cached_seen_at = datetime.fromisoformat(cached_seen_raw)
            cached_is_fresh = (utcnow_naive() - cached_seen_at).total_seconds() <= 600
        except Exception:
            cached_is_fresh = False
    oauth_code_candidate = oauth_code_from_query or (cached_code if (cached_pending and cached_is_fresh) else "")
    oauth_code_prefill = oauth_code_from_query
    if "ebay_oauth_code_input" not in st.session_state:
        st.session_state["ebay_oauth_code_input"] = oauth_code_prefill
    if oauth_code_from_query:
        st.session_state["ebay_oauth_code_input"] = oauth_code_from_query
    oauth_code = st.text_input("Paste eBay auth code", key="ebay_oauth_code_input")
    c_clear, c_fill = st.columns(2)
    with c_clear:
        if st.button("Clear Cached OAuth Code", key="ebay_oauth_clear_cached_code_btn"):
            st.session_state["ebay_oauth_last_code"] = ""
            st.session_state["ebay_oauth_last_code_seen_at"] = ""
            st.session_state["ebay_oauth_last_code_sha"] = ""
            st.rerun()
    with c_fill:
        if st.button("Use Last Captured Code", key="ebay_oauth_use_cached_code_btn"):
            cached = str(st.session_state.get("ebay_oauth_last_code") or "").strip()
            if cached:
                _queue_input_update("ebay_oauth_code_input", cached)
                st.rerun()

    if oauth_error:
        st.error(f"eBay OAuth returned error: {oauth_error} {oauth_error_desc}".strip())
    state_mismatch = bool(oauth_state_from_query and had_saved_oauth_state and oauth_state_from_query != oauth_state)
    if state_mismatch:
        st.error("OAuth state mismatch. Start authorization again from this page.")
    elif oauth_state_from_query and not had_saved_oauth_state:
        st.info("OAuth callback state could not be validated from prior session; proceeding with callback exchange.")
    elif oauth_code_candidate and not oauth_code_from_query:
        st.info("Using freshly captured callback code from session cache for exchange fallback.")

    auto_exchanged_code = str(st.session_state.get("ebay_oauth_auto_exchanged_code") or "").strip()
    auto_attempted_code = str(st.session_state.get("ebay_oauth_auto_attempted_code") or "").strip()
    if (
        oauth_code_candidate
        and not oauth_error
        and not state_mismatch
        and oauth_code_candidate != auto_exchanged_code
        and oauth_code_candidate != auto_attempted_code
    ):
        try:
            st.session_state["ebay_oauth_auto_attempted_code"] = oauth_code_candidate
            token_payload = _exchange_and_store_user_tokens(
                client=client,
                repo=repo,
                actor=user.username,
                oauth_code=oauth_code_candidate,
            )
            st.session_state["ebay_oauth_auto_exchanged_code"] = oauth_code_candidate
            st.session_state["ebay_oauth_last_code_pending_exchange"] = False
            st.success("OAuth callback received and token exchange completed.")
            st.json(token_payload)
            _clear_oauth_query_params()
            st.rerun()
        except Exception as exc:
            st.error(f"Automatic token exchange failed: {exc}")

    if st.button("Exchange Code"):
        try:
            token_payload = _exchange_and_store_user_tokens(
                client=client,
                repo=repo,
                actor=user.username,
                oauth_code=oauth_code,
            )
            st.success("Token exchange successful.")
            st.json(token_payload)
            st.session_state["ebay_oauth_last_code"] = ""
            st.session_state["ebay_oauth_last_code_pending_exchange"] = False
            st.session_state["ebay_oauth_auto_attempted_code"] = ""
            _queue_input_update("ebay_oauth_code_input", "")
            _clear_oauth_query_params()
            st.rerun()
        except Exception as exc:
            st.error(f"Token exchange failed: {exc}")
            if isinstance(exc, requests.HTTPError):
                response = getattr(exc, "response", None)
                if response is not None:
                    st.caption(
                        "Exchange diagnostics: "
                        f"status={response.status_code} | environment={settings.ebay_environment} | "
                        f"ru_name={settings.ebay_ru_name or '(missing)'} | "
                        f"client_id={((settings.ebay_client_id or '')[:8] + '...') if settings.ebay_client_id else '(missing)'}"
                    )
                    raw = (response.text or "").strip()
                    if raw:
                        st.code(raw[:3000], language="json")
                        if "invalid_grant" in raw.lower():
                            st.session_state["ebay_oauth_last_code"] = ""
                            st.session_state["ebay_oauth_last_code_pending_exchange"] = False
                            st.session_state["ebay_oauth_auto_attempted_code"] = ""
                            st.warning(
                                "Received `invalid_grant`. Cleared cached auth code; "
                                "use Authorize eBay Account again to obtain a fresh one-time code."
                            )
            st.info(
                "Common causes: expired/reused auth code (codes are one-time, ~5 min), "
                "wrong environment credentials (sandbox vs production), or RU Name mismatch."
            )

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
