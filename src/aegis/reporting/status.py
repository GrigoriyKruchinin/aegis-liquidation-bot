"""Builds the periodic status report ("the bot is alive and what it sees")."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis.core.errors import AegisError
from aegis.core.interfaces import BaseLendingProtocol, ICheckpointRepository, IStatusRepository
from aegis.core.logging import get_logger
from aegis.core.models import MarketStats, MarketStatus, StatusReport
from aegis.core.redact import safe_error

EMPTY_STATS = MarketStats(0, Decimal(0), 0, None)

logger = get_logger("reporting.status")


class StatusReporter:
    """Aggregates borrower stats, indexer progress and recent activity for every market.

    Args:
        markets: Registered markets per chain.
        stats: Read-only aggregate repository.
        checkpoints: Indexer checkpoints.
        last_scan_age: Coroutine returning seconds since the last scan heartbeat.
        hf_threshold: Health factor below which a position counts as "at risk".
    """

    def __init__(
        self,
        markets: Mapping[str, Sequence[BaseLendingProtocol]],
        stats: IStatusRepository,
        checkpoints: ICheckpointRepository,
        last_scan_age: Callable[[], Awaitable[float | None]],
        *,
        hf_threshold: Decimal,
    ) -> None:
        self._markets = markets
        self._stats = stats
        self._checkpoints = checkpoints
        self._last_scan_age = last_scan_age
        self._hf_threshold = hf_threshold

    async def _head(self, chain: str) -> int | None:
        markets = self._markets.get(chain) or ()
        if not markets:
            return None
        try:
            return await markets[0].provider.block_number()
        except AegisError as exc:
            logger.warning("status_head_unavailable", chain=chain, error=safe_error(exc))
            return None

    async def build(self, period_hours: int) -> StatusReport:
        """Build a report covering the last `period_hours`."""
        now = datetime.now(UTC)
        stats = await self._stats.market_stats(self._hf_threshold)
        chains = list(self._markets)
        heads = dict(
            zip(chains, await asyncio.gather(*(self._head(c) for c in chains)), strict=True)
        )
        rows: list[MarketStatus] = []
        for chain, protocols in self._markets.items():
            head = heads[chain]
            for protocol in protocols:
                checkpoint = await self._checkpoints.get(protocol.market)
                lag: int | None = None
                if checkpoint is not None and head is not None:
                    blocks = max(0, head - checkpoint)
                    lag = blocks * protocol.provider.config.block_time_ms // 1000
                rows.append(
                    MarketStatus(
                        chain=chain,
                        market_name=protocol.market.name,
                        stats=stats.get(protocol.market.key, EMPTY_STATS),
                        index_lag_seconds=lag,
                    )
                )
        activity = await self._stats.activity_since(now - timedelta(hours=period_hours))
        age = await self._last_scan_age()
        return StatusReport(
            generated_at=now,
            period_hours=period_hours,
            markets=tuple(rows),
            activity=activity,
            last_scan_age_seconds=None if age is None else int(age),
        )
