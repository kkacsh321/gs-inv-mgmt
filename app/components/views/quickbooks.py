from __future__ import annotations

import calendar
from datetime import datetime
import json

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.config import settings
from app.repository import InventoryRepository
from app.services.quickbooks import (
    QuickBooksClearingConfig,
    quickbooks_config_from_runtime,
    quickbooks_manual_journal_packet,
)
from app.services.runtime_settings import get_runtime_bool, get_runtime_int
from app.services.sync_jobs import execute_sync_job, is_sync_job_enabled


def _runtime_map(repo: InventoryRepository) -> dict[str, object]:
    return {
        str(row.key): row
        for row in repo.list_runtime_settings(environment=settings.app_env, active_only=False)
    }


def _runtime_value(rows: dict[str, object], key: str, default: str = "") -> str:
    row = rows.get(key)
    if row is None:
        return default
    return str(getattr(row, "value", "") or default)


def _save_runtime_setting(
    repo: InventoryRepository,
    *,
    key: str,
    value: str,
    value_type: str,
    description: str,
    actor: str,
) -> None:
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key=key,
        value=str(value or "").strip(),
        value_type=value_type,
        description=description,
        is_active=True,
        actor=actor,
    )


def _recent_quickbooks_run_rows(repo: InventoryRepository) -> list[dict[str, object]]:
    rows = []
    for run in repo.list_sync_runs(provider="quickbooks", limit=25):
        rows.append(
            {
                "id": int(getattr(run, "id", 0) or 0),
                "job_name": str(getattr(run, "job_name", "") or ""),
                "status": str(getattr(run, "status", "") or ""),
                "started_at": getattr(run, "started_at", None),
                "completed_at": getattr(run, "completed_at", None),
                "processed": int(getattr(run, "records_processed", 0) or 0),
                "failed": int(getattr(run, "records_failed", 0) or 0),
                "notes": str(getattr(run, "notes", "") or ""),
            }
        )
    return rows


def render_quickbooks(repo: InventoryRepository) -> None:
    user = current_user()
    if not ensure_permission(user, "manage_settings", "Manage QuickBooks"):
        return

    st.title("QuickBooks Online")
    st.caption(
        "GoldenStackers owns the QuickBooks flow directly. The goal is to reproduce the clean payout-summary behavior "
        "of accounting-first tools inside this app, while preserving SKU-level inventory evidence and avoiding buyer "
        "profile bloat."
    )

    defaults = QuickBooksClearingConfig()
    runtime_rows = _runtime_map(repo)

    st.markdown("### Integration Strategy")
    st.info(
        "Primary path: GoldenStackers custom QuickBooks integration using `Custom App Clearing Account`. "
        "Reference behavior: A2X/Link My Books style payout-clean accounting, but implemented in this app. "
        "Fallback path: monthly manual journal entry from eBay financial summary when automation is not ready."
    )
    st.markdown(
        "- Use one generic `eBay Sales Customer`, not one QuickBooks customer per buyer.\n"
        "- Post gross marketplace sales as QBO `SalesReceipt` payloads into the clearing account.\n"
        "- Post eBay fees, shipping labels, subscriptions, and adjustments as QBO `Purchase` payloads paid from the clearing account.\n"
        "- Match real bank-feed deposits against the clearing account instead of letting app writes touch the live checking ledger."
    )

    st.markdown("### Clearing Account Settings")
    with st.form("quickbooks_clearing_settings_form"):
        qb1, qb2, qb3 = st.columns(3)
        with qb1:
            qb_clearing_account = st.text_input(
                "Clearing Account Ref",
                value=_runtime_value(runtime_rows, "quickbooks_clearing_account_ref", defaults.clearing_account_ref),
                help="QBO asset account used for generated SalesReceipt deposits and fee Purchase payments.",
            )
            qb_sales_income_account = st.text_input(
                "Sales Income Account Ref",
                value=_runtime_value(
                    runtime_rows,
                    "quickbooks_sales_income_account_ref",
                    defaults.sales_income_account_ref,
                ),
                help="Income account used by the manual journal fallback for marketplace gross item sales.",
            )
            qb_shipping_income_account = st.text_input(
                "Shipping Income Account Ref",
                value=_runtime_value(
                    runtime_rows,
                    "quickbooks_shipping_income_account_ref",
                    defaults.shipping_income_account_ref,
                ),
                help="Income account used by the manual journal fallback for buyer-paid shipping.",
            )
            qb_payment_method = st.text_input(
                "Payment Method Ref",
                value=_runtime_value(runtime_rows, "quickbooks_ebay_payment_method_ref", defaults.ebay_payment_method_ref),
            )
            qb_doc_prefix = st.text_input(
                "DocNumber Prefix",
                value=_runtime_value(runtime_rows, "quickbooks_doc_number_prefix", defaults.doc_number_prefix),
                max_chars=8,
            )
        with qb2:
            qb_customer = st.text_input(
                "eBay Customer Ref",
                value=_runtime_value(runtime_rows, "quickbooks_ebay_customer_ref", defaults.ebay_customer_ref),
            )
            qb_vendor = st.text_input(
                "eBay Vendor Ref",
                value=_runtime_value(runtime_rows, "quickbooks_ebay_vendor_ref", defaults.ebay_vendor_ref),
            )
            qb_tax_code = st.text_input(
                "eBay Tax Code Ref",
                value=_runtime_value(runtime_rows, "quickbooks_tax_code_ref", defaults.tax_code_ref),
                help="Use NON/Tax Exempt for marketplace-facilitator eBay orders unless your accountant says otherwise.",
            )
        with qb3:
            qb_fee_account = st.text_input(
                "Fee Expense Account Ref",
                value=_runtime_value(
                    runtime_rows,
                    "quickbooks_ebay_fee_expense_account_ref",
                    defaults.ebay_fee_expense_account_ref,
                ),
            )
            qb_shipping_account = st.text_input(
                "Shipping Expense Account Ref",
                value=_runtime_value(
                    runtime_rows,
                    "quickbooks_ebay_shipping_expense_account_ref",
                    defaults.ebay_shipping_expense_account_ref,
                ),
            )
            qb_subscription_account = st.text_input(
                "Subscription Expense Account Ref",
                value=_runtime_value(
                    runtime_rows,
                    "quickbooks_ebay_subscription_expense_account_ref",
                    defaults.ebay_subscription_expense_account_ref,
                ),
            )
            qb_adjustment_account = st.text_input(
                "Other Adjustment Expense Account Ref",
                value=_runtime_value(
                    runtime_rows,
                    "quickbooks_ebay_adjustment_expense_account_ref",
                    defaults.ebay_adjustment_expense_account_ref,
                ),
            )
        save_qbo_settings = st.form_submit_button("Save QuickBooks Settings")

    if save_qbo_settings:
        settings_to_save = {
            "quickbooks_clearing_account_ref": (
                qb_clearing_account,
                "QBO asset account used as the app clearing account for marketplace sales and deductions.",
            ),
            "quickbooks_sales_income_account_ref": (
                qb_sales_income_account,
                "QBO income account ref/name for marketplace gross item sales in manual journal fallback.",
            ),
            "quickbooks_shipping_income_account_ref": (
                qb_shipping_income_account,
                "QBO income account ref/name for buyer-paid shipping income in manual journal fallback.",
            ),
            "quickbooks_ebay_customer_ref": (qb_customer, "Generic QBO customer ref/name for eBay sales."),
            "quickbooks_ebay_vendor_ref": (qb_vendor, "Generic QBO vendor ref/name for eBay fees."),
            "quickbooks_ebay_payment_method_ref": (qb_payment_method, "QBO payment method ref/name for eBay sales."),
            "quickbooks_ebay_fee_expense_account_ref": (qb_fee_account, "QBO expense account ref/name for eBay order fees."),
            "quickbooks_ebay_shipping_expense_account_ref": (
                qb_shipping_account,
                "QBO expense account ref/name for eBay shipping-label deductions.",
            ),
            "quickbooks_ebay_subscription_expense_account_ref": (
                qb_subscription_account,
                "QBO expense account ref/name for eBay store subscription deductions.",
            ),
            "quickbooks_ebay_adjustment_expense_account_ref": (
                qb_adjustment_account,
                "QBO expense account ref/name for other non-order eBay deductions.",
            ),
            "quickbooks_tax_code_ref": (qb_tax_code, "QBO tax code ref/name for eBay marketplace orders."),
            "quickbooks_doc_number_prefix": (qb_doc_prefix, "QBO DocNumber prefix for generated app payloads."),
        }
        try:
            for key, (value, description) in settings_to_save.items():
                _save_runtime_setting(
                    repo,
                    key=key,
                    value=str(value or "").strip(),
                    value_type="str",
                    description=description,
                    actor=user.username,
                )
            st.success("QuickBooks settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save QuickBooks settings: {exc}")

    st.markdown("### Dry-Run Export Job")
    with st.form("quickbooks_export_job_form"):
        enabled = st.checkbox(
            "Enable QuickBooks export dry-run evidence job",
            value=bool(is_sync_job_enabled("quickbooks_export", repo=repo)),
        )
        lookback_days = st.number_input(
            "Default Lookback Days",
            min_value=1,
            max_value=366,
            value=max(1, min(366, get_runtime_int(repo, "sync_job_quickbooks_export_lookback_days", 30))),
            step=1,
        )
        save_job_settings = st.form_submit_button("Save Job Settings")
    if save_job_settings:
        try:
            _save_runtime_setting(
                repo,
                key="sync_job_quickbooks_export_enabled",
                value="true" if enabled else "false",
                value_type="bool",
                description="Enable/disable QuickBooks dry-run export evidence job.",
                actor=user.username,
            )
            _save_runtime_setting(
                repo,
                key="sync_job_quickbooks_export_lookback_days",
                value=str(int(lookback_days)),
                value_type="int",
                description="Default lookback window for QuickBooks dry-run export payload evidence.",
                actor=user.username,
            )
            st.success("QuickBooks job settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save QuickBooks job settings: {exc}")

    run_cols = st.columns([1, 1, 2])
    with run_cols[0]:
        run_lookback = st.number_input(
            "Run Lookback Days",
            min_value=1,
            max_value=366,
            value=max(1, min(366, get_runtime_int(repo, "sync_job_quickbooks_export_lookback_days", 30))),
            step=1,
            key="quickbooks_run_lookback_days",
        )
    with run_cols[1]:
        run_now = st.button("Run Dry-Run Export", type="primary")
    with run_cols[2]:
        st.caption("Dry-run export records sync-run evidence only. It does not call Intuit APIs.")

    if run_now:
        try:
            result = execute_sync_job(
                repo,
                job_name="quickbooks_export",
                actor=user.username,
                lookback_days=int(run_lookback),
            )
            st.success(
                f"QuickBooks dry-run completed: run #{result.get('run_id')} status={result.get('status')} "
                f"processed={result.get('processed')} failed={result.get('failed')}."
            )
            if result.get("evidence_sha256"):
                st.code(str(result.get("evidence_sha256")), language="text")
        except Exception as exc:
            st.error(f"Unable to run QuickBooks dry-run export: {exc}")

    recent_rows = _recent_quickbooks_run_rows(repo)
    st.markdown("### Recent QuickBooks Sync Runs")
    if recent_rows:
        st.dataframe(pd.DataFrame(recent_rows), use_container_width=True, hide_index=True)
        selected_run_id = st.selectbox(
            "Inspect Run Events",
            options=[int(row["id"]) for row in recent_rows],
            format_func=lambda run_id: f"Run #{run_id}",
        )
        event_rows = [
            {
                "id": int(getattr(event, "id", 0) or 0),
                "action": str(getattr(event, "action", "") or ""),
                "status": str(getattr(event, "status", "") or ""),
                "message": str(getattr(event, "message", "") or ""),
                "created_at": getattr(event, "created_at", None),
                "payload_json": str(getattr(event, "payload_json", "") or ""),
            }
            for event in repo.list_sync_events(int(selected_run_id), limit=50)
        ]
        if event_rows:
            st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)
            with st.expander("Latest Event Payload JSON", expanded=False):
                latest_payload = event_rows[-1].get("payload_json") or "{}"
                try:
                    st.json(json.loads(str(latest_payload)))
                except Exception:
                    st.code(str(latest_payload), language="json")
    else:
        st.info("No QuickBooks sync runs recorded yet.")

    st.markdown("### Manual Journal Fallback")
    st.caption(
        "Use this when automation is not ready or your accountant wants month-end summary entries instead of API sync."
    )
    journal_cols = st.columns([1, 1, 2])
    today = datetime.now().date()
    with journal_cols[0]:
        journal_year = st.number_input(
            "Journal Year",
            min_value=2020,
            max_value=2100,
            value=today.year,
            step=1,
        )
    with journal_cols[1]:
        journal_month = st.selectbox(
            "Journal Month",
            options=list(range(1, 13)),
            index=today.month - 1,
            format_func=lambda month: calendar.month_name[int(month)],
        )
    with journal_cols[2]:
        st.markdown(
            "This worksheet follows the clearing-account fallback: debit clearing for gross sales plus buyer-paid "
            "shipping, credit income accounts, debit eBay fees/label spend, then credit clearing for deductions."
        )

    month_start = datetime(int(journal_year), int(journal_month), 1)
    month_end = datetime(
        int(journal_year),
        int(journal_month),
        calendar.monthrange(int(journal_year), int(journal_month))[1],
        23,
        59,
        59,
    )
    period_label = f"{calendar.month_name[int(journal_month)]} {int(journal_year)}"
    try:
        actual_rows: list[dict[str, object]] = []
        if hasattr(repo, "report_sales_actual_econ_rows"):
            actual_rows = repo.report_sales_actual_econ_rows(start_dt=month_start, end_dt=month_end)
        packet = quickbooks_manual_journal_packet(
            actual_rows,
            config=quickbooks_config_from_runtime(repo),
            period_label=period_label,
        )
        summary = packet.get("summary", {})
        metric_cols = st.columns(4)
        metric_cols[0].metric("Source Sales", str(summary.get("source_rows", 0)))
        metric_cols[1].metric("Gross + Shipping", f"${summary.get('income_receivable', '0.00')}")
        metric_cols[2].metric("Fees + Labels", f"${summary.get('marketplace_deductions', '0.00')}")
        metric_cols[3].metric("Expected Net Payout", f"${summary.get('expected_net_payout', '0.00')}")
        st.caption(f"Manual journal evidence SHA-256: {packet.get('evidence_sha256', '')}")
        journal_df = pd.DataFrame(packet.get("journal_rows") or [])
        if not journal_df.empty:
            st.dataframe(journal_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Manual Journal CSV",
                data=journal_df.to_csv(index=False),
                file_name=f"quickbooks_manual_journal_{int(journal_year)}_{int(journal_month):02d}.csv",
                mime="text/csv",
            )
        else:
            st.info("No journal rows for this month yet.")
    except Exception as exc:
        st.error(f"Unable to build manual journal fallback packet: {exc}")

    st.markdown(
        "1. Use this worksheet or the eBay Financial Summary for the month.\n"
        "2. Create one monthly journal entry in QuickBooks when API sync is not ready.\n"
        "3. Match real bank-feed deposits against the clearing account, not against raw sales directly.\n"
        "4. Confirm marketplace-facilitator sales tax treatment and entity/tax-return mapping with your tax advisor.\n"
        "5. Keep Reports and Taxes exports as the close evidence packet."
    )
