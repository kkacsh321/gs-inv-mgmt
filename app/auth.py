from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import time
import os
from functools import lru_cache

import streamlit as st

try:
    from app.config import settings
except ModuleNotFoundError:
    from config import settings

ROLES = ["viewer", "ops", "admin"]
DEFAULT_PERMISSIONS = {
    "viewer": {"read", "ai_chat_use", "ai_comp_use"},
    "ops": {
        "read",
        "create",
        "update",
        "bulk_update",
        "export",
        "ai_chat_use",
        "ai_comp_use",
        "ai_coin_grade",
        "ai_coin_identify",
    },
    "admin": {
        "read",
        "create",
        "update",
        "bulk_update",
        "export",
        "manage_settings",
        "manage_profiles",
        "ai_chat_use",
        "ai_comp_use",
        "ai_coin_grade",
        "ai_coin_identify",
    },
}


@dataclass(frozen=True)
class UserContext:
    username: str
    role: str


def _normalized_role(role: str) -> str:
    resolved = (role or "viewer").strip().lower()
    return resolved if resolved in ROLES else "viewer"


def _urlsafe_b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _urlsafe_b64_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("utf-8"))


def _auth_signing_key() -> str:
    return (settings.app_auth_signing_key or "").strip()


@lru_cache(maxsize=32)
def _derive_cookie_key(salt: bytes, iterations: int, password: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


class _EncryptedCookieStore:
    KEY_PARAMS_COOKIE = "EncryptedCookieManager.key_params"

    def __init__(self, *, cookie_manager, password: str):
        self._cookie_manager = cookie_manager
        self._password = password
        self._fernet = None

    def ready(self) -> bool:
        return self._cookie_manager is not None and bool(self._cookie_manager.ready())

    def save(self):
        return self._cookie_manager.save()

    def _setup_fernet(self):
        if self._fernet is not None:
            return
        from cryptography.fernet import Fernet

        key_params = self._get_key_params()
        if not key_params:
            key_params = self._initialize_key_params()
        salt, iterations, _magic = key_params
        key = _derive_cookie_key(salt=salt, iterations=iterations, password=self._password)
        self._fernet = Fernet(key)

    def _get_key_params(self):
        raw_key_params = self._cookie_manager.get(self.KEY_PARAMS_COOKIE)
        if not raw_key_params:
            return None
        try:
            raw_salt, raw_iterations, raw_magic = str(raw_key_params).split(":")
            return (
                base64.b64decode(raw_salt),
                int(raw_iterations),
                base64.b64decode(raw_magic),
            )
        except Exception:
            return None

    def _initialize_key_params(self):
        salt = os.urandom(16)
        iterations = 390000
        magic = os.urandom(16)
        self._cookie_manager[self.KEY_PARAMS_COOKIE] = b":".join(
            [
                base64.b64encode(salt),
                str(iterations).encode("ascii"),
                base64.b64encode(magic),
            ]
        ).decode("ascii")
        return salt, iterations, magic

    def get(self, key: str, default: str = "") -> str:
        self._setup_fernet()
        raw_value = self._cookie_manager.get(key)
        if not raw_value:
            return default
        try:
            decrypted = self._fernet.decrypt(str(raw_value).encode("utf-8")).decode("utf-8")
            return decrypted
        except Exception:
            return default

    def set(self, key: str, value: str):
        self._setup_fernet()
        encrypted = self._fernet.encrypt(str(value or "").encode("utf-8")).decode("utf-8")
        self._cookie_manager[key] = encrypted

    def delete(self, key: str):
        try:
            if key in self._cookie_manager:
                del self._cookie_manager[key]
        except Exception:
            return


def _get_cookie_manager():
    if not bool(settings.app_auth_cookie_enabled):
        return None
    try:
        from streamlit_cookies_manager import CookieManager
    except Exception:
        return None

    key = "auth_cookie_manager"
    if key not in st.session_state:
        prefix = (settings.app_name or "gs").strip().lower().replace(" ", "_")
        raw_manager = CookieManager(prefix=f"{prefix}/")
        st.session_state[key] = _EncryptedCookieStore(
            cookie_manager=raw_manager,
            password=_auth_signing_key() or "change-me-signing-key",
        )
    manager = st.session_state.get(key)
    try:
        if manager is None or not manager.ready():
            return None
    except Exception:
        return None
    return manager


def _cookie_manager_status() -> tuple[object | None, str, str]:
    if not bool(settings.app_auth_cookie_enabled):
        return None, "disabled", ""
    try:
        from streamlit_cookies_manager import CookieManager
    except Exception as exc:
        return None, "unavailable", f"{type(exc).__name__}: {exc}"
    key = "auth_cookie_manager"
    if key not in st.session_state:
        prefix = (settings.app_name or "gs").strip().lower().replace(" ", "_")
        raw_manager = CookieManager(prefix=f"{prefix}/")
        st.session_state[key] = _EncryptedCookieStore(
            cookie_manager=raw_manager,
            password=_auth_signing_key() or "change-me-signing-key",
        )
    manager = st.session_state.get(key)
    if manager is None:
        return None, "error", "manager_none"
    try:
        if manager.ready():
            return manager, "ready", ""
        return manager, "pending", ""
    except Exception as exc:
        return None, "error", f"{type(exc).__name__}: {exc}"


def _get_cookie_auth_token() -> str:
    manager = _get_cookie_manager()
    if manager is None:
        return ""
    try:
        return str(manager.get("auth_token", "") or "").strip()
    except Exception:
        return ""


def _set_cookie_auth_token(token: str) -> bool:
    manager = _get_cookie_manager()
    if manager is None:
        return False
    try:
        manager.set("auth_token", str(token or "").strip())
        manager.save()
        return True
    except Exception:
        return False


def _clear_cookie_auth_token() -> None:
    manager = _get_cookie_manager()
    if manager is None:
        return
    try:
        manager.delete("auth_token")
        manager.save()
    except Exception:
        return


def _build_auth_remember_token(*, username: str, role: str, expires_at: int) -> str:
    payload = {
        "u": (username or "").strip(),
        "r": _normalized_role(role),
        "exp": int(expires_at),
    }
    payload_raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _urlsafe_b64_encode(payload_raw)
    sig = hmac.new(_auth_signing_key().encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _urlsafe_b64_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def _parse_auth_remember_token(token: str) -> dict | None:
    raw = (token or "").strip()
    if not raw or "." not in raw:
        return None
    payload_b64, sig_b64 = raw.split(".", 1)
    if not payload_b64 or not sig_b64:
        return None
    try:
        expected_sig = hmac.new(
            _auth_signing_key().encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        got_sig = _urlsafe_b64_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, got_sig):
            return None
        payload = json.loads(_urlsafe_b64_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        username = str(payload.get("u") or "").strip()
        role = _normalized_role(str(payload.get("r") or "viewer"))
        expires_at = int(payload.get("exp") or 0)
        if not username or expires_at <= 0:
            return None
        return {"username": username, "role": role, "exp": expires_at}
    except Exception:
        return None


def _get_query_auth_token() -> str:
    value = ""
    try:
        value = st.query_params.get("auth", "")
    except Exception:
        try:
            fallback = st.experimental_get_query_params().get("auth", [""])
            value = fallback[0] if fallback else ""
            st.session_state["auth_query_token_read_status"] = "ok_experimental"
        except Exception as exc:
            st.session_state["auth_query_token_read_status"] = "error"
            st.session_state["auth_query_token_read_error"] = f"{type(exc).__name__}: {exc}"
            return ""
    else:
        st.session_state["auth_query_token_read_status"] = "ok"
        st.session_state["auth_query_token_read_error"] = ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _set_query_auth_token(token: str) -> bool:
    raw = str(token or "").strip()
    try:
        st.query_params["auth"] = raw
        st.session_state["auth_query_token_write_status"] = "ok"
        st.session_state["auth_query_token_write_error"] = ""
        return True
    except Exception as exc:
        try:
            st.experimental_set_query_params(auth=raw)
            st.session_state["auth_query_token_write_status"] = "ok_experimental"
            st.session_state["auth_query_token_write_error"] = ""
            return True
        except Exception as fallback_exc:
            st.session_state["auth_query_token_write_status"] = "error"
            st.session_state["auth_query_token_write_error"] = (
                f"{type(exc).__name__}: {exc} | fallback={type(fallback_exc).__name__}: {fallback_exc}"
            )
            return False


def _clear_query_auth_token() -> None:
    try:
        if "auth" in st.query_params:
            del st.query_params["auth"]
        st.session_state["auth_query_token_clear_status"] = "ok"
        st.session_state["auth_query_token_clear_error"] = ""
    except Exception:
        try:
            st.experimental_set_query_params()
            st.session_state["auth_query_token_clear_status"] = "ok_experimental"
            st.session_state["auth_query_token_clear_error"] = ""
        except Exception as exc:
            st.session_state["auth_query_token_clear_status"] = "error"
            st.session_state["auth_query_token_clear_error"] = f"{type(exc).__name__}: {exc}"
            return


def _restore_auth_from_query_token(user_map: dict) -> bool:
    token = _get_query_auth_token()
    if not token:
        return False
    claims = _parse_auth_remember_token(token)
    now_ts = int(time.time())
    if not claims or int(claims.get("exp") or 0) <= now_ts:
        _clear_query_auth_token()
        return False
    row = user_map.get(str(claims.get("username") or "").strip())
    if row is None or not bool(row.is_active):
        _clear_query_auth_token()
        return False
    st.session_state["auth_username"] = row.username
    st.session_state["auth_role"] = _normalized_role(row.role)
    st.session_state["auth_authenticated"] = True
    st.session_state["auth_remember_enabled"] = True
    return True


def _restore_auth_from_cookie_token(user_map: dict) -> bool:
    token = _get_cookie_auth_token()
    if not token:
        return False
    claims = _parse_auth_remember_token(token)
    now_ts = int(time.time())
    if not claims or int(claims.get("exp") or 0) <= now_ts:
        _clear_cookie_auth_token()
        return False
    row = user_map.get(str(claims.get("username") or "").strip())
    if row is None or not bool(row.is_active):
        _clear_cookie_auth_token()
        return False
    st.session_state["auth_username"] = row.username
    st.session_state["auth_role"] = _normalized_role(row.role)
    st.session_state["auth_authenticated"] = True
    st.session_state["auth_remember_enabled"] = True
    return True


def _ensure_remember_tokens_for_authenticated_user(user_map: dict) -> None:
    if not bool(st.session_state.get("auth_authenticated")):
        return
    if not bool(st.session_state.get("auth_remember_enabled")):
        return
    username = str(st.session_state.get("auth_username") or "").strip()
    if not username:
        return
    row = user_map.get(username)
    if row is None or not bool(row.is_active):
        return
    now_ts = int(time.time())
    remember_days = max(1, int(settings.app_auth_remember_days or 14))
    min_exp = now_ts + (remember_days * 24 * 60 * 60)
    query_claims = _parse_auth_remember_token(_get_query_auth_token())
    cookie_claims = _parse_auth_remember_token(_get_cookie_auth_token())
    existing_exp = 0
    if query_claims and str(query_claims.get("username") or "").strip() == username:
        existing_exp = max(existing_exp, int(query_claims.get("exp") or 0))
    if cookie_claims and str(cookie_claims.get("username") or "").strip() == username:
        existing_exp = max(existing_exp, int(cookie_claims.get("exp") or 0))
    expires_at = max(existing_exp, min_exp)
    token = _build_auth_remember_token(username=row.username, role=row.role, expires_at=expires_at)
    if _get_query_auth_token() != token:
        _set_query_auth_token(token)
    _set_cookie_auth_token(token)


def _load_rbac_from_db() -> tuple[list, dict[str, set[str]]]:
    try:
        from sqlalchemy import select
        from app.db.models import AppUser, RolePermission
        from app.db.session import SessionLocal
    except Exception:
        return [], {}

    db = SessionLocal()
    try:
        users = db.scalars(select(AppUser).where(AppUser.is_active.is_(True)).order_by(AppUser.username.asc())).all()
        perm_rows = db.scalars(select(RolePermission).order_by(RolePermission.role.asc())).all()
        permission_map: dict[str, set[str]] = {}
        for row in perm_rows:
            permission_map.setdefault(row.role, set()).add(row.permission)
        return users, permission_map
    except Exception:
        # Tables may not exist yet during bootstrap/migrations.
        return [], {}
    finally:
        db.close()


def init_user_context_sidebar() -> UserContext:
    users, db_permission_map = _load_rbac_from_db()
    st.session_state["auth_users_count"] = len(users)
    effective_permissions = DEFAULT_PERMISSIONS.copy()
    for role, perms in db_permission_map.items():
        effective_permissions[role] = set(perms)
    st.session_state["auth_role_permissions"] = effective_permissions

    user_map = {u.username: u for u in users}
    configured_default_user = (
        ""
        if settings.app_require_password_auth
        else ((settings.app_user_name or "employee").strip() or "employee")
    )
    if "auth_username" not in st.session_state:
        st.session_state["auth_username"] = configured_default_user
    if "auth_role" not in st.session_state:
        if st.session_state["auth_username"] in user_map:
            st.session_state["auth_role"] = _normalized_role(user_map[st.session_state["auth_username"]].role)
        else:
            st.session_state["auth_role"] = _normalized_role(settings.app_user_role)

    can_override = settings.app_allow_role_override and settings.app_env != "prod"
    require_password_auth = settings.app_require_password_auth
    if "auth_authenticated" not in st.session_state:
        st.session_state["auth_authenticated"] = False
    if "auth_remember_enabled" not in st.session_state:
        st.session_state["auth_remember_enabled"] = False
    _, cookie_state, cookie_err = _cookie_manager_status()
    st.session_state["auth_cookie_manager_state"] = cookie_state
    st.session_state["auth_cookie_manager_error"] = cookie_err
    st.session_state["auth_cookie_init_attempts"] = 0
    if require_password_auth and not st.session_state.get("auth_authenticated"):
        restored = _restore_auth_from_cookie_token(user_map)
        if not restored:
            _restore_auth_from_query_token(user_map)
    if require_password_auth and st.session_state.get("auth_authenticated"):
        _ensure_remember_tokens_for_authenticated_user(user_map)
    with st.sidebar.expander(
        "Session Identity",
        expanded=bool(require_password_auth and not st.session_state.get("auth_authenticated")),
    ):
        st.caption(f"Environment: `{settings.app_env}`")
        if require_password_auth and cookie_state in {"pending", "unavailable", "error"}:
            st.caption(
                "Secure cookie session storage is currently unavailable; "
                "authentication continues using in-memory + query-token fallback."
            )
            if cookie_err:
                st.caption(f"Cookie manager detail: `{cookie_err}`")
        if users:
            options = list(user_map.keys())
            if require_password_auth:
                if "auth_login_username" not in st.session_state:
                    st.session_state["auth_login_username"] = ""
                if "auth_login_password" not in st.session_state:
                    st.session_state["auth_login_password"] = ""
                if "auth_login_remember_me" not in st.session_state:
                    st.session_state["auth_login_remember_me"] = True
                with st.form("auth_login_form"):
                    login_username = st.text_input("Username", key="auth_login_username", placeholder="Enter username")
                    login_password = st.text_input("Password", type="password", key="auth_login_password")
                    remember_me = st.checkbox("Remember me on this browser", key="auth_login_remember_me")
                    login_submit = st.form_submit_button("Sign In")
                if login_submit:
                    row = user_map.get(str(login_username or "").strip())
                    try:
                        from app.services.security import verify_password
                    except ModuleNotFoundError:
                        from services.security import verify_password
                    if row and verify_password(login_password, row.password_hash, row.password_salt):
                        st.session_state["auth_username"] = row.username
                        st.session_state["auth_role"] = _normalized_role(row.role)
                        st.session_state["auth_authenticated"] = True
                        st.session_state["auth_remember_enabled"] = bool(remember_me)
                        if remember_me:
                            remember_days = max(1, int(settings.app_auth_remember_days or 14))
                            expires_at = int(time.time()) + (remember_days * 24 * 60 * 60)
                            remember_token = _build_auth_remember_token(
                                username=row.username,
                                role=row.role,
                                expires_at=expires_at,
                            )
                            _set_cookie_auth_token(remember_token)
                            _set_query_auth_token(
                                remember_token
                            )
                        else:
                            _clear_cookie_auth_token()
                            _clear_query_auth_token()
                        st.rerun()
                    else:
                        st.session_state["auth_authenticated"] = False
                        st.error("Invalid username/password.")

                if st.session_state.get("auth_authenticated"):
                    active_username = st.session_state.get("auth_username", "")
                    active_role = st.session_state.get("auth_role", "viewer")
                    st.text_input("Active User", value=active_username, disabled=True, key="auth_active_username")
                    st.text_input("Role", value=active_role, disabled=True, key="auth_role_locked_display")
                    if st.button("Sign Out", key="auth_sign_out"):
                        st.session_state["auth_authenticated"] = False
                        st.session_state["auth_remember_enabled"] = False
                        st.session_state["auth_username"] = ""
                        st.session_state["auth_role"] = "viewer"
                        _clear_cookie_auth_token()
                        _clear_query_auth_token()
                        st.rerun()
                else:
                    st.caption("Sign in required to access app pages.")
                    st.session_state["auth_role"] = "viewer"
            else:
                current_username = st.session_state["auth_username"]
                selected_idx = options.index(current_username) if current_username in options else 0
                username = st.selectbox(
                    "Username",
                    options,
                    index=selected_idx,
                    disabled=not can_override,
                    key="auth_username_input",
                )
                role = _normalized_role(user_map[username].role)
                st.text_input("Role", value=role, disabled=True, key="auth_role_locked_display")
                if can_override:
                    st.session_state["auth_username"] = (username or "employee").strip() or "employee"
                    if st.session_state["auth_username"] in user_map:
                        st.session_state["auth_role"] = _normalized_role(user_map[st.session_state["auth_username"]].role)
                    else:
                        st.session_state["auth_role"] = _normalized_role(role)
        else:
            st.info("No app users found yet. Go to the Admin page to bootstrap the first admin user.")
            if not require_password_auth:
                username = st.text_input(
                    "Username",
                    value=st.session_state["auth_username"],
                    disabled=not can_override,
                    key="auth_username_input",
                )
                role_idx = ROLES.index(_normalized_role(st.session_state["auth_role"]))
                role = st.selectbox(
                    "Role",
                    ROLES,
                    index=role_idx,
                    disabled=not can_override,
                    key="auth_role_input",
                )
                if can_override:
                    st.session_state["auth_username"] = (username or "employee").strip() or "employee"
                    st.session_state["auth_role"] = _normalized_role(role)
            else:
                st.session_state["auth_authenticated"] = False
                st.session_state["auth_remember_enabled"] = False
                st.session_state["auth_username"] = ""
                st.session_state["auth_role"] = "viewer"
        st.caption(
            "Role capabilities include core CRUD/export plus AI permissions "
            "(`ai_chat_use`, `ai_comp_use`, `ai_coin_grade`, `ai_coin_identify`)."
        )

    return current_user()


def current_user() -> UserContext:
    return UserContext(
        username=(st.session_state.get("auth_username") or "employee").strip() or "employee",
        role=_normalized_role(st.session_state.get("auth_role") or settings.app_user_role),
    )


def has_permission(role: str, permission: str) -> bool:
    # Admin is an explicit super-role and always has full access.
    if _normalized_role(role) == "admin":
        return True
    permission_map = st.session_state.get("auth_role_permissions", DEFAULT_PERMISSIONS)
    return permission in permission_map.get(_normalized_role(role), {"read"})


def ensure_permission(user: UserContext, permission: str, action_label: str) -> bool:
    if has_permission(user.role, permission):
        return True
    st.error(f"`{action_label}` requires `{permission}` permission. Signed in as `{user.username}` ({user.role}).")
    return False


def require_authenticated_session(*, allow_bootstrap_if_no_users: bool = False) -> bool:
    if not settings.app_require_password_auth:
        return True

    users_count = int(st.session_state.get("auth_users_count") or 0)
    if users_count == 0:
        if allow_bootstrap_if_no_users:
            return True
        st.warning("Authentication is enabled, but no app users exist yet.")
        st.info("Open the Admin page and bootstrap the first admin user.")
        return False

    if st.session_state.get("auth_authenticated"):
        return True

    st.warning("Sign in required.")
    st.info("Use the sidebar `Session Identity` panel to sign in with username and password.")
    return False


def auth_debug_snapshot() -> dict[str, object]:
    cookie_manager_ready = _get_cookie_manager() is not None
    cookie_token = _get_cookie_auth_token()
    query_token = _get_query_auth_token()
    cookie_claims = _parse_auth_remember_token(cookie_token) if cookie_token else None
    query_claims = _parse_auth_remember_token(query_token) if query_token else None
    now_ts = int(time.time())
    return {
        "auth_required": bool(settings.app_require_password_auth),
        "auth_authenticated_session": bool(st.session_state.get("auth_authenticated")),
        "auth_username_session": str(st.session_state.get("auth_username") or "").strip(),
        "auth_role_session": _normalized_role(st.session_state.get("auth_role") or "viewer"),
        "auth_remember_enabled_session": bool(st.session_state.get("auth_remember_enabled")),
        "cookie_enabled": bool(settings.app_auth_cookie_enabled),
        "cookie_manager_state": str(st.session_state.get("auth_cookie_manager_state") or ""),
        "cookie_manager_error": str(st.session_state.get("auth_cookie_manager_error") or ""),
        "cookie_manager_ready": bool(cookie_manager_ready),
        "cookie_token_present": bool(cookie_token),
        "cookie_token_valid": bool(cookie_claims and int(cookie_claims.get("exp") or 0) > now_ts),
        "cookie_token_expires_at": int(cookie_claims.get("exp") or 0) if cookie_claims else 0,
        "cookie_claim_username": str(cookie_claims.get("username") or "") if cookie_claims else "",
        "query_token_present": bool(query_token),
        "query_token_valid": bool(query_claims and int(query_claims.get("exp") or 0) > now_ts),
        "query_token_expires_at": int(query_claims.get("exp") or 0) if query_claims else 0,
        "query_claim_username": str(query_claims.get("username") or "") if query_claims else "",
        "query_token_read_status": str(st.session_state.get("auth_query_token_read_status") or ""),
        "query_token_read_error": str(st.session_state.get("auth_query_token_read_error") or ""),
        "query_token_write_status": str(st.session_state.get("auth_query_token_write_status") or ""),
        "query_token_write_error": str(st.session_state.get("auth_query_token_write_error") or ""),
        "query_token_clear_status": str(st.session_state.get("auth_query_token_clear_status") or ""),
        "query_token_clear_error": str(st.session_state.get("auth_query_token_clear_error") or ""),
        "remember_days": int(settings.app_auth_remember_days or 14),
    }
