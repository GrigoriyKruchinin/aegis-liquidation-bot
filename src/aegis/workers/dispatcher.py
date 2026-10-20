"""Celery-backed notification dispatcher."""

from __future__ import annotations

import asyncio

from celery import Celery
from kombu.exceptions import KombuError

from aegis.core.errors import NotificationError
from aegis.core.models import OpportunitySummary

NOTIFY_OPPORTUNITY_TASK = "aegis.notify.opportunity"
NOTIFY_SYSTEM_TASK = "aegis.notify.system"
NOTIFY_STATUS_TASK = "aegis.notify.status"
NOTIFY_LIQUIDATION_TASK = "aegis.notify.liquidation"
NOTIFY_DIGEST_TASK = "aegis.notify.digest"


class CeleryNotificationDispatcher:
    """`INotificationDispatcher` + `ILiquidationAlertDispatcher` enqueueing notify tasks by id."""

    def __init__(self, app: Celery) -> None:
        self._app = app

    async def dispatch(self, opportunity: OpportunitySummary) -> None:
        """Enqueue delivery (the broker publish runs in a thread to keep the loop free)."""
        if opportunity.id is None:
            raise NotificationError("cannot dispatch an unsaved opportunity")
        await asyncio.to_thread(
            self._app.send_task, NOTIFY_OPPORTUNITY_TASK, args=[opportunity.id], queue="notify"
        )

    async def dispatch_liquidation(self, event_id: int) -> None:
        """Enqueue the 🐋 large liquidation alert for an enriched event."""
        try:
            await asyncio.to_thread(
                self._app.send_task, NOTIFY_LIQUIDATION_TASK, args=[event_id], queue="notify"
            )
        except (KombuError, OSError) as exc:
            raise NotificationError(f"broker publish failed: {type(exc).__name__}") from exc
