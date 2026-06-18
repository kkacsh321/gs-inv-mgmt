from decimal import Decimal
import unittest
from types import SimpleNamespace

from app.services.ebay_fee_estimator import (
    calculate_expected_net_score,
    estimate_ebay_fees,
    resolve_product_known_unit_cost,
)


class _FakeRepo:
    def __init__(self, rows: dict[str, SimpleNamespace] | None = None):
        self.rows = rows or {}

    def get_runtime_setting(self, *, environment: str, key: str, active_only: bool = True):
        return self.rows.get(key)


class EbayFeeEstimatorTests(unittest.TestCase):
    def test_estimate_with_defaults(self) -> None:
        repo = _FakeRepo()
        result = estimate_ebay_fees(
            repo,
            unit_price=10.0,
            quantity=2,
            buyer_paid_shipping=5.0,
            promoted_rate_percent=0.0,
        )
        self.assertEqual(result["item_subtotal"], 20.0)
        self.assertEqual(result["gross_total"], 25.0)
        self.assertEqual(result["payment_fee"], 0.0)
        self.assertEqual(result["payment_rate_percent"], 0.0)
        self.assertEqual(result["payment_fixed_usd"], 0.0)
        self.assertGreater(result["estimated_total_fees"], 0.0)
        self.assertAlmostEqual(
            result["estimated_net_payout_before_shipping_cost"],
            round(result["gross_total"] - result["estimated_total_fees"], 2),
        )

    def test_estimate_uses_runtime_overrides(self) -> None:
        repo = _FakeRepo(
            rows={
                "ebay_fee_estimate_final_value_rate_percent": SimpleNamespace(value="15", value_type="float"),
                "ebay_fee_estimate_final_value_fixed_per_order_usd": SimpleNamespace(value="0.40", value_type="float"),
                "ebay_fee_estimate_payment_rate_percent": SimpleNamespace(value="3.2", value_type="float"),
                "ebay_fee_estimate_payment_fixed_per_order_usd": SimpleNamespace(value="0.35", value_type="float"),
                "ebay_fee_estimate_promoted_rate_percent": SimpleNamespace(value="5", value_type="float"),
            }
        )
        result = estimate_ebay_fees(
            repo,
            unit_price=100.0,
            quantity=1,
            buyer_paid_shipping=10.0,
        )
        self.assertEqual(result["final_value_rate_percent"], 15.0)
        self.assertEqual(result["promoted_rate_percent"], 5.0)
        self.assertEqual(result["gross_total"], 110.0)
        self.assertGreater(result["payment_fee"], 0.0)
        self.assertGreater(result["promoted_fee"], 0.0)

    def test_negative_inputs_are_clamped(self) -> None:
        repo = _FakeRepo()
        result = estimate_ebay_fees(
            repo,
            unit_price=-10.0,
            quantity=-5,
            buyer_paid_shipping=-2.0,
            promoted_rate_percent=-3.0,
        )
        self.assertEqual(result["unit_price"], 0.0)
        self.assertEqual(result["quantity"], 1.0)
        self.assertEqual(result["buyer_paid_shipping"], 0.0)
        self.assertEqual(result["promoted_rate_percent"], 0.0)

    def test_expected_net_score_includes_breakeven_and_price_cushion(self) -> None:
        score = calculate_expected_net_score(
            fee_estimate={
                "gross_total": 100.0,
                "item_subtotal": 100.0,
                "buyer_paid_shipping": 0.0,
                "estimated_total_fees": 12.0,
                "estimated_net_payout_before_shipping_cost": 88.0,
            },
            quantity=2,
            known_unit_cost=20.0,
            estimated_local_shipping_cost_per_item=5.0,
        )

        self.assertEqual(float(score["known_cogs_total"]), 40.0)
        self.assertEqual(float(score["estimated_local_shipping_total"]), 10.0)
        self.assertEqual(float(score["expected_net"]), 38.0)
        self.assertEqual(float(score["breakeven_listing_price"]), 56.82)
        self.assertEqual(float(score["breakeven_unit_price"]), 28.41)
        self.assertEqual(float(score["price_cushion"]), 43.18)
        self.assertEqual(score["score"], "strong")

    def test_resolve_product_known_unit_cost_prefers_product_cost_then_landed_cost(self) -> None:
        explicit = SimpleNamespace(
            product_cost="12.50",
            acquisition_cost="10.00",
            acquisition_tax_paid="1.00",
            acquisition_shipping_paid="2.00",
            acquisition_handling_paid="3.00",
        )
        landed = SimpleNamespace(
            product_cost=None,
            acquisition_cost="10.00",
            acquisition_tax_paid="1.00",
            acquisition_shipping_paid="2.00",
            acquisition_handling_paid="3.00",
        )

        self.assertEqual(resolve_product_known_unit_cost(explicit), Decimal("12.50"))
        self.assertEqual(resolve_product_known_unit_cost(landed), Decimal("16.00"))


if __name__ == "__main__":
    unittest.main()
