from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _DummySt:
    def __init__(self) -> None:
        self.session_state = {}


class _Upload:
    def __init__(self, *, name: str = "", content_type: str = "image/jpeg", data: bytes = b"img") -> None:
        self.name = name
        self.type = content_type
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


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


def _load_module(file_name: str, module_name: str):
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / file_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ciw = _load_module("coin_intake_wizard.py", "test_coin_intake_wizard")
iiw = _load_module("inventory_intake_wizard.py", "test_inventory_intake_wizard")


class IntakeWizardHelperTests(unittest.TestCase):
    def test_coin_ref_summary_formats_year_ranges(self) -> None:
        ref = types.SimpleNamespace(
            coin_name="Morgan Dollar",
            country="United States",
            denomination="$1",
            series="Morgan",
            year_start=1878,
            year_end=1921,
            metal_type="Silver",
        )
        got = ciw._coin_ref_summary(ref)
        self.assertEqual(got, "Morgan Dollar | United States | $1 | Morgan | 1878-1921 | Silver")

    def test_coin_ref_summary_handles_none(self) -> None:
        self.assertEqual(ciw._coin_ref_summary(None), "")

    def test_coin_json_extract_direct_and_embedded(self) -> None:
        self.assertEqual(ciw._try_extract_json_object('{"a":1}'), {"a": 1})
        self.assertEqual(ciw._try_extract_json_object("prefix {\"b\":2} suffix"), {"b": 2})
        self.assertEqual(ciw._try_extract_json_object("not-json"), {})

    def test_inventory_json_extract_direct_and_embedded(self) -> None:
        self.assertEqual(iiw._try_extract_json_object('{"x":"y"}'), {"x": "y"})
        self.assertEqual(iiw._try_extract_json_object("abc {\"m\":3} xyz"), {"m": 3})
        self.assertEqual(iiw._try_extract_json_object("[]"), {})

    def test_coin_buffer_upload_file_read(self) -> None:
        buf = ciw._BufferedUploadFile(name="n.jpg", content_type="image/jpeg", data=b"123")
        self.assertEqual(buf.name, "n.jpg")
        self.assertEqual(buf.type, "image/jpeg")
        self.assertEqual(buf.read(), b"123")

    def test_inventory_buffer_upload_file_read(self) -> None:
        buf = iiw._BufferedUploadFile(name="n.jpg", content_type="image/jpeg", data=b"456")
        self.assertEqual(buf.name, "n.jpg")
        self.assertEqual(buf.type, "image/jpeg")
        self.assertEqual(buf.read(), b"456")

    def test_buffer_coin_ai_images_sets_session_state(self) -> None:
        dummy_st = _DummySt()
        with patch.object(ciw, "st", dummy_st):
            ciw._buffer_coin_ai_images(
                primary=_Upload(name="obverse.png", content_type="image/png", data=b"a"),
                secondary=_Upload(name="", content_type="image/jpeg", data=b"b"),
            )
        buffered = dummy_st.session_state["coin_intake_ai_buffered_media"]
        self.assertEqual(len(buffered), 2)
        self.assertEqual(buffered[0]["name"], "obverse.png")
        self.assertEqual(buffered[0]["content_type"], "image/png")
        self.assertEqual(buffered[0]["data"], b"a")
        self.assertTrue(buffered[1]["name"].startswith("coin_ai_reverse_"))
        self.assertTrue(buffered[1]["name"].endswith(".jpeg"))

    def test_buffer_coin_ai_images_skips_empty_bytes(self) -> None:
        dummy_st = _DummySt()
        with patch.object(ciw, "st", dummy_st):
            ciw._buffer_coin_ai_images(
                primary=_Upload(name="", content_type="image/jpeg", data=b""),
                secondary=None,
            )
        self.assertEqual(dummy_st.session_state["coin_intake_ai_buffered_media"], [])

    def test_buffer_inventory_ai_images_sets_session_state(self) -> None:
        dummy_st = _DummySt()
        with patch.object(iiw, "st", dummy_st):
            iiw._buffer_inventory_ai_images(
                primary=_Upload(name="", content_type="image/webp", data=b"1"),
                secondary=_Upload(name="secondary.jpg", content_type="image/jpeg", data=b"2"),
            )
        buffered = dummy_st.session_state["inv_intake_ai_buffered_media"]
        self.assertEqual(len(buffered), 2)
        self.assertTrue(buffered[0]["name"].startswith("inventory_ai_primary_"))
        self.assertTrue(buffered[0]["name"].endswith(".webp"))
        self.assertEqual(buffered[1]["name"], "secondary.jpg")
        self.assertEqual(buffered[1]["data"], b"2")

    def test_buffer_inventory_ai_images_skips_empty_bytes(self) -> None:
        dummy_st = _DummySt()
        with patch.object(iiw, "st", dummy_st):
            iiw._buffer_inventory_ai_images(
                primary=_Upload(name="", content_type="image/jpeg", data=b""),
                secondary=None,
            )
        self.assertEqual(dummy_st.session_state["inv_intake_ai_buffered_media"], [])

    def test_inventory_grader_normalization_uses_structured_summary_when_available(self) -> None:
        raw = '{"estimated_grade_range":"MS63","submit_for_professional_grading":"yes"}'
        structured = {"estimated_grade_range": "MS63", "submit_for_professional_grading": "YES"}
        got = iiw._normalize_inventory_grader_output(raw_result_text=raw, structured_grade=structured)
        self.assertIn("Estimated Grade Range: MS63", got)
        self.assertIn("Submit For Professional Grading: YES", got)

    def test_inventory_grader_normalization_falls_back_when_structured_render_is_blank(self) -> None:
        raw = '{"estimated_grade_range":"AU55","recommendation_rationale":"decent upside if straight-graded"}'
        structured = {"estimated_grade_range": "", "submit_for_professional_grading": ""}
        got = iiw._normalize_inventory_grader_output(raw_result_text=raw, structured_grade=structured)
        self.assertIn("AU55", got)


if __name__ == "__main__":
    unittest.main()
