"""Health-factor scanner: batched checks -> full positions -> profit estimates."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from aegis.core.errors import AegisError
from aegis.core.interfaces import (
    BaseLendingProtocol,
    IBorrowerRepository,
    IProfitEstimator,
    ScanResult,
)
from aegis.core.logging import get_logger
from aegis.core.models import HealthCheck, LiquidationOpportunity
from aegis.core.redact import safe_error

logger = get_logger("scanner.health")


def _batches(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class HealthFactorScanner:
    """`IHealthFactorScanner` implementation (protocol-agnostic).

    Args:
        borrowers: Repository receiving the latest health snapshots.
        estimator: Profit estimator for liquidatable positions.
        min_debt_usd: Positions with less debt are ignored as dust.
        check_batch_size: Borrowers per `check_health` call.
        position_batch_size: Borrowers per `load_positions` call.
    """

    def __init__(
        self,
        borrowers: IBorrowerRepository,
        estimator: IProfitEstimator,
        *,
        min_debt_usd: Decimal,
        check_batch_size: int = 500,
        position_batch_size: int = 25,
    ) -> None:
        self._borrowers = borrowers
        self._estimator = estimator
        self._min_debt = min_debt_usd
        self._check_batch = check_batch_size
        self._position_batch = position_batch_size

    async def scan(self, protocol: BaseLendingProtocol, borrowers: Sequence[str]) -> ScanResult:
        """Check `borrowers` and return opportunities for liquidatable ones."""
        if not borrowers:
            return ScanResult(checked=0, liquidatable=0, opportunities=())
        await protocol.ensure_initialized()
        market = protocol.market
        batches = await asyncio.gather(
            *(protocol.check_health(batch) for batch in _batches(borrowers, self._check_batch))
        )
        checks: list[HealthCheck] = [check for batch in batches for check in batch]
        await self._borrowers.update_health(market, checks, self._min_debt)

        targets = [c.borrower for c in checks if c.liquidatable and c.debt_usd >= self._min_debt]
        opportunities: list[LiquidationOpportunity] = []
        for batch in _batches(targets, self._position_batch):
            try:
                positions = await protocol.load_positions(batch)
            except AegisError as exc:
                logger.warning("positions_load_failed", market=market.key, error=safe_error(exc))
                continue
            for position in positions:
                try:
                    estimate = await self._estimator.estimate(protocol, position)
                except AegisError as exc:
                    logger.warning(
                        "estimate_failed",
                        market=market.key,
                        borrower=position.borrower,
                        error=safe_error(exc),
                    )
                    continue
                if estimate is None:
                    continue
                opportunities.append(
                    LiquidationOpportunity(position, estimate, detected_at=datetime.now(UTC))
                )
        log = logger.info if targets else logger.debug  # per-block watcher scans stay quiet
        log(
            "scan_completed",
            market=market.key,
            checked=len(checks),
            liquidatable=len(targets),
            opportunities=len(opportunities),
        )
        return ScanResult(
            checked=len(checks), liquidatable=len(targets), opportunities=tuple(opportunities)
        )
