import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.coin_reference_sources import (
    DisabledCoinSourceAdapter,
    GreysheetAdapter,
    PaidCoinSourceConfig,
    resolve_paid_coin_source_adapter,
    resolve_paid_coin_source_config,
)


class CoinReferenceSourcesTests(unittest.TestCase):
    def test_disabled_adapter(self) -> None:
        adapter = DisabledCoinSourceAdapter()
        self.assertEqual(adapter.provider, "none")
        self.assertTrue(adapter.validate())
        self.assertEqual(adapter.fetch_records(query="morgan"), [])

    def test_greysheet_validate_paths(self) -> None:
        cfg = PaidCoinSourceConfig(
            enabled=True,
            provider="greysheet",
            base_url="",
            api_key="",
            license_acknowledged=False,
            allow_prod=False,
        )
        adapter = GreysheetAdapter(cfg)
        issues = adapter.validate()
        self.assertTrue(any("License acknowledgment" in item for item in issues))
        self.assertTrue(any("Base URL is required" in item for item in issues))
        self.assertTrue(any("API key is required" in item for item in issues))

        good_cfg = PaidCoinSourceConfig(
            enabled=True,
            provider="greysheet",
            base_url="https://example.com",
            api_key="k",
            license_acknowledged=True,
            allow_prod=True,
        )
        with patch("app.services.coin_reference_sources.settings", SimpleNamespace(app_env="prod")):
            self.assertEqual(GreysheetAdapter(good_cfg).validate(), [])
            blocked = PaidCoinSourceConfig(**{**good_cfg.__dict__, "allow_prod": False})
            issues = GreysheetAdapter(blocked).validate()
            self.assertTrue(any("Production usage is blocked" in item for item in issues))

    def test_greysheet_fetch_records_not_implemented(self) -> None:
        cfg = PaidCoinSourceConfig(
            enabled=True,
            provider="greysheet",
            base_url="https://example.com",
            api_key="k",
            license_acknowledged=True,
            allow_prod=True,
        )
        with self.assertRaises(NotImplementedError):
            GreysheetAdapter(cfg).fetch_records(query="morgan")

    @patch("app.services.coin_reference_sources.get_runtime_bool")
    @patch("app.services.coin_reference_sources.get_runtime_str")
    def test_resolve_config_and_adapter(self, mock_get_runtime_str, mock_get_runtime_bool) -> None:
        str_map = {
            "coin_ref_paid_source_provider": "greysheet",
            "coin_ref_paid_source_base_url": "https://example.com",
            "coin_ref_paid_source_api_key": "k",
        }
        bool_map = {
            "coin_ref_paid_source_enabled": True,
            "coin_ref_paid_source_license_ack": True,
            "coin_ref_paid_source_allow_prod": False,
        }
        mock_get_runtime_str.side_effect = lambda _repo, key, default: str_map.get(key, default)
        mock_get_runtime_bool.side_effect = lambda _repo, key, default: bool_map.get(key, default)

        cfg = resolve_paid_coin_source_config(repo=object())
        self.assertEqual(cfg.provider, "greysheet")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.base_url, "https://example.com")

        adapter = resolve_paid_coin_source_adapter(repo=object())
        self.assertIsInstance(adapter, GreysheetAdapter)

        bool_map["coin_ref_paid_source_enabled"] = False
        adapter2 = resolve_paid_coin_source_adapter(repo=object())
        self.assertIsInstance(adapter2, DisabledCoinSourceAdapter)

        bool_map["coin_ref_paid_source_enabled"] = True
        str_map["coin_ref_paid_source_provider"] = "other"
        adapter3 = resolve_paid_coin_source_adapter(repo=object())
        self.assertIsInstance(adapter3, DisabledCoinSourceAdapter)


if __name__ == "__main__":
    unittest.main()
