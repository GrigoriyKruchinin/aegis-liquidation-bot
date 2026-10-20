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
        min_profit_usd=Decimal(5),
    )
    schedule = build_beat_schedule(settings)
    assert set(schedule) == {
        "index-base",
        "scan-full-base",
        "index-optimism",
        "scan-full-optimism",
        "market-index-base",
        "market-index-optimism",
        "market-enrich",
        "market-digest",
        "status-report",
    }
    assert schedule["status-report"]["schedule"] == 3600.0  # hourly by default
    assert schedule["market-enrich"]["options"]["queue"] == "enrich"
    digest = schedule["market-digest"]
    assert digest["options"]["queue"] == "notify" and digest["schedule"].hour == {9}
    assert schedule["status-report"]["options"]["queue"] == "notify"
    assert schedule["scan-full-base"]["options"]["queue"] == "scan"


def test_status_report_can_be_disabled() -> None:
    settings = AppSettings(
        _env_file=None,
        telegram_bot_token=SecretStr("1:x"),
        telegram_chat_id="1",
        status_report_interval_hours=0,
    )
    assert "status-report" not in build_beat_schedule(settings)


def test_market_digest_can_be_disabled() -> None:
    settings = AppSettings(
        _env_file=None,
        telegram_bot_token=SecretStr("1:x"),
        telegram_chat_id="1",
        market_digest_hour_utc=-1,
    )
    assert "market-digest" not in build_beat_schedule(settings)


def test_enrich_task_routed_to_enrich_queue() -> None:
    from aegis.workers.app import app

    route = app.amqp.router.route({}, "aegis.market.enrich")
    assert route["queue"].name == "enrich"
    assert app.amqp.router.route({}, "aegis.market.index")["queue"].name == "market"


def test_celery_beat_receives_every_schedule_setting() -> None:
    """celery-beat gets no `.env`: each schedule setting must be passed explicitly."""
    from pathlib import Path

    from aegis.config.settings import SchedulerSettings, _EnvSettings

    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    beat = compose.split("  celery-beat:", 1)[1].split("\nvolumes:", 1)[0]
    missing = [
        name.upper()
        for name in SchedulerSettings.model_fields.keys() - _EnvSettings.model_fields.keys()
        if name != "redis_url" and f"{name.upper()}:" not in beat
    ]
    assert missing == []
