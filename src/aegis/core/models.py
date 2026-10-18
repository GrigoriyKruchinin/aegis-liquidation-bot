"""Immutable domain models.

Conventions: on-chain amounts are `int` in token base units (wei); USD values are `Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

WAD = 10**18
NATIVE_DECIMALS = 18


def to_units(amount: int, decimals: int) -> Decimal:
    """Convert a base-unit integer amount into a human-readable `Decimal`."""
    return Decimal(amount) / (Decimal(10) ** decimals)


def from_units(value: Decimal, decimals: int) -> int:
    """Convert a human-readable `Decimal` amount into base units, rounding down."""
    return int((value * (Decimal(10) ** decimals)).to_integral_value(rounding="ROUND_FLOOR"))


@dataclass(frozen=True, slots=True)
class Asset:
    """An ERC-20 token."""

    address: str
    symbol: str
    decimals: int


@dataclass(frozen=True, slots=True)
class MarketRef:
    """Identifies one lending market (protocol deployment) on one chain."""

    chain: str
    protocol: str
    address: str
    name: str

    @property
    def key(self) -> str:
        """Stable string key, e.g. ``base:aave_v3:0x...``."""
        return f"{self.chain}:{self.protocol}:{self.address.lower()}"


@dataclass(frozen=True, slots=True)
class Call:
    """A single read-only contract call for Multicall3 batching."""

    target: str
    data: bytes
    allow_failure: bool = True


@dataclass(frozen=True, slots=True)
class CallResult:
    """Result of a batched call."""

    success: bool
    data: bytes


@dataclass(frozen=True, slots=True)
class LogEntry:
    """A decoded-at-envelope-level event log."""

    address: str
    topics: tuple[bytes, ...]
    data: bytes
    block_number: int
    tx_hash: str
    log_index: int


@dataclass(frozen=True, slots=True)
class LogFilter:
    """Event filter used for borrower discovery."""

    address: str
    topics: tuple[bytes | None, ...]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Cheap, batched health snapshot of a borrower.

    Attributes:
        health_factor: `None` when the account has no debt (infinite health factor).
    """

    borrower: str
    health_factor: Decimal | None
    debt_usd: Decimal
    collateral_usd: Decimal
    liquidatable: bool


@dataclass(frozen=True, slots=True)
class CollateralLeg:
    """One collateral asset of a position.

    Attributes:
        liquidation_bonus: Discount earned by the liquidator as a fraction (0.05 = 5%).
        protocol_fee: Share of the bonus kept by the protocol as a fraction (0.1 = 10%).
    """

    asset: Asset
    amount: int
    price_usd: Decimal
    value_usd: Decimal
    liquidation_bonus: Decimal
    protocol_fee: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class DebtLeg:
    """One debt asset of a position."""

    asset: Asset
    amount: int
    price_usd: Decimal
    value_usd: Decimal


@dataclass(frozen=True, slots=True)
class Position:
    """Full position snapshot of a borrower in one market."""

    market: MarketRef
    borrower: str
    health_factor: Decimal
    collateral: tuple[CollateralLeg, ...]
    debt: tuple[DebtLeg, ...]
    block_number: int

    @property
    def total_collateral_usd(self) -> Decimal:
        """Sum of collateral values in USD."""
        return sum((leg.value_usd for leg in self.collateral), Decimal(0))

    @property
    def total_debt_usd(self) -> Decimal:
        """Sum of debt values in USD."""
        return sum((leg.value_usd for leg in self.debt), Decimal(0))


@dataclass(frozen=True, slots=True)
class TxTemplate:
    """Unsigned call used for gas and L1 data-fee estimation (never sent in V1)."""

    to: str
    data: bytes


@dataclass(frozen=True, slots=True)
class LiquidationQuote:
    """Protocol-computed liquidation terms for one debt/collateral pair (self-funded)."""

    debt_asset: Asset
    collateral_asset: Asset
    debt_to_cover: int
    debt_to_cover_usd: Decimal
    collateral_seized_usd: Decimal
    gross_profit_usd: Decimal
    bonus_pct: Decimal
    gas_units: int
    tx: TxTemplate
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GasEstimate:
    """Total transaction cost estimate including the L1 data fee."""

    gas_units: int
    gas_price_wei: int
    l1_fee_wei: int
    native_price_usd: Decimal

    @property
    def total_wei(self) -> int:
        """Execution cost plus L1 data fee in wei."""
        return self.gas_units * self.gas_price_wei + self.l1_fee_wei

    @property
    def cost_usd(self) -> Decimal:
        """Total cost in USD."""
        return to_units(self.total_wei, NATIVE_DECIMALS) * self.native_price_usd


@dataclass(frozen=True, slots=True)
class ProfitEstimate:
    """Economics of the best liquidation quote."""

    quote: LiquidationQuote
    gas: GasEstimate

    @property
    def net_profit_usd(self) -> Decimal:
        """Gross profit minus total gas cost."""
        return self.quote.gross_profit_usd - self.gas.cost_usd


@dataclass(frozen=True, slots=True)
class LiquidationOpportunity:
    """A liquidatable position with its estimated profit."""

    position: Position
    estimate: ProfitEstimate
    detected_at: datetime
    id: int | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class OpportunitySummary:
    """Flat, persistable view of an opportunity used for storage and notifications."""

    chain: str
    protocol: str
    market_name: str
    market_address: str
    borrower: str
    health_factor: Decimal
    debt_symbol: str
    debt_to_cover: Decimal
    debt_to_cover_usd: Decimal
    collateral_symbol: str
    collateral_seized_usd: Decimal
    total_debt_usd: Decimal
    total_collateral_usd: Decimal
    bonus_pct: Decimal
    gross_profit_usd: Decimal
    gas_cost_usd: Decimal
    net_profit_usd: Decimal
    block_number: int
    detected_at: datetime
    notes: tuple[str, ...] = ()
    id: int | None = field(default=None, compare=False)

    @classmethod
    def from_opportunity(cls, opportunity: LiquidationOpportunity) -> OpportunitySummary:
        """Flatten a full opportunity."""
        position = opportunity.position
        quote = opportunity.estimate.quote
        return cls(
            chain=position.market.chain,
            protocol=position.market.protocol,
            market_name=position.market.name,
            market_address=position.market.address,
            borrower=position.borrower,
            health_factor=position.health_factor,
            debt_symbol=quote.debt_asset.symbol,
            debt_to_cover=to_units(quote.debt_to_cover, quote.debt_asset.decimals),
            debt_to_cover_usd=quote.debt_to_cover_usd,
            collateral_symbol=quote.collateral_asset.symbol,
            collateral_seized_usd=quote.collateral_seized_usd,
            total_debt_usd=position.total_debt_usd,
            total_collateral_usd=position.total_collateral_usd,
            bonus_pct=quote.bonus_pct,
            gross_profit_usd=quote.gross_profit_usd,
            gas_cost_usd=opportunity.estimate.gas.cost_usd,
            net_profit_usd=opportunity.estimate.net_profit_usd,
            block_number=position.block_number,
            detected_at=opportunity.detected_at,
            notes=quote.notes,
            id=opportunity.id,
        )
