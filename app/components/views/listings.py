from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
import hashlib
import json
import re
import time

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit.errors import StreamlitAPIException

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import (
    build_product_options,
    iso_or_none,
    normalize_multiselect_values,
    to_decimal,
)
from app.components.views.shared import (
    MARKETPLACES,
    handoff_to_documents_draft,
    render_help_panel,
    load_media_bytes,
    render_media_capture_inputs,
    render_media_file_actions,
    render_media_gallery,
    render_table_toolbar,
    upload_media_for_listing,
)
from app.components.views.entity_ops import (
    render_saved_filter_bar,
    render_standard_row_actions,
)
from app.components.views.workspace_shell import render_workspace_feedback, render_workspace_task_completion
from app.config import settings
from app.db.models import Product
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.ai_quality import (
    find_forbidden_terms,
    is_weak_listing_details as _is_weak_details_shared,
    is_weak_listing_title,
    load_ai_quality_policy,
)
from app.services.ebay_aspects import (
    merge_ebay_aspects_defaults,
    missing_required_ebay_aspects,
    normalize_ebay_category_aspect_rows,
)
from app.services.ebay import (
    EBAY_DEFAULT_INVENTORY_CONDITIONS,
    EBAY_MAX_CONDITION_DESCRIPTION_CHARS,
    EBAY_MAX_INVENTORY_DESCRIPTION_CHARS,
    EbayClient,
    build_ebay_inventory_item_sku,
    ebay_condition_label,
    normalize_ebay_condition_policy_rows,
)
from app.services.ebay_fee_estimator import (
    calculate_expected_net_score,
    estimate_ebay_fees,
    resolve_product_known_unit_cost,
)
from app.services.listing_orchestration import (
    build_channel_adapters,
    capability_matrix_rows,
    orchestration_status_for_listing,
)
from app.services.listing_readiness import evaluate_ebay_readiness
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_bool, get_runtime_str, get_runtime_values
from app.services.sync_jobs import execute_sync_job
from app.services.validation import ValidationService, ValidationError
from app.services.video_processing import (
    is_ebay_video_upload_candidate,
    is_mov_video_media,
    is_mp4_video_media,
    mp4_filename_for_media,
    transcode_mov_to_mp4,
)
from app.services.workflow_contracts import build_listing_draft_payload, extract_listing_draft_payload
from app.utils.time import utc_today, utcnow_naive

LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY = "listings_ebay_publish"
LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS = [
    "ebay_pub_title",
    "ebay_pub_format",
    "ebay_pub_auction_duration",
    "ebay_pub_best_offer_enabled",
    "ebay_pub_best_offer_auto_accept",
    "ebay_pub_best_offer_minimum",
    "ebay_pub_qty",
    "ebay_pub_condition",
    "ebay_pub_category_id",
    "ebay_pub_store_category_names",
    "ebay_pub_fixed_price",
    "ebay_pub_auction_start",
    "ebay_pub_auction_reserve",
    "ebay_pub_auction_buy_now",
    "ebay_pub_description",
    "ebay_pub_merchant_location_key",
    "ebay_pub_payment_policy_id",
    "ebay_pub_fulfillment_policy_id",
    "ebay_pub_return_policy_id",
    "ebay_pub_access_token",
    "ebay_pub_marketplace_id",
    "ebay_pub_currency",
    "ebay_pub_content_language",
    "ebay_pub_upload_to_eps",
    "ebay_pub_upload_video_to_ebay",
    "ebay_pub_selected_images",
    "ebay_pub_primary_image_label",
    "ebay_pub_selected_video",
    "ebay_pub_package_weight_oz",
    "ebay_pub_package_length_in",
    "ebay_pub_package_width_in",
    "ebay_pub_package_height_in",
    "ebay_pub_subtitle",
    "ebay_pub_condition_description",
    "ebay_pub_aspects_json",
    "ebay_pub_shipping_service",
    "ebay_pub_handling_days",
    "ebay_pub_shipping_cost",
    "ebay_pub_estimated_buyer_shipping",
    "ebay_pub_estimated_promoted_rate",
    "ebay_pub_estimated_local_shipping_cost_per_item",
    "ebay_pub_volume_pricing_json",
    "ebay_pub_volume_discount_buy2",
    "ebay_pub_volume_discount_buy3",
    "ebay_pub_volume_discount_buy4",
    "ebay_pub_include_volume_pricing_in_description",
    "ebay_pub_post_mode",
    "ebay_pub_category_query",
    "ebay_pub_category_query_seed_product_id",
    "ebay_pub_category_suggestions",
    "ebay_pub_category_aspect_rows",
    "ebay_pub_category_suggestion_select",
    "ebay_pub_manage_offer_id",
    "ebay_manage_offer_id_input",
    "ebay_pub_dependency_preflight_result",
]
LISTINGS_EBAY_PUBLISH_PRESERVE_KEYS = [
    key
    for key in LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS
    if key != "ebay_pub_dependency_preflight_result"
]


def _photo_comp_created_listing_ids(
    repo: InventoryRepository,
    limit: int = 5000,
    audit_rows: list[object] | None = None,
) -> set[int]:
    rows = list(audit_rows) if audit_rows is not None else list(repo.list_audit_logs(limit=max(1, int(limit))))
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


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _known_unit_cost(product: object | None) -> float:
    return round(float(resolve_product_known_unit_cost(product)), 2)


def _bundle_component(product: object, quantity_per_listing: int) -> dict[str, object]:
    units = max(1, int(quantity_per_listing or 1))
    return {
        "product_id": int(getattr(product, "id", 0) or 0),
        "sku": str(getattr(product, "sku", "") or "").strip(),
        "title": str(getattr(product, "title", "") or "").strip(),
        "quantity_per_listing": units,
        "current_quantity": int(getattr(product, "current_quantity", 0) or 0),
    }


def _expected_net_score(
    *,
    fee_estimate: dict,
    quantity: int,
    known_unit_cost: float,
    estimated_local_shipping_cost_per_item: float,
) -> dict[str, float | str]:
    return calculate_expected_net_score(
        fee_estimate=fee_estimate,
        quantity=quantity,
        known_unit_cost=known_unit_cost,
        estimated_local_shipping_cost_per_item=estimated_local_shipping_cost_per_item,
    )


def _build_listing_bundle_metadata(
    *,
    enabled: bool,
    primary_product: object | None,
    units_per_listing: int,
    available_lots: int,
    additional_components: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    lots = max(1, int(available_lots or 1))
    if not enabled or primary_product is None:
        return {
            "enabled": False,
            "components": [],
            "units_per_listing_total": 1,
            "available_lots": lots,
            "inventory_units_committed": lots,
        }
    product_id = int(getattr(primary_product, "id", 0) or 0)
    component = _bundle_component(primary_product, units_per_listing)
    components = [component]
    for extra in list(additional_components or []):
        if not isinstance(extra, dict):
            continue
        try:
            extra_product_id = int(extra.get("product_id") or 0)
            extra_units = max(1, int(extra.get("quantity_per_listing") or 1))
        except Exception:
            continue
        if extra_product_id <= 0 or extra_product_id == product_id:
            continue
        components.append(
            {
                "product_id": extra_product_id,
                "sku": str(extra.get("sku") or "").strip(),
                "title": str(extra.get("title") or "").strip(),
                "quantity_per_listing": extra_units,
                "current_quantity": max(0, int(extra.get("current_quantity") or 0)),
            }
        )
    units_total = sum(max(1, int(row.get("quantity_per_listing") or 1)) for row in components)
    return {
        "enabled": True,
        "kind": "mixed_product_bundle" if len(components) > 1 else "single_product_lot",
        "primary_product_id": product_id,
        "components": components,
        "units_per_listing_total": units_total,
        "available_lots": lots,
        "inventory_units_committed": units_total * lots,
    }


def _filter_listing_rows_base(
    rows: list[dict],
    *,
    query: str,
    marketplaces: set[str],
    statuses: set[str],
    origin_filter: str,
    include_archived: bool,
) -> list[dict]:
    filtered: list[dict] = []
    q = str(query or "").strip().lower()
    origin = str(origin_filter or "all").strip().lower()
    for row in rows:
        if q and q not in str(row.get("title") or "").lower() and q not in str(row.get("external_listing_id") or "").lower():
            continue
        if marketplaces and str(row.get("marketplace") or "").strip().lower() not in marketplaces:
            continue
        if statuses and str(row.get("status") or "").strip().lower() not in statuses:
            continue
        row_origin = str(row.get("origin") or "other").strip().lower()
        if origin in {"photo_comp_draft", "other"} and row_origin != origin:
            continue
        if not include_archived and bool(row.get("archived")):
            continue
        filtered.append(row)
    return filtered


def _filter_listing_objects_base(
    listing_objs: list[object],
    *,
    query: str,
    marketplaces: set[str],
    statuses: set[str],
    origin_filter: str,
    include_archived: bool,
    resolve_origin,
) -> list[object]:
    filtered: list[object] = []
    q = str(query or "").strip().lower()
    origin = str(origin_filter or "all").strip().lower()
    for listing_obj in listing_objs:
        title = str(getattr(listing_obj, "listing_title", "") or "")
        external_listing_id = str(getattr(listing_obj, "external_listing_id", "") or "")
        marketplace = str(getattr(listing_obj, "marketplace", "") or "").strip().lower()
        status = str(getattr(listing_obj, "listing_status", "") or "").strip().lower()
        if q and q not in title.lower() and q not in external_listing_id.lower():
            continue
        if marketplaces and marketplace not in marketplaces:
            continue
        if statuses and status not in statuses:
            continue
        listing_origin = str(resolve_origin(listing_obj) or "other").strip().lower()
        if origin in {"photo_comp_draft", "other"} and listing_origin != origin:
            continue
        if not include_archived and bool(_listing_is_archived(listing_obj)):
            continue
        filtered.append(listing_obj)
    return filtered


def _maybe_hydrate_listing_format_diagnostics(
    rows: list[dict],
    *,
    diagnostics_required: bool,
    hydrate_rows,
) -> None:
    if diagnostics_required and rows:
        hydrate_rows(rows)


def _filter_listing_rows_with_format_issues(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("format_hint") or "").strip()]


def _orchestration_dependency_caption(
    *,
    load_orchestration_queue: bool,
    load_readiness_queue: bool,
    load_readiness_evaluation: bool,
) -> str:
    if not load_orchestration_queue:
        return (
            "Orchestration queue is deferred. Enable `Load Listing Orchestration Queue (slower)` "
            "to compute orchestration statuses and hydrate the queue table."
        )
    if not load_readiness_queue:
        return "Orchestration queue is driven by readiness rows. Enable `Load eBay Readiness Queue (slower)` to populate it."
    if not load_readiness_evaluation:
        return (
            "Orchestration queue depends on evaluated readiness rows. Enable "
            "`Load Readiness Evaluation (slower)` to populate orchestration statuses."
        )
    return ""


def _category_query_seed(*, title: str, category: str = "", metal_type: str = "", sku: str = "") -> str:
    parts: list[str] = []
    for raw in [title, category, metal_type]:
        value = str(raw or "").strip()
        if value:
            parts.append(value)
    if not parts and str(sku or "").strip():
        parts.append(str(sku or "").strip())
    return " ".join(parts).strip()


def _listings_ebay_publish_scope_key(listing_id: int) -> str:
    return f"listing:{int(listing_id)}"


def _listings_build_ebay_publish_draft_payload(
    *,
    listing_id: int,
    listing_signature: str,
    state_keys: list[str],
) -> dict:
    state: dict[str, object] = {}
    for key in state_keys:
        if key in st.session_state:
            state[key] = st.session_state.get(key)
    return build_listing_draft_payload(
        state=state,
        context={
            "selected_listing_id": int(listing_id),
            "listing_signature": str(listing_signature or "").strip(),
        },
        signature=str(listing_signature or "").strip(),
    )


def _listings_apply_ebay_publish_draft_payload(payload: dict, *, state_keys: list[str]) -> None:
    parsed = extract_listing_draft_payload(payload, state_keys=state_keys)
    state = parsed.get("state")
    if not isinstance(state, dict):
        return
    allowed = set(state_keys)
    deferred_updates: dict[str, object] = {}
    for key, value in state.items():
        if str(key) in allowed:
            resolved = str(key)
            try:
                st.session_state[resolved] = value
            except StreamlitAPIException:
                deferred_updates[resolved] = value
    if deferred_updates:
        st.session_state["ebay_pub_pending_updates"] = {
            **dict(st.session_state.get("ebay_pub_pending_updates") or {}),
            **deferred_updates,
        }
        prior_flash = str(st.session_state.get("ebay_pub_draft_flash") or "").strip()
        deferred_msg = "Some draft fields were deferred and will apply on the next rerun."
        st.session_state["ebay_pub_draft_flash"] = (
            f"{prior_flash} {deferred_msg}".strip() if prior_flash else deferred_msg
        )


def _listings_ebay_publish_signature(payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _listings_apply_pending_ebay_publish_updates(*, allowed_keys: set[str]) -> None:
    pending = st.session_state.pop("ebay_pub_pending_updates", None)
    if not isinstance(pending, dict):
        return
    deferred_updates: dict[str, object] = {}
    for key, value in pending.items():
        resolved = str(key or "").strip()
        if resolved in allowed_keys:
            try:
                st.session_state[resolved] = value
            except StreamlitAPIException:
                deferred_updates[resolved] = value
    if deferred_updates:
        st.session_state["ebay_pub_pending_updates"] = {
            **dict(st.session_state.get("ebay_pub_pending_updates") or {}),
            **deferred_updates,
        }
        prior_flash = str(st.session_state.get("ebay_pub_draft_flash") or "").strip()
        deferred_msg = "Some publish updates were deferred and will apply on the next rerun."
        st.session_state["ebay_pub_draft_flash"] = (
            f"{prior_flash} {deferred_msg}".strip() if prior_flash else deferred_msg
        )


def _queue_ebay_publish_category_id_update(category_id: str) -> None:
    resolved = str(category_id or "").strip()
    if not resolved:
        return
    st.session_state["ebay_pub_last_category_id"] = resolved
    _queue_ebay_publish_updates_preserving_form(
        {"ebay_pub_category_id": resolved},
        flash=f"Applied category ID `{resolved}`.",
    )


def _queue_ebay_publish_updates(updates: dict | None, *, flash: str = "") -> None:
    if not isinstance(updates, dict):
        return
    normalized: dict[str, object] = {}
    for key, value in updates.items():
        resolved = str(key or "").strip()
        if resolved:
            normalized[resolved] = value
    if not normalized:
        return
    st.session_state["ebay_pub_pending_updates"] = {
        **dict(st.session_state.get("ebay_pub_pending_updates") or {}),
        **normalized,
    }
    if str(flash or "").strip():
        st.session_state["ebay_pub_draft_flash"] = str(flash).strip()


def _queue_ebay_publish_updates_preserving_form(
    updates: dict | None,
    *,
    flash: str = "",
    preserve_keys: list[str] | None = None,
) -> None:
    candidate = dict(updates or {})
    keys_to_preserve = list(preserve_keys or [])
    preserved: dict[str, object] = {}
    for key in keys_to_preserve:
        resolved = str(key or "").strip()
        if not resolved or resolved in candidate:
            continue
        if resolved in st.session_state:
            preserved[resolved] = st.session_state.get(resolved)
    _queue_ebay_publish_updates({**preserved, **candidate}, flash=flash)
    # Avoid one-run signature reset from clobbering preserved form values
    # after an in-form action (for example, applying default aspects).
    st.session_state["ebay_pub_skip_signature_reset_once"] = True


def _safe_session_set(key: str, value, *, only_if_missing: bool = False) -> bool:
    resolved = str(key or "").strip()
    if not resolved:
        return False
    if only_if_missing and resolved in st.session_state:
        return False
    try:
        st.session_state[resolved] = value
        return True
    except StreamlitAPIException:
        return False


def _render_ebay_preflight_card(result: dict, *, title: str = "eBay Dependency Preflight") -> None:
    payload = result if isinstance(result, dict) else {}
    blockers = list(payload.get("blockers") or [])
    warnings = list(payload.get("warnings") or [])
    checks = list(payload.get("checks") or [])
    checked_at = str(payload.get("checked_at") or "").strip()
    pass_count = int(sum(1 for row in checks if bool((row or {}).get("ok"))))
    fail_count = int(sum(1 for row in checks if not bool((row or {}).get("ok"))))
    warn_count = int(len(warnings))

    st.markdown(f"#### {title}")
    if checked_at:
        st.caption(f"Last checked: {checked_at}")
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Pass", pass_count)
    with m2:
        st.metric("Warn", warn_count)
    with m3:
        st.metric("Fail", fail_count)
    if blockers:
        st.error("Blockers: " + " | ".join(blockers[:3]))
    elif warnings:
        st.warning("Warnings: " + " | ".join(warnings[:3]))
    else:
        st.success("All dependency checks passed.")
    if checks:
        st.dataframe(
            pd.DataFrame(checks),
            use_container_width=True,
            hide_index=True,
        )


def _listing_publish_meta(listing) -> dict:
    raw = str(getattr(listing, "marketplace_details", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            publish_meta = parsed.get("ebay_publish")
            if isinstance(publish_meta, dict):
                return publish_meta
    except Exception:
        return {}
    return {}


def _listing_marketplace_details_obj(raw_details: str) -> dict:
    raw = str(raw_details or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"notes": raw}
    except Exception:
        return {"notes": raw}


def _ebay_primary_image_metadata(selected_images: list[object], primary_image_label: str) -> dict:
    primary_image_media = selected_images[0] if selected_images else None
    return {
        "primary_image_label": str(primary_image_label or "Auto").strip() or "Auto",
        "primary_image_media_id": int(getattr(primary_image_media, "id", 0) or 0)
        if primary_image_media is not None
        else 0,
        "primary_image_filename": str(getattr(primary_image_media, "original_filename", "") or "").strip()
        if primary_image_media is not None
        else "",
    }


def _is_ebay_mp4_video_media(media) -> bool:
    return is_mp4_video_media(media)


def _default_ebay_video_label(video_options: dict[str, object], preferred_media_id: int = 0) -> str:
    preferred_id = int(preferred_media_id or 0)
    if preferred_id > 0:
        for label, media in (video_options or {}).items():
            if int(getattr(media, "id", 0) or 0) == preferred_id and is_ebay_video_upload_candidate(media):
                return str(label)
    for label, media in (video_options or {}).items():
        if is_ebay_video_upload_candidate(media):
            return str(label)
    return ""


def _selected_ebay_video_warning(upload_video_to_ebay: bool, selected_video_label: str) -> str:
    if not bool(upload_video_to_ebay):
        return ""
    if str(selected_video_label or "").strip() == "None":
        return (
            "Video upload is enabled, but no video is selected. "
            "The listing will publish without an eBay video."
        )
    return ""


def _coerce_selected_ebay_video_label(
    *,
    upload_video_to_ebay: bool,
    selected_video_label: str,
    default_video_label: str,
    valid_video_labels: set[str],
) -> str:
    selected = str(selected_video_label or "").strip() or "None"
    default = str(default_video_label or "").strip()
    valid = set(valid_video_labels or {"None"})
    if selected not in valid:
        return default if default in valid else "None"
    if bool(upload_video_to_ebay) and selected == "None" and default and default in valid:
        return default
    return selected


def _verify_inventory_video_ids(
    *,
    ebay: EbayClient,
    access_token: str,
    sku: str,
    expected_video_ids: list[str],
    content_language: str = "en-US",
    max_attempts: int = 3,
    sleep_seconds: float = 1.0,
) -> dict:
    expected = [str(value or "").strip() for value in expected_video_ids if str(value or "").strip()]
    if not expected:
        return {"verified": True, "actual_video_ids": []}
    last_payload: dict = {}
    for attempt in range(1, max(1, int(max_attempts or 1)) + 1):
        payload = ebay.get_inventory_item(
            access_token=access_token,
            sku=str(sku or "").strip(),
            content_language=content_language,
        )
        last_payload = payload if isinstance(payload, dict) else {}
        product_payload = last_payload.get("product") if isinstance(last_payload.get("product"), dict) else {}
        actual = [str(value or "").strip() for value in product_payload.get("videoIds") or [] if str(value or "").strip()]
        if all(video_id in actual for video_id in expected):
            return {"verified": True, "actual_video_ids": actual, "attempts": attempt}
        if attempt < max(1, int(max_attempts or 1)):
            time.sleep(max(0.0, float(sleep_seconds or 0.0)))
    product_payload = last_payload.get("product") if isinstance(last_payload.get("product"), dict) else {}
    actual = [str(value or "").strip() for value in product_payload.get("videoIds") or [] if str(value or "").strip()]
    raise RuntimeError(
        "eBay inventory item did not retain listing videoIds after update. "
        f"expected={expected}; actual={actual or []}"
    )


def _verify_trading_listing_video_ids(
    *,
    ebay: EbayClient,
    access_token: str,
    listing_id: str,
    expected_video_ids: list[str],
    marketplace_id: str = "EBAY_US",
) -> dict:
    expected = [str(value or "").strip() for value in expected_video_ids if str(value or "").strip()]
    if not expected:
        return {"verified": True, "actual_video_ids": []}
    result = ebay.get_trading_item_video_ids(
        access_token=access_token,
        item_id=str(listing_id or "").strip(),
        marketplace_id=marketplace_id,
    )
    actual = [str(value or "").strip() for value in result.get("video_ids") or [] if str(value or "").strip()]
    if all(video_id in actual for video_id in expected):
        return {**result, "verified": True, "actual_video_ids": actual}
    raise RuntimeError(
        "Trading GetItem did not return the expected listing video ID. "
        f"listing_id={listing_id}; expected={expected}; actual={actual or []}"
    )


def _ebay_image_url_from_result(result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    direct = str(result.get("imageUrl") or "").strip()
    if direct:
        return direct
    nested = result.get("image")
    if isinstance(nested, dict):
        return str(nested.get("imageUrl") or "").strip()
    return ""


def _is_transient_ebay_media_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in [
            " 500 ",
            " 502 ",
            " 503 ",
            " 504 ",
            "internal server error",
            "service unavailable",
            "gateway",
            "timed out",
            "timeout",
            "temporarily",
        ]
    )


def _create_eps_image_with_retry(
    *,
    ebay: EbayClient,
    access_token: str,
    media,
    storage,
    max_attempts: int = 3,
) -> tuple[str, dict]:
    original_url = str(getattr(media, "s3_url", "") or "").strip()
    filename = str(getattr(media, "original_filename", "") or "image.jpg").strip() or "image.jpg"
    content_type = str(getattr(media, "content_type", "") or "image/jpeg").strip() or "image/jpeg"
    errors: list[str] = []

    try:
        image_bytes, image_content_type = _read_media_bytes(media, storage)
    except Exception as exc:
        image_bytes = None
        image_content_type = ""
        errors.append(f"media bytes unavailable: {exc}")

    if image_bytes is not None:
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                image_result = ebay.create_image_from_file(
                    access_token=access_token,
                    file_bytes=image_bytes,
                    filename=filename,
                    content_type=image_content_type or content_type,
                )
                eps_url = _ebay_image_url_from_result(image_result)
                if eps_url:
                    return eps_url, {
                        "media_asset_id": int(getattr(media, "id", 0) or 0),
                        "filename": filename,
                        "source_url": original_url,
                        "eps_url": eps_url,
                        "mode": "file_upload",
                        "attempts": attempt,
                    }
                raise RuntimeError("No imageUrl returned from eBay Media API file upload.")
            except Exception as exc:
                errors.append(f"file-upload attempt {attempt}: {exc}")
                if attempt >= max_attempts or not _is_transient_ebay_media_error(exc):
                    break
                time.sleep(0.5 * attempt)

    if original_url.startswith("https://"):
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                image_result = ebay.create_image_from_url(access_token=access_token, image_url=original_url)
                eps_url = _ebay_image_url_from_result(image_result)
                if eps_url:
                    return eps_url, {
                        "media_asset_id": int(getattr(media, "id", 0) or 0),
                        "filename": filename,
                        "source_url": original_url,
                        "eps_url": eps_url,
                        "mode": "url_import",
                        "attempts": attempt,
                    }
                raise RuntimeError("No imageUrl returned from eBay Media API URL import.")
            except Exception as exc:
                errors.append(f"url-import attempt {attempt}: {exc}")
                if attempt >= max_attempts or not _is_transient_ebay_media_error(exc):
                    break
                time.sleep(0.5 * attempt)
    else:
        errors.append("URL import unavailable because media has no public HTTPS URL.")

    raise RuntimeError("eBay EPS image hosting failed; direct/self-hosted image fallback is disabled. " + " | ".join(errors[:6]))


def _merge_ebay_publish_metadata(raw_details: str, metadata: dict) -> str:
    details_obj = _listing_marketplace_details_obj(raw_details)
    publish_meta = details_obj.get("ebay_publish")
    if not isinstance(publish_meta, dict):
        publish_meta = {}
    publish_meta.update(metadata or {})
    details_obj["ebay_publish"] = publish_meta
    return json.dumps(details_obj, indent=2)


def _normalize_store_category_names(values: object) -> list[str]:
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    else:
        raw_values = []
    names: list[str] = []
    for value in raw_values:
        clean = str(value or "").strip()
        if clean and clean not in names:
            names.append(clean)
        if len(names) >= 2:
            break
    return names


def _store_category_option_rows(repo: InventoryRepository, *, marketplace_id: str) -> list[object]:
    return list(
        repo.list_ebay_store_categories(
            environment=settings.app_env,
            marketplace_id=str(marketplace_id or "EBAY_US").strip() or "EBAY_US",
            active_only=True,
        )
    )


def _sync_run_int(row: object, primary_attr: str, fallback_attr: str = "") -> int:
    value = getattr(row, primary_attr, None)
    if value is None and fallback_attr:
        value = getattr(row, fallback_attr, None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _latest_ebay_store_category_sync_summary(repo: InventoryRepository) -> dict:
    try:
        runs = repo.list_sync_runs(provider="ebay", limit=100)
    except Exception:
        return {}
    for run in runs or []:
        if str(getattr(run, "job_name", "") or "").strip() != "ebay_store_categories_sync":
            continue
        completed_at = getattr(run, "completed_at", None) or getattr(run, "finished_at", None)
        started_at = getattr(run, "started_at", None)
        return {
            "run_id": int(getattr(run, "id", 0) or 0),
            "status": str(getattr(run, "status", "") or "").strip() or "unknown",
            "processed": _sync_run_int(run, "records_processed", "processed"),
            "updated": _sync_run_int(run, "records_updated", "updated"),
            "failed": _sync_run_int(run, "records_failed", "failed"),
            "completed_at": completed_at,
            "started_at": started_at,
        }
    return {}


def _format_ebay_store_category_sync_summary(summary: dict) -> str:
    if not summary:
        return "No eBay store category sync run has been recorded yet."
    timestamp = summary.get("completed_at") or summary.get("started_at")
    if isinstance(timestamp, datetime):
        timestamp_text = timestamp.replace(microsecond=0).isoformat(sep=" ")
    elif timestamp:
        timestamp_text = str(timestamp)
    else:
        timestamp_text = "time unavailable"
    run_id = int(summary.get("run_id") or 0)
    run_text = f"run #{run_id}" if run_id else "latest run"
    return (
        "Latest eBay store category sync: "
        f"{run_text} {str(summary.get('status') or 'unknown')} at {timestamp_text}; "
        f"processed {int(summary.get('processed') or 0)}, "
        f"updated {int(summary.get('updated') or 0)}, "
        f"failed {int(summary.get('failed') or 0)}."
    )


def _render_ebay_store_category_manager(
    repo: InventoryRepository,
    *,
    marketplace_id: str,
    actor: str,
    key_prefix: str,
) -> None:
    with st.expander("Manage eBay Store Categories", expanded=False):
        st.caption(
            "Use the full eBay store category path. eBay Inventory API offers accept up to two "
            "store category paths, for example `/Coins/Bullion/Copper`."
        )
        latest_sync = _latest_ebay_store_category_sync_summary(repo)
        st.caption(_format_ebay_store_category_sync_summary(latest_sync))
        sync_col1, sync_col2 = st.columns([1, 3])
        with sync_col1:
            sync_from_ebay = st.button("Sync From eBay Store", key=f"{key_prefix}_store_category_sync_btn")
        with sync_col2:
            st.caption(
                "Uses eBay Trading API `GetStore` to import your current store category hierarchy into the local selector."
            )
        deactivate_missing = st.checkbox(
            "Deactivate previously synced categories missing from eBay response",
            value=False,
            key=f"{key_prefix}_store_category_sync_deactivate_missing",
            help=(
                "Only categories imported from eBay by this sync tool are deactivated. "
                "Manually entered categories are left alone."
            ),
        )
        if sync_from_ebay:
            user = current_user()
            if user and not ensure_permission(user, "update", "Sync eBay Store Categories"):
                st.stop()
            try:
                result = execute_sync_job(
                    repo,
                    job_name="ebay_store_categories_sync",
                    actor=actor,
                    marketplace_id=str(marketplace_id or "EBAY_US").strip() or "EBAY_US",
                    deactivate_missing=bool(deactivate_missing),
                )
                imported = int(result.get("processed") or 0)
                missing_count = int(result.get("missing") or 0)
                deactivated_count = int(result.get("deactivated") or 0)
                stale_note = ""
                if missing_count and deactivated_count:
                    stale_note = f" Deactivated {deactivated_count} stale eBay-synced categor{'y' if deactivated_count == 1 else 'ies'}."
                elif missing_count:
                    stale_note = (
                        f" {missing_count} previously synced categor{'y is' if missing_count == 1 else 'ies are'} "
                        "missing from eBay and remain active because deactivation was not selected."
                    )
                run_note = f" Sync run #{int(result.get('run_id') or 0)} recorded."
                st.success(
                    f"Synced {imported} eBay store categor{'y' if imported == 1 else 'ies'}.{stale_note}{run_note}"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"eBay store category sync failed: {exc}")
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            category_path = st.text_input(
                "Store Category Path",
                key=f"{key_prefix}_store_category_path",
                placeholder="/Coins/Bullion/Copper",
            )
        with c2:
            external_category_id = st.text_input(
                "Store Category ID",
                key=f"{key_prefix}_store_category_external_id",
                help="Optional eBay store category id, if known.",
            )
        with c3:
            sort_order = st.number_input(
                "Sort",
                min_value=0,
                step=1,
                key=f"{key_prefix}_store_category_sort_order",
            )
        active = st.checkbox("Active", value=True, key=f"{key_prefix}_store_category_active")
        notes = st.text_input("Notes", key=f"{key_prefix}_store_category_notes")
        if st.button("Save Store Category", key=f"{key_prefix}_store_category_save_btn"):
            try:
                row = repo.upsert_ebay_store_category(
                    environment=settings.app_env,
                    marketplace_id=str(marketplace_id or "EBAY_US").strip() or "EBAY_US",
                    category_path=category_path,
                    external_category_id=external_category_id,
                    sort_order=int(sort_order or 0),
                    is_active=bool(active),
                    source="manual",
                    notes=notes,
                    actor=actor,
                )
                st.success(f"Saved store category `{row.category_path}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save store category: {exc}")
        rows = repo.list_ebay_store_categories(
            environment=settings.app_env,
            marketplace_id=str(marketplace_id or "EBAY_US").strip() or "EBAY_US",
            active_only=False,
        )
        if rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": int(getattr(row, "id", 0) or 0),
                            "path": getattr(row, "category_path", ""),
                            "store_category_id": getattr(row, "external_category_id", ""),
                            "active": bool(getattr(row, "is_active", False)),
                            "sort": int(getattr(row, "sort_order", 0) or 0),
                            "source": getattr(row, "source", ""),
                            "last_sync_status": getattr(row, "last_sync_status", ""),
                            "last_synced_at": getattr(row, "last_synced_at", ""),
                            "notes": getattr(row, "notes", ""),
                        }
                        for row in rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No eBay store categories saved yet.")


def _listing_ebay_publish_meta(listing) -> dict:
    details_obj = _listing_marketplace_details_obj(str(getattr(listing, "marketplace_details", "") or ""))
    publish_meta = details_obj.get("ebay_publish")
    return publish_meta if isinstance(publish_meta, dict) else {}


def _listing_ebay_inventory_sku(product, listing) -> str:
    publish_meta = _listing_ebay_publish_meta(listing)
    stored_inventory_sku = str(publish_meta.get("inventory_sku") or "").strip()
    if stored_inventory_sku:
        return stored_inventory_sku
    product_sku = str(getattr(product, "sku", "") or "").strip()
    legacy_offer_id = str(publish_meta.get("offer_id") or "").strip()
    legacy_external_listing_id = str(getattr(listing, "external_listing_id", "") or "").strip()
    if legacy_offer_id or legacy_external_listing_id:
        return product_sku
    return build_ebay_inventory_item_sku(
        product_sku,
        listing_id=int(getattr(listing, "id", 0) or 0),
    )


def _merge_bundle_metadata(raw_details: str, bundle_metadata: dict) -> str:
    details_obj = _listing_marketplace_details_obj(raw_details)
    details_obj["bundle"] = bundle_metadata or {"enabled": False}
    return json.dumps(details_obj, indent=2)


def _listing_is_archived(listing) -> bool:
    raw = str(getattr(listing, "marketplace_details", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    lifecycle = parsed.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return bool(lifecycle.get("archived"))


def _persist_listing_publish_error(
    repo: InventoryRepository,
    listing,
    *,
    actor: str,
    error_message: str,
    stage: str,
    context: dict | None = None,
) -> None:
    if listing is None:
        return
    details_obj: dict = {}
    raw = str(getattr(listing, "marketplace_details", "") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                details_obj = parsed
            else:
                details_obj = {"notes": raw}
        except Exception:
            details_obj = {"notes": raw}
    publish_meta = details_obj.get("ebay_publish")
    if not isinstance(publish_meta, dict):
        publish_meta = {}
    publish_meta["last_publish_error"] = str(error_message or "").strip()
    publish_meta["last_publish_error_at"] = utcnow_naive().isoformat()
    publish_meta["last_publish_error_stage"] = str(stage or "").strip()
    context_obj = context if isinstance(context, dict) else {}
    publish_meta["last_publish_error_context"] = context_obj
    inventory_sku = str(context_obj.get("inventory_sku") or "").strip()
    product_sku = str(context_obj.get("product_sku") or "").strip()
    if inventory_sku:
        publish_meta["inventory_sku"] = inventory_sku
    if product_sku:
        publish_meta["product_sku"] = product_sku
    details_obj["ebay_publish"] = publish_meta
    repo.update_listing(
        int(getattr(listing, "id", 0) or 0),
        {"marketplace_details": json.dumps(details_obj, indent=2)},
        actor=actor,
    )


def _extract_listing_details_text(listing, fallback: str = "") -> str:
    raw = str(getattr(listing, "marketplace_details", "") or "").strip()
    if not raw:
        return str(fallback or "").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(parsed, dict):
        return raw
    notes = str(parsed.get("notes") or "").strip()
    if notes:
        return notes
    listing_description = str(parsed.get("listing_description") or "").strip()
    if listing_description:
        return listing_description
    ebay_publish = parsed.get("ebay_publish") or {}
    if isinstance(ebay_publish, dict):
        published_description = str(ebay_publish.get("listing_description") or "").strip()
        if published_description:
            return published_description
    return str(fallback or "").strip()


def _product_ai_grading_description(product) -> str:
    if product is None:
        return ""
    return str(getattr(product, "ai_grading_description", "") or "").strip()


def _with_ai_grading_notes(details: str, *, grading_description: str) -> str:
    base = str(details or "").strip()
    grading = str(grading_description or "").strip()
    if not grading:
        return base
    marker = "AI Grading Notes:"
    if marker.lower() in base.lower():
        return base
    notes_block = f"{marker}\n{grading}"
    return f"{base}\n\n{notes_block}" if base else notes_block


def _ai_grading_prefill_status(*, current_value: str, default_value: str) -> str:
    current = str(current_value or "").strip()
    default = str(default_value or "").strip()
    if not default:
        return ""
    if current == default:
        return "AI Grading Description is prefilled from linked product record."
    if current:
        return "AI Grading Description has been edited from linked product default."
    return "Linked product has AI grading text available."


def _normalize_aspects_payload(raw_text: str) -> dict[str, list[str]]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in parsed.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, list):
            vals = [str(item).strip() for item in value if str(item).strip()]
        else:
            raw_val = str(value or "").strip()
            vals = [raw_val] if raw_val else []
        if vals:
            out[name] = vals
    return out


def _norm_aspect_name(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _category_aspect_table_rows(
    category_aspects: list[dict[str, object]],
    existing_aspects: dict[str, list[str]],
) -> list[dict[str, object]]:
    existing_keys = {_norm_aspect_name(key): values for key, values in (existing_aspects or {}).items()}
    rows: list[dict[str, object]] = []
    for row in normalize_ebay_category_aspect_rows(category_aspects):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        values = row.get("values") or []
        current_values = existing_keys.get(_norm_aspect_name(name)) or []
        rows.append(
            {
                "aspect": name,
                "required": "yes" if bool(row.get("required")) else "no",
                "status": "filled" if current_values else "missing",
                "usage": str(row.get("usage") or ""),
                "mode": str(row.get("mode") or ""),
                "allowed_values": ", ".join(str(value) for value in values[:8]),
            }
        )
    return rows


def _category_aspect_input_key(prefix: str, aspect_name: str) -> str:
    digest = hashlib.sha1(str(aspect_name or "").strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _condition_option_labels(condition_rows: list[dict[str, object]], current_condition: str = "") -> dict[str, str]:
    rows = condition_rows if isinstance(condition_rows, list) else []
    labels: dict[str, str] = {}
    for row in rows:
        condition = str((row or {}).get("condition") or "").strip().upper()
        if not condition:
            continue
        label = str((row or {}).get("label") or "").strip() or ebay_condition_label(condition)
        condition_id = str((row or {}).get("condition_id") or "").strip()
        labels[condition] = f"{label} ({condition})" + (f" / eBay ID {condition_id}" if condition_id else "")
    if not labels:
        labels = {condition: ebay_condition_label(condition) for condition in EBAY_DEFAULT_INVENTORY_CONDITIONS}
    current = str(current_condition or "").strip().upper()
    if current and current not in labels:
        labels[current] = f"{ebay_condition_label(current)} ({current}) - not in loaded category policy"
    return labels


def _condition_options(condition_rows: list[dict[str, object]], current_condition: str = "") -> list[str]:
    labels = _condition_option_labels(condition_rows, current_condition)
    ordered = [str((row or {}).get("condition") or "").strip().upper() for row in (condition_rows or [])]
    options = [condition for condition in ordered if condition in labels]
    if not options:
        options = [condition for condition in EBAY_DEFAULT_INVENTORY_CONDITIONS if condition in labels]
    current = str(current_condition or "").strip().upper()
    if current and current not in options:
        options.append(current)
    return options


def _is_condition_valid_for_loaded_policy(condition_rows: list[dict[str, object]], condition: str) -> bool:
    if not condition_rows:
        return True
    allowed = {str((row or {}).get("condition") or "").strip().upper() for row in condition_rows}
    return str(condition or "").strip().upper() in allowed


def _normalize_volume_pricing_tiers(
    raw_text: str,
    *,
    base_price: float = 0.0,
) -> tuple[list[dict[str, float | int]], list[str]]:
    text = str(raw_text or "").strip()
    if not text:
        return [], []
    try:
        parsed = json.loads(text)
    except Exception:
        return [], ["Volume pricing tiers must be valid JSON."]
    if not isinstance(parsed, list):
        return [], ["Volume pricing tiers must be a JSON array."]
    tiers: list[dict[str, float | int]] = []
    errors: list[str] = []
    for idx, row in enumerate(parsed, start=1):
        if not isinstance(row, dict):
            errors.append(f"Tier #{idx} must be an object with `min_qty` and `price`.")
            continue
        min_qty_raw = row.get("min_qty", row.get("qty"))
        price_raw = row.get("price", row.get("unit_price"))
        percent_raw = row.get("percent_off", row.get("discount_percent"))
        try:
            min_qty = int(float(min_qty_raw))
        except Exception:
            errors.append(f"Tier #{idx} has invalid min_qty.")
            continue
        price: float | None = None
        percent_off: float | None = None
        if price_raw is not None and str(price_raw).strip() != "":
            try:
                price = float(price_raw)
            except Exception:
                errors.append(f"Tier #{idx} has invalid price.")
                continue
        elif percent_raw is not None and str(percent_raw).strip() != "":
            try:
                percent_off = float(percent_raw)
            except Exception:
                errors.append(f"Tier #{idx} has invalid percent_off.")
                continue
            if percent_off <= 0 or percent_off >= 100:
                errors.append(f"Tier #{idx} percent_off must be > 0 and < 100.")
                continue
            if float(base_price or 0.0) <= 0:
                errors.append(
                    f"Tier #{idx} uses percent_off but base listing price is missing/invalid."
                )
                continue
            price = round(float(base_price) * (1.0 - (percent_off / 100.0)), 2)
        else:
            errors.append(f"Tier #{idx} must include either `price` or `percent_off`.")
            continue
        if min_qty < 2:
            errors.append(f"Tier #{idx} min_qty must be >= 2.")
            continue
        if float(price or 0.0) <= 0:
            errors.append(f"Tier #{idx} price must be > 0.")
            continue
        tier_row: dict[str, float | int] = {"min_qty": min_qty, "price": round(float(price), 2)}
        if percent_off is not None:
            tier_row["percent_off"] = round(float(percent_off), 2)
        tiers.append(tier_row)
    if errors:
        return [], errors
    if len(tiers) > 1:
        tiers = sorted(tiers, key=lambda row: int(row["min_qty"]))
    seen: set[int] = set()
    deduped: list[dict[str, float | int]] = []
    for row in tiers:
        qty = int(row["min_qty"])
        if qty in seen:
            errors.append(f"Duplicate min_qty tier `{qty}` is not allowed.")
            continue
        seen.add(qty)
        deduped.append(row)
    if len(deduped) > 10:
        errors.append("Volume pricing supports at most 10 tiers.")
    if errors:
        return [], errors
    return deduped, []


def _volume_pricing_description_block(tiers: list[dict[str, float | int]]) -> str:
    rows = []
    for row in tiers:
        qty = int(row.get("min_qty") or 0)
        price = float(row.get("price") or 0.0)
        percent = float(row.get("percent_off") or 0.0)
        if qty >= 2 and price > 0:
            if percent > 0:
                rows.append(f"<li>Buy {qty}+ save {percent:g}% (${price:,.2f} each)</li>")
            else:
                rows.append(f"<li>Buy {qty}+ for ${price:,.2f} each</li>")
    if not rows:
        return ""
    return (
        "<h3>Volume Discount Pricing</h3>"
        "<p>Quantity discounts available:</p>"
        "<ul>"
        + "".join(rows)
        + "</ul>"
    )


def _volume_pricing_json_from_discount_controls(
    *,
    buy2_percent: float = 0.0,
    buy3_percent: float = 0.0,
    buy4_percent: float = 0.0,
) -> str:
    tiers: list[dict[str, float | int]] = []
    for qty, percent in (
        (2, float(buy2_percent or 0.0)),
        (3, float(buy3_percent or 0.0)),
        (4, float(buy4_percent or 0.0)),
    ):
        if percent > 0:
            tiers.append({"min_qty": qty, "percent_off": round(percent, 2)})
    return json.dumps(tiers, indent=2) if tiers else ""


def _volume_pricing_discount_controls_from_tiers(
    tiers: list[dict[str, float | int]] | None,
) -> tuple[float, float, float]:
    buy2 = 0.0
    buy3 = 0.0
    buy4 = 0.0
    for row in tiers or []:
        try:
            qty = int(float((row or {}).get("min_qty") or 0))
            percent = float((row or {}).get("percent_off") or 0.0)
        except Exception:
            continue
        if percent <= 0:
            continue
        if qty == 2:
            buy2 = percent
        elif qty == 3:
            buy3 = percent
        elif qty == 4:
            buy4 = percent
    return (buy2, buy3, buy4)


def _external_listing_id_owner(
    repo: InventoryRepository,
    *,
    marketplace: str,
    external_listing_id: str,
    exclude_listing_id: int,
    listings: list[object] | None = None,
    owner_by_market_and_external_id: dict[tuple[str, str], int] | None = None,
) -> int | None:
    ext_id = str(external_listing_id or "").strip()
    if not ext_id:
        return None
    target_market = str(marketplace or "").strip().lower()
    normalized_key = (target_market, ext_id)
    if owner_by_market_and_external_id is not None:
        owner_id = owner_by_market_and_external_id.get(normalized_key)
        if owner_id is None:
            return None
        return None if int(owner_id) == int(exclude_listing_id) else int(owner_id)
    listing_rows = listings if listings is not None else list(repo.list_listings())
    for row in listing_rows:
        if int(getattr(row, "id", 0) or 0) == int(exclude_listing_id):
            continue
        if str(getattr(row, "marketplace", "") or "").strip().lower() != target_market:
            continue
        if str(getattr(row, "external_listing_id", "") or "").strip() == ext_id:
            return int(getattr(row, "id", 0) or 0)
    return None


def _ebay_create_publish_defaults(repo: InventoryRepository) -> dict:
    default_format_type = str(
        st.session_state.get("ebay_workspace_store_listing_format_input")
        or get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE")
        or "FIXED_PRICE"
    ).strip().upper()
    if default_format_type not in {"FIXED_PRICE", "AUCTION"}:
        default_format_type = "FIXED_PRICE"
    default_duration = (
        "GTC"
        if default_format_type == "FIXED_PRICE"
        else str(
            st.session_state.get("ebay_workspace_store_auction_duration_input")
            or get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_5")
            or "DAYS_5"
        ).strip().upper()
    )
    return {
        "format_type": default_format_type,
        "listing_duration": default_duration,
        "category_id": str(
            st.session_state.get("ebay_workspace_store_category_id_input")
            or get_runtime_str(repo, "ebay_category_id", "")
            or ""
        ).strip(),
        "merchant_location_key": str(
            st.session_state.get("ebay_workspace_store_merchant_location_key_input")
            or get_runtime_str(repo, "ebay_merchant_location_key", settings.ebay_merchant_location_key)
            or ""
        ).strip(),
        "payment_policy_id": str(
            st.session_state.get("ebay_workspace_store_payment_policy_id_input")
            or get_runtime_str(repo, "ebay_payment_policy_id", settings.ebay_payment_policy_id)
            or ""
        ).strip(),
        "fulfillment_policy_id": str(
            st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input")
            or get_runtime_str(repo, "ebay_fulfillment_policy_id", settings.ebay_fulfillment_policy_id)
            or ""
        ).strip(),
        "return_policy_id": str(
            st.session_state.get("ebay_workspace_store_return_policy_id_input")
            or get_runtime_str(repo, "ebay_return_policy_id", settings.ebay_return_policy_id)
            or ""
        ).strip(),
        "marketplace_id": str(
            st.session_state.get("ebay_workspace_store_marketplace_id_input")
            or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
            or settings.ebay_marketplace_id
        ).strip(),
        "currency": str(
            st.session_state.get("ebay_workspace_store_currency_input")
            or get_runtime_str(repo, "ebay_currency", settings.ebay_currency)
            or settings.ebay_currency
        ).strip(),
        "content_language": str(
            st.session_state.get("ebay_workspace_store_content_language_input")
            or get_runtime_str(repo, "ebay_content_language", settings.ebay_content_language)
            or settings.ebay_content_language
        ).strip(),
        "best_offer_enabled": bool(
            st.session_state.get("ebay_workspace_store_best_offer_enabled_input")
            if "ebay_workspace_store_best_offer_enabled_input" in st.session_state
            else get_runtime_bool(repo, "ebay_best_offer_default", False)
        ),
        "auction_start_price": float(
            st.session_state.get("ebay_workspace_store_auction_start_input")
            or _to_float(get_runtime_str(repo, "ebay_auction_start_default", "1.0"), 1.0)
            or 1.0
        ),
        "auction_reserve_price": float(
            st.session_state.get("ebay_workspace_store_auction_reserve_input")
            or _to_float(get_runtime_str(repo, "ebay_auction_reserve_default", "0.0"), 0.0)
            or 0.0
        ),
        "auction_buy_now_price": float(
            st.session_state.get("ebay_workspace_store_auction_buy_now_input")
            or _to_float(get_runtime_str(repo, "ebay_auction_buy_now_default", "0.0"), 0.0)
            or 0.0
        ),
        "shipping_service": str(
            st.session_state.get("ebay_workspace_store_shipping_service_input")
            or get_runtime_str(repo, "ebay_shipping_service_default", "USPS Ground Advantage")
            or "USPS Ground Advantage"
        ).strip(),
        "handling_days": int(
            st.session_state.get("ebay_workspace_store_handling_days_input")
            or _to_float(get_runtime_str(repo, "ebay_handling_days_default", "3"), 3.0)
            or 3
        ),
        "shipping_cost": float(
            st.session_state.get("ebay_workspace_store_shipping_cost_input")
            or _to_float(get_runtime_str(repo, "ebay_shipping_cost_default", "0.0"), 0.0)
            or 0.0
        ),
        "package_weight_oz": float(
            st.session_state.get("ebay_workspace_store_package_weight_oz_input")
            or _to_float(get_runtime_str(repo, "ebay_package_weight_oz_default", "0.0"), 0.0)
            or 0.0
        ),
    }


def _merge_ebay_publish_defaults_into_details(
    raw_details: str,
    publish_defaults: dict,
) -> str:
    base = str(raw_details or "").strip()
    details_obj: dict = {}
    if base:
        try:
            parsed = json.loads(base)
            if isinstance(parsed, dict):
                details_obj = parsed
            else:
                details_obj = {"notes": base}
        except Exception:
            details_obj = {"notes": base}
    existing_publish = details_obj.get("ebay_publish") or {}
    if not isinstance(existing_publish, dict):
        existing_publish = {}
    merged_publish = {**existing_publish, **(publish_defaults or {})}
    details_obj["ebay_publish"] = merged_publish
    return json.dumps(details_obj, indent=2)


def _validate_ebay_create_publish_defaults(
    *,
    publish_defaults: dict,
    listing_price: float,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    fmt = str(publish_defaults.get("format_type") or "FIXED_PRICE").strip().upper()
    duration = str(publish_defaults.get("listing_duration") or "").strip().upper()
    auction_start = _to_float(publish_defaults.get("auction_start_price"), 0.0)
    auction_reserve = _to_float(publish_defaults.get("auction_reserve_price"), 0.0)
    auction_buy_now = _to_float(publish_defaults.get("auction_buy_now_price"), 0.0)

    if fmt not in {"FIXED_PRICE", "AUCTION"}:
        errors.append("Format must be FIXED_PRICE or AUCTION.")
        return errors, warnings

    if fmt == "FIXED_PRICE":
        if float(listing_price or 0) <= 0:
            errors.append("Buy It Now price must be > 0 for FIXED_PRICE.")
        if duration and duration != "GTC":
            warnings.append("Fixed-price duration is normally GTC; verify this override.")
    else:
        if auction_start <= 0:
            errors.append("Auction start price must be > 0.")
        if duration not in {"DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"}:
            errors.append("Auction duration must be DAYS_1, DAYS_3, DAYS_5, DAYS_7, or DAYS_10.")
        if auction_reserve > 0 and auction_reserve < auction_start:
            errors.append("Auction reserve price cannot be lower than start price.")
        if auction_buy_now > 0 and auction_buy_now < auction_start:
            errors.append("Auction Buy It Now price cannot be lower than start price.")
        if auction_buy_now > 0 and auction_reserve > 0 and auction_buy_now < auction_reserve:
            warnings.append("Auction Buy It Now is below reserve price; verify intended strategy.")
    return errors, warnings


def _load_workspace_store_profiles(repo: InventoryRepository) -> dict[str, dict]:
    raw = get_runtime_str(repo, "ebay_workspace_store_profiles_json", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for key, payload in parsed.items():
        if isinstance(payload, dict):
            out[str(key)] = payload
    return out


def _apply_store_profile_to_listing_create(profile_payload: dict) -> None:
    st.session_state["create_listing_ebay_format"] = str(
        profile_payload.get("listing_format") or "FIXED_PRICE"
    ).strip().upper()
    st.session_state["create_listing_ebay_duration"] = str(
        profile_payload.get("auction_duration")
        or ("GTC" if st.session_state["create_listing_ebay_format"] == "FIXED_PRICE" else "DAYS_5")
    ).strip().upper()
    st.session_state["create_listing_ebay_best_offer_enabled"] = bool(profile_payload.get("best_offer_enabled"))
    st.session_state["create_listing_ebay_category_id"] = str(profile_payload.get("category_id") or "").strip()
    st.session_state["create_listing_ebay_merchant_location_key"] = str(
        profile_payload.get("merchant_location_key") or ""
    ).strip()
    st.session_state["create_listing_ebay_payment_policy_id"] = str(
        profile_payload.get("payment_policy_id") or ""
    ).strip()
    st.session_state["create_listing_ebay_fulfillment_policy_id"] = str(
        profile_payload.get("fulfillment_policy_id") or ""
    ).strip()
    st.session_state["create_listing_ebay_return_policy_id"] = str(
        profile_payload.get("return_policy_id") or ""
    ).strip()
    st.session_state["create_listing_ebay_marketplace_id"] = str(profile_payload.get("marketplace_id") or "").strip()
    st.session_state["create_listing_ebay_currency"] = str(profile_payload.get("currency") or "").strip()
    st.session_state["create_listing_ebay_content_language"] = str(
        profile_payload.get("content_language") or ""
    ).strip()
    st.session_state["create_listing_ebay_auction_start_price"] = float(profile_payload.get("auction_start_default") or 1.0)
    st.session_state["create_listing_ebay_auction_reserve_price"] = float(profile_payload.get("auction_reserve_default") or 0.0)
    st.session_state["create_listing_ebay_auction_buy_now_price"] = float(profile_payload.get("auction_buy_now_default") or 0.0)
    if st.session_state["create_listing_ebay_marketplace_id"]:
        st.session_state["create_listing_marketplace"] = str(
            st.session_state["create_listing_ebay_marketplace_id"]
        ).strip().lower()


def _try_extract_json_object(raw_text: str) -> dict:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        snippet = text[first : last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _extract_offer_id_from_offer_exists_error(exc: Exception) -> str:
    message = str(exc or "")
    match = re.search(r'"offerId"\s*,\s*"value"\s*:\s*"([^"]+)"', message, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip()
    match = re.search(r"offerId[=:\s]+([A-Za-z0-9\-]+)", message, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip()
    return ""


def _resolve_existing_offer_id_for_sku(
    *,
    ebay: EbayClient,
    access_token: str,
    sku: str,
    marketplace_id: str,
    format_type: str,
) -> str:
    try:
        payload = ebay.get_offers(access_token=access_token, sku=sku)
    except Exception:
        return ""
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("offers") or payload.get("offerSummaries") or []
    if not isinstance(rows, list):
        return ""
    wanted_marketplace = str(marketplace_id or "").strip().upper()
    wanted_format = str(format_type or "").strip().upper()
    for row in rows:
        if not isinstance(row, dict):
            continue
        offer_id = str(row.get("offerId") or "").strip()
        if not offer_id:
            continue
        row_marketplace = str(row.get("marketplaceId") or "").strip().upper()
        row_format = str(row.get("format") or "").strip().upper()
        if wanted_marketplace and row_marketplace and row_marketplace != wanted_marketplace:
            continue
        if wanted_format and row_format and row_format != wanted_format:
            continue
        return offer_id
    return ""


def _create_or_update_offer_with_duplicate_recovery(
    *,
    ebay: EbayClient,
    access_token: str,
    payload: dict,
    content_language: str,
    sku: str,
    marketplace_id: str,
    format_type: str,
) -> tuple[str, bool]:
    try:
        offer_result = ebay.create_offer(
            access_token=access_token,
            payload=payload,
            content_language=content_language,
        )
        offer_id = str((offer_result or {}).get("offerId") or "").strip()
        if not offer_id:
            raise RuntimeError(f"eBay createOffer did not return offerId. payload={offer_result}")
        return offer_id, False
    except Exception as exc:
        raw = str(exc or "")
        is_duplicate = (
            "Offer entity already exists" in raw
            or '"errorId":25002' in raw
            or "errorId\":25002" in raw
        )
        if not is_duplicate:
            raise
        recovered_offer_id = _extract_offer_id_from_offer_exists_error(exc)
        if not recovered_offer_id:
            recovered_offer_id = _resolve_existing_offer_id_for_sku(
                ebay=ebay,
                access_token=access_token,
                sku=sku,
                marketplace_id=marketplace_id,
                format_type=format_type,
            )
        if not recovered_offer_id:
            raise RuntimeError(
                "eBay reports existing offer, but no offerId could be resolved for this SKU."
            ) from exc
        ebay.update_offer(
            access_token=access_token,
            offer_id=recovered_offer_id,
            payload=payload,
            content_language=content_language,
        )
        return recovered_offer_id, True


def _build_ebay_offer_payload(
    *,
    sku: str,
    marketplace_id: str,
    format_type: str,
    available_quantity: int,
    category_id: str,
    merchant_location_key: str,
    listing_description: str,
    listing_duration: str,
    payment_policy_id: str,
    fulfillment_policy_id: str,
    return_policy_id: str,
    currency: str,
    fixed_price: float,
    best_offer_enabled: bool,
    best_offer_auto_accept: float,
    best_offer_minimum: float,
    auction_start_price: float,
    auction_reserve_price: float,
    auction_buy_now_price: float,
    store_category_names: list[str] | None = None,
) -> dict:
    fmt = str(format_type or "FIXED_PRICE").strip().upper()
    if fmt not in {"FIXED_PRICE", "AUCTION"}:
        fmt = "FIXED_PRICE"

    payload: dict[str, object] = {
        "sku": str(sku or "").strip(),
        "marketplaceId": str(marketplace_id or "").strip(),
        "format": fmt,
        "categoryId": str(category_id or "").strip(),
        "merchantLocationKey": str(merchant_location_key or "").strip(),
        "listingDescription": str(listing_description or "").strip(),
        "listingDuration": str(listing_duration or "").strip().upper(),
        "listingPolicies": {
            "paymentPolicyId": str(payment_policy_id or "").strip(),
            "fulfillmentPolicyId": str(fulfillment_policy_id or "").strip(),
            "returnPolicyId": str(return_policy_id or "").strip(),
        },
        "pricingSummary": {},
    }
    if fmt != "AUCTION":
        payload["availableQuantity"] = max(1, int(available_quantity or 1))
    store_paths = [
        str(path or "").strip()
        for path in (store_category_names or [])
        if str(path or "").strip()
    ][:2]
    if store_paths:
        payload["storeCategoryNames"] = store_paths

    pricing_summary = payload["pricingSummary"]
    if not isinstance(pricing_summary, dict):
        pricing_summary = {}
        payload["pricingSummary"] = pricing_summary

    currency_value = str(currency or "").strip()
    if fmt == "FIXED_PRICE":
        pricing_summary["price"] = {
            "value": str(round(float(fixed_price or 0.0), 2)),
            "currency": currency_value,
        }
        listing_policies = payload["listingPolicies"]
        if isinstance(listing_policies, dict):
            if bool(best_offer_enabled):
                best_offer_terms: dict[str, object] = {"bestOfferEnabled": True}
                if float(best_offer_auto_accept or 0.0) > 0:
                    best_offer_terms["autoAcceptPrice"] = {
                        "value": str(round(float(best_offer_auto_accept or 0.0), 2)),
                        "currency": currency_value,
                    }
                if float(best_offer_minimum or 0.0) > 0:
                    best_offer_terms["autoDeclinePrice"] = {
                        "value": str(round(float(best_offer_minimum or 0.0), 2)),
                        "currency": currency_value,
                    }
                listing_policies["bestOfferTerms"] = best_offer_terms
            else:
                listing_policies["bestOfferTerms"] = {"bestOfferEnabled": False}
    else:
        pricing_summary["auctionStartPrice"] = {
            "value": str(round(float(auction_start_price or 0.0), 2)),
            "currency": currency_value,
        }
        if float(auction_reserve_price or 0.0) > 0:
            pricing_summary["auctionReservePrice"] = {
                "value": str(round(float(auction_reserve_price or 0.0), 2)),
                "currency": currency_value,
            }
        if float(auction_buy_now_price or 0.0) > 0:
            pricing_summary["price"] = {
                "value": str(round(float(auction_buy_now_price or 0.0), 2)),
                "currency": currency_value,
            }
    return payload


def _resolve_ai_listing_details(payload: dict, *, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return str(fallback or "").strip()
    for key in ("suggested_details", "suggested_description", "description", "details"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    marketplace_details = str(payload.get("suggested_marketplace_details") or "").strip()
    normalized = marketplace_details.lower().strip(" .,:;!?")
    if normalized in {"ebay", "ebay.com", "marketplace", "online marketplace"}:
        return str(fallback or "").strip()
    if marketplace_details and len(marketplace_details) >= 12:
        return marketplace_details
    return str(fallback or "").strip()


def _is_weak_listing_details(text: str, *, policy=None) -> bool:
    return _is_weak_details_shared(text, policy=policy)


def _build_fallback_ebay_listing_details(
    *,
    title: str = "",
    existing_description: str = "",
    marketplace: str = "ebay",
) -> str:
    listing_title = str(title or "").strip() or "Item Listing"
    base_description = str(existing_description or "").strip()
    return (
        f"{listing_title}\n\n"
        f"This {marketplace} listing is written to be clear, accurate, and buyer-friendly. "
        "You will receive the exact item shown in the photos.\n\n"
        "Description:\n"
        f"{base_description or 'See photos for condition and design details.'}\n\n"
        "Condition & notes:\n"
        "- Normal handling/storage wear may be present on pre-owned collectibles.\n"
        "- Photos are part of the listing description and show the actual item.\n"
        "- Please review all images and ask questions before purchase.\n\n"
        "Shipping & service:\n"
        "- Carefully packed and shipped promptly.\n"
        "- Combined shipping may be available when purchasing multiple items."
    ).strip()


def _maybe_add_package_data(
    payload: dict,
    product: Product,
    *,
    weight_oz: float | None = None,
    length_in: float | None = None,
    width_in: float | None = None,
    height_in: float | None = None,
) -> None:
    weight = _to_float(weight_oz if weight_oz is not None else product.package_weight_oz, 0.0)
    length = _to_float(length_in if length_in is not None else product.package_length_in, 0.0)
    width = _to_float(width_in if width_in is not None else product.package_width_in, 0.0)
    height = _to_float(height_in if height_in is not None else product.package_height_in, 0.0)

    if weight <= 0 and (length <= 0 or width <= 0 or height <= 0):
        return

    package: dict = {}
    if weight > 0:
        package["weight"] = {"value": weight, "unit": "OUNCE"}
    if length > 0 and width > 0 and height > 0:
        package["dimensions"] = {
            "length": length,
            "width": width,
            "height": height,
            "unit": "INCH",
        }
    if package:
        payload["packageWeightAndSize"] = package


def _read_media_bytes(media, storage: MediaStorageService) -> tuple[bytes, str]:
    media_bytes, content_type, error = load_media_bytes(media, storage=storage)
    if media_bytes is not None:
        return media_bytes, content_type or getattr(media, "content_type", None) or "application/octet-stream"
    raise RuntimeError(error or "Media file bytes could not be loaded from storage or URL.")


def _is_ebay_inventory_internal_error(exc: Exception) -> bool:
    text = str(exc or "")
    lowered = text.lower()
    return (
        "errorid\":25001" in lowered
        or "core inventory service internal error" in lowered
        or ("api_inventory" in lowered and "system error" in lowered)
    )


def _create_or_replace_inventory_item_with_fallback(
    *,
    ebay: EbayClient,
    access_token: str,
    sku: str,
    payload: dict,
    content_language: str,
    preserve_video_ids: bool = False,
) -> tuple[bool, str]:
    try:
        ebay.create_or_replace_inventory_item(
            access_token=access_token,
            sku=sku,
            payload=payload,
            content_language=content_language,
        )
        return False, ""
    except Exception as exc:
        if not _is_ebay_inventory_internal_error(exc):
            raise
        fallback_payload = dict(payload)
        if isinstance(fallback_payload.get("product"), dict):
            fallback_product = dict(fallback_payload["product"])
            if not preserve_video_ids:
                # Remove video IDs on fallback because inventory service can 500 on mixed media states.
                fallback_product.pop("videoIds", None)
            fallback_payload["product"] = fallback_product
        fallback_payload.pop("packageWeightAndSize", None)
        time.sleep(1.0)
        ebay.create_or_replace_inventory_item(
            access_token=access_token,
            sku=sku,
            payload=fallback_payload,
            content_language=content_language,
        )
        return True, str(exc)


def _render_template_placeholders(value: str, product: Product | None) -> str:
    text = (value or "").strip()
    if not text or product is None:
        return text
    replacements = {
        "{{sku}}": str(product.sku or "").strip(),
        "{{title}}": str(product.title or "").strip(),
        "{{category}}": str(product.category or "").strip(),
        "{{metal_type}}": str(product.metal_type or "").strip(),
        "{{weight_oz}}": str(product.weight_oz or ""),
    }
    out = text
    for token, token_value in replacements.items():
        out = out.replace(token, token_value)
    return out


def _listing_html_block_library() -> dict[str, str]:
    return {
        "Golden Stackers Header": (
            "<div>"
            "<h2>Golden Stackers LLC</h2>"
            "<p><strong>goldenstackers.com</strong> | sales@goldenstackers.com | 720-253-2354</p>"
            "<hr/>"
            "</div>"
        ),
        "Condition & Packaging": (
            "<h3>Condition & Packaging</h3>"
            "<ul>"
            "<li>Item: {{title}}</li>"
            "<li>SKU: {{sku}}</li>"
            "<li>Category: {{category}}</li>"
            "<li>Metal/Material: {{metal_type}}</li>"
            "<li>Weight: {{weight_oz}} oz</li>"
            "</ul>"
        ),
        "Shipping Policy": (
            "<h3>Shipping</h3>"
            "<p>Ships fast from Golden, Colorado. We pack securely and provide tracking on every order.</p>"
        ),
        "Returns Policy": (
            "<h3>Returns</h3>"
            "<p>Please review listing specifics before purchase. Contact us with any issue and we will make it right.</p>"
        ),
        "Authenticity Note": (
            "<h3>Authenticity</h3>"
            "<p>All items are photographed/described in good faith. See listing photos for exact item details.</p>"
        ),
    }


LISTING_HTML_BLOCKS_RUNTIME_KEY = "listing_html_blocks_json"


def _load_custom_listing_html_blocks(repo: InventoryRepository) -> dict[str, str]:
    raw = str(get_runtime_str(repo, LISTING_HTML_BLOCKS_RUNTIME_KEY, "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in parsed.items():
        block_name = str(key or "").strip()
        block_html = str(value or "").strip()
        if block_name and block_html:
            result[block_name] = block_html
    return result


def _save_custom_listing_html_blocks(repo: InventoryRepository, *, actor: str, blocks: dict[str, str]) -> None:
    payload = {str(k).strip(): str(v).strip() for k, v in (blocks or {}).items() if str(k).strip() and str(v).strip()}
    repo.upsert_runtime_setting(
        key=LISTING_HTML_BLOCKS_RUNTIME_KEY,
        value=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        value_type="json",
        environment=settings.app_env,
        description="Custom reusable HTML blocks for listing templates workspace.",
        is_active=True,
        actor=actor,
    )


def _merged_listing_html_block_library(repo: InventoryRepository) -> tuple[dict[str, str], dict[str, str]]:
    defaults = _listing_html_block_library()
    custom = _load_custom_listing_html_blocks(repo)
    merged = dict(defaults)
    merged.update(custom)
    return merged, custom


def _starter_listing_templates() -> list[dict]:
    lib = _listing_html_block_library()
    core = "\n\n".join(
        [
            lib["Golden Stackers Header"],
            lib["Condition & Packaging"],
            lib["Shipping Policy"],
            lib["Returns Policy"],
            lib["Authenticity Note"],
        ]
    )
    return [
        {
            "name": "GS eBay Branded",
            "marketplace": "ebay",
            "title": "{{title}} | {{sku}} | Golden Stackers",
            "details": core,
            "price_default": 0.0,
            "qty_default": 1,
            "status_default": "draft",
            "is_shared": True,
            "is_default": True,
        },
        {
            "name": "GS Craigslist Branded",
            "marketplace": "craigslist",
            "title": "{{title}} - {{sku}}",
            "details": core,
            "price_default": 0.0,
            "qty_default": 1,
            "status_default": "draft",
            "is_shared": True,
            "is_default": False,
        },
        {
            "name": "GS Facebook Branded",
            "marketplace": "facebook",
            "title": "{{title}} - {{sku}}",
            "details": core,
            "price_default": 0.0,
            "qty_default": 1,
            "status_default": "draft",
            "is_shared": True,
            "is_default": False,
        },
        {
            "name": "GS Whatnot Branded",
            "marketplace": "whatnot",
            "title": "{{title}} | {{sku}}",
            "details": core,
            "price_default": 0.0,
            "qty_default": 1,
            "status_default": "draft",
            "is_shared": True,
            "is_default": False,
        },
    ]


def render_ebay_template_workspace(repo: InventoryRepository) -> None:
    user = current_user()
    if not user:
        st.warning("Sign in required.")
        return

    st.markdown("## eBay Templates")
    st.caption("Manage reusable listing title/details templates outside of day-to-day listing operations.")
    st.page_link("pages/03_Listings.py", label="Open Listings")

    template_rows = []
    try:
        template_rows = repo.list_ebay_listing_template_profiles(
            environment=settings.app_env,
            username=user.username,
            include_shared=True,
            active_only=False,
        )
    except Exception:
        template_rows = []

    if template_rows:
        st.markdown("### Current Templates")
        table_rows: list[dict] = []
        for row in template_rows:
            table_rows.append(
                {
                    "id": int(row.id),
                    "name": str(row.name or ""),
                    "marketplace": str(row.marketplace or ""),
                    "owner": str(row.username or ""),
                    "shared": bool(row.is_shared),
                    "default": bool(row.is_default),
                    "active": bool(row.is_active),
                    "status_default": str(row.listing_status_default or "draft"),
                    "price_default": float(row.listing_price_default or 0.0),
                    "qty_default": int(row.quantity_default or 1),
                    "updated_at": iso_or_none(row.updated_at),
                }
            )
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No eBay listing templates found yet.")

    if template_rows:
        st.markdown("### Edit Existing Template")
        edit_lookup: dict[str, object] = {}
        for row in template_rows:
            owner = "Shared" if bool(row.is_shared) else "Mine"
            active = "active" if bool(row.is_active) else "inactive"
            label = f"#{int(row.id)} | {str(row.name or '').strip()} | {owner} | {active}"
            edit_lookup[label] = row
        selected_edit_label = st.selectbox(
            "Template To Edit",
            options=list(edit_lookup.keys()),
            key="ebay_template_edit_select",
        )
        selected_edit_row = edit_lookup.get(selected_edit_label)
        if selected_edit_row is not None:
            edit_signature = (
                f"{int(selected_edit_row.id)}:"
                f"{iso_or_none(selected_edit_row.updated_at)}:"
                f"{int(selected_edit_row.quantity_default or 1)}:"
                f"{int(bool(selected_edit_row.is_active))}"
            )
            if str(st.session_state.get("ebay_template_edit_signature") or "") != edit_signature:
                st.session_state["ebay_template_edit_name"] = str(selected_edit_row.name or "").strip()
                st.session_state["ebay_template_edit_marketplace"] = str(
                    selected_edit_row.marketplace or "ebay"
                ).strip().lower()
                st.session_state["ebay_template_edit_status_default"] = str(
                    selected_edit_row.listing_status_default or "draft"
                ).strip().lower()
                st.session_state["ebay_template_edit_price_default"] = float(
                    selected_edit_row.listing_price_default or 0.0
                )
                st.session_state["ebay_template_edit_qty_default"] = int(selected_edit_row.quantity_default or 1)
                st.session_state["ebay_template_edit_title"] = str(
                    selected_edit_row.listing_title_template or ""
                ).strip()
                st.session_state["ebay_template_edit_details"] = str(
                    selected_edit_row.marketplace_details_template or ""
                ).strip()
                st.session_state["ebay_template_edit_is_shared"] = bool(selected_edit_row.is_shared)
                st.session_state["ebay_template_edit_is_default"] = bool(selected_edit_row.is_default)
                st.session_state["ebay_template_edit_is_active"] = bool(selected_edit_row.is_active)
                st.session_state["ebay_template_edit_signature"] = edit_signature

            with st.form("edit_ebay_listing_template_form"):
                e1, e2, e3 = st.columns(3)
                with e1:
                    edit_name = st.text_input("Template Name", key="ebay_template_edit_name")
                with e2:
                    edit_marketplace = st.selectbox(
                        "Marketplace",
                        MARKETPLACES,
                        index=(
                            MARKETPLACES.index(st.session_state.get("ebay_template_edit_marketplace"))
                            if st.session_state.get("ebay_template_edit_marketplace") in MARKETPLACES
                            else MARKETPLACES.index("ebay")
                        ),
                        key="ebay_template_edit_marketplace",
                    )
                with e3:
                    edit_status_default = st.selectbox(
                        "Default Listing Status",
                        ["draft", "active", "ended", "sold"],
                        index=(
                            ["draft", "active", "ended", "sold"].index(
                                st.session_state.get("ebay_template_edit_status_default")
                            )
                            if st.session_state.get("ebay_template_edit_status_default") in {"draft", "active", "ended", "sold"}
                            else 0
                        ),
                        key="ebay_template_edit_status_default",
                    )
                e4, e5 = st.columns(2)
                with e4:
                    edit_price_default = st.number_input(
                        "Default Price",
                        min_value=0.0,
                        step=0.01,
                        key="ebay_template_edit_price_default",
                    )
                with e5:
                    edit_qty_default = st.number_input(
                        "Default Quantity",
                        min_value=1,
                        step=1,
                        key="ebay_template_edit_qty_default",
                    )
                edit_title = st.text_input(
                    "Listing Title Template",
                    key="ebay_template_edit_title",
                    help="Supports placeholders like {{sku}}, {{title}}, {{category}}, {{metal_type}}, {{weight_oz}}",
                )
                edit_details = st.text_area(
                    "Marketplace Details / HTML Template",
                    key="ebay_template_edit_details",
                    height=180,
                    help="Supports placeholders like {{sku}}, {{title}}, {{category}}, {{metal_type}}, {{weight_oz}}",
                )
                e6, e7, e8 = st.columns(3)
                with e6:
                    edit_is_shared = st.checkbox("Team-shared", key="ebay_template_edit_is_shared")
                with e7:
                    edit_is_default = st.checkbox("Set as default", key="ebay_template_edit_is_default")
                with e8:
                    edit_is_active = st.checkbox("Active", key="ebay_template_edit_is_active")
                edit_save = st.form_submit_button("Save Template Changes")
            if edit_save:
                if not ensure_permission(user, "update", "Edit eBay Listing Template"):
                    st.stop()
                try:
                    repo.update_ebay_listing_template_profile(
                        int(selected_edit_row.id),
                        {
                            "name": str(edit_name or "").strip(),
                            "marketplace": str(edit_marketplace or "ebay").strip().lower(),
                            "listing_status_default": str(edit_status_default or "draft").strip().lower(),
                            "listing_price_default": to_decimal(edit_price_default),
                            "quantity_default": int(edit_qty_default or 1),
                            "listing_title_template": str(edit_title or "").strip(),
                            "marketplace_details_template": str(edit_details or "").strip(),
                            "is_shared": bool(edit_is_shared),
                            "is_default": bool(edit_is_default),
                            "is_active": bool(edit_is_active),
                        },
                        actor=user.username,
                    )
                    st.success("Template updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    with st.expander("Reusable Branded HTML Blocks", expanded=False):
        st.caption("Insert and manage reusable Golden Stackers HTML blocks for templates.")
        block_library, custom_blocks = _merged_listing_html_block_library(repo)
        selected_block_name = st.selectbox(
            "Block",
            options=list(block_library.keys()),
            key="ebay_templates_html_block_select",
        )
        selected_block_html = str(block_library.get(selected_block_name) or "").strip()
        is_custom_block = selected_block_name in custom_blocks
        st.caption("Edit Block HTML (saves as custom runtime block; can override built-in names).")
        block_name_edit = st.text_input(
            "Block Name",
            value=selected_block_name,
            key="ebay_templates_html_block_name_edit",
        ).strip()
        block_html_edit = st.text_area(
            "Block HTML",
            value=selected_block_html,
            key="ebay_templates_html_block_html_edit",
            height=180,
        )
        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("Save Block", key="ebay_templates_save_block_btn"):
                if not ensure_permission(user, "update", "Save Reusable HTML Block"):
                    st.stop()
                if not block_name_edit:
                    st.warning("Block name is required.")
                elif not block_html_edit.strip():
                    st.warning("Block HTML is required.")
                else:
                    next_blocks = dict(custom_blocks)
                    next_blocks[block_name_edit] = block_html_edit.strip()
                    try:
                        _save_custom_listing_html_blocks(repo, actor=user.username, blocks=next_blocks)
                        st.success(f"Saved block `{block_name_edit}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        with b2:
            if st.button("Delete Custom Block", key="ebay_templates_delete_block_btn"):
                if not ensure_permission(user, "delete", "Delete Reusable HTML Block"):
                    st.stop()
                if not is_custom_block:
                    st.warning("Only custom blocks can be deleted. Built-in starter blocks are read-only.")
                else:
                    next_blocks = dict(custom_blocks)
                    next_blocks.pop(selected_block_name, None)
                    try:
                        _save_custom_listing_html_blocks(repo, actor=user.username, blocks=next_blocks)
                        st.success(f"Deleted custom block `{selected_block_name}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        with b3:
            st.caption(f"Block type: {'Custom' if is_custom_block else 'Built-in'}")
        if st.button("Insert Into Template Details", key="ebay_templates_insert_block_btn"):
            current = str(st.session_state.get("ebay_template_details") or "").strip()
            block = str(block_html_edit or block_library.get(selected_block_name) or "").strip()
            st.session_state["ebay_template_details"] = (
                f"{current}\n\n{block}".strip() if current else block
            )
            st.success(f"Inserted `{selected_block_name}` into template details.")
            st.rerun()
        preview_html = str(block_html_edit or block_library.get(selected_block_name) or "").strip()
        if preview_html:
            st.caption("Block preview")
            components.html(preview_html, height=150, scrolling=True)
            st.code(preview_html, language="html")
        if st.button("Create Golden Stackers Starter Templates", key="ebay_templates_seed_starter_btn"):
            if not ensure_permission(user, "create", "Create Starter Listing Templates"):
                st.stop()
            created_count = 0
            for payload in _starter_listing_templates():
                repo.upsert_ebay_listing_template_profile(
                    environment=settings.app_env,
                    username=user.username,
                    name=str(payload["name"]),
                    marketplace=str(payload["marketplace"]),
                    listing_title_template=str(payload["title"]),
                    marketplace_details_template=str(payload["details"]),
                    listing_price_default=to_decimal(payload["price_default"]),
                    quantity_default=int(payload["qty_default"]),
                    listing_status_default=str(payload["status_default"]),
                    is_shared=bool(payload["is_shared"]),
                    is_default=bool(payload["is_default"]),
                    is_active=True,
                    actor=user.username,
                )
                created_count += 1
            st.success(f"Upserted {created_count} starter branded template(s).")
            st.rerun()

    st.markdown("### Save eBay Listing Template")
    with st.form("save_ebay_listing_template_form"):
        t1, t2, t3 = st.columns(3)
        with t1:
            template_name = st.text_input("Template Name", key="ebay_template_name")
        with t2:
            template_marketplace = st.selectbox("Marketplace", MARKETPLACES, index=MARKETPLACES.index("ebay"))
        with t3:
            template_status_default = st.selectbox(
                "Default Listing Status",
                ["draft", "active", "ended", "sold"],
                index=0,
                key="ebay_template_status_default",
            )
        t4, t5 = st.columns(2)
        with t4:
            template_price_default = st.number_input(
                "Default Price",
                min_value=0.0,
                value=0.0,
                step=0.01,
                key="ebay_template_price_default",
            )
        with t5:
            template_qty_default = st.number_input(
                "Default Quantity",
                min_value=1,
                value=1,
                step=1,
                key="ebay_template_qty_default",
            )
        template_title = st.text_input(
            "Listing Title Template",
            key="ebay_template_listing_title",
            help="Supports placeholders like {{sku}}, {{title}}, {{category}}, {{metal_type}}, {{weight_oz}}",
        )
        template_details = st.text_area(
            "Marketplace Details / HTML Template",
            key="ebay_template_details",
            help="Supports placeholders like {{sku}}, {{title}}, {{category}}, {{metal_type}}, {{weight_oz}}",
            height=180,
        )
        t6, t7 = st.columns(2)
        with t6:
            template_is_shared = st.checkbox("Team-shared", value=False, key="ebay_template_is_shared")
        with t7:
            template_is_default = st.checkbox("Set as default", value=False, key="ebay_template_is_default")
        template_submit = st.form_submit_button("Save Template")
    if template_submit:
        if not ensure_permission(user, "create", "Save eBay Listing Template"):
            st.stop()
        try:
            repo.upsert_ebay_listing_template_profile(
                environment=settings.app_env,
                username=user.username,
                name=template_name.strip(),
                marketplace=template_marketplace.strip().lower(),
                listing_title_template=template_title.strip(),
                marketplace_details_template=template_details.strip(),
                listing_price_default=to_decimal(template_price_default),
                quantity_default=int(template_qty_default),
                listing_status_default=template_status_default.strip().lower(),
                is_shared=bool(template_is_shared),
                is_default=bool(template_is_default),
                is_active=True,
                actor=user.username,
            )
            st.success("Template saved.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _sanitize_listing_html(value: str) -> tuple[str, list[str]]:
    raw = (value or "").strip()
    html = _plain_text_listing_to_html(raw) if raw and not _looks_like_listing_html(raw) else raw
    if not html:
        return "", []

    notes: list[str] = []
    if html != raw:
        notes.append("Formatted plain text into eBay-safe HTML paragraphs/lists")
    sanitized = html
    patterns = [
        (r"(?is)<\s*script[^>]*>.*?<\s*/\s*script\s*>", "Removed <script> blocks"),
        (r"(?is)<\s*style[^>]*>.*?<\s*/\s*style\s*>", "Removed <style> blocks"),
        (r"(?is)<\s*(iframe|object|embed|form|input|button|textarea|select)\b[^>]*>.*?<\s*/\s*\1\s*>", "Removed disallowed embedded/form tags"),
        (r"(?is)<\s*(iframe|object|embed|form|input|button|textarea|select)\b[^>]*/\s*>", "Removed disallowed self-closing tags"),
    ]
    for pattern, note in patterns:
        updated = re.sub(pattern, "", sanitized)
        if updated != sanitized:
            notes.append(note)
            sanitized = updated

    on_attr_cleaned = re.sub(r'(?i)\s+on[a-z0-9_-]+\s*=\s*(".*?"|\'.*?\'|[^\s>]+)', "", sanitized)
    if on_attr_cleaned != sanitized:
        notes.append("Removed inline event handler attributes")
        sanitized = on_attr_cleaned

    js_proto_cleaned = re.sub(r"(?i)javascript\s*:", "", sanitized)
    if js_proto_cleaned != sanitized:
        notes.append("Removed javascript: URI patterns")
        sanitized = js_proto_cleaned

    return sanitized.strip(), notes


def _looks_like_listing_html(value: str) -> bool:
    return bool(re.search(r"(?is)<\s*(p|br|div|ul|ol|li|h[1-6]|strong|b|em|span|table|section)\b", str(value or "")))


def _plain_text_listing_to_html(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    blocks = re.split(r"\n\s*\n+", text)
    html_parts: list[str] = []
    pending_bullets: list[str] = []

    def _flush_bullets() -> None:
        if not pending_bullets:
            return
        items = "".join(f"<li>{escape(item.strip())}</li>" for item in pending_bullets if item.strip())
        if items:
            html_parts.append(f"<ul>{items}</ul>")
        pending_bullets.clear()

    heading_pattern = re.compile(r"^[A-Z][A-Za-z0-9 &'/.:-]{2,80}$")
    bullet_pattern = re.compile(r"^\s*(?:[-*•]|[0-9]+[.)])\s+(.+?)\s*$")
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if len(lines) == 1:
            raw_line = lines[0].strip()
            bullet_match = bullet_pattern.match(raw_line)
            if bullet_match:
                pending_bullets.append(bullet_match.group(1).strip())
                continue
            _flush_bullets()
            if heading_pattern.match(raw_line) and len(raw_line.split()) <= 8 and not raw_line.endswith("."):
                html_parts.append(f"<h3>{escape(raw_line)}</h3>")
            else:
                html_parts.append(f"<p>{escape(raw_line)}</p>")
            continue
        _flush_bullets()
        paragraph_lines: list[str] = []
        for line in lines:
            bullet_match = bullet_pattern.match(line)
            if bullet_match:
                if paragraph_lines:
                    html_parts.append(f"<p>{escape(' '.join(paragraph_lines).strip())}</p>")
                    paragraph_lines = []
                pending_bullets.append(bullet_match.group(1).strip())
            else:
                _flush_bullets()
                paragraph_lines.append(line.strip())
        if paragraph_lines:
            html_parts.append(f"<p>{escape(' '.join(paragraph_lines).strip())}</p>")
    _flush_bullets()
    return "\n".join(html_parts).strip()


def _validate_listing_html(value: str) -> list[str]:
    errors: list[str] = []
    html = (value or "").strip()
    if not html:
        errors.append("Listing description cannot be empty after sanitization.")
        return errors
    if len(html) > 50000:
        errors.append("Listing description is too long (> 50,000 chars).")
    if re.search(r"(?is)<\s*(script|iframe|object|embed|form|input|button|textarea|select)\b", html):
        errors.append("Listing description contains disallowed tags.")
    if re.search(r"(?i)\son[a-z0-9_-]+\s*=", html):
        errors.append("Listing description contains inline event handlers.")
    if re.search(r"(?i)javascript\s*:", html):
        errors.append("Listing description contains javascript: URI values.")
    return errors


def _execute_batch_publish_for_listing(
    *,
    repo: InventoryRepository,
    listing_obj,
    actor: str,
    batch_id: str,
    ebay: EbayClient,
    access_token: str,
    marketplace_id: str,
    currency: str,
    content_language: str,
    merchant_location_key: str,
    payment_policy_id: str,
    fulfillment_policy_id: str,
    return_policy_id: str,
    category_id: str,
    product_by_id: dict[int, Product] | None = None,
    listing_media_rows: list[object] | None = None,
) -> dict:
    listing_id = int(listing_obj.id)
    offer_id = ""
    external_listing_id = ""
    inventory_sku = ""
    message = ""
    status = "error"
    product_obj = None
    try:
        if product_by_id:
            product_obj = product_by_id.get(int(listing_obj.product_id or 0))
        if product_obj is None:
            product_obj = getattr(listing_obj, "product", None)
        if product_obj is None:
            product_obj = repo.db.get(Product, int(listing_obj.product_id))
        if product_obj is None:
            raise ValueError("Linked product not found.")
        if listing_media_rows is None:
            listing_media_rows = repo.list_media_assets_for_listing(int(listing_id))
        image_urls = [
            str(m.s3_url or "").strip()
            for m in listing_media_rows
            if str(m.media_type or "").strip().lower() == "image"
            and str(m.s3_url or "").strip().startswith("https://")
        ]
        if not image_urls:
            raise ValueError("No HTTPS listing images available.")
        image_urls = image_urls[:24]
        inventory_sku = _listing_ebay_inventory_sku(product_obj, listing_obj)

        inventory_payload = {
            "availability": {
                "shipToLocationAvailability": {"quantity": int(listing_obj.quantity_listed or 1)}
            },
            "condition": "NEW",
            "product": {
                "title": listing_obj.listing_title,
                "description": listing_obj.listing_title,
                "imageUrls": image_urls,
            },
        }
        _maybe_add_package_data(inventory_payload, product_obj)
        ebay.create_or_replace_inventory_item(
            access_token=access_token,
            sku=inventory_sku,
            payload=inventory_payload,
            content_language=content_language,
        )

        offer_payload = {
            "sku": inventory_sku,
            "marketplaceId": marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": int(listing_obj.quantity_listed or 1),
            "categoryId": category_id,
            "merchantLocationKey": merchant_location_key,
            "listingDescription": listing_obj.listing_title,
            "listingDuration": "GTC",
            "listingPolicies": {
                "paymentPolicyId": payment_policy_id,
                "fulfillmentPolicyId": fulfillment_policy_id,
                "returnPolicyId": return_policy_id,
            },
            "pricingSummary": {
                "price": {
                    "value": str(round(float(listing_obj.listing_price or 0), 2)),
                    "currency": currency,
                }
            },
        }
        offer_id, recovered_existing_offer = _create_or_update_offer_with_duplicate_recovery(
            ebay=ebay,
            access_token=access_token,
            payload=offer_payload,
            content_language=content_language,
            sku=inventory_sku,
            marketplace_id=marketplace_id,
            format_type="FIXED_PRICE",
        )
        publish_result = ebay.publish_offer(
            access_token=access_token,
            offer_id=offer_id,
            inventory_sku=inventory_sku,
            content_language=content_language,
        )
        external_listing_id = str(publish_result.get("listingId") or "").strip()
        if not external_listing_id:
            raise RuntimeError("eBay publishOffer did not return listingId.")
        listing_url = ebay.listing_url_for_id(external_listing_id)
        status = "success"
        message = "Published"
        if recovered_existing_offer:
            message = "Published (reused existing offer)"
        return_payload = {
            "external_listing_id": external_listing_id,
            "marketplace_url": listing_url,
            "listing_status": "active",
        }
    except Exception as exc:
        message = str(exc)
        return_payload = {}

    details_raw = (listing_obj.marketplace_details or "").strip()
    details_obj: dict = {}
    if details_raw:
        try:
            parsed = json.loads(details_raw)
            if isinstance(parsed, dict):
                details_obj = parsed
            else:
                details_obj = {"notes": details_raw}
        except Exception:
            details_obj = {"notes": details_raw}
    exec_history = details_obj.get("publish_batch_execution")
    if not isinstance(exec_history, list):
        exec_history = []
    exec_history.append(
        {
            "batch_id": batch_id,
            "executed_at": utcnow_naive().isoformat(),
            "executed_by": actor,
            "offer_id": offer_id,
            "inventory_sku": inventory_sku,
            "product_sku": str(getattr(product_obj, "sku", "") or "").strip(),
            "listing_id": external_listing_id,
            "status": status,
            "message": message,
        }
    )
    details_obj["publish_batch_execution"] = exec_history[-100:]
    publish_meta = details_obj.get("ebay_publish")
    if not isinstance(publish_meta, dict):
        publish_meta = {}
    batch_publish_meta = {
        "offer_id": offer_id,
        "inventory_sku": inventory_sku,
        "product_sku": str(getattr(product_obj, "sku", "") or "").strip(),
        "marketplace_id": marketplace_id,
    }
    if status == "success":
        batch_publish_meta["published_at"] = utcnow_naive().isoformat()
    publish_meta.update(batch_publish_meta)
    details_obj["ebay_publish"] = publish_meta
    return_payload["marketplace_details"] = json.dumps(details_obj, indent=2)
    repo.update_listing(listing_id, return_payload, actor=actor)
    return {
        "listing_id": listing_id,
        "status": status,
        "offer_id": offer_id,
        "external_listing_id": external_listing_id,
        "message": message,
    }


def _append_template_tracking_comment(
    details: str,
    template_id: int | None,
    template_name: str,
    environment: str,
) -> str:
    text = (details or "").strip()
    if not template_id:
        return text
    safe_name = re.sub(r"[;\n\r]", " ", (template_name or "").strip())
    safe_env = re.sub(r"[;\n\r]", " ", (environment or "").strip())
    marker = (
        f"<!-- gs_template_id:{int(template_id)};"
        f"gs_template_name:{safe_name};gs_template_env:{safe_env} -->"
    )
    marker_pattern = re.compile(
        r"<!--\s*gs_template_id:\d+;gs_template_name:[^;]*;gs_template_env:[^;]*\s*-->",
        re.IGNORECASE,
    )
    if marker_pattern.search(text):
        return marker_pattern.sub(marker, text)
    if not text:
        return marker
    return f"{text}\n{marker}"


def _extract_template_tracking_comment(details: str) -> tuple[int | None, str]:
    text = (details or "").strip()
    if not text:
        return None, ""
    match = re.search(
        r"<!--\s*gs_template_id:(\d+);gs_template_name:([^;]*);gs_template_env:[^;]*\s*-->",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, ""
    try:
        template_id = int(match.group(1))
    except Exception:
        template_id = None
    template_name = str(match.group(2) or "").strip()
    return template_id, template_name


def render_listings(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    quality_policy = load_ai_quality_policy(repo)
    st.subheader("Marketplace Listings")
    publish_flash = st.session_state.get("listings_publish_flash")
    if isinstance(publish_flash, dict) and publish_flash:
        level = str(publish_flash.get("level") or "info").strip().lower()
        message = str(publish_flash.get("message") or "").strip()
        warning_message = str(publish_flash.get("warning") or "").strip()
        offer_id = str(publish_flash.get("offer_id") or "").strip()
        listing_url = str(publish_flash.get("listing_url") or "").strip()
        if level == "error":
            st.error(message or "eBay publish action failed.")
        elif level == "warning":
            st.warning(message or "eBay publish action completed with warnings.")
        else:
            st.success(message or "eBay publish action completed.")
        if warning_message:
            st.warning(warning_message)
        meta_bits: list[str] = []
        if offer_id:
            meta_bits.append(f"offer_id={offer_id}")
        if meta_bits:
            st.caption(" | ".join(meta_bits))
        if listing_url:
            st.link_button("Open eBay Listing URL", listing_url)
        if st.button("Dismiss Publish Result", key="listings_publish_flash_dismiss_btn"):
            st.session_state.pop("listings_publish_flash", None)
            st.rerun()
    render_help_panel(
        section_title="Listings",
        goal="Track channel listings, status, links, and listing-level media for sell-through.",
        steps=[
            "Select an existing product, then set marketplace, title, price, and quantity.",
            "Store external listing ID and live marketplace URL after posting.",
            "Attach listing photos/videos for channel-specific listing packages.",
            "Use listing status to reflect draft, active, and ended lifecycle states.",
        ],
        roadmap_phase="v0.3 Channel Sync + Accounting Readiness",
    )
    load_workspace_telemetry = st.checkbox(
        "Load Listings Workflow Telemetry (slower)",
        value=False,
        key="listings_load_workspace_telemetry",
        help="Defers workspace feedback and workflow completion telemetry queries unless explicitly requested.",
    )
    if load_workspace_telemetry:
        render_workspace_feedback(
            repo=repo,
            actor=user.username,
            workspace_key="listings",
            section_title="Workspace Feedback",
        )
        render_workspace_task_completion(
            repo=repo,
            actor=user.username,
            workflow_key="listings",
            section_title="Workflow Completion: Listings",
            tasks=[
                ("Created draft listing", "listing_draft_created"),
                ("Reviewed/approved pending listing", "listing_review_completed"),
                ("Queued or executed publish/revise action", "listing_publish_or_revise"),
            ],
        )
    else:
        st.caption(
            "Listings workflow telemetry is deferred. Enable `Load Listings Workflow Telemetry (slower)` "
            "to load workspace feedback and completion state."
        )
    st.markdown("### Product Working Set")
    pw1, pw2 = st.columns([1, 1])
    with pw1:
        load_all_products = st.checkbox(
            "Load All Products (slower)",
            value=False,
            key="listings_load_all_products",
            help="When off, Listings product selectors use a recent bounded product set for faster page loads.",
        )
    with pw2:
        recent_product_limit = st.number_input(
            "Recent Product Limit",
            min_value=50,
            max_value=1000,
            value=500,
            step=50,
            key="listings_recent_product_limit",
            disabled=bool(load_all_products),
        )
    products = repo.list_products(
        limit=None if bool(load_all_products) else int(recent_product_limit or 500)
    )
    if load_all_products:
        st.caption(f"Loaded all product rows: {len(products)}.")
    else:
        st.caption(
            f"Loaded latest {len(products)} product row(s). Enable `Load All Products (slower)` "
            "to use full product history in create/bulk selectors."
        )

    if not products:
        st.info("Create at least one product before adding listings.")
        return

    _audit_log_cache: dict[int, list[object]] = {}
    _audit_logs_by_entity_cache: dict[tuple[str, int], list[object]] = {}

    def _get_audit_logs(limit: int) -> list[object]:
        normalized_limit = max(1, int(limit))
        exact = _audit_log_cache.get(normalized_limit)
        if exact is not None:
            return list(exact)
        reusable_limit = None
        for cached_limit in _audit_log_cache.keys():
            if cached_limit < normalized_limit:
                continue
            if reusable_limit is None or cached_limit < reusable_limit:
                reusable_limit = cached_limit
        if reusable_limit is not None:
            reused_rows = list(_audit_log_cache[reusable_limit][:normalized_limit])
            _audit_log_cache[normalized_limit] = reused_rows
            return reused_rows
        rows = list(repo.list_audit_logs(limit=normalized_limit))
        _audit_log_cache[normalized_limit] = list(rows)
        return rows

    def _audit_logs_for_entity(*, entity_type: str, limit: int) -> list[object]:
        normalized_type = str(entity_type or "").strip().lower()
        if not normalized_type:
            return []
        normalized_limit = max(1, int(limit))
        cache_key = (normalized_type, normalized_limit)
        cached = _audit_logs_by_entity_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        reusable_limit = None
        for cached_type, cached_limit in _audit_logs_by_entity_cache.keys():
            if cached_type != normalized_type:
                continue
            if cached_limit < normalized_limit:
                continue
            if reusable_limit is None or cached_limit < reusable_limit:
                reusable_limit = cached_limit
        if reusable_limit is not None:
            reused_rows = list(_audit_logs_by_entity_cache[(normalized_type, reusable_limit)][:normalized_limit])
            _audit_logs_by_entity_cache[cache_key] = reused_rows
            return reused_rows
        rows = _get_audit_logs(normalized_limit)
        filtered_rows = [
            row
            for row in rows
            if str(getattr(row, "entity_type", "") or "").strip().lower() == normalized_type
        ]
        _audit_logs_by_entity_cache[cache_key] = filtered_rows
        return filtered_rows

    _ebay_publish_preset_cache: dict[tuple[str, str, bool], list[object]] = {}

    def _list_ebay_publish_presets_cached(*, active_only: bool) -> list[object]:
        key = (str(settings.app_env), str(user.username or ""), bool(active_only))
        cached = _ebay_publish_preset_cache.get(key)
        if cached is not None:
            return list(cached)
        rows = list(
            repo.list_ebay_publish_presets(
                environment=settings.app_env,
                username=user.username,
                active_only=bool(active_only),
            )
        )
        _ebay_publish_preset_cache[key] = list(rows)
        return rows

    product_by_id = {int(p.id): p for p in products}
    has_coin_linked_products = any(getattr(p, "coin_reference_id", None) is not None for p in products)
    load_coin_reference_catalog = st.checkbox(
        "Load Coin Reference Catalog (slower)",
        value=False,
        key="listings_load_coin_reference_catalog",
        help="Defers loading linked coin reference catalog rows used for listing auto-context.",
    )
    coin_ref_by_id = {}
    if has_coin_linked_products and load_coin_reference_catalog:
        coin_ref_by_id = {
            int(row.id): row for row in repo.list_coin_references(active_only=True, limit=5000)
        }
    elif has_coin_linked_products:
        st.caption(
            "Coin reference context is deferred. Enable `Load Coin Reference Catalog (slower)` for linked-coin auto-context."
        )
    if "create_listing_marketplace" not in st.session_state:
        st.session_state["create_listing_marketplace"] = str(
            st.session_state.get("ebay_workspace_store_marketplace_id_input")
            or "ebay"
        ).strip().lower()
    workspace_store_profiles_cache: dict[str, dict] | None = None

    def _get_workspace_store_profiles() -> dict[str, dict]:
        nonlocal workspace_store_profiles_cache
        if workspace_store_profiles_cache is None:
            workspace_store_profiles_cache = _load_workspace_store_profiles(repo)
        return dict(workspace_store_profiles_cache)

    default_workspace_store_profile = get_runtime_str(repo, "ebay_workspace_default_store_profile", "").strip()
    if (
        default_workspace_store_profile
        and default_workspace_store_profile in _get_workspace_store_profiles()
        and not bool(st.session_state.get("listings_create_store_default_applied_once"))
    ):
        _apply_store_profile_to_listing_create(_get_workspace_store_profiles()[default_workspace_store_profile])
        st.session_state["listings_create_store_profile_selected"] = default_workspace_store_profile
        st.session_state["listings_create_store_default_applied_once"] = True

    st.markdown("### eBay Listing Templates")
    load_listing_templates = st.checkbox(
        "Load eBay Listing Templates (slower)",
        value=False,
        key="listings_load_listing_templates",
        help="Defers template-profile query and widget hydration unless explicitly requested.",
    )
    if not load_listing_templates:
        st.caption(
            "Template profile loading is deferred. Enable `Load eBay Listing Templates (slower)` to query and apply templates."
        )
    else:
        template_rows = []
        template_lookup: dict[str, object] = {}
        try:
            template_rows = repo.list_ebay_listing_template_profiles(
                environment=settings.app_env,
                username=user.username,
                include_shared=True,
                active_only=True,
            )
        except Exception:
            template_rows = []

        if template_rows:
            for row in template_rows:
                label = (
                    f"{row.name} [{'Shared' if bool(row.is_shared) else 'Mine'}"
                    f"{' | Default' if bool(row.is_default) else ''}]"
                )
                if label in template_lookup:
                    label = f"{label} #{row.id}"
                template_lookup[label] = row
            template_select = st.selectbox(
                "Select Template",
                options=["None"] + list(template_lookup.keys()),
                key="create_listing_template_select",
            )
            if st.button("Load Template Into Create Form", key="create_listing_template_load_btn"):
                selected_row = template_lookup.get(template_select)
                if selected_row is None:
                    st.warning("Choose a template first.")
                else:
                    st.session_state["create_listing_marketplace"] = str(selected_row.marketplace or "ebay").strip().lower()
                    st.session_state["create_listing_title"] = str(selected_row.listing_title_template or "").strip()
                    st.session_state["create_listing_price"] = float(selected_row.listing_price_default or 0.0)
                    st.session_state["create_listing_qty"] = int(selected_row.quantity_default or 1)
                    st.session_state["create_listing_status"] = str(selected_row.listing_status_default or "draft").strip().lower()
                    st.session_state["create_listing_details"] = str(selected_row.marketplace_details_template or "").strip()
                    st.session_state["create_listing_template_loaded_id"] = int(selected_row.id)
                    st.session_state["create_listing_template_loaded_name"] = str(selected_row.name or "").strip()
                    st.success(f"Loaded template `{selected_row.name}` into create form.")
                    st.rerun()
        else:
            st.caption("No eBay listing templates yet. Create one below.")

    st.markdown("### eBay Store Profile Context")
    load_store_profile_context = st.checkbox(
        "Load eBay Store Profile Context (slower)",
        value=False,
        key="listings_load_store_profile_context",
        help="Defers store-profile payload hydration unless explicitly requested.",
    )
    if not load_store_profile_context:
        st.caption(
            "Store profile context is deferred. Enable `Load eBay Store Profile Context (slower)` "
            "to load and apply saved eBay store profiles."
        )
    else:
        workspace_store_profiles = _get_workspace_store_profiles()
        if workspace_store_profiles:
            store_profile_keys = list(workspace_store_profiles.keys())
            if len(store_profile_keys) > 1:
                store_profile_keys = sorted(store_profile_keys)
            store_profile_options = ["None"] + store_profile_keys
            selected_store_profile = st.selectbox(
                "Apply Store Profile to Create Defaults",
                options=store_profile_options,
                key="listings_create_store_profile_selected",
                help="Loads format/policy/category defaults into eBay draft create controls.",
            )
            if st.button("Apply Store Profile", key="listings_create_store_profile_apply_btn"):
                if selected_store_profile == "None":
                    st.warning("Select a store profile first.")
                else:
                    payload = workspace_store_profiles.get(selected_store_profile) or {}
                    _apply_store_profile_to_listing_create(payload)
                    st.success(f"Applied store profile `{selected_store_profile}` to create defaults.")
                    st.rerun()
        else:
            st.caption("No saved workspace store profiles found. Configure them in eBay Workspace.")

    with st.expander("Reusable Branded HTML Blocks", expanded=False):
        st.caption("Insert reusable Golden Stackers HTML blocks into create-flow details.")
        block_library = _listing_html_block_library()
        selected_block_name = st.selectbox(
            "Block",
            options=list(block_library.keys()),
            key="listings_html_block_select",
        )
        if st.button("Insert Into Create Listing Details", key="listings_insert_block_create_btn"):
            current = str(st.session_state.get("create_listing_details") or "").strip()
            block = str(block_library.get(selected_block_name) or "").strip()
            st.session_state["create_listing_details"] = (
                f"{current}\n\n{block}".strip() if current else block
            )
            st.success(f"Inserted `{selected_block_name}` into create listing details.")
            st.rerun()
        preview_html = str(block_library.get(selected_block_name) or "").strip()
        if preview_html:
            st.caption("Block preview")
            components.html(preview_html, height=150, scrolling=True)
            st.code(preview_html, language="html")

    with st.expander("Template Management", expanded=False):
        st.caption("Template create/edit has moved to a dedicated page to keep Listings focused.")
        st.page_link("pages/25_eBay_Templates.py", label="Open eBay Templates")
        st.page_link("pages/26_Listing_Wizard.py", label="Open Listing Wizard")

    st.markdown("### Optional Initial Listing Media")
    load_create_media_capture = st.checkbox(
        "Load Create Listing Media Capture (slower)",
        value=False,
        key="listings_load_create_media_capture",
        help="Defers camera/file capture widget hydration for create flow unless explicitly requested.",
    )
    if load_create_media_capture:
        listing_uploaded_by = st.text_input("Uploaded By", value="employee", key="listing_uploaded_by")
        listing_files = render_media_capture_inputs(
            key_prefix="create_listing_media",
            upload_label="Listing Photos/Videos (optional)",
            allow_enhanced=True,
        )
    else:
        listing_uploaded_by = "employee"
        listing_files = []
        st.caption(
            "Create-flow media capture is deferred. Enable `Load Create Listing Media Capture (slower)` "
            "to attach initial listing photos/videos during create."
        )

    st.markdown("### Listings/eBay Copilot")
    listing_ai_seed = st.text_area(
        "AI Seed Prompt (optional)",
        key="listing_ai_seed_prompt",
        help="Example: optimize for eBay search intent while keeping conservative, policy-safe copy.",
    )
    if st.button("Generate Listing Copilot Suggestions", key="listing_generate_ai_suggestions_btn"):
        if not ensure_permission(user, "ai_comp_use", "Generate Listing Copilot Suggestions"):
            st.stop()
        try:
            system_message = get_runtime_str(
                repo,
                "comp_llm_system_message",
                "You are an eBay listing assistant. Return concise outputs.",
            ).strip()
            instruction = (
                "Return ONLY JSON with keys: "
                "`suggested_title`, `suggested_details`, `suggested_price`, `suggested_marketplace_details`, `publish_checklist`. "
                "`publish_checklist` must be an array of short strings focused on eBay readiness risks."
            )
            query_parts = [
                str(listing_ai_seed or "").strip(),
                str(st.session_state.get("create_listing_title") or "").strip(),
                str(st.session_state.get("create_listing_details") or "").strip(),
                str(st.session_state.get("create_listing_marketplace") or "ebay").strip(),
            ]
            query_text = " | ".join([p for p in query_parts if p]).strip() or "Suggest eBay listing draft defaults"
            result = execute_comp_summary(
                repo,
                query=query_text,
                ebay_rows=[],
                web_rows=[],
                spot_context={},
                system_message=system_message,
                instruction=instruction,
                workflow="listing",
            )
            payload = _try_extract_json_object(result.text)
            if not payload:
                st.warning("AI output was not valid JSON. Raw response captured below.")
            else:
                current_title = str(st.session_state.get("create_listing_title") or "").strip()
                current_details = str(st.session_state.get("create_listing_details") or "").strip()
                current_marketplace = str(st.session_state.get("create_listing_marketplace") or "ebay").strip()
                title_val = str(payload.get("suggested_title") or "").strip()
                details_val_raw = _resolve_ai_listing_details(
                    payload,
                    fallback="",
                )
                used_details_fallback = False
                details_val = details_val_raw
                blocked_title_terms = find_forbidden_terms(title_val, policy=quality_policy)
                blocked_detail_terms = find_forbidden_terms(details_val_raw, policy=quality_policy)
                if _is_weak_listing_details(details_val_raw, policy=quality_policy):
                    details_val = _build_fallback_ebay_listing_details(
                        title=title_val or current_title,
                        existing_description=current_details,
                        marketplace=current_marketplace or "ebay",
                    )
                    used_details_fallback = True
                price_val = str(payload.get("suggested_price") or "").strip()
                checklist_val = payload.get("publish_checklist")
                if title_val and not is_weak_listing_title(title_val, policy=quality_policy):
                    st.session_state["create_listing_title"] = title_val
                elif title_val:
                    warning = "AI suggested title was too weak/generic or violated policy; kept existing listing title."
                    if blocked_title_terms:
                        warning += f" Blocked term(s): {', '.join(blocked_title_terms[:4])}."
                    st.warning(warning)
                if details_val:
                    st.session_state["create_listing_details"] = details_val
                try:
                    parsed_price = float(price_val) if price_val else 0.0
                    if parsed_price > 0:
                        st.session_state["create_listing_price"] = parsed_price
                except Exception:
                    pass
                if isinstance(checklist_val, list):
                    st.session_state["listing_copilot_checklist"] = [
                        str(x).strip() for x in checklist_val if str(x).strip()
                    ]
                if used_details_fallback:
                    success_msg = (
                        "Listing copilot suggestions applied. AI details were too short/policy-flagged, "
                        "so enriched eBay-ready details were generated."
                    )
                    if blocked_detail_terms:
                        success_msg += f" Blocked detail term(s): {', '.join(blocked_detail_terms[:4])}."
                    st.success(success_msg)
                else:
                    st.success("Listing copilot suggestions applied to create defaults.")
            st.session_state["listing_copilot_raw"] = str(result.text or "").strip()
            st.rerun()
        except Exception as exc:
            st.error(f"Listing copilot suggestion generation failed: {exc}")

    raw_listing_ai = str(st.session_state.get("listing_copilot_raw") or "").strip()
    if raw_listing_ai:
        with st.expander("Last Listings Copilot Payload", expanded=False):
            st.code(raw_listing_ai, language="json")

    create_ebay_defaults = _ebay_create_publish_defaults(repo)
    create_preset_loaded = False
    default_create_preset: object | None = None

    def _get_default_create_preset() -> object | None:
        nonlocal create_preset_loaded, default_create_preset
        if not create_preset_loaded:
            create_preset_loaded = True
            preset_rows_for_create = _list_ebay_publish_presets_cached(active_only=True)
            if preset_rows_for_create:
                default_create_preset = preset_rows_for_create[0]
                for preset_row in preset_rows_for_create:
                    if bool(getattr(preset_row, "is_default", False)):
                        default_create_preset = preset_row
                        break
        return default_create_preset

    st.session_state.setdefault("create_listing_ebay_format", str(create_ebay_defaults.get("format_type") or "FIXED_PRICE"))
    st.session_state.setdefault("create_listing_ebay_duration", str(create_ebay_defaults.get("listing_duration") or "GTC"))
    st.session_state.setdefault("create_listing_ebay_best_offer_enabled", bool(create_ebay_defaults.get("best_offer_enabled")))
    st.session_state.setdefault("create_listing_ebay_category_id", str(create_ebay_defaults.get("category_id") or ""))
    st.session_state.setdefault("create_listing_ebay_merchant_location_key", str(create_ebay_defaults.get("merchant_location_key") or ""))
    st.session_state.setdefault("create_listing_ebay_payment_policy_id", str(create_ebay_defaults.get("payment_policy_id") or ""))
    st.session_state.setdefault("create_listing_ebay_fulfillment_policy_id", str(create_ebay_defaults.get("fulfillment_policy_id") or ""))
    st.session_state.setdefault("create_listing_ebay_return_policy_id", str(create_ebay_defaults.get("return_policy_id") or ""))
    st.session_state.setdefault("create_listing_ebay_marketplace_id", str(create_ebay_defaults.get("marketplace_id") or settings.ebay_marketplace_id))
    st.session_state.setdefault("create_listing_ebay_currency", str(create_ebay_defaults.get("currency") or settings.ebay_currency))
    st.session_state.setdefault("create_listing_ebay_content_language", str(create_ebay_defaults.get("content_language") or settings.ebay_content_language))
    st.session_state.setdefault(
        "create_listing_ebay_store_category_names",
        _normalize_store_category_names(create_ebay_defaults.get("store_category_names")),
    )
    st.session_state.setdefault("create_listing_ebay_auction_start_price", float(create_ebay_defaults.get("auction_start_price") or 1.0))
    st.session_state.setdefault("create_listing_ebay_auction_reserve_price", float(create_ebay_defaults.get("auction_reserve_price") or 0.0))
    st.session_state.setdefault("create_listing_ebay_auction_buy_now_price", float(create_ebay_defaults.get("auction_buy_now_price") or 0.0))
    create_format_type = str(
        st.session_state.get("create_listing_ebay_format")
        or create_ebay_defaults.get("format_type")
        or get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE")
    ).strip().upper()
    if create_format_type not in {"FIXED_PRICE", "AUCTION"}:
        create_format_type = "FIXED_PRICE"
    create_listing_duration = str(
        st.session_state.get("create_listing_ebay_duration")
        or create_ebay_defaults.get("listing_duration")
        or ("GTC" if create_format_type == "FIXED_PRICE" else get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_5"))
    ).strip().upper()
    load_create_ebay_readiness_preview = st.checkbox(
        "Load Create-Flow eBay Readiness Preview (slower)",
        value=False,
        key="listings_load_create_ebay_readiness_preview",
        help="Defers create-flow readiness evaluation and preset fallback resolution unless explicitly requested.",
    )
    st.caption(
        "Create-flow eBay defaults source: active workspace store profile values with runtime fallback "
        f"(format=`{create_format_type}`, duration=`{create_listing_duration}`)."
    )
    if load_create_ebay_readiness_preview:
        create_default_preset = _get_default_create_preset()
        readiness_preview = evaluate_ebay_readiness(
            listing_title=str(st.session_state.get("create_listing_title") or "").strip(),
            listing_description=str(st.session_state.get("create_listing_desc") or "").strip(),
            listing_price=float(st.session_state.get("create_listing_price") or 0.0),
            auction_start_price=float(st.session_state.get("create_listing_ebay_auction_start_price") or create_ebay_defaults.get("auction_start_price") or st.session_state.get("create_listing_price") or 0.0),
            auction_reserve_price=float(st.session_state.get("create_listing_ebay_auction_reserve_price") or create_ebay_defaults.get("auction_reserve_price") or 0.0),
            auction_buy_now_price=float(st.session_state.get("create_listing_ebay_auction_buy_now_price") or create_ebay_defaults.get("auction_buy_now_price") or 0.0),
            quantity_listed=int(st.session_state.get("create_listing_qty") or 1),
            listing_status="draft",
            format_type=create_format_type,
            listing_duration=create_listing_duration,
            media_count=len(listing_files or []),
            category_id=(
                str(st.session_state.get("create_listing_ebay_category_id") or "").strip()
                or (getattr(create_default_preset, "category_id", "") if create_default_preset else "")
                or str(create_ebay_defaults.get("category_id") or "").strip()
            ),
            merchant_location_key=(
                str(st.session_state.get("create_listing_ebay_merchant_location_key") or "").strip()
                or (getattr(create_default_preset, "merchant_location_key", "") if create_default_preset else "")
                or str(create_ebay_defaults.get("merchant_location_key") or "").strip()
            ),
            payment_policy_id=(
                str(st.session_state.get("create_listing_ebay_payment_policy_id") or "").strip()
                or (getattr(create_default_preset, "payment_policy_id", "") if create_default_preset else "")
                or str(create_ebay_defaults.get("payment_policy_id") or "").strip()
            ),
            fulfillment_policy_id=(
                str(st.session_state.get("create_listing_ebay_fulfillment_policy_id") or "").strip()
                or (getattr(create_default_preset, "fulfillment_policy_id", "") if create_default_preset else "")
                or str(create_ebay_defaults.get("fulfillment_policy_id") or "").strip()
            ),
            return_policy_id=(
                str(st.session_state.get("create_listing_ebay_return_policy_id") or "").strip()
                or (getattr(create_default_preset, "return_policy_id", "") if create_default_preset else "")
                or str(create_ebay_defaults.get("return_policy_id") or "").strip()
            ),
        )
        st.caption(
            f"Create-flow eBay readiness preview: status=`{readiness_preview.status}` score=`{readiness_preview.score}` "
            f"blockers=`{len(readiness_preview.blockers)}` warnings=`{len(readiness_preview.warnings)}`"
        )
        if readiness_preview.blockers:
            st.warning("Readiness blockers: " + " | ".join(readiness_preview.blockers))
        elif readiness_preview.warnings:
            st.info("Readiness warnings: " + " | ".join(readiness_preview.warnings))
    else:
        st.caption(
            "Create-flow readiness preview is deferred. Enable `Load Create-Flow eBay Readiness Preview (slower)` to run it."
        )
    copilot_checklist = st.session_state.get("listing_copilot_checklist") or []
    if copilot_checklist:
        st.caption("Copilot publish checklist")
        for item in copilot_checklist[:8]:
            st.write(f"- {item}")

    with st.form("create_listing_form", clear_on_submit=True):
        product_map = build_product_options(products, include_none=False, include_id=False)
        product_key = st.selectbox("Product", list(product_map.keys()), key="create_listing_product_key")
        selected_product_id = int(product_map[product_key])
        selected_product = product_by_id.get(selected_product_id)
        selected_coin_ref = (
            coin_ref_by_id.get(int(selected_product.coin_reference_id))
            if selected_product is not None and selected_product.coin_reference_id is not None
            else None
        )
        if selected_coin_ref is not None:
            year_start = getattr(selected_coin_ref, "year_start", None)
            year_end = getattr(selected_coin_ref, "year_end", None)
            years = (
                f"{int(year_start)}-{int(year_end)}"
                if year_start and year_end
                else (str(int(year_start)) if year_start else "")
            )
            st.caption(
                "Coin Ref: "
                f"{selected_coin_ref.coin_name} | {selected_coin_ref.country} | "
                f"{selected_coin_ref.denomination or '-'} | {years or 'n/a'}"
            )
        if selected_product is not None:
            ai_comp_ref = str(getattr(selected_product, "ai_comp", "") or "").strip()
            with st.expander("Product AI Comp (reference only)", expanded=False):
                if ai_comp_ref:
                    st.text_area(
                        "AI Comp Reference",
                        value=ai_comp_ref,
                        height=160,
                        disabled=True,
                        key="create_listing_ai_comp_reference",
                    )
                else:
                    st.caption("No AI Comp saved for this product.")
        marketplace = st.selectbox("Marketplace", MARKETPLACES, key="create_listing_marketplace")
        listing_title = st.text_input("Listing Title", key="create_listing_title")

        c1, c2, c3 = st.columns(3)
        with c1:
            listing_price = st.number_input(
                "Listing Price",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="create_listing_price",
            )
        with c2:
            quantity_listed = st.number_input(
                "Quantity Listed",
                min_value=1,
                value=1,
                step=1,
                key="create_listing_qty",
            )
        with c3:
            st.text_input("Initial Status", value="draft", disabled=True)
        create_bundle_enabled = st.checkbox(
            "This listing is a product lot / bundle",
            value=False,
            key="create_listing_bundle_enabled",
            help=(
                "Use when one marketplace listing unit contains multiple inventory units, "
                "for example one eBay listing for a lot of 10 coins."
            ),
        )
        create_bundle_primary_qty = 1
        create_bundle_metadata = _build_listing_bundle_metadata(
            enabled=False,
            primary_product=selected_product,
            units_per_listing=1,
            available_lots=int(quantity_listed),
        )
        create_bundle_overcommit = False
        if create_bundle_enabled:
            current_stock_qty = int(getattr(selected_product, "current_quantity", 0) or 0) if selected_product is not None else 0
            create_bundle_primary_qty = int(
                st.number_input(
                    "Units of selected product per listing",
                    min_value=1,
                    max_value=max(1, current_stock_qty),
                    value=max(1, min(max(1, current_stock_qty), int(st.session_state.get("create_listing_bundle_primary_qty") or 1))),
                    step=1,
                    key="create_listing_bundle_primary_qty",
                )
            )
            create_bundle_extra_options = {
                label: pid
                for label, pid in product_map.items()
                if int(pid or 0) != int(selected_product_id or 0)
            }
            st.session_state["create_listing_bundle_extra_product_labels"] = [
                label
                for label in list(st.session_state.get("create_listing_bundle_extra_product_labels") or [])
                if label in create_bundle_extra_options
            ]
            create_bundle_extra_labels = st.multiselect(
                "Additional products in this bundle",
                options=list(create_bundle_extra_options.keys()),
                key="create_listing_bundle_extra_product_labels",
                help="Optional. Choose extra products included in each marketplace listing unit.",
            )
            create_bundle_extra_components: list[dict[str, object]] = []
            for extra_label in create_bundle_extra_labels:
                extra_product_id = int(create_bundle_extra_options.get(extra_label) or 0)
                extra_product = product_by_id.get(extra_product_id)
                if extra_product is None:
                    continue
                extra_stock_qty = int(getattr(extra_product, "current_quantity", 0) or 0)
                extra_qty = int(
                    st.number_input(
                        f"Units of {getattr(extra_product, 'sku', '') or extra_product_id} per listing",
                        min_value=1,
                        max_value=max(1, extra_stock_qty),
                        value=max(
                            1,
                            min(
                                max(1, extra_stock_qty),
                                int(st.session_state.get(f"create_listing_bundle_extra_qty_{extra_product_id}") or 1),
                            ),
                        ),
                        step=1,
                        key=f"create_listing_bundle_extra_qty_{extra_product_id}",
                    )
                )
                create_bundle_extra_components.append(_bundle_component(extra_product, extra_qty))
            create_bundle_metadata = _build_listing_bundle_metadata(
                enabled=True,
                primary_product=selected_product,
                units_per_listing=int(create_bundle_primary_qty),
                available_lots=int(quantity_listed),
                additional_components=create_bundle_extra_components,
            )
            committed_units = int(create_bundle_metadata.get("inventory_units_committed") or 0)
            create_bundle_overcommitted_components: list[str] = []
            for component in list(create_bundle_metadata.get("components") or []):
                if not isinstance(component, dict):
                    continue
                units_needed = max(1, int(component.get("quantity_per_listing") or 1)) * int(quantity_listed)
                stock_qty = max(0, int(component.get("current_quantity") or 0))
                if units_needed > stock_qty:
                    create_bundle_overcommitted_components.append(
                        f"{component.get('sku') or component.get('product_id')}: needs {units_needed}, stock {stock_qty}"
                    )
            create_bundle_overcommit = bool(create_bundle_overcommitted_components)
            create_bundle_bits = [
                f"{row.get('sku') or row.get('product_id')} x {int(row.get('quantity_per_listing') or 1)}"
                for row in list(create_bundle_metadata.get("components") or [])
                if isinstance(row, dict)
            ]
            st.caption(
                f"Bundle composition per listing: {', '.join(create_bundle_bits)}. "
                f"{int(quantity_listed)} available lot(s) commits {committed_units} total inventory unit(s)."
            )
            if create_bundle_overcommit:
                st.warning(
                    "This bundle quantity exceeds current stock: "
                    + " | ".join(create_bundle_overcommitted_components)
                    + "."
                )
        listing_status = "draft"
        st.caption("New listings are always created as `draft` until reviewed.")
        listed_date = st.date_input("Listed Date", value=utc_today(), key="create_listing_listed_date")

        external_listing_id = st.text_input(
            "External Listing ID",
            help="Optional now. Fill this after posting to eBay/other marketplace.",
            key="create_listing_external_id",
        )
        marketplace_url = st.text_input(
            "Marketplace Listing URL",
            help="Direct public URL for eBay/Craigslist/Facebook/Whatnot listing.",
            key="create_listing_marketplace_url",
        )
        marketplace_details = st.text_area(
            "Marketplace Details",
            help="Optional freeform details or JSON metadata for channel-specific fields.",
            key="create_listing_details",
        )
        with st.expander("eBay Draft Publish Defaults (applied on create for eBay)", expanded=False):
            ef1, ef2, ef3 = st.columns(3)
            with ef1:
                st.selectbox(
                    "Format",
                    options=["FIXED_PRICE", "AUCTION"],
                    key="create_listing_ebay_format",
                )
            with ef2:
                st.text_input("Listing Duration", key="create_listing_ebay_duration")
            with ef3:
                st.checkbox("Best Offer Enabled", key="create_listing_ebay_best_offer_enabled")
            ep1, ep2, ep3 = st.columns(3)
            with ep1:
                st.text_input("Category ID", key="create_listing_ebay_category_id")
            with ep2:
                st.text_input("Merchant Location Key", key="create_listing_ebay_merchant_location_key")
            with ep3:
                st.text_input("Payment Policy ID", key="create_listing_ebay_payment_policy_id")
            ep4, ep5, ep6 = st.columns(3)
            with ep4:
                st.text_input("Fulfillment Policy ID", key="create_listing_ebay_fulfillment_policy_id")
            with ep5:
                st.text_input("Return Policy ID", key="create_listing_ebay_return_policy_id")
            with ep6:
                st.text_input("Marketplace ID", key="create_listing_ebay_marketplace_id")
            ec1, ec2 = st.columns(2)
            with ec1:
                st.text_input("Currency", key="create_listing_ebay_currency")
            with ec2:
                st.text_input("Content Language", key="create_listing_ebay_content_language")
            create_store_category_marketplace_id = str(
                st.session_state.get("create_listing_ebay_marketplace_id")
                or settings.ebay_marketplace_id
                or "EBAY_US"
            ).strip()
            _render_ebay_store_category_manager(
                repo,
                marketplace_id=create_store_category_marketplace_id,
                actor=user.username,
                key_prefix="create_listing_ebay",
            )
            create_store_category_options = [
                str(getattr(row, "category_path", "") or "").strip()
                for row in _store_category_option_rows(
                    repo,
                    marketplace_id=create_store_category_marketplace_id,
                )
                if str(getattr(row, "category_path", "") or "").strip()
            ]
            create_store_category_default = [
                path
                for path in _normalize_store_category_names(
                    st.session_state.get("create_listing_ebay_store_category_names")
                )
                if path in create_store_category_options
            ]
            if _normalize_store_category_names(
                st.session_state.get("create_listing_ebay_store_category_names")
            ) != create_store_category_default:
                st.session_state["create_listing_ebay_store_category_names"] = create_store_category_default
            st.multiselect(
                "Store Categories (optional)",
                options=create_store_category_options,
                default=create_store_category_default,
                max_selections=2,
                key="create_listing_ebay_store_category_names",
                help="Optional eBay store category full paths; eBay allows up to two per offer.",
            )
            if not create_store_category_options:
                st.caption("No saved eBay store categories yet. Add one above to use it on listings.")
            ea1, ea2, ea3 = st.columns(3)
            with ea1:
                st.number_input(
                    "Auction Start Price",
                    min_value=0.0,
                    step=1.0,
                    key="create_listing_ebay_auction_start_price",
                )
            with ea2:
                st.number_input(
                    "Auction Reserve Price",
                    min_value=0.0,
                    step=1.0,
                    key="create_listing_ebay_auction_reserve_price",
                )
            with ea3:
                st.number_input(
                    "Auction Buy It Now Price",
                    min_value=0.0,
                    step=1.0,
                    key="create_listing_ebay_auction_buy_now_price",
                )
        li1, li2, li3 = st.columns(3)
        with li1:
            auto_title_from_product = st.checkbox(
                "Auto title from Product/Coin Ref if blank",
                value=True,
                key="create_listing_auto_title_from_product",
            )
        with li2:
            include_product_ai_description = st.checkbox(
                "Include Product AI Description",
                value=False,
                key="create_listing_include_ai_description",
            )
        with li3:
            include_product_ai_grading = st.checkbox(
                "Include Product AI Grading",
                value=False,
                key="create_listing_include_ai_grading",
            )
        include_product_ai_comp = st.checkbox(
            "Include Product AI Comp",
            value=False,
            key="create_listing_include_ai_comp",
        )
        include_coin_reference_context = st.checkbox(
            "Include linked Coin Reference context in listing details",
            value=True,
            key="create_listing_include_coin_ref_context",
        )

        if st.form_submit_button("Create Listing"):
            if not ensure_permission(user, "create", "Create Listing"):
                st.stop()
            if not listing_title.strip() and not auto_title_from_product:
                st.error("Listing title is required.")
            else:
                try:
                    selected_product_id = int(product_map[product_key])
                    selected_product = product_by_id.get(selected_product_id)
                    selected_coin_ref = (
                        coin_ref_by_id.get(int(selected_product.coin_reference_id))
                        if selected_product is not None and selected_product.coin_reference_id is not None
                        else None
                    )
                    if create_bundle_overcommit:
                        raise ValidationError("Lot/bundle composition exceeds selected product stock.")
                    title_seed = listing_title.strip()
                    if not title_seed and auto_title_from_product and selected_product is not None:
                        if selected_coin_ref is not None:
                            year_start = getattr(selected_coin_ref, "year_start", None)
                            year_end = getattr(selected_coin_ref, "year_end", None)
                            year_text = (
                                f"{int(year_start)}-{int(year_end)}"
                                if year_start and year_end
                                else (str(int(year_start)) if year_start else "")
                            )
                            title_seed = " ".join(
                                [
                                    str(selected_coin_ref.coin_name or "").strip(),
                                    f"({year_text})" if year_text else "",
                                    str(selected_coin_ref.denomination or "").strip(),
                                ]
                            ).strip()
                        if not title_seed:
                            title_seed = str(selected_product.title or "").strip()
                    resolved_listing_title = _render_template_placeholders(title_seed, selected_product)
                    resolved_marketplace_details = _render_template_placeholders(
                        marketplace_details.strip(),
                        selected_product,
                    )
                    detail_sections: list[str] = []
                    if resolved_marketplace_details:
                        detail_sections.append(resolved_marketplace_details)
                    if include_coin_reference_context and selected_coin_ref is not None:
                        detail_sections.append(
                            (
                                "Coin Reference Context:\n"
                                f"- Name: {selected_coin_ref.coin_name}\n"
                                f"- Country: {selected_coin_ref.country}\n"
                                f"- Series: {selected_coin_ref.series}\n"
                                f"- Denomination: {selected_coin_ref.denomination}\n"
                                f"- Metal: {selected_coin_ref.metal_type}\n"
                                f"- KM: {selected_coin_ref.km_number}\n"
                                f"- PCGS: {selected_coin_ref.pcgs_no}\n"
                                f"- NGC: {selected_coin_ref.ngc_id}\n"
                            ).strip()
                        )
                    if include_product_ai_description and selected_product is not None:
                        ai_desc = str(selected_product.ai_description or "").strip()
                        if ai_desc:
                            detail_sections.append(f"AI Description:\n{ai_desc}")
                    if include_product_ai_grading and selected_product is not None:
                        ai_grade = str(selected_product.ai_grading_description or "").strip()
                        if ai_grade:
                            detail_sections.append(f"AI Grading Notes:\n{ai_grade}")
                    if include_product_ai_comp and selected_product is not None:
                        ai_comp = str(getattr(selected_product, "ai_comp", "") or "").strip()
                        if ai_comp:
                            detail_sections.append(f"AI Comp Notes:\n{ai_comp}")
                    resolved_marketplace_details = "\n\n".join([section for section in detail_sections if section]).strip()
                    effective_listing_price = float(listing_price or 0.0)
                    if str(marketplace or "").strip().lower() == "ebay":
                        create_ebay_publish_defaults = {
                            "format_type": str(st.session_state.get("create_listing_ebay_format") or create_format_type).strip().upper(),
                            "listing_duration": str(st.session_state.get("create_listing_ebay_duration") or create_listing_duration).strip().upper(),
                            "best_offer_enabled": bool(st.session_state.get("create_listing_ebay_best_offer_enabled")),
                            "category_id": str(st.session_state.get("create_listing_ebay_category_id") or "").strip(),
                            "merchant_location_key": str(st.session_state.get("create_listing_ebay_merchant_location_key") or "").strip(),
                            "payment_policy_id": str(st.session_state.get("create_listing_ebay_payment_policy_id") or "").strip(),
                            "fulfillment_policy_id": str(st.session_state.get("create_listing_ebay_fulfillment_policy_id") or "").strip(),
                            "return_policy_id": str(st.session_state.get("create_listing_ebay_return_policy_id") or "").strip(),
                            "marketplace_id": str(st.session_state.get("create_listing_ebay_marketplace_id") or "").strip(),
                            "currency": str(st.session_state.get("create_listing_ebay_currency") or "").strip(),
                            "content_language": str(st.session_state.get("create_listing_ebay_content_language") or "").strip(),
                            "store_category_names": _normalize_store_category_names(
                                st.session_state.get("create_listing_ebay_store_category_names")
                            ),
                            "auction_start_price": float(st.session_state.get("create_listing_ebay_auction_start_price") or 0.0),
                            "auction_reserve_price": float(st.session_state.get("create_listing_ebay_auction_reserve_price") or 0.0),
                            "auction_buy_now_price": float(st.session_state.get("create_listing_ebay_auction_buy_now_price") or 0.0),
                        }
                        create_errors, create_warnings = _validate_ebay_create_publish_defaults(
                            publish_defaults=create_ebay_publish_defaults,
                            listing_price=effective_listing_price,
                        )
                        if create_errors:
                            raise ValidationError(" ".join(create_errors))
                        for warning_msg in create_warnings:
                            st.warning(warning_msg)
                        if str(create_ebay_publish_defaults.get("format_type") or "").strip().upper() == "AUCTION":
                            effective_listing_price = max(
                                float(effective_listing_price or 0.0),
                                float(create_ebay_publish_defaults.get("auction_start_price") or 0.0),
                            )
                        resolved_marketplace_details = _merge_ebay_publish_defaults_into_details(
                            resolved_marketplace_details,
                            create_ebay_publish_defaults,
                        )
                    resolved_marketplace_details = _merge_bundle_metadata(
                        resolved_marketplace_details,
                        create_bundle_metadata,
                    )
                    template_loaded_id = st.session_state.get("create_listing_template_loaded_id")
                    template_loaded_name = str(
                        st.session_state.get("create_listing_template_loaded_name") or ""
                    ).strip()
                    resolved_marketplace_details = _append_template_tracking_comment(
                        resolved_marketplace_details,
                        int(template_loaded_id) if str(template_loaded_id or "").isdigit() else None,
                        template_loaded_name,
                        settings.app_env,
                    )
                    ValidationService.validate_listing_workflow(
                        listing_title=resolved_listing_title,
                        listing_price=to_decimal(effective_listing_price),
                        quantity_listed=int(quantity_listed),
                        listing_status=listing_status,
                        media_count=len(listing_files or []),
                        external_listing_id=external_listing_id.strip(),
                        marketplace_url=marketplace_url.strip(),
                    )
                    created_listing = repo.create_listing(
                        product_id=selected_product_id,
                        marketplace=marketplace,
                        listing_title=resolved_listing_title,
                        listing_price=to_decimal(effective_listing_price),
                        quantity_listed=int(quantity_listed),
                        external_listing_id=external_listing_id.strip(),
                        marketplace_url=marketplace_url.strip(),
                        marketplace_details=resolved_marketplace_details,
                        listing_status=listing_status,
                        listed_at=datetime.combine(listed_date, datetime.min.time()),
                        actor=user.username,
                    )
                    st.success("Listing created.")
                    if listing_files:
                        if not storage.enabled:
                            st.warning(
                                "Listing created, but media upload skipped because S3 storage is not configured."
                            )
                        else:
                            uploaded, errors = upload_media_for_listing(
                                repo=repo,
                                storage=storage,
                                listing_id=created_listing.id,
                                product_id=created_listing.product_id,
                                uploaded_files=listing_files,
                                uploaded_by=listing_uploaded_by,
                            )
                            if uploaded:
                                st.success(f"Uploaded {uploaded} media file(s) to the listing.")
                            for error in errors:
                                st.error(f"Upload failed: {error}")
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))

    st.markdown("### Listing Working Set")
    ws1, ws2 = st.columns([1, 1])
    with ws1:
        load_all_listings = st.checkbox(
            "Load All Listings (slower)",
            value=False,
            key="listings_load_all_rows",
            help="When off, Listings works from the most recent rows for faster page loads.",
        )
    with ws2:
        recent_listing_limit = st.number_input(
            "Recent Listing Limit",
            min_value=50,
            max_value=5000,
            value=500,
            step=50,
            key="listings_recent_row_limit",
            disabled=bool(load_all_listings),
        )
    listings = repo.list_listings(
        limit=None if bool(load_all_listings) else int(recent_listing_limit or 500)
    )
    if load_all_listings:
        st.caption(f"Loaded all listing rows: {len(listings)}.")
    else:
        st.caption(
            f"Loaded latest {len(listings)} listing row(s). Enable `Load All Listings (slower)` "
            "for full-history filtering/export."
        )
    referenced_product_ids = {
        int(getattr(row, "product_id", 0) or 0)
        for row in listings
        if int(getattr(row, "product_id", 0) or 0) > 0
    }
    missing_product_ids = sorted(pid for pid in referenced_product_ids if pid not in product_by_id)
    if missing_product_ids:
        referenced_products = repo.list_products(product_ids=missing_product_ids, limit=len(missing_product_ids))
        for product_row in referenced_products:
            product_by_id[int(product_row.id)] = product_row
        products = list(product_by_id.values())
        st.caption(
            f"Loaded {len(referenced_products)} additional product row(s) referenced by the current listing set."
        )
    listing_media_counts: dict[int, int] = {}
    listing_publish_meta_cache: dict[int, dict] = {}

    def _listing_publish_meta_cached(listing_obj: object) -> dict:
        listing_id = int(getattr(listing_obj, "id", 0) or 0)
        if listing_id > 0 and listing_id in listing_publish_meta_cache:
            return dict(listing_publish_meta_cache[listing_id])
        parsed = _listing_publish_meta(listing_obj)
        if listing_id > 0:
            listing_publish_meta_cache[listing_id] = dict(parsed)
        return dict(parsed)

    external_listing_owner_map_cache: dict[tuple[str, str], int] | None = None

    def _get_external_listing_owner_map() -> dict[tuple[str, str], int]:
        nonlocal external_listing_owner_map_cache
        if external_listing_owner_map_cache is None:
            external_listing_owner_map_cache = {}
            for listing_obj in listings:
                market = str(getattr(listing_obj, "marketplace", "") or "").strip().lower()
                ext_id = str(getattr(listing_obj, "external_listing_id", "") or "").strip()
                listing_id = int(getattr(listing_obj, "id", 0) or 0)
                if not market or not ext_id or listing_id <= 0:
                    continue
                external_listing_owner_map_cache[(market, ext_id)] = listing_id
        return external_listing_owner_map_cache

    st.markdown("### Bulk Draft Listing Creator")
    with st.expander("Create Draft Listings From Selected Products", expanded=False):
        load_bulk_draft_creator_data = st.checkbox(
            "Load Bulk Draft Creator Data (slower)",
            value=False,
            key="listings_load_bulk_draft_creator_data",
            help="Defers product/listing candidate scans and selector hydration unless explicitly requested.",
        )
        if not load_bulk_draft_creator_data:
            st.caption(
                "Bulk draft creator data is deferred. Enable `Load Bulk Draft Creator Data (slower)` "
                "to hydrate candidate filters and selectors."
            )
        else:
            st.caption(
                "Use this for batch intake-to-listing flow. All created listings are `draft` and must be reviewed before publish."
            )
            existing_listing_pairs = {
                (int(l.product_id), str(l.marketplace or "").strip().lower())
                for l in listings
            }
            bf1, bf2, bf3 = st.columns(3)
            with bf1:
                bulk_product_query = st.text_input(
                    "Product Search",
                    value="",
                    key="listings_bulk_create_product_query",
                ).strip().lower()
            with bf2:
                bulk_category_values = {
                    str(p.category or "").strip() for p in products if str(p.category or "").strip()
                }
                bulk_category_options = list(bulk_category_values)
                if len(bulk_category_options) > 1:
                    bulk_category_options = sorted(bulk_category_options)
                bulk_categories = st.multiselect(
                    "Filter Categories",
                    options=bulk_category_options,
                    default=[],
                    key="listings_bulk_create_categories",
                )
            with bf3:
                skip_existing_pairs = st.checkbox(
                    "Skip if product already has listing on marketplace",
                    value=True,
                    key="listings_bulk_create_skip_existing",
                )
            bm1, bm2 = st.columns(2)
            with bm1:
                bulk_marketplaces = st.multiselect(
                    "Target Marketplaces",
                    options=MARKETPLACES,
                    default=["ebay"],
                    key="listings_bulk_create_marketplaces",
                )
            with bm2:
                include_ai_notes = st.checkbox(
                    "Include product AI notes in marketplace details",
                    value=False,
                    key="listings_bulk_create_include_ai_notes",
                )
                include_ai_comp_notes = st.checkbox(
                    "Include product AI comp in marketplace details",
                    value=False,
                    key="listings_bulk_create_include_ai_comp_notes",
                )
            bp1, bp2, bp3, bp4 = st.columns(4)
            with bp1:
                price_mode = st.selectbox(
                    "Price Mode",
                    options=["Acquisition Markup %", "Fixed Price"],
                    key="listings_bulk_create_price_mode",
                )
            with bp2:
                markup_pct = st.number_input(
                    "Markup %",
                    min_value=0.0,
                    value=25.0,
                    step=1.0,
                    key="listings_bulk_create_markup_pct",
                    disabled=(price_mode != "Acquisition Markup %"),
                )
            with bp3:
                fixed_price = st.number_input(
                    "Fixed Price",
                    min_value=0.0,
                    value=25.0,
                    step=1.0,
                    key="listings_bulk_create_fixed_price",
                    disabled=(price_mode != "Fixed Price"),
                )
            with bp4:
                min_price = st.number_input(
                    "Min Price Floor",
                    min_value=0.01,
                    value=1.0,
                    step=0.25,
                    key="listings_bulk_create_min_price",
                )
            bq1, bq2 = st.columns(2)
            with bq1:
                qty_mode = st.selectbox(
                    "Quantity Mode",
                    options=["Use Product Quantity", "Fixed Quantity"],
                    key="listings_bulk_create_qty_mode",
                )
            with bq2:
                fixed_qty = st.number_input(
                    "Fixed Quantity",
                    min_value=1,
                    value=1,
                    step=1,
                    key="listings_bulk_create_fixed_qty",
                    disabled=(qty_mode != "Fixed Quantity"),
                )

            bulk_category_set = {str(v).strip() for v in bulk_categories if str(v).strip()}
            candidate_products = []
            for product in products:
                if int(product.current_quantity or 0) <= 0:
                    continue
                if bulk_category_set and str(product.category or "").strip() not in bulk_category_set:
                    continue
                if bulk_product_query:
                    hay = " ".join(
                        [
                            str(product.sku or "").strip(),
                            str(product.title or "").strip(),
                            str(product.category or "").strip(),
                            str(product.metal_type or "").strip(),
                        ]
                    ).lower()
                    if bulk_product_query not in hay:
                        continue
                candidate_products.append(product)
            if len(candidate_products) > 1:
                candidate_products = sorted(
                    candidate_products,
                    key=lambda p: (str(p.title or "").lower(), int(p.id)),
                )
            candidate_options = {
                (
                    f"#{int(p.id)} | {p.sku} | {p.title} | "
                    f"qty={int(p.current_quantity or 0)} | cat={str(p.category or '').strip() or '-'}"
                ): int(p.id)
                for p in candidate_products
            }
            selected_candidate_keys = st.multiselect(
                "Select Products",
                options=list(candidate_options.keys()),
                key="listings_bulk_create_selected_products",
            )
            if st.button("Create Draft Listings For Selected Products", key="listings_bulk_create_execute_btn"):
                if not ensure_permission(user, "create", "Bulk Create Draft Listings"):
                    st.stop()
                if not selected_candidate_keys:
                    st.error("Select at least one product.")
                elif not bulk_marketplaces:
                    st.error("Select at least one marketplace.")
                else:
                    selected_product_ids = [candidate_options[k] for k in selected_candidate_keys if k in candidate_options]
                    created_count = 0
                    skipped_count = 0
                    error_count = 0
                    for product_id in selected_product_ids:
                        product = product_by_id.get(int(product_id))
                        if product is None:
                            continue
                        for marketplace in bulk_marketplaces:
                            pair = (int(product.id), str(marketplace).strip().lower())
                            if skip_existing_pairs and pair in existing_listing_pairs:
                                skipped_count += 1
                                continue
                            try:
                                if price_mode == "Fixed Price":
                                    resolved_price = max(float(min_price), float(fixed_price))
                                else:
                                    base_cost = float(product.acquisition_cost or 0.0)
                                    resolved_price = max(float(min_price), base_cost * (1.0 + float(markup_pct) / 100.0))
                                resolved_qty = (
                                    max(1, int(fixed_qty))
                                    if qty_mode == "Fixed Quantity"
                                    else max(1, int(product.current_quantity or 1))
                                )
                                details_parts: list[str] = []
                                if include_ai_notes:
                                    ai_desc = str(product.ai_description or "").strip()
                                    ai_grade = str(product.ai_grading_description or "").strip()
                                    if ai_desc:
                                        details_parts.append(f"AI Description:\n{ai_desc}")
                                    if ai_grade:
                                        details_parts.append(f"AI Grading Notes:\n{ai_grade}")
                                if include_ai_comp_notes:
                                    ai_comp = str(getattr(product, "ai_comp", "") or "").strip()
                                    if ai_comp:
                                        details_parts.append(f"AI Comp Notes:\n{ai_comp}")
                                repo.create_listing(
                                    product_id=int(product.id),
                                    marketplace=str(marketplace).strip().lower(),
                                    listing_title=str(product.title or "").strip() or f"Product #{int(product.id)}",
                                    listing_price=to_decimal(resolved_price),
                                    quantity_listed=int(resolved_qty),
                                    marketplace_details="\n\n".join(details_parts).strip(),
                                    listing_status="draft",
                                    actor=user.username,
                                )
                                existing_listing_pairs.add(pair)
                                created_count += 1
                            except Exception:
                                error_count += 1
                    if created_count:
                        st.success(f"Created {created_count} draft listing(s).")
                    if skipped_count:
                        st.info(f"Skipped {skipped_count} product/marketplace pair(s) due to existing listings.")
                    if error_count:
                        st.error(f"{error_count} listing(s) failed to create.")
                    st.rerun()

    default_format_type = get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE").strip().upper()
    default_auction_duration = get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_5").strip().upper()
    load_listing_format_diagnostics = st.checkbox(
        "Load Listing Format Diagnostics (slower)",
        value=False,
        key="listings_load_format_diagnostics",
        help="Defers per-listing eBay format/publish metadata parsing used for format_hint diagnostics.",
    )
    if not load_listing_format_diagnostics:
        st.caption(
            "Listing format diagnostics are deferred. Enable `Load Listing Format Diagnostics (slower)` "
            "to compute per-listing format hints."
        )
    load_listing_media_counts = st.checkbox(
        "Load Listing Media Counts (slower)",
        value=False,
        key="listings_load_media_counts",
        help="Defers per-listing media relationship hydration in the main listing table.",
    )
    if not load_listing_media_counts:
        st.caption(
            "Listing media counts are deferred. Enable `Load Listing Media Counts (slower)` to hydrate exact counts."
        )
    else:
        listing_ids = [int(getattr(row, "id", 0) or 0) for row in listings if int(getattr(row, "id", 0) or 0) > 0]
        listing_media_counts = repo.listing_media_count_map(listing_ids=listing_ids)
    listing_obj_by_id: dict[int, object] = {int(getattr(l, "id", 0) or 0): l for l in listings}
    listing_media_rows_cache: dict[int, list[object]] = {}

    def _listing_media_rows_cached(listing_id: int) -> list[object]:
        resolved_listing_id = int(listing_id or 0)
        if resolved_listing_id <= 0:
            return []
        if resolved_listing_id not in listing_media_rows_cache:
            listing_media_rows_cache[resolved_listing_id] = list(
                repo.list_media_assets_for_listing(resolved_listing_id)
            )
        return list(listing_media_rows_cache[resolved_listing_id])

    def _listing_media_count_cached(listing_id: int) -> int:
        resolved_listing_id = int(listing_id or 0)
        if resolved_listing_id <= 0:
            return 0
        if resolved_listing_id in listing_media_rows_cache:
            return len(listing_media_rows_cache[resolved_listing_id])
        if load_listing_media_counts:
            return int(listing_media_counts.get(resolved_listing_id, 0))
        try:
            return int(repo.count_media_assets_for_listing(resolved_listing_id))
        except Exception:
            return 0

    listing_format_diagnostics_cache: dict[int, tuple[str, str]] = {}

    def _resolve_listing_format_diagnostics(listing_obj: object) -> tuple[str, str]:
        listing_id = int(getattr(listing_obj, "id", 0) or 0)
        if listing_id > 0 and listing_id in listing_format_diagnostics_cache:
            return listing_format_diagnostics_cache[listing_id]
        format_type = ""
        format_hint = ""
        if str(getattr(listing_obj, "marketplace", "") or "").strip().lower() == "ebay":
            publish_meta = _listing_publish_meta_cached(listing_obj)
            format_type = str(
                publish_meta.get("format")
                or publish_meta.get("format_type")
                or default_format_type
                or "FIXED_PRICE"
            ).strip().upper()
            if format_type not in {"FIXED_PRICE", "AUCTION"}:
                format_type = "FIXED_PRICE"
            auction_duration = str(
                publish_meta.get("listing_duration")
                or ("GTC" if format_type == "FIXED_PRICE" else default_auction_duration)
            ).strip().upper()
            auction_start_price = _to_float(
                publish_meta.get("auction_start_price"),
                float(getattr(listing_obj, "listing_price", 0) or 0),
            )
            auction_reserve_price = _to_float(publish_meta.get("auction_reserve_price"), 0.0)
            auction_buy_now_price = _to_float(publish_meta.get("auction_buy_now_price"), 0.0)
            format_hints: list[str] = []
            if format_type == "FIXED_PRICE":
                if float(getattr(listing_obj, "listing_price", 0) or 0) <= 0:
                    format_hints.append("Fixed Missing BIN")
            else:
                if float(auction_start_price or 0) <= 0:
                    format_hints.append("Auction Missing Start")
                if auction_duration not in {"DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"}:
                    format_hints.append("Auction Missing Duration")
                if float(auction_reserve_price or 0) > 0 and float(auction_reserve_price or 0) < float(
                    auction_start_price or 0
                ):
                    format_hints.append("Reserve < Start")
                if float(auction_buy_now_price or 0) > 0 and float(auction_buy_now_price or 0) < float(
                    auction_start_price or 0
                ):
                    format_hints.append("BIN < Start")
            format_hint = "; ".join(format_hints)
        resolved = (format_type, format_hint)
        if listing_id > 0:
            listing_format_diagnostics_cache[listing_id] = resolved
        return resolved

    def _hydrate_rows_with_format_diagnostics(rows: list[dict]) -> None:
        for row in rows:
            listing_id = int(row.get("id") or 0)
            listing_obj = listing_obj_by_id.get(listing_id)
            if listing_obj is None:
                row["format_type"] = ""
                row["format_hint"] = ""
                continue
            format_type, format_hint = _resolve_listing_format_diagnostics(listing_obj)
            row["format_type"] = format_type
            row["format_hint"] = format_hint

    photo_comp_listing_ids: set[int] = set()

    def _listing_origin_for_obj(listing_obj: object) -> str:
        if not bool(load_photo_comp_origin):
            return "other"
        listing_id = int(getattr(listing_obj, "id", 0) or 0)
        return "photo_comp_draft" if listing_id in photo_comp_listing_ids else "other"

    def _build_listing_rows(listing_subset: list[object]) -> list[dict]:
        rows: list[dict] = []
        for l in listing_subset:
            origin = _listing_origin_for_obj(l)
            rows.append(
                {
                    "id": l.id,
                    "product_id": l.product_id,
                    "marketplace": l.marketplace,
                    "external_listing_id": l.external_listing_id,
                    "marketplace_url": l.marketplace_url,
                    "title": l.listing_title,
                    "price": float(l.listing_price),
                    "qty": l.quantity_listed,
                    "listed_at": iso_or_none(l.listed_at),
                    "status": l.listing_status,
                    "archived": bool(_listing_is_archived(l)),
                    "format_type": "",
                    "format_hint": "",
                    "review_status": (l.review_status or "pending"),
                    "reviewed_at": iso_or_none(l.reviewed_at),
                    "reviewed_by": (l.reviewed_by or ""),
                    "origin": origin,
                    "origin_label": "Photo-Comp Draft" if origin == "photo_comp_draft" else "Other",
                    "media_count": (
                        int(listing_media_counts.get(int(getattr(l, "id", 0) or 0), 0))
                        if load_listing_media_counts
                        else None
                    ),
                }
            )
        return rows

    load_photo_comp_origin = st.checkbox(
        "Load Photo-Comp Origin Tags (slower)",
        value=False,
        key="listings_load_photo_comp_origin_tags",
        help="Defers the 5k audit-log scan used to tag listings created from photo-comp flow.",
    )
    if load_photo_comp_origin:
        photo_comp_listing_ids = _photo_comp_created_listing_ids(
            repo,
            audit_rows=_get_audit_logs(5000),
        )
    else:
        st.caption(
            "Photo-comp origin tagging is deferred. Enable `Load Photo-Comp Origin Tags (slower)` to compute origin labels."
        )
    st.markdown("### eBay Template Usage")
    load_template_usage = st.checkbox(
        "Load Template Usage (slower)",
        value=False,
        key="listings_load_template_usage",
        help="Defers template tracking scan across listings unless explicitly requested.",
    )
    template_usage_rows = []
    if not load_template_usage:
        st.caption(
            "Template usage is deferred. Enable `Load Template Usage (slower)` to query listing/template usage stats."
        )
    else:
        for l in listings:
            template_id, template_name = _extract_template_tracking_comment(l.marketplace_details or "")
            if not template_id:
                continue
            template_usage_rows.append(
                {
                    "listing_id": l.id,
                    "template_id": template_id,
                    "template_name": template_name or f"Template #{template_id}",
                    "marketplace": l.marketplace,
                    "listing_title": l.listing_title,
                    "listed_at": iso_or_none(l.listed_at),
                }
            )
        if template_usage_rows:
            usage_df = pd.DataFrame(template_usage_rows)
            counts_df = (
                usage_df.groupby(["template_id", "template_name"], as_index=False)
                .size()
                .rename(columns={"size": "usage_count"})
                .sort_values(["usage_count", "template_name"], ascending=[False, True])
            )
            u1, u2 = st.columns(2)
            with u1:
                st.dataframe(counts_df, use_container_width=True)
            with u2:
                st.dataframe(
                    usage_df.sort_values("listed_at", ascending=False).head(25),
                    use_container_width=True,
                )
        else:
            st.info("No eBay template usage found in the current listing set.")

    handoff_from = str(st.session_state.get("workspace_handoff_from") or "").strip().lower()
    handoff_target = str(st.session_state.get("workspace_handoff_target") or "").strip().lower()
    handoff_active = handoff_from in {"ebay_workspace", "operations_home"} and handoff_target == "listings"
    auto_photo_comp_queue_enabled = get_runtime_bool(
        repo,
        "ux_listings_auto_photo_comp_review_preset",
        False,
    )
    auto_preset_key = f"listings_auto_photo_comp_preset_applied::{settings.app_env}::{user.username}"
    if auto_photo_comp_queue_enabled and not handoff_active and not bool(st.session_state.get(auto_preset_key)):
        st.session_state["listings_filter_query"] = ""
        st.session_state["listings_filter_marketplaces"] = ["ebay"]
        st.session_state["listings_filter_status"] = ["draft"]
        st.session_state["listings_filter_origin"] = "photo_comp_draft"
        st.session_state[auto_preset_key] = True
        st.rerun()
    if handoff_active:
        h1, h2 = st.columns([4, 1])
        with h1:
            if handoff_from == "operations_home":
                st.info(
                    "Opened from Operations Home Photo-Comp queue context. "
                    "Filters were preloaded for photo-comp draft review."
                )
            else:
                st.info("Opened from eBay Workspace context. Filters were preloaded for eBay listing operations.")
        with h2:
            if st.button("Clear Handoff", key="listings_clear_handoff_btn", use_container_width=True):
                try:
                    repo.record_audit_event(
                        entity_type="navigation",
                        entity_id=None,
                        action="workspace_handoff_cleared",
                        actor=user.username,
                        changes={
                            "from": handoff_from,
                            "target": "listings",
                            "cleared_marketplaces": st.session_state.get("listings_filter_marketplaces") or [],
                            "cleared_statuses": st.session_state.get("listings_filter_status") or [],
                            "cleared_query": st.session_state.get("listings_filter_query") or "",
                            "cleared_origin": st.session_state.get("listings_filter_origin") or "all",
                        },
                    )
                except Exception:
                    pass
                st.session_state["listings_filter_marketplaces"] = []
                st.session_state["listings_filter_status"] = []
                st.session_state["listings_filter_query"] = ""
                st.session_state["listings_filter_origin"] = "all"
                st.session_state["workspace_handoff_from"] = ""
                st.session_state["workspace_handoff_target"] = ""
                st.rerun()

    st.markdown("### Listing Filters")
    marketplace_filter_values = {
        str(getattr(row, "marketplace", "") or "").strip()
        for row in listings
        if str(getattr(row, "marketplace", "") or "").strip()
    }
    marketplace_filter_options = list(marketplace_filter_values)
    if len(marketplace_filter_options) > 1:
        marketplace_filter_options = sorted(marketplace_filter_options)
    status_filter_values = {
        str(getattr(row, "listing_status", "") or "").strip()
        for row in listings
        if str(getattr(row, "listing_status", "") or "").strip()
    }
    status_filter_options = list(status_filter_values)
    if len(status_filter_options) > 1:
        status_filter_options = sorted(status_filter_options)
    # Streamlit multiselect raises if session-state values are not present in options.
    # Sanitize filter state before widget construction.
    sanitized_marketplaces = normalize_multiselect_values(
        st.session_state.get("listings_filter_marketplaces"),
        marketplace_filter_options,
    )
    sanitized_statuses = normalize_multiselect_values(
        st.session_state.get("listings_filter_status"),
        status_filter_options,
    )
    if list(st.session_state.get("listings_filter_marketplaces") or []) != sanitized_marketplaces:
        st.session_state["listings_filter_marketplaces"] = sanitized_marketplaces
    if list(st.session_state.get("listings_filter_status") or []) != sanitized_statuses:
        st.session_state["listings_filter_status"] = sanitized_statuses
    pending_filter_apply_key = "listings_filter_pending_apply"
    pending_filter_apply = st.session_state.get(pending_filter_apply_key)
    if isinstance(pending_filter_apply, dict):
        pending_marketplaces = normalize_multiselect_values(
            pending_filter_apply.get("marketplaces"),
            marketplace_filter_options,
        )
        pending_statuses = normalize_multiselect_values(
            pending_filter_apply.get("statuses"),
            status_filter_options,
        )
        pending_origin = str(pending_filter_apply.get("origin") or "all").strip().lower()
        if pending_origin not in {"all", "photo_comp_draft", "other"}:
            pending_origin = "all"
        st.session_state["listings_filter_query"] = str(pending_filter_apply.get("query") or "")
        st.session_state["listings_filter_marketplaces"] = list(pending_marketplaces)
        st.session_state["listings_filter_status"] = list(pending_statuses)
        st.session_state["listings_filter_origin"] = pending_origin
        st.session_state["listings_filter_format_issue_only"] = bool(
            pending_filter_apply.get("format_issue_only")
        )
        st.session_state["listings_filter_include_archived"] = bool(
            pending_filter_apply.get("include_archived")
        )
        st.session_state.pop(pending_filter_apply_key, None)

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        listing_filter_query = st.text_input("Search Title / External ID", key="listings_filter_query")
    with f2:
        listing_filter_marketplaces = st.multiselect(
            "Marketplace",
            options=marketplace_filter_options,
            key="listings_filter_marketplaces",
        )
    with f3:
        listing_filter_status = st.multiselect(
            "Status",
            options=status_filter_options,
            key="listings_filter_status",
        )
    with f4:
        listing_filter_origin = st.selectbox(
            "Origin",
            options=["all", "photo_comp_draft", "other"],
            index=0,
            key="listings_filter_origin",
            help="Filter listings created from Photo-Comp draft flow.",
        )
    listing_filter_marketplaces = normalize_multiselect_values(
        listing_filter_marketplaces,
        marketplace_filter_options,
    )
    listing_filter_status = normalize_multiselect_values(
        listing_filter_status,
        status_filter_options,
    )
    listing_filter_format_issue_only = st.checkbox(
        "Format Issue Only",
        value=False,
        key="listings_filter_format_issue_only",
        help="Show only listings with non-empty format_hint (fixed/auction setup issues).",
    )
    listing_filter_include_archived = st.checkbox(
        "Include Archived",
        value=False,
        key="listings_filter_include_archived",
        help="Show archived listings in table and side panel selection.",
    )
    effective_filter = render_saved_filter_bar(
        repo=repo,
        scope="listings",
        username=user.username,
        current_filters={
            "query": listing_filter_query,
            "marketplaces": listing_filter_marketplaces,
            "statuses": listing_filter_status,
            "origin": listing_filter_origin,
            "format_issue_only": bool(listing_filter_format_issue_only),
            "include_archived": bool(listing_filter_include_archived),
        },
    )
    preset_payload = {
        "query": "",
        "marketplaces": ["ebay"],
        "statuses": ["draft"],
        "origin": "photo_comp_draft",
        "format_issue_only": False,
        "include_archived": False,
    }
    format_fix_preset_payload = {
        "query": "",
        "marketplaces": ["ebay"],
        "statuses": ["draft", "active"],
        "origin": "all",
        "format_issue_only": True,
        "include_archived": False,
    }
    pf1, pf2, pf3, pf4 = st.columns(4)
    with pf1:
        if st.button("Use Photo-Comp Review Queue", key="listings_use_photo_comp_review_preset"):
            st.session_state[pending_filter_apply_key] = dict(preset_payload)
            st.success("Applied Photo-Comp Review Queue preset.")
            st.rerun()
    with pf2:
        if st.button("Save Team Preset: Photo-Comp Review Queue", key="listings_save_photo_comp_review_preset"):
            if ensure_permission(user, "create", "Save Photo-Comp Review Preset"):
                try:
                    repo.upsert_saved_filter_profile(
                        environment=settings.app_env,
                        username=user.username,
                        scope="listings",
                        name="Photo-Comp Review Queue",
                        filter_json=json.dumps(preset_payload),
                        is_shared=True,
                        is_default=False,
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Saved team preset `Photo-Comp Review Queue`.")
                except Exception as exc:
                    st.error(f"Unable to save preset: {exc}")
    with pf3:
        if st.button("Use Format Fix Queue", key="listings_use_format_fix_queue_preset"):
            st.session_state[pending_filter_apply_key] = dict(format_fix_preset_payload)
            st.success("Applied Format Fix Queue preset.")
            st.rerun()
    with pf4:
        if st.button("Save Team Preset: Format Fix Queue", key="listings_save_format_fix_queue_preset"):
            if ensure_permission(user, "create", "Save Format Fix Queue Preset"):
                try:
                    repo.upsert_saved_filter_profile(
                        environment=settings.app_env,
                        username=user.username,
                        scope="listings",
                        name="Format Fix Queue",
                        filter_json=json.dumps(format_fix_preset_payload),
                        is_shared=True,
                        is_default=False,
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Saved team preset `Format Fix Queue`.")
                except Exception as exc:
                    st.error(f"Unable to save preset: {exc}")
    q = str(effective_filter.get("query") or "").strip().lower()
    marketplaces: set[str] = set()
    for raw_value in (effective_filter.get("marketplaces") or []):
        normalized_value = str(raw_value or "").strip().lower()
        if normalized_value:
            marketplaces.add(normalized_value)
    statuses: set[str] = set()
    for raw_value in (effective_filter.get("statuses") or []):
        normalized_value = str(raw_value or "").strip().lower()
        if normalized_value:
            statuses.add(normalized_value)
    origin_filter = str(effective_filter.get("origin") or "all").strip().lower()
    format_issue_only = bool(effective_filter.get("format_issue_only"))
    include_archived = bool(effective_filter.get("include_archived"))
    diagnostics_required = bool(format_issue_only or load_listing_format_diagnostics)
    if format_issue_only and not bool(load_listing_format_diagnostics):
        st.info(
            "`Format Issue Only` requires listing format diagnostics. "
            "Diagnostics were enabled automatically for this run."
        )
    st.markdown("### Listing Table + Side Panel")
    lt1, lt2 = st.columns(2)
    with lt1:
        listings_render_full_tables = st.checkbox(
            "Render Full Listings Tables",
            value=False,
            key="listings_render_full_tables",
            help="When off, large listings tables render previews for faster load.",
        )
    with lt2:
        listings_preview_row_limit = st.number_input(
            "Listings Preview Row Limit",
            min_value=10,
            max_value=1000,
            value=150,
            step=10,
            key="listings_preview_row_limit",
        )
    base_filtered_listing_objs = _filter_listing_objects_base(
        listings,
        query=q,
        marketplaces=marketplaces,
        statuses=statuses,
        origin_filter=origin_filter,
        include_archived=include_archived,
        resolve_origin=_listing_origin_for_obj,
    )
    base_filtered_rows = _build_listing_rows(base_filtered_listing_objs)
    diagnostics_rows = base_filtered_rows
    if diagnostics_required and not format_issue_only and not bool(listings_render_full_tables):
        diagnostics_rows = base_filtered_rows[: int(listings_preview_row_limit)]
        if bool(load_listing_format_diagnostics) and len(base_filtered_rows) > len(diagnostics_rows):
            st.caption(
                "Format diagnostics are hydrated for preview rows only. "
                "Enable `Render Full Listings Tables` to hydrate diagnostics across all filtered rows."
            )
    _maybe_hydrate_listing_format_diagnostics(
        diagnostics_rows,
        diagnostics_required=diagnostics_required,
        hydrate_rows=_hydrate_rows_with_format_diagnostics,
    )
    filtered_rows = (
        _filter_listing_rows_with_format_issues(base_filtered_rows)
        if format_issue_only
        else base_filtered_rows
    )
    filtered_total_rows = int(len(filtered_rows))
    filtered_render_rows = (
        filtered_rows
        if bool(listings_render_full_tables)
        else filtered_rows[: int(listings_preview_row_limit)]
    )
    filtered_df = pd.DataFrame(filtered_render_rows)
    filtered_export_df_cache: pd.DataFrame | None = None

    def _filtered_export_df() -> pd.DataFrame:
        nonlocal filtered_export_df_cache
        if filtered_export_df_cache is None:
            filtered_export_df_cache = (
                filtered_df if bool(listings_render_full_tables) else pd.DataFrame(filtered_rows)
            )
        return filtered_export_df_cache

    def _render_listings_df(df: pd.DataFrame, *, hide_index: bool = False, total_rows: int | None = None) -> None:
        if bool(listings_render_full_tables):
            st.dataframe(df, use_container_width=True, hide_index=hide_index)
            return
        preview_limit = int(listings_preview_row_limit)
        total_rows_resolved = int(total_rows) if total_rows is not None else int(len(df.index))
        preview_df = df if int(len(df.index)) <= preview_limit else df.head(preview_limit)
        st.dataframe(preview_df, use_container_width=True, hide_index=hide_index)
        if total_rows_resolved > preview_limit:
            st.caption(
                f"Showing preview rows: {preview_limit} / {total_rows_resolved}. "
                "Enable `Render Full Listings Tables` to render all rows."
            )

    table_col, panel_col = st.columns([2, 1])
    with table_col:
        channel_adapters_cache: object | None = None

        def _get_channel_adapters() -> object:
            nonlocal channel_adapters_cache
            if channel_adapters_cache is None:
                channel_adapters_cache = build_channel_adapters()
            return channel_adapters_cache

        load_deep_queue_analytics = st.checkbox(
            "Load Deep Queue Analytics (slower)",
            value=False,
            key="listings_load_deep_queue_analytics",
            help=(
                "Master gate for heavy readiness-adjacent history scans "
                "(follow-up audit history and bulk publish metadata history)."
            ),
        )
        if not load_deep_queue_analytics:
            st.caption(
                "Deep queue analytics are deferred. Enable `Load Deep Queue Analytics (slower)` "
                "to access follow-up history and bulk publish history scans."
            )
        render_table_toolbar(
            df=filtered_df,
            section_key="listings_table",
            export_basename="listings_filtered",
            defer_exports=True,
            row_count=filtered_total_rows,
            export_df_factory=_filtered_export_df,
            active_filters={
                "query": q,
                "marketplaces": (
                    sorted(marketplaces) if len(marketplaces) > 1 else list(marketplaces)
                ),
                "statuses": sorted(statuses) if len(statuses) > 1 else list(statuses),
                "origin": origin_filter,
                "format_issue_only": bool(format_issue_only),
                "include_archived": bool(include_archived),
            },
        )
        _render_listings_df(filtered_df, total_rows=filtered_total_rows)
        load_listings_row_actions = st.checkbox(
            "Load Listing Row Actions (slower)",
            value=False,
            key="listings_load_row_actions",
            help="Defers bulk row-action control hydration for large filtered sets unless explicitly requested.",
        )
        if not load_listings_row_actions:
            st.caption(
                "Listing row actions are deferred. Enable `Load Listing Row Actions (slower)` "
                "to hydrate archive/restore/row action controls for the filtered set."
            )
        else:
            render_standard_row_actions(
                repo,
                entity_type="listing",
                rows=filtered_rows,
                id_field="id",
                title="Listing Row Actions",
            )

        st.markdown("### eBay Readiness Queue")
        load_readiness_queue = st.checkbox(
            "Load eBay Readiness Queue (slower)",
            value=False,
            key="listings_load_readiness_queue",
            help="Defers readiness scoring and blocker/warning queue computations unless explicitly requested.",
        )
        if not load_readiness_queue:
            st.caption(
                "Readiness queue is deferred. Enable `Load eBay Readiness Queue (slower)` to run readiness scoring and queue analytics."
            )
        preset_rows_for_readiness_cache: list[object] | None = None
        default_preset_cache: object | None = None

        def _get_default_publish_preset() -> object | None:
            nonlocal preset_rows_for_readiness_cache, default_preset_cache
            if preset_rows_for_readiness_cache is None:
                preset_rows_for_readiness_cache = _list_ebay_publish_presets_cached(active_only=True)
                if preset_rows_for_readiness_cache:
                    default_preset_cache = preset_rows_for_readiness_cache[0]
                    for preset_row in preset_rows_for_readiness_cache:
                        if bool(getattr(preset_row, "is_default", False)):
                            default_preset_cache = preset_row
                            break
            return default_preset_cache

        ebay_active_listings = (
            [
                l
                for l in listings
                if (l.marketplace or "").strip().lower() == "ebay" and not _listing_is_archived(l)
            ]
            if load_readiness_queue
            else []
        )
        load_readiness_evaluation = bool(load_readiness_queue and len(ebay_active_listings) > 0)
        if load_readiness_queue and ebay_active_listings:
            load_readiness_evaluation = st.checkbox(
                "Load Readiness Evaluation (slower)",
                value=False,
                key="listings_load_readiness_evaluation",
                help="Defers per-listing readiness scoring row-build work unless explicitly requested.",
            )
            if not load_readiness_evaluation:
                st.caption(
                    "Readiness evaluation is deferred. Enable `Load Readiness Evaluation (slower)` "
                    "to score listings and hydrate readiness rows."
                )

        default_runtime_format = "FIXED_PRICE"
        default_runtime_auction_duration = "DAYS_5"
        default_preset: object | None = None
        default_category_id = ""
        default_merchant_location_key = ""
        default_payment_policy_id = ""
        default_fulfillment_policy_id = ""
        default_return_policy_id = ""
        runtime_defaults_for_readiness_cache: dict[str, object] | None = None

        def _get_runtime_defaults_for_readiness() -> dict[str, object]:
            nonlocal runtime_defaults_for_readiness_cache
            if runtime_defaults_for_readiness_cache is None:
                runtime_defaults_for_readiness_cache = get_runtime_values(
                    repo,
                    {
                        "ebay_listing_format_default": "FIXED_PRICE",
                        "ebay_auction_duration_default": "DAYS_5",
                        "ebay_user_access_token": settings.ebay_user_access_token,
                        "ebay_marketplace_id": settings.ebay_marketplace_id,
                        "ebay_currency": settings.ebay_currency,
                        "ebay_content_language": settings.ebay_content_language,
                        "ebay_merchant_location_key": settings.ebay_merchant_location_key,
                        "ebay_payment_policy_id": settings.ebay_payment_policy_id,
                        "ebay_fulfillment_policy_id": settings.ebay_fulfillment_policy_id,
                        "ebay_return_policy_id": settings.ebay_return_policy_id,
                    },
                )
            return dict(runtime_defaults_for_readiness_cache)

        if load_readiness_evaluation:
            default_preset = _get_default_publish_preset()
            runtime_defaults_for_readiness = _get_runtime_defaults_for_readiness()
            default_runtime_format = str(
                runtime_defaults_for_readiness.get("ebay_listing_format_default") or "FIXED_PRICE"
            ).strip()
            default_runtime_auction_duration = str(
                runtime_defaults_for_readiness.get("ebay_auction_duration_default") or "DAYS_5"
            ).strip()
            default_category_id = (getattr(default_preset, "category_id", "") if default_preset else "") or ""
            default_merchant_location_key = (
                getattr(default_preset, "merchant_location_key", "") if default_preset else ""
            ) or ""
            default_payment_policy_id = (getattr(default_preset, "payment_policy_id", "") if default_preset else "") or ""
            default_fulfillment_policy_id = (
                getattr(default_preset, "fulfillment_policy_id", "") if default_preset else ""
            ) or ""
            default_return_policy_id = (getattr(default_preset, "return_policy_id", "") if default_preset else "") or ""
        listing_by_id = dict(listing_obj_by_id)
        listing_details_cache: dict[int, dict] = {}

        def _listing_marketplace_details_json(listing_obj: object) -> dict:
            listing_id = int(getattr(listing_obj, "id", 0) or 0)
            cached = listing_details_cache.get(listing_id)
            if cached is not None:
                return dict(cached)
            details_raw = str(getattr(listing_obj, "marketplace_details", "") or "").strip()
            if not details_raw:
                parsed: dict = {}
            else:
                try:
                    loaded = json.loads(details_raw)
                    parsed = dict(loaded) if isinstance(loaded, dict) else {"notes": details_raw}
                except Exception:
                    parsed = {"notes": details_raw}
            listing_details_cache[listing_id] = dict(parsed)
            return dict(parsed)

        bulk_publish_defaults_cache: dict[str, str] | None = None

        def _resolve_bulk_publish_defaults() -> dict[str, str]:
            nonlocal bulk_publish_defaults_cache
            if bulk_publish_defaults_cache is None:
                default_preset = _get_default_publish_preset()
                runtime_defaults_for_readiness = _get_runtime_defaults_for_readiness()
                bulk_publish_defaults_cache = {
                    "access_token": str(runtime_defaults_for_readiness.get("ebay_user_access_token") or "").strip(),
                    "marketplace_id": str(runtime_defaults_for_readiness.get("ebay_marketplace_id") or "").strip(),
                    "currency": str(runtime_defaults_for_readiness.get("ebay_currency") or "").strip(),
                    "content_language": str(runtime_defaults_for_readiness.get("ebay_content_language") or "").strip(),
                    "merchant_location_key": (
                        (default_preset.merchant_location_key if default_preset else "")
                        or str(runtime_defaults_for_readiness.get("ebay_merchant_location_key") or "")
                    ).strip(),
                    "payment_policy_id": (
                        (default_preset.payment_policy_id if default_preset else "")
                        or str(runtime_defaults_for_readiness.get("ebay_payment_policy_id") or "")
                    ).strip(),
                    "fulfillment_policy_id": (
                        (default_preset.fulfillment_policy_id if default_preset else "")
                        or str(runtime_defaults_for_readiness.get("ebay_fulfillment_policy_id") or "")
                    ).strip(),
                    "return_policy_id": (
                        (default_preset.return_policy_id if default_preset else "")
                        or str(runtime_defaults_for_readiness.get("ebay_return_policy_id") or "")
                    ).strip(),
                    "category_id": ((default_preset.category_id if default_preset else "") or "").strip(),
                }
            return dict(bulk_publish_defaults_cache)

        readiness_rows: list[dict] = []
        readiness_row_by_listing_id: dict[int, dict] = {}
        readiness_created_at_by_listing_id: dict[int, datetime] = {}
        readiness_category_aspects_cache: dict[tuple[str, str], list[dict]] = {}

        def _cached_readiness_category_aspects(category_id_value: str) -> list[dict]:
            category_id_clean = str(category_id_value or "").strip()
            if not category_id_clean:
                return []
            runtime_defaults_for_readiness = _get_runtime_defaults_for_readiness()
            marketplace_id_clean = str(
                runtime_defaults_for_readiness.get("ebay_marketplace_id") or settings.ebay_marketplace_id or "EBAY_US"
            ).strip()
            cache_key = (marketplace_id_clean.upper(), category_id_clean)
            if cache_key in readiness_category_aspects_cache:
                return list(readiness_category_aspects_cache.get(cache_key) or [])
            cached = repo.get_cached_ebay_category_aspects(
                environment=settings.app_env,
                marketplace_id=marketplace_id_clean,
                category_id=category_id_clean,
            )
            rows = normalize_ebay_category_aspect_rows((cached or {}).get("aspects") or []) if cached else []
            readiness_category_aspects_cache[cache_key] = list(rows)
            return list(rows)

        for listing in (ebay_active_listings if load_readiness_evaluation else []):
            publish_meta = _listing_publish_meta_cached(listing)
            format_type = str(
                publish_meta.get("format")
                or publish_meta.get("format_type")
                or (default_preset.format_type if default_preset else "")
                or default_runtime_format
            ).strip().upper()
            if format_type not in {"FIXED_PRICE", "AUCTION"}:
                format_type = "FIXED_PRICE"
            listing_duration = str(
                publish_meta.get("listing_duration")
                or (default_preset.listing_duration if default_preset else "")
                or ("GTC" if format_type == "FIXED_PRICE" else default_runtime_auction_duration)
            ).strip().upper()
            auction_start_price = _to_float(
                publish_meta.get("auction_start_price"),
                float(listing.listing_price or 0),
            )
            auction_reserve_price = _to_float(publish_meta.get("auction_reserve_price"), 0.0)
            auction_buy_now_price = _to_float(publish_meta.get("auction_buy_now_price"), 0.0)
            best_offer_enabled = bool(publish_meta.get("best_offer_enabled"))
            readiness_category_id = str(publish_meta.get("category_id") or default_category_id or "").strip()
            readiness_aspects = publish_meta.get("aspects")
            if not isinstance(readiness_aspects, dict):
                readiness_aspects = _normalize_aspects_payload(str(publish_meta.get("aspects_json") or ""))
            readiness = evaluate_ebay_readiness(
                listing_title=listing.listing_title,
                listing_description=str(
                    publish_meta.get("listing_description")
                    or publish_meta.get("description")
                    or listing.listing_title
                    or ""
                ),
                listing_price=float(listing.listing_price or 0),
                auction_start_price=float(auction_start_price or 0),
                auction_reserve_price=float(auction_reserve_price or 0),
                auction_buy_now_price=float(auction_buy_now_price or 0),
                quantity_listed=int(listing.quantity_listed or 0),
                listing_status=listing.listing_status,
                format_type=format_type,
                listing_duration=listing_duration,
                media_count=int(listing_media_counts.get(int(listing.id), 0)),
                category_id=readiness_category_id,
                merchant_location_key=default_merchant_location_key,
                payment_policy_id=default_payment_policy_id,
                fulfillment_policy_id=default_fulfillment_policy_id,
                return_policy_id=default_return_policy_id,
                aspects=readiness_aspects,
                condition_description=str(publish_meta.get("condition_description") or ""),
                category_aspects=_cached_readiness_category_aspects(readiness_category_id),
            )
            review_status = (listing.review_status or "pending").strip().lower()
            blockers = list(readiness.blockers)
            warnings = list(readiness.warnings)
            if format_type == "AUCTION" and best_offer_enabled:
                warnings.append("Best Offer is ignored for auction format")
            if review_status != "approved":
                blockers.append("Listing review must be approved before publish.")
            product = product_by_id.get(int(listing.product_id or 0))
            row = {
                "listing_id": listing.id,
                "sku": (product.sku if product else ""),
                "title": listing.listing_title,
                "status": listing.listing_status,
                "format_type": format_type,
                "best_offer_enabled": bool(best_offer_enabled),
                "listing_duration": listing_duration,
                "auction_start_price": float(auction_start_price or 0),
                "auction_reserve_price": float(auction_reserve_price or 0),
                "auction_buy_now_price": float(auction_buy_now_price or 0),
                "review_status": review_status,
                "reviewed_by": (listing.reviewed_by or "").strip(),
                "reviewed_at": iso_or_none(listing.reviewed_at),
                "external_listing_id": (listing.external_listing_id or "").strip(),
                "readiness_status": "blocked" if blockers else readiness.status,
                "readiness_score": readiness.score if not blockers else max(0, readiness.score - 30),
                "blocker_count": len(blockers),
                "warning_count": len(warnings),
                "blocker_list": list(blockers),
                "warning_list": list(warnings),
                "blockers": "; ".join(blockers),
                "warnings": "; ".join(warnings),
            }
            readiness_rows.append(row)
            readiness_row_by_listing_id[int(listing.id)] = row
            if listing.created_at is not None:
                readiness_created_at_by_listing_id[int(listing.id)] = listing.created_at
        if readiness_rows:
            load_reviewer_dashboard = st.checkbox(
                "Load Reviewer Dashboard (slower)",
                value=False,
                key="listings_load_reviewer_dashboard",
                help="Defers reviewer analytics/grouping work unless explicitly requested.",
            )
            if not load_reviewer_dashboard:
                st.caption(
                    "Reviewer dashboard is deferred. Enable `Load Reviewer Dashboard (slower)` "
                    "to hydrate pending/approval metrics and reviewer summary."
                )
            else:
                st.markdown("#### Reviewer Dashboard")
                now_utc = utcnow_naive()
                pending_rows = [
                    row
                    for row in readiness_rows
                    if str(row.get("review_status") or "pending").strip().lower() != "approved"
                ]
                approved_today = 0
                approved_7d = 0
                for listing in ebay_active_listings:
                    review_status = str(getattr(listing, "review_status", "pending") or "pending").strip().lower()
                    reviewed_at = getattr(listing, "reviewed_at", None)
                    if review_status != "approved" or reviewed_at is None:
                        continue
                    if reviewed_at >= (now_utc - timedelta(days=1)):
                        approved_today += 1
                    if reviewed_at >= (now_utc - timedelta(days=7)):
                        approved_7d += 1
                oldest_pending_days = 0
                if pending_rows:
                    pending_dates = []
                    for row in pending_rows:
                        created_at = readiness_created_at_by_listing_id.get(int(row.get("listing_id") or 0))
                        if created_at is not None:
                            pending_dates.append(created_at)
                    if pending_dates:
                        oldest_pending_days = max(0, int((now_utc - min(pending_dates)).days))
                rd1, rd2, rd3, rd4 = st.columns(4)
                rd1.metric("Pending Review", len(pending_rows))
                rd2.metric("Oldest Pending (days)", oldest_pending_days)
                rd3.metric("Approved (24h)", int(approved_today))
                rd4.metric("Approved (7d)", int(approved_7d))

                reviewer_rows = []
                for row in readiness_rows:
                    review_status = str(row.get("review_status") or "pending").strip().lower()
                    reviewed_by = str(row.get("reviewed_by") or "").strip() or "(unassigned)"
                    reviewer_rows.append({"reviewed_by": reviewed_by, "review_status": review_status})
                reviewer_df = pd.DataFrame(reviewer_rows)
                if not reviewer_df.empty:
                    reviewer_summary = (
                        reviewer_df.groupby(["reviewed_by", "review_status"], dropna=False)
                        .size()
                        .reset_index(name="count")
                        .sort_values(["count"], ascending=[False])
                    )
                    st.dataframe(reviewer_summary, use_container_width=True)

            blocker_counts: dict[str, int] = {}
            warning_counts: dict[str, int] = {}
            for row in readiness_rows:
                for blocker in (row.get("blocker_list") or []):
                    key = str(blocker or "").strip()
                    if not key:
                        continue
                    blocker_counts[key] = int(blocker_counts.get(key, 0) + 1)
                for warning in (row.get("warning_list") or []):
                    key = str(warning or "").strip()
                    if not key:
                        continue
                    warning_counts[key] = int(warning_counts.get(key, 0) + 1)
            blocker_reason_options_sorted = list(blocker_counts.keys())
            if len(blocker_reason_options_sorted) > 1:
                blocker_reason_options_sorted = sorted(blocker_reason_options_sorted)
            warning_reason_options_sorted = list(warning_counts.keys())
            if len(warning_reason_options_sorted) > 1:
                warning_reason_options_sorted = sorted(warning_reason_options_sorted)
            load_readiness_breakdown_tables = st.checkbox(
                "Load Readiness Breakdown Tables (slower)",
                value=False,
                key="listings_load_readiness_breakdown_tables",
                help="Defers blocker/warning DataFrame rendering unless explicitly requested.",
            )
            if not load_readiness_breakdown_tables:
                st.caption(
                    "Readiness blocker/warning breakdown tables are deferred. Enable "
                    "`Load Readiness Breakdown Tables (slower)` to render them."
                )
            else:
                st.markdown("#### Readiness Blocker Breakdown")
                bd1, bd2 = st.columns(2)
                with bd1:
                    if blocker_counts:
                        blocker_df = pd.DataFrame(
                            [{"blocker": key, "count": value} for key, value in blocker_counts.items()]
                        ).sort_values(["count", "blocker"], ascending=[False, True])
                        st.dataframe(blocker_df, use_container_width=True, hide_index=True)
                    else:
                        st.info("No readiness blockers in current scope.")
                with bd2:
                    if warning_counts:
                        warning_df = pd.DataFrame(
                            [{"warning": key, "count": value} for key, value in warning_counts.items()]
                        ).sort_values(["count", "warning"], ascending=[False, True])
                        st.dataframe(warning_df, use_container_width=True, hide_index=True)
                    else:
                        st.info("No readiness warnings in current scope.")
            if blocker_counts:
                load_top_blocker_actions = st.checkbox(
                    "Load Top Blocker Actions (slower)",
                    value=False,
                    key="listings_load_top_blocker_actions",
                    help="Defers blocker quick-filter buttons and follow-up workbench controls unless explicitly requested.",
                )
                if not load_top_blocker_actions:
                    st.caption(
                        "Top blocker quick actions are deferred. Enable "
                        "`Load Top Blocker Actions (slower)` to hydrate blocker quick filters and follow-up controls."
                    )
                    load_blocker_followup_workbench = False
                else:
                    st.markdown("#### Top Blocker Quick Filters")
                    top_blocker_items = list(blocker_counts.items())
                    top_blockers = (
                        top_blocker_items
                        if len(top_blocker_items) <= 1
                        else sorted(
                            top_blocker_items,
                            key=lambda item: (-int(item[1]), str(item[0])),
                        )
                    )[:4]
                    quick_cols = st.columns(len(top_blockers))
                    for idx, (reason, count) in enumerate(top_blockers):
                        with quick_cols[idx]:
                            button_label = f"{reason[:28]} ({int(count)})"
                            if st.button(
                                button_label,
                                key=f"listings_readiness_top_blocker_quick_filter_{idx}",
                                use_container_width=True,
                            ):
                                reason_l = str(reason or "").strip().lower()
                                st.session_state["listings_readiness_filter"] = "blocked"
                                st.session_state["listings_readiness_blocker_reason_filter"] = str(reason)
                                if "auction" in reason_l or "reserve" in reason_l or "start price" in reason_l:
                                    st.session_state["listings_readiness_format_filter"] = "auction"
                                elif "buy it now" in reason_l or "bin" in reason_l:
                                    st.session_state["listings_readiness_format_filter"] = "fixed"
                                st.success(f"Applied blocker quick filter: {reason}")
                                st.rerun()
                    load_blocker_followup_workbench = st.checkbox(
                        "Load Blocker Follow-up Workbench (slower)",
                        value=False,
                        key="listings_load_blocker_followup_workbench",
                        help="Defers follow-up task create/history/preset controls unless explicitly requested.",
                    )
                    if not load_blocker_followup_workbench:
                        st.caption(
                            "Blocker follow-up workbench is deferred. Enable "
                            "`Load Blocker Follow-up Workbench (slower)` to hydrate follow-up task controls."
                        )
                    else:
                        st.markdown("#### Create Follow-up Task From Blocker")
                        bf1, bf2, bf3, bf4 = st.columns(4)
                        with bf1:
                            selected_blocker_reason = st.selectbox(
                                "Blocker Reason",
                                options=blocker_reason_options_sorted,
                                key="listings_blocker_followup_reason",
                            )
                        with bf2:
                            followup_owner = st.text_input(
                                "Owner",
                                value=user.username,
                                key="listings_blocker_followup_owner",
                            )
                        with bf3:
                            followup_priority = st.selectbox(
                                "Priority",
                                options=["low", "medium", "high", "critical"],
                                index=1,
                                key="listings_blocker_followup_priority",
                            )
                        with bf4:
                            followup_due_days = st.number_input(
                                "Due in days",
                                min_value=1,
                                max_value=90,
                                value=7,
                                step=1,
                                key="listings_blocker_followup_due_days",
                            )
                        followup_note = st.text_input(
                            "Task Note (optional)",
                            value="",
                            key="listings_blocker_followup_note",
                            placeholder="Acceptance criteria or mitigation details.",
                        )
                        if st.button(
                            "Create Follow-up Task",
                            key="listings_blocker_followup_create_btn",
                        ):
                            if not ensure_permission(user, "create", "Create Follow-up Task"):
                                st.stop()
                            reason = str(selected_blocker_reason or "").strip()
                            if not reason:
                                st.error("Select a blocker reason.")
                            else:
                                try:
                                    due_date = (utcnow_naive() + timedelta(days=int(followup_due_days))).date()
                                    task_key = f"listing-blocker-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                                    repo.record_audit_event(
                                        entity_type="workspace_followup",
                                        entity_id=None,
                                        action="create",
                                        actor=user.username,
                                        changes={
                                            "task_key": task_key,
                                            "workflow": "listings_readiness:blocker",
                                            "title": f"[listings/readiness] Resolve blocker: {reason}",
                                            "owner": str(followup_owner or user.username).strip() or user.username,
                                            "priority": str(followup_priority or "medium").strip().lower(),
                                            "due_date": due_date.isoformat(),
                                            "note": str(followup_note or "").strip(),
                                            "status": "open",
                                            "environment": settings.app_env,
                                            "blocker_reason": reason,
                                            "blocker_count": int(blocker_counts.get(reason, 0)),
                                        },
                                    )
                                    st.success(f"Created follow-up task `{task_key}`.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Unable to create follow-up task: {exc}")
                followup_rows: list[dict] = []
                if load_blocker_followup_workbench:
                    st.markdown("#### Recent Blocker Follow-up Tasks")
                    load_followup_history = st.checkbox(
                        "Load Recent Blocker Follow-up Tasks (slower)",
                        value=bool(st.session_state.get("listings_load_blocker_followup_history", False)),
                        key="listings_load_blocker_followup_history",
                        help="Defers heavy audit-log history scanning unless explicitly requested.",
                        disabled=not load_deep_queue_analytics,
                    )
                    load_followup_history = bool(load_deep_queue_analytics and load_followup_history)
                    if not load_followup_history:
                        st.caption(
                            "Blocker follow-up history is deferred. Enable "
                            "`Load Recent Blocker Follow-up Tasks (slower)` to query it."
                        )
                        recent_followup_events = []
                    else:
                        recent_followup_events = _audit_logs_for_entity(
                            entity_type="workspace_followup",
                            limit=1500,
                        )
                    if recent_followup_events:
                        today = utc_today()
                        task_state_map: dict[str, dict] = {}
                        ordered_followup_events = (
                            recent_followup_events
                            if len(recent_followup_events) <= 1
                            else sorted(
                                recent_followup_events,
                                key=lambda r: (getattr(r, "changed_at", None) or datetime.min),
                                reverse=True,
                            )
                        )
                        for event in ordered_followup_events:
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
                            due_in_days = (
                                (due_date_obj - today).days
                                if due_date_obj is not None
                                else None
                            )
                            sla_status = "none"
                            if due_in_days is not None:
                                if due_in_days < 0:
                                    sla_status = "overdue"
                                elif due_in_days <= 2:
                                    sla_status = "due_soon"
                                else:
                                    sla_status = "on_track"
                            existing = task_state_map.get(task_key)
                            changed_at = getattr(event, "changed_at", None)
                            event_action = str(getattr(event, "action", "") or "").strip().lower()
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
                                    or ("resolved" if event_action == "resolve" else "open"),
                                    "created_at": iso_or_none(changed_at),
                                    "last_updated_at": iso_or_none(changed_at),
                                    "last_action": event_action,
                                    "last_actor": str(getattr(event, "changed_by", "") or "").strip(),
                                }
                            else:
                                existing["last_updated_at"] = iso_or_none(changed_at)
                                existing["last_action"] = event_action
                                existing["last_actor"] = str(getattr(event, "changed_by", "") or "").strip()
                                if due_in_days is not None:
                                    existing["due_in_days"] = due_in_days
                                    existing["sla_status"] = sla_status
                                if event_action == "resolve":
                                    existing["status"] = "resolved"
                                elif str(payload.get("status") or "").strip():
                                    existing["status"] = str(payload.get("status") or "").strip().lower()
                                if str(payload.get("resolution_note") or "").strip():
                                    existing["resolution_note"] = str(payload.get("resolution_note") or "").strip()
                        followup_rows = list(task_state_map.values())
                        followup_rows = (
                            followup_rows
                            if len(followup_rows) <= 1
                            else sorted(
                                followup_rows,
                                key=lambda row: (
                                    0
                                    if str(row.get("sla_status") or "") == "overdue"
                                    else (1 if str(row.get("sla_status") or "") == "due_soon" else 2),
                                    int(row.get("due_in_days")) if isinstance(row.get("due_in_days"), int) else 9999,
                                    str(row.get("last_updated_at") or ""),
                                    str(row.get("task_key") or ""),
                                ),
                                reverse=False,
                            )
                        )[:25]
                if followup_rows:
                    status_values: set[str] = set()
                    owner_values: set[str] = set()
                    priority_values: set[str] = set()
                    sla_values: set[str] = set()
                    for row in followup_rows:
                        row_status = str(row.get("status") or "").strip().lower()
                        row_owner = str(row.get("owner") or "").strip()
                        row_priority = str(row.get("priority") or "").strip().lower()
                        row_sla = str(row.get("sla_status") or "").strip().lower()
                        if row_status:
                            status_values.add(row_status)
                        if row_owner:
                            owner_values.add(row_owner)
                        if row_priority:
                            priority_values.add(row_priority)
                        if row_sla:
                            sla_values.add(row_sla)
                    status_options = list(status_values)
                    if len(status_options) > 1:
                        status_options = sorted(status_options)
                    owner_options = list(owner_values)
                    if len(owner_options) > 1:
                        owner_options = sorted(owner_options)
                    priority_options = list(priority_values)
                    if len(priority_options) > 1:
                        priority_options = sorted(priority_options)
                    sla_options = list(sla_values)
                    if len(sla_options) > 1:
                        sla_options = sorted(sla_options)
                    st.markdown("##### Saved Task Presets")
                    preset_scope = "listings_blocker_followups"
                    preset_map: dict[str, tuple[object, dict]] = {}
                    own_default_label = None
                    shared_default_label = None
                    username_normalized = str(user.username or "").strip()
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
                        row_is_shared = bool(getattr(row, "is_shared", False))
                        row_is_default = bool(getattr(row, "is_default", False))
                        row_username = str(getattr(row, "username", "")).strip()
                        visibility = "Shared" if row_is_shared else "Mine"
                        default_tag = " | Default" if row_is_default else ""
                        owner_tag = f" | Owner:{row_username}" if row_is_shared else ""
                        label = f"{row.name} [{visibility}{default_tag}{owner_tag}]"
                        if label in preset_map:
                            label = f"{label} #{row.id}"
                        preset_map[label] = (row, parsed)
                        if row_is_default:
                            if row_username == username_normalized and not row_is_shared:
                                own_default_label = label
                            elif row_is_shared and shared_default_label is None:
                                shared_default_label = label
                    default_loaded_key = f"{preset_scope}_default_loaded_{settings.app_env}_{user.username}"
                    if default_loaded_key not in st.session_state:
                        st.session_state[default_loaded_key] = False
                    default_label = own_default_label or shared_default_label
                    if default_label and not bool(st.session_state.get(default_loaded_key)):
                        _default_row, default_payload = preset_map.get(default_label, (None, {}))
                        st.session_state["listings_blocker_followup_status_filter"] = str(
                            default_payload.get("status") or "all"
                        )
                        st.session_state["listings_blocker_followup_owner_filter"] = str(
                            default_payload.get("owner") or "all"
                        )
                        st.session_state["listings_blocker_followup_priority_filter"] = str(
                            default_payload.get("priority") or "all"
                        )
                        st.session_state["listings_blocker_followup_sla_filter"] = str(
                            default_payload.get("sla_status") or "all"
                        )
                        st.session_state[default_loaded_key] = True
                        st.rerun()
                    preset_label_keys = list(preset_map.keys())
                    if len(preset_label_keys) > 1:
                        preset_label_keys = sorted(preset_label_keys)
                    preset_labels = ["None"] + preset_label_keys
                    sp1, sp2, sp3, sp4 = st.columns(4)
                    with sp1:
                        selected_task_preset = st.selectbox(
                            "Task Preset",
                            options=preset_labels,
                            key="listings_blocker_followup_preset_select",
                        )
                    selected_task_preset_row, selected_task_preset_payload = preset_map.get(
                        selected_task_preset,
                        (None, {}),
                    )
                    with sp2:
                        if st.button("Apply Task Preset", key="listings_blocker_followup_preset_apply"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                st.session_state["listings_blocker_followup_status_filter"] = str(selected_task_preset_payload.get("status") or "all")
                                st.session_state["listings_blocker_followup_owner_filter"] = str(selected_task_preset_payload.get("owner") or "all")
                                st.session_state["listings_blocker_followup_priority_filter"] = str(selected_task_preset_payload.get("priority") or "all")
                                st.session_state["listings_blocker_followup_sla_filter"] = str(selected_task_preset_payload.get("sla_status") or "all")
                                st.success(f"Applied task preset `{selected_task_preset}`.")
                                st.rerun()
                    with sp3:
                        with st.form("listings_blocker_followup_preset_save_form"):
                            preset_name = st.text_input("Save Current As", key="listings_blocker_followup_preset_name")
                            preset_shared = st.checkbox("Team-shared", value=False, key="listings_blocker_followup_preset_shared")
                            preset_default = st.checkbox(
                                "Set as default",
                                value=False,
                                key="listings_blocker_followup_preset_default",
                            )
                            save_task_preset = st.form_submit_button("Save Task Preset")
                        if save_task_preset:
                            normalized_name = str(preset_name or "").strip()
                            if not normalized_name:
                                st.error("Preset name is required.")
                            else:
                                payload = {
                                    "status": str(st.session_state.get("listings_blocker_followup_status_filter") or "all"),
                                    "owner": str(st.session_state.get("listings_blocker_followup_owner_filter") or "all"),
                                    "priority": str(st.session_state.get("listings_blocker_followup_priority_filter") or "all"),
                                    "sla_status": str(st.session_state.get("listings_blocker_followup_sla_filter") or "all"),
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
                                    st.success(f"Saved task preset `{normalized_name}`.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Unable to save task preset: {exc}")
                    with sp4:
                        if st.button("Set Default Preset", key="listings_blocker_followup_preset_set_default"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                if selected_task_preset_row is None:
                                    st.error("Preset not found.")
                                elif str(selected_task_preset_row.username or "").strip() != user.username:
                                    st.error("Only the preset owner can set it as default.")
                                else:
                                    try:
                                        repo.upsert_saved_filter_profile(
                                            environment=settings.app_env,
                                            username=user.username,
                                            scope=preset_scope,
                                            name=str(selected_task_preset_row.name or "").strip(),
                                            filter_json=json.dumps(selected_task_preset_payload),
                                            is_shared=bool(selected_task_preset_row.is_shared),
                                            is_default=True,
                                            is_active=bool(selected_task_preset_row.is_active),
                                            actor=user.username,
                                        )
                                        st.session_state[default_loaded_key] = True
                                        st.success(f"Set default task preset `{selected_task_preset_row.name}`.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Unable to set default preset: {exc}")
                        if st.button("Clear Default Preset", key="listings_blocker_followup_preset_clear_default"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                if selected_task_preset_row is None:
                                    st.error("Preset not found.")
                                elif str(selected_task_preset_row.username or "").strip() != user.username:
                                    st.error("Only the preset owner can clear default.")
                                else:
                                    try:
                                        repo.upsert_saved_filter_profile(
                                            environment=settings.app_env,
                                            username=user.username,
                                            scope=preset_scope,
                                            name=str(selected_task_preset_row.name or "").strip(),
                                            filter_json=json.dumps(selected_task_preset_payload),
                                            is_shared=bool(selected_task_preset_row.is_shared),
                                            is_default=False,
                                            is_active=bool(selected_task_preset_row.is_active),
                                            actor=user.username,
                                        )
                                        st.success(f"Cleared default flag for `{selected_task_preset_row.name}`.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Unable to clear default preset: {exc}")
                        if st.button("Delete Task Preset", key="listings_blocker_followup_preset_delete"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                if selected_task_preset_row is None:
                                    st.error("Preset not found.")
                                elif str(selected_task_preset_row.username or "").strip() != user.username:
                                    st.error("Only the preset owner can delete it.")
                                else:
                                    try:
                                        repo.delete_saved_filter_profile_by_id(
                                            profile_id=selected_task_preset_row.id,
                                            actor=user.username,
                                        )
                                        st.success(f"Deleted task preset `{selected_task_preset}`.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Unable to delete task preset: {exc}")
                    st.markdown("##### Task Filter Presets")
                    pf1, pf2, pf3, pf4 = st.columns(4)
                    with pf1:
                        if st.button(
                            "Overdue Critical",
                            key="listings_blocker_followup_preset_overdue_critical",
                            use_container_width=True,
                        ):
                            st.session_state["listings_blocker_followup_status_filter"] = (
                                "open" if "open" in status_values else "all"
                            )
                            st.session_state["listings_blocker_followup_owner_filter"] = "all"
                            st.session_state["listings_blocker_followup_priority_filter"] = (
                                "critical" if "critical" in priority_values else "all"
                            )
                            st.session_state["listings_blocker_followup_sla_filter"] = (
                                "overdue" if "overdue" in sla_values else "all"
                            )
                            st.rerun()
                    with pf2:
                        if st.button(
                            "My Open",
                            key="listings_blocker_followup_preset_my_open",
                            use_container_width=True,
                        ):
                            owner_default = user.username if user.username in owner_values else "all"
                            st.session_state["listings_blocker_followup_status_filter"] = (
                                "open" if "open" in status_values else "all"
                            )
                            st.session_state["listings_blocker_followup_owner_filter"] = owner_default
                            st.session_state["listings_blocker_followup_priority_filter"] = "all"
                            st.session_state["listings_blocker_followup_sla_filter"] = "all"
                            st.rerun()
                    with pf3:
                        if st.button(
                            "High Priority Open",
                            key="listings_blocker_followup_preset_high_open",
                            use_container_width=True,
                        ):
                            st.session_state["listings_blocker_followup_status_filter"] = (
                                "open" if "open" in status_values else "all"
                            )
                            st.session_state["listings_blocker_followup_owner_filter"] = "all"
                            st.session_state["listings_blocker_followup_priority_filter"] = (
                                "high" if "high" in priority_values else "all"
                            )
                            st.session_state["listings_blocker_followup_sla_filter"] = "all"
                            st.rerun()
                    with pf4:
                        if st.button(
                            "Reset Task Filters",
                            key="listings_blocker_followup_preset_reset",
                            use_container_width=True,
                        ):
                            st.session_state["listings_blocker_followup_status_filter"] = "all"
                            st.session_state["listings_blocker_followup_owner_filter"] = "all"
                            st.session_state["listings_blocker_followup_priority_filter"] = "all"
                            st.session_state["listings_blocker_followup_sla_filter"] = "all"
                            st.rerun()
                    lf1, lf2, lf3, lf4 = st.columns(4)
                    with lf1:
                        followup_status_filter = st.selectbox(
                            "Task Status Filter",
                            options=["all"] + status_options,
                            index=0,
                            key="listings_blocker_followup_status_filter",
                        )
                    with lf2:
                        followup_owner_filter = st.selectbox(
                            "Task Owner Filter",
                            options=["all"] + owner_options,
                            index=0,
                            key="listings_blocker_followup_owner_filter",
                        )
                    with lf3:
                        followup_priority_filter = st.selectbox(
                            "Task Priority Filter",
                            options=["all"] + priority_options,
                            index=0,
                            key="listings_blocker_followup_priority_filter",
                        )
                    with lf4:
                        followup_sla_filter = st.selectbox(
                            "Task SLA Filter",
                            options=["all"] + sla_options,
                            index=0,
                            key="listings_blocker_followup_sla_filter",
                        )

                    filtered_followup_rows = []
                    open_followup_count = 0
                    due_soon_followup_count = 0
                    overdue_followup_count = 0
                    open_task_map: dict[str, dict] = {}
                    for row in followup_rows:
                        row_status = str(row.get("status") or "").strip().lower()
                        row_owner = str(row.get("owner") or "").strip()
                        row_priority = str(row.get("priority") or "").strip().lower()
                        row_sla = str(row.get("sla_status") or "").strip().lower()
                        if followup_status_filter != "all" and row_status != followup_status_filter:
                            continue
                        if followup_owner_filter != "all" and row_owner != followup_owner_filter:
                            continue
                        if followup_priority_filter != "all" and row_priority != followup_priority_filter:
                            continue
                        if followup_sla_filter != "all" and row_sla != followup_sla_filter:
                            continue
                        filtered_followup_rows.append(row)
                        if row_status == "open":
                            open_followup_count += 1
                            if row_sla == "due_soon":
                                due_soon_followup_count += 1
                            elif row_sla == "overdue":
                                overdue_followup_count += 1
                            task_label = (
                                f"{str(row.get('task_key') or '')} | owner={str(row.get('owner') or '')} | "
                                f"priority={str(row.get('priority') or '')} | due={str(row.get('due_date') or '')}"
                            )
                            open_task_map[task_label] = row
                    sf1, sf2, sf3 = st.columns(3)
                    sf1.metric("Open Follow-ups", int(open_followup_count))
                    sf2.metric("Due Soon", int(due_soon_followup_count))
                    sf3.metric("Overdue", int(overdue_followup_count))
                    filtered_followup_df = pd.DataFrame(filtered_followup_rows)
                    st.dataframe(filtered_followup_df, use_container_width=True)
                    st.download_button(
                        "Download Recent Blocker Tasks CSV",
                        data=filtered_followup_df.to_csv(index=False).encode("utf-8"),
                        file_name=f"listings_blocker_followups_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv",
                        key="listings_blocker_followup_recent_csv_btn",
                    )
                    if open_task_map:
                        rf1, rf2 = st.columns([2, 2])
                        with rf1:
                            selected_open_task_label = st.selectbox(
                                "Resolve Follow-up Task",
                                options=list(open_task_map.keys()),
                                key="listings_blocker_followup_resolve_select",
                            )
                        with rf2:
                            resolve_note = st.text_input(
                                "Resolution Note (optional)",
                                key="listings_blocker_followup_resolve_note",
                                placeholder="What changed to resolve this blocker?",
                            )
                        if st.button(
                            "Mark Follow-up Resolved",
                            key="listings_blocker_followup_resolve_btn",
                        ):
                            if not ensure_permission(user, "update", "Resolve Follow-up Task"):
                                st.stop()
                            selected_task = open_task_map.get(selected_open_task_label)
                            if not selected_task:
                                st.error("Select an open follow-up task.")
                            else:
                                try:
                                    repo.record_audit_event(
                                        entity_type="workspace_followup",
                                        entity_id=None,
                                        action="resolve",
                                        actor=user.username,
                                        changes={
                                            "task_key": str(selected_task.get("task_key") or "").strip(),
                                            "resolution_note": str(resolve_note or "").strip(),
                                            "resolved_at": utcnow_naive().isoformat(timespec="seconds"),
                                            "status": "resolved",
                                            "environment": settings.app_env,
                                            "workflow": "listings_readiness:blocker",
                                            "source": "listings_readiness_panel",
                                        },
                                    )
                                    st.success(
                                        f"Marked follow-up `{str(selected_task.get('task_key') or '').strip()}` resolved."
                                    )
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Unable to resolve follow-up task: {exc}")
                else:
                    st.caption("No blocker follow-up tasks found yet.")

            readiness_df = pd.DataFrame(readiness_rows)
            readiness_records_cache: list[dict] | None = None

            def _get_readiness_records() -> list[dict]:
                nonlocal readiness_records_cache
                if readiness_records_cache is None:
                    readiness_records_cache = readiness_df.to_dict(orient="records")
                return readiness_records_cache

            readiness_filter = "all"
            readiness_format_filter = "all"
            readiness_blocker_filter = "all"
            readiness_warning_filter = "all"
            load_readiness_filters = st.checkbox(
                "Load Readiness Filters + Shortcuts (slower)",
                value=False,
                key="listings_load_readiness_filters",
                help="Defers readiness shortcut actions and filter-widget hydration unless explicitly requested.",
            )
            if not load_readiness_filters:
                st.caption(
                    "Readiness filters/shortcuts are deferred. Enable "
                    "`Load Readiness Filters + Shortcuts (slower)` to hydrate triage shortcuts and filter widgets."
                )
            else:
                st.markdown("#### Format Triage Shortcuts")
                tf1, tf2, tf3 = st.columns(3)
                with tf1:
                    if st.button("Auction Blocked", key="listings_readiness_quick_auction_blocked"):
                        st.session_state["listings_readiness_filter"] = "blocked"
                        st.session_state["listings_readiness_format_filter"] = "auction"
                        st.rerun()
                with tf2:
                    if st.button("Fixed Ready", key="listings_readiness_quick_fixed_ready"):
                        st.session_state["listings_readiness_filter"] = "ready"
                        st.session_state["listings_readiness_format_filter"] = "fixed"
                        st.rerun()
                with tf3:
                    if st.button("Reset Readiness Filters", key="listings_readiness_quick_reset"):
                        st.session_state["listings_readiness_filter"] = "all"
                        st.session_state["listings_readiness_format_filter"] = "all"
                        st.rerun()

                rf1, rf2, rf3, rf4 = st.columns(4)
                with rf1:
                    readiness_filter = st.selectbox(
                        "Readiness Filter",
                        options=["all", "blocked", "ready"],
                        index=1,
                        key="listings_readiness_filter",
                    )
                with rf2:
                    readiness_format_filter = st.selectbox(
                        "Format Filter",
                        options=["all", "fixed", "auction"],
                        index=0,
                        key="listings_readiness_format_filter",
                    )
                with rf3:
                    readiness_blocker_filter = st.selectbox(
                        "Blocker Reason Filter",
                        options=["all"] + blocker_reason_options_sorted,
                        index=0,
                        key="listings_readiness_blocker_reason_filter",
                        help="Show rows containing a specific blocker reason.",
                    )
                with rf4:
                    readiness_warning_filter = st.selectbox(
                        "Warning Reason Filter",
                        options=["all"] + warning_reason_options_sorted,
                        index=0,
                        key="listings_readiness_warning_reason_filter",
                        help="Show rows containing a specific warning reason.",
                    )
                readiness_mask = pd.Series(True, index=readiness_df.index)
                readiness_format_upper: pd.Series | None = None
                readiness_blockers_text: pd.Series | None = None
                readiness_warnings_text: pd.Series | None = None
                if readiness_filter != "all":
                    readiness_mask &= readiness_df["readiness_status"] == readiness_filter
                if readiness_format_filter != "all":
                    readiness_format_upper = readiness_df["format_type"].astype(str).str.upper()
                    if readiness_format_filter == "fixed":
                        readiness_mask &= readiness_format_upper == "FIXED_PRICE"
                    elif readiness_format_filter == "auction":
                        readiness_mask &= readiness_format_upper == "AUCTION"
                if readiness_blocker_filter != "all":
                    readiness_blockers_text = readiness_df["blockers"].astype(str)
                    readiness_mask &= readiness_blockers_text.str.contains(
                        str(readiness_blocker_filter),
                        case=False,
                        regex=False,
                    )
                if readiness_warning_filter != "all":
                    readiness_warnings_text = readiness_df["warnings"].astype(str)
                    readiness_mask &= readiness_warnings_text.str.contains(
                        str(readiness_warning_filter),
                        case=False,
                        regex=False,
                    )
                if not bool(readiness_mask.all()):
                    readiness_df = readiness_df[readiness_mask]
            load_readiness_queue_table = st.checkbox(
                "Load Readiness Queue Table (slower)",
                value=False,
                key="listings_load_readiness_queue_table",
                help="Defers readiness queue table and toolbar rendering unless explicitly requested.",
            )
            if not load_readiness_queue_table:
                st.caption(
                    "Readiness queue table is deferred. Enable "
                    "`Load Readiness Queue Table (slower)` to render the readiness table and exports."
                )
            else:
                render_table_toolbar(
                    df=readiness_df,
                    section_key="listings_ebay_readiness_queue",
                    export_basename="ebay_readiness_queue",
                    defer_exports=True,
                    active_filters={
                        "status": readiness_filter,
                        "format": readiness_format_filter,
                        "blocker_reason": readiness_blocker_filter,
                        "warning_reason": readiness_warning_filter,
                    },
                )
                _render_listings_df(readiness_df)
            load_bulk_review_actions = st.checkbox(
                "Load Bulk Review Actions (slower)",
                value=False,
                key="listings_load_bulk_review_actions",
                help="Defers readiness bulk-review selector/options map unless explicitly requested.",
            )
            if not load_bulk_review_actions:
                st.caption(
                    "Bulk review actions are deferred. Enable `Load Bulk Review Actions (slower)` "
                    "to hydrate large readiness selection controls."
                )
            else:
                st.markdown("#### Bulk Review Actions")
                review_options = {
                    f"#{int(row['listing_id'])} | {row.get('sku') or ''} | {row.get('title') or ''} | review={row.get('review_status') or ''}": int(row["listing_id"])
                    for row in _get_readiness_records()
                }
                selected_review_keys = st.multiselect(
                    "Select Listings",
                    options=list(review_options.keys()),
                    key="listings_bulk_review_selection",
                )
                br1, br2, br3 = st.columns(3)
                with br1:
                    bulk_approve = st.button("Bulk Approve Review", key="listings_bulk_review_approve")
                with br2:
                    bulk_reject = st.button("Bulk Reject Review", key="listings_bulk_review_reject")
                with br3:
                    bulk_pending = st.button("Bulk Set Pending Review", key="listings_bulk_review_pending")
                bulk_notes = st.text_input(
                    "Bulk Review Notes (optional)",
                    value="",
                    key="listings_bulk_review_notes",
                )
                if bulk_approve or bulk_reject or bulk_pending:
                    if not ensure_permission(user, "update", "Bulk Review Listings"):
                        st.stop()
                    if not selected_review_keys:
                        st.error("Select at least one listing for bulk review action.")
                    else:
                        decision = "approved" if bulk_approve else ("rejected" if bulk_reject else "pending")
                        selected_ids = [review_options[key] for key in selected_review_keys if key in review_options]
                        success_count = 0
                        error_count = 0
                        for listing_id in selected_ids:
                            try:
                                repo.review_listing(
                                    listing_id=listing_id,
                                    decision=decision,
                                    actor=user.username,
                                    notes=bulk_notes.strip(),
                                )
                                success_count += 1
                            except Exception:
                                error_count += 1
                        if success_count:
                            st.success(f"Updated review status for {success_count} listing(s) to `{decision}`.")
                        if error_count:
                            st.error(f"{error_count} listing(s) failed to update review status.")
                        st.rerun()

            load_bulk_publish_batch_planner = st.checkbox(
                "Load Bulk Publish Batch Planner (slower)",
                value=False,
                key="listings_load_bulk_publish_batch_planner",
                help="Defers readiness batch publish planner selector/options map and dry-run computations unless explicitly requested.",
            )
            if not load_bulk_publish_batch_planner:
                st.caption(
                    "Bulk publish batch planner is deferred. Enable `Load Bulk Publish Batch Planner (slower)` "
                    "to hydrate planner controls and run dry-run/publish actions."
                )
            else:
                st.markdown("#### Bulk Publish Batch Planner (Dry Run)")
                publish_candidates = {
                    f"#{int(row['listing_id'])} | {row.get('sku') or ''} | {row.get('title') or ''} | "
                    f"review={row.get('review_status') or ''} | readiness={row.get('readiness_status') or ''}": int(row["listing_id"])
                    for row in _get_readiness_records()
                }
                selected_publish_keys = st.multiselect(
                    "Select Listings For Batch Planning",
                    options=list(publish_candidates.keys()),
                    key="listings_bulk_publish_selection",
                )
                publish_batch_id = st.text_input(
                    "Batch ID",
                    value=f"publish-batch-{utcnow_naive().strftime('%Y%m%d-%H%M%S')}",
                    key="listings_bulk_publish_batch_id",
                ).strip()
                bp1, bp2, bp3 = st.columns(3)
                with bp1:
                    run_publish_dry_run = st.button("Run Dry-Run Validation", key="listings_bulk_publish_dry_run")
                with bp2:
                    tag_publish_batch = st.button(
                        "Tag Publishable Listings With Batch ID", key="listings_bulk_publish_tag"
                    )
                with bp3:
                    execute_publish_batch = st.button("Execute Publish Batch", key="listings_bulk_publish_execute")

                if run_publish_dry_run or tag_publish_batch or execute_publish_batch:
                    if not ensure_permission(user, "update", "Bulk Publish Batch Planning"):
                        st.stop()
                    if not selected_publish_keys:
                        st.error("Select at least one listing for batch planning.")
                    elif not publish_batch_id:
                        st.error("Batch ID is required.")
                    else:
                        plan_rows: list[dict] = []
                        publishable_ids: list[int] = []
                        for key in selected_publish_keys:
                            listing_id = publish_candidates.get(key)
                            if listing_id is None:
                                continue
                            listing_obj = listing_by_id.get(int(listing_id))
                            if listing_obj is None:
                                continue
                            row_ref = readiness_row_by_listing_id.get(int(listing_id))
                            reasons: list[str] = []
                            review_state = str((row_ref or {}).get("review_status") or "pending").strip().lower()
                            readiness_state = str((row_ref or {}).get("readiness_status") or "blocked").strip().lower()
                            if (listing_obj.marketplace or "").strip().lower() != "ebay":
                                reasons.append("marketplace_not_ebay")
                            if review_state != "approved":
                                reasons.append("review_not_approved")
                            if readiness_state != "ready":
                                reasons.append("readiness_not_ready")
                            is_publishable = len(reasons) == 0
                            if is_publishable:
                                publishable_ids.append(int(listing_id))
                            plan_rows.append(
                                {
                                    "batch_id": publish_batch_id,
                                    "listing_id": int(listing_id),
                                    "sku": str(
                                        getattr(
                                            product_by_id.get(int(getattr(listing_obj, "product_id", 0) or 0)),
                                            "sku",
                                            "",
                                        )
                                        or ""
                                    ).strip(),
                                    "title": listing_obj.listing_title,
                                    "marketplace": listing_obj.marketplace,
                                    "review_status": review_state,
                                    "readiness_status": readiness_state,
                                    "publishable": is_publishable,
                                    "reasons": ";".join(reasons),
                                }
                            )
                        plan_df = pd.DataFrame(plan_rows)
                        if plan_df.empty:
                            st.info("No listing rows available for batch planning.")
                        else:
                            p1, p2, p3 = st.columns(3)
                            p1.metric("Selected", len(plan_rows))
                            p2.metric("Publishable", len(publishable_ids))
                            p3.metric("Blocked", len(plan_rows) - len(publishable_ids))
                            _render_listings_df(plan_df)
                            if tag_publish_batch and publishable_ids:
                                tagged = 0
                                for listing_id in publishable_ids:
                                    listing_obj = listing_by_id.get(int(listing_id))
                                    if listing_obj is None:
                                        continue
                                    details_obj = _listing_marketplace_details_json(listing_obj)
                                    details_obj["publish_batch"] = {
                                        "batch_id": publish_batch_id,
                                        "planned_by": user.username,
                                        "planned_at": utcnow_naive().isoformat(),
                                        "candidate_count": len(plan_rows),
                                        "publishable_count": len(publishable_ids),
                                    }
                                    try:
                                        repo.update_listing(
                                            int(listing_id),
                                            {"marketplace_details": json.dumps(details_obj, indent=2)},
                                            actor=user.username,
                                        )
                                        tagged += 1
                                    except Exception:
                                        continue
                                if tagged:
                                    st.success(
                                        f"Tagged {tagged} publishable listing(s) with batch `{publish_batch_id}`."
                                    )
                                else:
                                    st.warning("No listings were tagged.")
                                st.rerun()
                            if execute_publish_batch:
                                ebay = EbayClient()
                                allow_sandbox_ops = _allow_sandbox_seller_ops()
                                sandbox_blocked = ebay.environment != "production" and not allow_sandbox_ops
                                if sandbox_blocked:
                                    st.error(
                                        "Sandbox seller operations are blocked. Enable `ebay_allow_sandbox_seller_ops` "
                                        "to execute bulk publish in sandbox."
                                    )
                                    st.stop()
                                if not ebay.is_configured():
                                    st.error("eBay app credentials are not configured.")
                                    st.stop()
                                publish_defaults = _resolve_bulk_publish_defaults()
                                if not str(publish_defaults.get("access_token") or "").strip():
                                    st.error("Missing eBay user access token. Set `ebay_user_access_token` first.")
                                    st.stop()
                                if not str(publish_defaults.get("category_id") or "").strip():
                                    st.error("Default eBay category is required (set in default publish preset).")
                                    st.stop()
                                if not str(publish_defaults.get("merchant_location_key") or "").strip():
                                    st.error("Merchant location key is required.")
                                    st.stop()
                                if not (
                                    str(publish_defaults.get("payment_policy_id") or "").strip()
                                    and str(publish_defaults.get("fulfillment_policy_id") or "").strip()
                                    and str(publish_defaults.get("return_policy_id") or "").strip()
                                ):
                                    st.error("Payment, fulfillment, and return policy IDs are required.")
                                    st.stop()

                                result_rows: list[dict] = []
                                for listing_id in publishable_ids:
                                    listing_obj = listing_by_id.get(int(listing_id))
                                    if listing_obj is None:
                                        result_rows.append(
                                            {
                                                "listing_id": listing_id,
                                                "status": "error",
                                                "message": "Listing not found",
                                            }
                                        )
                                        continue
                                    result_rows.append(
                                        _execute_batch_publish_for_listing(
                                            repo=repo,
                                            listing_obj=listing_obj,
                                            actor=user.username,
                                            batch_id=publish_batch_id,
                                            ebay=ebay,
                                            access_token=str(publish_defaults.get("access_token") or ""),
                                            marketplace_id=str(publish_defaults.get("marketplace_id") or ""),
                                            currency=str(publish_defaults.get("currency") or ""),
                                            content_language=str(publish_defaults.get("content_language") or ""),
                                            merchant_location_key=str(
                                                publish_defaults.get("merchant_location_key") or ""
                                            ),
                                            payment_policy_id=str(publish_defaults.get("payment_policy_id") or ""),
                                            fulfillment_policy_id=str(
                                                publish_defaults.get("fulfillment_policy_id") or ""
                                            ),
                                            return_policy_id=str(publish_defaults.get("return_policy_id") or ""),
                                            category_id=str(publish_defaults.get("category_id") or ""),
                                            product_by_id=product_by_id,
                                            listing_media_rows=_listing_media_rows_cached(int(listing_id)),
                                        )
                                    )
                                result_df = pd.DataFrame(result_rows)
                                if not result_df.empty:
                                    success_count = int((result_df["status"] == "success").sum())
                                    error_count = int((result_df["status"] == "error").sum())
                                    r1, r2, r3 = st.columns(3)
                                    r1.metric("Batch Rows", len(result_df))
                                    r2.metric("Published", success_count)
                                    r3.metric("Failed", error_count)
                                    _render_listings_df(result_df)
                                if not result_df.empty and int((result_df["status"] == "success").sum()) > 0:
                                    st.rerun()
        else:
            if load_readiness_queue and (not ebay_active_listings):
                st.info("No eBay listings found for readiness checks.")

        st.markdown("### Listing Orchestration Queue")
        load_orchestration_queue = st.checkbox(
            "Load Listing Orchestration Queue (slower)",
            value=False,
            key="listings_load_orchestration_queue",
            help="Defers orchestration-status derivation and queue table hydration unless explicitly requested.",
        )
        orchestration_caption = _orchestration_dependency_caption(
            load_orchestration_queue=bool(load_orchestration_queue),
            load_readiness_queue=bool(load_readiness_queue),
            load_readiness_evaluation=bool(load_readiness_evaluation),
        )
        if orchestration_caption:
            st.caption(orchestration_caption)
        else:
            orchestration_filter = st.selectbox(
                "Orchestration Status Filter",
                options=["all", "ready", "blocked", "published", "error"],
                index=1,
                key="listings_orchestration_filter",
            )
            orchestration_rows: list[dict] = []
            channel_adapters = _get_channel_adapters() if readiness_rows else {}
            for row in readiness_rows:
                orchestration_status = orchestration_status_for_listing(
                    adapters=channel_adapters,
                    channel_key="ebay",
                    listing_status=str(row.get("status") or ""),
                    readiness_status=str(row.get("readiness_status") or ""),
                    external_listing_id=str(row.get("external_listing_id") or ""),
                )
                if orchestration_filter != "all" and str(orchestration_status or "").strip() != orchestration_filter:
                    continue
                orchestration_rows.append(
                    {
                        "listing_id": row.get("listing_id"),
                        "sku": row.get("sku"),
                        "title": row.get("title"),
                        "channel": "ebay",
                        "orchestration_status": orchestration_status,
                        "readiness_score": row.get("readiness_score"),
                        "blockers": row.get("blockers"),
                        "warnings": row.get("warnings"),
                    }
                )
            if orchestration_rows:
                orchestration_df = pd.DataFrame(orchestration_rows)
                render_table_toolbar(
                    df=orchestration_df,
                    section_key="listings_orchestration_queue",
                    export_basename="listings_orchestration_queue",
                    defer_exports=True,
                    active_filters={"status": orchestration_filter},
                )
                _render_listings_df(orchestration_df)
            else:
                st.info("No orchestration rows available.")

        allow_sandbox_ops_cache: bool | None = None

        def _allow_sandbox_seller_ops() -> bool:
            nonlocal allow_sandbox_ops_cache
            if allow_sandbox_ops_cache is None:
                allow_sandbox_ops_cache = get_runtime_bool(
                    repo,
                    "ebay_allow_sandbox_seller_ops",
                    bool(settings.ebay_allow_sandbox_seller_ops),
                )
            return bool(allow_sandbox_ops_cache)

        st.markdown("### Bulk Publish Execution History")
        load_bulk_publish_history = st.checkbox(
            "Load Bulk Publish History (slower)",
            value=bool(st.session_state.get("listings_load_bulk_publish_history", False)),
            key="listings_load_bulk_publish_history",
            help="Defers metadata history scan across listings until explicitly requested.",
            disabled=not load_deep_queue_analytics,
        )
        load_bulk_publish_history = bool(load_deep_queue_analytics and load_bulk_publish_history)
        history_rows: list[dict] = []
        if not load_bulk_publish_history:
            st.caption("Bulk publish history is deferred. Enable `Load Bulk Publish History (slower)` to query it.")
        else:
            history_source_listings = [
                listing
                for listing in listings
                if (str(getattr(listing, "marketplace", "") or "").strip().lower() == "ebay")
                and (not _listing_is_archived(listing))
            ]
            for listing in history_source_listings:
                details_raw = str(getattr(listing, "marketplace_details", "") or "")
                if "publish_batch_execution" not in details_raw:
                    continue
                listing_id_value = int(getattr(listing, "id", 0) or 0)
                listing_sku_value = str(
                    getattr(product_by_id.get(int(getattr(listing, "product_id", 0) or 0)), "sku", "")
                    or ""
                ).strip()
                listing_title_value = str(getattr(listing, "listing_title", "") or "")
                listing_url_value = str(getattr(listing, "marketplace_url", "") or "")
                details_obj = _listing_marketplace_details_json(listing)
                if not details_obj:
                    continue
                events = details_obj.get("publish_batch_execution")
                if not isinstance(events, list):
                    continue
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    history_rows.append(
                        {
                            "executed_at": str(event.get("executed_at") or ""),
                            "batch_id": str(event.get("batch_id") or ""),
                            "executed_by": str(event.get("executed_by") or ""),
                            "listing_id": listing_id_value,
                            "sku": listing_sku_value,
                            "title": listing_title_value,
                            "offer_id": str(event.get("offer_id") or ""),
                            "external_listing_id": str(event.get("listing_id") or ""),
                            "marketplace_url": listing_url_value,
                            "status": str(event.get("status") or "success"),
                            "message": str(event.get("message") or ""),
                        }
                    )
        if history_rows:
            history_rows_sorted = (
                history_rows
                if len(history_rows) <= 1
                else sorted(
                    history_rows,
                    key=lambda row: str(row.get("executed_at") or ""),
                    reverse=True,
                )
            )
            h1, h2 = st.columns(2)
            with h1:
                batch_filter = st.text_input("Filter Batch ID", value="", key="listings_batch_history_filter")
            with h2:
                actor_filter = st.text_input("Filter Executor", value="", key="listings_batch_history_actor_filter")
            batch_filter_normalized = str(batch_filter or "").strip().lower()
            actor_filter_normalized = str(actor_filter or "").strip().lower()
            if not batch_filter_normalized and not actor_filter_normalized:
                history_filtered_rows = history_rows_sorted
            else:
                history_filtered_rows = []
                for row in history_rows_sorted:
                    batch_id_normalized = str(row.get("batch_id") or "").strip().lower()
                    executed_by_normalized = str(row.get("executed_by") or "").strip().lower()
                    if batch_filter_normalized and batch_filter_normalized not in batch_id_normalized:
                        continue
                    if actor_filter_normalized and actor_filter_normalized not in executed_by_normalized:
                        continue
                    history_filtered_rows.append(row)
            history_total_rows = int(len(history_filtered_rows))
            if history_total_rows == 0:
                st.info("No bulk publish history rows matched the current filters.")
            else:
                history_render_rows = (
                    history_filtered_rows
                    if bool(listings_render_full_tables)
                    else history_filtered_rows[: int(listings_preview_row_limit)]
                )
                history_df = pd.DataFrame(history_render_rows)
                history_export_df_cache: pd.DataFrame | None = None

                def _history_export_df() -> pd.DataFrame:
                    nonlocal history_export_df_cache
                    if history_export_df_cache is None:
                        history_export_df_cache = (
                            history_df if bool(listings_render_full_tables) else pd.DataFrame(history_filtered_rows)
                        )
                    return history_export_df_cache
                render_table_toolbar(
                    df=history_df,
                    section_key="listings_bulk_publish_history",
                    export_basename="listings_bulk_publish_history",
                    defer_exports=True,
                    row_count=history_total_rows,
                    export_df_factory=_history_export_df,
                    active_filters={"batch": batch_filter.strip(), "actor": actor_filter.strip()},
                )
                _render_listings_df(history_df, total_rows=history_total_rows)

                load_bulk_publish_retry_analysis = st.checkbox(
                    "Load Bulk Publish Retry Analysis (slower)",
                    value=False,
                    key="listings_load_bulk_publish_retry_analysis",
                    help="Defers failed-row extraction and retry-control hydration unless explicitly requested.",
                )
                if not load_bulk_publish_retry_analysis:
                    st.caption(
                        "Bulk publish retry analysis is deferred. Enable "
                        "`Load Bulk Publish Retry Analysis (slower)` to extract failed rows and hydrate retry controls."
                    )
                else:
                    failed_rows = [
                        row
                        for row in history_filtered_rows
                        if str(row.get("status") or "").strip().lower() == "error"
                    ]
                    if failed_rows:
                        load_retry_failed_actions = st.checkbox(
                            "Load Retry Failed Actions (slower)",
                            value=False,
                            key="listings_load_retry_failed_actions",
                            help="Defers failed-history retry selector/options map unless explicitly requested.",
                        )
                        if not load_retry_failed_actions:
                            st.caption(
                                "Retry Failed controls are deferred. Enable `Load Retry Failed Actions (slower)` "
                                "to hydrate failed-history retry selectors."
                            )
                        else:
                            st.markdown("#### Retry Failed History Rows")
                            retry_map = {
                                f"#{int(row.get('listing_id') or 0)} | {row.get('batch_id') or ''} | {row.get('executed_at') or ''} | {row.get('message') or ''}": int(row.get("listing_id") or 0)
                                for row in failed_rows
                            }
                            retry_selection = st.multiselect(
                                "Select Failed Listings To Retry",
                                options=list(retry_map.keys()),
                                key="listings_bulk_publish_retry_failed_selection",
                            )
                            retry_batch_id = st.text_input(
                                "Retry Batch ID",
                                value=f"retry-batch-{utcnow_naive().strftime('%Y%m%d-%H%M%S')}",
                                key="listings_bulk_publish_retry_batch_id",
                            ).strip()
                            run_retry_failed = st.button("Retry Failed Listings", key="listings_bulk_publish_retry_failed_btn")
                            if run_retry_failed:
                                if not ensure_permission(user, "update", "Retry Failed Bulk Publish Rows"):
                                    st.stop()
                                if not retry_selection:
                                    st.error("Select at least one failed row to retry.")
                                elif not retry_batch_id:
                                    st.error("Retry batch ID is required.")
                                else:
                                    ebay = EbayClient()
                                    allow_sandbox_ops = _allow_sandbox_seller_ops()
                                    sandbox_blocked = ebay.environment != "production" and not allow_sandbox_ops
                                    if sandbox_blocked:
                                        st.error(
                                            "Sandbox seller operations are blocked. Enable `ebay_allow_sandbox_seller_ops` "
                                            "to execute retry in sandbox."
                                        )
                                        st.stop()
                                    if not ebay.is_configured():
                                        st.error("eBay app credentials are not configured.")
                                        st.stop()
                                    publish_defaults = _resolve_bulk_publish_defaults()
                                    if not str(publish_defaults.get("access_token") or "").strip():
                                        st.error("Missing eBay user access token. Set `ebay_user_access_token` first.")
                                        st.stop()
                                    retry_rows: list[dict] = []
                                    for key in retry_selection:
                                        listing_id = retry_map.get(key)
                                        if listing_id is None:
                                            continue
                                        listing_obj = listing_by_id.get(int(listing_id))
                                        if listing_obj is None:
                                            retry_rows.append(
                                                {"listing_id": int(listing_id), "status": "error", "message": "Listing not found"}
                                            )
                                            continue
                                        retry_rows.append(
                                            _execute_batch_publish_for_listing(
                                                repo=repo,
                                                listing_obj=listing_obj,
                                                actor=user.username,
                                                batch_id=retry_batch_id,
                                                ebay=ebay,
                                                access_token=str(publish_defaults.get("access_token") or ""),
                                                marketplace_id=str(publish_defaults.get("marketplace_id") or ""),
                                                currency=str(publish_defaults.get("currency") or ""),
                                                content_language=str(publish_defaults.get("content_language") or ""),
                                                merchant_location_key=str(publish_defaults.get("merchant_location_key") or ""),
                                                payment_policy_id=str(publish_defaults.get("payment_policy_id") or ""),
                                                fulfillment_policy_id=str(publish_defaults.get("fulfillment_policy_id") or ""),
                                                return_policy_id=str(publish_defaults.get("return_policy_id") or ""),
                                                category_id=str(publish_defaults.get("category_id") or ""),
                                                product_by_id=product_by_id,
                                                listing_media_rows=_listing_media_rows_cached(int(listing_id)),
                                            )
                                        )
                                    retry_df = pd.DataFrame(retry_rows)
                                    if not retry_df.empty:
                                        _render_listings_df(retry_df)
                                    st.rerun()
        elif load_bulk_publish_history:
            st.info("No bulk publish execution history yet.")

        st.markdown("### Channel Capability Matrix")
        load_channel_capability_matrix = st.checkbox(
            "Load Channel Capability Matrix (slower)",
            value=False,
            key="listings_load_channel_capability_matrix",
            help="Defers capability-matrix build/render unless explicitly requested.",
        )
        if not load_channel_capability_matrix:
            st.caption(
                "Channel capability matrix is deferred. Enable `Load Channel Capability Matrix (slower)` to render it."
            )
        else:
            capability_rows = capability_matrix_rows(_get_channel_adapters())
            capability_total_rows = int(len(capability_rows))
            capability_render_rows = (
                capability_rows
                if bool(listings_render_full_tables)
                else capability_rows[: int(listings_preview_row_limit)]
            )
            capability_df = pd.DataFrame(capability_render_rows)
            capability_export_df_cache: pd.DataFrame | None = None

            def _capability_export_df() -> pd.DataFrame:
                nonlocal capability_export_df_cache
                if capability_export_df_cache is None:
                    capability_export_df_cache = (
                        capability_df if bool(listings_render_full_tables) else pd.DataFrame(capability_rows)
                    )
                return capability_export_df_cache

            render_table_toolbar(
                df=capability_df,
                section_key="listings_channel_capability_matrix",
                export_basename="listings_channel_capability_matrix",
                defer_exports=True,
                row_count=capability_total_rows,
                export_df_factory=_capability_export_df,
            )
            _render_listings_df(capability_df, total_rows=capability_total_rows)

    with panel_col:
        st.markdown("#### Listing Detail/Edit")
        load_listing_detail_panel = st.checkbox(
            "Load Listing Detail Panel (slower)",
            value=False,
            key="listings_load_detail_panel",
            help="Defers listing-detail selector and editor widgets unless explicitly requested.",
        )
        if not load_listing_detail_panel:
            st.caption(
                "Listing detail panel is deferred. Enable `Load Listing Detail Panel (slower)` "
                "to hydrate selector/edit controls."
            )
        elif not filtered_rows:
            st.info("No filtered listings available.")
        else:
            filtered_listing_ids = [int(row["id"]) for row in filtered_rows]
            load_detailed_side_selector = st.checkbox(
                "Load Detailed Listing Selector Labels (slower)",
                value=False,
                key="listings_side_panel_load_detailed_selector",
                help="Defers heavy title/marketplace selector-label map construction unless explicitly requested.",
            )
            if not load_detailed_side_selector:
                selected_listing_id = int(
                    st.selectbox(
                        "Select Listing ID",
                        options=filtered_listing_ids,
                        key="listings_side_panel_select_id",
                    )
                )
                selected_listing = listing_obj_by_id[selected_listing_id]
            else:
                select_options = {
                    f"#{row['id']} | {row['marketplace']} | {row['title']}": int(row["id"]) for row in filtered_rows
                }
                selected_label = st.selectbox(
                    "Select Listing",
                    options=list(select_options.keys()),
                    key="listings_side_panel_select",
                )
                selected_listing = listing_obj_by_id[int(select_options[selected_label])]
            load_side_panel_context = st.checkbox(
                "Load Side Panel Context (slower)",
                value=False,
                key="listings_sidepanel_load_context",
                help="Defers related sales/order and review-history context unless explicitly requested.",
            )
            if not load_side_panel_context:
                st.caption(
                    "Side-panel context is deferred. Enable `Load Side Panel Context (slower)` "
                    "to include related sales/orders and review history."
                )
            selected_listing_id = int(selected_listing.id)
            media_count = _listing_media_count_cached(selected_listing_id)
            linked_product = (
                product_by_id.get(int(selected_listing.product_id or 0))
                if selected_listing.product_id
                else None
            )

            lc1, lc2, lc3 = st.columns(3)
            with lc1:
                open_comp_for_listing = st.button(
                    "Comp Tool: Listing",
                    key=f"listing_comp_open_{selected_listing.id}",
                    help="Open Comp Tool with listing/product context prefilled.",
                )
            with lc2:
                open_comp_for_listing_manual = st.button(
                    "Comp Tool: Listing + Notes",
                    key=f"listing_comp_notes_open_{selected_listing.id}",
                    help="Open Comp Tool in manual mode with listing details context.",
                )
            with lc3:
                open_comp_for_listing_photo = st.button(
                    "Comp Tool: Photo Mode",
                    key=f"listing_comp_photo_open_{selected_listing.id}",
                    help="Open Comp Tool in Image/File Hint mode with listing hints prefilled.",
                )
            if open_comp_for_listing or open_comp_for_listing_manual or open_comp_for_listing_photo:
                query_parts = [
                    str(selected_listing.listing_title or "").strip(),
                    str(linked_product.metal_type or "").strip() if linked_product is not None else "",
                ]
                st.session_state["comp_prefill_query"] = " ".join([p for p in query_parts if p]).strip()
                if linked_product is not None:
                    st.session_state["comp_prefill_product_id"] = int(linked_product.id)
                st.session_state["comp_prefill_source_mode"] = (
                    "Image/File Hint"
                    if open_comp_for_listing_photo
                    else ("Manual Title/Description" if open_comp_for_listing_manual else "Inventory Item")
                )
                if open_comp_for_listing_manual:
                    st.session_state["comp_prefill_manual_title"] = str(selected_listing.listing_title or "").strip()
                    st.session_state["comp_prefill_manual_desc"] = "\n\n".join(
                        [
                            str(selected_listing.marketplace_details or "").strip(),
                            str(linked_product.description or "").strip() if linked_product is not None else "",
                            str(linked_product.ai_description or "").strip() if linked_product is not None else "",
                            str(linked_product.ai_grading_description or "").strip() if linked_product is not None else "",
                        ]
                    ).strip()
                elif open_comp_for_listing_photo:
                    st.session_state["comp_prefill_manual_title"] = str(selected_listing.listing_title or "").strip()
                    st.session_state["comp_prefill_manual_desc"] = str(selected_listing.marketplace_details or "").strip()
                st.session_state["comp_prefill_origin"] = f"listing:{int(selected_listing.id)}"
                st.switch_page("pages/06_Tools.py")

            st.markdown("##### Document Draft")
            load_related_document_sources = st.checkbox(
                "Load Related Sales/Orders (slower)",
                value=False,
                key=f"listing_documents_load_related_{selected_listing.id}",
                help="Defers related sales/order lookups unless explicitly needed for document-source selection.",
                disabled=not load_side_panel_context,
            )
            load_related_document_sources = bool(load_side_panel_context and load_related_document_sources)
            related_sales = []
            related_order_items = []
            related_orders = []
            if load_related_document_sources:
                related_sales = repo.list_sales_for_listing(int(selected_listing.id))
                order_ids_from_sales = {
                    int(sale.order_id)
                    for sale in related_sales
                    if sale.order_id is not None
                }
                related_order_items = repo.list_order_items_for_listing(int(selected_listing.id))
                order_ids_from_items = {
                    int(item.order_id)
                    for item in related_order_items
                    if item.order_id is not None
                }
                related_order_ids = order_ids_from_sales | order_ids_from_items
                related_orders = repo.list_orders_by_ids(related_order_ids)
            else:
                st.caption(
                    "Related sales/order sources are deferred. Enable "
                    "`Load Related Sales/Orders (slower)` to include linked sale/order sources."
                )
            dd1, dd2 = st.columns([2, 1])
            with dd1:
                listing_doc_type = st.selectbox(
                    "Document Type",
                    options=["invoice", "receipt"],
                    index=0,
                    key=f"listing_documents_doc_type_{selected_listing.id}",
                )
            load_detailed_document_source_labels = st.checkbox(
                "Load Detailed Document Source Labels (slower)",
                value=False,
                key=f"listing_documents_load_detailed_labels_{selected_listing.id}",
                help="Defers rich sale/order label formatting unless explicitly requested.",
                disabled=not load_related_document_sources,
            )
            load_detailed_document_source_labels = bool(
                load_related_document_sources and load_detailed_document_source_labels
            )
            if load_related_document_sources and not load_detailed_document_source_labels:
                st.caption(
                    "Detailed document-source labels are deferred. Enable "
                    "`Load Detailed Document Source Labels (slower)` for enriched sale/order label context."
                )
            source_options: dict[str, tuple[str, int]] = {}
            source_options[
                (
                    f"Listing #{int(selected_listing.id)} | "
                    f"{str(selected_listing.marketplace or '').strip()} | "
                    f"{str(selected_listing.external_listing_id or '').strip() or 'no-ext-id'} | "
                    f"ask=${float(selected_listing.listing_price or 0):,.2f}"
                )
            ] = ("Listing", int(selected_listing.id))
            related_sales_sorted = (
                related_sales
                if len(related_sales) <= 1
                else sorted(related_sales, key=lambda s: (s.sold_at or datetime.min, s.id), reverse=True)
            )
            for sale in related_sales_sorted:
                if load_detailed_document_source_labels:
                    label = (
                        f"Sale #{int(sale.id)} | {str(sale.marketplace or '').strip()} | "
                        f"{str(sale.external_order_id or '').strip() or 'no-ext-id'} | "
                        f"gross=${float(sale.sold_price or 0):,.2f}"
                    )
                else:
                    label = f"Sale #{int(sale.id)}"
                source_options[label] = ("Sale", int(sale.id))
            related_orders_sorted = (
                related_orders
                if len(related_orders) <= 1
                else sorted(related_orders, key=lambda o: (o.sold_at or datetime.min, o.id), reverse=True)
            )
            for order in related_orders_sorted:
                if load_detailed_document_source_labels:
                    label = (
                        f"Order #{int(order.id)} | {str(order.marketplace or '').strip()} | "
                        f"{str(order.external_order_id or '').strip() or 'no-ext-id'} | "
                        f"total=${float(order.total_amount or 0):,.2f}"
                    )
                else:
                    label = f"Order #{int(order.id)}"
                if label not in source_options:
                    source_options[label] = ("Order", int(order.id))
            if source_options:
                source_labels_all = list(source_options.keys())
                source_preview_limit = int(listings_preview_row_limit)
                load_full_document_source_list = st.checkbox(
                    "Load Full Document Source List (slower)",
                    value=False,
                    key=f"listing_documents_load_full_sources_{selected_listing.id}",
                    help="Defers very large document-source option lists unless explicitly requested.",
                    disabled=len(source_labels_all) <= source_preview_limit,
                )
                if (not load_full_document_source_list) and len(source_labels_all) > source_preview_limit:
                    source_labels = source_labels_all[:source_preview_limit]
                    st.caption(
                        f"Showing preview document sources: {len(source_labels)} / {len(source_labels_all)}. "
                        "Enable `Load Full Document Source List (slower)` to show all source options."
                    )
                else:
                    source_labels = source_labels_all
                source_pick_key = f"listing_documents_source_pick_{selected_listing.id}"
                existing_source_pick = st.session_state.get(source_pick_key)
                if existing_source_pick is not None and existing_source_pick not in source_labels:
                    st.session_state.pop(source_pick_key, None)
                selected_source_label = st.selectbox(
                    "Document Source",
                    options=source_labels,
                    key=source_pick_key,
                    help="Use listing directly for local invoice drafts, or use related sale/order records when available.",
                )
                with dd2:
                    if st.button(
                        "Open in Documents",
                        key=f"listing_to_documents_{selected_listing.id}",
                    ):
                        source_type, source_id = source_options[selected_source_label]
                        handoff_to_documents_draft(
                            source_type=source_type,
                            source_id=int(source_id),
                            doc_type=listing_doc_type,
                            handoff_from="listings",
                            repo=repo,
                            actor=user.username,
                        )
            else:
                st.caption("No related sales/orders found for this listing yet.")

            with st.form("listings_side_panel_edit_form"):
                lp1, lp2 = st.columns(2)
                with lp1:
                    edit_marketplace = st.selectbox(
                        "Marketplace",
                        MARKETPLACES,
                        index=MARKETPLACES.index(selected_listing.marketplace)
                        if selected_listing.marketplace in MARKETPLACES
                        else 0,
                    )
                    edit_title = st.text_input("Title", value=selected_listing.listing_title or "")
                    edit_status = st.selectbox(
                        "Status",
                        ["draft", "active", "ended", "sold"],
                        index=["draft", "active", "ended", "sold"].index(selected_listing.listing_status)
                        if selected_listing.listing_status in {"draft", "active", "ended", "sold"}
                        else 0,
                    )
                    st.caption(
                        f"Review status: `{selected_listing.review_status or 'pending'}`"
                    )
                    edit_price = st.number_input(
                        "Price",
                        min_value=0.0,
                        value=float(selected_listing.listing_price or 0),
                        step=1.0,
                    )
                    edit_qty = st.number_input(
                        "Quantity",
                        min_value=1,
                        value=int(selected_listing.quantity_listed or 1),
                        step=1,
                    )
                with lp2:
                    current_date = selected_listing.listed_at.date() if selected_listing.listed_at else utc_today()
                    edit_listed_date = st.date_input("Listed Date", value=current_date)
                    edit_external_id = st.text_input("External Listing ID", value=selected_listing.external_listing_id or "")
                    edit_marketplace_url = st.text_input("Marketplace URL", value=selected_listing.marketplace_url or "")
                    edit_marketplace_details = st.text_area(
                        "Marketplace Details",
                        value=selected_listing.marketplace_details or "",
                    )
                save_side_panel = st.form_submit_button("Save Listing Changes")

            if save_side_panel:
                if not ensure_permission(user, "update", "Update Listing"):
                    st.stop()
                try:
                    ValidationService.validate_listing_workflow(
                        listing_title=edit_title.strip(),
                        listing_price=to_decimal(edit_price),
                        quantity_listed=int(edit_qty),
                        listing_status=edit_status,
                        media_count=media_count,
                        external_listing_id=edit_external_id.strip(),
                        marketplace_url=edit_marketplace_url.strip(),
                    )
                    repo.update_listing(
                        selected_listing.id,
                        {
                            "marketplace": edit_marketplace,
                            "listing_title": edit_title.strip(),
                            "listing_status": edit_status,
                            "listing_price": to_decimal(edit_price),
                            "quantity_listed": int(edit_qty),
                            "listed_at": datetime.combine(edit_listed_date, datetime.min.time()),
                            "external_listing_id": edit_external_id.strip(),
                            "marketplace_url": edit_marketplace_url.strip(),
                            "marketplace_details": edit_marketplace_details.strip(),
                        },
                        actor=user.username,
                    )
                    st.success("Listing updated.")
                    st.rerun()
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))

            st.markdown("##### Duplicate Listing")
            st.caption(
                "Creates a new local draft from this listing. External eBay IDs, URLs, publish history, "
                "and stale publish errors are cleared so the duplicate can publish as a separate listing."
            )
            duplicate_default_title = str(selected_listing.listing_title or "").strip()
            duplicate_suffix = " (copy)"
            if duplicate_default_title and not duplicate_default_title.lower().endswith("(copy)"):
                duplicate_default_title = f"{duplicate_default_title}{duplicate_suffix}"
            with st.form(f"duplicate_listing_form_{selected_listing.id}"):
                duplicate_title = st.text_input(
                    "Duplicate Title",
                    value=duplicate_default_title,
                    key=f"duplicate_listing_title_{selected_listing.id}",
                )
                dc1, dc2 = st.columns(2)
                with dc1:
                    duplicate_price = st.number_input(
                        "Duplicate Price",
                        min_value=0.0,
                        value=float(selected_listing.listing_price or 0),
                        step=1.0,
                        key=f"duplicate_listing_price_{selected_listing.id}",
                    )
                with dc2:
                    duplicate_qty = st.number_input(
                        "Duplicate Quantity",
                        min_value=1,
                        value=max(1, int(selected_listing.quantity_listed or 1)),
                        step=1,
                        key=f"duplicate_listing_qty_{selected_listing.id}",
                    )
                duplicate_copy_details = st.checkbox(
                    "Copy reusable marketplace details",
                    value=True,
                    key=f"duplicate_listing_copy_details_{selected_listing.id}",
                    help=(
                        "Keeps reusable category, policy, bundle, and listing setup details while clearing "
                        "eBay offer/listing identity fields."
                    ),
                )
                duplicate_submit = st.form_submit_button("Create Duplicate Draft Listing")

            if duplicate_submit:
                if not ensure_permission(user, "create", "Duplicate Listing"):
                    st.stop()
                try:
                    duplicate = repo.duplicate_listing(
                        int(selected_listing.id),
                        listing_title=str(duplicate_title or "").strip(),
                        listing_price=to_decimal(duplicate_price),
                        quantity_listed=int(duplicate_qty),
                        copy_marketplace_details=bool(duplicate_copy_details),
                        actor=user.username,
                    )
                    st.success(
                        f"Created duplicate draft listing #{int(duplicate.id)}. "
                        "Review media and eBay publish settings before posting."
                    )
                    st.session_state["manage_listing_id"] = int(duplicate.id)
                    st.rerun()
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error(f"Unable to duplicate listing: {exc}")

            st.markdown("##### Danger Zone")
            listing_archived = bool(_listing_is_archived(selected_listing))
            if listing_archived:
                st.info("This listing is currently archived.")
            else:
                st.caption("Tip: archive listing first for reversible cleanup; use delete only for hard removal.")
            st.caption(
                "Delete bad or duplicate listings. This removes the listing record and detaches linked "
                "sales/order items/media references; related sales/orders are not deleted."
            )
            if not listing_archived:
                archive_reason = st.text_input(
                    "Archive Reason (optional)",
                    value="",
                    key=f"archive_listing_reason_{selected_listing.id}",
                )
                if st.button(
                    "Archive Listing",
                    key=f"archive_listing_btn_{selected_listing.id}",
                    use_container_width=True,
                ):
                    if not ensure_permission(user, "update", "Archive Listing"):
                        st.stop()
                    try:
                        repo.archive_listing(
                            int(selected_listing.id),
                            actor=user.username,
                            reason=str(archive_reason or "").strip(),
                        )
                        st.success(f"Archived listing #{int(selected_listing.id)}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to archive listing: {exc}")
            else:
                if st.button(
                    "Restore Archived Listing",
                    key=f"restore_listing_btn_{selected_listing.id}",
                    use_container_width=True,
                ):
                    if not ensure_permission(user, "update", "Restore Listing"):
                        st.stop()
                    try:
                        repo.restore_listing(int(selected_listing.id), actor=user.username)
                        st.success(f"Restored listing #{int(selected_listing.id)}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to restore listing: {exc}")
            if related_sales or related_order_items or int(media_count or 0) > 0:
                st.warning(
                    "Linked records detected: "
                    f"{len(related_sales)} sale(s), {len(related_order_items)} order item(s), "
                    f"{int(media_count or 0)} media file(s). They will be detached."
                )
            delete_confirm = st.checkbox(
                "I understand this permanently deletes the selected listing record.",
                value=False,
                key=f"delete_listing_confirm_{selected_listing.id}",
            )
            if st.button(
                "Delete Listing Permanently",
                key=f"delete_listing_btn_{selected_listing.id}",
                use_container_width=True,
            ):
                if not ensure_permission(user, "delete", "Delete Listing"):
                    st.stop()
                if not delete_confirm:
                    st.error("Confirm the delete checkbox before deleting this listing.")
                else:
                    deleted = repo.delete_listing(int(selected_listing.id), actor=user.username)
                    if deleted:
                        st.success(
                            f"Deleted listing #{int(selected_listing.id)} and detached linked records."
                        )
                        st.rerun()
                    st.warning("Listing was not found. It may have already been removed.")

            load_review_controls = st.checkbox(
                "Load Review Controls (slower)",
                value=False,
                key=f"listing_review_controls_load_{selected_listing.id}",
                help="Defers review action controls and review-history hydration unless explicitly requested.",
            )
            if not load_review_controls:
                st.caption(
                    "Review controls are deferred. Enable `Load Review Controls (slower)` "
                    "to manage review actions and inspect review history."
                )
            else:
                st.markdown("##### Review Actions")
                review_notes = st.text_area(
                    "Review Notes",
                    value="",
                    key=f"listing_review_notes_{selected_listing.id}",
                    help="Optional notes saved into listing marketplace details review metadata.",
                )
                r1, r2, r3 = st.columns(3)
                with r1:
                    approve_review = st.button("Approve Listing Review", key=f"approve_review_{selected_listing.id}")
                with r2:
                    reject_review = st.button("Reject Listing Review", key=f"reject_review_{selected_listing.id}")
                with r3:
                    reset_review = st.button("Set Pending Review", key=f"pending_review_{selected_listing.id}")

                if approve_review or reject_review or reset_review:
                    if not ensure_permission(user, "update", "Review Listing"):
                        st.stop()
                    decision = "approved" if approve_review else ("rejected" if reject_review else "pending")
                    try:
                        repo.review_listing(
                            selected_listing.id,
                            decision=decision,
                            actor=user.username,
                            notes=review_notes.strip(),
                        )
                        st.success(f"Listing review updated: `{decision}`.")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

                st.markdown("##### Review History")
                if not load_side_panel_context:
                    st.caption("Review history is deferred. Enable `Load Side Panel Context (slower)` to load it.")
                else:
                    try:
                        details_obj = json.loads((selected_listing.marketplace_details or "").strip() or "{}")
                        review_history_rows = details_obj.get("review_history", [])
                        if isinstance(review_history_rows, list) and review_history_rows:
                            review_history_dict_rows = [row for row in review_history_rows if isinstance(row, dict)]
                            review_history_rows_sorted = (
                                review_history_dict_rows
                                if len(review_history_dict_rows) <= 1
                                else sorted(
                                    review_history_dict_rows,
                                    key=lambda row: str(row.get("reviewed_at") or ""),
                                    reverse=True,
                                )
                            )
                            review_history_total_rows = int(len(review_history_rows_sorted))
                            review_history_render_rows = (
                                review_history_rows_sorted
                                if bool(listings_render_full_tables)
                                else review_history_rows_sorted[: int(listings_preview_row_limit)]
                            )
                            review_history_df = pd.DataFrame(review_history_render_rows)
                            review_history_export_df_cache: pd.DataFrame | None = None

                            def _review_history_export_df() -> pd.DataFrame:
                                nonlocal review_history_export_df_cache
                                if review_history_export_df_cache is None:
                                    review_history_export_df_cache = (
                                        review_history_df
                                        if bool(listings_render_full_tables)
                                        else pd.DataFrame(review_history_rows_sorted)
                                    )
                                return review_history_export_df_cache

                            render_table_toolbar(
                                df=review_history_df,
                                section_key=f"listings_side_review_history_{int(selected_listing.id)}",
                                export_basename=f"listing_{int(selected_listing.id)}_review_history",
                                defer_exports=True,
                                row_count=review_history_total_rows,
                                export_df_factory=_review_history_export_df,
                            )
                            _render_listings_df(review_history_df, total_rows=review_history_total_rows)
                        else:
                            st.caption("No review history yet.")
                    except Exception:
                        st.caption("No review history yet.")

    st.markdown("### Listing Media Manager")
    if not listings:
        st.info("No listings available.")
        return

    listing_ids = list(listing_obj_by_id.keys())
    if len(listing_ids) > 1:
        listing_ids = sorted(listing_ids, reverse=True)
    if "manage_listing_id" not in st.session_state or int(st.session_state.get("manage_listing_id") or 0) not in listing_obj_by_id:
        st.session_state["manage_listing_id"] = int(listing_ids[0])
    selected_listing_id = st.selectbox(
        "Choose Listing",
        options=listing_ids,
        key="manage_listing_id",
        format_func=lambda lid: (
            f"#{lid} | {str(listing_obj_by_id[int(lid)].marketplace or '').strip()} | "
            f"{str(listing_obj_by_id[int(lid)].listing_title or '').strip()}"
        ),
    )
    selected_listing = listing_obj_by_id[int(selected_listing_id)]
    mmc1, mmc2 = st.columns([3, 1])
    with mmc1:
        st.caption(
            f"Managing listing #{int(selected_listing.id)}. Duplicate creates a separate draft with eBay identity cleared."
        )
    with mmc2:
        if st.button(
            "Duplicate Draft",
            key=f"media_manager_duplicate_listing_{int(selected_listing.id)}",
            use_container_width=True,
        ):
            if not ensure_permission(user, "create", "Duplicate Listing"):
                st.stop()
            try:
                duplicate = repo.duplicate_listing(
                    int(selected_listing.id),
                    copy_marketplace_details=True,
                    actor=user.username,
                )
                st.session_state["manage_listing_id"] = int(duplicate.id)
                st.success(f"Created duplicate draft listing #{int(duplicate.id)}.")
                st.rerun()
            except (ValueError, ValidationError) as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Unable to duplicate listing: {exc}")
    load_listing_media_manager_data = st.checkbox(
        "Load Listing Media Manager Data (slower)",
        value=False,
        key="listings_load_media_manager_data",
        help="Defers listing media table/gallery hydration and media-linked publish options unless explicitly requested.",
    )
    if not load_listing_media_manager_data:
        st.caption(
            "Listing media manager data is deferred. Enable `Load Listing Media Manager Data (slower)` "
            "to hydrate media table/gallery and media-linked publish selectors."
        )
    listing_media = []
    if load_listing_media_manager_data:
        media_uploaded_by = st.text_input("Uploaded By", value="employee", key="listing_media_by")
        more_files = render_media_capture_inputs(
            key_prefix="manage_listing_media",
            upload_label="Add More Photos/Videos",
            allow_enhanced=True,
        )
        submit_media = st.button("Upload Media To Listing", key="listing_media_upload_submit")

        if submit_media:
            if not ensure_permission(user, "create", "Upload Listing Media"):
                st.stop()
            if not more_files:
                st.error("Select at least one file.")
            elif not storage.enabled:
                st.error("S3 storage is not configured.")
            else:
                uploaded, errors = upload_media_for_listing(
                    repo=repo,
                    storage=storage,
                    listing_id=selected_listing.id,
                    product_id=selected_listing.product_id,
                    uploaded_files=more_files,
                    uploaded_by=media_uploaded_by,
                )
                if uploaded:
                    st.success(f"Uploaded {uploaded} media file(s).")
                for error in errors:
                    st.error(f"Upload failed: {error}")

        if int(selected_listing.product_id or 0) > 0:
            try:
                attachable_media_ids = repo.list_unlinked_product_media_ids(
                    int(selected_listing.product_id),
                    include_archived=False,
                )
                attachable_media_rows = repo.list_media_assets_by_ids(
                    attachable_media_ids,
                    include_archived=False,
                )
            except Exception:
                attachable_media_rows = []
            if attachable_media_rows:
                with st.expander("Attach Existing Product Media", expanded=False):
                    st.caption(
                        "Attach unlinked product media to this listing without uploading another copy. "
                        "Media already attached to another listing is not shown."
                    )
                    attachable_options: dict[str, int] = {
                        (
                            f"#{int(row.id)} | {str(row.media_type or '').strip() or '-'} | "
                            f"{str(row.original_filename or '').strip() or '-'}"
                        ): int(row.id)
                        for row in attachable_media_rows
                    }
                    attach_selected_labels = st.multiselect(
                        "Existing Product Media",
                        options=list(attachable_options.keys()),
                        key=f"listing_media_attach_existing_{int(selected_listing.id)}",
                    )
                    attach_selected_ids = [
                        int(attachable_options[label])
                        for label in attach_selected_labels
                        if label in attachable_options
                    ]
                    if st.button(
                        "Attach Selected Media",
                        key=f"listing_media_attach_existing_btn_{int(selected_listing.id)}",
                        use_container_width=True,
                    ):
                        if not ensure_permission(user, "update", "Attach Listing Media"):
                            st.stop()
                        if not attach_selected_ids:
                            st.error("Select at least one existing product media file to attach.")
                        else:
                            result = repo.bulk_update_media_assets(
                                attach_selected_ids,
                                {"listing_id": int(selected_listing.id)},
                                actor=user.username,
                            )
                            updated_ids = list(result.get("updated_ids") or [])
                            missing_ids = list(result.get("missing_ids") or [])
                            if updated_ids:
                                st.success(f"Attached {len(updated_ids)} existing media file(s).")
                            if missing_ids:
                                st.warning(f"{len(missing_ids)} selected media file(s) were not found.")
                            st.rerun()

        listing_media = _listing_media_rows_cached(int(selected_listing.id))
        if not listing_media:
            st.info("No media currently attached to this listing.")
        else:
            listing_media_rows = [
                {
                    "id": m.id,
                    "type": m.media_type,
                    "filename": m.original_filename,
                    "content_type": m.content_type,
                    "size_bytes": m.size_bytes,
                    "url": m.s3_url,
                }
                for m in listing_media
            ]
            listing_media_total_rows = int(len(listing_media_rows))
            listing_media_render_rows = (
                listing_media_rows
                if bool(listings_render_full_tables)
                else listing_media_rows[: int(listings_preview_row_limit)]
            )
            listing_media_df = pd.DataFrame(listing_media_render_rows)
            listing_media_export_df_cache: pd.DataFrame | None = None

            def _listing_media_export_df() -> pd.DataFrame:
                nonlocal listing_media_export_df_cache
                if listing_media_export_df_cache is None:
                    listing_media_export_df_cache = (
                        listing_media_df if bool(listings_render_full_tables) else pd.DataFrame(listing_media_rows)
                    )
                return listing_media_export_df_cache

            render_table_toolbar(
                df=listing_media_df,
                section_key=f"listings_media_manager_{int(selected_listing.id)}",
                export_basename=f"listing_{int(selected_listing.id)}_media",
                defer_exports=True,
                row_count=listing_media_total_rows,
                export_df_factory=_listing_media_export_df,
            )
            _render_listings_df(listing_media_df, total_rows=listing_media_total_rows)
            show_listing_media_previews = st.checkbox(
                "Show Listing Media Preview Gallery (slower)",
                value=False,
                key=f"listings_show_media_preview_gallery_{int(selected_listing.id)}",
                help=(
                    "Defers media preview rendering. The table above is DB-only; previews can require browser "
                    "or storage fetches for images/videos."
                ),
            )
            if show_listing_media_previews:
                render_media_gallery(
                    listing_media,
                    section_title="Listing Media Preview Gallery",
                    columns=3,
                    storage=storage,
                    prefer_url_previews=True,
                )
            else:
                st.caption(
                    "Media previews are deferred. Enable `Show Listing Media Preview Gallery (slower)` "
                    "to render image/video previews."
                )
            show_listing_media_file_actions = st.checkbox(
                "Load Listing Media File Access + Downloads (slower)",
                value=False,
                key=f"listings_show_media_file_actions_{int(selected_listing.id)}",
                help="Loads selected file preview/download controls only when needed.",
            )
            if show_listing_media_file_actions:
                render_media_file_actions(
                    listing_media,
                    storage=storage,
                    key_prefix=f"listing_media_file_actions_{selected_listing.id}",
                    section_title="Listing Media File Access",
                    repo=repo,
                    actor=user.username,
                    user=user,
                )
            else:
                st.caption(
                    "Media file access/download controls are deferred. Enable the checkbox above when you need "
                    "to preview or download stored media bytes."
                )

    st.markdown("### Publish Selected Listing To eBay")
    st.caption(
        "Creates/updates eBay inventory item, creates offer, and publishes listing. "
        "On success, this updates external listing ID and URL on the selected listing."
    )
    load_ebay_publish_workspace = st.checkbox(
        "Load eBay Publish Workspace (slower)",
        value=bool(st.session_state.get("listings_load_ebay_publish_workspace", False)),
        key="listings_load_ebay_publish_workspace",
        help="Defers heavy publish workspace state hydration, dependency checks, and manage-offer controls unless explicitly requested.",
    )
    if not load_ebay_publish_workspace:
        st.caption(
            "eBay publish workspace is deferred. Enable `Load eBay Publish Workspace (slower)` "
            "to access publish/revise/preflight/manage controls."
        )
        return
    ebay = EbayClient()
    allow_sandbox_ops = get_runtime_bool(
        repo,
        "ebay_allow_sandbox_seller_ops",
        bool(settings.ebay_allow_sandbox_seller_ops),
    )
    sandbox_seller_ops_blocked = ebay.environment != "production" and not allow_sandbox_ops
    if sandbox_seller_ops_blocked:
        st.warning(
            "Sandbox mode detected. eBay seller operations are disabled by default because sandbox seller onboarding "
            "and policy/location APIs are often unreliable. Set `EBAY_ALLOW_SANDBOX_SELLER_OPS=true` to override."
        )
    if selected_listing.marketplace != "ebay":
        st.info("Selected listing marketplace is not `ebay`. Choose an eBay listing to publish.")
        return

    product = product_by_id.get(int(selected_listing.product_id or 0))
    if product is None:
        st.error(f"Product #{selected_listing.product_id} not found for selected listing.")
        return

    default_description = _extract_listing_details_text(
        selected_listing,
        fallback=(product.description or "").strip() or str(selected_listing.listing_title or "").strip(),
    )
    default_condition_description = _product_ai_grading_description(product)
    default_description = _with_ai_grading_notes(
        default_description,
        grading_description=default_condition_description,
    )
    publish_meta = _listing_publish_meta_cached(selected_listing)
    preset_rows: list[object] = []
    publish_media_rows = _listing_media_rows_cached(int(selected_listing.id))
    image_media_items = [m for m in publish_media_rows if m.media_type == "image"]
    video_media_items = [m for m in publish_media_rows if m.media_type == "video"]
    image_options = {
        f"#{m.id} | {m.original_filename}": m for m in image_media_items
    }
    video_options = {
        f"#{m.id} | {m.original_filename}": m for m in video_media_items
    }
    prior_video_upload_meta = publish_meta.get("video_upload") if isinstance(publish_meta.get("video_upload"), dict) else {}
    default_video_label = _default_ebay_video_label(
        video_options,
        preferred_media_id=int(_to_float((prior_video_upload_meta or {}).get("media_asset_id"), 0.0)),
    )
    publish_formats = ["FIXED_PRICE", "AUCTION"]
    auction_durations = ["DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"]
    condition_options = ["NEW", "LIKE_NEW", "USED_EXCELLENT", "USED_VERY_GOOD", "USED_GOOD", "USED_ACCEPTABLE"]
    runtime_defaults = get_runtime_values(
        repo,
        {
            "ebay_user_access_token": settings.ebay_user_access_token,
            "ebay_marketplace_id": settings.ebay_marketplace_id,
            "ebay_currency": settings.ebay_currency,
            "ebay_content_language": settings.ebay_content_language,
            "ebay_merchant_location_key": settings.ebay_merchant_location_key,
            "ebay_payment_policy_id": settings.ebay_payment_policy_id,
            "ebay_fulfillment_policy_id": settings.ebay_fulfillment_policy_id,
            "ebay_return_policy_id": settings.ebay_return_policy_id,
            "ebay_best_offer_default": False,
            "ebay_best_offer_auto_accept_default": "0.0",
            "ebay_best_offer_minimum_default": "0.0",
            "ebay_auction_duration_default": "DAYS_5",
            "ebay_auction_start_default": "1.0",
            "ebay_auction_reserve_default": "0.0",
            "ebay_auction_buy_now_default": "0.0",
            "ebay_category_id": "",
            "ebay_shipping_service_default": "USPS Ground Advantage",
            "ebay_handling_days_default": "3",
            "ebay_shipping_cost_default": "0.0",
        },
    )
    default_token = str(runtime_defaults.get("ebay_user_access_token") or "").strip()
    default_marketplace_id = str(runtime_defaults.get("ebay_marketplace_id") or "").strip()
    default_currency = str(runtime_defaults.get("ebay_currency") or "").strip()
    default_content_language = str(runtime_defaults.get("ebay_content_language") or "").strip()
    default_merchant_location = str(runtime_defaults.get("ebay_merchant_location_key") or "").strip()
    default_payment_policy = str(runtime_defaults.get("ebay_payment_policy_id") or "").strip()
    default_fulfillment_policy = str(runtime_defaults.get("ebay_fulfillment_policy_id") or "").strip()
    default_return_policy = str(runtime_defaults.get("ebay_return_policy_id") or "").strip()
    default_best_offer_enabled = bool(runtime_defaults.get("ebay_best_offer_default"))
    default_best_offer_auto_accept = _to_float(runtime_defaults.get("ebay_best_offer_auto_accept_default"), 0.0)
    default_best_offer_minimum = _to_float(runtime_defaults.get("ebay_best_offer_minimum_default"), 0.0)
    default_auction_start = _to_float(runtime_defaults.get("ebay_auction_start_default"), 1.0)
    default_auction_reserve = _to_float(runtime_defaults.get("ebay_auction_reserve_default"), 0.0)
    default_auction_buy_now = _to_float(runtime_defaults.get("ebay_auction_buy_now_default"), 0.0)

    defaults = {
        "ebay_pub_title": str(selected_listing.listing_title or "").strip(),
        "ebay_pub_format": "FIXED_PRICE",
        "ebay_pub_auction_duration": "DAYS_5",
        "ebay_pub_best_offer_enabled": bool(default_best_offer_enabled),
        "ebay_pub_best_offer_auto_accept": max(0.0, float(default_best_offer_auto_accept)),
        "ebay_pub_best_offer_minimum": max(0.0, float(default_best_offer_minimum)),
        "ebay_pub_qty": max(1, int(selected_listing.quantity_listed or 1)),
        "ebay_pub_condition": "NEW",
        "ebay_pub_category_id": str(runtime_defaults.get("ebay_category_id") or "").strip(),
        "ebay_pub_store_category_names": [],
        "ebay_pub_fixed_price": max(0.01, float(selected_listing.listing_price)),
        "ebay_pub_auction_start": max(0.01, float(default_auction_start)),
        "ebay_pub_auction_reserve": max(0.0, float(default_auction_reserve)),
        "ebay_pub_auction_buy_now": max(0.0, float(default_auction_buy_now)),
        "ebay_pub_description": default_description,
        "ebay_pub_merchant_location_key": default_merchant_location,
        "ebay_pub_payment_policy_id": default_payment_policy,
        "ebay_pub_fulfillment_policy_id": default_fulfillment_policy,
        "ebay_pub_return_policy_id": default_return_policy,
        "ebay_pub_access_token": default_token,
        "ebay_pub_marketplace_id": default_marketplace_id,
        "ebay_pub_currency": default_currency,
        "ebay_pub_content_language": default_content_language,
        "ebay_pub_upload_to_eps": True,
        "ebay_pub_upload_video_to_ebay": bool(default_video_label),
        "ebay_pub_selected_images": list(image_options.keys()),
        "ebay_pub_selected_video": default_video_label or "None",
        "ebay_pub_package_weight_oz": max(0.0, _to_float(product.package_weight_oz, 0.0)),
        "ebay_pub_package_length_in": max(0.0, _to_float(product.package_length_in, 0.0)),
        "ebay_pub_package_width_in": max(0.0, _to_float(product.package_width_in, 0.0)),
        "ebay_pub_package_height_in": max(0.0, _to_float(product.package_height_in, 0.0)),
        "ebay_pub_subtitle": "",
        "ebay_pub_condition_description": default_condition_description,
        "ebay_pub_aspects_json": "",
        "ebay_pub_shipping_service": str(runtime_defaults.get("ebay_shipping_service_default") or "USPS Ground Advantage").strip(),
        "ebay_pub_handling_days": int(_to_float(runtime_defaults.get("ebay_handling_days_default"), 3)),
        "ebay_pub_shipping_cost": float(_to_float(runtime_defaults.get("ebay_shipping_cost_default"), 0.0)),
        "ebay_pub_estimated_buyer_shipping": 0.0,
        "ebay_pub_estimated_promoted_rate": 0.0,
        "ebay_pub_volume_pricing_json": "",
        "ebay_pub_volume_discount_buy2": 0.0,
        "ebay_pub_volume_discount_buy3": 0.0,
        "ebay_pub_volume_discount_buy4": 0.0,
        "ebay_pub_include_volume_pricing_in_description": False,
    }
    ebay_publish_draft_state_keys = list(LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS)
    _listings_apply_pending_ebay_publish_updates(
        allowed_keys=set(ebay_publish_draft_state_keys),
    )
    if bool(st.session_state.pop("ebay_pub_clear_local_state_requested", False)):
        for draft_key in ebay_publish_draft_state_keys:
            st.session_state.pop(draft_key, None)
        st.session_state.pop("ebay_pub_selected_listing_signature", None)
        st.session_state.pop("ebay_pub_last_autosave_signature", None)
        st.session_state.pop("ebay_pub_last_autosave_scope", None)
        st.session_state.pop("ebay_pub_last_autosave_at", None)
        st.session_state.pop("ebay_pub_last_draft_id", None)
    draft_scope_key = _listings_ebay_publish_scope_key(int(selected_listing.id))
    load_publish_draft_and_presets = st.checkbox(
        "Load Publish Draft + Presets (slower)",
        value=False,
        key=f"listings_load_publish_draft_and_presets_{int(selected_listing.id)}",
        help="Defers workflow-draft retrieval and preset profile hydration unless explicitly requested.",
    )
    saved_publish_draft = None
    saved_publish_payload: dict = {}
    if load_publish_draft_and_presets:
        saved_publish_draft = repo.load_workflow_draft(
            environment=settings.app_env,
            workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
            username=user.username,
            scope_key=draft_scope_key,
            active_only=True,
        )
        if saved_publish_draft is not None:
            try:
                parsed = json.loads(str(saved_publish_draft.draft_json or "{}"))
                if isinstance(parsed, dict):
                    saved_publish_payload = parsed
            except Exception:
                saved_publish_payload = {}
        preset_rows = _list_ebay_publish_presets_cached(active_only=True)
    show_publish_draft_flash = str(st.session_state.pop("ebay_pub_draft_flash", "") or "").strip()
    if show_publish_draft_flash:
        st.success(show_publish_draft_flash)
    for state_key, state_value in defaults.items():
        _safe_session_set(state_key, state_value, only_if_missing=True)

    listing_signature = f"{int(selected_listing.id)}:{iso_or_none(selected_listing.updated_at)}"
    skip_signature_reset_once = bool(st.session_state.pop("ebay_pub_skip_signature_reset_once", False))
    if (
        str(st.session_state.get("ebay_pub_selected_listing_signature") or "") != listing_signature
        and not skip_signature_reset_once
    ):
        format_type = str(publish_meta.get("format") or "FIXED_PRICE").strip().upper()
        if format_type not in {"FIXED_PRICE", "AUCTION"}:
            format_type = "FIXED_PRICE"
        updates = {
            "ebay_pub_title": str(selected_listing.listing_title or "").strip(),
            "ebay_pub_format": format_type,
            "ebay_pub_auction_duration": str(
                publish_meta.get("listing_duration") or "DAYS_5"
            ).strip().upper(),
            "ebay_pub_best_offer_enabled": bool(
                publish_meta.get("best_offer_enabled", defaults["ebay_pub_best_offer_enabled"])
            ),
            "ebay_pub_best_offer_auto_accept": max(
                0.0,
                float(_to_float(publish_meta.get("best_offer_auto_accept"), defaults["ebay_pub_best_offer_auto_accept"])),
            ),
            "ebay_pub_best_offer_minimum": max(
                0.0,
                float(_to_float(publish_meta.get("best_offer_minimum"), defaults["ebay_pub_best_offer_minimum"])),
            ),
            "ebay_pub_qty": max(1, int(selected_listing.quantity_listed or 1)),
            "ebay_pub_category_id": str(
                publish_meta.get("category_id")
                or defaults["ebay_pub_category_id"]
                or ""
            ).strip(),
            "ebay_pub_store_category_names": _normalize_store_category_names(
                publish_meta.get("store_category_names")
                or defaults["ebay_pub_store_category_names"]
            ),
            "ebay_pub_fixed_price": max(0.01, float(selected_listing.listing_price or 0.01)),
            "ebay_pub_auction_start": max(
                0.01,
                float(_to_float(publish_meta.get("auction_start_price"), defaults["ebay_pub_auction_start"])),
            ),
            "ebay_pub_auction_reserve": max(
                0.0,
                float(_to_float(publish_meta.get("auction_reserve_price"), defaults["ebay_pub_auction_reserve"])),
            ),
            "ebay_pub_auction_buy_now": max(
                0.0,
                float(_to_float(publish_meta.get("auction_buy_now_price"), defaults["ebay_pub_auction_buy_now"])),
            ),
            "ebay_pub_description": _extract_listing_details_text(
                selected_listing,
                fallback=default_description,
            ),
            "ebay_pub_merchant_location_key": str(
                publish_meta.get("merchant_location_key") or defaults["ebay_pub_merchant_location_key"]
            ).strip(),
            "ebay_pub_payment_policy_id": str(
                publish_meta.get("payment_policy_id") or defaults["ebay_pub_payment_policy_id"]
            ).strip(),
            "ebay_pub_fulfillment_policy_id": str(
                publish_meta.get("fulfillment_policy_id") or defaults["ebay_pub_fulfillment_policy_id"]
            ).strip(),
            "ebay_pub_return_policy_id": str(
                publish_meta.get("return_policy_id") or defaults["ebay_pub_return_policy_id"]
            ).strip(),
            "ebay_pub_marketplace_id": str(
                publish_meta.get("marketplace_id") or defaults["ebay_pub_marketplace_id"]
            ).strip(),
            "ebay_pub_currency": str(publish_meta.get("currency") or defaults["ebay_pub_currency"]).strip(),
            "ebay_pub_content_language": str(
                publish_meta.get("content_language") or defaults["ebay_pub_content_language"]
            ).strip(),
            "ebay_pub_upload_video_to_ebay": bool(
                default_video_label and publish_meta.get("upload_video_to_ebay", defaults["ebay_pub_upload_video_to_ebay"])
            ),
            "ebay_pub_selected_video": default_video_label or "None",
            "ebay_pub_package_weight_oz": max(
                0.0,
                float(_to_float(publish_meta.get("package_weight_oz"), defaults["ebay_pub_package_weight_oz"])),
            ),
            "ebay_pub_package_length_in": max(
                0.0,
                float(_to_float(publish_meta.get("package_length_in"), defaults["ebay_pub_package_length_in"])),
            ),
            "ebay_pub_package_width_in": max(
                0.0,
                float(_to_float(publish_meta.get("package_width_in"), defaults["ebay_pub_package_width_in"])),
            ),
            "ebay_pub_package_height_in": max(
                0.0,
                float(_to_float(publish_meta.get("package_height_in"), defaults["ebay_pub_package_height_in"])),
            ),
            "ebay_pub_subtitle": str(publish_meta.get("subtitle") or "").strip(),
            "ebay_pub_condition_description": str(
                publish_meta.get("condition_description") or default_condition_description or ""
            ).strip(),
        }
        aspects_payload = publish_meta.get("aspects")
        if isinstance(aspects_payload, dict):
            updates["ebay_pub_aspects_json"] = json.dumps(aspects_payload, indent=2)
        else:
            updates["ebay_pub_aspects_json"] = str(publish_meta.get("aspects_json") or "").strip()
        updates["ebay_pub_shipping_service"] = str(
            publish_meta.get("shipping_service") or defaults["ebay_pub_shipping_service"]
        ).strip()
        updates["ebay_pub_handling_days"] = max(
            0,
            int(_to_float(publish_meta.get("handling_days"), defaults["ebay_pub_handling_days"])),
        )
        updates["ebay_pub_shipping_cost"] = max(
            0.0,
            float(_to_float(publish_meta.get("shipping_cost"), defaults["ebay_pub_shipping_cost"])),
        )
        updates["ebay_pub_estimated_buyer_shipping"] = max(
            0.0,
            float(
                _to_float(
                    publish_meta.get("estimated_buyer_paid_shipping"),
                    defaults["ebay_pub_estimated_buyer_shipping"],
                )
            ),
        )
        updates["ebay_pub_estimated_promoted_rate"] = max(
            0.0,
            float(
                _to_float(
                    publish_meta.get("estimated_promoted_rate_percent"),
                    defaults["ebay_pub_estimated_promoted_rate"],
                )
            ),
        )
        volume_tiers = publish_meta.get("volume_pricing_tiers")
        if isinstance(volume_tiers, list):
            updates["ebay_pub_volume_pricing_json"] = json.dumps(volume_tiers, indent=2)
            buy2, buy3, buy4 = _volume_pricing_discount_controls_from_tiers(volume_tiers)
            updates["ebay_pub_volume_discount_buy2"] = float(buy2)
            updates["ebay_pub_volume_discount_buy3"] = float(buy3)
            updates["ebay_pub_volume_discount_buy4"] = float(buy4)
        else:
            updates["ebay_pub_volume_pricing_json"] = ""
            updates["ebay_pub_volume_discount_buy2"] = 0.0
            updates["ebay_pub_volume_discount_buy3"] = 0.0
            updates["ebay_pub_volume_discount_buy4"] = 0.0
        _queue_ebay_publish_updates(updates)
        _listings_apply_pending_ebay_publish_updates(
            allowed_keys=set(ebay_publish_draft_state_keys),
        )
    _safe_session_set("ebay_pub_selected_listing_signature", listing_signature)

    if not load_publish_draft_and_presets:
        st.caption(
            "Workflow draft and preset controls are deferred. Enable "
            "`Load Publish Draft + Presets (slower)` to use draft/preset actions."
        )
    else:
        st.markdown("#### Workflow Draft")
        wd1, wd2, wd3 = st.columns([1, 1, 2])
        with wd1:
            if st.button("Save Publish Draft", key="ebay_pub_save_draft_btn"):
                # Commit save on next rerun so latest widget values (for example category ID) are present.
                st.session_state["ebay_pub_save_draft_requested"] = True
                st.rerun()
        with wd2:
            if st.button("Resume Publish Draft", key="ebay_pub_resume_draft_btn"):
                if saved_publish_payload:
                    _listings_apply_ebay_publish_draft_payload(
                        saved_publish_payload,
                        state_keys=ebay_publish_draft_state_keys,
                    )
                    # Avoid one-run signature reset clobbering resumed draft values (for example category ID).
                    st.session_state["ebay_pub_skip_signature_reset_once"] = True
                    repo.append_workflow_event(
                        environment=settings.app_env,
                        workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
                        username=user.username,
                        scope_key=draft_scope_key,
                        action="resume_draft",
                        status="ok",
                        message="Operator resumed Listings publish draft.",
                        payload={
                            "draft_id": int(getattr(saved_publish_draft, "id", 0) or 0),
                            "listing_id": int(selected_listing.id),
                        },
                        draft_id=int(getattr(saved_publish_draft, "id", 0) or 0),
                        actor=user.username,
                    )
                    st.session_state["ebay_pub_draft_flash"] = "Resumed publish draft."
                    st.rerun()
                else:
                    st.info("No saved publish draft for this listing yet.")
        with wd3:
            if st.button("Clear Publish Draft", key="ebay_pub_clear_draft_btn"):
                cleared = repo.clear_workflow_draft(
                    environment=settings.app_env,
                    workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
                    username=user.username,
                    scope_key=draft_scope_key,
                    actor=user.username,
                    reason="operator_clear_publish_draft",
                )
                if cleared:
                    repo.append_workflow_event(
                        environment=settings.app_env,
                        workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
                        username=user.username,
                        scope_key=draft_scope_key,
                        action="clear_draft",
                        status="ok",
                        message="Operator cleared Listings publish draft.",
                        payload={"listing_id": int(selected_listing.id)},
                        draft_id=int(getattr(saved_publish_draft, "id", 0) or 0),
                        actor=user.username,
                    )
                st.session_state["ebay_pub_clear_local_state_requested"] = True
                st.session_state["ebay_pub_draft_flash"] = "Cleared publish draft."
                st.rerun()
        autosave_at = str(st.session_state.get("ebay_pub_last_autosave_at") or "").strip()
        if autosave_at:
            st.caption(f"Last draft autosave: {autosave_at}")
        elif saved_publish_draft is not None:
            st.caption("Saved publish draft found. Resume to restore state.")
    valid_image_labels = set(image_options.keys())
    _safe_session_set(
        "ebay_pub_selected_images",
        [
            label
            for label in st.session_state.get("ebay_pub_selected_images", [])
            if label in valid_image_labels
        ]
        or list(image_options.keys()),
    )
    valid_video_labels = {"None"} | set(video_options.keys())
    coerced_video_label = _coerce_selected_ebay_video_label(
        upload_video_to_ebay=bool(st.session_state.get("ebay_pub_upload_video_to_ebay")),
        selected_video_label=str(st.session_state.get("ebay_pub_selected_video") or "None"),
        default_video_label=default_video_label,
        valid_video_labels=valid_video_labels,
    )
    if st.session_state.get("ebay_pub_selected_video") != coerced_video_label:
        _safe_session_set("ebay_pub_selected_video", coerced_video_label)

    if load_publish_draft_and_presets:
        st.markdown("#### eBay Publish Presets")
        preset_map: dict[str, object] = {}
        preset_by_name_lower: dict[str, object] = {}
        for preset_row in preset_rows:
            preset_name_raw = str(getattr(preset_row, "name", "")).strip()
            preset_label = (
                f"#{int(getattr(preset_row, 'id', 0) or 0)} | "
                f"{preset_name_raw}{' (default)' if bool(getattr(preset_row, 'is_default', False)) else ''}"
            )
            preset_map[preset_label] = preset_row
            if preset_name_raw:
                preset_by_name_lower[preset_name_raw.lower()] = preset_row
        selected_preset_label = st.selectbox(
            "Load Preset",
            options=["None"] + list(preset_map.keys()),
            key="ebay_pub_preset_select",
        )
        pcol1, pcol2 = st.columns(2)
        with pcol1:
            apply_preset = st.button("Apply Selected Preset", key="ebay_pub_apply_preset")
        with pcol2:
            with st.form("ebay_publish_save_preset_form"):
                preset_name = st.text_input("Preset Name", key="ebay_pub_save_preset_name")
                preset_make_default = st.checkbox("Set As Default For My User/Env", value=False)
                save_preset = st.form_submit_button("Save Current As Preset")

        if apply_preset and selected_preset_label != "None":
            preset = preset_map[selected_preset_label]
            format_type = str(preset.format_type or "FIXED_PRICE").strip().upper()
            updates = {
                "ebay_pub_format": format_type,
                "ebay_pub_auction_duration": str(preset.listing_duration or "DAYS_5").strip().upper(),
                "ebay_pub_condition": str(preset.condition_value or "NEW").strip().upper(),
                "ebay_pub_category_id": str(preset.category_id or "").strip(),
                "ebay_pub_merchant_location_key": str(preset.merchant_location_key or "").strip(),
                "ebay_pub_payment_policy_id": str(preset.payment_policy_id or "").strip(),
                "ebay_pub_fulfillment_policy_id": str(preset.fulfillment_policy_id or "").strip(),
                "ebay_pub_return_policy_id": str(preset.return_policy_id or "").strip(),
                "ebay_pub_marketplace_id": str(preset.marketplace_id or default_marketplace_id).strip(),
                "ebay_pub_currency": str(preset.currency or default_currency).strip(),
                "ebay_pub_content_language": str(preset.content_language or default_content_language).strip(),
            }
            if format_type != "FIXED_PRICE":
                updates["ebay_pub_best_offer_enabled"] = False
            _queue_ebay_publish_updates(updates, flash=f"Applied preset `{preset.name}`.")
            st.rerun()

        if save_preset:
            if not ensure_permission(user, "create", "Save eBay Publish Preset"):
                st.stop()
            if not (preset_name or "").strip():
                st.error("Preset name is required.")
            else:
                preset_name_normalized = str(preset_name).strip()
                preset_name_key = preset_name_normalized.lower()
                existing = preset_by_name_lower.get(preset_name_key)
                duration_value = (
                    "GTC"
                    if st.session_state.get("ebay_pub_format") == "FIXED_PRICE"
                    else st.session_state.get("ebay_pub_auction_duration", "DAYS_5")
                )
                payload = {
                    "marketplace_id": (st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
                    "currency": (st.session_state.get("ebay_pub_currency") or default_currency).strip(),
                    "content_language": (st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                    "merchant_location_key": (st.session_state.get("ebay_pub_merchant_location_key") or "").strip(),
                    "payment_policy_id": (st.session_state.get("ebay_pub_payment_policy_id") or "").strip(),
                    "fulfillment_policy_id": (st.session_state.get("ebay_pub_fulfillment_policy_id") or "").strip(),
                    "return_policy_id": (st.session_state.get("ebay_pub_return_policy_id") or "").strip(),
                    "category_id": (st.session_state.get("ebay_pub_category_id") or "").strip(),
                    "format_type": (st.session_state.get("ebay_pub_format") or "FIXED_PRICE").strip().upper(),
                    "listing_duration": (duration_value or "GTC").strip().upper(),
                    "condition_value": (st.session_state.get("ebay_pub_condition") or "NEW").strip().upper(),
                    "is_default": bool(preset_make_default),
                    "is_active": True,
                }
                try:
                    if existing is None:
                        repo.create_ebay_publish_preset(
                            environment=settings.app_env,
                            username=user.username,
                            name=preset_name_normalized,
                            actor=user.username,
                            **payload,
                        )
                    else:
                        repo.update_ebay_publish_preset(existing.id, payload, actor=user.username)
                    st.success(f"Preset `{preset_name_normalized}` saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Save preset failed: {exc}")

        st.markdown("#### Format Template Presets")
        st.caption("Apply a format-oriented template quickly before publish/revise.")
        fpt1, fpt2 = st.columns(2)
        with fpt1:
            apply_fixed_template = st.button(
                "Apply Template: Fixed Price Standard",
                key="ebay_pub_apply_fixed_template_btn",
                use_container_width=True,
            )
        with fpt2:
            apply_auction_template = st.button(
                "Apply Template: Auction Standard",
                key="ebay_pub_apply_auction_template_btn",
                use_container_width=True,
            )

        if apply_fixed_template:
            _queue_ebay_publish_updates(
                {
                    "ebay_pub_format": "FIXED_PRICE",
                    "ebay_pub_best_offer_enabled": bool(runtime_defaults.get("ebay_best_offer_default")),
                    "ebay_pub_best_offer_auto_accept": max(
                        0.0,
                        _to_float(runtime_defaults.get("ebay_best_offer_auto_accept_default"), 0.0),
                    ),
                    "ebay_pub_best_offer_minimum": max(
                        0.0,
                        _to_float(runtime_defaults.get("ebay_best_offer_minimum_default"), 0.0),
                    ),
                    "ebay_pub_fixed_price": max(0.01, float(selected_listing.listing_price or 0.0)),
                    "ebay_pub_marketplace_id": str(
                        st.session_state.get("ebay_workspace_store_marketplace_id_input")
                        or runtime_defaults.get("ebay_marketplace_id")
                        or default_marketplace_id
                    ).strip(),
                    "ebay_pub_currency": str(
                        st.session_state.get("ebay_workspace_store_currency_input")
                        or runtime_defaults.get("ebay_currency")
                        or default_currency
                    ).strip(),
                    "ebay_pub_content_language": str(
                        st.session_state.get("ebay_workspace_store_content_language_input")
                        or runtime_defaults.get("ebay_content_language")
                        or default_content_language
                    ).strip(),
                    "ebay_pub_merchant_location_key": str(
                        st.session_state.get("ebay_workspace_store_merchant_location_key_input")
                        or runtime_defaults.get("ebay_merchant_location_key")
                        or default_merchant_location
                    ).strip(),
                    "ebay_pub_payment_policy_id": str(
                        st.session_state.get("ebay_workspace_store_payment_policy_id_input")
                        or runtime_defaults.get("ebay_payment_policy_id")
                        or default_payment_policy
                    ).strip(),
                    "ebay_pub_fulfillment_policy_id": str(
                        st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input")
                        or runtime_defaults.get("ebay_fulfillment_policy_id")
                        or default_fulfillment_policy
                    ).strip(),
                    "ebay_pub_return_policy_id": str(
                        st.session_state.get("ebay_workspace_store_return_policy_id_input")
                        or runtime_defaults.get("ebay_return_policy_id")
                        or default_return_policy
                    ).strip(),
                    "ebay_pub_category_id": str(
                        st.session_state.get("ebay_workspace_store_category_id_input")
                        or runtime_defaults.get("ebay_category_id")
                        or ""
                    ).strip(),
                },
                flash="Applied fixed-price template defaults.",
            )
            st.rerun()

        if apply_auction_template:
            _queue_ebay_publish_updates(
                {
                    "ebay_pub_format": "AUCTION",
                    "ebay_pub_best_offer_enabled": False,
                    "ebay_pub_best_offer_auto_accept": 0.0,
                    "ebay_pub_best_offer_minimum": 0.0,
                    "ebay_pub_auction_duration": str(
                        st.session_state.get("ebay_workspace_store_auction_duration_input")
                        or runtime_defaults.get("ebay_auction_duration_default")
                        or "DAYS_5"
                    ).strip(),
                    "ebay_pub_auction_start": float(
                        st.session_state.get("ebay_workspace_store_auction_start_input")
                        or _to_float(runtime_defaults.get("ebay_auction_start_default"), 1.0)
                    ),
                    "ebay_pub_auction_reserve": float(
                        st.session_state.get("ebay_workspace_store_auction_reserve_input")
                        or _to_float(runtime_defaults.get("ebay_auction_reserve_default"), 0.0)
                    ),
                    "ebay_pub_auction_buy_now": float(
                        st.session_state.get("ebay_workspace_store_auction_buy_now_input")
                        or _to_float(runtime_defaults.get("ebay_auction_buy_now_default"), 0.0)
                    ),
                    "ebay_pub_marketplace_id": str(
                        st.session_state.get("ebay_workspace_store_marketplace_id_input")
                        or runtime_defaults.get("ebay_marketplace_id")
                        or default_marketplace_id
                    ).strip(),
                    "ebay_pub_currency": str(
                        st.session_state.get("ebay_workspace_store_currency_input")
                        or runtime_defaults.get("ebay_currency")
                        or default_currency
                    ).strip(),
                    "ebay_pub_content_language": str(
                        st.session_state.get("ebay_workspace_store_content_language_input")
                        or runtime_defaults.get("ebay_content_language")
                        or default_content_language
                    ).strip(),
                    "ebay_pub_merchant_location_key": str(
                        st.session_state.get("ebay_workspace_store_merchant_location_key_input")
                        or runtime_defaults.get("ebay_merchant_location_key")
                        or default_merchant_location
                    ).strip(),
                    "ebay_pub_payment_policy_id": str(
                        st.session_state.get("ebay_workspace_store_payment_policy_id_input")
                        or runtime_defaults.get("ebay_payment_policy_id")
                        or default_payment_policy
                    ).strip(),
                    "ebay_pub_fulfillment_policy_id": str(
                        st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input")
                        or runtime_defaults.get("ebay_fulfillment_policy_id")
                        or default_fulfillment_policy
                    ).strip(),
                    "ebay_pub_return_policy_id": str(
                        st.session_state.get("ebay_workspace_store_return_policy_id_input")
                        or runtime_defaults.get("ebay_return_policy_id")
                        or default_return_policy
                    ).strip(),
                    "ebay_pub_category_id": str(
                        st.session_state.get("ebay_workspace_store_category_id_input")
                        or runtime_defaults.get("ebay_category_id")
                        or ""
                    ).strip(),
                },
                flash="Applied auction template defaults.",
            )
            st.rerun()

    load_category_assist = st.checkbox(
        "Load eBay Category Assist (slower)",
        value=False,
        key=f"listings_load_ebay_category_assist_{int(selected_listing.id)}",
        help="Defers category suggestion controls and lookup/cached-query hydration unless explicitly requested.",
    )
    if not load_category_assist:
        st.caption(
            "Category assist is deferred. Enable `Load eBay Category Assist (slower)` "
            "to search and apply taxonomy suggestions."
        )
    else:
        current_seed_product_id = int(getattr(product, "id", 0) or 0)
        current_seed = _category_query_seed(
            title=str(getattr(selected_listing, "listing_title", "") or "").strip(),
            category=str(getattr(product, "category", "") or "").strip(),
            metal_type=str(getattr(product, "metal_type", "") or "").strip(),
            sku=str(getattr(product, "sku", "") or "").strip(),
        )
        _safe_session_set(
            "ebay_pub_category_query_seed_product_id",
            current_seed_product_id,
            only_if_missing=True,
        )
        if (
            "ebay_pub_category_query" not in st.session_state
            or int(st.session_state.get("ebay_pub_category_query_seed_product_id") or 0)
            != current_seed_product_id
        ):
            _safe_session_set("ebay_pub_category_query", current_seed)
            _safe_session_set("ebay_pub_category_query_seed_product_id", current_seed_product_id)
        _safe_session_set("ebay_pub_category_suggestions", [], only_if_missing=True)
        _safe_session_set("ebay_pub_category_suggestion_select", "(none)", only_if_missing=True)

        st.markdown("#### eBay Category Assist")
        st.caption("Search eBay taxonomy suggestions and apply category ID into the publish form.")
        ca1, ca2, ca3, ca4 = st.columns([2, 1, 1, 1])
        with ca1:
            category_query = st.text_input(
                "Category Search Keywords",
                key="ebay_pub_category_query",
                placeholder="Example: Morgan silver dollar uncirculated",
            ).strip()
        with ca2:
            fetch_category_suggestions = st.button(
                "Fetch Category Suggestions",
                key="ebay_pub_fetch_category_suggestions_btn",
            )
        with ca3:
            apply_category_suggestion = st.button(
                "Apply Selected Category",
                key="ebay_pub_apply_category_suggestion_btn",
            )
        with ca4:
            refresh_category_suggestions = st.button(
                "Refresh from eBay",
                key="ebay_pub_refresh_category_suggestions_btn",
                help="Bypass DB cache and fetch fresh suggestions from eBay Taxonomy API.",
            )

        suggestion_rows = st.session_state.get("ebay_pub_category_suggestions") or []
        suggestion_map: dict[str, str] = {"(none)": ""}
        for row in suggestion_rows:
            cid = str((row or {}).get("category_id") or "").strip()
            if not cid:
                continue
            path = str((row or {}).get("path") or "").strip()
            cname = str((row or {}).get("category_name") or "").strip()
            label = f"{cid} - {path}" if path else (f"{cid} - {cname}" if cname else cid)
            suggestion_map[label] = cid
        selected_category_suggestion_label = st.selectbox(
            "Suggested Categories",
            options=list(suggestion_map.keys()),
            key="ebay_pub_category_suggestion_select",
        )

        if fetch_category_suggestions or refresh_category_suggestions:
            token_for_category_lookup = str(st.session_state.get("ebay_pub_access_token") or "").strip() or str(
                default_token or ""
            ).strip()
            marketplace_for_category_lookup = str(
                st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id
            ).strip()
            if not ebay.is_configured():
                st.error("Category fetch failed: eBay app credentials are not configured.")
            elif not category_query:
                st.error("Category fetch failed: enter search keywords first.")
            else:
                try:
                    force_refresh = bool(refresh_category_suggestions)
                    cached = []
                    if not force_refresh:
                        cached = repo.list_cached_ebay_category_suggestions(
                            environment=settings.app_env,
                            marketplace_id=marketplace_for_category_lookup,
                            query=category_query,
                            limit=20,
                        )
                    if cached and not force_refresh:
                        fresh = list(cached)
                        st.session_state["ebay_pub_category_suggestions"] = fresh
                        st.success(f"Loaded {len(fresh)} cached category suggestion(s).")
                    else:
                        if not token_for_category_lookup:
                            st.error("Category fetch failed: missing user access token.")
                            st.stop()
                        fresh = ebay.get_category_suggestions(
                            access_token=token_for_category_lookup,
                            query=category_query,
                            marketplace_id=marketplace_for_category_lookup,
                            limit=20,
                        )
                        st.session_state["ebay_pub_category_suggestions"] = fresh
                        if fresh:
                            repo.cache_ebay_category_suggestions(
                                environment=settings.app_env,
                                marketplace_id=marketplace_for_category_lookup,
                                query=category_query,
                                suggestions=fresh,
                                actor=user.username,
                            )
                        if force_refresh:
                            st.success(f"Refreshed {len(fresh)} category suggestion(s) from eBay.")
                        else:
                            st.success(f"Loaded {len(fresh)} category suggestion(s) from eBay.")
                    if fresh:
                        first_id = str((fresh[0] or {}).get("category_id") or "").strip()
                        first_path = str((fresh[0] or {}).get("path") or "").strip()
                        first_name = str((fresh[0] or {}).get("category_name") or "").strip()
                        first_label = (
                            f"{first_id} - {first_path}"
                            if first_path
                            else (f"{first_id} - {first_name}" if first_name else first_id)
                        )
                        if first_label in suggestion_map:
                            try:
                                st.session_state["ebay_pub_category_suggestion_select"] = first_label
                            except StreamlitAPIException:
                                # Widget key may already be locked in this rerun; safe to skip.
                                pass
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category fetch failed: {exc}")

        if apply_category_suggestion:
            selected_id = str(suggestion_map.get(selected_category_suggestion_label) or "").strip()
            if not selected_id:
                first_row = suggestion_rows[0] if suggestion_rows else {}
                selected_id = str((first_row or {}).get("category_id") or "").strip()
            if not selected_id:
                st.info("Select a suggested category first.")
            else:
                _queue_ebay_publish_category_id_update(selected_id)
                st.rerun()
    current_category_state = str(st.session_state.get("ebay_pub_category_id") or "").strip()
    last_category_state = str(st.session_state.get("ebay_pub_last_category_id") or "").strip()
    last_category_listing_id = int(st.session_state.get("ebay_pub_last_category_listing_id") or 0)
    current_listing_id = int(getattr(selected_listing, "id", 0) or 0)
    last_category_matches_listing = (
        not current_listing_id
        or not last_category_listing_id
        or last_category_listing_id == current_listing_id
    )
    if not current_category_state and last_category_state and last_category_matches_listing:
        current_category_state = last_category_state
        st.session_state["ebay_pub_category_id"] = last_category_state
    if current_category_state:
        st.session_state["ebay_pub_last_category_id"] = current_category_state
        st.session_state["ebay_pub_last_category_listing_id"] = current_listing_id
    st.caption(f"Current Category ID in form state: `{current_category_state or '(empty)'}`")

    category_condition_rows = st.session_state.get("ebay_pub_category_condition_rows")
    if not isinstance(category_condition_rows, list):
        category_condition_rows = []
    condition_policy_marketplace_id = str(
        st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id or "EBAY_US"
    ).strip()
    condition_policy_signature = f"{condition_policy_marketplace_id.upper()}:{current_category_state}"
    if (
        current_category_state
        and st.session_state.get("ebay_pub_category_condition_signature") != condition_policy_signature
    ):
        category_condition_rows = []
        st.session_state["ebay_pub_category_condition_rows"] = []
        st.session_state["ebay_pub_category_condition_signature"] = condition_policy_signature
    elif not current_category_state and category_condition_rows:
        category_condition_rows = []
        st.session_state["ebay_pub_category_condition_rows"] = []
        st.session_state["ebay_pub_category_condition_signature"] = ""

    cc1, cc2, cc3 = st.columns([1, 1, 2])
    with cc1:
        if st.button("Load Category Conditions", key="ebay_pub_load_conditions_btn"):
            token_for_condition_lookup = str(st.session_state.get("ebay_pub_access_token") or "").strip() or str(
                default_token or ""
            ).strip()
            if not current_category_state:
                st.warning("Select or enter an eBay category ID first.")
            elif not token_for_condition_lookup:
                st.warning("Missing eBay user access token.")
            else:
                try:
                    policies = ebay.get_item_condition_policies(
                        access_token=token_for_condition_lookup,
                        category_id=current_category_state,
                        marketplace_id=condition_policy_marketplace_id,
                    )
                    category_condition_rows = normalize_ebay_condition_policy_rows(
                        policies,
                        category_id=current_category_state,
                    )
                    st.session_state["ebay_pub_category_condition_rows"] = category_condition_rows
                    st.session_state["ebay_pub_category_condition_signature"] = condition_policy_signature
                    current_condition = str(st.session_state.get("ebay_pub_condition") or "").strip().upper()
                    if category_condition_rows and not _is_condition_valid_for_loaded_policy(
                        category_condition_rows,
                        current_condition,
                    ):
                        st.session_state["ebay_pub_condition"] = str(category_condition_rows[0]["condition"])
                    st.success(f"Loaded {len(category_condition_rows)} eBay category condition option(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category condition fetch failed: {exc}")
    with cc2:
        if st.button("Refresh Category Conditions", key="ebay_pub_refresh_conditions_btn"):
            token_for_condition_lookup = str(st.session_state.get("ebay_pub_access_token") or "").strip() or str(
                default_token or ""
            ).strip()
            if not current_category_state:
                st.warning("Select or enter an eBay category ID first.")
            elif not token_for_condition_lookup:
                st.warning("Missing eBay user access token.")
            else:
                try:
                    policies = ebay.get_item_condition_policies(
                        access_token=token_for_condition_lookup,
                        category_id=current_category_state,
                        marketplace_id=condition_policy_marketplace_id,
                    )
                    category_condition_rows = normalize_ebay_condition_policy_rows(
                        policies,
                        category_id=current_category_state,
                    )
                    st.session_state["ebay_pub_category_condition_rows"] = category_condition_rows
                    st.session_state["ebay_pub_category_condition_signature"] = condition_policy_signature
                    current_condition = str(st.session_state.get("ebay_pub_condition") or "").strip().upper()
                    if category_condition_rows and not _is_condition_valid_for_loaded_policy(
                        category_condition_rows,
                        current_condition,
                    ):
                        st.session_state["ebay_pub_condition"] = str(category_condition_rows[0]["condition"])
                    st.success(f"Refreshed {len(category_condition_rows)} eBay category condition option(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Category condition refresh failed: {exc}")
    with cc3:
        if category_condition_rows:
            st.caption(
                f"eBay category conditions loaded for `{current_category_state}`: "
                + ", ".join(
                    f"{row.get('label')} ({row.get('condition')})"
                    for row in category_condition_rows[:6]
                )
            )
        else:
            st.caption(
                "Category-specific condition policy not loaded yet. Load it before publish to avoid eBay 25021 "
                "condition/category mismatches."
            )

    current_condition_state = str(st.session_state.get("ebay_pub_condition") or "NEW").strip().upper()
    condition_options = _condition_options(category_condition_rows, current_condition_state)
    condition_option_labels = _condition_option_labels(category_condition_rows, current_condition_state)

    category_aspect_rows = st.session_state.get("ebay_pub_category_aspect_rows")
    if not isinstance(category_aspect_rows, list):
        category_aspect_rows = []
    aspect_cache_marketplace_id = str(
        st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id or "EBAY_US"
    ).strip()
    aspect_cache_signature = f"{aspect_cache_marketplace_id.upper()}:{current_category_state}"
    if current_category_state and st.session_state.get("ebay_pub_category_aspect_signature") != aspect_cache_signature:
        category_aspect_rows = []
        st.session_state["ebay_pub_category_aspect_rows"] = category_aspect_rows
        st.session_state["ebay_pub_category_aspect_signature"] = aspect_cache_signature
    elif not current_category_state and category_aspect_rows:
        category_aspect_rows = []
        st.session_state["ebay_pub_category_aspect_rows"] = []
        st.session_state["ebay_pub_category_aspect_signature"] = ""
    car1, car2, car3 = st.columns([1, 1, 2])
    with car1:
        if st.button("Load Required Item Specifics", key="ebay_pub_load_required_aspects_btn"):
            marketplace_for_aspect_lookup = str(
                st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id or "EBAY_US"
            ).strip()
            if not current_category_state:
                st.warning("Select or enter an eBay category ID first.")
            else:
                cached = repo.get_cached_ebay_category_aspects(
                    environment=settings.app_env,
                    marketplace_id=marketplace_for_aspect_lookup,
                    category_id=current_category_state,
                )
                if cached:
                    category_aspect_rows = normalize_ebay_category_aspect_rows(cached.get("aspects") or [])
                    st.session_state["ebay_pub_category_aspect_rows"] = category_aspect_rows
                    st.session_state["ebay_pub_category_aspect_signature"] = aspect_cache_signature
                    st.success(
                        f"Loaded {len(category_aspect_rows)} cached category item specific(s)."
                    )
                else:
                    token_for_aspect_lookup = str(st.session_state.get("ebay_pub_access_token") or "").strip() or str(
                        default_token or ""
                    ).strip()
                    if not token_for_aspect_lookup:
                        st.warning("Missing eBay user access token.")
                        st.stop()
                    try:
                        raw_aspects = ebay.get_item_aspects_for_category(
                            access_token=token_for_aspect_lookup,
                            category_id=current_category_state,
                            marketplace_id=marketplace_for_aspect_lookup,
                        )
                        category_aspect_rows = normalize_ebay_category_aspect_rows(raw_aspects)
                        repo.cache_ebay_category_aspects(
                            environment=settings.app_env,
                            marketplace_id=marketplace_for_aspect_lookup,
                            category_id=current_category_state,
                            aspects=category_aspect_rows,
                            actor=user.username,
                        )
                        st.session_state["ebay_pub_category_aspect_rows"] = category_aspect_rows
                        st.session_state["ebay_pub_category_aspect_signature"] = aspect_cache_signature
                        required_count = sum(1 for row in category_aspect_rows if bool((row or {}).get("required")))
                        st.success(
                            f"Loaded {len(category_aspect_rows)} category item specific(s) from eBay; "
                            f"{required_count} required."
                        )
                    except Exception as exc:
                        st.error(f"Required item specifics fetch failed: {exc}")
    with car2:
        if st.button("Refresh Required Item Specifics", key="ebay_pub_refresh_required_aspects_btn"):
            token_for_aspect_lookup = str(st.session_state.get("ebay_pub_access_token") or "").strip() or str(
                default_token or ""
            ).strip()
            marketplace_for_aspect_lookup = str(
                st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id or "EBAY_US"
            ).strip()
            if not current_category_state:
                st.warning("Select or enter an eBay category ID first.")
            elif not token_for_aspect_lookup:
                st.warning("Missing eBay user access token.")
            else:
                try:
                    raw_aspects = ebay.get_item_aspects_for_category(
                        access_token=token_for_aspect_lookup,
                        category_id=current_category_state,
                        marketplace_id=marketplace_for_aspect_lookup,
                    )
                    category_aspect_rows = normalize_ebay_category_aspect_rows(raw_aspects)
                    repo.cache_ebay_category_aspects(
                        environment=settings.app_env,
                        marketplace_id=marketplace_for_aspect_lookup,
                        category_id=current_category_state,
                        aspects=category_aspect_rows,
                        actor=user.username,
                    )
                    st.session_state["ebay_pub_category_aspect_rows"] = category_aspect_rows
                    st.session_state["ebay_pub_category_aspect_signature"] = aspect_cache_signature
                    required_count = sum(1 for row in category_aspect_rows if bool((row or {}).get("required")))
                    st.success(
                        f"Refreshed {len(category_aspect_rows)} category item specific(s) from eBay; "
                        f"{required_count} required."
                    )
                except Exception as exc:
                    st.error(f"Required item specifics fetch failed: {exc}")
    with car3:
        if category_aspect_rows:
            required_count = sum(1 for row in category_aspect_rows if bool((row or {}).get("required")))
            st.caption(
                f"eBay category aspects loaded for `{current_category_state or '(no category)'}`: "
                f"{required_count} required of {len(category_aspect_rows)} total."
            )

    aspects_payload_cache: dict[str, list[str]] | None = None

    def _get_aspects_payload() -> dict[str, list[str]]:
        nonlocal aspects_payload_cache
        if aspects_payload_cache is None:
            aspects_payload_cache = _normalize_aspects_payload(st.session_state.get("ebay_pub_aspects_json") or "")
        return aspects_payload_cache

    st.markdown("#### Item Specifics Builder")
    load_item_specifics_editor = st.checkbox(
        "Load Item Specifics Editor (slower)",
        value=False,
        key=f"listings_load_item_specifics_editor_{int(selected_listing.id)}",
        help="Defers parsed item-specific tables and add/remove controls unless explicitly requested.",
    )
    if not load_item_specifics_editor:
        st.caption(
            "Item specifics editor controls are deferred. Enable `Load Item Specifics Editor (slower)` "
            "to use parsed preview and add/remove controls."
        )
    else:
        aspects_preview = _get_aspects_payload()
        load_default_aspects_preview = st.checkbox(
            "Load Suggested Default Aspects (slower)",
            value=False,
            key=f"listings_load_default_aspects_preview_{int(selected_listing.id)}",
            help="Defers default-aspect merge/preview rendering unless explicitly requested.",
        )
        if not load_default_aspects_preview:
            st.caption(
                "Suggested defaults are deferred. Enable `Load Suggested Default Aspects (slower)` "
                "to preview/apply bullion/coin default aspects."
            )
        else:
            defaults_preview_payload, _defaults_injected_keys = merge_ebay_aspects_defaults(
                category=str(product.category or "").strip(),
                metal_type=str(product.metal_type or "").strip(),
                title=str(selected_listing.listing_title or product.title or "").strip(),
                weight_oz=product.weight_oz,
                existing_aspects=aspects_preview,
            )
            default_only_payload = {
                key: values
                for key, values in defaults_preview_payload.items()
                if key not in aspects_preview
            }
            if default_only_payload:
                st.caption("Suggested bullion/coin defaults:")
                default_only_items = list(default_only_payload.items())
                if len(default_only_items) > 1:
                    default_only_items = sorted(default_only_items, key=lambda kv: kv[0].lower())
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"aspect": k, "values": ", ".join(v)}
                            for k, v in default_only_items
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
                default_only_keys = [str(k) for k, _ in default_only_items]
                if st.button("Apply Suggested Default Aspects", key="ebay_pub_apply_default_aspects_btn"):
                    _queue_ebay_publish_updates_preserving_form(
                        {"ebay_pub_aspects_json": json.dumps(defaults_preview_payload, indent=2)},
                        flash=(
                            "Applied default bullion/coin item specifics: "
                            + ", ".join(default_only_keys)
                        ),
                    )
                    st.rerun()

        aspects_preview_items: list[tuple[str, list[str]]] = []
        if aspects_preview:
            aspects_preview_items = list(aspects_preview.items())
            if len(aspects_preview_items) > 1:
                aspects_preview_items = sorted(aspects_preview_items, key=lambda kv: kv[0].lower())
            st.dataframe(
                pd.DataFrame(
                    [
                        {"aspect": k, "values": ", ".join(v)}
                        for k, v in aspects_preview_items
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No parsed item specifics yet.")
        if category_aspect_rows:
            table_rows = _category_aspect_table_rows(category_aspect_rows, aspects_preview)
            required_table_rows = [row for row in table_rows if row.get("required") == "yes"]
            missing_required_rows = missing_required_ebay_aspects(category_aspect_rows, aspects_preview)
            if required_table_rows:
                st.caption("Required item specifics from selected eBay category:")
                st.dataframe(
                    pd.DataFrame(required_table_rows),
                    use_container_width=True,
                    hide_index=True,
                )
            if missing_required_rows:
                st.warning(
                    "Missing required item specifics: "
                    + ", ".join(str(row.get("name") or "") for row in missing_required_rows)
                )
                missing_labels = [str(row.get("name") or "").strip() for row in missing_required_rows]
                selected_missing = st.selectbox(
                    "Required Specific",
                    options=missing_labels,
                    key="ebay_pub_required_aspect_select",
                )
                selected_missing_row = next(
                    (
                        row
                        for row in missing_required_rows
                        if str(row.get("name") or "").strip() == selected_missing
                    ),
                    {},
                )
                allowed_values = [
                    str(value or "").strip()
                    for value in (selected_missing_row.get("values") or [])
                    if str(value or "").strip()
                ]
                if allowed_values:
                    required_value = st.selectbox(
                        "Required Specific Value",
                        options=[""] + allowed_values,
                        key=_category_aspect_input_key("ebay_pub_required_aspect_value", selected_missing),
                    )
                else:
                    required_value = st.text_input(
                        "Required Specific Value",
                        key=_category_aspect_input_key("ebay_pub_required_aspect_value", selected_missing),
                    )
                if st.button("Apply Required Specific", key="ebay_pub_apply_required_aspect_btn"):
                    if not selected_missing:
                        st.warning("Select a required item specific first.")
                    elif not str(required_value or "").strip():
                        st.warning("Enter a value before applying.")
                    else:
                        next_payload = dict(aspects_preview)
                        next_payload[selected_missing] = [str(required_value).strip()]
                        _queue_ebay_publish_updates_preserving_form(
                            {"ebay_pub_aspects_json": json.dumps(next_payload, indent=2)},
                            flash=f"Applied required item specific `{selected_missing}`.",
                        )
                        st.rerun()
            elif required_table_rows:
                st.success("All loaded required item specifics are filled.")
        isb1, isb2 = st.columns(2)
        with isb1:
            ebay_pub_aspect_name = st.text_input(
                "Aspect Name",
                key="ebay_pub_aspect_name_input",
                placeholder="e.g. Brand",
            ).strip()
        with isb2:
            ebay_pub_aspect_values = st.text_input(
                "Aspect Values (comma-separated)",
                key="ebay_pub_aspect_values_input",
                placeholder="e.g. US Mint",
            ).strip()
        isb3, isb4, isb5 = st.columns(3)
        with isb3:
            if st.button("Add/Update Aspect", key="ebay_pub_aspect_add_btn"):
                if not ebay_pub_aspect_name:
                    st.warning("Enter an aspect name first.")
                else:
                    values = [v.strip() for v in ebay_pub_aspect_values.split(",") if v.strip()]
                    if not values:
                        st.warning("Enter at least one aspect value.")
                    else:
                        next_payload = dict(aspects_preview)
                        next_payload[ebay_pub_aspect_name] = values
                        _queue_ebay_publish_updates_preserving_form(
                            {"ebay_pub_aspects_json": json.dumps(next_payload, indent=2)},
                            flash=f"Aspect `{ebay_pub_aspect_name}` updated.",
                        )
                        st.rerun()
        with isb4:
            aspect_keys = [str(k) for k, _ in aspects_preview_items]
            remove_options = ["(select)"] + aspect_keys
            remove_name = st.selectbox(
                "Remove Aspect",
                options=remove_options,
                key="ebay_pub_aspect_remove_select",
            )
            if st.button("Remove Selected", key="ebay_pub_aspect_remove_btn"):
                if remove_name == "(select)":
                    st.info("Select an aspect to remove.")
                else:
                    next_payload = dict(aspects_preview)
                    next_payload.pop(remove_name, None)
                    _queue_ebay_publish_updates_preserving_form(
                        {"ebay_pub_aspects_json": json.dumps(next_payload, indent=2) if next_payload else ""},
                        flash=f"Removed aspect `{remove_name}`.",
                    )
                    st.rerun()
        with isb5:
            if st.button("Clear All Aspects", key="ebay_pub_aspect_clear_btn"):
                _queue_ebay_publish_updates_preserving_form(
                    {"ebay_pub_aspects_json": ""},
                    flash="Cleared item specifics.",
                )
                st.rerun()
    st.text_area(
        "Item Specifics JSON (Editable)",
        key="ebay_pub_aspects_json",
        height=150,
        help='Review/edit parsed specifics before revise/publish. Example: {"Certification":["Uncertified"]}',
    )

    with st.container():
        listing_title = st.text_input("Listing Title", key="ebay_pub_title")
        p1, p2, p3 = st.columns(3)
        with p1:
            publish_format = st.selectbox("Format", publish_formats, key="ebay_pub_format")
        with p2:
            available_quantity = st.number_input(
                "Available Quantity",
                min_value=1,
                step=1,
                key="ebay_pub_qty",
            )
        with p3:
            condition = st.selectbox(
                "Condition",
                condition_options,
                key="ebay_pub_condition",
                format_func=lambda value: condition_option_labels.get(str(value), str(value)),
            )

        d1, d2 = st.columns(2)
        with d1:
            category_id = st.text_input("eBay Category ID", key="ebay_pub_category_id")
            if str(category_id or "").strip():
                st.session_state["ebay_pub_last_category_id"] = str(category_id or "").strip()
                st.session_state["ebay_pub_last_category_listing_id"] = current_listing_id
        with d2:
            listing_duration = (
                "GTC"
                if publish_format == "FIXED_PRICE"
                else st.selectbox("Auction Duration", auction_durations, key="ebay_pub_auction_duration")
            )
        publish_store_category_marketplace_id = str(
            st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id or "EBAY_US"
        ).strip()
        _render_ebay_store_category_manager(
            repo,
            marketplace_id=publish_store_category_marketplace_id,
            actor=user.username,
            key_prefix=f"ebay_pub_{int(selected_listing.id)}",
        )
        publish_store_category_options = [
            str(getattr(row, "category_path", "") or "").strip()
            for row in _store_category_option_rows(
                repo,
                marketplace_id=publish_store_category_marketplace_id,
            )
            if str(getattr(row, "category_path", "") or "").strip()
        ]
        publish_store_category_default = [
            path
            for path in _normalize_store_category_names(st.session_state.get("ebay_pub_store_category_names"))
            if path in publish_store_category_options
        ]
        if _normalize_store_category_names(st.session_state.get("ebay_pub_store_category_names")) != publish_store_category_default:
            _safe_session_set("ebay_pub_store_category_names", publish_store_category_default)
        st.multiselect(
            "eBay Store Categories (optional)",
            options=publish_store_category_options,
            default=publish_store_category_default,
            max_selections=2,
            key="ebay_pub_store_category_names",
            help="Optional eBay store category full paths; eBay allows up to two per offer.",
        )
        if not publish_store_category_options:
            st.caption("No saved eBay store categories yet. Add one above to use it on this listing.")
        if publish_format == "FIXED_PRICE":
            fixed_price = st.number_input(
                "Buy It Now Price",
                min_value=0.01,
                step=1.0,
                key="ebay_pub_fixed_price",
            )
            best_offer_enabled = st.checkbox(
                "Enable Best Offer",
                key="ebay_pub_best_offer_enabled",
                help="Applies to fixed-price listings only.",
            )
            bo1, bo2 = st.columns(2)
            with bo1:
                best_offer_auto_accept = st.number_input(
                    "Auto-Accept Offer >=",
                    min_value=0.0,
                    step=0.01,
                    key="ebay_pub_best_offer_auto_accept",
                    help="Optional. Applied only when Best Offer is enabled.",
                )
            with bo2:
                best_offer_minimum = st.number_input(
                    "Auto-Decline Offer <",
                    min_value=0.0,
                    step=0.01,
                    key="ebay_pub_best_offer_minimum",
                    help="Optional. Applied only when Best Offer is enabled.",
                )
            auction_start_price = 0.0
            auction_reserve_price = 0.0
            auction_buy_now_price = 0.0
        else:
            best_offer_enabled = False
            best_offer_auto_accept = 0.0
            best_offer_minimum = 0.0
            a1, a2, a3 = st.columns(3)
            with a1:
                auction_start_price = st.number_input(
                    "Auction Start Price",
                    min_value=0.01,
                    step=1.0,
                    key="ebay_pub_auction_start",
                )
            with a2:
                auction_reserve_price = st.number_input(
                    "Reserve Price (Optional)",
                    min_value=0.0,
                    step=1.0,
                    key="ebay_pub_auction_reserve",
                )
            with a3:
                auction_buy_now_price = st.number_input(
                    "Auction Buy It Now (Optional)",
                    min_value=0.0,
                    step=1.0,
                    key="ebay_pub_auction_buy_now",
                )
            fixed_price = 0.0

        estimated_buyer_shipping = float(st.session_state.get("ebay_pub_estimated_buyer_shipping") or 0.0)
        estimated_promoted_rate = float(st.session_state.get("ebay_pub_estimated_promoted_rate") or 0.0)
        estimated_local_shipping_cost_per_item = float(
            st.session_state.get("ebay_pub_estimated_local_shipping_cost_per_item") or 0.0
        )
        listing_fee_estimate_cache: dict | None = None

        def _get_listing_fee_estimate() -> dict:
            nonlocal listing_fee_estimate_cache
            if listing_fee_estimate_cache is None:
                fee_estimate_unit_price = (
                    float(fixed_price or 0.0)
                    if publish_format == "FIXED_PRICE"
                    else float(auction_start_price or 0.0)
                )
                listing_fee_estimate_cache = estimate_ebay_fees(
                    repo,
                    unit_price=fee_estimate_unit_price,
                    quantity=int(available_quantity or 1),
                    buyer_paid_shipping=float(estimated_buyer_shipping or 0.0),
                    promoted_rate_percent=float(estimated_promoted_rate or 0.0),
                )
            return listing_fee_estimate_cache

        load_fee_estimate_assist = st.checkbox(
            "Load eBay Fee Estimate Assist (slower)",
            value=False,
            key=f"listings_load_fee_estimate_assist_{int(selected_listing.id)}",
            help="Defers fee-estimate calculations and metric rendering unless explicitly requested.",
        )
        if not load_fee_estimate_assist:
            st.caption(
                "Fee estimate assist is deferred. Enable `Load eBay Fee Estimate Assist (slower)` "
                "to render estimated fee/payout metrics."
            )
        else:
            st.markdown("##### Estimated eBay Fees (Pricing Assist)")
            fee1, fee2 = st.columns(2)
            with fee1:
                estimated_buyer_shipping = float(
                    st.number_input(
                        "Estimated Buyer-Paid Shipping (USD)",
                        min_value=0.0,
                        step=0.01,
                        key="ebay_pub_estimated_buyer_shipping",
                        help="For pricing guidance only; does not replace shipping-policy settings.",
                    )
                )
            with fee2:
                estimated_promoted_rate = float(
                    st.number_input(
                        "Estimated Promoted Listing Rate (%)",
                        min_value=0.0,
                        max_value=100.0,
                        step=0.1,
                        key="ebay_pub_estimated_promoted_rate",
                        help="Optional ad-rate impact included in estimated fees.",
                    )
                )
            estimated_local_shipping_cost_per_item = float(
                st.number_input(
                    "Estimated Local Fulfillment Cost per Item (USD)",
                    min_value=0.0,
                    step=0.01,
                    key="ebay_pub_estimated_local_shipping_cost_per_item",
                    help="Your estimated shipping/packing label spend per sold item for expected-net scoring.",
                )
            )
            listing_fee_estimate = _get_listing_fee_estimate()
            fm1, fm2, fm3, fm4 = st.columns(4)
            with fm1:
                st.metric("Est Gross", f"${float(listing_fee_estimate.get('gross_total') or 0.0):,.2f}")
            with fm2:
                st.metric("Est Fees", f"${float(listing_fee_estimate.get('estimated_total_fees') or 0.0):,.2f}")
            with fm3:
                st.metric(
                    "Est Net Payout",
                    f"${float(listing_fee_estimate.get('estimated_net_payout_before_shipping_cost') or 0.0):,.2f}",
                )
            with fm4:
                st.metric(
                    "Fee %",
                    f"{float(listing_fee_estimate.get('estimated_fee_percent_of_gross') or 0.0):.2f}%",
                )
            st.caption(
                "Estimate assumptions: "
                f"final value {float(listing_fee_estimate.get('final_value_rate_percent') or 0.0):.2f}% + "
                f"${float(listing_fee_estimate.get('final_value_fixed_usd') or 0.0):,.2f}, "
                f"payment {float(listing_fee_estimate.get('payment_rate_percent') or 0.0):.2f}% + "
                f"${float(listing_fee_estimate.get('payment_fixed_usd') or 0.0):,.2f}, "
                f"promoted {float(listing_fee_estimate.get('promoted_rate_percent') or 0.0):.2f}%."
            )
            known_unit_cost = _known_unit_cost(getattr(selected_listing, "product", None))
            expected_net = _expected_net_score(
                fee_estimate=listing_fee_estimate,
                quantity=int(available_quantity or 1),
                known_unit_cost=float(known_unit_cost or 0.0),
                estimated_local_shipping_cost_per_item=float(estimated_local_shipping_cost_per_item or 0.0),
            )
            st.markdown("##### Expected Net Score (Pre-Publish)")
            en1, en2, en3, en4, en5 = st.columns(5)
            with en1:
                st.metric("Known Unit Cost", f"${float(known_unit_cost or 0.0):,.2f}")
            with en2:
                st.metric("Est COGS Total", f"${float(expected_net.get('known_cogs_total') or 0.0):,.2f}")
            with en3:
                st.metric("Expected Net", f"${float(expected_net.get('expected_net') or 0.0):,.2f}")
            with en4:
                st.metric("Expected Margin %", f"{float(expected_net.get('expected_margin_pct_of_gross') or 0.0):.2f}%")
            with en5:
                st.metric("Breakeven Listing", f"${float(expected_net.get('breakeven_listing_price') or 0.0):,.2f}")
            st.caption(
                "Expected-net score: "
                f"`{str(expected_net.get('score') or '').upper()}` | "
                "formula = est net payout - local fulfillment cost - known COGS. "
                f"Price cushion vs breakeven: ${float(expected_net.get('price_cushion') or 0.0):,.2f}."
            )

        load_volume_pricing_builder = st.checkbox(
            "Load Volume Pricing Builder (slower)",
            value=False,
            key=f"listings_load_volume_pricing_builder_{int(selected_listing.id)}",
            help="Defers volume-pricing builder controls unless explicitly requested.",
        )
        if not load_volume_pricing_builder:
            st.caption(
                "Volume pricing builder is deferred. Enable `Load Volume Pricing Builder (slower)` "
                "to edit discount tiers and description-append settings."
            )
        else:
            st.markdown("##### Volume Pricing Tiers (Optional)")
            st.caption(
                "Store quantity discount tiers in draft metadata. These tiers are preserved in-app for eBay handoff/review."
            )
            vp1, vp2, vp3 = st.columns(3)
            with vp1:
                st.number_input(
                    "Buy 2 and save (%)",
                    min_value=0.0,
                    max_value=95.0,
                    step=1.0,
                    key="ebay_pub_volume_discount_buy2",
                )
            with vp2:
                st.number_input(
                    "Buy 3 and save (%)",
                    min_value=0.0,
                    max_value=95.0,
                    step=1.0,
                    key="ebay_pub_volume_discount_buy3",
                )
            with vp3:
                st.number_input(
                    "Buy 4+ and save (%)",
                    min_value=0.0,
                    max_value=95.0,
                    step=1.0,
                    key="ebay_pub_volume_discount_buy4",
                )
            vpb1, vpb2 = st.columns(2)
            with vpb1:
                if st.button("Apply Discount Builder", key="ebay_pub_apply_volume_discount_builder_btn"):
                    st.session_state["ebay_pub_volume_pricing_json"] = _volume_pricing_json_from_discount_controls(
                        buy2_percent=float(st.session_state.get("ebay_pub_volume_discount_buy2") or 0.0),
                        buy3_percent=float(st.session_state.get("ebay_pub_volume_discount_buy3") or 0.0),
                        buy4_percent=float(st.session_state.get("ebay_pub_volume_discount_buy4") or 0.0),
                    )
                    st.rerun()
            with vpb2:
                if st.button("Clear Volume Pricing", key="ebay_pub_clear_volume_pricing_btn"):
                    st.session_state["ebay_pub_volume_pricing_json"] = ""
                    st.rerun()
            st.text_area(
                "Volume Pricing JSON",
                key="ebay_pub_volume_pricing_json",
                height=110,
                placeholder='[{"min_qty": 2, "percent_off": 2}, {"min_qty": 3, "percent_off": 3}, {"min_qty": 5, "price": 4.60}]',
            )
            st.checkbox(
                "Append volume pricing block to listing description",
                key="ebay_pub_include_volume_pricing_in_description",
                help="Adds a buyer-visible quantity discount section to description text.",
            )

        listing_description = st.text_area(
            "Listing Description",
            height=160,
            key="ebay_pub_description",
        )
        with st.expander("Advanced Product Fields (Optional)", expanded=False):
            st.text_input(
                "Subtitle",
                key="ebay_pub_subtitle",
                help="Optional eBay subtitle-style field where supported.",
            )
            st.text_area(
                "Condition Description",
                key="ebay_pub_condition_description",
                height=80,
                help=f"Optional additional condition details. eBay limit: {EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters.",
            )
            condition_description_len = len(str(st.session_state.get("ebay_pub_condition_description") or ""))
            if condition_description_len > EBAY_MAX_CONDITION_DESCRIPTION_CHARS:
                st.error(
                    "Condition Description is too long for eBay: "
                    f"{condition_description_len}/{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters."
                )
            else:
                st.caption(
                    f"Condition Description: {condition_description_len}/{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters."
                )
            grading_prefill_msg = _ai_grading_prefill_status(
                current_value=st.session_state.get("ebay_pub_condition_description"),
                default_value=default_condition_description,
            )
            if grading_prefill_msg:
                st.caption(grading_prefill_msg)
            st.caption("Item specifics are edited in the Item Specifics Builder section above.")
        preview_sanitized_html = st.checkbox(
            "Preview sanitized HTML description",
            value=False,
            help="Shows the exact sanitized HTML that will be sent to eBay.",
            key="ebay_pub_preview_sanitized_html",
        )
        if preview_sanitized_html:
            sanitized_preview, sanitize_preview_notes = _sanitize_listing_html(listing_description)
            if sanitize_preview_notes:
                st.warning("Sanitization adjustments: " + "; ".join(sanitize_preview_notes))
            with st.expander("Sanitized HTML Preview", expanded=True):
                components.html(sanitized_preview or "<p></p>", height=220, scrolling=True)
                st.code(sanitized_preview or "", language="html")
        st.markdown("#### eBay Media Upload Options")
        e1, e2 = st.columns(2)
        with e1:
            use_eps_images = st.checkbox(
                "Upload selected images to eBay EPS first",
                key="ebay_pub_upload_to_eps",
                help="Recommended. eBay creates its own hosted image URLs from selected listing images.",
            )
        with e2:
            upload_video_to_ebay = st.checkbox(
                "Upload one MP4/MOV video to eBay and attach",
                key="ebay_pub_upload_video_to_ebay",
                help="eBay supports one video per listing. MOV/QuickTime files are converted to MP4 before upload.",
            )
        if image_options:
            selected_image_labels = st.multiselect(
                "Images for eBay listing",
                options=list(image_options.keys()),
                key="ebay_pub_selected_images",
            )
            primary_image_options = ["Auto", *selected_image_labels]
            current_primary_label = str(st.session_state.get("ebay_pub_primary_image_label") or "Auto")
            if current_primary_label not in primary_image_options:
                current_primary_label = "Auto"
                _safe_session_set("ebay_pub_primary_image_label", "Auto")
            primary_image_label = st.selectbox(
                "Main eBay Image",
                options=primary_image_options,
                index=primary_image_options.index(current_primary_label),
                key="ebay_pub_primary_image_label",
            )
        else:
            selected_image_labels = []
            primary_image_label = "Auto"
            st.info("No listing images available for eBay publish.")
        if video_options:
            selected_video_label = st.selectbox(
                "Video for eBay listing (optional)",
                options=["None"] + list(video_options.keys()),
                key="ebay_pub_selected_video",
            )
        else:
            selected_video_label = "None"
            st.info("No listing videos available.")
        media_option_video_warning = _selected_ebay_video_warning(upload_video_to_ebay, selected_video_label)
        if media_option_video_warning:
            st.warning(media_option_video_warning)

        selected_images_cache: list[object] | None = None
        selected_video_cache: object | None = None
        selected_video_resolved_cache = False

        def _resolve_selected_images() -> list[object]:
            nonlocal selected_images_cache
            if selected_images_cache is None:
                rows = [
                    image_options[label] for label in selected_image_labels if label in image_options
                ]
                if primary_image_label != "Auto" and primary_image_label in selected_image_labels:
                    primary_media = image_options.get(primary_image_label)
                    primary_id = int(getattr(primary_media, "id", 0) or 0) if primary_media is not None else 0
                    if primary_id > 0:
                        rows = sorted(
                            rows,
                            key=lambda row: 0 if int(getattr(row, "id", 0) or 0) == primary_id else 1,
                        )
                selected_images_cache = rows
            return list(selected_images_cache)

        def _resolve_selected_video() -> object | None:
            nonlocal selected_video_cache, selected_video_resolved_cache
            if not selected_video_resolved_cache:
                selected_video_resolved_cache = True
                selected_video_cache = video_options.get(selected_video_label)
            return selected_video_cache

        s1, s2, s3 = st.columns(3)
        with s1:
            merchant_location_key = st.text_input(
                "Merchant Location Key",
                key="ebay_pub_merchant_location_key",
            )
        with s2:
            payment_policy_id = st.text_input("Payment Policy ID", key="ebay_pub_payment_policy_id")
        with s3:
            fulfillment_policy_id = st.text_input(
                "Fulfillment Policy ID",
                key="ebay_pub_fulfillment_policy_id",
            )
        return_policy_id = st.text_input("Return Policy ID", key="ebay_pub_return_policy_id")
        st.markdown("#### Package Data (Shipping)")
        pkg1, pkg2, pkg3, pkg4 = st.columns(4)
        with pkg1:
            package_weight_oz = st.number_input(
                "Weight (oz)",
                min_value=0.0,
                step=0.1,
                key="ebay_pub_package_weight_oz",
            )
        with pkg2:
            package_length_in = st.number_input(
                "Length (in)",
                min_value=0.0,
                step=0.1,
                key="ebay_pub_package_length_in",
            )
        with pkg3:
            package_width_in = st.number_input(
                "Width (in)",
                min_value=0.0,
                step=0.1,
                key="ebay_pub_package_width_in",
            )
        with pkg4:
            package_height_in = st.number_input(
                "Height (in)",
                min_value=0.0,
                step=0.1,
                key="ebay_pub_package_height_in",
            )
        sp1, sp2, sp3 = st.columns(3)
        with sp1:
            shipping_service = st.text_input(
                "Shipping Service (metadata)",
                key="ebay_pub_shipping_service",
                help="Tracked in local metadata; eBay policy IDs still govern final service rules.",
            )
        with sp2:
            handling_days = st.number_input(
                "Handling Days (metadata)",
                min_value=0,
                step=1,
                key="ebay_pub_handling_days",
            )
        with sp3:
            shipping_cost = st.number_input(
                "Shipping Cost USD (metadata)",
                min_value=0.0,
                step=0.01,
                key="ebay_pub_shipping_cost",
            )
        c1, c2, c3 = st.columns(3)
        with c1:
            st.text_input("Marketplace ID", key="ebay_pub_marketplace_id")
        with c2:
            st.text_input("Currency", key="ebay_pub_currency")
        with c3:
            st.text_input("Content Language", key="ebay_pub_content_language")
        post_mode = st.selectbox(
            "eBay Post Mode",
            ["Publish Live Listing", "Save Unpublished Offer (API Draft)"],
            index=0,
            key="ebay_pub_post_mode",
            help=(
                "Save Unpublished Offer creates/updates an Inventory API offer and stores offer_id. "
                "It does NOT create a Seller Hub compose-draft UI row."
            ),
        )
        st.caption(
            "Note: eBay API offers are unpublished offer objects. They are not the same as Seller Hub "
            "compose drafts, but you can QA via `Get Offer Details` and publish later."
        )
        access_token = st.text_area(
            "User Access Token",
            height=120,
            help="Defaults to `EBAY_USER_ACCESS_TOKEN` if set.",
            key="ebay_pub_access_token",
        )

        submit_label = (
            "Save Unpublished eBay Offer"
            if post_mode == "Save Unpublished Offer (API Draft)"
            else "Publish To eBay"
        )
        ps1, ps2 = st.columns(2)
        with ps1:
            preflight_submit = st.button(
                "Run eBay Dependency Preflight",
                key="ebay_pub_run_preflight_btn",
                disabled=sandbox_seller_ops_blocked,
            )
        with ps2:
            submit_publish = st.button(
                submit_label,
                key="ebay_pub_submit_btn",
                disabled=sandbox_seller_ops_blocked,
            )

    discovered_offer_id = str(publish_meta.get("offer_id") or "").strip()
    condition_policy_blocker = category_condition_rows and not _is_condition_valid_for_loaded_policy(
        category_condition_rows,
        condition,
    )
    if condition_policy_blocker:
        st.error(
            f"Selected condition `{condition}` is not valid for eBay category `{current_category_state}`. "
            "Load/refresh Category Conditions and choose one of the returned options before publishing."
        )
    if condition_policy_blocker and (preflight_submit or submit_publish):
        return

    effective_listing_description_source_base = str(listing_description or "").strip()
    if bool(st.session_state.pop("ebay_pub_save_draft_requested", False)):
        payload = _listings_build_ebay_publish_draft_payload(
            listing_id=int(selected_listing.id),
            listing_signature=listing_signature,
            state_keys=ebay_publish_draft_state_keys,
        )
        row = repo.save_workflow_draft(
            environment=settings.app_env,
            workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
            username=user.username,
            scope_key=draft_scope_key,
            draft_payload=payload,
            status="active",
            last_step="ebay_publish",
            actor=user.username,
        )
        repo.append_workflow_event(
            environment=settings.app_env,
            workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
            username=user.username,
            scope_key=draft_scope_key,
            action="save_draft",
            status="ok",
            message="Operator manually saved Listings publish draft.",
            payload={"draft_id": int(row.id), "listing_id": int(selected_listing.id)},
            draft_id=int(row.id),
            actor=user.username,
        )
        st.session_state["ebay_pub_last_autosave_signature"] = _listings_ebay_publish_signature(payload)
        st.session_state["ebay_pub_last_autosave_scope"] = draft_scope_key
        st.session_state["ebay_pub_last_autosave_at"] = utcnow_naive().isoformat()
        st.session_state["ebay_pub_draft_flash"] = "Saved publish draft."
        st.rerun()

    effective_category_id = str(
        category_id
        or st.session_state.get("ebay_pub_category_id")
        or ""
    ).strip()
    effective_store_category_names = _normalize_store_category_names(
        st.session_state.get("ebay_pub_store_category_names")
    )
    subtitle = str(st.session_state.get("ebay_pub_subtitle") or "").strip()
    condition_description = str(st.session_state.get("ebay_pub_condition_description") or "").strip()
    condition_description_len = len(condition_description)
    if condition_description_len > EBAY_MAX_CONDITION_DESCRIPTION_CHARS:
        st.error(
            "eBay condition description must be "
            f"{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters or fewer "
            f"(currently {condition_description_len})."
        )
        if preflight_submit or submit_publish:
            return
    volume_pricing_tiers_cache: list[dict] | None = None
    volume_pricing_errors_cache: list[str] | None = None

    def _get_volume_pricing_tiers_and_errors() -> tuple[list[dict], list[str]]:
        nonlocal volume_pricing_tiers_cache, volume_pricing_errors_cache
        if volume_pricing_tiers_cache is None or volume_pricing_errors_cache is None:
            volume_pricing_tiers_cache, volume_pricing_errors_cache = _normalize_volume_pricing_tiers(
                st.session_state.get("ebay_pub_volume_pricing_json") or "",
                base_price=float(fixed_price or 0.0)
                if str(publish_format or "").strip().upper() == "FIXED_PRICE"
                else 0.0,
            )
        return volume_pricing_tiers_cache, volume_pricing_errors_cache

    include_volume_desc = bool(st.session_state.get("ebay_pub_include_volume_pricing_in_description"))
    effective_listing_description_source = str(effective_listing_description_source_base or "").strip()
    if include_volume_desc:
        volume_pricing_tiers_for_desc, _volume_pricing_errors_for_desc = _get_volume_pricing_tiers_and_errors()
        if volume_pricing_tiers_for_desc:
            volume_block = _volume_pricing_description_block(volume_pricing_tiers_for_desc)
            if volume_block and volume_block not in effective_listing_description_source:
                effective_listing_description_source = (
                    f"{effective_listing_description_source}\n\n{volume_block}"
                    if effective_listing_description_source
                    else volume_block
                )
    effective_aspects_payload_cache: dict | None = None
    injected_aspect_keys_cache: list[str] | None = None

    def _resolve_effective_aspects_payload() -> tuple[dict, list[str]]:
        nonlocal effective_aspects_payload_cache, injected_aspect_keys_cache
        if effective_aspects_payload_cache is None or injected_aspect_keys_cache is None:
            effective_aspects_payload_cache, injected_aspect_keys_cache = merge_ebay_aspects_defaults(
                category=str(product.category or "").strip(),
                metal_type=str(product.metal_type or "").strip(),
                title=str(listing_title or product.title or "").strip(),
                weight_oz=product.weight_oz,
                existing_aspects=_get_aspects_payload(),
            )
        return effective_aspects_payload_cache, injected_aspect_keys_cache

    local_category_aspects_cache: dict[tuple[str, str], list[dict]] = {}

    def _local_required_specific_blockers(*, category_id_value: str, marketplace_id_value: str) -> list[str]:
        category_id_clean = str(category_id_value or "").strip()
        marketplace_id_clean = str(marketplace_id_value or default_marketplace_id or "EBAY_US").strip()
        if not category_id_clean:
            return []
        cache_key = (marketplace_id_clean.upper(), category_id_clean)
        if cache_key not in local_category_aspects_cache:
            cached = repo.get_cached_ebay_category_aspects(
                environment=settings.app_env,
                marketplace_id=marketplace_id_clean,
                category_id=category_id_clean,
            )
            local_category_aspects_cache[cache_key] = (
                normalize_ebay_category_aspect_rows((cached or {}).get("aspects") or [])
                if cached
                else []
            )
        rows = list(local_category_aspects_cache.get(cache_key) or [])
        if not rows:
            return []
        effective_aspects, _injected = _resolve_effective_aspects_payload()
        return [
            f"Missing required eBay item specific: {str((row or {}).get('name') or '').strip()}"
            for row in missing_required_ebay_aspects(rows, effective_aspects)
            if str((row or {}).get("name") or "").strip()
        ]

    load_volume_pricing_preview = st.checkbox(
        "Load Volume Pricing Preview (slower)",
        value=False,
        key=f"listings_load_volume_pricing_preview_{int(selected_listing.id)}",
        help="Defers volume-pricing JSON parse and tier preview unless explicitly requested.",
    )
    if load_volume_pricing_preview:
        volume_pricing_tiers_preview, _volume_pricing_errors_preview = _get_volume_pricing_tiers_and_errors()
        if volume_pricing_tiers_preview:
            tier_preview = " | ".join(
                [f"qty>={int(t['min_qty'])}: ${float(t['price']):,.2f}" for t in volume_pricing_tiers_preview]
            )
            st.caption(f"Volume pricing tiers parsed: {tier_preview}")
    else:
        st.caption(
            "Volume pricing preview is deferred. Enable `Load Volume Pricing Preview (slower)` "
            "to parse and preview tier details."
        )
    effective_listing_description_cache: str | None = None
    sanitize_notes_cache: list[str] | None = None

    def _get_effective_listing_description_and_notes() -> tuple[str, list[str]]:
        nonlocal effective_listing_description_cache, sanitize_notes_cache
        if effective_listing_description_cache is None or sanitize_notes_cache is None:
            effective_listing_description_cache, sanitize_notes_cache = _sanitize_listing_html(
                effective_listing_description_source
            )
        return effective_listing_description_cache, sanitize_notes_cache

    listing_html_errors_cache: list[str] | None = None

    def _get_listing_html_errors() -> list[str]:
        nonlocal listing_html_errors_cache
        if listing_html_errors_cache is None:
            effective_listing_description, _sanitize_notes = _get_effective_listing_description_and_notes()
            listing_html_errors_cache = _validate_listing_html(effective_listing_description)
        return listing_html_errors_cache

    def _preflight_signature(
        *,
        token: str,
        marketplace_id: str,
        category_id: str,
        condition: str,
        merchant_location_key: str,
        payment_policy_id_value: str,
        fulfillment_policy_id_value: str,
        return_policy_id_value: str,
        format_type_value: str,
        auction_buy_now_price_value: float,
    ) -> str:
        token_clean = str(token or "").strip()
        token_fingerprint = f"{len(token_clean)}:{token_clean[-16:]}" if token_clean else ""
        return json.dumps(
            {
                "token_fingerprint": token_fingerprint,
                "marketplace_id": str(marketplace_id or "").strip(),
                "category_id": str(category_id or "").strip(),
                "condition": str(condition or "").strip().upper(),
                "merchant_location_key": str(merchant_location_key or "").strip(),
                "payment_policy_id": str(payment_policy_id_value or "").strip(),
                "fulfillment_policy_id": str(fulfillment_policy_id_value or "").strip(),
                "return_policy_id": str(return_policy_id_value or "").strip(),
                "format_type": str(format_type_value or "").strip().upper(),
                "auction_buy_now_price": round(float(auction_buy_now_price_value or 0.0), 2),
            },
            sort_keys=True,
        )

    def _run_or_reuse_preflight(
        *,
        token: str,
        marketplace_id: str,
        category_id: str,
        condition_value: str,
        merchant_location_key: str,
        payment_policy_id_value: str,
        fulfillment_policy_id_value: str,
        return_policy_id_value: str,
        format_type_value: str,
        auction_buy_now_price_value: float,
    ) -> tuple[dict, bool, str]:
        signature = _preflight_signature(
            token=token,
            marketplace_id=marketplace_id,
            category_id=category_id,
            condition=condition_value,
            merchant_location_key=merchant_location_key,
            payment_policy_id_value=payment_policy_id_value,
            fulfillment_policy_id_value=fulfillment_policy_id_value,
            return_policy_id_value=return_policy_id_value,
            format_type_value=format_type_value,
            auction_buy_now_price_value=auction_buy_now_price_value,
        )
        cached_signature = str(st.session_state.get("ebay_pub_dependency_preflight_signature") or "").strip()
        cached_payload = st.session_state.get("ebay_pub_dependency_preflight_result")
        if cached_signature == signature and isinstance(cached_payload, dict):
            return dict(cached_payload), True, signature
        result_payload = ebay.verify_publish_dependencies(
            access_token=token,
            marketplace_id=str(marketplace_id or "").strip(),
            category_id=str(category_id or "").strip(),
            merchant_location_key=str(merchant_location_key or "").strip(),
            payment_policy_id=str(payment_policy_id_value or "").strip(),
            fulfillment_policy_id=str(fulfillment_policy_id_value or "").strip(),
            return_policy_id=str(return_policy_id_value or "").strip(),
            format_type=str(format_type_value or "").strip().upper(),
            auction_buy_now_price=float(auction_buy_now_price_value or 0.0),
            condition=str(condition_value or "").strip().upper(),
        )
        result_payload["checked_at"] = utcnow_naive().isoformat()
        st.session_state["ebay_pub_dependency_preflight_result"] = result_payload
        st.session_state["ebay_pub_dependency_preflight_signature"] = signature
        return result_payload, False, signature

    resolved_merchant_location_key_cache: dict[tuple[str, str], str] = {}

    def _resolve_merchant_location_key_cached(*, token_to_use: str, merchant_location_key_value: str) -> str:
        token_clean = str(token_to_use or "").strip()
        merchant_location_key_clean = str(merchant_location_key_value or "").strip()
        if not token_clean or not merchant_location_key_clean:
            return merchant_location_key_clean
        cache_key = (token_clean[-16:], merchant_location_key_clean)
        if cache_key in resolved_merchant_location_key_cache:
            return str(resolved_merchant_location_key_cache.get(cache_key) or "").strip()
        resolved = ebay.resolve_merchant_location_key(
            access_token=token_clean,
            merchant_location_key=merchant_location_key_clean,
        )
        resolved_clean = str(resolved or "").strip()
        resolved_merchant_location_key_cache[cache_key] = resolved_clean
        return resolved_clean

    if preflight_submit:
        token_to_use = (access_token or "").strip() or default_token
        effective_merchant_location_key = str(merchant_location_key or "").strip()
        if token_to_use and effective_merchant_location_key:
            effective_merchant_location_key = _resolve_merchant_location_key_cached(
                token_to_use=token_to_use,
                merchant_location_key_value=effective_merchant_location_key,
            )
        marketplace_id_for_preflight = str(
            st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id
        ).strip()
        preflight_signature = _preflight_signature(
            token=token_to_use,
            marketplace_id=marketplace_id_for_preflight,
            category_id=effective_category_id,
            condition=condition,
            merchant_location_key=effective_merchant_location_key,
            payment_policy_id_value=payment_policy_id.strip(),
            fulfillment_policy_id_value=fulfillment_policy_id.strip(),
            return_policy_id_value=return_policy_id.strip(),
            format_type_value=str(publish_format or "FIXED_PRICE").strip().upper(),
            auction_buy_now_price_value=float(auction_buy_now_price or 0.0),
        )
        result_payload: dict
        if not ebay.is_configured():
            result_payload = {
                "blockers": ["eBay app credentials are not configured."],
                "warnings": [],
                "checks": [],
            }
        elif not token_to_use:
            result_payload = {
                "blockers": ["User access token is required."],
                "warnings": [],
                "checks": [],
            }
        else:
            result_payload, _reused_preflight, preflight_signature = _run_or_reuse_preflight(
                token=token_to_use,
                marketplace_id=marketplace_id_for_preflight,
                category_id=effective_category_id,
                condition_value=condition,
                merchant_location_key=effective_merchant_location_key,
                payment_policy_id_value=payment_policy_id.strip(),
                fulfillment_policy_id_value=fulfillment_policy_id.strip(),
                return_policy_id_value=return_policy_id.strip(),
                format_type_value=str(publish_format or "FIXED_PRICE").strip().upper(),
                auction_buy_now_price_value=float(auction_buy_now_price or 0.0),
            )
            if _reused_preflight:
                st.caption("Using cached dependency preflight result (inputs unchanged).")
        if "checked_at" not in result_payload:
            result_payload["checked_at"] = utcnow_naive().isoformat()
        st.session_state["ebay_pub_dependency_preflight_result"] = result_payload
        st.session_state["ebay_pub_dependency_preflight_signature"] = preflight_signature
        if list(result_payload.get("blockers") or []):
            st.error("eBay dependency preflight found blockers.")
        elif list(result_payload.get("warnings") or []):
            st.warning("eBay dependency preflight completed with warnings.")
        else:
            st.success("eBay dependency preflight passed.")

    load_publish_diagnostics_cards = st.checkbox(
        "Load eBay Diagnostics Cards (slower)",
        value=False,
        key=f"listings_load_publish_diagnostics_cards_{int(selected_listing.id)}",
        help="Defers preflight card and last-publish diagnostics rendering unless explicitly requested.",
    )
    if not load_publish_diagnostics_cards:
        st.caption(
            "Publish diagnostics cards are deferred. Enable `Load eBay Diagnostics Cards (slower)` "
            "to render preflight and last-error diagnostics cards."
        )
    else:
        preflight_card_payload = st.session_state.get("ebay_pub_dependency_preflight_result")
        if isinstance(preflight_card_payload, dict):
            _render_ebay_preflight_card(preflight_card_payload)
        publish_meta_snapshot = publish_meta
        last_publish_error = str(publish_meta_snapshot.get("last_publish_error") or "").strip()
        if last_publish_error:
            last_publish_error_at = str(publish_meta_snapshot.get("last_publish_error_at") or "").strip()
            last_publish_error_stage = str(publish_meta_snapshot.get("last_publish_error_stage") or "").strip()
            context_payload = publish_meta_snapshot.get("last_publish_error_context")
            if not isinstance(context_payload, dict):
                context_payload = {}
            stamp = f" ({last_publish_error_at})" if last_publish_error_at else ""
            stage_text = f" | stage={last_publish_error_stage}" if last_publish_error_stage else ""
            st.warning(f"Last eBay publish error{stamp}{stage_text}: {last_publish_error}")
            if context_payload:
                with st.expander("Last eBay Publish Diagnostics", expanded=False):
                    st.json(context_payload)
        video_diag_keys = [
            "upload_video_to_ebay",
            "video_attached",
            "video_warning",
            "video_upload",
            "inventory_video_verification",
            "post_offer_video_verification",
            "post_publish_video_verification",
            "trading_listing_video_verification",
        ]
        video_diag_payload = {
            key: publish_meta_snapshot.get(key) for key in video_diag_keys if key in publish_meta_snapshot
        }
        if video_diag_payload:
            with st.expander("Last eBay Video Diagnostics", expanded=False):
                st.caption(
                    "Shows whether eBay Media upload completed and whether Inventory retained `product.videoIds` "
                    "after inventory upsert, offer create/update, and live publish."
                )
                st.json(video_diag_payload)

    st.markdown("#### Manage Existing eBay Listing")
    st.caption("Revise, end, or relist existing eBay-linked listings from this app.")
    load_manage_existing_controls = st.checkbox(
        "Load Manage Existing eBay Listing Controls (slower)",
        value=False,
        key=f"listings_load_manage_existing_controls_{int(selected_listing.id)}",
        help="Defers manage/revise/relist/end form controls and offer-inspector widgets unless explicitly requested.",
    )
    manage_action = "revise"
    manage_submit = False
    manage_offer_id = ""
    inspect_offer_submit = False
    if not load_manage_existing_controls:
        st.caption(
            "Manage listing controls are deferred. Enable "
            "`Load Manage Existing eBay Listing Controls (slower)` to run revise/end/relist actions "
            "or inspect offer details."
        )
    else:
        suggested_manage_offer_id = str(
            st.session_state.get("ebay_pub_manage_offer_id") or discovered_offer_id or ""
        ).strip()
        if "ebay_manage_offer_id_input" not in st.session_state:
            st.session_state["ebay_manage_offer_id_input"] = suggested_manage_offer_id
        with st.form("manage_ebay_listing_actions_form"):
            a1, a2 = st.columns(2)
            with a1:
                st.text_input(
                    "Offer ID",
                    key="ebay_manage_offer_id_input",
                    help="Autodetected from marketplace details when available.",
                )
            with a2:
                manage_action = st.selectbox("Action", ["revise", "end", "relist"])
            manage_submit = st.form_submit_button("Run eBay Listing Action", disabled=sandbox_seller_ops_blocked)
        manage_offer_id = str(st.session_state.get("ebay_manage_offer_id_input") or "").strip()

        inspect_offer_submit = st.button(
            "Get Offer Details",
            key="ebay_manage_offer_details_btn",
            disabled=sandbox_seller_ops_blocked,
        )

    offers_for_manage_cache: list[dict] | None = None
    resolved_manage_offer_id_cache: dict[tuple[str, str], str] = {}

    def _resolve_effective_offer_id_for_manage(token_to_use: str, raw_offer_id: str) -> str:
        nonlocal offers_for_manage_cache
        token_clean = str(token_to_use or "").strip()
        raw_offer_clean = str(raw_offer_id or "").strip()
        cache_key = (token_clean[-16:] if token_clean else "", raw_offer_clean)
        if cache_key in resolved_manage_offer_id_cache:
            return str(resolved_manage_offer_id_cache.get(cache_key) or "").strip()

        resolved_offer_id = raw_offer_clean
        if not resolved_offer_id and (selected_listing.external_listing_id or "").strip():
            try:
                if offers_for_manage_cache is None:
                    offers_payload = ebay.get_offers(
                        access_token=token_clean,
                        sku=_listing_ebay_inventory_sku(product, selected_listing) if product else "",
                    )
                    offers_for_manage_cache = list(offers_payload.get("offers") or [])
                for offer in offers_for_manage_cache:
                    offer_listing_id = str(offer.get("listingId") or "").strip()
                    if offer_listing_id and offer_listing_id == (selected_listing.external_listing_id or "").strip():
                        resolved_offer_id = str(offer.get("offerId") or "").strip()
                        break
            except Exception:
                pass
        resolved_manage_offer_id_cache[cache_key] = resolved_offer_id
        return resolved_offer_id

    if inspect_offer_submit:
        if not ensure_permission(user, "read", "Inspect eBay Offer"):
            st.stop()
        if not ebay.is_configured():
            st.error("eBay app credentials are not configured.")
            st.stop()
        token_to_use = (access_token or "").strip() or default_token
        if not token_to_use:
            st.error("User access token is required.")
            st.stop()
        effective_offer_id = _resolve_effective_offer_id_for_manage(token_to_use, str(manage_offer_id or "").strip())
        if not effective_offer_id:
            st.error("Offer ID is required (or resolvable via SKU/listing ID) to inspect offer details.")
            st.stop()
        try:
            offer_payload = ebay.get_offer(access_token=token_to_use, offer_id=effective_offer_id)
            st.session_state["ebay_manage_offer_details_payload"] = offer_payload
            st.session_state["ebay_manage_offer_details_offer_id"] = effective_offer_id
            st.success(f"Loaded eBay offer details for `{effective_offer_id}`.")
        except Exception as exc:
            st.error(f"Offer details lookup failed: {exc}")
            st.stop()

    offer_details_payload = st.session_state.get("ebay_manage_offer_details_payload")
    offer_details_offer_id = str(st.session_state.get("ebay_manage_offer_details_offer_id") or "").strip()
    if load_manage_existing_controls and isinstance(offer_details_payload, dict) and offer_details_payload:
        with st.expander("Offer Details (eBay API)", expanded=False):
            listing_id = str(offer_details_payload.get("listingId") or "").strip()
            offer_status = str(offer_details_payload.get("status") or "").strip()
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Offer ID", offer_details_offer_id or "unknown")
            with c2:
                st.metric("Offer Status", offer_status or "unknown")
            with c3:
                st.metric("Listing ID", listing_id or "none")
            if listing_id:
                st.link_button("Open eBay Listing URL", ebay.listing_url_for_id(listing_id))
            st.link_button("Open eBay Seller Hub Listings", ebay.seller_hub_listings_url())
            st.json(offer_details_payload)

    if manage_submit:
        if not ensure_permission(user, "update", "Manage eBay Listing"):
            st.stop()
        if not ebay.is_configured():
            st.error("eBay app credentials are not configured.")
            st.stop()
        token_to_use = (access_token or "").strip() or default_token
        if not token_to_use:
            st.error("User access token is required.")
            st.stop()

        effective_offer_id = _resolve_effective_offer_id_for_manage(
            token_to_use,
            str(manage_offer_id or "").strip(),
        )

        if not effective_offer_id:
            st.error("Offer ID is required (or resolvable via SKU/listing ID) to manage eBay listing.")
            st.stop()

        if manage_action == "end":
            try:
                ebay.withdraw_offer(access_token=token_to_use, offer_id=effective_offer_id)
                repo.update_listing(
                    selected_listing.id,
                    {"listing_status": "ended"},
                    actor=user.username,
                )
                st.success(f"Ended eBay listing via offer `{effective_offer_id}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"End listing failed: {exc}")
                st.stop()

        if manage_action == "relist":
            try:
                publish_result = ebay.publish_offer(
                    access_token=token_to_use,
                    offer_id=effective_offer_id,
                    inventory_sku=_listing_ebay_inventory_sku(product, selected_listing) if product else "",
                    content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                )
                listing_id = str(
                    publish_result.get("listingId") or selected_listing.external_listing_id or ""
                ).strip()
                updates = {"listing_status": "active"}
                if listing_id:
                    owner_listing_id = _external_listing_id_owner(
                        repo,
                        marketplace=str(selected_listing.marketplace or "ebay"),
                        external_listing_id=listing_id,
                        exclude_listing_id=int(selected_listing.id),
                        listings=listings,
                        owner_by_market_and_external_id=_get_external_listing_owner_map(),
                    )
                    if owner_listing_id is None:
                        updates["external_listing_id"] = listing_id
                        updates["marketplace_url"] = ebay.listing_url_for_id(listing_id)
                    else:
                        st.warning(
                            f"eBay returned listingId `{listing_id}` already linked to local listing #{owner_listing_id}; "
                            "kept current row active without changing external listing ID."
                        )
                repo.update_listing(selected_listing.id, updates, actor=user.username)
                st.success(f"Relisted eBay offer `{effective_offer_id}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Relist failed: {exc}")
                st.stop()

        if manage_action == "revise":
            listing_html_errors = _get_listing_html_errors()
            if listing_html_errors:
                st.error("Listing description failed validation: " + " | ".join(listing_html_errors))
                st.stop()
            effective_listing_description, sanitize_notes = _get_effective_listing_description_and_notes()
            if not str(effective_listing_description or "").strip():
                st.error("eBay listing description must be between 1 and 4000 characters.")
                st.stop()
            if len(str(effective_listing_description or "")) > EBAY_MAX_INVENTORY_DESCRIPTION_CHARS:
                st.error(
                    "eBay listing description must be "
                    f"{EBAY_MAX_INVENTORY_DESCRIPTION_CHARS} characters or fewer "
                    f"(currently {len(str(effective_listing_description or ''))})."
                )
                st.stop()
            if sanitize_notes:
                st.info(
                    "Listing description was sanitized before eBay operations: "
                    + "; ".join(sanitize_notes)
                )
            volume_pricing_tiers, volume_pricing_errors = _get_volume_pricing_tiers_and_errors()
            if publish_format == "FIXED_PRICE" and float(fixed_price or 0) <= 0:
                st.error("Buy It Now price must be greater than 0.")
                st.stop()
            if publish_format == "FIXED_PRICE" and bool(best_offer_enabled):
                if float(best_offer_auto_accept or 0.0) > 0 and float(best_offer_auto_accept or 0.0) > float(fixed_price or 0.0):
                    st.error("Auto-accept offer cannot be greater than Buy It Now price.")
                    st.stop()
                if float(best_offer_minimum or 0.0) > 0 and float(best_offer_minimum or 0.0) > float(fixed_price or 0.0):
                    st.error("Auto-decline offer threshold cannot be greater than Buy It Now price.")
                    st.stop()
                if float(best_offer_auto_accept or 0.0) > 0 and float(best_offer_minimum or 0.0) > 0:
                    if float(best_offer_minimum or 0.0) > float(best_offer_auto_accept or 0.0):
                        st.error("Auto-decline threshold cannot be greater than auto-accept threshold.")
                        st.stop()
            if volume_pricing_errors:
                st.error("Volume pricing is invalid: " + " | ".join(volume_pricing_errors[:5]))
                st.stop()
            if volume_pricing_tiers and publish_format != "FIXED_PRICE":
                st.error("Volume pricing tiers are only supported for fixed-price listings.")
                st.stop()
            if volume_pricing_tiers and publish_format == "FIXED_PRICE":
                invalid_tiers = [
                    t for t in volume_pricing_tiers if float(t.get("price") or 0.0) > float(fixed_price or 0.0)
                ]
                if invalid_tiers:
                    st.error("Volume tier prices cannot exceed Buy It Now price.")
                    st.stop()
            if publish_format == "AUCTION":
                if float(auction_start_price or 0) <= 0:
                    st.error("Auction start price must be greater than 0.")
                    st.stop()
                if float(auction_reserve_price or 0) > 0 and float(auction_reserve_price or 0) < float(auction_start_price or 0):
                    st.error("Auction reserve price cannot be lower than auction start price.")
                    st.stop()
                if not str(listing_duration or "").strip():
                    st.error("Auction duration is required.")
                    st.stop()
                if float(auction_buy_now_price or 0) > 0 and float(auction_buy_now_price or 0) < float(auction_start_price or 0):
                    st.error("Auction Buy It Now price cannot be lower than auction start price.")
                    st.stop()
            if not effective_category_id:
                st.error("Category ID is required for revise.")
                st.stop()
            effective_merchant_location_key = str(merchant_location_key or "").strip()
            if effective_merchant_location_key:
                effective_merchant_location_key = _resolve_merchant_location_key_cached(
                    token_to_use=token_to_use,
                    merchant_location_key_value=effective_merchant_location_key,
                )
            if not effective_merchant_location_key:
                st.error("Merchant Location Key is required for revise.")
                st.stop()
            if not payment_policy_id.strip() or not fulfillment_policy_id.strip() or not return_policy_id.strip():
                st.error("Payment, fulfillment, and return policy IDs are required for revise.")
                st.stop()
            marketplace_id_for_preflight = (
                st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id
            )
            local_specific_blockers = _local_required_specific_blockers(
                category_id_value=effective_category_id,
                marketplace_id_value=str(marketplace_id_for_preflight or "").strip(),
            )
            if local_specific_blockers:
                st.error("eBay required item specifics blockers: " + " | ".join(local_specific_blockers[:3]))
                st.stop()
            preflight_result, reused_preflight, _preflight_signature_value = _run_or_reuse_preflight(
                token=token_to_use,
                marketplace_id=str(marketplace_id_for_preflight or "").strip(),
                category_id=effective_category_id,
                condition_value=condition,
                merchant_location_key=effective_merchant_location_key,
                payment_policy_id_value=payment_policy_id.strip(),
                fulfillment_policy_id_value=fulfillment_policy_id.strip(),
                return_policy_id_value=return_policy_id.strip(),
                format_type_value=str(publish_format or "FIXED_PRICE").strip().upper(),
                auction_buy_now_price_value=float(auction_buy_now_price or 0.0),
            )
            if reused_preflight:
                st.caption("Using cached dependency preflight result (inputs unchanged).")
            preflight_blockers = list(preflight_result.get("blockers") or [])
            preflight_warnings = list(preflight_result.get("warnings") or [])
            if preflight_warnings:
                st.warning("eBay dependency preflight warnings: " + " | ".join(preflight_warnings[:3]))
            if preflight_blockers:
                st.error("eBay dependency preflight blockers: " + " | ".join(preflight_blockers[:3]))
                with st.expander("Preflight Diagnostics", expanded=False):
                    st.json(preflight_result)
                st.stop()

            selected_images = _resolve_selected_images()
            image_urls = []
            eps_uploads: list[dict] = []
            eps_upload_errors: list[str] = []
            for media in selected_images:
                original_url = (media.s3_url or "").strip()
                if use_eps_images:
                    try:
                        eps_url, eps_meta = _create_eps_image_with_retry(
                            ebay=ebay,
                            access_token=token_to_use,
                            media=media,
                            storage=storage,
                        )
                        image_urls.append(eps_url)
                        eps_uploads.append(eps_meta)
                    except Exception as exc:
                        eps_upload_errors.append(f"{media.original_filename}: {exc}")
                else:
                    if not original_url or not original_url.startswith("https://"):
                        st.error(
                            f"Image `{media.original_filename}` requires a public HTTPS URL when EPS upload is disabled."
                        )
                        st.stop()
                    image_urls.append(original_url)

            image_source_mode = "ebay_eps" if use_eps_images else "direct_https_urls"
            if use_eps_images and eps_upload_errors:
                st.error(
                    "eBay EPS image hosting failed for one or more selected images. "
                    "Direct/self-hosted image fallback is disabled. "
                    + " | ".join(eps_upload_errors[:5])
                )
                st.stop()
            if not image_urls:
                st.error("At least one image is required to revise listing.")
                st.stop()
            if len(image_urls) > 24:
                image_urls = image_urls[:24]

            effective_aspects_payload, injected_aspect_keys = _resolve_effective_aspects_payload()
            if injected_aspect_keys:
                st.info(
                    "Auto-filled eBay item specifics defaults for bullion/coin listing: "
                    + ", ".join(injected_aspect_keys)
                )
            inventory_payload = {
                "availability": {"shipToLocationAvailability": {"quantity": int(available_quantity)}},
                "condition": condition,
                "product": {
                    "title": listing_title or selected_listing.listing_title,
                    "description": effective_listing_description or listing_title or selected_listing.listing_title,
                    "imageUrls": image_urls,
                },
            }
            if subtitle:
                inventory_payload["product"]["subtitle"] = subtitle
            if condition_description:
                inventory_payload["conditionDescription"] = condition_description
            if effective_aspects_payload:
                inventory_payload["product"]["aspects"] = effective_aspects_payload
            _maybe_add_package_data(
                inventory_payload,
                product,
                weight_oz=float(package_weight_oz or 0.0),
                length_in=float(package_length_in or 0.0),
                width_in=float(package_width_in or 0.0),
                height_in=float(package_height_in or 0.0),
            )

            currency = (st.session_state.get("ebay_pub_currency") or default_currency).strip()
            inventory_sku = _listing_ebay_inventory_sku(product, selected_listing)
            revise_offer_payload = _build_ebay_offer_payload(
                sku=inventory_sku,
                marketplace_id=(st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
                format_type=publish_format,
                available_quantity=int(available_quantity),
                category_id=effective_category_id,
                merchant_location_key=effective_merchant_location_key,
                listing_description=effective_listing_description or listing_title or selected_listing.listing_title,
                listing_duration=listing_duration,
                payment_policy_id=payment_policy_id.strip(),
                fulfillment_policy_id=fulfillment_policy_id.strip(),
                return_policy_id=return_policy_id.strip(),
                currency=currency,
                fixed_price=float(fixed_price or 0.0),
                best_offer_enabled=bool(best_offer_enabled),
                best_offer_auto_accept=float(best_offer_auto_accept or 0.0),
                best_offer_minimum=float(best_offer_minimum or 0.0),
                auction_start_price=float(auction_start_price or 0.0),
                auction_reserve_price=float(auction_reserve_price or 0.0),
                auction_buy_now_price=float(auction_buy_now_price or 0.0),
                store_category_names=effective_store_category_names,
            )
            local_listing_price = (
                float(fixed_price or 0.0)
                if publish_format == "FIXED_PRICE"
                else float(auction_start_price or 0.0)
            )

            try:
                fell_back_inventory, inventory_error = _create_or_replace_inventory_item_with_fallback(
                    ebay=ebay,
                    access_token=token_to_use,
                    sku=inventory_sku,
                    payload=inventory_payload,
                    content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                )
                if fell_back_inventory:
                    st.warning(
                        "eBay inventory upsert succeeded only after fallback payload retry "
                        "(dropped package/video fields). Initial error: "
                        + inventory_error
                    )
                ebay.update_offer(
                    access_token=token_to_use,
                    offer_id=effective_offer_id,
                    payload=revise_offer_payload,
                    content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                )
                revised_marketplace_details = _merge_ebay_publish_metadata(
                    str(selected_listing.marketplace_details or ""),
                    {
                        "format": publish_format,
                        "offer_id": effective_offer_id,
                        "inventory_sku": inventory_sku,
                        "product_sku": str(product.sku or "").strip(),
                        "marketplace_id": str(
                            st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id
                        ).strip(),
                        "category_id": effective_category_id,
                        "store_category_names": effective_store_category_names,
                        "content_language": str(
                            st.session_state.get("ebay_pub_content_language") or default_content_language
                        ).strip(),
                        "listing_title": str(listing_title or selected_listing.listing_title or "").strip(),
                        "listing_description": effective_listing_description
                        or listing_title
                        or selected_listing.listing_title,
                        "subtitle": subtitle,
                        "condition_description": condition_description,
                        "aspects": effective_aspects_payload,
                        "aspects_json": str(st.session_state.get("ebay_pub_aspects_json") or "").strip(),
                        "image_source": image_source_mode,
                        "image_count": len(image_urls),
                        **_ebay_primary_image_metadata(selected_images, primary_image_label),
                        "revised_at": utcnow_naive().isoformat(),
                        "last_publish_error": "",
                        "last_publish_error_at": "",
                        "last_publish_error_stage": "",
                        "last_publish_error_context": {},
                    },
                )
                repo.update_listing(
                    selected_listing.id,
                    {
                        "listing_title": str(listing_title or selected_listing.listing_title or "").strip(),
                        "quantity_listed": int(available_quantity),
                        "listing_price": to_decimal(local_listing_price),
                        "listing_status": "active",
                        "marketplace_details": revised_marketplace_details,
                    },
                    actor=user.username,
                )
                st.success(f"Revised eBay offer `{effective_offer_id}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Revise listing failed: {exc}")
                st.stop()

    if load_publish_draft_and_presets:
        autosave_payload = _listings_build_ebay_publish_draft_payload(
            listing_id=int(selected_listing.id),
            listing_signature=listing_signature,
            state_keys=ebay_publish_draft_state_keys,
        )
        autosave_signature = _listings_ebay_publish_signature(autosave_payload)
        last_autosave_signature = str(st.session_state.get("ebay_pub_last_autosave_signature") or "").strip()
        last_autosave_scope = str(st.session_state.get("ebay_pub_last_autosave_scope") or "").strip()
        autosave_signature_changed = autosave_signature != last_autosave_signature or last_autosave_scope != draft_scope_key
        autosave_debounce_seconds = 5
        last_autosave_at_raw = str(st.session_state.get("ebay_pub_last_autosave_at") or "").strip()
        should_autosave_now = True
        if autosave_signature_changed and last_autosave_scope == draft_scope_key and last_autosave_at_raw:
            try:
                last_autosave_at = datetime.fromisoformat(last_autosave_at_raw)
                elapsed = (utcnow_naive() - last_autosave_at).total_seconds()
                should_autosave_now = elapsed >= float(autosave_debounce_seconds)
            except Exception:
                should_autosave_now = True
        if autosave_signature_changed and should_autosave_now:
            autosave_row = repo.save_workflow_draft(
                environment=settings.app_env,
                workflow_key=LISTINGS_EBAY_PUBLISH_WORKFLOW_KEY,
                username=user.username,
                scope_key=draft_scope_key,
                draft_payload=autosave_payload,
                status="active",
                last_step="ebay_publish",
                actor=user.username,
            )
            st.session_state["ebay_pub_last_autosave_signature"] = autosave_signature
            st.session_state["ebay_pub_last_autosave_scope"] = draft_scope_key
            st.session_state["ebay_pub_last_autosave_at"] = utcnow_naive().isoformat()
            st.session_state["ebay_pub_last_draft_id"] = int(autosave_row.id)

    if not submit_publish:
        return
    if not ensure_permission(user, "create", "Publish eBay Listing"):
        st.stop()
    if not ebay.is_configured():
        st.error("eBay app credentials are not configured.")
        return
    token_to_use = access_token.strip() or default_token
    if not token_to_use:
        st.error("User access token is required.")
        return
    effective_merchant_location_key = str(merchant_location_key or "").strip()
    if effective_merchant_location_key:
        effective_merchant_location_key = _resolve_merchant_location_key_cached(
            token_to_use=token_to_use,
            merchant_location_key_value=effective_merchant_location_key,
        )
    listing_html_errors = _get_listing_html_errors()
    if listing_html_errors:
        st.error("Listing description failed validation: " + " | ".join(listing_html_errors))
        return
    effective_listing_description, sanitize_notes = _get_effective_listing_description_and_notes()
    if not str(effective_listing_description or "").strip():
        st.error("eBay listing description must be between 1 and 4000 characters.")
        return
    if len(str(effective_listing_description or "")) > EBAY_MAX_INVENTORY_DESCRIPTION_CHARS:
        st.error(
            "eBay listing description must be "
            f"{EBAY_MAX_INVENTORY_DESCRIPTION_CHARS} characters or fewer "
            f"(currently {len(str(effective_listing_description or ''))})."
        )
        return
    if sanitize_notes:
        st.info(
            "Listing description was sanitized before eBay operations: "
            + "; ".join(sanitize_notes)
        )
    volume_pricing_tiers, volume_pricing_errors = _get_volume_pricing_tiers_and_errors()
    if publish_format == "FIXED_PRICE" and float(fixed_price or 0) <= 0:
        st.error("Buy It Now price must be greater than 0.")
        return
    if publish_format == "FIXED_PRICE" and bool(best_offer_enabled):
        if float(best_offer_auto_accept or 0.0) > 0 and float(best_offer_auto_accept or 0.0) > float(fixed_price or 0.0):
            st.error("Auto-accept offer cannot be greater than Buy It Now price.")
            return
        if float(best_offer_minimum or 0.0) > 0 and float(best_offer_minimum or 0.0) > float(fixed_price or 0.0):
            st.error("Auto-decline offer threshold cannot be greater than Buy It Now price.")
            return
        if float(best_offer_auto_accept or 0.0) > 0 and float(best_offer_minimum or 0.0) > 0:
            if float(best_offer_minimum or 0.0) > float(best_offer_auto_accept or 0.0):
                st.error("Auto-decline threshold cannot be greater than auto-accept threshold.")
                return
    if volume_pricing_errors:
        st.error("Volume pricing is invalid: " + " | ".join(volume_pricing_errors[:5]))
        return
    if volume_pricing_tiers and publish_format != "FIXED_PRICE":
        st.error("Volume pricing tiers are only supported for fixed-price listings.")
        return
    if volume_pricing_tiers and publish_format == "FIXED_PRICE":
        invalid_tiers = [t for t in volume_pricing_tiers if float(t.get("price") or 0.0) > float(fixed_price or 0.0)]
        if invalid_tiers:
            st.error("Volume tier prices cannot exceed Buy It Now price.")
            return
    if publish_format == "AUCTION":
        if float(auction_start_price or 0) <= 0:
            st.error("Auction start price must be greater than 0.")
            return
        if float(auction_reserve_price or 0) > 0 and float(auction_reserve_price or 0) < float(auction_start_price or 0):
            st.error("Auction reserve price cannot be lower than auction start price.")
            return
        if not str(listing_duration or "").strip():
            st.error("Auction duration is required.")
            return
        if float(auction_buy_now_price or 0) > 0 and float(auction_buy_now_price or 0) < float(auction_start_price or 0):
            st.error("Auction Buy It Now price cannot be lower than auction start price.")
            return
    if not effective_category_id:
        st.error("Category ID is required.")
        return
    if not effective_merchant_location_key:
        st.error("Merchant Location Key is required.")
        return
    if not payment_policy_id.strip() or not fulfillment_policy_id.strip() or not return_policy_id.strip():
        st.error("Payment, fulfillment, and return policy IDs are required.")
        return
    preflight_marketplace_id = (
        st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id
    )
    local_specific_blockers = _local_required_specific_blockers(
        category_id_value=effective_category_id,
        marketplace_id_value=str(preflight_marketplace_id or "").strip(),
    )
    if local_specific_blockers:
        st.error("eBay required item specifics blockers: " + " | ".join(local_specific_blockers[:3]))
        return
    effective_condition_rows = list(category_condition_rows or [])
    if not effective_condition_rows and effective_category_id and token_to_use:
        try:
            policies = ebay.get_item_condition_policies(
                access_token=token_to_use,
                category_id=effective_category_id,
                marketplace_id=str(preflight_marketplace_id or "").strip(),
            )
            effective_condition_rows = normalize_ebay_condition_policy_rows(
                policies,
                category_id=effective_category_id,
            )
            if effective_condition_rows:
                st.session_state["ebay_pub_category_condition_rows"] = effective_condition_rows
                st.session_state["ebay_pub_category_condition_signature"] = (
                    f"{str(preflight_marketplace_id or '').strip().upper()}:{effective_category_id}"
                )
        except Exception as exc:
            st.warning(f"eBay category condition policy lookup failed; continuing with selected condition: {exc}")
    if effective_condition_rows and not _is_condition_valid_for_loaded_policy(effective_condition_rows, condition):
        st.error(
            f"Selected condition `{condition}` is not valid for eBay category `{effective_category_id}`. "
            "Load/refresh Category Conditions and choose one of the returned options before publishing."
        )
        return
    preflight_result, reused_preflight, _preflight_signature_value = _run_or_reuse_preflight(
        token=token_to_use,
        marketplace_id=str(preflight_marketplace_id or "").strip(),
        category_id=effective_category_id,
        condition_value=condition,
        merchant_location_key=effective_merchant_location_key,
        payment_policy_id_value=payment_policy_id.strip(),
        fulfillment_policy_id_value=fulfillment_policy_id.strip(),
        return_policy_id_value=return_policy_id.strip(),
        format_type_value=str(publish_format or "FIXED_PRICE").strip().upper(),
        auction_buy_now_price_value=float(auction_buy_now_price or 0.0),
    )
    if reused_preflight:
        st.caption("Using cached dependency preflight result (inputs unchanged).")
    preflight_blockers = list(preflight_result.get("blockers") or [])
    preflight_warnings = list(preflight_result.get("warnings") or [])
    if preflight_warnings:
        st.warning("eBay dependency preflight warnings: " + " | ".join(preflight_warnings[:3]))
    if preflight_blockers:
        st.error("eBay dependency preflight blockers: " + " | ".join(preflight_blockers[:3]))
        with st.expander("Preflight Diagnostics", expanded=False):
            st.json(preflight_result)
        return

    selected_images = _resolve_selected_images()
    image_urls = []
    eps_uploads: list[dict] = []
    eps_upload_errors: list[str] = []
    for media in selected_images:
        original_url = (media.s3_url or "").strip()
        if use_eps_images:
            try:
                eps_url, eps_meta = _create_eps_image_with_retry(
                    ebay=ebay,
                    access_token=token_to_use,
                    media=media,
                    storage=storage,
                )
                image_urls.append(eps_url)
                eps_uploads.append(eps_meta)
            except Exception as exc:
                eps_upload_errors.append(f"{media.original_filename}: {exc}")
        else:
            if not original_url or not original_url.startswith("https://"):
                st.error(
                    f"Image `{media.original_filename}` does not have an HTTPS URL. "
                    "Enable EPS upload or use public HTTPS media URLs."
                )
                return
            image_urls.append(original_url)

    image_source_mode = "ebay_eps" if use_eps_images else "direct_https_urls"
    if use_eps_images and eps_upload_errors:
        st.error(
            "eBay EPS image hosting failed for one or more selected images. "
            "Direct/self-hosted image fallback is disabled. "
            + " | ".join(eps_upload_errors[:5])
        )
        return
    if not image_urls:
        st.error(
            "At least one image is required to publish to eBay."
        )
        return
    if len(image_urls) > 24:
        image_urls = image_urls[:24]
        st.warning("eBay supports up to 24 images per listing. Extra images were ignored.")

    video_ids: list[str] = []
    uploaded_video_info: dict | None = None
    inventory_video_verification: dict[str, object] = {}
    post_offer_video_verification: dict[str, object] = {}
    post_publish_video_verification: dict[str, object] = {}
    trading_listing_video_verification: dict[str, object] = {}
    video_selection_warning = _selected_ebay_video_warning(upload_video_to_ebay, selected_video_label)
    if video_selection_warning:
        st.warning(video_selection_warning)
    if upload_video_to_ebay and selected_video_label != "None":
        selected_video = _resolve_selected_video()
        if selected_video is None:
            video_selection_warning = (
                "Selected video was not found. The listing will publish without an eBay video."
            )
            st.warning(video_selection_warning)
            selected_video = None
        elif not is_ebay_video_upload_candidate(selected_video):
            video_selection_warning = (
                f"Selected video `{getattr(selected_video, 'original_filename', '') or selected_video_label}` "
                "is not an MP4/MOV video supported by this eBay upload flow. "
                "The listing will publish without an eBay video."
            )
            st.warning(video_selection_warning)
            selected_video = None
        if selected_video is None:
            pass
        else:
            try:
                video_bytes, video_content_type = _read_media_bytes(selected_video, storage)
                original_filename = str(selected_video.original_filename or "listing-video.mp4").strip()
                upload_filename = mp4_filename_for_media(selected_video)
                converted_from = ""
                if is_mov_video_media(selected_video):
                    video_bytes = transcode_mov_to_mp4(video_bytes, filename=original_filename or "listing-video.mov")
                    video_content_type = "video/mp4"
                    converted_from = "mov"
                video_id = ebay.create_video(
                    access_token=token_to_use,
                    title=upload_filename or selected_listing.listing_title,
                    size_bytes=len(video_bytes),
                    description=selected_listing.listing_title,
                )
                ebay.upload_video(
                    access_token=token_to_use,
                    video_id=video_id,
                    file_bytes=video_bytes,
                )
                final_status = ""
                for _ in range(30):
                    video_state = ebay.get_video(access_token=token_to_use, video_id=video_id)
                    final_status = str(video_state.get("status") or "").upper()
                    if final_status == "LIVE":
                        break
                    if final_status in {"PROCESSING_FAILED", "BLOCKED"}:
                        raise RuntimeError(f"Video status reached terminal failure state: {final_status}")
                    time.sleep(3)
                if final_status != "LIVE":
                    raise RuntimeError(
                        "Video upload did not reach LIVE status within timeout. "
                        f"Last status: {final_status or 'unknown'}"
                    )
                video_ids = [video_id]
                uploaded_video_info = {
                    "media_asset_id": selected_video.id,
                    "filename": upload_filename,
                    "original_filename": original_filename,
                    "video_id": video_id,
                    "status": final_status,
                    "converted_from": converted_from,
                }
            except Exception as exc:
                st.error(f"eBay video upload failed: {exc}")
                return

    effective_aspects_payload, injected_aspect_keys = _resolve_effective_aspects_payload()
    if injected_aspect_keys:
        st.info(
            "Auto-filled eBay item specifics defaults for bullion/coin listing: "
            + ", ".join(injected_aspect_keys)
        )
    inventory_payload = {
        "availability": {
            "shipToLocationAvailability": {"quantity": int(available_quantity)}
        },
        "condition": condition,
        "product": {
            "title": listing_title or selected_listing.listing_title,
            "description": effective_listing_description or listing_title or selected_listing.listing_title,
            "imageUrls": image_urls[:24],
        },
    }
    if subtitle:
        inventory_payload["product"]["subtitle"] = subtitle
    if condition_description:
        inventory_payload["conditionDescription"] = condition_description
    if effective_aspects_payload:
        inventory_payload["product"]["aspects"] = effective_aspects_payload
    if video_ids:
        inventory_payload["product"]["videoIds"] = video_ids
    _maybe_add_package_data(
        inventory_payload,
        product,
        weight_oz=float(package_weight_oz or 0.0),
        length_in=float(package_length_in or 0.0),
        width_in=float(package_width_in or 0.0),
        height_in=float(package_height_in or 0.0),
    )

    currency = (st.session_state.get("ebay_pub_currency") or default_currency).strip()
    inventory_sku = _listing_ebay_inventory_sku(product, selected_listing)
    offer_payload = _build_ebay_offer_payload(
        sku=inventory_sku,
        marketplace_id=(st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
        format_type=publish_format,
        available_quantity=int(available_quantity),
        category_id=effective_category_id,
        merchant_location_key=effective_merchant_location_key,
        listing_description=effective_listing_description or listing_title or selected_listing.listing_title,
        listing_duration=listing_duration,
        payment_policy_id=payment_policy_id.strip(),
        fulfillment_policy_id=fulfillment_policy_id.strip(),
        return_policy_id=return_policy_id.strip(),
        currency=currency,
        fixed_price=float(fixed_price or 0.0),
        best_offer_enabled=bool(best_offer_enabled),
        best_offer_auto_accept=float(best_offer_auto_accept or 0.0),
        best_offer_minimum=float(best_offer_minimum or 0.0),
        auction_start_price=float(auction_start_price or 0.0),
        auction_reserve_price=float(auction_reserve_price or 0.0),
        auction_buy_now_price=float(auction_buy_now_price or 0.0),
        store_category_names=effective_store_category_names,
    )

    publish_stage = "init"
    publish_context: dict[str, object] = {
        "post_mode": str(post_mode or "").strip(),
        "format": str(publish_format or "").strip().upper(),
        "marketplace_id": str(st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
        "category_id": str(effective_category_id or "").strip(),
        "store_category_names": effective_store_category_names,
        "merchant_location_key": str(effective_merchant_location_key or "").strip(),
        "payment_policy_id": str(payment_policy_id or "").strip(),
        "fulfillment_policy_id": str(fulfillment_policy_id or "").strip(),
        "return_policy_id": str(return_policy_id or "").strip(),
        "inventory_sku": inventory_sku,
        "product_sku": str(product.sku or "").strip(),
        "use_eps_images": bool(use_eps_images),
        "upload_video_to_ebay": bool(upload_video_to_ebay),
        "video_warning": video_selection_warning,
        "available_quantity": int(available_quantity or 1),
    }
    try:
        publish_stage = "load_existing_offer"
        existing_offer_id = ""
        recovered_existing_offer = False
        existing_details_raw = str(selected_listing.marketplace_details or "").strip()
        if existing_details_raw:
            try:
                existing_details = json.loads(existing_details_raw)
                if isinstance(existing_details, dict):
                    existing_offer_id = str(
                        ((existing_details.get("ebay_publish") or {}) if isinstance(existing_details.get("ebay_publish"), dict) else {}).get("offer_id")
                        or ""
                    ).strip()
            except Exception:
                existing_offer_id = ""
        if not existing_offer_id:
            existing_offer_id = ""

        publish_stage = "upsert_inventory"
        fell_back_inventory, inventory_error = _create_or_replace_inventory_item_with_fallback(
            ebay=ebay,
            access_token=token_to_use,
            sku=inventory_sku,
            payload=inventory_payload,
            content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
            preserve_video_ids=bool(video_ids),
        )
        if fell_back_inventory:
            st.warning(
                "eBay inventory upsert succeeded only after fallback payload retry "
                + ("(dropped package fields; video IDs were preserved). Initial error: " if video_ids else "(dropped package/video fields). Initial error: ")
                + inventory_error
            )
        if video_ids:
            publish_stage = "verify_inventory_video_ids"
            inventory_video_verification = _verify_inventory_video_ids(
                ebay=ebay,
                access_token=token_to_use,
                sku=inventory_sku,
                expected_video_ids=video_ids,
                content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
            )
            publish_context["inventory_video_ids_verified"] = bool(
                inventory_video_verification.get("verified")
            )
            publish_context["inventory_video_ids"] = list(
                inventory_video_verification.get("actual_video_ids") or []
            )
        if existing_offer_id:
            publish_stage = "update_existing_offer"
            offer_id = existing_offer_id
            ebay.update_offer(
                access_token=token_to_use,
                offer_id=offer_id,
                payload=offer_payload,
                content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
            )
        else:
            publish_stage = "create_or_recover_offer"
            offer_id, recovered_existing_offer = _create_or_update_offer_with_duplicate_recovery(
                ebay=ebay,
                access_token=token_to_use,
                payload=offer_payload,
                content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                sku=inventory_sku,
                marketplace_id=(st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
                format_type=publish_format,
            )
        if video_ids:
            publish_stage = "verify_post_offer_inventory_video_ids"
            post_offer_video_verification = _verify_inventory_video_ids(
                ebay=ebay,
                access_token=token_to_use,
                sku=inventory_sku,
                expected_video_ids=video_ids,
                content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
            )
            publish_context["post_offer_inventory_video_ids_verified"] = bool(
                post_offer_video_verification.get("verified")
            )
            publish_context["post_offer_inventory_video_ids"] = list(
                post_offer_video_verification.get("actual_video_ids") or []
            )
        listing_id = ""
        listing_url = ""
        if post_mode == "Publish Live Listing":
            publish_stage = "publish_offer"
            publish_result = ebay.publish_offer(
                access_token=token_to_use,
                offer_id=offer_id,
                inventory_sku=inventory_sku,
                content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
            )
            listing_id = str(publish_result.get("listingId") or "").strip()
            if not listing_id:
                raise RuntimeError(f"eBay publishOffer did not return listingId. payload={publish_result}")
            listing_url = ebay.listing_url_for_id(listing_id)
            if video_ids:
                publish_stage = "verify_post_publish_inventory_video_ids"
                post_publish_video_verification = _verify_inventory_video_ids(
                    ebay=ebay,
                    access_token=token_to_use,
                    sku=inventory_sku,
                    expected_video_ids=video_ids,
                    content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                )
                publish_context["post_publish_inventory_video_ids_verified"] = bool(
                    post_publish_video_verification.get("verified")
                )
                publish_context["post_publish_inventory_video_ids"] = list(
                    post_publish_video_verification.get("actual_video_ids") or []
                )
                publish_stage = "verify_trading_listing_video_ids"
                trading_listing_video_verification = _verify_trading_listing_video_ids(
                    ebay=ebay,
                    access_token=token_to_use,
                    listing_id=listing_id,
                    expected_video_ids=video_ids,
                    marketplace_id=(st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
                )
                publish_context["trading_listing_video_ids_verified"] = bool(
                    trading_listing_video_verification.get("verified")
                )
                publish_context["trading_listing_video_ids"] = list(
                    trading_listing_video_verification.get("actual_video_ids") or []
                )

        details_obj: dict = {}
        existing_details = (selected_listing.marketplace_details or "").strip()
        if existing_details:
            try:
                parsed = json.loads(existing_details)
                if isinstance(parsed, dict):
                    details_obj = parsed
                else:
                    details_obj = {"notes": existing_details}
            except Exception:
                details_obj = {"notes": existing_details}
        publish_stage = "update_local_listing"
        details_obj["ebay_publish"] = {
            "format": publish_format,
            "post_mode": str(post_mode or "").strip(),
            "listing_duration": listing_duration,
            "best_offer_enabled": bool(best_offer_enabled) if publish_format == "FIXED_PRICE" else False,
            "best_offer_auto_accept": float(best_offer_auto_accept or 0) if publish_format == "FIXED_PRICE" else 0.0,
            "best_offer_minimum": float(best_offer_minimum or 0) if publish_format == "FIXED_PRICE" else 0.0,
            "auction_start_price": float(auction_start_price or 0) if publish_format == "AUCTION" else 0.0,
            "auction_reserve_price": float(auction_reserve_price or 0) if publish_format == "AUCTION" else 0.0,
            "auction_buy_now_price": float(auction_buy_now_price or 0) if publish_format == "AUCTION" else 0.0,
            "offer_id": offer_id,
            "inventory_sku": inventory_sku,
            "product_sku": str(product.sku or "").strip(),
            "marketplace_id": (st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
            "category_id": effective_category_id,
            "store_category_names": effective_store_category_names,
            "merchant_location_key": effective_merchant_location_key,
            "payment_policy_id": payment_policy_id.strip(),
            "fulfillment_policy_id": fulfillment_policy_id.strip(),
            "return_policy_id": return_policy_id.strip(),
            "currency": currency,
            "content_language": (st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
            "listing_title": str(listing_title or "").strip(),
            "listing_description": effective_listing_description or listing_title or selected_listing.listing_title,
            "subtitle": subtitle,
            "condition_description": condition_description,
            "aspects": effective_aspects_payload,
            "aspects_json": str(st.session_state.get("ebay_pub_aspects_json") or "").strip(),
            "package_weight_oz": float(package_weight_oz or 0.0),
            "package_length_in": float(package_length_in or 0.0),
            "package_width_in": float(package_width_in or 0.0),
            "package_height_in": float(package_height_in or 0.0),
            "shipping_service": str(shipping_service or "").strip(),
            "handling_days": int(handling_days or 0),
            "shipping_cost": float(shipping_cost or 0.0),
            "estimated_buyer_paid_shipping": float(estimated_buyer_shipping or 0.0),
            "estimated_promoted_rate_percent": float(estimated_promoted_rate or 0.0),
            "fee_estimate": _get_listing_fee_estimate(),
            "volume_pricing_tiers": volume_pricing_tiers,
            "volume_pricing_json": str(st.session_state.get("ebay_pub_volume_pricing_json") or "").strip(),
            "published_at": utcnow_naive().isoformat(),
            "image_source": image_source_mode,
            "image_count": len(image_urls),
            **_ebay_primary_image_metadata(selected_images, primary_image_label),
            "video_attached": bool(video_ids),
            "video_warning": video_selection_warning,
            "inventory_video_verification": inventory_video_verification,
            "post_offer_video_verification": post_offer_video_verification,
            "post_publish_video_verification": post_publish_video_verification,
            "trading_listing_video_verification": trading_listing_video_verification,
            "last_publish_error": "",
            "last_publish_error_at": "",
            "last_publish_error_stage": "",
            "last_publish_error_context": {},
        }
        if eps_uploads:
            details_obj["ebay_publish"]["eps_uploads"] = eps_uploads
        if eps_upload_errors:
            details_obj["ebay_publish"]["eps_upload_errors"] = eps_upload_errors[:10]
        if uploaded_video_info:
            details_obj["ebay_publish"]["video_upload"] = uploaded_video_info
        external_listing_owner_id = _external_listing_id_owner(
            repo,
            marketplace=str(selected_listing.marketplace or "ebay"),
            external_listing_id=listing_id,
            exclude_listing_id=int(selected_listing.id),
            listings=listings,
            owner_by_market_and_external_id=_get_external_listing_owner_map(),
        )
        external_listing_id_available = external_listing_owner_id is None
        repo.update_listing(
            selected_listing.id,
            (lambda _updates: _updates)(
                {
                "listing_title": str(listing_title or selected_listing.listing_title or "").strip(),
                "external_listing_id": (
                    listing_id
                    if listing_id and external_listing_id_available
                    else selected_listing.external_listing_id
                ),
                "marketplace_url": (
                    listing_url
                    if listing_url and (not listing_id or external_listing_id_available)
                    else selected_listing.marketplace_url
                ),
                "listing_status": "active" if post_mode == "Publish Live Listing" else "draft",
                "marketplace_details": json.dumps(details_obj, indent=2),
                "quantity_listed": int(available_quantity),
                }
            ),
            actor=user.username,
        )
        if listing_id:
            if external_listing_owner_id is not None:
                st.warning(
                    f"eBay listingId `{listing_id}` is already linked to local listing #{external_listing_owner_id}; "
                    "saved offer metadata without changing this row's external listing ID."
                )
        if post_mode == "Publish Live Listing":
            st.session_state["listings_publish_flash"] = {
                "level": "warning" if video_selection_warning else "success",
                "message": (
                    f"Published to eBay. listing_id={listing_id}, offer_id={offer_id}"
                    + (" (reused existing offer)" if recovered_existing_offer else "")
                ),
                "warning": video_selection_warning,
                "offer_id": offer_id,
                "listing_url": listing_url,
            }
        else:
            st.session_state["listings_publish_flash"] = {
                "level": "warning" if video_selection_warning else "success",
                "message": (
                    (
                        f"Saved unpublished eBay offer by reusing existing offer_id={offer_id}. "
                        if recovered_existing_offer
                        else f"Saved unpublished eBay offer (API draft). offer_id={offer_id}. "
                    )
                    +
                    "Use Manage Existing eBay Listing to publish when ready."
                ),
                "warning": video_selection_warning,
                "offer_id": offer_id,
                "listing_url": ebay.seller_hub_listings_url(),
            }
        st.session_state["ebay_pub_manage_offer_id"] = offer_id
        st.rerun()
    except Exception as exc:
        try:
            _persist_listing_publish_error(
                repo,
                selected_listing,
                actor=user.username,
                error_message=str(exc),
                stage=publish_stage,
                context=publish_context,
            )
        except Exception:
            pass
        st.error(f"eBay publish failed: {exc}")
