# Context: Payments Reconciliation Domain

Background knowledge for the build agent so generated logic reflects real payments-ops behavior rather than generic CRUD assumptions.

## Why Reconciliation Exists
An internal ledger records what a business *believes* happened (an order was charged, a refund was issued). A payment processor's settlement file records what *actually* moved money, after fees, holds, retries, and timing lags. These two views of "the same" event diverge constantly. Reconciliation is the process of proving they agree, or explaining precisely why they don't, without a human having to eyeball every row.

## Key Domain Concepts

**Gross vs. net amount**
Settlement files typically report `gross_amount` (what the customer paid), `fee_amount` (the processor's cut), and `net_amount` (what actually settles to the business). `gross - fee = net`, usually within a rounding tolerance. A ledger entry showing the gross amount will legitimately *not* equal the settled net amount — this is expected, not a discrepancy.

**Settlement delay**
Money doesn't settle instantly. A charge created on day N might not appear in a settlement batch until day N+1 through N+5, depending on the processor and payment method (cards settle faster than bank transfers, for instance). A missing settlement record within the expected window is normal; missing after the window has passed is worth flagging.

**Partial refunds**
A single original charge can be followed by one or more partial refund events, each producing its own settlement entry. The ledger's net position (original − sum of refunds) should reconcile against the sum of the corresponding settlement entries. This is a one-to-many matching problem, not one-to-one.

**Duplicate transactions**
Retries (network timeouts, double-clicks, idempotency-key bugs) can cause the same logical transaction to appear twice in either system. Duplicates with *matching* amounts are usually safe to auto-collapse; duplicates with *conflicting* amounts are a true discrepancy and need a human.

**Currency rounding**
Multi-currency processors compute fees and conversions with rounding rules that don't always net out to the sub-cent. A one-or-two-minor-unit difference (e.g., ₹0.01, $0.01) is expected noise, not a real problem — but only up to a defined epsilon.

**Out-of-order arrival**
Settlement files can arrive before the ledger has marked its own transaction "final" (e.g., an async webhook lands late), or a batch can contain events out of chronological order relative to when they were created. Matching logic shouldn't assume strict temporal ordering between the two sources.

## Why "Explainable Exception" Is a Distinct Category From "True Discrepancy"
This is the core design idea of the project, and the part that should get the most engineering attention:

- **Matched** = no human ever needs to look at this.
- **Explainable exception** = doesn't match exactly, but a known business rule fully accounts for the gap (fee deduction, partial refund, timing lag, rounding). Still worth logging for audit purposes, but doesn't need urgent review.
- **True discrepancy** = no known rule explains the gap. This is where real money could be wrong, and it's the only category that should generate operational urgency.

A reconciliation engine that dumps everything into one undifferentiated "exceptions" bucket forces a human to re-derive the "oh, that's just the fee" reasoning by hand every time — the entire point of the rule-based framework is to make that reasoning automatic and auditable.

## Why the Audit Trail Matters
In real payments operations, "why was this exception marked resolved, and who did it, and when" is a compliance question, not a nice-to-have. Every state change on an exception record should be append-only history, never an in-place overwrite — this is what "audit-trailed exception queue" in the brief is pointing at. Re-running reconciliation should also never silently erase the history of prior runs; each run is its own timestamped, queryable event.

## Realistic Scale & Tone
This should read like a small but real internal payments-ops tool, not a toy demo:
- Assume tens of thousands to low millions of records per reconciliation run.
- Assume the matching logic will be run repeatedly (daily/nightly), not once.
- Assume a human operator (finance/ops analyst) is the end user of the exception queue, not a developer — API responses and statuses should be legible to that audience.
