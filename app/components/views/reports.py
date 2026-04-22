from datetime import datetime
from collections import defaultdict, deque
import json
from types import SimpleNamespace

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.auth import current_user, ensure_permission, has_permission
from app.db.models import OrderFinanceEntry
from app.config import settings
from app.components.ui_helpers import iso_or_none
from app.components.views.shared import (
    dataframe_to_xlsx_bytes,
    handoff_to_documents_draft,
    render_help_panel,
)
from app.components.views.workspace_shell import render_workspace_feedback
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.fee_calibration import build_final_value_rate_calibration
from app.services.fee_reconciliation import build_ebay_fee_reconciliation_rows
from app.services.runtime_settings import get_runtime_bool, get_runtime_float, get_runtime_str
from app.utils.time import utc_today


def _safe_float(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _landed_unit_cost_from_product(product) -> float:
    return (
        _safe_float(getattr(product, "acquisition_cost", 0))
        + _safe_float(getattr(product, "acquisition_tax_paid", 0))
        + _safe_float(getattr(product, "acquisition_shipping_paid", 0))
        + _safe_float(getattr(product, "acquisition_handling_paid", 0))
    )


def _parse_csv_set(value: str) -> set[str]:
    return {str(part).strip().lower() for part in str(value or "").split(",") if str(part).strip()}


def _add_margin_pct_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    gross = df["gross_sales"].replace(0, pd.NA)
    df["fifo_margin_pct"] = (df["fifo_margin"] / gross).fillna(0.0)
    df["lot_margin_pct"] = (df["lot_margin"] / gross).fillna(0.0)
    df["fifo_margin_actual_pct"] = (df["fifo_margin_actual"] / gross).fillna(0.0)
    df["lot_margin_actual_pct"] = (df["lot_margin_actual"] / gross).fillna(0.0)
    return df


def _build_economics_intelligence_drilldowns(
    economics_intel_df: pd.DataFrame,
    *,
    min_margin_alert_pct: float,
    max_fee_variance_alert_usd: float,
    min_group_sales_for_alert: int,
) -> dict[str, pd.DataFrame]:
    empty = {
        "by_sku": pd.DataFrame(),
        "by_marketplace": pd.DataFrame(),
        "alerts": pd.DataFrame(),
    }
    if economics_intel_df.empty:
        return empty

    working = economics_intel_df.copy()
    for col in [
        "sold_price",
        "estimated_fee_alloc",
        "expected_shipping_alloc",
        "estimated_net_before_cogs",
        "actual_fee_alloc",
        "actual_shipping_alloc",
        "actual_net_before_cogs",
        "fee_variance_actual_minus_estimated",
        "net_variance_actual_minus_estimated",
    ]:
        if col not in working.columns:
            working[col] = 0.0
        working[col] = pd.to_numeric(working[col], errors="coerce").fillna(0.0)

    if "estimate_available" not in working.columns:
        working["estimate_available"] = False
    working["estimate_available"] = working["estimate_available"].astype(bool)
    working["estimate_available_count"] = working["estimate_available"].astype(int)
    working["abs_fee_variance"] = working["fee_variance_actual_minus_estimated"].abs()
    working["abs_net_variance"] = working["net_variance_actual_minus_estimated"].abs()
    gross = working["sold_price"].replace(0, pd.NA)
    working["actual_margin_pct_of_gross"] = ((working["actual_net_before_cogs"] / gross) * 100.0).fillna(0.0)

    def _aggregate(group_cols: list[str]) -> pd.DataFrame:
        grouped = (
            working.groupby(group_cols, dropna=False, as_index=False)
            .agg(
                sales_count=("sale_id", "count"),
                estimate_covered_sales=("estimate_available_count", "sum"),
                gross_sales=("sold_price", "sum"),
                estimated_fee_total=("estimated_fee_alloc", "sum"),
                actual_fee_total=("actual_fee_alloc", "sum"),
                fee_variance_total=("fee_variance_actual_minus_estimated", "sum"),
                avg_abs_fee_variance=("abs_fee_variance", "mean"),
                expected_net_total=("estimated_net_before_cogs", "sum"),
                actual_net_total=("actual_net_before_cogs", "sum"),
                net_variance_total=("net_variance_actual_minus_estimated", "sum"),
                avg_abs_net_variance=("abs_net_variance", "mean"),
            )
            .sort_values(["sales_count"], ascending=[False])
        )
        gross_group = grouped["gross_sales"].replace(0, pd.NA)
        grouped["expected_margin_pct_of_gross"] = ((grouped["expected_net_total"] / gross_group) * 100.0).fillna(0.0)
        grouped["actual_margin_pct_of_gross"] = ((grouped["actual_net_total"] / gross_group) * 100.0).fillna(0.0)
        grouped["estimate_coverage_pct"] = (
            (grouped["estimate_covered_sales"] / grouped["sales_count"].replace(0, pd.NA)) * 100.0
        ).fillna(0.0)
        min_samples = max(1, int(min_group_sales_for_alert or 1))
        margin_floor = float(min_margin_alert_pct or 0.0)
        fee_threshold = max(0.0, float(max_fee_variance_alert_usd or 0.0))
        grouped["alert_margin_below_floor"] = (
            (grouped["sales_count"] >= min_samples) & (grouped["actual_margin_pct_of_gross"] < margin_floor)
        )
        grouped["alert_fee_variance_high"] = (
            (grouped["estimate_covered_sales"] >= min_samples) & (grouped["avg_abs_fee_variance"] > fee_threshold)
        )
        grouped["alert_any"] = grouped["alert_margin_below_floor"] | grouped["alert_fee_variance_high"]
        return grouped.sort_values(["alert_any", "sales_count"], ascending=[False, False])

    by_sku_df = _aggregate(["sku", "product_title"])
    by_marketplace_df = _aggregate(["marketplace"])

    alerts_df = working.copy()
    margin_floor = float(min_margin_alert_pct or 0.0)
    fee_threshold = max(0.0, float(max_fee_variance_alert_usd or 0.0))
    alerts_df["alert_margin_below_floor"] = alerts_df["actual_margin_pct_of_gross"] < margin_floor
    alerts_df["alert_fee_variance_high"] = (
        alerts_df["estimate_available"] & (alerts_df["abs_fee_variance"] > fee_threshold)
    )
    alerts_df["alert_any"] = alerts_df["alert_margin_below_floor"] | alerts_df["alert_fee_variance_high"]
    alerts_df = alerts_df[alerts_df["alert_any"]].copy()
    if not alerts_df.empty:
        alerts_df = alerts_df.sort_values(["sold_at", "abs_net_variance"], ascending=[False, False])
        keep_cols = [
            "sold_at",
            "sale_id",
            "marketplace",
            "sku",
            "product_title",
            "sold_price",
            "estimated_net_before_cogs",
            "actual_net_before_cogs",
            "net_variance_actual_minus_estimated",
            "fee_variance_actual_minus_estimated",
            "actual_margin_pct_of_gross",
            "alert_margin_below_floor",
            "alert_fee_variance_high",
            "alert_any",
        ]
        alerts_df = alerts_df[[c for c in keep_cols if c in alerts_df.columns]]

    return {
        "by_sku": by_sku_df,
        "by_marketplace": by_marketplace_df,
        "alerts": alerts_df,
    }


def _summarize_fee_reconciliation(df: pd.DataFrame) -> dict[str, float | int]:
    if df.empty:
        return {
            "sales_count": 0,
            "total_actual_fee": 0.0,
            "total_estimated_fee": 0.0,
            "total_variance": 0.0,
            "estimate_coverage_count": 0,
        }
    return {
        "sales_count": int(len(df)),
        "total_actual_fee": float(df["actual_fee"].sum()),
        "total_estimated_fee": float(df["estimated_fee_scaled"].sum()),
        "total_variance": float(df["variance_actual_minus_estimate"].sum()),
        "estimate_coverage_count": int(df["fee_estimate_present"].astype(bool).sum()),
    }


def _fee_source_priority_counts(df: pd.DataFrame) -> dict[str, int]:
    if df.empty:
        return {
            "normalized_source_rows": 0,
            "notes_fallback_rows": 0,
            "sale_field_fallback_rows": 0,
        }
    count_map = {
        str(row.actual_fee_source or "").strip(): int(row.sales_count or 0)
        for row in df.itertuples(index=False)
    }
    return {
        "normalized_source_rows": int(
            count_map.get("normalized_order_finance_entries_marketplace_fee_sum", 0)
        ),
        "notes_fallback_rows": int(
            count_map.get("order_fee_breakdown_total_marketplace_fee", 0)
        ),
        "sale_field_fallback_rows": int(count_map.get("sale_fees_field", 0)),
    }


def _top_n_records(
    df: pd.DataFrame,
    *,
    sort_by: str,
    n: int = 10,
    ascending: bool = False,
    columns: list[str] | None = None,
) -> list[dict]:
    if df.empty or sort_by not in df.columns:
        return []
    slice_df = df
    if columns:
        keep = [c for c in columns if c in df.columns]
        if keep:
            slice_df = df[keep]
    top_df = (
        slice_df.nsmallest(int(n), sort_by)
        if ascending
        else slice_df.nlargest(int(n), sort_by)
    )
    return top_df.to_dict("records")


def _top_n_by_abs_records(
    df: pd.DataFrame,
    *,
    value_col: str,
    n: int = 10,
    drop_cols: list[str] | None = None,
) -> list[dict]:
    if df.empty or value_col not in df.columns:
        return []
    top_idx = df[value_col].abs().nlargest(int(n)).index
    top_df = df.loc[top_idx]
    if drop_cols:
        drop_existing = [c for c in drop_cols if c in top_df.columns]
        if drop_existing:
            top_df = top_df.drop(columns=drop_existing)
    return top_df.to_dict("records")


def _default_tax_marketplace_scope(
    sales_marketplace_options: list[str],
    facilitator_channels: set[str],
) -> list[str]:
    options = [str(v or "").strip().lower() for v in (sales_marketplace_options or []) if str(v or "").strip()]
    unique_options: list[str] = []
    seen: set[str] = set()
    for value in options:
        if value in seen:
            continue
        seen.add(value)
        unique_options.append(value)
    filtered = [m for m in unique_options if m not in set(facilitator_channels or set())]
    return filtered if filtered else unique_options


def _bounded_dataframe(
    df: pd.DataFrame,
    *,
    render_full_tables: bool,
    preview_row_limit: int,
) -> tuple[pd.DataFrame, bool]:
    if render_full_tables:
        return df, False
    limit = max(1, int(preview_row_limit or 1))
    return df.head(limit), int(len(df)) > limit


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


def _parse_marketplace_payload_json(raw_payload: str | None) -> dict:
    raw = str(raw_payload or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_iso_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _sale_from_row(row: dict):
    product_id = row.get("product_id")
    product_obj = None
    if product_id is not None:
        product_obj = SimpleNamespace(
            id=int(product_id),
            sku=str(row.get("sku") or "").strip() or None,
            title=str(row.get("product_title") or "").strip() or None,
            acquisition_cost=row.get("product_acquisition_cost"),
        )
    return SimpleNamespace(
        id=int(row.get("sale_id") or 0),
        sold_at=_parse_iso_datetime(row.get("sold_at")),
        marketplace=str(row.get("marketplace") or "").strip(),
        order_id=row.get("order_id"),
        product=product_obj,
        listing_id=row.get("listing_id"),
        external_order_id=str(row.get("external_order_id") or "").strip(),
        quantity_sold=int(row.get("quantity_sold") or 0),
        sold_price=_safe_float(row.get("sold_price")),
        fees=_safe_float(row.get("fees")),
        shipping_cost=_safe_float(row.get("shipping_cost")),
        shipping_provider=str(row.get("shipping_provider") or "").strip(),
        shipping_service=str(row.get("shipping_service") or "").strip(),
        shipping_package_type=str(row.get("shipping_package_type") or "").strip(),
        tracking_number=str(row.get("tracking_number") or "").strip(),
        tracking_status=str(row.get("tracking_status") or "").strip(),
        shipping_exception_code=str(row.get("shipping_exception_code") or "").strip(),
        shipping_exception_action=str(row.get("shipping_exception_action") or "").strip(),
        shipping_exception_notes=str(row.get("shipping_exception_notes") or "").strip(),
        shipping_exception_resolved_at=_parse_iso_datetime(row.get("shipping_exception_resolved_at")),
        shipping_exception_resolved_by=str(row.get("shipping_exception_resolved_by") or "").strip(),
        shipment_exported_at=_parse_iso_datetime(row.get("shipment_exported_at")),
        shipped_at=_parse_iso_datetime(row.get("shipped_at")),
        delivered_at=_parse_iso_datetime(row.get("delivered_at")),
        shipping_label_cost=row.get("shipping_label_cost"),
    )


def _order_from_row(row: dict):
    return SimpleNamespace(
        id=int(row.get("order_id") or 0),
        sold_at=_parse_iso_datetime(row.get("sold_at")),
        marketplace=str(row.get("marketplace") or "").strip(),
        external_order_id=str(row.get("external_order_id") or "").strip(),
        order_status=str(row.get("status") or "").strip(),
        subtotal_amount=_safe_float(row.get("subtotal_amount")),
        fees=_safe_float(row.get("fees")),
        shipping_cost=_safe_float(row.get("shipping_cost")),
        shipping_label_cost=row.get("shipping_label_cost"),
        total_amount=_safe_float(row.get("total_amount")),
        notes=str(row.get("notes") or "").strip(),
        items=[None] * int(row.get("item_count") or 0),
    )


def _product_from_row(row: dict):
    return SimpleNamespace(
        id=int(row.get("product_id") or 0),
        sku=str(row.get("sku") or "").strip(),
        title=str(row.get("title") or "").strip(),
        description=str(row.get("description") or "").strip(),
        category=str(row.get("category") or "").strip(),
        metal_type=str(row.get("metal_type") or "").strip(),
        current_quantity=int(row.get("current_quantity") or 0),
        acquisition_cost=row.get("acquisition_cost"),
        acquired_at=_parse_iso_datetime(row.get("acquired_at")),
        acquisition_tax_paid=row.get("acquisition_tax_paid"),
        acquisition_shipping_paid=row.get("acquisition_shipping_paid"),
        acquisition_handling_paid=row.get("acquisition_handling_paid"),
        weight_oz=row.get("weight_oz"),
        package_weight_oz=row.get("package_weight_oz"),
        package_length_in=row.get("package_length_in"),
        package_width_in=row.get("package_width_in"),
        package_height_in=row.get("package_height_in"),
    )


def _listing_from_row(row: dict):
    product_obj = None
    product_id = row.get("product_id")
    sku = str(row.get("sku") or "").strip()
    if product_id is not None or sku:
        product_obj = SimpleNamespace(
            id=int(product_id or 0) if product_id is not None else None,
            sku=sku or None,
        )
    return SimpleNamespace(
        id=int(row.get("listing_id") or 0),
        listed_at=_parse_iso_datetime(row.get("listed_at")),
        marketplace=str(row.get("marketplace") or "").strip(),
        product=product_obj,
        listing_title=str(row.get("listing_title") or "").strip(),
        listing_status=str(row.get("listing_status") or "").strip(),
        marketplace_url=str(row.get("marketplace_url") or "").strip(),
        marketplace_details=str(row.get("marketplace_details") or "").strip(),
        quantity_listed=int(row.get("quantity_listed") or 0),
        listing_price=_safe_float(row.get("listing_price")),
        external_listing_id=str(row.get("external_listing_id") or "").strip(),
    )


def _build_ebay_marketplace_fee_rows(repo: InventoryRepository, orders) -> list[dict]:
    rows: list[dict] = []
    order_lookup: dict[int, dict] = {}
    for order in orders:
        if str(getattr(order, "marketplace", "") or "").strip().lower() != "ebay":
            continue
        order_lookup[int(getattr(order, "id", 0) or 0)] = {
            "external_order_id": str(getattr(order, "external_order_id", "") or "").strip(),
            "sold_at": iso_or_none(getattr(order, "sold_at", None)),
        }

    order_ids = [oid for oid in order_lookup.keys() if oid > 0]
    if order_ids:
        normalized_entries = repo.db.scalars(
            select(OrderFinanceEntry).where(
                OrderFinanceEntry.order_id.in_(order_ids),
                OrderFinanceEntry.entry_kind == "marketplace_fee",
            )
        ).all()
        for entry in normalized_entries:
            order_meta = order_lookup.get(int(getattr(entry, "order_id", 0) or 0), {})
            rows.append(
                {
                    "order_id": int(getattr(entry, "order_id", 0) or 0),
                    "sold_at": order_meta.get("sold_at") or "",
                    "external_order_id": order_meta.get("external_order_id") or "",
                    "line_item_id": str(getattr(entry, "line_item_id", "") or "").strip(),
                    "sku": str(getattr(entry, "sku", "") or "").strip(),
                    "product_title": "",
                    "legacy_item_id": str(getattr(entry, "legacy_item_id", "") or "").strip(),
                    "fee_type": str(getattr(entry, "fee_type", "") or "").strip(),
                    "fee_amount": _safe_float(getattr(entry, "amount", 0)),
                    "fee_currency": str(getattr(entry, "currency", "") or "").strip(),
                    "fee_memo": str(getattr(entry, "memo", "") or "").strip(),
                    "transaction_id": str(getattr(entry, "transaction_id", "") or "").strip(),
                    "transaction_date": iso_or_none(getattr(entry, "transaction_date", None)) or "",
                    "transaction_type": str(getattr(entry, "transaction_type", "") or "").strip(),
                    "transaction_status": str(getattr(entry, "transaction_status", "") or "").strip(),
                    "source": str(getattr(entry, "source", "") or "").strip() or "normalized_order_finance_entries",
                }
            )
        if rows:
            return rows

    for order in orders:
        if str(getattr(order, "marketplace", "") or "").strip().lower() != "ebay":
            continue
        order_id = int(getattr(order, "id", 0) or 0)
        external_order_id = str(getattr(order, "external_order_id", "") or "").strip()
        sold_at = iso_or_none(getattr(order, "sold_at", None))
        payload = _parse_marketplace_payload_json(getattr(order, "marketplace_payload_json", None))
        if not payload:
            continue

        order_line_lookup: dict[str, dict] = {}
        for line in payload.get("lineItems") or []:
            if not isinstance(line, dict):
                continue
            line_id = str(line.get("lineItemId") or "").strip()
            if not line_id:
                continue
            order_line_lookup[line_id] = {
                "sku": str(line.get("sku") or "").strip(),
                "title": str(line.get("title") or "").strip(),
                "legacy_item_id": str(line.get("legacyItemId") or "").strip(),
            }

        for line in payload.get("lineItems") or []:
            if not isinstance(line, dict):
                continue
            line_id = str(line.get("lineItemId") or "").strip()
            fee_list = line.get("marketplaceFees") or []
            if not isinstance(fee_list, list):
                continue
            for fee in fee_list:
                if not isinstance(fee, dict):
                    continue
                amount = fee.get("amount") or {}
                rows.append(
                    {
                        "order_id": order_id,
                        "sold_at": sold_at,
                        "external_order_id": external_order_id,
                        "line_item_id": line_id,
                        "sku": str(line.get("sku") or "").strip(),
                        "product_title": str(line.get("title") or "").strip(),
                        "legacy_item_id": str(line.get("legacyItemId") or "").strip(),
                        "fee_type": str(fee.get("feeType") or "").strip(),
                        "fee_amount": _safe_float(amount.get("value")),
                        "fee_currency": str(amount.get("currency") or "").strip(),
                        "fee_memo": str(fee.get("feeMemo") or "").strip(),
                        "transaction_id": "",
                        "transaction_date": "",
                        "transaction_type": "",
                        "transaction_status": "",
                        "source": "order_payload_lineItems",
                    }
                )

        for tx in payload.get("_finance_transactions") or []:
            if not isinstance(tx, dict):
                continue
            tx_id = str(tx.get("transactionId") or "").strip()
            tx_date = str(tx.get("transactionDate") or "").strip()
            tx_type = str(tx.get("transactionType") or "").strip()
            tx_status = str(tx.get("transactionStatus") or "").strip()
            tx_fee_memo = str(tx.get("transactionMemo") or "").strip()
            tx_line_items = tx.get("orderLineItems") or []
            if not isinstance(tx_line_items, list):
                continue
            for tx_line in tx_line_items:
                if not isinstance(tx_line, dict):
                    continue
                line_id = str(tx_line.get("lineItemId") or "").strip()
                line_meta = order_line_lookup.get(line_id) or {}
                fee_list = tx_line.get("marketplaceFees") or []
                if not isinstance(fee_list, list):
                    continue
                for fee in fee_list:
                    if not isinstance(fee, dict):
                        continue
                    amount = fee.get("amount") or {}
                    rows.append(
                        {
                            "order_id": order_id,
                            "sold_at": sold_at,
                            "external_order_id": external_order_id,
                            "line_item_id": line_id,
                            "sku": str(line_meta.get("sku") or "").strip(),
                            "product_title": str(line_meta.get("title") or "").strip(),
                            "legacy_item_id": str(line_meta.get("legacy_item_id") or "").strip(),
                            "fee_type": str(fee.get("feeType") or "").strip(),
                            "fee_amount": _safe_float(amount.get("value")),
                            "fee_currency": str(amount.get("currency") or "").strip(),
                            "fee_memo": str(fee.get("feeMemo") or tx_fee_memo).strip(),
                            "transaction_id": tx_id,
                            "transaction_date": tx_date,
                            "transaction_type": tx_type,
                            "transaction_status": tx_status,
                            "source": "finance_transactions_orderLineItems",
                        }
                    )
    return rows


@st.cache_data(show_spinner=False, max_entries=64)
def _build_fee_source_priority_summary(reconciliation_df: pd.DataFrame) -> pd.DataFrame:
    if reconciliation_df is None or reconciliation_df.empty or "actual_fee_source" not in reconciliation_df.columns:
        return pd.DataFrame()
    source_priority = {
        "normalized_order_finance_entries_marketplace_fee_sum": 1,
        "order_fee_breakdown_total_marketplace_fee": 2,
        "sale_fees_field": 3,
    }
    grouped = (
        reconciliation_df.groupby(["actual_fee_source"], dropna=False, as_index=False)
        .agg(
            sales_count=("sale_id", "count"),
            actual_fee_total=("actual_fee", "sum"),
        )
    )
    total_rows = float(len(reconciliation_df))
    grouped["coverage_pct"] = grouped["sales_count"].map(
        lambda n: round((float(n or 0) / total_rows) * 100.0, 2) if total_rows > 0 else 0.0
    )
    grouped["priority_rank"] = grouped["actual_fee_source"].map(
        lambda s: source_priority.get(str(s or "").strip().lower(), 99)
    )
    grouped = grouped.sort_values(["priority_rank", "sales_count"], ascending=[True, False]).reset_index(drop=True)
    return grouped


@st.cache_data(show_spinner=False, max_entries=64)
def _build_fee_source_priority_trend(reconciliation_df: pd.DataFrame) -> pd.DataFrame:
    if (
        reconciliation_df is None
        or reconciliation_df.empty
        or "actual_fee_source" not in reconciliation_df.columns
        or "sold_at" not in reconciliation_df.columns
    ):
        return pd.DataFrame()
    base = reconciliation_df.copy()
    base["sold_at_dt"] = pd.to_datetime(base["sold_at"], errors="coerce")
    base = base[base["sold_at_dt"].notna()].copy()
    if base.empty:
        return pd.DataFrame()

    source_priority = {
        "normalized_order_finance_entries_marketplace_fee_sum": 1,
        "order_fee_breakdown_total_marketplace_fee": 2,
        "sale_fees_field": 3,
    }
    base["source_rank"] = base["actual_fee_source"].map(
        lambda s: source_priority.get(str(s or "").strip().lower(), 99)
    )

    daily = (
        base.assign(
            bucket_granularity="daily",
            bucket_date=base["sold_at_dt"].dt.strftime("%Y-%m-%d"),
        )
        .groupby(
            ["bucket_granularity", "bucket_date", "actual_fee_source", "source_rank"],
            dropna=False,
            as_index=False,
        )
        .agg(
            sales_count=("sale_id", "count"),
            actual_fee_total=("actual_fee", "sum"),
        )
    )
    weekly = (
        base.assign(
            bucket_granularity="weekly",
            bucket_date=base["sold_at_dt"].dt.to_period("W-MON").map(lambda p: str(p.start_time.date())),
        )
        .groupby(
            ["bucket_granularity", "bucket_date", "actual_fee_source", "source_rank"],
            dropna=False,
            as_index=False,
        )
        .agg(
            sales_count=("sale_id", "count"),
            actual_fee_total=("actual_fee", "sum"),
        )
    )
    out = pd.concat([daily, weekly], ignore_index=True)
    out = out.sort_values(
        ["bucket_granularity", "bucket_date", "source_rank", "sales_count"],
        ascending=[True, True, True, False],
    ).reset_index(drop=True)
    return out


@st.cache_data(show_spinner=False, max_entries=64)
def _build_normalized_source_weekly_coverage(trend_df: pd.DataFrame) -> pd.DataFrame:
    if (
        trend_df is None
        or trend_df.empty
        or "bucket_granularity" not in trend_df.columns
        or "bucket_date" not in trend_df.columns
        or "actual_fee_source" not in trend_df.columns
        or "sales_count" not in trend_df.columns
    ):
        return pd.DataFrame()
    weekly = trend_df[trend_df["bucket_granularity"] == "weekly"].copy()
    if weekly.empty:
        return pd.DataFrame()
    total_by_week = (
        weekly.groupby(["bucket_date"], dropna=False, as_index=False)
        .agg(total_sales_count=("sales_count", "sum"))
    )
    normalized_by_week = (
        weekly[weekly["actual_fee_source"] == "normalized_order_finance_entries_marketplace_fee_sum"]
        .groupby(["bucket_date"], dropna=False, as_index=False)
        .agg(normalized_sales_count=("sales_count", "sum"))
    )
    merged = total_by_week.merge(normalized_by_week, on="bucket_date", how="left")
    merged["normalized_sales_count"] = merged["normalized_sales_count"].fillna(0).astype(float)
    merged["total_sales_count"] = merged["total_sales_count"].fillna(0).astype(float)
    merged["normalized_coverage_pct"] = merged.apply(
        lambda r: round((float(r["normalized_sales_count"]) / float(r["total_sales_count"]) * 100.0), 2)
        if float(r["total_sales_count"] or 0) > 0
        else 0.0,
        axis=1,
    )
    return merged.sort_values(["bucket_date"], ascending=[True]).reset_index(drop=True)


@st.cache_data(show_spinner=False, max_entries=64)
def _build_weekly_fee_source_count_chart_data(trend_df: pd.DataFrame) -> pd.DataFrame:
    if (
        trend_df is None
        or trend_df.empty
        or "bucket_granularity" not in trend_df.columns
        or "bucket_date" not in trend_df.columns
        or "actual_fee_source" not in trend_df.columns
        or "sales_count" not in trend_df.columns
    ):
        return pd.DataFrame()
    weekly = trend_df[trend_df["bucket_granularity"] == "weekly"].copy()
    if weekly.empty:
        return pd.DataFrame()
    source_order = [
        "normalized_order_finance_entries_marketplace_fee_sum",
        "order_fee_breakdown_total_marketplace_fee",
        "sale_fees_field",
    ]
    pivot = (
        weekly.pivot_table(
            index="bucket_date",
            columns="actual_fee_source",
            values="sales_count",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )
    for col in source_order:
        if col not in pivot.columns:
            pivot[col] = 0
    keep_cols = ["bucket_date"] + source_order
    pivot = pivot[keep_cols].copy()
    pivot = pivot.rename(
        columns={
            "bucket_date": "week_start",
            "normalized_order_finance_entries_marketplace_fee_sum": "normalized_source",
            "order_fee_breakdown_total_marketplace_fee": "notes_fallback",
            "sale_fees_field": "sale_field_fallback",
        }
    )
    return pivot.sort_values(["week_start"], ascending=[True]).reset_index(drop=True)


@st.cache_data(show_spinner=False, max_entries=128)
def _filter_tax_drilldown_rows(
    tax_detail_df: pd.DataFrame,
    *,
    marketplace: str,
    taxability: str,
) -> pd.DataFrame:
    if tax_detail_df is None or tax_detail_df.empty:
        return pd.DataFrame()
    filtered = tax_detail_df.copy()
    marketplace_norm = str(marketplace or "all").strip().lower()
    taxability_norm = str(taxability or "all").strip().lower()
    if marketplace_norm != "all":
        filtered = filtered[
            filtered["marketplace"].astype(str).str.strip().str.lower() == marketplace_norm
        ]
    if taxability_norm == "taxable_only":
        filtered = filtered[filtered["taxable_subtotal"].astype(float) > 0.0]
    elif taxability_norm == "exempt_only":
        filtered = filtered[filtered["is_tax_exempt_category"].astype(bool)]
    return filtered


@st.cache_data(show_spinner=False, max_entries=128)
def _build_tax_drilldown_sale_option_rows(filtered_tax_detail: pd.DataFrame) -> list[dict]:
    if filtered_tax_detail is None or filtered_tax_detail.empty:
        return []
    rows: list[dict] = []
    for row in filtered_tax_detail.itertuples(index=False):
        sale_id = int(getattr(row, "sale_id", 0) or 0)
        if sale_id <= 0:
            continue
        label = (
            f"sale#{sale_id} | {str(getattr(row, 'marketplace', '') or '').strip()} | "
            f"{str(getattr(row, 'sku', '') or '').strip()} | "
            f"tax={float(getattr(row, 'estimated_tax_collected', 0.0) or 0.0):,.2f}"
        )
        rows.append({"label": label, "sale_id": sale_id})
    return rows


@st.cache_data(show_spinner=False, max_entries=128)
def _tax_drilldown_kpis(filtered_tax_detail: pd.DataFrame) -> dict[str, float | int]:
    if filtered_tax_detail is None or filtered_tax_detail.empty:
        return {
            "rows": 0,
            "taxable_subtotal": 0.0,
            "estimated_tax": 0.0,
        }
    return {
        "rows": int(len(filtered_tax_detail)),
        "taxable_subtotal": float(filtered_tax_detail["taxable_subtotal"].sum()),
        "estimated_tax": float(filtered_tax_detail["estimated_tax_collected"].sum()),
    }


@st.cache_data(show_spinner=False, max_entries=128)
def _build_documents_handoff_sale_option_rows(sales_df: pd.DataFrame) -> list[dict]:
    if sales_df is None or sales_df.empty:
        return []
    rows: list[dict] = []
    for row in sales_df.itertuples(index=False):
        sale_id = int(getattr(row, "sale_id", 0) or 0)
        if sale_id <= 0:
            continue
        label = (
            f"sale#{sale_id} | {str(getattr(row, 'sold_at', '') or '')} | "
            f"{str(getattr(row, 'marketplace', '') or '').strip()} | "
            f"{str(getattr(row, 'sku', '') or '').strip()} | "
            f"gross=${float(getattr(row, 'gross_sales', 0.0) or 0.0):,.2f}"
        )
        rows.append({"label": label, "source_id": sale_id})
    return rows


@st.cache_data(show_spinner=False, max_entries=128)
def _build_documents_handoff_order_option_rows(orders_df: pd.DataFrame) -> list[dict]:
    if orders_df is None or orders_df.empty:
        return []
    rows: list[dict] = []
    for row in orders_df.itertuples(index=False):
        order_id = int(getattr(row, "order_id", 0) or 0)
        if order_id <= 0:
            continue
        label = (
            f"order#{order_id} | {str(getattr(row, 'sold_at', '') or '')} | "
            f"{str(getattr(row, 'marketplace', '') or '').strip()} | "
            f"ext={str(getattr(row, 'external_order_id', '') or '').strip()} | "
            f"total=${float(getattr(row, 'total_amount', 0.0) or 0.0):,.2f}"
        )
        rows.append({"label": label, "source_id": order_id})
    return rows


def _tax_report_presets(
    *,
    default_jurisdiction: str,
    default_tax_rate_percent: float,
    default_shipping_taxable: bool,
) -> dict[str, dict]:
    return {
        "Golden Local Retail": {
            "jurisdiction": default_jurisdiction or "Golden, Colorado",
            "tax_rate_percent": float(default_tax_rate_percent),
            "shipping_taxable": bool(default_shipping_taxable),
            "marketplace_mode": "local_only",
        },
        "Marketplace Shipped": {
            "jurisdiction": default_jurisdiction or "Golden, Colorado",
            "tax_rate_percent": float(default_tax_rate_percent),
            "shipping_taxable": False,
            "marketplace_mode": "all",
        },
        "Bullion Exempt Focus": {
            "jurisdiction": default_jurisdiction or "Golden, Colorado",
            "tax_rate_percent": float(default_tax_rate_percent),
            "shipping_taxable": False,
            "marketplace_mode": "all",
        },
    }


def _build_fifo_unit_cost_map(
    all_sales,
    all_assignments,
    default_unit_cost_by_product: dict[int, float],
) -> dict[int, float]:
    lots_by_product: dict[int, list[dict]] = defaultdict(list)
    for a in sorted(all_assignments, key=lambda x: (x.acquired_at or datetime.min, x.id)):
        if a.product_id is None:
            continue
        unit_cost = _safe_float(a.unit_cost)
        if unit_cost <= 0 and a.allocated_cost is not None and a.quantity_acquired:
            unit_cost = _safe_float(a.allocated_cost) / max(1, int(a.quantity_acquired))
        lots_by_product[int(a.product_id)].append(
            {
                "remaining_qty": max(0, int(a.quantity_acquired or 0)),
                "unit_cost": unit_cost,
            }
        )

    queues: dict[int, deque] = {
        product_id: deque(lots) for product_id, lots in lots_by_product.items()
    }
    fifo_unit_cost_by_sale: dict[int, float] = {}
    sales_sorted = sorted(all_sales, key=lambda s: (s.sold_at or datetime.min, s.id))
    for sale in sales_sorted:
        product_id = int(sale.product_id) if sale.product_id is not None else None
        qty = max(1, int(sale.quantity_sold or 1))
        if product_id is None:
            fifo_unit_cost_by_sale[sale.id] = 0.0
            continue

        queue = queues.get(product_id)
        if queue is None:
            queue = deque()
            queues[product_id] = queue
        default_cost = max(0.0, _safe_float(default_unit_cost_by_product.get(product_id)))

        qty_remaining = qty
        total_cost = 0.0
        while qty_remaining > 0:
            if queue and int(queue[0]["remaining_qty"]) > 0:
                use_qty = min(qty_remaining, int(queue[0]["remaining_qty"]))
                total_cost += float(use_qty) * _safe_float(queue[0]["unit_cost"])
                queue[0]["remaining_qty"] = int(queue[0]["remaining_qty"]) - use_qty
                qty_remaining -= use_qty
                if int(queue[0]["remaining_qty"]) <= 0:
                    queue.popleft()
            else:
                total_cost += float(qty_remaining) * default_cost
                qty_remaining = 0

        fifo_unit_cost_by_sale[sale.id] = (total_cost / float(qty)) if qty > 0 else 0.0
    return fifo_unit_cost_by_sale


def _build_lot_weighted_unit_cost_map(
    all_assignments,
    default_unit_cost_by_product: dict[int, float],
) -> dict[int, float]:
    totals: dict[int, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
    for a in all_assignments:
        if a.product_id is None:
            continue
        pid = int(a.product_id)
        qty = float(max(0, int(a.quantity_acquired or 0)))
        if qty <= 0:
            continue
        unit_cost = _safe_float(a.unit_cost)
        if unit_cost <= 0 and a.allocated_cost is not None:
            unit_cost = _safe_float(a.allocated_cost) / qty
        totals[pid]["qty"] += qty
        totals[pid]["cost"] += unit_cost * qty

    result: dict[int, float] = {}
    for pid, agg in totals.items():
        if agg["qty"] > 0:
            result[pid] = agg["cost"] / agg["qty"]
    for pid, default_cost in default_unit_cost_by_product.items():
        result.setdefault(pid, max(0.0, _safe_float(default_cost)))
    return result


def _build_inventory_cycle_rows(
    products,
    movements,
    sales,
) -> list[dict]:
    product_by_id = {int(p.id): p for p in products if p is not None and p.id is not None}
    movements_by_product: dict[int, list] = defaultdict(list)
    for m in movements:
        if m.product_id is None:
            continue
        movements_by_product[int(m.product_id)].append(m)
    sales_by_product: dict[int, list] = defaultdict(list)
    for s in sales:
        if s.product_id is None:
            continue
        sales_by_product[int(s.product_id)].append(s)

    rows: list[dict] = []
    for product_id, product_movements in movements_by_product.items():
        product = product_by_id.get(product_id)
        product_sales = sorted(
            sales_by_product.get(product_id, []),
            key=lambda x: (x.sold_at or datetime.min, x.id),
        )
        sales_idx = 0
        sorted_movements = sorted(product_movements, key=lambda x: (x.occurred_at or datetime.min, x.id))
        current_cycle: dict | None = None
        cycle_number = 0

        for mv in sorted_movements:
            before_qty = int(mv.quantity_before or 0)
            after_qty = int(mv.quantity_after or 0)
            qty_delta = int(mv.quantity_delta or 0)
            started_new_cycle = current_cycle is None and after_qty > 0
            if started_new_cycle:
                cycle_number += 1
                current_cycle = {
                    "product_id": product_id,
                    "sku": product.sku if product else None,
                    "product_title": product.title if product else None,
                    "cycle_number": cycle_number,
                    "cycle_id": f"{product.sku or product_id}-C{cycle_number}",
                    "cycle_start": mv.occurred_at,
                    "cycle_end": None,
                    "cycle_status": "open",
                    "start_qty_before": before_qty,
                    "end_qty_after": after_qty,
                    "qty_in": 0,
                    "qty_out_movements": 0,
                    "acquisition_cost_known": 0.0,
                    "movement_count": 0,
                    "sale_count": 0,
                    "qty_sold_sales": 0,
                    "gross_sales": 0.0,
                    "fees": 0.0,
                    "shipping_cost": 0.0,
                    "net_sales": 0.0,
                }
            if current_cycle is None:
                continue

            current_cycle["movement_count"] += 1
            current_cycle["end_qty_after"] = after_qty
            if qty_delta > 0:
                current_cycle["qty_in"] += qty_delta
                if mv.unit_cost is not None:
                    current_cycle["acquisition_cost_known"] += _safe_float(mv.unit_cost) * float(qty_delta)
            elif qty_delta < 0:
                current_cycle["qty_out_movements"] += abs(qty_delta)

            cycle_start = current_cycle["cycle_start"] or datetime.min
            cycle_end_candidate = mv.occurred_at or datetime.min
            while sales_idx < len(product_sales):
                sale = product_sales[sales_idx]
                sold_at = sale.sold_at or datetime.min
                if sold_at < cycle_start:
                    sales_idx += 1
                    continue
                if sold_at > cycle_end_candidate:
                    break
                current_cycle["sale_count"] += 1
                current_cycle["qty_sold_sales"] += int(sale.quantity_sold or 0)
                current_cycle["gross_sales"] += _safe_float(sale.sold_price)
                current_cycle["fees"] += _safe_float(sale.fees)
                current_cycle["shipping_cost"] += _safe_float(sale.shipping_cost)
                current_cycle["net_sales"] += (
                    _safe_float(sale.sold_price)
                    - _safe_float(sale.fees)
                    - _safe_float(sale.shipping_cost)
                )
                sales_idx += 1

            if after_qty <= 0:
                current_cycle["cycle_end"] = mv.occurred_at
                current_cycle["cycle_status"] = "closed"
                known_cost = _safe_float(current_cycle["acquisition_cost_known"])
                current_cycle["estimated_margin_vs_known_cost"] = (
                    _safe_float(current_cycle["net_sales"]) - known_cost
                )
                rows.append(current_cycle)
                current_cycle = None

        if current_cycle is not None:
            while sales_idx < len(product_sales):
                sale = product_sales[sales_idx]
                sold_at = sale.sold_at or datetime.min
                if sold_at < (current_cycle["cycle_start"] or datetime.min):
                    sales_idx += 1
                    continue
                current_cycle["sale_count"] += 1
                current_cycle["qty_sold_sales"] += int(sale.quantity_sold or 0)
                current_cycle["gross_sales"] += _safe_float(sale.sold_price)
                current_cycle["fees"] += _safe_float(sale.fees)
                current_cycle["shipping_cost"] += _safe_float(sale.shipping_cost)
                current_cycle["net_sales"] += (
                    _safe_float(sale.sold_price)
                    - _safe_float(sale.fees)
                    - _safe_float(sale.shipping_cost)
                )
                sales_idx += 1
            current_cycle["cycle_status"] = "open"
            known_cost = _safe_float(current_cycle["acquisition_cost_known"])
            current_cycle["estimated_margin_vs_known_cost"] = (
                _safe_float(current_cycle["net_sales"]) - known_cost
            )
            rows.append(current_cycle)

    output = []
    for row in rows:
        output.append(
            {
                "product_id": row["product_id"],
                "sku": row["sku"],
                "product_title": row["product_title"],
                "cycle_number": row["cycle_number"],
                "cycle_id": row["cycle_id"],
                "cycle_status": row["cycle_status"],
                "cycle_start": iso_or_none(row["cycle_start"]),
                "cycle_end": iso_or_none(row["cycle_end"]),
                "start_qty_before": int(row["start_qty_before"]),
                "end_qty_after": int(row["end_qty_after"]),
                "qty_in": int(row["qty_in"]),
                "qty_out_movements": int(row["qty_out_movements"]),
                "qty_sold_sales": int(row["qty_sold_sales"]),
                "movement_count": int(row["movement_count"]),
                "sale_count": int(row["sale_count"]),
                "acquisition_cost_known": round(_safe_float(row["acquisition_cost_known"]), 2),
                "gross_sales": round(_safe_float(row["gross_sales"]), 2),
                "fees": round(_safe_float(row["fees"]), 2),
                "shipping_cost": round(_safe_float(row["shipping_cost"]), 2),
                "net_sales": round(_safe_float(row["net_sales"]), 2),
                "estimated_margin_vs_known_cost": round(
                    _safe_float(row["estimated_margin_vs_known_cost"]),
                    2,
                ),
            }
        )
    return sorted(
        output,
        key=lambda x: (x.get("sku") or "", x.get("cycle_number") or 0),
    )


def _build_inventory_cycle_summary_rows(cycle_rows: list[dict]) -> list[dict]:
    if not cycle_rows:
        return []

    grouped: dict[tuple[int, str, str], dict] = {}
    for row in cycle_rows:
        product_id = int(row.get("product_id") or 0)
        sku = str(row.get("sku") or "").strip()
        product_title = str(row.get("product_title") or "").strip()
        key = (product_id, sku, product_title)
        if key not in grouped:
            grouped[key] = {
                "product_id": product_id,
                "sku": sku,
                "product_title": product_title,
                "cycle_count": 0,
                "open_cycle_count": 0,
                "closed_cycle_count": 0,
                "qty_in_total": 0,
                "qty_out_total": 0,
                "qty_sold_total": 0,
                "sale_count_total": 0,
                "net_sales_total": 0.0,
                "acquisition_cost_known_total": 0.0,
                "estimated_margin_vs_known_cost_total": 0.0,
                "avg_closed_cycle_days": 0.0,
                "last_cycle_status": "",
                "last_cycle_end": "",
                "_closed_durations": [],
                "_last_cycle_number": 0,
            }
        agg = grouped[key]
        agg["cycle_count"] += 1
        status = str(row.get("cycle_status") or "").strip().lower()
        if status == "closed":
            agg["closed_cycle_count"] += 1
        else:
            agg["open_cycle_count"] += 1

        agg["qty_in_total"] += int(row.get("qty_in") or 0)
        agg["qty_out_total"] += int(row.get("qty_out_movements") or 0)
        agg["qty_sold_total"] += int(row.get("qty_sold_sales") or 0)
        agg["sale_count_total"] += int(row.get("sale_count") or 0)
        agg["net_sales_total"] += _safe_float(row.get("net_sales"))
        agg["acquisition_cost_known_total"] += _safe_float(row.get("acquisition_cost_known"))
        agg["estimated_margin_vs_known_cost_total"] += _safe_float(row.get("estimated_margin_vs_known_cost"))

        cycle_num = int(row.get("cycle_number") or 0)
        if cycle_num >= int(agg.get("_last_cycle_number") or 0):
            agg["_last_cycle_number"] = cycle_num
            agg["last_cycle_status"] = str(row.get("cycle_status") or "").strip()
            agg["last_cycle_end"] = str(row.get("cycle_end") or "").strip()

        if status == "closed":
            start_raw = str(row.get("cycle_start") or "").strip()
            end_raw = str(row.get("cycle_end") or "").strip()
            if start_raw and end_raw:
                try:
                    start_dt = datetime.fromisoformat(start_raw)
                    end_dt = datetime.fromisoformat(end_raw)
                    duration = (end_dt - start_dt).total_seconds() / 86400.0
                    if duration >= 0:
                        agg["_closed_durations"].append(float(duration))
                except Exception:
                    pass

    output: list[dict] = []
    for agg in grouped.values():
        durations = agg.pop("_closed_durations", [])
        agg.pop("_last_cycle_number", None)
        if durations:
            agg["avg_closed_cycle_days"] = round(float(sum(durations) / len(durations)), 2)
        else:
            agg["avg_closed_cycle_days"] = 0.0
        agg["net_sales_total"] = round(float(agg["net_sales_total"]), 2)
        agg["acquisition_cost_known_total"] = round(float(agg["acquisition_cost_known_total"]), 2)
        agg["estimated_margin_vs_known_cost_total"] = round(float(agg["estimated_margin_vs_known_cost_total"]), 2)
        output.append(agg)
    return sorted(
        output,
        key=lambda r: (
            -int(r.get("closed_cycle_count") or 0),
            str(r.get("sku") or ""),
        ),
    )


def _build_rebuy_cost_trend_rows(
    products,
    assignments,
    movements,
) -> list[dict]:
    product_by_id = {int(p.id): p for p in products if p is not None and p.id is not None}
    assignment_keys = set()
    acquisition_events: dict[int, list[dict]] = defaultdict(list)

    for a in assignments:
        if a.product_id is None:
            continue
        pid = int(a.product_id)
        qty = max(0, int(a.quantity_acquired or 0))
        unit_cost = _safe_float(a.unit_cost)
        if qty <= 0 or unit_cost <= 0:
            continue
        ts = a.acquired_at or datetime.min
        key = (pid, ts, qty, round(unit_cost, 6))
        assignment_keys.add(key)
        acquisition_events[pid].append(
            {
                "occurred_at": ts,
                "event_type": "lot_assignment",
                "qty_in": qty,
                "unit_cost": unit_cost,
                "source_ref": f"assignment:{a.id}",
            }
        )

    for m in movements:
        if m.product_id is None:
            continue
        pid = int(m.product_id)
        mv_type = (m.movement_type or "").strip().lower()
        if mv_type not in {"initial_stock", "repurchase_in"}:
            continue
        qty = max(0, int(m.quantity_delta or 0))
        unit_cost = _safe_float(m.unit_cost)
        if qty <= 0 or unit_cost <= 0:
            continue
        ts = m.occurred_at or datetime.min
        key = (pid, ts, qty, round(unit_cost, 6))
        if key in assignment_keys:
            continue
        acquisition_events[pid].append(
            {
                "occurred_at": ts,
                "event_type": mv_type,
                "qty_in": qty,
                "unit_cost": unit_cost,
                "source_ref": f"movement:{m.id}",
            }
        )

    rows: list[dict] = []
    for pid, events in acquisition_events.items():
        product = product_by_id.get(pid)
        cumulative_qty = 0.0
        cumulative_cost = 0.0
        for idx, event in enumerate(
            sorted(events, key=lambda x: (x["occurred_at"], x["event_type"], x["source_ref"])),
            start=1,
        ):
            qty = float(event["qty_in"])
            unit_cost = _safe_float(event["unit_cost"])
            cumulative_qty += qty
            cumulative_cost += qty * unit_cost
            weighted_unit_cost = (cumulative_cost / cumulative_qty) if cumulative_qty > 0 else 0.0
            rows.append(
                {
                    "product_id": pid,
                    "sku": product.sku if product else None,
                    "product_title": product.title if product else None,
                    "event_index": idx,
                    "as_of": iso_or_none(event["occurred_at"]),
                    "event_type": event["event_type"],
                    "qty_in": int(qty),
                    "unit_cost": round(unit_cost, 4),
                    "acquisition_value": round(qty * unit_cost, 2),
                    "cumulative_qty_acquired": round(cumulative_qty, 2),
                    "cumulative_acquisition_cost": round(cumulative_cost, 2),
                    "weighted_unit_cost": round(weighted_unit_cost, 4),
                    "source_ref": event["source_ref"],
                }
            )

    return sorted(rows, key=lambda x: (x.get("sku") or "", x.get("event_index") or 0))


def _build_listing_review_activity_rows(
    listings,
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    rows: list[dict] = []
    for listing in listings:
        marketplace = (listing.marketplace or "").strip().lower()
        sku = listing.product.sku if listing.product else None
        title = listing.listing_title
        payload_raw = (listing.marketplace_details or "").strip()
        if not payload_raw:
            continue
        try:
            payload = json.loads(payload_raw)
        except Exception:
            continue
        history = payload.get("review_history")
        if not isinstance(history, list):
            continue
        for event in history:
            if not isinstance(event, dict):
                continue
            reviewed_at_raw = str(event.get("reviewed_at") or "").strip()
            if not reviewed_at_raw:
                continue
            reviewed_at_dt: datetime | None = None
            try:
                reviewed_at_dt = datetime.fromisoformat(reviewed_at_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                reviewed_at_dt = None
            if reviewed_at_dt is None:
                continue
            if not (start_dt <= reviewed_at_dt <= end_dt):
                continue
            rows.append(
                {
                    "listing_id": listing.id,
                    "marketplace": marketplace,
                    "sku": sku,
                    "listing_title": title,
                    "review_decision": str(event.get("decision") or "").strip().lower(),
                    "reviewed_by": str(event.get("actor") or "").strip(),
                    "reviewed_at": reviewed_at_dt.isoformat(),
                    "review_date": reviewed_at_dt.date().isoformat(),
                    "review_notes": str(event.get("notes") or "").strip(),
                }
            )
    return sorted(rows, key=lambda x: (x.get("reviewed_at") or "", x.get("listing_id") or 0), reverse=True)


def _build_listing_format_outcome_rows(
    listings,
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    rows: list[dict] = []
    for listing in listings:
        listed_at = listing.listed_at
        if listed_at is not None and not (start_dt <= listed_at <= end_dt):
            continue
        meta = {}
        details_raw = str(listing.marketplace_details or "").strip()
        if details_raw:
            try:
                parsed = json.loads(details_raw)
                if isinstance(parsed, dict):
                    publish_meta = parsed.get("ebay_publish")
                    if isinstance(publish_meta, dict):
                        meta = publish_meta
            except Exception:
                meta = {}
        intent_format = str(
            meta.get("format") or meta.get("format_type") or "FIXED_PRICE"
        ).strip().upper()
        if intent_format not in {"FIXED_PRICE", "AUCTION"}:
            intent_format = "FIXED_PRICE"
        intent_duration = str(meta.get("listing_duration") or "").strip().upper()
        publish_history = meta.get("history") if isinstance(meta.get("history"), list) else []
        publish_attempt_count = len(publish_history)
        publish_success_count = len(
            [h for h in publish_history if str((h or {}).get("status") or "").strip().lower() in {"published", "success"}]
        )
        publish_error_events = [
            h for h in publish_history if str((h or {}).get("status") or "").strip().lower() in {"error", "failed"}
        ]
        publish_error_count = len(publish_error_events)
        last_error = ""
        if publish_error_events:
            last_error = str((publish_error_events[-1] or {}).get("error") or "").strip()
        published_at = str(meta.get("published_at") or "").strip()
        external_listing_id = str(listing.external_listing_id or "").strip()
        listing_state = str(listing.listing_status or "").strip().lower()
        if external_listing_id and listing_state in {"active", "ended", "sold"}:
            publish_outcome = "published"
        elif publish_error_count > 0:
            publish_outcome = "publish_error"
        elif publish_attempt_count > 0:
            publish_outcome = "attempted_no_publish"
        else:
            publish_outcome = "not_attempted"
        rows.append(
            {
                "listing_id": int(listing.id),
                "listed_at": iso_or_none(listed_at),
                "marketplace": str(listing.marketplace or "").strip().lower(),
                "sku": listing.product.sku if listing.product else None,
                "listing_title": str(listing.listing_title or "").strip(),
                "review_status": str(listing.review_status or "").strip().lower(),
                "listing_status": listing_state,
                "intent_format": intent_format,
                "intent_duration": intent_duration,
                "intent_best_offer_enabled": bool(meta.get("best_offer_enabled")),
                "intent_auction_start_price": _safe_float(meta.get("auction_start_price")),
                "intent_auction_reserve_price": _safe_float(meta.get("auction_reserve_price")),
                "intent_auction_buy_now_price": _safe_float(meta.get("auction_buy_now_price")),
                "publish_attempt_count": int(publish_attempt_count),
                "publish_success_count": int(publish_success_count),
                "publish_error_count": int(publish_error_count),
                "publish_outcome": publish_outcome,
                "published_at": published_at or None,
                "published_listing_id": external_listing_id or None,
                "last_publish_error": last_error or None,
            }
        )
    return sorted(
        rows,
        key=lambda x: (str(x.get("listed_at") or ""), int(x.get("listing_id") or 0)),
        reverse=True,
    )


def render_reports(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Reports")
    st.caption("Operational reports and export files for bookkeeping and QuickBooks workflows.")
    render_help_panel(
        section_title="Reports",
        goal="Generate operational and accounting exports with a selectable date range.",
        steps=[
            "Set report start/end dates to scope records for inventory, listings, and sales.",
            "Review report tables in-app before exporting to CSV or XLSX.",
            "Use QuickBooks export files as staging inputs for accounting sync workflows.",
            "Re-run exports after data corrections to keep downstream books accurate.",
        ],
        roadmap_phase="v0.3 Channel Sync + Accounting Readiness",
    )

    c1, c2 = st.columns(2)
    with c1:
        from_date = st.date_input("From Date", value=utc_today().replace(day=1))
    with c2:
        to_date = st.date_input("To Date", value=utc_today())

    start_dt = datetime.combine(from_date, datetime.min.time())
    end_dt = datetime.combine(to_date, datetime.max.time())
    load_extended_analytics = st.checkbox(
        "Load Extended Analytics (slower; includes fee reconciliation, lifecycle trends, and deep margin rollups)",
        value=False,
        key="reports_load_extended_analytics",
    )
    load_inventory_cycle_analytics = st.checkbox(
        "Load Inventory Cycle + Rebuy Analytics (slower)",
        value=False,
        key="reports_load_inventory_cycle_analytics",
    )
    load_shipping_tax_analytics = st.checkbox(
        "Load Shipping + Tax Analytics (slower)",
        value=False,
        key="reports_load_shipping_tax_analytics",
    )
    render_full_tables = st.checkbox(
        "Render full report tables (slower)",
        value=False,
        key="reports_render_full_tables",
    )
    preview_row_limit = int(
        st.number_input(
            "Report preview row limit",
            min_value=25,
            max_value=5000,
            value=250,
            step=25,
            key="reports_preview_row_limit",
            help="Used when full table rendering is disabled.",
        )
    )

    def _render_df_with_preview(df: pd.DataFrame, *, hide_index: bool = False) -> None:
        bounded_df, truncated = _bounded_dataframe(
            df,
            render_full_tables=render_full_tables,
            preview_row_limit=preview_row_limit,
        )
        if truncated:
            st.caption(
                f"Showing preview rows only (`{int(len(bounded_df))}` of `{int(len(df))}`). "
                "Enable `Render full report tables` for complete in-app rendering."
            )
        st.dataframe(bounded_df, use_container_width=True, hide_index=hide_index)

    def _load_rollup_rows(method_name: str, *, enabled: bool, **kwargs) -> tuple[list[dict], bool]:
        if not enabled:
            return [], False
        method = getattr(repo, method_name, None)
        if method is None:
            return [], False
        try:
            rows = method(**kwargs) or []
            return list(rows), True
        except Exception:
            return [], False

    all_products: list | None = None
    all_listings: list | None = None
    supports_products_rollup = hasattr(repo, "report_products_rows")
    supports_listings_rollup = hasattr(repo, "report_listings_rows")
    supports_sales_rollup = hasattr(repo, "report_sales_rows")
    all_sales: list | None = None
    all_orders: list | None = None
    supports_movement_rollup = hasattr(repo, "report_inventory_movement_rows")
    supports_cycle_rollup = hasattr(repo, "report_inventory_cycle_rows")
    supports_rebuy_rollup = hasattr(repo, "report_rebuy_cost_trend_rows")
    supports_orders_rollup = hasattr(repo, "report_orders_rows")
    supports_order_items_rollup = hasattr(repo, "report_order_items_rows")
    supports_returns_rollup = hasattr(repo, "report_returns_rows")
    supports_lot_assignment_rollup = hasattr(repo, "report_lot_assignment_rows")
    supports_cost_maps_rollup = hasattr(repo, "report_sale_unit_cost_maps")
    supports_listing_review_rollup = hasattr(repo, "report_listing_review_activity_rows")
    supports_listing_format_rollup = hasattr(repo, "report_listing_format_outcome_rows")

    product_rows, product_rows_loaded = _load_rollup_rows(
        "report_products_rows",
        enabled=supports_products_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    listing_rows, listing_rows_loaded = _load_rollup_rows(
        "report_listings_rows",
        enabled=supports_listings_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    order_rows, order_rows_loaded = _load_rollup_rows(
        "report_orders_rows",
        enabled=supports_orders_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    sales_rows, sales_rows_loaded = _load_rollup_rows(
        "report_sales_rows",
        enabled=supports_sales_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    all_returns: list | None = None
    all_order_items: list | None = None
    all_movements: list | None = None
    all_assignments: list | None = None

    def _get_all_products() -> list:
        nonlocal all_products
        if all_products is None:
            all_products = repo.list_products()
        return all_products

    def _get_all_listings() -> list:
        nonlocal all_listings
        if all_listings is None:
            all_listings = repo.list_listings()
        return all_listings

    def _get_all_sales() -> list:
        nonlocal all_sales
        if all_sales is None:
            all_sales = repo.list_sales()
        return all_sales

    def _get_all_orders() -> list:
        nonlocal all_orders
        if all_orders is None:
            all_orders = repo.list_orders()
        return all_orders

    def _get_all_order_items() -> list:
        nonlocal all_order_items
        if all_order_items is None:
            all_order_items = repo.list_order_items()
        return all_order_items

    def _get_all_returns() -> list:
        nonlocal all_returns
        if all_returns is None:
            all_returns = repo.list_returns()
        return all_returns

    def _get_all_movements() -> list:
        nonlocal all_movements
        if all_movements is None:
            all_movements = repo.list_inventory_movements(limit=20000)
        return all_movements

    def _get_all_assignments() -> list:
        nonlocal all_assignments
        if all_assignments is None:
            all_assignments = repo.list_product_lot_assignments()
        return all_assignments

    if product_rows_loaded:
        products = [_product_from_row(row) for row in product_rows]
    else:
        products = [
            p
            for p in _get_all_products()
            if p.acquired_at is None or start_dt <= p.acquired_at <= end_dt
        ]
    if listing_rows_loaded:
        listings = [_listing_from_row(row) for row in listing_rows]
    else:
        listings = [
            l
            for l in _get_all_listings()
            if l.listed_at is None or start_dt <= l.listed_at <= end_dt
        ]
    if sales_rows_loaded:
        sales = [_sale_from_row(row) for row in sales_rows]
    else:
        sales = [
            s
            for s in _get_all_sales()
            if s.sold_at is not None and start_dt <= s.sold_at <= end_dt
        ]
    if order_rows_loaded:
        orders = [_order_from_row(row) for row in order_rows]
    else:
        orders = [
            o
            for o in _get_all_orders()
            if o.sold_at is not None and start_dt <= o.sold_at <= end_dt
        ]
    order_items = []
    returns = []
    assignments = []
    movement_rows, movement_rows_loaded = _load_rollup_rows(
        "report_inventory_movement_rows",
        enabled=supports_movement_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    movements = []
    if not movement_rows_loaded:
        movements = [
            m
            for m in _get_all_movements()
            if m.occurred_at is None or start_dt <= m.occurred_at <= end_dt
        ]
    return_rows, return_rows_loaded = _load_rollup_rows(
        "report_returns_rows",
        enabled=supports_returns_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    if not return_rows_loaded:
        returns = [
            r
            for r in _get_all_returns()
            if r.returned_at is not None and start_dt <= r.returned_at <= end_dt
        ]
    assignment_rows, assignment_rows_loaded = _load_rollup_rows(
        "report_lot_assignment_rows",
        enabled=supports_lot_assignment_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    if not assignment_rows_loaded:
        assignments = [
            a
            for a in _get_all_assignments()
            if a.acquired_at is None or start_dt <= a.acquired_at <= end_dt
        ]
    default_unit_cost_by_product = {int(p.id): _landed_unit_cost_from_product(p) for p in products}
    fifo_unit_cost_by_sale: dict[int, float] = {}
    lot_weighted_unit_cost_by_product: dict[int, float] = {}
    if supports_cost_maps_rollup:
        try:
            maps_payload = repo.report_sale_unit_cost_maps(
                end_dt=end_dt,
                default_unit_cost_by_product=default_unit_cost_by_product,
            )
            fifo_unit_cost_by_sale = {
                int(k): _safe_float(v)
                for k, v in dict(maps_payload.get("fifo_unit_cost_by_sale") or {}).items()
            }
            lot_weighted_unit_cost_by_product = {
                int(k): _safe_float(v)
                for k, v in dict(maps_payload.get("lot_weighted_unit_cost_by_product") or {}).items()
            }
        except Exception:
            fifo_unit_cost_by_sale = {}
            lot_weighted_unit_cost_by_product = {}
    if not fifo_unit_cost_by_sale and not lot_weighted_unit_cost_by_product:
        all_sales = _get_all_sales()
        all_assignments = _get_all_assignments()
        fifo_unit_cost_by_sale = _build_fifo_unit_cost_map(
            all_sales=all_sales,
            all_assignments=all_assignments,
            default_unit_cost_by_product=default_unit_cost_by_product,
        )
        lot_weighted_unit_cost_by_product = _build_lot_weighted_unit_cost_map(
            all_assignments=all_assignments,
            default_unit_cost_by_product=default_unit_cost_by_product,
        )

    st.markdown("### Tax Reporting Scope")
    tax_default_jurisdiction = get_runtime_str(repo, "invoicing_tax_jurisdiction", "Golden, Colorado")
    tax_default_rate_raw = get_runtime_str(repo, "invoicing_tax_rate_percent_default", "7.50")
    try:
        tax_default_rate = float(tax_default_rate_raw)
    except Exception:
        tax_default_rate = 0.0
    tax_shipping_taxable_default = get_runtime_bool(
        repo,
        "invoicing_tax_shipping_taxable_default",
        False,
    )
    tax_exempt_categories = _parse_csv_set(
        get_runtime_str(repo, "invoicing_tax_exempt_categories_csv", "bullion,coins")
    )
    sales_marketplace_options = sorted(
        {
            str((s.marketplace or "")).strip().lower()
            for s in sales
            if str((s.marketplace or "")).strip()
        }
    )
    facilitator_channels = _parse_csv_set(
        get_runtime_str(repo, "marketplace_facilitator_channels_csv", "ebay")
    )
    default_tax_marketplaces = _default_tax_marketplace_scope(
        sales_marketplace_options=sales_marketplace_options,
        facilitator_channels=facilitator_channels,
    )
    if "reports_tax_jurisdiction" not in st.session_state:
        st.session_state["reports_tax_jurisdiction"] = str(tax_default_jurisdiction or "Golden, Colorado")
    if "reports_tax_rate_percent" not in st.session_state:
        st.session_state["reports_tax_rate_percent"] = float(max(0.0, tax_default_rate))
    if "reports_tax_shipping_taxable" not in st.session_state:
        st.session_state["reports_tax_shipping_taxable"] = bool(tax_shipping_taxable_default)
    if "reports_tax_marketplaces" not in st.session_state:
        st.session_state["reports_tax_marketplaces"] = list(default_tax_marketplaces)
    tr1, tr2, tr3 = st.columns(3)
    with tr1:
        tax_jurisdiction = st.text_input(
            "Tax Jurisdiction Context",
            key="reports_tax_jurisdiction",
        ).strip()
    with tr2:
        tax_rate_percent = st.number_input(
            "Estimated Tax Rate (%)",
            min_value=0.0,
            step=0.01,
            key="reports_tax_rate_percent",
        )
    with tr3:
        tax_shipping_taxable = st.checkbox(
            "Treat shipping as taxable",
            key="reports_tax_shipping_taxable",
        )
    if facilitator_channels:
        st.caption(
            "Marketplace facilitator channels are excluded by default in local tax scope: "
            + ", ".join(sorted(facilitator_channels))
            + "."
        )
    preset_map = _tax_report_presets(
        default_jurisdiction=tax_default_jurisdiction,
        default_tax_rate_percent=float(max(0.0, tax_default_rate)),
        default_shipping_taxable=bool(tax_shipping_taxable_default),
    )
    tp1, tp2 = st.columns([2, 1])
    with tp1:
        tax_preset_name = st.selectbox(
            "Tax Report Preset",
            options=list(preset_map.keys()),
            key="reports_tax_preset_name",
        )
    with tp2:
        if st.button("Apply Tax Report Preset", key="reports_apply_tax_preset_btn"):
            preset = preset_map.get(tax_preset_name) or {}
            st.session_state["reports_tax_jurisdiction"] = str(
                preset.get("jurisdiction") or tax_default_jurisdiction or "Golden, Colorado"
            )
            st.session_state["reports_tax_rate_percent"] = float(
                max(0.0, float(preset.get("tax_rate_percent") or 0.0))
            )
            st.session_state["reports_tax_shipping_taxable"] = bool(preset.get("shipping_taxable", False))
            marketplace_mode = str(preset.get("marketplace_mode") or "all").strip().lower()
            if marketplace_mode == "local_only":
                local_candidates = [m for m in sales_marketplace_options if m in {"local", "in_person", "pos"}]
                st.session_state["reports_tax_marketplaces"] = local_candidates or list(sales_marketplace_options)
            else:
                st.session_state["reports_tax_marketplaces"] = list(sales_marketplace_options)
            st.success(f"Applied tax report preset `{tax_preset_name}`.")
            st.rerun()
    # Keep state value normalized to available options before rendering keyed widget.
    current_tax_marketplaces = st.session_state.get("reports_tax_marketplaces") or list(sales_marketplace_options)
    st.session_state["reports_tax_marketplaces"] = [
        m for m in current_tax_marketplaces if m in sales_marketplace_options
    ]
    selected_tax_marketplaces = st.multiselect(
        "Tax Marketplace Filter",
        options=sales_marketplace_options,
        key="reports_tax_marketplaces",
        help="Estimate tax on selected marketplaces only.",
    )
    selected_tax_marketplace_set = {str(v).strip().lower() for v in selected_tax_marketplaces if str(v).strip()}
    st.caption(
        "Tax-exempt categories (runtime): "
        + (", ".join(sorted(tax_exempt_categories)) if tax_exempt_categories else "(none)")
    )
    st.info(
        "Tax outputs in this report are estimates for operational planning. "
        "Validate local/state tax treatment (including bullion/coin exemptions) with your tax advisor."
    )

    sales_df = pd.DataFrame(
        [
            {
                "sale_id": s.id,
                "sold_at": iso_or_none(s.sold_at),
                "marketplace": s.marketplace,
                "order_id": s.order_id,
                "sku": s.product.sku if s.product else None,
                "product_title": s.product.title if s.product else None,
                "listing_id": s.listing_id,
                "external_order_id": s.external_order_id,
                "qty": s.quantity_sold,
                "gross_sales": float(s.sold_price),
                "fees": float(s.fees),
                "shipping_cost": float(s.shipping_cost),
                "shipping_provider": s.shipping_provider,
                "shipping_service": s.shipping_service,
                "shipping_package_type": s.shipping_package_type,
                "tracking_number": s.tracking_number,
                "tracking_status": s.tracking_status,
                "shipping_exception_code": s.shipping_exception_code,
                "shipping_exception_action": s.shipping_exception_action,
                "shipping_exception_notes": s.shipping_exception_notes,
                "shipping_exception_resolved_at": iso_or_none(s.shipping_exception_resolved_at),
                "shipping_exception_resolved_by": s.shipping_exception_resolved_by,
                "shipment_exported_at": iso_or_none(s.shipment_exported_at),
                "shipped_at": iso_or_none(s.shipped_at),
                "delivered_at": iso_or_none(s.delivered_at),
                "net_sales": float(s.sold_price - s.fees - s.shipping_cost),
            }
            for s in sales
        ]
    )
    shipping_economics_df = pd.DataFrame()
    shipping_econ_summary_df = pd.DataFrame()
    tax_detail_rows = []
    tax_detail_df = pd.DataFrame()
    tax_summary_df = pd.DataFrame()
    tax_by_marketplace_df = pd.DataFrame()
    if load_shipping_tax_analytics:
        shipping_marketplaces = {"ebay", "facebook", "craigslist", "local", "in_person", "pos"}
        if hasattr(repo, "report_shipping_economics_rows"):
            shipping_rows = repo.report_shipping_economics_rows(
                start_dt=start_dt,
                end_dt=end_dt,
                marketplaces=shipping_marketplaces,
            )
            shipping_economics_df = pd.DataFrame(shipping_rows)
            if not shipping_economics_df.empty:
                shipping_economics_df["sold_at"] = shipping_economics_df["sold_at"].apply(iso_or_none)
                shipping_economics_df["shipping_label_purchased_at"] = shipping_economics_df[
                    "shipping_label_purchased_at"
                ].apply(iso_or_none)
            summary_rows = repo.report_shipping_economics_summary(
                start_dt=start_dt,
                end_dt=end_dt,
                marketplaces=shipping_marketplaces,
            )
            shipping_econ_summary_df = pd.DataFrame(summary_rows)
        else:
            shipping_economics_df = pd.DataFrame(
                [
                    {
                        "sale_id": int(s.id),
                        "sold_at": iso_or_none(s.sold_at),
                        "marketplace": str(s.marketplace or "").strip().lower(),
                        "external_order_id": str(s.external_order_id or "").strip(),
                        "order_id": int(s.order_id) if s.order_id is not None else None,
                        "sku": s.product.sku if s.product else None,
                        "product_title": s.product.title if s.product else None,
                        "qty": int(s.quantity_sold or 0),
                        "shipping_charged_to_buyer": round(_safe_float(s.shipping_cost), 2),
                        "shipping_label_spend": round(_safe_float(getattr(s, "shipping_label_cost", None)), 2),
                        "shipping_delta_charged_minus_spend": round(
                            _safe_float(s.shipping_cost) - _safe_float(getattr(s, "shipping_label_cost", None)),
                            2,
                        ),
                        "shipping_label_currency": str(getattr(s, "shipping_label_currency", "") or "").strip(),
                        "shipping_label_id": str(getattr(s, "shipping_label_id", "") or "").strip(),
                        "shipping_label_purchased_at": iso_or_none(getattr(s, "shipping_label_purchased_at", None)),
                        "shipping_provider": str(s.shipping_provider or "").strip(),
                        "shipping_service": str(s.shipping_service or "").strip(),
                        "tracking_number": str(s.tracking_number or "").strip(),
                    }
                    for s in sales
                    if str(s.marketplace or "").strip().lower() in shipping_marketplaces
                ]
            )
            shipping_econ_summary_df = pd.DataFrame()
            if not shipping_economics_df.empty:
                shipping_econ_summary_df = (
                    shipping_economics_df.groupby(["marketplace"], dropna=False, as_index=False)
                    .agg(
                        sales_count=("sale_id", "count"),
                        total_shipping_charged=("shipping_charged_to_buyer", "sum"),
                        total_label_spend=("shipping_label_spend", "sum"),
                    )
                    .sort_values(["sales_count"], ascending=[False])
                )
                shipping_econ_summary_df["shipping_delta_charged_minus_spend"] = (
                    shipping_econ_summary_df["total_shipping_charged"] - shipping_econ_summary_df["total_label_spend"]
                )
                # Compute coverage once per marketplace and merge, avoiding per-row
                # DataFrame scans over the full shipping dataset.
                coverage_counts = (
                    shipping_economics_df.assign(
                        _has_label_spend=shipping_economics_df["shipping_label_spend"] > 0
                    )
                    .groupby(["marketplace"], dropna=False, as_index=False)
                    .agg(label_spend_covered_count=("_has_label_spend", "sum"))
                )
                shipping_econ_summary_df = shipping_econ_summary_df.merge(
                    coverage_counts,
                    on="marketplace",
                    how="left",
                )
                shipping_econ_summary_df["label_spend_covered_count"] = (
                    shipping_econ_summary_df["label_spend_covered_count"].fillna(0).astype(int)
                )
                shipping_econ_summary_df["label_spend_coverage_percent"] = (
                    (
                        shipping_econ_summary_df["label_spend_covered_count"]
                        / shipping_econ_summary_df["sales_count"].replace(0, pd.NA)
                    )
                    * 100.0
                ).fillna(0.0)

        if hasattr(repo, "report_tax_estimate_detail_rows"):
            tax_detail_rows = repo.report_tax_estimate_detail_rows(
                start_dt=start_dt,
                end_dt=end_dt,
                tax_rate_percent=float(tax_rate_percent),
                shipping_taxable=bool(tax_shipping_taxable),
                tax_exempt_categories=tax_exempt_categories,
                marketplaces=selected_tax_marketplace_set,
            )
            for row in tax_detail_rows:
                row["sold_at"] = iso_or_none(row.get("sold_at"))
                row["tax_jurisdiction"] = tax_jurisdiction or tax_default_jurisdiction
                row["estimated_tax_rate_percent"] = float(tax_rate_percent)
        else:
            for s in sales:
                marketplace = str(s.marketplace or "").strip().lower()
                if selected_tax_marketplace_set and marketplace not in selected_tax_marketplace_set:
                    continue
                sold_price = _safe_float(s.sold_price)
                shipping_cost = _safe_float(s.shipping_cost)
                category = str((s.product.category if s.product else "") or "").strip().lower()
                is_exempt = bool(category and category in tax_exempt_categories)
                taxable_item_subtotal = 0.0 if is_exempt else sold_price
                taxable_shipping = shipping_cost if tax_shipping_taxable else 0.0
                taxable_subtotal = max(0.0, taxable_item_subtotal + taxable_shipping)
                estimated_tax = round(taxable_subtotal * (float(tax_rate_percent) / 100.0), 2)
                tax_detail_rows.append(
                    {
                        "sale_id": s.id,
                        "sold_at": iso_or_none(s.sold_at),
                        "marketplace": marketplace,
                        "sku": s.product.sku if s.product else None,
                        "product_title": s.product.title if s.product else None,
                        "category": category,
                        "gross_sales": sold_price,
                        "shipping_cost": shipping_cost,
                        "is_tax_exempt_category": bool(is_exempt),
                        "taxable_item_subtotal": round(taxable_item_subtotal, 2),
                        "taxable_shipping_subtotal": round(taxable_shipping, 2),
                        "taxable_subtotal": round(taxable_subtotal, 2),
                        "estimated_tax_collected": estimated_tax,
                        "tax_jurisdiction": tax_jurisdiction or tax_default_jurisdiction,
                        "estimated_tax_rate_percent": float(tax_rate_percent),
                    }
                )
        tax_detail_df = pd.DataFrame(tax_detail_rows)
        tax_summary_rows = []
        if not tax_detail_df.empty:
            taxable_subtotal_sum = float(tax_detail_df["taxable_subtotal"].sum())
            gross_sales_sum = float(tax_detail_df["gross_sales"].sum())
            exempt_subtotal_sum = max(0.0, gross_sales_sum - float(tax_detail_df["taxable_item_subtotal"].sum()))
            tax_summary_rows.append(
                {
                    "jurisdiction": tax_jurisdiction or tax_default_jurisdiction,
                    "tax_rate_percent": float(tax_rate_percent),
                    "shipping_taxable": bool(tax_shipping_taxable),
                    "marketplace_scope": ",".join(sorted(selected_tax_marketplace_set)) if selected_tax_marketplace_set else "all",
                    "sales_count": int(len(tax_detail_df)),
                    "gross_sales_subtotal": round(gross_sales_sum, 2),
                    "taxable_subtotal": round(taxable_subtotal_sum, 2),
                    "exempt_subtotal": round(exempt_subtotal_sum, 2),
                    "estimated_tax_collected": round(float(tax_detail_df["estimated_tax_collected"].sum()), 2),
                }
            )
        tax_summary_df = pd.DataFrame(tax_summary_rows)
        tax_by_marketplace_df = pd.DataFrame()
        if not tax_detail_df.empty:
            tax_by_marketplace_df = (
                tax_detail_df.groupby(["marketplace"], as_index=False)
                .agg(
                    sales_count=("sale_id", "count"),
                    gross_sales_subtotal=("gross_sales", "sum"),
                    taxable_subtotal=("taxable_subtotal", "sum"),
                    estimated_tax_collected=("estimated_tax_collected", "sum"),
                )
                .sort_values(["estimated_tax_collected"], ascending=[False])
            )

    inventory_df = pd.DataFrame(
        [
            {
                "product_id": p.id,
                "sku": p.sku,
                "title": p.title,
                "category": p.category,
                "metal_type": p.metal_type,
                "acquired_at": iso_or_none(p.acquired_at),
                "unit_cost": float(p.acquisition_cost) if p.acquisition_cost is not None else None,
                "unit_tax_paid": float(getattr(p, "acquisition_tax_paid", None)) if getattr(p, "acquisition_tax_paid", None) is not None else None,
                "unit_shipping_paid": float(getattr(p, "acquisition_shipping_paid", None)) if getattr(p, "acquisition_shipping_paid", None) is not None else None,
                "unit_handling_paid": float(getattr(p, "acquisition_handling_paid", None)) if getattr(p, "acquisition_handling_paid", None) is not None else None,
                "landed_unit_cost": (
                    (
                        float(p.acquisition_cost or 0)
                        + float(getattr(p, "acquisition_tax_paid", 0) or 0)
                        + float(getattr(p, "acquisition_shipping_paid", 0) or 0)
                        + float(getattr(p, "acquisition_handling_paid", 0) or 0)
                    )
                    if (
                        p.acquisition_cost is not None
                        or getattr(p, "acquisition_tax_paid", None) is not None
                        or getattr(p, "acquisition_shipping_paid", None) is not None
                        or getattr(p, "acquisition_handling_paid", None) is not None
                    )
                    else None
                ),
                "item_weight_oz": float(p.weight_oz) if p.weight_oz is not None else None,
                "package_weight_oz": float(p.package_weight_oz) if p.package_weight_oz is not None else None,
                "package_length_in": float(p.package_length_in) if p.package_length_in is not None else None,
                "package_width_in": float(p.package_width_in) if p.package_width_in is not None else None,
                "package_height_in": float(p.package_height_in) if p.package_height_in is not None else None,
                "qty_on_hand": p.current_quantity,
                "inventory_value": (
                    float(p.acquisition_cost * p.current_quantity)
                    if p.acquisition_cost is not None
                    else None
                ),
                "landed_inventory_value": (
                    (
                        float(p.current_quantity or 0)
                        * (
                            float(p.acquisition_cost or 0)
                            + float(getattr(p, "acquisition_tax_paid", 0) or 0)
                            + float(getattr(p, "acquisition_shipping_paid", 0) or 0)
                            + float(getattr(p, "acquisition_handling_paid", 0) or 0)
                        )
                    )
                    if (
                        p.acquisition_cost is not None
                        or getattr(p, "acquisition_tax_paid", None) is not None
                        or getattr(p, "acquisition_shipping_paid", None) is not None
                        or getattr(p, "acquisition_handling_paid", None) is not None
                    )
                    else None
                ),
            }
            for p in products
        ]
    )

    listings_df = pd.DataFrame(
        [
            {
                "listing_id": l.id,
                "listed_at": iso_or_none(l.listed_at),
                "marketplace": l.marketplace,
                "sku": l.product.sku if l.product else None,
                "title": l.listing_title,
                "status": l.listing_status,
                "marketplace_url": l.marketplace_url,
                "marketplace_details": l.marketplace_details,
                "qty_listed": l.quantity_listed,
                "price": float(l.listing_price),
                "external_listing_id": l.external_listing_id,
            }
            for l in listings
        ]
    )

    if assignment_rows_loaded:
        lots_df = pd.DataFrame(assignment_rows)
    else:
        lots_df = pd.DataFrame(
            [
                {
                    "assignment_id": a.id,
                    "lot_code": a.lot.lot_code if a.lot else None,
                    "source_name": a.lot.source.name if a.lot and a.lot.source else None,
                    "source_type": a.lot.source.source_type if a.lot and a.lot.source else None,
                    "vendor": a.lot.vendor if a.lot else None,
                    "purchase_date": iso_or_none(a.lot.purchase_date) if a.lot else None,
                    "sku": a.product.sku if a.product else None,
                    "product_title": a.product.title if a.product else None,
                    "quantity_acquired": a.quantity_acquired,
                    "unit_cost": float(a.unit_cost) if a.unit_cost is not None else None,
                    "allocated_cost": float(a.allocated_cost) if a.allocated_cost is not None else None,
                    "acquired_at": iso_or_none(a.acquired_at),
                }
                for a in assignments
            ]
        )

    qbo_sales_df = pd.DataFrame(
        [
            {
                "txn_date": s.sold_at.date().isoformat(),
                "doc_number": s.external_order_id or f"SALE-{s.id}",
                "customer_ref": s.marketplace.upper(),
                "item_sku": s.product.sku if s.product else "",
                "item_description": s.product.title if s.product else "",
                "quantity": s.quantity_sold,
                "rate": float(s.sold_price / s.quantity_sold) if s.quantity_sold else float(s.sold_price),
                "amount": float(s.sold_price),
                "fees": float(s.fees),
                "shipping_cost": float(s.shipping_cost),
                "tracking_number": s.tracking_number,
                "tracking_status": s.tracking_status,
                "cogs_input_estimate": _landed_unit_cost_from_product(s.product) * int(s.quantity_sold or 0)
                if s.product is not None
                else 0.0,
                "gross_margin_estimate": (
                    _safe_float(s.sold_price)
                    - _safe_float(s.fees)
                    - _safe_float(s.shipping_cost)
                    - (_landed_unit_cost_from_product(s.product) * int(s.quantity_sold or 0) if s.product else 0.0)
                ),
                "net_amount": float(s.sold_price - s.fees - s.shipping_cost),
                "marketplace": s.marketplace,
            }
            for s in sales
        ]
    )

    if return_rows_loaded:
        qbo_adjustments_df = pd.DataFrame(
            [
                {
                    "txn_date": str(row.get("returned_at") or "")[:10],
                    "doc_number": str(row.get("external_return_id") or "").strip()
                    or f"RETURN-{int(row.get('return_id') or 0)}",
                    "source_order": str(row.get("source_order") or "").strip(),
                    "marketplace": str(row.get("marketplace") or "").strip(),
                    "sku": str(row.get("sku") or "").strip(),
                    "description": str(row.get("reason") or row.get("notes") or "Return/Refund").strip(),
                    "adjustment_type": "refund",
                    "refund_amount": _safe_float(row.get("refund_amount")),
                    "refund_fees": _safe_float(row.get("refund_fees")),
                    "refund_shipping": _safe_float(row.get("refund_shipping")),
                    "net_adjustment": -(
                        _safe_float(row.get("refund_amount"))
                        + _safe_float(row.get("refund_fees"))
                        + _safe_float(row.get("refund_shipping"))
                    ),
                    "return_status": str(row.get("status") or "").strip(),
                    "restocked": bool(row.get("restocked")),
                }
                for row in return_rows
            ]
        )
    else:
        qbo_adjustments_df = pd.DataFrame(
            [
                {
                    "txn_date": r.returned_at.date().isoformat() if r.returned_at else "",
                    "doc_number": r.external_return_id or f"RETURN-{r.id}",
                    "source_order": r.sale.external_order_id if r.sale and r.sale.external_order_id else "",
                    "marketplace": r.marketplace,
                    "sku": r.product.sku if r.product else "",
                    "description": r.reason or r.notes or "Return/Refund",
                    "adjustment_type": "refund",
                    "refund_amount": _safe_float(r.refund_amount),
                    "refund_fees": _safe_float(r.refund_fees),
                    "refund_shipping": _safe_float(r.refund_shipping),
                    "net_adjustment": -(
                        _safe_float(r.refund_amount)
                        + _safe_float(r.refund_fees)
                        + _safe_float(r.refund_shipping)
                    ),
                    "return_status": r.return_status,
                    "restocked": bool(r.restocked),
                }
                for r in returns
            ]
        )

    if order_rows_loaded:
        orders_df = pd.DataFrame(order_rows)
    else:
        orders_df = pd.DataFrame(
            [
                {
                    "order_id": o.id,
                    "sold_at": iso_or_none(o.sold_at),
                    "marketplace": o.marketplace,
                    "external_order_id": o.external_order_id,
                    "status": o.order_status,
                    "subtotal_amount": float(o.subtotal_amount),
                    "fees": float(o.fees),
                    "shipping_cost": float(o.shipping_cost),
                    "shipping_label_cost": _safe_float(getattr(o, "shipping_label_cost", None)),
                    "shipping_label_currency": str(getattr(o, "shipping_label_currency", "") or "").strip(),
                    "shipping_delta_charged_minus_actual": round(
                        _safe_float(o.shipping_cost) - _safe_float(getattr(o, "shipping_label_cost", None)),
                        2,
                    ),
                    "total_amount": float(o.total_amount),
                    "item_count": len(o.items),
                    "notes": o.notes,
                }
                for o in orders
            ]
        )

    order_item_rows, order_item_rows_loaded = _load_rollup_rows(
        "report_order_items_rows",
        enabled=supports_order_items_rollup,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    if not order_item_rows_loaded:
        order_items = [
            oi
            for oi in _get_all_order_items()
            if oi.order is not None and oi.order.sold_at is not None and start_dt <= oi.order.sold_at <= end_dt
        ]
    if order_item_rows_loaded:
        order_items_df = pd.DataFrame(order_item_rows)
    else:
        order_items_df = pd.DataFrame(
            [
                {
                    "order_item_id": oi.id,
                    "order_id": oi.order_id,
                    "sold_at": iso_or_none(oi.order.sold_at) if oi.order else None,
                    "marketplace": oi.order.marketplace if oi.order else None,
                    "external_order_id": oi.order.external_order_id if oi.order else None,
                    "product_id": oi.product_id,
                    "listing_id": oi.listing_id,
                    "sku": oi.product.sku if oi.product else None,
                    "product_title": oi.product.title if oi.product else None,
                    "quantity": oi.quantity,
                    "unit_price": float(oi.unit_price),
                    "line_total": float(oi.line_total),
                    "line_fees": float(oi.line_fees),
                    "line_shipping": float(oi.line_shipping),
                    "notes": oi.notes,
                }
                for oi in order_items
            ]
        )
    ebay_order_fee_breakdown_df = pd.DataFrame(
        [
            {
                "order_id": o.id,
                "sold_at": iso_or_none(o.sold_at),
                "external_order_id": o.external_order_id,
                "fee_price_subtotal": _safe_float(breakdown.get("price_subtotal")),
                "fee_delivery_cost": _safe_float(breakdown.get("delivery_cost")),
                "fee_delivery_shipping_cost": _safe_float(breakdown.get("delivery_shipping_cost")),
                "fee_total_marketplace_fee": _safe_float(breakdown.get("total_marketplace_fee")),
                "fee_total_tax": _safe_float(breakdown.get("total_tax")),
                "fee_sales_tax": _safe_float(breakdown.get("sales_tax")),
                "fee_order_total": _safe_float(breakdown.get("order_total")),
                "order_fees_field": _safe_float(o.fees),
                "delta_order_fees_vs_breakdown_total_marketplace_fee": (
                    _safe_float(o.fees) - _safe_float(breakdown.get("total_marketplace_fee"))
                ),
            }
            for o in orders
            for breakdown in [_extract_order_fee_breakdown_from_notes(o.notes)]
            if (o.marketplace or "").strip().lower() == "ebay" and breakdown
        ]
    )
    ebay_marketplace_fee_rows: list[dict] = []
    if hasattr(repo, "report_ebay_marketplace_fee_rows"):
        try:
            ebay_marketplace_fee_rows = repo.report_ebay_marketplace_fee_rows(
                start_dt=start_dt,
                end_dt=end_dt,
            )
        except Exception:
            ebay_marketplace_fee_rows = []
    if not ebay_marketplace_fee_rows:
        ebay_marketplace_fee_rows = _build_ebay_marketplace_fee_rows(repo, orders)
    ebay_marketplace_fee_detail_df = pd.DataFrame(ebay_marketplace_fee_rows)
    ebay_marketplace_fee_summary_df = pd.DataFrame()
    ebay_marketplace_fee_by_sku_df = pd.DataFrame()
    ebay_marketplace_fee_by_category_df = pd.DataFrame()
    if not ebay_marketplace_fee_detail_df.empty:
        category_by_sku: dict[str, str] = {}
        for p in products:
            sku_key = str(getattr(p, "sku", "") or "").strip()
            if not sku_key:
                continue
            if sku_key in category_by_sku:
                continue
            category_by_sku[sku_key] = str(getattr(p, "category", "") or "").strip()
        ebay_marketplace_fee_detail_df["product_category"] = ebay_marketplace_fee_detail_df["sku"].map(
            lambda sku: category_by_sku.get(str(sku or "").strip(), "")
        )
        ebay_marketplace_fee_summary_df = (
            ebay_marketplace_fee_detail_df.groupby(
                ["fee_type", "fee_currency", "source"],
                dropna=False,
                as_index=False,
            )
            .agg(
                fee_row_count=("fee_type", "count"),
                order_count=("external_order_id", "nunique"),
                fee_amount_total=("fee_amount", "sum"),
                first_seen=("transaction_date", "min"),
                last_seen=("transaction_date", "max"),
            )
            .sort_values(["fee_amount_total"], ascending=[False])
        )
        ebay_marketplace_fee_by_sku_df = (
            ebay_marketplace_fee_detail_df.groupby(
                ["sku", "product_title", "product_category", "fee_type", "fee_currency"],
                dropna=False,
                as_index=False,
            )
            .agg(
                fee_row_count=("fee_type", "count"),
                order_count=("external_order_id", "nunique"),
                fee_amount_total=("fee_amount", "sum"),
            )
            .sort_values(["fee_amount_total"], ascending=[False])
        )
        ebay_marketplace_fee_by_category_df = (
            ebay_marketplace_fee_detail_df.groupby(
                ["product_category", "fee_type", "fee_currency"],
                dropna=False,
                as_index=False,
            )
            .agg(
                fee_row_count=("fee_type", "count"),
                order_count=("external_order_id", "nunique"),
                sku_count=("sku", "nunique"),
                fee_amount_total=("fee_amount", "sum"),
            )
            .sort_values(["fee_amount_total"], ascending=[False])
        )

    if return_rows_loaded:
        returns_df = pd.DataFrame(return_rows)
    else:
        returns_df = pd.DataFrame(
            [
                {
                    "return_id": r.id,
                    "returned_at": iso_or_none(r.returned_at),
                    "processed_at": iso_or_none(r.processed_at),
                    "marketplace": r.marketplace,
                    "external_return_id": r.external_return_id,
                    "sale_id": r.sale_id,
                    "order_id": r.order_id,
                    "product_id": r.product_id,
                    "sku": r.product.sku if r.product else None,
                    "product_title": r.product.title if r.product else None,
                    "status": r.return_status,
                    "reason": r.reason,
                    "disposition": r.disposition,
                    "quantity": r.quantity,
                    "refund_amount": float(r.refund_amount),
                    "refund_fees": float(r.refund_fees),
                    "refund_shipping": float(r.refund_shipping),
                    "restocked": r.restocked,
                    "notes": r.notes,
                    "source_order": r.sale.external_order_id if r.sale and r.sale.external_order_id else "",
                }
                for r in returns
            ]
        )

    if movement_rows_loaded:
        movements_df = pd.DataFrame(movement_rows)
    else:
        movements_df = pd.DataFrame(
            [
                {
                    "movement_id": m.id,
                    "occurred_at": iso_or_none(m.occurred_at),
                    "product_id": m.product_id,
                    "sku": m.product.sku if m.product else None,
                    "product_title": m.product.title if m.product else None,
                    "movement_type": m.movement_type,
                    "quantity_delta": m.quantity_delta,
                    "quantity_before": m.quantity_before,
                    "quantity_after": m.quantity_after,
                    "unit_cost": float(m.unit_cost) if m.unit_cost is not None else None,
                    "reference_type": m.reference_type,
                    "reference_id": m.reference_id,
                    "notes": m.notes,
                }
                for m in movements
            ]
        )

    marketplace_rows = []
    if hasattr(repo, "report_marketplace_reconciliation_rows"):
        try:
            marketplace_rows = repo.report_marketplace_reconciliation_rows(
                start_dt=start_dt,
                end_dt=end_dt,
            )
        except Exception:
            marketplace_rows = []
    if not marketplace_rows:
        marketplaces = sorted(
            {
                (s.marketplace or "").strip().lower()
                for s in sales
                if (s.marketplace or "").strip()
            }
            | {
                (o.marketplace or "").strip().lower()
                for o in orders
                if (o.marketplace or "").strip()
            }
        )
        for mp in marketplaces:
            mp_sales = [s for s in sales if (s.marketplace or "").strip().lower() == mp]
            mp_orders = [o for o in orders if (o.marketplace or "").strip().lower() == mp]
            mp_returns_df = (
                returns_df[returns_df["marketplace"].fillna("").astype(str).str.strip().str.lower() == mp]
                if not returns_df.empty and "marketplace" in returns_df.columns
                else pd.DataFrame()
            )

            sales_gross = sum(_safe_float(s.sold_price) for s in mp_sales)
            sales_fees = sum(_safe_float(s.fees) for s in mp_sales)
            sales_shipping = sum(_safe_float(s.shipping_cost) for s in mp_sales)
            sales_net = sum(_safe_float(s.sold_price) - _safe_float(s.fees) - _safe_float(s.shipping_cost) for s in mp_sales)
            returns_total = (
                float(
                    mp_returns_df.get("refund_amount", pd.Series(dtype=float)).fillna(0).astype(float).sum()
                )
                + float(
                    mp_returns_df.get("refund_fees", pd.Series(dtype=float)).fillna(0).astype(float).sum()
                )
                + float(
                    mp_returns_df.get("refund_shipping", pd.Series(dtype=float)).fillna(0).astype(float).sum()
                )
            )
            order_totals = sum(_safe_float(o.total_amount) for o in mp_orders)
            delta = order_totals - sales_gross

            marketplace_rows.append(
                {
                    "marketplace": mp,
                    "sales_count": len(mp_sales),
                    "orders_count": len(mp_orders),
                    "returns_count": int(len(mp_returns_df)),
                    "sales_gross": round(sales_gross, 2),
                    "sales_fees": round(sales_fees, 2),
                    "sales_shipping_cost": round(sales_shipping, 2),
                    "sales_net_before_returns": round(sales_net, 2),
                    "returns_refund_total": round(returns_total, 2),
                    "net_after_returns": round(sales_net - returns_total, 2),
                    "order_total_sum": round(order_totals, 2),
                    "delta_order_total_vs_sales_gross": round(delta, 2),
                    "reconcile_flag": abs(delta) > 0.01,
                }
            )
    reconciliation_df = pd.DataFrame(marketplace_rows)

    validation_rows = []
    for s in sales:
        reasons = []
        if (s.marketplace or "").strip() and not (s.external_order_id or "").strip():
            reasons.append("missing_external_order_id")
        if s.order_id is None:
            reasons.append("missing_order_link")
        if (_safe_float(s.sold_price) - _safe_float(s.fees) - _safe_float(s.shipping_cost)) < 0:
            reasons.append("negative_net_sale")
        if reasons:
            validation_rows.append(
                {
                    "entity_type": "sale",
                    "entity_id": s.id,
                    "marketplace": s.marketplace,
                    "reference": s.external_order_id or "",
                    "issues": ",".join(reasons),
                    "sold_at": iso_or_none(s.sold_at),
                }
            )
    if not returns_df.empty:
        for row in returns_df.to_dict("records"):
            reasons = []
            if row.get("sale_id") is None:
                reasons.append("return_missing_sale_link")
            if (
                _safe_float(row.get("refund_amount"))
                + _safe_float(row.get("refund_fees"))
                + _safe_float(row.get("refund_shipping"))
            ) <= 0:
                reasons.append("return_non_positive_refund_total")
            if reasons:
                validation_rows.append(
                    {
                        "entity_type": "return",
                        "entity_id": int(row.get("return_id") or 0),
                        "marketplace": str(row.get("marketplace") or "").strip(),
                        "reference": str(row.get("external_return_id") or "").strip(),
                        "issues": ",".join(reasons),
                        "sold_at": str(row.get("returned_at") or "").strip(),
                    }
                )
    accounting_validation_df = pd.DataFrame(validation_rows)

    actual_econ_by_sale_id: dict[int, dict[str, float]] = {}
    actual_econ_rows: list[dict] = []
    economics_intel_df = pd.DataFrame()
    economics_intel_by_sku_df = pd.DataFrame()
    economics_intel_by_marketplace_df = pd.DataFrame()
    economics_intel_alerts_df = pd.DataFrame()
    if hasattr(repo, "report_sales_actual_econ_rows"):
        try:
            actual_econ_rows = repo.report_sales_actual_econ_rows(
                start_dt=start_dt,
                end_dt=end_dt,
            )
        except Exception:
            actual_econ_rows = []
    if not actual_econ_rows:
        sales_by_order_id: dict[int, list] = defaultdict(list)
        for s in sales:
            if s.order_id is not None:
                sales_by_order_id[int(s.order_id)].append(s)
        order_by_id = {int(o.id): o for o in orders if o.id is not None}
        for s in sales:
            sold_price = _safe_float(s.sold_price)
            charged_shipping_sale = _safe_float(s.shipping_cost)
            sale_fee_fallback = _safe_float(s.fees)
            sale_label_fallback = _safe_float(getattr(s, "shipping_label_cost", None))

            order_fee_total = sale_fee_fallback
            order_shipping_charged_total = charged_shipping_sale
            order_shipping_actual_total = sale_label_fallback
            weight = 1.0

            if s.order_id is not None and int(s.order_id) in order_by_id:
                order = order_by_id[int(s.order_id)]
                siblings = sales_by_order_id.get(int(s.order_id), [])
                sibling_gross_total = sum(_safe_float(x.sold_price) for x in siblings)
                if sibling_gross_total > 0:
                    weight = sold_price / sibling_gross_total
                elif len(siblings) > 0:
                    weight = 1.0 / float(len(siblings))
                order_fee_total = _safe_float(order.fees)
                order_shipping_charged_total = _safe_float(order.shipping_cost)
                order_shipping_actual_total = _safe_float(getattr(order, "shipping_label_cost", None))

            allocated_fee_actual = order_fee_total * weight
            allocated_shipping_charged = order_shipping_charged_total * weight
            allocated_shipping_actual = order_shipping_actual_total * weight
            net_before_cogs_actual = sold_price - allocated_fee_actual - allocated_shipping_actual

            actual_econ_rows.append(
                {
                    "sale_id": int(s.id),
                    "order_id": int(s.order_id) if s.order_id is not None else None,
                    "marketplace": str(s.marketplace or "").strip().lower(),
                    "external_order_id": str(s.external_order_id or "").strip(),
                    "sku": s.product.sku if s.product else None,
                    "product_title": s.product.title if s.product else None,
                    "qty": int(s.quantity_sold or 0),
                    "sold_price": round(sold_price, 2),
                    "allocation_weight": round(weight, 6),
                    "order_fee_total_actual": round(order_fee_total, 2),
                    "order_shipping_charged_total": round(order_shipping_charged_total, 2),
                    "order_shipping_actual_total": round(order_shipping_actual_total, 2),
                    "allocated_fee_actual": round(allocated_fee_actual, 2),
                    "allocated_shipping_charged": round(allocated_shipping_charged, 2),
                    "allocated_shipping_actual": round(allocated_shipping_actual, 2),
                    "shipping_delta_charged_minus_actual": round(
                        allocated_shipping_charged - allocated_shipping_actual,
                        2,
                    ),
                    "net_before_cogs_actual": round(net_before_cogs_actual, 2),
                }
            )
    if hasattr(repo, "report_economics_intelligence_fact_rows"):
        try:
            economics_intel_df = pd.DataFrame(
                repo.report_economics_intelligence_fact_rows(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    marketplaces={str(m or "").strip().lower() for m in sales_df.get("marketplace", []) if str(m or "").strip()},
                )
            )
            if not economics_intel_df.empty:
                economics_intel_df["sold_at"] = economics_intel_df["sold_at"].apply(iso_or_none)
        except Exception:
            economics_intel_df = pd.DataFrame()
    for row in actual_econ_rows:
        sid = int(_safe_float(row.get("sale_id") or 0))
        if sid <= 0:
            continue
        actual_econ_by_sale_id[sid] = {
            "allocated_fee_actual": _safe_float(row.get("allocated_fee_actual")),
            "allocated_shipping_actual": _safe_float(row.get("allocated_shipping_actual")),
            "net_before_cogs_actual": _safe_float(row.get("net_before_cogs_actual")),
        }
    order_actual_econ_df = pd.DataFrame(actual_econ_rows)

    cogs_margin_df = pd.DataFrame(
        [
            {
                "sale_id": s.id,
                "sold_at": iso_or_none(s.sold_at),
                "marketplace": s.marketplace,
                "sku": s.product.sku if s.product else None,
                "product_title": s.product.title if s.product else None,
                "quantity": int(s.quantity_sold or 0),
                "gross_sales": _safe_float(s.sold_price),
                "fees": _safe_float(s.fees),
                "shipping_cost": _safe_float(s.shipping_cost),
                "net_before_cogs": _safe_float(s.sold_price) - _safe_float(s.fees) - _safe_float(s.shipping_cost),
                "actual_fee_alloc": _safe_float(actual_econ_by_sale_id.get(int(s.id), {}).get("allocated_fee_actual")),
                "actual_shipping_alloc": _safe_float(
                    actual_econ_by_sale_id.get(int(s.id), {}).get("allocated_shipping_actual")
                ),
                "actual_net_before_cogs": _safe_float(
                    actual_econ_by_sale_id.get(int(s.id), {}).get("net_before_cogs_actual")
                ),
                "fifo_unit_cost": _safe_float(fifo_unit_cost_by_sale.get(s.id)),
                "fifo_cogs": _safe_float(fifo_unit_cost_by_sale.get(s.id)) * int(s.quantity_sold or 0),
                "fifo_margin": (
                    _safe_float(s.sold_price)
                    - _safe_float(s.fees)
                    - _safe_float(s.shipping_cost)
                    - (_safe_float(fifo_unit_cost_by_sale.get(s.id)) * int(s.quantity_sold or 0))
                ),
                "lot_unit_cost": _safe_float(
                    lot_weighted_unit_cost_by_product.get(int(s.product_id))
                    if s.product_id is not None
                    else 0.0
                ),
                "lot_cogs": (
                    _safe_float(
                        lot_weighted_unit_cost_by_product.get(int(s.product_id))
                        if s.product_id is not None
                        else 0.0
                    )
                    * int(s.quantity_sold or 0)
                ),
                "lot_margin": (
                    _safe_float(s.sold_price)
                    - _safe_float(s.fees)
                    - _safe_float(s.shipping_cost)
                    - (
                        _safe_float(
                            lot_weighted_unit_cost_by_product.get(int(s.product_id))
                            if s.product_id is not None
                            else 0.0
                        )
                        * int(s.quantity_sold or 0)
                    )
                ),
                "fifo_margin_actual": (
                    _safe_float(actual_econ_by_sale_id.get(int(s.id), {}).get("net_before_cogs_actual"))
                    - (_safe_float(fifo_unit_cost_by_sale.get(s.id)) * int(s.quantity_sold or 0))
                ),
                "lot_margin_actual": (
                    _safe_float(actual_econ_by_sale_id.get(int(s.id), {}).get("net_before_cogs_actual"))
                    - (
                        _safe_float(
                            lot_weighted_unit_cost_by_product.get(int(s.product_id))
                            if s.product_id is not None
                            else 0.0
                        )
                        * int(s.quantity_sold or 0)
                    )
                ),
                "margin_method_delta": (
                    (
                        _safe_float(s.sold_price)
                        - _safe_float(s.fees)
                        - _safe_float(s.shipping_cost)
                        - (_safe_float(fifo_unit_cost_by_sale.get(s.id)) * int(s.quantity_sold or 0))
                    )
                    - (
                        _safe_float(s.sold_price)
                        - _safe_float(s.fees)
                        - _safe_float(s.shipping_cost)
                        - (
                            _safe_float(
                                lot_weighted_unit_cost_by_product.get(int(s.product_id))
                                if s.product_id is not None
                                else 0.0
                            )
                            * int(s.quantity_sold or 0)
                        )
                    )
                ),
            }
            for s in sales
        ]
    )

    margin_by_sku_df = (
        cogs_margin_df.groupby(["sku", "product_title", "marketplace"], dropna=False, as_index=False)
        .agg(
            quantity=("quantity", "sum"),
            gross_sales=("gross_sales", "sum"),
            fees=("fees", "sum"),
            shipping_cost=("shipping_cost", "sum"),
            actual_fee_alloc=("actual_fee_alloc", "sum"),
            actual_shipping_alloc=("actual_shipping_alloc", "sum"),
            actual_net_before_cogs=("actual_net_before_cogs", "sum"),
            fifo_cogs=("fifo_cogs", "sum"),
            lot_cogs=("lot_cogs", "sum"),
            fifo_margin=("fifo_margin", "sum"),
            lot_margin=("lot_margin", "sum"),
            fifo_margin_actual=("fifo_margin_actual", "sum"),
            lot_margin_actual=("lot_margin_actual", "sum"),
        )
        if not cogs_margin_df.empty
        else pd.DataFrame()
    )
    if not margin_by_sku_df.empty:
        margin_by_sku_df = _add_margin_pct_columns(margin_by_sku_df)

    margin_by_channel_df = (
        cogs_margin_df.groupby(["marketplace"], dropna=False, as_index=False)
        .agg(
            quantity=("quantity", "sum"),
            gross_sales=("gross_sales", "sum"),
            fees=("fees", "sum"),
            shipping_cost=("shipping_cost", "sum"),
            actual_fee_alloc=("actual_fee_alloc", "sum"),
            actual_shipping_alloc=("actual_shipping_alloc", "sum"),
            actual_net_before_cogs=("actual_net_before_cogs", "sum"),
            fifo_cogs=("fifo_cogs", "sum"),
            lot_cogs=("lot_cogs", "sum"),
            fifo_margin=("fifo_margin", "sum"),
            lot_margin=("lot_margin", "sum"),
            fifo_margin_actual=("fifo_margin_actual", "sum"),
            lot_margin_actual=("lot_margin_actual", "sum"),
        )
        if not cogs_margin_df.empty
        else pd.DataFrame()
    )
    if not margin_by_channel_df.empty:
        margin_by_channel_df = _add_margin_pct_columns(margin_by_channel_df)

    margin_by_period_df = pd.DataFrame()
    if not cogs_margin_df.empty:
        period_df = cogs_margin_df.copy()
        period_df["period_month"] = period_df["sold_at"].fillna("").astype(str).str.slice(0, 7)
        margin_by_period_df = (
            period_df.groupby(["period_month", "marketplace"], dropna=False, as_index=False)
            .agg(
                quantity=("quantity", "sum"),
                gross_sales=("gross_sales", "sum"),
                fees=("fees", "sum"),
                shipping_cost=("shipping_cost", "sum"),
                actual_fee_alloc=("actual_fee_alloc", "sum"),
                actual_shipping_alloc=("actual_shipping_alloc", "sum"),
                actual_net_before_cogs=("actual_net_before_cogs", "sum"),
                fifo_cogs=("fifo_cogs", "sum"),
                lot_cogs=("lot_cogs", "sum"),
                fifo_margin=("fifo_margin", "sum"),
                lot_margin=("lot_margin", "sum"),
                fifo_margin_actual=("fifo_margin_actual", "sum"),
                lot_margin_actual=("lot_margin_actual", "sum"),
            )
        )
        margin_by_period_df = _add_margin_pct_columns(margin_by_period_df)

    inventory_cycles_df = pd.DataFrame()
    rebuy_cost_trend_df = pd.DataFrame()
    review_activity_df = pd.DataFrame()
    review_summary_df = pd.DataFrame()
    listing_format_outcome_df = pd.DataFrame()
    inventory_cycle_summary_df = pd.DataFrame()
    ebay_fee_reconciliation_df = pd.DataFrame()
    ebay_fee_reconciliation_by_marketplace_df = pd.DataFrame()
    ebay_fee_actual_source_df = pd.DataFrame()
    ebay_fee_source_priority_df = pd.DataFrame()
    ebay_fee_source_priority_trend_df = pd.DataFrame()
    fee_reconciliation_summary: dict[str, float | int] = {}
    fee_source_counts: dict[str, int] = {}
    if load_inventory_cycle_analytics:
        inventory_cycles_df = pd.DataFrame(
            repo.report_inventory_cycle_rows(end_dt=end_dt)
            if supports_cycle_rollup
            else _build_inventory_cycle_rows(
                products=_get_all_products(),
                movements=_get_all_movements(),
                sales=_get_all_sales(),
            )
        )
        inventory_cycle_summary_df = pd.DataFrame(
            _build_inventory_cycle_summary_rows(
                inventory_cycles_df.to_dict("records")
                if not inventory_cycles_df.empty
                else []
            )
        )
        rebuy_cost_trend_df = pd.DataFrame(
            repo.report_rebuy_cost_trend_rows(end_dt=end_dt)
            if supports_rebuy_rollup
            else _build_rebuy_cost_trend_rows(
                products=_get_all_products(),
                assignments=_get_all_assignments(),
                movements=_get_all_movements(),
            )
        )
    if load_extended_analytics:
        review_activity_df = pd.DataFrame(
            repo.report_listing_review_activity_rows(
                start_dt=start_dt,
                end_dt=end_dt,
            )
            if hasattr(repo, "report_listing_review_activity_rows")
            else _build_listing_review_activity_rows(
                listings=_get_all_listings(),
                start_dt=start_dt,
                end_dt=end_dt,
            )
        )
        if not review_activity_df.empty:
            review_summary_df = (
                review_activity_df.groupby(
                    ["reviewed_by", "marketplace", "review_decision"],
                    dropna=False,
                    as_index=False,
                )
                .size()
                .rename(columns={"size": "review_events"})
                .sort_values(["review_events"], ascending=[False])
            )
        listing_format_outcome_df = pd.DataFrame(
            repo.report_listing_format_outcome_rows(
                start_dt=start_dt,
                end_dt=end_dt,
            )
            if hasattr(repo, "report_listing_format_outcome_rows")
            else _build_listing_format_outcome_rows(
                listings=_get_all_listings(),
                start_dt=start_dt,
                end_dt=end_dt,
            )
        )
        if hasattr(repo, "report_ebay_fee_reconciliation_rows"):
            ebay_fee_reconciliation_df = pd.DataFrame(
                repo.report_ebay_fee_reconciliation_rows(
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
            )
        else:
            ebay_fee_reconciliation_df = pd.DataFrame(build_ebay_fee_reconciliation_rows(sales))
        if not ebay_fee_reconciliation_df.empty:
            fee_reconciliation_summary = _summarize_fee_reconciliation(ebay_fee_reconciliation_df)
            ebay_fee_reconciliation_by_marketplace_df = (
                ebay_fee_reconciliation_df.groupby(["fee_estimate_present"], dropna=False, as_index=False)
                .agg(
                    sales_count=("sale_id", "count"),
                    gross_sales=("sale_gross", "sum"),
                    actual_fee=("actual_fee", "sum"),
                    estimated_fee_scaled=("estimated_fee_scaled", "sum"),
                    variance_actual_minus_estimate=("variance_actual_minus_estimate", "sum"),
                )
            )
            ebay_fee_reconciliation_by_marketplace_df["variance_pct_of_estimate"] = ebay_fee_reconciliation_by_marketplace_df.apply(
                lambda row: (
                    (float(row["variance_actual_minus_estimate"]) / float(row["estimated_fee_scaled"]) * 100.0)
                    if float(row["estimated_fee_scaled"] or 0.0) > 0
                    else 0.0
                ),
                axis=1,
            )
            ebay_fee_reconciliation_by_marketplace_df["estimate_bucket"] = ebay_fee_reconciliation_by_marketplace_df[
                "fee_estimate_present"
            ].map({True: "estimate_present", False: "estimate_missing"})
            ebay_fee_reconciliation_by_marketplace_df = ebay_fee_reconciliation_by_marketplace_df[
                [
                    "estimate_bucket",
                    "sales_count",
                    "gross_sales",
                    "actual_fee",
                    "estimated_fee_scaled",
                    "variance_actual_minus_estimate",
                    "variance_pct_of_estimate",
                ]
            ]
            ebay_fee_actual_source_df = (
                ebay_fee_reconciliation_df.groupby(["actual_fee_source"], dropna=False, as_index=False)
                .agg(
                    sales_count=("sale_id", "count"),
                    actual_fee=("actual_fee", "sum"),
                    estimated_fee_scaled=("estimated_fee_scaled", "sum"),
                    variance_actual_minus_estimate=("variance_actual_minus_estimate", "sum"),
                )
                .sort_values(["sales_count"], ascending=[False])
            )
            ebay_fee_source_priority_df = _build_fee_source_priority_summary(ebay_fee_reconciliation_df)
            fee_source_counts = _fee_source_priority_counts(ebay_fee_source_priority_df)
            ebay_fee_source_priority_trend_df = _build_fee_source_priority_trend(ebay_fee_reconciliation_df)

    report_scalar_cache = {
        "shipping_rows": int(len(shipping_economics_df)),
        "shipping_total_charged": (
            float(shipping_economics_df["shipping_charged_to_buyer"].sum())
            if not shipping_economics_df.empty
            else 0.0
        ),
        "shipping_total_label_spend": (
            float(shipping_economics_df["shipping_label_spend"].sum())
            if not shipping_economics_df.empty
            else 0.0
        ),
        "shipping_label_covered_count": (
            int((shipping_economics_df["shipping_label_spend"] > 0).sum())
            if not shipping_economics_df.empty
            else 0
        ),
        "cogs_gross_sales_total": (
            float(cogs_margin_df["gross_sales"].sum()) if not cogs_margin_df.empty else 0.0
        ),
        "cogs_fifo_margin_total": (
            float(cogs_margin_df["fifo_margin"].sum()) if not cogs_margin_df.empty else 0.0
        ),
        "cogs_lot_margin_total": (
            float(cogs_margin_df["lot_margin"].sum()) if not cogs_margin_df.empty else 0.0
        ),
        "cogs_negative_fifo_rows": (
            int((cogs_margin_df["fifo_margin"] < 0).sum()) if not cogs_margin_df.empty else 0
        ),
        "reconcile_flags": (
            int(reconciliation_df["reconcile_flag"].sum()) if not reconciliation_df.empty else 0
        ),
        "table_row_counts": {
            "sales": int(len(sales_df)),
            "tax_summary": int(len(tax_summary_df)),
            "tax_by_marketplace": int(len(tax_by_marketplace_df)),
            "tax_detail": int(len(tax_detail_df)),
            "inventory": int(len(inventory_df)),
            "listings": int(len(listings_df)),
            "orders": int(len(orders_df)),
            "order_items": int(len(order_items_df)),
            "returns": int(len(returns_df)),
            "movements": int(len(movements_df)),
            "reconciliation": int(len(reconciliation_df)),
            "accounting_validation": int(len(accounting_validation_df)),
        },
    }

    st.markdown("### eBay Fee Reconciliation")
    if not load_extended_analytics:
        st.info("Enable `Load Extended Analytics` to run fee reconciliation and calibration.")
    elif ebay_fee_reconciliation_df.empty:
        st.info("No eBay sales in the selected date range for fee reconciliation.")
    else:
        er1, er2, er3, er4 = st.columns(4)
        sales_count = int(fee_reconciliation_summary.get("sales_count") or len(ebay_fee_reconciliation_df))
        total_actual_fee = float(fee_reconciliation_summary.get("total_actual_fee") or 0.0)
        total_estimated_fee = float(fee_reconciliation_summary.get("total_estimated_fee") or 0.0)
        total_variance = float(fee_reconciliation_summary.get("total_variance") or 0.0)
        coverage_count = int(fee_reconciliation_summary.get("estimate_coverage_count") or 0)
        er1.metric("eBay Sales", f"{sales_count}")
        er2.metric("Actual Fees", f"${total_actual_fee:,.2f}")
        er3.metric("Estimated Fees", f"${total_estimated_fee:,.2f}")
        er4.metric("Variance", f"${total_variance:,.2f}")
        st.caption(
            f"Estimate coverage: {coverage_count}/{sales_count} sales have listing-time fee estimates."
        )
        if not ebay_fee_source_priority_df.empty:
            st.markdown("#### Actual Fee Source Priority")
            p1, p2, p3 = st.columns(3)
            p1.metric(
                "Normalized Source Rows",
                f"{int(fee_source_counts.get('normalized_source_rows') or 0)}",
            )
            p2.metric(
                "Notes Fallback Rows",
                f"{int(fee_source_counts.get('notes_fallback_rows') or 0)}",
            )
            p3.metric(
                "Sale Field Fallback Rows",
                f"{int(fee_source_counts.get('sale_field_fallback_rows') or 0)}",
            )
            with st.expander("Actual Fee Source Priority Breakdown", expanded=False):
                _render_df_with_preview(ebay_fee_source_priority_df, hide_index=True)
        if not ebay_fee_source_priority_trend_df.empty:
            weekly_coverage_df = _build_normalized_source_weekly_coverage(ebay_fee_source_priority_trend_df)
            if not weekly_coverage_df.empty:
                st.markdown("#### Normalized Source Coverage Trend (Weekly)")
                chart_df = weekly_coverage_df[["bucket_date", "normalized_coverage_pct"]].copy()
                chart_df = chart_df.rename(columns={"bucket_date": "week_start"})
                chart_df["week_start"] = pd.to_datetime(chart_df["week_start"], errors="coerce")
                chart_df = chart_df.set_index("week_start").sort_index()
                st.line_chart(chart_df, y="normalized_coverage_pct", use_container_width=True)
                with st.expander("Normalized Source Coverage Data (Weekly)", expanded=False):
                    _render_df_with_preview(weekly_coverage_df, hide_index=True)
                stacked_weekly_df = _build_weekly_fee_source_count_chart_data(ebay_fee_source_priority_trend_df)
                if not stacked_weekly_df.empty:
                    st.markdown("#### Fee Source Counts by Week")
                    stack_chart_df = stacked_weekly_df.copy()
                    stack_chart_df["week_start"] = pd.to_datetime(stack_chart_df["week_start"], errors="coerce")
                    stack_chart_df = stack_chart_df.set_index("week_start").sort_index()
                    st.bar_chart(
                        stack_chart_df[["normalized_source", "notes_fallback", "sale_field_fallback"]],
                        use_container_width=True,
                    )
                    with st.expander("Fee Source Counts by Week Data", expanded=False):
                        _render_df_with_preview(stacked_weekly_df, hide_index=True)
        if not ebay_fee_actual_source_df.empty:
            with st.expander("Actual Fee Source Breakdown", expanded=False):
                _render_df_with_preview(ebay_fee_actual_source_df, hide_index=True)
        current_final_value_rate = float(
            get_runtime_float(repo, "ebay_fee_estimate_final_value_rate_percent", 13.25)
        )
        calibration = build_final_value_rate_calibration(
            ebay_fee_reconciliation_df.to_dict(orient="records"),
            current_final_value_rate_percent=current_final_value_rate,
        )
        st.markdown("#### Fee Calibration Assist")
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Calibration Samples", f"{int(calibration.get('sample_count') or 0)}")
        cc2.metric("Current Final Value %", f"{current_final_value_rate:.3f}%")
        cc3.metric(
            "Suggested Final Value %",
            f"{float(calibration.get('suggested_final_value_rate_percent') or current_final_value_rate):.3f}%",
        )
        cc4.metric(
            "Suggested Delta",
            f"{float(calibration.get('delta_percent') or 0.0):+.3f}%",
        )
        st.caption(
            "Suggestion is based on implied final-value rates from recent eBay sales with estimate metadata. "
            "Use as calibration guidance, not an automatic accounting source-of-truth."
        )
        can_apply_calibration = has_permission(user.role, "manage_settings")
        apply_col1, apply_col2 = st.columns([1, 3])
        with apply_col1:
            if st.button(
                "Apply Suggested Final Value %",
                key="reports_apply_fee_calibration_btn",
                disabled=not can_apply_calibration
                or int(calibration.get("sample_count") or 0) <= 0,
            ):
                try:
                    suggested_value = float(
                        calibration.get("suggested_final_value_rate_percent")
                        or current_final_value_rate
                    )
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_fee_estimate_final_value_rate_percent",
                        value=f"{suggested_value:.4f}",
                        value_type="float",
                        description="Calibrated from Reports fee reconciliation assist.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success(
                        f"Updated `ebay_fee_estimate_final_value_rate_percent` to {suggested_value:.4f}%."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply fee calibration setting: {exc}")
        with apply_col2:
            if not can_apply_calibration:
                    st.caption("You need `manage_settings` permission to apply runtime calibration values.")

    st.markdown("### Shipping Economics (Charged vs Label Spend)")
    if not load_shipping_tax_analytics:
        st.caption(
            "Shipping economics is deferred. Enable `Load Shipping + Tax Analytics (slower)` to compute this section."
        )
    elif shipping_economics_df.empty:
        st.info("No sales in selected range with shipping economics data.")
    else:
        se1, se2, se3, se4 = st.columns(4)
        total_shipping_charged = float(report_scalar_cache.get("shipping_total_charged") or 0.0)
        total_label_spend = float(report_scalar_cache.get("shipping_total_label_spend") or 0.0)
        total_shipping_delta = total_shipping_charged - total_label_spend
        label_spend_covered_count = int(report_scalar_cache.get("shipping_label_covered_count") or 0)
        shipping_rows_count = int(report_scalar_cache.get("shipping_rows") or 0)
        se1.metric("Sales Rows", f"{shipping_rows_count}")
        se2.metric("Shipping Charged", f"${total_shipping_charged:,.2f}")
        se3.metric("Label Spend", f"${total_label_spend:,.2f}")
        se4.metric("Delta (Charged-Spend)", f"${total_shipping_delta:,.2f}")
        st.caption(
            f"Label-spend coverage: {label_spend_covered_count}/{shipping_rows_count} sales rows."
        )
        if not shipping_econ_summary_df.empty:
            _render_df_with_preview(shipping_econ_summary_df, hide_index=True)

    st.markdown("### Economics Intelligence Drilldowns + Alerts")
    if economics_intel_df.empty:
        st.info("No estimate-vs-actual economics rows in selected date range.")
    else:
        ed1, ed2, ed3 = st.columns(3)
        with ed1:
            economics_min_margin_alert_pct = float(
                st.number_input(
                    "Min Actual Margin % Alert",
                    min_value=-100.0,
                    max_value=100.0,
                    value=5.0,
                    step=0.5,
                    key="reports_econ_min_margin_alert_pct",
                )
            )
        with ed2:
            economics_max_fee_variance_alert_usd = float(
                st.number_input(
                    "Max Avg Fee Variance Alert ($)",
                    min_value=0.0,
                    value=3.0,
                    step=0.25,
                    key="reports_econ_max_fee_variance_alert_usd",
                )
            )
        with ed3:
            economics_min_group_sales_for_alert = int(
                st.number_input(
                    "Min Sales per Group for Alert",
                    min_value=1,
                    value=3,
                    step=1,
                    key="reports_econ_min_group_sales_for_alert",
                )
            )
        economics_drilldowns = _build_economics_intelligence_drilldowns(
            economics_intel_df,
            min_margin_alert_pct=float(economics_min_margin_alert_pct),
            max_fee_variance_alert_usd=float(economics_max_fee_variance_alert_usd),
            min_group_sales_for_alert=int(economics_min_group_sales_for_alert),
        )
        economics_intel_by_sku_df = economics_drilldowns.get("by_sku", pd.DataFrame())
        economics_intel_by_marketplace_df = economics_drilldowns.get("by_marketplace", pd.DataFrame())
        economics_intel_alerts_df = economics_drilldowns.get("alerts", pd.DataFrame())

        total_groups = int(len(economics_intel_by_sku_df))
        sku_alert_groups = (
            int(economics_intel_by_sku_df["alert_any"].astype(bool).sum())
            if (not economics_intel_by_sku_df.empty and "alert_any" in economics_intel_by_sku_df.columns)
            else 0
        )
        channel_alert_groups = (
            int(economics_intel_by_marketplace_df["alert_any"].astype(bool).sum())
            if (not economics_intel_by_marketplace_df.empty and "alert_any" in economics_intel_by_marketplace_df.columns)
            else 0
        )
        alert_rows = int(len(economics_intel_alerts_df))
        ea1, ea2, ea3, ea4 = st.columns(4)
        ea1.metric("SKU Groups", total_groups)
        ea2.metric("SKU Alert Groups", sku_alert_groups)
        ea3.metric("Marketplace Alert Groups", channel_alert_groups)
        ea4.metric("Alert Sale Rows", alert_rows)

        with st.expander("Economics by SKU", expanded=False):
            _render_df_with_preview(economics_intel_by_sku_df, hide_index=True)
        with st.expander("Economics by Marketplace", expanded=False):
            _render_df_with_preview(economics_intel_by_marketplace_df, hide_index=True)
        with st.expander("Economics Alert Rows", expanded=False):
            _render_df_with_preview(economics_intel_alerts_df, hide_index=True)

    st.markdown("### Purchase Document -> Lot Apply Audit")
    load_purchase_doc_apply_audit = st.checkbox(
        "Load Purchase-Document Lot-Apply Audit (slower)",
        value=False,
        key="reports_load_purchase_doc_lot_apply_audit",
    )
    if not load_purchase_doc_apply_audit:
        st.caption(
            "Audit table is deferred. Enable `Load Purchase-Document Lot-Apply Audit (slower)` to query events."
        )
    else:
        purchase_doc_audit_limit = int(
            st.number_input(
                "Purchase-Document Audit Lookback Rows",
                min_value=100,
                max_value=5000,
                value=1000,
                step=100,
                key="reports_purchase_doc_apply_audit_limit",
            )
        )
        audit_rows = repo.list_audit_logs(limit=purchase_doc_audit_limit)
        event_rows: list[dict] = []
        for row in audit_rows:
            if str(getattr(row, "entity_type", "") or "").strip().lower() != "purchase_document":
                continue
            action = str(getattr(row, "action", "") or "").strip().lower()
            if action not in {"auto_apply_extracted_fields_to_lot", "manual_apply_extracted_fields_to_lot"}:
                continue
            created_at = getattr(row, "created_at", None)
            if created_at is None:
                continue
            if created_at < start_dt or created_at > end_dt:
                continue
            payload: dict = {}
            try:
                parsed = json.loads(str(getattr(row, "changes_json", "") or "{}"))
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
            applied_fields_raw = payload.get("applied_fields")
            if isinstance(applied_fields_raw, list):
                applied_fields = [str(v).strip() for v in applied_fields_raw if str(v).strip()]
            else:
                applied_fields = []
            event_rows.append(
                {
                    "created_at": created_at.isoformat(),
                    "actor": str(getattr(row, "actor", "") or "").strip(),
                    "action": action,
                    "mode": str(payload.get("mode") or "").strip().lower(),
                    "workflow": str(payload.get("workflow") or "").strip(),
                    "purchase_document_id": int(getattr(row, "entity_id", 0) or 0),
                    "lot_id": int(payload.get("lot_id") or 0) if payload.get("lot_id") is not None else 0,
                    "applied_field_count": int(len(applied_fields)),
                    "applied_fields": ", ".join(applied_fields),
                }
            )
        if not event_rows:
            st.info("No purchase-document lot-apply audit events found in selected date range.")
        else:
            events_df = pd.DataFrame(event_rows).sort_values("created_at", ascending=False)
            auto_count = int(
                (events_df["action"].astype(str) == "auto_apply_extracted_fields_to_lot").sum()
            )
            manual_count = int(
                (events_df["action"].astype(str) == "manual_apply_extracted_fields_to_lot").sum()
            )
            pd1, pd2, pd3, pd4 = st.columns(4)
            pd1.metric("Events", int(len(events_df)))
            pd2.metric("Auto", auto_count)
            pd3.metric("Manual", manual_count)
            pd4.metric("Distinct Lots", int(events_df["lot_id"].replace(0, pd.NA).dropna().nunique()))
            _render_df_with_preview(events_df, hide_index=True)
            st.download_button(
                "Download Purchase-Document Lot-Apply Audit CSV",
                data=events_df.to_csv(index=False).encode("utf-8"),
                file_name=f"purchase_document_lot_apply_audit_{settings.app_env}.csv",
                mime="text/csv",
                key="reports_purchase_doc_lot_apply_audit_download",
            )

    st.markdown("### Tax Drilldown")
    if not load_shipping_tax_analytics:
        st.caption(
            "Tax drilldown is deferred. Enable `Load Shipping + Tax Analytics (slower)` to compute this section."
        )
    elif tax_detail_df.empty:
        st.info("No tax detail rows in selected date range/scope.")
    else:
        drill_marketplace_options = ["all"] + sorted(
            {
                str(v).strip().lower()
                for v in tax_detail_df["marketplace"].dropna().unique().tolist()
                if str(v).strip()
            }
        )
        td1, td2 = st.columns(2)
        with td1:
            drill_marketplace = st.selectbox(
                "Drilldown Marketplace",
                options=drill_marketplace_options,
                index=0,
                key="reports_tax_drill_marketplace",
            )
        with td2:
            drill_taxability = st.selectbox(
                "Drilldown Segment",
                options=["all", "taxable_only", "exempt_only"],
                index=0,
                key="reports_tax_drill_taxability",
            )
        filtered_tax_detail = _filter_tax_drilldown_rows(
            tax_detail_df,
            marketplace=drill_marketplace,
            taxability=drill_taxability,
        )
        tax_drill_kpis = _tax_drilldown_kpis(filtered_tax_detail)
        dt1, dt2, dt3 = st.columns(3)
        dt1.metric("Rows", int(tax_drill_kpis.get("rows") or 0))
        dt2.metric(
            "Taxable Subtotal",
            f"${float(tax_drill_kpis.get('taxable_subtotal') or 0.0):,.2f}",
        )
        dt3.metric(
            "Estimated Tax",
            f"${float(tax_drill_kpis.get('estimated_tax') or 0.0):,.2f}",
        )
        sale_option_rows = _build_tax_drilldown_sale_option_rows(filtered_tax_detail)
        sale_option_map = {str(row.get("label") or ""): int(row.get("sale_id") or 0) for row in sale_option_rows}
        if sale_option_map:
            hx1, hx2 = st.columns([3, 1])
            with hx1:
                selected_sale_label = st.selectbox(
                    "Create Invoice From Sale",
                    options=list(sale_option_map.keys()),
                    key="reports_tax_drill_sale_pick",
                )
            with hx2:
                if st.button("Open in Documents", key="reports_tax_drill_to_documents_btn"):
                    handoff_to_documents_draft(
                        source_type="Sale",
                        source_id=int(sale_option_map[selected_sale_label]),
                        doc_type="invoice",
                        handoff_from="reports_tax_drilldown",
                        tax_jurisdiction=str(tax_jurisdiction or "").strip(),
                        tax_rate_percent=float(tax_rate_percent or 0.0),
                        tax_shipping_taxable=bool(tax_shipping_taxable),
                        repo=repo,
                        actor=user.username,
                    )
        _render_df_with_preview(filtered_tax_detail)
        dx1, dx2 = st.columns(2)
        with dx1:
            st.download_button(
                label="Download Tax Drilldown CSV",
                data=filtered_tax_detail.to_csv(index=False).encode("utf-8"),
                file_name=f"tax_drilldown_{from_date}_{to_date}.csv",
                mime="text/csv",
                disabled=filtered_tax_detail.empty,
            )
        with dx2:
            st.download_button(
                label="Download Tax Drilldown XLSX",
                data=dataframe_to_xlsx_bytes(filtered_tax_detail, sheet_name="tax_drilldown"),
                file_name=f"tax_drilldown_{from_date}_{to_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                disabled=filtered_tax_detail.empty,
            )

    st.markdown("### Document Draft Handoff")
    st.caption("Open Documents with prefilled source context from either a sale or an order.")
    h1, h2, h3 = st.columns([1, 2, 1])
    with h1:
        handoff_source_type = st.selectbox(
            "Source Type",
            options=["Sale", "Order"],
            index=0,
            key="reports_documents_handoff_source_type",
        )
    with h2:
        handoff_doc_type = st.selectbox(
            "Document Type",
            options=["invoice", "receipt"],
            index=0,
            key="reports_documents_handoff_doc_type",
        )
    source_option_map: dict[str, int] = {}
    if handoff_source_type == "Sale":
        source_option_rows = _build_documents_handoff_sale_option_rows(sales_df)
        source_option_map = {
            str(row.get("label") or ""): int(row.get("source_id") or 0)
            for row in source_option_rows
        }
    else:
        source_option_rows = _build_documents_handoff_order_option_rows(orders_df)
        source_option_map = {
            str(row.get("label") or ""): int(row.get("source_id") or 0)
            for row in source_option_rows
        }
    if source_option_map:
        selected_source_label = st.selectbox(
            "Select Source",
            options=list(source_option_map.keys()),
            key="reports_documents_handoff_source_pick",
        )
        with h3:
            if st.button(
                "Open in Documents",
                key="reports_documents_handoff_open_btn",
            ):
                source_id = source_option_map.get(str(selected_source_label or ""))
                if source_id:
                    handoff_to_documents_draft(
                        source_type=handoff_source_type,
                        source_id=int(source_id),
                        doc_type=handoff_doc_type,
                        handoff_from="reports_documents_handoff",
                        tax_jurisdiction=str(tax_jurisdiction or "").strip(),
                        tax_rate_percent=float(tax_rate_percent or 0.0),
                        tax_shipping_taxable=bool(tax_shipping_taxable),
                        repo=repo,
                        actor=user.username,
                    )
    else:
        st.info(f"No {handoff_source_type.lower()} records in selected date range.")

    st.markdown("### Rebuy Cost Trend (Weighted/Lot)")
    if not load_inventory_cycle_analytics:
        st.caption("Enable `Load Inventory Cycle + Rebuy Analytics` to run cycle and rebuy trend reports.")
    elif rebuy_cost_trend_df.empty:
        st.info("No acquisition events with unit cost found for trend analysis.")
    else:
        sku_options = sorted({str(v) for v in rebuy_cost_trend_df["sku"].dropna().unique() if str(v).strip()})
        selected_sku = st.selectbox(
            "Trend SKU",
            options=sku_options,
            index=0,
            key="reports_rebuy_cost_trend_sku",
        )
        sku_rows = rebuy_cost_trend_df[rebuy_cost_trend_df["sku"] == selected_sku].copy()
        if sku_rows.empty:
            st.info("No rows for selected SKU.")
        else:
            chart_df = sku_rows[["as_of", "weighted_unit_cost", "unit_cost"]].copy()
            chart_df = chart_df.rename(
                columns={"weighted_unit_cost": "weighted_unit_cost_running", "unit_cost": "event_unit_cost"}
            )
            chart_df = chart_df.set_index("as_of")
            st.line_chart(chart_df, use_container_width=True)
            _render_df_with_preview(sku_rows)

    st.markdown("### Reports Copilot")
    st.caption("AI narrative summary for margin anomalies, reconciliation risk, and export recommendations.")
    if not load_extended_analytics:
        st.caption("Enable `Load Extended Analytics` to run Reports Copilot on full fee/margin context.")
    if st.button(
        "Analyze Report Snapshot",
        key="reports_copilot_analyze_btn",
        disabled=not load_extended_analytics,
    ):
        if not ensure_permission(user, "ai_comp_use", "Use Reports Copilot"):
            st.stop()
        try:
            context = {
                "date_range": {"from": from_date.isoformat(), "to": to_date.isoformat()},
                "table_row_counts": dict(report_scalar_cache.get("table_row_counts") or {}),
                "margin_snapshot": {
                    "gross_sales_total": float(report_scalar_cache.get("cogs_gross_sales_total") or 0.0),
                    "fifo_margin_total": float(report_scalar_cache.get("cogs_fifo_margin_total") or 0.0),
                    "lot_margin_total": float(report_scalar_cache.get("cogs_lot_margin_total") or 0.0),
                    "negative_fifo_margin_rows": int(report_scalar_cache.get("cogs_negative_fifo_rows") or 0),
                },
                "reconciliation_flags": int(report_scalar_cache.get("reconcile_flags") or 0),
                "top_negative_fifo_margin_rows": _top_n_records(
                    cogs_margin_df,
                    sort_by="fifo_margin",
                    ascending=True,
                    n=10,
                ),
                "top_margin_by_sku_rows": _top_n_records(
                    margin_by_sku_df,
                    sort_by="fifo_margin",
                    ascending=False,
                    n=10,
                ),
                "marketplace_reconciliation_rows": _top_n_by_abs_records(
                    reconciliation_df,
                    value_col="delta_order_total_vs_sales_gross",
                    n=10,
                ),
                "validation_issue_rows": (
                    accounting_validation_df.head(20).to_dict("records")
                    if not accounting_validation_df.empty
                    else []
                ),
            }
            result = execute_comp_summary(
                repo,
                query="Reports narrative summary and export recommendations",
                ebay_rows=[],
                web_rows=[],
                spot_context=context,
                system_message=get_runtime_str(
                    repo,
                    "comp_llm_system_message",
                    "You are an accounting and operations reporting copilot.",
                ).strip(),
                instruction=(
                    "Return ONLY JSON with keys: `executive_summary`, `margin_anomalies`, "
                    "`reconciliation_findings`, `recommended_exports`, `next_actions`. "
                    "Each key must be an array of concise bullet strings."
                ),
            )
            st.session_state["reports_copilot_raw"] = str(result.text or "").strip()
            st.success("Reports copilot analysis complete.")
            st.rerun()
        except Exception as exc:
            st.error(f"Reports copilot analysis failed: {exc}")

    raw_reports_ai = str(st.session_state.get("reports_copilot_raw") or "").strip()
    if raw_reports_ai:
        with st.expander("Reports Copilot Result", expanded=False):
            st.code(raw_reports_ai, language="json")

    reports = [
        ("Sales Detail", sales_df, "sales_detail"),
        ("Tax Summary (Estimated)", tax_summary_df, "tax_summary_estimated"),
        ("Tax by Marketplace (Estimated)", tax_by_marketplace_df, "tax_by_marketplace_estimated"),
        ("Tax Detail (Estimated)", tax_detail_df, "tax_detail_estimated"),
        ("Inventory Snapshot", inventory_df, "inventory_snapshot"),
        ("Listing Snapshot", listings_df, "listing_snapshot"),
        ("Orders", orders_df, "orders"),
        ("Order Items", order_items_df, "order_items"),
        ("eBay Order Fee Breakdown", ebay_order_fee_breakdown_df, "ebay_order_fee_breakdown"),
        ("Returns", returns_df, "returns"),
        ("Lot Assignment", lots_df, "lot_assignment"),
        ("Inventory Movements", movements_df, "inventory_movements"),
        ("QuickBooks Sales Export", qbo_sales_df, "qbo_sales_export"),
        ("QuickBooks Refund/Adjustment Export", qbo_adjustments_df, "qbo_adjustments_export"),
        ("Reconciliation by Marketplace", reconciliation_df, "reconciliation_marketplace"),
        ("Accounting Validation Flags", accounting_validation_df, "accounting_validation_flags"),
        ("COGS & Margin Detail", cogs_margin_df, "cogs_margin_detail"),
        ("Margin by SKU", margin_by_sku_df, "margin_by_sku"),
        ("Margin by Marketplace", margin_by_channel_df, "margin_by_marketplace"),
        ("Margin by Period", margin_by_period_df, "margin_by_period"),
        ("Inventory Cycle Summary by SKU", inventory_cycle_summary_df, "inventory_cycle_summary"),
        ("Inventory Cycles (Rebuy/Resell)", inventory_cycles_df, "inventory_cycles"),
        ("Rebuy Cost Trend Events", rebuy_cost_trend_df, "rebuy_cost_trend"),
        ("Listing Review Activity", review_activity_df, "listing_review_activity"),
        ("Listing Review Summary", review_summary_df, "listing_review_summary"),
        (
            "Listing Format Intent vs Publish Outcome",
            listing_format_outcome_df,
            "listing_format_intent_vs_outcome",
        ),
        (
            "eBay Fee Estimate vs Actual",
            ebay_fee_reconciliation_df,
            "ebay_fee_estimate_vs_actual",
        ),
        (
            "eBay Fee Reconciliation Summary",
            ebay_fee_reconciliation_by_marketplace_df,
            "ebay_fee_reconciliation_summary",
        ),
        (
            "eBay Fee Actual Source Breakdown",
            ebay_fee_actual_source_df,
            "ebay_fee_actual_source_breakdown",
        ),
        (
            "eBay Fee Source Priority",
            ebay_fee_source_priority_df,
            "ebay_fee_source_priority",
        ),
        (
            "eBay Fee Source Priority Trend",
            ebay_fee_source_priority_trend_df,
            "ebay_fee_source_priority_trend",
        ),
        (
            "eBay Marketplace Fee Detail (Per Order/Line)",
            ebay_marketplace_fee_detail_df,
            "ebay_marketplace_fee_detail",
        ),
        (
            "eBay Marketplace Fee Summary (By Fee Type)",
            ebay_marketplace_fee_summary_df,
            "ebay_marketplace_fee_summary",
        ),
        (
            "eBay Marketplace Fee by SKU",
            ebay_marketplace_fee_by_sku_df,
            "ebay_marketplace_fee_by_sku",
        ),
        (
            "eBay Marketplace Fee by Category",
            ebay_marketplace_fee_by_category_df,
            "ebay_marketplace_fee_by_category",
        ),
        (
            "Shipping Economics Detail",
            shipping_economics_df,
            "shipping_economics_detail",
        ),
        (
            "Shipping Economics Summary",
            shipping_econ_summary_df,
            "shipping_economics_summary",
        ),
        (
            "Order Actual Economics Allocation",
            order_actual_econ_df,
            "order_actual_economics_allocation",
        ),
        (
            "Economics Intelligence Facts (Estimate vs Actual)",
            economics_intel_df,
            "economics_intelligence_facts",
        ),
        (
            "Economics Intelligence by SKU",
            economics_intel_by_sku_df,
            "economics_intelligence_by_sku",
        ),
        (
            "Economics Intelligence by Marketplace",
            economics_intel_by_marketplace_df,
            "economics_intelligence_by_marketplace",
        ),
        (
            "Economics Intelligence Alert Rows",
            economics_intel_alerts_df,
            "economics_intelligence_alert_rows",
        ),
    ]
    for label, df, file_prefix in reports:
        st.markdown(f"### {label}")
        if df.empty:
            st.info("No records for this report in the selected date range.")
            continue

        st.caption(f"Rows: {int(len(df))}")
        _render_df_with_preview(df)
        dl1, dl2 = st.columns(2)
        with dl1:
            st.download_button(
                label=f"Download {label} CSV",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=f"{file_prefix}_{from_date}_{to_date}.csv",
                mime="text/csv",
            )
        with dl2:
            st.download_button(
                label=f"Download {label} XLSX",
                data=dataframe_to_xlsx_bytes(df, sheet_name=file_prefix[:31]),
                file_name=f"{file_prefix}_{from_date}_{to_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="reports",
        section_title="Workspace Feedback: Reports",
    )
