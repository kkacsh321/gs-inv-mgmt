# v0.5 Implementation Checklist (Tracked Tasks)

This checklist translates `ROADMAP.md` v0.5 scope into trackable implementation tasks.

Status legend:
- `[ ]` not started
- `[~]` in progress
- `[x]` complete

## GS-V05-001 AI Platform Foundation
- Status: `[x]`
- Goal: establish one consistent AI orchestration/runtime layer for all domain copilots and chat.
- Tasks:
  - [x] Add shared AI orchestration service (prompt templates, tool routing, retries, timeout/cost controls).
    - Added `app/services/ai_orchestration.py` with unified execution wrappers and citation payloads.
    - Wired Tools Comp Summary + Screenshot Review + Coin Grader + Coin Identifier through orchestration layer.
  - [x] Add domain context builders (inventory/listings/orders/shipping/sync/reports/admin).
    - Added reusable chat context builder service `app/services/chat_context_builders.py` for inventory/listings/sales/shipping/sync/orders/reports/admin/fallback snapshots.
    - Wired Ask GoldenStackers routing to consume builder service output and role-gated report/admin intents.
  - [x] Add provider fallback strategy (primary model + backup model/profile across providers like OpenAI/LocalAI).
  - [x] Add response citation schema (table/entity/time-window references in output payload).

## GS-V05-002 Domain Copilots (Embedded UX)
- Status: `[x]`
- Goal: provide page-native AI assistance where users already work, instead of isolated tooling.
- Tasks:
  - [x] Inventory/Coin/Product copilot: intake suggestions, title/spec normalization, SKU/category recommendations.
  - [x] Listings/eBay copilot: draft title/description/policy suggestions, readiness/risk checks, publish checklist.
  - [x] Integrate Comp Tool context handoff from Products/Listings (prefilled query/source/product context).
  - [x] Sales/Orders copilot: exception triage, mismatch explanations, refund/return guidance.
  - [x] Shipping copilot: queue prioritization, exception resolution suggestions, tracking risk summaries.
  - [x] Sync copilot: failure clustering, retry suggestions, run-lineage root-cause hints.
  - [x] Reports copilot: narrative summaries, margin anomaly explanations, export recommendations.

## GS-V05-003 In-App Data Chat (Ask GoldenStackers)
- Status: `[~]`
- Goal: add a secure conversational interface over operational data in the app.
- Tasks:
  - [x] Add dedicated Chat page with session history and transcript export.
  - [x] Add retrieval/query planner that can answer from DB data with explicit source references.
  - [x] Add “chat memory” scoping per user/session/environment with retention controls.
  - [x] Add quick prompts for common operations questions (inventory aging, eBay draft blockers, shipping exceptions, margin dips).
  - [x] Add optional AI refinement pass via shared orchestration/fallback layer (`chat_ai_refine_enabled`) while preserving read-only guardrails.
    - Added Admin AI Runtime controls for `chat_ai_refine_enabled`, `chat_ai_refine_system_message`, and `chat_ai_refine_instruction`.
    - Added chat-level session toggle and assistant-message `AI refined` indicator for operator clarity.
    - Added refinement lineage fields to AI chat audit metadata (provider/model/endpoint/fallback attempts).
    - Exposed refinement telemetry in Admin AI Usage dashboard (refined count + provider/model usage table).

## GS-V05-004 DB Query Safety + Governance
- Status: `[x]`
- Goal: ensure data chat and copilots are safe, role-aware, and auditable.
- Tasks:
  - [x] Add role-based table/field allowlists for AI query scope.
  - [x] Enforce read-only default for chat; write operations require explicit user confirmation workflow.
  - [x] Add max row/time/cost guardrails and safe-failure behavior for oversized/slow queries.
  - [x] Add policy layer for sensitive data redaction/masking in AI responses.
  - [x] Add AI interaction audit logs (prompt hash, scope, sources, action proposals, approval status).

## GS-V05-005 AI Admin Console
- Status: `[x]`
- Goal: let admins configure and control AI behavior without code changes.
- Tasks:
  - [x] Add endpoint-backed model discovery (`/models`) with manual override controls in AI Runtime profile create/edit flows.
  - [x] Add admin controls for per-domain copilot enable/disable toggles.
  - [x] Add role-level permissions for chat and copilot tools/actions.
  - [x] Add runtime prompt/system-template registry with versioning and rollback.
  - [x] Add usage telemetry dashboards (latency, errors, model/provider utilization, top prompt intents).
  - [x] Add config coverage tracking views for `.env` and runtime settings (missing/empty/default/overridden) with feature-flag-focused subsets.
  - [x] Add one-click action from Runtime Coverage to apply all missing runtime defaults immediately.
  - [x] Add config coverage exports (CSV) and identify custom/untracked runtime keys in coverage report.
  - [x] Add strict config-health warning panels for required env/runtime keys in Admin coverage views.
  - [x] Add config-health score indicators (healthy/warning/critical + percentage) for env/runtime coverage.
  - [x] Add one-click auto-fix actions for required env/runtime config gaps from coverage warning panels.
  - [x] Add one-click bulk default application actions for all missing/empty env keys and all missing/inactive runtime keys.
  - [x] Add Config Health snapshot into System Health view (required env/runtime readiness).
  - [x] Add top-level Admin Config Health summary card with required-key metrics and quick auto-fix actions.
  - [x] Extend top-level Admin Config Health summary with all-tracked-key missing/inactive counts and bulk default actions.
  - [x] Add top-level downloadable Config Health Snapshot JSON for operations handoff/audit.
  - [x] Centralize config-health policy definitions/thresholds in shared service and reuse across Admin + System Health.
  - [x] Add env drift detection for untracked `.env` keys and expose it in Admin coverage/summary and System Health.
  - [x] Add runtime drift detection for custom/untracked runtime keys in Admin coverage and top-level summary.
  - [x] Add runtime drift signal to System Health Config snapshot for operational visibility.

## GS-V05-006 System Health + Operational Diagnostics
- Status: `[x]`
- Goal: provide one page for runtime/service/integration health visibility and operational checks.
- Tasks:
  - [x] Add System Health page with CPU/load/memory/disk runtime metrics.
  - [x] Add DB + migration + sync-runner service checks.
  - [x] Add integration health panel (S3/eBay/spot/AI runtime profile summary).
  - [x] Add manual live-check actions for eBay token and spot quote fetch.
  - [x] Add background worker/container health checks and queue depth metrics.

## GS-V05-007 Google Workspace Integration Foundation
- Status: `[x]`
- Goal: enable Gmail/Calendar/Drive workflows for invoicing, follow-ups, and document collaboration.
- Tasks:
  - [x] Add Google auth/config model and admin settings UI (per environment).
    - Added Admin `Integrations` tab with DB-backed runtime settings for Google OAuth/client/scopes/default sender/Drive root folder.
  - [x] Add Gmail send action for invoices/receipts with template selection.
    - Added Documents-page `Send via Gmail` action using Google runtime settings and integration audit logging.
  - [x] Add Calendar event creation for follow-ups/shipment reminders.
    - Added Documents-page follow-up calendar event creation action with Admin-managed default calendar/timezone runtime settings.
  - [x] Add Drive upload/link flow for generated documents and media exports.
    - Added Documents-page Google Drive artifact upload for HTML/CSV/XLSX with link outputs and integration audit logging.
  - [x] Add audit trail + retry queue for Google integration actions.
    - Added DB-backed `integration_queue_jobs` table, automatic queueing on Google action failures, exponential backoff controls, and Admin `Google Retry Queue` run/retry controls.

## GS-V05-008 Voice Interface (Speech-To-Text + Text-To-Speech)
- Status: `[x]`
- Goal: support hands-free AI interactions in chat/copilot workflows while preserving existing safety controls.
- Tasks:
  - [x] Add browser mic capture + speech-to-text pipeline for AI chat input (per-role/per-env toggle).
  - [x] Add text-to-speech playback for AI responses with configurable voice/provider settings.
  - [x] Add runtime settings + admin UI for voice controls (enablement, provider, model/voice, language, limits).
  - [x] Add LocalAI option support for voice runtime with OpenAI-compatible audio endpoint handling.
  - [x] Add transcript/audit extensions to mark voice-origin prompts and spoken-response delivery metadata.
  - [x] Add graceful fallback UX when browser/device/provider voice capabilities are unavailable.

## GS-V05-009 Goldy Action Framework (In-App AI Tools)
- Status: `[ ]`
- Goal: enable GoldenStackers AI ("Goldy") to perform approved in-app operational actions safely.
- Tasks:
  - [ ] Add Goldy tool registry with role/environment permission mapping and explicit action policies.
  - [ ] Add action planner/preview UX (what Goldy intends to do, impacted entities, required approvals).
  - [ ] Add approval gate + execution pipeline for tool actions with idempotency and safe-failure handling.
  - [ ] Add execution audit model (requested_by, approved_by, tool/action, inputs hash, status, outputs, errors).
  - [ ] Implement first Goldy action set: inventory updates, listing draft ops, sync retry actions, and report/invoice assist actions.

## GS-V05-010 Slack Notification Integration Foundation
- Status: `[x]`
- Goal: provide operational notifications and alert routing into Slack for core business events.
- Tasks:
  - [x] Add Admin UI + DB-backed runtime settings for Slack token/channel/event toggles and daily summary schedule.
  - [x] Add notification service layer + templates for sync/shipping/sales/admin alerts.
    - Added `app/services/slack_notify.py` with runtime-configured `chat.postMessage` delivery and Admin test-send action.
    - Added first automatic alert hooks for sync run `failed/partial` outcomes and terminal Google queue retry failures.
    - Added runtime-editable Slack templates and routed dispatch helpers (`slack_template_sync_failures`, `slack_template_google_queue_failures`).
  - [x] Add retry/backoff + delivery audit for Slack posts.
    - Added Slack queue settings (`slack_queue_*`), DB-backed retry execution via `integration_queue_jobs`, and Admin Slack queue run/retry controls.
  - [x] Add channel routing rules by alert severity and environment.
    - Added runtime channel routing keys (`slack_channel_<event>`, `slack_channel_<severity>`, optional env+event overrides) used by dispatch layer.
    - Added one-click Admin channel preset helper for current environment.
  - [x] Add test-send controls in Admin and health checks in System Health.
    - Added Admin Integrations `Send Test Slack Message` action with integration audit logging.
    - Added System Health Slack status + manual `Run Slack Connectivity Check` and Admin recent Slack delivery-event table.
