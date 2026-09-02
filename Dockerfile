FROM python:3.12-slim AS builder

WORKDIR /app

# Install build deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Create a minimal package layout so pip can resolve the editable install
COPY reconciliation/__init__.py reconciliation/__init__.py

RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir anthropic

# --- Runtime stage ---
FROM python:3.12-slim

WORKDIR /app

# Runtime dependency: libpq for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY reconciliation/ reconciliation/
COPY migrations/ migrations/
COPY pyproject.toml ./
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
