"""策略市场状态与策略门槛。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
import logging

from datetime import date
from typing import Any
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.fact import MarketIndex, MarketRegimeDaily

logger = logging.getLogger(__name__)

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403

async def load_market_context(
    db: AsyncSession,
    trade_date: date,
    index_code: str = BENCHMARK_INDEX_CODE,
) -> dict[str, Any]:
    """基准指数与市场阶段（均截至 T 日），用于 A4/C6 的市场门槛。

    只使用 <= trade_date 的数据；指数或阶段数据缺失时对应门槛放行
    （不因为基准/阶段缺失而让策略永远空结果）。
    """
    regime_stmt = (
        select(
            MarketRegimeDaily.trade_date,
            MarketRegimeDaily.regime_code,
            MarketRegimeDaily.regime_label,
            MarketRegimeDaily.regime_since,
            MarketRegimeDaily.regime_days,
        )
        .where(MarketRegimeDaily.trade_date <= trade_date)
        .order_by(MarketRegimeDaily.trade_date.desc())
        .limit(1)
    )
    try:
        regime_row = (await db.execute(regime_stmt)).first()
    except ProgrammingError:
        logger.warning("market_regime_daily 表不存在，市场阶段门槛按放行处理")
        regime_row = None
    regime_info: dict[str, Any] = {
        "regime_available": regime_row is not None,
        "regime_trade_date": regime_row[0].isoformat() if regime_row else None,
        "regime_code": regime_row[1] if regime_row else None,
        "regime_label": regime_row[2] if regime_row else None,
        "regime_since": regime_row[3].isoformat() if regime_row and regime_row[3] else None,
        "regime_days": int(regime_row[4]) if regime_row and regime_row[4] is not None else None,
    }

    statement = (
        select(MarketIndex.trade_date, MarketIndex.close, MarketIndex.pct_chg)
        .where(MarketIndex.index_code == index_code)
        .where(MarketIndex.trade_date <= trade_date)
        .order_by(MarketIndex.trade_date.desc())
        .limit(30)
    )
    try:
        rows = (await db.execute(statement)).all()
    except ProgrammingError:
        logger.warning("market_indices 表不存在，市场状态门槛按放行处理")
        rows = []

    if not rows:
        logger.warning(
            "基准指数 %s 在 %s 前无数据，市场状态门槛按放行处理", index_code, trade_date
        )
        return {
            "available": False,
            "index_code": index_code,
            "c6_ok": True,
            **regime_info,
        }

    ordered = list(reversed(rows))
    closes = [
        float(close) for _, close, _ in ordered
        if close is not None and float(close) > 0
    ]
    last_close = closes[-1] if closes else None
    ma20 = round(sum(closes[-20:]) / 20.0, 4) if len(closes) >= 20 else None
    raw_pct = ordered[-1][2]
    last_pct = float(raw_pct) if raw_pct is not None else None

    return {
        "available": True,
        "index_code": index_code,
        "close": round(last_close, 2) if last_close is not None else None,
        "ma20": ma20,
        "pct_chg": last_pct,
        "c6_ok": True if (last_close is None or ma20 is None) else last_close >= ma20 * 0.995,
        **regime_info,
    }

def market_gate_payload(market: dict | None, algo_code: str) -> dict[str, Any] | None:
    """run 级市场门槛摘要（A4/C6），用于 0 信号时解释原因。"""
    if not market:
        return None
    available = bool(market.get("available", False))
    payload: dict[str, Any] = {
        "index_code": market.get("index_code", BENCHMARK_INDEX_CODE),
        "available": available,
        "close": jsonable(market.get("close")),
        "ma20": jsonable(market.get("ma20")),
        "pct_chg": jsonable(market.get("pct_chg")),
    }
    if algo_code == "a4":
        regime_available = bool(market.get("regime_available", False))
        regime_code = market.get("regime_code")
        passed = True if not regime_available else regime_code in A4_DEFENSIVE_REGIMES
        payload.update(
            {
                "passed": passed,
                "rule": (
                    "市场阶段数据缺失，防御型门槛按放行处理"
                    if not regime_available
                    else "仅在防御型市场阶段启用（震荡筑底/向下调整/避险下跌）"
                ),
                "regime_code": regime_code,
                "regime_label": market.get("regime_label"),
                "regime_trade_date": market.get("regime_trade_date"),
                "regime_since": market.get("regime_since"),
                "regime_days": market.get("regime_days"),
                "regime_available": regime_available,
                "threshold": sorted(A4_DEFENSIVE_REGIMES),
            }
        )
        return payload
    if not available:
        payload.update({"passed": True, "rule": "指数数据缺失，市场门槛按放行处理"})
        return payload
    ma20 = market.get("ma20")
    payload.update(
        {
            "passed": bool(market.get("c6_ok", True)),
            "rule": "沪深300 收盘 ≥ MA20 × 0.995",
            "threshold": None if ma20 is None else round(float(ma20) * 0.995, 4),
        }
    )
    return payload

def _market_flag(df: pd.DataFrame, key: str) -> bool:
    """读取市场状态门槛；无 attrs（单测/回测）或指数缺失时放行。"""
    market = df.attrs.get("market") or {}
    return bool(market.get(key, True))

__all__ = [
    "load_market_context",
    "market_gate_payload",
    "_market_flag",
]
