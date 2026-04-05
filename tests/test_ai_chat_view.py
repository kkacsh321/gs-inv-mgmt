import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.calls = []

    def subheader(self, *a, **k):
        self.calls.append(("subheader", a, k))

    def caption(self, *a, **k):
        self.calls.append(("caption", a, k))

    def info(self, *a, **k):
        self.calls.append(("info", a, k))

    def stop(self):
        raise RuntimeError("STOP")



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
    spec = importlib.util.spec_from_file_location("test_ai_chat_view_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ai_chat_view = _load_module()


class AiChatViewTests(unittest.TestCase):
    def test_render_ai_chat_denied_read_stops(self):
        fake_st = _FakeSt()
        user = SimpleNamespace(username="admin", role="admin")
        voice_cfg = SimpleNamespace(enabled=False, stt_enabled=False, tts_enabled=False, provider="", stt_model="", tts_model="", tts_voice="", tts_response_format="")

        with patch.object(ai_chat_view, "st", fake_st), \
            patch.object(ai_chat_view, "current_user", return_value=user), \
            patch.object(ai_chat_view, "_allowed_domains_for_role", return_value={"inventory"}), \
            patch.object(ai_chat_view, "resolve_voice_runtime_config", return_value=voice_cfg), \
            patch.object(ai_chat_view, "render_help_panel", return_value=None), \
            patch.object(ai_chat_view, "ensure_permission", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "STOP"):
                ai_chat_view.render_ai_chat(SimpleNamespace())

    def test_render_ai_chat_denied_ai_permission_stops(self):
        fake_st = _FakeSt()
        user = SimpleNamespace(username="admin", role="admin")
        voice_cfg = SimpleNamespace(enabled=False, stt_enabled=False, tts_enabled=False, provider="", stt_model="", tts_model="", tts_voice="", tts_response_format="")

        with patch.object(ai_chat_view, "st", fake_st), \
            patch.object(ai_chat_view, "current_user", return_value=user), \
            patch.object(ai_chat_view, "_allowed_domains_for_role", return_value={"inventory"}), \
            patch.object(ai_chat_view, "resolve_voice_runtime_config", return_value=voice_cfg), \
            patch.object(ai_chat_view, "render_help_panel", return_value=None), \
            patch.object(ai_chat_view, "ensure_permission", side_effect=[True, False]):
            with self.assertRaisesRegex(RuntimeError, "STOP"):
                ai_chat_view.render_ai_chat(SimpleNamespace())

    def test_render_ai_chat_domain_disabled_returns(self):
        fake_st = _FakeSt()
        user = SimpleNamespace(username="admin", role="admin")
        voice_cfg = SimpleNamespace(enabled=False, stt_enabled=False, tts_enabled=False, provider="", stt_model="", tts_model="", tts_voice="", tts_response_format="")

        with patch.object(ai_chat_view, "st", fake_st), \
            patch.object(ai_chat_view, "current_user", return_value=user), \
            patch.object(ai_chat_view, "_allowed_domains_for_role", return_value={"inventory"}), \
            patch.object(ai_chat_view, "resolve_voice_runtime_config", return_value=voice_cfg), \
            patch.object(ai_chat_view, "render_help_panel", return_value=None), \
            patch.object(ai_chat_view, "ensure_permission", side_effect=[True, True]), \
            patch.object(ai_chat_view, "is_ai_domain_enabled", return_value=False):
            ai_chat_view.render_ai_chat(SimpleNamespace())

        self.assertTrue(any(c[0] == "info" and "disabled" in str(c[1][0]).lower() for c in fake_st.calls))


if __name__ == "__main__":
    unittest.main()
