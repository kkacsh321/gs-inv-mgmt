import importlib.util
import sys
import types
import unittest
from pathlib import Path


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
    shared_name = "app.components.views.shared"
    if shared_name not in sys.modules:
        shared_path = root / "app" / "components" / "views" / "shared.py"
        shared_spec = importlib.util.spec_from_file_location(shared_name, shared_path)
        shared_mod = importlib.util.module_from_spec(shared_spec)
        assert shared_spec and shared_spec.loader
        shared_spec.loader.exec_module(shared_mod)
        sys.modules[shared_name] = shared_mod


def _load_lots_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    lots_path = root / "app" / "components" / "views" / "lots.py"
    spec = importlib.util.spec_from_file_location("test_lots_module", lots_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


lots = _load_lots_module()


class LotsHelpersTests(unittest.TestCase):
    def test_extract_json_object_variants(self):
        self.assertEqual(lots._extract_json_object(""), {})
        self.assertEqual(lots._extract_json_object('{"a":1}'), {"a": 1})
        wrapped = "prefix {\"x\": 2} suffix"
        self.assertEqual(lots._extract_json_object(wrapped), {"x": 2})
        self.assertEqual(lots._extract_json_object("no json here"), {})

    def test_extract_decimal_candidate(self):
        self.assertIsNone(lots._extract_decimal_candidate(None))
        self.assertEqual(lots._extract_decimal_candidate("$12.34"), 12.34)
        self.assertEqual(lots._extract_decimal_candidate(" -7.5 "), -7.5)
        self.assertIsNone(lots._extract_decimal_candidate("abc"))

    def test_extract_invoice_date_candidate(self):
        self.assertEqual(str(lots._extract_invoice_date_candidate("2026-04-01")), "2026-04-01")
        self.assertEqual(str(lots._extract_invoice_date_candidate("04/01/2026")), "2026-04-01")
        self.assertEqual(str(lots._extract_invoice_date_candidate("04-01-2026")), "2026-04-01")
        self.assertIsNone(lots._extract_invoice_date_candidate("bad-date"))

    def test_extract_first_line_item(self):
        payload = {"line_items": [{"description": "A"}, {"description": "B"}]}
        self.assertEqual(lots._extract_first_line_item(payload), {"description": "A"})
        self.assertEqual(lots._extract_first_line_item({"line_items": []}), {})
        self.assertEqual(lots._extract_first_line_item({"line_items": ["bad", 1]}), {})
        self.assertEqual(lots._extract_first_line_item({}), {})


if __name__ == "__main__":
    unittest.main()
