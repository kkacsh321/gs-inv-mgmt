import unittest
from types import SimpleNamespace

from app.services import config_health


class ConfigHealthTests(unittest.TestCase):
    def test_required_key_sets_are_copied(self) -> None:
        env_keys = config_health.required_env_keys()
        runtime_keys = config_health.required_runtime_keys()
        self.assertIn("APP_ENV", env_keys)
        self.assertIn("ai_domain_chat_enabled", runtime_keys)
        self.assertIn("ai_workflow_profile_accounting", runtime_keys)
        self.assertIn("listing_wizard_recent_product_limit", runtime_keys)
        self.assertIn("notification_route_ai_accountant_monitor", runtime_keys)
        env_keys.add("X")
        runtime_keys.add("Y")
        self.assertNotIn("X", config_health.REQUIRED_ENV_KEYS)
        self.assertNotIn("Y", config_health.REQUIRED_RUNTIME_KEYS)

    def test_health_state_thresholds_and_clamping(self) -> None:
        self.assertEqual(config_health.health_state(1.0), "healthy")
        self.assertEqual(config_health.health_state(0.95), "healthy")
        self.assertEqual(config_health.health_state(0.9), "warning")
        self.assertEqual(config_health.health_state(0.8), "warning")
        self.assertEqual(config_health.health_state(0.79), "critical")
        self.assertEqual(config_health.health_state(-1), "critical")
        self.assertEqual(config_health.health_state(2), "healthy")

    def test_env_missing_or_empty(self) -> None:
        missing = config_health.env_missing_or_empty(
            {"A", "B", "C"},
            {"A": "x", "B": "   "},
        )
        self.assertEqual(missing, ["B", "C"])

    def test_runtime_missing_or_inactive(self) -> None:
        rows = [
            SimpleNamespace(key="a", is_active=True),
            SimpleNamespace(key="b", is_active=False),
        ]
        missing = config_health.runtime_missing_or_inactive({"a", "b", "c"}, rows)
        self.assertEqual(missing, ["b", "c"])


if __name__ == "__main__":
    unittest.main()
