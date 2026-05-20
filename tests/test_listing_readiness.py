import unittest

from app.services.listing_readiness import evaluate_ebay_readiness


class ListingReadinessTests(unittest.TestCase):
    def _base_kwargs(self) -> dict:
        return {
            "listing_title": "1 oz Silver Eagle",
            "listing_price": 49.99,
            "auction_start_price": 0.0,
            "auction_reserve_price": 0.0,
            "auction_buy_now_price": 0.0,
            "quantity_listed": 1,
            "listing_status": "draft",
            "format_type": "FIXED_PRICE",
            "listing_duration": "GTC",
            "media_count": 1,
            "category_id": "12345",
            "merchant_location_key": "LOC1",
            "payment_policy_id": "PAY1",
            "fulfillment_policy_id": "FUL1",
            "return_policy_id": "RET1",
        }

    def test_fixed_price_ready(self) -> None:
        result = evaluate_ebay_readiness(**self._base_kwargs())
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.blockers, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.score, 100)

    def test_fixed_price_blockers(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "listing_title": "",
                "listing_price": 0,
                "quantity_listed": 0,
                "media_count": 0,
                "listing_status": "ended",
                "category_id": "",
                "merchant_location_key": "",
                "payment_policy_id": "",
                "fulfillment_policy_id": "",
                "return_policy_id": "",
            }
        )
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Missing listing title", result.blockers)
        self.assertIn("Buy It Now price must be > 0", result.blockers)
        self.assertIn("Quantity listed must be > 0", result.blockers)
        self.assertIn("At least 1 image/video required", result.blockers)
        self.assertIn("Listing is ended", result.blockers)
        self.assertIn("Missing eBay category ID", result.blockers)
        self.assertIn("Missing merchant location key", result.blockers)
        self.assertIn("Missing payment policy ID", result.blockers)
        self.assertIn("Missing fulfillment policy ID", result.blockers)
        self.assertIn("Missing return policy ID", result.blockers)
        self.assertEqual(result.score, 0)

    def test_unknown_format_and_non_draft_warning(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update({"format_type": "CLASSIFIED", "listing_status": "active"})
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Unknown eBay listing format", result.blockers)
        self.assertIn("Status is not draft; verify before publish", result.warnings)

    def test_auction_blockers_and_warnings(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "format_type": "AUCTION",
                "auction_start_price": 10,
                "auction_reserve_price": 9,
                "auction_buy_now_price": 8,
                "listing_duration": "GTC",
                "quantity_listed": 2,
            }
        )
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Auction reserve price cannot be lower than start price", result.blockers)
        self.assertIn("Auction duration must be one of DAYS_1/3/5/7/10", result.blockers)
        self.assertIn("Auction Buy It Now price cannot be lower than start price", result.blockers)
        self.assertIn("Auction quantity > 1; verify intended multi-quantity auction behavior", result.warnings)

    def test_auction_ready_with_strategy_warning(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "format_type": "AUCTION",
                "auction_start_price": 50,
                "auction_reserve_price": 60,
                "auction_buy_now_price": 55,
                "listing_duration": "DAYS_7",
            }
        )
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "ready")
        self.assertIn(
            "Auction Buy It Now is below reserve price; verify intended strategy",
            result.warnings,
        )
        self.assertLess(result.score, 100)

    def test_cached_required_item_specifics_block_when_missing(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "aspects": {"Brand": ["US Mint"]},
                "category_aspects": [
                    {"name": "Brand", "required": True, "values": []},
                    {"name": "Color", "required": True, "values": ["Red"]},
                    {"name": "Size", "required": False, "values": []},
                ],
            }
        )
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Missing required eBay item specific: Color", result.blockers)
        self.assertNotIn("Missing required eBay item specific: Brand", result.blockers)

    def test_category_condition_policy_blocks_invalid_condition(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "condition": "NEW",
                "category_conditions": [
                    {"condition": "USED_EXCELLENT", "label": "Used", "condition_id": "3000"},
                ],
            }
        )
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Selected eBay condition is not valid for this category: NEW", result.blockers)

    def test_category_condition_policy_allows_supported_condition(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "condition": "USED_EXCELLENT",
                "category_conditions": [
                    {"condition": "USED_EXCELLENT", "label": "Used", "condition_id": "3000"},
                ],
            }
        )
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.blockers, [])

    def test_condition_description_over_1000_chars_blocks_publish(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update({"condition_description": "x" * 1001})
        result = evaluate_ebay_readiness(**kwargs)
        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "eBay condition description must be 1000 characters or fewer (currently 1001)",
            result.blockers,
        )

    def test_numerical_coin_grade_requires_approved_grader_evidence(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "listing_title": "2021 Silver Eagle MS69 Type 2",
                "aspects": {"Certification": ["Uncertified"]},
            }
        )
        result = evaluate_ebay_readiness(**kwargs)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "Numerical coin grade requires an approved grading company item specific "
            "(Certification or Professional Grader).",
            result.blockers,
        )

    def test_numerical_coin_grade_allows_approved_grader_evidence(self) -> None:
        kwargs = self._base_kwargs()
        kwargs.update(
            {
                "listing_title": "2021 Silver Eagle PCGS MS69 Type 2",
                "aspects": {"Certification": ["PCGS"], "Grade": ["MS 69"]},
            }
        )
        result = evaluate_ebay_readiness(**kwargs)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.blockers, [])


if __name__ == "__main__":
    unittest.main()
