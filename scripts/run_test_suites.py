#!/usr/bin/env python3
"""Run segmented unittest suites for faster, parallelizable QA execution.

Suites:
- fast: all unittests except explicitly tagged integration modules
- integration: explicitly tagged integration modules
- all: full unittest discover run
"""

from __future__ import annotations

import argparse
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

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
    args = parser.parse_args()

    loader = unittest.defaultTestLoader
    discovered = loader.discover(start_dir="tests", pattern=args.pattern)
    suite = _select_suite(discovered, args.suite)
    total = suite.countTestCases()

    if total == 0:
        print(f"No tests selected for suite='{args.suite}'")
        return 0

    print(f"Running unittest suite '{args.suite}' with {total} test(s)")
    result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
