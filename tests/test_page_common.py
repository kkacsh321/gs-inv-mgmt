import base64
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


fake_auth = types.ModuleType("app.auth")
fake_auth.current_user = lambda: None
fake_auth.init_user_context_sidebar = lambda: SimpleNamespace(username="admin", role="admin")
fake_auth.require_authenticated_session = (
    lambda allow_bootstrap_if_no_users=False, allow_oauth_callback_query=False: True
)
fake_auth.has_oauth_callback_query_params = lambda: False

fake_config = types.ModuleType("app.config")
fake_config.settings = SimpleNamespace(app_env="local", app_name="GoldenStackers")

fake_init_db = types.ModuleType("app.db.init_db")
fake_init_db.init_db = lambda: None

fake_session = types.ModuleType("app.db.session")
fake_session.SessionLocal = lambda: MagicMock()

fake_repo = types.ModuleType("app.repository")
class _FakeRepoClass:
    def __init__(self, db):
        self.db = db

    def get_runtime_setting(self, environment, key, active_only=True):
        return None

    def record_audit_event(self, **kwargs):
        return None

fake_repo.InventoryRepository = _FakeRepoClass

fake_media = types.ModuleType("app.services.media_storage")
class _FakeStorage:
    def __init__(self):
        self.enabled = False

    def ensure_bucket(self):
        return None

fake_media.MediaStorageService = _FakeStorage

with patch.dict(
    sys.modules,
    {
        "app.auth": fake_auth,
        "app.config": fake_config,
        "app.db.init_db": fake_init_db,
        "app.db.session": fake_session,
        "app.repository": fake_repo,
        "app.services.media_storage": fake_media,
    },
):
    import app.page_common as page_common
    importlib.reload(page_common)


class _FakeRow:
    def __init__(self, value):
        self.value = value


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class PageCommonTests(unittest.TestCase):
    def test_normalize_quick_action_alias_and_prefixes(self):
        self.assertEqual(page_common._normalize_quick_action("/p"), "products")
        self.assertEqual(page_common._normalize_quick_action("go reports"), "reports")
        self.assertEqual(page_common._normalize_quick_action(" SY "), "sync")
        self.assertEqual(page_common._normalize_quick_action("acct"), "ai-accountant")

    def test_logo_data_url(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            p.write_bytes(b"abc")
            got = page_common._logo_data_url(p)
        self.assertEqual(got, "data:image/jpeg;base64," + base64.b64encode(b"abc").decode("ascii"))

    @patch.object(page_common, "st")
    def test_inject_sidebar_top_logo_calls_markdown(self, st_mock):
        st_mock.markdown = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            p.write_bytes(b"logo")
            page_common._inject_sidebar_top_logo(p)
        st_mock.markdown.assert_called_once()

    @patch.object(page_common, "st")
    @patch.object(page_common, "time")
    def test_runtime_ui_flags_uses_cache(self, time_mock, st_mock):
        time_mock.time.return_value = 100.0
        st_mock.session_state = {
            "ux_runtime_ui_flags_cache": {"expires_at": 105.0, "flags": {"navigation_mode": "legacy"}}
        }
        flags = page_common._runtime_ui_flags()
        self.assertEqual(flags["navigation_mode"], "legacy")

    @patch.object(page_common, "settings", SimpleNamespace(app_env="local", app_name="GS"))
    @patch.object(page_common, "st")
    @patch.object(page_common, "time")
    @patch.object(page_common, "InventoryRepository")
    @patch.object(page_common, "SessionLocal")
    @patch.object(page_common, "init_db")
    def test_runtime_ui_flags_db_path(self, init_db_mock, session_local_mock, repo_cls_mock, time_mock, st_mock):
        time_mock.time.return_value = 200.0
        st_mock.session_state = {}

        db = MagicMock()
        session_local_mock.return_value = db

        values = {
            "ux_navigation_mode": _FakeRow("legacy"),
            "ux_navigation_telemetry_enabled": _FakeRow("false"),
            "ux_role_default_landing_enabled": _FakeRow("true"),
            "ux_workspace_ebay_enabled": _FakeRow("false"),
            "ux_workspace_inventory_enabled": _FakeRow("true"),
            "ux_workspace_fulfillment_enabled": _FakeRow("false"),
            "ux_workspace_sync_enabled": _FakeRow("true"),
            "ux_workspace_revenue_enabled": _FakeRow("false"),
        }

        repo = MagicMock()
        repo.get_runtime_setting.side_effect = (
            lambda environment, key, active_only=True: values.get(key)
        )
        repo_cls_mock.return_value = repo

        flags = page_common._runtime_ui_flags()

        self.assertEqual(flags["navigation_mode"], "legacy")
        self.assertFalse(flags["nav_telemetry_enabled"])
        self.assertTrue(flags["role_default_landing_enabled"])
        self.assertFalse(flags["workspace_ebay_enabled"])
        self.assertTrue(flags["workspace_inventory_enabled"])
        self.assertFalse(flags["workspace_fulfillment_enabled"])
        self.assertTrue(flags["workspace_sync_enabled"])
        self.assertFalse(flags["workspace_revenue_enabled"])
        self.assertIn("ux_runtime_ui_flags_cache", st_mock.session_state)
        db.close.assert_called_once()
        init_db_mock.assert_called_once()

    @patch.object(page_common, "st")
    @patch.object(page_common, "init_db", side_effect=Exception("boom"))
    def test_runtime_ui_flags_fallback_on_error(self, _init_db_mock, st_mock):
        st_mock.session_state = {}
        flags = page_common._runtime_ui_flags()
        self.assertEqual(flags["navigation_mode"], "unified")
        self.assertTrue(flags["workspace_ebay_enabled"])

    @patch.object(page_common, "SessionLocal")
    @patch.object(page_common, "InventoryRepository")
    @patch.object(page_common, "init_db")
    def test_record_navigation_event(self, init_db_mock, repo_cls_mock, session_local_mock):
        db = MagicMock()
        session_local_mock.return_value = db
        repo = MagicMock()
        repo_cls_mock.return_value = repo

        page_common._record_navigation_event(actor="u", action="page_view", payload={"a": 1})

        init_db_mock.assert_called_once()
        repo.record_audit_event.assert_called_once()
        db.close.assert_called_once()

    @patch.object(page_common, "init_db", side_effect=Exception("boom"))
    def test_record_navigation_event_swallow_errors(self, _init_db_mock):
        page_common._record_navigation_event(actor="u", action="page_view", payload={})

    @patch.object(page_common, "st")
    @patch.object(page_common, "_runtime_ui_flags", return_value={"nav_telemetry_enabled": False})
    @patch.object(page_common, "_record_navigation_event")
    def test_capture_navigation_telemetry_disabled(self, record_mock, _flags_mock, st_mock):
        st_mock.session_state = {}
        page_common._capture_navigation_telemetry(username="u", role="ops", page_title="Products")
        record_mock.assert_not_called()

    @patch.object(page_common, "st")
    @patch.object(page_common, "time")
    @patch.object(page_common, "_runtime_ui_flags", return_value={"nav_telemetry_enabled": True, "navigation_mode": "unified"})
    @patch.object(page_common, "_record_navigation_event")
    def test_capture_navigation_telemetry_records_view_switch_and_bounce(
        self, record_mock, _flags_mock, time_mock, st_mock
    ):
        st_mock.session_state = {
            "ux_nav_last_page": "dashboard",
            "ux_nav_last_ts": 100.0,
        }
        time_mock.time.return_value = 105.0

        page_common._capture_navigation_telemetry(username="u", role="ops", page_title="Products")

        self.assertEqual(record_mock.call_count, 2)
        self.assertEqual(st_mock.session_state["ux_nav_switch_count"], 1)
        self.assertEqual(st_mock.session_state["ux_nav_bounce_count"], 1)
        self.assertEqual(st_mock.session_state["ux_nav_last_page"], "products")

    @patch.object(page_common, "st")
    @patch.object(page_common, "settings", SimpleNamespace(app_env="local", app_name="GoldenStackers"))
    @patch.object(page_common, "_capture_navigation_telemetry")
    @patch.object(page_common, "_render_quick_actions_sidebar")
    @patch.object(page_common, "_runtime_ui_flags", return_value={"navigation_mode": "unified", "role_default_landing_enabled": True})
    @patch.object(page_common, "require_authenticated_session", return_value=True)
    @patch.object(page_common, "init_user_context_sidebar", return_value=SimpleNamespace(username="admin", role="admin"))
    def test_setup_page_happy_path(
        self,
        _init_user_mock,
        _require_mock,
        _flags_mock,
        render_quick_actions_mock,
        capture_telemetry_mock,
        st_mock,
    ):
        st_mock.session_state = {}
        st_mock.sidebar = MagicMock()
        with patch.object(page_common, "SIDEBAR_LOGO_PATH", Path("/nonexistent")):
            page_common.setup_page("Dashboard")

        st_mock.set_page_config.assert_called_once()
        render_quick_actions_mock.assert_called_once_with("admin", nav_mode="unified")
        capture_telemetry_mock.assert_called_once()
        st_mock.title.assert_called_once_with("GoldenStackers")

    @patch.object(page_common, "st")
    @patch.object(page_common, "require_authenticated_session", return_value=False)
    @patch.object(page_common, "init_user_context_sidebar", return_value=SimpleNamespace(username="admin", role="admin"))
    def test_setup_page_unauthenticated_stops(self, _init_user_mock, _require_mock, st_mock):
        st_mock.session_state = {}
        st_mock.stop = MagicMock(side_effect=RuntimeError("stop"))
        with self.assertRaises(RuntimeError):
            page_common.setup_page("Dashboard")

    @patch.object(page_common, "st")
    def test_render_quick_actions_unknown_command(self, st_mock):
        st_mock.sidebar = MagicMock()
        st_mock.sidebar.expander.return_value = _Ctx()
        st_mock.form.return_value = _Ctx()
        st_mock.text_input.return_value = "/does-not-exist"
        st_mock.form_submit_button.return_value = True

        page_common._render_quick_actions_sidebar("viewer", nav_mode="legacy")

        st_mock.error.assert_called_with("Unknown quick action: `/does-not-exist`")

    @patch.object(page_common, "st")
    def test_render_quick_actions_navigation_failure(self, st_mock):
        st_mock.sidebar = MagicMock()
        st_mock.sidebar.expander.return_value = _Ctx()
        st_mock.form.return_value = _Ctx()
        st_mock.text_input.return_value = "/products"
        st_mock.form_submit_button.return_value = True
        st_mock.switch_page.side_effect = Exception("nav failed")

        page_common._render_quick_actions_sidebar("viewer", nav_mode="legacy")

        st_mock.error.assert_called_with("Navigation failed for `products`: nav failed")

    @patch.object(page_common, "_runtime_ui_flags")
    @patch.object(page_common, "st")
    def test_render_quick_actions_unified_pinned_navigation_failure(self, st_mock, flags_mock):
        flags_mock.return_value = {
            "workspace_ebay_enabled": True,
            "workspace_inventory_enabled": True,
            "workspace_fulfillment_enabled": True,
            "workspace_sync_enabled": True,
            "workspace_revenue_enabled": True,
        }
        st_mock.sidebar = MagicMock()
        st_mock.sidebar.expander.return_value = _Ctx()
        st_mock.form.return_value = _Ctx()
        st_mock.columns.return_value = [_Ctx(), _Ctx()]
        st_mock.form_submit_button.return_value = False
        st_mock.text_input.return_value = ""

        def _button_effect(*args, **kwargs):
            return kwargs.get("key") == "pinned_page_admin_0"

        st_mock.button.side_effect = _button_effect
        st_mock.switch_page.side_effect = Exception("boom")

        page_common._render_quick_actions_sidebar("admin", nav_mode="unified")

        st_mock.error.assert_any_call("Navigation failed for `Operations Home`: boom")

    @patch.object(page_common, "_runtime_ui_flags")
    @patch.object(page_common, "st")
    def test_render_quick_actions_unified_role_default_navigation_failure(self, st_mock, flags_mock):
        flags_mock.return_value = {
            "workspace_ebay_enabled": True,
            "workspace_inventory_enabled": True,
            "workspace_fulfillment_enabled": True,
            "workspace_sync_enabled": True,
            "workspace_revenue_enabled": True,
        }
        st_mock.sidebar = MagicMock()
        st_mock.sidebar.expander.return_value = _Ctx()
        st_mock.form.return_value = _Ctx()
        st_mock.columns.return_value = [_Ctx(), _Ctx()]
        st_mock.form_submit_button.return_value = False
        st_mock.text_input.return_value = ""

        def _button_effect(*args, **kwargs):
            return kwargs.get("key") == "pinned_role_default_admin"

        st_mock.button.side_effect = _button_effect
        st_mock.switch_page.side_effect = Exception("boom-default")

        page_common._render_quick_actions_sidebar("admin", nav_mode="unified")

        st_mock.error.assert_any_call("Navigation failed for role default: boom-default")

    @patch.object(page_common, "_runtime_ui_flags")
    @patch.object(page_common, "st")
    def test_render_quick_actions_unified_workflow_navigation_failure(self, st_mock, flags_mock):
        flags_mock.return_value = {
            "workspace_ebay_enabled": True,
            "workspace_inventory_enabled": True,
            "workspace_fulfillment_enabled": True,
            "workspace_sync_enabled": True,
            "workspace_revenue_enabled": True,
        }
        st_mock.sidebar = MagicMock()
        st_mock.sidebar.expander.return_value = _Ctx()
        st_mock.form.return_value = _Ctx()
        st_mock.columns.return_value = [_Ctx(), _Ctx()]
        st_mock.form_submit_button.return_value = False
        st_mock.text_input.return_value = ""

        def _button_effect(*args, **kwargs):
            return kwargs.get("key") == "workflow_stage_admin_0_0"

        st_mock.button.side_effect = _button_effect
        st_mock.switch_page.side_effect = Exception("boom-workflow")

        page_common._render_quick_actions_sidebar("admin", nav_mode="unified")

        st_mock.error.assert_any_call("Navigation failed for `Inventory Intake Wizard`: boom-workflow")

    @patch.object(page_common, "_runtime_ui_flags")
    @patch.object(page_common, "st")
    def test_render_quick_actions_unified_filters_all_gated_stage_links(self, st_mock, flags_mock):
        flags_mock.return_value = {
            "workspace_ebay_enabled": False,
            "workspace_inventory_enabled": False,
            "workspace_fulfillment_enabled": False,
            "workspace_sync_enabled": False,
            "workspace_revenue_enabled": False,
        }
        st_mock.sidebar = MagicMock()
        st_mock.sidebar.expander.return_value = _Ctx()
        st_mock.form.return_value = _Ctx()
        st_mock.form_submit_button.return_value = False
        st_mock.text_input.return_value = ""
        st_mock.button.return_value = False

        page_common._render_quick_actions_sidebar("ops", nav_mode="unified")

        st_mock.columns.assert_not_called()

    @patch.object(page_common, "SessionLocal")
    @patch.object(page_common, "InventoryRepository")
    @patch.object(page_common, "init_db")
    def test_repo_context(self, init_db_mock, repo_cls_mock, session_local_mock):
        db = MagicMock()
        session_local_mock.return_value = db
        repo = MagicMock()
        repo_cls_mock.return_value = repo

        with page_common.repo_context() as got:
            self.assertIs(got, repo)

        init_db_mock.assert_called_once()
        db.close.assert_called_once()

    @patch.object(page_common, "st")
    @patch.object(page_common, "MediaStorageService")
    def test_build_storage_enabled_success(self, storage_cls_mock, st_mock):
        storage = MagicMock()
        storage.enabled = True
        storage_cls_mock.return_value = storage

        got = page_common.build_storage()

        self.assertIs(got, storage)
        storage.ensure_bucket.assert_called_once()
        st_mock.sidebar.error.assert_not_called()

    @patch.object(page_common, "st")
    @patch.object(page_common, "MediaStorageService")
    def test_build_storage_enabled_failure(self, storage_cls_mock, st_mock):
        storage = MagicMock()
        storage.enabled = True
        storage.ensure_bucket.side_effect = Exception("s3")
        storage_cls_mock.return_value = storage

        page_common.build_storage()

        st_mock.sidebar.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
