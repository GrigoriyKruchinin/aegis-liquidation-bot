from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

import pytest
from pydantic import SecretStr
from web3.exceptions import ContractLogicError, Web3RPCError

from aegis.chains.evm import EvmChainProvider
from aegis.config.chains import BASE
from aegis.core.errors import ContractCallError, LogRangeTooLargeError, RpcError
from aegis.core.models import LogFilter


class FakeEth:
    def __init__(self, behaviour: list[Any]) -> None:
        self.behaviour = behaviour
        self.calls = 0

    async def _next(self) -> Any:
        self.calls += 1
        item = self.behaviour.pop(0) if len(self.behaviour) > 1 else self.behaviour[0]
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def block_number(self) -> Awaitable[Any]:
        return self._next()

    async def call(self, params: Any, block_identifier: Any = None) -> Any:
        return await self._next()

    async def get_logs(self, params: Any) -> Any:
        return await self._next()


class FakeClient:
    def __init__(self, behaviour: list[Any]) -> None:
        self.eth = FakeEth(behaviour)


def make_log_provider(main: FakeClient, log: FakeClient, **kwargs: Any) -> EvmChainProvider:
    provider = EvmChainProvider(
        BASE,
        [SecretStr("https://base-mainnet.g.alchemy.com/v2/SECRET")],
        log_rpc_urls=[SecretStr("https://public.example/rpc")],
        timeout_seconds=1,
        max_retries=1,
        **kwargs,
    )
    provider._clients = cast(Any, [main, log])
    return provider


def make_provider(*clients: FakeClient) -> EvmChainProvider:
    provider = EvmChainProvider(
        BASE,
        [SecretStr(f"https://rpc{i}.example/v2/SECRET") for i in range(len(clients))],
        timeout_seconds=1,
        max_retries=1,
    )
    provider._clients = cast(Any, list(clients))
    return provider


async def test_retries_transient_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aegis.core.retry.asyncio.sleep", _no_sleep)
    client = FakeClient([OSError("reset"), 42])
    provider = make_provider(client)
    assert await provider.block_number() == 42
    assert client.eth.calls == 2


async def test_falls_back_to_second_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aegis.core.retry.asyncio.sleep", _no_sleep)
    bad, good = FakeClient([OSError("down")]), FakeClient([7])
    provider = make_provider(bad, good)
    assert await provider.block_number() == 7
    # The healthy endpoint becomes the preferred one.
    assert await provider.block_number() == 7
    assert bad.eth.calls == 2


async def test_all_endpoints_fail_without_leaking_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aegis.core.retry.asyncio.sleep", _no_sleep)
    provider = make_provider(FakeClient([OSError("https://rpc0.example/v2/SECRET refused")]))
    with pytest.raises(RpcError) as info:
        await provider.block_number()
    assert "SECRET" not in str(info.value)


async def test_revert_is_not_retried() -> None:
    client = FakeClient([ContractLogicError("execution reverted")])
    provider = make_provider(client)
    with pytest.raises(ContractCallError):
        await provider.call("0x" + "11" * 20, b"\x00\x00\x00\x00")
    assert client.eth.calls == 1


async def test_log_range_error_mapped() -> None:
    client = FakeClient([Web3RPCError("query exceeds max block range 500")])
    provider = make_provider(client)
    with pytest.raises(LogRangeTooLargeError):
        await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 10_000)
    assert client.eth.calls == 1


@pytest.mark.parametrize("status", [400, 413])
async def test_log_range_http_status_mapped(status: int) -> None:
    import aiohttp

    error = aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=status,
        message="Bad Request",
    )
    client = FakeClient([error])
    with pytest.raises(LogRangeTooLargeError):
        await make_provider(client).get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 5000)
    assert client.eth.calls == 1


async def test_l1_fee_zero_on_generic_evm() -> None:
    from aegis.core.models import TxTemplate

    provider = make_provider(FakeClient([0]))
    assert await provider.l1_data_fee(TxTemplate("0x" + "11" * 20, b"")) == 0


async def _no_sleep(_: float) -> None:
    return None


def test_clients_have_no_request_issuing_middleware() -> None:
    provider = EvmChainProvider(BASE, [SecretStr("https://rpc.example/v2/KEY")])
    names = {name for _mw, name in provider._clients[0].middleware_onion.middleware}
    assert "validation" not in names  # would add 2x eth_chainId per eth_call
    assert not names & {"ens_name_to_address", "gas_price_strategy", "gas_estimate"}


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool]] = []

    async def acquire(self, bucket: str, cost: int, *, high_priority: bool) -> None:
        self.calls.append((bucket, cost, high_priority))


async def test_rate_limiter_charged_per_attempt_with_method_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aegis.core.retry.asyncio.sleep", _no_sleep)
    limiter = RecordingLimiter()
    provider = EvmChainProvider(
        BASE,
        [SecretStr("https://base-mainnet.g.alchemy.com/v2/K")],
        rate_limiter=limiter,
        high_priority=True,
        max_retries=1,
    )
    provider._clients = cast(Any, [FakeClient([OSError("reset"), 5])])
    assert await provider.block_number() == 5
    assert len(limiter.calls) == 2  # one budget acquisition per attempt
    bucket, cost, high = limiter.calls[0]
    assert bucket.startswith("alchemy.com:") and cost == 10 and high


async def test_http_429_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy

    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("aegis.core.retry.asyncio.sleep", record)
    error = aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=429,
        message="Too Many Requests",
        headers=CIMultiDictProxy(CIMultiDict({"Retry-After": "3"})),
    )
    provider = make_provider(FakeClient([error, 9]))
    assert await provider.block_number() == 9
    assert slept == [3.0]


async def test_log_range_error_tries_next_endpoint_first() -> None:
    narrow = FakeClient([Web3RPCError("query exceeds max block range 500")])
    wide = FakeClient([[]])
    provider = make_provider(narrow, wide)
    assert await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 10_000) == []
    assert narrow.eth.calls == 1 and wide.eth.calls == 1


async def test_log_range_error_raised_when_all_endpoints_reject() -> None:
    provider = make_provider(
        FakeClient([Web3RPCError("block range too large")]),
        FakeClient([Web3RPCError("range is too large")]),
    )
    with pytest.raises(LogRangeTooLargeError):
        await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 10_000)


async def test_missing_historical_state_is_not_retried() -> None:
    from aegis.core.errors import HistoricalStateUnavailableError

    client = FakeClient([Web3RPCError("missing trie node abc (path )")])
    provider = make_provider(client)
    with pytest.raises(HistoricalStateUnavailableError):
        await provider.call("0x" + "11" * 20, b"\x00" * 4, block=123)
    assert client.eth.calls == 1


async def test_transaction_not_found_maps_to_chain_data_error() -> None:
    from web3.exceptions import TransactionNotFound

    from aegis.core.errors import ChainDataNotFoundError

    class ReceiptEth(FakeEth):
        async def get_transaction_receipt(self, tx_hash: Any) -> Any:
            raise TransactionNotFound("not found")

    client = FakeClient([0])
    client.eth = ReceiptEth([0])
    provider = make_provider(client)
    with pytest.raises(ChainDataNotFoundError):
        await provider.transaction_receipt("0x" + "ab" * 32)


async def test_get_logs_prefers_log_endpoint_other_calls_do_not() -> None:
    main, log = FakeClient([7]), FakeClient([[]])
    provider = make_log_provider(main, log)
    assert await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 50_000) == []
    assert await provider.block_number() == 7
    assert (main.eth.calls, log.eth.calls) == (1, 1)


async def test_get_logs_falls_back_to_main_without_moving_other_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aegis.core.retry.asyncio.sleep", _no_sleep)
    main, log = FakeClient([[]]), FakeClient([OSError("down")])
    provider = make_log_provider(main, log)
    assert await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 10) == []
    assert await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 10) == []
    assert log.eth.calls == 2  # sticky on the main endpoint for logs after the failure


async def test_log_endpoint_charged_to_its_own_limiter() -> None:
    main_limiter, log_limiter = RecordingLimiter(), RecordingLimiter()
    provider = make_log_provider(
        FakeClient([7]),
        FakeClient([[]]),
        rate_limiter=main_limiter,
        log_rate_limiter=log_limiter,
    )
    await provider.get_logs(LogFilter("0x" + "11" * 20, (None,)), 1, 10)
    await provider.block_number()
    assert [c[1] for c in log_limiter.calls] == [75]
    assert [c[1] for c in main_limiter.calls] == [10]
    assert log_limiter.calls[0][0] != main_limiter.calls[0][0]


def test_log_url_equal_to_main_url_is_ignored() -> None:
    url = SecretStr("https://rpc.example/v2/KEY")
    provider = EvmChainProvider(
        BASE, [url], log_rpc_urls=[SecretStr("https://rpc.example/v2/KEY/")]
    )
    assert len(provider._clients) == 1
