#!/usr/bin/env python3
"""Run segmented unittest suites for faster, parallelizable QA execution.

Suites:
- fast: all unittests except explicitly tagged integration modules
- integration: explicitly tagged integration modules
- all: full unittest discover run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INTEGRATION_MODULE_TOKENS = (
    "test_repository_inventory_movements",
)


def _iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _is_integration_case(case: unittest.TestCase) -> bool:
    test_id = case.id().lower()
    return any(token in test_id for token in INTEGRATION_MODULE_TOKENS)


def _select_suite(discovered: unittest.TestSuite, suite_name: str) -> unittest.TestSuite:
    if suite_name == "all":
        return discovered

    selected = unittest.TestSuite()
    for case in _iter_cases(discovered):
        integration_case = _is_integration_case(case)
        if suite_name == "integration" and integration_case:
            selected.addTest(case)
        elif suite_name == "fast" and not integration_case:
            selected.addTest(case)
    return selected


def _suite_summary(discovered: unittest.TestSuite, selected: unittest.TestSuite, suite_name: str) -> dict[str, Any]:
    discovered_cases = list(_iter_cases(discovered))
    selected_cases = list(_iter_cases(selected))
    integration_discovered = sum(1 for case in discovered_cases if _is_integration_case(case))
    integration_selected = sum(1 for case in selected_cases if _is_integration_case(case))
    return {
        "suite": suite_name,
        "discovered_total": len(discovered_cases),
        "discovered_integration": integration_discovered,
        "discovered_fast": max(0, len(discovered_cases) - integration_discovered),
        "selected_total": len(selected_cases),
        "selected_integration": integration_selected,
        "selected_fast": max(0, len(selected_cases) - integration_selected),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run segmented unittest suites")
    parser.add_argument(
        "--suite",
        choices=("fast", "integration", "all"),
        default="all",
        help="suite selector",
    )
    parser.add_argument(
        "--pattern",
        default="test_*.py",
        help="discovery pattern (default: test_*.py)",
    )
    parser.add_argument(
        "--verbosity",
        type=int,
        default=2,
        help="unittest verbosity",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="only print/write suite selection summary without executing tests",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="optional path to write suite summary json",
    )
    args = parser.parse_args()

    loader = unittest.defaultTestLoader
    discovered = loader.discover(start_dir="tests", pattern=args.pattern)
    suite = _select_suite(discovered, args.suite)
    summary = _suite_summary(discovered, suite, args.suite)
    total = suite.countTestCases()

    if args.list_only:
        print(
            "Suite selection summary: "
            f"suite='{args.suite}' selected={summary['selected_total']} "
            f"(fast={summary['selected_fast']}, integration={summary['selected_integration']}) "
            f"from discovered={summary['discovered_total']}"
        )
        if args.json_out:
            output_path = Path(str(args.json_out)).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(summary)
            payload["list_only"] = True
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0

    if total == 0:
        print(f"No tests selected for suite='{args.suite}'")
        if args.json_out:
            output_path = Path(str(args.json_out)).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(summary)
            payload["duration_seconds"] = 0.0
            payload["success"] = True
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0

    print(f"Running unittest suite '{args.suite}' with {total} test(s)")
    started_at = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
    duration = time.perf_counter() - started_at
    if args.json_out:
        output_path = Path(str(args.json_out)).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(summary)
        payload["duration_seconds"] = round(float(duration), 4)
        payload["success"] = bool(result.wasSuccessful())
        payload["tests_run"] = int(getattr(result, "testsRun", total) or 0)
        payload["failures"] = int(len(getattr(result, "failures", []) or []))
        payload["errors"] = int(len(getattr(result, "errors", []) or []))
        payload["skipped"] = int(len(getattr(result, "skipped", []) or []))
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
