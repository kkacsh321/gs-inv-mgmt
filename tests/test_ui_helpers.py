from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.components import ui_helpers


class UiHelpersTests(unittest.TestCase):
    def test_decimal_helpers(self):
        self.assertIsNone(ui_helpers.to_decimal_or_none(None))
        self.assertIsNone(ui_helpers.to_decimal_or_none(0))
        self.assertEqual(ui_helpers.to_decimal_or_none(1.25), Decimal("1.25"))
        self.assertEqual(ui_helpers.to_decimal(2), Decimal("2"))

    def test_iso_or_none(self):
        dt = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(ui_helpers.iso_or_none(dt), "2026-01-02T03:04:05")
        self.assertIsNone(ui_helpers.iso_or_none(None))

    def test_build_product_options(self):
        products = [
            SimpleNamespace(id=1, sku="SKU1", title="One"),
            SimpleNamespace(id=2, sku="SKU2", title="Two"),
        ]
        basic = ui_helpers.build_product_options(products)
        self.assertEqual(basic["SKU1 | One"], 1)

        with_none = ui_helpers.build_product_options(products, include_none=True)
        self.assertIn("None", with_none)
        self.assertIsNone(with_none["None"])

        with_id = ui_helpers.build_product_options(products, include_id=True)
        self.assertIn("#1 | SKU1 | One", with_id)

    def test_build_listing_options(self):
        listings = [
            SimpleNamespace(id=9, marketplace="ebay", listing_title="Coin"),
        ]
        with_id = ui_helpers.build_listing_options(listings)
        self.assertEqual(with_id["#9 | ebay | Coin"], 9)

        no_id = ui_helpers.build_listing_options(listings, include_id=False)
        self.assertEqual(no_id["ebay | Coin"], 9)

        with_none = ui_helpers.build_listing_options(listings, include_none=True)
        self.assertIsNone(with_none["None"])

    def test_key_for_value(self):
        opts = {"A": 1, "B": 2}
        self.assertEqual(ui_helpers.key_for_value(opts, 2), "B")
        self.assertEqual(ui_helpers.key_for_value(opts, 3), "None")
        self.assertEqual(ui_helpers.key_for_value(opts, 3, fallback="A"), "A")

    def test_dataframe_date_bounds(self):
        values = [
            datetime(2026, 1, 4, 10, 0, 0),
            datetime(2026, 1, 2, 10, 0, 0),
        ]
        low, high = ui_helpers.dataframe_date_bounds(values)
        self.assertEqual(str(low), "2026-01-02")
        self.assertEqual(str(high), "2026-01-04")

        today_low, today_high = ui_helpers.dataframe_date_bounds([])
        self.assertEqual(today_low, today_high)

    def test_format_ebay_sync_note_for_customer(self):
        raw = (
            "Updated by eBay sync pull. buyer=micorn_78; shipping_service=USPSParcel; "
            "ship_to=Miles Cornwall, 1580 summer way, Idaho falls, ID, 83404-8258, US; "
            'fee_breakdown_json={"price_subtotal":325.0,"delivery_cost":6.78,"order_total":331.78}'
        )
        formatted = ui_helpers.format_ebay_sync_note_for_customer(raw)
        self.assertIn("Imported from eBay sync pull.", formatted)
        self.assertIn("Buyer: micorn_78", formatted)
        self.assertIn("Shipping Service: USPSParcel", formatted)
        self.assertIn("Ship To: Miles Cornwall, 1580 summer way, Idaho falls, ID, 83404-8258, US", formatted)
        self.assertNotIn("fee_breakdown_json", formatted)
        self.assertNotIn("price_subtotal", formatted)

    def test_format_ebay_sync_note_for_customer_passthrough(self):
        raw = "Manual order note from local sale"
        self.assertEqual(ui_helpers.format_ebay_sync_note_for_customer(raw), raw)


if __name__ == "__main__":
    unittest.main()
