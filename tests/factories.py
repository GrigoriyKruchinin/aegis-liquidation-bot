"""Builders for domain objects used across tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aegis.core.models import (
    Asset,
    CollateralLeg,
    DebtLeg,
    MarketRef,
    OpportunitySummary,
    Position,
)

USDC = Asset("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
WETH = Asset("0x4200000000000000000000000000000000000006", "WETH", 18)
BORROWER = "0x1111111111111111111111111111111111111111"
MARKET = MarketRef("base", "aave_v3", "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D", "Aave v3")


def debt_leg(value_usd: Decimal, asset: Asset = USDC, price: Decimal = Decimal(1)) -> DebtLeg:
    amount = int(value_usd / price * Decimal(10) ** asset.decimals)
    return DebtLeg(asset, amount, price, value_usd)


def collateral_leg(
    value_usd: Decimal,
    bonus: Decimal = Decimal("0.05"),
    fee: Decimal = Decimal("0.1"),
    asset: Asset = WETH,
    price: Decimal = Decimal(2000),
) -> CollateralLeg:
    amount = int(value_usd / price * Decimal(10) ** asset.decimals)
    return CollateralLeg(asset, amount, price, value_usd, bonus, fee)


def position(
    hf: Decimal,
    debts: tuple[DebtLeg, ...],
    collaterals: tuple[CollateralLeg, ...],
    market: MarketRef = MARKET,
) -> Position:
    return Position(market, BORROWER, hf, collaterals, debts, block_number=123)


def summary(**overrides: object) -> OpportunitySummary:
    values: dict[str, object] = {
        "chain": "base",
        "protocol": "aave_v3",
        "market_name": "Aave v3",
        "market_address": MARKET.address,
        "borrower": BORROWER,
        "health_factor": Decimal("0.9712"),
        "debt_symbol": "USDC",
        "debt_to_cover": Decimal("1000"),
        "debt_to_cover_usd": Decimal("1000"),
        "collateral_symbol": "WETH",
        "collateral_seized_usd": Decimal("1050"),
        "total_debt_usd": Decimal("1000"),
        "total_collateral_usd": Decimal("1100"),
        "bonus_pct": Decimal("5"),
        "gross_profit_usd": Decimal("45"),
        "gas_cost_usd": Decimal("0.0312"),
        "net_profit_usd": Decimal("44.9688"),
        "block_number": 123,
        "detected_at": datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return OpportunitySummary(**values)  # type: ignore[arg-type]
