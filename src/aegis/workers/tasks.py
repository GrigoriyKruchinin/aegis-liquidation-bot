"""Celery tasks. Thin adapters: all logic lives in the orchestrator and notifier."""

from __future__ import annotations

import os
from typing import Any

from celery import Task
from celery.signals import worker_process_init, worker_process_shutdown, worker_ready

from aegis.config.settings import get_settings
from aegis.core.container import Container, describe_markets
from aegis.core.errors import NotFoundError, NotificationError, TelegramRateLimitError
from aegis.core.logging import configure_logging, get_logger
from aegis.core.redact import safe_error
from aegis.notifier.templates import render_opportunity, render_startup, render_status
from aegis.notifier.templates.market import render_digest, render_large_liquidation
from aegis.scanner.orchestrator import ScanMode, alert_key
from aegis.workers import runtime
from aegis.workers.app import app
from aegis.workers.dispatcher import (
    NOTIFY_DIGEST_TASK,
    NOTIFY_LIQUIDATION_TASK,
    NOTIFY_OPPORTUNITY_TASK,
    NOTIFY_STATUS_TASK,
    NOTIFY_SYSTEM_TASK,
)
from aegis.workers.redis_tools import beat_heartbeat, mark_sent, was_sent

logger = get_logger("workers.tasks")

NOTIFY_MAX_RETRIES = 5


@worker_process_init.connect
def _init_process(**_: Any) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)


@worker_process_shutdown.connect
def _shutdown_process(**_: Any) -> None:
    runtime.shutdown()


@worker_ready.connect
def _announce(**_: Any) -> None:
    """The scanner worker posts a startup message once it is ready."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if os.environ.get("AEGIS_WORKER_ROLE") != "scanner" or not settings.send_startup_message:
        return
    markets = describe_markets(settings)
    app.send_task(
        NOTIFY_SYSTEM_TASK, args=[render_startup(markets, settings.min_profit_usd)], queue="notify"
    )


def _locked_scan(chain: str, name: str, ttl: int, mode: ScanMode | None) -> dict[str, Any]:
    async def body(c: Container) -> dict[str, Any]:
        async with c.locks.hold(f"{name}:{chain}", ttl) as acquired:
            if not acquired:
                return {"skipped": "already running"}
            await beat_heartbeat(c.redis)
            if mode is None:
                results = await c.orchestrator.index_chain(chain)
                return {"discovered": sum(r.discovered for r in results)}
            scans = await c.orchestrator.scan_chain(chain, mode)
            return {
                "checked": sum(s.checked for s in scans),
                "liquidatable": sum(s.liquidatable for s in scans),
                "opportunities": sum(len(s.opportunities) for s in scans),
            }

    return runtime.run(body)


@app.task(name="aegis.scan.index", ignore_result=True)
def index_chain(chain: str) -> dict[str, Any]:
    """Discover new borrowers on `chain`."""
    return _locked_scan(chain, "index", 15 * 60, None)


@app.task(name="aegis.market.index", ignore_result=True)
def market_index(chain: str) -> dict[str, Any]:
    """Index liquidation events on `chain`."""

    async def body(c: Container) -> dict[str, Any]:
        async with c.locks.hold(f"market_index:{chain}", 30 * 60) as acquired:
            if not acquired:
                return {"skipped": "already running"}
            results = await c.orchestrator.index_liquidations(chain)
            return {"events": sum(r.discovered for r in results)}

    return runtime.run(body)


@app.task(name="aegis.market.enrich", ignore_result=True)
def market_enrich() -> dict[str, Any]:
    """Enrich pending liquidation events (receipts, USD value, latency, our coverage)."""

    async def body(c: Container) -> dict[str, Any]:
        async with c.locks.hold("market_enrich", 30 * 60) as acquired:
            if not acquired:
                return {"skipped": "already running"}
            done = await c.enricher.enrich_pending(c.settings.market_enrich_batch)
            return {"enriched": done}

    return runtime.run(body)


@app.task(name="aegis.scan.full", ignore_result=True)
def scan_full(chain: str) -> dict[str, Any]:
    """Health-check every active borrower on `chain`."""
    return _locked_scan(chain, "scan_full", 10 * 60, "full")


@app.task(name="aegis.scan.watchlist", ignore_result=True)
def scan_watchlist(chain: str) -> dict[str, Any]:
    """Fast re-check of near-liquidation borrowers on `chain`."""
    return _locked_scan(chain, "scan_watchlist", 2 * 60, "watchlist")


@app.task(
    name=NOTIFY_OPPORTUNITY_TASK,
    bind=True,
    max_retries=NOTIFY_MAX_RETRIES,
    ignore_result=True,
    acks_late=True,
)
def notify_opportunity(self: Task[Any, Any], opportunity_id: int) -> None:
    """Render and deliver an opportunity alert, recording the outcome."""

    async def body(c: Container) -> None:
        if await was_sent(c.redis, opportunity_id):
            logger.info("notification_already_sent", opportunity_id=opportunity_id)
            return
        try:
            summary = await c.opportunities.get(opportunity_id)
        except NotFoundError:
            logger.error("opportunity_missing", opportunity_id=opportunity_id)
            return
        try:
            await c.notifier.send(render_opportunity(summary))
        except NotificationError as exc:
            final = self.request.retries >= NOTIFY_MAX_RETRIES
            await c.notifications.record(
                opportunity_id, "telegram", "failed" if final else "retrying", safe_error(exc)
            )
            if final:
                # Let the next scan alert again instead of silencing the borrower for the TTL.
                await c.deduplicator.release(alert_key(summary))
                logger.error("notification_failed", opportunity_id=opportunity_id)
                return
            if isinstance(exc, TelegramRateLimitError):
                countdown = exc.retry_after_seconds
            else:
                countdown = min(300, 10 * 2**self.request.retries)
            raise self.retry(countdown=countdown, exc=exc) from exc
        await mark_sent(c.redis, opportunity_id)
        await c.notifications.record(opportunity_id, "telegram", "sent")
        logger.info("notification_sent", opportunity_id=opportunity_id)

    runtime.run(body)


@app.task(name=NOTIFY_SYSTEM_TASK, bind=True, max_retries=3, ignore_result=True)
def notify_system(self: Task[Any, Any], text: str) -> None:
    """Deliver a pre-rendered system message."""

    async def body(c: Container) -> None:
        try:
            await c.notifier.send(text)
        except NotificationError as exc:
            await c.notifications.record(None, "telegram", "failed", safe_error(exc))
            raise self.retry(countdown=30, exc=exc) from exc
        await c.notifications.record(None, "telegram", "sent")

    runtime.run(body)


@app.task(name=NOTIFY_STATUS_TASK, bind=True, max_retries=3, ignore_result=True)
def notify_status(self: Task[Any, Any]) -> None:
    """Build and deliver the periodic 📊 status report."""

    async def body(c: Container) -> None:
        report = await c.status_reporter.build(c.settings.status_report_interval_hours or 1)
        try:
            await c.notifier.send(render_status(report))
        except NotificationError as exc:
            await c.notifications.record(None, "telegram", "failed", safe_error(exc))
            raise self.retry(countdown=60, exc=exc) from exc
        await c.notifications.record(None, "telegram", "sent")

    runtime.run(body)


@app.task(name=NOTIFY_LIQUIDATION_TASK, bind=True, max_retries=3, ignore_result=True)
def notify_liquidation(self: Task[Any, Any], event_id: int) -> None:
    """Deliver the 🐋 large liquidation alert for an enriched event (idempotent)."""

    async def body(c: Container) -> None:
        if await was_sent(c.redis, event_id, "liquidation"):
            return
        try:
            record = await c.events.get_record(event_id)
        except NotFoundError:
            logger.error("liquidation_event_missing", event_id=event_id)
            return
        try:
            await c.notifier.send(render_large_liquidation(record))
        except NotificationError as exc:
            await c.notifications.record(None, "telegram", "failed", safe_error(exc))
            countdown = exc.retry_after_seconds if isinstance(exc, TelegramRateLimitError) else 60
            raise self.retry(countdown=countdown, exc=exc) from exc
        await mark_sent(c.redis, event_id, "liquidation")
        await c.notifications.record(None, "telegram", "sent")

    runtime.run(body)


@app.task(name=NOTIFY_DIGEST_TASK, bind=True, max_retries=3, ignore_result=True)
def notify_digest(self: Task[Any, Any]) -> None:
    """Build and deliver the daily 📈 liquidation market digest."""

    async def body(c: Container) -> None:
        digest = await c.digest_reporter.build(hours=24)
        day = int(digest.until.strftime("%Y%m%d"))  # one digest per day, even if redelivered
        if await was_sent(c.redis, day, "digest"):
            return
        try:
            await c.notifier.send(render_digest(digest))
        except NotificationError as exc:
            await c.notifications.record(None, "telegram", "failed", safe_error(exc))
            countdown = exc.retry_after_seconds if isinstance(exc, TelegramRateLimitError) else 120
            raise self.retry(countdown=countdown, exc=exc) from exc
        await mark_sent(c.redis, day, "digest")
        await c.notifications.record(None, "telegram", "sent")

    runtime.run(body)
