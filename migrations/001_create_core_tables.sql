-- Migration 001: Create core tables for Payment Reconciliation Engine
-- This migration creates all six core tables in dependency order.

BEGIN;

-- ---------------------------------------------------------------------------
-- Enum types
-- ---------------------------------------------------------------------------
CREATE TYPE ledger_status AS ENUM (
    'pending', 'completed', 'refunded', 'partially_refunded', 'failed', 'cancelled'
);

CREATE TYPE settlement_status AS ENUM (
    'settled', 'pending', 'reversed', 'failed'
);

CREATE TYPE classification AS ENUM (
    'matched', 'explainable_exception', 'true_discrepancy'
);

CREATE TYPE exception_status AS ENUM (
    'open', 'under_review', 'resolved', 'accepted', 'escalated'
);

CREATE TYPE exception_category AS ENUM (
    'fee_difference', 'rounding_difference', 'timing_delay', 'partial_refund',
    'duplicate_conflicting', 'currency_mismatch', 'missing_settlement',
    'missing_ledger', 'amount_mismatch', 'unknown'
);

-- ---------------------------------------------------------------------------
-- Table: ledger_transactions
-- Internal record of charges/refunds as the business sees them.
-- ---------------------------------------------------------------------------
CREATE TABLE ledger_transactions (
    id              BIGSERIAL       PRIMARY KEY,
    order_id        VARCHAR(64)     NOT NULL,
    customer_id     VARCHAR(64)     NOT NULL,
    amount          NUMERIC(14, 4)  NOT NULL,
    currency        VARCHAR(3)      NOT NULL,
    status          ledger_status   NOT NULL DEFAULT 'completed',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_ledger_order_id ON ledger_transactions (order_id);
CREATE INDEX ix_ledger_order_currency ON ledger_transactions (order_id, currency);
CREATE INDEX ix_ledger_created_at ON ledger_transactions (created_at);
CREATE INDEX ix_ledger_status ON ledger_transactions (status);

-- ---------------------------------------------------------------------------
-- Table: settlement_records
-- Payment processor's settlement data — what actually moved money.
-- ---------------------------------------------------------------------------
CREATE TABLE settlement_records (
    id                          BIGSERIAL           PRIMARY KEY,
    external_transaction_ref    VARCHAR(128)        NOT NULL,
    gross_amount                NUMERIC(14, 4)      NOT NULL,
    fee_amount                  NUMERIC(14, 4)      NOT NULL DEFAULT 0,
    net_amount                  NUMERIC(14, 4)      NOT NULL,
    currency                    VARCHAR(3)          NOT NULL,
    settled_at                  TIMESTAMPTZ         NOT NULL,
    status                      settlement_status   NOT NULL DEFAULT 'settled',
    source_batch_id             VARCHAR(64)
);

CREATE INDEX ix_settlement_ref ON settlement_records (external_transaction_ref);
CREATE INDEX ix_settlement_ref_currency ON settlement_records (external_transaction_ref, currency);
CREATE INDEX ix_settlement_settled_at ON settlement_records (settled_at);
CREATE INDEX ix_settlement_status ON settlement_records (status);

-- ---------------------------------------------------------------------------
-- Table: reconciliation_runs
-- Metadata for each pipeline execution.
-- ---------------------------------------------------------------------------
CREATE TABLE reconciliation_runs (
    id                  BIGSERIAL       PRIMARY KEY,
    started_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    params_snapshot     JSONB,
    total_ledger        INTEGER         DEFAULT 0,
    total_settlement    INTEGER         DEFAULT 0,
    total_matched       INTEGER         DEFAULT 0,
    total_exceptions    INTEGER         DEFAULT 0,
    total_discrepancies INTEGER         DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Table: reconciliation_results
-- Per-record matching outcome with rule attribution.
-- ---------------------------------------------------------------------------
CREATE TABLE reconciliation_results (
    id                  BIGSERIAL       PRIMARY KEY,
    run_id              BIGINT          NOT NULL REFERENCES reconciliation_runs(id),
    ledger_id           BIGINT          REFERENCES ledger_transactions(id),
    settlement_id       BIGINT          REFERENCES settlement_records(id),
    classification      classification  NOT NULL,
    matched_rule        VARCHAR(128)    NOT NULL,
    confidence_notes    TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_results_run_id ON reconciliation_results (run_id);
CREATE INDEX ix_results_ledger_id ON reconciliation_results (ledger_id);
CREATE INDEX ix_results_settlement_id ON reconciliation_results (settlement_id);
CREATE INDEX ix_results_classification ON reconciliation_results (classification);

-- ---------------------------------------------------------------------------
-- Table: exceptions
-- Operational queue for records needing human review.
-- ---------------------------------------------------------------------------
CREATE TABLE exceptions (
    id                          BIGSERIAL           PRIMARY KEY,
    reconciliation_result_id    BIGINT              NOT NULL UNIQUE
                                                    REFERENCES reconciliation_results(id),
    status                      exception_status    NOT NULL DEFAULT 'open',
    category                    exception_category  NOT NULL DEFAULT 'unknown',
    opened_at                   TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    reviewer_notes              TEXT,
    resolved_by                 VARCHAR(128),
    resolved_at                 TIMESTAMPTZ
);

CREATE INDEX ix_exceptions_status ON exceptions (status);
CREATE INDEX ix_exceptions_category ON exceptions (category);

-- ---------------------------------------------------------------------------
-- Table: exception_audit_log
-- Append-only history of exception state changes (compliance record).
-- ---------------------------------------------------------------------------
CREATE TABLE exception_audit_log (
    id              BIGSERIAL           PRIMARY KEY,
    exception_id    BIGINT              NOT NULL REFERENCES exceptions(id),
    changed_by      VARCHAR(128)        NOT NULL,
    old_status      exception_status    NOT NULL,
    new_status      exception_status    NOT NULL,
    note            TEXT,
    changed_at      TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_audit_exception_id ON exception_audit_log (exception_id);
CREATE INDEX ix_audit_changed_at ON exception_audit_log (changed_at);

COMMIT;
