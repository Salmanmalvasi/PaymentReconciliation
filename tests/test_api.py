import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import os

from reconciliation.api import app, get_db
from reconciliation.models import Base, ReconciliationRun, ReconciliationResult, Classification
from reconciliation.exceptions import create_exception, ExceptionCategory
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

@pytest.fixture
def db_session():
    from sqlalchemy import event, Integer as SAInteger, BigInteger as SABigInteger
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False}
    )
    
    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    from sqlalchemy.ext.compiler import compiles
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
def client(db_session):
    def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()

@pytest.fixture
def sample_exception(db_session):
    from datetime import datetime, timezone
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

    exc = create_exception(
        db_session, result.id,
        ExceptionCategory.amount_mismatch, Classification.true_discrepancy,
    )
    db_session.commit()
    return exc

class TestExceptionExplainEndpoint:
    @patch('anthropic.Anthropic')
    def test_explain_success(self, mock_anthropic_class, client, sample_exception):
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        
        mock_response = MagicMock()
        mock_text = MagicMock()
        mock_text.text = "Hypothesis: AI test reason.\nAction: Escalate."
        mock_response.content = [mock_text]
        mock_client.messages.create.return_value = mock_response

        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        response = client.get(f"/exceptions/{sample_exception.id}/explain")
        assert response.status_code == 200
        data = response.json()
        
        assert "exception" in data
        assert data["exception"]["id"] == sample_exception.id
        assert data["exception"]["status"] == sample_exception.status.value
        assert data["exception"]["category"] == sample_exception.category.value
        
        assert "ai_suggestion" in data
        assert data["ai_suggestion"] is not None, f"Failed to get AI suggestion. Error: {data.get('error')}"
        assert data["ai_suggestion"]["hypothesis"] == "AI test reason."
        assert data["ai_suggestion"]["recommended_action"] == "Escalate."
        assert data["error"] is None

    @patch('anthropic.Anthropic')
    def test_explain_failure_graceful(self, mock_anthropic_class, client, sample_exception):
        mock_anthropic_class.side_effect = Exception("API timeout")

        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        response = client.get(f"/exceptions/{sample_exception.id}/explain")
        assert response.status_code == 200
        data = response.json()
        
        # Base exception data should be fully intact despite failure
        assert "exception" in data
        assert data["exception"]["id"] == sample_exception.id
        assert data["exception"]["status"] == sample_exception.status.value
        assert data["exception"]["category"] == sample_exception.category.value
        
        assert data["ai_suggestion"] is None
        assert data["error"] == "Failed to generate AI suggestion: API timeout"
