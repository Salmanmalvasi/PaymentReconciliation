# Brief: Payment Reconciliation & Exception Management Engine

## Summary
A backend system that automatically matches transactions between an internal ledger and payment processor settlement files, classifies mismatches into meaningful categories, and provides an audit-trailed queue for operational review. Mirrors the reliability and correctness concerns of real payments infrastructure (Stripe/Adyen-style settlement reconciliation).

**Tech stack:** Python, PostgreSQL, Pandas, FastAPI

## Resume Bullet This Project Targets
- Built a reconciliation engine that automatically matches transactions between an internal ledger and payment processor settlement files, mirroring core payments-network reliability concerns.
- Simulated real-world payment operations including settlement delays, partial refunds, duplicate transactions, currency rounding differences, and out-of-order event arrivals.
- Designed a rule-based matching framework classifying records as matched, explainable exceptions, or true discrepancies, with an audit-trailed exception queue for operational review.

## Deliverables
1. Synthetic data generator producing realistic ledger + settlement datasets with configurable anomaly injection.
2. PostgreSQL schema (migrations) for all core tables.
3. Rule-based matching engine (Python + Pandas) — each rule a discrete, documented, unit-tested function.
4. FastAPI service exposing ingestion, reconciliation, exception-queue, and metrics endpoints.
5. README explaining the reconciliation logic, rule tiers, and how to run everything.
6. A `RESULTS.md` summarizing a sample run: match rate, exception breakdown, 2–3 worked examples.

## Non-Functional Requirements
- **Correctness over cleverness** — explicit, auditable rules over black-box matching.
- **Idempotency** — re-running ingestion/reconciliation never double-counts or corrupts state.
- **Testability** — every matching rule unit-testable against small synthetic fixtures.
- **Performance** — vectorized Pandas / set-based SQL, scales to 100k–1M records per run.
- **Config over hardcoding** — tolerances (rounding epsilon, settlement delay window, fee tolerance) live in config, not magic numbers.

## Suggested Data Model
```
ledger_transactions
  id, order_id, customer_id, amount, currency, status, created_at, updated_at

settlement_records
  id, external_transaction_ref, gross_amount, fee_amount, net_amount,
  currency, settled_at, status, source_batch_id

reconciliation_runs
  id, started_at, completed_at, params_snapshot

reconciliation_results
  id, run_id, ledger_id (nullable), settlement_id (nullable),
  classification, matched_rule, confidence_notes, created_at

exceptions
  id, reconciliation_result_id, status, category, opened_at,
  reviewer_notes, resolved_by, resolved_at

exception_audit_log
  id, exception_id, changed_by, old_status, new_status,
  note, changed_at
```

## Stretch Goals (only after core is solid)
- FX-rate-aware multi-currency matching.
- Bulk CSV export of the exception queue.
- API key auth on FastAPI endpoints.
- Dockerize (API + Postgres) with `docker-compose.yml`.
- Nightly scheduled reconciliation job.
