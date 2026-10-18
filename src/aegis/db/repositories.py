"""SQLAlchemy implementations of the repository interfaces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Table,
    bindparam,
    func,
    literal_column,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import ReturningInsert

from aegis.core.errors import NotFoundError, RepositoryError
from aegis.core.models import HealthCheck, MarketRef, OpportunitySummary
from aegis.db.models import BorrowerRow, IndexerCheckpointRow, NotificationRow, OpportunityRow

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

    async def get(self, market: MarketRef) -> int | None:
        """Last indexed block or `None` if the market was never indexed."""
        async with self._sessions() as session:
            row = await session.get(
                IndexerCheckpointRow, (market.chain, market.protocol, market.address)
            )
            return None if row is None else row.last_block

    async def set(self, market: MarketRef, block: int) -> None:
        """Upsert the checkpoint."""
        stmt = insert(IndexerCheckpointRow).values(
            chain=market.chain, protocol=market.protocol, market=market.address, last_block=block
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "protocol", "market"],
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
