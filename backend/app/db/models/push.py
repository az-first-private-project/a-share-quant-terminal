"""盘后推送日志（V8 W3）。

唯一约束 `(run_id, channel)` 保证同一策略 run 只推送一次；失败可重试。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Date, DateTime, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PushLog(Base):
    __tablename__ = "push_log"
    __table_args__ = (
        UniqueConstraint("run_id", "channel", name="uq_push_log_run_channel"),
        Index("ix_push_log_trade_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    algo_code: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="wecom")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
