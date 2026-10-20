"""Redis-backed alert deduplication, task locks and heartbeats."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import RedisError

HEARTBEAT_PREFIX = "aegis:heartbeat:"


class RedisAlertDeduplicator:
    """`IAlertDeduplicator` using `SET NX EX`."""

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def acquire(self, key: str) -> bool:
        """Return True if no alert was sent for `key` within the TTL (and reserve it)."""
        return bool(await self._redis.set(key, "1", nx=True, ex=self._ttl))

    async def release(self, key: str) -> None:
        """Forget `key` so the next scan may alert again (used when delivery failed)."""
        await self._redis.delete(key)


# Delete the key only if it still holds our token (atomic compare-and-delete).
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisTaskLock:
    """Non-blocking per-task lock preventing overlapping runs of the same periodic task.

    Each holder uses a random token, and release is an atomic Lua compare-and-delete, so a lock
    that expired and was re-acquired by another process is never deleted by the old holder.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @asynccontextmanager
    async def hold(self, name: str, ttl_seconds: int) -> AsyncIterator[bool]:
        """Yield True if the lock was acquired; it is released on exit."""
        key = f"aegis:lock:{name}"
        token = uuid.uuid4().hex
        acquired = bool(await self._redis.set(key, token, nx=True, ex=ttl_seconds))
        try:
            yield acquired
        finally:
            if acquired:
                await self._redis.eval(_RELEASE_SCRIPT, 1, key, token)  # type: ignore[misc]


async def record_heartbeat(redis: Redis, name: str = "scan") -> None:
    """Record that a component is alive (consumed by container healthchecks and reports)."""
    await redis.set(f"{HEARTBEAT_PREFIX}{name}", str(int(time.time())), ex=3600)


async def beat_heartbeat(redis: Redis) -> None:
    """Record that a scheduled scan task ran."""
    await record_heartbeat(redis, "scan")


async def heartbeat_age_seconds(redis: Redis, name: str = "scan") -> float | None:
    """Seconds since the last heartbeat of `name`, or None if never recorded."""
    raw = await redis.get(f"{HEARTBEAT_PREFIX}{name}")
    if raw is None:
        return None
    return time.time() - int(raw)


SENT_MARKER_TTL_SECONDS = 7 * 24 * 3600


def _sent_key(item_id: int, kind: str) -> str:
    # Opportunities keep the original key layout so markers survive upgrades.
    return f"aegis:sent:{item_id}" if kind == "opportunity" else f"aegis:sent:{kind}:{item_id}"


async def was_sent(redis: Redis, item_id: int, kind: str = "opportunity") -> bool:
    """True if the alert for this item (opportunity / liquidation) was already delivered."""
    return bool(await redis.exists(_sent_key(item_id, kind)))


async def mark_sent(redis: Redis, item_id: int, kind: str = "opportunity") -> None:
    """Record delivery right after Telegram accepted the message (makes retries idempotent)."""
    await redis.set(_sent_key(item_id, kind), "1", ex=SENT_MARKER_TTL_SECONDS)


# Token bucket refilled continuously at `rate` units/s up to `capacity`.
# Returns "0" when the cost was granted, otherwise the seconds to wait before retrying.
_BUCKET_SCRIPT = """
local rate = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1]) or capacity
local ts = tonumber(data[2]) or now
tokens = math.min(capacity, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then
    tokens = tokens - cost
else
    wait = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], 60000)
return tostring(wait)
"""

MAX_LIMITER_SLEEP_SECONDS = 1.0
#: Largest single-call cost (eth_getLogs); a bucket always holds at least one such call.
MAX_SINGLE_CALL_COST = 75


class RedisRateLimiter:
    """`IRateLimiter`: compute-unit token buckets in Redis, shared by every process.

    The budget is split into two independent buckets so neither side can starve the other:
    high-priority callers (the watcher) get `reserve_pct` of it, background work (indexers,
    scheduled scans, market enrichment) gets the rest. Each bucket can hold at least one call
    of the most expensive method, so every call is charged its full cost.

    The limiter must never become a single point of failure: each Redis call is bounded by
    `redis_timeout_seconds`, and after a failure the limiter is bypassed (fails open) for
    `bypass_seconds` instead of stalling every RPC request on reconnect attempts.

    Args:
        redis: Redis client.
        units_per_second: Total budget per provider key (e.g. ~80% of the plan's CUPS).
        reserve_pct: Share of the budget dedicated to high-priority callers.
        sleep: Injectable sleep (tests).
        clock: Injectable monotonic clock (tests).
    """

    def __init__(
        self,
        redis: Redis,
        units_per_second: int,
        reserve_pct: int,
        *,
        redis_timeout_seconds: float = 0.5,
        bypass_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        high = units_per_second * reserve_pct / 100
        # 0% reserve: everyone shares one bucket; otherwise two disjoint shares.
        self._rates = (
            {"high": float(units_per_second), "low": float(units_per_second)}
            if reserve_pct == 0
            else {"high": max(0.1, high), "low": max(0.1, units_per_second - high)}
        )
        self._shared = reserve_pct == 0
        self._timeout = redis_timeout_seconds
        self._bypass_seconds = bypass_seconds
        self._sleep = sleep
        self._clock = clock
        self._bypass_until = float("-inf")

    async def acquire(self, bucket: str, cost: int, *, high_priority: bool) -> None:
        """Block until this caller's share allows `cost`. Fails open if Redis is unavailable."""
        share = "high" if high_priority else "low"
        rate = self._rates[share]
        capacity = max(rate, float(MAX_SINGLE_CALL_COST), float(cost))
        key = f"aegis:rpc:budget:{bucket}" + ("" if self._shared else f":{share}")
        while True:
            if self._clock() < self._bypass_until:
                return
            try:
                async with asyncio.timeout(self._timeout):
                    raw = await self._redis.eval(  # type: ignore[misc]
                        _BUCKET_SCRIPT, 1, key, str(rate), str(cost), str(capacity)
                    )
            except (RedisError, OSError, TimeoutError):
                self._bypass_until = self._clock() + self._bypass_seconds
                return
            wait = float(raw)
            if wait <= 0:
                return
            await self._sleep(min(wait, MAX_LIMITER_SLEEP_SECONDS))
