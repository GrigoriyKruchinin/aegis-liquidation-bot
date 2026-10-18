"""Profit estimator: picks the best self-funded liquidation quote net of gas."""

from __future__ import annotations

from decimal import Decimal

from aegis.core.interfaces import BaseLendingProtocol, IGasEstimator
from aegis.core.models import Position, ProfitEstimate

MAX_QUOTES_TO_COST = 3


class ProfitEstimator:
    """`IProfitEstimator`: net profit = liquidation bonus (USD) - total gas cost (USD).

    Quotes whose gross profit is already below `min_gross_usd` are never gas-costed (they cannot
    reach the alert threshold), which keeps RPC usage flat for unprofitable dust positions.

    Args:
        gas_estimator: Gas cost source.
        min_gross_usd: Skip quotes with less gross profit than this.
    """

    def __init__(
        self, gas_estimator: IGasEstimator, *, min_gross_usd: Decimal = Decimal(0)
    ) -> None:
        self._gas = gas_estimator
        self._min_gross = min_gross_usd

    async def estimate(
        self, protocol: BaseLendingProtocol, position: Position
    ) -> ProfitEstimate | None:
        """Return the most profitable estimate, or `None` if no quote can reach the threshold."""
        quotes = sorted(
            (
                q
                for q in protocol.quote_liquidations(position)
                if q.gross_profit_usd >= self._min_gross
            ),
            key=lambda q: q.gross_profit_usd,
            reverse=True,
        )[:MAX_QUOTES_TO_COST]
        if not quotes:
            return None
        estimates = [
            ProfitEstimate(quote=q, gas=await self._gas.estimate(q.tx, q.gas_units)) for q in quotes
        ]
        return max(estimates, key=lambda e: e.net_profit_usd)
