import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import requests

from app.services import sync_jobs


class _FakeRepo:
    def __init__(self) -> None:
        self.created_runs: list[dict] = []
        self.updated_runs: list[tuple[int, dict, str]] = []
        self.events: list[dict] = []
        self.next_run_id = 100

    def create_sync_run(self, **kwargs):
        self.created_runs.append(kwargs)
        run = SimpleNamespace(id=self.next_run_id)
        self.next_run_id += 1
        return run

    def update_sync_run(self, run_id: int, updates: dict, *, actor: str):
        self.updated_runs.append((run_id, updates, actor))

    def add_sync_event(self, **kwargs):
        self.events.append(kwargs)


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, *, products=None, listings=None, sales_by_id=None):
        self.products = products or []
        self.listings = listings or []
        self.sales_by_id = sales_by_id or {}
        self._scalar_calls = 0

    def scalars(self, _query):
        self._scalar_calls += 1
        # First scalars() call in orders import is Product list.
        if self._scalar_calls == 1:
            return _FakeScalarResult(self.products)
        # Remaining scalars() calls for listing selects can share the same rows.
        return _FakeScalarResult(self.listings)

    def scalar(self, _query):
        return None

    def get(self, _model, row_id):
        return self.sales_by_id.get(int(row_id))


class _FakeRepoWithDB(_FakeRepo):
    def __init__(self, *, db):
        super().__init__()
        self.db = db
        self.errors = []

    def add_sync_error(self, **kwargs):
        self.errors.append(kwargs)


class _BuildItemsDB:
    def __init__(self, existing_listing=None):
        self.existing_listing = existing_listing

    def scalar(self, _query):
        return self.existing_listing


class _BuildItemsRepo:
    def __init__(self, *, existing_listing=None, create_listing_raises: bool = False):
        self.db = _BuildItemsDB(existing_listing=existing_listing)
        self.create_listing_raises = create_listing_raises
        self.created_listing_args = []
        self._next_id = 700

    def create_listing(self, **kwargs):
        self.created_listing_args.append(kwargs)
        if self.create_listing_raises:
            raise RuntimeError("create failed")
        row = SimpleNamespace(id=self._next_id)
        self._next_id += 1
        return row


class _UpsertDB:
    def __init__(self, *, existing_order=None, existing_sales=None):
        self.existing_order = existing_order
        self.existing_sales = existing_sales or []

    def scalar(self, _query):
        return self.existing_order

    def scalars(self, _query):
        return _FakeScalarResult(self.existing_sales)


class _UpsertRepo:
    def __init__(self, *, existing_order=None, existing_sales=None):
        self.db = _UpsertDB(existing_order=existing_order, existing_sales=existing_sales)
        self.errors = []
        self.created_orders = []
        self.updated_orders = []
        self.created_sales = []
        self.updated_sales = []

    def add_sync_error(self, **kwargs):
        self.errors.append(kwargs)

    def create_order(self, **kwargs):
        self.created_orders.append(kwargs)
        return SimpleNamespace(id=501)

    def update_order(self, order_id: int, updates: dict, *, actor: str):
        self.updated_orders.append((order_id, updates, actor))
        return SimpleNamespace(id=order_id)

    def create_sale(self, **kwargs):
        self.created_sales.append(kwargs)
        return SimpleNamespace(id=800 + len(self.created_sales))

    def update_sale(self, sale_id: int, updates: dict, *, actor: str):
        self.updated_sales.append((sale_id, updates, actor))
        return SimpleNamespace(id=sale_id)


class _FakeSlackConfig:
    def __init__(self, enabled: bool, notify_sync_failures: bool) -> None:
        self.enabled = enabled
        self.notify_sync_failures = notify_sync_failures


class SyncJobsTests(unittest.TestCase):
    def test_csv_set_uses_default_for_empty(self) -> None:
        self.assertEqual(sync_jobs._csv_set("", {"a", "b"}), {"a", "b"})
        self.assertEqual(sync_jobs._csv_set("  ", {"a"}), {"a"})

    def test_csv_set_normalizes_values(self) -> None:
        result = sync_jobs._csv_set(" Failed, partial ,FAILED ", {"x"})
        self.assertEqual(result, {"failed", "partial"})

    def test_to_decimal_and_sum_line_totals_helpers(self) -> None:
        self.assertEqual(sync_jobs._to_decimal("1.25"), Decimal("1.25"))
        self.assertEqual(sync_jobs._to_decimal("bad"), Decimal("0"))
        self.assertEqual(sync_jobs._sum_line_totals([{"unit_price": "2.5", "quantity": 2}]), Decimal("5.0"))

    def test_sync_job_retry_policy_defaults_without_repo(self) -> None:
        policy = sync_jobs.sync_job_retry_policy("ebay_orders_pull_import", repo=None)
        self.assertEqual(policy["max_retries"], 3)
        self.assertEqual(policy["retry_backoff_seconds"], 0)
        self.assertEqual(policy["retryable_statuses"], ["failed", "partial"])
        self.assertEqual(policy["terminal_statuses"], ["failed", "partial", "success"])

    def test_sync_job_retry_policy_uses_runtime_values(self) -> None:
        with patch("app.services.sync_jobs.get_runtime_int", side_effect=[9, 15]), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=["failed,partial,error", "success,failed,partial,cancelled"],
        ):
            policy = sync_jobs.sync_job_retry_policy("ebay_orders_pull_import", repo=object())
        self.assertEqual(policy["max_retries"], 9)
        self.assertEqual(policy["retry_backoff_seconds"], 15)
        self.assertEqual(policy["retryable_statuses"], ["error", "failed", "partial"])
        self.assertEqual(policy["terminal_statuses"], ["cancelled", "failed", "partial", "success"])

    def test_sync_job_dispatch_meta_known_and_unknown(self) -> None:
        self.assertTrue(sync_jobs.sync_job_dispatch_meta("ebay_orders_pull_import")["supports_execute_now"])
        self.assertFalse(sync_jobs.sync_job_dispatch_meta("unknown")["supports_execute_now"])

    def test_extract_orders(self) -> None:
        self.assertEqual(sync_jobs._extract_orders({"orders": [{"id": 1}]}), [{"id": 1}])
        self.assertEqual(sync_jobs._extract_orders({"orders": {}}), [])

    def test_parse_ebay_datetime_valid_and_invalid(self) -> None:
        parsed = sync_jobs._parse_ebay_datetime("2026-03-29T12:00:00Z")
        self.assertEqual(parsed, datetime(2026, 3, 29, 12, 0, 0))
        self.assertIsNone(sync_jobs._parse_ebay_datetime("not-a-date"))
        self.assertIsNone(sync_jobs._parse_ebay_datetime(""))
        self.assertIsNone(sync_jobs._parse_ebay_datetime("   "))

    def test_map_order_status(self) -> None:
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "fulfilled"}), "delivered")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "shipped"}), "shipped")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "cancelled"}), "cancelled")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "payment_failed"}), "refunded")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "other"}), "paid")

    def test_map_tracking_status(self) -> None:
        self.assertEqual(sync_jobs._map_tracking_status("delivered", True), "delivered")
        self.assertEqual(sync_jobs._map_tracking_status("shipped", True), "in_transit")
        self.assertEqual(sync_jobs._map_tracking_status("cancelled", True), "exception")
        self.assertEqual(sync_jobs._map_tracking_status("unknown", False), "")

    def test_extract_shipping_enrichment_prefers_latest_fulfillment(self) -> None:
        order = {"orderFulfillmentStatus": "shipped", "creationDate": "2026-03-01T01:00:00Z"}
        fulfillments = [
            {
                "trackingNumber": "OLD123",
                "shippingCarrierCode": "usps",
                "shippedDate": "2026-03-01T01:00:00Z",
            },
            {
                "trackingNumber": "NEW999",
                "shippingCarrierCode": "ups",
                "shippedDate": "2026-03-02T01:00:00Z",
            },
        ]
        out = sync_jobs._extract_shipping_enrichment(order, fulfillments=fulfillments)
        self.assertEqual(out["tracking_number"], "NEW999")
        self.assertEqual(out["shipping_provider"], "ups")
        self.assertEqual(out["tracking_status"], "in_transit")

    def test_extract_shipping_enrichment_delivered_fallback(self) -> None:
        order = {
            "orderFulfillmentStatus": "fulfilled",
            "creationDate": "2026-03-01T01:00:00Z",
            "lastModifiedDate": "2026-03-05T03:00:00Z",
        }
        out = sync_jobs._extract_shipping_enrichment(order, fulfillments=[])
        self.assertEqual(out["tracking_status"], "label_created")
        self.assertEqual(out["delivered_at"], datetime(2026, 3, 5, 3, 0, 0))

    def test_extract_shipping_enrichment_sort_fallback_on_bad_rows(self) -> None:
        order = {"orderFulfillmentStatus": "shipped", "creationDate": "2026-03-01T01:00:00Z"}
        out = sync_jobs._extract_shipping_enrichment(
            order,
            fulfillments=[{"trackingNumber": "A1", "shippingCarrierCode": "usps"}, 5],
        )
        self.assertEqual(out["tracking_number"], "A1")
        self.assertEqual(out["shipping_provider"], "usps")

    def test_build_order_items_fallback_when_no_line_items(self) -> None:
        rows, listings_created, linked, unmapped = sync_jobs._build_order_items(
            {"pricingSummary": {"total": {"value": "44.50"}}},
            repo=_BuildItemsRepo(),
            product_map={},
            listing_map={},
            sku_listing_candidates={},
            actor="qa",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit_price"], Decimal("44.50"))
        self.assertEqual(listings_created, 0)
        self.assertEqual(linked, 0)
        self.assertEqual(unmapped, 0)

    def test_build_order_items_counts_unmapped_sku(self) -> None:
        rows, _created, _linked, unmapped = sync_jobs._build_order_items(
            {
                "lineItems": [
                    {
                        "sku": "MISSING-SKU",
                        "legacyItemId": "",
                        "quantity": 1,
                        "lineItemCost": {"value": "3.00"},
                        "title": "Missing Product",
                    }
                ]
            },
            repo=_BuildItemsRepo(),
            product_map={},
            listing_map={},
            sku_listing_candidates={},
            actor="qa",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(unmapped, 1)

    def test_build_order_items_uses_existing_listing_when_create_fails(self) -> None:
        existing_listing = SimpleNamespace(id=77)
        repo = _BuildItemsRepo(existing_listing=existing_listing, create_listing_raises=True)
        listing_map = {}
        rows, listings_created, linked, unmapped = sync_jobs._build_order_items(
            {
                "lineItems": [
                    {
                        "sku": "SKU1",
                        "legacyItemId": "LEG-1",
                        "quantity": 1,
                        "lineItemCost": {"value": "10.00"},
                        "title": "Listing A",
                    }
                ]
            },
            repo=repo,
            product_map={"SKU1": 1},
            listing_map=listing_map,
            sku_listing_candidates={},
            actor="qa",
        )
        self.assertEqual(rows[0]["listing_id"], 77)
        self.assertEqual(listings_created, 0)
        self.assertEqual(linked, 1)
        self.assertEqual(unmapped, 0)
        self.assertEqual(listing_map["LEG-1"], 77)

    def test_build_order_items_ambiguous_candidate_selection_paths(self) -> None:
        c1 = SimpleNamespace(id=11, listing_title="Alpha", listing_price=Decimal("9.99"), listing_status="draft")
        c2 = SimpleNamespace(id=12, listing_title="Bravo", listing_price=Decimal("10.00"), listing_status="draft")
        c3 = SimpleNamespace(id=13, listing_title="Charlie", listing_price=Decimal("8.50"), listing_status="active")

        # title exact match wins
        rows_a, _, linked_a, _ = sync_jobs._build_order_items(
            {"lineItems": [{"sku": "SKU2", "quantity": 1, "lineItemCost": {"value": "12.00"}, "title": "Bravo"}]},
            repo=_BuildItemsRepo(),
            product_map={"SKU2": 2},
            listing_map={},
            sku_listing_candidates={"SKU2": [c1, c2, c3]},
            actor="qa",
        )
        self.assertEqual(rows_a[0]["listing_id"], 12)
        self.assertEqual(linked_a, 1)

        # price match wins when title does not
        rows_b, _, linked_b, _ = sync_jobs._build_order_items(
            {"lineItems": [{"sku": "SKU2", "quantity": 1, "lineItemCost": {"value": "8.50"}, "title": "No Match"}]},
            repo=_BuildItemsRepo(),
            product_map={"SKU2": 2},
            listing_map={},
            sku_listing_candidates={"SKU2": [c1, c2, c3]},
            actor="qa",
        )
        self.assertEqual(rows_b[0]["listing_id"], 13)
        self.assertEqual(linked_b, 1)

        # active status wins when title and price are ambiguous
        c4 = SimpleNamespace(id=14, listing_title="Delta", listing_price=Decimal("7.00"), listing_status="draft")
        c5 = SimpleNamespace(id=15, listing_title="Echo", listing_price=Decimal("7.00"), listing_status="active")
        rows_c, _, linked_c, _ = sync_jobs._build_order_items(
            {"lineItems": [{"sku": "SKU3", "quantity": 1, "lineItemCost": {"value": "7.00"}, "title": "No Match"}]},
            repo=_BuildItemsRepo(),
            product_map={"SKU3": 3},
            listing_map={},
            sku_listing_candidates={"SKU3": [c4, c5]},
            actor="qa",
        )
        self.assertEqual(rows_c[0]["listing_id"], 15)
        self.assertEqual(linked_c, 1)

    def test_ebay_carrier_code_mapping(self) -> None:
        self.assertEqual(sync_jobs._ebay_carrier_code("usps"), "USPS")
        self.assertEqual(sync_jobs._ebay_carrier_code("pirateship"), "USPS")
        self.assertEqual(sync_jobs._ebay_carrier_code("unknown"), "OTHER")

    def test_notify_sync_status_slack_only_for_failed_or_partial(self) -> None:
        repo = object()
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.build_slack_alert_text", return_value="alert"
        ) as build_text, patch("app.services.sync_jobs.dispatch_slack_alert") as dispatch:
            sync_jobs._notify_sync_status_slack(
                repo,
                job_name="job",
                run_id=1,
                status="success",
                processed=3,
                failed=0,
                actor="qa",
            )
            dispatch.assert_not_called()
            sync_jobs._notify_sync_status_slack(
                repo,
                job_name="job",
                run_id=2,
                status="failed",
                processed=1,
                failed=1,
                actor="qa",
            )
            build_text.assert_called_once()
            dispatch.assert_called_once()

    def test_notify_sync_status_slack_swallows_internal_errors(self) -> None:
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.build_slack_alert_text", return_value="alert"
        ), patch("app.services.sync_jobs.dispatch_slack_alert", side_effect=RuntimeError("boom")):
            # should not raise because alert dispatch failures are intentionally non-fatal
            sync_jobs._notify_sync_status_slack(
                object(),
                job_name="job",
                run_id=99,
                status="failed",
                processed=1,
                failed=1,
                actor="qa",
            )

    def test_execute_sync_job_rejects_disabled(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=False):
            with self.assertRaisesRegex(ValueError, "disabled"):
                sync_jobs.execute_sync_job(object(), job_name="ebay_orders_pull_import", actor="qa")

    def test_execute_sync_job_dispatches_shopify_scaffold(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.execute_shopify_orders_pull_scaffold",
            return_value={"run_id": 5, "status": "success"},
        ) as scaffold:
            result = sync_jobs.execute_sync_job(
                object(),
                job_name="shopify_orders_pull",
                actor="qa",
                shop_domain="example.myshopify.com",
                access_token="tok",
                limit=10,
                offset=2,
            )
        self.assertEqual(result["status"], "success")
        scaffold.assert_called_once()

    def test_execute_sync_job_dispatches_ebay_jobs(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.execute_ebay_orders_pull_import",
            return_value={"run_id": 1, "status": "success"},
        ) as pull, patch(
            "app.services.sync_jobs.execute_ebay_shipping_tracking_push",
            return_value={"run_id": 2, "status": "success"},
        ) as push, patch(
            "app.services.sync_jobs.execute_ebay_connection_health_check",
            return_value={"run_id": 3, "status": "success"},
        ) as health:
            out1 = sync_jobs.execute_sync_job(
                object(),
                job_name="ebay_orders_pull_import",
                actor="qa",
                access_token="tok",
                limit=3,
                offset=1,
            )
            out2 = sync_jobs.execute_sync_job(
                object(),
                job_name="ebay_shipping_tracking_push",
                actor="qa",
                access_token="tok",
                sale_ids=(1, 2, 0),
            )
            out3 = sync_jobs.execute_sync_job(
                object(),
                job_name="ebay_connection_health_check",
                actor="qa",
                access_token="tok",
            )
        self.assertEqual(out1["status"], "success")
        self.assertEqual(out2["status"], "success")
        self.assertEqual(out3["status"], "success")
        pull.assert_called_once()
        push.assert_called_once()
        health.assert_called_once()

    def test_sync_job_catalog_contains_retry_and_dispatch_metadata(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.sync_job_retry_policy", return_value={"max_retries": 3}
        ):
            rows = sync_jobs.sync_job_catalog(repo=object())
        self.assertTrue(rows)
        self.assertIn("retry_policy", rows[0])
        self.assertIn("dispatch_meta", rows[0])

    def test_is_sync_job_enabled_uses_settings_without_repo(self) -> None:
        fake_settings = SimpleNamespace(
            sync_job_ebay_orders_pull_import_enabled=True,
            sync_job_ebay_shipping_tracking_push_enabled=False,
            sync_job_ebay_connection_health_check_enabled=True,
            sync_job_quickbooks_export_enabled=False,
            sync_job_shopify_orders_pull_enabled=True,
        )
        with patch("app.services.sync_jobs.settings", fake_settings):
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_orders_pull_import", repo=None))
            self.assertFalse(sync_jobs.is_sync_job_enabled("ebay_shipping_tracking_push", repo=None))
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_connection_health_check", repo=None))

    def test_is_sync_job_enabled_uses_runtime_when_repo_present(self) -> None:
        def _runtime_bool(_repo, key, default):
            mapping = {
                "sync_job_ebay_orders_pull_import_enabled": True,
                "sync_job_ebay_shipping_tracking_push_enabled": False,
                "sync_job_ebay_connection_health_check_enabled": True,
                "sync_job_quickbooks_export_enabled": True,
                "sync_job_shopify_orders_pull_enabled": False,
            }
            return mapping.get(key, default)

        with patch("app.services.sync_jobs.get_runtime_bool", side_effect=_runtime_bool):
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_orders_pull_import", repo=object()))
            self.assertFalse(sync_jobs.is_sync_job_enabled("ebay_shipping_tracking_push", repo=object()))
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_connection_health_check", repo=object()))

    def test_execute_sync_job_unknown_raises(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            with self.assertRaises(NotImplementedError):
                sync_jobs.execute_sync_job(object(), job_name="unknown_job", actor="qa")

    def test_execute_shopify_scaffold_creates_and_completes_run(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str", side_effect=lambda *_args, **_kwargs: ""
        ), patch(
            "app.services.sync_jobs.get_runtime_int", side_effect=lambda *_args, **_kwargs: 50
        ):
            result = sync_jobs.execute_shopify_orders_pull_scaffold(
                repo,
                actor="qa",
                shop_domain="shop.example",
                access_token="token",
                limit=20,
                offset=0,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(repo.created_runs), 1)
        self.assertGreaterEqual(len(repo.updated_runs), 2)  # running + completed
        self.assertEqual(len(repo.events), 1)

    def test_execute_shopify_scaffold_uses_existing_run_id(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str", side_effect=lambda *_args, **_kwargs: ""
        ), patch(
            "app.services.sync_jobs.get_runtime_int", side_effect=lambda *_args, **_kwargs: 50
        ):
            result = sync_jobs.execute_shopify_orders_pull_scaffold(
                repo,
                actor="qa",
                run_id=777,
            )
        self.assertEqual(result["run_id"], 777)
        self.assertEqual(len(repo.created_runs), 0)
        self.assertGreaterEqual(len(repo.updated_runs), 2)

    def test_execute_shopify_scaffold_rejects_disabled(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=False):
            with self.assertRaisesRegex(ValueError, "disabled"):
                sync_jobs.execute_shopify_orders_pull_scaffold(_FakeRepo(), actor="qa")

    def test_execute_ebay_orders_pull_import_validation(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            with self.assertRaisesRegex(ValueError, "Access token is required"):
                sync_jobs.execute_ebay_orders_pull_import(
                    _FakeRepo(),
                    access_token="",
                    actor="qa",
                    client=object(),
                )

    def test_execute_ebay_orders_pull_import_success_and_partial_and_failed(self) -> None:
        orders = [{"orderId": "A"}, {"orderId": "B"}]
        client = SimpleNamespace(pull_recent_orders=lambda *_args, **_kwargs: {"orders": orders})
        db = _FakeDB(
            products=[SimpleNamespace(id=1, sku="SKU1")],
            listings=[SimpleNamespace(id=10, product_id=1, external_listing_id="L1", listing_price=1, listing_status="active")],
        )
        repo = _FakeRepoWithDB(db=db)

        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs._upsert_ebay_order_into_local",
            side_effect=[
                {
                    "orders_created": 1,
                    "orders_updated": 0,
                    "sales_created": 1,
                    "sales_skipped": 0,
                    "sales_updated": 0,
                    "listings_created": 0,
                    "line_items_with_listing_link": 1,
                    "line_items_unmapped_sku": 0,
                },
                RuntimeError("boom"),
            ],
        ):
            out = sync_jobs.execute_ebay_orders_pull_import(
                repo,
                access_token="tok",
                actor="qa",
                client=client,
            )
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["failed"], 1)
        self.assertTrue(any(e.get("code") == "EBAY_ORDER_IMPORT_FAILED" for e in repo.errors))

        db2 = _FakeDB(
            products=[SimpleNamespace(id=1, sku="SKU1")],
            listings=[SimpleNamespace(id=10, product_id=1, external_listing_id="L1", listing_price=1, listing_status="active")],
        )
        repo2 = _FakeRepoWithDB(db=db2)
        client2 = SimpleNamespace(pull_recent_orders=lambda *_args, **_kwargs: {"orders": [{"orderId": "A"}]})
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs._upsert_ebay_order_into_local", side_effect=RuntimeError("all bad")
        ):
            out2 = sync_jobs.execute_ebay_orders_pull_import(
                repo2,
                access_token="tok",
                actor="qa",
                client=client2,
            )
        self.assertEqual(out2["status"], "failed")

    def test_execute_ebay_orders_pull_import_pull_failure_updates_run_and_reraises(self) -> None:
        repo = _FakeRepoWithDB(db=_FakeDB())
        client = SimpleNamespace(pull_recent_orders=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pull fail")))
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            with self.assertRaises(RuntimeError):
                sync_jobs.execute_ebay_orders_pull_import(repo, access_token="tok", actor="qa", client=client)
        self.assertTrue(any(e.get("code") == "EBAY_PULL_FAILED" for e in repo.errors))
        self.assertTrue(any(u[1].get("status") == "failed" for u in repo.updated_runs))

    def test_execute_ebay_orders_pull_import_refreshes_token_on_auth_failure(self) -> None:
        repo = _FakeRepoWithDB(db=_FakeDB(products=[], listings=[]))

        auth_error = requests.HTTPError("expired")
        auth_error.response = SimpleNamespace(status_code=401)

        class _Client:
            def __init__(self):
                self.calls = 0

            def pull_recent_orders(self, token, limit=25, offset=0):
                self.calls += 1
                if self.calls == 1:
                    raise auth_error
                return {"orders": []}

            def refresh_user_token(self, refresh_token, scopes=None):
                _ = scopes
                if refresh_token != "ref-1":
                    raise RuntimeError("bad refresh")
                return {"access_token": "tok-2", "refresh_token": "ref-2", "expires_in": 7200}

        client = _Client()
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=lambda _repo, key, default: "ref-1" if key == "ebay_user_refresh_token" else default,
        ):
            out = sync_jobs.execute_ebay_orders_pull_import(
                repo,
                access_token="tok-1",
                actor="qa",
                client=client,
            )
        self.assertEqual(out["status"], "success")
        self.assertEqual(client.calls, 2)

    def test_upsert_ebay_order_updates_existing_order_and_sales(self) -> None:
        existing_order = SimpleNamespace(id=41)
        existing_sales = [SimpleNamespace(id=91), SimpleNamespace(id=92)]
        repo = _UpsertRepo(existing_order=existing_order, existing_sales=existing_sales)
        ebay_client = SimpleNamespace(
            list_shipping_fulfillments=lambda **_kwargs: [
                {"trackingNumber": "TRK-1", "shippingCarrierCode": "usps", "shippedDate": "2026-03-01T01:00:00Z"}
            ]
        )
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [{"product_id": 1, "listing_id": 5, "quantity": 1, "unit_price": Decimal("25.00")}],
                0,
                1,
                0,
            ),
        ):
            out = sync_jobs._upsert_ebay_order_into_local(
                repo,
                {"orderId": "ORD-1", "creationDate": "2026-03-01T01:00:00Z", "pricingSummary": {}},
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=123,
            )
        self.assertEqual(out["orders_updated"], 1)
        self.assertEqual(out["orders_created"], 0)
        self.assertEqual(out["sales_skipped"], 1)
        self.assertEqual(out["sales_updated"], 2)
        self.assertEqual(len(repo.updated_orders), 1)
        self.assertEqual(len(repo.updated_sales), 2)

    def test_upsert_ebay_order_creates_order_and_sales_even_when_fulfillment_enrich_fails(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(
            list_shipping_fulfillments=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fulfillment down"))
        )
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [
                    {"product_id": 1, "listing_id": 5, "quantity": 1, "unit_price": Decimal("0.00")},
                    {"product_id": 2, "listing_id": 6, "quantity": 1, "unit_price": Decimal("0.00")},
                ],
                1,
                2,
                0,
            ),
        ):
            out = sync_jobs._upsert_ebay_order_into_local(
                repo,
                {
                    "orderId": "ORD-2",
                    "creationDate": "2026-03-01T01:00:00Z",
                    "pricingSummary": {
                        "deliveryCost": {"value": "4.00"},
                        "totalMarketplaceFee": {"value": "2.00"},
                    },
                },
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=124,
            )
        self.assertEqual(out["orders_created"], 1)
        self.assertEqual(out["sales_created"], 2)
        self.assertEqual(len(repo.created_orders), 1)
        self.assertEqual(len(repo.created_sales), 2)
        self.assertTrue(any(err.get("code") == "EBAY_ORDER_FULFILLMENT_ENRICH_FAILED" for err in repo.errors))

    def test_execute_ebay_shipping_tracking_push_validation(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            with self.assertRaisesRegex(ValueError, "Access token is required"):
                sync_jobs.execute_ebay_shipping_tracking_push(
                    _FakeRepo(),
                    access_token="",
                    actor="qa",
                    sale_ids=[1],
                    client=object(),
                )
            with self.assertRaisesRegex(ValueError, "At least one sale ID"):
                sync_jobs.execute_ebay_shipping_tracking_push(
                    _FakeRepo(),
                    access_token="token",
                    actor="qa",
                    sale_ids=[],
                    client=object(),
                )
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=False):
            with self.assertRaisesRegex(ValueError, "disabled"):
                sync_jobs.execute_ebay_shipping_tracking_push(
                    _FakeRepo(),
                    access_token="token",
                    actor="qa",
                    sale_ids=[1],
                    client=object(),
                )

    def test_execute_ebay_shipping_tracking_push_paths(self) -> None:
        sale_ok = SimpleNamespace(
            id=1,
            marketplace="ebay",
            external_order_id="ORD1",
            tracking_number="TRK1",
            shipping_provider="usps",
            shipped_at=None,
        )
        sale_bad_market = SimpleNamespace(
            id=2,
            marketplace="amazon",
            external_order_id="ORD2",
            tracking_number="TRK2",
            shipping_provider="ups",
            shipped_at=None,
        )
        sale_missing = SimpleNamespace(
            id=3,
            marketplace="ebay",
            external_order_id="",
            tracking_number="",
            shipping_provider="usps",
            shipped_at=None,
        )
        db = _FakeDB(sales_by_id={1: sale_ok, 2: sale_bad_market, 3: sale_missing})
        repo = _FakeRepoWithDB(db=db)
        client = SimpleNamespace(
            get_order=lambda **_kwargs: {"lineItems": [{"lineItemId": "LI1", "lineItemQuantity": 1}]},
            create_shipping_fulfillment=lambda **_kwargs: {"ok": True},
        )
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            out = sync_jobs.execute_ebay_shipping_tracking_push(
                repo,
                access_token="tok",
                actor="qa",
                sale_ids=[999, 2, 3, 1],
                client=client,
            )
        self.assertEqual(out["processed"], 3)
        self.assertEqual(out["updated"], 1)
        self.assertEqual(out["failed"], 3)
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any(e.get("code") == "EBAY_TRACKING_PUSH_SALE_NOT_FOUND" for e in repo.errors))

    def test_execute_ebay_shipping_tracking_push_api_failure_partial_status(self) -> None:
        sale_ok = SimpleNamespace(
            id=10,
            marketplace="ebay",
            external_order_id="ORD10",
            tracking_number="TRK10",
            shipping_provider="usps",
            shipped_at=None,
        )
        sale_fail = SimpleNamespace(
            id=11,
            marketplace="ebay",
            external_order_id="ORD11",
            tracking_number="TRK11",
            shipping_provider="ups",
            shipped_at=None,
        )
        db = _FakeDB(sales_by_id={10: sale_ok, 11: sale_fail})
        repo = _FakeRepoWithDB(db=db)

        def _create_shipping_fulfillment(**kwargs):
            if kwargs.get("order_id") == "ORD11":
                raise RuntimeError("push failed")
            return {"ok": True}

        client = SimpleNamespace(
            get_order=lambda **_kwargs: {"lineItems": [{"lineItemId": "", "lineItemQuantity": 1}]},
            create_shipping_fulfillment=_create_shipping_fulfillment,
        )
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            out = sync_jobs.execute_ebay_shipping_tracking_push(
                repo,
                access_token="tok",
                actor="qa",
                sale_ids=[10, 11],
                client=client,
            )
        self.assertEqual(out["processed"], 2)
        self.assertEqual(out["updated"], 1)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any(e.get("code") == "EBAY_TRACKING_PUSH_FAILED" for e in repo.errors))

    def test_execute_ebay_shipping_tracking_push_refreshes_on_auth_failure(self) -> None:
        sale = SimpleNamespace(
            id=20,
            marketplace="ebay",
            external_order_id="ORD20",
            tracking_number="TRK20",
            shipping_provider="usps",
            shipped_at=None,
        )
        repo = _FakeRepoWithDB(db=_FakeDB(sales_by_id={20: sale}))
        auth_error = requests.HTTPError("expired")
        auth_error.response = SimpleNamespace(status_code=401)

        class _Client:
            def __init__(self):
                self.order_calls = 0
                self.push_calls = 0

            def get_order(self, **_kwargs):
                self.order_calls += 1
                if self.order_calls == 1:
                    raise auth_error
                return {"lineItems": [{"lineItemId": "LI20", "lineItemQuantity": 1}]}

            def create_shipping_fulfillment(self, **_kwargs):
                self.push_calls += 1
                return {"ok": True}

            def refresh_user_token(self, refresh_token, scopes=None):
                _ = scopes
                if refresh_token != "ref-ship":
                    raise RuntimeError("bad refresh")
                return {"access_token": "tok-ship-2", "refresh_token": "ref-ship-2", "expires_in": 7200}

        client = _Client()
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=lambda _repo, key, default: "ref-ship" if key == "ebay_user_refresh_token" else default,
        ):
            out = sync_jobs.execute_ebay_shipping_tracking_push(
                repo,
                access_token="tok-ship-1",
                actor="qa",
                sale_ids=[20],
                client=client,
            )
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["updated"], 1)
        self.assertEqual(client.order_calls, 2)

    def test_execute_ebay_connection_health_check_success(self) -> None:
        repo = _FakeRepo()

        class _Client:
            SCOPES = []

            def is_configured(self):
                return True

            def decode_access_token_claims(self, _token):
                return {"scope": "https://api.ebay.com/oauth/api_scope/sell.account"}

            def get_account_privileges(self, _token):
                return {"sellerRegistrationCompleted": True}

            def get_identity_user(self, _token):
                return {"username": "goldenstackers"}

        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=lambda _repo, key, default="": "tok" if key == "ebay_user_access_token" else "",
        ):
            out = sync_jobs.execute_ebay_connection_health_check(
                repo,
                actor="qa",
                client=_Client(),
            )
        self.assertEqual(out["status"], "success")
        self.assertEqual(len(repo.created_runs), 1)
        self.assertGreaterEqual(len(repo.updated_runs), 2)
        self.assertTrue(repo.events)


if __name__ == "__main__":
    unittest.main()
