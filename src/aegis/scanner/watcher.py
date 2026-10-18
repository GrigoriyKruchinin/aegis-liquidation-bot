"""Block-synchronous fast watcher: per-block hot-set re-checks and price-move triggered rescans.

Celery beat is fine for minute-scale work (indexing, periodic full scans) but too coarse for
detection latency. The watcher is a long-running asyncio loop per chain that:

1. Phase-locks to block production: after a new block it sleeps roughly one block interval, then
   polls the head every `head_poll_ms` until the next block appears, so a block is noticed within
   ~`head_poll_ms` of being produced.
2. On every new block reads the oracle prices of all markets of the chain in a single Multicall3
   request. If any price moved by `price_move_bps` or more since the last snapshot, a full rescan
   of that market is started immediately in the background (instead of waiting for the next
   scheduled full scan).
3. On every new block re-checks the hot set (borrowers with HF below the watchlist threshold) of
   every market, so a near-liquidation position is caught in the block it becomes liquidatable.

Protocol-agnostic: it only uses `BaseLendingProtocol` and the orchestrator.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from aegis.core.errors import AegisError
from aegis.core.interfaces import BaseChainProvider, BaseLendingProtocol, ScanResult
from aegis.core.logging import get_logger
from aegis.core.models import Call
from aegis.core.redact import safe_error
from aegis.scanner.orchestrator import ScanMode

logger = get_logger("scanner.watcher")

BPS = 10_000
STATS_LOG_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class WatcherConfig:
    """Tuning knobs (see `AppSettings.watcher_*`).

    Attributes:
        min_interval_ms: Lower bound between two processed blocks (RPC cost control on fast
            chains such as Arbitrum); the effective tick is ``max(block_time, min_interval)``.
        head_poll_ms: Head polling period while waiting for the next block.
        price_move_bps: Oracle price move that triggers an immediate full rescan.
        rescan_cooldown_seconds: Minimum time between two price-triggered rescans per market.
    """

    min_interval_ms: int = 1000
    head_poll_ms: int = 200
    price_move_bps: int = 50
    rescan_cooldown_seconds: float = 5.0


class MarketScanner(Protocol):
    """Narrow view of the orchestrator used by the watcher (ISP)."""

    async def watchlist(self, protocol: BaseLendingProtocol) -> list[str]: ...

    async def scan_market(self, protocol: BaseLendingProtocol, mode: ScanMode) -> ScanResult: ...

    async def scan_borrowers(
        self, protocol: BaseLendingProtocol, borrowers: Sequence[str]
    ) -> ScanResult: ...


def max_move_bps(previous: tuple[int, ...], current: tuple[int, ...]) -> int:
    """Largest relative price change between two snapshots, in basis points."""
    if not previous or len(previous) != len(current):
        return 0
    moves = [
        abs(new - old) * BPS // old
        for old, new in zip(previous, current, strict=True)
        if old > 0 and new > 0
    ]
    return max(moves, default=0)


@dataclass
class _Stats:
    """Rolling counters logged once per `STATS_LOG_INTERVAL_SECONDS`."""

    started: float
    blocks: int = 0
    hot_checked: int = 0
    rescans: int = 0
    tick_ms: list[float] = field(default_factory=list)


@dataclass
class _MarketState:
    prices: tuple[int, ...] = ()
    last_rescan: float = float("-inf")
    rescan_task: asyncio.Task[ScanResult] | None = None


class ChainWatcher:
    """Fast detection loop for one chain.

    Args:
        provider: Chain provider.
        markets: Markets of this chain.
        scanner: Orchestrator (or any object with `watchlist` / `scan_market` / `scan_borrowers`).
        config: Tuning knobs.
        on_tick: Called after every processed block (used for the healthcheck heartbeat).
        sleep: Injectable sleep (tests).
        clock: Injectable monotonic clock (tests).
    """

    def __init__(
        self,
        provider: BaseChainProvider,
        markets: Sequence[BaseLendingProtocol],
        scanner: MarketScanner,
        config: WatcherConfig,
        *,
        on_tick: Callable[[], Awaitable[None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._markets = list(markets)
        self._scanner = scanner
        self._config = config
        self._on_tick = on_tick
        self._sleep = sleep
        self._clock = clock
        self._last_head: int | None = None
        self._state = {m.market.key: _MarketState() for m in self._markets}
        block_ms = provider.config.block_time_ms
        self._interval = max(block_ms, config.min_interval_ms) / 1000
        self._stats = _Stats(started=clock())

    @property
    def chain(self) -> str:
        """Chain name."""
        return self._provider.name

    async def tick(self) -> bool:
        """Process the current head if it is new. Returns True when a new block was processed.

        The chain-wide price multicall and the per-market hot-set checks run concurrently, so a
        tick costs one RPC round trip after the head poll instead of several sequential ones.
        """
        head = await self._provider.block_number()
        if self._last_head is not None and head <= self._last_head:
            return False
        started = self._clock()
        self._last_head = head
        await asyncio.gather(*(protocol.ensure_initialized() for protocol in self._markets))
        prices_task = asyncio.ensure_future(self._check_prices())
        await asyncio.gather(*(self._check_hot(p) for p in self._markets))
        rescans = await prices_task
        for protocol in self._markets:
            if protocol.market.key in rescans:
                self._start_rescan(protocol)
        self._record_tick(started, len(rescans))
        if self._on_tick is not None:
            await self._on_tick()
        return True

    def _record_tick(self, started: float, rescans: int) -> None:
        now = self._clock()
        stats = self._stats
        stats.blocks += 1
        stats.rescans += rescans
        stats.tick_ms.append((now - started) * 1000)
        if now - stats.started >= STATS_LOG_INTERVAL_SECONDS:
            ordered = sorted(stats.tick_ms)
            logger.info(
                "watcher_stats",
                chain=self.chain,
                head=self._last_head,
                ticks=stats.blocks,
                hot_checked=stats.hot_checked,
                price_rescans=stats.rescans,
                tick_ms_median=round(ordered[len(ordered) // 2]),
                tick_ms_max=round(ordered[-1]),
            )
            self._stats = _Stats(started=now)

    async def _check_prices(self) -> set[str]:
        """One chain-wide multicall for all oracle prices; returns markets to rescan."""
        plan: list[tuple[BaseLendingProtocol, int, int]] = []
        calls: list[Call] = []
        for protocol in self._markets:
            market_calls = protocol.price_calls()
            plan.append((protocol, len(calls), len(market_calls)))
            calls.extend(market_calls)
        if not calls:
            return set()
        try:
            results = await self._provider.multicall(calls)
        except AegisError as exc:
            logger.warning("watch_prices_failed", chain=self.chain, error=safe_error(exc))
            return set()
        now = self._clock()
        rescans: set[str] = set()
        for protocol, start, count in plan:
            if count == 0:
                continue
            state = self._state[protocol.market.key]
            prices = protocol.parse_prices(results[start : start + count])
            move = max_move_bps(state.prices, prices)
            if not state.prices:
                state.prices = prices
            elif (
                move >= self._config.price_move_bps
                and now - state.last_rescan >= self._config.rescan_cooldown_seconds
            ):
                # During the cooldown the old snapshot is kept, so the move is not lost.
                rescans.add(protocol.market.key)
                state.last_rescan = now
                state.prices = prices
                logger.info("price_move_rescan", market=protocol.market.key, move_bps=move)
        return rescans

    async def _check_hot(self, protocol: BaseLendingProtocol) -> None:
        """Re-check near-liquidation borrowers of one market (no RPC when the set is empty)."""
        try:
            hot = await self._scanner.watchlist(protocol)
            if hot:
                self._stats.hot_checked += len(hot)
                await self._scanner.scan_borrowers(protocol, hot)
        except AegisError as exc:
            logger.warning("watch_market_failed", market=protocol.market.key, error=safe_error(exc))

    def _start_rescan(self, protocol: BaseLendingProtocol) -> None:
        """Full rescans can take seconds on big markets: run them without blocking the loop."""
        state = self._state[protocol.market.key]
        if state.rescan_task is None or state.rescan_task.done():
            state.rescan_task = asyncio.create_task(self._scanner.scan_market(protocol, "full"))
            state.rescan_task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: asyncio.Task[ScanResult]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("price_rescan_failed", error=safe_error(exc))

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set."""
        poll = self._config.head_poll_ms / 1000
        logger.info("watcher_started", chain=self.chain, interval_s=self._interval, poll_s=poll)
        while not stop.is_set():
            started = self._clock()
            try:
                processed = await self.tick()
            except AegisError as exc:
                logger.warning("watch_tick_failed", chain=self.chain, error=safe_error(exc))
                await self._sleep(self._interval)
                continue
            if processed:
                elapsed = self._clock() - started
                await self._sleep(max(0.0, self._interval - elapsed - poll))
            else:
                await self._sleep(poll)
        for state in self._state.values():
            if state.rescan_task is not None and not state.rescan_task.done():
                state.rescan_task.cancel()
