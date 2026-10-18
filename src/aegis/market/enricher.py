"""Enriches indexed liquidation events: receipt, USD value, gas, latency and our coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from aegis.core.errors import AegisError, HistoricalStateUnavailableError, RpcError
from aegis.core.interfaces import (
    BaseLendingProtocol,
    ILiquidationEventRepository,
    IPriceOracle,
    ISightingRepository,
    IUptimeRepository,
)
from aegis.core.logging import get_logger
from aegis.core.models import (
    NATIVE_DECIMALS,
    EventEnrichment,
    EventValuation,
    LiquidationEvent,
    TxReceiptInfo,
    to_units,
)
from aegis.core.redact import safe_error
from aegis.market.lag import LagResult, find_liquidatable_since

#: Events that end a position's liquidatable window (searched for the competitor lag).
LAG_KINDS = frozenset({"aave_liquidation", "comet_absorb"})
#: Events that represent a paid on-chain action (they carry a share of the tx fee).
GAS_KINDS = frozenset({"aave_liquidation", "comet_absorb", "comet_buy"})
#: A sighting episode last seen at most this long before a liquidation still counts as ongoing.
SIGHTING_GAP_SECONDS = 600
#: A liquidation of the same borrower within this window before marks a partial-liquidation
#: series; follow-ups are excluded from speed statistics (they share the first one's window).
FOLLOW_UP_WINDOW_SECONDS = 3600

logger = get_logger("market.enricher")


class MarketEnricher:
    """Turns raw liquidation events into competitor / latency / coverage data.

    Transient RPC errors (rate limits, timeouts) make the whole event retry later, up to
    `max_attempts`; only the final attempt stores an event with missing pieces, recorded in
    `data_gaps`. Nodes without archive state are detected immediately and never retried for
    the historical parts.

    Args:
        markets: Market adapters keyed by `MarketRef.key`.
        native_oracles: Native token USD price per chain (gas costing).
        events: Liquidation event repository.
        sightings: Our sightings (to compute our lead time).
        uptime: Watcher uptime (to tell "missed" from "offline").
        lag_window_seconds: Search window for the start of the liquidatable period.
        max_attempts: Give up retrying an event after this many failures.
        concurrency: Events enriched in parallel (each needs ~10-15 RPC calls).
        lag_min_gross_usd: Skip the (RPC-heavy) lag search for dust liquidations below this
            gross profit; nobody races for them, so their timing says nothing about competition.
    """

    def __init__(
        self,
        markets: Mapping[str, BaseLendingProtocol],
        native_oracles: Mapping[str, IPriceOracle],
        events: ILiquidationEventRepository,
        sightings: ISightingRepository,
        uptime: IUptimeRepository,
        *,
        lag_window_seconds: int,
        max_attempts: int = 5,
        concurrency: int = 4,
        lag_min_gross_usd: Decimal = Decimal(1),
    ) -> None:
        self._markets = markets
        self._oracles = native_oracles
        self._events = events
        self._sightings = sightings
        self._uptime = uptime
        self._lag_window_seconds = lag_window_seconds
        self._max_attempts = max_attempts
        self._concurrency = concurrency
        self._lag_min_gross = lag_min_gross_usd
        self._gas_topics: frozenset[bytes] = frozenset().union(
            *(m.gas_bearing_topics() for m in markets.values())
        )

    async def enrich_pending(self, limit: int = 25) -> int:
        """Enrich up to `limit` pending events concurrently. Returns how many succeeded."""
        semaphore = asyncio.Semaphore(self._concurrency)

        async def one(event_id: int, attempts: int, event: LiquidationEvent) -> bool:
            async with semaphore:
                return await self._enrich_one(event_id, attempts, event)

        pending = await self._events.pending(limit, self._max_attempts)
        results = await asyncio.gather(
            *(one(i, a, e) for i, a, e in pending), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):  # last-resort guard, should not happen
                logger.error("market_enrich_crashed", error=safe_error(result))
        return sum(1 for result in results if result is True)

    async def _enrich_one(self, event_id: int, attempts: int, event: LiquidationEvent) -> bool:
        protocol = self._markets.get(event.market.key)
        try:
            if protocol is None:
                raise AegisError("market not configured")
            final = attempts + 1 >= self._max_attempts
            enrichment = await self.enrich(protocol, event, final_attempt=final)
            await self._events.save_enrichment(event_id, enrichment)
            if event.borrower and event.kind in LAG_KINDS:
                await self._sightings.close(
                    event.market, event.borrower, "liquidated", enrichment.block_time
                )
            return True
        except (AegisError, SQLAlchemyError) as exc:
            logger.warning(
                "market_enrich_failed",
                market=event.market.key,
                tx=event.tx_hash,
                attempt=attempts + 1,
                error=safe_error(exc),
            )
            try:
                await self._events.mark_failed(event_id, safe_error(exc))
            except (AegisError, SQLAlchemyError) as mark_exc:
                logger.error("market_mark_failed_failed", error=safe_error(mark_exc))
            return False

    async def enrich(
        self, protocol: BaseLendingProtocol, event: LiquidationEvent, *, final_attempt: bool = True
    ) -> EventEnrichment:
        """Compute everything we want to know about one event.

        Raises:
            RpcError: on transient failures when this is not the final attempt (retry later).
        """
        await protocol.ensure_initialized()
        receipt = await protocol.provider.transaction_receipt(event.tx_hash)
        block_time = datetime.fromtimestamp(receipt.block_timestamp, UTC)
        gaps: list[str] = []

        valuation: EventValuation | None = None
        try:
            valuation = await protocol.value_liquidation(event)
        except HistoricalStateUnavailableError:
            gaps.append("no_archive_value")
        except RpcError:
            if not final_attempt:
                raise
            gaps.append("value_rpc_error")

        gas_cost_usd = await self._gas_cost(event, receipt, gaps, final_attempt)

        follow_up = False
        lag: LagResult | None = None
        if event.borrower and event.kind in LAG_KINDS:
            follow_up = await self._is_follow_up(protocol, event)
            worth_racing = (
                event.kind == "comet_absorb"  # absorbs carry no profit; the race still tells
                or valuation is None
                or valuation.gross_profit_usd >= self._lag_min_gross
            )
            if follow_up:
                gaps.append("follow_up")
            elif not worth_racing:
                gaps.append("dust")
            else:
                lag = await self._lag(protocol, event, gaps, final_attempt)

        first_seen = known = None
        if event.borrower:
            first_seen = await self._sightings.first_seen(
                event.market, event.borrower, block_time, SIGHTING_GAP_SECONDS
            )
            known = await self._events.borrower_known(
                event.market, event.borrower, event.block_number
            )
        return EventEnrichment(
            block_time=block_time,
            receipt=receipt,
            valuation=valuation,
            gas_cost_usd=gas_cost_usd,
            debt_symbol=protocol.asset_symbol(event.debt_asset),
            collateral_symbol=protocol.asset_symbol(event.collateral_asset),
            liquidatable_since_block=lag.since_block if lag else None,
            lag_blocks=lag.lag_blocks if lag else None,
            lag_capped=lag.capped if lag else None,
            our_first_seen_at=first_seen,
            bot_online=await self._uptime.was_online(event.market.chain, block_time),
            borrower_known=known,
            follow_up=follow_up,
            data_gaps=",".join(gaps)[:64] or None,
        )

    async def _gas_cost(
        self,
        event: LiquidationEvent,
        receipt: TxReceiptInfo,
        gaps: list[str],
        final_attempt: bool,
    ) -> Decimal | None:
        """This event's share of the tx fee, priced at the native price of its block."""
        if event.kind not in GAS_KINDS:
            return None  # e.g. Comet AbsorbCollateral: paid by the AbsorbDebt of the same tx
        oracle = self._oracles.get(event.market.chain)
        if oracle is None:
            return None
        actions = sum(1 for topic in receipt.log_topic0s if topic in self._gas_topics)
        try:
            price = await oracle.native_price_usd_at(event.block_number)
        except HistoricalStateUnavailableError:
            gaps.append("gas_price_now")
            price = await oracle.native_price_usd()
        except RpcError:
            if not final_attempt:
                raise
            gaps.append("gas_price_now")
            price = await oracle.native_price_usd()
        return to_units(receipt.total_fee_wei, NATIVE_DECIMALS) * price / max(1, actions)

    async def _is_follow_up(self, protocol: BaseLendingProtocol, event: LiquidationEvent) -> bool:
        assert event.borrower is not None  # noqa: S101  # checked by the caller
        window = protocol.provider.config.blocks_for_seconds(FOLLOW_UP_WINDOW_SECONDS)
        previous = await self._events.previous_liquidation_block(
            event.market, event.borrower, event.block_number, event.block_number - window
        )
        return previous is not None

    async def _lag(
        self,
        protocol: BaseLendingProtocol,
        event: LiquidationEvent,
        gaps: list[str],
        final_attempt: bool,
    ) -> LagResult | None:
        assert event.borrower is not None  # noqa: S101  # checked by the caller
        window = protocol.provider.config.blocks_for_seconds(self._lag_window_seconds)
        try:
            return await find_liquidatable_since(
                protocol, event.borrower, event.block_number, window
            )
        except HistoricalStateUnavailableError:
            gaps.append("no_archive_lag")
        except RpcError:
            if not final_attempt:
                raise
            gaps.append("lag_rpc_error")
        return None


def index_markets(
    markets: Mapping[str, Sequence[BaseLendingProtocol]],
) -> dict[str, BaseLendingProtocol]:
    """Flatten per-chain market lists into a `MarketRef.key` -> adapter map."""
    return {m.market.key: m for items in markets.values() for m in items}
