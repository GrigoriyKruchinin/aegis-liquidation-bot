# syntax=docker/dockerfile:1.7

# ---- builder: resolve and install dependencies with uv ----------------------
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim image, non-root user -------------------------------------
FROM python:3.12-slim AS runtime
RUN groupadd --system aegis && useradd --system --gid aegis --home-dir /app aegis
WORKDIR /app
COPY --from=builder --chown=aegis:aegis /app/.venv /app/.venv
COPY --from=builder --chown=aegis:aegis /app/src /app/src
COPY --chown=aegis:aegis alembic.ini ./
COPY --chown=aegis:aegis migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER aegis
CMD ["celery", "-A", "aegis.workers.app", "worker", "-Q", "scan", "--loglevel=INFO"]
