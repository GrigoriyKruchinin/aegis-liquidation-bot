"""RPC cost accounting shared by all processes (compute-unit weighted rate limiting).

Hosted RPC providers (Alchemy, Infura, QuickNode, ...) limit throughput per *API key* across all
chains, weighted by method cost. `bucket_key` therefore groups URLs by provider family and key,
so Base, Arbitrum and Optimism endpoints of one Alchemy app share one budget.
"""

from __future__ import annotations

from hashlib import sha256
from urllib.parse import urlsplit

#: Approximate compute-unit cost per JSON-RPC method (Alchemy pricing; conservative elsewhere).
RPC_METHOD_COST: dict[str, int] = {
    "eth_blockNumber": 10,
    "eth_chainId": 0,
    "eth_gasPrice": 20,
    "eth_getBlockByNumber": 16,
    "eth_call": 26,
    "eth_getLogs": 75,
    "eth_getTransactionReceipt": 15,
}
DEFAULT_METHOD_COST = 26


def method_cost(method: str) -> int:
    """Compute-unit weight of an RPC method."""
    return RPC_METHOD_COST.get(method, DEFAULT_METHOD_COST)


def bucket_key(url: str) -> str:
    """Stable, secret-free rate-limit bucket id for an RPC URL.

    ``https://base-mainnet.g.alchemy.com/v2/KEY`` and ``https://arb-mainnet.g.alchemy.com/v2/KEY``
    map to the same bucket (``alchemy.com:<hash of KEY>``); the key itself never appears.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    family = ".".join(host.split(".")[-2:]) or "unknown"
    credentials = f"{parts.username or ''}:{parts.password or ''}{parts.path}?{parts.query}"
    digest = sha256(f"{family}|{credentials}".encode()).hexdigest()[:16]
    return f"{family}:{digest}"


class NoopRateLimiter:
    """`IRateLimiter` that never waits (tests, tools, or when limiting is disabled)."""

    async def acquire(self, bucket: str, cost: int, *, high_priority: bool) -> None:
        """Return immediately."""
        return None
