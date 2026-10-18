"""Celery-backed notification dispatcher."""

from __future__ import annotations

import asyncio

from celery import Celery

from aegis.core.errors import NotificationError
from aegis.core.models import OpportunitySummary

NOTIFY_OPPORTUNITY_TASK = "aegis.notify.opportunity"
NOTIFY_SYSTEM_TASK = "aegis.notify.system"
NOTIFY_STATUS_TASK = "aegis.notify.status"


class CeleryNotificationDispatcher:
    """`INotificationDispatcher` enqueueing the notify task by opportunity id."""

    def __init__(self, app: Celery) -> None:
        self._app = app

    async def dispatch(self, opportunity: OpportunitySummary) -> None:
        """Enqueue delivery (the broker publish runs in a thread to keep the loop free)."""
        if opportunity.id is None:
            raise NotificationError("cannot dispatch an unsaved opportunity")
        await asyncio.to_thread(
            self._app.send_task, NOTIFY_OPPORTUNITY_TASK, args=[opportunity.id], queue="notify"
        )
