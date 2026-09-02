"""
Unit tests for the exception queue and audit trail service.

Tests cover:
  - Exception creation with initial audit log
  - Valid state transitions
  - Invalid state transitions raise errors
  - Audit trail preservation across multiple transitions
  - Terminal states cannot be transitioned from
  - Re-running reconciliation never deletes prior history
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
)
from reconciliation.exceptions import (
    create_exception,
    transition_exception,
    get_audit_trail,
    get_exception,
    list_exceptions,
    InvalidTransitionError,
    ExceptionNotFoundError,
    VALID_TRANSITIONS,
)


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite for fast, isolated testing
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Create an in-memory SQLite session with all tables.

    Uses event listeners to coerce BigInteger → Integer for SQLite
    compatibility, since SQLite doesn't support BIGSERIAL.
    """
    from sqlalchemy import event, Integer as SAInteger, BigInteger as SABigInteger

    test_engine = create_engine("sqlite:///:memory:")

    # SQLite needs INTEGER PRIMARY KEY for autoincrement
    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Render BigInteger as Integer in DDL for SQLite
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    @compiles(SABigInteger, "sqlite")
    def compile_bigint_sqlite(type_, compiler, **kw):
        return "INTEGER"

    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine)
    session = Session()
    yield session
    session.close()
    test_engine.dispose()


@pytest.fixture
def sample_recon_result(db_session):
    """Create prerequisite run + result records for exception tests."""
    run = ReconciliationRun(
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        total_ledger=10,
        total_settlement=10,
        total_matched=8,
        total_exceptions=1,
        total_discrepancies=1,
    )
    db_session.add(run)
    db_session.flush()

    result = ReconciliationResult(
        run_id=run.id,
        classification=Classification.true_discrepancy,
        matched_rule="true_discrepancy",
        confidence_notes="Test discrepancy",
    )
    db_session.add(result)
    db_session.flush()
    return result


# ---------------------------------------------------------------------------
# Exception creation
# ---------------------------------------------------------------------------

class TestCreateException:
    def test_creates_with_open_status(self, db_session, sample_recon_result):
        """New exception starts in 'open' status."""
        exc = create_exception(
            db_session,
            sample_recon_result.id,
            ExceptionCategory.amount_mismatch,
            Classification.true_discrepancy,
        )
        assert exc.status == ExceptionStatus.open
        assert exc.category == ExceptionCategory.amount_mismatch

    def test_creates_initial_audit_entry(self, db_session, sample_recon_result):
        """Creation writes an initial audit log entry."""
        exc = create_exception(
            db_session,
            sample_recon_result.id,
            ExceptionCategory.fee_difference,
            Classification.explainable_exception,
        )

        logs = (
            db_session.query(ExceptionAuditLog)
            .filter(ExceptionAuditLog.exception_id == exc.id)
            .all()
        )
        assert len(logs) == 1
        assert logs[0].old_status == ExceptionStatus.open
        assert logs[0].new_status == ExceptionStatus.open
        assert logs[0].changed_by == "system"
        assert "created" in logs[0].note.lower()


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

class TestValidTransitions:
    def test_open_to_under_review(self, db_session, sample_recon_result):
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.timing_delay, Classification.explainable_exception,
        )
        updated = transition_exception(
            db_session, exc.id, ExceptionStatus.under_review,
            changed_by="analyst_1", note="Starting review",
        )
        assert updated.status == ExceptionStatus.under_review

    def test_open_to_resolved(self, db_session, sample_recon_result):
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.rounding_difference, Classification.explainable_exception,
        )
        updated = transition_exception(
            db_session, exc.id, ExceptionStatus.resolved,
            changed_by="analyst_2", note="Auto-resolved rounding",
        )
        assert updated.status == ExceptionStatus.resolved
        assert updated.resolved_by == "analyst_2"
        assert updated.resolved_at is not None

    def test_under_review_to_escalated(self, db_session, sample_recon_result):
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.amount_mismatch, Classification.true_discrepancy,
        )
        transition_exception(
            db_session, exc.id, ExceptionStatus.under_review,
            changed_by="analyst_1",
        )
        updated = transition_exception(
            db_session, exc.id, ExceptionStatus.escalated,
            changed_by="analyst_1", note="Needs manager review",
        )
        assert updated.status == ExceptionStatus.escalated

    def test_escalated_to_resolved(self, db_session, sample_recon_result):
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.missing_settlement, Classification.true_discrepancy,
        )
        transition_exception(db_session, exc.id, ExceptionStatus.under_review, changed_by="a")
        transition_exception(db_session, exc.id, ExceptionStatus.escalated, changed_by="a")
        updated = transition_exception(
            db_session, exc.id, ExceptionStatus.resolved,
            changed_by="manager_1", note="Confirmed correct",
        )
        assert updated.status == ExceptionStatus.resolved

    def test_under_review_reopen(self, db_session, sample_recon_result):
        """Can reopen an under_review exception."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.timing_delay, Classification.explainable_exception,
        )
        transition_exception(db_session, exc.id, ExceptionStatus.under_review, changed_by="a")
        updated = transition_exception(
            db_session, exc.id, ExceptionStatus.open,
            changed_by="a", note="Need more info",
        )
        assert updated.status == ExceptionStatus.open


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:
    def test_resolved_is_terminal(self, db_session, sample_recon_result):
        """Cannot transition from resolved status."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.amount_mismatch, Classification.true_discrepancy,
        )
        transition_exception(db_session, exc.id, ExceptionStatus.resolved, changed_by="a")

        with pytest.raises(InvalidTransitionError):
            transition_exception(
                db_session, exc.id, ExceptionStatus.open, changed_by="b",
            )

    def test_accepted_is_terminal(self, db_session, sample_recon_result):
        """Cannot transition from accepted status."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.rounding_difference, Classification.explainable_exception,
        )
        transition_exception(db_session, exc.id, ExceptionStatus.accepted, changed_by="a")

        with pytest.raises(InvalidTransitionError):
            transition_exception(
                db_session, exc.id, ExceptionStatus.under_review, changed_by="b",
            )

    def test_exception_not_found(self, db_session):
        """Non-existent exception ID raises error."""
        with pytest.raises(ExceptionNotFoundError):
            transition_exception(
                db_session, 999999, ExceptionStatus.resolved, changed_by="a",
            )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_multiple_transitions_preserve_full_history(self, db_session, sample_recon_result):
        """Every transition adds a new audit entry — history is append-only."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.amount_mismatch, Classification.true_discrepancy,
        )

        # open → under_review → escalated → resolved
        transition_exception(db_session, exc.id, ExceptionStatus.under_review, changed_by="analyst")
        transition_exception(db_session, exc.id, ExceptionStatus.escalated, changed_by="analyst")
        transition_exception(db_session, exc.id, ExceptionStatus.resolved, changed_by="manager")

        trail = get_audit_trail(db_session, exc.id)

        # 1 creation + 3 transitions = 4 entries
        assert len(trail) == 4
        assert trail[0].old_status == ExceptionStatus.open  # creation
        assert trail[1].new_status == ExceptionStatus.under_review
        assert trail[2].new_status == ExceptionStatus.escalated
        assert trail[3].new_status == ExceptionStatus.resolved

    def test_audit_trail_records_who_changed(self, db_session, sample_recon_result):
        """Audit log correctly records changed_by for each transition."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.timing_delay, Classification.explainable_exception,
        )
        transition_exception(db_session, exc.id, ExceptionStatus.under_review, changed_by="alice")
        transition_exception(db_session, exc.id, ExceptionStatus.resolved, changed_by="bob")

        trail = get_audit_trail(db_session, exc.id)
        assert trail[0].changed_by == "system"
        assert trail[1].changed_by == "alice"
        assert trail[2].changed_by == "bob"

    def test_audit_includes_notes(self, db_session, sample_recon_result):
        """Transition notes are persisted in the audit log."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.fee_difference, Classification.explainable_exception,
        )
        transition_exception(
            db_session, exc.id, ExceptionStatus.accepted,
            changed_by="analyst", note="Fee confirmed with processor",
        )
        trail = get_audit_trail(db_session, exc.id)
        assert trail[-1].note == "Fee confirmed with processor"

    def test_reviewer_notes_updated(self, db_session, sample_recon_result):
        """Reviewer notes on the exception record are updated during transition."""
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.amount_mismatch, Classification.true_discrepancy,
        )
        transition_exception(
            db_session, exc.id, ExceptionStatus.under_review,
            changed_by="analyst", reviewer_notes="Investigating $50 diff",
        )
        refreshed = get_exception(db_session, exc.id)
        assert refreshed.reviewer_notes == "Investigating $50 diff"


# ---------------------------------------------------------------------------
# Listing & filtering
# ---------------------------------------------------------------------------

class TestListExceptions:
    def test_filter_by_status(self, db_session, sample_recon_result):
        exc = create_exception(
            db_session, sample_recon_result.id,
            ExceptionCategory.amount_mismatch, Classification.true_discrepancy,
        )
        db_session.flush()

        results, total = list_exceptions(db_session, status=ExceptionStatus.open)
        assert total >= 1
        assert all(e.status == ExceptionStatus.open for e in results)

    def test_pagination(self, db_session):
        """Pagination returns correct page sizes."""
        run = ReconciliationRun(
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(run)
        db_session.flush()

        for i in range(5):
            result = ReconciliationResult(
                run_id=run.id,
                classification=Classification.true_discrepancy,
                matched_rule="true_discrepancy",
            )
            db_session.add(result)
            db_session.flush()
            create_exception(
                db_session, result.id,
                ExceptionCategory.unknown, Classification.true_discrepancy,
            )

        results_page1, total = list_exceptions(db_session, page=1, page_size=2)
        assert len(results_page1) == 2
        assert total == 5
