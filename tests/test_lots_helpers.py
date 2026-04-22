import importlib.util
import sys
import types
import unittest
from pathlib import Path


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSt:
    def __init__(self):
        self.successes = []

    def success(self, msg):
        self.successes.append(str(msg))


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
    shared_name = "app.components.views.shared"
    if shared_name not in sys.modules:
        shared_path = root / "app" / "components" / "views" / "shared.py"
        shared_spec = importlib.util.spec_from_file_location(shared_name, shared_path)
        shared_mod = importlib.util.module_from_spec(shared_spec)
        assert shared_spec and shared_spec.loader
        shared_spec.loader.exec_module(shared_mod)
        sys.modules[shared_name] = shared_mod


def _load_lots_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    lots_path = root / "app" / "components" / "views" / "lots.py"
    spec = importlib.util.spec_from_file_location("test_lots_module", lots_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


lots = _load_lots_module()


class LotsHelpersTests(unittest.TestCase):
    def test_extract_json_object_variants(self):
        self.assertEqual(lots._extract_json_object(""), {})
        self.assertEqual(lots._extract_json_object('{"a":1}'), {"a": 1})
        wrapped = "prefix {\"x\": 2} suffix"
        self.assertEqual(lots._extract_json_object(wrapped), {"x": 2})
        self.assertEqual(lots._extract_json_object("no json here"), {})

    def test_extract_decimal_candidate(self):
        self.assertIsNone(lots._extract_decimal_candidate(None))
        self.assertEqual(lots._extract_decimal_candidate("$12.34"), 12.34)
        self.assertEqual(lots._extract_decimal_candidate(" -7.5 "), -7.5)
        self.assertIsNone(lots._extract_decimal_candidate("abc"))

    def test_extract_invoice_date_candidate(self):
        self.assertEqual(str(lots._extract_invoice_date_candidate("2026-04-01")), "2026-04-01")
        self.assertEqual(str(lots._extract_invoice_date_candidate("04/01/2026")), "2026-04-01")
        self.assertEqual(str(lots._extract_invoice_date_candidate("04-01-2026")), "2026-04-01")
        self.assertIsNone(lots._extract_invoice_date_candidate("bad-date"))

    def test_extract_first_line_item(self):
        payload = {"line_items": [{"description": "A"}, {"description": "B"}]}
        self.assertEqual(lots._extract_first_line_item(payload), {"description": "A"})
        self.assertEqual(lots._extract_first_line_item({"line_items": []}), {})
        self.assertEqual(lots._extract_first_line_item({"line_items": ["bad", 1]}), {})
        self.assertEqual(lots._extract_first_line_item({}), {})

    def test_lot_create_defaults_and_source_normalization(self):
        labels = ["None (one-off/manual)", "Dealer A (vendor)"]
        defaults = lots._lot_create_defaults(labels)
        self.assertEqual(defaults["lots_create_source_key"], "None (one-off/manual)")
        self.assertIn("lots_create_total_tax_paid", defaults)
        self.assertIn("lots_create_total_shipping_paid", defaults)
        self.assertIn("lots_create_total_handling_paid", defaults)
        self.assertIn("lots_create_ebay_purchase", defaults)

        self.assertEqual(
            lots._normalize_lot_create_source_key(labels, "Dealer A (vendor)"),
            "Dealer A (vendor)",
        )
        self.assertEqual(
            lots._normalize_lot_create_source_key(labels, "Missing Value"),
            "None (one-off/manual)",
        )
        self.assertEqual(
            lots._normalize_lot_create_source_key([], "Anything"),
            "None (one-off/manual)",
        )

    def test_validate_lot_create_inputs(self):
        self.assertEqual(
            lots._validate_lot_create_inputs("", False, ""),
            "Lot code is required.",
        )
        self.assertEqual(
            lots._validate_lot_create_inputs("LOT-001", True, ""),
            "eBay Purchase Item ID is required when Purchased On eBay is enabled.",
        )
        self.assertIsNone(
            lots._validate_lot_create_inputs("LOT-001", True, "1234567890"),
        )
        self.assertIsNone(
            lots._validate_lot_create_inputs("LOT-001", False, ""),
        )

    def test_prime_lot_create_state_handles_flash_and_reset(self):
        labels = ["None (one-off/manual)", "Dealer A (vendor)"]
        state = {
            "lots_create_flash_message": "Purchase lot created.",
            "lots_create_reset_requested": True,
            "lots_create_lot_code": "OLD",
            "lots_create_source_key": "Missing",
            "lots_create_vendor": "Old Vendor",
            "lots_create_ebay_purchase": True,
            "lots_create_ebay_purchase_item_id": "123",
        }
        flash = lots._prime_lot_create_state(state, labels)
        self.assertEqual(flash, "Purchase lot created.")
        self.assertNotIn("lots_create_flash_message", state)
        self.assertNotIn("lots_create_reset_requested", state)
        self.assertEqual(state["lots_create_lot_code"], "")
        self.assertEqual(state["lots_create_vendor"], "")
        self.assertEqual(state["lots_create_source_key"], "None (one-off/manual)")
        self.assertFalse(state["lots_create_ebay_purchase"])
        self.assertEqual(state["lots_create_ebay_purchase_item_id"], "")

    def test_render_lot_create_state_feedback_shows_flash(self):
        labels = ["None (one-off/manual)"]
        state = {"lots_create_flash_message": "Purchase lot created."}
        fake_st = _FakeSt()
        lots._render_lot_create_state_feedback(fake_st, state, labels)
        self.assertEqual(fake_st.successes, ["Purchase lot created."])


if __name__ == "__main__":
    unittest.main()
