#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCOPED_CORE_PATHS = {
    "app/repository.py",
    "app/auth.py",
    "app/page_common.py",
    "app/config.py",
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _coverage_summary(coverage_payload: dict[str, Any]) -> dict[str, Any]:
    totals = coverage_payload.get("totals") or {}
    files = coverage_payload.get("files") or {}

    global_pct = _safe_float(totals.get("percent_covered"))
    global_display = str(totals.get("percent_covered_display") or f"{global_pct:.2f}")

    scoped_covered = 0
    scoped_total = 0
    for path, payload in files.items():
        normalized = str(path).replace("\\", "/")
        if normalized in SCOPED_CORE_PATHS or normalized.startswith("app/services/"):
            summary = payload.get("summary") or {}
            scoped_covered += _safe_int(summary.get("covered_lines"))
            scoped_total += _safe_int(summary.get("num_statements"))

    scoped_pct = (float(scoped_covered) / float(scoped_total) * 100.0) if scoped_total else 0.0
    return {
        "global_percent": round(global_pct, 2),
        "global_percent_display": global_display,
        "scoped_core_percent": round(scoped_pct, 2),
        "scoped_core_paths": sorted(SCOPED_CORE_PATHS | {"app/services/*"}),
        "covered_lines": _safe_int(totals.get("covered_lines")),
        "num_statements": _safe_int(totals.get("num_statements")),
        "missing_lines": _safe_int(totals.get("missing_lines")),
    }


def _playwright_summary(playwright_payload: dict[str, Any]) -> dict[str, Any]:
    stats = playwright_payload.get("stats") or {}
    return {
        "expected": _safe_int(stats.get("expected")),
        "unexpected": _safe_int(stats.get("unexpected")),
        "flaky": _safe_int(stats.get("flaky")),
        "skipped": _safe_int(stats.get("skipped")),
        "duration_ms": _safe_int(stats.get("duration")),
    }


def _coverage_gate_summary(
    *,
    coverage: dict[str, Any],
    global_gate: float,
    scoped_core_gate: float,
) -> dict[str, Any]:
    global_pct = _safe_float(coverage.get("global_percent"))
    scoped_pct = _safe_float(coverage.get("scoped_core_percent"))
    return {
        "global_gate_percent": round(float(global_gate), 2),
        "scoped_core_gate_percent": round(float(scoped_core_gate), 2),
        "global_pass": bool(global_pct >= float(global_gate)),
        "scoped_core_pass": bool(scoped_pct >= float(scoped_core_gate)),
        "all_pass": bool(global_pct >= float(global_gate) and scoped_pct >= float(scoped_core_gate)),
    }


def _build_markdown(
    *,
    unit_outcome: str,
    playwright_outcome: str,
    coverage: dict[str, Any],
    playwright: dict[str, Any],
    coverage_gates: dict[str, Any],
) -> str:
    gate_status = "PASS" if bool(coverage_gates.get("all_pass")) else "FAIL"
    return "\n".join(
        [
            "# QA Evidence Summary",
            "",
            "## Pipeline Outcomes",
            f"- Unit tests: `{unit_outcome}`",
            f"- Playwright: `{playwright_outcome}`",
            "",
            "## Coverage",
            f"- Global coverage: `{coverage.get('global_percent_display')}%`",
            f"- Scoped-core coverage: `{coverage.get('scoped_core_percent')}%`",
            f"- Lines: covered `{coverage.get('covered_lines')}` / total `{coverage.get('num_statements')}` (missing `{coverage.get('missing_lines')}`)",
            f"- Gate result: `{gate_status}` (global `>={coverage_gates.get('global_gate_percent')}%`, scoped-core `>={coverage_gates.get('scoped_core_gate_percent')}%`)",
            "",
            "## Playwright",
            f"- Expected passed: `{playwright.get('expected', 0)}`",
            f"- Unexpected failed: `{playwright.get('unexpected', 0)}`",
            f"- Flaky: `{playwright.get('flaky', 0)}`",
            f"- Skipped: `{playwright.get('skipped', 0)}`",
            f"- Duration (ms): `{playwright.get('duration_ms', 0)}`",
            "",
            "_Attach this artifact in GO_LIVE_CHECKLIST evidence links for QA release sign-off._",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build QA evidence artifacts from CI outputs.")
    parser.add_argument("--coverage-json", default="coverage.json")
    parser.add_argument("--playwright-json", default="playwright-results.json")
    parser.add_argument("--output-dir", default="qa-evidence")
    parser.add_argument("--unit-outcome", default="unknown")
    parser.add_argument("--playwright-outcome", default="unknown")
    parser.add_argument("--coverage-gate-global", default="38")
    parser.add_argument("--coverage-gate-core", default="88")
    parser.add_argument("--summary-out", default="")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    coverage_payload = _read_json(Path(args.coverage_json))
    playwright_payload = _read_json(Path(args.playwright_json))

    coverage = _coverage_summary(coverage_payload)
    playwright = _playwright_summary(playwright_payload)
    coverage_gates = _coverage_gate_summary(
        coverage=coverage,
        global_gate=_safe_float(args.coverage_gate_global),
        scoped_core_gate=_safe_float(args.coverage_gate_core),
    )
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    evidence = {
        "generated_at_utc": generated_at,
        "unit_outcome": args.unit_outcome,
        "playwright_outcome": args.playwright_outcome,
        "coverage": coverage,
        "coverage_gates": coverage_gates,
        "playwright": playwright,
    }

    summary_md = _build_markdown(
        unit_outcome=args.unit_outcome,
        playwright_outcome=args.playwright_outcome,
        coverage=coverage,
        playwright=playwright,
        coverage_gates=coverage_gates,
    )

    (output_dir / "qa_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    (output_dir / "qa_evidence.md").write_text(summary_md, encoding="utf-8")
    (output_dir / "coverage_gates.json").write_text(json.dumps(coverage_gates, indent=2), encoding="utf-8")

    if args.summary_out:
        Path(args.summary_out).write_text(summary_md, encoding="utf-8")


if __name__ == "__main__":
    main()
