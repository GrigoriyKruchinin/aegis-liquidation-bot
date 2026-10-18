"""Lending protocol adapters. Importing this package registers all built-in protocols."""

from __future__ import annotations

from aegis.protocols import aave_v3, compound_v3
from aegis.protocols.base import PROTOCOLS, ProtocolDeps, ProtocolFactory

__all__ = ["PROTOCOLS", "ProtocolDeps", "ProtocolFactory", "aave_v3", "compound_v3"]
