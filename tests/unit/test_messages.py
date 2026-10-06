from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import KillSwitch
from trading_agent.notify import messages as m


def test_money_shows_euros_and_percent() -> None:
    assert m.money(12.3, 1000) == "+€12.30 (+1.2 %)"
    assert m.money(-15.2, 1000) == "−€15.20 (−1.5 %)"
    assert m.money(0, 1000) == "€0.00 (0.0 %)"
    assert m.pct(-0.25) == "−0.2 %"


def test_shares_drop_trailing_zeros() -> None:
    assert [m.shares(Decimal(q)) for q in ("2", "2.0000", "0.5000", "12.3456", "100")] == [
        "2",
        "2",
        "0.5",
        "12.3456",
        "100",
    ]
    assert m.shares(3.3) == "3.3"
    assert m.fill_alert("AAA", Decimal("1.5000"), 100.05) == "🟢 Bought AAA 1.5 @ 100.05"
    assert (
        m.placement_summary(
            "US", [m.PlacedLine("GOOG", Decimal("0.4321"), 300.0, 290.0, 320.0)], [], "simulator"
        ).splitlines()[1]
        == "  GOOG 0.4321 @ 300.00, stop 290.00, target 320.00"
    )


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
    assert "ingest_eod_eu:2026-10-05 Mon 05 Oct 18:00" in text  # Berlin time
    assert "Kill switch: unknown" in text
    assert "LLM tracing: off" in text
    assert "Live interlock" not in text


def test_kill_switch_texts() -> None:
    since = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    until = datetime(2026, 10, 7, 22, 0, tzinfo=UTC)
    assert m.kill_switch_line(KillSwitch()) == "active"
    paused = KillSwitch(state="paused", reason="daily loss limit", since=since, until=until)
    assert m.kill_switch_line(paused) == "paused (daily loss limit) until Thu 08 Oct 00:00"
    halted = KillSwitch(state="halted", reason="/stop", since=since)
    assert m.kill_switch_line(halted) == "halted (/stop) since Wed 07 Oct 16:00"
    assert m.kill_switch_line(halted.model_copy(update={"since": None})) == "halted (/stop)"
    alert = m.kill_switch_alert(halted)
    assert alert.startswith("⛔ Halted: /stop. No new entries; protective stops stay.")
    assert "trading-agent reset" in alert
    status = m.render_status(
        m.StatusSnapshot(
            mode="live",
            started_at=since,
            now=since,
            heartbeat_at=None,
            heartbeat_ok=None,
            last_bars={},
            blocked={},
            next_jobs=[],
            kill_switch=m.kill_switch_line(paused),
            interlock="waiting for /confirm_live CODE",
            tracing="on (cloud.langfuse.com, live)",
        )
    )
    assert "Kill switch: paused (daily loss limit)" in status
    assert "Live interlock: waiting for /confirm_live CODE" in status
    assert "LLM tracing: on (cloud.langfuse.com, live)" in status


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


def test_budget_message() -> None:
    text = m.render_budget(
        m.BudgetSnapshot(
            month=date(2026, 10, 1),
            spent_usd=4.0,
            monthly_usd=16.0,
            mode="normal",
            by_model=[("google:gemini-3.8-flash", 120, 3.5), ("google:x", 3, 0.5)],
        )
    )
    assert text.splitlines()[0] == "💸 LLM budget Oct 2026: $4.00 of $16.00 (25 %), mode normal"
    assert "  google:gemini-3.8-flash: 120 calls, $3.5000" in text
    empty = m.render_budget(m.BudgetSnapshot(date(2026, 10, 1), 0.0, 16.0, "normal", []))
    assert "no calls this month" in empty


def _proposal(symbol: str, status: str, rank: int | None = None, **kw: object) -> Proposal:
    values: dict[str, object] = {
        "id": uuid4(),
        "source": "agent",
        "as_of": date(2026, 10, 2),
        "instrument_id": 1,
        "yahoo_symbol": symbol,
        "market": "US",
        "sector": None,
        "status": status,
        "strategy": "pullback_uptrend",
        "rank": rank,
        "thesis": "Pullback in an uptrend.",
        "invalidation": "Close below the stop.",
    }
    if status != "no_trade":
        values |= {"entry": 100.0, "stop": 96.0, "target": 108.0, "confidence": 0.35}
        values |= {"entry_ref": "close", "stop_ref": "atr_stop_2x", "target_ref": "atr_target_4x"}
    return Proposal.model_validate(values | kw)


def test_render_proposals_lists_ranked_blocked_and_passed() -> None:
    a = _proposal("AAA", "proposed", 2, critic_severity="minor", critic_summary="ok")
    b = _proposal("BBB", "proposed", 1)
    c = _proposal("CCC", "blocked", critic_severity="blocking", critic_summary="no")
    d = _proposal("DDD", "no_trade")
    text = m.render_proposals("Proposals US", [a, b, c, d], {a.id: "agree"}, 2, 0.0123)
    assert text.splitlines() == [
        "🧠 Proposals US (LLM $0.0123)",
        "1. BBB 100.00, stop 96.00, target 108.00, R:R 2.0, conf 0.35, no critique",
        "2. AAA 100.00, stop 96.00, target 108.00, R:R 2.0, conf 0.35, critic minor 👍",
        "Blocked by the critic: CCC",
        "Passed: DDD",
        "Not sent to the proposer: 2 (technical rating, held, failed)",
        "/why SYMBOL for details, /review to label them.",
    ]
    assert m.render_proposals("P", []).splitlines() == ["🧠 P", "No trade proposed."]


def test_render_why_shows_plan_critique_and_label() -> None:
    p = _proposal(
        "AAA",
        "proposed",
        1,
        critic_severity="major",
        critic_summary="Weak target.",
        payload={
            "critic": {"objections": [{"severity": "major", "point": "Resistance first."}]},
            "portfolio_manager": {"note": "Only energy name.", "rationale": "r"},
        },
    )
    text = m.render_why(p, ("disagree", "Extended."))
    assert "Plan: buy limit 100.00, stop 96.00, target 108.00, R:R 2.0 (close / atr_stop_2x" in text
    assert "Critic (major): Weak target.\n  - major: Resistance first." in text
    assert "Ranking: Only energy name." in text
    assert text.endswith("Your label: disagree (Extended.)")
    passed = _proposal("DDD", "no_trade", payload={"proposer": {"no_trade_reason": "Fading."}})
    assert "passed" in m.render_why(passed).splitlines()[0]
    assert "Why not: Fading." in m.render_why(passed)
