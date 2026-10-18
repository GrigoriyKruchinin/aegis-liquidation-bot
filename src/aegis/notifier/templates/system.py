"""System message templates (startup / health)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from aegis.config.chains import CHAINS
from aegis.core.models import MarketStatus, StatusReport
from aegis.notifier.templates.formatting import (
    SEPARATOR,
    compact_usd,
    duration,
    esc,
    ratio,
    usd,
)

#: Indexer lag below this is shown as "synced" (includes the reorg safety margin).
SYNCED_LAG_SECONDS = 300


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


def _index_state(row: MarketStatus) -> str:
    if row.index_lag_seconds is None:
        return "⏳ not indexed yet"
    if row.index_lag_seconds <= SYNCED_LAG_SECONDS:
        return "✅ synced"
    return f"⏳ indexing, ~{esc(duration(row.index_lag_seconds))} behind"


def _market_line(row: MarketStatus) -> str:
    stats = row.stats
    parts = [
        f"{stats.active_borrowers} borrowers",
        f"debt {esc(compact_usd(stats.total_debt_usd))}",
    ]
    if stats.at_risk:
        parts.append(f"⚠️ {stats.at_risk} at risk")
    if stats.min_health_factor is not None:
        parts.append(f"min HF <code>{esc(ratio(stats.min_health_factor, 3))}</code>")
    return f"  • <b>{esc(row.market_name)}:</b> {' · '.join(parts)} · {_index_state(row)}"


def render_status(report: StatusReport) -> str:
    """📊 Periodic status report: what is tracked, indexer progress and recent activity."""
    lines = [f"📊 <b>Aegis status</b> · last {report.period_hours}h", SEPARATOR]
    current_chain: str | None = None
    for row in report.markets:
        if row.chain != current_chain:
            current_chain = row.chain
            chain = CHAINS.get(row.chain)
            lines.append(f"🌐 <b>{esc(chain.display_name if chain else row.chain)}</b>")
        lines.append(_market_line(row))

    borrowers = sum(r.stats.active_borrowers for r in report.markets)
    debt = sum((r.stats.total_debt_usd for r in report.markets), Decimal(0))
    at_risk = sum(r.stats.at_risk for r in report.markets)
    activity = report.activity
    best = (
        f" (best {esc(usd(activity.best_net_profit_usd))})"
        if activity.best_net_profit_usd is not None
        else ""
    )
    scan = (
        "no scans yet"
        if report.last_scan_age_seconds is None
        else f"{esc(duration(report.last_scan_age_seconds))} ago"
    )
    failed = f" · ❌ failed: {activity.alerts_failed}" if activity.alerts_failed else ""
    lines += [
        SEPARATOR,
        f"🔎 <b>Opportunities:</b> {activity.opportunities}{best}",
        f"📨 <b>Alerts sent:</b> {activity.alerts_sent}{failed}",
        f"💓 <b>Last scan:</b> {scan}",
        "<blockquote>"
        f"Tracking {borrowers} borrowers with {esc(compact_usd(debt))} of debt; "
        f"{at_risk} close to liquidation. "
        "No alert means nothing profitable was liquidatable."
        "</blockquote>",
    ]
    return "\n".join(lines)
