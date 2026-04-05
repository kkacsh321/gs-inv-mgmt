import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.services.spot_price import (
    SpotPriceService,
    SpotRateLimitError,
    grams_to_troy_oz,
    troy_oz_to_grams,
)


class _FakeResp:
    def __init__(self, *, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _runtime_passthrough(_repo, _key, default):
    return default


class SpotPriceTests(unittest.TestCase):
    def _settings(self, **overrides):
        base = {
            "spot_price_provider": "yahoo_finance",
            "metals_api_key": "",
            "metals_api_base_url": "https://metals.local",
            "yahoo_finance_base_url": "https://query1.finance.yahoo.com/v8/finance/chart",
            "yahoo_symbol_gold": "GC=F",
            "yahoo_symbol_silver": "SI=F",
            "yahoo_symbol_platinum": "PL=F",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_unit_conversions(self):
        self.assertAlmostEqual(troy_oz_to_grams(1.0), 31.1034768)
        self.assertAlmostEqual(grams_to_troy_oz(31.1034768), 1.0)

    def test_is_configured_by_provider(self):
        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="yahoo_finance")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()
            self.assertTrue(svc.is_configured())

        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="metals_api", metals_api_key="")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()
            self.assertFalse(svc.is_configured())

        with patch(
            "app.services.spot_price.settings",
            self._settings(spot_price_provider="metals_api", metals_api_key="key123"),
        ), patch("app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough):
            svc = SpotPriceService()
            self.assertTrue(svc.is_configured())

    def test_latest_quotes_unsupported_provider(self):
        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="other")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()
            with self.assertRaisesRegex(RuntimeError, "Unsupported spot price provider"):
                svc.latest_quotes()

    def test_latest_quotes_metals_api_paths(self):
        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="metals_api", metals_api_key="")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()
            with self.assertRaisesRegex(RuntimeError, "Missing METALS_API_KEY"):
                svc._latest_quotes_metals_api()

        payload_ok = {
            "success": True,
            "timestamp": int(datetime(2026, 3, 29, tzinfo=timezone.utc).timestamp()),
            "rates": {"XAU": 0.0005, "XAG": 0.04},
        }
        with patch(
            "app.services.spot_price.settings",
            self._settings(spot_price_provider="metals_api", metals_api_key="abc"),
        ), patch("app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough), patch(
            "app.services.spot_price.requests.get", return_value=_FakeResp(payload=payload_ok)
        ):
            svc = SpotPriceService()
            quotes = svc._latest_quotes_metals_api()
        self.assertIn("gold", quotes)
        self.assertIn("silver", quotes)
        self.assertAlmostEqual(quotes["gold"].usd_per_troy_oz, 2000.0)

        with patch(
            "app.services.spot_price.settings",
            self._settings(spot_price_provider="metals_api", metals_api_key="abc"),
        ), patch("app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough), patch(
            "app.services.spot_price.requests.get",
            return_value=_FakeResp(payload={"success": False, "error": "bad key"}),
        ):
            svc = SpotPriceService()
            with self.assertRaisesRegex(RuntimeError, "Spot API error"):
                svc._latest_quotes_metals_api()

        with patch(
            "app.services.spot_price.settings",
            self._settings(spot_price_provider="metals_api", metals_api_key="abc"),
        ), patch("app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough), patch(
            "app.services.spot_price.requests.get", return_value=_FakeResp(payload={"success": True, "timestamp": 0, "rates": {}})
        ):
            svc = SpotPriceService()
            with self.assertRaisesRegex(RuntimeError, "No spot quotes returned"):
                svc._latest_quotes_metals_api()

    def test_latest_quotes_yahoo_paths(self):
        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="yahoo_finance")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()
            with patch.object(
                svc,
                "_request_yahoo_json",
                side_effect=[
                    {"chart": {"result": [{"meta": {"regularMarketPrice": 2100.25}}]}},
                    {"chart": {"result": [{"meta": {"previousClose": 25.5}}]}},
                    {"chart": {"result": [{"meta": {}, "indicators": {"quote": [{"close": [None, 1000.0]}]}}]}},
                ],
            ):
                quotes = svc._latest_quotes_yahoo()
        self.assertEqual(set(quotes.keys()), {"gold", "silver", "platinum"})
        self.assertEqual(quotes["silver"].usd_per_troy_oz, 25.5)
        self.assertEqual(quotes["platinum"].usd_per_troy_oz, 1000.0)

        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="yahoo_finance")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()
            with patch.object(svc, "_request_yahoo_json", return_value={"chart": {"result": []}}):
                with self.assertRaisesRegex(RuntimeError, "No spot quotes returned"):
                    svc._latest_quotes_yahoo()

    def test_request_yahoo_json_success_and_retries(self):
        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="yahoo_finance")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()

        with patch("app.services.spot_price.requests.get", return_value=_FakeResp(payload={"chart": {"result": [1]}})):
            payload = svc._request_yahoo_json(endpoint="https://x", params={})
        self.assertEqual(payload["chart"]["result"], [1])

        responses = [
            _FakeResp(status_code=429, headers={"Retry-After": "0"}),
            _FakeResp(payload={"chart": {"result": [2]}}),
        ]
        with patch("app.services.spot_price.requests.get", side_effect=responses), patch(
            "app.services.spot_price.time.sleep"
        ) as sleep:
            payload = svc._request_yahoo_json(endpoint="https://x", params={})
        self.assertEqual(payload["chart"]["result"], [2])
        self.assertTrue(sleep.called)

    def test_request_yahoo_json_rate_limit_and_request_exception(self):
        with patch("app.services.spot_price.settings", self._settings(spot_price_provider="yahoo_finance")), patch(
            "app.services.spot_price.get_runtime_str", side_effect=_runtime_passthrough
        ):
            svc = SpotPriceService()

        with patch(
            "app.services.spot_price.requests.get",
            side_effect=[_FakeResp(status_code=429), _FakeResp(status_code=429), _FakeResp(status_code=429)],
        ), patch("app.services.spot_price.time.sleep"):
            with self.assertRaises(SpotRateLimitError):
                svc._request_yahoo_json(endpoint="https://x", params={})

        with patch(
            "app.services.spot_price.requests.get",
            side_effect=requests.RequestException("network down"),
        ), patch("app.services.spot_price.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Yahoo Finance fetch failed"):
                svc._request_yahoo_json(endpoint="https://x", params={})


if __name__ == "__main__":
    unittest.main()

