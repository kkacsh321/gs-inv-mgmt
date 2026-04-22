from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


def _load_module():
    root = Path(__file__).resolve().parents[1]
    target = root / "scripts" / "build_qa_evidence.py"
    spec = importlib.util.spec_from_file_location("build_qa_evidence", target)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load build_qa_evidence module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildQaEvidenceTests(unittest.TestCase):
    def test_coverage_gate_summary(self) -> None:
        mod = _load_module()
        gates = mod._coverage_gate_summary(
            coverage={"global_percent": 38.2, "scoped_core_percent": 88.7},
            global_gate=38.0,
            scoped_core_gate=88.0,
        )
        self.assertTrue(gates["global_pass"])
        self.assertTrue(gates["scoped_core_pass"])
        self.assertTrue(gates["all_pass"])

    def test_markdown_includes_gate_result(self) -> None:
        mod = _load_module()
        text = mod._build_markdown(
            unit_outcome="success",
            playwright_outcome="success",
            coverage={
                "global_percent_display": "38.20",
                "scoped_core_percent": 88.7,
                "covered_lines": 100,
                "num_statements": 200,
                "missing_lines": 100,
            },
            playwright={"expected": 10, "unexpected": 0, "flaky": 0, "skipped": 1, "duration_ms": 1234},
            coverage_gates={
                "all_pass": True,
                "global_gate_percent": 38.0,
                "scoped_core_gate_percent": 88.0,
            },
        )
        self.assertIn("Gate result: `PASS`", text)
        self.assertIn("global `>=38.0%`", text)
        self.assertIn("scoped-core `>=88.0%`", text)


if __name__ == "__main__":
    unittest.main()

