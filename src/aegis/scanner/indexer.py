"""Incremental, checkpointed on-chain event indexing (borrower discovery, liquidations)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from aegis.core.errors import LogRangeTooLargeError
from aegis.core.interfaces import (
    BaseLendingProtocol,
    IBorrowerRepository,
    ICheckpointRepository,
    IndexResult,
)
from aegis.core.logging import get_logger
from aegis.core.models import LogEntry, LogFilter
from aegis.core.redact import safe_error

SECONDS_PER_DAY = 86_400
GROW_AFTER_SUCCESSES = 10

logger = get_logger("scanner.indexer")


class LogStream(Protocol):
    """One kind of events indexed per market, with its own checkpoint."""

    name: str

    def log_filter(self, protocol: BaseLendingProtocol) -> LogFilter | None:
        """Filter for this stream, or `None` if the market does not support it."""
        ...

    async def handle(self, protocol: BaseLendingProtocol, logs: Sequence[LogEntry]) -> int:
        """Persist the logs of one chunk; returns how many new items were recorded."""
        ...


class BorrowerStream:
    """Discovers accounts that may hold debt (`BaseLendingProtocol.discovery_filter`)."""

    name = "borrowers"

    def __init__(self, borrowers: IBorrowerRepository) -> None:
        self._borrowers = borrowers

    def log_filter(self, protocol: BaseLendingProtocol) -> LogFilter | None:
        """Discovery events of the market."""
        return protocol.discovery_filter()

    async def handle(self, protocol: BaseLendingProtocol, logs: Sequence[LogEntry]) -> int:
        """Upsert discovered borrowers with the last block they were seen in."""
        found: dict[str, int] = {}
        for log in logs:
            borrower = protocol.borrower_from_log(log)
            if borrower is not None:
                found[borrower] = max(found.get(borrower, 0), log.block_number)
        if not found:
            return 0
        return await self._borrowers.upsert_discovered(protocol.market, found)


class EventIndexer:
    """Incremental `eth_getLogs` indexer for one `LogStream`, in adaptive chunks.

    The chunk size halves whenever the RPC rejects a range as too large (providers impose
    different limits) and slowly grows back after consecutive successes. Other RPC failures
    propagate without touching the chunk size. The checkpoint is saved after every chunk, so an
    interrupted run (laptop asleep, network drop) resumes where it stopped. Indexing stays
    `reorg_safety_blocks` behind the head.

    Args:
        checkpoints: Checkpoint repository (keyed by market and stream name).
        stream: What to index and where to store it.
        lookback_days: Starting point of the very first run.
        chunk_blocks: Initial (and maximum) `eth_getLogs` block range.
        max_blocks_per_run: Upper bound of blocks processed per `sync` call.
        min_chunk_blocks: Smallest range tried before giving up.
    """

    def __init__(
        self,
        checkpoints: ICheckpointRepository,
        stream: LogStream,
        *,
        lookback_days: int,
        chunk_blocks: int,
        max_blocks_per_run: int,
        min_chunk_blocks: int = 10,
    ) -> None:
        self._checkpoints = checkpoints
        self._stream = stream
        self._lookback_days = lookback_days
        self._max_chunk = chunk_blocks
        self._min_chunk = min(min_chunk_blocks, chunk_blocks)
        self._max_blocks = max_blocks_per_run
        self._chunks: dict[str, int] = {}

    async def sync(self, protocol: BaseLendingProtocol) -> IndexResult:
        """Index new events of the stream for one market up to `head - reorg_safety_blocks`."""
        await protocol.ensure_initialized()
        log_filter = self._stream.log_filter(protocol)
        if log_filter is None:
            return IndexResult(from_block=0, to_block=0, discovered=0, caught_up=True)
        market = protocol.market
        stream = self._stream.name
        provider = protocol.provider
        head = await provider.block_number() - provider.config.reorg_safety_blocks
        last = await self._checkpoints.get(market, stream)
        if last is None:
            lookback = provider.config.blocks_for_seconds(self._lookback_days * SECONDS_PER_DAY)
            start = max(0, head - lookback)
        else:
            start = last + 1
        if start > head:
            return IndexResult(from_block=start, to_block=head, discovered=0, caught_up=True)

        end = min(head, start + self._max_blocks - 1)
        chunk = self._chunks.get(market.key, self._max_chunk)
        successes = 0
        discovered = 0
        cursor = start
        while cursor <= end:
            to_block = min(end, cursor + chunk - 1)
            try:
                logs = await provider.get_logs(log_filter, cursor, to_block)
            except LogRangeTooLargeError as exc:
                # Only an explicit "range too large" shrinks the window; outages propagate
                # unchanged (the checkpoint keeps progress) so the chunk size is not poisoned.
                if chunk <= self._min_chunk:
                    raise
                chunk = max(self._min_chunk, chunk // 2)
                successes = 0
                logger.info(
                    "log_chunk_shrunk",
                    market=market.key,
                    stream=stream,
                    chunk=chunk,
                    error=safe_error(exc),
                )
                continue
            if logs:
                discovered += await self._stream.handle(protocol, logs)
            await self._checkpoints.set(market, to_block, stream)
            cursor = to_block + 1
            successes += 1
            if successes >= GROW_AFTER_SUCCESSES and chunk < self._max_chunk:
                chunk = min(self._max_chunk, chunk * 2)
                successes = 0
        self._chunks[market.key] = chunk
        result = IndexResult(
            from_block=start, to_block=end, discovered=discovered, caught_up=end >= head
        )
        logger.info(
            "index_synced",
            market=market.key,
            stream=stream,
            from_block=start,
            to_block=end,
            discovered=discovered,
            caught_up=result.caught_up,
        )
        return result


class EventBorrowerIndexer(EventIndexer):
    """`IBorrowerIndexer` for the borrower-discovery stream."""

    def __init__(
        self,
        borrowers: IBorrowerRepository,
        checkpoints: ICheckpointRepository,
        *,
        lookback_days: int,
        chunk_blocks: int,
        max_blocks_per_run: int,
        min_chunk_blocks: int = 10,
    ) -> None:
        super().__init__(
            checkpoints,
            BorrowerStream(borrowers),
            lookback_days=lookback_days,
            chunk_blocks=chunk_blocks,
            max_blocks_per_run=max_blocks_per_run,
            min_chunk_blocks=min_chunk_blocks,
        )
