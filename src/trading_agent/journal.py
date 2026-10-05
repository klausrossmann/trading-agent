"""Journal in Telegram (IMPLEMENTATION.md 12.2): /proposals, /why, /review, evening digest."""

from collections import Counter
from datetime import date, datetime
from uuid import UUID

import structlog

from trading_agent import jobs
from trading_agent.data.ingest import Sessions
from trading_agent.db import book as book_repo
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as repo
from trading_agent.db import trades as trades_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.market import BERLIN
from trading_agent.domain.proposals import Label, Proposal
from trading_agent.domain.trading import Book
from trading_agent.notify import messages
from trading_agent.notify.telegram import Command, Interaction, Notifier, Reply

log = structlog.get_logger(__name__)

LABELS: dict[str, Label] = {"a": "agree", "d": "disagree"}
BOOKS: tuple[Book, ...] = ("agent_paper", "agent_shadow", "baseline_sim")


async def latest(sessions: Sessions) -> tuple[date | None, list[Proposal], dict[UUID, Label]]:
    """The latest scan day's proposals and their labels."""
    async with sessions() as s:
        day = await repo.latest_as_of(s)
        if day is None:
            return None, [], {}
        items = await repo.proposals(s, start=day)
        labels = await repo.labels(s, [p.id for p in items])
    return day, items, {k: v[0] for k, v in labels.items()}


class Review:
    """Steps through the latest unlabelled proposals; one optional reason per label."""

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions
        self.skipped: set[UUID] = set()
        self.awaiting: Proposal | None = None

    async def card(self, prefix: str = "") -> Reply:
        _, items, labels = await latest(self.sessions)
        todo = [p for p in items if p.id not in labels and p.id not in self.skipped]
        if not todo:
            return Reply(f"{prefix}Nothing left to label.")
        p = todo[0]
        buttons = (
            (("👍 Agree", f"l:{p.id}:a"), ("👎 Disagree", f"l:{p.id}:d")),
            (("Skip", f"s:{p.id}"),),
        )
        return Reply(f"{prefix}{len(todo)} to label\n\n{messages.render_why(p)}", buttons)

    async def start(self, _: list[str]) -> Reply:
        self.skipped.clear()
        return await self.card()

    async def on_button(self, data: str) -> Reply:
        kind, _, rest = data.partition(":")
        raw_id, _, code = rest.partition(":")
        try:
            proposal_id = UUID(raw_id)
        except ValueError:
            return Reply("Unknown button.")
        if kind == "s":
            self.skipped.add(proposal_id)
            return await self.card()
        if kind != "l" or code not in LABELS:
            return Reply("Unknown button.")
        async with self.sessions.begin() as s:
            p = await repo.get(s, proposal_id)
            if p is None:
                return Reply("That proposal no longer exists.")
            await repo.set_label(s, proposal_id, LABELS[code])
        self.awaiting = p
        log.info("review.labelled", symbol=p.yahoo_symbol, label=LABELS[code])
        prefix = f"Saved {p.yahoo_symbol}: {LABELS[code]}. Reply with a reason if you like.\n\n"
        return await self.card(prefix)

    async def on_text(self, text: str) -> str | None:
        p = self.awaiting
        if p is None:
            return None
        async with self.sessions.begin() as s:
            await repo.set_reason(s, p.id, text.strip()[:500])
        self.awaiting = None
        return f"Reason saved for {p.yahoo_symbol}."

    def interaction(self) -> Interaction:
        return Interaction(self.on_button, self.on_text)


def commands(sessions: Sessions, review: Review) -> dict[str, Command]:
    async def proposals(_: list[str]) -> str:
        day, items, labels = await latest(sessions)
        if day is None:
            return "No proposals yet."
        title = f"Proposals, as of {messages.day_label(day)}"
        return messages.render_proposals(title, items, labels)

    async def why(args: list[str]) -> str:
        if not args:
            return "Usage: /why SYMBOL, e.g. /why SAP.DE"
        async with sessions() as s:
            p = await repo.latest_for_symbol(s, args[0])
            label = (await repo.labels(s, [p.id])).get(p.id) if p else None
        if p is None:
            return f"No proposal for {args[0].upper()}."
        return messages.render_why(p, label)

    return {
        "proposals": Command("the latest agent proposals", proposals),
        "why": Command("thesis, critique and plan for SYMBOL", why),
        "review": Command("label the latest proposals agree/disagree", review.start),
    }


async def build_digest(book: jobs.BookContext, today: date) -> messages.Digest:
    sessions = book.sessions
    as_of, items, labels = await latest(sessions)
    async with sessions() as s:
        books = {name: await trades_repo.book_trades(s, name) for name in BOOKS}
        # By id, active or not: a trade may outlive its instrument's universe membership.
        instruments = await market_repo.instruments(
            s, {r.instrument_id for rows in books.values() for r in rows}
        )
        decided = await book_repo.decisions(s, [p.id for p in items])
    lines: list[messages.BookDay] = []
    for name, rows in books.items():
        lines.append(
            messages.BookDay(
                book=name,
                entered=[
                    instruments[r.instrument_id].yahoo_symbol for r in rows if r.entry_date == today
                ],
                closed=[
                    messages.ClosedLine(
                        symbol=instruments[r.instrument_id].yahoo_symbol,
                        reason=r.exit_reason or "",
                        pnl_eur=float(r.pnl_net_eur or 0),
                        budget_eur=float(
                            book.risk.budget_for(instruments[r.instrument_id].market, "paper")
                        ),
                    )
                    for r in rows
                    if r.exit_date == today
                ],
                open=sum(1 for r in rows if r.exit_date is None),
            )
        )
    start = datetime.combine(today, datetime.min.time(), BERLIN)
    symbols = {p.id: p.yahoo_symbol for p in items}
    return messages.Digest(
        day=today,
        proposals_as_of=as_of,
        statuses=dict(Counter(p.status for p in items)),
        to_label=sum(1 for p in items if p.id not in labels),
        books=lines,
        llm_usd_today=await DbAnalysisStore(sessions).spent_usd(start),
        placed=[symbols[k] for k, d in decided.items() if d.approved],
        rejected=[
            (symbols[k], f"{d.failures[0].name}: {d.failures[0].detail}")
            for k, d in decided.items()
            if not d.approved and d.failures
        ],
    )


async def evening_digest(
    book: jobs.BookContext, notifier: Notifier, today: date | None = None
) -> str:
    """After the US close: replay agent_shadow, then send the day's digest."""
    today = today or datetime.now(BERLIN).date()
    await jobs.agent_book(book, today)
    text = messages.render_digest(await build_digest(book, today))
    await notifier.send(text)
    return text
