from __future__ import annotations

import time
from decimal import Decimal

import pytest
from eth_abi.abi import encode

from aegis.core.errors import PriceUnavailableError
from aegis.core.models import GasEstimate, TxTemplate
from aegis.estimator.gas import L2GasEstimator
from aegis.estimator.oracle import DECIMALS, LATEST_ROUND_DATA, ChainlinkNativePriceOracle
from aegis.estimator.profit import ProfitEstimator
from aegis.protocols.aave_v3 import AaveV3Protocol
from tests.factories import collateral_leg, debt_leg, position
from tests.fakes import FakeGasEstimator, FakeOracle, FakeProvider

FEED = "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"


def test_gas_estimate_cost() -> None:
    gas = GasEstimate(
        gas_units=500_000,
        gas_price_wei=10**9,
        l1_fee_wei=10**14,
        native_price_usd=Decimal(2000),
    )
    assert gas.total_wei == 5 * 10**14 + 10**14
    assert gas.cost_usd == Decimal("1.2")


async def test_l2_gas_estimator_applies_margin() -> None:
    provider = FakeProvider()
    provider.gas_price_wei = 1000
    provider.l1_fee_wei = 7
    estimator = L2GasEstimator(provider, FakeOracle(Decimal(3000)), gas_price_multiplier_bps=15_000)
    gas = await estimator.estimate(TxTemplate("0x" + "11" * 20, b""), 100)
    assert gas.gas_price_wei == 1500
    assert gas.l1_fee_wei == 7
    assert gas.native_price_usd == Decimal(3000)


async def test_profit_estimator_picks_best_net() -> None:
    protocol = AaveV3Protocol(FakeProvider(), "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D")
    protocol._pool = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    pos = position(
        Decimal("0.97"),
        (debt_leg(Decimal(1000)),),
        (
            collateral_leg(Decimal(5000), bonus=Decimal("0.05")),
            collateral_leg(Decimal(5000), bonus=Decimal("0.10")),
        ),
    )
    gas = FakeGasEstimator(cost_wei=10**15, native_price=Decimal(2000))  # $2 gas
    estimate = await ProfitEstimator(gas).estimate(protocol, pos)
    assert estimate is not None
    assert estimate.quote.bonus_pct == Decimal("10.00")
    assert estimate.gas.cost_usd == Decimal(2)
    assert estimate.net_profit_usd == Decimal(88)  # 100 bonus - 10% fee - $2 gas


async def test_gas_inputs_are_cached_across_quotes() -> None:
    provider = FakeProvider()
    estimator = L2GasEstimator(provider, FakeOracle())
    tx = TxTemplate("0x" + "11" * 20, b"\x00" * 164)
    for _ in range(5):
        await estimator.estimate(tx, 100)
    await estimator.estimate(TxTemplate("0x" + "11" * 20, b"\x00" * 200), 100)
    assert provider.gas_price_calls == 1
    assert provider.l1_fee_calls == 2  # one per calldata length


async def test_profit_estimator_skips_gas_below_min_gross() -> None:
    protocol = AaveV3Protocol(FakeProvider(), "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D")
    protocol._pool = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    pos = position(Decimal("0.97"), (debt_leg(Decimal(100)),), (collateral_leg(Decimal(500)),))
    gas = FakeGasEstimator()
    assert await ProfitEstimator(gas, min_gross_usd=Decimal(5)).estimate(protocol, pos) is None
    assert gas.calls == []  # gross $4.5 < $5: never gas-costed


async def test_profit_estimator_none_without_quotes() -> None:
    protocol = AaveV3Protocol(FakeProvider(), "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D")
    pos = position(Decimal("0.97"), (debt_leg(Decimal(1000)),), ())
    assert await ProfitEstimator(FakeGasEstimator()).estimate(protocol, pos) is None


def _wire_feed(provider: FakeProvider, answer: int, updated_at: int) -> None:
    provider.on(
        FEED,
        LATEST_ROUND_DATA.selector,
        lambda _: encode(
            ["uint80", "int256", "uint256", "uint256", "uint80"], [1, answer, 0, updated_at, 1]
        ),
    )
    provider.on(FEED, DECIMALS.selector, lambda _: encode(["uint8"], [8]))


async def test_chainlink_oracle_price_and_cache() -> None:
    provider = FakeProvider()
    _wire_feed(provider, 2500 * 10**8, int(time.time()))
    oracle = ChainlinkNativePriceOracle(provider, FEED)
    assert await oracle.native_price_usd() == Decimal(2500)
    _wire_feed(provider, 1, int(time.time()))
    assert await oracle.native_price_usd() == Decimal(2500)  # cached


@pytest.mark.parametrize(("answer", "age"), [(0, 0), (-5, 0), (2500 * 10**8, 10**6)])
async def test_chainlink_oracle_rejects_bad_answers(answer: int, age: int) -> None:
    provider = FakeProvider()
    _wire_feed(provider, answer, int(time.time()) - age)
    with pytest.raises(PriceUnavailableError):
        await ChainlinkNativePriceOracle(provider, FEED).native_price_usd()
