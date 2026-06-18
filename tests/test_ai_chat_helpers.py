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
ROOT = Path(__file__).resolve().parents[1]


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

        self.assertIn("customers", chat.DEFAULT_CHAT_ALLOWED_DOMAINS_BY_ROLE["ops"])
        self.assertIn("customers", chat.DEFAULT_CHAT_ALLOWED_DOMAINS_BY_ROLE["admin"])
        self.assertNotIn("customers", chat.DEFAULT_CHAT_ALLOWED_DOMAINS_BY_ROLE["viewer"])

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
        allowed = {"inventory", "listings", "sales", "shipping", "sync", "orders", "customers", "reports", "admin"}
        with patch.object(chat, "build_inventory_snapshot", return_value=("inv", [{"s": 1}])), \
            patch.object(chat, "build_listings_snapshot", return_value=("lst", [])), \
            patch.object(chat, "build_sales_snapshot", return_value=("sales", [])), \
            patch.object(chat, "build_shipping_snapshot", return_value=("ship", [])), \
            patch.object(chat, "build_sync_snapshot", return_value=("sync", [])), \
            patch.object(chat, "build_orders_snapshot", return_value=("orders", [])), \
            patch.object(chat, "build_customers_snapshot", return_value=("customers", [])), \
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
            self.assertEqual(chat._answer_query(object(), "repeat buyer customer notes", allowed_domains=allowed, max_scan_rows=10)[2], "customers_snapshot")
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

        msg, _, route = chat._answer_query(object(), "customer notes", allowed_domains=denied, max_scan_rows=10)
        self.assertEqual(route, "denied_customers")
        self.assertIn("not allowed", msg)

    def test_goldy_helpers(self):
        domains = chat._resolve_goldy_domains({"inventory", "sync", "admin"}, "integrations_agent")
        self.assertEqual(domains, {"sync", "admin"})

        kurt_domains = chat._resolve_goldy_domains(
            {"inventory", "listings", "accounting"},
            "kurt_intake_agent",
        )
        self.assertEqual(kurt_domains, {"inventory", "listings"})

        murdock_domains = chat._resolve_goldy_domains(
            {"inventory", "listings", "sales", "accounting"},
            "murdock_listing_agent",
        )
        self.assertEqual(murdock_domains, {"inventory", "listings", "sales"})

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
        self.assertEqual(plan_multi["coordinator"], "Goldy")
        self.assertEqual(plan_multi["agent_role"], "accountant_agent")
        self.assertEqual(plan_multi["agent_label"], "Goldie (AI Accountant)")
        self.assertIn("routes accounting/tax work to Goldie", plan_multi["specialist_relationship"])
        self.assertTrue(any("routes accounting/tax work to Goldie" in step for step in plan_multi["steps"]))

        plan_kurt = chat._build_goldy_plan(
            prompt="intake this coin from photos",
            mode="single",
            selected_agent="kurt_intake_agent",
            allowed_domains={"inventory", "listings"},
        )
        self.assertEqual(plan_kurt["agent_label"], "Kurt (Inventory Intake)")
        self.assertIn("routes inventory intake", plan_kurt["specialist_relationship"])

        plan_murdock = chat._build_goldy_plan(
            prompt="write an ebay listing",
            mode="single",
            selected_agent="murdock_listing_agent",
            allowed_domains={"inventory", "listings"},
        )
        self.assertEqual(plan_murdock["agent_label"], "Murdock (Listing + Sales Copy)")
        self.assertIn("routes listing drafts", plan_murdock["specialist_relationship"])

    def test_goldie_is_accounting_intent(self):
        self.assertTrue(chat._is_ai_accountant_request("Goldie, why did profit drop?", "auto_router"))

    def test_auto_router_preserves_accounting_for_admin_scope(self):
        domains = chat._resolve_goldy_domains(
            {"admin", "inventory", "reports", "accounting", "tax"},
            "auto_router",
        )
        self.assertIn("accounting", domains)
        self.assertIn("tax", domains)

    def test_goldy_write_action_request_queues_blocked_approval(self):
        class Repo:
            def __init__(self):
                self.created = []
                self.updated = []

            def create_integration_queue_job(self, **kwargs):
                self.created.append(kwargs)
                return types.SimpleNamespace(id=42)

            def update_integration_queue_job(self, job_id, updates, actor):
                self.updated.append({"job_id": job_id, "updates": updates, "actor": actor})

        repo = Repo()
        result = chat._queue_goldy_action_approval_request(
            repo,
            prompt="publish listing 123",
            actor="ops1",
            role="ops",
            env_key="local",
            goldy_plan={"mode": "single", "agent_role": "listings_agent"},
        )

        self.assertEqual(result["queue_job_id"], 42)
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(repo.created[0]["integration"], "goldy")
        self.assertEqual(repo.created[0]["action"], "write_action_request")
        self.assertEqual(repo.created[0]["max_retries"], 0)
        self.assertEqual(repo.updated[0]["updates"]["status"], "blocked")

    def test_mirror_ask_turn_to_business_room_uses_selected_agent(self):
        with patch.object(chat, "record_business_room_turn", return_value=[{"id": 1}]) as mirror:
            mirrored = chat._mirror_ask_turn_to_business_room(
                object(),
                enabled=True,
                prompt="Murdock, draft a listing",
                answer="Draft plan",
                user_name="keith",
                user_role="admin",
                env_key="local",
                selected_agent="murdock_listing_agent",
                agent_label="Murdock (Listing + Sales Copy)",
                intent_key="listing_snapshot",
                elapsed_ms=123,
                metadata={"goldy_mode": "single"},
            )

        self.assertTrue(mirrored)
        kwargs = mirror.call_args.kwargs
        self.assertEqual(kwargs["agent_key"], "murdock_listing_agent")
        self.assertEqual(kwargs["agent_label"], "Murdock (Listing + Sales Copy)")
        self.assertEqual(kwargs["user_key"], "keith")
        self.assertEqual(kwargs["metadata"]["intent"], "listing_snapshot")
        self.assertEqual(kwargs["metadata"]["elapsed_ms"], 123)

    def test_mirror_ask_turn_to_business_room_can_be_disabled(self):
        with patch.object(chat, "record_business_room_turn") as mirror:
            mirrored = chat._mirror_ask_turn_to_business_room(
                object(),
                enabled=False,
                prompt="hello",
                answer="hi",
                user_name="keith",
                user_role="admin",
                env_key="local",
                selected_agent="auto_router",
                agent_label="Auto Router",
                intent_key="help_fallback",
                elapsed_ms=0,
            )

        self.assertFalse(mirrored)
        mirror.assert_not_called()

    def test_business_chat_room_lives_on_dedicated_page_not_nested_in_ask_expander(self):
        source = (ROOT / "app" / "components" / "views" / "ai_chat.py").read_text()

        self.assertIn('st.page_link("pages/19_Business_Chat_Room.py"', source)
        self.assertNotIn('with st.expander("Business Chat Room Roster"', source)
        self.assertNotIn('with st.expander("Business Chat Room"', source)


if __name__ == "__main__":
    unittest.main()
