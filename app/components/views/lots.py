from datetime import datetime
import hashlib
import json
from io import BytesIO

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError

from app.components.ui_helpers import iso_or_none, to_decimal_or_none
from app.components.views.shared import render_help_panel
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_multimodal_task
from app.services.media_storage import MediaStorageService
from app.services.purchase_doc_extraction import extract_with_textract_best_effort, merge_llm_and_textract
from app.services.runtime_settings import get_runtime_bool, get_runtime_str
from app.utils.time import utc_today


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(BytesIO(file_bytes))
        chunks: list[str] = []
        for page in reader.pages[:10]:
            text = str(page.extract_text() or "").strip()
            if text:
                chunks.append(text)
        return "\n\n".join(chunks).strip()
    except Exception:
        return ""


def _extract_decimal_candidate(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in {".", "-"})
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def _derive_lot_item_subtotal(payload: dict, total: float | None) -> float | None:
    subtotal = _extract_decimal_candidate(payload.get("subtotal"))
    if subtotal is not None:
        return subtotal
    if total is None:
        return None
    components = sum(
        value or 0.0
        for value in (
            _extract_decimal_candidate(payload.get("tax")),
            _extract_decimal_candidate(payload.get("shipping")),
            _extract_decimal_candidate(payload.get("handling")),
        )
    )
    if components <= 0:
        return total
    derived = round(float(total) - float(components), 2)
    return derived if derived >= 0 else total


def _extract_invoice_date_candidate(value: object):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _extract_first_line_item(parsed_json: dict) -> dict:
    line_items = parsed_json.get("line_items")
    if not isinstance(line_items, list):
        return {}
    for item in line_items:
        if isinstance(item, dict):
            return item
    return {}


def _build_purchase_lot_updates_from_payload(parsed_json: dict) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(parsed_json, dict):
        return {}, {}
    ai_vendor = str(parsed_json.get("vendor_name") or "").strip()
    ai_invoice_date = _extract_invoice_date_candidate(parsed_json.get("invoice_date"))
    ai_total = _extract_decimal_candidate(parsed_json.get("total"))
    ai_subtotal = _derive_lot_item_subtotal(parsed_json, ai_total)
    ai_tax = _extract_decimal_candidate(parsed_json.get("tax"))
    ai_shipping = _extract_decimal_candidate(parsed_json.get("shipping"))
    ai_handling = _extract_decimal_candidate(parsed_json.get("handling"))
    apply_updates: dict[str, object] = {}
    if ai_vendor:
        apply_updates["vendor"] = ai_vendor
    if ai_invoice_date is not None:
        apply_updates["purchase_date"] = datetime.combine(ai_invoice_date, datetime.min.time())
    if ai_subtotal is not None:
        apply_updates["total_cost"] = to_decimal_or_none(ai_subtotal)
    if ai_tax is not None:
        apply_updates["total_tax_paid"] = to_decimal_or_none(ai_tax)
    if ai_shipping is not None:
        apply_updates["total_shipping_paid"] = to_decimal_or_none(ai_shipping)
    if ai_handling is not None:
        apply_updates["total_handling_paid"] = to_decimal_or_none(ai_handling)
    candidates = {
        "vendor": ai_vendor,
        "invoice_date": ai_invoice_date.isoformat() if ai_invoice_date else "n/a",
        "subtotal": ai_subtotal if ai_subtotal is not None else "n/a",
        "total": ai_total if ai_total is not None else "n/a",
        "tax": ai_tax if ai_tax is not None else "n/a",
        "shipping": ai_shipping if ai_shipping is not None else "n/a",
        "handling": ai_handling if ai_handling is not None else "n/a",
    }
    return apply_updates, candidates


def _lot_is_archived(lot) -> bool:
    raw = str(getattr(lot, "notes", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    lifecycle = parsed.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return bool(lifecycle.get("archived"))


def _lot_create_defaults(source_labels: list[str]) -> dict:
    fallback_source = source_labels[0] if source_labels else "None (one-off/manual)"
    return {
        "lots_create_lot_code": "",
        "lots_create_source_key": fallback_source,
        "lots_create_vendor": "",
        "lots_create_purchase_date": utc_today(),
        "lots_create_total_cost": 0.0,
        "lots_create_total_tax_paid": 0.0,
        "lots_create_total_shipping_paid": 0.0,
        "lots_create_total_handling_paid": 0.0,
        "lots_create_expected_total_quantity": 0,
        "lots_create_ebay_purchase": False,
        "lots_create_ebay_purchase_item_id": "",
        "lots_create_ebay_purchase_url": "",
        "lots_create_notes": "",
    }


def _normalize_lot_create_source_key(
    source_labels: list[str],
    current_value: object,
) -> str:
    if current_value in source_labels:
        return str(current_value)
    return source_labels[0] if source_labels else "None (one-off/manual)"


def _validate_lot_create_inputs(
    lot_code: str,
    ebay_purchase: bool,
    ebay_purchase_item_id: str,
) -> str | None:
    if not lot_code:
        return "Lot code is required."
    if ebay_purchase and not ebay_purchase_item_id:
        return "eBay Purchase Item ID is required when Purchased On eBay is enabled."
    return None


def _prime_lot_create_state(session_state, source_labels: list[str]) -> str:
    lot_defaults = _lot_create_defaults(source_labels)
    create_flash = str(session_state.pop("lots_create_flash_message", "") or "").strip()
    for key, default_value in lot_defaults.items():
        if key not in session_state:
            session_state[key] = default_value
    if bool(session_state.pop("lots_create_reset_requested", False)):
        for key, default_value in lot_defaults.items():
            session_state[key] = default_value
    session_state["lots_create_source_key"] = _normalize_lot_create_source_key(
        source_labels,
        session_state.get("lots_create_source_key"),
    )
    return create_flash


def _render_lot_create_state_feedback(st_module, session_state, source_labels: list[str]) -> None:
    create_flash = _prime_lot_create_state(session_state, source_labels)
    if create_flash:
        st_module.success(create_flash)


def render_lots(repo: InventoryRepository) -> None:
    st.subheader("Purchase Lots")
    st.caption("Track bulk purchases and assign individual products back to source lots.")
    render_help_panel(
        section_title="Purchase Lots",
        goal="Track bulk buys and allocate inventory items back to source purchase lots.",
        steps=[
            "Create a lot with code, vendor/source, purchase date, and total cost.",
            "Assign one or more products to each lot with quantity and optional unit cost.",
            "Use assignments for cost basis traceability and reporting.",
            "Keep lot codes unique and consistent for reconciliation.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    sources = repo.list_inventory_sources(active_only=True)
    source_options = {"None (one-off/manual)": None, **{f"{s.name} ({s.source_type})": s.id for s in sources}}
    source_labels = list(source_options.keys())

    _render_lot_create_state_feedback(st, st.session_state, source_labels)

    st.text_input("Lot Code", key="lots_create_lot_code", help="Example: LOT-20260323-A")
    st.selectbox(
        "Common Source (Optional)",
        source_labels,
        key="lots_create_source_key",
        help="Select from managed Sources, or use manual vendor text below for one-off entries.",
    )
    st.text_input(
        "Vendor Override / One-Off Source (Optional)",
        key="lots_create_vendor",
        placeholder="Optional manual value when no common source is selected.",
    )
    st.date_input("Purchase Date", key="lots_create_purchase_date")
    st.number_input(
        "Lot Item Subtotal (before tax/shipping/fees)",
        min_value=0.0,
        step=1.0,
        key="lots_create_total_cost",
        help="Enter the item subtotal only. Do not enter the order total here when tax/shipping/fees are entered separately.",
    )
    st.number_input("Total Lot Tax Paid", min_value=0.0, step=1.0, key="lots_create_total_tax_paid")
    st.number_input("Total Lot Shipping Paid", min_value=0.0, step=1.0, key="lots_create_total_shipping_paid")
    st.number_input("Total Lot Handling Paid", min_value=0.0, step=1.0, key="lots_create_total_handling_paid")
    st.number_input(
        "Expected Total Lot Quantity",
        min_value=0,
        step=1,
        key="lots_create_expected_total_quantity",
        help="Optional. Use when a whole-lot cost should be spread across items that have not all been checked in yet.",
    )
    st.checkbox(
        "Purchased On eBay",
        key="lots_create_ebay_purchase",
        help="Enable when this lot was acquired through eBay so purchase references are tracked.",
    )
    ebay_purchase_enabled = bool(st.session_state.get("lots_create_ebay_purchase"))
    st.text_input(
        "eBay Purchase Item ID",
        key="lots_create_ebay_purchase_item_id",
        disabled=not ebay_purchase_enabled,
        help="Required when eBay purchase is enabled.",
    )
    st.text_input(
        "eBay Purchase Link",
        key="lots_create_ebay_purchase_url",
        disabled=not ebay_purchase_enabled,
        help="Optional direct URL to the eBay purchase/listing.",
    )
    if not ebay_purchase_enabled:
        st.caption("Tip: Enable `Purchased On eBay` to require Item ID validation on save.")
    st.text_area("Notes", key="lots_create_notes")

    if st.button("Create Lot", key="lots_create_submit_btn"):
        lot_code = str(st.session_state.get("lots_create_lot_code") or "").strip()
        source_key = str(st.session_state.get("lots_create_source_key") or "").strip()
        vendor = str(st.session_state.get("lots_create_vendor") or "").strip()
        purchase_date = st.session_state.get("lots_create_purchase_date") or utc_today()
        total_cost = float(st.session_state.get("lots_create_total_cost") or 0.0)
        total_tax_paid = float(st.session_state.get("lots_create_total_tax_paid") or 0.0)
        total_shipping_paid = float(st.session_state.get("lots_create_total_shipping_paid") or 0.0)
        total_handling_paid = float(st.session_state.get("lots_create_total_handling_paid") or 0.0)
        expected_total_quantity = int(st.session_state.get("lots_create_expected_total_quantity") or 0)
        ebay_purchase = bool(st.session_state.get("lots_create_ebay_purchase"))
        ebay_purchase_item_id = str(st.session_state.get("lots_create_ebay_purchase_item_id") or "").strip()
        ebay_purchase_url = str(st.session_state.get("lots_create_ebay_purchase_url") or "").strip()
        notes = str(st.session_state.get("lots_create_notes") or "").strip()
        validation_error = _validate_lot_create_inputs(lot_code, ebay_purchase, ebay_purchase_item_id)
        if validation_error:
            st.error(validation_error)
        else:
            try:
                repo.create_purchase_lot(
                    lot_code=lot_code,
                    vendor=vendor,
                    purchase_date=datetime.combine(purchase_date, datetime.min.time()),
                    total_cost=to_decimal_or_none(total_cost),
                    total_tax_paid=to_decimal_or_none(total_tax_paid),
                    total_shipping_paid=to_decimal_or_none(total_shipping_paid),
                    total_handling_paid=to_decimal_or_none(total_handling_paid),
                    expected_total_quantity=expected_total_quantity or None,
                    ebay_purchase=ebay_purchase,
                    ebay_purchase_item_id=ebay_purchase_item_id,
                    ebay_purchase_url=ebay_purchase_url,
                    notes=notes,
                    source_id=source_options.get(source_key),
                )
                st.session_state["lots_create_flash_message"] = "Purchase lot created."
                st.session_state["lots_create_reset_requested"] = True
                st.rerun()
            except IntegrityError:
                repo.db.rollback()
                st.error("Lot code must be unique.")

    lots = repo.list_purchase_lots()
    products = repo.list_products()
    include_archived_lots = st.checkbox(
        "Include Archived Lots",
        value=False,
        key="lots_include_archived",
        help="Show archived lots in table and selection controls.",
    )
    visible_lots = list(lots)
    if not include_archived_lots:
        visible_lots = [lot for lot in visible_lots if not _lot_is_archived(lot)]
    if visible_lots:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": lot.id,
                        "lot_code": lot.lot_code,
                        "source": lot.source.name if lot.source else None,
                        "source_type": lot.source.source_type if lot.source else None,
                        "vendor": lot.vendor,
                        "purchase_date": iso_or_none(lot.purchase_date),
                        "total_cost": float(lot.total_cost) if lot.total_cost is not None else None,
                        "total_tax_paid": float(lot.total_tax_paid) if lot.total_tax_paid is not None else None,
                        "total_shipping_paid": float(lot.total_shipping_paid) if lot.total_shipping_paid is not None else None,
                        "total_handling_paid": float(lot.total_handling_paid) if lot.total_handling_paid is not None else None,
                        "expected_total_quantity": int(lot.expected_total_quantity)
                        if lot.expected_total_quantity is not None
                        else None,
                        "ebay_purchase": bool(getattr(lot, "ebay_purchase", False)),
                        "ebay_purchase_item_id": str(getattr(lot, "ebay_purchase_item_id", "") or ""),
                        "ebay_purchase_url": str(getattr(lot, "ebay_purchase_url", "") or ""),
                        "archived": bool(_lot_is_archived(lot)),
                        "notes": lot.notes,
                    }
                    for lot in visible_lots
                ]
            ),
            use_container_width=True,
        )
    else:
        st.info("No visible lots yet.")

    if lots:
        st.markdown("### Manage Existing Lot Lifecycle")
        lot_manage_map = {
            f"#{int(lot.id)} | {str(lot.lot_code or '').strip()} | archived={str(_lot_is_archived(lot)).lower()}": lot
            for lot in lots
        }
        selected_manage_label = st.selectbox(
            "Select Lot",
            options=list(lot_manage_map.keys()),
            key="lots_manage_select",
        )
        selected_manage_lot = lot_manage_map[selected_manage_label]
        lot_archived = bool(_lot_is_archived(selected_manage_lot))
        if lot_archived:
            st.info("Selected lot is archived.")
            if st.button("Restore Lot", key=f"restore_lot_btn_{selected_manage_lot.id}"):
                try:
                    repo.restore_purchase_lot(int(selected_manage_lot.id), actor="employee")
                    st.success(f"Restored lot #{int(selected_manage_lot.id)}.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to restore lot: {exc}")
        else:
            blockers = repo.get_purchase_lot_archive_blockers(int(selected_manage_lot.id))
            blockers_total = sum(int(v or 0) for v in blockers.values())
            if blockers_total > 0:
                st.warning(
                    "Archive preflight: linked records detected "
                    f"(assignments={int(blockers.get('product_assignments', 0))}, "
                    f"documents={int(blockers.get('purchase_documents', 0))}, "
                    f"active_products={int(blockers.get('active_products', 0))}, "
                    f"active_listings={int(blockers.get('active_listings', 0))})."
                )
            force_archive_lot = st.checkbox(
                "Force archive lot despite linked records",
                value=False,
                key=f"force_archive_lot_{selected_manage_lot.id}",
                disabled=blockers_total <= 0,
                help="Required when linked assignments/documents/active records exist.",
            )
            archive_reason = st.text_input(
                "Archive Reason (optional)",
                value="",
                key=f"archive_lot_reason_{selected_manage_lot.id}",
            )
            if st.button("Archive Lot", key=f"archive_lot_btn_{selected_manage_lot.id}"):
                try:
                    repo.archive_purchase_lot(
                        int(selected_manage_lot.id),
                        actor="employee",
                        reason=str(archive_reason or "").strip(),
                        force=bool(force_archive_lot),
                    )
                    st.success(f"Archived lot #{int(selected_manage_lot.id)}.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to archive lot: {exc}")

        st.markdown("#### Lot P/L Snapshot (Estimated)")
        st.caption(
            "Estimated lot performance using linked product assignments and proportional sales attribution. "
            "Use for operational signal; exact accounting depends on final COGS policy."
        )
        if not hasattr(repo, "lot_profitability_snapshot"):
            st.info("Lot P/L snapshot is unavailable in this runtime context.")
        else:
            try:
                snapshot = repo.lot_profitability_snapshot(int(selected_manage_lot.id))
                summary = dict(snapshot.get("summary") or {})
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("Assigned Qty", int(summary.get("assigned_qty") or 0))
                s2.metric("Allocated Landed Cost", f"${float(summary.get('allocated_landed_cost') or 0):,.2f}")
                s3.metric(
                    "Est. Net Before COGS After Returns",
                    f"${float(summary.get('estimated_net_before_cogs') or 0):,.2f}",
                )
                s4.metric(
                    "Est. Profit After Returns",
                    f"${float(summary.get('estimated_lot_profit') or 0):,.2f}",
                )
                returns_refund_total = float(summary.get("returns_refund_total") or 0.0)
                returns_cogs_reversal = float(summary.get("returns_cogs_reversal") or 0.0)
                returns_profit_impact = float(summary.get("returns_profit_impact") or 0.0)
                if returns_refund_total or returns_cogs_reversal:
                    r1, r2, r3, r4 = st.columns(4)
                    r1.metric(
                        "Profit Before Returns",
                        f"${float(summary.get('estimated_lot_profit_before_returns') or 0):,.2f}",
                    )
                    r2.metric("Return Refunds", f"${returns_refund_total:,.2f}")
                    r3.metric("Return COGS Reversal", f"${returns_cogs_reversal:,.2f}")
                    r4.metric("Return Profit Impact", f"${returns_profit_impact:,.2f}")
                    st.caption(
                        "Lot profit after returns = profit before returns - return refunds + returned COGS reversal."
                    )
                cost_source = str(summary.get("cost_source") or "").strip()
                source_totals = dict(summary.get("cost_source_totals") or {})
                if source_totals:
                    source_text = "; ".join(
                        f"{source} ${float(total or 0.0):,.2f}"
                        for source, total in sorted(source_totals.items())
                    )
                    st.caption(f"Sold COGS source mix for this lot: {source_text}.")
                elif cost_source:
                    st.caption(f"Lot cost basis source: {cost_source}. No sold COGS has been attributed yet.")
                lot_rows = list(snapshot.get("rows") or [])
                if lot_rows:
                    st.dataframe(pd.DataFrame(lot_rows), use_container_width=True)
                else:
                    st.info("No product assignments found for this lot yet.")
            except Exception as exc:
                st.error(f"Unable to load lot P/L snapshot: {exc}")

    st.markdown("### Incoming Purchase Invoices / Documents")
    st.caption(
        "Upload PDF or take/upload invoice photos, run AI extraction, store originals, and link to lot/product/source."
    )
    storage = MediaStorageService()
    if not storage.enabled:
        st.warning("S3 storage is not configured. Configure storage before uploading purchase documents.")
    else:
        try:
            storage.ensure_bucket()
        except Exception as exc:
            st.error(f"Unable to initialize S3 bucket for purchase documents: {exc}")
            storage = None
    lot_options = {"None": None, **{f"{lot.lot_code} | {lot.vendor}": lot.id for lot in visible_lots}}
    product_options = {"None": None, **{f"{p.sku} | {p.title}": p.id for p in products}}
    source_options_for_docs = {"None": None, **{f"{s.name} ({s.source_type})": s.id for s in sources}}

    with st.form("upload_purchase_document_form", clear_on_submit=False):
        d1, d2, d3 = st.columns(3)
        with d1:
            doc_kind = st.selectbox(
                "Document Kind",
                options=["incoming_invoice", "purchase_order", "receipt", "other"],
                index=0,
            )
            doc_title = st.text_input(
                "Document Title",
                value="",
                placeholder="Example: APMEX invoice #12345",
            )
            link_lot_key = st.selectbox("Link Lot (optional)", list(lot_options.keys()))
        with d2:
            link_product_key = st.selectbox("Link Product (optional)", list(product_options.keys()))
            link_source_key = st.selectbox("Link Source (optional)", list(source_options_for_docs.keys()))
            run_ai_extract = st.checkbox(
                "Run AI extraction",
                value=True,
                help="Extract structured fields from the uploaded invoice/receipt content.",
            )
            textract_enabled = get_runtime_bool(repo, "purchase_doc_textract_enabled", True)
            extraction_mode_options = ["llm"]
            if textract_enabled:
                extraction_mode_options.extend(["textract", "both"])
            extraction_mode = st.selectbox(
                "Extraction Mode",
                options=extraction_mode_options,
                format_func=lambda mode: {
                    "llm": "LLM Multimodal",
                    "textract": "AWS Textract",
                    "both": "Both (merge)",
                }.get(str(mode), str(mode)),
                index=0,
                disabled=not run_ai_extract,
                help="LLM uses current AI runtime profile. Textract uses AWS Textract AnalyzeExpense.",
            )
        with d3:
            invoice_file = st.file_uploader(
                "Upload PDF/Image",
                type=["pdf", "png", "jpg", "jpeg", "webp"],
                key="lots_purchase_document_uploader",
                help="PDF and common image formats are supported.",
            )
            with st.expander("Camera (Optional)", expanded=False):
                camera_capture = st.camera_input(
                    "Or Take Picture",
                    key="lots_purchase_document_camera_input",
                )
        submit_doc = st.form_submit_button("Store Purchase Document")

    if submit_doc:
        if storage is None:
            st.error("Purchase document upload is unavailable until S3 storage is configured.")
        else:
            selected_file = camera_capture if camera_capture is not None else invoice_file
            if selected_file is None:
                st.error("Provide a PDF/image file or camera capture.")
            else:
                try:
                    file_name = str(getattr(selected_file, "name", "") or "").strip() or "purchase_document.bin"
                    file_bytes = bytes(selected_file.getvalue() or b"")
                    if not file_bytes:
                        raise ValueError("Uploaded file is empty.")
                    content_type = str(getattr(selected_file, "type", "") or "").strip() or "application/octet-stream"
                    upload_result = storage.upload_file(file_name=file_name, file_bytes=file_bytes, content_type=content_type)
                    sha256 = hashlib.sha256(file_bytes).hexdigest()
                    ai_payload: dict = {}
                    ai_summary = ""
                    llm_summary = ""
                    textract_summary = ""
                    textract_error = ""
                    if run_ai_extract:
                        if extraction_mode in {"llm", "both"}:
                            default_sys = get_runtime_str(
                                repo,
                                "purchase_doc_ai_system_message",
                                "You extract structured data from purchase invoices and receipts. "
                                "Return strict JSON only.",
                            )
                            default_instruction = get_runtime_str(
                                repo,
                                "purchase_doc_ai_instruction",
                                "Extract fields as JSON: vendor_name, invoice_number, invoice_date, due_date, "
                                "line_items[{description, quantity, unit_price, line_total}], subtotal, tax, "
                                "shipping, handling, total, currency, payment_method, account_reference, notes, confidence.",
                            )
                            instruction = default_instruction
                            additional_images = None
                            image_bytes = None
                            image_content_type = "image/jpeg"
                            if content_type.startswith("image/"):
                                image_bytes = file_bytes
                                image_content_type = content_type
                            elif content_type == "application/pdf":
                                pdf_text = _extract_pdf_text(file_bytes)
                                instruction = (
                                    default_instruction
                                    + "\n\nThis upload is a PDF purchase document. Extract from this OCR/text content:\n\n"
                                    + (pdf_text[:12000] if pdf_text else "[no extractable text found]")
                                    + "\n\nIf text is limited, return best-effort partial JSON and indicate low confidence."
                                )
                            else:
                                instruction = (
                                    default_instruction
                                    + "\n\nAnalyze the provided file context and return best-effort JSON."
                                )
                            ai_result = execute_multimodal_task(
                                repo,
                                tool_name="purchase_invoice_extractor",
                                system_message=default_sys,
                                instruction=instruction,
                                image_bytes=image_bytes,
                                image_content_type=image_content_type,
                                additional_images=additional_images,
                                workflow="intake",
                                context={
                                    "doc_kind": doc_kind,
                                    "file_name": file_name,
                                    "content_type": content_type,
                                },
                            )
                            llm_summary = str(ai_result.text or "").strip()
                            ai_payload = _extract_json_object(llm_summary)
                        if extraction_mode in {"textract", "both"}:
                            textract_payload, textract_summary, textract_error = extract_with_textract_best_effort(
                                file_bytes=file_bytes,
                                content_type=content_type,
                            )
                            if extraction_mode == "textract":
                                ai_payload = textract_payload
                            elif not textract_error:
                                ai_payload = merge_llm_and_textract(ai_payload, textract_payload)
                        if extraction_mode == "llm":
                            ai_summary = llm_summary
                        elif extraction_mode == "textract":
                            ai_summary = textract_summary
                        else:
                            ai_summary = json.dumps(
                                {
                                    "provider": "llm+aws_textract",
                                    "llm_summary": llm_summary,
                                    "textract_summary": textract_summary,
                                    "merged_payload": ai_payload,
                                },
                                indent=2,
                            )
                    created = repo.create_purchase_document(
                        document_kind=doc_kind,
                        title=(doc_title or "").strip() or file_name,
                        original_filename=file_name,
                        content_type=content_type,
                        size_bytes=len(file_bytes),
                        content_sha256=sha256,
                        s3_bucket=upload_result.bucket,
                        s3_key=upload_result.key,
                        s3_url=upload_result.url,
                        lot_id=lot_options.get(link_lot_key),
                        product_id=product_options.get(link_product_key),
                        source_id=source_options_for_docs.get(link_source_key),
                        ai_extracted_json=json.dumps(ai_payload) if ai_payload else "{}",
                        ai_summary=ai_summary,
                        uploaded_by="employee",
                        actor="employee",
                    )
                    if textract_error:
                        st.warning(
                            "Purchase document was stored, but Textract extraction was skipped/failed: "
                            f"{textract_error}"
                        )
                    auto_apply_enabled = bool(
                        get_runtime_bool(repo, "purchase_doc_auto_apply_linked_lot_fields", False)
                    )
                    linked_lot_id = lot_options.get(link_lot_key)
                    if auto_apply_enabled and linked_lot_id is not None and isinstance(ai_payload, dict):
                        apply_updates, _ = _build_purchase_lot_updates_from_payload(ai_payload)
                        if apply_updates:
                            repo.update_purchase_lot(
                                int(linked_lot_id),
                                apply_updates,
                                actor="employee",
                            )
                            repo.record_audit_event(
                                entity_type="purchase_document",
                                entity_id=int(created.id),
                                action="auto_apply_extracted_fields_to_lot",
                                actor="employee",
                                changes={
                                    "workflow": "lots_purchase_document_upload",
                                    "mode": "auto",
                                    "lot_id": int(linked_lot_id),
                                    "applied_fields": sorted(apply_updates.keys()),
                                },
                            )
                            st.success(
                                "Auto-applied extracted purchase-document fields to "
                                f"linked lot #{int(linked_lot_id)} ({int(len(apply_updates))} field(s))."
                            )
                    st.success(f"Stored purchase document #{int(created.id)}.")
                except Exception as exc:
                    st.error(f"Unable to store purchase document: {exc}")

    purchase_docs = repo.list_purchase_documents(limit=300)
    if purchase_docs:
        rows = []
        for doc in purchase_docs:
            ai_structured = {}
            try:
                ai_structured = json.loads(str(doc.ai_extracted_json or "{}"))
            except Exception:
                ai_structured = {}
            rows.append(
                {
                    "id": int(doc.id),
                    "created_at": iso_or_none(doc.created_at),
                    "kind": str(doc.document_kind or ""),
                    "title": str(doc.title or ""),
                    "filename": str(doc.original_filename or ""),
                    "content_type": str(doc.content_type or ""),
                    "size_bytes": int(doc.size_bytes or 0),
                    "lot_id": doc.lot_id,
                    "product_id": doc.product_id,
                    "source_id": doc.source_id,
                    "vendor_name_ai": str(ai_structured.get("vendor_name") or ""),
                    "invoice_number_ai": str(ai_structured.get("invoice_number") or ""),
                    "invoice_date_ai": str(ai_structured.get("invoice_date") or ""),
                    "total_ai": ai_structured.get("total"),
                    "currency_ai": str(ai_structured.get("currency") or ""),
                    "s3_url": str(doc.s3_url or ""),
                    "sha256": str(doc.content_sha256 or ""),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        doc_map = {f"#{int(d.id)} | {str(d.document_kind or '')} | {str(d.original_filename or '')}": d for d in purchase_docs}
        selected_doc_key = st.selectbox("Purchase Document Detail", options=list(doc_map.keys()), key="lots_purchase_doc_detail_select")
        selected_doc = doc_map[selected_doc_key]
        dcol1, dcol2 = st.columns(2)
        with dcol1:
            st.write(f"**S3 URL:** {selected_doc.s3_url}")
            st.write(f"**SHA-256:** `{selected_doc.content_sha256}`")
            st.write(f"**Linked Lot:** {selected_doc.lot_id}")
            st.write(f"**Linked Product:** {selected_doc.product_id}")
            st.write(f"**Linked Source:** {selected_doc.source_id}")
        with dcol2:
            parsed_json = _extract_json_object(str(selected_doc.ai_extracted_json or ""))
            if not parsed_json:
                parsed_json = _extract_json_object(str(selected_doc.ai_summary or ""))
            if parsed_json:
                st.json(parsed_json)
                st.markdown("#### Create Product From Purchase Document")
                if selected_doc.product_id is None:
                    line_items_raw = parsed_json.get("line_items")
                    line_items: list[dict] = []
                    if isinstance(line_items_raw, list):
                        line_items = [row for row in line_items_raw if isinstance(row, dict)]
                    if not line_items:
                        fallback_line = _extract_first_line_item(parsed_json)
                        if fallback_line:
                            line_items = [fallback_line]
                    if line_items:
                        line_item_options = []
                        for idx, row in enumerate(line_items):
                            desc = str(row.get("description") or "").strip() or f"Line {idx + 1}"
                            qty = _extract_decimal_candidate(row.get("quantity"))
                            unit_price = _extract_decimal_candidate(row.get("unit_price"))
                            line_total = _extract_decimal_candidate(row.get("line_total"))
                            line_item_options.append(
                                {
                                    "idx": idx,
                                    "description": desc,
                                    "quantity": int(qty) if qty is not None else 1,
                                    "unit_price": float(unit_price) if unit_price is not None else 0.0,
                                    "line_total": float(line_total) if line_total is not None else 0.0,
                                }
                            )
                        selected_line_idx = st.selectbox(
                            "Select Extracted Line Item",
                            options=list(range(len(line_item_options))),
                            format_func=lambda i: (
                                f"{int(i) + 1}. {line_item_options[int(i)]['description']} | "
                                f"qty={line_item_options[int(i)]['quantity']} | "
                                f"unit=${line_item_options[int(i)]['unit_price']:.2f} | "
                                f"line=${line_item_options[int(i)]['line_total']:.2f}"
                            ),
                            key=f"lots_doc_line_item_select_{int(selected_doc.id)}",
                        )
                        selected_line = line_item_options[int(selected_line_idx)]
                    else:
                        selected_line = {
                            "description": "",
                            "quantity": 1,
                            "unit_price": 0.0,
                            "line_total": 0.0,
                        }
                    ai_line_description = str(selected_line.get("description") or "").strip()
                    ai_line_qty = int(selected_line.get("quantity") or 1)
                    ai_line_unit_price = float(selected_line.get("unit_price") or 0.0)
                    ai_line_total = float(selected_line.get("line_total") or 0.0)
                    if ai_line_qty > 0 and ai_line_unit_price <= 0 and ai_line_total > 0:
                        ai_line_unit_price = ai_line_total / float(ai_line_qty)
                    ai_vendor_hint = str(parsed_json.get("vendor_name") or "").strip()
                    default_title = ai_line_description or str(selected_doc.title or "").strip() or "Imported Product"
                    default_category = "bullion"
                    default_qty = max(1, int(ai_line_qty or 1))
                    default_cost = max(0.0, float(ai_line_unit_price or 0.0))
                    default_notes = (
                        f"Created from purchase document #{int(selected_doc.id)}"
                        + (f" ({selected_doc.original_filename})" if str(selected_doc.original_filename or "").strip() else "")
                        + (f"\nVendor (AI): {ai_vendor_hint}" if ai_vendor_hint else "")
                    )
                    with st.form(f"lots_create_product_from_doc_form_{int(selected_doc.id)}"):
                        cp1, cp2 = st.columns(2)
                        with cp1:
                            create_product_sku = st.text_input(
                                "SKU",
                                value=f"DOC-{int(selected_doc.id)}-{utc_today().strftime('%m%d')}",
                            )
                            create_product_title = st.text_input("Title", value=default_title)
                            create_product_category = st.text_input("Category", value=default_category)
                            create_product_qty = st.number_input(
                                "Initial Quantity",
                                min_value=0,
                                value=int(default_qty),
                                step=1,
                            )
                        with cp2:
                            create_product_cost = st.number_input(
                                "Acquisition Unit Cost",
                                min_value=0.0,
                                value=float(default_cost),
                                step=0.01,
                            )
                            create_product_metal = st.text_input("Metal Type (optional)", value="")
                            create_product_weight_oz = st.number_input(
                                "Weight Ounces (optional)",
                                min_value=0.0,
                                value=0.0,
                                step=0.01,
                            )
                            assign_to_linked_lot = st.checkbox(
                                "Assign to linked lot now",
                                value=bool(selected_doc.lot_id is not None and int(default_qty) > 0),
                                disabled=selected_doc.lot_id is None,
                            )
                        create_product_description = st.text_area("Description", value=default_notes)
                        create_product_submit = st.form_submit_button("Create Product + Link Document")
                    if create_product_submit:
                        try:
                            created_product = repo.create_product(
                                sku=str(create_product_sku or "").strip(),
                                title=str(create_product_title or "").strip(),
                                category=str(create_product_category or "").strip() or "bullion",
                                description=str(create_product_description or "").strip(),
                                metal_type=str(create_product_metal or "").strip(),
                                weight_oz=to_decimal_or_none(create_product_weight_oz),
                                acquisition_cost=to_decimal_or_none(create_product_cost),
                                current_quantity=int(create_product_qty),
                                product_cost=to_decimal_or_none(create_product_cost),
                                lot_id=(int(selected_doc.lot_id) if bool(assign_to_linked_lot) and selected_doc.lot_id is not None else None),
                                actor="employee",
                            )
                            repo.update_purchase_document(
                                int(selected_doc.id),
                                {"product_id": int(created_product.id)},
                                actor="employee",
                            )
                            st.success(
                                f"Created product #{int(created_product.id)} (`{created_product.sku}`) and linked purchase document."
                            )
                            st.rerun()
                        except IntegrityError:
                            repo.db.rollback()
                            st.error("SKU already exists. Choose a different SKU.")
                        except Exception as exc:
                            repo.db.rollback()
                            st.error(f"Unable to create product from document: {exc}")

                    st.markdown("#### Bulk Create Products From Line Items")
                    if line_item_options:
                        bulk_seed_rows = []
                        for idx, item in enumerate(line_item_options):
                            seed_sku = (
                                f"DOC-{int(selected_doc.id)}-"
                                f"{utc_today().strftime('%m%d')}-{int(idx) + 1:02d}"
                            )
                            bulk_seed_rows.append(
                                {
                                    "create": False,
                                    "sku": seed_sku,
                                    "title": str(item.get("description") or f"Line {idx + 1}").strip(),
                                    "category": "bullion",
                                    "quantity": max(1, int(item.get("quantity") or 1)),
                                    "unit_cost": max(0.0, float(item.get("unit_price") or 0.0)),
                                    "description": (
                                        f"Created from purchase document #{int(selected_doc.id)}"
                                        + (
                                            f" ({selected_doc.original_filename})"
                                            if str(selected_doc.original_filename or "").strip()
                                            else ""
                                        )
                                    ),
                                    "assign_to_linked_lot": bool(selected_doc.lot_id is not None),
                                }
                            )
                        bulk_df = pd.DataFrame(bulk_seed_rows)
                        edited_bulk_df = st.data_editor(
                            bulk_df,
                            use_container_width=True,
                            hide_index=True,
                            num_rows="fixed",
                            column_config={
                                "create": st.column_config.CheckboxColumn("Create"),
                                "sku": st.column_config.TextColumn("SKU"),
                                "title": st.column_config.TextColumn("Title"),
                                "category": st.column_config.TextColumn("Category"),
                                "quantity": st.column_config.NumberColumn(
                                    "Qty",
                                    min_value=1,
                                    step=1,
                                ),
                                "unit_cost": st.column_config.NumberColumn(
                                    "Unit Cost",
                                    min_value=0.0,
                                    step=0.01,
                                    format="$%.2f",
                                ),
                                "description": st.column_config.TextColumn("Description"),
                                "assign_to_linked_lot": st.column_config.CheckboxColumn(
                                    "Assign Lot",
                                    disabled=selected_doc.lot_id is None,
                                ),
                            },
                            key=f"lots_bulk_doc_create_editor_{int(selected_doc.id)}",
                        )
                        if st.button(
                            "Create Selected Products",
                            key=f"lots_bulk_doc_create_submit_{int(selected_doc.id)}",
                        ):
                            success_rows: list[str] = []
                            error_rows: list[str] = []
                            first_created_product_id: int | None = None
                            for ridx, row in edited_bulk_df.iterrows():
                                if not bool(row.get("create")):
                                    continue
                                sku_val = str(row.get("sku") or "").strip()
                                title_val = str(row.get("title") or "").strip()
                                category_val = str(row.get("category") or "").strip() or "bullion"
                                qty_val = max(1, int(float(row.get("quantity") or 1)))
                                unit_cost_val = max(0.0, float(row.get("unit_cost") or 0.0))
                                desc_val = str(row.get("description") or "").strip()
                                assign_lot = bool(row.get("assign_to_linked_lot")) and selected_doc.lot_id is not None
                                try:
                                    created_product = repo.create_product(
                                        sku=sku_val,
                                        title=title_val,
                                        category=category_val,
                                        description=desc_val,
                                        metal_type="",
                                        weight_oz=None,
                                        acquisition_cost=to_decimal_or_none(unit_cost_val),
                                        current_quantity=qty_val,
                                        product_cost=to_decimal_or_none(unit_cost_val),
                                        lot_id=(int(selected_doc.lot_id) if assign_lot else None),
                                        actor="employee",
                                    )
                                    if first_created_product_id is None:
                                        first_created_product_id = int(created_product.id)
                                    success_rows.append(
                                        f"row {int(ridx) + 1}: #{int(created_product.id)} `{created_product.sku}`"
                                    )
                                except IntegrityError:
                                    repo.db.rollback()
                                    error_rows.append(f"row {int(ridx) + 1}: duplicate SKU `{sku_val}`")
                                except Exception as exc:
                                    repo.db.rollback()
                                    error_rows.append(f"row {int(ridx) + 1}: {exc}")
                            if first_created_product_id is not None:
                                try:
                                    repo.update_purchase_document(
                                        int(selected_doc.id),
                                        {"product_id": int(first_created_product_id)},
                                        actor="employee",
                                    )
                                except Exception:
                                    repo.db.rollback()
                            if success_rows:
                                st.success("Created products: " + "; ".join(success_rows))
                            if error_rows:
                                st.warning("Some rows failed: " + "; ".join(error_rows))
                            if success_rows:
                                st.rerun()
                    else:
                        st.caption("No extracted line items found for bulk conversion.")
                else:
                    st.caption(f"Already linked to product #{int(selected_doc.product_id)}.")
                st.markdown("#### Create Lot From Purchase Document")
                if selected_doc.lot_id is None:
                    ai_vendor_for_create = str(parsed_json.get("vendor_name") or "").strip()
                    ai_invoice_number_for_create = str(parsed_json.get("invoice_number") or "").strip()
                    ai_invoice_date_for_create = _extract_invoice_date_candidate(parsed_json.get("invoice_date"))
                    ai_total_for_create = _extract_decimal_candidate(parsed_json.get("total"))
                    ai_subtotal_for_create = _derive_lot_item_subtotal(parsed_json, ai_total_for_create)
                    ai_tax_for_create = _extract_decimal_candidate(parsed_json.get("tax"))
                    ai_shipping_for_create = _extract_decimal_candidate(parsed_json.get("shipping"))
                    ai_handling_for_create = _extract_decimal_candidate(parsed_json.get("handling"))
                    auto_lot_code = (
                        f"LOT-{utc_today().strftime('%Y%m%d')}-DOC{int(selected_doc.id)}"
                    )
                    default_lot_note = (
                        f"Created from purchase document #{int(selected_doc.id)} "
                        f"({str(selected_doc.original_filename or '').strip()})"
                    )
                    with st.form(f"lots_create_lot_from_doc_form_{int(selected_doc.id)}"):
                        cl1, cl2 = st.columns(2)
                        with cl1:
                            create_lot_code = st.text_input("Lot Code", value=auto_lot_code)
                            create_lot_vendor = st.text_input("Vendor", value=ai_vendor_for_create)
                            create_lot_purchase_date = st.date_input(
                                "Purchase Date",
                                value=(ai_invoice_date_for_create or utc_today()),
                            )
                            create_lot_total = st.number_input(
                                "Lot Item Subtotal (before tax/shipping/fees)",
                                min_value=0.0,
                                value=float(ai_subtotal_for_create or 0.0),
                                step=1.0,
                                help=(
                                    "Enter the item subtotal only. Do not enter the order total here when "
                                    "tax/shipping/fees are entered separately."
                                ),
                            )
                        with cl2:
                            create_lot_tax = st.number_input(
                                "Total Lot Tax Paid",
                                min_value=0.0,
                                value=float(ai_tax_for_create or 0.0),
                                step=1.0,
                            )
                            create_lot_shipping = st.number_input(
                                "Total Lot Shipping Paid",
                                min_value=0.0,
                                value=float(ai_shipping_for_create or 0.0),
                                step=1.0,
                            )
                            create_lot_handling = st.number_input(
                                "Total Lot Handling Paid",
                                min_value=0.0,
                                value=float(ai_handling_for_create or 0.0),
                                step=1.0,
                            )
                            create_lot_expected_total_quantity = st.number_input(
                                "Expected Total Lot Quantity",
                                min_value=0,
                                value=0,
                                step=1,
                                help="Optional. Use when this document covers items that have not all been checked in yet.",
                            )
                            create_lot_source_key = st.selectbox(
                                "Source (optional)",
                                options=list(source_options_for_docs.keys()),
                                index=(
                                    list(source_options_for_docs.keys()).index("None")
                                    if "None" in source_options_for_docs
                                    else 0
                                ),
                            )
                        create_lot_notes = st.text_area(
                            "Lot Notes",
                            value=(
                                default_lot_note
                                + (
                                    f"\nInvoice #: {ai_invoice_number_for_create}"
                                    if ai_invoice_number_for_create
                                    else ""
                                )
                            ).strip(),
                        )
                        create_lot_submit = st.form_submit_button("Create Lot + Link Document")
                    if create_lot_submit:
                        try:
                            created_lot = repo.create_purchase_lot(
                                lot_code=str(create_lot_code or "").strip(),
                                vendor=str(create_lot_vendor or "").strip(),
                                purchase_date=datetime.combine(create_lot_purchase_date, datetime.min.time()),
                                total_cost=to_decimal_or_none(create_lot_total),
                                total_tax_paid=to_decimal_or_none(create_lot_tax),
                                total_shipping_paid=to_decimal_or_none(create_lot_shipping),
                                total_handling_paid=to_decimal_or_none(create_lot_handling),
                                expected_total_quantity=int(create_lot_expected_total_quantity or 0) or None,
                                notes=str(create_lot_notes or "").strip(),
                                source_id=source_options_for_docs.get(create_lot_source_key),
                            )
                            repo.update_purchase_document(
                                int(selected_doc.id),
                                {
                                    "lot_id": int(created_lot.id),
                                    "source_id": (
                                        source_options_for_docs.get(create_lot_source_key)
                                        if source_options_for_docs.get(create_lot_source_key) is not None
                                        else selected_doc.source_id
                                    ),
                                },
                                actor="employee",
                            )
                            st.success(
                                f"Created lot #{int(created_lot.id)} (`{created_lot.lot_code}`) and linked purchase document."
                            )
                            st.rerun()
                        except IntegrityError:
                            repo.db.rollback()
                            st.error("Lot code already exists. Choose a different code.")
                        except Exception as exc:
                            repo.db.rollback()
                            st.error(f"Unable to create lot from document: {exc}")
                else:
                    st.caption(f"Already linked to lot #{int(selected_doc.lot_id)}.")
                st.markdown("#### Apply AI Data To Linked Lot")
                linked_lot_id = int(selected_doc.lot_id) if selected_doc.lot_id is not None else None
                if linked_lot_id is None:
                    st.caption("Link this document to a lot to enable lot auto-fill.")
                else:
                    apply_updates, candidates = _build_purchase_lot_updates_from_payload(parsed_json)

                    st.caption(
                        "Detected candidates: "
                        + ", ".join(
                            [
                                f"vendor={str(candidates.get('vendor') or 'n/a')}",
                                f"invoice_date={str(candidates.get('invoice_date') or 'n/a')}",
                                f"total={str(candidates.get('total') or 'n/a')}",
                                f"tax={str(candidates.get('tax') or 'n/a')}",
                                f"shipping={str(candidates.get('shipping') or 'n/a')}",
                                f"handling={str(candidates.get('handling') or 'n/a')}",
                            ]
                        )
                    )
                    if st.button(
                        "Apply AI Fields To Linked Lot",
                        key=f"lots_apply_ai_to_linked_lot_{int(selected_doc.id)}",
                        disabled=not bool(apply_updates),
                    ):
                        try:
                            repo.update_purchase_lot(
                                int(linked_lot_id),
                                apply_updates,
                                actor="employee",
                            )
                            repo.record_audit_event(
                                entity_type="purchase_document",
                                entity_id=int(selected_doc.id),
                                action="manual_apply_extracted_fields_to_lot",
                                actor="employee",
                                changes={
                                    "workflow": "lots_purchase_document_detail",
                                    "mode": "manual",
                                    "lot_id": int(linked_lot_id),
                                    "applied_fields": sorted(apply_updates.keys()),
                                },
                            )
                            st.success(f"Applied AI extracted fields to lot #{int(linked_lot_id)}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to apply AI fields to lot: {exc}")
            elif str(selected_doc.ai_summary or "").strip():
                st.text_area("AI Summary", value=str(selected_doc.ai_summary or ""), height=180)
            else:
                st.caption("No AI extraction stored for this document.")
    else:
        st.caption("No purchase documents uploaded yet.")

    active_lots = [lot for lot in lots if not _lot_is_archived(lot)]
    if not active_lots or not products:
        return

    st.markdown("### Assign Existing Product To Lot")
    with st.form("assign_product_lot_form", clear_on_submit=True):
        lot_map = {f"{lot.lot_code} | {lot.vendor}": lot.id for lot in active_lots}
        product_map = {f"{p.sku} | {p.title}": p.id for p in products}
        lot_key = st.selectbox("Lot", list(lot_map.keys()))
        product_key = st.selectbox("Product", list(product_map.keys()))
        quantity = st.number_input("Quantity Acquired", min_value=1, value=1, step=1)
        unit_cost = st.number_input("Unit Cost (Optional)", min_value=0.0, value=0.0, step=1.0)
        allocated_cost = st.number_input(
            "Allocated Lot Cost Total (Optional)",
            min_value=0.0,
            value=0.0,
            step=1.0,
            help="Use for mixed lots when this product/quantity should receive a known dollar share of the whole lot cost.",
        )
        allocation_weight = st.number_input(
            "Allocation Weight (Optional)",
            min_value=0.0,
            value=0.0,
            step=1.0,
            help="Use for mixed lots when whole-lot cost should be split proportionally by estimated value/share.",
        )
        unit_tax_paid = st.number_input("Unit Tax Paid (Optional)", min_value=0.0, value=0.0, step=1.0)
        unit_shipping_paid = st.number_input("Unit Shipping Paid (Optional)", min_value=0.0, value=0.0, step=1.0)
        unit_handling_paid = st.number_input("Unit Handling Paid (Optional)", min_value=0.0, value=0.0, step=1.0)
        acquired_date = st.date_input("Acquired Date", value=utc_today(), key="assign_acquired_date")
        if st.form_submit_button("Assign Product To Lot"):
            try:
                repo.assign_product_to_lot(
                    product_id=product_map[product_key],
                    lot_id=lot_map[lot_key],
                    quantity_acquired=int(quantity),
                    unit_cost=to_decimal_or_none(unit_cost),
                    allocated_cost=to_decimal_or_none(allocated_cost),
                    allocation_weight=to_decimal_or_none(allocation_weight),
                    unit_tax_paid=to_decimal_or_none(unit_tax_paid),
                    unit_shipping_paid=to_decimal_or_none(unit_shipping_paid),
                    unit_handling_paid=to_decimal_or_none(unit_handling_paid),
                    acquired_at=datetime.combine(acquired_date, datetime.min.time()),
                )
                st.success("Product assigned to lot.")
            except IntegrityError:
                repo.db.rollback()
                st.error("This product is already assigned to this lot.")

    assignments = repo.list_product_lot_assignments()
    if assignments:
        assignment_lookup = {
            f"#{int(a.id)} | product {int(a.product_id)} | lot {int(a.lot_id)} | qty {int(a.quantity_acquired or 0)}": a
            for a in assignments
        }
        with st.expander("Edit Lot Assignment Cost Allocation", expanded=False):
            selected_assignment_key = st.selectbox(
                "Assignment",
                options=list(assignment_lookup.keys()),
                key="lots_edit_assignment_key",
            )
            selected_assignment = assignment_lookup[selected_assignment_key]
            with st.form("lots_edit_assignment_form"):
                ea1, ea2, ea3 = st.columns(3)
                with ea1:
                    edit_quantity = st.number_input(
                        "Quantity Acquired",
                        min_value=1,
                        value=int(selected_assignment.quantity_acquired or 1),
                        step=1,
                    )
                    edit_unit_cost = st.number_input(
                        "Unit Cost",
                        min_value=0.0,
                        value=float(selected_assignment.unit_cost or 0.0),
                        step=0.01,
                    )
                    edit_allocated_cost = st.number_input(
                        "Allocated Lot Cost Total",
                        min_value=0.0,
                        value=float(selected_assignment.allocated_cost or 0.0),
                        step=0.01,
                    )
                with ea2:
                    edit_unit_tax = st.number_input(
                        "Unit Tax Paid",
                        min_value=0.0,
                        value=float(selected_assignment.unit_tax_paid or 0.0),
                        step=0.01,
                    )
                    edit_unit_shipping = st.number_input(
                        "Unit Shipping Paid",
                        min_value=0.0,
                        value=float(selected_assignment.unit_shipping_paid or 0.0),
                        step=0.01,
                    )
                    edit_unit_handling = st.number_input(
                        "Unit Handling Paid",
                        min_value=0.0,
                        value=float(selected_assignment.unit_handling_paid or 0.0),
                        step=0.01,
                    )
                with ea3:
                    edit_allocation_weight = st.number_input(
                        "Allocation Weight",
                        min_value=0.0,
                        value=float(selected_assignment.allocation_weight or 0.0),
                        step=0.01,
                    )
                    edit_acquired_date = st.date_input(
                        "Acquired Date",
                        value=(selected_assignment.acquired_at.date() if selected_assignment.acquired_at else utc_today()),
                        key=f"lots_edit_assignment_acquired_date_{int(selected_assignment.id)}",
                    )
                if st.form_submit_button("Save Assignment Allocation"):
                    try:
                        repo.update_product_lot_assignment(
                            int(selected_assignment.id),
                            {
                                "quantity_acquired": int(edit_quantity),
                                "unit_cost": to_decimal_or_none(edit_unit_cost),
                                "unit_tax_paid": to_decimal_or_none(edit_unit_tax),
                                "unit_shipping_paid": to_decimal_or_none(edit_unit_shipping),
                                "unit_handling_paid": to_decimal_or_none(edit_unit_handling),
                                "allocated_cost": to_decimal_or_none(edit_allocated_cost),
                                "allocation_weight": to_decimal_or_none(edit_allocation_weight),
                                "acquired_at": datetime.combine(edit_acquired_date, datetime.min.time()),
                            },
                            actor="employee",
                        )
                        st.success("Lot assignment allocation updated.")
                        st.rerun()
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Unable to update assignment allocation: {exc}")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": a.id,
                        "product_id": a.product_id,
                        "lot_id": a.lot_id,
                        "quantity_acquired": a.quantity_acquired,
                        "unit_cost": float(a.unit_cost) if a.unit_cost is not None else None,
                        "unit_tax_paid": float(a.unit_tax_paid) if a.unit_tax_paid is not None else None,
                        "unit_shipping_paid": float(a.unit_shipping_paid) if a.unit_shipping_paid is not None else None,
                        "unit_handling_paid": float(a.unit_handling_paid) if a.unit_handling_paid is not None else None,
                        "allocated_cost": float(a.allocated_cost) if a.allocated_cost is not None else None,
                        "allocation_weight": float(a.allocation_weight) if a.allocation_weight is not None else None,
                        "allocated_tax_paid": float(a.allocated_tax_paid) if a.allocated_tax_paid is not None else None,
                        "allocated_shipping_paid": float(a.allocated_shipping_paid) if a.allocated_shipping_paid is not None else None,
                        "allocated_handling_paid": float(a.allocated_handling_paid) if a.allocated_handling_paid is not None else None,
                        "acquired_at": iso_or_none(a.acquired_at),
                    }
                    for a in assignments
                ]
            ),
            use_container_width=True,
        )
