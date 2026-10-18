"""Async engine and session factory."""

from __future__ import annotations

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: SecretStr, *, pool_size: int = 5) -> AsyncEngine:
    """Create an asyncpg engine with connection health checks."""
    return create_async_engine(
        database_url.get_secret_value(),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with `expire_on_commit=False` (rows are read after commit)."""
    return async_sessionmaker(engine, expire_on_commit=False)
