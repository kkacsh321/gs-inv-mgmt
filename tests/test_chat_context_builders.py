import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.services import chat_context_builders as ccb


class _Repo:
    def __init__(self) -> None:
        now = datetime(2026, 3, 30, 12, 0, 0)
        self._products = [
            SimpleNamespace(id=1, sku="SKU-1", title="One", current_quantity=5, acquisition_cost=10.0),
            SimpleNamespace(id=2, sku="SKU-2", title="Two", current_quantity=0, acquisition_cost=20.0),
            SimpleNamespace(id=3, sku="SKU-3", title="Three", current_quantity=3, acquisition_cost=30.0),
        ]
        self._listings = [
            SimpleNamespace(listing_status="draft", review_status="pending"),
            SimpleNamespace(listing_status="active", review_status="approved"),
            SimpleNamespace(listing_status="ended", review_status="rejected"),
        ]
        self._sales = [
            SimpleNamespace(
                sold_at=now - timedelta(days=5),
                sold_price=100.0,
                fees=10.0,
                shipping_cost=5.0,
                product_id=1,
                quantity_sold=2,
                tracking_status="in_transit",
                tracking_number="",
                shipping_exception_code="",
            ),
            SimpleNamespace(
                sold_at=now - timedelta(days=40),
                sold_price=50.0,
                fees=5.0,
                shipping_cost=3.0,
                product_id=3,
                quantity_sold=1,
                tracking_status="delivered",
                tracking_number="TRK",
                shipping_exception_code="EX",
            ),
            SimpleNamespace(
                sold_at=now - timedelta(days=1),
                sold_price=80.0,
                fees=8.0,
                shipping_cost=4.0,
                product_id=3,
                quantity_sold=1,
                tracking_status="label_created",
                tracking_number="TRK2",
                shipping_exception_code="EX2",
            ),
        ]
        self._orders = [
            SimpleNamespace(order_status="paid"),
            SimpleNamespace(order_status="completed"),
            SimpleNamespace(order_status="cancelled"),
        ]
        self._sync_runs = [
            SimpleNamespace(id=1, provider="ebay", job_name="job1", status="failed", completed_at=now),
            SimpleNamespace(id=2, provider="ebay", job_name="job2", status="queued", completed_at=None),
            SimpleNamespace(id=3, provider="ebay", job_name="job3", status="running", completed_at=None),
        ]
        self._users = [
            SimpleNamespace(is_active=True),
            SimpleNamespace(is_active=False),
        ]
        self._runtime = [
            SimpleNamespace(is_active=True),
            SimpleNamespace(is_active=False),
        ]
        self._ai = [
            SimpleNamespace(is_active=True),
            SimpleNamespace(is_active=True),
        ]

    def list_products(self):
        return list(self._products)

    def list_listings(self):
        return list(self._listings)

    def list_sales(self):
        return list(self._sales)

    def list_orders(self):
        return list(self._orders)

    def list_sync_runs(self, limit=100):
        return list(self._sync_runs)[:limit]

    def list_app_users(self, active_only=False):
        return list(self._users)

    def list_runtime_settings(self, environment="local", active_only=False):
        return list(self._runtime)

    def list_ai_provider_configs(self, environment="local", active_only=False):
        return list(self._ai)


class ChatContextBuildersTests(unittest.TestCase):
    def test_money_and_safe_scan(self):
        self.assertEqual(ccb._money(1234.5), "$1,234.50")
        rows, capped = ccb._safe_scan_rows([1, 2], max_rows=3)
        self.assertEqual(rows, [1, 2])
        self.assertFalse(capped)
        rows2, capped2 = ccb._safe_scan_rows([1, 2, 3], max_rows=2)
        self.assertEqual(rows2, [1, 2])
        self.assertTrue(capped2)

    def test_inventory_snapshot(self):
        text, citations = ccb.build_inventory_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("2` SKUs with stock", text)
        self.assertIn("`8` total units", text)
        self.assertIn("SKU-1", text)
        self.assertEqual(citations[0]["table"], "products")
        self.assertEqual(citations[0]["rows_considered"], 2)

    def test_listings_snapshot(self):
        text, citations = ccb.build_listings_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("Draft: `1`", text)
        self.assertIn("Active: `1`", text)
        self.assertIn("Ended: `1`", text)
        self.assertIn("Pending/Not approved review: `2`", text)
        self.assertEqual(citations[0]["table"], "marketplace_listings")

    def test_sales_snapshot(self):
        text, citations = ccb.build_sales_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("`2` sales", text)
        self.assertIn("Gross sold: `$180.00`", text)
        self.assertIn("Fees: `$18.00`", text)
        self.assertEqual(citations[0]["table"], "sales")

    def test_shipping_snapshot(self):
        text, citations = ccb.build_shipping_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("`2` not yet delivered", text)
        self.assertIn("Missing tracking number: `1`", text)
        self.assertIn("With shipping exception code: `1`", text)
        self.assertEqual(citations[0]["table"], "sales")

    def test_sync_snapshot(self):
        text, citations = ccb.build_sync_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("Failed/Partial: `1`", text)
        self.assertIn("Queued: `1`", text)
        self.assertIn("Running: `1`", text)
        self.assertIn("Most recent failed/partial runs", text)
        self.assertEqual(citations[0]["table"], "sync_runs")

    def test_orders_snapshot(self):
        text, citations = ccb.build_orders_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("`3` total orders", text)
        self.assertIn("`1`", text)
        self.assertEqual(citations[0]["table"], "orders")

    def test_reports_snapshot(self):
        text, citations = ccb.build_reports_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("Gross sold: `$180.00`", text)
        self.assertIn("Estimated COGS: `$50.00`", text)
        self.assertIn("Estimated margin: `$103.00`", text)
        self.assertEqual(len(citations), 3)
        self.assertEqual(citations[0]["table"], "sales")

    def test_admin_snapshot_and_fallback_help(self):
        with patch.object(ccb, "settings", SimpleNamespace(app_env="local")):
            text, citations = ccb.build_admin_snapshot(_Repo(), max_scan_rows=100)
            self.assertIn("Admin snapshot (`local` environment)", text)
            self.assertIn("App users: `2` total (`1` active)", text)
            self.assertIn("Runtime settings: `2` total (`1` active)", text)
            self.assertIn("AI runtime profiles: `2` total (`2` active)", text)
            self.assertEqual(len(citations), 4)
            self.assertEqual(citations[0]["table"], "app_users")

        help_text, help_citations = ccb.build_fallback_help()
        self.assertIn("inventory snapshot", help_text)
        self.assertEqual(help_citations, [])


if __name__ == "__main__":
    unittest.main()
