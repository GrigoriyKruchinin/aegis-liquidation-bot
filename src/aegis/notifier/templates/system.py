"""System message templates (startup / health)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from aegis.config.chains import CHAINS
from aegis.notifier.templates.formatting import SEPARATOR, esc, usd


def render_startup(markets_by_chain: Mapping[str, list[str]], min_profit_usd: Decimal) -> str:
    """🟢 Scanner online message listing monitored markets."""
    lines = ["🟢 <b>Aegis scanner online</b>", SEPARATOR]
    for chain_name, markets in markets_by_chain.items():
        chain = CHAINS.get(chain_name)
        title = chain.display_name if chain else chain_name
        lines.append(f"🌐 <b>{esc(title)}:</b> {esc(', '.join(markets) or '—')}")
    lines += [
        SEPARATOR,
        f"🎯 <b>Alert threshold:</b> net profit ≥ {esc(usd(min_profit_usd))}",
        "<blockquote>Mode: read-only detector (V1). No transactions are sent.</blockquote>",
    ]
    return "\n".join(lines)
