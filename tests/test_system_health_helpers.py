import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import mock_open, patch


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
    shared_name = "app.components.views.shared"
    if shared_name not in sys.modules:
        shared_path = root / "app" / "components" / "views" / "shared.py"
        shared_spec = importlib.util.spec_from_file_location(shared_name, shared_path)
        shared_mod = importlib.util.module_from_spec(shared_spec)
        assert shared_spec and shared_spec.loader
        shared_spec.loader.exec_module(shared_mod)
        sys.modules[shared_name] = shared_mod


def _load_system_health_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "system_health.py"
    spec = importlib.util.spec_from_file_location("test_system_health_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


system_health = _load_system_health_module()


class SystemHealthHelpersTests(unittest.TestCase):
    def test_format_helpers_and_status_row(self):
        self.assertEqual(system_health._fmt_gb_from_kb(None), "n/a")
        self.assertIn("GB", system_health._fmt_gb_from_kb(1024 * 1024))
        row = system_health._status_row("DB", "ok", "healthy")
        self.assertEqual(row["component"], "DB")
        self.assertEqual(row["status"], "ok")

    def test_read_proc_meminfo(self):
        content = "MemTotal:       1000000 kB\nMemAvailable:   400000 kB\n"
        with patch("builtins.open", mock_open(read_data=content)):
            total, avail = system_health._read_proc_meminfo()
        self.assertEqual(total, 1000000)
        self.assertEqual(avail, 400000)

        with patch("builtins.open", side_effect=OSError("no proc")):
            total, avail = system_health._read_proc_meminfo()
        self.assertIsNone(total)
        self.assertIsNone(avail)

    def test_read_proc_rss_kb(self):
        content = "Name:\tpython\nVmRSS:\t   12345 kB\n"
        with patch("builtins.open", mock_open(read_data=content)):
            self.assertEqual(system_health._read_proc_rss_kb(), 12345)

        with patch("builtins.open", side_effect=OSError("no proc")):
            self.assertIsNone(system_health._read_proc_rss_kb())

    def test_render_system_health_permission_denied(self):
        class _FakeSt:
            def subheader(self, *_a, **_k):
                return None

            def stop(self):
                raise RuntimeError("st.stop")

        fake_st = _FakeSt()
        with patch.object(system_health, "st", fake_st), patch.object(
            system_health, "current_user", return_value=types.SimpleNamespace(username="u", role="viewer")
        ), patch.object(
            system_health, "render_help_panel"
        ), patch.object(
            system_health, "ensure_permission", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "st.stop"):
                system_health.render_system_health(types.SimpleNamespace())

    def test_render_system_health_refresh_rerun(self):
        class _FakeSt:
            def subheader(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def button(self, label, key=None):
                return str(label) == "Refresh Health Snapshot"

            def rerun(self):
                raise RuntimeError("st.rerun")

        fake_st = _FakeSt()
        with patch.object(system_health, "st", fake_st), patch.object(
            system_health, "current_user", return_value=types.SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            system_health, "render_help_panel"
        ), patch.object(
            system_health, "ensure_permission", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "st.rerun"):
                system_health.render_system_health(types.SimpleNamespace())

    def test_render_system_health_full_smoke(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def metric(self, *_a, **_k):
                return None

        class _FakeSt:
            def subheader(self, *_a, **_k):
                return None

            def stop(self):
                raise RuntimeError("st.stop")

            def caption(self, *_a, **_k):
                return None

            def button(self, *_a, **_k):
                return False

            def rerun(self):
                return None

            def markdown(self, *_a, **_k):
                return None

            def columns(self, n):
                count = len(n) if isinstance(n, (list, tuple)) else int(n)
                return [_Ctx() for _ in range(count)]

            def dataframe(self, *_a, **_k):
                return None

            def link_button(self, *_a, **_k):
                return None

            def form(self, *_a, **_k):
                return _Ctx()

            def form_submit_button(self, *_a, **_k):
                return False

            def selectbox(self, _label, options, index=0, **_k):
                opts = list(options)
                if not opts:
                    return None
                idx = int(index) if isinstance(index, int) else 0
                if idx < 0 or idx >= len(opts):
                    idx = 0
                return opts[idx]

            def date_input(self, _label, value=None, **_k):
                return value

            def text_input(self, _label, value="", **_k):
                return value

            def text_area(self, _label, value="", **_k):
                return value

            def checkbox(self, _label, value=False, **_k):
                return bool(value)

            def metric(self, *_a, **_k):
                return None

            def download_button(self, *_a, **_k):
                return None

            def json(self, *_a, **_k):
                return None

            def success(self, *_a, **_k):
                return None

            def error(self, *_a, **_k):
                return None

            def warning(self, *_a, **_k):
                return None

        class _ExecResult:
            def __init__(self, *, rows=None, scalar=None, first_row=None):
                self._rows = rows or []
                self._scalar = scalar
                self._first = first_row

            def all(self):
                return list(self._rows)

            def scalar_one(self):
                return self._scalar

            def first(self):
                return self._first

        class _DB:
            def execute(self, query, params=None):
                q = str(query)
                if "SELECT version_num FROM alembic_version" in q:
                    return _ExecResult(first_row=("abc123",))
                if "SELECT COUNT(*) FROM sync_errors WHERE resolved_at IS NULL" in q:
                    return _ExecResult(scalar=0)
                return _ExecResult(rows=[])

        class _Repo:
            def __init__(self):
                self.db = _DB()

            def list_runtime_settings(self, environment=None, active_only=False):
                return []

            def list_ai_provider_configs(self, environment=None, active_only=False):
                return []

            def list_sync_runs(self, limit=1000):
                return []

        fake_st = _FakeSt()
        fake_user = types.SimpleNamespace(username="admin", role="admin")
        fake_slack_cfg = types.SimpleNamespace(enabled=False, bot_token="", default_channel="")
        fake_storage = types.SimpleNamespace(enabled=False, client=None, bucket="")
        fake_ebay = types.SimpleNamespace(is_configured=lambda: False, environment="sandbox")
        fake_spot = types.SimpleNamespace(provider="none", is_configured=lambda: False)
        with patch.object(system_health, "st", fake_st), patch.object(
            system_health, "current_user", return_value=fake_user
        ), patch.object(
            system_health, "ensure_permission", return_value=True
        ), patch.object(
            system_health, "render_help_panel"
        ), patch.object(
            system_health, "required_env_keys", return_value=set()
        ), patch.object(
            system_health, "required_runtime_keys", return_value=set()
        ), patch.object(
            system_health, "read_env_file", return_value={}
        ), patch.object(
            system_health, "resolve_slack_notify_config", return_value=fake_slack_cfg
        ), patch.object(
            system_health, "MediaStorageService", return_value=fake_storage
        ), patch.object(
            system_health, "EbayClient", return_value=fake_ebay
        ), patch.object(
            system_health, "SpotPriceService", return_value=fake_spot
        ), patch.object(
            system_health, "get_runtime_bool", side_effect=lambda _r, _k, default=False: bool(default)
        ), patch.object(
            system_health, "get_runtime_int", side_effect=lambda _r, _k, default=0: int(default)
        ), patch.object(
            system_health, "get_runtime_str", side_effect=lambda _r, _k, default="": str(default)
        ):
            system_health.render_system_health(_Repo())


if __name__ == "__main__":
    unittest.main()
