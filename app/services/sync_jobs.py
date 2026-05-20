from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json

import requests
from sqlalchemy import func, select

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


def _is_transient_ebay_network_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    message = str(exc or "").lower()
    transient_markers = [
        "nameresolutionerror",
        "failed to resolve",
        "temporary failure in name resolution",
        "no address associated with hostname",
        "name or service not known",
        "nodename nor servname",
        "network is unreachable",
        "max retries exceeded",
        "connection aborted",
        "connection reset",
        "read timed out",
    ]
    return any(marker in message for marker in transient_markers)


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
        now = utcnow_naive()
        if access_token:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_user_access_token",
                value=access_token,
                value_type="str",
                description="Default eBay user access token used by verification and sync jobs.",
                actor=actor,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_user_access_token_refreshed_at",
                value=now.isoformat(timespec="seconds"),
                value_type="str",
                description="Timestamp when eBay user access token was most recently refreshed.",
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
                value=(now + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds"),
                value_type="str",
                description="Best-effort timestamp when eBay user access token is expected to expire.",
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


def _parse_iso_naive(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _persist_ebay_refresh_failure_state(
    repo: InventoryRepository,
    *,
    actor: str,
    error: str,
) -> None:
    try:
        now = utcnow_naive()
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="ebay_user_access_token_refresh_failed_at",
            value=now.isoformat(timespec="seconds"),
            value_type="str",
            description="Timestamp when eBay user token auto-refresh most recently failed.",
            actor=actor,
        )
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="ebay_user_access_token_refresh_last_error",
            value=str(error or "").strip()[:500],
            value_type="str",
            description="Last eBay user token auto-refresh error message.",
            actor=actor,
        )
    except Exception:
        pass


def _clear_ebay_refresh_failure_state(
    repo: InventoryRepository,
    *,
    actor: str,
) -> None:
    try:
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="ebay_user_access_token_refresh_failed_at",
            value="",
            value_type="str",
            description="Timestamp when eBay user token auto-refresh most recently failed.",
            actor=actor,
        )
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="ebay_user_access_token_refresh_last_error",
            value="",
            value_type="str",
            description="Last eBay user token auto-refresh error message.",
            actor=actor,
        )
    except Exception:
        pass


def maybe_auto_refresh_ebay_user_token(
    repo: InventoryRepository,
    *,
    actor: str,
    client: EbayClient | None = None,
    force: bool = False,
) -> dict[str, object]:
    enabled = bool(get_runtime_bool(repo, "ebay_user_token_auto_refresh_enabled", True))
    if not enabled and not force:
        return {"status": "skipped", "reason": "disabled"}

    ebay_client = client or EbayClient()
    if not ebay_client.is_configured():
        return {"status": "skipped", "reason": "client_not_configured"}

    access_token, refresh_token = _resolve_ebay_tokens(repo)
    if not str(refresh_token or "").strip():
        return {"status": "skipped", "reason": "missing_refresh_token"}

    now = utcnow_naive()
    interval_hours = max(
        1,
        min(
            72,
            int(
                get_runtime_int(
                    repo,
                    "ebay_user_token_auto_refresh_interval_hours",
                    12,
                )
            ),
        ),
    )
    min_ttl_minutes = max(
        5,
        min(
            240,
            int(
                get_runtime_int(
                    repo,
                    "ebay_user_token_auto_refresh_min_ttl_minutes",
                    45,
                )
            ),
        ),
    )
    failure_cooldown_minutes = max(
        1,
        min(
            24 * 60,
            int(
                get_runtime_int(
                    repo,
                    "ebay_user_token_auto_refresh_failure_cooldown_minutes",
                    30,
                )
            ),
        ),
    )

    expires_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_expires_at", "").strip())
    refreshed_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_refreshed_at", "").strip())
    failed_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_refresh_failed_at", "").strip())
    if failed_at is not None and not force:
        retry_at = failed_at + timedelta(minutes=failure_cooldown_minutes)
        if now < retry_at:
            return {
                "status": "skipped",
                "reason": "failure_cooldown_active",
                "retry_at": retry_at.isoformat(timespec="seconds"),
            }
    due_by_ttl = bool(expires_at is not None and (expires_at - now) <= timedelta(minutes=min_ttl_minutes))
    due_by_interval = bool(
        refreshed_at is not None and (now - refreshed_at) >= timedelta(hours=interval_hours)
    )
    due_missing_access = not bool(str(access_token or "").strip())
    due_missing_metadata = expires_at is None and refreshed_at is None

    should_refresh = bool(force or due_missing_access or due_by_ttl or due_by_interval or due_missing_metadata)
    if not should_refresh:
        return {
            "status": "skipped",
            "reason": "not_due",
            "expires_at": expires_at.isoformat(timespec="seconds") if expires_at else "",
        }

    try:
        new_access, new_refresh = _refresh_ebay_access_token(
            repo,
            ebay_client=ebay_client,
            actor=actor,
            refresh_token=str(refresh_token).strip(),
        )
    except Exception as exc:
        reason = "transient_network_unavailable" if _is_transient_ebay_network_error(exc) else "refresh_failed"
        _persist_ebay_refresh_failure_state(
            repo,
            actor=actor,
            error=str(exc or ""),
        )
        return {
            "status": "failed",
            "reason": reason,
            "error": str(exc)[:500],
        }
    _clear_ebay_refresh_failure_state(repo, actor=actor)
    new_expires_at = _parse_iso_naive(get_runtime_str(repo, "ebay_user_access_token_expires_at", "").strip())
    return {
        "status": "refreshed",
        "reason": "forced"
        if force
        else (
            "missing_access_token"
            if due_missing_access
            else ("near_expiry" if due_by_ttl else ("interval_elapsed" if due_by_interval else "missing_metadata"))
        ),
        "access_token_present": bool(new_access),
        "refresh_token_present": bool(new_refresh),
        "expires_at": new_expires_at.isoformat(timespec="seconds") if new_expires_at else "",
    }


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


def _notify_ebay_order_import_slack(
    repo: InventoryRepository,
    *,
    ebay_order: dict,
    actor: str,
) -> None:
    try:
        cfg = resolve_slack_notify_config(repo)
        if not cfg.enabled:
            return
        if not get_runtime_bool(repo, "slack_notify_order_imports", True):
            return

        order_id = str(ebay_order.get("orderId") or "").strip()
        pricing = ebay_order.get("pricingSummary") if isinstance(ebay_order.get("pricingSummary"), dict) else {}
        total_value = _to_decimal((pricing.get("total") or {}).get("value"))
        shipping_value = _to_decimal(
            ((pricing.get("deliveryCost") or {}).get("shippingCost") or {}).get("value")
            or (pricing.get("deliveryCost") or {}).get("value")
        )
        tax_value = _extract_ebay_tax_amount(pricing)
        buyer = _extract_ebay_buyer_username(ebay_order) or "(unknown)"
        shipping_service = _extract_ebay_shipping_service(ebay_order) or "(unspecified)"
        shipping_address = _extract_ebay_shipping_address(ebay_order) or "(not provided)"
        line_items = ebay_order.get("lineItems")
        line_item_count = len(line_items) if isinstance(line_items, list) else 0
        mapped_status = _map_order_status(ebay_order)
        created_at = str(ebay_order.get("creationDate") or "").strip()

        alert_text = build_slack_alert_text(
            repo,
            event_type="order_imported",
            default_template=(
                ":package: *New eBay order imported*\n"
                "- Env: `{env}`\n"
                "- Order: `{order_id}`\n"
                "- Buyer: `{buyer}`\n"
                "- Status: `{status}`\n"
                "- Total: `${total}` (shipping `${shipping}`, tax `${tax}`)\n"
                "- Items: `{line_item_count}`\n"
                "- Shipping service: `{shipping_service}`\n"
                "- Ship to: `{shipping_address}`\n"
                "- Created: `{created_at}`"
            ),
            context={
                "env": settings.app_env,
                "order_id": order_id or "(missing)",
                "buyer": buyer,
                "status": mapped_status or "not_shipped",
                "total": f"{total_value:.2f}",
                "shipping": f"{shipping_value:.2f}",
                "tax": f"{tax_value:.2f}",
                "line_item_count": line_item_count,
                "shipping_service": shipping_service,
                "shipping_address": shipping_address,
                "created_at": created_at or "(unknown)",
            },
        )
        dispatch_slack_alert(
            repo,
            actor=actor,
            event_type="order_imported",
            severity="info",
            text=alert_text,
            override_channel=str(get_runtime_str(repo, "slack_channel_order_imports", "") or "").strip(),
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
                if _is_transient_ebay_network_error(exc):
                    warnings.append(f"Transient eBay network check failed: {exc}")
                    checks.append({"name": "privileges_api", "status": "warn", "details": warnings[-1]})
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


def _extract_ebay_buyer_username(ebay_order: dict) -> str:
    buyer_raw = ebay_order.get("buyer") if isinstance(ebay_order.get("buyer"), dict) else {}
    candidates = [
        ebay_order.get("buyerUsername"),
        buyer_raw.get("username"),
        buyer_raw.get("userId"),
        buyer_raw.get("buyerId"),
        buyer_raw.get("email"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _extract_ebay_shipping_service(ebay_order: dict) -> str:
    instructions = ebay_order.get("fulfillmentStartInstructions")
    if isinstance(instructions, list):
        for row in instructions:
            if not isinstance(row, dict):
                continue
            shipping_step = row.get("shippingStep") if isinstance(row.get("shippingStep"), dict) else {}
            service_value = str(shipping_step.get("shippingServiceCode") or "").strip()
            if service_value:
                return service_value
            shipping_service = shipping_step.get("shippingService")
            if isinstance(shipping_service, dict):
                service_value = str(
                    shipping_service.get("shippingServiceCode")
                    or shipping_service.get("code")
                    or shipping_service.get("name")
                    or ""
                ).strip()
                if service_value:
                    return service_value
    pricing = ebay_order.get("pricingSummary") if isinstance(ebay_order.get("pricingSummary"), dict) else {}
    delivery_cost = pricing.get("deliveryCost") if isinstance(pricing.get("deliveryCost"), dict) else {}
    for key in ("shippingServiceCode", "shippingService", "serviceCode", "serviceName"):
        value = str(delivery_cost.get(key) or "").strip()
        if value:
            return value
    return ""


def _extract_ebay_shipping_address(ebay_order: dict) -> str:
    ship_to: dict = {}
    instructions = ebay_order.get("fulfillmentStartInstructions")
    if isinstance(instructions, list):
        for row in instructions:
            if not isinstance(row, dict):
                continue
            shipping_step = row.get("shippingStep") if isinstance(row.get("shippingStep"), dict) else {}
            candidate = shipping_step.get("shipTo")
            if isinstance(candidate, dict):
                ship_to = candidate
                break
    if not ship_to and isinstance(ebay_order.get("shippingAddress"), dict):
        ship_to = ebay_order.get("shippingAddress") or {}
    if not ship_to:
        return ""

    contact = ship_to.get("contactAddress") if isinstance(ship_to.get("contactAddress"), dict) else {}
    full_name = str(ship_to.get("fullName") or ship_to.get("name") or "").strip()
    parts = [
        full_name,
        str(contact.get("addressLine1") or ship_to.get("addressLine1") or "").strip(),
        str(contact.get("addressLine2") or ship_to.get("addressLine2") or "").strip(),
        str(contact.get("city") or ship_to.get("city") or "").strip(),
        str(contact.get("stateOrProvince") or ship_to.get("stateOrProvince") or "").strip(),
        str(contact.get("postalCode") or ship_to.get("postalCode") or "").strip(),
        str(contact.get("countryCode") or ship_to.get("countryCode") or "").strip(),
    ]
    clean = [p for p in parts if p]
    return ", ".join(clean)


def _extract_ebay_party_fields(ebay_order: dict) -> dict[str, str]:
    buyer_block = ebay_order.get("buyer") if isinstance(ebay_order.get("buyer"), dict) else {}
    instructions = ebay_order.get("fulfillmentStartInstructions")
    ship_to: dict = {}
    if isinstance(instructions, list):
        for row in instructions:
            if not isinstance(row, dict):
                continue
            shipping_step = row.get("shippingStep") if isinstance(row.get("shippingStep"), dict) else {}
            candidate = shipping_step.get("shipTo")
            if isinstance(candidate, dict):
                ship_to = candidate
                break
    contact = ship_to.get("contactAddress") if isinstance(ship_to.get("contactAddress"), dict) else {}
    reg_addr = (
        buyer_block.get("buyerRegistrationAddress")
        if isinstance(buyer_block.get("buyerRegistrationAddress"), dict)
        else {}
    )
    reg_contact = reg_addr.get("contactAddress") if isinstance(reg_addr.get("contactAddress"), dict) else {}

    buyer_username = _extract_ebay_buyer_username(ebay_order)
    buyer_name = (
        str(ship_to.get("fullName") or "").strip()
        or str(reg_addr.get("fullName") or "").strip()
        or str(buyer_block.get("username") or "").strip()
    )
    buyer_email = (
        str(ship_to.get("email") or "").strip()
        or str(reg_addr.get("email") or "").strip()
        or str(buyer_block.get("email") or "").strip()
    )

    city = str(contact.get("city") or reg_contact.get("city") or "").strip()
    state = str(contact.get("stateOrProvince") or reg_contact.get("stateOrProvince") or "").strip()
    postal = str(contact.get("postalCode") or reg_contact.get("postalCode") or "").strip()
    country = str(contact.get("countryCode") or reg_contact.get("countryCode") or "").strip().upper()
    return {
        "buyer_username": buyer_username,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "ship_to_city": city,
        "ship_to_state": state,
        "ship_to_postal_code": postal,
        "ship_to_country": country,
    }


def _extract_ebay_tax_amount(pricing: dict) -> Decimal:
    if not isinstance(pricing, dict):
        return Decimal("0")
    tax_sources = [
        pricing.get("totalTax"),
        pricing.get("salesTax"),
        pricing.get("tax"),
    ]
    for source in tax_sources:
        if isinstance(source, dict):
            value = _to_decimal(source.get("value"))
            if value > 0:
                return value
    return Decimal("0")


def _extract_ebay_fee_breakdown(pricing: dict) -> dict[str, float]:
    if not isinstance(pricing, dict):
        return {}
    payload: dict[str, float] = {}

    def _capture(name: str, raw_value: object) -> None:
        value = _to_decimal(raw_value)
        if value > 0:
            payload[name] = float(value)

    _capture("price_subtotal", (pricing.get("priceSubtotal") or {}).get("value") if isinstance(pricing.get("priceSubtotal"), dict) else None)
    _capture("delivery_cost", (pricing.get("deliveryCost") or {}).get("value") if isinstance(pricing.get("deliveryCost"), dict) else None)
    _capture(
        "delivery_shipping_cost",
        ((pricing.get("deliveryCost") or {}).get("shippingCost") or {}).get("value")
        if isinstance((pricing.get("deliveryCost") or {}).get("shippingCost"), dict)
        else None,
    )
    _capture("total_marketplace_fee", (pricing.get("totalMarketplaceFee") or {}).get("value") if isinstance(pricing.get("totalMarketplaceFee"), dict) else None)
    _capture("total_tax", (pricing.get("totalTax") or {}).get("value") if isinstance(pricing.get("totalTax"), dict) else None)
    _capture("sales_tax", (pricing.get("salesTax") or {}).get("value") if isinstance(pricing.get("salesTax"), dict) else None)
    _capture("order_total", (pricing.get("total") or {}).get("value") if isinstance(pricing.get("total"), dict) else None)

    for key, value in pricing.items():
        key_str = str(key or "").strip().lower()
        if "fee" not in key_str:
            continue
        if isinstance(value, dict):
            _capture(key_str, value.get("value"))
        elif isinstance(value, (int, float, Decimal, str)):
            _capture(key_str, value)
    return payload


def _extract_order_marketplace_fee(ebay_order: dict, pricing: dict) -> Decimal:
    fee = _to_decimal((pricing.get("totalMarketplaceFee") or {}).get("value"))
    if fee > 0:
        return fee
    # Some order payloads place marketplace fee at top-level.
    fee = _to_decimal((ebay_order.get("totalMarketplaceFee") or {}).get("value"))
    if fee > 0:
        return fee
    return Decimal("0")


def _extract_order_shipping_charged(ebay_order: dict, pricing: dict) -> Decimal:
    delivery_cost_raw = _to_decimal(
        ((pricing.get("deliveryCost") or {}).get("shippingCost") or {}).get("value")
        or (pricing.get("deliveryCost") or {}).get("value")
    )
    delivery_discount_raw = _to_decimal((pricing.get("deliveryDiscount") or {}).get("value"))
    shipping_from_pricing = delivery_cost_raw + delivery_discount_raw
    if shipping_from_pricing > 0:
        return shipping_from_pricing

    # Fallback: infer from line-level delivery blocks including handling/discount.
    line_items = ebay_order.get("lineItems") if isinstance(ebay_order.get("lineItems"), list) else []
    line_total = Decimal("0")
    for line in line_items:
        if not isinstance(line, dict):
            continue
        delivery = line.get("deliveryCost") if isinstance(line.get("deliveryCost"), dict) else {}
        shipping = _to_decimal((delivery.get("shippingCost") or {}).get("value"))
        handling = _to_decimal((delivery.get("handlingCost") or {}).get("value"))
        discount = _to_decimal((delivery.get("discountAmount") or {}).get("value"))
        candidate = shipping + handling - discount
        if candidate > 0:
            line_total += candidate
            continue
        alt = _to_decimal(line.get("lineItemShippingCost"))
        if alt > 0:
            line_total += alt
    if line_total > 0:
        return line_total
    return Decimal("0")


def _extract_line_item_fee(line: dict) -> Decimal:
    if not isinstance(line, dict):
        return Decimal("0")
    direct_candidates = [
        line.get("lineItemFee"),
        line.get("lineItemFinalValueFee"),
        line.get("lineItemMarketplaceFee"),
        line.get("marketplaceFee"),
        line.get("totalMarketplaceFee"),
    ]
    for candidate in direct_candidates:
        if isinstance(candidate, dict):
            value = _to_decimal(candidate.get("value"))
        else:
            value = _to_decimal(candidate)
        if value > 0:
            return value

    fee_rows = line.get("marketplaceFees")
    if isinstance(fee_rows, list):
        total = Decimal("0")
        for row in fee_rows:
            if isinstance(row, dict):
                total += _to_decimal((row.get("amount") or {}).get("value") if isinstance(row.get("amount"), dict) else row.get("value"))
        if total > 0:
            return total
    return Decimal("0")


def _extract_line_item_shipping(line: dict) -> Decimal:
    if not isinstance(line, dict):
        return Decimal("0")
    direct_candidates = [
        line.get("lineItemShippingCost"),
        line.get("shippingCost"),
    ]
    for candidate in direct_candidates:
        if isinstance(candidate, dict):
            value = _to_decimal(candidate.get("value"))
        else:
            value = _to_decimal(candidate)
        if value > 0:
            return value
    delivery = line.get("deliveryCost")
    if isinstance(delivery, dict):
        shipping = _to_decimal((delivery.get("shippingCost") or {}).get("value") if isinstance(delivery.get("shippingCost"), dict) else delivery.get("value"))
        handling = _to_decimal((delivery.get("handlingCost") or {}).get("value"))
        discount = _to_decimal((delivery.get("discountAmount") or {}).get("value"))
        value = shipping + handling - discount
        if value > 0:
            return value
    return Decimal("0")


def build_ebay_order_financial_diagnostics(
    ebay_order: dict,
    *,
    fulfillments: list[dict] | None = None,
    finance_transactions: list[dict] | None = None,
) -> dict[str, object]:
    pricing = ebay_order.get("pricingSummary") if isinstance(ebay_order.get("pricingSummary"), dict) else {}
    fee_breakdown = _extract_ebay_fee_breakdown(pricing)
    shipping_charged = _extract_order_shipping_charged(ebay_order, pricing)
    marketplace_fee = _extract_order_marketplace_fee(ebay_order, pricing)
    marketplace_fee_source = "order_payload"

    line_items = ebay_order.get("lineItems") if isinstance(ebay_order.get("lineItems"), list) else []
    line_fee_sum = sum((_extract_line_item_fee(line) for line in line_items), Decimal("0"))
    line_shipping_sum = sum((_extract_line_item_shipping(line) for line in line_items), Decimal("0"))
    if marketplace_fee <= 0 and line_fee_sum > 0:
        marketplace_fee = line_fee_sum
        marketplace_fee_source = "line_items"
    if shipping_charged <= 0 and line_shipping_sum > 0:
        shipping_charged = line_shipping_sum

    if marketplace_fee <= 0:
        tx_fee, _tx_fee_currency = _extract_marketplace_fee_from_transactions(
            order_id=str(ebay_order.get("orderId") or ""),
            transactions=finance_transactions or [],
        )
        if tx_fee is not None and tx_fee > 0:
            marketplace_fee = tx_fee
            marketplace_fee_source = "finance_transactions"

    shipping_label_spend, shipping_label_currency = _extract_shipping_label_spend(
        ebay_order,
        fulfillments=fulfillments or [],
    )
    shipping_label_spend_source = "fulfillment_or_order_payload"
    if shipping_label_spend is None:
        tx_spend, tx_currency = _extract_shipping_label_spend_from_transactions(
            order_id=str(ebay_order.get("orderId") or ""),
            transactions=finance_transactions or [],
        )
        if tx_spend is not None and tx_spend > 0:
            shipping_label_spend = tx_spend
            shipping_label_currency = tx_currency
            shipping_label_spend_source = "finance_transactions"
        else:
            shipping_label_spend_source = "unavailable"
    shipping_enrichment = _extract_shipping_enrichment(
        ebay_order,
        fulfillments=fulfillments or [],
    )
    return {
        "order_id": str(ebay_order.get("orderId") or "").strip(),
        "pricing_summary_present": bool(pricing),
        "line_items_count": int(len(line_items)),
        "line_fee_sum": float(line_fee_sum),
        "line_shipping_sum": float(line_shipping_sum),
        "pricing_subtotal": float(_to_decimal((pricing.get("priceSubtotal") or {}).get("value"))),
        "pricing_total": float(_to_decimal((pricing.get("total") or {}).get("value"))),
        "pricing_delivery_cost": float(_to_decimal((pricing.get("deliveryCost") or {}).get("value"))),
        "marketplace_fee_extracted": float(marketplace_fee),
        "marketplace_fee_source": marketplace_fee_source,
        "shipping_charged_extracted": float(shipping_charged),
        "shipping_label_spend_extracted": float(_to_decimal(shipping_label_spend)),
        "shipping_label_currency": str(shipping_label_currency or "USD").strip().upper() or "USD",
        "shipping_label_spend_source": shipping_label_spend_source,
        "shipping_delta_charged_minus_label_spend": float(
            shipping_charged - _to_decimal(shipping_label_spend)
        ),
        "shipping_enrichment": {
            "provider": str(shipping_enrichment.get("shipping_provider") or ""),
            "service": str(shipping_enrichment.get("shipping_service") or ""),
            "tracking_number": str(shipping_enrichment.get("tracking_number") or ""),
            "tracking_status": str(shipping_enrichment.get("tracking_status") or ""),
            "shipped_at": (
                shipping_enrichment.get("shipped_at").isoformat()
                if shipping_enrichment.get("shipped_at") is not None
                else ""
            ),
            "delivered_at": (
                shipping_enrichment.get("delivered_at").isoformat()
                if shipping_enrichment.get("delivered_at") is not None
                else ""
            ),
        },
        "fee_breakdown": fee_breakdown,
        "top_level_total_marketplace_fee": float(_to_decimal((ebay_order.get("totalMarketplaceFee") or {}).get("value"))),
        "pricing_delivery_discount": float(_to_decimal((pricing.get("deliveryDiscount") or {}).get("value"))),
        "recommended_order_writeback": {
            "fees": float(marketplace_fee),
            "shipping_cost": float(shipping_charged),
            "shipping_label_cost": float(_to_decimal(shipping_label_spend)),
            "shipping_label_currency": str(shipping_label_currency or "USD").strip().upper() or "USD",
        },
    }


def _build_ebay_sync_order_note(*, prefix: str, ebay_order: dict, pricing: dict) -> str:
    buyer = _extract_ebay_buyer_username(ebay_order) or "(unknown)"
    shipping_service = _extract_ebay_shipping_service(ebay_order)
    shipping_address = _extract_ebay_shipping_address(ebay_order)
    tax_amount = _extract_ebay_tax_amount(pricing)
    note_parts = [f"buyer={buyer}"]
    if shipping_service:
        note_parts.append(f"shipping_service={shipping_service}")
    if shipping_address:
        note_parts.append(f"ship_to={shipping_address}")
    if tax_amount > 0:
        note_parts.append(f"tax={tax_amount}")
    fee_breakdown = _extract_ebay_fee_breakdown(pricing)
    if fee_breakdown:
        note_parts.append(f"fee_breakdown_json={json.dumps(fee_breakdown, separators=(',', ':'))}")
    return f"{prefix} " + "; ".join(note_parts)


def _map_order_status(ebay_order: dict) -> str:
    status = str(ebay_order.get("orderFulfillmentStatus", "")).strip().lower()
    if status in {"delivered"}:
        return "delivered"
    if status in {"fulfilled", "shipped"}:
        return "shipped"
    if status in {"in_progress", "processing", "ready_for_shipment"}:
        return "packaging"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"payment_failed", "refunded"}:
        return "refunded"
    return "not_shipped"


def _map_tracking_status(order_fulfillment_status: str, has_tracking: bool) -> str:
    status = (order_fulfillment_status or "").strip().lower()
    if status in {"delivered"}:
        return "delivered" if has_tracking else "label_created"
    if status in {"fulfilled", "shipped", "in_progress"}:
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
    shipping_service = (
        str(best.get("shippingServiceCode") or "").strip()
        or str(best.get("shippingServiceName") or "").strip()
        or _extract_ebay_shipping_service(ebay_order)
    )
    shipped_at = _parse_ebay_datetime(best.get("shippedDate"))
    order_fulfillment_status = str(ebay_order.get("orderFulfillmentStatus") or "").strip().lower()
    row_delivery_status = " ".join(
        [
            str(best.get("deliveryStatus") or ""),
            str(best.get("shippingStatus") or ""),
            str(best.get("shipmentStatus") or ""),
            str(best.get("fulfillmentStatus") or ""),
            str(best.get("trackingStatus") or ""),
        ]
    ).strip().lower()
    has_delivered_signal = order_fulfillment_status in {"delivered"} or "deliver" in row_delivery_status
    delivered_at = None
    if has_delivered_signal:
        delivered_at = (
            _parse_ebay_datetime(best.get("deliveredDate"))
            or _parse_ebay_datetime(best.get("deliveryDate"))
        )
    if delivered_at is None and order_fulfillment_status in {"delivered"}:
        delivered_at = _parse_ebay_datetime(ebay_order.get("lastModifiedDate"))

    tracking_status = _map_tracking_status(
        str(ebay_order.get("orderFulfillmentStatus") or ""),
        has_tracking=bool(tracking_number),
    )
    return {
        "tracking_number": tracking_number,
        "shipping_provider": provider,
        "shipping_service": shipping_service,
        "tracking_status": tracking_status,
        "shipped_at": shipped_at,
        "delivered_at": delivered_at,
    }


def _extract_shipping_label_spend(
    ebay_order: dict,
    fulfillments: list[dict] | None,
) -> tuple[Decimal | None, str]:
    currency = "USD"

    def _money_from_obj(obj: object) -> tuple[Decimal, str]:
        if isinstance(obj, dict):
            if isinstance(obj.get("amount"), dict):
                nested = obj.get("amount") or {}
                value = _to_decimal(nested.get("value"))
                curr = str(nested.get("currency") or nested.get("currencyCode") or "").strip().upper()
                return value, (curr or "")
            value = _to_decimal(obj.get("value"))
            curr = str(obj.get("currency") or obj.get("currencyCode") or "").strip().upper()
            return value, (curr or "")
        return _to_decimal(obj), ""

    def _scan_payload(payload: object) -> list[tuple[Decimal, str]]:
        matches: list[tuple[Decimal, str]] = []
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_norm = str(key or "").strip().lower().replace("_", "")
                looks_like_label_spend_key = (
                    key_norm in {
                        "shippinglabelcost",
                        "labelcost",
                        "postagecost",
                        "shipmentcost",
                        "shippingcostpaid",
                    }
                    or ("label" in key_norm and ("ship" in key_norm or "postage" in key_norm))
                    or ("shippinglabel" in key_norm)
                )
                if looks_like_label_spend_key:
                    amount, curr = _money_from_obj(value)
                    if amount > 0:
                        matches.append((amount, curr))
                    if isinstance(value, list):
                        for row in value:
                            amt2, curr2 = _money_from_obj(row)
                            if amt2 > 0:
                                matches.append((amt2, curr2))
                matches.extend(_scan_payload(value))
        elif isinstance(payload, list):
            for row in payload:
                matches.extend(_scan_payload(row))
        return matches

    matches: list[tuple[Decimal, str]] = []
    matches.extend(_scan_payload(fulfillments or []))

    payment_summary = ebay_order.get("paymentSummary") if isinstance(ebay_order.get("paymentSummary"), dict) else {}
    for key in ("shippingLabelCost", "labelCost", "postageCost"):
        raw = payment_summary.get(key)
        amount, curr = _money_from_obj(raw)
        if amount > 0:
            matches.append((amount, curr))

    if not matches:
        return None, currency

    total = sum((amt for amt, _curr in matches), Decimal("0"))
    for _amt, curr in matches:
        if curr:
            currency = curr
            break
    return total, currency


def _extract_shipping_label_spend_from_transactions(
    *,
    order_id: str,
    transactions: list[dict] | None,
) -> tuple[Decimal | None, str]:
    target_order_id = str(order_id or "").strip()
    rows = transactions or []
    if not target_order_id or not isinstance(rows, list) or not rows:
        return None, "USD"

    def _money_from_obj(obj: object) -> tuple[Decimal, str]:
        if isinstance(obj, dict):
            if isinstance(obj.get("amount"), dict):
                nested = obj.get("amount") or {}
                value = _to_decimal(nested.get("value"))
                curr = str(nested.get("currency") or nested.get("currencyCode") or "").strip().upper()
                return value, (curr or "")
            value = _to_decimal(obj.get("value"))
            curr = str(obj.get("currency") or obj.get("currencyCode") or "").strip().upper()
            return value, (curr or "")
        return _to_decimal(obj), ""

    def _contains_order_id(payload: object) -> bool:
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_norm = str(key or "").strip().lower().replace("_", "")
                if key_norm in {
                    "orderid",
                    "legacyorderid",
                    "associatedorderid",
                    "orderreferenceid",
                } and str(value or "").strip() == target_order_id:
                    return True
                if _contains_order_id(value):
                    return True
            return False
        if isinstance(payload, list):
            return any(_contains_order_id(row) for row in payload)
        return False

    def _row_mentions_order_id(row: dict) -> bool:
        if _contains_order_id(row):
            return True
        try:
            text = json.dumps(row, default=str)
        except Exception:
            text = str(row)
        return target_order_id in str(text or "")

    def _collect_amount_candidates(payload: object) -> list[tuple[Decimal, str]]:
        candidates: list[tuple[Decimal, str]] = []
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_norm = str(key or "").strip().lower().replace("_", "")
                looks_like_amount = (
                    key_norm in {
                        "amount",
                        "transactionamount",
                        "feeamount",
                        "totalamount",
                        "netamount",
                        "debitamount",
                        "creditamount",
                        "grossamount",
                    }
                    or key_norm.endswith("amount")
                    or "fee" in key_norm
                )
                if looks_like_amount:
                    amount, curr = _money_from_obj(value)
                    if amount != 0:
                        candidates.append((amount, curr))
                candidates.extend(_collect_amount_candidates(value))
        elif isinstance(payload, list):
            for row in payload:
                candidates.extend(_collect_amount_candidates(row))
        return candidates

    total = Decimal("0")
    currency = "USD"
    matched = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _row_mentions_order_id(row):
            continue
        tx_type = " ".join(
            [
                str(row.get("transactionType") or ""),
                str(row.get("type") or ""),
                str(row.get("transactionSubtype") or ""),
                str(row.get("description") or ""),
                str(row.get("memo") or ""),
            ]
        ).strip().lower()
        if "label" not in tx_type and "postage" not in tx_type and "shipping" not in tx_type:
            # Keep strict to shipping/label semantics so we do not accidentally
            # use final-value fees or unrelated transactions.
            continue
        # Prefer canonical transaction amount fields first.
        canonical_amount_fields = (
            "amount",
            "transactionAmount",
            "chargeAmount",
            "debitAmount",
            "creditAmount",
            "totalFeeBasisAmount",
        )
        amount = Decimal("0")
        for field in canonical_amount_fields:
            raw = row.get(field)
            amt, _curr = _money_from_obj(raw)
            if amt != 0:
                amount = abs(_to_decimal(amt))
                break
        if amount <= 0:
            candidates = _collect_amount_candidates(row)
            if not candidates:
                continue
            amount = max((abs(_to_decimal(a)) for a, _c in candidates), default=Decimal("0"))
        if amount <= 0:
            continue
        matched = True
        total += amount
        for field in canonical_amount_fields:
            _amt, c = _money_from_obj(row.get(field))
            if c:
                currency = c
                break
    if not matched or total <= 0:
        return None, currency
    return total, currency


def _extract_marketplace_fee_from_transactions(
    *,
    order_id: str,
    transactions: list[dict] | None,
) -> tuple[Decimal | None, str]:
    target_order_id = str(order_id or "").strip()
    rows = transactions or []
    if not target_order_id or not isinstance(rows, list) or not rows:
        return None, "USD"

    def _money_from_obj(obj: object) -> tuple[Decimal, str]:
        if isinstance(obj, dict):
            value = _to_decimal(obj.get("value"))
            curr = str(obj.get("currency") or obj.get("currencyCode") or "").strip().upper()
            return value, (curr or "")
        return _to_decimal(obj), ""

    for row in rows:
        if not isinstance(row, dict):
            continue
        row_order_id = str(row.get("orderId") or "").strip()
        tx_type = str(row.get("transactionType") or "").strip().upper()
        if row_order_id != target_order_id or tx_type != "SALE":
            continue
        fee, currency = _money_from_obj(row.get("totalFeeAmount"))
        fee = abs(_to_decimal(fee))
        if fee > 0:
            return fee, (currency or "USD")
    return None, "USD"


def _build_order_finance_entries(
    *,
    order_id: int,
    external_order_id: str,
    marketplace: str,
    ebay_order: dict,
    finance_transactions: list[dict] | None,
) -> list[dict]:
    entries: list[dict] = []

    def _money_from_obj(obj: object) -> tuple[Decimal, str]:
        if isinstance(obj, dict):
            value = _to_decimal(obj.get("value"))
            curr = str(obj.get("currency") or obj.get("currencyCode") or "").strip().upper()
            return value, (curr or "")
        return _to_decimal(obj), ""

    line_lookup: dict[str, dict] = {}
    for line in ebay_order.get("lineItems") or []:
        if not isinstance(line, dict):
            continue
        line_id = str(line.get("lineItemId") or "").strip()
        if not line_id:
            continue
        line_lookup[line_id] = {
            "sku": str(line.get("sku") or "").strip(),
            "legacy_item_id": str(line.get("legacyItemId") or "").strip(),
            "title": str(line.get("title") or "").strip(),
        }

    rows = finance_transactions or []
    for tx in rows:
        if not isinstance(tx, dict):
            continue
        tx_order_id = str(tx.get("orderId") or "").strip()
        if tx_order_id and external_order_id and tx_order_id != external_order_id:
            continue
        tx_type = str(tx.get("transactionType") or "").strip().upper()
        tx_id = str(tx.get("transactionId") or "").strip()
        tx_status = str(tx.get("transactionStatus") or "").strip().upper()
        tx_memo = str(tx.get("transactionMemo") or tx.get("memo") or "").strip()
        tx_date = _parse_ebay_datetime(tx.get("transactionDate"))
        booking_entry = str(tx.get("bookingEntry") or "").strip().upper()

        if tx_type == "SHIPPING_LABEL":
            amount, currency = _money_from_obj(tx.get("amount"))
            amount = abs(_to_decimal(amount))
            if amount > 0:
                entries.append(
                    {
                        "order_id": order_id,
                        "marketplace": marketplace,
                        "external_order_id": external_order_id,
                        "transaction_id": tx_id,
                        "entry_kind": "shipping_label",
                        "fee_type": "SHIPPING_LABEL",
                        "amount": amount,
                        "currency": currency or "USD",
                        "booking_entry": booking_entry,
                        "transaction_type": tx_type,
                        "transaction_status": tx_status,
                        "transaction_date": tx_date,
                        "memo": tx_memo,
                        "source": "finance_transactions",
                        "raw": tx,
                    }
                )

        tx_line_items = tx.get("orderLineItems") or []
        if tx_type == "SALE" and isinstance(tx_line_items, list):
            for tx_line in tx_line_items:
                if not isinstance(tx_line, dict):
                    continue
                line_id = str(tx_line.get("lineItemId") or "").strip()
                line_meta = line_lookup.get(line_id) or {}
                for fee in tx_line.get("marketplaceFees") or []:
                    if not isinstance(fee, dict):
                        continue
                    amount, currency = _money_from_obj(fee.get("amount"))
                    amount = abs(_to_decimal(amount))
                    if amount <= 0:
                        continue
                    entries.append(
                        {
                            "order_id": order_id,
                            "marketplace": marketplace,
                            "external_order_id": external_order_id,
                            "transaction_id": tx_id,
                            "line_item_id": line_id,
                            "legacy_item_id": str(line_meta.get("legacy_item_id") or "").strip(),
                            "sku": str(line_meta.get("sku") or "").strip(),
                            "entry_kind": "marketplace_fee",
                            "fee_type": str(fee.get("feeType") or "").strip(),
                            "amount": amount,
                            "currency": currency or "USD",
                            "booking_entry": booking_entry,
                            "transaction_type": tx_type,
                            "transaction_status": tx_status,
                            "transaction_date": tx_date,
                            "memo": str(fee.get("feeMemo") or tx_memo).strip(),
                            "source": "finance_transactions_orderLineItems",
                            "raw": fee,
                        }
                    )

        if tx_type == "SALE":
            total_fee, currency = _money_from_obj(tx.get("totalFeeAmount"))
            total_fee = abs(_to_decimal(total_fee))
            if total_fee > 0:
                entries.append(
                    {
                        "order_id": order_id,
                        "marketplace": marketplace,
                        "external_order_id": external_order_id,
                        "transaction_id": tx_id,
                        "entry_kind": "marketplace_fee_total",
                        "fee_type": "TOTAL_MARKETPLACE_FEE",
                        "amount": total_fee,
                        "currency": currency or "USD",
                        "booking_entry": booking_entry,
                        "transaction_type": tx_type,
                        "transaction_status": tx_status,
                        "transaction_date": tx_date,
                        "memo": tx_memo,
                        "source": "finance_transactions",
                        "raw": tx,
                    }
                )
    return entries


def _derive_order_shipping_status(
    *,
    mapped_status: str,
    shipping_enrichment: dict,
) -> str:
    status = str(mapped_status or "").strip().lower()
    if status in {"cancelled", "refunded"}:
        return status
    if shipping_enrichment.get("delivered_at") is not None:
        return "delivered"
    if status == "packaging":
        return "packaging"
    has_tracking = bool(str(shipping_enrichment.get("tracking_number") or "").strip())
    has_shipped_at = shipping_enrichment.get("shipped_at") is not None
    tracking_status = str(shipping_enrichment.get("tracking_status") or "").strip().lower()
    if status == "shipped" or has_tracking or has_shipped_at or tracking_status in {"in_transit", "out_for_delivery"}:
        return "shipped"
    return "not_shipped"


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
                    "line_fees": _extract_line_item_fee(line),
                    "line_shipping": _extract_line_item_shipping(line),
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


def _reconcile_listing_status_after_sale_import(
    *,
    repo: InventoryRepository,
    listing_ids: set[int],
    actor: str,
) -> int:
    updated = 0
    if not hasattr(repo, "db") or not hasattr(repo.db, "get"):
        return 0
    for listing_id in sorted({int(v) for v in listing_ids if int(v) > 0}):
        listing = repo.db.get(MarketplaceListing, listing_id)
        if listing is None:
            continue
        qty_listed = max(0, int(getattr(listing, "quantity_listed", 0) or 0))
        if qty_listed <= 0:
            continue
        total_sold = int(
            repo.db.scalar(
                select(func.coalesce(func.sum(Sale.quantity_sold), 0)).where(Sale.listing_id == listing_id)
            )
            or 0
        )
        if total_sold < qty_listed:
            continue
        current_status = str(getattr(listing, "listing_status", "") or "").strip().lower()
        if current_status == "sold":
            continue
        try:
            repo.update_listing(listing_id, {"listing_status": "sold"}, actor=actor)
            updated += 1
        except Exception:
            if hasattr(repo.db, "rollback"):
                repo.db.rollback()
    return updated


def _hydrate_ebay_order_for_import(
    *,
    ebay_client: EbayClient,
    access_token: str,
    order: dict,
) -> dict:
    """
    Expand a row returned by list orders into the full order payload.

    The listing endpoint can omit useful fields for downstream profitability and
    shipping/customer analytics. We hydrate per-order details when possible, but
    keep import resilient by falling back to the list payload on failure.
    """
    if not isinstance(order, dict):
        return {}
    order_id = str(order.get("orderId") or "").strip()
    if not order_id:
        return dict(order)
    detail = ebay_client.get_order(access_token=access_token, order_id=order_id)
    if isinstance(detail, dict) and detail:
        return detail
    return dict(order)


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
    mapped_order_status = _map_order_status(ebay_order)
    pricing = ebay_order.get("pricingSummary") or {}
    subtotal_amount = _to_decimal((pricing.get("priceSubtotal") or {}).get("value"))
    total_amount = _to_decimal((pricing.get("total") or {}).get("value"))
    shipping_cost = _extract_order_shipping_charged(ebay_order, pricing)
    fees = _extract_order_marketplace_fee(ebay_order, pricing)
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
    line_fees_total = sum((_to_decimal(item.get("line_fees")) for item in order_items), Decimal("0"))
    line_shipping_total = sum((_to_decimal(item.get("line_shipping")) for item in order_items), Decimal("0"))
    if fees <= 0 and line_fees_total > 0:
        fees = line_fees_total
    if shipping_cost <= 0 and line_shipping_total > 0:
        shipping_cost = line_shipping_total
    shipping_enrichment = _extract_shipping_enrichment(ebay_order, fulfillments=[])
    shipping_label_cost, shipping_label_currency = _extract_shipping_label_spend(ebay_order, fulfillments=[])
    finance_transactions: list[dict] = []
    external_order_id = str(ebay_order.get("orderId") or "").strip()
    try:
        fulfillments = ebay_client.list_shipping_fulfillments(access_token=access_token, order_id=external_order_id)
        shipping_enrichment = _extract_shipping_enrichment(ebay_order, fulfillments=fulfillments)
        shipping_label_cost, shipping_label_currency = _extract_shipping_label_spend(
            ebay_order,
            fulfillments=fulfillments,
        )
    except Exception as exc:
        repo.add_sync_error(
            sync_run_id=sync_run_id,
            code="EBAY_ORDER_FULFILLMENT_ENRICH_FAILED",
            message=f"order_id={external_order_id}: {exc}",
            severity="warning",
        )
    list_finance_for_order = getattr(ebay_client, "list_finance_transactions_for_order", None)
    if callable(list_finance_for_order):
        try:
            finance_transactions = list_finance_for_order(
                access_token=access_token,
                order_id=external_order_id,
                limit=100,
            ) or []
        except Exception as exc:
            repo.add_sync_error(
                sync_run_id=sync_run_id,
                code="EBAY_ORDER_FINANCE_ENRICH_FAILED",
                message=f"order_id={external_order_id}: {exc}",
                severity="warning",
            )
            finance_transactions = []
    if fees <= 0:
        tx_fee, _tx_fee_currency = _extract_marketplace_fee_from_transactions(
            order_id=external_order_id,
            transactions=finance_transactions,
        )
        if tx_fee is not None and tx_fee > 0:
            fees = tx_fee
    if shipping_label_cost is None or _to_decimal(shipping_label_cost) <= 0:
        tx_spend, tx_currency = _extract_shipping_label_spend_from_transactions(
            order_id=external_order_id,
            transactions=finance_transactions,
        )
        if tx_spend is not None and tx_spend > 0:
            shipping_label_cost = tx_spend
            shipping_label_currency = tx_currency

    order_status = _derive_order_shipping_status(
        mapped_status=mapped_order_status,
        shipping_enrichment=shipping_enrichment,
    )
    party_fields = _extract_ebay_party_fields(ebay_order)
    marketplace_payload = dict(ebay_order or {})
    if finance_transactions:
        marketplace_payload["_finance_transactions"] = finance_transactions
    marketplace_payload_json = json.dumps(marketplace_payload, default=str)
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
            buyer_username=party_fields.get("buyer_username") or "",
            buyer_name=party_fields.get("buyer_name") or "",
            buyer_email=party_fields.get("buyer_email") or "",
            ship_to_city=party_fields.get("ship_to_city") or "",
            ship_to_state=party_fields.get("ship_to_state") or "",
            ship_to_postal_code=party_fields.get("ship_to_postal_code") or "",
            ship_to_country=party_fields.get("ship_to_country") or "",
            fees=fees,
            shipping_cost=shipping_cost,
            shipping_label_cost=shipping_label_cost,
            shipping_label_currency=shipping_label_currency,
            shipping_provider=shipping_enrichment.get("shipping_provider") or "",
            shipping_service=shipping_enrichment.get("shipping_service") or "",
            tracking_number=shipping_enrichment.get("tracking_number") or "",
            tracking_status=shipping_enrichment.get("tracking_status") or "",
            shipped_at=shipping_enrichment.get("shipped_at"),
            delivered_at=shipping_enrichment.get("delivered_at"),
            marketplace_payload_json=marketplace_payload_json,
            notes=_build_ebay_sync_order_note(
                prefix="Imported from eBay sync pull.",
                ebay_order=ebay_order,
                pricing=pricing,
            ),
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
                "buyer_username": party_fields.get("buyer_username") or "",
                "buyer_name": party_fields.get("buyer_name") or "",
                "buyer_email": party_fields.get("buyer_email") or "",
                "ship_to_city": party_fields.get("ship_to_city") or "",
                "ship_to_state": party_fields.get("ship_to_state") or "",
                "ship_to_postal_code": party_fields.get("ship_to_postal_code") or "",
                "ship_to_country": party_fields.get("ship_to_country") or "",
                "fees": fees,
                "shipping_cost": shipping_cost,
                "shipping_label_cost": shipping_label_cost,
                "shipping_label_currency": shipping_label_currency,
                "shipping_provider": shipping_enrichment.get("shipping_provider") or "",
                "shipping_service": shipping_enrichment.get("shipping_service") or "",
                "tracking_number": shipping_enrichment.get("tracking_number") or "",
                "tracking_status": shipping_enrichment.get("tracking_status") or "",
                "shipped_at": shipping_enrichment.get("shipped_at"),
                "delivered_at": shipping_enrichment.get("delivered_at"),
                "marketplace_payload_json": marketplace_payload_json,
                "notes": _build_ebay_sync_order_note(
                    prefix="Updated by eBay sync pull.",
                    ebay_order=ebay_order,
                    pricing=pricing,
                ),
            },
            actor=actor,
        )
        updated_order = 1

    replace_finance_entries = getattr(repo, "replace_order_finance_entries", None)
    if callable(replace_finance_entries):
        try:
            finance_entries = _build_order_finance_entries(
                order_id=int(order.id),
                external_order_id=external_order_id,
                marketplace="ebay",
                ebay_order=ebay_order,
                finance_transactions=finance_transactions,
            )
            replace_finance_entries(
                int(order.id),
                finance_entries,
                actor=actor,
            )
        except Exception as exc:
            repo.add_sync_error(
                sync_run_id=sync_run_id,
                code="EBAY_ORDER_FINANCE_ENTRY_PERSIST_FAILED",
                message=f"order_id={external_order_id}: {exc}",
                severity="warning",
            )

    imported_listing_ids = {
        int(item.get("listing_id"))
        for item in order_items
        if item.get("listing_id") is not None
    }
    line_items = ebay_order.get("lineItems")
    if isinstance(line_items, list):
        for line in line_items:
            if not isinstance(line, dict):
                continue
            legacy_item_id = str(line.get("legacyItemId") or "").strip()
            mapped_listing_id = listing_map.get(legacy_item_id)
            if mapped_listing_id is not None:
                imported_listing_ids.add(int(mapped_listing_id))

    existing_sales = repo.db.scalars(
        select(Sale).where(
            Sale.marketplace == "ebay",
            Sale.external_order_id == external_order_id,
        )
    ).all()
    if shipping_label_cost is None and existing_sales:
        existing_label_total = sum((_to_decimal(getattr(s, "shipping_label_cost", None)) for s in existing_sales), Decimal("0"))
        if existing_label_total > 0:
            shipping_label_cost = existing_label_total
    if fees <= 0 and existing_sales:
        existing_fee_total = sum((_to_decimal(getattr(s, "fees", None)) for s in existing_sales), Decimal("0"))
        if existing_fee_total > 0:
            fees = existing_fee_total
    if shipping_cost <= 0 and existing_sales:
        existing_shipping_total = sum((_to_decimal(getattr(s, "shipping_cost", None)) for s in existing_sales), Decimal("0"))
        if existing_shipping_total > 0:
            shipping_cost = existing_shipping_total
    created_sales = 0
    skipped_sales = 0
    updated_sales = 0
    if existing_sales:
        skipped_sales = len(order_items)
        existing_sales_denominator = sum((_to_decimal(getattr(s, "sold_price", None)) for s in existing_sales), Decimal("0"))
        existing_sales_count = len(existing_sales)
        for sale in existing_sales:
            updates = {}
            sale_price = _to_decimal(getattr(sale, "sold_price", None))
            if existing_sales_denominator > 0:
                weight = sale_price / existing_sales_denominator
            elif existing_sales_count > 0:
                weight = Decimal("1") / Decimal(existing_sales_count)
            else:
                weight = Decimal("0")
            updates["fees"] = fees * weight
            updates["shipping_cost"] = shipping_cost * weight
            if shipping_label_cost is not None:
                updates["shipping_label_cost"] = _to_decimal(shipping_label_cost) * weight
                updates["shipping_label_currency"] = shipping_label_currency
            if shipping_enrichment.get("tracking_number"):
                updates["tracking_number"] = shipping_enrichment["tracking_number"]
            if shipping_enrichment.get("tracking_status"):
                updates["tracking_status"] = shipping_enrichment["tracking_status"]
            if shipping_enrichment.get("shipping_provider"):
                updates["shipping_provider"] = shipping_enrichment["shipping_provider"]
            if shipping_enrichment.get("shipping_service"):
                updates["shipping_service"] = shipping_enrichment["shipping_service"]
            if shipping_enrichment.get("shipped_at") is not None:
                updates["shipped_at"] = shipping_enrichment["shipped_at"]
            if shipping_enrichment.get("delivered_at") is not None:
                updates["delivered_at"] = shipping_enrichment["delivered_at"]
            if updates:
                repo.update_sale(sale.id, updates, actor=actor)
                updated_sales += 1
    else:
        denominator = line_totals_sum if line_totals_sum > 0 else Decimal(len(order_items))
        known_fees = sum((_to_decimal(item.get("line_fees")) for item in order_items), Decimal("0"))
        known_shipping = sum((_to_decimal(item.get("line_shipping")) for item in order_items), Decimal("0"))
        remaining_fees = fees - known_fees
        remaining_shipping = shipping_cost - known_shipping
        if remaining_fees < 0:
            remaining_fees = Decimal("0")
        if remaining_shipping < 0:
            remaining_shipping = Decimal("0")
        fee_allocation_base = sum(
            (_to_decimal(item.get("unit_price")) * int(item.get("quantity") or 0))
            for item in order_items
            if _to_decimal(item.get("line_fees")) <= 0
        )
        ship_allocation_base = sum(
            (_to_decimal(item.get("unit_price")) * int(item.get("quantity") or 0))
            for item in order_items
            if _to_decimal(item.get("line_shipping")) <= 0
        )
        for item in order_items:
            qty = int(item["quantity"])
            sold_price = _to_decimal(item["unit_price"]) * qty
            weight = (sold_price / denominator) if line_totals_sum > 0 else (Decimal("1") / Decimal(len(order_items)))
            if _to_decimal(item.get("line_fees")) > 0:
                line_fees = _to_decimal(item.get("line_fees"))
            else:
                fee_weight = (
                    (sold_price / fee_allocation_base)
                    if fee_allocation_base > 0
                    else weight
                )
                line_fees = remaining_fees * fee_weight
            if _to_decimal(item.get("line_shipping")) > 0:
                line_shipping = _to_decimal(item.get("line_shipping"))
            else:
                ship_weight = (
                    (sold_price / ship_allocation_base)
                    if ship_allocation_base > 0
                    else weight
                )
                line_shipping = remaining_shipping * ship_weight
            line_label_spend = Decimal("0")
            if shipping_label_cost is not None:
                line_label_spend = _to_decimal(shipping_label_cost) * weight
            repo.create_sale(
                marketplace="ebay",
                sold_price=sold_price,
                fees=line_fees,
                shipping_cost=line_shipping,
                shipping_label_cost=line_label_spend,
                shipping_label_currency=shipping_label_currency,
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
        label_cost_total = sum((_to_decimal(getattr(s, "shipping_label_cost", None)) for s in repo.db.scalars(
            select(Sale).where(
                Sale.marketplace == "ebay",
                Sale.external_order_id == external_order_id,
            )
        ).all()), Decimal("0"))
        effective_label_cost = shipping_label_cost if shipping_label_cost is not None else (label_cost_total if label_cost_total > 0 else None)
        repo.update_order(
            order.id,
            {
                "fees": fees,
                "shipping_label_cost": effective_label_cost,
                "shipping_label_currency": shipping_label_currency,
            },
            actor=actor,
        )

    listings_marked_sold = _reconcile_listing_status_after_sale_import(
        repo=repo,
        listing_ids=imported_listing_ids,
        actor=actor,
    )

    return {
        "orders_created": created_order,
        "orders_updated": updated_order,
        "sales_created": created_sales,
        "sales_skipped": skipped_sales,
        "sales_updated": updated_sales,
        "listings_created": listings_created,
        "line_items_with_listing_link": listing_link_count,
        "line_items_unmapped_sku": unmapped_sku_count,
        "listings_marked_sold": listings_marked_sold,
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
            elif _is_transient_ebay_network_error(pull_exc):
                repo.add_sync_error(
                    sync_run_id=run_id,
                    code="EBAY_NETWORK_UNAVAILABLE",
                    message=str(pull_exc),
                    severity="warning",
                )
                repo.add_sync_event(
                    sync_run_id=run_id,
                    entity_type="ebay_orders",
                    entity_id="pull",
                    action="pull_orders",
                    status="warning",
                    message=f"Transient eBay network failure; import skipped: {pull_exc}",
                    payload_json="{}",
                )
                repo.update_sync_run(
                    run_id,
                    {
                        "status": "skipped",
                        "records_processed": 0,
                        "records_created": 0,
                        "records_updated": 0,
                        "records_failed": 0,
                        "line_items_with_listing_link": 0,
                        "line_items_unmapped_sku": 0,
                        "auto_listings_created": 0,
                        "completed_at": utcnow_naive(),
                        "notes": "eBay pull import skipped due to transient network/DNS failure.",
                    },
                    actor=actor,
                )
                return {
                    "run_id": run_id,
                    "status": "skipped",
                    "pulled": 0,
                    "processed": 0,
                    "created": 0,
                    "updated": 0,
                    "failed": 0,
                    "line_items_with_listing_link": 0,
                    "line_items_unmapped_sku": 0,
                    "auto_listings_created": 0,
                    "reason": "transient_network_unavailable",
                }
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
                hydrated_order = order
                try:
                    hydrated_order = _hydrate_ebay_order_for_import(
                        ebay_client=ebay_client,
                        access_token=current_access_token,
                        order=order,
                    )
                except Exception as hydrate_exc:
                    if _is_ebay_auth_error(hydrate_exc) and refresh_token:
                        current_access_token, refresh_token = _refresh_ebay_access_token(
                            repo,
                            ebay_client=ebay_client,
                            actor=actor,
                            refresh_token=refresh_token,
                        )
                        hydrated_order = _hydrate_ebay_order_for_import(
                            ebay_client=ebay_client,
                            access_token=current_access_token,
                            order=order,
                        )
                    else:
                        hydrated_order = order
                        repo.add_sync_event(
                            sync_run_id=run_id,
                            entity_type="order",
                            entity_id=external_order_id,
                            action="pull_order_hydrate",
                            status="warning",
                            message=f"detail hydration failed; using list payload: {hydrate_exc}",
                            payload_json="{}",
                        )
                try:
                    result = _upsert_ebay_order_into_local(
                        repo,
                        hydrated_order,
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
                            hydrated_order,
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
                if int(result.get("orders_created") or 0) > 0:
                    _notify_ebay_order_import_slack(
                        repo,
                        ebay_order=hydrated_order,
                        actor=actor,
                    )
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
