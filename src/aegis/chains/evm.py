"""Generic async EVM chain provider with retries, timeouts, concurrency limit and RPC fallback."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar, cast

import aiohttp
from eth_typing import BlockIdentifier, HexStr
from pydantic import SecretStr
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import (
    BadResponseFormat,
    BlockNotFound,
    ContractLogicError,
    ProviderConnectionError,
    TransactionNotFound,
    Web3RPCError,
)
from web3.types import FilterParams, LogReceipt, TopicFilter, TxParams

from aegis.chains.multicall import chunked, decode_aggregate3, encode_aggregate3
from aegis.chains.ratelimit import NoopRateLimiter, bucket_key, method_cost
from aegis.config.chains import ChainConfig
from aegis.core.abi import checksum
from aegis.core.errors import (
    ChainDataNotFoundError,
    ConfigurationError,
    ContractCallError,
    HistoricalStateUnavailableError,
    LogRangeTooLargeError,
    RpcError,
)
from aegis.core.interfaces import BaseChainProvider, BlockId, IRateLimiter
from aegis.core.logging import get_logger
from aegis.core.models import Call, CallResult, LogEntry, LogFilter, TxReceiptInfo, TxTemplate
from aegis.core.redact import redact, safe_error
from aegis.core.retry import retry_async

T = TypeVar("T")
Client = AsyncWeb3[AsyncHTTPProvider]

_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    ProviderConnectionError,
    BadResponseFormat,
    Web3RPCError,
    OSError,
)

_FAILOVER_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, *_TRANSIENT_ERRORS)

# Oversized eth_getLogs ranges come back as bare HTTP 400/413 (dRPC) or 500 (public OP nodes).
# A real outage also shrinks the window, but the indexer then gives up and restarts at full size.
_LOG_RANGE_HTTP_STATUSES = frozenset({400, 413, 500})
RATE_LIMIT_MAX_DELAY_SECONDS = 10.0
BLOCK_TIME_CACHE_SIZE = 10_000

_LOG_RANGE_HINTS = (
    "block range",
    "range is too large",
    "range too large",
    "exceed",
    "more than",
    "too many",
    "limited to",
    "response size",
    "query timeout",
)

logger = get_logger("chains.evm")


def _as_int(value: object) -> int:
    """Receipt fields can be ints or hex strings (chain-specific extras such as `l1Fee`)."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    if isinstance(value, bytes):
        return int.from_bytes(value, "big")
    return int(str(value))


def _rate_limit_delay(exc: BaseException, attempt: int) -> float | None:
    """Back off properly on HTTP 429: honour `Retry-After`, else 1 s, 2 s, 4 s (max 10 s)."""
    if not isinstance(exc, aiohttp.ClientResponseError) or exc.status != 429:
        return None
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header is not None:
        with contextlib.suppress(ValueError):
            return min(RATE_LIMIT_MAX_DELAY_SECONDS, max(0.0, float(header)))
    return min(RATE_LIMIT_MAX_DELAY_SECONDS, float(2**attempt))


_MISSING_STATE_HINTS = (
    "missing trie node",
    "header not found",
    "state not available",
    "historical state",
    "state is not available",
    "required historical state",
    "pruned",
    "archive",
)


def _is_missing_state_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _MISSING_STATE_HINTS)


def _is_log_range_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _LOG_RANGE_HINTS)


def _to_log_entry(log: LogReceipt) -> LogEntry:
    return LogEntry(
        address=checksum(log["address"]),
        topics=tuple(bytes(topic) for topic in log["topics"]),
        data=bytes(log["data"]),
        block_number=int(log["blockNumber"]),
        tx_hash="0x" + bytes(log["transactionHash"]).hex(),
        log_index=int(log["logIndex"]),
    )


# Default web3 middleware that only matters when *sending* transactions or resolving ENS names.
# `validation` alone issues two extra `eth_chainId` requests per `eth_call` (3 round trips instead
# of 1), which triples latency and RPC usage on the hot path.
_UNNEEDED_MIDDLEWARE = ("validation", "ens_name_to_address", "gas_price_strategy", "gas_estimate")


def _make_client(url: SecretStr) -> Client:
    """Lean read-only AsyncWeb3 client: one HTTP request per RPC call."""
    client = AsyncWeb3(
        AsyncHTTPProvider(url.get_secret_value(), exception_retry_configuration=None)
    )
    for name in _UNNEEDED_MIDDLEWARE:
        with contextlib.suppress(ValueError, KeyError):
            client.middleware_onion.remove(name)
    return client


class EvmChainProvider(BaseChainProvider):
    """Read-only provider for any EVM chain without an L1 data fee.

    Args:
        config: Static chain configuration.
        rpc_urls: Ordered RPC endpoints; later ones are fallbacks.
        timeout_seconds: Per-attempt request timeout.
        max_retries: Retries per endpoint for transient errors.
        max_concurrency: Max in-flight RPC requests for this chain.
        multicall_batch_size: Max calls per `aggregate3` request.
        rate_limiter: Shared compute-unit budget (see `aegis.chains.ratelimit`).
        high_priority: True for latency-critical callers (the watcher), which may use the
            budget reserve that background work (indexer, scheduled scans) must leave free.
        log_rpc_urls: Endpoints tried first for `eth_getLogs` only (e.g. free public nodes with
            wide block ranges, while the metered provider allows 10 blocks); `rpc_urls` remain
            their fallback. Other methods never use them.
        log_rate_limiter: Budget for the `log_rpc_urls` buckets (defaults to `rate_limiter`).
    """

    def __init__(
        self,
        config: ChainConfig,
        rpc_urls: Sequence[SecretStr],
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        max_concurrency: int = 8,
        multicall_batch_size: int = 150,
        rate_limiter: IRateLimiter | None = None,
        high_priority: bool = False,
        log_rpc_urls: Sequence[SecretStr] = (),
        log_rate_limiter: IRateLimiter | None = None,
    ) -> None:
        super().__init__(config)
        if not rpc_urls:
            raise ConfigurationError(f"no RPC URLs configured for chain '{config.name}'")
        main = {url.get_secret_value().rstrip("/") for url in rpc_urls}
        log_only = [url for url in log_rpc_urls if url.get_secret_value().rstrip("/") not in main]
        urls = [*rpc_urls, *log_only]
        self._clients: list[Client] = [_make_client(url) for url in urls]
        self._buckets = [bucket_key(url.get_secret_value()) for url in urls]
        limiter: IRateLimiter = rate_limiter or NoopRateLimiter()
        log_limiter = log_rate_limiter or limiter
        self._limiters = [limiter] * len(rpc_urls) + [log_limiter] * len(log_only)
        main_route = list(range(len(rpc_urls)))
        log_route = list(range(len(rpc_urls), len(urls))) + main_route
        # Each route remembers its own healthy endpoint, so logs served by a public node never
        # pull latency-critical calls away from the primary provider.
        self._routes: dict[str, list[int]] = {"default": main_route, "eth_getLogs": log_route}
        self._active: dict[str, int] = dict.fromkeys(self._routes, 0)
        self._high_priority = high_priority
        self._timeout = timeout_seconds
        self._attempts = max_retries + 1
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._batch_size = multicall_batch_size
        self._block_times: dict[int, int] = {}

    async def _request(self, op: str, fn: Callable[[Client], Awaitable[T]]) -> T:
        """Run `fn` against the active endpoint, falling back to the others on failure."""
        errors: list[str] = []
        deterministic: list[RpcError] = []
        not_found = False
        cost = method_cost(op)
        route_name = op if op in self._routes else "default"
        route = self._routes[route_name]
        async with self._semaphore:
            for offset in range(len(route)):
                position = (self._active[route_name] + offset) % len(route)
                index = route[position]
                client = self._clients[index]
                bucket = self._buckets[index]
                limiter = self._limiters[index]

                async def wait_budget(
                    bucket: str = bucket, limiter: IRateLimiter = limiter
                ) -> None:
                    await limiter.acquire(bucket, cost, high_priority=self._high_priority)

                try:
                    result = await retry_async(
                        lambda client=client: fn(client),  # type: ignore[misc]
                        attempts=self._attempts,
                        timeout_seconds=self._timeout,
                        retry_on=_TRANSIENT_ERRORS,
                        before_attempt=wait_budget,
                        retry_delay=_rate_limit_delay,
                    )
                except ContractLogicError as exc:
                    raise ContractCallError(f"{op} reverted: {redact(str(exc))}") from None
                except (LogRangeTooLargeError, HistoricalStateUnavailableError) as exc:
                    # Another endpoint may accept the range / be an archive node: try it first.
                    deterministic.append(exc)
                    errors.append(f"endpoint#{index}: {safe_error(exc)}")
                    continue
                except (TransactionNotFound, BlockNotFound) as exc:
                    not_found = True
                    errors.append(f"endpoint#{index}: {type(exc).__name__}")
                    continue
                except _FAILOVER_ERRORS as exc:
                    errors.append(f"endpoint#{index}: {safe_error(exc)}")
                    logger.warning(
                        "rpc_endpoint_failed",
                        chain=self.name,
                        op=op,
                        endpoint=index,
                        error=safe_error(exc),
                    )
                    continue
                self._active[route_name] = position
                return result
        summary = f"{self.name} {op} failed on all endpoints: {'; '.join(errors)}"
        if deterministic and len(deterministic) == len(errors):
            raise type(deterministic[0])(summary)
        if not_found:
            raise ChainDataNotFoundError(summary)
        raise RpcError(summary)

    async def block_number(self) -> int:
        """Latest block number."""

        async def fn(client: Client) -> int:
            return int(await client.eth.block_number)

        return await self._request("eth_blockNumber", fn)

    async def gas_price(self) -> int:
        """Current gas price in wei."""

        async def fn(client: Client) -> int:
            return int(await client.eth.gas_price)

        return await self._request("eth_gasPrice", fn)

    async def call(self, to: str, data: bytes, block: BlockId = "latest") -> bytes:
        """`eth_call` returning raw bytes.

        Raises:
            ContractCallError: if the call reverts.
            RpcError: if every endpoint fails.
        """
        params: TxParams = {"to": checksum(to), "data": data}

        async def fn(client: Client) -> bytes:
            try:
                return bytes(
                    await client.eth.call(params, block_identifier=cast(BlockIdentifier, block))
                )
            except Web3RPCError as exc:
                if isinstance(block, int) and _is_missing_state_error(exc):
                    raise HistoricalStateUnavailableError(redact(str(exc))) from None
                raise

        return await self._request("eth_call", fn)

    async def multicall(self, calls: Sequence[Call], block: BlockId = "latest") -> list[CallResult]:
        """Batch calls through Multicall3 `aggregate3` (chunks run concurrently)."""
        if not calls:
            return []
        batches = chunked(calls, self._batch_size)
        raw = await asyncio.gather(
            *(
                self.call(self.config.multicall3, encode_aggregate3(batch), block)
                for batch in batches
            )
        )
        results: list[CallResult] = []
        for data in raw:
            results.extend(decode_aggregate3(data))
        if len(results) != len(calls):
            raise RpcError(f"{self.name} multicall returned {len(results)}/{len(calls)} results")
        return results

    async def get_logs(
        self, log_filter: LogFilter, from_block: int, to_block: int
    ) -> list[LogEntry]:
        """`eth_getLogs` for an inclusive block range.

        Raises:
            LogRangeTooLargeError: if the node rejects the range (caller should shrink it).
        """
        params: FilterParams = {
            "address": checksum(log_filter.address),
            "topics": cast(
                list[TopicFilter],
                [list(t) if isinstance(t, tuple) else t for t in log_filter.topics],
            ),
            "fromBlock": from_block,
            "toBlock": to_block,
        }

        async def fn(client: Client) -> list[LogEntry]:
            try:
                logs = await client.eth.get_logs(params)
            except Web3RPCError as exc:
                if _is_log_range_error(exc):
                    raise LogRangeTooLargeError(redact(str(exc))) from None
                raise
            except aiohttp.ClientResponseError as exc:
                # Some providers (e.g. dRPC) answer an oversized range with a bare HTTP 400/413.
                if exc.status in _LOG_RANGE_HTTP_STATUSES:
                    raise LogRangeTooLargeError(f"HTTP {exc.status} for eth_getLogs") from None
                raise
            return [_to_log_entry(log) for log in logs]

        return await self._request("eth_getLogs", fn)

    async def l1_data_fee(self, tx: TxTemplate) -> int:
        """No L1 data fee on a generic EVM chain."""
        return 0

    async def block_timestamp(self, block: int) -> int:
        """Unix timestamp of a block (cached: blocks are immutable once buried)."""
        cached = self._block_times.get(block)
        if cached is not None:
            return cached

        async def fn(client: Client) -> int:
            header = await client.eth.get_block(block)
            return int(header["timestamp"])

        timestamp = await self._request("eth_getBlockByNumber", fn)
        if len(self._block_times) > BLOCK_TIME_CACHE_SIZE:
            self._block_times.clear()
        self._block_times[block] = timestamp
        return timestamp

    async def transaction_receipt(self, tx_hash: str) -> TxReceiptInfo:
        """Receipt (sender, gas, L1 fee, position in block) plus the block timestamp."""

        async def fn(client: Client) -> Any:
            return await client.eth.get_transaction_receipt(HexStr(tx_hash))

        receipt = await self._request("eth_getTransactionReceipt", fn)
        block = int(receipt["blockNumber"])
        to = receipt.get("to")
        return TxReceiptInfo(
            tx_from=checksum(receipt["from"]),
            tx_to=checksum(to) if to else None,
            tx_index=int(receipt["transactionIndex"]),
            gas_used=int(receipt["gasUsed"]),
            effective_gas_price=_as_int(receipt.get("effectiveGasPrice")),
            l1_fee=_as_int(receipt.get("l1Fee")),
            status=int(receipt.get("status", 1)),
            block_timestamp=await self.block_timestamp(block),
            logs_in_tx=len(receipt.get("logs", ())),
            log_topic0s=tuple(
                bytes(log["topics"][0]) for log in receipt.get("logs", ()) if log.get("topics")
            ),
        )

    async def aclose(self) -> None:
        """Close HTTP sessions of all endpoints."""
        for client in self._clients:
            provider: Any = client.provider
            await provider.disconnect()
