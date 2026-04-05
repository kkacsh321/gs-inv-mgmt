# GoldenStackers Go-Live Checklist

Use this checklist before promoting beyond local into Dev/Prod.

Status key:
- `[ ]` not started
- `[~]` in progress
- `[x]` done

## Current High-Priority Progress Snapshot (as of 2026-04-02)
- [~] **GS-V10-006 QA automation + coverage hardening (P0)**
  - Baseline pipelines are in place (unit tests in build + dedicated QA workflow + Playwright smoke).
  - Current measured Python coverage baseline is now above initial gate (`~41.77%` global, `~95.01%` scoped-core, `604` tests passing); next focus is deeper mutation-level e2e assertions.
- [x] **GS-V10-003 Event-Driven Automation hardening (implementation)**
  - Rule CRUD, approval gating, impact preview, replay simulation, drift logging, and hardening sign-off capture are implemented in Admin.
  - Operational remaining: execute production hardening evidence and attach sign-off links in this checklist.
- [~] **GS-V10-002 Label Buying Integration**
  - Queue + adapter scaffolding and runtime guardrails are in place, including Pirate Ship adapter tests, guided live-validation workflow, and Dev/Prod sign-off capture in Admin.
  - Remaining: complete live-provider production validation by environment.
- [x] **GS-V10-004 Observability baseline (implementation)**
  - Structured queue execute-exception capture, 24h error-signal visibility, threshold/runbook fields, critical alert validation, evidence exports, readiness scoring, calibration sign-off, and alert-routing acceptance sign-off capture are in place.
  - Operational remaining: run target-env calibration/acceptance evidence and attach owner/date/sign-off links in this checklist.
- [x] **GS-V10-005 Backup/Restore automation (implementation)**
  - Backup policy controls, restore-drill evidence capture, DR checklist snapshots, and SLA reporting are now implemented in Admin, including go-live evidence-pack exports.
  - Operational remaining: execute and link Dev/Prod restore drills with owner/date, RTO notes, and final DR sign-off.
- [~] **Go-live operations evidence collection**
  - Checklist remains open until Dev/Prod evidence links and owner/date sign-offs are filled.
- [~] **Production commerce/legal workflow readiness**
  - Listing->invoice->order/item->sale posting flow is implemented (including sold vs not-sold outcomes).
  - Tax-treatment posting audit context and immutable document-artifact retention are implemented in-app.
  - Remaining: legal/tax policy sign-off, retention policy sign-off, and production role-control sign-off.

## Priority Remaining Focus (Next Execution Window)
- [x] Implement CI coverage gates (current: global `>=30%`, scoped-core `>=85%`) and publish coverage artifacts on QA runs.
- [x] Expand test coverage on core business modules (`repository/services/auth/validation/security`) toward scoped-core `>=95%`.
- [ ] Expand Playwright critical flows (auth/session restore, intake, listing review/publish, shipping, sync retry, admin sign-off).
  - Implemented and passing locally: auth/session, intake (coin + generic), products, listings, shipping queues, sync controls, and admin governance sign-off/evidence surfaces.
  - Remaining: deepen shipping/sync assertions to full retry + mutation outcomes in seeded/live-like datasets.
- [ ] Live provider validation for shipping labels in Dev and Prod (API mode + rollback path).
- [ ] Alert-routing acceptance test (manual + auto critical health alerts) with channel ownership confirmation.
- [ ] Backup/restore drill execution with attached evidence and recovery time notes.
- [ ] Run QA automation suite (unit + Playwright smoke) and attach CI evidence link for release candidate.
  - QA workflow now publishes a dedicated `qa-evidence` artifact (`qa_evidence.md` + `qa_evidence.json`) for this evidence link.
- [ ] Complete all owner/date/evidence-link fields below and record final sign-off.
- [ ] Complete commerce/legal sign-offs (tax treatment, record retention, marketplace policy, role controls).

## Go-Live Execution Board (Working Tracker)

Use this table as the operational board for the current release candidate.

| Workstream | Task | Environment | Priority | Owner | Due Date | Status | Evidence Link | Notes |
|---|---|---|---|---|---|---|---|---|
| QA | Coverage gate enabled (`>=30%` global, `>=85%` scoped-core) | Dev/Prod | P0 |  |  | Done |  | QA workflow now enforces global and scoped-core fail-under gates |
| QA | Playwright critical path pass | Dev/Prod | P0 |  |  | In Progress |  | Local chromium suite currently 9 passing specs; CI evidence link pending |
| Shipping | Live provider validation run | Dev | P0 |  |  | Not Started |  |  |
| Shipping | Live provider validation run | Prod | P0 |  |  | Not Started |  |  |
| Observability | Critical alert routing acceptance | Dev | P0 |  |  | Not Started |  |  |
| Observability | Critical alert routing acceptance | Prod | P0 |  |  | Not Started |  |  |
| Data Safety | Backup + restore drill | Dev | P0 |  |  | Not Started |  |  |
| Data Safety | Backup + restore drill | Prod | P1 |  |  | Not Started |  |  |
| Commerce | Listing -> invoice -> order/item -> sale flow validation | Dev | P0 |  |  | Not Started |  |  |
| Commerce | Not-sold listing outcome validation (no sale posted) | Dev | P0 |  |  | Not Started |  |  |
| Legal/Tax | Tax treatment sign-off (including bullion/coin exemptions) | Prod | P0 |  |  | Not Started |  |  |
| Legal/Policy | Marketplace policy conformance sign-off | Prod | P0 |  |  | Not Started |  |  |
| Legal/Records | Invoice/receipt retention sign-off | Prod | P0 |  |  | Not Started |  |  |
| Security | Financial posting role-control sign-off | Prod | P0 |  |  | Not Started |  |  |
| Release | Go-live evidence pack exported + attached | Prod | P0 |  |  | Not Started |  |  |

## 1) Release + Environment Readiness
- [ ] Confirm GitHub workflow preflight passes (`Deployment Config Plan Check`).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Confirm Docker publish workflow produced versioned image and SHA tags.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Confirm Argo Dev/Prod manifest paths and variables are configured.
  - Owner:
  - Evidence link:
  - Date:

## 2) Kubernetes + ArgoCD
- [ ] Dev app sync succeeds in ArgoCD.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Prod app sync succeeds in ArgoCD.
  - Owner:
  - Evidence link:
  - Date:
- [ ] PreSync migration hook succeeds in both environments.
  - Owner:
  - Evidence link:
  - Date:

## 3) Security + Secrets
- [ ] No plaintext template secrets are deployed; namespace secrets are sourced from your secret manager.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Auth signing key and password-auth settings verified per environment.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Network policy validated for required ingress/egress only.
  - Owner:
  - Evidence link:
  - Date:

## 4) Data Safety
- [ ] Backup creation tested in Dev.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Restore tested in Dev from backup artifact.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Rollback runbook dry-run completed.
  - Owner:
  - Evidence link:
  - Date:

## 5) App Functional Readiness
- [ ] Login/session restore works as expected across restart.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Inventory intake -> listing draft -> review -> publish workflow validated.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Shipping queue and tracking push workflow validated.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Admin `Live Provider Validation Run` executed for target environment(s) with evidence export attached.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Sync failure triage/retry workflow validated.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Listing outcome branch validated in Documents (`Sold` creates records, `Not Sold` does not create sales and supports listing end/archive).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Listing invoice posting validated with optional linked `Order + OrderItem + Sale` chain.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Purchase-document intake validated end-to-end (LLM/Textract/Both extraction modes, lot/product link flows, and bulk line-item conversion evidence captured).
  - Owner:
  - Evidence link:
  - Date:

## 6) eBay Operations
- [ ] eBay auth/verify succeeds for target environment account.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Policies/locations and publish defaults validated.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Listing format/reporting checks validated in Reports:
  - `Listing Format Intent vs Publish Outcome`
  - Owner:
  - Evidence link:
  - Date:

## 6.1) Legal + Compliance Readiness (Commerce Operations)
- Use `Admin -> Governance Exports -> Commerce Legal Sign-Off Tracker` to record owner/date/evidence/status entries per policy item and include them in the go-live evidence pack.
- [ ] Tax treatment policy approved for each selling mode (eBay, local, Craigslist, Facebook Marketplace, etc.).
  - Include treatment rules for bullion/coin exemptions, taxable shipping handling, and manual overrides.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Invoice/receipt retention policy approved (storage location, retention period, retrieval process).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Marketplace policy conformance review complete (listing content, shipping, returns, prohibited practices).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Privacy/data-handling policy for customer/contact/shipping data approved for production operations.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Financial posting role controls approved (who can post invoices into orders/sales in production).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Legal/accounting reviewer sign-off captured that system outputs are reviewed for go-live usage.
  - Name:
  - Date:
  - Evidence link:

## 7) Observability + Supportability
- [ ] System Health shows environment + build metadata (`APP_BUILD_VERSION`, `APP_BUILD_SHA`).
  - Owner:
  - Evidence link:
  - Date:
- [ ] System Health `Error Signals (24h)` reviewed with acceptable baseline before release.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Error-signal thresholds (`health_*_24h`) and runbook URLs (`runbook_*_url`) configured for target environment.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Critical auto-alert policy configured/validated (`health_auto_alert_critical_enabled`, cooldown, Slack channel/template).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Manual critical health alert test executed from System Health (`Send Critical Health Alert Now`) and audit evidence captured.
  - Owner:
  - Evidence link:
  - Date:
- [ ] `Critical Alert Evidence (Recent)` shows expected sent/queued outcomes for health-critical test run(s).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Critical alert evidence CSV exported from System Health and attached to release/go-live artifact set.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Admin `Go-Live Evidence Pack (ZIP)` exported and attached to release/go-live artifact set.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Evidence pack includes shipping provider validation artifact (`shipping_provider_validation_30d.csv`) for target environment.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Admin `Record Evidence Capture Event` executed and visible in recent evidence-capture history.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Admin `Go-Live Readiness Score` reviewed and accepted for target release window.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Readiness scoring config (weights/thresholds) reviewed and approved for target environment.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Alert routing validated (Slack and/or ops channel policy).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Admin runtime/env coverage has no critical missing keys.
  - Owner:
  - Evidence link:
  - Date:

## 8) Sign-Off
- [ ] Engineering sign-off
  - Name:
  - Date:
- [ ] Operations sign-off
  - Name:
  - Date:
- [ ] Business owner sign-off
  - Name:
  - Date:

## 9) QA Coverage Hardening (P0 for Release Confidence)
- [x] CI coverage reporting enabled and visible for each QA run.
  - Owner:
  - Evidence link:
  - Date:
- [x] Initial global/scoped coverage gates enabled (`>=30%` global, `>=85%` scoped-core) and enforced.
  - Owner:
  - Evidence link:
  - Date:
- [ ] Core-module coverage target met (`>=95%` scoped core set).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Global coverage progressed to agreed interim release threshold (`>=55%` target track).
  - Owner:
  - Evidence link:
  - Date:
- [ ] Playwright critical operator path suite passes in CI for release candidate.
  - Owner:
  - Evidence link:
  - Date:
