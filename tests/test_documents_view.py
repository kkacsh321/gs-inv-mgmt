import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None
        return _noop


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.calls = []
        self._radio_value = "Order"
        self._select_map = {}
        self._button_map = {}
        self._checkbox_map = {}
        self._form_submit_map = {}

    def set_radio_value(self, value: str):
        self._radio_value = value

    def set_select_value(self, label: str, value):
        self._select_map[label] = value

    def set_button_value(self, label: str, value: bool):
        self._button_map[("label", label)] = bool(value)

    def set_button_key_value(self, key: str, value: bool):
        self._button_map[("key", key)] = bool(value)

    def set_checkbox_value(self, label: str, value: bool):
        self._checkbox_map[label] = bool(value)

    def set_form_submit_value(self, label: str, value: bool):
        self._form_submit_map[label] = bool(value)

    def subheader(self, *a, **k):
        self.calls.append(("subheader", a, k))

    def caption(self, *a, **k):
        self.calls.append(("caption", a, k))

    def markdown(self, *a, **k):
        self.calls.append(("markdown", a, k))

    def info(self, *a, **k):
        self.calls.append(("info", a, k))

    def warning(self, *a, **k):
        self.calls.append(("warning", a, k))

    def button(self, *a, **k):
        label = a[0] if a else k.get("label")
        key = k.get("key")
        if ("key", key) in self._button_map:
            return self._button_map[("key", key)]
        if ("label", label) in self._button_map:
            return self._button_map[("label", label)]
        return False

    def columns(self, n):
        count = n if isinstance(n, int) else len(n)
        return [_Ctx() for _ in range(int(count))]

    def dataframe(self, *a, **k):
        self.calls.append(("dataframe", a, k))

    def selectbox(self, _label, options, index=0, **_kwargs):
        if _label in self._select_map and self._select_map[_label] in options:
            return self._select_map[_label]
        if not options:
            return None
        idx = max(0, min(int(index), len(options) - 1))
        return options[idx]

    def radio(self, _label, options, horizontal=False, key=None):
        _ = (horizontal, key)
        if self._radio_value in options:
            return self._radio_value
        return options[0]

    def text_input(self, _label, value="", **_kwargs):
        return value

    def text_area(self, _label, value="", **_kwargs):
        return value

    def number_input(self, _label, min_value=None, value=0, step=None, **_kwargs):
        _ = (min_value, step)
        return value

    def checkbox(self, label, value=False, **_kwargs):
        if label in self._checkbox_map:
            return self._checkbox_map[label]
        return value

    def metric(self, *a, **k):
        self.calls.append(("metric", a, k))

    def data_editor(self, data, **_kwargs):
        return data

    def color_picker(self, _label, value="#000000", **_kwargs):
        return value

    def date_input(self, _label, value=None, **_kwargs):
        return value

    def time_input(self, _label, value=None, **_kwargs):
        return value

    def rerun(self):
        self.calls.append(("rerun", (), {}))

    def success(self, *a, **k):
        self.calls.append(("success", a, k))

    def error(self, *a, **k):
        self.calls.append(("error", a, k))

    def stop(self):
        raise RuntimeError("streamlit_stop")

    def download_button(self, *a, **k):
        self.calls.append(("download_button", a, k))

    def divider(self):
        self.calls.append(("divider", (), {}))

    def form(self, _key, **_kwargs):
        return _Ctx()

    def form_submit_button(self, *_args, **_kwargs):
        label = _args[0] if _args else _kwargs.get("label")
        return bool(self._form_submit_map.get(label, False))



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
    path = root / "app" / "components" / "views" / "documents.py"
    spec = importlib.util.spec_from_file_location("test_documents_view_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


documents_view = _load_module()


class DocumentsViewTests(unittest.TestCase):
    def _repo_stub(self):
        return SimpleNamespace(
            list_document_template_profiles=lambda **kwargs: [],
            list_orders=lambda: [],
            list_sales=lambda: [],
            list_listings=lambda: [],
        )

    def _repo_with_handoff_store_stub(self):
        writes = []
        audits = []

        def _upsert_runtime_setting(**kwargs):
            writes.append(kwargs)
            return SimpleNamespace(id=1)

        def _record_audit_event(**kwargs):
            audits.append(kwargs)
            return SimpleNamespace(id=1)

        return SimpleNamespace(
            list_document_template_profiles=lambda **kwargs: [],
            list_orders=lambda: [],
            list_sales=lambda: [],
            list_listings=lambda: [],
            upsert_runtime_setting=_upsert_runtime_setting,
            record_audit_event=_record_audit_event,
            writes=writes,
            audits=audits,
        )

    def _listing_repo_stub(self):
        listing = SimpleNamespace(
            id=99,
            marketplace="facebook",
            listing_title="Test Listing",
            listing_price=25.0,
            quantity_listed=1,
            external_listing_id="",
            marketplace_details="",
            product_id=None,
            review_status="pending",
        )
        updates = []

        def _update_listing(listing_id, payload, actor):
            updates.append((listing_id, payload, actor))
            return listing

        return SimpleNamespace(
            list_document_template_profiles=lambda **kwargs: [],
            list_orders=lambda: [],
            list_sales=lambda: [],
            list_listings=lambda: [listing],
            update_listing=_update_listing,
            updates=updates,
            db=SimpleNamespace(rollback=lambda: None),
        )

    def _listing_repo_post_stub(self):
        listing = SimpleNamespace(
            id=99,
            marketplace="facebook",
            listing_title="Test Listing",
            listing_price=25.0,
            quantity_listed=1,
            external_listing_id="",
            marketplace_details="",
            product_id=None,
            review_status="pending",
        )
        created_orders = []
        created_sales = []
        created_artifacts = []
        audit_events = []
        integration_events = []

        def _create_order(**kwargs):
            created_orders.append(kwargs)
            return SimpleNamespace(id=501)

        def _create_sale(**kwargs):
            created_sales.append(kwargs)
            return SimpleNamespace(id=601)

        def _create_artifact(**kwargs):
            created_artifacts.append(kwargs)
            return SimpleNamespace(id=701, content_sha256="sha", storage_ref="")

        def _record_audit_event(**kwargs):
            audit_events.append(kwargs)
            return SimpleNamespace(id=801)

        def _log_integration_event(**kwargs):
            integration_events.append(kwargs)
            return SimpleNamespace(id=901)

        return SimpleNamespace(
            list_document_template_profiles=lambda **kwargs: [],
            list_orders=lambda: [],
            list_sales=lambda: [],
            list_listings=lambda: [listing],
            create_order=_create_order,
            create_sale=_create_sale,
            create_document_artifact=_create_artifact,
            record_audit_event=_record_audit_event,
            log_integration_event=_log_integration_event,
            created_orders=created_orders,
            created_sales=created_sales,
            created_artifacts=created_artifacts,
            audit_events=audit_events,
            integration_events=integration_events,
            db=SimpleNamespace(rollback=lambda: None),
        )

    def _listing_repo_post_with_queue_stub(self):
        base = self._listing_repo_post_stub()
        queue_jobs = []

        def _create_integration_queue_job(**kwargs):
            queue_jobs.append(kwargs)
            return SimpleNamespace(id=990 + len(queue_jobs))

        base.create_integration_queue_job = _create_integration_queue_job
        base.queue_jobs = queue_jobs
        return base

    def test_render_documents_order_empty(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Order")
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(self._repo_stub())
        self.assertTrue(any(c[0] == "info" and "No orders" in str(c[1][0]) for c in fake_st.calls))

    def test_render_documents_sale_empty(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Sale")
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(self._repo_stub())
        self.assertTrue(any(c[0] == "info" and "No sales" in str(c[1][0]) for c in fake_st.calls))

    def test_render_documents_listing_empty(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(self._repo_stub())
        self.assertTrue(any(c[0] == "info" and "No listings" in str(c[1][0]) for c in fake_st.calls))

    def test_render_documents_listing_not_sold_end_listing(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_select_value("Listing Outcome", "Not Sold / Remove Listing")
        fake_st.set_button_key_value("documents_listing_end_not_sold_btn_99", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_stub()
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(repo)
        self.assertEqual(len(repo.updates), 1)
        listing_id, payload, actor = repo.updates[0]
        self.assertEqual(int(listing_id), 99)
        self.assertEqual(payload.get("listing_status"), "ended")
        self.assertEqual(actor, "admin")

    def test_render_documents_listing_not_sold_archive_listing(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_select_value("Listing Outcome", "Not Sold / Remove Listing")
        fake_st.set_button_key_value("documents_listing_archive_btn_99", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_stub()
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(repo)
        self.assertEqual(len(repo.updates), 1)
        listing_id, payload, actor = repo.updates[0]
        self.assertEqual(int(listing_id), 99)
        self.assertEqual(payload.get("listing_status"), "ended")
        self.assertEqual(payload.get("review_status"), "rejected")
        self.assertEqual(actor, "admin")

    def test_render_documents_listing_sold_create_sale_path(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_button_key_value("documents_listing_create_sale_btn_99", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_post_stub()
        user = SimpleNamespace(username="admin", role="admin")

        def _runtime_bool(_repo, key, default):
            if key == "documents_listing_posting_enabled":
                return True
            return default

        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "resolve_google_workspace_config", return_value=SimpleNamespace(enabled=False)), \
            patch.object(documents_view, "get_runtime_bool", side_effect=_runtime_bool), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(repo)

        self.assertEqual(len(repo.created_orders), 1)
        self.assertEqual(len(repo.created_sales), 1)
        self.assertEqual(len(repo.created_artifacts), 1)
        self.assertTrue(any(c[0] == "success" for c in fake_st.calls))

    def test_render_documents_listing_sold_policy_blocked(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_button_key_value("documents_listing_create_sale_btn_99", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_post_stub()
        user = SimpleNamespace(username="admin", role="admin")

        def _runtime_bool(_repo, key, default):
            if key == "documents_listing_posting_enabled":
                return False
            return default

        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "resolve_google_workspace_config", return_value=SimpleNamespace(enabled=False)), \
            patch.object(documents_view, "get_runtime_bool", side_effect=_runtime_bool), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "streamlit_stop"):
                documents_view.render_documents(repo)
        self.assertEqual(len(repo.created_sales), 0)
        self.assertTrue(any(c[0] == "error" and "Posting blocked by runtime role policy." in str(c[1][0]) for c in fake_st.calls))

    def test_render_documents_gmail_failure_queues_retry(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_form_submit_value("Send Document Email", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_post_with_queue_stub()
        user = SimpleNamespace(username="admin", role="admin")
        google_cfg = SimpleNamespace(
            enabled=True,
            access_token="tok",
            sender_email="sales@goldenstackers.com",
            default_calendar_id="primary",
            default_timezone="America/Denver",
            drive_root_folder_id="",
        )
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "resolve_google_workspace_config", return_value=google_cfg), \
            patch.object(documents_view, "send_gmail_message", side_effect=RuntimeError("gmail down")), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(repo)
        self.assertEqual(len(repo.queue_jobs), 1)
        self.assertEqual(repo.queue_jobs[0].get("action"), "gmail_send_document_email")
        self.assertTrue(any(c[0] == "warning" and "queued retry job" in str(c[1][0]).lower() for c in fake_st.calls))

    def test_render_documents_calendar_failure_queues_retry(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_form_submit_value("Create Calendar Event", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_post_with_queue_stub()
        user = SimpleNamespace(username="admin", role="admin")
        google_cfg = SimpleNamespace(
            enabled=True,
            access_token="tok",
            sender_email="sales@goldenstackers.com",
            default_calendar_id="primary",
            default_timezone="America/Denver",
            drive_root_folder_id="",
        )
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "resolve_google_workspace_config", return_value=google_cfg), \
            patch.object(documents_view, "create_calendar_event", side_effect=RuntimeError("calendar down")), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(repo)
        self.assertEqual(len(repo.queue_jobs), 1)
        self.assertEqual(repo.queue_jobs[0].get("action"), "calendar_create_event")
        self.assertTrue(any(c[0] == "warning" and "queued retry job" in str(c[1][0]).lower() for c in fake_st.calls))

    def test_render_documents_drive_failure_queues_retry(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Listing")
        fake_st.set_form_submit_value("Upload to Google Drive", True)
        fake_st.set_checkbox_value("Use line-item taxability overrides (Auto mode)", False)
        repo = self._listing_repo_post_with_queue_stub()
        user = SimpleNamespace(username="admin", role="admin")
        google_cfg = SimpleNamespace(
            enabled=True,
            access_token="tok",
            sender_email="sales@goldenstackers.com",
            default_calendar_id="primary",
            default_timezone="America/Denver",
            drive_root_folder_id="",
        )
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "ensure_permission", return_value=True), \
            patch.object(documents_view, "_render_retained_artifacts", return_value=None), \
            patch.object(documents_view.components, "html", return_value=None), \
            patch.object(documents_view, "resolve_google_workspace_config", return_value=google_cfg), \
            patch.object(documents_view, "upload_drive_file", side_effect=RuntimeError("drive down")), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(repo)
        self.assertEqual(len(repo.queue_jobs), 1)
        self.assertEqual(repo.queue_jobs[0].get("action"), "drive_upload_artifact")
        self.assertTrue(any(c[0] == "warning" and "queued retry job" in str(c[1][0]).lower() for c in fake_st.calls))

    def test_render_documents_clear_prefill_clears_session_keys(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Order")
        fake_st.set_button_key_value("documents_clear_prefill_btn", True)
        fake_st.session_state.update(
            {
                "documents_prefill_source_type": "Order",
                "documents_prefill_source_id": 123,
                "documents_prefill_doc_type": "invoice",
                "documents_prefill_tax_jurisdiction": "Golden, Colorado",
                "documents_prefill_tax_rate_percent": 8.4,
                "documents_prefill_tax_shipping_taxable": False,
                "documents_prefill_applied": True,
            }
        )
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=""):
            documents_view.render_documents(self._repo_stub())

        for key in [
            "documents_prefill_source_type",
            "documents_prefill_source_id",
            "documents_prefill_doc_type",
            "documents_prefill_tax_jurisdiction",
            "documents_prefill_tax_rate_percent",
            "documents_prefill_tax_shipping_taxable",
            "documents_prefill_applied",
        ]:
            self.assertNotIn(key, fake_st.session_state)
        self.assertTrue(any(c[0] == "rerun" for c in fake_st.calls))

    def test_render_documents_clear_handoff_history_persists_empty_list(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Order")
        fake_st.set_button_key_value("documents_clear_handoff_history_btn", True)
        user = SimpleNamespace(username="admin", role="admin")
        repo = self._repo_with_handoff_store_stub()
        handoffs_json = (
            '[{"at":"2026-04-01T12:00:00Z","source_type":"Sale","source_id":7,'
            '"doc_type":"invoice","handoff_from":"reports"}]'
        )
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=handoffs_json):
            documents_view.render_documents(repo)

        self.assertEqual(fake_st.session_state.get("documents_recent_handoffs"), [])
        self.assertEqual(len(repo.writes), 1)
        self.assertEqual(repo.writes[0].get("value"), "[]")
        self.assertEqual(len(repo.audits), 1)
        self.assertTrue(any(c[0] == "rerun" for c in fake_st.calls))

    def test_render_documents_reopen_handoff_sets_prefill(self):
        fake_st = _FakeSt()
        fake_st.set_radio_value("Order")
        fake_st.set_button_key_value("documents_reopen_handoff_btn", True)
        user = SimpleNamespace(username="admin", role="admin")
        handoffs_json = (
            '[{"at":"2026-04-01T12:00:00Z","source_type":"Listing","source_id":99,'
            '"doc_type":"receipt","handoff_from":"sales"}]'
        )
        with patch.object(documents_view, "st", fake_st), \
            patch.object(documents_view, "current_user", return_value=user), \
            patch.object(documents_view, "render_help_panel", return_value=None), \
            patch.object(documents_view, "get_runtime_bool", return_value=True), \
            patch.object(documents_view, "get_runtime_int", return_value=5), \
            patch.object(documents_view, "get_runtime_str", return_value=handoffs_json):
            documents_view.render_documents(self._repo_stub())

        self.assertEqual(fake_st.session_state.get("documents_prefill_source_type"), "Listing")
        self.assertEqual(fake_st.session_state.get("documents_prefill_source_id"), 99)
        self.assertEqual(fake_st.session_state.get("documents_prefill_doc_type"), "receipt")
        self.assertFalse(bool(fake_st.session_state.get("documents_prefill_applied")))
        self.assertTrue(any(c[0] == "rerun" for c in fake_st.calls))


if __name__ == "__main__":
    unittest.main()
