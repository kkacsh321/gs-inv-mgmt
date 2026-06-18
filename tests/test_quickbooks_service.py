from datetime import datetime
import unittest

from app.services.quickbooks import (
    QuickBooksClearingConfig,
    quickbooks_config_from_runtime,
    quickbooks_manual_journal_packet,
    quickbooks_non_order_fee_purchase_payload,
    quickbooks_order_fee_purchase_payload,
    quickbooks_payload_preview_records,
    quickbooks_payload_validation_issues,
    quickbooks_rows_from_actual_economics,
    quickbooks_sales_receipt_payload,
)


class QuickBooksServiceTests(unittest.TestCase):
    def test_config_from_runtime_uses_runtime_overrides(self):
        class Row:
            def __init__(self, value: str):
                self.value = value
                self.value_type = "str"

        class Repo:
            def get_runtime_setting(self, *, environment: str, key: str, active_only: bool = True):
                values = {
                    "quickbooks_clearing_account_ref": "Runtime Clearing",
                    "quickbooks_sales_income_account_ref": "Runtime Sales",
                    "quickbooks_shipping_income_account_ref": "Runtime Shipping Income",
                    "quickbooks_ebay_customer_ref": "Runtime eBay Customer",
                    "quickbooks_doc_number_prefix": "RT",
                }
                if key not in values:
                    return None
                return Row(values[key])

        config = quickbooks_config_from_runtime(Repo())

        self.assertEqual(config.clearing_account_ref, "Runtime Clearing")
        self.assertEqual(config.sales_income_account_ref, "Runtime Sales")
        self.assertEqual(config.shipping_income_account_ref, "Runtime Shipping Income")
        self.assertEqual(config.ebay_customer_ref, "Runtime eBay Customer")
        self.assertEqual(config.doc_number_prefix, "RT")
        self.assertEqual(config.ebay_vendor_ref, "eBay Vendor")

    def test_sales_receipt_payload_uses_clearing_account_and_ebay_customer(self):
        payload = quickbooks_sales_receipt_payload(
            {
                "external_order_id": "13-14720-44255",
                "sold_at": datetime(2026, 6, 9, 6, 48, 24),
                "sku": "DOC-3-0407",
                "quantity_sold": 20,
                "gross_amount": "79.95",
            }
        )

        self.assertEqual(payload["TxnDate"], "2026-06-09")
        self.assertEqual(payload["CustomerRef"], {"value": "eBay Sales Customer"})
        self.assertEqual(payload["PaymentMethodRef"], {"value": "eBay Payout"})
        self.assertEqual(payload["DepositToAccountRef"], {"value": "Custom App Clearing Account"})
        self.assertTrue(payload["DocNumber"].startswith("GS-SAL-"))
        line = payload["Line"][0]
        self.assertEqual(line["DetailType"], "SalesItemLineDetail")
        self.assertEqual(line["Amount"], "79.95")
        detail = line["SalesItemLineDetail"]
        self.assertEqual(detail["ItemRef"], {"value": "DOC-3-0407"})
        self.assertEqual(detail["Qty"], 20)
        self.assertEqual(detail["UnitPrice"], "4.00")
        self.assertEqual(detail["TaxCodeRef"], {"value": "NON"})
        self.assertEqual(quickbooks_payload_validation_issues(payload), [])

    def test_order_fee_purchase_payload_reduces_clearing_account(self):
        payload = quickbooks_order_fee_purchase_payload(
            {
                "external_order_id": "13-14720-44255",
                "fee_date": "2026-06-09T06:48:24",
                "fee_amount": "8.63",
            }
        )

        self.assertEqual(payload["TxnDate"], "2026-06-09")
        self.assertEqual(payload["AccountRef"], {"value": "Custom App Clearing Account"})
        self.assertEqual(payload["EntityRef"], {"value": "eBay Vendor"})
        self.assertTrue(payload["DocNumber"].startswith("GS-FEE-"))
        line = payload["Line"][0]
        self.assertEqual(line["Amount"], "8.63")
        self.assertEqual(
            line["AccountBasedExpenseLineDetail"]["AccountRef"],
            {"value": "Merchant Account Fees"},
        )
        self.assertEqual(quickbooks_payload_validation_issues(payload), [])

    def test_non_order_fee_payload_routes_shipping_and_subscription_categories(self):
        shipping = quickbooks_non_order_fee_purchase_payload(
            {"payout_id": "PAYOUT-1", "fee_type": "shipping_label", "amount": "6.07", "created_at": "2026-06-09"}
        )
        subscription = quickbooks_non_order_fee_purchase_payload(
            {"payout_id": "PAYOUT-2", "fee_type": "store_subscription", "amount": "21.95", "created_at": "2026-06-09"}
        )

        self.assertEqual(
            shipping["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"],
            {"value": "eBay Shipping Expense"},
        )
        self.assertEqual(
            subscription["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"],
            {"value": "eBay Subscription Expense"},
        )

    def test_custom_refs_and_validation_issues_for_missing_sku(self):
        config = QuickBooksClearingConfig(
            clearing_account_ref="QBO Clearing",
            ebay_customer_ref="QBO eBay Customer",
            ebay_payment_method_ref="QBO eBay Payout",
        )
        payload = quickbooks_sales_receipt_payload(
            {
                "id": 123,
                "sold_at": "2026-06-09",
                "sku": "",
                "quantity_sold": 1,
                "gross_amount": "5.00",
            },
            config=config,
        )

        self.assertEqual(payload["CustomerRef"], {"value": "QBO eBay Customer"})
        self.assertEqual(payload["DepositToAccountRef"], {"value": "QBO Clearing"})
        issues = quickbooks_payload_validation_issues(payload)
        self.assertIn("line_0_missing_item_ref", issues)

    def test_rows_from_actual_economics_and_preview_records(self):
        rows = quickbooks_rows_from_actual_economics(
            [
                {
                    "sale_id": 66,
                    "sold_at": "2026-06-09T06:48:24",
                    "external_order_id": "13-14720-44255",
                    "marketplace": "ebay",
                    "sku": "DOC-3-0407",
                    "product_title": "1 oz American Prospector Copper Coin",
                    "qty": 20,
                    "sold_price": 79.95,
                    "allocated_fee_actual": 8.63,
                    "allocated_shipping_charged": 13.41,
                    "allocated_shipping_actual": 7.9,
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "actual_shipping_source": "normalized_order_finance_entries_shipping_label_sum",
                }
            ]
        )

        self.assertEqual(rows[0]["txn_date"], "2026-06-09")
        self.assertEqual(rows[0]["doc_number"], "13-14720-44255")
        self.assertEqual(rows[0]["item_sku"], "DOC-3-0407")
        self.assertEqual(rows[0]["quantity"], 20)
        self.assertEqual(rows[0]["amount"], "79.95")
        self.assertEqual(rows[0]["fees"], "8.63")
        self.assertEqual(rows[0]["shipping_label_cost"], "7.90")

        records = quickbooks_payload_preview_records(rows)
        self.assertEqual([r["action"] for r in records], [
            "sales_receipt",
            "order_fee_purchase",
            "shipping_label_purchase",
        ])
        self.assertTrue(all(r["validation_status"] == "ok" for r in records))
        self.assertTrue(all(r["payload_sha256"] for r in records))
        self.assertEqual(records[0]["payload"]["Line"][0]["SalesItemLineDetail"]["Qty"], 20)

    def test_manual_journal_packet_balances_gross_shipping_fees_and_labels(self):
        packet = quickbooks_manual_journal_packet(
            [
                {
                    "sale_id": 66,
                    "sold_at": "2026-06-09T06:48:24",
                    "external_order_id": "13-14720-44255",
                    "marketplace": "ebay",
                    "sku": "DOC-3-0407",
                    "qty": 20,
                    "sold_price": "79.95",
                    "allocated_fee_actual": "8.63",
                    "allocated_shipping_charged": "13.41",
                    "allocated_shipping_actual": "7.90",
                }
            ],
            period_label="June 2026",
        )

        summary = packet["summary"]
        self.assertEqual(summary["gross_sales"], "79.95")
        self.assertEqual(summary["shipping_charged"], "13.41")
        self.assertEqual(summary["income_receivable"], "93.36")
        self.assertEqual(summary["marketplace_deductions"], "16.53")
        self.assertEqual(summary["expected_net_payout"], "76.83")
        self.assertEqual(summary["total_debits"], summary["total_credits"])
        self.assertEqual(summary["out_of_balance"], "0.00")
        self.assertEqual([row["source"] for row in packet["journal_rows"]], [
            "gross_sales_plus_shipping_charged",
            "gross_sales",
            "shipping_charged",
            "ebay_fees",
            "shipping_label_spend",
            "marketplace_deductions",
        ])
        self.assertTrue(packet["evidence_sha256"])


if __name__ == "__main__":
    unittest.main()
