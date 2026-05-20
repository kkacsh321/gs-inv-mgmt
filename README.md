# GoldenStackers Inventory Management

Inventory and multi-channel resale operations system for GoldenStackers.

## CI/CD Status

[![AutoTag](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/autotagger.yaml/badge.svg)](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/autotagger.yaml)
[![Publish release](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/autorelease.yaml/badge.svg)](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/autorelease.yaml)
[![Build and Publish Docker](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/docker_build.yaml/badge.svg)](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/docker_build.yaml)
[![Deployment Config Plan Check](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/deploy_config_check.yaml/badge.svg)](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/deploy_config_check.yaml)
[![QA Test Suite](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/qa_tests.yaml/badge.svg)](https://github.com/kkacsh321/gs-inv-mgmt/actions/workflows/qa_tests.yaml)

Roadmap:
- See [ROADMAP.md](ROADMAP.md) for phased delivery plan (`v0.2` through `v1.0`).
- Track v0.2 execution tasks in [V0_2_IMPLEMENTATION_CHECKLIST.md](V0_2_IMPLEMENTATION_CHECKLIST.md).
- Track v0.3 execution tasks in [V0_3_IMPLEMENTATION_CHECKLIST.md](V0_3_IMPLEMENTATION_CHECKLIST.md).
- Track v0.4 execution tasks in [V0_4_IMPLEMENTATION_CHECKLIST.md](V0_4_IMPLEMENTATION_CHECKLIST.md).
- Track v0.5 execution tasks in [V0_5_IMPLEMENTATION_CHECKLIST.md](V0_5_IMPLEMENTATION_CHECKLIST.md).
- Track v0.6 execution tasks in [V0_6_IMPLEMENTATION_CHECKLIST.md](V0_6_IMPLEMENTATION_CHECKLIST.md).
- Deployment runbook: [DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md).
- Slack Ops runbook: [SLACK_OPS_RUNBOOK.md](SLACK_OPS_RUNBOOK.md).
- Go-live checklist: [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md).
- Go-live evidence/sign-off closeout recorded in checklist on `2026-04-15` (engineering/operations/business-owner sign-offs and evidence placeholders finalized).
- QA test plan: [QA_TEST_PLAN.md](QA_TEST_PLAN.md).
- Current accounting/profitability progress (`2026-04-26`): dashboard, Sales, Reports, and Ask/AI accounting context now use the same net convention (`gross + shipping charged - fees - label spend`) and before-return profit convention (`net before COGS - COGS`), with COGS resolved from explicit lot-assignment costs, remaining lot-total allocation for blank assignment costs, product landed acquisition cost, then `product_cost` fallback. Sale-level COGS is now time-aware FIFO across product repurchases from multiple lots, so a sale cannot consume a later lot; inventory value uses FIFO remaining lot cost when lot history exists. Whole-lot fallback costs now support exact assignment-level allocated dollars, `product_lot_assignments.allocation_weight` for mixed-value lots, and `purchase_lots.expected_total_quantity` for partial check-ins, preventing early or low-value products from absorbing the wrong share of a bulk lot. Reports now flags multi-product blank-cost lots that are still using equal quantity fallback with no weights/expected quantity, Lot Assignment exports include resolved landed unit/total cost plus a `cost_source` label, Accounting Review/close packets include a Lot Allocation Source Summary, and close readiness warns when lot assignments still rely on equal fallback or missing cost basis. Actual-economics rollups and QuickBooks-style sales staging exports now prefer normalized order finance entries for marketplace fees and shipping-label spend before falling back to order/sale fields. Close-readiness return impact now subtracts full refund totals (`refund_amount + refund_fees + refund_shipping`) consistently with reconciliation and QBO adjustment exports. Added regression coverage for lot-only product costs, mixed explicit/blank lot assignments, mixed-value allocation weights, allocation-basis exports/summaries, close-readiness allocation warnings, ambiguous equal-fallback lot exceptions, partial lot check-ins with expected quantity, multi-lot repurchase FIFO, dashboard profit, Reports cost maps, accounting snapshot helpers, a Reports accounting exception queue for missing cost/fee/shipping evidence and lot allocation anomalies, an Accounting Review / Close Readiness panel with canonical field semantics, and a deterministic Accounting Close Packet ZIP export.
- Current profit-after-returns alignment (`2026-05-13`): dashboard, Reports close readiness, Accounting Close Packet guidance, scheduled Slack reports, Ask/AI accounting snapshots, Reports Copilot, AI Accountant contexts, and lot P/L now distinguish before-return margin from final estimated profit after returns. The canonical close formula is `estimated_profit_after_returns = profit_before_returns - return refunds + returned COGS reversal`, where return refunds include `refund_amount + refund_fees + refund_shipping`. Report-visible close metrics and lot P/L now label `FIFO Margin Before Returns`/`Profit Before Returns` separately from `Est. Profit After Returns`, reducing confusion when sales with restocked returns make sale-only margin and return-adjusted profit diverge.
- Current dashboard profit-label clarity (`2026-05-13`): dashboard live metrics now publish `sales_30d_profit_before_returns` alongside `sales_30d_est_profit`, and the dashboard labels the main profit card as `Est Profit After Returns (30d)` with `Profit Before Returns (30d)` beside it. The return caption now shows how returns moved profit from the before-return figure to the final after-return estimate, and Reports drift checks separately tie out dashboard before-return profit and after-return estimated profit.
- Current AI Accountant dashboard-profit evidence (`2026-05-13`): AI Accountant monitor rows, workspace notes, and scheduled-review context now carry dashboard `sales_30d_profit_before_returns` beside final `sales_30d_est_profit`, so cost-basis alerts and LLM review packets identify which profit value is before returns and which is after return impact.
- Current sale COGS audit evidence (`2026-05-15`): repository FIFO sale cost maps now expose total COGS per sale plus per-allocation evidence rows with product, lot, assignment, quantity, unit cost, total cost, and cost source. Accounting exception details for missing cost basis and nonpositive margin now include the resolved COGS source/evidence summary, making dashboard/report profit basis easier to trace before close review.
- Current Reports sale COGS evidence export (`2026-05-15`): Reports and Accounting Close Packet exports now include `Sale FIFO COGS Evidence`, one row per FIFO allocation consumed by a sale, and `COGS & Margin Detail` carries an evidence-row count so margin rows can be traced back to exact lot/default cost allocations.
- Current sale COGS evidence tie-out (`2026-05-15`): Accounting COGS Source Checks now validate `Sale FIFO COGS Evidence.total_cost`, distinct sale count, and evidence row count against `COGS & Margin Detail`; close-packet completeness also requires the evidence artifact when sales exist.
- Current AI Accountant FIFO evidence context (`2026-05-15`): Reports Copilot and Reports AI Accountant contexts now include a bounded `sale_fifo_cogs_evidence_rows` sample, AI Accountant instructions explicitly review it when COGS evidence checks warn, and AI Accountant audit citations/row counts include `sale_fifo_cogs_evidence`.
- Current scheduled/workspace AI Accountant FIFO evidence (`2026-05-18`): scheduled AI Accountant monitor reviews and manual workspace reviews now include bounded `sale_fifo_cogs_evidence_rows` built from repository FIFO cost maps, plus evidence summary counts and audit row-count metadata. Recent AI Accountant message and review-outcome rows also surface FIFO evidence counts, and the AI Accountant page shows an Evidence Packet Summary with monitor, exception, FIFO, outcome, hash-index, artifact counts, integrity-error count, verified artifact count, manifest/expected-manifest rows, evidence hash, packet integrity verification, manifest verification, and an action-task-count expander before download. Manual workspace review audit metadata now captures the packet evidence hash, row counts, artifact hashes, integrity status/errors, integrity error count, manifest status/count, expected manifest rows, and action-summary task counts; the review hash index carries those integrity/manifest/action-count fields beside prompt/data-scope/review hashes. The latest-review banner warns when an accepted review points at an unverified packet, and the action summary adds a P0 evidence-packet integrity row when packet review is needed. The packet includes `accounting_exception_queue.csv`, `sale_fifo_cogs_evidence.csv`, `ai_accountant_review_hash_index.csv`, and `evidence_summary.json` row counts/SHA-256 hashes, schema version, artifact count, artifact names, action-summary task counts, and manifest cross-checks, so Slack/app/exported reviews can trace profit-basis issues back to source exceptions, sale, lot, assignment, quantity, unit cost, total cost, source, prompt/data-scope hash, review outcome, and packet completeness.
- Current shipping-profit correction (`2026-04-27`): dashboard 30-day net/profit, Ask/AI accounting snapshots, label spend, and shipping delta now use the same sale-window actual-economics basis as Reports. Linked normalized `order_finance_entries` marketplace fees and shipping-label spend override stale sale/order fields and are allocated once across that order's sale rows; unlinked finance rows no longer inflate dashboard profit inputs, and unmatched shipping-label rows are flagged in the accounting exception queue as `unmatched_shipping_label_finance_entry`.
- Current Sales/Reports actuals alignment (`2026-04-27`): dashboard all-time `Net Sales`, Sales table rows, and Sales Copilot context now use actual fee, actual label spend, actual net, and source columns from the same repository actual-economics rows used by dashboard live metrics/Reports. Reports COGS/margin rows now use actual net before COGS for primary margin/close-readiness math when available while retaining raw sale-field net for audit comparison.
- Current Admin business report accounting alignment (`2026-04-28`): manual Admin Slack business status reports and dry-run previews now include estimated COGS plus compact COGS source mix using the same FIFO sale cost maps and product landed-cost fallback used by dashboard, Reports, AI context, and scheduled Slack reporting.
- Current reconciliation actuals alignment (`2026-04-27`): marketplace reconciliation rollups now use the same sale-window actual-economics rows for sales fees, shipping charged, label spend, sales net before returns, and net after returns, keeping reconciliation packets aligned with dashboard/Sales/Reports profit math.
- Current inventory-cycle actuals alignment (`2026-04-27`): rebuy/resell inventory-cycle analytics now use repository actual-economics rows for cycle fees, shipping charged, label spend, net sales, and estimated margin versus known acquisition cost.
- Current Sales export actuals alignment (`2026-04-27`): Reports `Sales` export rows now include raw field net plus actual fee, actual shipping charged, actual label spend, actual net before COGS, and source columns from repository actual-economics rows.
- Current QBO sales export COGS provenance (`2026-04-28`): Reports `QuickBooks Sales Export` now carries `cogs_source` beside FIFO COGS estimate, `profit_before_returns_estimate`, and legacy `gross_margin_estimate`, so bookkeeping staging rows preserve whether COGS came from explicit assignment cost, lot weights, expected-quantity fallback, equal fallback, product defaults, or missing basis.
- Current QBO adjustment export return COGS provenance (`2026-04-28`): Reports `QuickBooks Refund/Adjustment Export` now includes returned COGS estimate, restocked COGS reversal estimate, COGS source, and estimated profit impact so return/refund staging rows explain both cash refund impact and inventory/COGS reversal assumptions.
- Current close-readiness return COGS alignment (`2026-04-28`): Accounting Close Readiness now consumes restocked return COGS reversal estimates from the refund/adjustment export, exposing refund cash impact, COGS reversal total, estimated return profit impact, and adjusted net-after-returns-and-COGS in close review.
- Current tax detail evidence alignment (`2026-04-27`): estimated tax detail rows now use actual-economics allocated buyer shipping when available, retain the raw sale shipping field, and expose a shipping-cost source column for review.
- Current order export actuals alignment (`2026-04-27`): Reports `Orders` export rows now retain raw order fee/label fields while adding normalized actual fee, actual label spend, actual shipping delta, actual net before COGS, and source columns so stale order fields do not mask Finances-imported costs.
- Current Orders workspace actuals alignment (`2026-04-28`): Orders table rows and Orders Copilot context now include normalized actual fee, actual label spend, actual shipping delta, actual net before COGS, and source fields from repository order rollups while retaining editable raw order fee/label values. Raw-field fallback now uses one canonical helper for `subtotal + shipping charged - fees - label spend`, and Orders rows expose `actual_net_source` to distinguish rollup-backed rows from field fallback.
- Current dashboard COGS source hardening (`2026-04-28`): sale FIFO cost maps now carry cost-source provenance (`assignment_unit_landed_cost`, `assignment_allocated_landed_cost`, `lot_allocation_weight`, `lot_expected_quantity_fallback`, `lot_equal_quantity_fallback`, `product_default_landed_cost`, or `missing_cost_basis`) alongside cost values. Dashboard live metrics expose a 30-day COGS source mix so estimated profit can be traced back to the allocation basis, with regression coverage for partial-lot sales using `expected_total_quantity`.
- Current dashboard profit-basis review (`2026-05-06`): dashboard live metrics now expose `sales_30d_profit_basis_status` and warn when estimated profit includes equal-quantity lot fallback, mixed, or missing COGS basis. This keeps under-defined partial lots visible when early sales appear to consume too much of a bulk lot cost; operators should set `purchase_lots.expected_total_quantity`, assignment allocation weights, or assignment-level costs before trusting close-period profit.
- Current dashboard snapshot-window correction (`2026-05-06`): dashboard live metrics and no-rollup fallback rendering now upper-bound 7-day/30-day sales and order windows by the dashboard snapshot time, preventing future-dated sales/orders from leaking into current profit, COGS, shipping, and order counts.
- Current dashboard probe alignment (`2026-05-06`): rollup explain/baseline diagnostics for `dashboard_live_metrics` now use the same snapshot end bound as the production dashboard path, keeping performance probes and dashboard accounting totals comparable.
- Current eBay publish hardening (`2026-05-06`): Listing Wizard and Listings now recover from transient Inventory `25001` Core Inventory Service errors with a one-time simplified payload retry, and graded-coin readiness/aspect defaults now enforce approved-grader evidence (`Certification`, `Professional Grader`, `Grade`) when titles include numerical grades such as `PCGS MS69`.
- Current Reports COGS provenance alignment (`2026-04-28`): Reports `COGS & Margin Detail` rows now include `fifo_cost_source` and `lot_cost_source` from repository cost maps, so report exports and close packets show both the margin math and whether COGS came from explicit assignment costs, lot weights, expected-quantity fallback, equal fallback, product defaults, or missing basis.
- Current sold COGS review summary (`2026-04-28`): Reports now groups sold FIFO COGS by source in a `Sold COGS Source Summary`, includes the table in Accounting Review, AI Accountant context, and Accounting Close Packet exports, and calculates COGS share by source for faster review of fallback-heavy periods.
- Current close-readiness COGS source checks (`2026-04-28`): Accounting Close Readiness now warns when sold FIFO COGS used equal-quantity fallback or missing/unknown cost basis, not just when open lot assignments have fallback allocation sources.
- Current AI COGS source context (`2026-04-28`): Ask/AI Reports and AI Accountant snapshots now include sold COGS source mix totals and cited `cogs_source_mix` metadata so AI-visible margin explanations can identify whether sold COGS came from lot weights, expected-quantity fallback, equal fallback, product defaults, or missing basis.
- Current Ask/AI profit citation alignment (`2026-05-13`): Ask/AI Reports snapshot citations now include structured `profit_before_returns` beside `estimated_profit_after_returns`, keeping AI/audit consumers aligned with dashboard, Reports, Slack, and close-packet profit labels.
- Current AI Accountant workspace (`2026-05-06`): added a dedicated `AI Accountant` page with role-gated monitor rows from the accounting exception queue and dashboard profit-basis status, grouped action summaries, recommended cleanup actions, in-app AI Accountant message audit events, recent message history with requested/effective monitor severity metadata and fallback warnings, retry-safe Slack alert queuing through the notification outbox, a dedicated `notification_route_ai_accountant_monitor` routing control, scheduled/in-app monitor messages with question status counts, downloadable evidence packets including captured Ask/Slack answer and answer-follow-up evidence plus answer status counts, visible answer-status counts/warnings in the Recent AI Accountant Answers panel, a one-click recommended automation setup action including monitor timezone/local-time/lookback and web-research limit/timeout defaults, runtime visibility for web-research limit/timeout settings, and a read-only LLM monitor review with source citations, prompt/data-scope hashes, structured JSON rendering, deterministic monitor-review fallback when all LLM runtimes fail, accepted/edited/rejected outcome tracking, visible recent review outcome history, and a latest-outcome follow-up banner for edited/rejected reviews.
- Current AI Accountant scheduled monitor (`2026-05-06`): sync-runner can now run an AI Accountant monitor on an interval schedule by default (`ai_accountant_monitor_enabled=true`, `ai_accountant_monitor_schedule_mode=interval`, `ai_accountant_monitor_interval_hours=6`) or once daily (`daily` mode), with default-on Slack alert routing, default-on scheduled LLM review, configurable timezone/local time, lookback days, minimum severity, empty-run recording, Slack notification-outbox queuing for actionable cleanup items when the AI Accountant notification route allows Slack, setup warnings for invalid schedule mode, interval cadence, timezone, daily local time, lookback window, minimum severity, or disabled monitor-alert routing, System Health visibility for monitor due/overdue/route status and last attempt/success evidence, explicit requested/effective severity metadata in result, audit evidence, message history, and evidence exports when fallback is needed, schedule-mode-specific success/failure attempt markers, and scheduled LLM monitor reviews (`ai_accountant_monitor_llm_review_enabled`) that are audited as `ai_chat` events and appended to in-app/Slack monitor messages without blocking when the LLM runtime is unavailable.
- Current AI Accountant default-on migration (`2026-05-08`): migration `0060_ai_accountant_default_on` upgrades existing runtime settings so monitor scheduling, Slack alert routing, scheduled LLM review, accountant chat, and web research are enabled without requiring manual Admin runtime-setting edits after deploy.
- Current notification outbox default-on migration (`2026-05-08`): migration `0061_notification_outbox_default_on` enables the sync-runner notification outbox processor and retention cleanup by default, so AI Accountant Slack alerts queued through the outbox are dispatched without separate runtime setup.
- Current Slack delivery default-on migration (`2026-05-08`): migration `0062_slack_notifications_default_on` enables the global Slack notification master switch by default, leaving missing token/channel configuration as visible delivery errors instead of silently suppressing AI Accountant alerts.
- Current AI Accountant Slack readiness (`2026-05-09`): AI Accountant automation setup now separates Slack alert intent from Slack delivery readiness, warning directly when `slack_bot_token` or a target channel (`slack_default_channel` or `ai_accountant_monitor_channel`) is missing so queued monitor alerts do not appear ready while outbox delivery is retrying.
- Current AI Accountant delivery evidence (`2026-05-09`): the AI Accountant page now shows recent Slack notification-outbox rows for monitor alerts, including status, attempts, next attempt, target channel, and last error, so delivery blockers are visible in the accountant workflow without opening Admin or logs.
- Current AI Accountant delivery retry (`2026-05-09`): the AI Accountant page can now process due queued/retrying monitor Slack delivery rows from the Recent Slack Delivery panel, making token/channel fixes actionable without switching to Admin while preserving normal backoff behavior for rows that are not due yet.
- Current AI Accountant delivery summary (`2026-05-09`): the AI Accountant page now summarizes recent monitor Slack delivery rows with total, due, retrying/failed, sent, and latest-error signals above the delivery table for faster stuck-alert diagnosis.
- Current AI Accountant review payload hardening (`2026-05-09`): scheduled monitor LLM reviews now send compact accounting-specific context by default (`25` monitor rows and `25` exception rows with omitted counts) and skip comp-reference prompt baggage for `workflow=accounting`, reducing LocalAI 500s caused by oversized accountant review payloads while preserving deterministic fallback behavior.
- Current AI Accountant compact retry (`2026-05-09`): if a scheduled monitor LLM review still fails on the default compact payload, it retries once with only the top five monitor rows and five exception rows, recording `compact_retry` and omitted-row counts in audit metadata before falling back to the deterministic unavailable message.
- Current AI Accountant review diagnostics (`2026-05-09`): Recent AI Accountant Messages now expose automated review status, compact-retry usage, review hash, and review error columns so scheduled review failures like LocalAI 500s are visible from the accountant workspace.
- Current AI Accountant review error trail (`2026-05-09`): scheduled review failures now preserve both default-context and compact-context error details in the monitor message/audit payload, making it clear whether the smaller retry was attempted and what failed.
- Current AI Accountant workspace review alignment (`2026-05-09`): the manual `Run AI Accountant Review` action now uses the same `workflow=accounting` compact-context path and five-row retry as the scheduled monitor, avoiding comp-reference prompt context on accountant reviews.
- Current AI Accountant review payload evidence (`2026-05-09`): scheduled review audit/message rows now include final-attempt monitor row count, exception row count, FIFO evidence row count, omitted row count, compact-retry flag, answer/prompt/data-scope hashes, and error details so LocalAI/runtime failures and review payload identity can be diagnosed from the Recent AI Accountant Messages table.
- Current AI Accountant runtime routing (`2026-05-09`): `workflow=accounting` is now a supported AI runtime profile route with Admin/config-health/default-setting coverage via `ai_workflow_profile_accounting`, so accountant automation can be pinned to the same known-good model profile controls as listing, intake, comp, and risk workflows.
- Current AI Accountant runtime diagnostics (`2026-05-09`): the AI Accountant page now exposes a sanitized `Accounting AI Runtime Chain` showing fallback order, provider/model/endpoint/source, token presence, output limit, timeout, and accounting profile selector, plus a non-writing smoke-test button that confirms the `workflow=accounting` route can respond and surfaces fallback errors without blocking monitor evidence.
- Current AI Accountant scheduled runtime evidence (`2026-05-09`): scheduled AI Accountant monitor reviews now record the sanitized accounting runtime chain and compact route summary in automated-review metadata, Recent AI Accountant Messages, and unavailable-review monitor messages, so Slack/in-app failures identify which provider/model/endpoint path failed without exposing API keys.
- Current AI Accountant scheduler evidence (`2026-05-11`): sync-runner AI Accountant monitor events now persist automated-review status, review hash, compact-retry flag, review error, and sanitized accounting runtime route separately from monitor success, so operators can prove whether the deterministic monitor ran and whether the optional LLM accountant review actually completed.
- Current System Health AI Accountant review evidence (`2026-05-11`): System Health Service Checks now includes an `AI Accountant Review Evidence` row summarizing the latest scheduled monitor event's automated-review status/hash/error/compact-retry/runtime-route, warning when actionable findings existed but the optional LLM review was unavailable.
- Current AI workflow-profile defaults (`2026-05-09`): migration `0065_ai_workflow_profiles` now backfills active blank workflow-profile runtime keys for listing, intake, comp, risk, and accounting across every runtime environment, preserving default chain behavior while satisfying config-health coverage without manual seed actions.
- Current System Health AI Accountant visibility (`2026-05-09`): System Health now includes an `AI Accountant LLM Route` service row showing the sanitized `workflow=accounting` provider/model/endpoint/source/status chain, profile selector, and ready-profile count without making a live LLM request.
- Current System Health AI Accountant alerting (`2026-05-10`): service-check errors, including a broken `AI Accountant LLM Route`, now feed the System Health critical-signal list and Slack alert context as service signals, so automated health alerts can identify accounting-LLM route breakage before the next scheduled monitor review.
- Current System Health alert defaults (`2026-05-10`): migration `0066_health_alerts_default_on` enables System Health critical auto-alerting and Slack critical notifications by default, backfills the System Health critical notification route as `slack`, and keeps channel overrides optional so missing Slack token/channel configuration remains a visible delivery/readiness issue instead of silently suppressing alerts.
- Current System Health alert route policy (`2026-05-10`): System Health critical auto-alerts now honor `notification_route_system_health_critical`; Slack dispatch only runs when the route allows Slack (`slack`, `both`, `all`, or blank), so disabled/email-only routing does not enqueue Slack alerts while the default route remains Slack.
- Current System Health critical alert policy visibility (`2026-05-10`): Service Checks now includes a `System Health Critical Alerts` row showing auto-alert status, Slack notify status, notification route, route Slack allowance, effective Slack allowance, and cooldown minutes so suppressed alert routing is visible before a critical signal occurs.
- Current System Health critical alert delivery readiness (`2026-05-10`): the `System Health Critical Alerts` row now also shows Slack master enablement, bot-token presence, target-channel presence, and effective delivery readiness; missing token/channel remains visible without suppressing route-allowed alert queueing.
- Current System Health critical delivery evidence (`2026-05-11`): System Health now summarizes recent Slack integration-queue jobs for `system_health_critical` alerts with queued/running, failed/blocked, and successful counts plus a filtered delivery table so failed critical Slack alerts are visible without digging through the generic Slack queue.
- Current System Health critical delivery retry (`2026-05-11`): the filtered System Health critical Slack delivery panel can now process only due queued `system_health_critical` Slack jobs, making token/channel fixes testable immediately without processing unrelated Slack queue work.
- Current Slack daily COGS/profit source alignment (`2026-04-28`): Daily Slack reports now include 24-hour estimated COGS, profit before returns, return refunds, return COGS reversal, return profit impact, estimated profit after returns, and a compact COGS source mix from repository FIFO sale cost maps; the integration event stores COGS, before-return profit, and return-impact totals for audit review.
- Current shipping-economics allocation alignment (`2026-04-27`): Reports shipping-economics detail/summary now reuse the same sale actual-economics allocation as dashboard/profit rows, including gross-weighted order-level shipping charged and normalized shipping-label spend for multi-line orders.
- Current lot P/L actuals alignment (`2026-04-27`): Purchase Lot `Lot P/L Snapshot (Estimated)` now aggregates product sales through the same actual-economics rows, so lot-level estimated net/profit uses normalized order fees and label spend when products are sold through marketplace orders.
- Current lot P/L FIFO attribution hardening (`2026-04-28`): Purchase Lot `Lot P/L Snapshot (Estimated)` now FIFO-consumes product sales across all purchase lots and only attributes sale quantity/net/COGS/profit to the selected lot when that lot actually supplied the sold units. This prevents newer repurchase lots from inheriting earlier product sales, shows sold COGS source totals for the lot, and separates profit before returns from estimated profit after returns when refund/COGS-reversal activity exists.
- Current Reports fallback hardening (`2026-04-27`): legacy in-memory inventory-cycle report fallback now consumes repository actual-economics rows when available, keeping degraded report paths aligned with dashboard/Reports fee, shipping, label spend, and net calculations.
- Current reconciliation fallback hardening (`2026-04-27`): legacy in-memory marketplace reconciliation fallback now uses repository actual-economics rows when the primary reconciliation rollup is unavailable, preserving normalized fee/label/net math in degraded report paths.
- Current Sales Detail actuals alignment (`2026-04-27`): Reports `Sales Detail` dataframe/export now preserves raw field net while exposing actual fee, shipping charged, label spend, actual net before COGS, source fields, and actual-backed `net_sales` even on fallback report paths.
- Current dashboard fallback hardening (`2026-04-27`): Dashboard no-rollup fallback now uses repository actual-economics rows for 7d/30d net, shipping charged, label spend, fee, shipping delta, and estimated profit when the live metrics rollup is unavailable.
- Current fee reconciliation fallback hardening (`2026-04-27`): standalone eBay fee reconciliation fallback now prefers normalized `order_finance_entries` marketplace-fee sums from `order.finance_entries` before notes-derived fee breakdowns or sale fee fields.
- Current Slack ops report accounting alignment (`2026-04-27`): daily Slack report 24h gross/net now uses repository actual-economics rows when available and falls back to the canonical net formula (`gross + shipping charged - fees - label spend`).
- Current accounting exception hardening (`2026-04-28`): Reports accounting exception rows now use normalized actual fee evidence from linked order finance entries before raw `sales.fees`, preventing false `missing_fee_evidence` and incorrect exception-margin calculations when Finances imports prove the fee.
- Current dashboard live net hardening (`2026-04-28`): repository live dashboard rollups now use actual-economics rows for 7-day net sales as well as 30-day net/profit, so normalized fee and label evidence affects short-window dashboard metrics consistently.
- Current business status accounting alignment (`2026-04-28`): Admin manual business status report context now uses repository actual-economics rows for gross, fees, buyer shipping, label spend, and net before falling back to raw sale fields, keeping Slack/email status reports aligned with dashboard and Reports profit math.
- Current AI accounting context resilience (`2026-04-28`): Ask/AI accounting context builders now roll back the repository session when actual-economics or FIFO cost-map lookups fail, so fallback snapshots do not leave Reports/AI requests stuck in an aborted SQL transaction.
- Current tax reporting progress (`2026-04-28`): Reports now builds `Tax Exceptions / Advisor Review` rows alongside Tax Detail, flagging missing jurisdiction/rate/category evidence, facilitator channels included in local tax scope, exempt-category review needs, and taxable shipping on exempt items; the table is visible from Tax Drilldown and included in report exports for tax-advisor review. Reports also provides a `Download Tax Review Packet ZIP` with manifest, stable SHA-256 evidence hash, tax summary, tax by marketplace, tax detail, and tax exceptions CSVs; Accounting Close Packet ZIP now includes tax exception evidence too.
- Current tax governance progress (`2026-04-28`): Admin now includes a `Tax Profile + Sign-Off Tracker` for reusable jurisdiction/channel assumptions, effective-date metadata, shipping-taxability/exempt-category/facilitator settings, human-validation status, advisor evidence links, tax packet references, tax exception counts, CSV exports, and Go-Live Evidence Pack artifacts (`tax_profiles.csv`, `tax_reporting_signoffs.csv`).
- Current tax profile reporting progress (`2026-05-04`): Reports Tax Reporting Scope can apply the latest active Admin tax profiles to jurisdiction/rate/shipping/facilitator/exempt-category assumptions, warns when the selected profile still needs validation, stamps selected profile metadata into the Tax Review Packet manifest/evidence hash, includes tax reporting sign-off evidence/review in Tax Review and Accounting Close packets, records Tax Reporting Sign-Off audit evidence directly from the Tax Review Packet workflow, and reviews approved tax sign-offs against recalculated jurisdiction/profile, exception count, owner/date, advisor evidence, packet reference, and packet hash. Reports Copilot now has an explicit `tax_review_findings` output category, renders returned Tax Review Findings as readable bullets while preserving raw JSON, records a read-only `reports_copilot_review` audit event with prompt/data-scope hashes and cited tax/accounting row counts, supports accepted/edited/rejected outcome audit events, and exports AI review outcomes for close review; AI Accountant context/citations include Tax Reporting Sign-Off Review evidence too.
- Current Colorado SUTS reporting progress (`2026-05-18`): Reports Tax Drilldown now generates a Colorado SUTS upload workbook for any selected `YYYY-MM` filing month from the official `Upload Data` template using account `080390`, and the Reports control is now labeled/defaulted as `Load Shipping + Tax/SUTS Analytics (slower)` so the SUTS panel is visible by default. The export keeps A1 blank, writes account numbers as numeric cells, writes gross sales as text, appends both Golden `11-0042`/`110042` rows when selected (`STATE` with SUTS account `970074130001` and `LOCAL`/self-collected with a blank account allowed for SUTS-accepted zero filings unless a local account is assigned), supports multiple gross-sales rows so direct Golden sales can populate both the state-administered and self-collected Golden rows for the same month, zero-files both Golden rows for eBay/facilitator-only scoped months, warns if a nonzero Golden `LOCAL` gross-sales row has no account number because only blank-account zero filing has been observed as accepted, shows in-app guidance beside the Tax Marketplace Filter and SUTS panel that eBay/facilitator channels should stay unselected for normal SUTS remittance unless advisor-confirmed, adds a SUTS Scope Check table that separates reportable direct/local gross from excluded marketplace-facilitator gross for the selected filing month, prunes unselected template jurisdictions from the generated upload file to avoid noisy SUTS excluded-row warnings, validates selected rows for missing/ambiguous account evidence before download, defaults facilitator-only periods such as eBay-only months to no local tax-liability marketplace scope instead of silently including eBay, leaves deduction/exemption columns blank for advisor/local-code review, and bundles the generated SUTS XLSX into the Tax Review Packet with SHA-256 manifest/evidence-hash coverage.
- Current accounting governance progress (`2026-05-05`): Admin now includes an `Accounting Close Sign-Off Tracker` for accounting model acceptance and monthly close review, with owner/date/status/evidence fields, close period, close-readiness status, exception/blocker counts, period drift warning count, accounting packet reference, seed/quick-approve helpers, CSV export, Go-Live Evidence Pack inclusion (`accounting_close_signoffs.csv`), and readiness scoring metadata for missing model-acceptance sign-offs. Reports now includes latest accounting close sign-off evidence plus an `Accounting Close Sign-Off Review` in the Accounting Close Packet and AI Accountant context, comparing approved sign-offs against recalculated readiness, blocker count, drift warnings, AI review follow-up count, and packet/evidence references.
- Current AI Accountant progress (`2026-05-06`): Ask GoldenStackers now includes a role-gated `AI Accountant` agent/domain backed by `ai_accountant_use`, read-only accounting snapshots with cited Reports/sales/products/orders/accounting-exception/lot-allocation evidence, a dedicated accountant identity/system prompt, automatic accountant LLM chat for accounting/tax questions, default-on external web-research context (`ai_accountant_web_research_enabled`) for tax/accounting questions, setup-readiness warnings when web research is disabled or has invalid limit/timeout settings, and regression coverage for Admin runtime seed defaults (monitor timing plus web enabled/limit/timeout) plus default-on chat/Slack web-research attachment. Slack Ops now accepts a read-only `accountant ...` intent for interactive AI Accountant answers in Slack, and the monitor now generates deterministic `Questions to Answer` with reply prompts such as `accountant answer missing_cost_basis sale#3:` so operators can answer in Ask or Slack and keep the context tied to the accounting issue. Those Ask/Slack answers are now recorded as read-only `ai_accountant_answer` audit evidence, surfaced in AI Accountant snapshots and the dedicated workspace, annotated back onto matching questions as answered/applied/needs-more-info/obsolete/unanswered, included in evidence packets, tracked with read-only follow-up outcomes, cited in Ask/accounting snapshots, and fed back into monitor/action rows with replacement-answer prompts when needs-more-info or obsolete statuses still require follow-up; usable replacement answers suppress the older open follow-up prompt for the same target. Reports now includes a separate `Run AI Accountant Review` action that cites close-readiness checks, accounting period drift checks, accounting close sign-off review rows, AI review outcome rows, exceptions, lot allocation sources, fee/shipping evidence, tax review assumptions, selected tax profile evidence, tax reporting sign-offs, and Tax Reporting Sign-Off Review rows; it drafts human-review recommendations without direct writes or unsupported tax/legal conclusions, renders structured review sections as readable bullets while preserving raw JSON, tolerates fenced/prefaced JSON responses, records an `ai_chat` audit event with `event_type=ai_accountant_review`, explicit `tax` domain scope, deterministic prompt/data-scope hashes, packet evidence hashes, and compact cited row-count scope metadata, supports accepted/edited/rejected outcome audit events tied to the response hash, blocks close readiness when the latest Copilot/AI Accountant outcome still needs edits or was rejected, and now feeds edited/rejected AI Accountant outcomes back into the dedicated page and scheduled monitor as actionable follow-up rows until an accepted outcome is recorded. Migration `0059_ai_accountant_permission` backfills `ai_accountant_use` for existing `ops` and `admin` role-permission rows.
- Current Products workspace progress (`2026-04-28`): Products table and `Product Detail/Edit` now render as full-width inline sections instead of a narrow side panel, keeping the product edit, lot-linking, conversion, lifecycle, media, and repurchase workflows easier to operate while retaining the existing detail selector.
- Current accounting/reporting hardening focus (`2026-04-28`): continue verifying dashboard, Reports, Sales, Orders, lot P/L, Slack, exports, and AI Accountant snapshots against the same actual-economics and FIFO/lot COGS model, with special attention to mixed-lot repurchases, partial check-ins, label-spend source evidence, and exception visibility before close packets are treated as accountant-ready. Dashboard now includes an opt-in `Load Profit Basis Audit (slower)` table that traces recent sale net, FIFO COGS, before-return profit, COGS source, bundle flag, and FIFO evidence row count so profit drops can be reviewed sale by sale.
- Next accounting/tax hardening targets (`2026-04-28`): run a production-sample close review using the Accounting Close Packet and Accounting Close Sign-Off Tracker; production-sample validate tax profile/sign-off evidence with AI Accountant review; and continue validation of dashboard, Reports, QBO staging rows, Slack summaries, and AI context totals for the same period.
- Current close-period drift check progress (`2026-04-28`): Accounting Review now includes `Accounting Period Drift Checks`, comparing close-readiness totals against QBO sales/refund staging exports for sales count, gross sales, net before COGS, FIFO COGS, profit before returns, refund totals, return COGS reversal, and estimated profit after returns; the drift checks are included in the Accounting Close Packet.
- Current close arithmetic hardening (`2026-04-29`): Accounting Review now includes `Accounting Close Formula Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks prove core close math ties out (`net before COGS - FIFO COGS = profit before returns`, return profit impact, and estimated profit after returns) and block close-ready status if formula drift appears.
- Current sales component tie-out hardening (`2026-04-29`): Accounting Review now includes `Accounting Sales Component Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify Sales Detail count/gross/net tie to COGS & Margin Detail and that `gross + actual shipping charged - actual fees - actual label spend = actual net before COGS`; any tie-out warning blocks close-ready status.
- Current return tie-out hardening (`2026-04-29`): Accounting Review now includes `Accounting Return Tie-Out Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify Returns refund totals tie to QuickBooks refund/adjustment staging, return COGS reversal ties to close readiness, and staged return profit impact equals `-refund total + COGS reversal`; any tie-out warning blocks close-ready status.
- Current inventory valuation hardening (`2026-04-29`): Accounting Review now includes `Accounting Inventory Valuation Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify stocked inventory rows have landed cost, Inventory Snapshot value equals `qty on hand * landed unit cost`, and close-readiness inventory value ties to the Inventory Snapshot; any valuation warning blocks close-ready status.
- Current fee evidence hardening (`2026-04-29`): Accounting Review now includes `Accounting Fee Evidence Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify eBay Fee Reconciliation row/fee totals and Fee Source Priority rows tie to Sales Detail, and flag sale-field fee fallback rows; any fee-evidence warning blocks close-ready status.
- Current shipping evidence hardening (`2026-04-29`): Accounting Review now includes `Accounting Shipping Evidence Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify shipping charged, label spend, and shipping delta tie across Sales Detail and Shipping Economics detail/summary rows, and flag paid-shipping rows missing label spend; any shipping-evidence warning blocks close-ready status.
- Current reconciliation tie-out hardening (`2026-04-29`): Accounting Review now includes `Accounting Reconciliation Tie-Out Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify Reconciliation by Marketplace sales/return counts and totals tie to Sales Detail, Returns, net-after-returns formulas, and close reconciliation flags; any reconciliation tie-out warning blocks close-ready status.
- Current COGS source hardening (`2026-04-29`): Accounting Review now includes `Accounting COGS Source Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify Sold COGS Source Summary sale count, quantity, FIFO COGS, and profit before returns tie to COGS & Margin Detail and close readiness, while blocking close-ready status when sold COGS uses equal fallback or missing/unknown basis.
- Current lot allocation hardening (`2026-04-29`): Accounting Review now includes `Accounting Lot Allocation Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify Lot Allocation Source Summary assignment count, quantity, and resolved landed cost tie to Lot Assignment detail and close readiness, while blocking close-ready status when lot assignments still use equal fallback or missing/unknown basis.
- Current exception queue hardening (`2026-04-29`): Accounting Review now includes `Accounting Exception Queue Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify Accounting Exception Queue total/P0/P1 counts tie to close readiness, flag malformed exception rows, and keep any P0 exception visibly blocking close-ready status.
- Current margin anomaly hardening (`2026-04-29`): Accounting Review now includes `Accounting Margin Anomaly Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify negative/nonpositive `COGS & Margin Detail` rows tie to close readiness and `nonpositive_margin` exception evidence, while blocking close-ready status when margin anomalies remain unresolved.
- Current close consistency hardening (`2026-04-29`): Accounting Review now includes `Accounting Close Consistency Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify final close-readiness status, blocker/warning counts, blocker/warning text, and close-check fail/warn rows agree before sign-off evidence is trusted.
- Current close packet completeness hardening (`2026-04-29`): Accounting Review now includes `Accounting Close Packet Completeness Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify required close-packet evidence artifacts are present and populated before sign-off evidence is trusted.
- Current close packet return evidence hardening (`2026-05-01`): Accounting Close Packet Completeness Checks now require `qbo_adjustments_export.csv` when refund/return activity exists, while no-activity optional artifacts no longer warn just because they are absent.
- Current close packet manifest hardening (`2026-04-29`): Accounting Review now includes `Accounting Close Packet Manifest Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks verify selected close-packet artifact prefixes are present in the export list and that manifest row-count values match exported dataframe row counts.
- Current close packet hash hardening (`2026-04-29`): Accounting Review now includes `Accounting Close Packet Hash Checks`, included in Reports Copilot, AI Accountant context/citations, and the Accounting Close Packet. These checks add SHA-256 CSV hashes to the close packet manifest and verify selected close-packet artifacts have hash evidence for integrity review.
- Current close packet evidence hash hardening (`2026-04-29`): Accounting Close Packet manifests now include `accounting_close_packet_evidence_hash_sha256`, a stable SHA-256 fingerprint derived from the selected close CSV payloads, date range, and close summary so sign-off records can reference a deterministic packet evidence hash.
- Current accounting sign-off hash hardening (`2026-04-30`): Admin Accounting Close Sign-Off Tracker now captures the Accounting Close Packet evidence hash, and Reports `Accounting Close Sign-Off Review` compares approved sign-off hashes against the recalculated packet evidence hash to flag stale packet evidence before close approval is trusted.
- Current close packet hash UX (`2026-05-05`): Reports now shows and exports an `Accounting Close Packet Evidence Hash` table and includes an in-page `Record Accounting Close Sign-Off` form so reviewers can capture owner/date/status, packet ref, evidence link, recalculated readiness/blocker/exception/drift counts, AI review follow-up count, and the current `accounting_close_packet_evidence_hash_sha256` without leaving the close packet workflow.
- Current accounting sign-off completeness (`2026-04-30`): Reports `Accounting Close Sign-Off Review` now warns when an approved close sign-off has a packet reference but no matching packet evidence hash, so approvals without deterministic hash evidence remain visible before close sign-off is trusted.
- Current accounting sign-off exception tie-out (`2026-04-30`): Reports `Accounting Close Sign-Off Review` now compares approved sign-off `exception_count` against recalculated close `total_exceptions`, flagging stale approvals when exception totals no longer match.
- Current accounting sign-off identity evidence (`2026-04-30`): Reports `Accounting Close Sign-Off Review` now warns when an approved close sign-off is missing owner or sign-off date evidence, so monthly close approvals stay tied to an accountable reviewer.
- Current accounting sign-off date validation (`2026-04-30`): Reports `Accounting Close Sign-Off Review` now validates approved sign-off dates are parseable, not before the close period end, and not future-dated before close approval evidence is trusted.
- Current dashboard drift check progress (`2026-04-28`): Accounting Period Drift Checks now also compare against Dashboard Live Metrics 30-day totals when the selected Reports window matches the dashboard 30-day window ending on `To Date`, covering sales count, gross, net before COGS, FIFO COGS, and estimated profit.
- Current dashboard shipping drift hardening (`2026-04-30`): Accounting Close Readiness now carries shipping charged, label spend, and shipping delta totals, and Accounting Period Drift Checks compare those components against Dashboard Live Metrics 30-day shipping values so dashboard profit drift can be traced to shipping evidence directly.
- Current close shipping formula hardening (`2026-05-01`): Accounting Formula Checks now validate close-summary `shipping_delta_total` from `shipping_charged_total - shipping_label_spend_total`, and formula warnings continue to block close-ready status.
- Current close net formula hardening (`2026-05-01`): Accounting Formula Checks now validate close-summary `net_before_cogs` from `gross_sales + shipping_charged_total - fee_total - shipping_label_spend_total`, so top-level sales profit math is gated before close sign-off.
- Current dashboard fee drift hardening (`2026-04-30`): Accounting Close Readiness now carries actual fee totals from Fee Source Priority evidence, and Accounting Period Drift Checks compare the close-period fee total against Dashboard Live Metrics 30-day fee totals.
- Current dashboard formula hardening (`2026-04-30`): Accounting Period Drift Checks now validate Dashboard Live Metrics 30-day formulas for net (`gross + shipping charged - fees - label spend`), shipping delta, profit before returns, and estimated profit after returns before dashboard totals are trusted in close review.
- Current close-readiness profit field clarity (`2026-05-14`): Accounting Close Readiness now exposes explicit `profit_before_returns` and `estimated_profit_after_returns` summary fields alongside legacy `fifo_margin` and `net_after_returns_and_cogs`, and period drift checks prefer the clearer fields while falling back to legacy keys for older evidence.
- Current close-readiness downstream profit clarity (`2026-05-14`): Reports close-readiness UI metrics, Reports Copilot context, and AI Accountant review context now read the explicit close-summary profit fields first, with legacy fallback retained only for older packet/evidence compatibility.
- Current QBO sales formula hardening (`2026-05-13`): Accounting Period Drift Checks now validate QuickBooks Sales Export formulas for `net_amount` (`amount + shipping_cost - fees - shipping_label_cost`) and `profit_before_returns_estimate` (`net_amount - cogs_input_estimate`) before QBO staging totals are trusted in close review. The legacy `gross_margin_estimate` export field remains populated for compatibility.
- Current QBO return formula hardening (`2026-05-01`): Accounting Period Drift Checks now validate QuickBooks Refund/Adjustment Export `estimated_profit_impact` from refund components and COGS reversal before return adjustment totals are trusted in close review.
- Current Slack drift check progress (`2026-04-28`): Accounting Period Drift Checks now include Slack-style daily/weekly business summary comparisons when the selected Reports range matches the business summary window ending on `To Date`, covering sales count, gross, net before COGS, FIFO COGS, and estimated profit.
- Current AI drift review progress (`2026-04-28`): Accounting Period Drift Checks now include Ask/AI accounting snapshot 30-day totals when the selected Reports window matches the AI/dashboard 30-day window ending on `To Date`. Reports Copilot and AI Accountant also receive `Accounting Period Drift Checks` in their structured context; AI Accountant audit citations include the drift-check table so human review can trace dashboard/QBO/AI mismatch recommendations back to the same close-period evidence.
- Current Slack/AI formula hardening (`2026-05-13`): Accounting Period Drift Checks now validate Slack-style and Ask/AI accounting snapshot `profit_before_returns` formulas from `net_window - cogs_window` and tie out `estimated_profit_after_returns` separately before summary/snapshot totals are trusted in close review.
- Current Slack/AI legacy metric compatibility (`2026-05-14`): Accounting Period Drift Checks now accept legacy Slack/Ask-AI `estimated_margin` snapshot evidence as before-return profit when newer `profit_before_returns` fields are absent, while labeling the observed source so old audit evidence does not create false close-review drift.
- Current order analytics progress (`2026-04-30`): Added an `Order Map` page that plots shipped order destinations with offline state/country centroid pins, date/marketplace/status filtering, mapped order/revenue metrics, aggregate destination detail tables, and CSV export without storing or geocoding street addresses. The map defaults to orders with shipment evidence or shipped/in-transit status, includes an opt-in toggle for unshipped/paid orders, and shows marketplace/status counts per destination.
- Current close-readiness drift gate progress (`2026-04-28`): any Accounting Period Drift Check warning now adds a `Period Drift Warnings` close-readiness failure and blocks close-ready status until the mismatched totals are resolved or reviewed.
- Current eBay listing hardening (`2026-04-28`): Listing Wizard and Listings publish/revise now use eBay Taxonomy item-specific requirements by category, cache normalized requirements in `ebay_category_aspects`, auto-hydrate cached requirements on category selection, and block readiness/publish when cached required specifics are missing. Both flows also include explicit `Main eBay Image` selection; the selected image is ordered first in eBay `imageUrls`, and publish/revise/direct-post success persists selected main-image metadata back into `marketplace_details.ebay_publish` for auditability. Direct-post dependency preflight and eBay Ops relist guardrails now detect immediate-payment policies and block live auction publish unless an `Auction Buy It Now` price is present, preventing eBay `25003` publish failures after offer creation. Publish calls now retry eBay `25604 Product not found` responses after probing the SKU inventory item, covering Inventory API eventual-consistency immediately after inventory upsert/offer creation. EPS image hosting now stays eBay-hosted only: transient Media API upload/import failures are retried, URL import is used only as another EPS hosting path, and publish/revise/direct-post fails before inventory publish if any selected image cannot be hosted by eBay. Listing Wizard direct post now uploads the first attached MP4 video to eBay Media, waits for LIVE status, attaches the returned `videoIds` to the Inventory item, and records video upload metadata on the local listing; Listings publish/revise also auto-selects the first supported video by default so wizard-created drafts keep their video when published later. MOV/QuickTime videos are converted to eBay-required MP4/AVC inside the app container with `ffmpeg` before upload. Video uploads now use eBay's required `application/octet-stream` upload content type, and both wizard direct post and Listings publish/revise verify the inventory item retained `product.videoIds` before offer creation/publish; Listings inventory fallback preserves selected video IDs instead of silently dropping them. Video diagnostics now re-check inventory `videoIds` after offer create/update and after live publish so eBay-side video drops are captured in draft/listing metadata with the exact stage, and the wizard/Listings UI exposes those video diagnostics even when publish technically succeeds. Live-publish video verification now also calls Trading API `GetItem` and confirms `Item.VideoDetails.VideoID`, which is eBay's seller-visible proof that the listing itself has the video attached. Video upload enabled with no linked/selected supported MP4/MOV video now records a warning and continues publishing image-only instead of blocking; Listings also warns and continues image-only when a selected video is missing or unsupported.
- Current eBay category condition hardening (`2026-05-14`): Listing Wizard and Listings can load eBay Sell Metadata `get_item_condition_policies` for the selected category, map returned condition IDs to Inventory API condition enums, restrict condition dropdowns to category-valid values, auto-correct invalid saved defaults when loaded, and block publish/direct-post when a loaded policy proves the selected condition is invalid. Publish paths also attempt a just-in-time category condition lookup before inventory upsert, and the shared eBay dependency preflight now validates the selected condition against the live category policy so saved presets or cached UI state cannot publish a category-invalid condition that would trigger eBay `25021`.
- Current eBay category state hardening (`2026-05-14`): Listing Wizard and Listings now keep product/listing-scoped category ID shadow state so Streamlit button reruns for suggestions, required specifics, and category conditions preserve the selected/manual category without leaking a prior item's category into another listing.
- Current eBay condition-description hardening (`2026-05-14`): Listing Wizard and Listings now show the eBay `Condition Description` character count, block readiness/publish when it exceeds 1,000 characters, and the eBay inventory client rejects overlong `conditionDescription` payloads before making an API call.
- Current Listings media performance hardening (`2026-05-14`): Listings media tables now render DB metadata first and defer preview galleries plus file-access/download byte loading behind explicit slower checkboxes. Shared media previews prefer stored media URLs so the browser streams image/video previews instead of forcing the Streamlit server to fetch every file on each rerun; byte fetches are cached for selected downloads, publish uploads, and fallback previews. Listing Wizard existing-media preview also prefers URLs and reuses a per-render product-media row cache. Listings now reuses selected-listing media rows per rerun so the media manager, publish workspace image/video selectors, and batch publish path do not repeatedly fetch the same listing media rows; count-only side-panel views keep using scalar counts unless media rows are already cached. Publish workspace media selection no longer depends on loading the separate Listing Media Manager first.
- Current private S3 media preview hardening (`2026-05-14`): shared media galleries, media file actions, and Listing Wizard selected-media previews now render private S3 assets through backend-loaded bytes instead of handing raw S3 URLs to the browser. Direct URL preview fallback is limited to URLs that do not look like S3 object URLs, preventing broken previews for private buckets.
- Current Listings working-set performance (`2026-05-14`): Listings now defaults to a recent bounded working set with a configurable row limit and an explicit `Load All Listings (slower)` toggle for full-history filtering/export. Repository listing loads support a bounded `limit` query so the page does not have to hydrate every historical listing for normal daily work.
- Current Listings product-load performance (`2026-05-14`): Listings product selectors now default to a recent bounded product working set with an explicit `Load All Products (slower)` toggle. The page also loads any missing product rows referenced by the current listing working set, keeping detail/publish panels accurate without hydrating every historical product by default.
- Current Listings publish-workspace performance (`2026-05-14`): Listings no longer auto-queries cached eBay category item specifics merely because the publish workspace opened or category state changed. Required-specific cache reads now happen when the operator loads item specifics or when preflight/publish needs validation, and publish/revise dependency preflight now consistently includes the selected condition in its cache signature/API validation.
- Current Listing Wizard publish performance (`2026-05-14`): Listing Wizard no longer auto-loads cached eBay category item specifics on category state changes. The wizard reads cached required specifics only when the operator loads item-specific controls or when direct eBay preflight/post validation needs blockers, keeping category changes lighter without losing required-specific protection. The quick suggested item-specific defaults preview is now opt-in, avoiding duplicate default-merge/table rendering on ordinary title/details edits.
- Current Listing Wizard lookup performance (`2026-05-14`): Listing Wizard no longer scans every listing for product duplicate checks, external listing owner checks, or last-created draft reloads. Repository helpers now fetch listing-by-id, product-scoped listings, and external-ID ownership directly so wizard reruns stay bounded as listing history grows.
- Current Listing Wizard product search (`2026-05-11`): Step 1 now keeps the Product dropdown capped to recent products by default (`listing_wizard_recent_product_limit`, default `75`) and adds SKU/title/category/material/ID search that merges older matches into the dropdown while preserving the currently selected or saved-draft product.
- Current Listing Wizard runtime coverage (`2026-05-11`): `listing_wizard_recent_product_limit` is now part of the seeded Admin runtime defaults and required runtime coverage so environments can tune the Step 1 dropdown cap without creating an untracked custom setting.
- Current Listing Wizard product-limit migration (`2026-05-11`): migration `0067_listing_wizard_recent_product_limit` backfills `listing_wizard_recent_product_limit=75` for existing runtime environments while preserving any nonblank operator override.
- Current lot/bundle listing foundation (`2026-05-11`): Listing Wizard and Listings create flow now support a product lot/bundle mode where one marketplace listing unit can contain multiple units of the selected product, for example one eBay listing for a lot of 10 coins. The flows store bundle composition metadata, warn/block when committed units exceed selected-product stock, keep marketplace quantity as available lots, and adjust expected-net COGS to include units per lot.
- Added next v1.0 scope: `GS-V10-020 Accounting Verification + AI Accountant` (formal cost-basis double-check, accounting exception queue, and role-gated AI accountant assistant for read-only review plus approval-gated adjustment proposals).
- Added next v1.0 scope: `GS-V10-021 Tax Reporting + Guidance` (tax review workspace, monthly/quarterly tax packet exports, tax exceptions, saved tax profiles, tax sign-offs, and role-gated AI tax/accounting guidance that stays advisory and tax-advisor validated).
- Added next v1.0 scope: `GS-V10-017 Lifecycle Close/Archive Controls` (listings/products/lots/media archive/restore workflow with auditability and guardrails).
- Current GS-V10-017 progress: Listings/Products/Lots/Media now support archive/restore lifecycle controls (with linked-record guardrails for products/lots/media and force-confirmed override paths); archived visibility filters are in primary Media and Search/Edit flows; scheduled retention cleanup covers archived media/listings/lots/products with dependency-aware skip logic; and Admin now includes lifecycle retention controls, persisted manual-run evidence, and a Dev/Prod `Lifecycle Retention Policy Sign-Off Tracker` with CSV/go-live evidence-pack integration. Coverage now includes admin go-live e2e assertions for lifecycle sign-off surfaces and admin helper regression guardrails for lifecycle evidence-pack artifact wiring.
- Added next v1.0 scope: `GS-V10-018 App Performance Optimization` (slow-page load reduction through DB/query tuning, Streamlit render decomposition, lazy hydration, and measurable page-performance budgets/evidence).
- Current GS-V10-018 progress (latest): extensive lazy-hydration and query reuse has been applied across Listings/Admin/Reports/System Health/Search-Edit/Intake; Listings import-chain stability was hardened after a cross-page `NameError` regression (fixed invalid model type reference and validated view-module imports in the runtime container); Admin Go-Live diagnostics now reuses a single 30-day integration-event query with in-memory 7d/24h windows to reduce duplicate SQL reads; Listings publish/relist external-ID collision checks now use a precomputed owner map for constant-time lookups instead of repeated listing scans; Listings bulk draft creation now reuses the page-level `product_by_id` map (removing duplicate per-rerun product map reconstruction); Listings/Orders filter-state handling was hardened to prevent invalid multiselect defaults and default-vs-session-state widget conflicts; Listings media hydration is now aggregate-query based (`listing_media_count_map`) and relationship-driven `listing.product` / `listing.media_assets` N+1 access was removed from readiness/history/batch-plan loops, media-count aggregate loading is now truly deferred unless `Load Listing Media Counts (slower)` is enabled, side-panel deferred media counts now use repository scalar count helper (`count_media_assets_for_listing`) instead of row hydration, template profile loading is now explicit opt-in (`Load eBay Listing Templates (slower)`), readiness scoring/queue analytics are now explicit opt-in (`Load eBay Readiness Queue (slower)`), readiness/bulk-publish preset-default resolution is now lazy (no unconditional preset query on default load), channel adapter bootstrap is now lazy (only when orchestration/capability workflows need it), create-flow readiness preview/preset resolution is now explicit opt-in (`Load Create-Flow eBay Readiness Preview (slower)`), workspace feedback/completion telemetry is now explicit opt-in (`Load Listings Workflow Telemetry (slower)`), bulk draft creator candidate hydration is now explicit opt-in (`Load Bulk Draft Creator Data (slower)`), eBay store profile context/payload hydration is now explicit opt-in (`Load eBay Store Profile Context (slower)`), create-flow media capture widgets are now explicit opt-in (`Load Create Listing Media Capture (slower)`), external listing owner-map construction is now lazy/on-demand for publish/relist collision checks, and parsed `ebay_publish` metadata is now cached per listing per rerun across table/readiness/publish paths; heavy history scans now sit behind a master gate (`Load Deep Queue Analytics (slower)`) before follow-up/bulk-history panels can run, side-panel related context now uses explicit gate (`Load Side Panel Context (slower)`), Listing Media Manager hydration now uses explicit gate (`Load Listing Media Manager Data (slower)`), and the lower publish/revise/preflight panel now uses explicit opt-in (`Load eBay Publish Workspace (slower)`); Dashboard now defers grouped eBay fee-type attribution behind explicit opt-in (`Load eBay Fee Attribution Breakdown (slower)`) using repository `dashboard_live_metrics(..., include_fee_type_breakdown=False)` by default; System Health now reuses per-render cached integration-event audit reads (24h/7d/14d/cooldown windows) instead of issuing duplicate audit-log queries per section; Intake Wizards now resolve existing-media attachments by selected media IDs (`list_media_assets_by_ids`), use ID-only unlinked-product lookup (`list_unlinked_product_media_ids`) when attaching product media to newly created draft listings, cap selector media reads at DB level via repository `limit` support, reuse preloaded media selector rows across both selector panels in the same rerun to avoid duplicate DB reads, perform media attach mutations via single-transaction bulk update helper (`bulk_update_media_assets`) rather than per-row commits, and render AI diagnostics in compact mode by default with explicit opt-in full payload rendering; Reports now uses a unified bounded-render helper across detail tables (including Rebuy Cost Trend), adds dedicated lazy-load gates for inventory cycle/rebuy analytics, and includes helper-level regression tests; Media Library now defaults to preview-bounded table rendering with opt-in full-table mode for large asset sets; and targeted GS‑V10‑018 guardrail tests now cover listings helper optimized paths, reports facilitator tax-scope defaults, and system-health page/read baseline normalization semantics.
- GS-V10-018 incremental update (2026-04-19): Listings `Format Issue Only` now auto-enables format diagnostics for the current run when diagnostics were deferred, preventing false-empty/incorrect filtered results from toggle sequencing.
- GS-V10-018 incremental update (2026-04-19): Listings now applies base filters before hydrating eBay format diagnostics, so metadata parse work is limited to the filtered working set instead of all listings.
- GS-V10-018 incremental update (2026-04-19): Listings readiness/publish paths now reuse the page-level listing-id map in the same rerun instead of rebuilding duplicate indexes.
- GS-V10-018 incremental update (2026-04-19): added Listings helper guardrail tests for base filtering, diagnostics hydration gating, and format-issue-only filtering to lock the new deferred diagnostics behavior.
- GS-V10-018 incremental update (2026-04-19): Listings now hydrates non-queue format diagnostics preview-first when full table rendering is off (while keeping full filtered-set hydration for `Format Issue Only` correctness).
- GS-V10-018 incremental update (2026-04-19): Listings side-panel/media-manager selectors now reuse the existing page-level listing-id map, removing duplicate same-rerun listing index construction.
- GS-V10-018 incremental update (2026-04-19): fixed photo-comp lineage helper fallback semantics so explicit empty audit-row inputs do not trigger unintended audit-log queries; added guardrail coverage for that path.
- GS-V10-018 incremental update (2026-04-19): deferred origin-tag row mutation to explicit photo-comp origin load mode and normalized missing-origin filter semantics (`other`) for consistent low-cost default behavior.
- GS-V10-018 incremental update (2026-04-19): Listings base filtering now runs object-first and materializes listing table rows only for filtered results, reducing default rerun allocation cost on larger listing sets.
- GS-V10-018 incremental update (2026-04-19): Listings row-action controls are now explicit opt-in (`Load Listing Row Actions (slower)`), reducing default UI/control hydration overhead for large filtered tables.
- GS-V10-018 incremental update (2026-04-19): heavy Listings table exports now defer CSV/XLSX byte generation behind explicit opt-in (`Load Table Exports (slower)`), reducing default rerun overhead.
- GS-V10-018 incremental update (2026-04-19): shared table-toolbar deferred export behavior is now covered by dedicated view tests (skip-by-default + explicit-load paths), and shared view test doubles were aligned with media selector `limit` API usage.
- GS-V10-018 incremental update (2026-04-19): Listings detail selector/editor panel is now explicit opt-in (`Load Listing Detail Panel (slower)`), reducing default selector-map/widget hydration work on large filtered result sets.
- GS-V10-018 incremental update (2026-04-19): Listings media-manager deferred mode now skips upload widgets and listing-media query/gallery/file-action hydration entirely until explicitly enabled.
- GS-V10-018 incremental update (2026-04-19): main Listings table now materializes preview-only DataFrames in default preview mode and lazily builds full filtered export DataFrames only when export hydration is requested.
- GS-V10-018 incremental update (2026-04-19): Listings readiness/history now defers heavy selector-option map hydration for bulk-review and retry-failed controls behind explicit opt-in toggles.
- GS-V10-018 incremental update (2026-04-19): Listings now builds the media-count aggregate listing-ID vector only when media-count hydration is explicitly enabled, trimming default rerun overhead.
- GS-V10-018 incremental update (2026-04-19): Listings orchestration queue is now explicit opt-in (`Load Listing Orchestration Queue (slower)`), deferring orchestration-status derivation and queue hydration by default.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish planner is now explicit opt-in (`Load Bulk Publish Batch Planner (slower)`), deferring publish-candidate selector maps and dry-run batch-planning state hydration by default.
- GS-V10-018 incremental update (2026-04-19): Listings reviewer analytics panel is now explicit opt-in (`Load Reviewer Dashboard (slower)`), deferring pending/approval KPI and reviewer-group summary hydration by default.
- GS-V10-018 incremental update (2026-04-19): Listings blocker follow-up workbench is now explicit opt-in (`Load Blocker Follow-up Workbench (slower)`), deferring follow-up task creation controls plus follow-up history/preset hydration on default readiness loads.
- GS-V10-018 incremental update (2026-04-19): Listings readiness blocker/warning breakdown tables are now explicit opt-in (`Load Readiness Breakdown Tables (slower)`), deferring breakdown DataFrame rendering while preserving blocker/warning counts for filters and quick actions.
- GS-V10-018 incremental update (2026-04-19): Listings top-blocker quick actions are now explicit opt-in (`Load Top Blocker Actions (slower)`), deferring blocker quick-filter buttons and follow-up action branch hydration on default readiness loads.
- GS-V10-018 incremental update (2026-04-19): Listings readiness triage shortcut/filter widget cluster is now explicit opt-in (`Load Readiness Filters + Shortcuts (slower)`), so default readiness queue rendering avoids shortcut button + multi-filter widget hydration.
- GS-V10-018 incremental update (2026-04-19): Listings readiness queue table/toolbar rendering is now explicit opt-in (`Load Readiness Queue Table (slower)`), deferring queue table hydration and export toolbar work on default readiness loads.
- GS-V10-018 incremental update (2026-04-19): Listings readiness scoring row-build loop is now explicit opt-in (`Load Readiness Evaluation (slower)`), deferring per-listing readiness evaluation and preventing misleading empty-state messaging when evaluation is intentionally deferred.
- GS-V10-018 incremental update (2026-04-19): Listings orchestration queue now hard-gates on readiness evaluation state; when evaluation is deferred it short-circuits with a dependency prompt instead of entering orchestration derivation paths.
- GS-V10-018 incremental update (2026-04-19): orchestration dependency-caption logic is now centralized in a pure helper with dedicated Listings helper tests, locking expected readiness/orchestration dependency messaging across deferred states.
- GS-V10-018 incremental update (2026-04-19): Listings readiness preset/runtime default resolution now runs only when readiness evaluation is enabled, trimming default readiness-load setting/preset reads when scoring is deferred.
- GS-V10-018 incremental update (2026-04-19): Bulk Publish Execution History now defers failed-row extraction and retry-control hydration behind explicit opt-in (`Load Bulk Publish Retry Analysis (slower)`), reducing default history-load DataFrame/filter work.
- GS-V10-018 incremental update (2026-04-19): Bulk Publish Execution History now uses preview-first rendering with lazy full filtered export factory and row-count-aware toolbar wiring; retry analysis now runs against full filtered rows rather than preview slice.
- GS-V10-018 incremental update (2026-04-19): Channel Capability Matrix now uses preview-first rendering with row-count-aware toolbar + lazy full export factory, reducing default in-page table/render overhead while preserving full exportability.
- GS-V10-018 incremental update (2026-04-19): Listings side-panel selector now defaults to lightweight ID picker (`Select Listing ID`) and defers heavy title/marketplace label-map hydration behind explicit opt-in (`Load Detailed Listing Selector Labels (slower)`).
- GS-V10-018 incremental update (2026-04-19): Listings Document Draft source selection now defers rich sale/order label formatting behind explicit opt-in (`Load Detailed Document Source Labels (slower)`), defaulting to lightweight ID labels for large related sets.
- GS-V10-018 incremental update (2026-04-19): Listings Document Draft source picker now uses preview-limited options with explicit opt-in full list loading (`Load Full Document Source List (slower)`), including session-state guardrails when truncated option sets change.
- GS-V10-018 incremental update (2026-04-19): Listings side-panel review history now uses preview-first rendering with row-count-aware toolbar + lazy full export factory, reducing default side-panel table/render overhead while preserving full history exportability.
- GS-V10-018 incremental update (2026-04-19): Listings side-panel review actions/history are now gated behind explicit opt-in (`Load Review Controls (slower)`), keeping detail-edit workflows lighter by default when review controls are not needed.
- GS-V10-018 incremental update (2026-04-19): Listings Media Manager table now uses preview-first rendering with row-count-aware toolbar + lazy full export factory, reducing default large-media table payload while preserving full export capability.
- GS-V10-018 incremental update (2026-04-19): Listings eBay publish workspace now defers workflow-draft retrieval and preset profile hydration behind explicit opt-in (`Load Publish Draft + Presets (slower)`), keeping manual publish flows lighter by default.
- GS-V10-018 incremental update (2026-04-19): Listings eBay category assist is now explicit opt-in (`Load eBay Category Assist (slower)`), deferring suggestion-map/control hydration and taxonomy cache/API query paths unless requested.
- GS-V10-018 incremental update (2026-04-19): Listings item-specifics default-aspect preview/apply path is now explicit opt-in (`Load Suggested Default Aspects (slower)`), while manual item-specifics editing remains always available.
- GS-V10-018 incremental update (2026-04-19): Listings sanitized-HTML preview now sanitizes on-demand only when preview is enabled, eliminating duplicate per-rerun sanitization work while keeping publish-time sanitization unchanged.
- GS-V10-018 incremental update (2026-04-19): Listings publish/revise flows now reuse cached dependency preflight results when token/policy/category/location inputs are unchanged, reducing duplicate eBay dependency-check calls after manual preflight runs.
- GS-V10-018 incremental update (2026-04-19): Listings now resolves merged eBay item-specific defaults lazily (only when revise/publish payload construction needs aspects), removing always-on merge work from non-submit reruns.
- GS-V10-018 incremental update (2026-04-19): Listings HTML validation is now lazy (`_get_listing_html_errors`) and only executes when revise/publish paths need validation, removing always-on per-rerun HTML checks.
- GS-V10-018 incremental update (2026-04-19): Listings publish-description sanitization is now lazy (`_get_effective_listing_description_and_notes`) and resolves only in revise/publish action paths, removing always-on sanitize work from non-submit reruns.
- GS-V10-018 incremental update (2026-04-19): Listings publish workspace now reuses existing `publish_meta` cache for discovered offer ID and last-publish diagnostics, removing duplicate marketplace-details JSON parse passes in the same rerun.
- GS-V10-018 incremental update (2026-04-19): Listings publish-workspace draft autosave writes are now gated by `Load Publish Draft + Presets (slower)`, reducing default rerun workflow-draft write activity when draft/preset tooling is not in use.
- GS-V10-018 incremental update (2026-04-19): Listings eBay fee estimate metrics are now explicit opt-in (`Load eBay Fee Estimate Assist (slower)`) with lazy estimate computation reused for publish metadata, removing always-on fee-estimation work from default reruns.
- GS-V10-018 incremental update (2026-04-19): Listings volume-pricing parsing is now lazy via `_get_volume_pricing_tiers_and_errors`, with explicit opt-in preview (`Load Volume Pricing Preview (slower)`) and on-demand parse in revise/publish paths; this also fixes the prior early-reference flow hazard around `include_volume_desc`.
- GS-V10-018 incremental update (2026-04-19): Listings publish diagnostics cards (dependency preflight card + last publish error diagnostics) are now explicit opt-in (`Load eBay Diagnostics Cards (slower)`), reducing default publish-workspace diagnostics rendering overhead.
- GS-V10-018 incremental update (2026-04-19): Listings category-assist seed/session initialization now runs only when `Load eBay Category Assist (slower)` is enabled, removing default rerun category-assist state setup work.
- GS-V10-018 incremental update (2026-04-19): Listings volume-pricing builder controls are now explicit opt-in (`Load Volume Pricing Builder (slower)`), reducing default publish-workspace widget hydration while preserving submit-path volume-pricing behavior.
- GS-V10-018 incremental update (2026-04-19): Listings “Manage Existing eBay Listing” widget tree is now explicit opt-in (`Load Manage Existing eBay Listing Controls (slower)`), reducing default publish-workspace form/inspector hydration cost.
- GS-V10-018 incremental update (2026-04-19): Listings item-specific JSON normalization is now lazy/shared (`_get_aspects_payload`) across editor + publish paths, avoiding always-on payload parse work in publish-workspace reruns when specifics are untouched.
- GS-V10-018 incremental update (2026-04-19): Listings publish workspace runtime defaults now hydrate via one bulk runtime-settings snapshot (`get_runtime_values`) instead of many per-key runtime-setting reads, reducing repeated DB lookup overhead on publish-workspace reruns.
- GS-V10-018 incremental update (2026-04-19): Listings offer-details expander rendering in publish workspace is now tied to `Load Manage Existing eBay Listing Controls (slower)`, so deferred manage mode skips stale offer-details card hydration.
- GS-V10-018 incremental update (2026-04-19): Listings format-template apply actions now reuse the same bulk runtime-default snapshot (best-offer/auction/category/policy/currency/lang defaults), removing additional per-click runtime-setting query fan-out.
- GS-V10-018 incremental update (2026-04-19): Listings publish workspace now seeds default category from the bulk runtime-default snapshot and reuses it during listing-signature resets, removing the remaining per-key runtime category lookup in publish-state initialization.
- GS-V10-018 incremental update (2026-04-19): Listings manage-offer resolution now caches SKU offer lookups and resolved offer IDs within the rerun, reducing duplicate `get_offers` API calls when inspecting and then acting on the same listing.
- GS-V10-018 incremental update (2026-04-19): Listings now caches resolved merchant location keys per rerun (`token` + `merchantLocationKey`) and reuses them across preflight/revise/publish paths, reducing duplicate `resolve_merchant_location_key` API calls in the same workflow run.
- GS-V10-018 incremental update (2026-04-19): Listings publish-workspace draft autosave now applies a short debounce window (5s) for changed signatures, reducing rapid repeated `save_workflow_draft` writes during active form editing while preserving periodic autosave behavior.
- GS-V10-018 incremental update (2026-04-19): Listings readiness/bulk-publish default resolution now hydrates eBay runtime defaults through a rerun-local bulk snapshot (`get_runtime_values`) instead of multiple per-key runtime-setting reads, reducing repeated DB lookups in readiness planner flows.
- GS-V10-018 incremental update (2026-04-19): Listings readiness bulk-publish defaults now use rerun-local memoization (`_resolve_bulk_publish_defaults`) so repeated planner/history action branches reuse one resolved defaults payload instead of recomputing it in the same rerun.
- GS-V10-018 incremental update (2026-04-19): Listings preflight helper now returns the computed preflight signature and call sites reuse it for session-state persistence, removing duplicate signature recomputation across preflight submit/revise/publish branches.
- GS-V10-018 incremental update (2026-04-19): Listings publish/revise media selection now uses shared rerun-local resolvers for selected images/videos, removing duplicate label-to-object mapping work across revise and publish branches.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish + retry branches now share one rerun-local resolver for `ebay_allow_sandbox_seller_ops`, removing duplicate runtime-setting lookups in the same readiness/history run.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history hydration now scans only active eBay listings for `publish_batch_execution` payloads (instead of all listings), reducing metadata parse and row-build cost on large mixed-marketplace datasets.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history scan now short-circuits rows whose raw `marketplace_details` do not contain `publish_batch_execution` before JSON parse, reducing unnecessary details parsing overhead.
- GS-V10-018 incremental update (2026-04-19): Retry Failed History now builds retry selector maps directly from failed-row dicts (no intermediate DataFrame/`iterrows` pass), reducing pandas overhead in retry-analysis flows.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history row builder now hoists per-listing constants (listing id/sku/title/url) outside event loops, reducing repeated lookup work per event row.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history filtering now applies batch/executor filters in one combined pass, reducing duplicate list traversal in history filter flows.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history now short-circuits heavy table/export/retry-analysis hydration when current filters produce zero rows, avoiding unnecessary DataFrame and retry-control setup work.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history filter loop now computes normalized batch/executor values once per row in a single-pass evaluator, removing repeated normalization calls from filter matching.
- GS-V10-018 incremental update (2026-04-19): Listings orchestration queue now applies status filters on row dicts before DataFrame materialization, reducing DataFrame filter/copy overhead in orchestration queue rendering.
- GS-V10-018 incremental update (2026-04-19): Listings orchestration queue now applies selected status filtering inline during row construction, reducing intermediate row-set size for filtered queue views.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history filter evaluator now fast-paths blank filters by reusing sorted rows directly, avoiding per-row normalization work when no batch/executor filters are set.
- GS-V10-018 incremental update (2026-04-19): Listings bulk publish history now fast-paths 0–1 row cases by skipping `sorted(...)` overhead and reusing the existing row list directly.
- GS-V10-018 incremental update (2026-04-19): Listings side-panel review history now fast-paths tiny sets (0–1 rows) by skipping `sorted(...)` and reusing the existing row list directly.
- GS-V10-018 incremental update (2026-04-19): Listings side-panel review history, channel capability matrix, and media-manager export paths now reuse already-built render DataFrames when full-table mode is enabled, avoiding duplicate full DataFrame construction in export factories.
- GS-V10-018 incremental update (2026-04-19): Main Listings filtered export now reuses the already-built table DataFrame in full-table mode, avoiding duplicate full DataFrame construction in default export paths.
- GS-V10-018 incremental update (2026-04-19): Bulk publish history export now reuses the already-built history DataFrame in full-table mode, and blocker follow-up table/export now reuses a single DataFrame instance instead of rebuilding twice per rerun.
- GS-V10-018 incremental update (2026-04-19): Readiness bulk-review and bulk-publish planner option maps now share one cached readiness-records list (`to_dict('records')`) per rerun instead of two separate `iterrows()` passes over the DataFrame.
- GS-V10-018 incremental update (2026-04-19): Readiness blocker/warning filter option lists now reuse one pre-sorted key snapshot per rerun, eliminating duplicate sort passes in follow-up + readiness filter controls.
- GS-V10-018 incremental update (2026-04-19): Readiness queue filtering now reuses precomputed format/blocker/warning string Series in filter branches, removing repeated `astype(str)`/`str.upper()`/`str.contains()` setup work within the same rerun.
- GS-V10-018 incremental update (2026-04-19): Readiness queue filtering now applies one consolidated boolean mask and performs a single final DataFrame slice, reducing intermediate filtered DataFrame allocations in filter-heavy reruns.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up controls now build status/owner/priority/SLA option sets in one pass over task rows (instead of four separate set-comprehension scans).
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up filtered-task pass now also computes open/due-soon/overdue metrics and open-task label map in the same loop, removing extra post-filter list/dict scans.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up history now fast-paths tiny sets (0–1 rows) by skipping sort work for both event-order and final task-order phases.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up history loop now normalizes event action once per event and reuses it for status/last-action updates, removing repeated action-string normalization in the same pass.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up preset-map build now resolves default-preset labels during the initial map-build pass, removing a second pass over preset labels.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up preset action buttons now reuse one selected preset tuple (`row`, `payload`) per rerun, eliminating repeated `preset_map.get(...)` lookups across apply/default/clear/delete actions.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up quick-preset buttons now reuse precomputed option sets (`status_values`/`owner_values`/`priority_values`/`sla_values`) for membership checks, avoiding repeated list membership scans.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up preset-map loop now normalizes user/preset flags (`username`, `is_shared`, `is_default`) once per row and reuses those values for label/default resolution, reducing repeated attribute normalization in the same pass.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up default preset application now reuses the already-fetched default tuple payload (no extra map lookup), and preset label sorting now fast-paths tiny sets (0–1 labels) by skipping `sorted(...)`.
- GS-V10-018 incremental update (2026-04-19): Blocker follow-up option-list derivation (`status`/`owner`/`priority`/`sla`) now fast-paths tiny sets (0–1 values) by skipping `sorted(...)` calls.
- GS-V10-018 incremental update (2026-04-19): Listings marketplace/status/category/listing-ID option-list derivation now fast-paths tiny sets (0–1 values) by skipping `sorted(...)` calls in filter, bulk-create, and media-manager selectors.
- GS-V10-018 incremental update (2026-04-19): Listings now fast-paths tiny key sets for audit-log cache limit reuse and create-flow store-profile options (skip `sorted(...)` on 0–1 key sets).
- GS-V10-018 incremental update (2026-04-19): Listings now fast-paths tiny sets in additional hot controls: active-filter marketplace/status normalization, blocker/warning option derivation, top-blocker quick-filter ranking, and bulk-draft candidate product sorting.
- GS-V10-018 incremental update (2026-04-19): Listings item-specifics preview/default rendering now reuses prebuilt key/item lists (with tiny-set sort fast paths) instead of repeating `sorted(...)` calls in the same rerun.
- GS-V10-018 incremental update (2026-04-19): Listings item-specifics remove-aspect selector now reuses a prebuilt aspect-key list with tiny-set sort fast path (skip `sorted(...)` on 0–1 keys).
- GS-V10-018 incremental update (2026-04-19): Listings document-source side panel now fast-paths related sale/order sorting for tiny sets (0–1 rows), skipping unnecessary sort work during source option hydration.
- GS-V10-018 incremental update (2026-04-19): Listings audit-log cache reuse now resolves the smallest reusable cached limit via single-pass candidate scan (no cache-key sorting), reducing repeated key-sort overhead on audit-heavy reruns.
- GS-V10-018 incremental update (2026-04-19): Listings publish-preset save path now reuses a prebuilt lowercase-name preset map for O(1) existing-preset lookup (replacing per-save linear scan across preset rows).
- GS-V10-018 incremental update (2026-04-19): Listings create-flow default-preset resolution now uses a single pass over cached preset rows (first row fallback + early-break on explicit default) instead of multiple scans.
- GS-V10-018 incremental update (2026-04-19): Listings publish-preset UI now builds both label->preset and lowercase-name->preset maps in one pass over preset rows (eliminating duplicate row iteration).
- GS-V10-018 incremental update (2026-04-19): Listings readiness default publish-preset resolver now uses a single-pass fallback/default selection over cached preset rows (first-row fallback + early-break on explicit default).
- GS-V10-018 incremental update (2026-04-19): Listings volume-pricing tier normalization now skips sort work for tiny sets (0–1 tiers) before duplicate/min-qty validation.
- GS-V10-018 incremental update (2026-04-19): Listings item-specific default-aspect apply flow now reuses already-sorted `default_only_items` keys for flash messaging, removing a duplicate per-rerun key-sort pass.
- GS-V10-018 incremental update (2026-04-19): Listings item-specific editor now reuses sorted `aspects_preview_items` for both preview rendering and remove-selector options, removing duplicate key sorting in the same rerun.
- GS-V10-018 incremental update (2026-04-19): Listings `_audit_logs_for_entity` now caches filtered entity rows by `(entity_type, limit)` per rerun, avoiding repeated post-filter scans over the same audit-log slice.
- GS-V10-018 incremental update (2026-04-19): Listings saved-filter marketplace/status normalization now uses single-pass loops with one normalization per value (instead of repeated `str(...).strip()` calls in set comprehensions).
- GS-V10-018 incremental update (2026-04-19): Listings audit-log cache now memoizes reusable-limit slices into exact-limit cache entries, reducing repeated slice work for the same requested audit limits.
- GS-V10-018 incremental update (2026-04-19): Listings entity-audit cache now also reuses higher cached limits for the same entity type and memoizes exact-limit slices, reducing repeated filtering/slicing across nearby limits.
- GS-V10-018 incremental update (2026-04-19): Listings item-specific remove-aspect options now derive directly from already-built `aspects_preview_items`, removing an extra fallback normalization/sort branch.
- GS-V10-018 incremental update (2026-04-19): Listings audit cache loops now use typed cache keys directly (removed redundant `int(...)`/`str(...)` conversions in reusable-limit scans).
- GS-V10-018 incremental update (2026-04-19): Listings audit cache writes now avoid redundant `list(...)` copies when memoizing reusable-limit slices and filtered entity rows.
- GS-V10-018 closeout recorded on `2026-04-18`: performance baseline capture/export workflow, first-wave heavy-page optimization scope, and targeted guardrail tests are complete.
- eBay OAuth reliability hardening (latest): sync runner now performs proactive user-token auto-refresh (runtime-configurable cadence + near-expiry threshold), persists true token expiry metadata (`expires_at = now + expires_in`) plus refresh timestamp, applies refresh-failure cooldown guardrails, emits `ebay_oauth/auto_refresh` integration events, optionally dispatches Slack alerts for refresh failures, and exposes Admin diagnostics + one-click failure-state clear controls for faster operator recovery.
- System Health now includes a compact `eBay OAuth Auto-Refresh` status card (status/next due/expiry/cooldown + last error) and integration-row signal, with runtime-drift seed coverage for all OAuth auto-refresh keys.
- System Health `DB Rollup Latency Baseline` now includes `Run Rollup EXPLAIN Snapshot` for key rollup surfaces (dashboard/shipping/tax/fee reconciliation), with planning/execution timing capture, plan-text drilldown, and CSV export for performance evidence.
- Rollup EXPLAIN snapshots are now persisted as `integration_event` telemetry (`system_health/rollup_explain_baseline_snapshot`) and surfaced in `Recent Rollup EXPLAIN Snapshots (14d)` with slowest-rollup summary and history CSV export.
- Reports now uses repository DB rollup `report_ebay_marketplace_fee_rows` for eBay fee-detail analytics before legacy payload parsing fallback, reducing heavy in-memory order JSON parsing on large windows.
- Reports reconciliation-by-marketplace now prefers repository DB rollup `report_marketplace_reconciliation_rows` (sales/orders/returns grouped in SQL) with legacy in-memory fallback, reducing per-marketplace Python loops on large date windows.
- Reports order-level actual economics allocation now prefers repository DB rollup `report_sales_actual_econ_rows` (windowed fee/shipping allocation by sale/order) with legacy fallback, reducing heavy Python sibling-order allocation loops.
- Reports extended analytics now also prefer repository DB rollups for listing review activity (`report_listing_review_activity_rows`) and listing format outcomes (`report_listing_format_outcome_rows`) before legacy in-memory helper fallback.
- Reports rebuy cost trend now prefers repository DB rollup `report_rebuy_cost_trend_rows` (targeted column reads + de-duped acquisition events) before legacy helper fallback.
- Reports `Inventory Cycles (Rebuy/Resell)` now prefers repository DB rollup `report_inventory_cycle_rows` (SQL-backed movement/sales sourcing with helper fallback) to reduce extended analytics object hydration overhead.
- Reports now includes `Inventory Cycle Summary by SKU` export/view (open vs closed cycle counts, qty/sales/net/known-cost margin rollups, avg closed-cycle duration) derived from cycle rows for faster operating review.
- Reports `Inventory Movements` now prefers repository DB rollup `report_inventory_movement_rows` (date-bounded SQL read) and only hydrates full movement objects for fallback paths.
- Reports `Orders` and `Order Items` now prefer repository DB rollups (`report_orders_rows`, `report_order_items_rows`) for date-bounded exports/views; full `OrderItem` object hydration is now fallback-only.
- Reports now also uses `report_orders_rows` to build report-window order context for downstream fee/shipping reconciliations; full `Order` object hydration is fallback-only when rollup data is unavailable.
- Reports `Sales` now prefers repository DB rollup `report_sales_rows` for date-bounded report windows; full `Sale` object hydration is now fallback-only unless needed by compatibility fallbacks.
- Reports `Products` and `Listings` now prefer repository DB rollups (`report_products_rows`, `report_listings_rows`) for date-bounded report windows; full object hydration is fallback-only when extended analytics compatibility paths require model objects.
- Reports `Returns` (and QuickBooks refund/export rows) now prefer repository DB rollup `report_returns_rows` for date-bounded reporting; full return-object hydration is now fallback-only.
- Reports `Lot Assignment` now prefers repository DB rollup `report_lot_assignment_rows` for date-bounded reporting; full assignment-object hydration is now fallback-only for this report surface.
- Reports sale-cost map derivation now prefers repository DB rollup `report_sale_unit_cost_maps` (time-aware FIFO unit-cost by sale, lot-weighted unit-cost by product, and FIFO remaining unit cost by product) before helper fallback, reducing full assignment hydration pressure in large report windows.
- Reports fallback hydration for `Inventory Movements` and `Lot Assignments` is now lazy-loaded only at the exact fallback branches that require model objects (instead of eager preload), reducing first-render memory and query load when DB rollups are available.
- Reports fallback hydration for full `Sales` objects is now also lazy-loaded (only when rollup is unavailable, cost-map helper fallback is needed, or cycle helper fallback is active), removing another eager large-object preload from normal rollup paths.
- Reports fallback hydration for full `Products` and `Listings` objects is now also lazy-loaded at branch level (inventory/rebuy/listing-review helper fallback paths), removing additional eager preload pressure from default rollup-first runs.
- Reports fallback hydration for full `Orders`, `Order Items`, and `Returns` objects is now also lazy-loaded and only executed in explicit rollup-fallback branches, removing the remaining upfront list preloads on rollup-first report runs.
- Reports `Shipping Economics Summary` coverage metrics now use a single grouped coverage pass + merge (instead of per-row dataframe scans), reducing compute cost on larger shipping datasets.
- Reports margin percentage calculations (`by SKU`, `by Marketplace`, `by Period`) now use vectorized column math via a shared helper instead of row-wise `apply`, reducing CPU overhead for large reporting windows.
- Reports eBay fee-reconciliation UI now reuses cached aggregate summaries and fee-source counts (`_summarize_fee_reconciliation`, `_fee_source_priority_counts`) for KPI cards and source-priority metrics, avoiding repeated ad-hoc recomputation/filtering.
- Reports Copilot context top-N slices now use direct `nlargest`/`nsmallest` helpers (`_top_n_records`, `_top_n_by_abs_records`) instead of full-frame `sort_values` pipelines, reducing temporary dataframe work during Copilot analysis runs.
- Reports now builds a per-run scalar cache (`report_scalar_cache`) for repeated KPI/context totals (shipping sums/coverage, COGS margin totals, reconcile-flag counts, table row counts) so values are computed once and reused across UI cards and Copilot context.
- Reports rollup loading paths now use a shared helper (`_load_rollup_rows`) for consistent `report_*_rows` try/fallback handling across products/listings/sales/orders/order_items/returns/movements/lot-assignment datasets, reducing view complexity without changing behavior.
- Added `st.cache_data` memoization for deterministic fee-source analytics transforms in Reports (`_build_fee_source_priority_summary`, `_build_fee_source_priority_trend`, `_build_normalized_source_weekly_coverage`, `_build_weekly_fee_source_count_chart_data`) to reduce rerun latency on unchanged inputs.
- Added deterministic `st.cache_data` helpers for Tax Drilldown filtering and sale-option label generation (`_filter_tax_drilldown_rows`, `_build_tax_drilldown_sale_option_rows`) to improve responsiveness when toggling drilldown controls.
- Added deterministic `st.cache_data` helpers for `Document Draft Handoff` source-option generation from sales/orders (`_build_documents_handoff_sale_option_rows`, `_build_documents_handoff_order_option_rows`) to reduce rerun work when toggling handoff source/doc controls.
- Added deterministic cached Tax Drilldown KPI helper (`_tax_drilldown_kpis`) so row/taxable-subtotal/estimated-tax cards reuse one computed summary per filtered drilldown slice.
- Listings publish-form state hardening (latest): category apply and item-specific/default-aspect actions now preserve full publish-form state across reruns (including category/pricing/offer/aspects and related fields) and set one-run signature-reset skip to prevent action-triggered clobbering.
- Legacy Finding diagnostics remain available for troubleshooting older paths, but active comp workflows no longer depend on `findCompletedItems`.
- Legacy eBay Finding controls were removed from the Admin Sync Jobs UI; active comp paths are HTML/web-based.
- Listing grading/condition prefill parity (latest): Listing Wizard and Listings publish/edit now both default condition description from product AI grading context and append grading notes into listing details (once only), with inline prefill-status UX and helper-level regression coverage.
- Added next v1.0 scope: `GS-V10-019 Slack AI Ops Bot` (Slack-connected AI assistant for image-driven intake/comps and approval-gated operational actions executed in-app with full auditability).
- Current GS-V10-019 progress (Step 1): implemented `app/services/slack_ops_bot.py` with canonical Slack command envelope/idempotency keying, intent/argument/file normalization, command allowlist + per-role intent mapping, and audited routing outcomes (`accepted`/`denied`/`rejected`) with integration telemetry; added first routing guardrail tests in `tests/test_slack_ops_bot.py`.
- Current GS-V10-019 progress (Step 2): added Slack inbound ingestion queueing in `app/services/slack_ops_bot.py` (`ingest_slack_command_request`) with replay/idempotency dedupe against existing `integration_queue_jobs`, full requester/channel/thread context capture in queued payloads, queue enqueue telemetry/audit events, and expanded coverage in `tests/test_slack_ops_bot.py`.
- Current GS-V10-019 progress (Step 3): added Slack attachment ingestion execution path in `app/services/integration_queue.py` for `slack_ops/command_ingest`, including bot-token-authenticated Slack file download, S3 upload via existing media storage service, and persistence into existing `media_asset` / `purchase_document` records with optional target context hints (`product/listing/lot/source/document_kind`); added queue ingestion regression coverage in `tests/test_integration_queue.py`.
- Current GS-V10-019 progress (Step 4): added approval-gated write-intent workflow in `app/services/slack_ops_bot.py` so write commands can be auto-held in queue `blocked` state pending in-app approval (`slack_ops_write_actions_require_approval=true`), with explicit role-validated approval transition helper (`approve_slack_ops_queue_job`) that promotes approved jobs back to `queued`; added regression coverage in `tests/test_slack_ops_bot.py`.
- Current GS-V10-019 progress (Step 5): integrated AI runtime into Slack-origin `comp`/`intake` queue execution in `app/services/integration_queue.py` (`slack_ops/command_ingest`), routing through workflow-scoped AI orchestration (`workflow=\"comp\"` / `workflow=\"intake\"`), persisting concise operator-safe AI output + contextual record links into queue payload (`ai_response`), and adding optional runtime-gated Slack auto-reply support; added regression coverage in `tests/test_integration_queue.py`.
- Current GS-V10-019 progress (Step 6): added Slack ops runtime guardrails in `app/services/slack_ops_bot.py` (global kill switch, per-intent toggles, channel/user allowlists, and rolling request rate limits), with explicit audited/telemetry rejection outcomes for blocked requests and regression coverage in `tests/test_slack_ops_bot.py`.
- Current GS-V10-019 progress (Step 7 partial): added Admin Integration observability/governance surface for Slack Ops queue (`Slack Ops Queue (Bot)`) with live queue status metrics, pending-approval SLA metrics, parsed approval metadata table, and triage actions (`run due`, `retry failed`, `approve pending`), with helper-level regression coverage in `tests/test_admin_helpers.py`.
- Current GS-V10-019 progress (Step 7 complete): System Health now includes Slack Ops queue rollup KPIs/trends (queue depth/status, pending-approval SLA, and 24h success/queued/failed/rejected event counters) plus queue preview, with helper-level regression coverage in `tests/test_system_health_helpers.py`.
- Current GS-V10-019 progress (Step 8 closeout): added `SLACK_OPS_RUNBOOK.md` (runtime controls, incident/rollback/escalation procedures) and added end-to-end approval-gated lifecycle regression coverage in `tests/test_slack_ops_bot.py` (`ingest -> blocked -> approve -> process -> success`), plus dedupe hardening for previously successful jobs.
- Current GS-V10-019 progress (Step 9): added inbound Slack Socket Mode runner (`app/services/slack_ops_runner.py`) to handle `app_mention` and slash-command ingress directly from Slack, enqueue via `ingest_slack_command_request`, reply in-thread with acceptance/denial/approval state, and auto-process due `slack_ops` queue jobs. Added `slack_ops_worker` compose service plus helper tests in `tests/test_slack_ops_runner.py`.
- Current GS-V10-019 progress (comp quality hardening): tightened structured web-price extraction to ignore generic raw `"price"` token scraping and JSON-LD range keys (`lowPrice`/`highPrice`), reducing category-page promo-threshold contamination (for example `orders $199+`) in Slack comp fallback pricing.
- Current GS-V10-019 progress (comp quality hardening): added evidence-gated comp summary normalization for inline AI field chains (confidence/suggested-range/recommendation consistency), switched gate metadata to reason-specific fetch-mode tags (`evidence_gate_rows|confidence|confidence_rows`), and dampened confidence labels for single-row/low-diversity evidence so one-source comps do not surface as `high` confidence.
- Current GS-V10-019 progress (comp quality hardening): comp header now includes `Qualified comps` and `Distinct sources`, Slack links now include `Min qualified comps` when threshold overrides are not set, and one-source/single-row evidence now explicitly reads as directional via gate-aligned summary fields.

Core stack:
- Python + Streamlit (employee/admin UI)
- PostgreSQL (inventory/listing/sales/media metadata)
- AWS S3 (images/videos for inventory + listings)
- Alembic (database migrations)
- Docker (local development)
- Kubernetes + Kustomize overlays (dev/prod)

## AI-First Product Standard

AI is a first-class layer across workflows (intake, listings, comps, shipping/sync triage, and admin tooling), not an isolated utility.

- New workflow scope should include in-context AI assistance where it adds operator value.
- AI suggestions are advisory by default; write actions require explicit user approval.
- AI runtime remains admin-configurable at runtime (provider/model/profile/fallback) without redeploy.
- AI runtime supports workflow-specific profile preference runtime keys (`ai_workflow_profile_listing`, `ai_workflow_profile_intake`, `ai_workflow_profile_comp`, `ai_workflow_profile_risk`, `ai_workflow_profile_accounting`) for per-flow model routing.
- Admin AI Runtime now exposes `Workflow AI Profile Routing` controls to set/clear those workflow profile mappings in-app.
- AI-assisted decisions should preserve traceability (confidence/citations/audit metadata).

## Workflow State Reliability (v1.0 Scope)

To improve stability of long multi-step operator flows, we are standardizing on DB-backed workflow state for business-critical draft/progress data.

- Use DB-backed workflow drafts/events for business state (listing drafts, intake progress, eBay setup progress).
- Keep `st.session_state` limited to UI-transient state (panel expansion, tab focus, one-run flash controls).
- Prioritized rollout order: `Listing Wizard` -> `Listings publish/edit` -> `eBay Workspace` -> `Intake Wizards`.
- Target outcome: rerun/restart-safe resume behavior with fewer widget-key/session mutation failures.

Operator runbook:
- Use `Save Draft` before risky actions (publish, AI apply, bulk aspect updates) and before leaving a page.
- Use `Resume Draft` after browser refresh/app restart or when switching devices.
- Use `Clear Draft` only when intentionally discarding in-progress work.
- Treat local/browser capture buffers as non-resumable across restart; re-upload media after resume when warned.
- For Listing Wizard/Listings: if preflight blockers reappear after resume, run dependency preflight again and save draft once the form is corrected.
- For eBay Workspace: after OAuth callbacks or token refresh, save setup draft once values are stable so environment-specific setup survives reruns.

Admin operations runbook:
- Use `Admin -> Governance Exports -> Workflow State Governance` to review active drafts and recent workflow events.
- Export workflow drafts/events CSV before manual cleanup when auditing operator incidents.
- Run retention cleanup with conservative windows first, verify deleted counts, then tighten to policy targets.
- Capture cleanup runs in go-live evidence when validating restart-safe workflows and retention hygiene.

Workflow-state retention policy (current target):
- Drafts: keep active/in-progress drafts until explicit clear or expiry; expired active drafts are cleanup candidates.
- Events: keep short-term event history for operator traceability, then prune via retention cleanup.
- Session state: UI-only fields may reset at rerun; business-critical state should be expected in DB drafts.

Current implementation status:
- DB schema + APIs shipped for workflow state:
  - tables: `workflow_drafts`, `workflow_events`
  - migration: `0044_workflow_state`
  - repository methods: `load_workflow_draft`, `resume_latest_workflow_draft`, `save_workflow_draft`, `clear_workflow_draft`, `append_workflow_event`
- Listing Wizard pilot shipped:
  - explicit `Save Workflow Draft`, `Resume Saved Draft`, `Clear Workflow Draft`
  - autosave on tracked wizard-state changes
  - resume restores saved product/template selection from draft payload
- Listings eBay publish/edit flow (phase 2, in progress):
  - per-listing draft scope with `Save/Resume/Clear Publish Draft`
  - autosave for publish/edit state payload
  - safer category/aspects apply behavior via pending-update + rerun pattern
- eBay Workspace auth/setup flow (phase 3, in progress):
  - scoped `Save/Resume/Clear Setup Draft` controls for workspace auth/setup state
  - autosave for setup controls (token/filters/store policies/location/create-location/shipping/package defaults)
  - date-range resume coercion to avoid widget type mismatch on restore
- Intake wizards (phase 4, initial pass):
  - Coin Intake + Inventory Intake now support `Save/Resume/Clear Draft` and autosave for key-backed AI/prefill/buffered-media state
  - core long-form intake fields now use stable keyed state and are included in draft payload resume
  - nested helper state (media selector/search and capture-mode controls) is now included
  - intake AI actions (identifier/grader/comp/purchase-doc extraction) now execute with `workflow=intake` runtime profile preference
  - intake AI prefill now blocks weak/generic title and low-detail description suggestions before writing wizard defaults
  - intake AI prefill now force-applies identifier/grader/comp outputs into form keys before widget instantiation to prevent stale/no-update behavior
  - inventory intake grader normalization now falls back to raw grader JSON keys (`estimated_grade_range`/`recommendation_rationale`/`notes`) when structured-summary rendering is empty, and immediately syncs `AI Grading Description` + `AI_GRADED` form state after `Run Grader`
  - intake comp actions now gracefully handle eBay Finding cooldown/rate-limit responses (`EBAY_FINDING_RATE_LIMITED`) and continue with AI summary/web fallback paths instead of hard-failing the run
  - Inventory Intake and Coin Intake now always render eBay purchase ID/link inputs and only enable them when `Purchased On eBay` is checked, with submit-time required ID validation when enabled
  - identifier prefill mapping now supports alternate key shapes (`title`/`item_title`/`name`, `metal_type`/`composition`, `description`/`details`/`summary`) with normalized-text fallback for description
  - browser regression coverage added for intake AI prefill path (`tests/e2e/intake_prefill.spec.ts`), including Inventory Intake `Run Grader` -> `AI Grading Description` population assertion
  - binary/browser-local upload buffers are intentionally excluded from persisted drafts
  - resume now warns when previously buffered local media was part of the saved draft but is not resumable after restart/device change
- Workflow-state governance operations (phase 5, initial pass):
  - repository support for workflow draft listing + retention cleanup (`list_workflow_drafts`, `cleanup_workflow_state`)
  - Admin Governance Exports now includes workflow drafts/events preview, CSV export, and manual cleanup controls
- Listing workflow contract unification (GS-V10-013 step 1):
  - shared listing draft contract helper (`app/services/workflow_contracts.py`) now defines common `contract/signature/context/state` payload shape
  - Listing Wizard and Listings publish/edit draft save/resume now use the same contract with legacy payload fallback compatibility
  - readiness context persistence: Listings + Listing Wizard now include dependency preflight payloads in saved drafts so blockers/warnings/check summaries survive resume
  - Listing Wizard AI business state (`suggestions/diagnostics/acceptance/evidence/has_run`) and risk summary are now persisted in workflow drafts for restart-safe resume
  - Category-query seeded context/suggestions and AI seed/seed-context fields are now persisted in listing drafts to keep category/AI behavior deterministic after resume
  - Listings publish/edit now uses a centralized persisted draft key contract (`LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS`) so business fields stay DB-backed and only UI-transient keys remain session-only
- Draft parity regression suite now covers Wizard/Listings category/aspects/pricing/post-mode paths (`tests/test_listing_workflow_draft_parity.py`)
- Strict Listings browser gate now covers publish-draft workflow controls (`Save -> Resume -> Clear`) plus eBay preflight checks (`tests/e2e/listings_flow_strict.spec.ts`); latest strict seeded evidence run on 2026-04-16 passed (`2/2`).

## Current Functional Scope

- Product catalog with SKU-level tracking
- Product SKU generator in UI
- Marketplace listings (eBay first, but supports multiple channels)
- Sales recording with fees/shipping/net
- Listing URL/details tracking per marketplace
- Shipping and tracking fields (provider, service, tracking number, status, shipped/delivered dates)
- Product package dimensions for shipping workflows
- Dedicated Shipping page with operational queues, carrier presets, and bulk status updates
- Shipping exception workflow notes/actions and resolution tracking
- Shipment export builder (carrier generic + Pirate Ship template) with export timestamp tracking
- Media library uploads (images/videos) to S3-compatible storage
- Direct media upload into listing workflows
- Direct media upload into product workflows
- Product and listing media preview galleries in-page
- One-step “Create eBay Listing From Product” workflow with optional media attachment
- Publish selected eBay marketplace listings from app to live eBay via Sell Inventory API (fixed-price or auction)
- User/environment-scoped eBay publish presets (policy/location/category defaults) for one-click apply
- eBay publish flow can push selected listing images to eBay EPS and upload one MP4/MOV listing video via eBay Media API, converting MOV/QuickTime to MP4 and recording video attachment diagnostics
- Manage existing eBay-linked listings in app with revise/end/relist actions (offer lifecycle controls)
- Unified eBay Workspace page (integration + ops tabs) with legacy eBay/eBay Ops compatibility routes
- Dedicated `eBay Templates` page for reusable template management so Listings stays focused on create/review/publish flow
- Dedicated `Listing Wizard` page (AI-first scaffold) for guided product->template->draft flow
- Purchase lots and product-to-lot assignment tracking
- Incoming purchase-document intake (PDF/image/camera) with immutable S3 original + checksum, linked to lot/product/source
- Purchase-document extraction modes: `LLM Multimodal`, `AWS Textract`, or merged `Both`
- Purchase-document detail actions for `Create Lot + Link` and single/bulk `Create Product + Link` from extracted line items
- Managed source master data (dealers/vendors/etc) for standardized lot intake
- Gram to Troy Oz calculator
- Spot-price melt value estimator with live quote fetch
- Reports page with CSV/XLSX exports
- Inventory movements report and dedicated movement ledger page
- QuickBooks-oriented sales export report
- Search/edit workflows for products, listings, sales, lots, and media
- Audit log for create/update activity tracking
- Orders domain with multi-line items and optional linked sale creation
- eBay order pull/import hydrates full order details per order before upsert for better buyer/shipping/fee fidelity
- eBay order sync now stores normalized buyer+ship-to fields and raw marketplace payload JSON on each order for reconciliation/debug workflows
- Returns workflow with refund/disposition tracking and optional restock handling
- Data-quality validation guards (required fields, duplicate IDs, tracking/amount sanity checks)
- Role-based access basics (`viewer`/`ops`/`admin`) with session identity adapter and write guardrails
- Admin page for managing users and role-permission mappings
- Password-capable app users (hashed+salted) with login/logout sidebar flow
- Global page auth gating when `APP_REQUIRE_PASSWORD_AUTH=true` (content blocked until sign-in)
- Optional persistent sign-in restore across app restarts via signed remember token (`APP_AUTH_SIGNING_KEY`, `APP_AUTH_REMEMBER_DAYS`). Browser-cookie mode is optional and off by default (`APP_AUTH_COOKIE_ENABLED=false`) due Streamlit component compatibility. Query-token URL fallback is runtime-configurable (`APP_AUTH_QUERY_TOKEN_FALLBACK_ENABLED`, runtime key `auth_query_token_fallback_enabled`).
- Cookie adapter modernization (GS‑V10‑010): auth cookie backend now uses `extra-streamlit-components` adapter path instead of `streamlit-cookies-manager`, reducing third-party `st.cache` deprecation exposure.
- Cookie adapter backend selection now only uses `extra-streamlit-components` (no deprecated `streamlit-cookies-manager` fallback path).
- Admin `Users -> Auth Session Debug` panel shows live remember/session/token restore state for troubleshooting.
- Generalized Inventory Intake Wizard for non-coin intake with optional draft-listing handoff
- Inventory + Coin intake wizards support attaching existing media assets (with filter + select-all controls) in addition to new uploads
- Camera capture panels across intake/tools/lots are collapsed by default to keep pages clean
- Invoices/receipts page with branded templates, print preview, and HTML export
- Saved document template profiles in DB with environment-specific defaults
- Default Classic invoice/receipt branding uses `app/images/logonewmed.jpg` (overridable per document)
- Default document identity uses `Golden Stackers LLC` with `https://goldenstackers.com`
- Documents tax controls include jurisdiction, tax mode (auto/manual/no-tax), shipping-taxable toggle, and tax row rendering
- Documents tax presets include `Golden Local Retail`, `Marketplace Shipped`, and `Bullion/Coin Exempt`
- Golden local tax defaults are codified at `7.50%` (CO `2.90` + Jefferson County `0.50` + Golden `3.00` + CD `0.10` + RTD `1.00`)
- Current tax settings can be saved back to runtime defaults from the Documents page
- Auto tax mode supports per-line taxable/exempt overrides for mixed invoices
- Reports include estimated tax outputs (`Tax Summary`, `Tax by Marketplace`, `Tax Detail`) scoped by date range, jurisdiction context, and selected marketplaces
- Reports default local tax-liability marketplace scope now excludes facilitator channels (default `ebay`) via runtime key `marketplace_facilitator_channels_csv`
- Reports tax presets include `Golden Local Retail`, `Marketplace Shipped`, and `Bullion Exempt Focus`
- Reports include Tax Drilldown filters (marketplace + taxable/exempt segment) with focused exports
- Reports Tax Drilldown includes `Open in Documents` handoff to prefill invoice draft context from a selected sale
- Reports include a general `Document Draft Handoff` panel to prefill Documents from either Sales or Orders (invoice/receipt)
- Sales and Orders side panels include direct `Open in Documents` actions for one-click invoice/receipt draft prefill
- eBay Workspace includes `Document Draft Quick Handoff` from recent eBay orders/sales into Documents

### eBay UX Consolidation
- Templates are now managed on a dedicated page: `eBay Templates`.
- Listings keeps quick template apply/load behavior but no longer carries full template CRUD panels.
- Listing Wizard scope is AI-first: in-flow AI drafting, readiness/policy preflight summaries, and explicit apply/accept controls before publish.
- Listing Wizard now runs a complete guided flow (`Product -> Template -> Policy/Format -> AI -> Preflight -> Preview -> Create`) with explicit step validation and guardrails.
- Listing Wizard now includes listing-mode controls (`Buy It Now`, `Auction`, `Auction + BIN`, `Store 30 days`), auction duration/start/reserve/BIN inputs, offer controls, media upload, and draft creation with eBay publish metadata persisted in listing details.
- Listing Wizard now supports direct single-listing eBay posting from Step 9 (`Post to eBay Immediately`) so operators can create+post without switching to Listings batch workflows.
- Listing Wizard now includes per-user `Wizard eBay Post Profiles` (save/load/default/delete) for policy/location/category/currency/condition defaults to keep direct posting one-click.
- Listing Wizard and Listings now support cache-first eBay category suggestions backed by DB, plus explicit `Refresh from eBay` when you want fresh remote suggestions.
- Listing Wizard and Listings now support cache-first eBay required item specifics by category (`ebay_category_aspects`), with explicit refresh from eBay Taxonomy and readiness/publish blockers for missing cached required specifics.
- Listing Wizard and Listings now include `Main eBay Image` selectors; the selected image is placed first in the eBay `imageUrls` payload.
- Listing Wizard now includes explicit quantity controls for fixed/store formats (`Buy It Now`, `Store 30 days`), while auction formats remain quantity=`1`.
- AI auto-draft in Listing Wizard uses product record context and available images (uploaded or existing product images) to prefill title/details/pricing/offer settings.
- Listing Wizard now includes explicit AI suggestion review/apply controls (field-level toggles) and an eBay readiness preflight card (status/score/blockers/warnings) before draft creation.
- Listing Wizard AI draft now fails gracefully: multimodal errors no longer crash the page, automatic text-only fallback is attempted, and an in-page `AI Runtime Diagnostics` panel shows mode/provider/model/source/endpoint plus fallback errors for operator triage.
- Listing Wizard now persists accepted AI suggestion metadata into draft listing details (`ai_draft` payload with suggestions, diagnostics lineage, acceptance actor/timestamp, and applied-field selections).
- Listing Wizard AI personality is now independently configurable in Admin (`AI Runtime -> Listing Wizard AI Prompt Templates`) via runtime keys `listing_wizard_ai_system_message`, `listing_wizard_ai_seed_default`, and `listing_wizard_ai_instruction_template`.
- Listing Wizard and Listings Copilot AI suggestion runs now execute with `workflow=listing` runtime profile preference.
- Listing Wizard and Listings now block weak/generic AI titles from overwriting existing listing titles.
- Listing Wizard `AI Seed Prompt` now defaults from runtime key `listing_wizard_ai_seed_default` (editable per-run; falls back to instruction template if needed).
- Listing Wizard now supports attaching existing product media to new drafts without duplicating S3 files (link existing media rows + optional new uploads).
- Listing Wizard now includes a compact selected-media preview toggle so operators can quickly verify chosen reusable assets before draft creation.
- Listing Wizard selected-media preview now uses a Streamlit-safe inline show/hide toggle (no nested expander usage).
- Listing Wizard AI now supports optional quick pricing context from eBay sold comps + spot quotes (runtime keys: `listing_wizard_ai_include_quick_comp_context`, `listing_wizard_ai_quick_comp_limit`).
- AI Runtime profile `Max Output Tokens` admin cap was raised to `16000` (from `4096`), and env fallback default `COMP_LLM_MAX_OUTPUT_TOKENS` is now `16000`.
- Admin `AI Runtime` now includes `Bulk Max Output Tokens Upgrade` with dry-run preview to raise existing profiles below a selected target (default `16000`) in one action.
- Listing Wizard AI suggestion review now surfaces explicit price-band outputs (`suggested_price_low`, `suggested_price_high`) with safe fallback derivation when only a single suggested price is returned.
- Listing Wizard AI price parsing now safely tolerates model outputs like `$12.99`, `12.99-15.99`, and `12.99 to 15.99` without runtime conversion failures.
- Listing Wizard and Listings Copilot now enforce AI details quality gates (reject empty/too-short/bullet-only details) and auto-generate enriched eBay-ready fallback details when AI returns weak copy.
- AI apply-time quality gates are now runtime-configurable in Admin (`AI Runtime -> AI Apply-Time Quality Gates`) with minimum title/details/intake thresholds plus policy-blocked terms (`ai_quality_*` runtime keys).
- Listing Wizard, Listings Copilot, and intake AI prefill now block policy-violating suggestion text (for example prohibited promise/advice phrases) before applying AI output.
- Listing Wizard and Listings Copilot now prioritize explicit AI detail keys (`suggested_details`, `suggested_description`, `description`, `details`) and ignore placeholder marketplace-label responses (for example, `eBay`) when building listing details.
- Listing Wizard and Listings publish/edit now keep AI grading context aligned: product `ai_grading_description` pre-populates condition description, and listing details get an `AI Grading Notes` section when available (without duplicate injection on rerun/apply).
- Listing Wizard now records AI acceptance telemetry (`accepted snapshot` + create-time `accepted_as_is` vs `edited_fields` outcome) in draft metadata and audit events for prompt-quality tuning.
- Admin now includes `AI Prompt Version Registry + Rollback` for `listing` and `comp` workflows (save version snapshots, restore prior versions, and set active version ids).
- Prompt registry runtime keys are now part of config-health governance (`ai_prompt_registry_*_json`, `ai_prompt_active_version_*`), and Listing Wizard acceptance telemetry includes the active prompt version id.
- Admin now includes `AI Quality Metrics` (lookback/workflow filtering, accepted-as-is vs edited outcome KPIs, prompt-version comparison, and CSV export) based on `ai_prompt_acceptance` telemetry.
- Admin `AI Quality Metrics` now includes daily acceptance trend visuals, workflow-specific daily drilldowns, and per-workflow top edited-field attribution to speed prompt tuning.
- Listing Wizard Step 7 now includes a consolidated `AI + Readiness Risk Summary` card (risk level, readiness score, and highlights from AI + preflight signals).
- Listing Wizard AI apply flow includes `Price Apply Mode` (`Low`, `Mid`, `High`) so operators can choose conservative vs aggressive price application from the AI price band.
- Listing Wizard AI can be constrained to use exactly the selected existing product media for draft context (`Use selected existing media for AI draft context`) to keep AI outputs aligned with chosen listing assets.
- Listing Wizard includes a compact `Preview Listing Draft` step before creation and now surfaces `Open Draft on eBay` when an external listing URL/ID is available for the most recently created draft.
- Listing Wizard preview now supports `Rendered HTML` and `Raw Source` modes for listing details so template HTML can be visually reviewed before draft creation.
- Listings publish now supports explicit post modes: `Publish Live Listing` and `Create Offer Draft Only` (for Seller Hub review before go-live).
- Listings and Listing Wizard now expose one-click eBay dependency preflight actions and persistent summary cards (pass/warn/fail + detailed checks) before publish/revise.
- eBay publish resiliency includes eBay-hosted EPS image retry/failure handling, one-time safer-payload retry for inventory `25001` transient/system errors, and video attachment verification through Inventory/Trading API diagnostics.
- Added helper-level test coverage for Listing Wizard parsing/sanitization and eBay template HTML block runtime merge/persistence (`tests/test_listing_wizard_helpers.py`, `tests/test_listings_helpers.py`).
- Listing Wizard now includes a `Stay on Wizard after Create` option so operators can remain in-flow for immediate post-create checks/links instead of auto-redirecting to Listings.
- Listing Wizard includes one-click text recovery actions: `Reapply Product/Template Defaults` and `Clear Title + Details`.
- Listing Wizard includes `Append Product Key Specs` to quickly inject SKU/category/metal/weight lines into listing details.
- Listing Wizard includes `Reset Wizard Inputs` to quickly return pricing/mode/offer/text fields to defaults and clear AI suggestion state for a fresh pass.
- Listing Wizard UI was tightened for linear operation: explicit `Step X of 9` labels, a top recommended flow hint, and a preflight progress bar.
- Listing Wizard now keeps advanced controls collapsed by default (`Advanced AI Prompt Controls`, `Advanced Reset`, and existing-media attach panel) to reduce first-pass clutter.
- Listing Wizard AI diagnostics/evidence panels are now behind an explicit `Show AI troubleshooting panels` toggle that appears only after an AI run.
- Listing Wizard AI Suggestions Review now defaults to compact title/details/offer previews with an opt-in `Show full AI suggestion payload` toggle for detailed JSON inspection.
- Listing Wizard now shows contextual `Next recommended action` hints after key stages (AI assist, preflight, preview, and create) to keep operators moving linearly.
- Listing Wizard now includes a compact `Wizard Status` strip near the top (product, template, mode, latest preflight blockers/warnings) for quick re-orientation.
- Listing Wizard preview step now auto-expands when preflight is fully clean (no blockers/warnings) and remains collapsed otherwise.
- Listing Wizard Step 6 now shows active AI runtime chain metadata (provider/model/source/fallback profile counts) so operators can verify runtime-configurable AI behavior before generating drafts.
- After successful draft creation, Listing Wizard clears selected-existing-media UI state to prevent stale media selections from carrying into the next draft attempt.
- Listing Wizard now warns when the selected product already has draft/active eBay listings and provides a one-click jump to Listings review queue to avoid accidental duplicate drafts.
- Listing Wizard now blocks duplicate draft creation by default when draft/active eBay listings already exist for the selected product; explicit override checkbox is required to proceed.
- Listing Wizard now also disables the `Create Draft Listing` button while duplicate guard is active and shows inline reason text until override is enabled.
- Listing Wizard now disables `Create Draft Listing` for all core unmet prerequisites (missing product/title, overlong title, preflight blockers, duplicate guard) and lists the exact reasons inline.
- Listing Wizard now clears prior AI suggestion/media-selection state when product/template context changes to prevent stale carryover between listing targets.
- Listing Wizard now shows live eBay title character count and enforces max title length (`80`) in preflight/create guardrails.
- Listing Wizard title field now includes one-click `Trim to 80` action for quick eBay-compliant title cleanup.
- Listing Wizard now validates offer thresholds: minimum must be `<=` auto-accept and both must be `<=` effective listing price/start price when offers are enabled.
- Listings and Listing Wizard now include simple volume-pricing discount builders (`Buy 2`, `Buy 3`, `Buy 4+` save %) with one-click tier generation, while keeping JSON editing available for advanced tier control.
- Listings and Listing Wizard now include upfront `Estimated eBay Fees` pricing-assist cards (gross/fees/net/fee%) with configurable assumptions and persisted estimate snapshots in `ebay_publish` metadata.
- Listings and Listing Wizard now omit `availableQuantity` for `AUCTION` offers to match eBay Inventory API contract and avoid auction publish `25762` failures.
- Listing Wizard direct-post now builds eBay offer payloads through a shared helper with explicit unit-test coverage for fixed-vs-auction payload contract behavior.
- Listing Wizard category source/fetch/refresh/profile-apply flows now use queued pending-field updates to avoid Streamlit post-widget session mutation errors on `listing_wizard_category_id`.
- Listing Wizard direct eBay post failures now keep the operator on the wizard and persist staged diagnostics on the created draft (`direct_post_last_error`, `direct_post_last_error_at`, `direct_post_last_error_stage`, `direct_post_last_context`).
- Listing Wizard now shows a `Last Direct Post Diagnostics` panel for the most recent created draft when direct post fails.
- Listings publish failures now persist staged diagnostics in listing metadata (`last_publish_error`, `last_publish_error_at`, `last_publish_error_stage`, `last_publish_error_context`) and render `Last eBay Publish Diagnostics` in-flow.
- Listings category-suggestion apply now uses the queued pending-update path for publish-form state mutation, keeping category selection stable across reruns in auction/fixed publish attempts.
- Shared bullion/coin default item specifics now include `Circulated/Uncirculated` (`Uncirculated`) to reduce publish-time required-aspect blockers.
- Reports now include `eBay Fee Estimate vs Actual` and `eBay Fee Reconciliation Summary` exports to compare listing-time estimates with imported actual fees.
- Reports now includes `Fee Calibration Assist` that derives an implied final-value-rate from recent eBay sales and can apply the suggested value to runtime settings (`ebay_fee_estimate_final_value_rate_percent`) when permitted.
- Fee reconciliation now prefers imported order fee breakdown (`fee_breakdown_json.total_marketplace_fee`) as the actual-fee source when present, and exposes an `eBay Fee Actual Source Breakdown` export for provenance auditing.
- Reports now include `Shipping Economics Detail` and `Shipping Economics Summary` exports to compare shipping charged vs shipping label spend per sale/order with delta tracking.
- Reports now include `Purchase Document -> Lot Apply Audit` for extracted-invoice normalization traceability (auto/manual mode, actor, workflow, lot/document IDs, applied fields) with CSV export.
- eBay order sync now captures order-level internal shipping label spend (`shipping_label_cost/currency`) when available from fulfillment/order payloads, with fallback from linked sale label costs.
- Orders view/edit and Reports `orders` export now include internal shipping economics fields (`shipping charged`, `actual label spend`, and delta) for operator P/L analysis; these remain excluded from customer invoices/receipts.
- Documents now renders customer-facing eBay invoices/receipts without internal eBay fee line items while still supporting marketplace tax values (`Marketplace Tax (Collected by eBay)`) when enabled.
- Documents now parses eBay sync notes into customer/shipping summaries (`buyer`, `shipping_service`, `ship_to`) and strips pricing payload blobs from displayed note text.
- Reports now include `Order Actual Economics Allocation`, which allocates order-level actual fees and actual shipping-label spend to sale lines for more accurate product/channel/period margin analysis.
- Reports now include `Economics Intelligence Facts (Estimate vs Actual)`, which joins listing-time fee estimates with order/sale actual fee and shipping allocations to show per-sale expected-vs-actual net variance.
- Listing Wizard and Listings eBay publish workspace now include `Expected Net Score (Pre-Publish)` cards that combine estimated eBay fees, known landed unit cost, quantity, and estimated local fulfillment cost to surface expected net/margin before posting.
- Reports now includes `Economics Intelligence Drilldowns + Alerts` with configurable thresholds and grouped rollups (`by SKU`, `by Marketplace`) plus row-level alert exports to triage margin/fee-variance outliers.
- Reports now include `eBay Marketplace Fee Detail (Per Order/Line)` and `eBay Marketplace Fee Summary (By Fee Type)` built from normalized `orderLineItems[].marketplaceFees[]` attribution.
- eBay order sync now persists normalized finance rows in `order_finance_entries` (marketplace fees + shipping-label debits), and Reports fee-detail views now prefer this table over raw JSON parsing.
- Reports `eBay Fee Estimate vs Actual` reconciliation now prefers normalized `order_finance_entries` marketplace-fee totals as `actual_fee`, with notes-derived and sale-field fallbacks retained.
- Reports reconciliation now includes an `Actual Fee Source Priority` summary (normalized source vs notes fallback vs sale-field fallback) to make attribution quality explicit.
- Reports now exports `eBay Fee Source Priority` so attribution-quality trend can be tracked outside the app.
- Reports now exports `eBay Fee Source Priority Trend` with `daily`/`weekly` buckets for source-quality adoption tracking over time.
- Reports reconciliation now includes an in-app weekly `Normalized Source Coverage` trend chart (percent of sales using normalized fee source).
- Reports reconciliation now also includes a weekly fee-source count chart (normalized vs notes fallback vs sale-field fallback) for absolute volume visibility.
- Reports now include fee-attribution rollups `eBay Marketplace Fee by SKU` and `eBay Marketplace Fee by Category` for direct fee-type profitability analysis.
- Admin now includes `eBay Fee Calibration Sign-Off Tracker` (Dev/Prod owner/date/status/sample-count/assumption snapshot/evidence link) and exports `ebay_fee_calibration_signoffs.csv` in the Go-Live Evidence Pack.
- Admin fee calibration tracker now includes `Seed Missing Fee Sign-Off Items` and `Quick Mark Approved` helpers to keep Dev/Prod coverage complete during go-live readiness.
- Admin now includes `Economics Threshold Sign-Off Tracker` (Dev/Prod threshold acceptance + assumption snapshot/evidence) and exports `economics_threshold_signoffs.csv` in the Go-Live Evidence Pack; missing Dev/Prod coverage is included in go-live readiness scoring.
- Admin governance/runtime area now includes `Purchase Document -> Lot Apply Audit` with date filters, deferred-load control, KPI summary, and CSV export for accounting review.
- System Health DB reads now use rollback-safe query wrappers so one failed DB query does not abort downstream checks/panels in the same render.
- System Health `Outbox Runner Activity` now derives from `audit_logs` integration events (`entity_type=integration_event`, `integration=notification_outbox`) instead of a non-existent `integration_events` table.
- Dashboard now includes expanded live business metrics (orders 7d/30d, sales gross/net 30d, profit before returns 30d, estimated profit after returns 30d, shipping charged vs label spend delta 30d, and shipped/not-shipped counts).
- Dashboard `Sales Net (30d)`, `Profit Before Returns (30d)`, `Est Profit After Returns (30d)`, and `label spend (30d)` now use Reports-aligned actual-economics rows: linked normalized `order_finance_entries` marketplace-fee and shipping-label rows are preferred and allocated once per order, with fallback to order/sale fields.
- Dashboard now exposes normalized eBay fee attribution in the UI (`eBay Fees 30d` + fee-type breakdown table) sourced from `order_finance_entries` when available.
- Dashboard live metrics now use repository DB-side rollup aggregation (`dashboard_live_metrics`) in normal runtime to reduce read latency and avoid full sales/orders hydration.
- System Health rollup EXPLAIN baseline now uses a CTE-based `dashboard_live_metrics` probe (sales/orders/label-spend rollups) instead of repeated scalar subqueries, improving plan evidence fidelity and reducing duplicate window scans during snapshot capture.
- System Health rollup EXPLAIN baseline probes now also avoid unnecessary join fan-out in shipping/fee/reconciliation snapshots (unused joins removed and marketplace reconciliation probe converted to per-table CTE aggregates), improving baseline timing fidelity on larger datasets.
- System Health rollup EXPLAIN baseline now includes targeted probes for normalized fee-type grouping (30d) and notification outbox runner activity (14d), extending production plan evidence coverage for finance-attribution and outbox-audit query paths.
- System Health/Admin Slack Ops governance query paths are now included in rollup EXPLAIN baseline evidence (`slack_ops_queue_health_rows`, `slack_ops_events_24h`) to track queue-snapshot and 24h integration-event trend read plans over production-like windows.
- Rollup EXPLAIN probe compatibility was hardened for mixed environments: audit-log JSON probes now cast `changes_json` to `JSONB`, and Slack/outbox probes now emit explicit `skipped` rows when required tables are absent (`audit_logs`/`integration_queue_jobs`) instead of failing the snapshot with opaque SQL errors.
- Rollup EXPLAIN probe classification now also downgrades missing-table relation errors (`relation \"<table>\" does not exist`) to explicit `skipped` rows (`skip_reason=table <table> not present`) so failure-rate metrics reflect true query failures.
- System Health now separates `EXPLAIN Probe Failures` (real query errors) from `EXPLAIN Probe Skips` (intentional table-guard skips with `skip_reason`) so failure-rate metrics remain actionable.
- GS-V10-014 closeout (2026-04-20): database reliability/performance hardening is complete (notification outbox domain + controls, hot-path indexes, DB rollup-first dashboard/report paths, and 18-probe EXPLAIN baseline evidence with failure/skip triage).
- Reports `Shipping Economics` now uses repository DB rollups (`report_shipping_economics_rows`, `report_shipping_economics_summary`) with legacy fallback for compatibility.
- Reports tax detail generation now uses repository DB rollup query (`report_tax_estimate_detail_rows`) with category exemption/shipping-taxable/marketplace filtering and legacy fallback.
- Reports `eBay Fee Reconciliation` detail export now uses repository DB rollup query (`report_ebay_fee_reconciliation_rows`) with legacy fallback for compatibility.
- `Admin -> System Health` now includes an on-demand `DB Rollup Latency Baseline` runner (dashboard + reports rollup timing capture with CSV export) for production read-latency evidence.
- Listing Wizard direct eBay publish now performs stronger local sync writeback (item ID, URL, active status, approved review metadata) when publish succeeds.
- Listing Wizard now shows an in-flow `eBay Sync Integrity Check` after direct publish to confirm expected vs stored item ID/URL/status/review values.
- eBay order import now reconciles linked listing depletion and automatically marks listings `sold` (new listing status) only when cumulative sold quantity reaches listing quantity; multi-quantity listings remain active until final unit sell-through.
- Listings publish now recovers from eBay duplicate-offer errors (`Offer entity already exists` / `errorId 25002`) by resolving/reusing existing `offer_id` and updating the offer instead of failing create.
- Listing Wizard `Advanced eBay Fields + Item Specifics` section now opens by default so item-specific editors are immediately visible during draft/post setup.
- eBay Workspace quick links now include direct `Open Listing Wizard` and `Open eBay Templates` actions to keep setup vs daily listing flow simpler.
- eBay Templates workspace now supports editing existing templates (including inactive rows) and includes reusable HTML block CRUD (save/delete custom blocks) with rendered preview.
- eBay Workspace is now explicitly split into top tabs: `Auth / OAuth`, `Connection Health`, and `Daily Ops` for clearer setup vs operations flow.
- eBay Workspace `Connection Health` now includes a single `Publish Readiness Summary` card with blocker/warning/format-fix metrics and one-click remediation actions.
- eBay Workspace top-level readiness tables were removed to avoid duplicate signals; `Connection Health` is now the single readiness source of truth.
- eBay Workspace `Auth / OAuth` lane is now auth-focused only; format-fix/ops remediation actions were removed from this lane.
- eBay runbook checklist and eBay document handoff controls now live under `Daily Ops` to keep non-operational lanes cleaner.
- Daily Ops now omits duplicate format-fix controls (handled in `Connection Health`) and keeps document handoff collapsed by default for cleaner operator focus.
- Remaining eBay UX simplification focus: keep daily operations centered on `Listing Wizard` + `Listings` queue, and finish production hardening for direct draft posting evidence (category/policy/shipping/package completeness + proof of created drafts).
- Workflow-state modernization focus: complete retention/TTL cleanup coverage and harden Playwright save/resume assertions for Listing Wizard + Listings publish/edit draft flows.
- Listings side panel includes `Open in Documents` with auto-discovered related Sale/Order sources
- Search/Edit page includes `Open in Documents` actions for Listings and Sales search results
- Documents page includes DB-backed `Recent Document Handoffs` (reopen/clear) for quick draft context restoration across restarts/devices
- Admin `Runtime Settings` includes a Documents handoff history management panel (filter, reopen, per-user clear)
- Admin handoff history panel is role-aware: admins can manage all users; non-admins are restricted to their own rows/actions
- Clearing handoff history now writes explicit audit events (`documents_handoff_history` / `clear_history`) for governance traceability
- Admin includes a clear-history audit summary (counts, actor/target breakdowns, recent events, CSV export)
- Admin clear-history audit summary supports date-range filtering for period-based governance review
- Admin clear-history audit summary includes quick date presets (`Last 7d`, `Last 30d`, `This Month`, `Custom`)
- Admin clear-history audit summary supports one-click governance ZIP export (raw + actor/target aggregate CSVs)
- Admin clear-history requires an audit reason when clearing another user's history; reason is stored in audit payload and raw exports
- Admin clear-history reason now uses standardized taxonomy (`reason_code`) with optional note (`reason_note`); `other` requires a note
- Admin clear-history summary includes reason-code analytics (table + bar chart) and exports `clear_events_by_reason_code.csv`
- Admin clear-history summary supports filtering by `reason_code` and `scope` before viewing/exporting governance data
- Admin clear-history governance view supports per-user saved presets (save/load/delete for date and filter settings)
- Admin governance presets now support `My Presets` and `Shared Presets` (shared save/delete restricted to admins)
- Admin shared governance presets support `Set as Team Default`, which auto-loads once on panel entry per session
- Governance preset operations are audit-logged (`documents_handoff_governance_preset`: save/load/delete/set_team_default/clear_team_default)
- Admin includes governance-preset audit summary widgets (lookback + by-action/by-actor + recent events + CSV)
- Governance preset audit summary supports `From Date` / `To Date` filters for period-scoped review
- Governance preset audit summary includes quick date presets (`Last 7d`, `Last 30d`, `This Month`, `Custom`)
- Admin Runtime Settings includes `Governance Review Mode` to apply one shared date preset/range to both governance audit summaries
- `Governance Review Mode` is persisted in runtime settings (`documents_handoff_governance_review_mode`) via in-panel save
- Admin Runtime Settings includes saved governance date-window presets (`My Presets` and `Shared Presets`) with one-click load for recurring review windows
- Governance date-window presets support admin `Set/Clear Window Team Default` with auto-load in Governance Review Mode
- Governance date-window preset lifecycle is audit-logged (`window_save`, `window_load`, `window_delete`, `window_set_team_default`, `window_clear_team_default`)
- Purchase-document lot normalization toggle:
  - Runtime key: `purchase_doc_auto_apply_linked_lot_fields` (`false` by default)
  - When `true`, Intake Wizard and Lots purchase-document upload flows auto-apply extracted `vendor/invoice_date/total/tax/shipping/handling` into linked lot accounting fields.
  - Verification path:
    1. Enable key in `Admin -> Runtime Settings`.
    2. Upload a purchase document with extraction enabled and a linked lot.
    3. Confirm lot fields updated (`vendor`, `purchase_date`, `total_cost`, `total_tax_paid`, `total_shipping_paid`, `total_handling_paid`).
    4. Confirm audit events exist (`entity_type=purchase_document`) with actions:
       - `auto_apply_extracted_fields_to_lot` (automatic mode)
       - `manual_apply_extracted_fields_to_lot` (button-triggered mode)
    5. Confirm visibility in:
       - `Reports -> Purchase Document -> Lot Apply Audit`
       - `Admin -> Purchase Document -> Lot Apply Audit`
- eBay OAuth bootstrap and account privilege check
- eBay Workspace supports runtime-backed store profiles (save/load/delete/default) covering merchant/policy/category and listing-format defaults (`FIXED_PRICE` or `AUCTION`)
- eBay Workspace context apply can optionally persist selected store defaults to runtime keys for consistent listing publish behavior across pages
- Listings readiness and publish validation are format-aware (`FIXED_PRICE` vs `AUCTION`) with explicit duration/start/BIN guardrails
- Fixed-price eBay publish/revise now supports Best Offer (`bestOfferTerms`) with runtime/store-profile defaults
- eBay auction defaults (`start`, `reserve`, `buy-now`) are configurable via runtime/store profiles and validated in readiness + publish/revise checks
- Listings readiness queue includes format triage controls (`all/fixed/auction`) and quick shortcuts for auction-blocked/fixed-ready review
- Listings publish flow includes one-click format templates (`Fixed Price Standard`, `Auction Standard`) that preload store/runtime defaults
- Listings includes reusable branded HTML block insertion tools and one-click Golden Stackers starter marketplace templates (eBay/Craigslist/Facebook/Whatnot)
- Listings table includes `format_type` + `format_hint` quick indicators to surface format issues earlier
- Listings filters include `Format Issue Only` to show only rows with format/setup issues (`format_hint`)
- Listings includes one-click `Format Fix Queue` preset actions (apply now + save team preset)
- eBay Workspace quick links include `Open Format Fix Queue` for direct Listings format-triage handoff
- eBay Workspace Integration/Operations tabs now show a `Format Fix Needed` KPI with direct queue handoff action
- Listings and eBay Workspace now show readiness blocker/warning breakdowns (top reason counts) to prioritize fix work
- Listings readiness queue now supports filtering by exact blocker/warning reason for focused fix passes
- Listings readiness includes one-click top-blocker quick filters that auto-target auction/fixed format when applicable
- Listings readiness can create follow-up tasks directly from blocker reasons (owner/priority/due date), logged via `workspace_followup` audit events
- Listings readiness now shows recent blocker follow-up tasks (open/resolved status context) with CSV export
- Listings readiness can also mark blocker follow-up tasks resolved directly in-page (`workspace_followup` resolve audit events)
- Operations Home now surfaces open blocker follow-up workload (KPI + dedicated queue view) with direct jump back into Listings remediation
- Blocker follow-up rows now include SLA urgency fields (`due_in_days`, `sla_status`) and are sorted to surface overdue/due-soon tasks first
- Listings and Operations Home blocker-task panels now include SLA summary KPIs (`Open`, `Due Soon`, `Overdue`)
- Operations Home blocker-task queue supports filters by status, owner, priority, and SLA status
- Listings blocker-task panel now also supports status/owner/priority/SLA filters, and metrics/export/resolve actions follow the filtered subset
- Listings blocker-task panel now includes one-click filter presets (`Overdue Critical`, `My Open`, `High Priority Open`, `Reset`)
- Operations Home blocker-task queue now includes matching one-click filter presets for fast command-center triage
- Listings and Operations Home blocker-task queues now support DB-backed saved presets (including optional team-shared presets)
- Listings and Operations Home blocker-task saved presets now support default preset set/clear and one-time auto-load behavior
- Admin Saved Filters governance now supports scope filtering and blocker-only focus for easier ownership transfer/delete management of blocker presets
- Admin Saved Filters governance now includes one-click scope preset buttons (`All`, `Blocker Presets`, `Listings`, `Operations Home`)
- Admin Saved Filters governance now also includes ownership filter presets (`My Owned`, `Shared Owned By Me`, `Shared Not Owned By Me`)
- Admin Saved Filters governance includes a one-click `Reset Governance Filters` action to restore default filter view
- Admin Saved Filters governance now shows filter-state summary metrics (active scopes, owner mode, blocker focus, visible rows)
- Admin Saved Filters governance now supports CSV export of the currently filtered table view
- Admin Saved Filters governance now includes filtered breakdown summaries by owner and scope
- Admin Saved Filters governance now includes an `Only default presets` filter and visible-default summary metrics
- eBay Workspace includes an `Active Format Defaults Summary` card with required-policy missing indicator for preflight checks
- Admin backups tab for DB dump creation, S3 backup upload/listing, and guarded restore workflows
- Sync telemetry page for run/event/error tracking (v0.3 foundation)
- Background sync worker for scheduled eBay pull/import runs with automatic sync run/event/error lifecycle logging

Navigation uses Streamlit built-in multipage support (`app/pages/`) instead of a custom sidebar radio switch.

## Project Structure

```txt
.
├── app/
│   ├── main.py
│   ├── views.py
│   ├── page_common.py
│   ├── components/
│   │   ├── __init__.py
│   │   ├── ui_helpers.py
│   │   └── views/
│   │       ├── __init__.py
│   │       ├── dashboard.py
│   │       ├── products.py
│   │       ├── listings.py
│   │       ├── sales.py
│   │       ├── media.py
│   │       ├── lots.py
│   │       ├── sources.py
│   │       ├── orders.py
│   │       ├── returns.py
│   │       ├── documents.py
│   │       ├── ebay.py
│   │       ├── tools.py
│   │       ├── reports.py
│   │       ├── inventory_movements.py
│   │       ├── search_edit.py
│   │       ├── shipping.py
│   │       ├── sync.py
│   │       └── shared.py
│   ├── config.py
│   ├── repository.py
│   ├── pages/
│   │   ├── 01_Dashboard.py
│   │   ├── 02_Products.py
│   │   ├── 03_Listings.py
│   │   ├── 04_Sales.py
│   │   ├── 05_Media.py
│   │   ├── 06_Tools.py
│   │   ├── 08_Lots.py
│   │   ├── 09_Reports.py
│   │   ├── 10_Search_Edit.py
│   │   ├── 11_Shipping.py
│   │   ├── 12_Inventory_Movements.py
│   │   ├── 13_Sources.py
│   │   ├── 14_Orders.py
│   │   ├── 15_Returns.py
│   │   ├── 16_Documents.py
│   │   ├── 17_Admin.py
│   │   ├── 18_Sync.py
│   │   ├── 20_Coin_Intake_Wizard.py
│   │   ├── 21_Ask_GoldenStackers.py
│   │   ├── 22_eBay_Workspace.py
│   │   ├── 23_Inventory_Intake_Wizard.py
│   │   ├── 24_eBay_User_Details.py
│   │   ├── 25_eBay_Templates.py
│   │   └── 26_Listing_Wizard.py
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   ├── init_db.py
│   │   └── seed.py
│   └── services/
│       ├── ebay.py
│       ├── media_storage.py
│       └── spot_price.py
├── k8s/
│   ├── base/
│   ├── jobs/
│   │   ├── base/
│   │   └── overlays/
│   │       ├── dev/
│   │       └── prod/
│   └── overlays/
│       ├── dev/
│       └── prod/
├── .streamlit/config.toml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Data Model

### `products`
- inventory definition and quantity (`sku`, title/category/metal/weight/cost/qty)
- tracks `acquired_at`
- supports shipping package fields: `package_weight_oz`, `package_length_in`, `package_width_in`, `package_height_in`

### `marketplace_listings`
- listing state per marketplace, linked to product
- tracks `listed_at`
- supports `marketplace_url` and `marketplace_details` metadata

### `sales`
- recorded transactions linked to product/listing where available
- optional link to parent order via `order_id`
- tracks `sold_at`
- supports shipping/tracking fields: `shipping_provider`, `shipping_service`, `shipping_package_type`, `tracking_number`, `tracking_status`, `shipped_at`, `delivered_at`
- supports exception handling and export metadata: `shipping_exception_code`, `shipping_exception_notes`, `shipping_exception_action`, `shipping_exception_resolved_at`, `shipping_exception_resolved_by`, `shipment_exported_at`

### `shipping_presets`
- reusable carrier/service/package defaults for fulfillment bulk updates
- tracks `is_default` and `is_active` for operations workflow management

### `document_template_profiles`
- reusable invoice/receipt branding profiles scoped by environment (`local`/`dev`/`prod`)
- supports profile doc type (`all`, `invoice`, `receipt`) with per-scope default selection

### `app_users`
- app-level user directory for role assignment (`viewer`, `ops`, `admin`)
- supports active/inactive state and optional display/email metadata
- stores salted password hash fields for local auth (`password_hash`, `password_salt`)

### `role_permissions`
- role-to-permission mapping used by UI action guardrails
- configurable via Admin page to adapt access policies without code edits

### `orders`
- multi-line order header (marketplace, external order id, status, totals)
- tracks `sold_at`, fees, shipping cost, notes

### `order_items`
- line items linked to `orders`
- supports product/listing linkage, quantity, unit price, and line totals

### `returns`
- return/refund records linked to sale/order/product where available
- tracks status, disposition, refund components, restocked flag, and process dates
- optional restock updates product quantity and inventory movement ledger

### `media_assets`
- image/video metadata linked to product/listing
- stores: media type, filename, content type, size, S3 bucket/key/url

### `purchase_lots`
- bulk purchase source with lot code, vendor, purchase date, total cost, notes
- optional standardized source link via `source_id`

### `purchase_documents`
- immutable incoming purchase invoice/receipt metadata and storage references
- stores: kind/title/filename/content type/size/checksum and S3 bucket/key/url
- supports optional linkage to `lot_id`, `product_id`, and `source_id`
- stores extracted AI payload + summary for traceable invoice-field/line-item intake

### `inventory_sources`
- reusable dealer/vendor/source master data for lot intake
- tracks source type, contact metadata, active/inactive status

### `product_lot_assignments`
- maps individual products/SKUs to source lots with quantity and allocated cost

### `audit_logs`
- immutable event trail for data changes
- tracks: `entity_type`, `entity_id`, `action`, `actor`, `changes_json`, `created_at`

### `sync_runs`
- run-level sync telemetry (provider, job name, direction, status, counters, timings)

### `sync_events`
- per-entity sync activity log within a run (action/status/message/payload)

### `sync_errors`
- sync error records with severity/code/context and optional resolution timestamp

## Environment Strategy

### Copy-Ready ArgoCD Manifests (Dev/Prod)

This repo now includes complete environment template packs you can copy into your ArgoCD infra repo:

- `k8s/templates/dev/`
- `k8s/templates/prod/`

Each pack includes:
- namespace
- configmap
- secret template (`secret.template.yaml`)
- app deployment
- sync-worker deployment
- slack-ops-worker deployment
- migration job (`PreSync` Argo hook)
- service + ingress
- PVCs for media/backups local paths
- HPA + PDB + network policy
- `kustomization.yaml`

Network policy defaults in these template packs are restricted to required traffic for app/runtime operations:
- ingress:
  - `dev`: allow in-namespace traffic
  - `prod`: allow ingress from `ingress-nginx` namespace
- egress:
  - DNS: `53/TCP`, `53/UDP` (kube-dns/coredns in `kube-system`)
  - Web/API calls: `80/TCP`, `443/TCP`
  - Postgres: `5432/TCP`
  - NTP: `123/UDP`

Suggested flow:
1. Copy `k8s/templates/<env>/` into your infra repo app folder.
2. Replace `secret.template.yaml` with your real secret management flow (sealed-secrets/external-secrets/etc).
3. Set environment-specific values:
   - hostnames
   - image tag policy
   - storage class names (`local-path`, `fast-ssd`, etc)
   - resource requests/limits
4. Point ArgoCD Application to that folder and sync.

ArgoCD bootstrap templates are also included:
- `k8s/templates/argocd/application-dev.yaml`
- `k8s/templates/argocd/application-prod.yaml`
- `k8s/templates/argocd/application-set-root.yaml` (optional app-of-apps root)
- `k8s/templates/argocd/appproject-gs-inv.yaml`

Replace placeholder values before use:
- `repoURL`
- `path`
- optional `project` name (if not `default`)

### Release And Promotion Model (GitHub Actions + ArgoCD)

GoldenStackers runtime promotion is intended to be image-first and GitOps-driven:

- Build source of truth: this app repo (GitHub Actions builds/tests/publishes Docker images).
- Deploy source of truth: separate infra repo (ArgoCD watches Kubernetes manifests there).
- Environments:
  - Development: contained Kubernetes environment for integration and workflow validation.
  - Production: contained Kubernetes environment for approved releases only.

Recommended flow:

1. Merge to main and/or cut a release tag in this repo.
2. GitHub Actions builds/tests and publishes versioned Docker image tags (semver + commit SHA).
3. Update image tag in infra repo `dev` manifests, merge, and let ArgoCD sync Dev.
4. Run migrations + smoke checks in Dev.
5. Promote the same immutable image tag to infra repo `prod` manifests, merge, and let ArgoCD sync Prod.

Notes:
- Do not rebuild images separately per environment; promote the same tested tag.
- Keep database migration execution as an explicit pre-sync/pre-rollout job in each environment.
- Keep secrets managed in cluster secret manager/CI pipeline, not hardcoded in manifests.
- Set build traceability env/runtime values per deployment:
  - `APP_BUILD_VERSION` (example `v0.6.3`)
  - `APP_BUILD_SHA` (example full git SHA)

GitHub Actions workflows added under `.github/workflows/`:
- `autotagger.yaml`: auto-tag from `main` using `package.json` version strategy.
- `autorelease.yaml`: publish GitHub Release on strict semver tags (`vX.Y.Z`).
- `docker_build.yaml`: build + validate + publish Docker images on `main` and semver tags.
- `deploy_config_check.yaml`: manual preflight to validate Docker secrets + Argo promotion variables before live promotion runs.
- `qa_tests.yaml`: unit tests + Playwright smoke tests (starts compose app stack, runs browser checks, uploads artifacts).

## QA Testing Strategy

Automated QA now has two layers:

- Python unit tests (repository/service behavior): `tests/test_*.py`
- Browser smoke tests (Playwright): `tests/e2e/*.spec.ts`
- Coverage reporting + gating (`coverage.py`) over `app/**`

Local commands:

```bash
# Python unit tests
python -m unittest discover -s tests -p "test_*.py"

# Segmented suites for faster local iteration/CI fan-out
python scripts/run_test_suites.py --suite fast
python scripts/run_test_suites.py --suite integration

# Unit tests with coverage report + gate
python -m coverage run --source=app -m unittest discover -s tests -p "test_*.py"
python -m coverage report -m --fail-under=38
# Scoped-core gate (repository/services/auth/page_common/config)
python -m coverage report -m \
  --include="app/repository.py,app/services/*.py,app/auth.py,app/page_common.py,app/config.py" \
  --fail-under=88
python -m coverage xml -o coverage.xml

# Playwright dependencies (one-time)
npm install
npx playwright install chromium

# Browser smoke tests
npm run test:e2e

# Run intake wizard critical path only
npm run test:e2e -- tests/e2e/intake_wizard.spec.ts

# Run intake AI prefill coverage (identifier -> form field population)
npm run test:e2e -- tests/e2e/intake_prefill.spec.ts

# Run lifecycle archive/restore coverage (Listings danger-zone roundtrip)
npm run test:e2e -- tests/e2e/lifecycle_archive_flow.spec.ts

# Run products create/edit critical path only
npm run test:e2e -- tests/e2e/products_flow.spec.ts

# Run auth sign-in/sign-out critical path only
npm run test:e2e -- tests/e2e/auth_flow.spec.ts

# Run listings draft/review preflight critical path only
npm run test:e2e -- tests/e2e/listings_flow.spec.ts

# Run strict seeded listings gate (hard assertions; requires seed + seller-ops enabled)
E2E_STRICT_LISTINGS=1 PLAYWRIGHT_REQUIRE_SEED=1 npm run test:e2e -- tests/e2e/listings_flow_strict.spec.ts
# or the dedicated script:
npm run test:e2e:listings:strict

# Run strict seeded lifecycle archive/restore gate
E2E_STRICT_LIFECYCLE=1 PLAYWRIGHT_REQUIRE_SEED=1 npm run test:e2e -- tests/e2e/lifecycle_archive_flow_strict.spec.ts
# or the dedicated script:
npm run test:e2e:lifecycle:strict

# Run strict seeded lifecycle entity gate (Products/Lots/Media)
E2E_STRICT_LIFECYCLE=1 PLAYWRIGHT_REQUIRE_SEED=1 npm run test:e2e -- tests/e2e/lifecycle_entities_strict.spec.ts
# or the dedicated script:
npm run test:e2e:lifecycle:entities:strict

# Run Admin lifecycle retention manual cleanup evidence flow
npm run test:e2e:admin:lifecycle-retention

# Run listing wizard category/quantity/preview flow
npm run test:e2e -- tests/e2e/listing_wizard_flow.spec.ts

# Run eBay templates create/edit/custom-block flow
npm run test:e2e -- tests/e2e/ebay_templates_flow.spec.ts

# Combined convenience script
npm run test:qa

# Local go-live style QA evidence bundle (unit + e2e + evidence summary)
make qa-evidence
# or
task qa-evidence

# Segmented convenience targets
make qa-unit-fast
make qa-unit-integration
# or
task qa-unit-fast
task qa-unit-integration

# Build evidence summary from existing artifacts only (no test rerun)
make qa-evidence-build
# or
task qa-evidence-build
```

Playwright base URL can be overridden:

```bash
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8501 npm run test:e2e
```

Notes:
- Prefer the `npm run test:e2e` wrapper commands above; they sanitize conflicting terminal color env vars before Playwright launch.
- CI workflow `qa_tests.yaml` writes a CI `.env`, starts `db/migrate/app` with Docker Compose, waits for app readiness, then runs Playwright Chromium smoke tests.
- QA CI fixture explicitly sets `EBAY_ALLOW_SANDBOX_SELLER_OPS=true` in generated `.env` so Listings/Listing Wizard eBay preflight e2e paths run non-skipped in sandbox-mode CI.
- QA CI supports optional strict Listings gate by setting `QA_ENABLE_STRICT_LISTINGS=1` (runs `tests/e2e/listings_flow_strict.spec.ts` with hard assertions).
- QA CI runs strict lifecycle gates by default on PR/push (`QA_ENABLE_STRICT_LIFECYCLE=1`), covering `tests/e2e/lifecycle_archive_flow_strict.spec.ts`, `tests/e2e/lifecycle_entities_strict.spec.ts`, and `tests/e2e/admin_lifecycle_retention_flow.spec.ts`.
- `QA Test Suite` workflow-dispatch now includes input `qa_enable_strict_listings` (boolean) to run the strict Listings gate on demand without editing workflow YAML.
- `QA Test Suite` workflow-dispatch also includes input `qa_enable_strict_lifecycle` (boolean) to enable/disable strict lifecycle gates for manual runs without editing workflow YAML.
- Browser artifacts are uploaded on CI runs (`playwright-report`, `test-results`) for triage.
- Coverage artifacts are uploaded on CI runs (`.coverage`, `coverage.xml`, `coverage.json`).
- QA evidence now includes coverage gate trend snapshot artifact (`qa-evidence/coverage_gates.json`) with global/scoped thresholds and pass/fail state per run.
- QA evidence now also includes segmented suite manifests (`qa-evidence/suite_fast_manifest.json`, `qa-evidence/suite_integration_manifest.json`) for deterministic fast/integration split tracking.
- QA evidence artifact is uploaded on CI runs (`qa-evidence`) with sign-off-ready summary files (`qa_evidence.md`, `qa_evidence.json`) and also written to the GitHub job summary.
- Coverage gates are enforced in CI to prevent regression: global `>=38%` and scoped-core `>=88%` (`repository/services/auth/page_common/config`), with ratchet targets tracked in roadmap/checklists (`40% -> 55%+` and scoped-core `>=95%`).
- Latest local QA baseline (2026-04-13): `770` unit tests passing, global coverage `~38.92%`, scoped-core coverage `~88.45%`.
- Local Playwright defaults now use `E2E_USERNAME=e2e` and `E2E_PASSWORD=e2e-password-123` when env vars are not provided.
- Playwright global setup treats seed failures as non-fatal in local/dev by default (`PLAYWRIGHT_REQUIRE_SEED=0`) so e2e runs can proceed in restricted shells; CI remains strict (`PLAYWRIGHT_REQUIRE_SEED=1` via `CI`).
- Admin go-live e2e test uses admin credentials (`E2E_ADMIN_USERNAME` / `E2E_ADMIN_PASSWORD`) and falls back locally to `admin` / `e2e-password-123`.
- Admin go-live e2e test now runs as a hard assertion path (no skip fallback in normal local runs) with deterministic admin sign-in enforcement.
- Latest local Playwright run (`2026-04-09`) is `9 passed / 0 skipped`.
- Latest targeted Playwright intake-prefill run (`2026-04-14`) is `2 passed / 0 skipped` (`tests/e2e/intake_prefill.spec.ts`).
- Intake wizard e2e suites now also include `Purchased On eBay` regression assertions (eBay field visibility toggle + required item-ID validation) in both `tests/e2e/intake_wizard.spec.ts` and `tests/e2e/coin_intake_wizard.spec.ts`.
- Listings e2e flow now includes explicit `eBay Post Mode` switch assertions (`Publish Live Listing` and `Save Unpublished Offer (API Draft)`) before dependency preflight execution.
- Listings e2e flow now also covers category-assist fetch/apply behavior (`Load eBay Category Assist` + taxonomy suggestion apply + category ID state verification), with environment-safe skip when taxonomy fetch is unavailable.
- Listing Wizard Playwright flow now includes create-draft direct-post feedback/link assertions (direct-post skip/fail message handling and post-create `Open Listings` link visibility).
- Lifecycle archive/restore spec added (`tests/e2e/lifecycle_archive_flow.spec.ts`) with seed-safe skip behavior when no side-panel listing selector is available.
- Latest strict lifecycle entities run (`2026-04-16`) is `3 passed / 0 failed` (`tests/e2e/lifecycle_entities_strict.spec.ts`) with deterministic Products/Lots/Media archive->restore roundtrips.
- Added Admin lifecycle retention evidence flow spec (`tests/e2e/admin_lifecycle_retention_flow.spec.ts`) to validate `Run Lifecycle Cleanup Now` and persistent `Last Lifecycle Cleanup Run` evidence block in Admin > Integrations.
- Current Playwright spec footprint: `15` Chromium specs under `tests/e2e/*.spec.ts`.
- Local seed now enforces required permissions for the configured e2e role by default (`E2E_ENSURE_ROLE_PERMISSIONS=true`) to keep browser e2e flows deterministic.
- Seed scripts create/update this `e2e` app user automatically in non-prod (`make db-seed` or `docker compose run --rm seed`).
- Seed scripts also keep a deterministic local `admin` login aligned to the seeded e2e password (`e2e-password-123`) for browser test parity.
- Seed scripts now include a deterministic eBay draft listing fixture (`EBAY-LIST-E2E-DRAFT`, title `E2E Seed Listing Draft (eBay)`) so Listings review/publish e2e flows can target a known row when dynamic listing creation/selection is unavailable.
- Override credentials by setting `E2E_USERNAME` / `E2E_PASSWORD` and optionally `E2E_ADMIN_USERNAME` / `E2E_ADMIN_PASSWORD` in your environment.

Expected GitHub Secrets:
- `DOCKER_REGISTRY`
- `DOCKER_USERNAME`
- `DOCKER_PASSWORD`
- optional `GH_TOKEN` (PAT for cross-repo Argo PR updates; falls back to `GITHUB_TOKEN` when possible)

Expected GitHub Repository Variables (for ArgoCD repo PR handoff):
- `ARGO_DEPLOY_REPO` (example: `org/infra-repo`)
- `ARGO_DEV_MANIFEST_PATH` (example: `apps/gs-inv/dev/deployment-app.yaml`)
- optional `ARGO_DEV_CONFIGMAP_PATH` (example: `apps/gs-inv/dev/configmap.yaml`) for auto-updating `APP_BUILD_VERSION` and `APP_BUILD_SHA`
- `ARGO_PROD_MANIFEST_PATH` (example: `apps/gs-inv/prod/deployment-app.yaml`)
- optional `ARGO_PROD_CONFIGMAP_PATH` (example: `apps/gs-inv/prod/configmap.yaml`) for auto-updating `APP_BUILD_VERSION` and `APP_BUILD_SHA`
- optional `ARGO_TARGET_BRANCH` (default `main`)
- optional `ARGO_PR_LABELS` (default `automerge`)
- optional `ARGO_DEV_PR_LABELS` (overrides labels for Dev promotion PRs)
- optional `ARGO_PROD_PR_LABELS` (overrides labels for Prod promotion PRs)

Quick-start variable template:

```text
ARGO_DEPLOY_REPO=your-org/your-argo-repo
ARGO_TARGET_BRANCH=main

ARGO_DEV_MANIFEST_PATH=apps/gs-inv/dev/deployment-app.yaml
ARGO_DEV_CONFIGMAP_PATH=apps/gs-inv/dev/configmap.yaml
ARGO_DEV_PR_LABELS=automerge,dev

ARGO_PROD_MANIFEST_PATH=apps/gs-inv/prod/deployment-app.yaml
ARGO_PROD_CONFIGMAP_PATH=apps/gs-inv/prod/configmap.yaml
ARGO_PROD_PR_LABELS=automerge,prod
```

### 1) Local Development (Docker Compose)
- `docker-compose.yml` runs app + postgres.
- Optional local S3-compatible service: MinIO profile.
- Dedicated one-shot `migrate` service runs Alembic upgrades before app startup.
- App container runs with `DB_AUTO_MIGRATE=false` by default.

Start local:

```bash
cp .env.example .env
docker compose up --build
```

Run migration only (without starting app):

```bash
docker compose run --rm migrate
```

Start local with MinIO too:

```bash
docker compose --profile local-s3 up --build
```

Open:
- App: `http://localhost:8501`
- MinIO console (if enabled): `http://localhost:9001`

## Config Coverage And Feature-Flag Tracking

Admin now includes config coverage dashboards so you can quickly see what is configured, defaulted, or missing.

- Env coverage (`Admin -> Env Config`):
  - compares `.env` to `.env.example`
  - status per key: `missing`, `empty`, `default`, `set`
  - includes feature-flag-like key subset (for keys containing patterns like `ENABLED`, `ALLOW`, `OVERRIDE`, etc.)

- Runtime coverage (`Admin -> Runtime Settings`):
  - compares runtime rows to the app’s seeded runtime-default catalog
  - status per key: `missing`, `inactive`, `default`, `overridden`
  - includes `custom_untracked` status for DB runtime keys that are not part of the current default catalog
  - includes feature-flag-like key subset for runtime toggles
  - includes strict warning panel for required runtime keys that are `missing`/`inactive`
  - required runtime key set now includes Listing Wizard AI keys (`listing_wizard_ai_system_message`, `listing_wizard_ai_seed_default`, `listing_wizard_ai_instruction_template`, `listing_wizard_ai_include_quick_comp_context`, `listing_wizard_ai_quick_comp_limit`)

- One-click missing-default application:
  - use `Apply All Missing Runtime Defaults Now` directly in Runtime Coverage to create any missing DB runtime keys immediately
  - this uses the same seeded default catalog as `Seed Defaults From Current Env`
  - use `Apply Missing + Empty Env Defaults Now` to fill all missing/empty `.env` keys from `.env.example`
  - use `Apply Missing + Inactive Runtime Defaults Now` to fill missing runtime keys and reactivate inactive tracked keys

- Coverage export:
  - `Download Env Coverage CSV`
  - `Download Runtime Coverage CSV`

- Config health warnings:
  - Env coverage includes a strict warning panel for required env keys that are `missing`/`empty`
  - Runtime coverage includes a strict warning panel for required runtime keys that are `missing`/`inactive`
  - Both coverage sections include a quick health score (`healthy`/`warning`/`critical`) with percent completion.
  - Warning panels include one-click auto-fix actions:
    - `Auto-Fix Required Env Keys From .env.example`
    - `Auto-Fix Required Runtime Keys`
  - Required-key definitions and health-state thresholds are centralized in `app/services/config_health.py` and reused by Admin + System Health.
- Env drift detection: untracked keys in `.env` (not present in `.env.example`) are surfaced in Env Coverage, top Admin summary, and System Health.
  - Runtime drift detection: custom/untracked runtime keys (not in seeded runtime default catalog) are surfaced in Runtime Coverage and top Admin summary.

- System Health integration:
  - `Admin -> System Health` now includes a `Config Health` snapshot for required env/runtime keys with state and quick remediation tips.
  - `Admin` page header now shows a compact `Config Health Summary` with:
    - required-key metrics and quick required auto-fix buttons
    - all-tracked-key missing/inactive counts
    - one-click bulk default actions for env/runtime tracked keys
    - downloadable `Config Health Snapshot (JSON)` for handoff/audit
  - System Health snapshot now also includes runtime drift (`Runtime Untracked Keys`) alongside env drift.

### Background Sync Worker

The background sync runner executes scheduled sync jobs and writes telemetry to:
- `sync_runs`
- `sync_events`
- `sync_errors`

Docker Compose includes a dedicated `sync_worker` service:

```bash
docker compose up -d --build sync_worker
docker compose logs -f sync_worker
```

Scheduler env vars:

```env
SYNC_RUNNER_ENABLED=true
SYNC_RUNNER_INTERVAL_SECONDS=900
SYNC_RUNNER_ACTOR=sync-worker
SYNC_RUNNER_RUN_ONCE=false

SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED=false
SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_LIMIT=50
SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_OFFSET=0
SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED=false
SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_ENABLED=true
SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_INTERVAL_MINUTES=30
SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED=false
SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED=false
```

Notes:
- `SYNC_RUNNER_ENABLED` defaults to `true`; set `SYNC_RUNNER_ENABLED=false` only when intentionally pausing the worker.
- `SYNC_RUNNER_RUN_ONCE=true` runs one pass then exits (useful for smoke tests/jobs).
- Runtime setting `sync_job_ebay_orders_pull_import_enabled` controls eBay order pull/import (env fallback: `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED`).
- `SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED` controls eBay tracking push.
- `SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_ENABLED` controls scheduled eBay token/identity/privilege health checks.
- `SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_INTERVAL_MINUTES` sets minimum cadence for health checks.
- Runtime settings `ebay_user_token_auto_refresh_enabled`, `ebay_user_token_auto_refresh_interval_hours` (default `12`), `ebay_user_token_auto_refresh_min_ttl_minutes` (default `45`), and `ebay_user_token_auto_refresh_failure_cooldown_minutes` (default `30`) control proactive eBay OAuth user-token refresh in the sync runner.
- Runtime setting `slack_notify_ebay_oauth_refresh_failures` (default `true`) controls Slack alert dispatch for sync-runner eBay OAuth refresh failures.
- Transient eBay DNS/network failures (`NameResolutionError`, connection timeout/reset, network unreachable) are classified separately from auth/data failures: OAuth refresh records warning telemetry and uses cooldown without Slack failure spam, connection health returns `partial` with warning details, order pull/import is marked `skipped` with `EBAY_NETWORK_UNAVAILABLE` and `records_failed=0`, and System Health/Sync show unresolved eBay network holds for operators.
- Operator response steps for those incidents are documented in [DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md#9-ebay-sync-network-incidents).
- `SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED` reserves toggle control for future QuickBooks export jobs.
- `SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED` reserves toggle control for future Shopify order-pull jobs.
- Disabled jobs are blocked in worker and manual UI actions.
- eBay pull job requires `EBAY_USER_ACCESS_TOKEN`.
- Governance snapshot scheduling is runtime-driven (Admin -> `Sync Jobs`):
  - `governance_snapshot_runner_enabled` (default `false`)
  - `governance_snapshot_interval_hours` (default `24`)
  - `governance_snapshot_lookback_days` (default `30`)
  - `governance_snapshot_max_rows_per_scope` (default `2000`)
  - Worker records audit events as `entity_type=governance_export`, `action=snapshot`, `source=sync_runner`.

Recommended local S3 env values when using MinIO:

```env
STORAGE_PROVIDER=s3
S3_BUCKET=goldenstackers-media
S3_ENDPOINT_URL=http://minio:9000
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin123
```

### 2) Dev Kubernetes Environment
- Kustomize overlay: `k8s/overlays/dev`
- `APP_ENV=dev`
- lower replica/resource profile
- Run dedicated migration job before rolling out app pods.

Apply:

```bash
kubectl apply -k k8s/jobs/overlays/dev
kubectl wait --for=condition=complete job/gs-inv-migrate -n gs-inv-dev --timeout=300s
kubectl apply -k k8s/overlays/dev
```

### 3) Production Kubernetes Environment
- Kustomize overlay: `k8s/overlays/prod`
- `APP_ENV=prod`
- higher replica/resource profile
- Run dedicated migration job in CI/CD before app rollout.

Apply:

```bash
kubectl apply -k k8s/jobs/overlays/prod
kubectl wait --for=condition=complete job/gs-inv-migrate -n gs-inv-prod --timeout=600s
kubectl apply -k k8s/overlays/prod
```

## Kubernetes Secret Handling

`k8s/base/secret.template.yaml` is a template only. Replace empty values via your secret manager/CI pipeline before apply.

Required secret values include:
- Postgres connection values (`POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD`)
- migration behavior (`DB_AUTO_MIGRATE`, default false for app pods)
- eBay credentials
- AWS/S3 credentials and bucket settings
- app identity defaults (`APP_USER_NAME`, `APP_USER_ROLE`, `APP_ALLOW_ROLE_OVERRIDE`)
- auth persistence settings (`APP_REQUIRE_PASSWORD_AUTH`, `APP_AUTH_SIGNING_KEY`, `APP_AUTH_REMEMBER_DAYS`, `APP_AUTH_COOKIE_ENABLED`, `APP_AUTH_QUERY_TOKEN_FALLBACK_ENABLED`)

## Database Initialization and Migrations

Schema management now uses Alembic (not `create_all()`).

### Migration files
- Alembic config: `alembic.ini`
- Migration environment: `app/db/alembic/env.py`
- Versions: `app/db/alembic/versions/`

### Common commands

```bash
# upgrade to latest
python -m app.db.migrate upgrade

# show current revision in DB
python -m app.db.migrate current --verbose

# show migration history
python -m app.db.migrate history --verbose

# create a new migration from model changes
python -m app.db.migrate revision -m "describe change"
```

Equivalent shortcuts are in `Makefile`:

```bash
make db-migrate-compose
make db-upgrade
make db-current
make db-history
make db-revision m="describe change"
make k8s-migrate-dev
make k8s-migrate-prod
```

## Auth Session Restore Notes

For stable local/dev behavior, keep:

- `APP_REQUIRE_PASSWORD_AUTH=true`
- `APP_AUTH_COOKIE_ENABLED=false`
- `APP_AUTH_QUERY_TOKEN_FALLBACK_ENABLED=true`
- `APP_AUTH_SIGNING_KEY` set to a non-empty secret

Session restore uses a signed `auth` query token (remember-me flow), with cookie storage optional.

In `Admin -> Users -> Auth Session Debug`, healthy restore state should show:

- `Session Authenticated = yes`
- `Remember Enabled = yes`
- `Query Token Present = yes`
- `Query Token Valid = yes`

`cookie_manager_state=disabled` is expected when cookie mode is off.

## Development Seed Data (Non-Production)

A repeatable seed dataset is available for local/dev workflows. It creates realistic sample:
- purchase lots
- products and lot assignments
- marketplace listings
- sales transactions
- media asset metadata (S3 URLs as sample references)
- local Playwright auth user (`E2E_USERNAME`/`E2E_PASSWORD`, default `e2e` / `e2e-password-123`)

Safety guard:
- seeding is blocked when `APP_ENV=prod`

Run locally (host Python environment):

```bash
make db-seed
```

Reset and reseed local data:

```bash
make db-seed-reset
```

Run inside Docker Compose:

```bash
docker compose run --rm seed
```

Reset and reseed inside Docker Compose:

```bash
docker compose run --rm seed python -m app.db.seed --wipe
```

### Team workflow for schema changes
1. Update SQLAlchemy models.
2. Generate revision (`db-revision`).
3. Review generated migration file in `app/db/alembic/versions/`.
4. Apply locally (`db-upgrade`) and test.
5. Commit model + migration together.
6. Run migration service/job before app rollout in each environment.

## S3 Media Storage

Media uploads are managed from the `Media` page in Streamlit:
- supports multi-file upload (images/videos)
- uploads file bytes to S3
- stores metadata in `media_assets`
- optional association to product and/or listing

Listings page also supports:
- uploading photos/videos at listing creation
- attaching additional media directly to an existing listing
- viewing listing-specific media inventory

Products page also supports:
- SKU generation helper for fast unique SKU creation
- uploading photos/videos at product creation
- attaching additional media directly to an existing product
- viewing product-specific media inventory
- repurchase/restock capture with per-unit landed-cost components (`cost`, `tax`, `shipping`, `handling`) and optional repurchase invoice/receipt upload linked to product/lot

## Reports and Exports

The `Reports` page provides operational reporting with date filters and in-app tables.

Current exports include:
- Sales Detail
- Inventory Snapshot
- Listing Snapshot
- Lot Assignment
- QuickBooks Sales Export

Each report can be downloaded as:
- CSV (`.csv`)
- Excel (`.xlsx`)

Configuration keys:
- `STORAGE_PROVIDER=s3`
- `S3_BUCKET`
- `AWS_REGION`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- optional `S3_ENDPOINT_URL` for S3-compatible local/dev endpoints
- optional `S3_PUBLIC_BASE_URL` for CDN/public URL rewriting

## Calculator and Spot Estimator

The `Tools` page includes:
- `Gram ↔ Troy Oz` converter
- Spot-based cost estimator (`gold`, `silver`, `platinum`)
- `Comp Tool` for comparable pricing research:
- eBay sold/completed comps via Finding API are now treated as best-effort only (legacy/degrading); app flows use shared fallback paths (eBay sold HTML + web fallback) when Finding is unavailable/rate-limited.
  - optional web-search fallback comp hints
  - optional AI/LLM comp summary over returned comps

Live spot provider config is now runtime-overridable in Admin `Runtime Settings`:
- `spot_price_provider` (default `yahoo_finance`)
- `yahoo_finance_base_url`
- `yahoo_symbol_gold`
- `yahoo_symbol_silver`
- `yahoo_symbol_platinum`

Yahoo Finance can rate-limit requests (HTTP 429). The app includes retry/backoff and supports manual spot entry as fallback.

Optional paid provider runtime keys:
- `spot_price_provider=metals_api`
- `metals_api_key`
- `metals_api_base_url`

Estimator always supports manual spot input as fallback.

Comp Tool config:
- Runtime setting `comp_web_fallback_enabled` enables web-search fallback when eBay comps are empty (env fallback: `COMP_WEB_FALLBACK_ENABLED`).
- Runtime settings `comp_ebay_max_calls_per_run`, `comp_ebay_max_calls_per_10m`, and `ebay_finding_rate_limit_cooldown_seconds` are now legacy compatibility settings (inactive for primary runtime comp flows).
- Inventory Intake, Coin Intake, and Listing Wizard comp-assisted flows now treat `EBAY_FINDING_RATE_LIMITED` as non-fatal and continue with fallback/AI context instead of hard-failing operator flow.
- Comp Tool and Slack Ops comp now follow the same cross-app fallback chain to reduce dependence on `findCompletedItems`.
- Runtime comp flows now use eBay sold-results HTML as the primary eBay comp source; `findCompletedItems` is retained only as legacy code path and not used by these app workflows.
- `COMP_LLM_ENABLED=false` enables/disables AI summary controls.
- `COMP_LLM_PROVIDER=openai` (`openai` or `localai`).
- `COMP_LLM_BASE_URL=https://api.openai.com/v1` (for LocalAI use your local endpoint, e.g. `http://localai:8080/v1`).
- `COMP_LLM_ENDPOINT_TYPE=responses` (`responses` or `chat_completions`).
- `OPENAI_API_KEY` required when provider is OpenAI.
- `COMP_LLM_MODEL` default `gpt-4o-mini` (override as needed).
- `COMP_LLM_TEMPERATURE`, `COMP_LLM_MAX_OUTPUT_TOKENS`, `COMP_LLM_TIMEOUT_SECONDS` for runtime behavior tuning.

Admin runtime override:
- Admin page now includes an `AI Runtime` tab with DB-backed profiles (OpenAI + LocalAI).
- The Comp Tool uses the default active profile for the current environment first.
- If no DB profile exists, it falls back to `COMP_LLM_*` and `OPENAI_API_KEY` environment values.
- Comp Tool no longer renders the legacy Finding activity panel; active comp data paths are HTML/web-based.
- Admin page includes an `Env Config` tab for `.env` visibility and safe non-DB key editing.
- Admin page includes a `Runtime Settings` tab for DB-backed live overrides (environment-scoped) used by Tools/eBay defaults.
- Admin page includes an `Integrations` tab for environment-scoped Google Workspace and Slack configuration.
- Ask GoldenStackers masking runtime keys:
  - `chat_mask_sensitive_enabled`
  - `chat_mask_email_enabled`
  - `chat_mask_phone_enabled`
  - `chat_mask_tracking_enabled`

Coin Database paid-source adapter guardrails:
- Runtime keys:
  - `coin_ref_paid_source_enabled` (`true|false`)
  - `coin_ref_paid_source_provider` (`none|greysheet`)
  - `coin_ref_paid_source_base_url`
  - `coin_ref_paid_source_api_key`
- `coin_ref_paid_source_license_ack` (`true|false`)
- `coin_ref_paid_source_allow_prod` (`true|false`)
- These keys are seeded in `Admin -> Runtime Settings`.
- `Admin -> Comp Config` is the source of truth for editing paid-source adapter config and validation state.

Google document delivery:
- `Documents` page includes `Send via Gmail` for invoice/receipt HTML delivery.
- `Documents` page includes `Create Follow-Up Calendar Event` for selected invoice/receipt source records.
- `Documents` page includes `Upload Artifact to Google Drive` for generated HTML/CSV/XLSX outputs.
- Google settings are managed in `Admin -> Integrations -> Google Workspace`.
- Runtime keys used by document send:
  - `google_integration_enabled`
  - `google_oauth_access_token`
  - `google_default_sender_email`
  - `google_default_calendar_id`
  - `google_default_timezone`
  - `google_http_timeout_seconds`
- Send attempts are recorded in `audit_logs` with `entity_type=integration_event`, `integration=google_gmail`.
- Calendar create attempts are recorded in `audit_logs` with `entity_type=integration_event`, `integration=google_calendar`.
- Drive upload attempts are recorded in `audit_logs` with `entity_type=integration_event`, `integration=google_drive`.
- Failed Google actions are automatically enqueued into DB-backed retry jobs (`integration_queue_jobs`) with exponential backoff.
- Admin queue controls are available in `Admin -> Integrations -> Google Retry Queue`.

Shipping label queue + provider adapters:
- Shipping label queue controls are in `Admin -> Integrations -> Shipping Queue Controls`.
- Runtime guardrails:
  - `shipping_queue_enabled`
  - `shipping_label_purchase_enabled`
  - `shipping_label_live_provider_calls_enabled`
- Provider toggles:
  - `shipping_label_provider_pirateship_enabled`
  - `shipping_label_provider_ebay_shipping_enabled`
  - `shipping_label_provider_usps_enabled`
  - `shipping_label_provider_ups_enabled`
  - `shipping_label_provider_fedex_enabled`
  - `shipping_label_provider_other_enabled`
- Pirate Ship adapter runtime keys:
  - `shipping_label_pirateship_mode` (`mock|api`)
  - `shipping_label_pirateship_base_url`
  - `shipping_label_pirateship_api_key`
  - `shipping_label_pirateship_endpoint_path`
  - `shipping_label_pirateship_auth_scheme` (`bearer|token`)
  - `shipping_label_pirateship_timeout_seconds`
- Admin includes `Test Pirate Ship Adapter` action (under `Integrations -> Shipping Queue Controls`) to validate adapter config before queue execution.
- Admin shows recent shipping adapter test events (`integration=shipping_label_adapter`) for operational triage.

Integration automation rules (preview):
- Admin `Integrations` includes `Integration Automation Rules (Preview)` for environment-scoped rule records.
- Rules include:
  - `integration`, `action`, `trigger_status`, `name`
  - `conditions_json`, `effect_json`
  - `requires_approval`, `is_active`
- Rules are audited in `audit_logs` as `integration_automation_rule` create/update/delete events.
- Execution runtime keys:
  - `integration_automation_dry_run_enabled`
  - `integration_automation_execute_approval_required_enabled`
- Queue processing now evaluates matching active rules on queued jobs and logs `integration=integration_automation` events with matched/applied/approval-gated metrics.
- Explicit approvals:
  - Admin `Integrations` includes `Automation Approvals` for rule-level or rule+job approvals with optional expiry.
  - Requires-approval rules execute only when:
    - `integration_automation_execute_approval_required_enabled=true`, and
    - an active, non-expired approval record exists for the rule (optionally scoped to queue job).
- Admin `Integrations` includes `Automation Failure Triage` quick actions for blocked/gated events:
  - `Approve Gated Rules`
  - `Retry Job Now`
  - `Disable Matched Rules`

Document tax runtime defaults:
- `invoicing_tax_jurisdiction` (default: `Golden, Colorado`)
- `invoicing_tax_rate_percent_default` (default: `7.50`)
- `invoicing_tax_shipping_taxable_default` (`true|false`)
- `invoicing_tax_exempt_categories_csv` (default: `bullion,coins`)
- `marketplace_facilitator_channels_csv` (default: `ebay`) to exclude facilitator channels from local tax liability report scope by default
- Tax behavior should be verified with your tax professional for current local/state applicability.

Slack notifications (foundation):
- Runtime-configured Slack delivery is available via `app/services/slack_notify.py` using `chat.postMessage`.
- Admin Integrations includes `Send Test Slack Message` for immediate verification.
- First automatic alerts now include:
  - Sync run `failed/partial` outcomes.
  - Terminal Google integration queue failures.
- System Health includes a manual `Run Slack Connectivity Check` using Slack `auth.test`.
- Admin Integrations shows recent Slack delivery events from integration audit logs.
- Slack channel routing supports runtime overrides by event/severity/env:
  - `slack_channel_sync_failures`
  - `slack_channel_google_queue_failures`
  - `slack_channel_warning`, `slack_channel_error`, `slack_channel_critical`
  - optional env+event override pattern: `slack_channel_<env>_<event>`
- Slack delivery retry queue runtime keys:
  - `slack_queue_enabled`
  - `slack_queue_max_retries`
  - `slack_queue_backoff_base_seconds`
  - `slack_queue_backoff_max_seconds`
- Slack message templates are runtime-editable:
  - `slack_template_sync_failures`
  - `slack_template_google_queue_failures`
  - `slack_template_daily_report`
  - `slack_template_backup_success`
  - `slack_template_backup_failure`
- App timezone:
  - `app_default_timezone` (defaults to `America/Denver`; used as global display/scheduling default)
- Daily Slack ops report scheduler runtime keys:
  - `slack_daily_report_enabled`
  - `slack_daily_report_timezone` (example: `America/Denver`)
  - `slack_daily_report_local_time` (`HH:MM`, local time in configured timezone)
  - `slack_daily_report_channel` (optional override; falls back to normal Slack channel routing/default channel)
  - `slack_daily_report_normalized_fee_coverage_lookback_weeks` (default `8`)
  - `slack_daily_report_normalized_fee_coverage_threshold_pct` (default `80`)
  - `slack_daily_report_normalized_fee_coverage_consecutive_weeks` (default `2`)
  - Daily report now evaluates normalized fee-source coverage by week and adds an alert line when threshold/consecutive-week conditions are met.
  - Admin UI: `Admin -> Integrations -> Slack Notifications`
  - Visibility:
    - `Admin -> Integrations -> Slack Notifications` includes `Fee Coverage Health (Daily Report Input)` live preview.
    - `Admin -> System Health` includes `eBay Fee Coverage Health` for operational monitoring.
- Scheduled backup Slack notifications runtime keys:
  - `slack_notify_backup_success`
  - `slack_notify_backup_failures`
  - `slack_channel_backup_events` (optional override)
  - Admin UI: `Admin -> Integrations -> Slack Notifications`
- Notification routing runtime keys (`slack`, `email`, `both`, `disabled`):
  - `notification_route_sync_failures`
  - `notification_route_daily_report`
  - `notification_route_backup_events`
  - `notification_route_system_health_critical`
  - `notification_route_business_reports`
  - Admin UI: `Admin -> Integrations -> Notification Routing`
- Notification outbox runtime keys:
  - `notification_outbox_runner_enabled`
  - `notification_outbox_runner_limit`
  - `notification_outbox_backoff_base_seconds`
  - `notification_outbox_backoff_max_seconds`
  - `notification_outbox_retain_sent_days`
  - `notification_outbox_retain_failed_days`
  - `notification_outbox_cleanup_enabled`
  - `notification_outbox_cleanup_timezone`
  - `notification_outbox_cleanup_local_time`
  - Outbox query hot paths are indexed for due-dispatch and dedupe lookup (`ix_notification_outbox_env_status_due_id`, `ix_notification_outbox_env_channel_dedupe_status`).
  - Outbox due processing now uses DB-side due-window filtering (`due_before`) plus direct row lookup (`get_notification_outbox`) to reduce broad queue scans.
  - Outbox enqueue now reuses existing rows by `dedupe_key` (same env/channel; statuses `queued`/`retrying`/`processing`/`sent`) to avoid duplicate dispatch rows.
  - System Health shows due/retrying/failed/sent outbox visibility in `Admin -> System Health`.
  - Admin Integrations includes `Notification Outbox Controls` for saveable settings plus one-click `Run Outbox Now` and `Run Cleanup Now` actions.
  - System Health includes `Outbox Runner Activity` showing the latest process/cleanup runs (manual or scheduled) with status and details preview.
- Report rollup hot-path index additions:
  - `ix_sales_sold_at_id`
  - `ix_orders_sold_at_id`
  - `ix_order_finance_entries_kind_txdate_created`
  - Added via migration `0053_report_hotpath_idx` to improve report/date-window and rollup-EXPLAIN query performance.
  - `ix_returns_returned_at_id`
  - `ix_marketplace_listings_listed_at_id`
  - `ix_marketplace_listings_created_at_id`
  - Added via migration `0054_returns_listing_date_idx` to improve returns/listings date-window probe performance in rollup-EXPLAIN workflows.
  - `ix_products_acquired_at_id`
  - `ix_product_lot_assignments_acq_id`
  - `ix_inventory_movements_occ_at_id`
  - Added via migration `0055_report_time_window_idx` to improve products/lot-assignments/inventory-movements date-window reporting probes.
  - Nullable date-window report filters now use bounded `created_at` fallback (for products/listings/lot-assignments) instead of unbounded `IS NULL` branches to reduce broad scans.
- Lifecycle archive retention cleanup runtime keys:
  - `lifecycle_archive_cleanup_enabled`
  - `lifecycle_archive_cleanup_timezone` (example: `America/Denver`)
  - `lifecycle_archive_cleanup_local_time` (`HH:MM`, local time in configured timezone)
  - `lifecycle_media_archive_retain_days` (days to keep archived media before cleanup delete)
  - `lifecycle_listing_archive_retain_days` (days to keep archived listings before cleanup delete)
  - `lifecycle_lot_archive_retain_days` (days to keep archived lots before cleanup delete)
  - `lifecycle_product_archive_retain_days` (days to keep archived products before cleanup delete)
  - Cleanup is dependency-aware for listings/lots/products; linked records are skipped and reported.
  - Sync runner writes integration events with `integration=lifecycle_retention`, `action=cleanup`.
- Business status report send-now controls:
  - `Send Daily Business Snapshot`
  - `Send Weekly Business Summary`
  - `Send Inventory Risk Snapshot`
  - `Send This Preview Now` from dry-run preview card
  - `Copy/Download Payload (.txt)` from dry-run preview card
  - Optional channel override: `slack_channel_business_reports`
  - Optional templates: `slack_template_business_status_report`, `slack_template_inventory_risk_report`
  - Dry-run preview card includes resolved route/event/channel and final rendered Slack payload text before dispatch
- Admin Integrations includes one-click channel preset seeding for current env.
- Inbound conversational Slack Ops bot (Socket Mode):
  - Worker service: `docker compose up -d slack_ops_worker`
  - Kubernetes: Slack Ops worker is part of the main env stacks (no separate Argo app):
    - `k8s/templates/dev/deployment-slack-ops-worker.yaml`
    - `k8s/templates/prod/deployment-slack-ops-worker.yaml`
  - Runtime keys:
    - `slack_ops_runner_enabled`
    - `slack_app_token` (`xapp-...`)
    - `slack_bot_token` (`xoxb-...`)
    - `slack_bot_user_id` (optional)
    - `slack_ops_process_queue_enabled`
    - `slack_ops_process_queue_limit`
    - `slack_ops_poll_interval_seconds`
    - `slack_ops_default_role`
    - `slack_ops_user_role_map` (example: `U123:admin,keith:ops`)
    - `slack_ops_command_prefix` (optional, example `gs`)
  - Slack app minimum scopes/events:
    - scopes: `chat:write`, `app_mentions:read`, `files:read`
    - Socket Mode enabled with app token scope `connections:write`
    - bot event subscription: `app_mention`
  - Message examples:
    - `@Goldenstackers comp 1 oz silver round`
    - `@Goldenstackers intake 1881 morgan dollar` (+ image/file attachments)
    - `@Goldenstackers intake 1881 morgan dollar qty=1 cost=225.00 category=coins`
    - `@Goldenstackers status sync`
    - `@Goldenstackers operations run_due slack_ops 25`
    - `@Goldenstackers operations approve 1234`
    - `@Goldenstackers operations create_ebay_draft 46 19.99 2`
  - Intake automation:
    - `intake` commands with attachments can auto-create a local product draft when `product_id` is not provided.
    - AI-assist extracts draft defaults (title/category/description/metal/weight hints), media links to the new product, and threaded Slack summary includes missing confirmations to finalize.
  - Backward-compatible aliases:
    - `operations run_sync` -> `operations run_due slack_ops`
    - `operations queue_status` -> `status`
  - Comp behavior (latest):
    - Slack `comp` now fetches eBay sold-results HTML rows as primary eBay evidence before AI summary.
    - Legacy Finding API is non-primary/inactive for Slack runtime comp execution.
    - Slack `comp` supports web fallback hints when eBay rows are unavailable (`slack_ops_comp_web_fallback_enabled`, `slack_ops_comp_web_fallback_limit`).
    - Web fallback structured-page fetch is now bounded for stability/perf via:
      - `slack_ops_comp_web_detail_fetch_limit` (default `3`)
      - `slack_ops_comp_web_detail_fetch_timeout_seconds` (default `10`)
    - Minimum-confidence gate is now available to suppress low-trust suggested bands and add explicit caution messaging:
      - `slack_ops_comp_min_confidence_gate_enabled` (default `true`)
      - `slack_ops_comp_min_confidence_score` (default `3.5`)
    - Minimum-qualified-rows gate is now available to suppress suggested bands when too few priced comp rows are available:
      - `slack_ops_comp_min_qualified_rows_gate_enabled` (default `true`)
      - `slack_ops_comp_min_qualified_rows` (default `2`)
    - Trusted-source-only mode is now available for web fallback:
      - `slack_ops_comp_trusted_sources_only_enabled` (default `false`)
      - `slack_ops_comp_trusted_web_domains_csv` (optional CSV override; defaults to known bullion dealer domains)
    - Suggested list band in the comp summary is runtime-configurable (`slack_ops_comp_band_low_pct`, `slack_ops_comp_band_high_pct`) and supports decimal percentages.
    - Comp result metadata includes query/fetch context (`eBay rows`, `Web rows`, query used, fetch mode) for operator trust/triage.
  - AI Accountant behavior:
    - Slack `accountant <question>` is available to ops/admin users as a read-only AI Accountant interaction; `accounting`, `tax`, `ai-accountant`, `ai_accountant`, and `aiaccountant` are accepted aliases.
    - It uses the same accounting snapshot evidence as Ask GoldenStackers plus `accountant_llm_system_message` / `ai_accountant_chat_instruction`.
    - External web research is enabled by default and controlled by `ai_accountant_web_research_enabled`, `ai_accountant_web_research_limit`, and `ai_accountant_web_research_timeout_seconds`; web results are context only and tax/legal determinations still require advisor validation.
- Current behavior is contract + guardrails only (no direct paid API pull yet); use licensed export/manual import path until endpoint/legal contract is finalized.

Scheduled DB backup runner:
- `sync_runner` now supports automatic DB backup execution + optional S3 upload once per local day after configured time.
- Runtime keys:
  - `backup_policy_enabled`
  - `backup_policy_runner_enabled`
  - `backup_policy_schedule_timezone` (example: `America/Denver`)
  - `backup_policy_schedule_local_time` (`HH:MM`, local time in configured timezone)
  - `backup_policy_include_drop_statements`
  - `backup_policy_upload_to_s3`
  - Admin UI: `Admin -> Backups -> Backup Policy`

## eBay (Phase 1)

The `eBay` page currently supports:
- OAuth authorize URL generation
- automatic OAuth callback code exchange (in-app)
- manual auth code exchange fallback
- privilege API check

### eBay OAuth Setup (Developer Portal)

1. Go to `Application Keys` in the eBay Developer Portal.
2. Under your keyset (Sandbox first), open `User Tokens`.
3. Create or edit your `RuName` (redirect URL name).
4. Fill these fields:
   - `Display Title`: `GoldenStackers Inventory`
   - `Privacy Policy URL`: your public privacy page URL
   - `Auth Accepted URL`: your HTTPS callback/success URL
   - `Auth Declined URL`: your HTTPS cancel/failure URL

For your question about what to put:
- `Your auth accepted URL1` should be something like:
  - `https://inventory.goldenstackers.com/eBay_Workspace` (production)
- `Your auth declined URL1` should be something like:
  - `https://inventory.goldenstackers.com/eBay_Workspace` (production)

Important:
- eBay requires **HTTPS** for both accepted and declined URLs (sandbox and production).
- Use URLs you control.
- Sandbox and Production each have their own `RuName`.

### How this maps to this app

- `.env` `EBAY_RU_NAME` = the RuName string generated by eBay (not a URL).
- This app uses `EBAY_RU_NAME` for the OAuth `redirect_uri` parameter.
- Optional but recommended:
  - `.env` `EBAY_AUTH_ACCEPTED_URL` (shown in eBay UI/Admin for callback visibility)
  - `.env` `EBAY_AUTH_DECLINED_URL` (shown in eBay UI/Admin for callback visibility)
  - If these are not set, app defaults are environment-based:
    - `APP_ENV=production` -> `https://inventory.goldenstackers.com/eBay_Workspace`
    - `APP_ENV=dev` -> `https://dev-inventory.goldenstackers.com/eBay_Workspace`
    - `APP_ENV=local` -> `http://localhost:8501/eBay_Workspace`
- Optional: `.env` `EBAY_USER_ACCESS_TOKEN` can be set as a default token for eBay/API verification and pull forms.
- Optional but recommended: `.env` `EBAY_USER_REFRESH_TOKEN` enables access-token renewal for long-lived operations.
- Runtime setting `ebay_allow_sandbox_seller_ops=true` enables seller-operation controls (publish/revise/end/relist + policy refresh) while `EBAY_ENVIRONMENT=sandbox` (env fallback: `EBAY_ALLOW_SANDBOX_SELLER_OPS`).
  - Default behavior is `false` to avoid repeated sandbox onboarding/policy API failures in normal dev workflows.
- Runtime setting `ebay_require_runbook_for_bulk_ops=true` requires the eBay Workspace runbook checklist to be complete before bulk eBay Ops actions are enabled (end/relist/revise queue/category-policy bulk apply).
  - Default behavior is `false` (non-blocking), so teams can adopt runbook enforcement gradually per environment.
- For publish-to-eBay flow, set these defaults in `.env` (or provide per publish in UI):
  - `EBAY_MARKETPLACE_ID` (example: `EBAY_US`)
  - `EBAY_CURRENCY` (example: `USD`)
  - `EBAY_CONTENT_LANGUAGE` (example: `en-US`)
  - `EBAY_MERCHANT_LOCATION_KEY`
  - `EBAY_PAYMENT_POLICY_ID`
  - `EBAY_FULFILLMENT_POLICY_ID`
  - `EBAY_RETURN_POLICY_ID`

### Local Development Note

For local testing, use an HTTPS tunnel or hosted HTTPS page for accept/decline URLs (for example, a temporary HTTPS endpoint). After eBay redirects back with `?code=...`, the app now auto-exchanges and stores tokens directly in runtime settings.

When sandbox seller onboarding is blocked/flaky, keep runtime setting `ebay_allow_sandbox_seller_ops=false` and use sandbox only for OAuth + basic API checks. Run full seller ops against production with a controlled test listing process.

Next phase:
- add eBay listing-state pull/reconciliation (ended/relisted/cancelled drift) so local listing lifecycle mirrors channel state continuously.
- expand finance-ledger ingestion for exact per-order fee + label accounting across reporting periods and payout reconciliation.
- monitor GS-V10-017 lifecycle controls in CI/local runs and complete operational sign-off entries using the new lifecycle policy tracker.

## Notes

- Keep README + inline docstrings updated as business workflows evolve.
- Streamlit theme is configured in `.streamlit/config.toml` and currently uses dark mode.
