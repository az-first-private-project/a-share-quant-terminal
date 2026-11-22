"""纸面组合聚合回填单元测试（先于实现）。"""
from __future__ import annotations

from datetime import date, timedelta

from app.compass.paper_backfill import aggregate_paper_navs

D0 = date(2099, 1, 1)
D1 = D0 + timedelta(days=1)
D2 = D0 + timedelta(days=2)
D3 = D0 + timedelta(days=3)


def row(pid: str, day: date, nav: float, benchmark: float) -> dict:
    return {
        "portfolio_id": pid,
        "trade_date": day,
        "nav": nav,
        "benchmark": benchmark,
    }


def test_aggregate_normalizes_to_common_start_and_averages() -> None:
    rows = [
        row("A", D0, 1.0, 1.0),
        row("A", D1, 1.1, 1.02),
        row("A", D2, 1.2, 1.03),
        row("B", D1, 0.5, 0.5),
        row("B", D2, 0.55, 0.51),
    ]
    result = aggregate_paper_navs(rows, min_days=2, max_stale=1)

    assert result["portfolios"] == 2
    assert [r["trade_date"] for r in result["rows"]] == [D1, D2]
    assert result["rows"][0]["nav_rule"] == 100.0
    assert result["rows"][0]["nav_unconstrained"] == 100.0
    assert abs(result["rows"][1]["nav_rule"] - 109.55) < 0.01
    assert abs(result["rows"][1]["nav_unconstrained"] - 101.49) < 0.01


def test_aggregate_skips_dates_after_stale_gap() -> None:
    rows = [
        row("A", D0, 1.0, 1.0),
        row("A", D1, 1.1, 1.1),
        row("B", D0, 1.0, 1.0),
        row("B", D1, 1.0, 1.0),
        row("B", D2, 0.9, 0.9),
        row("B", D3, 0.8, 0.8),
    ]
    result = aggregate_paper_navs(rows, min_days=2, max_stale=1)

    # A 在 D2 用 D1 净值前向填充（间隔 1 个交易日），D3 超过容忍度后整日跳过
    assert [r["trade_date"] for r in result["rows"]] == [D0, D1, D2]
    assert result["rows"][-1]["nav_rule"] == 100.0


def test_aggregate_drops_portfolio_without_common_start() -> None:
    rows = [
        row("A", D0, 1.0, 1.0),
        row("A", D1, 1.1, 1.1),
        row("B", D2, 1.0, 1.0),
        row("B", D3, 1.1, 1.1),
    ]
    result = aggregate_paper_navs(rows, min_days=1, max_stale=1)

    assert result["portfolios"] == 1
    assert [r["trade_date"] for r in result["rows"]] == [D2, D3]


def test_aggregate_returns_empty_when_insufficient_history() -> None:
    rows = [row("A", D0, 1.0, 1.0)]
    result = aggregate_paper_navs(rows, min_days=2)
    assert result["rows"] == []
    assert result["portfolios"] == 0


def test_aggregate_computes_drawdown_for_both_series() -> None:
    rows = [
        row("A", D0, 1.0, 1.0),
        row("A", D1, 0.8, 0.9),
        row("A", D2, 1.2, 0.95),
        row("B", D0, 1.0, 1.0),
        row("B", D1, 0.8, 0.9),
        row("B", D2, 1.2, 0.95),
    ]
    result = aggregate_paper_navs(rows, min_days=3)

    rows = result["rows"]
    assert rows[1]["nav_rule"] == 80.0
    assert rows[1]["drawdown_rule"] == -20.0
    assert rows[1]["drawdown_unconstrained"] == -10.0
    assert rows[2]["drawdown_rule"] == 0.0
    assert rows[2]["drawdown_unconstrained"] == -5.0
