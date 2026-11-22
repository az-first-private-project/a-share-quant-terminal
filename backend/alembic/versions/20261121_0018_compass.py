"""仓位罗盘 V9 数据表（幂等迁移）

Revision ID: 0018_compass
Revises: 0017_signal_schema_v4
Create Date: 2026-11-21

覆盖：ETF 映射与行情、情绪分位、决策卡、T+1 校验、情景触发、失效记录、
纸面组合与推送日志。全部使用 IF NOT EXISTS 语义，兼容全新库与旧库。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_compass"
down_revision: str | None = "0017_signal_schema_v4"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(item.get("name") == name for item in sa.inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _has_table("dim_etf_sector_map"):
        op.create_table(
            "dim_etf_sector_map",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("sector_code", sa.String(24), nullable=False),
            sa.Column("etf_code", sa.String(12), nullable=False),
            sa.Column("etf_name", sa.String(64), nullable=False),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("tracking_index", sa.String(64)),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("valid_to", sa.Date()),
            sa.Column("source", sa.String(32), server_default="manual"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.UniqueConstraint("sector_code", "etf_code", "valid_from",
                                name="uq_etf_sector_map_version"),
        )
    if not _has_index("dim_etf_sector_map", "uq_etf_sector_map_primary"):
        op.create_index(
            "uq_etf_sector_map_primary", "dim_etf_sector_map", ["sector_code"],
            unique=True, postgresql_where=sa.text("is_primary AND valid_to IS NULL"),
        )
    if not _has_index("dim_etf_sector_map", "ix_etf_sector_map_etf"):
        op.create_index("ix_etf_sector_map_etf", "dim_etf_sector_map", ["etf_code"])

    if not _has_table("fact_etf_daily"):
        op.create_table(
            "fact_etf_daily",
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("etf_code", sa.String(12), nullable=False),
            sa.Column("open", sa.Numeric(12, 3)),
            sa.Column("high", sa.Numeric(12, 3)),
            sa.Column("low", sa.Numeric(12, 3)),
            sa.Column("close", sa.Numeric(12, 3)),
            sa.Column("pre_close", sa.Numeric(12, 3)),
            sa.Column("pct_chg", sa.Numeric(8, 3)),
            sa.Column("amount", sa.Numeric(24, 2)),
            sa.Column("aum", sa.Numeric(24, 2)),
            sa.Column("premium_rate", sa.Numeric(8, 4)),
            sa.Column("is_suspended", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("trade_date", "etf_code", name="pk_fact_etf_daily"),
        )
    if not _has_index("fact_etf_daily", "ix_fact_etf_daily_etf"):
        op.create_index("ix_fact_etf_daily_etf", "fact_etf_daily", ["etf_code"])

    if not _has_table("fact_sector_sentiment_percentile"):
        op.create_table(
            "fact_sector_sentiment_percentile",
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("sector_code", sa.String(24), nullable=False),
            sa.Column("window_days", sa.SmallInteger(), nullable=False, server_default="250"),
            sa.Column("valid_samples", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("percentile", sa.SmallInteger()),
            sa.Column("crowding_level", sa.String(16), nullable=False, server_default="样本不足"),
            sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("trade_date", "sector_code", "window_days",
                                    name="pk_fact_sector_sentiment_percentile"),
        )
    if not _has_index("fact_sector_sentiment_percentile", "ix_sector_sentiment_pct_trade"):
        op.create_index("ix_sector_sentiment_pct_trade",
                        "fact_sector_sentiment_percentile", ["trade_date"])

    if not _has_table("fact_decision_card"):
        op.create_table(
            "fact_decision_card",
            sa.Column("card_id", sa.String(36), primary_key=True),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("rule_version", sa.String(16), nullable=False),
            sa.Column("signal_version", sa.String(16), server_default="sector-v5.1"),
            sa.Column("regime_state", sa.String(16), nullable=False),
            sa.Column("regime_tier", sa.String(8), nullable=False),
            sa.Column("temp_score", sa.Numeric(5, 1)),
            sa.Column("market_cap", sa.SmallInteger(), server_default="0"),
            sa.Column("exec_cap", sa.SmallInteger(), server_default="0"),
            sa.Column("per_sector_cap", sa.SmallInteger(), server_default="0"),
            sa.Column("candidates", JSONB),
            sa.Column("rejected", JSONB),
            sa.Column("empty_reasons", JSONB),
            sa.Column("derivation", JSONB),
            sa.Column("status", sa.String(16), server_default="generated"),
            sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.Column("supersedes_id", sa.String(36)),
            sa.UniqueConstraint("trade_date", "rule_version", name="uq_decision_card_day_version"),
        )
    if not _has_index("fact_decision_card", "ix_decision_card_trade_date"):
        op.create_index("ix_decision_card_trade_date", "fact_decision_card", ["trade_date"])
    if not _has_index("fact_decision_card", "ix_decision_card_status"):
        op.create_index("ix_decision_card_status", "fact_decision_card", ["status"])

    if not _has_table("fact_decision_check"):
        op.create_table(
            "fact_decision_check",
            sa.Column("check_id", sa.String(36), primary_key=True),
            sa.Column("card_id", sa.String(36), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("etf_code", sa.String(12), nullable=False),
            sa.Column("prev_close", sa.Numeric(12, 3)),
            sa.Column("open_price", sa.Numeric(12, 3)),
            sa.Column("open_gap", sa.Numeric(8, 3)),
            sa.Column("result", sa.String(16), server_default="PENDING"),
            sa.Column("user_action", sa.String(16)),
            sa.Column("action_at", sa.DateTime(timezone=True)),
            sa.Column("recheck_gap", sa.Numeric(8, 3)),
            sa.Column("final_status", sa.String(24)),
            sa.Column("user_override", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("idempotency_key", sa.String(96)),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.UniqueConstraint("card_id", "etf_code", name="uq_decision_check_card_etf"),
            sa.UniqueConstraint("idempotency_key", name="uq_decision_check_idem"),
        )
    if not _has_index("fact_decision_check", "ix_decision_check_card"):
        op.create_index("ix_decision_check_card", "fact_decision_check", ["card_id"])

    if not _has_table("fact_scenario_trigger"):
        op.create_table(
            "fact_scenario_trigger",
            sa.Column("trigger_id", sa.String(36), primary_key=True),
            sa.Column("card_id", sa.String(36), nullable=False),
            sa.Column("scenario_id", sa.String(8), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("evidence", JSONB),
            sa.Column("triggered_at", sa.DateTime(timezone=True)),
            sa.Column("expired_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("card_id", "scenario_id", name="uq_scenario_trigger_card_sid"),
        )
    if not _has_index("fact_scenario_trigger", "ix_scenario_trigger_card"):
        op.create_index("ix_scenario_trigger_card", "fact_scenario_trigger", ["card_id"])

    if not _has_table("fact_signal_failure"):
        op.create_table(
            "fact_signal_failure",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("failure_date", sa.Date(), nullable=False),
            sa.Column("failure_type", sa.String(32), nullable=False),
            sa.Column("impact_pct", sa.Numeric(8, 2)),
            sa.Column("root_cause", sa.Text(), nullable=False),
            sa.Column("fix_version", sa.String(16)),
            sa.Column("status", sa.String(16), server_default="open"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )
    if not _has_index("fact_signal_failure", "ix_signal_failure_date"):
        op.create_index("ix_signal_failure_date", "fact_signal_failure", ["failure_date"])

    if not _has_table("fact_paper_portfolio"):
        op.create_table(
            "fact_paper_portfolio",
            sa.Column("trade_date", sa.Date(), primary_key=True),
            sa.Column("nav_rule", sa.Numeric(14, 4)),
            sa.Column("nav_unconstrained", sa.Numeric(14, 4)),
            sa.Column("drawdown_rule", sa.Numeric(8, 3)),
            sa.Column("drawdown_unconstrained", sa.Numeric(8, 3)),
            sa.Column("violations_rule", sa.Integer(), server_default="0"),
            sa.Column("violations_unconstrained", sa.Integer(), server_default="0"),
            sa.Column("benchmark_code", sa.String(16), server_default="sh000905"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )

    if not _has_table("compass_push_log"):
        op.create_table(
            "compass_push_log",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("card_id", sa.String(36), nullable=False),
            sa.Column("channel", sa.String(16), nullable=False, server_default="wecom"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("card_id", "channel", name="uq_compass_push_card_channel"),
        )


def downgrade() -> None:
    for table in (
        "compass_push_log",
        "fact_paper_portfolio",
        "fact_signal_failure",
        "fact_scenario_trigger",
        "fact_decision_check",
        "fact_decision_card",
        "fact_sector_sentiment_percentile",
        "fact_etf_daily",
        "dim_etf_sector_map",
    ):
        if _has_table(table):
            op.drop_table(table)
