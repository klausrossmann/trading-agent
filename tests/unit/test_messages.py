from datetime import UTC, date, datetime, timedelta

from trading_agent.notify import messages as m


def test_money_shows_euros_and_percent() -> None:
    assert m.money(12.3, 1000) == "+€12.30 (+1.2 %)"
    assert m.money(-15.2, 1000) == "−€15.20 (−1.5 %)"
    assert m.money(0, 1000) == "€0.00 (0.0 %)"
    assert m.pct(-0.25) == "−0.2 %"


def _briefing(**kw: object) -> m.Briefing:
    fields: dict[str, object] = {
        "day": date(2026, 10, 5),
        "markets": [m.MarketLine("SPY", date(2026, 10, 2), 612.4, 0.42, {"daily": "up"})],
        "eur_usd": 1.1355,
        "earnings": [m.EarningsLine("AAPL", date(2026, 10, 7), "amc")],
        "book": [
            m.SleeveLine(
                "US", 1000, 3, 12.3, [m.PositionLine("MSFT", date(2026, 10, 1), 0.6, 5.1)]
            ),
            m.SleeveLine("EU", 5000, 0, 0.0),
        ],
        "book_start": date(2026, 10, 5),
        "setups": ["NVDA", "SAP.DE"],
        "last_bars": {"US": date(2026, 10, 2), "EU": date(2026, 10, 2)},
        "blocked": [],
    }
    return m.Briefing(**(fields | kw))  # pyright: ignore[reportArgumentType]


def test_briefing_text() -> None:
    text = m.render_briefing(_briefing())
    assert text.startswith("📰 Briefing Mon 5 Oct")
    for expected in (
        "SPY 612.40 +0.4 % (daily up)",
        "EUR/USD 1.1355",
        "AAPL Wed 7 Oct amc",
        "US: 1 open, 3 closed, P&L +€12.30 (+1.2 %)",
        "MSFT since Thu 1 Oct: +0.60 R, +€5.10 (+0.5 %)",
        "EU: 0 open, 0 closed",
        "NVDA, SAP.DE",
        "last bars US 02 Oct, EU 02 Oct; quality 0 blocked",
    ):
        assert expected in text


def test_briefing_before_book_start_and_without_data() -> None:
    text = m.render_briefing(
        _briefing(book=None, earnings=[], setups=[], blocked=None, eur_usd=None)
    )
    assert "starts 2026-10-05" in text
    assert "none in the universe" in text
    assert "quality not checked yet" in text
    assert "EUR/USD" not in text


def test_status_text() -> None:
    now = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    text = m.render_status(
        m.StatusSnapshot(
            mode="paper",
            started_at=now - timedelta(hours=2, minutes=5),
            now=now,
            heartbeat_at=now - timedelta(minutes=3),
            heartbeat_ok=True,
            last_bars={"US": date(2026, 10, 2), "EU": None},
            blocked={"EU": ["EXS1.DE"]},
            next_jobs=[("ingest_eod_eu:2026-10-05", now + timedelta(hours=6))],
        )
    )
    assert "Agent running (paper), up 2 h 5 min" in text
    assert "Heartbeat: ok, 3 min ago" in text
    assert "last bars US 02 Oct, EU -" in text
    assert "EU: EXS1.DE" in text
    assert "ingest_eod_eu:2026-10-05 Mon 05 Oct 16:00" in text


def test_alerts() -> None:
    assert m.data_alert("prices_us", {}) is None
    failed = {f"S{i}": "HTTP 500" for i in range(12)}
    text = m.data_alert("prices_us", failed, ["AAPL"])
    assert text is not None
    assert "failed for 12: S0 (HTTP 500)" in text
    assert "and 2 more" in text
    assert "restated (split?): AAPL" in text
    assert m.quality_alert("EU", {}) is None
    assert m.quality_alert("EU", {"X.DE": ["stale", "stale"]}) == (
        "⚠️ Data quality EU: excluded from today's scan: X.DE (stale)"
    )
    assert m.job_alert("ingest_eod_us:2026-10-05", "HTTP 503").endswith("failed: HTTP 503")
