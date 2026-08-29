# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first for layer caching
COPY pyproject.toml ./
# Copy source package so hatchling can discover it during install
COPY src/ ./src/

# Install the project and its core dependencies into a prefix directory
# (no cloud SDKs — those are optional extras installed at runtime if needed)
RUN pip install --no-cache-dir --prefix=/install .

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# --- non-root user (uid 1000) ---
RUN groupadd --gid 1000 appgroup \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /sbin/nologin appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ ./src/

# Configuration volume mount-point (remote_servers.yaml will be bind-mounted here)
RUN mkdir -p /app/config \
    && chown -R appuser:appgroup /app

# Switch to non-root user
USER appuser

# PORT can be overridden at runtime; default is 8080
ENV PORT=8080

EXPOSE 8080

# Entrypoint: run the MCP relay module
CMD ["python", "-m", "mcp_relay.main"]
