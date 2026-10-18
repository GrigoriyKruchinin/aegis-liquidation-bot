"""Repository tests against a real PostgreSQL (migrated schema).

Uses the isolated ``<POSTGRES_DB>_test`` database from `tests/integration/conftest.py`.
Run with `make up-local` (Postgres on 127.0.0.1:5433) and then `make test-integration`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aegis.core.models import HealthCheck
from aegis.db.repositories import (
    SqlBorrowerRepository,
    SqlCheckpointRepository,
    SqlNotificationRepository,
    SqlOpportunityRepository,
)
from tests.factories import MARKET, summary

pytestmark = pytest.mark.integration


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


async def test_status_aggregates(sessions: async_sessionmaker[AsyncSession]) -> None:
    from datetime import UTC, datetime, timedelta

    from aegis.db.repositories import SqlStatusRepository

    borrowers = SqlBorrowerRepository(sessions)
    a, b, c = "0x" + "a1" * 20, "0x" + "b1" * 20, "0x" + "c1" * 20
    await borrowers.upsert_discovered(MARKET, {a: 1, b: 1, c: 1})
    await borrowers.update_health(
        MARKET,
        [
            HealthCheck(a, Decimal("1.02"), Decimal(1000), Decimal(2000), False),
            HealthCheck(b, Decimal("2.5"), Decimal(500), Decimal(3000), False),
            HealthCheck(c, None, Decimal(0), Decimal(0), False),  # inactive
        ],
        Decimal(10),
    )
    opportunities = SqlOpportunityRepository(sessions)
    opportunity_id = await opportunities.add(summary(detected_at=datetime.now(UTC)))
    notifications = SqlNotificationRepository(sessions)
    await notifications.record(opportunity_id, "telegram", "sent")
    await notifications.record(opportunity_id, "telegram", "retrying")
    await notifications.record(None, "telegram", "sent")  # system message: not an alert

    repo = SqlStatusRepository(sessions)
    stats = (await repo.market_stats(Decimal("1.05")))[MARKET.key]
    assert stats.active_borrowers == 2
    assert stats.total_debt_usd == Decimal(1500)
    assert stats.at_risk == 1
    assert stats.min_health_factor == Decimal("1.02")

    activity = await repo.activity_since(datetime.now(UTC) - timedelta(hours=1))
    assert activity.opportunities == 1
    assert activity.best_net_profit_usd == summary().net_profit_usd
    assert (activity.alerts_sent, activity.alerts_failed) == (1, 0)
    future = await repo.activity_since(datetime.now(UTC) + timedelta(hours=1))
    assert future.opportunities == 0 and future.best_net_profit_usd is None
