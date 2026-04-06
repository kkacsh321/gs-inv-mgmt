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
- Go-live checklist: [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md).
- QA test plan: [QA_TEST_PLAN.md](QA_TEST_PLAN.md).

Core stack:
- Python + Streamlit (employee/admin UI)
- PostgreSQL (inventory/listing/sales/media metadata)
- AWS S3 (images/videos for inventory + listings)
- Alembic (database migrations)
- Docker (local development)
- Kubernetes + Kustomize overlays (dev/prod)

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
- eBay publish flow can push selected listing images to eBay EPS and upload one MP4 listing video via eBay Media API
- Manage existing eBay-linked listings in app with revise/end/relist actions (offer lifecycle controls)
- Unified eBay Workspace page (integration + ops tabs) with legacy eBay/eBay Ops compatibility routes
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
- Search/edit workflows for products, listings, sales, and media
- Audit log for create/update activity tracking
- Orders domain with multi-line items and optional linked sale creation
- Returns workflow with refund/disposition tracking and optional restock handling
- Data-quality validation guards (required fields, duplicate IDs, tracking/amount sanity checks)
- Role-based access basics (`viewer`/`ops`/`admin`) with session identity adapter and write guardrails
- Admin page for managing users and role-permission mappings
- Password-capable app users (hashed+salted) with login/logout sidebar flow
- Global page auth gating when `APP_REQUIRE_PASSWORD_AUTH=true` (content blocked until sign-in)
- Optional persistent sign-in restore across app restarts via signed remember token (`APP_AUTH_SIGNING_KEY`, `APP_AUTH_REMEMBER_DAYS`). Browser-cookie mode is optional and off by default (`APP_AUTH_COOKIE_ENABLED=false`) due Streamlit component compatibility. Query-token URL fallback is runtime-configurable (`APP_AUTH_QUERY_TOKEN_FALLBACK_ENABLED`, runtime key `auth_query_token_fallback_enabled`).
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
- Current tax settings can be saved back to runtime defaults from the Documents page
- Auto tax mode supports per-line taxable/exempt overrides for mixed invoices
- Reports include estimated tax outputs (`Tax Summary`, `Tax by Marketplace`, `Tax Detail`) scoped by date range, jurisdiction context, and selected marketplaces
- Reports tax presets include `Golden Local Retail`, `Marketplace Shipped`, and `Bullion Exempt Focus`
- Reports include Tax Drilldown filters (marketplace + taxable/exempt segment) with focused exports
- Reports Tax Drilldown includes `Open in Documents` handoff to prefill invoice draft context from a selected sale
- Reports include a general `Document Draft Handoff` panel to prefill Documents from either Sales or Orders (invoice/receipt)
- Sales and Orders side panels include direct `Open in Documents` actions for one-click invoice/receipt draft prefill
- eBay Workspace includes `Document Draft Quick Handoff` from recent eBay orders/sales into Documents
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
│   │   └── 23_Inventory_Intake_Wizard.py
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
python -m coverage report -m --fail-under=30
# Scoped-core gate (repository/services/auth/page_common/config)
python -m coverage report -m \
  --include="app/repository.py,app/services/*.py,app/auth.py,app/page_common.py,app/config.py" \
  --fail-under=85
python -m coverage xml -o coverage.xml

# Playwright dependencies (one-time)
npm install
npx playwright install chromium

# Browser smoke tests
npx playwright test

# Run intake wizard critical path only
npx playwright test tests/e2e/intake_wizard.spec.ts

# Run products create/edit critical path only
npx playwright test tests/e2e/products_flow.spec.ts

# Run auth sign-in/sign-out critical path only
npx playwright test tests/e2e/auth_flow.spec.ts

# Run listings draft/review preflight critical path only
npx playwright test tests/e2e/listings_flow.spec.ts

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
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8501 npx playwright test
```

Notes:
- CI workflow `qa_tests.yaml` writes a CI `.env`, starts `db/migrate/app` with Docker Compose, waits for app readiness, then runs Playwright Chromium smoke tests.
- Browser artifacts are uploaded on CI runs (`playwright-report`, `test-results`) for triage.
- Coverage artifacts are uploaded on CI runs (`.coverage`, `coverage.xml`, `coverage.json`).
- QA evidence artifact is uploaded on CI runs (`qa-evidence`) with sign-off-ready summary files (`qa_evidence.md`, `qa_evidence.json`) and also written to the GitHub job summary.
- Coverage gates are enforced in CI to prevent regression: global `>=30%` and scoped-core `>=85%` (`repository/services/auth/page_common/config`), with ratchet targets tracked in roadmap/checklists (`40% -> 55%+` and scoped-core `>=95%`).
- Latest local QA baseline (2026-04-02): `604` unit tests passing, global coverage `~41.77%`, scoped-core coverage `~95.01%` (with `app/repository.py` now `~93.97%`).
- Local Playwright defaults now use `E2E_USERNAME=e2e` and `E2E_PASSWORD=e2e-password-123` when env vars are not provided.
- Local seed now enforces required permissions for the configured e2e role by default (`E2E_ENSURE_ROLE_PERMISSIONS=true`) to keep browser e2e flows deterministic.
- Seed scripts create/update this `e2e` app user automatically in non-prod (`make db-seed` or `docker compose run --rm seed`).
- Override credentials by setting `E2E_USERNAME` / `E2E_PASSWORD` in your environment.

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
- `SYNC_RUNNER_ENABLED=false` makes the worker exit without scheduling.
- `SYNC_RUNNER_RUN_ONCE=true` runs one pass then exits (useful for smoke tests/jobs).
- Runtime setting `sync_job_ebay_orders_pull_import_enabled` controls eBay order pull/import (env fallback: `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED`).
- `SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED` controls eBay tracking push.
- `SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_ENABLED` controls scheduled eBay token/identity/privilege health checks.
- `SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_INTERVAL_MINUTES` sets minimum cadence for health checks.
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
  - eBay sold/completed comps via Finding API
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
- `invoicing_tax_rate_percent_default` (default: `8.81`)
- `invoicing_tax_shipping_taxable_default` (`true|false`)
- `invoicing_tax_exempt_categories_csv` (default: `bullion,coins`)
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
- Admin Integrations includes one-click channel preset seeding for current env.
- Current behavior is contract + guardrails only (no direct paid API pull yet); use licensed export/manual import path until endpoint/legal contract is finalized.

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
- pull active eBay listings into local `marketplace_listings`
- ingest eBay orders into `sales`
- reconcile quantity and listing status automatically

## Notes

- Keep README + inline docstrings updated as business workflows evolve.
- Streamlit theme is configured in `.streamlit/config.toml` and currently uses dark mode.
