import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal


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


def _load_search_edit_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    view_path = root / "app" / "components" / "views" / "search_edit.py"
    spec = importlib.util.spec_from_file_location("test_search_edit_module", view_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


search_edit = _load_search_edit_module()


class SearchEditHelpersTests(unittest.TestCase):
    def test_build_lot_assignment_rows(self):
        assignments = [
            SimpleNamespace(
                id=101,
                lot_id=1,
                product_id=10,
                quantity_acquired=2,
                unit_cost=5.5,
                unit_tax_paid=0.2,
                unit_shipping_paid=0.1,
                unit_handling_paid=0.05,
                allocated_cost=11.0,
                allocated_tax_paid=0.4,
                allocated_shipping_paid=0.2,
                allocated_handling_paid=0.1,
                acquired_at=datetime(2026, 4, 8, 12, 0, 0),
            )
        ]
        product_index = {10: SimpleNamespace(sku="SKU-10", title="Standing Liberty Round")}
        rows = search_edit._build_lot_assignment_rows(assignments, product_index)
        self.assertIn(1, rows)
        self.assertEqual(len(rows[1]), 1)
        first = rows[1][0]
        self.assertEqual(first["assignment_id"], 101)
        self.assertEqual(first["product_id"], 10)
        self.assertEqual(first["sku"], "SKU-10")
        self.assertEqual(first["product_title"], "Standing Liberty Round")
        self.assertEqual(first["quantity_acquired"], 2)
        self.assertEqual(first["allocated_cost"], 11.0)

    def test_build_lot_table_rows_with_attached_products(self):
        lot = SimpleNamespace(
            id=1,
            lot_code="LOT-001",
            source_id=2,
            source=SimpleNamespace(name="Dealer A"),
            vendor="Dealer A",
            purchase_date=datetime(2026, 4, 1, 0, 0, 0),
            total_cost=100.0,
            total_tax_paid=5.0,
            total_shipping_paid=3.0,
            total_handling_paid=2.0,
            ebay_purchase=True,
            ebay_purchase_item_id="12345",
            ebay_purchase_url="https://ebay.com/itm/12345",
            notes="test",
        )
        lot_assignment_rows = {
            1: [
                {"product_id": 10, "sku": "SKU-A"},
                {"product_id": 11, "sku": "SKU-B"},
            ]
        }
        table_rows = search_edit._build_lot_table_rows([lot], lot_assignment_rows)
        self.assertEqual(len(table_rows), 1)
        row = table_rows[0]
        self.assertEqual(row["lot_code"], "LOT-001")
        self.assertEqual(row["source_name"], "Dealer A")
        self.assertEqual(row["attached_products_count"], 2)
        self.assertIn("SKU-A", row["attached_products"])
        self.assertIn("SKU-B", row["attached_products"])
        self.assertTrue(row["ebay_purchase"])

    def test_validate_lot_update_inputs(self):
        self.assertEqual(
            search_edit._validate_lot_update_inputs("", False, ""),
            "Lot code is required.",
        )
        self.assertEqual(
            search_edit._validate_lot_update_inputs("LOT-1", True, ""),
            "eBay Purchase Item ID is required when Purchased On eBay is enabled.",
        )
        self.assertIsNone(search_edit._validate_lot_update_inputs("LOT-1", True, "123"))
        self.assertIsNone(search_edit._validate_lot_update_inputs("LOT-1", False, ""))

    def test_build_lot_update_payload(self):
        payload = search_edit._build_lot_update_payload(
            source_id=7,
            lot_code=" LOT-9 ",
            vendor=" Dealer ",
            purchase_date=datetime(2026, 4, 8, 15, 30, 0).date(),
            total_cost=100.0,
            total_tax_paid=5.0,
            total_shipping_paid=3.0,
            total_handling_paid=1.0,
            ebay_purchase=True,
            ebay_purchase_item_id=" 12345 ",
            ebay_purchase_url=" https://ebay.com/itm/12345 ",
            notes=" note ",
        )
        self.assertEqual(payload["lot_code"], "LOT-9")
        self.assertEqual(payload["source_id"], 7)
        self.assertEqual(payload["vendor"], "Dealer")
        self.assertEqual(payload["purchase_date"], datetime(2026, 4, 8, 0, 0, 0))
        self.assertEqual(payload["total_cost"], Decimal("100.0"))
        self.assertEqual(payload["total_tax_paid"], Decimal("5.0"))
        self.assertEqual(payload["total_shipping_paid"], Decimal("3.0"))
        self.assertEqual(payload["total_handling_paid"], Decimal("1.0"))
        self.assertTrue(payload["ebay_purchase"])
        self.assertEqual(payload["ebay_purchase_item_id"], "12345")
        self.assertEqual(payload["ebay_purchase_url"], "https://ebay.com/itm/12345")
        self.assertEqual(payload["notes"], "note")


if __name__ == "__main__":
    unittest.main()
