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
- E2E smoke/critical flows: Playwright Chromium (`tests/e2e/smoke.spec.ts`, `tests/e2e/auth_flow.spec.ts`, `tests/e2e/intake_wizard.spec.ts`, `tests/e2e/coin_intake_wizard.spec.ts`, `tests/e2e/intake_prefill.spec.ts`, `tests/e2e/lifecycle_archive_flow.spec.ts`, `tests/e2e/products_flow.spec.ts`, `tests/e2e/listings_flow.spec.ts`, `tests/e2e/shipping_flow.spec.ts`, `tests/e2e/sync_flow.spec.ts`, `tests/e2e/admin_golive_flow.spec.ts`, `tests/e2e/admin_lifecycle_retention_flow.spec.ts`)
- Listing Wizard flow now also asserts create-draft direct-post feedback handling (skip/fail branch messaging) and post-create link visibility (`Open Listings`) in `tests/e2e/listing_wizard_flow.spec.ts`.
- Strict seeded listing gate (opt-in): `tests/e2e/listings_flow_strict.spec.ts` (`E2E_STRICT_LISTINGS=1`, `PLAYWRIGHT_REQUIRE_SEED=1`)
  - Includes publish-draft roundtrip checks (`Save Publish Draft`, `Resume Publish Draft`, `Clear Publish Draft`) for deterministic DB-backed state restore/clear behavior.
  - Latest strict seeded evidence run (`2026-04-16`): `2 passed`.
- Strict seeded lifecycle gate (opt-in): `tests/e2e/lifecycle_archive_flow_strict.spec.ts` (`E2E_STRICT_LIFECYCLE=1`, `PLAYWRIGHT_REQUIRE_SEED=1`)
  - Covers Listings lifecycle danger-zone archive/restore transition flow under strict assertions.
  - Latest strict seeded evidence run (`2026-04-16`): `1 passed`.
- Strict seeded lifecycle entities gate (opt-in): `tests/e2e/lifecycle_entities_strict.spec.ts` (`E2E_STRICT_LIFECYCLE=1`, `PLAYWRIGHT_REQUIRE_SEED=1`)
  - Covers deterministic archive/restore roundtrips for Products, Lots, and Media under seeded strict auth.
  - Latest strict seeded evidence run (`2026-04-16`): `3 passed`.
- CI:
  - `docker_build.yaml` runs static compile + unit tests before image publish.
- `qa_tests.yaml` runs unit tests + Playwright smoke against compose app stack and uploads artifacts.
- `qa_tests.yaml` supports optional strict listings gate via `QA_ENABLE_STRICT_LISTINGS=1`.
- `qa_tests.yaml` runs strict lifecycle gates by default on PR/push (`QA_ENABLE_STRICT_LIFECYCLE=1`) and allows workflow-dispatch override via `qa_enable_strict_lifecycle`.
  - Includes Listings strict lifecycle gate, lifecycle entities strict gate, and Admin lifecycle retention evidence flow.
- `qa_tests.yaml` now also builds a dedicated `qa-evidence` artifact (`qa_evidence.md` + `qa_evidence.json`) and appends the same summary to the workflow job summary for go-live sign-off linking.
  - Coverage gate trend evidence is now included per run via `qa-evidence/coverage_gates.json` (global/scoped thresholds + pass/fail snapshot) for ratchet tracking.
  - Segmented suite manifests are now exported per run (`qa-evidence/suite_fast_manifest.json`, `qa-evidence/suite_integration_manifest.json`) to keep fast/integration split deterministic for CI fan-out planning.

## Playwright Auth Baseline

- Local defaults:
  - `E2E_USERNAME=e2e`
  - `E2E_PASSWORD=e2e-password-123`
- Admin go-live flow credentials:
  - `E2E_ADMIN_USERNAME` / `E2E_ADMIN_PASSWORD`
  - local fallback: `admin` / `e2e-password-123`
- Local seed keeps deterministic browser-test users available (`e2e` and `admin`) for non-prod test runs.
- `admin_golive_flow.spec.ts` now enforces deterministic admin sign-in and hard assertions for go-live evidence surfaces (no skip-safe fallback in normal local runs).
- Latest local browser-suite evidence (`2026-04-09`): `9 passed / 0 skipped`.

## Next Coverage Expansion

1. Deepen shipping mutation assertions (queue status transitions, tracking updates, label artifact persistence)
   - Latest increment: `shipping_flow.spec.ts` now exercises label-job queue mutation (`Queue Label Purchase Jobs` + `Process Due Shipping Jobs`) and tracking writeback verification when queue rows are available.
   - Follow-on increment: added deterministic carrier-preset create/save assertion so shipping write-path coverage remains valid even when queue rows are empty.
2. Deepen sync mutation assertions (retry-failed flow, queue state transitions, unresolved-error resolution)
   - Latest increment: `sync_flow.spec.ts` now creates a failed manual run and validates retry-run creation; also attempts unresolved exception resolution path when queue rows exist.
   - Follow-on increment: added deterministic run-detail mutation assertion (`Update Sync Run` counter updates) independent of exception queue population.
3. Continue extending admin governance assertions (evidence pack artifact checks, sign-off row CRUD)
   - Latest increment: admin go-live e2e now asserts lifecycle retention sign-off tracker + lifecycle CSV action visibility.
   - Follow-on increment: admin go-live e2e now writes one `Go-Live Section Sign-Off` record and verifies success + table visibility for deterministic mutation coverage.
   - Added unit regression guard that checks lifecycle evidence-pack artifact filename wiring remains present in Admin source.
4. Add Listing Wizard direct-post e2e assertions (preflight pass path, `Post to eBay Immediately` result handling, created-draft link visibility)
   - Latest increment: intake wizard e2e suites now include `Purchased On eBay` regression assertions (hidden/visible eBay fields + required `eBay Purchase Item ID` validation) in both generic and coin flows.
   - Latest increment: `tests/e2e/intake_prefill.spec.ts` now includes Inventory Intake `Run Grader` path assertion that verifies `AI Grading Description` is populated after successful grader execution.
   - Follow-on increment: added unit regression for inventory intake grader sparse-structured fallback (`tests/test_intake_wizard_helpers.py`) to ensure `Run Grader` still fills `AI Grading Description`.
5. Add Listings publish-mode e2e assertions (`Publish Live Listing` vs `Create Offer Draft Only`) including category-suggestion apply flow
   - Latest increment: `tests/e2e/listings_flow.spec.ts` now asserts mode-switch behavior for `eBay Post Mode` (`Publish Live Listing` <-> `Save Unpublished Offer (API Draft)`) prior to preflight.
   - Latest increment: same flow now exercises category assist fetch/apply path (`Load eBay Category Assist` -> `Fetch Category Suggestions` -> `Apply Selected Category`) and verifies category state writeback, with environment-safe skip when taxonomy fetch is unavailable.
6. Add workflow-state e2e assertions (`Save/Resume/Clear`) for Listing Wizard and Listings publish/edit with rerun-safe continuity checks
   - Listings strict seeded roundtrip path is now implemented; continue with Listing Wizard strict roundtrip and cross-page resume assertions.

## Quality Gates (Target)

- Unit tests required pass on pull requests and `main`.
- Playwright smoke required pass on pull requests and `main`.
- Enforce coverage minimums with ratcheting:
  - Global Python line gate (current enforced: `>=38%`; next target: `>=40%`)
  - Scoped-core gate (current enforced: `>=88%` on repository/services/auth/page_common/config; next target: `>=95%`)
  - Critical-flow Playwright scenario count threshold (current baseline: `14` chromium specs)

## Execution Commands

```bash
python -m unittest discover -s tests -p "test_*.py"
python scripts/run_test_suites.py --suite fast
python scripts/run_test_suites.py --suite integration
npm install
npx playwright install chromium
npm run test:e2e
npm run test:e2e:listings:strict
npm run test:e2e:lifecycle:strict
npm run test:e2e:lifecycle:entities:strict
npm run test:e2e:admin:lifecycle-retention
```

### Performance Guardrail Focus (GS‑V10‑018)

```bash
PYTHONPATH=. pytest -q tests/test_reports_helpers.py tests/test_listings_helpers.py tests/test_system_health_helpers.py
```

This focused suite validates:
- bounded report rendering semantics and facilitator-channel tax-scope defaults,
- optimized listings helper paths (photo-comp lineage extraction and owner-map collision lookup),
- system-health page/read baseline normalization + summary semantics.

Notes:
- Prefer `npm run test:e2e` (or `npm run test:e2e -- <spec>`) so the wrapper unsets conflicting terminal color env vars before Playwright launches.
