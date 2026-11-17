"""V9 拆分：日期/DB/任务/质量写入辅助。

由原 ``app/ingest/valuation.py`` 拆分而来；``valuation.py`` 保留门面与
网络/同步编排，既有 ``from app.ingest.valuation import ...`` 导入保持可用。
"""
from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:  # pragma: no cover —— 依赖是否安装由运行环境决定
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]

from app.core import config
from app.core.logging import get_logger
from app.core.status import QualityStatus
from app.ingest.base import FetchResult, chunked, make_history_limiter, upsert_rows
from app.ingest.quality import QualityReport, make_error_report, record_quality

if TYPE_CHECKING:  # pragma: no cover —— 只为类型标注，运行期不 import 数据库栈
    from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.valuation_math import *  # noqa: F401,F403

logger = get_logger(__name__)

def _today_cn() -> date:
    """A 股所在的"今天"（Asia/Shanghai）。"""
    try:
        return datetime.now(ZoneInfo(config.TIMEZONE)).date()
    except Exception:  # pragma: no cover —— Windows 缺 tzdata 时兜底固定 +8
        return (datetime.now(timezone.utc) + timedelta(hours=8)).date()

def _as_date(value: Any) -> date:
    """date/datetime/字符串 → ``datetime.date``（无效值抛 ValueError）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if parsed is pd.NaT or pd.isna(parsed):
        raise ValueError(f"无法解析日期: {value!r}")
    return parsed.date()

@asynccontextmanager
async def _db_scope(db: "AsyncSession | None") -> AsyncIterator["AsyncSession"]:
    """会话作用域：外部传入 → 复用（只 flush）；否则自建 ``session_scope()``（自动 commit）。

    这是"事务所有权"约定的唯一入口：``db=None`` 时本模块自己提交（CLI / 后台任务），
    传入时把提交时机交给 ``app/pipeline.py``（一个事务里可回滚）。
    """
    if db is not None:
        try:
            yield db
        finally:
            await db.flush()
        return
    from app.db.session import session_scope

    async with session_scope() as session:
        yield session

def _as_db_value(value: Any, digits: int | None = None) -> float | None:
    """DataFrame 里的值 → 可写库的 float（NaN/Inf/None → None，按列精度四舍五入）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, digits) if digits is not None else number

def _normalize_codes(codes: Iterable[Any] | None) -> list[str]:
    """代码清单归一化（去重、保序、统一 6 位）。"""
    cleaned: dict[str, None] = {}
    for code in codes or []:
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if digits:
            cleaned[digits.zfill(6)[-6:]] = None
    return list(cleaned)

def report_lag_days(report_date: date) -> int:
    """报告期 → 法定披露滞后（自然日）；未知报告期用最保守口径。"""
    return REPORT_LAG_DAYS_BY_MONTH.get(report_date.month, REPORT_LAG_FALLBACK_DAYS)

def _announced_date(report_date: date, lag_days: int | None = None) -> date:
    """报告期 → 市场可知日期（point-in-time 的时间锚）。"""
    days = report_lag_days(report_date) if lag_days is None else int(lag_days)
    return report_date + timedelta(days=days)

_QUARTER_END_DAYS = {12: 31, 9: 30, 6: 30, 3: 31}

def latest_knowable_report_period(
    today: date, lag_days: int | None = None
) -> date | None:
    """按"报告期 + 法定披露滞后"的保守口径，算出今天**最多**能知道到哪个报告期。

    例：``today=2026-10-22`` → 2026-09-30 + 30d = 2026-10-30（还不可知）→
        回退到 2026-06-30（2026-08-31 可知）。

    用途：``sync_financials(only_missing=True)`` 用它判断"这只股票的财报已经是最新可得的
    了吗"，是的话跳过网络请求 —— 这就是"每日增量几乎零请求"的关键。
    """
    for year in range(today.year, today.year - 3, -1):
        for month in (12, 9, 6, 3):
            period = date(year, month, _QUARTER_END_DAYS[month])
            if _announced_date(period, lag_days) <= today:
                return period
    return None

def _json_safe(value: Any) -> Any:
    """checkpoint 里可能有 date / numpy 标量，转成 JSONB 能接受的值。"""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return _as_db_value(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value

def _financial_rows_from_frame(
    code: str, frame: pd.DataFrame, src: str = "sina"
) -> list[dict[str, Any]]:
    """``fetch_financials`` 的返回表 → ``dim_stock_financial`` 行（精度按模型列定义）。

    只丢弃"报告期存在、所有财务字段全空"的占位行（只留报告期写进去只会让
    only_missing 误以为"已有报告"）。仅有 ROE 的行必须保留：688836 的 2025 年报
    在指标表里只有 ROE、没有 EPS/BPS，丢掉它会让营收 TTM 缺少上年基数（PS 全空）。
    这类行的 ``revenue`` 先为空，随后由 :func:`sync_revenue_bulk` 从东财业绩报表补齐。
    """
    if frame is None or len(frame) == 0:
        return []
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict("records"):
        report_date = record.get("report_date")
        if not isinstance(report_date, date):
            continue
        eps = _as_db_value(record.get("eps"), 6)
        bps = _as_db_value(record.get("bps"), 6)
        eps_ttm = _as_db_value(record.get("eps_ttm"), 6)
        roe = _as_db_value(record.get("roe"), 4)
        payout_ratio = _as_db_value(record.get("payout_ratio"), 4)
        revenue = _as_db_value(record.get("revenue"), 2)
        if (
            eps is None
            and bps is None
            and eps_ttm is None
            and roe is None
            and payout_ratio is None
            and revenue is None
        ):
            continue
        rows.append(
            {
                "code": code,
                "report_date": report_date,
                "announced_date": _announced_date(report_date),
                "eps": eps,
                "bps": bps,
                "roe": roe,
                "payout_ratio": payout_ratio,
                "eps_ttm": eps_ttm,
                "revenue": revenue,
                "src": src,
            }
        )
    return rows

async def _resolve_market_codes(db: "AsyncSession | None") -> list[str]:
    """``codes=None`` 时的股票池：``dim_stock`` 里 ``status=1``（在市）的标的。"""
    from sqlalchemy import select

    from app.db.models.dim import DimStock

    stmt = select(DimStock.code).where(DimStock.status == 1).order_by(DimStock.code)
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [str(code) for code in rows]

async def _load_latest_financial_dates(
    db: "AsyncSession | None", codes: Sequence[str] | None = None
) -> dict[str, date]:
    """每个股票**已入库的最新报告期**（only_missing 判断用）。"""
    from sqlalchemy import func, select

    from app.db.models.dim import DimStockFinancial

    stmt = select(
        DimStockFinancial.code, func.max(DimStockFinancial.report_date)
    ).group_by(DimStockFinancial.code)
    target = _normalize_codes(codes) if codes is not None else []
    if target:
        stmt = stmt.where(DimStockFinancial.code.in_(target))
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).all()
    return {str(code): period for code, period in rows if period is not None}

async def _upsert_financial_rows(
    db: "AsyncSession | None", rows: Sequence[dict[str, Any]]
) -> int:
    """幂等写入财报缓存（``ON CONFLICT (code, report_date) DO UPDATE``）。"""
    if not rows:
        return 0
    from app.db.models.dim import DimStockFinancial

    async with _db_scope(db) as session:
        return await upsert_rows(
            session,
            DimStockFinancial,
            list(rows),
            ["code", "report_date"],
            update_columns=[
                "announced_date", "eps", "bps", "roe", "payout_ratio",
                "eps_ttm", "src",
            ],
            # 注意：revenue 不在 update_columns 里。新浪指标表没有绝对营收（只有增长率），
            # 若参与 upsert 更新，每天一次 sync_financials 都会把
            # sync_revenue_bulk 从东财业绩报表同步来的绝对营收洗成 NULL。
            # 新插入的行 revenue 可来自财务摘要兜底，之后由 sync_revenue_bulk 校正。
        )

def _dividend_rows_from_frame(
    code: str, frame: pd.DataFrame, src: str = "cninfo"
) -> list[dict[str, Any]]:
    """``normalize_dividends`` 的返回表 → ``dim_stock_dividend`` 行。"""
    if frame is None or len(frame) == 0:
        return []
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict("records"):
        ex_date = record.get("ex_date")
        dps = _as_db_value(record.get("dps"), 6)
        if not isinstance(ex_date, date) or dps is None or dps <= 0:
            continue
        announce_date = record.get("announce_date")
        report_period = record.get("report_period")
        rows.append(
            {
                "code": code,
                "ex_date": ex_date,
                "dps": dps,
                "announce_date": announce_date if isinstance(announce_date, date) else None,
                "report_period": (
                    str(report_period)[:16] if report_period is not None and pd.notna(
                        report_period
                    ) else None
                ),
                "src": src,
            }
        )
    return rows

async def _upsert_dividend_rows(
    db: "AsyncSession | None",
    rows: Sequence[dict[str, Any]],
    update_columns: Sequence[str] | None = None,
) -> int:
    """幂等写入分红缓存（``ON CONFLICT (code, ex_date) DO UPDATE``）。"""
    if not rows:
        return 0
    from app.db.models.dim import DimStockDividend

    async with _db_scope(db) as session:
        return await upsert_rows(
            session,
            DimStockDividend,
            list(rows),
            ["code", "ex_date"],
            update_columns=(
                list(update_columns)
                if update_columns is not None
                else ["dps", "announce_date", "report_period", "src"]
            ),
        )

async def _load_dividend_updated(
    db: "AsyncSession | None", codes: Sequence[str]
) -> dict[str, datetime]:
    """每个股票分红记录的**最后刷新时间**（``only_missing`` 判断用）。

    注意：没有任何分红记录的股票不会出现在这个 map 里（"查过但没有分红"要靠
    ``ingest_job.checkpoint.checked_codes`` 兜住，见 :func:`sync_dividends`）。
    """
    from sqlalchemy import func, select

    from app.db.models.dim import DimStockDividend

    target = _normalize_codes(codes)
    if not target:
        return {}
    stmt = (
        select(DimStockDividend.code, func.max(DimStockDividend.updated_at))
        .where(DimStockDividend.code.in_(target))
        .group_by(DimStockDividend.code)
    )
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).all()
    result: dict[str, datetime] = {}
    for code, updated_at in rows:
        if updated_at is not None:
            result[str(code)] = updated_at
    return result

async def _load_dividends_for_codes(
    db: "AsyncSession | None", codes: Sequence[str]
) -> pd.DataFrame:
    """读一批股票的分红明细（阶段 2 的输入，**零网络**）。"""
    columns = ["code", "ex_date", "dps"]
    target = _normalize_codes(codes)
    if not target:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    from sqlalchemy import select

    from app.db.models.dim import DimStockDividend

    stmt = (
        select(DimStockDividend.code, DimStockDividend.ex_date, DimStockDividend.dps)
        .where(DimStockDividend.code.in_(target))
        .order_by(DimStockDividend.code, DimStockDividend.ex_date)
    )
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).all()
    if not rows:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    return pd.DataFrame(rows, columns=columns)

async def _job_begin(
    db: "AsyncSession | None",
    task: str,
    dataset: str,
    params: dict[str, Any],
    total: int | None,
) -> str | None:
    """写一条 ``ingest_job``（status=running）。失败只记日志，不影响采集。"""
    from app.db.models.meta import IngestJob

    job_id = str(uuid.uuid4())
    row = {
        "job_id": job_id,
        "task": task,
        "dataset": dataset,
        "trade_date": None,
        "params": _json_safe(params),
        "status": "running",
        "total": total,
        "done": 0,
        "failed": 0,
        "rows_written": 0,
        "started_at": datetime.now(timezone.utc),
        "finished_at": None,
        "elapsed_ms": None,
        "error": None,
        "checkpoint": None,
    }
    try:
        async with _db_scope(db) as session:
            await upsert_rows(
                session,
                IngestJob,
                [row],
                ["job_id"],
                update_columns=[
                    "task", "dataset", "trade_date", "params", "status", "total",
                    "done", "failed", "rows_written", "started_at", "finished_at",
                    "elapsed_ms", "error", "checkpoint",
                ],
                update_timestamp=False,
            )
    except Exception:  # noqa: BLE001 —— 监控写失败不该拖垮采集
        logger.exception("写入 ingest_job 失败 task=%s", task)
        return None
    logger.info("估值任务开始 job=%s task=%s total=%s", job_id, task, total)
    return job_id

async def _job_update(
    db: "AsyncSession | None",
    job_id: str | None,
    *,
    done: int | None = None,
    failed: int | None = None,
    rows_written: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """更新进度与 checkpoint（断点续传的载体）。"""
    if job_id is None:
        return
    from sqlalchemy import update

    from app.db.models.meta import IngestJob

    fields: dict[str, Any] = {}
    if done is not None:
        fields["done"] = done
    if failed is not None:
        fields["failed"] = failed
    if rows_written is not None:
        fields["rows_written"] = rows_written
    if checkpoint is not None:
        fields["checkpoint"] = _json_safe(checkpoint)
    if error is not None:
        fields["error"] = error[:2000]
    if not fields:
        return
    try:
        async with _db_scope(db) as session:
            await session.execute(
                update(IngestJob).where(IngestJob.job_id == job_id).values(**fields)
            )
    except Exception:  # noqa: BLE001
        logger.exception("更新 ingest_job 失败 job=%s", job_id)

async def _job_finish(
    db: "AsyncSession | None",
    job_id: str | None,
    status: str,
    *,
    started_at: float,
    done: int | None = None,
    failed: int | None = None,
    rows_written: int | None = None,
    checkpoint: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """写任务终态与耗时。"""
    if job_id is None:
        return
    from sqlalchemy import update

    from app.db.models.meta import IngestJob

    fields: dict[str, Any] = {
        "status": status,
        "finished_at": datetime.now(timezone.utc),
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }
    if done is not None:
        fields["done"] = done
    if failed is not None:
        fields["failed"] = failed
    if rows_written is not None:
        fields["rows_written"] = rows_written
    if checkpoint is not None:
        fields["checkpoint"] = _json_safe(checkpoint)
    if error is not None:
        fields["error"] = error[:2000]
    try:
        async with _db_scope(db) as session:
            await session.execute(
                update(IngestJob).where(IngestJob.job_id == job_id).values(**fields)
            )
    except Exception:  # noqa: BLE001
        logger.exception("写入 ingest_job 终态失败 job=%s", job_id)
    logger.info(
        "估值任务结束 job=%s status=%s rows=%s failed=%s elapsed_ms=%s",
        job_id, status, rows_written, failed, fields["elapsed_ms"],
    )

async def _load_checkpoint(
    db: "AsyncSession | None", task: str, dataset: str, params: dict[str, Any]
) -> dict[str, Any]:
    """读最近一次同 task/同参数的 checkpoint（参数不一致就不用，避免串数据）。"""
    from sqlalchemy import select

    from app.db.models.meta import IngestJob

    try:
        async with _db_scope(db) as session:
            stmt = (
                select(IngestJob)
                .where(IngestJob.task == task, IngestJob.dataset == dataset)
                .order_by(IngestJob.started_at.desc())
                .limit(1)
            )
            job = (await session.execute(stmt)).scalars().first()
            if job is None or not isinstance(job.checkpoint, dict):
                return {}
            if job.checkpoint.get("params") == _json_safe(params):
                return job.checkpoint
    except Exception:  # noqa: BLE001
        logger.exception("读取断点失败 task=%s", task)
    return {}

async def _record_quality(
    db: "AsyncSession | None",
    report: QualityReport,
    trade_date: date | None,
    status: str,
) -> None:
    """写 ``meta_data_quality``（前端新鲜度徽标的唯一来源）。失败不影响主流程。"""
    try:
        async with _db_scope(db) as session:
            await record_quality(session, report.dataset, report, trade_date, status)
    except Exception:  # noqa: BLE001
        logger.exception("写入估值质量记录失败 dataset=%s", report.dataset)

async def _codes_with_prices(
    db: "AsyncSession | None",
    start: date,
    end: date,
    codes: Sequence[str] | None = None,
) -> list[str]:
    """区间内**有日线**的股票代码（区间内没行情就没必要算估值）。"""
    from sqlalchemy import select

    from app.db.models.fact import FactStockDaily

    stmt = (
        select(FactStockDaily.code)
        .where(
            FactStockDaily.trade_date >= start,
            FactStockDaily.trade_date <= end,
        )
        .distinct()
        .order_by(FactStockDaily.code)
    )
    target = _normalize_codes(codes) if codes is not None else []
    if target:
        stmt = stmt.where(FactStockDaily.code.in_(target))
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [str(code) for code in rows]

async def _load_financials_for_codes(
    db: "AsyncSession | None", codes: Sequence[str]
) -> pd.DataFrame:
    """从财报缓存表读一批股票的财报行（阶段 2 的输入，**零网络**）。

    列与 ``DimStockFinancial`` 一致（``build_valuation_frame`` 直接可用的形态）。
    """
    columns = [
        "code", "report_date", "announced_date", "eps_ttm", "bps", "payout_ratio",
        "eps", "roe", "revenue",
    ]
    empty = pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    target = _normalize_codes(codes)
    if not target:
        return empty

    from sqlalchemy import select

    from app.db.models.dim import DimStockFinancial

    stmt = (
        select(
            DimStockFinancial.code,
            DimStockFinancial.report_date,
            DimStockFinancial.announced_date,
            DimStockFinancial.eps_ttm,
            DimStockFinancial.bps,
            DimStockFinancial.payout_ratio,
            DimStockFinancial.eps,
            DimStockFinancial.roe,
            DimStockFinancial.revenue,
        )
        .where(DimStockFinancial.code.in_(target))
        .order_by(DimStockFinancial.code, DimStockFinancial.report_date)
    )
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).all()
    if not rows:
        return empty
    return pd.DataFrame(rows, columns=columns)

async def _load_price_rows(
    db: "AsyncSession | None", codes: Sequence[str], start: date, end: date
) -> pd.DataFrame:
    """读一批股票在 ``[start, end]`` 的收盘价与总市值（code/trade_date/close/market_cap）。"""
    columns = ["code", "trade_date", "close", "market_cap"]
    target = _normalize_codes(codes)
    if not target:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    from sqlalchemy import select

    from app.db.models.fact import FactStockDaily

    stmt = (
        select(
            FactStockDaily.code,
            FactStockDaily.trade_date,
            FactStockDaily.close,
            FactStockDaily.market_cap,
        )
        .where(
            FactStockDaily.trade_date >= start,
            FactStockDaily.trade_date <= end,
            FactStockDaily.code.in_(target),
        )
        .order_by(FactStockDaily.code, FactStockDaily.trade_date)
    )
    async with _db_scope(db) as session:
        rows = (await session.execute(stmt)).all()
    if not rows:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    return pd.DataFrame(rows, columns=columns)

def _valuation_update_rows(code: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    """估值展开表 → UPDATE 用的行（四列全空的行跳过）。"""
    if frame is None or len(frame) == 0:
        return []
    rows: list[dict[str, Any]] = []
    columns = ["trade_date", "pe_ttm", "pb", "dv_ttm"]
    if "ps_ttm" in frame.columns:
        columns.append("ps_ttm")
    for record in frame[columns].to_dict("records"):
        trade_date = record["trade_date"]
        if not isinstance(trade_date, date):
            continue
        pe = _as_db_value(record.get("pe_ttm"), 4)
        pb = _as_db_value(record.get("pb"), 4)
        dv = _as_db_value(record.get("dv_ttm"), 4)
        ps = _as_db_value(record.get("ps_ttm"), 4)
        if pe is None and pb is None and dv is None and ps is None:
            continue
        rows.append(
            {
                "code": code,
                "trade_date": trade_date,
                "pe_ttm": pe,
                "pb": pb,
                "dv_ttm": dv,
                "ps_ttm": ps,
            }
        )
    return rows

def _typed_null(value: Any, type_: Any) -> Any:
    """``None`` → 带类型的 SQL NULL（``CAST(NULL AS NUMERIC(...))``）。

    ``VALUES`` 里的裸 ``None`` 会被 SQLAlchemy 渲染成无类型字面量 ``NULL``。若某列在
    这一批里恰好全空（实测 300548 整段无 BPS），PostgreSQL 只能把该列推断成 ``text``，
    UPDATE numeric 列就抛 ``DatatypeMismatchError``。显式 CAST 让空列仍带数值类型。
    """
    if value is None:
        from sqlalchemy import cast, literal

        return cast(literal(None), type_)
    return value

def _valuation_update_statement(rows: Sequence[dict[str, Any]]) -> Any:
    """构造 ``UPDATE fact_stock_daily SET pe_ttm/pb/dv_ttm/ps_ttm FROM (VALUES ...)`` 语句。

    **只更新这四列**：不 INSERT（绝不凭空造出行情行）、不碰 close/volume/amount/adj_factor。
    ``ps_ttm`` 用 ``COALESCE(原值, 新值)``：快照/东财已写入的官方 PS 优先保留（它们按
    当日最新财报计算，比本模块 report_date+90 天的保守 PIT 口径更及时），只有原本为空
    的回补日才用「总市值 / 营收 TTM」补算 —— 这正好补上快照没跑的日子。
    """
    from sqlalchemy import Date, Numeric, String, column, func, update, values

    from app.db.models.fact import FactStockDaily

    table = FactStockDaily.__table__
    pe_type = Numeric(16, 4)
    pb_type = Numeric(16, 4)
    dv_type = Numeric(10, 4)
    ps_type = Numeric(16, 4)
    value_rows = [
        (
            row["code"],
            row["trade_date"],
            _typed_null(row.get("pe_ttm"), pe_type),
            _typed_null(row.get("pb"), pb_type),
            _typed_null(row.get("dv_ttm"), dv_type),
            _typed_null(row.get("ps_ttm"), ps_type),
        )
        for row in rows
    ]
    payload = values(
        column("code", String(6)),
        column("trade_date", Date()),
        column("pe_ttm", pe_type),
        column("pb", pb_type),
        column("dv_ttm", dv_type),
        column("ps_ttm", ps_type),
        name="v",
    ).data(value_rows)
    return (
        update(table)
        .where(table.c.code == payload.c.code, table.c.trade_date == payload.c.trade_date)
        .values(
            pe_ttm=payload.c.pe_ttm,
            pb=payload.c.pb,
            dv_ttm=payload.c.dv_ttm,
            ps_ttm=func.coalesce(table.c.ps_ttm, payload.c.ps_ttm),
            # 坑：FactStockDaily.updated_at 有 onupdate，SQLAlchemy 会给任何 UPDATE 自动
            # 补上 SET updated_at = now()。本函数的契约是"只更新估值列、不动其他列"，
            # 所以这里显式把它设成自引用（= 保持原值），把 onupdate 的隐式写入压掉。
            updated_at=table.c.updated_at,
        )
    )

async def _bulk_update_valuation(
    db: "AsyncSession | None", rows: Sequence[dict[str, Any]]
) -> int:
    """批量更新日线的估值三列，返回实际更新行数（``rowcount``）。"""
    if not rows:
        return 0
    total = 0
    async with _db_scope(db) as session:
        for chunk in chunked(list(rows), UPDATE_CHUNK_ROWS):
            statement = _valuation_update_statement(chunk)
            result = await session.execute(statement)
            affected = getattr(result, "rowcount", None)
            if affected is None or affected < 0:
                affected = len(chunk)
            total += int(affected)
    return total

__all__ = [
    "_today_cn",
    "_as_date",
    "_db_scope",
    "_as_db_value",
    "_normalize_codes",
    "report_lag_days",
    "_announced_date",
    "_QUARTER_END_DAYS",
    "latest_knowable_report_period",
    "_json_safe",
    "_financial_rows_from_frame",
    "_resolve_market_codes",
    "_load_latest_financial_dates",
    "_upsert_financial_rows",
    "_dividend_rows_from_frame",
    "_upsert_dividend_rows",
    "_load_dividend_updated",
    "_load_dividends_for_codes",
    "_job_begin",
    "_job_update",
    "_job_finish",
    "_load_checkpoint",
    "_record_quality",
    "_codes_with_prices",
    "_load_financials_for_codes",
    "_load_price_rows",
    "_valuation_update_rows",
    "_typed_null",
    "_valuation_update_statement",
    "_bulk_update_valuation",
]
