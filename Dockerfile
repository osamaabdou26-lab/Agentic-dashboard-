# syntax=docker/dockerfile:1

# --- Build stage: resolve and install the package into an isolated prefix ---
FROM python:3.12-slim AS builder

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml ./
COPY src ./src

RUN pip install --prefix=/install .

# --- Runtime stage: no compiler, no build cache, non-root user ---
FROM python:3.12-slim AS runtime

RUN groupadd --system searchiq \
    && useradd --system --gid searchiq --home-dir /app --create-home searchiq

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEARCHIQ_DB=/app/data/searchiq.db \
    PORT=8000

COPY --from=builder /install /usr/local
COPY docker-entrypoint.sh ./

RUN chmod +x docker-entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R searchiq:searchiq /app

USER searchiq
EXPOSE 8000

# The container has no data on first boot; the entrypoint seeds a sample store
# before the healthcheck's first window closes, same as the Streamlit deploy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/status', timeout=3)"

ENTRYPOINT ["./docker-entrypoint.sh"]
