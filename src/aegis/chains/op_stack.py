"""OP-stack chains (Base, Optimism): L1 data fee via the `GasPriceOracle` predeploy."""

from __future__ import annotations

from aegis.chains.evm import EvmChainProvider
from aegis.core.abi import AbiFunction
from aegis.core.errors import AegisError
from aegis.core.logging import get_logger
from aegis.core.models import TxTemplate
from aegis.core.redact import safe_error

GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"
GET_L1_FEE_UPPER_BOUND = AbiFunction("getL1FeeUpperBound(uint256)", ("uint256",))
GET_L1_FEE = AbiFunction("getL1Fee(bytes)", ("uint256",))

# Approximate bytes of an unsigned EIP-1559 tx envelope excluding calldata.
TX_ENVELOPE_OVERHEAD_BYTES = 40

logger = get_logger("chains.op_stack")


class OpStackChainProvider(EvmChainProvider):
    """Provider for OP-stack rollups."""

    async def l1_data_fee(self, tx: TxTemplate) -> int:
        """Upper-bound L1 data fee (Fjord+), falling back to `getL1Fee(bytes)`."""
        size = len(tx.data) + TX_ENVELOPE_OVERHEAD_BYTES
        try:
            raw = await self.call(GAS_PRICE_ORACLE, GET_L1_FEE_UPPER_BOUND.encode(size))
            return int(GET_L1_FEE_UPPER_BOUND.decode(raw)[0])
        except AegisError as exc:
            logger.debug("l1_fee_upper_bound_unavailable", chain=self.name, error=safe_error(exc))
        padded = tx.data + b"\x01" * TX_ENVELOPE_OVERHEAD_BYTES
        raw = await self.call(GAS_PRICE_ORACLE, GET_L1_FEE.encode(padded))
        return int(GET_L1_FEE.decode(raw)[0])
