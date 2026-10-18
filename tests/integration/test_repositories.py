"""Repository tests against a real PostgreSQL (migrated schema).

Run with: RUN_INTEGRATION=1 POSTGRES_HOST=... POSTGRES_PORT=... make test ARGS=tests/integration
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aegis.config.settings import AppSettings
from aegis.core.models import HealthCheck
from aegis.db.repositories import (
    SqlBorrowerRepository,
    SqlCheckpointRepository,
    SqlNotificationRepository,
    SqlOpportunityRepository,
)
from aegis.db.session import create_engine, create_session_factory
from tests.factories import MARKET, summary

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(AppSettings().database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE notifications, opportunities, borrowers, indexer_checkpoints CASCADE")
        )
    yield create_session_factory(engine)
    await engine.dispose()


async def test_borrower_lifecycle(sessions: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlBorrowerRepository(sessions)
    a, b = "0x" + "aa" * 20, "0x" + "bb" * 20
    assert await repo.upsert_discovered(MARKET, {a: 10, b: 11}) == 2
    assert await repo.upsert_discovered(MARKET, {a: 20}) == 0
    assert sorted(await repo.list_active(MARKET)) == [a, b]

    await repo.update_health(
        MARKET,
        [
            HealthCheck(a, Decimal("1.01"), Decimal(500), Decimal(600), False),
            HealthCheck(b, None, Decimal(0), Decimal(10), False),
        ],
        min_debt_usd=Decimal(10),
    )
    assert await repo.list_active(MARKET) == [a]
    assert await repo.list_watchlist(MARKET, Decimal("1.05")) == [a]
    assert await repo.list_watchlist(MARKET, Decimal("1.00")) == []

    # A new Borrow event reactivates the account.
    await repo.upsert_discovered(MARKET, {b: 30})
    assert sorted(await repo.list_active(MARKET)) == [a, b]


async def test_huge_health_factor_is_clamped(sessions: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlBorrowerRepository(sessions)
    a = "0x" + "cc" * 20
    await repo.upsert_discovered(MARKET, {a: 1})
    await repo.update_health(
        MARKET,
        [HealthCheck(a, Decimal(10) ** 40, Decimal(100), Decimal(10**9), False)],
        Decimal(10),
    )
    assert await repo.list_watchlist(MARKET, Decimal(10) ** 7) == [a]


async def test_checkpoints(sessions: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlCheckpointRepository(sessions)
    assert await repo.get(MARKET) is None
    await repo.set(MARKET, 100)
    await repo.set(MARKET, 250)
    assert await repo.get(MARKET) == 250


async def test_opportunity_roundtrip_and_notification(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    opportunities = SqlOpportunityRepository(sessions)
    original = summary(notes=("absorb + buyCollateral",))
    opportunity_id = await opportunities.add(original)
    loaded = await opportunities.get(opportunity_id)
    assert loaded.id == opportunity_id
    assert loaded.net_profit_usd == original.net_profit_usd
    assert loaded.notes == original.notes
    assert loaded.detected_at == original.detected_at
    await SqlNotificationRepository(sessions).record(opportunity_id, "telegram", "sent")
