from __future__ import annotations

from collections.abc import Mapping


COGS_REVIEW_SOURCES = frozenset({"lot_equal_quantity_fallback", "missing_cost_basis", "unknown", "mixed_fifo_cost"})
COGS_ESTIMATE_SOURCES = frozenset({"lot_expected_quantity_fallback", "mixed_estimate_fifo_cost"})

COGS_BASIS_REASON_BY_SOURCE = {
    "lot_equal_quantity_fallback": (
        "Lot COGS used equal-quantity fallback. Set expected lot quantity, allocation weights, "
        "or assignment-level landed costs before close sign-off."
    ),
    "missing_cost_basis": "No product, lot, or assignment cost basis was available. Add landed-cost evidence.",
    "unknown": "COGS source is unknown. Review sale cost evidence before close sign-off.",
    "mixed_fifo_cost": "Sale COGS blended reviewed and fallback FIFO basis sources. Review allocation evidence.",
    "mixed_estimate_fifo_cost": (
        "Sale COGS blended explicit/default basis with expected-quantity lot estimates. "
        "Recheck after final allocation."
    ),
    "mixed_verified_fifo_cost": "Sale COGS blended multiple verified FIFO basis sources.",
    "lot_expected_quantity_fallback": "Partial-lot COGS used expected lot quantity. Recheck after final lot allocation.",
}


def normalize_cogs_source(source: object) -> str:
    return str(source or "unknown").strip() or "unknown"


def cogs_basis_bucket(source: object) -> str:
    normalized = normalize_cogs_source(source)
    if normalized in COGS_REVIEW_SOURCES:
        return "review"
    if normalized in COGS_ESTIMATE_SOURCES:
        return "estimate"
    return "ok"


def cogs_basis_review_fields(source: object) -> dict[str, object]:
    normalized = normalize_cogs_source(source)
    bucket = cogs_basis_bucket(normalized)
    return {
        "cogs_basis_bucket": bucket,
        "basis_review_required": bucket == "review",
        "basis_is_estimate": bucket == "estimate",
        "basis_review_severity": bucket,
        "basis_review_reason": COGS_BASIS_REASON_BY_SOURCE.get(normalized, ""),
    }


def safe_float(value: object) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def net_before_cogs(
    *,
    gross: object,
    shipping_charged: object = 0.0,
    fees: object = 0.0,
    label_spend: object = 0.0,
) -> float:
    return round(safe_float(gross) + safe_float(shipping_charged) - safe_float(fees) - safe_float(label_spend), 2)


def shipping_delta(*, shipping_charged: object = 0.0, label_spend: object = 0.0) -> float:
    return round(safe_float(shipping_charged) - safe_float(label_spend), 2)


def profit_before_returns(*, net_before_cogs_amount: object, cogs: object = 0.0) -> float:
    return round(safe_float(net_before_cogs_amount) - safe_float(cogs), 2)


def return_refund_total(
    *,
    refund_amount: object = 0.0,
    refund_fees: object = 0.0,
    refund_shipping: object = 0.0,
) -> float:
    return round(safe_float(refund_amount) + safe_float(refund_fees) + safe_float(refund_shipping), 2)


def returns_profit_impact(*, refund_total: object = 0.0, cogs_reversal: object = 0.0) -> float:
    return round(-safe_float(refund_total) + safe_float(cogs_reversal), 2)


def profit_after_returns(*, profit_before_returns_amount: object = 0.0, returns_profit_impact_amount: object = 0.0) -> float:
    return round(safe_float(profit_before_returns_amount) + safe_float(returns_profit_impact_amount), 2)


def cogs_evidence_split(
    source_totals: Mapping[str, object],
    source_counts: Mapping[str, object] | None = None,
) -> dict[str, float | int]:
    counts = source_counts or {}
    split: dict[str, float | int] = {
        "verified_amount": 0.0,
        "estimated_amount": 0.0,
        "review_needed_amount": 0.0,
        "verified_sale_rows": 0,
        "estimated_sale_rows": 0,
        "review_needed_sale_rows": 0,
    }
    for source, total in source_totals.items():
        bucket = cogs_basis_bucket(source)
        if bucket == "review":
            amount_key = "review_needed_amount"
            rows_key = "review_needed_sale_rows"
        elif bucket == "estimate":
            amount_key = "estimated_amount"
            rows_key = "estimated_sale_rows"
        else:
            amount_key = "verified_amount"
            rows_key = "verified_sale_rows"
        split[amount_key] = round(float(split[amount_key]) + safe_float(total), 2)
        split[rows_key] = int(split[rows_key]) + int(counts.get(source, 0) or 0)
    return split


def format_cogs_evidence_split(split: Mapping[str, object]) -> str:
    total = (
        safe_float(split.get("verified_amount"))
        + safe_float(split.get("estimated_amount"))
        + safe_float(split.get("review_needed_amount"))
    )
    if total <= 0:
        return ""
    return (
        f"verified ${safe_float(split.get('verified_amount')):,.2f}; "
        f"estimated ${safe_float(split.get('estimated_amount')):,.2f}; "
        f"review-needed ${safe_float(split.get('review_needed_amount')):,.2f}"
    )
