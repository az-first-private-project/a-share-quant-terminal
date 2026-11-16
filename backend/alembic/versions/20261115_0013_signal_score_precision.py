"""信号 score 精度扩展到 NUMERIC(20,4)（V8 W2）

Revision ID: 0013_signal_score_precision
Revises: 0012_market_factor_ohlc
Create Date: 2026-11-15

c1 按流通市值排序，原始 score 可达 1e12，NUMERIC(12,4) 会溢出。
stg_signal 与 stg_signal_perf 同步扩展。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_signal_score_precision"
down_revision: str | None = "0012_market_factor_ohlc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "stg_signal", "score",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(20, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "stg_signal_perf", "score",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(20, 4),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "stg_signal", "score",
        existing_type=sa.Numeric(20, 4),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
    op.alter_column(
        "stg_signal_perf", "score",
        existing_type=sa.Numeric(20, 4),
        type_=sa.Numeric(12, 4),
        existing_nullable=True,
    )
