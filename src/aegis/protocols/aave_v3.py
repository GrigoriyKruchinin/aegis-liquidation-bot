"""Aave v3 lending protocol adapter (read-only).

Borrowers are discovered from `Borrow` events (`onBehalfOf` is the debtor). Health is
checked with batched `getUserAccountData`; full positions use the user configuration bitmap
plus aToken / variable debt token balances and `AaveOracle` prices.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from eth_abi.abi import decode as abi_decode
from eth_abi.exceptions import DecodingError

from aegis.config.chains import ChainConfig
from aegis.core.abi import AbiFunction, checksum, event_topic, topic_to_address
from aegis.core.errors import ProtocolError
from aegis.core.interfaces import BaseChainProvider, BaseLendingProtocol, BlockId
from aegis.core.logging import get_logger
from aegis.core.models import (
    WAD,
    Asset,
    Call,
    CallResult,
    CollateralLeg,
    DebtLeg,
    EventValuation,
    HealthCheck,
    LiquidationEvent,
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

PROTOCOL_NAME = "aave_v3"

# --- ABI -------------------------------------------------------------------------------------
GET_POOL = AbiFunction("getPool()", ("address",))
GET_PRICE_ORACLE = AbiFunction("getPriceOracle()", ("address",))
GET_RESERVES_LIST = AbiFunction("getReservesList()", ("address[]",))
GET_RESERVE_DATA = AbiFunction(
    "getReserveData(address)",
    (
        "(uint256)",  # configuration bitmap
        "uint128",  # liquidityIndex
        "uint128",  # currentLiquidityRate
        "uint128",  # variableBorrowIndex
        "uint128",  # currentVariableBorrowRate
        "uint128",  # currentStableBorrowRate (deprecated)
        "uint40",  # lastUpdateTimestamp
        "uint16",  # id
        "address",  # aTokenAddress
        "address",  # stableDebtTokenAddress (deprecated)
        "address",  # variableDebtTokenAddress
        "address",  # interestRateStrategyAddress
        "uint128",  # accruedToTreasury
        "uint128",  # unbacked
        "uint128",  # isolationModeTotalDebt
    ),
)
GET_USER_ACCOUNT_DATA = AbiFunction(
    "getUserAccountData(address)",
    ("uint256", "uint256", "uint256", "uint256", "uint256", "uint256"),
)
GET_USER_CONFIGURATION = AbiFunction("getUserConfiguration(address)", ("(uint256)",))
GET_ASSETS_PRICES = AbiFunction("getAssetsPrices(address[])", ("uint256[]",))
BASE_CURRENCY_UNIT = AbiFunction("BASE_CURRENCY_UNIT()", ("uint256",))
BALANCE_OF = AbiFunction("balanceOf(address)", ("uint256",))
GET_USER_EMODE = AbiFunction("getUserEMode(address)", ("uint256",))
GET_EMODE_COLLATERAL_CONFIG = AbiFunction(  # Aave v3.2+
    "getEModeCategoryCollateralConfig(uint8)", ("uint16", "uint16", "uint16")
)
GET_EMODE_COLLATERAL_BITMAP = AbiFunction(  # Aave v3.2+
    "getEModeCategoryCollateralBitmap(uint8)", ("uint128",)
)
GET_EMODE_CATEGORY_DATA = AbiFunction(  # Aave < v3.2
    "getEModeCategoryData(uint8)", ("(uint16,uint16,uint16,address,string)",)
)
LIQUIDATION_CALL = AbiFunction("liquidationCall(address,address,address,uint256,bool)", ())

# Borrow(address indexed reserve, address user, address indexed onBehalfOf, uint256 amount,
#        uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)
BORROW_TOPIC = event_topic("Borrow(address,address,address,uint256,uint8,uint256,uint16)")
# LiquidationCall(address indexed collateralAsset, address indexed debtAsset, address indexed user,
#                 uint256 debtToCover, uint256 liquidatedCollateralAmount, address liquidator,
#                 bool receiveAToken)
LIQUIDATION_CALL_TOPIC = event_topic(
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
)
LIQUIDATION_CALL_DATA = ("uint256", "uint256", "address", "bool")

# --- Protocol constants ----------------------------------------------------------------------
PERCENTAGE_FACTOR = Decimal(10_000)
CLOSE_FACTOR_HF_THRESHOLD = Decimal("0.95")
DEFAULT_CLOSE_FACTOR = Decimal("0.5")
MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD_USD = Decimal(2000)
MIN_LEFTOVER_USD = Decimal(1000)
DUST_SAFETY_MARGIN = Decimal("1.01")  # price drift between our quote and execution
DUST_EPSILON_USD = Decimal("0.01")
EMODE_MAX_CATEGORY_ID = 32
LIQUIDATION_GAS_UNITS = 450_000

logger = get_logger("protocols.aave_v3")


@dataclass(frozen=True, slots=True)
class ReserveConfig:
    """Decoded subset of the Aave v3 reserve configuration bitmap."""

    liquidation_threshold: int
    liquidation_bonus: int
    decimals: int
    active: bool
    frozen: bool
    paused: bool
    liquidation_protocol_fee: int
    emode_category: int = 0

    @classmethod
    def from_bitmap(cls, data: int) -> ReserveConfig:
        """Decode the packed `ReserveConfigurationMap`."""
        return cls(
            liquidation_threshold=(data >> 16) & 0xFFFF,
            liquidation_bonus=(data >> 32) & 0xFFFF,
            decimals=(data >> 48) & 0xFF,
            active=bool((data >> 56) & 1),
            frozen=bool((data >> 57) & 1),
            paused=bool((data >> 60) & 1),
            liquidation_protocol_fee=(data >> 152) & 0xFFFF,
            emode_category=(data >> 168) & 0xFF,  # only meaningful before v3.2
        )

    @property
    def bonus_fraction(self) -> Decimal:
        """Liquidation bonus as a fraction (10500 -> 0.05)."""
        return max(
            Decimal(0), (Decimal(self.liquidation_bonus) - PERCENTAGE_FACTOR) / PERCENTAGE_FACTOR
        )

    @property
    def protocol_fee_fraction(self) -> Decimal:
        """Share of the bonus kept by the protocol (1000 -> 0.1)."""
        return Decimal(self.liquidation_protocol_fee) / PERCENTAGE_FACTOR


@dataclass(frozen=True, slots=True)
class Reserve:
    """One Aave v3 reserve with its token addresses."""

    id: int
    asset: Asset
    a_token: str
    variable_debt_token: str
    config: ReserveConfig


@dataclass(frozen=True, slots=True)
class EModeCategory:
    """eMode category collateral parameters (bps) and the reserve ids it applies to."""

    id: int
    liquidation_threshold: int
    liquidation_bonus: int
    collateral_reserve_ids: frozenset[int]

    def applies_to(self, reserve_id: int) -> bool:
        """True if the reserve is an eMode collateral of this category."""
        return reserve_id in self.collateral_reserve_ids


def effective_collateral_params(
    reserve: Reserve, emode: EModeCategory | None
) -> tuple[int, Decimal]:
    """Liquidation threshold (bps) and bonus (fraction) that Aave applies to this user's collateral.

    A user in an eMode category gets the category's (usually much smaller) bonus for collateral
    that belongs to the category; other collateral keeps the reserve parameters.
    """
    if emode is not None and emode.applies_to(reserve.id):
        bonus = max(
            Decimal(0),
            (Decimal(emode.liquidation_bonus) - PERCENTAGE_FACTOR) / PERCENTAGE_FACTOR,
        )
        return emode.liquidation_threshold, bonus
    return reserve.config.liquidation_threshold, reserve.config.bonus_fraction


def max_liquidatable_debt_usd(
    health_factor: Decimal, debt: DebtLeg, collateral: CollateralLeg, total_debt_usd: Decimal
) -> Decimal:
    """Aave v3.3+ maximum debt (USD) that can be repaid for one debt/collateral pair.

    The whole reserve debt is liquidatable unless HF > 0.95 and both the debt and the collateral
    reserve of the user are worth at least 2000 USD; then it is capped at 50% of the user's
    *total* debt (not of this reserve's debt).
    """
    if (
        health_factor > CLOSE_FACTOR_HF_THRESHOLD
        and debt.value_usd >= MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD_USD
        and collateral.value_usd >= MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD_USD
    ):
        return min(debt.value_usd, total_debt_usd * DEFAULT_CLOSE_FACTOR)
    return debt.value_usd


def quote_pair(
    health_factor: Decimal,
    debt: DebtLeg,
    collateral: CollateralLeg,
    total_debt_usd: Decimal | None = None,
) -> tuple[int, Decimal, Decimal, Decimal] | None:
    """Self-funded liquidation terms for one debt/collateral pair.

    Applies the v3.3 close factor and the "must not leave dust" rule: a partial liquidation
    (debt and collateral both left over) must leave at least 1000 USD of each, otherwise the
    amount is reduced to satisfy it or the pair is dropped.

    Returns:
        ``(debt_to_cover_units, debt_to_cover_usd, collateral_seized_usd, gross_profit_usd)`` or
        `None` when the pair yields nothing executable.
    """
    if debt.value_usd <= 0 or collateral.value_usd <= 0 or debt.price_usd <= 0:
        return None
    total_debt = debt.value_usd if total_debt_usd is None else total_debt_usd
    multiplier = Decimal(1) + collateral.liquidation_bonus
    debt_usd = max_liquidatable_debt_usd(health_factor, debt, collateral, total_debt)
    if debt_usd * multiplier > collateral.value_usd:
        debt_usd = collateral.value_usd / multiplier

    leftover = MIN_LEFTOVER_USD * DUST_SAFETY_MARGIN
    debt_left = debt.value_usd - debt_usd
    collateral_left = collateral.value_usd - debt_usd * multiplier
    partial = debt_left > DUST_EPSILON_USD and collateral_left > DUST_EPSILON_USD
    if partial and (debt_left < leftover or collateral_left < leftover):
        debt_usd = min(debt.value_usd - leftover, (collateral.value_usd - leftover) / multiplier)
        if debt_usd <= 0:
            return None

    seized_usd = debt_usd * multiplier
    gross_usd = (seized_usd - debt_usd) * (Decimal(1) - collateral.protocol_fee)
    debt_units = min(debt.amount, from_units(debt_usd / debt.price_usd, debt.asset.decimals))
    if debt_units <= 0 or gross_usd <= 0:
        return None
    return debt_units, debt_usd, seized_usd, gross_usd


class AaveV3Protocol(BaseLendingProtocol):
    """Aave v3 Pool adapter."""

    protocol_name = PROTOCOL_NAME

    def __init__(self, provider: BaseChainProvider, addresses_provider: str) -> None:
        super().__init__(
            provider,
            MarketRef(
                chain=provider.name,
                protocol=PROTOCOL_NAME,
                address=checksum(addresses_provider),
                name="Aave v3",
            ),
        )
        self._addresses_provider = checksum(addresses_provider)
        self._pool: str | None = None
        self._oracle: str | None = None
        self._base_unit = Decimal(10**8)
        self._reserves: list[Reserve] = []
        self._emodes: dict[int, EModeCategory] = {}

    @property
    def pool(self) -> str:
        """Pool proxy address (available after `initialize`)."""
        if self._pool is None:
            raise ProtocolError("Aave v3 adapter is not initialized")
        return self._pool

    @property
    def oracle(self) -> str:
        """AaveOracle address (available after `initialize`)."""
        if self._oracle is None:
            raise ProtocolError("Aave v3 adapter is not initialized")
        return self._oracle

    async def initialize(self) -> None:
        """Resolve Pool/Oracle and load reserve metadata."""
        results = await self._provider.multicall(
            [
                Call(self._addresses_provider, GET_POOL.encode(), allow_failure=False),
                Call(self._addresses_provider, GET_PRICE_ORACLE.encode(), allow_failure=False),
            ]
        )
        pool_raw = decode_result(GET_POOL, results[0])
        oracle_raw = decode_result(GET_PRICE_ORACLE, results[1])
        if pool_raw is None or oracle_raw is None:
            raise ProtocolError(f"{self.market.key}: cannot resolve Pool / Oracle")
        pool, oracle = checksum(pool_raw[0]), checksum(oracle_raw[0])

        results = await self._provider.multicall(
            [
                Call(pool, GET_RESERVES_LIST.encode(), allow_failure=False),
                Call(oracle, BASE_CURRENCY_UNIT.encode()),
            ]
        )
        reserves_raw = decode_result(GET_RESERVES_LIST, results[0])
        unit_raw = decode_result(BASE_CURRENCY_UNIT, results[1])
        if reserves_raw is None:
            raise ProtocolError(f"{self.market.key}: cannot read reserves list")
        assets = [checksum(a) for a in reserves_raw[0]]

        data_results = await self._provider.multicall(
            [Call(pool, GET_RESERVE_DATA.encode(asset)) for asset in assets]
        )
        configs: dict[str, tuple[ReserveConfig, int, str, str]] = {}
        for asset, result in zip(assets, data_results, strict=True):
            decoded = decode_result(GET_RESERVE_DATA, result)
            if decoded is None:
                logger.warning("aave_reserve_unreadable", chain=self.market.chain, asset=asset)
                continue
            config = ReserveConfig.from_bitmap(int(decoded[0][0]))
            configs[asset] = (config, int(decoded[7]), checksum(decoded[8]), checksum(decoded[10]))

        metadata = await load_assets(
            self._provider, list(configs), {a: c[0].decimals for a, c in configs.items()}
        )
        self._reserves = [
            Reserve(id=rid, asset=metadata[a], a_token=a_tok, variable_debt_token=v_tok, config=cfg)
            for a, (cfg, rid, a_tok, v_tok) in configs.items()
        ]
        self._emodes = await self._load_emodes(pool, self._reserves)
        self._pool, self._oracle = pool, oracle
        if unit_raw is not None and int(unit_raw[0]) > 0:
            self._base_unit = Decimal(int(unit_raw[0]))
        logger.info(
            "aave_initialized",
            chain=self.market.chain,
            pool=pool,
            reserves=len(self._reserves),
            emode_categories=len(self._emodes),
        )

    async def _load_emodes(self, pool: str, reserves: list[Reserve]) -> dict[int, EModeCategory]:
        """Load eMode categories (v3.2+ collateral config + bitmap, legacy fallback)."""
        ids = range(1, EMODE_MAX_CATEGORY_ID + 1)
        calls = [Call(pool, GET_EMODE_COLLATERAL_CONFIG.encode(i)) for i in ids]
        calls += [Call(pool, GET_EMODE_COLLATERAL_BITMAP.encode(i)) for i in ids]
        calls += [Call(pool, GET_EMODE_CATEGORY_DATA.encode(i)) for i in ids]
        results = await self._provider.multicall(calls)
        n = len(ids)
        categories: dict[int, EModeCategory] = {}
        for offset, category_id in enumerate(ids):
            config = decode_result(GET_EMODE_COLLATERAL_CONFIG, results[offset])
            bitmap = decode_result(GET_EMODE_COLLATERAL_BITMAP, results[n + offset])
            if config is not None and bitmap is not None:
                _ltv, lt, bonus = (int(v) for v in config)
                members = frozenset(r.id for r in reserves if (int(bitmap[0]) >> r.id) & 1)
            else:
                legacy = decode_result(GET_EMODE_CATEGORY_DATA, results[2 * n + offset])
                if legacy is None:
                    continue
                _ltv, lt, bonus, _source, _label = legacy[0]
                lt, bonus = int(lt), int(bonus)
                members = frozenset(
                    r.id for r in reserves if r.config.emode_category == category_id
                )
            if lt > 0:
                categories[category_id] = EModeCategory(category_id, lt, bonus, members)
        return categories

    def discovery_filter(self) -> LogFilter:
        """`Borrow` events emitted by the Pool."""
        return LogFilter(address=self.pool, topics=(BORROW_TOPIC,))

    def borrower_from_log(self, log: LogEntry) -> str | None:
        """`onBehalfOf` is the second indexed topic of `Borrow`."""
        if len(log.topics) < 3 or log.topics[0] != BORROW_TOPIC:
            return None
        return topic_to_address(log.topics[2])

    async def check_health(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[HealthCheck]:
        """Batched `getUserAccountData`."""
        calls = [Call(self.pool, GET_USER_ACCOUNT_DATA.encode(checksum(b))) for b in borrowers]
        results = await self._provider.multicall(calls, block)
        checks: list[HealthCheck] = []
        for borrower, result in zip(borrowers, results, strict=True):
            decoded = decode_result(GET_USER_ACCOUNT_DATA, result)
            if decoded is None:
                logger.warning(
                    "aave_account_unreadable", chain=self.market.chain, borrower=borrower
                )
                continue
            collateral_base, debt_base, _avail, _lt, _ltv, hf_raw = (int(v) for v in decoded)
            debt_usd = Decimal(debt_base) / self._base_unit
            hf = None if debt_base == 0 else Decimal(hf_raw) / Decimal(WAD)
            checks.append(
                HealthCheck(
                    borrower=borrower,
                    health_factor=hf,
                    debt_usd=debt_usd,
                    collateral_usd=Decimal(collateral_base) / self._base_unit,
                    liquidatable=hf is not None and hf < 1,
                )
            )
        return checks

    async def load_positions(
        self, borrowers: Sequence[str], block: BlockId = "latest"
    ) -> list[Position]:
        """Full positions: account data + configuration bitmap + token balances + prices."""
        if not borrowers:
            return []
        pool, reserves = self.pool, self._reserves
        head_calls: list[Call] = [
            Call(self.oracle, GET_ASSETS_PRICES.encode([r.asset.address for r in reserves]))
        ]
        for borrower in borrowers:
            head_calls.append(Call(pool, GET_USER_ACCOUNT_DATA.encode(checksum(borrower))))
            head_calls.append(Call(pool, GET_USER_CONFIGURATION.encode(checksum(borrower))))
            head_calls.append(Call(pool, GET_USER_EMODE.encode(checksum(borrower))))
        head = await self._provider.multicall(head_calls, block)
        prices_raw = decode_result(GET_ASSETS_PRICES, head[0])
        if prices_raw is None:
            raise ProtocolError(f"{self.market.key}: oracle getAssetsPrices failed")
        prices = {
            r.asset.address: Decimal(int(p)) / self._base_unit
            for r, p in zip(reserves, prices_raw[0], strict=True)
        }

        block_number = block if isinstance(block, int) else await self._provider.block_number()
        plans: list[tuple[str, Decimal, EModeCategory | None, list[tuple[Reserve, bool]]]] = []
        balance_calls: list[Call] = []
        for i, borrower in enumerate(borrowers):
            account = decode_result(GET_USER_ACCOUNT_DATA, head[1 + 3 * i])
            user_cfg = decode_result(GET_USER_CONFIGURATION, head[2 + 3 * i])
            user_emode = decode_result(GET_USER_EMODE, head[3 + 3 * i])
            if account is None or user_cfg is None or int(account[1]) == 0:
                continue
            emode = self._emodes.get(int(user_emode[0])) if user_emode else None
            bitmap = int(user_cfg[0][0])
            legs: list[tuple[Reserve, bool]] = []
            for reserve in reserves:
                if (bitmap >> (2 * reserve.id)) & 1:
                    legs.append((reserve, False))
                    balance_calls.append(
                        Call(reserve.variable_debt_token, BALANCE_OF.encode(borrower))
                    )
                if (bitmap >> (2 * reserve.id + 1)) & 1:
                    legs.append((reserve, True))
                    balance_calls.append(Call(reserve.a_token, BALANCE_OF.encode(borrower)))
            plans.append((borrower, Decimal(int(account[5])) / Decimal(WAD), emode, legs))

        balances = await self._provider.multicall(balance_calls, block)
        positions: list[Position] = []
        cursor = 0
        for borrower, hf, emode, legs in plans:
            collateral: list[CollateralLeg] = []
            debt: list[DebtLeg] = []
            for reserve, is_collateral in legs:
                raw = decode_result(BALANCE_OF, balances[cursor])
                cursor += 1
                amount = int(raw[0]) if raw else 0
                price = prices.get(reserve.asset.address, Decimal(0))
                if amount == 0 or price <= 0:
                    continue
                value = to_units(amount, reserve.asset.decimals) * price
                if is_collateral:
                    threshold, bonus = effective_collateral_params(reserve, emode)
                    if threshold == 0:
                        continue
                    collateral.append(
                        CollateralLeg(
                            asset=reserve.asset,
                            amount=amount,
                            price_usd=price,
                            value_usd=value,
                            liquidation_bonus=bonus,
                            protocol_fee=reserve.config.protocol_fee_fraction,
                        )
                    )
                else:
                    debt.append(DebtLeg(reserve.asset, amount, price, value))
            if debt:
                positions.append(
                    Position(
                        market=self.market,
                        borrower=checksum(borrower),
                        health_factor=hf,
                        collateral=tuple(collateral),
                        debt=tuple(debt),
                        block_number=int(block_number),
                    )
                )
        return positions

    def quote_liquidations(self, position: Position) -> list[LiquidationQuote]:
        """One quote per (debt, collateral) pair, using close factor and bonus rules."""
        paused = {r.asset.address for r in self._reserves if r.config.paused or not r.config.active}
        quotes: list[LiquidationQuote] = []
        for debt in position.debt:
            if debt.asset.address in paused:
                continue
            for collateral in position.collateral:
                if collateral.asset.address in paused:
                    continue
                terms = quote_pair(
                    position.health_factor, debt, collateral, position.total_debt_usd
                )
                if terms is None:
                    continue
                debt_units, debt_usd, seized_usd, gross_usd = terms
                quotes.append(
                    LiquidationQuote(
                        debt_asset=debt.asset,
                        collateral_asset=collateral.asset,
                        debt_to_cover=debt_units,
                        debt_to_cover_usd=debt_usd,
                        collateral_seized_usd=seized_usd,
                        gross_profit_usd=gross_usd,
                        bonus_pct=collateral.liquidation_bonus * 100,
                        gas_units=LIQUIDATION_GAS_UNITS,
                        tx=TxTemplate(
                            to=self.pool,
                            data=LIQUIDATION_CALL.encode(
                                collateral.asset.address,
                                debt.asset.address,
                                checksum(position.borrower),
                                debt_units,
                                False,
                            ),
                        ),
                    )
                )
        return quotes

    def price_calls(self) -> list[Call]:
        """One `getAssetsPrices` call covering every reserve."""
        if self._oracle is None or not self._reserves:
            return []
        return [
            Call(self._oracle, GET_ASSETS_PRICES.encode([r.asset.address for r in self._reserves]))
        ]

    def parse_prices(self, results: Sequence[CallResult]) -> tuple[int, ...]:
        """Raw oracle prices in base-currency units, in reserve order."""
        decoded = decode_result(GET_ASSETS_PRICES, results[0]) if results else None
        return tuple(int(p) for p in decoded[0]) if decoded else ()

    # --- Market measurement ------------------------------------------------------------------

    def _reserve(self, address: str | None) -> Reserve | None:
        if address is None:
            return None
        wanted = address.lower()
        return next((r for r in self._reserves if r.asset.address.lower() == wanted), None)

    def asset_symbol(self, address: str | None) -> str | None:
        """Symbol of a reserve asset."""
        reserve = self._reserve(address)
        return reserve.asset.symbol if reserve else None

    def gas_bearing_topics(self) -> frozenset[bytes]:
        """Each `LiquidationCall` is one paid action."""
        return frozenset({LIQUIDATION_CALL_TOPIC})

    def liquidation_filter(self) -> LogFilter:
        """`LiquidationCall` events of the Pool."""
        return LogFilter(address=self.pool, topics=(LIQUIDATION_CALL_TOPIC,))

    def decode_liquidation(self, log: LogEntry) -> LiquidationEvent | None:
        """Decode `LiquidationCall` (collateral, debt and user are indexed)."""
        if len(log.topics) < 4 or log.topics[0] != LIQUIDATION_CALL_TOPIC:
            return None
        try:
            debt, collateral, liquidator, _receive_a_token = abi_decode(
                list(LIQUIDATION_CALL_DATA), log.data
            )
        except (DecodingError, ValueError):
            return None
        return LiquidationEvent(
            market=self.market,
            kind="aave_liquidation",
            tx_hash=log.tx_hash,
            log_index=log.log_index,
            block_number=log.block_number,
            borrower=topic_to_address(log.topics[3]),
            liquidator=checksum(liquidator),
            debt_asset=topic_to_address(log.topics[2]),
            collateral_asset=topic_to_address(log.topics[1]),
            debt_amount=int(debt),
            collateral_amount=int(collateral),
        )

    async def value_liquidation(self, event: LiquidationEvent) -> EventValuation | None:
        """Repaid debt and seized collateral at the oracle prices of the event's block."""
        debt_reserve = self._reserve(event.debt_asset)
        collateral_reserve = self._reserve(event.collateral_asset)
        if debt_reserve is None or collateral_reserve is None:
            return None
        assets = [collateral_reserve.asset.address, debt_reserve.asset.address]
        raw = await self._provider.call(
            self.oracle, GET_ASSETS_PRICES.encode(assets), event.block_number
        )
        collateral_price, debt_price = (
            Decimal(int(p)) / self._base_unit for p in GET_ASSETS_PRICES.decode(raw)[0]
        )
        return EventValuation(
            debt_usd=to_units(event.debt_amount, debt_reserve.asset.decimals) * debt_price,
            collateral_usd=to_units(event.collateral_amount, collateral_reserve.asset.decimals)
            * collateral_price,
        )

    async def is_liquidatable_at(self, borrower: str, block: int) -> bool | None:
        """HF < 1 with non-zero debt at the end of `block` (needs historical state)."""
        raw = await self._provider.call(
            self.pool, GET_USER_ACCOUNT_DATA.encode(checksum(borrower)), block
        )
        _collateral, debt, _avail, _lt, _ltv, hf = GET_USER_ACCOUNT_DATA.decode(raw)
        return int(debt) > 0 and int(hf) < WAD


@PROTOCOLS.register(PROTOCOL_NAME)
def create_aave_v3(
    provider: BaseChainProvider, chain: ChainConfig, deps: ProtocolDeps
) -> list[BaseLendingProtocol]:
    """Factory: one Aave v3 market per chain where it is deployed."""
    if chain.aave_v3_addresses_provider is None:
        return []
    return [AaveV3Protocol(provider, chain.aave_v3_addresses_provider)]
