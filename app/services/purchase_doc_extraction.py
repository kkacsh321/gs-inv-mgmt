import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import boto3

from app.config import settings


@dataclass(frozen=True)
class PurchaseDocExtractResult:
    payload: dict[str, Any]
    summary_text: str
    raw_provider: str


def _to_str(value: Any) -> str:
    return str(value or "").strip()


def _to_decimal_like(value: Any) -> float | None:
    text = _to_str(value)
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in {".", "-"})
    if not cleaned:
        return None
    try:
        return float(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None


def _summary_fields_to_payload(summary_fields: list[dict[str, Any]]) -> dict[str, Any]:
    field_map: dict[str, str] = {}
    for row in summary_fields or []:
        label = _to_str((row.get("Type") or {}).get("Text")).upper()
        value = _to_str((row.get("ValueDetection") or {}).get("Text"))
        if label and value and label not in field_map:
            field_map[label] = value

    payload: dict[str, Any] = {
        "vendor_name": field_map.get("VENDOR_NAME", ""),
        "invoice_number": field_map.get("INVOICE_RECEIPT_ID", ""),
        "invoice_date": field_map.get("INVOICE_RECEIPT_DATE", ""),
        "due_date": field_map.get("DUE_DATE", ""),
        "subtotal": _to_decimal_like(field_map.get("SUBTOTAL")),
        "tax": _to_decimal_like(field_map.get("TAX")),
        "shipping": _to_decimal_like(field_map.get("SHIPPING_HANDLING_CHARGE")),
        "total": _to_decimal_like(field_map.get("TOTAL")),
        "currency": _to_str(field_map.get("CURRENCY", "USD")) or "USD",
        "payment_method": _to_str(field_map.get("PAYMENT_TERMS", "")),
        "account_reference": _to_str(field_map.get("ACCOUNT_NUMBER", "")),
        "notes": "",
        "confidence": "medium",
        "line_items": [],
        "provider": "aws_textract",
    }
    return payload


def _line_items_to_payload(line_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    line_items: list[dict[str, Any]] = []
    for group in line_groups or []:
        for raw_item in group.get("LineItems") or []:
            fields = raw_item.get("LineItemExpenseFields") or []
            entry: dict[str, Any] = {
                "description": "",
                "quantity": None,
                "unit_price": None,
                "line_total": None,
            }
            for field in fields:
                kind = _to_str((field.get("Type") or {}).get("Text")).upper()
                text = _to_str((field.get("ValueDetection") or {}).get("Text"))
                if not text:
                    continue
                if kind in {"ITEM", "ITEM_NAME", "DESCRIPTION", "PRODUCT_CODE"} and not entry["description"]:
                    entry["description"] = text
                elif kind in {"QUANTITY", "QTY"}:
                    entry["quantity"] = _to_decimal_like(text)
                elif kind in {"PRICE", "UNIT_PRICE"}:
                    entry["unit_price"] = _to_decimal_like(text)
                elif kind in {"AMOUNT", "TOTAL", "LINE_TOTAL"}:
                    entry["line_total"] = _to_decimal_like(text)
            if any(entry.values()):
                if not entry["description"]:
                    entry["description"] = "Line item"
                line_items.append(entry)
    return line_items


def _merge_payloads(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    merged = dict(fallback or {})
    for key, value in (primary or {}).items():
        if key == "line_items":
            primary_items = value if isinstance(value, list) else []
            fallback_items = merged.get("line_items") if isinstance(merged.get("line_items"), list) else []
            merged["line_items"] = primary_items if primary_items else fallback_items
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            if key not in merged:
                merged[key] = value
            continue
        merged[key] = value
    return merged


def extract_with_textract(file_bytes: bytes, content_type: str) -> PurchaseDocExtractResult:
    if not file_bytes:
        raise ValueError("File bytes are required for Textract extraction.")

    session = boto3.session.Session(
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=settings.aws_region,
    )
    client = session.client("textract")

    expense_response = client.analyze_expense(Document={"Bytes": file_bytes})
    documents = expense_response.get("ExpenseDocuments") or []
    if not documents:
        raise RuntimeError("Textract returned no ExpenseDocuments.")

    first = documents[0]
    summary_fields = first.get("SummaryFields") or []
    line_groups = first.get("LineItemGroups") or []

    payload = _summary_fields_to_payload(summary_fields)
    payload["line_items"] = _line_items_to_payload(line_groups)
    payload["notes"] = (
        f"Textract extraction from {content_type or 'unknown'} via AnalyzeExpense; "
        f"summary_fields={len(summary_fields)} line_items={len(payload.get('line_items') or [])}."
    )

    return PurchaseDocExtractResult(
        payload=payload,
        summary_text=json.dumps(payload, indent=2),
        raw_provider="aws_textract",
    )


def merge_llm_and_textract(llm_payload: dict[str, Any], textract_payload: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_payloads(llm_payload or {}, textract_payload or {})
    merged["provider"] = "llm+aws_textract"
    return merged
