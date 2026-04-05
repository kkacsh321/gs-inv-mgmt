from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests

from app.services.runtime_settings import get_runtime_int, get_runtime_str


@dataclass
class ShippingLabelResult:
    label_id: str
    label_url: str
    label_currency: str = "USD"
    label_cost: float | None = None
    tracking_number: str = ""
    provider_payload: dict[str, Any] | None = None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _pick(body: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = body
        ok = True
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except Exception:
                    ok = False
                    break
            else:
                ok = False
                break
        if ok and current not in (None, ""):
            return current
    return None


def _pirateship_purchase_label(repo: Any, *, payload: dict[str, Any], timeout_seconds: int = 20) -> ShippingLabelResult:
    mode = get_runtime_str(repo, "shipping_label_pirateship_mode", "mock").strip().lower() or "mock"
    timeout_seconds = max(5, min(120, int(get_runtime_int(repo, "shipping_label_pirateship_timeout_seconds", timeout_seconds))))
    provider = str(payload.get("shipping_provider") or "pirateship").strip() or "pirateship"
    tracking_number = str(payload.get("tracking_number") or "").strip()
    sale_id = str(payload.get("sale_id") or "").strip() or "sale"
    default_label_id = f"{provider}-LBL-LIVE-{sale_id}"
    default_label_url = f"https://labels.goldenstackers.local/{provider}/{default_label_id}.pdf"
    label_cost = _as_float(payload.get("shipping_label_cost"))
    label_currency = str(payload.get("shipping_label_currency") or "USD").strip() or "USD"

    if mode == "mock":
        if not tracking_number:
            tracking_number = f"PS-MOCK-{sale_id}"
        return ShippingLabelResult(
            label_id=default_label_id,
            label_url=default_label_url,
            label_currency=label_currency,
            label_cost=label_cost,
            tracking_number=tracking_number,
            provider_payload={"mode": "mock", "provider": "pirateship"},
        )

    if mode != "api":
        raise ValueError("Invalid `shipping_label_pirateship_mode`; expected `mock` or `api`.")

    base_url = get_runtime_str(repo, "shipping_label_pirateship_base_url", "").strip().rstrip("/")
    api_key = get_runtime_str(repo, "shipping_label_pirateship_api_key", "").strip()
    endpoint_path = get_runtime_str(repo, "shipping_label_pirateship_endpoint_path", "/v1/labels/purchase").strip()
    auth_scheme = get_runtime_str(repo, "shipping_label_pirateship_auth_scheme", "bearer").strip().lower()
    if not base_url or not api_key:
        raise ValueError("Pirate Ship live mode requires base URL + API key runtime settings.")

    endpoint = f"{base_url}/{endpoint_path.lstrip('/')}"
    if auth_scheme == "bearer":
        auth_header = f"Bearer {api_key}"
    elif auth_scheme in {"token", "apikey", "api_key"}:
        auth_header = api_key
    else:
        raise ValueError("Invalid `shipping_label_pirateship_auth_scheme`; expected `bearer` or `token`.")
    request_payload = {
        "sale_id": payload.get("sale_id"),
        "shipping_provider": provider,
        "shipping_service": payload.get("shipping_service"),
        "shipping_package_type": payload.get("shipping_package_type"),
        "tracking_number": tracking_number,
        "shipping_label_cost": payload.get("shipping_label_cost"),
        "shipping_label_currency": label_currency,
    }
    response = requests.post(
        endpoint,
        headers={
            "Authorization": auth_header,
            "Content-Type": "application/json",
        },
        data=json.dumps(request_payload),
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    body = response.json() if response.content else {}
    if not isinstance(body, dict):
        raise ValueError("Pirate Ship API response was not a JSON object.")

    label_id = str(
        _pick(body, "label_id", "id", "label.id", "shipment.label_id", "shipment.id") or default_label_id
    ).strip() or default_label_id
    label_url = str(
        _pick(body, "label_url", "url", "label.url", "label.href", "files.0.url") or default_label_url
    ).strip() or default_label_url
    out_tracking = str(
        _pick(body, "tracking_number", "tracking.number", "shipment.tracking_number") or tracking_number
    ).strip()
    out_currency = str(
        _pick(body, "label_currency", "currency", "cost.currency", "shipment.currency") or label_currency
    ).strip() or "USD"
    out_cost = _as_float(
        _pick(body, "label_cost", "cost.total", "rate.total", "shipment.label_cost", "cost")
    )

    return ShippingLabelResult(
        label_id=label_id,
        label_url=label_url,
        label_currency=out_currency,
        label_cost=out_cost if out_cost is not None else label_cost,
        tracking_number=out_tracking,
        provider_payload=body,
    )


def purchase_shipping_label(repo: Any, *, provider: str, payload: dict[str, Any]) -> ShippingLabelResult:
    normalized = str(provider or "").strip().lower()
    if normalized in {"pirateship", "pirate_ship"}:
        return _pirateship_purchase_label(repo, payload=payload)
    if normalized == "ebay_shipping":
        raise ValueError("eBay shipping live adapter is not implemented yet.")
    if normalized in {"usps", "ups", "fedex", "other", ""}:
        raise ValueError(f"Live adapter for provider `{normalized or 'other'}` is not implemented yet.")
    raise ValueError(f"Unsupported shipping label provider `{provider}`.")
