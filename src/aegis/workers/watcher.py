"""Fast watcher service entry point: ``python -m aegis.workers.watcher``.

Runs one `ChainWatcher` per enabled chain in a single asyncio process (outside Celery, whose
task granularity is too coarse for per-block detection). Alerts go through the same
dedup + Celery `notify` queue as the scheduled scans.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from aegis.config.settings import get_settings
from aegis.core.container import Container
from aegis.core.logging import configure_logging, get_logger
from aegis.scanner.watcher import ChainWatcher, WatcherConfig
from aegis.workers.app import app
from aegis.workers.dispatcher import CeleryNotificationDispatcher
from aegis.workers.redis_tools import record_heartbeat

logger = get_logger("workers.watcher")


async def run() -> None:
    """Build the container and run all chain watchers until SIGTERM/SIGINT."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    container = Container.build(settings, lambda: CeleryNotificationDispatcher(app))
    config = WatcherConfig(
        min_interval_ms=settings.watcher_min_interval_ms,
        head_poll_ms=settings.watcher_head_poll_ms,
        price_move_bps=settings.watcher_price_move_bps,
        rescan_cooldown_seconds=settings.watcher_rescan_cooldown_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def heartbeat() -> None:
        await record_heartbeat(container.redis, "watcher")

    watchers = [
        ChainWatcher(
            container.providers[chain],
            markets,
            container.orchestrator,
            config,
            on_tick=heartbeat,
        )
        for chain, markets in container.markets.items()
        if markets
    ]
    try:
        await asyncio.gather(*(w.run(stop) for w in watchers))
    finally:
        await container.aclose()
        logger.info("watcher_stopped")


def main() -> None:
    """Process entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
