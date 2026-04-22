import base64
import json
import re
import traceback
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse
from typing import Any

import hashlib
import requests

from app.config import settings
from app.db.models import IntegrationQueueJob, Product, Sale
from app.services.google_workspace import (
    create_calendar_event,
    resolve_google_workspace_config,
    send_gmail_message,
    upload_drive_file,
)
from app.services.runtime_settings import get_runtime_bool, get_runtime_float, get_runtime_int, get_runtime_str
from app.services.integration_automation import evaluate_and_apply_rules_for_job
from app.services.shipping_labels import purchase_shipping_label
from app.services.slack_notify import (
    build_slack_alert_text,
    dispatch_slack_alert,
    resolve_slack_notify_config,
    send_slack_message,
)
from app.utils.time import utcnow_naive


def _calc_backoff_seconds(repo: Any, retry_count: int, *, integration: str) -> int:
    normalized = str(integration or "").strip().lower()
    if normalized == "slack":
        base_key = "slack_queue_backoff_base_seconds"
        max_key = "slack_queue_backoff_max_seconds"
        base_default = 60
        max_default = 3600
    elif normalized == "shipping":
        base_key = "shipping_queue_backoff_base_seconds"
        max_key = "shipping_queue_backoff_max_seconds"
        base_default = 60
        max_default = 3600
    else:
        base_key = "google_queue_backoff_base_seconds"
        max_key = "google_queue_backoff_max_seconds"
        base_default = 120
        max_default = 3600
    base_seconds = max(5, min(3600, get_runtime_int(repo, base_key, base_default)))
    max_seconds = max(base_seconds, min(86400, get_runtime_int(repo, max_key, max_default)))
    seconds = base_seconds * (2 ** max(0, int(retry_count)))
    return min(max_seconds, seconds)


def _capture_queue_execute_exception(
    repo: Any,
    *,
    actor: str,
    job: IntegrationQueueJob,
    exc: Exception,
) -> str:
    message = str(exc)[:2000] or exc.__class__.__name__
    try:
        repo.log_integration_event(
            actor=actor,
            integration=f"{job.integration}_queue",
            action=f"{job.action}_execute_exception",
            status="error",
            details={
                "queue_job_id": int(job.id),
                "retry_count": int(job.retry_count or 0),
                "error": message[:500],
                "exception_type": exc.__class__.__name__,
                "traceback": traceback.format_exc(limit=25)[:4000],
            },
        )
    except Exception:
        pass
    return message


def _emit_terminal_queue_failure_alert(
    repo: Any,
    *,
    actor: str,
    job: IntegrationQueueJob,
    retry_count: int,
    error_text: str,
) -> None:
    try:
        slack_cfg = resolve_slack_notify_config(repo)
        if not slack_cfg.enabled:
            return
        enabled_general = get_runtime_bool(repo, "slack_notify_integration_queue_failures", True)
        enabled_google_legacy = get_runtime_bool(repo, "slack_notify_google_queue_failures", True)
        if str(job.integration or "").strip().lower() == "google":
            if not (enabled_general or enabled_google_legacy):
                return
        elif not enabled_general:
            return
        alert_text = build_slack_alert_text(
            repo,
            event_type="integration_queue_failures",
            default_template=(
                ":warning: *GoldenStackers* integration queue job failed permanently\n"
                "- Env: `{env}`\n"
                "- Integration: `{integration}`\n"
                "- Job: `#{job_id}` `{action}`\n"
                "- Retries: `{retry_count}/{max_retries}`\n"
                "- Error: `{error}`"
            ),
            context={
                "env": settings.app_env,
                "integration": str(job.integration or ""),
                "job_id": int(job.id),
                "action": str(job.action or ""),
                "retry_count": int(retry_count),
                "max_retries": int(job.max_retries or 0),
                "error": str(error_text or "")[:280],
            },
        )
        dispatch_slack_alert(
            repo,
            actor=actor,
            event_type="integration_queue_failures",
            severity="error",
            text=alert_text,
        )
    except Exception:
        pass


def execute_integration_queue_job(repo: Any, job: Any, *, actor: str) -> tuple[bool, str]:
    integration = str(getattr(job, "integration", "") or "").strip().lower()
    action = str(getattr(job, "action", "") or "").strip().lower()
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(str(getattr(job, "payload_json", "") or "{}"))
    except Exception:
        payload = {}

    def _coerce_int(v: Any) -> int | None:
        try:
            if v in (None, ""):
                return None
            return int(v)
        except Exception:
            return None

    def _download_slack_file_bytes(file_row: dict[str, Any], *, timeout_seconds: int) -> tuple[bytes, str]:
        file_name = str(file_row.get("name") or "slack_file").strip() or "slack_file"
        override_b64 = str(file_row.get("content_b64") or "").strip()
        if override_b64:
            return base64.b64decode(override_b64), file_name
        url = str(file_row.get("url_private_download") or file_row.get("url_private") or "").strip()
        if not url:
            raise ValueError(f"Missing url_private for file `{file_name}`.")
        slack_cfg = resolve_slack_notify_config(repo)
        if not slack_cfg.bot_token:
            raise ValueError("Slack bot token is not configured (`slack_bot_token`) for file ingestion.")
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {slack_cfg.bot_token}"},
            timeout=max(3, min(int(timeout_seconds), 120)),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Slack file download HTTP error {resp.status_code} for `{file_name}`.")
        return bytes(resp.content or b""), file_name

    def _ingest_slack_ops_command() -> tuple[bool, str]:
        if action != "command_ingest":
            return False, f"Unsupported slack_ops action `{action}`."
        command_payload = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        raw_payload = command_payload.get("raw_payload") if isinstance(command_payload.get("raw_payload"), dict) else {}
        intent = str(command_payload.get("intent") or "").strip().lower()
        args = command_payload.get("args") if isinstance(command_payload.get("args"), list) else []
        normalized_args = [str(v).strip() for v in args if str(v).strip()]
        query_text = str(command_payload.get("command_text") or raw_payload.get("query") or "").strip()
        file_rows = command_payload.get("files") if isinstance(command_payload.get("files"), list) else []
        request_context = payload.get("request_context") if isinstance(payload.get("request_context"), dict) else {}
        target_channel = str(request_context.get("channel_id") or "").strip()
        target_thread_ts = str(
            request_context.get("thread_ts")
            or request_context.get("message_ts")
            or ""
        ).strip()
        request_actor = (
            str(request_context.get("app_username") or request_context.get("slack_username") or actor).strip() or actor
        )

        def _extract_json_object(raw_text: str) -> dict[str, Any]:
            text = str(raw_text or "").strip()
            if not text:
                return {}
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return {}
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _safe_decimal(v: Any) -> Decimal | None:
            try:
                if v in (None, ""):
                    return None
                return Decimal(str(v))
            except Exception:
                return None

        def _safe_int(v: Any) -> int | None:
            try:
                if v in (None, ""):
                    return None
                return int(v)
            except Exception:
                return None

        def _parse_override_pairs(parts: list[str]) -> dict[str, str]:
            out: dict[str, str] = {}
            for token in parts:
                normalized = str(token or "").strip()
                if "=" not in normalized:
                    continue
                key, value = normalized.split("=", 1)
                k = str(key or "").strip().lower()
                v = str(value or "").strip()
                if k and v:
                    out[k] = v
            return out

        def _parse_bool_override(raw_value: Any) -> bool | None:
            if raw_value is None:
                return None
            token = str(raw_value or "").strip().lower()
            if token in {"1", "true", "yes", "on"}:
                return True
            if token in {"0", "false", "no", "off"}:
                return False
            return None

        def _remove_override_tokens_from_text(raw_text: str, overrides: dict[str, str]) -> str:
            text = " ".join(str(raw_text or "").split()).strip()
            if not text:
                return ""
            for key, value in (overrides or {}).items():
                pair = f"{key}={value}".strip()
                if not pair:
                    continue
                text = re.sub(rf"(?i)\b{re.escape(pair)}\b", " ", text)
            return " ".join(text.split()).strip()

        def _build_intake_sku(*, category: str, metal_type: str) -> str:
            category_seed = "".join(ch for ch in str(category or "oth").lower() if ch.isalnum())[:3] or "oth"
            metal_seed = "".join(ch for ch in str(metal_type or "mix").lower() if ch.isalnum())[:3] or "mix"
            date_seed = utcnow_naive().strftime("%y%m%d")
            rand_seed = uuid4().hex[:4].upper()
            return f"SLK-{category_seed.upper()}-{metal_seed.upper()}-{date_seed}-{rand_seed}"

        def _comp_query_variants(raw_query: str) -> list[str]:
            base = " ".join(str(raw_query or "").replace("-", " ").replace("_", " ").split()).strip()
            if not base:
                return []
            lowered = base.lower()
            replacements = {
                "ampex": "apmex",
                "1oz": "1 oz",
                ".9999": "9999",
                ".999": "999",
            }
            normalized = lowered
            for src, dst in replacements.items():
                normalized = normalized.replace(src, dst)
            tokens = [tok for tok in normalized.split(" ") if tok]
            variants: list[str] = []
            if tokens:
                variants.append(" ".join(tokens))
            if "silver" in tokens and "round" not in tokens:
                variants.append(" ".join(tokens + ["round"]))
            if "silver" in tokens and "bar" not in tokens:
                variants.append(" ".join(tokens + ["bar"]))
            if "silver" in tokens and "bullion" not in tokens:
                variants.append(" ".join(tokens + ["bullion"]))
            if len(tokens) > 4:
                variants.append(" ".join(tokens[:4]))
            if len(tokens) > 3:
                variants.append(" ".join(tokens[:3]))
            deduped: list[str] = []
            seen: set[str] = set()
            for item in variants:
                q = " ".join(str(item or "").split()).strip()
                if not q:
                    continue
                if q in seen:
                    continue
                seen.add(q)
                deduped.append(q)
            return deduped[:8]

        def _comp_row_effective_price(row: dict[str, Any]) -> float:
            sold = _safe_decimal(row.get("sold_price"))
            if sold is not None and sold > 0:
                ship = _safe_decimal(row.get("shipping_cost")) or Decimal("0")
                return float(sold + ship)
            listed = _safe_decimal(row.get("listed_price"))
            if listed is not None and listed > 0:
                ship = _safe_decimal(row.get("shipping_cost")) or Decimal("0")
                return float(listed + ship)
            total = _safe_decimal(row.get("total_price"))
            if total is not None and total > 0:
                return float(total)
            return 0.0

        def _comp_row_confidence_score(row: dict[str, Any]) -> float:
            score = 0.0
            effective_price = _comp_row_effective_price(row)
            if effective_price > 0:
                score += 1.0
            sold_price = _safe_decimal(row.get("sold_price"))
            if sold_price is not None and sold_price > 0:
                score += 4.0
            domain = str(row.get("source_domain") or "").strip().lower()
            url = str(row.get("item_url") or row.get("view_item_url") or row.get("url") or "").strip()
            if not domain and url:
                domain = (urlparse(url).netloc or "").lower()
            domain = domain.removeprefix("www.")
            preferred_bullion_domains = {
                "apmex.com",
                "jmbullion.com",
                "sdbullion.com",
                "monumentmetals.com",
                "providentmetals.com",
                "silver.com",
                "goldeneaglecoin.com",
                "bullionexchanges.com",
                "herobullion.com",
            }
            if domain == "ebay.com":
                score += 3.0
            if domain in preferred_bullion_domains:
                score += 1.5
            if _is_product_like_web_row(row):
                score += 2.0
            hint_source = str(row.get("price_hint_source") or "").strip().lower()
            if hint_source == "structured_page":
                score += 2.5
            elif hint_source == "snippet_or_url":
                score += 0.5
            return score

        def _comp_confidence_label(score: float) -> str:
            value = float(score or 0.0)
            if value >= 6.0:
                return "high"
            if value >= 3.5:
                return "medium"
            return "low"

        def _default_trusted_comp_web_domains() -> set[str]:
            return {
                "apmex.com",
                "jmbullion.com",
                "sdbullion.com",
                "monumentmetals.com",
                "providentmetals.com",
                "silver.com",
                "goldeneaglecoin.com",
                "bullionexchanges.com",
                "herobullion.com",
            }

        def _trusted_comp_web_domains() -> set[str]:
            csv_value = str(get_runtime_str(repo, "slack_ops_comp_trusted_web_domains_csv", "") or "").strip()
            if not csv_value:
                return _default_trusted_comp_web_domains()
            out: set[str] = set()
            for raw in csv_value.split(","):
                token = str(raw or "").strip().lower().removeprefix("www.")
                if token:
                    out.add(token)
            return out or _default_trusted_comp_web_domains()

        def _parse_domains_csv(raw_csv: Any) -> set[str]:
            text = str(raw_csv or "").strip()
            if not text:
                return set()
            out: set[str] = set()
            for raw in text.split(","):
                token = str(raw or "").strip().lower().removeprefix("www.")
                token = token.split("/", 1)[0].strip()
                if token:
                    out.add(token)
            return out

        def _filter_comp_web_rows_by_trust(
            web_rows: list[dict[str, Any]],
            *,
            trusted_only: bool,
            trusted_domains: set[str],
        ) -> list[dict[str, Any]]:
            rows = list(web_rows or [])
            if not trusted_only:
                return rows
            if not rows:
                return []
            filtered: list[dict[str, Any]] = []
            for row in rows:
                domain = str(row.get("source_domain") or "").strip().lower().removeprefix("www.")
                if not domain:
                    url = str(row.get("item_url") or row.get("view_item_url") or row.get("url") or "").strip()
                    if url:
                        domain = (urlparse(url).netloc or "").lower().removeprefix("www.")
                if domain in trusted_domains:
                    filtered.append(row)
            return filtered

        def _is_product_like_web_row(row: dict[str, Any]) -> bool:
            url = str(row.get("item_url") or row.get("view_item_url") or row.get("url") or "").strip()
            if not url:
                return False
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
            if not path:
                return False
            host = host.removeprefix("www.")
            if any(
                token in path
                for token in (
                    "/category/",
                    "/categories/",
                    "/search",
                    "/collections/",
                    "/deals",
                    "/sale",
                )
            ):
                return False
            if any(
                token in path
                for token in (
                    "/search",
                    "/collections",
                    "/blog",
                    "/learn",
                    "/guides",
                    "/news",
                )
            ):
                return False
            first_segment = ""
            path_parts = [part for part in str(path or "").split("/") if part]
            if path_parts:
                first_segment = str(path_parts[0] or "").strip().lower()
            bullion_hosts = {
                "jmbullion.com",
                "sdbullion.com",
                "monumentmetals.com",
                "providentmetals.com",
                "silver.com",
                "goldeneaglecoin.com",
                "bullionexchanges.com",
                "herobullion.com",
            }
            category_root_segments = {
                "silver",
                "gold",
                "platinum",
                "palladium",
                "copper",
                "bullion",
                "coins",
                "bars",
                "rounds",
                "deals",
                "sale",
                "specials",
            }
            if host in bullion_hosts:
                if first_segment in category_root_segments:
                    return False
                if any(
                    token in path
                    for token in (
                        "/product/",
                        "-oz-",
                        "-gram-",
                        "-kilo-",
                        "-kg-",
                        "-lb-",
                        "-coin-",
                        "-bar-",
                        "-round-",
                    )
                ):
                    return True
                if len(path_parts) == 1 and len(first_segment) >= 8:
                    return True
                return False
            if "apmex.com" in host:
                return "/product/" in path
            if "ebay.com" in host:
                return "/itm/" in path
            if any(token in path for token in ("/product/", "/item/", "/itm/", "/p/")):
                return True
            # Generic-host fallback: treat single-segment slugs as likely product pages,
            # but avoid known non-product section roots.
            if len(path_parts) == 1 and first_segment and first_segment not in category_root_segments:
                return True
            return False

        def _prefer_product_like_web_rows(web_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = list(web_rows or [])
            if not rows:
                return []
            priced_product_rows = [
                row
                for row in rows
                if _comp_row_effective_price(row) > 0 and _is_product_like_web_row(row)
            ]
            if not priced_product_rows:
                return rows
            return priced_product_rows

        def _build_top_comp_snippet(
            ebay_rows: list[dict[str, Any]],
            web_rows: list[dict[str, Any]],
        ) -> str:
            combined = list(ebay_rows or []) + _prefer_product_like_web_rows(list(web_rows or []))
            if not combined:
                return ""
            priced = [r for r in combined if _comp_row_effective_price(r) > 0]
            targets = priced if priced else combined
            targets = sorted(
                targets,
                key=lambda r: (_comp_row_confidence_score(r), _comp_row_effective_price(r)),
                reverse=True,
            )[:3]
            parts: list[str] = []
            for idx, row in enumerate(targets, start=1):
                title = str(row.get("title") or row.get("item_title") or "comp").strip()
                title = title[:80] + ("…" if len(title) > 80 else "")
                price = _comp_row_effective_price(row)
                confidence = _comp_confidence_label(_comp_row_confidence_score(row))
                url = str(row.get("item_url") or row.get("view_item_url") or row.get("url") or "").strip()
                if price > 0 and url:
                    parts.append(f"{idx}) [{confidence}] {title} - ${price:,.2f} - {url}")
                elif price > 0:
                    parts.append(f"{idx}) [{confidence}] {title} - ${price:,.2f}")
                elif url:
                    parts.append(f"{idx}) [{confidence}] {title} - {url}")
                else:
                    parts.append(f"{idx}) [{confidence}] {title}")
            return "Top comps: " + " | ".join(parts)

        def _build_comp_stats_snippet(
            ebay_rows: list[dict[str, Any]],
            web_rows: list[dict[str, Any]],
            *,
            band_low_pct: float,
            band_high_pct: float,
        ) -> str:
            combined = list(ebay_rows or []) + _prefer_product_like_web_rows(list(web_rows or []))
            priced_rows = [r for r in combined if _comp_row_effective_price(r) > 0]
            prices = sorted([_comp_row_effective_price(r) for r in priced_rows])
            if not prices:
                return ""
            def _comp_row_domain(row: dict[str, Any]) -> str:
                domain = str(row.get("domain") or row.get("source_domain") or "").strip().lower().removeprefix("www.")
                if domain:
                    return domain
                url = str(row.get("item_url") or row.get("view_item_url") or row.get("url") or "").strip()
                if not url:
                    sold = _safe_decimal(row.get("sold_price"))
                    if sold is not None and sold > 0:
                        return "ebay"
                    return ""
                return str(urlparse(url).netloc or "").strip().lower().removeprefix("www.")

            distinct_sources = len({d for d in (_comp_row_domain(row) for row in priced_rows) if d})
            count = len(prices)
            mid = count // 2
            median = prices[mid] if count % 2 == 1 else (prices[mid - 1] + prices[mid]) / 2.0
            low = prices[0]
            high = prices[-1]
            suggested_low = median * max(0.01, float(band_low_pct))
            suggested_high = median * max(float(band_low_pct), float(band_high_pct))
            _score, overall_confidence = _comp_overall_confidence(ebay_rows, web_rows)
            return (
                "Comps: "
                f"{count} | Qualified comps: {count} | Median ${median:,.2f} | Range ${low:,.2f}-${high:,.2f} | "
                f"Suggested list band ${suggested_low:,.2f}-${suggested_high:,.2f} | Distinct sources: {distinct_sources} | "
                f"Evidence confidence {overall_confidence}"
            )

        def _comp_overall_confidence(rows_ebay: list[dict[str, Any]], rows_web: list[dict[str, Any]]) -> tuple[float, str]:
            combined_rows = list(rows_ebay or []) + _prefer_product_like_web_rows(list(rows_web or []))
            qualified_rows = [row for row in combined_rows if _comp_row_effective_price(row) > 0]
            confidence_values = [_comp_row_confidence_score(row) for row in qualified_rows]
            if not qualified_rows:
                return 0.0, "low"
            avg_score = float(sum(confidence_values) / float(len(confidence_values)))
            def _comp_row_domain(row: dict[str, Any]) -> str:
                domain = str(row.get("domain") or "").strip().lower().removeprefix("www.")
                if domain:
                    return domain
                url = str(row.get("item_url") or row.get("view_item_url") or row.get("url") or "").strip()
                if not url:
                    sold = _safe_decimal(row.get("sold_price"))
                    if sold is not None and sold > 0:
                        return "ebay"
                    return ""
                return str(urlparse(url).netloc or "").strip().lower().removeprefix("www.")

            distinct_domains = {d for d in (_comp_row_domain(row) for row in qualified_rows) if d}
            qualified_count = len(qualified_rows)
            adjusted_score = avg_score
            if qualified_count <= 1:
                adjusted_score = min(adjusted_score, 5.9)
            elif qualified_count <= 2 and len(distinct_domains) <= 1:
                adjusted_score = min(adjusted_score, 5.9)
            return adjusted_score, _comp_confidence_label(adjusted_score)

        def _comp_qualified_row_count(rows_ebay: list[dict[str, Any]], rows_web: list[dict[str, Any]]) -> int:
            combined_rows = list(rows_ebay or []) + _prefer_product_like_web_rows(list(rows_web or []))
            return int(sum(1 for row in combined_rows if _comp_row_effective_price(row) > 0))

        def _extract_price_hints_simple(raw_text: str) -> list[float]:
            text = unescape(str(raw_text or "")).replace("\xa0", " ").strip()
            if not text:
                return []
            threshold_prices: set[float] = set()

            threshold_matches = re.finditer(
                r"(?i)(?:US\$|USD|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
                text,
            )
            for match in threshold_matches:
                raw = str(match.group(1) or "")
                try:
                    candidate = float(raw.replace(",", "").strip())
                except Exception:
                    continue
                start_idx = int(match.start())
                end_idx = int(match.end())
                before = text[max(0, start_idx - 40):start_idx].lower()
                after = text[end_idx:min(len(text), end_idx + 20)].lower()
                looks_like_threshold = bool(
                    re.search(r"(?:orders?|minimum|min)\s*(?:of|over|above)?\s*$", before)
                    or (
                        ("shipping" in before or "shipping" in after)
                        and ("order" in before or "order" in after or "+" in after)
                    )
                )
                if looks_like_threshold:
                    threshold_prices.add(candidate)

            matches = re.finditer(
                r"(?i)(?:US\$|USD|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
                text,
            )
            prices: list[float] = []
            for match in matches:
                raw = str(match.group(1) or "")
                try:
                    prices.append(float(str(raw).replace(",", "").strip()))
                except Exception:
                    continue
            deduped: list[float] = []
            seen: set[float] = set()
            for value in prices:
                if value <= 0 or value in seen or value in threshold_prices:
                    continue
                seen.add(value)
                deduped.append(value)
            return deduped

        def _resolve_web_result_url(raw_href: str) -> str:
            href = unescape(str(raw_href or "")).strip()
            if not href:
                return ""
            if href.startswith("//"):
                href = "https:" + href
            parsed = urlparse(href)
            host = (parsed.netloc or "").lower()
            if "duckduckgo.com" not in host:
                return href
            try:
                qs = parse_qs(parsed.query or "")
                uddg_values = qs.get("uddg") or []
                if uddg_values:
                    return unquote(str(uddg_values[0] or "")).strip() or href
            except Exception:
                return href
            return href

        def _extract_structured_page_prices(raw_html: str) -> list[float]:
            html_text = str(raw_html or "")
            
            def _parse_price_token(raw_token: Any) -> float | None:
                token = str(raw_token or "").strip()
                if not token:
                    return None
                match = re.search(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)", token)
                if not match:
                    return None
                try:
                    value = float(str(match.group(1) or "").replace(",", "").strip())
                except Exception:
                    return None
                if value <= 0:
                    return None
                return value

            def _extract_json_ld_prices(text: str) -> list[float]:
                scripts = re.findall(
                    r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    text,
                )
                prices: list[float] = []
                seen_local: set[float] = set()

                def _add_price(candidate: Any) -> None:
                    value = _parse_price_token(candidate)
                    if value is None or value in seen_local:
                        return
                    seen_local.add(value)
                    prices.append(value)

                def _walk(node: Any, *, in_offer: bool = False, in_product: bool = False) -> None:
                    if isinstance(node, list):
                        for child in node:
                            _walk(child, in_offer=in_offer, in_product=in_product)
                        return
                    if not isinstance(node, dict):
                        return
                    node_type_raw = node.get("@type")
                    if isinstance(node_type_raw, list):
                        node_types = {str(v or "").strip().lower() for v in node_type_raw}
                    else:
                        node_types = {str(node_type_raw or "").strip().lower()}
                    is_offer_node = any("offer" in t for t in node_types if t)
                    is_product_node = any("product" in t for t in node_types if t)
                    next_in_offer = bool(in_offer or is_offer_node or ("offers" in node))
                    next_in_product = bool(in_product or is_product_node)
                    for raw_key, value in node.items():
                        key = str(raw_key or "").strip().lower()
                        if key in {
                            "price",
                            "pricevalue",
                            "saleprice",
                            "currentprice",
                        } and (next_in_offer or next_in_product):
                            _add_price(value)
                        if key == "offers":
                            _walk(value, in_offer=True, in_product=next_in_product)
                        elif isinstance(value, (dict, list)):
                            _walk(value, in_offer=next_in_offer, in_product=next_in_product)

                for script_body in scripts:
                    body = str(script_body or "").strip()
                    if not body:
                        continue
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        continue
                    _walk(parsed)
                return prices

            candidates: list[str] = []
            candidates.extend(
                re.findall(
                    r'(?is)<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([0-9]+(?:\.[0-9]{1,2})?)["\']',
                    html_text,
                )
            )
            candidates.extend(
                re.findall(
                    r'(?is)<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
                    html_text,
                )
            )
            candidates.extend(
                re.findall(
                    r'(?is)<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([0-9]+(?:\.[0-9]{1,2})?)["\']',
                    html_text,
                )
            )
            candidates.extend(_extract_json_ld_prices(html_text))
            prices: list[float] = []
            seen: set[float] = set()
            for raw in candidates:
                value = _parse_price_token(raw)
                if value is None or value in seen:
                    continue
                seen.add(value)
                prices.append(value)
            return prices

        def _fetch_structured_page_prices(url: str, *, timeout_seconds: int = 20) -> list[float]:
            target = str(url or "").strip()
            if not target:
                return []
            request_timeout = max(2, min(30, int(timeout_seconds or 20)))
            try:
                response = requests.get(
                    target,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; GoldenStackersSlackOps/1.0)"},
                    timeout=request_timeout,
                )
                response.raise_for_status()
            except Exception:
                return []
            return _extract_structured_page_prices(str(response.text or ""))

        def _slack_web_comp_search(
            raw_query: str,
            *,
            limit: int = 10,
            detail_fetch_limit: int = 3,
            detail_fetch_timeout_seconds: int = 10,
        ) -> list[dict[str, Any]]:
            q = " ".join(str(raw_query or "").split()).strip()
            if not q:
                return []
            try:
                response = requests.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": q},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; GoldenStackersSlackOps/1.0)"},
                    timeout=20,
                )
                response.raise_for_status()
                html_text = str(response.text or "")
            except Exception:
                return []
            anchors = re.findall(
                r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                html_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            snippets = re.findall(
                r'<[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</[^>]+>',
                html_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            out: list[dict[str, Any]] = []
            max_rows = max(1, min(int(limit), 25))
            max_detail_fetches = max(0, min(int(detail_fetch_limit), max_rows))
            fetch_timeout = max(2, min(30, int(detail_fetch_timeout_seconds)))
            for idx, (href, title_html) in enumerate(anchors[:max_rows]):
                title = unescape(re.sub(r"<[^>]+>", " ", str(title_html or ""))).strip()
                snippet = unescape(
                    re.sub(r"<[^>]+>", " ", str(snippets[idx] if idx < len(snippets) else ""))
                ).strip()
                resolved_url = _resolve_web_result_url(str(href or ""))
                price_hints = _extract_price_hints_simple(f"{title} {snippet} {resolved_url}")
                is_product_like = _is_product_like_web_row({"item_url": resolved_url})
                structured_prices: list[float] = []
                if idx < max_detail_fetches:
                    structured_prices = _fetch_structured_page_prices(
                        resolved_url,
                        timeout_seconds=fetch_timeout,
                    )
                # Guard against category/search pages leaking shipping-threshold prices from snippets.
                # For non-product-like URLs, only accept prices when we can extract structured page prices.
                effective_prices = (
                    sorted(structured_prices)
                    if structured_prices
                    else (sorted(price_hints) if is_product_like else [])
                )
                if effective_prices:
                    mid = len(effective_prices) // 2
                    listed_price = (
                        float(effective_prices[mid])
                        if len(effective_prices) % 2 == 1
                        else float((effective_prices[mid - 1] + effective_prices[mid]) / 2.0)
                    )
                else:
                    listed_price = 0.0
                listed_low = float(min(effective_prices)) if effective_prices else 0.0
                listed_high = float(max(effective_prices)) if effective_prices else 0.0
                host = (urlparse(resolved_url).netloc or "").lower()
                out.append(
                    {
                        "title": title,
                        "item_url": resolved_url,
                        "source_domain": host,
                        "snippet": snippet,
                        "listed_price": listed_price,
                        "listed_price_low": listed_low,
                        "listed_price_high": listed_high,
                        "shipping_cost": 0.0,
                        "total_price": listed_price,
                        "price_hint_count": int(len(effective_prices)),
                        "price_hint_source": (
                            "structured_page"
                            if structured_prices
                            else ("snippet_or_url" if price_hints else "none")
                        ),
                    }
                )
            return out

        def _persist_slack_summary(*, summary: str, error: str = "", links: list[str] | None = None) -> None:
            if getattr(job, "id", None) is None:
                return
            response_payload = {
                "intent": intent,
                "summary": " ".join(str(summary or "").strip().split())[:2000],
                "error": str(error or "").strip()[:500],
                "links": list(links or []),
                "generated_at": utcnow_naive().isoformat(timespec="seconds"),
            }
            try:
                next_payload = dict(payload)
                next_payload["ai_response"] = response_payload
                repo.update_integration_queue_job(
                    int(job.id),
                    {"payload_json": json.dumps(next_payload, sort_keys=True)},
                    actor=actor,
                )
            except Exception:
                pass
            if target_channel and get_runtime_bool(repo, "slack_ops_ai_auto_reply_enabled", False):
                try:
                    summary_text = str(response_payload["summary"] or "").strip() or "_No summary available._"
                    reply_lines = [f"*Slack Ops {intent.title()} Summary*", summary_text]
                    if response_payload["error"]:
                        reply_lines.append(f"Error: `{response_payload['error']}`")
                    if response_payload["links"]:
                        reply_lines.append("Related: " + " | ".join(response_payload["links"]))
                    send_slack_message(
                        repo,
                        text="\n".join(reply_lines),
                        channel=target_channel,
                        thread_ts=target_thread_ts,
                    )
                except Exception:
                    pass

        def _status_summary() -> str:
            slack_rows = repo.list_integration_queue_jobs(
                environment=settings.app_env,
                integration="slack_ops",
                statuses={"queued", "running", "blocked", "failed", "success"},
                limit=500,
            )
            now_ref = utcnow_naive()
            counts = {"queued": 0, "running": 0, "blocked": 0, "failed": 0, "success": 0, "due": 0}
            for row in slack_rows:
                row_status = str(getattr(row, "status", "") or "").strip().lower()
                if row_status in counts:
                    counts[row_status] += 1
                next_attempt = getattr(row, "next_attempt_at", None)
                if row_status == "queued" and (next_attempt is None or next_attempt <= now_ref):
                    counts["due"] += 1
            metrics = repo.dashboard_metrics()
            return (
                "*GoldenStackers Status*\n"
                f"- Slack Ops Queue: queued `{counts['queued']}` (due `{counts['due']}`), "
                f"blocked `{counts['blocked']}`, running `{counts['running']}`, failed `{counts['failed']}`\n"
                f"- Products: `{int(metrics.get('product_count', 0))}`\n"
                f"- Listings: `{int(metrics.get('listing_count', 0))}`\n"
                f"- Sales: `{int(metrics.get('sale_count', 0))}`\n"
                f"- Inventory Cost Snapshot: `${float(metrics.get('inventory_cost', 0.0)):,.2f}`"
            )

        def _operations_help() -> str:
            return (
                "*Slack Ops operations commands*\n"
                "- `operations run_due <integration> [limit]` where integration in `slack_ops|google|shipping|slack`\n"
                "- `operations approve <queue_job_id>`\n"
                "- `operations create_ebay_draft <product_id> [price] [qty]`"
            )

        if intent == "status":
            summary = _status_summary()
            _persist_slack_summary(summary=summary)
            return True, "Slack status summary generated."

        if intent == "operations":
            if not normalized_args:
                summary = _operations_help()
                _persist_slack_summary(summary=summary)
                return True, "Slack operations help generated."
            op = str(normalized_args[0] or "").strip().lower()
            if op in {"run_sync", "run"}:
                op = "run_due"
            elif op in {"queue_status", "status"}:
                summary = _status_summary()
                _persist_slack_summary(summary=summary)
                return True, "Slack queue status summary generated."
            if op in {"help", "?"}:
                summary = _operations_help()
                _persist_slack_summary(summary=summary)
                return True, "Slack operations help generated."
            if op == "run_due":
                integration_name = (
                    str(normalized_args[1] or "").strip().lower() if len(normalized_args) > 1 else "slack_ops"
                )
                if integration_name not in {"slack_ops", "google", "shipping", "slack"}:
                    summary = (
                        f"Unsupported integration `{integration_name}` for run_due.\n"
                        + _operations_help()
                    )
                    _persist_slack_summary(summary=summary)
                    return False, f"Unsupported integration `{integration_name}`."
                limit = 10
                if len(normalized_args) > 2:
                    limit = max(1, min(100, int(_coerce_int(normalized_args[2]) or 10)))
                run_summary = process_due_integration_queue_jobs(
                    repo,
                    integration=integration_name,
                    actor=request_actor,
                    limit=limit,
                )
                summary = (
                    f"*Run Due Result* `{integration_name}`\n"
                    f"- processed `{int(run_summary.get('processed') or 0)}`\n"
                    f"- success `{int(run_summary.get('success') or 0)}`\n"
                    f"- queued `{int(run_summary.get('queued') or 0)}`\n"
                    f"- failed `{int(run_summary.get('failed') or 0)}`\n"
                    f"- blocked `{int(run_summary.get('blocked') or 0)}`"
                )
                _persist_slack_summary(summary=summary)
                return True, f"Run due executed for `{integration_name}`."
            if op == "approve":
                if len(normalized_args) < 2:
                    summary = "Missing queue job id.\n" + _operations_help()
                    _persist_slack_summary(summary=summary)
                    return False, "Missing queue job id."
                queue_job_id = _coerce_int(normalized_args[1]) or 0
                if queue_job_id <= 0:
                    summary = "Invalid queue job id.\n" + _operations_help()
                    _persist_slack_summary(summary=summary)
                    return False, "Invalid queue job id."
                from app.services.slack_ops_bot import approve_slack_ops_queue_job

                approval = approve_slack_ops_queue_job(
                    repo,
                    queue_job_id=int(queue_job_id),
                    approver_username=request_actor,
                    approver_role="ops",
                    actor=actor,
                )
                summary = (
                    f"*Slack Ops Approval*\n- queue job: `#{queue_job_id}`\n"
                    f"- status: `{str(approval.get('status') or '')}`"
                )
                _persist_slack_summary(summary=summary)
                return True, f"Queue job #{queue_job_id} approved."
            if op == "create_ebay_draft":
                if len(normalized_args) < 2:
                    summary = "Missing product id.\n" + _operations_help()
                    _persist_slack_summary(summary=summary)
                    return False, "Missing product id."
                product_id = _coerce_int(normalized_args[1]) or 0
                if product_id <= 0:
                    summary = "Invalid product id.\n" + _operations_help()
                    _persist_slack_summary(summary=summary)
                    return False, "Invalid product id."
                product = repo.db.get(Product, int(product_id))
                if product is None:
                    summary = f"Product `{product_id}` not found."
                    _persist_slack_summary(summary=summary)
                    return False, f"Product {product_id} not found."
                price = float(getattr(product, "acquisition_cost", 0) or 0) * 1.2
                if price <= 0:
                    price = 1.0
                if len(normalized_args) > 2:
                    try:
                        price = max(0.01, float(normalized_args[2]))
                    except Exception:
                        pass
                qty = max(1, int(getattr(product, "current_quantity", 1) or 1))
                if len(normalized_args) > 3:
                    qty = max(1, int(_coerce_int(normalized_args[3]) or qty))
                listing = repo.create_listing(
                    product_id=int(product.id),
                    marketplace="ebay",
                    listing_title=str(getattr(product, "title", "") or "").strip()[:240] or f"Product {product.id}",
                    listing_price=Decimal(str(round(float(price), 2))),
                    quantity_listed=int(qty),
                    actor=request_actor,
                )
                summary = (
                    f"*Created eBay Draft Listing*\n"
                    f"- listing id: `#{int(getattr(listing, 'id', 0) or 0)}`\n"
                    f"- product id: `#{int(product.id)}`\n"
                    f"- title: `{str(getattr(listing, 'listing_title', '') or '')[:120]}`\n"
                    f"- price: `${float(getattr(listing, 'listing_price', 0) or 0):,.2f}`\n"
                    f"- qty: `{int(getattr(listing, 'quantity_listed', 0) or 0)}`"
                )
                _persist_slack_summary(
                    summary=summary,
                    links=[f"Product #{int(product.id)}", f"Listing #{int(getattr(listing, 'id', 0) or 0)}"],
                )
                return True, f"Created draft listing #{int(getattr(listing, 'id', 0) or 0)}."
            summary = f"Unsupported operations command `{op}`.\n" + _operations_help()
            _persist_slack_summary(summary=summary)
            return False, f"Unsupported operations command `{op}`."

        if not file_rows and intent not in {"comp", "intake"}:
            return True, "Slack ops command ingested (no file attachments)."

        storage = None
        if file_rows:
            from app.services.media_storage import MediaStorageService

            storage = MediaStorageService()
            if not storage.enabled:
                return False, "S3 media storage is not configured for Slack attachment ingestion."
            storage.ensure_bucket()

        uploader = request_actor
        product_id = _coerce_int(command_payload.get("raw_payload", {}).get("product_id"))
        listing_id = _coerce_int(command_payload.get("raw_payload", {}).get("listing_id"))
        lot_id = _coerce_int(command_payload.get("raw_payload", {}).get("lot_id"))
        source_id = _coerce_int(command_payload.get("raw_payload", {}).get("source_id"))
        document_kind = (
            str(command_payload.get("raw_payload", {}).get("document_kind") or "incoming_invoice").strip().lower()
            or "incoming_invoice"
        )

        ingested_media = 0
        ingested_documents = 0
        created_media_ids: list[int] = []
        errors: list[str] = []
        first_image_bytes: bytes | None = None
        first_image_content_type = "image/jpeg"
        for raw_file in file_rows:
            if not isinstance(raw_file, dict):
                continue
            name = str(raw_file.get("name") or "slack_file").strip() or "slack_file"
            mimetype = str(raw_file.get("mimetype") or "application/octet-stream").strip().lower()
            is_document = mimetype.startswith("application/") or mimetype in {
                "text/plain",
                "text/csv",
                "text/markdown",
            }
            try:
                file_bytes, file_name = _download_slack_file_bytes(
                    raw_file,
                    timeout_seconds=get_runtime_int(repo, "slack_http_timeout_seconds", 15),
                )
                assert storage is not None
                upload = storage.upload_file(
                    file_name=file_name,
                    file_bytes=file_bytes,
                    content_type=mimetype,
                )
                if is_document:
                    repo.create_purchase_document(
                        document_kind=document_kind,
                        title=file_name,
                        original_filename=file_name,
                        content_type=upload.content_type,
                        size_bytes=upload.size_bytes,
                        content_sha256=hashlib.sha256(file_bytes).hexdigest(),
                        s3_bucket=upload.bucket,
                        s3_key=upload.key,
                        s3_url=upload.url,
                        lot_id=lot_id,
                        product_id=product_id,
                        source_id=source_id,
                        uploaded_by=uploader,
                        actor=actor,
                    )
                    ingested_documents += 1
                else:
                    media_type = "image" if mimetype.startswith("image/") else ("video" if mimetype.startswith("video/") else "document")
                    media = repo.create_media_asset(
                        media_type=media_type,
                        original_filename=file_name,
                        content_type=upload.content_type,
                        size_bytes=upload.size_bytes,
                        s3_bucket=upload.bucket,
                        s3_key=upload.key,
                        s3_url=upload.url,
                        product_id=product_id,
                        listing_id=listing_id,
                        uploaded_by=uploader,
                    )
                    ingested_media += 1
                    media_id = int(getattr(media, "id", 0) or 0)
                    if media_id > 0:
                        created_media_ids.append(media_id)
                    if first_image_bytes is None and mimetype.startswith("image/"):
                        first_image_bytes = file_bytes
                        first_image_content_type = upload.content_type or mimetype
            except Exception as exc:
                errors.append(f"{name}: {str(exc)}")

        if errors:
            return False, "Slack attachment ingest had errors: " + " | ".join(errors[:10])

        ai_error = ""
        ai_summary = ""
        comp_ebay_rows: list[dict[str, Any]] = []
        comp_web_rows: list[dict[str, Any]] = []
        comp_query_used = ""
        comp_fetch_mode = ""
        trusted_source_filter_enabled = get_runtime_bool(
            repo,
            "slack_ops_comp_trusted_sources_only_enabled",
            False,
        )
        trusted_source_filter_override: bool | None = None
        trusted_domains_override: set[str] | None = None
        confidence_gate_override: bool | None = None
        rows_gate_override: bool | None = None
        min_confidence_override: float | None = None
        min_rows_override: int | None = None
        min_qualified_rows: int = 2
        trusted_web_domains = _trusted_comp_web_domains()
        filtered_web_rows_count = 0
        if intent in {"comp", "intake"} and get_runtime_bool(repo, "slack_ops_ai_assist_enabled", True):
            try:
                from app.services.ai_orchestration import execute_comp_summary, execute_multimodal_task

                if intent == "comp":
                    from app.services.ebay import EbayClient

                    comp_overrides = _parse_override_pairs(normalized_args)
                    query_seed = str(query_text or "").strip()
                    if query_seed.lower().startswith("comp "):
                        query_seed = query_seed.split(" ", 1)[1].strip()
                    if not query_seed:
                        non_override_args = [t for t in normalized_args if "=" not in str(t or "")]
                        query_seed = " ".join(non_override_args).strip()
                    query_seed = _remove_override_tokens_from_text(query_seed, comp_overrides)
                    if not query_seed:
                        query_seed = "Slack comp request"
                    query_candidates = _comp_query_variants(query_seed)
                    comp_query_used = query_seed
                    sold_only = str(comp_overrides.get("sold_only") or "true").strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    trusted_source_filter_override = _parse_bool_override(comp_overrides.get("trusted_only"))
                    if trusted_source_filter_override is not None:
                        trusted_source_filter_enabled = bool(trusted_source_filter_override)
                    trusted_domains_override_candidate = _parse_domains_csv(comp_overrides.get("trusted_domains"))
                    if trusted_domains_override_candidate:
                        trusted_domains_override = trusted_domains_override_candidate
                        trusted_web_domains = set(trusted_domains_override_candidate)
                        if trusted_source_filter_override is None:
                            trusted_source_filter_enabled = True
                    confidence_gate_override = _parse_bool_override(comp_overrides.get("confidence_gate"))
                    rows_gate_override = _parse_bool_override(comp_overrides.get("rows_gate"))
                    min_confidence_raw = str(comp_overrides.get("min_confidence") or "").strip()
                    if min_confidence_raw:
                        try:
                            min_confidence_override = float(min_confidence_raw)
                        except Exception:
                            min_confidence_override = None
                    min_rows_candidate = _safe_int(comp_overrides.get("min_rows"))
                    if min_rows_candidate is not None:
                        min_rows_override = int(min_rows_candidate)
                    category_id = str(comp_overrides.get("category_id") or "").strip()
                    per_page = max(
                        5,
                        min(
                            50,
                            int(_safe_int(comp_overrides.get("limit")) or 25),
                        ),
                    )
                    max_variants = max(
                        1,
                        min(
                            4,
                            int(_safe_int(comp_overrides.get("variants")) or 3),
                        ),
                    )
                    client = EbayClient()
                    if client.is_configured():
                        for candidate in query_candidates[:max_variants]:
                            try:
                                attempt_rows = client.search_sold_items_html(
                                    keywords=candidate,
                                    limit=per_page,
                                )
                            except Exception:
                                continue
                            if attempt_rows:
                                comp_ebay_rows = list(attempt_rows)
                                comp_fetch_mode = "ebay_sold_html_primary"
                                break
                    if not comp_ebay_rows and get_runtime_bool(
                        repo, "slack_ops_comp_ebay_html_fallback_enabled", True
                    ):
                        comp_ebay_rows = client.search_sold_items_html(
                            keywords=query_seed,
                            limit=max(
                                1,
                                min(50, int(get_runtime_int(repo, "slack_ops_comp_ebay_html_fallback_limit", 20))),
                            ),
                        )
                        if comp_ebay_rows:
                            comp_fetch_mode = "ebay_sold_html_fallback"
                    if not comp_ebay_rows and get_runtime_bool(
                        repo, "slack_ops_comp_web_fallback_enabled", True
                    ):
                        comp_web_rows = _slack_web_comp_search(
                            query_seed,
                            limit=max(
                                1,
                                min(20, int(get_runtime_int(repo, "slack_ops_comp_web_fallback_limit", 10))),
                            ),
                            detail_fetch_limit=max(
                                0,
                                min(
                                    20,
                                    int(
                                        get_runtime_int(
                                            repo,
                                            "slack_ops_comp_web_detail_fetch_limit",
                                            3,
                                        )
                                    ),
                                ),
                            ),
                            detail_fetch_timeout_seconds=max(
                                2,
                                min(
                                    30,
                                    int(
                                        get_runtime_int(
                                            repo,
                                            "slack_ops_comp_web_detail_fetch_timeout_seconds",
                                            10,
                                        )
                                    ),
                                ),
                            ),
                        )
                        comp_web_rows = _prefer_product_like_web_rows(comp_web_rows)
                        pre_filter_count = len(comp_web_rows)
                        comp_web_rows = _filter_comp_web_rows_by_trust(
                            comp_web_rows,
                            trusted_only=trusted_source_filter_enabled,
                            trusted_domains=trusted_web_domains,
                        )
                        filtered_web_rows_count = max(0, pre_filter_count - len(comp_web_rows))
                        if comp_web_rows:
                            comp_fetch_mode = "web_fallback"
                    ai_result = execute_comp_summary(
                        repo,
                        query=query_seed,
                        ebay_rows=comp_ebay_rows,
                        web_rows=comp_web_rows,
                        spot_context=None,
                        system_message=get_runtime_str(
                            repo,
                            "slack_ops_comp_system_message",
                            "You are a concise comps analyst for resale operators. Return short markdown.",
                        ),
                        instruction=get_runtime_str(
                            repo,
                            "slack_ops_comp_instruction",
                            (
                                "Provide concise markdown with confidence, suggested range, and top risk caveats. "
                                "Keep it operator-safe and avoid over-claiming."
                            ),
                        ),
                        workflow="comp",
                    )
                else:
                    ai_result = execute_multimodal_task(
                        repo,
                        tool_name="slack_intake_assistant",
                        system_message=get_runtime_str(
                            repo,
                            "slack_ops_intake_system_message",
                            "You assist inventory intake operators with concise, actionable summaries.",
                        ),
                        instruction=get_runtime_str(
                            repo,
                            "slack_ops_intake_instruction",
                            (
                                "Provide concise markdown with likely item identification, salient attributes, "
                                "and suggested next intake fields to confirm."
                            ),
                        ),
                        image_bytes=first_image_bytes,
                        image_content_type=first_image_content_type,
                        context={"source": "slack_ops"},
                        workflow="intake",
                    )
                ai_summary = " ".join(str(ai_result.text or "").strip().split())[:1000]
                if intent == "comp":
                    band_low_pct = max(
                        0.01,
                        min(5.0, float(get_runtime_float(repo, "slack_ops_comp_band_low_pct", 90.0)) / 100.0),
                    )
                    band_high_pct = max(
                        band_low_pct,
                        min(5.0, float(get_runtime_float(repo, "slack_ops_comp_band_high_pct", 110.0)) / 100.0),
                    )
                    stats_snippet = _build_comp_stats_snippet(
                        comp_ebay_rows,
                        comp_web_rows,
                        band_low_pct=band_low_pct,
                        band_high_pct=band_high_pct,
                    )
                    top_comp_snippet = _build_top_comp_snippet(comp_ebay_rows, comp_web_rows)
                    if stats_snippet:
                        ai_summary = (stats_snippet + "\n\n" + ai_summary).strip()
                    if top_comp_snippet:
                        ai_summary = (ai_summary + "\n\n" + top_comp_snippet).strip()
                    confidence_score, confidence_label = _comp_overall_confidence(comp_ebay_rows, comp_web_rows)
                    ai_summary = re.sub(
                        r"(?i)\*\*confidence:\*\*\s*(?:low|medium(?:\s*-\s*high)?|high)\b",
                        f"**Confidence:** {confidence_label.title()} (rule-based)",
                        ai_summary,
                        count=1,
                    )
                    ai_summary = re.sub(
                        r"(?i)(?:#{1,6}\s*)?confidence\s*:\s*(?:low|medium(?:\s*-\s*high)?|high)\b",
                        f"**Confidence:** {confidence_label.title()} (rule-based)",
                        ai_summary,
                        count=1,
                    )
                    qualified_row_count = _comp_qualified_row_count(comp_ebay_rows, comp_web_rows)
                    min_qualified_rows = max(
                        1,
                        min(
                            20,
                            int(
                                min_rows_override
                                if min_rows_override is not None
                                else get_runtime_int(repo, "slack_ops_comp_min_qualified_rows", 2)
                            ),
                        ),
                    )
                    min_rows_gate_enabled = (
                        bool(rows_gate_override)
                        if rows_gate_override is not None
                        else get_runtime_bool(
                            repo,
                            "slack_ops_comp_min_qualified_rows_gate_enabled",
                            True,
                        )
                    )
                    min_confidence_score = max(
                        0.0,
                        min(
                            10.0,
                            float(
                                min_confidence_override
                                if min_confidence_override is not None
                                else get_runtime_float(repo, "slack_ops_comp_min_confidence_score", 3.5)
                            ),
                        ),
                    )
                    confidence_gate_enabled = (
                        bool(confidence_gate_override)
                        if confidence_gate_override is not None
                        else get_runtime_bool(
                            repo,
                            "slack_ops_comp_min_confidence_gate_enabled",
                            True,
                        )
                    )
                    confidence_gate_triggered = confidence_gate_enabled and confidence_score < min_confidence_score
                    rows_gate_triggered = min_rows_gate_enabled and qualified_row_count < min_qualified_rows
                    gate_reasons: list[str] = []
                    if confidence_gate_triggered:
                        gate_reasons.append(
                            "evidence is below minimum threshold "
                            f"({confidence_score:.2f} < {min_confidence_score:.2f}, label={confidence_label})"
                        )
                    if rows_gate_triggered:
                        gate_reasons.append(
                            "qualified comp row count is below minimum threshold "
                            f"({qualified_row_count} < {min_qualified_rows})"
                        )
                    if gate_reasons:
                        if rows_gate_triggered and not confidence_gate_triggered:
                            ai_summary = re.sub(
                                r"(?i)Evidence confidence\s+(low|medium|high)",
                                r"Evidence confidence \1 (single-comp; row-gated)",
                                ai_summary,
                                count=1,
                            )
                        if confidence_gate_triggered and rows_gate_triggered:
                            unavailable_reason = "insufficient evidence confidence and comp count"
                        elif confidence_gate_triggered:
                            unavailable_reason = "insufficient evidence confidence"
                        else:
                            unavailable_reason = "insufficient qualified comps"
                        ai_summary = re.sub(
                            r"Suggested list band \$[0-9,]+\.[0-9]{2}-\$[0-9,]+\.[0-9]{2}",
                            f"Suggested list band unavailable ({unavailable_reason})",
                            ai_summary,
                        )
                        ai_summary = re.sub(
                            r"(?i)\*\*suggested\s+range:\*\*\s*\$?[0-9,]+(?:\.[0-9]{1,2})?\s*[-–]\s*\$?[0-9,]+(?:\.[0-9]{1,2})?(?=\s+\*\*|$)",
                            f"**Suggested Range:** Unavailable ({unavailable_reason})",
                            ai_summary,
                        )
                        ai_summary = re.sub(
                            r"(?i)suggested\s+range\s*:\s*\$?[0-9,]+(?:\.[0-9]{1,2})?\s*[-–]\s*\$?[0-9,]+(?:\.[0-9]{1,2})?(?=\s+\*\*|$)",
                            f"**Suggested Range:** Unavailable ({unavailable_reason})",
                            ai_summary,
                        )
                        ai_summary = re.sub(
                            r"(?i)\*\*recommendation:\*\*.*?(?=\s+(?:\*\*[a-z][^*]*\*\*|top comps:|comp evidence gate triggered:|related:)|$)",
                            "**Recommendation:** Directional-only comp. Hold final pricing until stronger sold/product evidence is available.",
                            ai_summary,
                        )
                        ai_summary = re.sub(
                            r"(?i)recommendation\s*:.*?(?=\s+(?:\*\*[a-z][^*]*\*\*|top comps:|comp evidence gate triggered:|related:)|$)",
                            "**Recommendation:** Directional-only comp. Hold final pricing until stronger sold/product evidence is available.",
                            ai_summary,
                        )
                        ai_summary = (
                            ai_summary
                            + "\n\n"
                            + (
                                "Comp evidence gate triggered: "
                                + "; ".join(gate_reasons)
                                + ". "
                                "Use this comp as directional only and verify with stronger sold/product evidence before pricing."
                            )
                        ).strip()
                        gate_mode = (
                            "evidence_gate_confidence_rows"
                            if (confidence_gate_triggered and rows_gate_triggered)
                            else "evidence_gate_confidence"
                            if confidence_gate_triggered
                            else "evidence_gate_rows"
                        )
                        if not comp_fetch_mode:
                            comp_fetch_mode = gate_mode
                        elif gate_mode not in comp_fetch_mode:
                            comp_fetch_mode = f"{comp_fetch_mode}+{gate_mode}"
            except Exception as exc:
                ai_error = str(exc)[:500]

        links: list[str] = []
        if product_id is not None:
            links.append(f"Product #{product_id}")
        if listing_id is not None:
            links.append(f"Listing #{listing_id}")
        if lot_id is not None:
            links.append(f"Lot #{lot_id}")
        if source_id is not None:
            links.append(f"Source #{source_id}")
        if intent == "comp":
            links.append(f"eBay rows: {len(comp_ebay_rows)}")
            links.append(f"Web rows: {len(comp_web_rows)}")
            if trusted_source_filter_enabled:
                links.append(
                    f"Trusted-source web filter: on (removed {int(filtered_web_rows_count)} rows)"
                )
            if trusted_source_filter_override is not None:
                links.append(
                    f"Trusted-source override: {'true' if trusted_source_filter_override else 'false'}"
                )
            if trusted_domains_override:
                links.append("Trusted-domains override: " + ",".join(sorted(trusted_domains_override)))
            if confidence_gate_override is not None:
                links.append(
                    f"Confidence-gate override: {'true' if confidence_gate_override else 'false'}"
                )
            if rows_gate_override is not None:
                links.append(f"Rows-gate override: {'true' if rows_gate_override else 'false'}")
            if min_confidence_override is not None:
                links.append(f"Min-confidence override: {float(min_confidence_override):.2f}")
            if min_rows_override is not None:
                links.append(f"Min-rows override: {int(min_rows_override)}")
            else:
                links.append(f"Min qualified comps: {int(min_qualified_rows)}")
            if comp_query_used:
                links.append(f"Query: {comp_query_used[:140]}")
            if comp_fetch_mode:
                links.append(f"Fetch mode: {comp_fetch_mode}")
            if ai_error:
                links.append("Comp fetch had errors; AI summary may be partial")

        if intent == "intake" and product_id is None:
            overrides = _parse_override_pairs(normalized_args)
            created_product = None
            try:
                structured_payload: dict[str, Any] = {}
                if first_image_bytes is not None or query_text:
                    from app.services.ai_orchestration import execute_multimodal_task

                    structured_result = execute_multimodal_task(
                        repo,
                        tool_name="slack_intake_product_builder",
                        system_message=get_runtime_str(
                            repo,
                            "slack_ops_intake_system_message",
                            "You assist inventory intake operators with concise, actionable summaries.",
                        ),
                        instruction=(
                            "Return ONLY JSON object with keys: suggested_title, suggested_category, "
                            "suggested_description, suggested_metal_type, suggested_weight_oz, "
                            "suggested_quantity, suggested_acquisition_cost_usd. "
                            "Allowed categories: bullion, collectibles, antiques, coins, normal_goods, other."
                            f"\nOperator request text: {query_text or '(none)'}"
                        ),
                        image_bytes=first_image_bytes,
                        image_content_type=first_image_content_type,
                        context={"source": "slack_ops"},
                        workflow="intake",
                    )
                    structured_payload = _extract_json_object(str(structured_result.text or ""))

                allowed_categories = {"bullion", "collectibles", "antiques", "coins", "normal_goods", "other"}
                category = str(
                    overrides.get("category")
                    or structured_payload.get("suggested_category")
                    or "other"
                ).strip().lower()
                if category not in allowed_categories:
                    category = "other"
                title = str(
                    overrides.get("title")
                    or structured_payload.get("suggested_title")
                    or query_text
                    or f"Slack Intake {utcnow_naive().strftime('%Y-%m-%d')}"
                ).strip()
                title = title[:240] or f"Slack Intake {utcnow_naive().strftime('%Y-%m-%d')}"
                description = str(
                    overrides.get("description")
                    or structured_payload.get("suggested_description")
                    or ai_summary
                    or query_text
                ).strip()
                metal_type = str(
                    overrides.get("metal")
                    or overrides.get("metal_type")
                    or structured_payload.get("suggested_metal_type")
                    or ""
                ).strip()
                quantity = max(
                    1,
                    int(
                        _safe_int(overrides.get("qty"))
                        or _safe_int(overrides.get("quantity"))
                        or _safe_int(structured_payload.get("suggested_quantity"))
                        or 1
                    ),
                )
                acquisition_cost = (
                    _safe_decimal(overrides.get("cost"))
                    or _safe_decimal(overrides.get("acquisition_cost"))
                    or _safe_decimal(structured_payload.get("suggested_acquisition_cost_usd"))
                    or Decimal("0")
                )
                weight_oz = (
                    _safe_decimal(overrides.get("weight_oz"))
                    or _safe_decimal(structured_payload.get("suggested_weight_oz"))
                )
                created_product = repo.create_product(
                    sku=_build_intake_sku(category=category, metal_type=metal_type),
                    title=title,
                    category=category,
                    description=description,
                    metal_type=metal_type,
                    weight_oz=weight_oz,
                    acquisition_cost=acquisition_cost,
                    current_quantity=quantity,
                    inventory_class="sellable",
                    actor=request_actor,
                )
                created_product_id = int(getattr(created_product, "id", 0) or 0)
                if created_product_id > 0:
                    product_id = created_product_id
                    links.append(f"Product #{created_product_id}")
                    if created_media_ids:
                        try:
                            repo.bulk_update_media_assets(
                                created_media_ids,
                                {"product_id": created_product_id},
                                actor=request_actor,
                            )
                        except Exception:
                            pass
                    missing_fields: list[str] = []
                    if float(acquisition_cost or 0) <= 0:
                        missing_fields.append("acquisition_cost")
                    if not metal_type:
                        missing_fields.append("metal_type")
                    if weight_oz in (None, Decimal("0")):
                        missing_fields.append("weight_oz")
                    ai_summary = (
                        f"*Created intake product draft* `#{created_product_id}`\n"
                        f"- SKU: `{str(getattr(created_product, 'sku', '') or '')}`\n"
                        f"- Title: `{title}`\n"
                        f"- Category: `{category}`\n"
                        f"- Qty: `{quantity}`\n"
                        f"- Cost: `${float(acquisition_cost or 0):,.2f}`\n"
                        f"- Media linked: `{len(created_media_ids)}`"
                    )
                    if missing_fields:
                        ai_summary += (
                            "\n- Missing confirmations: `"
                            + ", ".join(missing_fields)
                            + "` (update from Products page or next Slack ops command)."
                        )
            except Exception as intake_exc:
                if not ai_error:
                    ai_error = str(intake_exc)[:500]

        _persist_slack_summary(summary=ai_summary, error=ai_error, links=links)
        msg = f"Slack attachments ingested. media={ingested_media} documents={ingested_documents}"
        if intent == "intake" and product_id is not None:
            msg += f" | Created product draft #{int(product_id)}."
        if ai_summary:
            msg += " | AI summary generated."
        elif intent in {"comp", "intake"} and ai_error:
            msg += f" | AI assist unavailable: {ai_error[:180]}"
        return (True, msg)

    if integration != "google":
        if integration == "slack":
            if action != "post_message":
                return False, f"Unsupported slack action `{action}`."
            send_slack_message(
                repo,
                text=str(payload.get("text") or ""),
                channel=str(payload.get("channel") or ""),
            )
            return True, "Slack post completed."
        if integration == "shipping":
            if action != "purchase_label":
                return False, f"Unsupported shipping action `{action}`."
            if not get_runtime_bool(repo, "shipping_queue_enabled", True):
                return False, "Shipping queue is disabled by runtime setting."
            if not get_runtime_bool(repo, "shipping_label_purchase_enabled", True):
                return False, "Shipping label purchase is disabled by runtime setting."
            sale_id_raw = payload.get("sale_id")
            try:
                sale_id = int(sale_id_raw)
            except Exception:
                return False, "Missing/invalid `sale_id` payload."
            sale = repo.db.get(Sale, sale_id)
            if sale is None:
                return False, f"Sale `{sale_id}` not found."

            dry_run = bool(payload.get("dry_run", False))
            provider = str(payload.get("shipping_provider") or "").strip()
            provider_key = provider.replace(" ", "_").lower() or "other"
            provider_enabled_key = f"shipping_label_provider_{provider_key}_enabled"
            if not get_runtime_bool(repo, provider_enabled_key, True):
                return False, f"Shipping label provider `{provider or 'other'}` is disabled by runtime setting."
            service = str(payload.get("shipping_service") or "").strip()
            package_type = str(payload.get("shipping_package_type") or "").strip()
            tracking_number = str(payload.get("tracking_number") or "").strip()
            label_id = str(payload.get("shipping_label_id") or "").strip()
            label_url = str(payload.get("shipping_label_url") or "").strip()
            label_currency = str(payload.get("shipping_label_currency") or "USD").strip() or "USD"
            label_cost_raw = payload.get("shipping_label_cost")
            label_cost = None
            if label_cost_raw not in (None, ""):
                try:
                    label_cost = float(label_cost_raw)
                except Exception:
                    label_cost = None
            live_provider_calls = get_runtime_bool(repo, "shipping_label_live_provider_calls_enabled", False)

            if dry_run:
                return True, "Shipping label dry-run completed (no sale fields updated)."
            if live_provider_calls:
                provider_result = purchase_shipping_label(repo, provider=provider, payload=payload)
                if not label_id:
                    label_id = str(provider_result.label_id or "").strip()
                if not label_url:
                    label_url = str(provider_result.label_url or "").strip()
                if provider_result.label_cost is not None:
                    label_cost = float(provider_result.label_cost)
                if provider_result.label_currency:
                    label_currency = str(provider_result.label_currency).strip() or label_currency
                if not tracking_number:
                    tracking_number = str(provider_result.tracking_number or "").strip()

            updates: dict[str, Any] = {}
            if provider:
                updates["shipping_provider"] = provider
            if service:
                updates["shipping_service"] = service
            if package_type:
                updates["shipping_package_type"] = package_type
            if tracking_number:
                updates["tracking_number"] = tracking_number
            current_status = str(getattr(sale, "tracking_status", "") or "").strip().lower()
            if current_status in {"", "label_created"}:
                updates["tracking_status"] = "label_created"
            if not label_id:
                provider_token = provider.replace(" ", "_").lower() or "carrier"
                label_id = f"{provider_token}-LBL-{int(job.id)}-{int(sale.id)}"
            if not label_url:
                label_url = f"https://labels.goldenstackers.local/{provider or 'carrier'}/{label_id}.pdf"
            updates["shipping_label_id"] = label_id
            updates["shipping_label_url"] = label_url
            updates["shipping_label_currency"] = label_currency
            updates["shipping_label_purchased_at"] = utcnow_naive()
            updates["shipping_label_cost"] = label_cost
            if updates:
                repo.update_sale(int(sale.id), updates, actor=actor)
            return (
                True,
                "Shipping label purchase completed."
                if live_provider_calls
                else "Shipping label purchase scaffold completed.",
            )
        if integration == "slack_ops":
            return _ingest_slack_ops_command()
        return False, f"Unsupported integration `{integration}`."

    cfg = resolve_google_workspace_config(repo)
    if action == "gmail_send_document_email":
        send_gmail_message(
            config=cfg,
            to_email=str(payload.get("to_email") or ""),
            subject=str(payload.get("subject") or ""),
            body_html=str(payload.get("body_html") or ""),
            body_text=str(payload.get("body_text") or ""),
        )
        return True, "Gmail send completed."

    if action == "calendar_create_event":
        create_calendar_event(
            config=cfg,
            summary=str(payload.get("summary") or ""),
            start_iso=str(payload.get("start_iso") or ""),
            end_iso=str(payload.get("end_iso") or ""),
            description=str(payload.get("description") or ""),
            timezone=str(payload.get("timezone") or cfg.default_timezone or "America/Denver"),
            calendar_id=str(payload.get("calendar_id") or cfg.default_calendar_id or "primary"),
        )
        return True, "Calendar event created."

    if action == "drive_upload_artifact":
        file_b64 = str(payload.get("file_b64") or "")
        if not file_b64:
            return False, "Missing `file_b64` payload."
        file_bytes = base64.b64decode(file_b64)
        upload_drive_file(
            config=cfg,
            file_name=str(payload.get("file_name") or ""),
            file_bytes=file_bytes,
            mime_type=str(payload.get("mime_type") or "application/octet-stream"),
            folder_id=str(payload.get("folder_id") or ""),
        )
        return True, "Drive upload completed."

    return False, f"Unsupported integration action `{action}`."


def process_integration_queue_job(repo: Any, *, job_id: int, actor: str) -> tuple[bool, str]:
    job = repo.db.get(IntegrationQueueJob, int(job_id))
    if job is None:
        raise ValueError(f"Integration queue job {job_id} not found.")

    now = utcnow_naive()
    repo.update_integration_queue_job(
        int(job.id),
        {
            "status": "running",
            "last_attempt_at": now,
        },
        actor=actor,
    )
    job = repo.db.get(IntegrationQueueJob, int(job.id))

    try:
        ok, message = execute_integration_queue_job(repo, job, actor=actor)
    except Exception as exc:
        ok, message = False, _capture_queue_execute_exception(repo, actor=actor, job=job, exc=exc)

    if ok:
        repo.update_integration_queue_job(
            int(job.id),
            {
                "status": "success",
                "completed_at": utcnow_naive(),
                "last_error": "",
            },
            actor=actor,
        )
        repo.log_integration_event(
            actor=actor,
            integration=f"{job.integration}_queue",
            action=f"{job.action}_retry_execute",
            status="success",
            details={"queue_job_id": int(job.id), "retry_count": int(job.retry_count or 0)},
        )
        return True, message

    next_retry = int(job.retry_count or 0) + 1
    exceeded = next_retry > int(job.max_retries or 0)
    if exceeded:
        repo.update_integration_queue_job(
            int(job.id),
            {
                "status": "failed",
                "retry_count": next_retry,
                "last_error": message[:2000],
            },
            actor=actor,
        )
        repo.log_integration_event(
            actor=actor,
            integration=f"{job.integration}_queue",
            action=f"{job.action}_retry_execute",
            status="failed",
            details={"queue_job_id": int(job.id), "retry_count": next_retry, "error": message[:500]},
        )
        _emit_terminal_queue_failure_alert(
            repo,
            actor=actor,
            job=job,
            retry_count=next_retry,
            error_text=message,
        )
        return False, message

    backoff_seconds = _calc_backoff_seconds(repo, next_retry, integration=str(job.integration or ""))
    repo.update_integration_queue_job(
        int(job.id),
        {
            "status": "queued",
            "retry_count": next_retry,
            "next_attempt_at": utcnow_naive() + timedelta(seconds=backoff_seconds),
            "last_error": message[:2000],
        },
        actor=actor,
    )
    repo.log_integration_event(
        actor=actor,
        integration=f"{job.integration}_queue",
        action=f"{job.action}_retry_execute",
        status="queued",
        details={
            "queue_job_id": int(job.id),
            "retry_count": next_retry,
            "next_attempt_in_seconds": backoff_seconds,
            "error": message[:500],
        },
    )
    return False, message


def process_due_google_queue_jobs(repo: Any, *, actor: str, limit: int = 10) -> dict[str, int]:
    return process_due_integration_queue_jobs(
        repo,
        integration="google",
        actor=actor,
        limit=limit,
    )


def process_due_integration_queue_jobs(
    repo: Any,
    *,
    integration: str,
    actor: str,
    limit: int = 10,
) -> dict[str, int]:
    jobs = repo.list_integration_queue_jobs(
        environment=settings.app_env,
        integration=str(integration or "").strip().lower(),
        statuses={"queued"},
        limit=max(1, min(int(limit), 100)),
    )
    now = utcnow_naive()
    due = [row for row in jobs if row.next_attempt_at is None or row.next_attempt_at <= now]
    summary = {
        "processed": 0,
        "success": 0,
        "queued": 0,
        "failed": 0,
        "blocked": 0,
        "rules_matched": 0,
        "rules_applied": 0,
        "rules_approval_gated": 0,
    }
    for row in due:
        refreshed_job = repo.db.get(IntegrationQueueJob, int(row.id))
        if refreshed_job is None:
            continue
        rule_result = evaluate_and_apply_rules_for_job(
            repo,
            job=refreshed_job,
            actor=actor,
            trigger_status="queued",
        )
        summary["rules_matched"] += len(rule_result.get("matched_rule_ids") or [])
        summary["rules_applied"] += len(rule_result.get("applied_rule_ids") or [])
        summary["rules_approval_gated"] += len(rule_result.get("approval_gated_rule_ids") or [])
        if bool(rule_result.get("blocked")):
            summary["blocked"] += 1
            try:
                repo.update_integration_queue_job(
                    int(refreshed_job.id),
                    {
                        "last_error": str(rule_result.get("blocked_reason") or "Blocked by automation rule.")[:2000],
                    },
                    actor=actor,
                )
            except Exception:
                pass
            continue

        ok, _ = process_integration_queue_job(repo, job_id=int(row.id), actor=actor)
        summary["processed"] += 1
        if ok:
            summary["success"] += 1
        else:
            refreshed = repo.db.get(IntegrationQueueJob, int(row.id))
            status = str(getattr(refreshed, "status", "") or "").strip().lower()
            if status == "queued":
                summary["queued"] += 1
            else:
                summary["failed"] += 1
    return summary
