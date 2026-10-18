"""Celery application: Redis broker/backend, JSON only, `scan` and `notify` queues."""

from __future__ import annotations

from typing import Any

from celery import Celery

from aegis.config.settings import SchedulerSettings, get_scheduler_settings


def build_beat_schedule(settings: SchedulerSettings) -> dict[str, dict[str, Any]]:
    """Per-chain periodic tasks (indexer sync, full scan) and the status report.

    Near-liquidation positions are re-checked every block by the `watcher` service, not here.
    """
    schedule: dict[str, dict[str, Any]] = {}
    for chain in settings.enabled_chains:
        schedule[f"index-{chain}"] = {
            "task": "aegis.scan.index",
            "schedule": float(settings.index_interval_seconds),
            "args": (chain,),
            "options": {"queue": "scan", "expires": settings.index_interval_seconds},
        }
        schedule[f"scan-full-{chain}"] = {
            "task": "aegis.scan.full",
            "schedule": float(settings.scan_interval_seconds),
            "args": (chain,),
            "options": {"queue": "scan", "expires": settings.scan_interval_seconds},
        }
    if settings.status_report_interval_hours > 0:
        interval = settings.status_report_interval_hours * 3600
        schedule["status-report"] = {
            "task": "aegis.notify.status",
            "schedule": float(interval),
            "options": {"queue": "notify", "expires": interval},
        }
    return schedule


def create_celery_app(settings: SchedulerSettings) -> Celery:
    """Create the configured Celery app."""
    app = Celery("aegis", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        result_expires=3600,
        timezone="UTC",
        enable_utc=True,
        task_default_queue="scan",
        task_routes={"aegis.scan.*": {"queue": "scan"}, "aegis.notify.*": {"queue": "notify"}},
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
        broker_connection_retry_on_startup=True,
        beat_schedule=build_beat_schedule(settings),
        include=["aegis.workers.tasks"],
    )
    return app


app = create_celery_app(get_scheduler_settings())
