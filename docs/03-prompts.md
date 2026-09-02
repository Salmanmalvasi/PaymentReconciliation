# Prompts: Build Sequence for Antigravity

Use these in order, one per turn/task. Paste `01-brief.md` and `02-context.md` in as attached context for the whole session (or paste their contents into the first prompt) so every step is grounded in the same spec.

---

### Prompt 0 — Session setup
```
Attached are two files: a project brief and domain context for a Payment
Reconciliation & Exception Management Engine (Python, PostgreSQL, Pandas,
FastAPI). Read both fully before doing anything. Confirm you understand
the three-tier classification (matched / explainable exception / true
discrepancy) and the audit-trail requirement before we start building.
Do not write code yet — just summarize the plan back to me in your own
words, broken into build phases.
```

### Prompt 1 — Schema
```
Implement the PostgreSQL schema from the brief's data model section as
migrations (use Alembic if the project uses SQLAlchemy, otherwise plain
.sql migration files — your call, but be consistent). Include appropriate
indexes for the columns reconciliation queries will filter/join on
(order_id, external_transaction_ref, created_at, settled_at, status).
Add a short schema.md explaining each table's purpose.
```

### Prompt 2 — Synthetic data generator
```
Build the synthetic data generator described in the brief. It must produce
paired ledger_transactions and settlement_records datasets with these
anomaly types injectable at configurable rates: settlement delays,
partial refunds, duplicate transactions, currency rounding differences,
out-of-order arrivals, currency mismatches, and missing counterparts.
CLI flags: --num-records, --anomaly-rate, --seed. Output to CSV and to
the Postgres staging tables. Print a summary of how many of each anomaly
type were injected, so we can verify the matching engine later against
known ground truth.
```

### Prompt 3 — Matching engine core
```
Build the rule-based matching engine as a pipeline of small, independently
testable rule functions, run in priority order, each consuming only
records not yet matched by an earlier rule. Implement rules for:
1. Exact match (ref + amount + currency)
2. Fuzzy match (amount within rounding tolerance + within settlement
   delay window)
3. Fee-explained match (gross - fee = net within tolerance)
4. Partial refund match (one-to-many, net position reconciles)
5. Timing-only explainable exception
6. Currency rounding explainable exception
7. True discrepancy (catch-all for anything left unexplained, plus
   explicit duplicate-with-conflicting-amounts and currency-mismatch
   detection)
Each rule needs a docstring stating what it detects and why, and must log
which rule fired into reconciliation_results. Write this using vectorized
Pandas operations, not row-by-row loops.
```

### Prompt 4 — Unit tests for matching rules
```
Write unit tests for every matching rule from Prompt 3, using small
synthetic fixtures — one fixture set per anomaly type from the brief.
Include at least one test that proves the tiering works correctly (i.e.
a record that could theoretically match two rules is classified by the
higher-priority rule, not both). Run the generator from Prompt 2 with a
fixed seed, run the full pipeline, and add an integration test asserting
the classification counts roughly match the known injected anomaly counts.
```

### Prompt 5 — Exception queue & audit trail
```
Implement the exceptions table workflow (open → under_review →
resolved/accepted/escalated) and the append-only exception_audit_log.
Every status change must write a new audit_log row rather than mutating
history. Add a service-layer function for transitioning exception state
that enforces valid transitions and always logs. Write tests confirming
audit history is preserved across multiple state changes and that
re-running reconciliation never deletes prior exception/audit history.
```

### Prompt 6 — FastAPI service
```
Build the FastAPI service exposing:
- POST /ingest/ledger, POST /ingest/settlements
- POST /reconcile/run
- GET /reconciliation/results (filterable by classification, date range;
  paginated)
- GET /exceptions (filterable by status; paginated)
- PATCH /exceptions/{id} (status update + reviewer notes, writes audit log)
- GET /exceptions/{id}/audit-trail
- GET /metrics/summary (match rate, exception breakdown, oldest unresolved
  exception, run-over-run trend)
Use Pydantic models for request/response schemas so the auto-generated
OpenAPI docs are clean and usable. Add basic error handling (404s for
missing IDs, validation errors surfaced clearly).
```

### Prompt 7 — Docs & results
```
Write the README covering: what this project does, the reconciliation
logic and rule tiers in plain language, how to run the generator, how to
run migrations, how to start the API, and example curl calls for each
endpoint. Then run a full end-to-end demo (generate data → ingest → run
reconciliation → hit the API) and write RESULTS.md summarizing: overall
match rate, exception breakdown by category, and 2-3 specific record
examples walked through step by step showing which rule classified them
and why.
```

### Prompt 8 — Optional stretch (only after 0–7 are solid)
```
Pick from the brief's stretch goals based on what's most valuable to
demo: Dockerize the API + Postgres with docker-compose, add simple API
key auth, add a bulk CSV export for the exception queue, or add
FX-rate-aware multi-currency matching. Implement one at a time and tell
me which you recommend doing first and why before starting.
```
