"""Per-process asyncio runtime for Celery tasks.

Each worker process keeps one event loop and one `Container`, so RPC sessions, the DB pool
and Redis connections are reused across tasks instead of being rebuilt every run.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aegis.config.settings import get_settings
from aegis.core.container import Container
from aegis.workers.app import app
from aegis.workers.dispatcher import CeleryNotificationDispatcher

T = TypeVar("T")

_state: dict[str, object] = {}


def _loop() -> asyncio.AbstractEventLoop:
    pid = os.getpid()
    loop = _state.get("loop")
    if not isinstance(loop, asyncio.AbstractEventLoop) or _state.get("pid") != pid:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _state.clear()
        _state.update(loop=loop, pid=pid)
    return loop


def container() -> Container:
    """The process-wide container (built lazily)."""
    _loop()
    current = _state.get("container")
    if not isinstance(current, Container):
        current = Container.build(get_settings(), lambda: CeleryNotificationDispatcher(app))
        _state["container"] = current
    return current


def run[T](fn: Callable[[Container], Awaitable[T]]) -> T:
    """Run `fn(container)` to completion on the process event loop."""
    loop = _loop()
    return loop.run_until_complete(fn(container()))


def shutdown() -> None:
    """Close the container and the loop (called on worker process shutdown)."""
    loop = _state.get("loop")
    current = _state.get("container")
    if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed():
        if isinstance(current, Container):
            loop.run_until_complete(current.aclose())
        loop.close()
    _state.clear()
