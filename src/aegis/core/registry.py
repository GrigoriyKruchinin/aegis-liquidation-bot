"""Generic name -> factory registry used for chains and protocols (Open/Closed principle)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar

from aegis.core.errors import ConfigurationError

F = TypeVar("F", bound=Callable[..., object])


class Registry[F: Callable[..., object]]:
    """Maps a string key to a factory. New implementations register themselves."""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._factories: dict[str, F] = {}

    def register(self, key: str) -> Callable[[F], F]:
        """Decorator registering `factory` under `key`."""

        def decorator(factory: F) -> F:
            if key in self._factories:
                raise ConfigurationError(f"{self._kind} '{key}' is already registered")
            self._factories[key] = factory
            return factory

        return decorator

    def get(self, key: str) -> F:
        """Return the factory for `key`.

        Raises:
            ConfigurationError: if nothing is registered under `key`.
        """
        try:
            return self._factories[key]
        except KeyError:
            raise ConfigurationError(f"unknown {self._kind} '{key}'") from None

    def __contains__(self, key: object) -> bool:
        return key in self._factories

    def __iter__(self) -> Iterator[str]:
        return iter(self._factories)
