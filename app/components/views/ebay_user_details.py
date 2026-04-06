from datetime import timedelta
from typing import Any

import pandas as pd
import streamlit as st

from app.auth import current_user
from app.components.views.ebay_context import render_active_ebay_context_banner
from app.components.views.shared import render_help_panel
from app.config import settings
from app.repository import InventoryRepository
from app.services.ebay import EbayClient
from app.services.runtime_settings import get_runtime_str
from app.utils.time import utcnow_naive


def _resolved_username(claims: dict[str, Any], identity_payload: dict[str, Any]) -> str:
    from_claims = str(
        claims.get("preferred_username")
        or claims.get("username")
        or claims.get("user_name")
        or claims.get("sub")
        or ""
    ).strip()
    if from_claims:
        return from_claims
    return str(
        identity_payload.get("username")
        or identity_payload.get("userId")
        or identity_payload.get("userID")
        or identity_payload.get("individualAccount", {}).get("email")
        or ""
    ).strip()


def _persist_tokens(repo: InventoryRepository, *, actor: str, access_token: str, refresh_token: str) -> None:
    if access_token.strip():
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="ebay_user_access_token",
            value=access_token.strip(),
            value_type="str",
            description="Default eBay user access token used by verification and sync jobs.",
            actor=actor,
        )
    if refresh_token.strip():
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="ebay_user_refresh_token",
            value=refresh_token.strip(),
            value_type="str",
            description="Default eBay user refresh token used for access token renewal.",
            actor=actor,
        )


def render_ebay_user_details(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("eBay User Details")
    st.caption("Inspect current eBay user token, identity, account privileges, and refresh state.")
    render_help_panel(
        section_title="eBay User Details",
        goal="Verify active eBay user identity and seller readiness for production workflows.",
        steps=[
            "Paste or load the current eBay user access token.",
            "Run user details check to validate privileges + identity API responses.",
            "Optionally refresh the access token using refresh token and persist both tokens.",
        ],
        roadmap_phase="v1.0 Channel Operations Hardening",
    )
    render_active_ebay_context_banner(section_title="eBay User Context")

    client = EbayClient()
    if not client.is_configured():
        st.warning("eBay client credentials are not configured for this environment.")
        return

    default_access = get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token).strip()
    default_refresh = get_runtime_str(repo, "ebay_user_refresh_token", settings.ebay_user_refresh_token).strip()
    if "ebay_user_details_access_token" not in st.session_state:
        st.session_state["ebay_user_details_access_token"] = default_access
    if "ebay_user_details_refresh_token" not in st.session_state:
        st.session_state["ebay_user_details_refresh_token"] = default_refresh

    st.caption(f"Environment: `{settings.ebay_environment}`")
    access_token = st.text_area(
        "Access Token",
        key="ebay_user_details_access_token",
        height=140,
        help="Use a user OAuth access token (not app client-credentials token).",
    )
    refresh_token = st.text_area(
        "Refresh Token",
        key="ebay_user_details_refresh_token",
        height=120,
        help="Optional but recommended for automatic renewal.",
    )
    c1, c2, c3 = st.columns(3)
    fetch_details = c1.button("Fetch User Details", key="ebay_user_details_fetch_btn")
    refresh_access = c2.button("Refresh Access Token", key="ebay_user_details_refresh_btn")
    persist_tokens = c3.button("Persist Tokens", key="ebay_user_details_persist_btn")

    if persist_tokens:
        try:
            _persist_tokens(
                repo,
                actor=user.username,
                access_token=access_token,
                refresh_token=refresh_token,
            )
            st.success("Tokens persisted to runtime settings.")
        except Exception as exc:
            st.error(f"Unable to persist tokens: {exc}")

    if refresh_access:
        if not refresh_token.strip():
            st.error("Refresh token is required.")
        else:
            try:
                payload = client.refresh_user_token(refresh_token=refresh_token.strip())
                new_access = str(payload.get("access_token") or "").strip()
                new_refresh = str(payload.get("refresh_token") or "").strip() or refresh_token.strip()
                st.session_state["ebay_user_details_access_token"] = new_access
                st.session_state["ebay_user_details_refresh_token"] = new_refresh
                _persist_tokens(
                    repo,
                    actor=user.username,
                    access_token=new_access,
                    refresh_token=new_refresh,
                )
                expires_in = int(payload.get("expires_in") or 0)
                if expires_in > 0:
                    expiry_ts = utcnow_naive().replace(microsecond=0) + timedelta(seconds=max(0, expires_in - 60))
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ebay_user_access_token_expires_at",
                        value=expiry_ts.isoformat(timespec="seconds"),
                        value_type="str",
                        description="Best-effort expiry timestamp for current eBay user access token.",
                        actor=user.username,
                    )
                st.success("Access token refreshed and persisted.")
                st.json(
                    {
                        "token_type": payload.get("token_type"),
                        "expires_in": payload.get("expires_in"),
                        "scope": payload.get("scope"),
                    }
                )
            except Exception as exc:
                st.error(f"Refresh failed: {exc}")

    if fetch_details:
        if not access_token.strip():
            st.error("Paste an access token first.")
        else:
            identity_payload: dict[str, Any] = {}
            identity_error = ""
            try:
                claims = client.decode_access_token_claims(access_token.strip())
                privileges = client.get_account_privileges(access_token.strip())
                try:
                    identity_payload = client.get_identity_user(access_token.strip())
                except Exception as exc:
                    identity_error = str(exc)

                username = _resolved_username(claims, identity_payload)
                seller_registration_completed = bool(privileges.get("sellerRegistrationCompleted"))
                token_scope = str(claims.get("scope") or "").strip()

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Resolved User", username or "(unknown)")
                m2.metric("Seller Registered", "yes" if seller_registration_completed else "no")
                m3.metric("Token Scope Present", "yes" if token_scope else "no")
                m4.metric("JWT Claims Parsed", "yes" if claims else "no")

                feedback_rows = [
                    {
                        "check": "Token parse",
                        "status": "pass" if claims else "warn",
                        "details": "JWT claims parsed." if claims else "Token appears opaque/non-JWT.",
                    },
                    {
                        "check": "Privileges endpoint",
                        "status": "pass",
                        "details": "Sell Account privileges call succeeded.",
                    },
                    {
                        "check": "Identity endpoint",
                        "status": "pass" if identity_payload else ("warn" if identity_error else "info"),
                        "details": (
                            f"Resolved user={username or '(unknown)'}"
                            if identity_payload
                            else (identity_error or "Identity check not available.")
                        ),
                    },
                    {
                        "check": "Seller registration",
                        "status": "pass" if seller_registration_completed else "warn",
                        "details": "sellerRegistrationCompleted=true"
                        if seller_registration_completed
                        else "sellerRegistrationCompleted=false",
                    },
                ]
                st.dataframe(pd.DataFrame(feedback_rows), use_container_width=True, hide_index=True)
                with st.expander("Account Privileges Response", expanded=False):
                    st.json(privileges)
                with st.expander("Decoded JWT Claims", expanded=False):
                    st.json(claims or {"note": "No decodable JWT claims found."})
                with st.expander("Identity API Response", expanded=False):
                    st.json(identity_payload or {"note": identity_error or "No identity payload available."})
            except Exception as exc:
                st.error(f"User detail fetch failed: {exc}")

