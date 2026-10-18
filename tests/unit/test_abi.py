from __future__ import annotations

import pytest

from aegis.chains.multicall import decode_aggregate3, encode_aggregate3
from aegis.core.abi import AbiFunction, event_topic, split_types, topic_to_address
from aegis.core.errors import AbiDecodingError
from aegis.core.models import Call
from aegis.protocols.aave_v3 import BORROW_TOPIC
from aegis.protocols.compound_v3 import WITHDRAW_TOPIC


def test_split_types_nested() -> None:
    assert split_types("address,(uint8,address)[],bytes") == (
        "address",
        "(uint8,address)[]",
        "bytes",
    )
    assert split_types("") == ()


def test_selector_and_roundtrip() -> None:
    fn = AbiFunction("balanceOf(address)", ("uint256",))
    data = fn.encode("0x" + "11" * 20)
    assert data[:4].hex() == "70a08231"
    assert len(data) == 36
    assert fn.decode((123).to_bytes(32, "big")) == (123,)


def test_decode_error() -> None:
    with pytest.raises(AbiDecodingError):
        AbiFunction("x()", ("uint256",)).decode(b"\x01")


def test_event_topics_match_mainnet() -> None:
    # Verified against live Aave v3 / Comet logs.
    assert BORROW_TOPIC.hex().startswith("b3d084820f")
    assert event_topic("Withdraw(address,address,uint256)") == WITHDRAW_TOPIC


def test_topic_to_address() -> None:
    topic = b"\x00" * 12 + bytes.fromhex("ab" * 20)
    assert topic_to_address(topic).lower() == "0x" + "ab" * 20


def test_aggregate3_roundtrip() -> None:
    from eth_abi.abi import encode

    calls = [Call("0x" + "11" * 20, b"\x01\x02"), Call("0x" + "22" * 20, b"", allow_failure=False)]
    encoded = encode_aggregate3(calls)
    assert encoded[:4].hex() == "82ad56cb"
    ret = encode(["(bool,bytes)[]"], [[(True, b"\xaa"), (False, b"")]])
    results = decode_aggregate3(ret)
    assert [(r.success, r.data) for r in results] == [(True, b"\xaa"), (False, b"")]
