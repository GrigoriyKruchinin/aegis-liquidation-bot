"""Static per-chain configuration and protocol deployment registry.

Addresses here are public mainnet deployments. Aave v3 sub-contracts (Pool, Oracle) are
resolved at runtime through the `PoolAddressesProvider`, so only the provider is pinned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProviderKind = Literal["evm", "op_stack", "arbitrum"]
PriceDenomination = Literal["usd", "native"]

MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"


@dataclass(frozen=True, slots=True)
class CometMarketConfig:
    """A single Compound v3 (Comet) market.

    Attributes:
        name: Human-readable market name (e.g. ``cUSDCv3``).
        address: Comet proxy address.
        price_denomination: Unit of Comet `getPrice` results: ``usd`` for USD-based markets,
            ``native`` for markets whose feeds are priced in the native token (e.g. cWETHv3).
    """

    name: str
    address: str
    price_denomination: PriceDenomination = "usd"


@dataclass(frozen=True, slots=True)
class ChainConfig:
    """Static description of a supported chain."""

    name: str
    display_name: str
    chain_id: int
    provider_kind: ProviderKind
    block_time_ms: int
    native_symbol: str
    wrapped_native: str
    native_usd_feed: str
    explorer_url: str
    alchemy_network: str | None
    reorg_safety_blocks: int
    multicall3: str = MULTICALL3_ADDRESS
    aave_v3_addresses_provider: str | None = None
    compound_v3_markets: tuple[CometMarketConfig, ...] = field(default_factory=tuple)

    def blocks_for_seconds(self, seconds: int) -> int:
        """Approximate number of blocks produced in `seconds`."""
        return max(1, seconds * 1000 // self.block_time_ms)

    def address_url(self, address: str) -> str:
        """Block explorer URL for an address."""
        return f"{self.explorer_url}/address/{address}"

    def tx_url(self, tx_hash: str) -> str:
        """Block explorer URL for a transaction."""
        return f"{self.explorer_url}/tx/{tx_hash}"


BASE = ChainConfig(
    name="base",
    display_name="Base",
    chain_id=8453,
    provider_kind="op_stack",
    block_time_ms=2000,
    native_symbol="ETH",
    wrapped_native="0x4200000000000000000000000000000000000006",
    native_usd_feed="0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
    explorer_url="https://basescan.org",
    alchemy_network="base-mainnet",
    reorg_safety_blocks=10,
    aave_v3_addresses_provider="0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D",
    compound_v3_markets=(
        CometMarketConfig("cUSDCv3", "0xb125E6687d4313864e53df431d5425969c15Eb2F"),
        CometMarketConfig("cWETHv3", "0x46e6b214b524310239732D51387075E0e70970bf", "native"),
    ),
)

ARBITRUM = ChainConfig(
    name="arbitrum",
    display_name="Arbitrum One",
    chain_id=42161,
    provider_kind="arbitrum",
    block_time_ms=250,
    native_symbol="ETH",
    wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    native_usd_feed="0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
    explorer_url="https://arbiscan.io",
    alchemy_network="arb-mainnet",
    reorg_safety_blocks=40,
    aave_v3_addresses_provider="0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
    compound_v3_markets=(
        CometMarketConfig("cUSDCv3", "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf"),
        CometMarketConfig("cUSDC.ev3", "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA"),
        CometMarketConfig("cUSDTv3", "0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07"),
        CometMarketConfig("cWETHv3", "0x6f7D514bbD4aFf3BcD1140B7344b32f063dEe486", "native"),
    ),
)

OPTIMISM = ChainConfig(
    name="optimism",
    display_name="Optimism",
    chain_id=10,
    provider_kind="op_stack",
    block_time_ms=2000,
    native_symbol="ETH",
    wrapped_native="0x4200000000000000000000000000000000000006",
    native_usd_feed="0x13e3Ee699D1909E989722E753853AE30b17e08c5",
    explorer_url="https://optimistic.etherscan.io",
    alchemy_network="opt-mainnet",
    reorg_safety_blocks=10,
    aave_v3_addresses_provider="0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
    compound_v3_markets=(
        CometMarketConfig("cUSDCv3", "0x2e44e174f7D53F0212823acC11C01A11d58c5bCB"),
        CometMarketConfig("cUSDTv3", "0x995E394b8B2437aC8Ce61Ee0bC610D617962B214"),
        CometMarketConfig("cWETHv3", "0xE36A30D249f7761327fd973001A32010b521b6Fd", "native"),
    ),
)

CHAINS: dict[str, ChainConfig] = {chain.name: chain for chain in (BASE, ARBITRUM, OPTIMISM)}
