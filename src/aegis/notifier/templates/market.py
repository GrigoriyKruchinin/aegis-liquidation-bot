"""Liquidation market templates: 📈 daily digest and 🐋 large liquidation alert."""

from __future__ import annotations

from decimal import Decimal

from aegis.config.chains import CHAINS
from aegis.core.models import LiquidationRecord, MarketDigest
from aegis.notifier.templates.formatting import (
    SEPARATOR,
    compact_usd,
    esc,
    short_address,
    usd,
)


def _chain_name(chain: str) -> str:
    config = CHAINS.get(chain)
    return config.display_name if config else chain


def _address_link(chain: str, address: str) -> str:
    config = CHAINS.get(chain)
    short = esc(short_address(address))
    return f'<a href="{esc(config.address_url(address))}">{short}</a>' if config else short


def _tx_link(chain: str, tx_hash: str, label: str) -> str:
    config = CHAINS.get(chain)
    return f'<a href="{esc(config.tx_url(tx_hash))}">{esc(label)}</a>' if config else esc(label)


def _pair(record: LiquidationRecord) -> str:
    return f"{record.debt_symbol or '?'}/{record.collateral_symbol or '?'}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _net(record: LiquidationRecord) -> Decimal:
    return record.net_profit_usd if record.net_profit_usd is not None else Decimal(0)


def render_digest(digest: MarketDigest) -> str:
    """📈 Daily summary: volume per chain, biggest liquidations, top liquidators, speed."""
    period = (
        f"{digest.since.strftime('%Y-%m-%d %H:%M')} → {digest.until.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    lines = ["📈 <b>Liquidation market · last 24h</b>", f"<i>{esc(period)}</i>", SEPARATOR]
    if not digest.chains:
        lines.append("😴 No paid liquidations on the monitored markets.")
    for chain in digest.chains:
        lines.append(
            f"🌐 <b>{esc(_chain_name(chain.chain))}:</b>"
            f" {_plural(chain.liquidations, 'liquidation')}"
            f" · volume {esc(compact_usd(chain.volume_usd))}"
            f" · liquidators net {esc(usd(chain.net_profit_usd))}"
        )
    lines += [
        SEPARATOR,
        f"🎯 <b>Net ≥ {esc(usd(digest.threshold_usd))}:</b> {digest.meaningful}"
        f" · {esc(usd(digest.meaningful_net_usd))} in total",
    ]
    if digest.lag_measured:
        share = 100 * digest.same_block // digest.lag_measured
        lines.append(
            f"⚡ <b>Same-block liquidations:</b> {digest.same_block}/{digest.lag_measured}"
            f" ({share}%)"
        )
    if digest.top_liquidations:
        lines += [SEPARATOR, "🏆 <b>Largest liquidations</b>"]
        for index, record in enumerate(digest.top_liquidations, start=1):
            lines.append(
                f"{index}. {_tx_link(record.chain, record.tx_hash, usd(_net(record)))}"
                f" · {esc(_chain_name(record.chain))} · {esc(record.market_name)}"
                f" · {esc(_pair(record))}"
            )
    if digest.top_liquidators:
        lines += [SEPARATOR, "🥇 <b>Top liquidators</b>"]
        for index, stats in enumerate(digest.top_liquidators, start=1):
            home = stats.chains[0] if stats.chains else ""
            lines.append(
                f"{index}. {_address_link(home, stats.liquidator)}"
                f" · {_plural(stats.liquidations, 'liquidation')}"
                f" · {esc(usd(stats.net_profit_usd))}"
                f" · {esc(', '.join(_chain_name(c) for c in stats.chains))}"
            )
    notes = ["Net = collateral received - amount paid - gas."]
    notes.append(
        f"{digest.pending} events are still being enriched and are not counted yet."
        if digest.pending
        else "All indexed events are enriched."
    )
    if digest.unpriced:
        notes.append(f"Excluded (could not be priced): {digest.unpriced}.")
    lines.append(f"<blockquote>{esc(' '.join(notes))}</blockquote>")
    return "\n".join(lines)


def render_large_liquidation(record: LiquidationRecord) -> str:
    """🐋 A liquidation with a large profit was just observed on-chain."""
    if record.lag_blocks is None or record.lag_capped:
        speed = "not measured"
    elif record.lag_blocks == 0:
        speed = "same block as it became liquidatable"
    else:
        speed = f"{record.lag_blocks} blocks after it became liquidatable"
    position = "—" if record.tx_index is None else str(record.tx_index)
    lines = [
        "🐋 <b>Large liquidation</b>",
        SEPARATOR,
        f"🌐 <b>Network:</b> {esc(_chain_name(record.chain))}",
        f"🏦 <b>Market:</b> {esc(record.market_name)} · {esc(_pair(record))}",
        f"🤖 <b>Liquidator:</b> {_address_link(record.chain, record.liquidator)}",
        f"<code>{esc(record.liquidator)}</code>",
        SEPARATOR,
        f"💳 <b>Debt repaid:</b> {esc(usd(record.debt_usd or Decimal(0)))}",
        f"⛽ <b>Gas:</b> {esc(usd(record.gas_cost_usd or Decimal(0)))}",
        f"💰 <b>Liquidator net: {esc(usd(_net(record)))}</b>",
        f"⚡ <b>Speed:</b> {esc(speed)} · tx #{esc(position)} in block",
        SEPARATOR,
        f"🔗 {_tx_link(record.chain, record.tx_hash, 'Transaction')}"
        f" · block <code>{record.block_number}</code>",
        f"🕒 {esc(record.block_time.strftime('%Y-%m-%d %H:%M:%S UTC'))}",
    ]
    return "\n".join(lines)
