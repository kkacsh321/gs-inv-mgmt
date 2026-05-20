import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _bootstrap_views_package() -> None:
    if "boto3" not in sys.modules:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = types.ModuleType("botocore")
    if "botocore.config" not in sys.modules:
        fake_botocore_config = types.ModuleType("botocore.config")
        fake_botocore_config.Config = lambda *args, **kwargs: None
        sys.modules["botocore.config"] = fake_botocore_config
    if "botocore.exceptions" not in sys.modules:
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
        fake_botocore_exceptions.BotoCoreError = Exception
        fake_botocore_exceptions.ClientError = Exception
        sys.modules["botocore.exceptions"] = fake_botocore_exceptions

    if "app.components.views" not in sys.modules:
        pkg = types.ModuleType("app.components.views")
        pkg.__path__ = []
        sys.modules["app.components.views"] = pkg

    root = Path(__file__).resolve().parents[1]
    for name in ("shared", "workspace_shell"):
        full = f"app.components.views.{name}"
        if full in sys.modules:
            continue
        path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        sys.modules[full] = mod


def _load_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "ai_chat.py"
    spec = importlib.util.spec_from_file_location("test_ai_chat_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


chat = _load_module()


class AiChatHelpersTests(unittest.TestCase):
    def test_normalize_contains_parse(self):
        self.assertEqual(chat._normalize("  A   B  "), "a b")
        self.assertTrue(chat._contains_any("hello world", ["world"]))
        self.assertEqual(chat._parse_csv_tokens("a, B\n c ,,"), {"a", "b", "c"})

    def test_allowed_domains_for_role(self):
        with patch.object(chat, "get_runtime_str", return_value="inventory, listings"):
            out = chat._allowed_domains_for_role(object(), "ops")
        self.assertEqual(out, {"inventory", "listings"})

        with patch.object(chat, "get_runtime_str", return_value=""):
            out_default = chat._allowed_domains_for_role(object(), "viewer")
        self.assertIn("inventory", out_default)

    def test_write_intent_and_mask_tail(self):
        self.assertTrue(chat._is_write_intent("Please publish this listing"))
        self.assertFalse(chat._is_write_intent("show me inventory"))
        self.assertEqual(chat._mask_tail("ABCDEFGH", keep=4), "****EFGH")
        self.assertEqual(chat._mask_tail("AB", keep=4), "**")

    def test_sensitive_masking(self):
        text = "email me at auser@example.com phone 303-555-1212 tracking: ZXCVBNM123456"

        def _rt_true(_repo, key, default=True):
            return True

        with patch.object(chat, "get_runtime_bool", side_effect=_rt_true):
            masked, rules = chat._apply_sensitive_masking(object(), text)
        self.assertIn("a***@example.com", masked)
        self.assertIn("***-***-1212", masked)
        self.assertIn("tracking", masked.lower())
        self.assertIn("email", rules)
        self.assertIn("phone", rules)
        self.assertIn("tracking", rules)

        with patch.object(chat, "get_runtime_bool", return_value=False):
            passthrough, rules2 = chat._apply_sensitive_masking(object(), text)
        self.assertEqual(passthrough, text)
        self.assertEqual(rules2, [])

    def test_ai_accountant_web_research_defaults_on_for_tax_questions(self):
        defaults = {}

        def _runtime_bool(_repo, key, default=True):
            defaults[str(key)] = default
            return default

        with patch.object(chat, "get_runtime_bool", side_effect=_runtime_bool):
            should_attach = chat._should_attach_ai_accountant_web_research(
                object(),
                "research Colorado bullion sales tax",
                is_ai_accountant_request=True,
            )

        self.assertTrue(should_attach)
        self.assertEqual(defaults["ai_accountant_web_research_enabled"], True)

        with patch.object(chat, "get_runtime_bool", return_value=False):
            disabled = chat._should_attach_ai_accountant_web_research(
                object(),
                "research Colorado bullion sales tax",
                is_ai_accountant_request=True,
            )
        self.assertFalse(disabled)

        with patch.object(chat, "get_runtime_bool", return_value=True):
            non_accountant = chat._should_attach_ai_accountant_web_research(
                object(),
                "research Colorado bullion sales tax",
                is_ai_accountant_request=False,
            )
        self.assertFalse(non_accountant)

    def test_answer_query_routes(self):
        allowed = {"inventory", "listings", "sales", "shipping", "sync", "orders", "reports", "admin"}
        with patch.object(chat, "build_inventory_snapshot", return_value=("inv", [{"s": 1}])), \
            patch.object(chat, "build_listings_snapshot", return_value=("lst", [])), \
            patch.object(chat, "build_sales_snapshot", return_value=("sales", [])), \
            patch.object(chat, "build_shipping_snapshot", return_value=("ship", [])), \
            patch.object(chat, "build_sync_snapshot", return_value=("sync", [])), \
            patch.object(chat, "build_orders_snapshot", return_value=("orders", [])), \
            patch.object(chat, "build_reports_snapshot", return_value=("reports", [])), \
            patch.object(chat, "build_accounting_snapshot", return_value=("accounting", [])), \
            patch.object(chat, "build_admin_snapshot", return_value=("admin", [])), \
            patch.object(chat, "build_fallback_help", return_value=("help", [])):
            self.assertEqual(chat._answer_query(object(), "inventory on hand", allowed_domains=allowed, max_scan_rows=10)[2], "inventory_snapshot")
            self.assertEqual(chat._answer_query(object(), "listing review", allowed_domains=allowed, max_scan_rows=10)[2], "listing_snapshot")
            self.assertEqual(chat._answer_query(object(), "sales last 30 days", allowed_domains=allowed, max_scan_rows=10)[2], "sales_snapshot_30d")
            self.assertEqual(chat._answer_query(object(), "accounting close profit", allowed_domains=allowed | {"accounting"}, max_scan_rows=10)[2], "accounting_snapshot")
            self.assertEqual(chat._answer_query(object(), "shipping exception", allowed_domains=allowed, max_scan_rows=10)[2], "shipping_snapshot")
            self.assertEqual(chat._answer_query(object(), "sync failed run", allowed_domains=allowed, max_scan_rows=10)[2], "sync_snapshot")
            self.assertEqual(chat._answer_query(object(), "order fulfillment", allowed_domains=allowed, max_scan_rows=10)[2], "order_snapshot")
            self.assertEqual(chat._answer_query(object(), "report trend", allowed_domains=allowed, max_scan_rows=10)[2], "reports_snapshot")
            self.assertEqual(chat._answer_query(object(), "admin users", allowed_domains=allowed, max_scan_rows=10)[2], "admin_snapshot")
            self.assertEqual(chat._answer_query(object(), "what can you do", allowed_domains=allowed, max_scan_rows=10)[2], "help_fallback")

        denied = {"inventory"}
        msg, _, route = chat._answer_query(object(), "listing review", allowed_domains=denied, max_scan_rows=10)
        self.assertEqual(route, "denied_listings")
        self.assertIn("not allowed", msg)

        msg, _, route = chat._answer_query(object(), "accounting close", allowed_domains=denied, max_scan_rows=10)
        self.assertEqual(route, "denied_accounting")
        self.assertIn("not allowed", msg)

    def test_goldy_helpers(self):
        domains = chat._resolve_goldy_domains({"inventory", "sync", "admin"}, "integrations_agent")
        self.assertEqual(domains, {"sync", "admin"})

        plan_single = chat._build_goldy_plan(
            prompt="help",
            mode="single",
            selected_agent="auto_router",
            allowed_domains={"inventory", "sales"},
        )
        self.assertEqual(plan_single["mode"], "single")
        self.assertTrue(plan_single["requires_approval_for_writes"])

        plan_multi = chat._build_goldy_plan(
            prompt="help",
            mode="multi",
            selected_agent="accountant_agent",
            allowed_domains={"reports"},
        )
        self.assertEqual(plan_multi["mode"], "multi")
        self.assertEqual(plan_multi["agent_role"], "accountant_agent")


if __name__ == "__main__":
    unittest.main()
