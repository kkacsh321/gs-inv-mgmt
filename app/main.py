from pathlib import Path
import sys

import streamlit as st

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.page_common import APP_CAPTION, ROLE_DEFAULT_PAGE, setup_page
from app.auth import current_user
from app.components.views.shared import render_help_panel

setup_page("Home")
user = current_user()

if (
    st.session_state.get("ux_navigation_mode", "unified") == "unified"
    and bool(st.session_state.get("ux_role_default_landing_enabled", True))
    and not st.session_state.get("role_default_landing_applied")
):
    st.session_state["role_default_landing_applied"] = True
    target = ROLE_DEFAULT_PAGE.get(user.role)
    if target and hasattr(st, "switch_page"):
        st.switch_page(target)

st.markdown("### Welcome")
st.write(
    "Use the Streamlit sidebar pages for Operations Home, Dashboard, Products, Listings, Sales, Shipping, Media, "
    "Inventory Movements, Sources, Orders, Returns, Documents, Admin, Tools, Lots, Reports, Search & Edit, and eBay."
)
st.caption(APP_CAPTION)
render_help_panel(
    section_title="Home",
    goal="Use Operations Home for queue triage, then detailed pages for execution.",
    steps=[
        "Start with Operations Home to prioritize listing/shipping/sync/accounting queues.",
        "Use Products and Lots to establish inventory and cost basis.",
        "Create Listings and then record Sales as transactions complete.",
        "Use Shipping queues for fulfillment operations and status updates.",
        "Run Reports for exports and accounting handoff, then use Search/Edit for corrections.",
    ],
    roadmap_phase="v0.2 to v0.3",
)

st.markdown("### Notes")
st.write("- Navigation now uses Streamlit built-in multipage support via `app/pages/`.")
st.write("- Database migrations run through the dedicated one-shot migrate service/job.")
