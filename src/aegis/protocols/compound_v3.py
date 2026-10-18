"""Compound v3 (Comet) lending protocol adapter (read-only).

Comet liquidation is a two-step model: anyone may `absorb` an underwater account (the protocol
takes over debt and collateral), then the liquidator profits by `buyCollateral` at a discount of
``storeFrontPriceFactor * (1 - liquidationFactor)``. Buying is only possible while protocol
reserves are below `targetReserves`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from aegis.config.chains import ChainConfig, CometMarketConfig
from aegis.core.abi import AbiFunction, checksum, event_topic, topic_to_address
from aegis.core.errors import ProtocolError
from aegis.core.interfaces import BaseChainProvider, BaseLendingProtocol, BlockId, IPriceOracle
from aegis.core.logging import get_logger
from aegis.core.models import (
    WAD,
    Asset,
    Call,
    CallResult,
    CollateralLeg,
    DebtLeg,
    HealthCheck,
    LiquidationQuote,
    LogEntry,
    LogFilter,
    MarketRef,
    Position,
    TxTemplate,
    from_units,
    to_units,
)
from aegis.protocols.base import PROTOCOLS, ProtocolDeps, decode_result, load_assets

PROTOCOL_NAME = "compound_v3"

BASE_TOKEN = AbiFunction("baseToken()", ("address",))
BASE_TOKEN_PRICE_FEED = AbiFunction("baseTokenPriceFeed()", ("address",))
NUM_ASSETS = AbiFunction("numAssets()", ("uint8",))
GET_ASSET_INFO = AbiFunction(
    "getAssetInfo(uint8)",
    ("(uint8,address,address,uint64,uint64,uint64,uint64,uint128)",),
)
GET_PRICE = AbiFunction("getPrice(address)", ("uint256",))
STORE_FRONT_PRICE_FACTOR = AbiFunction("storeFrontPriceFactor()", ("uint256",))
GET_RESERVES = AbiFunction("getReserves()", ("int256",))
TARGET_RESERVES = AbiFunction("targetReserves()", ("uint256",))
IS_LIQUIDATABLE = AbiFunction("isLiquidatable(address)", ("bool",))
BORROW_BALANCE_OF = AbiFunction("borrowBalanceOf(address)", ("uint256",))
COLLATERAL_BALANCE_OF = AbiFunction("collateralBalanceOf(address,address)", ("uint128",))
ABSORB = AbiFunction("absorb(address,address[])", ())

# Withdraw(address indexed src, address indexed to, uint256 amount) — base withdrawals/borrows.
WITHDRAW_TOPIC = event_topic("Withdraw(address,address,uint256)")

PRICE_SCALE = Decimal(10**8)
ABSORB_GAS_UNITS = 250_000
BUY_COLLATERAL_GAS_UNITS = 120_000
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

logger = get_logger("protocols.compound_v3")


@dataclass(frozen=True, slots=True)
class CometAsset:
    """A Comet collateral asset."""

    asset: Asset
    price_feed: str
    liquidate_collateral_factor: Decimal
    liquidation_factor: Decimal


@dataclass(frozen=True, slots=True)
class MarketState:
    """Per-scan dynamic state: prices in USD and whether `buyCollateral` is open."""

    base_price_usd: Decimal
    asset_prices_usd: dict[str, Decimal]
    buy_collateral_open: bool


def collateral_discount(store_front_price_factor: Decimal, liquidation_factor: Decimal) -> Decimal:
    """Discount the liquidator receives in `buyCollateral` (fraction)."""
    return store_front_price_factor * (Decimal(1) - liquidation_factor)


class CompoundV3Protocol(BaseLendingProtocol):
    """Adapter for one Comet market."""

    protocol_name = PROTOCOL_NAME

    def __init__(
        self, provider: BaseChainProvider, market: CometMarketConfig, native_oracle: IPriceOracle
    ) -> None:
        super().__init__(
            provider,
            MarketRef(
                chain=provider.name,
                protocol=PROTOCOL_NAME,
                address=checksum(market.address),
                name=market.name,
            ),
        )
        self._comet = checksum(market.address)
        self._denomination = market.price_denomination
        self._native_oracle = native_oracle
        self._base: Asset | None = None
        self._base_feed: str | None = None
        self._store_front = Decimal(0)
        self._assets: list[CometAsset] = []
        self._buy_collateral_open = True

    @property
    def base(self) -> Asset:
        """Base (borrowable) asset."""
        if self._base is None:
            raise ProtocolError(f"{self.market.key}: adapter is not initialized")
        return self._base

    async def initialize(self) -> None:
        """Load base token, collateral assets and factors."""
        head = await self._provider.multicall(
            [
                Call(self._comet, BASE_TOKEN.encode(), allow_failure=False),
                Call(self._comet, BASE_TOKEN_PRICE_FEED.encode(), allow_failure=False),
                Call(self._comet, NUM_ASSETS.encode(), allow_failure=False),
                Call(self._comet, STORE_FRONT_PRICE_FACTOR.encode(), allow_failure=False),
            ]
        )
        base_raw = decode_result(BASE_TOKEN, head[0])
        feed_raw = decode_result(BASE_TOKEN_PRICE_FEED, head[1])
        num_raw = decode_result(NUM_ASSETS, head[2])
        sf_raw = decode_result(STORE_FRONT_PRICE_FACTOR, head[3])
        if base_raw is None or feed_raw is None or num_raw is None or sf_raw is None:
            raise ProtocolError(f"{self.market.key}: cannot read Comet configuration")

        infos = await self._provider.multicall(
            [Call(self._comet, GET_ASSET_INFO.encode(i)) for i in range(int(num_raw[0]))]
        )
        raw_assets: list[tuple[str, str, int, int]] = []
        for result in infos:
            decoded = decode_result(GET_ASSET_INFO, result)
            if decoded is None:
                continue
            _offset, asset, feed, _scale, _borrow_cf, liquidate_cf, liquidation_factor, _cap = (
                decoded[0]
            )
            raw_assets.append(
                (checksum(asset), checksum(feed), int(liquidate_cf), int(liquidation_factor))
            )

        base_address = checksum(base_raw[0])
        metadata = await load_assets(self._provider, [base_address, *(a[0] for a in raw_assets)])
        self._base = metadata[base_address]
        self._base_feed = checksum(feed_raw[0])
        self._store_front = Decimal(int(sf_raw[0])) / Decimal(WAD)
        self._assets = [
            CometAsset(
                asset=metadata[address],
                price_feed=feed,
                liquidate_collateral_factor=Decimal(lcf) / Decimal(WAD),
                liquidation_factor=Decimal(lf) / Decimal(WAD),
            )
            for address, feed, lcf, lf in raw_assets
        ]
        logger.info(
            "comet_initialized",
            chain=self.market.chain,
            market=self.market.name,
            base=self._base.symbol,
            assets=len(self._assets),
        )

    def discovery_filter(self) -> LogFilter:
        """Base-token `Withdraw` events (borrowing is withdrawing base below zero)."""
        return LogFilter(address=self._comet, topics=(WITHDRAW_TOPIC,))

    def borrower_from_log(self, log: LogEntry) -> str | None:
        """`src` (first indexed topic) is the account whose balance decreased."""
        if len(log.topics) < 3 or log.topics[0] != WITHDRAW_TOPIC:
            return None
        return topic_to_address(log.topics[1])

    def _state_calls(self) -> list[Call]:
        calls = [Call(self._comet, GET_PRICE.encode(self._base_feed))]
        calls += [Call(self._comet, GET_PRICE.encode(a.price_feed)) for a in self._assets]
        calls.append(Call(self._comet, GET_RESERVES.encode()))
        calls.append(Call(self._comet, TARGET_RESERVES.encode()))
        return calls

    async def _parse_state(self, results: Sequence[CallResult]) -> MarketState:
        multiplier = Decimal(1)
        if self._denomination == "native":
            multiplier = await self._native_oracle.native_price_usd()

        def price(result: CallResult) -> Decimal:
            raw = decode_result(GET_PRICE, result)
            return Decimal(int(raw[0])) / PRICE_SCALE * multiplier if raw else Decimal(0)

        base_price = price(results[0])
        if base_price <= 0:
            raise ProtocolError(f"{self.market.key}: base price unavailable")
        asset_prices = {
            a.asset.address: price(r) for a, r in zip(self._assets, results[1:], strict=False)
        }
        reserves_raw = decode_result(GET_RESERVES, results[len(self._assets) + 1])
        target_raw = decode_result(TARGET_RESERVES, results[len(self._assets) + 2])
        open_ = (
            reserves_raw is None or target_raw is None or int(reserves_raw[0]) < int(target_raw[0])
        )
        return MarketState(base_price, asset_prices, open_)

    def _account_calls(self, borrower: str) -> list[Call]:
        account = checksum(borrower)
        calls = [
            Call(self._comet, IS_LIQUIDATABLE.encode(account)),
            Call(self._comet, BORROW_BALANCE_OF.encode(account)),
        ]
        calls += [
            Call(self._comet, COLLATERAL_BALANCE_OF.encode(account, a.asset.address))
            for a in self._assets
        ]
        return calls

    async def _fetch(
        self, borrowers: Sequence[str], block: BlockId
    ) -> tuple[MarketState, list[tuple[str, bool, int, list[int]]]]:
        state_calls = self._state_calls()
        per_account = 2 + len(self._assets)
        calls = list(state_calls)
        for borrower in borrowers:
            calls += self._account_calls(borrower)
        results = await self._provider.multicall(calls, block)
        state = await self._parse_state(results[: len(state_calls)])
        accounts: list[tuple[str, bool, int, list[int]]] = []
        for i, borrower in enumerate(borrowers):
            start = len(state_calls) + i * per_account
            chunk = results[start : start + per_account]
            liq = decode_result(IS_LIQUIDATABLE, chunk[0])
            borrow = decode_result(BORROW_BALANCE_OF, chunk[1])
            if liq is None or borrow is None:
                logger.warning(
                    "comet_account_unreadable", market=self.market.key, borrower=borrower
                )
                continue
            balances = []
            for result in chunk[2:]:
                raw = decode_result(COLLATERAL_BALANCE_OF, result)
                balances.append(int(raw[0]) if raw else 0)
            accounts.append((borrower, bool(liq[0]), int(borrow[0]), balances))
        return state, accounts

    def _health(
        self, state: MarketState, borrow: int, balances: list[int]
    ) -> tuple[Decimal, Decimal, Decimal | None]:
        debt_usd = to_units(borrow, self.base.decimals) * state.base_price_usd
        collateral_usd = Decimal(0)
        capacity_usd = Decimal(0)
        for asset, amount in zip(self._assets, balances, strict=True):
            value = to_units(amount, asset.asset.decimals) * state.asset_prices_usd.get(
                asset.asset.address, Decimal(0)
            )
            collateral_usd += value
            capacity_usd += value * asset.liquidate_collateral_factor
        hf = None if borrow == 0 else capacity_usd / debt_usd if debt_usd > 0 else None
        return debt_usd, collateral_usd, hf

    async def check_health(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[HealthCheck]:
        """Batched `isLiquidatable` + balances; HF = sum(collateral x liquidateCF) / debt."""
        if not borrowers:
            return []
        state, accounts = await self._fetch(borrowers, block)
        checks: list[HealthCheck] = []
        for borrower, liquidatable, borrow, balances in accounts:
            debt_usd, collateral_usd, hf = self._health(state, borrow, balances)
            checks.append(
                HealthCheck(borrower, hf, debt_usd, collateral_usd, liquidatable and borrow > 0)
            )
        return checks

    async def load_positions(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[Position]:
        """Full positions for liquidatable accounts."""
        if not borrowers:
            return []
        state, accounts = await self._fetch(borrowers, block)
        self._buy_collateral_open = state.buy_collateral_open
        block_number = block if isinstance(block, int) else await self._provider.block_number()
        positions: list[Position] = []
        for borrower, _liq, borrow, balances in accounts:
            if borrow == 0:
                continue
            debt_usd, _collateral_usd, hf = self._health(state, borrow, balances)
            legs: list[CollateralLeg] = []
            for asset, amount in zip(self._assets, balances, strict=True):
                price = state.asset_prices_usd.get(asset.asset.address, Decimal(0))
                if amount == 0 or price <= 0:
                    continue
                legs.append(
                    CollateralLeg(
                        asset=asset.asset,
                        amount=amount,
                        price_usd=price,
                        value_usd=to_units(amount, asset.asset.decimals) * price,
                        liquidation_bonus=collateral_discount(
                            self._store_front, asset.liquidation_factor
                        ),
                    )
                )
            positions.append(
                Position(
                    market=self.market,
                    borrower=checksum(borrower),
                    health_factor=hf if hf is not None else Decimal(0),
                    collateral=tuple(legs),
                    debt=(DebtLeg(self.base, borrow, state.base_price_usd, debt_usd),),
                    block_number=int(block_number),
                )
            )
        return positions

    def quote_liquidations(self, position: Position) -> list[LiquidationQuote]:
        """Single quote: absorb the account, then buy all its collateral at the discount."""
        if not position.collateral or not position.debt:
            return []
        if not self._buy_collateral_open:
            logger.info(
                "comet_buy_collateral_closed", market=self.market.key, borrower=position.borrower
            )
            return []
        seized_usd = position.total_collateral_usd
        gross_usd = sum(
            (leg.value_usd * leg.liquidation_bonus for leg in position.collateral), Decimal(0)
        )
        if gross_usd <= 0:
            return []
        cost_usd = seized_usd - gross_usd
        base_leg = position.debt[0]
        largest = max(position.collateral, key=lambda leg: leg.value_usd)
        return [
            LiquidationQuote(
                debt_asset=base_leg.asset,
                collateral_asset=largest.asset,
                debt_to_cover=from_units(cost_usd / base_leg.price_usd, base_leg.asset.decimals),
                debt_to_cover_usd=cost_usd,
                collateral_seized_usd=seized_usd,
                gross_profit_usd=gross_usd,
                bonus_pct=gross_usd / seized_usd * 100,
                gas_units=ABSORB_GAS_UNITS + BUY_COLLATERAL_GAS_UNITS * len(position.collateral),
                tx=TxTemplate(
                    to=self._comet,
                    data=ABSORB.encode(ZERO_ADDRESS, [checksum(position.borrower)]),
                ),
                notes=("absorb + buyCollateral",),
            )
        ]


@PROTOCOLS.register(PROTOCOL_NAME)
def create_compound_v3(
    provider: BaseChainProvider, chain: ChainConfig, deps: ProtocolDeps
) -> list[BaseLendingProtocol]:
    """Factory: one adapter per configured Comet market on the chain."""
    return [
        CompoundV3Protocol(provider, market, deps.native_oracle)
        for market in chain.compound_v3_markets
    ]
