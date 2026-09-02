-- Migration 002: Rollback core tables
-- Drops all tables and enum types in reverse dependency order.

BEGIN;

DROP TABLE IF EXISTS exception_audit_log CASCADE;
DROP TABLE IF EXISTS exceptions CASCADE;
DROP TABLE IF EXISTS reconciliation_results CASCADE;
DROP TABLE IF EXISTS reconciliation_runs CASCADE;
DROP TABLE IF EXISTS settlement_records CASCADE;
DROP TABLE IF EXISTS ledger_transactions CASCADE;

DROP TYPE IF EXISTS exception_category;
DROP TYPE IF EXISTS exception_status;
DROP TYPE IF EXISTS classification;
DROP TYPE IF EXISTS settlement_status;
DROP TYPE IF EXISTS ledger_status;

COMMIT;
