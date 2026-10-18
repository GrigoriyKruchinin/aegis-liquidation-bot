"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

USD = Numeric(38, 8)
RATIO = Numeric(38, 18)
TOKEN_AMOUNT = Numeric(78, 18)


class Base(DeclarativeBase):
    """Declarative base."""


class BorrowerRow(Base):
    """A discovered account with its latest health snapshot."""

    __tablename__ = "borrowers"
    __table_args__ = (
        UniqueConstraint("chain", "protocol", "market", "address", name="uq_borrowers_market_addr"),
        Index("ix_borrowers_market_active", "chain", "protocol", "market", "active"),
        Index("ix_borrowers_market_hf", "chain", "protocol", "market", "health_factor"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(42))
    address: Mapped[str] = mapped_column(String(42))
    first_seen_block: Mapped[int] = mapped_column(BigInteger)
    last_seen_block: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    health_factor: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    debt_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    collateral_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexerCheckpointRow(Base):
    """Last indexed block per market."""

    __tablename__ = "indexer_checkpoints"

    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(32), primary_key=True)
    market: Mapped[str] = mapped_column(String(42), primary_key=True)
    #: Independent event streams per market (e.g. ``borrowers``, ``liquidations``).
    stream: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="borrowers", server_default="borrowers"
    )
    last_block: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OpportunityRow(Base):
    """A detected liquidation opportunity (above the profit threshold)."""

    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opportunities_detected_at", "detected_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(32))
    market_name: Mapped[str] = mapped_column(String(64))
    market_address: Mapped[str] = mapped_column(String(42))
    borrower: Mapped[str] = mapped_column(String(42), index=True)
    health_factor: Mapped[Decimal] = mapped_column(RATIO)
    debt_symbol: Mapped[str] = mapped_column(String(32))
    debt_to_cover: Mapped[Decimal] = mapped_column(TOKEN_AMOUNT)
    debt_to_cover_usd: Mapped[Decimal] = mapped_column(USD)
    collateral_symbol: Mapped[str] = mapped_column(String(32))
    collateral_seized_usd: Mapped[Decimal] = mapped_column(USD)
    total_debt_usd: Mapped[Decimal] = mapped_column(USD)
    total_collateral_usd: Mapped[Decimal] = mapped_column(USD)
    bonus_pct: Mapped[Decimal] = mapped_column(RATIO)
    gross_profit_usd: Mapped[Decimal] = mapped_column(USD)
    gas_cost_usd: Mapped[Decimal] = mapped_column(USD)
    net_profit_usd: Mapped[Decimal] = mapped_column(USD)
    block_number: Mapped[int] = mapped_column(BigInteger)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)


class NotificationRow(Base):
    """Delivery audit log."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int | None] = mapped_column(
        ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LiquidationEventRow(Base):
    """On-chain liquidation activity (ours to measure the market and competitors)."""

    __tablename__ = "liquidation_events"
    __table_args__ = (
        UniqueConstraint("chain", "tx_hash", "log_index", name="uq_liquidation_events_log"),
        Index("ix_liquidation_events_block_time", "block_time"),
        Index("ix_liquidation_events_enriched", "enriched"),
        Index("ix_liquidation_events_market_borrower", "chain", "protocol", "market", "borrower"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(42))
    market_name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    tx_hash: Mapped[str] = mapped_column(String(66))
    log_index: Mapped[int] = mapped_column(BigInteger)
    block_number: Mapped[int] = mapped_column(BigInteger)
    borrower: Mapped[str | None] = mapped_column(String(42), nullable=True)
    liquidator: Mapped[str] = mapped_column(String(42), index=True)
    debt_asset: Mapped[str | None] = mapped_column(String(42), nullable=True)
    collateral_asset: Mapped[str | None] = mapped_column(String(42), nullable=True)
    debt_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    collateral_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    debt_amount: Mapped[Decimal] = mapped_column(Numeric(78, 0))
    collateral_amount: Mapped[Decimal] = mapped_column(Numeric(78, 0))
    usd_value_hint: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # --- enrichment ------------------------------------------------------------------------
    enriched: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    enrich_attempts: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    enrich_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tx_from: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    tx_to: Mapped[str | None] = mapped_column(String(42), nullable=True)
    tx_index: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tx_status: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gas_used: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    effective_gas_price: Mapped[Decimal | None] = mapped_column(Numeric(78, 0), nullable=True)
    l1_fee: Mapped[Decimal | None] = mapped_column(Numeric(78, 0), nullable=True)
    debt_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    collateral_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    gross_profit_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    gas_cost_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    net_profit_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    #: First block of the uninterrupted liquidatable window that ended with this liquidation.
    liquidatable_since_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Blocks between becoming liquidatable and being liquidated (0 = same block).
    lag_blocks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: True when the position was already liquidatable at the start of the search window.
    lag_capped: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Another liquidation of the same borrower happened shortly before (partial liquidation
    #: series): excluded from competitor-speed statistics.
    follow_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Why USD value / lag are missing ("no_archive", "dust", ...), if they are.
    data_gaps: Mapped[str | None] = mapped_column(String(64), nullable=True)
    our_first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    our_lead_seconds: Mapped[Decimal | None] = mapped_column(Numeric(20, 3), nullable=True)
    bot_online: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    borrower_known: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class SightingRow(Base):
    """An episode during which our bot saw a borrower liquidatable."""

    __tablename__ = "sightings"
    __table_args__ = (
        # At most one open episode per borrower and market (concurrent scanners upsert into it).
        Index(
            "uq_sightings_open",
            "chain",
            "protocol",
            "market",
            "borrower",
            unique=True,
            postgresql_where=text("NOT closed"),
        ),
        Index("ix_sightings_borrower", "chain", "protocol", "market", "borrower", "first_seen_at"),
        Index("ix_sightings_first_seen", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(42))
    market_name: Mapped[str] = mapped_column(String(64))
    borrower: Mapped[str] = mapped_column(String(42))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observations: Mapped[int] = mapped_column(BigInteger, default=1)
    min_health_factor: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    max_debt_usd: Mapped[Decimal] = mapped_column(USD)
    best_net_profit_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    best_gross_profit_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    closed_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)


class UptimeRow(Base):
    """One row per chain per minute in which the watcher processed at least one block."""

    __tablename__ = "uptime_minutes"

    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
