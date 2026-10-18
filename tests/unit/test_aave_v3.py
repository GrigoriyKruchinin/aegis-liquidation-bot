from __future__ import annotations

from decimal import Decimal

from eth_abi.abi import encode

from aegis.core.models import LogEntry
from aegis.protocols.aave_v3 import (
    BORROW_TOPIC,
    GET_USER_ACCOUNT_DATA,
    AaveV3Protocol,
    EModeCategory,
    Reserve,
    ReserveConfig,
    effective_collateral_params,
    max_liquidatable_debt_usd,
    quote_pair,
)
from tests.factories import BORROWER, USDC, WETH, collateral_leg, debt_leg, position
from tests.fakes import FakeProvider

POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
PROVIDER = "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D"


def test_reserve_config_bitmap() -> None:
    bitmap = (
        8000  # ltv
        | (8250 << 16)
        | (10500 << 32)
        | (18 << 48)
        | (1 << 56)
        | (1000 << 152)
    )
    cfg = ReserveConfig.from_bitmap(bitmap)
    assert cfg.liquidation_threshold == 8250
    assert cfg.bonus_fraction == Decimal("0.05")
    assert cfg.decimals == 18
    assert cfg.active and not cfg.paused and not cfg.frozen
    assert cfg.protocol_fee_fraction == Decimal("0.1")


def test_max_liquidatable_debt_rules() -> None:
    big_debt, big_col = debt_leg(Decimal(5000)), collateral_leg(Decimal(5000))
    # HF > 0.95 and both legs >= $2000 -> capped at 50% of the TOTAL debt.
    assert max_liquidatable_debt_usd(Decimal("0.99"), big_debt, big_col, Decimal(5000)) == 2500
    assert max_liquidatable_debt_usd(Decimal("0.99"), big_debt, big_col, Decimal(10_000)) == 5000
    # Otherwise the whole reserve debt is liquidatable.
    assert max_liquidatable_debt_usd(Decimal("0.94"), big_debt, big_col, Decimal(10_000)) == 5000
    small = debt_leg(Decimal(1999))
    assert max_liquidatable_debt_usd(Decimal("0.99"), small, big_col, Decimal(9000)) == 1999
    small_col = collateral_leg(Decimal(1500))
    assert max_liquidatable_debt_usd(Decimal("0.99"), big_debt, small_col, Decimal(5000)) == 5000


def test_quote_pair_uses_total_debt_for_close_factor() -> None:
    # Two $5k debt legs, HF 0.97: the full $5k USDC leg is liquidatable (cap = 50% x $10k).
    terms = quote_pair(
        Decimal("0.97"), debt_leg(Decimal(5000)), collateral_leg(Decimal(20_000)), Decimal(10_000)
    )
    assert terms is not None
    assert terms[1] == Decimal(5000)


def test_quote_pair_respects_min_leftover_dust_rule() -> None:
    # 50% of $3000 = $1500 would leave only $925 collateral -> reduced to leave >= $1010.
    terms = quote_pair(Decimal("0.97"), debt_leg(Decimal(3000)), collateral_leg(Decimal(2500)))
    assert terms is not None
    _units, debt_usd, seized, _gross = terms
    assert Decimal(2500) - seized >= Decimal(1010) - Decimal("0.0001")
    assert Decimal(3000) - debt_usd >= Decimal(1010)
    assert debt_usd < Decimal(1500)


def _reserve(rid: int, bonus_bps: int = 10_700, lt: int = 8000) -> Reserve:
    cfg = ReserveConfig(lt, bonus_bps, 18, True, False, False, 1000)
    return Reserve(rid, WETH, "0x" + "0a" * 20, "0x" + "0b" * 20, cfg)


def test_emode_bonus_overrides_reserve_bonus() -> None:
    category = EModeCategory(1, 9500, 10_100, frozenset({3}))
    lt, bonus = effective_collateral_params(_reserve(3), category)
    assert (lt, bonus) == (9500, Decimal("0.01"))
    # Collateral outside the category keeps reserve parameters.
    lt, bonus = effective_collateral_params(_reserve(4), category)
    assert (lt, bonus) == (8000, Decimal("0.07"))
    # No eMode.
    assert effective_collateral_params(_reserve(3), None) == (8000, Decimal("0.07"))


def test_emode_enables_ltv0_collateral() -> None:
    category = EModeCategory(2, 9000, 10_200, frozenset({5}))
    lt, _bonus = effective_collateral_params(_reserve(5, lt=0), category)
    assert lt == 9000


def test_quote_pair_full_close_small_position() -> None:
    # HF 0.97, debt $1000 (< $2000 -> 100% close), collateral $5000, bonus 5%, fee 10%.
    terms = quote_pair(Decimal("0.97"), debt_leg(Decimal(1000)), collateral_leg(Decimal(5000)))
    assert terms is not None
    units, debt_usd, seized, gross = terms
    assert debt_usd == Decimal(1000)
    assert seized == Decimal(1050)
    assert gross == Decimal(45)  # 50 bonus - 10% protocol fee
    assert units == 1000 * 10**6


def test_quote_pair_half_close_large_position() -> None:
    terms = quote_pair(Decimal("0.98"), debt_leg(Decimal(10_000)), collateral_leg(Decimal(20_000)))
    assert terms is not None
    _units, debt_usd, seized, gross = terms
    assert debt_usd == Decimal(5000)
    assert seized == Decimal(5250)
    assert gross == Decimal("225.0")


def test_quote_pair_capped_by_collateral() -> None:
    terms = quote_pair(Decimal("0.5"), debt_leg(Decimal(1000)), collateral_leg(Decimal(525)))
    assert terms is not None
    _units, debt_usd, seized, gross = terms
    assert seized == Decimal(525)
    assert debt_usd == Decimal(500)
    assert gross == Decimal("22.5")


def test_quote_pair_rejects_empty() -> None:
    assert quote_pair(Decimal("0.9"), debt_leg(Decimal(0)), collateral_leg(Decimal(10))) is None


def _protocol() -> tuple[AaveV3Protocol, FakeProvider]:
    provider = FakeProvider()
    protocol = AaveV3Protocol(provider, PROVIDER)
    protocol._pool = POOL
    protocol._oracle = "0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156"
    return protocol, provider


def test_borrower_from_log_uses_on_behalf_of() -> None:
    protocol, _ = _protocol()
    on_behalf = b"\x00" * 12 + bytes.fromhex("22" * 20)
    log = LogEntry(POOL, (BORROW_TOPIC, b"\x00" * 32, on_behalf, b"\x00" * 32), b"", 1, "0x", 0)
    assert protocol.borrower_from_log(log) == "0x" + "22" * 20
    wrong = LogEntry(POOL, (b"\x01" * 32, b"\x00" * 32, on_behalf), b"", 1, "0x", 0)
    assert protocol.borrower_from_log(wrong) is None


async def test_check_health_decodes_account_data() -> None:
    protocol, provider = _protocol()
    healthy = "0x" + "33" * 20
    no_debt = "0x" + "44" * 20

    def handler(data: bytes) -> bytes:
        user = "0x" + data[-20:].hex()
        if user == BORROWER.lower():
            values = [2000 * 10**8, 1000 * 10**8, 0, 8000, 7500, 97 * 10**16]
        elif user == healthy:
            values = [5000 * 10**8, 1000 * 10**8, 0, 8000, 7500, 3 * 10**18]
        else:
            values = [10**8, 0, 0, 0, 0, 2**256 - 1]
        return encode(["uint256"] * 6, values)

    provider.on(POOL, GET_USER_ACCOUNT_DATA.selector, handler)
    checks = await protocol.check_health([BORROWER, healthy, no_debt])
    by = {c.borrower: c for c in checks}
    assert by[BORROWER].liquidatable and by[BORROWER].health_factor == Decimal("0.97")
    assert by[BORROWER].debt_usd == Decimal(1000)
    assert not by[healthy].liquidatable
    assert by[no_debt].health_factor is None and not by[no_debt].liquidatable


def test_quotes_build_liquidation_call() -> None:
    protocol, _ = _protocol()
    pos = position(Decimal("0.97"), (debt_leg(Decimal(1000)),), (collateral_leg(Decimal(5000)),))
    quotes = protocol.quote_liquidations(pos)
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.debt_asset == USDC
    assert quote.gross_profit_usd == Decimal(45)
    assert quote.tx.to == POOL
    assert quote.tx.data[:4].hex() == "00a718a9"  # liquidationCall selector


async def test_load_positions_applies_user_emode() -> None:
    from aegis.protocols.aave_v3 import (
        BALANCE_OF,
        GET_ASSETS_PRICES,
        GET_USER_CONFIGURATION,
        GET_USER_EMODE,
    )

    protocol, provider = _protocol()
    usdc_reserve = Reserve(0, USDC, "0x" + "a0" * 20, "0x" + "d0" * 20, _reserve(0).config)
    weth_reserve = Reserve(1, WETH, "0x" + "a1" * 20, "0x" + "d1" * 20, _reserve(1).config)
    protocol._reserves = [usdc_reserve, weth_reserve]
    protocol._emodes = {1: EModeCategory(1, 9300, 10_100, frozenset({1}))}
    other = "0x" + "77" * 20

    provider.on(
        protocol.oracle,
        GET_ASSETS_PRICES.selector,
        lambda _: encode(["uint256[]"], [[10**8, 2000 * 10**8]]),
    )
    provider.on(
        POOL,
        GET_USER_ACCOUNT_DATA.selector,
        lambda _: encode(["uint256"] * 6, [2000 * 10**8, 1000 * 10**8, 0, 0, 0, 98 * 10**16]),
    )
    # borrows USDC (bit 0), uses WETH as collateral (bit 3)
    provider.on(POOL, GET_USER_CONFIGURATION.selector, lambda _: encode(["uint256"], [0b1001]))

    def emode(data: bytes) -> bytes:
        return encode(["uint256"], [1 if data[-20:].hex() == BORROWER[2:] else 0])

    provider.on(POOL, GET_USER_EMODE.selector, emode)
    provider.on(
        usdc_reserve.variable_debt_token,
        BALANCE_OF.selector,
        lambda _: encode(["uint256"], [1000 * 10**6]),
    )
    provider.on(weth_reserve.a_token, BALANCE_OF.selector, lambda _: encode(["uint256"], [10**18]))

    positions = await protocol.load_positions([BORROWER, other], block=77)
    by = {p.borrower.lower(): p for p in positions}
    assert by[BORROWER.lower()].collateral[0].liquidation_bonus == Decimal("0.01")  # eMode
    assert by[other.lower()].collateral[0].liquidation_bonus == Decimal("0.07")  # reserve
    assert by[BORROWER.lower()].debt[0].value_usd == Decimal(1000)
    assert by[BORROWER.lower()].health_factor == Decimal("0.98")
