import streamlit as st


def render_active_ebay_context_banner(*, section_title: str = "Active Account Context") -> None:
    active_profile = str(st.session_state.get("ebay_workspace_active_profile") or "manual").strip() or "manual"
    active_store_profile = (
        str(st.session_state.get("ebay_workspace_active_store_profile") or "manual-store").strip()
        or "manual-store"
    )
    status_filter = list(st.session_state.get("ebay_workspace_status_filter") or ["draft", "active", "ended"])
    linked_only = bool(st.session_state.get("ebay_workspace_linked_only"))
    search_text = str(st.session_state.get("ebay_workspace_search") or "").strip()
    use_date_filter = bool(st.session_state.get("ebay_workspace_use_date_filter"))
    listed_date_range = st.session_state.get("ebay_workspace_listed_date_range")
    token_present = bool(str(st.session_state.get("ebay_workspace_access_token") or "").strip())
    listing_format = str(st.session_state.get("ebay_pub_format") or "").strip().upper() or "FIXED_PRICE"
    best_offer_enabled = bool(st.session_state.get("ebay_pub_best_offer_enabled"))
    marketplace_id = str(st.session_state.get("ebay_pub_marketplace_id") or "").strip()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Profile", active_profile)
    c2.metric("Store Profile", active_store_profile)
    c3.metric("Token Loaded", "yes" if token_present else "no")
    c4.metric("Status Filter Count", len(status_filter))
    c5.metric("Date Window", "on" if use_date_filter else "off")
    st.caption(
        f"{section_title}: "
        f"status={','.join(status_filter) or 'none'} | "
        f"linked_only={'yes' if linked_only else 'no'} | "
        f"search={'(none)' if not search_text else search_text[:80]} | "
        f"format={listing_format} | "
        f"best_offer={'on' if (listing_format == 'FIXED_PRICE' and best_offer_enabled) else 'off'} | "
        f"marketplace={marketplace_id or '(default)'}"
    )
    if use_date_filter and listed_date_range is not None:
        if isinstance(listed_date_range, tuple) and len(listed_date_range) == 2:
            st.caption(
                f"Listed date range: `{listed_date_range[0]}` to `{listed_date_range[1]}`"
            )
