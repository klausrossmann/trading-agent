"""Tax report (IMPLEMENTATION.md 14; orchestration)."""

from datetime import date

from trading_agent import jobs, trading
from trading_agent.db import reports as reports_repo
from trading_agent.domain.trading import Book
from trading_agent.portfolio import tax


async def tax_report(ctx: jobs.BookContext, year: int, book: Book = "agent_live") -> str:
    """The yearly tax helper for one book, stored as report `tax:<book>` at 31 Dec."""
    brackets, fills, instruments = await trading.load_book(ctx.sessions, book, ctx.fees)
    body = tax.render(year, book, tax.sales([b for b, _ in brackets], fills, instruments, year))
    async with ctx.sessions.begin() as s:
        await reports_repo.save_report(s, f"tax:{book}", date(year, 12, 31), body)
    return body
