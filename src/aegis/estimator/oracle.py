"""Native token USD price from a Chainlink aggregator (used for gas costing)."""

from __future__ import annotations

import time
from decimal import Decimal

from aegis.core.abi import AbiFunction
from aegis.core.errors import PriceUnavailableError
from aegis.core.interfaces import BaseChainProvider
from aegis.core.models import Call
from aegis.protocols.base import decode_result

LATEST_ROUND_DATA = AbiFunction(
    "latestRoundData()", ("uint80", "int256", "uint256", "uint256", "uint80")
)
DECIMALS = AbiFunction("decimals()", ("uint8",))
HISTORY_CACHE_SIZE = 5_000


class ChainlinkNativePriceOracle:
    """`IPriceOracle` backed by a Chainlink `NATIVE / USD` feed, with a short cache.

    Args:
        provider: Chain provider.
        feed: Aggregator proxy address.
        max_age_seconds: Reject answers older than this (stale feed).
        cache_seconds: Reuse a fetched price for this long.
    """

    def __init__(
        self,
        provider: BaseChainProvider,
        feed: str,
        *,
        max_age_seconds: int = 26 * 3600,
        cache_seconds: float = 30.0,
    ) -> None:
        self._provider = provider
        self._feed = feed
        self._max_age = max_age_seconds
        self._cache_seconds = cache_seconds
        self._cached: tuple[float, Decimal] | None = None
        self._history: dict[int, Decimal] = {}

    async def native_price_usd(self) -> Decimal:
        """Latest validated price.

        Raises:
            PriceUnavailableError: if the answer is non-positive or stale.
        """
        now = time.monotonic()
        if self._cached is not None and now - self._cached[0] < self._cache_seconds:
            return self._cached[1]
        results = await self._provider.multicall(
            [Call(self._feed, LATEST_ROUND_DATA.encode()), Call(self._feed, DECIMALS.encode())]
        )
        round_data = decode_result(LATEST_ROUND_DATA, results[0])
        decimals = decode_result(DECIMALS, results[1])
        if round_data is None or decimals is None:
            raise PriceUnavailableError(f"{self._provider.name}: native price feed unreadable")
        _round, answer, _started, updated_at, _answered = round_data
        if int(answer) <= 0:
            raise PriceUnavailableError(f"{self._provider.name}: non-positive native price")
        if time.time() - int(updated_at) > self._max_age:
            raise PriceUnavailableError(f"{self._provider.name}: stale native price feed")
        price = Decimal(int(answer)) / (Decimal(10) ** int(decimals[0]))
        self._cached = (now, price)
        return price

    async def native_price_usd_at(self, block: int) -> Decimal:
        """Answer of the feed as of `block` (no staleness check: used for history).

        Raises:
            PriceUnavailableError: if the answer is non-positive or unreadable.
            HistoricalStateUnavailableError: if the node has no state for `block`.
        """
        cached = self._history.get(block)
        if cached is not None:
            return cached
        results = await self._provider.multicall(
            [Call(self._feed, LATEST_ROUND_DATA.encode()), Call(self._feed, DECIMALS.encode())],
            block,
        )
        round_data = decode_result(LATEST_ROUND_DATA, results[0])
        decimals = decode_result(DECIMALS, results[1])
        if round_data is None or decimals is None or int(round_data[1]) <= 0:
            raise PriceUnavailableError(
                f"{self._provider.name}: native price at {block} unreadable"
            )
        price = Decimal(int(round_data[1])) / (Decimal(10) ** int(decimals[0]))
        if len(self._history) > HISTORY_CACHE_SIZE:
            self._history.clear()
        self._history[block] = price
        return price
