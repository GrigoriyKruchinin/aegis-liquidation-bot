"""Liquidation market report built from the collected analytics data.

Usage (inside the stack, see ``make market-report``)::

    python -m aegis.tools.market_report --days 30                 # Markdown
    python -m aegis.tools.market_report --days 30 --format json
    python -m aegis.tools.market_report --days 30 --format csv --table events
    python -m aegis.tools.market_report --days 30 --format csv --table sightings

Only the database is needed (no RPC, no secrets beyond the DB password).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from aegis.config.chains import CHAINS
from aegis.config.settings import get_database_settings
from aegis.db.models import LiquidationEventRow, SightingRow, UptimeRow
from aegis.db.session import create_engine, create_session_factory

LIQUIDATION_KINDS = ("aave_liquidation", "comet_absorb")
PROFIT_KINDS = ("aave_liquidation", "comet_buy")
PROFIT_BUCKETS: tuple[tuple[str, Decimal | None, Decimal | None], ...] = (
    ("< $0 (loss)", None, Decimal(0)),
    ("$0 - $5", Decimal(0), Decimal(5)),
    ("$5 - $50", Decimal(5), Decimal(50)),
    ("$50 - $500", Decimal(50), Decimal(500)),
    ("≥ $500", Decimal(500), None),
)
MEANINGFUL_NET_USD = Decimal(5)
LAG_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("same block (0)", 0, 0),
    ("1 block", 1, 1),
    ("2-5 blocks", 2, 5),
    ("6-30 blocks", 6, 30),
    ("31-300 blocks", 31, 300),
    ("> 300 blocks", 301, None),
)
EVENT_COLUMNS = (
    "block_time", "chain", "protocol", "market_name", "kind", "block_number", "tx_hash",
    "tx_index", "borrower", "liquidator", "tx_from", "tx_to", "debt_symbol", "collateral_symbol",
    "debt_usd", "collateral_usd", "gross_profit_usd", "gas_cost_usd", "net_profit_usd",
    "gas_used", "effective_gas_price", "l1_fee", "lag_blocks", "lag_capped",
    "liquidatable_since_block", "follow_up", "data_gaps", "our_first_seen_at",
    "our_lead_seconds", "bot_online",
    "borrower_known", "enriched", "enrich_error",
)  # fmt: skip
SIGHTING_COLUMNS = (
    "first_seen_at", "last_seen_at", "chain", "protocol", "market_name", "borrower",
    "observations", "min_health_factor", "max_debt_usd", "best_net_profit_usd",
    "best_gross_profit_usd", "closed", "closed_reason",
)  # fmt: skip


@dataclass
class Data:
    """Rows of the report period."""

    since: datetime
    until: datetime
    events: list[LiquidationEventRow]
    sightings: list[SightingRow]
    uptime_minutes: dict[str, int] = field(default_factory=dict)


async def load(days: int) -> Data:
    """Load events, sightings and uptime of the last `days` days."""
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    engine = create_engine(get_database_settings().database_url, pool_size=1)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            events = list(
                (
                    await session.scalars(
                        select(LiquidationEventRow)
                        .where(
                            (LiquidationEventRow.block_time >= since)
                            | LiquidationEventRow.block_time.is_(None)
                        )
                        .order_by(LiquidationEventRow.block_number)
                    )
                ).all()
            )
            sightings = list(
                (
                    await session.scalars(
                        select(SightingRow).where(SightingRow.first_seen_at >= since)
                    )
                ).all()
            )
            uptime_rows = (
                await session.execute(
                    select(UptimeRow.chain, func.count())
                    .where(UptimeRow.minute >= since)
                    .group_by(UptimeRow.chain)
                )
            ).all()
    finally:
        await engine.dispose()
    return Data(since, until, events, sightings, {c: int(n) for c, n in uptime_rows})


def _d(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal(0)


def _usd(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _median(values: Iterable[float]) -> float | None:
    items = list(values)
    return statistics.median(items) if items else None


def _pct(part: int, whole: int) -> str:
    return "—" if whole == 0 else f"{100 * part / whole:.0f}%"


def _lag_buckets(events: Sequence[LiquidationEventRow]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, min_lag, max_lag in LAG_BUCKETS:
        count = sum(
            1
            for e in events
            if not e.lag_capped
            and e.lag_blocks is not None
            and e.lag_blocks >= min_lag
            and (max_lag is None or e.lag_blocks <= max_lag)
        )
        rows.append({"bucket": label, "count": count})
    rows.append(
        {
            "bucket": "already liquidatable at window start (capped)",
            "count": sum(1 for e in events if e.lag_capped),
        }
    )
    return rows


def summarize(data: Data) -> dict[str, Any]:
    """Compute every figure of the report (also used for the JSON output)."""
    period_minutes = max(1, int((data.until - data.since).total_seconds() // 60))
    enriched = [e for e in data.events if e.enriched]
    liquidations = [e for e in enriched if e.kind in LIQUIDATION_KINDS]
    profit_events = [e for e in enriched if e.kind in PROFIT_KINDS and e.net_profit_usd is not None]

    per_market: dict[str, dict[str, Any]] = {}
    for event in enriched:
        key = f"{event.chain} · {event.market_name}"
        row = per_market.setdefault(
            key,
            {"liquidations": 0, "buys": 0, "debt_usd": Decimal(0), "gross_usd": Decimal(0),
             "gas_usd": Decimal(0), "net_usd": Decimal(0), "gross_values": []},
        )  # fmt: skip
        if event.kind in LIQUIDATION_KINDS:
            row["liquidations"] += 1
        if event.kind == "comet_buy":
            row["buys"] += 1
        if event.kind in PROFIT_KINDS:
            row["debt_usd"] += _d(event.debt_usd)
            row["gross_usd"] += _d(event.gross_profit_usd)
            row["gas_usd"] += _d(event.gas_cost_usd)
            row["net_usd"] += _d(event.net_profit_usd)
            if event.gross_profit_usd is not None:
                row["gross_values"].append(float(event.gross_profit_usd))

    buckets = []
    for label, low, high in PROFIT_BUCKETS:
        selected = [
            e for e in profit_events
            if (low is None or _d(e.net_profit_usd) >= low)
            and (high is None or _d(e.net_profit_usd) < high)
        ]  # fmt: skip
        buckets.append(
            {"bucket": label, "count": len(selected),
             "net_usd": sum((_d(e.net_profit_usd) for e in selected), Decimal(0))}
        )  # fmt: skip

    liquidators: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "gross_usd": Decimal(0), "lags": [], "tx_index": [], "gas": [],
                 "chains": set(), "senders": Counter()}
    )  # fmt: skip
    for event in enriched:
        if event.kind not in (*LIQUIDATION_KINDS, "comet_buy"):
            continue
        comp = liquidators[event.liquidator]
        comp["count"] += 1
        comp["gross_usd"] += _d(event.gross_profit_usd) if event.kind in PROFIT_KINDS else 0
        comp["chains"].add(event.chain)
        if event.tx_from:
            comp["senders"][event.tx_from] += 1
        if event.lag_blocks is not None and not event.lag_capped and not event.follow_up:
            comp["lags"].append(event.lag_blocks)
        if event.tx_index is not None:
            comp["tx_index"].append(event.tx_index)
        if event.gas_cost_usd is not None:
            comp["gas"].append(float(event.gas_cost_usd))
    total_actions = sum(c["count"] for c in liquidators.values()) or 1
    top = sorted(liquidators.items(), key=lambda kv: kv[1]["count"], reverse=True)[:15]
    top_by_profit = sorted(liquidators.items(), key=lambda kv: kv[1]["gross_usd"], reverse=True)[
        :10
    ]

    # Follow-ups (partial-liquidation series) share the first event's window: no own lag.
    lag_events = [e for e in liquidations if e.lag_blocks is not None and not e.follow_up]
    lag_rows = _lag_buckets(lag_events)
    meaningful = [e for e in lag_events if _d(e.net_profit_usd) >= MEANINGFUL_NET_USD]
    lag_rows_meaningful = _lag_buckets(meaningful)
    lag_seconds_by_chain = {}
    for chain in sorted({e.chain for e in lag_events}):
        block_s = CHAINS[chain].block_time_ms / 1000 if chain in CHAINS else 1.0
        lags = [e.lag_blocks * block_s for e in lag_events
                if e.chain == chain and not e.lag_capped and e.lag_blocks is not None]  # fmt: skip
        lag_seconds_by_chain[chain] = _median(lags)

    with_borrower = [e for e in liquidations if e.borrower]
    online = [e for e in with_borrower if e.bot_online]
    known = [e for e in online if e.borrower_known]
    detected_first = [
        e for e in online if e.our_lead_seconds is not None and e.our_lead_seconds > 0
    ]
    leads = [float(e.our_lead_seconds) for e in detected_first if e.our_lead_seconds is not None]
    pool = [
        e for e in enriched
        if e.kind == "aave_liquidation" and _d(e.net_profit_usd) >= 5
    ]  # fmt: skip
    pool_detected = [e for e in pool if e in detected_first]
    days = max(1e-9, (data.until - data.since).total_seconds() / 86400)

    sighting_reasons = Counter(s.closed_reason or ("open" if not s.closed else "?")
                               for s in data.sightings)  # fmt: skip
    profitable_sightings = [s for s in data.sightings if _d(s.best_net_profit_usd) >= 5]

    return {
        "period": {"since": data.since.isoformat(), "until": data.until.isoformat(),
                   "days": round(days, 2)},
        "events": {"total": len(data.events), "enriched": len(enriched),
                   "pending_or_failed": len(data.events) - len(enriched),
                   "data_gaps": dict(Counter(gap for e in enriched
                                             for gap in (e.data_gaps or "").split(",") if gap))},
        "uptime": {chain: {"minutes": n, "pct": round(100 * n / period_minutes, 1)}
                   for chain, n in sorted(data.uptime_minutes.items())},
        "markets": {k: {**{x: v for x, v in row.items() if x != "gross_values"},
                        "median_gross_usd": _median(row["gross_values"]),
                        "max_gross_usd": max(row["gross_values"], default=None)}
                    for k, row in sorted(per_market.items())},
        "profit_buckets": buckets,
        "liquidators": [
            {"liquidator": addr, "count": c["count"],
             "share_pct": round(100 * c["count"] / total_actions, 1),
             "gross_usd": c["gross_usd"], "median_lag_blocks": _median(c["lags"]),
             "median_tx_index": _median(c["tx_index"]),
             "median_gas_usd": _median(c["gas"]), "chains": sorted(c["chains"]),
             "top_sender": c["senders"].most_common(1)[0][0] if c["senders"] else None,
             "senders": len(c["senders"])}
            for addr, c in top
        ],
        "lag": {"buckets": lag_rows, "buckets_net_ge_5": lag_rows_meaningful,
                "median_seconds_by_chain": lag_seconds_by_chain},
        "liquidators_by_profit": [
            {"liquidator": addr, "gross_usd": c["gross_usd"], "count": c["count"],
             "median_lag_blocks": _median(c["lags"]), "chains": sorted(c["chains"])}
            for addr, c in top_by_profit
        ],
        "detector_coverage": {
            "liquidations_with_borrower": len(with_borrower),
            "while_detector_online": len(online),
            "borrower_known": len(known),
            "detected_before_liquidation": len(detected_first),
            "median_lead_seconds": _median(leads),
        },
        "profit_pool": {
            "aave_liquidations_net_ge_5": len(pool),
            "net_usd": sum((_d(e.net_profit_usd) for e in pool), Decimal(0)),
            "detected_first": len(pool_detected),
            "detected_first_net_usd": sum(
                (_d(e.net_profit_usd) for e in pool_detected), Decimal(0)
            ),
            "projected_30d_net_usd": sum((_d(e.net_profit_usd) for e in pool), Decimal(0))
            * Decimal(30) / Decimal(str(days)),
        },
        "sightings": {"episodes": len(data.sightings), "by_outcome": dict(sighting_reasons),
                      "with_est_net_ge_5": len(profitable_sightings)},
    }  # fmt: skip


def render_markdown(summary: dict[str, Any]) -> str:
    """Human-readable report."""
    s = summary
    out: list[str] = []
    period = s["period"]
    out.append(f"# Aegis market report — last {period['days']} days")
    out.append(f"_{period['since']} → {period['until']}_\n")
    ev = s["events"]
    out.append(
        f"Events: **{ev['total']}** indexed, {ev['enriched']} enriched, "
        f"{ev['pending_or_failed']} pending/failed."
    )
    if ev["data_gaps"]:
        out.append(
            "Data gaps (excluded from the related statistics): "
            + ", ".join(f"{k}: {v}" for k, v in sorted(ev["data_gaps"].items()))
        )
    out.append("")

    out.append("## Detector uptime (watcher)")
    if s["uptime"]:
        out.append("| Chain | Online minutes | Coverage |\n|---|---|---|")
        out += [f"| {c} | {u['minutes']} | {u['pct']}% |" for c, u in s["uptime"].items()]
    else:
        out.append("_No uptime recorded yet._")
    out.append("")

    out.append("## Liquidations by market")
    out.append(
        "| Market | Liquidations | Comet buys | Debt repaid | Liquidators' gross | Gas | Net |"
        " Median gross | Max gross |\n|---|---|---|---|---|---|---|---|---|"
    )
    for name, row in s["markets"].items():
        median = row["median_gross_usd"]
        maximum = row["max_gross_usd"]
        out.append(
            f"| {name} | {row['liquidations']} | {row['buys']} | {_usd(row['debt_usd'])} |"
            f" {_usd(row['gross_usd'])} | {_usd(row['gas_usd'])} | {_usd(row['net_usd'])} |"
            f" {'—' if median is None else f'${median:,.2f}'} |"
            f" {'—' if maximum is None else f'${maximum:,.2f}'} |"
        )
    out.append("")

    out.append("## Net profit per liquidation (Aave + Comet buys)")
    out.append("| Net profit | Count | Sum |\n|---|---|---|")
    out += [f"| {b['bucket']} | {b['count']} | {_usd(b['net_usd'])} |" for b in s["profit_buckets"]]
    out.append("")

    out.append("## Liquidators (most active contracts)")
    out.append(
        "| Liquidator | Actions | Share | Gross | Median lag (blocks) | Median tx index |"
        " Median gas | Chains | EOAs |\n|---|---|---|---|---|---|---|---|---|"
    )
    for c in s["liquidators"]:
        lag = c["median_lag_blocks"]
        idx = c["median_tx_index"]
        gas = c["median_gas_usd"]
        out.append(
            f"| `{c['liquidator']}` | {c['count']} | {c['share_pct']}% | {_usd(c['gross_usd'])} |"
            f" {'—' if lag is None else lag} | {'—' if idx is None else idx} |"
            f" {'—' if gas is None else f'${gas:.4f}'} | {', '.join(c['chains'])} |"
            f" {c['senders']} |"
        )
    out.append("")

    out.append("## Top liquidators by profit")
    out.append(
        "| Liquidator | Gross | Actions | Median lag (blocks) | Chains |\n|---|---|---|---|---|"
    )
    for c in s["liquidators_by_profit"]:
        lag = c["median_lag_blocks"]
        out.append(
            f"| `{c['liquidator']}` | {_usd(c['gross_usd'])} | {c['count']} |"
            f" {'—' if lag is None else lag} | {', '.join(c['chains'])} |"
        )
    out.append("")

    out.append("## Liquidation speed: blocks from 'liquidatable' to 'liquidated'")
    out.append("| Lag | All liquidations | Net ≥ $5 |\n|---|---|---|")
    for every, meaningful in zip(s["lag"]["buckets"], s["lag"]["buckets_net_ge_5"], strict=True):
        out.append(f"| {every['bucket']} | {every['count']} | {meaningful['count']} |")
    medians = s["lag"]["median_seconds_by_chain"]
    if medians:
        out.append(
            "\nMedian lag: "
            + ", ".join(f"{c} {'—' if v is None else f'{v:.1f}s'}" for c, v in medians.items())
        )
    out.append("")

    vis = s["detector_coverage"]
    out.append("## Detector coverage")
    out.append(
        f"- Liquidations with a borrower: **{vis['liquidations_with_borrower']}**\n"
        f"- …while the detector was online: **{vis['while_detector_online']}**\n"
        f"- …of those, borrower already indexed: **{vis['borrower_known']}**"
        f" ({_pct(vis['borrower_known'], vis['while_detector_online'])})\n"
        f"- …detected as liquidatable **before** the liquidation:"
        f" **{vis['detected_before_liquidation']}**"
        f" ({_pct(vis['detected_before_liquidation'], vis['while_detector_online'])})\n"
        f"- Median lead time: "
        f"{'—' if vis['median_lead_seconds'] is None else f'{vis["median_lead_seconds"]:.1f}s'}"
    )
    out.append("")

    add = s["profit_pool"]
    out.append("## Profit pool (Aave, net ≥ $5)")
    out.append(
        f"- Liquidations: **{add['aave_liquidations_net_ge_5']}**, net {_usd(add['net_usd'])}"
        f" → projected **{_usd(add['projected_30d_net_usd'])} / 30 days** (all liquidators)\n"
        f"- Detected before the liquidation: **{add['detected_first']}**,"
        f" net {_usd(add['detected_first_net_usd'])}"
    )
    out.append("")

    sig = s["sightings"]
    out.append("## Detected liquidatable positions (sightings)")
    out.append(
        f"- Episodes: **{sig['episodes']}** · outcomes: "
        + (", ".join(f"{k}: {v}" for k, v in sig["by_outcome"].items()) or "—")
        + f"\n- With estimated net ≥ $5: **{sig['with_est_net_ge_5']}**"
    )
    return "\n".join(out) + "\n"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(type(value).__name__)


def write_csv(rows: Sequence[Any], columns: Sequence[str]) -> None:
    """Dump ORM rows as CSV to stdout."""
    writer = csv.writer(sys.stdout)
    writer.writerow(columns)
    for row in rows:
        values = []
        for column in columns:
            value = getattr(row, column)
            values.append(value.isoformat() if isinstance(value, datetime) else value)
        writer.writerow(values)


async def run(days: int, fmt: str, table: str) -> None:
    """Load data and print the requested output."""
    data = await load(days)
    if fmt == "csv":
        if table == "sightings":
            write_csv(data.sightings, SIGHTING_COLUMNS)
        else:
            write_csv(data.events, EVENT_COLUMNS)
        return
    summary = summarize(data)
    if fmt == "json":
        print(json.dumps(summary, default=_jsonable, indent=2))
    else:
        print(render_markdown(summary), end="")


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Aegis liquidation market report")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--format", choices=["md", "json", "csv"], default="md")
    parser.add_argument("--table", choices=["events", "sightings"], default="events")
    args = parser.parse_args()
    asyncio.run(run(args.days, args.format, args.table))


if __name__ == "__main__":
    main()
