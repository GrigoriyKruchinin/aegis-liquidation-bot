"""Container healthchecks.

Usage: ``python -m aegis.workers.healthcheck heartbeat --name scan --max-age 180``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from redis.asyncio import Redis

from aegis.config.settings import get_scheduler_settings
from aegis.workers.redis_tools import heartbeat_age_seconds


async def _check_heartbeat(name: str, max_age: float) -> int:
    redis = Redis.from_url(get_scheduler_settings().redis_url)
    try:
        age = await heartbeat_age_seconds(redis, name)
    finally:
        await redis.aclose()
    if age is None:
        print("no heartbeat yet")
        return 1
    print(f"heartbeat age {age:.0f}s")
    return 0 if age <= max_age else 1


def main() -> int:
    """Entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description="Aegis healthchecks")
    parser.add_argument("check", choices=["heartbeat"])
    parser.add_argument("--name", default="scan", help="heartbeat name (scan | watcher)")
    parser.add_argument("--max-age", type=float, default=180.0)
    args = parser.parse_args()
    return asyncio.run(_check_heartbeat(args.name, args.max_age))


if __name__ == "__main__":
    sys.exit(main())
