from contextlib import contextmanager
from pathlib import Path
import base64
import time

import streamlit as st

try:
    from app.auth import (
        current_user,
        has_oauth_callback_query_params,
        init_user_context_sidebar,
        require_authenticated_session,
    )
    from app.config import settings
    from app.db.init_db import init_db
    from app.db.session import SessionLocal
    from app.repository import InventoryRepository
    from app.services.media_storage import MediaStorageService
except ModuleNotFoundError:
    # Fallback for script-execution contexts where package root resolution differs.
    from auth import current_user, has_oauth_callback_query_params, init_user_context_sidebar, require_authenticated_session
    from config import settings
    from db.init_db import init_db
    from db.session import SessionLocal
    from repository import InventoryRepository
    from services.media_storage import MediaStorageService


APP_CAPTION = (
    "Inventory operations for precious metals, bullion, coins, collectibles, antiques, "
    "and multi-channel resale."
)
SIDEBAR_LOGO_PATH = Path(__file__).resolve().parent / "images" / "logonewsm.jpg"
QUICK_ACTION_PAGE_MAP: dict[str, str] = {
    "home": "main.py",
    "operations": "pages/00_Operations_Home.py",
    "dashboard": "pages/01_Dashboard.py",
    "products": "pages/02_Products.py",
    "listings": "pages/03_Listings.py",
    "sales": "pages/04_Sales.py",
    "media": "pages/05_Media.py",
    "tools": "pages/06_Tools.py",
    "ebay": "pages/22_eBay_Workspace.py",
    "lots": "pages/08_Lots.py",
    "reports": "pages/09_Reports.py",
    "search": "pages/10_Search_Edit.py",
    "shipping": "pages/11_Shipping.py",
    "inventory-movements": "pages/12_Inventory_Movements.py",
    "sources": "pages/13_Sources.py",
    "orders": "pages/14_Orders.py",
    "returns": "pages/15_Returns.py",
    "documents": "pages/16_Documents.py",
    "admin": "pages/17_Admin.py",
    "sync": "pages/18_Sync.py",
    "ebay-ops": "pages/22_eBay_Workspace.py",
    "ebay-workspace": "pages/22_eBay_Workspace.py",
    "ebay-user-details": "pages/24_eBay_User_Details.py",
    "ebay-templates": "pages/25_eBay_Templates.py",
    "listing-wizard": "pages/26_Listing_Wizard.py",
    "coin-intake": "pages/20_Coin_Intake_Wizard.py",
    "inventory-intake": "pages/23_Inventory_Intake_Wizard.py",
    "ask-gs": "pages/21_Ask_GoldenStackers.py",
    "ai-accountant": "pages/28_AI_Accountant.py",
    "health": "pages/17_Admin.py",
}
QUICK_ACTION_ALIASES: dict[str, str] = {
    "h": "home",
    "op": "operations",
    "d": "dashboard",
    "p": "products",
    "l": "listings",
    "sa": "sales",
    "m": "media",
    "t": "tools",
    "e": "ebay",
    "r": "reports",
    "se": "search",
    "sh": "shipping",
    "im": "inventory-movements",
    "so": "sources",
    "o": "orders",
    "re": "returns",
    "doc": "documents",
    "a": "admin",
    "sy": "sync",
    "ops-ebay": "ebay-ops",
    "ew": "ebay-workspace",
    "eud": "ebay-user-details",
    "et": "ebay-templates",
    "lw": "listing-wizard",
    "ci": "coin-intake",
    "ii": "inventory-intake",
    "ask": "ask-gs",
    "chat": "ask-gs",
    "acct": "ai-accountant",
    "accountant": "ai-accountant",
    "he": "health",
}

ROLE_PINNED_PAGES: dict[str, list[tuple[str, str]]] = {
    "admin": [
        ("Operations Home", "pages/00_Operations_Home.py"),
        ("eBay Workspace", "pages/22_eBay_Workspace.py"),
        ("Listing Wizard", "pages/26_Listing_Wizard.py"),
        ("eBay Templates", "pages/25_eBay_Templates.py"),
        ("eBay User Details", "pages/24_eBay_User_Details.py"),
        ("Sync", "pages/18_Sync.py"),
        ("Admin", "pages/17_Admin.py"),
    ],
    "ops": [
        ("Operations Home", "pages/00_Operations_Home.py"),
        ("Listings", "pages/03_Listings.py"),
        ("Listing Wizard", "pages/26_Listing_Wizard.py"),
        ("eBay Templates", "pages/25_eBay_Templates.py"),
        ("Shipping", "pages/11_Shipping.py"),
        ("eBay Workspace", "pages/22_eBay_Workspace.py"),
        ("eBay User Details", "pages/24_eBay_User_Details.py"),
    ],
    "viewer": [
        ("Dashboard", "pages/01_Dashboard.py"),
        ("Reports", "pages/09_Reports.py"),
        ("Search & Edit", "pages/10_Search_Edit.py"),
    ],
}

ROLE_DEFAULT_PAGE: dict[str, str] = {
    "admin": "pages/22_eBay_Workspace.py",
    "ops": "pages/00_Operations_Home.py",
    "viewer": "pages/01_Dashboard.py",
}

ROLE_WORKFLOW_GROUPS: dict[str, list[tuple[str, list[tuple[str, str, str]]]]] = {
    "admin": [
        ("Intake", [("Inventory Intake Wizard", "pages/23_Inventory_Intake_Wizard.py", "workspace_inventory"), ("Products", "pages/02_Products.py", "workspace_inventory"), ("Lots", "pages/08_Lots.py", "workspace_inventory"), ("Sources", "pages/13_Sources.py", "workspace_inventory")]),
        ("Listing", [("eBay Workspace", "pages/22_eBay_Workspace.py", "workspace_ebay"), ("eBay User Details", "pages/24_eBay_User_Details.py", "workspace_ebay"), ("eBay Templates", "pages/25_eBay_Templates.py", "workspace_ebay"), ("Listing Wizard", "pages/26_Listing_Wizard.py", "workspace_ebay"), ("Listings", "pages/03_Listings.py", "workspace_ebay"), ("Media", "pages/05_Media.py", "workspace_ebay"), ("Tools", "pages/06_Tools.py", "workspace_ebay")]),
        ("Fulfillment", [("Orders", "pages/14_Orders.py", "workspace_fulfillment"), ("Shipping", "pages/11_Shipping.py", "workspace_fulfillment"), ("Returns", "pages/15_Returns.py", "workspace_fulfillment")]),
        ("Reconcile", [("Sales", "pages/04_Sales.py", "workspace_revenue"), ("Sync", "pages/18_Sync.py", "workspace_sync"), ("Documents", "pages/16_Documents.py", "workspace_revenue"), ("Reports", "pages/09_Reports.py", "workspace_revenue"), ("AI Accountant", "pages/28_AI_Accountant.py", "workspace_revenue")]),
        ("Admin", [("Operations Home", "pages/00_Operations_Home.py", ""), ("Admin", "pages/17_Admin.py", ""), ("Search & Edit", "pages/10_Search_Edit.py", ""), ("Ask GoldenStackers", "pages/21_Ask_GoldenStackers.py", "")]),
    ],
    "ops": [
        ("Intake", [("Inventory Intake Wizard", "pages/23_Inventory_Intake_Wizard.py", "workspace_inventory"), ("Products", "pages/02_Products.py", "workspace_inventory"), ("Lots", "pages/08_Lots.py", "workspace_inventory")]),
        ("Listing", [("eBay Workspace", "pages/22_eBay_Workspace.py", "workspace_ebay"), ("eBay User Details", "pages/24_eBay_User_Details.py", "workspace_ebay"), ("eBay Templates", "pages/25_eBay_Templates.py", "workspace_ebay"), ("Listing Wizard", "pages/26_Listing_Wizard.py", "workspace_ebay"), ("Listings", "pages/03_Listings.py", "workspace_ebay"), ("Media", "pages/05_Media.py", "workspace_ebay"), ("Tools", "pages/06_Tools.py", "workspace_ebay")]),
        ("Fulfillment", [("Orders", "pages/14_Orders.py", "workspace_fulfillment"), ("Shipping", "pages/11_Shipping.py", "workspace_fulfillment"), ("Returns", "pages/15_Returns.py", "workspace_fulfillment")]),
        ("Reconcile", [("Sales", "pages/04_Sales.py", "workspace_revenue"), ("Sync", "pages/18_Sync.py", "workspace_sync"), ("Reports", "pages/09_Reports.py", "workspace_revenue"), ("AI Accountant", "pages/28_AI_Accountant.py", "workspace_revenue")]),
    ],
    "viewer": [
        ("Overview", [("Dashboard", "pages/01_Dashboard.py", ""), ("Reports", "pages/09_Reports.py", "workspace_revenue"), ("Search & Edit", "pages/10_Search_Edit.py", "")]),
        ("Support", [("Ask GoldenStackers", "pages/21_Ask_GoldenStackers.py", "")]),
    ],
}


def _runtime_ui_flags() -> dict[str, object]:
    cache_key = "ux_runtime_ui_flags_cache"
    now = time.time()
    cached = st.session_state.get(cache_key)
    if isinstance(cached, dict) and float(cached.get("expires_at", 0.0)) > now:
        return dict(cached.get("flags", {}))

    flags = {
        "navigation_mode": "unified",
        "nav_telemetry_enabled": True,
        "role_default_landing_enabled": True,
        "workspace_ebay_enabled": True,
        "workspace_inventory_enabled": True,
        "workspace_fulfillment_enabled": True,
        "workspace_sync_enabled": True,
        "workspace_revenue_enabled": True,
    }
    try:
        init_db()
        db = SessionLocal()
        try:
            repo = InventoryRepository(db)
            def _read_bool_setting(key: str, default: bool = True) -> bool:
                row = repo.get_runtime_setting(
                    environment=settings.app_env,
                    key=key,
                    active_only=True,
                )
                raw = str((row.value if row else ("true" if default else "false")) or "").strip().lower()
                return raw in {"1", "true", "yes", "on"}

            nav_mode = repo.get_runtime_setting(
                environment=settings.app_env,
                key="ux_navigation_mode",
                active_only=True,
            )
            nav_mode_val = str((nav_mode.value if nav_mode else "unified") or "unified").strip().lower()
            flags["navigation_mode"] = nav_mode_val if nav_mode_val in {"unified", "legacy"} else "unified"

            nav_tel = repo.get_runtime_setting(
                environment=settings.app_env,
                key="ux_navigation_telemetry_enabled",
                active_only=True,
            )
            nav_tel_raw = str((nav_tel.value if nav_tel else "true") or "true").strip().lower()
            flags["nav_telemetry_enabled"] = nav_tel_raw in {"1", "true", "yes", "on"}

            role_default = repo.get_runtime_setting(
                environment=settings.app_env,
                key="ux_role_default_landing_enabled",
                active_only=True,
            )
            role_default_raw = str((role_default.value if role_default else "true") or "true").strip().lower()
            flags["role_default_landing_enabled"] = role_default_raw in {"1", "true", "yes", "on"}
            flags["workspace_ebay_enabled"] = _read_bool_setting("ux_workspace_ebay_enabled", True)
            flags["workspace_inventory_enabled"] = _read_bool_setting("ux_workspace_inventory_enabled", True)
            flags["workspace_fulfillment_enabled"] = _read_bool_setting("ux_workspace_fulfillment_enabled", True)
            flags["workspace_sync_enabled"] = _read_bool_setting("ux_workspace_sync_enabled", True)
            flags["workspace_revenue_enabled"] = _read_bool_setting("ux_workspace_revenue_enabled", True)
        finally:
            db.close()
    except Exception:
        pass

    st.session_state[cache_key] = {"flags": flags, "expires_at": now + 15.0}
    return flags


def _record_navigation_event(*, actor: str, action: str, payload: dict) -> None:
    try:
        init_db()
        db = SessionLocal()
        try:
            repo = InventoryRepository(db)
            repo.record_audit_event(
                entity_type="navigation",
                entity_id=None,
                action=action,
                actor=actor or "system",
                changes=payload,
            )
        finally:
            db.close()
    except Exception:
        return


def _capture_navigation_telemetry(*, username: str, role: str, page_title: str) -> None:
    flags = _runtime_ui_flags()
    if not bool(flags.get("nav_telemetry_enabled", True)):
        return

    page_key = str(page_title or "unknown").strip().lower().replace(" ", "_")
    now = time.time()

    last_page = str(st.session_state.get("ux_nav_last_page") or "").strip()
    last_ts = float(st.session_state.get("ux_nav_last_ts") or 0.0)

    last_view_ts = float(st.session_state.get(f"ux_nav_last_view_ts::{page_key}") or 0.0)
    if now - last_view_ts >= 30.0:
        _record_navigation_event(
            actor=username,
            action="page_view",
            payload={"page": page_key, "page_title": page_title, "role": role, "mode": flags.get("navigation_mode")},
        )
        st.session_state[f"ux_nav_last_view_ts::{page_key}"] = now

    if last_page and last_page != page_key:
        delta = max(0.0, now - last_ts)
        _record_navigation_event(
            actor=username,
            action="page_switch",
            payload={
                "from_page": last_page,
                "to_page": page_key,
                "seconds_since_last_page": round(delta, 2),
                "role": role,
                "mode": flags.get("navigation_mode"),
            },
        )
        st.session_state["ux_nav_switch_count"] = int(st.session_state.get("ux_nav_switch_count") or 0) + 1
        if delta < 10.0:
            st.session_state["ux_nav_bounce_count"] = int(st.session_state.get("ux_nav_bounce_count") or 0) + 1

    st.session_state["ux_nav_last_page"] = page_key
    st.session_state["ux_nav_last_ts"] = now


def _logo_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _inject_sidebar_top_logo(path: Path) -> None:
    logo_url = _logo_data_url(path)
    st.markdown(
        f"""
        <style>
        section[data-testid="stSidebar"] div[data-testid="stSidebarHeader"] {{
            background-image: url("{logo_url}");
            background-repeat: no-repeat;
            background-position: center 2.2rem;
            background-size: min(180px, 80%);
            min-height: 9.5rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _normalize_quick_action(raw: str) -> str:
    cmd = (raw or "").strip().lower()
    if cmd.startswith("go "):
        cmd = cmd[3:].strip()
    if cmd.startswith("/"):
        cmd = cmd[1:].strip()
    if cmd in QUICK_ACTION_ALIASES:
        cmd = QUICK_ACTION_ALIASES[cmd]
    return cmd


def _render_quick_actions_sidebar(user_role: str, *, nav_mode: str) -> None:
    if nav_mode == "unified":
        ui_flags = _runtime_ui_flags()
        pinned = ROLE_PINNED_PAGES.get(user_role, ROLE_PINNED_PAGES["viewer"])
        with st.sidebar.expander("Pinned Pages", expanded=False):
            for idx, (label, target) in enumerate(pinned):
                if st.button(label, key=f"pinned_page_{user_role}_{idx}", use_container_width=True):
                    try:
                        st.switch_page(target)
                    except Exception as exc:
                        st.error(f"Navigation failed for `{label}`: {exc}")
            default_target = ROLE_DEFAULT_PAGE.get(user_role, ROLE_DEFAULT_PAGE["viewer"])
            if st.button("Open Role Default", key=f"pinned_role_default_{user_role}", use_container_width=True):
                try:
                    st.switch_page(default_target)
                except Exception as exc:
                    st.error(f"Navigation failed for role default: {exc}")

        groups = ROLE_WORKFLOW_GROUPS.get(user_role, ROLE_WORKFLOW_GROUPS["viewer"])
        with st.sidebar.expander("Workflow Stages", expanded=False):
            st.caption("Grouped by daily operating flow.")
            for group_idx, (group_name, links) in enumerate(groups):
                filtered_links = []
                for label, target, gate in links:
                    if gate == "workspace_ebay" and not ui_flags.get("workspace_ebay_enabled", True):
                        continue
                    if gate == "workspace_inventory" and not ui_flags.get("workspace_inventory_enabled", True):
                        continue
                    if gate == "workspace_fulfillment" and not ui_flags.get("workspace_fulfillment_enabled", True):
                        continue
                    if gate == "workspace_sync" and not ui_flags.get("workspace_sync_enabled", True):
                        continue
                    if gate == "workspace_revenue" and not ui_flags.get("workspace_revenue_enabled", True):
                        continue
                    filtered_links.append((label, target))
                if not filtered_links:
                    continue
                st.caption(group_name)
                cols = st.columns(2)
                for link_idx, (label, target) in enumerate(filtered_links):
                    with cols[link_idx % 2]:
                        if st.button(
                            label,
                            key=f"workflow_stage_{user_role}_{group_idx}_{link_idx}",
                            use_container_width=True,
                        ):
                            try:
                                st.switch_page(target)
                            except Exception as exc:
                                st.error(f"Navigation failed for `{label}`: {exc}")

    with st.sidebar.expander("Quick Actions", expanded=False):
        with st.form("quick_actions_form"):
            command = st.text_input(
                "Command",
                value="",
                placeholder="Type command (e.g. /products, go reports, sy)",
                help="Keyboard shortcut pattern: type a command and press Enter.",
            )
            submitted = st.form_submit_button("Go")
        st.caption(
            "Shortcuts: `/p` Products, `/l` Listings, `/sa` Sales, `/sh` Shipping, "
            "`/sy` Sync, `/a` Admin, `/op` Operations, `/doc` Documents, `/ci` Coin Intake, `/ii` Inventory Intake, `/ask` Chat, `/he` Health (Admin tab), `/eud` eBay User Details."
        )
        if submitted:
            normalized = _normalize_quick_action(command)
            target = QUICK_ACTION_PAGE_MAP.get(normalized)
            if not target:
                st.error(f"Unknown quick action: `{command}`")
                return
            try:
                st.switch_page(target)
            except Exception as exc:
                st.error(f"Navigation failed for `{normalized}`: {exc}")


def setup_page(
    page_title: str,
    *,
    allow_bootstrap_if_no_users: bool = False,
    allow_oauth_callback_query: bool = False,
) -> None:
    st.set_page_config(page_title=f"{settings.app_name} | {page_title}", layout="wide")
    user = init_user_context_sidebar()
    if SIDEBAR_LOGO_PATH.exists():
        _inject_sidebar_top_logo(SIDEBAR_LOGO_PATH)
    allow_oauth = bool(allow_oauth_callback_query and has_oauth_callback_query_params())
    if not require_authenticated_session(
        allow_bootstrap_if_no_users=allow_bootstrap_if_no_users,
        allow_oauth_callback_query=allow_oauth,
    ):
        st.stop()
    ui_flags = _runtime_ui_flags()
    nav_mode = str(ui_flags.get("navigation_mode") or "unified")
    st.session_state["ux_navigation_mode"] = nav_mode
    st.session_state["ux_role_default_landing_enabled"] = bool(ui_flags.get("role_default_landing_enabled", True))
    st.sidebar.caption(f"Signed in as `{user.username}` ({user.role})")
    st.sidebar.caption(f"Navigation Mode: `{nav_mode}`")
    _render_quick_actions_sidebar(user.role, nav_mode=nav_mode)
    _capture_navigation_telemetry(username=user.username, role=user.role, page_title=page_title)
    st.title(settings.app_name)
    st.caption(APP_CAPTION)


@contextmanager
def repo_context():
    init_db()
    db = SessionLocal()
    try:
        yield InventoryRepository(db)
    finally:
        db.close()


def build_storage() -> MediaStorageService:
    storage = MediaStorageService()
    if storage.enabled:
        try:
            storage.ensure_bucket()
        except Exception as exc:
            st.sidebar.error(f"S3 bucket check failed: {exc}")
    return storage
