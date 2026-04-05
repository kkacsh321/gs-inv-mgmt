import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib.util
import sys
import types


def _load_shared_module():
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

    root = Path(__file__).resolve().parents[1]
    shared_path = root / "app" / "components" / "views" / "shared.py"
    spec = importlib.util.spec_from_file_location("test_shared_module", shared_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


shared = _load_shared_module()


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.button_map = {}
        self.checkbox_map = {}
        self.selectbox_map = {}
        self.multiselect_map = {}
        self.text_input_map = {}
        self.radio_map = {}
        self.file_uploader_map = {}
        self.camera_map = {}
        self.download_calls = []
        self.dataframes = []
        self.images = []
        self.videos = []
        self.markdowns = []
        self.captions = []
        self.infos = []
        self.errors = []
        self.successes = []
        self.switch_pages = []
        self.rerun_called = False

    def expander(self, *_a, **_k):
        return _Ctx()

    def columns(self, n):
        return [_Ctx() for _ in range(int(n))]

    def tabs(self, labels):
        return [_Ctx() for _ in labels]

    def caption(self, text):
        self.captions.append(str(text))

    def markdown(self, text):
        self.markdowns.append(str(text))

    def info(self, text):
        self.infos.append(str(text))

    def warning(self, text):
        self.infos.append(str(text))

    def error(self, text):
        self.errors.append(str(text))

    def success(self, text):
        self.successes.append(str(text))

    def text_input(self, label, value="", key=None, **_k):
        return self.text_input_map.get(key or label, value)

    def selectbox(self, label, options, index=0, key=None, **_k):
        options = list(options)
        if not options:
            return None
        if (key or label) in self.selectbox_map:
            return self.selectbox_map[key or label]
        if 0 <= int(index) < len(options):
            return options[int(index)]
        return options[0]

    def multiselect(self, label, options, default=None, key=None, **_k):
        if (key or label) in self.multiselect_map:
            return list(self.multiselect_map[key or label])
        if default is not None:
            return list(default)
        return list(options)

    def checkbox(self, label, value=False, key=None, **_k):
        return bool(self.checkbox_map.get(key or label, value))

    def button(self, label, key=None, **_k):
        return bool(self.button_map.get(key or label, False))

    def download_button(self, *args, **kwargs):
        self.download_calls.append((args, kwargs))
        return None

    def dataframe(self, df, **_k):
        self.dataframes.append(df)

    def file_uploader(self, label, key=None, **_k):
        return self.file_uploader_map.get(key or label)

    def radio(self, label, options, key=None, **_k):
        return self.radio_map.get(key or label, list(options)[0])

    def camera_input(self, label, key=None, **_k):
        return self.camera_map.get(key or label)

    def image(self, value, **_k):
        self.images.append(value)

    def video(self, value, **_k):
        self.videos.append(value)

    def rerun(self):
        self.rerun_called = True
        raise RuntimeError("rerun")

    def switch_page(self, page):
        self.switch_pages.append(page)

    def stop(self):
        raise RuntimeError("stop")


class _FakeUpload:
    def __init__(self, name, content_type, data):
        self.name = name
        self.type = content_type
        self._data = data

    def read(self):
        return self._data


class SharedViewsTests(unittest.TestCase):
    def test_basic_helpers(self):
        self.assertEqual(shared.as_money(1234.5), "$1,234.50")
        self.assertEqual(shared.pretty_json(""), "{}")
        self.assertIn('"a": 1', shared.pretty_json('{"a":1}'))
        self.assertEqual(shared.pretty_json("nope"), "nope")
        self.assertEqual(shared.infer_media_type("image/jpeg"), "image")
        self.assertEqual(shared.infer_media_type("video/mp4"), "video")
        self.assertEqual(shared.infer_media_type("application/pdf"), "other")
        sku = shared.generate_sku("bullion", "silver")
        self.assertTrue(sku.startswith("GS-BUL-SIL-"))

    def test_upload_media_for_listing(self):
        created = []
        uploaded = []

        class Repo:
            def create_media_asset(self, **kwargs):
                created.append(kwargs)

        class Storage:
            def upload_file(self, **kwargs):
                uploaded.append(kwargs)
                return SimpleNamespace(
                    content_type=kwargs["content_type"],
                    size_bytes=len(kwargs["file_bytes"]),
                    bucket="b",
                    key=f"k/{kwargs['file_name']}",
                    url=f"https://x/{kwargs['file_name']}",
                )

        files = [
            _FakeUpload("a.jpg", "image/jpeg", b"img"),
            _FakeUpload("b.mp4", "video/mp4", b"vid"),
        ]
        count, errors = shared.upload_media_for_listing(
            Repo(),
            Storage(),
            listing_id=1,
            product_id=2,
            uploaded_files=files,
            uploaded_by="tester",
        )
        self.assertEqual(count, 2)
        self.assertEqual(errors, [])
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(len(created), 2)

    def test_handoff_to_documents_draft(self):
        fake_st = _FakeSt()
        fake_st.session_state["documents_recent_handoffs"] = []
        upserts = []

        class Repo:
            def upsert_runtime_setting(self, **kwargs):
                upserts.append(kwargs)

        with patch.object(shared, "st", fake_st), patch.object(
            shared, "settings", SimpleNamespace(app_env="local")
        ), patch(
            "app.services.runtime_settings.get_runtime_str", return_value=""
        ):
            shared.handoff_to_documents_draft(
                source_type="sale",
                source_id=42,
                doc_type="invoice",
                handoff_from="sales",
                repo=Repo(),
                actor="admin",
            )
        self.assertEqual(fake_st.session_state["documents_prefill_source_id"], 42)
        self.assertEqual(fake_st.switch_pages[-1], "pages/16_Documents.py")
        self.assertEqual(len(upserts), 1)

    def test_render_table_toolbar(self):
        fake_st = _FakeSt()
        df = shared.pd.DataFrame([{"a": 1}, {"a": 2}])
        with patch.object(shared, "st", fake_st):
            shared.render_table_toolbar(
                df=df,
                section_key="k",
                export_basename="exp",
                active_filters={"status": ["open", "closed"], "query": "abc"},
            )
        self.assertEqual(len(fake_st.download_calls), 2)

    def test_render_existing_media_attach_selector_empty(self):
        fake_st = _FakeSt()

        class Repo:
            def list_media_assets(self):
                return []

        with patch.object(shared, "st", fake_st):
            result = shared.render_existing_media_attach_selector(
                repo=Repo(),
                key_prefix="sel",
                section_title="Attach",
                help_text="help",
            )
        self.assertEqual(result, [])

    def test_render_existing_media_attach_selector_selected(self):
        fake_st = _FakeSt()
        fake_st.checkbox_map["sel2_only_unlinked"] = False
        fake_st.multiselect_map["sel2_selected_labels"] = [
            "#1 | image | file.jpg | p=- | l=-"
        ]

        row = SimpleNamespace(
            id=1,
            media_type="image",
            original_filename="file.jpg",
            content_type="image/jpeg",
            product_id=None,
            listing_id=None,
        )

        class Repo:
            def list_media_assets(self):
                return [row]

        with patch.object(shared, "st", fake_st):
            result = shared.render_existing_media_attach_selector(
                repo=Repo(),
                key_prefix="sel2",
                section_title="Attach",
                help_text="help",
            )
        self.assertEqual(result, [1])

    def test_render_media_capture_inputs_basic(self):
        fake_st = _FakeSt()
        fake_st.file_uploader_map["cap_files_upload"] = [_FakeUpload("x.jpg", "image/jpeg", b"x")]
        fake_st.file_uploader_map["cap_video_capture"] = _FakeUpload("v.mp4", "video/mp4", b"v")
        fake_st.camera_map["cap_camera_photo"] = _FakeUpload("c.jpg", "image/jpeg", b"c")
        with patch.object(shared, "st", fake_st):
            items = shared.render_media_capture_inputs(key_prefix="cap", allow_enhanced=False)
        self.assertEqual(len(items), 3)

    def test_render_media_gallery(self):
        fake_st = _FakeSt()
        items = [
            SimpleNamespace(id=1, media_type="image", original_filename="a.jpg", s3_url="https://x/a.jpg", product_id=1, listing_id=2, size_bytes=10, content_type="image/jpeg", s3_bucket="b", s3_key="k1"),
            SimpleNamespace(id=2, media_type="video", original_filename="b.mp4", s3_url="https://x/b.mp4", product_id=1, listing_id=2, size_bytes=11, content_type="video/mp4", s3_bucket="b", s3_key="k2"),
            SimpleNamespace(id=3, media_type="document", original_filename="c.pdf", s3_url="https://x/c.pdf", product_id=1, listing_id=2, size_bytes=12, content_type="application/pdf", s3_bucket="b", s3_key="k3"),
        ]
        with patch.object(shared, "st", fake_st), patch.object(
            shared, "load_media_bytes", side_effect=[(b"img", "image/jpeg", None), (b"vid", "video/mp4", None), (None, "application/pdf", None)]
        ):
            shared.render_media_gallery(items, storage=None)
        self.assertEqual(len(fake_st.images), 1)
        self.assertEqual(len(fake_st.videos), 1)

    def test_load_media_bytes_storage_and_http(self):
        media = SimpleNamespace(content_type="image/jpeg", s3_bucket="b", s3_key="k", s3_url="https://x/a.jpg")
        storage = SimpleNamespace(enabled=True, get_object_bytes=lambda b, k: (b"ok", "image/jpeg"))
        data, ctype, err = shared.load_media_bytes(media, storage=storage)
        self.assertEqual(data, b"ok")
        self.assertEqual(ctype, "image/jpeg")
        self.assertIsNone(err)

        bad_storage = SimpleNamespace(enabled=True, get_object_bytes=lambda b, k: (_ for _ in ()).throw(RuntimeError("boom")))
        fake_resp = SimpleNamespace(content=b"http", headers={"Content-Type": "image/png"}, raise_for_status=lambda: None)
        with patch.object(shared.requests, "get", return_value=fake_resp):
            data, ctype, err = shared.load_media_bytes(media, storage=bad_storage)
        self.assertEqual(data, b"http")
        self.assertEqual(ctype, "image/png")
        self.assertIsNone(err)

    def test_render_media_file_actions_no_media(self):
        fake_st = _FakeSt()
        with patch.object(shared, "st", fake_st):
            shared.render_media_file_actions([])
        self.assertTrue(any("No media files" in m for m in fake_st.infos))

    def test_render_media_file_actions_delete(self):
        fake_st = _FakeSt()
        fake_st.checkbox_map["m_confirm_delete"] = True
        fake_st.button_map["m_delete_selected_btn"] = True

        media_row = SimpleNamespace(
            id=7,
            media_type="image",
            original_filename="x.jpg",
            content_type="image/jpeg",
            s3_bucket="b",
            s3_key="k",
            s3_url="https://x/x.jpg",
        )

        class Repo:
            def __init__(self):
                self.deleted = []

            def delete_media_asset(self, media_id, actor):
                self.deleted.append((media_id, actor))
                return True

        class Storage:
            enabled = True

            def __init__(self):
                self.deleted = []

            def delete_object(self, bucket, key):
                self.deleted.append((bucket, key))

        repo = Repo()
        storage = Storage()
        with patch.object(shared, "st", fake_st), patch.object(shared, "load_media_bytes", return_value=(b"img", "image/jpeg", None)):
            shared.render_media_file_actions(
                [media_row],
                storage=storage,
                key_prefix="m",
                repo=repo,
                actor="admin",
                user=None,
            )
        self.assertEqual(repo.deleted, [(7, "admin")])
        self.assertEqual(storage.deleted, [("b", "k")])

    def test_render_ebay_push_history_no_runs(self):
        fake_st = _FakeSt()
        repo = SimpleNamespace(list_sync_runs=lambda provider=None, limit=100: [])
        with patch.object(shared, "st", fake_st):
            shared.render_ebay_push_history(repo)
        self.assertTrue(any("No eBay push runs" in m for m in fake_st.infos))

    def test_render_ebay_push_history_resolve(self):
        fake_st = _FakeSt()
        fake_st.button_map["ebay_push_history_resolve_run_errors_5"] = True
        run = SimpleNamespace(
            id=5,
            direction="push",
            job_name="ebay_orders_push",
            status="failed",
            started_at=datetime(2026, 4, 1, 10, 0, 0),
            completed_at=datetime(2026, 4, 1, 10, 1, 0),
            records_processed=1,
            records_updated=0,
            records_failed=1,
            retry_count=0,
            retry_of_run_id=None,
            notes="x",
        )
        err1 = SimpleNamespace(id=1, resolved_at=None, severity="error", code="E", message="m", occurred_at=datetime(2026, 4, 1, 10, 0, 0))
        err2 = SimpleNamespace(id=2, resolved_at=datetime(2026, 4, 1, 10, 2, 0), severity="error", code="E2", message="m2", occurred_at=datetime(2026, 4, 1, 10, 0, 0))

        class Repo:
            def __init__(self):
                self.resolved = []

            def list_sync_runs(self, provider=None, limit=100):
                return [run]

            def list_sync_errors(self, run_id, limit=500):
                return [err1, err2]

            def list_sync_events(self, run_id, limit=500):
                return []

            def resolve_sync_error(self, error_id, actor=""):
                self.resolved.append((error_id, actor))

            def retry_sync_run(self, *_a, **_k):
                return SimpleNamespace(id=999)

        repo = Repo()
        with patch.object(shared, "st", fake_st), patch.object(shared, "is_sync_job_enabled", return_value=True), patch.object(
            shared, "ensure_permission", return_value=True
        ):
            with self.assertRaises(RuntimeError):
                shared.render_ebay_push_history(repo, actor="admin", user=SimpleNamespace(role="admin"))
        self.assertEqual(repo.resolved, [(1, "admin")])


if __name__ == "__main__":
    unittest.main()
