"""IBKR only: simulator-era rows leave the agent_paper book

Brackets without any order IBKR acknowledged (no broker order id, no perm id) were filled by
the simulator. They, the equity history and the derived trades move to book `sim_archive`, so
the agent never reconciles, manages or sells them at IBKR. Nothing is deleted; the rows of the
removed comparison books (baseline_sim, agent_shadow) stay but are no longer read.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE brackets SET book = 'sim_archive'
        WHERE book = 'agent_paper'
          AND NOT EXISTS (
            SELECT 1 FROM orders o
            WHERE o.bracket_id = brackets.id
              AND (o.broker_order_id IS NOT NULL OR o.perm_id IS NOT NULL)
          )
        """
    )
    op.execute("UPDATE equity_daily SET book = 'sim_archive' WHERE book = 'agent_paper'")
    op.execute("UPDATE trades SET book = 'sim_archive' WHERE book = 'agent_paper'")


def downgrade() -> None:
    for table in ("brackets", "equity_daily", "trades"):
        op.execute(f"UPDATE {table} SET book = 'agent_paper' WHERE book = 'sim_archive'")  # noqa: S608
