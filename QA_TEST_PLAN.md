# GoldenStackers QA Test Plan

## Objectives

- Catch regressions in core inventory/listing/sales/shipping business logic before publish.
- Catch critical UI regressions in operator workflows (Streamlit) with browser smoke/flow tests.
- Produce CI artifacts suitable for go-live evidence.

## Current Baseline

- Unit tests: `python -m unittest discover -s tests -p "test_*.py"`
- Segmented unit suites for CI fan-out:
  - fast: `python scripts/run_test_suites.py --suite fast`
  - integration: `python scripts/run_test_suites.py --suite integration`
- E2E smoke/critical flows: Playwright Chromium (`tests/e2e/smoke.spec.ts`, `tests/e2e/auth_flow.spec.ts`, `tests/e2e/intake_wizard.spec.ts`, `tests/e2e/coin_intake_wizard.spec.ts`, `tests/e2e/products_flow.spec.ts`, `tests/e2e/listings_flow.spec.ts`, `tests/e2e/shipping_flow.spec.ts`, `tests/e2e/sync_flow.spec.ts`, `tests/e2e/admin_golive_flow.spec.ts`)
- CI:
  - `docker_build.yaml` runs static compile + unit tests before image publish.
  - `qa_tests.yaml` runs unit tests + Playwright smoke against compose app stack and uploads artifacts.
  - `qa_tests.yaml` now also builds a dedicated `qa-evidence` artifact (`qa_evidence.md` + `qa_evidence.json`) and appends the same summary to the workflow job summary for go-live sign-off linking.

## Next Coverage Expansion

1. Deepen shipping mutation assertions (queue status transitions, tracking updates, label artifact persistence)
2. Deepen sync mutation assertions (retry-failed flow, queue state transitions, unresolved-error resolution)
3. Extend admin governance assertions (evidence pack artifact checks, sign-off row CRUD)

## Quality Gates (Target)

- Unit tests required pass on pull requests and `main`.
- Playwright smoke required pass on pull requests and `main`.
- Enforce coverage minimums with ratcheting:
  - Global Python line gate (current enforced: `>=30%`; next target: `>=40%`)
  - Scoped-core gate (current enforced: `>=85%` on repository/services/auth/page_common/config; next target: `>=95%`)
  - Critical-flow Playwright scenario count threshold (current baseline: `9` chromium specs)

## Execution Commands

```bash
python -m unittest discover -s tests -p "test_*.py"
python scripts/run_test_suites.py --suite fast
python scripts/run_test_suites.py --suite integration
npm install
npx playwright install chromium
npx playwright test
```
