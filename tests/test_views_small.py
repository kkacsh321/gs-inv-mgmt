import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib.util
import sys
import types


def _load_view_module(name: str):
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _bootstrap_views_package() -> None:
    if "boto3" not in sys.modules:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore.config" not in sys.modules:
        if "botocore" not in sys.modules:
            sys.modules["botocore"] = types.ModuleType("botocore")
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


_bootstrap_views_package()
dashboard = _load_view_module("dashboard")
inventory_movements = _load_view_module("inventory_movements")
media = _load_view_module("media")
operations_home = _load_view_module("operations_home")
orders = _load_view_module("orders")
returns = _load_view_module("returns")
sales = _load_view_module("sales")
shipping = _load_view_module("shipping")
sources = _load_view_module("sources")
sync = _load_view_module("sync")


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Column(_Ctx):
    def __init__(self):
        self.metrics = []

    def metric(self, label, value):
        self.metrics.append((label, value))


class _FakeSt:
    def __init__(self, *, submit_values=None, button_values=None):
        self.submit_values = list(submit_values or [])
        self.button_values = list(button_values or [])
        self.session_state = {}
        self.infos = []
        self.warnings = []
        self.errors = []
        self.successes = []
        self.dataframes = []
        self.json_payloads = []

    def subheader(self, *_a, **_k):
        return None

    def caption(self, *_a, **_k):
        return None

    def metric(self, *_a, **_k):
        return None

    def markdown(self, *_a, **_k):
        return None

    def info(self, msg):
        self.infos.append(str(msg))

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.errors.append(str(msg))

    def success(self, msg):
        self.successes.append(str(msg))

    def columns(self, n):
        if isinstance(n, (list, tuple)):
            count = len(n)
        else:
            count = int(n)
        return [_Column() for _ in range(count)]

    def tabs(self, labels):
        return [_Ctx() for _ in labels]

    def expander(self, *_a, **_k):
        return _Ctx()

    def form(self, *_a, **_k):
        return _Ctx()

    def form_submit_button(self, *_a, **_k):
        if self.submit_values:
            return bool(self.submit_values.pop(0))
        return False

    def button(self, *_a, **_k):
        if self.button_values:
            return bool(self.button_values.pop(0))
        return False

    def text_input(self, _label, value="", **_k):
        return value

    def text_area(self, _label, value="", **_k):
        return value

    def selectbox(self, _label, options, index=0, **_k):
        opts = list(options)
        if not opts:
            return None
        return opts[index if 0 <= int(index) < len(opts) else 0]

    def multiselect(self, _label, options, default=None, **_k):
        if default is not None:
            return list(default)
        return list(options)

    def number_input(self, _label, value=0, **_k):
        return value

    def date_input(self, _label, value=None, **_k):
        return value

    def checkbox(self, _label, value=False, **_k):
        return bool(value)

    def dataframe(self, data, **_k):
        self.dataframes.append(data)

    def download_button(self, *_a, **_k):
        return None

    def json(self, payload, **_k):
        self.json_payloads.append(payload)

    def code(self, *_a, **_k):
        return None

    def divider(self):
        return None

    def rerun(self):
        raise RuntimeError("streamlit rerun called")

    def stop(self):
        raise RuntimeError("streamlit stop called")


class SmallViewsTests(unittest.TestCase):
    def test_render_dashboard(self) -> None:
        fake_st = _FakeSt()
        repo = SimpleNamespace(
            dashboard_metrics=lambda: {
                "product_count": 10,
                "listing_count": 3,
                "sale_count": 5,
                "inventory_cost": 100.0,
                "gross_sales": 250.0,
                "net_sales": 220.0,
            }
        )
        with patch.object(dashboard, "st", fake_st), patch.object(dashboard, "render_help_panel"), patch.object(
            dashboard, "as_money", side_effect=lambda v: f"${v:,.2f}"
        ):
            dashboard.render_dashboard(repo)

    def test_render_inventory_movements(self) -> None:
        fake_st = _FakeSt()
        product = SimpleNamespace(sku="SKU-1", title="Silver Bar")
        row = SimpleNamespace(
            id=1,
            product_id=10,
            product=product,
            movement_type="sale",
            quantity_delta=-1,
            quantity_before=5,
            quantity_after=4,
            unit_cost=20.0,
            reference_type="sale",
            reference_id="100",
            notes="sold one",
            occurred_at=datetime(2026, 4, 1, 10, 0, 0),
            created_at=datetime(2026, 4, 1, 10, 1, 0),
        )
        repo = SimpleNamespace(list_inventory_movements=lambda limit=10000: [row])
        with patch.object(inventory_movements, "st", fake_st), patch.object(
            inventory_movements, "render_help_panel"
        ), patch.object(
            inventory_movements, "dataframe_date_bounds", return_value=(row.occurred_at.date(), row.occurred_at.date())
        ), patch.object(
            inventory_movements, "dataframe_to_xlsx_bytes", return_value=b"xlsx"
        ):
            inventory_movements.render_inventory_movements(repo)
        self.assertEqual(len(fake_st.dataframes), 1)
        self.assertEqual(len(fake_st.json_payloads), 1)

    def test_render_media_enabled_and_disabled(self) -> None:
        # Disabled path
        st_disabled = _FakeSt()
        disabled_repo = SimpleNamespace()
        disabled_storage = SimpleNamespace(enabled=False)
        with patch.object(media, "st", st_disabled), patch.object(media, "current_user", return_value=SimpleNamespace(username="u")), patch.object(
            media, "render_help_panel"
        ):
            media.render_media(disabled_repo, disabled_storage)
        self.assertTrue(st_disabled.warnings)

        # Enabled path
        st_enabled = _FakeSt(button_values=[False])
        asset = SimpleNamespace(
            id=1,
            media_type="image",
            original_filename="a.jpg",
            content_type="image/jpeg",
            size_bytes=123,
            product_id=10,
            listing_id=20,
            s3_bucket="b",
            s3_key="k",
            s3_url="https://x/y.jpg",
            uploaded_by="u",
        )
        repo = SimpleNamespace(
            list_products=lambda: [SimpleNamespace(id=10, sku="SKU-1", title="Item")],
            list_listings=lambda: [SimpleNamespace(id=20, marketplace="ebay", listing_title="L1")],
            list_media_assets=lambda: [asset],
        )
        storage = SimpleNamespace(enabled=True)
        with patch.object(media, "st", st_enabled), patch.object(media, "current_user", return_value=SimpleNamespace(username="u")), patch.object(
            media, "render_help_panel"
        ), patch.object(media, "build_product_options", return_value={"None": None, "SKU-1": 10}), patch.object(
            media, "build_listing_options", return_value={"None": None, "L1": 20}
        ), patch.object(
            media, "render_media_capture_inputs", return_value=[]
        ), patch.object(
            media, "render_media_gallery"
        ) as gallery, patch.object(
            media, "render_media_file_actions"
        ) as file_actions:
            media.render_media(repo, storage)
        self.assertEqual(len(st_enabled.dataframes), 1)
        gallery.assert_called_once()
        file_actions.assert_called_once()

    def test_render_sources_create_and_update(self) -> None:
        fake_st = _FakeSt(submit_values=[True, True])
        def _text_input(label, value="", **_kwargs):
            if label == "Source Name":
                return "APMEX-New"
            return value
        fake_st.text_input = _text_input
        selected = SimpleNamespace(
            id=1,
            name="APMEX",
            source_type="dealer",
            contact_name="",
            contact_email="",
            contact_phone="",
            source_url="",
            ebay_store_url="",
            account_id="",
            payment_method="",
            is_active=True,
            notes="",
        )
        class Repo:
            def __init__(self):
                self.db = SimpleNamespace(rollback=lambda: None)
                self.created = 0
                self.updated = 0

            def create_inventory_source(self, **_kwargs):
                self.created += 1

            def list_inventory_sources(self, active_only=False):
                return [selected]

            def update_inventory_source(self, *_args, **_kwargs):
                self.updated += 1

        repo = Repo()
        with patch.object(sources, "st", fake_st), patch.object(sources, "render_help_panel"):
            sources.render_sources(repo)
        self.assertEqual(repo.created, 1)
        self.assertEqual(repo.updated, 1)
        self.assertTrue(fake_st.successes)

    def test_render_returns_create_and_update(self) -> None:
        fake_st = _FakeSt(submit_values=[True, True])
        product = SimpleNamespace(sku="SKU-1", title="Item")
        ret = SimpleNamespace(
            id=1,
            marketplace="ebay",
            external_return_id="R-1",
            sale_id=1,
            order_id=1,
            product_id=1,
            product=product,
            return_status="requested",
            disposition="pending",
            reason="",
            quantity=1,
            refund_amount=0.0,
            refund_fees=0.0,
            refund_shipping=0.0,
            restocked=False,
            returned_at=datetime(2026, 4, 1, 10, 0, 0),
            processed_at=None,
            notes="",
        )

        class Repo:
            def __init__(self):
                self.created = 0
                self.updated = 0

            def list_sales(self):
                return [SimpleNamespace(id=1, marketplace="ebay", external_order_id="O-1")]

            def list_orders(self):
                return [SimpleNamespace(id=1, marketplace="ebay", external_order_id="O-1")]

            def list_products(self):
                return [SimpleNamespace(id=1, sku="SKU-1", title="Item")]

            def create_return(self, **_kwargs):
                self.created += 1

            def list_returns(self):
                return [ret]

            def update_return(self, *_args, **_kwargs):
                self.updated += 1

        repo = Repo()
        with patch.object(returns, "st", fake_st), patch.object(returns, "current_user", return_value=SimpleNamespace(username="admin", role="admin")), patch.object(
            returns, "ensure_permission", return_value=True
        ), patch.object(
            returns, "render_help_panel"
        ), patch.object(
            returns, "build_product_options", return_value={"None": None, "#1 | SKU-1": 1}
        ), patch.object(
            returns, "to_decimal", side_effect=lambda v: v
        ), patch.object(
            returns, "iso_or_none", side_effect=lambda dt: dt.isoformat() if dt else None
        ), patch.object(
            returns, "utc_today", return_value=datetime(2026, 4, 1).date()
        ):
            returns.render_returns(repo)
        self.assertEqual(repo.created, 1)
        self.assertEqual(repo.updated, 1)
        self.assertGreaterEqual(len(fake_st.dataframes), 1)

    def test_render_orders_with_side_panel(self) -> None:
        fake_st = _FakeSt()
        sold_at = datetime(2026, 4, 1, 9, 0, 0)
        order_row = SimpleNamespace(
            id=1,
            marketplace="ebay",
            external_order_id="EO-1",
            order_status="paid",
            sold_at=sold_at,
            subtotal_amount=100.0,
            fees=5.0,
            shipping_cost=4.0,
            total_amount=109.0,
            items=[],
            notes="",
        )
        item_row = SimpleNamespace(
            id=11,
            order_id=1,
            product_id=2,
            listing_id=3,
            product=SimpleNamespace(sku="SKU-1"),
            quantity=1,
            unit_price=100.0,
            line_total=100.0,
            line_fees=5.0,
            line_shipping=4.0,
        )

        class Repo:
            def list_products(self):
                return [SimpleNamespace(id=2, sku="SKU-1", title="Item")]

            def list_listings(self):
                return [SimpleNamespace(id=3, marketplace="ebay", listing_title="L1")]

            def list_orders(self):
                return [order_row]

            def list_order_items(self):
                return [item_row]

        repo = Repo()
        with patch.object(orders, "st", fake_st), patch.object(
            orders, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            orders, "render_help_panel"
        ), patch.object(
            orders, "build_product_options", return_value={"SKU-1": 2}
        ), patch.object(
            orders, "build_listing_options", return_value={"None": None, "L1": 3}
        ), patch.object(
            orders, "render_table_toolbar"
        ), patch.object(
            orders, "render_standard_row_actions"
        ), patch.object(
            orders, "render_saved_filter_bar",
            side_effect=lambda **kwargs: kwargs["current_filters"],
        ), patch.object(
            orders, "render_workspace_feedback"
        ), patch.object(
            orders, "to_decimal", side_effect=lambda v: v
        ), patch.object(
            orders, "utc_today", return_value=sold_at.date()
        ):
            orders.render_orders(repo)
        self.assertGreaterEqual(len(fake_st.dataframes), 2)

    def test_render_sales_with_side_panel(self) -> None:
        fake_st = _FakeSt()
        sold_at = datetime(2026, 4, 1, 10, 0, 0)
        order_row = SimpleNamespace(
            id=1,
            marketplace="ebay",
            external_order_id="EO-1",
            order_status="paid",
            subtotal_amount=100.0,
            fees=5.0,
            shipping_cost=4.0,
            total_amount=109.0,
            items=[],
        )
        sale_row = SimpleNamespace(
            id=10,
            marketplace="ebay",
            order_id=1,
            product_id=2,
            listing_id=3,
            external_order_id="EO-1",
            shipping_provider="usps",
            shipping_service="ground",
            shipping_package_type="box",
            tracking_number="T-1",
            tracking_status="in_transit",
            shipping_exception_code="",
            shipment_exported_at=None,
            sold_price=120.0,
            fees=5.0,
            shipping_cost=4.0,
            quantity_sold=1,
            sold_at=sold_at,
            shipped_at=None,
            delivered_at=None,
        )

        class Repo:
            def list_products(self):
                return [SimpleNamespace(id=2, sku="SKU-1", title="Item")]

            def list_listings(self):
                return [SimpleNamespace(id=3, marketplace="ebay", listing_title="L1")]

            def list_orders(self):
                return [order_row]

            def list_sales(self):
                return [sale_row]

        repo = Repo()
        with patch.object(sales, "st", fake_st), patch.object(
            sales, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            sales, "render_help_panel"
        ), patch.object(
            sales, "build_product_options", return_value={"None": None, "SKU-1": 2}
        ), patch.object(
            sales, "build_listing_options", return_value={"None": None, "L1": 3}
        ), patch.object(
            sales, "render_table_toolbar"
        ), patch.object(
            sales, "render_standard_row_actions"
        ), patch.object(
            sales, "render_saved_filter_bar",
            side_effect=lambda **kwargs: kwargs["current_filters"],
        ), patch.object(
            sales, "render_workspace_feedback"
        ), patch.object(
            sales, "to_decimal", side_effect=lambda v: v
        ), patch.object(
            sales, "utc_today", return_value=sold_at.date()
        ):
            sales.render_sales(repo)
        self.assertGreaterEqual(len(fake_st.dataframes), 1)

    def test_render_sync_minimal(self) -> None:
        fake_st = _FakeSt()

        class Repo:
            def list_sync_runs(self, limit=200, provider=None):
                return []

            def list_sync_errors(self, run_id=None, unresolved_only=False, limit=500):
                return []

            def list_sync_events(self, run_id=None, limit=200):
                return []

            def list_sync_error_queue(self, provider=None, unresolved_only=True, limit=300):
                return []

        repo = Repo()
        with patch.object(sync, "st", fake_st), patch.object(
            sync, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            sync, "render_help_panel"
        ), patch.object(
            sync, "is_sync_job_enabled", return_value=True
        ), patch.object(
            sync, "sync_job_catalog",
            return_value=[{"job_name": "ebay_orders_pull_import", "provider": "ebay", "enabled": True, "retry_policy": {}, "dispatch_meta": {}}],
        ), patch.object(
            sync, "render_workspace_loading_state"
        ), patch.object(
            sync, "render_workspace_empty_state"
        ), patch.object(
            sync, "render_workspace_error_state"
        ), patch.object(
            sync, "render_workspace_task_completion"
        ), patch.object(
            sync, "render_workspace_feedback"
        ):
            sync.render_sync(repo)
        self.assertGreaterEqual(len(fake_st.dataframes), 1)

    def test_render_sync_with_run_detail_and_exception_queue(self) -> None:
        fake_st = _FakeSt()
        now = datetime(2026, 4, 2, 10, 0, 0)
        run_row = SimpleNamespace(
            id=11,
            retry_of_run_id=None,
            retry_count=0,
            provider="ebay",
            job_name="ebay_orders_pull_import",
            direction="pull",
            status="failed",
            started_at=now,
            completed_at=now,
            records_processed=10,
            records_created=1,
            records_updated=2,
            records_failed=1,
            line_items_with_listing_link=0,
            line_items_unmapped_sku=1,
            auto_listings_created=0,
            notes="x",
        )
        err_row = SimpleNamespace(
            id=21,
            severity="error",
            code="MAP_FAIL",
            message="mapping failure",
            occurred_at=now,
            resolved_at=None,
        )
        event_row = SimpleNamespace(
            id=31,
            entity_type="order",
            entity_id="1",
            action="upsert",
            status="success",
            message="ok",
            created_at=now,
        )

        class Repo:
            def list_sync_runs(self, limit=250, provider=None):
                return [run_row]

            def list_sync_errors(self, run_id=None, unresolved_only=False, limit=500):
                return [err_row]

            def list_sync_events(self, run_id=None, limit=500):
                return [event_row]

            def list_sync_error_queue(self, provider=None, unresolved_only=True, limit=300):
                return [(err_row, run_row)]

        repo = Repo()
        with patch.object(sync, "st", fake_st), patch.object(
            sync, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            sync, "render_help_panel"
        ), patch.object(
            sync, "is_sync_job_enabled", return_value=True
        ), patch.object(
            sync, "sync_job_dispatch_meta", return_value={"supports_retry_execute_now": True}
        ), patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed", "partial", "success"],
                "retryable_statuses": ["failed", "partial"],
                "max_retries": 3,
                "retry_backoff_seconds": 0,
                "runtime_keys": {"max_retries": "sync_job_ebay_orders_pull_import_max_retries"},
            },
        ), patch.object(
            sync, "sync_job_catalog",
            return_value=[{"job_name": "ebay_orders_pull_import", "provider": "ebay", "enabled": True, "retry_policy": {}, "dispatch_meta": {"supports_execute_now": True}}],
        ), patch.object(
            sync, "render_workspace_loading_state"
        ), patch.object(
            sync, "render_workspace_empty_state"
        ), patch.object(
            sync, "render_workspace_error_state"
        ), patch.object(
            sync, "render_workspace_task_completion"
        ), patch.object(
            sync, "render_workspace_feedback"
        ):
            sync.render_sync(repo)
        self.assertGreaterEqual(len(fake_st.dataframes), 5)

    def test_render_sync_execute_now_disabled_job_shows_error(self) -> None:
        fake_st = _FakeSt(button_values=[True])

        class Repo:
            def list_sync_runs(self, limit=250, provider=None):
                return []

            def list_sync_errors(self, run_id=None, unresolved_only=False, limit=500):
                return []

            def list_sync_events(self, run_id=None, limit=500):
                return []

            def list_sync_error_queue(self, provider=None, unresolved_only=True, limit=300):
                return []

        repo = Repo()
        with patch.object(sync, "st", fake_st), patch.object(
            sync, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            sync, "render_help_panel"
        ), patch.object(
            sync, "ensure_permission", return_value=True
        ), patch.object(
            sync, "sync_job_catalog",
            return_value=[
                {
                    "job_name": "shopify_orders_pull",
                    "provider": "shopify",
                    "enabled": False,
                    "retry_policy": {},
                    "dispatch_meta": {"supports_execute_now": True},
                }
            ],
        ), patch.object(
            sync, "is_sync_job_enabled", return_value=True
        ), patch.object(
            sync, "render_workspace_loading_state"
        ), patch.object(
            sync, "render_workspace_empty_state"
        ), patch.object(
            sync, "render_workspace_error_state"
        ), patch.object(
            sync, "render_workspace_task_completion"
        ), patch.object(
            sync, "render_workspace_feedback"
        ):
            sync.render_sync(repo)
        self.assertTrue(any("disabled by configuration" in m.lower() for m in fake_st.errors))

    def test_render_sync_finalize_disabled_queued_without_confirm(self) -> None:
        fake_st = _FakeSt(submit_values=[True], button_values=[False, False])
        queued_run = SimpleNamespace(
            id=42,
            retry_of_run_id=None,
            retry_count=0,
            provider="shopify",
            job_name="shopify_orders_pull",
            direction="pull",
            status="queued",
            started_at=datetime(2026, 4, 2, 10, 0, 0),
            completed_at=None,
            records_processed=0,
            records_created=0,
            records_updated=0,
            records_failed=0,
            line_items_with_listing_link=0,
            line_items_unmapped_sku=0,
            auto_listings_created=0,
            notes="",
        )

        class Repo:
            def __init__(self):
                self.updated = 0

            def list_sync_runs(self, limit=250, provider=None):
                return [queued_run]

            def list_sync_errors(self, run_id=None, unresolved_only=False, limit=500):
                return []

            def list_sync_events(self, run_id=None, limit=500):
                return []

            def list_sync_error_queue(self, provider=None, unresolved_only=True, limit=300):
                return []

            def update_sync_run(self, run_id, payload, actor):
                self.updated += 1

        repo = Repo()

        def _enabled(job_name, repo=None):
            if job_name in {"ebay_orders_pull_import", "ebay_shipping_tracking_push"}:
                return True
            return False

        with patch.object(sync, "st", fake_st), patch.object(
            sync, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            sync, "render_help_panel"
        ), patch.object(
            sync, "ensure_permission", return_value=True
        ), patch.object(
            sync, "sync_job_catalog",
            return_value=[
                {
                    "job_name": "shopify_orders_pull",
                    "provider": "shopify",
                    "enabled": False,
                    "retry_policy": {},
                    "dispatch_meta": {"supports_execute_now": True},
                }
            ],
        ), patch.object(
            sync, "is_sync_job_enabled", side_effect=_enabled
        ), patch.object(
            sync, "render_workspace_loading_state"
        ), patch.object(
            sync, "render_workspace_empty_state"
        ), patch.object(
            sync, "render_workspace_task_completion"
        ), patch.object(
            sync, "render_workspace_feedback"
        ), patch.object(
            sync, "render_workspace_error_state"
        ) as render_error_state:
            sync.render_sync(repo)

        self.assertEqual(repo.updated, 0)
        render_error_state.assert_called()

    def test_render_shipping_minimal(self) -> None:
        fake_st = _FakeSt()

        class Repo:
            def list_sales(self):
                return []

            def list_shipping_presets(self, active_only=False):
                return []

            def list_sync_runs(self, provider=None, limit=300):
                return []

        repo = Repo()
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            shipping, "render_help_panel"
        ), patch.object(
            shipping, "render_workspace_empty_state"
        ), patch.object(
            shipping, "render_workspace_error_state"
        ), patch.object(
            shipping, "render_workspace_task_completion"
        ), patch.object(
            shipping, "render_workspace_feedback"
        ), patch.object(
            shipping, "render_ebay_push_history"
        ), patch.object(
            shipping, "ensure_permission", return_value=True
        ), patch.object(
            shipping, "is_sync_job_enabled", return_value=True
        ):
            shipping.render_shipping(repo)

    def test_render_shipping_read_only_permission(self) -> None:
        fake_st = _FakeSt()
        sold_at = datetime(2026, 4, 1, 9, 0, 0)
        sale_row = SimpleNamespace(
            id=1,
            marketplace="ebay",
            external_order_id="EO-1",
            product=SimpleNamespace(sku="SKU-1", title="Item"),
            tracking_status="needs_label",
            tracking_number="",
            shipping_provider="",
            shipping_service="",
            shipping_package_type="",
            shipping_label_id="",
            shipping_label_cost=None,
            shipping_label_currency="USD",
            shipping_label_purchased_at=None,
            shipping_label_url="",
            sold_at=sold_at,
            shipped_at=None,
            delivered_at=None,
        )

        class Repo:
            def list_sales(self):
                return [sale_row]

        repo = Repo()
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "current_user", return_value=SimpleNamespace(username="employee", role="viewer")
        ), patch.object(
            shipping, "render_help_panel"
        ), patch.object(
            shipping, "ensure_permission", return_value=False
        ):
            shipping.render_shipping(repo)
        self.assertGreaterEqual(len(fake_st.dataframes), 1)

    def test_shipping_queue_and_export_helpers(self) -> None:
        fake_st = _FakeSt(submit_values=[True], button_values=[False])
        sold_at = datetime(2026, 4, 1, 9, 0, 0)
        sale_row = SimpleNamespace(
            id=7,
            marketplace="ebay",
            external_order_id="EO-7",
            product=SimpleNamespace(
                sku="SKU-7",
                title="Seven",
                package_weight_oz=1.0,
                package_length_in=2.0,
                package_width_in=3.0,
                package_height_in=4.0,
            ),
            tracking_status="",
            tracking_number="",
            shipping_provider="",
            shipping_service="",
            shipping_package_type="",
            shipping_label_id="LBL-7",
            shipping_label_cost=4.0,
            shipping_label_currency="USD",
            shipping_label_purchased_at=None,
            shipping_label_url="https://example.test/label/7",
            shipping_exception_code="",
            shipping_exception_action="",
            shipping_exception_notes="",
            shipping_exception_resolved_at=None,
            shipment_exported_at=None,
            quantity_sold=1,
            sold_at=sold_at,
            shipped_at=None,
            delivered_at=None,
        )
        updates = []

        class Repo:
            def update_sale(self, sale_id, data, actor=""):
                updates.append((sale_id, data, actor))

            def mark_shipments_exported(self, sale_ids, actor=""):
                return len(sale_ids)

        repo = Repo()
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "render_workspace_empty_state"
        ), patch.object(
            shipping, "dataframe_to_xlsx_bytes", return_value=b"xlsx"
        ), patch.object(
            shipping, "utc_today", return_value=sold_at.date()
        ):
            shipping._render_queue(
                repo,
                "needs_label",
                "needs label",
                "label_created",
                "tester",
                [sale_row],
                [],
            )
            shipping._render_shipment_export(repo, "tester", [sale_row])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], 7)
        self.assertGreaterEqual(len(fake_st.dataframes), 2)

    def test_render_operations_home_minimal(self) -> None:
        fake_st = _FakeSt()
        now = datetime(2026, 4, 2, 12, 0, 0)
        product_row = SimpleNamespace(
            id=10,
            sku="SKU-10",
            title="Silver Round",
            category="bullion",
            current_quantity=2,
            acquired_at=now,
        )
        listing_row = SimpleNamespace(
            id=20,
            product_id=10,
            marketplace="ebay",
            listing_title="Silver Round 1oz",
            listing_status="draft",
            external_listing_id="",
            marketplace_url="",
            quantity_listed=1,
            listing_price=35.0,
            listed_at=now,
            marketplace_details="{}",
            review_status="pending",
        )
        sale_row = SimpleNamespace(
            id=30,
            product_id=10,
            order_id=40,
            listing_id=20,
            marketplace="ebay",
            external_order_id="EO-30",
            sold_at=now,
            tracking_status="needs_label",
            tracking_number="",
            shipping_provider="",
        )
        order_row = SimpleNamespace(
            id=40,
            marketplace="ebay",
            external_order_id="EO-30",
            sold_at=now,
        )
        sync_row = SimpleNamespace(
            id=50,
            provider="ebay",
            job_name="ebay_orders_pull_import",
            status="failed",
            retry_count=0,
            line_items_with_listing_link=0,
            line_items_unmapped_sku=1,
            auto_listings_created=0,
            started_at=now,
            completed_at=now,
        )

        class Repo:
            def list_products(self):
                return [product_row]

            def list_listings(self):
                return [listing_row]

            def list_sales(self):
                return [sale_row]

            def list_orders(self):
                return [order_row]

            def list_sync_runs(self, limit=200):
                return [sync_row]

            def dashboard_metrics(self):
                return {
                    "inventory_cost": 100.0,
                    "gross_sales": 200.0,
                    "net_sales": 180.0,
                }

            def list_audit_logs(self, limit=2000):
                return []

            def list_saved_filter_profiles(
                self, environment, scope, username, include_shared=True, active_only=True
            ):
                return []

        repo = Repo()
        with patch.object(operations_home, "st", fake_st), patch.object(
            operations_home, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            operations_home, "render_help_panel"
        ), patch.object(
            operations_home, "render_saved_filter_bar",
            side_effect=lambda **kwargs: kwargs["current_filters"],
        ), patch.object(
            operations_home, "render_standard_row_actions"
        ), patch.object(
            operations_home, "render_status_semantic_legend"
        ), patch.object(
            operations_home, "render_workspace_task_completion"
        ), patch.object(
            operations_home, "render_workspace_feedback"
        ), patch.object(
            operations_home, "render_workspace_empty_state"
        ), patch.object(
            operations_home, "normalize_status_semantic", side_effect=lambda value: str(value or "")
        ), patch.object(
            operations_home, "utcnow_naive", return_value=now
        ), patch.object(
            operations_home, "as_money", side_effect=lambda amount: f"${float(amount):,.2f}"
        ):
            operations_home.render_operations_home(repo)
        self.assertGreaterEqual(len(fake_st.dataframes), 3)

    def test_render_operations_home_governance_queue_empty_state(self) -> None:
        fake_st = _FakeSt()
        fake_st.session_state["ops_home_queue_view"] = "Governance Cadence Follow-ups"
        now = datetime(2026, 4, 2, 12, 0, 0)

        class Repo:
            def list_products(self):
                return []

            def list_listings(self):
                return []

            def list_sales(self):
                return []

            def list_orders(self):
                return []

            def list_sync_runs(self, limit=200):
                return []

            def dashboard_metrics(self):
                return {"inventory_cost": 0.0, "gross_sales": 0.0, "net_sales": 0.0}

            def list_audit_logs(self, limit=2000):
                return []

        repo = Repo()
        with patch.object(operations_home, "st", fake_st), patch.object(
            operations_home, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            operations_home, "render_help_panel"
        ), patch.object(
            operations_home, "render_saved_filter_bar",
            side_effect=lambda **kwargs: kwargs["current_filters"],
        ), patch.object(
            operations_home, "render_standard_row_actions"
        ), patch.object(
            operations_home, "render_status_semantic_legend"
        ), patch.object(
            operations_home, "render_workspace_task_completion"
        ), patch.object(
            operations_home, "render_workspace_feedback"
        ), patch.object(
            operations_home, "render_workspace_empty_state"
        ) as empty_state, patch.object(
            operations_home, "normalize_status_semantic", side_effect=lambda value: str(value or "")
        ), patch.object(
            operations_home, "utcnow_naive", return_value=now
        ), patch.object(
            operations_home, "as_money", side_effect=lambda amount: f"${float(amount):,.2f}"
        ):
            operations_home.render_operations_home(repo)
        self.assertTrue(any("Governance Cadence Follow-ups" in str(call.kwargs.get("title", "")) for call in empty_state.mock_calls))

    def test_render_operations_home_governance_queue_resolve_action(self) -> None:
        class _KeyedFakeSt(_FakeSt):
            def __init__(self):
                super().__init__()
                self._buttons = {"ops_home_cadence_followup_resolve_btn": True}
                self.rerun = lambda: (_ for _ in ()).throw(RuntimeError("streamlit rerun called"))

            def button(self, _label, **kwargs):
                key = kwargs.get("key")
                if key in self._buttons:
                    return bool(self._buttons.pop(key))
                return False

        fake_st = _KeyedFakeSt()
        fake_st.session_state["ops_home_queue_view"] = "Governance Cadence Follow-ups"
        now = datetime(2026, 4, 2, 12, 0, 0)
        followup_payload = {
            "workflow": "governance_snapshot_cadence",
            "task_key": "GOV-1",
            "title": "Governance Snapshot Follow-up",
            "owner": "admin",
            "priority": "high",
            "status": "open",
            "due_date": "2026-04-03",
            "note": "weekly snapshot missing",
        }
        audit_row = SimpleNamespace(
            entity_type="workspace_followup",
            action="create",
            changes_json=operations_home.json.dumps(followup_payload),
            changed_at=now,
            changed_by="admin",
        )

        class Repo:
            def __init__(self):
                self.audit_events = []

            def list_products(self):
                return []

            def list_listings(self):
                return []

            def list_sales(self):
                return []

            def list_orders(self):
                return []

            def list_sync_runs(self, limit=200):
                return []

            def dashboard_metrics(self):
                return {"inventory_cost": 0.0, "gross_sales": 0.0, "net_sales": 0.0}

            def list_audit_logs(self, limit=2000):
                return [audit_row]

            def record_audit_event(self, **kwargs):
                self.audit_events.append(kwargs)

        repo = Repo()
        with patch.object(operations_home, "st", fake_st), patch.object(
            operations_home, "current_user", return_value=SimpleNamespace(username="admin", role="admin")
        ), patch.object(
            operations_home, "render_help_panel"
        ), patch.object(
            operations_home, "render_saved_filter_bar",
            side_effect=lambda **kwargs: kwargs["current_filters"],
        ), patch.object(
            operations_home, "render_standard_row_actions"
        ), patch.object(
            operations_home, "render_status_semantic_legend"
        ), patch.object(
            operations_home, "render_workspace_task_completion"
        ), patch.object(
            operations_home, "render_workspace_feedback"
        ), patch.object(
            operations_home, "render_workspace_empty_state"
        ), patch.object(
            operations_home, "normalize_status_semantic", side_effect=lambda value: str(value or "")
        ), patch.object(
            operations_home, "utcnow_naive", return_value=now
        ), patch.object(
            operations_home, "utc_today", return_value=now.date()
        ), patch.object(
            operations_home, "as_money", side_effect=lambda amount: f"${float(amount):,.2f}"
        ):
            operations_home.render_operations_home(repo)
        self.assertTrue(repo.audit_events)
        self.assertTrue(any("Resolved cadence follow-up" in msg for msg in fake_st.successes))

    def test_render_operations_home_blocker_queue_open_workflow_action(self) -> None:
        class _KeyedFakeSt(_FakeSt):
            def __init__(self):
                super().__init__()
                self._buttons = {"ops_home_open_listings_blocker_workflow_btn": True}

            def button(self, _label, **kwargs):
                key = kwargs.get("key")
                if key in self._buttons:
                    return bool(self._buttons.pop(key))
                return False

        fake_st = _KeyedFakeSt()
        fake_st.session_state["ops_home_queue_view"] = "Listings Blocker Follow-ups"
        now = datetime(2026, 4, 2, 12, 0, 0)
        blocker_payload = {
            "workflow": "listings_readiness:blocker",
            "task_key": "BLK-1",
            "title": "[Listings/Readiness] Missing Media",
            "owner": "ops",
            "priority": "high",
            "status": "open",
            "due_date": "2026-04-03",
            "blocker_reason": "media_required",
        }
        audit_row = SimpleNamespace(
            entity_type="workspace_followup",
            action="create",
            changes_json=operations_home.json.dumps(blocker_payload),
            changed_at=now,
            changed_by="ops",
        )

        class Repo:
            def list_products(self):
                return []

            def list_listings(self):
                return []

            def list_sales(self):
                return []

            def list_orders(self):
                return []

            def list_sync_runs(self, limit=200):
                return []

            def dashboard_metrics(self):
                return {"inventory_cost": 0.0, "gross_sales": 0.0, "net_sales": 0.0}

            def list_audit_logs(self, limit=2000):
                return [audit_row]

        repo = Repo()
        with patch.object(operations_home, "st", fake_st), patch.object(
            operations_home, "current_user", return_value=SimpleNamespace(username="ops", role="ops")
        ), patch.object(
            operations_home, "render_help_panel"
        ), patch.object(
            operations_home, "render_saved_filter_bar",
            side_effect=lambda **kwargs: kwargs["current_filters"],
        ), patch.object(
            operations_home, "render_standard_row_actions"
        ), patch.object(
            operations_home, "render_status_semantic_legend"
        ), patch.object(
            operations_home, "render_workspace_task_completion"
        ), patch.object(
            operations_home, "render_workspace_feedback"
        ), patch.object(
            operations_home, "render_workspace_empty_state"
        ), patch.object(
            operations_home, "normalize_status_semantic", side_effect=lambda value: str(value or "")
        ), patch.object(
            operations_home, "utcnow_naive", return_value=now
        ), patch.object(
            operations_home, "utc_today", return_value=now.date()
        ), patch.object(
            operations_home, "as_money", side_effect=lambda amount: f"${float(amount):,.2f}"
        ):
            operations_home.render_operations_home(repo)
        self.assertEqual(fake_st.session_state.get("listings_readiness_filter"), "blocked")
        self.assertEqual(fake_st.session_state.get("workspace_handoff_target"), "listings")


if __name__ == "__main__":
    unittest.main()
