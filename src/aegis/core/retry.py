"""Async retry helper with exponential backoff and per-attempt timeout."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async[T](
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    timeout_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    base_delay_seconds: float = 0.25,
    max_delay_seconds: float = 4.0,
) -> T:
    """Run `func` with a timeout per attempt, retrying on the given exceptions.

    Args:
        func: Zero-argument coroutine factory.
        attempts: Total number of attempts (>= 1).
        timeout_seconds: Timeout applied to each attempt.
        retry_on: Exception types that trigger a retry. `TimeoutError` is always retried.
        base_delay_seconds: Initial backoff delay.
        max_delay_seconds: Backoff cap.

    Raises:
        The last exception raised by `func` when all attempts fail.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    catch: tuple[type[BaseException], ...] = (TimeoutError, *retry_on)
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            async with asyncio.timeout(timeout_seconds):
                return await func()
        except catch as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            delay = min(max_delay_seconds, base_delay_seconds * 2**attempt)
            await asyncio.sleep(delay * (0.5 + random.random() / 2))  # noqa: S311  # jitter only
    assert last_exc is not None  # noqa: S101  # loop ran at least once
    raise last_exc
