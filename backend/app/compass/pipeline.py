"""决策流水线：五态聚合、初筛、五道门槛、预算与空仓原因。

纯函数，不访问数据库，便于单测与复用。
输入字典字段见 DRD 4.3；所有金额单位为元，百分比字段为数值（40 表示 40%）。
"""
from __future__ import annotations

import math
from typing import Any

from app.compass.constants import (
    CAP_RATIO,
    CAP_STEP,
    CORRELATION_LIMIT,
    CROWDING_HOT,
    EMPTY_REASON_PRIORITY,
    ETF_MAX_PREMIUM,
    ETF_MIN_ADV20,
    ETF_MIN_AUM,
    MAX_PER_SECTOR,
    MIN_BREADTH,
    MIN_COMPOSITE,
    MIN_MEMBERS,
    MIN_SENTIMENT,
    MIN_TECHNICAL,
    OUTPUT_LIMIT,
    RANK_POOL_SIZE,
    REGIME_TIER,
    RULE_VERSION,
    TIER_CAP,
)

_NUMERIC_KEYS = (
    "composite", "technical", "sentiment", "news",
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def aggregate_regime(state: str) -> str:
    """五态 → 三档。未知状态直接抛错，由服务层转 DATA_NOT_READY。"""
    tier = REGIME_TIER.get(str(state or "").lower())
    if tier is None:
        raise ValueError(f"unknown regime state: {state!r}")
    return tier


def tier_cap(tier: str) -> int:
    if tier not in TIER_CAP:
        raise ValueError(f"unknown tier: {tier!r}")
    return TIER_CAP[tier]


def compute_per_sector_cap(exec_cap: int) -> int:
    """单板块上限 = floor(min(20%, 总预算×40%) / 5%) × 5%。"""
    raw = min(float(MAX_PER_SECTOR), float(exec_cap) * CAP_RATIO)
    if raw <= 0:
        return 0
    # +1e-9 抵消二进制浮点误差，保证 16.0 不会被算成 15.999…
    return int(math.floor(raw / CAP_STEP + 1e-9) * CAP_STEP)


def rank_pool(candidates: list[dict], top_n: int = RANK_POOL_SIZE) -> list[dict]:
    """按综合分/技术分/情绪分/代码字典序取前 N，保证确定性。"""
    ordered = sorted(
        candidates,
        key=lambda c: (
            -_f(c.get("composite")),
            -_f(c.get("technical")),
            -_f(c.get("sentiment")),
            str(c.get("sector_code") or ""),
        ),
    )
    return ordered[:top_n]


def _corr(correlations: dict, a: str, b: str) -> float | None:
    value = correlations.get((a, b))
    if value is None:
        value = correlations.get((b, a))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ak(cand: dict) -> bool:
    return (
        _f(cand.get("composite")) >= MIN_COMPOSITE
        and _f(cand.get("technical")) >= MIN_TECHNICAL
        and _f(cand.get("sentiment")) >= MIN_SENTIMENT
    )


def _etf_result(cand: dict, etf_map: dict[str, list[dict]]) -> tuple[bool, str | None, str | None]:
    code = str(cand.get("sector_code") or "")
    rows = [e for e in (etf_map.get(code) or []) if e.get("is_primary", True)]
    if not rows:
        return False, "NO_TRADABLE_ETF", "无主映射 ETF"
    if len(rows) > 1:
        return False, "MULTIPLE_PRIMARY_ETF", "主映射不唯一，需人工修复"
    e = rows[0]
    etf_code = str(e.get("etf_code") or "")
    if not e.get("listed", True):
        return False, "NO_TRADABLE_ETF", f"{etf_code} 未上市"
    if e.get("suspended"):
        return False, "ETF_SUSPENDED", f"{etf_code} 停牌"
    if _f(e.get("aum")) < ETF_MIN_AUM:
        return False, "ETF_TOO_SMALL", f"{etf_code} 规模不足 2 亿"
    if _f(e.get("adv20")) < ETF_MIN_ADV20:
        return False, "ETF_ILLIQUID", f"{etf_code} 日均成交不足 3000 万"
    if abs(_f(e.get("premium_rate"))) > ETF_MAX_PREMIUM:
        return False, "ETF_PREMIUM_TOO_HIGH", f"{etf_code} 绝对溢价超过 1.5%"
    return True, None, None


def evaluate_candidate(cand: dict, ctx: dict) -> dict:
    """按 G0 数据完整性 → G1 绝对 → G2 连续 → G3 广度 → G4 相关 → G5 ETF。"""
    code = str(cand.get("sector_code") or "")
    name = str(cand.get("sector_name") or code)

    def fail(reason: str, text: str) -> dict:
        return {"passed": False, "reason_code": reason, "reason_text": text}

    if not cand.get("data_complete", True):
        return fail("DATA_INCOMPLETE", "技术分或情绪分缺失")
    if cand.get("is_partial"):
        return fail("PARTIAL_MEMBERSHIP", "成分名单不完整")
    if _f(cand.get("member_count")) < MIN_MEMBERS:
        return fail("INSUFFICIENT_MEMBERS", f"有效成分少于 {MIN_MEMBERS} 只")

    if _f(cand.get("composite")) < MIN_COMPOSITE:
        return fail("ABSOLUTE_FAILED", f"综合分 {_f(cand.get('composite')):.1f} < {MIN_COMPOSITE}")
    if _f(cand.get("technical")) < MIN_TECHNICAL:
        return fail("ABSOLUTE_FAILED", f"技术分 {_f(cand.get('technical')):.1f} < {MIN_TECHNICAL}")
    if _f(cand.get("sentiment")) < MIN_SENTIMENT:
        return fail("ABSOLUTE_FAILED", f"情绪分 {_f(cand.get('sentiment')):.1f} < {MIN_SENTIMENT}")

    prev = set(ctx.get("prev_rank_pool") or set())
    if code not in prev:
        return fail("CONTINUITY_FAILED", "连续性不足（上一交易日不在初筛池）")

    up = int(_f(cand.get("up_count")))
    down = int(_f(cand.get("down_count")))
    denominator = up + down
    if denominator < MIN_MEMBERS:
        return fail("BREADTH_FAILED", "有效涨跌家数不足，无法判断广度")
    ratio = up / denominator
    if ratio < MIN_BREADTH:
        return fail("BREADTH_FAILED", f"上涨占比 {ratio:.0%} < 55%")

    flow = cand.get("main_net_inflow")
    if flow is None:
        return fail("MONEYFLOW_MISSING", "板块主力资金流缺失")
    if _f(flow) <= 0:
        return fail("MONEYFLOW_NEGATIVE", "板块主力净流入为负")

    correlations = ctx.get("correlations") or {}
    for accepted in ctx.get("accepted") or []:
        other = str(accepted.get("sector_code") or "")
        if other == code:
            continue
        value = _corr(correlations, code, other)
        if value is not None and value > CORRELATION_LIMIT:
            return fail("CORRELATION_DUPLICATE", f"与 {other} 相关系数 {value:.2f} > 0.85")

    passed, reason, text = _etf_result(cand, ctx.get("etf_map") or {})
    if not passed:
        return fail(reason or "NO_TRADABLE_ETF", text or "ETF 不可执行")

    return {"passed": True, "reason_code": None, "reason_text": None}


def _rejection(cand: dict, reason_code: str, reason_text: str) -> dict:
    return {
        "sector_code": cand.get("sector_code"),
        "sector_name": cand.get("sector_name"),
        "composite": round(_f(cand.get("composite")), 1),
        "reason_code": reason_code,
        "reason_text": reason_text,
    }


def _empty_reasons(
    tier: str,
    pool: list[dict],
    rejected: list[dict],
) -> list[dict]:
    reasons: list[dict] = []

    if tier == "defense":
        reasons.append({"code": "REGIME_DEFENSE", "text": "市场状态为防守，预算上限仅 10%"})

    if not any(_ak(c) for c in pool):
        reasons.append({"code": "NO_ABSOLUTE_PASS", "text": "0 个板块通过绝对门槛"})

    crowding = [
        c for c in pool
        if c.get("sentiment_percentile") is not None
        and _f(c.get("sentiment_percentile")) >= CROWDING_HOT
        and c.get("main_net_inflow") is not None
        and _f(c.get("main_net_inflow")) < 0
    ]
    if len(crowding) >= 3:
        reasons.append({
            "code": "CROWDING_REVERSAL",
            "text": f"{len(crowding)} 个板块情绪分位 > 90 且资金流转负",
            "count": len(crowding),
        })

    breadth_broken = [
        c for c in pool
        if c.get("breadth_below_ma20") is not None
        and _f(c.get("breadth_below_ma20")) > 0.60
    ]
    if breadth_broken:
        reasons.append({
            "code": "BREADTH_BREAKDOWN",
            "text": "板块内跌破 20 日线成分占比超过 60%",
        })

    if pool and all(r["reason_code"] == "NO_TRADABLE_ETF" for r in rejected):
        reasons.append({"code": "NO_TRADABLE_ETF", "text": "通过门槛的板块无合格 ETF"})

    if not pool:
        reasons.append({"code": "DATA_INCOMPLETE", "text": "核心数据缺失，无法形成结论"})

    reasons.sort(key=lambda r: EMPTY_REASON_PRIORITY.get(r["code"], 99))
    return reasons


def build_decision(
    regime_state: str,
    temp_score: float | None,
    candidates: list[dict],
    etf_map: dict[str, list[dict]],
    correlations: dict,
    prev_rank_pool: set[str] | list[str],
    *,
    rule_version: str = RULE_VERSION,
) -> dict:
    """生成一份可落库的决策结果（不含数据库读写）。"""
    tier = aggregate_regime(regime_state)
    market_cap = tier_cap(tier)
    pool = rank_pool(candidates)
    prev = set(prev_rank_pool)

    accepted: list[dict] = []
    rejected: list[dict] = []

    for cand in pool:
        result = evaluate_candidate(cand, {
            "prev_rank_pool": prev,
            "accepted": accepted,
            "correlations": correlations,
            "etf_map": etf_map,
        })
        if result["passed"]:
            accepted.append(cand)
        else:
            rejected.append(_rejection(cand, result["reason_code"], result["reason_text"]))

    output = accepted[:OUTPUT_LIMIT]
    for cand in accepted[OUTPUT_LIMIT:]:
        rejected.append(_rejection(
            cand, "TOP_N_CUTOFF",
            f"通过门槛，但综合分排名第 {accepted.index(cand) + 1}，未进入 TOP3",
        ))

    exec_cap = market_cap if output else 0
    per_sector_cap = compute_per_sector_cap(exec_cap)
    empty_reasons = [] if output else _empty_reasons(tier, pool, rejected)

    return {
        "rule_version": rule_version,
        "regime_state": regime_state,
        "regime_tier": tier,
        "temp_score": temp_score,
        "market_cap": market_cap,
        "exec_cap": exec_cap,
        "per_sector_cap": per_sector_cap,
        "initial_pool_size": len(pool),
        "evaluated_count": len(candidates),
        "qualified_count": len(accepted),
        "output": output,
        "rejected": rejected,
        "empty_reasons": empty_reasons,
    }
