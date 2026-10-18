"""Exception hierarchy for Aegis Liquidations.

Messages must never contain secrets (RPC URLs with keys, bot tokens, private keys).
"""

from __future__ import annotations


class AegisError(Exception):
    """Base class for all application errors."""


class ConfigurationError(AegisError):
    """Invalid or missing configuration."""


class RpcError(AegisError):
    """An RPC request failed after all retries and fallbacks."""


class RpcTimeoutError(RpcError):
    """An RPC request timed out."""


class LogRangeTooLargeError(RpcError):
    """The RPC node rejected an `eth_getLogs` request because the block range is too large."""


class ChainDataNotFoundError(RpcError):
    """The node does not know the requested transaction / block (lagging or pruned node)."""


class HistoricalStateUnavailableError(RpcError):
    """The node cannot serve state at an old block (not an archive node). Not transient."""


class ContractCallError(AegisError):
    """A contract call reverted or returned undecodable data."""


class AbiDecodingError(ContractCallError):
    """Return data could not be decoded with the expected ABI types."""


class ProtocolError(AegisError):
    """A lending protocol adapter could not complete an operation."""


class PriceUnavailableError(AegisError):
    """A price feed returned a stale, zero or negative price."""


class NotificationError(AegisError):
    """A notification could not be delivered."""


class TelegramRateLimitError(NotificationError):
    """Telegram returned HTTP 429."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"telegram rate limit, retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class RepositoryError(AegisError):
    """A persistence operation failed."""


class NotFoundError(RepositoryError):
    """A requested record does not exist."""
