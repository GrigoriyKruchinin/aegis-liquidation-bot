"""Gas cost estimation: L2 execution gas x gas price + L1 data fee, valued in USD."""

from __future__ import annotations

import asyncio
import time

from aegis.core.interfaces import BaseChainProvider, IPriceOracle
from aegis.core.models import GasEstimate, TxTemplate


class L2GasEstimator:
    """`IGasEstimator` using protocol-specific gas baselines (no `eth_estimateGas` in V1).

    The gas price and the L1 data fee are cached briefly: every quote of a scan shares them, and
    the L1 fee depends only on the calldata size, so it is cached per (target, calldata length).

    Args:
        provider: Chain provider (supplies gas price and L1 data fee).
        native_oracle: Native token USD price.
        gas_price_multiplier_bps: Safety margin applied to the gas price (10_000 = 1x).
        gas_price_cache_seconds: Reuse a fetched gas price for this long.
        l1_fee_cache_seconds: Reuse a fetched L1 data fee for this long.
    """

    def __init__(
        self,
        provider: BaseChainProvider,
        native_oracle: IPriceOracle,
        *,
        gas_price_multiplier_bps: int = 12_000,
        gas_price_cache_seconds: float = 3.0,
        l1_fee_cache_seconds: float = 30.0,
    ) -> None:
        self._provider = provider
        self._oracle = native_oracle
        self._multiplier_bps = gas_price_multiplier_bps
        self._gas_ttl = gas_price_cache_seconds
        self._l1_ttl = l1_fee_cache_seconds
        self._gas_price: tuple[float, int] | None = None
        self._l1_fees: dict[tuple[str, int], tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def _cached_gas_price(self) -> int:
        async with self._lock:
            now = time.monotonic()
            if self._gas_price is None or now - self._gas_price[0] > self._gas_ttl:
                self._gas_price = (now, await self._provider.gas_price())
            return self._gas_price[1]

    async def _cached_l1_fee(self, tx: TxTemplate) -> int:
        key = (tx.to.lower(), len(tx.data))
        async with self._lock:
            now = time.monotonic()
            cached = self._l1_fees.get(key)
            if cached is None or now - cached[0] > self._l1_ttl:
                cached = (now, await self._provider.l1_data_fee(tx))
                self._l1_fees[key] = cached
            return cached[1]

    async def estimate(self, tx: TxTemplate, gas_units: int) -> GasEstimate:
        """Estimate the total cost of `tx` assuming `gas_units` of execution gas."""
        gas_price = await self._cached_gas_price()
        l1_fee = await self._cached_l1_fee(tx)
        native_price = await self._oracle.native_price_usd()
        return GasEstimate(
            gas_units=gas_units,
            gas_price_wei=gas_price * self._multiplier_bps // 10_000,
            l1_fee_wei=l1_fee,
            native_price_usd=native_price,
        )
