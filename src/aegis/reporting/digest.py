"""Builds the daily liquidation market digest from enriched liquidation events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis.core.interfaces import ILiquidationEventRepository
from aegis.core.models import ChainDigest, LiquidationRecord, LiquidatorStats, MarketDigest

#: Event kinds where the liquidator is paid (Comet absorbs are protocol bookkeeping).
PAID_KINDS = ("aave_liquidation", "comet_buy")
TOP_LIQUIDATIONS = 3
TOP_LIQUIDATORS = 3


def _net(record: LiquidationRecord) -> Decimal:
    return record.net_profit_usd if record.net_profit_usd is not None else Decimal(0)


def summarize_market(
    records: Sequence[LiquidationRecord],
    *,
    since: datetime,
    until: datetime,
    pending: int,
    threshold_usd: Decimal,
) -> MarketDigest:
    """Aggregate paid liquidations of one period (pure; no I/O).

    Args:
        records: Enriched events of `PAID_KINDS` within the period.
        since: Period start (inclusive).
        until: Period end (exclusive).
        pending: Events not enriched yet.
        threshold_usd: Net profit from which a liquidation counts as meaningful.
    """
    per_chain: dict[str, list[LiquidationRecord]] = defaultdict(list)
    for record in records:
        per_chain[record.chain].append(record)
    chains = tuple(
        ChainDigest(
            chain=chain,
            liquidations=len(items),
            volume_usd=sum((r.debt_usd or Decimal(0) for r in items), Decimal(0)),
            net_profit_usd=sum((_net(r) for r in items), Decimal(0)),
        )
        for chain, items in sorted(per_chain.items())
    )

    meaningful = [
        r for r in records if r.net_profit_usd is not None and r.net_profit_usd >= threshold_usd
    ]
    # Follow-ups of a partial-liquidation series share the first event's window: no own lag.
    measured = [
        r for r in meaningful if r.lag_blocks is not None and not r.lag_capped and not r.follow_up
    ]
    by_liquidator: dict[str, list[LiquidationRecord]] = defaultdict(list)
    for record in meaningful:
        by_liquidator[record.liquidator].append(record)
    liquidators = sorted(
        (
            LiquidatorStats(
                liquidator=address,
                liquidations=len(items),
                net_profit_usd=sum((_net(r) for r in items), Decimal(0)),
                chains=tuple(sorted({r.chain for r in items})),
            )
            for address, items in by_liquidator.items()
        ),
        key=lambda s: s.net_profit_usd,
        reverse=True,
    )
    return MarketDigest(
        since=since,
        until=until,
        chains=chains,
        top_liquidations=tuple(sorted(meaningful, key=_net, reverse=True)[:TOP_LIQUIDATIONS]),
        top_liquidators=tuple(liquidators[:TOP_LIQUIDATORS]),
        meaningful=len(meaningful),
        meaningful_net_usd=sum((_net(r) for r in meaningful), Decimal(0)),
        same_block=sum(1 for r in measured if r.lag_blocks == 0),
        lag_measured=len(measured),
        pending=pending,
        threshold_usd=threshold_usd,
        unpriced=sum(1 for r in records if r.net_profit_usd is None),
    )


class MarketDigestReporter:
    """Loads a period's liquidations and summarizes them.

    Args:
        events: Liquidation event repository.
        threshold_usd: Net profit from which a liquidation counts as meaningful.
    """

    def __init__(self, events: ILiquidationEventRepository, *, threshold_usd: Decimal) -> None:
        self._events = events
        self._threshold = threshold_usd

    async def build(self, hours: int = 24, now: datetime | None = None) -> MarketDigest:
        """Digest of the last `hours` up to `now` (default: current time)."""
        until = now or datetime.now(UTC)
        since = until - timedelta(hours=hours)
        records = await self._events.enriched_between(since, until, PAID_KINDS)
        return summarize_market(
            records,
            since=since,
            until=until,
            pending=await self._events.pending_count(),
            threshold_usd=self._threshold,
        )
