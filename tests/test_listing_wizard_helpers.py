import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _bootstrap_views_package() -> None:
    if "boto3" not in sys.modules:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = types.ModuleType("botocore")
    if "botocore.config" not in sys.modules:
        fake_botocore_config = types.ModuleType("botocore.config")
        fake_botocore_config.Config = lambda *args, **kwargs: None
        sys.modules["botocore.config"] = fake_botocore_config
    if "botocore.exceptions" not in sys.modules:
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
        fake_botocore_exceptions.BotoCoreError = Exception
        fake_botocore_exceptions.ClientError = Exception
        sys.modules["botocore.exceptions"] = fake_botocore_exceptions
    if "app.components.views" not in sys.modules:
        pkg = types.ModuleType("app.components.views")
        pkg.__path__ = []
        sys.modules["app.components.views"] = pkg

    root = Path(__file__).resolve().parents[1]
    for name in ("shared", "workspace_shell", "entity_ops"):
        full_name = f"app.components.views.{name}"
        if full_name in sys.modules:
            continue
        mod_path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full_name, mod_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        sys.modules[full_name] = module


def _load_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    module_path = root / "app" / "components" / "views" / "listing_wizard.py"
    spec = importlib.util.spec_from_file_location("test_listing_wizard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


listing_wizard = _load_module()


class ListingWizardHelperTests(unittest.TestCase):
    def test_ai_grading_prefill_status_messages(self):
        self.assertEqual(
            listing_wizard._ai_grading_prefill_status(current_value="", default_value=""),
            "",
        )
        self.assertIn(
            "prefilled",
            listing_wizard._ai_grading_prefill_status(
                current_value="MS63 details",
                default_value="MS63 details",
            ).lower(),
        )
        self.assertIn(
            "edited",
            listing_wizard._ai_grading_prefill_status(
                current_value="MS62 details",
                default_value="MS63 details",
            ).lower(),
        )
        self.assertIn(
            "available",
            listing_wizard._ai_grading_prefill_status(
                current_value="",
                default_value="MS63 details",
            ).lower(),
        )

    def test_safe_price_float_parses_currency_and_ranges(self):
        self.assertEqual(listing_wizard._safe_price_float("$12.50"), 12.5)
        self.assertEqual(listing_wizard._safe_price_float("12.50 to 15.00"), 12.5)
        self.assertEqual(listing_wizard._safe_price_float("12.50-15.00"), 12.5)
        self.assertEqual(listing_wizard._safe_price_float("not-a-number", default=7.0), 7.0)

    def test_known_unit_cost_sums_landed_components(self):
        product = types.SimpleNamespace(
            acquisition_cost=10.0,
            acquisition_tax_paid=1.5,
            acquisition_shipping_paid=0.75,
            acquisition_handling_paid=0.25,
        )
        self.assertEqual(listing_wizard._known_unit_cost(product), 12.5)
        self.assertEqual(listing_wizard._known_unit_cost(None), 0.0)

    def test_expected_net_score_computes_variance_bands(self):
        score = listing_wizard._expected_net_score(
            fee_estimate={
                "gross_total": 100.0,
                "estimated_total_fees": 12.0,
                "estimated_net_payout_before_shipping_cost": 88.0,
            },
            quantity=2,
            known_unit_cost=20.0,
            estimated_local_shipping_cost_per_item=5.0,
        )
        self.assertEqual(float(score.get("known_cogs_total") or 0.0), 40.0)
        self.assertEqual(float(score.get("estimated_local_shipping_total") or 0.0), 10.0)
        self.assertEqual(float(score.get("expected_net") or 0.0), 38.0)
        self.assertEqual(str(score.get("score") or ""), "strong")

    def test_suggested_price_band_derives_missing_values(self):
        price, low, high = listing_wizard._suggested_price_band({"suggested_price": "100"})
        self.assertEqual(price, 100.0)
        self.assertEqual(low, 90.0)
        self.assertEqual(high, 110.0)

        price2, low2, high2 = listing_wizard._suggested_price_band(
            {"suggested_price_low": "80", "suggested_price_high": "100"}
        )
        self.assertEqual(price2, 90.0)
        self.assertEqual(low2, 80.0)
        self.assertEqual(high2, 100.0)

        price3, low3, high3 = listing_wizard._suggested_price_band(
            {"suggested_price_low": "110", "suggested_price_high": "90"}
        )
        self.assertEqual(price3, 100.0)
        self.assertEqual(low3, 90.0)
        self.assertEqual(high3, 110.0)

    def test_sanitize_preview_html_removes_scripts_styles_and_handlers(self):
        raw = (
            "<div onclick='alert(1)'>Hi</div>"
            "<script>alert('x')</script>"
            "<style>body{display:none;}</style>"
            "<a href='javascript:alert(1)'>x</a>"
        )
        sanitized = listing_wizard._sanitize_preview_html(raw)
        self.assertIn("<div>Hi</div>", sanitized)
        self.assertNotIn("<script", sanitized.lower())
        self.assertNotIn("<style", sanitized.lower())
        self.assertNotIn("onclick", sanitized.lower())
        self.assertNotIn("javascript:", sanitized.lower())

    def test_build_ebay_offer_payload_fixed_price_includes_quantity_and_best_offer(self):
        payload = listing_wizard._wizard_build_ebay_offer_payload(
            sku="SKU-1",
            marketplace_id="EBAY_US",
            format_type="FIXED_PRICE",
            listing_qty=5,
            category_id="16679",
            merchant_location_key="goldenstackers-main",
            listing_description="desc",
            listing_duration="GTC",
            payment_policy_id="pay-1",
            fulfillment_policy_id="ful-1",
            return_policy_id="ret-1",
            currency="USD",
            fixed_price=19.99,
            best_offer_enabled=True,
            best_offer_auto_accept=18.5,
            best_offer_minimum=17.0,
            auction_start_price=0.0,
            auction_reserve_price=0.0,
            auction_buy_now_price=0.0,
        )
        self.assertEqual(payload["format"], "FIXED_PRICE")
        self.assertEqual(payload["availableQuantity"], 5)
        self.assertEqual(payload["pricingSummary"]["price"]["value"], "19.99")
        self.assertEqual(
            payload["listingPolicies"]["bestOfferTerms"]["autoAcceptPrice"]["value"],
            "18.5",
        )
        self.assertEqual(
            payload["listingPolicies"]["bestOfferTerms"]["autoDeclinePrice"]["value"],
            "17.0",
        )

    def test_build_ebay_offer_payload_auction_omits_quantity(self):
        payload = listing_wizard._wizard_build_ebay_offer_payload(
            sku="SKU-2",
            marketplace_id="EBAY_US",
            format_type="AUCTION",
            listing_qty=7,
            category_id="16679",
            merchant_location_key="goldenstackers-main",
            listing_description="desc",
            listing_duration="DAYS_7",
            payment_policy_id="pay-1",
            fulfillment_policy_id="ful-1",
            return_policy_id="ret-1",
            currency="USD",
            fixed_price=0.0,
            best_offer_enabled=False,
            best_offer_auto_accept=0.0,
            best_offer_minimum=0.0,
            auction_start_price=9.99,
            auction_reserve_price=19.99,
            auction_buy_now_price=24.99,
        )
        self.assertEqual(payload["format"], "AUCTION")
        self.assertNotIn("availableQuantity", payload)
        self.assertEqual(payload["pricingSummary"]["auctionStartPrice"]["value"], "9.99")
        self.assertEqual(payload["pricingSummary"]["auctionReservePrice"]["value"], "19.99")
        self.assertEqual(payload["pricingSummary"]["price"]["value"], "24.99")

    def test_merge_pending_field_updates_merges_existing_and_new(self):
        merged = listing_wizard._wizard_merge_pending_field_updates(
            {"listing_wizard_title": "A"},
            {"listing_wizard_category_id": "16679"},
        )
        self.assertEqual(merged.get("listing_wizard_title"), "A")
        self.assertEqual(merged.get("listing_wizard_category_id"), "16679")

    def test_merge_pending_field_updates_ignores_blank_keys(self):
        merged = listing_wizard._wizard_merge_pending_field_updates(
            {"listing_wizard_title": "A"},
            {"": "bad", "   ": "bad2", "listing_wizard_price": 10.0},
        )
        self.assertNotIn("", merged)
        self.assertEqual(merged.get("listing_wizard_title"), "A")
        self.assertEqual(merged.get("listing_wizard_price"), 10.0)

    def test_apply_pending_field_updates_applies_allowed_keys_only(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {
                    "listing_wizard_pending_field_updates": {
                        "listing_wizard_title": "Updated Title",
                        "listing_wizard_category_id": "16679",
                        "bad_key": "ignored",
                    }
                }

        original_st = listing_wizard.st
        try:
            listing_wizard.st = _FakeSt()
            listing_wizard._wizard_apply_pending_field_updates()
            state = listing_wizard.st.session_state
            self.assertEqual(state.get("listing_wizard_title"), "Updated Title")
            self.assertEqual(state.get("listing_wizard_category_id"), "16679")
            self.assertNotIn("bad_key", state)
            self.assertNotIn("listing_wizard_pending_field_updates", state)
        finally:
            listing_wizard.st = original_st

    def test_apply_pending_field_updates_defers_locked_widget_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "listing_wizard_category_id":
                    raise listing_wizard.StreamlitAPIException("locked widget key")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState(
                    {
                        "listing_wizard_pending_field_updates": {
                            "listing_wizard_title": "Updated Title",
                            "listing_wizard_category_id": "16679",
                        }
                    }
                )

        original_st = listing_wizard.st
        try:
            listing_wizard.st = _FakeSt()
            listing_wizard._wizard_apply_pending_field_updates()
            state = listing_wizard.st.session_state
            self.assertEqual(state.get("listing_wizard_title"), "Updated Title")
            self.assertEqual(
                state.get("listing_wizard_pending_field_updates"),
                {"listing_wizard_category_id": "16679"},
            )
            self.assertIn("deferred", str(state.get("listing_wizard_apply_flash") or "").lower())
        finally:
            listing_wizard.st = original_st

    def test_apply_draft_payload_to_session_defers_locked_widget_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "listing_wizard_category_id":
                    raise listing_wizard.StreamlitAPIException("locked widget key")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState()

        payload = {
            "contract": {"type": "listing_draft", "version": 1},
            "state": {
                "listing_wizard_title": "Draft Title",
                "listing_wizard_category_id": "16679",
            },
            "context": {"selected_product_id": 1},
        }

        original_st = listing_wizard.st
        try:
            listing_wizard.st = _FakeSt()
            listing_wizard._wizard_apply_draft_payload_to_session(payload)
            state = listing_wizard.st.session_state
            self.assertEqual(state.get("listing_wizard_title"), "Draft Title")
            self.assertEqual(
                state.get("listing_wizard_pending_field_updates"),
                {"listing_wizard_category_id": "16679"},
            )
            self.assertIn("deferred", str(state.get("listing_wizard_apply_flash") or "").lower())
        finally:
            listing_wizard.st = original_st


if __name__ == "__main__":
    unittest.main()
