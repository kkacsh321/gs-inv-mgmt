from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json

from sqlalchemy import select

from app.db.models import MarketplaceListing, Order, Product, Sale
from app.config import settings
from app.repository import InventoryRepository
from app.services.ebay import EbayClient
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.slack_notify import build_slack_alert_text, dispatch_slack_alert, resolve_slack_notify_config
from app.utils.time import utcnow_naive


def _is_ebay_auth_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return True
    message = str(exc or "").lower()
    if "token" in message and ("expired" in message or "invalid" in message):
        return True
    return False


def _resolve_ebay_tokens(
    repo: InventoryRepository,
    *,
    access_token: str = "",
) -> tuple[str, str]:
    resolved_access = str(access_token or "").strip() or get_runtime_str(
        repo,
        "ebay_user_access_token",
        settings.ebay_user_access_token,
    ).strip()
    resolved_refresh = get_runtime_str(
        repo,
        "ebay_user_refresh_token",
        settings.ebay_user_refresh_token,
    ).strip()
    return resolved_access, resolved_refresh


def _persist_ebay_tokens(
    repo: InventoryRepository,
    *,
    actor: str,
    access_token: str,
    refresh_token: str = "",
    expires_in: int = 0,
) -> None:
    try:
        if access_token:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_user_access_token",
                value=access_token,
                value_type="str",
                description="Default eBay user access token used by verification and sync jobs.",
                actor=actor,
            )
        if refresh_token:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_user_refresh_token",
                value=refresh_token,
                value_type="str",
                description="Default eBay user refresh token used to renew access tokens.",
                actor=actor,
            )
        if expires_in and int(expires_in) > 0:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_user_access_token_expires_at",
                value=(utcnow_naive()).isoformat(timespec="seconds"),
                value_type="str",
                description="Best-effort timestamp when eBay access token was last refreshed.",
                actor=actor,
            )
    except Exception:
        pass


def _refresh_ebay_access_token(
    repo: InventoryRepository,
    *,
    ebay_client: EbayClient,
    actor: str,
    refresh_token: str,
) -> tuple[str, str]:
    payload = ebay_client.refresh_user_token(refresh_token)
    new_access = str(payload.get("access_token") or "").strip()
    if not new_access:
        raise ValueError("eBay refresh token call returned no access_token.")
    new_refresh = str(payload.get("refresh_token") or "").strip() or refresh_token
    _persist_ebay_tokens(
        repo,
        actor=actor,
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=int(payload.get("expires_in") or 0),
    )
    return new_access, new_refresh


def _csv_set(value: str, default: set[str]) -> set[str]:
    raw = (value or "").strip()
    if not raw:
        return set(default)
    out = {part.strip().lower() for part in raw.split(",") if part and part.strip()}
    return out or set(default)


def _notify_sync_status_slack(
    repo: InventoryRepository,
    *,
    job_name: str,
    run_id: int,
    status: str,
    processed: int,
    failed: int,
    actor: str,
) -> None:
    try:
        normalized = str(status or "").strip().lower()
        if normalized not in {"failed", "partial"}:
            return
        cfg = resolve_slack_notify_config(repo)
        if not cfg.enabled or not cfg.notify_sync_failures:
            return
        alert_text = build_slack_alert_text(
            repo,
            event_type="sync_failures",
            default_template=(
                ":warning: *GoldenStackers* sync run `{job_name}` `{status}`\n"
                "- Env: `{env}`\n"
                "- Run: `#{run_id}`\n"
                "- Processed: `{processed}`\n"
                "- Failed: `{failed}`\n"
                "- Actor: `{actor}`"
            ),
            context={
                "job_name": job_name,
                "status": normalized,
                "run_id": run_id,
                "processed": processed,
                "failed": failed,
                "actor": actor,
                "env": settings.app_env,
            },
        )
        dispatch_slack_alert(
            repo,
            actor=actor,
            event_type="sync_failures",
            severity="warning" if normalized == "partial" else "error",
            text=alert_text,
        )
    except Exception:
        pass


def sync_job_retry_policy(job_name: str, repo: InventoryRepository | None = None) -> dict[str, object]:
    resolved = (job_name or "").strip().lower()
    retry_key = f"sync_job_{resolved}_max_retries"
    backoff_key = f"sync_job_{resolved}_retry_backoff_seconds"
    retryable_key = f"sync_job_{resolved}_retryable_statuses"
    terminal_key = f"sync_job_{resolved}_terminal_statuses"

    if repo is None:
        max_retries = 3
        retry_backoff_seconds = 0
        retryable_statuses = {"failed", "partial"}
        terminal_statuses = {"success", "failed", "partial"}
    else:
        max_retries = max(0, min(25, int(get_runtime_int(repo, retry_key, 3))))
        retry_backoff_seconds = max(0, min(86400, int(get_runtime_int(repo, backoff_key, 0))))
        retryable_statuses = _csv_set(
            get_runtime_str(repo, retryable_key, "failed,partial"),
            {"failed", "partial"},
        )
        terminal_statuses = _csv_set(
            get_runtime_str(repo, terminal_key, "success,failed,partial"),
            {"success", "failed", "partial"},
        )

    return {
        "max_retries": int(max_retries),
        "retry_backoff_seconds": int(retry_backoff_seconds),
        "retryable_statuses": sorted(retryable_statuses),
        "terminal_statuses": sorted(terminal_statuses),
        "runtime_keys": {
            "max_retries": retry_key,
            "retry_backoff_seconds": backoff_key,
            "retryable_statuses": retryable_key,
            "terminal_statuses": terminal_key,
        },
    }


def sync_job_catalog(repo: InventoryRepository | None = None) -> list[dict[str, object]]:
    rows = [
        {
            "job_name": "ebay_orders_pull_import",
            "provider": "ebay",
            "direction": "pull",
            "implemented": True,
            "description": "Pull recent eBay orders and upsert local orders/order_items/sales.",
            "enabled": bool(is_sync_job_enabled("ebay_orders_pull_import", repo=repo)),
        },
        {
            "job_name": "ebay_shipping_tracking_push",
            "provider": "ebay",
            "direction": "push",
            "implemented": True,
            "description": "Push local tracking details to eBay order fulfillment.",
            "enabled": bool(is_sync_job_enabled("ebay_shipping_tracking_push", repo=repo)),
        },
        {
            "job_name": "ebay_connection_health_check",
            "provider": "ebay",
            "direction": "pull",
            "implemented": True,
            "description": "Validate eBay token/identity/privileges and keep integration health current.",
            "enabled": bool(is_sync_job_enabled("ebay_connection_health_check", repo=repo)),
        },
        {
            "job_name": "quickbooks_export",
            "provider": "quickbooks",
            "direction": "push",
            "implemented": False,
            "description": "Placeholder for accounting export dispatcher wiring.",
            "enabled": bool(is_sync_job_enabled("quickbooks_export", repo=repo)),
        },
        {
            "job_name": "shopify_orders_pull",
            "provider": "shopify",
            "direction": "pull",
            "implemented": True,
            "description": "Shopify order ingestion scaffold (sync run/event wiring + safe no-op pull).",
            "enabled": bool(is_sync_job_enabled("shopify_orders_pull", repo=repo)),
        },
    ]
    for row in rows:
        row["retry_policy"] = sync_job_retry_policy(str(row.get("job_name") or ""), repo=repo)
        row["dispatch_meta"] = sync_job_dispatch_meta(str(row.get("job_name") or ""))
    return rows


def sync_job_dispatch_meta(job_name: str) -> dict[str, object]:
    resolved = (job_name or "").strip().lower()
    if resolved == "ebay_orders_pull_import":
        return {
            "supports_execute_now": True,
            "supports_retry_execute_now": True,
            "required_args": ["access_token"],
            "optional_args": ["limit", "offset", "run_id", "retry_of_run_id"],
        }
    if resolved == "ebay_shipping_tracking_push":
        return {
            "supports_execute_now": True,
            "supports_retry_execute_now": False,
            "required_args": ["access_token"],
            "optional_args": ["sale_ids", "run_id", "retry_of_run_id"],
        }
    if resolved == "ebay_connection_health_check":
        return {
            "supports_execute_now": True,
            "supports_retry_execute_now": True,
            "required_args": [],
            "optional_args": ["access_token", "run_id", "retry_of_run_id", "client"],
        }
    if resolved == "shopify_orders_pull":
        return {
            "supports_execute_now": True,
            "supports_retry_execute_now": True,
            "required_args": [],
            "optional_args": ["shop_domain", "access_token", "limit", "offset", "run_id", "retry_of_run_id"],
        }
    return {
        "supports_execute_now": False,
        "supports_retry_execute_now": False,
        "required_args": [],
        "optional_args": [],
    }


def _extract_orders(payload: dict) -> list[dict]:
    orders = payload.get("orders")
    if isinstance(orders, list):
        return orders
    return []


def is_sync_job_enabled(job_name: str, repo: InventoryRepository | None = None) -> bool:
    resolved = (job_name or "").strip().lower()
    if repo is None:
        mapping = {
            "ebay_orders_pull_import": bool(settings.sync_job_ebay_orders_pull_import_enabled),
            "ebay_shipping_tracking_push": bool(settings.sync_job_ebay_shipping_tracking_push_enabled),
            "ebay_connection_health_check": bool(
                getattr(settings, "sync_job_ebay_connection_health_check_enabled", True)
            ),
            "quickbooks_export": bool(settings.sync_job_quickbooks_export_enabled),
            "shopify_orders_pull": bool(settings.sync_job_shopify_orders_pull_enabled),
        }
    else:
        mapping = {
            "ebay_orders_pull_import": get_runtime_bool(
                repo,
                "sync_job_ebay_orders_pull_import_enabled",
                bool(settings.sync_job_ebay_orders_pull_import_enabled),
            ),
            "ebay_shipping_tracking_push": get_runtime_bool(
                repo,
                "sync_job_ebay_shipping_tracking_push_enabled",
                bool(settings.sync_job_ebay_shipping_tracking_push_enabled),
            ),
            "ebay_connection_health_check": get_runtime_bool(
                repo,
                "sync_job_ebay_connection_health_check_enabled",
                bool(getattr(settings, "sync_job_ebay_connection_health_check_enabled", True)),
            ),
            "quickbooks_export": get_runtime_bool(
                repo,
                "sync_job_quickbooks_export_enabled",
                bool(settings.sync_job_quickbooks_export_enabled),
            ),
            "shopify_orders_pull": get_runtime_bool(
                repo,
                "sync_job_shopify_orders_pull_enabled",
                bool(settings.sync_job_shopify_orders_pull_enabled),
            ),
        }
    return mapping.get(resolved, True)


def execute_sync_job(
    repo: InventoryRepository,
    *,
    job_name: str,
    actor: str,
    **kwargs,
) -> dict:
    resolved = (job_name or "").strip().lower()
    if not is_sync_job_enabled(resolved, repo=repo):
        raise ValueError(f"Sync job `{resolved}` is disabled by configuration.")

    if resolved == "ebay_orders_pull_import":
        return execute_ebay_orders_pull_import(
            repo,
            access_token=str(kwargs.get("access_token") or "").strip(),
            actor=actor,
            limit=int(kwargs.get("limit", 25)),
            offset=int(kwargs.get("offset", 0)),
            run_id=kwargs.get("run_id"),
            retry_of_run_id=kwargs.get("retry_of_run_id"),
            client=kwargs.get("client"),
        )
    if resolved == "ebay_shipping_tracking_push":
        sale_ids = kwargs.get("sale_ids") or []
        return execute_ebay_shipping_tracking_push(
            repo,
            access_token=str(kwargs.get("access_token") or "").strip(),
            actor=actor,
            sale_ids=list(sale_ids),
            run_id=kwargs.get("run_id"),
            retry_of_run_id=kwargs.get("retry_of_run_id"),
            client=kwargs.get("client"),
        )
    if resolved == "shopify_orders_pull":
        return execute_shopify_orders_pull_scaffold(
            repo,
            actor=actor,
            shop_domain=str(kwargs.get("shop_domain") or "").strip(),
            access_token=str(kwargs.get("access_token") or "").strip(),
            limit=int(kwargs.get("limit", 50)),
            offset=int(kwargs.get("offset", 0)),
            run_id=kwargs.get("run_id"),
            retry_of_run_id=kwargs.get("retry_of_run_id"),
        )
    if resolved == "ebay_connection_health_check":
        return execute_ebay_connection_health_check(
            repo,
            actor=actor,
            access_token=str(kwargs.get("access_token") or "").strip(),
            run_id=kwargs.get("run_id"),
            retry_of_run_id=kwargs.get("retry_of_run_id"),
            client=kwargs.get("client"),
        )

    raise NotImplementedError(f"Sync job `{resolved}` is not implemented yet.")


def execute_ebay_connection_health_check(
    repo: InventoryRepository,
    *,
    actor: str,
    access_token: str = "",
    run_id: int | None = None,
    retry_of_run_id: int | None = None,
    client: EbayClient | None = None,
) -> dict:
    if not is_sync_job_enabled("ebay_connection_health_check", repo=repo):
        raise ValueError("Sync job `ebay_connection_health_check` is disabled by configuration.")

    ebay_client = client or EbayClient()
    resolved_access, refresh_token = _resolve_ebay_tokens(repo, access_token=access_token)

    if run_id is None:
        run = repo.create_sync_run(
            provider="ebay",
            job_name="ebay_connection_health_check",
            direction="pull",
            status="queued",
            retry_of_run_id=retry_of_run_id,
            retry_count=1 if retry_of_run_id else 0,
            notes="eBay connection health check queued.",
            actor=actor,
        )
        run_id = int(run.id)

    repo.update_sync_run(
        int(run_id),
        {
            "status": "running",
            "started_at": utcnow_naive(),
            "notes": "eBay connection health check running.",
        },
        actor=actor,
    )

    checks: list[dict[str, object]] = []
    failures: list[str] = []
    warnings: list[str] = []
    resolved_user = ""
    seller_registered = False
    token_scope_present = False
    claims_parsed = False
    token_refreshed = False

    if not ebay_client.is_configured():
        failures.append("eBay client keys/RU name are not configured.")
        checks.append({"name": "client_configured", "status": "failed", "details": failures[-1]})
    else:
        checks.append({"name": "client_configured", "status": "pass", "details": "Client credentials configured."})

    if not resolved_access:
        failures.append("No eBay user access token found.")
        checks.append({"name": "access_token", "status": "failed", "details": failures[-1]})
    else:
        claims = ebay_client.decode_access_token_claims(resolved_access)
        claims_parsed = bool(claims)
        scope_value = str(claims.get("scope") or "").strip()
        token_scope_present = bool(scope_value)
        if claims_parsed:
            checks.append({"name": "token_claims", "status": "pass", "details": "JWT claims parsed."})
        else:
            warnings.append("Token claims were not parseable (opaque token is possible).")
            checks.append({"name": "token_claims", "status": "warn", "details": warnings[-1]})

        try:
            privileges = ebay_client.get_account_privileges(resolved_access)
        except Exception as exc:
            if _is_ebay_auth_error(exc) and refresh_token:
                try:
                    refreshed_access, _new_refresh = _refresh_ebay_access_token(
                        repo,
                        ebay_client=ebay_client,
                        actor=actor,
                        refresh_token=refresh_token,
                    )
                    token_refreshed = True
                    resolved_access = refreshed_access
                    privileges = ebay_client.get_account_privileges(resolved_access)
                    checks.append(
                        {
                            "name": "token_refresh",
                            "status": "pass",
                            "details": "Access token refreshed automatically.",
                        }
                    )
                except Exception as refresh_exc:
                    failures.append(f"Token refresh failed: {refresh_exc}")
                    checks.append({"name": "token_refresh", "status": "failed", "details": failures[-1]})
                    privileges = {}
            else:
                failures.append(f"Privileges check failed: {exc}")
                checks.append({"name": "privileges_api", "status": "failed", "details": failures[-1]})
                privileges = {}

        if privileges:
            seller_registered = bool(privileges.get("sellerRegistrationCompleted"))
            checks.append({"name": "privileges_api", "status": "pass", "details": "Privileges endpoint returned."})
            if not seller_registered:
                warnings.append("sellerRegistrationCompleted=false (listing operations may be limited).")
                checks.append({"name": "seller_registration", "status": "warn", "details": warnings[-1]})
            else:
                checks.append({"name": "seller_registration", "status": "pass", "details": "Seller registered."})

            try:
                identity_payload = ebay_client.get_identity_user(resolved_access)
                resolved_user = str(
                    identity_payload.get("username")
                    or identity_payload.get("userId")
                    or identity_payload.get("userID")
                    or ""
                ).strip()
                checks.append(
                    {
                        "name": "identity_api",
                        "status": "pass",
                        "details": f"Identity resolved: {resolved_user or '(unknown)'}",
                    }
                )
            except Exception as exc:
                warnings.append(f"Identity API check failed: {exc}")
                checks.append({"name": "identity_api", "status": "warn", "details": warnings[-1]})

    status = "success"
    if failures:
        status = "failed"
    elif warnings:
        status = "partial"

    payload = {
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "resolved_user": resolved_user,
        "seller_registered": bool(seller_registered),
        "token_scope_present": bool(token_scope_present),
        "claims_parsed": bool(claims_parsed),
        "token_refreshed": bool(token_refreshed),
    }
    repo.add_sync_event(
        sync_run_id=int(run_id),
        entity_type="ebay_connection",
        entity_id="health",
        action="health_check",
        status="ok" if status == "success" else ("warn" if status == "partial" else "error"),
        message=f"eBay connection health check finished with status={status}.",
        payload_json=json.dumps(payload),
    )
    for idx, msg in enumerate(failures[:10], start=1):
        repo.add_sync_error(
            sync_run_id=int(run_id),
            code=f"health_failure_{idx}",
            message=msg,
            severity="error",
            context_json=json.dumps(payload),
        )

    repo.update_sync_run(
        int(run_id),
        {
            "status": status,
            "records_processed": int(len(checks)),
            "records_created": 0,
            "records_updated": 0,
            "records_failed": int(len(failures)),
            "completed_at": utcnow_naive(),
            "notes": (
                f"health={status} user={resolved_user or '(unknown)'} "
                f"seller_registered={'yes' if seller_registered else 'no'} "
                f"token_scope_present={'yes' if token_scope_present else 'no'} "
                f"claims_parsed={'yes' if claims_parsed else 'no'} "
                f"token_refreshed={'yes' if token_refreshed else 'no'}"
            ),
        },
        actor=actor,
    )

    return {
        "run_id": int(run_id),
        "status": status,
        "processed": int(len(checks)),
        "failed": int(len(failures)),
        "warnings": int(len(warnings)),
        "resolved_user": resolved_user,
        "seller_registered": bool(seller_registered),
        "token_scope_present": bool(token_scope_present),
        "claims_parsed": bool(claims_parsed),
        "token_refreshed": bool(token_refreshed),
    }


def execute_shopify_orders_pull_scaffold(
    repo: InventoryRepository,
    *,
    actor: str,
    shop_domain: str = "",
    access_token: str = "",
    limit: int = 50,
    offset: int = 0,
    run_id: int | None = None,
    retry_of_run_id: int | None = None,
) -> dict:
    if not is_sync_job_enabled("shopify_orders_pull", repo=repo):
        raise ValueError("Sync job `shopify_orders_pull` is disabled by configuration.")

    resolved_shop_domain = str(shop_domain or "").strip() or get_runtime_str(
        repo,
        "sync_job_shopify_orders_pull_shop_domain",
        settings.sync_job_shopify_orders_pull_shop_domain,
    ).strip()
    resolved_access_token = str(access_token or "").strip() or get_runtime_str(
        repo,
        "sync_job_shopify_orders_pull_access_token",
        settings.sync_job_shopify_orders_pull_access_token,
    ).strip()
    default_limit = max(1, int(get_runtime_int(
        repo,
        "sync_job_shopify_orders_pull_limit",
        int(settings.sync_job_shopify_orders_pull_limit or 50),
    )))
    default_offset = max(0, int(get_runtime_int(
        repo,
        "sync_job_shopify_orders_pull_offset",
        int(settings.sync_job_shopify_orders_pull_offset or 0),
    )))
    resolved_limit = max(1, min(250, int(limit if limit is not None else default_limit)))
    resolved_offset = max(0, int(offset if offset is not None else default_offset))

    if run_id is None:
        run = repo.create_sync_run(
            provider="shopify",
            job_name="shopify_orders_pull",
            direction="pull",
            status="queued",
            retry_of_run_id=retry_of_run_id,
            retry_count=1 if retry_of_run_id else 0,
            notes=(
                f"Shopify pull scaffold queued (domain={resolved_shop_domain or 'n/a'}, "
                f"limit={resolved_limit}, offset={resolved_offset})."
            ),
            actor=actor,
        )
        run_id = run.id

    repo.update_sync_run(
        run_id,
        {
            "status": "running",
            "started_at": utcnow_naive(),
            "notes": (
                f"Shopify pull scaffold running (domain={resolved_shop_domain or 'n/a'}, "
                f"token_set={'yes' if bool(resolved_access_token) else 'no'})."
            ),
        },
        actor=actor,
    )

    repo.add_sync_event(
        sync_run_id=run_id,
        entity_type="shopify_order",
        entity_id="scaffold",
        action="pull_scaffold",
        status="ok",
        message=(
            "Shopify pull scaffold executed. API ingestion mapping is pending full adapter implementation."
        ),
        payload={
            "shop_domain": resolved_shop_domain,
            "token_set": bool(resolved_access_token),
            "limit": resolved_limit,
            "offset": resolved_offset,
        },
    )

    repo.update_sync_run(
        run_id,
        {
            "status": "success",
            "records_processed": 0,
            "records_created": 0,
            "records_updated": 0,
            "records_failed": 0,
            "completed_at": utcnow_naive(),
            "notes": (
                "Shopify pull scaffold completed (no external API records ingested yet). "
                f"domain={resolved_shop_domain or 'n/a'} limit={resolved_limit} offset={resolved_offset}."
            ),
        },
        actor=actor,
    )
    return {
        "run_id": run_id,
        "status": "success",
        "processed": 0,
        "created": 0,
        "updated": 0,
        "failed": 0,
        "scaffold": True,
    }


def _to_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _parse_ebay_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _map_order_status(ebay_order: dict) -> str:
    status = str(ebay_order.get("orderFulfillmentStatus", "")).strip().lower()
    if status in {"fulfilled", "delivered"}:
        return "delivered"
    if status in {"shipped", "in_progress"}:
        return "shipped"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"payment_failed", "refunded"}:
        return "refunded"
    return "paid"


def _map_tracking_status(order_fulfillment_status: str, has_tracking: bool) -> str:
    status = (order_fulfillment_status or "").strip().lower()
    if status in {"delivered", "fulfilled"}:
        return "delivered" if has_tracking else "label_created"
    if status in {"shipped", "in_progress"}:
        return "in_transit" if has_tracking else "label_created"
    if status in {"cancelled", "canceled", "failed"}:
        return "exception"
    return "label_created" if has_tracking else ""


def _extract_shipping_enrichment(
    ebay_order: dict,
    fulfillments: list[dict] | None,
) -> dict:
    rows = fulfillments or []
    best = rows[0] if rows else {}
    if rows:
        try:
            rows_sorted = sorted(
                rows,
                key=lambda r: _parse_ebay_datetime(r.get("shippedDate")) or datetime.min,
                reverse=True,
            )
            best = rows_sorted[0]
        except Exception:
            best = rows[0]

    tracking_number = (
        str(best.get("trackingNumber") or "").strip()
        or str(best.get("shipmentTrackingNumber") or "").strip()
    )
    provider = (
        str(best.get("shippingCarrierCode") or "").strip().lower()
        or str(best.get("shippingCarrierName") or "").strip().lower()
    )
    shipped_at = (
        _parse_ebay_datetime(best.get("shippedDate"))
        or _parse_ebay_datetime(ebay_order.get("creationDate"))
    )
    delivered_at = (
        _parse_ebay_datetime(best.get("deliveredDate"))
        or _parse_ebay_datetime(best.get("deliveryDate"))
    )
    if delivered_at is None and str(ebay_order.get("orderFulfillmentStatus") or "").strip().lower() in {
        "delivered",
        "fulfilled",
    }:
        delivered_at = _parse_ebay_datetime(ebay_order.get("lastModifiedDate"))

    tracking_status = _map_tracking_status(
        str(ebay_order.get("orderFulfillmentStatus") or ""),
        has_tracking=bool(tracking_number),
    )
    return {
        "tracking_number": tracking_number,
        "shipping_provider": provider,
        "tracking_status": tracking_status,
        "shipped_at": shipped_at,
        "delivered_at": delivered_at,
    }


def _build_order_items(
    ebay_order: dict,
    *,
    repo: InventoryRepository,
    product_map: dict[str, int],
    listing_map: dict[str, int],
    sku_listing_candidates: dict[str, list[MarketplaceListing]],
    actor: str,
) -> tuple[list[dict], int, int, int]:
    line_items = ebay_order.get("lineItems")
    rows: list[dict] = []
    listings_created = 0
    listing_link_count = 0
    unmapped_sku_count = 0
    if isinstance(line_items, list):
        for line in line_items:
            qty = max(1, int(line.get("quantity") or line.get("lineItemQuantity") or 1))
            line_total = _to_decimal(
                (line.get("lineItemCost") or {}).get("value")
                or (line.get("total") or {}).get("value")
                or 0
            )
            unit_price = (line_total / qty) if qty > 0 else Decimal("0")
            sku = str(line.get("sku") or "").strip()
            product_id = product_map.get(sku)
            if sku and product_id is None:
                unmapped_sku_count += 1
            legacy_item_id = str(line.get("legacyItemId") or "").strip()
            listing_id = listing_map.get(legacy_item_id)
            line_title = str(line.get("title") or line.get("lineItemTitle") or "").strip()

            if listing_id is None and legacy_item_id and product_id is not None:
                listing_title = str(line.get("title") or line.get("lineItemTitle") or "").strip() or f"eBay Item {legacy_item_id}"
                try:
                    created_listing = repo.create_listing(
                        product_id=product_id,
                        marketplace="ebay",
                        listing_title=listing_title,
                        listing_price=unit_price,
                        quantity_listed=max(1, qty),
                        external_listing_id=legacy_item_id,
                        listing_status="active",
                        listed_at=utcnow_naive(),
                        actor=actor,
                    )
                    listing_id = created_listing.id
                    listing_map[legacy_item_id] = listing_id
                    listings_created += 1
                except Exception:
                    existing_listing = repo.db.scalar(
                        select(MarketplaceListing).where(
                            MarketplaceListing.marketplace == "ebay",
                            MarketplaceListing.external_listing_id == legacy_item_id,
                        )
                    )
                    if existing_listing is not None:
                        listing_id = existing_listing.id
                        listing_map[legacy_item_id] = listing_id
            # Harder edge case: no/unknown legacy item ID, but line has SKU and there are
            # potentially multiple local eBay listings for the same SKU.
            if listing_id is None and sku:
                candidates = list(sku_listing_candidates.get(sku, []))
                if candidates:
                    if len(candidates) == 1:
                        listing_id = candidates[0].id
                    else:
                        normalized_title = line_title.strip().lower()
                        if normalized_title:
                            title_exact = [
                                c for c in candidates if (c.listing_title or "").strip().lower() == normalized_title
                            ]
                            if len(title_exact) == 1:
                                listing_id = title_exact[0].id
                                candidates = title_exact
                            elif len(title_exact) > 1:
                                candidates = title_exact

                        if listing_id is None:
                            target_unit_price = float(unit_price)
                            price_matches = [
                                c
                                for c in candidates
                                if abs(float(c.listing_price or 0) - target_unit_price) <= 0.01
                            ]
                            if len(price_matches) == 1:
                                listing_id = price_matches[0].id
                                candidates = price_matches
                            elif len(price_matches) > 1:
                                candidates = price_matches

                        if listing_id is None:
                            active_candidates = [
                                c for c in candidates if (c.listing_status or "").strip().lower() == "active"
                            ]
                            if len(active_candidates) == 1:
                                listing_id = active_candidates[0].id
            if listing_id is not None:
                listing_link_count += 1

            rows.append(
                {
                    "product_id": product_id,
                    "listing_id": listing_id,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "line_fees": Decimal("0"),
                    "line_shipping": Decimal("0"),
                    "notes": (
                        f"ebay_line_item_id={line.get('lineItemId', '')}; "
                        f"legacy_item_id={line.get('legacyItemId', '')}; sku={sku}"
                    ),
                }
            )
    if rows:
        return rows, listings_created, listing_link_count, unmapped_sku_count

    fallback_total = _to_decimal((ebay_order.get("pricingSummary") or {}).get("total", {}).get("value") or 0)
    return (
        [
            {
                "product_id": None,
                "listing_id": None,
                "quantity": 1,
                "unit_price": fallback_total,
                "line_fees": Decimal("0"),
                "line_shipping": Decimal("0"),
                "notes": "Generated fallback line item from order total.",
            }
        ],
        listings_created,
        0,
        0,
    )


def _sum_line_totals(items: list[dict]) -> Decimal:
    total = Decimal("0")
    for item in items:
        total += _to_decimal(item.get("unit_price")) * int(item.get("quantity") or 0)
    return total


def _upsert_ebay_order_into_local(
    repo: InventoryRepository,
    ebay_order: dict,
    *,
    actor: str,
    product_map: dict[str, int],
    listing_map: dict[str, int],
    sku_listing_candidates: dict[str, list[MarketplaceListing]],
    ebay_client: EbayClient,
    access_token: str,
    sync_run_id: int,
) -> dict[str, int]:
    external_order_id = str(ebay_order.get("orderId") or "").strip()
    if not external_order_id:
        raise ValueError("eBay order missing orderId.")

    sold_at = (
        _parse_ebay_datetime(ebay_order.get("creationDate"))
        or _parse_ebay_datetime(ebay_order.get("lastModifiedDate"))
        or utcnow_naive()
    )
    order_status = _map_order_status(ebay_order)
    pricing = ebay_order.get("pricingSummary") or {}
    subtotal_amount = _to_decimal((pricing.get("priceSubtotal") or {}).get("value"))
    total_amount = _to_decimal((pricing.get("total") or {}).get("value"))
    shipping_cost = _to_decimal(
        ((pricing.get("deliveryCost") or {}).get("shippingCost") or {}).get("value")
        or (pricing.get("deliveryCost") or {}).get("value")
    )
    fees = _to_decimal((pricing.get("totalMarketplaceFee") or {}).get("value"))
    if subtotal_amount == 0 and total_amount > 0:
        subtotal_amount = total_amount
    if total_amount == 0 and subtotal_amount > 0:
        total_amount = subtotal_amount

    order_items, listings_created, listing_link_count, unmapped_sku_count = _build_order_items(
        ebay_order,
        repo=repo,
        product_map=product_map,
        listing_map=listing_map,
        sku_listing_candidates=sku_listing_candidates,
        actor=actor,
    )
    line_totals_sum = _sum_line_totals(order_items)
    shipping_enrichment = _extract_shipping_enrichment(ebay_order, fulfillments=[])
    external_order_id = str(ebay_order.get("orderId") or "").strip()
    try:
        fulfillments = ebay_client.list_shipping_fulfillments(access_token=access_token, order_id=external_order_id)
        shipping_enrichment = _extract_shipping_enrichment(ebay_order, fulfillments=fulfillments)
    except Exception as exc:
        repo.add_sync_error(
            sync_run_id=sync_run_id,
            code="EBAY_ORDER_FULFILLMENT_ENRICH_FAILED",
            message=f"order_id={external_order_id}: {exc}",
            severity="warning",
        )

    existing_order = repo.db.scalar(
        select(Order).where(
            Order.marketplace == "ebay",
            Order.external_order_id == external_order_id,
        )
    )
    created_order = 0
    updated_order = 0
    if existing_order is None:
        order = repo.create_order(
            marketplace="ebay",
            sold_at=sold_at,
            items=order_items,
            external_order_id=external_order_id,
            order_status=order_status,
            fees=fees,
            shipping_cost=shipping_cost,
            notes=f"Imported from eBay sync pull. buyer={ebay_order.get('buyerUsername', '')}",
            actor=actor,
        )
        created_order = 1
    else:
        order = repo.update_order(
            existing_order.id,
            {
                "order_status": order_status,
                "sold_at": sold_at,
                "subtotal_amount": subtotal_amount,
                "total_amount": total_amount,
                "fees": fees,
                "shipping_cost": shipping_cost,
                "notes": f"Updated by eBay sync pull. buyer={ebay_order.get('buyerUsername', '')}",
            },
            actor=actor,
        )
        updated_order = 1

    existing_sales = repo.db.scalars(
        select(Sale).where(
            Sale.marketplace == "ebay",
            Sale.external_order_id == external_order_id,
        )
    ).all()
    created_sales = 0
    skipped_sales = 0
    updated_sales = 0
    if existing_sales:
        skipped_sales = len(order_items)
        for sale in existing_sales:
            updates = {}
            if shipping_enrichment.get("tracking_number"):
                updates["tracking_number"] = shipping_enrichment["tracking_number"]
            if shipping_enrichment.get("tracking_status"):
                updates["tracking_status"] = shipping_enrichment["tracking_status"]
            if shipping_enrichment.get("shipping_provider"):
                updates["shipping_provider"] = shipping_enrichment["shipping_provider"]
            if shipping_enrichment.get("shipped_at") is not None:
                updates["shipped_at"] = shipping_enrichment["shipped_at"]
            if shipping_enrichment.get("delivered_at") is not None:
                updates["delivered_at"] = shipping_enrichment["delivered_at"]
            if updates:
                repo.update_sale(sale.id, updates, actor=actor)
                updated_sales += 1
    else:
        denominator = line_totals_sum if line_totals_sum > 0 else Decimal(len(order_items))
        for item in order_items:
            qty = int(item["quantity"])
            sold_price = _to_decimal(item["unit_price"]) * qty
            weight = (
                (sold_price / denominator)
                if line_totals_sum > 0
                else (Decimal("1") / Decimal(len(order_items)))
            )
            line_fees = fees * weight
            line_shipping = shipping_cost * weight
            repo.create_sale(
                marketplace="ebay",
                sold_price=sold_price,
                fees=line_fees,
                shipping_cost=line_shipping,
                quantity_sold=qty,
                order_id=order.id,
                product_id=item.get("product_id"),
                listing_id=item.get("listing_id"),
                external_order_id=external_order_id,
                shipping_provider=shipping_enrichment.get("shipping_provider") or "",
                tracking_number=shipping_enrichment.get("tracking_number") or "",
                tracking_status=shipping_enrichment.get("tracking_status") or "",
                shipped_at=shipping_enrichment.get("shipped_at"),
                delivered_at=shipping_enrichment.get("delivered_at"),
                sold_at=sold_at,
                actor=actor,
            )
            created_sales += 1

    return {
        "orders_created": created_order,
        "orders_updated": updated_order,
        "sales_created": created_sales,
        "sales_skipped": skipped_sales,
        "sales_updated": updated_sales,
        "listings_created": listings_created,
        "line_items_with_listing_link": listing_link_count,
        "line_items_unmapped_sku": unmapped_sku_count,
    }


def execute_ebay_orders_pull_import(
    repo: InventoryRepository,
    *,
    access_token: str,
    actor: str,
    limit: int = 25,
    offset: int = 0,
    run_id: int | None = None,
    retry_of_run_id: int | None = None,
    client: EbayClient | None = None,
) -> dict:
    if not is_sync_job_enabled("ebay_orders_pull_import", repo=repo):
        raise ValueError("Sync job `ebay_orders_pull_import` is disabled by configuration.")
    ebay_client = client or EbayClient()
    resolved_access_token, refresh_token = _resolve_ebay_tokens(repo, access_token=access_token)
    if not resolved_access_token and refresh_token:
        resolved_access_token, refresh_token = _refresh_ebay_access_token(
            repo,
            ebay_client=ebay_client,
            actor=actor,
            refresh_token=refresh_token,
        )
    if not resolved_access_token.strip():
        raise ValueError("Access token is required.")
    current_access_token = resolved_access_token.strip()

    if run_id is None:
        run = repo.create_sync_run(
            provider="ebay",
            job_name="ebay_orders_pull_import",
            direction="pull",
            status="queued",
            retry_of_run_id=retry_of_run_id,
            retry_count=1 if retry_of_run_id else 0,
            notes=f"eBay pull/import run (limit={int(limit)}, offset={int(offset)}).",
            actor=actor,
        )
        run_id = run.id

    repo.update_sync_run(
        run_id,
        {
            "status": "running",
            "started_at": utcnow_naive(),
            "notes": f"eBay pull/import running (limit={int(limit)}, offset={int(offset)}).",
        },
        actor=actor,
    )

    try:
        try:
            payload = ebay_client.pull_recent_orders(current_access_token, limit=int(limit), offset=int(offset))
        except Exception as pull_exc:
            if _is_ebay_auth_error(pull_exc) and refresh_token:
                current_access_token, refresh_token = _refresh_ebay_access_token(
                    repo,
                    ebay_client=ebay_client,
                    actor=actor,
                    refresh_token=refresh_token,
                )
                payload = ebay_client.pull_recent_orders(current_access_token, limit=int(limit), offset=int(offset))
            else:
                raise
        orders = _extract_orders(payload)
        product_map = {p.sku: p.id for p in repo.db.scalars(select(Product)).all() if p.sku}
        product_id_to_sku = {pid: sku for sku, pid in product_map.items()}
        listing_map = {
            row.external_listing_id: row.id
            for row in repo.db.scalars(
                select(MarketplaceListing).where(
                    MarketplaceListing.marketplace == "ebay",
                    MarketplaceListing.external_listing_id != "",
                )
            ).all()
            if row.external_listing_id
        }
        ebay_listings = repo.db.scalars(
            select(MarketplaceListing).where(MarketplaceListing.marketplace == "ebay")
        ).all()
        sku_listing_candidates: dict[str, list[MarketplaceListing]] = {}
        for listing in ebay_listings:
            sku = product_id_to_sku.get(int(listing.product_id)) if listing.product_id is not None else None
            if not sku:
                continue
            sku_listing_candidates.setdefault(sku, []).append(listing)

        created_count = 0
        updated_count = 0
        failed_count = 0
        processed_count = 0
        line_items_with_listing_link_total = 0
        line_items_unmapped_sku_total = 0
        auto_listings_created_total = 0

        for order in orders:
            external_order_id = str(order.get("orderId") or "").strip()
            try:
                try:
                    result = _upsert_ebay_order_into_local(
                        repo,
                        order,
                        actor=actor,
                        product_map=product_map,
                        listing_map=listing_map,
                        sku_listing_candidates=sku_listing_candidates,
                        ebay_client=ebay_client,
                        access_token=current_access_token,
                        sync_run_id=run_id,
                    )
                except Exception as upsert_exc:
                    if _is_ebay_auth_error(upsert_exc) and refresh_token:
                        current_access_token, refresh_token = _refresh_ebay_access_token(
                            repo,
                            ebay_client=ebay_client,
                            actor=actor,
                            refresh_token=refresh_token,
                        )
                        result = _upsert_ebay_order_into_local(
                            repo,
                            order,
                            actor=actor,
                            product_map=product_map,
                            listing_map=listing_map,
                            sku_listing_candidates=sku_listing_candidates,
                            ebay_client=ebay_client,
                            access_token=current_access_token,
                            sync_run_id=run_id,
                        )
                    else:
                        raise
                processed_count += 1
                created_count += int(
                    result["orders_created"] + result["sales_created"] + result["listings_created"]
                )
                updated_count += int(result["orders_updated"] + result.get("sales_updated", 0))
                line_items_with_listing_link_total += int(result["line_items_with_listing_link"])
                line_items_unmapped_sku_total += int(result["line_items_unmapped_sku"])
                auto_listings_created_total += int(result["listings_created"])
                repo.add_sync_event(
                    sync_run_id=run_id,
                    entity_type="order",
                    entity_id=external_order_id,
                    action="pull_import_upsert",
                    status="ok",
                    message=(
                        f"order_created={result['orders_created']}, order_updated={result['orders_updated']}, "
                        f"sales_created={result['sales_created']}, sales_updated={result.get('sales_updated', 0)}, "
                        f"sales_skipped={result['sales_skipped']}, "
                        f"listings_created={result['listings_created']}, "
                        f"line_items_with_listing_link={result['line_items_with_listing_link']}, "
                        f"line_items_unmapped_sku={result['line_items_unmapped_sku']}"
                    ),
                    payload_json="{}",
                )
            except Exception as exc:
                failed_count += 1
                repo.add_sync_error(
                    sync_run_id=run_id,
                    code="EBAY_ORDER_IMPORT_FAILED",
                    message=f"order_id={external_order_id or '<missing>'}: {exc}",
                    severity="error",
                )
                repo.add_sync_event(
                    sync_run_id=run_id,
                    entity_type="order",
                    entity_id=external_order_id,
                    action="pull_import_upsert",
                    status="failed",
                    message=str(exc),
                    payload_json="{}",
                )

        terminal_status = "success"
        if failed_count and failed_count < len(orders):
            terminal_status = "partial"
        elif failed_count == len(orders) and len(orders) > 0:
            terminal_status = "failed"

        repo.update_sync_run(
            run_id,
            {
                "status": terminal_status,
                "records_processed": processed_count,
                "records_created": created_count,
                "records_updated": updated_count,
                "records_failed": failed_count,
                "line_items_with_listing_link": line_items_with_listing_link_total,
                "line_items_unmapped_sku": line_items_unmapped_sku_total,
                "auto_listings_created": auto_listings_created_total,
                "completed_at": utcnow_naive(),
                "notes": (
                    f"Pulled {len(orders)} order(s). "
                    f"processed={processed_count}, created={created_count}, "
                    f"updated={updated_count}, failed={failed_count}, "
                    f"line_items_with_listing_link={line_items_with_listing_link_total}, "
                    f"line_items_unmapped_sku={line_items_unmapped_sku_total}, "
                    f"auto_listings_created={auto_listings_created_total}"
                ),
            },
            actor=actor,
        )
        _notify_sync_status_slack(
            repo,
            job_name="ebay_orders_pull_import",
            run_id=run_id,
            status=terminal_status,
            processed=processed_count,
            failed=failed_count,
            actor=actor,
        )
        return {
            "run_id": run_id,
            "status": terminal_status,
            "pulled": len(orders),
            "processed": processed_count,
            "created": created_count,
            "updated": updated_count,
            "failed": failed_count,
            "line_items_with_listing_link": line_items_with_listing_link_total,
            "line_items_unmapped_sku": line_items_unmapped_sku_total,
            "auto_listings_created": auto_listings_created_total,
        }
    except Exception as exc:
        repo.add_sync_error(
            sync_run_id=run_id,
            code="EBAY_PULL_FAILED",
            message=str(exc),
            severity="error",
        )
        repo.update_sync_run(
            run_id,
            {
                "status": "failed",
                "records_processed": 0,
                "records_created": 0,
                "records_updated": 0,
                "records_failed": 1,
                "line_items_with_listing_link": 0,
                "line_items_unmapped_sku": 0,
                "auto_listings_created": 0,
                "completed_at": utcnow_naive(),
                "notes": "eBay pull import failed.",
            },
            actor=actor,
        )
        _notify_sync_status_slack(
            repo,
            job_name="ebay_orders_pull_import",
            run_id=run_id,
            status="failed",
            processed=0,
            failed=1,
            actor=actor,
        )
        raise


def _ebay_carrier_code(provider: str | None) -> str:
    raw = (provider or "").strip().lower()
    mapping = {
        "usps": "USPS",
        "ups": "UPS",
        "fedex": "FEDEX",
        "dhl": "DHL",
        "ebay_shipping": "USPS",
        "pirateship": "USPS",
    }
    return mapping.get(raw, "OTHER")


def execute_ebay_shipping_tracking_push(
    repo: InventoryRepository,
    *,
    access_token: str,
    actor: str,
    sale_ids: list[int],
    run_id: int | None = None,
    retry_of_run_id: int | None = None,
    client: EbayClient | None = None,
) -> dict:
    if not is_sync_job_enabled("ebay_shipping_tracking_push", repo=repo):
        raise ValueError("Sync job `ebay_shipping_tracking_push` is disabled by configuration.")
    ebay_client = client or EbayClient()
    resolved_access_token, refresh_token = _resolve_ebay_tokens(repo, access_token=access_token)
    if not resolved_access_token and refresh_token:
        resolved_access_token, refresh_token = _refresh_ebay_access_token(
            repo,
            ebay_client=ebay_client,
            actor=actor,
            refresh_token=refresh_token,
        )
    if not resolved_access_token.strip():
        raise ValueError("Access token is required.")
    current_access_token = resolved_access_token.strip()
    sale_ids = [int(sid) for sid in sale_ids if int(sid) > 0]
    if not sale_ids:
        raise ValueError("At least one sale ID is required.")

    if run_id is None:
        run = repo.create_sync_run(
            provider="ebay",
            job_name="ebay_shipping_tracking_push",
            direction="push",
            status="queued",
            retry_of_run_id=retry_of_run_id,
            retry_count=1 if retry_of_run_id else 0,
            notes=f"eBay tracking push run (sales={len(sale_ids)}).",
            actor=actor,
        )
        run_id = run.id

    repo.update_sync_run(
        run_id,
        {
            "status": "running",
            "started_at": utcnow_naive(),
            "notes": f"eBay tracking push running (sales={len(sale_ids)}).",
        },
        actor=actor,
    )

    processed = 0
    created = 0
    updated = 0
    failed = 0
    for sale_id in sale_ids:
        sale = repo.db.get(Sale, sale_id)
        if sale is None:
            failed += 1
            repo.add_sync_error(
                sync_run_id=run_id,
                code="EBAY_TRACKING_PUSH_SALE_NOT_FOUND",
                message=f"sale_id={sale_id} not found.",
            )
            continue

        processed += 1
        order_id = (sale.external_order_id or "").strip()
        tracking = (sale.tracking_number or "").strip()
        if (sale.marketplace or "").strip().lower() != "ebay":
            failed += 1
            repo.add_sync_error(
                sync_run_id=run_id,
                code="EBAY_TRACKING_PUSH_INVALID_MARKETPLACE",
                message=f"sale_id={sale.id} marketplace={sale.marketplace} is not ebay.",
            )
            repo.add_sync_event(
                sync_run_id=run_id,
                entity_type="sale",
                entity_id=str(sale.id),
                action="push_tracking",
                status="failed",
                message="Marketplace is not ebay.",
            )
            continue
        if not order_id or not tracking:
            failed += 1
            repo.add_sync_error(
                sync_run_id=run_id,
                code="EBAY_TRACKING_PUSH_MISSING_FIELDS",
                message=f"sale_id={sale.id} missing external_order_id or tracking_number.",
            )
            repo.add_sync_event(
                sync_run_id=run_id,
                entity_type="sale",
                entity_id=str(sale.id),
                action="push_tracking",
                status="failed",
                message="Missing external order ID or tracking number.",
            )
            continue

        try:
            def _push_once(token_value: str) -> None:
                order_payload = ebay_client.get_order(access_token=token_value, order_id=order_id)
                line_items = order_payload.get("lineItems") if isinstance(order_payload, dict) else None
                fulfillment_line_items: list[dict] = []
                if isinstance(line_items, list):
                    for item in line_items:
                        line_item_id = str(item.get("lineItemId") or "").strip()
                        if not line_item_id:
                            continue
                        quantity = int(item.get("lineItemQuantity") or item.get("quantity") or 1)
                        fulfillment_line_items.append(
                            {
                                "lineItemId": line_item_id,
                                "quantity": max(1, quantity),
                            }
                        )

                shipped_at = sale.shipped_at or utcnow_naive()
                payload = {
                    "lineItems": fulfillment_line_items,
                    "shippedDate": shipped_at.isoformat(timespec="seconds") + "Z",
                    "shippingCarrierCode": _ebay_carrier_code(sale.shipping_provider),
                    "trackingNumber": tracking,
                }
                ebay_client.create_shipping_fulfillment(
                    access_token=token_value,
                    order_id=order_id,
                    payload=payload,
                )

            try:
                _push_once(current_access_token)
            except Exception as push_exc:
                if _is_ebay_auth_error(push_exc) and refresh_token:
                    current_access_token, refresh_token = _refresh_ebay_access_token(
                        repo,
                        ebay_client=ebay_client,
                        actor=actor,
                        refresh_token=refresh_token,
                    )
                    _push_once(current_access_token)
                else:
                    raise
            updated += 1
            repo.add_sync_event(
                sync_run_id=run_id,
                entity_type="sale",
                entity_id=str(sale.id),
                action="push_tracking",
                status="ok",
                message=f"tracking pushed for order_id={order_id}",
            )
        except Exception as exc:
            failed += 1
            repo.add_sync_error(
                sync_run_id=run_id,
                code="EBAY_TRACKING_PUSH_FAILED",
                message=f"sale_id={sale.id} order_id={order_id}: {exc}",
            )
            repo.add_sync_event(
                sync_run_id=run_id,
                entity_type="sale",
                entity_id=str(sale.id),
                action="push_tracking",
                status="failed",
                message=str(exc),
            )

    status = "success"
    if failed and failed < processed:
        status = "partial"
    elif failed and failed == processed:
        status = "failed"

    repo.update_sync_run(
        run_id,
        {
            "status": status,
            "records_processed": processed,
            "records_created": created,
            "records_updated": updated,
            "records_failed": failed,
            "completed_at": utcnow_naive(),
            "notes": (
                f"eBay tracking push completed. processed={processed}, created={created}, "
                f"updated={updated}, failed={failed}."
            ),
        },
        actor=actor,
    )
    _notify_sync_status_slack(
        repo,
        job_name="ebay_shipping_tracking_push",
        run_id=run_id,
        status=status,
        processed=processed,
        failed=failed,
        actor=actor,
    )

    return {
        "run_id": run_id,
        "status": status,
        "processed": processed,
        "created": created,
        "updated": updated,
        "failed": failed,
    }
