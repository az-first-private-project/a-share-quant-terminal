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


def _has_table(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def _has_index(bind, table: str, index: str) -> bool:
    return any(item["name"] == index for item in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 基线用 Base.metadata.create_all 建表，会包含后来新增的 push_log。
    # 全新库执行到本迁移时表已存在，直接 CREATE TABLE 会报 DuplicateTable；
    # 老库（只到 0013）则仍需要本迁移补建。这里按实际 schema 幂等处理。
    created = False
    if not _has_table(bind, "push_log"):
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
        created = True

    if created or not _has_index(bind, "push_log", "ix_push_log_trade_date"):
        op.create_index("ix_push_log_trade_date", "push_log", ["trade_date"])

    op.alter_column(
        "stg_run", "signal_schema",
        existing_type=sa.SmallInteger(),
        server_default="3",
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.alter_column(
        "stg_run", "signal_schema",
        existing_type=sa.SmallInteger(),
        server_default="2",
        existing_nullable=False,
    )
    if _has_table(bind, "push_log"):
        if _has_index(bind, "push_log", "ix_push_log_trade_date"):
            op.drop_index("ix_push_log_trade_date", table_name="push_log")
        op.drop_table("push_log")
