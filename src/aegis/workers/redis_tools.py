"""Redis-backed alert deduplication, task locks and heartbeats."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

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


def _sent_key(opportunity_id: int) -> str:
    return f"aegis:sent:{opportunity_id}"


async def was_sent(redis: Redis, opportunity_id: int) -> bool:
    """True if the alert for this opportunity was already delivered."""
    return bool(await redis.exists(_sent_key(opportunity_id)))


async def mark_sent(redis: Redis, opportunity_id: int) -> None:
    """Record delivery right after Telegram accepted the message (makes retries idempotent)."""
    await redis.set(_sent_key(opportunity_id), "1", ex=SENT_MARKER_TTL_SECONDS)
