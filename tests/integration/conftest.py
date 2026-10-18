"""Integration fixtures.

Repository tests use a dedicated database ``<POSTGRES_DB>_test`` that is created on demand, so
they never touch the data of a running bot. Connection settings come from `.env` (password,
user, db name) and can be overridden:

    TEST_POSTGRES_HOST (default: localhost)
    TEST_POSTGRES_PORT (default: 5433, the port published by docker-compose.local.yml)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aegis.config.settings import DatabaseSettings
from aegis.db.models import Base

CONNECT_TIMEOUT_SECONDS = 5
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _test_database() -> tuple[DatabaseSettings, str]:
    base = DatabaseSettings(_env_file=ENV_FILE if ENV_FILE.exists() else None)
    settings = base.model_copy(
        update={
            "postgres_host": os.environ.get("TEST_POSTGRES_HOST", "localhost"),
            "postgres_port": int(os.environ.get("TEST_POSTGRES_PORT", "5433")),
        }
    )
    return settings, f"{settings.postgres_db}_test"


async def _ensure_database(settings: DatabaseSettings, name: str) -> None:
    admin_url = settings.model_copy(update={"postgres_db": "postgres"}).database_url
    engine = create_async_engine(
        admin_url.get_secret_value(),
        isolation_level="AUTOCOMMIT",
        connect_args={"timeout": CONNECT_TIMEOUT_SECONDS},
    )
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to a clean, freshly created test schema."""
    settings, name = _test_database()
    try:
        await _ensure_database(settings, name)
    except (OSError, TimeoutError) as exc:
        pytest.skip(
            f"PostgreSQL not reachable at {settings.postgres_host}:{settings.postgres_port} "
            f"({type(exc).__name__}); start it with `make up-local` or set TEST_POSTGRES_*"
        )
    url: SecretStr = settings.model_copy(update={"postgres_db": name}).database_url
    engine = create_async_engine(
        url.get_secret_value(), connect_args={"timeout": CONNECT_TIMEOUT_SECONDS}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
