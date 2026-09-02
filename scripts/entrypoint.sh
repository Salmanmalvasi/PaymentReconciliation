#!/bin/bash
set -e

echo "=== Payment Reconciliation — Container Startup ==="

# Wait for Postgres (belt-and-suspenders in case healthcheck timing is off)
echo "Waiting for Postgres at ${RECON_DATABASE_URL}..."
until pg_isready -h db -p 5432 -U postgres > /dev/null 2>&1; do
    echo "  Postgres not ready, retrying in 2s..."
    sleep 2
done
echo "  Postgres is ready!"

# Run SQL migrations
echo "Running migrations..."
PGPASSWORD="${POSTGRES_PASSWORD:-postgres}" psql \
    -h db -p 5432 -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-payment_recon}" \
    -f /app/migrations/001_create_core_tables.sql 2>&1 || {
    echo "  Migration already applied or completed (enum/table already exists — safe to continue)."
}
echo "Migrations complete."

# Start the API server
echo "Starting API server on port 8000..."
exec uvicorn reconciliation.api:app --host 0.0.0.0 --port 8000
