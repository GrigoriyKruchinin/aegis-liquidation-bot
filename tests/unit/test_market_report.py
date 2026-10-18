from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis.db.models import LiquidationEventRow, SightingRow
from aegis.tools.market_report import Data, _jsonable, render_markdown, summarize

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def _event(**overrides: object) -> LiquidationEventRow:
    values: dict[str, object] = {
        "chain": "base", "protocol": "aave_v3", "market": "0x" + "11" * 20,
        "market_name": "Aave v3", "kind": "aave_liquidation", "tx_hash": "0x" + "aa" * 32,
        "log_index": 0, "block_number": 1, "borrower": "0x" + "b0" * 20,
        "liquidator": "0x" + "c1" * 20, "tx_from": "0x" + "e1" * 20, "tx_index": 1,
        "debt_amount": Decimal(1), "collateral_amount": Decimal(1), "enriched": True,
        "block_time": NOW - timedelta(hours=1), "debt_usd": Decimal(1000),
        "collateral_usd": Decimal(1050), "gross_profit_usd": Decimal(50),
        "gas_cost_usd": Decimal("0.05"), "net_profit_usd": Decimal("49.95"), "lag_blocks": 0,
        "lag_capped": False, "bot_online": True, "borrower_known": True,
        "our_lead_seconds": None,
    }  # fmt: skip
    values.update(overrides)
    return LiquidationEventRow(**values)


def _data() -> Data:
    events = [
        _event(),
        _event(log_index=1, lag_blocks=3, our_lead_seconds=Decimal(4)),
        _event(log_index=2, liquidator="0x" + "c2" * 20, gross_profit_usd=Decimal(2),
               net_profit_usd=Decimal("1.9"), lag_blocks=None),
        _event(log_index=3, kind="comet_absorb", market_name="cUSDCv3", gross_profit_usd=0,
               net_profit_usd=Decimal("-0.01"), lag_blocks=40, lag_capped=True),
        _event(log_index=4, enriched=False),
    ]  # fmt: skip
    sightings = [
        SightingRow(
            chain="base", protocol="aave_v3", market="0x", market_name="Aave v3",
            borrower="0x" + "b0" * 20, first_seen_at=NOW - timedelta(hours=2),
            last_seen_at=NOW, observations=3, max_debt_usd=Decimal(1000),
            best_net_profit_usd=Decimal(49), closed=True, closed_reason="liquidated",
        )
    ]  # fmt: skip
    return Data(NOW - timedelta(days=7), NOW, events, sightings, {"base": 5040})


def test_summarize_figures() -> None:
    summary = summarize(_data())
    assert summary["events"] == {
        "total": 5,
        "enriched": 4,
        "pending_or_failed": 1,
        "data_gaps": {},
    }
    assert summary["uptime"]["base"]["pct"] == 50.0
    aave = summary["markets"]["base · Aave v3"]
    assert aave["liquidations"] == 3 and aave["gross_usd"] == Decimal(102)
    top = summary["competitors"][0]
    assert top["liquidator"] == "0x" + "c1" * 20 and top["count"] == 3
    lags = {b["bucket"]: b["count"] for b in summary["lag"]["buckets"]}
    assert lags["same block (0)"] == 1 and lags["2-5 blocks"] == 1
    assert lags["already liquidatable at window start (capped)"] == 1
    visibility = summary["our_visibility"]
    assert visibility["seen_before_liquidation"] == 1 and visibility["median_lead_seconds"] == 4
    addressable = summary["addressable"]
    assert addressable["aave_liquidations_net_ge_5"] == 2
    assert addressable["seen_first_by_us"] == 1
    assert addressable["projected_30d_net_usd"] > addressable["net_usd"]
    assert summary["sightings"]["by_outcome"] == {"liquidated": 1}


def test_markdown_and_json_render() -> None:
    summary = summarize(_data())
    text = render_markdown(summary)
    assert "# Aegis market report" in text and "## Competitors" in text
    assert "same block (0)" in text and "Seen first by us" in text
    assert json.loads(json.dumps(summary, default=_jsonable))["events"]["total"] == 5


def test_empty_report() -> None:
    summary = summarize(Data(NOW - timedelta(days=1), NOW, [], [], {}))
    assert "No uptime recorded yet" in render_markdown(summary)
