"""Generic async EVM chain provider with retries, timeouts, concurrency limit and RPC fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar, cast

import aiohttp
from eth_typing import BlockIdentifier
from pydantic import SecretStr
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import (
    BadResponseFormat,
    ContractLogicError,
    ProviderConnectionError,
    Web3RPCError,
)
from web3.types import FilterParams, LogReceipt, TopicFilter, TxParams

from aegis.chains.multicall import chunked, decode_aggregate3, encode_aggregate3
from aegis.config.chains import ChainConfig
from aegis.core.abi import checksum
from aegis.core.errors import (
    ConfigurationError,
    ContractCallError,
    LogRangeTooLargeError,
    RpcError,
)
from aegis.core.interfaces import BaseChainProvider, BlockId
from aegis.core.logging import get_logger
from aegis.core.models import Call, CallResult, LogEntry, LogFilter, TxTemplate
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

_LOG_RANGE_HTTP_STATUSES = frozenset({400, 413})

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


class EvmChainProvider(BaseChainProvider):
    """Read-only provider for any EVM chain without an L1 data fee.

    Args:
        config: Static chain configuration.
        rpc_urls: Ordered RPC endpoints; later ones are fallbacks.
        timeout_seconds: Per-attempt request timeout.
        max_retries: Retries per endpoint for transient errors.
        max_concurrency: Max in-flight RPC requests for this chain.
        multicall_batch_size: Max calls per `aggregate3` request.
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
    ) -> None:
        super().__init__(config)
        if not rpc_urls:
            raise ConfigurationError(f"no RPC URLs configured for chain '{config.name}'")
        self._clients: list[Client] = [
            AsyncWeb3(AsyncHTTPProvider(url.get_secret_value(), exception_retry_configuration=None))
            for url in rpc_urls
        ]
        self._timeout = timeout_seconds
        self._attempts = max_retries + 1
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._batch_size = multicall_batch_size
        self._active = 0

    async def _request(self, op: str, fn: Callable[[Client], Awaitable[T]]) -> T:
        """Run `fn` against the active endpoint, falling back to the others on failure."""
        errors: list[str] = []
        async with self._semaphore:
            for offset in range(len(self._clients)):
                index = (self._active + offset) % len(self._clients)
                client = self._clients[index]
                try:
                    result = await retry_async(
                        lambda client=client: fn(client),  # type: ignore[misc]
                        attempts=self._attempts,
                        timeout_seconds=self._timeout,
                        retry_on=_TRANSIENT_ERRORS,
                    )
                except ContractLogicError as exc:
                    raise ContractCallError(f"{op} reverted: {redact(str(exc))}") from None
                except LogRangeTooLargeError:
                    raise
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
                self._active = index
                return result
        raise RpcError(f"{self.name} {op} failed on all endpoints: {'; '.join(errors)}")

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
            return bytes(
                await client.eth.call(params, block_identifier=cast(BlockIdentifier, block))
            )

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
            "topics": cast(list[TopicFilter], list(log_filter.topics)),
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

    async def aclose(self) -> None:
        """Close HTTP sessions of all endpoints."""
        for client in self._clients:
            provider: Any = client.provider
            await provider.disconnect()
