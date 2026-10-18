"""Test doubles implementing the core interfaces."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from aegis.config.chains import BASE, ChainConfig
from aegis.core.errors import ContractCallError
from aegis.core.interfaces import BaseChainProvider, BlockId, IndexResult, ScanResult
from aegis.core.models import (
    Call,
    CallResult,
    GasEstimate,
    HealthCheck,
    LogEntry,
    LogFilter,
    MarketRef,
    OpportunitySummary,
    TxTemplate,
)

Handler = Callable[[bytes], bytes]


class FakeProvider(BaseChainProvider):
    """In-memory chain: `handlers[(target_lower, selector)] -> fn(calldata) -> returndata`."""

    def __init__(self, config: ChainConfig = BASE) -> None:
        super().__init__(config)
        self.handlers: dict[tuple[str, bytes], Handler] = {}
        self.logs: list[LogEntry] = []
        self.head = 1_000_000
        self.gas_price_wei = 1_000_000
        self.l1_fee_wei = 0
        self.get_logs_calls: list[tuple[int, int]] = []
        self.max_log_range: int | None = None
        self.logs_down = False
        self.gas_price_calls = 0
        self.l1_fee_calls = 0

    def on(self, target: str, selector: bytes, handler: Handler) -> None:
        self.handlers[(target.lower(), selector)] = handler

    async def block_number(self) -> int:
        return self.head

    async def gas_price(self) -> int:
        self.gas_price_calls += 1
        return self.gas_price_wei

    async def call(self, to: str, data: bytes, block: BlockId = "latest") -> bytes:
        handler = self.handlers.get((to.lower(), data[:4]))
        if handler is None:
            raise ContractCallError(f"no handler for {to} {data[:4].hex()}")
        return handler(data)

    async def multicall(self, calls: Sequence[Call], block: BlockId = "latest") -> list[CallResult]:
        results = []
        for call in calls:
            try:
                results.append(CallResult(True, await self.call(call.target, call.data, block)))
            except ContractCallError:
                results.append(CallResult(False, b""))
        return results

    async def get_logs(
        self, log_filter: LogFilter, from_block: int, to_block: int
    ) -> list[LogEntry]:
        from aegis.core.errors import LogRangeTooLargeError, RpcError

        self.get_logs_calls.append((from_block, to_block))
        if self.logs_down:
            raise RpcError("all endpoints failed")
        if self.max_log_range is not None and to_block - from_block + 1 > self.max_log_range:
            raise LogRangeTooLargeError("range too large")
        return [
            log
            for log in self.logs
            if from_block <= log.block_number <= to_block
            and log.address.lower() == log_filter.address.lower()
        ]

    async def l1_data_fee(self, tx: TxTemplate) -> int:
        self.l1_fee_calls += 1
        return self.l1_fee_wei


@dataclass
class FakeOracle:
    price: Decimal = Decimal(2000)

    async def native_price_usd(self) -> Decimal:
        return self.price


@dataclass
class FakeGasEstimator:
    cost_wei: int = 0
    native_price: Decimal = Decimal(2000)
    calls: list[int] = field(default_factory=list)

    async def estimate(self, tx: TxTemplate, gas_units: int) -> GasEstimate:
        self.calls.append(gas_units)
        return GasEstimate(
            gas_units=gas_units,
            gas_price_wei=0,
            l1_fee_wei=self.cost_wei,
            native_price_usd=self.native_price,
        )


@dataclass
class MemoryBorrowerRepository:
    rows: dict[str, dict[str, tuple[int, bool, Decimal | None]]] = field(default_factory=dict)
    health_updates: list[HealthCheck] = field(default_factory=list)

    async def upsert_discovered(self, market: MarketRef, borrowers: Mapping[str, int]) -> int:
        table = self.rows.setdefault(market.key, {})
        new = 0
        for address, block in borrowers.items():
            if address not in table:
                new += 1
                table[address] = (block, True, None)
            else:
                _b, _a, hf = table[address]
                table[address] = (max(block, _b), True, hf)
        return new

    async def list_active(self, market: MarketRef) -> list[str]:
        return [a for a, (_b, active, _hf) in self.rows.get(market.key, {}).items() if active]

    async def list_watchlist(self, market: MarketRef, hf_threshold: Decimal) -> list[str]:
        return [
            a
            for a, (_b, active, hf) in self.rows.get(market.key, {}).items()
            if active and hf is not None and hf < hf_threshold
        ]

    async def update_health(
        self, market: MarketRef, checks: Sequence[HealthCheck], min_debt_usd: Decimal
    ) -> None:
        self.health_updates.extend(checks)
        table = self.rows.setdefault(market.key, {})
        for check in checks:
            block = table.get(check.borrower, (0, True, None))[0]
            active = check.health_factor is not None and check.debt_usd >= min_debt_usd
            table[check.borrower] = (block, active, check.health_factor)


@dataclass
class MemoryCheckpointRepository:
    blocks: dict[str, int] = field(default_factory=dict)

    async def get(self, market: MarketRef) -> int | None:
        return self.blocks.get(market.key)

    async def set(self, market: MarketRef, block: int) -> None:
        self.blocks[market.key] = block


@dataclass
class MemoryOpportunityRepository:
    items: list[OpportunitySummary] = field(default_factory=list)

    async def add(self, summary: OpportunitySummary) -> int:
        self.items.append(summary)
        return len(self.items)

    async def get(self, opportunity_id: int) -> OpportunitySummary:
        return self.items[opportunity_id - 1]


@dataclass
class MemoryDeduplicator:
    keys: set[str] = field(default_factory=set)
    released: list[str] = field(default_factory=list)

    async def acquire(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def release(self, key: str) -> None:
        self.keys.discard(key)
        self.released.append(key)


@dataclass
class RecordingDispatcher:
    sent: list[OpportunitySummary] = field(default_factory=list)
    fail: bool = False

    async def dispatch(self, opportunity: OpportunitySummary) -> None:
        if self.fail:
            raise RuntimeError("broker down")
        self.sent.append(opportunity)


@dataclass
class StubIndexer:
    calls: list[str] = field(default_factory=list)

    async def sync(self, protocol: object) -> IndexResult:
        self.calls.append(str(protocol))
        return IndexResult(0, 0, 0, True)


@dataclass
class StubScanner:
    result: ScanResult
    seen: list[Sequence[str]] = field(default_factory=list)

    async def scan(self, protocol: object, borrowers: Sequence[str]) -> ScanResult:
        self.seen.append(borrowers)
        return self.result
