import re
import json
import io
import base64
from html import unescape
from pathlib import Path
from typing import Any
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from urllib.parse import parse_qs, quote_plus, urlparse, unquote

import pandas as pd
import requests
import streamlit as st

from app.auth import current_user, ensure_permission, has_permission
from app.config import settings
from app.repository import InventoryRepository
from app.services.media_storage import MediaStorageService
from app.services.ebay import EbayClient
from app.services.llm_runtime import (
    resolve_comp_llm_runtime_chain,
    resolve_comp_llm_runtime_config,
)
from app.services.ai_orchestration import execute_comp_summary, execute_multimodal_task
from app.services.ai_text import (
    coin_grader_structured_to_text,
    normalize_ai_text,
    parse_coin_grader_structured,
)
from app.services.ebay_fee_estimator import calculate_ebay_fee_profit_estimate, resolve_product_known_unit_cost
from app.services.runtime_settings import (
    get_runtime_bool,
    get_runtime_float,
    get_runtime_int,
    get_runtime_str,
    is_ai_domain_enabled,
)
from app.services.spot_price import (
    SpotPriceService,
    SpotRateLimitError,
    grams_to_troy_oz,
    troy_oz_to_grams,
)
from app.components.views.shared import render_help_panel
from app.components.views.workspace_shell import render_workspace_feedback
from app.components.views.shared import generate_sku

DEFAULT_COMP_DEALER_DOMAINS: tuple[str, ...] = (
    "apmex.com",
    "jmbullion.com",
    "sdbullion.com",
    "monumentmetals.com",
    "providentmetals.com",
    "boldpreciousmetals.com",
    "bgasc.com",
    "goldeneaglecoin.com",
    "scottsdalemint.com",
    "silvergoldbull.com",
    "moneymetals.com",
    "bullionexchanges.com",
    "herobullion.com",
    "silver.com",
    "kitco.com",
    "usgoldbureau.com",
    "libertycoin.com",
)

COIN_REF_CSV_FIELD_ALIASES: dict[str, str] = {
    "coin_name": "coin_name",
    "name": "coin_name",
    "country": "country",
    "issuer": "issuer",
    "denomination": "denomination",
    "series": "series",
    "year_start": "year_start",
    "year_from": "year_start",
    "year_end": "year_end",
    "year_to": "year_end",
    "mint_mark": "mint_mark",
    "mint": "mint_mark",
    "composition": "composition",
    "metal_type": "metal_type",
    "metal": "metal_type",
    "weight_grams": "weight_grams",
    "weight_g": "weight_grams",
    "asw_oz": "asw_oz",
    "diameter_mm": "diameter_mm",
    "thickness_mm": "thickness_mm",
    "km_number": "km_number",
    "km": "km_number",
    "pcgs_no": "pcgs_no",
    "pcgs": "pcgs_no",
    "ngc_id": "ngc_id",
    "ngc": "ngc_id",
    "mintage": "mintage",
    "estimated_value_low": "estimated_value_low",
    "value_low": "estimated_value_low",
    "estimated_value_high": "estimated_value_high",
    "value_high": "estimated_value_high",
    "price_source": "price_source",
    "source_url": "source_url",
    "tags": "tags",
    "obverse_description": "obverse_description",
    "reverse_description": "reverse_description",
    "notes": "notes",
    "is_active": "is_active",
}


def _coin_csv_normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map: dict[str, str] = {}
    for col in list(df.columns):
        normalized = str(col or "").strip().lower().replace(" ", "_")
        mapped = COIN_REF_CSV_FIELD_ALIASES.get(normalized)
        if mapped:
            rename_map[col] = mapped
    return df.rename(columns=rename_map)


def _coin_csv_cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    raw = str(value).strip()
    if raw.lower() in {"nan", "none", "null"}:
        return ""
    return raw


def _coin_csv_cell_int(value: Any) -> int | None:
    raw = _coin_csv_cell_str(value)
    if not raw:
        return None
    try:
        return int(float(raw))
    except Exception:
        return None


def _coin_csv_cell_decimal(value: Any) -> Decimal | None:
    raw = _coin_csv_cell_str(value)
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "")
    try:
        out = Decimal(cleaned)
        if out < 0:
            return None
        return out
    except Exception:
        return None


def _coin_csv_cell_bool(value: Any, *, default: bool = True) -> bool:
    raw = _coin_csv_cell_str(value).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _try_extract_json_object(raw_text: str) -> dict[str, Any]:
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
        snippet = text[first:last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _render_grader_summary_text(summary_text: str) -> None:
    text = str(summary_text or "").strip()
    if not text:
        return
    # Render as plain text so markdown parsing cannot corrupt model output.
    st.markdown("##### Grader Summary")
    st.code(text, language="text")


def _coin_ref_match_key_from_parts(
    *,
    coin_name: str,
    country: str,
    series: str,
    year_start: int | None,
    mint_mark: str,
) -> str:
    return "|".join(
        [
            (coin_name or "").strip().lower(),
            (country or "").strip().lower(),
            (series or "").strip().lower(),
            str(int(year_start)) if year_start is not None else "",
            (mint_mark or "").strip().lower(),
        ]
    )


def _coin_ref_payload_from_csv_row(row: pd.Series) -> dict[str, Any]:
    coin_name = _coin_csv_cell_str(row.get("coin_name"))
    country = _coin_csv_cell_str(row.get("country"))
    series = _coin_csv_cell_str(row.get("series"))
    year_start = _coin_csv_cell_int(row.get("year_start"))
    payload: dict[str, Any] = {
        "coin_name": coin_name,
        "country": country,
        "issuer": _coin_csv_cell_str(row.get("issuer")),
        "denomination": _coin_csv_cell_str(row.get("denomination")),
        "series": series,
        "year_start": year_start,
        "year_end": _coin_csv_cell_int(row.get("year_end")),
        "mint_mark": _coin_csv_cell_str(row.get("mint_mark")),
        "composition": _coin_csv_cell_str(row.get("composition")),
        "metal_type": _coin_csv_cell_str(row.get("metal_type")),
        "weight_grams": _coin_csv_cell_decimal(row.get("weight_grams")),
        "asw_oz": _coin_csv_cell_decimal(row.get("asw_oz")),
        "diameter_mm": _coin_csv_cell_decimal(row.get("diameter_mm")),
        "thickness_mm": _coin_csv_cell_decimal(row.get("thickness_mm")),
        "km_number": _coin_csv_cell_str(row.get("km_number")),
        "pcgs_no": _coin_csv_cell_str(row.get("pcgs_no")),
        "ngc_id": _coin_csv_cell_str(row.get("ngc_id")),
        "mintage": _coin_csv_cell_str(row.get("mintage")),
        "estimated_value_low": _coin_csv_cell_decimal(row.get("estimated_value_low")),
        "estimated_value_high": _coin_csv_cell_decimal(row.get("estimated_value_high")),
        "price_source": _coin_csv_cell_str(row.get("price_source")),
        "source_url": _coin_csv_cell_str(row.get("source_url")),
        "tags": _coin_csv_cell_str(row.get("tags")),
        "obverse_description": _coin_csv_cell_str(row.get("obverse_description")),
        "reverse_description": _coin_csv_cell_str(row.get("reverse_description")),
        "notes": _coin_csv_cell_str(row.get("notes")),
        "is_active": _coin_csv_cell_bool(row.get("is_active"), default=True),
    }
    payload["_match_key"] = _coin_ref_match_key_from_parts(
        coin_name=coin_name,
        country=country,
        series=series,
        year_start=year_start,
        mint_mark=payload["mint_mark"],
    )
    return payload


def _parse_domain_csv(value: str) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_COMP_DEALER_DOMAINS
    domains: list[str] = []
    for token in raw.replace("\n", ",").split(","):
        clean = token.strip().lower()
        if not clean:
            continue
        if clean.startswith("https://") or clean.startswith("http://"):
            clean = urlparse(clean).netloc.lower() or clean
        clean = clean.lstrip("www.")
        if clean and clean not in domains:
            domains.append(clean)
    return tuple(domains) if domains else DEFAULT_COMP_DEALER_DOMAINS




def _money_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    raw = str(value).strip().replace("$", "").replace(",", "")
    if not raw:
        return Decimal("0.00")
    try:
        return Decimal(raw)
    except Exception:
        return Decimal("0.00")


def calculate_ebay_fee_estimate(
    *,
    sale_price: Any,
    buyer_shipping_charged: Any = 0,
    sales_tax_collected: Any = 0,
    item_cost: Any = 0,
    shipping_label_cost: Any = 0,
    packaging_cost: Any = 0,
    final_value_fee_percent: Any = 13.25,
    fixed_order_fee: Any | None = None,
    promoted_ad_percent: Any = 0,
    additional_fee_percent: Any = 0,
    insertion_or_upgrade_fee: Any = 0,
    include_sales_tax_in_fee_basis: bool = True,
) -> dict[str, Decimal]:
    return calculate_ebay_fee_profit_estimate(
        sale_price=sale_price,
        buyer_shipping_charged=buyer_shipping_charged,
        sales_tax_collected=sales_tax_collected,
        item_cost=item_cost,
        shipping_label_cost=shipping_label_cost,
        packaging_cost=packaging_cost,
        final_value_fee_percent=final_value_fee_percent,
        fixed_order_fee=fixed_order_fee,
        promoted_ad_percent=promoted_ad_percent,
        additional_fee_percent=additional_fee_percent,
        insertion_or_upgrade_fee=insertion_or_upgrade_fee,
        include_sales_tax_in_fee_basis=include_sales_tax_in_fee_basis,
    )


def _money_text(value: Any) -> str:
    return f"${float(_money_decimal(value)):,.2f}"


def _product_known_unit_cost(product: Any) -> Decimal:
    return resolve_product_known_unit_cost(product)


def _render_ebay_fee_calculator(repo: InventoryRepository) -> None:
    st.caption(
        "Estimate eBay fees, shipping economics, cost basis, and profit before posting or repricing. "
        "Rates are editable because eBay fees vary by category, store subscription, seller performance, "
        "listing upgrades, ads, and order details."
    )
    st.info(
        "eBay describes final value fees as a percentage of the total sale amount plus a per-order fee. "
        "The total sale amount can include item price, buyer-paid shipping/handling, sales tax, and other applicable fees. "
        "Use this as a pricing estimate and reconcile actual sales against imported eBay finance entries."
    )

    with st.expander("Load Cost From Inventory Product", expanded=False):
        ps1, ps2 = st.columns([2, 1])
        with ps1:
            product_search = st.text_input(
                "Search product by SKU, title, category, metal, or ID",
                key="ebay_fee_calc_product_search",
                placeholder="Optional",
            )
        with ps2:
            product_limit = st.number_input(
                "Result limit",
                min_value=5,
                max_value=100,
                value=25,
                step=5,
                key="ebay_fee_calc_product_limit",
            )
        try:
            product_rows = repo.list_products(
                search_query=str(product_search or "").strip() or None,
                limit=int(product_limit or 25),
            )
        except Exception as exc:
            product_rows = []
            st.warning(f"Unable to load products for fee calculator: {exc}")
        if product_rows:
            product_options = {
                f"#{int(getattr(row, 'id', 0) or 0)} | {getattr(row, 'sku', '')} | {getattr(row, 'title', '')}": row
                for row in product_rows
            }
            selected_product_label = st.selectbox(
                "Product",
                options=list(product_options.keys()),
                key="ebay_fee_calc_product_pick",
            )
            selected_product = product_options.get(selected_product_label)
            selected_product_cost = _product_known_unit_cost(selected_product)
            st.caption(
                f"Resolved product cost: {_money_text(selected_product_cost)}. "
                "Uses `product_cost` first, then landed acquisition fields."
            )
            if st.button("Use Product Cost In Calculator", key="ebay_fee_calc_apply_product_cost"):
                st.session_state["ebay_fee_calc_item_cost"] = float(selected_product_cost)
                st.success(f"Applied {_money_text(selected_product_cost)} to Item cost / COGS.")
        else:
            st.caption("No matching products found.")

    c1, c2, c3 = st.columns(3)
    with c1:
        sale_price = st.number_input(
            "Sale price",
            min_value=0.0,
            value=100.0,
            step=1.0,
            format="%.2f",
            key="ebay_fee_calc_sale_price",
        )
        item_cost = st.number_input(
            "Item cost / COGS",
            min_value=0.0,
            value=50.0,
            step=1.0,
            format="%.2f",
            key="ebay_fee_calc_item_cost",
        )
        buyer_shipping_charged = st.number_input(
            "Buyer shipping charged",
            min_value=0.0,
            value=0.0,
            step=0.5,
            format="%.2f",
            key="ebay_fee_calc_shipping_charged",
        )
    with c2:
        shipping_label_cost = st.number_input(
            "Shipping label cost",
            min_value=0.0,
            value=5.0,
            step=0.5,
            format="%.2f",
            key="ebay_fee_calc_label_cost",
        )
        packaging_cost = st.number_input(
            "Packaging / handling cost",
            min_value=0.0,
            value=0.75,
            step=0.25,
            format="%.2f",
            key="ebay_fee_calc_packaging_cost",
        )
        sales_tax_collected = st.number_input(
            "Sales tax collected by marketplace",
            min_value=0.0,
            value=0.0,
            step=0.5,
            format="%.2f",
            key="ebay_fee_calc_sales_tax",
        )
    with c3:
        final_value_fee_percent = st.number_input(
            "Final value fee %",
            min_value=0.0,
            max_value=100.0,
            value=float(get_runtime_float(repo, "ebay_fee_estimate_final_value_rate_percent", 13.25)),
            step=0.05,
            format="%.2f",
            key="ebay_fee_calc_fvf_pct",
        )
        fixed_order_fee = st.number_input(
            "Fixed per-order fee",
            min_value=0.0,
            value=float(get_runtime_float(repo, "ebay_fee_estimate_final_value_fixed_per_order_usd", 0.30)),
            step=0.05,
            format="%.2f",
            key="ebay_fee_calc_fixed_fee",
        )
        promoted_ad_percent = st.number_input(
            "Promoted listing ad rate %",
            min_value=0.0,
            max_value=100.0,
            value=float(get_runtime_float(repo, "ebay_fee_estimate_promoted_rate_percent", 0.0)),
            step=0.25,
            format="%.2f",
            key="ebay_fee_calc_ad_pct",
        )

    a1, a2, a3 = st.columns(3)
    with a1:
        additional_fee_percent = st.number_input(
            "Additional fee / surcharge %",
            min_value=0.0,
            max_value=100.0,
            value=float(get_runtime_float(repo, "ebay_fee_estimate_payment_rate_percent", 0.0)),
            step=0.25,
            format="%.2f",
            key="ebay_fee_calc_additional_pct",
            help="Use for international fees, below-standard seller surcharges, or another percentage fee.",
        )
    with a2:
        insertion_or_upgrade_fee = st.number_input(
            "Insertion / upgrade / fixed surcharge",
            min_value=0.0,
            value=float(get_runtime_float(repo, "ebay_fee_estimate_payment_fixed_per_order_usd", 0.0)),
            step=0.25,
            format="%.2f",
            key="ebay_fee_calc_upgrade_fee",
        )
    with a3:
        include_sales_tax_in_fee_basis = st.checkbox(
            "Include sales tax in fee basis",
            value=True,
            key="ebay_fee_calc_include_tax",
        )

    estimate = calculate_ebay_fee_estimate(
        sale_price=sale_price,
        buyer_shipping_charged=buyer_shipping_charged,
        sales_tax_collected=sales_tax_collected,
        item_cost=item_cost,
        shipping_label_cost=shipping_label_cost,
        packaging_cost=packaging_cost,
        final_value_fee_percent=final_value_fee_percent,
        fixed_order_fee=fixed_order_fee,
        promoted_ad_percent=promoted_ad_percent,
        additional_fee_percent=additional_fee_percent,
        insertion_or_upgrade_fee=insertion_or_upgrade_fee,
        include_sales_tax_in_fee_basis=bool(include_sales_tax_in_fee_basis),
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Estimated eBay Fees", _money_text(estimate["estimated_total_fees"]))
    m2.metric("Net Before COGS", _money_text(estimate["net_before_cogs"]))
    m3.metric("Estimated Profit", _money_text(estimate["estimated_profit"]))
    m4.metric("Margin", f"{estimate['margin_percent']:,.2f}%")
    st.caption(
        f"Estimated sale price needed to break even with these assumptions: "
        f"{_money_text(estimate['breakeven_sale_price'])}."
    )

    breakdown_rows = [
        {"Component": "Sale price", "Amount": estimate["sale_price"]},
        {"Component": "Buyer shipping charged", "Amount": estimate["buyer_shipping_charged"]},
        {"Component": "Sales tax collected", "Amount": estimate["sales_tax_collected"]},
        {"Component": "Fee basis", "Amount": estimate["fee_basis"]},
        {"Component": "Final value fee", "Amount": -estimate["final_value_fee"]},
        {"Component": "Fixed order fee", "Amount": -estimate["fixed_order_fee"]},
        {"Component": "Promoted listing ad fee", "Amount": -estimate["promoted_ad_fee"]},
        {"Component": "Additional fee / surcharge", "Amount": -estimate["additional_fee"]},
        {"Component": "Insertion / upgrade fee", "Amount": -estimate["insertion_or_upgrade_fee"]},
        {"Component": "Shipping label cost", "Amount": -estimate["shipping_label_cost"]},
        {"Component": "Packaging / handling cost", "Amount": -estimate["packaging_cost"]},
        {"Component": "Item cost / COGS", "Amount": -estimate["item_cost"]},
        {"Component": "Estimated profit", "Amount": estimate["estimated_profit"]},
    ]
    breakdown_df = pd.DataFrame(
        [{"Component": row["Component"], "Amount": float(row["Amount"])} for row in breakdown_rows]
    )
    st.dataframe(breakdown_df, use_container_width=True, hide_index=True)
    st.download_button(
        "Download Fee Estimate CSV",
        data=breakdown_df.to_csv(index=False).encode("utf-8"),
        file_name="ebay_fee_profit_estimate.csv",
        mime="text/csv",
        key="download_ebay_fee_estimate_csv",
    )


def _tokenize_query(value: str) -> list[str]:
    raw = " ".join((value or "").replace("-", " ").replace("_", " ").split()).strip()
    if not raw:
        return []
    tokens = [t for t in raw.split(" ") if len(t) >= 2]
    # preserve order while deduplicating
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(token)
    return out


def _query_variants(value: str) -> list[str]:
    tokens = _tokenize_query(value)
    if not tokens:
        return []
    variants = [" ".join(tokens)]
    if len(tokens) > 4:
        variants.append(" ".join(tokens[:4]))
    if len(tokens) > 3:
        variants.append(" ".join(tokens[:3]))
    if len(tokens) > 2:
        variants.append(" ".join(tokens[:2]))
    # remove duplicates while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for query in variants:
        q = query.strip()
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)
    return deduped


def _uploaded_image_to_bytes(uploaded: Any) -> tuple[bytes | None, str]:
    if uploaded is None:
        return None, ""
    try:
        payload = uploaded.getvalue()
    except Exception:
        return None, ""
    if not payload:
        return None, ""
    content_type = str(getattr(uploaded, "type", "") or "").strip().lower()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return payload, content_type


def _uploaded_file_name(uploaded: Any) -> str:
    if uploaded is None:
        return ""
    return str(getattr(uploaded, "name", "") or "").strip()


def _parse_photo_comp_retry_preset(raw_payload: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw_payload or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _build_inventory_mode_query(
    *,
    selected_product,
    use_title: bool,
    use_sku: bool,
    use_metal: bool,
    prefill_query: str,
    prefill_apply_once: bool,
) -> str:
    query_parts: list[str] = []
    if use_title:
        query_parts.append(str(getattr(selected_product, "title", "") or "").strip())
    if use_sku:
        query_parts.append(str(getattr(selected_product, "sku", "") or "").strip())
    if use_metal:
        query_parts.append(str(getattr(selected_product, "metal_type", "") or "").strip())
    query = " ".join([p for p in query_parts if p]).strip()
    if prefill_apply_once and str(prefill_query or "").strip():
        query = str(prefill_query or "").strip()
    return query


def _generate_comp_query_from_hint_image(
    *,
    repo: InventoryRepository,
    uploaded_file: Any,
    manual_hint: str = "",
) -> tuple[str, dict[str, Any], str]:
    if uploaded_file is None:
        return "", {}, ""
    system_message = get_runtime_str(
        repo,
        "comp_llm_system_message",
        "You are a resale pricing analyst. Provide concise markdown.",
    ).strip()
    instruction = (
        "Analyze this product image and return ONLY JSON with keys: "
        "`query_keywords`, `item_summary`, `condition_hint`. "
        "Keep `query_keywords` concise and optimized for marketplace comp searches."
    )
    if str(manual_hint or "").strip():
        instruction += f"\nOperator hint: {str(manual_hint or '').strip()}"
    mm_result = execute_multimodal_task(
        repo,
        tool_name="comp_image_query_seed",
        system_message=system_message,
        instruction=instruction,
        image_bytes=uploaded_file.getvalue(),
        image_content_type=str(uploaded_file.type or "image/jpeg"),
        max_output_tokens_override=500,
        context={"hint_file_name": str(uploaded_file.name or "")},
    )
    payload = _try_extract_json_object(mm_result.text)
    generated_query = ""
    if payload:
        query_keywords = str(payload.get("query_keywords") or "").strip()
        item_summary = str(payload.get("item_summary") or "").strip()
        condition_hint = str(payload.get("condition_hint") or "").strip()
        generated_query = " ".join(
            [part for part in [query_keywords, item_summary, condition_hint] if part]
        ).strip()
    return generated_query, payload, str(mm_result.text or "").strip()


def _media_type_from_content_type(content_type: str) -> str:
    lowered = (content_type or "").strip().lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("video/"):
        return "video"
    return "other"


def _persist_ai_input_media(
    *,
    repo: InventoryRepository,
    storage: MediaStorageService,
    files: list[tuple[bytes, str, str]],
    product_id: int | None,
    listing_id: int | None,
    uploaded_by: str,
) -> tuple[int, list[str]]:
    if not files or not storage.enabled:
        return 0, []
    uploaded = 0
    errors: list[str] = []
    for payload, content_type, filename in files:
        if not payload:
            continue
        try:
            result = storage.upload_file(
                file_name=filename or "ai_input.jpg",
                file_bytes=payload,
                content_type=content_type or "application/octet-stream",
            )
            repo.create_media_asset(
                media_type=_media_type_from_content_type(result.content_type),
                original_filename=filename or "ai_input.jpg",
                content_type=result.content_type,
                size_bytes=result.size_bytes,
                s3_bucket=result.bucket,
                s3_key=result.key,
                s3_url=result.url,
                product_id=product_id,
                listing_id=listing_id,
                uploaded_by=uploaded_by,
            )
            uploaded += 1
        except Exception as exc:
            errors.append(str(exc))
    return uploaded, errors


def _extract_first_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1).strip())
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    brace_match = re.search(r"(\{.*\})", raw, flags=re.DOTALL)
    if brace_match:
        try:
            parsed = json.loads(brace_match.group(1).strip())
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _looks_like_truncated_json_output(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    # Common truncation patterns: unterminated JSON object or abrupt key cutoff.
    open_braces = raw.count("{")
    close_braces = raw.count("}")
    if open_braces > close_braces:
        return True
    if raw.endswith(',"') or raw.endswith(":") or raw.endswith(',"den') or raw.endswith('"den'):
        return True
    return False


def _repair_json_object_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    # Keep the first object-like region only.
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidate = raw[start : end + 1]
    elif start >= 0:
        candidate = raw[start:]
    else:
        candidate = raw

    candidate = candidate.strip()
    if not candidate.startswith("{"):
        candidate = "{" + candidate

    # If quotes are unbalanced, close the final quote.
    quote_count = candidate.count('"')
    if quote_count % 2 == 1:
        candidate += '"'

    # Remove obvious trailing separators.
    candidate = re.sub(r"[,\s:]+$", "", candidate)

    # Balance braces.
    open_braces = candidate.count("{")
    close_braces = candidate.count("}")
    if close_braces < open_braces:
        candidate += "}" * (open_braces - close_braces)

    return candidate


def _extract_or_repair_first_json_object(text: str) -> dict[str, Any]:
    parsed = _extract_first_json_object(text)
    if parsed:
        return parsed
    repaired = _repair_json_object_text(text)
    if not repaired:
        return {}
    try:
        payload = json.loads(repaired)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _effective_total_price(row: dict) -> float:
    total = float(row.get("total_price") or 0.0)
    if total > 0:
        return total
    listed = float(row.get("listed_price") or 0.0)
    shipping = float(row.get("shipping_cost") or 0.0)
    return listed + shipping


def _comp_csv_money(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        if pd.isna(value):
            return 0.0
    except Exception:
        pass
    raw = str(value or "").strip()
    if not raw or raw.lower() in {"nan", "none", "null", "--"}:
        return 0.0
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = (
        raw.replace("$", "")
        .replace(",", "")
        .replace("USD", "")
        .replace("US", "")
        .replace("(", "")
        .replace(")", "")
        .strip()
    )
    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", cleaned)
    if not match:
        return 0.0
    try:
        amount = float(match.group(0))
    except Exception:
        return 0.0
    if negative and amount > 0:
        amount *= -1
    return max(0.0, amount)


def _comp_csv_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    raw = str(value or "").strip()
    if raw.lower() in {"nan", "none", "null"}:
        return ""
    return raw


def _normalize_comp_csv_column(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _first_present_csv_col(column_map: dict[str, str], candidates: list[str]) -> str:
    for candidate in candidates:
        key = _normalize_comp_csv_column(candidate)
        if key in column_map:
            return column_map[key]
    return ""


def _parse_ebay_product_research_csv(data: bytes | str | None) -> list[dict]:
    if data is None:
        return []
    try:
        raw_bytes = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    except Exception:
        return []
    if not raw_bytes:
        return []
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes), dtype=str, encoding="utf-8-sig")
    except Exception:
        try:
            df = pd.read_csv(io.BytesIO(raw_bytes), dtype=str)
        except Exception:
            return []
    if df.empty:
        return []
    column_map = {_normalize_comp_csv_column(col): col for col in df.columns}
    title_col = _first_present_csv_col(
        column_map,
        ["title", "item title", "listing title", "name", "product title"],
    )
    sold_col = _first_present_csv_col(
        column_map,
        ["sold price", "sale price", "item price", "price", "avg sold price"],
    )
    total_col = _first_present_csv_col(
        column_map,
        ["total price", "total", "order total", "sold total", "item total"],
    )
    shipping_col = _first_present_csv_col(
        column_map,
        ["shipping", "shipping cost", "shipping price", "postage"],
    )
    url_col = _first_present_csv_col(
        column_map,
        ["item url", "listing url", "url", "view item url", "link"],
    )
    item_id_col = _first_present_csv_col(
        column_map,
        ["item id", "itemid", "listing id", "legacy item id"],
    )
    date_col = _first_present_csv_col(
        column_map,
        ["sold date", "date sold", "sale date", "end date", "date"],
    )
    condition_col = _first_present_csv_col(column_map, ["condition", "item condition"])
    currency_col = _first_present_csv_col(column_map, ["currency", "currency id"])
    rows: list[dict] = []
    for _, source_row in df.iterrows():
        sold_price = _comp_csv_money(source_row.get(sold_col)) if sold_col else 0.0
        shipping_cost = _comp_csv_money(source_row.get(shipping_col)) if shipping_col else 0.0
        total_price = _comp_csv_money(source_row.get(total_col)) if total_col else 0.0
        if total_price <= 0 and sold_price > 0:
            total_price = sold_price + shipping_cost
        if sold_price <= 0 and total_price > 0:
            sold_price = max(0.0, total_price - shipping_cost)
        if total_price <= 0:
            continue
        rows.append(
            {
                "item_id": _comp_csv_text(source_row.get(item_id_col)) if item_id_col else "",
                "title": _comp_csv_text(source_row.get(title_col)) if title_col else "",
                "sold_price": sold_price,
                "shipping_cost": shipping_cost,
                "total_price": total_price,
                "currency": (_comp_csv_text(source_row.get(currency_col)) if currency_col else "USD") or "USD",
                "condition": _comp_csv_text(source_row.get(condition_col)) if condition_col else "",
                "end_time": _comp_csv_text(source_row.get(date_col)) if date_col else "",
                "view_url": _comp_csv_text(source_row.get(url_col)) if url_col else "",
                "gallery_url": "",
                "source": "ebay_product_research",
                "evidence": "sold_market",
            }
        )
    return rows


def _extract_target_weight_oz(text: str) -> float:
    raw = str(text or "").lower()
    patterns = [
        r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:troy\s*)?oz\b",
        r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:troy\s*)?ounces?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except Exception:
            continue
        if value > 0:
            return value
    return 0.0


def _comp_row_relevance(query: str, row: dict) -> dict[str, Any]:
    target_weight = _extract_target_weight_oz(query)
    title = str(row.get("title") or "")
    snippet = str(row.get("snippet") or "")
    url = str(row.get("view_url") or "")
    title_blob = f"{title} {url}".lower()
    full_blob = f"{title} {snippet} {url}".lower()
    flags: list[str] = []
    score = 1.0
    if target_weight > 0:
        title_weight = _extract_target_weight_oz(title_blob)
        full_weight = _extract_target_weight_oz(full_blob)
        if title_weight > 0 and abs(title_weight - target_weight) > 0.05:
            score -= 0.65
            flags.append("weight_mismatch")
        elif title_weight <= 0 and full_weight <= 0:
            score -= 0.35
            flags.append("target_weight_missing")
        elif title_weight <= 0 and full_weight > 0:
            score -= 0.20
            flags.append("target_weight_not_in_title")
    generic_terms = [
        "for sale | ebay",
        "bullion bars for sale",
        "buy monarch silver bullion bars",
        "silver-bullion/silver-bars",
    ]
    if any(term in full_blob for term in generic_terms):
        score -= 0.30
        flags.append("generic_category_page")
    if len(title.strip()) < 12:
        score -= 0.15
        flags.append("thin_title")
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 4),
        "flags": flags,
        "target_weight_oz": target_weight,
    }


def _representative_price(prices: list[float]) -> float:
    vals = sorted(float(p) for p in prices if float(p) > 0)
    if not vals:
        return 0.0
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def _comp_stats(rows: list[dict]) -> dict[str, float]:
    if not rows:
        return {"count": 0, "avg": 0.0, "median": 0.0, "low": 0.0, "high": 0.0}
    prices = sorted(_effective_total_price(r) for r in rows)
    count = len(prices)
    mid = count // 2
    median = prices[mid] if count % 2 == 1 else (prices[mid - 1] + prices[mid]) / 2
    return {
        "count": float(count),
        "avg": sum(prices) / count,
        "median": median,
        "low": min(prices),
        "high": max(prices),
    }


def _qualified_comp_rows(rows: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    priced = [row for row in rows if _effective_total_price(row) > 0.0]
    relevance_excluded = [
        row
        for row in priced
        if float(row.get("relevance_score", 1.0) or 0.0) < 0.55
    ]
    relevance_ok = [
        row
        for row in priced
        if float(row.get("relevance_score", 1.0) or 0.0) >= 0.55
    ]
    candidate_priced = relevance_ok if len(relevance_ok) >= 3 else priced
    if len(priced) < 4:
        return candidate_priced, {
            "method": "all_priced_lt4",
            "priced_rows": len(priced),
            "qualified_rows": len(candidate_priced),
            "removed_rows": max(0, len(priced) - len(candidate_priced)),
            "low_cut": 0.0,
            "high_cut": 0.0,
            "relevance_removed_rows": len(relevance_excluded) if candidate_priced is not priced else 0,
            "removed_samples": _comp_removed_sample_rows(relevance_excluded if candidate_priced is not priced else []),
        }
    prices = sorted(_effective_total_price(row) for row in candidate_priced)
    median = _representative_price(prices)
    low_cut = max(0.0, median * 0.45)
    high_cut = median * 1.85
    qualified = [
        row for row in candidate_priced if low_cut <= _effective_total_price(row) <= high_cut
    ]
    if len(qualified) < 3:
        qualified = candidate_priced
        method = "all_priced_guardrail_min3"
    else:
        method = "median_band_45_185_pct"
    qualified_ids = {id(row) for row in qualified}
    removed = [row for row in priced if id(row) not in qualified_ids]
    removed_samples = _comp_removed_sample_rows(removed)
    return qualified, {
        "method": method,
        "priced_rows": len(priced),
        "qualified_rows": len(qualified),
        "removed_rows": max(0, len(priced) - len(qualified)),
        "low_cut": round(low_cut, 2),
        "high_cut": round(high_cut, 2),
        "relevance_removed_rows": len(relevance_excluded) if len(relevance_ok) >= 3 else 0,
        "removed_samples": removed_samples,
    }


def _comp_removed_sample_rows(rows: list[dict]) -> list[dict[str, Any]]:
    return [
        {
            "price": round(_effective_total_price(row), 2),
            "domain": str(row.get("domain") or "").strip(),
            "title": str(row.get("title") or "").strip()[:160],
            "view_url": str(row.get("view_url") or "").strip(),
            "price_confidence_score": float(row.get("price_confidence_score") or 0.0),
            "price_confidence_label": str(row.get("price_confidence_label") or "").strip(),
            "relevance_score": float(row.get("relevance_score", 1.0) or 0.0),
            "relevance_flags": str(row.get("relevance_flags") or ""),
        }
        for row in rows[:10]
    ]


def _comp_cost_breakdown(rows: list[dict]) -> dict[str, float]:
    if not rows:
        return {
            "sold_avg": 0.0,
            "listed_avg": 0.0,
            "item_avg": 0.0,
            "shipping_avg": 0.0,
            "total_avg": 0.0,
            "shipping_pct_of_total": 0.0,
        }
    sold_prices = [float(r.get("sold_price") or 0.0) for r in rows]
    listed_prices = [float(r.get("listed_price") or 0.0) for r in rows]
    item_prices = [
        float(r.get("sold_price") or 0.0) if float(r.get("sold_price") or 0.0) > 0 else float(r.get("listed_price") or 0.0)
        for r in rows
    ]
    shipping_prices = [float(r.get("shipping_cost") or 0.0) for r in rows]
    total_prices = [_effective_total_price(r) for r in rows]
    sold_non_zero = [p for p in sold_prices if p > 0]
    listed_non_zero = [p for p in listed_prices if p > 0]
    item_avg = sum(item_prices) / len(item_prices)
    shipping_avg = sum(shipping_prices) / len(shipping_prices)
    total_avg = sum(total_prices) / len(total_prices)
    shipping_pct = (shipping_avg / total_avg * 100.0) if total_avg > 0 else 0.0
    return {
        "sold_avg": (sum(sold_non_zero) / len(sold_non_zero)) if sold_non_zero else 0.0,
        "listed_avg": (sum(listed_non_zero) / len(listed_non_zero)) if listed_non_zero else 0.0,
        "item_avg": item_avg,
        "shipping_avg": shipping_avg,
        "total_avg": total_avg,
        "shipping_pct_of_total": shipping_pct,
    }


def _comp_evidence_quality(rows: list[dict], web_rows: list[dict]) -> dict[str, Any]:
    sold_rows = [row for row in rows if float(row.get("sold_price") or 0.0) > 0.0]
    web_priced_rows = [row for row in web_rows if _effective_total_price(row) > 0.0]
    web_high_confidence_rows = [
        row
        for row in web_priced_rows
        if float(row.get("price_confidence_score") or 0.0) >= 0.65
        and float(row.get("relevance_score", 1.0) or 0.0) >= 0.55
    ]
    web_active_market_rows = [
        row
        for row in web_rows
        if str(row.get("source") or "").strip().lower() == "web"
        and str(row.get("domain") or "").strip().lower()
    ]
    dealer_rows = [
        row
        for row in web_rows
        if str(row.get("search_scope") or "").strip().lower() == "configured_dealer"
    ]
    if sold_rows:
        label = "sold_market"
        note = "Pricing metrics are based on sold eBay comparable rows."
    elif web_high_confidence_rows:
        label = "active_market_priced"
        note = (
            "No sold eBay rows were available; pricing metrics use priced active/listed web evidence. "
            "Treat confidence as medium at most until sold comps are available."
        )
    elif web_priced_rows:
        label = "active_market_low_confidence"
        note = "No sold eBay rows were available; priced web evidence should be treated as active/listed-market guidance."
    elif web_active_market_rows:
        label = "research_links_only"
        note = "No sold or priced rows were available; rows are research links only and should not drive pricing."
    else:
        label = "no_evidence"
        note = "No comparable evidence was returned."
    return {
        "label": label,
        "note": note,
        "sold_rows": len(sold_rows),
        "web_rows": len(web_rows or []),
        "web_priced_rows": len(web_priced_rows),
        "web_high_confidence_rows": len(web_high_confidence_rows),
        "configured_dealer_rows": len(dealer_rows),
    }


def _filter_ai_web_comp_rows(web_rows: list[dict]) -> list[dict]:
    return [
        row
        for row in (web_rows or [])
        if _effective_total_price(row) > 0.0
        or str(row.get("price_confidence_label") or "").strip().lower() in {"medium", "high"}
        or str(row.get("search_scope") or "").strip().lower() == "configured_dealer"
    ]


def _comp_quality_diagnostics(
    attempts: list[dict],
    rows: list[dict],
    web_rows: list[dict],
) -> dict[str, Any]:
    web_priced_by_domain: dict[str, int] = {}
    web_unpriced_by_domain: dict[str, int] = {}
    for row in web_rows or []:
        domain = str(row.get("domain") or "").strip().lower() or "(unknown)"
        if _effective_total_price(row) > 0.0:
            web_priced_by_domain[domain] = int(web_priced_by_domain.get(domain, 0) + 1)
        else:
            web_unpriced_by_domain[domain] = int(web_unpriced_by_domain.get(domain, 0) + 1)
    top_priced_domains = sorted(web_priced_by_domain.items(), key=lambda kv: int(kv[1]), reverse=True)[:8]
    top_unpriced_domains = sorted(web_unpriced_by_domain.items(), key=lambda kv: int(kv[1]), reverse=True)[:8]
    ebay_notes = [
        str(attempt.get("note") or "")
        for attempt in attempts or []
        if str(attempt.get("note") or "").strip().lower().startswith("ebay_")
    ]
    retry_actions: list[str] = []
    if not rows:
        retry_actions.append("Use broader item terms or production eBay sold/research data for true sold comps.")
    if web_rows and not web_priced_by_domain:
        retry_actions.append("Increase Web Detail Fetch Limit or use a specific dealer/domain include filter.")
    if top_unpriced_domains:
        retry_actions.append(f"Try Domain Focus for `{top_unpriced_domains[0][0]}`.")
    if not web_rows:
        retry_actions.append("Enable web fallback or broaden the query to brand + type + weight.")
    return {
        "ebay_status": "; ".join(ebay_notes[-4:]) if ebay_notes else "not attempted",
        "top_priced_domains": top_priced_domains,
        "top_unpriced_domains": top_unpriced_domains,
        "retry_actions": retry_actions,
    }


def _comp_quality_diagnostics_rows(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"Signal": "eBay sold status", "Value": str(diagnostics.get("ebay_status") or "")}
    ]
    priced = diagnostics.get("top_priced_domains") or []
    unpriced = diagnostics.get("top_unpriced_domains") or []
    rows.append(
        {
            "Signal": "Top priced domains",
            "Value": ", ".join(f"{domain} ({count})" for domain, count in priced) or "none",
        }
    )
    rows.append(
        {
            "Signal": "Top unpriced domains",
            "Value": ", ".join(f"{domain} ({count})" for domain, count in unpriced) or "none",
        }
    )
    for idx, action in enumerate(diagnostics.get("retry_actions") or [], start=1):
        rows.append({"Signal": f"Suggested retry {idx}", "Value": str(action)})
    return rows


def _friendly_ebay_attempt_note(note: str) -> str:
    raw = str(note or "").strip()
    lowered = raw.lower()
    if lowered.startswith("ebay_sold_html_httperror") and "status=403" in lowered:
        return "eBay public sold-results HTML blocked this server with HTTP 403; web fallback was used."
    if lowered.startswith("ebay_finding_error"):
        return "eBay Finding API fallback failed; see detail column. Web fallback was used."
    if lowered.startswith("ebay_finding_sandbox"):
        return "eBay Finding API searched sandbox data, which usually has little/no real sold comp inventory."
    if lowered.startswith("ebay_finding_production"):
        return "eBay Finding API searched production data."
    if lowered == "ebay_product_research_csv_import":
        return "Manual eBay Product Research/Terapeak CSV imported as sold-market evidence."
    if lowered == "ebay_marketplace_insights":
        return "Official eBay Marketplace Insights item-sales API returned sold-market rows."
    if lowered.startswith("ebay_marketplace_insights_denied"):
        return "Marketplace Insights API denied access; verify the token includes the required buy.marketplace.insights scope."
    if lowered.startswith("ebay_marketplace_insights_token_error"):
        return "Marketplace Insights app-token request failed; verify eBay app credentials and API access."
    if lowered.startswith("ebay_marketplace_insights_not_configured"):
        return "Marketplace Insights API was not configured because no eBay access token is available."
    if lowered == "ebay_sold_html_empty":
        return "eBay public sold-results HTML returned no parsed sold rows."
    return raw


def _format_attempt_rows(attempts: list[dict]) -> list[dict]:
    formatted: list[dict] = []
    for attempt in attempts or []:
        row = dict(attempt or {})
        note = str(row.get("note") or "")
        row["status"] = _friendly_ebay_attempt_note(note)
        formatted.append(row)
    return formatted


def _comp_evidence_export_payload(
    *,
    query: str,
    attempts: list[dict],
    rows: list[dict],
    web_rows: list[dict],
    stats: dict[str, float],
    cost_breakdown: dict[str, float],
    evidence_quality: dict[str, Any],
    diagnostics: dict[str, Any],
    spot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "query": str(query or "").strip(),
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "stats": dict(stats or {}),
        "cost_breakdown": dict(cost_breakdown or {}),
        "evidence_quality": dict(evidence_quality or {}),
        "diagnostics": dict(diagnostics or {}),
        "spot_context": dict(spot_context or {}),
        "attempts": list(attempts or []),
        "ebay_sold_rows": list(rows or []),
        "web_rows": list(web_rows or []),
        "notes": [
            "eBay sold rows with sold_price are sold-market evidence.",
            "Web rows are active/listed/research evidence unless explicitly proven sold.",
            "Use diagnostics and raw URLs before relying on low-confidence rows for pricing.",
        ],
    }


def _detect_metal_from_query(query: str) -> str:
    q = (query or "").strip().lower()
    if "platinum" in q or "xpt" in q:
        return "platinum"
    if "silver" in q or "xag" in q:
        return "silver"
    if "gold" in q or "xau" in q:
        return "gold"
    return ""


def _price_confidence_label(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.65:
        return "medium"
    if score >= 0.45:
        return "low"
    return "very_low"


def _web_price_confidence(
    price_source: str,
    price_count: int,
    tier_count: int,
    parser_source: str = "",
    domain: str = "",
    dealer_domains: tuple[str, ...] | None = None,
) -> tuple[float, str]:
    source = (price_source or "").strip().lower()
    parser = (parser_source or "").strip().lower()
    host = (domain or "").strip().lower()
    score = 0.0
    if source == "page_fetch":
        score = 0.68
    elif source == "snippet_or_url":
        score = 0.48
    if parser in {"json_ld", "domain_specific"}:
        score += 0.08
    elif parser == "reader_proxy":
        score += 0.06
    elif parser == "embedded_json":
        score += 0.05
    elif parser == "tier_table":
        score += 0.04
    elif parser == "html_general":
        score += 0.03
    if int(price_count or 0) >= 3:
        score += 0.12
    elif int(price_count or 0) == 2:
        score += 0.07
    elif int(price_count or 0) == 1:
        score += 0.03
    if int(tier_count or 0) > 0:
        score += 0.12

    # Domain-aware reliability tuning.
    dealer_high_conf = tuple(dealer_domains or DEFAULT_COMP_DEALER_DOMAINS)
    if any(token in host for token in dealer_high_conf):
        score += 0.06
        if parser == "domain_specific":
            score += 0.03
        if int(tier_count or 0) > 0:
            score += 0.02
    elif "ebay." in host:
        score += 0.03
    elif any(token in host for token in ("amazon.", "etsy.", "walmart.")):
        score -= 0.02
    elif any(token in host for token in ("facebook.com", "craigslist.org")):
        score -= 0.05

    score = max(0.0, min(0.99, float(score)))
    return score, _price_confidence_label(score)


def _best_page_parser_source(
    base_prices: list[float],
    json_prices: list[float],
    json_ld_prices: list[float],
    domain_prices: list[float],
    tier_count: int,
) -> str:
    if domain_prices:
        return "domain_specific"
    if json_ld_prices:
        return "json_ld"
    if json_prices:
        return "embedded_json"
    if base_prices:
        return "html_general"
    if int(tier_count or 0) > 0:
        return "tier_table"
    return "none"


def _extract_price_hints(value: str) -> list[float]:
    text = unescape(value or "").replace("\xa0", " ").strip()
    if not text:
        return []

    parsed: list[float] = []

    # Covers:
    # - $11.99
    # - US $11.99
    # - $9 99 (split/superscript cents rendered as whitespace)
    usd_like_matches = re.findall(
        r"(?i)(?:USD|US\$|US|CAD|AUD|NZD|C|CA|AU)?\s*\$\s*([0-9][0-9,]*)(?:\s*(?:[.,]|\s)\s*([0-9]{1,2}))?",
        text,
    )
    for dollars_raw, cents_raw in usd_like_matches:
        dollars = str(dollars_raw or "").replace(",", "").strip()
        cents = str(cents_raw or "").strip()
        if not dollars:
            continue
        try:
            if cents:
                parsed.append(float(f"{int(dollars)}.{int(cents):02d}"))
            else:
                parsed.append(float(dollars))
        except Exception:
            continue

    for symbol in ("£", "€"):
        symbol_matches = re.findall(
            rf"(?i){re.escape(symbol)}\s*([0-9][0-9,]*)(?:\s*(?:[.,]|\s)\s*([0-9]{{1,2}}))?",
            text,
        )
        for major_raw, minor_raw in symbol_matches:
            major = str(major_raw or "").replace(",", "").strip()
            minor = str(minor_raw or "").strip()
            if not major:
                continue
            try:
                if minor:
                    parsed.append(float(f"{int(major)}.{int(minor):02d}"))
                else:
                    parsed.append(float(major))
            except Exception:
                continue

    # Keyword-led numeric prices without explicit currency symbol (common in modern ecommerce UIs).
    keyword_matches = re.findall(
        r"(?is)(?:as\s+low\s+as|our\s+price|sale\s+price|price|now|buy\s+now|add\s+to\s+cart|only)\s*[:\-]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        text,
    )
    for raw in keyword_matches:
        try:
            parsed.append(float(str(raw).replace(",", "").strip()))
        except Exception:
            continue

    if not parsed:
        return []
    seen: set[float] = set()
    out: list[float] = []
    for price in parsed:
        if price in seen:
            continue
        seen.add(price)
        out.append(price)
    return out


def _extract_price_hints_from_html(html_text: str) -> list[float]:
    if not html_text:
        return []
    text = html_text[:600000]
    candidates: list[str] = []

    candidates.extend(
        re.findall(r'(?i)"price"\s*:\s*"([0-9][0-9,]*(?:\.[0-9]{1,2})?)"', text)
    )
    candidates.extend(
        re.findall(r'(?i)"price"\s*:\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)', text)
    )
    candidates.extend(
        re.findall(r'(?i)"(?:salePrice|listPrice|currentPrice|priceAmount|amount)"\s*:\s*"([0-9][0-9,]*(?:\.[0-9]{1,2})?)"', text)
    )
    candidates.extend(
        re.findall(r'(?i)"(?:salePrice|listPrice|currentPrice|priceAmount|amount)"\s*:\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)', text)
    )
    cents_candidates = re.findall(
        r'(?i)"(?:priceCents|amountCents|unitPriceCents|salePriceCents)"\s*:\s*([0-9]{2,8})',
        text,
    )
    candidates.extend(
        re.findall(
            r'(?i)<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']',
            text,
        )
    )
    candidates.extend(
        re.findall(
            r'(?i)<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']',
            text,
        )
    )
    candidates.extend(
        re.findall(
            r'(?i)itemprop=["\']price["\'][^>]*>\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)',
            text,
        )
    )
    candidates.extend(
        re.findall(
            r'(?i)\bdata-(?:price|sale-price|product-price|amount)=["\']\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']',
            text,
        )
    )
    candidates.extend(
        re.findall(
            r'(?i)\bcontent=["\']\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\'][^>]*(?:price|amount)',
            text,
        )
    )
    candidates.extend(
        re.findall(
            r'(?i)<meta[^>]+property=["\'](?:og:price:amount|product:price:amount)["\'][^>]+content=["\']([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']',
            text,
        )
    )
    candidates.extend(
        re.findall(
            r'(?i)<meta[^>]+name=["\'](?:twitter:data1|price|product_price)["\'][^>]+content=["\']\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']',
            text,
        )
    )

    visible_text = unescape(re.sub(r"<[^>]+>", " ", text))
    candidates.extend(
        [str(p) for p in _extract_price_hints(visible_text)]
    )

    parsed: list[float] = []
    for raw in candidates:
        try:
            parsed.append(float(str(raw).replace(",", "").strip()))
        except Exception:
            continue
    parsed = [p for p in parsed if p > 0]
    for raw_cents in cents_candidates:
        try:
            cents = int(str(raw_cents).strip())
            if cents > 0:
                parsed.append(float(cents) / 100.0)
        except Exception:
            continue
    if not parsed:
        return []
    seen: set[float] = set()
    out: list[float] = []
    for price in parsed:
        if price in seen:
            continue
        seen.add(price)
        out.append(price)
    return out


def _extract_json_ld_prices(html_text: str) -> list[float]:
    if not html_text:
        return []
    text = html_text[:800000]
    blocks = re.findall(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text,
    )
    out: list[float] = []
    for block in blocks:
        raw = (block or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            _walk_json_for_prices(parsed, out)
            continue
        except Exception:
            pass
        # Fallback: pull direct price keys from malformed JSON-LD blocks.
        hits = re.findall(
            r'(?i)"(?:price|lowPrice|highPrice|priceAmount|amount)"\s*:\s*"?([0-9][0-9,]*(?:\.[0-9]{1,2})?)"?',
            raw,
        )
        for hit in hits:
            try:
                out.append(float(str(hit).replace(",", "").strip()))
            except Exception:
                continue
    cleaned = [float(p) for p in out if isinstance(p, (int, float)) and float(p) > 0]
    if not cleaned:
        return []
    return [float(p) for p in sorted(set(round(float(p), 4) for p in cleaned))]


def _extract_domain_specific_prices(
    url: str,
    html_text: str,
    dealer_domains: tuple[str, ...] | None = None,
) -> list[float]:
    host = (urlparse(url).netloc or "").lower()
    text = html_text[:900000] if html_text else ""
    if not text:
        return []
    out: list[float] = []

    # eBay web item pages
    if "ebay." in host:
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)<(?:div|span)[^>]+class=["\'][^"\']*(?:x-price-primary|x-bin-price|display-price|notranslate)[^"\']*["\'][^>]*>(.*?)</(?:div|span)>',
                        text,
                    )
                )
            )
        )
        for major, minor in re.findall(
            r'(?is)a-price-whole[^>]*>\s*([0-9][0-9,]*)\s*<.*?a-price-fraction[^>]*>\s*([0-9]{2})\s*<',
            text,
        ):
            try:
                out.append(float(f"{int(str(major).replace(',', ''))}.{int(minor):02d}"))
            except Exception:
                continue

    # Amazon product pages
    if "amazon." in host:
        # Typical offscreen full price text.
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)<span[^>]+class=["\'][^"\']*a-offscreen[^"\']*["\'][^>]*>(.*?)</span>',
                        text,
                    )
                )
            )
        )
        for major, minor in re.findall(
            r'(?is)a-price-whole[^>]*>\s*([0-9][0-9,]*)\s*<.*?a-price-fraction[^>]*>\s*([0-9]{2})\s*<',
            text,
        ):
            try:
                out.append(float(f"{int(str(major).replace(',', ''))}.{int(minor):02d}"))
            except Exception:
                continue

    # Shopify/bullion dealer style product templates
    if any(token in host for token in ("shopify", "bullion", "coin", "mint")):
        for raw in re.findall(
            r'(?i)\b(?:compare_at_price|max_price|min_price|price)\b["\']?\s*[:=]\s*["\']?([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']?',
            text,
        ):
            try:
                out.append(float(str(raw).replace(",", "").strip()))
            except Exception:
                continue
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)(?:as\s+low\s+as|our\s+price|sale\s+price|add\s+to\s+cart)[^$]{0,80}\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)',
                        text,
                    )
                )
            )
        )

    # Additional known dealer/storefront patterns
    dealer_hosts = tuple(dealer_domains or DEFAULT_COMP_DEALER_DOMAINS)
    if any(token in host for token in (dealer_hosts + ("goldenstatemint.com",))):
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)<(?:span|div|td)[^>]+(?:price|product-price|tier-price|as-low-as)[^>]*>(.*?)</(?:span|div|td)>',
                        text,
                    )
                )
            )
        )
        for major, minor in re.findall(
            r'(?is)\$\s*([0-9][0-9,]*)\s*<sup[^>]*>\s*([0-9]{2})\s*</sup>',
            text,
        ):
            try:
                out.append(float(f"{int(str(major).replace(',', ''))}.{int(minor):02d}"))
            except Exception:
                continue
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)<(?:span|div|p)[^>]+class=["\'][^"\']*(?:price-box__price|price-sales|regular-price|final-price|price-item--sale|price-item--regular|product-price|price--withoutTax|as-low-as|tier-price)[^"\']*["\'][^>]*>(.*?)</(?:span|div|p)>',
                        text,
                    )
                )
            )
        )
        for raw in re.findall(
            r'(?i)\b(?:data-price|data-product-price|data-sale-price|data-amount|itemprop=["\']price["\'])\s*=\s*["\']\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)["\']',
            text,
        ):
            try:
                out.append(float(str(raw).replace(",", "").strip()))
            except Exception:
                continue

    # Etsy listing pages
    if "etsy." in host:
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)<(?:p|span|div)[^>]+class=["\'][^"\']*(?:wt-text-title|wt-text-caption|currency-value|money|price)[^"\']*["\'][^>]*>(.*?)</(?:p|span|div)>',
                        text,
                    )
                )
            )
        )

    # Walmart listing pages
    if "walmart." in host:
        out.extend(
            _extract_price_hints(
                " ".join(
                    re.findall(
                        r'(?is)<(?:span|div)[^>]+(?:itemprop=["\']price["\']|data-automation-id=["\']product-price["\']|class=["\'][^"\']*(?:price-characteristic|price-group|price-main|price)[^"\']*["\'])[^>]*>(.*?)</(?:span|div)>',
                        text,
                    )
                )
            )
        )

    # Generic class-based price containers as broad final pass.
    out.extend(
        _extract_price_hints(
            " ".join(
                re.findall(
                    r'(?is)<(?:span|div|p|td)[^>]+class=["\'][^"\']*(?:price|pricing|amount|money|cost|sale)[^"\']*["\'][^>]*>(.*?)</(?:span|div|p|td)>',
                    text,
                )
            )
        )
    )

    cleaned = [float(p) for p in out if float(p) > 0]
    if not cleaned:
        return []
    return [float(p) for p in sorted(set(round(p, 4) for p in cleaned))]


def _walk_json_for_prices(node, out: list[float]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).strip().lower()
            if key_l in {
                "price",
                "amount",
                "priceamount",
                "pricevalue",
                "currentprice",
                "saleprice",
                "listprice",
                "ourprice",
                "price_string",
                "pricestring",
                "displayprice",
                "formattedprice",
                "value",
                "lowprice",
                "highprice",
            }:
                if isinstance(value, (int, float)):
                    if float(value) > 0:
                        out.append(float(value))
                elif isinstance(value, str):
                    parsed = _extract_price_hints(value)
                    out.extend(parsed)
            elif "cent" in key_l and isinstance(value, (int, float, str)):
                try:
                    cents = int(float(value))
                    if cents > 0:
                        out.append(float(cents) / 100.0)
                except Exception:
                    pass
            _walk_json_for_prices(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_json_for_prices(item, out)


def _extract_json_embedded_prices(html_text: str) -> list[float]:
    if not html_text:
        return []
    text = html_text[:600000]
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", text, flags=re.IGNORECASE | re.DOTALL)
    candidates: list[float] = []
    for script in scripts:
        raw = (script or "").strip()
        if not raw or "price" not in raw.lower():
            continue
        parsed_any = False
        if raw.startswith("{") or raw.startswith("["):
            try:
                parsed = json.loads(raw)
                _walk_json_for_prices(parsed, candidates)
                parsed_any = True
            except Exception:
                parsed_any = False
        if not parsed_any:
            # Fallback for embedded JS payloads that are not pure JSON
            # (e.g., `window.__STATE__ = {...}`).
            fragment_hits = re.findall(
                r'(?i)"(?:price|salePrice|listPrice|currentPrice|priceAmount|amount|priceString|displayPrice|formattedPrice)"\s*:\s*"?([0-9][0-9,]*(?:\.[0-9]{1,2})?)"?',
                raw,
            )
            for hit in fragment_hits:
                try:
                    candidates.append(float(str(hit).replace(",", "").strip()))
                except Exception:
                    continue
    cleaned = [float(p) for p in candidates if isinstance(p, (int, float)) and float(p) > 0]
    if not cleaned:
        return []
    deduped = sorted(set(round(p, 4) for p in cleaned))
    return [float(p) for p in deduped]


def _extract_tier_prices_from_html(html_text: str) -> dict:
    if not html_text:
        return {"tiers": [], "low": 0.0, "high": 0.0}
    text = unescape(re.sub(r"<[^>]+>", " ", html_text)).replace("\xa0", " ")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln and ln.strip()]
    tiers: list[dict] = []
    qty_pattern = re.compile(r"(?i)\b(\d+\s*-\s*\d+|\d+\+|\d+\s*to\s*\d+)\b")
    for line in lines:
        if not qty_pattern.search(line):
            continue
        price_hints = _extract_price_hints(line)
        if not price_hints:
            continue
        qty_match = qty_pattern.search(line)
        qty_label = qty_match.group(1) if qty_match else ""
        tier_min = min(price_hints)
        tier_max = max(price_hints)
        tiers.append(
            {
                "qty": qty_label,
                "min_price": tier_min,
                "max_price": tier_max,
                "price_count": len(price_hints),
            }
        )
    if not tiers:
        return {"tiers": [], "low": 0.0, "high": 0.0}
    low = min(float(t["min_price"]) for t in tiers)
    high = max(float(t["max_price"]) for t in tiers)
    return {"tiers": tiers, "low": low, "high": high}


def _fetch_page_price_details(url: str, dealer_domains: tuple[str, ...] | None = None) -> dict:
    target = (url or "").strip()
    empty = {
        "prices": [],
        "tier_low": 0.0,
        "tier_high": 0.0,
        "tier_count": 0,
        "tiers_json": "[]",
        "parser_source": "none",
        "source_counts_json": "{}",
    }
    if not target.startswith(("http://", "https://")):
        return empty
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GoldenStackersCompTool/1.0)"}
    try:
        rich_headers = {
            **headers,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        response = requests.get(target, headers=rich_headers, timeout=15)
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return empty
        html_text = response.text or ""
        base_prices = _extract_price_hints_from_html(html_text)
        json_prices = _extract_json_embedded_prices(html_text)
        json_ld_prices = _extract_json_ld_prices(html_text)
        domain_prices = _extract_domain_specific_prices(target, html_text, dealer_domains=dealer_domains)
        tier_data = _extract_tier_prices_from_html(html_text)
        prices = base_prices + json_prices + json_ld_prices + domain_prices
        if tier_data.get("low", 0.0) > 0:
            prices.append(float(tier_data.get("low", 0.0)))
        if tier_data.get("high", 0.0) > 0:
            prices.append(float(tier_data.get("high", 0.0)))
        deduped = sorted(set(round(float(p), 4) for p in prices if float(p) > 0))
        source_counts = {
            "html_general": len(base_prices),
            "embedded_json": len(json_prices),
            "json_ld": len(json_ld_prices),
            "domain_specific": len(domain_prices),
            "tier_table": len(tier_data.get("tiers") or []),
            "reader_proxy": 0,
        }
        parser_source = _best_page_parser_source(
            base_prices=base_prices,
            json_prices=json_prices,
            json_ld_prices=json_ld_prices,
            domain_prices=domain_prices,
            tier_count=len(tier_data.get("tiers") or []),
        )
        if not deduped:
            try:
                proxy_url = f"https://r.jina.ai/{target}"
                proxy_response = requests.get(
                    proxy_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; GoldenStackersCompTool/1.0)"},
                    timeout=20,
                )
                proxy_response.raise_for_status()
                proxy_text = str(proxy_response.text or "")[:400000]
                proxy_prices = _extract_price_hints(proxy_text)
                if proxy_prices:
                    deduped = sorted(set(round(float(p), 4) for p in proxy_prices if float(p) > 0))
                    source_counts["reader_proxy"] = len(proxy_prices)
                    parser_source = "reader_proxy"
            except Exception:
                pass
        return {
            "prices": [float(p) for p in deduped],
            "tier_low": float(tier_data.get("low") or 0.0),
            "tier_high": float(tier_data.get("high") or 0.0),
            "tier_count": len(tier_data.get("tiers") or []),
            "tiers_json": json.dumps(tier_data.get("tiers") or []),
            "parser_source": parser_source,
            "source_counts_json": json.dumps(source_counts),
        }
    except Exception:
        return empty


@lru_cache(maxsize=512)
def _fetch_page_price_details_cached_json(url: str, dealer_domains_csv: str) -> str:
    dealer_domains = _parse_domain_csv(dealer_domains_csv)
    return json.dumps(_fetch_page_price_details(url, dealer_domains=dealer_domains))


def _fetch_page_price_details_cached(url: str, dealer_domains: tuple[str, ...] | None = None) -> dict:
    try:
        dealer_domains_csv = ",".join(list(dealer_domains or DEFAULT_COMP_DEALER_DOMAINS))
        return json.loads(_fetch_page_price_details_cached_json((url or "").strip(), dealer_domains_csv))
    except Exception:
        return {"prices": [], "tier_low": 0.0, "tier_high": 0.0, "tier_count": 0, "tiers_json": "[]", "parser_source": "none", "source_counts_json": "{}"}


def _fetch_page_details_batch(
    urls: list[str],
    *,
    max_workers: int = 6,
    dealer_domains: tuple[str, ...] | None = None,
) -> dict[str, dict]:
    targets = [(u or "").strip() for u in urls if (u or "").strip()]
    if not targets:
        return {}
    unique_targets = list(dict.fromkeys(targets))
    workers = max(1, min(int(max_workers), len(unique_targets)))
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_fetch_page_price_details_cached, url, dealer_domains): url for url in unique_targets
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                results[url] = future.result()
            except Exception:
                results[url] = {
                    "prices": [],
                    "tier_low": 0.0,
                    "tier_high": 0.0,
                    "tier_count": 0,
                    "tiers_json": "[]",
                    "parser_source": "none",
                    "source_counts_json": "{}",
                }
    return results


def _resolve_duckduckgo_result_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if "duckduckgo.com/l/?" not in url:
        if "bing.com/ck/a" not in url:
            return url
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        encoded_target = params.get("u", [""])[0]
        if not encoded_target:
            return url
        if encoded_target.startswith("a1"):
            encoded_target = encoded_target[2:]
        try:
            padded = encoded_target + ("=" * (-len(encoded_target) % 4))
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
            return decoded if decoded.startswith(("http://", "https://")) else url
        except Exception:
            return url
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    uddg = params.get("uddg", [""])[0]
    return unquote(uddg) if uddg else url


def _web_comp_search(
    query: str,
    limit: int = 20,
    page_fetch_limit: int = 8,
    dealer_domains: tuple[str, ...] | None = None,
    include_dealer_targeted: bool = True,
) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    endpoint = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GoldenStackersCompTool/1.0)"}
    response = requests.get(endpoint, params={"q": q}, headers=headers, timeout=30)
    response.raise_for_status()
    html_text = response.text or ""

    search_provider = "duckduckgo"
    anchors = re.findall(
        r'<a[^>]*class=["\'][^"\']*result__a[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    snippets = re.findall(
        r'<[^>]*class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not anchors and ("anomaly" in html_text.lower() or response.status_code == 202):
        try:
            bing_response = requests.get(
                "https://www.bing.com/search",
                params={"q": q},
                headers=headers,
                timeout=30,
            )
            bing_response.raise_for_status()
            bing_html = bing_response.text or ""
            blocks = re.findall(
                r'<li[^>]*class=["\'][^"\']*\bb_algo\b[^"\']*["\'][^>]*>(.*?)</li>',
                bing_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            bing_anchors: list[tuple[str, str]] = []
            bing_snippets: list[str] = []
            for block in blocks:
                link_match = re.search(
                    r'<h2[^>]*>\s*<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                    block,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if not link_match:
                    continue
                bing_anchors.append((link_match.group(1), link_match.group(2)))
                snippet_match = re.search(
                    r'<p[^>]*>(.*?)</p>',
                    block,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                bing_snippets.append(snippet_match.group(1) if snippet_match else "")
            if bing_anchors:
                anchors = bing_anchors
                snippets = bing_snippets
                search_provider = "bing"
        except Exception:
            pass
    rows: list[dict] = []
    candidates: list[dict] = []
    max_page_fetches = min(max(1, int(limit)), max(1, int(page_fetch_limit)))
    for idx, (href, title_html) in enumerate(anchors[: max(1, int(limit))]):
        title = unescape(re.sub(r"<[^>]+>", " ", title_html)).strip()
        snippet_html = snippets[idx] if idx < len(snippets) else ""
        snippet = unescape(re.sub(r"<[^>]+>", " ", snippet_html)).strip()
        resolved_url = _resolve_duckduckgo_result_url(unescape(href))
        price_hints = _extract_price_hints(f"{title} {snippet} {resolved_url}")
        price_hint_source = "snippet_or_url" if price_hints else "none"
        candidates.append(
            {
                "title": title,
                "snippet": snippet,
                "resolved_url": resolved_url,
                "price_hints": list(price_hints),
                "price_hint_source": price_hint_source,
                "page_details": {
                    "prices": [],
                    "tier_low": 0.0,
                    "tier_high": 0.0,
                    "tier_count": 0,
                    "tiers_json": "[]",
                    "parser_source": "none",
                    "source_counts_json": "{}",
                },
            }
        )

    # Always fetch some destination pages, prioritizing sparse/low-signal snippet results first.
    fetch_targets: list[str] = []
    prioritized = sorted(
        candidates,
        key=lambda c: (
            len(c.get("price_hints") or []),
            len(str(c.get("snippet") or "")),
        ),
    )
    for candidate in prioritized:
        target_url = str(candidate.get("resolved_url") or "").strip()
        if not target_url:
            continue
        fetch_targets.append(target_url)
        if len(fetch_targets) >= max_page_fetches:
            break

    page_details_map = _fetch_page_details_batch(
        fetch_targets,
        max_workers=min(6, max_page_fetches),
        dealer_domains=dealer_domains,
    )

    for candidate in candidates:
        page_details = {
            "prices": [],
            "tier_low": 0.0,
            "tier_high": 0.0,
            "tier_count": 0,
            "tiers_json": "[]",
            "parser_source": "none",
            "source_counts_json": "{}",
        }
        title = str(candidate.get("title") or "")
        snippet = str(candidate.get("snippet") or "")
        resolved_url = str(candidate.get("resolved_url") or "")
        price_hints = list(candidate.get("price_hints") or [])
        price_hint_source = str(candidate.get("price_hint_source") or "none")
        host = (urlparse(resolved_url).netloc or "").lower()
        if resolved_url in page_details_map:
            page_details = page_details_map.get(resolved_url) or page_details
            page_price_hints = page_details.get("prices") or []
            if page_price_hints:
                combined = [float(p) for p in (price_hints + page_price_hints) if float(p) > 0]
                price_hints = sorted(set(round(float(p), 4) for p in combined))
                price_hint_source = "page_fetch"
        listed_price = _representative_price(price_hints)
        listed_low = min(price_hints) if price_hints else 0.0
        listed_high = max(price_hints) if price_hints else 0.0
        tier_count = int(page_details.get("tier_count") or 0)
        parser_source = str(page_details.get("parser_source") or "none")
        confidence_score, confidence_label = _web_price_confidence(
            price_source=price_hint_source,
            price_count=len(price_hints),
            tier_count=tier_count,
            parser_source=parser_source,
            domain=host,
            dealer_domains=dealer_domains,
        )
        baseline_score, _ = _web_price_confidence(
            price_source=price_hint_source,
            price_count=len(price_hints),
            tier_count=tier_count,
            parser_source=parser_source,
            domain="",
            dealer_domains=dealer_domains,
        )
        row = {
            "source": "web",
            "search_provider": search_provider,
            "search_scope": "broad_web",
            "search_query": q,
            "domain": host,
            "title": title,
            "snippet": snippet,
            "view_url": resolved_url,
            "sold_price": 0.0,
            "listed_price": listed_price,
            "listed_price_low": listed_low,
            "listed_price_high": listed_high,
            "tier_price_low": float(page_details.get("tier_low") or 0.0),
            "tier_price_high": float(page_details.get("tier_high") or 0.0),
            "tier_count": tier_count,
            "tier_prices_json": str(page_details.get("tiers_json") or "[]"),
            "page_parser_source": parser_source,
            "page_source_counts_json": str(page_details.get("source_counts_json") or "{}"),
            "shipping_cost": 0.0,
            "total_price": listed_price,
            "currency": "USD",
            "condition": "",
            "end_time": "",
            "price_hint_count": len(price_hints),
            "price_hint_source": price_hint_source,
            "price_confidence_score": confidence_score,
            "price_confidence_label": confidence_label,
            "price_confidence_domain_delta": round(float(confidence_score) - float(baseline_score), 4),
        }
        relevance = _comp_row_relevance(q, row)
        row["relevance_score"] = relevance["score"]
        row["relevance_flags"] = ",".join(relevance["flags"])
        row["target_weight_oz"] = relevance["target_weight_oz"]
        rows.append(row)
    if include_dealer_targeted and dealer_domains:
        priced_rows = [row for row in rows if float(row.get("total_price") or 0.0) > 0.0]
        if len(priced_rows) < 3:
            seen_urls = {str(row.get("view_url") or "").strip() for row in rows if str(row.get("view_url") or "").strip()}
            dealer_limit = min(6, len(dealer_domains))
            for dealer_domain in list(dealer_domains)[:dealer_limit]:
                domain = str(dealer_domain or "").strip().lower()
                if not domain:
                    continue
                targeted_query = f"site:{domain} {q}"
                try:
                    targeted_rows = _web_comp_search(
                        targeted_query,
                        limit=min(5, max(1, int(limit))),
                        page_fetch_limit=min(2, max(1, int(page_fetch_limit))),
                        dealer_domains=dealer_domains,
                        include_dealer_targeted=False,
                    )
                except Exception:
                    targeted_rows = []
                for row in targeted_rows:
                    view_url = str(row.get("view_url") or "").strip()
                    if view_url and view_url in seen_urls:
                        continue
                    if view_url:
                        seen_urls.add(view_url)
                    row["search_scope"] = "configured_dealer"
                    row["search_query"] = targeted_query
                    rows.append(row)
    return rows


def render_tools(spot: SpotPriceService, repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    comp_tool_enabled = is_ai_domain_enabled(repo, "comp_tool")
    coin_grader_enabled = is_ai_domain_enabled(repo, "coin_grader")
    coin_identifier_enabled = is_ai_domain_enabled(repo, "coin_identifier")
    can_use_comp_tool = has_permission(user.role, "ai_comp_use")
    can_use_coin_grader = has_permission(user.role, "ai_coin_grade")
    can_use_coin_identifier = has_permission(user.role, "ai_coin_identify")
    st.subheader("Tools")
    render_help_panel(
        section_title="Tools",
        goal="Run fast calculations for weight conversion and spot-based pricing estimates.",
        steps=[
            "Use Gram ↔ Troy Oz conversions when creating or validating listing details.",
            "Use Spot Estimator for melt value and target pricing with purity/premium inputs.",
            "Fetch spot when available or enter manual spot during provider throttling.",
            "Use estimator outputs as guidance, then apply channel fee/shipping realities.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )
    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
        [
            "Gram ↔ Troy Oz",
            "Spot Estimator",
            "eBay Fee Calculator",
            "Comp Tool",
            "Coin Grader",
            "Coin Identifier",
            "Coin Database",
        ]
    )

    with tab1:
        st.caption("Precious metals are typically priced in troy ounces.")
        grams = st.number_input("Grams", min_value=0.0, value=31.1035, step=0.01, key="grams_input")
        troy_oz = grams_to_troy_oz(grams)
        st.metric("Troy Ounces", f"{troy_oz:,.6f} oz t")

        oz_troy = st.number_input("Troy Ounces", min_value=0.0, value=1.0, step=0.01, key="toz_input")
        grams_back = troy_oz_to_grams(oz_troy)
        st.metric("Grams", f"{grams_back:,.4f} g")

    with tab2:
        st.caption("Estimate melt value and pricing versus current spot.")

        metal = st.selectbox("Metal", ["gold", "silver", "platinum"])
        c1, c2, c3 = st.columns(3)
        with c1:
            weight_grams = st.number_input("Weight (grams)", min_value=0.0, value=31.1035, step=0.01)
        with c2:
            purity_pct = st.number_input("Purity %", min_value=0.0, max_value=100.0, value=99.9, step=0.1)
        with c3:
            premium_pct = st.number_input("Premium %", min_value=-100.0, value=5.0, step=0.1)

        spot_price = st.number_input(
            "Spot Price USD / Troy Oz (manual or live fetched)",
            min_value=0.0,
            value=0.0,
            step=0.01,
        )

        if st.button("Fetch Current Spot"):
            if not spot.is_configured():
                st.warning(
                    "Spot provider is not configured. Configure SPOT_PRICE_PROVIDER settings or enter manual spot."
                )
            else:
                try:
                    quotes = spot.latest_quotes()
                    quote = quotes.get(metal)
                    if quote:
                        if "fetched_spot_prices" not in st.session_state:
                            st.session_state["fetched_spot_prices"] = {}
                        st.session_state["fetched_spot_prices"][metal] = quote.usd_per_troy_oz
                        st.success(
                            f"Fetched {metal} spot: ${quote.usd_per_troy_oz:,.2f}/oz t "
                            f"({quote.source}, {quote.as_of.isoformat()})"
                        )
                    else:
                        st.error(f"No quote returned for {metal}.")
                except SpotRateLimitError as exc:
                    fetched_prices = st.session_state.get("fetched_spot_prices", {})
                    if metal in fetched_prices:
                        st.warning(
                            f"{exc} Using last fetched {metal} quote from this session instead."
                        )
                    else:
                        st.warning(f"{exc} Enter spot manually for now.")
                except Exception as exc:
                    st.error(f"Spot fetch failed: {exc}")

        fetched_prices = st.session_state.get("fetched_spot_prices", {})
        if metal in fetched_prices:
            use_fetched = st.checkbox("Use fetched spot quote", value=True)
            if use_fetched:
                spot_price = float(fetched_prices[metal])

        troy_oz_total = grams_to_troy_oz(weight_grams)
        fine_troy_oz = troy_oz_total * (purity_pct / 100.0)
        melt_value = fine_troy_oz * spot_price
        estimated_cost = melt_value * (1.0 + premium_pct / 100.0)
        spread = estimated_cost - melt_value

        r1, r2, r3 = st.columns(3)
        r1.metric("Fine Troy Oz", f"{fine_troy_oz:,.6f}")
        r2.metric("Melt Value", f"${melt_value:,.2f}")
        r3.metric("Estimated Cost", f"${estimated_cost:,.2f}")
        st.caption(f"Premium/discount vs melt: ${spread:,.2f}")

    with tab3:
        _render_ebay_fee_calculator(repo)

    with tab4:
        if not comp_tool_enabled:
            st.info("Comp Tool is currently disabled by Admin AI domain toggle.")
        if not can_use_comp_tool:
            st.info(f"`{user.role}` role does not have `ai_comp_use` permission.")
        st.caption(
            "Comp Tool: estimate market pricing using sold comparable data. "
            "Start with eBay sold comps and use links for external research."
        )
        products = repo.list_products()
        product_map = {f"{p.sku} | {p.title} | #{p.id}": p for p in products}
        prefill_origin = str(st.session_state.get("comp_prefill_origin") or "").strip()
        prefill_source_mode = str(st.session_state.get("comp_prefill_source_mode") or "").strip()
        prefill_query = str(st.session_state.get("comp_prefill_query") or "").strip()
        prefill_product_id = st.session_state.get("comp_prefill_product_id")
        prefill_manual_title = str(st.session_state.get("comp_prefill_manual_title") or "").strip()
        prefill_manual_desc = str(st.session_state.get("comp_prefill_manual_desc") or "").strip()
        prefill_apply_once = bool(prefill_origin) and not bool(st.session_state.get("comp_prefill_applied"))
        if prefill_origin:
            st.info(f"Comp Tool prefilled from `{prefill_origin}`.")
            if st.button("Clear Prefill", key="comp_clear_prefill_btn"):
                for key in [
                    "comp_prefill_origin",
                    "comp_prefill_source_mode",
                    "comp_prefill_query",
                    "comp_prefill_product_id",
                    "comp_prefill_manual_title",
                    "comp_prefill_manual_desc",
                    "comp_prefill_applied",
                ]:
                    st.session_state.pop(key, None)
                st.rerun()

        source_options = ["Inventory Item", "Manual Title/Description", "Image/File Hint"]
        if prefill_apply_once and prefill_source_mode in source_options:
            st.session_state["comp_source_mode"] = prefill_source_mode
        source_mode = st.radio(
            "Query Source",
            options=source_options,
            horizontal=True,
            key="comp_source_mode",
        )
        photo_workflow_mode = source_mode == "Image/File Hint"
        query = ""
        selected_product = None
        hint_file = None
        comp_media_target_product_id: int | None = None
        if source_mode == "Inventory Item":
            if not product_map:
                st.info("No products available yet.")
            else:
                if prefill_apply_once and prefill_product_id is not None:
                    for label, row in product_map.items():
                        if int(row.id) == int(prefill_product_id):
                            st.session_state["comp_inventory_product_key"] = label
                            break
                product_key = st.selectbox("Product", options=list(product_map.keys()), key="comp_inventory_product_key")
                selected_product = product_map[product_key]
                use_title = st.checkbox("Use title in query", value=True)
                use_sku = st.checkbox("Use SKU in query", value=False)
                use_metal = st.checkbox("Use metal type in query", value=True)
                query = _build_inventory_mode_query(
                    selected_product=selected_product,
                    use_title=bool(use_title),
                    use_sku=bool(use_sku),
                    use_metal=bool(use_metal),
                    prefill_query=prefill_query,
                    prefill_apply_once=bool(prefill_apply_once),
                )
        elif source_mode == "Manual Title/Description":
            if prefill_apply_once and prefill_manual_title:
                st.session_state["comp_manual_title"] = prefill_manual_title
            if prefill_apply_once and prefill_manual_desc:
                st.session_state["comp_manual_desc"] = prefill_manual_desc
            manual_title = st.text_input("Title Keywords", value=prefill_manual_title, key="comp_manual_title")
            manual_desc = st.text_area("Description Keywords", value=prefill_manual_desc, key="comp_manual_desc")
            query = " ".join([manual_title.strip(), manual_desc.strip()]).strip()
            if not query and prefill_query:
                query = prefill_query
        else:
            with st.expander("Camera Hint (Optional)", expanded=False):
                hint_camera = st.camera_input(
                    "Take Hint Photo (optional)",
                    key="comp_hint_camera",
                )
            hint_upload = st.file_uploader(
                "Image/File Hint (name used as keyword hint)",
                type=["jpg", "jpeg", "png", "webp", "gif", "mp4", "mov"],
                accept_multiple_files=False,
                key="comp_hint_upload",
            )
            hint_file = hint_camera or hint_upload
            if prefill_apply_once:
                if prefill_query:
                    st.session_state["comp_manual_hint"] = prefill_query
                elif prefill_manual_title:
                    st.session_state["comp_manual_hint"] = prefill_manual_title
            manual_hint = st.text_input(
                "Optional Additional Keywords",
                key="comp_manual_hint",
            )
            filename_hint = Path(hint_file.name).stem if hint_file is not None else ""
            query = " ".join([filename_hint.strip().replace("_", " "), manual_hint.strip()]).strip()
            if hint_file is not None:
                if st.button("Generate Query From Hint Image (AI)", key="comp_generate_query_from_hint_image_btn"):
                    if not comp_tool_enabled:
                        st.error("Comp Tool is disabled by Admin.")
                    elif not ensure_permission(user, "ai_comp_use", "Generate Comp Query From Image"):
                        pass
                    else:
                        try:
                            generated_query, payload, raw_output = _generate_comp_query_from_hint_image(
                                repo=repo,
                                uploaded_file=hint_file,
                                manual_hint=manual_hint,
                            )
                            if generated_query:
                                st.session_state["comp_hint_ai_generated_query"] = generated_query
                                st.session_state["comp_hint_ai_payload"] = payload
                                st.success("Generated comp query from image. Applied below.")
                                st.rerun()
                            st.warning("AI image query output was not valid JSON. Keeping filename/manual hint query.")
                            st.session_state["comp_hint_ai_query_raw"] = raw_output
                        except Exception as exc:
                            st.error(f"Image query generation failed: {exc}")
                generated_query = str(st.session_state.get("comp_hint_ai_generated_query") or "").strip()
                if generated_query:
                    st.caption(f"AI-generated query seed: `{generated_query}`")
                    query = generated_query
                    hs1, hs2 = st.columns(2)
                    with hs1:
                        if st.button(
                            "Use Query Seed In Inventory Intake Wizard",
                            key="comp_hint_send_to_inventory_intake_btn",
                        ):
                            payload = st.session_state.get("comp_hint_ai_payload") or {}
                            item_summary = str(payload.get("item_summary") or "").strip()
                            condition_hint = str(payload.get("condition_hint") or "").strip()
                            st.session_state["inv_intake_ai_seed_prompt"] = generated_query
                            if item_summary:
                                st.session_state["inv_intake_default_title"] = item_summary
                            if condition_hint:
                                st.session_state["inv_intake_default_description"] = condition_hint
                            st.session_state["workspace_handoff_from"] = "tools_comp"
                            st.session_state["workspace_handoff_target"] = "inventory_intake_wizard"
                            st.success("Applied seed to Inventory Intake Wizard.")
                            if hasattr(st, "switch_page"):
                                st.switch_page("pages/23_Inventory_Intake_Wizard.py")
                    with hs2:
                        if st.button(
                            "Use Query Seed In Listings Draft Create",
                            key="comp_hint_send_to_listings_draft_btn",
                        ):
                            payload = st.session_state.get("comp_hint_ai_payload") or {}
                            item_summary = str(payload.get("item_summary") or "").strip()
                            condition_hint = str(payload.get("condition_hint") or "").strip()
                            st.session_state["create_listing_marketplace"] = "ebay"
                            st.session_state["create_listing_title"] = item_summary or generated_query
                            st.session_state["create_listing_details"] = condition_hint or generated_query
                            st.session_state["create_listing_price"] = 0.0
                            st.session_state["create_listing_qty"] = 1
                            st.session_state["workspace_handoff_from"] = "tools_comp"
                            st.session_state["workspace_handoff_target"] = "listings"
                            st.success("Applied seed to Listings create draft flow.")
                            if hasattr(st, "switch_page"):
                                st.switch_page("pages/03_Listings.py")
                comp_media_products = repo.list_products()
                comp_media_options = ["(none)"] + [f"#{p.id} | {p.sku} | {p.title}" for p in comp_media_products]
                comp_media_pick = st.selectbox(
                    "Attach Hint Image/Video To Product (optional)",
                    options=comp_media_options,
                    key="comp_hint_media_product_pick",
                )
                if comp_media_pick != "(none)":
                    comp_media_target_product_id = int(comp_media_pick.split("|")[0].replace("#", "").strip())
        if prefill_apply_once:
            st.session_state["comp_prefill_applied"] = True

        if query:
            st.caption(f"Effective Comp Query: `{query}`")

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            comp_limit = st.number_input("Comps to fetch", min_value=5, max_value=100, value=25, step=5)
        with c2:
            sold_only = st.checkbox("Sold-only comps", value=True)
        with c3:
            category_id = st.text_input("Optional eBay Category ID", value="")
        with c4:
            default_web_limit = max(5, min(100, get_runtime_int(repo, "comp_web_fallback_limit", 20)))
            web_fallback_limit = st.number_input(
                "Web Fallback Limit",
                min_value=5,
                max_value=100,
                value=int(default_web_limit),
                step=5,
                help="Maximum number of web fallback links to evaluate when eBay comps are empty.",
            )
        with c5:
            default_web_detail_limit = max(1, min(100, get_runtime_int(repo, "comp_web_detail_fetch_limit", 20)))
            web_detail_fetch_limit = st.number_input(
                "Web Detail Fetch Limit",
                min_value=1,
                max_value=100,
                value=int(default_web_detail_limit),
                step=1,
                help="How many web fallback links should be opened and parsed for on-page prices.",
            )
        min_web_confidence = st.selectbox(
            "Minimum Web Price Confidence",
            options=["any", "very_low", "low", "medium", "high"],
            index=0,
            help="Filter web fallback rows to confidence level or higher.",
        )
        wc1, wc2 = st.columns(2)
        with wc1:
            min_web_confidence_score = st.slider(
                "Minimum Confidence Score",
                min_value=0.0,
                max_value=0.99,
                value=0.0,
                step=0.01,
                help="Numeric confidence filter for web fallback rows.",
            )
            parser_source_filter = st.multiselect(
                "Parser Sources",
                options=["domain_specific", "json_ld", "embedded_json", "reader_proxy", "tier_table", "html_general", "none"],
                default=[],
                help="Optional parser-source filter for web fallback rows.",
            )
        with wc2:
            domain_include_raw = st.text_input(
                "Domain Include (comma-separated)",
                value="",
                help="Keep rows where domain contains any listed token.",
            )
            domain_exclude_raw = st.text_input(
                "Domain Exclude (comma-separated)",
                value="",
                help="Remove rows where domain contains any listed token.",
            )
        auto_broaden = st.checkbox(
            "Auto-broaden search if no comps found",
            value=True,
            help="Retries with relaxed query variants and completed-not-sold fallback.",
        )
        use_web_fallback = st.checkbox(
            "Use web-search fallback if eBay comps are empty",
            value=get_runtime_bool(
                repo,
                "comp_web_fallback_enabled",
                bool(settings.comp_web_fallback_enabled),
            ),
        )
        use_ai_summary = st.checkbox(
            "Use AI/LLM summary over comp results",
            value=False,
            help="Uses Admin AI runtime config when present; falls back to COMP_LLM_* env settings.",
        )
        st.caption(
            "eBay sold-results HTML is the primary comp source. Web fallback is used when eBay rows are empty."
        )
        auto_seed_query_from_photo = False
        photo_always_include_web_fallback = False
        photo_auto_ai_summary = False
        if photo_workflow_mode:
            st.markdown("##### Photo-Comp Workflow")
            p1, p2, p3 = st.columns(3)
            with p1:
                auto_seed_query_from_photo = st.checkbox(
                    "Auto-seed query from image on run",
                    value=True,
                    help="If query is empty, generate one from the uploaded/captured image before search.",
                )
            with p2:
                photo_always_include_web_fallback = st.checkbox(
                    "Always include web fallback in photo mode",
                    value=True,
                    help="Runs web fallback hints even when eBay rows are found.",
                )
            with p3:
                photo_auto_ai_summary = st.checkbox(
                    "Auto-run AI summary after search",
                    value=True,
                    help="Runs AI summary immediately after comp rows are collected in photo mode.",
                )
            st.caption(
                "Photo flow: capture/upload image -> optional AI query seed -> eBay + web comp pass -> optional AI summary."
            )
        comp_screenshot_files = st.file_uploader(
            "Comp Evidence Screenshots (optional, for multimodal AI review)",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key="comp_evidence_screenshots",
        )
        product_research_csv = st.file_uploader(
            "eBay Product Research/Terapeak CSV (optional sold comps)",
            type=["csv"],
            accept_multiple_files=False,
            key="comp_ebay_product_research_csv",
            help=(
                "Upload a manual eBay Product Research/Terapeak export when public sold HTML or "
                "API access is blocked. Imported priced rows are treated as sold-market evidence."
            ),
        )
        save_comp_hint_media = st.checkbox(
            "Save hint image/video to Media Library when linked product is selected",
            value=True,
            help="Stores uploaded comp hint media to S3/media_assets for future reference.",
        )
        comp_system_message = get_runtime_str(
            repo,
            "comp_llm_system_message",
            "You are a resale pricing analyst. Provide concise markdown.",
        ).strip()
        comp_instruction = get_runtime_str(
            repo,
            "comp_llm_instruction_template",
            (
                "Summarize likely fair-market pricing for resale. "
                "Return concise markdown with: confidence level, suggested listing range, "
                "key comparables notes, and outlier warnings. "
                "If spot_context indicates precious-metal bullion/coin relevance, include "
                "spot-anchored commentary (melt-floor framing) and explicitly separate "
                "numismatic premium versus melt-driven valuation."
            ),
        ).strip()
        include_spot_context = st.checkbox(
            "Include current spot context in AI summary (bullion/coins)",
            value=True,
            help="Adds live spot quotes + product/query metal hints to improve melt-floor-aware guidance.",
        )
        runtime_cfg_chain = resolve_comp_llm_runtime_chain(repo)
        runtime_cfg = runtime_cfg_chain[0] if runtime_cfg_chain else resolve_comp_llm_runtime_config(repo)
        fallback_profiles = max(0, len(runtime_cfg_chain) - 1)
        dealer_domains = _parse_domain_csv(
            get_runtime_str(
                repo,
                "comp_dealer_domains_csv",
                ",".join(DEFAULT_COMP_DEALER_DOMAINS),
            )
        )
        with st.expander("Configured Dealer Domains (Comps)", expanded=False):
            st.caption(
                "Domain-aware comp parsing/weighting currently checks: "
                + ", ".join(dealer_domains)
            )
        st.caption(
            f"AI runtime source: `{runtime_cfg.source}` | provider: `{runtime_cfg.provider}` | "
            f"model: `{runtime_cfg.model}` | endpoint: `{runtime_cfg.endpoint_type}` | "
            f"fallback_profiles: `{fallback_profiles}`"
        )
        client = EbayClient()
        ebay_user_access_token = get_runtime_str(
            repo,
            "ebay_user_access_token",
            str(getattr(settings, "ebay_user_access_token", "") or ""),
        ).strip()
        marketplace_insights_scope = EbayClient.MARKETPLACE_INSIGHTS_SCOPE
        marketplace_id = str(getattr(settings, "ebay_marketplace_id", "EBAY_US") or "EBAY_US").strip() or "EBAY_US"
        with st.expander("eBay Sold Comps Sources", expanded=False):
            html_diag = client.sold_html_last_error()
            html_status = "not tested"
            if html_diag:
                status_code = int(html_diag.get("status_code") or 0)
                if status_code == 403:
                    html_status = "blocked"
                elif status_code >= 200 and status_code < 400:
                    html_status = "available"
                else:
                    html_status = f"{html_diag.get('type') or 'checked'}"
            finding_status = "not configured"
            if client.is_configured():
                cooldown = int(client.finding_rate_limit_cooldown_remaining_seconds())
                finding_status = f"{client.environment}/cooldown {cooldown}s" if cooldown > 0 else f"{client.environment}/ready"
            source_status_rows = [
                {
                    "Source": "Marketplace Insights API",
                    "Status": (
                        "ready to test"
                        if ebay_user_access_token or client.is_configured()
                        else "not configured"
                    ),
                    "Detail": "Uses saved user token, or app token with buy.marketplace.insights scope.",
                },
                {
                    "Source": "Finding API",
                    "Status": finding_status,
                    "Detail": str((client.finding_last_error() or {}).get("type") or ""),
                },
                {
                    "Source": "Public sold HTML",
                    "Status": html_status,
                    "Detail": str((html_diag or {}).get("response_excerpt") or "")[:160],
                },
                {
                    "Source": "Manual Terapeak/Product Research CSV import",
                    "Status": "available",
                    "Detail": "Upload CSV above; imported rows are source=ebay_product_research, evidence=sold_market.",
                },
            ]
            st.dataframe(pd.DataFrame(source_status_rows), use_container_width=True, hide_index=True)
            if st.button("Smoke-Test Marketplace Insights", key="comp_marketplace_insights_smoke_btn"):
                smoke_query = str(query or "silver").strip() or "silver"
                smoke_access_token = ebay_user_access_token
                if not smoke_access_token and client.is_configured():
                    try:
                        smoke_token_payload = client.fetch_application_token(scopes=[marketplace_insights_scope])
                        smoke_access_token = str(smoke_token_payload.get("access_token") or "").strip()
                    except Exception as exc:
                        st.error(f"Marketplace Insights app-token request failed: {type(exc).__name__}: {str(exc)[:500]}")
                        smoke_access_token = ""
                if not smoke_access_token:
                    st.warning("Marketplace Insights API is not configured: no eBay access token is available.")
                else:
                    try:
                        smoke_rows = client.search_marketplace_insights_sold_comps(
                            access_token=smoke_access_token,
                            query=smoke_query,
                            marketplace_id=marketplace_id,
                            category_id=str(category_id or ""),
                            limit=1,
                        )
                    except requests.HTTPError as exc:
                        status_code = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
                        status_label = "denied" if status_code in {401, 403} else f"error {status_code or ''}".strip()
                        st.error(f"Marketplace Insights API {status_label}: {str(exc)[:500]}")
                    except Exception as exc:
                        st.error(f"Marketplace Insights API error: {type(exc).__name__}: {str(exc)[:500]}")
                    else:
                        st.success(f"Marketplace Insights API available. Smoke-test rows returned: {len(smoke_rows)}.")
        run_button_label = "Run Photo-Comp Workflow" if photo_workflow_mode else "Run Comp Search"
        run_comp_clicked = st.button(run_button_label, disabled=(not comp_tool_enabled or not can_use_comp_tool))
        run_comp = bool(run_comp_clicked or st.session_state.pop("comp_autorun_once", False))

        if run_comp:
            if not comp_tool_enabled:
                st.error("Comp Tool is disabled by Admin.")
            elif not ensure_permission(user, "ai_comp_use", "Run Comp Tool"):
                pass
            else:
                try:
                    effective_query = str(query or "").strip()
                    if (
                        photo_workflow_mode
                        and auto_seed_query_from_photo
                        and not effective_query
                        and hint_file is not None
                    ):
                        try:
                            generated_query, payload, raw_output = _generate_comp_query_from_hint_image(
                                repo=repo,
                                uploaded_file=hint_file,
                                manual_hint=str(st.session_state.get("comp_manual_hint") or ""),
                            )
                            if generated_query:
                                st.session_state["comp_hint_ai_generated_query"] = generated_query
                                st.session_state["comp_hint_ai_payload"] = payload
                                effective_query = generated_query
                                st.caption(f"Auto-generated photo query: `{generated_query}`")
                            elif raw_output:
                                st.session_state["comp_hint_ai_query_raw"] = raw_output
                        except Exception as auto_seed_exc:
                            st.warning(f"Auto image query seed failed: {auto_seed_exc}")
                    if not effective_query:
                        st.error("Provide query input first.")
                        st.stop()
                    query = effective_query
                    effective_sold_only = bool(sold_only)
                    effective_auto_broaden = bool(auto_broaden)
                    effective_parser_source_filter = list(parser_source_filter or [])
                    effective_min_web_confidence = str(min_web_confidence or "any")
                    effective_min_web_confidence_score = float(min_web_confidence_score or 0.0)
                    effective_domain_include_raw = str(domain_include_raw or "")
                    effective_domain_exclude_raw = str(domain_exclude_raw or "")
                    effective_use_web_fallback = bool(use_web_fallback) or (
                        bool(photo_workflow_mode) and bool(photo_always_include_web_fallback)
                    )
                    effective_use_ai_summary = bool(use_ai_summary) or (
                        bool(photo_workflow_mode) and bool(photo_auto_ai_summary)
                    )
                    retry_profile = str(st.session_state.pop("comp_retry_profile", "") or "").strip().lower()
                    retry_domain_token = str(st.session_state.pop("comp_retry_domain_token", "") or "").strip().lower()
                    preset_overrides = st.session_state.pop("comp_retry_preset_overrides", None)
                    default_preset_overrides = st.session_state.get("comp_retry_default_overrides")
                    if retry_profile:
                        if retry_profile == "web_structured":
                            effective_use_web_fallback = True
                            effective_parser_source_filter = [
                                "domain_specific",
                                "json_ld",
                                "embedded_json",
                                "reader_proxy",
                            ]
                            effective_min_web_confidence_score = max(
                                float(effective_min_web_confidence_score), 0.35
                            )
                        elif retry_profile == "web_broad":
                            effective_use_web_fallback = True
                            effective_parser_source_filter = []
                            effective_min_web_confidence = "any"
                            effective_min_web_confidence_score = 0.0
                            effective_domain_include_raw = ""
                            effective_domain_exclude_raw = ""
                        elif retry_profile == "ebay_broad":
                            effective_sold_only = False
                            effective_auto_broaden = True
                        elif retry_profile == "dealer_focus":
                            effective_use_web_fallback = True
                            effective_parser_source_filter = [
                                "domain_specific",
                                "json_ld",
                                "embedded_json",
                                "reader_proxy",
                            ]
                            effective_min_web_confidence_score = max(
                                float(effective_min_web_confidence_score), 0.2
                            )
                            effective_domain_include_raw = ",".join(list(dealer_domains or ()))
                            effective_domain_exclude_raw = ""
                        elif retry_profile == "domain_focus" and retry_domain_token:
                            effective_use_web_fallback = True
                            effective_parser_source_filter = []
                            effective_min_web_confidence = "any"
                            effective_min_web_confidence_score = 0.0
                            effective_domain_include_raw = retry_domain_token
                            effective_domain_exclude_raw = ""
                    if isinstance(preset_overrides, dict):
                        if "sold_only" in preset_overrides:
                            effective_sold_only = bool(preset_overrides.get("sold_only"))
                        if "auto_broaden" in preset_overrides:
                            effective_auto_broaden = bool(preset_overrides.get("auto_broaden"))
                        if "use_web_fallback" in preset_overrides:
                            effective_use_web_fallback = bool(preset_overrides.get("use_web_fallback"))
                        if "use_ai_summary" in preset_overrides:
                            effective_use_ai_summary = bool(preset_overrides.get("use_ai_summary"))
                        if "parser_source_filter" in preset_overrides:
                            raw_parsers = preset_overrides.get("parser_source_filter") or []
                            if isinstance(raw_parsers, list):
                                effective_parser_source_filter = [
                                    str(v).strip().lower() for v in raw_parsers if str(v).strip()
                                ]
                        if "min_web_confidence" in preset_overrides:
                            effective_min_web_confidence = str(
                                preset_overrides.get("min_web_confidence") or effective_min_web_confidence
                            ).strip().lower() or effective_min_web_confidence
                        if "min_web_confidence_score" in preset_overrides:
                            try:
                                effective_min_web_confidence_score = float(
                                    preset_overrides.get("min_web_confidence_score")
                                )
                            except Exception:
                                pass
                        if "domain_include_raw" in preset_overrides:
                            effective_domain_include_raw = str(
                                preset_overrides.get("domain_include_raw") or ""
                            )
                        if "domain_exclude_raw" in preset_overrides:
                            effective_domain_exclude_raw = str(
                                preset_overrides.get("domain_exclude_raw") or ""
                            )
                        if "web_fallback_limit" in preset_overrides:
                            try:
                                web_fallback_limit = int(preset_overrides.get("web_fallback_limit"))
                            except Exception:
                                pass
                        if "web_detail_fetch_limit" in preset_overrides:
                            try:
                                web_detail_fetch_limit = int(preset_overrides.get("web_detail_fetch_limit"))
                            except Exception:
                                pass
                    elif isinstance(default_preset_overrides, dict):
                        if "sold_only" in default_preset_overrides:
                            effective_sold_only = bool(default_preset_overrides.get("sold_only"))
                        if "auto_broaden" in default_preset_overrides:
                            effective_auto_broaden = bool(default_preset_overrides.get("auto_broaden"))
                        if "use_web_fallback" in default_preset_overrides:
                            effective_use_web_fallback = bool(default_preset_overrides.get("use_web_fallback"))
                        if "use_ai_summary" in default_preset_overrides:
                            effective_use_ai_summary = bool(default_preset_overrides.get("use_ai_summary"))
                        if "parser_source_filter" in default_preset_overrides:
                            raw_parsers = default_preset_overrides.get("parser_source_filter") or []
                            if isinstance(raw_parsers, list):
                                effective_parser_source_filter = [
                                    str(v).strip().lower() for v in raw_parsers if str(v).strip()
                                ]
                        if "min_web_confidence" in default_preset_overrides:
                            effective_min_web_confidence = str(
                                default_preset_overrides.get("min_web_confidence") or effective_min_web_confidence
                            ).strip().lower() or effective_min_web_confidence
                        if "min_web_confidence_score" in default_preset_overrides:
                            try:
                                effective_min_web_confidence_score = float(
                                    default_preset_overrides.get("min_web_confidence_score")
                                )
                            except Exception:
                                pass
                        if "domain_include_raw" in default_preset_overrides:
                            effective_domain_include_raw = str(
                                default_preset_overrides.get("domain_include_raw") or ""
                            )
                        if "domain_exclude_raw" in default_preset_overrides:
                            effective_domain_exclude_raw = str(
                                default_preset_overrides.get("domain_exclude_raw") or ""
                            )
                        if "web_fallback_limit" in default_preset_overrides:
                            try:
                                web_fallback_limit = int(default_preset_overrides.get("web_fallback_limit"))
                            except Exception:
                                pass
                        if "web_detail_fetch_limit" in default_preset_overrides:
                            try:
                                web_detail_fetch_limit = int(default_preset_overrides.get("web_detail_fetch_limit"))
                            except Exception:
                                pass
                    if (
                        save_comp_hint_media
                        and hint_file is not None
                        and comp_media_target_product_id is not None
                        and storage.enabled
                    ):
                        hint_bytes = hint_file.getvalue()
                        uploaded_count, upload_errors = _persist_ai_input_media(
                            repo=repo,
                            storage=storage,
                            files=[
                                (
                                    hint_bytes,
                                    (hint_file.type or "application/octet-stream"),
                                    (hint_file.name or "comp_hint_file"),
                                )
                            ],
                            product_id=comp_media_target_product_id,
                            listing_id=None,
                            uploaded_by=user.username,
                        )
                        if uploaded_count:
                            st.success(f"Saved {uploaded_count} comp hint media file(s) to product media.")
                        for media_error in upload_errors:
                            st.error(f"Comp hint media save failed: {media_error}")

                    attempts: list[dict] = []
                    rows: list[dict] = []
                    web_rows: list[dict] = []
                    if product_research_csv is not None:
                        manual_rows = _parse_ebay_product_research_csv(product_research_csv.getvalue())
                        attempts.append(
                            {
                                "query": str(getattr(product_research_csv, "name", "") or "uploaded_csv"),
                                "sold_only": "manual_import",
                                "results": len(manual_rows),
                                "note": "ebay_product_research_csv_import",
                                "detail": "Manual Product Research/Terapeak CSV rows imported as sold-market evidence.",
                            }
                        )
                        if manual_rows:
                            rows.extend(manual_rows)
                    ebay_api_configured = bool(client.is_configured())
                    if not ebay_api_configured:
                        attempts.append(
                            {
                                "query": effective_query,
                                "sold_only": "n/a",
                                "results": 0,
                                "note": "ebay_api_not_configured_html_still_attempted",
                            }
                        )
                        st.info(
                            "eBay API credentials are not configured; still attempting public eBay sold-result HTML "
                            "and web fallback search."
                        )
                    variants = _query_variants(effective_query)
                    if not variants:
                        variants = [effective_query]

                    for query_try in variants:
                        try:
                            attempt_rows = client.search_sold_items_html(
                                keywords=query_try,
                                limit=int(comp_limit),
                            )
                            html_diag = client.sold_html_last_error()
                            if attempt_rows:
                                attempt_note = "ebay_sold_html_primary"
                            elif html_diag:
                                html_status = int(html_diag.get("status_code") or 0)
                                attempt_note = (
                                    "ebay_sold_html_"
                                    f"{html_diag.get('type') or 'empty'}"
                                    f": status={html_status}"
                                )
                            else:
                                attempt_note = "ebay_sold_html_empty"
                        except Exception as ebay_html_exc:
                            attempt_rows = []
                            attempt_note = f"ebay_sold_html_error: {type(ebay_html_exc).__name__}"
                        attempts.append(
                            {
                                "query": query_try,
                                "sold_only": "true" if bool(effective_sold_only) else "false",
                                "results": len(attempt_rows),
                                "note": attempt_note,
                                "detail": _friendly_ebay_attempt_note(attempt_note),
                            }
                        )
                        if attempt_rows:
                            rows.extend(attempt_rows)
                            break

                    if not rows and ebay_user_access_token:
                        marketplace_insights_access_token = ebay_user_access_token
                    elif not rows and ebay_api_configured:
                        try:
                            token_payload = client.fetch_application_token(scopes=[marketplace_insights_scope])
                            marketplace_insights_access_token = str(token_payload.get("access_token") or "").strip()
                        except Exception as token_exc:
                            marketplace_insights_access_token = ""
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "marketplace_insights",
                                    "results": 0,
                                    "note": "ebay_marketplace_insights_token_error",
                                    "detail": str(token_exc)[:500],
                                }
                            )
                    else:
                        marketplace_insights_access_token = ""

                    if not rows and marketplace_insights_access_token:
                        try:
                            marketplace_insights_rows = client.search_marketplace_insights_sold_comps(
                                access_token=marketplace_insights_access_token,
                                query=effective_query,
                                marketplace_id=marketplace_id,
                                category_id=str(category_id or ""),
                                limit=int(comp_limit),
                            )
                        except requests.HTTPError as insights_exc:
                            status_code = int(
                                getattr(getattr(insights_exc, "response", None), "status_code", 0) or 0
                            )
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "marketplace_insights",
                                    "results": 0,
                                    "note": (
                                        "ebay_marketplace_insights_denied"
                                        if status_code in {401, 403}
                                        else "ebay_marketplace_insights_error"
                                    ),
                                    "detail": str(insights_exc)[:500],
                                }
                            )
                        except Exception as insights_exc:
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "marketplace_insights",
                                    "results": 0,
                                    "note": f"ebay_marketplace_insights_error: {type(insights_exc).__name__}",
                                    "detail": str(insights_exc)[:500],
                                }
                            )
                        else:
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "marketplace_insights",
                                    "results": len(marketplace_insights_rows),
                                    "note": "ebay_marketplace_insights",
                                }
                            )
                            if marketplace_insights_rows:
                                rows.extend(marketplace_insights_rows)
                    elif not rows:
                        attempts.append(
                            {
                                "query": effective_query,
                                "sold_only": "marketplace_insights",
                                "results": 0,
                                "note": "ebay_marketplace_insights_not_configured",
                                "detail": "No saved eBay user access token.",
                            }
                        )

                    if not rows and ebay_api_configured:
                        try:
                            finding_rows = client.find_completed_items(
                                keywords=effective_query,
                                sold_only=bool(effective_sold_only),
                                category_id=str(category_id or ""),
                                entries_per_page=int(comp_limit),
                                source="comp_tool_ui",
                            )
                        except Exception as finding_exc:
                            finding_rows = []
                            finding_diag = client.finding_last_error()
                            finding_detail = str(finding_exc)
                            if finding_diag:
                                finding_detail = (
                                    f"{finding_diag.get('type') or type(finding_exc).__name__}: "
                                    f"{finding_diag.get('response_excerpt') or finding_diag.get('keywords') or finding_detail}"
                                )
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "true" if bool(effective_sold_only) else "false",
                                    "results": 0,
                                    "note": f"ebay_finding_error: {type(finding_exc).__name__}",
                                    "detail": finding_detail[:500],
                                }
                            )
                        else:
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "true" if bool(effective_sold_only) else "false",
                                    "results": len(finding_rows),
                                    "note": f"ebay_finding_{str(getattr(client, 'environment', '') or 'unknown')}",
                                }
                            )
                            if finding_rows:
                                rows = finding_rows

                    if not rows:
                        if effective_use_web_fallback:
                            raw_web_rows = _web_comp_search(
                                effective_query,
                                limit=int(web_fallback_limit),
                                page_fetch_limit=int(web_detail_fetch_limit),
                                dealer_domains=dealer_domains,
                            )
                            web_rows = list(raw_web_rows)
                            if effective_min_web_confidence != "any":
                                allowed = ["very_low", "low", "medium", "high"]
                                min_idx = allowed.index(effective_min_web_confidence)
                                web_rows = [
                                    row
                                    for row in web_rows
                                    if allowed.index(str(row.get("price_confidence_label") or "very_low")) >= min_idx
                                ]
                            if float(effective_min_web_confidence_score) > 0:
                                web_rows = [
                                    row
                                    for row in web_rows
                                    if float(row.get("price_confidence_score") or 0.0)
                                    >= float(effective_min_web_confidence_score)
                                ]
                            if effective_parser_source_filter:
                                allowed_sources = {
                                    str(v).strip().lower() for v in effective_parser_source_filter if str(v).strip()
                                }
                                web_rows = [
                                    row
                                    for row in web_rows
                                    if str(row.get("page_parser_source") or "none").strip().lower() in allowed_sources
                                ]
                            include_tokens = [
                                token.strip().lower()
                                for token in str(effective_domain_include_raw or "").split(",")
                                if token.strip()
                            ]
                            exclude_tokens = [
                                token.strip().lower()
                                for token in str(effective_domain_exclude_raw or "").split(",")
                                if token.strip()
                            ]
                            if include_tokens:
                                web_rows = [
                                    row
                                    for row in web_rows
                                    if any(token in str(row.get("domain") or "").lower() for token in include_tokens)
                                ]
                            if exclude_tokens:
                                web_rows = [
                                    row
                                    for row in web_rows
                                    if not any(token in str(row.get("domain") or "").lower() for token in exclude_tokens)
                                ]
                            if raw_web_rows and not web_rows:
                                st.warning(
                                    "Web fallback returned rows, but active web filters removed all results. "
                                    "Showing unfiltered web rows for this run."
                                )
                                web_rows = raw_web_rows
                            attempts.append(
                                {
                                    "query": effective_query,
                                    "sold_only": "web_fallback",
                                    "results": len(web_rows),
                                    "note": ", ".join(
                                        sorted(
                                            {
                                                str(row.get("search_provider") or "web").strip()
                                                for row in web_rows
                                            }
                                        )
                                    )
                                    or "web_fallback_no_rows",
                                }
                            )
                    elif photo_workflow_mode and photo_always_include_web_fallback:
                        web_rows = _web_comp_search(
                            effective_query,
                            limit=int(web_fallback_limit),
                            page_fetch_limit=int(web_detail_fetch_limit),
                            dealer_domains=dealer_domains,
                        )
                        attempts.append(
                            {
                                "query": effective_query,
                                "sold_only": "web_fallback_overlay",
                                "results": len(web_rows),
                            }
                        )

                    if not rows and not web_rows:
                        st.warning("No comps returned for this query.")
                        if photo_workflow_mode:
                            retry_run_label = str(st.session_state.pop("comp_retry_run_label", "") or "").strip()
                            active_default_label = str(st.session_state.get("comp_retry_default_label") or "").strip()
                            strategy = "manual"
                            if retry_profile:
                                strategy = retry_profile
                            elif isinstance(preset_overrides, dict):
                                strategy = "preset_override"
                            elif isinstance(default_preset_overrides, dict):
                                strategy = "default_preset"
                            try:
                                repo.record_audit_event(
                                    entity_type="comp_photo_retry",
                                    entity_id=None,
                                    action="run",
                                    actor=user.username,
                                    changes={
                                        "query": effective_query,
                                        "strategy": strategy,
                                        "run_label": retry_run_label,
                                        "default_preset_label": active_default_label,
                                        "rows_total": 0,
                                        "rows_priced": 0,
                                        "rows_missing_price": 0,
                                        "coverage_pct": 0.0,
                                        "web_rows_total": 0,
                                        "web_rows_priced": 0,
                                        "web_rows_missing_price": 0,
                                        "top_missing_domains_json": "[]",
                                        "top_priced_domains_json": "[]",
                                        "used_web_fallback": bool(effective_use_web_fallback),
                                        "used_ai_summary": bool(effective_use_ai_summary),
                                        "sold_only": bool(effective_sold_only),
                                        "auto_broaden": bool(effective_auto_broaden),
                                        "web_fallback_limit": int(web_fallback_limit),
                                        "web_detail_fetch_limit": int(web_detail_fetch_limit),
                                        "min_web_confidence": str(effective_min_web_confidence or "any"),
                                        "min_web_confidence_score": float(effective_min_web_confidence_score),
                                        "parser_source_filter": list(effective_parser_source_filter or []),
                                        "domain_include_raw": str(effective_domain_include_raw or ""),
                                        "domain_exclude_raw": str(effective_domain_exclude_raw or ""),
                                        "result": "no_rows",
                                    },
                                )
                            except Exception:
                                pass
                        env_label = str(getattr(settings, "ebay_environment", "sandbox") or "sandbox").strip().lower()
                        if env_label == "sandbox":
                            st.caption(
                                "This is common in sandbox data. Try broader keywords (brand + item type), "
                                "disable sold-only, or run against production credentials."
                            )
                        else:
                            st.caption(
                                "Try broader keywords (brand + item type) and optionally disable sold-only. "
                                "Legacy Finding diagnostics are informational only."
                            )
                        st.dataframe(pd.DataFrame(_format_attempt_rows(attempts)), use_container_width=True)
                    else:
                        effective_rows = rows if rows else web_rows
                        priced_rows = [r for r in effective_rows if _effective_total_price(r) > 0]
                        qualified_rows, qualification = _qualified_comp_rows(priced_rows)
                        stats_rows = qualified_rows or priced_rows or effective_rows
                        stats = _comp_stats(stats_rows)
                        cost_breakdown = _comp_cost_breakdown(stats_rows)
                        evidence_quality = _comp_evidence_quality(rows, web_rows)
                        m1, m2, m3, m4, m5 = st.columns(5)
                        m1.metric("Comps", int(stats["count"]))
                        m2.metric("Median Total", f"${stats['median']:,.2f}")
                        m3.metric("Average Total", f"${stats['avg']:,.2f}")
                        m4.metric("Range", f"${stats['low']:,.2f} - ${stats['high']:,.2f}")
                        m5.metric("Evidence", str(evidence_quality["label"]).replace("_", " ").title())
                        b1, b2, b3 = st.columns(3)
                        b1.metric("Avg Sold Price", f"${cost_breakdown['sold_avg']:,.2f}")
                        b2.metric("Avg Shipping", f"${cost_breakdown['shipping_avg']:,.2f}")
                        b3.metric("Shipping % of Total", f"{cost_breakdown['shipping_pct_of_total']:.1f}%")
                        c1, c2 = st.columns(2)
                        c1.metric("Avg Listed Price", f"${cost_breakdown['listed_avg']:,.2f}")
                        c2.metric("Avg Effective Item Price", f"${cost_breakdown['item_avg']:,.2f}")
                        st.caption(
                            f"Suggested list range (90%-110% median): "
                            f"${stats['median'] * 0.9:,.2f} - ${stats['median'] * 1.1:,.2f}"
                        )
                        if int(qualification.get("removed_rows") or 0) > 0:
                            st.caption(
                                "Qualified price filter removed "
                                f"{int(qualification.get('removed_rows') or 0)} outlier row(s). "
                                f"Method: `{qualification.get('method')}`; "
                                f"kept ${float(qualification.get('low_cut') or 0):,.2f}-"
                                f"${float(qualification.get('high_cut') or 0):,.2f}."
                            )
                            with st.expander("Qualified Price Filter Removed Rows", expanded=False):
                                st.dataframe(
                                    pd.DataFrame(qualification.get("removed_samples") or []),
                                    use_container_width=True,
                                    hide_index=True,
                                )
                        st.caption(str(evidence_quality["note"]))
                        st.caption(
                            "Evidence mix: "
                            f"sold eBay rows={int(evidence_quality['sold_rows'])}, "
                            f"priced web rows={int(evidence_quality['web_priced_rows'])}, "
                            f"high-confidence web rows={int(evidence_quality['web_high_confidence_rows'])}, "
                            f"configured-dealer rows={int(evidence_quality['configured_dealer_rows'])}."
                        )
                        quality_diagnostics = _comp_quality_diagnostics(attempts, rows, web_rows)
                        with st.expander("Comp Run Diagnostics", expanded=False):
                            st.dataframe(
                                pd.DataFrame(_comp_quality_diagnostics_rows(quality_diagnostics)),
                                use_container_width=True,
                                hide_index=True,
                            )
                        evidence_export_payload = _comp_evidence_export_payload(
                            query=effective_query,
                            attempts=attempts,
                            rows=rows,
                            web_rows=web_rows,
                            stats=stats,
                            cost_breakdown=cost_breakdown,
                            evidence_quality=evidence_quality,
                            diagnostics=quality_diagnostics,
                            spot_context={},
                        )
                        evidence_export_payload["qualification"] = qualification
                        st.download_button(
                            "Download Comp Evidence JSON",
                            data=json.dumps(evidence_export_payload, indent=2, default=str).encode("utf-8"),
                            file_name="comp_evidence_package.json",
                            mime="application/json",
                            key="download_comp_evidence_json",
                        )
                        if web_rows and not rows:
                            priced_count = sum(1 for r in web_rows if _effective_total_price(r) > 0)
                            st.caption(
                                f"Web hints shown: {len(web_rows)} total, {priced_count} with explicit price hints."
                            )
                        if attempts:
                            st.caption("Search strategy used:")
                            st.dataframe(pd.DataFrame(_format_attempt_rows(attempts)), use_container_width=True)
                        if rows:
                            st.markdown("##### eBay Comparable Results")
                            st.dataframe(pd.DataFrame(rows), use_container_width=True)
                        if web_rows:
                            st.markdown("##### Web Fallback Comparable Hints")
                            st.dataframe(pd.DataFrame(web_rows), use_container_width=True)
                            web_df = pd.DataFrame(web_rows)
                            if not web_df.empty:
                                source_summary = (
                                    web_df.groupby(
                                        ["search_scope", "search_provider", "page_parser_source", "price_confidence_label"],
                                        dropna=False,
                                    )
                                    .size()
                                    .reset_index(name="rows")
                                    .sort_values(["rows"], ascending=[False])
                                )
                                domain_summary = (
                                    web_df.groupby(["domain"], dropna=False)
                                    .agg(
                                        rows=("domain", "size"),
                                        priced_rows=("listed_price", lambda s: int((pd.to_numeric(s, errors="coerce").fillna(0) > 0).sum())),
                                    )
                                    .reset_index()
                                    .sort_values(["rows"], ascending=[False])
                                )
                                st.caption("Web parser coverage summary")
                                st.dataframe(source_summary, use_container_width=True)
                                st.caption("Web domain capture summary")
                                st.dataframe(domain_summary, use_container_width=True)

                        if photo_workflow_mode:
                            st.markdown("##### Photo-Comp Quality")
                            combined_rows = list(rows or []) + list(web_rows or [])
                            total_rows = len(combined_rows)
                            priced_rows_count = sum(
                                1 for row in combined_rows if float(_effective_total_price(row)) > 0
                            )
                            missing_price_rows = max(0, int(total_rows - priced_rows_count))
                            coverage_pct = (float(priced_rows_count) / float(total_rows) * 100.0) if total_rows > 0 else 0.0
                            web_total_rows = len(web_rows or [])
                            web_priced_rows = sum(
                                1 for row in (web_rows or []) if float(_effective_total_price(row)) > 0
                            )
                            web_missing_rows = max(0, int(web_total_rows - web_priced_rows))
                            web_missing_domain_counts: dict[str, int] = {}
                            web_priced_domain_counts: dict[str, int] = {}
                            for row in (web_rows or []):
                                domain = str(row.get("domain") or "").strip().lower()
                                if not domain:
                                    continue
                                if float(_effective_total_price(row)) > 0:
                                    web_priced_domain_counts[domain] = int(
                                        web_priced_domain_counts.get(domain, 0) + 1
                                    )
                                else:
                                    web_missing_domain_counts[domain] = int(
                                        web_missing_domain_counts.get(domain, 0) + 1
                                    )
                            top_missing_domains = sorted(
                                web_missing_domain_counts.items(),
                                key=lambda kv: int(kv[1]),
                                reverse=True,
                            )[:10]
                            top_priced_domains = sorted(
                                web_priced_domain_counts.items(),
                                key=lambda kv: int(kv[1]),
                                reverse=True,
                            )[:10]
                            q1, q2, q3, q4 = st.columns(4)
                            q1.metric("Combined Rows", int(total_rows))
                            q2.metric("Rows With Price", int(priced_rows_count))
                            q3.metric("Coverage %", f"{coverage_pct:.1f}%")
                            q4.metric("Missing Price Rows", int(missing_price_rows))
                            retry_run_label = str(st.session_state.pop("comp_retry_run_label", "") or "").strip()
                            active_default_label = str(st.session_state.get("comp_retry_default_label") or "").strip()
                            strategy = "manual"
                            if retry_profile:
                                strategy = retry_profile
                            elif isinstance(preset_overrides, dict):
                                strategy = "preset_override"
                            elif isinstance(default_preset_overrides, dict):
                                strategy = "default_preset"
                            try:
                                repo.record_audit_event(
                                    entity_type="comp_photo_retry",
                                    entity_id=None,
                                    action="run",
                                    actor=user.username,
                                    changes={
                                        "query": effective_query,
                                        "strategy": strategy,
                                        "run_label": retry_run_label,
                                        "default_preset_label": active_default_label,
                                        "rows_total": int(total_rows),
                                        "rows_priced": int(priced_rows_count),
                                        "rows_missing_price": int(missing_price_rows),
                                        "coverage_pct": round(float(coverage_pct), 2),
                                        "web_rows_total": int(web_total_rows),
                                        "web_rows_priced": int(web_priced_rows),
                                        "web_rows_missing_price": int(web_missing_rows),
                                        "top_missing_domains_json": json.dumps(top_missing_domains),
                                        "top_priced_domains_json": json.dumps(top_priced_domains),
                                        "used_web_fallback": bool(effective_use_web_fallback),
                                        "used_ai_summary": bool(effective_use_ai_summary),
                                        "sold_only": bool(effective_sold_only),
                                        "auto_broaden": bool(effective_auto_broaden),
                                        "web_fallback_limit": int(web_fallback_limit),
                                        "web_detail_fetch_limit": int(web_detail_fetch_limit),
                                        "min_web_confidence": str(effective_min_web_confidence or "any"),
                                        "min_web_confidence_score": float(effective_min_web_confidence_score),
                                        "parser_source_filter": list(effective_parser_source_filter or []),
                                        "domain_include_raw": str(effective_domain_include_raw or ""),
                                        "domain_exclude_raw": str(effective_domain_exclude_raw or ""),
                                    },
                                )
                            except Exception:
                                pass
                            if web_total_rows > 0:
                                st.caption(
                                    f"Web fallback rows: {web_total_rows} total, {web_priced_rows} priced, "
                                    f"{web_missing_rows} missing explicit price."
                                )
                            if missing_price_rows > 0:
                                st.warning(
                                    "Some rows are still missing parsed prices. Use a retry profile below to re-run quickly."
                                )
                            rr1, rr2, rr3 = st.columns(3)
                            with rr1:
                                if st.button("Retry: Web Structured", key="comp_retry_web_structured_btn"):
                                    st.session_state["comp_retry_profile"] = "web_structured"
                                    st.session_state["comp_retry_run_label"] = "Retry: Web Structured"
                                    st.session_state["comp_autorun_once"] = True
                                    st.rerun()
                            with rr2:
                                if st.button("Retry: Web Broad", key="comp_retry_web_broad_btn"):
                                    st.session_state["comp_retry_profile"] = "web_broad"
                                    st.session_state["comp_retry_run_label"] = "Retry: Web Broad"
                                    st.session_state["comp_autorun_once"] = True
                                    st.rerun()
                            with rr3:
                                if st.button("Retry: eBay Broad", key="comp_retry_ebay_broad_btn"):
                                    st.session_state["comp_retry_profile"] = "ebay_broad"
                                    st.session_state["comp_retry_run_label"] = "Retry: eBay Broad"
                                    st.session_state["comp_autorun_once"] = True
                                    st.rerun()
                            rr4, rr5 = st.columns(2)
                            with rr4:
                                if st.button("Retry: Dealer Domains", key="comp_retry_dealer_focus_btn"):
                                    st.session_state["comp_retry_profile"] = "dealer_focus"
                                    st.session_state["comp_retry_run_label"] = "Retry: Dealer Domains"
                                    st.session_state["comp_autorun_once"] = True
                                    st.rerun()
                            with rr5:
                                st.caption("Dealer scope uses Admin Comp Config domain list.")

                            st.markdown("###### Saved Photo-Comp Retry Presets")
                            preset_scope = "tools_photo_comp_retry"
                            preset_rows = repo.list_saved_filter_profiles(
                                environment=settings.app_env,
                                scope=preset_scope,
                                username=user.username,
                                include_shared=True,
                                active_only=True,
                            )
                            preset_options = ["(none)"]
                            preset_row_map: dict[str, Any] = {}
                            for preset_row in preset_rows:
                                visibility = "Shared" if bool(preset_row.is_shared) else "Mine"
                                owner_tag = (
                                    f" | Owner:{preset_row.username}"
                                    if bool(preset_row.is_shared)
                                    else ""
                                )
                                default_tag = " | Default" if bool(preset_row.is_default) else ""
                                label = f"{preset_row.name} [{visibility}{owner_tag}{default_tag}]"
                                if label in preset_row_map:
                                    label = f"{label} #{preset_row.id}"
                                preset_row_map[label] = preset_row
                                preset_options.append(label)
                            default_load_key = (
                                f"tools_photo_comp_retry_default_loaded_{settings.app_env}_{user.username}"
                            )
                            if default_load_key not in st.session_state:
                                st.session_state[default_load_key] = False
                            if not st.session_state.get(default_load_key):
                                own_default_row = None
                                shared_default_row = None
                                for row in preset_rows:
                                    if not bool(row.is_default):
                                        continue
                                    if (
                                        str(row.username or "").strip().lower()
                                        == str(user.username or "").strip().lower()
                                        and not bool(row.is_shared)
                                    ):
                                        own_default_row = row
                                        break
                                    if bool(row.is_shared) and shared_default_row is None:
                                        shared_default_row = row
                                default_row = own_default_row or shared_default_row
                                if default_row is not None:
                                    default_payload = _parse_photo_comp_retry_preset(default_row.filter_json)
                                    if default_payload:
                                        st.session_state["comp_retry_default_overrides"] = default_payload
                                        st.session_state["comp_retry_default_label"] = str(default_row.name or "").strip()
                                        st.session_state[default_load_key] = True
                                        st.caption(
                                            f"Loaded default retry preset: `{default_row.name}` "
                                            f"({'shared' if bool(default_row.is_shared) else 'mine'})"
                                        )
                            selected_preset_label = st.selectbox(
                                "Retry Preset",
                                options=preset_options,
                                key="tools_photo_comp_retry_preset_select",
                            )
                            pr1, pr2, pr3, pr4 = st.columns(4)
                            with pr1:
                                if st.button("Apply Retry Preset", key="tools_photo_comp_retry_preset_apply_btn"):
                                    selected_row = preset_row_map.get(selected_preset_label)
                                    if selected_row is None:
                                        st.error("Select a preset first.")
                                    else:
                                        payload = _parse_photo_comp_retry_preset(selected_row.filter_json)
                                        if payload:
                                            st.session_state["comp_retry_preset_overrides"] = payload
                                            st.session_state["comp_retry_default_overrides"] = payload
                                            st.session_state["comp_retry_default_label"] = str(selected_row.name or "").strip()
                                            st.session_state["comp_retry_run_label"] = (
                                                f"Retry Preset: {str(selected_row.name or '').strip()}"
                                            )
                                            st.session_state["comp_autorun_once"] = True
                                            st.rerun()
                                        else:
                                            st.error("Preset payload is empty/invalid.")
                            with pr2:
                                with st.form("tools_photo_comp_retry_preset_save_form"):
                                    save_name = st.text_input(
                                        "Save Current As",
                                        key="tools_photo_comp_retry_preset_name",
                                    )
                                    save_shared = st.checkbox(
                                        "Team-shared",
                                        value=False,
                                        key="tools_photo_comp_retry_preset_shared",
                                    )
                                    save_default = st.checkbox(
                                        "Set as default",
                                        value=False,
                                        key="tools_photo_comp_retry_preset_default",
                                    )
                                    save_preset_clicked = st.form_submit_button("Save Preset")
                                if save_preset_clicked:
                                    resolved_name = str(save_name or "").strip()
                                    if not resolved_name:
                                        st.error("Preset name is required.")
                                    else:
                                        payload = {
                                            "sold_only": bool(effective_sold_only),
                                            "auto_broaden": bool(effective_auto_broaden),
                                            "use_web_fallback": bool(effective_use_web_fallback),
                                            "use_ai_summary": bool(effective_use_ai_summary),
                                            "web_fallback_limit": int(web_fallback_limit),
                                            "web_detail_fetch_limit": int(web_detail_fetch_limit),
                                            "min_web_confidence": str(effective_min_web_confidence or "any"),
                                            "min_web_confidence_score": float(effective_min_web_confidence_score),
                                            "parser_source_filter": list(effective_parser_source_filter or []),
                                            "domain_include_raw": str(effective_domain_include_raw or ""),
                                            "domain_exclude_raw": str(effective_domain_exclude_raw or ""),
                                        }
                                        repo.upsert_saved_filter_profile(
                                            environment=settings.app_env,
                                            username=user.username,
                                            scope=preset_scope,
                                            name=resolved_name,
                                            filter_json=json.dumps(payload),
                                            is_shared=bool(save_shared),
                                            is_default=bool(save_default),
                                            is_active=True,
                                            actor=user.username,
                                        )
                                        st.success(f"Saved retry preset `{resolved_name}`.")
                                        st.rerun()
                            with pr3:
                                if st.button("Delete Retry Preset", key="tools_photo_comp_retry_preset_delete_btn"):
                                    selected_row = preset_row_map.get(selected_preset_label)
                                    if selected_row is None:
                                        st.error("Select a preset first.")
                                    elif str(selected_row.username or "").strip() != str(user.username or "").strip():
                                        st.error("Only preset owner can delete this preset.")
                                    else:
                                        repo.delete_saved_filter_profile_by_id(
                                            profile_id=int(selected_row.id),
                                            actor=user.username,
                                        )
                                        st.success("Deleted retry preset.")
                                        st.rerun()
                            with pr4:
                                if st.button("Clear Active Default", key="tools_photo_comp_retry_clear_default_btn"):
                                    st.session_state.pop("comp_retry_default_overrides", None)
                                    st.session_state.pop("comp_retry_default_label", None)
                                    st.session_state[default_load_key] = True
                                    st.success("Cleared active default retry preset for this session.")

                            if web_total_rows > 0 and web_missing_rows > 0:
                                missing_domain_counts: dict[str, int] = {}
                                for row in (web_rows or []):
                                    domain = str(row.get("domain") or "").strip().lower()
                                    if not domain:
                                        continue
                                    if float(_effective_total_price(row)) > 0:
                                        continue
                                    missing_domain_counts[domain] = int(missing_domain_counts.get(domain, 0) + 1)
                                missing_domain_options = sorted(
                                    missing_domain_counts.keys(),
                                    key=lambda d: int(missing_domain_counts.get(d, 0)),
                                    reverse=True,
                                )
                                if missing_domain_options:
                                    d1, d2 = st.columns([3, 1])
                                    with d1:
                                        selected_missing_domain = st.selectbox(
                                            "Retry Target Domain (missing-price focus)",
                                            options=missing_domain_options,
                                            format_func=lambda d: f"{d} ({missing_domain_counts.get(d, 0)} missing)",
                                            key="comp_retry_missing_domain_pick",
                                        )
                                    with d2:
                                        if st.button("Retry: Domain Focus", key="comp_retry_domain_focus_btn"):
                                            st.session_state["comp_retry_profile"] = "domain_focus"
                                            st.session_state["comp_retry_domain_token"] = str(
                                                selected_missing_domain or ""
                                            ).strip().lower()
                                            st.session_state["comp_retry_run_label"] = (
                                                f"Retry: Domain Focus ({str(selected_missing_domain or '').strip().lower()})"
                                            )
                                            st.session_state["comp_autorun_once"] = True
                                            st.rerun()
                            telemetry_logs = repo.list_audit_logs(limit=500)
                            telemetry_rows: list[dict[str, Any]] = []
                            for log in telemetry_logs:
                                if str(log.entity_type or "").strip().lower() != "comp_photo_retry":
                                    continue
                                if str(log.action or "").strip().lower() != "run":
                                    continue
                                payload = _parse_photo_comp_retry_preset(log.changes_json)
                                telemetry_rows.append(
                                    {
                                        "time": log.created_at,
                                        "actor": log.actor,
                                        "strategy": str(payload.get("strategy") or ""),
                                        "run_label": str(payload.get("run_label") or ""),
                                        "query": str(payload.get("query") or ""),
                                        "coverage_pct": float(payload.get("coverage_pct") or 0.0),
                                        "rows_total": int(payload.get("rows_total") or 0),
                                        "rows_priced": int(payload.get("rows_priced") or 0),
                                        "rows_missing_price": int(payload.get("rows_missing_price") or 0),
                                    }
                                )
                                if len(telemetry_rows) >= 20:
                                    break
                            if telemetry_rows:
                                st.caption("Recent Retry Telemetry")
                                st.dataframe(pd.DataFrame(telemetry_rows), use_container_width=True)

                        spot_context: dict = {}
                        if include_spot_context:
                            try:
                                quotes = spot.latest_quotes()
                                spot_context["quotes_usd_per_troy_oz"] = {
                                    metal: float(quote.usd_per_troy_oz) for metal, quote in quotes.items()
                                }
                                if quotes:
                                    any_quote = next(iter(quotes.values()))
                                    spot_context["as_of"] = any_quote.as_of.isoformat()
                                    spot_context["source"] = any_quote.source
                            except Exception as exc:
                                spot_context["fetch_error"] = str(exc)
                            detected_metal = _detect_metal_from_query(effective_query)
                            product_metal = (
                                (selected_product.metal_type or "").strip().lower()
                                if selected_product is not None
                                else ""
                            )
                            spot_context["detected_metal"] = product_metal or detected_metal
                            if selected_product is not None and selected_product.weight_oz is not None:
                                spot_context["product_weight_oz"] = float(selected_product.weight_oz)
                                spot_context["product_sku"] = (selected_product.sku or "").strip()
                                spot_context["product_category"] = (selected_product.category or "").strip()
                        spot_context["comp_cost_breakdown"] = {
                            "stats_source": "qualified_rows" if qualified_rows else ("priced_rows" if priced_rows else "all_rows"),
                            "avg_item_price": cost_breakdown["item_avg"],
                            "avg_shipping_cost": cost_breakdown["shipping_avg"],
                            "avg_total_price": cost_breakdown["total_avg"],
                            "shipping_pct_of_total": cost_breakdown["shipping_pct_of_total"],
                            "qualification": qualification,
                        }
                        spot_context["comp_evidence_quality"] = evidence_quality
                        st.session_state["comp_last_spot_context"] = spot_context
                        evidence_export_payload["spot_context"] = spot_context
                        ai_web_rows = _filter_ai_web_comp_rows(web_rows)
                        st.session_state["comp_last_query"] = effective_query
                        st.session_state["comp_last_ebay_rows"] = rows
                        st.session_state["comp_last_web_rows"] = web_rows
                        st.session_state["comp_last_ai_web_rows"] = ai_web_rows
                        st.session_state["comp_last_product_context"] = {
                            "sku": (selected_product.sku or "").strip() if selected_product is not None else "",
                            "metal_type": (selected_product.metal_type or "").strip() if selected_product is not None else "",
                            "weight_oz": float(selected_product.weight_oz) if selected_product is not None and selected_product.weight_oz is not None else 0.0,
                            "category": (selected_product.category or "").strip() if selected_product is not None else "",
                        }
                        if effective_use_ai_summary:
                            try:
                                comp_result = execute_comp_summary(
                                    repo,
                                    query=effective_query,
                                    ebay_rows=rows,
                                    web_rows=ai_web_rows,
                                    spot_context=spot_context,
                                    system_message=comp_system_message,
                                    instruction=comp_instruction,
                                )
                                summary = comp_result.text
                                used_cfg = comp_result.used_config
                                fallback_errors = comp_result.fallback_errors
                                st.markdown("##### AI Comp Summary")
                                st.markdown(summary)
                                st.caption(
                                    f"AI profile used: `{used_cfg.provider}` / `{used_cfg.model}`. "
                                    f"fallback_attempts: `{len(fallback_errors)}`"
                                )
                                comp_citation = comp_result.citation
                                with st.expander("AI Citation", expanded=False):
                                    st.code(json.dumps(comp_citation, indent=2), language="json")
                                st.session_state["comp_last_ai_citation"] = comp_citation
                                st.session_state["comp_last_ai_summary"] = summary
                                if comp_screenshot_files:
                                    screenshot_instruction = (
                                        "Review the provided comp screenshots and add concise notes about: "
                                        "observed listed prices, shipping mentions, condition cues, and any mismatch "
                                        "against parsed comp rows. Keep response short markdown bullets."
                                        f"\n\nQuery: {effective_query}\n"
                                        f"Top parsed rows sample: {json.dumps((effective_rows or [])[:8])}"
                                    )
                                    primary = comp_screenshot_files[0]
                                    primary_bytes = primary.getvalue()
                                    additional = []
                                    for extra in comp_screenshot_files[1:4]:
                                        additional.append((extra.getvalue(), extra.type or "image/jpeg"))
                                    screenshot_result = execute_multimodal_task(
                                        repo,
                                        tool_name="comp_screenshot_review",
                                        system_message=(
                                            "You are a resale pricing analyst reviewing screenshot evidence."
                                        ),
                                        instruction=screenshot_instruction,
                                        image_bytes=primary_bytes,
                                        image_content_type=primary.type or "image/jpeg",
                                        additional_images=additional,
                                        max_output_tokens_override=max(
                                            int(runtime_cfg.max_output_tokens),
                                            int(get_runtime_int(repo, "comp_screenshot_ai_max_output_tokens", 900)),
                                        ),
                                        context={
                                            "query": effective_query,
                                            "screenshots_count": len(comp_screenshot_files or []),
                                        },
                                    )
                                    screenshot_notes = screenshot_result.text
                                    screenshot_cfg = screenshot_result.used_config
                                    screenshot_fallback_errors = screenshot_result.fallback_errors
                                    st.markdown("##### AI Screenshot Review")
                                    st.markdown(screenshot_notes)
                                    st.caption(
                                        f"AI profile used: `{screenshot_cfg.provider}` / "
                                        f"`{screenshot_cfg.multimodal_model or screenshot_cfg.model}`. "
                                        f"fallback_attempts: `{len(screenshot_fallback_errors)}`"
                                    )
                                    screenshot_citation = screenshot_result.citation
                                    with st.expander("AI Screenshot Citation", expanded=False):
                                        st.code(json.dumps(screenshot_citation, indent=2), language="json")
                                    st.session_state["comp_last_ai_screenshot_review"] = screenshot_notes
                            except Exception as exc:
                                st.error(f"AI comp synthesis failed: {exc}")
                except Exception as exc:
                    st.error(f"Comp search failed: {exc}")

        if st.button(
            "Generate AI Summary From Last Comp Run",
            disabled=(not comp_tool_enabled or not can_use_comp_tool),
        ):
            try:
                if not comp_tool_enabled:
                    st.error("Comp Tool is disabled by Admin.")
                    raise RuntimeError("comp_tool_disabled")
                if not ensure_permission(user, "ai_comp_use", "Generate AI Comp Summary"):
                    raise RuntimeError("comp_tool_no_permission")
                    last_query = str(st.session_state.get("comp_last_query") or "").strip()
                    if not last_query:
                        st.error("Run a comp search first.")
                    else:
                        last_ebay_rows = st.session_state.get("comp_last_ebay_rows") or []
                        last_web_rows = st.session_state.get("comp_last_web_rows") or []
                        last_ai_web_rows = st.session_state.get("comp_last_ai_web_rows")
                        if last_ai_web_rows is None:
                            last_ai_web_rows = _filter_ai_web_comp_rows(last_web_rows)
                        last_spot_context = st.session_state.get("comp_last_spot_context") or {}
                        comp_result = execute_comp_summary(
                            repo,
                            query=last_query,
                            ebay_rows=last_ebay_rows,
                            web_rows=last_ai_web_rows,
                            spot_context=last_spot_context,
                            system_message=comp_system_message,
                            instruction=comp_instruction,
                        )
                    summary = comp_result.text
                    used_cfg = comp_result.used_config
                    fallback_errors = comp_result.fallback_errors
                    st.markdown("##### AI Comp Summary")
                    st.markdown(summary)
                    st.caption(
                        f"AI profile used: `{used_cfg.provider}` / `{used_cfg.model}`. "
                        f"fallback_attempts: `{len(fallback_errors)}`"
                    )
                    comp_citation = comp_result.citation
                    with st.expander("AI Citation", expanded=False):
                        st.code(json.dumps(comp_citation, indent=2), language="json")
                    st.session_state["comp_last_ai_citation"] = comp_citation
                    st.session_state["comp_last_ai_summary"] = summary
                    if comp_screenshot_files:
                        screenshot_instruction = (
                            "Review the provided comp screenshots and add concise notes about: "
                            "observed listed prices, shipping mentions, condition cues, and any mismatch "
                            "against parsed comp rows. Keep response short markdown bullets."
                            f"\n\nQuery: {last_query}\n"
                            f"Top parsed rows sample: {json.dumps(((last_ebay_rows or last_web_rows) or [])[:8])}"
                        )
                        primary = comp_screenshot_files[0]
                        additional = [(f.getvalue(), f.type or "image/jpeg") for f in comp_screenshot_files[1:4]]
                        screenshot_result = execute_multimodal_task(
                            repo,
                            tool_name="comp_screenshot_review",
                            system_message=(
                                "You are a resale pricing analyst reviewing screenshot evidence."
                            ),
                            instruction=screenshot_instruction,
                            image_bytes=primary.getvalue(),
                            image_content_type=primary.type or "image/jpeg",
                            additional_images=additional,
                            max_output_tokens_override=max(
                                int(runtime_cfg.max_output_tokens),
                                int(get_runtime_int(repo, "comp_screenshot_ai_max_output_tokens", 900)),
                            ),
                            context={
                                "query": last_query,
                                "screenshots_count": len(comp_screenshot_files or []),
                            },
                        )
                        screenshot_notes = screenshot_result.text
                        screenshot_cfg = screenshot_result.used_config
                        screenshot_fallback_errors = screenshot_result.fallback_errors
                        st.markdown("##### AI Screenshot Review")
                        st.markdown(screenshot_notes)
                        st.caption(
                            f"AI profile used: `{screenshot_cfg.provider}` / "
                            f"`{screenshot_cfg.multimodal_model or screenshot_cfg.model}`. "
                            f"fallback_attempts: `{len(screenshot_fallback_errors)}`"
                        )
                        screenshot_citation = screenshot_result.citation
                        with st.expander("AI Screenshot Citation", expanded=False):
                            st.code(json.dumps(screenshot_citation, indent=2), language="json")
                        st.session_state["comp_last_ai_screenshot_review"] = screenshot_notes
            except Exception as exc:
                if str(exc) in {"comp_tool_disabled", "comp_tool_no_permission"}:
                    pass
                else:
                    st.error(f"AI comp synthesis failed: {exc}")

        if selected_product is not None and st.session_state.get("comp_last_ai_summary"):
            if st.button(
                "Apply Last AI Comp Summary To Selected Product",
                key="apply_comp_summary_to_product",
                disabled=(not comp_tool_enabled or not can_use_comp_tool),
            ):
                if not comp_tool_enabled:
                    st.error("Comp Tool is disabled by Admin.")
                elif not ensure_permission(user, "ai_comp_use", "Apply AI Comp Summary"):
                    pass
                else:
                    try:
                        repo.update_product(
                            selected_product.id,
                            {"ai_comp": str(st.session_state.get("comp_last_ai_summary") or "").strip()},
                            actor=user.username,
                        )
                        st.success(f"Updated product #{selected_product.id} AI Comp from last comp summary.")
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Unable to update product AI Comp: {exc}")

        if source_mode == "Image/File Hint":
            st.markdown("#### Photo-Comp Product Draft")
            ai_payload = st.session_state.get("comp_hint_ai_payload") or {}
            ai_item_summary = str(ai_payload.get("item_summary") or "").strip()
            ai_condition_hint = str(ai_payload.get("condition_hint") or "").strip()
            draft_default_title = ai_item_summary or query or "AI Photo-Comp Draft Item"
            draft_default_desc = ai_condition_hint
            d1, d2, d3, d4 = st.columns(4)
            with d1:
                draft_category = st.selectbox(
                    "Draft Category",
                    ["bullion", "coins", "collectibles", "antiques", "normal_goods", "other"],
                    index=2,
                    key="comp_photo_draft_category",
                )
            with d2:
                draft_metal = st.text_input(
                    "Draft Metal Type",
                    value=_detect_metal_from_query(query) or "",
                    key="comp_photo_draft_metal",
                )
            with d3:
                draft_qty = st.number_input(
                    "Draft Quantity",
                    min_value=0,
                    value=1,
                    step=1,
                    key="comp_photo_draft_qty",
                )
            with d4:
                draft_cost = st.number_input(
                    "Draft Unit Cost",
                    min_value=0.0,
                    value=0.0,
                    step=1.0,
                    key="comp_photo_draft_cost",
                )
            draft_title = st.text_input(
                "Draft Title",
                value=draft_default_title,
                key="comp_photo_draft_title",
            )
            draft_description = st.text_area(
                "Draft Description",
                value=draft_default_desc,
                key="comp_photo_draft_description",
            )
            last_comp_rows = (st.session_state.get("comp_last_ebay_rows") or []) + (
                st.session_state.get("comp_last_web_rows") or []
            )
            last_comp_prices = sorted(
                [float(_effective_total_price(row)) for row in last_comp_rows if float(_effective_total_price(row)) > 0]
            )
            suggested_listing_price = 0.0
            if last_comp_prices:
                mid = len(last_comp_prices) // 2
                if len(last_comp_prices) % 2 == 1:
                    suggested_listing_price = float(last_comp_prices[mid])
                else:
                    suggested_listing_price = float((last_comp_prices[mid - 1] + last_comp_prices[mid]) / 2.0)
            create_draft_listings = st.checkbox(
                "Also create draft marketplace listing(s)",
                value=False,
                key="comp_photo_create_draft_listings",
            )
            draft_marketplaces: list[str] = []
            draft_listing_price = suggested_listing_price
            draft_listing_qty = max(1, int(draft_qty))
            if create_draft_listings:
                l1, l2, l3 = st.columns(3)
                with l1:
                    draft_marketplaces = st.multiselect(
                        "Draft Marketplaces",
                        options=["ebay", "facebook_marketplace", "craigslist", "whatnot", "shopify", "local"],
                        default=["ebay"],
                        key="comp_photo_draft_listing_marketplaces",
                    )
                with l2:
                    draft_listing_price = st.number_input(
                        "Draft Listing Price",
                        min_value=0.0,
                        value=float(suggested_listing_price),
                        step=1.0,
                        key="comp_photo_draft_listing_price",
                    )
                with l3:
                    draft_listing_qty = st.number_input(
                        "Draft Listing Quantity",
                        min_value=1,
                        value=max(1, int(draft_qty)),
                        step=1,
                        key="comp_photo_draft_listing_qty",
                    )
                st.caption("All created listings are saved as `draft` with review status `pending`.")
            attach_hint_to_draft = st.checkbox(
                "Attach hint image/video to created draft product",
                value=True,
                key="comp_photo_draft_attach_hint_media",
                disabled=(hint_file is None or not storage.enabled),
            )
            if st.button(
                "Create Product Draft From Photo-Comp",
                key="comp_photo_create_product_draft_btn",
                disabled=(not comp_tool_enabled or not can_use_comp_tool),
            ):
                if not comp_tool_enabled:
                    st.error("Comp Tool is disabled by Admin.")
                elif not ensure_permission(user, "ai_comp_use", "Create Product Draft From Photo-Comp"):
                    pass
                else:
                    try:
                        if create_draft_listings and not draft_marketplaces:
                            st.error("Select at least one marketplace or disable draft listing creation.")
                            st.stop()
                        created_product = repo.create_product(
                            sku=generate_sku(draft_category, draft_metal or "mixed"),
                            title=draft_title.strip() or draft_default_title,
                            category=draft_category,
                            description=draft_description.strip(),
                            metal_type=draft_metal.strip(),
                            weight_oz=None,
                            acquisition_cost=Decimal(str(draft_cost)),
                            current_quantity=int(draft_qty),
                            acquired_at=None,
                            lot_id=None,
                            actor=user.username,
                        )
                        ai_comp_value = str(st.session_state.get("comp_last_ai_summary") or "").strip()
                        if ai_comp_value:
                            repo.update_product(
                                created_product.id,
                                {"ai_comp": ai_comp_value},
                                actor=user.username,
                            )
                        if (
                            attach_hint_to_draft
                            and hint_file is not None
                            and storage.enabled
                        ):
                            uploaded_count, upload_errors = _persist_ai_input_media(
                                repo=repo,
                                storage=storage,
                                files=[
                                    (
                                        hint_file.getvalue(),
                                        (hint_file.type or "application/octet-stream"),
                                        (hint_file.name or "comp_hint_file"),
                                    )
                                ],
                                product_id=created_product.id,
                                listing_id=None,
                                uploaded_by=user.username,
                            )
                            if uploaded_count:
                                st.success(f"Saved {uploaded_count} hint media file(s) to new draft product media.")
                            for media_error in upload_errors:
                                st.error(f"Draft media save failed: {media_error}")
                        created_listing_ids: list[int] = []
                        listing_create_errors: list[str] = []
                        if create_draft_listings and draft_marketplaces:
                            for marketplace in draft_marketplaces:
                                try:
                                    created_listing = repo.create_listing(
                                        product_id=int(created_product.id),
                                        marketplace=str(marketplace).strip(),
                                        listing_title=draft_title.strip() or draft_default_title,
                                        listing_price=Decimal(str(draft_listing_price)),
                                        quantity_listed=int(draft_listing_qty),
                                        actor=user.username,
                                    )
                                    created_listing_ids.append(int(created_listing.id))
                                except Exception as listing_exc:
                                    listing_create_errors.append(
                                        f"{marketplace}: {listing_exc}"
                                    )
                            if created_listing_ids:
                                st.success(
                                    f"Created {len(created_listing_ids)} draft listing(s): "
                                    + ", ".join([f"#{lid}" for lid in created_listing_ids])
                                )
                            for listing_error in listing_create_errors:
                                st.error(f"Draft listing create failed: {listing_error}")
                        st.session_state["products_filter_query"] = str(created_product.sku or "").strip()
                        if create_draft_listings and created_listing_ids:
                            st.session_state["listings_filter_query"] = str(created_product.sku or "").strip()
                            st.session_state["listings_filter_marketplaces"] = list(draft_marketplaces)
                        try:
                            repo.record_audit_event(
                                entity_type="navigation",
                                entity_id=int(created_product.id),
                                action="photo_comp_product_draft_created",
                                actor=user.username,
                                changes={
                                    "from": "tools_comp",
                                    "source_mode": source_mode,
                                    "query": query,
                                    "product_id": int(created_product.id),
                                    "product_sku": str(created_product.sku or "").strip(),
                                    "draft_listing_ids": created_listing_ids,
                                    "draft_listing_marketplaces": list(draft_marketplaces),
                                },
                            )
                        except Exception:
                            pass
                        if create_draft_listings and created_listing_ids:
                            st.success(
                                f"Created draft-ready product #{created_product.id} (`{created_product.sku}`) "
                                "and linked draft listing(s). Opening Listings for review."
                            )
                            if hasattr(st, "switch_page"):
                                st.switch_page("pages/03_Listings.py")
                        else:
                            st.success(
                                f"Created draft-ready product #{created_product.id} (`{created_product.sku}`). "
                                "Opening Products for review."
                            )
                            if hasattr(st, "switch_page"):
                                st.switch_page("pages/02_Products.py")
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Unable to create product draft from photo-comp: {exc}")

        if query:
            encoded = quote_plus(query)
            st.markdown("#### External Research Links")
            st.markdown(f"[eBay Sold Listings Search](https://www.ebay.com/sch/i.html?_nkw={encoded}&LH_Sold=1&LH_Complete=1)")
            st.markdown(f"[Google Shopping Search](https://www.google.com/search?tbm=shop&q={encoded})")

    with tab5:
        if not coin_grader_enabled:
            st.info("Coin Grader is currently disabled by Admin AI domain toggle.")
        if not can_use_coin_grader:
            st.info(f"`{user.role}` role does not have `ai_coin_grade` permission.")
        st.caption(
            "Coin Grader (AI-assisted): upload or capture a coin image to get a grade estimate. "
            "Always verify with expert/TPG standards (PCGS/NGC) before high-value decisions."
        )
        grader_cfg_chain = resolve_comp_llm_runtime_chain(repo)
        grader_cfg = grader_cfg_chain[0] if grader_cfg_chain else resolve_comp_llm_runtime_config(repo)
        st.caption(
            f"AI runtime source: `{grader_cfg.source}` | provider: `{grader_cfg.provider}` | "
            f"text model: `{grader_cfg.model}` | multimodal model: `{grader_cfg.multimodal_model}` | "
            f"endpoint: `{grader_cfg.endpoint_type}` | fallback_profiles: `{max(0, len(grader_cfg_chain) - 1)}`"
        )
        gcol1, gcol2 = st.columns(2)
        with gcol1:
            grade_image_upload = st.file_uploader(
                "Upload Obverse Image",
                type=["jpg", "jpeg", "png", "webp"],
                key="coin_grader_obv_upload",
            )
        with gcol2:
            with st.expander("Camera (Obverse)", expanded=False):
                grade_camera_capture = st.camera_input("Capture Obverse Photo", key="coin_grader_obv_camera")
        grcol1, grcol2 = st.columns(2)
        with grcol1:
            grade_reverse_upload = st.file_uploader(
                "Upload Reverse Image (optional)",
                type=["jpg", "jpeg", "png", "webp"],
                key="coin_grader_rev_upload",
            )
        with grcol2:
            with st.expander("Camera (Reverse)", expanded=False):
                grade_reverse_camera = st.camera_input("Capture Reverse Photo (optional)", key="coin_grader_rev_camera")
        gctx1, gctx2 = st.columns(2)
        with gctx1:
            grader_products = repo.list_products()
            grader_product_options = ["(none)"] + [f"#{p.id} | {p.sku} | {p.title}" for p in grader_products]
            grader_product_pick = st.selectbox("Link to Product (optional)", grader_product_options, key="coin_grader_product")
            grader_product_id = int(grader_product_pick.split("|")[0].replace("#", "").strip()) if grader_product_pick != "(none)" else None
        with gctx2:
            grader_listings = repo.list_listings()
            grader_listing_options = ["(none)"] + [f"#{l.id} | {l.marketplace} | {l.listing_title}" for l in grader_listings]
            grader_listing_pick = st.selectbox("Link to Listing (optional)", grader_listing_options, key="coin_grader_listing")
            grader_listing_id = int(grader_listing_pick.split("|")[0].replace("#", "").strip()) if grader_listing_pick != "(none)" else None
        gsave1, gsave2 = st.columns(2)
        with gsave1:
            save_grader_media = st.checkbox(
                "Save grader input image(s) to Media Library",
                value=True,
                key="coin_grader_save_media",
            )
        with gsave2:
            update_product_from_grade = st.checkbox(
                "Update linked product AI grading fields",
                value=True,
                key="coin_grader_update_product",
            )
        grade_notes = st.text_area(
            "Coin Details / Notes (optional)",
            value="",
            key="coin_grader_notes",
            help="Include denomination, year, mint mark, known issues, lighting notes, etc.",
        )
        gcost1, gcost2, gcost3 = st.columns(3)
        with gcost1:
            estimated_as_is_value = st.number_input(
                "Estimated Current Value (USD, optional)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key="coin_grader_estimated_as_is_value",
                help="Your best estimate of current raw/as-is value before grading.",
            )
        with gcost2:
            grading_fee_estimate = st.number_input(
                "Estimated Grading Fee (USD)",
                min_value=0.0,
                value=40.0,
                step=1.0,
                key="coin_grader_grading_fee_estimate",
            )
        with gcost3:
            shipping_insurance_estimate = st.number_input(
                "Estimated Shipping/Insurance (USD)",
                min_value=0.0,
                value=25.0,
                step=1.0,
                key="coin_grader_shipping_insurance_estimate",
            )
        expected_selling_fee_pct = st.number_input(
            "Expected Selling Fee % (if graded and sold)",
            min_value=0.0,
            value=13.0,
            step=0.5,
            key="coin_grader_expected_selling_fee_pct",
            help="Used as cost context for professional grading recommendation.",
        )
        grade_system_message = get_runtime_str(
            repo,
            "coin_grader_system_message",
            "You are a conservative numismatic grading assistant. Explain uncertainty clearly.",
        ).strip()
        grade_instruction_template = get_runtime_str(
            repo,
            "coin_grader_instruction_template",
            (
                "Analyze the coin image(s) and return STRICT JSON only (no markdown, no prose outside JSON) "
                "with keys: estimated_grade_range, confidence_0_100, key_observations (array), red_flags (array), "
                "estimated_as_is_value_usd, estimated_post_grade_value_usd, estimated_grading_total_cost_usd, "
                "estimated_net_upside_usd, submit_for_professional_grading (YES|NO|CONDITIONAL), "
                "recommendation_rationale, suggested_grade_service_priority (array), notes. "
                "Use conservative estimates and explicitly account for grading cost, shipping/insurance, and selling fees."
            ),
        ).strip()
        if st.button(
            "Run Coin Grader",
            key="coin_grader_run",
            disabled=(not coin_grader_enabled or not can_use_coin_grader),
        ):
            image_bytes, image_content_type = _uploaded_image_to_bytes(grade_image_upload)
            if image_bytes is None:
                image_bytes, image_content_type = _uploaded_image_to_bytes(grade_camera_capture)
            reverse_bytes, reverse_content_type = _uploaded_image_to_bytes(grade_reverse_upload)
            if reverse_bytes is None:
                reverse_bytes, reverse_content_type = _uploaded_image_to_bytes(grade_reverse_camera)
            image_filename = _uploaded_file_name(grade_image_upload) or _uploaded_file_name(grade_camera_capture)
            reverse_filename = _uploaded_file_name(grade_reverse_upload) or _uploaded_file_name(grade_reverse_camera)
            if not coin_grader_enabled:
                st.error("Coin Grader is disabled by Admin.")
            elif not ensure_permission(user, "ai_coin_grade", "Run Coin Grader"):
                pass
            elif image_bytes is None:
                st.error("Upload or capture a coin image first.")
            elif not grader_cfg.enabled:
                st.error("AI runtime is disabled. Enable AI runtime in Admin.")
            else:
                try:
                    grading_total_cost = float(grading_fee_estimate or 0.0) + float(shipping_insurance_estimate or 0.0)
                    instruction = (
                        f"{grade_instruction_template}\n\n"
                        "Cost/value context (operator-provided):\n"
                        f"- estimated_as_is_value_usd: {float(estimated_as_is_value or 0.0):.2f}\n"
                        f"- grading_fee_estimate_usd: {float(grading_fee_estimate or 0.0):.2f}\n"
                        f"- shipping_insurance_estimate_usd: {float(shipping_insurance_estimate or 0.0):.2f}\n"
                        f"- expected_selling_fee_percent: {float(expected_selling_fee_pct or 0.0):.2f}\n"
                        f"- estimated_grading_total_cost_usd: {float(grading_total_cost):.2f}\n\n"
                        f"User notes:\n{grade_notes.strip() or '(none)'}"
                    )
                    grade_execution = execute_multimodal_task(
                        repo,
                        tool_name="coin_grader",
                        system_message=grade_system_message,
                        instruction=instruction,
                        image_bytes=image_bytes,
                        image_content_type=image_content_type or "image/jpeg",
                        additional_images=[(reverse_bytes, reverse_content_type or "image/jpeg")] if reverse_bytes else [],
                        context={
                            "has_obverse": bool(image_bytes),
                            "has_reverse": bool(reverse_bytes),
                            "product_id": grader_product_id,
                            "listing_id": grader_listing_id,
                        },
                    )
                    grade_result = grade_execution.text
                    structured_grade = parse_coin_grader_structured(grade_result)
                    grade_used_cfg = grade_execution.used_config
                    grade_fallback_errors = grade_execution.fallback_errors
                    st.markdown("##### Coin Grade Estimate")
                    if structured_grade:
                        st.json(structured_grade)
                        formatted_grade_text = coin_grader_structured_to_text(structured_grade)
                        _render_grader_summary_text(formatted_grade_text)
                    else:
                        st.markdown(grade_result)
                        st.warning(
                            "Grader output was not valid schema JSON. "
                            "Consider increasing max tokens or using a stronger multimodal model."
                        )
                    st.caption(
                        f"AI profile used: `{grade_used_cfg.provider}` / "
                        f"`{grade_used_cfg.multimodal_model or grade_used_cfg.model}`. "
                        f"fallback_attempts: `{len(grade_fallback_errors)}`"
                    )
                    grade_citation = grade_execution.citation
                    with st.expander("AI Citation", expanded=False):
                        st.code(json.dumps(grade_citation, indent=2), language="json")
                    grade_text_for_product = (
                        coin_grader_structured_to_text(structured_grade) if structured_grade else normalize_ai_text(grade_result)
                    )
                    st.session_state["coin_grader_last_result"] = grade_text_for_product
                    if save_grader_media and storage.enabled and (grader_product_id is not None or grader_listing_id is not None):
                        files_to_save: list[tuple[bytes, str, str]] = []
                        if image_bytes is not None:
                            files_to_save.append((image_bytes, image_content_type or "image/jpeg", image_filename or "grader_obverse.jpg"))
                        if reverse_bytes is not None:
                            files_to_save.append((reverse_bytes, reverse_content_type or "image/jpeg", reverse_filename or "grader_reverse.jpg"))
                        uploaded_count, upload_errors = _persist_ai_input_media(
                            repo=repo,
                            storage=storage,
                            files=files_to_save,
                            product_id=grader_product_id,
                            listing_id=grader_listing_id,
                            uploaded_by=user.username,
                        )
                        if uploaded_count:
                            st.success(f"Saved {uploaded_count} grader input image(s) to media library.")
                        for media_error in upload_errors:
                            st.error(f"Grader media save failed: {media_error}")
                    if update_product_from_grade and grader_product_id is not None:
                        repo.update_product(
                            grader_product_id,
                            {
                                "ai_graded": True,
                                "ai_grading_description": grade_text_for_product,
                            },
                            actor=user.username,
                        )
                        st.success(f"Updated product #{grader_product_id} AI grading fields.")
                    repo.create_coin_ai_run(
                        environment=settings.app_env,
                        tool_name="coin_grader",
                        username=user.username,
                        product_id=grader_product_id,
                        listing_id=grader_listing_id,
                        input_hint=grade_notes,
                        image_filename=", ".join([n for n in [image_filename, reverse_filename] if n]),
                        image_content_type=image_content_type or "image/jpeg",
                        result_markdown=grade_result,
                        result_json=json.dumps(
                            {
                                "ai_citation": grade_citation,
                                "grading_structured": structured_grade,
                                "grading_input_context": {
                                    "estimated_as_is_value_usd": float(estimated_as_is_value or 0.0),
                                    "grading_fee_estimate_usd": float(grading_fee_estimate or 0.0),
                                    "shipping_insurance_estimate_usd": float(shipping_insurance_estimate or 0.0),
                                    "expected_selling_fee_percent": float(expected_selling_fee_pct or 0.0),
                                    "estimated_grading_total_cost_usd": float(grading_total_cost or 0.0),
                                },
                            }
                        ),
                        web_rows_json="[]",
                        actor=user.username,
                    )
                except Exception as exc:
                    st.error(f"Coin grader failed: {exc}")
        grader_history = repo.list_coin_ai_runs(tool_name="coin_grader", limit=20)
        if grader_history:
            st.markdown("##### Recent Coin Grader Runs")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "created_at": row.created_at,
                            "username": row.username,
                            "product_id": row.product_id,
                            "listing_id": row.listing_id,
                            "image_filename": row.image_filename,
                            "input_hint": row.input_hint,
                        }
                        for row in grader_history
                    ]
                ),
                use_container_width=True,
            )

    with tab6:
        if not coin_identifier_enabled:
            st.info("Coin Identifier is currently disabled by Admin AI domain toggle.")
        if not can_use_coin_identifier:
            st.info(f"`{user.role}` role does not have `ai_coin_identify` permission.")
        st.caption(
            "Coin Identifier: use AI image understanding plus optional web fallback hints "
            "to identify coin type and comparable market references."
        )
        identifier_cfg_chain = resolve_comp_llm_runtime_chain(repo)
        identifier_cfg = identifier_cfg_chain[0] if identifier_cfg_chain else resolve_comp_llm_runtime_config(repo)
        st.caption(
            f"AI runtime source: `{identifier_cfg.source}` | provider: `{identifier_cfg.provider}` | "
            f"text model: `{identifier_cfg.model}` | multimodal model: `{identifier_cfg.multimodal_model}` | "
            f"endpoint: `{identifier_cfg.endpoint_type}` | fallback_profiles: `{max(0, len(identifier_cfg_chain) - 1)}`"
        )
        icol1, icol2 = st.columns(2)
        with icol1:
            identify_image_upload = st.file_uploader(
                "Upload Obverse Image",
                type=["jpg", "jpeg", "png", "webp"],
                key="coin_identifier_obv_upload",
            )
        with icol2:
            with st.expander("Camera (Obverse)", expanded=False):
                identify_camera_capture = st.camera_input("Capture Obverse Photo", key="coin_identifier_obv_camera")
        ircol1, ircol2 = st.columns(2)
        with ircol1:
            identify_reverse_upload = st.file_uploader(
                "Upload Reverse Image (optional)",
                type=["jpg", "jpeg", "png", "webp"],
                key="coin_identifier_rev_upload",
            )
        with ircol2:
            with st.expander("Camera (Reverse)", expanded=False):
                identify_reverse_camera = st.camera_input("Capture Reverse Photo (optional)", key="coin_identifier_rev_camera")
        ictx1, ictx2 = st.columns(2)
        with ictx1:
            identifier_products = repo.list_products()
            identifier_product_options = ["(none)"] + [f"#{p.id} | {p.sku} | {p.title}" for p in identifier_products]
            identifier_product_pick = st.selectbox("Link to Product (optional)", identifier_product_options, key="coin_identifier_product")
            identifier_product_id = int(identifier_product_pick.split("|")[0].replace("#", "").strip()) if identifier_product_pick != "(none)" else None
        with ictx2:
            identifier_listings = repo.list_listings()
            identifier_listing_options = ["(none)"] + [f"#{l.id} | {l.marketplace} | {l.listing_title}" for l in identifier_listings]
            identifier_listing_pick = st.selectbox("Link to Listing (optional)", identifier_listing_options, key="coin_identifier_listing")
            identifier_listing_id = int(identifier_listing_pick.split("|")[0].replace("#", "").strip()) if identifier_listing_pick != "(none)" else None
        isave1, isave2 = st.columns(2)
        with isave1:
            save_identifier_media = st.checkbox(
                "Save identifier input image(s) to Media Library",
                value=True,
                key="coin_identifier_save_media",
            )
        with isave2:
            update_product_from_identifier = st.checkbox(
                "Update linked product AI description",
                value=True,
                key="coin_identifier_update_product",
            )
        identify_hint = st.text_input(
            "Optional Identifier Hint",
            value="",
            key="coin_identifier_hint",
            help="Examples: 1921 Morgan dollar, standing liberty quarter, Roman bronze.",
        )
        auto_create_inventory_from_identifier = st.checkbox(
            "Auto-create inventory product when no product is linked",
            value=False,
            key="coin_identifier_auto_create_product",
        )
        create_qty = st.number_input(
            "Auto-create Quantity",
            min_value=1,
            value=1,
            step=1,
            key="coin_identifier_auto_create_qty",
        )
        create_category = st.selectbox(
            "Auto-create Category",
            options=["coins", "bullion", "collectibles", "antiques", "other"],
            index=0,
            key="coin_identifier_auto_create_category",
        )
        id_web_limit = st.number_input(
            "Identifier Web Result Limit",
            min_value=5,
            max_value=100,
            value=max(5, min(100, get_runtime_int(repo, "coin_identifier_web_limit", 20))),
            step=5,
            key="coin_identifier_web_limit",
        )
        id_detail_limit = st.number_input(
            "Identifier Web Detail Fetch Limit",
            min_value=1,
            max_value=100,
            value=max(1, min(100, get_runtime_int(repo, "coin_identifier_web_detail_limit", 20))),
            step=1,
            key="coin_identifier_web_detail_limit",
        )
        identify_system_message = get_runtime_str(
            repo,
            "coin_identifier_system_message",
            "You are a careful numismatic identifier. Prefer precision and state uncertainty clearly.",
        ).strip()
        identify_instruction_template = get_runtime_str(
            repo,
            "coin_identifier_instruction_template",
            (
                "Identify the coin from image and notes. "
                "If obverse and reverse images are both present, use both. "
                "Respond as strict JSON object with keys: "
                "coin_name, possible_country_or_mint, year_or_period, denomination, metal, "
                "confidence, search_keywords (array of <= 10 short keywords), notes."
            ),
        ).strip()
        if st.button(
            "Run Coin Identifier",
            key="coin_identifier_run",
            disabled=(not coin_identifier_enabled or not can_use_coin_identifier),
        ):
            image_bytes, image_content_type = _uploaded_image_to_bytes(identify_image_upload)
            if image_bytes is None:
                image_bytes, image_content_type = _uploaded_image_to_bytes(identify_camera_capture)
            reverse_bytes, reverse_content_type = _uploaded_image_to_bytes(identify_reverse_upload)
            if reverse_bytes is None:
                reverse_bytes, reverse_content_type = _uploaded_image_to_bytes(identify_reverse_camera)
            image_filename = _uploaded_file_name(identify_image_upload) or _uploaded_file_name(identify_camera_capture)
            reverse_filename = _uploaded_file_name(identify_reverse_upload) or _uploaded_file_name(identify_reverse_camera)
            if not coin_identifier_enabled:
                st.error("Coin Identifier is disabled by Admin.")
            elif not ensure_permission(user, "ai_coin_identify", "Run Coin Identifier"):
                pass
            elif image_bytes is None and not identify_hint.strip():
                st.error("Upload/capture an image or provide identifier hint text.")
            elif not identifier_cfg.enabled:
                st.error("AI runtime is disabled. Enable AI runtime in Admin.")
            else:
                try:
                    identifier_max_tokens = max(
                        int(identifier_cfg.max_output_tokens),
                        int(get_runtime_int(repo, "coin_identifier_max_output_tokens", 1200)),
                    )
                    instruction = (
                        f"{identify_instruction_template}\n\n"
                        f"User hint: {identify_hint.strip() or '(none)'}"
                    )
                    identify_execution = execute_multimodal_task(
                        repo,
                        tool_name="coin_identifier",
                        system_message=identify_system_message,
                        instruction=instruction,
                        image_bytes=image_bytes,
                        image_content_type=image_content_type or "image/jpeg",
                        additional_images=[(reverse_bytes, reverse_content_type or "image/jpeg")] if reverse_bytes else [],
                        max_output_tokens_override=identifier_max_tokens,
                        context={
                            "has_obverse": bool(image_bytes),
                            "has_reverse": bool(reverse_bytes),
                            "product_id": identifier_product_id,
                            "listing_id": identifier_listing_id,
                        },
                    )
                    identify_result_text = identify_execution.text
                    identify_used_cfg = identify_execution.used_config
                    identify_fallback_errors = identify_execution.fallback_errors
                    st.caption(
                        f"AI profile used: `{identify_used_cfg.provider}` / "
                        f"`{identify_used_cfg.multimodal_model or identify_used_cfg.model}`. "
                        f"fallback_attempts: `{len(identify_fallback_errors)}`"
                    )
                    identify_citation = identify_execution.citation
                    identify_json = _extract_or_repair_first_json_object(identify_result_text)
                    if not identify_json and _looks_like_truncated_json_output(identify_result_text):
                        retry_instruction = (
                            f"{identify_instruction_template}\n\n"
                            "Previous answer was truncated. Return one complete JSON object only, "
                            "no markdown, no code fences.\n\n"
                            f"User hint: {identify_hint.strip() or '(none)'}"
                        )
                        identify_retry_execution = execute_multimodal_task(
                            repo,
                            tool_name="coin_identifier_retry",
                            system_message=identify_system_message,
                            instruction=retry_instruction,
                            image_bytes=image_bytes,
                            image_content_type=image_content_type or "image/jpeg",
                            additional_images=[(reverse_bytes, reverse_content_type or "image/jpeg")] if reverse_bytes else [],
                            max_output_tokens_override=max(identifier_max_tokens, 1600),
                            context={
                                "has_obverse": bool(image_bytes),
                                "has_reverse": bool(reverse_bytes),
                                "product_id": identifier_product_id,
                                "listing_id": identifier_listing_id,
                            },
                        )
                        identify_result_text = identify_retry_execution.text
                        identify_retry_cfg = identify_retry_execution.used_config
                        identify_retry_errors = identify_retry_execution.fallback_errors
                        st.caption(
                            f"AI retry profile used: `{identify_retry_cfg.provider}` / "
                            f"`{identify_retry_cfg.multimodal_model or identify_retry_cfg.model}`. "
                            f"fallback_attempts: `{len(identify_retry_errors)}`"
                        )
                        identify_retry_citation = identify_retry_execution.citation
                        identify_citation = identify_retry_citation
                        identify_json = _extract_or_repair_first_json_object(identify_result_text)
                    st.markdown("##### Identification Result")
                    if identify_json:
                        st.json(identify_json)
                    else:
                        st.warning(
                            "Identifier output was not valid JSON. Consider raising profile max tokens "
                            "or simplifying prompt/model."
                        )
                        st.markdown(identify_result_text)
                    with st.expander("AI Citation", expanded=False):
                        st.code(json.dumps(identify_citation, indent=2), language="json")

                    search_keywords = identify_json.get("search_keywords") if isinstance(identify_json, dict) else None
                    if isinstance(search_keywords, list):
                        keyword_query = " ".join([str(k).strip() for k in search_keywords if str(k).strip()])
                    else:
                        keyword_query = ""
                    coin_name = str(identify_json.get("coin_name") or "").strip() if isinstance(identify_json, dict) else ""
                    web_query = " ".join(
                        part
                        for part in [
                            keyword_query.strip(),
                            coin_name,
                            identify_hint.strip(),
                        ]
                        if part
                    ).strip()
                    if web_query:
                        web_rows = _web_comp_search(
                            web_query,
                            limit=int(id_web_limit),
                            page_fetch_limit=int(id_detail_limit),
                        )
                        st.markdown("##### Identifier Web Hints")
                        if web_rows:
                            st.dataframe(pd.DataFrame(web_rows), use_container_width=True)
                        else:
                            st.info("No web hints found for this identification query.")
                        st.session_state["coin_identifier_last_web_rows"] = web_rows
                    if save_identifier_media and storage.enabled and (identifier_product_id is not None or identifier_listing_id is not None):
                        files_to_save: list[tuple[bytes, str, str]] = []
                        if image_bytes is not None:
                            files_to_save.append((image_bytes, image_content_type or "image/jpeg", image_filename or "identifier_obverse.jpg"))
                        if reverse_bytes is not None:
                            files_to_save.append((reverse_bytes, reverse_content_type or "image/jpeg", reverse_filename or "identifier_reverse.jpg"))
                        uploaded_count, upload_errors = _persist_ai_input_media(
                            repo=repo,
                            storage=storage,
                            files=files_to_save,
                            product_id=identifier_product_id,
                            listing_id=identifier_listing_id,
                            uploaded_by=user.username,
                        )
                        if uploaded_count:
                            st.success(f"Saved {uploaded_count} identifier input image(s) to media library.")
                        for media_error in upload_errors:
                            st.error(f"Identifier media save failed: {media_error}")
                    if update_product_from_identifier and identifier_product_id is not None:
                        identifier_text_for_product = normalize_ai_text(identify_result_text.strip())
                        repo.update_product(
                            identifier_product_id,
                            {
                                "ai_description": identifier_text_for_product,
                            },
                            actor=user.username,
                        )
                        st.success(f"Updated product #{identifier_product_id} AI Description.")
                    if (
                        auto_create_inventory_from_identifier
                        and identifier_product_id is None
                        and isinstance(identify_json, dict)
                    ):
                        inferred_title = str(identify_json.get("coin_name") or "").strip() or "AI Identified Coin"
                        inferred_metal = str(identify_json.get("metal") or "").strip()
                        identifier_text_for_product = normalize_ai_text(identify_result_text.strip())
                        created_product = repo.create_product(
                            sku=generate_sku(create_category, inferred_metal or "coin"),
                            title=inferred_title,
                            category=create_category,
                            description=identifier_text_for_product,
                            metal_type=inferred_metal,
                            weight_oz=None,
                            acquisition_cost=Decimal("0.00"),
                            current_quantity=int(create_qty),
                            acquired_at=None,
                            lot_id=None,
                            actor=user.username,
                        )
                        repo.update_product(
                            created_product.id,
                            {
                                "ai_description": identifier_text_for_product,
                                "ai_graded": False,
                            },
                            actor=user.username,
                        )
                        st.success(f"Created new inventory product #{created_product.id} from identifier result.")
                    st.session_state["coin_identifier_last_result"] = normalize_ai_text(identify_result_text)
                    repo.create_coin_ai_run(
                        environment=settings.app_env,
                        tool_name="coin_identifier",
                        username=user.username,
                        product_id=identifier_product_id,
                        listing_id=identifier_listing_id,
                        input_hint=identify_hint,
                        image_filename=", ".join([n for n in [image_filename, reverse_filename] if n]),
                        image_content_type=image_content_type or "image/jpeg",
                        result_markdown=identify_result_text,
                        result_json=json.dumps(
                            {
                                "identify_json": identify_json or {},
                                "ai_citation": identify_citation,
                            }
                        ),
                        web_rows_json=json.dumps(st.session_state.get("coin_identifier_last_web_rows") or []),
                        actor=user.username,
                    )
                except Exception as exc:
                    st.error(f"Coin identifier failed: {exc}")
        identifier_history = repo.list_coin_ai_runs(tool_name="coin_identifier", limit=20)
        if identifier_history:
            st.markdown("##### Recent Coin Identifier Runs")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "created_at": row.created_at,
                            "username": row.username,
                            "product_id": row.product_id,
                            "listing_id": row.listing_id,
                            "image_filename": row.image_filename,
                            "input_hint": row.input_hint,
                        }
                        for row in identifier_history
                    ]
                ),
                use_container_width=True,
            )

    with tab7:
        st.caption(
            "Build and maintain your in-house coin reference database (series/specs/value bands) "
            "without depending on paid Greysheet APIs."
        )
        st.info(
            "Greysheet pricing data is paid/licensed. This module is designed for your own records plus "
            "free/public source references and manual imports."
        )
        cdb1, cdb2, cdb3, cdb4 = st.columns(4)
        with cdb1:
            coin_query = st.text_input("Search", value="", key="coin_db_search")
        with cdb2:
            coin_country = st.text_input("Country", value="", key="coin_db_country")
        with cdb3:
            coin_metal = st.text_input("Metal", value="", key="coin_db_metal")
        with cdb4:
            coin_active_only = st.checkbox("Active only", value=True, key="coin_db_active_only")

        coin_rows = repo.list_coin_references(
            query=coin_query.strip(),
            country=coin_country.strip() or None,
            metal_type=coin_metal.strip() or None,
            active_only=bool(coin_active_only),
            limit=1000,
        )
        if coin_rows:
            coin_df = pd.DataFrame(
                [
                    {
                        "id": row.id,
                        "coin_name": row.coin_name,
                        "country": row.country,
                        "issuer": row.issuer,
                        "denomination": row.denomination,
                        "series": row.series,
                        "year_start": row.year_start,
                        "year_end": row.year_end,
                        "mint_mark": row.mint_mark,
                        "metal_type": row.metal_type,
                        "composition": row.composition,
                        "weight_grams": float(row.weight_grams) if row.weight_grams is not None else None,
                        "asw_oz": float(row.asw_oz) if row.asw_oz is not None else None,
                        "diameter_mm": float(row.diameter_mm) if row.diameter_mm is not None else None,
                        "thickness_mm": float(row.thickness_mm) if row.thickness_mm is not None else None,
                        "km_number": row.km_number,
                        "pcgs_no": row.pcgs_no,
                        "ngc_id": row.ngc_id,
                        "mintage": row.mintage,
                        "estimated_value_low": float(row.estimated_value_low) if row.estimated_value_low is not None else None,
                        "estimated_value_high": float(row.estimated_value_high) if row.estimated_value_high is not None else None,
                        "price_source": row.price_source,
                        "source_url": row.source_url,
                        "tags": row.tags,
                        "is_active": bool(row.is_active),
                        "updated_at": row.updated_at,
                    }
                    for row in coin_rows
                ]
            )
            st.dataframe(coin_df, use_container_width=True)
            st.download_button(
                "Download Coin Database CSV",
                data=coin_df.to_csv(index=False).encode("utf-8"),
                file_name="coin_database.csv",
                mime="text/csv",
                key="coin_db_download_csv",
            )
        else:
            st.info("No coin reference rows found for current filter.")

        st.markdown("##### CSV Import / Upsert")
        st.caption(
            "Upload CSV and upsert into Coin Database. Matching priority: `km_number`, then `pcgs_no`, then "
            "`ngc_id`, then `(coin_name,country,series,year_start,mint_mark)`."
        )
        st.caption(
            "Supported columns include: `coin_name,name,country,issuer,denomination,series,year_start,year_end,"
            "mint_mark,metal_type,weight_grams,asw_oz,km_number,pcgs_no,ngc_id,estimated_value_low,"
            "estimated_value_high,price_source,source_url,tags,is_active`."
        )
        import_file = st.file_uploader(
            "Coin Database CSV",
            type=["csv"],
            key="coin_db_import_csv",
            help="Use UTF-8 CSV with header row. Unknown columns are ignored.",
        )
        import_apply = st.checkbox(
            "Apply changes (unchecked = dry run only)",
            value=False,
            key="coin_db_import_apply",
        )
        if st.button("Run CSV Import", key="coin_db_import_run"):
            if import_file is None:
                st.error("Upload a CSV file first.")
            else:
                try:
                    imported_df = pd.read_csv(io.BytesIO(import_file.getvalue()))
                    imported_df = _coin_csv_normalize_columns(imported_df)
                    if "coin_name" not in imported_df.columns:
                        st.error("CSV must include `coin_name` (or alias `name`).")
                    else:
                        existing = repo.list_coin_references(active_only=False, limit=20000)
                        by_km: dict[str, Any] = {}
                        by_pcgs: dict[str, Any] = {}
                        by_ngc: dict[str, Any] = {}
                        by_sig: dict[str, Any] = {}
                        for row in existing:
                            km = (row.km_number or "").strip().lower()
                            pcgs = (row.pcgs_no or "").strip().lower()
                            ngc = (row.ngc_id or "").strip().lower()
                            sig = _coin_ref_match_key_from_parts(
                                coin_name=row.coin_name or "",
                                country=row.country or "",
                                series=row.series or "",
                                year_start=row.year_start,
                                mint_mark=row.mint_mark or "",
                            )
                            if km:
                                by_km[km] = row
                            if pcgs:
                                by_pcgs[pcgs] = row
                            if ngc:
                                by_ngc[ngc] = row
                            if sig:
                                by_sig[sig] = row

                        created = 0
                        updated = 0
                        errors: list[dict[str, Any]] = []
                        actions_preview: list[dict[str, Any]] = []
                        for idx, source_row in imported_df.iterrows():
                            payload = _coin_ref_payload_from_csv_row(source_row)
                            coin_name = str(payload.get("coin_name") or "").strip()
                            if not coin_name:
                                errors.append({"row": int(idx) + 2, "error": "Missing coin_name"})
                                continue
                            km_key = str(payload.get("km_number") or "").strip().lower()
                            pcgs_key = str(payload.get("pcgs_no") or "").strip().lower()
                            ngc_key = str(payload.get("ngc_id") or "").strip().lower()
                            sig_key = str(payload.get("_match_key") or "").strip()
                            matched = None
                            matched_by = ""
                            if km_key and km_key in by_km:
                                matched = by_km[km_key]
                                matched_by = "km_number"
                            elif pcgs_key and pcgs_key in by_pcgs:
                                matched = by_pcgs[pcgs_key]
                                matched_by = "pcgs_no"
                            elif ngc_key and ngc_key in by_ngc:
                                matched = by_ngc[ngc_key]
                                matched_by = "ngc_id"
                            elif sig_key and sig_key in by_sig:
                                matched = by_sig[sig_key]
                                matched_by = "signature"

                            actions_preview.append(
                                {
                                    "row": int(idx) + 2,
                                    "action": "update" if matched is not None else "create",
                                    "matched_by": matched_by,
                                    "matched_id": int(matched.id) if matched is not None else None,
                                    "coin_name": coin_name,
                                    "country": payload.get("country", ""),
                                    "series": payload.get("series", ""),
                                    "km_number": payload.get("km_number", ""),
                                    "pcgs_no": payload.get("pcgs_no", ""),
                                    "ngc_id": payload.get("ngc_id", ""),
                                }
                            )

                            if not import_apply:
                                if matched is not None:
                                    updated += 1
                                else:
                                    created += 1
                                continue

                            try:
                                write_payload = dict(payload)
                                write_payload.pop("_match_key", None)
                                if matched is None:
                                    repo.create_coin_reference(
                                        actor=user.username,
                                        **write_payload,
                                    )
                                    created += 1
                                else:
                                    repo.update_coin_reference(
                                        int(matched.id),
                                        write_payload,
                                        actor=user.username,
                                    )
                                    updated += 1
                            except Exception as exc:
                                errors.append({"row": int(idx) + 2, "error": str(exc), "coin_name": coin_name})

                        mode_label = "applied" if import_apply else "dry run"
                        st.success(
                            f"Coin CSV {mode_label} complete. create={created}, update={updated}, errors={len(errors)}"
                        )
                        if actions_preview:
                            st.markdown("Preview actions")
                            st.dataframe(pd.DataFrame(actions_preview), use_container_width=True, hide_index=True)
                        if errors:
                            st.error("Some rows failed.")
                            st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
                        if import_apply and not errors:
                            st.rerun()
                except Exception as exc:
                    st.error(f"CSV import failed: {exc}")

        st.markdown("##### Add Coin Reference")
        with st.form("coin_db_create_form", clear_on_submit=True):
            r1c1, r1c2, r1c3, r1c4 = st.columns(4)
            with r1c1:
                create_coin_name = st.text_input("Coin Name *", value="", key="coin_db_create_name")
            with r1c2:
                create_country = st.text_input("Country", value="", key="coin_db_create_country")
            with r1c3:
                create_denomination = st.text_input("Denomination", value="", key="coin_db_create_denom")
            with r1c4:
                create_series = st.text_input("Series", value="", key="coin_db_create_series")
            r2c1, r2c2, r2c3, r2c4 = st.columns(4)
            with r2c1:
                create_year_start = st.number_input("Year Start", min_value=0, value=0, step=1, key="coin_db_create_year_start")
            with r2c2:
                create_year_end = st.number_input("Year End", min_value=0, value=0, step=1, key="coin_db_create_year_end")
            with r2c3:
                create_mint_mark = st.text_input("Mint Mark", value="", key="coin_db_create_mint")
            with r2c4:
                create_metal = st.text_input("Metal Type", value="", key="coin_db_create_metal")
            r3c1, r3c2, r3c3, r3c4 = st.columns(4)
            with r3c1:
                create_weight_grams = st.number_input("Weight (grams)", min_value=0.0, value=0.0, step=0.0001, format="%.4f", key="coin_db_create_weight")
            with r3c2:
                create_asw_oz = st.number_input("ASW (oz)", min_value=0.0, value=0.0, step=0.0001, format="%.4f", key="coin_db_create_asw")
            with r3c3:
                create_diameter_mm = st.number_input("Diameter (mm)", min_value=0.0, value=0.0, step=0.01, format="%.2f", key="coin_db_create_diameter")
            with r3c4:
                create_thickness_mm = st.number_input("Thickness (mm)", min_value=0.0, value=0.0, step=0.01, format="%.2f", key="coin_db_create_thickness")
            r4c1, r4c2, r4c3 = st.columns(3)
            with r4c1:
                create_km = st.text_input("KM Number", value="", key="coin_db_create_km")
            with r4c2:
                create_pcgs = st.text_input("PCGS No", value="", key="coin_db_create_pcgs")
            with r4c3:
                create_ngc = st.text_input("NGC ID", value="", key="coin_db_create_ngc")
            r5c1, r5c2, r5c3 = st.columns(3)
            with r5c1:
                create_est_low = st.number_input("Est Value Low", min_value=0.0, value=0.0, step=0.01, key="coin_db_create_est_low")
            with r5c2:
                create_est_high = st.number_input("Est Value High", min_value=0.0, value=0.0, step=0.01, key="coin_db_create_est_high")
            with r5c3:
                create_price_source = st.text_input("Price Source", value="", key="coin_db_create_price_source")
            create_source_url = st.text_input("Source URL", value="", key="coin_db_create_source_url")
            create_composition = st.text_input("Composition", value="", key="coin_db_create_composition")
            create_mintage = st.text_input("Mintage", value="", key="coin_db_create_mintage")
            create_issuer = st.text_input("Issuer", value="", key="coin_db_create_issuer")
            create_tags = st.text_input("Tags (comma-separated)", value="", key="coin_db_create_tags")
            create_obverse = st.text_area("Obverse Description", value="", key="coin_db_create_obverse")
            create_reverse = st.text_area("Reverse Description", value="", key="coin_db_create_reverse")
            create_notes = st.text_area("Notes", value="", key="coin_db_create_notes")
            create_is_active = st.checkbox("Active", value=True, key="coin_db_create_is_active")
            create_submit = st.form_submit_button("Add Coin Reference")
        if create_submit:
            try:
                year_start = int(create_year_start) if int(create_year_start) > 0 else None
                year_end = int(create_year_end) if int(create_year_end) > 0 else None
                repo.create_coin_reference(
                    coin_name=create_coin_name.strip(),
                    country=create_country.strip(),
                    issuer=create_issuer.strip(),
                    denomination=create_denomination.strip(),
                    series=create_series.strip(),
                    year_start=year_start,
                    year_end=year_end,
                    mint_mark=create_mint_mark.strip(),
                    composition=create_composition.strip(),
                    metal_type=create_metal.strip(),
                    weight_grams=Decimal(str(create_weight_grams)) if float(create_weight_grams) > 0 else None,
                    asw_oz=Decimal(str(create_asw_oz)) if float(create_asw_oz) > 0 else None,
                    diameter_mm=Decimal(str(create_diameter_mm)) if float(create_diameter_mm) > 0 else None,
                    thickness_mm=Decimal(str(create_thickness_mm)) if float(create_thickness_mm) > 0 else None,
                    km_number=create_km.strip(),
                    pcgs_no=create_pcgs.strip(),
                    ngc_id=create_ngc.strip(),
                    mintage=create_mintage.strip(),
                    estimated_value_low=Decimal(str(create_est_low)) if float(create_est_low) > 0 else None,
                    estimated_value_high=Decimal(str(create_est_high)) if float(create_est_high) > 0 else None,
                    price_source=create_price_source.strip(),
                    source_url=create_source_url.strip(),
                    tags=create_tags.strip(),
                    obverse_description=create_obverse.strip(),
                    reverse_description=create_reverse.strip(),
                    notes=create_notes.strip(),
                    is_active=bool(create_is_active),
                    actor=user.username,
                )
                st.success("Coin reference added.")
                st.rerun()
            except Exception as exc:
                st.error(f"Create failed: {exc}")

        if coin_rows:
            st.markdown("##### Edit Coin Reference")
            edit_map = {
                f"#{row.id} | {row.coin_name} | {row.country} | {row.series}": row
                for row in coin_rows
            }
            selected_edit_key = st.selectbox("Select row", options=list(edit_map.keys()), key="coin_db_edit_select")
            selected_row = edit_map[selected_edit_key]
            edit_key_prefix = f"coin_db_edit_{int(selected_row.id)}"
            with st.form("coin_db_edit_form"):
                e1, e2, e3, e4 = st.columns(4)
                with e1:
                    edit_coin_name = st.text_input("Coin Name *", value=selected_row.coin_name or "", key=f"{edit_key_prefix}_name")
                with e2:
                    edit_country = st.text_input("Country", value=selected_row.country or "", key=f"{edit_key_prefix}_country")
                with e3:
                    edit_denomination = st.text_input("Denomination", value=selected_row.denomination or "", key=f"{edit_key_prefix}_denom")
                with e4:
                    edit_series = st.text_input("Series", value=selected_row.series or "", key=f"{edit_key_prefix}_series")
                e5, e6, e7, e8 = st.columns(4)
                with e5:
                    edit_est_low = st.number_input(
                        "Est Value Low",
                        min_value=0.0,
                        value=float(selected_row.estimated_value_low or 0.0),
                        step=0.01,
                        key=f"{edit_key_prefix}_est_low",
                    )
                with e6:
                    edit_est_high = st.number_input(
                        "Est Value High",
                        min_value=0.0,
                        value=float(selected_row.estimated_value_high or 0.0),
                        step=0.01,
                        key=f"{edit_key_prefix}_est_high",
                    )
                with e7:
                    edit_price_source = st.text_input("Price Source", value=selected_row.price_source or "", key=f"{edit_key_prefix}_price_source")
                with e8:
                    edit_is_active = st.checkbox("Active", value=bool(selected_row.is_active), key=f"{edit_key_prefix}_is_active")
                edit_source_url = st.text_input("Source URL", value=selected_row.source_url or "", key=f"{edit_key_prefix}_source_url")
                edit_tags = st.text_input("Tags", value=selected_row.tags or "", key=f"{edit_key_prefix}_tags")
                edit_notes = st.text_area("Notes", value=selected_row.notes or "", key=f"{edit_key_prefix}_notes")
                edit_submit = st.form_submit_button("Save Coin Reference")
            if edit_submit:
                try:
                    repo.update_coin_reference(
                        int(selected_row.id),
                        {
                            "coin_name": edit_coin_name.strip(),
                            "country": edit_country.strip(),
                            "denomination": edit_denomination.strip(),
                            "series": edit_series.strip(),
                            "estimated_value_low": Decimal(str(edit_est_low)) if float(edit_est_low) > 0 else None,
                            "estimated_value_high": Decimal(str(edit_est_high)) if float(edit_est_high) > 0 else None,
                            "price_source": edit_price_source.strip(),
                            "source_url": edit_source_url.strip(),
                            "tags": edit_tags.strip(),
                            "notes": edit_notes.strip(),
                            "is_active": bool(edit_is_active),
                        },
                        actor=user.username,
                    )
                    st.success("Coin reference updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Update failed: {exc}")

    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="tools",
        section_title="Workspace Feedback: Tools",
    )
