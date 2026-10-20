from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from eth_abi.abi import encode

from aegis.config.chains import CometMarketConfig
from aegis.core.errors import NotFoundError, NotificationError
from aegis.core.interfaces import BaseLendingProtocol
from aegis.core.models import (
    EventEnrichment,
    LiquidationEvent,
    LiquidationRecord,
    LogEntry,
    MarketRef,
    Sighting,
    TxReceiptInfo,
)
from aegis.market.enricher import MarketEnricher
from aegis.market.lag import find_liquidatable_since
from aegis.market.stream import LiquidationStream
from aegis.protocols.aave_v3 import (
    GET_ASSETS_PRICES,
    GET_USER_ACCOUNT_DATA,
    LIQUIDATION_CALL_TOPIC,
    AaveV3Protocol,
    Reserve,
    ReserveConfig,
)
from aegis.protocols.compound_v3 import (
    ABSORB_COLLATERAL_TOPIC,
    ABSORB_DEBT_TOPIC,
    BUY_COLLATERAL_TOPIC,
    GET_PRICE,
    CometAsset,
    CompoundV3Protocol,
)
from aegis.scanner.indexer import EventIndexer
from tests.factories import BORROWER, MARKET, USDC, WETH
from tests.fakes import FakeOracle, FakeProvider, MemoryCheckpointRepository
from tests.unit.test_scanner import StubProtocol

POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
ORACLE = "0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156"
COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"
LIQUIDATOR = "0x" + "9a" * 20


def _topic(address: str) -> bytes:
    return b"\x00" * 12 + bytes.fromhex(address[2:])


def _aave() -> tuple[AaveV3Protocol, FakeProvider]:
    provider = FakeProvider()
    protocol = AaveV3Protocol(provider, "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D")
    protocol._pool, protocol._oracle = POOL, ORACLE
    cfg = ReserveConfig(8000, 10500, 6, True, False, False, 1000)
    protocol._reserves = [
        Reserve(0, USDC, "0x" + "a0" * 20, "0x" + "d0" * 20, cfg),
        Reserve(1, WETH, "0x" + "a1" * 20, "0x" + "d1" * 20, cfg),
    ]
    protocol._loaded_at = 1e18  # skip initialize()
    return protocol, provider


def _aave_log(block: int = 100) -> LogEntry:
    data = encode(
        ["uint256", "uint256", "address", "bool"], [1000 * 10**6, 525 * 10**15, LIQUIDATOR, False]
    )
    topics = (LIQUIDATION_CALL_TOPIC, _topic(WETH.address), _topic(USDC.address), _topic(BORROWER))
    return LogEntry(POOL, topics, data, block, "0x" + "ab" * 32, 3)


async def test_aave_decode_and_value_liquidation() -> None:
    protocol, provider = _aave()
    event = protocol.decode_liquidation(_aave_log())
    assert event is not None
    assert event.kind == "aave_liquidation" and (event.borrower or "").lower() == BORROWER
    assert event.liquidator.lower() == LIQUIDATOR and event.debt_asset == USDC.address
    assert event.debt_amount == 1000 * 10**6 and event.collateral_amount == 525 * 10**15
    seen_blocks: list[object] = []

    def prices(data: bytes) -> bytes:
        return encode(["uint256[]"], [[2000 * 10**8, 10**8]])  # [collateral, debt]

    original_call = provider.call

    async def call(to: str, data: bytes, block: object = "latest") -> bytes:
        seen_blocks.append(block)
        return await original_call(to, data, block)  # type: ignore[arg-type]

    provider.on(ORACLE, GET_ASSETS_PRICES.selector, prices)
    provider.call = call  # type: ignore[method-assign]
    valuation = await protocol.value_liquidation(event)
    assert valuation is not None
    assert valuation.debt_usd == Decimal(1000) and valuation.collateral_usd == Decimal(1050)
    assert valuation.gross_profit_usd == Decimal(50)
    assert seen_blocks == [100]  # priced at the event's own block
    assert protocol.asset_symbol(USDC.address) == "USDC"


async def test_aave_is_liquidatable_at_block() -> None:
    protocol, provider = _aave()
    provider.on(
        POOL,
        GET_USER_ACCOUNT_DATA.selector,
        lambda _: encode(["uint256"] * 6, [1, 100, 0, 0, 0, 99 * 10**16]),
    )
    assert await protocol.is_liquidatable_at(BORROWER, 50) is True


def test_aave_decode_ignores_other_logs() -> None:
    protocol, _ = _aave()
    log = LogEntry(POOL, (b"\x01" * 32,), b"", 1, "0x", 0)
    assert protocol.decode_liquidation(log) is None


def _comet() -> tuple[CompoundV3Protocol, FakeProvider]:
    provider = FakeProvider()
    protocol = CompoundV3Protocol(provider, CometMarketConfig("cUSDCv3", COMET), FakeOracle())
    protocol._base, protocol._base_feed = USDC, "0x" + "f0" * 20
    protocol._store_front = Decimal("0.6")
    protocol._assets = [CometAsset(WETH, "0x" + "f1" * 20, Decimal("0.9"), Decimal("0.95"))]
    return protocol, provider


async def test_comet_decode_and_value_all_kinds() -> None:
    protocol, provider = _comet()
    absorb = protocol.decode_liquidation(
        LogEntry(
            COMET,
            (ABSORB_DEBT_TOPIC, _topic(LIQUIDATOR), _topic(BORROWER)),
            encode(["uint256", "uint256"], [1000 * 10**6, 1000 * 10**8]),
            7,
            "0x01",
            0,
        )
    )
    absorbed = protocol.decode_liquidation(
        LogEntry(
            COMET,
            (ABSORB_COLLATERAL_TOPIC, _topic(LIQUIDATOR), _topic(BORROWER), _topic(WETH.address)),
            encode(["uint256", "uint256"], [10**18, 2000 * 10**8]),
            7,
            "0x01",
            1,
        )
    )
    buy = protocol.decode_liquidation(
        LogEntry(
            COMET,
            (BUY_COLLATERAL_TOPIC, _topic(LIQUIDATOR), _topic(WETH.address)),
            encode(["uint256", "uint256"], [1940 * 10**6, 10**18]),
            9,
            "0x02",
            0,
        )
    )
    assert absorb and absorb.kind == "comet_absorb" and (absorb.borrower or "").lower() == BORROWER
    assert absorbed and absorbed.kind == "comet_absorb_collateral"
    assert buy and buy.kind == "comet_buy" and buy.borrower is None

    absorb_value = await protocol.value_liquidation(absorb)
    assert absorb_value is not None and absorb_value.gross_profit_usd == 0
    absorbed_value = await protocol.value_liquidation(absorbed)
    assert absorbed_value is not None
    assert absorbed_value.collateral_usd == Decimal(2000)
    assert absorbed_value.gross_profit_usd == Decimal("60.000")  # 3% storefront discount

    def price(data: bytes) -> bytes:
        feed = "0x" + data[-20:].hex()
        return encode(["uint256"], [10**8 if feed == "0x" + "f0" * 20 else 2000 * 10**8])

    provider.on(COMET, GET_PRICE.selector, price)
    buy_value = await protocol.value_liquidation(buy)
    assert buy_value is not None and buy_value.gross_profit_usd == Decimal(60)


class LagProtocol(StubProtocol):
    def __init__(self, became_at: int | None) -> None:
        super().__init__(FakeProvider())
        self.became_at = became_at
        self.calls = 0

    async def is_liquidatable_at(self, borrower: str, block: int) -> bool | None:
        self.calls += 1
        if self.became_at is None:
            return None
        return block >= self.became_at


async def test_lag_binary_search() -> None:
    protocol = LagProtocol(became_at=937)
    result = await find_liquidatable_since(protocol, BORROWER, event_block=1000, window_blocks=500)
    assert result is not None
    assert (result.since_block, result.lag_blocks, result.capped) == (937, 63, False)
    assert protocol.calls <= 12  # log2(500) + 2


async def test_lag_same_block_and_capped_and_unsupported() -> None:
    same = await find_liquidatable_since(LagProtocol(1000), BORROWER, 1000, 500)
    assert same is not None and same.lag_blocks == 0 and not same.capped
    capped = await find_liquidatable_since(LagProtocol(10), BORROWER, 1000, 500)
    assert capped is not None and capped.capped and capped.lag_blocks == 500
    assert await find_liquidatable_since(LagProtocol(None), BORROWER, 1000, 500) is None


@dataclass
class MemoryEvents:
    events: list[LiquidationEvent] = field(default_factory=list)
    enriched: dict[int, EventEnrichment] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)
    attempts: dict[int, int] = field(default_factory=dict)
    known: bool = True

    async def add(self, events: Sequence[LiquidationEvent]) -> int:
        new = [e for e in events if (e.tx_hash, e.log_index) not in
               {(x.tx_hash, x.log_index) for x in self.events}]  # fmt: skip
        self.events.extend(new)
        return len(new)

    async def pending(
        self, limit: int, max_attempts: int
    ) -> list[tuple[int, int, LiquidationEvent]]:
        return [
            (i, self.attempts.get(i, 0), e)
            for i, e in enumerate(self.events)
            if i not in self.enriched and self.attempts.get(i, 0) < max_attempts
        ][:limit]

    async def previous_liquidation_block(
        self, market: MarketRef, borrower: str, before_block: int, since_block: int
    ) -> int | None:
        earlier = [
            e.block_number
            for e in self.events
            if e.borrower == borrower and since_block <= e.block_number < before_block
        ]
        return max(earlier, default=None)

    async def events_in_tx(self, chain: str, tx_hash: str) -> int:
        return sum(1 for e in self.events if e.tx_hash == tx_hash)

    async def save_enrichment(self, event_id: int, enrichment: EventEnrichment) -> None:
        self.enriched[event_id] = enrichment

    async def mark_failed(self, event_id: int, error: str) -> None:
        self.failed[event_id] = error
        self.attempts[event_id] = self.attempts.get(event_id, 0) + 1

    async def borrower_known(self, market: MarketRef, borrower: str, before_block: int) -> bool:
        return self.known

    async def enriched_between(
        self, since: datetime, until: datetime, kinds: Sequence[str]
    ) -> list[LiquidationRecord]:
        return []

    async def get_record(self, event_id: int) -> LiquidationRecord:
        raise NotFoundError(str(event_id))

    async def pending_count(self) -> int:
        return len(self.events) - len(self.enriched)


@dataclass
class RecordingAlerts:
    sent: list[int] = field(default_factory=list)
    fail: bool = False

    async def dispatch_liquidation(self, event_id: int) -> None:
        if self.fail:
            raise NotificationError("broker down")
        self.sent.append(event_id)


@dataclass
class MemorySightings:
    first: datetime | None = None
    closed: list[str] = field(default_factory=list)
    recorded: list[tuple[list[Sighting], frozenset[str]]] = field(default_factory=list)

    async def record(
        self, market: MarketRef, sightings: Sequence[Sighting], healthy: frozenset[str]
    ) -> None:
        self.recorded.append((list(sightings), healthy))

    async def first_seen(
        self, market: MarketRef, borrower: str, at: datetime, gap_seconds: int
    ) -> datetime | None:
        return self.first

    async def close(
        self, market: MarketRef, borrower: str, reason: str, started_before: datetime
    ) -> None:
        self.closed.append(reason)


@dataclass
class MemoryUptime:
    online: bool = True

    async def mark(self, chain: str, minute: datetime) -> None:
        return None

    async def was_online(self, chain: str, at: datetime) -> bool:
        return self.online


async def _enrichable_event() -> tuple[AaveV3Protocol, MemoryEvents, datetime]:
    """One indexed Aave liquidation whose receipt and prices are available ($49.988 net)."""
    protocol, provider = _aave()
    events = MemoryEvents()
    await events.add([protocol.decode_liquidation(_aave_log(100))])  # type: ignore[list-item]
    block_time = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    provider.receipts["0x" + "ab" * 32] = TxReceiptInfo(
        tx_from="0x" + "ee" * 20, tx_to=LIQUIDATOR, tx_index=2, gas_used=500_000,
        effective_gas_price=10**7, l1_fee=10**12, status=1,
        block_timestamp=int(block_time.timestamp()), logs_in_tx=9,
    )  # fmt: skip
    provider.on(ORACLE, GET_ASSETS_PRICES.selector,
                lambda _: encode(["uint256[]"], [[2000 * 10**8, 10**8]]))  # fmt: skip
    provider.on(
        POOL,
        GET_USER_ACCOUNT_DATA.selector,
        lambda _: encode(["uint256"] * 6, [1, 100, 0, 0, 0, 98 * 10**16]),
    )
    return protocol, events, block_time


@pytest.mark.parametrize(
    ("min_net", "age_minutes", "expected"),
    [(Decimal(10), 60, [0]), (Decimal(100), 60, []), (Decimal(10), 1, []), (Decimal(0), 60, [])],
)
async def test_large_liquidation_alert(
    min_net: Decimal, age_minutes: int, expected: list[int]
) -> None:
    protocol, events, _ = await _enrichable_event()
    alerts = RecordingAlerts()
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {"base": FakeOracle(Decimal(2000))}, events,
        MemorySightings(), MemoryUptime(), lag_window_seconds=60, alerts=alerts,
        alert_min_net_usd=min_net, alert_max_age_seconds=age_minutes * 60,
    )  # fmt: skip
    assert await enricher.enrich_pending() == 1
    assert alerts.sent == expected


async def test_alert_failure_keeps_enrichment() -> None:
    protocol, events, _ = await _enrichable_event()
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {"base": FakeOracle(Decimal(2000))}, events,
        MemorySightings(), MemoryUptime(), lag_window_seconds=60,
        alerts=RecordingAlerts(fail=True), alert_min_net_usd=Decimal(1),
    )  # fmt: skip
    assert await enricher.enrich_pending() == 1
    assert 0 in events.enriched and not events.failed


async def test_stream_and_enricher_end_to_end() -> None:
    protocol, provider = _aave()
    provider.head = 200
    provider.logs = [_aave_log(100)]
    events, sightings = MemoryEvents(), MemorySightings()
    indexer = EventIndexer(
        MemoryCheckpointRepository(),
        LiquidationStream(events),
        lookback_days=1,
        chunk_blocks=1000,
        max_blocks_per_run=10_000,
    )
    result = await indexer.sync(protocol)
    assert result.discovered == 1 and len(events.events) == 1
    assert (await indexer.sync(protocol)).discovered == 0  # checkpointed, no duplicates

    block_time = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    provider.receipts["0x" + "ab" * 32] = TxReceiptInfo(
        tx_from="0x" + "ee" * 20, tx_to=LIQUIDATOR, tx_index=2, gas_used=500_000,
        effective_gas_price=10**7, l1_fee=10**12, status=1,
        block_timestamp=int(block_time.timestamp()), logs_in_tx=9,
    )  # fmt: skip
    provider.on(ORACLE, GET_ASSETS_PRICES.selector,
                lambda _: encode(["uint256[]"], [[2000 * 10**8, 10**8]]))  # fmt: skip

    def account(data: bytes) -> bytes:  # liquidatable for every queried block >= 95
        return encode(["uint256"] * 6, [1, 100, 0, 0, 0, 98 * 10**16])

    provider.on(POOL, GET_USER_ACCOUNT_DATA.selector, account)
    sightings.first = block_time - timedelta(seconds=12)
    enricher = MarketEnricher(
        {protocol.market.key: protocol},
        {"base": FakeOracle(Decimal(2000))},
        events,
        sightings,
        MemoryUptime(),
        lag_window_seconds=60,
    )
    assert await enricher.enrich_pending() == 1
    enrichment = enricher_result = events.enriched[0]
    assert enrichment.block_time == block_time
    assert enrichment.valuation is not None and enrichment.valuation.gross_profit_usd == 50
    # (500k gas x 0.01 gwei + 1e12 L1 fee) = 6e12 wei = 0.000006 ETH x $2000 = $0.012
    assert enrichment.gas_cost_usd == Decimal("0.012")
    assert enrichment.net_profit_usd == Decimal("49.988")
    assert enricher_result.lag_capped is True  # liquidatable for the whole 30-block window
    assert enrichment.our_lead_seconds == Decimal("12")
    assert enrichment.bot_online is True and enrichment.borrower_known is True
    assert enrichment.debt_symbol == "USDC" and enrichment.collateral_symbol == "WETH"
    assert sightings.closed == ["liquidated"]


async def test_enricher_marks_failures() -> None:
    protocol, _provider = _aave()
    events = MemoryEvents()
    await events.add([protocol.decode_liquidation(_aave_log())])  # type: ignore[list-item]
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {}, events, MemorySightings(), MemoryUptime(),
        lag_window_seconds=60,
    )  # fmt: skip
    assert await enricher.enrich_pending() == 0  # no receipt available
    assert 0 in events.failed


async def test_orchestrator_records_sightings() -> None:
    from aegis.core.interfaces import ScanResult
    from aegis.core.models import HealthCheck
    from aegis.scanner.orchestrator import ScanOrchestrator
    from tests.fakes import (
        MemoryBorrowerRepository,
        MemoryDeduplicator,
        MemoryOpportunityRepository,
        RecordingDispatcher,
        StubIndexer,
        StubScanner,
    )

    check = HealthCheck(BORROWER, Decimal("0.97"), Decimal(500), Decimal(510), True)
    result = ScanResult(2, 1, (), (check,), frozenset({"0x" + "22" * 20}))
    sightings = MemorySightings()
    orchestrator = ScanOrchestrator(
        {"base": [StubProtocol(FakeProvider())]}, StubIndexer(), StubScanner(result),
        MemoryBorrowerRepository(), MemoryOpportunityRepository(), MemoryDeduplicator(),
        RecordingDispatcher(), min_profit_usd=Decimal(5), watchlist_hf_threshold=Decimal("1.05"),
        sightings=sightings,
    )  # fmt: skip
    await orchestrator.scan_chain("base", "full")
    ((recorded, healthy),) = sightings.recorded
    assert recorded[0].borrower == BORROWER and recorded[0].est_net_profit_usd is None
    assert healthy == frozenset({"0x" + "22" * 20})
    _ = MARKET, BaseLendingProtocol


async def test_enricher_skips_lag_search_for_dust() -> None:
    protocol, provider = _aave()
    events = MemoryEvents()
    dust_log = _aave_log(100)
    await events.add([protocol.decode_liquidation(dust_log)])  # type: ignore[list-item]
    provider.receipts[dust_log.tx_hash] = TxReceiptInfo(
        "0x" + "ee" * 20, None, 0, 1, 1, 0, 1, 1_700_000_000, 1
    )
    # 0.525 WETH priced at $1905.3 -> $1000.28 collateral vs $1000 debt: gross < $1 (dust)
    provider.on(ORACLE, GET_ASSETS_PRICES.selector,
                lambda _: encode(["uint256[]"], [[19_053 * 10**5, 10**8]]))  # fmt: skip
    lag_calls: list[bytes] = []

    def account(data: bytes) -> bytes:
        lag_calls.append(data)
        return encode(["uint256"] * 6, [1, 1, 0, 0, 0, 0])

    provider.on(POOL, GET_USER_ACCOUNT_DATA.selector, account)
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {}, events, MemorySightings(), MemoryUptime(),
        lag_window_seconds=60,
    )  # fmt: skip
    assert await enricher.enrich_pending() == 1
    enrichment = events.enriched[0]
    assert enrichment.valuation is not None and enrichment.valuation.gross_profit_usd < 1
    assert enrichment.lag_blocks is None and lag_calls == []


def _receipt(topics: tuple[bytes, ...] = ()) -> TxReceiptInfo:
    return TxReceiptInfo(
        "0x" + "ee" * 20, None, 0, 1_000_000, 10**7, 0, 1, 1_700_000_000, len(topics), topics
    )


class FlakyOracleProvider(FakeProvider):
    """Raises a configurable error for historical (block-pinned) eth_calls."""

    def __init__(self, error: Exception | None) -> None:
        super().__init__()
        self.error = error

    async def call(self, to: str, data: bytes, block: object = "latest") -> bytes:
        if self.error is not None and isinstance(block, int):
            raise self.error
        return await super().call(to, data, block)  # type: ignore[arg-type]


def _flaky_aave(error: Exception | None) -> tuple[AaveV3Protocol, FlakyOracleProvider]:
    provider = FlakyOracleProvider(error)
    protocol, _ = _aave()
    protocol._provider = provider
    return protocol, provider


async def test_transient_rpc_error_retries_then_stores_with_gaps() -> None:
    from aegis.core.errors import RpcError

    protocol, provider = _flaky_aave(RpcError("429 too many requests"))
    log = _aave_log(100)
    provider.receipts[log.tx_hash] = _receipt((LIQUIDATION_CALL_TOPIC,))
    events = MemoryEvents()
    await events.add([protocol.decode_liquidation(log)])  # type: ignore[list-item]
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {"base": FakeOracle()}, events, MemorySightings(),
        MemoryUptime(), lag_window_seconds=60, max_attempts=3,
    )  # fmt: skip
    assert await enricher.enrich_pending() == 0  # attempt 1: retry later
    assert await enricher.enrich_pending() == 0  # attempt 2: retry later
    assert events.attempts[0] == 2 and 0 not in events.enriched
    assert await enricher.enrich_pending() == 1  # final attempt: stored, gaps recorded
    gaps = events.enriched[0].data_gaps or ""
    assert "value_rpc_error" in gaps and events.enriched[0].valuation is None


async def test_no_archive_is_recorded_without_retries() -> None:
    from aegis.core.errors import HistoricalStateUnavailableError

    protocol, provider = _flaky_aave(HistoricalStateUnavailableError("missing trie node"))
    log = _aave_log(100)
    provider.receipts[log.tx_hash] = _receipt()
    events = MemoryEvents()
    await events.add([protocol.decode_liquidation(log)])  # type: ignore[list-item]
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {"base": FakeOracle()}, events, MemorySightings(),
        MemoryUptime(), lag_window_seconds=60,
    )  # fmt: skip
    assert await enricher.enrich_pending() == 1  # first attempt already final for archive gaps
    gaps = events.enriched[0].data_gaps or ""
    assert "no_archive_value" in gaps and "no_archive_lag" in gaps


async def test_gas_split_by_receipt_and_follow_up() -> None:
    protocol, provider = _aave()
    first, second = _aave_log(100), _aave_log(130)
    second = LogEntry(second.address, second.topics, second.data, 130, "0x" + "cd" * 32, 0)
    # One tx with two liquidations (+ an unrelated log): each gets half the fee.
    provider.receipts[first.tx_hash] = _receipt((LIQUIDATION_CALL_TOPIC, LIQUIDATION_CALL_TOPIC,
                                                 b"\x01" * 32))  # fmt: skip
    provider.receipts[second.tx_hash] = _receipt((LIQUIDATION_CALL_TOPIC,))
    provider.on(ORACLE, GET_ASSETS_PRICES.selector,
                lambda _: encode(["uint256[]"], [[2000 * 10**8, 10**8]]))  # fmt: skip
    provider.on(POOL, GET_USER_ACCOUNT_DATA.selector,
                lambda _: encode(["uint256"] * 6, [1, 100, 0, 0, 0, 98 * 10**16]))  # fmt: skip
    events = MemoryEvents()
    await events.add([protocol.decode_liquidation(first), protocol.decode_liquidation(second)])  # type: ignore[list-item]
    enricher = MarketEnricher(
        {protocol.market.key: protocol}, {"base": FakeOracle(Decimal(2000))}, events,
        MemorySightings(), MemoryUptime(), lag_window_seconds=60,
    )  # fmt: skip
    assert await enricher.enrich_pending() == 2
    # fee = 1e6 gas * 1e7 wei = 1e13 wei = 0.00001 ETH = $0.02, split in two -> $0.01
    assert events.enriched[0].gas_cost_usd == Decimal("0.01")
    assert events.enriched[1].gas_cost_usd == Decimal("0.02")
    assert not events.enriched[0].follow_up
    assert events.enriched[1].follow_up  # same borrower liquidated 30 blocks earlier
    assert events.enriched[1].lag_blocks is None
