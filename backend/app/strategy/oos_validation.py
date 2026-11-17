"""Walk-forward / 样本外验证（V9 P0-2）。

设计约束：
  - 复用 :func:`app.strategy.backtest.run_backtest` 的成交、费率、缓存与
    信号生成逻辑，不新建第二套回测引擎；
  - 训练窗与测试窗严格不重叠：``train_end < test_start``；
  - 数据不足时降级为 expanding window，并在每个 fold 上显式标注
    ``window_type="expanding"`` / ``degraded=True``，不静默缩短训练窗；
  - 所有 fold 全部返回，不允许只保留表现好的 fold；
  - 本模块只做验证编排，不修改 ``SPECS`` / 参数默认值。

用法（CLI 见 ``backend/scripts/oos_cli.py``）：

    folds = build_folds(date(2024, 1, 2), date(2026, 11, 16))
    result = await run_oos("a3", folds)
"""
from __future__ import annotations

import calendar
import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

from app.core.errors import InvalidParamError
from app.strategy.backtest import run_backtest
from app.strategy.metrics import METRIC_KEYS

logger = logging.getLogger(__name__)

#: 单个测试窗至少需要的自然日数；过短的窗口不产生有效调仓，直接跳过
_MIN_TEST_DAYS = 20


@dataclass(frozen=True)
class OOSFold:
    """一个训练/测试窗对（日期均含首尾）。"""

    fold_index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    window_type: str = "fixed"  # fixed | expanding
    degraded: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not (
            isinstance(self.train_start, date)
            and isinstance(self.train_end, date)
            and isinstance(self.test_start, date)
            and isinstance(self.test_end, date)
        ):
            raise InvalidParamError("OOS fold 的日期必须是 date")
        if self.train_start > self.train_end:
            raise InvalidParamError(
                "OOS fold 训练窗起点不能晚于终点",
                detail={
                    "train_start": self.train_start.isoformat(),
                    "train_end": self.train_end.isoformat(),
                },
            )
        if self.train_end >= self.test_start:
            raise InvalidParamError(
                "OOS fold 训练窗与测试窗重叠：train_end 必须早于 test_start",
                detail={
                    "train_end": self.train_end.isoformat(),
                    "test_start": self.test_start.isoformat(),
                },
            )
        if self.test_start > self.test_end:
            raise InvalidParamError(
                "OOS fold 测试窗起点不能晚于终点",
                detail={
                    "test_start": self.test_start.isoformat(),
                    "test_end": self.test_end.isoformat(),
                },
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "window_type": self.window_type,
            "degraded": self.degraded,
            "note": self.note,
        }


def _add_months(value: date, months: int) -> date:
    """按自然月平移，月末自动裁剪（1/31 + 1 月 → 2/28 或 2/29）。"""
    total = value.year * 12 + (value.month - 1) + int(months)
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _month_span(start: date, end: date) -> int:
    """两个日期之间的自然月数（向下取整）。"""
    return (end.year - start.year) * 12 + (end.month - start.month)


def _validate_window(
    start: date,
    end: date,
    *,
    train_months: int,
    test_months: int,
    step_months: int,
    min_folds: int,
) -> None:
    for name, value in (
        ("train_months", train_months),
        ("test_months", test_months),
        ("step_months", step_months),
        ("min_folds", min_folds),
    ):
        if int(value) <= 0:
            raise InvalidParamError(f"{name} 必须为正整数，收到: {value!r}")
    if start > end:
        raise InvalidParamError(
            "验证区间起点不能晚于终点",
            detail={"start": start.isoformat(), "end": end.isoformat()},
        )
    if step_months < test_months:
        # 步进小于测试窗会造成测试窗重叠，违反样本外独立性
        raise InvalidParamError(
            "step_months 必须大于等于 test_months，避免测试窗重叠",
            detail={"step_months": step_months, "test_months": test_months},
        )


def _fixed_folds(
    start: date,
    end: date,
    *,
    train_months: int,
    test_months: int,
    step_months: int,
) -> list[OOSFold]:
    folds: list[OOSFold] = []
    test_start = _add_months(start, train_months)
    index = 1
    while test_start <= end:
        test_end = min(_add_months(test_start, test_months) - timedelta(days=1), end)
        if (test_end - test_start).days < _MIN_TEST_DAYS:
            break
        train_end = test_start - timedelta(days=1)
        train_start = _add_months(test_start, -train_months)
        folds.append(
            OOSFold(
                fold_index=index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                window_type="fixed",
            )
        )
        index += 1
        if test_end >= end:
            break
        test_start = _add_months(test_start, step_months)
    return folds


def _expanding_folds(
    start: date,
    end: date,
    *,
    train_months: int,
    test_months: int,
    step_months: int,
    min_folds: int,
) -> list[OOSFold]:
    """固定起点、逐步扩张训练窗；用于数据长度不足固定 24 个月时。"""
    available = _month_span(start, end)
    # 先保证能切出 min_folds 个测试窗，再尽量保留更长的初始训练窗
    initial_train = available - test_months - (min_folds - 1) * step_months
    initial_train = max(6, min(int(train_months), int(initial_train)))
    folds: list[OOSFold] = []
    test_start = _add_months(start, initial_train)
    index = 1
    while test_start <= end:
        test_end = min(_add_months(test_start, test_months) - timedelta(days=1), end)
        if (test_end - test_start).days < _MIN_TEST_DAYS:
            break
        folds.append(
            OOSFold(
                fold_index=index,
                train_start=start,
                train_end=test_start - timedelta(days=1),
                test_start=test_start,
                test_end=test_end,
                window_type="expanding",
                degraded=True,
                note=(
                    f"数据不足固定 {train_months}/{test_months}/{step_months} 窗口，"
                    f"降级为 expanding：初始训练 {initial_train} 个月，测试窗逐折扩张"
                ),
            )
        )
        index += 1
        if test_end >= end:
            break
        test_start = _add_months(test_start, step_months)
    return folds


def build_folds(
    start: date,
    end: date,
    *,
    train_months: int = 24,
    test_months: int = 6,
    step_months: int = 6,
    min_folds: int = 4,
    allow_expanding: bool = True,
) -> list[OOSFold]:
    """构造 walk-forward 折。

    优先使用固定训练窗（默认 24/6/6）；若可用数据不足以切出 ``min_folds``
    个折，且 ``allow_expanding=True``，降级为 expanding window，并在
    每个 fold 上标注 ``degraded=True``。
    """
    _validate_window(
        start,
        end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        min_folds=min_folds,
    )
    fixed = _fixed_folds(
        start,
        end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
    )
    if len(fixed) >= min_folds or not allow_expanding:
        return fixed

    expanding = _expanding_folds(
        start,
        end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        min_folds=min_folds,
    )
    # 降级只在确实能增加折数时启用；否则保留固定窗结果，避免制造伪覆盖
    return expanding if len(expanding) > len(fixed) else fixed


def _pick_metrics(payload: Mapping[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics") or {}
    return {key: metrics.get(key) for key in METRIC_KEYS}


def _window_result(
    algo_code: str,
    start: date,
    end: date,
    strategy_params: Mapping[str, Any] | None,
    bt_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "algo": algo_code,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "strategy_params": dict(strategy_params or {}),
    }


async def _run_window(
    algo_code: str,
    start: date,
    end: date,
    *,
    strategy_params: Mapping[str, Any] | None = None,
    bt_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = _window_result(algo_code, start, end, strategy_params, bt_params)
    try:
        result = await run_backtest(
            algo_code,
            start,
            end,
            dict(strategy_params or {}),
            dict(bt_params or {}),
            db=None,
        )
    except Exception as exc:  # noqa: BLE001 - 单窗失败要留痕，不能中断整轮验证
        logger.exception("OOS 窗口失败 algo=%s %s~%s", algo_code, start, end)
        return {
            **base,
            "status": "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "metrics": {},
        }
    metrics = _pick_metrics(result)
    return {
        **base,
        "status": "success",
        "bt_id": result.get("bt_id"),
        "metrics": metrics,
    }


async def run_single_window(
    algo_code: str,
    start: date,
    end: date,
    *,
    strategy_params: Mapping[str, Any] | None = None,
    bt_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单窗口验证（用于参数对照，如 a4 pe_max=12 在 2022–2023 的样本外测试）。"""
    return await _run_window(
        algo_code,
        start,
        end,
        strategy_params=strategy_params,
        bt_params=bt_params,
    )


async def run_oos(
    algo_code: str,
    folds: Sequence[OOSFold],
    *,
    strategy_params: Mapping[str, Any] | None = None,
    bt_params: Mapping[str, Any] | None = None,
    include_train: bool = True,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """逐折运行训练窗与测试窗，返回全部结果与汇总。"""
    if not folds:
        raise InvalidParamError("OOS fold 列表为空，无法执行验证")

    rows: list[dict[str, Any]] = []
    for fold in folds:
        logger.info(
            "OOS fold %d/%d train=%s~%s test=%s~%s",
            fold.fold_index,
            len(folds),
            fold.train_start,
            fold.train_end,
            fold.test_start,
            fold.test_end,
        )
        train = None
        if include_train:
            train = await _run_window(
                algo_code,
                fold.train_start,
                fold.train_end,
                strategy_params=strategy_params,
                bt_params=bt_params,
            )
        test = await _run_window(
            algo_code,
            fold.test_start,
            fold.test_end,
            strategy_params=strategy_params,
            bt_params=bt_params,
        )
        row = {
            **fold.as_dict(),
            "train": train,
            "test": test,
        }
        rows.append(row)
        if progress is not None:
            progress(row)

    return {
        "algo": algo_code,
        "strategy_params": dict(strategy_params or {}),
        "bt_params": dict(bt_params or {}),
        "folds": rows,
        "summary": summarize_oos(rows),
    }


def _mean(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(statistics.mean(clean), 6) if clean else None


def _median(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(statistics.median(clean), 6) if clean else None


def summarize_oos(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """汇总测试窗指标：均值/中位数/正超额折数，不隐藏任何折。"""
    tests = [
        row["test"]
        for row in rows
        if isinstance(row.get("test"), Mapping) and row["test"].get("status") == "success"
    ]
    if not tests:
        return {
            "n_folds": len(rows),
            "evaluated_folds": 0,
            "failed_folds": len(rows),
            "note": "全部测试窗执行失败，无可用样本外指标",
        }

    def metric(name: str) -> list[float]:
        return [
            item["metrics"][name]
            for item in tests
            if isinstance(item.get("metrics"), Mapping)
            and item["metrics"].get(name) is not None
        ]

    annual = metric("annual_return")
    excess = metric("excess_return")
    sharpe = metric("sharpe")
    drawdown = metric("max_drawdown")
    trades = metric("trade_count")
    return {
        "n_folds": len(rows),
        "evaluated_folds": len(tests),
        "failed_folds": len(rows) - len(tests),
        "annual_return_mean": _mean(annual),
        "annual_return_median": _median(annual),
        "excess_return_mean": _mean(excess),
        "excess_return_median": _median(excess),
        "sharpe_mean": _mean(sharpe),
        "sharpe_median": _median(sharpe),
        "max_drawdown_mean": _mean(drawdown),
        "max_drawdown_median": _median(drawdown),
        "trade_count_mean": _mean(trades),
        "positive_excess_folds": sum(1 for value in excess if float(value) > 0),
        "negative_excess_folds": sum(1 for value in excess if float(value) < 0),
        "window_types": sorted({str(row.get("window_type")) for row in rows}),
        "degraded": any(bool(row.get("degraded")) for row in rows),
    }


__all__ = [
    "OOSFold",
    "build_folds",
    "run_oos",
    "run_single_window",
    "summarize_oos",
]
