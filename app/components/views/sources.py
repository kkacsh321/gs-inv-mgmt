import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError

from app.components.views.shared import render_help_panel
from app.repository import InventoryRepository

SOURCE_TYPES = ["dealer", "vendor", "auction", "estate_sale", "collector", "other"]


def render_sources(repo: InventoryRepository) -> None:
    st.subheader("Dealers / Vendors / Sources")
    st.caption("Manage common acquisition sources used by purchase lots.")
    render_help_panel(
        section_title="Sources",
        goal="Create reusable source records and use them in lot intake workflows.",
        steps=[
            "Add common dealers/vendors/sources once with contact metadata.",
            "Keep source names unique and mark inactive instead of deleting history.",
            "Use active sources in Lots page selection to standardize data.",
            "Use actor attribution when updating source records.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    with st.form("create_source_form", clear_on_submit=True):
        name = st.text_input("Source Name", placeholder="APMEX, Local Coin Shop, Estate Auction")
        source_type = st.selectbox("Source Type", SOURCE_TYPES, index=1)
        c1, c2 = st.columns(2)
        with c1:
            contact_name = st.text_input("Contact Name (Optional)")
            contact_email = st.text_input("Contact Email (Optional)")
            source_url = st.text_input("Source URL (Optional)")
            ebay_store_url = st.text_input("eBay Seller Store URL (Optional)")
            account_id = st.text_input("Account ID (Optional)")
        with c2:
            contact_phone = st.text_input("Contact Phone (Optional)")
            payment_method = st.selectbox(
                "Payment Method (Optional)",
                ["", "cash", "ach", "wire", "credit_card", "check", "paypal", "crypto", "other"],
            )
            is_active = st.checkbox("Active", value=True)
        notes = st.text_area("Notes")
        if st.form_submit_button("Create Source"):
            if not name.strip():
                st.error("Source name is required.")
            else:
                try:
                    repo.create_inventory_source(
                        name=name.strip(),
                        source_type=source_type,
                        contact_name=contact_name.strip(),
                        contact_email=contact_email.strip(),
                        contact_phone=contact_phone.strip(),
                        source_url=source_url.strip(),
                        ebay_store_url=ebay_store_url.strip(),
                        account_id=account_id.strip(),
                        payment_method=payment_method.strip(),
                        notes=notes.strip(),
                        is_active=is_active,
                    )
                    st.success("Source created.")
                except IntegrityError:
                    repo.db.rollback()
                    st.error("Source name must be unique.")

    sources = repo.list_inventory_sources(active_only=False)
    if not sources:
        st.info("No sources yet.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": s.id,
                    "name": s.name,
                    "source_type": s.source_type,
                    "contact_name": s.contact_name,
                    "contact_email": s.contact_email,
                    "contact_phone": s.contact_phone,
                    "source_url": s.source_url,
                    "ebay_store_url": s.ebay_store_url,
                    "account_id": s.account_id,
                    "payment_method": s.payment_method,
                    "is_active": s.is_active,
                    "notes": s.notes,
                }
                for s in sources
            ]
        ),
        use_container_width=True,
    )

    st.markdown("### Edit Source")
    actor = st.text_input("Actor (for audit log)", value="ops-admin", key="source_actor")
    source_map = {f"#{s.id} | {s.name}": s for s in sources}
    selected_key = st.selectbox("Select Source", list(source_map.keys()))
    selected = source_map[selected_key]

    with st.form("edit_source_form"):
        name = st.text_input("Source Name", value=selected.name)
        source_type = st.selectbox(
            "Source Type",
            SOURCE_TYPES,
            index=SOURCE_TYPES.index(selected.source_type) if selected.source_type in SOURCE_TYPES else 0,
        )
        c1, c2 = st.columns(2)
        with c1:
            contact_name = st.text_input("Contact Name (Optional)", value=selected.contact_name or "")
            contact_email = st.text_input("Contact Email (Optional)", value=selected.contact_email or "")
            source_url = st.text_input("Source URL (Optional)", value=selected.source_url or "")
            ebay_store_url = st.text_input("eBay Seller Store URL (Optional)", value=getattr(selected, "ebay_store_url", "") or "")
            account_id = st.text_input("Account ID (Optional)", value=selected.account_id or "")
        with c2:
            contact_phone = st.text_input("Contact Phone (Optional)", value=selected.contact_phone or "")
            payment_method_options = ["", "cash", "ach", "wire", "credit_card", "check", "paypal", "crypto", "other"]
            payment_method = st.selectbox(
                "Payment Method (Optional)",
                payment_method_options,
                index=payment_method_options.index(selected.payment_method)
                if (selected.payment_method or "") in payment_method_options
                else 0,
            )
            is_active = st.checkbox("Active", value=selected.is_active)
        notes = st.text_area("Notes", value=selected.notes or "")
        if st.form_submit_button("Save Source Changes"):
            try:
                repo.update_inventory_source(
                    selected.id,
                    {
                        "name": name.strip(),
                        "source_type": source_type,
                        "contact_name": contact_name.strip(),
                        "contact_email": contact_email.strip(),
                        "contact_phone": contact_phone.strip(),
                        "source_url": source_url.strip(),
                        "ebay_store_url": ebay_store_url.strip(),
                        "account_id": account_id.strip(),
                        "payment_method": payment_method.strip(),
                        "notes": notes.strip(),
                        "is_active": bool(is_active),
                    },
                    actor=actor,
                )
                st.success("Source updated.")
            except IntegrityError:
                repo.db.rollback()
                st.error("Update failed due to unique constraint or invalid data.")
