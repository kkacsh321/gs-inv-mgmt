import json
import hashlib
from datetime import date, datetime, timedelta

import streamlit as st

from app.auth import current_user
from app.components.views.ebay import render_ebay
from app.components.views.ebay_context import render_active_ebay_context_banner
from app.components.views.ebay_ops import render_ebay_ops
from app.components.views.shared import handoff_to_documents_draft, render_help_panel, safe_switch_page
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
from app.services.runtime_settings import get_runtime_bool, get_runtime_str
from app.services.sync_jobs import build_ebay_order_financial_diagnostics
from app.utils.time import utc_today


_PENDING_WORKSPACE_INPUT_UPDATES_KEY = "ebay_workspace_pending_input_updates"
EBAY_WORKSPACE_SETUP_WORKFLOW_KEY = "ebay_workspace_setup"
EBAY_WORKSPACE_SETUP_DRAFT_SESSION_KEYS = [
    "ebay_workspace_access_token_input",
    "ebay_workspace_status_filter_input",
    "ebay_workspace_linked_only_input",
    "ebay_workspace_search_input",
    "ebay_workspace_use_date_filter_input",
    "ebay_workspace_listed_date_range_input",
    "ebay_workspace_account_alias",
    "ebay_workspace_profile_store_token",
    "ebay_workspace_profile_select",
    "ebay_workspace_store_alias",
    "ebay_workspace_store_profile_select",
    "ebay_workspace_store_merchant_location_key_input",
    "ebay_workspace_store_payment_policy_id_input",
    "ebay_workspace_store_fulfillment_policy_id_input",
    "ebay_workspace_store_return_policy_id_input",
    "ebay_workspace_store_category_id_input",
    "ebay_workspace_store_marketplace_id_input",
    "ebay_workspace_store_currency_input",
    "ebay_workspace_store_content_language_input",
    "ebay_workspace_store_listing_format_input",
    "ebay_workspace_store_best_offer_enabled_input",
    "ebay_workspace_store_auction_duration_input",
    "ebay_workspace_store_auction_start_input",
    "ebay_workspace_store_auction_reserve_input",
    "ebay_workspace_store_auction_buy_now_input",
    "ebay_workspace_store_shipping_service_input",
    "ebay_workspace_store_handling_days_input",
    "ebay_workspace_store_shipping_cost_input",
    "ebay_workspace_store_package_weight_oz_input",
    "ebay_workspace_store_persist_runtime_defaults",
    "ebay_workspace_inventory_location_select",
    "ebay_workspace_create_location_key",
    "ebay_workspace_create_location_name",
    "ebay_workspace_create_location_line1",
    "ebay_workspace_create_location_city",
    "ebay_workspace_create_location_state",
    "ebay_workspace_create_location_postal",
    "ebay_workspace_create_location_country",
    "ebay_workspace_create_location_type",
    "ebay_workspace_create_location_timezone",
    "ebay_workspace_create_location_phone",
]


def _ebay_workspace_setup_scope_key(*, username: str) -> str:
    return f"env:{str(settings.app_env or '').strip().lower()}|user:{str(username or '').strip().lower()}"


def _ebay_workspace_parse_draft_json(draft_json: str) -> dict:
    try:
        parsed = json.loads(str(draft_json or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_date_range(value) -> tuple[date, date] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start_raw, end_raw = value[0], value[1]
        try:
            if isinstance(start_raw, date):
                start_date = start_raw
            else:
                start_date = datetime.fromisoformat(str(start_raw)).date()
            if isinstance(end_raw, date):
                end_date = end_raw
            else:
                end_date = datetime.fromisoformat(str(end_raw)).date()
            return (start_date, end_date)
        except Exception:
            return None
    return None


def _ebay_workspace_apply_setup_draft_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    for key in EBAY_WORKSPACE_SETUP_DRAFT_SESSION_KEYS:
        if key not in payload:
            continue
        if key == "ebay_workspace_listed_date_range_input":
            coerced = _coerce_date_range(payload.get(key))
            if coerced is not None:
                st.session_state[key] = coerced
            continue
        st.session_state[key] = payload.get(key)


def _ebay_workspace_build_setup_draft_payload() -> dict:
    state: dict[str, object] = {}
    for key in EBAY_WORKSPACE_SETUP_DRAFT_SESSION_KEYS:
        if key in st.session_state:
            value = st.session_state.get(key)
            if key == "ebay_workspace_listed_date_range_input":
                coerced = _coerce_date_range(value)
                if coerced is not None:
                    value = [coerced[0].isoformat(), coerced[1].isoformat()]
            state[key] = value
    return state


def _ebay_workspace_setup_signature(payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _queue_workspace_input_updates(session_state, updates: dict) -> None:
    pending = session_state.get(_PENDING_WORKSPACE_INPUT_UPDATES_KEY)
    if not isinstance(pending, dict):
        pending = {}
    for key, value in (updates or {}).items():
        pending[str(key)] = value
    session_state[_PENDING_WORKSPACE_INPUT_UPDATES_KEY] = pending


def _apply_queued_workspace_input_updates(session_state) -> None:
    pending = session_state.pop(_PENDING_WORKSPACE_INPUT_UPDATES_KEY, None)
    if not isinstance(pending, dict):
        return
    for key, value in pending.items():
        session_state[str(key)] = value


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
        "Continue in the `Auth / OAuth` tab to complete token exchange."
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
    setup_scope_key = _ebay_workspace_setup_scope_key(username=user.username)
    st.subheader("eBay Workspace")
    st.caption("Unified eBay workspace for auth setup, connection health, and daily listing execution.")
    st.page_link("pages/24_eBay_User_Details.py", label="Open eBay User Details", icon=":material/badge:")
    _render_oauth_callback_banner()
    render_help_panel(
        section_title="eBay Workspace",
        goal="Run eBay account auth, connection health checks, and daily listing operations from one workspace.",
        steps=[
            "Use `Auth / OAuth` for OAuth, account checks, and order pull/import.",
            "Use `Connection Health` for publish-readiness blockers/warnings and remediation actions.",
            "Use `Daily Ops` for bulk end/relist/revise and policy/location management.",
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
    default_shipping_service = get_runtime_str(
        repo, "ebay_shipping_service_default", "USPS Ground Advantage"
    ).strip() or "USPS Ground Advantage"
    default_handling_days = int(float(get_runtime_str(repo, "ebay_handling_days_default", "1") or 1))
    default_shipping_cost = float(get_runtime_str(repo, "ebay_shipping_cost_default", "0.0") or 0.0)
    default_package_weight_oz = float(get_runtime_str(repo, "ebay_package_weight_oz_default", "0.0") or 0.0)
    auction_duration_options = ["DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"]
    if default_auction_duration not in set(auction_duration_options):
        default_auction_duration = "DAYS_7"
    default_listed_date_range = (utc_today() - timedelta(days=30), utc_today())
    if "ebay_workspace_access_token" not in st.session_state:
        st.session_state["ebay_workspace_access_token"] = default_token
    if "ebay_workspace_status_filter" not in st.session_state:
        st.session_state["ebay_workspace_status_filter"] = ["draft", "active", "ended", "sold"]
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
    _apply_queued_workspace_input_updates(st.session_state)
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
    if "ebay_workspace_store_shipping_service_input" not in st.session_state:
        st.session_state["ebay_workspace_store_shipping_service_input"] = default_shipping_service
    if "ebay_workspace_store_handling_days_input" not in st.session_state:
        st.session_state["ebay_workspace_store_handling_days_input"] = max(0, int(default_handling_days))
    if "ebay_workspace_store_shipping_cost_input" not in st.session_state:
        st.session_state["ebay_workspace_store_shipping_cost_input"] = max(0.0, float(default_shipping_cost))
    if "ebay_workspace_store_package_weight_oz_input" not in st.session_state:
        st.session_state["ebay_workspace_store_package_weight_oz_input"] = max(0.0, float(default_package_weight_oz))
    if "ebay_workspace_inventory_locations_cache" not in st.session_state:
        st.session_state["ebay_workspace_inventory_locations_cache"] = []
    if "ebay_workspace_inventory_location_select" not in st.session_state:
        st.session_state["ebay_workspace_inventory_location_select"] = "(none)"
    if "ebay_workspace_inventory_locations_status" not in st.session_state:
        st.session_state["ebay_workspace_inventory_locations_status"] = {"level": "", "message": ""}
    if "ebay_workspace_create_location_country" not in st.session_state:
        st.session_state["ebay_workspace_create_location_country"] = "US"
    if "ebay_workspace_create_location_city" not in st.session_state:
        st.session_state["ebay_workspace_create_location_city"] = "Golden"
    if "ebay_workspace_create_location_state" not in st.session_state:
        st.session_state["ebay_workspace_create_location_state"] = "CO"
    if "ebay_workspace_create_location_type" not in st.session_state:
        st.session_state["ebay_workspace_create_location_type"] = "WAREHOUSE"
    if "ebay_workspace_create_location_timezone" not in st.session_state:
        st.session_state["ebay_workspace_create_location_timezone"] = "America/Denver"

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

    pending_setup_resume_payload = st.session_state.pop("ebay_workspace_setup_resume_payload", None)
    if isinstance(pending_setup_resume_payload, dict):
        _ebay_workspace_apply_setup_draft_payload(pending_setup_resume_payload)
        st.session_state["ebay_workspace_setup_draft_flash"] = "Resumed workspace setup draft."

    saved_setup_draft = repo.load_workflow_draft(
        environment=settings.app_env,
        workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
        username=user.username,
        scope_key=setup_scope_key,
        active_only=True,
    )
    saved_setup_payload: dict = {}
    if saved_setup_draft is not None:
        saved_setup_payload = _ebay_workspace_parse_draft_json(str(saved_setup_draft.draft_json or "{}"))

    with st.expander("Workspace Controls", expanded=True):
        setup_draft_flash = str(st.session_state.pop("ebay_workspace_setup_draft_flash", "") or "").strip()
        if setup_draft_flash:
            st.success(setup_draft_flash)
        if saved_setup_draft is not None:
            draft_updated = str(getattr(saved_setup_draft, "updated_at", "") or "").strip()
            d1, d2, d3 = st.columns([2, 1, 1])
            with d1:
                st.caption(
                    "Saved setup draft available"
                    + (f" (last updated {draft_updated})" if draft_updated else "")
                )
            with d2:
                if st.button("Resume Setup Draft", key="ebay_workspace_resume_setup_draft_btn"):
                    resumed = repo.resume_latest_workflow_draft(
                        environment=settings.app_env,
                        workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
                        username=user.username,
                        active_only=True,
                    )
                    resumed_payload = saved_setup_payload
                    resumed_scope = setup_scope_key
                    if resumed is not None and str(getattr(resumed, "scope_key", "") or "").strip() == setup_scope_key:
                        resumed_payload = _ebay_workspace_parse_draft_json(str(resumed.draft_json or "{}"))
                    elif resumed is not None:
                        resumed_scope = str(getattr(resumed, "scope_key", "") or "").strip() or setup_scope_key
                        resumed_payload = _ebay_workspace_parse_draft_json(str(resumed.draft_json or "{}"))
                    st.session_state["ebay_workspace_setup_resume_payload"] = resumed_payload
                    repo.append_workflow_event(
                        environment=settings.app_env,
                        workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
                        username=user.username,
                        scope_key=resumed_scope,
                        action="resume_draft",
                        status="ok",
                        message="Operator resumed eBay workspace setup draft.",
                        payload={"draft_id": int(getattr(resumed, "id", 0) or getattr(saved_setup_draft, "id", 0) or 0)},
                        draft_id=int(getattr(resumed, "id", 0) or getattr(saved_setup_draft, "id", 0) or 0),
                        actor=user.username,
                    )
                    st.rerun()
            with d3:
                if st.button("Clear Setup Draft", key="ebay_workspace_clear_setup_draft_btn"):
                    repo.clear_workflow_draft(
                        environment=settings.app_env,
                        workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
                        username=user.username,
                        scope_key=setup_scope_key,
                        actor=user.username,
                        reason="operator_reset",
                    )
                    for key in list(EBAY_WORKSPACE_SETUP_DRAFT_SESSION_KEYS):
                        st.session_state.pop(key, None)
                    st.session_state.pop("ebay_workspace_last_setup_autosave_signature", None)
                    st.session_state.pop("ebay_workspace_last_setup_autosave_scope", None)
                    st.session_state["ebay_workspace_setup_draft_flash"] = "Cleared workspace setup draft."
                    st.rerun()
        if st.button("Save Setup Draft", key="ebay_workspace_save_setup_draft_btn"):
            setup_payload = _ebay_workspace_build_setup_draft_payload()
            row = repo.save_workflow_draft(
                environment=settings.app_env,
                workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
                username=user.username,
                scope_key=setup_scope_key,
                draft_payload=setup_payload,
                schema_version="v1",
                status="active",
                last_step="workspace_controls",
                actor=user.username,
            )
            repo.append_workflow_event(
                environment=settings.app_env,
                workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
                username=user.username,
                scope_key=setup_scope_key,
                action="save_draft",
                status="ok",
                message="Operator saved eBay workspace setup draft.",
                payload={"draft_id": int(getattr(row, "id", 0) or 0)},
                draft_id=int(getattr(row, "id", 0) or 0),
                actor=user.username,
            )
            st.session_state["ebay_workspace_last_setup_autosave_signature"] = _ebay_workspace_setup_signature(setup_payload)
            st.session_state["ebay_workspace_last_setup_autosave_scope"] = setup_scope_key
            st.session_state["ebay_workspace_setup_draft_flash"] = "Saved workspace setup draft."
            st.rerun()
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
                help="Applied across this workspace for Auth/OAuth checks and daily eBay operations.",
                key="ebay_workspace_access_token_input",
            )
        with wc2:
            st.multiselect(
                "Default Ops Status Filter",
                options=["draft", "active", "ended", "sold"],
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
                updates = {
                    "ebay_workspace_status_filter_input": list(
                        payload.get("status_filter") or ["draft", "active", "ended", "sold"]
                    ),
                    "ebay_workspace_linked_only_input": bool(payload.get("linked_only")),
                    "ebay_workspace_search_input": str(payload.get("search") or "").strip(),
                    "ebay_workspace_use_date_filter_input": bool(payload.get("use_date_filter")),
                }
                access_token = str(payload.get("access_token") or "").strip()
                if access_token:
                    updates["ebay_workspace_access_token_input"] = access_token
                stored_range = payload.get("listed_date_range")
                if isinstance(stored_range, list) and len(stored_range) == 2:
                    start_raw, end_raw = stored_range[0], stored_range[1]
                    try:
                        start_date = datetime.fromisoformat(str(start_raw)).date()
                        end_date = datetime.fromisoformat(str(end_raw)).date()
                        updates["ebay_workspace_listed_date_range_input"] = (start_date, end_date)
                    except Exception:
                        if isinstance(start_raw, date) and isinstance(end_raw, date):
                            updates["ebay_workspace_listed_date_range_input"] = (start_raw, end_raw)
                st.session_state["ebay_workspace_active_profile"] = selected_profile
                _queue_workspace_input_updates(st.session_state, updates)
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
        st.caption("Tip: Merchant Location Key must be an eBay inventory location key (not an address label).")
        loc_rows = st.session_state.get("ebay_workspace_inventory_locations_cache")
        if not isinstance(loc_rows, list):
            loc_rows = []
        loc_lookup: dict[str, str] = {"(none)": ""}
        for row in loc_rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("merchantLocationKey") or "").strip()
            if not key:
                continue
            name = str(row.get("name") or "").strip()
            status = str(row.get("merchantLocationStatus") or "").strip()
            city = str(((row.get("location") or {}).get("address") or {}).get("city") or "").strip()
            label = f"{key} | {name or '-'} | {city or '-'} | {status or '-'}"
            loc_lookup[label] = key
        lf1, lf2, lf3 = st.columns([1, 2, 1])
        with lf1:
            fetch_inventory_locations = st.button(
                "Fetch Inventory Locations",
                key="ebay_workspace_fetch_inventory_locations_btn",
                use_container_width=True,
            )
        with lf2:
            selected_inventory_location_label = st.selectbox(
                "Inventory Location",
                options=list(loc_lookup.keys()),
                key="ebay_workspace_inventory_location_select",
            )
        with lf3:
            apply_inventory_location = st.button(
                "Apply Selected Key",
                key="ebay_workspace_apply_inventory_location_btn",
                use_container_width=True,
                disabled=(selected_inventory_location_label == "(none)"),
            )

        if fetch_inventory_locations:
            token_for_locations = str(st.session_state.get("ebay_workspace_access_token_input") or "").strip() or default_token
            if not client.is_configured():
                st.error("eBay app credentials are not configured.")
                st.session_state["ebay_workspace_inventory_locations_status"] = {
                    "level": "error",
                    "message": "eBay app credentials are not configured.",
                }
            elif not token_for_locations:
                st.error("User access token is required to fetch inventory locations.")
                st.session_state["ebay_workspace_inventory_locations_status"] = {
                    "level": "error",
                    "message": "User access token is required to fetch inventory locations.",
                }
            else:
                try:
                    fetched_rows = client.list_inventory_locations(
                        access_token=token_for_locations,
                        limit=200,
                        offset=0,
                    )
                    st.session_state["ebay_workspace_inventory_locations_cache"] = list(fetched_rows or [])
                    if fetched_rows:
                        first = fetched_rows[0]
                        first_key = str((first or {}).get("merchantLocationKey") or "").strip()
                        if first_key:
                            first_label = next(
                                (lbl for lbl, val in loc_lookup.items() if val == first_key),
                                    "(none)",
                            )
                            _queue_workspace_input_updates(
                                st.session_state,
                                {"ebay_workspace_inventory_location_select": first_label},
                            )
                        st.session_state["ebay_workspace_inventory_locations_status"] = {
                            "level": "success",
                            "message": f"Loaded {len(fetched_rows)} inventory location(s).",
                        }
                    else:
                        st.session_state["ebay_workspace_inventory_locations_status"] = {
                            "level": "warning",
                            "message": (
                                "No inventory locations found in this eBay account. "
                                "Create one in eBay Seller Hub or Inventory API, then fetch again."
                            ),
                        }
                    st.rerun()
                except Exception as exc:
                    st.error(f"Inventory location fetch failed: {exc}")
                    st.session_state["ebay_workspace_inventory_locations_status"] = {
                        "level": "error",
                        "message": f"Inventory location fetch failed: {exc}",
                    }

        if apply_inventory_location:
            selected_key = str(loc_lookup.get(selected_inventory_location_label) or "").strip()
            if not selected_key:
                st.info("Select an inventory location first.")
            else:
                _queue_workspace_input_updates(
                    st.session_state,
                    {"ebay_workspace_store_merchant_location_key_input": selected_key},
                )
                st.success(f"Applied merchant location key `{selected_key}`.")
                st.rerun()

        st.markdown("##### Create Inventory Location")
        st.caption("Create a new eBay inventory location key directly from this app.")
        cl1, cl2 = st.columns(2)
        with cl1:
            create_location_key = st.text_input(
                "Location Key",
                key="ebay_workspace_create_location_key",
                help="Unique key, e.g. goldenstackers-main",
            ).strip()
            create_location_name = st.text_input(
                "Location Name",
                key="ebay_workspace_create_location_name",
                help="Human-friendly label shown in eBay.",
            ).strip()
            create_location_line1 = st.text_input(
                "Address Line 1",
                key="ebay_workspace_create_location_line1",
            ).strip()
            create_location_city = st.text_input(
                "City",
                key="ebay_workspace_create_location_city",
            ).strip()
        with cl2:
            create_location_state = st.text_input(
                "State / Province",
                key="ebay_workspace_create_location_state",
            ).strip()
            create_location_postal = st.text_input(
                "Postal Code",
                key="ebay_workspace_create_location_postal",
            ).strip()
            create_location_country = st.text_input(
                "Country Code",
                key="ebay_workspace_create_location_country",
                help="ISO country code, e.g. US",
            ).strip().upper()
            create_location_type = st.selectbox(
                "Location Type",
                options=["WAREHOUSE", "STORE"],
                key="ebay_workspace_create_location_type",
            )
            create_location_timezone = st.text_input(
                "Time Zone ID",
                key="ebay_workspace_create_location_timezone",
                help="IANA timezone, e.g. America/Denver",
            ).strip() or "America/Denver"
        create_location_phone = st.text_input(
            "Phone (optional)",
            key="ebay_workspace_create_location_phone",
        ).strip()
        st.caption(
            "Required address rule: provide either `city + state/province + country` "
            "or `postalCode + country`."
        )
        create_inventory_location = st.button(
            "Create Inventory Location",
            key="ebay_workspace_create_inventory_location_btn",
            use_container_width=False,
        )
        if create_inventory_location:
            token_for_create = str(st.session_state.get("ebay_workspace_access_token_input") or "").strip() or default_token
            missing_basic = [
                label
                for label, value in [
                    ("Location Key", create_location_key),
                    ("Location Name", create_location_name),
                    ("Country Code", create_location_country),
                ]
                if not str(value or "").strip()
            ]
            has_city_state_country = bool(create_location_city and create_location_state and create_location_country)
            has_postal_country = bool(create_location_postal and create_location_country)
            if not client.is_configured():
                st.error("eBay app credentials are not configured.")
            elif not token_for_create:
                st.error("User access token is required to create inventory location.")
            elif missing_basic:
                st.error("Missing required fields: " + ", ".join(missing_basic))
            elif not (has_city_state_country or has_postal_country):
                st.error(
                    "Address requirements not met. Provide either city + state/province + country "
                    "or postalCode + country."
                )
            else:
                address_payload: dict[str, str] = {}
                if create_location_line1:
                    address_payload["addressLine1"] = create_location_line1
                if create_location_city:
                    address_payload["city"] = create_location_city
                if create_location_state:
                    address_payload["stateOrProvince"] = create_location_state
                if create_location_postal:
                    address_payload["postalCode"] = create_location_postal
                address_payload["country"] = create_location_country
                payload = {
                    "name": create_location_name,
                    "merchantLocationStatus": "ENABLED",
                    "locationTypes": [create_location_type],
                    "location": {
                        "address": address_payload,
                    },
                }
                if create_location_phone:
                    payload["phone"] = create_location_phone
                try:
                    client.create_or_replace_inventory_location(
                        access_token=token_for_create,
                        merchant_location_key=create_location_key,
                        payload=payload,
                    )
                    try:
                        refreshed = client.list_inventory_locations(
                            access_token=token_for_create,
                            limit=200,
                            offset=0,
                        )
                    except Exception:
                        refreshed = st.session_state.get("ebay_workspace_inventory_locations_cache") or []
                    st.session_state["ebay_workspace_inventory_locations_cache"] = list(refreshed or [])
                    st.session_state["ebay_workspace_inventory_locations_status"] = {
                        "level": "success",
                        "message": f"Created inventory location `{create_location_key}`.",
                    }
                    _queue_workspace_input_updates(
                        st.session_state,
                        {
                            "ebay_workspace_store_merchant_location_key_input": create_location_key,
                        },
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Create inventory location failed: {exc}")

        status_payload = st.session_state.get("ebay_workspace_inventory_locations_status") or {}
        status_level = str(status_payload.get("level") or "").strip().lower()
        status_message = str(status_payload.get("message") or "").strip()
        if status_message:
            if status_level == "success":
                st.success(status_message)
            elif status_level == "warning":
                st.warning(status_message)
            elif status_level == "error":
                st.error(status_message)
            else:
                st.info(status_message)
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
        st.markdown("##### Shipping Defaults")
        fs1, fs2 = st.columns(2)
        with fs1:
            st.text_input(
                "Shipping Service Default",
                key="ebay_workspace_store_shipping_service_input",
            )
            st.number_input(
                "Handling Days Default",
                min_value=0,
                step=1,
                key="ebay_workspace_store_handling_days_input",
            )
        with fs2:
            st.number_input(
                "Shipping Cost Default",
                min_value=0.0,
                step=0.01,
                key="ebay_workspace_store_shipping_cost_input",
            )
            st.number_input(
                "Package Weight (oz) Default",
                min_value=0.0,
                step=0.1,
                key="ebay_workspace_store_package_weight_oz_input",
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
                    "shipping_service_default": str(
                        st.session_state.get("ebay_workspace_store_shipping_service_input") or ""
                    ).strip(),
                    "handling_days_default": int(
                        st.session_state.get("ebay_workspace_store_handling_days_input") or 0
                    ),
                    "shipping_cost_default": float(
                        st.session_state.get("ebay_workspace_store_shipping_cost_input") or 0.0
                    ),
                    "package_weight_oz_default": float(
                        st.session_state.get("ebay_workspace_store_package_weight_oz_input") or 0.0
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
                updates = {
                    "ebay_workspace_store_merchant_location_key_input": str(
                        store_payload.get("merchant_location_key") or ""
                    ).strip(),
                    "ebay_workspace_store_payment_policy_id_input": str(
                        store_payload.get("payment_policy_id") or ""
                    ).strip(),
                    "ebay_workspace_store_fulfillment_policy_id_input": str(
                        store_payload.get("fulfillment_policy_id") or ""
                    ).strip(),
                    "ebay_workspace_store_return_policy_id_input": str(
                        store_payload.get("return_policy_id") or ""
                    ).strip(),
                    "ebay_workspace_store_category_id_input": str(
                        store_payload.get("category_id") or ""
                    ).strip(),
                    "ebay_workspace_store_listing_format_input": str(
                        store_payload.get("listing_format") or "FIXED_PRICE"
                    ).strip(),
                    "ebay_workspace_store_best_offer_enabled_input": bool(
                        store_payload.get("best_offer_enabled")
                    ),
                    "ebay_workspace_store_auction_duration_input": str(
                        store_payload.get("auction_duration") or "DAYS_7"
                    ).strip(),
                    "ebay_workspace_store_auction_start_input": float(
                        store_payload.get("auction_start_default") or 1.0
                    ),
                    "ebay_workspace_store_auction_reserve_input": float(
                        store_payload.get("auction_reserve_default") or 0.0
                    ),
                    "ebay_workspace_store_auction_buy_now_input": float(
                        store_payload.get("auction_buy_now_default") or 0.0
                    ),
                    "ebay_workspace_store_shipping_service_input": str(
                        store_payload.get("shipping_service_default") or default_shipping_service
                    ).strip(),
                    "ebay_workspace_store_handling_days_input": int(
                        store_payload.get("handling_days_default") or default_handling_days
                    ),
                    "ebay_workspace_store_shipping_cost_input": float(
                        store_payload.get("shipping_cost_default") or default_shipping_cost
                    ),
                    "ebay_workspace_store_package_weight_oz_input": float(
                        store_payload.get("package_weight_oz_default") or default_package_weight_oz
                    ),
                    "ebay_workspace_store_marketplace_id_input": str(
                        store_payload.get("marketplace_id") or default_marketplace_id
                    ).strip(),
                    "ebay_workspace_store_currency_input": str(
                        store_payload.get("currency") or default_currency
                    ).strip(),
                    "ebay_workspace_store_content_language_input": str(
                        store_payload.get("content_language") or default_content_language
                    ).strip(),
                }
                st.session_state["ebay_workspace_active_store_profile"] = selected_store_profile
                _queue_workspace_input_updates(st.session_state, updates)
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
                    safe_switch_page("pages/03_Listings.py")
        if apply_workspace:
            st.session_state["ebay_workspace_access_token"] = str(
                st.session_state.get("ebay_workspace_access_token_input") or ""
            ).strip()
            st.session_state["ebay_workspace_status_filter"] = list(
                st.session_state.get("ebay_workspace_status_filter_input") or ["draft", "active", "ended", "sold"]
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
            st.session_state["ebay_pub_shipping_service"] = str(
                st.session_state.get("ebay_workspace_store_shipping_service_input") or ""
            ).strip()
            st.session_state["ebay_pub_handling_days"] = int(
                st.session_state.get("ebay_workspace_store_handling_days_input") or 0
            )
            st.session_state["ebay_pub_shipping_cost"] = float(
                st.session_state.get("ebay_workspace_store_shipping_cost_input") or 0.0
            )
            st.session_state["ebay_pub_package_weight_oz"] = float(
                st.session_state.get("ebay_workspace_store_package_weight_oz_input") or 0.0
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
                        ("ebay_shipping_service_default", st.session_state.get("ebay_pub_shipping_service"), "str"),
                        ("ebay_handling_days_default", st.session_state.get("ebay_pub_handling_days"), "int"),
                        ("ebay_shipping_cost_default", st.session_state.get("ebay_pub_shipping_cost"), "float"),
                        ("ebay_package_weight_oz_default", st.session_state.get("ebay_pub_package_weight_oz"), "float"),
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
            st.success("Applied shared workspace context to Auth/OAuth, Connection Health, and Daily Ops tabs.")
            st.rerun()
        if clear_workspace:
            st.session_state["ebay_workspace_access_token"] = default_token
            st.session_state["ebay_workspace_status_filter"] = ["draft", "active", "ended", "sold"]
            st.session_state["ebay_workspace_linked_only"] = False
            st.session_state["ebay_workspace_search"] = ""
            st.session_state["ebay_workspace_use_date_filter"] = False
            st.session_state["ebay_workspace_listed_date_range"] = default_listed_date_range
            _queue_workspace_input_updates(
                st.session_state,
                {
                    "ebay_workspace_access_token_input": default_token,
                    "ebay_workspace_status_filter_input": ["draft", "active", "ended", "sold"],
                    "ebay_workspace_linked_only_input": False,
                    "ebay_workspace_search_input": "",
                    "ebay_workspace_use_date_filter_input": False,
                    "ebay_workspace_listed_date_range_input": default_listed_date_range,
                },
            )
            st.session_state["ebay_ops_access_token"] = default_token
            st.session_state["ebay_ops_status_filter"] = ["draft", "active", "ended", "sold"]
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
    st.session_state.setdefault("ebay_ops_status_filter", list(st.session_state.get("ebay_workspace_status_filter") or ["draft", "active", "ended", "sold"]))
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
            _queue_workspace_input_updates(
                st.session_state,
                {
                    "ebay_workspace_store_merchant_location_key_input": str(
                        payload.get("merchant_location_key") or default_merchant_location_key
                    ).strip(),
                    "ebay_workspace_store_payment_policy_id_input": str(
                        payload.get("payment_policy_id") or default_payment_policy_id
                    ).strip(),
                    "ebay_workspace_store_fulfillment_policy_id_input": str(
                        payload.get("fulfillment_policy_id") or default_fulfillment_policy_id
                    ).strip(),
                    "ebay_workspace_store_return_policy_id_input": str(
                        payload.get("return_policy_id") or default_return_policy_id
                    ).strip(),
                    "ebay_workspace_store_category_id_input": str(
                        payload.get("category_id") or default_category_id
                    ).strip(),
                    "ebay_workspace_store_listing_format_input": str(
                        payload.get("listing_format") or default_listing_format
                    ).strip(),
                    "ebay_workspace_store_best_offer_enabled_input": bool(
                        payload.get("best_offer_enabled")
                    ),
                    "ebay_workspace_store_auction_duration_input": str(
                        payload.get("auction_duration") or default_auction_duration
                    ).strip(),
                    "ebay_workspace_store_auction_start_input": float(
                        payload.get("auction_start_default") or default_auction_start
                    ),
                    "ebay_workspace_store_auction_reserve_input": float(
                        payload.get("auction_reserve_default") or default_auction_reserve
                    ),
                    "ebay_workspace_store_auction_buy_now_input": float(
                        payload.get("auction_buy_now_default") or default_auction_buy_now
                    ),
                    "ebay_workspace_store_marketplace_id_input": str(
                        payload.get("marketplace_id") or default_marketplace_id
                    ).strip(),
                    "ebay_workspace_store_currency_input": str(
                        payload.get("currency") or default_currency
                    ).strip(),
                    "ebay_workspace_store_content_language_input": str(
                        payload.get("content_language") or default_content_language
                    ).strip(),
                    "ebay_workspace_store_alias": default_store_profile_name,
                },
            )
            st.session_state["ebay_workspace_active_store_profile"] = default_store_profile_name
            st.session_state["ebay_workspace_store_default_loaded_once"] = True
            st.rerun()
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
    st.caption(
        "Readiness details and remediation actions are centralized in the "
        "`Connection Health` tab (`Publish Readiness Summary`)."
    )
    st.markdown("### Quick Links")
    st.caption("Jump directly to the next operational surface without using sidebar navigation.")
    ql1, ql2, ql3, ql4, ql5 = st.columns(5)
    with ql1:
        if st.button("Open Listings", key="ebay_workspace_quick_open_listings", use_container_width=True):
            st.session_state["listings_filter_marketplaces"] = ["ebay"]
            st.session_state["listings_filter_status"] = list(
                st.session_state.get("ebay_workspace_status_filter") or ["draft", "active", "ended", "sold"]
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
                safe_switch_page("pages/03_Listings.py")
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
                safe_switch_page("pages/18_Sync.py")
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
                safe_switch_page("pages/11_Shipping.py")
    with ql4:
        if st.button("Open Admin", key="ebay_workspace_quick_open_admin", use_container_width=True):
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/17_Admin.py")
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
                safe_switch_page("pages/03_Listings.py")
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
                safe_switch_page("pages/03_Listings.py")
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
                safe_switch_page("pages/03_Listings.py")

    wrow1, wrow2 = st.columns(2)
    with wrow1:
        if st.button(
            "Open Listing Wizard",
            key="ebay_workspace_quick_open_listing_wizard",
            use_container_width=True,
        ):
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "listing_wizard"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "listing_wizard",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/26_Listing_Wizard.py")
    with wrow2:
        if st.button(
            "Open eBay Templates",
            key="ebay_workspace_quick_open_ebay_templates",
            use_container_width=True,
        ):
            st.session_state["workspace_handoff_from"] = "ebay_workspace"
            st.session_state["workspace_handoff_target"] = "ebay_templates"
            try:
                repo.record_audit_event(
                    entity_type="navigation",
                    entity_id=None,
                    action="workspace_handoff_applied",
                    actor=user.username,
                    changes={
                        "from": "ebay_workspace",
                        "to": "ebay_templates",
                    },
                )
            except Exception:
                pass
            if hasattr(st, "switch_page"):
                safe_switch_page("pages/25_eBay_Templates.py")

    tab_auth, tab_health, tab_operations = st.tabs(["Auth / OAuth", "Connection Health", "Daily Ops"])
    format_fix_backlog_count = _ebay_format_fix_backlog_count(
        repo,
        default_format_type=default_listing_format,
        default_auction_duration=default_auction_duration,
    )
    warning_total = 0
    for _reason, count in (readiness_breakdown.get("top_warnings") or []):
        try:
            warning_total += int(count)
        except Exception:
            continue

    with tab_auth:
        st.caption("Authorize eBay accounts, exchange OAuth codes, and verify account privileges.")
        render_ebay(client, repo)

    with tab_health:
        st.caption("Operational readiness signals for eBay listing and sync execution.")
        blocked_count = int(readiness_breakdown.get("blocked_count") or 0)
        blocker_types = int(readiness_breakdown.get("unique_blockers") or 0)
        top_blockers = readiness_breakdown.get("top_blockers") or []
        top_warnings = readiness_breakdown.get("top_warnings") or []

        with st.container(border=True):
            st.markdown("#### Publish Readiness Summary")
            if blocked_count > 0:
                st.error(
                    f"Blocked: {blocked_count} eBay listing(s) have readiness blockers across {blocker_types} blocker type(s)."
                )
            elif warning_total > 0:
                st.warning(
                    f"Ready with warnings: {warning_total} warning signal(s) detected. Review before publish."
                )
            else:
                st.success("Ready: no current readiness blockers or warnings detected.")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Blocked Listings", blocked_count)
            c2.metric("Blocker Types", blocker_types)
            c3.metric("Warning Signals", int(warning_total))
            c4.metric("Format Fix Needed", int(format_fix_backlog_count))

            if top_blockers:
                st.caption(
                    "Top blockers: "
                    + ", ".join(
                        f"{str(reason)} ({int(count)})"
                        for reason, count in top_blockers[:3]
                    )
                )
            if top_warnings:
                st.caption(
                    "Top warnings: "
                    + ", ".join(
                        f"{str(reason)} ({int(count)})"
                        for reason, count in top_warnings[:3]
                    )
                )

            a1, a2, a3, a4 = st.columns(4)
            with a1:
                if st.button(
                    "Open Blocked Listings",
                    key="ebay_workspace_health_open_blocked_listings",
                    use_container_width=True,
                ):
                    st.session_state["listings_filter_marketplaces"] = ["ebay"]
                    st.session_state["listings_filter_status"] = ["draft", "active"]
                    st.session_state["listings_filter_query"] = ""
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
                                "preset": "blocked_listings_queue",
                                "source_tab": "health",
                                "marketplaces": ["ebay"],
                                "statuses": ["draft", "active"],
                                "query": "",
                                "origin": "all",
                                "format_issue_only": False,
                            },
                        )
                    except Exception:
                        pass
                    if hasattr(st, "switch_page"):
                        safe_switch_page("pages/03_Listings.py")
            with a2:
                if st.button(
                    "Open Format Fix Queue",
                    key="ebay_workspace_health_open_format_fix",
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
                                "source_tab": "health",
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
                        safe_switch_page("pages/03_Listings.py")
            with a3:
                if st.button(
                    "Open Listing Wizard",
                    key="ebay_workspace_health_open_listing_wizard",
                    use_container_width=True,
                ):
                    st.session_state["workspace_handoff_from"] = "ebay_workspace"
                    st.session_state["workspace_handoff_target"] = "listing_wizard"
                    try:
                        repo.record_audit_event(
                            entity_type="navigation",
                            entity_id=None,
                            action="workspace_handoff_applied",
                            actor=user.username,
                            changes={
                                "from": "ebay_workspace",
                                "to": "listing_wizard",
                                "source_tab": "health",
                            },
                        )
                    except Exception:
                        pass
                    if hasattr(st, "switch_page"):
                        safe_switch_page("pages/26_Listing_Wizard.py")
            with a4:
                if st.button(
                    "Open Admin (eBay Verify)",
                    key="ebay_workspace_health_open_admin",
                    use_container_width=True,
                ):
                    st.session_state["workspace_handoff_from"] = "ebay_workspace"
                    st.session_state["workspace_handoff_target"] = "admin"
                    try:
                        repo.record_audit_event(
                            entity_type="navigation",
                            entity_id=None,
                            action="workspace_handoff_applied",
                            actor=user.username,
                            changes={
                                "from": "ebay_workspace",
                                "to": "admin",
                                "source_tab": "health",
                                "focus": "ebay_verify",
                            },
                        )
                    except Exception:
                        pass
                    if hasattr(st, "switch_page"):
                        safe_switch_page("pages/17_Admin.py")

        with st.expander("Finances Scope Probe", expanded=False):
            st.caption(
                "Verify the current user token can call eBay Finances API (`sell.finances`). "
                "If probe fails with auth/permission errors, re-authorize in-app to refresh granted scopes."
            )
            if "ebay_workspace_fin_probe_result" not in st.session_state:
                st.session_state["ebay_workspace_fin_probe_result"] = {}
            fp1, fp2, fp3 = st.columns([2, 2, 1])
            with fp1:
                probe_token = st.text_area(
                    "Access Token (optional override)",
                    value=str(st.session_state.get("ebay_workspace_access_token_input") or "").strip(),
                    key="ebay_workspace_fin_probe_token",
                    height=90,
                )
            with fp2:
                probe_order_id = st.text_input(
                    "Optional Order ID",
                    key="ebay_workspace_fin_probe_order_id",
                    placeholder="e.g. 23-14477-17302",
                )
            with fp3:
                st.write("")
                st.write("")
                if st.button("Run Finances Probe", key="ebay_workspace_fin_probe_run", use_container_width=True):
                    resolved_token = str(probe_token or "").strip() or str(
                        st.session_state.get("ebay_workspace_access_token_input") or ""
                    ).strip()
                    if not resolved_token:
                        st.error("Access token is required.")
                    else:
                        try:
                            order_id = str(probe_order_id or "").strip()
                            if order_id:
                                if "-" not in order_id:
                                    st.warning(
                                        "Entered value does not look like an eBay order ID (`23-...-...`). "
                                        "This may be an item/listing ID."
                                    )
                                rows = client.list_finance_transactions_for_order(
                                    access_token=resolved_token,
                                    order_id=order_id,
                                    limit=100,
                                )
                                match_count = len(
                                    [
                                        r
                                        for r in rows
                                        if order_id in json.dumps(r, default=str)
                                    ]
                                )
                                st.session_state["ebay_workspace_fin_probe_result"] = {
                                    "status": "ok",
                                    "mode": "order_filtered",
                                    "order_id": order_id,
                                    "rows_returned": len(rows),
                                    "rows_matching_order_id_text": match_count,
                                    "sample": rows[:3],
                                }
                            else:
                                rows = client.list_finance_transactions(
                                    access_token=resolved_token,
                                    limit=10,
                                )
                                st.session_state["ebay_workspace_fin_probe_result"] = {
                                    "status": "ok",
                                    "mode": "recent_transactions",
                                    "rows_returned": len(rows),
                                    "sample": rows[:3],
                                }
                            st.success("Finances probe succeeded.")
                        except Exception as exc:
                            st.session_state["ebay_workspace_fin_probe_result"] = {
                                "status": "failed",
                                "error": str(exc),
                            }
                            st.error(f"Finances probe failed: {exc}")
            fin_probe = st.session_state.get("ebay_workspace_fin_probe_result") or {}
            if isinstance(fin_probe, dict) and fin_probe:
                st.json(fin_probe)

        with st.expander("Order Financial Extraction Tester", expanded=False):
            st.caption(
                "Fetch an eBay order + fulfillments (or paste raw JSON) and run the same financial extraction "
                "logic used by sync import for fees/shipping/label spend."
            )
            if "ebay_workspace_diag_order_json" not in st.session_state:
                st.session_state["ebay_workspace_diag_order_json"] = "{}"
            if "ebay_workspace_diag_fulfillments_json" not in st.session_state:
                st.session_state["ebay_workspace_diag_fulfillments_json"] = "[]"
            if "ebay_workspace_diag_result" not in st.session_state:
                st.session_state["ebay_workspace_diag_result"] = {}

            d1, d2, d3 = st.columns([2, 2, 1])
            with d1:
                diag_token = st.text_area(
                    "Access Token (optional override)",
                    value=str(st.session_state.get("ebay_workspace_access_token_input") or "").strip(),
                    height=90,
                    key="ebay_workspace_diag_access_token",
                )
            with d2:
                diag_order_id = st.text_input(
                    "eBay Order ID",
                    key="ebay_workspace_diag_order_id",
                    placeholder="e.g. 12-12345-12345",
                )
            with d3:
                st.write("")
                st.write("")
                if st.button("Fetch Live Order", key="ebay_workspace_diag_fetch_live", use_container_width=True):
                    resolved_token = str(diag_token or "").strip() or str(
                        st.session_state.get("ebay_workspace_access_token_input") or ""
                    ).strip()
                    if not resolved_token:
                        st.error("Access token is required to fetch live order data.")
                    elif not str(diag_order_id or "").strip():
                        st.error("Order ID is required.")
                    else:
                        try:
                            order_payload = client.get_order(
                                access_token=resolved_token,
                                order_id=str(diag_order_id).strip(),
                            )
                            fulfillments = client.list_shipping_fulfillments(
                                access_token=resolved_token,
                                order_id=str(diag_order_id).strip(),
                            )
                            finance_transactions: list[dict] = []
                            list_finance_for_order = getattr(client, "list_finance_transactions_for_order", None)
                            if callable(list_finance_for_order):
                                try:
                                    finance_transactions = list_finance_for_order(
                                        access_token=resolved_token,
                                        order_id=str(diag_order_id).strip(),
                                        limit=100,
                                    )
                                except Exception:
                                    finance_transactions = []
                            st.session_state["ebay_workspace_diag_order_json"] = json.dumps(
                                order_payload,
                                indent=2,
                                default=str,
                            )
                            st.session_state["ebay_workspace_diag_fulfillments_json"] = json.dumps(
                                fulfillments,
                                indent=2,
                                default=str,
                            )
                            st.session_state["ebay_workspace_diag_finance_tx_json"] = json.dumps(
                                finance_transactions,
                                indent=2,
                                default=str,
                            )
                            st.success("Fetched live order + fulfillment + finance transaction payloads.")
                        except Exception as exc:
                            st.error(f"Live order fetch failed: {exc}")

            st.text_area(
                "Order JSON",
                key="ebay_workspace_diag_order_json",
                height=220,
            )
            st.text_area(
                "Fulfillments JSON",
                key="ebay_workspace_diag_fulfillments_json",
                height=180,
            )
            st.text_area(
                "Finance Transactions JSON (optional)",
                key="ebay_workspace_diag_finance_tx_json",
                height=180,
            )

            if st.button("Run Extraction Diagnostic", key="ebay_workspace_diag_run", use_container_width=True):
                try:
                    parsed_order = json.loads(str(st.session_state.get("ebay_workspace_diag_order_json") or "{}"))
                    parsed_fulfillments = json.loads(
                        str(st.session_state.get("ebay_workspace_diag_fulfillments_json") or "[]")
                    )
                    parsed_finance_tx = json.loads(
                        str(st.session_state.get("ebay_workspace_diag_finance_tx_json") or "[]")
                    )
                    if not isinstance(parsed_order, dict):
                        raise ValueError("Order JSON must be an object.")
                    if not isinstance(parsed_fulfillments, list):
                        raise ValueError("Fulfillments JSON must be an array.")
                    if not isinstance(parsed_finance_tx, list):
                        raise ValueError("Finance Transactions JSON must be an array.")
                    st.session_state["ebay_workspace_diag_result"] = build_ebay_order_financial_diagnostics(
                        parsed_order,
                        fulfillments=parsed_fulfillments,
                        finance_transactions=parsed_finance_tx,
                    )
                    st.success("Extraction diagnostic complete.")
                except Exception as exc:
                    st.error(f"Diagnostic failed: {exc}")

            diag_result = st.session_state.get("ebay_workspace_diag_result") or {}
            if isinstance(diag_result, dict) and diag_result:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Fees (Extracted)", f"${float(diag_result.get('marketplace_fee_extracted') or 0):,.2f}")
                m2.metric(
                    "Shipping Charged (Extracted)",
                    f"${float(diag_result.get('shipping_charged_extracted') or 0):,.2f}",
                )
                m3.metric(
                    "Label Spend (Extracted)",
                    f"${float(diag_result.get('shipping_label_spend_extracted') or 0):,.2f}",
                )
                m4.metric(
                    "Shipping Delta",
                    f"${float(diag_result.get('shipping_delta_charged_minus_label_spend') or 0):,.2f}",
                )
                st.json(diag_result)

    with tab_operations:
        st.caption("Run daily revise/end/relist and listing operations.")
        st.info(
            "For format-fix and publish-readiness remediation, use the `Connection Health` tab summary card."
        )
        render_ebay_ops(repo)

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

        with st.expander("Document Draft Quick Handoff (eBay)", expanded=False):
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

    autosave_setup_payload = _ebay_workspace_build_setup_draft_payload()
    autosave_setup_signature = _ebay_workspace_setup_signature(autosave_setup_payload)
    previous_setup_signature = str(st.session_state.get("ebay_workspace_last_setup_autosave_signature") or "").strip()
    previous_setup_scope = str(st.session_state.get("ebay_workspace_last_setup_autosave_scope") or "").strip()
    if autosave_setup_signature != previous_setup_signature or previous_setup_scope != setup_scope_key:
        autosave_row = repo.save_workflow_draft(
            environment=settings.app_env,
            workflow_key=EBAY_WORKSPACE_SETUP_WORKFLOW_KEY,
            username=user.username,
            scope_key=setup_scope_key,
            draft_payload=autosave_setup_payload,
            schema_version="v1",
            status="active",
            last_step="workspace_autosave",
            actor=user.username,
        )
        st.session_state["ebay_workspace_last_setup_autosave_signature"] = autosave_setup_signature
        st.session_state["ebay_workspace_last_setup_autosave_scope"] = setup_scope_key
        st.session_state["ebay_workspace_last_setup_draft_id"] = int(getattr(autosave_row, "id", 0) or 0)

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
