from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import fakeredis
import pytest
from celery.exceptions import Retry

from aegis.core.errors import NotFoundError, NotificationError
from aegis.workers import runtime, tasks
from tests.factories import summary
from tests.fakes import MemoryDeduplicator, MemoryOpportunityRepository


@dataclass
class FakeNotifier:
    error: Exception | None = None
    sent: list[str] = field(default_factory=list)

    async def send(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(text)


@dataclass
class FakeNotifications:
    records: list[tuple[int | None, str, str]] = field(default_factory=list)

    async def record(
        self, opportunity_id: int | None, channel: str, status: str, error: str | None = None
    ) -> None:
        self.records.append((opportunity_id, channel, status))


@dataclass
class FakeContainer:
    notifier: FakeNotifier = field(default_factory=FakeNotifier)
    opportunities: MemoryOpportunityRepository = field(default_factory=MemoryOpportunityRepository)
    notifications: FakeNotifications = field(default_factory=FakeNotifications)
    deduplicator: MemoryDeduplicator = field(default_factory=MemoryDeduplicator)
    redis: fakeredis.FakeAsyncRedis = field(default_factory=fakeredis.FakeAsyncRedis)


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> FakeContainer:
    fake = FakeContainer()

    def run(fn: Callable[[Any], Coroutine[Any, Any, Any]]) -> Any:
        import asyncio

        return asyncio.run(fn(fake))

    monkeypatch.setattr(runtime, "run", run)
    return fake


def _apply(opportunity_id: int, retries: int = 0) -> None:
    tasks.notify_opportunity.apply(args=(opportunity_id,), retries=retries, throw=True)


def test_notify_sends_and_records(container: FakeContainer) -> None:
    import asyncio

    asyncio.run(container.opportunities.add(summary()))
    _apply(1)
    assert len(container.notifier.sent) == 1
    assert "Liquidation Opportunity" in container.notifier.sent[0]
    assert container.notifications.records == [(1, "telegram", "sent")]


def test_notify_is_idempotent_after_success(container: FakeContainer) -> None:
    import asyncio

    asyncio.run(container.opportunities.add(summary()))
    _apply(1)
    _apply(1)  # e.g. redelivered after a worker crash (acks_late)
    assert len(container.notifier.sent) == 1


def test_notify_rate_limit_exhausted_records_failure(container: FakeContainer) -> None:
    import asyncio

    from aegis.core.errors import TelegramRateLimitError

    asyncio.run(container.opportunities.add(summary()))
    container.notifier.error = TelegramRateLimitError(120)
    with pytest.raises(Retry):
        _apply(1)
    _apply(1, retries=tasks.NOTIFY_MAX_RETRIES)
    assert container.notifications.records[-1] == (1, "telegram", "failed")
    assert len(container.deduplicator.released) == 1


def test_notify_retries_on_failure(container: FakeContainer) -> None:
    import asyncio

    asyncio.run(container.opportunities.add(summary()))
    container.notifier.error = NotificationError("boom")
    with pytest.raises(Retry):
        _apply(1)
    assert container.notifications.records == [(1, "telegram", "retrying")]


def test_notify_final_failure_releases_dedup(container: FakeContainer) -> None:
    import asyncio

    asyncio.run(container.opportunities.add(summary()))
    container.notifier.error = NotificationError("boom")
    _apply(1, retries=tasks.NOTIFY_MAX_RETRIES)
    assert container.notifications.records == [(1, "telegram", "failed")]
    assert len(container.deduplicator.released) == 1


def test_notify_missing_opportunity_is_dropped(
    container: FakeContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(_: int) -> Any:
        raise NotFoundError("gone")

    monkeypatch.setattr(container.opportunities, "get", missing)
    _apply(99)
    assert container.notifier.sent == []
