from datetime import timedelta
from typing import Any

from app.config import settings
from app.utils.time import utcnow_naive


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _safe_scan_rows(rows: list[Any], *, max_rows: int) -> tuple[list[Any], bool]:
    if len(rows) <= max_rows:
        return rows, False
    return rows[:max_rows], True


def build_inventory_snapshot(repo: Any, *, max_scan_rows: int) -> tuple[str, list[dict]]:
    products, capped = _safe_scan_rows(repo.list_products(), max_rows=max_scan_rows)
    on_hand = [p for p in products if int(p.current_quantity or 0) > 0]
    total_units = sum(int(p.current_quantity or 0) for p in on_hand)
    inventory_value = sum(float(p.acquisition_cost or 0.0) * int(p.current_quantity or 0) for p in on_hand)
    top_on_hand = sorted(
        [
            {
                "sku": p.sku,
                "title": p.title,
                "qty": int(p.current_quantity or 0),
                "unit_cost": float(p.acquisition_cost or 0.0),
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
    pending_review = sum(1 for l in listings if (l.review_status or "pending").strip().lower() != "approved")
    lines = [
        f"Listing status snapshot across `{total}` listings:",
        f"- Draft: `{draft}`",
        f"- Active: `{active}`",
        f"- Ended: `{ended}`",
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
    gross = sum(float(s.sold_price or 0.0) for s in recent)
    fees = sum(float(s.fees or 0.0) for s in recent)
    shipping = sum(float(s.shipping_cost or 0.0) for s in recent)
    net = gross - fees - shipping
    lines = [
        f"Sales snapshot (last 30 days, `{len(recent)}` sales):",
        f"- Gross sold: `{_money(gross)}`",
        f"- Fees: `{_money(fees)}`",
        f"- Shipping cost: `{_money(shipping)}`",
        f"- Net (gross - fees - shipping): `{_money(net)}`",
    ]
    citations = [
        {
            "table": "sales",
            "filters": f"sold_at >= {window_start.isoformat()}",
            "rows_considered": len(recent),
            "capped": bool(capped),
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
    product_cost_by_id = {int(p.id): float(p.acquisition_cost or 0.0) for p in products}
    recent_sales = [s for s in sales if s.sold_at is not None and s.sold_at >= window_start]
    gross = sum(float(s.sold_price or 0.0) for s in recent_sales)
    fees = sum(float(s.fees or 0.0) for s in recent_sales)
    shipping = sum(float(s.shipping_cost or 0.0) for s in recent_sales)
    est_cogs = sum(
        (product_cost_by_id.get(int(s.product_id or 0), 0.0) * int(s.quantity_sold or 0))
        for s in recent_sales
    )
    est_margin = gross - fees - shipping - est_cogs
    open_orders = [
        o for o in orders if (o.order_status or "").strip().lower() not in {"cancelled", "delivered", "completed"}
    ]
    lines = [
        f"Reports snapshot (last 30 days, `{len(recent_sales)}` sales):",
        f"- Gross sold: `{_money(gross)}`",
        f"- Fees + shipping: `{_money(fees + shipping)}`",
        f"- Estimated COGS: `{_money(est_cogs)}`",
        f"- Estimated margin: `{_money(est_margin)}`",
        f"- Open/in-progress orders: `{len(open_orders)}`",
    ]
    citations = [
        {
            "table": "sales",
            "filters": f"sold_at >= {window_start.isoformat()}",
            "rows_considered": len(recent_sales),
            "capped": bool(sales_capped),
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
