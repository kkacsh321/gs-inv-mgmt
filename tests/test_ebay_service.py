import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.services.ebay import EbayClient


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
    def _settings(self, **overrides):
        base = {
            "ebay_environment": "sandbox",
            "ebay_client_id": "cid",
            "ebay_client_secret": "csecret",
            "ebay_ru_name": "runame",
            "ebay_marketplace_id": "EBAY_US",
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

    def test_get_account_privileges(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"privileges": []})):
            out = client.get_account_privileges("tok")
        self.assertIn("privileges", out)

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
        bad = _FakeResponse(status_code=400, payload={"error": "bad"}, text="bad")
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
        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(payload={}, text="")):
            out = client.withdraw_offer(access_token="tok", offer_id="off1")
        self.assertEqual(out, {})

        with patch(
            "app.services.ebay.requests.post",
            return_value=_FakeResponse(payload={"image": {"imageUrl": "http://img"}}, text='{"image":{"imageUrl":"http://img"}}'),
        ):
            out = client.create_image_from_url(access_token="tok", image_url="http://img")
        self.assertIn("image", out)

        with patch("app.services.ebay.requests.post", return_value=_FakeResponse(status_code=200, payload={}, text="")):
            client.upload_video(access_token="tok", video_id="VID1", file_bytes=b"abc")
        with patch("app.services.ebay.requests.get", return_value=_FakeResponse(payload={"videoId": "VID1"})):
            out = client.get_video(access_token="tok", video_id="VID1")
        self.assertEqual(out["videoId"], "VID1")

    def test_find_completed_items_returns_empty_on_blank_keywords(self) -> None:
        with patch("app.services.ebay.settings", self._settings()):
            client = EbayClient()
        self.assertEqual(client.find_completed_items(keywords=""), [])

    def test_listing_url_for_environment(self) -> None:
        with patch("app.services.ebay.settings", self._settings(ebay_environment="sandbox")):
            self.assertIn("sandbox.ebay.com", EbayClient().listing_url_for_id("123"))
        with patch("app.services.ebay.settings", self._settings(ebay_environment="production")):
            self.assertEqual(EbayClient().listing_url_for_id("123"), "https://www.ebay.com/itm/123")


if __name__ == "__main__":
    unittest.main()
