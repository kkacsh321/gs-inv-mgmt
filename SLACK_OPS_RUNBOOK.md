# Slack Ops Bot Runbook (GS-V10-019)

This runbook covers production operation of the Slack AI Ops Bot pipeline:

- Slack request ingest (`app/services/slack_ops_bot.py`)
- Slack Socket Mode inbound runner (`app/services/slack_ops_runner.py`)
- Queue execution (`integration=slack_ops`, `action=command_ingest`)
- Admin governance/triage controls
- System Health monitoring and incident response

## Runtime Controls

Primary runtime settings:

- `slack_ops_enabled`
- `slack_ops_intent_intake_enabled`
- `slack_ops_intent_comp_enabled`
- `slack_ops_intent_status_enabled`
- `slack_ops_intent_operations_enabled`
- `slack_ops_allowed_channels`
- `slack_ops_allowed_users`
- `slack_ops_rate_limit_window_minutes`
- `slack_ops_rate_limit_max_requests`
- `slack_ops_write_actions_require_approval`
- `slack_ops_ai_assist_enabled`
- `slack_ops_ai_auto_reply_enabled`
- `slack_app_token`
- `slack_bot_token`
- `slack_bot_user_id`
- `slack_ops_runner_enabled`
- `slack_ops_process_queue_enabled`
- `slack_ops_process_queue_limit`
- `slack_ops_poll_interval_seconds`
- `slack_ops_comp_band_low_pct` (default `90`; supports decimal values like `92.5`)
- `slack_ops_comp_band_high_pct` (default `110`; supports decimal values like `115`)
- `slack_ops_comp_ebay_html_fallback_enabled` (default `true`)
- `slack_ops_comp_ebay_html_fallback_limit` (default `20`)
- `slack_ops_comp_web_fallback_enabled` (default `true`)
- `slack_ops_comp_web_fallback_limit` (default `10`)
- `slack_ops_comp_web_detail_fetch_limit` (default `3`)
- `slack_ops_comp_web_detail_fetch_timeout_seconds` (default `10`)
- `slack_ops_comp_min_confidence_gate_enabled` (default `true`)
- `slack_ops_comp_min_confidence_score` (default `3.5`)
- `slack_ops_comp_min_qualified_rows_gate_enabled` (default `true`)
- `slack_ops_comp_min_qualified_rows` (default `2`)
- `slack_ops_comp_trusted_sources_only_enabled` (default `false`)
- `slack_ops_comp_trusted_web_domains_csv` (optional CSV override; empty uses built-in bullion-domain allowlist)
- `slack_ops_default_role`
- `slack_ops_user_role_map`
- `slack_ops_command_prefix`

Comp data-source note:
- Slack `comp` now uses eBay sold-results HTML as the primary eBay source.
- Legacy eBay Finding `findCompletedItems` is treated as deprecated/non-primary and is no longer used in Slack runtime comp execution.
- Web fallback detail-page fetches are intentionally bounded (count + timeout) to prevent slow/risky scrape fan-out in worker runs.
- Web fallback parsing now excludes shipping-threshold promo values (for example `free shipping on orders $199`) from listed-price hints to reduce false comp anchors.
- Web fallback now prefers product-detail URLs over category/search URLs when priced product rows are available (for example, APMEX `/product/...` preferred over `/category/...`).
- Non-product category/search URLs no longer contribute snippet-only prices; they need structured page-price extraction to be considered priced comps.
- Structured page extraction now includes JSON-LD product/offer price parsing and `og:price:amount` hints to improve price capture on product pages with sparse snippets.
- Top-comp summary ordering now uses confidence weighting (sold/structured/product-like evidence first), so high-price snippet-only rows no longer dominate the first-ranked comp by default.
- Slack comp summary now includes explicit confidence labels on each top comp row (`[high|medium|low]`) plus aggregate `Evidence confidence` in the comp stats snippet.
- When minimum-confidence gate is enabled and evidence score is below threshold, suggested list band is suppressed and a caution note is appended; fetch mode includes `low_confidence_gate`.
- When minimum-qualified-rows gate is enabled and priced comp rows are below threshold, suggested list band is also suppressed and the caution note includes the row-count deficit.
- When trusted-source mode is enabled, web fallback keeps only rows whose domain is in the trusted allowlist; summary links include how many web rows were removed by that filter.
- Per-request comp override is supported: `trusted_only=true|false` (recorded in summary links as `Trusted-source override: ...`).
- Additional per-request comp gate overrides are supported:
  - `confidence_gate=true|false`
  - `rows_gate=true|false`
  - `min_confidence=<float>`
  - `min_rows=<int>`
- Per-request trusted domains override is supported:
  - `trusted_domains=domain1.com,domain2.com` (applies to trusted-source filtering for that request)
  - When `trusted_domains=` is provided, trusted-source filtering is auto-enabled for that request unless `trusted_only=false` is also supplied.

Safe baseline:

- keep `slack_ops_write_actions_require_approval=true`
- keep `slack_ops_ai_auto_reply_enabled=false` until operator trust is established
- scope `slack_ops_allowed_channels` and `slack_ops_allowed_users` to known operator lanes
- keep `slack_ops_runner_enabled=false` until Slack app scopes/subscriptions are verified

## Slack App Setup (Inbound Conversation)

Use Socket Mode for inbound bot chat:

1. Slack app settings:
   - Enable Socket Mode (create App-Level Token with `connections:write`).
   - Enable Event Subscriptions and subscribe bot events:
     - `app_mention`
   - Add OAuth bot scopes:
     - `chat:write`
     - `app_mentions:read`
     - `files:read` (required for attachment ingest)
2. Install/reinstall app to workspace after scope changes.
3. In app runtime settings:
   - set `slack_notifications_enabled=true`
   - set `slack_bot_token` (`xoxb-...`)
   - set `slack_app_token` (`xapp-...`)
   - set `slack_ops_runner_enabled=true`
   - set `slack_ops_process_queue_enabled=true`
4. Start worker service:
   - `docker compose up -d slack_ops_worker`
   - Kubernetes (integrated stack): deploy main env stack that now includes:
     - `k8s/templates/dev/deployment-slack-ops-worker.yaml`
     - `k8s/templates/prod/deployment-slack-ops-worker.yaml`
5. Validate:
   - mention bot in allowed channel: `@Goldenstackers comp 1oz silver round`
   - check queue + status in `Admin -> Integrations -> Slack Ops Queue (Bot)`
   - check health metrics in `Admin -> System Health -> Slack Ops Queue Health`

## Command Patterns

Supported intents:

- `comp <query>`
- `intake <item hint>` (attach image/files for best results)
- `status <scope>`
- `operations <action>`

Examples:

- `@Goldenstackers comp 1 oz copper round`
- `@Goldenstackers comp 1 oz silver bar trusted_only=true`
- `@Goldenstackers comp 1 oz silver bar trusted_only=true trusted_domains=jmbullion.com,apmex.com`
- `@Goldenstackers comp 1 oz silver bar confidence_gate=false rows_gate=false min_confidence=9.0 min_rows=5`
- `@Goldenstackers intake 1881 morgan dollar` (+ photos)
- `@Goldenstackers intake 1881 morgan dollar qty=1 cost=225.00 category=coins`
- `@Goldenstackers status sync`
- `@Goldenstackers operations run_due slack_ops 25`
- `@Goldenstackers operations approve 1234`
- `@Goldenstackers operations create_ebay_draft 46 19.99 2`

Backward-compatible aliases:

- `operations run_sync` -> `operations run_due slack_ops`
- `operations queue_status` -> `status`

Intake draft behavior (current):

- If `intake` includes image/file attachments and no explicit `product_id`, the queue worker now:
  - ingests media/documents
  - runs AI-assisted intake field extraction
  - creates a local product draft automatically
  - links ingested media to that product
  - posts a threaded summary with created product ID/SKU and missing confirmations

## Normal Operations

1. In Slack, operator issues command (intent + args + optional files).
2. App routes/guards request and queues accepted command.
3. Write-intent commands are set to `blocked` pending approval.
4. Authorized approver (Admin Integrations panel) approves pending command.
5. Queue runner executes command; artifacts and optional AI summary are written back to queue payload and domain records.

Primary monitoring surfaces:

- `Admin -> Integrations -> Slack Ops Queue (Bot)`
- `Admin -> System Health -> Slack Ops Queue Health`

## Approval Escalation

When a write-intent command is time-sensitive:

1. Verify intent/request payload in Admin queue table.
2. Approve only if requester/target/channel are expected.
3. If out-of-hours escalation is needed, require second-operator acknowledgement in Slack thread before approval.
4. Add short rationale in ops notes (ticket/slack thread) linking queue job id.

## Incident Handling

### Symptom: unexpected requests or abuse

1. Set `slack_ops_enabled=false` (hard stop).
2. Tighten `slack_ops_allowed_channels` / `slack_ops_allowed_users`.
3. Review recent queue jobs + integration events for source and blast radius.
4. Re-enable only after guardrails are corrected.

### Symptom: queue backlog or high failure rate

1. Inspect `blocked`, `failed`, and `queued` counts in Admin/System Health.
2. Retry failed jobs after root-cause correction.
3. For command-specific failures, disable affected intent toggle temporarily.
4. Capture incident evidence (job ids, error text, recovery action).

### Symptom: noisy/rate-limited traffic

1. Lower blast radius by reducing `slack_ops_rate_limit_max_requests`.
2. Narrow `slack_ops_allowed_channels`.
3. Keep `slack_ops_enabled=true` only if traffic is contained; otherwise disable globally.

## Rollback / Disable Procedure

Fast rollback (no deploy):

1. `slack_ops_enabled=false`
2. keep queued history for audit (do not delete jobs during incident)
3. optionally pause queue retries for affected jobs (`blocked`/`failed` triage in Admin)

Functional rollback (partial):

1. Disable only specific intents (`slack_ops_intent_*_enabled=false`)
2. Keep read-only intents (`comp`, `status`) enabled if safe
3. Keep write intents gated with approval until confidence is restored

## Evidence and Audit Expectations

For go-live and incident closeout, capture:

- screenshot/export of Admin Slack Ops queue metrics
- screenshot/export of System Health Slack Ops section
- sample approved command lifecycle (`queued/blocked -> approved -> success`)
- integration event trail with job id and requester/approver context

## Ops Validation Checklist (Purchase-Document Auto Apply)

Use this quick check after deployments or runtime-setting changes affecting intake/accounting normalization.

1. Enable runtime key `purchase_doc_auto_apply_linked_lot_fields=true` in `Admin -> Runtime Settings`.
2. In either `Inventory Intake Wizard` or `Lots` purchase-document upload:
   - upload a purchase invoice/receipt,
   - keep AI extraction enabled,
   - ensure a purchase lot is linked.
3. Verify linked lot accounting fields are updated from extraction payload:
   - `vendor`
   - `purchase_date`
   - `total_cost`
   - `total_tax_paid`
   - `total_shipping_paid`
   - `total_handling_paid`
4. Verify audit trail (`entity_type=purchase_document`) includes:
   - `auto_apply_extracted_fields_to_lot` (runtime-enabled auto mode)
   - `manual_apply_extracted_fields_to_lot` (button-triggered mode)
5. Verify visibility/export in both surfaces:
   - `Reports -> Purchase Document -> Lot Apply Audit`
   - `Admin -> Purchase Document -> Lot Apply Audit`

### Rollback Check (Disable Auto Apply)

Use this when pausing automatic lot normalization while keeping document extraction available.

1. Set runtime key `purchase_doc_auto_apply_linked_lot_fields=false` in `Admin -> Runtime Settings`.
2. Upload another purchase document with extraction enabled and a linked lot.
3. Confirm linked lot accounting fields are **not** auto-updated by upload alone.
4. Use manual apply action and confirm updates happen only on explicit operator action.
5. Verify audit trail behavior:
   - no new `auto_apply_extracted_fields_to_lot` event for the rollback verification upload;
   - `manual_apply_extracted_fields_to_lot` appears only when manual apply is clicked.
