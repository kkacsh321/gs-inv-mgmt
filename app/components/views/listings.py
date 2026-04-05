from datetime import datetime, timedelta
import json
import re
import time

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import build_product_options, iso_or_none, to_decimal
from app.components.views.shared import (
    MARKETPLACES,
    handoff_to_documents_draft,
    render_help_panel,
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
from app.services.ebay import EbayClient
from app.services.listing_orchestration import (
    build_channel_adapters,
    capability_matrix_rows,
    orchestration_status_for_listing,
)
from app.services.listing_readiness import evaluate_ebay_readiness
from app.services.media_storage import MediaStorageService
from app.services.runtime_settings import get_runtime_bool, get_runtime_str
from app.services.validation import ValidationService, ValidationError
from app.utils.time import utc_today, utcnow_naive


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


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


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
            or get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7")
            or "DAYS_7"
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
        or ("GTC" if st.session_state["create_listing_ebay_format"] == "FIXED_PRICE" else "DAYS_7")
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


def _maybe_add_package_data(payload: dict, product: Product) -> None:
    weight = _to_float(product.package_weight_oz, 0.0)
    length = _to_float(product.package_length_in, 0.0)
    width = _to_float(product.package_width_in, 0.0)
    height = _to_float(product.package_height_in, 0.0)

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
    if storage is not None and storage.enabled and media.s3_bucket and media.s3_key:
        try:
            return storage.get_object_bytes(media.s3_bucket, media.s3_key)
        except Exception:
            pass
    if media.s3_url:
        response = requests.get(media.s3_url, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type") or media.content_type or "application/octet-stream"
        return response.content, content_type
    raise RuntimeError("Media file bytes could not be loaded from storage or URL.")


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


def _sanitize_listing_html(value: str) -> tuple[str, list[str]]:
    html = (value or "").strip()
    if not html:
        return "", []

    notes: list[str] = []
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
) -> dict:
    listing_id = int(listing_obj.id)
    offer_id = ""
    external_listing_id = ""
    message = ""
    status = "error"
    try:
        product_obj = repo.db.get(Product, int(listing_obj.product_id))
        if product_obj is None:
            raise ValueError("Linked product not found.")
        image_urls = [
            str(m.s3_url or "").strip()
            for m in (listing_obj.media_assets or [])
            if str(m.media_type or "").strip().lower() == "image"
            and str(m.s3_url or "").strip().startswith("https://")
        ]
        if not image_urls:
            raise ValueError("No HTTPS listing images available.")
        image_urls = image_urls[:24]

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
            sku=product_obj.sku,
            payload=inventory_payload,
            content_language=content_language,
        )

        offer_payload = {
            "sku": product_obj.sku,
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
        offer_result = ebay.create_offer(access_token=access_token, payload=offer_payload)
        offer_id = str(offer_result.get("offerId") or "").strip()
        if not offer_id:
            raise RuntimeError("eBay createOffer did not return offerId.")
        publish_result = ebay.publish_offer(access_token=access_token, offer_id=offer_id)
        external_listing_id = str(publish_result.get("listingId") or "").strip()
        if not external_listing_id:
            raise RuntimeError("eBay publishOffer did not return listingId.")
        listing_url = ebay.listing_url_for_id(external_listing_id)
        status = "success"
        message = "Published"
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
            "listing_id": external_listing_id,
            "status": status,
            "message": message,
        }
    )
    details_obj["publish_batch_execution"] = exec_history[-100:]
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
    st.subheader("Marketplace Listings")
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
    products = repo.list_products()

    if not products:
        st.info("Create at least one product before adding listings.")
        return

    product_by_id = {int(p.id): p for p in products}
    coin_ref_by_id = {int(row.id): row for row in repo.list_coin_references(active_only=True, limit=5000)}
    if "create_listing_marketplace" not in st.session_state:
        st.session_state["create_listing_marketplace"] = str(
            st.session_state.get("ebay_workspace_store_marketplace_id_input")
            or "ebay"
        ).strip().lower()
    workspace_store_profiles = _load_workspace_store_profiles(repo)
    default_workspace_store_profile = get_runtime_str(repo, "ebay_workspace_default_store_profile", "").strip()
    if (
        default_workspace_store_profile
        and default_workspace_store_profile in workspace_store_profiles
        and not bool(st.session_state.get("listings_create_store_default_applied_once"))
    ):
        _apply_store_profile_to_listing_create(workspace_store_profiles[default_workspace_store_profile])
        st.session_state["listings_create_store_profile_selected"] = default_workspace_store_profile
        st.session_state["listings_create_store_default_applied_once"] = True

    st.markdown("### eBay Listing Templates")
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
    if workspace_store_profiles:
        store_profile_options = ["None"] + sorted(workspace_store_profiles.keys())
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
        st.caption("Insert reusable Golden Stackers HTML blocks into create-flow details or template details.")
        block_library = _listing_html_block_library()
        selected_block_name = st.selectbox(
            "Block",
            options=list(block_library.keys()),
            key="listings_html_block_select",
        )
        hb1, hb2 = st.columns(2)
        with hb1:
            if st.button("Insert Into Create Listing Details", key="listings_insert_block_create_btn"):
                current = str(st.session_state.get("create_listing_details") or "").strip()
                block = str(block_library.get(selected_block_name) or "").strip()
                st.session_state["create_listing_details"] = (
                    f"{current}\n\n{block}".strip() if current else block
                )
                st.success(f"Inserted `{selected_block_name}` into create listing details.")
                st.rerun()
        with hb2:
            if st.button("Insert Into Template Details", key="listings_insert_block_template_btn"):
                current = str(st.session_state.get("ebay_template_details") or "").strip()
                block = str(block_library.get(selected_block_name) or "").strip()
                st.session_state["ebay_template_details"] = (
                    f"{current}\n\n{block}".strip() if current else block
                )
                st.success(f"Inserted `{selected_block_name}` into template details.")
                st.rerun()
        preview_html = str(block_library.get(selected_block_name) or "").strip()
        if preview_html:
            st.caption("Block preview")
            components.html(preview_html, height=150, scrolling=True)
            st.code(preview_html, language="html")
        if st.button("Create Golden Stackers Starter Templates", key="listings_seed_starter_templates_btn"):
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

    with st.expander("Manage eBay Listing Templates", expanded=False):
        with st.form("save_ebay_listing_template_form"):
            t1, t2, t3 = st.columns(3)
            with t1:
                template_name = st.text_input("Template Name", key="ebay_template_name")
            with t2:
                template_marketplace = st.selectbox("Marketplace", MARKETPLACES, index=MARKETPLACES.index("ebay"))
            with t3:
                template_status_default = st.selectbox(
                    "Default Listing Status",
                    ["draft", "active", "ended"],
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

    st.markdown("### Optional Initial Listing Media")
    listing_uploaded_by = st.text_input("Uploaded By", value="employee", key="listing_uploaded_by")
    listing_files = render_media_capture_inputs(
        key_prefix="create_listing_media",
        upload_label="Listing Photos/Videos (optional)",
        allow_enhanced=True,
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
                "`suggested_title`, `suggested_price`, `suggested_marketplace_details`, `publish_checklist`. "
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
            )
            payload = _try_extract_json_object(result.text)
            if not payload:
                st.warning("AI output was not valid JSON. Raw response captured below.")
            else:
                title_val = str(payload.get("suggested_title") or "").strip()
                details_val = str(payload.get("suggested_marketplace_details") or "").strip()
                price_val = str(payload.get("suggested_price") or "").strip()
                checklist_val = payload.get("publish_checklist")
                if title_val:
                    st.session_state["create_listing_title"] = title_val
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
                st.success("Listing copilot suggestions applied to create defaults.")
            st.session_state["listing_copilot_raw"] = str(result.text or "").strip()
            st.rerun()
        except Exception as exc:
            st.error(f"Listing copilot suggestion generation failed: {exc}")

    raw_listing_ai = str(st.session_state.get("listing_copilot_raw") or "").strip()
    if raw_listing_ai:
        with st.expander("Last Listings Copilot Payload", expanded=False):
            st.code(raw_listing_ai, language="json")

    preset_rows_for_create = repo.list_ebay_publish_presets(
        environment=settings.app_env,
        username=user.username,
        active_only=True,
    )
    default_create_preset = next((p for p in preset_rows_for_create if bool(p.is_default)), None)
    if default_create_preset is None and preset_rows_for_create:
        default_create_preset = preset_rows_for_create[0]
    create_ebay_defaults = _ebay_create_publish_defaults(repo)
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
    st.session_state.setdefault("create_listing_ebay_auction_start_price", float(create_ebay_defaults.get("auction_start_price") or 1.0))
    st.session_state.setdefault("create_listing_ebay_auction_reserve_price", float(create_ebay_defaults.get("auction_reserve_price") or 0.0))
    st.session_state.setdefault("create_listing_ebay_auction_buy_now_price", float(create_ebay_defaults.get("auction_buy_now_price") or 0.0))
    create_format_type = str(
        st.session_state.get("create_listing_ebay_format")
        or (default_create_preset.format_type if default_create_preset else "")
        or create_ebay_defaults.get("format_type")
        or get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE")
    ).strip().upper()
    if create_format_type not in {"FIXED_PRICE", "AUCTION"}:
        create_format_type = "FIXED_PRICE"
    create_listing_duration = str(
        st.session_state.get("create_listing_ebay_duration")
        or (default_create_preset.listing_duration if default_create_preset else "")
        or create_ebay_defaults.get("listing_duration")
        or ("GTC" if create_format_type == "FIXED_PRICE" else get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7"))
    ).strip().upper()
    readiness_preview = evaluate_ebay_readiness(
        listing_title=str(st.session_state.get("create_listing_title") or "").strip(),
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
            or (default_create_preset.category_id if default_create_preset else "")
            or str(create_ebay_defaults.get("category_id") or "").strip()
        ),
        merchant_location_key=(
            str(st.session_state.get("create_listing_ebay_merchant_location_key") or "").strip()
            or (default_create_preset.merchant_location_key if default_create_preset else "")
            or str(create_ebay_defaults.get("merchant_location_key") or "").strip()
        ),
        payment_policy_id=(
            str(st.session_state.get("create_listing_ebay_payment_policy_id") or "").strip()
            or (default_create_preset.payment_policy_id if default_create_preset else "")
            or str(create_ebay_defaults.get("payment_policy_id") or "").strip()
        ),
        fulfillment_policy_id=(
            str(st.session_state.get("create_listing_ebay_fulfillment_policy_id") or "").strip()
            or (default_create_preset.fulfillment_policy_id if default_create_preset else "")
            or str(create_ebay_defaults.get("fulfillment_policy_id") or "").strip()
        ),
        return_policy_id=(
            str(st.session_state.get("create_listing_ebay_return_policy_id") or "").strip()
            or (default_create_preset.return_policy_id if default_create_preset else "")
            or str(create_ebay_defaults.get("return_policy_id") or "").strip()
        ),
    )
    st.caption(
        f"Create-flow eBay readiness preview: status=`{readiness_preview.status}` score=`{readiness_preview.score}` "
        f"blockers=`{len(readiness_preview.blockers)}` warnings=`{len(readiness_preview.warnings)}`"
    )
    st.caption(
        "Create-flow eBay defaults source: active workspace store profile values with runtime fallback "
        f"(format=`{create_format_type}`, duration=`{create_listing_duration}`)."
    )
    if readiness_preview.blockers:
        st.warning("Readiness blockers: " + " | ".join(readiness_preview.blockers))
    elif readiness_preview.warnings:
        st.info("Readiness warnings: " + " | ".join(readiness_preview.warnings))
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

    st.markdown("### Bulk Draft Listing Creator")
    with st.expander("Create Draft Listings From Selected Products", expanded=False):
        st.caption(
            "Use this for batch intake-to-listing flow. All created listings are `draft` and must be reviewed before publish."
        )
        products_for_bulk = repo.list_products()
        listings_for_bulk = repo.list_listings()
        existing_listing_pairs = {
            (int(l.product_id), str(l.marketplace or "").strip().lower())
            for l in listings_for_bulk
        }
        bf1, bf2, bf3 = st.columns(3)
        with bf1:
            bulk_product_query = st.text_input(
                "Product Search",
                value="",
                key="listings_bulk_create_product_query",
            ).strip().lower()
        with bf2:
            bulk_categories = st.multiselect(
                "Filter Categories",
                options=sorted({str(p.category or "").strip() for p in products_for_bulk if str(p.category or "").strip()}),
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

        candidate_products = []
        for product in products_for_bulk:
            if int(product.current_quantity or 0) <= 0:
                continue
            if bulk_categories and str(product.category or "").strip() not in set(bulk_categories):
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
        candidate_products = sorted(candidate_products, key=lambda p: (str(p.title or "").lower(), int(p.id)))
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
                product_map_bulk = {int(p.id): p for p in products_for_bulk}
                selected_product_ids = [candidate_options[k] for k in selected_candidate_keys if k in candidate_options]
                created_count = 0
                skipped_count = 0
                error_count = 0
                for product_id in selected_product_ids:
                    product = product_map_bulk.get(int(product_id))
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

    listings = repo.list_listings()
    default_format_type = get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE").strip().upper()
    default_auction_duration = get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7").strip().upper()
    listing_rows = []
    for l in listings:
        format_type = ""
        format_hint = ""
        if str(l.marketplace or "").strip().lower() == "ebay":
            publish_meta = _listing_publish_meta(l)
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
            auction_start_price = _to_float(publish_meta.get("auction_start_price"), float(l.listing_price or 0))
            auction_reserve_price = _to_float(publish_meta.get("auction_reserve_price"), 0.0)
            auction_buy_now_price = _to_float(publish_meta.get("auction_buy_now_price"), 0.0)
            format_hints: list[str] = []
            if format_type == "FIXED_PRICE":
                if float(l.listing_price or 0) <= 0:
                    format_hints.append("Fixed Missing BIN")
            else:
                if float(auction_start_price or 0) <= 0:
                    format_hints.append("Auction Missing Start")
                if auction_duration not in {"DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"}:
                    format_hints.append("Auction Missing Duration")
                if float(auction_reserve_price or 0) > 0 and float(auction_reserve_price or 0) < float(auction_start_price or 0):
                    format_hints.append("Reserve < Start")
                if float(auction_buy_now_price or 0) > 0 and float(auction_buy_now_price or 0) < float(auction_start_price or 0):
                    format_hints.append("BIN < Start")
            format_hint = "; ".join(format_hints)
        listing_rows.append(
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
                "format_type": format_type,
                "format_hint": format_hint,
                "review_status": (l.review_status or "pending"),
                "reviewed_at": iso_or_none(l.reviewed_at),
                "reviewed_by": (l.reviewed_by or ""),
                "media_count": len(l.media_assets),
            }
        )
    photo_comp_listing_ids = _photo_comp_created_listing_ids(repo)
    for row in listing_rows:
        row["origin"] = "photo_comp_draft" if int(row["id"]) in photo_comp_listing_ids else "other"
        row["origin_label"] = "Photo-Comp Draft" if row["origin"] == "photo_comp_draft" else "Other"
    template_usage_rows = []
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
        st.markdown("### eBay Template Usage")
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
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        listing_filter_query = st.text_input("Search Title / External ID", key="listings_filter_query")
    with f2:
        listing_filter_marketplaces = st.multiselect(
            "Marketplace",
            options=sorted({str(row["marketplace"]) for row in listing_rows if row.get("marketplace")}),
            default=[],
            key="listings_filter_marketplaces",
        )
    with f3:
        listing_filter_status = st.multiselect(
            "Status",
            options=sorted({str(row["status"]) for row in listing_rows if row.get("status")}),
            default=[],
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
    listing_filter_format_issue_only = st.checkbox(
        "Format Issue Only",
        value=False,
        key="listings_filter_format_issue_only",
        help="Show only listings with non-empty format_hint (fixed/auction setup issues).",
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
        },
    )
    preset_payload = {
        "query": "",
        "marketplaces": ["ebay"],
        "statuses": ["draft"],
        "origin": "photo_comp_draft",
        "format_issue_only": False,
    }
    format_fix_preset_payload = {
        "query": "",
        "marketplaces": ["ebay"],
        "statuses": ["draft", "active"],
        "origin": "all",
        "format_issue_only": True,
    }
    pf1, pf2, pf3, pf4 = st.columns(4)
    with pf1:
        if st.button("Use Photo-Comp Review Queue", key="listings_use_photo_comp_review_preset"):
            st.session_state["listings_filter_query"] = str(preset_payload["query"])
            st.session_state["listings_filter_marketplaces"] = list(preset_payload["marketplaces"])
            st.session_state["listings_filter_status"] = list(preset_payload["statuses"])
            st.session_state["listings_filter_origin"] = str(preset_payload["origin"])
            st.session_state["listings_filter_format_issue_only"] = bool(
                preset_payload["format_issue_only"]
            )
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
            st.session_state["listings_filter_query"] = str(format_fix_preset_payload["query"])
            st.session_state["listings_filter_marketplaces"] = list(format_fix_preset_payload["marketplaces"])
            st.session_state["listings_filter_status"] = list(format_fix_preset_payload["statuses"])
            st.session_state["listings_filter_origin"] = str(format_fix_preset_payload["origin"])
            st.session_state["listings_filter_format_issue_only"] = bool(
                format_fix_preset_payload["format_issue_only"]
            )
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
    marketplaces = {
        str(v).strip().lower() for v in (effective_filter.get("marketplaces") or []) if str(v).strip()
    }
    statuses = {str(v).strip().lower() for v in (effective_filter.get("statuses") or []) if str(v).strip()}
    origin_filter = str(effective_filter.get("origin") or "all").strip().lower()
    format_issue_only = bool(effective_filter.get("format_issue_only"))
    filtered_rows = []
    for row in listing_rows:
        if q and q not in str(row.get("title") or "").lower() and q not in str(row.get("external_listing_id") or "").lower():
            continue
        if marketplaces and str(row.get("marketplace") or "").strip().lower() not in marketplaces:
            continue
        if statuses and str(row.get("status") or "").strip().lower() not in statuses:
            continue
        if origin_filter in {"photo_comp_draft", "other"} and str(row.get("origin") or "").strip().lower() != origin_filter:
            continue
        if format_issue_only and not str(row.get("format_hint") or "").strip():
            continue
        filtered_rows.append(row)

    filtered_df = pd.DataFrame(filtered_rows)
    st.markdown("### Listing Table + Side Panel")
    table_col, panel_col = st.columns([2, 1])
    with table_col:
        channel_adapters = build_channel_adapters()
        render_table_toolbar(
            df=filtered_df,
            section_key="listings_table",
            export_basename="listings_filtered",
            active_filters={
                "query": q,
                "marketplaces": sorted(marketplaces),
                "statuses": sorted(statuses),
                "origin": origin_filter,
                "format_issue_only": bool(format_issue_only),
            },
        )
        st.dataframe(filtered_df, use_container_width=True)
        render_standard_row_actions(
            repo,
            entity_type="listing",
            rows=filtered_rows,
            id_field="id",
            title="Listing Row Actions",
        )

        st.markdown("### eBay Readiness Queue")
        preset_rows_for_readiness = repo.list_ebay_publish_presets(
            environment=settings.app_env,
            username=user.username,
            active_only=True,
        )
        default_preset = next((p for p in preset_rows_for_readiness if bool(p.is_default)), None)
        if default_preset is None and preset_rows_for_readiness:
            default_preset = preset_rows_for_readiness[0]
        readiness_rows: list[dict] = []
        for listing in listings:
            if (listing.marketplace or "").strip().lower() != "ebay":
                continue
            publish_meta = _listing_publish_meta(listing)
            format_type = str(
                publish_meta.get("format")
                or publish_meta.get("format_type")
                or (default_preset.format_type if default_preset else "")
                or get_runtime_str(repo, "ebay_listing_format_default", "FIXED_PRICE")
            ).strip().upper()
            if format_type not in {"FIXED_PRICE", "AUCTION"}:
                format_type = "FIXED_PRICE"
            listing_duration = str(
                publish_meta.get("listing_duration")
                or (default_preset.listing_duration if default_preset else "")
                or ("GTC" if format_type == "FIXED_PRICE" else get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7"))
            ).strip().upper()
            auction_start_price = _to_float(
                publish_meta.get("auction_start_price"),
                float(listing.listing_price or 0),
            )
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
                category_id=(default_preset.category_id if default_preset else ""),
                merchant_location_key=(default_preset.merchant_location_key if default_preset else ""),
                payment_policy_id=(default_preset.payment_policy_id if default_preset else ""),
                fulfillment_policy_id=(default_preset.fulfillment_policy_id if default_preset else ""),
                return_policy_id=(default_preset.return_policy_id if default_preset else ""),
            )
            review_status = (listing.review_status or "pending").strip().lower()
            blockers = list(readiness.blockers)
            warnings = list(readiness.warnings)
            if format_type == "AUCTION" and best_offer_enabled:
                warnings.append("Best Offer is ignored for auction format")
            if review_status != "approved":
                blockers.append("Listing review must be approved before publish.")
            product = repo.db.get(Product, listing.product_id)
            readiness_rows.append(
                {
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
            )
        if readiness_rows:
            st.markdown("#### Reviewer Dashboard")
            now_utc = utcnow_naive()
            pending_rows = [
                row for row in readiness_rows if str(row.get("review_status") or "pending").strip().lower() != "approved"
            ]
            approved_today = 0
            approved_7d = 0
            for listing in listings:
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
                    listing_obj = next((l for l in listings if int(l.id) == int(row.get("listing_id") or 0)), None)
                    if listing_obj and listing_obj.created_at is not None:
                        pending_dates.append(listing_obj.created_at)
                if pending_dates:
                    oldest_pending_days = max(0, int((now_utc - min(pending_dates)).days))
            rd1, rd2, rd3, rd4 = st.columns(4)
            rd1.metric("Pending Review", len(pending_rows))
            rd2.metric("Oldest Pending (days)", oldest_pending_days)
            rd3.metric("Approved (24h)", int(approved_today))
            rd4.metric("Approved (7d)", int(approved_7d))

            reviewer_rows = []
            for listing in listings:
                review_status = str(getattr(listing, "review_status", "pending") or "pending").strip().lower()
                reviewed_by = str(getattr(listing, "reviewed_by", "") or "").strip() or "(unassigned)"
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

            st.markdown("#### Readiness Blocker Breakdown")
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
                st.markdown("#### Top Blocker Quick Filters")
                top_blockers = sorted(
                    blocker_counts.items(),
                    key=lambda item: (-int(item[1]), str(item[0])),
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
                st.markdown("#### Create Follow-up Task From Blocker")
                bf1, bf2, bf3, bf4 = st.columns(4)
                blocker_reason_options = sorted(blocker_counts.keys())
                with bf1:
                    selected_blocker_reason = st.selectbox(
                        "Blocker Reason",
                        options=blocker_reason_options,
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
                st.markdown("#### Recent Blocker Follow-up Tasks")
                recent_followup_events = [
                    row
                    for row in repo.list_audit_logs(limit=1500)
                    if str(getattr(row, "entity_type", "") or "").strip().lower() == "workspace_followup"
                ]
                today = utc_today()
                task_state_map: dict[str, dict] = {}
                for event in sorted(
                    recent_followup_events,
                    key=lambda r: (getattr(r, "changed_at", None) or datetime.min),
                    reverse=True,
                ):
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
                            "status": str(payload.get("status") or "").strip().lower() or ("resolved" if str(getattr(event, "action", "")).strip().lower() == "resolve" else "open"),
                            "created_at": iso_or_none(changed_at),
                            "last_updated_at": iso_or_none(changed_at),
                            "last_action": str(getattr(event, "action", "") or "").strip().lower(),
                            "last_actor": str(getattr(event, "changed_by", "") or "").strip(),
                        }
                    else:
                        existing["last_updated_at"] = iso_or_none(changed_at)
                        existing["last_action"] = str(getattr(event, "action", "") or "").strip().lower()
                        existing["last_actor"] = str(getattr(event, "changed_by", "") or "").strip()
                        if due_in_days is not None:
                            existing["due_in_days"] = due_in_days
                            existing["sla_status"] = sla_status
                        if str(getattr(event, "action", "") or "").strip().lower() == "resolve":
                            existing["status"] = "resolved"
                        elif str(payload.get("status") or "").strip():
                            existing["status"] = str(payload.get("status") or "").strip().lower()
                        if str(payload.get("resolution_note") or "").strip():
                            existing["resolution_note"] = str(payload.get("resolution_note") or "").strip()
                followup_rows = list(task_state_map.values())
                followup_rows = sorted(
                    followup_rows,
                    key=lambda row: (
                        0 if str(row.get("sla_status") or "") == "overdue" else (
                            1 if str(row.get("sla_status") or "") == "due_soon" else 2
                        ),
                        int(row.get("due_in_days")) if isinstance(row.get("due_in_days"), int) else 9999,
                        str(row.get("last_updated_at") or ""),
                        str(row.get("task_key") or ""),
                    ),
                    reverse=False,
                )[:25]
                if followup_rows:
                    status_options = sorted({str(row.get("status") or "").strip().lower() for row in followup_rows if str(row.get("status") or "").strip()})
                    owner_options = sorted({str(row.get("owner") or "").strip() for row in followup_rows if str(row.get("owner") or "").strip()})
                    priority_options = sorted({str(row.get("priority") or "").strip().lower() for row in followup_rows if str(row.get("priority") or "").strip()})
                    sla_options = sorted({str(row.get("sla_status") or "").strip().lower() for row in followup_rows if str(row.get("sla_status") or "").strip()})
                    st.markdown("##### Saved Task Presets")
                    preset_scope = "listings_blocker_followups"
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
                    preset_labels = ["None"] + sorted(preset_map.keys())
                    sp1, sp2, sp3, sp4 = st.columns(4)
                    with sp1:
                        selected_task_preset = st.selectbox(
                            "Task Preset",
                            options=preset_labels,
                            key="listings_blocker_followup_preset_select",
                        )
                    with sp2:
                        if st.button("Apply Task Preset", key="listings_blocker_followup_preset_apply"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                payload = preset_map.get(selected_task_preset, (None, {}))[1]
                                st.session_state["listings_blocker_followup_status_filter"] = str(payload.get("status") or "all")
                                st.session_state["listings_blocker_followup_owner_filter"] = str(payload.get("owner") or "all")
                                st.session_state["listings_blocker_followup_priority_filter"] = str(payload.get("priority") or "all")
                                st.session_state["listings_blocker_followup_sla_filter"] = str(payload.get("sla_status") or "all")
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
                                row = preset_map.get(selected_task_preset, (None, {}))[0]
                                payload = preset_map.get(selected_task_preset, (None, {}))[1]
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
                                        st.success(f"Set default task preset `{row.name}`.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Unable to set default preset: {exc}")
                        if st.button("Clear Default Preset", key="listings_blocker_followup_preset_clear_default"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                row = preset_map.get(selected_task_preset, (None, {}))[0]
                                payload = preset_map.get(selected_task_preset, (None, {}))[1]
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
                        if st.button("Delete Task Preset", key="listings_blocker_followup_preset_delete"):
                            if selected_task_preset == "None":
                                st.info("Select a task preset first.")
                            else:
                                row = preset_map.get(selected_task_preset, (None, {}))[0]
                                if row is None:
                                    st.error("Preset not found.")
                                elif str(row.username or "").strip() != user.username:
                                    st.error("Only the preset owner can delete it.")
                                else:
                                    try:
                                        repo.delete_saved_filter_profile_by_id(profile_id=row.id, actor=user.username)
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
                                "open" if "open" in status_options else "all"
                            )
                            st.session_state["listings_blocker_followup_owner_filter"] = "all"
                            st.session_state["listings_blocker_followup_priority_filter"] = (
                                "critical" if "critical" in priority_options else "all"
                            )
                            st.session_state["listings_blocker_followup_sla_filter"] = (
                                "overdue" if "overdue" in sla_options else "all"
                            )
                            st.rerun()
                    with pf2:
                        if st.button(
                            "My Open",
                            key="listings_blocker_followup_preset_my_open",
                            use_container_width=True,
                        ):
                            owner_default = user.username if user.username in owner_options else "all"
                            st.session_state["listings_blocker_followup_status_filter"] = (
                                "open" if "open" in status_options else "all"
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
                                "open" if "open" in status_options else "all"
                            )
                            st.session_state["listings_blocker_followup_owner_filter"] = "all"
                            st.session_state["listings_blocker_followup_priority_filter"] = (
                                "high" if "high" in priority_options else "all"
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

                    open_followups = [
                        row for row in filtered_followup_rows if str(row.get("status") or "").strip().lower() == "open"
                    ]
                    due_soon_followups = [
                        row
                        for row in open_followups
                        if str(row.get("sla_status") or "").strip().lower() == "due_soon"
                    ]
                    overdue_followups = [
                        row
                        for row in open_followups
                        if str(row.get("sla_status") or "").strip().lower() == "overdue"
                    ]
                    sf1, sf2, sf3 = st.columns(3)
                    sf1.metric("Open Follow-ups", int(len(open_followups)))
                    sf2.metric("Due Soon", int(len(due_soon_followups)))
                    sf3.metric("Overdue", int(len(overdue_followups)))
                    st.dataframe(pd.DataFrame(filtered_followup_rows), use_container_width=True)
                    st.download_button(
                        "Download Recent Blocker Tasks CSV",
                        data=pd.DataFrame(filtered_followup_rows).to_csv(index=False).encode("utf-8"),
                        file_name=f"listings_blocker_followups_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv",
                        key="listings_blocker_followup_recent_csv_btn",
                    )
                    open_task_map = {
                        (
                            f"{str(row.get('task_key') or '')} | owner={str(row.get('owner') or '')} | "
                            f"priority={str(row.get('priority') or '')} | due={str(row.get('due_date') or '')}"
                        ): row
                        for row in filtered_followup_rows
                        if str(row.get("status") or "").strip().lower() == "open"
                    }
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
            top_blocker_options = sorted(blocker_counts.keys())
            top_warning_options = sorted(warning_counts.keys())
            with rf3:
                readiness_blocker_filter = st.selectbox(
                    "Blocker Reason Filter",
                    options=["all"] + top_blocker_options,
                    index=0,
                    key="listings_readiness_blocker_reason_filter",
                    help="Show rows containing a specific blocker reason.",
                )
            with rf4:
                readiness_warning_filter = st.selectbox(
                    "Warning Reason Filter",
                    options=["all"] + top_warning_options,
                    index=0,
                    key="listings_readiness_warning_reason_filter",
                    help="Show rows containing a specific warning reason.",
                )
            if readiness_filter != "all":
                readiness_df = readiness_df[readiness_df["readiness_status"] == readiness_filter]
            if readiness_format_filter == "fixed":
                readiness_df = readiness_df[
                    readiness_df["format_type"].astype(str).str.upper() == "FIXED_PRICE"
                ]
            elif readiness_format_filter == "auction":
                readiness_df = readiness_df[
                    readiness_df["format_type"].astype(str).str.upper() == "AUCTION"
                ]
            if readiness_blocker_filter != "all":
                readiness_df = readiness_df[
                    readiness_df["blockers"].astype(str).str.contains(
                        str(readiness_blocker_filter),
                        case=False,
                        regex=False,
                    )
                ]
            if readiness_warning_filter != "all":
                readiness_df = readiness_df[
                    readiness_df["warnings"].astype(str).str.contains(
                        str(readiness_warning_filter),
                        case=False,
                        regex=False,
                    )
                ]
            render_table_toolbar(
                df=readiness_df,
                section_key="listings_ebay_readiness_queue",
                export_basename="ebay_readiness_queue",
                active_filters={
                    "status": readiness_filter,
                    "format": readiness_format_filter,
                    "blocker_reason": readiness_blocker_filter,
                    "warning_reason": readiness_warning_filter,
                },
            )
            st.dataframe(readiness_df, use_container_width=True)
            st.markdown("#### Bulk Review Actions")
            review_options = {
                f"#{int(row['listing_id'])} | {row.get('sku') or ''} | {row.get('title') or ''} | review={row.get('review_status') or ''}": int(row["listing_id"])
                for _, row in readiness_df.iterrows()
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

            st.markdown("#### Bulk Publish Batch Planner (Dry Run)")
            publish_candidates = {
                f"#{int(row['listing_id'])} | {row.get('sku') or ''} | {row.get('title') or ''} | "
                f"review={row.get('review_status') or ''} | readiness={row.get('readiness_status') or ''}": int(row["listing_id"])
                for _, row in readiness_df.iterrows()
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
                tag_publish_batch = st.button("Tag Publishable Listings With Batch ID", key="listings_bulk_publish_tag")
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
                    listing_by_id = {int(l.id): l for l in listings}
                    plan_rows: list[dict] = []
                    publishable_ids: list[int] = []
                    for key in selected_publish_keys:
                        listing_id = publish_candidates.get(key)
                        if listing_id is None:
                            continue
                        listing_obj = listing_by_id.get(int(listing_id))
                        if listing_obj is None:
                            continue
                        row_ref = next(
                            (row for row in readiness_rows if int(row.get("listing_id") or 0) == int(listing_id)),
                            None,
                        )
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
                                "sku": (listing_obj.product.sku if listing_obj.product else ""),
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
                        st.dataframe(plan_df, use_container_width=True)
                        if tag_publish_batch and publishable_ids:
                            tagged = 0
                            for listing_id in publishable_ids:
                                listing_obj = listing_by_id.get(int(listing_id))
                                if listing_obj is None:
                                    continue
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
                                st.success(f"Tagged {tagged} publishable listing(s) with batch `{publish_batch_id}`.")
                            else:
                                st.warning("No listings were tagged.")
                            st.rerun()
                        if execute_publish_batch:
                            ebay = EbayClient()
                            allow_sandbox_ops = get_runtime_bool(
                                repo,
                                "ebay_allow_sandbox_seller_ops",
                                bool(settings.ebay_allow_sandbox_seller_ops),
                            )
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

                            default_token = get_runtime_str(
                                repo,
                                "ebay_user_access_token",
                                settings.ebay_user_access_token,
                            ).strip()
                            if not default_token:
                                st.error("Missing eBay user access token. Set `ebay_user_access_token` first.")
                                st.stop()

                            default_marketplace_id = get_runtime_str(
                                repo,
                                "ebay_marketplace_id",
                                settings.ebay_marketplace_id,
                            ).strip()
                            default_currency = get_runtime_str(
                                repo,
                                "ebay_currency",
                                settings.ebay_currency,
                            ).strip()
                            default_content_language = get_runtime_str(
                                repo,
                                "ebay_content_language",
                                settings.ebay_content_language,
                            ).strip()
                            default_merchant_location = (
                                (default_preset.merchant_location_key if default_preset else "") or
                                get_runtime_str(repo, "ebay_merchant_location_key", settings.ebay_merchant_location_key)
                            ).strip()
                            default_payment_policy = (
                                (default_preset.payment_policy_id if default_preset else "") or
                                get_runtime_str(repo, "ebay_payment_policy_id", settings.ebay_payment_policy_id)
                            ).strip()
                            default_fulfillment_policy = (
                                (default_preset.fulfillment_policy_id if default_preset else "") or
                                get_runtime_str(repo, "ebay_fulfillment_policy_id", settings.ebay_fulfillment_policy_id)
                            ).strip()
                            default_return_policy = (
                                (default_preset.return_policy_id if default_preset else "") or
                                get_runtime_str(repo, "ebay_return_policy_id", settings.ebay_return_policy_id)
                            ).strip()
                            default_category_id = ((default_preset.category_id if default_preset else "") or "").strip()
                            if not default_category_id:
                                st.error("Default eBay category is required (set in default publish preset).")
                                st.stop()
                            if not default_merchant_location:
                                st.error("Merchant location key is required.")
                                st.stop()
                            if not (default_payment_policy and default_fulfillment_policy and default_return_policy):
                                st.error("Payment, fulfillment, and return policy IDs are required.")
                                st.stop()

                            result_rows: list[dict] = []
                            for listing_id in publishable_ids:
                                listing_obj = listing_by_id.get(int(listing_id))
                                if listing_obj is None:
                                    result_rows.append(
                                        {"listing_id": listing_id, "status": "error", "message": "Listing not found"}
                                    )
                                    continue
                                result_rows.append(
                                    _execute_batch_publish_for_listing(
                                        repo=repo,
                                        listing_obj=listing_obj,
                                        actor=user.username,
                                        batch_id=publish_batch_id,
                                        ebay=ebay,
                                        access_token=default_token,
                                        marketplace_id=default_marketplace_id,
                                        currency=default_currency,
                                        content_language=default_content_language,
                                        merchant_location_key=default_merchant_location,
                                        payment_policy_id=default_payment_policy,
                                        fulfillment_policy_id=default_fulfillment_policy,
                                        return_policy_id=default_return_policy,
                                        category_id=default_category_id,
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
                                st.dataframe(result_df, use_container_width=True)
                            if not result_df.empty and int((result_df["status"] == "success").sum()) > 0:
                                st.rerun()
        else:
            st.info("No eBay listings found for readiness checks.")

        st.markdown("### Listing Orchestration Queue")
        orchestration_rows: list[dict] = []
        for row in readiness_rows:
            orchestration_status = orchestration_status_for_listing(
                adapters=channel_adapters,
                channel_key="ebay",
                listing_status=str(row.get("status") or ""),
                readiness_status=str(row.get("readiness_status") or ""),
                external_listing_id=str(row.get("external_listing_id") or ""),
            )
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
            orchestration_filter = st.selectbox(
                "Orchestration Status Filter",
                options=["all", "ready", "blocked", "published", "error"],
                index=1,
                key="listings_orchestration_filter",
            )
            if orchestration_filter != "all":
                orchestration_df = orchestration_df[
                    orchestration_df["orchestration_status"] == orchestration_filter
                ]
            render_table_toolbar(
                df=orchestration_df,
                section_key="listings_orchestration_queue",
                export_basename="listings_orchestration_queue",
                active_filters={"status": orchestration_filter},
            )
            st.dataframe(orchestration_df, use_container_width=True)
        else:
            st.info("No orchestration rows available.")

        st.markdown("### Bulk Publish Execution History")
        history_rows: list[dict] = []
        for listing in listings:
            details_raw = (listing.marketplace_details or "").strip()
            if not details_raw:
                continue
            try:
                details_obj = json.loads(details_raw)
            except Exception:
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
                        "listing_id": int(listing.id),
                        "sku": listing.product.sku if listing.product else "",
                        "title": listing.listing_title,
                        "offer_id": str(event.get("offer_id") or ""),
                        "external_listing_id": str(event.get("listing_id") or ""),
                        "marketplace_url": listing.marketplace_url or "",
                        "status": str(event.get("status") or "success"),
                        "message": str(event.get("message") or ""),
                    }
                )
        if history_rows:
            history_df = pd.DataFrame(history_rows).sort_values("executed_at", ascending=False)
            h1, h2 = st.columns(2)
            with h1:
                batch_filter = st.text_input("Filter Batch ID", value="", key="listings_batch_history_filter")
            with h2:
                actor_filter = st.text_input("Filter Executor", value="", key="listings_batch_history_actor_filter")
            if batch_filter.strip():
                history_df = history_df[
                    history_df["batch_id"].astype(str).str.lower().str.contains(batch_filter.strip().lower())
                ]
            if actor_filter.strip():
                history_df = history_df[
                    history_df["executed_by"].astype(str).str.lower().str.contains(actor_filter.strip().lower())
                ]
            render_table_toolbar(
                df=history_df,
                section_key="listings_bulk_publish_history",
                export_basename="listings_bulk_publish_history",
                active_filters={"batch": batch_filter.strip(), "actor": actor_filter.strip()},
            )
            st.dataframe(history_df, use_container_width=True)

            failed_df = history_df[history_df["status"].astype(str).str.lower() == "error"].copy()
            if not failed_df.empty:
                st.markdown("#### Retry Failed History Rows")
                retry_map = {
                    f"#{int(row['listing_id'])} | {row.get('batch_id') or ''} | {row.get('executed_at') or ''} | {row.get('message') or ''}": int(row["listing_id"])
                    for _, row in failed_df.iterrows()
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
                        allow_sandbox_ops = get_runtime_bool(
                            repo,
                            "ebay_allow_sandbox_seller_ops",
                            bool(settings.ebay_allow_sandbox_seller_ops),
                        )
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
                        default_token = get_runtime_str(
                            repo,
                            "ebay_user_access_token",
                            settings.ebay_user_access_token,
                        ).strip()
                        default_marketplace_id = get_runtime_str(
                            repo,
                            "ebay_marketplace_id",
                            settings.ebay_marketplace_id,
                        ).strip()
                        default_currency = get_runtime_str(
                            repo,
                            "ebay_currency",
                            settings.ebay_currency,
                        ).strip()
                        default_content_language = get_runtime_str(
                            repo,
                            "ebay_content_language",
                            settings.ebay_content_language,
                        ).strip()
                        default_merchant_location = (
                            (default_preset.merchant_location_key if default_preset else "") or
                            get_runtime_str(repo, "ebay_merchant_location_key", settings.ebay_merchant_location_key)
                        ).strip()
                        default_payment_policy = (
                            (default_preset.payment_policy_id if default_preset else "") or
                            get_runtime_str(repo, "ebay_payment_policy_id", settings.ebay_payment_policy_id)
                        ).strip()
                        default_fulfillment_policy = (
                            (default_preset.fulfillment_policy_id if default_preset else "") or
                            get_runtime_str(repo, "ebay_fulfillment_policy_id", settings.ebay_fulfillment_policy_id)
                        ).strip()
                        default_return_policy = (
                            (default_preset.return_policy_id if default_preset else "") or
                            get_runtime_str(repo, "ebay_return_policy_id", settings.ebay_return_policy_id)
                        ).strip()
                        default_category_id = ((default_preset.category_id if default_preset else "") or "").strip()
                        listing_by_id = {int(l.id): l for l in listings}
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
                                    access_token=default_token,
                                    marketplace_id=default_marketplace_id,
                                    currency=default_currency,
                                    content_language=default_content_language,
                                    merchant_location_key=default_merchant_location,
                                    payment_policy_id=default_payment_policy,
                                    fulfillment_policy_id=default_fulfillment_policy,
                                    return_policy_id=default_return_policy,
                                    category_id=default_category_id,
                                )
                            )
                        retry_df = pd.DataFrame(retry_rows)
                        if not retry_df.empty:
                            st.dataframe(retry_df, use_container_width=True)
                        st.rerun()
        else:
            st.info("No bulk publish execution history yet.")

        st.markdown("### Channel Capability Matrix")
        capability_rows = capability_matrix_rows(channel_adapters)
        st.dataframe(pd.DataFrame(capability_rows), use_container_width=True)

    with panel_col:
        st.markdown("#### Listing Detail/Edit")
        if not filtered_rows:
            st.info("No filtered listings available.")
        else:
            listing_index = {l.id: l for l in listings}
            select_options = {
                f"#{row['id']} | {row['marketplace']} | {row['title']}": int(row["id"]) for row in filtered_rows
            }
            selected_label = st.selectbox(
                "Select Listing",
                options=list(select_options.keys()),
                key="listings_side_panel_select",
            )
            selected_listing = listing_index[select_options[selected_label]]
            media_count = len(selected_listing.media_assets or [])
            linked_product = repo.db.get(Product, int(selected_listing.product_id)) if selected_listing.product_id else None

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
            related_sales = [
                sale
                for sale in repo.list_sales()
                if sale.listing_id is not None and int(sale.listing_id) == int(selected_listing.id)
            ]
            order_ids_from_sales = {
                int(sale.order_id)
                for sale in related_sales
                if sale.order_id is not None
            }
            related_order_items = [
                item
                for item in repo.list_order_items()
                if item.listing_id is not None and int(item.listing_id) == int(selected_listing.id)
            ]
            order_ids_from_items = {int(item.order_id) for item in related_order_items if item.order_id is not None}
            related_order_ids = order_ids_from_sales | order_ids_from_items
            order_index = {int(order.id): order for order in repo.list_orders()}
            related_orders = [
                order_index[oid]
                for oid in sorted(related_order_ids)
                if int(oid) in order_index
            ]
            dd1, dd2 = st.columns([2, 1])
            with dd1:
                listing_doc_type = st.selectbox(
                    "Document Type",
                    options=["invoice", "receipt"],
                    index=0,
                    key=f"listing_documents_doc_type_{selected_listing.id}",
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
            for sale in sorted(related_sales, key=lambda s: (s.sold_at or datetime.min, s.id), reverse=True):
                label = (
                    f"Sale #{int(sale.id)} | {str(sale.marketplace or '').strip()} | "
                    f"{str(sale.external_order_id or '').strip() or 'no-ext-id'} | "
                    f"gross=${float(sale.sold_price or 0):,.2f}"
                )
                source_options[label] = ("Sale", int(sale.id))
            for order in sorted(related_orders, key=lambda o: (o.sold_at or datetime.min, o.id), reverse=True):
                label = (
                    f"Order #{int(order.id)} | {str(order.marketplace or '').strip()} | "
                    f"{str(order.external_order_id or '').strip() or 'no-ext-id'} | "
                    f"total=${float(order.total_amount or 0):,.2f}"
                )
                if label not in source_options:
                    source_options[label] = ("Order", int(order.id))
            if source_options:
                selected_source_label = st.selectbox(
                    "Document Source",
                    options=list(source_options.keys()),
                    key=f"listing_documents_source_pick_{selected_listing.id}",
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
                        ["draft", "active", "ended"],
                        index=["draft", "active", "ended"].index(selected_listing.listing_status)
                        if selected_listing.listing_status in {"draft", "active", "ended"}
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
            try:
                details_obj = json.loads((selected_listing.marketplace_details or "").strip() or "{}")
                history_rows = details_obj.get("review_history", [])
                if isinstance(history_rows, list) and history_rows:
                    st.dataframe(
                        pd.DataFrame(history_rows).sort_values("reviewed_at", ascending=False),
                        use_container_width=True,
                    )
                else:
                    st.caption("No review history yet.")
            except Exception:
                st.caption("No review history yet.")

    st.markdown("### Listing Media Manager")
    if not listings:
        st.info("No listings available.")
        return

    listing_map = {
        f"#{l.id} | {l.marketplace} | {l.listing_title}": l for l in listings
    }
    listing_key = st.selectbox("Choose Listing", list(listing_map.keys()), key="manage_listing_key")
    selected_listing = listing_map[listing_key]

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

    listing_media = repo.list_media_assets_for_listing(selected_listing.id)
    if not listing_media:
        st.info("No media currently attached to this listing.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
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
            ),
            use_container_width=True,
        )
        render_media_gallery(
            listing_media,
            section_title="Listing Media Preview Gallery",
            columns=3,
            storage=storage,
        )
        render_media_file_actions(
            listing_media,
            storage=storage,
            key_prefix=f"listing_media_file_actions_{selected_listing.id}",
            section_title="Listing Media File Access",
            repo=repo,
            actor=user.username,
            user=user,
        )

    st.markdown("### Publish Selected Listing To eBay")
    st.caption(
        "Creates/updates eBay inventory item, creates offer, and publishes listing. "
        "On success, this updates external listing ID and URL on the selected listing."
    )
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

    product = repo.db.get(Product, selected_listing.product_id)
    if product is None:
        st.error(f"Product #{selected_listing.product_id} not found for selected listing.")
        return

    default_description = (
        (product.description or "").strip()
        or (selected_listing.marketplace_details or "").strip()
        or selected_listing.listing_title
    )
    preset_rows = repo.list_ebay_publish_presets(
        environment=settings.app_env,
        username=user.username,
        active_only=True,
    )
    image_media_items = [m for m in listing_media if m.media_type == "image"]
    video_media_items = [m for m in listing_media if m.media_type == "video"]
    image_options = {
        f"#{m.id} | {m.original_filename}": m for m in image_media_items
    }
    video_options = {
        f"#{m.id} | {m.original_filename}": m for m in video_media_items
    }
    publish_formats = ["FIXED_PRICE", "AUCTION"]
    auction_durations = ["DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"]
    condition_options = ["NEW", "LIKE_NEW", "USED_EXCELLENT", "USED_VERY_GOOD", "USED_GOOD", "USED_ACCEPTABLE"]
    default_token = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    default_marketplace_id = get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id).strip()
    default_currency = get_runtime_str(repo, "ebay_currency", settings.ebay_currency).strip()
    default_content_language = get_runtime_str(
        repo,
        "ebay_content_language",
        settings.ebay_content_language,
    ).strip()
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
    default_best_offer_enabled = get_runtime_bool(
        repo,
        "ebay_best_offer_default",
        False,
    )
    default_auction_start = _to_float(get_runtime_str(repo, "ebay_auction_start_default", "1.0"), 1.0)
    default_auction_reserve = _to_float(get_runtime_str(repo, "ebay_auction_reserve_default", "0.0"), 0.0)
    default_auction_buy_now = _to_float(get_runtime_str(repo, "ebay_auction_buy_now_default", "0.0"), 0.0)

    defaults = {
        "ebay_pub_format": "FIXED_PRICE",
        "ebay_pub_auction_duration": "DAYS_7",
        "ebay_pub_best_offer_enabled": bool(default_best_offer_enabled),
        "ebay_pub_qty": max(1, int(selected_listing.quantity_listed or 1)),
        "ebay_pub_condition": "NEW",
        "ebay_pub_category_id": "",
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
        "ebay_pub_upload_video_to_ebay": False,
        "ebay_pub_selected_images": list(image_options.keys()),
        "ebay_pub_selected_video": "None",
    }
    for state_key, state_value in defaults.items():
        if state_key not in st.session_state:
            st.session_state[state_key] = state_value
    valid_image_labels = set(image_options.keys())
    st.session_state["ebay_pub_selected_images"] = [
        label
        for label in st.session_state.get("ebay_pub_selected_images", [])
        if label in valid_image_labels
    ] or list(image_options.keys())
    valid_video_labels = {"None"} | set(video_options.keys())
    if st.session_state.get("ebay_pub_selected_video") not in valid_video_labels:
        st.session_state["ebay_pub_selected_video"] = "None"

    st.markdown("#### eBay Publish Presets")
    preset_map = {f"#{p.id} | {p.name}{' (default)' if p.is_default else ''}": p for p in preset_rows}
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
        st.session_state["ebay_pub_format"] = preset.format_type
        st.session_state["ebay_pub_auction_duration"] = preset.listing_duration or "DAYS_7"
        st.session_state["ebay_pub_condition"] = preset.condition_value or "NEW"
        st.session_state["ebay_pub_category_id"] = preset.category_id or ""
        st.session_state["ebay_pub_merchant_location_key"] = preset.merchant_location_key or ""
        st.session_state["ebay_pub_payment_policy_id"] = preset.payment_policy_id or ""
        st.session_state["ebay_pub_fulfillment_policy_id"] = preset.fulfillment_policy_id or ""
        st.session_state["ebay_pub_return_policy_id"] = preset.return_policy_id or ""
        st.session_state["ebay_pub_marketplace_id"] = preset.marketplace_id or default_marketplace_id
        st.session_state["ebay_pub_currency"] = preset.currency or default_currency
        st.session_state["ebay_pub_content_language"] = (
            preset.content_language or default_content_language
        )
        if st.session_state["ebay_pub_format"] != "FIXED_PRICE":
            st.session_state["ebay_pub_best_offer_enabled"] = False
        st.success(f"Applied preset `{preset.name}`.")
        st.rerun()

    if save_preset:
        if not ensure_permission(user, "create", "Save eBay Publish Preset"):
            st.stop()
        if not (preset_name or "").strip():
            st.error("Preset name is required.")
        else:
            existing = next((p for p in preset_rows if p.name.lower() == preset_name.strip().lower()), None)
            duration_value = (
                "GTC"
                if st.session_state.get("ebay_pub_format") == "FIXED_PRICE"
                else st.session_state.get("ebay_pub_auction_duration", "DAYS_7")
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
                        name=preset_name.strip(),
                        actor=user.username,
                        **payload,
                    )
                else:
                    repo.update_ebay_publish_preset(existing.id, payload, actor=user.username)
                st.success(f"Preset `{preset_name.strip()}` saved.")
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
        st.session_state["ebay_pub_format"] = "FIXED_PRICE"
        st.session_state["ebay_pub_best_offer_enabled"] = bool(
            get_runtime_bool(repo, "ebay_best_offer_default", False)
        )
        st.session_state["ebay_pub_fixed_price"] = max(0.01, float(selected_listing.listing_price or 0.0))
        st.session_state["ebay_pub_marketplace_id"] = str(
            st.session_state.get("ebay_workspace_store_marketplace_id_input")
            or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
            or default_marketplace_id
        ).strip()
        st.session_state["ebay_pub_currency"] = str(
            st.session_state.get("ebay_workspace_store_currency_input")
            or get_runtime_str(repo, "ebay_currency", settings.ebay_currency)
            or default_currency
        ).strip()
        st.session_state["ebay_pub_content_language"] = str(
            st.session_state.get("ebay_workspace_store_content_language_input")
            or get_runtime_str(repo, "ebay_content_language", settings.ebay_content_language)
            or default_content_language
        ).strip()
        st.session_state["ebay_pub_merchant_location_key"] = str(
            st.session_state.get("ebay_workspace_store_merchant_location_key_input")
            or get_runtime_str(repo, "ebay_merchant_location_key", settings.ebay_merchant_location_key)
            or default_merchant_location
        ).strip()
        st.session_state["ebay_pub_payment_policy_id"] = str(
            st.session_state.get("ebay_workspace_store_payment_policy_id_input")
            or get_runtime_str(repo, "ebay_payment_policy_id", settings.ebay_payment_policy_id)
            or default_payment_policy
        ).strip()
        st.session_state["ebay_pub_fulfillment_policy_id"] = str(
            st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input")
            or get_runtime_str(repo, "ebay_fulfillment_policy_id", settings.ebay_fulfillment_policy_id)
            or default_fulfillment_policy
        ).strip()
        st.session_state["ebay_pub_return_policy_id"] = str(
            st.session_state.get("ebay_workspace_store_return_policy_id_input")
            or get_runtime_str(repo, "ebay_return_policy_id", settings.ebay_return_policy_id)
            or default_return_policy
        ).strip()
        st.session_state["ebay_pub_category_id"] = str(
            st.session_state.get("ebay_workspace_store_category_id_input")
            or get_runtime_str(repo, "ebay_category_id", "")
            or ""
        ).strip()
        st.success("Applied fixed-price template defaults.")
        st.rerun()

    if apply_auction_template:
        st.session_state["ebay_pub_format"] = "AUCTION"
        st.session_state["ebay_pub_best_offer_enabled"] = False
        st.session_state["ebay_pub_auction_duration"] = str(
            st.session_state.get("ebay_workspace_store_auction_duration_input")
            or get_runtime_str(repo, "ebay_auction_duration_default", "DAYS_7")
            or "DAYS_7"
        ).strip()
        st.session_state["ebay_pub_auction_start"] = float(
            st.session_state.get("ebay_workspace_store_auction_start_input")
            or _to_float(get_runtime_str(repo, "ebay_auction_start_default", "1.0"), 1.0)
        )
        st.session_state["ebay_pub_auction_reserve"] = float(
            st.session_state.get("ebay_workspace_store_auction_reserve_input")
            or _to_float(get_runtime_str(repo, "ebay_auction_reserve_default", "0.0"), 0.0)
        )
        st.session_state["ebay_pub_auction_buy_now"] = float(
            st.session_state.get("ebay_workspace_store_auction_buy_now_input")
            or _to_float(get_runtime_str(repo, "ebay_auction_buy_now_default", "0.0"), 0.0)
        )
        st.session_state["ebay_pub_marketplace_id"] = str(
            st.session_state.get("ebay_workspace_store_marketplace_id_input")
            or get_runtime_str(repo, "ebay_marketplace_id", settings.ebay_marketplace_id)
            or default_marketplace_id
        ).strip()
        st.session_state["ebay_pub_currency"] = str(
            st.session_state.get("ebay_workspace_store_currency_input")
            or get_runtime_str(repo, "ebay_currency", settings.ebay_currency)
            or default_currency
        ).strip()
        st.session_state["ebay_pub_content_language"] = str(
            st.session_state.get("ebay_workspace_store_content_language_input")
            or get_runtime_str(repo, "ebay_content_language", settings.ebay_content_language)
            or default_content_language
        ).strip()
        st.session_state["ebay_pub_merchant_location_key"] = str(
            st.session_state.get("ebay_workspace_store_merchant_location_key_input")
            or get_runtime_str(repo, "ebay_merchant_location_key", settings.ebay_merchant_location_key)
            or default_merchant_location
        ).strip()
        st.session_state["ebay_pub_payment_policy_id"] = str(
            st.session_state.get("ebay_workspace_store_payment_policy_id_input")
            or get_runtime_str(repo, "ebay_payment_policy_id", settings.ebay_payment_policy_id)
            or default_payment_policy
        ).strip()
        st.session_state["ebay_pub_fulfillment_policy_id"] = str(
            st.session_state.get("ebay_workspace_store_fulfillment_policy_id_input")
            or get_runtime_str(repo, "ebay_fulfillment_policy_id", settings.ebay_fulfillment_policy_id)
            or default_fulfillment_policy
        ).strip()
        st.session_state["ebay_pub_return_policy_id"] = str(
            st.session_state.get("ebay_workspace_store_return_policy_id_input")
            or get_runtime_str(repo, "ebay_return_policy_id", settings.ebay_return_policy_id)
            or default_return_policy
        ).strip()
        st.session_state["ebay_pub_category_id"] = str(
            st.session_state.get("ebay_workspace_store_category_id_input")
            or get_runtime_str(repo, "ebay_category_id", "")
            or ""
        ).strip()
        st.success("Applied auction template defaults.")
        st.rerun()

    with st.form("publish_ebay_listing_form"):
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
            )

        d1, d2 = st.columns(2)
        with d1:
            category_id = st.text_input("eBay Category ID", key="ebay_pub_category_id")
        with d2:
            listing_duration = (
                "GTC"
                if publish_format == "FIXED_PRICE"
                else st.selectbox("Auction Duration", auction_durations, key="ebay_pub_auction_duration")
            )
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
            auction_start_price = 0.0
            auction_reserve_price = 0.0
            auction_buy_now_price = 0.0
        else:
            best_offer_enabled = False
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

        listing_description = st.text_area(
            "Listing Description",
            height=160,
            key="ebay_pub_description",
        )
        preview_sanitized_html = st.checkbox(
            "Preview sanitized HTML description",
            value=False,
            help="Shows the exact sanitized HTML that will be sent to eBay.",
            key="ebay_pub_preview_sanitized_html",
        )
        sanitized_preview, sanitize_preview_notes = _sanitize_listing_html(listing_description)
        if sanitize_preview_notes:
            st.warning("Sanitization adjustments: " + "; ".join(sanitize_preview_notes))
        if preview_sanitized_html:
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
                "Upload one MP4 video to eBay and attach",
                key="ebay_pub_upload_video_to_ebay",
                help="eBay supports one video per listing.",
            )
        if image_options:
            selected_image_labels = st.multiselect(
                "Images for eBay listing",
                options=list(image_options.keys()),
                key="ebay_pub_selected_images",
            )
        else:
            selected_image_labels = []
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
        c1, c2, c3 = st.columns(3)
        with c1:
            st.text_input("Marketplace ID", key="ebay_pub_marketplace_id")
        with c2:
            st.text_input("Currency", key="ebay_pub_currency")
        with c3:
            st.text_input("Content Language", key="ebay_pub_content_language")
        access_token = st.text_area(
            "User Access Token",
            height=120,
            help="Defaults to `EBAY_USER_ACCESS_TOKEN` if set.",
            key="ebay_pub_access_token",
        )

        submit_publish = st.form_submit_button("Publish To eBay", disabled=sandbox_seller_ops_blocked)

    discovered_offer_id = ""
    try:
        details_raw = (selected_listing.marketplace_details or "").strip()
        if details_raw:
            details_obj = json.loads(details_raw)
            if isinstance(details_obj, dict):
                publish_meta = details_obj.get("ebay_publish") or {}
                if isinstance(publish_meta, dict):
                    discovered_offer_id = str(publish_meta.get("offer_id") or "").strip()
    except Exception:
        discovered_offer_id = ""

    effective_listing_description, sanitize_notes = _sanitize_listing_html(listing_description)
    listing_html_errors = _validate_listing_html(effective_listing_description)
    if sanitize_notes:
        st.info(
            "Listing description was sanitized before eBay operations: "
            + "; ".join(sanitize_notes)
        )

    st.markdown("#### Manage Existing eBay Listing")
    st.caption("Revise, end, or relist existing eBay-linked listings from this app.")
    with st.form("manage_ebay_listing_actions_form"):
        a1, a2 = st.columns(2)
        with a1:
            manage_offer_id = st.text_input(
                "Offer ID",
                value=discovered_offer_id,
                help="Autodetected from marketplace details when available.",
            )
        with a2:
            manage_action = st.selectbox("Action", ["revise", "end", "relist"])
        manage_submit = st.form_submit_button("Run eBay Listing Action", disabled=sandbox_seller_ops_blocked)

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

        effective_offer_id = (manage_offer_id or "").strip()
        if not effective_offer_id and (selected_listing.external_listing_id or "").strip():
            try:
                offers_payload = ebay.get_offers(access_token=token_to_use, sku=product.sku)
                offers = offers_payload.get("offers") or []
                for offer in offers:
                    offer_listing_id = str(offer.get("listingId") or "").strip()
                    if offer_listing_id and offer_listing_id == (selected_listing.external_listing_id or "").strip():
                        effective_offer_id = str(offer.get("offerId") or "").strip()
                        break
            except Exception:
                pass

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
                publish_result = ebay.publish_offer(access_token=token_to_use, offer_id=effective_offer_id)
                listing_id = str(
                    publish_result.get("listingId") or selected_listing.external_listing_id or ""
                ).strip()
                updates = {"listing_status": "active"}
                if listing_id:
                    updates["external_listing_id"] = listing_id
                    updates["marketplace_url"] = ebay.listing_url_for_id(listing_id)
                repo.update_listing(selected_listing.id, updates, actor=user.username)
                st.success(f"Relisted eBay offer `{effective_offer_id}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Relist failed: {exc}")
                st.stop()

        if manage_action == "revise":
            if listing_html_errors:
                st.error("Listing description failed validation: " + " | ".join(listing_html_errors))
                st.stop()
            if publish_format == "FIXED_PRICE" and float(fixed_price or 0) <= 0:
                st.error("Buy It Now price must be greater than 0.")
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
            if not category_id.strip():
                st.error("Category ID is required for revise.")
                st.stop()
            if not merchant_location_key.strip():
                st.error("Merchant Location Key is required for revise.")
                st.stop()
            if not payment_policy_id.strip() or not fulfillment_policy_id.strip() or not return_policy_id.strip():
                st.error("Payment, fulfillment, and return policy IDs are required for revise.")
                st.stop()

            selected_images = [image_options[label] for label in selected_image_labels if label in image_options]
            image_urls = []
            for media in selected_images:
                original_url = (media.s3_url or "").strip()
                if not original_url or not original_url.startswith("https://"):
                    st.error(f"Image `{media.original_filename}` requires public HTTPS URL for revise.")
                    st.stop()
                if use_eps_images:
                    try:
                        image_result = ebay.create_image_from_url(access_token=token_to_use, image_url=original_url)
                        eps_url = (image_result.get("imageUrl") or "").strip()
                        if not eps_url:
                            raise RuntimeError("No imageUrl returned from eBay Media API.")
                        image_urls.append(eps_url)
                    except Exception as exc:
                        st.error(f"Image EPS upload failed for `{media.original_filename}`: {exc}")
                        st.stop()
                else:
                    image_urls.append(original_url)

            if not image_urls:
                st.error("At least one image is required to revise listing.")
                st.stop()
            if len(image_urls) > 24:
                image_urls = image_urls[:24]

            inventory_payload = {
                "availability": {"shipToLocationAvailability": {"quantity": int(available_quantity)}},
                "condition": condition,
                "product": {
                    "title": selected_listing.listing_title,
                    "description": effective_listing_description or selected_listing.listing_title,
                    "imageUrls": image_urls,
                },
            }
            _maybe_add_package_data(inventory_payload, product)

            revise_offer_payload = {
                "sku": product.sku,
                "marketplaceId": (st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
                "format": publish_format,
                "availableQuantity": int(available_quantity),
                "categoryId": category_id.strip(),
                "merchantLocationKey": merchant_location_key.strip(),
                "listingDescription": effective_listing_description or selected_listing.listing_title,
                "listingDuration": listing_duration,
                "listingPolicies": {
                    "paymentPolicyId": payment_policy_id.strip(),
                    "fulfillmentPolicyId": fulfillment_policy_id.strip(),
                    "returnPolicyId": return_policy_id.strip(),
                },
                "pricingSummary": {},
            }
            currency = (st.session_state.get("ebay_pub_currency") or default_currency).strip()
            local_listing_price = float(selected_listing.listing_price)
            if publish_format == "FIXED_PRICE":
                revise_offer_payload["pricingSummary"]["price"] = {
                    "value": str(round(float(fixed_price), 2)),
                    "currency": currency,
                }
                if bool(best_offer_enabled):
                    revise_offer_payload.setdefault("listingPolicies", {})
                    revise_offer_payload["listingPolicies"]["bestOfferTerms"] = {"bestOfferEnabled": True}
                local_listing_price = float(fixed_price)
            else:
                revise_offer_payload["pricingSummary"]["auctionStartPrice"] = {
                    "value": str(round(float(auction_start_price), 2)),
                    "currency": currency,
                }
                local_listing_price = float(auction_start_price)
                if float(auction_reserve_price) > 0:
                    revise_offer_payload["pricingSummary"]["auctionReservePrice"] = {
                        "value": str(round(float(auction_reserve_price), 2)),
                        "currency": currency,
                    }
                if float(auction_buy_now_price) > 0:
                    revise_offer_payload["pricingSummary"]["price"] = {
                        "value": str(round(float(auction_buy_now_price), 2)),
                        "currency": currency,
                    }
                    local_listing_price = float(auction_buy_now_price)

            try:
                ebay.create_or_replace_inventory_item(
                    access_token=token_to_use,
                    sku=product.sku,
                    payload=inventory_payload,
                    content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                )
                ebay.update_offer(
                    access_token=token_to_use,
                    offer_id=effective_offer_id,
                    payload=revise_offer_payload,
                    content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
                )
                repo.update_listing(
                    selected_listing.id,
                    {
                        "quantity_listed": int(available_quantity),
                        "listing_price": to_decimal(local_listing_price),
                        "listing_status": "active",
                    },
                    actor=user.username,
                )
                st.success(f"Revised eBay offer `{effective_offer_id}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Revise listing failed: {exc}")
                st.stop()

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
    if listing_html_errors:
        st.error("Listing description failed validation: " + " | ".join(listing_html_errors))
        return
    if publish_format == "FIXED_PRICE" and float(fixed_price or 0) <= 0:
        st.error("Buy It Now price must be greater than 0.")
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
    if not category_id.strip():
        st.error("Category ID is required.")
        return
    if not merchant_location_key.strip():
        st.error("Merchant Location Key is required.")
        return
    if not payment_policy_id.strip() or not fulfillment_policy_id.strip() or not return_policy_id.strip():
        st.error("Payment, fulfillment, and return policy IDs are required.")
        return

    selected_images = [image_options[label] for label in selected_image_labels if label in image_options]
    image_urls = []
    eps_uploads: list[dict] = []
    for media in selected_images:
        original_url = (media.s3_url or "").strip()
        if not original_url or not original_url.startswith("https://"):
            st.error(
                f"Image `{media.original_filename}` does not have an HTTPS URL. "
                "Use public HTTPS media URLs for eBay publish."
            )
            return
        if use_eps_images:
            try:
                image_result = ebay.create_image_from_url(access_token=token_to_use, image_url=original_url)
                eps_url = (image_result.get("imageUrl") or "").strip()
                if not eps_url:
                    raise RuntimeError("No imageUrl returned from eBay Media API.")
                image_urls.append(eps_url)
                eps_uploads.append(
                    {
                        "media_asset_id": media.id,
                        "filename": media.original_filename,
                        "source_url": original_url,
                        "eps_url": eps_url,
                    }
                )
            except Exception as exc:
                st.error(f"eBay EPS image upload failed for `{media.original_filename}`: {exc}")
                return
        else:
            image_urls.append(original_url)

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
    if upload_video_to_ebay and selected_video_label != "None":
        selected_video = video_options.get(selected_video_label)
        if selected_video is None:
            st.error("Selected video was not found.")
            return
        filename_lower = (selected_video.original_filename or "").lower()
        content_type_lower = (selected_video.content_type or "").lower()
        if not (filename_lower.endswith(".mp4") or content_type_lower == "video/mp4"):
            st.error("Only MP4 video upload is currently supported for eBay video attach.")
            return
        try:
            video_bytes, video_content_type = _read_media_bytes(selected_video, storage)
            video_id = ebay.create_video(
                access_token=token_to_use,
                title=selected_video.original_filename or selected_listing.listing_title,
                size_bytes=len(video_bytes),
                description=selected_listing.listing_title,
            )
            ebay.upload_video(
                access_token=token_to_use,
                video_id=video_id,
                file_bytes=video_bytes,
                content_type=video_content_type or "video/mp4",
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
                "filename": selected_video.original_filename,
                "video_id": video_id,
                "status": final_status,
            }
        except Exception as exc:
            st.error(f"eBay video upload failed: {exc}")
            return

    inventory_payload = {
        "availability": {
            "shipToLocationAvailability": {"quantity": int(available_quantity)}
        },
        "condition": condition,
        "product": {
            "title": selected_listing.listing_title,
            "description": effective_listing_description or selected_listing.listing_title,
            "imageUrls": image_urls[:24],
        },
    }
    if video_ids:
        inventory_payload["product"]["videoIds"] = video_ids
    _maybe_add_package_data(inventory_payload, product)

    offer_payload = {
        "sku": product.sku,
        "marketplaceId": (st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
        "format": publish_format,
        "availableQuantity": int(available_quantity),
        "categoryId": category_id.strip(),
        "merchantLocationKey": merchant_location_key.strip(),
        "listingDescription": effective_listing_description or selected_listing.listing_title,
        "listingDuration": listing_duration,
        "listingPolicies": {
            "paymentPolicyId": payment_policy_id.strip(),
            "fulfillmentPolicyId": fulfillment_policy_id.strip(),
            "returnPolicyId": return_policy_id.strip(),
        },
        "pricingSummary": {},
    }
    currency = (st.session_state.get("ebay_pub_currency") or default_currency).strip()
    if publish_format == "FIXED_PRICE":
        offer_payload["pricingSummary"]["price"] = {
            "value": str(round(float(fixed_price), 2)),
            "currency": currency,
        }
        if bool(best_offer_enabled):
            offer_payload.setdefault("listingPolicies", {})
            offer_payload["listingPolicies"]["bestOfferTerms"] = {"bestOfferEnabled": True}
    else:
        offer_payload["pricingSummary"]["auctionStartPrice"] = {
            "value": str(round(float(auction_start_price), 2)),
            "currency": currency,
        }
        if float(auction_reserve_price) > 0:
            offer_payload["pricingSummary"]["auctionReservePrice"] = {
                "value": str(round(float(auction_reserve_price), 2)),
                "currency": currency,
            }
        if float(auction_buy_now_price) > 0:
            offer_payload["pricingSummary"]["price"] = {
                "value": str(round(float(auction_buy_now_price), 2)),
                "currency": currency,
            }

    try:
        ebay.create_or_replace_inventory_item(
            access_token=token_to_use,
            sku=product.sku,
            payload=inventory_payload,
            content_language=(st.session_state.get("ebay_pub_content_language") or default_content_language).strip(),
        )
        offer_result = ebay.create_offer(access_token=token_to_use, payload=offer_payload)
        offer_id = str(offer_result.get("offerId") or "").strip()
        if not offer_id:
            raise RuntimeError(f"eBay createOffer did not return offerId. payload={offer_result}")
        publish_result = ebay.publish_offer(access_token=token_to_use, offer_id=offer_id)
        listing_id = str(publish_result.get("listingId") or "").strip()
        if not listing_id:
            raise RuntimeError(f"eBay publishOffer did not return listingId. payload={publish_result}")

        listing_url = ebay.listing_url_for_id(listing_id)
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
        details_obj["ebay_publish"] = {
            "format": publish_format,
            "listing_duration": listing_duration,
            "best_offer_enabled": bool(best_offer_enabled) if publish_format == "FIXED_PRICE" else False,
            "auction_start_price": float(auction_start_price or 0) if publish_format == "AUCTION" else 0.0,
            "auction_reserve_price": float(auction_reserve_price or 0) if publish_format == "AUCTION" else 0.0,
            "auction_buy_now_price": float(auction_buy_now_price or 0) if publish_format == "AUCTION" else 0.0,
            "offer_id": offer_id,
            "marketplace_id": (st.session_state.get("ebay_pub_marketplace_id") or default_marketplace_id).strip(),
            "published_at": utcnow_naive().isoformat(),
            "image_source": "ebay_eps" if use_eps_images else "direct_https_urls",
            "image_count": len(image_urls),
            "video_attached": bool(video_ids),
        }
        if eps_uploads:
            details_obj["ebay_publish"]["eps_uploads"] = eps_uploads
        if uploaded_video_info:
            details_obj["ebay_publish"]["video_upload"] = uploaded_video_info
        repo.update_listing(
            selected_listing.id,
            {
                "external_listing_id": listing_id,
                "marketplace_url": listing_url,
                "listing_status": "active",
                "marketplace_details": json.dumps(details_obj, indent=2),
                "quantity_listed": int(available_quantity),
            },
            actor=user.username,
        )
        st.success(f"Published to eBay. listing_id={listing_id}, offer_id={offer_id}")
        st.link_button("Open eBay Listing URL", listing_url)
        st.rerun()
    except Exception as exc:
        st.error(f"eBay publish failed: {exc}")
