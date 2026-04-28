# ══════════════════════════════════════════════════════════════════════
# Unified Bond Gateway — Dockerfile
# Multi-stage: build -> slim production image
# ══════════════════════════════════════════════════════════════════════

# ── Stage 1: dependency builder ───────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build essentials (for bcrypt, asyncpg, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libpq-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install passlib[bcrypt] python-dotenv

# ── Stage 2: production image ─────────────────────────────────────────
FROM python:3.11-slim AS production

LABEL maintainer="Bond Gateway <admin@bond-gateway.in>"
LABEL description="Unified Indian Bond Investment Gateway"
LABEL version="1.0.0"

# Security: create non-root user
RUN groupadd -r bondapp && useradd -r -g bondapp bondapp

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Install only runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . /app

# Create data directory with correct ownership
RUN mkdir -p /app/backend/app/data \
    && mkdir -p /app/logs \
    && chown -R bondapp:bondapp /app

# Switch to non-root
USER bondapp

# Expose gateway port
EXPOSE 9000

# Health-check: ping the /api/v1/health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:9000/api/v1/health || exit 1

# Environment defaults (override via docker-compose / k8s secrets)
ENV GATEWAY_PORT=9000 \
    GATEWAY_HOST=0.0.0.0 \
    LOG_LEVEL=INFO \
    LOG_JSON=1 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Start the gateway
CMD ["python", "-m", "uvicorn", \
    "backend.main:app", \
    "--host", "0.0.0.0", "--port", "9000", \
     "--workers", "4", \
     "--log-level", "info", \
     "--no-access-log"]
