"""Shared pytest configuration.

Environment defaults are set before any `aegis` import so tests never depend on a local `.env`.
"""

from __future__ import annotations

import os

import pytest

_TEST_ENV = {
    "AEGIS_ENV_FILE": "",
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "TELEGRAM_CHAT_ID": "1",
    "ALCHEMY_API_KEY": "test-alchemy-key",
    "REDIS_URL": "redis://localhost:6379/15",
    "LOG_JSON": "false",
}
for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `integration` tests unless RUN_INTEGRATION=1."""
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration test: set RUN_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
