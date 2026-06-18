# QuickBooks Online Integration Blueprint

GoldenStackers uses a clearing-account architecture for QuickBooks Online so marketplace sales, eBay fees, shipping labels, subscriptions, and real bank deposits can be reconciled without duplicating revenue or posting app-created entries into the live bank ledger.

## Implementation Decision

GoldenStackers will build and own the QuickBooks integration inside this app. Do not use native eBay/QuickBooks e-commerce connectors and do not use bridge tools such as A2X or Link My Books as the production integration path.

Those accounting-first bridge tools are useful as a reference model only: they avoid buyer-profile bloat, summarize marketplace economics cleanly, and reconcile net payouts against underlying gross sales and fees. GoldenStackers should duplicate that clean accounting behavior while preserving our own SKU, lot, COGS, customer, and marketplace evidence.

The fallback route is manual month-end journal entry support from eBay financial summaries and GoldenStackers report exports when API posting is not ready or when the accountant wants summary entries.

## Clearing Account Rule

The app must not post SalesReceipt or Purchase transactions directly into the connected business checking account.

All app-generated QuickBooks transactions post through an asset account named:

`Custom App Clearing Account`

The real bank-feed deposit should later be matched/cleared manually against this account, normally as a transfer or bank-feed match workflow in QuickBooks.

## QuickBooks API Endpoints

Use Intuit QuickBooks Online REST API v3:

- `POST /v3/company/{realmId}/salesreceipt`
- `POST /v3/company/{realmId}/purchase`

OAuth 2.0 connection, realm/company selection, token refresh, account/item/vendor/customer resolution, and live posting should remain behind an explicit sync job and audit trail.

## SalesReceipt Payload

Use one SalesReceipt per successful marketplace order or sale row selected for export.

Required mapping:

- `TxnDate`: sale/order date as `YYYY-MM-DD`
- `CustomerRef`: generic customer `eBay Sales Customer`
- `PaymentMethodRef`: `eBay Payout` or `PayPal`
- `DepositToAccountRef`: `Custom App Clearing Account`
- `Line[].SalesItemLineDetail.ItemRef`: product SKU/code matching the QuickBooks Products and Services list
- `Line[].SalesItemLineDetail.Qty`: inventory units sold
- `Line[].SalesItemLineDetail.UnitPrice`: gross unit price before eBay fees
- `Line[].Amount`: gross line amount before eBay fees
- `Line[].SalesItemLineDetail.TaxCodeRef`: `NON` for eBay marketplace-facilitator orders
- `DocNumber`: deterministic idempotency key from app/order/sale identity

Sales tax for eBay marketplace orders is intentionally not posted as merchant-collected tax because eBay is the marketplace facilitator and remits tax directly. Local/direct sales need their own tax mapping and should not blindly reuse the eBay `NON` rule.

## Purchase Payload For Order Fees

Use one Purchase per order-level eBay fee deduction.

Required mapping:

- `TxnDate`: fee date as `YYYY-MM-DD`
- `AccountRef`: `Custom App Clearing Account`
- `EntityRef`: vendor `eBay Vendor`
- `Line[].AccountBasedExpenseLineDetail.AccountRef`: `Merchant Account Fees` or equivalent
- `Line[].Amount`: eBay final value/processing fee amount for the order
- `DocNumber`: deterministic idempotency key from app/order/sale identity

This reduces the clearing account balance, matching the way eBay subtracts fees before payout.

For tax planning, Reports also exposes these marketplace fees in `Quarterly Estimated Tax Fee Detail` as potential commissions/fees evidence. IRS Schedule C instructions include a line for commissions and fees and note that property dealers may report commissions/fees paid to facilitate sales there, but the app does not make the filing determination. Confirm deductibility, capitalization, entity return mapping, and account mapping with your tax advisor.

## Purchase Payload For Non-Order Payout Deductions

When eBay payout evidence contains deductions not tied to an individual order, create a standalone Purchase:

- Shipping labels: expense category `eBay Shipping Expense`
- Store subscriptions: expense category `eBay Subscription Expense`
- Other adjustments/ad fees: expense category `Bank Charges & Fees` or a more specific configured account
- Payment account: `Custom App Clearing Account`
- Vendor: `eBay Vendor`
- Amount: exact deduction amount from payout evidence

## App Service Foundation

The pure payload builder lives in `app/services/quickbooks.py`:

- `quickbooks_sales_receipt_payload(...)`
- `quickbooks_order_fee_purchase_payload(...)`
- `quickbooks_non_order_fee_purchase_payload(...)`
- `quickbooks_payload_validation_issues(...)`

The builder does not call the QuickBooks API. It produces reviewed JSON payloads for the future live `quickbooks_export` sync job and catches obvious readiness issues such as missing item refs or nonpositive line amounts.

Reports derives three preview/export tables from the existing QuickBooks staging export:

- `QuickBooks SalesReceipt Payloads`
- `QuickBooks Fee Purchase Payloads`
- `QuickBooks Shipping Label Purchase Payloads`
- `QuickBooks Payload Readiness`

The payload tables show the endpoint, source document number, generated QBO `DocNumber`, clearing-account/account refs, validation status, validation issues, and raw `payload_json`. Shipping-label Purchase payloads use the configured `eBay Shipping Expense` account and reduce `Custom App Clearing Account` separately from order-level eBay fee Purchase rows. The readiness table summarizes payload counts, total preview amounts, review-row counts, missing item refs, missing expense account refs, and nonpositive line amounts. They are review artifacts only; no live QuickBooks writes happen from Reports.

Admin > Integrations exposes runtime settings for the clearing account, generic eBay customer/vendor/payment method, eBay fee/shipping/subscription/adjustment expense accounts, eBay marketplace-facilitator tax code, and DocNumber prefix. Reports payload previews resolve those DB-backed settings before generating JSON, so review exports match the configured environment.

## Dry-Run Sync Job

The `quickbooks_export` sync job is implemented as a dry-run evidence producer. It does not call Intuit APIs and it does not create QuickBooks transactions.

When executed, the job:

- Reads repository sales actual-economics rows for the configured lookback window.
- Generates clearing-account payload previews for SalesReceipt, order-fee Purchase, and shipping-label Purchase entries.
- Records sync-run summary evidence with payload counts, action counts, validation-review counts, and a SHA-256 evidence hash.
- Records a bounded payload manifest in sync events using deterministic QBO `DocNumber` values and payload hashes.
- Marks the run `partial` when any payload needs mapping review, such as a missing SKU/item ref.
- Blocks `live_post=true` with a sync error until OAuth, QuickBooks ID resolution, idempotency state, sandbox validation, and production approval are implemented.

Admin runtime settings include `sync_job_quickbooks_export_enabled` and `sync_job_quickbooks_export_lookback_days` so every environment has default dry-run controls without requiring new environment variables.

The dedicated QuickBooks page owns all operator-facing QuickBooks controls: clearing-account refs, sales/shipping income refs, generic customer/vendor refs, expense account refs, tax code, DocNumber prefix, dry-run job enablement, dry-run lookback, recent QuickBooks sync runs, and manual journal fallback worksheets. Admin keeps raw runtime setting management and seed defaults only.

## Manual Journal Fallback

If API posting is not ready, use a monthly manual journal route:

- Open the QuickBooks page and select the target month.
- Review/download the generated balanced manual journal worksheet.
- Use GoldenStackers Reports/Taxes exports as supporting detail for gross sales, buyer-paid shipping, eBay fees, shipping-label spend, subscriptions, refunds, and COGS.
- Debit the clearing account for gross item sales plus buyer-paid shipping.
- Credit the configured marketplace sales and shipping income accounts.
- Debit eBay fees and shipping-label spend to the configured expense accounts.
- Credit the clearing account for marketplace deductions and reconcile net payout deposits through the clearing account or accountant-approved bank-feed workflow.
- Do not create one QuickBooks customer per eBay buyer.
- Confirm final entity, tax, and account mapping with the accountant before filing or close sign-off.

## Required Live Sync Guardrails

Before enabling direct posting:

- Resolve configured names to QuickBooks IDs for customer, vendor, payment method, clearing account, expense accounts, and item refs.
- Store OAuth tokens securely and refresh them through Intuit OAuth 2.0.
- Use deterministic `DocNumber` values and/or sync-state records to prevent duplicate pushes.
- Log each attempted payload, endpoint, result, QuickBooks entity ID, and error in sync runs/events/errors.
- Block or queue failures when SKU/item, account, customer, vendor, or payment method mappings are missing.
- Keep live posting disabled by default until a human signs off on a sandbox and production validation run.
