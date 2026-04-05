from datetime import datetime
import json

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import iso_or_none
from app.components.views.shared import dataframe_to_xlsx_bytes, render_ebay_push_history, render_help_panel
from app.components.views.workspace_shell import (
    normalize_status_semantic,
    render_workspace_empty_state,
    render_workspace_error_state,
    render_workspace_feedback,
    render_workspace_task_completion,
)
from app.config import settings
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.ebay import EbayClient
from app.services.integration_queue import process_due_integration_queue_jobs, process_integration_queue_job
from app.services.sync_jobs import execute_sync_job, is_sync_job_enabled
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.utils.time import utc_today, utcnow_naive

TRACKING_STATUS_OPTIONS = [
    "",
    "label_created",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "exception",
]
EXCEPTION_ACTION_OPTIONS = [
    "",
    "contact_buyer",
    "carrier_claim_opened",
    "refund_issued",
    "replacement_shipped",
    "monitoring",
    "other",
]


def _preset_label(preset) -> str:
    default = " (default)" if preset.is_default else ""
    package = f" | {preset.shipping_package_type}" if preset.shipping_package_type else ""
    return f"{preset.name}{default} | {preset.shipping_provider} | {preset.shipping_service}{package}"


def _in_queue(sale, queue_key: str) -> bool:
    status = (sale.tracking_status or "").strip()
    tracking = (sale.tracking_number or "").strip()
    if queue_key == "needs_label":
        return (not tracking) or status in {"", "label_created"}
    if queue_key == "in_transit":
        return status in {"in_transit", "out_for_delivery"}
    if queue_key == "delivered":
        return status == "delivered"
    if queue_key == "exceptions":
        return status == "exception"
    return False


def _render_queue(
    repo: InventoryRepository,
    queue_key: str,
    queue_label: str,
    default_status: str,
    actor: str,
    sales,
    presets,
) -> None:
    queue_sales = [s for s in sales if _in_queue(s, queue_key)]
    if not queue_sales:
        render_workspace_empty_state(
            title=f"Shipping Queue ({queue_label})",
            detail="No sales in this queue.",
        )
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": s.id,
                    "marketplace": s.marketplace,
                    "external_order_id": s.external_order_id,
                    "sku": s.product.sku if s.product else None,
                    "tracking_status": s.tracking_status,
                    "tracking_semantic": normalize_status_semantic(s.tracking_status),
                    "tracking_number": s.tracking_number,
                    "shipping_provider": s.shipping_provider,
                    "shipping_service": s.shipping_service,
                    "shipping_package_type": s.shipping_package_type,
                    "shipping_label_id": s.shipping_label_id,
                    "shipping_label_cost": float(s.shipping_label_cost) if s.shipping_label_cost is not None else None,
                    "shipping_label_currency": s.shipping_label_currency,
                    "shipping_label_purchased_at": iso_or_none(s.shipping_label_purchased_at),
                    "shipping_label_url": s.shipping_label_url,
                    "exception_code": s.shipping_exception_code,
                    "exception_action": s.shipping_exception_action,
                    "exception_notes": s.shipping_exception_notes,
                    "exception_resolved_at": iso_or_none(s.shipping_exception_resolved_at),
                    "shipment_exported_at": iso_or_none(s.shipment_exported_at),
                    "sold_at": iso_or_none(s.sold_at),
                    "shipped_at": iso_or_none(s.shipped_at),
                    "delivered_at": iso_or_none(s.delivered_at),
                }
                for s in queue_sales
            ]
        ),
        use_container_width=True,
    )

    sale_map = {
        f"#{s.id} | {s.marketplace} | {s.external_order_id or 'no-order-id'}": s.id for s in queue_sales
    }
    sale_by_id = {s.id: s for s in queue_sales}
    preset_options = {"None": None}
    for preset in presets:
        preset_options[_preset_label(preset)] = preset

    with st.form(f"shipping_bulk_form_{queue_key}"):
        selected_keys = st.multiselect("Select Sales", list(sale_map.keys()))
        new_status = st.selectbox(
            "Set Tracking Status",
            TRACKING_STATUS_OPTIONS,
            index=TRACKING_STATUS_OPTIONS.index(default_status),
            key=f"new_status_{queue_key}",
        )
        preset_key = st.selectbox(
            "Apply Shipping Preset (Optional)",
            list(preset_options.keys()),
            key=f"preset_{queue_key}",
            help="Preset fills provider/service/package type for all selected sales.",
        )
        c1, c2 = st.columns(2)
        with c1:
            shipping_provider = st.text_input("Set Shipping Provider (Optional)", key=f"provider_{queue_key}")
            shipping_service = st.text_input("Set Shipping Service (Optional)", key=f"service_{queue_key}")
            shipping_package_type = st.text_input("Set Package Type (Optional)", key=f"package_{queue_key}")
        with c2:
            tracking_number = st.text_input("Set Tracking Number (Optional)", key=f"tracking_{queue_key}")
            set_shipped = st.checkbox("Update Shipped Date", value=False, key=f"set_shipped_{queue_key}")
            shipped_date = st.date_input(
                "Shipped Date",
                value=utc_today(),
                disabled=not set_shipped,
                key=f"shipped_date_{queue_key}",
            )
            set_delivered = st.checkbox("Update Delivered Date", value=False, key=f"set_delivered_{queue_key}")
            delivered_date = st.date_input(
                "Delivered Date",
                value=utc_today(),
                disabled=not set_delivered,
                key=f"delivered_date_{queue_key}",
            )
        if queue_key == "exceptions":
            st.markdown("**Exception Actions**")
            ex1, ex2 = st.columns(2)
            with ex1:
                exception_code = st.text_input(
                    "Exception Code (Optional)",
                    key=f"exception_code_{queue_key}",
                    help="Examples: address_issue, damaged, lost, return_to_sender.",
                )
                exception_action = st.selectbox(
                    "Exception Action (Optional)",
                    EXCEPTION_ACTION_OPTIONS,
                    key=f"exception_action_{queue_key}",
                )
            with ex2:
                exception_notes = st.text_area(
                    "Exception Notes (Optional)",
                    key=f"exception_notes_{queue_key}",
                    help="Capture next step, communication details, and owner.",
                )
                resolve_exception = st.checkbox(
                    "Mark Exception Resolved",
                    value=False,
                    key=f"resolve_exception_{queue_key}",
                    help="Sets `shipping_exception_resolved_at/by` and clears active exception fields.",
                )
        submit = st.form_submit_button("Apply Bulk Update")

    if submit:
        if not selected_keys:
            render_workspace_error_state(
                title=f"Shipping Queue ({queue_label})",
                detail="Select at least one sale.",
            )
            return
        selected_preset = preset_options[preset_key]
        updated = 0
        errors: list[str] = []
        for selected_key in selected_keys:
            sale_id = sale_map[selected_key]
            updates: dict = {"tracking_status": new_status}
            if selected_preset is not None:
                updates["shipping_provider"] = selected_preset.shipping_provider
                updates["shipping_service"] = selected_preset.shipping_service
                updates["shipping_package_type"] = selected_preset.shipping_package_type
            if shipping_provider.strip():
                updates["shipping_provider"] = shipping_provider.strip()
            if shipping_service.strip():
                updates["shipping_service"] = shipping_service.strip()
            if shipping_package_type.strip():
                updates["shipping_package_type"] = shipping_package_type.strip()
            if tracking_number.strip():
                updates["tracking_number"] = tracking_number.strip()
            if set_shipped:
                updates["shipped_at"] = datetime.combine(shipped_date, datetime.min.time())
            if set_delivered:
                updates["delivered_at"] = datetime.combine(delivered_date, datetime.min.time())
            if queue_key == "exceptions":
                if exception_code.strip():
                    updates["shipping_exception_code"] = exception_code.strip()
                if exception_action.strip():
                    updates["shipping_exception_action"] = exception_action.strip()
                if exception_notes.strip():
                    existing_notes = (sale_by_id.get(sale_id).shipping_exception_notes or "").strip()
                    stamp = utcnow_naive().isoformat(timespec="seconds")
                    appended = f"[{stamp}] {exception_notes.strip()}"
                    updates["shipping_exception_notes"] = (
                        f"{existing_notes}\n{appended}".strip() if existing_notes else appended
                    )
                if resolve_exception:
                    updates["shipping_exception_resolved_at"] = utcnow_naive()
                    updates["shipping_exception_resolved_by"] = actor.strip() or "shipping-ops"
                    updates["shipping_exception_code"] = ""
                    updates["shipping_exception_action"] = ""
            elif new_status != "exception":
                updates["shipping_exception_resolved_at"] = utcnow_naive()
                updates["shipping_exception_resolved_by"] = actor.strip() or "shipping-ops"
                updates["shipping_exception_code"] = ""
                updates["shipping_exception_action"] = ""
            try:
                repo.update_sale(sale_id, updates, actor=actor)
                updated += 1
            except ValueError as exc:
                errors.append(f"Sale #{sale_id}: {exc}")
        if updated:
            st.success(f"Updated {updated} sale(s).")
        for err in errors:
            st.error(err)


def _render_shipping_presets(repo: InventoryRepository, actor: str, user) -> None:
    st.markdown("### Carrier Presets")
    st.caption("Save reusable carrier/service/package combinations for bulk operations.")
    can_manage = ensure_permission(user, "manage_settings", "Manage Shipping Presets")

    with st.form("create_shipping_preset_form", clear_on_submit=True):
        p1, p2, p3, p4 = st.columns(4)
        with p1:
            name = st.text_input("Preset Name")
        with p2:
            shipping_provider = st.selectbox(
                "Provider",
                ["ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"],
                key="preset_provider",
            )
        with p3:
            shipping_service = st.text_input("Service", placeholder="Ground Advantage")
        with p4:
            shipping_package_type = st.text_input("Package Type", placeholder="small_box")
        c1, c2 = st.columns(2)
        with c1:
            is_default = st.checkbox("Default Preset", value=False)
        with c2:
            is_active = st.checkbox("Active", value=True)
        notes = st.text_area("Notes", placeholder="When to use this preset.")
        create_submit = st.form_submit_button("Create Preset", disabled=not can_manage)

    if create_submit:
        if not name.strip():
            st.error("Preset name is required.")
        elif not shipping_service.strip():
            st.error("Shipping service is required.")
        else:
            repo.create_shipping_preset(
                name=name.strip(),
                shipping_provider=shipping_provider.strip(),
                shipping_service=shipping_service.strip(),
                shipping_package_type=shipping_package_type.strip(),
                notes=notes.strip(),
                is_default=is_default,
                is_active=is_active,
                actor=actor,
            )
            st.success("Shipping preset created.")

    presets = repo.list_shipping_presets(active_only=False)
    if not presets:
        render_workspace_empty_state(
            title="Carrier Presets",
            detail="No shipping presets yet.",
        )
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": p.id,
                    "name": p.name,
                    "shipping_provider": p.shipping_provider,
                    "shipping_service": p.shipping_service,
                    "shipping_package_type": p.shipping_package_type,
                    "is_default": p.is_default,
                    "is_active": p.is_active,
                    "notes": p.notes,
                }
                for p in presets
            ]
        ),
        use_container_width=True,
    )

    preset_map = {f"#{p.id} | {p.name}": p for p in presets}
    selected_key = st.selectbox("Edit Preset", list(preset_map.keys()), key="edit_preset_key")
    selected = preset_map[selected_key]
    with st.form("edit_shipping_preset_form"):
        n1, n2, n3, n4 = st.columns(4)
        with n1:
            new_name = st.text_input("Preset Name", value=selected.name)
        with n2:
            new_provider = st.selectbox(
                "Provider",
                ["ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"],
                index=max(
                    0,
                    ["ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"].index(
                        selected.shipping_provider
                    )
                    if selected.shipping_provider in ["ebay_shipping", "pirateship", "usps", "ups", "fedex", "other"]
                    else 0,
                ),
                key="edit_preset_provider",
            )
        with n3:
            new_service = st.text_input("Service", value=selected.shipping_service)
        with n4:
            new_package_type = st.text_input("Package Type", value=selected.shipping_package_type or "")
        c1, c2 = st.columns(2)
        with c1:
            new_default = st.checkbox("Default Preset", value=selected.is_default)
        with c2:
            new_active = st.checkbox("Active", value=selected.is_active)
        new_notes = st.text_area("Notes", value=selected.notes or "")
        update_submit = st.form_submit_button("Save Preset")

    if update_submit:
        if not can_manage:
            st.error("Only admin can update shipping presets.")
            return
        repo.update_shipping_preset(
            selected.id,
            {
                "name": new_name.strip(),
                "shipping_provider": new_provider.strip(),
                "shipping_service": new_service.strip(),
                "shipping_package_type": new_package_type.strip(),
                "is_default": new_default,
                "is_active": new_active,
                "notes": new_notes.strip(),
            },
            actor=actor,
        )
        st.success("Preset updated.")


def _export_rows_for_format(selected_sales, fmt: str) -> pd.DataFrame:
    if fmt == "pirateship_upload":
        return pd.DataFrame(
            [
                {
                    "Order Number": s.external_order_id or f"SALE-{s.id}",
                    "SKU": s.product.sku if s.product else "",
                    "Item": s.product.title if s.product else "",
                    "Marketplace": s.marketplace,
                    "Provider": s.shipping_provider,
                    "Service": s.shipping_service,
                    "Package Type": s.shipping_package_type,
                    "Weight Oz": float(s.product.package_weight_oz) if s.product and s.product.package_weight_oz else "",
                    "Length In": float(s.product.package_length_in) if s.product and s.product.package_length_in else "",
                    "Width In": float(s.product.package_width_in) if s.product and s.product.package_width_in else "",
                    "Height In": float(s.product.package_height_in) if s.product and s.product.package_height_in else "",
                    "Tracking Number": s.tracking_number,
                    "Label ID": s.shipping_label_id or "",
                    "Label Cost": float(s.shipping_label_cost) if s.shipping_label_cost is not None else "",
                    "Label Currency": s.shipping_label_currency or "",
                    "Label URL": s.shipping_label_url or "",
                }
                for s in selected_sales
            ]
        )

    return pd.DataFrame(
        [
            {
                "sale_id": s.id,
                "marketplace": s.marketplace,
                "external_order_id": s.external_order_id,
                "sku": s.product.sku if s.product else None,
                "product_title": s.product.title if s.product else None,
                "quantity_sold": s.quantity_sold,
                "shipping_provider": s.shipping_provider,
                "shipping_service": s.shipping_service,
                "shipping_package_type": s.shipping_package_type,
                "tracking_number": s.tracking_number,
                "tracking_status": s.tracking_status,
                "shipping_label_id": s.shipping_label_id,
                "shipping_label_cost": float(s.shipping_label_cost) if s.shipping_label_cost is not None else None,
                "shipping_label_currency": s.shipping_label_currency,
                "shipping_label_purchased_at": iso_or_none(s.shipping_label_purchased_at),
                "shipping_label_url": s.shipping_label_url,
                "package_weight_oz": float(s.product.package_weight_oz) if s.product and s.product.package_weight_oz else None,
                "package_length_in": float(s.product.package_length_in) if s.product and s.product.package_length_in else None,
                "package_width_in": float(s.product.package_width_in) if s.product and s.product.package_width_in else None,
                "package_height_in": float(s.product.package_height_in) if s.product and s.product.package_height_in else None,
                "shipped_at": iso_or_none(s.shipped_at),
                "shipment_exported_at": iso_or_none(s.shipment_exported_at),
            }
            for s in selected_sales
        ]
    )


def _render_shipment_export(repo: InventoryRepository, actor: str, sales) -> None:
    st.markdown("### Shipment Export Builder")
    st.caption("Build CSV/XLSX shipment batches and mark records as exported.")
    if not sales:
        render_workspace_empty_state(
            title="Shipment Export Builder",
            detail="No sales records available for export.",
        )
        return

    status_opts = sorted({(s.tracking_status or "").strip() for s in sales})
    provider_opts = sorted({(s.shipping_provider or "").strip() for s in sales if (s.shipping_provider or "").strip()})
    c1, c2, c3 = st.columns(3)
    with c1:
        selected_statuses = st.multiselect("Tracking Status Filter", status_opts, default=status_opts)
    with c2:
        selected_providers = st.multiselect("Provider Filter", provider_opts, default=provider_opts)
    with c3:
        export_format = st.selectbox(
            "Export Format",
            ["carrier_generic", "pirateship_upload"],
            format_func=lambda x: "Carrier Generic" if x == "carrier_generic" else "Pirate Ship Upload Template",
        )

    filtered_sales = [
        s
        for s in sales
        if (not selected_statuses or (s.tracking_status or "").strip() in selected_statuses)
        and (not selected_providers or (s.shipping_provider or "").strip() in selected_providers)
    ]
    if not filtered_sales:
        render_workspace_empty_state(
            title="Shipment Export Builder",
            detail="No sales match export filters.",
        )
        return

    sale_map = {
        f"#{s.id} | {s.marketplace} | {s.external_order_id or 'no-order-id'} | {s.tracking_status or 'no-status'}": s
        for s in filtered_sales
    }
    selected_keys = st.multiselect("Select Sales for Export", list(sale_map.keys()))
    if not selected_keys:
        st.caption("Select one or more sales to generate export files.")
        return

    selected_sales = [sale_map[key] for key in selected_keys]
    export_df = _export_rows_for_format(selected_sales, export_format)
    st.dataframe(export_df, use_container_width=True)

    from_date = min((s.sold_at.date() for s in selected_sales if s.sold_at is not None), default=utc_today())
    to_date = max((s.sold_at.date() for s in selected_sales if s.sold_at is not None), default=utc_today())
    prefix = "pirateship_upload" if export_format == "pirateship_upload" else "carrier_shipments"
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            label="Download Export CSV",
            data=export_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{prefix}_{from_date}_{to_date}.csv",
            mime="text/csv",
        )
    with d2:
        st.download_button(
            label="Download Export XLSX",
            data=dataframe_to_xlsx_bytes(export_df, sheet_name=prefix[:31]),
            file_name=f"{prefix}_{from_date}_{to_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if st.button("Mark Selected as Exported", key="mark_shipments_exported"):
        count = repo.mark_shipments_exported([s.id for s in selected_sales], actor=actor)
        st.success(f"Marked {count} shipment(s) as exported.")


def _render_label_purchase_queue(repo: InventoryRepository, actor: str, sales, presets, user) -> None:
    st.markdown("### Label Purchase Queue")
    st.caption(
        "Queue label-purchase jobs for shipping providers (eBay Shipping/Pirate Ship). "
        "Current implementation is a safe scaffold (no live provider purchase)."
    )
    queue_enabled = get_runtime_bool(repo, "shipping_queue_enabled", True)
    purchase_enabled = get_runtime_bool(repo, "shipping_label_purchase_enabled", True)
    if not queue_enabled:
        st.warning("Shipping queue is disabled by runtime setting (`shipping_queue_enabled=false`).")
    if not purchase_enabled:
        st.warning("Shipping label purchase is disabled by runtime setting (`shipping_label_purchase_enabled=false`).")
    if not ensure_permission(user, "create", "Queue Label Purchase Jobs"):
        return

    needs_label_sales = [s for s in sales if _in_queue(s, "needs_label")]
    if not needs_label_sales:
        render_workspace_empty_state(
            title="Label Purchase Queue",
            detail="No sales currently in needs-label queue.",
        )
    else:
        preset_options = {"None": None}
        for preset in presets:
            preset_options[_preset_label(preset)] = preset
        sale_map = {
            f"#{s.id} | {s.marketplace} | {s.external_order_id or 'no-order-id'} | {s.product.sku if s.product else 'no-sku'}": s
            for s in needs_label_sales
        }
        with st.form("shipping_queue_label_purchase_form"):
            selected_sale_keys = st.multiselect(
                "Select Sales To Queue For Label Purchase",
                options=list(sale_map.keys()),
            )
            l1, l2, l3 = st.columns(3)
            with l1:
                provider = st.selectbox(
                    "Provider",
                    ["pirateship", "ebay_shipping", "usps", "ups", "fedex", "other"],
                    index=0,
                    key="shipping_label_provider",
                )
            with l2:
                service = st.text_input("Service", value="Ground Advantage", key="shipping_label_service")
            with l3:
                package_type = st.text_input("Package Type", value="small_box", key="shipping_label_package")
            preset_key = st.selectbox(
                "Apply Preset (Optional)",
                options=list(preset_options.keys()),
                key="shipping_label_preset",
            )
            t1, t2, t3 = st.columns(3)
            with t1:
                tracking_number = st.text_input(
                    "Tracking Number (Optional)",
                    value="",
                    key="shipping_label_tracking",
                    help="Optional: if provided, scaffold will store this on sale.",
                )
            with t2:
                label_cost = st.number_input(
                    "Label Cost (Optional)",
                    min_value=0.0,
                    step=0.01,
                    value=0.0,
                    key="shipping_label_cost",
                    help="Optional: set to > 0 to store label purchase cost.",
                )
                max_retries = st.number_input(
                    "Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(get_runtime_int(repo, "shipping_queue_max_retries", 5)))),
                    key="shipping_label_max_retries",
                )
            with t3:
                label_currency = st.text_input(
                    "Label Currency",
                    value="USD",
                    key="shipping_label_currency",
                    help="Optional: defaults to USD.",
                )
                dry_run = st.checkbox(
                    "Dry Run",
                    value=True,
                    key="shipping_label_dry_run",
                    help="When enabled, job executes without updating sale fields.",
                )
            queue_submit = st.form_submit_button("Queue Label Purchase Jobs")

        if queue_submit:
            if not queue_enabled or not purchase_enabled:
                st.error("Shipping queue/label purchase is disabled in runtime settings.")
                return
            if not selected_sale_keys:
                st.error("Select at least one sale.")
            else:
                selected_preset = preset_options.get(preset_key)
                effective_provider = (
                    str(getattr(selected_preset, "shipping_provider", "") or "").strip() or provider.strip()
                )
                effective_service = (
                    str(getattr(selected_preset, "shipping_service", "") or "").strip() or service.strip()
                )
                effective_package = (
                    str(getattr(selected_preset, "shipping_package_type", "") or "").strip() or package_type.strip()
                )
                created = 0
                for sale_key in selected_sale_keys:
                    sale = sale_map[sale_key]
                    payload = {
                        "sale_id": int(sale.id),
                        "shipping_provider": effective_provider,
                        "shipping_service": effective_service,
                        "shipping_package_type": effective_package,
                        "tracking_number": tracking_number.strip(),
                        "shipping_label_cost": float(label_cost) if float(label_cost) > 0 else None,
                        "shipping_label_currency": (label_currency or "USD").strip() or "USD",
                        "dry_run": bool(dry_run),
                    }
                    repo.create_integration_queue_job(
                        environment=settings.app_env,
                        integration="shipping",
                        action="purchase_label",
                        payload_json=json.dumps(payload),
                        requested_by=actor,
                        max_retries=int(max_retries),
                        actor=actor,
                    )
                    created += 1
                st.success(f"Queued {created} shipping label job(s).")
                st.rerun()

    st.markdown("#### Shipping Queue Jobs")
    queue_jobs = repo.list_integration_queue_jobs(
        environment=settings.app_env,
        integration="shipping",
        statuses={"queued", "running", "failed", "success"},
        limit=200,
    )
    if not queue_jobs:
        st.caption("No shipping queue jobs yet.")
        return

    job_rows = []
    for job in queue_jobs:
        payload = {}
        try:
            parsed = json.loads(str(job.payload_json or "{}"))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
        job_rows.append(
            {
                "job_id": job.id,
                "status": job.status,
                "action": job.action,
                "sale_id": payload.get("sale_id"),
                "provider": payload.get("shipping_provider"),
                "service": payload.get("shipping_service"),
                "package": payload.get("shipping_package_type"),
                "label_cost": payload.get("shipping_label_cost"),
                "label_currency": payload.get("shipping_label_currency"),
                "dry_run": bool(payload.get("dry_run", False)),
                "retry_count": int(job.retry_count or 0),
                "max_retries": int(job.max_retries or 0),
                "next_attempt_at": iso_or_none(job.next_attempt_at),
                "last_error": str(job.last_error or "")[:240],
                "requested_by": job.requested_by,
                "updated_by": job.updated_by,
                "created_at": iso_or_none(job.created_at),
                "updated_at": iso_or_none(job.updated_at),
            }
        )
    jobs_df = pd.DataFrame(job_rows)
    st.dataframe(jobs_df, use_container_width=True, hide_index=True)

    q1, q2 = st.columns(2)
    with q1:
        process_limit = st.number_input(
            "Process Due Limit",
            min_value=1,
            max_value=100,
            value=10,
            key="shipping_queue_process_limit",
        )
        if st.button("Process Due Shipping Jobs", key="shipping_queue_process_due_btn"):
            if not queue_enabled:
                st.error("Shipping queue is disabled by runtime setting.")
                return
            result = process_due_integration_queue_jobs(
                repo,
                integration="shipping",
                actor=actor,
                limit=int(process_limit),
            )
            st.success(
                f"Processed={result.get('processed', 0)}, success={result.get('success', 0)}, "
                f"queued={result.get('queued', 0)}, failed={result.get('failed', 0)}."
            )
            st.rerun()
    with q2:
        queued_job_opts = [int(row["job_id"]) for row in job_rows if str(row.get("status") or "") in {"queued", "failed"}]
        selected_job_ids = st.multiselect(
            "Run Selected Jobs Now",
            options=queued_job_opts,
            key="shipping_queue_run_selected_ids",
        )
        if st.button("Run Selected Shipping Jobs", key="shipping_queue_run_selected_btn"):
            if not queue_enabled:
                st.error("Shipping queue is disabled by runtime setting.")
                return
            if not selected_job_ids:
                st.error("Select one or more queued/failed jobs.")
            else:
                ok_count = 0
                fail_count = 0
                for job_id in selected_job_ids:
                    ok, _ = process_integration_queue_job(repo, job_id=int(job_id), actor=actor)
                    if ok:
                        ok_count += 1
                    else:
                        fail_count += 1
                st.success(f"Run complete. success={ok_count}, failed={fail_count}.")
                st.rerun()


def _render_ebay_tracking_push(repo: InventoryRepository, actor: str, sales, user) -> None:
    st.markdown("### eBay Tracking Push")
    st.caption("Push local tracking numbers/shipped date to eBay order fulfillment and log sync telemetry.")
    push_job_enabled = is_sync_job_enabled("ebay_shipping_tracking_push", repo=repo)
    client = EbayClient()
    if not client.is_configured():
        render_workspace_empty_state(
            title="eBay Tracking Push",
            detail="eBay credentials are not configured.",
        )
        return

    ebay_sales = [
        s
        for s in sales
        if (s.marketplace or "").strip().lower() == "ebay"
        and (s.external_order_id or "").strip()
        and (s.tracking_number or "").strip()
    ]
    if not ebay_sales:
        render_workspace_empty_state(
            title="eBay Tracking Push",
            detail="No eBay sales with both external order ID and tracking number are ready for push.",
        )
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "sale_id": s.id,
                    "external_order_id": s.external_order_id,
                    "tracking_number": s.tracking_number,
                    "tracking_status": s.tracking_status,
                    "shipping_provider": s.shipping_provider,
                    "shipped_at": iso_or_none(s.shipped_at),
                }
                for s in ebay_sales
            ]
        ),
        use_container_width=True,
    )

    sale_map = {
        f"#{s.id} | order={s.external_order_id} | tracking={s.tracking_number}": s.id
        for s in ebay_sales
    }
    selected_keys = st.multiselect("Select eBay Sales To Push", options=list(sale_map.keys()), key="shipping_ebay_push_keys")
    token = st.text_area(
        "eBay User Access Token",
        value=get_runtime_str(repo, "ebay_user_access_token", settings.ebay_user_access_token),
        height=100,
        key="shipping_ebay_push_token",
    ).strip()
    if not push_job_enabled:
        st.warning("`ebay_shipping_tracking_push` is disabled by configuration.")
    push_submit = st.button(
        "Push Tracking To eBay",
        key="shipping_push_ebay_button",
        disabled=not push_job_enabled,
        help="Enable `SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED=true` to run this job.",
    )

    if push_submit:
        if not ensure_permission(user, "create", "Push eBay Tracking Updates"):
            return
        if not push_job_enabled:
            st.error("`ebay_shipping_tracking_push` is disabled by configuration.")
            return
        if not selected_keys:
            st.error("Select at least one sale.")
            return
        if not token:
            st.error("Access token is required.")
            return
        selected_ids = [sale_map[key] for key in selected_keys]
        try:
            result = execute_sync_job(
                repo,
                job_name="ebay_shipping_tracking_push",
                access_token=token,
                actor=actor,
                sale_ids=selected_ids,
            )
            st.success(
                f"eBay tracking push run #{result['run_id']} status `{result['status']}`. "
                f"processed={result['processed']}, updated={result['updated']}, failed={result['failed']}."
            )
        except Exception as exc:
            st.error(f"Tracking push failed: {exc}")


def _render_shipping_copilot(repo: InventoryRepository, sales, user) -> None:
    st.markdown("### Shipping Copilot")
    st.caption("AI queue prioritization + exception-resolution guidance from current shipping data.")
    if st.button("Analyze Shipping Queue", key="shipping_copilot_analyze_btn"):
        if not ensure_permission(user, "ai_comp_use", "Use Shipping Copilot"):
            return
        try:
            needs_label = [s for s in sales if _in_queue(s, "needs_label")]
            in_transit = [s for s in sales if _in_queue(s, "in_transit")]
            delivered = [s for s in sales if _in_queue(s, "delivered")]
            exceptions = [s for s in sales if _in_queue(s, "exceptions")]
            context = {
                "totals": {
                    "sales": len(sales),
                    "needs_label": len(needs_label),
                    "in_transit": len(in_transit),
                    "delivered": len(delivered),
                    "exceptions": len(exceptions),
                },
                "exceptions_sample": [
                    {
                        "sale_id": int(s.id),
                        "marketplace": str(s.marketplace or ""),
                        "external_order_id": str(s.external_order_id or ""),
                        "tracking_status": str(s.tracking_status or ""),
                        "exception_code": str(s.shipping_exception_code or ""),
                        "exception_action": str(s.shipping_exception_action or ""),
                        "exception_notes": str(s.shipping_exception_notes or "")[:240],
                        "tracking_number": str(s.tracking_number or ""),
                    }
                    for s in exceptions[:25]
                ],
                "unshipped_sample": [
                    {
                        "sale_id": int(s.id),
                        "marketplace": str(s.marketplace or ""),
                        "external_order_id": str(s.external_order_id or ""),
                        "tracking_status": str(s.tracking_status or ""),
                        "tracking_number": str(s.tracking_number or ""),
                        "sold_at": iso_or_none(s.sold_at),
                    }
                    for s in needs_label[:25]
                ],
            }
            system_message = get_runtime_str(
                repo,
                "comp_llm_system_message",
                "You are an operations copilot for marketplace reselling workflows.",
            ).strip()
            instruction = (
                "Return ONLY JSON with keys: `queue_priority_plan`, `exception_resolution_plan`, "
                "`operational_risks`, `recommended_bulk_actions`. "
                "Each value must be an array of short bullet strings prioritized from highest to lowest impact."
            )
            result = execute_comp_summary(
                repo,
                query="Shipping queue triage and exception resolution plan",
                ebay_rows=[],
                web_rows=[],
                spot_context=context,
                system_message=system_message,
                instruction=instruction,
            )
            st.session_state["shipping_copilot_raw"] = str(result.text or "").strip()
            st.success("Shipping copilot analysis complete.")
            st.rerun()
        except Exception as exc:
            st.error(f"Shipping copilot analysis failed: {exc}")

    raw_val = str(st.session_state.get("shipping_copilot_raw") or "").strip()
    if raw_val:
        with st.expander("Shipping Copilot Result", expanded=False):
            st.code(raw_val, language="json")


def render_shipping(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Shipping")
    st.caption("Operational shipping queues, presets, and shipment exports.")
    render_help_panel(
        section_title="Shipping",
        goal="Work fulfillment queues quickly and keep tracking status accurate.",
        steps=[
            "Use queue tabs to prioritize needs-label, in-transit, delivered, and exception work.",
            "Use carrier presets to apply provider/service/package defaults in one action.",
            "Bulk-select sales and apply status, tracking, exception actions, and shipping metadata.",
            "Set shipped/delivered dates when milestones are reached for reporting accuracy.",
            "Generate shipment export files and mark records as exported to track outbound batches.",
            "Use actor field so updates are traceable in audit history.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )
    actor = user.username
    st.caption(f"Signed in as `{user.username}` ({user.role}). Shipping updates are attributed to this identity.")
    sales_all = repo.list_sales()
    if not sales_all:
        render_workspace_empty_state(
            title="Shipping",
            detail="No sales records yet.",
        )
        return
    marketplace_options = ["all"] + sorted(
        {
            str(s.marketplace or "").strip().lower()
            for s in sales_all
            if str(s.marketplace or "").strip()
        }
    )
    if "shipping_focus_marketplace" not in st.session_state:
        st.session_state["shipping_focus_marketplace"] = "all"
    if str(st.session_state.get("shipping_focus_marketplace") or "all") not in marketplace_options:
        st.session_state["shipping_focus_marketplace"] = "all"
    handoff_active = (
        str(st.session_state.get("workspace_handoff_from") or "").strip().lower() == "ebay_workspace"
        and str(st.session_state.get("workspace_handoff_target") or "").strip().lower() == "shipping"
    )
    if handoff_active:
        h1, h2 = st.columns([4, 1])
        with h1:
            st.info("Opened from eBay Workspace context. Marketplace focus was preloaded for eBay fulfillment.")
        with h2:
            if st.button("Clear Handoff", key="shipping_clear_handoff_btn", use_container_width=True):
                try:
                    repo.record_audit_event(
                        entity_type="navigation",
                        entity_id=None,
                        action="workspace_handoff_cleared",
                        actor=user.username,
                        changes={
                            "from": "ebay_workspace",
                            "target": "shipping",
                            "cleared_marketplace_focus": st.session_state.get("shipping_focus_marketplace") or "all",
                        },
                    )
                except Exception:
                    pass
                st.session_state["shipping_focus_marketplace"] = "all"
                st.session_state["workspace_handoff_from"] = ""
                st.session_state["workspace_handoff_target"] = ""
                st.rerun()
    selected_marketplace = st.selectbox(
        "Marketplace Focus",
        options=marketplace_options,
        key="shipping_focus_marketplace",
        help="Filter queues and exports to one marketplace when needed.",
    )
    sales = (
        sales_all
        if selected_marketplace == "all"
        else [s for s in sales_all if str(s.marketplace or "").strip().lower() == selected_marketplace]
    )
    if selected_marketplace != "all":
        st.caption(f"Showing shipping workflow for marketplace: `{selected_marketplace}`")
    if not sales:
        render_workspace_empty_state(
            title="Shipping",
            detail="No sales match the selected marketplace filter.",
        )
        return
    if not ensure_permission(user, "bulk_update", "Shipping Bulk Operations"):
        render_workspace_empty_state(
            title="Shipping Access",
            detail="Read-only access: shipping queue data is visible, but updates are disabled.",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": s.id,
                        "marketplace": s.marketplace,
                        "external_order_id": s.external_order_id,
                        "tracking_status": s.tracking_status,
                        "tracking_number": s.tracking_number,
                        "shipping_provider": s.shipping_provider,
                        "shipping_service": s.shipping_service,
                        "shipping_package_type": s.shipping_package_type,
                        "shipping_label_id": s.shipping_label_id,
                        "shipping_label_cost": float(s.shipping_label_cost) if s.shipping_label_cost is not None else None,
                        "shipping_label_currency": s.shipping_label_currency,
                        "shipping_label_purchased_at": iso_or_none(s.shipping_label_purchased_at),
                        "shipping_label_url": s.shipping_label_url,
                        "sold_at": iso_or_none(s.sold_at),
                        "shipped_at": iso_or_none(s.shipped_at),
                        "delivered_at": iso_or_none(s.delivered_at),
                    }
                    for s in sales
                ]
            ),
            use_container_width=True,
        )
        return
    _render_shipping_presets(repo, actor, user)
    st.divider()
    _render_shipping_copilot(repo, sales, user)
    st.divider()

    presets = repo.list_shipping_presets(active_only=True)
    t1, t2, t3, t4 = st.tabs(["Needs Label", "In Transit", "Delivered", "Exceptions"])
    with t1:
        _render_queue(repo, "needs_label", "needs label", "label_created", actor, sales, presets)
    with t2:
        _render_queue(repo, "in_transit", "in transit", "in_transit", actor, sales, presets)
    with t3:
        _render_queue(repo, "delivered", "delivered", "delivered", actor, sales, presets)
    with t4:
        _render_queue(repo, "exceptions", "exceptions", "exception", actor, sales, presets)

    st.divider()
    _render_shipment_export(repo, actor, sales)
    st.divider()
    _render_label_purchase_queue(repo, actor, sales, presets, user)
    st.divider()
    _render_ebay_tracking_push(repo, actor, sales, user)
    st.divider()
    render_ebay_push_history(
        repo,
        section_title="eBay Push History",
        key_prefix="shipping_ebay_push_history",
        actor=actor,
        user=user,
    )
    st.divider()
    render_workspace_task_completion(
        repo=repo,
        actor=user.username,
        workflow_key="shipping",
        section_title="Workflow Completion: Shipping",
        tasks=[
            ("Created labels for needs-label queue", "shipping_labels_created"),
            ("Resolved shipping exceptions", "shipping_exceptions_resolved"),
            ("Pushed tracking updates to eBay", "shipping_tracking_pushed"),
        ],
    )
    st.divider()
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="shipping",
        section_title="Workspace Feedback",
    )
