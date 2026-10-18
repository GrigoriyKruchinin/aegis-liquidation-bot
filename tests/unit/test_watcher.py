from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from eth_abi.abi import encode

from aegis.config.chains import ARBITRUM, BASE, ChainConfig
from aegis.core.abi import AbiFunction
from aegis.core.interfaces import BaseLendingProtocol, BlockId, ScanResult
from aegis.core.models import Call, CallResult, MarketRef
from aegis.scanner.watcher import ChainWatcher, WatcherConfig, max_move_bps
from tests.fakes import FakeProvider
from tests.unit.test_scanner import StubProtocol

PRICE = AbiFunction("price(uint256)", ("uint256",))
ORACLE = "0x" + "0c" * 20


class PricedProtocol(StubProtocol):
    def __init__(self, provider: FakeProvider, name: str) -> None:
        super().__init__(provider)
        self._market = MarketRef("base", "stub", "0x" + name * 20, name)

    def price_calls(self) -> list[Call]:
        return [Call(ORACLE, PRICE.encode(int(self._market.name, 16)))]

    def parse_prices(self, results: Sequence[CallResult]) -> tuple[int, ...]:
        return tuple(int(PRICE.decode(r.data)[0]) for r in results)


@dataclass
class RecordingScanner:
    hot: dict[str, list[str]] = field(default_factory=dict)
    scanned: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    full: list[str] = field(default_factory=list)

    async def watchlist(self, protocol: BaseLendingProtocol) -> list[str]:
        return self.hot.get(protocol.market.name, [])

    async def scan_market(self, protocol: BaseLendingProtocol, mode: str) -> ScanResult:
        self.full.append(protocol.market.name)
        return ScanResult(0, 0, ())

    async def scan_borrowers(
        self, protocol: BaseLendingProtocol, borrowers: Sequence[str]
    ) -> ScanResult:
        self.scanned.append((protocol.market.name, tuple(borrowers)))
        return ScanResult(len(borrowers), 0, ())


class CountingProvider(FakeProvider):
    def __init__(self, config: ChainConfig = BASE) -> None:
        super().__init__(config)
        self.multicalls = 0
        self.prices = {0xAA: 1000, 0xBB: 2000}

    async def multicall(self, calls: Sequence[Call], block: BlockId = "latest") -> list[CallResult]:
        self.multicalls += 1
        return await super().multicall(calls, block)


def _setup(
    cooldown: float = 5.0,
) -> tuple[ChainWatcher, CountingProvider, RecordingScanner, list[float]]:
    provider = CountingProvider()
    provider.on(
        ORACLE,
        PRICE.selector,
        lambda data: encode(["uint256"], [provider.prices[int.from_bytes(data[4:], "big")]]),
    )
    scanner = RecordingScanner()
    now = [100.0]
    watcher = ChainWatcher(
        provider,
        [PricedProtocol(provider, "aa"), PricedProtocol(provider, "bb")],
        scanner,
        WatcherConfig(price_move_bps=50, rescan_cooldown_seconds=cooldown),
        clock=lambda: now[0],
    )
    return watcher, provider, scanner, now


def test_max_move_bps() -> None:
    assert max_move_bps((), (1, 2)) == 0
    assert max_move_bps((1000, 2000), (1005, 2000)) == 50
    assert max_move_bps((1000, 2000), (1000, 1900)) == 500
    assert max_move_bps((0, 1000), (5, 1000)) == 0  # unreadable prices ignored
    assert max_move_bps((1, 2), (1, 2, 3)) == 0


def test_effective_interval_respects_block_time_and_minimum() -> None:
    base = ChainWatcher(FakeProvider(BASE), [], RecordingScanner(), WatcherConfig())
    arb = ChainWatcher(FakeProvider(ARBITRUM), [], RecordingScanner(), WatcherConfig())
    fast_arb = ChainWatcher(
        FakeProvider(ARBITRUM), [], RecordingScanner(), WatcherConfig(min_interval_ms=100)
    )
    assert base._interval == 2.0  # every block
    assert arb._interval == 1.0  # capped for RPC cost
    assert fast_arb._interval == 0.25  # every block when allowed


async def test_tick_only_processes_new_blocks_and_scans_hot_set() -> None:
    watcher, provider, scanner, _ = _setup()
    scanner.hot = {"aa": ["0x1"], "bb": []}
    assert await watcher.tick()
    assert scanner.scanned == [("aa", ("0x1",))]  # empty hot set -> no RPC for "bb"
    assert provider.multicalls == 1  # all markets' prices in ONE multicall
    assert not await watcher.tick()  # same head
    assert provider.multicalls == 1
    provider.head += 1
    assert await watcher.tick()
    assert len(scanner.scanned) == 2


async def test_price_move_triggers_background_full_rescan_with_cooldown() -> None:
    watcher, provider, scanner, now = _setup(cooldown=5.0)
    await watcher.tick()  # first snapshot, no rescan
    assert scanner.full == []

    provider.head += 1
    provider.prices[0xBB] = 2004  # 0.2% move: below threshold
    await watcher.tick()
    await asyncio.sleep(0)
    assert scanner.full == []

    provider.head += 1
    provider.prices[0xBB] = 1980  # 1% move vs snapshot -> rescan "bb" only
    await watcher.tick()
    await asyncio.sleep(0)
    assert scanner.full == ["bb"]

    provider.head += 1
    provider.prices[0xBB] = 1900  # another big move inside the cooldown -> deferred
    now[0] += 1
    await watcher.tick()
    await asyncio.sleep(0)
    assert scanner.full == ["bb"]

    provider.head += 1
    now[0] += 10  # cooldown over: deferred move still detected (snapshot kept)
    await watcher.tick()
    await asyncio.sleep(0)
    assert scanner.full == ["bb", "bb"]


async def test_run_loop_phase_locks_to_blocks() -> None:
    watcher, provider, _, _now = _setup()
    sleeps: list[float] = []
    stop = asyncio.Event()
    ticks: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(round(seconds, 3))
        if len(sleeps) == 1:
            return  # after block #1 -> wait ~one block
        if len(sleeps) == 2:
            provider.head += 1  # next block appears while polling
            return
        stop.set()

    async def on_tick() -> None:
        ticks.append(provider.head)

    watcher._sleep = fake_sleep
    watcher._on_tick = on_tick
    await watcher.run(stop)
    # block processed -> sleep(interval - poll); no new block -> sleep(poll); new block processed
    assert sleeps[0] == 1.8 and sleeps[1] == 0.2
    assert ticks == [1_000_000, 1_000_001]


async def test_stats_are_logged_periodically() -> None:
    from structlog.testing import capture_logs

    watcher, provider, scanner, now = _setup()
    scanner.hot = {"aa": ["0x1", "0x2"]}
    with capture_logs() as logs:
        await watcher.tick()
        provider.head += 1
        now[0] += 61
        await watcher.tick()
    stats = [e for e in logs if e["event"] == "watcher_stats"]
    assert len(stats) == 1
    assert stats[0]["ticks"] == 2 and stats[0]["hot_checked"] == 4
