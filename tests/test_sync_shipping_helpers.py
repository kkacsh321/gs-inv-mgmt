import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
        full = f"app.components.views.{name}"
        if full in sys.modules:
            continue
        path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        sys.modules[full] = mod


def _load_view(name: str):
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


sync = _load_view("sync")
shipping = _load_view("shipping")


class SyncShippingHelpersTests(unittest.TestCase):
    def test_retry_allowed_for_run(self):
        run = SimpleNamespace(
            id=1,
            job_name="ebay_orders_pull_import",
            status="failed",
            retry_count=0,
            completed_at=datetime(2026, 4, 2, 12, 0, 0),
        )
        repo = SimpleNamespace()
        with patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed", "partial", "success"],
                "retryable_statuses": ["failed", "partial"],
                "max_retries": 3,
                "retry_backoff_seconds": 0,
                "runtime_keys": {"max_retries": "sync_job_ebay_orders_pull_import_max_retries"},
            },
        ), patch.object(sync, "is_sync_job_enabled", return_value=True):
            allowed, reason = sync._retry_allowed_for_run(run, repo)
        self.assertTrue(allowed)
        self.assertIn("available", reason.lower())

        run_too_many = SimpleNamespace(
            id=2,
            job_name="ebay_orders_pull_import",
            status="failed",
            retry_count=3,
            completed_at=datetime(2026, 4, 2, 12, 0, 0),
        )
        with patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed"],
                "retryable_statuses": ["failed"],
                "max_retries": 3,
                "retry_backoff_seconds": 0,
                "runtime_keys": {"max_retries": "x"},
            },
        ), patch.object(sync, "is_sync_job_enabled", return_value=True):
            allowed, reason = sync._retry_allowed_for_run(run_too_many, repo)
        self.assertFalse(allowed)
        self.assertIn("max retries", reason.lower())

        run_backoff = SimpleNamespace(
            id=3,
            job_name="ebay_orders_pull_import",
            status="failed",
            retry_count=0,
            completed_at=datetime(2026, 4, 2, 12, 0, 0),
        )
        with patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed"],
                "retryable_statuses": ["failed"],
                "max_retries": 3,
                "retry_backoff_seconds": 3600,
                "runtime_keys": {},
            },
        ), patch.object(sync, "is_sync_job_enabled", return_value=True), patch.object(
            sync, "utcnow_naive", return_value=datetime(2026, 4, 2, 12, 10, 0)
        ):
            allowed, reason = sync._retry_allowed_for_run(run_backoff, repo)
        self.assertFalse(allowed)
        self.assertIn("backoff", reason.lower())

    def test_retry_allowed_guardrails(self):
        repo = SimpleNamespace()
        run_running = SimpleNamespace(
            id=4,
            job_name="ebay_orders_pull_import",
            status="running",
            retry_count=0,
            completed_at=None,
        )
        with patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed", "partial", "success"],
                "retryable_statuses": ["failed", "partial"],
                "max_retries": 2,
                "retry_backoff_seconds": 0,
                "runtime_keys": {},
            },
        ), patch.object(sync, "is_sync_job_enabled", return_value=True):
            allowed, reason = sync._retry_allowed_for_run(run_running, repo)
        self.assertFalse(allowed)
        self.assertIn("terminal statuses", reason.lower())

        run_success = SimpleNamespace(
            id=5,
            job_name="ebay_orders_pull_import",
            status="success",
            retry_count=0,
            completed_at=datetime(2026, 4, 2, 12, 0, 0),
        )
        with patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed", "partial", "success"],
                "retryable_statuses": ["failed", "partial"],
                "max_retries": 2,
                "retry_backoff_seconds": 0,
                "runtime_keys": {},
            },
        ), patch.object(sync, "is_sync_job_enabled", return_value=True):
            allowed, reason = sync._retry_allowed_for_run(run_success, repo)
        self.assertFalse(allowed)
        self.assertIn("only enabled", reason.lower())

        run_failed = SimpleNamespace(
            id=6,
            job_name="ebay_orders_pull_import",
            status="failed",
            retry_count=0,
            completed_at=datetime(2026, 4, 2, 12, 0, 0),
        )
        with patch.object(
            sync, "sync_job_retry_policy",
            return_value={
                "terminal_statuses": ["failed"],
                "retryable_statuses": ["failed"],
                "max_retries": 2,
                "retry_backoff_seconds": 0,
                "runtime_keys": {},
            },
        ), patch.object(sync, "is_sync_job_enabled", return_value=False):
            allowed, reason = sync._retry_allowed_for_run(run_failed, repo)
        self.assertFalse(allowed)
        self.assertIn("disabled", reason.lower())

    def test_sync_lineage_helpers(self):
        r1 = SimpleNamespace(id=1, retry_of_run_id=None, status="failed", started_at=datetime(2026, 4, 2, 11, 0, 0), completed_at=datetime(2026, 4, 2, 11, 5, 0))
        r2 = SimpleNamespace(id=2, retry_of_run_id=1, status="partial", started_at=datetime(2026, 4, 2, 12, 0, 0), completed_at=datetime(2026, 4, 2, 12, 2, 0))
        r3 = SimpleNamespace(id=3, retry_of_run_id=2, status="success", started_at=datetime(2026, 4, 2, 13, 0, 0), completed_at=datetime(2026, 4, 2, 13, 1, 0))
        idx = {1: r1, 2: r2, 3: r3}
        self.assertEqual(sync._run_root_id(r3, idx), 1)
        self.assertEqual(sync._run_chain_depth(r3, idx), 2)
        self.assertEqual(sync._lineage_terminal_status([r1, r2, r3]), "success")

    def test_sync_lineage_edge_cases(self):
        self.assertEqual(sync._lineage_terminal_status([]), "unknown")

        orphan = SimpleNamespace(id=10, retry_of_run_id=999, status="failed", started_at=None, completed_at=None)
        idx = {}
        self.assertEqual(sync._run_root_id(orphan, idx), 999)
        self.assertEqual(sync._run_chain_depth(orphan, idx), 1)

        cyc = SimpleNamespace(id=11, retry_of_run_id=11, status="failed", started_at=None, completed_at=None)
        idx2 = {11: cyc}
        self.assertEqual(sync._run_root_id(cyc, idx2), 11)
        self.assertEqual(sync._run_chain_depth(cyc, idx2), 1)

    def test_shipping_queue_helpers(self):
        preset = SimpleNamespace(
            name="USPS GA",
            is_default=True,
            shipping_provider="usps",
            shipping_service="ground",
            shipping_package_type="box",
        )
        label = shipping._preset_label(preset)
        self.assertIn("default", label)
        self.assertIn("ground", label)

        sale = SimpleNamespace(tracking_status="", tracking_number="")
        self.assertTrue(shipping._in_queue(sale, "needs_label"))
        sale2 = SimpleNamespace(tracking_status="in_transit", tracking_number="T")
        self.assertTrue(shipping._in_queue(sale2, "in_transit"))
        sale3 = SimpleNamespace(tracking_status="delivered", tracking_number="T")
        self.assertTrue(shipping._in_queue(sale3, "delivered"))
        sale4 = SimpleNamespace(tracking_status="exception", tracking_number="T")
        self.assertTrue(shipping._in_queue(sale4, "exceptions"))
        self.assertFalse(shipping._in_queue(sale4, "unknown"))

    def test_export_rows_for_format(self):
        sale = SimpleNamespace(
            id=1,
            external_order_id="EO-1",
            marketplace="ebay",
            shipping_provider="usps",
            shipping_service="ground",
            shipping_package_type="box",
            tracking_number="TRK-1",
            shipping_label_id="LBL-1",
            shipping_label_cost=4.25,
            shipping_label_currency="USD",
            shipping_label_purchased_at=None,
            shipping_label_url="https://example.test/label/1",
            tracking_status="label_created",
            quantity_sold=1,
            sold_at=datetime(2026, 4, 2, 10, 0, 0),
            shipped_at=None,
            shipment_exported_at=None,
            product=SimpleNamespace(
                sku="SKU-1",
                title="Item One",
                package_weight_oz=1.0,
                package_length_in=2.0,
                package_width_in=3.0,
                package_height_in=4.0,
            ),
        )
        generic_df = shipping._export_rows_for_format([sale], "carrier_generic")
        pirate_df = shipping._export_rows_for_format([sale], "pirateship_upload")
        self.assertEqual(int(generic_df.iloc[0]["sale_id"]), 1)
        self.assertEqual(str(generic_df.iloc[0]["shipping_label_id"]), "LBL-1")
        self.assertEqual(str(pirate_df.iloc[0]["Order Number"]), "EO-1")
        self.assertEqual(str(pirate_df.iloc[0]["Provider"]), "usps")

    def test_render_queue_submit_without_selection(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeSt:
            def __init__(self):
                self.errors = []
                self.successes = []

            def dataframe(self, *_a, **_k):
                return None

            def form(self, *_a, **_k):
                return _Ctx()

            def multiselect(self, *_a, **_k):
                return []

            def selectbox(self, _label, options, index=0, **_k):
                opts = list(options)
                return opts[int(index)] if opts else None

            def columns(self, n):
                return [_Ctx() for _ in range(int(n))]

            def text_input(self, _label, value="", **_k):
                return value

            def checkbox(self, _label, value=False, **_k):
                return bool(value)

            def date_input(self, _label, value=None, **_k):
                return value

            def text_area(self, _label, value="", **_k):
                return value

            def form_submit_button(self, *_a, **_k):
                return True

            def error(self, msg):
                self.errors.append(str(msg))

            def success(self, msg):
                self.successes.append(str(msg))

        fake_st = _FakeSt()
        sale = SimpleNamespace(
            id=1,
            marketplace="ebay",
            external_order_id="EO-1",
            product=SimpleNamespace(sku="SKU-1"),
            tracking_status="",
            tracking_number="",
            shipping_provider="",
            shipping_service="",
            shipping_package_type="",
            shipping_label_id="",
            shipping_label_cost=None,
            shipping_label_currency="",
            shipping_label_purchased_at=None,
            shipping_label_url="",
            shipping_exception_code="",
            shipping_exception_action="",
            shipping_exception_notes="",
            shipping_exception_resolved_at=None,
            shipment_exported_at=None,
            sold_at=datetime(2026, 4, 2, 10, 0, 0),
            shipped_at=None,
            delivered_at=None,
        )
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "render_workspace_error_state"
        ) as err_state:
            shipping._render_queue(
                SimpleNamespace(),
                "needs_label",
                "needs label",
                "label_created",
                "tester",
                [sale],
                [],
            )
        self.assertTrue(err_state.called)

    def test_render_queue_exceptions_resolve_updates(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeSt:
            def __init__(self):
                self.successes = []
                self.errors = []

            def markdown(self, *_a, **_k):
                return None

            def dataframe(self, *_a, **_k):
                return None

            def form(self, *_a, **_k):
                return _Ctx()

            def multiselect(self, _label, options, **_k):
                opts = list(options)
                return [opts[0]] if opts else []

            def selectbox(self, _label, options, index=0, **_k):
                opts = list(options)
                return opts[int(index)] if opts else None

            def columns(self, n):
                return [_Ctx() for _ in range(int(n))]

            def text_input(self, _label, value="", **_k):
                if "Exception Code" in str(_label):
                    return "carrier_delay"
                return value

            def checkbox(self, _label, value=False, **_k):
                if "Mark Exception Resolved" in str(_label):
                    return True
                return bool(value)

            def date_input(self, _label, value=None, **_k):
                return value

            def text_area(self, _label, value="", **_k):
                if "Exception Notes" in str(_label):
                    return "investigated"
                return value

            def form_submit_button(self, *_a, **_k):
                return True

            def error(self, msg):
                self.errors.append(str(msg))

            def success(self, msg):
                self.successes.append(str(msg))

        updates = []

        class _Repo:
            def update_sale(self, sale_id, data, actor=""):
                updates.append((sale_id, data, actor))

        fake_st = _FakeSt()
        sale = SimpleNamespace(
            id=2,
            marketplace="ebay",
            external_order_id="EO-2",
            product=SimpleNamespace(sku="SKU-2"),
            tracking_status="exception",
            tracking_number="TRK-2",
            shipping_provider="usps",
            shipping_service="ground",
            shipping_package_type="box",
            shipping_label_id="LBL-2",
            shipping_label_cost=None,
            shipping_label_currency="USD",
            shipping_label_purchased_at=None,
            shipping_label_url="",
            shipping_exception_code="damaged",
            shipping_exception_action="monitoring",
            shipping_exception_notes="old note",
            shipping_exception_resolved_at=None,
            shipment_exported_at=None,
            sold_at=datetime(2026, 4, 2, 10, 0, 0),
            shipped_at=None,
            delivered_at=None,
        )
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "utcnow_naive", return_value=datetime(2026, 4, 2, 12, 0, 0)
        ):
            shipping._render_queue(
                _Repo(),
                "exceptions",
                "exceptions",
                "exception",
                "tester",
                [sale],
                [],
            )
        self.assertTrue(updates)
        update_payload = updates[0][1]
        self.assertEqual(update_payload.get("shipping_exception_code"), "")
        self.assertEqual(update_payload.get("shipping_exception_action"), "")
        self.assertEqual(update_payload.get("shipping_exception_resolved_by"), "tester")
        self.assertTrue(fake_st.successes)

    def test_render_queue_non_exception_clears_exception_fields_and_error_paths(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeSt:
            def __init__(self):
                self.successes = []
                self.errors = []

            def markdown(self, *_a, **_k):
                return None

            def dataframe(self, *_a, **_k):
                return None

            def form(self, *_a, **_k):
                return _Ctx()

            def multiselect(self, _label, options, **_k):
                return list(options)

            def selectbox(self, _label, options, index=0, **_k):
                opts = list(options)
                return opts[int(index)] if opts else None

            def columns(self, n):
                return [_Ctx() for _ in range(int(n))]

            def text_input(self, _label, value="", **_k):
                if "Set Tracking Number" in str(_label):
                    return "TRK-NEW"
                return value

            def checkbox(self, _label, value=False, **_k):
                return bool(value)

            def date_input(self, _label, value=None, **_k):
                return value

            def text_area(self, _label, value="", **_k):
                return value

            def form_submit_button(self, *_a, **_k):
                return True

            def error(self, msg):
                self.errors.append(str(msg))

            def success(self, msg):
                self.successes.append(str(msg))

        updates = []

        class _Repo:
            def update_sale(self, sale_id, data, actor=""):
                if int(sale_id) == 4:
                    raise ValueError("bad update")
                updates.append((sale_id, data, actor))

        fake_st = _FakeSt()
        sale_ok = SimpleNamespace(
            id=3,
            marketplace="ebay",
            external_order_id="EO-3",
            product=SimpleNamespace(sku="SKU-3"),
            tracking_status="label_created",
            tracking_number="",
            shipping_provider="usps",
            shipping_service="ground",
            shipping_package_type="box",
            shipping_label_id="LBL-3",
            shipping_label_cost=None,
            shipping_label_currency="USD",
            shipping_label_purchased_at=None,
            shipping_label_url="",
            shipping_exception_code="damaged",
            shipping_exception_action="monitoring",
            shipping_exception_notes="existing",
            shipping_exception_resolved_at=None,
            shipment_exported_at=None,
            sold_at=datetime(2026, 4, 2, 10, 0, 0),
            shipped_at=None,
            delivered_at=None,
        )
        sale_err = SimpleNamespace(
            **{
                **sale_ok.__dict__,
                "id": 4,
                "external_order_id": "EO-4",
                "product": SimpleNamespace(sku="SKU-4"),
            }
        )
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "utcnow_naive", return_value=datetime(2026, 4, 2, 12, 0, 0)
        ):
            shipping._render_queue(
                _Repo(),
                "needs_label",
                "needs label",
                "label_created",
                "tester",
                [sale_ok, sale_err],
                [],
            )
        self.assertTrue(updates)
        ok_payload = updates[0][1]
        self.assertEqual(ok_payload.get("tracking_status"), "label_created")
        self.assertEqual(ok_payload.get("shipping_exception_code"), "")
        self.assertEqual(ok_payload.get("shipping_exception_action"), "")
        self.assertEqual(ok_payload.get("shipping_exception_resolved_by"), "tester")
        self.assertTrue(any("Sale #4" in msg for msg in fake_st.errors))

    def test_sync_copilot_render_paths(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeSt:
            def __init__(self):
                self.session_state = {}
                self.successes = []
                self.errors = []
                self._button_calls = 0

            def markdown(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def button(self, *_a, **_k):
                self._button_calls += 1
                return self._button_calls == 1

            def success(self, msg):
                self.successes.append(str(msg))

            def error(self, msg):
                self.errors.append(str(msg))

            def rerun(self):
                return None

            def expander(self, *_a, **_k):
                return _Ctx()

            def code(self, *_a, **_k):
                return None

        fake_st = _FakeSt()
        run = SimpleNamespace(
            id=1,
            status="failed",
            job_name="ebay_orders_pull_import",
            provider="ebay",
            retry_count=0,
            retry_of_run_id=None,
            records_failed=1,
            started_at=datetime(2026, 4, 2, 10, 0, 0),
            completed_at=datetime(2026, 4, 2, 10, 1, 0),
        )
        err = SimpleNamespace(id=9, severity="error", code="MAP_FAIL", message="x", resolved_at=None)
        user = SimpleNamespace(username="admin")
        with patch.object(sync, "st", fake_st), patch.object(
            sync, "ensure_permission", return_value=True
        ), patch.object(
            sync, "execute_comp_summary", return_value=SimpleNamespace(text='{"ok": true}')
        ):
            sync._render_sync_copilot(SimpleNamespace(), user, [run], [(err, run)])
        self.assertTrue(fake_st.successes)
        self.assertIn("sync_copilot_raw", fake_st.session_state)

    def test_render_label_purchase_queue_actions(self):
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeSt:
            def __init__(self):
                self.successes = []
                self.errors = []
                self.warnings = []
                self._button_calls = 0
                self._form_calls = 0

            def markdown(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def warning(self, msg):
                self.warnings.append(str(msg))

            def error(self, msg):
                self.errors.append(str(msg))

            def success(self, msg):
                self.successes.append(str(msg))

            def form(self, *_a, **_k):
                return _Ctx()

            def columns(self, n):
                return [_Ctx() for _ in range(int(n))]

            def multiselect(self, _label, options, **_k):
                opts = list(options)
                # Queue form selects a sale key; run-selected uses first queued job id.
                if opts and isinstance(opts[0], str) and opts[0].startswith("#"):
                    return [opts[0]]
                if opts and isinstance(opts[0], int):
                    return [opts[0]]
                return []

            def selectbox(self, _label, options, index=0, **_k):
                opts = list(options)
                return opts[int(index)] if opts else None

            def text_input(self, _label, value="", **_k):
                return value

            def number_input(self, _label, value=0, **_k):
                return value

            def checkbox(self, _label, value=False, **_k):
                return bool(value)

            def form_submit_button(self, _label, **_k):
                self._form_calls += 1
                return self._form_calls == 1

            def dataframe(self, *_a, **_k):
                return None

            def button(self, _label, **_k):
                self._button_calls += 1
                # Trigger process-due then run-selected branches.
                return self._button_calls in {1, 2}

            def rerun(self):
                return None

        fake_st = _FakeSt()
        sale = SimpleNamespace(
            id=5,
            marketplace="ebay",
            external_order_id="EO-5",
            product=SimpleNamespace(sku="SKU-5"),
            tracking_status="",
            tracking_number="",
        )
        queue_jobs = [
            SimpleNamespace(
                id=101,
                status="queued",
                action="purchase_label",
                payload_json='{"sale_id":5,"shipping_provider":"pirateship","shipping_service":"Ground Advantage","shipping_package_type":"small_box"}',
                retry_count=0,
                max_retries=5,
                next_attempt_at=None,
                last_error="",
                requested_by="ops",
                updated_by="ops",
                created_at=None,
                updated_at=None,
            )
        ]

        class Repo:
            def __init__(self):
                self.created_jobs = []

            def create_integration_queue_job(self, **kwargs):
                self.created_jobs.append(kwargs)

            def list_integration_queue_jobs(self, **_kwargs):
                return queue_jobs

        repo = Repo()
        process_calls = []
        run_calls = []
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "ensure_permission", return_value=True
        ), patch.object(
            shipping, "render_workspace_empty_state"
        ), patch.object(
            shipping, "get_runtime_bool", side_effect=lambda _r, _k, default=True: bool(default)
        ), patch.object(
            shipping, "get_runtime_int", side_effect=lambda _r, _k, default=5: int(default)
        ), patch.object(
            shipping, "process_due_integration_queue_jobs",
            side_effect=lambda *a, **k: process_calls.append((a, k)) or {"processed": 1, "success": 1, "queued": 0, "failed": 0},
        ), patch.object(
            shipping, "process_integration_queue_job",
            side_effect=lambda *a, **k: run_calls.append((a, k)) or (True, {}),
        ):
            shipping._render_label_purchase_queue(repo, "tester", [sale], [], SimpleNamespace(username="admin"))

        self.assertEqual(len(repo.created_jobs), 1)
        self.assertTrue(process_calls)
        self.assertTrue(run_calls)
        self.assertTrue(fake_st.successes)

    def test_render_ebay_tracking_push_execute(self):
        class _FakeSt:
            def __init__(self):
                self.successes = []
                self.errors = []
                self._button_calls = 0

            def markdown(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def dataframe(self, *_a, **_k):
                return None

            def multiselect(self, _label, options, **_k):
                opts = list(options)
                return [opts[0]] if opts else []

            def text_area(self, _label, value="", **_k):
                return "token-123"

            def button(self, _label, **_k):
                self._button_calls += 1
                return self._button_calls == 1

            def success(self, msg):
                self.successes.append(str(msg))

            def error(self, msg):
                self.errors.append(str(msg))

        fake_st = _FakeSt()
        sale = SimpleNamespace(
            id=7,
            marketplace="ebay",
            external_order_id="EO-7",
            tracking_number="TRK-7",
            tracking_status="in_transit",
            shipping_provider="usps",
            shipped_at=None,
        )
        executed = []

        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "is_sync_job_enabled", return_value=True
        ), patch.object(
            shipping, "EbayClient", return_value=SimpleNamespace(is_configured=lambda: True)
        ), patch.object(
            shipping, "ensure_permission", return_value=True
        ), patch.object(
            shipping, "execute_sync_job",
            side_effect=lambda *a, **k: executed.append((a, k)) or {"run_id": 88, "status": "success", "processed": 1, "updated": 1, "failed": 0},
        ), patch.object(
            shipping, "get_runtime_str", return_value="token-123"
        ):
            shipping._render_ebay_tracking_push(
                SimpleNamespace(),
                actor="tester",
                sales=[sale],
                user=SimpleNamespace(username="admin", role="admin"),
            )

        self.assertTrue(executed)
        self.assertTrue(fake_st.successes)

    def test_render_ebay_tracking_push_not_configured_and_no_sales(self):
        class _FakeSt:
            def __init__(self):
                self.warnings = []
                self.infos = []
                self.errors = []

            def markdown(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def dataframe(self, *_a, **_k):
                return None

            def multiselect(self, _label, options, **_k):
                return []

            def text_area(self, _label, value="", **_k):
                return value

            def button(self, _label, **_k):
                return False

            def warning(self, msg):
                self.warnings.append(str(msg))

            def info(self, msg):
                self.infos.append(str(msg))

            def error(self, msg):
                self.errors.append(str(msg))

        # Not configured path
        fake_st = _FakeSt()
        empty_calls = []
        with patch.object(shipping, "st", fake_st), patch.object(
            shipping, "is_sync_job_enabled", return_value=True
        ), patch.object(
            shipping, "EbayClient", return_value=SimpleNamespace(is_configured=lambda: False)
        ), patch.object(
            shipping, "render_workspace_empty_state", side_effect=lambda **k: empty_calls.append(k)
        ):
            shipping._render_ebay_tracking_push(
                SimpleNamespace(),
                actor="tester",
                sales=[],
                user=SimpleNamespace(username="admin", role="admin"),
            )
        self.assertTrue(any("credentials are not configured" in str(c.get("detail", "")).lower() for c in empty_calls))

        # Configured but no eligible sales path
        fake_st2 = _FakeSt()
        empty_calls_2 = []
        bad_sale = SimpleNamespace(
            id=8,
            marketplace="ebay",
            external_order_id="",
            tracking_number="",
            tracking_status="",
            shipping_provider="",
            shipped_at=None,
        )
        with patch.object(shipping, "st", fake_st2), patch.object(
            shipping, "is_sync_job_enabled", return_value=True
        ), patch.object(
            shipping, "EbayClient", return_value=SimpleNamespace(is_configured=lambda: True)
        ), patch.object(
            shipping, "render_workspace_empty_state", side_effect=lambda **k: empty_calls_2.append(k)
        ):
            shipping._render_ebay_tracking_push(
                SimpleNamespace(),
                actor="tester",
                sales=[bad_sale],
                user=SimpleNamespace(username="admin", role="admin"),
            )
        self.assertTrue(any("no ebay sales" in str(c.get("detail", "")).lower() for c in empty_calls_2))

    def test_render_ebay_tracking_push_validation_errors(self):
        class _FakeSt:
            def __init__(self, token_value="token-123", selected=True):
                self.successes = []
                self.errors = []
                self.warnings = []
                self._button_calls = 0
                self._token_value = token_value
                self._selected = selected

            def markdown(self, *_a, **_k):
                return None

            def caption(self, *_a, **_k):
                return None

            def dataframe(self, *_a, **_k):
                return None

            def multiselect(self, _label, options, **_k):
                opts = list(options)
                if self._selected and opts:
                    return [opts[0]]
                return []

            def text_area(self, _label, value="", **_k):
                return self._token_value

            def button(self, _label, **_k):
                self._button_calls += 1
                return self._button_calls == 1

            def success(self, msg):
                self.successes.append(str(msg))

            def error(self, msg):
                self.errors.append(str(msg))

            def warning(self, msg):
                self.warnings.append(str(msg))

        sale = SimpleNamespace(
            id=7,
            marketplace="ebay",
            external_order_id="EO-7",
            tracking_number="TRK-7",
            tracking_status="in_transit",
            shipping_provider="usps",
            shipped_at=None,
        )

        # Disabled job guard path (warning + error on submit)
        fake_disabled = _FakeSt(token_value="token-123", selected=True)
        with patch.object(shipping, "st", fake_disabled), patch.object(
            shipping, "is_sync_job_enabled", return_value=False
        ), patch.object(
            shipping, "EbayClient", return_value=SimpleNamespace(is_configured=lambda: True)
        ), patch.object(
            shipping, "ensure_permission", return_value=True
        ), patch.object(
            shipping, "get_runtime_str", return_value="token-123"
        ):
            shipping._render_ebay_tracking_push(
                SimpleNamespace(),
                actor="tester",
                sales=[sale],
                user=SimpleNamespace(username="admin", role="admin"),
            )
        self.assertTrue(fake_disabled.warnings)
        self.assertTrue(any("disabled by configuration" in e.lower() for e in fake_disabled.errors))

        # Missing token path
        fake_no_token = _FakeSt(token_value="", selected=True)
        with patch.object(shipping, "st", fake_no_token), patch.object(
            shipping, "is_sync_job_enabled", return_value=True
        ), patch.object(
            shipping, "EbayClient", return_value=SimpleNamespace(is_configured=lambda: True)
        ), patch.object(
            shipping, "ensure_permission", return_value=True
        ), patch.object(
            shipping, "get_runtime_str", return_value=""
        ):
            shipping._render_ebay_tracking_push(
                SimpleNamespace(),
                actor="tester",
                sales=[sale],
                user=SimpleNamespace(username="admin", role="admin"),
            )
        self.assertTrue(any("access token is required" in e.lower() for e in fake_no_token.errors))


if __name__ == "__main__":
    unittest.main()
