"""🚨 Liquidation opportunity message template."""

from __future__ import annotations

from aegis.config.chains import CHAINS
from aegis.core.models import OpportunitySummary
from aegis.notifier.templates.formatting import SEPARATOR, amount, esc, ratio, short_address, usd

PROTOCOL_NAMES = {"aave_v3": "Aave v3", "compound_v3": "Compound v3"}


def render_opportunity(summary: OpportunitySummary) -> str:
    """Render an opportunity alert in Telegram HTML."""
    chain = CHAINS.get(summary.chain)
    network = chain.display_name if chain else summary.chain
    protocol = PROTOCOL_NAMES.get(summary.protocol, summary.protocol)
    borrower = esc(summary.borrower)
    short = esc(short_address(summary.borrower))
    borrower_link = (
        f'<a href="{esc(chain.address_url(summary.borrower))}">{short}</a>' if chain else short
    )
    hf_icon = "🔴" if summary.health_factor < 1 else "🟠"
    notes = f" · {esc(', '.join(summary.notes))}" if summary.notes else ""
    detected = summary.detected_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "🚨 <b>Liquidation Opportunity</b>",
        SEPARATOR,
        f"🌐 <b>Network:</b> {esc(network)}",
        f"🏦 <b>Protocol:</b> {esc(protocol)} · {esc(summary.market_name)}",
        f"👤 <b>Borrower:</b> {borrower_link}",
        f"<code>{borrower}</code>",
        f"{hf_icon} <b>Health Factor:</b> <code>{esc(ratio(summary.health_factor))}</code>",
        SEPARATOR,
        f"💳 <b>Debt to cover:</b> {esc(amount(summary.debt_to_cover))} {esc(summary.debt_symbol)}"
        f" (≈ {esc(usd(summary.debt_to_cover_usd))})",
        f"💎 <b>Collateral:</b> {esc(summary.collateral_symbol)}"
        f" (seize ≈ {esc(usd(summary.collateral_seized_usd))})",
        f"🎁 <b>Liquidation bonus:</b> {esc(ratio(summary.bonus_pct, 2))}%",
        f"⛽ <b>Gas (L2 + L1 data):</b> {esc(usd(summary.gas_cost_usd))}",
        SEPARATOR,
        f"💰 <b>Est. net profit: {esc(usd(summary.net_profit_usd))}</b>",
        "<blockquote>"
        f"Gross {esc(usd(summary.gross_profit_usd))} - gas {esc(usd(summary.gas_cost_usd))}."
        f" Self-funded estimate at block <code>{summary.block_number}</code>{notes}.\n"
        f"Position: debt {esc(usd(summary.total_debt_usd))}"
        f" / collateral {esc(usd(summary.total_collateral_usd))}"
        "</blockquote>",
        f"🕒 {esc(detected)} · read-only mode (V1)",
    ]
    return "\n".join(lines)
