"""Abstract interfaces. High-level modules depend only on these (DIP)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from aegis.config.chains import ChainConfig
from aegis.core.models import (
    Call,
    CallResult,
    GasEstimate,
    HealthCheck,
    LiquidationOpportunity,
    LiquidationQuote,
    LogEntry,
    LogFilter,
    MarketRef,
    OpportunitySummary,
    Position,
    ProfitEstimate,
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


class IPriceOracle(Protocol):
    """Native token USD price source used for gas costing."""

    async def native_price_usd(self) -> Decimal: ...


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
    """Outcome of one health-factor scan."""

    checked: int
    liquidatable: int
    opportunities: tuple[LiquidationOpportunity, ...]


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
    """Last indexed block per market."""

    async def get(self, market: MarketRef) -> int | None: ...

    async def set(self, market: MarketRef, block: int) -> None: ...


class IOpportunityRepository(Protocol):
    """Persistence of detected opportunities."""

    async def add(self, summary: OpportunitySummary) -> int: ...

    async def get(self, opportunity_id: int) -> OpportunitySummary: ...


class INotificationRepository(Protocol):
    """Audit log of sent / failed notifications."""

    async def record(
        self, opportunity_id: int | None, channel: str, status: str, error: str | None = None
    ) -> None: ...
