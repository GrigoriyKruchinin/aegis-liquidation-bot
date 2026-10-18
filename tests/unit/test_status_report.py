from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aegis.core.models import ActivityStats, MarketRef, MarketStats, MarketStatus, StatusReport
from aegis.notifier.templates import render_status
from aegis.notifier.templates.formatting import compact_usd, duration
from aegis.reporting.status import StatusReporter
from tests.factories import MARKET
from tests.fakes import FakeProvider, MemoryCheckpointRepository
from tests.unit.test_scanner import StubProtocol


def test_compact_usd() -> None:
    assert compact_usd(Decimal("950.4")) == "$950"
    assert compact_usd(Decimal(12_340)) == "$12.3K"
    assert compact_usd(Decimal(4_560_000)) == "$4.6M"
    assert compact_usd(Decimal(1_200_000_000)) == "$1.2B"


def test_duration() -> None:
    assert duration(45) == "45s"
    assert duration(12 * 60) == "12m"
    assert duration(3 * 3600 + 20 * 60) == "3h 20m"
    assert duration(2 * 86400 + 4 * 3600) == "2d 4h"
    assert duration(3600) == "1h"


def _report(lag: int | None, activity: ActivityStats | None = None) -> StatusReport:
    stats = MarketStats(1234, Decimal(45_600_000), 3, Decimal("1.0123"))
    return StatusReport(
        generated_at=datetime(2026, 10, 18, tzinfo=UTC),
        period_hours=6,
        markets=(
            MarketStatus("base", "Aave v3", stats, lag),
            MarketStatus("base", "cUSDCv3", MarketStats(0, Decimal(0), 0, None), None),
            MarketStatus("optimism", "<b>evil</b>", stats, 10),
        ),
        activity=activity or ActivityStats(0, None, 0, 0),
        last_scan_age_seconds=4,
    )


def test_render_status_contents() -> None:
    text = render_status(_report(lag=30))
    assert "📊 <b>Aegis status</b> · last 6h" in text
    assert "🌐 <b>Base</b>" in text and "🌐 <b>Optimism</b>" in text
    assert "1234 borrowers · debt $45.6M · ⚠️ 3 at risk · min HF <code>1.012</code>" in text
    assert "✅ synced" in text
    assert "⏳ not indexed yet" in text
    assert "💓 <b>Last scan:</b> 4s ago" in text
    assert "&lt;b&gt;evil&lt;/b&gt;" in text and "<b>evil</b>" not in text
    assert len(text) < 4096


def test_render_status_indexing_and_activity() -> None:
    text = render_status(
        _report(
            lag=2 * 3600 + 900,
            activity=ActivityStats(2, Decimal(42), 1, 1, 7, Decimal("123.4"), 3),
        )
    )
    assert (
        "🏁 <b>Market liquidations:</b> 7 · liquidators earned $123.40 · seen by us first: 3"
        in text
    )
    assert "⏳ indexing, ~2h 15m behind" in text
    assert "🔎 <b>Opportunities:</b> 2 (best $42.00)" in text
    assert "📨 <b>Alerts sent:</b> 1 · ❌ failed: 1" in text


class FakeStats:
    def __init__(self, stats: dict[str, MarketStats]) -> None:
        self.stats = stats
        self.since: datetime | None = None

    async def market_stats(self, hf_threshold: Decimal) -> dict[str, MarketStats]:
        return self.stats

    async def activity_since(self, since: datetime) -> ActivityStats:
        self.since = since
        return ActivityStats(1, Decimal(7), 1, 0)


async def test_reporter_builds_rows_with_index_lag() -> None:
    provider = FakeProvider()  # Base, 2s blocks
    provider.head = 10_000
    checkpoints = MemoryCheckpointRepository()
    await checkpoints.set(MARKET, 9_000)  # 1000 blocks behind -> 2000s
    stats = FakeStats({MARKET.key: MarketStats(5, Decimal(100), 1, Decimal("1.01"))})

    async def scan_age() -> float | None:
        return 3.7

    other = MarketRef("base", "compound_v3", "0x" + "99" * 20, "cUSDCv3")
    protocol_b = StubProtocol(provider)
    protocol_b._market = other
    reporter = StatusReporter(
        {"base": [StubProtocol(provider), protocol_b]},
        stats,
        checkpoints,
        scan_age,
        hf_threshold=Decimal("1.05"),
    )
    report = await reporter.build(6)
    first, second = report.markets
    assert first.index_lag_seconds == 2000 and first.stats.active_borrowers == 5
    assert second.index_lag_seconds is None and second.stats.active_borrowers == 0
    assert report.last_scan_age_seconds == 3
    assert report.activity.opportunities == 1
    assert stats.since is not None
    assert (report.generated_at - stats.since).total_seconds() == 6 * 3600
