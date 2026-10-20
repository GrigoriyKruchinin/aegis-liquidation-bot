from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from aegis.core.errors import NotificationError, TelegramRateLimitError
from aegis.notifier.telegram import TelegramNotifier
from aegis.notifier.templates import render_opportunity, render_startup
from aegis.notifier.templates.formatting import amount, esc, ratio, usd
from tests.factories import summary

TOKEN = "123456:SECRET-TOKEN"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def test_formatting_helpers() -> None:
    assert usd(Decimal("1234.567")) == "$1,234.57"
    assert usd(Decimal("0.03124")) == "$0.0312"
    assert usd(Decimal("-2.5")) == "-$2.50"
    assert amount(Decimal("1000.123456")) == "1,000.12"
    assert amount(Decimal("0.500000")) == "0.5"
    assert ratio(Decimal("0.971234")) == "0.9712"
    assert esc("<b>&") == "&lt;b&gt;&amp;"


def test_opportunity_template_contents() -> None:
    text = render_opportunity(summary())
    assert "🚨 <b>Liquidatable Position</b>" in text
    assert "Base" in text and "Aave v3" in text
    assert "<code>0x1111111111111111111111111111111111111111</code>" in text
    assert "https://basescan.org/address/0x1111111111111111111111111111111111111111" in text
    assert "<code>0.9712</code>" in text
    assert "$44.97" in text
    assert "<blockquote>" in text and "</blockquote>" in text
    assert len(text) < 4096


def test_opportunity_template_escapes_untrusted_values() -> None:
    text = render_opportunity(summary(debt_symbol="<script>x</script>", market_name="A&B"))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "A&amp;B" in text


def test_startup_template() -> None:
    text = render_startup({"base": ["Aave v3", "cUSDCv3"], "optimism": []}, Decimal(5))
    assert "🟢" in text and "Aave v3, cUSDCv3" in text and "$5.00" in text
    assert "Optimism" in text


@pytest.fixture
def notifier() -> TelegramNotifier:
    return TelegramNotifier(SecretStr(TOKEN), "42", client=httpx.AsyncClient())


@respx.mock
async def test_send_success(notifier: TelegramNotifier) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await notifier.send("<b>hi</b>")
    payload = route.calls.last.request.content.decode()
    assert '"parse_mode":"HTML"' in payload and '"chat_id":"42"' in payload


@respx.mock
async def test_short_rate_limit_retried(
    notifier: TelegramNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("aegis.notifier.telegram.asyncio.sleep", no_sleep)
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    await notifier.send("x")


@respx.mock
async def test_long_rate_limit_raises(notifier: TelegramNotifier) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 120}})
    )
    with pytest.raises(TelegramRateLimitError) as info:
        await notifier.send("x")
    assert info.value.retry_after_seconds == 120


@respx.mock
async def test_client_error_raises_without_token(notifier: TelegramNotifier) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )
    )
    with pytest.raises(NotificationError) as info:
        await notifier.send("x")
    assert "chat not found" in str(info.value)
    assert "SECRET-TOKEN" not in str(info.value)


async def test_too_long_message_rejected(notifier: TelegramNotifier) -> None:
    with pytest.raises(NotificationError):
        await notifier.send("x" * 5000)
