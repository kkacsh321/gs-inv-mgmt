import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.services.ebay import EbayClient, normalize_ebay_condition_policy_rows


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict | list | None = None,
        text: str = "",
        headers: dict | None = None,
        raise_http_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}
        self._raise_http_error = raise_http_error

    def raise_for_status(self):
        if self._raise_http_error or self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


class EbayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        EbayClient._finding_rate_limited_until = None
        EbayClient._finding_last_probe_at = None
        EbayClient._finding_last_error = {}

    def _settings(self, **overrides):
        base = {
            "ebay_environment": "sandbox",
            "ebay_client_id": "cid",
            "ebay_client_secret": "csecret",
            "ebay_ru_name": "runame",
            "ebay_marketplace_id": "EBAY_US",
            "ebay_finding_rate_limit_cooldown_seconds": 600,
            "ebay_finding_rate_limit_severe_cooldown_seconds": 3600,
            "ebay_finding_rate_limit_probe_interval_seconds": 120,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_authorize_url_contains_expected_params(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
            url = client.authorize_url(state="abc123")
        self.assertIn("client_id=cid", url)
        self.assertIn("redirect_uri=runame", url)
        self.assertIn("state=abc123", url)

    def test_is_configured_and_basic_helpers(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
            self.assertTrue(client.is_configured())
            self.assertIn("identity/v1/oauth2/token", client._token_endpoint())
            self.assertIn("Basic", f"Basic {client._basic_auth_header()}")

        with patch("app.services.ebay.settings", self._settings(ebay_client_id="", ebay_client_secret="", ebay_ru_name="")):
            self.assertFalse(EbayClient().is_configured())

    def test_rest_headers_content_language_optional(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        h1 = client._rest_headers("tok")
        self.assertNotIn("Content-Language", h1)
        h2 = client._rest_headers("tok", content_language="en-US")
        self.assertEqual(h2["Content-Language"], "en-US")

    def test_exchange_code_for_tokens_and_application_token(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(payload={"access_token": "x"})) as post:
            tokens = client.exchange_code_for_tokens("code123")
        self.assertEqual(tokens["access_token"], "x")
        self.assertIn("authorization_code", str(post.call_args.kwargs["data"]))

        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(payload={"token_type": "Bearer"})) as post:
            app_tok = client.fetch_application_token()
        self.assertEqual(app_tok["token_type"], "Bearer")
        self.assertIn("client_credentials", str(post.call_args.kwargs["data"]))

    def test_refresh_user_token(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch(
            "app.services.ebay.requests.post",
            return_value=_FakeResponse(payload={"access_token": "newtok", "refresh_token": "newref", "expires_in": 7200}),
        ) as post:
            out = client.refresh_user_token("oldref")
        self.assertEqual(out.get("access_token"), "newtok")
        payload = post.call_args.kwargs["data"]
        self.assertEqual(payload.get("grant_type"), "refresh_token")
        self.assertEqual(payload.get("refresh_token"), "oldref")

    def test_get_account_privileges(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"privileges": []})):
            out = client.get_account_privileges("tok")
        self.assertIn("privileges", out)

    def test_get_identity_user(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch(
            "app.services.ebay.requests.get",
            return_value=_FakeResponse(payload={"username": "sandbox-user-1"}),
        ):
            out = client.get_identity_user("tok")
        self.assertEqual(out.get("username"), "sandbox-user-1")

    def test_get_item_aspects_for_category_fetches_default_tree_then_aspects(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        tree = _FakeResponse(payload={"categoryTreeId": "0"}, text="tree")
        aspects = _FakeResponse(
            payload={
                "aspects": [
                    {
                        "localizedAspectName": "Brand",
                        "aspectConstraint": {"aspectRequired": True},
                    }
                ]
            },
            text="aspects",
        )
        with patch("app.services.ebay.requests.get", side_effect=[tree, aspects]) as req_get:
            rows = client.get_item_aspects_for_category(
                access_token="tok",
                category_id="111",
                marketplace_id="EBAY_US",
            )
        self.assertEqual(rows[0]["localizedAspectName"], "Brand")
        self.assertEqual(req_get.call_count, 2)
        self.assertIn("get_default_category_tree_id", req_get.call_args_list[0].args[0])
        self.assertIn("get_item_aspects_for_category", req_get.call_args_list[1].args[0])
        self.assertEqual(req_get.call_args_list[1].kwargs["params"]["category_id"], "111")

    def test_get_item_condition_policies_filters_by_category(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        response = _FakeResponse(
            payload={
                "itemConditionPolicies": [
                    {
                        "categoryId": "111",
                        "itemConditionRequired": True,
                        "itemConditions": [
                            {"conditionId": "3000", "conditionDescription": "Used"},
                        ],
                    }
                ]
            },
            text="conditions",
        )
        with patch("app.services.ebay.requests.get", return_value=response) as req_get:
            policies = client.get_item_condition_policies(
                access_token="tok",
                category_id="111",
                marketplace_id="EBAY_US",
            )
        self.assertEqual(policies[0]["categoryId"], "111")
        self.assertIn("get_item_condition_policies", req_get.call_args.args[0])
        self.assertEqual(req_get.call_args.kwargs["params"]["filter"], "categoryIds:{111}")

    def test_normalize_condition_policy_rows_maps_condition_ids_to_inventory_enums(self) -> None:
        rows = normalize_ebay_condition_policy_rows(
            [
                {
                    "categoryId": "111",
                    "itemConditionRequired": True,
                    "itemConditions": [
                        {"conditionId": "1000", "conditionDescription": "Brand New"},
                        {"conditionId": "3000", "conditionDescription": "Used"},
                        {"conditionId": "999999", "conditionDescription": "Unsupported"},
                    ],
                }
            ],
            category_id="111",
        )
        self.assertEqual([row["condition"] for row in rows], ["NEW", "USED_EXCELLENT"])
        self.assertEqual(rows[0]["label"], "Brand New")
        self.assertTrue(rows[0]["required"])
        self.assertEqual(rows[1]["condition_id"], "3000")

    def test_decode_access_token_claims(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        token = "a.eyJzdWIiOiJ1c2VyLTEyMyIsInByZWZlcnJlZF91c2VybmFtZSI6ImVheS11c2VyIiwiZXhwIjoxNzAwMDAwMDAwfQ.b"
        claims = client.decode_access_token_claims(token)
        self.assertEqual(claims.get("sub"), "user-123")
        self.assertEqual(claims.get("preferred_username"), "eay-user")
        self.assertEqual(claims.get("exp"), 1700000000)

    def test_decode_access_token_claims_invalid_returns_empty(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        self.assertEqual(client.decode_access_token_claims("not-a-jwt"), {})

    def test_policy_list_calls(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"paymentPolicies": [{"id": "p1"}]})):
            rows = client.list_payment_policies(access_token="tok", marketplace_id="EBAY_US")
        self.assertEqual(rows[0]["id"], "p1")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"fulfillmentPolicies": [{"id": "f1"}]})):
            rows = client.list_fulfillment_policies(access_token="tok", marketplace_id="EBAY_US")
        self.assertEqual(rows[0]["id"], "f1")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"returnPolicies": [{"id": "r1"}]})):
            rows = client.list_return_policies(access_token="tok", marketplace_id="EBAY_US")
        self.assertEqual(rows[0]["id"], "r1")

    def test_raise_for_status_with_body_includes_response_preview(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        response = _FakeResponse(status_code=400, text="bad payload", raise_http_error=True)
        with self.assertRaises(requests.HTTPError) as ctx:
            client._raise_for_status_with_body(response)
        self.assertIn("eBay response body: bad payload", str(ctx.exception))

    def test_list_inventory_locations_fallbacks_on_400(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        first = _FakeResponse(status_code=400, payload={"errors": ["bad"]}, text="bad")
        second = _FakeResponse(status_code=200, payload={"locations": [{"merchantLocationKey": "LOC1"}]}, text="ok")
        with patch("app.services.ebay.requests.get", side_effect=[first, second]) as req_get:
            rows = client.list_inventory_locations(access_token="tok", limit=200, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["merchantLocationKey"], "LOC1")
        self.assertEqual(req_get.call_count, 2)

    def test_finance_transactions_use_apiz_host_and_marketplace_header(self) -> None:
        with patch("app.services.ebay.settings", self._settings(ebay_environment="production", ebay_marketplace_id="EBAY_US")):
            client = EbayClient()
        with patch(
            "app.services.ebay.requests.get",
            return_value=_FakeResponse(status_code=200, payload={"transactions": []}, text="ok"),
        ) as req_get:
            _ = client.list_finance_transactions(access_token="tok", limit=5)
            _ = client.list_finance_transactions_for_order(access_token="tok", order_id="23-1-1", limit=5)
        self.assertEqual(req_get.call_count, 2)
        first_url = req_get.call_args_list[0].args[0]
        second_url = req_get.call_args_list[1].args[0]
        first_headers = req_get.call_args_list[0].kwargs.get("headers") or {}
        self.assertIn("apiz.ebay.com/sell/finances/v1/transaction", first_url)
        self.assertIn("apiz.ebay.com/sell/finances/v1/transaction", second_url)
        self.assertEqual(first_headers.get("X-EBAY-C-MARKETPLACE-ID"), "EBAY_US")

    def test_create_video_prefers_location_header(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        response = _FakeResponse(
            status_code=201,
            payload={"videoId": "VID-FALLBACK"},
            text='{"videoId":"VID-FALLBACK"}',
            headers={"Location": "https://apim.sandbox.ebay.com/commerce/media/v1_beta/video/VID-123"},
        )
        with patch("app.services.ebay.requests.post", return_value=response):
            vid = client.create_video(access_token="tok", title="title", size_bytes=1234)
        self.assertEqual(vid, "VID-123")

    def test_create_video_uses_json_id_without_location_header(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        response = _FakeResponse(
            status_code=201,
            payload={"videoId": "VID-JSON"},
            text='{"videoId":"VID-JSON"}',
            headers={},
        )
        with patch("app.services.ebay.requests.post", return_value=response):
            vid = client.create_video(access_token="tok", title="title", size_bytes=1234)
        self.assertEqual(vid, "VID-JSON")

    def test_create_video_raises_when_no_id_found(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        response = _FakeResponse(status_code=201, payload={}, text="", headers={})
        with patch("app.services.ebay.requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "no video ID"):
                client.create_video(access_token="tok", title="title", size_bytes=1234)

    def test_finding_global_id_mapping(self) -> None:
        with patch("app.services.ebay.settings", self._settings(ebay_marketplace_id="EBAY_US")):
            self.assertEqual(EbayClient()._finding_global_id(), "EBAY-US")
        with patch("app.services.ebay.settings", self._settings(ebay_marketplace_id="EBAY_GB")):
            self.assertEqual(EbayClient()._finding_global_id(), "EBAY-GB")
        with patch("app.services.ebay.settings", self._settings(ebay_marketplace_id="CUSTOM")):
            self.assertEqual(EbayClient()._finding_global_id(), "CUSTOM")

    def test_find_completed_items_parses_rows_and_global_id_retry(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        bad = _FakeResponse(
            status_code=400,
            payload={"errorMessage": [{"error": [{"message": ["Please specify a valid GLOBAL-ID"]}]}]},
            text='{"errorMessage":[{"error":[{"message":["Please specify a valid GLOBAL-ID"]}]}]}',
        )
        ok = _FakeResponse(
            status_code=200,
            payload={
                "findCompletedItemsResponse": [
                    {
                        "ack": ["Success"],
                        "searchResult": [
                            {
                                "item": [
                                    {
                                        "title": ["Coin 1"],
                                        "itemId": ["123"],
                                        "viewItemURL": ["http://example/item/123"],
                                        "galleryURL": ["http://example/image.jpg"],
                                        "condition": [{"conditionDisplayName": ["Used"]}],
                                        "listingInfo": [{"endTime": ["2026-03-29T00:00:00Z"]}],
                                        "sellingStatus": [{"currentPrice": [{"__value__": "10.5", "@currencyId": "USD"}]}],
                                        "shippingInfo": [{"shippingServiceCost": [{"__value__": "2.5"}]}],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
            text="ok",
        )
        with patch("app.services.ebay.requests.get", side_effect=[bad, ok]) as req_get:
            rows = client.find_completed_items(keywords="coin", sold_only=True)
        self.assertEqual(req_get.call_count, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], "123")
        self.assertEqual(rows[0]["total_price"], 13.0)

    def test_find_completed_items_rate_limited_raises_runtime_error(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        rate_limited = _FakeResponse(
            status_code=500,
            payload={
                "errorMessage": [
                    {
                        "error": [
                            {
                                "errorId": ["10001"],
                                "domain": ["Security"],
                                "subdomain": ["RateLimiter"],
                                "message": ["Service call has exceeded the number of times the operation is allowed to be called"],
                            }
                        ]
                    }
                ]
            },
            text='{"errorMessage":[{"error":[{"errorId":["10001"],"subdomain":["RateLimiter"],"message":["Service call has exceeded the number of times the operation is allowed to be called"]}]}]}',
        )
        with patch("app.services.ebay.requests.get", return_value=rate_limited):
            with self.assertRaisesRegex(RuntimeError, "EBAY_FINDING_RATE_LIMITED"):
                client.find_completed_items(keywords="silver bar")
        self.assertGreater(EbayClient.finding_rate_limit_cooldown_remaining_seconds(), 0)
        err = EbayClient.finding_last_error()
        self.assertEqual(err.get("type"), "remote_rate_limited")
        self.assertEqual(err.get("rate_limit_scope"), "severe_quota_exhausted")
        self.assertGreaterEqual(int(err.get("cooldown_seconds") or 0), 3600)

    def test_find_completed_items_rate_limit_cooldown_short_circuit(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        EbayClient._set_finding_rate_limit_cooldown()
        # Simulate a recent probe so this call must short-circuit locally.
        EbayClient._finding_last_probe_at = datetime.now(timezone.utc)
        with patch("app.services.ebay.requests.get") as req_get:
            with self.assertRaisesRegex(RuntimeError, "EBAY_FINDING_RATE_LIMITED"):
                client.find_completed_items(keywords="silver")
        req_get.assert_not_called()

    def test_find_completed_items_allows_probe_during_cooldown_and_clears_on_success(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        EbayClient._set_finding_rate_limit_cooldown()
        ok = _FakeResponse(
            status_code=200,
            payload={
                "findCompletedItemsResponse": [
                    {"ack": ["Success"], "searchResult": [{"item": []}]}
                ]
            },
            text="ok",
        )
        with patch("app.services.ebay.requests.get", return_value=ok) as req_get:
            rows = client.find_completed_items(keywords="silver", source="unit_test_probe")
        self.assertEqual(rows, [])
        req_get.assert_called_once()
        self.assertEqual(EbayClient.finding_rate_limit_cooldown_remaining_seconds(), 0)

    def test_find_completed_items_telemetry_snapshot(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        ok = _FakeResponse(
            status_code=200,
            payload={
                "findCompletedItemsResponse": [
                    {"ack": ["Success"], "searchResult": [{"item": []}]}
                ]
            },
            text="ok",
        )
        with patch("app.services.ebay.requests.get", return_value=ok):
            client.find_completed_items(keywords="credit suisse", source="unit_test_source")
        snap = EbayClient.finding_call_snapshot(window_seconds=600)
        self.assertGreaterEqual(int(snap.get("count") or 0), 1)
        self.assertIn("unit_test_source", snap.get("by_source") or {})

    def test_pull_get_and_fulfillment_calls(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"orders": [{"orderId": "o1"}]})):
            out = client.pull_recent_orders("tok", limit=50, offset=0)
        self.assertEqual(out["orders"][0]["orderId"], "o1")

        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"orderId": "o1"})):
            out = client.get_order(access_token="tok", order_id="o1")
        self.assertEqual(out["orderId"], "o1")

        with patch(
            "app.services.ebay.requests.get",
            return_value=_FakeResponse(payload={"fulfillments": [{"id": "a"}]}, text='{"fulfillments":[{"id":"a"}]}'),
        ):
            rows = client.list_shipping_fulfillments(access_token="tok", order_id="o1")
        self.assertEqual(rows[0]["id"], "a")
        with patch(
            "app.services.ebay.requests.get",
            return_value=_FakeResponse(
                payload={"shippingFulfillments": [{"id": "b"}]},
                text='{"shippingFulfillments":[{"id":"b"}]}',
            ),
        ):
            rows = client.list_shipping_fulfillments(access_token="tok", order_id="o1")
        self.assertEqual(rows[0]["id"], "b")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload=[{"id": "c"}], text='[{"id":"c"}]')):
            rows = client.list_shipping_fulfillments(access_token="tok", order_id="o1")
        self.assertEqual(rows[0]["id"], "c")

    def test_offer_media_and_video_calls(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        with patch("app.services.ebay.requests.put", return_value=_FakeResponse(status_code=204, payload={}, text="")):
            client.create_or_replace_inventory_item(access_token="tok", sku="SKU1", payload={"availability": {}})

        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(payload={"offerId": "off1"})):
            out = client.create_offer(access_token="tok", payload={"sku": "SKU1"})
        self.assertEqual(out["offerId"], "off1")

        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"offerId": "off1"})):
            out = client.get_offer(access_token="tok", offer_id="off1")
        self.assertEqual(out["offerId"], "off1")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"offers": []})):
            out = client.get_offers(access_token="tok", sku="SKU1")
        self.assertIn("offers", out)

        with patch("app.services.ebay.requests.put", return_value=_FakeResponse(payload={}, text="")):
            out = client.update_offer(access_token="tok", offer_id="off1", payload={"availableQuantity": 1})
        self.assertEqual(out, {})

        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(payload={"listingId": "123"})):
            out = client.publish_offer(access_token="tok", offer_id="off1")
        self.assertEqual(out["listingId"], "123")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"sku": "SKU1"}, text='{"sku":"SKU1"}')):
            out = client.get_inventory_item(access_token="tok", sku="SKU1")
        self.assertEqual(out["sku"], "SKU1")
        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(payload={}, text="")):
            out = client.withdraw_offer(access_token="tok", offer_id="off1")
        self.assertEqual(out, {})

        with patch(
            "app.services.ebay.requests.post",
            return_value=_FakeResponse(payload={"image": {"imageUrl": "http://img"}}, text='{"image":{"imageUrl":"http://img"}}'),
        ):
            out = client.create_image_from_url(access_token="tok", image_url="http://img")
        self.assertIn("image", out)

    def test_publish_offer_retries_product_not_found_after_inventory_probe(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        product_not_found = _FakeResponse(
            status_code=500,
            payload={
                "errors": [
                    {
                        "errorId": 25604,
                        "message": "Input error. Seller Inventory Service can not publish the data. Product not found.",
                    }
                ]
            },
            text='{"errors":[{"errorId":25604,"message":"Product not found"}]}',
        )
        success = _FakeResponse(payload={"listingId": "123"}, text='{"listingId":"123"}')
        with patch("app.services.ebay.time.sleep") as sleep, patch(
            "app.services.ebay.requests.get",
            return_value=_FakeResponse(payload={"sku": "SKU1"}, text='{"sku":"SKU1"}'),
        ) as get, patch("app.services.ebay.requests.post", side_effect=[product_not_found, success]) as post:
            out = client.publish_offer(
                access_token="tok",
                offer_id="off1",
                inventory_sku="SKU1",
                retry_product_not_found_delay_seconds=0.01,
            )

        self.assertEqual(out["listingId"], "123")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(get.call_count, 1)
        sleep.assert_called_once()

    def test_publish_offer_does_not_retry_non_product_not_found_error(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        response = _FakeResponse(status_code=500, text='{"errors":[{"errorId":999,"message":"boom"}]}')
        with patch("app.services.ebay.requests.post", return_value=response) as post:
            with self.assertRaises(requests.HTTPError):
                client.publish_offer(access_token="tok", offer_id="off1", inventory_sku="SKU1")
        self.assertEqual(post.call_count, 1)

        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(status_code=200, payload={}, text="")) as post:
            client.upload_video(access_token="tok", video_id="VID1", file_bytes=b"abc")
        self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"], "application/octet-stream")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"videoId": "VID1"})):
            out = client.get_video(access_token="tok", video_id="VID1")
        self.assertEqual(out["videoId"], "VID1")

    def test_get_trading_item_video_ids_parses_video_details(self) -> None:
        with patch("app.services.ebay.settings", self._settings(ebay_environment="production")):
            client = EbayClient()

        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
          <Ack>Success</Ack>
          <Item>
            <ItemID>123</ItemID>
            <VideoDetails>
              <VideoID>VID-MOV</VideoID>
            </VideoDetails>
          </Item>
        </GetItemResponse>"""
        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(text=xml)) as post:
            out = client.get_trading_item_video_ids(
                access_token="tok",
                item_id="123",
                marketplace_id="EBAY_US",
            )

        self.assertEqual(out["video_ids"], ["VID-MOV"])
        self.assertEqual(out["site_id"], "0")
        self.assertEqual(post.call_args.kwargs["headers"]["X-EBAY-API-CALL-NAME"], "GetItem")
        self.assertEqual(post.call_args.kwargs["headers"]["X-EBAY-API-IAF-TOKEN"], "tok")

    def test_get_trading_item_video_ids_raises_on_failure_ack(self) -> None:
        with patch("app.services.ebay.settings", self._settings(ebay_environment="production")):
            client = EbayClient()

        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
          <Ack>Failure</Ack>
          <Errors>
            <ShortMessage>Invalid item.</ShortMessage>
            <ErrorCode>17</ErrorCode>
          </Errors>
        </GetItemResponse>"""
        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(text=xml)):
            with self.assertRaisesRegex(RuntimeError, "Trading GetItem failed"):
                client.get_trading_item_video_ids(access_token="tok", item_id="123")

    def test_create_inventory_item_blocks_overlong_condition_description_before_api_call(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        with patch("app.services.ebay.requests.put") as put:
            with self.assertRaisesRegex(ValueError, "1000 characters or fewer"):
                client.create_or_replace_inventory_item(
                    access_token="tok",
                    sku="SKU1",
                    payload={
                        "availability": {},
                        "condition": "USED_EXCELLENT",
                        "conditionDescription": "x" * 1001,
                        "product": {"title": "Test"},
                    },
                )
        put.assert_not_called()

    def test_verify_publish_dependencies_blocks_immediate_pay_auction_without_bin(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        with patch.object(client, "resolve_merchant_location_key", return_value="loc"), patch.object(
            client,
            "get_inventory_location",
            return_value={"merchantLocationKey": "loc"},
        ), patch.object(
            client,
            "get_payment_policy",
            return_value={"paymentPolicyId": "pay-1", "immediatePay": True},
        ), patch.object(
            client,
            "get_fulfillment_policy",
            return_value={"fulfillmentPolicyId": "ful-1"},
        ), patch.object(
            client,
            "get_return_policy",
            return_value={"returnPolicyId": "ret-1"},
        ), patch.object(
            client,
            "get_category_subtree",
            return_value={"categoryTreeNode": {"category": {"categoryId": "166679"}}},
        ):
            result = client.verify_publish_dependencies(
                access_token="tok",
                marketplace_id="EBAY_US",
                category_id="166679",
                merchant_location_key="loc",
                payment_policy_id="pay-1",
                fulfillment_policy_id="ful-1",
                return_policy_id="ret-1",
                format_type="AUCTION",
                auction_buy_now_price=0.0,
            )

        self.assertIn(
            "payment_policy: immediate payment requires an Auction Buy It Now price for live auction publish",
            result["blockers"],
        )
        payment_check = next(row for row in result["checks"] if row["check"] == "payment_policy")
        self.assertIn("immediate_pay_required=true", payment_check["detail"])

    def test_verify_publish_dependencies_blocks_invalid_category_condition(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        condition_policy = {
            "itemConditionPolicies": [
                {
                    "categoryId": "166679",
                    "itemConditions": [
                        {"conditionId": "3000", "conditionDescription": "Used"},
                    ],
                }
            ]
        }
        with patch.object(client, "resolve_merchant_location_key", return_value="loc"), patch.object(
            client,
            "get_inventory_location",
            return_value={"merchantLocationKey": "loc"},
        ), patch.object(
            client,
            "get_payment_policy",
            return_value={"paymentPolicyId": "pay-1", "immediatePay": False},
        ), patch.object(
            client,
            "get_fulfillment_policy",
            return_value={"fulfillmentPolicyId": "ful-1"},
        ), patch.object(
            client,
            "get_return_policy",
            return_value={"returnPolicyId": "ret-1"},
        ), patch.object(
            client,
            "get_category_subtree",
            return_value={"categoryTreeNode": {"category": {"categoryId": "166679"}}},
        ), patch.object(
            client,
            "get_item_condition_policies",
            return_value=condition_policy,
        ):
            result = client.verify_publish_dependencies(
                access_token="tok",
                marketplace_id="EBAY_US",
                category_id="166679",
                merchant_location_key="loc",
                payment_policy_id="pay-1",
                fulfillment_policy_id="ful-1",
                return_policy_id="ret-1",
                condition="NEW",
            )

        self.assertTrue(
            any("category_condition: selected condition `NEW` is not valid" in row for row in result["blockers"])
        )
        condition_check = next(row for row in result["checks"] if row["check"] == "category_condition")
        self.assertFalse(condition_check["ok"])

    def test_verify_publish_dependencies_accepts_valid_category_condition(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()

        condition_policy = {
            "itemConditionPolicies": [
                {
                    "categoryId": "166679",
                    "itemConditions": [
                        {"conditionId": "3000", "conditionDescription": "Used"},
                    ],
                }
            ]
        }
        with patch.object(client, "resolve_merchant_location_key", return_value="loc"), patch.object(
            client,
            "get_inventory_location",
            return_value={"merchantLocationKey": "loc"},
        ), patch.object(
            client,
            "get_payment_policy",
            return_value={"paymentPolicyId": "pay-1", "immediatePay": False},
        ), patch.object(
            client,
            "get_fulfillment_policy",
            return_value={"fulfillmentPolicyId": "ful-1"},
        ), patch.object(
            client,
            "get_return_policy",
            return_value={"returnPolicyId": "ret-1"},
        ), patch.object(
            client,
            "get_category_subtree",
            return_value={"categoryTreeNode": {"category": {"categoryId": "166679"}}},
        ), patch.object(
            client,
            "get_item_condition_policies",
            return_value=condition_policy,
        ):
            result = client.verify_publish_dependencies(
                access_token="tok",
                marketplace_id="EBAY_US",
                category_id="166679",
                merchant_location_key="loc",
                payment_policy_id="pay-1",
                fulfillment_policy_id="ful-1",
                return_policy_id="ret-1",
                condition="USED_EXCELLENT",
            )

        self.assertEqual([], result["blockers"])
        condition_check = next(row for row in result["checks"] if row["check"] == "category_condition")
        self.assertTrue(condition_check["ok"])
        self.assertIn("USED_EXCELLENT valid", condition_check["detail"])

    def test_find_completed_items_returns_empty_on_blank_keywords(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        self.assertEqual(client.find_completed_items(keywords=""), [])

    def test_search_sold_items_html_parses_rows(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        html = """
        <li class="s-item">
          <a class="s-item__link" href="https://www.ebay.com/itm/137217809542">
            <h3 class="s-item__title">APMEX 1 oz Silver Bar .999 Fine</h3>
          </a>
          <span class="s-item__price">$39.95</span>
          <span class="s-item__shipping">$4.99 shipping</span>
        </li>
        """
        with patch(
            "app.services.ebay.requests.get",
            return_value=_FakeResponse(status_code=200, text=html, payload={}),
        ):
            rows = client.search_sold_items_html(keywords="apmex 1oz silver bar", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], "137217809542")
        self.assertEqual(rows[0]["sold_price"], 39.95)
        self.assertEqual(rows[0]["shipping_cost"], 4.99)
        self.assertAlmostEqual(float(rows[0]["total_price"]), 44.94, places=2)

    def test_find_completed_items_with_fallback_uses_html_on_rate_limit(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch.object(
            client,
            "find_completed_items",
            side_effect=RuntimeError("EBAY_FINDING_RATE_LIMITED: eBay Finding API rate limit exceeded for findCompletedItems."),
        ), patch.object(
            client,
            "search_sold_items_html",
            return_value=[{"item_id": "1", "title": "fallback", "sold_price": 10.0, "shipping_cost": 0.0, "total_price": 10.0}],
        ):
            outcome = client.find_completed_items_with_fallback(
                keywords="silver bar",
                sold_only=True,
                entries_per_page=25,
                source="unit_test",
            )
        self.assertEqual(outcome.get("mode"), "ebay_sold_html_primary")
        self.assertEqual(len(outcome.get("rows") or []), 1)

    def test_listing_url_for_environment(self) -> None:
        with patch("app.services.ebay.settings", self._settings(ebay_environment="sandbox")):
            self.assertIn("sandbox.ebay.com", EbayClient().listing_url_for_id("123"))
        with patch("app.services.ebay.settings", self._settings(ebay_environment="production")):
            self.assertEqual(EbayClient().listing_url_for_id("123"), "https://www.ebay.com/itm/123")


if __name__ == "__main__":
    unittest.main()
