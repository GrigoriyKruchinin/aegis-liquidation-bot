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
