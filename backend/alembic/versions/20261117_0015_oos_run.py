"""样本外 walk-forward 结果表（V9 P0-2）

Revision ID: 0015_oos_run
Revises: 0014_push_log
Create Date: 2026-11-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_oos_run"
down_revision: str | None = "0014_push_log"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def _has_index(bind, table: str, index: str) -> bool:
    return any(item["name"] == index for item in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "stg_oos_run"):
        op.create_table(
            "stg_oos_run",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("fold_index", sa.Integer(), nullable=False),
            sa.Column("algo_code", sa.String(length=16), nullable=False),
            sa.Column("train_start", sa.Date(), nullable=False),
            sa.Column("train_end", sa.Date(), nullable=False),
            sa.Column("test_start", sa.Date(), nullable=False),
            sa.Column("test_end", sa.Date(), nullable=False),
            sa.Column("window_type", sa.String(length=16), server_default="fixed", nullable=False),
            sa.Column("degraded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("param_fingerprint", sa.String(length=32), nullable=True),
            sa.Column("signal_schema", sa.SmallInteger(), server_default="3", nullable=False),
            sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", "fold_index", name="pk_stg_oos_run"),
        )

    if not _has_index(bind, "stg_oos_run", "ix_stg_oos_run_algo_created"):
        op.create_index(
            "ix_stg_oos_run_algo_created", "stg_oos_run", ["algo_code", "created_at"]
        )
    if not _has_index(bind, "stg_oos_run", "ix_stg_oos_run_status"):
        op.create_index("ix_stg_oos_run_status", "stg_oos_run", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "stg_oos_run"):
        if _has_index(bind, "stg_oos_run", "ix_stg_oos_run_status"):
            op.drop_index("ix_stg_oos_run_status", table_name="stg_oos_run")
        if _has_index(bind, "stg_oos_run", "ix_stg_oos_run_algo_created"):
            op.drop_index("ix_stg_oos_run_algo_created", table_name="stg_oos_run")
        op.drop_table("stg_oos_run")
