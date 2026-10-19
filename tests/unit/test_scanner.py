from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from aegis.core.errors import RpcError
from aegis.core.interfaces import BaseLendingProtocol, BlockId, ScanResult
from aegis.core.models import (
    HealthCheck,
    LiquidationOpportunity,
    LiquidationQuote,
    LogEntry,
    LogFilter,
    Position,
)
from aegis.scanner.health import HealthFactorScanner
from aegis.scanner.indexer import EventBorrowerIndexer
from aegis.scanner.orchestrator import ScanOrchestrator, alert_key
from tests.factories import MARKET, collateral_leg, debt_leg, position, summary
from tests.fakes import (
    FakeGasEstimator,
    FakeProvider,
    MemoryBorrowerRepository,
    MemoryCheckpointRepository,
    MemoryDeduplicator,
    MemoryOpportunityRepository,
    RecordingDispatcher,
    StubIndexer,
    StubScanner,
)

TOPIC = b"\x01" * 32


class StubProtocol(BaseLendingProtocol):
    protocol_name = "stub"

    def __init__(self, provider: FakeProvider, checks: list[HealthCheck] | None = None) -> None:
        super().__init__(provider, MARKET)
        self.checks = checks or []
        self.loaded: list[str] = []
        self.initialized = 0

    async def initialize(self) -> None:
        self.initialized += 1

    def discovery_filter(self) -> LogFilter:
        return LogFilter(MARKET.address, (TOPIC,))

    def borrower_from_log(self, log: LogEntry) -> str | None:
        return "0x" + log.topics[1][-20:].hex()

    async def check_health(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[HealthCheck]:
        return [c for c in self.checks if c.borrower in borrowers]

    async def load_positions(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[Position]:
        self.loaded.extend(borrowers)
        return [
            position(Decimal("0.9"), (debt_leg(Decimal(1000)),), (collateral_leg(Decimal(2000)),))
            for _ in borrowers
        ]

    def quote_liquidations(self, position: Position) -> list[LiquidationQuote]:
        from aegis.core.models import TxTemplate
        from aegis.protocols.aave_v3 import quote_pair

        terms = quote_pair(position.health_factor, position.debt[0], position.collateral[0])
        assert terms is not None
        units, debt_usd, seized, gross = terms
        return [
            LiquidationQuote(
                position.debt[0].asset,
                position.collateral[0].asset,
                units,
                debt_usd,
                seized,
                gross,
                Decimal(5),
                100_000,
                TxTemplate(MARKET.address, b""),
            )
        ]


def _log(block: int, borrower_byte: str) -> LogEntry:
    topic1 = b"\x00" * 12 + bytes.fromhex(borrower_byte * 20)
    return LogEntry(MARKET.address, (TOPIC, topic1), b"", block, "0x", 0)


def _indexer(
    **kwargs: int,
) -> tuple[EventBorrowerIndexer, MemoryBorrowerRepository, MemoryCheckpointRepository]:
    borrowers, checkpoints = MemoryBorrowerRepository(), MemoryCheckpointRepository()
    params = {"lookback_days": 1, "chunk_blocks": 1000, "max_blocks_per_run": 100_000}
    params.update(kwargs)
    return EventBorrowerIndexer(borrowers, checkpoints, **params), borrowers, checkpoints


async def test_indexer_first_run_uses_lookback_and_checkpoints() -> None:
    provider = FakeProvider()  # Base: 2s blocks -> 43_200 blocks per day
    provider.head = 100_000
    head = 100_000 - provider.config.reorg_safety_blocks
    provider.logs = [_log(head - 10, "aa"), _log(head - 5, "aa"), _log(head - 50_000, "bb")]
    indexer, borrowers, checkpoints = _indexer()
    result = await indexer.sync(StubProtocol(provider))
    assert result.from_block == head - 43_200
    assert result.caught_up and result.discovered == 1
    assert checkpoints.blocks[MARKET.key] == head
    assert list(borrowers.rows[MARKET.key]) == ["0x" + "aa" * 20]

    provider.head += 100
    provider.logs.append(_log(head + 50, "cc"))
    result = await indexer.sync(StubProtocol(provider))
    assert result.from_block == head + 1
    assert result.discovered == 1


async def test_indexer_shrinks_chunk_on_rpc_error() -> None:
    provider = FakeProvider()
    provider.head = 10_000
    provider.max_log_range = 300
    indexer, _, checkpoints = _indexer(chunk_blocks=1000, max_blocks_per_run=2000)
    await checkpoints.set(MARKET, 5000)
    result = await indexer.sync(StubProtocol(provider))
    assert result.from_block == 5001
    assert result.to_block == 7000
    assert not result.caught_up
    successful = [(a, b) for a, b in provider.get_logs_calls if b - a + 1 <= 300]
    assert successful[0][0] == 5001 and successful[-1][1] == 7000
    assert checkpoints.blocks[MARKET.key] == 7000


async def test_indexer_does_not_grow_back_into_rejected_range() -> None:
    provider = FakeProvider()
    provider.head = 100_000
    provider.max_log_range = 300
    indexer, _, checkpoints = _indexer(chunk_blocks=1000, max_blocks_per_run=20_000)
    await checkpoints.set(MARKET, 5000)
    await indexer.sync(StubProtocol(provider))
    rejected = [(a, b) for a, b in provider.get_logs_calls if b - a + 1 > 300]
    assert len(rejected) == 2  # 1000 and 500 once, never re-tried after 10 successes


async def test_indexer_outage_does_not_shrink_chunk() -> None:
    provider = FakeProvider()
    provider.head = 10_000
    indexer, _, checkpoints = _indexer(chunk_blocks=1000, max_blocks_per_run=2000)
    await checkpoints.set(MARKET, 5000)
    provider.logs_down = True
    with pytest.raises(RpcError):
        await indexer.sync(StubProtocol(provider))
    assert provider.get_logs_calls == [(5001, 6000)]  # one attempt, no halving loop
    provider.logs_down = False
    provider.get_logs_calls.clear()
    await indexer.sync(StubProtocol(provider))
    assert provider.get_logs_calls[0] == (5001, 6000)  # full chunk size kept


async def test_indexer_gives_up_below_min_chunk() -> None:
    provider = FakeProvider()
    provider.max_log_range = 1
    indexer, _, _ = _indexer(chunk_blocks=40)
    with pytest.raises(RpcError):
        await indexer.sync(StubProtocol(provider))


async def test_health_scanner_only_loads_liquidatable_above_dust() -> None:
    checks = [
        HealthCheck("0x" + "01" * 20, Decimal("0.9"), Decimal(1000), Decimal(1100), True),
        HealthCheck("0x" + "02" * 20, Decimal("0.9"), Decimal(5), Decimal(6), True),
        HealthCheck("0x" + "03" * 20, Decimal("1.5"), Decimal(1000), Decimal(3000), False),
    ]
    protocol = StubProtocol(FakeProvider(), checks)
    from aegis.estimator.profit import ProfitEstimator

    repo = MemoryBorrowerRepository()
    scanner = HealthFactorScanner(
        repo, ProfitEstimator(FakeGasEstimator()), min_debt_usd=Decimal(10)
    )
    result = await scanner.scan(protocol, [c.borrower for c in checks])
    assert result.checked == 3 and result.liquidatable == 1
    assert protocol.loaded == ["0x" + "01" * 20]
    assert len(result.opportunities) == 1
    assert len(repo.health_updates) == 3
    assert protocol.initialized == 1


async def test_health_scanner_empty() -> None:
    from aegis.estimator.profit import ProfitEstimator

    scanner = HealthFactorScanner(
        MemoryBorrowerRepository(), ProfitEstimator(FakeGasEstimator()), min_debt_usd=Decimal(10)
    )
    result = await scanner.scan(StubProtocol(FakeProvider()), [])
    assert result == ScanResult(0, 0, ())


def _opportunity(net_gross: Decimal) -> LiquidationOpportunity:
    from datetime import UTC, datetime

    from aegis.core.models import GasEstimate, ProfitEstimate, TxTemplate

    pos = position(Decimal("0.9"), (debt_leg(Decimal(1000)),), (collateral_leg(Decimal(2000)),))
    quote = LiquidationQuote(
        pos.debt[0].asset,
        pos.collateral[0].asset,
        1,
        Decimal(1000),
        Decimal(1050),
        net_gross,
        Decimal(5),
        1,
        TxTemplate(MARKET.address, b""),
    )
    gas = GasEstimate(1, 0, 0, Decimal(2000))
    return LiquidationOpportunity(pos, ProfitEstimate(quote, gas), datetime.now(UTC))


def _orchestrator(
    opportunities: tuple[LiquidationOpportunity, ...], dispatcher: RecordingDispatcher
) -> tuple[
    ScanOrchestrator, MemoryDeduplicator, MemoryOpportunityRepository, MemoryBorrowerRepository
]:
    repo = MemoryBorrowerRepository()
    dedup, opps = MemoryDeduplicator(), MemoryOpportunityRepository()
    orchestrator = ScanOrchestrator(
        {"base": [StubProtocol(FakeProvider())]},
        StubIndexer(),
        StubScanner(ScanResult(1, len(opportunities), opportunities)),
        repo,
        opps,
        dedup,
        dispatcher,
        min_profit_usd=Decimal(5),
        watchlist_hf_threshold=Decimal("1.05"),
    )
    return orchestrator, dedup, opps, repo


async def test_orchestrator_filters_threshold_and_dedups() -> None:
    dispatcher = RecordingDispatcher()
    orchestrator, _dedup, opps, _ = _orchestrator(
        (_opportunity(Decimal(4)), _opportunity(Decimal(50))), dispatcher
    )
    await orchestrator.scan_chain("base", "full")
    assert len(dispatcher.sent) == 1
    assert dispatcher.sent[0].id == 1
    assert dispatcher.sent[0].net_profit_usd == Decimal(50)
    await orchestrator.scan_chain("base", "watchlist")
    assert len(dispatcher.sent) == 1  # deduplicated
    assert len(opps.items) == 1


async def test_orchestrator_releases_dedup_on_dispatch_failure() -> None:
    dispatcher = RecordingDispatcher(fail=True)
    orchestrator, dedup, _, _ = _orchestrator((_opportunity(Decimal(50)),), dispatcher)
    results = await orchestrator.scan_chain("base", "full")
    assert results == []  # failure logged, not raised
    assert dedup.keys == set()
    assert len(dedup.released) == 1


async def test_orchestrator_unknown_chain_is_noop() -> None:
    orchestrator, *_ = _orchestrator((), RecordingDispatcher())
    assert await orchestrator.scan_chain("arbitrum", "full") == []
    assert orchestrator.chains == ["base"]


def test_alert_key_is_case_insensitive() -> None:
    a = alert_key(summary(borrower="0xABCDEF0000000000000000000000000000000001"))
    b = alert_key(summary(borrower="0xabcdef0000000000000000000000000000000001"))
    assert a == b
