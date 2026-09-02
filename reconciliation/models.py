"""
SQLAlchemy database models for the Payment Reconciliation Engine.

Tables:
  - ledger_transactions: Internal record of what the business believes happened.
  - settlement_records: Payment processor's view of what actually moved money.
  - reconciliation_runs: Timestamped snapshot of each reconciliation execution.
  - reconciliation_results: Per-record matching outcome with rule attribution.
  - exceptions: Operational queue for records requiring human review.
  - exception_audit_log: Append-only history of every exception state change.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Numeric,
    DateTime,
    Enum,
    Text,
    ForeignKey,
    Index,
    JSON,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Declarative base for all models."""
    pass


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class LedgerStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    refunded = "refunded"
    partially_refunded = "partially_refunded"
    failed = "failed"
    cancelled = "cancelled"


class SettlementStatus(str, enum.Enum):
    settled = "settled"
    pending = "pending"
    reversed = "reversed"
    failed = "failed"


class Classification(str, enum.Enum):
    matched = "matched"
    explainable_exception = "explainable_exception"
    true_discrepancy = "true_discrepancy"


class ExceptionStatus(str, enum.Enum):
    open = "open"
    under_review = "under_review"
    resolved = "resolved"
    accepted = "accepted"
    escalated = "escalated"


class ExceptionCategory(str, enum.Enum):
    fee_difference = "fee_difference"
    rounding_difference = "rounding_difference"
    timing_delay = "timing_delay"
    partial_refund = "partial_refund"
    duplicate_conflicting = "duplicate_conflicting"
    currency_mismatch = "currency_mismatch"
    missing_settlement = "missing_settlement"
    missing_ledger = "missing_ledger"
    amount_mismatch = "amount_mismatch"
    unknown = "unknown"


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------

class LedgerTransaction(Base):
    """
    Internal ledger: what the business believes happened.

    Each row is a charge, refund, or adjustment as recorded in the company's
    own systems before settlement data arrives.
    """
    __tablename__ = "ledger_transactions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    order_id = Column(String(64), nullable=False, index=True)
    customer_id = Column(String(64), nullable=False)
    amount = Column(Numeric(14, 4), nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(Enum(LedgerStatus), nullable=False, default=LedgerStatus.completed)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    # back-references
    reconciliation_results = relationship("ReconciliationResult", back_populates="ledger_transaction")

    __table_args__ = (
        Index("ix_ledger_order_currency", "order_id", "currency"),
        Index("ix_ledger_created_at", "created_at"),
        Index("ix_ledger_status", "status"),
    )


class SettlementRecord(Base):
    """
    Processor settlement file: what actually moved money.

    Rows arrive in batch files from Stripe/Adyen-style processors, potentially
    days after the original charge, with gross/fee/net breakdown.
    """
    __tablename__ = "settlement_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    external_transaction_ref = Column(String(128), nullable=False, index=True)
    gross_amount = Column(Numeric(14, 4), nullable=False)
    fee_amount = Column(Numeric(14, 4), nullable=False, default=0)
    net_amount = Column(Numeric(14, 4), nullable=False)
    currency = Column(String(3), nullable=False)
    settled_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(Enum(SettlementStatus), nullable=False, default=SettlementStatus.settled)
    source_batch_id = Column(String(64), nullable=True)

    # back-references
    reconciliation_results = relationship("ReconciliationResult", back_populates="settlement_record")

    __table_args__ = (
        Index("ix_settlement_ref_currency", "external_transaction_ref", "currency"),
        Index("ix_settlement_settled_at", "settled_at"),
        Index("ix_settlement_status", "status"),
    )


class ReconciliationRun(Base):
    """
    One execution of the reconciliation pipeline.

    Stores start/end timestamps and a JSON snapshot of the config parameters
    used, ensuring full reproducibility and auditability.
    """
    __tablename__ = "reconciliation_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    params_snapshot = Column(JSON, nullable=True)
    total_ledger = Column(Integer, default=0)
    total_settlement = Column(Integer, default=0)
    total_matched = Column(Integer, default=0)
    total_exceptions = Column(Integer, default=0)
    total_discrepancies = Column(Integer, default=0)

    results = relationship("ReconciliationResult", back_populates="run")


class ReconciliationResult(Base):
    """
    Per-record outcome of reconciliation.

    Links a ledger row and/or settlement row, records which rule fired,
    and captures the three-tier classification.
    """
    __tablename__ = "reconciliation_results"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(BigInteger, ForeignKey("reconciliation_runs.id"), nullable=False, index=True)
    ledger_id = Column(BigInteger, ForeignKey("ledger_transactions.id"), nullable=True, index=True)
    settlement_id = Column(BigInteger, ForeignKey("settlement_records.id"), nullable=True, index=True)
    classification = Column(Enum(Classification), nullable=False)
    matched_rule = Column(String(128), nullable=False)
    confidence_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    run = relationship("ReconciliationRun", back_populates="results")
    ledger_transaction = relationship("LedgerTransaction", back_populates="reconciliation_results")
    settlement_record = relationship("SettlementRecord", back_populates="reconciliation_results")
    exception = relationship("Exception", back_populates="reconciliation_result", uselist=False)


class Exception(Base):
    """
    Operational exception queue entry.

    Created automatically for explainable exceptions and true discrepancies.
    Status transitions are enforced by the service layer and every change
    is recorded in the audit log.
    """
    __tablename__ = "exceptions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    reconciliation_result_id = Column(
        BigInteger, ForeignKey("reconciliation_results.id"), nullable=False, unique=True
    )
    status = Column(Enum(ExceptionStatus), nullable=False, default=ExceptionStatus.open)
    category = Column(Enum(ExceptionCategory), nullable=False, default=ExceptionCategory.unknown)
    opened_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reviewer_notes = Column(Text, nullable=True)
    resolved_by = Column(String(128), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    reconciliation_result = relationship("ReconciliationResult", back_populates="exception")
    audit_logs = relationship("ExceptionAuditLog", back_populates="exception", order_by="ExceptionAuditLog.changed_at")

    __table_args__ = (
        Index("ix_exceptions_status", "status"),
        Index("ix_exceptions_category", "category"),
    )


class ExceptionAuditLog(Base):
    """
    Append-only audit trail for exception state changes.

    Never mutated after insertion — this is the compliance record for
    who changed what, when, and why.
    """
    __tablename__ = "exception_audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exception_id = Column(BigInteger, ForeignKey("exceptions.id"), nullable=False, index=True)
    changed_by = Column(String(128), nullable=False)
    old_status = Column(Enum(ExceptionStatus), nullable=False)
    new_status = Column(Enum(ExceptionStatus), nullable=False)
    note = Column(Text, nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    exception = relationship("Exception", back_populates="audit_logs")
