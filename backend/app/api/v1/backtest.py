"""
回测 API（方案 D4/D5）
======================
POST /api/v1/backtest/{algo_code}          创建回测任务（立即返回，后台执行）
GET  /api/v1/backtest/{bt_id}              查询结果（queued/running/success/failed）
GET  /api/v1/backtest/{bt_id}/export.csv   净值曲线导出
"""
from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import (
    DBAPIError,
    InterfaceError,
    OperationalError,
    TimeoutError as SATimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import BACKTEST
from app.core.errors import (
    DataUnavailableError, InvalidParamError, NotFoundError, UnknownAlgoError,
)
from app.core.logging import get_logger
from app.db.models import (
    BacktestRun, DimStock, FactStockDaily, FactStockScoreDaily, OOSRun,
)
from app.db.session import AsyncSessionLocal, get_db
from app.engine.backtest_view import build_backtest_extras
from app.strategy.backtest import run_backtest
from app.strategy.grid import create_grid, execute_grid, get_grid, requeue_failed_cells
from app.strategy.oos_validation import build_folds, load_oos_rows, run_oos, save_oos_rows
from app.strategy.runner import SIGNAL_SCHEMA_VERSION, params_fingerprint
from app.strategy.strategies import CANDIDATE_SPECS, SPECS, get_spec

router = APIRouter(prefix="/api/v1/backtest", tags=["回测"])
logger = get_logger(__name__)


class GridRequest(BaseModel):
    """参数敏感性矩阵入参（T10）：轴名与单次回测一致。"""

    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
    rebalance_days: list[int] = Field(..., min_length=1, max_length=10)
    max_positions: list[int] = Field(..., min_length=1, max_length=10)
    strategy: dict[str, Any] | None = None


class OOSRequest(BaseModel):
    """样本外 walk-forward 入参（V9 P0-2）。"""

    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
    train_months: int = Field(24, ge=1, le=60)
    test_months: int = Field(6, ge=1, le=24)
    step_months: int = Field(6, ge=1, le=24)
    strategy: dict[str, Any] | None = None

#: 回测起点自动裁剪：A3 依赖评分、A4 依赖估值（C1–C6 从起点逐日判断）
async def _data_start(algo_code: str, db: AsyncSession) -> date | None:
    """该策略所需数据的最早可用日期。

    回测如果从"没有特征/评分/资金流"的日期开始，第一个调仓日就会因
    `DataUnavailableError` 整体失败。与其让用户猜，不如**自动裁剪起点**并在
    响应里写明 `effective_start`（诚实、可解释）。
    """
    if algo_code == "a3":
        return (await db.execute(select(func.min(FactStockScoreDaily.trade_date)))).scalar()
    if algo_code == "a4":
        return (await db.execute(
            select(func.min(FactStockDaily.trade_date)).where(FactStockDaily.pe_ttm.isnot(None))
        )).scalar()
    return None


async def _default_end_date(db: AsyncSession) -> date:
    """默认结束日：库内最新有行情的交易日，且不晚于今天。

    与 ``/health.latest_trade_date`` 同源（``fact_stock_daily.max(trade_date)``）。
    直接取 ``date.today()`` 会在周末/节假日或当日行情尚未落库时把非交易日
    写进回测区间，而净值曲线实际只到上一个交易日。
    """
    latest = (await db.execute(
        select(func.max(FactStockDaily.trade_date))
    )).scalar()
    if latest is None:
        raise DataUnavailableError(
            "数据库中尚无个股行情数据",
            next_action="POST /api/v1/admin/ingest/daily_snapshot",
            detail={"dataset": "fact_stock_daily"},
        )
    return min(date.today(), latest)


def _collect_strategy_params(algo_code: str, query: dict) -> dict:
    """按策略 schema 从 query string 收集策略参数（不传则用策略默认值）。"""
    params: dict = {}
    for field in get_spec(algo_code).params_schema:
        key = field["key"]
        raw = query.get(key)
        if raw is None or raw == "":
            continue
        if field.get("type") == "integer":
            try:
                params[key] = int(raw)
            except (TypeError, ValueError):
                raise InvalidParamError(f"参数 {key} 必须是整数，收到 {raw!r}") from None
        else:
            try:
                params[key] = float(raw)
            except (TypeError, ValueError):
                raise InvalidParamError(f"参数 {key} 必须是数值，收到 {raw!r}") from None
    return params


def _serialize(row: BacktestRun) -> dict:
    return {
        "bt_id": row.bt_id,
        "algo": row.algo_code,
        "start_date": row.start_date.isoformat(),
        "end_date": row.end_date.isoformat(),
        "params": row.params,
        "metrics": row.metrics,
        "equity_curve": row.equity_curve,
        "trades": row.trades,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _load_extras(
    curve: list[dict] | None,
    trades: list[dict] | None,
    start: date,
    end: date,
    params: dict | None,
    db: AsyncSession,
) -> dict:
    """为已落库的回测补充 T9 派生视图（只读，不重算回测）。"""
    initial_cash = float((params or {}).get("initial_cash") or 1_000_000.0)
    if not curve or not trades:
        return build_backtest_extras(curve, trades, {}, initial_cash=initial_cash)

    codes = sorted({
        str(item.get("code") or "").zfill(6)
        for item in trades
        if item.get("code")
    })
    daily_rows_by_code: dict[str, dict[str, float]] = {}
    industries: dict[str, str | None] = {}
    if codes:
        rows = (await db.execute(
            select(FactStockDaily.code, FactStockDaily.trade_date, FactStockDaily.close)
            .where(FactStockDaily.code.in_(codes))
            .where(FactStockDaily.trade_date >= start)
            .where(FactStockDaily.trade_date <= end)
        )).all()
        for code, trade_day, close in rows:
            if close is None:
                continue
            daily_rows_by_code.setdefault(code, {})[trade_day.isoformat()] = float(close)
        industries = dict((await db.execute(
            select(DimStock.code, DimStock.industry_l1).where(DimStock.code.in_(codes))
        )).all())

    return build_backtest_extras(
        curve,
        trades,
        daily_rows_by_code,
        industry_by_code=industries,
        initial_cash=initial_cash,
    )


#: 后台回测遇到瞬时数据库错误时的额外重试次数与退避基数（秒）
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF_SEC = 2.0


def _is_transient_db_error(exc: BaseException) -> bool:
    """是否是可通过重试恢复的瞬时数据库/连接错误。

    只覆盖连接被回收、容器网络抖动、连接池等待超时；业务性错误
    （数据缺失、参数非法）重试没有意义，直接落 failed。
    """
    if isinstance(exc, (OperationalError, InterfaceError, SATimeoutError, TimeoutError)):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


async def execute_backtest(
    *,
    bt_id: str,
    algo_code: str,
    start: date,
    end: date,
    strategy_params: dict,
    bt_params: dict,
) -> None:
    """后台执行回测；成功/失败都由引擎落库，异常只记日志不向事件循环抛出。

    瞬时数据库错误（连接被服务器回收、Docker 网络抖动、连接池等待超时）
    按指数退避重试，复用同一 ``bt_id``：引擎会把它重新置为 running，
    不会产生重复回测记录。业务性失败（数据不可用/参数非法）不重试。
    """
    attempts = _TRANSIENT_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            await run_backtest(
                algo_code=algo_code,
                start=start,
                end=end,
                strategy_params=strategy_params,
                bt_params=bt_params,
                bt_id=bt_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 失败已写入 backtest_run.status=failed
            if _is_transient_db_error(exc) and attempt < attempts:
                delay = _TRANSIENT_BACKOFF_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "后台回测遇到瞬时数据库错误，%.0fs 后重试（%d/%d）bt_id=%s: %s",
                    delay, attempt, attempts, bt_id, exc,
                )
                await asyncio.sleep(delay)
                continue
            logger.exception("后台回测失败 bt_id=%s algo=%s", bt_id, algo_code)
            return


async def execute_oos(
    *,
    oos_id: str,
    algo_code: str,
    folds,
    strategy_params: dict,
    bt_params: dict,
) -> None:
    """后台执行 walk-forward；逐折落库，单折失败不中断整组。"""
    group_id = uuid.UUID(str(oos_id))
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(OOSRun)
                .where(OOSRun.id == group_id)
                .values(status="running", error=None)
            )
            await db.commit()
            result = await run_oos(
                algo_code,
                folds,
                strategy_params=strategy_params,
                bt_params=bt_params,
                include_train=True,
            )
            await save_oos_rows(
                db, oos_id, algo_code, folds, result["folds"], strategy_params
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - 后台任务不能向事件循环抛
        logger.exception("后台样本外验证失败 oos_id=%s algo=%s", oos_id, algo_code)
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(OOSRun)
                    .where(OOSRun.id == group_id)
                    .values(status="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")
                )
                await db.commit()
        except Exception:  # pragma: no cover - 标记失败再失败只记日志
            logger.exception("标记 OOS failed 失败 oos_id=%s", oos_id)


@router.post("/{algo_code}")
async def create_backtest(
    algo_code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    start: Optional[date] = Query(None, description="默认近 1 年"),
    end: Optional[date] = Query(None, description="默认库内最新行情交易日"),
    rebalance_days: int = Query(BACKTEST.rebalance_days, ge=1, le=250),
    max_positions: int = Query(BACKTEST.max_positions, ge=1, le=100),
    benchmark: str = Query(BACKTEST.benchmark_code),
    commission: float = Query(BACKTEST.commission_rate, ge=0, le=0.01),
    stamp_tax: float = Query(BACKTEST.stamp_tax_rate, ge=0, le=0.01),
    slippage: float = Query(BACKTEST.slippage_rate, ge=0, le=0.02),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """创建回测任务并立即返回，实际计算在后台执行。

    同步阶段只做参数校验与数据起点裁剪（失败直接 4xx）；执行期错误（数据不足等）
    落 ``backtest_run.status=failed``，由前端轮询 GET 获取。策略参数从 query string
    按该策略的 `params_schema` 收集（A3/A4/C1–C6 参数各异）。
    """
    if algo_code not in SPECS and algo_code not in CANDIDATE_SPECS:
        raise UnknownAlgoError(f"未知策略: {algo_code}",
                               detail={"available": sorted(SPECS.keys())})

    if end is None:
        end = await _default_end_date(db)
    start = start or (end - timedelta(days=365))
    if start >= end:
        raise InvalidParamError("start 必须早于 end")
    if (end - start).days > 365 * 6:
        raise InvalidParamError("回测区间不能超过 6 年（本系统历史深度为近 5 年）")

    # 数据可用起点裁剪：避免"第一个调仓日没有特征/评分"导致整体 409
    requested_start = start
    data_start = await _data_start(algo_code, db)
    trimmed = False
    if data_start and data_start > start:
        start = data_start
        trimmed = True
        if start >= end:
            raise InvalidParamError(
                f"数据只从 {data_start} 开始，回测区间内没有可用数据",
                detail={"effective_start": data_start.isoformat()},
            )

    strategy_params = _collect_strategy_params(algo_code, dict(request.query_params))

    bt_params = {
        "rebalance_days": rebalance_days,
        "max_positions": max_positions,
        "benchmark_code": benchmark,
        "commission_rate": commission,
        "stamp_tax_rate": stamp_tax,
        "slippage_rate": slippage,
    }
    bt_id = str(uuid.uuid4())
    db.add(BacktestRun(
        bt_id=bt_id,
        algo_code=algo_code,
        start_date=start,
        end_date=end,
        params={**bt_params, "strategy": dict(strategy_params)},
        status="queued",
    ))
    # 先提交 queued 行：后台任务使用独立会话，必须能读到它
    await db.commit()
    background_tasks.add_task(
        execute_backtest,
        bt_id=bt_id,
        algo_code=algo_code,
        start=start,
        end=end,
        strategy_params=strategy_params,
        bt_params=bt_params,
    )
    return {
        "bt_id": bt_id,
        "algo": algo_code,
        "status": "queued",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "requested_start": requested_start.isoformat(),
        "effective_start": start.isoformat(),
        "start_trimmed": trimmed,
        "poll": f"/api/v1/backtest/{bt_id}",
    }


@router.post("/{algo_code}/grid")
async def create_backtest_grid(
    algo_code: str,
    payload: GridRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """创建参数敏感性矩阵（T10）：立即返回，逐格走后台任务。

    每格复用单次回测引擎（同一代码路径），命中同参数成功记录时直接复用不重算。
    """
    if algo_code not in SPECS and algo_code not in CANDIDATE_SPECS:
        raise UnknownAlgoError(f"未知策略: {algo_code}",
                               detail={"available": sorted(SPECS.keys())})

    requested_start = payload.start_date
    start = requested_start
    end = payload.end_date
    if start >= end:
        raise InvalidParamError("start_date 必须早于 end_date")
    if (end - start).days > 365 * 6:
        raise InvalidParamError("矩阵区间不能超过 6 年（本系统历史深度为近 5 年）")

    trimmed = False
    data_start = await _data_start(algo_code, db)
    if data_start and data_start > start:
        start = data_start
        trimmed = True
        if start >= end:
            raise InvalidParamError(
                f"数据只从 {data_start} 开始，矩阵区间内没有可用数据",
                detail={"effective_start": data_start.isoformat()},
            )

    strategy_params = payload.strategy or {}
    result = await create_grid(
        db,
        algo_code=algo_code,
        start=start,
        end=end,
        rebalance_days=payload.rebalance_days,
        max_positions=payload.max_positions,
        strategy_params=strategy_params,
    )

    if result["pending"]:
        background_tasks.add_task(
            execute_grid,
            algo_code=algo_code,
            start=start,
            end=end,
            strategy_params=strategy_params,
            pending=result["pending"],
        )

    return {
        "grid_id": result["grid_id"],
        "algo": algo_code,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "requested_start": requested_start.isoformat(),
        "effective_start": start.isoformat(),
        "start_trimmed": trimmed,
        "axes": result["axes"],
        "cells": result["cells"],
        "total": len(result["cells"]),
        "reused": result["reused"],
        "queued": len(result["pending"]),
        "elapsed_ms": 0,
    }


@router.post("/grid/{grid_id}/retry")
async def retry_backtest_grid(
    grid_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """重试矩阵内失败的格子（T10）。

    只重置 ``failed`` 格；成功格保持原记录，``queued/running`` 说明已有任务在跑，
    重复调用不会造成同格并发执行。每格按自己的参数快照与起止日重跑。
    """
    result = await requeue_failed_cells(db, grid_id)
    pending = result["pending"]
    if pending:
        background_tasks.add_task(
            execute_grid,
            algo_code=result["algo"],
            start=result["start"],
            end=result["end"],
            strategy_params=None,
            pending=pending,
        )
    return {
        "grid_id": result["grid_id"],
        "algo": result["algo"],
        "retried": len(pending),
        "queued": len(pending),
        "elapsed_ms": 0,
    }


@router.get("/grid/{grid_id}")
async def get_backtest_grid(grid_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """矩阵状态与每格指标摘要（只读，不重算任何回测）。"""
    return await get_grid(db, grid_id)


@router.get("/{algo_code}/oos")
async def get_oos_result(
    algo_code: str,
    oos_id: Optional[str] = Query(None, description="OOS 批次 ID；缺省取最新一组"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读取样本外 walk-forward 结果（只读，不重算）。"""
    if algo_code not in SPECS and algo_code not in CANDIDATE_SPECS:
        raise UnknownAlgoError(
            f"未知策略: {algo_code}", detail={"available": sorted(SPECS.keys())}
        )
    payload = await load_oos_rows(db, algo_code, oos_id)
    if payload is None:
        raise NotFoundError(
            f"策略 {algo_code} 尚无可用的样本外结果",
            detail={"algo": algo_code, "oos_id": oos_id},
        )
    return payload


@router.post("/{algo_code}/oos")
async def create_oos_result(
    algo_code: str,
    payload: OOSRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """创建样本外 walk-forward 任务并立即返回；结果由 GET 轮询。"""
    if algo_code not in SPECS and algo_code not in CANDIDATE_SPECS:
        raise UnknownAlgoError(
            f"未知策略: {algo_code}", detail={"available": sorted(SPECS.keys())}
        )
    if payload.start_date >= payload.end_date:
        raise InvalidParamError("start_date 必须早于 end_date")
    if (payload.end_date - payload.start_date).days > 365 * 6:
        raise InvalidParamError("样本外区间不能超过 6 年")
    if payload.step_months < payload.test_months:
        raise InvalidParamError("step_months 必须大于等于 test_months，避免测试窗重叠")

    active = (
        await db.execute(
            select(func.count())
            .select_from(OOSRun)
            .where(OOSRun.algo_code == algo_code)
            .where(OOSRun.status.in_(("pending", "running")))
        )
    ).scalar_one()
    if int(active or 0) > 0:
        raise InvalidParamError(
            f"策略 {algo_code} 已有样本外任务在排队/运行中",
            code="BUSY",
            status_code=429,
            suggestion="等待当前任务完成后再试",
        )

    requested_start = payload.start_date
    start = requested_start
    data_start = await _data_start(algo_code, db)
    trimmed = False
    if data_start and data_start > start:
        start = data_start
        trimmed = True
        if start >= payload.end_date:
            raise InvalidParamError(
                f"数据只从 {data_start} 开始，样本外区间内没有可用数据",
                detail={"effective_start": data_start.isoformat()},
            )

    strategy_params = dict(payload.strategy or {})
    folds = build_folds(
        start,
        payload.end_date,
        train_months=payload.train_months,
        test_months=payload.test_months,
        step_months=payload.step_months,
    )
    if not folds:
        raise InvalidParamError(
            "当前区间无法切出任何样本外折，请放宽区间或缩短训练窗",
            detail={
                "effective_start": start.isoformat(),
                "end": payload.end_date.isoformat(),
                "train_months": payload.train_months,
                "test_months": payload.test_months,
            },
        )

    oos_uuid = uuid.uuid4()
    oos_id = str(oos_uuid)
    fingerprint = params_fingerprint(algo_code, strategy_params)
    for fold in folds:
        db.add(
            OOSRun(
                id=oos_uuid,
                fold_index=fold.fold_index,
                algo_code=algo_code,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                window_type=fold.window_type,
                degraded=fold.degraded,
                status="pending",
                params=strategy_params,
                param_fingerprint=fingerprint,
                signal_schema=int(SIGNAL_SCHEMA_VERSION),
            )
        )
    await db.commit()

    bt_params = {
        "rebalance_days": int(BACKTEST.rebalance_days),
        "max_positions": int(BACKTEST.max_positions),
        "benchmark": str(BACKTEST.benchmark_code),
        "commission": float(BACKTEST.commission_rate),
        "stamp_tax": float(BACKTEST.stamp_tax_rate),
        "slippage": float(BACKTEST.slippage_rate),
    }
    background_tasks.add_task(
        execute_oos,
        oos_id=oos_id,
        algo_code=algo_code,
        folds=folds,
        strategy_params=strategy_params,
        bt_params=bt_params,
    )
    return {
        "oos_id": oos_id,
        "algo": algo_code,
        "status": "queued",
        "requested_start": requested_start.isoformat(),
        "effective_start": start.isoformat(),
        "start_trimmed": trimmed,
        "window_type": folds[0].window_type,
        "degraded": any(fold.degraded for fold in folds),
        "folds": [fold.as_dict() for fold in folds],
        "poll": f"/api/v1/backtest/{algo_code}/oos?oos_id={oos_id}",
    }


@router.get("/{bt_id}")
async def get_backtest(bt_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = (await db.execute(
        select(BacktestRun).where(BacktestRun.bt_id == bt_id)
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"回测不存在: {bt_id}", detail={"bt_id": bt_id})
    payload = _serialize(row)
    payload.update(await _load_extras(
        row.equity_curve, row.trades, row.start_date, row.end_date, row.params, db
    ))
    return payload


@router.get("/{bt_id}/trades")
async def get_backtest_trades(
    bt_id: str,
    code: Optional[str] = Query(None, min_length=1, max_length=6, pattern=r"^\d{1,6}$"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """回测成交明细（可只取某只股票），用于 K 线买卖点标记。

    直接读 `backtest_run.trades`，**不重新计算回测**；`shares=0` 的记录是被拒单
    （涨跌停/停牌/无数据），前端需与真实成交区分展示。
    """
    row = (await db.execute(
        select(BacktestRun).where(BacktestRun.bt_id == bt_id)
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"回测不存在: {bt_id}", detail={"bt_id": bt_id})

    all_trades = list(row.trades or [])
    target = code.zfill(6) if code else None
    items = [t for t in all_trades if target is None or str(t.get("code", "")).zfill(6) == target]
    return {
        "bt_id": row.bt_id,
        "algo_code": row.algo_code,
        "code": target,
        "total": len(all_trades),
        "count": len(items),
        "items": items,
    }


@router.get("/{bt_id}/export.csv")
async def export_backtest_csv(
    bt_id: str,
    code: Optional[str] = Query(None, min_length=1, max_length=6, pattern=r"^\d{1,6}$"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    row = (await db.execute(
        select(BacktestRun).where(BacktestRun.bt_id == bt_id)
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"回测不存在: {bt_id}", detail={"bt_id": bt_id})

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date", "nav", "benchmark"])
    for point in (row.equity_curve or []):
        writer.writerow([point.get("date"), point.get("nav"), point.get("benchmark")])

    extras = await _load_extras(
        row.equity_curve, row.trades, row.start_date, row.end_date, row.params, db
    )

    def _pct2(value: float | None) -> float | str:
        return "" if value is None else round(value, 2)

    # 月度与回撤 block：与界面同口径（百分数，2 位小数）
    writer.writerow([])
    writer.writerow(["# monthly returns (%)"])
    writer.writerow(["month", "strategy", "benchmark", "excess"])
    for item in extras["monthly_returns"]:
        writer.writerow([
            item.get("ym"), _pct2(item.get("ret")),
            _pct2(item.get("benchmark")), _pct2(item.get("excess")),
        ])
    writer.writerow([])
    writer.writerow(["# drawdown (%)"])
    writer.writerow(["date", "nav", "peak", "drawdown"])
    for item in extras["drawdown_curve"]:
        writer.writerow([
            item.get("date"), item.get("nav"), item.get("peak"),
            _pct2(item.get("drawdown")),
        ])

    writer.writerow([])
    writer.writerow(["# trades"])
    writer.writerow(["date", "code", "name", "side", "price", "shares", "amount", "fee", "reason"])
    target = code.zfill(6) if code else None
    trades = [
        t for t in (row.trades or [])
        if target is None or str(t.get("code", "")).zfill(6) == target
    ]
    if target:
        # C1：与 /trades?code= 的过滤口径保持一致，导出范围随展示范围
        writer.writerow([f"# filtered by code={target}"])
    for t in trades:
        writer.writerow([t.get("date"), t.get("code"), t.get("name"), t.get("side"),
                         t.get("price"), t.get("shares"), t.get("amount"),
                         t.get("fee"), t.get("reason")])

    return Response(
        content=("\ufeff" + buffer.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="backtest_{row.algo_code}_{bt_id[:8]}.csv"'},
    )


__all__ = ["router"]
