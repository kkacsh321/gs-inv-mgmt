import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class MainModuleTests(unittest.TestCase):
    def _load_main_with_state(self, state: dict, role: str = "ops"):
        st_mod = types.ModuleType("streamlit")
        st_mod.session_state = dict(state)
        st_mod.switch_page = MagicMock()
        st_mod.markdown = MagicMock()
        st_mod.write = MagicMock()
        st_mod.caption = MagicMock()

        page_common_mod = types.ModuleType("app.page_common")
        page_common_mod.APP_CAPTION = "caption"
        page_common_mod.ROLE_DEFAULT_PAGE = {"admin": "pages/17_Admin.py", "ops": "pages/00_Operations_Home.py"}
        page_common_mod.setup_page = MagicMock()

        auth_mod = types.ModuleType("app.auth")
        auth_mod.current_user = MagicMock(return_value=SimpleNamespace(role=role))

        shared_mod = types.ModuleType("app.components.views.shared")
        shared_mod.render_help_panel = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "streamlit": st_mod,
                "app.page_common": page_common_mod,
                "app.auth": auth_mod,
                "app.components.views.shared": shared_mod,
            },
        ):
            sys.modules.pop("app.main", None)
            importlib.import_module("app.main")

        return st_mod, page_common_mod, auth_mod, shared_mod

    def test_main_applies_role_default_landing(self):
        st_mod, page_common_mod, _auth_mod, shared_mod = self._load_main_with_state(
            {"ux_navigation_mode": "unified", "ux_role_default_landing_enabled": True}, role="ops"
        )

        page_common_mod.setup_page.assert_called()
        st_mod.switch_page.assert_called_once_with("pages/00_Operations_Home.py")
        self.assertTrue(st_mod.session_state.get("role_default_landing_applied"))
        shared_mod.render_help_panel.assert_called_once()

    def test_main_does_not_switch_when_not_unified_mode(self):
        st_mod, _page_common_mod, _auth_mod, _shared_mod = self._load_main_with_state(
            {"ux_navigation_mode": "legacy", "ux_role_default_landing_enabled": True}, role="ops"
        )
        st_mod.switch_page.assert_not_called()

    def test_main_does_not_switch_when_already_applied(self):
        st_mod, _page_common_mod, _auth_mod, _shared_mod = self._load_main_with_state(
            {
                "ux_navigation_mode": "unified",
                "ux_role_default_landing_enabled": True,
                "role_default_landing_applied": True,
            },
            role="ops",
        )
        st_mod.switch_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
