"""仓位罗盘 · 复盘口径与权限脱敏单元测试（先于实现）。"""
from __future__ import annotations

from app.compass.review import (
    build_review,
    empty_day_stats,
    max_drawdown,
    redact_card,
)


def test_max_drawdown_uses_peak_to_trough() -> None:
    assert max_drawdown([100.0, 120.0, 90.0, 110.0]) == -25.0
    assert max_drawdown([100.0, 110.0, 115.0, 120.0]) == 0.0
    assert max_drawdown([]) is None


def test_empty_day_stats_counts_avoidance_and_missed_upside() -> None:
    returns = [-1.0, 2.0, -3.0, 1.0]
    empty = [True, True, True, False]
    stats = empty_day_stats(returns, empty)

    assert stats["empty_avoidance"] == 4.0    # 避开 -1 与 -3
    assert stats["missed_upside"] == 2.0      # 错过 +2
    assert stats["net_avoidance"] == 2.0
    assert stats["empty_days"] == 3


def test_build_review_flags_insufficient_sample() -> None:
    review = build_review(
        nav_rule=[100.0 + i for i in range(30)],
        nav_unconstrained=[100.0 + i * 0.8 for i in range(30)],
        benchmark_returns=[0.1] * 30,
        empty_flags=[False] * 30,
    )
    assert review["sample_insufficient"] is True
    assert review["stats"]["max_drawdown_rule"] is None


def test_build_review_returns_stats_and_chart_when_enough_samples() -> None:
    nav_rule = [100.0, 105.0, 95.0, 108.0] * 20
    nav_loose = [100.0, 102.0, 88.0, 101.0] * 20
    review = build_review(
        nav_rule=nav_rule,
        nav_unconstrained=nav_loose,
        benchmark_returns=[-0.5, 0.8] * 40,
        empty_flags=[True, False] * 40,
        violations_rule=3,
        violations_unconstrained=14,
    )
    assert review["sample_insufficient"] is False
    assert review["stats"]["max_drawdown_rule"] < 0
    assert review["stats"]["violations_rule"] == 3
    assert len(review["chart"]["dates"]) == 80


def test_free_tier_redacts_etf_and_truncates_candidates() -> None:
    card = {
        "tier": "pro",
        "budget": {"exec_cap": 40},
        "candidates": [
            {"sector_code": "A", "sector_name": "半导体", "etf_code": "512480"},
            {"sector_code": "B", "sector_name": "创新药", "etf_code": "159992"},
            {"sector_code": "C", "sector_name": "消费电子", "etf_code": "159732"},
        ],
        "scenarios": [
            {"id": "SC-01", "body": "free"},
            {"id": "SC-02", "body": "pro"},
            {"id": "SC-03", "body": "free"},
        ],
    }
    free = redact_card(card, "free")
    assert len(free["candidates"]) == 1
    assert "etf_code" not in free["candidates"][0]
    assert [s["id"] for s in free["scenarios"]] == ["SC-01", "SC-03"]
    assert free["locked_scenario_ids"] == ["SC-02", "SC-04", "SC-05", "SC-06"]

    pro = redact_card(card, "pro")
    assert len(pro["candidates"]) == 3
    assert pro["candidates"][0]["etf_code"] == "512480"
    assert len(pro["scenarios"]) == 3
