"""Orchestrator iterating registered markets per chain. Contains no protocol-specific code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Literal, TypeVar

from aegis.core.errors import AegisError
from aegis.core.interfaces import (
    BaseLendingProtocol,
    IAlertDeduplicator,
    IBorrowerIndexer,
    IBorrowerRepository,
    IHealthFactorScanner,
    IndexResult,
    INotificationDispatcher,
    IOpportunityRepository,
    ScanResult,
)
from aegis.core.logging import get_logger
from aegis.core.models import OpportunitySummary
from aegis.core.redact import safe_error

ScanMode = Literal["full", "watchlist"]
T = TypeVar("T")

logger = get_logger("scanner.orchestrator")


def alert_key(summary: OpportunitySummary) -> str:
    """Deduplication key for an opportunity (one alert per borrower per market per TTL)."""
    return (
        f"aegis:alert:{summary.chain}:{summary.protocol}:"
        f"{summary.market_address.lower()}:{summary.borrower.lower()}"
    )


class ScanOrchestrator:
    """Runs indexing and scanning for every market of a chain and dispatches alerts."""

    def __init__(
        self,
        markets: Mapping[str, Sequence[BaseLendingProtocol]],
        indexer: IBorrowerIndexer,
        scanner: IHealthFactorScanner,
        borrowers: IBorrowerRepository,
        opportunities: IOpportunityRepository,
        deduplicator: IAlertDeduplicator,
        dispatcher: INotificationDispatcher,
        *,
        min_profit_usd: Decimal,
        watchlist_hf_threshold: Decimal,
    ) -> None:
        self._markets = markets
        self._indexer = indexer
        self._scanner = scanner
        self._borrowers = borrowers
        self._opportunities = opportunities
        self._dedup = deduplicator
        self._dispatcher = dispatcher
        self._min_profit = min_profit_usd
        self._watchlist_hf = watchlist_hf_threshold

    @property
    def chains(self) -> list[str]:
        """Chains with at least one market."""
        return [chain for chain, markets in self._markets.items() if markets]

    async def _for_each_market(
        self,
        chain: str,
        op: str,
        fn: Callable[[BaseLendingProtocol], Awaitable[T]],
    ) -> list[T]:
        """Run `fn` for every market of `chain` concurrently; one failure never stops others."""
        markets = list(self._markets.get(chain, ()))
        results = await asyncio.gather(*(fn(m) for m in markets), return_exceptions=True)
        ok: list[T] = []
        for market, result in zip(markets, results, strict=True):
            if isinstance(result, AegisError):
                logger.error(f"{op}_failed", market=market.market.key, error=safe_error(result))
            elif isinstance(result, BaseException):
                logger.exception(f"{op}_crashed", market=market.market.key, exc_info=result)
            else:
                ok.append(result)
        return ok

    async def index_chain(self, chain: str) -> list[IndexResult]:
        """Discover new borrowers on all markets of `chain`."""
        return await self._for_each_market(chain, "index", self._indexer.sync)

    async def scan_chain(self, chain: str, mode: ScanMode) -> list[ScanResult]:
        """Scan all markets of `chain` (`full` = all active borrowers, `watchlist` = low HF)."""

        async def scan(protocol: BaseLendingProtocol) -> ScanResult:
            return await self.scan_market(protocol, mode)

        return await self._for_each_market(chain, f"scan_{mode}", scan)

    async def watchlist(self, protocol: BaseLendingProtocol) -> list[str]:
        """Borrowers of a market whose last known HF is below the watchlist threshold."""
        return await self._borrowers.list_watchlist(protocol.market, self._watchlist_hf)

    async def scan_market(self, protocol: BaseLendingProtocol, mode: ScanMode) -> ScanResult:
        """Scan one market (`full` = all active borrowers, `watchlist` = low HF)."""
        if mode == "full":
            borrowers = await self._borrowers.list_active(protocol.market)
        else:
            borrowers = await self.watchlist(protocol)
        return await self.scan_borrowers(protocol, borrowers)

    async def scan_borrowers(
        self, protocol: BaseLendingProtocol, borrowers: Sequence[str]
    ) -> ScanResult:
        """Scan an explicit borrower set and dispatch alerts for profitable opportunities."""
        result = await self._scanner.scan(protocol, borrowers)
        for opportunity in result.opportunities:
            await self._handle(OpportunitySummary.from_opportunity(opportunity))
        return result

    async def _handle(self, summary: OpportunitySummary) -> None:
        log = logger.bind(
            chain=summary.chain,
            protocol=summary.protocol,
            market=summary.market_name,
            borrower=summary.borrower,
        )
        if summary.net_profit_usd < self._min_profit:
            log.debug("opportunity_below_threshold", net_profit_usd=str(summary.net_profit_usd))
            return
        key = alert_key(summary)
        if not await self._dedup.acquire(key):
            log.debug("opportunity_deduplicated")
            return
        try:
            opportunity_id = await self._opportunities.add(summary)
            await self._dispatcher.dispatch(replace(summary, id=opportunity_id))
        except BaseException:
            await self._dedup.release(key)
            raise
        log.info(
            "opportunity_dispatched",
            opportunity_id=opportunity_id,
            net_profit_usd=str(summary.net_profit_usd),
            health_factor=str(summary.health_factor),
        )
