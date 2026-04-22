from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def _load_module():
    root = Path(__file__).resolve().parents[1]
    target = root / "scripts" / "run_test_suites.py"
    spec = importlib.util.spec_from_file_location("run_test_suites_script", target)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load run_test_suites module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_case(module_name: str, name: str):
    def _test_method(self):
        return None

    cls = type(
        f"Generated{name.title()}",
        (unittest.TestCase,),
        {name: _test_method},
    )
    cls.__module__ = module_name
    return cls(name)


class RunTestSuitesScriptTests(unittest.TestCase):
    def test_select_suite_fast_and_integration(self) -> None:
        mod = _load_module()
        discovered = unittest.TestSuite(
            [
                _make_case("tests.test_repository_inventory_movements", "test_integration_path"),
                _make_case("tests.test_sync_jobs", "test_fast_path"),
            ]
        )

        fast = mod._select_suite(discovered, "fast")
        integration = mod._select_suite(discovered, "integration")
        summary_fast = mod._suite_summary(discovered, fast, "fast")
        summary_integration = mod._suite_summary(discovered, integration, "integration")

        self.assertEqual(summary_fast["selected_total"], 1)
        self.assertEqual(summary_fast["selected_fast"], 1)
        self.assertEqual(summary_fast["selected_integration"], 0)

        self.assertEqual(summary_integration["selected_total"], 1)
        self.assertEqual(summary_integration["selected_fast"], 0)
        self.assertEqual(summary_integration["selected_integration"], 1)

    def test_main_list_only_writes_json_summary(self) -> None:
        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "suite_fast.json"
            argv = [
                "run_test_suites.py",
                "--suite",
                "fast",
                "--pattern",
                "test_run_test_suites_script.py",
                "--list-only",
                "--json-out",
                str(out_path),
            ]
            with patch("sys.argv", argv):
                code = mod.main()
            self.assertEqual(code, 0)
            self.assertTrue(out_path.exists())
            payload = __import__("json").loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["suite"], "fast")
            self.assertTrue(payload["list_only"])
            self.assertGreaterEqual(int(payload["selected_total"]), 1)


if __name__ == "__main__":
    unittest.main()
