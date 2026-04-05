import json
import unittest
from unittest.mock import patch

from app.services import shipping_labels


class _FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, content: bytes = b"x") -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


class ShippingLabelsTests(unittest.TestCase):
    def test_as_float(self) -> None:
        self.assertEqual(shipping_labels._as_float("1.5"), 1.5)
        self.assertIsNone(shipping_labels._as_float(""))
        self.assertIsNone(shipping_labels._as_float("bad"))

    def test_pick_supports_nested_dict_and_list(self) -> None:
        body = {"label": {"id": "L1"}, "files": [{"url": "http://x"}]}
        self.assertEqual(shipping_labels._pick(body, "label.id"), "L1")
        self.assertEqual(shipping_labels._pick(body, "files.0.url"), "http://x")
        self.assertIsNone(shipping_labels._pick(body, "missing.path"))
        self.assertIsNone(shipping_labels._pick(body, "files.bad.url"))

    def test_pirateship_mock_mode(self) -> None:
        with patch("app.services.shipping_labels.get_runtime_str", side_effect=lambda *_args, **_kwargs: "mock"), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=20
        ):
            result = shipping_labels._pirateship_purchase_label(
                object(),
                payload={"sale_id": 55, "shipping_provider": "pirateship"},
            )
        self.assertIn("LBL-LIVE-55", result.label_id)
        self.assertEqual(result.provider_payload["mode"], "mock")
        self.assertTrue(result.tracking_number.startswith("PS-MOCK-"))

    def test_pirateship_api_mode_requires_base_url_and_key(self) -> None:
        def runtime_str(_repo, key, default):
            mapping = {
                "shipping_label_pirateship_mode": "api",
                "shipping_label_pirateship_base_url": "",
                "shipping_label_pirateship_api_key": "",
            }
            return mapping.get(key, default)

        with patch("app.services.shipping_labels.get_runtime_str", side_effect=runtime_str), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=20
        ):
            with self.assertRaisesRegex(ValueError, "requires base URL"):
                shipping_labels._pirateship_purchase_label(object(), payload={"sale_id": 1})

    def test_pirateship_api_mode_parses_response(self) -> None:
        def runtime_str(_repo, key, default):
            mapping = {
                "shipping_label_pirateship_mode": "api",
                "shipping_label_pirateship_base_url": "https://pirate.example",
                "shipping_label_pirateship_api_key": "secret",
                "shipping_label_pirateship_endpoint_path": "/v1/labels/purchase",
                "shipping_label_pirateship_auth_scheme": "bearer",
            }
            return mapping.get(key, default)

        api_payload = {
            "shipment": {"label_id": "LBL-API-1", "tracking_number": "TRACK-1"},
            "files": [{"url": "https://labels.example/LBL-API-1.pdf"}],
            "cost": {"total": "3.25", "currency": "USD"},
        }

        with patch("app.services.shipping_labels.get_runtime_str", side_effect=runtime_str), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=20
        ), patch("app.services.shipping_labels.requests.post", return_value=_FakeResponse(api_payload)):
            result = shipping_labels._pirateship_purchase_label(
                object(),
                payload={"sale_id": 1, "shipping_provider": "pirateship"},
            )
        self.assertEqual(result.label_id, "LBL-API-1")
        self.assertEqual(result.tracking_number, "TRACK-1")
        self.assertEqual(result.label_cost, 3.25)

    def test_pirateship_api_mode_invalid_auth_scheme(self) -> None:
        def runtime_str(_repo, key, default):
            mapping = {
                "shipping_label_pirateship_mode": "api",
                "shipping_label_pirateship_base_url": "https://pirate.example",
                "shipping_label_pirateship_api_key": "secret",
                "shipping_label_pirateship_endpoint_path": "/v1/labels/purchase",
                "shipping_label_pirateship_auth_scheme": "weird",
            }
            return mapping.get(key, default)

        with patch("app.services.shipping_labels.get_runtime_str", side_effect=runtime_str), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=20
        ):
            with self.assertRaisesRegex(ValueError, "auth_scheme"):
                shipping_labels._pirateship_purchase_label(object(), payload={"sale_id": 1})

    def test_pirateship_api_mode_non_object_json_response(self) -> None:
        def runtime_str(_repo, key, default):
            mapping = {
                "shipping_label_pirateship_mode": "api",
                "shipping_label_pirateship_base_url": "https://pirate.example",
                "shipping_label_pirateship_api_key": "secret",
                "shipping_label_pirateship_endpoint_path": "/v1/labels/purchase",
                "shipping_label_pirateship_auth_scheme": "bearer",
            }
            return mapping.get(key, default)

        with patch("app.services.shipping_labels.get_runtime_str", side_effect=runtime_str), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=20
        ), patch("app.services.shipping_labels.requests.post", return_value=_FakeResponse(payload=["bad"])):
            with self.assertRaisesRegex(ValueError, "not a JSON object"):
                shipping_labels._pirateship_purchase_label(object(), payload={"sale_id": 1})

    def test_pirateship_api_mode_token_auth_scheme_and_timeout_clamp(self) -> None:
        calls = []

        def runtime_str(_repo, key, default):
            mapping = {
                "shipping_label_pirateship_mode": "api",
                "shipping_label_pirateship_base_url": "https://pirate.example",
                "shipping_label_pirateship_api_key": "secret-token",
                "shipping_label_pirateship_endpoint_path": "/v1/labels/purchase",
                "shipping_label_pirateship_auth_scheme": "token",
            }
            return mapping.get(key, default)

        def fake_post(url, headers=None, data=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}, "data": data, "timeout": timeout})
            return _FakeResponse(
                payload={"label_id": "L2", "label_url": "https://labels.example/L2.pdf", "tracking_number": "T2"},
            )

        with patch("app.services.shipping_labels.get_runtime_str", side_effect=runtime_str), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=1
        ), patch("app.services.shipping_labels.requests.post", side_effect=fake_post):
            result = shipping_labels._pirateship_purchase_label(
                object(), payload={"sale_id": 2, "shipping_provider": "pirateship", "tracking_number": "TRACK-EXIST"}
            )
        self.assertEqual(result.label_id, "L2")
        self.assertEqual(calls[0]["headers"].get("Authorization"), "secret-token")
        self.assertEqual(calls[0]["timeout"], 5)

    def test_pirateship_invalid_mode_raises(self) -> None:
        def runtime_str(_repo, key, default):
            mapping = {
                "shipping_label_pirateship_mode": "invalid",
            }
            return mapping.get(key, default)

        with patch("app.services.shipping_labels.get_runtime_str", side_effect=runtime_str), patch(
            "app.services.shipping_labels.get_runtime_int", return_value=20
        ):
            with self.assertRaisesRegex(ValueError, "expected `mock` or `api`"):
                shipping_labels._pirateship_purchase_label(object(), payload={"sale_id": 1})

    def test_purchase_shipping_label_provider_routing(self) -> None:
        with patch("app.services.shipping_labels._pirateship_purchase_label") as pirate:
            pirate.return_value = shipping_labels.ShippingLabelResult(label_id="x", label_url="u")
            result = shipping_labels.purchase_shipping_label(object(), provider="pirateship", payload={})
        self.assertEqual(result.label_id, "x")
        with self.assertRaisesRegex(ValueError, "not implemented"):
            shipping_labels.purchase_shipping_label(object(), provider="ebay_shipping", payload={})
        with self.assertRaisesRegex(ValueError, "Unsupported shipping label provider"):
            shipping_labels.purchase_shipping_label(object(), provider="foo", payload={})
        with self.assertRaisesRegex(ValueError, "not implemented"):
            shipping_labels.purchase_shipping_label(object(), provider="", payload={})


if __name__ == "__main__":
    unittest.main()
