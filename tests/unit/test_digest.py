from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis.core.models import LiquidationRecord
from aegis.notifier.templates.market import render_digest, render_large_liquidation
from aegis.reporting.digest import PAID_KINDS, MarketDigestReporter, summarize_market

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
WHALE = "0x" + "a1" * 20
MINNOW = "0x" + "b2" * 20


def _record(**overrides: object) -> LiquidationRecord:
    values: dict[str, object] = {
        "id": 1, "chain": "base", "market_name": "Aave v3", "kind": "aave_liquidation",
        "tx_hash": "0x" + "ab" * 32, "block_number": 100, "block_time": NOW - timedelta(hours=2),
        "liquidator": WHALE, "tx_index": 3, "debt_symbol": "USDC", "collateral_symbol": "WETH",
        "debt_usd": Decimal(10_000), "gross_profit_usd": Decimal(450),
        "gas_cost_usd": Decimal("0.5"), "net_profit_usd": Decimal("449.5"), "lag_blocks": 0,
        "lag_capped": False, "follow_up": False,
    }  # fmt: skip
    values.update(overrides)
    return LiquidationRecord(**values)  # type: ignore[arg-type]


RECORDS = [
    _record(),
    _record(id=2, chain="arbitrum", net_profit_usd=Decimal(20), debt_usd=Decimal(500),
            lag_blocks=3),
    _record(id=3, liquidator=MINNOW, net_profit_usd=Decimal("0.4"), lag_blocks=None),
    _record(id=4, net_profit_usd=Decimal(60), follow_up=True, lag_blocks=0),
    _record(id=6, net_profit_usd=None, gross_profit_usd=None, lag_blocks=0),
]  # fmt: skip


def test_summarize_market() -> None:
    digest = summarize_market(
        RECORDS, since=NOW - timedelta(days=1), until=NOW, pending=7, threshold_usd=Decimal(5)
    )
    chains = {c.chain: c for c in digest.chains}
    assert chains["base"].liquidations == 4
    assert chains["base"].net_profit_usd == Decimal("509.9")
    assert chains["arbitrum"].volume_usd == Decimal(500)
    assert digest.meaningful == 3 and digest.meaningful_net_usd == Decimal("529.5")
    # The follow-up shares its window with the first liquidation: no own lag.
    assert (digest.same_block, digest.lag_measured) == (1, 2)
    assert [r.id for r in digest.top_liquidations] == [1, 4, 2]
    top = digest.top_liquidators[0]
    assert top.liquidator == WHALE and top.liquidations == 3
    assert top.chains == ("arbitrum", "base")
    assert digest.pending == 7 and digest.unpriced == 1


def test_unpriced_never_meaningful_even_at_zero_threshold() -> None:
    digest = summarize_market(
        RECORDS, since=NOW - timedelta(days=1), until=NOW, pending=0, threshold_usd=Decimal(0)
    )
    assert 6 not in [r.id for r in digest.top_liquidations] and digest.meaningful == 4


def test_render_digest_escapes_and_links() -> None:
    digest = summarize_market(
        [*RECORDS, _record(id=5, market_name="<b>evil</b>")],
        since=NOW - timedelta(days=1),
        until=NOW,
        pending=0,
        threshold_usd=Decimal(5),
    )
    text = render_digest(digest)
    assert "📈 <b>Liquidation market · last 24h</b>" in text
    assert "basescan.org/tx/0x" + "ab" * 32 in text
    assert "&lt;b&gt;evil&lt;/b&gt;" in text and "<b>evil</b>" not in text
    assert "Same-block liquidations:</b> 2/3" in text
    assert "All indexed events are enriched." in text
    assert "Excluded (could not be priced): 1." in text


def test_render_empty_digest() -> None:
    digest = summarize_market(
        [], since=NOW - timedelta(days=1), until=NOW, pending=3, threshold_usd=Decimal(5)
    )
    text = render_digest(digest)
    assert "No paid liquidations" in text and "3 events are still being enriched" in text
    assert "Same-block" not in text and "Largest" not in text


def test_render_large_liquidation() -> None:
    text = render_large_liquidation(_record())
    assert "🐋 <b>Large liquidation</b>" in text
    assert "$449.50" in text and "same block as it became liquidatable" in text
    assert f"<code>{WHALE}</code>" in text
    later = render_large_liquidation(replace(_record(), lag_blocks=4, tx_index=None))
    assert "4 blocks after" in later and "tx #— in block" in later


class _Events:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime, datetime, tuple[str, ...]]] = []

    async def enriched_between(
        self, since: datetime, until: datetime, kinds: Sequence[str]
    ) -> list[LiquidationRecord]:
        self.calls.append((since, until, tuple(kinds)))
        return RECORDS

    async def pending_count(self) -> int:
        return 0


async def test_reporter_queries_last_24h_of_paid_kinds() -> None:
    events = _Events()
    reporter = MarketDigestReporter(events, threshold_usd=Decimal(5))  # type: ignore[arg-type]
    digest = await reporter.build(now=NOW)
    assert events.calls == [(NOW - timedelta(hours=24), NOW, PAID_KINDS)]
    assert digest.meaningful == 3
