import json
from datetime import date, datetime, timedelta

import streamlit as st

from app.auth import current_user
from app.components.views.ebay import render_ebay
from app.components.views.ebay_context import render_active_ebay_context_banner
from app.components.views.ebay_ops import render_ebay_ops
from app.components.views.shared import handoff_to_documents_draft, render_help_panel
from app.components.views.workspace_shell import (
    normalize_status_semantic,
    render_status_semantic_legend,
    render_workspace_feedback,
    render_workspace_task_completion,
)
from app.config import settings
from app.repository import InventoryRepository
from app.services.ebay import EbayClient
from app.services.listing_readiness import evaluate_ebay_readiness
from app.services.runtime_settings import get_runtime_str
from app.utils.time import utc_today


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


def _render_oauth_callback_banner() -> None:
    oauth_code = _read_query_param("code")
    oauth_state = _read_query_param("state")
    oauth_error = _read_query_param("error")
    oauth_error_desc = _read_query_param("error_description")
    oauth_expires = _read_query_param("expires_in")

    if oauth_error:
        details = f"{oauth_error}: {oauth_error_desc}".strip(": ").strip()
        st.error(f"OAuth callback received from eBay with an error. {details}")
        return
    if not oauth_code and not oauth_state:
        return

    state_preview = f"{oauth_state[:8]}..." if oauth_state else "(missing)"
    expiry_note = f" Code expires in ~{oauth_expires}s." if oauth_expires else ""
    st.success(
        "OAuth callback received from eBay. "
        f"State: `{state_preview}`.{expiry_note} "
        "Continue in the Integration tab to complete token exchange."
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
    hints: list[str] = []
    if format_type == "FIXED_PRICE":
        if _num(row.listing_price, 0.0) <= 0:
            hints.append("Fixed Missing BIN")
    else:
        if auction_start_price <= 0:
            hints.append("Auction Missing Start")
        if auction_duration not in {"DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"}:
            hints.append("Auction Missing Duration")
        if auction_reserve_price > 0 and auction_reserve_price < auction_start_price:
            hints.append("Reserve < Start")
        if auction_buy_now_price > 0 and auction_buy_now_price < auction_start_price:
            hints.append("BIN < Start")
    return "; ".join(hints)


def _listing_publish_meta(row) -> dict:
    raw = str(row.marketplace_details or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    meta = parsed.get("ebay_publish")
    if isinstance(meta, dict):
        return meta
    return parsed


def _to_float(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(fallback)


def _ebay_format_fix_backlog_count(repo: InventoryRepository, *, default_format_type: str, default_auction_duration: str) -> int:
    count = 0
    for row in repo.list_listings():
        if (row.marketplace or "").strip().lower() != "ebay":
            continue
        if str(_listing_format_hint(row, default_format_type=default_format_type, default_auction_duration=default_auction_duration)).strip():
            count += 1
    return count


def _ebay_readiness_blocker_breakdown(
    repo: InventoryRepository,
    *,
    default_format_type: str,
    default_auction_duration: str,
    category_id: str,
    merchant_location_key: str,
    payment_policy_id: str,
    fulfillment_policy_id: str,
    return_policy_id: str,
) -> dict:
    blocked_count = 0
    blocker_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    for listing in repo.list_listings():
        if (listing.marketplace or "").strip().lower() != "ebay":
            continue
        publish_meta = _listing_publish_meta(listing)
        format_type = str(
            publish_meta.get("format")
            or publish_meta.get("format_type")
            or default_format_type
            or "FIXED_PRICE"
        ).strip().upper()
        if format_type not in {"FIXED_PRICE", "AUCTION"}:
            format_type = "FIXED_PRICE"
        listing_duration = str(
            publish_meta.get("listing_duration")
            or ("GTC" if format_type == "FIXED_PRICE" else default_auction_duration)
        ).strip().upper()
        auction_start_price = _to_float(publish_meta.get("auction_start_price"), _to_float(listing.listing_price, 0.0))
        auction_reserve_price = _to_float(publish_meta.get("auction_reserve_price"), 0.0)
        auction_buy_now_price = _to_float(publish_meta.get("auction_buy_now_price"), 0.0)
        best_offer_enabled = bool(publish_meta.get("best_offer_enabled"))
        readiness = evaluate_ebay_readiness(
            listing_title=listing.listing_title,
            listing_price=float(listing.listing_price or 0),
            auction_start_price=float(auction_start_price or 0),
            auction_reserve_price=float(auction_reserve_price or 0),
            auction_buy_now_price=float(auction_buy_now_price or 0),
            quantity_listed=int(listing.quantity_listed or 0),
            listing_status=listing.listing_status,
            format_type=format_type,
            listing_duration=listing_duration,
            media_count=len(listing.media_assets),
            category_id=(category_id or "").strip(),
            merchant_location_key=(merchant_location_key or "").strip(),
            payment_policy_id=(payment_policy_id or "").strip(),
            fulfillment_policy_id=(fulfillment_policy_id or "").strip(),
            return_policy_id=(return_policy_id or "").strip(),
        )
        blockers = list(readiness.blockers)
        warnings = list(readiness.warnings)
        if format_type == "AUCTION" and best_offer_enabled:
            warnings.append("Best Offer is ignored for auction format")
        review_status = str(getattr(listing, "review_status", "pending") or "pending").strip().lower()
        if review_status != "approved":
            blockers.append("Listing review must be approved before publish.")
        if blockers:
            blocked_count += 1
        for blocker in blockers:
            key = str(blocker or "").strip()
            if not key:
                continue
            blocker_counts[key] = int(blocker_counts.get(key, 0) + 1)
        for warning in warnings:
            key = str(warning or "").strip()
            if not key:
                continue
            warning_counts[key] = int(warning_counts.get(key, 0) + 1)
    top_blockers = sorted(blocker_counts.items(), key=lambda x: (-int(x[1]), str(x[0])))[:10]
    top_warnings = sorted(warning_counts.items(), key=lambda x: (-int(x[1]), str(x[0])))[:10]
    return {
        "blocked_count": int(blocked_count),
        "unique_blockers": int(len(blocker_counts)),
        "top_blockers": top_blockers,
        "top_warnings": top_warnings,
    }


def render_ebay_workspace(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("eBay Workspace")
    st.caption("Unified eBay workspace for integration setup, listing operations, and daily execution.")
    st.page_link("pages/24_eBay_User_Details.py", label="Open eBay User Details", icon=":material/badge:")
    _render_oauth_callback_banner()
    render_help_panel(
        section_title="eBay Workspace",
        goal="Run eBay integration checks and daily listing operations from one consolidated workspace.",
        steps=[
            "Use Integration tab for OAuth, account checks, and order pull/import.",
            "Use Operations tab for bulk end/relist/revise and policy/location management.",
            "Use one workspace to reduce context switching during daily eBay operations.",
        ],
        roadmap_phase="v0.6 GS-V06-001 eBay Workspace Unification",
    )

    client = EbayClient()
    default_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    default_merchant_location_key = get_runtime_str(
        repo,
        "ebay_merchant_location_key",
        settings.ebay_merchant_location_key,
    ).strip()
    default_payment_policy_id = get_runtime_str(
        repo,
        "ebay_payment_policy_id",
        settings.ebay_payment_policy_id,
    ).strip()
    default_fulfillment_policy_id = get_runtime_str(
        repo,
        "ebay_fulfillment_policy_id",
        settings.ebay_fulfillment_policy_id,
    ).strip()
    default_return_policy_id = get_runtime_str(
        repo,
        "ebay_return_policy_id",
        settings.ebay_return_policy_id,
    ).strip()
    default_marketplace_id = get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id).strip()
    default_currency = get_runtime_str(repo, "ebay_currency", settings.ebay_currency).strip()
    default_content_language = get_runtime_str(
        repo,
        "ebay_content_language",
        settings.ebay_content_language,
    ).strip()
    default_category_id = get_runtime_str(repo, "ebay_category_id", "").strip()
    default_listing_format = get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE").strip().upper()
    if default_listing_format not in {"FIXED_PRICE", "AUCTION"}:
        default_listing_format = "FIXED_PRICE"
    default_best_offer_enabled = (
        get_runtime_str(repo, "ebay_best_offer_default", "false").strip().lower() in {"1", "true", "yes", "on"}
    )
    default_auction_duration = get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7").strip().upper()
    default_auction_start = float(get_runtime_str(repo, "ebay_auction_start_default", "1.0") or 1.0)
    default_auction_reserve = float(get_runtime_str(repo, "ebay_auction_reserve_default", "0.0") or 0.0)
    default_auction_buy_now = float(get_runtime_str(repo, "ebay_auction_buy_now_default", "0.0") or 0.0)
    auction_duration_options = ["DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"]
    if default_auction_duration not in set(auction_duration_options):
        default_auction_duration = "DAYS_7"
    default_listed_date_range = (utc_today() - timedelta(days=30), utc_today())
    if "ebay_workspace_access_token" not in st.session_state:
        st.session_state["ebay_workspace_access_token"] = default_token
    if "ebay_workspace_status_filter" not in st.session_state:
        st.session_state["ebay_workspace_status_filter"] = ["draft", "active", "ended"]
    if "ebay_workspace_linked_only" not in st.session_state:
        st.session_state["ebay_workspace_linked_only"] = False
    if "ebay_workspace_search" not in st.session_state:
        st.session_state["ebay_workspace_search"] = ""
    if "ebay_workspace_access_token_input" not in st.session_state:
        st.session_state["ebay_workspace_access_token_input"] = st.session_state["ebay_workspace_access_token"]
    if "ebay_workspace_status_filter_input" not in st.session_state:
        st.session_state["ebay_workspace_status_filter_input"] = list(
            st.session_state["ebay_workspace_status_filter"]
        )
    if "ebay_workspace_linked_only_input" not in st.session_state:
        st.session_state["ebay_workspace_linked_only_input"] = bool(st.session_state["ebay_workspace_linked_only"])
    if "ebay_workspace_search_input" not in st.session_state:
        st.session_state["ebay_workspace_search_input"] = st.session_state["ebay_workspace_search"]
    if "ebay_workspace_use_date_filter" not in st.session_state:
        st.session_state["ebay_workspace_use_date_filter"] = False
    if "ebay_workspace_listed_date_range" not in st.session_state:
        st.session_state["ebay_workspace_listed_date_range"] = default_listed_date_range
    if "ebay_workspace_use_date_filter_input" not in st.session_state:
        st.session_state["ebay_workspace_use_date_filter_input"] = bool(
            st.session_state["ebay_workspace_use_date_filter"]
        )
    if "ebay_workspace_listed_date_range_input" not in st.session_state:
        st.session_state["ebay_workspace_listed_date_range_input"] = st.session_state[
            "ebay_workspace_listed_date_range"
        ]
    if "ebay_workspace_account_alias" not in st.session_state:
        st.session_state["ebay_workspace_account_alias"] = "default"
    if "ebay_workspace_profile_store_token" not in st.session_state:
        st.session_state["ebay_workspace_profile_store_token"] = False
    if "ebay_workspace_saved_profiles" not in st.session_state:
        st.session_state["ebay_workspace_saved_profiles"] = {}
    if "ebay_workspace_active_profile" not in st.session_state:
        st.session_state["ebay_workspace_active_profile"] = "manual"
    if "ebay_workspace_store_alias" not in st.session_state:
        st.session_state["ebay_workspace_store_alias"] = "default-store"
    if "ebay_workspace_store_saved_profiles" not in st.session_state:
        st.session_state["ebay_workspace_store_saved_profiles"] = {}
    if "ebay_workspace_active_store_profile" not in st.session_state:
        st.session_state["ebay_workspace_active_store_profile"] = "manual-store"
    if "ebay_workspace_store_merchant_location_key_input" not in st.session_state:
        st.session_state["ebay_workspace_store_merchant_location_key_input"] = default_merchant_location_key
    if "ebay_workspace_store_payment_policy_id_input" not in st.session_state:
        st.session_state["ebay_workspace_store_payment_policy_id_input"] = default_payment_policy_id
    if "ebay_workspace_store_fulfillment_policy_id_input" not in st.session_state:
        st.session_state["ebay_workspace_store_fulfillment_policy_id_input"] = default_fulfillment_policy_id
    if "ebay_workspace_store_return_policy_id_input" not in st.session_state:
        st.session_state["ebay_workspace_store_return_policy_id_input"] = default_return_policy_id
    if "ebay_workspace_store_category_id_input" not in st.session_state:
        st.session_state["ebay_workspace_store_category_id_input"] = default_category_id
    if "ebay_workspace_store_marketplace_id_input" not in st.session_state:
        st.session_state["ebay_workspace_store_marketplace_id_input"] = default_marketplace_id
    if "ebay_workspace_store_currency_input" not in st.session_state:
        st.session_state["ebay_workspace_store_currency_input"] = default_currency
    if "ebay_workspace_store_content_language_input" not in st.session_state:
        st.session_state["ebay_workspace_store_content_language_input"] = default_content_language
    if "ebay_workspace_store_listing_format_input" not in st.session_state:
        st.session_state["ebay_workspace_store_listing_format_input"] = default_listing_format
    if "ebay_workspace_store_best_offer_enabled_input" not in st.session_state:
        st.session_state["ebay_workspace_store_best_offer_enabled_input"] = bool(default_best_offer_enabled)
    if "ebay_workspace_store_auction_duration_input" not in st.session_state:
        st.session_state["ebay_workspace_store_auction_duration_input"] = default_auction_duration
    if "ebay_workspace_store_auction_start_input" not in st.session_state:
        st.session_state["ebay_workspace_store_auction_start_input"] = max(0.01, float(default_auction_start))
    if "ebay_workspace_store_auction_reserve_input" not in st.session_state:
        st.session_state["ebay_workspace_store_auction_reserve_input"] = max(0.0, float(default_auction_reserve))
    if "ebay_workspace_store_auction_buy_now_input" not in st.session_state:
        st.session_state["ebay_workspace_store_auction_buy_now_input"] = max(0.0, float(default_auction_buy_now))

    # Optional persisted profile store for multi-account-like context switching.
    # Tokens are not stored unless explicitly requested in profile save.
    raw_profiles = get_runtime_str(repo, "ebay_workspace_saved_profiles_json", "").strip()
    if raw_profiles and not st.session_state.get("ebay_workspace_profiles_loaded_once"):
        try:
            parsed = json.loads(raw_profiles)
            if isinstance(parsed, dict):
                st.session_state["ebay_workspace_saved_profiles"] = parsed
        except Exception:
            pass
    st.session_state["ebay_workspace_profiles_loaded_once"] = True

    raw_store_profiles = get_runtime_str(repo, "ebay_workspace_store_profiles_json", "").strip()
    if raw_store_profiles and not st.session_state.get("ebay_workspace_store_profiles_loaded_once"):
        try:
            parsed_store = json.loads(raw_store_profiles)
            if isinstance(parsed_store, dict):
                st.session_state["ebay_workspace_store_saved_profiles"] = parsed_store
        except Exception:
            pass
    st.session_state["ebay_workspace_store_profiles_loaded_once"] = True

    with st.expander("Workspace Controls", expanded=True):
        render_status_semantic_legend(title="Workspace Queue Semantics")
        st.markdown("#### Account Context")
        alias_col_1, alias_col_2 = st.columns([2, 1])
        with alias_col_1:
            st.text_input(
                "Account Alias",
                key="ebay_workspace_account_alias",
                help="Logical account/profile name for quickly switching workspace defaults (e.g., main, sandbox, us-store).",
            )
        with alias_col_2:
            st.checkbox(
                "Store token in profile",
                key="ebay_workspace_profile_store_token",
                help="Off by default. Enable only if you want account profiles to persist access tokens in Runtime Settings.",
            )
        wc1, wc2 = st.columns([2, 1])
        with wc1:
            st.text_area(
                "Shared User Access Token",
                height=90,
                help="Applied to eBay Integration + eBay Ops token fields in this workspace.",
                key="ebay_workspace_access_token_input",
            )
        with wc2:
            st.multiselect(
                "Default Ops Status Filter",
                options=["draft", "active", "ended"],
                key="ebay_workspace_status_filter_input",
            )
            st.checkbox(
                "Ops Linked-Only Default",
                key="ebay_workspace_linked_only_input",
            )
        st.text_input(
            "Default Ops Search",
            key="ebay_workspace_search_input",
        )
        df1, df2 = st.columns([1, 2])
        with df1:
            st.checkbox(
                "Default Date Window Enabled",
                key="ebay_workspace_use_date_filter_input",
                help="When enabled, eBay Ops Local tab filters listings by listed date range.",
            )
        with df2:
            st.date_input(
                "Default Listed Date Range",
                key="ebay_workspace_listed_date_range_input",
                help="Applied to eBay Ops Local tab when date window is enabled.",
            )
        selected_defaults = st.session_state.get("ebay_workspace_status_filter_input") or []
        if selected_defaults:
            semantic_preview = [
                {
                    "status": str(value),
                    "semantic": normalize_status_semantic(str(value)),
                }
                for value in selected_defaults
            ]
            st.caption("Default status filter semantics")
            st.dataframe(semantic_preview, use_container_width=True, hide_index=True)
        profile_map = st.session_state.get("ebay_workspace_saved_profiles") or {}
        profile_labels = ["None"] + sorted(profile_map.keys())
        st.selectbox(
            "Saved Account Profile",
            options=profile_labels,
            key="ebay_workspace_profile_select",
        )

        p1, p2, p3 = st.columns(3)
        with p1:
            save_profile = st.button("Save Profile", key="ebay_workspace_profile_save_btn")
        with p2:
            load_profile = st.button("Load Profile", key="ebay_workspace_profile_load_btn")
        with p3:
            delete_profile = st.button("Delete Profile", key="ebay_workspace_profile_delete_btn")

        if save_profile:
            alias = str(st.session_state.get("ebay_workspace_account_alias") or "").strip().lower()
            if not alias:
                st.error("Account alias is required to save profile.")
            else:
                payload = {
                    "status_filter": list(st.session_state.get("ebay_workspace_status_filter_input") or []),
                    "linked_only": bool(st.session_state.get("ebay_workspace_linked_only_input")),
                    "search": str(st.session_state.get("ebay_workspace_search_input") or "").strip(),
                    "use_date_filter": bool(st.session_state.get("ebay_workspace_use_date_filter_input")),
                    "listed_date_range": st.session_state.get("ebay_workspace_listed_date_range_input"),
                }
                if bool(st.session_state.get("ebay_workspace_profile_store_token")):
                    payload["access_token"] = str(st.session_state.get("ebay_workspace_access_token_input") or "").strip()
                profile_map[alias] = payload
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_workspace_saved_profiles_json",
                        value=json.dumps(profile_map, default=str),
                        value_type="str",
                        description="Persisted eBay Workspace account-context profiles for quick switching.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["ebay_workspace_saved_profiles"] = profile_map
                    st.success(f"Saved profile `{alias}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save profile: {exc}")

        if load_profile:
            selected_profile = str(st.session_state.get("ebay_workspace_profile_select") or "None")
            if selected_profile == "None" or selected_profile not in profile_map:
                st.error("Select a saved profile first.")
            else:
                payload = profile_map.get(selected_profile) or {}
                if str(payload.get("access_token") or "").strip():
                    st.session_state["ebay_workspace_access_token_input"] = str(payload.get("access_token") or "").strip()
                st.session_state["ebay_workspace_status_filter_input"] = list(
                    payload.get("status_filter") or ["draft", "active", "ended"]
                )
                st.session_state["ebay_workspace_linked_only_input"] = bool(payload.get("linked_only"))
                st.session_state["ebay_workspace_search_input"] = str(payload.get("search") or "").strip()
                st.session_state["ebay_workspace_use_date_filter_input"] = bool(payload.get("use_date_filter"))
                stored_range = payload.get("listed_date_range")
                if isinstance(stored_range, list) and len(stored_range) == 2:
                    start_raw, end_raw = stored_range[0], stored_range[1]
                    try:
                        start_date = datetime.fromisoformat(str(start_raw)).date()
                        end_date = datetime.fromisoformat(str(end_raw)).date()
                        st.session_state["ebay_workspace_listed_date_range_input"] = (start_date, end_date)
                    except Exception:
                        if isinstance(start_raw, date) and isinstance(end_raw, date):
                            st.session_state["ebay_workspace_listed_date_range_input"] = (start_raw, end_raw)
                st.session_state["ebay_workspace_active_profile"] = selected_profile
                st.success(f"Loaded profile `{selected_profile}` into workspace controls.")
                st.rerun()

        if delete_profile:
            selected_profile = str(st.session_state.get("ebay_workspace_profile_select") or "None")
            if selected_profile == "None" or selected_profile not in profile_map:
                st.error("Select a saved profile first.")
            else:
                profile_map.pop(selected_profile, None)
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_workspace_saved_profiles_json",
                        value=json.dumps(profile_map, default=str),
                        value_type="str",
                        description="Persisted eBay Workspace account-context profiles for quick switching.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["ebay_workspace_saved_profiles"] = profile_map
                    st.success(f"Deleted profile `{selected_profile}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete profile: {exc}")

        st.markdown("#### Store + Listing Format Context")
        st.caption(
            "Manage eBay store/policy/listing-format defaults (Auction vs Buy It Now) and apply them across workspace flows."
        )
        s0, s1, s2 = st.columns([2, 2, 2])
        with s0:
            st.text_input(
                "Store Profile Alias",
                key="ebay_workspace_store_alias",
                help="Logical store profile name (e.g., us-main, coins-store, bullion-store).",
            )
        store_profile_map = st.session_state.get("ebay_workspace_store_saved_profiles") or {}
        store_profile_labels = ["None"] + sorted(store_profile_map.keys())
        with s1:
            st.selectbox(
                "Saved Store Profile",
                options=store_profile_labels,
                key="ebay_workspace_store_profile_select",
            )
        default_store_profile_name = get_runtime_str(
            repo,
            "ebay_workspace_default_store_profile",
            "",
        ).strip()
        with s2:
            st.caption(f"Default Store Profile: `{default_store_profile_name or '(none)'}`")

        sp1, sp2, sp3 = st.columns(3)
        with sp1:
            save_store_profile = st.button("Save Store Profile", key="ebay_workspace_store_profile_save_btn")
        with sp2:
            load_store_profile = st.button("Load Store Profile", key="ebay_workspace_store_profile_load_btn")
        with sp3:
            delete_store_profile = st.button("Delete Store Profile", key="ebay_workspace_store_profile_delete_btn")
        sd1, sd2 = st.columns(2)
        with sd1:
            set_default_store_profile = st.button(
                "Set Default Store Profile",
                key="ebay_workspace_store_profile_set_default_btn",
                disabled=(
                    str(st.session_state.get("ebay_workspace_store_profile_select") or "None") == "None"
                ),
            )
        with sd2:
            clear_default_store_profile = st.button(
                "Clear Default Store Profile",
                key="ebay_workspace_store_profile_clear_default_btn",
            )

        f1, f2, f3 = st.columns(3)
        with f1:
            st.text_input(
                "Merchant Location Key",
                key="ebay_workspace_store_merchant_location_key_input",
            )
        with f2:
            st.text_input(
                "Payment Policy ID",
                key="ebay_workspace_store_payment_policy_id_input",
            )
        with f3:
            st.text_input(
                "Fulfillment Policy ID",
                key="ebay_workspace_store_fulfillment_policy_id_input",
            )
        f4, f5, f6 = st.columns(3)
        with f4:
            st.text_input(
                "Return Policy ID",
                key="ebay_workspace_store_return_policy_id_input",
            )
        with f5:
            st.text_input(
                "Category ID (optional)",
                key="ebay_workspace_store_category_id_input",
            )
        with f6:
            st.selectbox(
                "Listing Format Default",
                options=["FIXED_PRICE", "AUCTION"],
                key="ebay_workspace_store_listing_format_input",
            )
        st.checkbox(
            "Best Offer Default (fixed-price only)",
            key="ebay_workspace_store_best_offer_enabled_input",
        )
        f7, f8, f9 = st.columns(3)
        with f7:
            st.selectbox(
                "Auction Duration Default",
                options=auction_duration_options,
                key="ebay_workspace_store_auction_duration_input",
            )
        with f8:
            st.number_input(
                "Auction Start Default",
                min_value=0.01,
                step=1.0,
                key="ebay_workspace_store_auction_start_input",
            )
        with f9:
            st.number_input(
                "Auction Reserve Default",
                min_value=0.0,
                step=1.0,
                key="ebay_workspace_store_auction_reserve_input",
            )
        st.number_input(
            "Auction Buy It Now Default",
            min_value=0.0,
            step=1.0,
            key="ebay_workspace_store_auction_buy_now_input",
        )
        f8, f9 = st.columns(2)
        with f8:
            st.text_input(
                "Marketplace ID",
                key="ebay_workspace_store_marketplace_id_input",
            )
        with f9:
            st.text_input(
                "Currency",
                key="ebay_workspace_store_currency_input",
            )
        st.text_input(
            "Content Language",
            key="ebay_workspace_store_content_language_input",
        )
        persist_runtime_defaults = st.checkbox(
            "Persist as runtime defaults when applying context",
            value=False,
            key="ebay_workspace_store_persist_runtime_defaults",
            help="Writes selected store/policy/format defaults into Runtime Settings keys.",
        )
        st.markdown("#### Active Format Defaults Summary")
        summary_format = str(st.session_state.get("ebay_workspace_store_listing_format_input") or "FIXED_PRICE").strip().upper()
        summary_policy_fields = {
            "merchant_location_key": str(st.session_state.get("ebay_workspace_store_merchant_location_key_input") or "").strip(),
            "payment_policy_id": str(st.session_state.get("ebay_workspace_store_payment_policy_id_input") or "").strip(),
            "fulfillment_policy_id": str(st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input") or "").strip(),
            "return_policy_id": str(st.session_state.get("ebay_workspace_store_return_policy_id_input") or "").strip(),
        }
        missing_required = [k for k, v in summary_policy_fields.items() if not v]
        sx1, sx2, sx3 = st.columns(3)
        sx1.metric("Format", summary_format)
        sx2.metric("Required Policy Fields Missing", int(len(missing_required)))
        sx3.metric(
            "Best Offer Default",
            "on" if bool(st.session_state.get("ebay_workspace_store_best_offer_enabled_input")) else "off",
        )
        summary_rows = [
            {"field": "marketplace_id", "value": str(st.session_state.get("ebay_workspace_store_marketplace_id_input") or "").strip()},
            {"field": "currency", "value": str(st.session_state.get("ebay_workspace_store_currency_input") or "").strip()},
            {"field": "content_language", "value": str(st.session_state.get("ebay_workspace_store_content_language_input") or "").strip()},
            {"field": "merchant_location_key", "value": summary_policy_fields["merchant_location_key"]},
            {"field": "payment_policy_id", "value": summary_policy_fields["payment_policy_id"]},
            {"field": "fulfillment_policy_id", "value": summary_policy_fields["fulfillment_policy_id"]},
            {"field": "return_policy_id", "value": summary_policy_fields["return_policy_id"]},
            {"field": "category_id", "value": str(st.session_state.get("ebay_workspace_store_category_id_input") or "").strip()},
        ]
        if summary_format == "FIXED_PRICE":
            summary_rows.append(
                {
                    "field": "fixed_best_offer_enabled",
                    "value": "true" if bool(st.session_state.get("ebay_workspace_store_best_offer_enabled_input")) else "false",
                }
            )
        else:
            summary_rows.extend(
                [
                    {
                        "field": "auction_duration",
                        "value": str(st.session_state.get("ebay_workspace_store_auction_duration_input") or "").strip(),
                    },
                    {
                        "field": "auction_start_default",
                        "value": f"{float(st.session_state.get('ebay_workspace_store_auction_start_input') or 0.0):.2f}",
                    },
                    {
                        "field": "auction_reserve_default",
                        "value": f"{float(st.session_state.get('ebay_workspace_store_auction_reserve_input') or 0.0):.2f}",
                    },
                    {
                        "field": "auction_buy_now_default",
                        "value": f"{float(st.session_state.get('ebay_workspace_store_auction_buy_now_input') or 0.0):.2f}",
                    },
                ]
            )
        st.dataframe(summary_rows, use_container_width=True, hide_index=True)
        if missing_required:
            st.warning("Missing required policy inputs: " + ", ".join(missing_required))

        if save_store_profile:
            store_alias = str(st.session_state.get("ebay_workspace_store_alias") or "").strip().lower()
            if not store_alias:
                st.error("Store profile alias is required.")
            else:
                store_payload = {
                    "merchant_location_key": str(
                        st.session_state.get("ebay_workspace_store_merchant_location_key_input") or ""
                    ).strip(),
                    "payment_policy_id": str(
                        st.session_state.get("ebay_workspace_store_payment_policy_id_input") or ""
                    ).strip(),
                    "fulfillment_policy_id": str(
                        st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input") or ""
                    ).strip(),
                    "return_policy_id": str(
                        st.session_state.get("ebay_workspace_store_return_policy_id_input") or ""
                    ).strip(),
                    "category_id": str(st.session_state.get("ebay_workspace_store_category_id_input") or "").strip(),
                    "listing_format": str(
                        st.session_state.get("ebay_workspace_store_listing_format_input") or "FIXED_PRICE"
                    ).strip(),
                    "best_offer_enabled": bool(st.session_state.get("ebay_workspace_store_best_offer_enabled_input")),
                    "auction_duration": str(
                        st.session_state.get("ebay_workspace_store_auction_duration_input") or "DAYS_7"
                    ).strip(),
                    "auction_start_default": float(st.session_state.get("ebay_workspace_store_auction_start_input") or 1.0),
                    "auction_reserve_default": float(
                        st.session_state.get("ebay_workspace_store_auction_reserve_input") or 0.0
                    ),
                    "auction_buy_now_default": float(
                        st.session_state.get("ebay_workspace_store_auction_buy_now_input") or 0.0
                    ),
                    "marketplace_id": str(
                        st.session_state.get("ebay_workspace_store_marketplace_id_input") or ""
                    ).strip(),
                    "currency": str(st.session_state.get("ebay_workspace_store_currency_input") or "").strip(),
                    "content_language": str(
                        st.session_state.get("ebay_workspace_store_content_language_input") or ""
                    ).strip(),
                }
                store_profile_map[store_alias] = store_payload
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_workspace_store_profiles_json",
                        value=json.dumps(store_profile_map, default=str),
                        value_type="str",
                        description="Persisted eBay store/policy/listing-format profiles for workspace operations.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["ebay_workspace_store_saved_profiles"] = store_profile_map
                    st.success(f"Saved store profile `{store_alias}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save store profile: {exc}")

        if load_store_profile:
            selected_store_profile = str(st.session_state.get("ebay_workspace_store_profile_select") or "None")
            if selected_store_profile == "None" or selected_store_profile not in store_profile_map:
                st.error("Select a saved store profile first.")
            else:
                store_payload = store_profile_map.get(selected_store_profile) or {}
                st.session_state["ebay_workspace_store_merchant_location_key_input"] = str(
                    store_payload.get("merchant_location_key") or ""
                ).strip()
                st.session_state["ebay_workspace_store_payment_policy_id_input"] = str(
                    store_payload.get("payment_policy_id") or ""
                ).strip()
                st.session_state["ebay_workspace_store_fulfillment_policy_id_input"] = str(
                    store_payload.get("fulfillment_policy_id") or ""
                ).strip()
                st.session_state["ebay_workspace_store_return_policy_id_input"] = str(
                    store_payload.get("return_policy_id") or ""
                ).strip()
                st.session_state["ebay_workspace_store_category_id_input"] = str(
                    store_payload.get("category_id") or ""
                ).strip()
                st.session_state["ebay_workspace_store_listing_format_input"] = str(
                    store_payload.get("listing_format") or "FIXED_PRICE"
                ).strip()
                st.session_state["ebay_workspace_store_best_offer_enabled_input"] = bool(
                    store_payload.get("best_offer_enabled")
                )
                st.session_state["ebay_workspace_store_auction_duration_input"] = str(
                    store_payload.get("auction_duration") or "DAYS_7"
                ).strip()
                st.session_state["ebay_workspace_store_auction_start_input"] = float(
                    store_payload.get("auction_start_default") or 1.0
                )
                st.session_state["ebay_workspace_store_auction_reserve_input"] = float(
                    store_payload.get("auction_reserve_default") or 0.0
                )
                st.session_state["ebay_workspace_store_auction_buy_now_input"] = float(
                    store_payload.get("auction_buy_now_default") or 0.0
                )
                st.session_state["ebay_workspace_store_marketplace_id_input"] = str(
                    store_payload.get("marketplace_id") or default_marketplace_id
                ).strip()
                st.session_state["ebay_workspace_store_currency_input"] = str(
                    store_payload.get("currency") or default_currency
                ).strip()
                st.session_state["ebay_workspace_store_content_language_input"] = str(
                    store_payload.get("content_language") or default_content_language
                ).strip()
                st.session_state["ebay_workspace_active_store_profile"] = selected_store_profile
                st.success(f"Loaded store profile `{selected_store_profile}`.")
                st.rerun()

        if delete_store_profile:
            selected_store_profile = str(st.session_state.get("ebay_workspace_store_profile_select") or "None")
            if selected_store_profile == "None" or selected_store_profile not in store_profile_map:
                st.error("Select a saved store profile first.")
            else:
                store_profile_map.pop(selected_store_profile, None)
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_workspace_store_profiles_json",
                        value=json.dumps(store_profile_map, default=str),
                        value_type="str",
                        description="Persisted eBay store/policy/listing-format profiles for workspace operations.",
                        is_active=True,
                        actor=user.username,
                    )
                    if selected_store_profile == default_store_profile_name:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key="ebay_workspace_default_store_profile",
                            value="",
                            value_type="str",
                            description="Default eBay workspace store profile for preloading store/policy/listing-format context.",
                            is_active=True,
                            actor=user.username,
                        )
                    st.session_state["ebay_workspace_store_saved_profiles"] = store_profile_map
                    st.success(f"Deleted store profile `{selected_store_profile}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete store profile: {exc}")

        if set_default_store_profile:
            selected_store_profile = str(st.session_state.get("ebay_workspace_store_profile_select") or "None")
            if selected_store_profile == "None" or selected_store_profile not in store_profile_map:
                st.error("Select a saved store profile first.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_workspace_default_store_profile",
                        value=selected_store_profile,
                        value_type="str",
                        description="Default eBay workspace store profile for preloading store/policy/listing-format context.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success(f"Set default store profile to `{selected_store_profile}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set default store profile: {exc}")

        if clear_default_store_profile:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ebay_workspace_default_store_profile",
                    value="",
                    value_type="str",
                    description="Default eBay workspace store profile for preloading store/policy/listing-format context.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Cleared default store profile.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to clear default store profile: {exc}")

        a1, a2, a3 = st.columns(3)
        with a1:
            apply_workspace = st.button("Apply Workspace Context", key="ebay_workspace_apply_context")
        with a2:
            clear_workspace = st.button("Reset Workspace Context", key="ebay_workspace_reset_context")
        with a3:
            if st.button("Open Listings Page", key="ebay_workspace_open_listings"):
                if hasattr(st, "switch_page"):
                    st.switch_page("pages/03_Listings.py")
        if apply_workspace:
            st.session_state["ebay_workspace_access_token"] = str(
                st.session_state.get("ebay_workspace_access_token_input") or ""
            ).strip()
            st.session_state["ebay_workspace_status_filter"] = list(
                st.session_state.get("ebay_workspace_status_filter_input") or ["draft", "active", "ended"]
            )
            st.session_state["ebay_workspace_linked_only"] = bool(
                st.session_state.get("ebay_workspace_linked_only_input")
            )
            st.session_state["ebay_workspace_search"] = str(
                st.session_state.get("ebay_workspace_search_input") or ""
            ).strip()
            st.session_state["ebay_workspace_use_date_filter"] = bool(
                st.session_state.get("ebay_workspace_use_date_filter_input")
            )
            st.session_state["ebay_workspace_listed_date_range"] = st.session_state.get(
                "ebay_workspace_listed_date_range_input"
            )
            st.session_state["ebay_ops_access_token"] = st.session_state["ebay_workspace_access_token"]
            st.session_state["ebay_ops_status_filter"] = st.session_state["ebay_workspace_status_filter"]
            st.session_state["ebay_ops_linked_only"] = st.session_state["ebay_workspace_linked_only"]
            st.session_state["ebay_ops_search_query"] = st.session_state["ebay_workspace_search"]
            st.session_state["ebay_ops_use_date_filter"] = st.session_state["ebay_workspace_use_date_filter"]
            st.session_state["ebay_ops_listed_date_range"] = st.session_state["ebay_workspace_listed_date_range"]
            st.session_state["ebay_pull_access_token"] = st.session_state["ebay_workspace_access_token"]
            st.session_state["ebay_verify_access_token"] = st.session_state["ebay_workspace_access_token"]
            st.session_state["ebay_workspace_active_profile"] = (
                str(st.session_state.get("ebay_workspace_account_alias") or "").strip().lower() or "manual"
            )
            st.session_state["ebay_workspace_active_store_profile"] = (
                str(st.session_state.get("ebay_workspace_store_alias") or "").strip().lower() or "manual-store"
            )
            st.session_state["ebay_pub_merchant_location_key"] = str(
                st.session_state.get("ebay_workspace_store_merchant_location_key_input") or ""
            ).strip()
            st.session_state["ebay_pub_payment_policy_id"] = str(
                st.session_state.get("ebay_workspace_store_payment_policy_id_input") or ""
            ).strip()
            st.session_state["ebay_pub_fulfillment_policy_id"] = str(
                st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input") or ""
            ).strip()
            st.session_state["ebay_pub_return_policy_id"] = str(
                st.session_state.get("ebay_workspace_store_return_policy_id_input") or ""
            ).strip()
            st.session_state["ebay_pub_category_id"] = str(
                st.session_state.get("ebay_workspace_store_category_id_input") or ""
            ).strip()
            st.session_state["ebay_pub_marketplace_id"] = str(
                st.session_state.get("ebay_workspace_store_marketplace_id_input") or default_marketplace_id
            ).strip()
            st.session_state["ebay_pub_currency"] = str(
                st.session_state.get("ebay_workspace_store_currency_input") or default_currency
            ).strip()
            st.session_state["ebay_pub_content_language"] = str(
                st.session_state.get("ebay_workspace_store_content_language_input") or default_content_language
            ).strip()
            st.session_state["ebay_pub_format"] = str(
                st.session_state.get("ebay_workspace_store_listing_format_input") or "FIXED_PRICE"
            ).strip()
            st.session_state["ebay_pub_best_offer_enabled"] = bool(
                st.session_state.get("ebay_workspace_store_best_offer_enabled_input")
            )
            st.session_state["ebay_pub_auction_duration"] = str(
                st.session_state.get("ebay_workspace_store_auction_duration_input") or "DAYS_7"
            ).strip()
            st.session_state["ebay_pub_auction_start"] = float(
                st.session_state.get("ebay_workspace_store_auction_start_input") or 1.0
            )
            st.session_state["ebay_pub_auction_reserve"] = float(
                st.session_state.get("ebay_workspace_store_auction_reserve_input") or 0.0
            )
            st.session_state["ebay_pub_auction_buy_now"] = float(
                st.session_state.get("ebay_workspace_store_auction_buy_now_input") or 0.0
            )
            if bool(st.session_state.get("ebay_workspace_store_persist_runtime_defaults")):
                try:
                    runtime_updates = [
                        ("ebay_merchant_location_key", st.session_state.get("ebay_pub_merchant_location_key"), "str"),
                        ("ebay_payment_policy_id", st.session_state.get("ebay_pub_payment_policy_id"), "str"),
                        ("ebay_fulfillment_policy_id", st.session_state.get("ebay_pub_fulfillment_policy_id"), "str"),
                        ("ebay_return_policy_id", st.session_state.get("ebay_pub_return_policy_id"), "str"),
                        ("ebay_category_id", st.session_state.get("ebay_pub_category_id"), "str"),
                        ("ebay_marketplace_id", st.session_state.get("ebay_pub_marketplace_id"), "str"),
                        ("ebay_currency", st.session_state.get("ebay_pub_currency"), "str"),
                        ("ebay_content_language", st.session_state.get("ebay_pub_content_language"), "str"),
                        ("ebay_listing_format_default", st.session_state.get("ebay_pub_format"), "str"),
                        (
                            "ebay_best_offer_default",
                            "true" if bool(st.session_state.get("ebay_pub_best_offer_enabled")) else "false",
                            "bool",
                        ),
                        ("ebay_auction_duration_default", st.session_state.get("ebay_pub_auction_duration"), "str"),
                        ("ebay_auction_start_default", st.session_state.get("ebay_pub_auction_start"), "float"),
                        ("ebay_auction_reserve_default", st.session_state.get("ebay_pub_auction_reserve"), "float"),
                        ("ebay_auction_buy_now_default", st.session_state.get("ebay_pub_auction_buy_now"), "float"),
                    ]
                    for key, value, value_type in runtime_updates:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=str(key),
                            value=str(value or "").strip(),
                            value_type=value_type,
                            description=f"eBay workspace-applied default for `{key}`.",
                            is_active=True,
                            actor=user.username,
                        )
                except Exception as exc:
                    st.warning(f"Workspace context applied, but runtime defaults persist failed: {exc}")
            st.success("Applied shared workspace context to Integration + Operations tabs.")
            st.rerun()
        if clear_workspace:
            st.session_state["ebay_workspace_access_token"] = default_token
            st.session_state["ebay_workspace_status_filter"] = ["draft", "active", "ended"]
            st.session_state["ebay_workspace_linked_only"] = False
            st.session_state["ebay_workspace_search"] = ""
            st.session_state["ebay_workspace_use_date_filter"] = False
            st.session_state["ebay_workspace_listed_date_range"] = default_listed_date_range
            st.session_state["ebay_workspace_access_token_input"] = default_token
            st.session_state["ebay_workspace_status_filter_input"] = ["draft", "active", "ended"]
            st.session_state["ebay_workspace_linked_only_input"] = False
            st.session_state["ebay_workspace_search_input"] = ""
            st.session_state["ebay_workspace_use_date_filter_input"] = False
            st.session_state["ebay_workspace_listed_date_range_input"] = default_listed_date_range
            st.session_state["ebay_ops_access_token"] = default_token
            st.session_state["ebay_ops_status_filter"] = ["draft", "active", "ended"]
            st.session_state["ebay_ops_linked_only"] = False
            st.session_state["ebay_ops_search_query"] = ""
            st.session_state["ebay_ops_use_date_filter"] = False
            st.session_state["ebay_ops_listed_date_range"] = default_listed_date_range
            st.session_state["ebay_pull_access_token"] = default_token
            st.session_state["ebay_verify_access_token"] = default_token
            st.session_state["ebay_workspace_active_profile"] = "manual"
            st.session_state["ebay_workspace_active_store_profile"] = "manual-store"
            st.info("Workspace context reset to defaults.")
            st.rerun()

    # Ensure tab-local widgets stay in sync even before the first explicit apply.
    st.session_state.setdefault("ebay_ops_access_token", str(st.session_state.get("ebay_workspace_access_token") or default_token))
    st.session_state.setdefault("ebay_pull_access_token", str(st.session_state.get("ebay_workspace_access_token") or default_token))
    st.session_state.setdefault("ebay_verify_access_token", str(st.session_state.get("ebay_workspace_access_token") or default_token))
    st.session_state.setdefault("ebay_ops_status_filter", list(st.session_state.get("ebay_workspace_status_filter") or ["draft", "active", "ended"]))
    st.session_state.setdefault("ebay_ops_linked_only", bool(st.session_state.get("ebay_workspace_linked_only")))
    st.session_state.setdefault("ebay_ops_search_query", str(st.session_state.get("ebay_workspace_search") or ""))
    st.session_state.setdefault("ebay_ops_use_date_filter", bool(st.session_state.get("ebay_workspace_use_date_filter")))
    st.session_state.setdefault("ebay_ops_listed_date_range", st.session_state.get("ebay_workspace_listed_date_range"))

    if not bool(st.session_state.get("ebay_workspace_store_default_loaded_once")):
        default_store_profile_name = get_runtime_str(
            repo,
            "ebay_workspace_default_store_profile",
            "",
        ).strip()
        store_profile_map = st.session_state.get("ebay_workspace_store_saved_profiles") or {}
        if default_store_profile_name and default_store_profile_name in store_profile_map:
            payload = store_profile_map.get(default_store_profile_name) or {}
            st.session_state["ebay_workspace_store_merchant_location_key_input"] = str(
                payload.get("merchant_location_key") or default_merchant_location_key
            ).strip()
            st.session_state["ebay_workspace_store_payment_policy_id_input"] = str(
                payload.get("payment_policy_id") or default_payment_policy_id
            ).strip()
            st.session_state["ebay_workspace_store_fulfillment_policy_id_input"] = str(
                payload.get("fulfillment_policy_id") or default_fulfillment_policy_id
            ).strip()
            st.session_state["ebay_workspace_store_return_policy_id_input"] = str(
                payload.get("return_policy_id") or default_return_policy_id
            ).strip()
            st.session_state["ebay_workspace_store_category_id_input"] = str(
                payload.get("category_id") or default_category_id
            ).strip()
            st.session_state["ebay_workspace_store_listing_format_input"] = str(
                payload.get("listing_format") or default_listing_format
            ).strip()
            st.session_state["ebay_workspace_store_best_offer_enabled_input"] = bool(
                payload.get("best_offer_enabled")
            )
            st.session_state["ebay_workspace_store_auction_duration_input"] = str(
                payload.get("auction_duration") or default_auction_duration
            ).strip()
            st.session_state["ebay_workspace_store_auction_start_input"] = float(
                payload.get("auction_start_default") or default_auction_start
            )
            st.session_state["ebay_workspace_store_auction_reserve_input"] = float(
                payload.get("auction_reserve_default") or default_auction_reserve
            )
            st.session_state["ebay_workspace_store_auction_buy_now_input"] = float(
                payload.get("auction_buy_now_default") or default_auction_buy_now
            )
            st.session_state["ebay_workspace_store_marketplace_id_input"] = str(
                payload.get("marketplace_id") or default_marketplace_id
            ).strip()
            st.session_state["ebay_workspace_store_currency_input"] = str(
                payload.get("currency") or default_currency
            ).strip()
            st.session_state["ebay_workspace_store_content_language_input"] = str(
                payload.get("content_language") or default_content_language
            ).strip()
            st.session_state["ebay_workspace_store_alias"] = default_store_profile_name
            st.session_state["ebay_workspace_active_store_profile"] = default_store_profile_name
        st.session_state["ebay_workspace_store_default_loaded_once"] = True

    st.markdown("### Current Active Account Context")
    render_active_ebay_context_banner(section_title="eBay Workspace")
    readiness_breakdown = _ebay_readiness_blocker_breakdown(
        repo,
        default_format_type=str(st.session_state.get("ebay_workspace_store_listing_format_input") or default_listing_format),
        default_auction_duration=str(st.session_state.get("ebay_workspace_store_auction_duration_input") or default_auction_duration),
        category_id=str(st.session_state.get("ebay_workspace_store_category_id_input") or "").strip(),
        merchant_location_key=str(st.session_state.get("ebay_workspace_store_merchant_location_key_input") or "").strip(),
        payment_policy_id=str(st.session_state.get("ebay_workspace_store_payment_policy_id_input") or "").strip(),
        fulfillment_policy_id=str(st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input") or "").strip(),
        return_policy_id=str(st.session_state.get("ebay_workspace_store_return_policy_id_input") or "").strip(),
    )
    st.markdown("### Readiness Blocker Summary")
    rb1, rb2 = st.columns(2)
    with rb1:
        r1, r2 = st.columns(2)
        r1.metric("Blocked eBay Listings", int(readiness_breakdown.get("blocked_count") or 0))
        r2.metric("Unique Blocker Types", int(readiness_breakdown.get("unique_blockers") or 0))
        top_blockers = readiness_breakdown.get("top_blockers") or []
        if top_blockers:
            st.dataframe(
                [
                    {"blocker": str(reason), "count": int(count)}
                    for reason, count in top_blockers
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No readiness blockers detected.")
    with rb2:
        top_warnings = readiness_breakdown.get("top_warnings") or []
        if top_warnings:
            st.dataframe(
                [
                    {"warning": str(reason), "count": int(count)}
                    for reason, count in top_warnings
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No readiness warnings detected.")
    st.markdown("### Quick Links")
    st.caption("Jump directly to the next operational surface without using sidebar navigation.")
    ql1, ql2, ql3, ql4, ql5 = st.columns(5)
    with ql1:
        if st.button("Open Listings", key="ebay_workspace_quick_open_listings", use_container_width=True):
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = list(
                st.session_state.get("ebay_workspace_status_filter") or ["draft", "active", "ended"]
            )
            st.session_state["listings_filter_query"] = str(st.session_state.get("ebay_workspace_search") or "").strip()
            st.session_state["listings_filter_origin"] = "all"
            st.session_state["listings_filter_format_issue_only"] = False
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "listings",
                        "preset": "workspace_listings",
                        "marketplaces": ["ebay"],
                        "statuses": st.session_state.get("listings_filter_status") or [],
                        "query": st.session_state.get("listings_filter_query") or "",
                        "origin": "all",
                        "format_issue_only": False,
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                st.switch_page("pages/03_Listings.py")
    with ql2:
        if st.button("Open Sync", key="ebay_workspace_quick_open_sync", use_container_width=True):
            st.session_state["sync_provider_filter"] = "ebay"
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "sync"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "sync",
                        "provider": "ebay",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                st.switch_page("pages/18_Sync.py")
    with ql3:
        if st.button("Open Shipping", key="ebay_workspace_quick_open_shipping", use_container_width=True):
            st.session_state["shipping_focus_marketplace"] = "ebay"
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "shipping"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "shipping",
                        "marketplace_focus": "ebay",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                st.switch_page("pages/11_Shipping.py")
    with ql4:
        if st.button("Open Admin", key="ebay_workspace_quick_open_admin", use_container_width=True):
            if hasattr(st, "switch_page"):
                st.switch_page("pages/17_Admin.py")
    with ql5:
        if st.button("Open Format Fix Queue", key="ebay_workspace_quick_open_format_fix", use_container_width=True):
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = ["draft", "active"]
            st.session_state["listings_filter_query"] = ""
            st.session_state["listings_filter_origin"] = "all"
            st.session_state["listings_filter_format_issue_only"] = True
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "listings",
                        "preset": "format_fix_queue",
                        "marketplaces": ["ebay"],
                        "statuses": ["draft", "active"],
                        "query": "",
                        "origin": "all",
                        "format_issue_only": True,
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                st.switch_page("pages/03_Listings.py")
    frow1, frow2 = st.columns(2)
    with frow1:
        if st.button(
            "Open Listings (Fixed Template)",
            key="ebay_workspace_quick_open_listings_fixed",
            use_container_width=True,
        ):
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = ["draft", "active"]
            st.session_state["listings_filter_query"] = ""
            st.session_state["listings_filter_origin"] = "all"
            st.session_state["listings_filter_format_issue_only"] = False
            st.session_state["ebay_pub_format"] = "FIXED_PRICE"
            st.session_state["ebay_pub_auction_duration"] = "GTC"
            st.session_state["ebay_pub_best_offer_enabled"] = bool(
                get_runtime_bool(repo, "ebay_best_offer_default", False)
            )
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "listings",
                        "preset": "fixed_template",
                        "marketplaces": ["ebay"],
                        "statuses": ["draft", "active"],
                        "query": "",
                        "origin": "all",
                        "format_issue_only": False,
                        "format_type": "FIXED_PRICE",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                st.switch_page("pages/03_Listings.py")
    with frow2:
        if st.button(
            "Open Listings (Auction Template)",
            key="ebay_workspace_quick_open_listings_auction",
            use_container_width=True,
        ):
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = ["draft", "active"]
            st.session_state["listings_filter_query"] = ""
            st.session_state["listings_filter_origin"] = "all"
            st.session_state["listings_filter_format_issue_only"] = False
            st.session_state["ebay_pub_format"] = "AUCTION"
            st.session_state["ebay_pub_best_offer_enabled"] = False
            st.session_state["ebay_pub_auction_duration"] = str(
                st.session_state.get("ebay_workspace_store_auction_duration_input")
                or get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7")
                or "DAYS_7"
            ).strip().upper()
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "listings"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "listings",
                        "preset": "auction_template",
                        "marketplaces": ["ebay"],
                        "statuses": ["draft", "active"],
                        "query": "",
                        "origin": "all",
                        "format_issue_only": False,
                        "format_type": "AUCTION",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                st.switch_page("pages/03_Listings.py")

    st.markdown("### Document Draft Quick Handoff (eBay)")
    st.caption("Start invoice/receipt drafting directly from recent eBay orders or sales.")
    ebay_orders = [
        order
        for order in repo.list_orders()
        if str(order.marketplace or "").strip().lower() == "ebay"
    ]
    ebay_sales = [
        sale
        for sale in repo.list_sales()
        if str(sale.marketplace or "").strip().lower() == "ebay"
    ]
    handoff_rows = []
    for order in sorted(ebay_orders, key=lambda x: (x.sold_at or datetime.min, x.id), reverse=True)[:200]:
        handoff_rows.append(
            {
                "source_type": "Order",
                "source_id": int(order.id),
                "sold_at": order.sold_at,
                "external_id": str(order.external_order_id or "").strip(),
                "amount": float(order.total_amount or 0),
                "label": (
                    f"order#{int(order.id)} | {str(order.external_order_id or '').strip() or 'no-ext-id'} | "
                    f"{(order.sold_at.isoformat() if order.sold_at else '')} | total=${float(order.total_amount or 0):,.2f}"
                ),
            }
        )
    for sale in sorted(ebay_sales, key=lambda x: (x.sold_at or datetime.min, x.id), reverse=True)[:200]:
        handoff_rows.append(
            {
                "source_type": "Sale",
                "source_id": int(sale.id),
                "sold_at": sale.sold_at,
                "external_id": str(sale.external_order_id or "").strip(),
                "amount": float(sale.sold_price or 0),
                "label": (
                    f"sale#{int(sale.id)} | {str(sale.external_order_id or '').strip() or 'no-ext-id'} | "
                    f"{(sale.sold_at.isoformat() if sale.sold_at else '')} | gross=${float(sale.sold_price or 0):,.2f}"
                ),
            }
        )
    handoff_rows = sorted(
        handoff_rows,
        key=lambda row: (row.get("sold_at") or datetime.min, int(row.get("source_id") or 0)),
        reverse=True,
    )
    if not handoff_rows:
        st.info("No eBay order/sale records available for handoff.")
    else:
        eh1, eh2, eh3 = st.columns([1, 1, 2])
        with eh1:
            handoff_source_scope = st.selectbox(
                "Source",
                options=["All", "Order", "Sale"],
                index=0,
                key="ebay_workspace_docs_handoff_source",
            )
        with eh2:
            handoff_doc_type = st.selectbox(
                "Document",
                options=["invoice", "receipt"],
                index=0,
                key="ebay_workspace_docs_handoff_doc_type",
            )
        filtered_handoff_rows = handoff_rows
        if handoff_source_scope in {"Order", "Sale"}:
            filtered_handoff_rows = [
                row for row in handoff_rows if str(row.get("source_type") or "") == handoff_source_scope
            ]
        if filtered_handoff_rows:
            selected_handoff_label = st.selectbox(
                "Select eBay Record",
                options=[str(row.get("label") or "") for row in filtered_handoff_rows],
                key="ebay_workspace_docs_handoff_pick",
            )
            selected_handoff_row = next(
                (row for row in filtered_handoff_rows if str(row.get("label") or "") == selected_handoff_label),
                None,
            )
            with eh3:
                if st.button(
                    "Open in Documents",
                    key="ebay_workspace_docs_handoff_open_btn",
                ) and selected_handoff_row:
                    handoff_to_documents_draft(
                        source_type=str(selected_handoff_row.get("source_type") or "Sale"),
                        source_id=int(selected_handoff_row.get("source_id") or 0),
                        doc_type=handoff_doc_type,
                        handoff_from="ebay_workspace",
                        repo=repo,
                        actor=user.username,
                    )
        else:
            st.info(f"No eBay {handoff_source_scope.lower()} records found in the recent window.")

    st.markdown("### eBay Operations Runbook")
    st.caption("Pre-flight checklist before running bulk publish/end/relist operations.")
    with st.expander("Runbook Checklist", expanded=False):
        rc1, rc2 = st.columns(2)
        with rc1:
            rb_profile = st.checkbox(
                "Verified active profile + token match the intended account",
                key="ebay_workspace_runbook_profile_token",
            )
            rb_filters = st.checkbox(
                "Reviewed status/search/date filters against target listings",
                key="ebay_workspace_runbook_filters",
            )
            rb_policies = st.checkbox(
                "Confirmed required policy/location/category defaults are set",
                key="ebay_workspace_runbook_policies",
            )
        with rc2:
            rb_media = st.checkbox(
                "Checked listing readiness (required fields/media/price) for selected rows",
                key="ebay_workspace_runbook_readiness",
            )
            rb_sandbox = st.checkbox(
                "Environment/sandbox constraints reviewed (seller ops allowed if needed)",
                key="ebay_workspace_runbook_sandbox",
            )
            rb_sync = st.checkbox(
                "Verified no unresolved sync failures block expected state transitions",
                key="ebay_workspace_runbook_sync_health",
            )
        runbook_all_checked = all([rb_profile, rb_filters, rb_policies, rb_media, rb_sandbox, rb_sync])
        st.session_state["ebay_workspace_runbook_ready"] = bool(runbook_all_checked)
        st.metric("Runbook Ready", "yes" if runbook_all_checked else "no")
        if runbook_all_checked:
            st.success("Runbook checks complete for this session.")
        else:
            st.warning("Complete all checklist items before running high-impact bulk actions.")

    tab_integration, tab_operations = st.tabs(["Integration", "Operations"])
    format_fix_backlog_count = _ebay_format_fix_backlog_count(
        repo,
        default_format_type=default_listing_format,
        default_auction_duration=default_auction_duration,
    )

    with tab_integration:
        fx1, fx2 = st.columns([1, 2])
        fx1.metric("Format Fix Needed", int(format_fix_backlog_count))
        with fx2:
            if st.button(
                "Open Format Fix Queue",
                key="ebay_workspace_integration_open_format_fix",
                use_container_width=True,
            ):
                st.session_state["listings_filter_marketplaces"] = ["ebay"]
                st.session_state["listings_filter_status"] = ["draft", "active"]
                st.session_state["listings_filter_query"] = ""
                st.session_state["listings_filter_origin"] = "all"
                st.session_state["listings_filter_format_issue_only"] = True
                st.session_state["workspace_handoff_from"] = "ebay_workspace"
                st.session_state["workspace_handoff_target"] = "listings"
                try:
                    repo.record_audit_event(
                        entity_type="navigation",
                        entity_id=None,
                        action="workspace_handoff_applied",
                        actor=user.username,
                        changes={
                            "from": "ebay_workspace",
                            "to": "listings",
                            "preset": "format_fix_queue",
                            "source_tab": "integration",
                            "marketplaces": ["ebay"],
                            "statuses": ["draft", "active"],
                            "query": "",
                            "origin": "all",
                            "format_issue_only": True,
                        },
                    )
                except Exception:
                    pass
                if hasattr(st, "switch_page"):
                    st.switch_page("pages/03_Listings.py")
        render_ebay(client, repo)

    with tab_operations:
        ox1, ox2 = st.columns([1, 2])
        ox1.metric("Format Fix Needed", int(format_fix_backlog_count))
        with ox2:
            if st.button(
                "Open Format Fix Queue",
                key="ebay_workspace_operations_open_format_fix",
                use_container_width=True,
            ):
                st.session_state["listings_filter_marketplaces"] = ["ebay"]
                st.session_state["listings_filter_status"] = ["draft", "active"]
                st.session_state["listings_filter_query"] = ""
                st.session_state["listings_filter_origin"] = "all"
                st.session_state["listings_filter_format_issue_only"] = True
                st.session_state["workspace_handoff_from"] = "ebay_workspace"
                st.session_state["workspace_handoff_target"] = "listings"
                try:
                    repo.record_audit_event(
                        entity_type="navigation",
                        entity_id=None,
                        action="workspace_handoff_applied",
                        actor=user.username,
                        changes={
                            "from": "ebay_workspace",
                            "to": "listings",
                            "preset": "format_fix_queue",
                            "source_tab": "operations",
                            "marketplaces": ["ebay"],
                            "statuses": ["draft", "active"],
                            "query": "",
                            "origin": "all",
                            "format_issue_only": True,
                        },
                    )
                except Exception:
                    pass
                if hasattr(st, "switch_page"):
                    st.switch_page("pages/03_Listings.py")
        render_ebay_ops(repo)

    st.divider()
    render_workspace_task_completion(
        repo=repo,
        actor=user.username,
        workflow_key="ebay_workspace",
        section_title="Workflow Completion: eBay Workspace",
        tasks=[
            ("Verified account context and token", "ebay_context_verified"),
            ("Processed revise/end/relist queue", "ebay_ops_queue_processed"),
            ("Reviewed API listings and status", "ebay_api_listing_reviewed"),
        ],
    )
    st.divider()
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="ebay_workspace",
        section_title="eBay Workspace Feedback",
    )
