# v0.4 Implementation Checklist (Tracked Tasks)

This checklist translates `ROADMAP.md` v0.4 scope into trackable implementation tasks.

Status legend:
- `[ ]` not started
- `[~]` in progress
- `[x]` complete

## GS-V04-001 Operations Home Command Center
- Status: `[x]`
- Goal: make Operations Home the default triage surface with faster routing and cleaner queue visibility.
- Tasks:
  - [x] Add marketplace-level filtering for cross-page queue consistency.
  - [x] Add sync-provider filtering for failed-run triage.
  - [x] Add queue-router table with one-click navigation actions.
  - [x] Fix sync queue field mapping to use canonical `job_name` + `completed_at`.
  - [x] Add SLA age indicators (e.g., oldest unshipped, oldest failed sync) with color thresholds.
  - [x] Add a clear inventory -> eBay -> shipping workflow stage table with stage-specific routing.
  - [x] Add dedicated eBay listing-workflow queue tab for fast draft/active listing operations.
  - [x] Add saved command-center view presets (by role/team).

## GS-V04-002 Global Workbench UX
- Status: `[x]`
- Goal: unify search/edit/list actions into one predictable interaction model.
- Tasks:
  - [x] Introduce shared table toolbar pattern (filter chips, save view, export, row count).
  - [x] Add consistent side-panel record detail/edit behavior across Products/Listings/Sales/Orders.
  - [x] Ship first side-panel edit slice for Products + Listings.
  - [x] Extend side-panel edit slice to Sales + Orders.
  - [x] Add keyboard-friendly quick actions and common shortcuts.

## GS-V04-003 Multi-Channel Listing Orchestration Foundation
- Status: `[x]`
- Goal: prepare publish workflows to scale beyond eBay without page sprawl.
- Tasks:
  - [x] Define channel readiness checks (required fields/media/policy mappings) in reusable service layer.
  - [x] Add unified listing-orchestration queue view (ready, blocked, published, error).
  - [x] Add channel capability matrix UI (eBay/Shopify/Whatnot/Facebook/Craigslist).
  - [x] Add first Comp Tool tab to pull eBay sold/completed comparables from inventory/title/image-hint queries for pricing decisions.
  - [x] Add web-search fallback comps when eBay returns no comparable rows.
  - [x] Add optional AI/LLM comp synthesis from eBay + web comp sets.
  - [x] Add Admin-managed AI runtime profiles (OpenAI/LocalAI, URL/model/endpoint/token defaults) with live DB-backed selection and env fallback.
  - [x] Enforce listing review gate (`draft` until approved) with review actions in Listings.

## GS-V04-004 Integration and Retry Abstraction Hardening
- Status: `[x]`
- Goal: ensure connectors share one execution model for predictable operations.
- Tasks:
  - [x] Extend sync dispatcher with provider/job metadata for run-now/retry compatibility checks.
  - [x] Add per-job retry policy defaults (max retries, backoff hint, terminal-state rule).
  - [x] Enforce per-job retry policy in Sync retry actions (`max_retries`, backoff window, retryable/terminal statuses) using Runtime Settings keys (`sync_job_<job_name>_*`).
  - [x] Add sync run lineage view linking source run -> retries -> terminal outcome.
  - [x] Add Admin config governance: `.env` visibility/edit controls for safe keys and DB-backed runtime settings for hot-configurable non-infra defaults.
  - [x] Move spot provider/symbol/base-url settings and selected sync job toggles to DB runtime override path (env fallback retained).

## GS-V04-005 Timeline and Audit Consolidation
- Status: `[x]`
- Goal: reduce investigation time by unifying local edits + sync events.
- Tasks:
  - [x] Add unified timeline component with event-type filters and date range.
  - [x] Add “why changed” diff cards for key entities (product/listing/order/sale).
  - [x] Add jump links from timeline events to owning pages and run IDs.

## GS-V04-006 Comp Capture Hardening
- Status: `[x]`
- Goal: increase web-fallback pricing capture quality so comp analytics are reliable beyond eBay sold data.
- Tasks:
  - [x] Add configurable web fallback result/detail-fetch limits in Comp Tool UI + runtime defaults.
  - [x] Improve web price regex parsing for more listing formats (`US $`, split cents, sale prices).
  - [x] Add destination-page extraction from embedded app-state JSON and metadata blocks.
  - [x] Add quantity-tier pricing extraction (`1-19`, `20+`, etc.) and derive low/high listed-price bands.
  - [x] Add domain-specific parsers for major sources (eBay item pages, Amazon, key bullion dealers).
  - [x] Add cents-normalization and superscript/tier-markup parsing to reduce false zero-price rows on dealer/ecommerce product pages.
  - [x] Add confidence scoring for extracted prices (`snippet`, `meta`, `json`, `tier_table`, `none`) and filter controls.
  - [x] Add parser/domain coverage telemetry in Comp Tool to make missed-price triage measurable by source + domain.
  - [x] Add async/cached fetch pipeline for better coverage without UI latency spikes.
  - [x] Add multimodal screenshot-review path for comp evidence images to supplement parsed web/eBay rows.
  - [x] Add reader-proxy fallback (`r.jina.ai`) when direct page fetch returns no usable prices (JS/bot-protected pages).
  - [x] Add keyword-led no-currency-symbol extraction (e.g., `as low as 7.99`, `only 9.99`) for modern ecommerce layouts.
  - [x] Expand metadata/embedded-key parsing (`og:price`, `twitter:data1`, `priceString`/`formattedPrice`) for more ecommerce templates.
  - [x] Improve web comp representative listed-price selection (median of extracted hints vs naive minimum) to reduce noisy underestimates.
  - [x] Broaden page-detail fetch candidate selection (priority-sorted, not only empty-hint rows) to recover prices when snippets are incomplete.
  - [x] Add additional domain parser coverage for Etsy/Walmart plus generic class-based price container fallback extraction.
  - [x] Add bullion-dealer focused domain parser expansion (APMEX/JM/SD/Monument/Provident/Bold/BGASC/etc.) plus domain-aware confidence weighting.
  - [x] Add Admin-configurable dealer-domain list runtime setting for comp parser/weighting updates without code changes.

## GS-V04-007 AI Numismatic Assistant Tools
- Status: `[x]`
- Goal: accelerate coin intake and listing workflows with image-based grading + identification guidance.
- Tasks:
  - [x] Add Coin Grader tool tab with camera/upload image inputs and AI grade-estimate markdown output.
  - [x] Add Coin Identifier tool tab with camera/upload image inputs and structured AI identification output.
  - [x] Add optional web-hint follow-up search from identifier keywords for market/context validation.
  - [x] Add history log for grader/identifier runs tied to products/listings.
  - [x] Add side-by-side obverse/reverse image handling and higher-confidence prompt templates.
  - [x] Add separate multimodal model selection in AI runtime config (Admin + runtime wiring), distinct from text model.
  - [x] Add full selected-profile edit UX for AI runtime configs (all fields + reliable profile-switch state hydration).
  - [x] Persist tool input media to media library for future listing/inventory reuse when product/listing context is provided.
  - [x] Add inventory-facing AI fields (`ai_graded`, `ai_grading_description`, `ai_description`) with product edit controls.
  - [x] Add create/update inventory actions from coin grading/identification/comp tool outputs.
  - [x] Add Coin Identifier robustness for truncated/malformed JSON outputs (repair + retry path).
  - [x] Add LocalAI multimodal endpoint/model guardrails and actionable error messaging.
  - [x] Add professional grading recommendation output requirements (submit YES/NO/CONDITIONAL with cost-threshold rationale).

## GS-V04-008 eBay Listing Templates and Branded HTML
- Status: `[x]`
- Goal: reduce repetitive listing setup time and standardize branded listing quality.
- Tasks:
  - [x] Add DB-backed eBay listing template profiles (environment/user/shared scopes).
  - [x] Add branded HTML description template editor with placeholders (title, SKU, condition, shipping policy notes).
  - [x] Add one-click template apply in Listings create/publish flows.
  - [x] Add preview/sanitize checks for HTML listing body before publish.
  - [x] Add template usage analytics (which template used per listing/publish run).

## GS-V04-009 Repeatable Inventory Lifecycle (Rebuy/Resell)
- Status: `[x]`
- Goal: support repeated acquisition and sale of the same SKU at different costs over time with clear traceability.
- Tasks:
  - [x] Add product-level repurchase/restock action that records inventory movement + optional lot assignment in one transaction.
  - [x] Add product lifecycle snapshot panel (acquired/sold/on-hand + recent lot assignments + movement history).
  - [x] Add cycle segmentation/reporting (inventory cycle IDs or period windows per SKU).
  - [x] Add weighted/lot cost trend chart for repeated rebuys over time.
  - [x] Add quick “buy/sell many times” operator workflow (batch receive + sale linkage for same SKU).
  - [x] Add inventory classification (`sellable`/`raw_material`/`supply`) and conversion workflows for material-to-product transformations.
  - [x] Add bulk multi-target conversion (one source SKU -> multiple sellable targets) with movement/audit traceability.

## GS-V04-016 Local-Sale Invoice Posting Flow
- Status: `[x]`
- Goal: support off-platform/local selling with clean invoice-first workflow and one-click transaction posting.
- Tasks:
  - [x] Add Listing as first-class source in Documents page (editable qty/price/fees/shipping).
  - [x] Add one-click posting from listing invoice to linked transaction records.
  - [x] Add optional linked `Order + OrderItem` creation when posting listing invoices.
  - [x] Add `Post & Open Sales` redirect to created sale row for operator speed.
  - [x] Add `Listing Outcome` branch (`Sold` vs `Not Sold / Remove Listing`) so no-sale outcomes are handled explicitly.

## GS-V04-010 Listing Review Workflow and Bulk Ops
- Status: `[x]`
- Goal: enforce review-first listing lifecycle and speed reviewer throughput.
- Tasks:
  - [x] Enforce review gate in readiness queue (non-approved listings blocked from ready/publish path).
  - [x] Add bulk review actions in Listings queue (approve/reject/pending with notes).
  - [x] Add reviewer productivity dashboard (pending count, age, approvals/day).
  - [x] Add optional two-person approval policy toggle for higher-risk channels.

## GS-V04-011 Listing Review Audit UX
- Status: `[x]`
- Goal: keep review decisions fully traceable for compliance and training.
- Tasks:
  - [x] Persist append-only review-history entries on each approve/reject/pending action.
  - [x] Show review-history timeline in Listings side panel.
  - [x] Add export/report view for review activity by reviewer/date/channel.

## GS-V04-012 Bulk Publish Planning Queue
- Status: `[x]`
- Goal: accelerate publish operations by batching reviewed-ready listings with clear dry-run validation.
- Tasks:
  - [x] Add Listings queue bulk publish dry-run validator (selected rows, publishable vs blocked reasons).
  - [x] Add batch-ID tagging on publishable listings for downstream execution grouping.
  - [x] Add execute-batch action to run actual eBay publish for tagged/publishable listings with partial-failure reporting.

## GS-V04-013 Bulk Publish Observability
- Status: `[x]`
- Goal: make bulk publish operations auditable and easy to troubleshoot from one place.
- Tasks:
  - [x] Add Listings-side bulk publish execution history table with filtering and exports.
  - [x] Persist and surface failed-row execution history (not only successful executions).
  - [x] Add quick retry action from history rows for failed listings.

## GS-V04-014 Coin Reference Database (Free-First)
- Status: `[x]`
- Goal: build an internal coin reference database with rich metadata and free-source workflows, without requiring paid Greysheet integration.
- Tasks:
  - [x] Add DB-backed `coin_reference_catalog` table for coin specs/reference/value bands.
  - [x] Add repository CRUD/search methods for coin reference records.
  - [x] Add Coin Database UI in Tools for filter/search/create/edit/export.
  - [x] Add development seed sample rows for common US coin types.
  - [x] Add CSV import/upsert workflow for public/free coin reference datasets.
  - [x] Add optional paid-source adapter contract (Greysheet/manual import) behind feature toggle and clear licensing guardrails.

## GS-V04-015 Coin-to-Inventory-to-eBay UX Flow
- Status: `[x]`
- Goal: make intake/listing flow cleaner by connecting coin reference data + AI context directly into product and listing creation.
- Tasks:
  - [x] Add explicit product -> coin reference link (`products.coin_reference_id`) in schema/model.
  - [x] Add Products create-flow coin-reference selector with one-click default field prefill.
  - [x] Add Products side-panel coin-reference edit/link controls.
  - [x] Add Listings create-flow context helpers: auto-title from product/coin reference and optional AI/coin-context detail injection.
  - [x] Add one-click "Create draft eBay listing from product panel" with smart defaults.
  - [x] Add dedicated intake wizard page (Reference -> AI assist -> Product -> Draft listing) for fastest daily operator path.
