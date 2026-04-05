# UX Consolidation Plan (v0.6)

Objective: consolidate overlapping pages into fewer operational workspaces while preserving permissions, auditability, and feature parity.

## 1) Consolidation Targets

### eBay Domain (first)
- Current: `eBay` + `eBay Ops` split.
- Target: single `eBay Workspace` with role-aware tabs:
  - Auth & Verify
  - Live Listings
  - Draft/Review Queue
  - Lifecycle Actions (revise/end/relist)
  - Policies & Locations
  - Sync/Push History

### Other Domains (next)
- Inventory Ops:
  - Products + Lots + Inventory Movements + Sources
- Fulfillment Ops:
  - Orders + Shipping + Returns
- Sync Ops:
  - Sync page + queue controls + retry controls + unresolved errors
- Revenue Ops:
  - Sales + Documents + Reports

## 2) Shared Workspace UX Contract

Every consolidated workspace should use the same structure:
- Header:
  - workspace title
  - KPI chips (counts/alerts)
  - refresh and export actions
- Global filter row:
  - search, status, date range, owner, channel
  - saved view/preset controls
- Primary table:
  - standardized columns + status chips
  - multi-select for bulk actions
- Action rail:
  - domain-safe bulk operations
  - queue/dispatch/retry controls where applicable
- Side panel detail:
  - entity details
  - timeline/audit/sync lineage
  - quick edits

## 3) Navigation / IA Plan

- Move from page-per-entity toward workspace-per-workflow.
- Add role-aware defaults:
  - `ops`: Operations Home or Fulfillment Ops
  - `admin`: Admin or eBay Workspace
  - `viewer`: Dashboard or Reports
- Keep legacy pages reachable during migration with redirect guidance.

## 4) Rollout Strategy

Phase 1:
- Ship eBay Workspace behind feature flag: `ux_workspace_ebay_enabled`.
- Keep legacy eBay pages available.

Phase 2:
- Launch Inventory Ops and Fulfillment Ops workspaces behind flags.

Phase 3:
- Launch Sync Ops and Revenue Ops workspaces.
- Sunset legacy pages after parity checklist passes.

## 5) Parity Checklist (per workspace)

- Permissions: role checks identical to legacy.
- Audit: all mutating actions still logged.
- Timeline: sync/audit lineage still accessible.
- Bulk actions: no regression in capability.
- Exports: existing CSV/XLS/PDF flows preserved.

## 6) Success Metrics

- Fewer page switches per task.
- Lower median clicks for top operator workflows.
- Faster completion time for:
  - create + publish eBay listing
  - shipping exception resolution
  - sync failure triage and retry
- Reduced operator confusion (fewer “where is X?” support issues).

## 7) GS-V06-003 Service Consolidation Scopes

### Inventory Ops Workspace
- Primary operator goal: intake, classify, and prepare inventory for listing without page hopping.
- Pages consolidated:
  - Products
  - Lots
  - Inventory Movements
  - Sources
  - Inventory Intake Wizard
- Core queues:
  - needs source assignment
  - needs lot assignment
  - needs media
  - needs listing handoff
- Primary actions:
  - create/update product
  - assign/reassign lot and source
  - run AI title/description assist
  - handoff to draft listing

### Fulfillment Ops Workspace
- Primary operator goal: move orders to shipped/delivered while triaging exceptions quickly.
- Pages consolidated:
  - Orders
  - Shipping
  - Returns
- Core queues:
  - needs label
  - in transit
  - delivery exceptions
  - return pending
- Primary actions:
  - bulk status updates
  - tracking push/retry
  - exception resolve workflows
  - return intake and resolution

### Sync Ops Workspace
- Primary operator goal: keep integrations healthy and clear failures before SLA breaches.
- Pages consolidated:
  - Sync
  - Admin Sync Jobs panel
  - eBay push history views
- Core queues:
  - queued and blocked-by-config runs
  - failed/partial runs
  - unresolved sync errors
  - retry lineage chains
- Primary actions:
  - retry + execute-now
  - resolve errors
  - toggle jobs by environment
  - inspect run lineage and root causes

### Revenue Ops Workspace
- Primary operator goal: reconcile sales/orders/documents and produce clean exports.
- Pages consolidated:
  - Sales
  - Documents
  - Reports
- Core queues:
  - missing order/listing linkage
  - missing external order ids
  - invoice/receipt generation backlog
  - export-ready vs blocked
- Primary actions:
  - row-level reconciliation edits
  - export bundles (CSV/XLSX/PDF)
  - document generation and print

## 8) Migration Sequence + Dependency Map

Execution order:
1. Inventory Ops (lowest cross-domain risk, high UX impact)
2. Fulfillment Ops (depends on stable orders/shipping linkages)
3. Sync Ops (depends on queue semantics and lineage views now in place)
4. Revenue Ops (depends on stable order/listing/sale references)

Dependencies:
- Inventory Ops before broad listing automation to ensure clean product/lot/source lineage.
- Fulfillment Ops before final Revenue Ops exports to improve shipment/status integrity.
- Sync Ops controls must remain available during every migration phase.
- Legacy pages remain accessible behind redirects until parity check is complete per workspace.
