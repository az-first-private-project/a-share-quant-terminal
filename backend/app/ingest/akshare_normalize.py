"""V9 拆分：AKShare 列名兼容、单位换算与原始返回标准化。

由原 ``app/ingest/akshare_provider.py`` 拆分而来；provider 模块保留门面，
既有 ``from app.ingest.akshare_provider import ...`` 导入保持可用。
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

try:  # pragma: no cover —— 依赖是否安装由运行环境决定
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]

from app.core import config
from app.core.logging import get_logger
from app.ingest.em_board import CONCEPT_ALIASES

# 东财接口 TLS 指纹补丁：必须在调用任何 akshare 东财接口之前生效（见 em_tls_patch.py）
from app.ingest import em_tls_patch  # noqa: F401  (import 即生效)

logger = get_logger(__name__)

SHARES_PER_LOT = 100

INDEX_VOLUME_IN_LOTS = False

YI = 100_000_000.0

TENCENT_INDEX_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"

_TENCENT_INDEX_HEADERS = {
    "User-Agent": config.HTTP_USER_AGENT,
    "Referer": "https://gu.qq.com/",
}

COL_CODE = ("代码", "证券代码", "股票代码", "code", "symbol")

COL_NAME = ("名称", "证券简称", "股票简称", "name")

COL_DATE = ("日期", "trade_date", "date", "时间")

COL_OPEN = ("开盘", "今开", "open")

COL_CLOSE = ("收盘", "最新价", "现价", "close")

COL_HIGH = ("最高", "high")

COL_LOW = ("最低", "low")

COL_PRE_CLOSE = ("昨收", "昨收盘", "前收盘", "pre_close")

COL_PCT = ("涨跌幅", "涨跌幅(%)", "pct_chg", "pctChg")

COL_AMPLITUDE = ("振幅", "振幅(%)", "amplitude")

COL_VOLUME = ("成交量", "成交量(手)", "volume")

COL_AMOUNT = ("成交额", "成交额(元)", "amount")

COL_TURNOVER = ("换手率", "换手率(%)", "turnover_rate")

COL_PE = ("市盈率-动态", "市盈率(动态)", "市盈率", "pe_ttm")

COL_PB = ("市净率", "pb")

COL_PS = ("市销率", "ps_ttm")

COL_DV = ("股息率", "股息率(%)", "dv_ttm")

COL_MARKET_CAP = ("总市值", "market_cap")

COL_FLOAT_CAP = ("流通市值", "float_cap")

SECTOR_CODE = ("板块代码", "代码", "sector_code", "板块编号")

SECTOR_NAME = ("板块名称", "名称", "sector_name")

SECTOR_UP_COUNT = ("上涨家数", "up_count")

SECTOR_DOWN_COUNT = ("下跌家数", "down_count")

SECTOR_LEADER = ("领涨股票", "leading_stock")

SECTOR_LEADER_PCT = ("领涨股票-涨跌幅", "领涨股票涨跌幅", "leading_stock_pct")

MF_MAIN_T0 = ("今日主力净流入-净额", "主力净流入-净额", "主力净流入", "main_net_t0")

MF_MAIN_5D = ("5日主力净流入-净额", "5日主力净流入", "main_net_5d")

_EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"

_EM_UT = "b2884a393a59ad64002292a3e90d46a5"

_EM_MF_FS = ("m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,"
             "m:1+t:23+f:!2,m:0+t:81+s:2048+f:!2")

_EM_MF_SPEC = {
    # indicator -> (fid, fields, 源字段, 归一化列名)
    "今日": ("f62", "f12,f14,f62", "f62", "今日主力净流入-净额"),
    "5日": ("f164", "f12,f14,f164", "f164", "5日主力净流入-净额"),
}

_EM_MF_PAGE_CACHE: dict[tuple[str, str, int], list[dict]] = {}

_EM_MF_TOTAL_CACHE: dict[tuple[str, str], int] = {}

_EM_MF_PAGE_SIZE = 100

_EM_MF_TOLERANCE_RATIO = 0.005

_EM_MF_MIN_TOLERANCE = 5

_EM_BOARD_CONS_PAGE_SIZE = 100

_EM_BOARD_CONS_RETRIES = 4

def _prune_mf_cache(date_key: str) -> None:
    """丢弃非当日缓存，避免常驻进程按天累积（每天约 1 万个 dict）。"""
    for cache in (_EM_MF_PAGE_CACHE, _EM_MF_TOTAL_CACHE):
        stale = [key for key in cache if key[1] != date_key]
        for key in stale:
            cache.pop(key, None)

def _parse_cn_amount(value: Any) -> float | None:
    """解析中文数量字符串（"16.74亿" / "-810.40万" / "1234"）→ 元；无效返回 None。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "--"):
        return None
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None

MARGIN_DATE = ("信用交易日期", "交易日期", "日期", "date")

MARGIN_BALANCE = ("融资余额", "融资余额(元)", "融资余额(亿元)")

SHORT_BALANCE = ("融券余量金额", "融券余额", "融券余额(元)", "融券总额")

MARGIN_BUY = ("融资买入额", "融资买入额(元)", "融资买入额(亿元)")

NORTH_DATE = ("日期", "date")

NORTH_NET = ("当日成交净买额", "当日净买额", "北向资金净流入", "northward_net")

MARGIN_MAX_PUBLISHED_LAG_DAYS = 15

DELIST_CODE = ("公司代码", "证券代码", "股票代码", "代码")

DELIST_NAME = ("公司简称", "证券简称", "股票简称", "名称")

DELIST_DATE = ("终止上市日期", "退市日期", "终止上市日", "暂停上市日期")

LIST_DATE = ("上市日期", "上市时间", "上市日")

_EMPTY_STRINGS = {"", "-", "--", "—", "nan", "none", "null", "NaN", "None", "NoneType"}

def _pick(row: Any, candidates: Sequence[str], default: Any = None) -> Any:
    """从一行数据里按候选列名依次尝试取值，全部缺失返回 ``default``。

    Args:
        row: pandas Series / dict。
        candidates: 候选列名（按优先级）。
        default: 都没取到时的返回值。

    为什么需要它：AKShare 不同版本/不同接口的列名不稳定（"涨跌幅" vs "涨跌幅(%)"，
    "最新价" vs "收盘价"），硬编码 df["列名"] 一旦改版就 KeyError 崩全链路。
    优先级写死在调用处，缺列给 None，保证采集"能少不能崩"。
    """
    for column in candidates:
        try:
            value = row[column]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            continue
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        if isinstance(value, str) and value.strip() in _EMPTY_STRINGS:
            continue
        return value
    return default

def _num(value: Any) -> float | None:
    """把任意值转成 float；无法转换/NaN/Inf 返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if text in _EMPTY_STRINGS:
            return None
        if text.endswith("亿"):
            try:
                return float(text[:-1]) * YI
            except ValueError:
                return None
        if text.endswith("万"):
            try:
                return float(text[:-1]) * 10_000.0
            except ValueError:
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

def _to_int(value: Any) -> int | None:
    number = _num(value)
    if number is None:
        return None
    try:
        return int(round(number))
    except (OverflowError, ValueError):
        return None

def _to_date(value: Any) -> date | None:
    """把日期字符串/Timestamp/datetime 统一成 ``datetime.date``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    text = str(value).strip()
    if not text or text in _EMPTY_STRINGS:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        parsed = pd.to_datetime(text, errors="coerce")
        if parsed is not pd.NaT and not pd.isna(parsed):
            return parsed.date()
    except (TypeError, ValueError):
        pass
    return None

def _stock_code(value: Any) -> str | None:
    """统一 6 位代码（去空格、补零、剔除非数字）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in _EMPTY_STRINGS:
        return None
    # 处理 "SH600519" / "600519.SH" 这类带后缀格式
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(6)[-6:]

def _today_cn() -> date:
    """A 股所在的"今天"（Asia/Shanghai）。"""
    try:
        return datetime.now(ZoneInfo(config.TIMEZONE)).date()
    except Exception:  # pragma: no cover —— Windows 缺 tzdata 时兜底固定 +8
        return (datetime.now(timezone.utc) + pd.Timedelta(hours=8)).date()

def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))

SPOT_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "amplitude",
    "volume",
    "amount",
    "turnover_rate",
    "market_cap",
    "float_cap",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "dv_ttm",
)

HISTORY_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "amplitude",
    "volume",
    "amount",
    "turnover_rate",
    "adj_factor",
)

def _normalize_spot(
    df: pd.DataFrame | None, *, volume_in_lots: bool = True
) -> pd.DataFrame:
    """全市场快照归一化。

    数据源两套口径：
        - 东财 ``stock_zh_a_spot_em``：成交量单位**手** → ×100 转股；
        - 新浪 ``stock_zh_a_spot``（东财不可用时的备用源）：成交量单位**股**，
          传 ``volume_in_lots=False`` 不再换算（实测 amount/(volume*price)≈1 验证）。
    最新价即当日收盘价（盘后采集；盘中采集的值也按"最新价"落库，由 ``src`` 追溯）。
    """
    if df is None or df.empty:
        return _empty_frame(SPOT_COLUMNS)

    records: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        code = _stock_code(_pick(row, COL_CODE))
        if not code:
            continue
        close = _num(_pick(row, COL_CLOSE))
        pre_close = _num(_pick(row, COL_PRE_CLOSE))
        pct = _num(_pick(row, COL_PCT))
        if pct is None and close is not None and pre_close:
            pct = round((close / pre_close - 1) * 100, 3)
        volume_raw = _num(_pick(row, COL_VOLUME))
        records.append(
            {
                "code": code,
                "name": str(_pick(row, COL_NAME) or "").strip(),
                "open": _num(_pick(row, COL_OPEN)),
                "high": _num(_pick(row, COL_HIGH)),
                "low": _num(_pick(row, COL_LOW)),
                "close": close,
                "pre_close": pre_close,
                "pct_chg": pct,
                "amplitude": _num(_pick(row, COL_AMPLITUDE)),
                # 东财是"手"（×100 转股），新浪是"股"（原样）
                "volume": (
                    _to_int(volume_raw * SHARES_PER_LOT)
                    if volume_raw is not None and volume_in_lots
                    else _to_int(volume_raw)
                ),
                "amount": _num(_pick(row, COL_AMOUNT)),
                "turnover_rate": _num(_pick(row, COL_TURNOVER)),
                "market_cap": _num(_pick(row, COL_MARKET_CAP)),
                "float_cap": _num(_pick(row, COL_FLOAT_CAP)),
                "pe_ttm": _num(_pick(row, COL_PE)),
                "pb": _num(_pick(row, COL_PB)),
                "ps_ttm": _num(_pick(row, COL_PS)),
                "dv_ttm": _num(_pick(row, COL_DV)),
            }
        )
    return pd.DataFrame(records, columns=list(SPOT_COLUMNS))

def _normalize_stock_history(
    raw_df: pd.DataFrame | None,
    hfq_df: pd.DataFrame | None,
) -> pd.DataFrame:
    """个股历史日线归一化（不复权 + 后复权两表合并）。

    复权因子算法（回测正确性的核心）::

        adj_factor = 后复权收盘 / 不复权收盘

    用"收盘价比值"而不是"涨跌幅累乘"：前者是交易所口径的精确因子，
    后者在除权日会有四舍五入误差。原始收盘价为空/<=0 的日期（停牌、脏数据）跳过。

    成交量单位：东财历史行情 ``成交量`` 单位是**手**，×100 转股（硬约束 3）。
    """
    if raw_df is None or getattr(raw_df, "empty", True):
        return _empty_frame(HISTORY_COLUMNS)

    # 后复权收盘价映射：date -> hfq close
    hfq_map: dict[date, float] = {}
    if hfq_df is not None and not getattr(hfq_df, "empty", True):
        for row in hfq_df.to_dict("records"):
            trade_date = _to_date(_pick(row, COL_DATE))
            hfq_close = _num(_pick(row, COL_CLOSE))
            if trade_date is not None and hfq_close is not None:
                hfq_map[trade_date] = hfq_close

    records: list[dict[str, Any]] = []
    prev_close: float | None = None
    for row in raw_df.to_dict("records"):
        trade_date = _to_date(_pick(row, COL_DATE))
        if trade_date is None:
            continue
        close = _num(_pick(row, COL_CLOSE))
        if close is None or close <= 0:
            # 原始收盘价无效：该日数据不可信，直接跳过（不写脏行）
            logger.debug("跳过无效原始收盘价的交易日: %s close=%r", trade_date, close)
            continue

        high = _num(_pick(row, COL_HIGH))
        low = _num(_pick(row, COL_LOW))
        open_ = _num(_pick(row, COL_OPEN))
        pct = _num(_pick(row, COL_PCT))
        pre_close = _num(_pick(row, COL_PRE_CLOSE))
        if pre_close is None:
            if pct is not None and pct <= -100:
                pre_close = None
            elif pct is not None:
                pre_close = round(close / (1 + pct / 100.0), 3)
            else:
                pre_close = prev_close
        if pct is None and pre_close:
            pct = round((close / pre_close - 1) * 100, 3)
        amplitude = _num(_pick(row, COL_AMPLITUDE))
        if amplitude is None and high is not None and low is not None and pre_close:
            amplitude = round((high - low) / pre_close * 100, 3)
        volume_lots = _num(_pick(row, COL_VOLUME))
        hfq_close = hfq_map.get(trade_date)
        adj_factor = (
            round(hfq_close / close, 6) if hfq_close is not None and close > 0 else None
        )

        records.append(
            {
                "trade_date": trade_date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "pre_close": pre_close,
                "pct_chg": pct,
                "amplitude": amplitude,
                # 手 → 股
                "volume": _to_int(volume_lots * SHARES_PER_LOT)
                if volume_lots is not None
                else None,
                # 东财"成交额"单位已是元
                "amount": _num(_pick(row, COL_AMOUNT)),
                "turnover_rate": _num(_pick(row, COL_TURNOVER)),
                "adj_factor": adj_factor,
            }
        )
        prev_close = close

    if not records:
        return _empty_frame(HISTORY_COLUMNS)
    frame = pd.DataFrame(records, columns=list(HISTORY_COLUMNS))
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    return frame

def _normalize_sector_list(
    industry_df: pd.DataFrame | None,
    concept_df: pd.DataFrame | None,
) -> pd.DataFrame:
    """板块列表（行业 + 概念）归一化，同时用于**当日板块日线快照**。

    东财板块列表自带：最新价/涨跌幅/换手率/上涨家数/下跌家数/领涨股票 ——
    正好可以拼出当日的 ``fact_sector_daily`` 一行，不需要逐个板块再拉历史。
    """
    columns = (
        "sector_code",
        "sector_name",
        "sector_type",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "pct_chg",
        "amplitude",
        "volume",
        "amount",
        "turnover_rate",
        "up_count",
        "down_count",
        "leading_stock",
        "leading_stock_pct",
    )
    records: list[dict[str, Any]] = []
    for frame, sector_type in ((industry_df, "industry"), (concept_df, "concept")):
        if frame is None or getattr(frame, "empty", True):
            continue
        for row in frame.to_dict("records"):
            sector_code = _pick(row, SECTOR_CODE)
            sector_name = _pick(row, SECTOR_NAME)
            if not sector_code or not sector_name:
                continue
            close = _num(_pick(row, COL_CLOSE))
            pct = _num(_pick(row, COL_PCT))
            pre_close = (
                round(close / (1 + pct / 100.0), 3)
                if close is not None and pct is not None and pct > -100
                else None
            )
            records.append(
                {
                    "sector_code": str(sector_code).strip(),
                    "sector_name": str(sector_name).strip(),
                    "sector_type": sector_type,
                    "open": _num(_pick(row, COL_OPEN)),
                    "high": _num(_pick(row, COL_HIGH)),
                    "low": _num(_pick(row, COL_LOW)),
                    "close": close,
                    "pre_close": pre_close,
                    "pct_chg": pct,
                    "amplitude": _num(_pick(row, COL_AMPLITUDE)),
                    "volume": None,  # 板块列表没有成交量，不能拿"总市值"冒充（历史 bug #18）
                    "amount": None,  # 同上，amount 必须真的是成交额
                    "turnover_rate": _num(_pick(row, COL_TURNOVER)),
                    "up_count": _to_int(_pick(row, SECTOR_UP_COUNT)),
                    "down_count": _to_int(_pick(row, SECTOR_DOWN_COUNT)),
                    "leading_stock": _pick(row, SECTOR_LEADER),
                    "leading_stock_pct": _num(_pick(row, SECTOR_LEADER_PCT)),
                }
            )
    return pd.DataFrame(records, columns=list(columns))

def _normalize_sector_hist(
    df: pd.DataFrame | None,
    sector_code: str,
    sector_name: str,
    sector_type: str,
    trade_date: date,
) -> dict[str, Any] | None:
    """单个板块历史日线归一化，只取 ``trade_date`` 当天一行。"""
    if df is None or getattr(df, "empty", True):
        return None
    for row in df.to_dict("records"):
        row_date = _to_date(_pick(row, COL_DATE))
        if row_date != trade_date:
            continue
        close = _num(_pick(row, COL_CLOSE))
        volume_lots = _num(_pick(row, COL_VOLUME))
        return {
            "sector_code": sector_code,
            "sector_name": sector_name,
            "sector_type": sector_type,
            "open": _num(_pick(row, COL_OPEN)),
            "high": _num(_pick(row, COL_HIGH)),
            "low": _num(_pick(row, COL_LOW)),
            "close": close,
            "pre_close": _num(_pick(row, COL_PRE_CLOSE)),
            "pct_chg": _num(_pick(row, COL_PCT)),
            "amplitude": _num(_pick(row, COL_AMPLITUDE)),
            # 板块历史行情的成交量同样按"手"处理（新浪/东财口径）
            "volume": _to_int(volume_lots * SHARES_PER_LOT)
            if volume_lots is not None
            else None,
            "amount": _num(_pick(row, COL_AMOUNT)),
            "turnover_rate": _num(_pick(row, COL_TURNOVER)),
            "up_count": None,
            "down_count": None,
            "leading_stock": None,
            "leading_stock_pct": None,
        }
    return None

SECTOR_MEMBER_COLUMNS = ("sector_code", "sector_name", "sector_type", "stock_code", "stock_name")

_EM_BOARD_CODE_CACHE: dict[str, tuple[date, dict[str, str], dict[str, str]]] = {}

def normalize_board_name(name: str) -> str:
    """板块名归一化：去括号注释/空格/罗马数字后缀/通用尾缀，再忽略大小写。

    THS 与东财的板块命名并不一致（如 ``MR(混合现实)`` vs ``MR``、
    ``证券Ⅱ`` vs ``证券``），精确匹配会漏掉大量实际存在的板块。
    """
    text = str(name or "").strip()
    text = re.sub(r"[（(].*?[）)]", "", text).replace(" ", "").replace("　", "")
    text = re.sub(r"[ⅡⅢⅣ]$", "", text)
    for suffix in ("概念", "行业", "板块", "指数"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text.upper()

def build_board_code_maps(
    frame: pd.DataFrame | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """东财板块清单 → (精确名→BK代码, 归一化名→BK代码)。

    归一化后出现一对多歧义时直接剔除该键，避免把不同板块的成分串到一起。
    """
    exact: dict[str, str] = {}
    normalized: dict[str, str] = {}
    ambiguous: set[str] = set()
    if frame is None or frame.empty:
        return exact, normalized
    if "板块名称" not in frame.columns or "板块代码" not in frame.columns:
        return exact, normalized
    for raw_name, raw_code in zip(frame["板块名称"], frame["板块代码"]):
        name = str(raw_name or "").strip()
        code = str(raw_code or "").strip()
        if not name or not code:
            continue
        exact[name] = code
        key = normalize_board_name(name)
        if key in normalized and normalized[key] != code:
            ambiguous.add(key)
        else:
            normalized[key] = code
    for key in ambiguous:
        normalized.pop(key, None)
    return exact, normalized

def resolve_em_board_code(
    exact: dict[str, str],
    normalized: dict[str, str],
    sector_name: str,
    sector_type: str,
) -> str | None:
    """THS 板块名 → 东财 BK 代码：精确 → 归一化 → 概念同义词。

    同义词只对概念板块生效，且只收录语义相同的改名/缩写（见
    ``em_board.CONCEPT_ALIASES``）；解析不到返回 None，由调用方跳过。
    """
    code = exact.get(sector_name) or normalized.get(normalize_board_name(sector_name))
    if code or sector_type != "concept":
        return code
    for alias in CONCEPT_ALIASES.get(sector_name, []):
        code = exact.get(alias) or normalized.get(normalize_board_name(alias))
        if code:
            return code
    return None

def _normalize_sector_members(
    df: pd.DataFrame | None,
    sector_code: str,
    sector_name: str,
    sector_type: str,
) -> pd.DataFrame:
    """板块成分股归一化（代码 + 名称）。"""
    if df is None or getattr(df, "empty", True):
        return _empty_frame(SECTOR_MEMBER_COLUMNS)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in df.to_dict("records"):
        code = _stock_code(_pick(row, COL_CODE))
        if not code or code in seen:
            continue
        seen.add(code)
        records.append(
            {
                "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_type": sector_type,
                "stock_code": code,
                "stock_name": str(_pick(row, COL_NAME) or "").strip() or None,
            }
        )
    return pd.DataFrame(records, columns=list(SECTOR_MEMBER_COLUMNS))

MONEYFLOW_COLUMNS = ("code", "main_net_t0", "main_net_5d")

def _normalize_moneyflow(
    today_df: pd.DataFrame | None,
    five_day_df: pd.DataFrame | None,
) -> pd.DataFrame:
    """个股资金流归一化：今日主力净流入 + 5 日主力净流入（单位：元）。"""
    records: dict[str, dict[str, Any]] = {}

    def _absorb(frame: pd.DataFrame | None, column_candidates: Sequence[str],
                target: str) -> None:
        if frame is None or getattr(frame, "empty", True):
            return
        for row in frame.to_dict("records"):
            code = _stock_code(_pick(row, COL_CODE))
            if not code:
                continue
            value = _num(_pick(row, column_candidates))
            item = records.setdefault(code, {"code": code, "main_net_t0": None,
                                             "main_net_5d": None})
            if value is not None:
                item[target] = value

    _absorb(today_df, MF_MAIN_T0, "main_net_t0")
    _absorb(five_day_df, MF_MAIN_5D, "main_net_5d")
    if not records:
        return _empty_frame(MONEYFLOW_COLUMNS)
    return pd.DataFrame(list(records.values()), columns=list(MONEYFLOW_COLUMNS))

def _margin_map_sse(
    df: pd.DataFrame | None, default_date: date | None = None
) -> dict[date, dict[str, float | None]]:
    """上交所两融：金额单位**元** → 亿元。

    SSE 返回带 ``信用交易日期`` 列；``default_date`` 仅在源缺日期列时兜底。
    """
    result: dict[date, dict[str, float | None]] = {}
    if df is None or getattr(df, "empty", True):
        return result
    for row in df.to_dict("records"):
        trade_date = _to_date(_pick(row, MARGIN_DATE)) or default_date
        if trade_date is None:
            continue
        margin = _num(_pick(row, MARGIN_BALANCE))
        short = _num(_pick(row, SHORT_BALANCE))
        buy = _num(_pick(row, MARGIN_BUY))
        result[trade_date] = {
            "margin_balance": round(margin / YI, 4) if margin is not None else None,
            "short_balance": round(short / YI, 4) if short is not None else None,
            # 买方金额统一存元；上交所原值即元
            "margin_buy": buy,
        }
    return result

def _margin_map_szse(
    df: pd.DataFrame | None, default_date: date | None = None
) -> dict[date, dict[str, float | None]]:
    """深交所两融：金额单位**已是亿元**，不换算。

    深交所 ``stock_margin_szse(date=...)`` 只返回**单日一行且没有日期列**，
    因此必须用调用方传入的 ``default_date`` 补齐。
    """
    result: dict[date, dict[str, float | None]] = {}
    if df is None or getattr(df, "empty", True):
        return result
    for row in df.to_dict("records"):
        trade_date = _to_date(_pick(row, MARGIN_DATE)) or default_date
        if trade_date is None:
            continue
        result[trade_date] = {
            "margin_balance": _num(_pick(row, MARGIN_BALANCE)),
            "short_balance": _num(_pick(row, SHORT_BALANCE)),
            # 深交所融资买入额单位是亿元，统一换成元
            "margin_buy": (
                round(buy * YI, 2) if (buy := _num(_pick(row, MARGIN_BUY))) is not None else None
            ),
        }
    return result

def _normalize_bond_yields(df: pd.DataFrame | None) -> pd.DataFrame:
    """中债收益率 → (trade_date, cn_1y, cn_10y, term_spread)。

    只保留「中债国债收益率曲线」；1Y/10Y 缺失时保留 NULL。
    """
    columns = ["trade_date", "cn_1y", "cn_10y", "term_spread"]
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame([], columns=columns)
    rows: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        if str(_pick(row, ("曲线名称",)) or "").strip() != "中债国债收益率曲线":
            continue
        trade_date = _to_date(_pick(row, ("日期", "date")))
        if trade_date is None:
            continue
        cn_1y = _num(_pick(row, ("1年", "1Y")))
        cn_10y = _num(_pick(row, ("10年", "10Y")))
        rows.append({
            "trade_date": trade_date,
            "cn_1y": cn_1y,
            "cn_10y": cn_10y,
            "term_spread": (
                round(cn_10y - cn_1y, 4)
                if cn_1y is not None and cn_10y is not None else None
            ),
        })
    if not rows:
        return pd.DataFrame([], columns=columns)
    return (
        pd.DataFrame(rows, columns=columns)
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )

def _normalize_margin_buy_sse(df: pd.DataFrame | None) -> pd.DataFrame:
    """上交所融资买入额 → (trade_date, margin_buy_amount)，单位元。"""
    columns = ["trade_date", "margin_buy_amount"]
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame([], columns=columns)
    rows: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        trade_date = _to_date(_pick(row, MARGIN_DATE))
        if trade_date is None:
            continue
        rows.append({
            "trade_date": trade_date,
            "margin_buy_amount": _num(_pick(row, MARGIN_BUY)),
        })
    if not rows:
        return pd.DataFrame([], columns=columns)
    return (
        pd.DataFrame(rows, columns=columns)
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )

def _normalize_margin_buy_szse(
    df: pd.DataFrame | None, trade_date: date
) -> pd.DataFrame:
    """深交所融资买入额 → (trade_date, margin_buy_amount)，亿元 → 元。"""
    columns = ["trade_date", "margin_buy_amount"]
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame([], columns=columns)
    row = df.to_dict("records")[0]
    buy = _num(_pick(row, MARGIN_BUY))
    return pd.DataFrame(
        [{
            "trade_date": trade_date,
            "margin_buy_amount": round(buy * YI, 2) if buy is not None else None,
        }],
        columns=columns,
    )

def _margin_effective_date(
    sse_map: dict[date, dict[str, float | None]],
    requested_date: date,
    max_lag_days: int = MARGIN_MAX_PUBLISHED_LAG_DAYS,
) -> date | None:
    """区间内已披露的最新两融日期；无数据或过旧（超过 ``max_lag_days``）返回 None。

    过旧判定不可省略：akshare 的 ``stock_margin_sse()`` 无参调用默认区间截止
    2023-09-22，网络抖动触发兜底时会把三年前的数据当成"最新已披露日"。
    """
    published = [day for day in sse_map if day <= requested_date]
    if not published:
        return None
    latest = max(published)
    if (requested_date - latest).days > max_lag_days:
        return None
    return latest

def _normalize_north(df: pd.DataFrame | None) -> dict[date, float | None]:
    """北向资金：``当日成交净买额`` 单位亿元；可能长期为空（2024-08 后披露口径变化），
    属于预期内，**任何情况下不得报错阻断采集**。"""
    result: dict[date, float | None] = {}
    if df is None or getattr(df, "empty", True):
        return result
    for row in df.to_dict("records"):
        trade_date = _to_date(_pick(row, NORTH_DATE))
        if trade_date is None:
            continue
        result[trade_date] = _num(_pick(row, NORTH_NET))
    return result

INDEX_COLUMNS = (
    "index_code",
    "index_name",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "volume",
    "amount",
)

def _normalize_index(
    df: pd.DataFrame | None,
    index_code: str,
    index_name: str,
) -> pd.DataFrame:
    """指数日线归一化（新浪 ``stock_zh_index_daily``）。

    新浪返回全历史，这里先按日期升序算 ``pre_close``（前一行收盘），再由编排层
    过滤目标交易日 —— 只抓一天也必须保留前一行才能算涨跌幅。
    """
    if df is None or getattr(df, "empty", True):
        return _empty_frame(INDEX_COLUMNS)
    records: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        trade_date = _to_date(_pick(row, COL_DATE))
        if trade_date is None:
            continue
        volume_raw = _num(_pick(row, COL_VOLUME))
        records.append(
            {
                "index_code": index_code,
                "index_name": index_name,
                "trade_date": trade_date,
                "open": _num(_pick(row, COL_OPEN)),
                "high": _num(_pick(row, COL_HIGH)),
                "low": _num(_pick(row, COL_LOW)),
                "close": _num(_pick(row, COL_CLOSE)),
                "volume": _to_int(volume_raw * SHARES_PER_LOT)
                if volume_raw is not None and INDEX_VOLUME_IN_LOTS
                else _to_int(volume_raw),
                "amount": _num(_pick(row, COL_AMOUNT)),
            }
        )
    if not records:
        return _empty_frame(INDEX_COLUMNS)
    frame = pd.DataFrame(records, columns=list(INDEX_COLUMNS))
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    frame["pre_close"] = frame["close"].shift(1)
    close_series = pd.to_numeric(frame["close"], errors="coerce")
    pre_close_series = pd.to_numeric(frame["pre_close"], errors="coerce")
    valid = (
        close_series.notna()
        & pre_close_series.notna()
        & (pre_close_series > 0)
    )
    pct_series = (close_series / pre_close_series - 1) * 100
    frame["pct_chg"] = pct_series.where(valid).round(3)
    return frame[list(INDEX_COLUMNS)]

def _normalize_csi_index(
    df: pd.DataFrame | None,
    index_code: str,
    index_name: str,
) -> pd.DataFrame:
    """中证指数历史（``stock_zh_index_hist_csindex``）归一化。

    列名：日期/开盘/最高/最低/收盘/成交量/成交金额。
    与新浪指数保持同一 ``INDEX_COLUMNS`` 口径，成交额单位沿用源返回值
    （该字段只做展示，不参与比价 R 的计算）。
    """
    if df is None or getattr(df, "empty", True):
        return _empty_frame(INDEX_COLUMNS)
    records: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        trade_date = _to_date(_pick(row, ("日期", "date")))
        close = _num(_pick(row, ("收盘", "close")))
        if trade_date is None or close is None:
            continue
        records.append({
            "index_code": index_code,
            "index_name": index_name,
            "trade_date": trade_date,
            "open": _num(_pick(row, ("开盘", "open"))),
            "high": _num(_pick(row, ("最高", "high"))),
            "low": _num(_pick(row, ("最低", "low"))),
            "close": close,
            "volume": _to_int(_num(_pick(row, ("成交量", "volume")))),
            "amount": _num(_pick(row, ("成交金额", "amount"))),
        })
    if not records:
        return _empty_frame(INDEX_COLUMNS)
    frame = pd.DataFrame(records, columns=[
        "index_code", "index_name", "trade_date", "open", "high", "low", "close",
        "volume", "amount",
    ])
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    frame["pre_close"] = frame["close"].shift(1)
    close_series = pd.to_numeric(frame["close"], errors="coerce")
    pre_close_series = pd.to_numeric(frame["pre_close"], errors="coerce")
    valid = close_series.notna() & pre_close_series.notna() & (pre_close_series > 0)
    frame["pct_chg"] = ((close_series / pre_close_series - 1) * 100).where(valid).round(3)
    return frame[list(INDEX_COLUMNS)]

def _normalize_tencent_index(
    rows: Sequence[Sequence[Any]] | None,
    index_code: str,
    index_name: str,
    trade_date: date,
) -> pd.DataFrame:
    """腾讯指数日线归一化：只保留目标日一行（前收由前一条记录算出）。

    行格式 ``[日期, 开盘, 收盘, 最高, 最低, 成交量(手)]``，成交量 ×100 转"股"
    与新浪口径一致；接口不返回成交额，``amount`` 留空。
    """
    records: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        volume = _num(row[5])
        records.append(
            {
                "date": row[0],
                "open": _num(row[1]),
                "close": _num(row[2]),
                "high": _num(row[3]),
                "low": _num(row[4]),
                "volume": volume * SHARES_PER_LOT if volume is not None else None,
            }
        )
    normalized = _normalize_index(pd.DataFrame(records), index_code, index_name)
    if normalized.empty:
        return _empty_frame(INDEX_COLUMNS)
    target = normalized[normalized["trade_date"] == trade_date]
    return target.reset_index(drop=True)

def _normalize_us_index(
    df: pd.DataFrame | None,
    index_code: str,
    index_name: str,
    trade_date: date,
) -> dict[str, Any] | None:
    """美股指数（yfinance）归一化：只取 ``trade_date`` 当天一行。

    美股日期按交易所本地日（yfinance 返回 tz-aware DatetimeIndex，这里转本地日期）。
    成交量单位是**股**（美股没有"手"），不乘 100。
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        frame = df.copy()
        index = pd.to_datetime(frame.index, errors="coerce")
        if getattr(index, "tz", None) is not None:
            index = index.tz_localize(None)
        frame["trade_date"] = index.date
    except Exception:  # noqa: BLE001 —— yfinance 结构不稳定，失败就跳过
        return None
    frame = frame.reset_index(drop=True)
    frame = frame.sort_values("trade_date")
    pre_close = None
    for row in frame.to_dict("records"):
        row_date = _to_date(_pick(row, ("trade_date", "Date", "日期")))
        close = _num(_pick(row, ("Close", "Adj Close", "收盘")))
        if row_date == trade_date:
            return {
                "index_code": index_code,
                "index_name": index_name,
                "trade_date": row_date,
                "open": _num(_pick(row, ("Open", "开盘"))),
                "high": _num(_pick(row, ("High", "最高"))),
                "low": _num(_pick(row, ("Low", "最低"))),
                "close": close,
                "pre_close": pre_close,
                "pct_chg": round((close / pre_close - 1) * 100, 3)
                if close is not None and pre_close
                else None,
                "volume": _to_int(_pick(row, ("Volume", "成交量"))),
                "amount": None,
            }
        if close is not None:
            pre_close = close
    return None

CALENDAR_COLUMNS = ("trade_date",)

def _normalize_calendar(df: pd.DataFrame | None) -> pd.DataFrame:
    """交易日历归一化：``ak.tool_trade_date_hist_sina()`` 的 trade_date 列。"""
    if df is None or getattr(df, "empty", True):
        return _empty_frame(CALENDAR_COLUMNS)
    dates: set[date] = set()
    for row in df.to_dict("records"):
        value = _pick(row, ("trade_date", "日期", "date"))
        parsed = _to_date(value)
        if parsed is not None:
            dates.add(parsed)
    return pd.DataFrame({"trade_date": sorted(dates)}, columns=list(CALENDAR_COLUMNS))

__all__ = [
    "SHARES_PER_LOT",
    "INDEX_VOLUME_IN_LOTS",
    "YI",
    "TENCENT_INDEX_URL",
    "_TENCENT_INDEX_HEADERS",
    "COL_CODE",
    "COL_NAME",
    "COL_DATE",
    "COL_OPEN",
    "COL_CLOSE",
    "COL_HIGH",
    "COL_LOW",
    "COL_PRE_CLOSE",
    "COL_PCT",
    "COL_AMPLITUDE",
    "COL_VOLUME",
    "COL_AMOUNT",
    "COL_TURNOVER",
    "COL_PE",
    "COL_PB",
    "COL_PS",
    "COL_DV",
    "COL_MARKET_CAP",
    "COL_FLOAT_CAP",
    "SECTOR_CODE",
    "SECTOR_NAME",
    "SECTOR_UP_COUNT",
    "SECTOR_DOWN_COUNT",
    "SECTOR_LEADER",
    "SECTOR_LEADER_PCT",
    "MF_MAIN_T0",
    "MF_MAIN_5D",
    "_EM_CLIST_URL",
    "_EM_UT",
    "_EM_MF_FS",
    "_EM_MF_SPEC",
    "_EM_MF_PAGE_CACHE",
    "_EM_MF_TOTAL_CACHE",
    "_EM_MF_PAGE_SIZE",
    "_EM_MF_TOLERANCE_RATIO",
    "_EM_MF_MIN_TOLERANCE",
    "_EM_BOARD_CONS_PAGE_SIZE",
    "_EM_BOARD_CONS_RETRIES",
    "_prune_mf_cache",
    "_parse_cn_amount",
    "MARGIN_DATE",
    "MARGIN_BALANCE",
    "SHORT_BALANCE",
    "MARGIN_BUY",
    "NORTH_DATE",
    "NORTH_NET",
    "MARGIN_MAX_PUBLISHED_LAG_DAYS",
    "DELIST_CODE",
    "DELIST_NAME",
    "DELIST_DATE",
    "LIST_DATE",
    "_EMPTY_STRINGS",
    "_pick",
    "_num",
    "_to_int",
    "_to_date",
    "_stock_code",
    "_today_cn",
    "_empty_frame",
    "SPOT_COLUMNS",
    "HISTORY_COLUMNS",
    "_normalize_spot",
    "_normalize_stock_history",
    "_normalize_sector_list",
    "_normalize_sector_hist",
    "SECTOR_MEMBER_COLUMNS",
    "_EM_BOARD_CODE_CACHE",
    "normalize_board_name",
    "build_board_code_maps",
    "resolve_em_board_code",
    "_normalize_sector_members",
    "MONEYFLOW_COLUMNS",
    "_normalize_moneyflow",
    "_margin_map_sse",
    "_margin_map_szse",
    "_normalize_bond_yields",
    "_normalize_margin_buy_sse",
    "_normalize_margin_buy_szse",
    "_margin_effective_date",
    "_normalize_north",
    "INDEX_COLUMNS",
    "_normalize_index",
    "_normalize_csi_index",
    "_normalize_tencent_index",
    "_normalize_us_index",
    "CALENDAR_COLUMNS",
    "_normalize_calendar",
]
