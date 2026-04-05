import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


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
    for name in ("shared", "workspace_shell"):
        full = f"app.components.views.{name}"
        if full in sys.modules:
            continue
        path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        sys.modules[full] = mod


def _load_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "reports.py"
    spec = importlib.util.spec_from_file_location("test_reports_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


reports = _load_module()


class ReportsHelpersTests(unittest.TestCase):
    def test_safe_float_and_csv_set_and_presets(self):
        self.assertEqual(reports._safe_float(None), 0.0)
        self.assertEqual(reports._safe_float("bad"), 0.0)
        self.assertAlmostEqual(reports._safe_float("12.5"), 12.5)

        self.assertEqual(reports._parse_csv_set(" a, B ,,c "), {"a", "b", "c"})

        presets = reports._tax_report_presets(
            default_jurisdiction="Golden, Colorado",
            default_tax_rate_percent=8.9,
            default_shipping_taxable=True,
        )
        self.assertIn("Golden Local Retail", presets)
        self.assertEqual(presets["Marketplace Shipped"]["shipping_taxable"], False)

    def test_build_fifo_unit_cost_map(self):
        assignments = [
            SimpleNamespace(id=1, product_id=1, acquired_at=datetime(2026, 1, 1), quantity_acquired=5, unit_cost=2.0, allocated_cost=None),
            SimpleNamespace(id=2, product_id=1, acquired_at=datetime(2026, 1, 2), quantity_acquired=3, unit_cost=0, allocated_cost=9.0),
            SimpleNamespace(id=3, product_id=None, acquired_at=datetime(2026, 1, 1), quantity_acquired=2, unit_cost=1.0, allocated_cost=None),
        ]
        sales = [
            SimpleNamespace(id=11, product_id=1, sold_at=datetime(2026, 1, 3), quantity_sold=4),
            SimpleNamespace(id=12, product_id=1, sold_at=datetime(2026, 1, 4), quantity_sold=4),
            SimpleNamespace(id=13, product_id=2, sold_at=datetime(2026, 1, 5), quantity_sold=2),
            SimpleNamespace(id=14, product_id=None, sold_at=datetime(2026, 1, 6), quantity_sold=1),
        ]
        out = reports._build_fifo_unit_cost_map(sales, assignments, {2: 5.0})
        self.assertAlmostEqual(out[11], 2.0)
        self.assertAlmostEqual(out[12], 2.75)
        self.assertAlmostEqual(out[13], 5.0)
        self.assertAlmostEqual(out[14], 0.0)

    def test_build_lot_weighted_unit_cost_map(self):
        assignments = [
            SimpleNamespace(product_id=1, quantity_acquired=2, unit_cost=4.0, allocated_cost=None),
            SimpleNamespace(product_id=1, quantity_acquired=1, unit_cost=0.0, allocated_cost=9.0),
            SimpleNamespace(product_id=2, quantity_acquired=0, unit_cost=5.0, allocated_cost=None),
        ]
        out = reports._build_lot_weighted_unit_cost_map(assignments, {2: 3.5, 3: -1})
        self.assertAlmostEqual(out[1], (2 * 4 + 1 * 9) / 3)
        self.assertAlmostEqual(out[2], 3.5)
        self.assertAlmostEqual(out[3], 0.0)

    def test_build_inventory_cycle_rows(self):
        products = [
            SimpleNamespace(id=1, sku="SKU1", title="Coin A"),
            SimpleNamespace(id=2, sku="SKU2", title="Coin B"),
        ]
        movements = [
            SimpleNamespace(id=1, product_id=1, occurred_at=datetime(2026, 1, 1), quantity_before=0, quantity_after=2, quantity_delta=2, unit_cost=10.0),
            SimpleNamespace(id=2, product_id=1, occurred_at=datetime(2026, 1, 2), quantity_before=2, quantity_after=0, quantity_delta=-2, unit_cost=None),
            SimpleNamespace(id=3, product_id=2, occurred_at=datetime(2026, 1, 3), quantity_before=0, quantity_after=3, quantity_delta=3, unit_cost=2.0),
        ]
        sales = [
            SimpleNamespace(id=11, product_id=1, sold_at=datetime(2026, 1, 1, 12), quantity_sold=1, sold_price=25.0, fees=1.0, shipping_cost=2.0),
            SimpleNamespace(id=12, product_id=2, sold_at=datetime(2026, 1, 4), quantity_sold=1, sold_price=10.0, fees=0.5, shipping_cost=1.0),
        ]
        rows = reports._build_inventory_cycle_rows(products, movements, sales)
        self.assertEqual(len(rows), 2)
        closed = next(r for r in rows if r["sku"] == "SKU1")
        self.assertEqual(closed["cycle_status"], "closed")
        self.assertEqual(closed["sale_count"], 1)
        self.assertAlmostEqual(closed["net_sales"], 22.0)
        self.assertAlmostEqual(closed["estimated_margin_vs_known_cost"], 2.0)

        open_row = next(r for r in rows if r["sku"] == "SKU2")
        self.assertEqual(open_row["cycle_status"], "open")
        self.assertEqual(open_row["qty_in"], 3)

    def test_build_rebuy_cost_trend_rows(self):
        products = [SimpleNamespace(id=1, sku="SKU1", title="Coin A")]
        assignments = [
            SimpleNamespace(id=7, product_id=1, acquired_at=datetime(2026, 1, 1), quantity_acquired=2, unit_cost=5.0),
        ]
        movements = [
            SimpleNamespace(id=8, product_id=1, occurred_at=datetime(2026, 1, 1), movement_type="repurchase_in", quantity_delta=2, unit_cost=5.0),
            SimpleNamespace(id=9, product_id=1, occurred_at=datetime(2026, 1, 2), movement_type="repurchase_in", quantity_delta=1, unit_cost=7.0),
            SimpleNamespace(id=10, product_id=1, occurred_at=datetime(2026, 1, 3), movement_type="sale", quantity_delta=-1, unit_cost=1.0),
        ]
        rows = reports._build_rebuy_cost_trend_rows(products, assignments, movements)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_type"], "lot_assignment")
        self.assertEqual(rows[1]["event_type"], "repurchase_in")
        self.assertAlmostEqual(rows[-1]["weighted_unit_cost"], (2 * 5 + 1 * 7) / 3, places=4)

    def test_build_listing_review_activity_rows(self):
        history_ok = {
            "review_history": [
                {
                    "decision": "approved",
                    "actor": "admin",
                    "reviewed_at": "2026-01-10T12:00:00Z",
                    "notes": "looks good",
                },
                "bad-item",
            ]
        }
        listing = SimpleNamespace(
            id=1,
            marketplace="ebay",
            product=SimpleNamespace(sku="SKU1"),
            listing_title="Title",
            marketplace_details=json.dumps(history_ok),
        )
        listing_bad = SimpleNamespace(
            id=2,
            marketplace="ebay",
            product=None,
            listing_title="Bad",
            marketplace_details="{",
        )
        rows = reports._build_listing_review_activity_rows(
            [listing, listing_bad],
            start_dt=datetime(2026, 1, 1),
            end_dt=datetime(2026, 1, 31),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_decision"], "approved")

    def test_build_listing_format_outcome_rows(self):
        published = SimpleNamespace(
            id=1,
            listed_at=datetime(2026, 1, 10),
            marketplace="ebay",
            product=SimpleNamespace(sku="SKU1"),
            listing_title="Published",
            review_status="approved",
            listing_status="active",
            external_listing_id="abc",
            marketplace_details=json.dumps({"ebay_publish": {"format": "AUCTION", "listing_duration": "DAYS_7", "history": [{"status": "published"}]}}),
        )
        failed = SimpleNamespace(
            id=2,
            listed_at=datetime(2026, 1, 11),
            marketplace="ebay",
            product=None,
            listing_title="Failed",
            review_status="pending",
            listing_status="draft",
            external_listing_id="",
            marketplace_details=json.dumps({"ebay_publish": {"history": [{"status": "failed", "error": "bad req"}]}}),
        )
        attempted = SimpleNamespace(
            id=3,
            listed_at=datetime(2026, 1, 12),
            marketplace="ebay",
            product=None,
            listing_title="Attempted",
            review_status="pending",
            listing_status="draft",
            external_listing_id="",
            marketplace_details=json.dumps({"ebay_publish": {"history": [{"status": "queued"}]}}),
        )
        untouched = SimpleNamespace(
            id=4,
            listed_at=datetime(2026, 1, 13),
            marketplace="ebay",
            product=None,
            listing_title="Untouched",
            review_status="pending",
            listing_status="draft",
            external_listing_id="",
            marketplace_details="",
        )
        rows = reports._build_listing_format_outcome_rows(
            [published, failed, attempted, untouched],
            start_dt=datetime(2026, 1, 1),
            end_dt=datetime(2026, 1, 31),
        )
        by_id = {r["listing_id"]: r for r in rows}
        self.assertEqual(by_id[1]["publish_outcome"], "published")
        self.assertEqual(by_id[2]["publish_outcome"], "publish_error")
        self.assertEqual(by_id[3]["publish_outcome"], "attempted_no_publish")
        self.assertEqual(by_id[4]["publish_outcome"], "not_attempted")


if __name__ == "__main__":
    unittest.main()
