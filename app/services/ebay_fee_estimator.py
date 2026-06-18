from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.runtime_settings import get_runtime_float


DEFAULT_FINAL_VALUE_RATE_PERCENT = 13.25
DEFAULT_FINAL_VALUE_FIXED_USD = 0.30
DEFAULT_PAYMENT_RATE_PERCENT = 0.00
DEFAULT_PAYMENT_FIXED_USD = 0.00
DEFAULT_PROMOTED_RATE_PERCENT = 0.00


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _non_negative(value: float) -> float:
    return max(0.0, float(value))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


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


def resolve_product_known_unit_cost(product: Any) -> Decimal:
    if product is None:
        return Decimal("0.00")
    product_cost = _money_decimal(getattr(product, "product_cost", None))
    if product_cost > Decimal("0.00"):
        return product_cost
    landed = (
        _money_decimal(getattr(product, "acquisition_cost", None))
        + _money_decimal(getattr(product, "acquisition_tax_paid", None))
        + _money_decimal(getattr(product, "acquisition_shipping_paid", None))
        + _money_decimal(getattr(product, "acquisition_handling_paid", None))
    )
    return max(landed, Decimal("0.00"))


def _rate_decimal(percent_value: Any) -> Decimal:
    return _money_decimal(percent_value) / Decimal("100")


def _round_decimal_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def calculate_ebay_fee_profit_estimate(
    *,
    sale_price: Any,
    buyer_shipping_charged: Any = 0,
    sales_tax_collected: Any = 0,
    item_cost: Any = 0,
    shipping_label_cost: Any = 0,
    packaging_cost: Any = 0,
    final_value_fee_percent: Any = DEFAULT_FINAL_VALUE_RATE_PERCENT,
    fixed_order_fee: Any | None = None,
    promoted_ad_percent: Any = 0,
    additional_fee_percent: Any = 0,
    insertion_or_upgrade_fee: Any = 0,
    include_sales_tax_in_fee_basis: bool = True,
) -> dict[str, Decimal]:
    sale = max(_money_decimal(sale_price), Decimal("0.00"))
    shipping_charged = max(_money_decimal(buyer_shipping_charged), Decimal("0.00"))
    tax_collected = max(_money_decimal(sales_tax_collected), Decimal("0.00"))
    cogs = max(_money_decimal(item_cost), Decimal("0.00"))
    label_cost = max(_money_decimal(shipping_label_cost), Decimal("0.00"))
    pack_cost = max(_money_decimal(packaging_cost), Decimal("0.00"))
    insertion_fee = max(_money_decimal(insertion_or_upgrade_fee), Decimal("0.00"))
    fvf_rate = max(_rate_decimal(final_value_fee_percent), Decimal("0"))
    promoted_rate = max(_rate_decimal(promoted_ad_percent), Decimal("0"))
    additional_rate = max(_rate_decimal(additional_fee_percent), Decimal("0"))
    if fixed_order_fee is None:
        fixed_fee = Decimal("0.30") if sale + shipping_charged <= Decimal("10.00") else Decimal("0.40")
    else:
        fixed_fee = max(_money_decimal(fixed_order_fee), Decimal("0.00"))

    gross_customer_paid = sale + shipping_charged + tax_collected
    gross_revenue = sale + shipping_charged
    fee_basis = sale + shipping_charged + (tax_collected if include_sales_tax_in_fee_basis else Decimal("0.00"))
    final_value_fee = fee_basis * fvf_rate
    promoted_fee = sale * promoted_rate
    additional_fee = fee_basis * additional_rate
    estimated_total_fees = final_value_fee + fixed_fee + promoted_fee + additional_fee + insertion_fee
    net_before_cogs = gross_revenue - estimated_total_fees - label_cost - pack_cost
    estimated_profit = net_before_cogs - cogs
    margin_percent = (
        estimated_profit / gross_revenue * Decimal("100")
        if gross_revenue > Decimal("0.00")
        else Decimal("0.00")
    )

    variable_rate = fvf_rate + promoted_rate + additional_rate
    fixed_fee_basis_addon = shipping_charged + (
        tax_collected if include_sales_tax_in_fee_basis else Decimal("0.00")
    )
    denominator = Decimal("1.00") - variable_rate
    breakeven_sale_price = Decimal("0.00")
    if denominator > Decimal("0.00"):
        numerator = (
            cogs
            + label_cost
            + pack_cost
            + fixed_fee
            + insertion_fee
            + (fvf_rate + additional_rate) * fixed_fee_basis_addon
            - shipping_charged
        )
        breakeven_sale_price = max(numerator / denominator, Decimal("0.00"))

    return {
        "sale_price": _round_decimal_money(sale),
        "buyer_shipping_charged": _round_decimal_money(shipping_charged),
        "sales_tax_collected": _round_decimal_money(tax_collected),
        "gross_customer_paid": _round_decimal_money(gross_customer_paid),
        "gross_revenue": _round_decimal_money(gross_revenue),
        "fee_basis": _round_decimal_money(fee_basis),
        "final_value_fee": _round_decimal_money(final_value_fee),
        "fixed_order_fee": _round_decimal_money(fixed_fee),
        "promoted_ad_fee": _round_decimal_money(promoted_fee),
        "additional_fee": _round_decimal_money(additional_fee),
        "insertion_or_upgrade_fee": _round_decimal_money(insertion_fee),
        "estimated_total_fees": _round_decimal_money(estimated_total_fees),
        "shipping_label_cost": _round_decimal_money(label_cost),
        "packaging_cost": _round_decimal_money(pack_cost),
        "item_cost": _round_decimal_money(cogs),
        "net_before_cogs": _round_decimal_money(net_before_cogs),
        "estimated_profit": _round_decimal_money(estimated_profit),
        "margin_percent": margin_percent.quantize(Decimal("0.01")),
        "breakeven_sale_price": _round_decimal_money(breakeven_sale_price),
    }


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
    profit_estimate = calculate_ebay_fee_profit_estimate(
        sale_price=item_subtotal,
        buyer_shipping_charged=buyer_shipping,
        item_cost=0,
        shipping_label_cost=0,
        packaging_cost=0,
        final_value_fee_percent=final_value_rate_percent,
        fixed_order_fee=final_value_fixed_usd,
        promoted_ad_percent=promoted_rate,
        additional_fee_percent=payment_rate_percent,
        insertion_or_upgrade_fee=payment_fixed_usd,
        include_sales_tax_in_fee_basis=False,
    )

    final_value_fee = float(profit_estimate["final_value_fee"]) + final_value_fixed_usd
    payment_fee = float(profit_estimate["additional_fee"]) + payment_fixed_usd
    promoted_fee = float(profit_estimate["promoted_ad_fee"])
    total_fees = float(profit_estimate["estimated_total_fees"])
    net_payout_before_shipping_cost = float(profit_estimate["net_before_cogs"])

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


def calculate_expected_net_score(
    *,
    fee_estimate: dict[str, Any],
    quantity: int,
    known_unit_cost: float,
    estimated_local_shipping_cost_per_item: float,
) -> dict[str, float | str]:
    qty = max(1, int(quantity or 1))
    gross = max(0.0, _to_float((fee_estimate or {}).get("gross_total")))
    est_fees = max(0.0, _to_float((fee_estimate or {}).get("estimated_total_fees")))
    est_payout = max(0.0, _to_float((fee_estimate or {}).get("estimated_net_payout_before_shipping_cost")))
    cogs_total = max(0.0, _to_float(known_unit_cost) * float(qty))
    local_ship_total = max(0.0, _to_float(estimated_local_shipping_cost_per_item) * float(qty))
    expected_net = round(est_payout - local_ship_total - cogs_total, 2)
    expected_margin_pct = round(((expected_net / gross) * 100.0), 2) if gross > 0 else 0.0
    buyer_shipping = max(0.0, _to_float((fee_estimate or {}).get("buyer_paid_shipping")))
    item_subtotal_raw = _to_float((fee_estimate or {}).get("item_subtotal"), default=-1.0)
    item_subtotal = max(0.0, item_subtotal_raw if item_subtotal_raw >= 0.0 else (gross - buyer_shipping))
    final_value_rate = max(0.0, _to_float((fee_estimate or {}).get("final_value_rate_percent"))) / 100.0
    payment_rate = max(0.0, _to_float((fee_estimate or {}).get("payment_rate_percent"))) / 100.0
    promoted_rate = max(0.0, _to_float((fee_estimate or {}).get("promoted_rate_percent"))) / 100.0
    fixed_fees = max(0.0, _to_float((fee_estimate or {}).get("final_value_fixed_usd"))) + max(
        0.0,
        _to_float((fee_estimate or {}).get("payment_fixed_usd")),
    )
    variable_rate = final_value_rate + payment_rate + promoted_rate
    shipping_fee_rate = final_value_rate + payment_rate
    if variable_rate <= 0.0 and gross > 0.0 and est_fees > 0.0:
        variable_rate = min(0.95, est_fees / gross)
        shipping_fee_rate = variable_rate
    denominator = 1.0 - variable_rate
    breakeven_listing_price = 0.0
    if denominator > 0.0:
        required_before_variable_fee = (
            cogs_total
            + local_ship_total
            + fixed_fees
            + (shipping_fee_rate * buyer_shipping)
            - buyer_shipping
        )
        breakeven_listing_price = round(max(0.0, required_before_variable_fee / denominator), 2)
    breakeven_unit_price = round(breakeven_listing_price / float(qty), 2) if qty > 0 else 0.0
    price_cushion = round(item_subtotal - breakeven_listing_price, 2)
    if expected_margin_pct >= 25.0:
        score = "strong"
    elif expected_margin_pct >= 10.0:
        score = "good"
    elif expected_margin_pct >= 0.0:
        score = "thin"
    else:
        score = "negative"
    return {
        "gross_total": round(gross, 2),
        "estimated_total_fees": round(est_fees, 2),
        "estimated_payout_before_shipping": round(est_payout, 2),
        "known_cogs_total": round(cogs_total, 2),
        "estimated_local_shipping_total": round(local_ship_total, 2),
        "expected_net": expected_net,
        "expected_margin_pct_of_gross": expected_margin_pct,
        "breakeven_listing_price": breakeven_listing_price,
        "breakeven_unit_price": breakeven_unit_price,
        "price_cushion": price_cushion,
        "score": score,
    }
