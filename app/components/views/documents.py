from datetime import datetime, timedelta
from pathlib import Path
import base64
import json

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import iso_or_none
from app.components.ui_helpers import format_ebay_sync_note_for_customer
from app.components.document_templates import TEMPLATES, build_document_html
from app.components.views.shared import dataframe_to_xlsx_bytes, render_help_panel
from app.components.views.workspace_shell import render_workspace_feedback
from app.config import settings
from app.repository import InventoryRepository
from app.services.google_workspace import (
    create_calendar_event,
    resolve_google_workspace_config,
    send_gmail_message,
    upload_drive_file,
)
from app.services.runtime_settings import get_runtime_bool, get_runtime_int
from app.services.runtime_settings import get_runtime_str
from app.utils.time import utc_today

DEFAULT_LOGO_PATH = Path(__file__).resolve().parents[2] / "images" / "logonewmed.jpg"


def _file_to_data_url(path: Path) -> str:
    content = path.read_bytes()
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _resolve_logo_src(template_name: str, logo_override: str) -> str:
    override = (logo_override or "").strip()
    if override:
        if override.startswith("http://") or override.startswith("https://") or override.startswith("data:"):
            return override
        possible = Path(override)
        if not possible.is_absolute():
            possible = Path(__file__).resolve().parents[3] / override
        if possible.exists() and possible.is_file():
            return _file_to_data_url(possible)
        return override

    if template_name == "Classic" and DEFAULT_LOGO_PATH.exists():
        return _file_to_data_url(DEFAULT_LOGO_PATH)
    return ""


def _build_items_for_order(order) -> list[dict]:
    items: list[dict] = []
    for line in order.items:
        qty = int(line.quantity or 0)
        unit_price = float(line.unit_price or 0)
        items.append(
            {
                "sku": line.product.sku if line.product else "",
                "title": line.product.title if line.product else "",
                "qty": qty,
                "unit_price": unit_price,
                "line_total": float(line.line_total or (unit_price * qty)),
                "category": (line.product.category if line.product else ""),
            }
        )
    return items


def _build_items_for_sale(sale) -> list[dict]:
    qty = int(sale.quantity_sold or 1)
    unit_price = (float(sale.sold_price) / qty) if qty else float(sale.sold_price)
    return [
        {
            "sku": sale.product.sku if sale.product else "",
            "title": sale.product.title if sale.product else "",
            "qty": qty,
            "unit_price": unit_price,
            "line_total": float(sale.sold_price or 0),
            "category": (sale.product.category if sale.product else ""),
        }
    ]


def _build_items_for_listing(listing, quantity: int, unit_price: float) -> list[dict]:
    qty = max(1, int(quantity or 1))
    unit = max(0.0, float(unit_price or 0.0))
    product = getattr(listing, "product", None)
    return [
        {
            "sku": product.sku if product else "",
            "title": (listing.listing_title or "").strip() or (product.title if product else ""),
            "qty": qty,
            "unit_price": unit,
            "line_total": float(unit * qty),
            "category": (product.category if product else ""),
        }
    ]


def _to_money_float(value) -> float:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _extract_ebay_marketplace_financials(order) -> dict:
    payload_raw = str(getattr(order, "marketplace_payload_json", "") or "").strip()
    if not payload_raw:
        return {"tax_amount": 0.0, "discount_amount": 0.0}
    try:
        payload = json.loads(payload_raw)
    except Exception:
        return {"tax_amount": 0.0, "discount_amount": 0.0}
    if not isinstance(payload, dict):
        return {"tax_amount": 0.0, "discount_amount": 0.0}

    pricing = payload.get("pricingSummary") if isinstance(payload.get("pricingSummary"), dict) else {}
    subtotal = _to_money_float(pricing.get("priceSubtotal"))
    delivery_cost = _to_money_float(pricing.get("deliveryCost"))
    delivery_discount_raw = _to_money_float(pricing.get("deliveryDiscount"))
    total = _to_money_float(pricing.get("total"))
    explicit_tax = _to_money_float(pricing.get("totalTax"))

    line_tax = 0.0
    line_items = payload.get("lineItems")
    if isinstance(line_items, list):
        for line in line_items:
            if not isinstance(line, dict):
                continue
            taxes = line.get("taxes")
            if isinstance(taxes, list):
                for tx in taxes:
                    if not isinstance(tx, dict):
                        continue
                    line_tax += _to_money_float(tx.get("amount")) or _to_money_float(tx.get("taxAmount"))

    tax_amount = float(max(explicit_tax, line_tax))
    if tax_amount <= 0 and total > 0:
        # eBay total generally equals subtotal + shipping + deliveryDiscount (+/-) + tax.
        inferred = total - subtotal - delivery_cost - delivery_discount_raw
        tax_amount = float(max(0.0, round(inferred, 2)))

    discount_amount = float(abs(delivery_discount_raw)) if delivery_discount_raw < 0 else 0.0
    return {
        "tax_amount": float(max(0.0, tax_amount)),
        "discount_amount": float(max(0.0, discount_amount)),
    }


def _parse_csv_set(value: str) -> set[str]:
    return {str(part).strip().lower() for part in str(value or "").split(",") if str(part).strip()}


def _taxable_subtotal_auto(items: list[dict], exempt_categories: set[str]) -> float:
    taxable = 0.0
    for item in items:
        category = str(item.get("category") or "").strip().lower()
        line_total = float(item.get("line_total") or 0.0)
        if line_total <= 0:
            continue
        if category and category in exempt_categories:
            continue
        taxable += line_total
    return max(0.0, taxable)


def _default_tax_presets(
    *,
    default_jurisdiction: str,
    default_tax_rate_percent: float,
    default_shipping_taxable: bool,
) -> dict[str, dict]:
    return {
        "Golden Local Retail": {
            "jurisdiction": default_jurisdiction or "Golden, Colorado",
            "tax_mode": "Auto (Category Rules)",
            "tax_rate_percent": float(default_tax_rate_percent),
            "shipping_taxable": bool(default_shipping_taxable),
        },
        "Marketplace Shipped": {
            "jurisdiction": default_jurisdiction or "Golden, Colorado",
            "tax_mode": "Auto (Category Rules)",
            "tax_rate_percent": float(default_tax_rate_percent),
            "shipping_taxable": False,
        },
        "Bullion/Coin Exempt": {
            "jurisdiction": default_jurisdiction or "Golden, Colorado",
            "tax_mode": "No Tax",
            "tax_rate_percent": 0.0,
            "shipping_taxable": False,
        },
    }


def _derive_tax_exemption_basis(
    *,
    tax_mode: str,
    exempt_categories: set[str],
    shipping_is_taxable: bool,
    use_line_item_taxability: bool,
) -> str:
    mode = str(tax_mode or "").strip()
    if mode == "No Tax":
        return "tax_mode_no_tax"
    notes: list[str] = []
    if mode == "Auto (Category Rules)":
        if exempt_categories:
            notes.append("auto_category_exemptions:" + ",".join(sorted(exempt_categories)))
        if use_line_item_taxability:
            notes.append("line_item_taxability_overrides_enabled")
    elif mode == "Manual Taxable Subtotal":
        notes.append("manual_taxable_subtotal_override")
    notes.append("shipping_taxable" if shipping_is_taxable else "shipping_exempt")
    return ";".join(notes) if notes else "standard_tax_treatment"


def _render_retained_artifacts(
    repo: InventoryRepository,
    *,
    source_type: str,
    source_id: int | None,
) -> None:
    st.markdown("### Retained Document Artifacts")
    rows = repo.list_document_artifacts_for_source(
        source_type=source_type,
        source_id=source_id,
        limit=25,
    )
    if not rows:
        st.caption("No immutable artifacts retained yet for this source.")
        return

    artifact_df = pd.DataFrame(
        [
            {
                "id": int(row.id),
                "doc_type": str(row.doc_type or "").strip(),
                "document_number": str(row.document_number or "").strip(),
                "artifact_kind": str(row.artifact_kind or "").strip(),
                "file_name": str(row.file_name or "").strip(),
                "mime_type": str(row.mime_type or "").strip(),
                "size_bytes": int(row.size_bytes or 0),
                "sha256": str(row.content_sha256 or "").strip(),
                "storage_backend": str(row.storage_backend or "").strip(),
                "storage_ref": str(row.storage_ref or "").strip(),
                "created_by": str(row.created_by or "").strip(),
                "created_at": iso_or_none(row.created_at) or "",
            }
            for row in rows
        ]
    )
    st.dataframe(artifact_df, use_container_width=True, hide_index=True)

    selected_artifact_id = st.selectbox(
        "Download retained artifact",
        options=[int(r.id) for r in rows],
        format_func=lambda aid: next(
            (
                f"#{int(r.id)} | {str(r.file_name or '').strip()} | {str(r.doc_type or '').strip()} | {str(r.content_sha256 or '').strip()[:12]}"
                for r in rows
                if int(r.id) == int(aid)
            ),
            str(aid),
        ),
        key=f"documents_retained_artifact_select_{str(source_type).lower()}_{int(source_id) if source_id is not None else 0}",
    )
    selected_row = next((r for r in rows if int(r.id) == int(selected_artifact_id)), None)
    if selected_row is None:
        return
    try:
        artifact_bytes = repo.get_document_artifact_content(int(selected_row.id))
    except Exception as exc:
        st.warning(f"Unable to read retained artifact content: {exc}")
        return
    st.download_button(
        "Download Retained Artifact",
        data=artifact_bytes,
        file_name=str(selected_row.file_name or f"document_artifact_{int(selected_row.id)}.bin"),
        mime=str(selected_row.mime_type or "application/octet-stream"),
        key=f"documents_retained_artifact_download_{int(selected_row.id)}",
    )


def render_documents(repo: InventoryRepository) -> None:
    user = current_user()
    st.subheader("Invoices and Receipts")
    st.caption("Generate branded invoice/receipt documents with print-friendly templates.")
    render_help_panel(
        section_title="Invoices and Receipts",
        goal="Generate consistent branded customer-facing documents for orders and sales.",
        steps=[
            "Select source record type (Order or Sale) and choose the target record.",
            "Choose document type (invoice/receipt) and a branding template.",
            "Set business identity fields and optional notes for the document footer/body.",
            "Preview print layout, then download HTML/CSV/XLSX outputs or print directly.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    environment = settings.app_env
    actor = user.username
    google_queue_enabled = get_runtime_bool(repo, "google_queue_enabled", True)
    google_queue_max_retries = max(0, min(20, get_runtime_int(repo, "google_queue_max_retries", 5)))
    st.caption(f"Signed in as `{user.username}` ({user.role}). Profile changes are attributed to this identity.")
    prefill_source_type = str(st.session_state.get("documents_prefill_source_type") or "").strip()
    prefill_source_id = st.session_state.get("documents_prefill_source_id")
    prefill_doc_type = str(st.session_state.get("documents_prefill_doc_type") or "").strip().lower()
    prefill_apply_once = bool(prefill_source_type) and not bool(st.session_state.get("documents_prefill_applied"))
    if prefill_apply_once:
        if prefill_source_type in {"Order", "Sale", "Listing"}:
            st.session_state["documents_source_type"] = prefill_source_type
        if prefill_doc_type in {"invoice", "receipt"}:
            st.session_state["documents_doc_type"] = prefill_doc_type
        if str(st.session_state.get("documents_prefill_tax_jurisdiction") or "").strip():
            st.session_state["documents_tax_jurisdiction"] = str(
                st.session_state.get("documents_prefill_tax_jurisdiction")
            ).strip()
        if st.session_state.get("documents_prefill_tax_rate_percent") is not None:
            try:
                st.session_state["documents_tax_rate_percent"] = float(
                    st.session_state.get("documents_prefill_tax_rate_percent")
                )
            except Exception:
                pass
        if st.session_state.get("documents_prefill_tax_shipping_taxable") is not None:
            st.session_state["documents_tax_shipping_taxable"] = bool(
                st.session_state.get("documents_prefill_tax_shipping_taxable")
            )
        st.session_state["documents_prefill_applied"] = True
    if prefill_source_type:
        st.info(
            f"Prefilled from report handoff: `{prefill_source_type}` #{prefill_source_id or ''}. "
            "Source and tax settings were loaded."
        )
        if st.button("Clear Prefill", key="documents_clear_prefill_btn"):
            for key in [
                "documents_prefill_source_type",
                "documents_prefill_source_id",
                "documents_prefill_doc_type",
                "documents_prefill_tax_jurisdiction",
                "documents_prefill_tax_rate_percent",
                "documents_prefill_tax_shipping_taxable",
                "documents_prefill_applied",
            ]:
                st.session_state.pop(key, None)
            st.rerun()

    st.markdown("### Recent Document Handoffs")
    setting_key = f"documents_recent_handoffs_json__{str(user.username).strip().lower()}"
    persisted_handoffs: list[dict] = []
    try:
        raw_handoffs = get_runtime_str(repo, setting_key, "").strip()
        if raw_handoffs:
            parsed_handoffs = json.loads(raw_handoffs)
            if isinstance(parsed_handoffs, list):
                persisted_handoffs = [row for row in parsed_handoffs if isinstance(row, dict)]
    except Exception:
        persisted_handoffs = []
    session_handoffs = list(st.session_state.get("documents_recent_handoffs") or [])
    merged_handoffs = persisted_handoffs + session_handoffs
    deduped_handoffs: list[dict] = []
    seen_handoffs: set[tuple[str, int, str]] = set()
    for row in merged_handoffs:
        key = (
            str(row.get("source_type") or "").strip(),
            int(row.get("source_id") or 0),
            str(row.get("doc_type") or "").strip().lower(),
        )
        if key in seen_handoffs:
            continue
        seen_handoffs.add(key)
        deduped_handoffs.append(row)
    recent_handoffs = deduped_handoffs[:50]
    st.session_state["documents_recent_handoffs"] = recent_handoffs
    if not recent_handoffs:
        st.caption("No handoff history yet for this user/environment.")
    else:
        hr1, hr2 = st.columns([4, 1])
        with hr1:
            st.caption("Re-open recent Sale/Order draft contexts without navigating back.")
        with hr2:
            if st.button("Clear History", key="documents_clear_handoff_history_btn"):
                st.session_state["documents_recent_handoffs"] = []
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=setting_key,
                        value="[]",
                        value_type="str",
                        description="Recent Documents handoff contexts (per-user) for quick reopen.",
                        is_active=True,
                        actor=actor,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_history",
                            entity_id=None,
                            action="clear_history",
                            actor=actor,
                            changes={
                                "scope": "self",
                                "target_user": str(user.username).strip().lower(),
                                "environment": settings.app_env,
                                "reason_code": "user_request",
                                "reason_note": "",
                                "reason": "user_initiated_clear",
                            },
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
                st.rerun()
        preview_df = pd.DataFrame(
            [
                {
                    "at": str(row.get("at") or ""),
                    "source_type": str(row.get("source_type") or ""),
                    "source_id": int(row.get("source_id") or 0),
                    "doc_type": str(row.get("doc_type") or "invoice"),
                    "from": str(row.get("handoff_from") or ""),
                }
                for row in recent_handoffs[:12]
            ]
        )
        st.dataframe(preview_df, use_container_width=True, hide_index=True)
        selected_recent_idx = st.selectbox(
            "Select Recent Handoff",
            options=list(range(min(len(recent_handoffs), 12))),
            format_func=lambda i: (
                f"{recent_handoffs[i].get('source_type')} #{int(recent_handoffs[i].get('source_id') or 0)} | "
                f"{str(recent_handoffs[i].get('doc_type') or 'invoice')} | "
                f"{str(recent_handoffs[i].get('handoff_from') or 'unknown')}"
            ),
            key="documents_recent_handoff_select_idx",
        )
        if st.button("Reopen Selected Handoff", key="documents_reopen_handoff_btn"):
            selected_recent = recent_handoffs[int(selected_recent_idx)]
            st.session_state["documents_prefill_source_type"] = str(
                selected_recent.get("source_type") or ""
            ).strip()
            st.session_state["documents_prefill_source_id"] = int(selected_recent.get("source_id") or 0)
            st.session_state["documents_prefill_doc_type"] = str(
                selected_recent.get("doc_type") or "invoice"
            ).strip().lower()
            st.session_state["documents_prefill_applied"] = False
            st.rerun()

    source_type = st.radio("Source Type", ["Order", "Sale", "Listing"], horizontal=True, key="documents_source_type")
    doc_type = st.selectbox("Document Type", ["invoice", "receipt"], key="documents_doc_type")
    profiles = repo.list_document_template_profiles(
        environment=environment,
        doc_type=doc_type,
        include_all_doc_type=True,
        active_only=True,
    )
    profile_map = {"None": None}
    for p in profiles:
        default_tag = " (default)" if p.is_default else ""
        profile_map[f"#{p.id} | {p.name} | {p.doc_type}{default_tag}"] = p

    default_profile = next((p for p in profiles if p.is_default), None)
    default_key = next((k for k, v in profile_map.items() if v and default_profile and v.id == default_profile.id), "None")
    selected_profile_key = st.selectbox(
        f"Saved Profile ({environment})",
        list(profile_map.keys()),
        index=list(profile_map.keys()).index(default_key) if default_key in profile_map else 0,
        help="Profiles are stored in DB and can be environment-specific defaults.",
    )
    selected_profile = profile_map[selected_profile_key]

    template_name_default = selected_profile.template_name if selected_profile else "Classic"
    accent_default = selected_profile.accent_color if selected_profile else "#b45309"
    template_name = st.selectbox("Template", list(TEMPLATES.keys()), index=list(TEMPLATES.keys()).index(template_name_default) if template_name_default in TEMPLATES else 0)
    accent_color = st.color_picker("Brand Accent Color", value=accent_default)
    logo_override = st.text_input(
        "Logo Override (URL or path, optional)",
        value="",
        help="Leave blank to use default logo for Classic template (app/images/logonewmed.jpg).",
    )

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        company_name = st.text_input(
            "Business Name",
            value=selected_profile.company_name if selected_profile else "Golden Stackers LLC",
        )
    with b2:
        company_email = st.text_input(
            "Business Email",
            value=selected_profile.company_email if selected_profile else "sales@goldenstackers.com",
        )
    with b3:
        company_phone = st.text_input(
            "Business Phone",
            value=selected_profile.company_phone if selected_profile else "720-253-2354",
        )
    with b4:
        company_website = st.text_input(
            "Website",
            value=selected_profile.company_website if selected_profile else "https://goldenstackers.com",
        )

    customer_label = st.text_input("Customer Label", value="Marketplace Buyer")
    custom_doc_number = st.text_input("Document Number Override (Optional)")
    document_date = st.date_input("Document Date", value=utc_today())

    if source_type == "Order":
        orders = repo.list_orders()
        if not orders:
            st.info("No orders available yet.")
            return
        order_map = {f"#{o.id} | {o.marketplace} | {o.external_order_id}": o for o in orders}
        order_keys = list(order_map.keys())
        order_index = 0
        if prefill_apply_once and prefill_source_type == "Order" and prefill_source_id is not None:
            for idx, key in enumerate(order_keys):
                candidate = order_map.get(key)
                if candidate is not None and int(candidate.id) == int(prefill_source_id):
                    order_index = idx
                    break
        selected_key = st.selectbox("Order", order_keys, index=order_index, key="documents_order_select")
        selected = order_map[selected_key]
        source_label = "Order"
        source_number = selected.external_order_id or f"ORDER-{selected.id}"
        sold_at = iso_or_none(selected.sold_at) or ""
        items = _build_items_for_order(selected)
        subtotal = float(selected.subtotal_amount or 0)
        fees = float(selected.fees or 0)
        shipping_cost = float(selected.shipping_cost or 0)
        total = float(selected.total_amount or subtotal)
        source_marketplace_normalized = str(selected.marketplace or "").strip().lower()
        notes = selected.notes or ""
        if source_marketplace_normalized == "ebay":
            notes = format_ebay_sync_note_for_customer(notes)
        marketplace_financials = (
            _extract_ebay_marketplace_financials(selected)
            if source_marketplace_normalized == "ebay"
            else {"tax_amount": 0.0, "discount_amount": 0.0}
        )
    elif source_type == "Sale":
        sales = repo.list_sales()
        if not sales:
            st.info("No sales available yet.")
            return
        sale_map = {f"#{s.id} | {s.marketplace} | {s.external_order_id or 'no-order-id'}": s for s in sales}
        sale_keys = list(sale_map.keys())
        sale_index = 0
        if prefill_apply_once and prefill_source_type == "Sale" and prefill_source_id is not None:
            for idx, key in enumerate(sale_keys):
                candidate = sale_map.get(key)
                if candidate is not None and int(candidate.id) == int(prefill_source_id):
                    sale_index = idx
                    break
        selected_key = st.selectbox("Sale", sale_keys, index=sale_index, key="documents_sale_select")
        selected = sale_map[selected_key]
        source_label = "Sale"
        source_number = selected.external_order_id or f"SALE-{selected.id}"
        sold_at = iso_or_none(selected.sold_at) or ""
        items = _build_items_for_sale(selected)
        subtotal = float(selected.sold_price or 0)
        fees = float(selected.fees or 0)
        shipping_cost = float(selected.shipping_cost or 0)
        total = subtotal
        notes = ""
        source_marketplace_normalized = str(selected.marketplace or "").strip().lower()
        marketplace_financials = {"tax_amount": 0.0, "discount_amount": 0.0}
        if source_marketplace_normalized == "ebay":
            linked_order = None
            order_id = getattr(selected, "order_id", None)
            if order_id is not None:
                linked_order = next(
                    (row for row in repo.list_orders() if int(row.id) == int(order_id)),
                    None,
                )
            if linked_order is not None:
                notes = format_ebay_sync_note_for_customer(getattr(linked_order, "notes", "") or "")
                order_financials = _extract_ebay_marketplace_financials(linked_order)
                order_subtotal = float(getattr(linked_order, "subtotal_amount", 0.0) or 0.0)
                ratio = (float(subtotal) / order_subtotal) if order_subtotal > 0 else 1.0
                ratio = max(0.0, min(1.0, ratio))
                marketplace_financials = {
                    "tax_amount": round(float(order_financials.get("tax_amount") or 0.0) * ratio, 2),
                    "discount_amount": round(float(order_financials.get("discount_amount") or 0.0) * ratio, 2),
                }
                if float(fees or 0.0) <= 0.0:
                    fees = round(float(getattr(linked_order, "fees", 0.0) or 0.0) * ratio, 2)
                if float(shipping_cost or 0.0) <= 0.0:
                    shipping_cost = round(float(getattr(linked_order, "shipping_cost", 0.0) or 0.0) * ratio, 2)
    else:
        listings = repo.list_listings()
        if not listings:
            st.info("No listings available yet.")
            return
        listing_map = {
            f"#{l.id} | {str(l.marketplace or '').strip()} | "
            f"{str(l.listing_title or '').strip()[:60]} | "
            f"price=${float(l.listing_price or 0):,.2f}": l
            for l in listings
        }
        listing_keys = list(listing_map.keys())
        listing_index = 0
        if prefill_apply_once and prefill_source_type == "Listing" and prefill_source_id is not None:
            for idx, key in enumerate(listing_keys):
                candidate = listing_map.get(key)
                if candidate is not None and int(candidate.id) == int(prefill_source_id):
                    listing_index = idx
                    break
        selected_key = st.selectbox("Listing", listing_keys, index=listing_index, key="documents_listing_select")
        selected = listing_map[selected_key]
        li1, li2, li3 = st.columns(3)
        with li1:
            listing_qty = st.number_input(
                "Invoice Quantity",
                min_value=1,
                value=max(1, int(selected.quantity_listed or 1)),
                step=1,
                key=f"documents_listing_qty_{int(selected.id)}",
            )
        with li2:
            listing_unit_price = st.number_input(
                "Unit Price",
                min_value=0.0,
                value=float(selected.listing_price or 0.0),
                step=0.01,
                key=f"documents_listing_unit_price_{int(selected.id)}",
            )
        with li3:
            listing_shipping_cost = st.number_input(
                "Shipping Cost",
                min_value=0.0,
                value=0.0,
                step=0.01,
                key=f"documents_listing_shipping_{int(selected.id)}",
            )
        lf1, lf2 = st.columns(2)
        with lf1:
            listing_fees = st.number_input(
                "Fees",
                min_value=0.0,
                value=0.0,
                step=0.01,
                key=f"documents_listing_fees_{int(selected.id)}",
            )
        with lf2:
            listing_sale_date = st.date_input(
                "Sale Date (for invoice)",
                value=utc_today(),
                key=f"documents_listing_sold_date_{int(selected.id)}",
            )

        source_label = "Listing"
        source_number = selected.external_listing_id or f"LISTING-{selected.id}"
        sold_at = datetime.combine(listing_sale_date, datetime.min.time()).isoformat()
        items = _build_items_for_listing(selected, int(listing_qty), float(listing_unit_price))
        subtotal = round(float(listing_qty) * float(listing_unit_price), 2)
        fees = float(listing_fees or 0.0)
        shipping_cost = float(listing_shipping_cost or 0.0)
        total = round(float(subtotal + fees + shipping_cost), 2)
        notes = str(selected.marketplace_details or "").strip()
        source_marketplace_normalized = str(selected.marketplace or "").strip().lower()
        marketplace_financials = {"tax_amount": 0.0, "discount_amount": 0.0}

    default_doc_number = (
        f"{'INV' if doc_type == 'invoice' else 'RCT'}-{datetime.now().strftime('%Y%m%d')}-{selected.id}"
    )
    document_number = custom_doc_number.strip() or default_doc_number
    memo_notes_default = selected_profile.notes if selected_profile and selected_profile.notes.strip() else notes
    memo_notes = st.text_area("Notes", value=memo_notes_default)
    st.markdown("### Tax Settings")
    st.caption(
        "Golden, CO local default profile is codified at 7.50% "
        "(CO 2.90 + Jefferson County 0.50 + Golden 3.00 + CD 0.10 + RTD 1.00)."
    )
    default_tax_jurisdiction = get_runtime_str(repo, "invoicing_tax_jurisdiction", "Golden, Colorado")
    default_tax_rate_raw = get_runtime_str(repo, "invoicing_tax_rate_percent_default", "7.50")
    try:
        default_tax_rate = float(default_tax_rate_raw)
    except Exception:
        default_tax_rate = 0.0
    shipping_taxable_default = get_runtime_bool(repo, "invoicing_tax_shipping_taxable_default", False)
    if "documents_tax_jurisdiction" not in st.session_state:
        st.session_state["documents_tax_jurisdiction"] = str(default_tax_jurisdiction or "Golden, Colorado")
    if "documents_tax_mode" not in st.session_state:
        st.session_state["documents_tax_mode"] = "Auto (Category Rules)"
    if "documents_tax_rate_percent" not in st.session_state:
        st.session_state["documents_tax_rate_percent"] = float(max(0.0, default_tax_rate))
    if "documents_tax_shipping_taxable" not in st.session_state:
        st.session_state["documents_tax_shipping_taxable"] = bool(shipping_taxable_default)

    preset_map = _default_tax_presets(
        default_jurisdiction=default_tax_jurisdiction,
        default_tax_rate_percent=float(max(0.0, default_tax_rate)),
        default_shipping_taxable=bool(shipping_taxable_default),
    )
    tp1, tp2, tp3 = st.columns([2, 1, 1])
    with tp1:
        selected_tax_preset = st.selectbox(
            "Tax Preset",
            options=list(preset_map.keys()),
            key="documents_tax_preset",
        )
    with tp2:
        if st.button("Apply Tax Preset", key="documents_apply_tax_preset_btn"):
            preset = preset_map.get(selected_tax_preset) or {}
            st.session_state["documents_tax_jurisdiction"] = str(
                preset.get("jurisdiction") or default_tax_jurisdiction or "Golden, Colorado"
            )
            st.session_state["documents_tax_mode"] = str(preset.get("tax_mode") or "Auto (Category Rules)")
            st.session_state["documents_tax_rate_percent"] = float(
                max(0.0, float(preset.get("tax_rate_percent") or 0.0))
            )
            st.session_state["documents_tax_shipping_taxable"] = bool(preset.get("shipping_taxable", False))
            st.success(f"Applied tax preset `{selected_tax_preset}`.")
            st.rerun()
    with tp3:
        if st.button("Save Current As Runtime Defaults", key="documents_save_tax_runtime_defaults_btn"):
            if ensure_permission(user, "manage_profiles", "Save Tax Runtime Defaults"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="invoicing_tax_jurisdiction",
                        value=str(st.session_state.get("documents_tax_jurisdiction") or "").strip(),
                        value_type="str",
                        description="Default jurisdiction label for invoice/receipt tax display.",
                        is_active=True,
                        actor=actor,
                    )
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="invoicing_tax_rate_percent_default",
                        value=str(float(st.session_state.get("documents_tax_rate_percent") or 0.0)),
                        value_type="str",
                        description="Default sales-tax rate percent used by Documents tax calculator.",
                        is_active=True,
                        actor=actor,
                    )
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="invoicing_tax_shipping_taxable_default",
                        value="true" if bool(st.session_state.get("documents_tax_shipping_taxable")) else "false",
                        value_type="bool",
                        description="Default toggle for whether shipping is taxable in Documents tax calculator.",
                        is_active=True,
                        actor=actor,
                    )
                    st.success("Saved current tax settings as runtime defaults.")
                except Exception as exc:
                    st.error(f"Unable to save runtime defaults: {exc}")

    tax_jurisdiction = st.text_input(
        "Tax Jurisdiction",
        key="documents_tax_jurisdiction",
        help="Used as tax label context on the invoice/receipt.",
    ).strip()
    tax_mode = st.selectbox(
        "Tax Mode",
        ["Auto (Category Rules)", "Manual Taxable Subtotal", "No Tax"],
        key="documents_tax_mode",
        help=(
            "Auto mode exempts categories in runtime key "
            "`invoicing_tax_exempt_categories_csv` (default: bullion,coins)."
        ),
    )
    tax_rate_percent = st.number_input(
        "Sales Tax Rate (%)",
        min_value=0.0,
        key="documents_tax_rate_percent",
        step=0.01,
    )
    shipping_is_taxable = st.checkbox(
        "Shipping is taxable",
        key="documents_tax_shipping_taxable",
    )
    exempt_categories = _parse_csv_set(
        get_runtime_str(repo, "invoicing_tax_exempt_categories_csv", "bullion,coins")
    )
    st.caption(
        "Exempt categories (runtime): "
        + (", ".join(sorted(exempt_categories)) if exempt_categories else "(none)")
    )
    if "bullion" in exempt_categories or "coins" in exempt_categories:
        st.info(
            "Colorado bullion/coin tax handling is enabled via exempt category rules. "
            "Verify with your tax professional for current local/state law applicability."
        )
    use_line_item_taxability = st.checkbox(
        "Use line-item taxability overrides (Auto mode)",
        value=True,
        help="When enabled, you can manually mark each line taxable/exempt for mixed invoices.",
    )
    marketplace_overrides_available = source_marketplace_normalized == "ebay" and source_type in {"Order", "Sale"}
    use_marketplace_overrides = False
    if marketplace_overrides_available:
        st.info(
            "eBay handles marketplace tax collection/remittance. "
            "Use imported marketplace tax/discount values on this invoice when available."
        )
        use_marketplace_overrides = st.checkbox(
            "Use eBay Marketplace Tax/Discount Values",
            value=True,
            key=f"documents_use_ebay_marketplace_financials_{source_type.lower()}_{int(selected.id)}",
        )
    auto_taxable_subtotal = _taxable_subtotal_auto(items, exempt_categories)
    if use_line_item_taxability and items:
        line_tax_rows = []
        for idx, item in enumerate(items):
            category = str(item.get("category") or "").strip().lower()
            line_total = float(item.get("line_total") or 0.0)
            auto_taxable = line_total > 0 and not (category and category in exempt_categories)
            line_tax_rows.append(
                {
                    "row": idx + 1,
                    "sku": str(item.get("sku") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "category": category,
                    "line_total": float(line_total),
                    "taxable": bool(auto_taxable),
                }
            )
        line_tax_df = pd.DataFrame(line_tax_rows)
        edited_line_tax_df = st.data_editor(
            line_tax_df,
            use_container_width=True,
            hide_index=True,
            key=f"documents_tax_line_editor::{source_type.lower()}::{int(selected.id)}",
            disabled=["row", "sku", "title", "category", "line_total"],
        )
        if isinstance(edited_line_tax_df, pd.DataFrame) and not edited_line_tax_df.empty:
            taxable_mask = edited_line_tax_df["taxable"].astype(bool)
            auto_taxable_subtotal = round(
                float(edited_line_tax_df[taxable_mask]["line_total"].sum()),
                2,
            )
            st.caption(f"Taxable subtotal from line-item overrides: ${auto_taxable_subtotal:,.2f}")
    manual_taxable_subtotal = st.number_input(
        "Manual Taxable Subtotal",
        min_value=0.0,
        value=max(0.0, auto_taxable_subtotal),
        step=1.0,
        disabled=(tax_mode != "Manual Taxable Subtotal"),
    )
    if tax_mode == "No Tax":
        taxable_subtotal = 0.0
    elif tax_mode == "Manual Taxable Subtotal":
        taxable_subtotal = float(manual_taxable_subtotal)
    else:
        taxable_subtotal = float(auto_taxable_subtotal)
    if shipping_is_taxable:
        taxable_subtotal += float(shipping_cost or 0.0)
    taxable_subtotal = max(0.0, taxable_subtotal)
    tax_amount = round(taxable_subtotal * (float(tax_rate_percent) / 100.0), 2)
    discount_amount = 0.0
    tax_label = f"Sales Tax ({tax_jurisdiction or 'Local'})"
    if use_marketplace_overrides:
        tax_amount = float(round(float(marketplace_financials.get("tax_amount") or 0.0), 2))
        discount_amount = float(round(float(marketplace_financials.get("discount_amount") or 0.0), 2))
        tax_label = "Marketplace Tax (Collected by eBay)"
    show_fees_on_customer_doc = source_marketplace_normalized != "ebay"
    displayed_fees = float(fees or 0.0) if show_fees_on_customer_doc else 0.0

    calculated_total = round(
        float(subtotal or 0.0)
        + float(displayed_fees or 0.0)
        + float(shipping_cost or 0.0)
        - float(discount_amount or 0.0)
        + float(tax_amount or 0.0),
        2,
    )
    use_calculated_total = st.checkbox(
        "Use tax-adjusted computed total",
        value=True,
        help="When off, document uses the source record total field and still displays tax as informational.",
    )
    document_total = float(calculated_total if use_calculated_total else total)
    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Taxable Subtotal", f"${taxable_subtotal:,.2f}")
    t2.metric("Tax Amount", f"${tax_amount:,.2f}")
    t3.metric("Discount", f"${discount_amount:,.2f}")
    t4.metric("Document Total", f"${document_total:,.2f}")

    html = build_document_html(
        doc_type=doc_type,
        template_name=template_name,
        accent_color=accent_color,
        company_name=company_name,
        company_email=company_email,
        company_phone=company_phone,
        company_website=company_website,
        logo_src=_resolve_logo_src(template_name, logo_override),
        customer_label=customer_label,
        document_number=document_number,
        document_date=document_date.isoformat(),
        source_label=source_label,
        source_number=source_number,
        source_marketplace=selected.marketplace,
        sold_at=sold_at,
        notes=memo_notes,
        items=items,
        subtotal=subtotal,
        fees=displayed_fees,
        show_fees=show_fees_on_customer_doc,
        shipping_cost=shipping_cost,
        discount_amount=discount_amount,
        discount_label="Marketplace Discount" if use_marketplace_overrides else "Discount",
        tax_amount=tax_amount,
        tax_label=tax_label,
        total=document_total,
    )
    html_bytes = html.encode("utf-8")

    if source_type == "Listing":
        st.markdown("### Create Sale Record From This Invoice")
        st.caption(
            "Use this for local/cash/marketplace transactions so invoice generation and sales tracking stay aligned."
        )
        posting_enabled = get_runtime_bool(repo, "documents_listing_posting_enabled", True)
        posting_roles_raw = get_runtime_str(repo, "documents_listing_posting_roles_csv", "ops,admin")
        posting_roles = {
            str(token).strip().lower()
            for token in str(posting_roles_raw or "").split(",")
            if str(token).strip()
        } or {"ops", "admin"}
        can_post_financial = bool(posting_enabled) and str(user.role or "").strip().lower() in posting_roles
        if not posting_enabled:
            st.warning("Listing invoice posting is disabled by runtime policy in this environment.")
        elif not can_post_financial:
            st.warning(
                "Your role is not allowed to post financial records from Documents in this environment. "
                f"Allowed roles: {', '.join(sorted(posting_roles))}"
            )
        listing_outcome = st.selectbox(
            "Listing Outcome",
            options=["Sold", "Not Sold / Remove Listing"],
            index=0,
            key=f"documents_listing_outcome_{int(selected.id)}",
        )
        if listing_outcome != "Sold":
            st.info("No sale will be posted. Use one of the listing cleanup actions below.")
            ns1, ns2 = st.columns(2)
            with ns1:
                if st.button(
                    "End Listing As Not Sold",
                    key=f"documents_listing_end_not_sold_btn_{int(selected.id)}",
                ):
                    if not ensure_permission(user, "update", "End Listing As Not Sold"):
                        st.stop()
                    try:
                        updated_details = "\n\n".join(
                            [
                                str(selected.marketplace_details or "").strip(),
                                f"[not_sold] Ended via Documents on {datetime.now().isoformat()} by {actor}",
                            ]
                        ).strip()
                        repo.update_listing(
                            int(selected.id),
                            {
                                "listing_status": "ended",
                                "review_status": str(getattr(selected, "review_status", "pending") or "pending"),
                                "marketplace_details": updated_details,
                            },
                            actor=actor,
                        )
                        st.success(f"Listing #{int(selected.id)} marked ended (not sold).")
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Unable to end listing: {exc}")
            with ns2:
                if st.button(
                    "Archive/Remove Listing",
                    key=f"documents_listing_archive_btn_{int(selected.id)}",
                ):
                    if not ensure_permission(user, "update", "Archive Listing"):
                        st.stop()
                    try:
                        updated_details = "\n\n".join(
                            [
                                str(selected.marketplace_details or "").strip(),
                                f"[removed_unsold] Archived via Documents on {datetime.now().isoformat()} by {actor}",
                            ]
                        ).strip()
                        repo.update_listing(
                            int(selected.id),
                            {
                                "listing_status": "ended",
                                "review_status": "rejected",
                                "marketplace_details": updated_details,
                            },
                            actor=actor,
                        )
                        st.success(f"Listing #{int(selected.id)} archived/removed from active workflow.")
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Unable to archive listing: {exc}")
            st.markdown("### Preview")
            if st.button("Print / Save as PDF", key="documents_print_btn"):
                components.html(
                    """
                    <script>
                    window.print();
                    </script>
                    """,
                    height=0,
                    scrolling=False,
                )
            components.html(html, height=900, scrolling=True)
            _render_retained_artifacts(repo, source_type="Listing", source_id=int(selected.id))
            return

        post1, post2, post3 = st.columns(3)
        with post1:
            post_sale_external_order_id = st.text_input(
                "External Order ID (optional)",
                value="",
                key=f"documents_listing_post_sale_external_order_id_{int(selected.id)}",
                help="For local transactions, you can leave blank and we auto-generate a local reference.",
            )
        with post2:
            post_sale_tracking_number = st.text_input(
                "Tracking Number (optional)",
                value="",
                key=f"documents_listing_post_sale_tracking_number_{int(selected.id)}",
            )
        with post3:
            post_sale_tracking_status = st.selectbox(
                "Tracking Status",
                options=["", "needs_label", "label_created", "in_transit", "delivered", "exception"],
                index=0,
                key=f"documents_listing_post_sale_tracking_status_{int(selected.id)}",
            )

        post_sale_notes = st.text_area(
            "Sale Notes",
            value=f"Generated from Documents {doc_type} {document_number}",
            key=f"documents_listing_post_sale_notes_{int(selected.id)}",
        )
        create_linked_order = st.checkbox(
            "Also create linked Order + OrderItem",
            value=True,
            key=f"documents_listing_create_linked_order_{int(selected.id)}",
            help="Recommended for accounting traceability (Order -> OrderItem -> Sale).",
        )
        linked_order_status = st.selectbox(
            "Order Status",
            options=["paid", "pending", "fulfilled", "cancelled"],
            index=0,
            key=f"documents_listing_linked_order_status_{int(selected.id)}",
            disabled=not create_linked_order,
        )
        linked_order_notes = st.text_input(
            "Order Notes",
            value=f"Created from Documents {doc_type} {document_number}",
            key=f"documents_listing_linked_order_notes_{int(selected.id)}",
            disabled=not create_linked_order,
        )
        create_sale_from_listing_btn = st.button(
            "Create Sale Record From Invoice",
            key=f"documents_listing_create_sale_btn_{int(selected.id)}",
        )
        post_and_open_sales_btn = st.button(
            "Post & Open Sales",
            key=f"documents_listing_create_sale_open_sales_btn_{int(selected.id)}",
        )
        if create_sale_from_listing_btn or post_and_open_sales_btn:
            if not can_post_financial:
                st.error("Posting blocked by runtime role policy.")
                st.stop()
            if not ensure_permission(user, "create", "Create Sale Record From Invoice"):
                st.stop()
            try:
                local_ref = (
                    f"LOCAL-{datetime.now().strftime('%Y%m%d-%H%M%S')}-L{int(selected.id)}"
                )
                external_order_id_for_sale = (
                    str(post_sale_external_order_id or "").strip() or local_ref
                )
                created_order_id: int | None = None
                if create_linked_order:
                    existing_order = next(
                        (
                            row
                            for row in repo.list_orders()
                            if str(row.marketplace or "").strip().lower()
                            == str(selected.marketplace or "local").strip().lower()
                            and str(row.external_order_id or "").strip()
                            == external_order_id_for_sale
                        ),
                        None,
                    )
                    if existing_order is not None:
                        created_order_id = int(existing_order.id)
                    else:
                        order_items_payload = [
                            {
                                "product_id": (
                                    int(selected.product_id) if selected.product_id is not None else None
                                ),
                                "listing_id": int(selected.id),
                                "quantity": int(sum(int(item.get("qty") or 0) for item in items) or 1),
                                "unit_price": float(items[0].get("unit_price") or 0.0) if items else 0.0,
                                "line_fees": float(fees or 0.0),
                                "line_shipping": float(shipping_cost or 0.0),
                                "notes": str(linked_order_notes or "").strip(),
                            }
                        ]
                        created_order = repo.create_order(
                            marketplace=str(selected.marketplace or "local").strip().lower() or "local",
                            sold_at=datetime.combine(document_date, datetime.min.time()),
                            items=order_items_payload,
                            external_order_id=external_order_id_for_sale,
                            order_status=str(linked_order_status or "paid").strip().lower(),
                            fees=float(fees or 0.0),
                            shipping_cost=float(shipping_cost or 0.0),
                            notes=str(linked_order_notes or "").strip(),
                            actor=actor,
                        )
                        created_order_id = int(created_order.id)
                created_sale = repo.create_sale(
                    marketplace=str(selected.marketplace or "local").strip().lower() or "local",
                    sold_price=float(subtotal or 0.0),
                    fees=float(fees or 0.0),
                    shipping_cost=float(shipping_cost or 0.0),
                    quantity_sold=int(sum(int(item.get("qty") or 0) for item in items) or 1),
                    tracking_number=str(post_sale_tracking_number or "").strip(),
                    tracking_status=str(post_sale_tracking_status or "").strip().lower(),
                    order_id=created_order_id,
                    product_id=(int(selected.product_id) if selected.product_id is not None else None),
                    listing_id=int(selected.id),
                    external_order_id=external_order_id_for_sale,
                    sold_at=datetime.combine(document_date, datetime.min.time()),
                    actor=actor,
                )
                retained_artifact = repo.create_document_artifact(
                    environment=settings.app_env,
                    source_type="Listing",
                    source_id=int(selected.id),
                    doc_type=doc_type,
                    document_number=document_number,
                    artifact_kind="printable_html",
                    file_name=f"{doc_type}_{document_number}.html",
                    mime_type="text/html",
                    content_bytes=html_bytes,
                    storage_backend="db_inline",
                    storage_ref="",
                    actor=actor,
                )
                try:
                    repo.record_audit_event(
                        entity_type="sale",
                        entity_id=int(created_sale.id),
                        action="create_from_documents_listing_invoice",
                        actor=actor,
                        changes={
                            "document_number": document_number,
                            "doc_type": doc_type,
                            "listing_id": int(selected.id),
                            "tax_amount": float(tax_amount or 0.0),
                            "tax_jurisdiction": str(tax_jurisdiction or "").strip(),
                            "tax_rate_percent": float(tax_rate_percent or 0.0),
                            "tax_mode": str(tax_mode or "").strip(),
                            "taxable_subtotal": float(taxable_subtotal or 0.0),
                            "shipping_taxable": bool(shipping_is_taxable),
                            "tax_exempt_categories": sorted(exempt_categories),
                            "tax_exemption_basis": _derive_tax_exemption_basis(
                                tax_mode=tax_mode,
                                exempt_categories=exempt_categories,
                                shipping_is_taxable=bool(shipping_is_taxable),
                                use_line_item_taxability=bool(use_line_item_taxability),
                            ),
                            "order_id": created_order_id,
                            "created_linked_order": bool(create_linked_order),
                            "document_artifact_id": int(retained_artifact.id),
                            "document_artifact_sha256": str(retained_artifact.content_sha256 or ""),
                            "document_artifact_storage_ref": str(retained_artifact.storage_ref or ""),
                            "notes": str(post_sale_notes or "").strip(),
                        },
                    )
                except Exception:
                    pass
                st.success(
                    f"Created sale #{int(created_sale.id)} from listing #{int(selected.id)} "
                    f"(external order id: `{external_order_id_for_sale}`"
                    + (f", order #{int(created_order_id)}" if created_order_id is not None else "")
                    + f"). Retained immutable artifact #{int(retained_artifact.id)}."
                )
                if post_and_open_sales_btn:
                    st.session_state["sales_prefill_sale_id"] = int(created_sale.id)
                    st.session_state["workspace_handoff_target"] = "sales"
                    if hasattr(st, "switch_page"):
                        st.switch_page("pages/04_Sales.py")
            except Exception as exc:
                repo.db.rollback()
                st.error(f"Unable to create sale from invoice: {exc}")
        _render_retained_artifacts(repo, source_type="Listing", source_id=int(selected.id))

    st.markdown("### Preview")
    if st.button("Print / Save as PDF", key="documents_print_btn"):
        components.html(
            """
            <script>
            window.print();
            </script>
            """,
            height=0,
            scrolling=False,
        )
    components.html(html, height=900, scrolling=True)

    line_items_df = pd.DataFrame(items)
    line_items_csv_bytes = line_items_df.to_csv(index=False).encode("utf-8")
    line_items_xlsx_bytes = dataframe_to_xlsx_bytes(line_items_df, sheet_name="line_items")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "Download Printable HTML",
            data=html_bytes,
            file_name=f"{doc_type}_{document_number}.html",
            mime="text/html",
        )
    with c2:
        st.download_button(
            "Download Line Items CSV",
            data=line_items_csv_bytes,
            file_name=f"{doc_type}_{document_number}_line_items.csv",
            mime="text/csv",
        )
    with c3:
        st.download_button(
            "Download Line Items XLSX",
            data=line_items_xlsx_bytes,
            file_name=f"{doc_type}_{document_number}_line_items.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    st.divider()
    st.markdown("### Send via Gmail")
    google_cfg = resolve_google_workspace_config(repo)
    if not google_cfg.enabled:
        st.info(
            "Google send is disabled. Enable/configure it in `Admin -> Integrations -> Google Workspace`."
        )
    else:
        if not google_cfg.access_token:
            st.warning(
                "Google integration is enabled but no access token is set (`google_oauth_access_token`). "
                "Set token in Admin Integrations to use send."
            )
        with st.form("documents_send_gmail_form"):
            g1, g2 = st.columns(2)
            with g1:
                recipient_email = st.text_input("Recipient Email")
                gmail_subject = st.text_input(
                    "Subject",
                    value=f"{'Invoice' if doc_type == 'invoice' else 'Receipt'} {document_number} - {company_name}",
                )
            with g2:
                sender_preview = st.text_input("Sender (from runtime settings)", value=google_cfg.sender_email, disabled=True)
                plain_text_note = st.text_area(
                    "Plain Text Note (Optional)",
                    value="Please see the attached HTML content in this message.",
                    height=90,
                )
            send_gmail = st.form_submit_button("Send Document Email")
        if send_gmail:
            try:
                result = send_gmail_message(
                    config=google_cfg,
                    to_email=recipient_email.strip(),
                    subject=gmail_subject.strip(),
                    body_html=html,
                    body_text=plain_text_note.strip(),
                )
                repo.log_integration_event(
                    actor=actor,
                    integration="google_gmail",
                    action="send_document_email",
                    status="success",
                    details={
                        "document_type": doc_type,
                        "document_number": document_number,
                        "source_type": source_type.lower(),
                        "source_id": int(selected.id),
                        "recipient": recipient_email.strip(),
                        "gmail_message_id": result.get("id", ""),
                        "gmail_thread_id": result.get("threadId", ""),
                    },
                )
                st.success(
                    f"Sent via Gmail to `{recipient_email.strip()}` "
                    f"(message id: `{result.get('id', '')}`)."
                )
            except Exception as exc:
                if google_queue_enabled:
                    try:
                        queue_payload = {
                            "to_email": recipient_email.strip(),
                            "subject": gmail_subject.strip(),
                            "body_html": html,
                            "body_text": plain_text_note.strip(),
                        }
                        queued = repo.create_integration_queue_job(
                            environment=settings.app_env,
                            integration="google",
                            action="gmail_send_document_email",
                            payload_json=json.dumps(queue_payload),
                            requested_by=actor,
                            max_retries=google_queue_max_retries,
                            actor=actor,
                        )
                        st.warning(f"Gmail send failed; queued retry job #{queued.id}.")
                    except Exception:
                        pass
                try:
                    repo.log_integration_event(
                        actor=actor,
                        integration="google_gmail",
                        action="send_document_email",
                        status="failed",
                        details={
                            "document_type": doc_type,
                            "document_number": document_number,
                            "source_type": source_type.lower(),
                            "source_id": int(selected.id),
                            "recipient": recipient_email.strip(),
                            "error": str(exc),
                        },
                    )
                except Exception:
                    pass
                st.error(f"Gmail send failed: {exc}")

    st.markdown("### Create Follow-Up Calendar Event")
    if not google_cfg.enabled:
        st.info("Google Calendar actions are disabled. Enable Google integration in Admin.")
    else:
        default_summary = f"GoldenStackers follow-up: {source_label} {source_number}"
        with st.form("documents_create_calendar_event_form"):
            ce1, ce2 = st.columns(2)
            with ce1:
                event_summary = st.text_input("Event Summary", value=default_summary)
                event_date = st.date_input("Event Date", value=utc_today())
                event_start_time = st.time_input("Start Time", value=datetime.now().time().replace(second=0, microsecond=0))
            with ce2:
                event_duration_minutes = st.number_input("Duration (Minutes)", min_value=5, max_value=480, value=30, step=5)
                event_timezone = st.text_input("Time Zone", value=google_cfg.default_timezone or "America/Denver")
                event_calendar_id = st.text_input("Calendar ID", value=google_cfg.default_calendar_id or "primary")
            event_description = st.text_area(
                "Event Description",
                value=(
                    f"Document: {doc_type} {document_number}\n"
                    f"Source: {source_label} {source_number}\n"
                    f"Marketplace: {selected.marketplace}\n"
                    f"Customer Label: {customer_label}"
                ),
                height=120,
            )
            create_event_btn = st.form_submit_button("Create Calendar Event")
        if create_event_btn:
            start_dt = datetime.combine(event_date, event_start_time)
            end_dt = start_dt + timedelta(minutes=int(event_duration_minutes))
            try:
                result = create_calendar_event(
                    config=google_cfg,
                    summary=event_summary.strip(),
                    start_iso=start_dt.isoformat(),
                    end_iso=end_dt.isoformat(),
                    description=event_description.strip(),
                    timezone=event_timezone.strip() or "America/Denver",
                    calendar_id=event_calendar_id.strip() or "primary",
                )
                repo.log_integration_event(
                    actor=actor,
                    integration="google_calendar",
                    action="create_event",
                    status="success",
                    details={
                        "document_type": doc_type,
                        "document_number": document_number,
                        "source_type": source_type.lower(),
                        "source_id": int(selected.id),
                        "event_id": result.get("id", ""),
                        "event_status": result.get("status", ""),
                        "event_link": result.get("htmlLink", ""),
                    },
                )
                st.success(f"Calendar event created: `{result.get('id', '')}`")
                if result.get("htmlLink"):
                    st.markdown(f"[Open Event]({result.get('htmlLink')})")
            except Exception as exc:
                if google_queue_enabled:
                    try:
                        queue_payload = {
                            "summary": event_summary.strip(),
                            "start_iso": start_dt.isoformat(),
                            "end_iso": end_dt.isoformat(),
                            "description": event_description.strip(),
                            "timezone": event_timezone.strip() or "America/Denver",
                            "calendar_id": event_calendar_id.strip() or "primary",
                        }
                        queued = repo.create_integration_queue_job(
                            environment=settings.app_env,
                            integration="google",
                            action="calendar_create_event",
                            payload_json=json.dumps(queue_payload),
                            requested_by=actor,
                            max_retries=google_queue_max_retries,
                            actor=actor,
                        )
                        st.warning(f"Calendar create failed; queued retry job #{queued.id}.")
                    except Exception:
                        pass
                try:
                    repo.log_integration_event(
                        actor=actor,
                        integration="google_calendar",
                        action="create_event",
                        status="failed",
                        details={
                            "document_type": doc_type,
                            "document_number": document_number,
                            "source_type": source_type.lower(),
                            "source_id": int(selected.id),
                            "error": str(exc),
                        },
                    )
                except Exception:
                    pass
                st.error(f"Calendar create failed: {exc}")

    st.markdown("### Upload Artifact to Google Drive")
    if not google_cfg.enabled:
        st.info("Google Drive upload is disabled. Enable Google integration in Admin.")
    else:
        artifact_options = {
            f"{doc_type.upper()} HTML": {
                "file_name": f"{doc_type}_{document_number}.html",
                "bytes": html_bytes,
                "mime_type": "text/html",
            },
            "Line Items CSV": {
                "file_name": f"{doc_type}_{document_number}_line_items.csv",
                "bytes": line_items_csv_bytes,
                "mime_type": "text/csv",
            },
            "Line Items XLSX": {
                "file_name": f"{doc_type}_{document_number}_line_items.xlsx",
                "bytes": line_items_xlsx_bytes,
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        }
        with st.form("documents_upload_drive_form"):
            d1, d2 = st.columns(2)
            with d1:
                selected_artifact = st.selectbox("Artifact", list(artifact_options.keys()))
                drive_folder_id = st.text_input(
                    "Drive Folder ID (Optional)",
                    value=google_cfg.drive_root_folder_id or "",
                )
            with d2:
                custom_drive_name = st.text_input("Override File Name (Optional)")
                show_drive_link = st.checkbox("Show web links after upload", value=True)
            upload_drive_btn = st.form_submit_button("Upload to Google Drive")

        if upload_drive_btn:
            artifact = artifact_options[selected_artifact]
            target_name = custom_drive_name.strip() or artifact["file_name"]
            try:
                result = upload_drive_file(
                    config=google_cfg,
                    file_name=target_name,
                    file_bytes=artifact["bytes"],
                    mime_type=artifact["mime_type"],
                    folder_id=drive_folder_id.strip(),
                )
                repo.log_integration_event(
                    actor=actor,
                    integration="google_drive",
                    action="upload_document_artifact",
                    status="success",
                    details={
                        "document_type": doc_type,
                        "document_number": document_number,
                        "artifact": selected_artifact,
                        "file_name": target_name,
                        "source_type": source_type.lower(),
                        "source_id": int(selected.id),
                        "drive_file_id": result.get("id", ""),
                        "drive_web_view_link": result.get("webViewLink", ""),
                    },
                )
                st.success(f"Uploaded to Drive: `{result.get('name', target_name)}` (id: `{result.get('id', '')}`)")
                if show_drive_link:
                    if result.get("webViewLink"):
                        st.markdown(f"[Open in Drive]({result.get('webViewLink')})")
                    if result.get("webContentLink"):
                        st.markdown(f"[Direct Download]({result.get('webContentLink')})")
            except Exception as exc:
                if google_queue_enabled:
                    try:
                        queue_payload = {
                            "file_name": target_name,
                            "mime_type": artifact["mime_type"],
                            "folder_id": drive_folder_id.strip(),
                            "file_b64": base64.b64encode(artifact["bytes"]).decode("ascii"),
                        }
                        queued = repo.create_integration_queue_job(
                            environment=settings.app_env,
                            integration="google",
                            action="drive_upload_artifact",
                            payload_json=json.dumps(queue_payload),
                            requested_by=actor,
                            max_retries=google_queue_max_retries,
                            actor=actor,
                        )
                        st.warning(f"Drive upload failed; queued retry job #{queued.id}.")
                    except Exception:
                        pass
                try:
                    repo.log_integration_event(
                        actor=actor,
                        integration="google_drive",
                        action="upload_document_artifact",
                        status="failed",
                        details={
                            "document_type": doc_type,
                            "document_number": document_number,
                            "artifact": selected_artifact,
                            "source_type": source_type.lower(),
                            "source_id": int(selected.id),
                            "error": str(exc),
                        },
                    )
                except Exception:
                    pass
                st.error(f"Drive upload failed: {exc}")

    st.divider()
    st.markdown("### Manage Saved Template Profiles")
    st.caption("Store reusable branding presets by environment and document type.")
    with st.form("create_document_profile_form", clear_on_submit=True):
        p1, p2, p3 = st.columns(3)
        with p1:
            profile_name = st.text_input("Profile Name")
        with p2:
            profile_env = st.selectbox(
                "Environment",
                ["local", "dev", "prod"],
                index=["local", "dev", "prod"].index(environment) if environment in ["local", "dev", "prod"] else 0,
            )
        with p3:
            profile_doc_type = st.selectbox("Profile Doc Type", ["all", "invoice", "receipt"])
        cp1, cp2 = st.columns(2)
        with cp1:
            profile_is_default = st.checkbox("Set as Default", value=False)
        with cp2:
            profile_is_active = st.checkbox("Active", value=True)
        create_submit = st.form_submit_button("Save Current Fields as Profile")

    if create_submit:
        if not ensure_permission(user, "manage_profiles", "Create Document Profile"):
            st.stop()
        if not profile_name.strip():
            st.error("Profile name is required.")
        else:
            repo.create_document_template_profile(
                environment=profile_env,
                doc_type=profile_doc_type,
                name=profile_name.strip(),
                template_name=template_name,
                accent_color=accent_color,
                company_name=company_name.strip(),
                company_email=company_email.strip(),
                company_phone=company_phone.strip(),
                company_website=company_website.strip(),
                notes=memo_notes.strip(),
                is_default=profile_is_default,
                is_active=profile_is_active,
                actor=actor,
            )
            st.success(f"Saved profile `{profile_name}` for `{profile_env}`.")

    all_profiles = repo.list_document_template_profiles(environment=environment, active_only=False)
    if all_profiles:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": p.id,
                        "environment": p.environment,
                        "doc_type": p.doc_type,
                        "name": p.name,
                        "template_name": p.template_name,
                        "accent_color": p.accent_color,
                        "company_name": p.company_name,
                        "company_email": p.company_email,
                        "company_phone": p.company_phone,
                        "company_website": p.company_website,
                        "is_default": p.is_default,
                        "is_active": p.is_active,
                    }
                    for p in all_profiles
                ]
            ),
            use_container_width=True,
        )

        editable_map = {f"#{p.id} | {p.name} | {p.doc_type}": p for p in all_profiles}
        edit_key = st.selectbox("Edit Profile", list(editable_map.keys()), key="edit_document_profile")
        edit_row = editable_map[edit_key]
        with st.form("edit_document_profile_form"):
            ep1, ep2, ep3 = st.columns(3)
            with ep1:
                edit_name = st.text_input("Profile Name", value=edit_row.name)
            with ep2:
                edit_doc_type = st.selectbox(
                    "Doc Type",
                    ["all", "invoice", "receipt"],
                    index=["all", "invoice", "receipt"].index(edit_row.doc_type)
                    if edit_row.doc_type in ["all", "invoice", "receipt"]
                    else 0,
                )
            with ep3:
                edit_template = st.selectbox(
                    "Template",
                    list(TEMPLATES.keys()),
                    index=list(TEMPLATES.keys()).index(edit_row.template_name)
                    if edit_row.template_name in TEMPLATES
                    else 0,
                )
            ee1, ee2, ee3, ee4 = st.columns(4)
            with ee1:
                edit_accent = st.text_input("Accent Color", value=edit_row.accent_color)
            with ee2:
                edit_default = st.checkbox("Default", value=edit_row.is_default)
            with ee3:
                edit_active = st.checkbox("Active", value=edit_row.is_active)
            with ee4:
                edit_env = st.selectbox(
                    "Environment",
                    ["local", "dev", "prod"],
                    index=["local", "dev", "prod"].index(edit_row.environment)
                    if edit_row.environment in ["local", "dev", "prod"]
                    else 0,
                )
            ef1, ef2, ef3, ef4 = st.columns(4)
            with ef1:
                edit_company_name = st.text_input("Business Name", value=edit_row.company_name)
            with ef2:
                edit_company_email = st.text_input("Business Email", value=edit_row.company_email)
            with ef3:
                edit_company_phone = st.text_input("Business Phone", value=edit_row.company_phone)
            with ef4:
                edit_company_website = st.text_input("Website", value=edit_row.company_website)
            edit_notes = st.text_area("Profile Notes", value=edit_row.notes or "")
            edit_submit = st.form_submit_button("Update Profile")

        if edit_submit:
            if not ensure_permission(user, "manage_profiles", "Update Document Profile"):
                st.stop()
            repo.update_document_template_profile(
                edit_row.id,
                {
                    "name": edit_name.strip(),
                    "environment": edit_env.strip(),
                    "doc_type": edit_doc_type.strip(),
                    "template_name": edit_template.strip(),
                    "accent_color": edit_accent.strip() or "#b45309",
                    "company_name": edit_company_name.strip(),
                    "company_email": edit_company_email.strip(),
                    "company_phone": edit_company_phone.strip(),
                    "company_website": edit_company_website.strip(),
                    "notes": edit_notes.strip(),
                    "is_default": edit_default,
                    "is_active": edit_active,
                },
                actor=actor,
            )
            st.success("Profile updated.")

    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="documents",
        section_title="Workspace Feedback: Documents",
    )
