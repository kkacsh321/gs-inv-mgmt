import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_sales_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("`2` sales", text)
        self.assertIn("Gross sold: `$180.00`", text)
        self.assertIn("Fees: `$18.00`", text)
        self.assertIn("Net (gross + shipping charged - fees - label spend): `$171.00`", text)
        self.assertEqual(citations[0]["table"], "sales")

    def test_sales_snapshot_prefers_repository_actual_economics(self):
        class ActualsRepo(_Repo):
            def report_sales_actual_econ_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "sale_id": 1,
                        "sold_price": 100.0,
                        "allocated_fee_actual": 7.5,
                        "allocated_shipping_charged": 5.0,
                        "allocated_shipping_actual": 4.25,
                        "net_before_cogs_actual": 93.25,
                    }
                ]

        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_sales_snapshot(ActualsRepo(), max_scan_rows=100)

        self.assertIn("`1` sales", text)
        self.assertIn("Gross sold: `$100.00`", text)
        self.assertIn("Fees: `$7.50`", text)
        self.assertIn("Label spend: `$4.25`", text)
        self.assertIn("Net (gross + shipping charged - fees - label spend): `$93.25`", text)
        self.assertEqual(citations[0]["table"], "sales, order_finance_entries")
        self.assertIn("normalized finance", citations[0]["finance_basis"])

    def test_sales_snapshot_rolls_back_failed_actual_economics_lookup(self):
        class FailingActualsRepo(_Repo):
            def __init__(self):
                super().__init__()
                self.db = SimpleNamespace(rollback=Mock())

            def report_sales_actual_econ_rows(self, *, start_dt, end_dt):
                raise RuntimeError("aborted transaction")

        repo = FailingActualsRepo()
        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_sales_snapshot(repo, max_scan_rows=100)

        self.assertIn("sale fields fallback", citations[0]["finance_basis"])
        self.assertIn("Net (gross + shipping charged - fees - label spend)", text)
        repo.db.rollback.assert_called_once()

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
        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_reports_snapshot(_Repo(), max_scan_rows=100)
        self.assertIn("Gross sold: `$180.00`", text)
        self.assertIn("Estimated COGS: `$50.00`", text)
        self.assertIn("Estimated margin before returns: `$121.00`", text)
        self.assertEqual(len(citations), 4)
        self.assertEqual(citations[0]["table"], "sales")
        self.assertEqual(citations[1]["table"], "returns")

    def test_reports_snapshot_prefers_repository_actual_economics(self):
        class ActualsRepo(_Repo):
            def report_sales_actual_econ_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "sale_id": 1,
                        "sold_price": 100.0,
                        "allocated_fee_actual": 7.5,
                        "allocated_shipping_charged": 5.0,
                        "allocated_shipping_actual": 4.25,
                        "net_before_cogs_actual": 93.25,
                    }
                ]

        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_reports_snapshot(ActualsRepo(), max_scan_rows=100)

        self.assertIn("Gross sold: `$100.00`", text)
        self.assertIn("Fees + label spend: `$11.75`", text)
        self.assertIn("Estimated COGS: `$50.00`", text)
        self.assertIn("Estimated margin before returns: `$43.25`", text)
        self.assertEqual(citations[0]["table"], "sales, order_finance_entries")
        self.assertIn("normalized finance", citations[0]["finance_basis"])

    def test_reports_snapshot_includes_return_adjusted_profit_when_returns_present(self):
        class ReturnsRepo(_Repo):
            def __init__(self):
                super().__init__()
                for idx, sale in enumerate(self._sales, start=10):
                    sale.id = idx

            def report_sale_unit_cost_maps(self, *, end_dt, default_unit_cost_by_product):
                return {
                    "fifo_unit_cost_by_sale": {10: 20.0, 12: 30.0},
                    "fifo_unit_cost_source_by_sale": {
                        10: "lot_expected_quantity_fallback",
                        12: "product_default_landed_cost",
                    },
                }

            def report_returns_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "return_id": 1,
                        "sale_id": 10,
                        "quantity": 1,
                        "refund_amount": 30.0,
                        "refund_fees": 2.0,
                        "refund_shipping": 3.0,
                    }
                ]

        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_reports_snapshot(ReturnsRepo(), max_scan_rows=100)

        self.assertIn("Estimated margin before returns: `$101.00`", text)
        self.assertIn("Return refunds: `$35.00`", text)
        self.assertIn("Return COGS reversal: `$20.00`", text)
        self.assertIn("Return profit impact: `$-15.00`", text)
        self.assertIn("Estimated profit after returns: `$86.00`", text)
        self.assertEqual(citations[0]["returns_count"], 1)
        self.assertEqual(citations[0]["profit_before_returns"], 101.0)
        self.assertEqual(citations[0]["estimated_profit_after_returns"], 86.0)

    def test_accounting_snapshot_includes_exceptions_and_lot_sources(self):
        class AccountingRepo(_Repo):
            def report_accounting_exception_rows(self, *, start_dt, end_dt):
                return [
                    {"exception_type": "missing_cost_basis", "severity": "P1"},
                    {"exception_type": "lot_equal_fallback_review_needed", "severity": "P2"},
                ]

            def report_lot_assignment_rows(self, *, start_dt=None, end_dt=None):
                return [
                    {"cost_source": "lot_allocation_weight"},
                    {"cost_source": "lot_equal_quantity_fallback"},
                    {"cost_source": "missing_cost_basis"},
                ]

        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_accounting_snapshot(AccountingRepo(), max_scan_rows=100)

        self.assertIn("AI Accountant snapshot", text)
        self.assertIn("Profit before returns", text)
        self.assertIn("Estimated profit after returns", text)
        self.assertIn("missing_cost_basis", text)
        self.assertIn("AI Accountant questions to answer", text)
        self.assertIn("What cost-basis evidence should we use", text)
        self.assertIn("Recent AI Accountant operator answers recorded: `0`", text)
        self.assertIn("Recent AI Accountant answer follow-ups recorded: `0`", text)
        self.assertIn("fallback/missing-basis rows", text)
        self.assertEqual(citations[-5]["table"], "accounting_exception_queue")
        self.assertEqual(citations[-4]["table"], "ai_accountant_monitor_questions")
        self.assertEqual(citations[-3]["table"], "ai_accountant_answers")
        self.assertEqual(citations[-2]["table"], "ai_accountant_answer_followups")
        self.assertEqual(citations[-1]["table"], "product_lot_assignments")

    def test_accounting_snapshots_prefer_repository_fifo_cost_maps(self):
        class RepoWithCostMaps(_Repo):
            def __init__(self):
                super().__init__()
                for idx, sale in enumerate(self._sales, start=10):
                    sale.id = idx

            def report_sale_unit_cost_maps(self, *, end_dt, default_unit_cost_by_product):
                return {
                    "fifo_unit_cost_by_sale": {10: 11.0, 12: 31.0},
                    "fifo_unit_cost_source_by_sale": {
                        10: "lot_expected_quantity_fallback",
                        12: "lot_allocation_weight",
                    },
                    "fifo_remaining_unit_cost_by_product": {1: 12.0, 3: 35.0},
                    "lot_weighted_unit_cost_by_product": {1: 10.0, 3: 30.0},
                }

        repo = RepoWithCostMaps()
        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            inventory_text, inventory_citations = ccb.build_inventory_snapshot(repo, max_scan_rows=100)
            reports_text, reports_citations = ccb.build_reports_snapshot(repo, max_scan_rows=100)

        self.assertIn("Estimated inventory cost basis: `$165.00`", inventory_text)
        self.assertIn("Estimated COGS: `$53.00`", reports_text)
        self.assertIn("Estimated margin before returns: `$118.00`", reports_text)
        self.assertIn("Sold COGS source mix:", reports_text)
        self.assertIn("`lot_allocation_weight`: `$31.00`", reports_text)
        self.assertIn("`lot_expected_quantity_fallback`: `$22.00`", reports_text)
        self.assertIn("FIFO remaining lot cost", inventory_citations[0]["cost_basis"])
        self.assertIn("time-aware FIFO sale COGS", reports_citations[0]["cost_basis"])
        self.assertEqual(
            reports_citations[0]["cogs_source_mix"]["lot_expected_quantity_fallback"]["sale_rows"],
            1,
        )

    def test_inventory_snapshot_rolls_back_failed_cost_map_lookup(self):
        class FailingCostMapRepo(_Repo):
            def __init__(self):
                super().__init__()
                self.db = SimpleNamespace(rollback=Mock())

            def report_sale_unit_cost_maps(self, *, end_dt, default_unit_cost_by_product):
                raise RuntimeError("aborted transaction")

        repo = FailingCostMapRepo()
        with patch.object(ccb, "utcnow_naive", lambda: datetime(2026, 3, 30, 12, 0, 0)):
            text, citations = ccb.build_inventory_snapshot(repo, max_scan_rows=100)

        self.assertIn("Estimated inventory cost basis", text)
        self.assertIn("product landed acquisition cost", citations[0]["cost_basis"])
        repo.db.rollback.assert_called_once()

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
