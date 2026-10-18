"""Arbitrum Nitro: L1 data fee via the virtual `NodeInterface` contract."""

from __future__ import annotations

from aegis.chains.evm import EvmChainProvider
from aegis.core.abi import AbiFunction, checksum
from aegis.core.models import TxTemplate

NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"
GAS_ESTIMATE_L1_COMPONENT = AbiFunction(
    "gasEstimateL1Component(address,bool,bytes)", ("uint64", "uint256", "uint256")
)


class ArbitrumChainProvider(EvmChainProvider):
    """Provider for Arbitrum One."""

    async def l1_data_fee(self, tx: TxTemplate) -> int:
        """L1 component = `gasEstimateForL1` (in L2 gas) x L2 base fee."""
        data = GAS_ESTIMATE_L1_COMPONENT.encode(checksum(tx.to), False, tx.data)
        raw = await self.call(NODE_INTERFACE, data)
        gas_for_l1, base_fee, _l1_base_fee = GAS_ESTIMATE_L1_COMPONENT.decode(raw)
        return int(gas_for_l1) * int(base_fee)
