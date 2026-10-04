"""proposals and user_labels: agent decisions and your agree/disagree labels

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    now = sa.text("now()")
    empty = sa.text("'{}'::jsonb")
    op.create_table(
        "proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("entry_ref", sa.Text(), nullable=True),
        sa.Column("stop_ref", sa.Text(), nullable=True),
        sa.Column("target_ref", sa.Text(), nullable=True),
        sa.Column("entry", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("stop", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("target", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("invalidation", sa.Text(), nullable=False),
        sa.Column("critic_severity", sa.Text(), nullable=True),
        sa.Column("critic_summary", sa.Text(), nullable=True),
        sa.Column(
            "analyses",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=empty,
            nullable=False,
        ),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=empty, nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_proposals_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
        sa.UniqueConstraint("source", "instrument_id", "as_of", name="uq_proposals_source_day"),
    )
    op.create_index(op.f("ix_proposals_as_of"), "proposals", ["as_of"], unique=False)
    op.create_table(
        "user_labels",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name=op.f("fk_user_labels_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("proposal_id", name=op.f("pk_user_labels")),
    )


def downgrade() -> None:
    op.drop_table("user_labels")
    op.drop_index(op.f("ix_proposals_as_of"), table_name="proposals")
    op.drop_table("proposals")
