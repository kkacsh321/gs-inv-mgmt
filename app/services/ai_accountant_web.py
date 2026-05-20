from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests


ACCOUNTANT_WEB_RESEARCH_TERMS = {
    "bullion",
    "coin exemption",
    "exemption",
    "federal tax",
    "irs",
    "local tax",
    "lookup",
    "regulation",
    "research",
    "sales tax",
    "search",
    "state tax",
    "tax",
    "use tax",
}


def should_run_ai_accountant_web_research(prompt: str) -> bool:
    normalized = " ".join(str(prompt or "").strip().lower().split())
    if not normalized:
        return False
    return any(term in normalized for term in ACCOUNTANT_WEB_RESEARCH_TERMS)


def _clean_duckduckgo_href(value: str) -> str:
    raw = html.unescape(str(value or "").strip())
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.netloc.endswith("duckduckgo.com") or parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(str(uddg[0] or "").strip())
    return raw


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(text).split())


def search_ai_accountant_web(
    query: str,
    *,
    limit: int = 5,
    timeout_seconds: int = 10,
) -> list[dict[str, Any]]:
    normalized_query = " ".join(str(query or "").strip().split())
    if not normalized_query:
        return []
    max_rows = max(1, min(10, int(limit or 5)))
    response = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": normalized_query},
        headers={"User-Agent": "GoldenStackersAIAccountant/1.0"},
        timeout=max(2, min(30, int(timeout_seconds or 10))),
    )
    response.raise_for_status()
    body = str(getattr(response, "text", "") or "")
    rows: list[dict[str, Any]] = []
    for match in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        url = _clean_duckduckgo_href(match.group("href"))
        title = _strip_tags(match.group("title"))
        if not url or not title:
            continue
        tail = body[match.end() : match.end() + 1500]
        snippet_match = re.search(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
            tail,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet = _strip_tags(snippet_match.group("snippet")) if snippet_match else ""
        rows.append(
            {
                "title": title[:240],
                "url": url[:1000],
                "snippet": snippet[:500],
                "source": "duckduckgo_html",
            }
        )
        if len(rows) >= max_rows:
            break
    return rows
