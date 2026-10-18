from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from aegis.config.settings import AppSettings
from aegis.core.errors import ConfigurationError


def make(**kwargs: object) -> AppSettings:
    base: dict[str, object] = {
        "telegram_bot_token": SecretStr("1:abc"),
        "telegram_chat_id": "42",
        "alchemy_api_key": None,
    }
    base.update(kwargs)
    return AppSettings(_env_file=None, **base)  # type: ignore[arg-type]


def test_defaults() -> None:
    settings = make()
    assert settings.min_profit_usd == Decimal(5)
    assert settings.enabled_chains == ["base", "arbitrum", "optimism"]


def test_csv_lists_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLED_CHAINS", "Base, optimism")
    monkeypatch.setenv("BASE_RPC_URLS", "https://a.example/1,https://b.example/2")
    settings = make()
    assert settings.enabled_chains == ["base", "optimism"]
    assert [u.get_secret_value() for u in settings.base_rpc_urls] == [
        "https://a.example/1",
        "https://b.example/2",
    ]


def test_unknown_chain_rejected() -> None:
    with pytest.raises(ValidationError):
        make(enabled_chains=["base", "solana"])


def test_rpc_urls_explicit_first_then_alchemy() -> None:
    settings = make(
        alchemy_api_key=SecretStr("KEY"), base_rpc_urls=[SecretStr("https://x.example")]
    )
    urls = [u.get_secret_value() for u in settings.rpc_urls_for("base")]
    assert urls == ["https://x.example", "https://base-mainnet.g.alchemy.com/v2/KEY"]
    arb = settings.rpc_urls_for("arbitrum")[0].get_secret_value()
    assert arb == "https://arb-mainnet.g.alchemy.com/v2/KEY"
    opt = settings.rpc_urls_for("optimism")[0].get_secret_value()
    assert opt == "https://opt-mainnet.g.alchemy.com/v2/KEY"


def test_missing_rpc_raises() -> None:
    with pytest.raises(ConfigurationError, match="ALCHEMY_API_KEY"):
        make().rpc_urls_for("base")


def test_database_url_escapes_password() -> None:
    settings = make(
        postgres_password=SecretStr("p@ss/word"),
        postgres_host="db",
        postgres_port=5432,
        postgres_user="aegis",
        postgres_db="aegis",
    )
    url = settings.database_url.get_secret_value()
    assert url == "postgresql+asyncpg://aegis:p%40ss%2Fword@db:5432/aegis"


def test_secrets_not_in_repr() -> None:
    settings = make(alchemy_api_key=SecretStr("SUPERSECRET"))
    assert "SUPERSECRET" not in repr(settings)
    assert "1:abc" not in repr(settings)


def test_scoped_settings_need_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis.config.settings import DatabaseSettings, SchedulerSettings

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    scheduler = SchedulerSettings(_env_file=None, enabled_chains="base")  # type: ignore[arg-type]
    assert scheduler.enabled_chains == ["base"]
    assert "telegram_bot_token" not in SchedulerSettings.model_fields
    assert "telegram_bot_token" not in DatabaseSettings.model_fields
    assert (
        DatabaseSettings(_env_file=None)
        .database_url.get_secret_value()
        .startswith("postgresql+asyncpg://")
    )
