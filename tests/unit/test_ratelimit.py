from __future__ import annotations

import fakeredis
import pytest
from pydantic import SecretStr

from aegis.chains.ratelimit import bucket_key, method_cost
from aegis.config.settings import AppSettings
from aegis.workers.redis_tools import RedisRateLimiter


def test_bucket_key_groups_one_alchemy_app_across_chains() -> None:
    base = bucket_key("https://base-mainnet.g.alchemy.com/v2/KEY1")
    arb = bucket_key("https://arb-mainnet.g.alchemy.com/v2/KEY1")
    other_app = bucket_key("https://base-mainnet.g.alchemy.com/v2/KEY2")
    assert base == arb
    assert base != other_app
    assert base.startswith("alchemy.com:") and "KEY1" not in base
    assert bucket_key("https://mainnet.optimism.io").startswith("optimism.io:")


def test_method_costs() -> None:
    assert method_cost("eth_getLogs") > method_cost("eth_call") > method_cost("eth_blockNumber")
    assert method_cost("eth_somethingNew") == 26


def test_duplicate_rpc_urls_are_removed() -> None:
    settings = AppSettings(
        _env_file=None,
        telegram_bot_token=SecretStr("1:x"),
        telegram_chat_id="1",
        alchemy_api_key=SecretStr("K"),
        base_rpc_urls=[
            SecretStr("https://base-mainnet.g.alchemy.com/v2/K/"),
            SecretStr("https://mainnet.base.org"),
        ],
    )
    urls = [u.get_secret_value() for u in settings.rpc_urls_for("base")]
    assert urls == ["https://base-mainnet.g.alchemy.com/v2/K/", "https://mainnet.base.org"]


@pytest.fixture
def sleeps() -> list[float]:
    return []


def _limiter(redis: object, sleeps: list[float], **kwargs: object) -> RedisRateLimiter:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return RedisRateLimiter(redis, 100, 30, sleep=fake_sleep, **kwargs)  # type: ignore[arg-type]


async def test_limiter_keeps_reserve_for_high_priority(sleeps: list[float]) -> None:
    limiter = _limiter(fakeredis.FakeAsyncRedis(), sleeps)
    # Low priority may spend down to the 30-unit reserve without waiting...
    for _ in range(7):
        await limiter.acquire("b", 10, high_priority=False)
    assert sleeps == []
    # ...and the high-priority watcher can still spend the reserve immediately.
    for _ in range(3):
        await limiter.acquire("b", 10, high_priority=True)
    assert sleeps == []


async def test_limiter_makes_low_priority_wait() -> None:
    redis = fakeredis.FakeAsyncRedis()
    waited: list[float] = []

    async def sleep_and_refill(seconds: float) -> None:
        waited.append(seconds)
        await redis.delete("aegis:rpc:budget:b")  # the bucket refills while we wait

    limiter = RedisRateLimiter(redis, 100, 30, sleep=sleep_and_refill)
    for _ in range(7):
        await limiter.acquire("b", 10, high_priority=False)
    assert waited == []
    await limiter.acquire("b", 10, high_priority=False)  # would dip into the reserve -> wait
    assert len(waited) == 1 and 0 < waited[0] <= 1.0


async def test_limiter_fails_open_and_bypasses_when_redis_is_down(sleeps: list[float]) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    class DownRedis:
        calls = 0

        async def eval(self, *args: object) -> object:
            DownRedis.calls += 1
            raise RedisConnectionError("down")

    now = [0.0]
    limiter = _limiter(DownRedis(), sleeps, clock=lambda: now[0], bypass_seconds=5.0)
    await limiter.acquire("b", 10, high_priority=False)  # must not raise or hang
    await limiter.acquire("b", 10, high_priority=False)  # bypassed: Redis not even tried
    assert DownRedis.calls == 1 and sleeps == []
    now[0] = 6.0
    await limiter.acquire("b", 10, high_priority=False)  # retried after the bypass window
    assert DownRedis.calls == 2


async def test_limiter_times_out_slow_redis(sleeps: list[float]) -> None:
    import asyncio

    class SlowRedis:
        async def eval(self, *args: object) -> object:
            await asyncio.sleep(10)
            return "0"

    limiter = _limiter(SlowRedis(), sleeps, redis_timeout_seconds=0.05)
    await asyncio.wait_for(limiter.acquire("b", 10, high_priority=True), timeout=1)
