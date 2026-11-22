"""成分股广度：收盘价跌破 MA20 的成分占比。

统计口径：
- 分母只统计当日有有效收盘价、有效 MA20（>0）且未停牌的成分股；
- 分子为 close < MA20 的成分数（等于 MA20 不计入）；
- 有效成分少于 MIN_MEMBERS 时返回 insufficient，不用 0 代替；
- 成分归属使用 SCD2 当日有效版本，避免未来成分污染。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compass.constants import MIN_MEMBERS
from app.db.models import DimSectorMember, FactFeatureDaily, FactStockDaily


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_breadth_below_ma20(members: list[dict]) -> dict:
    """纯函数：输入 [{close, ma20, is_suspended}]，输出比例与样本数。"""
    valid: list[tuple[float, float]] = []
    for member in members:
        if member.get("is_suspended"):
            continue
        close = _f(member.get("close"))
        ma20 = _f(member.get("ma20"))
        if close is None or ma20 is None or ma20 <= 0:
            continue
        valid.append((close, ma20))

    if len(valid) < MIN_MEMBERS:
        return {
            "status": "insufficient",
            "ratio": None,
            "below": 0,
            "valid": len(valid),
        }

    below = sum(1 for close, ma20 in valid if close < ma20)
    return {
        "status": "ok",
        "ratio": round(below / len(valid), 4),
        "below": below,
        "valid": len(valid),
    }


async def load_breadth_below_ma20(
    db: AsyncSession,
    trade_date: date,
    sector_codes: list[str],
) -> dict[str, dict]:
    """从 SCD2 成分、个股日线与 MA20 计算指定板块的广度。"""
    if not sector_codes:
        return {}

    member_rows = (await db.execute(
        select(DimSectorMember.sector_code, DimSectorMember.stock_code)
        .where(and_(
            DimSectorMember.sector_code.in_(sector_codes),
            DimSectorMember.effective_from <= trade_date,
            or_(
                DimSectorMember.effective_to.is_(None),
                DimSectorMember.effective_to > trade_date,
            ),
        ))
    )).all()

    by_sector: dict[str, set[str]] = {code: set() for code in sector_codes}
    for sector_code, stock_code in member_rows:
        by_sector.setdefault(sector_code, set()).add(stock_code)

    all_codes = sorted({code for codes in by_sector.values() for code in codes})
    if not all_codes:
        return {
            code: compute_breadth_below_ma20([])
            for code in sector_codes
        }

    stock_rows = (await db.execute(
        select(
            FactStockDaily.code,
            FactStockDaily.close,
            FactStockDaily.is_suspended,
        ).where(and_(
            FactStockDaily.trade_date == trade_date,
            FactStockDaily.code.in_(all_codes),
        ))
    )).all()
    stock_map = {
        code: {"close": close, "is_suspended": bool(suspended)}
        for code, close, suspended in stock_rows
    }

    feature_rows = (await db.execute(
        select(FactFeatureDaily.code, FactFeatureDaily.ma20).where(and_(
            FactFeatureDaily.trade_date == trade_date,
            FactFeatureDaily.code.in_(all_codes),
        ))
    )).all()
    ma20_map = {code: ma20 for code, ma20 in feature_rows}

    result: dict[str, dict] = {}
    for sector_code in sector_codes:
        members = []
        for code in sorted(by_sector.get(sector_code) or set()):
            stock = stock_map.get(code) or {}
            members.append({
                "close": stock.get("close"),
                "is_suspended": stock.get("is_suspended", False),
                "ma20": ma20_map.get(code),
            })
        result[sector_code] = compute_breadth_below_ma20(members)
    return result
