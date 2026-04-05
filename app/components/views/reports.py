from datetime import datetime
from collections import defaultdict, deque
import json

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import iso_or_none
from app.components.views.shared import (
    dataframe_to_xlsx_bytes,
    handoff_to_documents_draft,
    render_help_panel,
)
from app.components.views.workspace_shell import render_workspace_feedback
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.runtime_settings import get_runtime_bool, get_runtime_str
from app.utils.time import utc_today


def _safe_float(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _parse_csv_set(value: str) -> set[str]:
    return {str(part).strip().lower() for part in str(value or "").split(",") if str(part).strip()}


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
        if external_listing_id and listing_state in {"active", "ended"}:
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

    products = [
        p
        for p in repo.list_products()
        if p.acquired_at is None or start_dt <= p.acquired_at <= end_dt
    ]
    listings = [
        l
        for l in repo.list_listings()
        if l.listed_at is None or start_dt <= l.listed_at <= end_dt
    ]
    sales = [
        s
        for s in repo.list_sales()
        if s.sold_at is not None and start_dt <= s.sold_at <= end_dt
    ]
    orders = [
        o
        for o in repo.list_orders()
        if o.sold_at is not None and start_dt <= o.sold_at <= end_dt
    ]
    order_items = [
        oi
        for oi in repo.list_order_items()
        if oi.order is not None and oi.order.sold_at is not None and start_dt <= oi.order.sold_at <= end_dt
    ]
    returns = [
        r
        for r in repo.list_returns()
        if r.returned_at is not None and start_dt <= r.returned_at <= end_dt
    ]
    assignments = [
        a
        for a in repo.list_product_lot_assignments()
        if a.acquired_at is None or start_dt <= a.acquired_at <= end_dt
    ]
    movements = [
        m
        for m in repo.list_inventory_movements(limit=5000)
        if m.occurred_at is None or start_dt <= m.occurred_at <= end_dt
    ]
    all_sales = repo.list_sales()
    all_assignments = repo.list_product_lot_assignments()
    default_unit_cost_by_product = {
        int(p.id): _safe_float(p.acquisition_cost)
        for p in repo.list_products()
    }
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
    tax_default_rate_raw = get_runtime_str(repo, "invoicing_tax_rate_percent_default", "8.81")
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
    if "reports_tax_jurisdiction" not in st.session_state:
        st.session_state["reports_tax_jurisdiction"] = str(tax_default_jurisdiction or "Golden, Colorado")
    if "reports_tax_rate_percent" not in st.session_state:
        st.session_state["reports_tax_rate_percent"] = float(max(0.0, tax_default_rate))
    if "reports_tax_shipping_taxable" not in st.session_state:
        st.session_state["reports_tax_shipping_taxable"] = bool(tax_shipping_taxable_default)
    if "reports_tax_marketplaces" not in st.session_state:
        st.session_state["reports_tax_marketplaces"] = list(sales_marketplace_options)
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

    tax_detail_rows = []
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
                "cogs_input_estimate": _safe_float(s.product.acquisition_cost) * int(s.quantity_sold or 0)
                if s.product is not None
                else 0.0,
                "gross_margin_estimate": (
                    _safe_float(s.sold_price)
                    - _safe_float(s.fees)
                    - _safe_float(s.shipping_cost)
                    - (_safe_float(s.product.acquisition_cost) * int(s.quantity_sold or 0) if s.product else 0.0)
                ),
                "net_amount": float(s.sold_price - s.fees - s.shipping_cost),
                "marketplace": s.marketplace,
            }
            for s in sales
        ]
    )

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
                "total_amount": float(o.total_amount),
                "item_count": len(o.items),
                "notes": o.notes,
            }
            for o in orders
        ]
    )

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
            }
            for r in returns
        ]
    )

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
        mp_returns = [r for r in returns if (r.marketplace or "").strip().lower() == mp]

        sales_gross = sum(_safe_float(s.sold_price) for s in mp_sales)
        sales_fees = sum(_safe_float(s.fees) for s in mp_sales)
        sales_shipping = sum(_safe_float(s.shipping_cost) for s in mp_sales)
        sales_net = sum(_safe_float(s.sold_price) - _safe_float(s.fees) - _safe_float(s.shipping_cost) for s in mp_sales)
        returns_total = sum(
            _safe_float(r.refund_amount) + _safe_float(r.refund_fees) + _safe_float(r.refund_shipping)
            for r in mp_returns
        )
        order_totals = sum(_safe_float(o.total_amount) for o in mp_orders)
        delta = order_totals - sales_gross

        marketplace_rows.append(
            {
                "marketplace": mp,
                "sales_count": len(mp_sales),
                "orders_count": len(mp_orders),
                "returns_count": len(mp_returns),
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
    for r in returns:
        reasons = []
        if r.sale_id is None:
            reasons.append("return_missing_sale_link")
        if (_safe_float(r.refund_amount) + _safe_float(r.refund_fees) + _safe_float(r.refund_shipping)) <= 0:
            reasons.append("return_non_positive_refund_total")
        if reasons:
            validation_rows.append(
                {
                    "entity_type": "return",
                    "entity_id": r.id,
                    "marketplace": r.marketplace,
                    "reference": r.external_return_id or "",
                    "issues": ",".join(reasons),
                    "sold_at": iso_or_none(r.returned_at),
                }
            )
    accounting_validation_df = pd.DataFrame(validation_rows)

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
            fifo_cogs=("fifo_cogs", "sum"),
            lot_cogs=("lot_cogs", "sum"),
            fifo_margin=("fifo_margin", "sum"),
            lot_margin=("lot_margin", "sum"),
        )
        if not cogs_margin_df.empty
        else pd.DataFrame()
    )
    if not margin_by_sku_df.empty:
        margin_by_sku_df["fifo_margin_pct"] = margin_by_sku_df.apply(
            lambda row: (row["fifo_margin"] / row["gross_sales"]) if row["gross_sales"] else 0.0,
            axis=1,
        )
        margin_by_sku_df["lot_margin_pct"] = margin_by_sku_df.apply(
            lambda row: (row["lot_margin"] / row["gross_sales"]) if row["gross_sales"] else 0.0,
            axis=1,
        )

    margin_by_channel_df = (
        cogs_margin_df.groupby(["marketplace"], dropna=False, as_index=False)
        .agg(
            quantity=("quantity", "sum"),
            gross_sales=("gross_sales", "sum"),
            fees=("fees", "sum"),
            shipping_cost=("shipping_cost", "sum"),
            fifo_cogs=("fifo_cogs", "sum"),
            lot_cogs=("lot_cogs", "sum"),
            fifo_margin=("fifo_margin", "sum"),
            lot_margin=("lot_margin", "sum"),
        )
        if not cogs_margin_df.empty
        else pd.DataFrame()
    )
    if not margin_by_channel_df.empty:
        margin_by_channel_df["fifo_margin_pct"] = margin_by_channel_df.apply(
            lambda row: (row["fifo_margin"] / row["gross_sales"]) if row["gross_sales"] else 0.0,
            axis=1,
        )
        margin_by_channel_df["lot_margin_pct"] = margin_by_channel_df.apply(
            lambda row: (row["lot_margin"] / row["gross_sales"]) if row["gross_sales"] else 0.0,
            axis=1,
        )

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
                fifo_cogs=("fifo_cogs", "sum"),
                lot_cogs=("lot_cogs", "sum"),
                fifo_margin=("fifo_margin", "sum"),
                lot_margin=("lot_margin", "sum"),
            )
        )
        margin_by_period_df["fifo_margin_pct"] = margin_by_period_df.apply(
            lambda row: (row["fifo_margin"] / row["gross_sales"]) if row["gross_sales"] else 0.0,
            axis=1,
        )
        margin_by_period_df["lot_margin_pct"] = margin_by_period_df.apply(
            lambda row: (row["lot_margin"] / row["gross_sales"]) if row["gross_sales"] else 0.0,
            axis=1,
        )

    inventory_cycles_df = pd.DataFrame(
        _build_inventory_cycle_rows(
            products=repo.list_products(),
            movements=repo.list_inventory_movements(limit=20000),
            sales=repo.list_sales(),
        )
    )
    rebuy_cost_trend_df = pd.DataFrame(
        _build_rebuy_cost_trend_rows(
            products=repo.list_products(),
            assignments=repo.list_product_lot_assignments(),
            movements=repo.list_inventory_movements(limit=20000),
        )
    )
    review_activity_df = pd.DataFrame(
        _build_listing_review_activity_rows(
            listings=repo.list_listings(),
            start_dt=start_dt,
            end_dt=end_dt,
        )
    )
    review_summary_df = pd.DataFrame()
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
        _build_listing_format_outcome_rows(
            listings=repo.list_listings(),
            start_dt=start_dt,
            end_dt=end_dt,
        )
    )

    st.markdown("### Tax Drilldown")
    if tax_detail_df.empty:
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
        filtered_tax_detail = tax_detail_df.copy()
        if drill_marketplace != "all":
            filtered_tax_detail = filtered_tax_detail[
                filtered_tax_detail["marketplace"].astype(str).str.strip().str.lower() == drill_marketplace
            ]
        if drill_taxability == "taxable_only":
            filtered_tax_detail = filtered_tax_detail[
                filtered_tax_detail["taxable_subtotal"].astype(float) > 0.0
            ]
        elif drill_taxability == "exempt_only":
            filtered_tax_detail = filtered_tax_detail[
                filtered_tax_detail["is_tax_exempt_category"].astype(bool)
            ]
        dt1, dt2, dt3 = st.columns(3)
        dt1.metric("Rows", int(len(filtered_tax_detail)))
        dt2.metric(
            "Taxable Subtotal",
            f"${float(filtered_tax_detail['taxable_subtotal'].sum()) if not filtered_tax_detail.empty else 0.0:,.2f}",
        )
        dt3.metric(
            "Estimated Tax",
            f"${float(filtered_tax_detail['estimated_tax_collected'].sum()) if not filtered_tax_detail.empty else 0.0:,.2f}",
        )
        sale_option_map = {
            (
                f"sale#{int(row.sale_id)} | {str(row.marketplace or '').strip()} | "
                f"{str(row.sku or '').strip()} | tax={float(row.estimated_tax_collected or 0.0):,.2f}"
            ): int(row.sale_id)
            for row in filtered_tax_detail.itertuples(index=False)
            if int(getattr(row, "sale_id", 0) or 0) > 0
        }
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
        st.dataframe(filtered_tax_detail, use_container_width=True)
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
        source_option_map = {
            (
                f"sale#{int(row.sale_id)} | {str(row.sold_at or '')} | "
                f"{str(row.marketplace or '').strip()} | {str(row.sku or '').strip()} | "
                f"gross=${float(row.gross_sales or 0.0):,.2f}"
            ): int(row.sale_id)
            for row in sales_df.itertuples(index=False)
            if int(getattr(row, "sale_id", 0) or 0) > 0
        }
    else:
        source_option_map = {
            (
                f"order#{int(row.order_id)} | {str(row.sold_at or '')} | "
                f"{str(row.marketplace or '').strip()} | ext={str(row.external_order_id or '').strip()} | "
                f"total=${float(row.total_amount or 0.0):,.2f}"
            ): int(row.order_id)
            for row in orders_df.itertuples(index=False)
            if int(getattr(row, "order_id", 0) or 0) > 0
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
    if rebuy_cost_trend_df.empty:
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
            st.dataframe(sku_rows, use_container_width=True)

    st.markdown("### Reports Copilot")
    st.caption("AI narrative summary for margin anomalies, reconciliation risk, and export recommendations.")
    if st.button("Analyze Report Snapshot", key="reports_copilot_analyze_btn"):
        if not ensure_permission(user, "ai_comp_use", "Use Reports Copilot"):
            st.stop()
        try:
            gross_sales_total = float(cogs_margin_df["gross_sales"].sum()) if not cogs_margin_df.empty else 0.0
            fifo_margin_total = float(cogs_margin_df["fifo_margin"].sum()) if not cogs_margin_df.empty else 0.0
            lot_margin_total = float(cogs_margin_df["lot_margin"].sum()) if not cogs_margin_df.empty else 0.0
            neg_fifo_rows = int((cogs_margin_df["fifo_margin"] < 0).sum()) if not cogs_margin_df.empty else 0
            reconcile_flags = int(reconciliation_df["reconcile_flag"].sum()) if not reconciliation_df.empty else 0
            context = {
                "date_range": {"from": from_date.isoformat(), "to": to_date.isoformat()},
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
                "margin_snapshot": {
                    "gross_sales_total": gross_sales_total,
                    "fifo_margin_total": fifo_margin_total,
                    "lot_margin_total": lot_margin_total,
                    "negative_fifo_margin_rows": neg_fifo_rows,
                },
                "reconciliation_flags": reconcile_flags,
                "top_negative_fifo_margin_rows": (
                    cogs_margin_df.sort_values("fifo_margin", ascending=True).head(10).to_dict("records")
                    if not cogs_margin_df.empty
                    else []
                ),
                "top_margin_by_sku_rows": (
                    margin_by_sku_df.sort_values("fifo_margin", ascending=False).head(10).to_dict("records")
                    if not margin_by_sku_df.empty
                    else []
                ),
                "marketplace_reconciliation_rows": (
                    reconciliation_df.assign(
                        _delta_abs=reconciliation_df["delta_order_total_vs_sales_gross"].abs()
                    )
                    .sort_values("_delta_abs", ascending=False)
                    .head(10)
                    .drop(columns=["_delta_abs"])
                    .to_dict("records")
                    if not reconciliation_df.empty
                    else []
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
        ("Inventory Cycles (Rebuy/Resell)", inventory_cycles_df, "inventory_cycles"),
        ("Rebuy Cost Trend Events", rebuy_cost_trend_df, "rebuy_cost_trend"),
        ("Listing Review Activity", review_activity_df, "listing_review_activity"),
        ("Listing Review Summary", review_summary_df, "listing_review_summary"),
        (
            "Listing Format Intent vs Publish Outcome",
            listing_format_outcome_df,
            "listing_format_intent_vs_outcome",
        ),
    ]

    for label, df, file_prefix in reports:
        st.markdown(f"### {label}")
        if df.empty:
            st.info("No records for this report in the selected date range.")
            continue

        st.dataframe(df, use_container_width=True)
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
