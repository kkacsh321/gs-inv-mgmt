from datetime import datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
import types
import unittest


def _bootstrap_views_package() -> None:
    root = Path(__file__).resolve().parents[1]
    views_path = str(root / "app" / "components" / "views")
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
        pkg.__path__ = [views_path]
        sys.modules["app.components.views"] = pkg
    else:
        existing_path = list(getattr(sys.modules["app.components.views"], "__path__", []) or [])
        if views_path not in existing_path:
            sys.modules["app.components.views"].__path__ = [views_path, *existing_path]

    for name in ("shared", "entity_ops"):
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
    module_path = root / "app" / "components" / "views" / "customers.py"
    spec = importlib.util.spec_from_file_location("test_customers_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


customers = _load_module()


class CustomersViewHelperTests(unittest.TestCase):
    def test_days_since_datetime_normalizes_future_and_timezone_values(self):
        now = datetime(2026, 6, 2, 12, 0, 0)

        self.assertEqual(customers._days_since_datetime(datetime(2026, 6, 1, 23, 0, 0), now=now), 1)
        self.assertEqual(customers._days_since_datetime(datetime(2026, 6, 3, 12, 0, 0), now=now), 0)
        self.assertIsNone(customers._days_since_datetime(None, now=now))

    def test_customer_follow_up_status_buckets(self):
        self.assertEqual(customers._customer_follow_up_status(0, None), "No orders")
        self.assertEqual(customers._customer_follow_up_status(1, 12), "Recent")
        self.assertEqual(customers._customer_follow_up_status(1, 45), "Warm")
        self.assertEqual(customers._customer_follow_up_status(1, 90), "Dormant 90d+")

    def test_customer_contact_summary_uses_identity_email_and_address(self):
        summary = customers._customer_contact_summary(
            {
                "display_name": "Buyer Name",
                "shipping_name": "Ship Name",
                "ebay_username": "buyer42",
                "primary_email": "buyer@example.com",
                "shipping_address": "15892 W 1st Dr | Golden, CO, 80401 | US",
            }
        )

        self.assertEqual(
            summary,
            "Buyer Name | buyer@example.com | 15892 W 1st Dr | Golden, CO, 80401 | US",
        )

    def test_filter_order_rows_matches_query_and_status(self):
        rows = [
            {
                "external_order_id": "ORDER-1",
                "status": "shipped",
                "items": "Morgan Dollar",
                "tracking_number": "TRACK1",
                "ship_to": "Golden, CO",
            },
            {
                "external_order_id": "ORDER-2",
                "status": "paid",
                "items": "Silver Eagle",
                "tracking_number": "",
                "ship_to": "Denver, CO",
            },
        ]

        filtered = customers._filter_order_rows(rows, query="morgan", statuses=["shipped"])

        self.assertEqual(filtered, [rows[0]])
        self.assertEqual(customers._filter_order_rows(rows, query="denver", statuses=["shipped"]), [])

    def test_customer_row_normalizes_rollup_fields(self):
        row = customers._customer_row(
            types.SimpleNamespace(
                id=7,
                marketplace="ebay",
                ebay_username="buyer42",
                display_name="Buyer Name",
                primary_email="buyer@example.com",
                shipping_name="Buyer Name",
                shipping_address_line1="15892 W 1st Dr",
                shipping_city="Golden",
                shipping_state="CO",
                shipping_postal_code="80401",
                shipping_country="US",
                order_count=3,
                total_spend=Decimal("123.45"),
                is_repeat_buyer=True,
                notes="Prefers combined shipping and asks about Morgan dollars. " * 8,
                first_order_at=datetime(2026, 1, 1, 12, 0, 0),
                last_order_at=datetime(2026, 2, 1, 12, 0, 0),
            )
        )

        self.assertEqual(row["id"], 7)
        self.assertEqual(row["ebay_username"], "buyer42")
        self.assertEqual(row["order_count"], 3)
        self.assertEqual(row["total_spend"], 123.45)
        self.assertIn("15892 W 1st Dr", row["shipping_address"])
        self.assertTrue(row["is_repeat_buyer"])
        self.assertIn(row["follow_up_status"], {"Recent", "Warm", "Dormant 90d+"})
        self.assertIsInstance(row["days_since_last_order"], int)
        self.assertTrue(row["has_internal_notes"])
        self.assertLessEqual(len(row["notes_preview"]), 180)
        self.assertIn("combined shipping", row["notes_preview"])
        self.assertIn("2026-02-01", row["last_order_at"])

    def test_customer_row_notes_preview_empty_without_notes(self):
        row = customers._customer_row(types.SimpleNamespace(id=1, notes=""))

        self.assertFalse(row["has_internal_notes"])
        self.assertEqual(row["notes_preview"], "")

    def test_order_row_includes_status_and_ship_to_summary(self):
        item = types.SimpleNamespace(
            id=5,
            quantity=2,
            product=types.SimpleNamespace(sku="GS-COIN-1", title="Coin"),
            listing=None,
        )
        row = customers._order_row(
            types.SimpleNamespace(
                id=9,
                marketplace="ebay",
                external_order_id="ORDER-1",
                order_status="shipped",
                sold_at=datetime(2026, 1, 1, 12, 0, 0),
                subtotal_amount=Decimal("25.00"),
                shipping_cost=Decimal("5.00"),
                shipping_label_cost=Decimal("4.25"),
                total_amount=Decimal("25.00"),
                shipping_service="USPS",
                tracking_status="in_transit",
                tracking_number="TRACK1",
                ship_to_city="Golden",
                ship_to_state="CO",
                ship_to_postal_code="80401",
                ship_to_country="US",
                items=[item],
            )
        )

        self.assertEqual(row["status"], "shipped")
        self.assertEqual(row["shipping_charged"], 5.0)
        self.assertEqual(row["label_spend"], 4.25)
        self.assertEqual(row["ship_to"], "Golden, CO, 80401, US")
        self.assertEqual(row["item_count"], 1)
        self.assertEqual(row["items"], "2x GS-COIN-1")

    def test_order_items_summary_caps_long_item_lists(self):
        order = types.SimpleNamespace(
            items=[
                types.SimpleNamespace(id=1, quantity=1, product=types.SimpleNamespace(sku="SKU-1"), listing=None),
                types.SimpleNamespace(id=2, quantity=1, product=types.SimpleNamespace(sku="SKU-2"), listing=None),
                types.SimpleNamespace(id=3, quantity=1, product=types.SimpleNamespace(sku="SKU-3"), listing=None),
                types.SimpleNamespace(id=4, quantity=1, product=types.SimpleNamespace(sku="SKU-4"), listing=None),
            ]
        )

        self.assertEqual(
            customers._order_items_summary(order, max_items=2),
            "SKU-1; SKU-2; +2 more",
        )


if __name__ == "__main__":
    unittest.main()
