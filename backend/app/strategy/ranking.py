"""策略排序、打分与信号输出。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
from typing import Any, Sequence
import pandas as pd
from app.engine.stock_score import FACTOR_WEIGHTS
from app.strategy import condition as cond_dsl

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403
from app.strategy.conditions import *  # noqa: F401,F403

def _sort_candidates(algo_code: str, df: pd.DataFrame) -> pd.DataFrame:
    """按策略排序口径排序（同值时按 code 升序，保证结果确定性）。"""
    spec = get_spec(algo_code)
    field_name, ascending = _SORT_SPEC[spec.code]
    frame = df.copy()
    frame["_sort_value"] = _numeric_column(frame, field_name)
    frame = frame[frame["_sort_value"].notna()]
    if "code" in frame.columns:
        frame = frame.sort_values(
            ["_sort_value", "code"],
            ascending=[ascending, True],
            kind="mergesort",
        )
    else:
        frame = frame.sort_values("_sort_value", ascending=ascending, kind="mergesort")
    return frame.drop(columns=["_sort_value"])

def _score_series(algo_code: str, df: pd.DataFrame) -> pd.Series:
    """`score` = 本策略排序指标的原始值（A3=综合评分，A4=股息率，C*=各自排序指标）。"""
    spec = get_spec(algo_code).code
    field_name, _ = _SORT_SPEC[spec]
    return _numeric_column(df, field_name).astype("float64").round(4)

#: 综合评分分项的中文名与原始因子列（权重/分位以 fact_stock_score_daily.detail 为准）
_FACTOR_META: dict[str, tuple[str, tuple[str, ...]]] = {
    "value": ("价值", ("pe_ttm", "pb", "ps_ttm")),
    "quality": ("质量", ("pb", "dv_ttm")),
    "momentum": ("动量", ("momentum_20", "momentum_60")),
    "volatility": ("波动", ("volatility_20",)),
    "liquidity": ("流动性", ("amount_ma20", "amount")),
    "sentiment": ("情绪", ()),
}

def _score_breakdown(record: dict) -> dict | None:
    """A3 分项贡献：直接读 fact_stock_score_daily.detail，不臆造权重。"""
    detail = record.get("detail")
    if not isinstance(detail, dict):
        return None
    percentiles = detail.get("percentile") or {}
    weights = detail.get("weights") or {}
    raw = detail.get("raw") or {}

    parts: list[dict[str, Any]] = []
    weight_sum = 0.0
    for key in FACTOR_WEIGHTS:
        name, raw_keys = _FACTOR_META.get(key, (key, ()))
        score = record.get(f"{key}_score")
        weight = weights.get(key)
        if score is None or weight is None:
            continue
        z = percentiles.get(key)
        if z is not None:
            weight_sum += float(weight)
        parts.append(
            {
                "key": key,
                "name": name,
                "score": jsonable(score),
                "z": jsonable(z),
                "weight": jsonable(weight),
                "raw": {name_key: jsonable(raw.get(name_key)) for name_key in raw_keys},
            }
        )
    if not parts:
        return None

    # 与 composite_factor 同口径：仅对当日有分位的因子按可用权重归一
    for part in parts:
        if part["z"] is None or weight_sum <= 0:
            part["contribution"] = None
            continue
        part["contribution"] = round(
            float(part["score"]) * float(part["weight"]) / weight_sum, 4
        )

    return {
        "composite": jsonable(record.get("composite_score")),
        "weight_sum": round(weight_sum, 4),
        "parts": parts,
        "formula": (
            "综合分 = 10 × Σ(分位 × 权重) / Σ(当日可用权重)；"
            "贡献 = 分项得分 × 权重 / Σ(当日可用权重)，逐项贡献之和 ≈ 综合分（±0.01）"
        ),
        "sentiment_source": detail.get("sentiment_source"),
    }

def _metrics_for_row(
    algo_code: str,
    record: dict,
    *,
    triggered: list[dict] | None = None,
    breakdown: dict | None = None,
) -> dict:
    """构造单条信号的 metrics（该策略依据的全部指标 + 前端展示字段）。"""
    spec = get_spec(algo_code)
    metrics: dict[str, Any] = {}
    for field_name in _METRIC_FIELDS[spec.code]:
        metrics[field_name] = jsonable(record.get(field_name))
    # 通用展示字段（契约 5.2 data[] 顶层列由 metrics 派生，保证缓存命中前后结构一致）
    for field_name in DISPLAY_FIELDS:
        if field_name in metrics:
            continue
        if field_name in record:
            metrics[field_name] = jsonable(record.get(field_name))
    if triggered is not None:
        metrics["triggered"] = jsonable(triggered)
    if breakdown is not None:
        metrics["score_breakdown"] = jsonable(breakdown)
    return metrics

def _empty_signal_frame() -> pd.DataFrame:
    """空信号表（列齐全，便于 API 直接序列化成空数组）。"""
    return pd.DataFrame(
        columns=["code", "name", "rank", "score", "metrics", *DISPLAY_FIELDS]
    )

def rank_signals(algo_code: str, df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """按策略口径过滤 + 排序 + 截断，生成 rank/score/metrics。

    返回列：``code, name, rank, score, metrics`` + 契约 5.2 的通用展示列。
    **纯函数**：不访问数据库，便于单测与回测复用。
    """
    spec = get_spec(algo_code)
    normalized = _validate_params(spec.code, params)

    if df is None or len(df) == 0:
        return _empty_signal_frame()

    # Decimal（asyncpg Numeric）→ float64，禁止 Decimal 与 float 直接比较
    working = coerce_numeric_frame(df)
    evaluation = _evaluate_strategy(spec.code, working, normalized)
    if not evaluation.mask.any():
        return _empty_signal_frame()
    filtered = evaluation.frame.loc[evaluation.mask].copy()
    filtered.attrs.update(dict(evaluation.frame.attrs))

    ordered = _sort_candidates(spec.code, filtered)
    ordered = ordered.copy()
    ordered["score"] = _score_series(spec.code, ordered).reindex(ordered.index)

    limit = int(normalized["limit"])
    top = ordered.head(limit).copy()
    top["rank"] = list(range(1, len(top) + 1))
    metrics_rows: list[dict] = []
    for index, record in zip(top.index, top.to_dict("records")):
        triggered = (
            cond_dsl.triggered_row(evaluation.details, evaluation.frame, index)
            if evaluation.details
            else None
        )
        breakdown = _score_breakdown(record) if spec.code == "a3" else None
        metrics_rows.append(
            _metrics_for_row(
                spec.code, record, triggered=triggered, breakdown=breakdown
            )
        )
    top["metrics"] = metrics_rows
    top["code"] = top["code"].astype(str).str.zfill(6)
    if "name" not in top.columns:
        top["name"] = None

    columns = ["code", "name", "rank", "score", "metrics"]
    columns += [item for item in DISPLAY_FIELDS if item in top.columns]
    # 排序依据字段也带出来（便于测试/排查，前端不依赖它）
    sort_field, _ = _SORT_SPEC[spec.code]
    if sort_field in top.columns and sort_field not in columns:
        columns.append(sort_field)
    return top.loc[:, columns].reset_index(drop=True)

__all__ = [
    "_sort_candidates",
    "_score_series",
    "_FACTOR_META",
    "_score_breakdown",
    "_metrics_for_row",
    "_empty_signal_frame",
    "rank_signals",
]
