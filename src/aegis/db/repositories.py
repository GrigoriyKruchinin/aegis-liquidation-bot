"""SQLAlchemy implementations of the repository interfaces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Table,
    bindparam,
    func,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import ReturningInsert

from aegis.core.errors import NotFoundError, RepositoryError
from aegis.core.models import (
    ActivityStats,
    EventEnrichment,
    HealthCheck,
    LiquidationEvent,
    MarketRef,
    MarketStats,
    OpportunitySummary,
    Sighting,
)
from aegis.db.models import (
    BorrowerRow,
    IndexerCheckpointRow,
    LiquidationEventRow,
    NotificationRow,
    OpportunityRow,
    SightingRow,
    UptimeRow,
)

# Stored health factors are clamped to fit NUMERIC(38, 18).
MAX_STORED_HF = Decimal(1_000_000)
UPSERT_BATCH = 1000


def _market_filter(market: MarketRef) -> tuple[ColumnElement[bool], ...]:
    return (
        BorrowerRow.chain == market.chain,
        BorrowerRow.protocol == market.protocol,
        BorrowerRow.market == market.address,
    )


class SqlBorrowerRepository:
    """`IBorrowerRepository` backed by PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert_discovered(self, market: MarketRef, borrowers: Mapping[str, int]) -> int:
        """Insert new borrowers (or reactivate known ones). Returns the number of new rows."""
        items = list(borrowers.items())
        inserted = 0
        try:
            async with self._sessions.begin() as session:
                for i in range(0, len(items), UPSERT_BATCH):
                    rows = [
                        {
                            "chain": market.chain,
                            "protocol": market.protocol,
                            "market": market.address,
                            "address": address,
                            "first_seen_block": block,
                            "last_seen_block": block,
                            "active": True,
                        }
                        for address, block in items[i : i + UPSERT_BATCH]
                    ]
                    base = insert(BorrowerRow).values(rows)
                    stmt: ReturningInsert[tuple[bool]] = base.on_conflict_do_update(
                        constraint="uq_borrowers_market_addr",
                        set_={
                            "last_seen_block": func.greatest(
                                BorrowerRow.last_seen_block, base.excluded.last_seen_block
                            ),
                            "active": True,
                        },
                    ).returning(literal_column("(xmax = 0)", Boolean))
                    result = await session.execute(stmt)
                    inserted += sum(1 for (is_new,) in result if is_new)
        except SQLAlchemyError as exc:
            raise RepositoryError(f"borrower upsert failed: {type(exc).__name__}") from exc
        return inserted

    async def list_active(self, market: MarketRef) -> list[str]:
        """All active borrowers of a market."""
        stmt = select(BorrowerRow.address).where(*_market_filter(market), BorrowerRow.active)
        async with self._sessions() as session:
            return list((await session.scalars(stmt)).all())

    async def list_watchlist(self, market: MarketRef, hf_threshold: Decimal) -> list[str]:
        """Active borrowers whose last known HF is below `hf_threshold`."""
        stmt = select(BorrowerRow.address).where(
            *_market_filter(market),
            BorrowerRow.active,
            BorrowerRow.health_factor.is_not(None),
            BorrowerRow.health_factor < hf_threshold,
        )
        async with self._sessions() as session:
            return list((await session.scalars(stmt)).all())

    async def update_health(
        self, market: MarketRef, checks: Sequence[HealthCheck], min_debt_usd: Decimal
    ) -> None:
        """Store the latest snapshot; accounts without meaningful debt become inactive."""
        if not checks:
            return
        now = datetime.now(UTC)
        table = BorrowerRow.__table__
        assert isinstance(table, Table)  # noqa: S101  # narrows FromClause for typing
        stmt = (
            update(table)
            .where(
                table.c.chain == market.chain,
                table.c.protocol == market.protocol,
                table.c.market == market.address,
                table.c.address == bindparam("b_address"),
            )
            .values(
                health_factor=bindparam("b_hf"),
                debt_usd=bindparam("b_debt"),
                collateral_usd=bindparam("b_collateral"),
                active=bindparam("b_active"),
                last_checked_at=bindparam("b_now"),
            )
        )
        params = [
            {
                "b_address": check.borrower,
                "b_hf": None
                if check.health_factor is None
                else min(check.health_factor, MAX_STORED_HF),
                "b_debt": check.debt_usd,
                "b_collateral": check.collateral_usd,
                "b_active": check.health_factor is not None and check.debt_usd >= min_debt_usd,
                "b_now": now,
            }
            for check in checks
        ]
        try:
            async with self._sessions.begin() as session:
                await session.execute(stmt, params)
        except SQLAlchemyError as exc:
            raise RepositoryError(f"health update failed: {type(exc).__name__}") from exc


class SqlCheckpointRepository:
    """`ICheckpointRepository` backed by PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, market: MarketRef, stream: str = "borrowers") -> int | None:
        """Last indexed block of `stream`, or `None` if it was never indexed."""
        async with self._sessions() as session:
            row = await session.get(
                IndexerCheckpointRow, (market.chain, market.protocol, market.address, stream)
            )
            return None if row is None else row.last_block

    async def set(self, market: MarketRef, block: int, stream: str = "borrowers") -> None:
        """Upsert the checkpoint of `stream`."""
        stmt = insert(IndexerCheckpointRow).values(
            chain=market.chain,
            protocol=market.protocol,
            market=market.address,
            stream=stream,
            last_block=block,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "protocol", "market", "stream"],
            set_={"last_block": stmt.excluded.last_block, "updated_at": func.now()},
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)


class SqlOpportunityRepository:
    """`IOpportunityRepository` backed by PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, summary: OpportunitySummary) -> int:
        """Persist and return the new id."""
        row = OpportunityRow(
            chain=summary.chain,
            protocol=summary.protocol,
            market_name=summary.market_name,
            market_address=summary.market_address,
            borrower=summary.borrower,
            health_factor=min(summary.health_factor, MAX_STORED_HF),
            debt_symbol=summary.debt_symbol[:32],
            debt_to_cover=summary.debt_to_cover,
            debt_to_cover_usd=summary.debt_to_cover_usd,
            collateral_symbol=summary.collateral_symbol[:32],
            collateral_seized_usd=summary.collateral_seized_usd,
            total_debt_usd=summary.total_debt_usd,
            total_collateral_usd=summary.total_collateral_usd,
            bonus_pct=summary.bonus_pct,
            gross_profit_usd=summary.gross_profit_usd,
            gas_cost_usd=summary.gas_cost_usd,
            net_profit_usd=summary.net_profit_usd,
            block_number=summary.block_number,
            detected_at=summary.detected_at,
            notes=list(summary.notes),
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
            return row.id

    async def get(self, opportunity_id: int) -> OpportunitySummary:
        """Load an opportunity.

        Raises:
            NotFoundError: if it does not exist.
        """
        async with self._sessions() as session:
            row = await session.get(OpportunityRow, opportunity_id)
        if row is None:
            raise NotFoundError(f"opportunity {opportunity_id} not found")
        return OpportunitySummary(
            chain=row.chain,
            protocol=row.protocol,
            market_name=row.market_name,
            market_address=row.market_address,
            borrower=row.borrower,
            health_factor=row.health_factor,
            debt_symbol=row.debt_symbol,
            debt_to_cover=row.debt_to_cover,
            debt_to_cover_usd=row.debt_to_cover_usd,
            collateral_symbol=row.collateral_symbol,
            collateral_seized_usd=row.collateral_seized_usd,
            total_debt_usd=row.total_debt_usd,
            total_collateral_usd=row.total_collateral_usd,
            bonus_pct=row.bonus_pct,
            gross_profit_usd=row.gross_profit_usd,
            gas_cost_usd=row.gas_cost_usd,
            net_profit_usd=row.net_profit_usd,
            block_number=row.block_number,
            detected_at=row.detected_at,
            notes=tuple(row.notes or ()),
            id=row.id,
        )


class SqlNotificationRepository:
    """`INotificationRepository` backed by PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self, opportunity_id: int | None, channel: str, status: str, error: str | None = None
    ) -> None:
        """Append a delivery record."""
        async with self._sessions.begin() as session:
            session.add(
                NotificationRow(
                    opportunity_id=opportunity_id, channel=channel, status=status, error=error
                )
            )


class SqlStatusRepository:
    """`IStatusRepository`: aggregate queries for the status report (read-only)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def market_stats(self, hf_threshold: Decimal) -> dict[str, MarketStats]:
        """Active borrowers, total debt, at-risk count and min HF per market."""
        at_risk = func.count().filter(BorrowerRow.health_factor < hf_threshold)
        stmt = (
            select(
                BorrowerRow.chain,
                BorrowerRow.protocol,
                BorrowerRow.market,
                func.count(),
                func.coalesce(func.sum(BorrowerRow.debt_usd), 0),
                at_risk,
                func.min(BorrowerRow.health_factor),
            )
            .where(BorrowerRow.active)
            .group_by(BorrowerRow.chain, BorrowerRow.protocol, BorrowerRow.market)
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).all()
        return {
            MarketRef(chain, protocol, market, "").key: MarketStats(
                active_borrowers=int(count),
                total_debt_usd=Decimal(debt or 0),
                at_risk=int(risky),
                min_health_factor=None if min_hf is None else Decimal(min_hf),
            )
            for chain, protocol, market, count, debt, risky, min_hf in rows
        }

    async def activity_since(self, since: datetime) -> ActivityStats:
        """Opportunities detected and opportunity alerts delivered since `since`."""
        opportunities = select(func.count(), func.max(OpportunityRow.net_profit_usd)).where(
            OpportunityRow.detected_at >= since
        )
        alerts = (
            select(NotificationRow.status, func.count())
            .where(
                NotificationRow.created_at >= since,
                NotificationRow.opportunity_id.is_not(None),
                NotificationRow.status.in_(("sent", "failed")),
            )
            .group_by(NotificationRow.status)
        )
        events = LiquidationEventRow
        market = select(
            func.count().filter(events.kind.in_(("aave_liquidation", "comet_absorb"))),
            func.coalesce(
                func.sum(events.gross_profit_usd).filter(
                    events.kind.in_(("aave_liquidation", "comet_buy"))
                ),
                0,
            ),
            func.count().filter(
                events.kind.in_(("aave_liquidation", "comet_absorb")), events.our_lead_seconds > 0
            ),
        ).where(events.block_time >= since)
        async with self._sessions() as session:
            count, best = (await session.execute(opportunities)).one()
            by_status = {status: int(n) for status, n in (await session.execute(alerts)).all()}
            liquidations, gross, seen_first = (await session.execute(market)).one()
        return ActivityStats(
            opportunities=int(count),
            best_net_profit_usd=None if best is None else Decimal(best),
            alerts_sent=by_status.get("sent", 0),
            alerts_failed=by_status.get("failed", 0),
            market_liquidations=int(liquidations),
            market_gross_usd=Decimal(gross or 0),
            market_seen_first=int(seen_first),
        )


class SqlLiquidationEventRepository:
    """`ILiquidationEventRepository` backed by PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, events: Sequence[LiquidationEvent]) -> int:
        """Insert new events; duplicates (re-indexed ranges) are ignored."""
        if not events:
            return 0
        rows = [
            {
                "chain": e.market.chain,
                "protocol": e.market.protocol,
                "market": e.market.address,
                "market_name": e.market.name,
                "kind": e.kind,
                "tx_hash": e.tx_hash,
                "log_index": e.log_index,
                "block_number": e.block_number,
                "borrower": e.borrower,
                "liquidator": e.liquidator,
                "debt_asset": e.debt_asset,
                "collateral_asset": e.collateral_asset,
                "debt_amount": Decimal(e.debt_amount),
                "collateral_amount": Decimal(e.collateral_amount),
                "usd_value_hint": e.usd_value_hint,
            }
            for e in events
        ]
        inserted = 0
        try:
            async with self._sessions.begin() as session:
                # asyncpg allows 32767 bind parameters per statement: insert in batches.
                for i in range(0, len(rows), UPSERT_BATCH):
                    stmt = (
                        insert(LiquidationEventRow)
                        .values(rows[i : i + UPSERT_BATCH])
                        .on_conflict_do_nothing(constraint="uq_liquidation_events_log")
                        .returning(LiquidationEventRow.id)
                    )
                    inserted += len((await session.execute(stmt)).all())
        except SQLAlchemyError as exc:
            raise RepositoryError(f"liquidation event insert failed: {type(exc).__name__}") from exc
        return inserted

    async def pending(
        self, limit: int, max_attempts: int
    ) -> list[tuple[int, int, LiquidationEvent]]:
        """Oldest events that still need enrichment."""
        stmt = (
            select(LiquidationEventRow)
            .where(
                LiquidationEventRow.enriched.is_(False),
                LiquidationEventRow.enrich_attempts < max_attempts,
            )
            .order_by(LiquidationEventRow.block_number)
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = (await session.scalars(stmt)).all()
        return [
            (
                row.id,
                row.enrich_attempts,
                LiquidationEvent(
                    market=MarketRef(row.chain, row.protocol, row.market, row.market_name),
                    kind=row.kind,
                    tx_hash=row.tx_hash,
                    log_index=row.log_index,
                    block_number=row.block_number,
                    borrower=row.borrower,
                    liquidator=row.liquidator,
                    debt_asset=row.debt_asset,
                    collateral_asset=row.collateral_asset,
                    debt_amount=int(row.debt_amount),
                    collateral_amount=int(row.collateral_amount),
                    usd_value_hint=row.usd_value_hint,
                ),
            )
            for row in rows
        ]

    async def previous_liquidation_block(
        self, market: MarketRef, borrower: str, before_block: int, since_block: int
    ) -> int | None:
        """Latest earlier liquidation/absorb of the same borrower in the window."""
        stmt = select(func.max(LiquidationEventRow.block_number)).where(
            LiquidationEventRow.chain == market.chain,
            LiquidationEventRow.protocol == market.protocol,
            LiquidationEventRow.market == market.address,
            LiquidationEventRow.borrower == borrower,
            LiquidationEventRow.kind.in_(("aave_liquidation", "comet_absorb")),
            LiquidationEventRow.block_number < before_block,
            LiquidationEventRow.block_number >= since_block,
        )
        async with self._sessions() as session:
            result: int | None = await session.scalar(stmt)
            return result

    async def events_in_tx(self, chain: str, tx_hash: str) -> int:
        """Count of recorded events in one transaction."""
        stmt = select(func.count()).where(
            LiquidationEventRow.chain == chain, LiquidationEventRow.tx_hash == tx_hash
        )
        async with self._sessions() as session:
            return int(await session.scalar(stmt) or 0)

    async def save_enrichment(self, event_id: int, enrichment: EventEnrichment) -> None:
        """Store enrichment results and mark the event done."""
        receipt = enrichment.receipt
        valuation = enrichment.valuation
        values: dict[str, object] = {
            "enriched": True,
            "enrich_error": None,
            "block_time": enrichment.block_time,
            "tx_from": receipt.tx_from,
            "tx_to": receipt.tx_to,
            "tx_index": receipt.tx_index,
            "tx_status": receipt.status,
            "gas_used": receipt.gas_used,
            "effective_gas_price": Decimal(receipt.effective_gas_price),
            "l1_fee": Decimal(receipt.l1_fee),
            "debt_symbol": (enrichment.debt_symbol or "")[:32] or None,
            "collateral_symbol": (enrichment.collateral_symbol or "")[:32] or None,
            "debt_usd": valuation.debt_usd if valuation else None,
            "collateral_usd": valuation.collateral_usd if valuation else None,
            "gross_profit_usd": valuation.gross_profit_usd if valuation else None,
            "gas_cost_usd": enrichment.gas_cost_usd,
            "net_profit_usd": enrichment.net_profit_usd,
            "liquidatable_since_block": enrichment.liquidatable_since_block,
            "lag_blocks": enrichment.lag_blocks,
            "lag_capped": enrichment.lag_capped,
            "our_first_seen_at": enrichment.our_first_seen_at,
            "our_lead_seconds": enrichment.our_lead_seconds,
            "bot_online": enrichment.bot_online,
            "borrower_known": enrichment.borrower_known,
            "follow_up": enrichment.follow_up,
            "data_gaps": enrichment.data_gaps,
        }
        stmt = update(LiquidationEventRow).where(LiquidationEventRow.id == event_id).values(values)
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def mark_failed(self, event_id: int, error: str) -> None:
        """Record a failed enrichment attempt (retried until `max_attempts`)."""
        stmt = (
            update(LiquidationEventRow)
            .where(LiquidationEventRow.id == event_id)
            .values(
                enrich_attempts=LiquidationEventRow.enrich_attempts + 1, enrich_error=error[:500]
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def borrower_known(self, market: MarketRef, borrower: str, before_block: int) -> bool:
        """True if the borrower was discovered by our indexer before `before_block`."""
        stmt = select(func.count()).where(
            *_market_filter(market),
            BorrowerRow.address == borrower,
            BorrowerRow.first_seen_block < before_block,
        )
        async with self._sessions() as session:
            return bool(await session.scalar(stmt))


class SqlSightingRepository:
    """`ISightingRepository` backed by PostgreSQL.

    A partial unique index guarantees at most one open episode per borrower and market, so
    concurrent scanners (watcher + Celery workers) upsert into the same row.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self, market: MarketRef, sightings: Sequence[Sighting], healthy: frozenset[str]
    ) -> None:
        """Open or extend episodes; close the open episodes of borrowers now healthy."""
        if not sightings and not healthy:
            return
        try:
            await self._record(market, sightings, healthy)
        except SQLAlchemyError as exc:
            raise RepositoryError(f"sighting record failed: {type(exc).__name__}") from exc

    async def _record(
        self, market: MarketRef, sightings: Sequence[Sighting], healthy: frozenset[str]
    ) -> None:
        now = datetime.now(UTC)
        market_cols = (
            SightingRow.chain == market.chain,
            SightingRow.protocol == market.protocol,
            SightingRow.market == market.address,
        )
        async with self._sessions.begin() as session:
            if healthy:
                open_borrowers = set(
                    (
                        await session.scalars(
                            select(SightingRow.borrower).where(
                                *market_cols, SightingRow.closed.is_(False)
                            )
                        )
                    ).all()
                )
                to_close = sorted(open_borrowers & healthy)
                if to_close:
                    await session.execute(
                        update(SightingRow)
                        .where(
                            *market_cols,
                            SightingRow.closed.is_(False),
                            SightingRow.borrower.in_(to_close),
                        )
                        .values(closed=True, closed_reason="healthy")
                    )
            if not sightings:
                return
            rows = [
                {
                    "chain": market.chain,
                    "protocol": market.protocol,
                    "market": market.address,
                    "market_name": market.name,
                    "borrower": s.borrower,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "observations": 1,
                    "min_health_factor": _clamp_hf(s.health_factor),
                    "max_debt_usd": s.debt_usd,
                    "best_net_profit_usd": s.est_net_profit_usd,
                    "best_gross_profit_usd": s.est_gross_profit_usd,
                    "closed": False,
                }
                for s in {s.borrower: s for s in sightings}.values()
            ]
            base = insert(SightingRow).values(rows)
            current = SightingRow.__table__.c
            stmt = base.on_conflict_do_update(
                index_elements=["chain", "protocol", "market", "borrower"],
                index_where=text("NOT closed"),
                set_={
                    "last_seen_at": base.excluded.last_seen_at,
                    "observations": current.observations + 1,
                    # PostgreSQL LEAST/GREATEST ignore NULLs.
                    "min_health_factor": func.least(
                        current.min_health_factor, base.excluded.min_health_factor
                    ),
                    "max_debt_usd": func.greatest(current.max_debt_usd, base.excluded.max_debt_usd),
                    "best_net_profit_usd": func.greatest(
                        current.best_net_profit_usd, base.excluded.best_net_profit_usd
                    ),
                    "best_gross_profit_usd": func.greatest(
                        current.best_gross_profit_usd, base.excluded.best_gross_profit_usd
                    ),
                },
            )
            await session.execute(stmt)

    async def first_seen(
        self, market: MarketRef, borrower: str, at: datetime, gap_seconds: int
    ) -> datetime | None:
        """Start of the episode that was ongoing at `at` (open, or seen within the gap)."""
        stmt = (
            select(SightingRow.first_seen_at)
            .where(
                SightingRow.chain == market.chain,
                SightingRow.protocol == market.protocol,
                SightingRow.market == market.address,
                SightingRow.borrower == borrower,
                SightingRow.first_seen_at <= at,
                or_(
                    SightingRow.closed.is_(False),
                    SightingRow.closed_reason == "liquidated",
                    SightingRow.last_seen_at >= at - timedelta(seconds=gap_seconds),
                ),
            )
            .order_by(SightingRow.first_seen_at.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            result: datetime | None = await session.scalar(stmt)
            return result

    async def close(
        self, market: MarketRef, borrower: str, reason: str, started_before: datetime
    ) -> None:
        """Close the open episode of a borrower that started before `started_before`."""
        stmt = (
            update(SightingRow)
            .where(
                SightingRow.chain == market.chain,
                SightingRow.protocol == market.protocol,
                SightingRow.market == market.address,
                SightingRow.borrower == borrower,
                SightingRow.closed.is_(False),
                SightingRow.first_seen_at <= started_before,
            )
            .values(closed=True, closed_reason=reason)
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)


class SqlUptimeRepository:
    """`IUptimeRepository`: minute buckets in which the watcher processed blocks."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def mark(self, chain: str, minute: datetime) -> None:
        """Record that the watcher was alive on `chain` during `minute` (idempotent)."""
        bucket = minute.replace(second=0, microsecond=0)
        stmt = insert(UptimeRow).values(chain=chain, minute=bucket).on_conflict_do_nothing()
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def was_online(self, chain: str, at: datetime) -> bool:
        """True if the watcher was alive on `chain` in the minute of `at` or the one before."""
        bucket = at.replace(second=0, microsecond=0)
        stmt = select(func.count()).where(
            UptimeRow.chain == chain,
            UptimeRow.minute.in_((bucket, bucket - timedelta(minutes=1))),
        )
        async with self._sessions() as session:
            return bool(await session.scalar(stmt))


def _clamp_hf(value: Decimal | None) -> Decimal | None:
    return None if value is None else min(value, MAX_STORED_HF)


def _max_optional(current: Decimal | None, new: Decimal | None) -> Decimal | None:
    if new is None:
        return current
    return new if current is None or new > current else current
