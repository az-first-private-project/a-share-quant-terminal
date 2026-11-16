"""企业微信推送日志 + signal_schema 默认值（V8 W3）

Revision ID: 0014_push_log
Revises: 0013_signal_score_precision
Create Date: 2026-11-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014_push_log"
down_revision: str | None = "0013_signal_score_precision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("algo_code", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "channel", name="uq_push_log_run_channel"),
    )
    op.create_index("ix_push_log_trade_date", "push_log", ["trade_date"])
    op.alter_column(
        "stg_run", "signal_schema",
        existing_type=sa.SmallInteger(),
        server_default="3",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "stg_run", "signal_schema",
        existing_type=sa.SmallInteger(),
        server_default="2",
        existing_nullable=False,
    )
    op.drop_index("ix_push_log_trade_date", table_name="push_log")
    op.drop_table("push_log")
