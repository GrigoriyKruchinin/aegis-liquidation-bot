"""Telegram Bot API notifier (HTML parse mode) over httpx."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import SecretStr

from aegis.core.errors import NotificationError, TelegramRateLimitError
from aegis.core.logging import get_logger
from aegis.core.redact import redact, safe_error

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096
MAX_INLINE_RETRY_AFTER_SECONDS = 30

logger = get_logger("notifier.telegram")


class TelegramNotifier:
    """`INotifier` sending HTML messages to one chat.

    Short 429 waits and transient errors are retried inline; long rate limits raise
    `TelegramRateLimitError` so the caller (Celery) can reschedule.

    Args:
        token: Bot token (never logged).
        chat_id: Target chat / group / channel id.
        client: Optional shared `httpx.AsyncClient` (created if omitted).
        max_attempts: Attempts for transient failures.
    """

    def __init__(
        self,
        token: SecretStr,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._max_attempts = max_attempts

    @property
    def _url(self) -> str:
        return f"{TELEGRAM_API}/bot{self._token.get_secret_value()}/sendMessage"

    async def send(self, text: str) -> None:
        """Send an HTML message.

        Raises:
            TelegramRateLimitError: when Telegram asks to wait longer than 30 seconds.
            NotificationError: on non-retryable API errors or exhausted retries.
        """
        if len(text) > MAX_MESSAGE_LENGTH:
            raise NotificationError(f"message too long ({len(text)} > {MAX_MESSAGE_LENGTH})")
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        last_error = "unknown error"
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                last_error = safe_error(exc)
                logger.warning("telegram_transport_error", attempt=attempt, error=last_error)
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 200:
                return
            body = self._json(response)
            description = redact(str(body.get("description", response.reason_phrase)))
            if response.status_code == 429:
                retry_after = int(body.get("parameters", {}).get("retry_after", 5))
                if retry_after > MAX_INLINE_RETRY_AFTER_SECONDS:
                    raise TelegramRateLimitError(retry_after)
                logger.warning("telegram_rate_limited", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                continue
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {description}"
                await asyncio.sleep(2**attempt)
                continue
            raise NotificationError(
                f"telegram rejected message: HTTP {response.status_code}: {description}"
            )
        raise NotificationError(f"telegram delivery failed after retries: {last_error}")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def aclose(self) -> None:
        """Close the HTTP client if owned."""
        if self._owns_client:
            await self._client.aclose()
