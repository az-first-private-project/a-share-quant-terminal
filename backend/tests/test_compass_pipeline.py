"""仓位罗盘 · 决策流水线单元测试（先于实现）。

覆盖：
- 五态 → 三档预算聚合
- 单板块上限的 5% 步长取整
- 初筛池排序确定性
- 五道门槛（绝对、连续性、广度、相关性、ETF）
- TOP3 截断与不补位
- 空仓原因优先级
"""
from __future__ import annotations

from app.compass.pipeline import (
    aggregate_regime,
    build_decision,
    compute_per_sector_cap,
    rank_pool,
    tier_cap,
)


def cand(code: str, composite: float, **overrides) -> dict:
    base = {
        "sector_code": code,
        "sector_name": f"板块{code}",
        "composite": composite,
        "technical": composite,
        "sentiment": composite,
        "news": composite,
        "sentiment_percentile": 50,
        "crowding_level": "正常",
        "member_count": 20,
        "is_partial": False,
        "data_complete": True,
        "up_count": 12,
        "down_count": 8,
        "main_net_inflow": 1.0e8,
        "close": 100.0,
        "ma20": 95.0,
        "breadth_below_ma20": 0.2,
    }
    base.update(overrides)
    return base


def etf(code: str = "512480", **overrides) -> dict:
    base = {
        "etf_code": code,
        "etf_name": f"ETF{code}",
        "is_primary": True,
        "listed": True,
        "suspended": False,
        "aum": 1.28e10,
        "adv20": 9.4e8,
        "premium_rate": 0.2,
    }
    base.update(overrides)
    return base


def etf_map(*codes: str, **overrides) -> dict[str, list[dict]]:
    return {code: [etf(f"51{idx:04d}", **overrides)] for idx, code in enumerate(codes)}


def test_regime_tier_mapping_covers_all_five_states() -> None:
    assert aggregate_regime("trend_up") == "attack"
    assert aggregate_regime("repair_up") == "range"
    assert aggregate_regime("bottoming") == "defense"
    assert aggregate_regime("pull_down") == "defense"
    assert aggregate_regime("risk_off") == "defense"


def test_tier_caps_are_fixed() -> None:
    assert tier_cap("attack") == 80
    assert tier_cap("range") == 40
    assert tier_cap("defense") == 10


def test_per_sector_cap_uses_5pct_floor() -> None:
    assert compute_per_sector_cap(80) == 20   # min(20, 32) -> 20
    assert compute_per_sector_cap(40) == 15   # min(20, 16) -> 15
    assert compute_per_sector_cap(10) == 0    # min(20, 4)  -> 0
    assert compute_per_sector_cap(0) == 0


def test_rank_pool_top8_is_deterministic_on_ties() -> None:
    rows = [
        cand("A", 8.0, technical=8.2, sentiment=7.8),
        cand("B", 8.0, technical=8.2, sentiment=7.9),
        cand("C", 8.0, technical=8.1, sentiment=7.9),
        cand("D", 8.0, technical=8.2, sentiment=7.9),
    ]
    pool = rank_pool(rows)
    # 综合分相同 → 技术分降序 → 情绪分降序 → 代码字典序
    assert [c["sector_code"] for c in pool] == ["B", "D", "A", "C"]

    many = [cand(f"S{i}", 9.0 - i * 0.1) for i in range(12)]
    assert len(rank_pool(many)) == 8
    assert [c["sector_code"] for c in rank_pool(many)][:3] == ["S0", "S1", "S2"]


def test_build_decision_outputs_top3_without_backfill() -> None:
    rows = [cand(f"S{i}", 9.0 - i * 0.2) for i in range(6)]
    decision = build_decision(
        regime_state="trend_up",
        temp_score=74,
        candidates=rows,
        etf_map=etf_map(*[r["sector_code"] for r in rows]),
        correlations={},
        prev_rank_pool={r["sector_code"] for r in rows},
    )

    assert decision["regime_tier"] == "attack"
    assert decision["market_cap"] == 80
    assert decision["exec_cap"] == 80
    assert decision["per_sector_cap"] == 20
    assert [c["sector_code"] for c in decision["output"]] == ["S0", "S1", "S2"]
    cutoff = [r for r in decision["rejected"] if r["reason_code"] == "TOP_N_CUTOFF"]
    assert len(cutoff) == 3


def test_absolute_gate_boundaries() -> None:
    rows = [
        cand("OK", 6.5, technical=6.0, sentiment=5.5),
        cand("LOW", 6.4, technical=7.0, sentiment=7.0),
        cand("TECH", 7.0, technical=5.9, sentiment=7.0),
        cand("SENTI", 7.0, technical=7.0, sentiment=5.4),
    ]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=etf_map("OK", "LOW", "TECH", "SENTI"),
        correlations={}, prev_rank_pool={"OK", "LOW", "TECH", "SENTI"},
    )
    reasons = {r["sector_code"]: r["reason_code"] for r in decision["rejected"]}
    assert [c["sector_code"] for c in decision["output"]] == ["OK"]
    assert reasons["LOW"] == "ABSOLUTE_FAILED"
    assert reasons["TECH"] == "ABSOLUTE_FAILED"
    assert reasons["SENTI"] == "ABSOLUTE_FAILED"


def test_continuity_gate_requires_previous_trading_day() -> None:
    rows = [cand("A", 8.0), cand("B", 7.8)]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=etf_map("A", "B"), correlations={}, prev_rank_pool={"A"},
    )
    reasons = {r["sector_code"]: r["reason_code"] for r in decision["rejected"]}
    assert reasons["B"] == "CONTINUITY_FAILED"


def test_breadth_gate_requires_55pct_and_positive_flow() -> None:
    rows = [
        cand("A", 8.0, up_count=11, down_count=9, main_net_inflow=1.0),
        cand("B", 7.9, up_count=10, down_count=10, main_net_inflow=1.0),
        cand("C", 7.8, up_count=12, down_count=8, main_net_inflow=-1.0),
    ]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=etf_map("A", "B", "C"), correlations={},
        prev_rank_pool={"A", "B", "C"},
    )
    reasons = {r["sector_code"]: r["reason_code"] for r in decision["rejected"]}
    assert [c["sector_code"] for c in decision["output"]] == ["A"]
    assert reasons["B"] == "BREADTH_FAILED"
    assert reasons["C"] == "MONEYFLOW_NEGATIVE"


def test_correlation_gate_keeps_higher_composite() -> None:
    rows = [cand("A", 8.0), cand("B", 7.9), cand("C", 7.8)]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=etf_map("A", "B", "C"),
        correlations={("A", "B"): 0.86, ("A", "C"): 0.2, ("B", "C"): 0.2},
        prev_rank_pool={"A", "B", "C"},
    )
    reasons = {r["sector_code"]: r["reason_code"] for r in decision["rejected"]}
    assert [c["sector_code"] for c in decision["output"]] == ["A", "C"]
    assert reasons["B"] == "CORRELATION_DUPLICATE"


def test_correlation_boundary_085_is_allowed() -> None:
    rows = [cand("A", 8.0), cand("B", 7.9)]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=etf_map("A", "B"),
        correlations={("A", "B"): 0.85},
        prev_rank_pool={"A", "B"},
    )
    assert [c["sector_code"] for c in decision["output"]] == ["A", "B"]


def test_etf_gate_boundaries_and_reasons() -> None:
    rows = [cand(code, 8.0) for code in ("NONE", "SUSP", "SMALL", "ILLIQ", "PREM", "OK")]
    mapping = {
        "NONE": [],
        "SUSP": [etf("510001", suspended=True)],
        "SMALL": [etf("510002", aum=1.99e8)],
        "ILLIQ": [etf("510003", adv20=2.99e7)],
        "PREM": [etf("510004", premium_rate=1.51)],
        "OK": [etf("510005", aum=2.0e8, adv20=3.0e7, premium_rate=1.5)],
    }
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=mapping, correlations={}, prev_rank_pool={r["sector_code"] for r in rows},
    )
    reasons = {r["sector_code"]: r["reason_code"] for r in decision["rejected"]}
    assert reasons["NONE"] == "NO_TRADABLE_ETF"
    assert reasons["SUSP"] == "ETF_SUSPENDED"
    assert reasons["SMALL"] == "ETF_TOO_SMALL"
    assert reasons["ILLIQ"] == "ETF_ILLIQUID"
    assert reasons["PREM"] == "ETF_PREMIUM_TOO_HIGH"
    assert [c["sector_code"] for c in decision["output"]] == ["OK"]


def test_multiple_primary_etf_is_rejected() -> None:
    rows = [cand("A", 8.0)]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map={"A": [etf("510001"), etf("510002")]},
        correlations={}, prev_rank_pool={"A"},
    )
    assert decision["rejected"][0]["reason_code"] == "MULTIPLE_PRIMARY_ETF"
    assert decision["output"] == []


def test_defense_with_empty_pool_sets_exec_cap_zero() -> None:
    decision = build_decision(
        regime_state="risk_off", temp_score=24, candidates=[],
        etf_map={}, correlations={}, prev_rank_pool=set(),
    )
    assert decision["regime_tier"] == "defense"
    assert decision["market_cap"] == 10
    assert decision["exec_cap"] == 0
    assert decision["per_sector_cap"] == 0
    assert decision["empty_reasons"][0]["code"] == "REGIME_DEFENSE"


def test_empty_reasons_include_absolute_and_crowding_in_priority_order() -> None:
    rows = [
        cand("A", 6.2, sentiment_percentile=94, main_net_inflow=-1.0),
        cand("B", 6.1, sentiment_percentile=93, main_net_inflow=-1.0),
        cand("C", 6.0, sentiment_percentile=92, main_net_inflow=-1.0),
    ]
    decision = build_decision(
        regime_state="pull_down", temp_score=28, candidates=rows,
        etf_map=etf_map("A", "B", "C"), correlations={},
        prev_rank_pool={"A", "B", "C"},
    )
    codes = [r["code"] for r in decision["empty_reasons"]]
    assert codes[:3] == ["REGIME_DEFENSE", "NO_ABSOLUTE_PASS", "CROWDING_REVERSAL"]
    assert decision["exec_cap"] == 0


def test_core_data_missing_excludes_candidate_from_pool() -> None:
    rows = [cand("BAD", 9.5, data_complete=False), cand("OK", 8.0)]
    decision = build_decision(
        regime_state="repair_up", temp_score=56, candidates=rows,
        etf_map=etf_map("BAD", "OK"), correlations={},
        prev_rank_pool={"BAD", "OK"},
    )
    assert [c["sector_code"] for c in decision["output"]] == ["OK"]
    assert any(r["sector_code"] == "BAD" and r["reason_code"] == "DATA_INCOMPLETE"
               for r in decision["rejected"])
