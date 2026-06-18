from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.components.views.tools import calculate_ebay_fee_estimate, _product_known_unit_cost


class EbayFeeCalculatorTests(unittest.TestCase):
    def test_estimates_fee_net_and_profit(self) -> None:
        estimate = calculate_ebay_fee_estimate(
            sale_price=100,
            buyer_shipping_charged=5,
            sales_tax_collected=0,
            item_cost=50,
            shipping_label_cost=4,
            packaging_cost=1,
            final_value_fee_percent=13.25,
            fixed_order_fee=0.40,
            promoted_ad_percent=2,
        )

        self.assertEqual(estimate["fee_basis"], Decimal("105.00"))
        self.assertEqual(estimate["final_value_fee"], Decimal("13.91"))
        self.assertEqual(estimate["promoted_ad_fee"], Decimal("2.00"))
        self.assertEqual(estimate["estimated_total_fees"], Decimal("16.31"))
        self.assertEqual(estimate["net_before_cogs"], Decimal("83.69"))
        self.assertEqual(estimate["estimated_profit"], Decimal("33.69"))

    def test_sales_tax_toggle_changes_fee_basis_only(self) -> None:
        with_tax = calculate_ebay_fee_estimate(
            sale_price=100,
            sales_tax_collected=8,
            final_value_fee_percent=10,
            fixed_order_fee=0,
            include_sales_tax_in_fee_basis=True,
        )
        without_tax = calculate_ebay_fee_estimate(
            sale_price=100,
            sales_tax_collected=8,
            final_value_fee_percent=10,
            fixed_order_fee=0,
            include_sales_tax_in_fee_basis=False,
        )

        self.assertEqual(with_tax["fee_basis"], Decimal("108.00"))
        self.assertEqual(with_tax["estimated_total_fees"], Decimal("10.80"))
        self.assertEqual(without_tax["fee_basis"], Decimal("100.00"))
        self.assertEqual(without_tax["estimated_total_fees"], Decimal("10.00"))

    def test_default_fixed_order_fee_follows_order_size(self) -> None:
        small = calculate_ebay_fee_estimate(
            sale_price=10,
            final_value_fee_percent=0,
            fixed_order_fee=None,
        )
        standard = calculate_ebay_fee_estimate(
            sale_price=10.01,
            final_value_fee_percent=0,
            fixed_order_fee=None,
        )

        self.assertEqual(small["fixed_order_fee"], Decimal("0.30"))
        self.assertEqual(standard["fixed_order_fee"], Decimal("0.40"))

    def test_product_known_unit_cost_prefers_product_cost_then_landed_cost(self) -> None:
        explicit = SimpleNamespace(
            product_cost=Decimal("12.50"),
            acquisition_cost=Decimal("10.00"),
            acquisition_tax_paid=Decimal("1.00"),
            acquisition_shipping_paid=Decimal("2.00"),
            acquisition_handling_paid=Decimal("3.00"),
        )
        landed = SimpleNamespace(
            product_cost=None,
            acquisition_cost=Decimal("10.00"),
            acquisition_tax_paid=Decimal("1.00"),
            acquisition_shipping_paid=Decimal("2.00"),
            acquisition_handling_paid=Decimal("3.00"),
        )

        self.assertEqual(_product_known_unit_cost(explicit), Decimal("12.50"))
        self.assertEqual(_product_known_unit_cost(landed), Decimal("16.00"))


if __name__ == "__main__":
    unittest.main()
