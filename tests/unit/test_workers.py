from __future__ import annotations

from decimal import Decimal

import fakeredis
from pydantic import SecretStr

from aegis.config.settings import AppSettings
from aegis.workers.app import build_beat_schedule
from aegis.workers.redis_tools import (
    RedisAlertDeduplicator,
    RedisTaskLock,
    beat_heartbeat,
    heartbeat_age_seconds,
)


def _redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


async def test_dedup_acquire_once_then_release() -> None:
    dedup = RedisAlertDeduplicator(_redis(), ttl_seconds=60)
    assert await dedup.acquire("k")
    assert not await dedup.acquire("k")
    await dedup.release("k")
    assert await dedup.acquire("k")


async def test_task_lock_excludes_overlap() -> None:
    lock = RedisTaskLock(_redis())
    async with lock.hold("scan:base", 60) as first:
        assert first
        async with lock.hold("scan:base", 60) as second:
            assert not second
    async with lock.hold("scan:base", 60) as again:
        assert again


async def test_heartbeat_roundtrip() -> None:
    redis = _redis()
    assert await heartbeat_age_seconds(redis) is None
    await beat_heartbeat(redis)
    age = await heartbeat_age_seconds(redis)
    assert age is not None and age < 5


def test_beat_schedule_per_chain() -> None:
    settings = AppSettings(
        _env_file=None,
        telegram_bot_token=SecretStr("1:x"),
        telegram_chat_id="1",
        enabled_chains=["base", "optimism"],
        watchlist_interval_seconds=6,
        min_profit_usd=Decimal(5),
    )
    schedule = build_beat_schedule(settings)
    assert set(schedule) == {
        "index-base",
        "scan-full-base",
        "scan-watchlist-base",
        "index-optimism",
        "scan-full-optimism",
        "scan-watchlist-optimism",
    }
    assert schedule["scan-watchlist-base"]["schedule"] == 6.0
    assert schedule["scan-full-base"]["options"]["queue"] == "scan"
