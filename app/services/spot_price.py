from dataclasses import dataclass
from datetime import datetime, timezone
import time

import requests

from app.config import settings
from app.services.runtime_settings import get_runtime_str

TROY_OUNCE_IN_GRAMS = 31.1034768


@dataclass
class SpotQuote:
    metal: str
    usd_per_troy_oz: float
    as_of: datetime
    source: str


class SpotRateLimitError(RuntimeError):
    pass


class SpotPriceService:
    def __init__(self, repo=None) -> None:
        self.provider = get_runtime_str(repo, "spot_price_provider", settings.spot_price_provider).strip().lower()
        self.api_key = get_runtime_str(repo, "metals_api_key", settings.metals_api_key).strip()
        self.metals_api_base_url = get_runtime_str(
            repo,
            "metals_api_base_url",
            settings.metals_api_base_url,
        ).strip().rstrip("/")
        self.yahoo_base_url = get_runtime_str(
            repo,
            "yahoo_finance_base_url",
            settings.yahoo_finance_base_url,
        ).strip().rstrip("/")
        self.yahoo_symbols = {
            "gold": get_runtime_str(repo, "yahoo_symbol_gold", settings.yahoo_symbol_gold).strip(),
            "silver": get_runtime_str(repo, "yahoo_symbol_silver", settings.yahoo_symbol_silver).strip(),
            "platinum": get_runtime_str(repo, "yahoo_symbol_platinum", settings.yahoo_symbol_platinum).strip(),
        }

    def is_configured(self) -> bool:
        if self.provider == "yahoo_finance":
            return True
        if self.provider == "metals_api":
            return bool(self.api_key)
        return False

    def latest_quotes(self) -> dict[str, SpotQuote]:
        if self.provider == "yahoo_finance":
            return self._latest_quotes_yahoo()
        if self.provider == "metals_api":
            return self._latest_quotes_metals_api()
        raise RuntimeError(
            "Unsupported spot price provider. Set SPOT_PRICE_PROVIDER to yahoo_finance or metals_api."
        )

    def _latest_quotes_metals_api(self) -> dict[str, SpotQuote]:
        if not self.api_key:
            raise RuntimeError("Missing METALS_API_KEY. Configure it in environment settings.")

        endpoint = f"{self.metals_api_base_url}/latest"
        params = {
            "access_key": self.api_key,
            "base": "USD",
            "symbols": "XAU,XAG,XPT",
        }

        response = requests.get(endpoint, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()

        if not payload.get("success", False):
            raise RuntimeError(f"Spot API error: {payload}")

        rates = payload.get("rates", {})
        ts = datetime.fromtimestamp(payload.get("timestamp", 0), tz=timezone.utc)

        quotes: dict[str, SpotQuote] = {}
        for metal_code, name in {"XAU": "gold", "XAG": "silver", "XPT": "platinum"}.items():
            rate = rates.get(metal_code)
            if not rate:
                continue
            usd_per_troy_oz = 1.0 / float(rate)
            quotes[name] = SpotQuote(
                metal=name,
                usd_per_troy_oz=usd_per_troy_oz,
                as_of=ts,
                source="metals-api.com",
            )

        if not quotes:
            raise RuntimeError("No spot quotes returned from provider.")

        return quotes

    def _latest_quotes_yahoo(self) -> dict[str, SpotQuote]:
        quotes: dict[str, SpotQuote] = {}
        now = datetime.now(tz=timezone.utc)

        for metal, symbol in self.yahoo_symbols.items():
            endpoint = f"{self.yahoo_base_url}/{symbol}"
            params = {"interval": "1d", "range": "5d"}

            payload = self._request_yahoo_json(endpoint=endpoint, params=params)

            result = payload.get("chart", {}).get("result", [])
            if not result:
                continue

            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price:
                indicators = result[0].get("indicators", {}).get("quote", [])
                closes = indicators[0].get("close", []) if indicators else []
                closes = [c for c in closes if c is not None]
                if closes:
                    price = closes[-1]
            if not price:
                continue

            quotes[metal] = SpotQuote(
                metal=metal,
                usd_per_troy_oz=float(price),
                as_of=now,
                source=f"Yahoo Finance ({symbol})",
            )

        if not quotes:
            raise RuntimeError("No spot quotes returned from Yahoo Finance.")

        return quotes

    def _request_yahoo_json(self, endpoint: str, params: dict) -> dict:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GoldenStackers/1.0)"}

        delays = [0.0, 1.5, 3.0]
        last_error: Exception | None = None
        for delay in delays:
            if delay > 0:
                time.sleep(delay)
            try:
                response = requests.get(endpoint, params=params, headers=headers, timeout=20)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            time.sleep(float(retry_after))
                        except ValueError:
                            pass
                    last_error = SpotRateLimitError(
                        "Yahoo Finance rate limit (HTTP 429). Try again shortly."
                    )
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc

        if isinstance(last_error, SpotRateLimitError):
            raise last_error
        raise RuntimeError(f"Yahoo Finance fetch failed: {last_error}")


def grams_to_troy_oz(grams: float) -> float:
    return grams / TROY_OUNCE_IN_GRAMS


def troy_oz_to_grams(troy_oz: float) -> float:
    return troy_oz * TROY_OUNCE_IN_GRAMS
