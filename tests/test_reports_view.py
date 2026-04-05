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
        self._button_key_map = {}
        self.rerun_called = False

    def set_button_key_value(self, key: str, value: bool):
        self._button_key_map[key] = bool(value)

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

    def success(self, *a, **k):
        self.calls.append(("success", a, k))

    def error(self, *a, **k):
        self.calls.append(("error", a, k))

    def dataframe(self, *a, **k):
        self.calls.append(("dataframe", a, k))

    def download_button(self, *a, **k):
        self.calls.append(("download_button", a, k))

    def code(self, *a, **k):
        self.calls.append(("code", a, k))

    def metric(self, *a, **k):
        self.calls.append(("metric", a, k))

    def line_chart(self, *a, **k):
        self.calls.append(("line_chart", a, k))

    def columns(self, n):
        count = int(n) if isinstance(n, int) else len(n)
        return [_Ctx() for _ in range(count)]

    def expander(self, *_a, **_k):
        return _Ctx()

    def date_input(self, _label, value=None, **_kwargs):
        return value

    def text_input(self, _label, value="", **_kwargs):
        return value

    def number_input(self, _label, min_value=None, value=0.0, step=None, **_kwargs):
        _ = (min_value, step)
        return value

    def checkbox(self, _label, value=False, **_kwargs):
        return bool(value)

    def selectbox(self, _label, options, index=0, **_kwargs):
        opts = list(options)
        if not opts:
            return None
        idx = max(0, min(int(index), len(opts) - 1))
        return opts[idx]

    def multiselect(self, _label, options, default=None, **_kwargs):
        if default is not None:
            return list(default)
        return list(options)

    def button(self, _label, **kwargs):
        key = kwargs.get("key")
        if key in self._button_key_map:
            return bool(self._button_key_map[key])
        return False

    def rerun(self):
        self.rerun_called = True

    def stop(self):
        raise RuntimeError("streamlit_stop")


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
    path = root / "app" / "components" / "views" / "reports.py"
    spec = importlib.util.spec_from_file_location("test_reports_view_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


reports_view = _load_module()


class ReportsViewTests(unittest.TestCase):
    def _repo_stub(self):
        return SimpleNamespace(
            list_products=lambda: [],
            list_listings=lambda: [],
            list_sales=lambda: [],
            list_orders=lambda: [],
            list_order_items=lambda: [],
            list_returns=lambda: [],
            list_product_lot_assignments=lambda: [],
            list_inventory_movements=lambda limit=5000: [],
        )

    def test_render_reports_empty(self):
        fake_st = _FakeSt()
        repo = self._repo_stub()
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(reports_view, "st", fake_st), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ):
            reports_view.render_reports(repo)
        self.assertTrue(any(c[0] == "info" for c in fake_st.calls))

    def test_render_reports_copilot_permission_denied_and_success(self):
        repo = self._repo_stub()
        user = SimpleNamespace(username="admin", role="admin")

        # Permission denied branch calls st.stop().
        fake_st_denied = _FakeSt()
        fake_st_denied.set_button_key_value("reports_copilot_analyze_btn", True)
        with patch.object(reports_view, "st", fake_st_denied), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "ensure_permission", return_value=False
        ):
            with self.assertRaises(RuntimeError):
                reports_view.render_reports(repo)

        # Success branch sets result and reruns.
        fake_st_ok = _FakeSt()
        fake_st_ok.set_button_key_value("reports_copilot_analyze_btn", True)
        with patch.object(reports_view, "st", fake_st_ok), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "ensure_permission", return_value=True
        ), patch.object(
            reports_view, "execute_comp_summary", return_value=SimpleNamespace(text='{"ok":true}')
        ):
            reports_view.render_reports(repo)
        self.assertIn("reports_copilot_raw", fake_st_ok.session_state)
        self.assertTrue(fake_st_ok.rerun_called)


if __name__ == "__main__":
    unittest.main()

