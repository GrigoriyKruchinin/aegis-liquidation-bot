"""Shared helpers for lending protocol adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aegis.config.chains import ChainConfig
from aegis.core.abi import ERC20_DECIMALS, ERC20_SYMBOL, AbiFunction
from aegis.core.errors import AbiDecodingError
from aegis.core.interfaces import BaseChainProvider, BaseLendingProtocol, IPriceOracle
from aegis.core.models import Asset, Call, CallResult
from aegis.core.registry import Registry


@dataclass(frozen=True, slots=True)
class ProtocolDeps:
    """Shared dependencies injected into protocol factories."""

    native_oracle: IPriceOracle


ProtocolFactory = Callable[
    [BaseChainProvider, ChainConfig, ProtocolDeps], list[BaseLendingProtocol]
]

PROTOCOLS: Registry[ProtocolFactory] = Registry("lending protocol")


def decode_result(fn: AbiFunction, result: CallResult) -> tuple[Any, ...] | None:
    """Decode a multicall result, returning `None` on failure or undecodable data."""
    if not result.success or not result.data:
        return None
    try:
        return fn.decode(result.data)
    except AbiDecodingError:
        return None


async def load_assets(
    provider: BaseChainProvider,
    addresses: Sequence[str],
    known_decimals: dict[str, int] | None = None,
) -> dict[str, Asset]:
    """Fetch symbol and decimals for many tokens in one multicall.

    Tokens with non-standard `symbol()` (e.g. bytes32) fall back to a shortened address.
    """
    known = known_decimals or {}
    calls: list[Call] = []
    for address in addresses:
        calls.append(Call(address, ERC20_SYMBOL.encode()))
        calls.append(Call(address, ERC20_DECIMALS.encode()))
    results = await provider.multicall(calls)
    assets: dict[str, Asset] = {}
    for i, address in enumerate(addresses):
        symbol_raw = decode_result(ERC20_SYMBOL, results[2 * i])
        decimals_raw = decode_result(ERC20_DECIMALS, results[2 * i + 1])
        symbol = str(symbol_raw[0]) if symbol_raw else f"{address[:6]}…{address[-4:]}"
        if address in known:
            decimals = known[address]
        elif decimals_raw is not None:
            decimals = int(decimals_raw[0])
        else:
            raise AbiDecodingError(f"cannot read decimals() of token {address}")
        assets[address] = Asset(address=address, symbol=symbol, decimals=decimals)
    return assets
