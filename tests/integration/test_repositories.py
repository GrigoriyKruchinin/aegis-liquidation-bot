"""Repository tests against a real PostgreSQL (migrated schema).

Uses the isolated ``<POSTGRES_DB>_test`` database from `tests/integration/conftest.py`.
Run with `make up` (Postgres on 127.0.0.1:5433) and then `make test-integration`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
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


async def test_market_measurement_repositories(sessions: async_sessionmaker[AsyncSession]) -> None:
    from datetime import UTC, datetime, timedelta

    from aegis.core.models import (
        EventEnrichment,
        EventValuation,
        LiquidationEvent,
        Sighting,
        TxReceiptInfo,
    )
    from aegis.db.repositories import (
        SqlLiquidationEventRepository,
        SqlSightingRepository,
        SqlStatusRepository,
        SqlUptimeRepository,
    )

    borrower = "0x" + "d4" * 20
    await SqlBorrowerRepository(sessions).upsert_discovered(MARKET, {borrower: 10})
    events = SqlLiquidationEventRepository(sessions)
    event = LiquidationEvent(
        MARKET, "aave_liquidation", "0x" + "ab" * 32, 1, 50, borrower, "0x" + "9a" * 20,
        "0x" + "01" * 20, "0x" + "02" * 20, 10**9, 10**18,
    )  # fmt: skip
    assert await events.add([event]) == 1
    assert await events.add([event]) == 0  # idempotent
    assert await events.events_in_tx("base", event.tx_hash) == 1
    assert await events.borrower_known(MARKET, borrower, 50)
    assert not await events.borrower_known(MARKET, borrower, 5)
    ((event_id, attempts, loaded),) = await events.pending(10, 5)
    assert attempts == 0
    assert loaded == event

    sightings = SqlSightingRepository(sessions)
    await sightings.record(MARKET, [Sighting(borrower, Decimal("0.98"), Decimal(900), None, None)],
                           frozenset())  # fmt: skip
    await sightings.record(
        MARKET, [Sighting(borrower, Decimal("0.95"), Decimal(950), Decimal(7), Decimal(8))],
        frozenset(),
    )  # fmt: skip
    now = datetime.now(UTC)
    first = await sightings.first_seen(MARKET, borrower, now, 3600)
    assert first is not None and first <= now

    uptime = SqlUptimeRepository(sessions)
    await uptime.mark("base", now)
    await uptime.mark("base", now)  # idempotent
    assert await uptime.was_online("base", now)
    assert not await uptime.was_online("base", now - timedelta(hours=5))

    receipt = TxReceiptInfo(
        "0x" + "ee" * 20, None, 3, 400_000, 10**7, 10**12, 1, int(now.timestamp()), 5
    )
    enrichment = EventEnrichment(
        block_time=now, receipt=receipt,
        valuation=EventValuation(Decimal(1000), Decimal(1050)), gas_cost_usd=Decimal("0.02"),
        debt_symbol="USDC", collateral_symbol="WETH", liquidatable_since_block=48, lag_blocks=2,
        lag_capped=False, our_first_seen_at=first, bot_online=True, borrower_known=True,
    )  # fmt: skip
    await events.save_enrichment(event_id, enrichment)
    assert await events.pending(10, 5) == []
    await sightings.close(MARKET, borrower, "liquidated", now)

    activity = await SqlStatusRepository(sessions).activity_since(now - timedelta(hours=1))
    assert activity.market_liquidations == 1
    assert activity.market_gross_usd == Decimal(50)
    assert activity.market_seen_first == 1

    record = await events.get_record(event_id)
    assert record.net_profit_usd == Decimal("49.98") and record.tx_index == 3
    assert record.debt_symbol == "USDC" and record.lag_blocks == 2
    window = await events.enriched_between(now - timedelta(minutes=1), now + timedelta(minutes=1),
                                           ("aave_liquidation",))  # fmt: skip
    assert [r.id for r in window] == [event_id]
    assert await events.enriched_between(now - timedelta(minutes=1), now, ("comet_buy",)) == []
    assert await events.pending_count() == 0


async def test_paid_liquidations_enriched_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from aegis.core.errors import NotFoundError
    from aegis.core.models import LiquidationEvent
    from aegis.db.repositories import SqlLiquidationEventRepository

    events = SqlLiquidationEventRepository(sessions)
    absorb = LiquidationEvent(
        MARKET, "comet_absorb", "0x" + "c1" * 32, 0, 10, "0x" + "d1" * 20, "0x" + "9a" * 20,
        "0x" + "01" * 20, None, 1, 0,
    )  # fmt: skip
    paid = LiquidationEvent(
        MARKET, "aave_liquidation", "0x" + "c2" * 32, 0, 900, "0x" + "d2" * 20,
        "0x" + "9a" * 20, "0x" + "01" * 20, "0x" + "02" * 20, 1, 1,
    )  # fmt: skip
    await events.add([absorb, paid])
    first = await events.pending(1, 5)
    assert first[0][2].kind == "aave_liquidation"  # newer, but paid: goes first
    assert await events.pending_count() == 2
    with pytest.raises(NotFoundError):
        await events.get_record(first[0][0])  # not enriched yet


async def test_liquidation_events_bulk_insert(sessions: async_sessionmaker[AsyncSession]) -> None:
    from aegis.core.models import LiquidationEvent
    from aegis.db.repositories import SqlLiquidationEventRepository

    events = [
        LiquidationEvent(
            MARKET,
            "aave_liquidation",
            f"0x{i:064x}",
            0,
            i,
            None,
            "0x" + "9a" * 20,
            None,
            None,
            1,
            1,
        )
        for i in range(2500)  # 2500 x 15 columns > asyncpg's 32767 parameter limit
    ]
    assert await SqlLiquidationEventRepository(sessions).add(events) == 2500


async def test_sighting_episodes(sessions: async_sessionmaker[AsyncSession]) -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    from aegis.core.models import LiquidationEvent, Sighting
    from aegis.db.models import SightingRow
    from aegis.db.repositories import SqlLiquidationEventRepository, SqlSightingRepository

    repo = SqlSightingRepository(sessions)
    a, b = "0x" + "a5" * 20, "0x" + "b5" * 20
    sight = Sighting(a, Decimal("0.99"), Decimal(100), None, None)
    # Concurrent scanners must not create two open episodes for the same borrower.
    await asyncio.gather(*(repo.record(MARKET, [sight], frozenset()) for _ in range(5)))
    await repo.record(MARKET, [Sighting(a, Decimal("0.90"), Decimal(150), Decimal(9), None)],
                      frozenset())  # fmt: skip
    async with sessions() as session:
        rows = (await session.scalars(select(SightingRow).where(SightingRow.borrower == a))).all()
    assert len(rows) == 1
    assert rows[0].observations == 6 and rows[0].min_health_factor == Decimal("0.90")
    assert rows[0].best_net_profit_usd == Decimal(9)

    # Ongoing episode counts as "seen" however long ago it started.
    later = datetime.now(UTC) + timedelta(hours=10)
    assert await repo.first_seen(MARKET, a, later, 600) is not None
    # Healthy -> closed; a closed episode last seen long before does not count.
    await repo.record(MARKET, [], frozenset({a, b}))
    assert await repo.first_seen(MARKET, a, later, 600) is None
    # A new episode opens after the closed one.
    await repo.record(MARKET, [sight], frozenset())
    async with sessions() as session:
        count = len(
            (await session.scalars(select(SightingRow).where(SightingRow.borrower == a))).all()
        )
    assert count == 2

    events = SqlLiquidationEventRepository(sessions)
    await events.add(
        [
            LiquidationEvent(
                MARKET, "aave_liquidation", "0x" + "c1" * 32, 0, 100, a, b, None, None, 1, 1
            ),
            LiquidationEvent(
                MARKET, "aave_liquidation", "0x" + "c2" * 32, 0, 140, a, b, None, None, 1, 1
            ),
        ]
    )
    assert await events.previous_liquidation_block(MARKET, a, 140, 50) == 100
    assert await events.previous_liquidation_block(MARKET, a, 100, 50) is None
