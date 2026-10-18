"""Chain providers and their registry (keyed by `ChainConfig.provider_kind`)."""

from __future__ import annotations

from aegis.chains.arbitrum import ArbitrumChainProvider
from aegis.chains.evm import EvmChainProvider
from aegis.chains.op_stack import OpStackChainProvider
from aegis.core.registry import Registry

CHAIN_PROVIDERS: Registry[type[EvmChainProvider]] = Registry("chain provider")
CHAIN_PROVIDERS.register("evm")(EvmChainProvider)
CHAIN_PROVIDERS.register("op_stack")(OpStackChainProvider)
CHAIN_PROVIDERS.register("arbitrum")(ArbitrumChainProvider)

__all__ = ["CHAIN_PROVIDERS", "ArbitrumChainProvider", "EvmChainProvider", "OpStackChainProvider"]
