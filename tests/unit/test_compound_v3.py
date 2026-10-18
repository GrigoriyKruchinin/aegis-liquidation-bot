from __future__ import annotations

from decimal import Decimal

from eth_abi.abi import encode

from aegis.config.chains import CometMarketConfig
from aegis.core.models import LogEntry
from aegis.protocols.compound_v3 import (
    BORROW_BALANCE_OF,
    COLLATERAL_BALANCE_OF,
    GET_PRICE,
    GET_RESERVES,
    IS_LIQUIDATABLE,
    TARGET_RESERVES,
    WITHDRAW_TOPIC,
    CometAsset,
    CompoundV3Protocol,
    collateral_discount,
)
from tests.factories import BORROWER, USDC, WETH, collateral_leg, debt_leg, position
from tests.fakes import FakeOracle, FakeProvider

COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"
BASE_FEED = "0x" + "aa" * 20
WETH_FEED = "0x" + "bb" * 20


def _protocol(denomination: str = "usd") -> tuple[CompoundV3Protocol, FakeProvider]:
    provider = FakeProvider()
    market = CometMarketConfig("cUSDCv3", COMET, denomination)  # type: ignore[arg-type]
    protocol = CompoundV3Protocol(provider, market, FakeOracle(Decimal(2000)))
    protocol._base = USDC
    protocol._base_feed = BASE_FEED
    protocol._store_front = Decimal("0.6")
    protocol._assets = [CometAsset(WETH, WETH_FEED, Decimal("0.9"), Decimal("0.95"))]
    return protocol, provider


def test_collateral_discount() -> None:
    assert collateral_discount(Decimal("0.6"), Decimal("0.95")) == Decimal("0.030")


def test_borrower_from_withdraw_log() -> None:
    protocol, _ = _protocol()
    src = b"\x00" * 12 + bytes.fromhex("55" * 20)
    log = LogEntry(COMET, (WITHDRAW_TOPIC, src, b"\x00" * 32), b"", 1, "0x", 0)
    assert protocol.borrower_from_log(log) == "0x" + "55" * 20


def _wire(provider: FakeProvider, *, borrow: int, collateral: int, liquidatable: bool) -> None:
    def price(data: bytes) -> bytes:
        feed = "0x" + data[-20:].hex()
        return encode(["uint256"], [10**8 if feed == BASE_FEED else 2000 * 10**8])

    provider.on(COMET, GET_PRICE.selector, price)
    provider.on(COMET, GET_RESERVES.selector, lambda _: encode(["int256"], [10]))
    provider.on(COMET, TARGET_RESERVES.selector, lambda _: encode(["uint256"], [100]))
    provider.on(COMET, IS_LIQUIDATABLE.selector, lambda _: encode(["bool"], [liquidatable]))
    provider.on(COMET, BORROW_BALANCE_OF.selector, lambda _: encode(["uint256"], [borrow]))
    provider.on(COMET, COLLATERAL_BALANCE_OF.selector, lambda _: encode(["uint128"], [collateral]))


async def test_check_health_computes_hf() -> None:
    protocol, provider = _protocol()
    # Debt 1000 USDC, collateral 0.5 WETH = $1000, liquidate CF 0.9 -> HF 0.9.
    _wire(provider, borrow=1000 * 10**6, collateral=5 * 10**17, liquidatable=True)
    (check,) = await protocol.check_health([BORROWER])
    assert check.liquidatable
    assert check.debt_usd == Decimal(1000)
    assert check.collateral_usd == Decimal(1000)
    assert check.health_factor == Decimal("0.9")


async def test_native_denomination_scales_prices() -> None:
    protocol, provider = _protocol("native")
    _wire(provider, borrow=10**6, collateral=0, liquidatable=False)
    (check,) = await protocol.check_health([BORROWER])
    assert check.debt_usd == Decimal(2000)  # 1 unit priced at 1 native x $2000


async def test_load_positions_and_quote() -> None:
    protocol, provider = _protocol()
    _wire(provider, borrow=1000 * 10**6, collateral=5 * 10**17, liquidatable=True)
    (pos,) = await protocol.load_positions([BORROWER], block=500)
    assert pos.block_number == 500
    assert pos.collateral[0].liquidation_bonus == Decimal("0.030")
    (quote,) = protocol.quote_liquidations(pos)
    assert quote.gross_profit_usd == Decimal("30.000")
    assert quote.debt_to_cover_usd == Decimal("970.000")
    assert quote.gas_units == 250_000 + 120_000


def test_quote_empty_when_buy_collateral_closed() -> None:
    protocol, _ = _protocol()
    protocol._buy_collateral_open = False
    pos = position(Decimal("0.9"), (debt_leg(Decimal(1000)),), (collateral_leg(Decimal(1000)),))
    assert protocol.quote_liquidations(pos) == []
