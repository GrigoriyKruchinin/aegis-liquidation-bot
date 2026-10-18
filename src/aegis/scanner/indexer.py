"""Incremental, checkpointed borrower discovery from on-chain events."""

from __future__ import annotations

from aegis.core.errors import LogRangeTooLargeError
from aegis.core.interfaces import (
    BaseLendingProtocol,
    IBorrowerRepository,
    ICheckpointRepository,
    IndexResult,
)
from aegis.core.logging import get_logger
from aegis.core.redact import safe_error

SECONDS_PER_DAY = 86_400
GROW_AFTER_SUCCESSES = 10

logger = get_logger("scanner.indexer")


class EventBorrowerIndexer:
    """`IBorrowerIndexer` scanning `eth_getLogs` in adaptive chunks.

    The chunk size halves whenever the RPC rejects a range as too large (providers impose
    different limits) and slowly grows back after consecutive successes. Other RPC failures
    propagate without touching the chunk size. The checkpoint is saved after every chunk, so an
    interrupted run resumes where it stopped. Indexing stays
    `reorg_safety_blocks` behind the head.

    Args:
        borrowers: Borrower repository.
        checkpoints: Checkpoint repository.
        lookback_days: Starting point of the very first run.
        chunk_blocks: Initial (and maximum) `eth_getLogs` block range.
        max_blocks_per_run: Upper bound of blocks processed per `sync` call.
        min_chunk_blocks: Smallest range tried before giving up.
    """

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
        self._borrowers = borrowers
        self._checkpoints = checkpoints
        self._lookback_days = lookback_days
        self._max_chunk = chunk_blocks
        self._min_chunk = min(min_chunk_blocks, chunk_blocks)
        self._max_blocks = max_blocks_per_run
        self._chunks: dict[str, int] = {}

    async def sync(self, protocol: BaseLendingProtocol) -> IndexResult:
        """Index new discovery events for one market up to `head - reorg_safety_blocks`."""
        await protocol.ensure_initialized()
        market = protocol.market
        provider = protocol.provider
        head = await provider.block_number() - provider.config.reorg_safety_blocks
        last = await self._checkpoints.get(market)
        if last is None:
            lookback = provider.config.blocks_for_seconds(self._lookback_days * SECONDS_PER_DAY)
            start = max(0, head - lookback)
        else:
            start = last + 1
        if start > head:
            return IndexResult(from_block=start, to_block=head, discovered=0, caught_up=True)

        end = min(head, start + self._max_blocks - 1)
        log_filter = protocol.discovery_filter()
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
                    "log_chunk_shrunk", market=market.key, chunk=chunk, error=safe_error(exc)
                )
                continue
            found: dict[str, int] = {}
            for log in logs:
                borrower = protocol.borrower_from_log(log)
                if borrower is not None:
                    found[borrower] = max(found.get(borrower, 0), log.block_number)
            if found:
                discovered += await self._borrowers.upsert_discovered(market, found)
            await self._checkpoints.set(market, to_block)
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
            from_block=start,
            to_block=end,
            discovered=discovered,
            caught_up=result.caught_up,
        )
        return result
