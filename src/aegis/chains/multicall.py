"""Multicall3 `aggregate3` encoding helpers."""

from __future__ import annotations

from collections.abc import Sequence

from aegis.core.abi import AbiFunction
from aegis.core.models import Call, CallResult

AGGREGATE3 = AbiFunction("aggregate3((address,bool,bytes)[])", ("(bool,bytes)[]",))


def encode_aggregate3(calls: Sequence[Call]) -> bytes:
    """Encode a batch of calls for `Multicall3.aggregate3`."""
    return AGGREGATE3.encode([(call.target, call.allow_failure, call.data) for call in calls])


def decode_aggregate3(data: bytes) -> list[CallResult]:
    """Decode `aggregate3` return data into ordered results."""
    (results,) = AGGREGATE3.decode(data)
    return [CallResult(success=bool(ok), data=bytes(ret)) for ok, ret in results]


def chunked(calls: Sequence[Call], size: int) -> list[Sequence[Call]]:
    """Split calls into batches of at most `size`."""
    return [calls[i : i + size] for i in range(0, len(calls), size)]
