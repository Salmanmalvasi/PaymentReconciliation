"""
FastAPI service for the Payment Reconciliation Engine.

Endpoints:
    POST   /ingest/ledger              — Bulk ingest ledger transactions
    POST   /ingest/settlements         — Bulk ingest settlement records
    POST   /reconcile/run              — Trigger a reconciliation run
    GET    /reconciliation/results     — Query results (filterable, paginated)
    GET    /exceptions                 — List exception queue (filterable, paginated)
    PATCH  /exceptions/{id}            — Update exception status + notes
    GET    /exceptions/{id}/audit-trail — Full audit history
    GET    /metrics/summary            — Dashboard metrics
"""

import csv
import io
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic

import pandas as pd
from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func

from reconciliation.config import settings
from reconciliation.database import get_db, engine
from reconciliation.models import (
    Base,
    LedgerTransaction,
    SettlementRecord,
    ReconciliationRun,
    ReconciliationResult,
    Exception as ExceptionModel,
    ExceptionAuditLog,
    Classification,
    ExceptionStatus,
    ExceptionCategory,
    LedgerStatus,
    SettlementStatus,
)
from reconciliation.schemas import (
    LedgerTransactionCreate,
    SettlementRecordCreate,
    LedgerTransactionResponse,
    SettlementRecordResponse,
    ReconciliationRunResponse,
    ReconciliationResultResponse,
    ReconciliationResultsPage,
    ExceptionResponse,
    ExceptionUpdateRequest,
    ExceptionListPage,
    AuditLogResponse,
    MetricsSummaryResponse,
    BulkIngestionResponse,
    ExceptionExplainResponse,
    AISuggestion,
)
from reconciliation.engine import run_reconciliation, MatchResult
from reconciliation.exceptions import (
    create_exception,
    transition_exception,
    get_audit_trail,
    get_exception,
    list_exceptions,
    InvalidTransitionError,
    ExceptionNotFoundError,
)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=(
        "Payment Reconciliation & Exception Management Engine. "
        "Automatically matches transactions between an internal ledger and "
        "payment processor settlement files, classifies mismatches, and "
        "provides an audit-trailed exception queue for operational review."
    ),
)


@app.on_event("startup")
def startup():
    """Create tables on startup (for dev/demo — use migrations in production)."""
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Ingestion endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/ingest/ledger",
    response_model=BulkIngestionResponse,
    tags=["Ingestion"],
    summary="Ingest ledger transactions from CSV",
)
async def ingest_ledger(
    file: UploadFile = File(..., description="CSV file with ledger transactions"),
    db: Session = Depends(get_db),
):
    """
    Bulk ingest ledger transactions from an uploaded CSV file.

    Expected columns: order_id, customer_id, amount, currency, status, created_at, updated_at
    Idempotent: re-ingesting the same file will skip duplicate order_ids.
    """
    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    count = 0
    for row in reader:
        # Skip if order_id already exists (idempotency)
        exists = db.query(LedgerTransaction).filter(
            LedgerTransaction.order_id == row["order_id"]
        ).first()
        if exists:
            continue

        txn = LedgerTransaction(
            order_id=row["order_id"],
            customer_id=row["customer_id"],
            amount=float(row["amount"]),
            currency=row["currency"],
            status=LedgerStatus(row.get("status", "completed")),
            created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row.get("updated_at") else datetime.now(timezone.utc),
        )
        db.add(txn)
        count += 1

    db.commit()
    return BulkIngestionResponse(
        message=f"Successfully ingested {count} ledger transactions",
        records_ingested=count,
    )


@app.post(
    "/ingest/settlements",
    response_model=BulkIngestionResponse,
    tags=["Ingestion"],
    summary="Ingest settlement records from CSV",
)
async def ingest_settlements(
    file: UploadFile = File(..., description="CSV file with settlement records"),
    db: Session = Depends(get_db),
):
    """
    Bulk ingest settlement records from an uploaded CSV file.

    Expected columns: external_transaction_ref, gross_amount, fee_amount,
                      net_amount, currency, settled_at, status, source_batch_id
    """
    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    count = 0
    for row in reader:
        record = SettlementRecord(
            external_transaction_ref=row["external_transaction_ref"],
            gross_amount=float(row["gross_amount"]),
            fee_amount=float(row.get("fee_amount", 0)),
            net_amount=float(row["net_amount"]),
            currency=row["currency"],
            settled_at=datetime.fromisoformat(row["settled_at"]) if row.get("settled_at") else datetime.now(timezone.utc),
            status=SettlementStatus(row.get("status", "settled")),
            source_batch_id=row.get("source_batch_id"),
        )
        db.add(record)
        count += 1

    db.commit()
    return BulkIngestionResponse(
        message=f"Successfully ingested {count} settlement records",
        records_ingested=count,
    )


# ---------------------------------------------------------------------------
# Reconciliation endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/reconcile/run",
    response_model=ReconciliationRunResponse,
    tags=["Reconciliation"],
    summary="Trigger a reconciliation run",
)
def reconcile_run(db: Session = Depends(get_db)):
    """
    Execute a full reconciliation run.

    Loads all unreconciled ledger and settlement records, runs the matching
    engine, persists results, and creates exception queue entries.
    Idempotent: re-running creates a new run without corrupting prior history.
    """
    # Create run record
    run = ReconciliationRun(
        started_at=datetime.now(timezone.utc),
        params_snapshot={
            "rounding_tolerance": settings.rounding_tolerance,
            "fee_tolerance": settings.fee_tolerance,
            "settlement_delay_days": settings.settlement_delay_days,
        },
    )
    db.add(run)
    db.flush()

    # Load data from database
    ledger_rows = db.query(LedgerTransaction).all()
    settlement_rows = db.query(SettlementRecord).all()

    # Convert to DataFrames
    ledger_data = []
    for r in ledger_rows:
        ledger_data.append({
            "db_id": r.id,
            "order_id": r.order_id,
            "customer_id": r.customer_id,
            "amount": float(r.amount),
            "currency": r.currency,
            "status": r.status.value if r.status else "completed",
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        })

    settlement_data = []
    for r in settlement_rows:
        settlement_data.append({
            "db_id": r.id,
            "external_transaction_ref": r.external_transaction_ref,
            "gross_amount": float(r.gross_amount),
            "fee_amount": float(r.fee_amount),
            "net_amount": float(r.net_amount),
            "currency": r.currency,
            "settled_at": r.settled_at,
            "status": r.status.value if r.status else "settled",
            "source_batch_id": r.source_batch_id,
        })

    if not ledger_data and not settlement_data:
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return run

    ledger_df = pd.DataFrame(ledger_data) if ledger_data else pd.DataFrame(
        columns=["db_id", "order_id", "customer_id", "amount", "currency",
                 "status", "created_at", "updated_at"]
    )
    settlement_df = pd.DataFrame(settlement_data) if settlement_data else pd.DataFrame(
        columns=["db_id", "external_transaction_ref", "gross_amount", "fee_amount",
                 "net_amount", "currency", "settled_at", "status", "source_batch_id"]
    )

    # Run engine
    output = run_reconciliation(ledger_df, settlement_df)

    # Persist results
    matched_count = 0
    exception_count = 0
    discrepancy_count = 0

    for result in output.results:
        ledger_db_id = None
        settlement_db_id = None

        if result.ledger_idx is not None and result.ledger_idx < len(ledger_df):
            ledger_db_id = int(ledger_df.iloc[result.ledger_idx]["db_id"])
        if result.settlement_idx is not None and result.settlement_idx < len(settlement_df):
            settlement_db_id = int(settlement_df.iloc[result.settlement_idx]["db_id"])

        recon_result = ReconciliationResult(
            run_id=run.id,
            ledger_id=ledger_db_id,
            settlement_id=settlement_db_id,
            classification=Classification(result.classification),
            matched_rule=result.matched_rule,
            confidence_notes=result.confidence_notes,
        )
        db.add(recon_result)
        db.flush()

        if result.classification == "matched":
            matched_count += 1
        elif result.classification == "explainable_exception":
            exception_count += 1
            # Create exception queue entry
            cat = ExceptionCategory.unknown
            try:
                cat = ExceptionCategory(result.category)
            except ValueError:
                pass
            create_exception(db, recon_result.id, cat, Classification(result.classification))
        elif result.classification == "true_discrepancy":
            discrepancy_count += 1
            cat = ExceptionCategory.unknown
            try:
                cat = ExceptionCategory(result.category)
            except ValueError:
                pass
            create_exception(db, recon_result.id, cat, Classification(result.classification))

    # Update run summary
    run.completed_at = datetime.now(timezone.utc)
    run.total_ledger = len(ledger_df)
    run.total_settlement = len(settlement_df)
    run.total_matched = matched_count
    run.total_exceptions = exception_count
    run.total_discrepancies = discrepancy_count

    db.commit()
    db.refresh(run)
    return run


@app.get(
    "/reconciliation/results",
    response_model=ReconciliationResultsPage,
    tags=["Reconciliation"],
    summary="Query reconciliation results",
)
def get_results(
    classification: Optional[str] = Query(None, description="Filter by classification"),
    run_id: Optional[int] = Query(None, description="Filter by run ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_db),
):
    """Get reconciliation results with optional filtering and pagination."""
    query = db.query(ReconciliationResult)

    if classification:
        query = query.filter(ReconciliationResult.classification == classification)
    if run_id:
        query = query.filter(ReconciliationResult.run_id == run_id)

    total = query.count()
    results = (
        query
        .order_by(ReconciliationResult.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return ReconciliationResultsPage(
        results=[ReconciliationResultResponse.model_validate(r) for r in results],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# Exception endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/exceptions",
    response_model=ExceptionListPage,
    tags=["Exceptions"],
    summary="List exception queue",
)
def get_exceptions(
    status: Optional[str] = Query(None, description="Filter by status"),
    category: Optional[str] = Query(None, description="Filter by category"),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_db),
):
    """List exceptions with optional status/category filtering and pagination."""
    status_enum = ExceptionStatus(status) if status else None
    category_enum = ExceptionCategory(category) if category else None

    exceptions, total = list_exceptions(
        db, status=status_enum, category=category_enum,
        page=page, page_size=page_size,
    )

    return ExceptionListPage(
        exceptions=[ExceptionResponse.model_validate(e) for e in exceptions],
        total=total,
        page=page,
        page_size=page_size,
    )


@app.patch(
    "/exceptions/{exception_id}",
    response_model=ExceptionResponse,
    tags=["Exceptions"],
    summary="Update exception status",
)
def update_exception(
    exception_id: int,
    update: ExceptionUpdateRequest,
    db: Session = Depends(get_db),
):
    """
    Update an exception's status and/or reviewer notes.

    Enforces valid state transitions and writes an audit log entry.
    Valid transitions:
        open → under_review, resolved, accepted, escalated
        under_review → resolved, accepted, escalated, open
        escalated → under_review, resolved, accepted
        resolved → (terminal)
        accepted → (terminal)
    """
    try:
        new_status = ExceptionStatus(update.status)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: {update.status}. Valid values: {[s.value for s in ExceptionStatus]}",
        )

    try:
        exc = transition_exception(
            db=db,
            exception_id=exception_id,
            new_status=new_status,
            changed_by=update.changed_by,
            note=update.note,
            reviewer_notes=update.reviewer_notes,
        )
        db.commit()
        db.refresh(exc)
        return ExceptionResponse.model_validate(exc)
    except ExceptionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get(
    "/exceptions/{exception_id}/audit-trail",
    response_model=list[AuditLogResponse],
    tags=["Exceptions"],
    summary="Get exception audit trail",
)
def get_exception_audit_trail(
    exception_id: int,
    db: Session = Depends(get_db),
):
    """Get the complete, chronological audit trail for an exception."""
    try:
        trail = get_audit_trail(db, exception_id)
        return [AuditLogResponse.model_validate(log) for log in trail]
    except ExceptionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")


@app.get(
    "/exceptions/{exception_id}/explain",
    response_model=ExceptionExplainResponse,
    tags=["Exceptions"],
    summary="AI-assisted exception explanation",
)
def explain_exception(
    exception_id: int,
    db: Session = Depends(get_db),
):
    """
    Get an AI-generated explanation for an exception.
    
    This endpoint calls the Anthropic API to generate a plain-English hypothesis 
    and recommended action for the human reviewer. It is strictly read-only and 
    advisory, completely independent of the classification engine.
    """
    try:
        exc = get_exception(db, exception_id)
    except ExceptionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")

    # Fetch context
    recon_result = db.query(ReconciliationResult).filter(ReconciliationResult.id == exc.reconciliation_result_id).first()
    
    ledger_txn = None
    if recon_result.ledger_id:
        ledger_txn = db.query(LedgerTransaction).filter(LedgerTransaction.id == recon_result.ledger_id).first()
        
    settlement_rec = None
    if recon_result.settlement_id:
        settlement_rec = db.query(SettlementRecord).filter(SettlementRecord.id == recon_result.settlement_id).first()
        
    audit_trail = get_audit_trail(db, exception_id)
    
    # Prepare prompt context
    ledger_str = "None"
    if ledger_txn:
        ledger_str = f"Order ID: {ledger_txn.order_id}, Amount: {ledger_txn.amount} {ledger_txn.currency}, Status: {ledger_txn.status.value}"
        
    settlement_str = "None"
    if settlement_rec:
        settlement_str = f"Ref: {settlement_rec.external_transaction_ref}, Gross: {settlement_rec.gross_amount} {settlement_rec.currency}, Net: {settlement_rec.net_amount}, Status: {settlement_rec.status.value}"
        
    audit_str = str([f"{a.old_status.value} -> {a.new_status.value} ({a.note or 'no note'})" for a in audit_trail])

    context = f"""
    Exception ID: {exc.id}
    Category: {exc.category.value}
    Status: {exc.status.value}
    Rule Fired: {recon_result.matched_rule}
    Confidence Notes: {recon_result.confidence_notes}
    
    Ledger Transaction: {ledger_str}
    Settlement Record: {settlement_str}
    Audit Trail: {audit_str}
    """
    
    prompt = f"""
    You are a financial operations assistant. Please review the following payment reconciliation exception:
    
    {context}
    
    Provide exactly two things:
    1. A one-sentence plain-English hypothesis for why this record didn't cleanly match, aimed at a finance/ops reviewer, not a developer.
    2. A recommended next action (e.g., "likely safe to accept as fee rounding" or "recommend escalate — no rule explains this gap").
    
    Format your response exactly as:
    Hypothesis: <your hypothesis>
    Action: <your action>
    """
    
    ai_suggestion = None
    error_note = None
    
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
            
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        
        # Parse the expected format
        hypothesis_part = text
        action_part = "Review required."
        
        if "Action:" in text:
            parts = text.split("Action:", 1)
            hypothesis_part = parts[0].replace("Hypothesis:", "").strip()
            action_part = parts[1].strip()
        elif "Hypothesis:" in text:
            hypothesis_part = text.replace("Hypothesis:", "").strip()
            
        ai_suggestion = AISuggestion(
            hypothesis=hypothesis_part,
            recommended_action=action_part
        )
    except Exception as e:
        error_note = f"Failed to generate AI suggestion: {str(e)}"
        
    return ExceptionExplainResponse(
        exception=ExceptionResponse.model_validate(exc),
        ai_suggestion=ai_suggestion,
        error=error_note
    )


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------

@app.get(
    "/metrics/summary",
    response_model=MetricsSummaryResponse,
    tags=["Metrics"],
    summary="Dashboard summary metrics",
)
def get_metrics_summary(db: Session = Depends(get_db)):
    """
    Aggregate dashboard metrics: match rate, exception breakdown,
    oldest unresolved exception, and run-over-run trend.
    """
    # Total runs
    total_runs = db.query(func.count(ReconciliationRun.id)).scalar() or 0

    # Latest run
    latest_run = (
        db.query(ReconciliationRun)
        .order_by(ReconciliationRun.started_at.desc())
        .first()
    )

    total_matched = 0
    total_exceptions = 0
    total_discrepancies = 0
    match_rate = 0.0

    if latest_run:
        total_matched = latest_run.total_matched or 0
        total_exceptions = latest_run.total_exceptions or 0
        total_discrepancies = latest_run.total_discrepancies or 0
        total_records = total_matched + total_exceptions + total_discrepancies
        match_rate = (total_matched / total_records * 100) if total_records > 0 else 0.0

    # Exception breakdown by category
    breakdown_rows = (
        db.query(
            ExceptionModel.category,
            func.count(ExceptionModel.id),
        )
        .group_by(ExceptionModel.category)
        .all()
    )
    exception_breakdown = {row[0].value: row[1] for row in breakdown_rows}

    # Oldest unresolved
    oldest = (
        db.query(ExceptionModel.opened_at)
        .filter(ExceptionModel.status.in_([
            ExceptionStatus.open,
            ExceptionStatus.under_review,
            ExceptionStatus.escalated,
        ]))
        .order_by(ExceptionModel.opened_at.asc())
        .first()
    )

    # Run trend (last 10)
    recent_runs = (
        db.query(ReconciliationRun)
        .order_by(ReconciliationRun.started_at.desc())
        .limit(10)
        .all()
    )
    run_trend = []
    for r in reversed(recent_runs):
        total = (r.total_matched or 0) + (r.total_exceptions or 0) + (r.total_discrepancies or 0)
        rate = ((r.total_matched or 0) / total * 100) if total > 0 else 0
        run_trend.append({
            "run_id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "match_rate": round(rate, 2),
            "total_records": total,
        })

    return MetricsSummaryResponse(
        total_runs=total_runs,
        latest_run_id=latest_run.id if latest_run else None,
        latest_run_at=latest_run.started_at if latest_run else None,
        match_rate=round(match_rate, 2),
        total_matched=total_matched,
        total_exceptions=total_exceptions,
        total_discrepancies=total_discrepancies,
        exception_breakdown=exception_breakdown,
        oldest_unresolved=oldest[0] if oldest else None,
        run_trend=run_trend,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"], summary="Health check")
def health():
    return {"status": "healthy", "version": settings.api_version}
