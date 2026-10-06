"""llm_calls and analyses: trace ids, attempt kind and the messages of every LLM call

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("trace_id", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("attempt", sa.SmallInteger(), nullable=True))
    op.add_column("llm_calls", sa.Column("kind", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("trace_id", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("span_id", sa.Text(), nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("llm_calls", sa.Column("finish_reason", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("provider_response_id", sa.Text(), nullable=True))
    op.create_index(op.f("ix_llm_calls_trace_id"), "llm_calls", ["trace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_calls_trace_id"), table_name="llm_calls")
    for column in (
        "provider_response_id",
        "finish_reason",
        "messages",
        "span_id",
        "trace_id",
        "kind",
        "attempt",
    ):
        op.drop_column("llm_calls", column)
    op.drop_column("analyses", "trace_id")
