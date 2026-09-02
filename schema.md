# Database Schema

## Overview

The schema consists of six tables that model the full lifecycle of payment reconciliation: from raw data ingestion through matching, exception management, and audit compliance.

## Tables

### `ledger_transactions`
**Purpose:** Internal record of what the business *believes* happened — charges, refunds, adjustments as recorded before settlement data arrives.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `order_id` | VARCHAR(64) | Business order identifier, used as primary join key to settlements |
| `customer_id` | VARCHAR(64) | Customer identifier |
| `amount` | NUMERIC(14,4) | Transaction amount (gross, from the business perspective) |
| `currency` | VARCHAR(3) | ISO 4217 currency code |
| `status` | ENUM | One of: pending, completed, refunded, partially_refunded, failed, cancelled |
| `created_at` | TIMESTAMPTZ | When the transaction was created |
| `updated_at` | TIMESTAMPTZ | Last modification timestamp |

### `settlement_records`
**Purpose:** Payment processor's view of what *actually* moved money. Arrives in batch files with gross/fee/net breakdown.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `external_transaction_ref` | VARCHAR(128) | Processor's reference ID, maps to `order_id` in the ledger |
| `gross_amount` | NUMERIC(14,4) | What the customer paid |
| `fee_amount` | NUMERIC(14,4) | Processor's fee |
| `net_amount` | NUMERIC(14,4) | What settles to the business (`gross - fee`) |
| `currency` | VARCHAR(3) | ISO 4217 currency code |
| `settled_at` | TIMESTAMPTZ | When the settlement occurred |
| `status` | ENUM | One of: settled, pending, reversed, failed |
| `source_batch_id` | VARCHAR(64) | Which batch file this record came from |

### `reconciliation_runs`
**Purpose:** Metadata for each execution of the reconciliation pipeline. Ensures full reproducibility.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `started_at` | TIMESTAMPTZ | When the run began |
| `completed_at` | TIMESTAMPTZ | When the run finished |
| `params_snapshot` | JSONB | Frozen copy of config parameters used |
| `total_ledger` | INTEGER | Count of ledger records processed |
| `total_settlement` | INTEGER | Count of settlement records processed |
| `total_matched` | INTEGER | Records classified as matched |
| `total_exceptions` | INTEGER | Records classified as explainable exceptions |
| `total_discrepancies` | INTEGER | Records classified as true discrepancies |

### `reconciliation_results`
**Purpose:** Per-record outcome of reconciliation. Links ledger and/or settlement rows, records which rule fired, and the three-tier classification.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `run_id` | BIGINT (FK) | Which reconciliation run produced this result |
| `ledger_id` | BIGINT (FK, nullable) | Linked ledger transaction (null if settlement-only orphan) |
| `settlement_id` | BIGINT (FK, nullable) | Linked settlement record (null if ledger-only orphan) |
| `classification` | ENUM | matched, explainable_exception, or true_discrepancy |
| `matched_rule` | VARCHAR(128) | Name of the rule that produced this classification |
| `confidence_notes` | TEXT | Human-readable explanation of why this rule fired |
| `created_at` | TIMESTAMPTZ | When this result was created |

### `exceptions`
**Purpose:** Operational queue for records needing human review. Auto-created for explainable exceptions and true discrepancies.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `reconciliation_result_id` | BIGINT (FK, unique) | Links to the specific reconciliation result |
| `status` | ENUM | open → under_review → resolved/accepted/escalated |
| `category` | ENUM | fee_difference, rounding_difference, timing_delay, etc. |
| `opened_at` | TIMESTAMPTZ | When the exception was created |
| `reviewer_notes` | TEXT | Notes from the reviewer |
| `resolved_by` | VARCHAR(128) | Who resolved the exception |
| `resolved_at` | TIMESTAMPTZ | When the exception was resolved |

### `exception_audit_log`
**Purpose:** Append-only compliance record of every exception state change. Never mutated after insertion.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `exception_id` | BIGINT (FK) | Which exception was changed |
| `changed_by` | VARCHAR(128) | Who made the change |
| `old_status` | ENUM | Status before the change |
| `new_status` | ENUM | Status after the change |
| `note` | TEXT | Reason for the change |
| `changed_at` | TIMESTAMPTZ | When the change occurred |

## Key Indexes

- `ledger_transactions`: Indexed on `order_id`, `(order_id, currency)`, `created_at`, `status`
- `settlement_records`: Indexed on `external_transaction_ref`, `(external_transaction_ref, currency)`, `settled_at`, `status`
- `reconciliation_results`: Indexed on `run_id`, `ledger_id`, `settlement_id`, `classification`
- `exceptions`: Indexed on `status`, `category`
- `exception_audit_log`: Indexed on `exception_id`, `changed_at`
