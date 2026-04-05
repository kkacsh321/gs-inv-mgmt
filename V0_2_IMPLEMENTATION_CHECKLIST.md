# v0.2 Implementation Checklist (Tracked Tasks)

This checklist translates `ROADMAP.md` v0.2 scope into trackable implementation tasks.

Status legend:
- `[ ]` not started
- `[~]` in progress
- `[x]` complete

## GS-V02-001 Inventory Movements Ledger
- Status: `[x]`
- Goal: make every inventory quantity change traceable to a movement event.
- Tasks:
  - [x] Add `inventory_movements` table and indexes via Alembic migration.
  - [x] Add SQLAlchemy model and repository read method.
  - [x] Record movement on product creation (`initial_stock`) when quantity > 0.
  - [x] Record movement on sale create (`sale`) with quantity before/after.
  - [x] Record movement on sale update adjustments (`sale_adjustment_revert` / `sale_adjustment_apply`).
  - [x] Add movement report to UI exports.
  - [x] Add dedicated movement viewer page with filters and drill-down.
  - [x] Add tests for movement integrity and update edge cases.

## GS-V02-002 Orders Domain (Header + Line Items)
- Status: `[x]`
- Goal: support multi-item orders independent of single-sale records.
- Tasks:
  - [x] Add tables: `orders`, `order_items`.
  - [x] Add repository create/read/update operations.
  - [x] Add Orders UI page for create/search/edit.
  - [x] Link sales capture to orders when applicable.
  - [x] Add export/report coverage for order-level views.

## GS-V02-003 Returns and Refund Workflow
- Status: `[x]`
- Goal: support return intake and financial reconciliation.
- Tasks:
  - [x] Add `returns` table and status workflow.
  - [x] Implement restock / non-restock return outcomes.
  - [x] Capture refund amount, reason, and disposition.
  - [x] Add Returns UI page and reporting.

## GS-V02-004 Shipping Queue Improvements
- Status: `[x]`
- Goal: improve fulfillment operations and exception handling.
- Tasks:
  - [x] Queue tabs and bulk status updates.
  - [x] Tracking/provider/service/shipped/delivered fields.
  - [x] Bulk carrier operation presets.
  - [x] Shipment export files (carrier-ready CSV formats).
  - [x] Exception resolution workflow notes/actions.

## GS-V02-005 Data Quality Rules
- Status: `[x]`
- Goal: reduce bad data and duplicate records.
- Tasks:
  - [x] Add source master data (`inventory_sources`) and use in lot intake selection.
  - [x] Extend source records with optional `account_id` and `payment_method` for vendor/dealer payment traceability.
  - [x] Extend source records with optional `source_url` for vendor/dealer reference links.
  - [x] Validation service for create/update operations.
  - [x] Required-field enforcement by entity and workflow state.
  - [x] Duplicate guards for high-risk fields (orders, tracking, listing IDs).
  - [x] Tracking format and quantity/cost sanity checks.

## GS-V02-006 Role-Based Access Basics
- Status: `[x]`
- Goal: establish admin/ops/viewer guardrails with user attribution.
- Tasks:
  - [x] Add user identity + role model or identity adapter.
  - [x] Add role checks in UI actions and write operations.
  - [x] Restrict high-risk operations (edit/delete/bulk update) by role.
  - [x] Ensure audit events attribute real signed-in user identity.
  - [x] Add Admin page for managing users and role-permission mappings.

## GS-V02-007 Invoice and Receipt Documents
- Status: `[x]`
- Goal: generate branded, print-ready invoices and receipts from sales/orders.
- Tasks:
  - [x] Add dedicated Documents page in Streamlit navigation.
  - [x] Add invoice/receipt document generation from Order and Sale sources.
  - [x] Add multiple visual templates with configurable branding fields.
  - [x] Add saved template profiles in DB with per-environment defaults.
  - [x] Add printable HTML preview and HTML download.
  - [x] Add line-item CSV/XLSX export for downstream accounting workflows.

## GS-V02-008 Admin Operations Hardening
- Status: `[x]`
- Goal: make privileged operations safer and fully manageable in-app.
- Tasks:
  - [x] Enforce password-required user creation for new app users.
  - [x] Add login/logout session flow with page-level auth gating when password auth is enabled.
  - [x] Add Admin migrations tab with revision visibility and targeted upgrade support.
  - [x] Add guarded rollback/downgrade controls (non-prod UI enforcement).
  - [x] Add Admin maintenance actions for seed and operational data reset.
  - [x] Add Admin backups tab with SQL dump, S3 upload/list, and guarded non-prod restore.
