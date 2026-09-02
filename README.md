# Payment Reconciliation & Exception Management Engine

A production-grade backend system that automatically matches transactions between an internal ledger and payment processor settlement files, classifies mismatches into meaningful categories, and provides an audit-trailed queue for operational review.

## Architecture

```
┌──────────────┐    ┌──────────────┐
│  Ledger CSV  │    │ Settlement   │
│  (internal)  │    │ CSV (proc.)  │
└──────┬───────┘    └──────┬───────┘
       │                   │
       ▼                   ▼
┌──────────────────────────────────┐
│        FastAPI Ingestion         │
│   POST /ingest/ledger            │
│   POST /ingest/settlements       │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│     Rule-Based Matching Engine   │
│                                  │
│  1. Exact match                  │
│  2. Fee-explained match          │
│  3. Fuzzy amount match           │
│  4. Partial refund match         │
│  5. Timing exception             │
│  6. Rounding exception           │
│  7. True discrepancy (catch-all) │
└──────────────┬───────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌──────────────┐ ┌──────────────┐
│   Matched    │ │  Exception   │
│  (no review) │ │    Queue     │
└──────────────┘ │  + Audit Log │
                 └──────────────┘
```

## Three-Tier Classification

| Tier | Meaning | Action |
|------|---------|--------|
| **Matched** | Ledger and settlement agree — no human review needed | Auto-archived |
| **Explainable Exception** | Doesn't match exactly, but a known business rule fully accounts for the gap (fee, rounding, timing) | Logged for audit, low urgency |
| **True Discrepancy** | No known rule explains the gap — real money could be wrong | Operational urgency, requires human review |

## Rule Tiers (Priority Order)

### Rule 1: Exact Match
Reference + gross amount + currency all identical. Clean, perfectly reconciled transaction.

### Rule 2: Fee-Explained Match
Ledger amount matches settlement gross, and `gross - fee = net` within tolerance. The difference between ledger and settlement net is fully explained by the processor fee.

### Rule 3: Fuzzy Amount Match
Amount within configurable rounding tolerance (default: $0.02), same reference. Expected noise from multi-currency processing.

### Rule 4: Partial Refund Match
One-to-many matching: an original charge followed by partial refund entries. Groups by reference, sums settlement amounts, checks if net position reconciles.

### Rule 5: Timing Exception
Reference and amount match, but settlement arrived outside the expected delay window (default: 5 days). Normal in payments ops.

### Rule 6: Rounding Exception
Sub-cent difference within extended tolerance. Expected noise from fee calculations.

### Rule 7: True Discrepancy (Catch-All)
Everything left unexplained: currency mismatches, large amount differences, orphaned records (ledger-only or settlement-only), duplicates with conflicting amounts.

## Tech Stack

- **Python 3.11+** — application logic
- **PostgreSQL** — persistent storage
- **Pandas** — vectorized data matching
- **FastAPI** — REST API
- **SQLAlchemy 2.0** — ORM
- **Pydantic** — request/response validation

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/Salmanmalvasi/PaymentReconciliation.git
cd PaymentReconciliation
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure

Create a `.env` file (or set environment variables):

```env
RECON_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/payment_recon
RECON_ROUNDING_TOLERANCE=0.02
RECON_FEE_TOLERANCE=0.05
RECON_SETTLEMENT_DELAY_DAYS=5
```

### 3. Run Migrations

```bash
# With PostgreSQL running:
psql -U postgres -d payment_recon -f migrations/001_create_core_tables.sql

# To rollback:
psql -U postgres -d payment_recon -f migrations/001_rollback.sql
```

> **Note:** The API also auto-creates tables on startup via SQLAlchemy for dev/demo convenience.

### 4. Generate Synthetic Data

```bash
python -m reconciliation.generator --num-records 10000 --anomaly-rate 0.15 --seed 42
```

Options:
- `--num-records` — Number of base transactions (default: 10,000)
- `--anomaly-rate` — Fraction to inject anomalies (default: 0.15)
- `--seed` — Random seed for reproducibility (default: 42)
- `--output-dir` — Output directory (default: `data/`)

Output files:
- `data/ledger_transactions.csv`
- `data/settlement_records.csv`
- `data/ground_truth.csv` (anomaly labels for verification)

### 5. Start the API

```bash
uvicorn reconciliation.api:app --reload --host 0.0.0.0 --port 8000
```

API docs available at: `http://localhost:8000/docs`

### 6. Run Tests

```bash
python -m pytest tests/ -v
```

## API Endpoints

### Ingestion
```bash
# Ingest ledger transactions
curl -X POST http://localhost:8000/ingest/ledger \
  -F "file=@data/ledger_transactions.csv"

# Ingest settlement records
curl -X POST http://localhost:8000/ingest/settlements \
  -F "file=@data/settlement_records.csv"
```

### Reconciliation
```bash
# Trigger a reconciliation run
curl -X POST http://localhost:8000/reconcile/run

# Query results (with filtering)
curl "http://localhost:8000/reconciliation/results?classification=true_discrepancy&page=1&page_size=20"
```

### Exception Queue
```bash
# List exceptions (filterable by status/category)
curl "http://localhost:8000/exceptions?status=open&page=1"

# Update exception status
curl -X PATCH http://localhost:8000/exceptions/1 \
  -H "Content-Type: application/json" \
  -d '{"status": "under_review", "changed_by": "analyst_1", "note": "Investigating"}'

# Get audit trail
curl http://localhost:8000/exceptions/1/audit-trail
```

### Metrics
```bash
# Dashboard summary
curl http://localhost:8000/metrics/summary
```

### Health Check
```bash
curl http://localhost:8000/health
```

## Configuration

All tolerances are configurable via environment variables (prefix `RECON_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `RECON_DATABASE_URL` | `postgresql://...` | PostgreSQL connection string |
| `RECON_ROUNDING_TOLERANCE` | `0.02` | Max minor-unit difference for rounding noise |
| `RECON_FEE_TOLERANCE` | `0.05` | Max acceptable diff after fee deduction |
| `RECON_SETTLEMENT_DELAY_DAYS` | `5` | Days before missing settlement is flagged |
| `RECON_DEFAULT_NUM_RECORDS` | `10000` | Default generator record count |
| `RECON_DEFAULT_ANOMALY_RATE` | `0.15` | Default anomaly injection rate |

## Project Structure

```
PaymentReconciliation/
├── reconciliation/
│   ├── __init__.py          # Package init
│   ├── api.py               # FastAPI endpoints
│   ├── config.py            # Pydantic settings
│   ├── database.py          # SQLAlchemy engine/session
│   ├── engine.py            # Rule-based matching engine
│   ├── exceptions.py        # Exception queue service layer
│   ├── generator.py         # Synthetic data generator
│   ├── models.py            # SQLAlchemy ORM models
│   └── schemas.py           # Pydantic request/response schemas
├── migrations/
│   ├── 001_create_core_tables.sql
│   └── 001_rollback.sql
├── tests/
│   ├── test_engine.py       # Unit tests for matching rules
│   └── test_exceptions.py   # Unit tests for exception queue
├── schema.md                # Database schema documentation
├── pyproject.toml           # Project config & dependencies
├── README.md                # This file
└── RESULTS.md               # Sample run results
```

## License

MIT
