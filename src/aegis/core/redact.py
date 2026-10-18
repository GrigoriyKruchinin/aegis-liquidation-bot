"""Redaction of secrets (RPC URLs with API keys, bot tokens) from error messages."""

from __future__ import annotations

import re

# scheme://[userinfo@]host[/path?query] -> scheme://host/***  (userinfo may hold credentials)
_URL_RE = re.compile(r"(?P<scheme>https?|wss?)://(?:[^/\s'\"@]*@)?(?P<host>[^/\s'\"@?#]+)[^\s'\"]*")
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Strip URL credentials/paths/queries (which may hold API keys) and Telegram bot tokens."""
    text = _BOT_TOKEN_RE.sub("bot***", text)
    return _URL_RE.sub(lambda m: f"{m['scheme']}://{m['host']}/***", text)


def safe_error(exc: BaseException) -> str:
    """Render an exception as `Type: message` with secrets redacted."""
    return redact(f"{type(exc).__name__}: {exc}")[:500]
