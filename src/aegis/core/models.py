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
    """Event filter. A tuple in a topic position means "any of" (OR)."""

    address: str
    topics: tuple[bytes | tuple[bytes, ...] | None, ...]


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
    """Unsigned call used for gas and L1 data-fee estimation (never sent)."""

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


@dataclass(frozen=True, slots=True)
class MarketStats:
    """Aggregated borrower snapshot of one market."""

    active_borrowers: int
    total_debt_usd: Decimal
    at_risk: int
    min_health_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class MarketStatus:
    """One row of the periodic status report.

    Attributes:
        index_lag_seconds: How far the indexer is behind the chain head (`None` = not started).
    """

    chain: str
    market_name: str
    stats: MarketStats
    index_lag_seconds: int | None


@dataclass(frozen=True, slots=True)
class ActivityStats:
    """Opportunities and alerts within the report period."""

    opportunities: int
    best_net_profit_usd: Decimal | None
    alerts_sent: int
    alerts_failed: int
    #: Liquidations executed by anyone in the period (Aave liquidationCall + Comet absorb).
    market_liquidations: int = 0
    #: Profit realised by liquidators in the period (Aave + Comet buyCollateral, before gas).
    market_gross_usd: Decimal = Decimal(0)
    #: Of `market_liquidations`, how many the detector had seen liquidatable beforehand.
    market_seen_first: int = 0


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Periodic "bot is alive" summary."""

    generated_at: datetime
    period_hours: int
    markets: tuple[MarketStatus, ...]
    activity: ActivityStats
    last_scan_age_seconds: int | None


#: Kinds of on-chain liquidation activity recorded for market analysis.
LIQUIDATION_KINDS = ("aave_liquidation", "comet_absorb", "comet_absorb_collateral", "comet_buy")


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    """A liquidation-related event decoded from a protocol log (raw, before enrichment).

    Attributes:
        kind: One of `LIQUIDATION_KINDS`.
        borrower: Liquidated account (`None` for Comet `BuyCollateral`).
        liquidator: Address credited by the protocol (Aave `liquidator`, Comet absorber/buyer).
        debt_amount: Debt repaid / absorbed / base paid, in base units of `debt_asset`.
        collateral_amount: Collateral seized / absorbed / bought, in base units.
        usd_value_hint: USD value reported by the protocol itself (Comet), if any.
    """

    market: MarketRef
    kind: str
    tx_hash: str
    log_index: int
    block_number: int
    borrower: str | None
    liquidator: str
    debt_asset: str | None
    collateral_asset: str | None
    debt_amount: int
    collateral_amount: int
    usd_value_hint: Decimal | None = None


@dataclass(frozen=True, slots=True)
class EventValuation:
    """USD valuation of a liquidation event at its block."""

    debt_usd: Decimal
    collateral_usd: Decimal

    @property
    def gross_profit_usd(self) -> Decimal:
        """What the liquidator received above what it paid (before gas)."""
        return self.collateral_usd - self.debt_usd


@dataclass(frozen=True, slots=True)
class TxReceiptInfo:
    """Execution details of the liquidating transaction."""

    tx_from: str
    tx_to: str | None
    tx_index: int
    gas_used: int
    effective_gas_price: int
    l1_fee: int
    status: int
    block_timestamp: int
    logs_in_tx: int
    #: topic0 of every log in the transaction (to split gas between liquidations in one tx).
    log_topic0s: tuple[bytes, ...] = ()

    @property
    def total_fee_wei(self) -> int:
        """L2 execution fee plus the OP-stack L1 data fee (Arbitrum folds it into gas)."""
        return self.gas_used * self.effective_gas_price + self.l1_fee


@dataclass(frozen=True, slots=True)
class Sighting:
    """Our bot saw a borrower liquidatable (regardless of the profit threshold)."""

    borrower: str
    health_factor: Decimal | None
    debt_usd: Decimal
    est_net_profit_usd: Decimal | None
    est_gross_profit_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class EventEnrichment:
    """Everything learned about a liquidation event after it was indexed."""

    block_time: datetime
    receipt: TxReceiptInfo
    valuation: EventValuation | None
    gas_cost_usd: Decimal | None
    debt_symbol: str | None
    collateral_symbol: str | None
    liquidatable_since_block: int | None
    lag_blocks: int | None
    lag_capped: bool | None
    our_first_seen_at: datetime | None
    bot_online: bool | None
    borrower_known: bool | None
    follow_up: bool = False
    data_gaps: str | None = None

    @property
    def net_profit_usd(self) -> Decimal | None:
        """Gross profit minus this event's share of the transaction fee."""
        if self.valuation is None or self.gas_cost_usd is None:
            return None
        return self.valuation.gross_profit_usd - self.gas_cost_usd

    @property
    def our_lead_seconds(self) -> Decimal | None:
        """How long before the liquidation the detector first saw the position liquidatable."""
        if self.our_first_seen_at is None:
            return None
        return Decimal(str((self.block_time - self.our_first_seen_at).total_seconds()))


@dataclass(frozen=True, slots=True)
class LiquidationRecord:
    """An enriched liquidation as read back for analytics (digest, alerts)."""

    id: int
    chain: str
    market_name: str
    kind: str
    tx_hash: str
    block_number: int
    block_time: datetime
    liquidator: str
    tx_index: int | None
    debt_symbol: str | None
    collateral_symbol: str | None
    debt_usd: Decimal | None
    gross_profit_usd: Decimal | None
    gas_cost_usd: Decimal | None
    net_profit_usd: Decimal | None
    lag_blocks: int | None
    lag_capped: bool | None
    follow_up: bool


@dataclass(frozen=True, slots=True)
class LiquidatorStats:
    """One liquidator contract's activity within a digest period."""

    liquidator: str
    liquidations: int
    net_profit_usd: Decimal
    chains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChainDigest:
    """Per-chain totals of a digest period (paid liquidations only).

    Attributes:
        volume_usd: Aave debt repaid plus the base asset Comet buyers paid for collateral.
    """

    chain: str
    liquidations: int
    volume_usd: Decimal
    net_profit_usd: Decimal


@dataclass(frozen=True, slots=True)
class MarketDigest:
    """Liquidation market summary for a period (the daily 📈 Telegram digest).

    Attributes:
        meaningful: Liquidations with net profit at or above the digest threshold.
        same_block: Of `meaningful` with a measured lag, how many happened in the block the
            position became liquidatable.
        lag_measured: How many of `meaningful` have a measured (uncapped) lag.
        pending: Events not enriched yet (their values are missing from the totals).
        unpriced: Enriched events without a net profit (pricing gaps), excluded from totals.
    """

    since: datetime
    until: datetime
    chains: tuple[ChainDigest, ...]
    top_liquidations: tuple[LiquidationRecord, ...]
    top_liquidators: tuple[LiquidatorStats, ...]
    meaningful: int
    meaningful_net_usd: Decimal
    same_block: int
    lag_measured: int
    pending: int
    threshold_usd: Decimal
    unpriced: int = 0
