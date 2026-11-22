"""仓位罗盘数据模型（V9）。

原则：
- 决策卡是快照，生成后不可覆写；新版本写新行。
- 失效记录只追加，不提供删除。
- ETF 映射用 SCD2（valid_from/valid_to），保证历史可复现。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Index, Integer, Numeric, PrimaryKeyConstraint,
    SmallInteger, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class DimEtfSectorMap(Base):
    """板块 ↔ ETF 映射（SCD2）。每个板块同一时点最多一个主映射。"""

    __tablename__ = "dim_etf_sector_map"
    __table_args__ = (
        UniqueConstraint("sector_code", "etf_code", "valid_from", name="uq_etf_sector_map_version"),
        Index(
            "uq_etf_sector_map_primary", "sector_code",
            unique=True,
            postgresql_where=text("is_primary AND valid_to IS NULL"),
        ),
        Index("ix_etf_sector_map_etf", "etf_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sector_code: Mapped[str] = mapped_column(String(24), nullable=False)
    etf_code: Mapped[str] = mapped_column(String(12), nullable=False)
    etf_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tracking_index: Mapped[str | None] = mapped_column(String(64))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FactEtfDaily(Base):
    """ETF 日行情（含规模、溢价、停牌）。"""

    __tablename__ = "fact_etf_daily"
    __table_args__ = (
        PrimaryKeyConstraint("trade_date", "etf_code", name="pk_fact_etf_daily"),
        Index("ix_fact_etf_daily_etf", "etf_code"),
    )

    trade_date: Mapped[date] = mapped_column(Date)
    etf_code: Mapped[str] = mapped_column(String(12))
    open: Mapped[float | None] = mapped_column(Numeric(12, 3))
    high: Mapped[float | None] = mapped_column(Numeric(12, 3))
    low: Mapped[float | None] = mapped_column(Numeric(12, 3))
    close: Mapped[float | None] = mapped_column(Numeric(12, 3))
    pre_close: Mapped[float | None] = mapped_column(Numeric(12, 3))
    pct_chg: Mapped[float | None] = mapped_column(Numeric(8, 3))
    amount: Mapped[float | None] = mapped_column(Numeric(24, 2), comment="成交额(元)")
    aum: Mapped[float | None] = mapped_column(Numeric(24, 2), comment="基金规模(元)")
    premium_rate: Mapped[float | None] = mapped_column(Numeric(8, 4), comment="溢价率 %")
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FactSectorSentimentPercentile(Base):
    """板块情绪分位（250 日滚动）。样本不足时 percentile 为 NULL。"""

    __tablename__ = "fact_sector_sentiment_percentile"
    __table_args__ = (
        PrimaryKeyConstraint("trade_date", "sector_code", "window_days",
                             name="pk_fact_sector_sentiment_percentile"),
        Index("ix_sector_sentiment_pct_trade", "trade_date"),
    )

    trade_date: Mapped[date] = mapped_column(Date)
    sector_code: Mapped[str] = mapped_column(String(24))
    window_days: Mapped[int] = mapped_column(SmallInteger, default=250)
    valid_samples: Mapped[int] = mapped_column(Integer, default=0)
    percentile: Mapped[int | None] = mapped_column(SmallInteger)
    crowding_level: Mapped[str] = mapped_column(String(16), default="样本不足")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class FactDecisionCard(Base):
    """每日决策卡快照。card_id 稳定，生成后核心字段不可更新。"""

    __tablename__ = "fact_decision_card"
    __table_args__ = (
        UniqueConstraint("trade_date", "rule_version", name="uq_decision_card_day_version"),
        Index("ix_decision_card_trade_date", "trade_date"),
        Index("ix_decision_card_status", "status"),
    )

    card_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_version: Mapped[str] = mapped_column(String(16), default="sector-v5.1")
    regime_state: Mapped[str] = mapped_column(String(16), nullable=False)
    regime_tier: Mapped[str] = mapped_column(String(8), nullable=False)
    temp_score: Mapped[float | None] = mapped_column(Numeric(5, 1))
    market_cap: Mapped[int] = mapped_column(SmallInteger, default=0)
    exec_cap: Mapped[int] = mapped_column(SmallInteger, default=0)
    per_sector_cap: Mapped[int] = mapped_column(SmallInteger, default=0)
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    rejected: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    empty_reasons: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    derivation: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="generated")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[str | None] = mapped_column(String(36))


class FactDecisionCheck(Base):
    """T+1 开盘校验记录。一个 card+ETF 只允许一条。"""

    __tablename__ = "fact_decision_check"
    __table_args__ = (
        UniqueConstraint("card_id", "etf_code", name="uq_decision_check_card_etf"),
        UniqueConstraint("idempotency_key", name="uq_decision_check_idem"),
        Index("ix_decision_check_card", "card_id"),
    )

    check_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    card_id: Mapped[str] = mapped_column(String(36), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    etf_code: Mapped[str] = mapped_column(String(12), nullable=False)
    prev_close: Mapped[float | None] = mapped_column(Numeric(12, 3))
    open_price: Mapped[float | None] = mapped_column(Numeric(12, 3))
    open_gap: Mapped[float | None] = mapped_column(Numeric(8, 3))
    result: Mapped[str] = mapped_column(String(16), default="PENDING")
    user_action: Mapped[str | None] = mapped_column(String(16))
    action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recheck_gap: Mapped[float | None] = mapped_column(Numeric(8, 3))
    final_status: Mapped[str | None] = mapped_column(String(24))
    user_override: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(96))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FactScenarioTrigger(Base):
    """情景卡每日状态与触发证据。一张卡一个情景一行。"""

    __tablename__ = "fact_scenario_trigger"
    __table_args__ = (
        UniqueConstraint("card_id", "scenario_id", name="uq_scenario_trigger_card_sid"),
        Index("ix_scenario_trigger_card", "card_id"),
    )

    trigger_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    card_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FactSignalFailure(Base):
    """信号失效记录：只追加，不删除。"""

    __tablename__ = "fact_signal_failure"
    __table_args__ = (Index("ix_signal_failure_date", "failure_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    failure_date: Mapped[date] = mapped_column(Date, nullable=False)
    failure_type: Mapped[str] = mapped_column(String(32), nullable=False)
    impact_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    fix_version: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class FactPaperPortfolio(Base):
    """纸面组合日净值（复盘只读回撤与违规次数，不展示收益率）。"""

    __tablename__ = "fact_paper_portfolio"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav_rule: Mapped[float | None] = mapped_column(Numeric(14, 4))
    nav_unconstrained: Mapped[float | None] = mapped_column(Numeric(14, 4))
    drawdown_rule: Mapped[float | None] = mapped_column(Numeric(8, 3))
    drawdown_unconstrained: Mapped[float | None] = mapped_column(Numeric(8, 3))
    violations_rule: Mapped[int] = mapped_column(Integer, default=0)
    violations_unconstrained: Mapped[int] = mapped_column(Integer, default=0)
    benchmark_code: Mapped[str] = mapped_column(String(16), default="sh000905")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CompassPushLog(Base):
    """仓位罗盘推送日志（幂等：card_id + channel）。"""

    __tablename__ = "compass_push_log"
    __table_args__ = (
        UniqueConstraint("card_id", "channel", name="uq_compass_push_card_channel"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    card_id: Mapped[str] = mapped_column(String(36), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default="wecom")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
