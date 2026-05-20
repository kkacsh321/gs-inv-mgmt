from datetime import timedelta
from typing import Any

from app.config import settings
from app.services.ai_accountant_monitor import (
    annotate_ai_accountant_question_rows,
    build_ai_accountant_monitor_rows,
    build_ai_accountant_question_rows,
    list_ai_accountant_answer_followups,
    list_ai_accountant_answers,
    list_ai_accountant_review_outcomes,
)
from app.utils.time import utcnow_naive


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _safe_scan_rows(rows: list[Any], *, max_rows: int) -> tuple[list[Any], bool]:
    if len(rows) <= max_rows:
        return rows, False
    return rows[:max_rows], True


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _rollback_repo_session(repo: Any) -> None:
    db = getattr(repo, "db", None)
    if db is None or not hasattr(db, "rollback"):
        return
    try:
        db.rollback()
    except Exception:
        pass


def _product_default_landed_unit_cost(product: Any) -> float:
    landed = (
        _safe_float(getattr(product, "acquisition_cost", None))
        + _safe_float(getattr(product, "acquisition_tax_paid", None))
        + _safe_float(getattr(product, "acquisition_shipping_paid", None))
        + _safe_float(getattr(product, "acquisition_handling_paid", None))
    )
    if landed > 0:
        return landed
    return _safe_float(getattr(product, "product_cost", None))


def _repository_cost_maps(repo: Any, products: list[Any], *, end_dt) -> dict[str, Any]:
    if not hasattr(repo, "report_sale_unit_cost_maps"):
        return {}
    default_unit_cost_by_product = {
        int(getattr(p, "id")): _product_default_landed_unit_cost(p)
        for p in products
        if getattr(p, "id", None) is not None
    }
    try:
        return dict(
            repo.report_sale_unit_cost_maps(
                end_dt=end_dt,
                default_unit_cost_by_product=default_unit_cost_by_product,
            )
            or {}
        )
    except Exception:
        _rollback_repo_session(repo)
        return {}


def _repository_actual_economics_rows(repo: Any, *, start_dt, end_dt) -> list[dict[str, Any]]:
    if not hasattr(repo, "report_sales_actual_econ_rows"):
        return []
    try:
        rows = repo.report_sales_actual_econ_rows(start_dt=start_dt, end_dt=end_dt) or []
    except Exception:
        _rollback_repo_session(repo)
        return []
    return [dict(row or {}) for row in rows if isinstance(row, dict)]


def build_inventory_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    products, capped = _safe_scan_rows(repo.list_products(), max_rows=max_scan_rows)
    on_hand = [p for p in products if int(p.current_quantity or 0) > 0]
    total_units = sum(int(p.current_quantity or 0) for p in on_hand)
    cost_maps = _repository_cost_maps(repo, products, end_dt=utcnow_naive())
    fifo_remaining_unit_cost_by_product = dict(cost_maps.get("fifo_remaining_unit_cost_by_product") or {})
    lot_weighted_unit_cost_by_product = dict(cost_maps.get("lot_weighted_unit_cost_by_product") or {})

    def _inventory_unit_cost(product: Any) -> float:
        product_id = int(getattr(product, "id", 0) or 0)
        return _safe_float(
            fifo_remaining_unit_cost_by_product.get(
                product_id,
                lot_weighted_unit_cost_by_product.get(
                    product_id,
                    _product_default_landed_unit_cost(product),
                ),
            )
        )

    inventory_value = sum(_inventory_unit_cost(p) * int(p.current_quantity or 0) for p in on_hand)
    top_on_hand = sorted(
        [
            {
                "sku": p.sku,
                "title": p.title,
                "qty": int(p.current_quantity or 0),
                "unit_cost": _inventory_unit_cost(p),
            }
            for p in on_hand
        ],
        key=lambda x: x["qty"],
        reverse=True,
    )[:5]
    lines = [
        f"Inventory snapshot: `{len(on_hand)}` SKUs with stock, `{total_units}` total units on hand.",
        f"Estimated inventory cost basis: `{_money(inventory_value)}`.",
    ]
    if top_on_hand:
        lines.append("Top on-hand SKUs:")
        for row in top_on_hand:
            lines.append(f"- `{row['sku']}` qty `{row['qty']}` @ {_money(row['unit_cost'])}")
    citations = [
        {
            "table": "products",
            "filters": "current_quantity > 0",
            "rows_considered": len(on_hand),
            "capped": bool(capped),
            "cost_basis": (
                "FIFO remaining lot cost, lot-weighted cost, product landed acquisition cost, product_cost fallback"
                if cost_maps
                else "product landed acquisition cost / product_cost fallback"
            ),
            "as_of_utc": utcnow_naive().isoformat(),
        }
    ]
    return "\n".join(lines), citations


def build_listings_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    listings, capped = _safe_scan_rows(repo.list_listings(), max_rows=max_scan_rows)
    total = len(listings)
    draft = sum(1 for l in listings if (l.listing_status or "").strip().lower() == "draft")
    active = sum(1 for l in listings if (l.listing_status or "").strip().lower() == "active")
    ended = sum(1 for l in listings if (l.listing_status or "").strip().lower() == "ended")
    sold = sum(1 for l in listings if (l.listing_status or "").strip().lower() == "sold")
    pending_review = sum(1 for l in listings if (l.review_status or "pending").strip().lower() != "approved")
    lines = [
        f"Listing status snapshot across `{total}` listings:",
        f"- Draft: `{draft}`",
        f"- Active: `{active}`",
        f"- Ended: `{ended}`",
        f"- Sold: `{sold}`",
        f"- Pending/Not approved review: `{pending_review}`",
    ]
    citations = [
        {
            "table": "marketplace_listings",
            "filters": "all rows",
            "rows_considered": total,
            "capped": bool(capped),
            "as_of_utc": utcnow_naive().isoformat(),
        }
    ]
    return "\n".join(lines), citations


def build_sales_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    sales, capped = _safe_scan_rows(repo.list_sales(), max_rows=max_scan_rows)
    now = utcnow_naive()
    window_start = now - timedelta(days=30)
    recent = [s for s in sales if s.sold_at is not None and s.sold_at >= window_start]
    actual_rows = _repository_actual_economics_rows(repo, start_dt=window_start, end_dt=now)
    if actual_rows:
        gross = sum(_safe_float(row.get("sold_price")) for row in actual_rows)
        fees = sum(_safe_float(row.get("allocated_fee_actual")) for row in actual_rows)
        shipping = sum(_safe_float(row.get("allocated_shipping_charged")) for row in actual_rows)
        label_spend = sum(_safe_float(row.get("allocated_shipping_actual")) for row in actual_rows)
        net = sum(_safe_float(row.get("net_before_cogs_actual")) for row in actual_rows)
        rows_considered = len(actual_rows)
        table = "sales, order_finance_entries"
        finance_basis = "repository actual-economics rows; linked normalized finance entries override sale/order fields"
    else:
        gross = sum(float(s.sold_price or 0.0) for s in recent)
        fees = sum(float(s.fees or 0.0) for s in recent)
        shipping = sum(float(s.shipping_cost or 0.0) for s in recent)
        label_spend = sum(float(getattr(s, "shipping_label_cost", 0.0) or 0.0) for s in recent)
        net = gross + shipping - fees - label_spend
        rows_considered = len(recent)
        table = "sales"
        finance_basis = "sale fields fallback"
    lines = [
        f"Sales snapshot (last 30 days, `{rows_considered}` sales):",
        f"- Gross sold: `{_money(gross)}`",
        f"- Fees: `{_money(fees)}`",
        f"- Shipping charged: `{_money(shipping)}`",
        f"- Label spend: `{_money(label_spend)}`",
        f"- Net (gross + shipping charged - fees - label spend): `{_money(net)}`",
    ]
    citations = [
        {
            "table": table,
            "filters": f"sold_at >= {window_start.isoformat()}",
            "rows_considered": rows_considered,
            "capped": bool(capped),
            "finance_basis": finance_basis,
            "as_of_utc": now.isoformat(),
        }
    ]
    return "\n".join(lines), citations


def build_shipping_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    sales, capped = _safe_scan_rows(repo.list_sales(), max_rows=max_scan_rows)
    not_delivered = [
        s
        for s in sales
        if (s.tracking_status or "").strip().lower() not in {"delivered"}
    ]
    no_tracking = [s for s in not_delivered if not str(s.tracking_number or "").strip()]
    exceptions = [s for s in not_delivered if str(s.shipping_exception_code or "").strip()]
    lines = [
        f"Shipping snapshot (`{len(not_delivered)}` not yet delivered):",
        f"- Missing tracking number: `{len(no_tracking)}`",
        f"- With shipping exception code: `{len(exceptions)}`",
    ]
    citations = [
        {
            "table": "sales",
            "filters": "tracking_status != delivered",
            "rows_considered": len(not_delivered),
            "capped": bool(capped),
            "as_of_utc": utcnow_naive().isoformat(),
        }
    ]
    return "\n".join(lines), citations


def build_sync_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    runs = repo.list_sync_runs(limit=max(1, min(int(max_scan_rows), 5000)))
    failed = [r for r in runs if (r.status or "").strip().lower() in {"failed", "partial"}]
    queued = [r for r in runs if (r.status or "").strip().lower() == "queued"]
    running = [r for r in runs if (r.status or "").strip().lower() == "running"]
    lines = [
        f"Sync snapshot (latest `{len(runs)}` runs):",
        f"- Failed/Partial: `{len(failed)}`",
        f"- Queued: `{len(queued)}`",
        f"- Running: `{len(running)}`",
    ]
    if failed:
        lines.append("Most recent failed/partial runs:")
        for row in failed[:5]:
            lines.append(
                f"- `#{row.id}` provider=`{row.provider}` job=`{row.job_name}` status=`{row.status}` "
                f"completed_at=`{row.completed_at}`"
            )
    citations = [
        {
            "table": "sync_runs",
            "filters": "latest 300 runs",
            "rows_considered": len(runs),
            "capped": len(runs) >= max_scan_rows,
            "as_of_utc": utcnow_naive().isoformat(),
        }
    ]
    return "\n".join(lines), citations


def build_orders_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    orders, capped = _safe_scan_rows(repo.list_orders(), max_rows=max_scan_rows)
    open_like = [
        o for o in orders if (o.order_status or "").strip().lower() not in {"cancelled", "delivered", "completed"}
    ]
    lines = [
        f"Order snapshot: `{len(orders)}` total orders.",
        f"- Potentially open/in-progress orders: `{len(open_like)}`",
    ]
    citations = [
        {
            "table": "orders",
            "filters": "all rows; open-like excludes cancelled/delivered/completed",
            "rows_considered": len(orders),
            "capped": bool(capped),
            "as_of_utc": utcnow_naive().isoformat(),
        }
    ]
    return "\n".join(lines), citations


def build_reports_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    products, products_capped = _safe_scan_rows(repo.list_products(), max_rows=max_scan_rows)
    sales, sales_capped = _safe_scan_rows(repo.list_sales(), max_rows=max_scan_rows)
    orders, orders_capped = _safe_scan_rows(repo.list_orders(), max_rows=max_scan_rows)

    now = utcnow_naive()
    window_start = now - timedelta(days=30)
    product_cost_by_id = {int(p.id): _product_default_landed_unit_cost(p) for p in products}
    cost_maps = _repository_cost_maps(repo, products, end_dt=now)
    fifo_unit_cost_by_sale = dict(cost_maps.get("fifo_unit_cost_by_sale") or {})
    fifo_unit_cost_source_by_sale = {
        int(k): str(v or "").strip() or "unknown"
        for k, v in dict(cost_maps.get("fifo_unit_cost_source_by_sale") or {}).items()
        if k is not None
    }
    recent_sales = [s for s in sales if s.sold_at is not None and s.sold_at >= window_start]
    actual_rows = _repository_actual_economics_rows(repo, start_dt=window_start, end_dt=now)
    if actual_rows:
        gross = sum(_safe_float(row.get("sold_price")) for row in actual_rows)
        fees = sum(_safe_float(row.get("allocated_fee_actual")) for row in actual_rows)
        shipping = sum(_safe_float(row.get("allocated_shipping_charged")) for row in actual_rows)
        label_spend = sum(_safe_float(row.get("allocated_shipping_actual")) for row in actual_rows)
        net_before_cogs = sum(_safe_float(row.get("net_before_cogs_actual")) for row in actual_rows)
        sales_rows_considered = len(actual_rows)
        sales_table = "sales, order_finance_entries"
        finance_basis = "repository actual-economics rows; linked normalized finance entries override sale/order fields"
    else:
        gross = sum(float(s.sold_price or 0.0) for s in recent_sales)
        fees = sum(float(s.fees or 0.0) for s in recent_sales)
        shipping = sum(float(s.shipping_cost or 0.0) for s in recent_sales)
        label_spend = sum(float(getattr(s, "shipping_label_cost", 0.0) or 0.0) for s in recent_sales)
        net_before_cogs = gross + shipping - fees - label_spend
        sales_rows_considered = len(recent_sales)
        sales_table = "sales"
        finance_basis = "sale fields fallback"
    est_cogs = 0.0
    cogs_source_totals: dict[str, float] = {}
    cogs_source_counts: dict[str, int] = {}
    for sale in recent_sales:
        sale_id = getattr(sale, "id", None)
        sale_has_fifo = (
            fifo_unit_cost_by_sale
            and sale_id is not None
            and int(sale_id) in fifo_unit_cost_by_sale
        )
        if sale_has_fifo:
            unit_cost = _safe_float(fifo_unit_cost_by_sale[int(sale_id)])
            cost_source = fifo_unit_cost_source_by_sale.get(int(sale_id), "unknown")
        else:
            unit_cost = product_cost_by_id.get(int(sale.product_id or 0), 0.0)
            cost_source = "product_default_landed_cost" if unit_cost > 0 else "missing_cost_basis"
        sale_cogs = unit_cost * int(sale.quantity_sold or 0)
        est_cogs += sale_cogs
        cogs_source_totals[cost_source] = cogs_source_totals.get(cost_source, 0.0) + sale_cogs
        cogs_source_counts[cost_source] = cogs_source_counts.get(cost_source, 0) + 1
    est_margin = net_before_cogs - est_cogs
    return_rows: list[dict] = []
    if hasattr(repo, "report_returns_rows"):
        try:
            return_rows = list(repo.report_returns_rows(start_dt=window_start, end_dt=now) or [])
        except Exception:
            db = getattr(repo, "db", None)
            if db is not None and hasattr(db, "rollback"):
                db.rollback()
            return_rows = []
    returns_refund_total = sum(
        _safe_float(row.get("refund_amount"))
        + _safe_float(row.get("refund_fees"))
        + _safe_float(row.get("refund_shipping"))
        for row in return_rows
    )
    returns_cogs_reversal = 0.0
    for row in return_rows:
        sale_id = row.get("sale_id")
        if sale_id is None:
            continue
        returns_cogs_reversal += _safe_float(fifo_unit_cost_by_sale.get(int(sale_id))) * max(
            1,
            int(row.get("quantity") or 1),
        )
    returns_profit_impact = -returns_refund_total + returns_cogs_reversal
    est_profit_after_returns = est_margin + returns_profit_impact
    open_orders = [
        o for o in orders if (o.order_status or "").strip().lower() not in {"cancelled", "delivered", "completed"}
    ]
    lines = [
        f"Reports snapshot (last 30 days, `{len(recent_sales)}` sales):",
        f"- Gross sold: `{_money(gross)}`",
        f"- Fees + label spend: `{_money(fees + label_spend)}`",
        f"- Shipping charged: `{_money(shipping)}`",
        f"- Estimated COGS: `{_money(est_cogs)}`",
        f"- Estimated margin before returns: `{_money(est_margin)}`",
        f"- Open/in-progress orders: `{len(open_orders)}`",
    ]
    if return_rows:
        lines.extend(
            [
                f"- Returns: `{len(return_rows)}`",
                f"- Return refunds: `{_money(returns_refund_total)}`",
                f"- Return COGS reversal: `{_money(returns_cogs_reversal)}`",
                f"- Return profit impact: `{_money(returns_profit_impact)}`",
                f"- Estimated profit after returns: `{_money(est_profit_after_returns)}`",
            ]
        )
    if cogs_source_totals:
        lines.append("Sold COGS source mix:")
        for source, total in sorted(cogs_source_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:6]:
            lines.append(f"- `{source}`: `{_money(total)}` across `{cogs_source_counts.get(source, 0)}` sale rows")
    citations = [
        {
            "table": sales_table,
            "filters": f"sold_at >= {window_start.isoformat()}",
            "rows_considered": sales_rows_considered,
            "capped": bool(sales_capped),
            "finance_basis": finance_basis,
            "cost_basis": (
                "time-aware FIFO sale COGS, product landed acquisition cost, product_cost fallback"
                if fifo_unit_cost_by_sale
                else "product landed acquisition cost / product_cost fallback"
            ),
            "cogs_source_mix": {
                source: {
                    "cogs": round(float(total), 2),
                    "sale_rows": int(cogs_source_counts.get(source, 0)),
                }
                for source, total in sorted(cogs_source_totals.items())
            },
            "returns_count": len(return_rows),
            "returns_refund_total": round(float(returns_refund_total), 2),
            "returns_cogs_reversal": round(float(returns_cogs_reversal), 2),
            "returns_profit_impact": round(float(returns_profit_impact), 2),
            "profit_before_returns": round(float(est_margin), 2),
            "estimated_profit_after_returns": round(float(est_profit_after_returns), 2),
            "as_of_utc": now.isoformat(),
        },
        {
            "table": "returns",
            "filters": f"returned_at >= {window_start.isoformat()}",
            "rows_considered": len(return_rows),
            "capped": False,
            "as_of_utc": now.isoformat(),
        },
        {
            "table": "products",
            "filters": "used for estimated COGS map",
            "rows_considered": len(products),
            "capped": bool(products_capped),
            "as_of_utc": now.isoformat(),
        },
        {
            "table": "orders",
            "filters": "open-like excludes cancelled/delivered/completed",
            "rows_considered": len(open_orders),
            "capped": bool(orders_capped),
            "as_of_utc": now.isoformat(),
        },
    ]
    return "\n".join(lines), citations


def build_accounting_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    reports_text, reports_citations = build_reports_snapshot(repo, max_scan_rows=max_scan_rows)
    now = utcnow_naive()
    window_start = now - timedelta(days=30)
    citations = list(reports_citations)
    lines = [
        "AI Accountant snapshot (read-only; estimates require human review before close):",
        reports_text,
        "Accounting guardrails:",
        "- Profit before returns: `gross + shipping charged - fees - label spend - COGS`.",
        "- Estimated profit after returns: `profit before returns - return refunds + return COGS reversal`.",
        "- COGS precedence: assignment landed cost, lot allocation, product landed acquisition cost, `product_cost` fallback.",
        "- Tax outputs are planning estimates; local/state tax treatment still needs advisor validation.",
    ]

    exception_rows: list[dict] = []
    if hasattr(repo, "report_accounting_exception_rows"):
        try:
            exception_rows = list(
                repo.report_accounting_exception_rows(
                    start_dt=window_start,
                    end_dt=now,
                )
                or []
            )
        except Exception:
            exception_rows = []
    exception_rows, exceptions_capped = _safe_scan_rows(exception_rows, max_rows=max_scan_rows)
    dashboard_metrics: dict[str, Any] = {}
    if hasattr(repo, "dashboard_live_metrics"):
        try:
            dashboard_metrics = dict(repo.dashboard_live_metrics(now=now, include_fee_type_breakdown=False) or {})
        except TypeError:
            try:
                dashboard_metrics = dict(repo.dashboard_live_metrics(now=now) or {})
            except Exception:
                dashboard_metrics = {}
        except Exception:
            dashboard_metrics = {}
    review_outcomes = []
    try:
        review_outcomes = list_ai_accountant_review_outcomes(repo)
    except Exception:
        review_outcomes = []
    try:
        answer_rows = list_ai_accountant_answers(repo, limit=8)
    except Exception:
        answer_rows = []
    try:
        answer_followup_rows = list_ai_accountant_answer_followups(repo, limit=8)
    except Exception:
        answer_followup_rows = []
    monitor_rows = build_ai_accountant_monitor_rows(
        exception_rows,
        dashboard_metrics=dashboard_metrics,
        review_outcome_rows=review_outcomes,
        answer_rows=answer_rows,
        max_rows=max_scan_rows,
    )
    question_rows = annotate_ai_accountant_question_rows(
        build_ai_accountant_question_rows(monitor_rows, max_rows=8),
        answer_rows,
    )
    if exception_rows:
        by_type: dict[str, int] = {}
        blocker_count = 0
        for row in exception_rows:
            key = str(row.get("exception_type") or "unknown")
            by_type[key] = by_type.get(key, 0) + 1
            if str(row.get("severity") or "").strip().upper() in {"P0", "P1"}:
                blocker_count += 1
        lines.append(f"Accounting exceptions in window: `{len(exception_rows)}` (`{blocker_count}` P0/P1).")
        for key, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
            lines.append(f"- `{key}`: `{count}`")
    else:
        lines.append("Accounting exceptions in window: `0` found by available repository checks.")
    citations.append(
        {
            "table": "accounting_exception_queue",
            "filters": f"window_start={window_start.isoformat()}; window_end={now.isoformat()}",
            "rows_considered": len(exception_rows),
            "capped": bool(exceptions_capped),
            "as_of_utc": now.isoformat(),
        }
    )
    if question_rows:
        resolved_statuses = {"answered", "applied"}
        unanswered_count = sum(
            1 for row in question_rows if str(row.get("answer_status") or "") not in resolved_statuses
        )
        lines.append(
            f"AI Accountant questions to answer: `{len(question_rows)}` (`{unanswered_count}` unanswered)."
        )
        for row in question_rows[:5]:
            if str(row.get("answer_status") or "") in resolved_statuses:
                lines.append(
                    f"- `{row.get('severity')}` `{row.get('task_type')}`: {row.get('answer_status')} by "
                    f"`{row.get('latest_answer_actor') or 'unknown'}`. Latest evidence: "
                    f"{str(row.get('latest_answer_preview') or '')[:140]}"
                )
            else:
                lines.append(
                    f"- `{row.get('severity')}` `{row.get('task_type')}`: "
                    f"{row.get('question')} Reply with `{row.get('reply_prompt')}`"
                )
    else:
        lines.append("AI Accountant questions to answer: `0` currently generated from monitor evidence.")
    citations.append(
        {
            "table": "ai_accountant_monitor_questions",
            "filters": f"window_start={window_start.isoformat()}; window_end={now.isoformat()}",
            "rows_considered": len(question_rows),
            "capped": len(question_rows) >= 8,
            "as_of_utc": now.isoformat(),
        }
    )
    if answer_rows:
        lines.append(f"Recent AI Accountant operator answers recorded: `{len(answer_rows)}`.")
        for row in answer_rows[:5]:
            followup_status = str(row.get("followup_status") or "unreviewed").strip() or "unreviewed"
            lines.append(
                f"- `{row.get('task_type')}` `{row.get('reference')}` by "
                f"`{row.get('actor') or 'unknown'}` (`{followup_status}`): "
                f"{str(row.get('answer_preview') or '')[:160]}"
            )
    else:
        lines.append("Recent AI Accountant operator answers recorded: `0`.")
    if answer_followup_rows:
        lines.append(f"Recent AI Accountant answer follow-ups recorded: `{len(answer_followup_rows)}`.")
        for row in answer_followup_rows[:5]:
            lines.append(
                f"- `{row.get('outcome')}` by `{row.get('actor') or 'unknown'}` for "
                f"`{str(row.get('answer_hash_sha256') or '')[:12]}`: "
                f"{str(row.get('notes') or '')[:140]}"
            )
    else:
        lines.append("Recent AI Accountant answer follow-ups recorded: `0`.")
    citations.append(
        {
            "table": "ai_accountant_answers",
            "filters": "latest recorded operator answers",
            "rows_considered": len(answer_rows),
            "capped": len(answer_rows) >= 8,
            "as_of_utc": now.isoformat(),
        }
    )
    citations.append(
        {
            "table": "ai_accountant_answer_followups",
            "filters": "latest recorded answer follow-up events",
            "rows_considered": len(answer_followup_rows),
            "capped": len(answer_followup_rows) >= 8,
            "as_of_utc": now.isoformat(),
        }
    )

    lot_rows: list[dict] = []
    if hasattr(repo, "report_lot_assignment_rows"):
        try:
            lot_rows = list(repo.report_lot_assignment_rows(start_dt=None, end_dt=now) or [])
        except Exception:
            lot_rows = []
    lot_rows, lots_capped = _safe_scan_rows(lot_rows, max_rows=max_scan_rows)
    if lot_rows:
        source_counts: dict[str, int] = {}
        for row in lot_rows:
            source = str(row.get("cost_source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        fallback_count = sum(
            count
            for source, count in source_counts.items()
            if source in {"lot_equal_quantity_fallback", "missing_cost_basis"}
        )
        lines.append(f"Lot allocation rows reviewed: `{len(lot_rows)}` (`{fallback_count}` fallback/missing-basis rows).")
        for source, count in sorted(source_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
            lines.append(f"- `{source}`: `{count}`")
    else:
        lines.append("Lot allocation rows reviewed: `0` available in repository snapshot.")
    citations.append(
        {
            "table": "product_lot_assignments",
            "filters": f"assigned_at <= {now.isoformat()}; includes resolved cost_source where available",
            "rows_considered": len(lot_rows),
            "capped": bool(lots_capped),
            "as_of_utc": now.isoformat(),
        }
    )
    return "\n".join(lines), citations


def build_admin_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    users, users_capped = _safe_scan_rows(repo.list_app_users(active_only=False), max_rows=max_scan_rows)
    runtime_rows, runtime_capped = _safe_scan_rows(
        repo.list_runtime_settings(environment=settings.app_env, active_only=False),
        max_rows=max_scan_rows,
    )
    ai_profiles, ai_profiles_capped = _safe_scan_rows(
        repo.list_ai_provider_configs(environment=settings.app_env, active_only=False),
        max_rows=max_scan_rows,
    )
    sync_runs = repo.list_sync_runs(limit=max(1, min(int(max_scan_rows), 5000)))
    failed_sync = [r for r in sync_runs if (r.status or "").strip().lower() in {"failed", "partial"}]
    lines = [
        f"Admin snapshot (`{settings.app_env}` environment):",
        f"- App users: `{len(users)}` total (`{sum(1 for u in users if bool(u.is_active))}` active)",
        f"- Runtime settings: `{len(runtime_rows)}` total (`{sum(1 for r in runtime_rows if bool(r.is_active))}` active)",
        f"- AI runtime profiles: `{len(ai_profiles)}` total (`{sum(1 for p in ai_profiles if bool(p.is_active))}` active)",
        f"- Recent sync runs: `{len(sync_runs)}` (`{len(failed_sync)}` failed/partial)",
    ]
    citations = [
        {
            "table": "app_users",
            "filters": "all rows",
            "rows_considered": len(users),
            "capped": bool(users_capped),
            "as_of_utc": utcnow_naive().isoformat(),
        },
        {
            "table": "runtime_settings",
            "filters": f"environment={settings.app_env}",
            "rows_considered": len(runtime_rows),
            "capped": bool(runtime_capped),
            "as_of_utc": utcnow_naive().isoformat(),
        },
        {
            "table": "ai_provider_configs",
            "filters": f"environment={settings.app_env}",
            "rows_considered": len(ai_profiles),
            "capped": bool(ai_profiles_capped),
            "as_of_utc": utcnow_naive().isoformat(),
        },
        {
            "table": "sync_runs",
            "filters": "latest N runs",
            "rows_considered": len(sync_runs),
            "capped": len(sync_runs) >= max_scan_rows,
            "as_of_utc": utcnow_naive().isoformat(),
        },
    ]
    return "\n".join(lines), citations


def build_fallback_help() -> tuple[str, list[dict]]:
    return (
        "I can answer read-only operations questions from app data. Try one of:\n"
        "- `inventory snapshot`\n"
        "- `listing draft and review status`\n"
        "- `sales last 30 days`\n"
        "- `shipping exceptions`\n"
        "- `sync failures`\n"
        "- `orders status`\n"
        "- `reports summary`\n"
        "- `admin config status` (admin role)",
        [],
    )
