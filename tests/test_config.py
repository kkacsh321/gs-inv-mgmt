import importlib
import os
import unittest
from unittest.mock import patch


class ConfigSettingsTests(unittest.TestCase):
    def _load_settings(self):
        import app.config as config

        importlib.reload(config)
        return config.settings

    def test_ebay_auth_callback_defaults_for_production(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "EBAY_AUTH_ACCEPTED_URL": "",
                "EBAY_AUTH_DECLINED_URL": "",
            },
            clear=False,
        ):
            settings = self._load_settings()
            self.assertEqual(
                settings.ebay_auth_accepted_url_effective,
                "https://inventory.goldenstackers.com/eBay_Workspace",
            )
            self.assertEqual(
                settings.ebay_auth_declined_url_effective,
                "https://inventory.goldenstackers.com/eBay_Workspace",
            )

    def test_ebay_auth_callback_defaults_for_dev(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "dev",
                "EBAY_AUTH_ACCEPTED_URL": "",
                "EBAY_AUTH_DECLINED_URL": "",
            },
            clear=False,
        ):
            settings = self._load_settings()
            self.assertEqual(
                settings.ebay_auth_accepted_url_effective,
                "https://dev-inventory.goldenstackers.com/eBay_Workspace",
            )
            self.assertEqual(
                settings.ebay_auth_declined_url_effective,
                "https://dev-inventory.goldenstackers.com/eBay_Workspace",
            )

    def test_ebay_auth_callback_explicit_values_override_defaults(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "EBAY_AUTH_ACCEPTED_URL": "https://custom.example.com/ok",
                "EBAY_AUTH_DECLINED_URL": "https://custom.example.com/no",
            },
            clear=False,
        ):
            settings = self._load_settings()
            self.assertEqual(settings.ebay_auth_accepted_url_effective, "https://custom.example.com/ok")
            self.assertEqual(settings.ebay_auth_declined_url_effective, "https://custom.example.com/no")


if __name__ == "__main__":
    unittest.main()
