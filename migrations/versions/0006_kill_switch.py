"""kill_switch: the single stored kill-switch state

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kill_switch",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_code_sha256", sa.Text(), nullable=True),
        sa.Column("reset_code_expires", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_kill_switch_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kill_switch")),
    )


def downgrade() -> None:
    op.drop_table("kill_switch")
