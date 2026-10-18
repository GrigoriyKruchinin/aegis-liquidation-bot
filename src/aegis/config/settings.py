"""Application settings loaded from environment variables / `.env`."""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from aegis.config.chains import CHAINS
from aegis.core.errors import ConfigurationError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def _split_csv(value: object) -> object:
    """Allow list settings to be given as comma-separated strings."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class _EnvSettings(BaseSettings):
    """Shared loader configuration for every settings group."""

    model_config = SettingsConfigDict(
        # AEGIS_ENV_FILE="" disables the file (used by tests); default is `.env`.
        env_file=os.environ.get("AEGIS_ENV_FILE", ".env") or None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---------------------------------------------------------------------------
    app_env: Literal["local", "production"] = "local"
    log_level: LogLevel = "INFO"
    log_json: bool = True


class DatabaseSettings(_EnvSettings):
    """PostgreSQL connection only (used by migrations; no other secrets required)."""

    postgres_user: str = "aegis"
    postgres_password: SecretStr = SecretStr("aegis")
    postgres_db: str = "aegis"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @property
    def database_url(self) -> SecretStr:
        """Async SQLAlchemy URL (contains the password, hence `SecretStr`)."""
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return SecretStr(
            f"postgresql+asyncpg://{quote(self.postgres_user, safe='')}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


class SchedulerSettings(_EnvSettings):
    """Broker and schedule only (used by Celery beat and healthchecks; no secrets)."""

    redis_url: str = "redis://localhost:6379/0"
    enabled_chains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["base", "arbitrum", "optimism"]
    )
    index_interval_seconds: int = Field(default=60, ge=5)
    scan_interval_seconds: int = Field(default=30, ge=5)
    #: Periodic "bot is alive" Telegram report; 0 disables it.
    status_report_interval_hours: int = Field(default=1, ge=0, le=168)
    #: Market measurement: index competitors' liquidations and analyse them.
    market_enabled: bool = True
    market_index_interval_seconds: int = Field(default=300, ge=30)
    market_enrich_interval_seconds: int = Field(default=120, ge=30)

    @field_validator("enabled_chains", mode="before")
    @classmethod
    def _parse_chains_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("enabled_chains")
    @classmethod
    def _validate_chains(cls, value: list[str]) -> list[str]:
        normalized = [name.lower() for name in value]
        unknown = sorted(set(normalized) - CHAINS.keys())
        if unknown:
            raise ValueError(f"unknown chains: {', '.join(unknown)}; known: {sorted(CHAINS)}")
        if not normalized:
            raise ValueError("at least one chain must be enabled")
        return normalized


class AppSettings(DatabaseSettings, SchedulerSettings):
    """Full configuration for scanner and notifier workers.

    All secrets are `SecretStr` and must never be logged. V1 loads no private keys.
    """

    # --- Telegram ----------------------------------------------------------------------------
    telegram_bot_token: SecretStr
    telegram_chat_id: str = Field(min_length=1)

    # --- RPC ---------------------------------------------------------------------------------
    alchemy_api_key: SecretStr | None = None
    base_rpc_urls: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    arbitrum_rpc_urls: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    optimism_rpc_urls: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    rpc_timeout_seconds: float = Field(default=15.0, gt=0)
    rpc_max_retries: int = Field(default=3, ge=0, le=10)
    rpc_max_concurrency: int = Field(default=8, ge=1, le=64)
    multicall_batch_size: int = Field(default=150, ge=1, le=1000)
    #: Shared throughput budget per RPC provider key, in compute units/s across ALL chains and
    #: processes (Alchemy limits CUPS per app). 0 disables client-side rate limiting.
    rpc_cu_per_second: int = Field(default=300, ge=0)
    #: Share of the budget reserved for the latency-critical watcher (indexer/scans use the rest).
    rpc_reserve_pct: int = Field(default=30, ge=0, le=90)

    # --- Scanning ----------------------------------------------------------------------------
    min_profit_usd: Decimal = Field(default=Decimal("5"), ge=0)
    watchlist_hf_threshold: Decimal = Field(default=Decimal("1.05"), gt=1)
    min_debt_usd: Decimal = Field(default=Decimal("10"), ge=0)
    # --- Fast watcher (per-block hot-set checks + price-move triggers) ----------------------
    watcher_min_interval_ms: int = Field(default=1000, ge=100, le=60_000)
    watcher_head_poll_ms: int = Field(default=200, ge=50, le=10_000)
    watcher_price_move_bps: int = Field(default=50, ge=1, le=5000)
    watcher_rescan_cooldown_seconds: int = Field(default=5, ge=1, le=600)
    # --- Market measurement ------------------------------------------------------------------
    market_lookback_days: int = Field(default=30, ge=1, le=365)
    market_log_chunk_blocks: int = Field(default=10_000, ge=10)
    market_max_blocks_per_run: int = Field(default=1_000_000, ge=1000)
    #: How far back the binary search looks for the start of a liquidatable window.
    market_lag_window_minutes: int = Field(default=120, ge=1, le=24 * 60)
    market_enrich_batch: int = Field(default=25, ge=1, le=500)
    #: Dust liquidations below this gross profit skip the archive lag search (saves RPC).
    market_lag_min_gross_usd: Decimal = Field(default=Decimal(1), ge=0)
    indexer_lookback_days: int = Field(default=30, ge=1)
    indexer_log_chunk_blocks: int = Field(default=2000, ge=1)
    indexer_max_blocks_per_run: int = Field(default=200_000, ge=1)

    # --- Notifications -----------------------------------------------------------------------
    alert_dedup_ttl_seconds: int = Field(default=3600, ge=60)
    send_startup_message: bool = True

    @field_validator("base_rpc_urls", "arbitrum_rpc_urls", "optimism_rpc_urls", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    def rpc_urls_for(self, chain: str) -> list[SecretStr]:
        """Resolve RPC URLs for a chain: explicit list first, then the Alchemy key.

        Explicit URLs and the Alchemy URL are combined (explicit first) to form a fallback list.

        Raises:
            ConfigurationError: if no RPC URL can be resolved for the chain.
        """
        explicit: dict[str, list[SecretStr]] = {
            "base": self.base_rpc_urls,
            "arbitrum": self.arbitrum_rpc_urls,
            "optimism": self.optimism_rpc_urls,
        }
        urls = list(explicit.get(chain, []))
        chain_config = CHAINS.get(chain)
        if self.alchemy_api_key is not None and chain_config and chain_config.alchemy_network:
            key = self.alchemy_api_key.get_secret_value()
            urls.append(SecretStr(f"https://{chain_config.alchemy_network}.g.alchemy.com/v2/{key}"))
        # The same URL twice is no fallback (same node, same quota): keep the first occurrence.
        unique: dict[str, SecretStr] = {}
        for url in urls:
            unique.setdefault(url.get_secret_value().rstrip("/"), url)
        urls = list(unique.values())
        if not urls:
            raise ConfigurationError(
                f"no RPC URL for chain '{chain}': set ALCHEMY_API_KEY or {chain.upper()}_RPC_URLS"
            )
        return urls


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return the process-wide full settings instance."""
    return AppSettings()


@lru_cache(maxsize=1)
def get_database_settings() -> DatabaseSettings:
    """Database-only settings (migrations)."""
    return DatabaseSettings()


@lru_cache(maxsize=1)
def get_scheduler_settings() -> SchedulerSettings:
    """Broker/schedule-only settings (Celery app, beat, healthchecks)."""
    return SchedulerSettings()
