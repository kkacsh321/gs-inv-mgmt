import importlib.util
import sys
import types
import unittest
from pathlib import Path

from app.services.workflow_contracts import build_listing_draft_payload, extract_listing_draft_payload


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


def _load_view_module(module_name: str):
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    module_path = root / "app" / "components" / "views" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{module_name}_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


listing_wizard = _load_view_module("listing_wizard")
listings = _load_view_module("listings")


class ListingWorkflowDraftParityTests(unittest.TestCase):
    def test_required_wizard_business_keys_are_persisted(self):
        keyset = set(listing_wizard.LISTING_WIZARD_DRAFT_SESSION_KEYS)
        self.assertIn("listing_wizard_category_id", keyset)
        self.assertIn("listing_wizard_aspects_json", keyset)
        self.assertIn("listing_wizard_price", keyset)
        self.assertIn("listing_wizard_quantity", keyset)
        self.assertIn("listing_wizard_mode", keyset)
        self.assertIn("listing_wizard_offer_enabled", keyset)
        self.assertIn("listing_wizard_offer_auto_accept", keyset)
        self.assertIn("listing_wizard_offer_minimum", keyset)
        self.assertIn("listing_wizard_volume_pricing_json", keyset)
        self.assertIn("listing_wizard_store_category_names", keyset)
        self.assertIn("listing_wizard_package_weight_oz", keyset)
        self.assertIn("listing_wizard_shipping_cost", keyset)
        self.assertIn("listing_wizard_direct_post_mode", keyset)

    def test_required_listings_business_keys_are_persisted(self):
        keyset = set(listings.LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS)
        self.assertIn("ebay_pub_category_id", keyset)
        self.assertIn("ebay_pub_aspects_json", keyset)
        self.assertIn("ebay_pub_fixed_price", keyset)
        self.assertIn("ebay_pub_qty", keyset)
        self.assertIn("ebay_pub_best_offer_enabled", keyset)
        self.assertIn("ebay_pub_best_offer_auto_accept", keyset)
        self.assertIn("ebay_pub_best_offer_minimum", keyset)
        self.assertIn("ebay_pub_volume_pricing_json", keyset)
        self.assertIn("ebay_pub_store_category_names", keyset)
        self.assertIn("ebay_pub_package_weight_oz", keyset)
        self.assertIn("ebay_pub_shipping_cost", keyset)
        self.assertIn("ebay_pub_post_mode", keyset)

    def test_wizard_and_listings_parity_fields_exist(self):
        wizard_keys = set(listing_wizard.LISTING_WIZARD_DRAFT_SESSION_KEYS)
        listings_keys = set(listings.LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS)
        parity_pairs = [
            ("listing_wizard_category_id", "ebay_pub_category_id"),
            ("listing_wizard_store_category_names", "ebay_pub_store_category_names"),
            ("listing_wizard_aspects_json", "ebay_pub_aspects_json"),
            ("listing_wizard_price", "ebay_pub_fixed_price"),
            ("listing_wizard_quantity", "ebay_pub_qty"),
            ("listing_wizard_offer_enabled", "ebay_pub_best_offer_enabled"),
            ("listing_wizard_offer_auto_accept", "ebay_pub_best_offer_auto_accept"),
            ("listing_wizard_offer_minimum", "ebay_pub_best_offer_minimum"),
            ("listing_wizard_volume_pricing_json", "ebay_pub_volume_pricing_json"),
            ("listing_wizard_package_weight_oz", "ebay_pub_package_weight_oz"),
            ("listing_wizard_shipping_cost", "ebay_pub_shipping_cost"),
            ("listing_wizard_direct_post_mode", "ebay_pub_post_mode"),
        ]
        for left, right in parity_pairs:
            self.assertIn(left, wizard_keys)
            self.assertIn(right, listings_keys)

    def test_contract_roundtrip_preserves_parity_paths(self):
        wizard_state = {
            "listing_wizard_category_id": "16679",
            "listing_wizard_store_category_names": ["/Coins/Bullion"],
            "listing_wizard_aspects_json": '{"Certification":["Uncertified"]}',
            "listing_wizard_price": 49.99,
            "listing_wizard_quantity": 3,
            "listing_wizard_offer_enabled": True,
            "listing_wizard_offer_auto_accept": 45.0,
            "listing_wizard_offer_minimum": 42.5,
            "listing_wizard_volume_pricing_json": '{"buy2":2,"buy3":3,"buy4":5}',
            "listing_wizard_package_weight_oz": 6.0,
            "listing_wizard_shipping_cost": 4.99,
            "listing_wizard_direct_post_mode": "Create Offer Draft Only",
        }
        listings_state = {
            "ebay_pub_category_id": "16679",
            "ebay_pub_store_category_names": ["/Coins/Bullion"],
            "ebay_pub_aspects_json": '{"Certification":["Uncertified"]}',
            "ebay_pub_fixed_price": 49.99,
            "ebay_pub_qty": 3,
            "ebay_pub_best_offer_enabled": True,
            "ebay_pub_best_offer_auto_accept": 45.0,
            "ebay_pub_best_offer_minimum": 42.5,
            "ebay_pub_volume_pricing_json": '{"buy2":2,"buy3":3,"buy4":5}',
            "ebay_pub_package_weight_oz": 6.0,
            "ebay_pub_shipping_cost": 4.99,
            "ebay_pub_post_mode": "Create Offer Draft Only",
        }
        payload = build_listing_draft_payload(
            state={**wizard_state, **listings_state},
            context={"selected_product_id": 10, "selected_listing_id": 20},
            signature="sig-parity",
        )
        parsed = extract_listing_draft_payload(
            payload,
            state_keys=[*wizard_state.keys(), *listings_state.keys()],
            context_keys=["selected_product_id", "selected_listing_id"],
        )
        state = parsed.get("state") or {}
        self.assertEqual(parsed.get("signature"), "sig-parity")
        for key, value in wizard_state.items():
            self.assertEqual(state.get(key), value)
        for key, value in listings_state.items():
            self.assertEqual(state.get(key), value)


if __name__ == "__main__":
    unittest.main()
