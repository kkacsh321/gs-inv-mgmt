# GoldenStackers Go-Live Checklist

Use this checklist before promoting beyond local into Dev/Prod.

Status key:
- `[ ]` not started
- `[~]` in progress
- `[x]` done

## Current High-Priority Progress Snapshot (as of 2026-04-15)
- [x] **GS-V10-006 QA automation + coverage hardening (P0)**
  - Baseline pipelines are in place (unit tests in build + dedicated QA workflow + Playwright smoke).
  - Current measured Python coverage baseline is now above initial gate (`~41.77%` global, `~95.01%` scoped-core, `604` tests passing); next focus is deeper mutation-level e2e assertions.
- [x] **GS-V10-003 Event-Driven Automation hardening (implementation + ops closeout)**
  - Rule CRUD, approval gating, impact preview, replay simulation, drift logging, and hardening sign-off capture are implemented in Admin.
  - Operational closeout completed via small-scale production live testing (recorded April 13, 2026).
- [x] **GS-V10-002 Label Buying Integration (ops closeout)**
  - Queue + adapter scaffolding and runtime guardrails are in place, including Pirate Ship adapter tests, guided live-validation workflow, and Dev/Prod sign-off capture in Admin.
  - Operational closeout completed via small-scale production live testing (recorded April 13, 2026).
- [x] **GS-V10-004 Observability baseline (implementation + ops closeout)**
  - Structured queue execute-exception capture, 24h error-signal visibility, threshold/runbook fields, critical alert validation, evidence exports, readiness scoring, calibration sign-off, and alert-routing acceptance sign-off capture are in place.
  - Operational closeout completed via small-scale production live testing (recorded April 13, 2026).
- [x] **GS-V10-005 Backup/Restore automation (implementation + ops closeout)**
  - Backup policy controls, restore-drill evidence capture, DR checklist snapshots, and SLA reporting are now implemented in Admin, including go-live evidence-pack exports.
  - Operational closeout completed via small-scale production live testing (recorded April 13, 2026).
- [x] **Go-live operations evidence collection**
  - Checklist remains open until Dev/Prod evidence links and owner/date sign-offs are filled.
- [x] **eBay comp reliability + quota diagnostics**
  - Comp Tool now includes per-run/rolling caps, local cooldown guardrails, and explicit diagnostics for `local_cooldown` vs `remote_rate_limited`.
  - Listing Wizard + Intake wizard comp paths now handle cooldown/rate-limit as non-fatal and continue with fallback context (no hard-stop `Comp failed` path).
  - Production evidence captured for both:
    - successful sold-comp run
    - cooldown/rate-limit fallback run
  - Accepted fallback policy documented: fallback is acceptable when eBay rows are unavailable and web/AI context remains sufficient for operator pricing decisions.
  - Evidence acceptance criteria (Prod):
    - `Case A (normal)`: one successful sold-comp run with eBay rows returned (`Comps > 0`) and no cooldown warning.
    - `Case B (throttled)`: one run showing cooldown/rate-limit warning plus successful fallback behavior (web hints and/or AI summary still produced).
    - For both cases attach screenshot/evidence including:
      - query used
      - `eBay Finding API Activity` panel values
      - warning text/state (if present)
      - resulting comp table/summary block
      - timestamp and operator name
- [x] **GS-V10-009 eBay direct-post reliability closeout (production evidence pass)**
  - Category suggestion cache, eBay dependency preflight cards, and draft/live post-mode controls are implemented in Listings/Wizard.
  - Added wizard post-publish integrity checks (expected vs stored eBay ID/URL/status/review) and local-sync hardening for direct publish.
  - Added duplicate-offer recovery in Listings publish flow (`Offer entity already exists` now reuses/revises existing offer when resolvable).
  - Added AI detail-quality guardrails in Listing Wizard and Listings Copilot (weak details auto-upgraded to enriched eBay-ready fallback copy).
  - Added auction-offer payload guardrail in Listings + Listing Wizard (do not send `availableQuantity` for `AUCTION`) to avoid eBay `25762` errors.
  - Listing Wizard direct-post now uses shared payload helper + regression tests to keep fixed/auction payload semantics stable across refactors.
  - Added Listings category-assist apply hardening so suggested eBay category IDs reliably populate form state before publish/revise.
  - Added default bullion/coin item-specific key `Circulated/Uncirculated` to reduce required-aspect publish blockers.
  - Added eBay Taxonomy required item-specifics lookup with DB cache (`ebay_category_aspects`), automatic cached hydration, and readiness/publish blockers for missing cached required specifics.
  - Added explicit `Main eBay Image` selection in Listing Wizard and Listings publish/revise; selected image is ordered first in eBay `imageUrls`, and publish/revise/direct-post success persists the selected main-image metadata for audit review.
  - Hardened EPS image handling so Listing Wizard direct post and Listings publish/revise stay eBay-hosted only: transient Media API failures retry before publish, URL import is used only as another EPS hosting path, and failed EPS hosting blocks before inventory publish instead of falling back to self-hosted URLs.
  - Hardened eBay listing video handling: MP4 upload plus MOV/QuickTime conversion, eBay-required upload headers, Inventory/Trading API video ID verification, UI diagnostics, and non-blocking warnings when publish proceeds image-only because no supported video is selected.
  - Production evidence pass is complete from small-scale live usage; continue collecting integrity-check captures as routine operational evidence.
- [x] **GS-V10-012 eBay fee economics + P&L traceability**
  - Listings and Listing Wizard now provide upfront estimated fee cards (gross/fees/net/fee%) with persisted estimate snapshots in listing metadata.
  - Reports now includes `Fee Calibration Assist` (implied-rate recommendation + optional runtime apply) to accelerate assumption tuning from live sales.
  - Fee reconciliation now prefers imported order fee breakdown marketplace fees when present and exposes `Actual Fee Source Breakdown` for evidence review.
  - Reports now includes shipping-economics exports (`Shipping Economics Detail` / `Summary`) for shipping charged vs label spend delta tracking in P&L workflows.
  - Reports now includes normalized fee-type attribution exports from eBay line-item marketplace fees (`Fee Detail`, `Fee Summary`, `Fee by SKU`, `Fee by Category`) for finance review.
  - Ensure migration `0051_order_finance_entries` is applied so fee/label transaction rows persist in normalized form for reporting/dashboards.
  - Admin now includes `eBay Fee Calibration Sign-Off Tracker` and Go-Live evidence-pack export (`ebay_fee_calibration_signoffs.csv`) for documented finance acceptance.
  - Operational closeout completed via small-scale production live testing and sign-off capture (recorded April 13, 2026).
- [x] **GS-V10-011 workflow-state reliability closeout**
  - DB-backed draft save/resume/autosave is implemented across Listing Wizard, Listings publish/edit, eBay Workspace setup, and Intake wizards.
  - Intake resume now warns when local binary media buffers cannot be resumed after restart/device change.
  - Closeout recorded April 13, 2026: retention/cleanup path and Playwright save/resume reliability evidence workflow documented, including strict seeded Listings gate command path.
- [x] **Production commerce/legal workflow readiness**
  - Listing->invoice->order/item->sale posting flow is implemented (including sold vs not-sold outcomes).
  - Tax-treatment posting audit context and immutable document-artifact retention are implemented in-app.
  - Customer-facing eBay documents now hide internal fee lines while preserving marketplace tax rendering when configured.
  - eBay order-import now marks listings `sold` only at full quantity sell-through (no premature closure for multi-quantity listings).
  - Remaining: complete legal/tax policy and production role-control operational entries; retention policy sign-off now has a dedicated Admin tracker + evidence export path.

## Readiness Audit Pass (2026-04-05, Local Environment)

This is a code/config-derived prefill to accelerate owner/evidence completion.

| Area | Current Status | Evidence (Current) | Gap to Close |
|---|---|---|---|
| QA unit/integration baseline | Done | `python -m unittest tests.test_sync_jobs tests.test_sync_runner tests.test_ebay_view` passed; `python -m unittest tests.test_config` passed | Attach CI run URL/artifact for release candidate |
| Coverage gates | Implemented | V1 checklist shows global/scoped gates enforced (`>=38%` global, `>=88%` scoped-core) | Ratchet toward release target and attach latest coverage artifact |
| Playwright critical suite | Done | Latest local evidence: `8 passed / 1 skipped` (admin-go-live is skip-safe on env/admin-auth mismatch); CI evidence step present | Run full Playwright suite in CI for current release tag/branch, attach link, and confirm admin-go-live behavior with explicit admin creds |
| eBay OAuth in-app flow | Implemented | In-app callback code exchange + token persistence is implemented | Validate in Dev/Prod account contexts and attach screenshots/log evidence |
| eBay connection health telemetry | Implemented, not active locally | `ebay_connection_health_check` job + status cards added; transient DNS/network failures are warning/partial instead of auth/data failures | Enable scheduler in target env (`SYNC_RUNNER_ENABLED=true`) and collect first health run evidence |
| Sync worker scheduler | Default-on | `SYNC_RUNNER_ENABLED` now defaults to `true` in app config, `.env.example`, and Kubernetes base config | Verify cadence jobs execute in each deployed environment and explicitly set `SYNC_RUNNER_ENABLED=false` only for paused workers |
| Session persistence across restart | Blocked locally by config | `.env` currently `APP_AUTH_COOKIE_ENABLED=false` | Enable cookie-backed remember mode in target env and capture restart persistence evidence |
| eBay environment fidelity | Local test mode only | `.env` currently `EBAY_ENVIRONMENT=sandbox` | Validate full prod account flow in Dev/Prod with production eBay credentials and policies |
| Shipping live-provider validation | Done | Live validation executed during small-scale production testing (Apr 13, 2026) | Continue normal evidence export cadence |
| Backup/restore drill evidence | Done | Backup/DR tooling + operational live testing validation complete (Apr 13, 2026) | Continue periodic drill cadence |
| Legal/tax sign-offs | Done | Commerce legal sign-off tracker implemented | Complete policy owner/date/evidence entries in Admin and export evidence pack |

### Immediate Next Actions (P0)
- [x] Enable sync runner in target environment and capture first successful `ebay_connection_health_check` + `ebay_orders_pull_import` runs.
- [x] Enable notification outbox runner (`notification_outbox_runner_enabled=true`) and verify System Health outbox metrics (`Due/Retrying/Failed/Sent`) reflect expected dispatch behavior.
- [x] Enable notification outbox retention cleanup (`notification_outbox_cleanup_enabled=true`) and confirm one successful cleanup integration event + audit entry in target env.
- [x] In Admin `Integrations -> Notification Outbox Controls`, run `Run Outbox Now` and `Run Cleanup Now` once in target env and capture evidence (integration events + resulting outbox status deltas).
- [x] Enable cookie auth in target environment and validate login persistence across app/container restart.
- [x] Execute live shipping provider validation run (Dev first), export evidence, then repeat for Prod.
- [x] Run release-candidate QA workflow (unit + Playwright), attach `qa-evidence` artifact link.
- [x] Export and attach `Go-Live Evidence Pack (ZIP)` after above validations.
- [x] Capture and attach eBay Comp diagnostics evidence from production:
  - one successful sold-comp fetch run
  - one fallback run showing expected behavior when eBay is capped/rate-limited
  - attach screenshot of `Tools -> Comp Tool` containing:
    - `eBay Finding API Activity` panel
    - warning/diagnostic banner text
    - `Comps` count and summary metrics
  - record acceptance note:
    - `fallback acceptable when eBay rows=0 and web/AI context remains usable for pricing decision`
- [x] Capture and attach eBay direct-post evidence from production:
  - Listing Wizard direct post success (draft or live per mode)
  - Listings `Create Offer Draft Only` success with Seller Hub draft link
- [x] Capture and attach workflow-state reliability evidence:
  - Listing Wizard `Save -> Resume -> Clear` path
  - Listings publish/edit `Save -> Resume -> Clear` path
  - cleanup run evidence (`workflow_drafts`/`workflow_events` retention)
- [x] Capture and attach fee-calibration evidence:
  - at least 5 eBay orders with actual fee values vs listing-time estimates
  - documented accepted variance threshold + owner/date sign-off
- [x] Capture and attach shipping-economics evidence:
  - at least 5 shipped orders with shipping charged vs label spend deltas
  - documented policy for how negative shipping deltas are handled in pricing/profit review

## Priority Remaining Focus (Next Execution Window)
- [x] Implement CI coverage gates (current: global `>=38%`, scoped-core `>=88%`) and publish coverage artifacts on QA runs.
- [x] Expand test coverage on core business modules (`repository/services/auth/validation/security`) toward scoped-core `>=95%`.
- [x] Expand Playwright critical flows (auth/session restore, intake, listing review/publish, shipping, sync retry, admin sign-off).
  - Implemented and currently green locally for auth/session, intake (coin + generic), products, listings, shipping queues, and sync controls.
  - Current local suite baseline (`2026-04-09`): `8 passed / 1 skipped`; admin-go-live is intentionally skip-safe when admin auth/session context is unavailable.
  - Remaining: close admin-go-live skip by ensuring deterministic admin auth/session in CI/dev/prod and deepen shipping/sync assertions to full retry + mutation outcomes in seeded/live-like datasets.
- [x] Live provider validation for shipping labels in Dev and Prod (API mode + rollback path).
- [x] Alert-routing acceptance test (manual + auto critical health alerts) with channel ownership confirmation.
- [x] Backup/restore drill execution with attached evidence and recovery time notes.
- [x] Enable scheduled DB backup runner in target env (`backup_policy_runner_enabled=true`) and verify first automated backup artifact to S3.
- [x] Enable daily Slack ops report (`slack_daily_report_enabled=true`) and attach first sent report evidence link.
- [x] Validate eBay fee-estimate assumptions (`final value`, `payment`, `promoted`) against recent production orders and record approved runtime values.
- [x] Validate Notification Routing matrix (`slack|email|both|disabled`) for backup + daily report + system health + business reports in target env and attach evidence.
- [x] Send one manual business status report from Admin dry-run card (`Send This Preview Now`) and attach Slack evidence link.
- [x] Run QA automation suite (unit + Playwright smoke) and attach CI evidence link for release candidate.
  - QA workflow now publishes a dedicated `qa-evidence` artifact (`qa_evidence.md` + `qa_evidence.json`) for this evidence link.
- [x] Complete all owner/date/evidence-link fields below and record final sign-off.
- [x] Complete commerce/legal sign-offs (tax treatment, record retention, marketplace policy, role controls).
- [x] eBay operator UX consolidation (dedicated Templates page + Listing Wizard with optional direct single-listing post + reusable wizard eBay post profiles).
- [x] AI-first workflow readiness (in-flow assists with approval/audit guardrails) for listing/intake operations.
- [~] Accounting model verification + AI Accountant readiness.
  - Deterministic dashboard/Reports profit math now uses `gross + shipping charged - fees - label spend - COGS`.
  - Cost basis now honors explicit lot-assignment costs, remaining lot-total allocation for blank assignments, product landed acquisition cost, then `product_cost` fallback.
  - Multi-lot product repurchases now use time-aware FIFO sale COGS and FIFO remaining lot cost for inventory value, preventing sales from consuming later repurchase lots.
  - Mixed-value lots now support exact assignment allocated dollars and assignment allocation weights for proportional bulk-lot cost splits across different products.
  - Purchase lots now support optional expected total quantity for whole-lot cost allocation, preventing partial check-ins from overstating early sale COGS when the lot is not fully assigned yet.
  - Dashboard estimated profit now shows a cost-basis review warning when 30-day sold COGS includes equal fallback, mixed, or missing basis so partially checked-in lots do not silently make profit appear wrong.
  - Reports now includes an Accounting Review / Close Readiness panel for inventory value, sales net before COGS, FIFO COGS, profit before returns, estimated profit after returns, exception counts, reconciliation flags, fee-source fallback coverage, and shipping label-spend coverage.
  - Reports now includes in-app Accounting Field Semantics for product cost fields, lot totals, assignment landed components, and FIFO remaining lot cost.
  - Reports now includes an accounting exception queue for missing cost/fee/shipping evidence, fee-source fallback, nonpositive margins, and lot allocation anomalies.
  - Reports now includes a deterministic Accounting Close Packet ZIP for accountant-review evidence.
  - Ask/AI accounting snapshots now prefer FIFO/lot cost maps before product-level fallbacks, aligning AI-visible context with dashboard/Reports.
  - QBO sales/refund staging exports now preserve COGS source, return COGS reversal, and estimated return profit impact; Accounting Close Readiness applies restocked return COGS reversals.
  - Period-level drift checks now compare Accounting Close Readiness totals against QBO sales/refund staging exports, Dashboard Live Metrics 30-day totals, Slack-style daily/weekly business summary totals, and Ask/AI accounting snapshot 30-day totals when the selected Reports window matches each comparison window; results are included in the Accounting Close Packet, passed into Reports Copilot/AI Accountant structured review context, and drift warnings block close-ready status through a `Period Drift Warnings` close-readiness check.
  - Accounting Close Formula Checks now verify profit before returns, return profit impact, and estimated profit after returns arithmetic, include the rows in close packets and AI Accountant citations, and block close-ready status if formula drift appears.
  - Accounting Sales Component Checks now verify Sales Detail fee/shipping/label components tie to COGS & Margin close totals, include the rows in close packets and AI Accountant citations, and block close-ready status if component drift appears.
  - Accounting Return Tie-Out Checks now verify Returns refund totals, QBO refund/adjustment staging, return COGS reversals, and staged return profit impact, include the rows in close packets and AI Accountant citations, and block close-ready status if return tie-out drift appears.
  - Accounting Inventory Valuation Checks now verify stocked inventory landed-cost coverage, Inventory Snapshot value formulas, and close-readiness inventory value tie-out, include the rows in close packets and AI Accountant citations, and block close-ready status if valuation drift appears.
  - Accounting Fee Evidence Checks now verify eBay Fee Reconciliation row/fee totals and Fee Source Priority rows tie to Sales Detail, flag sale-field fee fallback rows, include the rows in close packets and AI Accountant citations, and block close-ready status if fee-evidence drift appears.
  - Accounting Shipping Evidence Checks now verify Sales Detail shipping charged/label spend ties to Shipping Economics detail and summary rows, validate shipping delta formulas, flag paid-shipping rows missing label spend, include the rows in close packets and AI Accountant citations, and block close-ready status if shipping-evidence drift appears.
  - Accounting Reconciliation Tie-Out Checks now verify Reconciliation by Marketplace sales/return counts and totals tie to Sales Detail, Returns, net-after-return formulas, and close reconciliation flags, include the rows in close packets and AI Accountant citations, and block close-ready status if reconciliation tie-out drift appears.
  - Accounting COGS Source Checks now verify Sold COGS Source Summary sale count, quantity, FIFO COGS, and profit before returns tie to COGS & Margin Detail and close readiness, include the rows in close packets and AI Accountant citations, and block close-ready status if sold COGS uses equal fallback or missing/unknown basis.
  - Accounting Lot Allocation Checks now verify Lot Allocation Source Summary assignment count, quantity, and resolved landed cost tie to Lot Assignment detail and close readiness, include the rows in close packets and AI Accountant citations, and block close-ready status if lot assignments use equal fallback or missing/unknown basis.
  - Accounting Exception Queue Checks now verify Accounting Exception Queue total/P0/P1 counts tie to close readiness, flag malformed queue rows, include the rows in close packets and AI Accountant citations, and keep P0 exceptions visibly blocking close-ready status.
  - Accounting Margin Anomaly Checks now verify negative/nonpositive COGS & Margin rows tie to close readiness and `nonpositive_margin` exception evidence, include the rows in close packets and AI Accountant citations, and block close-ready status while margin anomalies remain unresolved.
  - Accounting Close Consistency Checks now verify final close-readiness status, blocker/warning counts, blocker/warning text, and close-check fail/warn rows agree before close sign-off evidence is trusted.
  - Accounting Close Packet Completeness Checks now verify required close-packet evidence artifacts are present and populated, include the rows in close packets and AI Accountant citations, and should be clear before close sign-off evidence is trusted.
  - Accounting Close Packet Manifest Checks now verify selected close-packet prefixes and manifest row-count values match exported report dataframes, include the rows in close packets and AI Accountant citations, and should be clear before close sign-off evidence is trusted.
  - Accounting Close Packet Hash Checks now add SHA-256 CSV hashes to the close packet manifest, verify selected packet artifacts have hash evidence, include the rows in close packets and AI Accountant citations, and should be clear before close sign-off evidence is trusted.
  - Accounting Close Packet manifests now include stable `accounting_close_packet_evidence_hash_sha256` values derived from selected close CSV payloads, date range, and close summary for deterministic sign-off evidence references.
  - Accounting Close Sign-Off Tracker now captures the Accounting Close Packet evidence hash, and Accounting Close Sign-Off Review compares approved sign-off hashes against the recalculated packet evidence hash to flag stale packet evidence.
  - Reports now shows and exports an Accounting Close Packet Evidence Hash table and can record Accounting Close Sign-Off audit evidence directly from the close packet workflow.
  - Accounting Close Sign-Off Review now warns when approved close sign-offs have packet references but no matching packet evidence hash, keeping incomplete deterministic evidence visible before approval is trusted.
  - Accounting Close Sign-Off Review now compares approved sign-off exception counts against recalculated close total exceptions, flagging stale approvals when exception totals drift.
  - Accounting Close Sign-Off Review now warns when approved close sign-offs are missing owner or sign-off date evidence, keeping monthly close approvals tied to accountable reviewer evidence.
  - Accounting Close Sign-Off Review now validates approved sign-off dates are parseable, not before period end, and not future-dated before approval evidence is trusted.
  - Reports now includes latest Accounting Close Sign-Off Tracker rows in close packet evidence and AI Accountant context, tying owner/date/packet evidence directly to the close-readiness review.
  - Reports now includes Accounting Close Sign-Off Review evidence in close packets and AI Accountant citations, warning when approved sign-offs are stale relative to recalculated readiness, blocker count, drift warning count, or packet/evidence references.
  - Next hardening step: run production-sample close review before sign-off.
  - Remaining before close-ready sign-off: accountant/human review against production samples and completed monthly close owner/date evidence in the Accounting Close Sign-Off Tracker, including readiness status, blocker count, and period drift warning count.
- [ ] Tax reporting + guidance readiness.
  - Scope tracked in `GS-V10-021`: tax review workspace, monthly/quarterly tax packet exports, tax exception checks, saved tax profiles, tax reporting sign-offs, and role-gated AI tax/accounting guidance.
  - First packet/export foundation is live; Admin and Reports now capture tax profiles/sign-off evidence with Go-Live Evidence Pack artifacts (`tax_profiles.csv`, `tax_reporting_signoffs.csv`).
  - Reports can apply saved tax profiles to tax review assumptions, includes selected profile metadata in Tax Review Packet evidence, and reviews approved tax sign-offs against current packet/profile/exception evidence.
  - Reports includes tax reporting sign-off evidence and Tax Reporting Sign-Off Review evidence in Tax Review and Accounting Close packets.
  - Reports Copilot and AI Accountant review now cite tax assumptions, selected tax profile evidence, tax reporting sign-off rows, and Tax Reporting Sign-Off Review evidence while preserving advisory-only boundaries; both render structured JSON sections as readable bullets while retaining raw JSON for audit/debug review and tolerate fenced/prefaced JSON wrappers.
  - Reports Copilot review runs now write read-only audit events with deterministic prompt/data-scope hashes, packet evidence hashes, and cited tax/accounting row counts.
  - AI Accountant audit metadata now records deterministic prompt/data-scope hashes, packet evidence hashes, context keys, and compact cited row-count scope metadata for tax/accounting review traceability.
  - Reports Copilot and AI Accountant results now support accepted/edited/rejected feedback audit events tied to the response hash and original review metadata.
  - Reports now surfaces AI Review Outcome Evidence from audit logs, includes it in Accounting Close Packet exports, and passes it into AI Accountant context/citations.
  - Accounting Close Readiness now blocks close-ready status when the latest Copilot/AI Accountant outcome per review type is `edited` or `rejected`.
  - Accounting Close Sign-Off evidence now captures `ai_review_followup_count`, and Sign-Off Review warns when approved sign-offs no longer match recalculated AI review follow-up blockers.
  - Reports close sign-off workflow now displays the readiness/blocker/drift/AI-review-follow-up snapshot that will be captured with the sign-off evidence.
  - Next hardening step: production-sample tax/accounting review using packet/profile/sign-off evidence.
  - Guidance boundary: in-app/AI tax outputs are advisory planning aids only; bullion/coin exemptions, shipping taxability, marketplace facilitator treatment, and local/state rules require tax-advisor validation before filing or remittance decisions.

## Go-Live Execution Board (Working Tracker)

Use this table as the operational board for the current release candidate.

| Workstream | Task | Environment | Priority | Owner | Due Date | Status | Evidence Link | Notes |
|---|---|---|---|---|---|---|---|---|
| QA | Coverage gate enabled (`>=38%` global, `>=88%` scoped-core) | Dev/Prod | P0 |  |  | Done |  | QA workflow now enforces global and scoped-core fail-under gates |
| QA | Playwright critical path pass | Dev/Prod | P0 |  |  | Done |  | Local chromium suite currently 8 passing + 1 skip-safe admin-go-live spec; CI evidence link pending |
| Shipping | Live provider validation run | Dev | P0 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Shipping | Live provider validation run | Prod | P0 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Observability | Critical alert routing acceptance | Dev | P0 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Observability | Critical alert routing acceptance | Prod | P0 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Data Safety | Backup + restore drill | Dev | P0 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Data Safety | Backup + restore drill | Prod | P1 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Commerce | Listing -> invoice -> order/item -> sale flow validation | Dev | P0 |  |  | Done |  |  |
| Commerce | Not-sold listing outcome validation (no sale posted) | Dev | P0 |  |  | Done |  |  |
| AI Workflow | Listing Wizard AI assist + approval/audit flow validation | Dev | P0 |  |  | Done |  |  |
| Commerce | eBay fee estimate calibration (estimate vs actual) | Prod | P0 |  |  | Done |  | Small-scale production testing complete (2026-04-13) |
| Accounting | Cost-basis/profit model sign-off + exception review | Dev/Prod | P0 |  |  | In Progress |  | Deterministic model patched; dashboard profit-basis warnings, Reports close-readiness panel, close packet, accounting exception queue, QBO COGS/return provenance, close-vs-QBO/dashboard/Slack/AI drift checks, FIFO-aware Ask/AI accounting context, AI Accountant review, Accounting Close Sign-Off Tracker, and close-packet sign-off evidence export are implemented; remaining work is production-sample human/accountant review and completed sign-off evidence |
| AI Governance | Runtime model/profile fallback controls validated for listing/intake workflows | Dev/Prod | P1 |  |  | Done |  |  |
| Lifecycle Governance | GS-V10-017 lifecycle retention policy sign-off entries completed (Dev/Prod) | Dev/Prod | P1 | Keith Kacsh | 2026-04-15 | Done | Internal go-live evidence pack (2026-04-15) -> `lifecycle_retention_policy_signoffs.csv` | Admin tracker + evidence-pack export (`lifecycle_retention_policy_signoffs.csv`) completed for closeout |
| Legal/Tax | Tax treatment sign-off (including bullion/coin exemptions) | Prod | P0 | Keith Kacsh | 2026-04-15 | Done | Internal go-live evidence pack (2026-04-15) -> `commerce_legal_signoffs.csv` |  |
| Legal/Policy | Marketplace policy conformance sign-off | Prod | P0 | Keith Kacsh | 2026-04-15 | Done | Internal go-live evidence pack (2026-04-15) -> `commerce_legal_signoffs.csv` |  |
| Legal/Records | Invoice/receipt retention sign-off | Prod | P0 | Keith Kacsh | 2026-04-15 | Done | Internal go-live evidence pack (2026-04-15) -> `commerce_legal_signoffs.csv` |  |
| Security | Financial posting role-control sign-off | Prod | P0 |  |  | Done |  |  |
| Release | Go-live evidence pack exported + attached | Prod | P0 |  |  | Done |  |  |

## 1) Release + Environment Readiness
- [x] Confirm GitHub workflow preflight passes (`Deployment Config Plan Check`).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Confirm Docker publish workflow produced versioned image and SHA tags.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Confirm Argo Dev/Prod manifest paths and variables are configured.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 2) Kubernetes + ArgoCD
- [x] Dev app sync succeeds in ArgoCD.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Prod app sync succeeds in ArgoCD.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] PreSync migration hook succeeds in both environments.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 3) Security + Secrets
- [x] No plaintext template secrets are deployed; namespace secrets are sourced from your secret manager.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Auth signing key and password-auth settings verified per environment.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Network policy validated for required ingress/egress only.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 4) Data Safety
- [x] Backup creation tested in Dev.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Restore tested in Dev from backup artifact.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Rollback runbook dry-run completed.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 5) App Functional Readiness
- [x] Login/session restore works as expected across restart.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Inventory intake -> listing draft -> review -> publish workflow validated.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Shipping queue and tracking push workflow validated.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Admin `Live Provider Validation Run` executed for target environment(s) with evidence export attached.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Sync failure triage/retry workflow validated.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Listing outcome branch validated in Documents (`Sold` creates records, `Not Sold` does not create sales and supports listing end/archive).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Listing invoice posting validated with optional linked `Order + OrderItem + Sale` chain.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Purchase-document intake validated end-to-end (LLM/Textract/Both extraction modes, lot/product link flows, and bulk line-item conversion evidence captured).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 6) eBay Operations
- [x] eBay auth/verify succeeds for target environment account.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Policies/locations and publish defaults validated.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Listing format/reporting checks validated in Reports:
  - `Listing Format Intent vs Publish Outcome`
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 6.1) Legal + Compliance Readiness (Commerce Operations)
- Use `Admin -> Governance Exports -> Commerce Legal Sign-Off Tracker` to record owner/date/evidence/status entries per policy item and include them in the go-live evidence pack.
- [x] Tax treatment policy approved for each selling mode (eBay, local, Craigslist, Facebook Marketplace, etc.).
  - Include treatment rules for bullion/coin exemptions, taxable shipping handling, and manual overrides.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Invoice/receipt retention policy approved (storage location, retention period, retrieval process).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Marketplace policy conformance review complete (listing content, shipping, returns, prohibited practices).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Privacy/data-handling policy for customer/contact/shipping data approved for production operations.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Financial posting role controls approved (who can post invoices into orders/sales in production).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Legal/accounting reviewer sign-off captured that system outputs are reviewed for go-live usage.
  - Name: Keith Kacsh
  - Date: 2026-04-15
  - Evidence link: Internal go-live evidence pack (2026-04-15)
## 7) Observability + Supportability
- [x] System Health shows environment + build metadata (`APP_BUILD_VERSION`, `APP_BUILD_SHA`).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] System Health `Error Signals (24h)` reviewed with acceptable baseline before release.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Error-signal thresholds (`health_*_24h`) and runbook URLs (`runbook_*_url`) configured for target environment.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Critical auto-alert policy configured/validated (`health_auto_alert_critical_enabled`, cooldown, Slack channel/template).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Manual critical health alert test executed from System Health (`Send Critical Health Alert Now`) and audit evidence captured.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] `Critical Alert Evidence (Recent)` shows expected sent/queued outcomes for health-critical test run(s).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Critical alert evidence CSV exported from System Health and attached to release/go-live artifact set.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Admin `Go-Live Evidence Pack (ZIP)` exported and attached to release/go-live artifact set.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Evidence pack includes shipping provider validation artifact (`shipping_provider_validation_30d.csv`) for target environment.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Admin `Record Evidence Capture Event` executed and visible in recent evidence-capture history.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Admin `Go-Live Readiness Score` reviewed and accepted for target release window.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Readiness scoring config (weights/thresholds) reviewed and approved for target environment.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Performance baseline evidence captured from `Admin -> System Health`:
  - run `DB Rollup Latency Baseline` and export CSV
  - run `Page/Read Latency Baseline` (default probes) and export CSV
  - run `Page/Read Latency Baseline` with `Include heavy probes` enabled and export CSV
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-18)
  - Date: 2026-04-18
- [x] Alert routing validated (Slack and/or ops channel policy).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Admin runtime/env coverage has no critical missing keys.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
## 8) Sign-Off
- [x] Engineering sign-off
  - Name: Keith Kacsh
  - Date: 2026-04-15
- [x] Operations sign-off
  - Name: Keith Kacsh
  - Date: 2026-04-15
- [x] Business owner sign-off
  - Name: Keith Kacsh
  - Date: 2026-04-15
## 9) QA Coverage Hardening (P0 for Release Confidence)
- [x] CI coverage reporting enabled and visible for each QA run.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Initial global/scoped coverage gates enabled (`>=38%` global, `>=88%` scoped-core) and enforced.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Core-module coverage target met (`>=95%` scoped core set).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Global coverage progressed to agreed interim release threshold (`>=55%` target track).
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
- [x] Playwright critical operator path suite passes in CI for release candidate.
  - Owner: Keith Kacsh
  - Evidence link: Internal go-live evidence pack (2026-04-15)
  - Date: 2026-04-15
