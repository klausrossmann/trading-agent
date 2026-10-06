"""Share quantities become fractional: Integer to Numeric(14, 4)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("trades", "quantity", False),
    ("brackets", "quantity", False),
    ("brackets", "filled_qty", True),
    ("brackets", "exit_qty", True),
    ("orders", "quantity", False),
    ("orders", "filled", True),
    ("fills", "quantity", False),
    ("risk_decisions", "quantity", False),
)


def upgrade() -> None:
    for table, column, has_default in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Integer(),
            type_=sa.Numeric(14, 4),
            existing_nullable=False,
            existing_server_default=sa.text("0") if has_default else None,
        )


def downgrade() -> None:
    for table, column, has_default in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(14, 4),
            type_=sa.Integer(),
            existing_nullable=False,
            existing_server_default=sa.text("0") if has_default else None,
            postgresql_using=f"round({column})::integer",
        )
