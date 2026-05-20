# v0.3 Implementation Checklist (Tracked Tasks)

This checklist translates `ROADMAP.md` v0.3 scope into trackable implementation tasks.
Overall phase status: `[x] complete`

Status legend:
- `[ ]` not started
- `[~]` in progress
- `[x]` complete

## GS-V03-001 Sync Queue Foundation
- Status: `[x]`
- Goal: establish durable sync telemetry and retry-oriented tracking primitives.
- Tasks:
  - [x] Add schema tables `sync_runs`, `sync_events`, `sync_errors` via Alembic migration.
  - [x] Add SQLAlchemy models and repository CRUD/list methods for run/event/error logging.
  - [x] Add initial Sync page for viewing runs/events/errors and manual run status updates.
  - [x] Add background job runner that writes run lifecycle records automatically.
  - [x] Add queue state transitions (`queued` -> `running` -> terminal state) in manual sync flow.
  - [x] Add retry semantics and queued retry counters with parent-run linkage.
  - [x] Add retry execution action for eBay pull/import runs directly from Sync page.
  - [x] Add env-configured job toggles with UI + worker enforcement for safe enable/disable (`SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED`, `SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED`).
  - [x] Add Admin sync-job governance tab with central status visibility and env snippet generation for controlled toggle changes.
  - [x] Enforce job toggles on queued retry actions (`Retry Source Run`, `Retry Failed Run`, eBay push-history `Retry Run`) to prevent disabled-job queue buildup.
  - [x] Add queued-run hygiene guard to bulk-finalize queued runs whose jobs are disabled, with audit-note closure tagging.
  - [x] Add provider/job dispatcher scaffold (`execute_sync_job`) and sync job catalog to standardize future QuickBooks/Shopify job integration.

## GS-V03-002 eBay Pull Sync
- Status: `[x]`
- Goal: ingest eBay listings/orders into local canonical records without manual copy/paste.
- Tasks:
  - [x] Add eBay pull adapter service (orders + listing linkage + fulfillment tracking/shipping enrichment wired).
  - [x] Map eBay order payloads to local `orders`/`order_items`/`sales` upsert workflows with listing traceability.
  - [x] Add traceability quality metrics per run: `line_items_with_listing_link`, `line_items_unmapped_sku`, `auto_listings_created`.
  - [x] Persist per-entity sync events/errors with actionable diagnostics.
  - [x] Add manual run controls and latest-run summary in Sync page.

## GS-V03-003 Accounting Export + Reconciliation
- Status: `[x]`
- Goal: reduce bookkeeping prep time and improve month-end consistency.
- Tasks:
  - [x] Extend exports for sales/fees/shipping/refunds/COGS inputs.
  - [x] Add reconciliation report (channel totals vs local totals).
  - [x] Add quick validation checks and discrepancy flags.
  - [x] April 26, 2026 hardening: aligned dashboard, Reports, reconciliation, QBO-style exports, and Ask/chat report snapshots on the same net/profit convention (`gross + shipping charged - fees - label spend - COGS`).
  - [x] April 26, 2026 hardening: added assignment allocated dollars and allocation weights for mixed-value lots bought at one bulk price.
  - [x] April 26, 2026 hardening: added expected total quantity support for whole-lot cost allocation so partially checked-in lots do not overstate early sale COGS.
  - [x] April 28, 2026 hardening: QBO sales/refund adjustment exports now preserve COGS source, restocked return COGS reversal, and estimated return profit impact for close-review staging.

## GS-V03-004 Cost Basis and Margin Analytics
- Status: `[x]`
- Goal: produce reliable profit metrics across SKU, lot, and channel dimensions.
- Tasks:
  - [x] Implement lot-specific and FIFO cost-basis calculations.
  - [x] Add margin views for SKU/channel/period.
  - [x] Add exportable profitability reports.
  - [x] April 26, 2026 hardening: documented and tested cost-basis precedence across product-lot assignment costs, remaining lot-total allocation, product landed acquisition cost, and `product_cost` fallback.
  - [x] April 26, 2026 hardening: Ask/AI inventory and reports context now prefers FIFO/lot repository cost maps before product-level fallbacks.
  - [x] April 28, 2026 hardening: Purchase Lot P/L now FIFO-attributes sales across repurchase lots and exposes sold COGS source totals for selected lots.

## GS-V03-005 Media-Driven Listing Workflow
- Status: `[x]`
- Goal: make product/listing media immediately visible and streamline listing creation.
- Tasks:
  - [x] Add product and listing media galleries (image/video previews) directly on Products/Listings pages.
  - [x] Add camera-assisted media capture inputs (photo + video/file capture path) across Media/Products/Listings uploads.
  - [x] Add media file access actions (preview/download/open URL + bulk ZIP) and non-form enhanced capture flows in Media/Product/Listing create + manager sections.
  - [x] Add a “Create eBay Listing from Product” action that reuses product data + selected media.
  - [x] Persist linkage between source product media selections and generated marketplace listings.
  - [x] Add validation for required listing fields/media before publish/create.
  - [x] Add explicit main-image selection for eBay listing payloads so the chosen image is sent first in `imageUrls` and recorded in publish/revise/direct-post metadata.
  - [x] April 28, 2026 UX hardening: Products table and `Product Detail/Edit` now render as full-width inline sections instead of a narrow side panel.
  - [x] April 28, 2026 direct-post hardening: eBay dependency preflight blocks live auction publish when the selected payment policy requires immediate payment but no `Auction Buy It Now` price is set.

## GS-V03-006 eBay Seller Operations Console
- Status: `[x]`
- Goal: manage eBay publishing and ongoing operational actions directly from GoldenStackers app.
- Tasks:
  - [x] Add direct publish flow from Listings to eBay (inventory item + offer + publish).
  - [x] Add user/environment-scoped eBay publish presets with one-click apply.
  - [x] Add eBay media upload path (selected image EPS upload + one MP4/MOV video attach with MOV conversion, diagnostics, and non-blocking image-only warnings when no supported video is selected).
  - [x] Add eBay Taxonomy item-specifics lookup/cache per category and readiness blockers for missing cached required specifics.
  - [x] Add preflight guard for immediate-payment auction policy mismatches before calling eBay `publishOffer`.
  - [x] Add eBay operation dashboard tiles (drafts pending publish, active, ended, sync failures).
  - [x] Add listing revision/end/relist actions from app for existing eBay-linked listings.
  - [x] Add dedicated eBay Ops page with filtered listing tables and bulk end/relist/revise queue actions.
  - [x] Add eBay API Listings tab in eBay Ops showing live offer/listing status from eBay API.

## GS-V03-007 eBay Control Center Expansion
- Status: `[x]`
- Goal: complete day-to-day eBay operations entirely inside app with fewer manual portal checks.
- Tasks:
  - [x] Add eBay API listings visibility in operations console.
  - [x] Add bulk category/policy assignment helpers for publish/revise operations.
  - [x] Add merchant location + policy management tab in eBay Ops with API refresh and default-apply controls.
  - [x] Add sandbox-aware seller-ops guardrails with explicit override (`EBAY_ALLOW_SANDBOX_SELLER_OPS`).
  - [x] Add shipping/tracking push actions from local sales/orders to eBay.
  - [x] Add exception queue for eBay API failures with retry + resolution workflows.
  - [x] Add dedicated eBay push history views (run/event/error drill-down) in Shipping and eBay Ops.
  - [x] Add eBay push-history quick actions (retry, open in Sync, resolve unresolved run errors).

## GS-V03-008 Central Ops UX Foundation
- Status: `[x]`
- Goal: reduce operator friction and prepare app UX for true single-pane daily operations.
- Tasks:
  - [x] Define first-pass role-based landing dashboard actions (admin/ops/viewer) on Operations Home.
  - [x] Add “Operations Home” queue cards (needs listing, needs shipment, sync failures, accounting exceptions).
  - [x] Implement global saved filters/search patterns reused across Products/Listings/Sales/Orders.
  - [x] Persist saved filters in DB (`saved_filter_profiles`) for user/environment-scoped reuse across sessions/devices.
  - [x] Add default-per-scope and team-shared saved filters for cross-user operational consistency.
  - [x] Add admin controls for shared-filter ownership transfer and shared-filter delete governance.
  - [x] Standardize table row actions and edit panels for consistent cross-page workflows.
  - [x] Add entity timeline drawer (audit + sync lineage) for product/listing/order drill-down.
