"""
AKShare 主数据源实现（方案 5：AKShare 主采集 + Baostock 兜底校验）
====================================================================
合规边界（写进代码，避免后来者"顺手"加反爬）：

    1. AKShare 本质是对公开页面的二次封装，不是交易所授权接口；
    2. 必须限频：全市场快照 ≤ 1 次/分钟、历史回补 ≤ 2 请求/秒（由
       ``service.py`` 的 ``SNAPSHOT_LIMITER`` / ``HISTORY_LIMITER`` 统一节流）；
    3. 固定 UA（``config.HTTP_USER_AGENT``），**不实现任何反爬绕过**（无代理池/验证码）；
    4. 数据仅个人研究，不得对外分发或商用。

列名兼容策略（AKShare 接口文档与实际返回经常不一致）：

    所有取值都走 :func:`_pick`（候选列名逐个试）→ 缺列返回 None，**绝不 KeyError**。
    原始数值统一经 :func:`_num` 转 float（"--"、""、NaN 都变 None），不会把脏字符串写进库。

单位口径（入库前在这里一次性统一，硬约束 3）：

    - 成交量：东财/新浪的"手"→ **股**（×100）；
    - 成交额 / 市值 / 资金流：已是 **元**，不再换算；
    - 两融：上交所是元（/1e8 → 亿元）、深交所已是亿元；
    - 北向"当日成交净买额"：亿元。

akshare 未安装时本模块仍可 import（``ak = None``），``available()`` 返回 False，
方便 CI 与离线开发。
"""

from __future__ import annotations

import math
import time
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

try:  # pragma: no cover —— 依赖是否安装由运行环境决定
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]

from app.core import config
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.ingest.base import BaseProvider, FetchResult

# 东财接口 TLS 指纹补丁：必须在调用任何 akshare 东财接口之前生效（见 em_tls_patch.py）
from app.ingest import em_tls_patch  # noqa: F401  (import 即生效)

logger = get_logger(__name__)

from app.ingest.akshare_normalize import *  # noqa: F401,F403

class AkshareProvider(BaseProvider):
    """AKShare 主数据源（方案 5：主采集）。"""

    name = "akshare"

    def __init__(self) -> None:
        self._throttle_interval = 1.0 / max(config.HISTORY_REQUESTS_PER_SEC, 0.1)
        self._last_call_at = 0.0
        self._ua_installed = False

    # ---------- 可用性 / 合规 ----------
    def available(self) -> bool:
        """akshare 已安装且允许联网时为 True（CI 离线自动降级）。"""
        return ak is not None and bool(config.INGEST_NETWORK_ENABLED)

    def _install_user_agent(self) -> None:
        """固定 UA（合规要求）：best-effort 修改 requests 默认 UA。

        AKShare 内部大多用 ``requests.get`` 且不传 UA，requests 会回落到
        ``requests.sessions.default_headers()``。这里替换默认 header 工厂，
        让整个采集进程带 ``config.HTTP_USER_AGENT``。**不做任何反爬绕过**。
        失败（requests 版本差异）不影响采集，只是 UA 不生效。
        """
        if self._ua_installed:
            return
        self._ua_installed = True
        try:
            import requests
            import requests.sessions

            original = requests.utils.default_headers

            def _headers_with_ua() -> dict[str, str]:
                headers = dict(original())
                headers["User-Agent"] = config.HTTP_USER_AGENT
                return headers

            requests.utils.default_headers = _headers_with_ua  # type: ignore[assignment]
            requests.sessions.default_headers = _headers_with_ua  # type: ignore[attr-defined]
            logger.debug("已设置固定 User-Agent：%s", config.HTTP_USER_AGENT)
        except Exception:  # noqa: BLE001 —— UA 设置失败不影响主流程
            logger.debug("设置固定 User-Agent 失败（忽略）", exc_info=True)

    def _throttle(self, interval: float | None = None) -> None:
        """同步节流（Provider 内多接口循环之间使用；跨任务节流在编排层做）。"""
        wait = self._throttle_interval if interval is None else interval
        now = time.monotonic()
        sleep_for = self._last_call_at + wait - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._last_call_at = time.monotonic()

    def _safe_call(self, func: Any, *args: Any, what: str = "", **kwargs: Any) -> Any:
        """统一包裹一次 AKShare 调用：异常 → ProviderError（上层重试/降级）。

        约定：``NotImplementedError`` 原样抛出（降级链据此判断"能力不支持"），
        其余一切异常（requests/JSON/KeyError/TypeError）都包装成 ProviderError，
        保证编排层只需要处理一种异常类型。
        """
        if ak is None:  # pragma: no cover —— available() 已拦截，这里防御
            raise ProviderError("AKShare 未安装", detail={"provider": "akshare"})
        if not config.INGEST_NETWORK_ENABLED:  # pragma: no cover
            raise ProviderError(
                "INGEST_NETWORK_ENABLED=0，禁止联网采集",
                detail={"provider": "akshare"},
            )

        self._install_user_agent()
        label = what or getattr(func, "__name__", "akshare_call")
        try:
            return func(*args, **kwargs)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 外部库异常类型不可控，统一收敛
            raise ProviderError(
                f"AKShare 调用失败[{label}]: {type(exc).__name__}: {exc}",
                detail={"provider": "akshare", "what": label,
                        "exception": type(exc).__name__},
            ) from exc

    def _fetch_one(self, frame: Any, what: str, **kwargs: Any) -> pd.DataFrame:
        """调用并校验返回类型是 DataFrame。"""
        result = self._safe_call(frame, what=what, **kwargs)
        if result is None:
            raise ProviderError(f"AKShare 返回空对象[{what}]", detail={"provider": "akshare"})
        if not isinstance(result, pd.DataFrame):
            raise ProviderError(
                f"AKShare 返回类型异常[{what}]: {type(result).__name__}",
                detail={"provider": "akshare"},
            )
        return result

    # ---------- A7 交易日历 ----------
    def fetch_trade_calendar(self, start: date, end: date) -> FetchResult:
        started = time.perf_counter()
        raw = self._fetch_one(ak.tool_trade_date_hist_sina, "tool_trade_date_hist_sina")
        frame = _normalize_calendar(raw)
        if not frame.empty:
            frame = frame[
                (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
            ].reset_index(drop=True)
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            elapsed_ms=elapsed,
            detail={"data": frame, "source": self.name, "range": [start.isoformat(),
                                                                   end.isoformat()]},
        )

    # ---------- A2 全市场快照 ----------
    def fetch_daily_snapshot(self, trade_date: date) -> FetchResult:
        """一次请求拿全市场快照。

        主源东财 ``stock_zh_a_spot_em``（字段最全：市值/PE/PB/换手）；
        失败时降级到新浪 ``stock_zh_a_spot``（只有 OHLCV，但东财接口被网络/风控
        拦截时仍能保证"全市场当日行情"这条主链不断）。
        """
        started = time.perf_counter()
        spot_source = "em"
        try:
            raw = self._fetch_one(ak.stock_zh_a_spot_em, "stock_zh_a_spot_em")
            frame = _normalize_spot(raw, volume_in_lots=True)
        except ProviderError as exc:
            logger.warning("东财全市场快照失败，降级新浪源：%s", exc)
            raw = self._fetch_one(ak.stock_zh_a_spot, "stock_zh_a_spot")
            frame = _normalize_spot(raw, volume_in_lots=False)
            spot_source = "sina"
        if frame.empty:
            raise ProviderError(
                "全市场快照归一化后为空（东财与新浪均无有效数据）",
                detail={"provider": self.name},
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("全市场快照 %s：%d 行（来源 %s）", trade_date, len(frame), spot_source)
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            elapsed_ms=elapsed,
            detail={"data": frame, "source": self.name,
                    "spot_source": spot_source,
                    "trade_date": trade_date.isoformat()},
        )

    # ---------- A3 历史日线 ----------
    def fetch_stock_history(self, code: str, start: date, end: date) -> FetchResult:
        """不复权 + 后复权两次请求，合并出 adj_factor。

        两次请求之间 ``_throttle()`` 一次，满足"历史回补 ≤ 2 请求/秒"的合规要求。
        """
        started = time.perf_counter()
        code = _stock_code(code) or str(code)
        start_date = start.strftime("%Y%m%d")
        end_date = end.strftime("%Y%m%d")

        raw = self._fetch_one(
            ak.stock_zh_a_hist,
            "stock_zh_a_hist(raw)",
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
        self._throttle()
        hfq = self._fetch_one(
            ak.stock_zh_a_hist,
            "stock_zh_a_hist(hfq)",
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="hfq",
        )
        frame = _normalize_stock_history(raw, hfq)
        elapsed = int((time.perf_counter() - started) * 1000)
        adj_missing = int(frame["adj_factor"].isna().sum()) if len(frame) else 0
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            failed=adj_missing,
            elapsed_ms=elapsed,
            detail={
                "data": frame,
                "source": self.name,
                "code": code,
                "range": [start.isoformat(), end.isoformat()],
                "adj_factor_missing": adj_missing,
            },
        )

    # ---------- A1 股票池 ----------
    def fetch_stock_universe(self) -> FetchResult:
        """全市场标的：代码/名称/市值/行业/上市退市日期/股本。

        数据来源（按可靠性组合，任一环节失败都尽量降级而不是整单失败）：
            1. ``stock_info_a_code_name``   —— 代码 + 名称（基础清单；失败时用交易所名单兜底）；
            2. ``stock_info_sh_name_code`` / ``stock_info_sz_name_code``
               —— 上市日期 + 深市总股本/流通股本（实测列名：证券代码/上市日期；
               深市是 A股代码/A股上市日期/A股总股本/A股流通股本）；
            3. 全市场快照：东财 ``stock_zh_a_spot_em``（含市值/PE/PB），
               失败降级新浪 ``stock_zh_a_spot``（只有 OHLCV，且新浪快照含北交所代码）；
            4. ``stock_board_industry_name_em`` + 板块成分 —— 行业归属（失败不阻断）；
            5. ``stock_info_sh_delist`` / ``stock_info_sz_delist`` —— 退市股（失败不阻断）。
        """
        started = time.perf_counter()
        records: dict[str, dict[str, Any]] = {}
        base_source = "a_code_name"

        try:
            base = self._fetch_one(ak.stock_info_a_code_name, "stock_info_a_code_name")
            for row in base.to_dict("records"):
                code = _stock_code(_pick(row, COL_CODE))
                if not code:
                    continue
                records[code] = {
                    "code": code,
                    "name": str(_pick(row, COL_NAME) or "").strip(),
                }
        except ProviderError as exc:
            base_source = "exchange_lists"
            logger.warning(
                "基础清单 stock_info_a_code_name 失败，降级用交易所名单+行情快照：%s", exc
            )

        # 交易所名单：上市日期 + 深市股本（顺带可用于补基础清单缺失的标的）
        listing_rows = self._fetch_cn_stock_lists()
        listing_map = {row["code"]: row for row in listing_rows}
        if not records:
            for code, extra in listing_map.items():
                records[code] = {"code": code, **{
                    key: value for key, value in extra.items() if key != "code"
                }}
        else:
            for code, item in records.items():
                extra = listing_map.get(code)
                if not extra:
                    continue
                for key in ("list_date", "total_share", "float_share"):
                    if extra.get(key) is not None:
                        item[key] = extra[key]

        # 全市场快照：东财 → 新浪 两级降级
        spot_source: str | None = None
        try:
            try:
                spot_raw = self._fetch_one(ak.stock_zh_a_spot_em, "stock_zh_a_spot_em")
                spot = _normalize_spot(spot_raw, volume_in_lots=True)
                spot_source = "em"
            except ProviderError as exc:
                logger.warning("东财快照失败，股票池降级新浪快照：%s", exc)
                spot_raw = self._fetch_one(ak.stock_zh_a_spot, "stock_zh_a_spot")
                spot = _normalize_spot(spot_raw, volume_in_lots=False)
                spot_source = "sina"
            for row in spot.to_dict("records"):
                code = row["code"]
                item = records.setdefault(
                    code, {"code": code, "name": row.get("name") or ""}
                )
                for key in ("name", "close", "market_cap", "float_cap",
                            "turnover_rate", "pe_ttm", "pb"):
                    if row.get(key) is not None:
                        item[key] = row[key]
        except ProviderError as exc:
            logger.warning("股票池补市值/价格失败（降级继续）：%s", exc)

        if not records:
            raise ProviderError(
                "股票池为空：基础清单、交易所名单与行情快照均不可用",
                detail={"provider": self.name},
            )

        # 行业归属（东财板块；接口不可用时快速返回空，不阻断股票池）
        industry_map = self._fetch_industry_map()
        for code, item in records.items():
            item["industry_l1"] = industry_map.get(code)
            item.setdefault("delist_date", None)
            item.setdefault("list_date", listing_map.get(code, {}).get("list_date"))

        # 退市清单（代码/名称/退市日/上市日）
        delist_map = self._fetch_delisted_map()
        for code, info in delist_map.items():
            item = records.setdefault(code, {"code": code, "name": info.get("name") or ""})
            item["delist_date"] = info.get("delist_date")
            if info.get("name"):
                item["name"] = info["name"]
            if info.get("list_date") is not None and item.get("list_date") is None:
                item["list_date"] = info["list_date"]

        columns = [
            "code", "name", "close", "market_cap", "float_cap", "industry_l1",
            "turnover_rate", "pe_ttm", "pb", "list_date", "delist_date",
            "total_share", "float_share",
        ]
        frame = pd.DataFrame(list(records.values()), columns=columns)
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info(
            "股票池拉取完成：%d 只（基础源 %s，快照源 %s，行业归属 %d，退市 %d）",
            len(frame), base_source, spot_source, len(industry_map), len(delist_map),
        )
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            elapsed_ms=elapsed,
            detail={
                "data": frame,
                "source": self.name,
                "base_source": base_source,
                "spot_source": spot_source,
                "industry_mapped": len(industry_map),
                "delisted": len(delist_map),
                "list_date_mapped": int(frame["list_date"].notna().sum())
                if "list_date" in frame.columns else 0,
            },
        )

    def _fetch_cn_stock_lists(self) -> list[dict[str, Any]]:
        """上交所/深交所股票名单：代码/名称/上市日期/股本（深市有股本，沪市无）。

        实测列名（akshare 1.19.1）：
            - 上交所：证券代码 / 证券简称 / 上市日期；
            - 深交所：A股代码 / A股简称 / A股上市日期 / A股总股本 / A股流通股本。
        """
        rows: list[dict[str, Any]] = []
        sources: list[tuple[Any, str, str]] = []
        sh_func = getattr(ak, "stock_info_sh_name_code", None)
        sz_func = getattr(ak, "stock_info_sz_name_code", None)
        if sh_func is not None:
            sources.append((sh_func, "stock_info_sh_name_code[主板A股]", "主板A股"))
            sources.append((sh_func, "stock_info_sh_name_code[科创板]", "科创板"))
        if sz_func is not None:
            sources.append((sz_func, "stock_info_sz_name_code[A股列表]", "A股列表"))

        for func, label, symbol in sources:
            self._throttle()
            try:
                frame = self._fetch_one(func, label, symbol=symbol)
            except ProviderError as exc:
                logger.warning("交易所名单获取失败[%s]：%s", label, exc)
                continue
            for row in frame.to_dict("records"):
                code = _stock_code(
                    _pick(row, ("A股代码", "证券代码", "公司代码", "股票代码", "代码"))
                )
                if not code:
                    continue
                rows.append(
                    {
                        "code": code,
                        "name": str(
                            _pick(row, ("证券简称", "A股简称", "公司简称", "名称")) or ""
                        ).strip(),
                        "list_date": _to_date(
                            _pick(row, ("A股上市日期", "上市日期", "上市时间", "上市日"))
                        ),
                        "total_share": _num(_pick(row, ("A股总股本", "总股本"))),
                        "float_share": _num(_pick(row, ("A股流通股本", "流通股本"))),
                    }
                )
        return rows

    def _fetch_industry_map(self) -> dict[str, str]:
        """行业板块 → 成分映射（code → 行业名）。失败返回已拿到的部分，不抛异常。"""
        mapping: dict[str, str] = {}
        try:
            boards = self._fetch_one(
                ak.stock_board_industry_name_em, "stock_board_industry_name_em"
            )
        except ProviderError as exc:
            logger.warning("行业板块列表获取失败，跳过行业归属：%s", exc)
            return mapping

        names = []
        for row in boards.to_dict("records"):
            name = _pick(row, SECTOR_NAME)
            if name:
                names.append(str(name).strip())

        interval = 1.0 / max(config.HISTORY_REQUESTS_PER_SEC, 0.1)
        for board_name in names:
            self._throttle(interval)
            try:
                cons = self._fetch_one(
                    ak.stock_board_industry_cons_em,
                    f"stock_board_industry_cons_em[{board_name}]",
                    symbol=board_name,  # 参数是板块**名称**，不是 BK 代码
                )
            except ProviderError as exc:
                logger.warning("行业成分获取失败[%s]：%s", board_name, exc)
                continue
            for row in cons.to_dict("records"):
                code = _stock_code(_pick(row, COL_CODE))
                if code:
                    mapping[code] = board_name
        return mapping

    def _fetch_delisted_map(self) -> dict[str, dict[str, Any]]:
        """退市股清单（代码 → 名称/终止上市日期/上市日期）。拿不到就空，绝不抛异常。"""
        result: dict[str, dict[str, Any]] = {}
        for func, label in (
            (getattr(ak, "stock_info_sh_delist", None), "stock_info_sh_delist"),
            (getattr(ak, "stock_info_sz_delist", None), "stock_info_sz_delist"),
        ):
            if func is None:
                continue
            try:
                frame = self._fetch_one(func, label)
            except ProviderError as exc:
                logger.warning("退市清单获取失败[%s]：%s", label, exc)
                continue
            for row in frame.to_dict("records"):
                code = _stock_code(_pick(row, DELIST_CODE))
                if not code:
                    continue
                result[code] = {
                    "name": _pick(row, DELIST_NAME),
                    "delist_date": _to_date(_pick(row, DELIST_DATE)),
                    "list_date": _to_date(_pick(row, LIST_DATE)),
                }
        return result

    # ---------- A4 板块 ----------
    def fetch_sector_list(self, trade_date: date) -> FetchResult:
        """板块列表 + 板块日线。

        - ``trade_date == 今天``：直接用东财板块快照（一次请求拿全，快）；
        - ``trade_date < 今天``：逐板块拉历史日线（``stock_board_industry_hist_em`` /
          ``stock_board_concept_hist_em``，约 2 req/s，~5 分钟），用于 A4 历史回补；
        - 未来日期：直接返回空。
        """
        started = time.perf_counter()
        today = _today_cn()
        if trade_date > today:
            return FetchResult(
                rows=0,
                total=0,
                elapsed_ms=0,
                detail={"data": _empty_frame(("sector_code",)), "source": self.name,
                        "empty_reason": "future_date"},
            )

        industry = self._fetch_one(
            ak.stock_board_industry_name_em, "stock_board_industry_name_em"
        )
        self._throttle()
        concept = self._fetch_one(
            ak.stock_board_concept_name_em, "stock_board_concept_name_em"
        )

        if trade_date == today:
            frame = _normalize_sector_list(industry, concept)
        else:
            frame = self._fetch_sector_history(industry, concept, trade_date)

        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("板块日线 %s：%d 行（%s）", trade_date, len(frame),
                    "快照" if trade_date == today else "历史回补")
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            elapsed_ms=elapsed,
            detail={"data": frame, "source": self.name,
                    "trade_date": trade_date.isoformat(),
                    "mode": "snapshot" if trade_date == today else "history"},
        )

    def _fetch_sector_history(
        self,
        industry: pd.DataFrame,
        concept: pd.DataFrame,
        trade_date: date,
    ) -> pd.DataFrame:
        """逐板块拉历史单日行情（A4 历史回补路径）。失败的板块跳过并计数。"""
        columns = (
            "sector_code", "sector_name", "sector_type", "open", "high", "low", "close",
            "pre_close", "pct_chg", "amplitude", "volume", "amount", "turnover_rate",
            "up_count", "down_count", "leading_stock", "leading_stock_pct",
        )
        records: list[dict[str, Any]] = []
        failed = 0
        date_text = trade_date.strftime("%Y%m%d")

        for frame, sector_type, func_name, period in (
            # 注意：行业板块历史接口 period 用 "日k"，概念板块历史接口用 "daily"（akshare 1.19 实测签名）
            (industry, "industry", "stock_board_industry_hist_em", "日k"),
            (concept, "concept", "stock_board_concept_hist_em", "daily"),
        ):
            func = getattr(ak, func_name, None)
            if func is None or frame is None or frame.empty:
                continue
            for row in frame.to_dict("records"):
                sector_code = _pick(row, SECTOR_CODE)
                sector_name = _pick(row, SECTOR_NAME)
                if not sector_code or not sector_name:
                    continue
                self._throttle()
                try:
                    hist = self._fetch_one(
                        func,
                        f"{func_name}[{sector_name}]",
                        symbol=str(sector_name).strip(),  # 参数是板块名称
                        start_date=date_text,
                        end_date=date_text,
                        period=period,
                        adjust="",
                    )
                except ProviderError as exc:
                    failed += 1
                    logger.warning("板块历史获取失败[%s]：%s", sector_name, exc)
                    continue
                item = _normalize_sector_hist(
                    hist, str(sector_code).strip(), str(sector_name).strip(),
                    sector_type, trade_date,
                )
                if item is not None:
                    records.append(item)
        if failed:
            logger.warning("板块历史回补 %s：失败 %d 个板块", trade_date, failed)
        if not records:
            return _empty_frame(columns)
        return pd.DataFrame(records, columns=list(columns))

    def _em_board_code_maps(
        self, sector_type: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """东财板块名→BK 代码映射，按天缓存。

        akshare 的 cons 接口按名称调用时每次都会重拉整张板块清单再精确匹配，
        名称不存在时直接 ``IndexError``。预先解析成 BK 代码后：
        ① 每个板块只剩 1 次请求；② 未匹配的板块可以快速跳过而不是重试。
        清单刷新失败时沿用上一次成功的映射（BK 代码很稳定），避免单次
        DNS/网络抖动让全部板块在本轮都不可抓。
        """
        key = "industry" if sector_type == "industry" else "concept"
        cached = _EM_BOARD_CODE_CACHE.get(key)
        today = date.today()
        if cached is not None and cached[0] == today:
            return cached[1], cached[2]
        try:
            raw = self._em_board_list_df(key)
            exact, normalized = build_board_code_maps(raw)
            if not exact:
                raise ProviderError(f"东财{key}板块清单为空")
        except ProviderError:
            if cached is None:
                raise
            logger.warning(
                "东财 %s 板块清单刷新失败，沿用 %s 的缓存映射（%d 个板块）",
                key, cached[0], len(cached[1]),
            )
            return cached[1], cached[2]
        _EM_BOARD_CODE_CACHE[key] = (today, exact, normalized)
        return exact, normalized

    def _em_clist_page(
        self, fs: str, fields: str, page: int, rows_so_far: int, what: str
    ) -> tuple[list[dict], int]:
        """抓取东财 clist 的一页并校验完整性（残页重试同一页，绝不返回半份）。

        akshare 的板块接口每页固定 100 行、翻页间 sleep 0.5~1.5s，480 个板块
        一轮要 20 分钟以上，直接顶穿采集预算；这里直连 clist，按 ≤2 请求/秒
        自己翻页，并对每一页做 total 自洽校验。
        """
        from curl_cffi import requests as _cffi_req

        params = {
            "fid": "f12", "po": "1", "pz": str(_EM_BOARD_CONS_PAGE_SIZE),
            "pn": str(page), "np": "1", "fltt": "2", "invt": "2", "ut": _EM_UT,
            "fs": fs, "fields": fields,
        }
        last_error: Exception | None = None
        for attempt in range(_EM_BOARD_CONS_RETRIES):
            try:
                response = _cffi_req.get(
                    _EM_CLIST_URL, params=params, impersonate="chrome", timeout=15
                )
                response.raise_for_status()
                data = (response.json() or {}).get("data")
                if not isinstance(data, dict):
                    raise ProviderError("响应缺少 data（可能被风控）")
                if data.get("total") is None:
                    raise ProviderError("响应缺少 total")
                total = int(data["total"])
                diff = data.get("diff")
                if diff is None and total == 0:
                    diff = []
                if isinstance(diff, dict):
                    diff = list(diff.values())
                if not isinstance(diff, list):
                    raise ProviderError("响应缺少 diff")
                rows = [
                    item for item in diff
                    if isinstance(item, dict) and str(item.get("f12") or "").strip()
                ]
                if (
                    len(rows) < _EM_BOARD_CONS_PAGE_SIZE
                    and rows_so_far + len(rows) < total
                ):
                    raise ProviderError(
                        f"疑似截断：本页 {len(rows)} 行 < {_EM_BOARD_CONS_PAGE_SIZE}，"
                        f"累计 {rows_so_far + len(rows)}/{total}"
                    )
                return rows, total
            except Exception as exc:  # noqa: BLE001 —— 单页内自愈，不污染外层
                last_error = exc
                if attempt < _EM_BOARD_CONS_RETRIES - 1:
                    time.sleep(min(1.5 * (attempt + 1), 5.0))
        raise ProviderError(
            f"东财 {what} 第 {page} 页连续 {_EM_BOARD_CONS_RETRIES} 次失败或返回残页："
            f"{last_error}",
            detail={"provider": self.name, "what": what, "page": page},
        )

    def _em_clist_rows(self, fs: str, fields: str, what: str) -> list[dict]:
        """按 fs 条件抓取东财 clist 全部行（严格完整性，零容差）。"""
        rows: list[dict] = []
        seen: set[str] = set()
        total = 0
        page = 1
        while True:
            page_rows, total = self._em_clist_page(fs, fields, page, len(rows), what)
            for item in page_rows:
                code = str(item.get("f12") or "").strip()
                if code and code not in seen:
                    seen.add(code)
                    rows.append(item)
            if len(rows) >= total or not page_rows:
                break
            page += 1
            self._throttle()
        if len(rows) < total:
            # 去重后仍少于 total：榜单在翻页期间变动或分页不自洽。宁可整批失败，
            # 也不能把残缺名单交给 SCD2 去关闭区间。
            raise ProviderError(
                f"东财 {what} 不完整：total={total}，实际 {len(rows)}",
                detail={"provider": self.name, "what": what,
                        "total": total, "fetched": len(rows)},
            )
        return rows

    def _em_board_list_df(self, sector_type: str) -> pd.DataFrame:
        """直连东财抓板块清单（行业/概念），返回 板块名称/板块代码 两列。"""
        fs = "m:90 t:2 f:!50" if sector_type == "industry" else "m:90 t:3 f:!50"
        rows = self._em_clist_rows(fs, "f12,f14", f"{sector_type}_board_list")
        return pd.DataFrame(
            {
                "板块名称": [item.get("f14") for item in rows],
                "板块代码": [str(item.get("f12")) for item in rows],
            }
        )

    def _em_board_cons_df(self, em_code: str) -> pd.DataFrame:
        """直连东财抓取板块全部成分（严格完整性，零容差）。"""
        rows = self._em_clist_rows(
            f"b:{em_code} f:!50", "f12,f14", f"板块成分[{em_code}]"
        )
        return pd.DataFrame(
            {
                "代码": [str(item.get("f12")) for item in rows],
                "名称": [item.get("f14") for item in rows],
            }
        )

    def fetch_sector_members(
        self, sector_code: str, sector_name: str, sector_type: str
    ) -> FetchResult:
        """板块成分股。

        THS 板块名先按「精确 → 归一化 → 概念同义词」解析成东财 BK 代码，再用代码取成分；
        东财清单里没有的板块返回 ``skip_reason=em_name_not_found`` 的空结果，
        由编排层按"不支持的板块"跳过（不重试、不触发熔断、不动已有区间）。
        """
        started = time.perf_counter()
        exact, normalized = self._em_board_code_maps(sector_type)
        em_code = resolve_em_board_code(exact, normalized, sector_name, sector_type)
        if not em_code:
            elapsed = int((time.perf_counter() - started) * 1000)
            frame = _empty_frame(SECTOR_MEMBER_COLUMNS)
            return FetchResult(
                rows=0,
                total=0,
                elapsed_ms=elapsed,
                detail={
                    "data": frame,
                    "source": self.name,
                    "sector_code": sector_code,
                    "sector_name": sector_name,
                    "sector_type": sector_type,
                    "skip_reason": "em_name_not_found",
                },
            )
        raw = self._em_board_cons_df(em_code)
        frame = _normalize_sector_members(raw, sector_code, sector_name, sector_type)
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            elapsed_ms=elapsed,
            detail={"data": frame, "source": self.name,
                    "sector_code": sector_code, "sector_name": sector_name,
                    "sector_type": sector_type},
        )

    # ---------- A5 资金流与两融 ----------
    def _em_moneyflow_page(
        self, indicator: str, pn: int, page_size: int, rows_so_far: int,
        total: int | None,
    ) -> tuple[list[dict], int]:
        """抓取并校验东财资金流的一页（截断页重试同页，绝不返回残页）。

        东财在风控/链路抖动时会返回 HTTP 200 但 ``diff`` 被截短（少于 page_size
        且 total 仍更大），也可能 ``data`` 直接为 null。若把这种页当成功缓存，
        残页会在当日所有重试里永久生效，全市场资金流就永远缺行。
        """
        from curl_cffi import requests as _cffi_req

        fid, field_csv, _raw_field, _col_name = _EM_MF_SPEC[indicator]
        params = {"fid": fid, "po": "1", "pz": str(page_size), "pn": str(pn),
                  "np": "1", "fltt": "2", "invt": "2", "ut": _EM_UT,
                  "fs": _EM_MF_FS, "fields": field_csv}
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                response = _cffi_req.get(
                    _EM_CLIST_URL, params=params, impersonate="chrome", timeout=15
                )
                response.raise_for_status()
                data = (response.json() or {}).get("data")
                if not isinstance(data, dict):
                    raise ProviderError("响应缺少 data（可能被风控）")
                diff = data.get("diff")
                if isinstance(diff, dict):
                    diff = list(diff.values())
                if not isinstance(diff, list):
                    raise ProviderError("响应缺少 diff")
                if data.get("total") is None:
                    raise ProviderError("响应缺少 total")
                page_total = int(data["total"])
                page_rows = [
                    item for item in diff
                    if isinstance(item, dict) and str(item.get("f12") or "").strip()
                ]
                if (
                    len(page_rows) < page_size
                    and rows_so_far + len(page_rows) < page_total
                ):
                    raise ProviderError(
                        f"疑似截断：本页 {len(page_rows)} 行 < {page_size}，"
                        f"累计 {rows_so_far + len(page_rows)}/{page_total}"
                    )
                return page_rows, page_total
            except Exception as exc:  # noqa: BLE001 —— 单页内自愈，不污染外层
                last_error = exc
                if attempt < 5:
                    time.sleep(min(2.0 * (attempt + 1), 8.0))
        raise ProviderError(
            f"东财资金流第 {pn} 页连续 6 次失败或返回残页：{last_error}",
            detail={"provider": self.name, "page": pn, "indicator": indicator},
        )

    def _em_moneyflow_df(self, indicator: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """直连东财 clist 分页抓个股资金流（每页独立重试 + 完整性校验）。

        为什么不用 ak.stock_individual_fund_flow_rank：该函数要连续翻 ~54 页，
        东财按连接**突发性**重置（实测坏窗口内 0.2s 即被掐断，坏窗口持续数秒~数分钟），
        任何一页失败则整个函数作废，外层重试又在同一风暴里翻页，几乎必败。
        这里改为：curl_cffi(chrome) TLS 指纹直连 + 每页独立重试 6 次。

        返回 ``(frame, stats)``；只有与 ``total`` 自洽（容差内）的结果才会返回并
        写入页缓存，否则抛 ``ProviderError`` 让降级链换源，绝不落半份数据。
        """
        fid, _field_csv, raw_field, col_name = _EM_MF_SPEC[indicator]
        date_key = time.strftime("%Y%m%d")
        _prune_mf_cache(date_key)
        total_key = (indicator, date_key)
        total = _EM_MF_TOTAL_CACHE.get(total_key)
        rows: list[dict] = []
        seen: set[str] = set()
        pages_hit = pages_fetched = 0
        pn = 1
        while True:
            cache_key = (indicator, date_key, pn)
            page_rows = _EM_MF_PAGE_CACHE.get(cache_key)
            if page_rows is None:
                if pn > 1:
                    time.sleep(0.3)
                page_rows, page_total = self._em_moneyflow_page(
                    indicator, pn, _EM_MF_PAGE_SIZE, len(rows), total
                )
                total = page_total if total is None else max(total, page_total)
                _EM_MF_PAGE_CACHE[cache_key] = page_rows
                _EM_MF_TOTAL_CACHE[total_key] = total
                pages_fetched += 1
            else:
                pages_hit += 1
            for item in page_rows:
                code = str(item.get("f12") or "").strip()
                if code and code not in seen:
                    seen.add(code)
                    rows.append(item)
            if total is None or len(rows) >= total or not page_rows:
                break
            pn += 1

        if total is not None:
            shortfall = total - len(rows)
            tolerance = max(_EM_MF_MIN_TOLERANCE, math.ceil(total * _EM_MF_TOLERANCE_RATIO))
            if shortfall > tolerance:
                raise ProviderError(
                    f"东财资金流分页不完整：total={total}，实际 {len(rows)} 行",
                    detail={"provider": self.name, "indicator": indicator,
                            "total": total, "fetched": len(rows)},
                )
            if shortfall > 0:
                logger.warning("东财资金流 %s 少量缺行：total=%d，实际 %d",
                               indicator, total, len(rows))

        frame = pd.DataFrame({
            "代码": [str(item.get("f12", "")) for item in rows],
            "名称": [item.get("f14") for item in rows],
            col_name: [item.get(raw_field) for item in rows],
        })
        stats = {"total": total, "fetched": len(rows), "pages": pn,
                 "pages_hit": pages_hit, "pages_fetched": pages_fetched}
        return frame, stats

    def _ths_moneyflow_df(self, symbol: str, out_col: str) -> pd.DataFrame:
        """同花顺个股资金流兜底（东财被风控时的降级源）。

        口径差异：THS ``净额`` = 流入-流出（**总净额**），非东财"主力净额"
        （超大单+大单）。仅作降级使用，结果 detail 里会标注 source=ths。
        数值为 "16.74亿" / "-810.40万" 字符串，需解析为元。
        """
        df = None
        last_exc: ProviderError | None = None
        for attempt in range(3):  # THS 域名偶发 DNS 瞬时失败（Errno -5），重试即可
            try:
                df = self._fetch_one(
                    ak.stock_fund_flow_individual,
                    f"stock_fund_flow_individual[{symbol}]",
                    symbol=symbol,
                )
                break
            except ProviderError as exc:
                last_exc = exc
                time.sleep(2.0 * (attempt + 1))
        if df is None:
            raise last_exc  # type: ignore[misc]
        if df is not None and getattr(df, "empty", True):
            return pd.DataFrame(columns=["代码", "名称", out_col])
        return pd.DataFrame({
            "代码": df["股票代码"].astype(str),
            "名称": df["股票简称"],
            out_col: df["净额"].map(_parse_cn_amount),
        })

    def fetch_moneyflow(self, trade_date: date) -> FetchResult:
        """个股资金流：今日 + 5 日（单位：元）。5 日失败降级为只有今日。

        源策略：东财 clist 直连（每页重试+当日页缓存）优先；
        东财被风控（连续坏窗口）时整体降级到同花顺（口径：总净额，非主力净额）。
        """
        started = time.perf_counter()
        source_tag = "akshare"
        today_stats: dict[str, Any] = {}
        five_stats: dict[str, Any] = {}
        five_day_df = None
        try:
            today_df, today_stats = self._em_moneyflow_df("今日")
            self._throttle()
            try:
                five_day_df, five_stats = self._em_moneyflow_df("5日")
            except ProviderError as exc:
                logger.warning("资金流 5 日榜获取失败（降级为仅今日）：%s", exc)
        except ProviderError as em_exc:
            logger.warning(
                "东财资金流不可用（%s），降级到同花顺（口径：总净额）", em_exc)
            source_tag = "ths-fallback"
            today_df = self._ths_moneyflow_df("即时", "今日主力净流入-净额")
            self._throttle()
            try:
                five_day_df = self._ths_moneyflow_df("5日排行", "5日主力净流入-净额")
            except ProviderError as exc:
                logger.warning("同花顺 5 日排行获取失败（降级为仅今日）：%s", exc)

        frame = _normalize_moneyflow(today_df, five_day_df)
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            elapsed_ms=elapsed,
            detail={"data": frame, "source": self.name, "source_tag": source_tag,
                    "trade_date": trade_date.isoformat(),
                    "five_day_available": five_day_df is not None,
                    "page_checks": {"today": today_stats, "five_day": five_stats}},
        )

    def fetch_margin(self, trade_date: date) -> FetchResult:
        """两融 + 北向（市场级，单位：亿元）。

        关键坑（实测确认）：两融数据是 **T+1 披露** 的。晚上跑 D 日流水线时，
        交易所还没有 D 日数据；而深交所接口**返回的 DataFrame 没有日期列**，
        盲目把返回值当 D 日会把 D-1 的数据写到 D 上（脏数据）。
        因此这里：

        1. 上交所按区间拉取（带 ``信用交易日期``）→ 取 ``<= trade_date`` 的最新
           已披露日作为 **effective_date**（可能 = D-1），过旧则视为未披露；
        2. 深交所按 **effective_date 显式请求**（接口支持 date 参数）；
        3. 落库行使用 effective_date，并在 detail 里标记 ``published_date_lag``。

        两个市场都必须成功：库里存的是沪深合计，缺任一市场会低估约一半；
        也**不做无日期参数的兜底**（akshare 默认区间分别截止 2023-09-22 和
        2024-04-11，会把陈旧数据标成请求日）。

        单位：上交所金额**元**（/1e8→亿元）；深交所金额**已是亿元**；
        北向"当日成交净买额"缺失属预期内（2024-08 后口径变化），失败不报错。
        """
        started = time.perf_counter()
        requested_date = trade_date
        lookback_start = requested_date - timedelta(days=15)
        sse_df = szse_df = north_df = None
        sse_ok = szse_ok = False

        try:
            sse_df = self._fetch_one(
                ak.stock_margin_sse,
                "stock_margin_sse",
                start_date=lookback_start.strftime("%Y%m%d"),
                end_date=requested_date.strftime("%Y%m%d"),
            )
            sse_ok = True
        except ProviderError as exc:
            # 不做无日期参数兜底：akshare 的默认区间截止 2023-09-22，
            # 会把多年前的数据当"最新已披露日"落库。
            logger.warning("上交所两融获取失败：%s", exc)

        sse_map = _margin_map_sse(sse_df) if sse_ok else {}
        effective_date = _margin_effective_date(sse_map, requested_date)
        if sse_ok and effective_date is None:
            # 上交所成功但区间内无近期数据：说明尚未披露或源返回陈旧历史，
            # 宁可"跳过"也不写错日期
            raise ProviderError(
                f"上交所两融区间内无近期已披露数据（{lookback_start} ~ {requested_date}）",
                detail={"provider": "akshare", "reason": "margin_not_published"},
            )
        if effective_date is None:
            effective_date = requested_date

        try:
            szse_df = self._fetch_one(
                ak.stock_margin_szse,
                "stock_margin_szse",
                date=effective_date.strftime("%Y%m%d"),
            )
            szse_ok = True
        except ProviderError as exc:
            # 不做无参兜底：该接口返回单行且无日期列，无参时 akshare 固定取
            # date="20240411"，会被误标成请求日写入。宁可缺一个市场的数据。
            logger.warning("深交所两融获取失败：%s", exc)

        if not sse_ok:
            # 没有上交所数据就无法确认披露日，SZSE 的日期只能靠猜
            raise ProviderError(
                "上交所两融获取失败，无法确认披露日期",
                detail={"provider": "akshare", "reason": "sse_unavailable"},
            )
        if not szse_ok:
            # 库里存的是沪深合计；只有单市场会低估约一半，不能落库
            raise ProviderError(
                "深交所两融获取失败，无法确认沪深合计余额",
                detail={"provider": "akshare", "reason": "szse_unavailable"},
            )

        szse_map = _margin_map_szse(szse_df, effective_date)

        # 北向：软失败（2024-08 后可能长期无数据，属预期内）
        self._throttle()
        try:
            north_df = self._fetch_one(ak.stock_hsgt_hist_em, "stock_hsgt_hist_em")
        except ProviderError as exc:
            logger.info("北向资金获取失败（预期内，忽略）：%s", exc)
        north_map = _normalize_north(north_df)

        sse_item = sse_map.get(effective_date, {})
        szse_item = szse_map.get(effective_date, {})
        margin_balance = None
        short_balance = None
        margin_buy_amount = None
        if sse_item.get("margin_balance") is not None or \
                szse_item.get("margin_balance") is not None:
            margin_balance = round(
                (sse_item.get("margin_balance") or 0.0)
                + (szse_item.get("margin_balance") or 0.0), 4,
            )
        if sse_item.get("short_balance") is not None or \
                szse_item.get("short_balance") is not None:
            short_balance = round(
                (sse_item.get("short_balance") or 0.0)
                + (szse_item.get("short_balance") or 0.0), 4,
            )
        if sse_item.get("margin_buy") is not None and \
                szse_item.get("margin_buy") is not None:
            margin_buy_amount = round(
                float(sse_item["margin_buy"]) + float(szse_item["margin_buy"]), 2
            )

        rows = [
            {
                "trade_date": effective_date,
                "margin_balance": margin_balance,
                "short_balance": short_balance,
                "northward_net": north_map.get(effective_date),
                "margin_buy_amount": margin_buy_amount,
            }
        ]
        frame = pd.DataFrame(rows)
        elapsed = int((time.perf_counter() - started) * 1000)
        lag = (requested_date - effective_date).days
        if lag > 0:
            logger.info(
                "两融数据 T+1 披露：请求日 %s 尚未披露，实际落库 %s（滞后 %d 天）",
                requested_date, effective_date, lag,
            )
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            failed=0,
            elapsed_ms=elapsed,
            detail={
                "data": frame,
                "source": self.name,
                "requested_date": requested_date.isoformat(),
                "effective_date": effective_date.isoformat(),
                "published_date_lag": lag,
                "sse_available": sse_ok,
                "szse_available": szse_ok,
                "north_available": north_map.get(effective_date) is not None,
            },
        )

    # ---------- 宏观利率 ----------
    def fetch_bond_yields(self, start: date, end: date) -> FetchResult:
        """中债国债收益率曲线（1Y/10Y，%）。

        中债接口按区间返回多张曲线，这里只保留「中债国债收益率曲线」。
        区间过长时分段调用由编排层负责（单次建议 <= 1 年）。
        """
        started = time.perf_counter()
        frame = self._fetch_one(
            ak.bond_china_yield,
            "bond_china_yield",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        self._throttle()
        normalized = _normalize_bond_yields(frame)
        return FetchResult(
            rows=len(normalized),
            total=len(normalized),
            failed=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"data": normalized, "source": self.name},
        )

    # ---------- 融资买入额（回补用） ----------
    def fetch_margin_buy_sse(self, start: date, end: date) -> FetchResult:
        """上交所区间融资买入额（元）。"""
        started = time.perf_counter()
        frame = self._fetch_one(
            ak.stock_margin_sse,
            "stock_margin_sse",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        self._throttle()
        normalized = _normalize_margin_buy_sse(frame)
        return FetchResult(
            rows=len(normalized),
            total=len(normalized),
            failed=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"data": normalized, "source": self.name},
        )

    def fetch_margin_buy_szse(self, trade_date: date) -> FetchResult:
        """深交所单日融资买入额（源单位亿元 → 元）。"""
        started = time.perf_counter()
        frame = self._fetch_one(
            ak.stock_margin_szse,
            "stock_margin_szse",
            date=trade_date.strftime("%Y%m%d"),
        )
        self._throttle()
        normalized = _normalize_margin_buy_szse(frame, trade_date)
        available = not normalized.empty and normalized.iloc[0]["margin_buy_amount"] is not None
        return FetchResult(
            rows=1 if available else 0,
            total=1,
            failed=0 if available else 1,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"data": normalized, "source": self.name},
        )

    # ---------- 风格轮动指数 ----------
    def fetch_style_indices(self, start: date, end: date) -> FetchResult:
        """创业板50（新浪）+ 红利低波50（中证指数）历史日线。

        两个源独立降级：任一失败不阻断另一个；调用方按 code 做质量校验。
        中证接口返回的是价格指数点位，不做 ETF/其它指数替代。
        """
        started = time.perf_counter()
        frames: list[pd.DataFrame] = []
        sources: dict[str, str] = {}
        failed = 0

        try:
            raw = self._fetch_one(
                ak.stock_zh_index_daily,
                "stock_zh_index_daily[sz399673]",
                symbol="sz399673",
            )
            self._throttle()
            frame = _normalize_index(raw, "sz399673", "创业板50")
            frame = frame[
                (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
            ].copy()
            frame["src"] = "sina"
            if frame.empty:
                failed += 1
                logger.warning("创业板50 日线在 %s~%s 为空", start, end)
            else:
                frames.append(frame)
                sources["sz399673"] = "sina"
        except ProviderError as exc:
            failed += 1
            logger.warning("创业板50 日线获取失败：%s", exc)

        try:
            raw = self._fetch_one(
                ak.stock_zh_index_hist_csindex,
                "stock_zh_index_hist_csindex[H30269]",
                symbol="H30269",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            self._throttle()
            frame = _normalize_csi_index(raw, "H30269", "红利低波50")
            frame = frame[
                (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
            ].copy()
            frame["src"] = "csindex"
            if frame.empty:
                failed += 1
                logger.warning("红利低波50 日线在 %s~%s 为空", start, end)
            else:
                frames.append(frame)
                sources["H30269"] = "csindex"
        except ProviderError as exc:
            failed += 1
            logger.warning("红利低波50 日线获取失败：%s", exc)

        frame = (
            pd.concat(frames, ignore_index=True)
            if frames else _empty_frame(INDEX_COLUMNS)
        )
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            failed=failed,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"data": frame, "source": self.name, "sources": sources},
        )

    # ---------- 牛熊估值 6 宽基 ----------
    def fetch_broad_indices(self, start: date, end: date) -> FetchResult:
        """6 个宽基指数历史日线（文件硬约束口径）。

        中证红利（000922）用中证指数历史接口；其余 5 个用新浪。
        单指数失败不阻断其它指数，调用方按 code 做质量校验。
        """
        from app.core.constants import BROAD_INDEX_MAP

        started = time.perf_counter()
        frames: list[pd.DataFrame] = []
        sources: dict[str, str] = {}
        failed = 0

        for index_code, index_name in BROAD_INDEX_MAP.items():
            try:
                if index_code == "sh000922":
                    raw = self._fetch_one(
                        ak.stock_zh_index_hist_csindex,
                        "stock_zh_index_hist_csindex[000922]",
                        symbol="000922",
                        start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"),
                    )
                    self._throttle()
                    frame = _normalize_csi_index(raw, index_code, index_name)
                    source = "csindex"
                else:
                    raw = self._fetch_one(
                        ak.stock_zh_index_daily,
                        f"stock_zh_index_daily[{index_code}]",
                        symbol=index_code,
                    )
                    self._throttle()
                    frame = _normalize_index(raw, index_code, index_name)
                    source = "sina"
                frame = frame[
                    (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
                ].copy()
                if frame.empty:
                    failed += 1
                    logger.warning("%s 日线在 %s~%s 为空", index_code, start, end)
                    continue
                frame["src"] = source
                frames.append(frame)
                sources[index_code] = source
            except ProviderError as exc:
                failed += 1
                logger.warning("%s 日线获取失败：%s", index_code, exc)

        frame = (
            pd.concat(frames, ignore_index=True)
            if frames else _empty_frame(INDEX_COLUMNS)
        )
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            failed=failed,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"data": frame, "source": self.name, "sources": sources},
        )

    # ---------- A6 指数 ----------
    def _fetch_sina_index(
        self, index_code: str, index_name: str, trade_date: date
    ) -> pd.DataFrame:
        """新浪（AKShare ``stock_zh_index_daily``）单指数，仅保留目标日。"""
        raw = self._fetch_one(
            ak.stock_zh_index_daily,
            f"stock_zh_index_daily[{index_code}]",
            symbol=index_code,
        )
        self._throttle()
        normalized = _normalize_index(raw, index_code, index_name)
        if normalized.empty:
            return _empty_frame(INDEX_COLUMNS)
        target = normalized[normalized["trade_date"] == trade_date]
        return target.reset_index(drop=True)

    def _fetch_tencent_index(
        self, index_code: str, index_name: str, trade_date: date
    ) -> pd.DataFrame:
        """腾讯单指数兜底（新浪 DNS 抖动/单指数失败时）。"""
        params = {"param": f"{index_code},day,,,10"}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = requests.get(
                    TENCENT_INDEX_URL, params=params,
                    headers=_TENCENT_INDEX_HEADERS, timeout=(5, 10),
                )
                if response.status_code in (403, 501):
                    # WAF 拦截页：重试只会延长封禁
                    raise ProviderError(
                        f"腾讯指数接口被拦截 HTTP {response.status_code}",
                        detail={"index_code": index_code},
                    )
                response.raise_for_status()
                data = (response.json() or {}).get("data") or {}
                rows = (data.get(index_code) or {}).get("day") or []
                return _normalize_tencent_index(
                    rows, index_code, index_name, trade_date
                )
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 —— DNS/连接抖动重试一次
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        raise ProviderError(
            f"腾讯指数日线获取失败 {index_code}：{last_error}",
            detail={"index_code": index_code},
        )

    def fetch_index_daily(self, trade_date: date, include_us: bool = False) -> FetchResult:
        """A 股指数日线（逐指数拉历史后过滤目标日）；美股可选（yfinance 未装则跳过）。

        每个指数独立降级：新浪取不到（抛错或当日无行）→ 腾讯无复权 K 线。
        降级链只在"整源有行"时返回，单指数缺失不会触发下一级源，所以兜底
        必须放在这里，否则一个指数失败就要等下次重跑（实测 11-04 缺上证指数）。
        """
        from app.core.constants import INDEX_MAP

        started = time.perf_counter()
        frames: list[pd.DataFrame] = []
        failed = 0
        index_sources: dict[str, str] = {}
        for index_code, (index_name, is_a_share) in INDEX_MAP.items():
            if not is_a_share:
                continue
            source = "sina"
            target = _empty_frame(INDEX_COLUMNS)
            try:
                target = self._fetch_sina_index(index_code, index_name, trade_date)
            except ProviderError as exc:
                logger.warning("新浪指数日线获取失败[%s]：%s", index_code, exc)
            if target.empty:
                source = "tencent"
                try:
                    target = self._fetch_tencent_index(
                        index_code, index_name, trade_date
                    )
                except ProviderError as exc:
                    logger.warning("腾讯指数日线获取失败[%s]：%s", index_code, exc)
            if target.empty:
                failed += 1
                continue
            frames.append(target)
            index_sources[index_code] = source

        if include_us:
            frames.extend(self._fetch_us_indices(trade_date))

        if not frames:
            frame = _empty_frame(INDEX_COLUMNS)
        else:
            frame = pd.concat(frames, ignore_index=True)
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(
            rows=len(frame),
            total=len(frame),
            failed=failed,
            elapsed_ms=elapsed,
            detail={"data": frame, "source": self.name,
                    "trade_date": trade_date.isoformat(),
                    "include_us": include_us,
                    "index_sources": index_sources},
        )

    def _fetch_us_indices(self, trade_date: date) -> list[pd.DataFrame]:
        """美股三大指数（可选依赖 yfinance）。未安装/失败 → 返回空列表，不报错。"""
        try:  # pragma: no cover —— 取决于环境是否安装 yfinance
            import yfinance as yf
        except ImportError:
            logger.info("yfinance 未安装，跳过美股指数（不影响 A 股链路）")
            return []

        from app.core.constants import INDEX_MAP

        frames: list[pd.DataFrame] = []
        for index_code, (index_name, is_a_share) in INDEX_MAP.items():
            if is_a_share:
                continue
            try:
                ticker = yf.Ticker(index_code)
                hist = ticker.history(
                    start=(trade_date - pd.Timedelta(days=10)).isoformat(),
                    end=(trade_date + pd.Timedelta(days=1)).isoformat(),
                    auto_adjust=False,
                )
            except Exception as exc:  # noqa: BLE001 —— 可选能力，失败忽略
                logger.warning("美股指数获取失败[%s]：%s", index_code, exc)
                continue
            item = _normalize_us_index(hist, index_code, index_name, trade_date)
            if item is not None:
                frames.append(pd.DataFrame([item], columns=list(INDEX_COLUMNS)))
        return frames

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
    "AkshareProvider",
]
