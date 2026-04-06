import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None
        return _noop


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.calls = []
        self._button_map = {}
        self._form_submit_map = {}
        self.query_params = {}
        self.last_text_input_value = None

    def set_button(self, label, value):
        self._button_map[label] = value

    def set_form_submit(self, label, value):
        self._form_submit_map[label] = value

    def subheader(self, *a, **k):
        self.calls.append(("subheader", a, k))

    def caption(self, *a, **k):
        self.calls.append(("caption", a, k))

    def markdown(self, *a, **k):
        self.calls.append(("markdown", a, k))

    def info(self, *a, **k):
        self.calls.append(("info", a, k))

    def warning(self, *a, **k):
        self.calls.append(("warning", a, k))

    def write(self, *a, **k):
        self.calls.append(("write", a, k))

    def link_button(self, *a, **k):
        self.calls.append(("link_button", a, k))

    def code(self, *a, **k):
        self.calls.append(("code", a, k))

    def text_input(self, _label, value="", **_kwargs):
        self.last_text_input_value = value
        return value or ""

    def text_area(self, _label, height=None, key=None, help=None):
        _ = (height, key, help)
        return str(self.session_state.get(key) or "")

    def button(self, label, **_kwargs):
        return bool(self._button_map.get(label, False))

    def success(self, *a, **k):
        self.calls.append(("success", a, k))

    def error(self, *a, **k):
        self.calls.append(("error", a, k))

    def json(self, *a, **k):
        self.calls.append(("json", a, k))

    def dataframe(self, *a, **k):
        self.calls.append(("dataframe", a, k))

    def metric(self, *a, **k):
        self.calls.append(("metric", a, k))

    def columns(self, n):
        return [_Ctx() for _ in range(int(n))]

    def form(self, _key):
        return _Ctx()

    def number_input(self, _label, min_value=None, max_value=None, value=0):
        _ = (min_value, max_value)
        return value

    def form_submit_button(self, label, **_kwargs):
        return bool(self._form_submit_map.get(label, False))

    def selectbox(self, _label, options, **_kwargs):
        return options[0]

    def rerun(self):
        self.calls.append(("rerun", (), {}))



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
    for name in ("shared", "ebay_context"):
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
    path = root / "app" / "components" / "views" / "ebay.py"
    spec = importlib.util.spec_from_file_location("test_ebay_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ebay_view = _load_module()


class EbayViewTests(unittest.TestCase):
    def test_render_ebay_not_configured(self):
        fake_st = _FakeSt()
        client = SimpleNamespace(is_configured=lambda: False)
        repo = SimpleNamespace(list_listings=lambda: [], list_sync_runs=lambda provider=None, limit=200: [])
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(ebay_view, "st", fake_st), \
            patch.object(ebay_view, "current_user", return_value=user), \
            patch.object(ebay_view, "is_sync_job_enabled", return_value=True), \
            patch.object(ebay_view, "render_help_panel", return_value=None), \
            patch.object(ebay_view, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_view, "get_runtime_str", return_value=""):
            ebay_view.render_ebay(client, repo)
        self.assertTrue(any(c[0] == "warning" for c in fake_st.calls))

    def test_render_ebay_pull_import_success(self):
        fake_st = _FakeSt()
        fake_st.session_state["ebay_verify_access_token"] = "token123"
        fake_st.session_state["ebay_pull_access_token"] = "token123"
        fake_st.set_form_submit("Pull + Import Orders", True)

        listing = SimpleNamespace(
            id=101,
            marketplace="ebay",
            listing_status="draft",
            external_listing_id="",
            listing_title="Title",
            product_id=1,
            listing_price=10.0,
            quantity_listed=1,
        )
        sync_run = SimpleNamespace(status="failed")

        client = SimpleNamespace(
            is_configured=lambda: True,
            authorize_url=lambda state="": "https://example.com/auth",
            get_account_privileges=lambda token: {"ok": True},
            exchange_code_for_tokens=lambda code: {"access_token": "x"},
        )
        repo = SimpleNamespace(
            list_listings=lambda: [listing],
            list_sync_runs=lambda provider=None, limit=200: [sync_run],
        )
        user = SimpleNamespace(username="admin", role="admin")

        with patch.object(ebay_view, "st", fake_st), \
            patch.object(ebay_view, "current_user", return_value=user), \
            patch.object(ebay_view, "is_sync_job_enabled", return_value=True), \
            patch.object(ebay_view, "ensure_permission", return_value=True), \
            patch.object(ebay_view, "render_help_panel", return_value=None), \
            patch.object(ebay_view, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_view, "get_runtime_str", return_value="token123"), \
            patch.object(ebay_view, "execute_sync_job", return_value={
                "status": "success",
                "processed": 1,
                "created": 1,
                "updated": 0,
                "failed": 0,
                "run_id": 77,
                "line_items_with_listing_link": 1,
                "line_items_unmapped_sku": 0,
                "auto_listings_created": 0,
            }) as run_job:
            ebay_view.render_ebay(client, repo)

        self.assertTrue(run_job.called)
        self.assertTrue(any(c[0] == "success" for c in fake_st.calls))
        self.assertTrue(any(c[0] == "dataframe" for c in fake_st.calls))

    def test_render_ebay_auto_exchanges_query_code(self):
        fake_st = _FakeSt()
        fake_st.query_params = {"code": "oauth-code-123", "state": "state-123"}
        fake_st.session_state["ebay_oauth_state"] = "state-123"

        client = SimpleNamespace(
            is_configured=lambda: True,
            authorize_url=lambda state="": "https://example.com/auth",
            exchange_code_for_tokens=lambda code: {
                "access_token": "acc-1",
                "refresh_token": "ref-1",
                "expires_in": 3600,
            },
            get_account_privileges=lambda token: {"ok": True},
        )
        repo = SimpleNamespace(
            list_listings=lambda: [],
            list_sync_runs=lambda provider=None, limit=200: [],
            upsert_runtime_setting=lambda **kwargs: None,
        )
        user = SimpleNamespace(username="admin", role="admin")

        with patch.object(ebay_view, "st", fake_st), \
            patch.object(ebay_view, "current_user", return_value=user), \
            patch.object(ebay_view, "is_sync_job_enabled", return_value=True), \
            patch.object(ebay_view, "render_help_panel", return_value=None), \
            patch.object(ebay_view, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_view, "get_runtime_str", return_value=""):
            ebay_view.render_ebay(client, repo)

        self.assertEqual(fake_st.session_state.get("ebay_workspace_access_token"), "acc-1")
        self.assertEqual(fake_st.session_state.get("ebay_verify_access_token"), "acc-1")
        self.assertEqual(fake_st.session_state.get("ebay_pull_access_token"), "acc-1")
        self.assertEqual(fake_st.query_params.get("code"), None)
        self.assertTrue(any(c[0] == "rerun" for c in fake_st.calls))

    def test_render_ebay_does_not_prefill_from_last_oauth_code_when_query_missing(self):
        fake_st = _FakeSt()
        fake_st.session_state["ebay_oauth_last_code"] = "cached-oauth-code"

        client = SimpleNamespace(
            is_configured=lambda: True,
            authorize_url=lambda state="": "https://example.com/auth",
            exchange_code_for_tokens=lambda code: {"access_token": "acc-1"},
            get_account_privileges=lambda token: {"ok": True},
        )
        repo = SimpleNamespace(
            list_listings=lambda: [],
            list_sync_runs=lambda provider=None, limit=200: [],
            upsert_runtime_setting=lambda **kwargs: None,
        )
        user = SimpleNamespace(username="admin", role="admin")

        with patch.object(ebay_view, "st", fake_st), \
            patch.object(ebay_view, "current_user", return_value=user), \
            patch.object(ebay_view, "is_sync_job_enabled", return_value=True), \
            patch.object(ebay_view, "render_help_panel", return_value=None), \
            patch.object(ebay_view, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_view, "get_runtime_str", return_value=""):
            ebay_view.render_ebay(client, repo)

        self.assertEqual(fake_st.last_text_input_value, "")


if __name__ == "__main__":
    unittest.main()
