"""V9 拆分：财报/分红标准化与 TTM 估值数学。

由原 ``app/ingest/valuation.py`` 拆分而来；``valuation.py`` 保留门面与
网络/同步编排，既有 ``from app.ingest.valuation import ...`` 导入保持可用。
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

try:  # pragma: no cover —— 依赖是否安装由运行环境决定
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]

from app.core.logging import get_logger

logger = get_logger(__name__)

REPORT_LAG_DAYS_BY_MONTH: dict[int, int] = {3: 30, 6: 62, 9: 30, 12: 120}

REPORT_LAG_FALLBACK_DAYS = 120

DATASET = "valuation"

DEFAULT_START_YEAR = 2019

SYNC_BATCH_CODES = 20

SYNC_CHECKPOINT_EVERY = 50

COMPUTE_BATCH_CODES = 200

UPDATE_CHUNK_ROWS = 1000

DIVIDEND_REFRESH_DAYS = 30

DIVIDEND_BULK_YEARS = 6

DIVIDEND_BULK_CHUNK = 2000

DV_WINDOW_DAYS = 365

FIN_COLUMNS: tuple[str, ...] = ("report_date", "eps", "bps", "roe", "payout_ratio")

TTM_COLUMNS: tuple[str, ...] = ("report_date", "eps_ttm", "eps_ttm_basis")

VALUATION_COLUMNS: tuple[str, ...] = ("trade_date", "pe_ttm", "pb", "dv_ttm", "report_date")

DIVIDEND_COLUMNS: tuple[str, ...] = ("ex_date", "announce_date", "dps", "report_period")

FIN_DATE_CANDIDATES = (
    "日期", "报告期", "报告日期", "报表日期",
    "report_date", "trade_date", "date", "REPORT_DATE",
)

FIN_DATE_PATTERNS = (
    r"^日期$", r"^报告期", r"^报告日期", r"^报表日期", r"^(?:report_date|trade_date|date)$",
)

FIN_EPS_CANDIDATES = (
    "每股收益_调整后(元)", "每股收益_调整后",
    "eps",
)

FIN_EPS_PATTERNS = (r"^每股收益[_\-]?调整后", r"^eps$")

FIN_EPS_FALLBACK_CANDIDATES = (
    "摊薄每股收益(元)", "摊薄每股收益",
    "基本每股收益(元)", "基本每股收益",
    "每股收益(元)", "每股收益",
)

FIN_EPS_FALLBACK_PATTERNS = (
    r"^摊薄每股收益", r"^基本每股收益", r"^每股收益",
)

FIN_BPS_CANDIDATES = (
    "每股净资产_调整前(元)", "每股净资产_调整前",
    "bps",
)

FIN_BPS_PATTERNS = (r"^每股净资产[_\-]?调整前", r"^bps$")

FIN_BPS_FALLBACK_CANDIDATES = (
    "每股净资产_调整后(元)", "每股净资产_调整后",
    "每股净资产(元)", "每股净资产",
)

FIN_BPS_FALLBACK_PATTERNS = (
    r"^每股净资产[_\-]?调整后", r"^每股净资产",
)

FIN_ROE_CANDIDATES = (
    "净资产收益率(%)", "净资产收益率", "加权净资产收益率(%)", "净资产收益率_摊薄(%)", "roe",
)

FIN_ROE_PATTERNS = (r"^净资产收益率", r"^加权净资产收益率", r"^roe$")

FIN_PAYOUT_CANDIDATES = (
    "股息发放率(%)", "股息发放率", "股利支付率(%)", "股利支付率", "分红率(%)", "payout_ratio",
)

FIN_PAYOUT_PATTERNS = (r"股息发放率", r"股利支付率", r"分红率", r"^payout_ratio$")

FIN_REVENUE_CANDIDATES = (
    "营业总收入(元)", "营业收入(元)", "主营业务收入(元)", "营业总收入", "营业收入", "revenue",
)

FIN_REVENUE_PATTERNS = (
    r"^(?:营业总收入|营业收入|主营业务收入)(?:\(元\))?$",
    r"^revenue$",
)

REVENUE_MIN_YUAN = 1_000_000.0

DIV_EX_DATE_CANDIDATES = ("除权日", "除息日", "除权除息日", "ex_date", "EX_DATE")

DIV_EX_DATE_PATTERNS = (r"^除权日", r"^除息日", r"^ex_date$")

DIV_PAY_DATE_CANDIDATES = ("派息日", "派息日期", "现金红利发放日", "股息发放日", "pay_date")

DIV_PAY_DATE_PATTERNS = (r"^派息日", r"派息日期", r"^pay_date$")

DIV_ANNOUNCE_CANDIDATES = (
    "实施方案公告日期", "公告日期", "预案公告日", "分红公告日", "announce_date",
)

DIV_ANNOUNCE_PATTERNS = (r"^实施方案公告日期", r"^公告日期", r"^announce_date$")

DIV_DPS_PER_10_CANDIDATES = (
    "派息比例", "派息比例(元)", "每10股派息(元)", "现金分红比例", "分红比例", "dividend_per_10",
)

DIV_DPS_PER_10_PATTERNS = (r"^派息比例", r"每10股", r"^dividend_per_10$")

DIV_REPORT_PERIOD_CANDIDATES = ("报告时间", "报告期", "report_period")

DIV_REPORT_PERIOD_PATTERNS = (r"^报告时间", r"^报告期", r"^report_period$")

ABSTRACT_METRIC_CANDIDATES = ("指标", "指标名称", "项目", "metric")

ABSTRACT_METRIC_PATTERNS = (r"^指标", r"^项目$", r"^metric$")

ABSTRACT_NET_PROFIT = ("归母净利润", "归属于母公司股东的净利润", "归属于母公司所有者的净利润", "净利润")

ABSTRACT_NET_PROFIT_PATTERNS = (r"^归母净利润", r"归属于母公司", r"^净利润")

ABSTRACT_REVENUE = ("营业总收入", "营业收入")

ABSTRACT_REVENUE_PATTERNS = (r"^营业总收入", r"^营业收入")

ABSTRACT_ROE = ("净资产收益率", "净资产收益率(%)", "ROE")

ABSTRACT_ROE_PATTERNS = (r"^净资产收益率", r"^roe$")

ABSTRACT_TOTAL_SHARE = ("总股本", "实收资本(或股本)", "实收资本", "股本")

ABSTRACT_TOTAL_SHARE_PATTERNS = (r"^总股本", r"^实收资本", r"^股本")

_EMPTY_TOKENS = {"", "-", "--", "—", "——", "nan", "NaN", "NONE", "None", "none", "null", "NULL"}

def _resolve_column(
    columns: Iterable[Any],
    candidates: Sequence[str],
    patterns: Sequence[str] = (),
) -> Any | None:
    """在 ``columns`` 里按「候选名精确匹配 → 正则匹配」找到目标列，找不到返回 None。

    Args:
        columns: ``df.columns``（可能是 ``pd.Index``）。
        candidates: 候选列名，顺序即优先级。
        patterns: 正则模式，用于列名漂移（版本升级改名）时的兜底匹配。

    Returns:
        命中的**原始**列标签（可直接 ``df[col]``），没有命中返回 ``None``。

    为什么不用 ``df.get(name)``：新浪接口的列名带单位后缀且随版本变化
    （"每股净资产_调整前(元)" / "每股净资产_调整前"），只做精确匹配等于把可用性
    绑死在某一个 akshare 版本上。
    """
    names = [str(column).strip() for column in columns]
    for candidate in candidates:
        if candidate in names:
            return list(columns)[names.index(candidate)]
    for pattern in patterns:
        try:
            regex = re.compile(pattern)
        except re.error:  # pragma: no cover —— 模式写死在本模块，防御
            continue
        for original, name in zip(columns, names):
            if regex.search(name):
                return original
    return None

def _num(value: Any) -> float | None:
    """任意值 → float；无法转换 / NaN / Inf → None（"--"、""、带 % 与千分位都能处理）。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if text in _EMPTY_TOKENS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number

def _numeric_column(frame: pd.DataFrame, column: Any | None) -> pd.Series | None:
    """安全取数值列：列不存在返回 None；脏字符串（"--"/"1,234"/"12.5%"）先清洗再 coerce。"""
    if column is None or column not in frame.columns:
        return None
    series = frame[column]
    if series.dtype == object or isinstance(series.dtype, pd.StringDtype):
        text = series.astype("string").str.replace(",", "", regex=False)
        text = text.str.replace("%", "", regex=False).str.strip()
        text = text.mask(text.isin(_EMPTY_TOKENS))
        series = text
    return pd.to_numeric(series, errors="coerce").astype("float64")

def _date_series(values: Any) -> pd.Series:
    """任意日期列（date / Timestamp / "2024-03-31" / "20240331" / int） → ``datetime.date`` 序列。

    无效值变 ``NaT``（调用方自己 dropna）。

    为什么**逐值解析**而不是 ``pd.to_datetime(series, errors="coerce")``：后者会按
    第一个元素**推断统一格式**，遇到 ``"20240331"`` 与 ``"2023-12-31"`` 混在一列时
    会把不符合首个格式的值全部判成 NaT（实测 pandas 2.3 的坑）。本函数只用于财报/分红
    这类**小表**（一只股票最多几十行），逐值解析的开销可以忽略。
    """
    series = values if isinstance(values, pd.Series) else pd.Series(list(values))
    if pd.api.types.is_integer_dtype(series):
        series = series.astype("Int64").astype("string").astype(object)
    parsed: list[date | None] = []
    for value in series.astype(object):
        parsed.append(_parse_period(value))
    return pd.Series(parsed, index=series.index, dtype="object")

def _parse_period(value: Any) -> date | None:
    """把一个"报告期"值解析成 date（支持 20260630 / "2026-06-30" / date / Timestamp）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in _EMPTY_TOKENS:
        return None
    if text.isdigit() and len(text) == 8:
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    parsed = pd.to_datetime(text, errors="coerce")
    if parsed is pd.NaT or (not isinstance(parsed, pd.Timestamp) and pd.isna(parsed)):
        return None
    return parsed.date()

def _finite(series: pd.Series) -> pd.Series:
    """把 ±Inf 换成 NaN（``Numeric`` 列不能写 Inf，PG 会直接报错）。"""
    return series.replace([np.inf, -np.inf], np.nan)

def _empty_financials() -> pd.DataFrame:
    """归一化财报表的空表（列齐全，下游不用判断列是否存在）。"""
    return pd.DataFrame(
        {
            "report_date": pd.Series(dtype="object"),
            "eps": pd.Series(dtype="float64"),
            "bps": pd.Series(dtype="float64"),
            "roe": pd.Series(dtype="float64"),
            "payout_ratio": pd.Series(dtype="float64"),
        }
    )

def _empty_ttm() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "report_date": pd.Series(dtype="object"),
            "eps_ttm": pd.Series(dtype="float64"),
            "eps_ttm_basis": pd.Series(dtype="object"),
        }
    )

def _empty_valuation() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.Series(dtype="object"),
            "pe_ttm": pd.Series(dtype="float64"),
            "pb": pd.Series(dtype="float64"),
            "dv_ttm": pd.Series(dtype="float64"),
            "report_date": pd.Series(dtype="object"),
        }
    )

def _empty_financials_ttm() -> pd.DataFrame:
    """``fetch_financials`` 的返回口径（归一化列 + TTM 列 + revenue）。"""
    frame = _empty_financials()
    frame["eps_ttm"] = pd.Series(dtype="float64")
    frame["eps_ttm_basis"] = pd.Series(dtype="object")
    frame["revenue"] = pd.Series(dtype="float64")
    return frame[list(FIN_COLUMNS) + ["eps_ttm", "eps_ttm_basis", "revenue"]]

def _coalesce_numeric(
    frame: pd.DataFrame,
    primary_candidates: Sequence[str],
    primary_patterns: Sequence[str],
    fallback_candidates: Sequence[str],
    fallback_patterns: Sequence[str],
) -> pd.Series | None:
    """两列数值的**逐行**合并：主列有值用主列，主列为空/缺列时用兜底列。

    "逐行"很关键：``每股净资产_调整前`` 在某些行是 ``"--"``（空）但在另一行有值，
    按"整列优先级"选列会把整列都丢掉。两列都不存在时返回 None（调用方补全 NaN 列）。
    """
    primary = _numeric_column(
        frame, _resolve_column(frame.columns, primary_candidates, primary_patterns)
    )
    fallback = _numeric_column(
        frame, _resolve_column(frame.columns, fallback_candidates, fallback_patterns)
    )
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return primary.where(primary.notna(), fallback)

def normalize_financials(raw: pd.DataFrame | None) -> pd.DataFrame:
    """把新浪 ``stock_financial_analysis_indicator`` 的原始表归一化。

    输出列（按 ``report_date`` **升序**）：``[report_date, eps, bps, roe, payout_ratio]``

    取值规则（缺列 → 该列全 NaN，**绝不 KeyError**；主列为空时**逐行**退兜底列）：
        - ``eps``：优先 ``每股收益_调整后``，空则退 ``摊薄每股收益``（再兜底 ``基本每股收益``）；
        - ``bps``：优先 ``每股净资产_调整前``，空则退 ``每股净资产_调整后``。
          **为什么"调整前"优先**：``每股净资产_调整后`` 有脏数据 —— 实测 688981
          （中芯国际）``调整后=594.8588`` vs ``调整前=25.7375``（真实 BPS 约 19–26），
          用"调整后"算 PB 会错 20 倍；
        - ``roe``：``净资产收益率``；
        - ``payout_ratio``：``股息发放率``（%，只作参考入库，**不再用于算股息率** ——
          股息率走巨潮分红明细，见 :func:`dv_ttm_series`）。

    列名匹配是容错的（候选名 + 正则前缀匹配），因为 akshare 改版会改列名/去单位后缀。
    数值列强制 ``pd.to_numeric(errors="coerce")``，``"--"``/``""``/``"12.5%"`` 这类脏值
    → NaN 而不是让后面算 PE 时报错或写进脏字符串。

    Args:
        raw: 原始 DataFrame（可为 None / 空表）。

    Returns:
        归一化后的 DataFrame（空输入返回同列空表）。
    """
    if raw is None or len(raw) == 0:
        return _empty_financials()

    date_column = _resolve_column(raw.columns, FIN_DATE_CANDIDATES, FIN_DATE_PATTERNS)
    if date_column is None:
        logger.warning(
            "财报指标表缺少日期列（现有列：%s），无法归一化",
            [str(c) for c in list(raw.columns)[:12]],
        )
        return _empty_financials()

    eps = _coalesce_numeric(
        raw, FIN_EPS_CANDIDATES, FIN_EPS_PATTERNS,
        FIN_EPS_FALLBACK_CANDIDATES, FIN_EPS_FALLBACK_PATTERNS,
    )
    bps = _coalesce_numeric(
        raw, FIN_BPS_CANDIDATES, FIN_BPS_PATTERNS,
        FIN_BPS_FALLBACK_CANDIDATES, FIN_BPS_FALLBACK_PATTERNS,
    )
    frame = pd.DataFrame(
        {
            "report_date": _date_series(raw[date_column]),
            "eps": eps,
            "bps": bps,
            "roe": _numeric_column(raw, _resolve_column(raw.columns, FIN_ROE_CANDIDATES,
                                                        FIN_ROE_PATTERNS)),
            "payout_ratio": _numeric_column(
                raw, _resolve_column(raw.columns, FIN_PAYOUT_CANDIDATES, FIN_PAYOUT_PATTERNS)
            ),
        }
    )
    for column in ("eps", "bps", "roe", "payout_ratio"):
        if frame[column].isna().all():
            frame[column] = pd.Series(np.nan, index=frame.index, dtype="float64")

    frame = frame.dropna(subset=["report_date"])
    if frame.empty:
        return _empty_financials()

    # 同一报告期多行（新浪偶尔返回重复期）时保留最后一行（最新修订）
    frame = frame.drop_duplicates(subset=["report_date"], keep="last")
    frame = frame.sort_values("report_date", kind="mergesort").reset_index(drop=True)
    return frame[list(FIN_COLUMNS)]

def eps_ttm_series(fins: pd.DataFrame) -> pd.DataFrame:
    """由**季度累计**口径的 EPS 组装 TTM（trailing twelve months）。

    口径推导（A 股财报是"年初至今累计"而不是单季度）::

        报告期 MMDD = 1231（年报）：
            eps_ttm = 该期累计值                      # 12 个月，正好是 TTM

        其他季度（0331 / 0630 / 0930）：
            eps_ttm = 本期累计 + 上年年报累计 − 上年同期累计
                    现年 YTD    + 去年 FY       − 去年 YTD

        例：2024-03-31 的一季报 TTM
            = 2024Q1(0.40) + 2023FY(1.20) − 2023Q1(0.30) = 1.30

    三期里**任一期缺失**就返回 NaN（不做线性外推：外推出来的 PE 在策略里没有任何
    可信度，宁可返回 NULL 让策略层跳过）。1231 特例说明基础期就是完整一年，不需要差分。

    Args:
        fins: 至少包含 ``report_date`` / ``eps`` 两列（``normalize_financials`` 的输出）。

    Returns:
        ``[report_date, eps_ttm, eps_ttm_basis]``，按 ``report_date`` 升序。
        ``eps_ttm_basis`` 记录用了哪三期（如 ``"2024-03-31+2023-12-31-2023-03-31"``），
        便于回溯"这个 PE 是用哪几份财报算的"。
    """
    if fins is None or len(fins) == 0:
        return _empty_ttm()
    frame = pd.DataFrame(fins)
    if "report_date" not in frame.columns:
        logger.warning("eps_ttm_series 缺少 report_date 列，返回空表")
        return _empty_ttm()

    frame = frame.copy()
    frame["report_date"] = _date_series(frame["report_date"])
    frame["eps"] = (
        pd.to_numeric(frame["eps"], errors="coerce")
        if "eps" in frame.columns
        else pd.Series(np.nan, index=frame.index, dtype="float64")
    )
    frame = frame.dropna(subset=["report_date"])
    if frame.empty:
        return _empty_ttm()
    frame = frame.drop_duplicates(subset=["report_date"], keep="last")
    frame = frame.sort_values("report_date", kind="mergesort").reset_index(drop=True)

    # 报告期很稀疏（一只股票 8 年也就 32 行），用 dict 查表比逐行累积更清楚也更快
    eps_by_period: dict[date, float | None] = {}
    for period, value in zip(frame["report_date"], frame["eps"]):
        eps_by_period[period] = None if pd.isna(value) else float(value)

    records: list[dict[str, Any]] = []
    for period in eps_by_period:
        current = eps_by_period[period]
        if period.month == 12 and period.day == 31:
            eps_ttm = current
            basis = f"{period.isoformat()}(annual)"
        else:
            prior_annual = date(period.year - 1, 12, 31)
            prior_same = date(period.year - 1, period.month, period.day)
            base_annual = eps_by_period.get(prior_annual)
            base_same = eps_by_period.get(prior_same)
            if current is None or base_annual is None or base_same is None:
                eps_ttm = None
            else:
                eps_ttm = current + base_annual - base_same
            basis = (
                f"{period.isoformat()}+{prior_annual.isoformat()}"
                f"-{prior_same.isoformat()}"
            )
        records.append(
            {"report_date": period, "eps_ttm": eps_ttm, "eps_ttm_basis": basis}
        )

    result = pd.DataFrame(records, columns=list(TTM_COLUMNS))
    result["eps_ttm"] = pd.to_numeric(result["eps_ttm"], errors="coerce")
    return result

def revenue_ttm_series(fins: pd.DataFrame) -> pd.DataFrame:
    """由**季度累计**口径的营业总收入组装 TTM（与 :func:`eps_ttm_series` 同一套公式）。

    输入需要 ``report_date`` / ``revenue`` 两列（``dim_stock_financial`` 的营收列）。
    营收与 EPS 都是"年初至今累计"，所以直接复用 EPS 的三期推导；营收为 NULL/缺列时
    该报告期返回 NaN，绝不做外推。

    Returns:
        ``[report_date, revenue_ttm]``，按 ``report_date`` 升序；单位与输入一致（元）。
    """
    columns = ["report_date", "revenue_ttm"]
    if fins is None or len(fins) == 0 or "revenue" not in getattr(fins, "columns", []):
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    frame = pd.DataFrame(fins[["report_date", "revenue"]]).rename(
        columns={"revenue": "eps"}
    )
    ttm = eps_ttm_series(frame)
    result = ttm[["report_date", "eps_ttm"]].rename(columns={"eps_ttm": "revenue_ttm"})
    result["revenue_ttm"] = pd.to_numeric(result["revenue_ttm"], errors="coerce")
    return result[columns]

def ps_ttm_values(
    prices: pd.DataFrame,
    valuation: pd.DataFrame,
    fins: pd.DataFrame,
) -> pd.Series:
    """市销率 = 当日总市值 / 营业总收入 TTM。

    ``prices`` 与 ``valuation`` 必须**同序**（``compute_valuation`` 里两者同源同序）。
    营收缺失、为 0 或绝对值低于 :data:`REVENUE_MIN_YUAN`（说明库里混进了增长率/比率）→
    NaN，写库时由 ``COALESCE`` 保留已有 PS，绝不会用增长率算出天文数字。营收 TTM 为
    **负**是合法情形（亏损公司收入冲回/重述），此时 PS 为负，与东财口径一致；仅用
    绝对值门槛把"15.71 这种增长率"挡掉。
    """
    if valuation is None or len(valuation) == 0:
        return pd.Series(dtype="float64")
    if prices is not None and "market_cap" in prices.columns:
        market_cap = pd.to_numeric(prices["market_cap"], errors="coerce")
    else:
        market_cap = pd.Series(np.nan, index=valuation.index, dtype="float64")
    market_cap = market_cap.reset_index(drop=True)

    ttm = revenue_ttm_series(fins)
    revenue_by_period = (
        dict(zip(ttm["report_date"], ttm["revenue_ttm"])) if len(ttm) else {}
    )
    if "report_date" in valuation.columns:
        revenue = pd.to_numeric(
            valuation["report_date"].map(revenue_by_period), errors="coerce"
        )
    else:
        revenue = pd.Series(np.nan, index=valuation.index, dtype="float64")
    revenue = revenue.reset_index(drop=True)

    ps = market_cap / revenue
    return _finite(
        ps.where(
            market_cap.notna()
            & (market_cap > 0)
            & revenue.notna()
            & (revenue.abs() >= REVENUE_MIN_YUAN)
        )
    )

def _prepare_financials(fins: pd.DataFrame | None, lag_days: int | None = None) -> pd.DataFrame:
    """把财报表整理成"可用日期"维度，供 ``merge_asof`` 使用。

    输入兼容两种形态：
        1. ``normalize_financials`` (+ ``eps_ttm_series``) 的输出（列：eps/bps/roe/payout_ratio/...）；
        2. ``dim_stock_financial`` 的行（列：code/report_date/announced_date/eps_ttm/bps/payout_ratio）。

    point-in-time 可用日期 ``available_date`` 的取值：
        - 有 ``announced_date`` → 用它（阶段 1 落库时算好的 报告期+法定滞后）；
        - 没有（NaN/None）→ ``report_date + 报告类型对应滞后``（本模块内部推导的兜底口径）。

    防御：``announced_date`` 早于 ``report_date`` 属于脏数据（会让"未来财报"提前可用），
    这种行回落到 ``report_date + 报告类型对应滞后``。

    Args:
        lag_days: 显式覆盖兜底滞后（自然日）；``None`` = 按报告类型取法定披露期限。

    Returns:
        ``[report_date, eps_ttm, bps, payout_ratio, available_date]``，按 ``available_date`` 升序。
    """
    columns = ["report_date", "eps_ttm", "bps", "payout_ratio", "available_date"]
    if fins is None:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    frame = pd.DataFrame(fins)
    if len(frame) == 0 or "report_date" not in frame.columns:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    frame = frame.copy()
    frame["report_date"] = _date_series(frame["report_date"])
    frame = frame.dropna(subset=["report_date"])
    if frame.empty:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    # --- eps_ttm：优先用已有列；缺失列（或整列全 NaN）时由 eps 现场推导 ---
    if "eps_ttm" in frame.columns:
        frame["eps_ttm"] = pd.to_numeric(frame["eps_ttm"], errors="coerce")
    else:
        frame["eps_ttm"] = np.nan
    if frame["eps_ttm"].isna().all() and "eps" in frame.columns:
        frame["eps"] = pd.to_numeric(frame["eps"], errors="coerce")
        if frame["eps"].notna().any():
            ttm = eps_ttm_series(frame)
            frame = frame.drop(columns=[c for c in ("eps_ttm_basis",) if c in frame.columns])
            frame = frame.drop(columns=["eps_ttm"]).merge(
                ttm[["report_date", "eps_ttm"]], on="report_date", how="left"
            )

    for column in ("bps", "payout_ratio"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = np.nan

    frame = frame.sort_values("report_date", kind="mergesort")
    frame = frame.drop_duplicates(subset=["report_date"], keep="last")

    # --- point-in-time 可用日期 ---
    period_dt = pd.to_datetime(frame["report_date"])
    if lag_days is None:
        lag = (
            period_dt.dt.month.map(REPORT_LAG_DAYS_BY_MONTH)
            .fillna(REPORT_LAG_FALLBACK_DAYS)
            .astype(int)
        )
        fallback = period_dt + pd.to_timedelta(lag, unit="D")
    else:
        fallback = period_dt + pd.Timedelta(days=int(lag_days))
    if "announced_date" in frame.columns:
        announced = pd.to_datetime(frame["announced_date"].astype(object), errors="coerce")
        # 缺失（NULL）→ 回落到报告类型滞后；脏数据（早于报告期）也不可信，同样回落
        available = announced.fillna(fallback)
        invalid = announced.notna() & (announced < period_dt)
        available = available.where(~invalid, fallback)
    else:
        available = fallback
    frame["available_date"] = available

    frame = frame.dropna(subset=["available_date"])
    frame = frame.sort_values(["available_date", "report_date"], kind="mergesort")
    frame = frame.drop_duplicates(subset=["available_date"], keep="last")
    return frame[columns].reset_index(drop=True)

def build_valuation_frame(
    prices: pd.DataFrame,
    fins: pd.DataFrame,
    lag_days: int | None = None,
) -> pd.DataFrame:
    """把"按报告期"的估值因子展开到"按交易日"（point-in-time，禁止未来函数）。

    对每个交易日 ``d``，取 ``available_date <= d`` 的**最新**报告期（``available_date`` =
    ``announced_date``，没有则 ``report_date + lag_days``）。核心是
    ``pd.merge_asof(direction="backward")``：向量化、不逐行 apply，5 年全市场也就秒级。

    计算口径::

        pe_ttm    = close / eps_ttm          # eps_ttm 为 NaN/0 → NaN；**允许负值**
        pb        = close / bps              # bps <= 0（资不抵债）→ NaN
        dv_ttm(%) = payout_ratio/100 * eps_ttm / close * 100   # payout 缺失 → NaN

    注意：这里的 ``dv_ttm`` 是**旧口径**（分红发放率 × EPS / 股价），只保留给纯函数/单测
    与"没有分红明细时的应急口径"。生产链路（:func:`compute_valuation`）会用
    :func:`dv_ttm_series` 的**巨潮分红明细口径**覆盖它 —— 因为新浪"股息发放率"大量为空，
    且语义是"分红/净利润"而不是股息率。

    Args:
        prices: ``[trade_date, close]``（升序；可多一列 ``code`` 见下）。``trade_date``
            可以是 date / Timestamp / 字符串。
        fins: 见 :func:`_prepare_financials` 的两种输入形态；多报告期、乱序都可以。
        lag_days: 没有 ``announced_date`` 时的披露滞后（自然日）；``None`` = 按报告类型取
            法定披露期限（一季报/三季报 30 天、半年报 62 天、年报 120 天）。

    Returns:
        ``[trade_date, pe_ttm, pb, dv_ttm, report_date]``，**行序与 ``prices`` 一致**
        （内部排序只为 merge_asof 服务，输出会还原）。``trade_date`` / ``report_date``
        是 ``datetime.date``；缺失的因子是 ``NaN``（写库时统一转 NULL）。

    多只股票一起算：``prices`` 与 ``fins`` **都带 ``code`` 列**时按 code 分组递归计算，
    输出会在最前面多一列 ``code``（当日全市场一次算完用得上）。否则按单只股票处理
    （``fins`` 里的 ``code`` 列会被忽略，因为价格序列不带代码）。
    """
    if prices is None or len(prices) == 0:
        return _empty_valuation()

    price_frame = pd.DataFrame(prices).copy()
    if "trade_date" not in price_frame.columns or "close" not in price_frame.columns:
        logger.error(
            "build_valuation_frame 的 prices 缺少 trade_date/close 列（现有：%s），返回空表",
            [str(c) for c in price_frame.columns],
        )
        return _empty_valuation()

    fins_frame = pd.DataFrame(fins) if fins is not None else None
    if (
        "code" in price_frame.columns
        and fins_frame is not None
        and "code" in fins_frame.columns
    ):
        pieces: list[pd.DataFrame] = []
        for code, price_group in price_frame.groupby("code", sort=False):
            fin_group = fins_frame[fins_frame["code"] == code]
            piece = build_valuation_frame(
                price_group.drop(columns=["code"]), fin_group, lag_days=lag_days
            )
            piece.insert(0, "code", code)
            pieces.append(piece)
        if not pieces:
            return _empty_valuation()
        return pd.concat(pieces, ignore_index=True)

    price_frame["trade_date"] = pd.to_datetime(price_frame["trade_date"], errors="coerce")
    price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
    price_frame = price_frame.dropna(subset=["trade_date"]).reset_index(drop=True)
    if price_frame.empty:
        return _empty_valuation()
    # 输出要还原调用方行序，先记下位置（外部看到的就是"输入什么顺序输出什么顺序"）
    price_frame["_order"] = np.arange(len(price_frame))
    left = price_frame.sort_values("trade_date", kind="mergesort")

    prepared = _prepare_financials(fins_frame, lag_days)
    if prepared.empty:
        merged = left.copy()
        merged["report_date"] = pd.NaT
        merged["eps_ttm"] = np.nan
        merged["bps"] = np.nan
        merged["payout_ratio"] = np.nan
    else:
        right = prepared.sort_values("available_date", kind="mergesort")
        merged = pd.merge_asof(
            left,
            right,
            left_on="trade_date",
            right_on="available_date",
            direction="backward",
            allow_exact_matches=True,
        )

    close = pd.to_numeric(merged["close"], errors="coerce")
    eps = pd.to_numeric(merged["eps_ttm"], errors="coerce")
    bps = pd.to_numeric(merged["bps"], errors="coerce")
    payout = pd.to_numeric(merged["payout_ratio"], errors="coerce")

    pe = close / eps
    pe = _finite(pe.where(close.notna() & eps.notna() & (eps != 0)))
    pb = close / bps
    pb = _finite(pb.where(close.notna() & bps.notna() & (bps > 0)))
    dv = (payout / 100.0) * eps / close * 100.0
    dv = _finite(
        dv.where(close.notna() & (close > 0) & eps.notna() & (eps != 0) & payout.notna())
    )

    result = pd.DataFrame(
        {
            "trade_date": merged["trade_date"].dt.date,
            "pe_ttm": pe,
            "pb": pb,
            "dv_ttm": dv,
            "report_date": merged["report_date"],
            "_order": merged["_order"],
        }
    )
    # report_date 统一成 date / None（DatetimeNaT → None，方便写日志和测试断言）
    periods = pd.to_datetime(result["report_date"], errors="coerce")
    result["report_date"] = periods.dt.date.astype(object).where(periods.notna(), None)

    result = result.sort_values("_order", kind="mergesort").reset_index(drop=True)
    return result[list(VALUATION_COLUMNS)]

def _empty_dividends() -> pd.DataFrame:
    """归一化分红表的空表。"""
    return pd.DataFrame(
        {
            "ex_date": pd.Series(dtype="object"),
            "announce_date": pd.Series(dtype="object"),
            "dps": pd.Series(dtype="float64"),
            "report_period": pd.Series(dtype="object"),
        }
    )

def normalize_dividends(raw: pd.DataFrame | None) -> pd.DataFrame:
    """把巨潮 ``stock_dividend_cninfo`` 的原始表归一化。

    输出列（按 ``ex_date`` 升序）：``[ex_date, announce_date, dps, report_period]``

    换算口径（**关键**）：
        - ``派息比例`` 的单位是**每 10 股派 X 元**（实测 600519 的方案是 6.0 元/10 股），
          所以 ``dps = 派息比例 / 10``（元/股）。``6.0 → 0.6``；
        - ``ex_date`` 优先「除权日」；为空时退「派息日」；再为空退「实施方案公告日期」
          （这两种兜底会让 point-in-time 略微提前/滞后，所以记 warning 便于抽查）；
        - 只保留**现金分红**行：``派息比例`` 为空/<=0（纯送股、纯转增方案）直接跳过。

    同一 ``ex_date`` 出现多行（例如年度方案 + 特别分红）时 ``dps`` 求和、公告日取最新。

    Args:
        raw: 巨潮分红原始表（可为 None / 空表）。

    Returns:
        归一化后的 DataFrame（空输入返回同列空表）。
    """
    if raw is None or len(raw) == 0:
        return _empty_dividends()

    dps_per_10_column = _resolve_column(
        raw.columns, DIV_DPS_PER_10_CANDIDATES, DIV_DPS_PER_10_PATTERNS
    )
    if dps_per_10_column is None:
        logger.warning(
            "分红表缺少「派息比例」列（现有列：%s），无法归一化",
            [str(c) for c in list(raw.columns)[:12]],
        )
        return _empty_dividends()

    ex_column = _resolve_column(raw.columns, DIV_EX_DATE_CANDIDATES, DIV_EX_DATE_PATTERNS)
    pay_column = _resolve_column(raw.columns, DIV_PAY_DATE_CANDIDATES, DIV_PAY_DATE_PATTERNS)
    announce_column = _resolve_column(
        raw.columns, DIV_ANNOUNCE_CANDIDATES, DIV_ANNOUNCE_PATTERNS
    )
    period_column = _resolve_column(
        raw.columns, DIV_REPORT_PERIOD_CANDIDATES, DIV_REPORT_PERIOD_PATTERNS
    )

    ex_dates = _date_series(raw[ex_column]) if ex_column is not None else pd.Series(
        pd.NaT, index=raw.index
    )
    pay_dates = _date_series(raw[pay_column]) if pay_column is not None else pd.Series(
        pd.NaT, index=raw.index
    )
    announce_dates = _date_series(raw[announce_column]) if announce_column is not None else (
        pd.Series(pd.NaT, index=raw.index)
    )
    dps = _numeric_column(raw, dps_per_10_column)
    assert dps is not None  # 上面已确认列存在
    dps = dps / 10.0  # 每 10 股派 X 元 → 每股

    frame = pd.DataFrame(
        {
            "ex_date": ex_dates,
            "announce_date": announce_dates,
            "dps": dps,
            "report_period": (
                raw[period_column].astype("string").str.slice(0, 16)
                if period_column is not None
                else pd.Series(pd.NA, index=raw.index, dtype="string")
            ),
        }
    )
    # ex_date 兜底链：除权日 → 派息日 → 公告日
    fallback_mask = frame["ex_date"].isna() & pay_dates.notna()
    if fallback_mask.any():
        frame.loc[fallback_mask, "ex_date"] = pay_dates[fallback_mask]
    second_mask = frame["ex_date"].isna() & announce_dates.notna()
    if second_mask.any():
        frame.loc[second_mask, "ex_date"] = announce_dates[second_mask]
        logger.warning(
            "分红缺少除权日/派息日，%d 行回落到公告日（point-in-time 可能不严谨）",
            int(second_mask.sum()),
        )
    if fallback_mask.any():
        logger.debug("分红缺少除权日，%d 行回落到派息日", int(fallback_mask.sum()))
    # 兜底赋值可能引入 Timestamp 等类型，统一再收敛一次成 datetime.date
    frame["ex_date"] = _date_series(frame["ex_date"])

    frame = frame.dropna(subset=["ex_date"])
    # 只留现金分红（纯送股/转增方案的派息比例是 0 或空）
    frame = frame[frame["dps"].notna() & (frame["dps"] > 0)]
    if frame.empty:
        return _empty_dividends()

    # 同一除权日多方案 → dps 求和（都是真实现金分红），公告日/report_period 取最新
    grouped = (
        frame.groupby("ex_date", as_index=False)
        .agg(
            announce_date=("announce_date", "max"),
            dps=("dps", "sum"),
            report_period=("report_period", "last"),
        )
        .sort_values("ex_date", kind="mergesort")
        .reset_index(drop=True)
    )
    grouped["dps"] = pd.to_numeric(grouped["dps"], errors="coerce").astype("float64")
    return grouped[list(DIVIDEND_COLUMNS)]

def _prepare_dividends(dividends: pd.DataFrame | None) -> pd.DataFrame:
    """把分红表整理成 ``[ex_date, dps]``（去无效、合并同除权日、升序）。"""
    if dividends is None or len(dividends) == 0:
        return _empty_dividends()[["ex_date", "dps"]]
    frame = pd.DataFrame(dividends).copy()
    if "ex_date" not in frame.columns:
        if "date" in frame.columns:  # 宽容：别的表可能叫 date
            frame = frame.rename(columns={"date": "ex_date"})
        else:
            return _empty_dividends()[["ex_date", "dps"]]
    if "dps" not in frame.columns:
        return _empty_dividends()[["ex_date", "dps"]]
    frame["ex_date"] = _date_series(frame["ex_date"])
    frame["dps"] = pd.to_numeric(frame["dps"], errors="coerce")
    frame = frame.dropna(subset=["ex_date"])
    frame = frame[frame["dps"].notna() & (frame["dps"] > 0)]
    if frame.empty:
        return _empty_dividends()[["ex_date", "dps"]]
    frame = (
        frame.groupby("ex_date", as_index=False)["dps"]
        .sum()
        .sort_values("ex_date", kind="mergesort")
        .reset_index(drop=True)
    )
    return frame[["ex_date", "dps"]]

def dv_ttm_series(
    prices: pd.DataFrame,
    dividends: pd.DataFrame,
    window_days: int = DV_WINDOW_DAYS,
) -> pd.DataFrame:
    """按交易日算近 12 个月股息率（%）。

    口径::

        dv_ttm(d) = Σ{ dps | ex_date ∈ (d − window_days, d] } / close(d) × 100

    - **除权日对齐**：分红在除权日收盘后生效，所以 ``d >= ex_date`` 的交易日才开始计入
      （窗口左开右闭）；提前入库但还没除权的方案不会污染历史（point-in-time）；
    - 完全没有分红记录 → **0.0**（真实的"无股息"，不是数据缺失；A4 用 ``dv_ttm >= 4``
      过滤，0 不会误入）；
    - 没有有效收盘价（NaN/<=0）→ **NaN**；
    - **向量化**：把每笔分红转换成一个"开始计入 / 停止计入"的位置差分数组
      （``np.searchsorted`` + ``np.cumsum``），复杂度 O((n+m) log n)，不逐日 apply。

    Args:
        prices: ``[trade_date, close]``（可多一列 ``code``，见下）。
        dividends: ``[ex_date, dps]``（``normalize_dividends`` 或 ``dim_stock_dividend`` 的行）。
        window_days: 回看窗口（自然日），默认 365。

    Returns:
        ``[trade_date, dv_ttm]``，**行序与 ``prices`` 一致**。``trade_date`` 是
        ``datetime.date``。

    多只股票一起算：``prices`` 与 ``dividends`` **都带 ``code`` 列**时按 code 分组递归计算，
    输出最前面多一列 ``code``。
    """
    columns = ["trade_date", "dv_ttm"]
    if prices is None or len(prices) == 0:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    price_frame = pd.DataFrame(prices).copy()
    if "trade_date" not in price_frame.columns or "close" not in price_frame.columns:
        logger.error(
            "dv_ttm_series 的 prices 缺少 trade_date/close 列（现有：%s），返回空表",
            [str(c) for c in price_frame.columns],
        )
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    dividend_frame = pd.DataFrame(dividends) if dividends is not None else None
    if (
        "code" in price_frame.columns
        and dividend_frame is not None
        and "code" in dividend_frame.columns
    ):
        pieces: list[pd.DataFrame] = []
        for code, price_group in price_frame.groupby("code", sort=False):
            dividend_group = dividend_frame[dividend_frame["code"] == code]
            piece = dv_ttm_series(
                price_group.drop(columns=["code"]), dividend_group, window_days=window_days
            )
            piece.insert(0, "code", code)
            pieces.append(piece)
        if not pieces:
            return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
        return pd.concat(pieces, ignore_index=True)

    price_frame["trade_date"] = pd.to_datetime(price_frame["trade_date"], errors="coerce")
    price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
    price_frame = price_frame.dropna(subset=["trade_date"]).reset_index(drop=True)
    if price_frame.empty:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    price_frame["_order"] = np.arange(len(price_frame))
    ordered = price_frame.sort_values("trade_date", kind="mergesort").reset_index(drop=True)
    trade_days = ordered["trade_date"].to_numpy()
    count = len(ordered)

    # 差分数组：diff[start] += dps（从该交易日开始计入），diff[end] -= dps（窗口滑出）
    diff = np.zeros(count + 1, dtype="float64")
    prepared = _prepare_dividends(dividend_frame)
    if not prepared.empty:
        ex_days = pd.to_datetime(prepared["ex_date"]).to_numpy()
        dps = pd.to_numeric(prepared["dps"], errors="coerce").fillna(0.0).to_numpy()
        starts = np.searchsorted(trade_days, ex_days, side="left")
        ends = np.searchsorted(
            trade_days, ex_days + np.timedelta64(int(window_days), "D"), side="left"
        )
        # 有效事件：有现金分红，且除权日之后至少还有一个交易日
        valid = (dps != 0) & (starts < count)
        if valid.any():
            np.add.at(diff, starts[valid], dps[valid])
            np.add.at(diff, np.clip(ends[valid], 0, count), -dps[valid])
    rolling = np.cumsum(diff)[:count]

    close = pd.to_numeric(ordered["close"], errors="coerce").to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        dv = np.where(close > 0, rolling / close * 100.0, np.nan)
    dv = np.where(np.isfinite(dv), dv, np.nan)

    result = pd.DataFrame(
        {
            "trade_date": ordered["trade_date"].dt.date,
            "dv_ttm": dv,
            "_order": ordered["_order"],
        }
    )
    result = result.sort_values("_order", kind="mergesort").reset_index(drop=True)
    return result[columns]

__all__ = [
    "REPORT_LAG_DAYS_BY_MONTH",
    "REPORT_LAG_FALLBACK_DAYS",
    "DATASET",
    "DEFAULT_START_YEAR",
    "SYNC_BATCH_CODES",
    "SYNC_CHECKPOINT_EVERY",
    "COMPUTE_BATCH_CODES",
    "UPDATE_CHUNK_ROWS",
    "DIVIDEND_REFRESH_DAYS",
    "DIVIDEND_BULK_YEARS",
    "DIVIDEND_BULK_CHUNK",
    "DV_WINDOW_DAYS",
    "FIN_COLUMNS",
    "TTM_COLUMNS",
    "VALUATION_COLUMNS",
    "DIVIDEND_COLUMNS",
    "FIN_DATE_CANDIDATES",
    "FIN_DATE_PATTERNS",
    "FIN_EPS_CANDIDATES",
    "FIN_EPS_PATTERNS",
    "FIN_EPS_FALLBACK_CANDIDATES",
    "FIN_EPS_FALLBACK_PATTERNS",
    "FIN_BPS_CANDIDATES",
    "FIN_BPS_PATTERNS",
    "FIN_BPS_FALLBACK_CANDIDATES",
    "FIN_BPS_FALLBACK_PATTERNS",
    "FIN_ROE_CANDIDATES",
    "FIN_ROE_PATTERNS",
    "FIN_PAYOUT_CANDIDATES",
    "FIN_PAYOUT_PATTERNS",
    "FIN_REVENUE_CANDIDATES",
    "FIN_REVENUE_PATTERNS",
    "REVENUE_MIN_YUAN",
    "DIV_EX_DATE_CANDIDATES",
    "DIV_EX_DATE_PATTERNS",
    "DIV_PAY_DATE_CANDIDATES",
    "DIV_PAY_DATE_PATTERNS",
    "DIV_ANNOUNCE_CANDIDATES",
    "DIV_ANNOUNCE_PATTERNS",
    "DIV_DPS_PER_10_CANDIDATES",
    "DIV_DPS_PER_10_PATTERNS",
    "DIV_REPORT_PERIOD_CANDIDATES",
    "DIV_REPORT_PERIOD_PATTERNS",
    "ABSTRACT_METRIC_CANDIDATES",
    "ABSTRACT_METRIC_PATTERNS",
    "ABSTRACT_NET_PROFIT",
    "ABSTRACT_NET_PROFIT_PATTERNS",
    "ABSTRACT_REVENUE",
    "ABSTRACT_REVENUE_PATTERNS",
    "ABSTRACT_ROE",
    "ABSTRACT_ROE_PATTERNS",
    "ABSTRACT_TOTAL_SHARE",
    "ABSTRACT_TOTAL_SHARE_PATTERNS",
    "_EMPTY_TOKENS",
    "_resolve_column",
    "_num",
    "_numeric_column",
    "_date_series",
    "_parse_period",
    "_finite",
    "_empty_financials",
    "_empty_ttm",
    "_empty_valuation",
    "_empty_financials_ttm",
    "_coalesce_numeric",
    "normalize_financials",
    "eps_ttm_series",
    "revenue_ttm_series",
    "ps_ttm_values",
    "_prepare_financials",
    "build_valuation_frame",
    "_empty_dividends",
    "normalize_dividends",
    "_prepare_dividends",
    "dv_ttm_series",
]
