import unittest
from datetime import datetime
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import patch
import requests

from app.services import sync_jobs


class _FakeRepo:
    def __init__(self) -> None:
        self.created_runs: list[dict] = []
        self.updated_runs: list[tuple[int, dict, str]] = []
        self.events: list[dict] = []
        self.errors: list[dict] = []
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

    def add_sync_error(self, **kwargs):
        self.errors.append(kwargs)

    def get_runtime_setting(self, *, environment: str, key: str, active_only: bool = True):
        return None


class _StoreCategorySyncRepo(_FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.upserted_categories: list[dict] = []
        self.reconcile_calls: list[dict] = []

    def upsert_ebay_store_category(self, **kwargs):
        self.upserted_categories.append(kwargs)
        return SimpleNamespace(id=len(self.upserted_categories), category_path=kwargs.get("category_path"))

    def reconcile_ebay_store_category_sync(self, **kwargs):
        self.reconcile_calls.append(kwargs)
        return {"missing_count": 1, "deactivated_count": 1 if kwargs.get("deactivate_missing") else 0}


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
    def __init__(self, existing_listing=None, listings_by_id=None):
        self.existing_listing = existing_listing
        self.listings_by_id = listings_by_id or {}

    def scalar(self, _query):
        return self.existing_listing

    def get(self, _model, row_id):
        return self.listings_by_id.get(int(row_id))


class _BuildItemsRepo:
    def __init__(self, *, existing_listing=None, listings_by_id=None, create_listing_raises: bool = False):
        self.db = _BuildItemsDB(existing_listing=existing_listing, listings_by_id=listings_by_id)
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
    def __init__(self, *, existing_order=None, existing_sales=None, existing_order_items=None):
        self.existing_order = existing_order
        self.existing_sales = existing_sales or []
        self.existing_order_items = existing_order_items or []
        self._scalars_calls = 0

    def scalar(self, _query):
        return self.existing_order

    def scalars(self, _query):
        self._scalars_calls += 1
        if self._scalars_calls == 2:
            return _FakeScalarResult(self.existing_order_items)
        return _FakeScalarResult(self.existing_sales)

    def commit(self):
        return None


class _UpsertRepo:
    def __init__(self, *, existing_order=None, existing_sales=None, existing_order_items=None):
        self.db = _UpsertDB(
            existing_order=existing_order,
            existing_sales=existing_sales,
            existing_order_items=existing_order_items,
        )
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


class _ListingReconcileDB:
    def __init__(self, *, listing=None, total_sold: int = 0):
        self._listing = listing
        self._total_sold = total_sold

    def get(self, _model, _id):
        return self._listing

    def scalar(self, _query):
        return self._total_sold


class _ListingReconcileRepo:
    def __init__(self, *, listing=None, total_sold: int = 0):
        self.db = _ListingReconcileDB(listing=listing, total_sold=total_sold)
        self.updated = []

    def update_listing(self, listing_id: int, updates: dict, *, actor: str):
        self.updated.append((listing_id, updates, actor))
        return SimpleNamespace(id=listing_id)


class _FakeSlackConfig:
    def __init__(self, enabled: bool, notify_sync_failures: bool) -> None:
        self.enabled = enabled
        self.notify_sync_failures = notify_sync_failures


class SyncJobsTests(unittest.TestCase):
    def test_transient_ebay_network_error_detection(self) -> None:
        exc = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.ebay.com', port=443): "
            "Max retries exceeded with url: /identity/v1/oauth2/token "
            "(Caused by NameResolutionError(\"Failed to resolve 'api.ebay.com'\"))"
        )
        self.assertTrue(sync_jobs._is_transient_ebay_network_error(exc))
        self.assertFalse(sync_jobs._is_transient_ebay_network_error(RuntimeError("bad credentials")))

    def test_maybe_auto_refresh_skips_during_failure_cooldown(self) -> None:
        repo = _FakeRepo()
        now = datetime(2026, 4, 18, 12, 0, 0)
        with patch("app.services.sync_jobs.get_runtime_bool", return_value=True), patch(
            "app.services.sync_jobs._resolve_ebay_tokens",
            return_value=("access", "refresh"),
        ), patch("app.services.sync_jobs.get_runtime_int", side_effect=[12, 45, 30]), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=[
                "2026-04-18T15:00:00",  # expires_at
                "2026-04-18T11:00:00",  # refreshed_at
                "2026-04-18T11:45:00",  # failed_at => cooldown active until 12:15
            ],
        ), patch("app.services.sync_jobs.utcnow_naive", return_value=now), patch(
            "app.services.sync_jobs._refresh_ebay_access_token",
        ) as refresh_mock, patch("app.services.sync_jobs.EbayClient") as client_cls:
            client_cls.return_value.is_configured.return_value = True
            result = sync_jobs.maybe_auto_refresh_ebay_user_token(repo, actor="qa")
        refresh_mock.assert_not_called()
        self.assertEqual(result.get("status"), "skipped")
        self.assertEqual(result.get("reason"), "failure_cooldown_active")
        self.assertEqual(result.get("retry_at"), "2026-04-18T12:15:00")

    def test_maybe_auto_refresh_skips_when_refresh_token_missing(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.sync_jobs.get_runtime_bool", return_value=True), patch(
            "app.services.sync_jobs._resolve_ebay_tokens",
            return_value=("access", ""),
        ), patch(
            "app.services.sync_jobs.EbayClient"
        ) as client_cls:
            client_cls.return_value.is_configured.return_value = True
            result = sync_jobs.maybe_auto_refresh_ebay_user_token(repo, actor="qa")
        self.assertEqual(result.get("status"), "skipped")
        self.assertEqual(result.get("reason"), "missing_refresh_token")

    def test_maybe_auto_refresh_classifies_transient_network_failure(self) -> None:
        repo = _FakeRepo()
        now = datetime(2026, 4, 18, 12, 0, 0)
        transient = requests.ConnectionError("Failed to resolve 'api.ebay.com'")
        with patch("app.services.sync_jobs.get_runtime_bool", return_value=True), patch(
            "app.services.sync_jobs._resolve_ebay_tokens",
            return_value=("access", "refresh"),
        ), patch("app.services.sync_jobs.get_runtime_int", side_effect=[12, 45, 30]), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=[
                "2026-04-18T12:10:00",
                "2026-04-18T10:00:00",
                "",
            ],
        ), patch("app.services.sync_jobs.utcnow_naive", return_value=now), patch(
            "app.services.sync_jobs._refresh_ebay_access_token",
            side_effect=transient,
        ), patch(
            "app.services.sync_jobs._persist_ebay_refresh_failure_state"
        ) as persist_failure, patch("app.services.sync_jobs.EbayClient") as client_cls:
            client_cls.return_value.is_configured.return_value = True
            result = sync_jobs.maybe_auto_refresh_ebay_user_token(repo, actor="qa")
        persist_failure.assert_called_once()
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result.get("reason"), "transient_network_unavailable")

    def test_maybe_auto_refresh_refreshes_when_near_expiry(self) -> None:
        repo = _FakeRepo()
        now = datetime(2026, 4, 18, 12, 0, 0)
        with patch("app.services.sync_jobs.get_runtime_bool", return_value=True), patch(
            "app.services.sync_jobs._resolve_ebay_tokens",
            return_value=("access", "refresh"),
        ), patch("app.services.sync_jobs.get_runtime_int", side_effect=[12, 45, 30]), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=[
                "2026-04-18T12:10:00",  # expires_at
                "2026-04-18T10:00:00",  # refreshed_at
                "",  # failed_at
                "2026-04-18T14:00:00",  # post-refresh expires_at lookup
            ],
        ), patch("app.services.sync_jobs.utcnow_naive", return_value=now), patch(
            "app.services.sync_jobs._refresh_ebay_access_token",
            return_value=("new-access", "new-refresh"),
        ) as refresh_mock, patch("app.services.sync_jobs.EbayClient") as client_cls:
            client_cls.return_value.is_configured.return_value = True
            result = sync_jobs.maybe_auto_refresh_ebay_user_token(repo, actor="qa")
        refresh_mock.assert_called_once()
        self.assertEqual(result.get("status"), "refreshed")
        self.assertEqual(result.get("reason"), "near_expiry")
        self.assertTrue(result.get("access_token_present"))

    def test_maybe_auto_refresh_returns_failed_when_refresh_raises(self) -> None:
        class _Repo(_FakeRepo):
            def __init__(self) -> None:
                super().__init__()
                self.runtime_upserts = []

            def upsert_runtime_setting(self, **kwargs):
                self.runtime_upserts.append(kwargs)

        repo = _Repo()
        now = datetime(2026, 4, 18, 12, 0, 0)
        with patch("app.services.sync_jobs.get_runtime_bool", return_value=True), patch(
            "app.services.sync_jobs._resolve_ebay_tokens",
            return_value=("access", "refresh"),
        ), patch("app.services.sync_jobs.get_runtime_int", side_effect=[12, 45, 30]), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=[
                "2026-04-18T12:10:00",  # expires_at (near expiry => due)
                "2026-04-18T10:00:00",  # refreshed_at
                "",  # failed_at
            ],
        ), patch("app.services.sync_jobs.utcnow_naive", return_value=now), patch(
            "app.services.sync_jobs._refresh_ebay_access_token",
            side_effect=RuntimeError("refresh broke"),
        ), patch("app.services.sync_jobs.EbayClient") as client_cls:
            client_cls.return_value.is_configured.return_value = True
            result = sync_jobs.maybe_auto_refresh_ebay_user_token(repo, actor="qa")
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result.get("reason"), "refresh_failed")
        self.assertIn("refresh broke", str(result.get("error")))
        keys = {str(row.get("key") or "") for row in repo.runtime_upserts}
        self.assertIn("ebay_user_access_token_refresh_failed_at", keys)
        self.assertIn("ebay_user_access_token_refresh_last_error", keys)

    def test_persist_ebay_tokens_writes_expiry_timestamp_in_future(self) -> None:
        class _Repo:
            def __init__(self):
                self.calls = []

            def upsert_runtime_setting(self, **kwargs):
                self.calls.append(kwargs)

        repo = _Repo()
        fixed_now = datetime(2026, 4, 18, 12, 0, 0)
        with patch("app.services.sync_jobs.utcnow_naive", return_value=fixed_now):
            sync_jobs._persist_ebay_tokens(
                repo,
                actor="qa",
                access_token="tok",
                refresh_token="ref",
                expires_in=7200,
            )
        expires_call = next((c for c in repo.calls if c.get("key") == "ebay_user_access_token_expires_at"), None)
        self.assertIsNotNone(expires_call)
        self.assertEqual(expires_call.get("value"), "2026-04-18T14:00:00")

    def test_reconcile_listing_status_marks_sold_only_when_depleted(self) -> None:
        listing = SimpleNamespace(id=5, quantity_listed=2, listing_status="active")
        repo = _ListingReconcileRepo(listing=listing, total_sold=1)
        updated = sync_jobs._reconcile_listing_status_after_sale_import(
            repo=repo,
            listing_ids={5},
            actor="qa",
        )
        self.assertEqual(updated, 0)
        self.assertEqual(len(repo.updated), 0)

        repo2 = _ListingReconcileRepo(listing=listing, total_sold=2)
        updated2 = sync_jobs._reconcile_listing_status_after_sale_import(
            repo=repo2,
            listing_ids={5},
            actor="qa",
        )
        self.assertEqual(updated2, 1)
        self.assertEqual(repo2.updated[0][1].get("listing_status"), "sold")

    def test_persist_ebay_tokens_swallow_upsert_failures(self) -> None:
        class _Repo:
            def upsert_runtime_setting(self, **_kwargs):
                raise RuntimeError("db down")

        # Should not raise; persistence is best-effort.
        sync_jobs._persist_ebay_tokens(
            _Repo(),
            actor="qa",
            access_token="tok",
            refresh_token="ref",
            expires_in=7200,
        )

    def test_refresh_ebay_access_token_raises_when_access_missing(self) -> None:
        client = SimpleNamespace(refresh_user_token=lambda _refresh: {"refresh_token": "new-ref"})
        with self.assertRaisesRegex(ValueError, "returned no access_token"):
            sync_jobs._refresh_ebay_access_token(
                object(),
                ebay_client=client,
                actor="qa",
                refresh_token="ref",
            )

    def test_notify_ebay_order_import_slack_swallows_dispatch_errors(self) -> None:
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.get_runtime_bool", return_value=True
        ), patch("app.services.sync_jobs.get_runtime_str", return_value=""), patch(
            "app.services.sync_jobs.build_slack_alert_text",
            return_value="order-alert",
        ), patch(
            "app.services.sync_jobs.dispatch_slack_alert",
            side_effect=RuntimeError("boom"),
        ):
            sync_jobs._notify_ebay_order_import_slack(
                object(),
                ebay_order={"orderId": "1", "buyer": {"username": "u1"}},
                actor="qa",
            )

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

    def test_build_ebay_sync_order_note_includes_buyer_shipping_and_tax(self) -> None:
        ebay_order = {
            "buyer": {"username": "goldbuyer01"},
            "fulfillmentStartInstructions": [
                {
                    "shippingStep": {
                        "shippingServiceCode": "USPSGround",
                        "shipTo": {
                            "fullName": "Keith K",
                            "contactAddress": {
                                "addressLine1": "15892 W 1st Dr",
                                "city": "Golden",
                                "stateOrProvince": "CO",
                                "postalCode": "80401",
                                "countryCode": "US",
                            },
                        },
                    }
                }
            ],
        }
        pricing = {"totalTax": {"value": "2.33"}}
        note = sync_jobs._build_ebay_sync_order_note(
            prefix="Imported from eBay sync pull.",
            ebay_order=ebay_order,
            pricing=pricing,
        )
        self.assertIn("buyer=goldbuyer01", note)
        self.assertIn("shipping_service=USPSGround", note)
        self.assertIn("ship_to=Keith K, 15892 W 1st Dr, Golden, CO, 80401, US", note)
        self.assertIn("tax=2.33", note)

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

    def test_extract_shipping_service_fallback_paths(self) -> None:
        nested = sync_jobs._extract_ebay_shipping_service(
            {
                "fulfillmentStartInstructions": [
                    {"shippingStep": {"shippingService": {"name": "NestedService"}}}
                ]
            }
        )
        pricing = sync_jobs._extract_ebay_shipping_service(
            {"pricingSummary": {"deliveryCost": {"serviceName": "DeliveryName"}}}
        )
        self.assertEqual(nested, "NestedService")
        self.assertEqual(pricing, "DeliveryName")

    def test_extract_shipping_address_and_party_fields_with_shipping_address_fallback(self) -> None:
        order = {
            "buyer": {
                "username": "buyer-x",
                "buyerRegistrationAddress": {
                    "fullName": "Reg Name",
                    "email": "reg@example.com",
                    "contactAddress": {
                        "city": "Denver",
                        "stateOrProvince": "CO",
                        "postalCode": "80202",
                        "countryCode": "us",
                    },
                },
            },
            "fulfillmentStartInstructions": [5],
            "shippingAddress": {
                "name": "Ship Name",
                "addressLine1": "123 Main",
                "city": "Golden",
                "stateOrProvince": "CO",
                "postalCode": "80401",
                "countryCode": "US",
            },
        }
        address = sync_jobs._extract_ebay_shipping_address(order)
        party = sync_jobs._extract_ebay_party_fields(order)
        self.assertIn("Ship Name, 123 Main, Golden, CO, 80401, US", address)
        self.assertEqual(party["buyer_username"], "buyer-x")
        self.assertEqual(party["buyer_name"], "Reg Name")
        self.assertEqual(party["buyer_email"], "reg@example.com")
        self.assertEqual(party["ship_to_city"], "Denver")
        self.assertEqual(party["ship_to_country"], "US")

    def test_extract_ebay_tax_amount_fallbacks(self) -> None:
        self.assertEqual(sync_jobs._extract_ebay_tax_amount({"salesTax": {"value": "1.20"}}), Decimal("1.20"))
        self.assertEqual(sync_jobs._extract_ebay_tax_amount({"tax": {"value": "0.60"}}), Decimal("0.60"))
        self.assertEqual(sync_jobs._extract_ebay_tax_amount("bad"), Decimal("0"))

    def test_line_item_shipping_and_fee_helpers_cover_non_dict_and_nested_rows(self) -> None:
        shipping = sync_jobs._extract_order_shipping_charged(
            {
                "lineItems": [
                    "bad-row",
                    {"deliveryCost": {"shippingCost": {"value": "3.00"}, "handlingCost": {"value": "1.00"}}},
                    {"lineItemShippingCost": "2.50"},
                ]
            },
            {},
        )
        fee = sync_jobs._extract_line_item_fee(
            {"marketplaceFees": [{"amount": {"value": "1.15"}}, {"value": "0.25"}, "bad"]}
        )
        non_dict_fee = sync_jobs._extract_line_item_fee("x")
        nested_shipping = sync_jobs._extract_line_item_shipping(
            {"deliveryCost": {"value": "5.00", "discountAmount": {"value": "1.00"}}}
        )
        non_dict_shipping = sync_jobs._extract_line_item_shipping("x")
        self.assertEqual(shipping, Decimal("6.50"))
        self.assertEqual(fee, Decimal("1.40"))
        self.assertEqual(non_dict_fee, Decimal("0"))
        self.assertEqual(nested_shipping, Decimal("4.00"))
        self.assertEqual(non_dict_shipping, Decimal("0"))

    def test_map_order_status(self) -> None:
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "fulfilled"}), "shipped")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "shipped"}), "shipped")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "in_progress"}), "packaging")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "cancelled"}), "cancelled")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "payment_failed"}), "refunded")
        self.assertEqual(sync_jobs._map_order_status({"orderFulfillmentStatus": "other"}), "not_shipped")

    def test_map_tracking_status(self) -> None:
        self.assertEqual(sync_jobs._map_tracking_status("delivered", True), "delivered")
        self.assertEqual(sync_jobs._map_tracking_status("fulfilled", True), "in_transit")
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
            "orderFulfillmentStatus": "delivered",
            "creationDate": "2026-03-01T01:00:00Z",
            "lastModifiedDate": "2026-03-05T03:00:00Z",
        }
        out = sync_jobs._extract_shipping_enrichment(order, fulfillments=[])
        self.assertEqual(out["tracking_status"], "label_created")
        self.assertEqual(out["delivered_at"], datetime(2026, 3, 5, 3, 0, 0))

    def test_extract_shipping_enrichment_fulfilled_without_delivery_date_not_delivered(self) -> None:
        order = {
            "orderFulfillmentStatus": "fulfilled",
            "creationDate": "2026-03-01T01:00:00Z",
            "lastModifiedDate": "2026-03-05T03:00:00Z",
        }
        out = sync_jobs._extract_shipping_enrichment(order, fulfillments=[])
        self.assertEqual(out["tracking_status"], "label_created")
        self.assertIsNone(out["delivered_at"])

    def test_extract_shipping_enrichment_shipped_delivery_date_without_delivered_signal(self) -> None:
        order = {
            "orderFulfillmentStatus": "shipped",
            "creationDate": "2026-03-01T01:00:00Z",
        }
        fulfillments = [
            {
                "trackingNumber": "TRACK123",
                "shippingCarrierCode": "usps",
                # Some payloads include a deliveryDate estimate while still in transit.
                "deliveryDate": "2026-03-06T12:00:00Z",
            }
        ]
        out = sync_jobs._extract_shipping_enrichment(order, fulfillments=fulfillments)
        self.assertEqual(out["tracking_status"], "in_transit")
        self.assertIsNone(out["delivered_at"])

    def test_extract_shipping_enrichment_sort_fallback_on_bad_rows(self) -> None:
        order = {"orderFulfillmentStatus": "shipped", "creationDate": "2026-03-01T01:00:00Z"}
        out = sync_jobs._extract_shipping_enrichment(
            order,
            fulfillments=[{"trackingNumber": "A1", "shippingCarrierCode": "usps"}, 5],
        )
        self.assertEqual(out["tracking_number"], "A1")
        self.assertEqual(out["shipping_provider"], "usps")

    def test_extract_shipping_label_spend_prefers_fulfillment_label_cost(self) -> None:
        amount, currency = sync_jobs._extract_shipping_label_spend(
            ebay_order={},
            fulfillments=[
                {"shippingLabelCost": {"value": "4.25", "currency": "USD"}},
                {"shippingLabelCost": {"value": "1.00", "currency": "USD"}},
            ],
        )
        self.assertEqual(amount, Decimal("5.25"))
        self.assertEqual(currency, "USD")

    def test_extract_shipping_label_spend_uses_payment_summary_fallback(self) -> None:
        amount, currency = sync_jobs._extract_shipping_label_spend(
            ebay_order={"paymentSummary": {"shippingLabelCost": {"value": "7.10", "currency": "USD"}}},
            fulfillments=[],
        )
        self.assertEqual(amount, Decimal("7.10"))
        self.assertEqual(currency, "USD")

    def test_extract_shipping_label_spend_supports_shipping_label_charges_shape(self) -> None:
        amount, currency = sync_jobs._extract_shipping_label_spend(
            ebay_order={},
            fulfillments=[
                {
                    "shippingLabelCharges": [
                        {"amount": {"value": "3.40", "currency": "USD"}},
                        {"amount": {"value": "0.60", "currency": "USD"}},
                    ]
                }
            ],
        )
        self.assertEqual(amount, Decimal("4.00"))
        self.assertEqual(currency, "USD")

    def test_extract_shipping_label_spend_from_finance_transactions(self) -> None:
        amount, currency = sync_jobs._extract_shipping_label_spend_from_transactions(
            order_id="23-14477-17302",
            transactions=[
                {
                    "transactionType": "SHIPPING_LABEL",
                    "orderId": "23-14477-17302",
                    "amount": {"value": "-8.12", "currency": "USD"},
                },
                {
                    "transactionType": "FINAL_VALUE_FEE",
                    "orderId": "23-14477-17302",
                    "amount": {"value": "-22.05", "currency": "USD"},
                },
            ],
        )
        self.assertEqual(amount, Decimal("8.12"))
        self.assertEqual(currency, "USD")

    def test_extract_shipping_label_spend_from_finance_transactions_matches_description_text(self) -> None:
        amount, currency = sync_jobs._extract_shipping_label_spend_from_transactions(
            order_id="23-14477-17302",
            transactions=[
                {
                    "transactionType": "OTHER",
                    "description": "Shipping label for order no. 23-14477-17302",
                    "amount": {"value": "-8.97", "currency": "USD"},
                    "totalFunds": {"value": "135.56", "currency": "USD"},
                }
            ],
        )
        self.assertEqual(amount, Decimal("8.97"))
        self.assertEqual(currency, "USD")

    def test_extract_marketplace_fee_from_finance_transactions_sale_total_fee(self) -> None:
        amount, currency = sync_jobs._extract_marketplace_fee_from_transactions(
            order_id="23-14477-17302",
            transactions=[
                {
                    "transactionType": "SALE",
                    "orderId": "23-14477-17302",
                    "totalFeeAmount": {"value": "22.05", "currency": "USD"},
                },
                {
                    "transactionType": "SHIPPING_LABEL",
                    "orderId": "23-14477-17302",
                    "amount": {"value": "8.97", "currency": "USD"},
                },
            ],
        )
        self.assertEqual(amount, Decimal("22.05"))
        self.assertEqual(currency, "USD")

    def test_build_order_finance_entries_creates_marketplace_fee_and_shipping_label_rows(self) -> None:
        rows = sync_jobs._build_order_finance_entries(
            order_id=2,
            external_order_id="23-14477-17302",
            marketplace="ebay",
            ebay_order={
                "lineItems": [
                    {
                        "lineItemId": "10080248303323",
                        "legacyItemId": "137217809542",
                        "sku": "DOC-44-0408",
                        "title": "Item",
                    }
                ]
            },
            finance_transactions=[
                {
                    "transactionId": "T-LABEL",
                    "orderId": "23-14477-17302",
                    "transactionType": "SHIPPING_LABEL",
                    "amount": {"value": "8.97", "currency": "USD"},
                    "bookingEntry": "DEBIT",
                    "transactionDate": "2026-04-13T13:16:09.905Z",
                },
                {
                    "transactionId": "T-SALE",
                    "orderId": "23-14477-17302",
                    "transactionType": "SALE",
                    "totalFeeAmount": {"value": "22.05", "currency": "USD"},
                    "orderLineItems": [
                        {
                            "lineItemId": "10080248303323",
                            "marketplaceFees": [
                                {
                                    "feeType": "FINAL_VALUE_FEE",
                                    "amount": {"value": "19.31", "currency": "USD"},
                                }
                            ],
                        }
                    ],
                },
            ],
        )
        kinds = {str(r.get("entry_kind")) for r in rows}
        self.assertIn("shipping_label", kinds)
        self.assertIn("marketplace_fee", kinds)

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

    def test_build_order_items_recovers_product_id_from_legacy_listing_match(self) -> None:
        listing = SimpleNamespace(id=206, product_id=2)
        rows, listings_created, linked, unmapped = sync_jobs._build_order_items(
            {
                "lineItems": [
                    {
                        "sku": "GS-CO-PR-26100-ABCD-L206",
                        "legacyItemId": "137394544357",
                        "quantity": 1,
                        "lineItemCost": {"value": "79.95"},
                        "title": "Full Tube of 20 1 oz American Prospector Copper Tribute Coins",
                    }
                ]
            },
            repo=_BuildItemsRepo(listings_by_id={206: listing}),
            product_map={},
            listing_map={"137394544357": 206},
            sku_listing_candidates={},
            actor="qa",
        )

        self.assertEqual(rows[0]["listing_id"], 206)
        self.assertEqual(rows[0]["product_id"], 2)
        self.assertEqual(listings_created, 0)
        self.assertEqual(linked, 1)
        self.assertEqual(unmapped, 0)

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

    def test_notify_ebay_order_import_slack_dispatches_for_created_order(self) -> None:
        ebay_order = {
            "orderId": "12-34567-89012",
            "orderFulfillmentStatus": "in_progress",
            "creationDate": "2026-04-11T18:00:00Z",
            "buyer": {"username": "goldbuyer01"},
            "pricingSummary": {
                "total": {"value": "104.55"},
                "deliveryCost": {"value": "9.99"},
                "totalTax": {"value": "5.56"},
            },
            "lineItems": [{"lineItemId": "x1"}, {"lineItemId": "x2"}],
            "fulfillmentStartInstructions": [
                {
                    "shippingStep": {
                        "shippingServiceCode": "USPSGround",
                        "shipTo": {
                            "fullName": "Keith K",
                            "contactAddress": {
                                "addressLine1": "15892 W 1st Dr",
                                "city": "Golden",
                                "stateOrProvince": "CO",
                                "postalCode": "80401",
                                "countryCode": "US",
                            },
                        },
                    }
                }
            ],
        }
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.get_runtime_bool", return_value=True
        ), patch("app.services.sync_jobs.get_runtime_str", return_value="#orders"), patch(
            "app.services.sync_jobs.build_slack_alert_text",
            return_value="order-alert",
        ) as build_text, patch("app.services.sync_jobs.dispatch_slack_alert") as dispatch:
            sync_jobs._notify_ebay_order_import_slack(
                object(),
                ebay_order=ebay_order,
                actor="qa",
            )
            build_text.assert_called_once()
            dispatch.assert_called_once()
            kwargs = dispatch.call_args.kwargs
            self.assertEqual(kwargs["event_type"], "order_imported")
            self.assertEqual(kwargs["override_channel"], "#orders")
            context = build_text.call_args.kwargs["context"]
            self.assertFalse(context["repeat_buyer"])
            self.assertIn("new/first observed buyer", context["repeat_line"])

    def test_notify_ebay_order_import_slack_includes_repeat_buyer_context(self) -> None:
        ebay_order = {
            "orderId": "ORDER-REPEAT",
            "buyer": {"username": "repeatbuyer"},
            "pricingSummary": {"total": {"value": "42.00"}},
            "lineItems": [],
        }
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.get_runtime_bool", return_value=True
        ), patch("app.services.sync_jobs.get_runtime_str", return_value="#orders"), patch(
            "app.services.sync_jobs.build_slack_alert_text",
            return_value="order-alert",
        ) as build_text, patch("app.services.sync_jobs.dispatch_slack_alert"):
            sync_jobs._notify_ebay_order_import_slack(
                object(),
                ebay_order=ebay_order,
                actor="qa",
                customer_context={
                    "repeat_buyer": True,
                    "customer_order_count": 3,
                    "customer_total_spend": 123.45,
                },
            )

        context = build_text.call_args.kwargs["context"]
        self.assertTrue(context["repeat_buyer"])
        self.assertEqual(context["customer_order_count"], 3)
        self.assertEqual(context["customer_total_spend"], "123.45")
        self.assertIn("repeat buyer", context["repeat_line"])

    def test_notify_ebay_order_import_slack_includes_customer_notes_preview(self) -> None:
        ebay_order = {
            "orderId": "ORDER-NOTES",
            "buyer": {"username": "repeatbuyer"},
            "pricingSummary": {"total": {"value": "42.00"}},
            "lineItems": [],
        }
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.get_runtime_bool", return_value=True
        ), patch("app.services.sync_jobs.get_runtime_str", return_value="#orders"), patch(
            "app.services.sync_jobs.build_slack_alert_text",
            return_value="order-alert",
        ) as build_text, patch("app.services.sync_jobs.dispatch_slack_alert"):
            sync_jobs._notify_ebay_order_import_slack(
                object(),
                ebay_order=ebay_order,
                actor="qa",
                customer_context={
                    "repeat_buyer": True,
                    "customer_order_count": 3,
                    "customer_total_spend": 123.45,
                    "customer_notes_preview": "Prefers combined shipping. " * 20,
                },
            )

        context = build_text.call_args.kwargs["context"]
        self.assertTrue(context["customer_has_internal_notes"])
        self.assertLessEqual(len(context["customer_notes_preview"]), 180)
        self.assertIn("Internal customer notes", context["customer_notes_line"])

    def test_notify_ebay_order_import_slack_respects_runtime_toggle(self) -> None:
        with patch("app.services.sync_jobs.resolve_slack_notify_config", return_value=_FakeSlackConfig(True, True)), patch(
            "app.services.sync_jobs.get_runtime_bool", return_value=False
        ), patch("app.services.sync_jobs.dispatch_slack_alert") as dispatch:
            sync_jobs._notify_ebay_order_import_slack(
                object(),
                ebay_order={"orderId": "1"},
                actor="qa",
            )
            dispatch.assert_not_called()

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

    def test_execute_sync_job_dispatches_quickbooks_export_dry_run(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.execute_quickbooks_export_dry_run",
            return_value={"run_id": 6, "status": "success"},
        ) as qbo:
            result = sync_jobs.execute_sync_job(
                object(),
                job_name="quickbooks_export",
                actor="qa",
                lookback_days=7,
            )
        self.assertEqual(result["status"], "success")
        qbo.assert_called_once()

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
        ) as health, patch(
            "app.services.sync_jobs.execute_ebay_store_categories_sync",
            return_value={"run_id": 4, "status": "success"},
        ) as store_sync:
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
            out4 = sync_jobs.execute_sync_job(
                object(),
                job_name="ebay_store_categories_sync",
                actor="qa",
                access_token="tok",
                marketplace_id="EBAY_US",
                deactivate_missing=True,
            )
        self.assertEqual(out1["status"], "success")
        self.assertEqual(out2["status"], "success")
        self.assertEqual(out3["status"], "success")
        self.assertEqual(out4["status"], "success")
        pull.assert_called_once()
        push.assert_called_once()
        health.assert_called_once()
        store_sync.assert_called_once()

    def test_sync_job_catalog_contains_retry_and_dispatch_metadata(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.sync_job_retry_policy", return_value={"max_retries": 3}
        ):
            rows = sync_jobs.sync_job_catalog(repo=object())
        self.assertTrue(rows)
        self.assertIn("retry_policy", rows[0])
        self.assertIn("dispatch_meta", rows[0])
        qbo = next(row for row in rows if row["job_name"] == "quickbooks_export")
        self.assertTrue(qbo["implemented"])
        self.assertTrue(qbo["dispatch_meta"]["supports_execute_now"])

    def test_store_categories_sync_dispatch_meta_avoids_order_import_retry_ui(self) -> None:
        meta = sync_jobs.sync_job_dispatch_meta("ebay_store_categories_sync")
        self.assertTrue(meta["supports_execute_now"])
        self.assertFalse(meta["supports_retry_execute_now"])
        self.assertIn("marketplace_id", meta["optional_args"])
        self.assertIn("deactivate_missing", meta["optional_args"])

    def test_quickbooks_export_dispatch_meta_supports_execute_now(self) -> None:
        meta = sync_jobs.sync_job_dispatch_meta("quickbooks_export")
        self.assertTrue(meta["supports_execute_now"])
        self.assertTrue(meta["supports_retry_execute_now"])
        self.assertIn("lookback_days", meta["optional_args"])
        self.assertIn("live_post", meta["optional_args"])

    def test_execute_quickbooks_export_dry_run_records_payload_manifest(self) -> None:
        class Repo(_FakeRepo):
            def report_sales_actual_econ_rows(self, *, start_dt, end_dt):
                return [
                    {
                        "sale_id": 66,
                        "sold_at": "2026-06-09T06:48:24",
                        "external_order_id": "13-14720-44255",
                        "marketplace": "ebay",
                        "sku": "DOC-3-0407",
                        "product_title": "1 oz American Prospector Copper Coin",
                        "qty": 20,
                        "sold_price": 79.95,
                        "allocated_fee_actual": 8.63,
                        "allocated_shipping_charged": 13.41,
                        "allocated_shipping_actual": 7.9,
                    }
                ]

        repo = Repo()
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            result = sync_jobs.execute_quickbooks_export_dry_run(
                repo,
                actor="qa",
                start_dt=datetime(2026, 6, 1),
                end_dt=datetime(2026, 6, 10),
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(repo.created_runs[0]["provider"], "quickbooks")
        self.assertEqual(repo.created_runs[0]["job_name"], "quickbooks_export")
        self.assertEqual(len(repo.events), 2)
        self.assertEqual(repo.events[0]["action"], "payload_preview_summary")
        summary_payload = json.loads(repo.events[0]["payload_json"])
        self.assertEqual(summary_payload["payload_rows"], 3)
        self.assertEqual(summary_payload["action_counts"]["sales_receipt"], 1)
        self.assertEqual(summary_payload["action_counts"]["order_fee_purchase"], 1)
        self.assertEqual(summary_payload["action_counts"]["shipping_label_purchase"], 1)
        self.assertTrue(summary_payload["evidence_sha256"])
        self.assertEqual(repo.updated_runs[-1][1]["status"], "success")

    def test_execute_quickbooks_export_dry_run_blocks_live_post(self) -> None:
        repo = _FakeRepo()
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            result = sync_jobs.execute_quickbooks_export_dry_run(
                repo,
                actor="qa",
                live_post=True,
                start_dt=datetime(2026, 6, 1),
                end_dt=datetime(2026, 6, 10),
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(repo.errors[0]["code"], "quickbooks_live_post_disabled")
        self.assertEqual(repo.updated_runs[-1][1]["status"], "failed")

    def test_is_sync_job_enabled_uses_settings_without_repo(self) -> None:
        fake_settings = SimpleNamespace(
            sync_job_ebay_orders_pull_import_enabled=True,
            sync_job_ebay_shipping_tracking_push_enabled=False,
            sync_job_ebay_connection_health_check_enabled=True,
            sync_job_ebay_store_categories_sync_enabled=True,
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
                "sync_job_ebay_store_categories_sync_enabled": True,
                "sync_job_quickbooks_export_enabled": True,
                "sync_job_shopify_orders_pull_enabled": False,
            }
            return mapping.get(key, default)

        with patch("app.services.sync_jobs.get_runtime_bool", side_effect=_runtime_bool):
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_orders_pull_import", repo=object()))
            self.assertFalse(sync_jobs.is_sync_job_enabled("ebay_shipping_tracking_push", repo=object()))
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_connection_health_check", repo=object()))
            self.assertTrue(sync_jobs.is_sync_job_enabled("ebay_store_categories_sync", repo=object()))

    def test_execute_sync_job_unknown_raises(self) -> None:
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            with self.assertRaises(NotImplementedError):
                sync_jobs.execute_sync_job(object(), job_name="unknown_job", actor="qa")

    def test_execute_ebay_store_categories_sync_imports_and_reconciles(self) -> None:
        repo = _StoreCategorySyncRepo()
        client = SimpleNamespace(
            is_configured=lambda: True,
            get_store_categories=lambda **_kwargs: {
                "ack": "Success",
                "site_id": "0",
                "categories": [
                    {
                        "category_path": "/Coins/Bullion",
                        "external_category_id": "101",
                        "sort_order": 2,
                    },
                    {
                        "category_path": "/Supplies",
                        "external_category_id": "102",
                        "sort_order": 3,
                    },
                ],
            },
        )
        settings = SimpleNamespace(
            app_env="local",
            ebay_marketplace_id="EBAY_US",
            ebay_user_access_token="",
            ebay_user_refresh_token="",
        )
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.settings", settings
        ), patch("app.services.sync_jobs.get_runtime_str", side_effect=lambda *_args, **_kwargs: ""):
            result = sync_jobs.execute_ebay_store_categories_sync(
                repo,
                actor="qa",
                access_token="tok",
                marketplace_id="EBAY_US",
                deactivate_missing=True,
                client=client,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["deactivated"], 1)
        self.assertEqual([row["category_path"] for row in repo.upserted_categories], ["/Coins/Bullion", "/Supplies"])
        self.assertTrue(all(row["mark_synced"] for row in repo.upserted_categories))
        self.assertEqual(repo.reconcile_calls[0]["synced_category_paths"], ["/Coins/Bullion", "/Supplies"])
        self.assertTrue(repo.reconcile_calls[0]["deactivate_missing"])
        self.assertEqual(repo.updated_runs[-1][1]["status"], "success")
        self.assertTrue(repo.events)

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

    def test_execute_ebay_orders_pull_import_hydrates_order_details_before_upsert(self) -> None:
        class _Client:
            def pull_recent_orders(self, *_args, **_kwargs):
                return {"orders": [{"orderId": "A", "buyer": {"username": "summary-user"}}]}

            def get_order(self, *, access_token: str, order_id: str):
                _ = access_token
                return {"orderId": order_id, "buyer": {"username": "detail-user"}}

        db = _FakeDB(products=[], listings=[])
        repo = _FakeRepoWithDB(db=db)
        captured_orders: list[dict] = []

        def _capture_order(_repo, ebay_order, **_kwargs):
            captured_orders.append(dict(ebay_order))
            return {
                "orders_created": 0,
                "orders_updated": 1,
                "sales_created": 0,
                "sales_skipped": 0,
                "sales_updated": 0,
                "listings_created": 0,
                "line_items_with_listing_link": 0,
                "line_items_unmapped_sku": 0,
            }

        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs._upsert_ebay_order_into_local",
            side_effect=_capture_order,
        ):
            out = sync_jobs.execute_ebay_orders_pull_import(
                repo,
                access_token="tok",
                actor="qa",
                client=_Client(),
            )

        self.assertEqual(out["status"], "success")
        self.assertEqual(len(captured_orders), 1)
        self.assertEqual(captured_orders[0].get("buyer", {}).get("username"), "detail-user")

    def test_execute_ebay_orders_pull_import_hydrate_failure_falls_back_to_summary_payload(self) -> None:
        class _Client:
            def pull_recent_orders(self, *_args, **_kwargs):
                return {"orders": [{"orderId": "A", "buyer": {"username": "summary-user"}}]}

            def get_order(self, *, access_token: str, order_id: str):
                _ = (access_token, order_id)
                raise RuntimeError("detail unavailable")

        db = _FakeDB(products=[], listings=[])
        repo = _FakeRepoWithDB(db=db)
        captured_orders: list[dict] = []

        def _capture_order(_repo, ebay_order, **_kwargs):
            captured_orders.append(dict(ebay_order))
            return {
                "orders_created": 0,
                "orders_updated": 1,
                "sales_created": 0,
                "sales_skipped": 0,
                "sales_updated": 0,
                "listings_created": 0,
                "line_items_with_listing_link": 0,
                "line_items_unmapped_sku": 0,
            }

        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs._upsert_ebay_order_into_local",
            side_effect=_capture_order,
        ):
            out = sync_jobs.execute_ebay_orders_pull_import(
                repo,
                access_token="tok",
                actor="qa",
                client=_Client(),
            )

        self.assertEqual(out["status"], "success")
        self.assertEqual(len(captured_orders), 1)
        self.assertEqual(captured_orders[0].get("buyer", {}).get("username"), "summary-user")
        self.assertTrue(
            any(
                e.get("action") == "pull_order_hydrate" and e.get("status") == "warning"
                for e in repo.events
            )
        )

    def test_execute_ebay_orders_pull_import_pull_failure_updates_run_and_reraises(self) -> None:
        repo = _FakeRepoWithDB(db=_FakeDB())
        client = SimpleNamespace(pull_recent_orders=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pull fail")))
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            with self.assertRaises(RuntimeError):
                sync_jobs.execute_ebay_orders_pull_import(repo, access_token="tok", actor="qa", client=client)
        self.assertTrue(any(e.get("code") == "EBAY_PULL_FAILED" for e in repo.errors))
        self.assertTrue(any(u[1].get("status") == "failed" for u in repo.updated_runs))

    def test_execute_ebay_orders_pull_import_transient_network_skips_without_failed_record(self) -> None:
        repo = _FakeRepoWithDB(db=_FakeDB())
        transient = requests.ConnectionError("NameResolutionError: failed to resolve api.ebay.com")
        client = SimpleNamespace(pull_recent_orders=lambda *_args, **_kwargs: (_ for _ in ()).throw(transient))
        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True):
            out = sync_jobs.execute_ebay_orders_pull_import(
                repo,
                access_token="tok",
                actor="qa",
                client=client,
            )
        self.assertEqual(out.get("status"), "skipped")
        self.assertEqual(out.get("failed"), 0)
        self.assertEqual(out.get("reason"), "transient_network_unavailable")
        self.assertTrue(any(e.get("code") == "EBAY_NETWORK_UNAVAILABLE" for e in repo.errors))
        self.assertTrue(any(u[1].get("status") == "skipped" and u[1].get("records_failed") == 0 for u in repo.updated_runs))

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

    def test_execute_ebay_orders_pull_import_skips_when_refresh_token_reconnect_required(self) -> None:
        repo = _FakeRepoWithDB(db=_FakeDB(products=[], listings=[]))

        auth_error = requests.HTTPError("expired")
        auth_error.response = SimpleNamespace(status_code=401)
        refresh_error = requests.HTTPError(
            "400 Client Error: Bad Request for url: https://api.ebay.com/identity/v1/oauth2/token"
        )
        refresh_error.response = SimpleNamespace(status_code=400)

        class _Client:
            def pull_recent_orders(self, *_args, **_kwargs):
                raise auth_error

            def refresh_user_token(self, _refresh_token, scopes=None):
                _ = scopes
                raise refresh_error

        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=lambda _repo, key, default: "ref-1" if key == "ebay_user_refresh_token" else default,
        ):
            out = sync_jobs.execute_ebay_orders_pull_import(
                repo,
                access_token="tok-1",
                actor="qa",
                client=_Client(),
            )
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "ebay_reconnect_required")
        self.assertEqual(out["failed"], 0)
        self.assertIn("reconnect eBay", str(out.get("error") or ""))
        self.assertTrue(any(e.get("code") == "EBAY_RECONNECT_REQUIRED" for e in repo.errors))
        self.assertTrue(any(u[1].get("status") == "skipped" and u[1].get("records_failed") == 0 for u in repo.updated_runs))

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
                {
                    "orderId": "ORD-1",
                    "creationDate": "2026-03-01T01:00:00Z",
                    "pricingSummary": {},
                    "buyer": {"username": "updated-buyer"},
                    "fulfillmentStartInstructions": [
                        {
                            "shippingStep": {
                                "shipTo": {
                                    "fullName": "Updated Buyer",
                                    "contactAddress": {
                                        "city": "Golden",
                                        "stateOrProvince": "CO",
                                        "postalCode": "80401",
                                        "countryCode": "US",
                                    },
                                }
                            }
                        }
                    ],
                },
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
        order_updates = repo.updated_orders[0][1]
        self.assertEqual(order_updates.get("buyer_username"), "updated-buyer")
        self.assertEqual(order_updates.get("ship_to_city"), "Golden")
        self.assertIn('"orderId": "ORD-1"', str(order_updates.get("marketplace_payload_json") or ""))
        self.assertIn("listings_marked_sold", out)

    def test_upsert_ebay_order_backfills_existing_sale_listing_and_product_links(self) -> None:
        existing_order = SimpleNamespace(id=42)
        existing_sale = SimpleNamespace(
            id=93,
            listing_id=None,
            product_id=None,
            quantity_sold=1,
            sold_price=Decimal("79.95"),
            fees=Decimal("0"),
            shipping_cost=Decimal("0"),
            shipping_label_cost=Decimal("0"),
        )
        existing_order_item = SimpleNamespace(
            id=94,
            order_id=42,
            listing_id=None,
            product_id=None,
            quantity=1,
            unit_price=Decimal("79.95"),
        )
        repo = _UpsertRepo(
            existing_order=existing_order,
            existing_sales=[existing_sale],
            existing_order_items=[existing_order_item],
        )
        ebay_client = SimpleNamespace(list_shipping_fulfillments=lambda **_kwargs: [])
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [
                    {
                        "product_id": 2,
                        "listing_id": 206,
                        "quantity": 1,
                        "unit_price": Decimal("79.95"),
                    }
                ],
                0,
                1,
                0,
            ),
        ):
            out = sync_jobs._upsert_ebay_order_into_local(
                repo,
                {
                    "orderId": "ORD-LINK",
                    "creationDate": "2026-06-09T07:02:21Z",
                    "pricingSummary": {"total": {"value": "79.95"}},
                },
                actor="qa",
                product_map={},
                listing_map={"137394544357": 206},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=126,
            )

        self.assertEqual(out["orders_updated"], 1)
        self.assertEqual(out["sales_skipped"], 1)
        self.assertEqual(out["sales_updated"], 1)
        self.assertEqual(out["order_item_links_backfilled"], 1)
        self.assertEqual(len(repo.updated_sales), 1)
        sale_id, updates, actor = repo.updated_sales[0]
        self.assertEqual(sale_id, 93)
        self.assertEqual(actor, "qa")
        self.assertEqual(updates.get("listing_id"), 206)
        self.assertEqual(updates.get("product_id"), 2)
        self.assertEqual(existing_order_item.listing_id, 206)
        self.assertEqual(existing_order_item.product_id, 2)

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

    def test_upsert_ebay_order_notes_use_nested_buyer_fallback(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(list_shipping_fulfillments=lambda **_kwargs: [])
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [{"product_id": 1, "listing_id": 5, "quantity": 1, "unit_price": Decimal("10.00")}],
                0,
                1,
                0,
            ),
        ):
            out = sync_jobs._upsert_ebay_order_into_local(
                repo,
                {
                    "orderId": "ORD-3",
                    "buyer": {"username": "nestedbuyer"},
                    "creationDate": "2026-03-01T01:00:00Z",
                    "pricingSummary": {"totalTax": {"value": "1.11"}},
                },
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=125,
            )
        self.assertEqual(out["orders_created"], 1)
        self.assertEqual(len(repo.created_orders), 1)
        created_note = str(repo.created_orders[0].get("notes") or "")
        self.assertIn("buyer=nestedbuyer", created_note)
        self.assertIn("tax=1.11", created_note)

    def test_upsert_ebay_order_persists_buyer_ship_to_and_raw_payload(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(list_shipping_fulfillments=lambda **_kwargs: [])
        ebay_order = {
            "orderId": "23-14477-17302",
            "creationDate": "2026-04-13T05:54:42.000Z",
            "buyer": {
                "username": "mart2303",
                "buyerRegistrationAddress": {
                    "fullName": "Rumondang hasibuan",
                    "email": "45d0751ab0b48a6a9420@members.ebay.com",
                    "contactAddress": {
                        "city": "Jakarta pusat",
                        "stateOrProvince": "JK",
                        "postalCode": "10130",
                        "countryCode": "ID",
                    },
                },
            },
            "fulfillmentStartInstructions": [
                {
                    "shippingStep": {
                        "shipTo": {
                            "fullName": "Nopemwan Ede JBO",
                            "contactAddress": {
                                "city": "Newark",
                                "stateOrProvince": "DE",
                                "postalCode": "19711-8036",
                                "countryCode": "US",
                            },
                            "email": "45d0751ab0b48a6a9420@members.ebay.com",
                        }
                    }
                }
            ],
            "lineItems": [],
            "pricingSummary": {"total": {"value": "142.01"}},
        }
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [{"product_id": 1, "listing_id": 5, "quantity": 1, "unit_price": Decimal("142.01")}],
                0,
                1,
                0,
            ),
        ):
            sync_jobs._upsert_ebay_order_into_local(
                repo,
                ebay_order,
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=203,
            )
        self.assertEqual(len(repo.created_orders), 1)
        created_order = repo.created_orders[0]
        self.assertEqual(created_order.get("buyer_username"), "mart2303")
        self.assertEqual(created_order.get("buyer_name"), "Nopemwan Ede JBO")
        self.assertEqual(created_order.get("buyer_email"), "45d0751ab0b48a6a9420@members.ebay.com")
        self.assertEqual(created_order.get("ship_to_city"), "Newark")
        self.assertEqual(created_order.get("ship_to_state"), "DE")
        self.assertEqual(created_order.get("ship_to_postal_code"), "19711-8036")
        self.assertEqual(created_order.get("ship_to_country"), "US")
        payload = created_order.get("marketplace_payload_json") or ""
        self.assertIn('"orderId": "23-14477-17302"', payload)
        self.assertIn('"username": "mart2303"', payload)

    def test_upsert_ebay_order_uses_line_item_fallback_for_fees_and_shipping(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(list_shipping_fulfillments=lambda **_kwargs: [])
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [
                    {
                        "product_id": 1,
                        "listing_id": 5,
                        "quantity": 1,
                        "unit_price": Decimal("25.00"),
                        "line_fees": Decimal("2.20"),
                        "line_shipping": Decimal("4.10"),
                    }
                ],
                0,
                1,
                0,
            ),
        ):
            sync_jobs._upsert_ebay_order_into_local(
                repo,
                {"orderId": "ORD-LINE-FALLBACK", "creationDate": "2026-03-01T01:00:00Z", "pricingSummary": {}},
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=200,
            )
        self.assertEqual(len(repo.created_orders), 1)
        created_order = repo.created_orders[0]
        self.assertEqual(created_order.get("fees"), Decimal("2.20"))
        self.assertEqual(created_order.get("shipping_cost"), Decimal("4.10"))

    def test_upsert_ebay_order_reads_top_level_marketplace_fee_and_discounted_shipping(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(list_shipping_fulfillments=lambda **_kwargs: [])
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [
                    {
                        "product_id": 1,
                        "listing_id": 5,
                        "quantity": 1,
                        "unit_price": Decimal("130.00"),
                        "line_fees": Decimal("0"),
                        "line_shipping": Decimal("0"),
                    }
                ],
                0,
                1,
                0,
            ),
        ):
            sync_jobs._upsert_ebay_order_into_local(
                repo,
                {
                    "orderId": "ORD-TOPLEVEL-FEE",
                    "creationDate": "2026-04-13T05:54:42.000Z",
                    "pricingSummary": {
                        "deliveryCost": {"value": "25.88", "currency": "USD"},
                        "deliveryDiscount": {"value": "-13.87", "currency": "USD"},
                    },
                    "totalMarketplaceFee": {"value": "22.05", "currency": "USD"},
                },
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=201,
            )
        self.assertEqual(len(repo.created_orders), 1)
        created_order = repo.created_orders[0]
        self.assertEqual(created_order.get("fees"), Decimal("22.05"))
        self.assertEqual(created_order.get("shipping_cost"), Decimal("12.01"))

    def test_upsert_ebay_order_uses_finance_transactions_for_label_spend_fallback(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(
            list_shipping_fulfillments=lambda **_kwargs: [],
            list_finance_transactions_for_order=lambda **_kwargs: [
                {
                    "transactionType": "SHIPPING_LABEL",
                    "orderId": "23-14477-17302",
                    "amount": {"value": "-7.45", "currency": "USD"},
                }
            ],
        )
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [
                    {
                        "product_id": 1,
                        "listing_id": 5,
                        "quantity": 1,
                        "unit_price": Decimal("130.00"),
                        "line_fees": Decimal("0"),
                        "line_shipping": Decimal("0"),
                    }
                ],
                0,
                1,
                0,
            ),
        ):
            sync_jobs._upsert_ebay_order_into_local(
                repo,
                {
                    "orderId": "23-14477-17302",
                    "creationDate": "2026-04-13T05:54:42.000Z",
                    "pricingSummary": {"deliveryCost": {"value": "12.01", "currency": "USD"}},
                },
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=202,
            )
        self.assertEqual(len(repo.created_orders), 1)
        created_order = repo.created_orders[0]
        self.assertEqual(created_order.get("shipping_label_cost"), Decimal("7.45"))
        self.assertEqual(created_order.get("shipping_label_currency"), "USD")

    def test_upsert_ebay_order_uses_finance_transactions_for_fee_fallback(self) -> None:
        repo = _UpsertRepo(existing_order=None, existing_sales=[])
        ebay_client = SimpleNamespace(
            list_shipping_fulfillments=lambda **_kwargs: [],
            list_finance_transactions_for_order=lambda **_kwargs: [
                {
                    "transactionType": "SALE",
                    "orderId": "23-14477-17302",
                    "totalFeeAmount": {"value": "22.05", "currency": "USD"},
                }
            ],
        )
        with patch(
            "app.services.sync_jobs._build_order_items",
            return_value=(
                [
                    {
                        "product_id": 1,
                        "listing_id": 5,
                        "quantity": 1,
                        "unit_price": Decimal("130.00"),
                        "line_fees": Decimal("0"),
                        "line_shipping": Decimal("0"),
                    }
                ],
                0,
                1,
                0,
            ),
        ):
            sync_jobs._upsert_ebay_order_into_local(
                repo,
                {
                    "orderId": "23-14477-17302",
                    "creationDate": "2026-04-13T05:54:42.000Z",
                    "pricingSummary": {"deliveryCost": {"value": "12.01", "currency": "USD"}},
                },
                actor="qa",
                product_map={},
                listing_map={},
                sku_listing_candidates={},
                ebay_client=ebay_client,
                access_token="tok",
                sync_run_id=204,
            )
        self.assertEqual(len(repo.created_orders), 1)
        created_order = repo.created_orders[0]
        self.assertEqual(created_order.get("fees"), Decimal("22.05"))
        payload = str(created_order.get("marketplace_payload_json") or "")
        self.assertIn("_finance_transactions", payload)

    def test_build_ebay_order_financial_diagnostics_returns_expected_keys(self) -> None:
        payload = sync_jobs.build_ebay_order_financial_diagnostics(
            {
                "orderId": "ORD-DIAG-1",
                "pricingSummary": {
                    "priceSubtotal": {"value": "25.00"},
                    "total": {"value": "31.50"},
                    "deliveryCost": {"shippingCost": {"value": "4.00"}},
                    "totalMarketplaceFee": {"value": "2.10"},
                },
                "lineItems": [
                    {"lineItemFee": {"value": "2.10"}, "lineItemShippingCost": {"value": "4.00"}},
                ],
            },
            fulfillments=[{"shippingLabelCost": {"value": "3.45", "currency": "USD"}}],
        )
        self.assertEqual(payload.get("order_id"), "ORD-DIAG-1")
        self.assertEqual(payload.get("marketplace_fee_extracted"), 2.1)
        self.assertEqual(payload.get("shipping_charged_extracted"), 4.0)
        self.assertEqual(payload.get("shipping_label_spend_extracted"), 3.45)

    def test_build_ebay_order_financial_diagnostics_uses_finance_transactions_fallback(self) -> None:
        payload = sync_jobs.build_ebay_order_financial_diagnostics(
            {
                "orderId": "23-14477-17302",
                "pricingSummary": {"deliveryCost": {"value": "12.01", "currency": "USD"}},
            },
            fulfillments=[],
            finance_transactions=[
                {
                    "transactionType": "SHIPPING_LABEL",
                    "orderId": "23-14477-17302",
                    "amount": {"value": "-6.55", "currency": "USD"},
                },
                {
                    "transactionType": "SALE",
                    "orderId": "23-14477-17302",
                    "totalFeeAmount": {"value": "22.05", "currency": "USD"},
                },
            ],
        )
        self.assertEqual(payload.get("shipping_label_spend_extracted"), 6.55)
        self.assertEqual(payload.get("shipping_label_spend_source"), "finance_transactions")
        self.assertEqual(payload.get("marketplace_fee_extracted"), 22.05)
        self.assertEqual(payload.get("marketplace_fee_source"), "finance_transactions")

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

    def test_execute_ebay_connection_health_check_transient_network_is_partial_warning(self) -> None:
        repo = _FakeRepo()

        class _Client:
            SCOPES = []

            def is_configured(self):
                return True

            def decode_access_token_claims(self, _token):
                return {"scope": "https://api.ebay.com/oauth/api_scope/sell.account"}

            def get_account_privileges(self, _token):
                raise requests.ConnectionError("NameResolutionError: failed to resolve api.ebay.com")

        with patch("app.services.sync_jobs.is_sync_job_enabled", return_value=True), patch(
            "app.services.sync_jobs.get_runtime_str",
            side_effect=lambda _repo, key, default="": "tok" if key == "ebay_user_access_token" else "",
        ):
            out = sync_jobs.execute_ebay_connection_health_check(
                repo,
                actor="qa",
                client=_Client(),
            )
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["failed"], 0)
        self.assertEqual(out["warnings"], 1)
        self.assertTrue(any(u[1].get("status") == "partial" and u[1].get("records_failed") == 0 for u in repo.updated_runs))


if __name__ == "__main__":
    unittest.main()
