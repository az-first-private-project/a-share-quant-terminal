"""仓位罗盘 · 情景卡求值单元测试（先于实现）。"""
from __future__ import annotations

from app.compass.scenarios import evaluate_scenarios, resolve_action_priority


def base_ctx(**overrides) -> dict:
    ctx = {
        "regime_state": "repair_up",
        "regime_tier": "range",
        "output_count": 2,
        "candidates": [],
        "checks": [],
        "rolling_excess_60d": 0.02,
        "breadth_below_ma20": 0.2,
    }
    ctx.update(overrides)
    return ctx


def by_id(rows: list[dict]) -> dict[str, dict]:
    return {row["id"]: row for row in rows}


def test_sc01_triggers_on_defense_and_empty_pool() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(
        regime_state="risk_off", regime_tier="defense", output_count=0,
    )))
    assert rows["SC-01"]["status"] == "TRIGGERED"
    assert rows["SC-01"]["evidence"]["regime_state"] == "risk_off"


def test_sc01_not_triggered_when_pool_has_output() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(
        regime_state="pull_down", regime_tier="defense", output_count=1,
    )))
    assert rows["SC-01"]["status"] == "ARMED"


def test_sc02_is_standing_constraint() -> None:
    rows = by_id(evaluate_scenarios(base_ctx()))
    assert rows["SC-02"]["status"] == "STANDING"


def test_sc03_triggers_at_percentile_90_with_negative_flow() -> None:
    hot = {"sector_code": "A", "sentiment_percentile": 90, "main_net_inflow": -1.0}
    rows = by_id(evaluate_scenarios(base_ctx(candidates=[hot])))
    assert rows["SC-03"]["status"] == "TRIGGERED"
    assert rows["SC-03"]["evidence"]["sector_code"] == "A"


def test_sc03_not_triggered_at_89_or_unknown_percentile() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(candidates=[
        {"sector_code": "A", "sentiment_percentile": 89, "main_net_inflow": -1.0},
        {"sector_code": "B", "sentiment_percentile": None, "main_net_inflow": -1.0},
    ])))
    assert rows["SC-03"]["status"] == "ARMED"


def test_sc04_triggers_when_any_check_warns_or_suspends() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(checks=[{"etf_code": "512480", "result": "WARN"}])))
    assert rows["SC-04"]["status"] == "TRIGGERED"

    rows = by_id(evaluate_scenarios(base_ctx(checks=[{"etf_code": "512480", "result": "SUSPENDED"}])))
    assert rows["SC-04"]["status"] == "TRIGGERED"


def test_sc05_triggers_on_ma20_break_or_breadth_breakdown() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(candidates=[
        {"sector_code": "A", "close": 99.0, "ma20": 100.0},
    ])))
    assert rows["SC-05"]["status"] == "TRIGGERED"
    assert rows["SC-05"]["evidence"]["reason"] == "CLOSE_BELOW_MA20"

    rows = by_id(evaluate_scenarios(base_ctx(breadth_below_ma20=0.61)))
    assert rows["SC-05"]["status"] == "TRIGGERED"
    assert rows["SC-05"]["evidence"]["reason"] == "BREADTH_BREAKDOWN"


def test_sc05_triggers_on_candidate_level_breadth() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(candidates=[
        {"sector_code": "A", "breadth_below_ma20": 0.61},
    ])))
    assert rows["SC-05"]["status"] == "TRIGGERED"
    assert rows["SC-05"]["evidence"]["reason"] == "BREADTH_BREAKDOWN"
    assert rows["SC-05"]["evidence"]["sector_code"] == "A"


def test_sc06_triggers_on_negative_rolling_excess() -> None:
    rows = by_id(evaluate_scenarios(base_ctx(rolling_excess_60d=-0.01)))
    assert rows["SC-06"]["status"] == "TRIGGERED"


def test_action_priority_prefers_system_then_empty_then_exit() -> None:
    scenarios = [
        {"id": "SC-03", "status": "TRIGGERED"},
        {"id": "SC-05", "status": "TRIGGERED"},
        {"id": "SC-01", "status": "TRIGGERED"},
        {"id": "SC-06", "status": "TRIGGERED"},
        {"id": "SC-04", "status": "TRIGGERED"},
        {"id": "SC-02", "status": "STANDING"},
    ]
    assert resolve_action_priority(scenarios) == "SC-06"

    scenarios = [s for s in scenarios if s["id"] != "SC-06"]
    assert resolve_action_priority(scenarios) == "SC-01"

    scenarios = [s for s in scenarios if s["id"] != "SC-01"]
    assert resolve_action_priority(scenarios) == "SC-05"
