"""六张情景卡求值与动作优先级。

纯函数：输入当日快照，输出每张卡的状态与证据；不读写数据库。
"""
from __future__ import annotations

from typing import Any

from app.compass.constants import ACTION_PRIORITY, SCENARIO_TITLES

SCENARIO_DEFS: dict[str, dict] = {
    "SC-01": {
        "title": SCENARIO_TITLES["SC-01"],
        "condition": "IF 市场状态 ∈ {pull_down, risk_off} AND 候选池为空",
        "action": "执行仓位归零；只保留观察，不新开仓，不补仓。",
        "invalid": "市场状态回到 repair_up 以上，且至少 1 个板块通过五道门槛。",
        "basis": "2021-02、2022-04、2024-01 三次防守区间，空仓期沪深 300 中位回撤 -8.6%。",
    },
    "SC-02": {
        "title": SCENARIO_TITLES["SC-02"],
        "condition": "IF 候选池 ≥ 2 个",
        "action": "单个板块不超过 min(20%, 总预算 × 40%)，按 5% 步长向下取整。",
        "invalid": "候选池只剩 1 个板块时，该上限不变。",
        "basis": "2025-03 至 2026-11 纸面组合，单板块超配是回撤扩大的第一归因。",
    },
    "SC-03": {
        "title": SCENARIO_TITLES["SC-03"],
        "condition": "IF 情绪分位 ≥ 90 AND 主力净流转负",
        "action": "停止加仓，不新开仓；已有仓位不因情绪单独卖出。",
        "invalid": "情绪分位回落至 80 以下，且资金流转正 2 日。",
        "basis": "2026-08-14 半导体分位 93 仍加仓，5 日回撤 -6.4%。",
    },
    "SC-04": {
        "title": SCENARIO_TITLES["SC-04"],
        "condition": "IF T+1 开盘偏离绝对值 > 2% 或 ETF 停牌",
        "action": "延迟至 10:00 复核；仍超阈值则放弃本次。",
        "invalid": "开盘偏离回到 2% 以内。",
        "basis": "2026-01 至 2026-11，开盘偏离 > 2% 的样本中，当日回归中位数 1.1%。",
    },
    "SC-05": {
        "title": SCENARIO_TITLES["SC-05"],
        "condition": "IF 板块指数收盘跌破 20 日线 OR 60% 成分股跌破",
        "action": "清仓该方向，不摊平；资金回到预算池。",
        "invalid": "指数重新站上 20 日线，且广度恢复 55% 以上。",
        "basis": "2024-01、2025-09、2026-10 三次退潮，20 日线规则平均少亏 4.2%。",
    },
    "SC-06": {
        "title": SCENARIO_TITLES["SC-06"],
        "condition": "IF 滚动 60 日超额转负",
        "action": "全系统降级为“仅供参考”，暂停情景卡推送，公开失效报告。",
        "invalid": "连续 20 日超额回正，且通过样本外复核。",
        "basis": "产品的自我否决条款：信号失效时，先停用，不解释。",
    },
}

_TITLES = SCENARIO_TITLES


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _card(sid: str, status: str, evidence: dict | None = None) -> dict:
    return {
        "id": sid,
        **SCENARIO_DEFS[sid],
        "status": status,
        "evidence": evidence or {},
    }


def evaluate_scenarios(ctx: dict) -> list[dict]:
    """按 DRD 4.4 求值。状态：TRIGGERED / ARMED / STANDING。"""
    regime_state = str(ctx.get("regime_state") or "")
    tier = str(ctx.get("regime_tier") or "")
    output_count = int(ctx.get("output_count") or 0)
    candidates = ctx.get("candidates") or []
    checks = ctx.get("checks") or []
    breadth_below_ma20 = ctx.get("breadth_below_ma20")
    rolling_excess = ctx.get("rolling_excess_60d")

    # SC-01 空仓优先
    sc01 = _card(
        "SC-01",
        "TRIGGERED" if regime_state in {"pull_down", "risk_off"} and output_count == 0 else "ARMED",
        {"regime_state": regime_state, "output_count": output_count},
    )

    # SC-02 单板块上限：长期生效
    sc02 = _card("SC-02", "STANDING", {"formula": "min(20%, 总预算×40%)，按 5% 步长向下取整"})

    # SC-03 停止加仓
    hot = [
        c for c in candidates
        if c.get("sentiment_percentile") is not None
        and _f(c.get("sentiment_percentile")) >= 90
        and c.get("main_net_inflow") is not None
        and _f(c.get("main_net_inflow")) < 0
    ]
    sc03 = _card(
        "SC-03",
        "TRIGGERED" if hot else "ARMED",
        {"sector_code": hot[0].get("sector_code")} if hot else {},
    )

    # SC-04 延迟执行
    risk_checks = [c for c in checks if c.get("result") in {"WARN", "SUSPENDED"}]
    sc04 = _card(
        "SC-04",
        "TRIGGERED" if risk_checks else "ARMED",
        {"checks": risk_checks[:3]} if risk_checks else {},
    )

    # SC-05 退出
    below_ma = [
        c for c in candidates
        if c.get("close") is not None and c.get("ma20") is not None
        and _f(c.get("close")) < _f(c.get("ma20"))
    ]
    breadth_candidates = [
        c for c in candidates
        if c.get("breadth_below_ma20") is not None
        and _f(c.get("breadth_below_ma20")) > 0.60
    ]
    breadth_broken = (
        (breadth_below_ma20 is not None and _f(breadth_below_ma20) > 0.60)
        or bool(breadth_candidates)
    )
    if below_ma:
        sc05 = _card("SC-05", "TRIGGERED", {
            "reason": "CLOSE_BELOW_MA20",
            "sector_code": below_ma[0].get("sector_code"),
        })
    elif breadth_broken:
        ratio = (
            breadth_below_ma20
            if breadth_below_ma20 is not None
            else breadth_candidates[0].get("breadth_below_ma20")
        )
        evidence = {
            "reason": "BREADTH_BREAKDOWN",
            "breadth_below_ma20": round(_f(ratio), 4),
        }
        if breadth_candidates:
            evidence["sector_code"] = breadth_candidates[0].get("sector_code")
        sc05 = _card("SC-05", "TRIGGERED", evidence)
    else:
        sc05 = _card("SC-05", "ARMED")

    # SC-06 失效降级
    sc06 = _card(
        "SC-06",
        "TRIGGERED" if rolling_excess is not None and _f(rolling_excess) < 0 else "ARMED",
        {"rolling_excess_60d": rolling_excess} if rolling_excess is not None else {},
    )

    return [sc01, sc02, sc03, sc04, sc05, sc06]


def resolve_action_priority(scenarios: list[dict]) -> str | None:
    """返回当前应执行的动作卡 ID；无触发返回 None。SC-02 是约束不是动作。"""
    triggered = {s.get("id") for s in scenarios if s.get("status") == "TRIGGERED"}
    for sid in ACTION_PRIORITY:
        if sid in triggered:
            return sid
    return None
