"""Liquidation event stream for the generic `EventIndexer`."""

from __future__ import annotations

from collections.abc import Sequence

from aegis.core.interfaces import BaseLendingProtocol, ILiquidationEventRepository
from aegis.core.models import LiquidationEvent, LogEntry, LogFilter


class LiquidationStream:
    """Indexes liquidation activity (`BaseLendingProtocol.liquidation_filter`)."""

    name = "liquidations"

    def __init__(self, events: ILiquidationEventRepository) -> None:
        self._events = events

    def log_filter(self, protocol: BaseLendingProtocol) -> LogFilter | None:
        """Liquidation events of the market (None if the adapter does not support them)."""
        return protocol.liquidation_filter()

    async def handle(self, protocol: BaseLendingProtocol, logs: Sequence[LogEntry]) -> int:
        """Decode and store; duplicates are ignored by the repository."""
        decoded: list[LiquidationEvent] = []
        for log in logs:
            event = protocol.decode_liquidation(log)
            if event is not None:
                decoded.append(event)
        return await self._events.add(decoded)
