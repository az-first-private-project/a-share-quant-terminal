"""策略条件树、策略求值与逐条依据。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
import logging

from dataclasses import dataclass
from typing import Any, Sequence
import pandas as pd
from app.core.errors import DataUnavailableError, InvalidParamError, UnknownAlgoError
from app.strategy import condition as cond_dsl

logger = logging.getLogger(__name__)

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403
from app.strategy.market import *  # noqa: F401,F403

def _cond(
    field_name: str,
    op: str,
    value: Any = None,
    *,
    key: str,
    label: str,
    detail_fields: Sequence[str] = (),
) -> cond_dsl.Condition:
    """构造带解释元数据的叶子条件。"""
    return cond_dsl.Condition(
        field=field_name,
        op=op,
        value=value,
        key=key,
        label=label,
        detail_fields=tuple(detail_fields),
    )

def _build_condition_specs(algo_code: str, params: dict) -> cond_dsl.ConditionGroup:
    """按参数构造 B 系列条件树（语义逐字对照旧 strategy_mask）。"""
    p = dict(params)
    if algo_code == "b1":
        ratio = float(p["min_vol_ratio"])
        min_amount = float(p["min_amount"])
        children = [
            _cond("ema_cross_3d", "eq", True, key="ema_cross_3d",
                  label="EMA5 近 3 日上穿 EMA20", detail_fields=("ema5", "ema20")),
            _cond("close", "gt", cond_dsl.FieldRef("ema20"), key="close_above_ema20",
                  label="收盘站上 EMA20", detail_fields=("close", "ema20")),
            _cond("ema20_up", "eq", True, key="ema20_up",
                  label="EMA20 向上", detail_fields=("ema20",)),
            _cond("roc10", "gt", 0, key="roc10_positive",
                  label="10 日动量 ROC10 > 0", detail_fields=("roc10",)),
            _cond("vol_ma5", "notna", key="vol_ma5_ready",
                  label="5 日均量可用", detail_fields=("vol_ma5",)),
            _cond("volume", "ge", cond_dsl.FieldRef("vol_ma5", ratio), key="volume_expansion",
                  label=f"成交量 ≥ 5 日均量 × {ratio:g}（放量）",
                  detail_fields=("volume", "vol_ma5")),
            _cond("close", "gt", cond_dsl.FieldRef("ma60"), key="close_above_ma60",
                  label="收盘站上 MA60", detail_fields=("close", "ma60")),
            _cond("amount_ma20", "notna", key="amount_ma20_ready",
                  label="20 日均成交额可用", detail_fields=("amount_ma20",)),
            _cond("amount_ma20", "ge", min_amount, key="min_amount",
                  label=f"20 日均成交额 ≥ {min_amount / 1e8:g} 亿元",
                  detail_fields=("amount_ma20",)),
        ]
    elif algo_code == "b2":
        rsi_max = float(p["rsi_max"])
        trend_dev = float(p["trend_dev"])
        amp_min = float(p["amp_min"])
        min_amount = float(p["min_amount"])
        children = [
            _cond("rsi6", "lt", rsi_max, key="rsi6_oversold",
                  label=f"RSI6 < {rsi_max:g}", detail_fields=("rsi6",)),
            cond_dsl.ConditionGroup(
                logic="OR", key="bullish_bar", label="当日收阳或收盘不低于昨收",
                detail_fields=("open", "close", "pre_close"),
                children=[
                    _cond("close", "gt", cond_dsl.FieldRef("open"), key="close_above_open",
                          label="收盘 > 开盘（收阳）", detail_fields=("open", "close")),
                    _cond("close", "ge", cond_dsl.FieldRef("pre_close"), key="close_above_pre_close",
                          label="收盘 ≥ 昨收", detail_fields=("close", "pre_close")),
                ],
            ),
            _cond("ma120", "notna", key="ma120_ready",
                  label="MA120 可用", detail_fields=("ma120",)),
            _cond("close", "gt", cond_dsl.FieldRef("ma120", trend_dev), key="above_trend",
                  label=f"收盘 > MA120 × {trend_dev:g}", detail_fields=("close", "ma120")),
            _cond("amp60", "notna", key="amp60_ready",
                  label="60 日振幅可用", detail_fields=("amp60",)),
            _cond("amp60", "gt", amp_min, key="amp60_enough",
                  label=f"60 日振幅 > {amp_min:g}%", detail_fields=("amp60",)),
            _cond("amount_ma20", "notna", key="amount_ma20_ready",
                  label="20 日均成交额可用", detail_fields=("amount_ma20",)),
            _cond("amount_ma20", "ge", min_amount, key="min_amount",
                  label=f"20 日均成交额 ≥ {min_amount / 1e8:g} 亿元",
                  detail_fields=("amount_ma20",)),
        ]
    elif algo_code == "b3":
        j_max = float(p["j_max"])
        k_max = float(p["k_max"])
        min_amount = float(p["min_amount"])
        children = [
            _cond("kdj_j_min2", "lt", j_max, key="kdj_j_low",
                  label=f"今日或昨日 J < {j_max:g}", detail_fields=("kdj_j", "kdj_j_min2")),
            _cond("kdj_golden_cross", "eq", True, key="kdj_golden_cross",
                  label="K 上穿 D（KDJ 金叉）", detail_fields=("kdj_k", "kdj_d")),
            _cond("kdj_k", "lt", k_max, key="kdj_k_low",
                  label=f"K < {k_max:g}", detail_fields=("kdj_k",)),
            _cond("ema60", "notna", key="ema60_ready",
                  label="EMA60 可用", detail_fields=("ema60",)),
            _cond("close", "gt", cond_dsl.FieldRef("ema60"), key="close_above_ema60",
                  label="收盘 > EMA60", detail_fields=("close", "ema60")),
            _cond("close", "ge", cond_dsl.FieldRef("pre_close"), key="close_above_pre_close",
                  label="收盘 ≥ 昨收", detail_fields=("close", "pre_close")),
            _cond("amount_ma20", "notna", key="amount_ma20_ready",
                  label="20 日均成交额可用", detail_fields=("amount_ma20",)),
            _cond("amount_ma20", "ge", min_amount, key="min_amount",
                  label=f"20 日均成交额 ≥ {min_amount / 1e8:g} 亿元",
                  detail_fields=("amount_ma20",)),
        ]
    elif algo_code == "b4":
        squeeze_pct = float(p["squeeze_pct"])
        vol_mult = float(p["vol_mult"])
        range_max = float(p["range_max"])
        min_amount = float(p["min_amount"])
        children = [
            _cond("boll_width_pct60", "notna", key="band_pct_ready",
                  label="布林带宽 60 日分位可用", detail_fields=("boll_width_pct60",)),
            _cond("boll_width_pct60", "le", squeeze_pct, key="band_squeezed",
                  label=f"带宽分位 ≤ {squeeze_pct:g}%", detail_fields=("boll_width_pct60",)),
            _cond("close", "gt", cond_dsl.FieldRef("boll_upper"), key="close_above_upper",
                  label="收盘突破布林上轨", detail_fields=("close", "boll_upper")),
            _cond("close", "gt", cond_dsl.FieldRef("high20_close"), key="new_high20",
                  label="收盘创 20 日新高", detail_fields=("close", "high20_close")),
            _cond("vol_ma5", "notna", key="vol_ma5_ready",
                  label="5 日均量可用", detail_fields=("vol_ma5",)),
            _cond("volume", "ge", cond_dsl.FieldRef("vol_ma5", vol_mult), key="volume_expansion",
                  label=f"成交量 ≥ 5 日均量 × {vol_mult:g}",
                  detail_fields=("volume", "vol_ma5")),
            _cond("range20_pct", "notna", key="range20_ready",
                  label="20 日区间宽度可用", detail_fields=("range20_pct",)),
            _cond("range20_pct", "lt", range_max, key="range20_narrow",
                  label=f"20 日区间宽度 < {range_max:g}%", detail_fields=("range20_pct",)),
            _cond("amount_ma20", "notna", key="amount_ma20_ready",
                  label="20 日均成交额可用", detail_fields=("amount_ma20",)),
            _cond("amount_ma20", "ge", min_amount, key="min_amount",
                  label=f"20 日均成交额 ≥ {min_amount / 1e8:g} 亿元",
                  detail_fields=("amount_ma20",)),
        ]
    elif algo_code == "b5":
        j_max = float(p["j_max"])
        dev_max = float(p["dev_max"])
        min_amount = float(p["min_amount"])
        children = [
            _cond("boll_lower", "notna", key="boll_lower_ready",
                  label="布林下轨可用", detail_fields=("boll_lower",)),
            _cond("low", "le", cond_dsl.FieldRef("boll_lower"), key="low_touch_lower",
                  label="最低价触及布林下轨", detail_fields=("low", "boll_lower")),
            _cond("kdj_j_min2", "lt", j_max, key="kdj_j_low",
                  label=f"今日或昨日 J < {j_max:g}", detail_fields=("kdj_j", "kdj_j_min2")),
            _cond("vol_ma5", "notna", key="vol_ma5_ready",
                  label="5 日均量可用", detail_fields=("vol_ma5",)),
            _cond("volume", "lt", cond_dsl.FieldRef("vol_ma5"), key="volume_shrink",
                  label="缩量（成交量 < 5 日均量）", detail_fields=("volume", "vol_ma5")),
            cond_dsl.ConditionGroup(
                logic="OR", key="bullish_bar", label="当日收阳或收盘不低于昨收",
                detail_fields=("open", "close", "pre_close"),
                children=[
                    _cond("close", "gt", cond_dsl.FieldRef("open"), key="close_above_open",
                          label="收盘 > 开盘（收阳）", detail_fields=("open", "close")),
                    _cond("close", "ge", cond_dsl.FieldRef("pre_close"), key="close_above_pre_close",
                          label="收盘 ≥ 昨收", detail_fields=("close", "pre_close")),
                ],
            ),
            cond_dsl.ConditionGroup(
                logic="OR", key="trend_support",
                label=f"收盘 > MA120，或低于布林下轨 ≤ {dev_max:g}%",
                detail_fields=("close", "ma120", "boll_lower", "boll_lower_dev_pct"),
                children=[
                    _cond("close", "gt", cond_dsl.FieldRef("ma120"), key="close_above_ma120",
                          label="收盘 > MA120", detail_fields=("close", "ma120")),
                    _cond("boll_lower_dev_pct", "le", dev_max, key="lower_deviation",
                          label=f"低于布林下轨 ≤ {dev_max:g}%",
                          detail_fields=("boll_lower", "close", "boll_lower_dev_pct")),
                ],
            ),
            _cond("amount_ma20", "notna", key="amount_ma20_ready",
                  label="20 日均成交额可用", detail_fields=("amount_ma20",)),
            _cond("amount_ma20", "ge", min_amount, key="min_amount",
                  label=f"20 日均成交额 ≥ {min_amount / 1e8:g} 亿元",
                  detail_fields=("amount_ma20",)),
        ]
    else:  # pragma: no cover - 调用方只传 B 系列
        raise UnknownAlgoError(f"未知策略: {algo_code}")
    return cond_dsl.ConditionGroup(logic="AND", children=children)

#: 默认参数下的条件树（V8 M1 起 b1–b5 停用，注册表为空；恢复策略时再构建）
CONDITION_SPECS: dict[str, cond_dsl.ConditionGroup] = {}

def _market_condition_detail(df: pd.DataFrame, algo_code: str) -> cond_dsl.ConditionDetail:
    """B4/B5 的市场环境要求（scope=market，单独展示）。"""
    market = df.attrs.get("market") or {}
    if algo_code == "b4":
        flag = _market_flag(df, "b4_ok")
        key, label = "b4_market", "沪深300 收盘 ≥ MA20 × 0.995"
        detail_text = (
            "指数数据缺失，按放行处理"
            if market.get("available") is False
            else f"沪深300 close={market.get('close')} ma20={market.get('ma20')}"
        )
    else:
        flag = _market_flag(df, "b5_ok")
        key, label = "b5_market", "沪深300 当日跌幅 > -2%"
        detail_text = (
            "指数数据缺失，按放行处理"
            if market.get("available") is False
            else f"沪深300 pct_chg={market.get('pct_chg')}"
        )
    passed = pd.Series(bool(flag), index=df.index, dtype=bool)
    return cond_dsl.ConditionDetail(
        key=key,
        label=label,
        scope="market",
        required=True,
        op=None,
        passed=passed,
        actual=passed,
        detail_text=detail_text,
    )

def _a4_regime_detail(df: pd.DataFrame) -> cond_dsl.ConditionDetail:
    """A4 防御型市场阶段要求（scope=market，单独展示）。"""
    market = df.attrs.get("market") or {}
    regime_available = bool(market.get("regime_available", False))
    regime_code = market.get("regime_code")
    passed_flag = (
        True if not regime_available else regime_code in A4_DEFENSIVE_REGIMES
    )
    detail_text = (
        "市场阶段数据缺失，按放行处理"
        if not regime_available
        else (
            f"regime={regime_code} label={market.get('regime_label')} "
            f"days={market.get('regime_days')} since={market.get('regime_since')}"
        )
    )
    passed = pd.Series(bool(passed_flag), index=df.index, dtype=bool)
    return cond_dsl.ConditionDetail(
        key="a4_regime",
        label="仅在防御型市场阶段启用（震荡筑底/向下调整/避险下跌）",
        scope="market",
        required=True,
        op=None,
        passed=passed,
        actual=passed,
        threshold=sorted(A4_DEFENSIVE_REGIMES),
        detail_text=detail_text,
    )

@dataclass
class StrategyEvaluation:
    """一次策略求值：整体掩码 + 逐条明细 + 实际求值用的 frame。"""

    mask: pd.Series
    details: list[cond_dsl.ConditionDetail]
    frame: pd.DataFrame

def _evaluate_strategy(
    algo_code: str, df: pd.DataFrame, params: dict
) -> StrategyEvaluation:
    """策略求值（DI 条件树 + 市场门槛 + A3/A4 专用口径）。"""
    spec = get_spec(algo_code)
    code = spec.code
    normalized = _validate_params(code, params)
    frame = df

    if code in CONDITION_SPECS:
        if code == "b5" and "boll_lower_dev_pct" not in frame.columns:
            frame = df.copy()
            lower = _numeric_column(frame, "boll_lower")
            close = _numeric_column(frame, "close")
            frame["boll_lower_dev_pct"] = (lower - close) / lower.where(lower > 0) * 100.0
        group = _build_condition_specs(code, normalized)
        mask = cond_dsl.evaluate(frame, group)
        details = cond_dsl.explain(frame, group)
        if code in {"b4", "b5"}:
            details.append(_market_condition_detail(frame, code))
            if not _market_flag(frame, f"{code}_ok"):
                logger.info(
                    "%s 市场状态门槛未通过，本次不产生信号", code.upper()
                )
                mask &= False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool), details=details, frame=frame
        )

    if code == "a3":
        composite = _numeric_column(frame, "composite_score")
        if "ownership" in frame.columns:
            ownership = frame["ownership"].astype("object")
            central = ownership.eq("央企").fillna(False)
        else:
            central = pd.Series(False, index=frame.index, dtype=bool)
        mask = central & composite.notna() & (composite >= float(normalized["min_score"]))
    elif code == "a4":
        pe = _numeric_column(frame, "pe_ttm")
        dividend = _numeric_column(frame, "dv_ttm")
        pb = _numeric_column(frame, "pb")
        score = _numeric_column(frame, "composite_score")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        ma120 = _numeric_column(frame, "ma120")
        close = _numeric_column(frame, "close")
        mask = (
            (pe > 0)
            & (pe < float(normalized["pe_max"]))
            & dividend.notna()
            & (dividend >= float(normalized["div_min"]))
            & (dividend <= float(normalized["dv_max"]))
            & (pb > 0)
            & (pb <= float(normalized["pb_max"]))
            & (score >= float(normalized["min_score"]))
            & (amount_ma20 >= float(normalized["min_amount"]))
            & (ma120 > 0)
            & (close >= ma120 * float(normalized["trend_dev"]))
        )
        market = frame.attrs.get("market") or {}
        regime_available = bool(market.get("regime_available", False))
        regime_code = market.get("regime_code")
        if regime_available and regime_code not in A4_DEFENSIVE_REGIMES:
            logger.info("A4 防御型市场门槛未通过 regime=%s，本次不产生信号", regime_code)
            mask = mask & False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool),
            details=[_a4_regime_detail(frame)],
            frame=frame,
        )
    elif code == "c1":
        high_prev = _numeric_column(frame, "high20_prev")
        adj_close = _numeric_column(frame, "adj_close")
        adj_open = _numeric_column(frame, "adj_open")
        prev_close = _numeric_column(frame, "prev_adj_close")
        amount = _numeric_column(frame, "amount")
        mask = (
            (adj_close > high_prev)
            & (amount >= float(normalized["min_amount"]))
            & (adj_close > adj_open)
            & (adj_close > prev_close)
        )
    elif code == "c2":
        range40 = _numeric_column(frame, "range40_ratio")
        high40 = _numeric_column(frame, "high40")
        low10 = _numeric_column(frame, "low10")
        high10 = _numeric_column(frame, "high10")
        volume = _numeric_column(frame, "volume")
        vol_ma20_prev = _numeric_column(frame, "vol_ma20_prev")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        tight_ratio = high10 / low10.where(low10 > 0)
        mask = (
            (range40 > float(normalized["mom_min"]))
            & (tight_ratio < float(normalized["tight_max"]))
            & (low10 >= float(normalized["high_level"]) * high40)
            & (volume < float(normalized["shrink_max"]) * vol_ma20_prev)
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
    elif code == "c3":
        rps = _numeric_column(frame, "rps")
        adj_close = _numeric_column(frame, "adj_close")
        high120 = _numeric_column(frame, "high120")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        mask = (
            (rps >= float(normalized["rps_threshold"]))
            & (adj_close >= float(normalized["near_high_ratio"]) * high120)
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
    elif code == "c4":
        prev_limit = (
            _to_bool_series(frame["prev_is_limit_up"])
            if "prev_is_limit_up" in frame.columns
            else pd.Series(False, index=frame.index, dtype=bool)
        )
        adj_close = _numeric_column(frame, "adj_close")
        adj_open = _numeric_column(frame, "adj_open")
        adj_low = _numeric_column(frame, "adj_low")
        prev_close = _numeric_column(frame, "prev_adj_close")
        prev_volume = _numeric_column(frame, "prev_volume")
        volume = _numeric_column(frame, "volume")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        mask = (
            prev_limit
            & (adj_close < adj_open)
            & (volume > float(normalized["vol_mult"]) * prev_volume)
            & (adj_low >= prev_close)
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
    elif code == "c5":
        ma5_today = _numeric_column(frame, "ma5_today")
        ma20_today = _numeric_column(frame, "ma20_today")
        ma5_prev = _numeric_column(frame, "ma5_prev")
        ma20_prev = _numeric_column(frame, "ma20_prev")
        vol_ma20_prev = _numeric_column(frame, "vol_ma20_prev")
        volume = _numeric_column(frame, "volume")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        mask = (
            (ma5_prev < ma20_prev)
            & (ma5_today > ma20_today)
            & (volume > float(normalized["vol_mult"]) * vol_ma20_prev)
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
    elif code == "c6":
        band_pct = _numeric_column(frame, "boll_width_pct60")
        close = _numeric_column(frame, "close")
        upper = _numeric_column(frame, "boll_upper")
        high20 = _numeric_column(frame, "high20_close")
        volume = _numeric_column(frame, "volume")
        vol_ma5 = _numeric_column(frame, "vol_ma5")
        range20 = _numeric_column(frame, "range20_pct")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        mask = (
            band_pct.notna()
            & (band_pct <= float(normalized["squeeze_pct"]))
            & (close > upper)
            & (close > high20)
            & (volume >= float(normalized["vol_mult"]) * vol_ma5)
            & (range20.notna())
            & (range20 < float(normalized["range_max"]))
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
        market = frame.attrs.get("market") or {}
        if market.get("available") is not False and not market.get("b4_ok", True):
            logger.info("C6 市场环境门槛未通过，本次不产生信号")
            mask = mask & False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool),
            details=[_market_condition_detail(frame, "b4")],
            frame=frame,
        )
    else:  # pragma: no cover - get_spec 已保证策略存在
        raise UnknownAlgoError(f"未知策略: {algo_code}")
    return StrategyEvaluation(
        mask=mask.fillna(False).astype(bool), details=[], frame=frame
    )

def strategy_mask(algo_code: str, df: pd.DataFrame, params: dict) -> pd.Series:
    """返回策略条件命中的布尔 Series（B1–B5 由 CONDITION_SPECS 生成）。"""
    return _evaluate_strategy(algo_code, df, params).mask

def filter_by_strategy(algo_code: str, df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """按策略条件过滤候选池（纯函数）。"""
    if df is None or len(df) == 0:
        empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            empty.attrs.update(dict(df.attrs))
        return empty
    mask = strategy_mask(algo_code, df, params)
    filtered = df.loc[mask].copy()
    filtered.attrs.update(dict(df.attrs))
    return filtered

__all__ = [
    "_cond",
    "_build_condition_specs",
    "CONDITION_SPECS",
    "_market_condition_detail",
    "_a4_regime_detail",
    "StrategyEvaluation",
    "_evaluate_strategy",
    "strategy_mask",
    "filter_by_strategy",
]
