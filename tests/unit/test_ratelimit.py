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


async def test_shares_are_independent_no_starvation(sleeps: list[float]) -> None:
    redis = fakeredis.FakeAsyncRedis()
    limiter = _limiter(redis, sleeps)  # 100 CU/s: high 30, low 70; each holds >= 75 CU
    # A busy watcher drains its own share completely...
    for _ in range(3):
        await limiter.acquire("b", 25, high_priority=True)
    # ...and background work is not affected (this was a starvation bug before).
    for _ in range(3):
        await limiter.acquire("b", 25, high_priority=False)
    assert sleeps == []
    assert await redis.exists("aegis:rpc:budget:b:high", "aegis:rpc:budget:b:low") == 2


async def test_limiter_makes_caller_wait_when_its_share_is_spent() -> None:
    redis = fakeredis.FakeAsyncRedis()
    waited: list[float] = []

    async def sleep_and_refill(seconds: float) -> None:
        waited.append(seconds)
        await redis.delete("aegis:rpc:budget:b:low")  # the bucket refills while we wait

    limiter = RedisRateLimiter(redis, 100, 30, sleep=sleep_and_refill)
    for _ in range(3):  # 75 CU capacity
        await limiter.acquire("b", 25, high_priority=False)
    assert waited == []
    await limiter.acquire("b", 25, high_priority=False)
    assert len(waited) == 1 and 0 < waited[0] <= 1.0


async def test_small_budgets_charge_the_full_call_cost() -> None:
    redis = fakeredis.FakeAsyncRedis()
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)
        await redis.delete("aegis:rpc:budget:b:low")

    limiter = RedisRateLimiter(redis, 10, 30, sleep=record)  # low share = 7 CU/s
    for _ in range(2):  # 75-CU capacity: two 26-CU eth_calls fit, the third must wait
        await limiter.acquire("b", 26, high_priority=False)
    assert waited == []
    await limiter.acquire("b", 26, high_priority=False)
    assert waited  # charged 26, not min(26, 7)


async def test_zero_reserve_uses_one_shared_bucket(sleeps: list[float]) -> None:
    redis = fakeredis.FakeAsyncRedis()
    limiter = RedisRateLimiter(redis, 100, 0)
    await limiter.acquire("b", 10, high_priority=True)
    await limiter.acquire("b", 10, high_priority=False)
    assert await redis.exists("aegis:rpc:budget:b") == 1


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
