"""brackets, orders, fills: the execution path (M8)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
PRICE = sa.Numeric(precision=14, scale=4)


def _stamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "brackets",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("book", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry", PRICE, nullable=False),
        sa.Column("stop", PRICE, nullable=False),
        sa.Column("initial_stop", PRICE, nullable=False),
        sa.Column("target", PRICE, nullable=False),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("entry_price", PRICE, nullable=True),
        sa.Column("exit_qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("exit_price", PRICE, nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column(
            "decision",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["id"], ["proposals.id"], name=op.f("fk_brackets_id_proposals")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_brackets_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brackets")),
    )
    op.create_index(op.f("ix_brackets_book"), "brackets", ["book"], unique=False)
    op.create_index(op.f("ix_brackets_state"), "brackets", ["state"], unique=False)
    op.create_table(
        "orders",
        sa.Column("order_ref", sa.Text(), nullable=False),
        sa.Column("bracket_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", PRICE, nullable=True),
        sa.Column("stop_price", PRICE, nullable=True),
        sa.Column("tif", sa.Text(), nullable=False),
        sa.Column("good_till", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_ref", sa.Text(), nullable=True),
        sa.Column("oca_group", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filled", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("avg_fill_price", PRICE, nullable=True),
        sa.Column("broker_order_id", sa.Integer(), nullable=True),
        sa.Column("perm_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["bracket_id"], ["brackets.id"], name=op.f("fk_orders_bracket_id_brackets")
        ),
        sa.PrimaryKeyConstraint("order_ref", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_bracket_id"), "orders", ["bracket_id"], unique=False)
    op.create_table(
        "fills",
        sa.Column("exec_id", sa.Text(), nullable=False),
        sa.Column("order_ref", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("commission", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.ForeignKeyConstraint(
            ["order_ref"], ["orders.order_ref"], name=op.f("fk_fills_order_ref_orders")
        ),
        sa.PrimaryKeyConstraint("exec_id", name=op.f("pk_fills")),
    )
    op.create_index(op.f("ix_fills_order_ref"), "fills", ["order_ref"], unique=False)


def downgrade() -> None:
    op.drop_table("fills")
    op.drop_table("orders")
    op.drop_table("brackets")
