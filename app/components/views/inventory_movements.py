from datetime import datetime

import pandas as pd
import streamlit as st

from app.components.ui_helpers import dataframe_date_bounds
from app.components.views.shared import dataframe_to_xlsx_bytes, render_help_panel
from app.repository import InventoryRepository


def render_inventory_movements(repo: InventoryRepository) -> None:
    st.subheader("Inventory Movements")
    st.caption("Inventory quantity movement ledger with filters and export.")
    render_help_panel(
        section_title="Inventory Movements",
        goal="Trace every inventory quantity change event per SKU.",
        steps=[
            "Filter by date range, SKU/title text, movement type, and reference type.",
            "Review quantity before/after and delta to verify inventory math.",
            "Select a row to drill into movement metadata for troubleshooting.",
            "Export filtered rows to CSV/XLSX for reconciliation and audit workflows.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    all_rows = repo.list_inventory_movements(limit=10000)
    if not all_rows:
        st.info("No inventory movements yet.")
        return

    min_date, max_date = dataframe_date_bounds([m.occurred_at for m in all_rows if m.occurred_at is not None])
    c1, c2 = st.columns(2)
    with c1:
        from_date = st.date_input("From Date", value=min_date, key="inv_movements_from_date")
    with c2:
        to_date = st.date_input("To Date", value=max_date, key="inv_movements_to_date")

    type_opts = sorted({m.movement_type for m in all_rows if m.movement_type})
    ref_opts = sorted({m.reference_type for m in all_rows if m.reference_type})
    c3, c4 = st.columns(2)
    with c3:
        selected_types = st.multiselect("Movement Types", options=type_opts, default=type_opts)
    with c4:
        selected_refs = st.multiselect("Reference Types", options=ref_opts, default=ref_opts)

    text_query = st.text_input(
        "Search SKU / Title / Notes / Reference ID",
        placeholder="ex: GS-BUL-SIL, eagle, sale id, correction note",
    ).strip().lower()

    start_dt = datetime.combine(from_date, datetime.min.time())
    end_dt = datetime.combine(to_date, datetime.max.time())

    filtered = [
        m
        for m in all_rows
        if m.occurred_at is not None
        and start_dt <= m.occurred_at <= end_dt
        and (not selected_types or m.movement_type in selected_types)
        and (not selected_refs or m.reference_type in selected_refs)
        and (
            not text_query
            or text_query in (m.product.sku.lower() if m.product else "")
            or text_query in (m.product.title.lower() if m.product else "")
            or text_query in (m.notes or "").lower()
            or text_query in str(m.reference_id or "").lower()
        )
    ]

    if not filtered:
        st.info("No movements match current filters.")
        return

    movement_df = pd.DataFrame(
        [
            {
                "movement_id": m.id,
                "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None,
                "sku": m.product.sku if m.product else None,
                "title": m.product.title if m.product else None,
                "movement_type": m.movement_type,
                "quantity_delta": m.quantity_delta,
                "quantity_before": m.quantity_before,
                "quantity_after": m.quantity_after,
                "reference_type": m.reference_type,
                "reference_id": m.reference_id,
                "notes": m.notes,
            }
            for m in filtered
        ]
    )

    st.dataframe(movement_df, use_container_width=True)
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "Download Filtered CSV",
            data=movement_df.to_csv(index=False).encode("utf-8"),
            file_name=f"inventory_movements_{from_date}_{to_date}.csv",
            mime="text/csv",
        )
    with dl2:
        st.download_button(
            "Download Filtered XLSX",
            data=dataframe_to_xlsx_bytes(movement_df, "movements"),
            file_name=f"inventory_movements_{from_date}_{to_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    row_map = {f"#{m.id} | {m.movement_type} | {m.product.sku if m.product else 'no-sku'}": m for m in filtered}
    selected_key = st.selectbox("Drill-down Movement", list(row_map.keys()))
    selected = row_map[selected_key]
    st.json(
        {
            "movement_id": selected.id,
            "product_id": selected.product_id,
            "sku": selected.product.sku if selected.product else None,
            "title": selected.product.title if selected.product else None,
            "movement_type": selected.movement_type,
            "quantity_delta": selected.quantity_delta,
            "quantity_before": selected.quantity_before,
            "quantity_after": selected.quantity_after,
            "unit_cost": float(selected.unit_cost) if selected.unit_cost is not None else None,
            "reference_type": selected.reference_type,
            "reference_id": selected.reference_id,
            "notes": selected.notes,
            "occurred_at": selected.occurred_at.isoformat() if selected.occurred_at else None,
            "created_at": selected.created_at.isoformat() if selected.created_at else None,
        }
    )
