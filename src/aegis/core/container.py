"""Composition root: wires settings, providers, protocols, repositories and services.

This is the only module that knows concrete implementations (Dependency Inversion).
Adding a chain or protocol never requires editing this file: register it in
`aegis.chains.CHAIN_PROVIDERS` / `aegis.protocols.PROTOCOLS` and configure it in
`aegis.config.chains`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aegis.chains import CHAIN_PROVIDERS
from aegis.config.chains import CHAINS
from aegis.config.settings import AppSettings
from aegis.core.interfaces import (
    BaseChainProvider,
    BaseLendingProtocol,
    INotificationDispatcher,
    IRateLimiter,
    ScanResult,
)
from aegis.db.repositories import (
    SqlBorrowerRepository,
    SqlCheckpointRepository,
    SqlLiquidationEventRepository,
    SqlNotificationRepository,
    SqlOpportunityRepository,
    SqlSightingRepository,
    SqlStatusRepository,
    SqlUptimeRepository,
)
from aegis.db.session import create_engine, create_session_factory
from aegis.estimator.gas import L2GasEstimator
from aegis.estimator.oracle import ChainlinkNativePriceOracle
from aegis.estimator.profit import ProfitEstimator
from aegis.market.enricher import MarketEnricher, index_markets
from aegis.market.stream import LiquidationStream
from aegis.notifier.telegram import TelegramNotifier
from aegis.protocols import PROTOCOLS, ProtocolDeps
from aegis.reporting.status import StatusReporter
from aegis.scanner.health import HealthFactorScanner
from aegis.scanner.indexer import EventBorrowerIndexer, EventIndexer
from aegis.scanner.orchestrator import ScanOrchestrator
from aegis.workers.redis_tools import (
    RedisAlertDeduplicator,
    RedisRateLimiter,
    RedisTaskLock,
    heartbeat_age_seconds,
)

DispatcherFactory = Callable[[], INotificationDispatcher]


def _build_markets(
    settings: AppSettings,
    rate_limiter: IRateLimiter | None = None,
    *,
    high_priority: bool = False,
) -> tuple[
    dict[str, BaseChainProvider],
    dict[str, list[BaseLendingProtocol]],
    dict[str, ChainlinkNativePriceOracle],
]:
    """Providers, market adapters and native oracles per enabled chain (no network I/O)."""
    providers: dict[str, BaseChainProvider] = {}
    markets: dict[str, list[BaseLendingProtocol]] = {}
    oracles: dict[str, ChainlinkNativePriceOracle] = {}
    for chain_name in settings.enabled_chains:
        chain = CHAINS[chain_name]
        provider = CHAIN_PROVIDERS.get(chain.provider_kind)(
            chain,
            settings.rpc_urls_for(chain_name),
            timeout_seconds=settings.rpc_timeout_seconds,
            max_retries=settings.rpc_max_retries,
            max_concurrency=settings.rpc_max_concurrency,
            multicall_batch_size=settings.multicall_batch_size,
            rate_limiter=rate_limiter,
            high_priority=high_priority,
        )
        oracle = ChainlinkNativePriceOracle(provider, chain.native_usd_feed)
        deps = ProtocolDeps(native_oracle=oracle)
        providers[chain_name] = provider
        oracles[chain_name] = oracle
        markets[chain_name] = [
            market for key in PROTOCOLS for market in PROTOCOLS.get(key)(provider, chain, deps)
        ]
    return providers, markets, oracles


def describe_markets(settings: AppSettings) -> dict[str, list[str]]:
    """Human-readable market names per chain, without opening DB/Redis/HTTP connections."""
    _providers, markets, _oracles = _build_markets(settings)
    return {chain: [m.market.name for m in items] for chain, items in markets.items()}


@dataclass(slots=True)
class Container:
    """Holds every long-lived service of a process."""

    settings: AppSettings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    providers: dict[str, BaseChainProvider]
    markets: dict[str, list[BaseLendingProtocol]]
    orchestrator: ScanOrchestrator
    notifier: TelegramNotifier
    opportunities: SqlOpportunityRepository
    notifications: SqlNotificationRepository
    locks: RedisTaskLock
    deduplicator: RedisAlertDeduplicator
    status_reporter: StatusReporter
    enricher: MarketEnricher
    uptime: SqlUptimeRepository
    closed: bool = field(default=False)

    @classmethod
    def build(
        cls,
        settings: AppSettings,
        dispatcher_factory: DispatcherFactory,
        *,
        rpc_high_priority: bool = False,
    ) -> Container:
        """Construct all services (no network I/O happens here).

        Args:
            settings: Application settings.
            dispatcher_factory: Builds the notification dispatcher.
            rpc_high_priority: True for the latency-critical watcher process: its RPC calls may
                use the reserved part of the shared compute-unit budget.
        """
        engine = create_engine(settings.database_url)
        sessions = create_session_factory(engine)
        redis = Redis.from_url(settings.redis_url)

        borrowers = SqlBorrowerRepository(sessions)
        checkpoints = SqlCheckpointRepository(sessions)
        opportunities = SqlOpportunityRepository(sessions)
        deduplicator = RedisAlertDeduplicator(redis, settings.alert_dedup_ttl_seconds)

        limiter = (
            RedisRateLimiter(redis, settings.rpc_cu_per_second, settings.rpc_reserve_pct)
            if settings.rpc_cu_per_second > 0
            else None
        )
        providers, markets, oracles = _build_markets(
            settings, limiter, high_priority=rpc_high_priority
        )
        estimators = {
            name: ProfitEstimator(
                L2GasEstimator(providers[name], oracles[name]),
                min_gross_usd=settings.min_profit_usd,
            )
            for name in providers
        }

        indexer = EventBorrowerIndexer(
            borrowers,
            checkpoints,
            lookback_days=settings.indexer_lookback_days,
            chunk_blocks=settings.indexer_log_chunk_blocks,
            max_blocks_per_run=settings.indexer_max_blocks_per_run,
        )
        scanner = _PerChainScanner(
            {
                name: HealthFactorScanner(borrowers, estimator, min_debt_usd=settings.min_debt_usd)
                for name, estimator in estimators.items()
            }
        )
        events = SqlLiquidationEventRepository(sessions)
        sightings = SqlSightingRepository(sessions)
        uptime = SqlUptimeRepository(sessions)
        liquidation_indexer = EventIndexer(
            checkpoints,
            LiquidationStream(events),
            lookback_days=settings.market_lookback_days,
            chunk_blocks=settings.market_log_chunk_blocks,
            max_blocks_per_run=settings.market_max_blocks_per_run,
        )
        orchestrator = ScanOrchestrator(
            markets,
            indexer,
            scanner,
            borrowers,
            opportunities,
            deduplicator,
            dispatcher_factory(),
            min_profit_usd=settings.min_profit_usd,
            watchlist_hf_threshold=settings.watchlist_hf_threshold,
            sightings=sightings if settings.market_enabled else None,
            liquidation_indexer=liquidation_indexer if settings.market_enabled else None,
        )
        enricher = MarketEnricher(
            index_markets(markets),
            oracles,
            events,
            sightings,
            uptime,
            lag_window_seconds=settings.market_lag_window_minutes * 60,
            lag_min_gross_usd=settings.market_lag_min_gross_usd,
        )
        return cls(
            settings=settings,
            engine=engine,
            sessions=sessions,
            redis=redis,
            providers=providers,
            markets=markets,
            orchestrator=orchestrator,
            notifier=TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id),
            opportunities=opportunities,
            notifications=SqlNotificationRepository(sessions),
            locks=RedisTaskLock(redis),
            deduplicator=deduplicator,
            enricher=enricher,
            uptime=uptime,
            status_reporter=StatusReporter(
                markets,
                SqlStatusRepository(sessions),
                checkpoints,
                lambda: heartbeat_age_seconds(redis),
                hf_threshold=settings.watchlist_hf_threshold,
            ),
        )

    def market_names(self) -> dict[str, list[str]]:
        """Human-readable market names per chain (for system messages)."""
        return {chain: [m.market.name for m in markets] for chain, markets in self.markets.items()}

    async def aclose(self) -> None:
        """Release all network resources."""
        if self.closed:
            return
        self.closed = True
        for provider in self.providers.values():
            await provider.aclose()
        await self.notifier.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


class _PerChainScanner:
    """Routes each market to the scanner (and thus gas estimator) of its own chain."""

    def __init__(self, scanners: dict[str, HealthFactorScanner]) -> None:
        self._scanners = scanners

    async def scan(self, protocol: BaseLendingProtocol, borrowers: Sequence[str]) -> ScanResult:
        """Delegate to the scanner of the market's chain."""
        return await self._scanners[protocol.market.chain].scan(protocol, borrowers)
