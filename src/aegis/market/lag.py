"""How long a position stayed liquidatable before someone liquidated it."""

from __future__ import annotations

from dataclasses import dataclass

from aegis.core.interfaces import BaseLendingProtocol


@dataclass(frozen=True, slots=True)
class LagResult:
    """Liquidatable window that ended with a liquidation.

    Attributes:
        since_block: First block (end-of-block state) at which the position was liquidatable.
        lag_blocks: ``event_block - since_block``; 0 means it became liquidatable and was
            liquidated within the same block (e.g. a back-run of the oracle update).
        capped: The position was already liquidatable at the start of the search window, so the
            real lag is at least `lag_blocks`.
    """

    since_block: int
    lag_blocks: int
    capped: bool


async def find_liquidatable_since(
    protocol: BaseLendingProtocol, borrower: str, event_block: int, window_blocks: int
) -> LagResult | None:
    """Binary-search the start of the liquidatable window before `event_block`.

    Assumes the position stayed liquidatable from that start until the liquidation (true for
    the vast majority of cases). Needs historical state (archive RPC) for the search window.
    Returns `None` if the adapter cannot answer.
    """
    high = event_block - 1
    at_high = await protocol.is_liquidatable_at(borrower, high)
    if at_high is None:
        return None
    if not at_high:
        return LagResult(since_block=event_block, lag_blocks=0, capped=False)
    low = max(0, event_block - window_blocks)
    if low >= high:
        return LagResult(since_block=high, lag_blocks=event_block - high, capped=True)
    if await protocol.is_liquidatable_at(borrower, low):
        return LagResult(since_block=low, lag_blocks=event_block - low, capped=True)
    # Invariant: not liquidatable at `low`, liquidatable at `high`.
    while high - low > 1:
        middle = (low + high) // 2
        if await protocol.is_liquidatable_at(borrower, middle):
            high = middle
        else:
            low = middle
    return LagResult(since_block=high, lag_blocks=event_block - high, capped=False)
