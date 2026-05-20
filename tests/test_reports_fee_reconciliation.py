import json
import unittest
from types import SimpleNamespace

from app.services.fee_reconciliation import (
    build_ebay_fee_reconciliation_rows,
    parse_listing_fee_estimate_payload,
)


class ReportsFeeReconciliationTests(unittest.TestCase):
    def test_parse_listing_fee_estimate_payload_invalid_shapes(self) -> None:
        self.assertEqual(parse_listing_fee_estimate_payload(None), {})
        self.assertEqual(parse_listing_fee_estimate_payload("{bad json"), {})
        self.assertEqual(parse_listing_fee_estimate_payload(json.dumps(["not", "a", "dict"])), {})
        self.assertEqual(parse_listing_fee_estimate_payload(json.dumps({"ebay_publish": []})), {})
        self.assertEqual(parse_listing_fee_estimate_payload(json.dumps({"ebay_publish": {"fee_estimate": []}})), {})

    def test_parse_listing_fee_estimate_payload(self) -> None:
        listing = SimpleNamespace(
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {"estimated_total_fees": 9.99, "quantity": 2},
                    }
                }
            )
        )
        payload = parse_listing_fee_estimate_payload(listing.marketplace_details)
        self.assertEqual(payload.get("estimated_total_fees"), 9.99)
        self.assertEqual(payload.get("quantity"), 2)

    def test_build_rows_scales_estimated_fee(self) -> None:
        listing = SimpleNamespace(
            external_listing_id="123",
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {"estimated_total_fees": 10.0, "quantity": 2},
                    }
                }
            ),
        )
        sale = SimpleNamespace(
            id=1,
            sold_at=None,
            external_order_id="ORDER-1",
            listing_id=10,
            listing=listing,
            product=SimpleNamespace(sku="SKU1", title="Product 1"),
            quantity_sold=1,
            sold_price=20.0,
            fees=6.0,
            marketplace="ebay",
        )
        rows = build_ebay_fee_reconciliation_rows([sale])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["estimated_fee_scaled"], 5.0)
        self.assertEqual(row["actual_fee"], 6.0)
        self.assertEqual(row["actual_fee_source"], "sale_fees_field")
        self.assertEqual(row["variance_actual_minus_estimate"], 1.0)
        self.assertTrue(row["fee_estimate_present"])
        self.assertIn("implied_final_value_rate_percent", row)

    def test_build_rows_includes_estimate_component_fields(self) -> None:
        listing = SimpleNamespace(
            external_listing_id="123",
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {
                            "estimated_total_fees": 5.0,
                            "quantity": 1,
                            "final_value_rate_percent": 13.25,
                            "final_value_fixed_usd": 0.30,
                            "payment_rate_percent": 2.90,
                            "payment_fixed_usd": 0.30,
                            "promoted_rate_percent": 0.0,
                        },
                    }
                }
            ),
        )
        sale = SimpleNamespace(
            id=2,
            sold_at=None,
            external_order_id="ORDER-2",
            listing_id=10,
            listing=listing,
            product=SimpleNamespace(sku="SKU2", title="Product 2"),
            quantity_sold=1,
            sold_price=20.0,
            fees=3.55,
            marketplace="ebay",
        )
        rows = build_ebay_fee_reconciliation_rows([sale])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["estimate_final_value_rate_percent"], 13.25, places=2)
        self.assertAlmostEqual(row["estimate_payment_rate_percent"], 2.9, places=2)
        self.assertAlmostEqual(row["estimate_final_value_fixed_usd"], 0.30, places=2)
        self.assertAlmostEqual(row["estimate_payment_fixed_usd"], 0.30, places=2)
        self.assertAlmostEqual(row["estimate_promoted_rate_percent"], 0.0, places=2)

    def test_prefers_order_fee_breakdown_marketplace_fee_when_present(self) -> None:
        listing = SimpleNamespace(
            external_listing_id="XYZ",
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {
                            "estimated_total_fees": 4.0,
                            "quantity": 1,
                            "final_value_rate_percent": 13.25,
                            "final_value_fixed_usd": 0.30,
                            "payment_rate_percent": 2.9,
                            "payment_fixed_usd": 0.30,
                            "promoted_rate_percent": 0.0,
                        }
                    }
                }
            ),
        )
        order = SimpleNamespace(notes="Imported from eBay sync pull. fee_breakdown_json={\"total_marketplace_fee\":2.15}")
        sale = SimpleNamespace(
            id=3,
            sold_at=None,
            external_order_id="ORDER-3",
            listing_id=12,
            listing=listing,
            product=SimpleNamespace(sku="SKU3", title="Product 3"),
            quantity_sold=1,
            sold_price=20.0,
            fees=3.90,
            marketplace="ebay",
            order=order,
        )
        rows = build_ebay_fee_reconciliation_rows([sale])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["actual_fee"], 2.15)
        self.assertEqual(row["actual_fee_source"], "order_fee_breakdown_total_marketplace_fee")
        self.assertEqual(row["sale_fee_field"], 3.9)
        self.assertEqual(row["order_fee_breakdown_total_marketplace_fee"], 2.15)
        self.assertTrue(row["order_fee_breakdown_present"])

    def test_prefers_normalized_order_finance_entries_when_present(self) -> None:
        listing = SimpleNamespace(
            external_listing_id="XYZ",
            marketplace_details=json.dumps({"ebay_publish": {"fee_estimate": {"estimated_total_fees": 4.0, "quantity": 1}}}),
        )
        order = SimpleNamespace(
            notes='Imported from eBay sync pull. fee_breakdown_json={"total_marketplace_fee":2.15}',
            finance_entries=[
                SimpleNamespace(entry_kind="marketplace_fee", amount=1.25),
                SimpleNamespace(entry_kind="marketplace_fee", amount=1.75),
                SimpleNamespace(entry_kind="shipping_label", amount=4.25),
            ],
        )
        sale = SimpleNamespace(
            id=30,
            sold_at=None,
            external_order_id="ORDER-30",
            listing_id=12,
            listing=listing,
            product=SimpleNamespace(sku="SKU30", title="Product 30"),
            quantity_sold=1,
            sold_price=20.0,
            fees=9.99,
            marketplace="ebay",
            order=order,
        )

        rows = build_ebay_fee_reconciliation_rows([sale])

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["actual_fee"], 3.0)
        self.assertEqual(row["actual_fee_source"], "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(row["normalized_order_finance_marketplace_fee_total"], 3.0)
        self.assertTrue(row["normalized_order_finance_marketplace_fee_present"])
        self.assertEqual(row["order_fee_breakdown_total_marketplace_fee"], 2.15)

    def test_non_ebay_sales_are_ignored(self) -> None:
        sale = SimpleNamespace(
            marketplace="local",
            listing=None,
            fees=0.0,
            quantity_sold=1,
            sold_price=1.0,
            id=1,
            sold_at=None,
            external_order_id="",
            listing_id=None,
            product=None,
        )
        rows = build_ebay_fee_reconciliation_rows([sale])
        self.assertEqual(rows, [])

    def test_order_fee_breakdown_notes_malformed_falls_back_to_sale_fees(self) -> None:
        listing = SimpleNamespace(
            external_listing_id="XYZ",
            marketplace_details=json.dumps({"ebay_publish": {"fee_estimate": {"estimated_total_fees": 0}}}),
        )
        order = SimpleNamespace(notes="Imported from eBay sync pull. fee_breakdown_json={not json}; extra=true")
        sale = SimpleNamespace(
            id=4,
            sold_at=None,
            external_order_id="ORDER-4",
            listing_id=12,
            listing=listing,
            product=SimpleNamespace(sku="SKU4", title="Product 4"),
            quantity_sold=1,
            sold_price=10.0,
            fees=1.23,
            marketplace="ebay",
            order=order,
        )
        rows = build_ebay_fee_reconciliation_rows([sale])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["actual_fee_source"], "sale_fees_field")
        self.assertEqual(row["actual_fee"], 1.23)
        self.assertFalse(row["order_fee_breakdown_present"])


if __name__ == "__main__":
    unittest.main()
