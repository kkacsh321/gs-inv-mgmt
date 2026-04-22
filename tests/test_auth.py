import unittest
import sys
import types
import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import auth


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.query_params = {}
        self.errors = []
        self.warnings = []
        self.infos = []

    def error(self, msg):
        self.errors.append(str(msg))

    def warning(self, msg):
        self.warnings.append(str(msg))

    def info(self, msg):
        self.infos.append(str(msg))


class _RaisingQueryParams(dict):
    def get(self, key, default=None):
        raise RuntimeError("query get failed")

    def __setitem__(self, key, value):
        raise RuntimeError("query set failed")


class _ContainsRaisingQueryParams(dict):
    def __contains__(self, _key):
        raise RuntimeError("contains failed")


class _FakeRawCookieManager(dict):
    def __init__(self, ready_value=True):
        super().__init__()
        self._ready_value = ready_value
        self.saved = False

    def ready(self):
        if isinstance(self._ready_value, Exception):
            raise self._ready_value
        return bool(self._ready_value)

    def save(self):
        self.saved = True
        return True

    def get(self, key, default=None):
        return super().get(key, default)


class _FakeFernet:
    def encrypt(self, payload: bytes) -> bytes:
        return b"enc:" + payload

    def decrypt(self, payload: bytes) -> bytes:
        if not payload.startswith(b"enc:"):
            raise ValueError("bad token")
        return payload[4:]


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.fake_st = _FakeSt()

    def test_normalized_role_defaults_to_viewer(self) -> None:
        self.assertEqual(auth._normalized_role("ADMIN"), "admin")
        self.assertEqual(auth._normalized_role("unknown"), "viewer")
        self.assertEqual(auth._normalized_role(""), "viewer")

    def test_build_and_parse_auth_token(self) -> None:
        with patch("app.auth._auth_signing_key", return_value="secret"):
            token = auth._build_auth_remember_token(username="alice", role="ops", expires_at=9999999999)
            claims = auth._parse_auth_remember_token(token)
        self.assertIsNotNone(claims)
        self.assertEqual(claims["username"], "alice")
        self.assertEqual(claims["role"], "ops")

    def test_parse_auth_token_rejects_tamper(self) -> None:
        with patch("app.auth._auth_signing_key", return_value="secret"):
            token = auth._build_auth_remember_token(username="alice", role="ops", expires_at=9999999999)
            tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
            claims = auth._parse_auth_remember_token(tampered)
        self.assertIsNone(claims)

    def test_parse_auth_token_invalid_shapes(self) -> None:
        self.assertIsNone(auth._parse_auth_remember_token(""))
        self.assertIsNone(auth._parse_auth_remember_token("no-dot"))
        self.assertIsNone(auth._parse_auth_remember_token(".sig"))
        self.assertIsNone(auth._parse_auth_remember_token("payload."))

    def test_has_permission_admin_super_role(self) -> None:
        self.fake_st.session_state["auth_role_permissions"] = {"viewer": {"read"}}
        with patch("app.auth.st", self.fake_st):
            self.assertTrue(auth.has_permission("admin", "manage_settings"))
            self.assertFalse(auth.has_permission("viewer", "manage_settings"))

    def test_ensure_permission_writes_error_on_denied(self) -> None:
        self.fake_st.session_state["auth_role_permissions"] = {"viewer": {"read"}}
        with patch("app.auth.st", self.fake_st):
            ok = auth.ensure_permission(auth.UserContext(username="sam", role="viewer"), "update", "Edit Product")
        self.assertFalse(ok)
        self.assertTrue(any("requires" in m for m in self.fake_st.errors))

    def test_require_authenticated_session_behaviors(self) -> None:
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_require_password_auth=False)
        ):
            self.assertTrue(auth.require_authenticated_session())

        self.fake_st.session_state.clear()
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_require_password_auth=True)
        ):
            self.assertFalse(auth.require_authenticated_session())
            self.assertTrue(any("Authentication is enabled" in m for m in self.fake_st.warnings))

        self.fake_st.session_state = {"auth_users_count": 1, "auth_authenticated": True}
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_require_password_auth=True)
        ):
            self.assertTrue(auth.require_authenticated_session())

    def test_require_authenticated_session_allows_oauth_callback_query(self) -> None:
        self.fake_st.session_state = {"auth_users_count": 1, "auth_authenticated": False}
        self.fake_st.query_params = {"code": "abc123", "state": "state123"}
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_require_password_auth=True)
        ):
            self.assertTrue(auth.require_authenticated_session(allow_oauth_callback_query=True))

    def test_auth_debug_snapshot_includes_token_claims(self) -> None:
        now_ts = 1000
        self.fake_st.session_state.update(
            {
                "auth_authenticated": True,
                "auth_username": "admin",
                "auth_role": "admin",
                "auth_remember_enabled": True,
                "auth_cookie_manager_state": "ready",
                "auth_cookie_manager_error": "",
            }
        )
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings",
            SimpleNamespace(
                app_require_password_auth=True,
                app_auth_cookie_enabled=True,
                app_auth_remember_days=14,
                app_user_role="viewer",
            ),
        ), patch("app.auth._get_cookie_manager", return_value=object()), patch(
            "app.auth._get_cookie_auth_token", return_value="cookie"
        ), patch("app.auth._get_query_auth_token", return_value="query"), patch(
            "app.auth._parse_auth_remember_token",
            side_effect=[{"username": "admin", "exp": now_ts + 100}, {"username": "admin", "exp": now_ts + 50}],
        ), patch("app.auth.time.time", return_value=now_ts):
            snap = auth.auth_debug_snapshot()

        self.assertTrue(snap["cookie_manager_ready"])
        self.assertTrue(snap["cookie_token_valid"])
        self.assertTrue(snap["query_token_valid"])
        self.assertEqual(snap["auth_role_session"], "admin")

    def test_get_query_auth_token_primary_and_fallback_paths(self) -> None:
        self.fake_st.query_params = {"auth": ["tok-primary"]}
        with patch("app.auth.st", self.fake_st):
            token = auth._get_query_auth_token()
        self.assertEqual(token, "tok-primary")
        self.assertEqual(self.fake_st.session_state["auth_query_token_read_status"], "ok")

        fallback_st = _FakeSt()
        fallback_st.query_params = _RaisingQueryParams()
        fallback_st.experimental_get_query_params = lambda: {"auth": ["tok-fallback"]}
        with patch("app.auth.st", fallback_st):
            token = auth._get_query_auth_token()
        self.assertEqual(token, "tok-fallback")
        self.assertEqual(fallback_st.session_state["auth_query_token_read_status"], "ok_experimental")

        err_st = _FakeSt()
        err_st.query_params = _RaisingQueryParams()

        def _boom():
            raise RuntimeError("experimental failed")

        err_st.experimental_get_query_params = _boom
        with patch("app.auth.st", err_st):
            token = auth._get_query_auth_token()
        self.assertEqual(token, "")
        self.assertEqual(err_st.session_state["auth_query_token_read_status"], "error")

    def test_set_and_clear_query_auth_token_primary_and_fallback_paths(self) -> None:
        with patch("app.auth.st", self.fake_st):
            self.assertTrue(auth._set_query_auth_token("abc"))
            self.assertEqual(self.fake_st.query_params["auth"], "abc")
            auth._clear_query_auth_token()
            self.assertNotIn("auth", self.fake_st.query_params)

        fallback_st = _FakeSt()
        fallback_st.query_params = _RaisingQueryParams()
        captured: dict[str, str] = {}
        fallback_st.experimental_set_query_params = lambda **kwargs: captured.update(kwargs)
        with patch("app.auth.st", fallback_st):
            self.assertTrue(auth._set_query_auth_token("xyz"))
            self.assertEqual(fallback_st.session_state["auth_query_token_write_status"], "ok_experimental")
            self.assertEqual(captured.get("auth"), "xyz")

        err_st = _FakeSt()
        err_st.query_params = _RaisingQueryParams()

        def _boom(**_kwargs):
            raise RuntimeError("set failed")

        err_st.experimental_set_query_params = _boom
        with patch("app.auth.st", err_st):
            self.assertFalse(auth._set_query_auth_token("zzz"))
            self.assertEqual(err_st.session_state["auth_query_token_write_status"], "error")
            auth._clear_query_auth_token()
            self.assertEqual(err_st.session_state["auth_query_token_clear_status"], "ok")

    def test_restore_auth_from_query_and_cookie_tokens(self) -> None:
        user_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=True)}
        self.fake_st.session_state.clear()

        with patch("app.auth.st", self.fake_st), patch("app.auth._get_query_auth_token", return_value="q"), patch(
            "app.auth._parse_auth_remember_token", return_value={"username": "alice", "role": "ops", "exp": 9999}
        ), patch("app.auth.time.time", return_value=1000):
            ok = auth._restore_auth_from_query_token(user_map)
        self.assertTrue(ok)
        self.assertTrue(self.fake_st.session_state["auth_authenticated"])
        self.assertEqual(self.fake_st.session_state["auth_role"], "ops")

        with patch("app.auth.st", self.fake_st), patch("app.auth._get_cookie_auth_token", return_value="c"), patch(
            "app.auth._parse_auth_remember_token", return_value={"username": "alice", "role": "ops", "exp": 9999}
        ), patch("app.auth.time.time", return_value=1000):
            ok = auth._restore_auth_from_cookie_token(user_map)
        self.assertTrue(ok)

    def test_restore_auth_clears_invalid_tokens(self) -> None:
        user_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=True)}
        with patch("app.auth.st", self.fake_st), patch("app.auth._get_query_auth_token", return_value="q"), patch(
            "app.auth._parse_auth_remember_token", return_value={"username": "alice", "exp": 10}
        ), patch("app.auth.time.time", return_value=1000), patch("app.auth._clear_query_auth_token") as clear_q:
            ok = auth._restore_auth_from_query_token(user_map)
        self.assertFalse(ok)
        clear_q.assert_called_once()

        inactive_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=False)}
        with patch("app.auth.st", self.fake_st), patch("app.auth._get_cookie_auth_token", return_value="c"), patch(
            "app.auth._parse_auth_remember_token", return_value={"username": "alice", "exp": 9999}
        ), patch("app.auth.time.time", return_value=1000), patch("app.auth._clear_cookie_auth_token") as clear_c:
            ok = auth._restore_auth_from_cookie_token(inactive_map)
        self.assertFalse(ok)
        clear_c.assert_called_once()

    def test_restore_auth_rejects_unknown_user(self) -> None:
        with patch("app.auth.st", self.fake_st), patch("app.auth._get_query_auth_token", return_value="q"), patch(
            "app.auth._parse_auth_remember_token", return_value={"username": "missing", "exp": 9999}
        ), patch("app.auth.time.time", return_value=1000), patch("app.auth._clear_query_auth_token") as clear_q:
            ok = auth._restore_auth_from_query_token({})
        self.assertFalse(ok)
        clear_q.assert_called_once()

    def test_ensure_remember_tokens_for_authenticated_user(self) -> None:
        self.fake_st.session_state.update(
            {
                "auth_authenticated": True,
                "auth_remember_enabled": True,
                "auth_username": "alice",
            }
        )
        user_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=True)}

        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_auth_remember_days=2)
        ), patch("app.auth.time.time", return_value=100), patch(
            "app.auth._get_query_auth_token", return_value="old-q"
        ), patch("app.auth._get_cookie_auth_token", return_value="old-c"), patch(
            "app.auth._parse_auth_remember_token", return_value=None
        ), patch("app.auth._build_auth_remember_token", return_value="new-token"), patch(
            "app.auth._set_query_auth_token"
        ) as set_q, patch("app.auth._set_cookie_auth_token", return_value=False) as set_c:
            auth._ensure_remember_tokens_for_authenticated_user(user_map)
        set_q.assert_called_once_with("new-token")
        set_c.assert_called_once_with("new-token")

    def test_ensure_remember_tokens_skips_when_not_authenticated_or_user_missing(self) -> None:
        user_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=True)}
        self.fake_st.session_state.clear()
        with patch("app.auth.st", self.fake_st), patch("app.auth._set_query_auth_token") as set_q, patch(
            "app.auth._set_cookie_auth_token"
        ) as set_c:
            auth._ensure_remember_tokens_for_authenticated_user(user_map)
        set_q.assert_not_called()
        set_c.assert_not_called()

        self.fake_st.session_state.update(
            {"auth_authenticated": True, "auth_remember_enabled": True, "auth_username": "missing"}
        )
        with patch("app.auth.st", self.fake_st), patch("app.auth._set_query_auth_token") as set_q2, patch(
            "app.auth._set_cookie_auth_token"
        ) as set_c2:
            auth._ensure_remember_tokens_for_authenticated_user(user_map)
        set_q2.assert_not_called()
        set_c2.assert_not_called()

    def test_cookie_manager_status_and_get_cookie_manager_paths(self) -> None:
        disabled_settings = SimpleNamespace(app_auth_cookie_enabled=False, app_name="GS", app_auth_signing_key="")
        with patch("app.auth.st", self.fake_st), patch("app.auth.settings", disabled_settings):
            manager, state, err = auth._cookie_manager_status()
            self.assertIsNone(manager)
            self.assertEqual(state, "disabled")
            self.assertEqual(err, "")
            self.assertIsNone(auth._get_cookie_manager())

        pending_mgr = _FakeRawCookieManager(ready_value=False)
        enc_pending = auth._EncryptedCookieStore(cookie_manager=pending_mgr, password="pw")
        ready_mgr = _FakeRawCookieManager(ready_value=True)
        enc_ready = auth._EncryptedCookieStore(cookie_manager=ready_mgr, password="pw")
        boom_mgr = _FakeRawCookieManager(ready_value=RuntimeError("nope"))
        enc_boom = auth._EncryptedCookieStore(cookie_manager=boom_mgr, password="pw")

        self.fake_st.session_state["auth_cookie_manager"] = enc_pending
        enabled_settings = SimpleNamespace(app_auth_cookie_enabled=True, app_name="GS", app_auth_signing_key="k")
        with patch("app.auth._cookie_manager_backend", return_value=(_FakeRawCookieManager, "extra_streamlit_components")), patch(
            "app.auth.st", self.fake_st
        ), patch("app.auth.settings", enabled_settings):
            manager, state, _err = auth._cookie_manager_status()
            self.assertIs(manager, enc_pending)
            self.assertEqual(state, "pending")
            self.assertIsNone(auth._get_cookie_manager())

        self.fake_st.session_state["auth_cookie_manager"] = enc_ready
        with patch("app.auth._cookie_manager_backend", return_value=(_FakeRawCookieManager, "extra_streamlit_components")), patch(
            "app.auth.st", self.fake_st
        ), patch("app.auth.settings", enabled_settings):
            self.assertIsNotNone(auth._get_cookie_manager())

        self.fake_st.session_state["auth_cookie_manager"] = enc_boom
        with patch("app.auth._cookie_manager_backend", return_value=(_FakeRawCookieManager, "extra_streamlit_components")), patch(
            "app.auth.st", self.fake_st
        ), patch("app.auth.settings", enabled_settings):
            manager, state, err = auth._cookie_manager_status()
            self.assertIsNone(manager)
            self.assertEqual(state, "error")
            self.assertIn("RuntimeError", err)

    def test_cookie_auth_token_set_get_clear_paths(self) -> None:
        mgr = _FakeRawCookieManager(ready_value=True)
        enc = auth._EncryptedCookieStore(cookie_manager=mgr, password="pw")
        with patch.object(auth._EncryptedCookieStore, "_setup_fernet", autospec=True) as setup_mock:
            setup_mock.side_effect = lambda self: setattr(self, "_fernet", _FakeFernet())
            enc.set("auth_token", "abc")
            self.assertEqual(enc.get("auth_token"), "abc")
            self.assertEqual(enc.get("missing", "dft"), "dft")
            mgr["auth_token"] = "not-encrypted"
            self.assertEqual(enc.get("auth_token", "fallback"), "fallback")
            enc.delete("auth_token")
            self.assertNotIn("auth_token", mgr)

        self.fake_st.session_state["auth_cookie_manager"] = enc
        enabled_settings = SimpleNamespace(app_auth_cookie_enabled=True, app_name="GS", app_auth_signing_key="k")
        with patch("app.auth._cookie_manager_backend", return_value=(_FakeRawCookieManager, "extra_streamlit_components")), patch(
            "app.auth.st", self.fake_st
        ), patch("app.auth.settings", enabled_settings):
            self.assertTrue(auth._set_cookie_auth_token("remember"))
            self.assertEqual(auth._get_cookie_auth_token(), "remember")
            auth._clear_cookie_auth_token()
            self.assertEqual(auth._get_cookie_auth_token(), "")

    def test_require_authenticated_session_bootstrap_toggle(self) -> None:
        self.fake_st.session_state = {"auth_users_count": 0, "auth_authenticated": False}
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_require_password_auth=True)
        ):
            self.assertTrue(auth.require_authenticated_session(allow_bootstrap_if_no_users=True))
            self.assertFalse(auth.require_authenticated_session(allow_bootstrap_if_no_users=False))

    def test_current_user_defaults(self) -> None:
        self.fake_st.session_state = {}
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_user_role="ops")
        ):
            user = auth.current_user()
        self.assertEqual(user.username, "employee")
        self.assertEqual(user.role, "ops")

    def test_cookie_manager_import_error_paths(self) -> None:
        settings_mock = SimpleNamespace(app_auth_cookie_enabled=True, app_name="GS", app_auth_signing_key="k")
        with patch("app.auth._cookie_manager_backend", return_value=(None, "")), patch(
            "app.auth.st", self.fake_st
        ), patch("app.auth.settings", settings_mock):
            self.assertIsNone(auth._get_cookie_manager())
            manager, state, err = auth._cookie_manager_status()
        self.assertIsNone(manager)
        self.assertEqual(state, "error")
        self.assertEqual(err, "manager_none")

    def test_cookie_manager_backend_prefers_extra_streamlit_components_only(self) -> None:
        fake_extra_mod = types.ModuleType("extra_streamlit_components")
        fake_extra_mod.CookieManager = _FakeRawCookieManager
        with patch.dict(sys.modules, {"extra_streamlit_components": fake_extra_mod}):
            cls, backend = auth._cookie_manager_backend()
        self.assertIs(cls, _FakeRawCookieManager)
        self.assertEqual(backend, "extra_streamlit_components")

        with patch.dict(sys.modules, {"extra_streamlit_components": None}):
            cls2, backend2 = auth._cookie_manager_backend()
        self.assertIsNone(cls2)
        self.assertEqual(backend2, "")

    def test_cookie_manager_status_builds_manager_ready_and_none_paths(self) -> None:
        settings_mock = SimpleNamespace(app_auth_cookie_enabled=True, app_name="GS", app_auth_signing_key="k")
        class _CookieMgrFactory(_FakeRawCookieManager):
            def __init__(self, *args, **kwargs):
                super().__init__(ready_value=True)

        st_local = _FakeSt()
        with patch("app.auth._cookie_manager_backend", return_value=(_CookieMgrFactory, "extra_streamlit_components")), patch(
            "app.auth.st", st_local
        ), patch("app.auth.settings", settings_mock):
            manager, state, err = auth._cookie_manager_status()
        self.assertIsNotNone(manager)
        self.assertEqual(state, "ready")
        self.assertEqual(err, "")

        st_local.session_state["auth_cookie_manager"] = None
        with patch("app.auth._cookie_manager_backend", return_value=(_CookieMgrFactory, "extra_streamlit_components")), patch(
            "app.auth.st", st_local
        ), patch("app.auth.settings", settings_mock):
            manager2, state2, err2 = auth._cookie_manager_status()
        self.assertIsNone(manager2)
        self.assertEqual(state2, "error")
        self.assertEqual(err2, "manager_none")

    def test_get_cookie_auth_token_exception_path(self) -> None:
        class _BadManager:
            def get(self, *_a, **_k):
                raise RuntimeError("boom")

        with patch("app.auth._get_cookie_manager", return_value=_BadManager()):
            self.assertEqual(auth._get_cookie_auth_token(), "")

    def test_parse_auth_remember_token_invalid_payload_shapes(self) -> None:
        with patch("app.auth._auth_signing_key", return_value="secret"):
            list_payload = auth._urlsafe_b64_encode(b'["not","dict"]')
            sig = auth._urlsafe_b64_encode(
                __import__("hmac").new(b"secret", list_payload.encode("utf-8"), __import__("hashlib").sha256).digest()
            )
            self.assertIsNone(auth._parse_auth_remember_token(f"{list_payload}.{sig}"))

            bad_payload = auth._urlsafe_b64_encode(b'{"u":"","r":"ops","exp":0}')
            bad_sig = auth._urlsafe_b64_encode(
                __import__("hmac").new(b"secret", bad_payload.encode("utf-8"), __import__("hashlib").sha256).digest()
            )
            self.assertIsNone(auth._parse_auth_remember_token(f"{bad_payload}.{bad_sig}"))

    def test_clear_query_auth_token_double_failure_sets_error(self) -> None:
        st_err = _FakeSt()
        st_err.query_params = _ContainsRaisingQueryParams()

        def _boom(**_kwargs):
            raise RuntimeError("experimental clear failed")

        st_err.experimental_set_query_params = _boom
        with patch("app.auth.st", st_err):
            auth._clear_query_auth_token()
        self.assertEqual(st_err.session_state.get("auth_query_token_clear_status"), "error")
        self.assertIn("RuntimeError", st_err.session_state.get("auth_query_token_clear_error", ""))

    def test_restore_cookie_auth_invalid_claims_clears_cookie(self) -> None:
        user_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=True)}
        with patch("app.auth.st", self.fake_st), patch("app.auth._get_cookie_auth_token", return_value="tok"), patch(
            "app.auth._parse_auth_remember_token", return_value=None
        ), patch("app.auth.time.time", return_value=1000), patch("app.auth._clear_cookie_auth_token") as clear_cookie:
            ok = auth._restore_auth_from_cookie_token(user_map)
        self.assertFalse(ok)
        clear_cookie.assert_called_once()

    def test_ensure_remember_tokens_branch_mismatch_and_existing_query_token(self) -> None:
        self.fake_st.session_state.update(
            {
                "auth_authenticated": True,
                "auth_remember_enabled": True,
                "auth_username": "alice",
            }
        )
        user_map = {"alice": SimpleNamespace(username="alice", role="ops", is_active=True)}
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_auth_remember_days=2)
        ), patch("app.auth.time.time", return_value=100), patch(
            "app.auth._get_query_auth_token", return_value="same-token"
        ), patch("app.auth._get_cookie_auth_token", return_value="cookie-token"), patch(
            "app.auth._parse_auth_remember_token",
            side_effect=[
                {"username": "other-user", "exp": 10_000},
                {"username": "alice", "exp": 11_000},
            ],
        ), patch("app.auth._build_auth_remember_token", return_value="same-token"), patch(
            "app.auth._set_query_auth_token"
        ) as set_query, patch("app.auth._set_cookie_auth_token") as set_cookie:
            auth._ensure_remember_tokens_for_authenticated_user(user_map)
        set_query.assert_not_called()
        set_cookie.assert_called_once_with("same-token")

    def test_ensure_remember_tokens_empty_username(self) -> None:
        self.fake_st.session_state.update(
            {
                "auth_authenticated": True,
                "auth_remember_enabled": True,
                "auth_username": "",
            }
        )
        with patch("app.auth.st", self.fake_st), patch("app.auth._set_query_auth_token") as set_query, patch(
            "app.auth._set_cookie_auth_token"
        ) as set_cookie:
            auth._ensure_remember_tokens_for_authenticated_user({})
        set_query.assert_not_called()
        set_cookie.assert_not_called()

    def test_load_rbac_from_db_import_and_runtime_error_paths(self) -> None:
        with patch.dict(sys.modules, {"sqlalchemy": None}):
            users, perms = auth._load_rbac_from_db()
        self.assertEqual(users, [])
        self.assertEqual(perms, {})

        fake_models = types.ModuleType("app.db.models")
        fake_models.AppUser = object()
        fake_models.RolePermission = object()
        fake_session = types.ModuleType("app.db.session")

        class _BrokenSession:
            def scalars(self, *_a, **_k):
                raise RuntimeError("db failed")

            def close(self):
                return None

        fake_session.SessionLocal = lambda: _BrokenSession()
        fake_sqlalchemy = types.ModuleType("sqlalchemy")
        fake_sqlalchemy.select = lambda *_a, **_k: object()
        with patch.dict(
            sys.modules,
            {
                "sqlalchemy": fake_sqlalchemy,
                "app.db.models": fake_models,
                "app.db.session": fake_session,
            },
        ):
            users2, perms2 = auth._load_rbac_from_db()
        self.assertEqual(users2, [])
        self.assertEqual(perms2, {})

    def test_require_authenticated_session_warns_when_not_authenticated(self) -> None:
        self.fake_st.session_state = {"auth_users_count": 1, "auth_authenticated": False}
        with patch("app.auth.st", self.fake_st), patch(
            "app.auth.settings", SimpleNamespace(app_require_password_auth=True)
        ):
            ok = auth.require_authenticated_session()
        self.assertFalse(ok)
        self.assertTrue(any("Sign in required." in str(m) for m in self.fake_st.warnings))

    def test_init_user_context_sidebar_password_login_success(self) -> None:
        def _text_input(label, *args, **kwargs):
            if label == "Username":
                return "alice"
            if label == "Password":
                return "pass"
            return kwargs.get("value", "")

        st_mock = SimpleNamespace(
            session_state={},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(side_effect=_text_input),
            checkbox=MagicMock(return_value=True),
            form_submit_button=MagicMock(return_value=True),
            button=MagicMock(return_value=False),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="alice"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_mock.sidebar.expander.return_value = _Ctx()

        users = [SimpleNamespace(username="alice", role="ops", is_active=True, password_hash="h", password_salt="s")]
        settings_mock = SimpleNamespace(
            app_require_password_auth=True,
            app_allow_role_override=False,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )

        with patch("app.auth.st", st_mock), patch("app.auth.settings", settings_mock), patch(
            "app.auth._load_rbac_from_db", return_value=(users, {"ops": {"read", "update"}})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")), patch(
            "app.auth._restore_auth_from_cookie_token", return_value=False
        ), patch("app.auth._restore_auth_from_query_token", return_value=False), patch(
            "app.services.security.verify_password", return_value=True
        ), patch(
            "app.auth._build_auth_remember_token", return_value="tok"
        ), patch(
            "app.auth._set_cookie_auth_token", return_value=False
        ) as set_cookie, patch(
            "app.auth._set_query_auth_token"
        ) as set_query:
            user = auth.init_user_context_sidebar()

        self.assertEqual(user.username, "alice")
        self.assertEqual(user.role, "ops")
        self.assertTrue(st_mock.session_state.get("auth_authenticated"))
        set_cookie.assert_called_once_with("tok")
        set_query.assert_called_once_with("tok")
        st_mock.rerun.assert_called_once()

    def test_init_user_context_sidebar_password_login_skips_query_token_on_oauth_callback(self) -> None:
        def _text_input(label, *args, **kwargs):
            if label == "Username":
                return "alice"
            if label == "Password":
                return "pass"
            return kwargs.get("value", "")

        st_mock = SimpleNamespace(
            session_state={},
            query_params={"code": "oauth-code", "state": "oauth-state"},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(side_effect=_text_input),
            checkbox=MagicMock(return_value=True),
            form_submit_button=MagicMock(return_value=True),
            button=MagicMock(return_value=False),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="alice"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_mock.sidebar.expander.return_value = _Ctx()

        users = [SimpleNamespace(username="alice", role="ops", is_active=True, password_hash="h", password_salt="s")]
        settings_mock = SimpleNamespace(
            app_require_password_auth=True,
            app_allow_role_override=False,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )

        with patch("app.auth.st", st_mock), patch("app.auth.settings", settings_mock), patch(
            "app.auth._load_rbac_from_db", return_value=(users, {"ops": {"read", "update"}})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")), patch(
            "app.auth._restore_auth_from_cookie_token", return_value=False
        ), patch("app.auth._restore_auth_from_query_token", return_value=False), patch(
            "app.services.security.verify_password", return_value=True
        ), patch(
            "app.auth._build_auth_remember_token", return_value="tok"
        ), patch(
            "app.auth._set_cookie_auth_token"
        ) as set_cookie, patch(
            "app.auth._set_query_auth_token"
        ) as set_query:
            user = auth.init_user_context_sidebar()

        self.assertEqual(user.username, "alice")
        self.assertEqual(user.role, "ops")
        self.assertTrue(st_mock.session_state.get("auth_authenticated"))
        set_cookie.assert_called_once_with("tok")
        set_query.assert_not_called()
        st_mock.rerun.assert_called_once()

    def test_init_user_context_sidebar_password_login_invalid(self) -> None:
        st_mock = SimpleNamespace(
            session_state={},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(side_effect=["alice", "wrong"]),
            checkbox=MagicMock(return_value=False),
            form_submit_button=MagicMock(return_value=True),
            button=MagicMock(return_value=False),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="alice"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_mock.sidebar.expander.return_value = _Ctx()

        users = [SimpleNamespace(username="alice", role="ops", is_active=True, password_hash="h", password_salt="s")]
        settings_mock = SimpleNamespace(
            app_require_password_auth=True,
            app_allow_role_override=False,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )

        with patch("app.auth.st", st_mock), patch("app.auth.settings", settings_mock), patch(
            "app.auth._load_rbac_from_db", return_value=(users, {})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")), patch(
            "app.auth._restore_auth_from_cookie_token", return_value=False
        ), patch("app.auth._restore_auth_from_query_token", return_value=False), patch(
            "app.services.security.verify_password", return_value=False
        ):
            user = auth.init_user_context_sidebar()

        self.assertFalse(st_mock.session_state.get("auth_authenticated", False))
        self.assertEqual(user.role, "viewer")
        st_mock.error.assert_called_with("Invalid username/password.")

    def test_init_user_context_sidebar_non_password_override(self) -> None:
        st_mock = SimpleNamespace(
            session_state={},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(),
            checkbox=MagicMock(return_value=False),
            form_submit_button=MagicMock(return_value=False),
            button=MagicMock(return_value=False),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="bob"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_mock.sidebar.expander.return_value = _Ctx()

        users = [
            SimpleNamespace(username="alice", role="viewer", is_active=True),
            SimpleNamespace(username="bob", role="admin", is_active=True),
        ]
        settings_mock = SimpleNamespace(
            app_require_password_auth=False,
            app_allow_role_override=True,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )

        with patch("app.auth.st", st_mock), patch("app.auth.settings", settings_mock), patch(
            "app.auth._load_rbac_from_db", return_value=(users, {})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")):
            user = auth.init_user_context_sidebar()

        self.assertEqual(st_mock.session_state.get("auth_username"), "bob")
        self.assertEqual(st_mock.session_state.get("auth_role"), "admin")
        self.assertEqual(user.username, "bob")
        self.assertEqual(user.role, "admin")

    def test_derive_cookie_key_and_encrypted_store_key_params_paths(self) -> None:
        fake_hashes = types.SimpleNamespace(SHA256=lambda: "sha256")

        class _FakePBKDF2HMAC:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def derive(self, payload: bytes) -> bytes:
                return b"derived:" + payload

        fake_pbkdf2_mod = types.ModuleType("cryptography.hazmat.primitives.kdf.pbkdf2")
        fake_pbkdf2_mod.PBKDF2HMAC = _FakePBKDF2HMAC
        fake_primitives_mod = types.ModuleType("cryptography.hazmat.primitives")
        fake_primitives_mod.hashes = fake_hashes

        with patch.dict(
            sys.modules,
            {
                "cryptography": types.ModuleType("cryptography"),
                "cryptography.hazmat": types.ModuleType("cryptography.hazmat"),
                "cryptography.hazmat.primitives": fake_primitives_mod,
                "cryptography.hazmat.primitives.kdf": types.ModuleType("cryptography.hazmat.primitives.kdf"),
                "cryptography.hazmat.primitives.kdf.pbkdf2": fake_pbkdf2_mod,
            },
        ):
            auth._derive_cookie_key.cache_clear()
            key = auth._derive_cookie_key(salt=b"1234567890123456", iterations=10, password="pw")
            self.assertTrue(isinstance(key, bytes))
            self.assertEqual(key, base64.urlsafe_b64encode(b"derived:pw"))

        mgr = _FakeRawCookieManager(ready_value=True)
        store = auth._EncryptedCookieStore(cookie_manager=mgr, password="pw")
        self.assertIsNone(store._get_key_params())
        salt, iterations, magic = store._initialize_key_params()
        self.assertEqual(iterations, 390000)
        self.assertEqual(len(salt), 16)
        self.assertEqual(len(magic), 16)
        parsed = store._get_key_params()
        self.assertIsNotNone(parsed)
        mgr[auth._EncryptedCookieStore.KEY_PARAMS_COOKIE] = "bad:value"
        self.assertIsNone(store._get_key_params())

    def test_set_and_clear_cookie_token_exception_paths(self) -> None:
        class _BrokenManager:
            def set(self, *_a, **_k):
                raise RuntimeError("set failed")

            def delete(self, *_a, **_k):
                raise RuntimeError("delete failed")

            def save(self):
                raise RuntimeError("save failed")

        with patch("app.auth._get_cookie_manager", return_value=_BrokenManager()):
            self.assertFalse(auth._set_cookie_auth_token("tok"))
            # should not raise
            auth._clear_cookie_auth_token()
        with patch("app.auth._get_cookie_manager", return_value=None):
            self.assertFalse(auth._set_cookie_auth_token("tok"))
            auth._clear_cookie_auth_token()

    def test_encrypted_cookie_store_setup_fernet_with_existing_key_params(self) -> None:
        mgr = _FakeRawCookieManager(ready_value=True)
        salt = base64.b64encode(b"1234567890abcdef").decode("ascii")
        magic = base64.b64encode(b"fedcba0987654321").decode("ascii")
        mgr[auth._EncryptedCookieStore.KEY_PARAMS_COOKIE] = f"{salt}:390000:{magic}"
        store = auth._EncryptedCookieStore(cookie_manager=mgr, password="pw")
        fake_fernet_mod = types.ModuleType("cryptography.fernet")
        fake_fernet_mod.Fernet = lambda key: SimpleNamespace(key=key)
        with patch.dict(sys.modules, {"cryptography.fernet": fake_fernet_mod}), patch(
            "app.auth._derive_cookie_key", return_value=b"derived-key"
        ):
            store._setup_fernet()
        self.assertEqual(getattr(store._fernet, "key", b""), b"derived-key")

    def test_load_rbac_from_db_success_path(self) -> None:
        class _Sel:
            def where(self, *_a, **_k):
                return self

            def order_by(self, *_a, **_k):
                return self

        fake_sqlalchemy = types.ModuleType("sqlalchemy")
        fake_sqlalchemy.select = lambda *_a, **_k: _Sel()

        class _F:
            @staticmethod
            def is_(_v):
                return True

            @staticmethod
            def asc():
                return True

        fake_models = types.ModuleType("app.db.models")
        fake_models.AppUser = SimpleNamespace(is_active=_F(), username=_F())
        fake_models.RolePermission = SimpleNamespace(role=_F())

        class _DB:
            def __init__(self):
                self.calls = 0

            def scalars(self, *_a, **_k):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        all=lambda: [SimpleNamespace(username="alice", role="ops", is_active=True)]
                    )
                return SimpleNamespace(
                    all=lambda: [SimpleNamespace(role="ops", permission="read"), SimpleNamespace(role="ops", permission="update")]
                )

            def close(self):
                return None

        fake_session = types.ModuleType("app.db.session")
        fake_session.SessionLocal = lambda: _DB()
        with patch.dict(
            sys.modules,
            {
                "sqlalchemy": fake_sqlalchemy,
                "app.db.models": fake_models,
                "app.db.session": fake_session,
            },
        ):
            users, perms = auth._load_rbac_from_db()
        self.assertEqual(len(users), 1)
        self.assertEqual(perms.get("ops"), {"read", "update"})

    def test_init_user_context_sidebar_sign_out_branch(self) -> None:
        st_mock = SimpleNamespace(
            session_state={"auth_authenticated": True, "auth_username": "alice", "auth_role": "ops"},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(return_value=""),
            checkbox=MagicMock(return_value=False),
            form_submit_button=MagicMock(return_value=False),
            button=MagicMock(return_value=True),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="alice"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_mock.sidebar.expander.return_value = _Ctx()
        users = [SimpleNamespace(username="alice", role="ops", is_active=True, password_hash="h", password_salt="s")]
        settings_mock = SimpleNamespace(
            app_require_password_auth=True,
            app_allow_role_override=False,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )
        with patch("app.auth.st", st_mock), patch("app.auth.settings", settings_mock), patch(
            "app.auth._load_rbac_from_db", return_value=(users, {})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")), patch(
            "app.auth._clear_cookie_auth_token"
        ) as clear_cookie, patch("app.auth._clear_query_auth_token") as clear_query:
            auth.init_user_context_sidebar()
        self.assertFalse(st_mock.session_state.get("auth_authenticated"))
        clear_cookie.assert_called_once()
        clear_query.assert_called_once()
        st_mock.rerun.assert_called_once()

    def test_init_user_context_sidebar_no_users_password_and_non_password_paths(self) -> None:
        st_pw = SimpleNamespace(
            session_state={},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(return_value=""),
            checkbox=MagicMock(return_value=False),
            form_submit_button=MagicMock(return_value=False),
            button=MagicMock(return_value=False),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="viewer"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_pw.sidebar.expander.return_value = _Ctx()
        settings_pw = SimpleNamespace(
            app_require_password_auth=True,
            app_allow_role_override=False,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )
        with patch("app.auth.st", st_pw), patch("app.auth.settings", settings_pw), patch(
            "app.auth._load_rbac_from_db", return_value=([], {})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")):
            auth.init_user_context_sidebar()
        self.assertFalse(st_pw.session_state.get("auth_authenticated"))
        self.assertEqual(st_pw.session_state.get("auth_role"), "viewer")

        st_np = SimpleNamespace(
            session_state={"auth_username": "employee", "auth_role": "viewer"},
            sidebar=MagicMock(),
            form=MagicMock(return_value=_Ctx()),
            text_input=MagicMock(return_value="staff-user"),
            checkbox=MagicMock(return_value=False),
            form_submit_button=MagicMock(return_value=False),
            button=MagicMock(return_value=False),
            caption=MagicMock(),
            selectbox=MagicMock(return_value="ops"),
            info=MagicMock(),
            error=MagicMock(),
            rerun=MagicMock(),
        )
        st_np.sidebar.expander.return_value = _Ctx()
        settings_np = SimpleNamespace(
            app_require_password_auth=False,
            app_allow_role_override=True,
            app_env="local",
            app_user_name="employee",
            app_user_role="viewer",
            app_auth_cookie_enabled=False,
            app_auth_remember_days=14,
        )
        with patch("app.auth.st", st_np), patch("app.auth.settings", settings_np), patch(
            "app.auth._load_rbac_from_db", return_value=([], {})
        ), patch("app.auth._cookie_manager_status", return_value=(None, "disabled", "")):
            auth.init_user_context_sidebar()
        self.assertEqual(st_np.session_state.get("auth_username"), "staff-user")
        self.assertEqual(st_np.session_state.get("auth_role"), "ops")


if __name__ == "__main__":
    unittest.main()
