"""Compare configured RPC endpoints: request latency and head freshness.

Usage: ``make rpc-latency`` (or ``python -m aegis.tools.rpc_latency --samples 15``).

Detection speed is bounded by how fresh the node's view of the chain is: a node that serves new
blocks late makes every downstream optimisation irrelevant. Absolute "head age" also contains
chain-specific timestamp semantics, so compare endpoints of the same chain with each other. Put the
freshest endpoint first in ``<CHAIN>_RPC_URLS`` (the first URL is preferred).
Only hosts are printed; URL paths (API keys) are never shown.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass

from pydantic import SecretStr
from web3 import AsyncHTTPProvider, AsyncWeb3

from aegis.config.settings import get_settings
from aegis.core.redact import redact, safe_error


@dataclass(frozen=True, slots=True)
class EndpointStats:
    """Latency profile of one endpoint."""

    host: str
    rtt_ms: float
    head_age_s: float
    errors: int


async def measure(url: SecretStr, samples: int, pause: float) -> EndpointStats:
    """Sample `eth_blockNumber` round trips and the age of the returned head block."""
    w3 = AsyncWeb3(AsyncHTTPProvider(url.get_secret_value(), exception_retry_configuration=None))
    rtts: list[float] = []
    ages: list[float] = []
    errors = 0
    try:
        for _ in range(samples):
            try:
                started = time.time()
                head = await w3.eth.block_number
                seen = time.time()
                block = await w3.eth.get_block(head)
                rtts.append((seen - started) * 1000)
                ages.append(seen - int(block["timestamp"]))
            except Exception:  # diagnostic tool: count the failure and continue
                errors += 1
            await asyncio.sleep(pause)
    finally:
        await w3.provider.disconnect()
    host = redact(url.get_secret_value()).removesuffix("/***")
    if not rtts:
        return EndpointStats(host, float("nan"), float("nan"), errors)
    return EndpointStats(host, statistics.median(rtts), statistics.median(ages), errors)


async def run(samples: int) -> None:
    """Measure every configured endpoint of every enabled chain concurrently."""
    settings = get_settings()
    jobs: list[tuple[str, asyncio.Task[EndpointStats]]] = []
    for chain in settings.enabled_chains:
        for url in settings.rpc_urls_for(chain):
            jobs.append((chain, asyncio.create_task(measure(url, samples, 0.35))))
    print(f"{'chain':10} {'endpoint':45} {'rtt (median)':>13} {'head age':>9} {'errors':>6}")
    for chain, job in jobs:
        try:
            stats = await job
        except Exception as exc:  # report and continue with the next endpoint
            print(f"{chain:10} failed: {safe_error(exc)}")
            continue
        print(
            f"{chain:10} {stats.host:45} {stats.rtt_ms:10.0f} ms {stats.head_age_s:8.2f}s "
            f"{stats.errors:6d}"
        )
    print(
        "\nhead age = how old the newest block is (by its timestamp) when the node returns it."
        "\nCompare endpoints of the same chain: part of the age is inherent to the chain (OP-stack"
        "\nstamps a block at the start of its 2s slot; timestamps have 1s resolution). Lower is"
        "\nfresher. Put the freshest endpoint first in <CHAIN>_RPC_URLS."
    )


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Compare RPC endpoint latency/freshness")
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    asyncio.run(run(args.samples))


if __name__ == "__main__":
    main()
