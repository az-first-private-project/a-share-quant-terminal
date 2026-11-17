"""数据源尝试埋点表（V9 P0-3）

Revision ID: 0016_provider_health
Revises: 0015_oos_run
Create Date: 2026-11-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016_provider_health"
down_revision: str | None = "0015_oos_run"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def _has_index(bind, table: str, index: str) -> bool:
    return any(item["name"] == index for item in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "meta_provider_health"):
        op.create_table(
            "meta_provider_health",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=True),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("task", sa.String(length=64), nullable=False),
            sa.Column("result", sa.String(length=16), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index(bind, "meta_provider_health", "ix_provider_health_date_provider_task"):
        op.create_index(
            "ix_provider_health_date_provider_task",
            "meta_provider_health",
            ["trade_date", "provider", "task"],
        )
    if not _has_index(bind, "meta_provider_health", "ix_provider_health_created"):
        op.create_index(
            "ix_provider_health_created", "meta_provider_health", ["created_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "meta_provider_health"):
        if _has_index(bind, "meta_provider_health", "ix_provider_health_created"):
            op.drop_index("ix_provider_health_created", table_name="meta_provider_health")
        if _has_index(bind, "meta_provider_health", "ix_provider_health_date_provider_task"):
            op.drop_index(
                "ix_provider_health_date_provider_task", table_name="meta_provider_health"
            )
        op.drop_table("meta_provider_health")
