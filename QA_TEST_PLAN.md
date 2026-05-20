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
   - Latest unit/helper increment: eBay category item-specific requirements now have service/repository/readiness coverage (`tests/test_ebay_aspects.py`, `tests/test_ebay_service.py`, `tests/test_repository_ebay_category_aspects.py`, `tests/test_listing_readiness.py`) and view-helper coverage for primary image ordering plus publish/revise/direct-post metadata persistence (`tests/test_listing_wizard_helpers.py`, `tests/test_listings_helpers.py`).
   - Latest preflight increment: eBay dependency preflight blocks immediate-payment auction-policy mismatches without an `Auction Buy It Now` price before calling `publishOffer` (`tests/test_ebay_service.py`).
   - Latest publish resilience increment: eBay publish retries `25604 Product not found` after probing the SKU inventory item to handle Inventory API eventual consistency after inventory upsert/offer creation (`tests/test_ebay_service.py`).
   - Latest media resilience increment: EPS image hosting retry/failure handling is covered so Listing Wizard direct post and Listings publish/revise retry transient eBay Media API failures, use URL import only as an EPS-hosting path, and fail before inventory publish instead of sending self-hosted image URLs (`tests/test_listing_wizard_helpers.py`, `tests/test_listings_helpers.py`).
   - Latest video resilience increment: Listing Wizard and Listings helper coverage now exercises supported-video selection, MOV-to-MP4 conversion, eBay video upload headers, Inventory `videoIds` verification, Trading API `GetItem` live-listing video verification, stale `None` video-selection recovery, and non-blocking image-only warnings when requested video media is missing/unsupported (`tests/test_listing_wizard_helpers.py`, `tests/test_listings_helpers.py`, `tests/test_ebay_service.py`).
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

### Accounting/Profitability Guardrail Focus (GS-V10-020)

```bash
PYTHONPATH=. pytest -q tests/test_repository_inventory_movements.py tests/test_reports_helpers.py tests/test_reports_fee_reconciliation.py tests/test_chat_context_builders.py tests/test_reports_view.py
PYTHONPATH=. pytest -q tests/test_views_small.py::SmallViewsTests::test_render_dashboard
```

This focused suite validates:
- dashboard and Reports use the same profit conventions: before-return profit is `gross + shipping charged - fees - label spend - COGS`; estimated profit after returns is `profit before returns - return refunds + returned COGS reversal`,
- dashboard and Reports use the same sale-window actual-economics basis for marketplace fees, label spend, and shipping delta, including linked normalized finance entries, unlinked-entry guardrails, and `unmatched_shipping_label_finance_entry` accounting exceptions,
- Ask/AI sales and reports snapshots use the same repository actual-economics basis before falling back to raw sale fields,
- Dashboard all-time net sales and Sales table rows expose repository actual-economics fee/label/net values and source fields, and Reports COGS/margin rows use actual net before COGS for primary margin math when available,
- marketplace reconciliation rows use repository actual-economics rows for fee, shipping, label spend, net before returns, and net after returns,
- inventory-cycle analytics use repository actual-economics rows for cycle fees, shipping, label spend, net sales, and margin versus known acquisition cost,
- Reports `Sales` export rows expose raw field net plus actual fee, shipping charged, label spend, net before COGS, and source columns,
- Reports `QuickBooks Sales Export` exposes FIFO COGS estimate, `profit_before_returns_estimate`, legacy `gross_margin_estimate`, actual-economics fee/label sources, and `cogs_source` provenance,
- Reports `QuickBooks Refund/Adjustment Export` exposes refund cash impact, returned COGS estimate, restocked COGS reversal estimate, COGS source, and estimated profit impact,
- Accounting Close Readiness applies restocked return COGS reversal totals to net-after-returns-and-COGS and exposes the return profit impact,
- estimated tax detail rows use actual-economics allocated buyer shipping when available and expose raw sale-field shipping plus source evidence columns,
- Reports `Orders` export rows expose raw order fee/label fields plus normalized actual fee, actual label spend, actual shipping delta, actual net before COGS, and source columns,
- Orders workspace table rows and Orders Copilot context expose normalized actual fee/label/net/source fields while retaining raw order fields, and raw fallback uses the canonical order net helper with `actual_net_source` provenance,
- Reports shipping-economics detail/summary use the same repository sale actual-economics allocation as dashboard/profit rows, including gross-weighted multi-line order shipping and normalized label spend,
- Purchase Lot `Lot P/L Snapshot (Estimated)` uses repository actual-economics rows for product sales net/profit attribution when normalized order fees/labels exist,
- Purchase Lot `Lot P/L Snapshot (Estimated)` uses FIFO lot consumption for repurchased products and exposes sold COGS source totals for the selected lot,
- legacy in-memory Reports inventory-cycle fallback uses repository actual-economics rows for fee, shipping, label spend, and net calculations when available,
- legacy in-memory marketplace reconciliation fallback uses repository actual-economics rows for normalized fee, shipping, label spend, net-before-returns, and net-after-returns calculations when available,
- Reports `Sales Detail` dataframe/export preserves raw field net while exposing actual fee, shipping charged, label spend, actual net before COGS, source fields, and actual-backed `net_sales`,
- Dashboard no-rollup fallback uses repository actual-economics rows for 7d/30d net, fee, shipping, label spend, shipping delta, and estimated profit when live metrics are unavailable,
- standalone eBay fee reconciliation fallback prefers normalized `order_finance_entries` marketplace-fee sums from `order.finance_entries` before notes-derived fee breakdowns or sale fee fields,
- daily Slack ops report 24h gross/net uses repository actual-economics rows when available and canonical shipping economics as raw fallback,
- Reports accounting exception rows use normalized actual fee evidence before raw `sales.fees` for missing-fee and margin checks,
- Dashboard live metrics use repository actual-economics rows for 7-day net sales as well as 30-day net/profit,
- Dashboard live metrics and no-rollup fallback rendering exclude future-dated sales/orders from current 7-day/30-day gross/net/COGS/profit/order windows,
- Dashboard rollup explain/baseline diagnostics use the same snapshot end bound as dashboard live metrics,
- Admin manual business status report context uses repository actual-economics rows for gross, fees, buyer shipping, label spend, and net before raw sale-field fallback,
- Ask/AI accounting context fallback paths roll back failed actual-economics and FIFO cost-map lookups before continuing with sale/product field fallback,
- lot-only cost basis for products without direct product cost,
- mixed explicit/blank product-lot allocations without double-counting lot totals,
- mixed-value product-lot allocation weights for bulk lots containing different product values,
- Lot Assignment exports expose resolved landed cost and allocation `cost_source`,
- Accounting Review/close packet Lot Allocation Source Summary groups allocation basis by `cost_source`,
- close-readiness checks warn on equal fallback or missing lot cost allocation basis,
- accounting exceptions for multi-product blank-cost lots still relying on equal fallback allocation,
- partial purchase-lot check-ins with `expected_total_quantity` so early assignments do not absorb full lot cost,
- dashboard COGS source provenance for partial-lot sales so estimated profit identifies the allocation basis being used,
- dashboard estimated-profit warnings for equal-fallback, mixed, or missing COGS basis so under-defined partial lots are visible before profit is trusted,
- Reports COGS & Margin Detail export/display provenance with `fifo_cost_source` and `lot_cost_source`,
- Reports sold COGS source summary rollups by FIFO source for close-review packet evidence,
- Accounting Close Readiness warnings for sold equal-fallback COGS and missing/unknown sold COGS basis,
- Ask/AI Reports snapshot sold COGS source mix text and citation metadata,
- Daily Slack report 24-hour estimated COGS and COGS source mix context/event details,
- Admin manual business status report estimated COGS and COGS source mix context,
- multi-lot product repurchases with time-aware FIFO, including no future-lot consumption and FIFO remaining inventory value,
- product landed acquisition cost and `product_cost` fallback behavior,
- sale FIFO cost maps, FIFO-aware Ask/AI accounting snapshots, normalized-finance actual economics allocation and QBO staging exports, full-refund close-readiness impact, fee reconciliation rollups, Reports accounting exception queue coverage, close-readiness classification, and Accounting Close Packet ZIP contents.
- Reports Accounting Close Formula Checks validate profit before returns, return profit impact, and estimated profit after returns arithmetic, feed formula warnings into close readiness, and are included in close packets plus AI Accountant context/citations.
- Reports Accounting Sales Component Checks validate Sales Detail count/gross/net against COGS & Margin Detail and recompute actual net before COGS from gross, shipping charged, fees, and label spend; component warnings feed close readiness and AI Accountant evidence.
- Reports Accounting Return Tie-Out Checks validate Returns refund totals, QBO refund/adjustment staging, return COGS reversals, and staged return profit impact; return tie-out warnings feed close readiness and AI Accountant evidence.
- Reports Accounting Inventory Valuation Checks validate stocked inventory landed-cost coverage, Inventory Snapshot value formulas, and close-readiness inventory value tie-out; valuation warnings feed close readiness and AI Accountant evidence.
- Reports Accounting Fee Evidence Checks validate eBay Fee Reconciliation row/fee totals and Fee Source Priority rows against Sales Detail, while flagging sale-field fee fallback rows; fee-evidence warnings feed close readiness and AI Accountant evidence.
- Reports Accounting Shipping Evidence Checks validate Sales Detail shipping charged/label spend against Shipping Economics detail and summary rows, verify shipping delta formulas, and flag paid-shipping rows missing label spend; shipping-evidence warnings feed close readiness and AI Accountant evidence.
- Reports Accounting Reconciliation Tie-Out Checks validate marketplace reconciliation sales/return counts and totals against Sales Detail, Returns, net-after-return formulas, and close reconciliation flags; reconciliation warnings feed close readiness and AI Accountant evidence.
- Reports Accounting COGS Source Checks validate Sold COGS Source Summary sale count, quantity, FIFO COGS, and profit before returns against COGS & Margin Detail and close readiness, while flagging sold equal-fallback or missing/unknown-basis COGS.
- Reports Accounting Lot Allocation Checks validate Lot Allocation Source Summary assignment count, quantity, and resolved landed cost against Lot Assignment detail and close readiness, while flagging equal-fallback or missing/unknown-basis assignments.
- Reports Accounting Exception Queue Checks validate Accounting Exception Queue total/P0/P1 counts against close readiness, flag malformed exception rows, and keep P0 exceptions visible as close blockers.
- Reports Accounting Margin Anomaly Checks validate negative/nonpositive COGS & Margin rows against close readiness and `nonpositive_margin` exception evidence, while keeping unresolved margin anomalies visible as close blockers.
- Reports Accounting Close Consistency Checks validate final close-readiness status, blocker/warning counts, blocker/warning text, and close-check fail/warn rows before relying on sign-off evidence.
- Reports Accounting Close Packet Completeness Checks validate required close-packet evidence artifacts are present and populated before relying on sign-off evidence.
- Reports Accounting Close Packet Completeness Checks require QuickBooks Refund/Adjustment export evidence when return/refund activity exists, and optional no-activity artifacts do not warn solely because they are absent.
- Reports Accounting Close Packet Manifest Checks validate selected close-packet prefixes and manifest row-count values against exported report dataframes.
- Reports Accounting Close Packet Hash Checks validate SHA-256 CSV hash evidence is present for selected close-packet artifacts and included in the packet manifest.
- Accounting Close Packet manifests include stable `accounting_close_packet_evidence_hash_sha256` values derived from selected close CSV payloads, date range, and close summary.
- Accounting Close Sign-Off Review validates approved sign-off packet hashes against recalculated Accounting Close Packet evidence hashes, flagging stale packet evidence.
- Reports Accounting Close Packet Evidence Hash table exposes the current packet evidence hash for reviewer copy/paste into Admin sign-off evidence.
- Accounting Close Sign-Off Review warns when approved close sign-offs have packet references but no matching packet evidence hash.
- Accounting Close Sign-Off Review compares approved sign-off exception counts against recalculated close total exceptions.
- Accounting Close Sign-Off Review warns when approved close sign-offs are missing owner or sign-off date evidence.
- Accounting Close Sign-Off Review validates approved sign-off dates are parseable, not before period end, and not future-dated.
- Accounting Formula Checks validate close-summary shipping delta from shipping charged minus label spend before close-ready status can be trusted.
- Accounting Formula Checks validate close-summary net before COGS from gross sales plus shipping charged minus fees and label spend before close-ready status can be trusted.
- Accounting Period Drift Checks compare close-readiness shipping charged, label spend, and shipping delta totals against Dashboard Live Metrics 30-day shipping component values.
- Accounting Period Drift Checks compare close-readiness fee totals from Fee Source Priority evidence against Dashboard Live Metrics 30-day fee totals.
- Accounting Period Drift Checks validate Dashboard Live Metrics 30-day net, shipping delta, profit before returns, and estimated profit after returns formulas from dashboard component totals.
- Accounting Period Drift Checks validate QuickBooks Sales Export net and `profit_before_returns_estimate` formulas from QBO staging component totals, with `gross_margin_estimate` retained only as a legacy compatibility alias.
- Accounting Period Drift Checks validate QuickBooks Refund/Adjustment Export estimated profit impact from refund components and COGS reversal.
- Accounting Period Drift Checks validate Slack-style and Ask/AI accounting snapshot `profit_before_returns` formulas from net minus COGS and tie out `estimated_profit_after_returns` separately.
- Order Map helper/view coverage validates offline destination aggregation, state-name normalization, marketplace/status filtering, destination marketplace/status breakdowns, shipped-order default filtering, unshipped-order opt-in behavior, map rendering, CSV export, and destination table output without network geocoding.
- Admin governance artifact wiring for accounting close sign-offs (`accounting_close_signoffs.csv`) in the Go-Live Evidence Pack.
- Reports Accounting Close Packet and AI Accountant context include latest accounting close sign-off rows (`accounting_close_signoffs.csv`) for owner/date/packet evidence review.
- Reports Accounting Close Packet and AI Accountant context include accounting close sign-off review rows (`accounting_close_signoff_review.csv`) that compare approved sign-offs to recalculated readiness, blocker count, drift warning count, and packet/evidence references.
- Reports in-page Accounting Close Sign-Off capture writes `accounting_close_signoff` audit evidence with owner/date/status, packet ref, evidence link, recalculated readiness/blocker/exception/drift counts, AI review follow-up count, and current packet evidence hash.
- role-gated AI Accountant routing in Ask GoldenStackers (`ai_accountant_use`), accounting snapshot citations, write-intent guardrails, Reports AI Accountant review import/render coverage, and Reports accountant audit-event logging.
- dedicated AI Accountant workspace helper coverage for monitor row generation, severity summaries, in-app audit message payloads, and Slack notification-outbox payload/dedupe behavior.
- scheduled AI Accountant monitor coverage for actionable severity filtering, in-app audit message recording, Slack notification-outbox queuing, and sync-runner due/disabled paths.
- Reports AI Accountant context/audit citations include tax review assumptions, selected tax profile evidence, and tax reporting sign-off evidence while preserving advisory-only tax/legal guardrails.
- Reports Tax Review Packet workflow records Tax Reporting Sign-Off audit evidence with packet hash, advisor evidence, owner/date/status, profile key, jurisdiction, and tax exception count.
- Reports Tax Reporting Sign-Off Review flags stale approved tax sign-offs when recalculated jurisdiction/profile, tax exception count, owner/date, advisor evidence, packet reference, or packet hash no longer match.
- Reports Tax Reporting Sign-Off Review exports in Tax Review and Accounting Close packets and appears in AI Accountant context/citations.
- Reports AI Accountant audit events include explicit `tax` domain scope when tax profile/sign-off evidence is cited.
- Reports Copilot context includes tax review assumptions, selected tax profile evidence, tax reporting sign-off rows, and Tax Reporting Sign-Off Review rows.
- Reports Copilot prompt asks for explicit `tax_review_findings` output from tax packet/profile/sign-off evidence.
- Reports Copilot renders structured JSON sections as readable bullets, including Tax Review Findings, while preserving raw JSON for audit/debug review.
- Reports Copilot review runs write read-only `reports_copilot_review` audit events with deterministic prompt/data-scope hashes, packet evidence hashes, and cited tax/accounting row counts.
- Reports AI Accountant renders structured close/profit/lot/fee/action/tax-guardrail JSON sections as readable bullets while preserving raw JSON for audit/debug review.
- Reports Copilot/AI Accountant section rendering accepts fenced or prefaced JSON responses so tax/accounting findings remain visible when model output includes wrappers.
- Reports AI Accountant audit events include deterministic prompt/data-scope hashes, packet evidence hashes, context keys, and compact cited row-count scope metadata.
- Reports Copilot and AI Accountant accepted/edited/rejected feedback actions write outcome audit events tied to the response hash and original review metadata.
- Reports AI Review Outcome Evidence is extracted from audit logs, exported in the Accounting Close Packet, and passed into AI Accountant context/citations.
- Accounting Close Readiness blocks close-ready status when the latest Copilot/AI Accountant outcome per review type is `edited` or `rejected`.
- Accounting Close Sign-Off evidence captures `ai_review_followup_count`, and Sign-Off Review warns when approved sign-offs no longer match recalculated AI review follow-up blockers.
- Products page full-width `Product Detail/Edit` layout is covered by product helper/import smoke checks and should remain usable for accounting cleanup workflows involving product cost fields, lot assignments, conversions, media, lifecycle state, and repurchases.

Next validation targets:
- close-period drift check comparing dashboard live metrics, Reports close packet totals, QBO staging exports, Slack report context, and Ask/AI accounting snapshots for one shared window,
  - First implemented drift coverage compares Accounting Close Readiness totals against QBO sales/refund staging exports and verifies pass/warn behavior.
  - Dashboard drift coverage compares close-readiness totals against Dashboard Live Metrics 30-day values when the Reports window matches the dashboard 30-day window.
  - Slack drift coverage compares close-readiness totals against Slack-style daily/weekly business summary values when the Reports window matches those summary windows.
  - Ask/AI accounting drift coverage compares close-readiness totals against Ask/AI accounting snapshot 30-day values when the Reports window matches the AI/dashboard 30-day window.
  - AI review drift coverage verifies Reports AI Accountant context and audit citations include the close-period drift-check table.
- production-sample close review with Accounting Close Packet artifact reference and Reports/Admin Accounting Close Sign-Off evidence, verifying the exported packet includes the resulting `accounting_close_signoffs.csv` row,
- production-sample tax profile/sign-off evidence review using the Tax Review Packet, Reports/Admin Tax Reporting Sign-Off evidence, Tax Reporting Sign-Off Review, Tax Profile tracker, and AI Accountant tax-evidence citations.

### Tax Reporting + Guidance Focus (GS-V10-021)

Planned validation scope:
- tax review workspace aligns Tax Summary, Tax by Marketplace, Tax Detail, Tax Drilldown, Documents handoff, marketplace-facilitator exclusions, exempt-category treatment, and shipping-taxability assumptions,
- first-pass `Tax Exceptions / Advisor Review` rows flag missing jurisdiction/rate/category evidence, facilitator channels included in local tax scope, exempt-category review needs, and taxable shipping on exempt items,
- tax review packet exports include manifest, stable SHA-256 evidence hash, Tax Summary, Tax by Marketplace, Tax Detail, Tax Exceptions / Advisor Review, facilitator treatment, jurisdiction/rate assumptions, exemption basis, and advisor notes,
- tax exception checks flag missing jurisdiction, missing/ambiguous tax mode, missing exemption basis, facilitator mismatch, taxable shipping conflicts, marketplace-collected tax mismatch, and bullion/coin exemption review needs,
- saved tax profiles include jurisdiction/channel rules, effective dates, evidence notes, human-validation status, CSV export, and Go-Live Evidence Pack artifact coverage,
- Reports tax review can apply saved profile assumptions and includes selected profile metadata in Tax Review Packet evidence,
- tax sign-off records capture period, jurisdiction, owner, advisor/evidence reference, status, exception count, packet/export reference, CSV export, Go-Live Evidence Pack artifact coverage, and Tax Review/Accounting Close packet inclusion,
- AI Tax/Accounting guidance remains read-only and advisory, cites source rows/profiles/sign-offs, labels estimates versus marketplace-collected/remitted amounts, blocks unsupported filing/legal conclusions, and records audit metadata.

### eBay Sync Network Resilience Focus

```bash
PYTHONPATH=. pytest -q tests/test_sync_jobs.py tests/test_sync_runner.py tests/test_ebay_health.py
```

This focused suite validates eBay OAuth refresh, connection health, and orders pull/import distinguish transient DNS/network failures from auth/data failures.

Notes:
- Prefer `npm run test:e2e` (or `npm run test:e2e -- <spec>`) so the wrapper unsets conflicting terminal color env vars before Playwright launches.
