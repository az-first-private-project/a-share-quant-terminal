"""
内置策略（A3/A4 价值守正 + B1–B5 短线进攻）与候选池构建
========================================================
**一个策略 = 一个候选池构建步骤 + 一个排序评分步骤**：

  - `build_candidates(algo_code, trade_date, params, db)`：
      一条 SQL 横截面取数（`fact_stock_daily` + `fact_feature_daily` +
      `fact_stock_score_daily` + `fact_moneyflow` + `dim_stock`，全部 LEFT JOIN），
      先在 SQL 里做基础可用性过滤，再在 pandas 里做统一的 A 股口径过滤与策略条件。
      **禁止 N+1 查询**：候选池只查一次库，一次取回当日全市场。
      B4/B5 额外读取沪深300 指数状态（`market_indices`），把市场门槛放进 `frame.attrs["market"]`。
  - `rank_signals(algo_code, df, params)`：纯函数（无 DB），把候选池按策略口径再筛一次、
      排序、截断到 `limit`，并生成 `rank / score / metrics`，供 API 直接序列化。
      其中 **score = 本策略排序指标的原始值**。

现存启用策略（A1/A2 已按回测结果下线；B1–B5 于 V8 M1 停用，代码保留）：
  A3 央企 × 综合评分
      ``dim_stock.ownership == '央企'`` 且 ``composite_score >= min_score(默认6)``，
      按 ``composite_score`` 降序。回测 2025-01 起 +22.9%、最大回撤 -10.4%、Sharpe 0.85。
  A4 高股息质量 × 低估值
      ``pe_ttm > 0``、``pe_ttm < pe_max``、``div_min <= dv_ttm <= dv_max``、
      ``0 < pb <= pb_max``、``composite_score >= min_score``、20 日均额达标、
      ``close >= MA120 × trend_dev``，且仅在防御型市场阶段
      （bottoming/pull_down/risk_off）启用，按 ``dv_ttm`` 降序。

新增短线策略（来自《短线量化策略设计报告》，日线落地口径）：
  B1 双均线动量突破
      EMA5/EMA20 近 3 日金叉、收盘站上 EMA20 且 EMA20 上行、ROC10 > 0、
      量能 ≥ 5 日均量 × min_vol_ratio、收盘 > MA60、20 日均额 ≥ min_amount，
      按 ``roc10`` 降序。
  B2 RSI6 超卖回归
      ``rsi6 < rsi_max(默认20)`` 且当日出现止跌 K 线（收阳或收盘不低于昨收）、
      收盘 > MA120 × trend_dev(默认0.9，剔除深熊)、60 日振幅 > amp_min(默认15%)、
      20 日均额达标，按 ``rsi6`` 升序（越超卖越靠前）。
  B3 KDJ 低位金叉
      今日或昨日 J < j_max(默认10)、当日 K 上穿 D、K < k_max(默认30)、
      收盘 > EMA60、收盘不低于昨收、20 日均额达标，按 ``kdj_j`` 升序。
  B4 布林挤压突破
      前一日带宽处于 60 日最低 squeeze_pct% 分位、收盘突破上轨且创 20 日新高、
      量能 ≥ 5 日均量 × vol_mult、20 日区间宽度 < range_max、20 日均额达标，
      且沪深300 收盘 ≥ MA20 × 0.995（市场状态门槛），按带宽分位升序。
  B5 布林下轨 × KDJ 双确认
      最低价触及布林下轨、今日或昨日 J < j_max(默认0)、缩量、
      止跌 K 线、收盘 > MA120 或偏离下轨不超过 dev_max%、20 日均额达标，
      且沪深300 当日跌幅 > -2%（市场状态门槛），按 ``kdj_j`` 升序。

统一前置过滤（所有策略一致）：
  剔除 ST、status != 1（停牌/退市）、delist_date 已到、上市不足 60 个交易日、
  is_suspended、当日无行情（无 fact_stock_daily 行）。
"""

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403
from app.strategy.market import *  # noqa: F401,F403
from app.strategy.conditions import *  # noqa: F401,F403
from app.strategy.ranking import *  # noqa: F401,F403
from app.strategy.candidates import *  # noqa: F401,F403

__all__ = [
    "NEW_STOCK_MIN_TRADING_DAYS",
    "_FALLBACK_NEW_STOCK_CALENDAR_DAYS",
    "BENCHMARK_INDEX_CODE",
    "A4_DEFENSIVE_REGIMES",
    "DISPLAY_FIELDS",
    "_param",
    "StrategySpec",
    "SPECS",
    "CANDIDATE_SPECS",
    "_SORT_SPEC",
    "_METRIC_FIELDS",
    "jsonable",
    "get_spec",
    "list_specs",
    "describe_specs",
    "_check_range",
    "_validate_params",
    "validate_params",
    "_DIM_COLUMNS",
    "_DAILY_COLUMNS",
    "_FEATURE_COLUMNS",
    "_SCORE_COLUMNS",
    "_FLOW_COLUMNS",
    "_NUMERIC_COLUMNS",
    "_BOOL_COLUMNS",
    "_decimal_to_float",
    "_build_record",
    "_fetch_cross_section",
    "_WINDOW_STRATEGIES",
    "_DERIVED_COLUMNS",
    "_window_features",
    "_enrich_history",
    "_to_float_series",
    "coerce_numeric_frame",
    "_to_float_quiet",
    "_to_bool_quiet",
    "_to_bool_series",
    "_normalize_dtypes",
    "_load_open_dates",
    "listed_trading_days",
    "apply_common_filters",
    "_numeric_column",
    "_B_READY_COLUMN",
    "_MIN_FEATURE_COVERAGE",
    "_assert_data_ready",
    "load_market_context",
    "market_gate_payload",
    "_market_flag",
    "_cond",
    "_build_condition_specs",
    "CONDITION_SPECS",
    "_market_condition_detail",
    "_a4_regime_detail",
    "StrategyEvaluation",
    "_evaluate_strategy",
    "strategy_mask",
    "filter_by_strategy",
    "_sort_candidates",
    "_score_series",
    "_FACTOR_META",
    "_score_breakdown",
    "_metrics_for_row",
    "_empty_signal_frame",
    "rank_signals",
    "build_candidates",
]
