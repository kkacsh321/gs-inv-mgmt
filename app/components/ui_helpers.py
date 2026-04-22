from datetime import datetime
from decimal import Decimal
import json
import re
from typing import Any

import pandas as pd

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


def normalize_multiselect_values(values: Any, options: list[Any]) -> list[str]:
    if not isinstance(values, list):
        return []
    option_set = {str(v) for v in (options or [])}
    normalized: list[str] = []
    for value in values:
        token = str(value)
        if token in option_set:
            normalized.append(token)
    return normalized


def safe_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows or [])
    if frame.empty:
        return frame
    for col in frame.columns:
        if frame[col].dtype == "object":
            frame[col] = frame[col].map(
                lambda v: (
                    json.dumps(v, ensure_ascii=True, default=str)
                    if isinstance(v, (dict, list, tuple, set))
                    else ("" if v is None else str(v))
                )
            )
    return frame


def format_ebay_sync_note_for_customer(raw_notes: str | None) -> str:
    raw = str(raw_notes or "").strip()
    if not raw:
        return ""

    parts: dict[str, str] = {}
    for key in ("buyer", "shipping_service", "ship_to"):
        pattern = re.compile(rf"{re.escape(key)}=([^;]+)", flags=re.IGNORECASE)
        match = pattern.search(raw)
        if match:
            parts[key] = str(match.group(1) or "").strip()

    if not parts:
        return raw

    lines: list[str] = []
    if "ebay sync pull" in raw.lower():
        lines.append("Imported from eBay sync pull.")
    if parts.get("buyer"):
        lines.append(f"Buyer: {parts['buyer']}")
    if parts.get("shipping_service"):
        lines.append(f"Shipping Service: {parts['shipping_service']}")
    if parts.get("ship_to"):
        lines.append(f"Ship To: {parts['ship_to']}")
    return "\n".join(lines).strip()
