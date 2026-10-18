"""Abstract interfaces. High-level modules depend only on these (DIP)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aegis.config.chains import ChainConfig
from aegis.core.models import (
    ActivityStats,
    Call,
    CallResult,
    EventEnrichment,
    EventValuation,
    GasEstimate,
    HealthCheck,
    LiquidationEvent,
    LiquidationOpportunity,
    LiquidationQuote,
    LogEntry,
    LogFilter,
    MarketRef,
    MarketStats,
    OpportunitySummary,
    Position,
    ProfitEstimate,
    Sighting,
    TxReceiptInfo,
    TxTemplate,
)

BlockId = int | str


class BaseChainProvider(ABC):
    """Read-only access to one EVM chain."""

    def __init__(self, config: ChainConfig) -> None:
        self._config = config

    @property
    def config(self) -> ChainConfig:
        """Static chain configuration."""
        return self._config

    @property
    def name(self) -> str:
        """Chain name (registry key)."""
        return self._config.name

    @abstractmethod
    async def block_number(self) -> int:
        """Latest block number."""

    @abstractmethod
    async def gas_price(self) -> int:
        """Current L2 gas price in wei."""

    @abstractmethod
    async def call(self, to: str, data: bytes, block: BlockId = "latest") -> bytes:
        """Execute `eth_call` and return raw return data."""

    @abstractmethod
    async def multicall(self, calls: Sequence[Call], block: BlockId = "latest") -> list[CallResult]:
        """Execute many calls via Multicall3, preserving order."""

    @abstractmethod
    async def get_logs(
        self, log_filter: LogFilter, from_block: int, to_block: int
    ) -> list[LogEntry]:
        """Fetch logs for an inclusive block range."""

    @abstractmethod
    async def l1_data_fee(self, tx: TxTemplate) -> int:
        """L1 data-availability fee in wei for a transaction (0 on chains without one)."""

    @abstractmethod
    async def transaction_receipt(self, tx_hash: str) -> TxReceiptInfo:
        """Receipt details of a mined transaction, including its block timestamp."""

    async def aclose(self) -> None:  # noqa: B027  # optional hook
        """Release network resources."""


class BaseLendingProtocol(ABC):
    """Adapter for one lending market on one chain (Strategy pattern)."""

    #: Registry key, e.g. ``aave_v3``.
    protocol_name: str
    #: How long static market metadata stays valid before `ensure_initialized` reloads it.
    metadata_ttl_seconds: float = 600.0

    def __init__(self, provider: BaseChainProvider, market: MarketRef) -> None:
        self._provider = provider
        self._market = market
        self._loaded_at: float | None = None

    async def ensure_initialized(self) -> None:
        """Load metadata on first use and refresh it after `metadata_ttl_seconds`."""
        now = time.monotonic()
        if self._loaded_at is None or now - self._loaded_at > self.metadata_ttl_seconds:
            await self.initialize()
            self._loaded_at = now

    @property
    def market(self) -> MarketRef:
        """Market identity."""
        return self._market

    @property
    def provider(self) -> BaseChainProvider:
        """Chain provider used by this adapter."""
        return self._provider

    @abstractmethod
    async def initialize(self) -> None:
        """Load/refresh static market metadata (reserves, assets, factors)."""

    @abstractmethod
    def discovery_filter(self) -> LogFilter:
        """Event filter whose logs reveal accounts that may hold debt."""

    @abstractmethod
    def borrower_from_log(self, log: LogEntry) -> str | None:
        """Extract the borrower address from a discovery log."""

    @abstractmethod
    async def check_health(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[HealthCheck]:
        """Batched, cheap health check of many borrowers."""

    @abstractmethod
    async def load_positions(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[Position]:
        """Load full position snapshots (only for liquidatable/near-liquidatable accounts)."""

    @abstractmethod
    def quote_liquidations(self, position: Position) -> list[LiquidationQuote]:
        """Compute self-funded liquidation quotes for every viable debt/collateral pair."""

    def price_calls(self) -> list[Call]:
        """Calls reading every price this market uses (batched chain-wide by the watcher).

        Adapters that do not support price-move triggers return an empty list.
        """
        return []

    def parse_prices(self, results: Sequence[CallResult]) -> tuple[int, ...]:
        """Decode `price_calls` results into raw prices (0 for unreadable entries)."""
        return ()

    # --- Market measurement (optional; adapters that do not support it keep the defaults) ----

    def liquidation_filter(self) -> LogFilter | None:
        """Event filter matching this market's liquidation activity."""
        return None

    def decode_liquidation(self, log: LogEntry) -> LiquidationEvent | None:
        """Decode one liquidation-related log."""
        return None

    async def value_liquidation(self, event: LiquidationEvent) -> EventValuation | None:
        """USD value of the event at its own block (historical oracle prices)."""
        return None

    async def is_liquidatable_at(self, borrower: str, block: int) -> bool | None:
        """Whether `borrower` was liquidatable at the end of `block` (`None` = unsupported)."""
        return None

    def asset_symbol(self, address: str | None) -> str | None:
        """Symbol of a market asset, if known."""
        return None

    def gas_bearing_topics(self) -> frozenset[bytes]:
        """topic0 of events that each represent one paid liquidation action (to split gas)."""
        return frozenset()


class IPriceOracle(Protocol):
    """Native token USD price source used for gas costing."""

    async def native_price_usd(self) -> Decimal: ...

    async def native_price_usd_at(self, block: int) -> Decimal:
        """Price as of `block` (historical analysis; needs archive state)."""
        ...


class IGasEstimator(Protocol):
    """Estimates total transaction cost (L2 execution + L1 data fee)."""

    async def estimate(self, tx: TxTemplate, gas_units: int) -> GasEstimate: ...


class IProfitEstimator(Protocol):
    """Selects the most profitable quote for a position."""

    async def estimate(
        self, protocol: BaseLendingProtocol, position: Position
    ) -> ProfitEstimate | None: ...


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Outcome of one indexer run."""

    from_block: int
    to_block: int
    discovered: int
    caught_up: bool


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of one health-factor scan.

    Attributes:
        liquidatable_checks: Every liquidatable account above the dust filter (whether or not it
            is profitable), used to record sightings for market analysis.
        healthy: Accounts checked and found not liquidatable.
    """

    checked: int
    liquidatable: int
    opportunities: tuple[LiquidationOpportunity, ...]
    liquidatable_checks: tuple[HealthCheck, ...] = ()
    healthy: frozenset[str] = frozenset()


class IBorrowerIndexer(Protocol):
    """Discovers borrowers from on-chain events."""

    async def sync(self, protocol: BaseLendingProtocol) -> IndexResult: ...


class IHealthFactorScanner(Protocol):
    """Checks borrower health and produces liquidation opportunities."""

    async def scan(self, protocol: BaseLendingProtocol, borrowers: Sequence[str]) -> ScanResult: ...


class INotifier(Protocol):
    """Delivers pre-rendered messages to a channel."""

    async def send(self, text: str) -> None: ...


class INotificationDispatcher(Protocol):
    """Schedules delivery of a persisted opportunity notification."""

    async def dispatch(self, opportunity: OpportunitySummary) -> None: ...


class IAlertDeduplicator(Protocol):
    """Prevents repeated alerts for the same position within a TTL."""

    async def acquire(self, key: str) -> bool: ...

    async def release(self, key: str) -> None: ...


class IBorrowerRepository(Protocol):
    """Persistence of discovered borrowers and their latest health snapshot."""

    async def upsert_discovered(self, market: MarketRef, borrowers: Mapping[str, int]) -> int: ...

    async def list_active(self, market: MarketRef) -> list[str]: ...

    async def list_watchlist(self, market: MarketRef, hf_threshold: Decimal) -> list[str]: ...

    async def update_health(
        self, market: MarketRef, checks: Sequence[HealthCheck], min_debt_usd: Decimal
    ) -> None: ...


class ICheckpointRepository(Protocol):
    """Last indexed block per market and event stream."""

    async def get(self, market: MarketRef, stream: str = "borrowers") -> int | None: ...

    async def set(self, market: MarketRef, block: int, stream: str = "borrowers") -> None: ...


class IOpportunityRepository(Protocol):
    """Persistence of detected opportunities."""

    async def add(self, summary: OpportunitySummary) -> int: ...

    async def get(self, opportunity_id: int) -> OpportunitySummary: ...


class INotificationRepository(Protocol):
    """Audit log of sent / failed notifications."""

    async def record(
        self, opportunity_id: int | None, channel: str, status: str, error: str | None = None
    ) -> None: ...


class IStatusRepository(Protocol):
    """Read-only aggregates for the periodic status report."""

    async def market_stats(self, hf_threshold: Decimal) -> dict[str, MarketStats]:
        """Stats keyed by `MarketRef.key`."""
        ...

    async def activity_since(self, since: datetime) -> ActivityStats: ...


class IRateLimiter(Protocol):
    """Shared RPC throughput budget (compute units per second) per provider bucket."""

    async def acquire(self, bucket: str, cost: int, *, high_priority: bool) -> None:
        """Wait until `cost` units may be spent from `bucket`.

        Low-priority callers (indexer, scheduled scans) may only use the budget above a reserve
        kept for high-priority callers (the latency-critical watcher).
        """
        ...


class ISightingRepository(Protocol):
    """Episodes during which our bot saw a borrower liquidatable."""

    async def record(
        self, market: MarketRef, sightings: Sequence[Sighting], healthy: frozenset[str]
    ) -> None:
        """Open/extend episodes for `sightings`; close open episodes of `healthy` borrowers."""
        ...

    async def first_seen(
        self, market: MarketRef, borrower: str, at: datetime, gap_seconds: int
    ) -> datetime | None:
        """Start of our episode that was still ongoing at `at`.

        An episode counts if it started before `at` and was still open, or was last seen no
        more than `gap_seconds` before `at`.
        """
        ...

    async def close(
        self, market: MarketRef, borrower: str, reason: str, started_before: datetime
    ) -> None:
        """Close the open episode of `borrower` if it started before `started_before`."""
        ...


class IUptimeRepository(Protocol):
    """Minute buckets in which the watcher was processing blocks."""

    async def mark(self, chain: str, minute: datetime) -> None: ...

    async def was_online(self, chain: str, at: datetime) -> bool: ...


class ILiquidationEventRepository(Protocol):
    """Raw and enriched liquidation events (market / competitor analysis)."""

    async def add(self, events: Sequence[LiquidationEvent]) -> int:
        """Insert new events (idempotent per tx/log); returns how many were new."""
        ...

    async def pending(
        self, limit: int, max_attempts: int
    ) -> list[tuple[int, int, LiquidationEvent]]:
        """Events not enriched yet as ``(id, attempts so far, event)``, oldest first."""
        ...

    async def previous_liquidation_block(
        self, market: MarketRef, borrower: str, before_block: int, since_block: int
    ) -> int | None:
        """Block of the latest earlier liquidation of `borrower` in ``[since, before)``."""
        ...

    async def events_in_tx(self, chain: str, tx_hash: str) -> int:
        """Number of recorded events sharing one transaction (to split its gas)."""
        ...

    async def save_enrichment(self, event_id: int, enrichment: EventEnrichment) -> None: ...

    async def mark_failed(self, event_id: int, error: str) -> None: ...

    async def borrower_known(self, market: MarketRef, borrower: str, before_block: int) -> bool:
        """Whether our indexer had discovered the borrower before `before_block`."""
        ...
