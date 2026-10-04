"""risk_decisions and equity_daily: the risk engine's results and daily sleeve equity

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=12, scale=2)


def upgrade() -> None:
    op.create_table(
        "risk_decisions",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("trip", sa.Text(), nullable=True),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], name=op.f("fk_risk_decisions_proposal_id_proposals")
        ),
        sa.PrimaryKeyConstraint("proposal_id", name=op.f("pk_risk_decisions")),
    )
    op.create_table(
        "equity_daily",
        sa.Column("book", sa.Text(), nullable=False),
        sa.Column("sleeve", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity_eur", MONEY, nullable=False),
        sa.Column("cash_eur", MONEY, nullable=False),
        sa.Column("invested_eur", MONEY, nullable=False),
        sa.PrimaryKeyConstraint("book", "sleeve", "date", name=op.f("pk_equity_daily")),
    )


def downgrade() -> None:
    op.drop_table("equity_daily")
    op.drop_table("risk_decisions")
