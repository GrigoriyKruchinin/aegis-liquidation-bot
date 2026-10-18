"""Minimal typed ABI helpers built on `eth_abi` (human-readable signatures, no JSON ABIs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_abi.exceptions import DecodingError
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from aegis.core.errors import AbiDecodingError


def split_types(type_list: str) -> tuple[str, ...]:
    """Split a comma-separated ABI type list, respecting nested tuple parentheses."""
    types: list[str] = []
    depth = 0
    current: list[str] = []
    for char in type_list:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            types.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        types.append(tail)
    return tuple(types)


@dataclass(frozen=True, slots=True)
class AbiFunction:
    """A contract function described by its canonical signature and output types.

    Example:
        ``AbiFunction("balanceOf(address)", ("uint256",))``
    """

    signature: str
    outputs: tuple[str, ...]
    selector: bytes = field(init=False, repr=False)
    inputs: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        open_idx = self.signature.index("(")
        object.__setattr__(self, "selector", keccak(text=self.signature)[:4])
        object.__setattr__(self, "inputs", split_types(self.signature[open_idx + 1 : -1]))

    def encode(self, *args: Any) -> bytes:
        """ABI-encode call data (selector + arguments)."""
        return self.selector + abi_encode(list(self.inputs), list(args))

    def decode(self, data: bytes) -> tuple[Any, ...]:
        """Decode return data.

        Raises:
            AbiDecodingError: if the data does not match the output types.
        """
        try:
            return tuple(abi_decode(list(self.outputs), data))
        except (DecodingError, ValueError, TypeError) as exc:
            raise AbiDecodingError(f"cannot decode {self.signature}: {exc}") from exc


def event_topic(signature: str) -> bytes:
    """Topic0 for an event signature, e.g. ``Transfer(address,address,uint256)``."""
    return keccak(text=signature)


def topic_to_address(topic: bytes) -> ChecksumAddress:
    """Extract a checksummed address from a 32-byte indexed topic."""
    if len(topic) != 32:
        raise AbiDecodingError(f"topic must be 32 bytes, got {len(topic)}")
    return to_checksum_address(topic[-20:])


def checksum(address: str) -> ChecksumAddress:
    """Return the EIP-55 checksummed form of an address."""
    return to_checksum_address(address)


# Common ERC-20 metadata functions.
ERC20_SYMBOL = AbiFunction("symbol()", ("string",))
ERC20_DECIMALS = AbiFunction("decimals()", ("uint8",))
ERC20_BALANCE_OF = AbiFunction("balanceOf(address)", ("uint256",))
