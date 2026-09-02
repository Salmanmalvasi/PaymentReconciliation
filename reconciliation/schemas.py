"""
Pydantic schemas for API request/response models.

These schemas provide clean, typed contracts for the FastAPI endpoints
and generate usable OpenAPI documentation.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class LedgerTransactionCreate(BaseModel):
    order_id: str = Field(..., description="Business order identifier")
    customer_id: str = Field(..., description="Customer identifier")
    amount: float = Field(..., description="Transaction amount")
    currency: str = Field(..., max_length=3, description="ISO 4217 currency code")
    status: str = Field(default="completed", description="Transaction status")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LedgerTransactionResponse(BaseModel):
    id: int
    order_id: str
    customer_id: str
    amount: float
    currency: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

class SettlementRecordCreate(BaseModel):
    external_transaction_ref: str = Field(..., description="Processor's reference ID")
    gross_amount: float = Field(..., description="Customer-paid amount")
    fee_amount: float = Field(default=0.0, description="Processor fee")
    net_amount: float = Field(..., description="Amount settled to business")
    currency: str = Field(..., max_length=3, description="ISO 4217 currency code")
    settled_at: Optional[datetime] = None
    status: str = Field(default="settled")
    source_batch_id: Optional[str] = None


class SettlementRecordResponse(BaseModel):
    id: int
    external_transaction_ref: str
    gross_amount: float
    fee_amount: float
    net_amount: float
    currency: str
    settled_at: datetime
    status: str
    source_batch_id: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationRunResponse(BaseModel):
    id: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_ledger: int
    total_settlement: int
    total_matched: int
    total_exceptions: int
    total_discrepancies: int
    params_snapshot: Optional[dict] = None

    model_config = {"from_attributes": True}


class ReconciliationResultResponse(BaseModel):
    id: int
    run_id: int
    ledger_id: Optional[int] = None
    settlement_id: Optional[int] = None
    classification: str
    matched_rule: str
    confidence_notes: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReconciliationResultsPage(BaseModel):
    results: list[ReconciliationResultResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ExceptionResponse(BaseModel):
    id: int
    reconciliation_result_id: int
    status: str
    category: str
    opened_at: datetime
    reviewer_notes: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ExceptionUpdateRequest(BaseModel):
    status: str = Field(..., description="New status (under_review, resolved, accepted, escalated)")
    changed_by: str = Field(..., description="Who is making this change")
    note: Optional[str] = Field(None, description="Reason for the status change")
    reviewer_notes: Optional[str] = Field(None, description="Notes for the exception record")


class ExceptionListPage(BaseModel):
    exceptions: list[ExceptionResponse]
    total: int
    page: int
    page_size: int


class AuditLogResponse(BaseModel):
    id: int
    exception_id: int
    changed_by: str
    old_status: str
    new_status: str
    note: Optional[str] = None
    changed_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class MetricsSummaryResponse(BaseModel):
    total_runs: int
    latest_run_id: Optional[int] = None
    latest_run_at: Optional[datetime] = None
    match_rate: float = Field(..., description="Percentage of records classified as matched")
    total_matched: int
    total_exceptions: int
    total_discrepancies: int
    exception_breakdown: dict[str, int] = Field(
        default_factory=dict, description="Count by exception category"
    )
    oldest_unresolved: Optional[datetime] = Field(
        None, description="Opened_at of the oldest open exception"
    )
    run_trend: list[dict] = Field(
        default_factory=list,
        description="Last 10 runs with match rate trend",
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class BulkIngestionResponse(BaseModel):
    message: str
    records_ingested: int
