"""纸面组合聚合回填：paper_nav → fact_paper_portfolio。

口径：
- 只纳入 active 且净值记录数 >= MIN_NAV_DAYS 的组合；
- 以所有纳入组合的“共同起始日”为 100 基准；
- 单组合缺失最多前向填充 MAX_STALE_STEPS 个交易日；任一组合超过容忍度则整日跳过，
  避免组合成分漂移造成净值跳变；
- nav_rule = 等权归一净值，nav_unconstrained = 等权归一基准；
- 回撤按各自序列的峰值回撤计算，单位 %。
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import FactPaperPortfolio, PaperNav, PaperPortfolio

logger = get_logger(__name__)

MIN_NAV_DAYS = 60
MAX_STALE_STEPS = 3
BENCHMARK_CODE = "sh000300"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def aggregate_paper_navs(
    rows: list[dict],
    *,
    min_days: int = MIN_NAV_DAYS,
    max_stale: int = MAX_STALE_STEPS,
) -> dict:
    """把多组合净值聚合为一条复盘序列（纯函数，便于单测）。"""
    grouped: dict[str, dict[date, tuple[float, float]]] = {}
    for row in rows:
        pid = str(row.get("portfolio_id") or "")
        day = row.get("trade_date")
        nav = _f(row.get("nav"))
        benchmark = _f(row.get("benchmark"))
        if not pid or not isinstance(day, date):
            continue
        if nav is None or benchmark is None or nav <= 0 or benchmark <= 0:
            continue
        grouped.setdefault(pid, {})[day] = (nav, benchmark)

    eligible = {
        pid: sorted(items.items())
        for pid, items in grouped.items()
        if len(items) >= max(1, min_days)
    }
    if not eligible:
        return {"rows": [], "portfolios": 0, "reason": "insufficient_history"}

    start = max(items[0][0] for items in eligible.values())
    eligible = {
        pid: items for pid, items in eligible.items()
        if any(day == start for day, _ in items)
    }
    if not eligible:
        return {"rows": [], "portfolios": 0, "reason": "no_common_start"}

    bases = {
        pid: next(value for day, value in items if day == start)
        for pid, items in eligible.items()
    }
    all_dates = sorted({
        day
        for items in eligible.values()
        for day, _ in items
        if day >= start
    })
    date_index = {day: index for index, day in enumerate(all_dates)}
    pointers = {pid: 0 for pid in eligible}

    series: list[dict] = []
    for current in all_dates:
        index = date_index[current]
        snapshot: dict[str, tuple[float, float]] = {}
        for pid, items in eligible.items():
            pointer = pointers[pid]
            while pointer + 1 < len(items) and items[pointer + 1][0] <= current:
                pointer += 1
            pointers[pid] = pointer
            day, (nav, benchmark) = items[pointer]
            if day > current:
                continue
            if index - date_index[day] > max_stale:
                continue
            base_nav, base_benchmark = bases[pid]
            snapshot[pid] = (
                nav / base_nav * 100.0,
                benchmark / base_benchmark * 100.0,
            )

        # 任一组合断档超过容忍度：整日跳过，保证聚合成分稳定
        if len(snapshot) != len(eligible):
            continue

        series.append({
            "trade_date": current,
            "nav_rule": _round(mean(value[0] for value in snapshot.values())),
            "nav_unconstrained": _round(mean(value[1] for value in snapshot.values())),
        })

    peak_rule: float | None = None
    peak_unconstrained: float | None = None
    for row in series:
        peak_rule = row["nav_rule"] if peak_rule is None else max(peak_rule, row["nav_rule"])
        peak_unconstrained = (
            row["nav_unconstrained"] if peak_unconstrained is None
            else max(peak_unconstrained, row["nav_unconstrained"])
        )
        row["drawdown_rule"] = _round((row["nav_rule"] / peak_rule - 1.0) * 100.0) if peak_rule else 0.0
        row["drawdown_unconstrained"] = (
            _round((row["nav_unconstrained"] / peak_unconstrained - 1.0) * 100.0)
            if peak_unconstrained else 0.0
        )

    return {
        "rows": series,
        "portfolios": len(eligible),
        "start": start,
        "end": series[-1]["trade_date"] if series else None,
        "reason": None,
    }


async def backfill_paper_portfolio(
    db: AsyncSession,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    min_days: int = MIN_NAV_DAYS,
) -> dict:
    """读取 active 纸面组合净值并 upsert 到 fact_paper_portfolio。"""
    stmt = (
        select(
            PaperNav.portfolio_id,
            PaperNav.trade_date,
            PaperNav.nav,
            PaperNav.benchmark,
        )
        .join(PaperPortfolio, PaperPortfolio.portfolio_id == PaperNav.portfolio_id)
        .where(PaperPortfolio.status == "active")
        .order_by(PaperNav.portfolio_id, PaperNav.trade_date)
    )
    if start_date is not None:
        stmt = stmt.where(PaperNav.trade_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(PaperNav.trade_date <= end_date)

    rows = [
        {
            "portfolio_id": portfolio_id,
            "trade_date": trade_date,
            "nav": nav,
            "benchmark": benchmark,
        }
        for portfolio_id, trade_date, nav, benchmark in (await db.execute(stmt)).all()
    ]
    aggregated = aggregate_paper_navs(rows, min_days=min_days)
    if not aggregated["rows"]:
        return {
            "status": "insufficient",
            "rows": 0,
            "portfolios": 0,
            "reason": aggregated.get("reason"),
        }

    now = datetime.now()
    values = [
        {
            "trade_date": row["trade_date"],
            "nav_rule": row["nav_rule"],
            "nav_unconstrained": row["nav_unconstrained"],
            "drawdown_rule": row["drawdown_rule"],
            "drawdown_unconstrained": row["drawdown_unconstrained"],
            "violations_rule": 0,
            "violations_unconstrained": 0,
            "benchmark_code": BENCHMARK_CODE,
            "updated_at": now,
        }
        for row in aggregated["rows"]
    ]
    insert_stmt = pg_insert(FactPaperPortfolio).values(values)
    insert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=["trade_date"],
        set_={
            "nav_rule": insert_stmt.excluded.nav_rule,
            "nav_unconstrained": insert_stmt.excluded.nav_unconstrained,
            "drawdown_rule": insert_stmt.excluded.drawdown_rule,
            "drawdown_unconstrained": insert_stmt.excluded.drawdown_unconstrained,
            "violations_rule": insert_stmt.excluded.violations_rule,
            "violations_unconstrained": insert_stmt.excluded.violations_unconstrained,
            "benchmark_code": insert_stmt.excluded.benchmark_code,
            "updated_at": insert_stmt.excluded.updated_at,
        },
    )
    await db.execute(insert_stmt)
    await db.flush()

    result = {
        "status": "ok",
        "rows": len(values),
        "portfolios": aggregated["portfolios"],
        "start": aggregated["start"].isoformat() if aggregated["start"] else None,
        "end": aggregated["end"].isoformat() if aggregated["end"] else None,
    }
    logger.info(
        "纸面组合回填完成 rows=%d portfolios=%d %s~%s",
        result["rows"], result["portfolios"], result["start"], result["end"],
    )
    return result
