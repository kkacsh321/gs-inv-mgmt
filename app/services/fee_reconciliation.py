from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return str(value)
    except Exception:
        return None


def _extract_order_fee_breakdown_from_notes(notes: str | None) -> dict:
    raw = str(notes or "").strip()
    if not raw:
        return {}
    marker = "fee_breakdown_json="
    idx = raw.find(marker)
    if idx < 0:
        return {}
    json_raw = raw[idx + len(marker):].strip()
    if "; " in json_raw:
        json_raw = json_raw.split("; ", 1)[0].strip()
    if not json_raw:
        return {}
    try:
        payload = json.loads(json_raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_listing_fee_estimate_payload(listing_marketplace_details: str | None) -> dict:
    raw = str(listing_marketplace_details or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    publish_meta = parsed.get("ebay_publish")
    if not isinstance(publish_meta, dict):
        return {}
    fee_estimate = publish_meta.get("fee_estimate")
    if isinstance(fee_estimate, dict):
        return fee_estimate
    return {}


def build_ebay_fee_reconciliation_rows(sales: list[Any]) -> list[dict]:
    rows: list[dict] = []
    for s in sales:
        marketplace = str(getattr(s, "marketplace", "") or "").strip().lower()
        if marketplace != "ebay":
            continue
        listing = getattr(s, "listing", None)
        fee_estimate = parse_listing_fee_estimate_payload(
            str(getattr(listing, "marketplace_details", "") or "") if listing is not None else ""
        )
        est_total_raw = _safe_float(fee_estimate.get("estimated_total_fees"))
        est_basis_qty_raw = int(_safe_float(fee_estimate.get("quantity") or 0))
        sale_qty = max(1, int(getattr(s, "quantity_sold", 0) or 1))
        est_basis_qty = max(1, est_basis_qty_raw) if est_total_raw > 0 else 0
        est_scaled = 0.0
        if est_total_raw > 0 and est_basis_qty > 0:
            est_scaled = est_total_raw * (float(sale_qty) / float(est_basis_qty))
        order = getattr(s, "order", None)
        order_fee_breakdown = _extract_order_fee_breakdown_from_notes(
            str(getattr(order, "notes", "") or "") if order is not None else ""
        )
        order_marketplace_fee = _safe_float(order_fee_breakdown.get("total_marketplace_fee"))
        sale_fee_field = _safe_float(getattr(s, "fees", 0.0))
        if order_marketplace_fee > 0:
            actual_fee = order_marketplace_fee
            actual_fee_source = "order_fee_breakdown_total_marketplace_fee"
        else:
            actual_fee = sale_fee_field
            actual_fee_source = "sale_fees_field"
        variance = actual_fee - est_scaled
        variance_pct = (variance / est_scaled * 100.0) if est_scaled > 0 else 0.0
        estimate_final_value_rate_percent = _safe_float(fee_estimate.get("final_value_rate_percent"))
        estimate_final_value_fixed_usd = _safe_float(fee_estimate.get("final_value_fixed_usd"))
        estimate_payment_rate_percent = _safe_float(fee_estimate.get("payment_rate_percent"))
        estimate_payment_fixed_usd = _safe_float(fee_estimate.get("payment_fixed_usd"))
        estimate_promoted_rate_percent = _safe_float(fee_estimate.get("promoted_rate_percent"))
        sale_gross = _safe_float(getattr(s, "sold_price", 0.0))
        implied_final_value_rate = 0.0
        if sale_gross > 0:
            non_fv_component = (
                (sale_gross * estimate_payment_rate_percent / 100.0)
                + estimate_payment_fixed_usd
                + (sale_gross * estimate_promoted_rate_percent / 100.0)
                + estimate_final_value_fixed_usd
            )
            implied_final_value_rate = ((actual_fee - non_fv_component) / sale_gross) * 100.0
        rows.append(
            {
                "sale_id": int(getattr(s, "id", 0) or 0),
                "sold_at": _iso_or_none(getattr(s, "sold_at", None)),
                "external_order_id": str(getattr(s, "external_order_id", "") or "").strip(),
                "listing_id": int(getattr(s, "listing_id", 0) or 0) or None,
                "external_listing_id": str(getattr(listing, "external_listing_id", "") or "").strip() if listing else "",
                "sku": getattr(getattr(s, "product", None), "sku", None),
                "product_title": getattr(getattr(s, "product", None), "title", None),
                "quantity_sold": sale_qty,
                "sale_gross": round(sale_gross, 2),
                "actual_fee": round(actual_fee, 2),
                "actual_fee_source": actual_fee_source,
                "sale_fee_field": round(sale_fee_field, 2),
                "order_fee_breakdown_total_marketplace_fee": round(order_marketplace_fee, 2),
                "order_fee_breakdown_present": bool(order_marketplace_fee > 0),
                "delta_sale_fee_field_vs_order_breakdown": round(sale_fee_field - order_marketplace_fee, 2)
                if order_marketplace_fee > 0
                else 0.0,
                "estimated_fee_scaled": round(est_scaled, 2),
                "estimated_fee_source_total": round(est_total_raw, 2),
                "estimated_fee_source_qty": int(est_basis_qty_raw or 0),
                "variance_actual_minus_estimate": round(variance, 2),
                "variance_percent_of_estimate": round(variance_pct, 2),
                "fee_estimate_present": bool(est_total_raw > 0),
                "estimate_final_value_rate_percent": round(estimate_final_value_rate_percent, 4),
                "estimate_final_value_fixed_usd": round(estimate_final_value_fixed_usd, 2),
                "estimate_payment_rate_percent": round(estimate_payment_rate_percent, 4),
                "estimate_payment_fixed_usd": round(estimate_payment_fixed_usd, 2),
                "estimate_promoted_rate_percent": round(estimate_promoted_rate_percent, 4),
                "implied_final_value_rate_percent": round(implied_final_value_rate, 4),
            }
        )
    return rows
