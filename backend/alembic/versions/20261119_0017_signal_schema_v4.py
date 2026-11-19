"""stg_run.signal_schema 默认值升级到 4（2026-11 因子优化）

Revision ID: 0017_signal_schema_v4
Revises: 0016_provider_health
Create Date: 2026-11-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_signal_schema_v4"
down_revision: str | None = "0016_provider_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "stg_run",
        "signal_schema",
        server_default="4",
        existing_type=sa.SmallInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "stg_run",
        "signal_schema",
        server_default="3",
        existing_type=sa.SmallInteger(),
        existing_nullable=True,
    )
