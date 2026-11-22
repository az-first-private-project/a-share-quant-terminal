"""复盘口径与权限脱敏。

复盘只统计回撤、错误交易次数、空仓避损与错过涨幅，不展示收益率。
"""
from __future__ import annotations

import copy
from typing import Any

from app.compass.constants import ALL_SCENARIO_IDS, FREE_SCENARIO_IDS

MIN_REVIEW_DAYS = 60


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def max_drawdown(navs: list[float]) -> float | None:
    """最大回撤（百分比，负数）。"""
    if not navs:
        return None
    peak = float(navs[0])
    worst = 0.0
    for value in navs:
        current = float(value)
        if current > peak:
            peak = current
        if peak > 0:
            worst = min(worst, (current / peak - 1.0) * 100.0)
    return _round(worst)


def drawdown_series(navs: list[float]) -> list[float]:
    """逐日回撤序列，用于图表。"""
    peak = None
    series: list[float] = []
    for value in navs:
        current = float(value)
        peak = current if peak is None else max(peak, current)
        series.append(_round((current / peak - 1.0) * 100.0) if peak else 0.0)
    return series


def empty_day_stats(
    benchmark_returns: list[float],
    empty_flags: list[bool],
) -> dict:
    """空仓避损 = 空仓日避开下跌；错过涨幅 = 空仓日错过上涨。单位：百分点。"""
    size = min(len(benchmark_returns), len(empty_flags))
    avoidance = 0.0
    missed = 0.0
    days = 0
    for index in range(size):
        if not empty_flags[index]:
            continue
        days += 1
        value = float(benchmark_returns[index])
        if value < 0:
            avoidance += -value
        elif value > 0:
            missed += value
    return {
        "empty_days": days,
        "empty_avoidance": _round(avoidance),
        "missed_upside": _round(missed),
        "net_avoidance": _round(avoidance - missed),
    }


def build_review(
    *,
    nav_rule: list[float],
    nav_unconstrained: list[float],
    benchmark_returns: list[float],
    empty_flags: list[bool],
    violations_rule: int = 0,
    violations_unconstrained: int = 0,
    dates: list[str] | None = None,
) -> dict:
    size = len(nav_rule)
    sample_insufficient = size < MIN_REVIEW_DAYS or len(nav_unconstrained) < MIN_REVIEW_DAYS

    if sample_insufficient:
        return {
            "sample_insufficient": True,
            "stats": {
                "max_drawdown_rule": None,
                "max_drawdown_unconstrained": None,
                "violations_rule": violations_rule,
                "violations_unconstrained": violations_unconstrained,
                "empty_avoidance": None,
                "missed_upside": None,
                "net_avoidance": None,
                "empty_days": None,
            },
            "chart": {"dates": [], "drawdown_rule": [], "drawdown_unconstrained": []},
        }

    empty_stats = empty_day_stats(benchmark_returns, empty_flags)
    chart_dates = dates if dates is not None else [f"D{index + 1:03d}" for index in range(size)]

    return {
        "sample_insufficient": False,
        "stats": {
            "max_drawdown_rule": max_drawdown(nav_rule),
            "max_drawdown_unconstrained": max_drawdown(nav_unconstrained),
            "violations_rule": violations_rule,
            "violations_unconstrained": violations_unconstrained,
            **empty_stats,
        },
        "chart": {
            "dates": chart_dates,
            "drawdown_rule": drawdown_series(nav_rule),
            "drawdown_unconstrained": drawdown_series(nav_unconstrained),
        },
    }


def redact_card(card: dict, tier: str) -> dict:
    """字段级脱敏：免费版只保留 1 个候选、去掉 ETF 代码、锁定 4 张情景卡。"""
    out = copy.deepcopy(card)
    if tier == "pro":
        return out

    candidates = out.get("candidates") or []
    trimmed = []
    for cand in candidates[:1]:
        item = dict(cand)
        item.pop("etf_code", None)
        item.pop("etf_name", None)
        trimmed.append(item)
    out["candidates"] = trimmed

    scenarios = out.get("scenarios") or []
    out["scenarios"] = [s for s in scenarios if s.get("id") in FREE_SCENARIO_IDS]
    out["locked_scenario_ids"] = [
        sid for sid in ALL_SCENARIO_IDS if sid not in FREE_SCENARIO_IDS
    ]
    return out
