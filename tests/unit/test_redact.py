from __future__ import annotations

from aegis.core.redact import redact, safe_error


def test_redacts_url_paths_and_tokens() -> None:
    text = "failed url='https://base-mainnet.g.alchemy.com/v2/SECRETKEY' bot123:AAbb-cc"
    out = redact(text)
    assert "SECRETKEY" not in out
    assert "AAbb" not in out
    assert "https://base-mainnet.g.alchemy.com/***" in out


def test_safe_error_includes_type() -> None:
    out = safe_error(ValueError("see https://x.example/key?token=1"))
    assert out == "ValueError: see https://x.example/***"


def test_redacts_userinfo_credentials() -> None:
    text = "url='https://user:SECRETKEY@my-node.quiknode.pro/abc' wss://a:PW@h.io/x"
    out = redact(text)
    assert "SECRETKEY" not in out and "PW" not in out and "user" not in out
    assert "https://my-node.quiknode.pro/***" in out
    assert "wss://h.io/***" in out
