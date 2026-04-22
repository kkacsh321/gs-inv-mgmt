from __future__ import annotations

from typing import Any

from app.services.runtime_settings import get_runtime_float


DEFAULT_FINAL_VALUE_RATE_PERCENT = 13.25
DEFAULT_FINAL_VALUE_FIXED_USD = 0.30
DEFAULT_PAYMENT_RATE_PERCENT = 2.90
DEFAULT_PAYMENT_FIXED_USD = 0.30
DEFAULT_PROMOTED_RATE_PERCENT = 0.00


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _non_negative(value: float) -> float:
    return max(0.0, float(value))


def estimate_ebay_fees(
    repo: Any,
    *,
    unit_price: float,
    quantity: int = 1,
    buyer_paid_shipping: float = 0.0,
    promoted_rate_percent: float | None = None,
) -> dict[str, float]:
    qty = max(1, int(quantity or 1))
    price = _non_negative(unit_price)
    buyer_shipping = _non_negative(buyer_paid_shipping)

    final_value_rate_percent = _non_negative(
        get_runtime_float(
            repo,
            "ebay_fee_estimate_final_value_rate_percent",
            DEFAULT_FINAL_VALUE_RATE_PERCENT,
        )
    )
    final_value_fixed_usd = _non_negative(
        get_runtime_float(
            repo,
            "ebay_fee_estimate_final_value_fixed_per_order_usd",
            DEFAULT_FINAL_VALUE_FIXED_USD,
        )
    )
    payment_rate_percent = _non_negative(
        get_runtime_float(
            repo,
            "ebay_fee_estimate_payment_rate_percent",
            DEFAULT_PAYMENT_RATE_PERCENT,
        )
    )
    payment_fixed_usd = _non_negative(
        get_runtime_float(
            repo,
            "ebay_fee_estimate_payment_fixed_per_order_usd",
            DEFAULT_PAYMENT_FIXED_USD,
        )
    )
    promoted_percent_default = _non_negative(
        get_runtime_float(
            repo,
            "ebay_fee_estimate_promoted_rate_percent",
            DEFAULT_PROMOTED_RATE_PERCENT,
        )
    )
    promoted_rate = promoted_percent_default if promoted_rate_percent is None else _non_negative(promoted_rate_percent)

    item_subtotal = price * qty
    gross_total = item_subtotal + buyer_shipping

    final_value_fee = (gross_total * final_value_rate_percent / 100.0) + final_value_fixed_usd
    payment_fee = (gross_total * payment_rate_percent / 100.0) + payment_fixed_usd
    promoted_fee = item_subtotal * promoted_rate / 100.0
    total_fees = final_value_fee + payment_fee + promoted_fee
    net_payout_before_shipping_cost = gross_total - total_fees

    return {
        "unit_price": _round_money(price),
        "quantity": float(qty),
        "buyer_paid_shipping": _round_money(buyer_shipping),
        "item_subtotal": _round_money(item_subtotal),
        "gross_total": _round_money(gross_total),
        "final_value_rate_percent": round(final_value_rate_percent, 4),
        "final_value_fixed_usd": _round_money(final_value_fixed_usd),
        "payment_rate_percent": round(payment_rate_percent, 4),
        "payment_fixed_usd": _round_money(payment_fixed_usd),
        "promoted_rate_percent": round(promoted_rate, 4),
        "final_value_fee": _round_money(final_value_fee),
        "payment_fee": _round_money(payment_fee),
        "promoted_fee": _round_money(promoted_fee),
        "estimated_total_fees": _round_money(total_fees),
        "estimated_fee_percent_of_gross": round((total_fees / gross_total * 100.0), 2) if gross_total > 0 else 0.0,
        "estimated_net_payout_before_shipping_cost": _round_money(net_payout_before_shipping_cost),
    }
