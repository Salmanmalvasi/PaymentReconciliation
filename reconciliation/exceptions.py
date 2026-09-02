"""
Exception queue service layer.

Manages the lifecycle of reconciliation exceptions:
    open → under_review → resolved | accepted | escalated

Every state change writes an append-only audit log entry. History is never
mutated — this is the compliance backbone of the system.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from reconciliation.models import (
    Exception as ExceptionModel,
    ExceptionAuditLog,
    ExceptionStatus,
    ExceptionCategory,
    ReconciliationResult,
    Classification,
)


# ---------------------------------------------------------------------------
# Valid state transitions
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[ExceptionStatus, set[ExceptionStatus]] = {
    ExceptionStatus.open: {
        ExceptionStatus.under_review,
        ExceptionStatus.resolved,
        ExceptionStatus.accepted,
        ExceptionStatus.escalated,
    },
    ExceptionStatus.under_review: {
        ExceptionStatus.resolved,
        ExceptionStatus.accepted,
        ExceptionStatus.escalated,
        ExceptionStatus.open,  # allow reopen
    },
    ExceptionStatus.escalated: {
        ExceptionStatus.under_review,
        ExceptionStatus.resolved,
        ExceptionStatus.accepted,
    },
    ExceptionStatus.resolved: set(),   # terminal
    ExceptionStatus.accepted: set(),   # terminal
}


class InvalidTransitionError(Exception):
    """Raised when an invalid exception status transition is attempted."""
    pass


class ExceptionNotFoundError(Exception):
    """Raised when a requested exception does not exist."""
    pass


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

def create_exception(
    db: Session,
    reconciliation_result_id: int,
    category: ExceptionCategory,
    classification: Classification,
) -> ExceptionModel:
    """
    Create a new exception queue entry for a reconciliation result.

    Automatically sets status to 'open' and logs the initial audit entry.
    Only creates exceptions for explainable_exception and true_discrepancy
    classifications.
    """
    exc = ExceptionModel(
        reconciliation_result_id=reconciliation_result_id,
        status=ExceptionStatus.open,
        category=category,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(exc)
    db.flush()  # get the ID

    # Initial audit entry
    audit = ExceptionAuditLog(
        exception_id=exc.id,
        changed_by="system",
        old_status=ExceptionStatus.open,
        new_status=ExceptionStatus.open,
        note=f"Exception created for {classification.value} result",
        changed_at=datetime.now(timezone.utc),
    )
    db.add(audit)

    return exc


def transition_exception(
    db: Session,
    exception_id: int,
    new_status: ExceptionStatus,
    changed_by: str,
    note: Optional[str] = None,
    reviewer_notes: Optional[str] = None,
) -> ExceptionModel:
    """
    Transition an exception to a new status.

    Enforces valid state transitions and always writes an audit log entry.
    Raises InvalidTransitionError for invalid transitions.
    """
    exc = db.query(ExceptionModel).filter(ExceptionModel.id == exception_id).first()
    if not exc:
        raise ExceptionNotFoundError(f"Exception {exception_id} not found")

    old_status = exc.status

    if new_status not in VALID_TRANSITIONS.get(old_status, set()):
        raise InvalidTransitionError(
            f"Cannot transition from {old_status.value} to {new_status.value}. "
            f"Valid transitions: {[s.value for s in VALID_TRANSITIONS.get(old_status, set())]}"
        )

    # Update exception
    exc.status = new_status
    if reviewer_notes is not None:
        exc.reviewer_notes = reviewer_notes

    if new_status in (ExceptionStatus.resolved, ExceptionStatus.accepted):
        exc.resolved_by = changed_by
        exc.resolved_at = datetime.now(timezone.utc)

    # Write audit log (append-only, never mutate)
    audit = ExceptionAuditLog(
        exception_id=exception_id,
        changed_by=changed_by,
        old_status=old_status,
        new_status=new_status,
        note=note,
        changed_at=datetime.now(timezone.utc),
    )
    db.add(audit)
    db.flush()

    return exc


def get_exception(db: Session, exception_id: int) -> ExceptionModel:
    """Retrieve a single exception by ID."""
    exc = db.query(ExceptionModel).filter(ExceptionModel.id == exception_id).first()
    if not exc:
        raise ExceptionNotFoundError(f"Exception {exception_id} not found")
    return exc


def get_audit_trail(db: Session, exception_id: int) -> list[ExceptionAuditLog]:
    """Retrieve the full audit trail for an exception, ordered chronologically."""
    exc = db.query(ExceptionModel).filter(ExceptionModel.id == exception_id).first()
    if not exc:
        raise ExceptionNotFoundError(f"Exception {exception_id} not found")

    return (
        db.query(ExceptionAuditLog)
        .filter(ExceptionAuditLog.exception_id == exception_id)
        .order_by(ExceptionAuditLog.changed_at)
        .all()
    )


def list_exceptions(
    db: Session,
    status: Optional[ExceptionStatus] = None,
    category: Optional[ExceptionCategory] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ExceptionModel], int]:
    """
    List exceptions with optional filtering and pagination.

    Returns (exceptions, total_count) tuple.
    """
    query = db.query(ExceptionModel)

    if status:
        query = query.filter(ExceptionModel.status == status)
    if category:
        query = query.filter(ExceptionModel.category == category)

    total = query.count()
    exceptions = (
        query
        .order_by(ExceptionModel.opened_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return exceptions, total
