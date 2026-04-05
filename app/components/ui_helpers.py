from datetime import datetime
from decimal import Decimal
from typing import Any

from app.utils.time import utc_today


def to_decimal_or_none(value: float | int | None) -> Decimal | None:
    if value in (None, 0):
        return None
    return Decimal(str(value))


def to_decimal(value: float | int) -> Decimal:
    return Decimal(str(value))


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def build_product_options(
    products: list[Any], include_none: bool = False, include_id: bool = False
) -> dict[str, int | None]:
    options = {
        (
            f"#{p.id} | {p.sku} | {p.title}"
            if include_id
            else f"{p.sku} | {p.title}"
        ): p.id
        for p in products
    }
    if include_none:
        return {"None": None, **options}
    return options


def build_listing_options(
    listings: list[Any], include_none: bool = False, include_id: bool = True
) -> dict[str, int | None]:
    options = {
        (
            f"#{l.id} | {l.marketplace} | {l.listing_title}"
            if include_id
            else f"{l.marketplace} | {l.listing_title}"
        ): l.id
        for l in listings
    }
    if include_none:
        return {"None": None, **options}
    return options


def key_for_value(options: dict[str, Any], value: Any, fallback: str = "None") -> str:
    return next((k for k, v in options.items() if v == value), fallback)


def dataframe_date_bounds(values: list[datetime]) -> tuple[datetime.date, datetime.date]:
    now = utc_today()
    if not values:
        return now, now
    min_date = min(values).date()
    max_date = max(values).date()
    return min_date, max_date
