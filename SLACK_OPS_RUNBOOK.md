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
- `kurt <item hint>` (alias for inventory intake agent)
- `listing <product/listing hint>` / `murdock <product/listing hint>` (approval-gated listing-draft assistance)
- `customer <question>` / `customers <question>` / `repeat-buyer <question>` (read-only customer/repeat-buyer snapshot)
- `status <scope>`
- `operations <action>`

Customer intelligence defaults are seeded by migration `0072_customer_chat_defaults`; existing ops/admin Ask domain settings are updated to include `customers`, and Slack customer prompts/toggles are enabled by default.

Examples:

- `@Goldenstackers comp 1 oz copper round`
- `@Goldenstackers comp 1 oz silver bar trusted_only=true`
- `@Goldenstackers customer repeat buyers with notes`
- `@Goldenstackers comp 1 oz silver bar trusted_only=true trusted_domains=jmbullion.com,apmex.com`
- `@Goldenstackers comp 1 oz silver bar confidence_gate=false rows_gate=false min_confidence=9.0 min_rows=5`
- `@Goldenstackers intake 1881 morgan dollar` (+ photos)
- `@Goldenstackers kurt 1881 morgan dollar qty=1 cost=225.00 category=coins` (+ photos)
- `@Goldenstackers intake 1881 morgan dollar qty=1 cost=225.00 category=coins`
- `@Goldenstackers murdock product_id=123 write an exciting eBay description`
- `@Goldenstackers listing product_id=123 check blockers and draft listing copy`
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

Planned wizard simplification direction:

- App-native Business Chat Room priority:
  - the application Business Chat Room is the durable place for users and agents to coordinate,
  - the room is now a dedicated Streamlit chat page with chat bubbles and evidence upload support for images, videos, PDFs, CSVs, and documents,
  - Slack should remain a bridge for commands, alerts, and replies, but important agent/user coordination should be mirrored into the app room,
  - room messages are stored as `business_chat_room` audit events with room/thread/sender/source metadata.
  - Ask GoldenStackers user/agent turns mirror into the room with selected-agent, intent, elapsed-time, Goldy mode/role, and write-approval metadata.
  - app room notes infer addressed agents from text and aliases, storing normalized `directed_to` keys such as `kurt_intake_agent`, `murdock_listing_agent`, and `goldie_accountant_agent`.
  - agent replies infer and display handoff targets, so room messages can visibly direct follow-up to another specialist while keeping the human in the thread.
  - same-turn agent follow-ups are bounded by `business_chat_room_max_agent_replies`; this lets named specialists answer handoffs without allowing infinite agent chatter.
  - write/action prompts create blocked `business_chat_room/write_action_request` queue jobs with approval metadata; chat does not directly mutate inventory, listing, order, accounting, or integration records.
  - pending room-origin action requests are visible on the room page and blocked requests can be approved or cancelled there with in-room system audit messages.
  - approved room action jobs acknowledge safely in the integration queue and post back that no direct write ran until a workflow-specific executor is available.
  - room action requests include deterministic route metadata and recommended workflow handoffs for intake/listing/accounting/pricing/business-monitor/general requests.
  - approved room action requests save workflow draft handoffs scoped to `business_chat_room:<queue_job_id>` so operators can review/resume the routed workflow without direct chat writes.
  - Inventory Intake Wizard and Listing Wizard include Business Chat Room handoff inboxes for routed room requests; loading a handoff adds the approved prompt context to the wizard AI seed but does not create inventory, create listings, or publish.
  - handoff inboxes display review-card fields from embedded Kurt/Murdock draft contracts or safe prompt hints, and any prefill remains session-local until the operator submits the normal wizard workflow.
  - room action messages display inferred route metadata, and approval notices include the target workflow plus next step so Slack/app operators can see where the work will land before approving.
  - pending action requests in the room use a routed selector and direct workflow page links so operators do not need to manually copy queue IDs or guess the next workspace.
  - Slack-origin Kurt/Murdock draft contracts render in the room as field/confidence cards with missing confirmations and apply-plan status, making Slack work reviewable in the app.
  - app-origin room action requests for Kurt intake or Murdock listing also attach draft contracts to the approval payload, using only prompt-derived hints until a human reviews/applies them in the target workflow.
  - approved room workflow draft handoffs preserve draft contracts and apply plans, so the target wizard inbox still has structured review evidence after queue acknowledgement.
  - when an operator loads a room handoff in Inventory Intake Wizard or Listing Wizard, the app writes a workflow event for audit history while leaving product/listing writes to the normal submit path.
  - pending room approvals show the selected request's draft contract before approval so operators can review proposed fields and missing confirmations before queueing the handoff.
  - approve/cancel transitions also update embedded queue-payload approval metadata when available, preventing approved handoffs from still showing `pending` in downstream evidence.
  - destination wizard handoff inboxes show missing confirmations, proposed actions, and apply-plan status from the preserved room draft contract.
  - destination wizard handoff inboxes include `Mark Handoff Reviewed` to clear reviewed room-origin handoffs from the active inbox while preserving a workflow event.
  - Business Chat Room includes an active workflow handoff view so approved room-origin work can be found from the room after queue acknowledgement.
  - Business Chat Room can mark selected active workflow handoffs reviewed from the room, preserving lifecycle evidence while clearing stale active drafts.
  - active handoff selection in the room renders preserved draft-contract/apply-plan evidence before operators open or mark the handoff reviewed.
  - room-level and wizard-level reviewed actions share a single handoff lifecycle helper, so Slack-origin and app-origin handoffs close with consistent active-draft clearing and workflow event metadata.
  - Kurt intake handoff review cards classify cost hints before apply; generic `cost $X` remains ambiguous until an operator confirms product unit cost, whole-lot landed cost, assignment landed cost, or unknown cost basis.
  - Murdock listing handoff review cards show product/link, title, description, category, condition, price/economics, media, and item-specific readiness before operators open or apply listing work.
  - concise Slack replies such as `kurt answer quantity: 20` are parsed into agent-answer metadata on the queued command; downstream workflows can apply those answers to draft contracts with operator/source evidence while keeping writes approval-gated.
  - approved Business Chat Room handoff saving applies parsed agent/operator answers to the embedded draft contract and recomputes the apply plan before the target wizard opens it.
  - Slack Ops answer-only commands mirror into the room as evidence and do not create products, listings, or other writes by themselves.
  - in-app room answer-only messages mirror the same safety boundary: capture structured answer evidence, acknowledge it in the room, and do not queue/apply writes.
  - when a matching active room-origin handoff exists for the current user, an in-app Kurt/Murdock answer updates that handoff draft contract/apply plan as evidence only; operational records remain unchanged until the target workflow is reviewed/submitted.
  - Slack Ops answer-only commands use the same latest-active-handoff update path when a matching Kurt/Murdock handoff exists for the Slack/app user.
  - operators can target a specific handoff with syntax like `kurt answer handoff 88 quantity: 20` or `murdock answer draft #321 condition id: 3000`.
  - Slack Ops unsupported/denied responses include a compact command reference covering Kurt, Murdock, Goldie/accountant, comps, status, targeted handoff/draft answers, and the answer-only safety boundary.
  - Business Chat Room and destination wizard handoff panels show targeted answer command suggestions beside missing confirmations.
  - Business Chat Room AI draft contract cards also show targeted answer command suggestions when queue/draft metadata is present.
  - targeted answer command blocks list all generated missing-confirmation replies for the selected handoff or draft card.
  - ambiguous Kurt intake costs generate clarification commands for `product_unit_cost`, `lot_landed_total`, and `assignment_landed_cost`.
  - Murdock listing readiness gaps can generate targeted answer commands for `product_id`, `title`, `description_html`, `category_id`, `condition_id`, `suggested_price`, `main_image_id`, and `item_specifics`, even when the AI draft did not ask a specific missing-question row.
  - destination wizard handoff inboxes show captured operator answers with field, answer, source, and actor before review/load.
  - repeated Kurt/Murdock answers are deduplicated in draft contracts and room handoff payloads, so Slack retry delivery or duplicate replies do not create repeated answer/evidence rows.
  - Business Chat Room active handoff details show captured operator answers from both draft-contract and handoff-payload evidence before the operator opens the target workflow.
  - Business Chat Room active handoff details show prompt-hint review fields for handoffs without embedded draft contracts, keeping safe extracted IDs/titles/quantity/cost hints visible before workflow review.
  - Business Chat Room active handoff details show structured attachment evidence, including stored media/document references and persistence errors, so file-backed Slack/app requests can be reviewed before opening the target workflow.
  - Inventory Intake Wizard and Listing Wizard handoff inboxes show the same structured attachment evidence rows when metadata is available, with count-only fallback for older handoffs.
  - Business Chat Room, Inventory Intake Wizard, and Listing Wizard share merged/deduped operator-answer evidence rendering, so answers captured in a draft contract or handoff payload stay visible across review surfaces.
  - Inventory Intake Wizard and Listing Wizard show targeted answer command suggestions even without explicit missing-question rows, so cost-basis clarifiers and Murdock readiness prompts remain visible in destination review.
  - Business Chat Room draft cards, active handoff details, Inventory Intake Wizard, and Listing Wizard request up to eight answer-command suggestions per handoff review, enough to show the full common Murdock readiness cleanup set.
  - Business Chat Room AI draft cards show targeted answer command suggestions even without explicit missing-question rows, so readiness-derived Murdock prompts remain visible in chat history.
  - targeted Murdock answer suggestions use concrete reply examples for category/condition IDs, buyer-facing HTML description format, existing media asset IDs, and JSON-style item specifics, reducing ambiguous Slack/app replies during listing cleanup.
  - JSON-style `item_specifics`/aspect answers and simple `Name=Value; Name=Value` replies are stored as structured draft values and displayed as compact JSON in review evidence; malformed JSON remains captured as the original answer text for operator correction.
  - generated Murdock item-specific answer suggestions prefer `Name=Value; Name=Value` because it is easier to type in Slack; JSON is still accepted for exact structured replies.
  - Kurt/Murdock Slack Ops AI summaries mirror into the room as best-effort messages with queue job, Slack channel/thread, intent, and draft-signature metadata; mirroring failures do not block Slack queue execution.
- Future memory/retrieval note:
  - pgvector may be added later as an app-side semantic retrieval layer for Business Chat Room history, Slack mirrored threads, intake/listing drafts, comps, accounting review evidence, and ops decisions,
  - Slack messages should still mirror to source/audit records first; vector memory should index those source records later rather than becoming the primary record,
  - memory hits must be permission-filtered and cited back to source records before agents use them in replies or proposed actions.
- Business Chat Room agents:
  - `kurt ...` routes to the inventory intake path. Kurt should extract product/lot/source/cost/media fields, ask missing confirmation questions, and keep writes approval-gated.
  - `murdock ...` or `listing ...` routes to the listing-draft path. Murdock should prepare eBay-ready title/description/readiness guidance and keep listing updates or publish actions approval-gated.
  - `goldie ...` keeps routing to the read-only accounting specialist.
  - Scout (research/pricing) and Atlas (business monitoring) are planned specialist roles for comp evidence and business health triage.
  - Atlas and Goldie include customer/repeat-buyer context in their Business Chat Room domain scopes; internal notes remain operator context only.
  - Business Chat Room agent context includes customer rollup counts and top repeat-buyer summaries, but not internal note bodies.
  - Agent prompts receive only bounded customer rollups; internal customer notes remain visible only in operator-facing customer/order surfaces.
  - The Business Chat Room page shows the same safe Customer Context rollup for operators.
  - Prompt board and coordination suggestions use customer rollups for Atlas follow-up prompts and Goldie boundary-review prompts without exposing note bodies.
  - Customer rollups include bounded repeat-buyer and dormant-customer summaries; internal note bodies are still excluded.
  - Customer Context links back to the Customers workspace and provides safe Atlas/Goldie prompt starters for repeat-buyer and dormant-customer triage.
  - Suggested prompts in Customer Context, prompt board, standup, coordination, and Agent Focus can be staged into an editable prepared-prompt box before sending through the normal room chat and approval-gated action path.
  - Targeted answer commands shown on draft cards, active handoffs, workload, and Agent Focus can also be staged into the prepared-prompt box before sending.
  - Sent prepared prompts retain their staging source label in room metadata/history for traceability after operator review.
  - Prepared prompts show pre-send read-only versus write/action approval status so operators know when sending will queue a human-approved workflow request.
  - Prepared prompts show pending attachment previews before send so operators can confirm which evidence files will be attached to the staged message.
  - Sent room attachments render as compact evidence rows in chat history with stored references and persistence errors instead of raw JSON.
  - Attachment evidence rendering is shared between room chat history and active handoff review so operators see the same file evidence columns in both places.
  - Sent prepared prompts retain and display their pre-send status (`read_only` or `approval_required`) in room metadata/history.
  - Approval-required prepared prompt history includes the routed action/workflow beside the status for easier queued-approval audit.
  - Sent prepared prompt history includes the staged prompt label alongside the source panel when a suggestion or answer command was used.
- eBay buyer block management:
  - `Admin -> eBay Buyer Blocks` stores the app-side blocked-buyer mirror, normalizes/dedupes usernames, records audit events, exports/copies the list, shows matched customer/order context, suggests unblocked eBay customer candidates, and links to eBay Buyer Management pages.
  - The tab also includes a Sell Account `user_preferences` API smoke test so operators can verify supported seller-preference API access without confusing it with the manual individual blocked-buyer list.
  - Apply the live marketplace block in eBay Buyer Management, then paste/import the resulting list back into Admin to keep the local mirror aligned.
- Kurt/Murdock AI responses can include a normalized `ai_agent_draft` contract in the queue payload:
  - proposed fields with confidence/evidence/warnings,
  - missing confirmation questions,
  - proposed approval-gated apply actions,
  - Slack thread/source references,
  - an apply plan showing `missing_required_confirmations`, `pending_human_approval`, or `ready`.
- Slack `intake` should evolve into a draft/review/apply workflow:
  - create or update an intake draft from photos, invoices, and short operator notes,
  - reply in-thread with extracted fields, confidence, cost/lot evidence status, and missing questions,
  - accept concise operator answers in the same thread and attach them to the draft audit trail,
  - require human approval before product, lot, cost, or media-link writes are applied.
- Slack listing workflows should support product/listing driven draft assistance:
  - run comps and fee/breakeven estimates,
  - draft eBay-ready title/description/item specifics/category/condition suggestions,
  - summarize image/EPS/video readiness and publish blockers,
  - queue listing draft updates or publish/revise actions behind existing approval gates.
- AI responses used for intake/listing writes must preserve prompt hash, data-scope hash, response hash, applied fields, actor/approver, queue job ID, and Slack thread reference.

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

## Ops Validation Checklist (Quarterly Estimated Tax Planning)

Use this before making quarterly estimated-tax payments or handing a packet to an advisor.

1. Open `Reports -> Quarterly Estimated Tax Planning`.
2. Select the tax year and estimated-payment quarter. The app uses IRS income windows: Q1 Jan-Mar, Q2 Apr-May, Q3 Jun-Aug, and Q4 Sep-Dec.
3. Review federal income-tax, Colorado income-tax, self-employment tax, SE net-earnings multiplier, prior-payment, other-income, and deductible-adjustment inputs.
4. Confirm the worksheet includes actual-economics sales, fee, shipping charged, label spend, FIFO COGS, return adjustments, and local/SUTS tax context.
5. Resolve any missing/review-needed COGS warnings before relying on profit/income estimates.
6. Download `Quarterly Estimated Tax Packet XLSX` and send it to the tax advisor before filing/payment.
7. After payment, record Federal/Colorado payment evidence in the same Reports section with confirmation/reference, evidence link, packet hash, and notes.

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
