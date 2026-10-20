"""End-to-end detection on an anvil fork of Optimism.

A real Aave v3 borrower is made liquidatable by pointing the AaveOracle price source of their
collateral to a mock aggregator (via `anvil_setCode` + `anvil_setStorageAt`). The test asserts
that the scanner detects the position, the estimator produces a profitable quote and the
Telegram template renders it.

Requires `anvil` (Foundry) on PATH or in ~/.foundry/bin and network access to a fork RPC:
    RUN_INTEGRATION=1 FORK_URL_OPTIMISM=https://... make test ARGS=tests/integration
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_utils.crypto import keccak
from pydantic import SecretStr
from web3 import AsyncHTTPProvider, AsyncWeb3

from aegis.chains.op_stack import OpStackChainProvider
from aegis.config.chains import OPTIMISM
from aegis.core.abi import AbiFunction, checksum
from aegis.core.models import Call, OpportunitySummary
from aegis.estimator.gas import L2GasEstimator
from aegis.estimator.oracle import ChainlinkNativePriceOracle
from aegis.estimator.profit import ProfitEstimator
from aegis.notifier.templates import render_opportunity
from aegis.protocols.aave_v3 import GET_ASSETS_PRICES, AaveV3Protocol
from aegis.protocols.base import decode_result
from aegis.scanner.health import HealthFactorScanner
from aegis.scanner.indexer import EventBorrowerIndexer
from tests.fakes import MemoryBorrowerRepository, MemoryCheckpointRepository

pytestmark = pytest.mark.integration

DEFAULT_FORK_URL = "https://mainnet.optimism.io"
GET_SOURCE_OF_ASSET = AbiFunction("getSourceOfAsset(address)", ("address",))
TARGET_HF = Decimal("0.92")
MOCK_BASE = 0xAE61500000000000000000000000000000000000


def _mock_address(index: int) -> str:
    return checksum(f"0x{MOCK_BASE + index:040x}")


def _anvil_binary() -> str | None:
    found = shutil.which("anvil")
    if found:
        return found
    candidate = Path.home() / ".foundry" / "bin" / "anvil"
    return str(candidate) if candidate.exists() else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def anvil_url() -> Iterator[str]:
    binary = _anvil_binary()
    if binary is None:
        pytest.skip("anvil not installed")
    port = _free_port()
    fork_url = os.environ.get("FORK_URL_OPTIMISM", DEFAULT_FORK_URL)
    proc = subprocess.Popen(  # noqa: S603  # fixed binary and arguments
        [binary, "--fork-url", fork_url, "--port", str(port), "--silent", "--no-rate-limit"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail("anvil exited during startup")
                import time

                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
async def provider(anvil_url: str) -> AsyncIterator[OpStackChainProvider]:
    chain = OpStackChainProvider(OPTIMISM, [SecretStr(anvil_url)], timeout_seconds=60)
    yield chain
    await chain.aclose()


def _mock_aggregator_code(answer: int) -> str:
    """Runtime bytecode returning `answer` (32 bytes) for any call (`latestAnswer`)."""
    return "0x7f" + answer.to_bytes(32, "big").hex() + "60005260206000f3"


async def _set_price(
    w3: AsyncWeb3[AsyncHTTPProvider], oracle: str, asset: str, answer: int, index: int
) -> None:
    mock = _mock_address(index)
    await w3.provider.make_request("anvil_setCode", [mock, _mock_aggregator_code(answer)])
    # AaveOracle: `mapping(address => AggregatorInterface) assetsSources` lives at slot 0.
    slot = keccak(encode(["address", "uint256"], [asset, 0]))
    value = "0x" + encode(["address"], [mock]).hex()
    await w3.provider.make_request("anvil_setStorageAt", [oracle, "0x" + slot.hex(), value])


async def test_detects_crafted_underwater_aave_position(
    provider: OpStackChainProvider, anvil_url: str
) -> None:
    protocol = AaveV3Protocol(provider, OPTIMISM.aave_v3_addresses_provider or "")
    borrowers_repo = MemoryBorrowerRepository()
    indexer = EventBorrowerIndexer(
        borrowers_repo,
        MemoryCheckpointRepository(),
        lookback_days=2,
        chunk_blocks=10_000,
        max_blocks_per_run=200_000,
    )
    await indexer.sync(protocol)
    borrowers = await borrowers_repo.list_active(protocol.market)
    assert borrowers, "no Aave borrowers discovered on the fork"

    # Pick a healthy borrower whose debt and collateral assets do not overlap.
    checks = await protocol.check_health(borrowers)
    candidates = sorted(
        (
            c
            for c in checks
            if c.health_factor and Decimal(1) < c.health_factor < 3 and c.debt_usd > 200
        ),
        key=lambda c: c.debt_usd,
        reverse=True,
    )
    target = None
    for check in candidates:
        (pos,) = await protocol.load_positions([check.borrower]) or (None,)
        if pos is None or not pos.collateral:
            continue
        debt_assets = {d.asset.address for d in pos.debt}
        if debt_assets.isdisjoint(c.asset.address for c in pos.collateral):
            target = (check, pos)
            break
    assert target is not None, "no suitable borrower found"
    check, pos = target
    assert check.health_factor is not None

    # Scale every collateral price so that HF ~= TARGET_HF.
    oracle = protocol.oracle
    collateral_assets = [c.asset.address for c in pos.collateral]
    raw = await provider.multicall([Call(oracle, GET_ASSETS_PRICES.encode(collateral_assets))])
    decoded = decode_result(GET_ASSETS_PRICES, raw[0])
    assert decoded is not None
    factor = TARGET_HF / check.health_factor
    w3 = AsyncWeb3(AsyncHTTPProvider(anvil_url))
    for index, (asset, price) in enumerate(zip(collateral_assets, decoded[0], strict=True)):
        await _set_price(w3, oracle, asset, int(Decimal(int(price)) * factor), index)
        source = GET_SOURCE_OF_ASSET.decode(
            await provider.call(oracle, GET_SOURCE_OF_ASSET.encode(asset))
        )
        assert checksum(source[0]) == _mock_address(index)
    await w3.provider.disconnect()

    native = ChainlinkNativePriceOracle(provider, OPTIMISM.native_usd_feed, max_age_seconds=10**9)
    scanner = HealthFactorScanner(
        borrowers_repo,
        ProfitEstimator(L2GasEstimator(provider, native)),
        min_debt_usd=Decimal(10),
    )
    result = await scanner.scan(protocol, [check.borrower])

    assert result.liquidatable == 1
    assert len(result.opportunities) == 1
    opportunity = result.opportunities[0]
    assert opportunity.position.health_factor < 1
    assert abs(opportunity.position.health_factor - TARGET_HF) < Decimal("0.02")
    estimate = opportunity.estimate
    assert estimate.quote.gross_profit_usd > 0
    assert estimate.gas.l1_fee_wei > 0  # OP-stack L1 data fee is accounted for
    assert estimate.net_profit_usd < estimate.quote.gross_profit_usd
    assert estimate.quote.debt_to_cover_usd <= pos.total_debt_usd

    text = render_opportunity(OpportunitySummary.from_opportunity(opportunity))
    assert "Liquidatable Position" in text and check.borrower in text
    await asyncio.sleep(0)
