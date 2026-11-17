"""V9 P0-2：样本外窗口与编排测试（不连数据库）。"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.errors import InvalidParamError  # noqa: E402
from app.strategy import oos_validation as oos  # noqa: E402
from app.strategy.metrics import METRIC_KEYS  # noqa: E402


def test_fixed_folds_have_no_overlap() -> None:
    folds = oos.build_folds(date(2020, 1, 1), date(2026, 12, 31))
    assert len(folds) >= 4
    assert all(fold.window_type == "fixed" for fold in folds)
    assert all(fold.degraded is False for fold in folds)
    for fold in folds:
        assert fold.train_end < fold.test_start
        assert fold.test_start <= fold.test_end
    for previous, current in zip(folds, folds[1:]):
        assert current.test_start > previous.test_end


def test_short_history_degrades_to_expanding() -> None:
    folds = oos.build_folds(date(2024, 1, 2), date(2026, 11, 16))
    assert len(folds) >= 4
    assert all(fold.window_type == "expanding" for fold in folds)
    assert all(fold.degraded is True for fold in folds)
    for fold in folds:
        assert fold.train_start == date(2024, 1, 2)
        assert fold.train_end < fold.test_start
    for previous, current in zip(folds, folds[1:]):
        assert current.test_start > previous.test_end


def test_overlapping_step_is_rejected() -> None:
    with pytest.raises(InvalidParamError):
        oos.build_folds(
            date(2024, 1, 1),
            date(2026, 12, 31),
            train_months=12,
            test_months=6,
            step_months=3,
        )


def test_fold_rejects_train_test_overlap() -> None:
    with pytest.raises(InvalidParamError):
        oos.OOSFold(
            fold_index=1,
            train_start=date(2024, 1, 1),
            train_end=date(2024, 6, 1),
            test_start=date(2024, 6, 1),
            test_end=date(2024, 12, 31),
        )


def _metrics(value: float = 0.01) -> dict:
    return {key: value for key in METRIC_KEYS}


def test_run_oos_runs_train_and_test_for_every_fold(monkeypatch) -> None:
    calls: list[tuple[date, date]] = []

    async def fake_run_backtest(algo, start, end, strategy_params=None, bt_params=None, db=None):
        calls.append((start, end))
        return {"bt_id": f"bt-{start.isoformat()}", "metrics": _metrics(0.02)}

    monkeypatch.setattr(oos, "run_backtest", fake_run_backtest)
    folds = [
        oos.OOSFold(1, date(2024, 1, 1), date(2024, 12, 31), date(2025, 1, 1), date(2025, 6, 30)),
        oos.OOSFold(2, date(2024, 1, 1), date(2025, 6, 30), date(2025, 7, 1), date(2025, 12, 31)),
    ]

    result = asyncio.run(oos.run_oos("a3", folds, include_train=True))

    assert len(calls) == 4
    assert len(result["folds"]) == 2
    assert all(row["test"]["status"] == "success" for row in result["folds"])
    assert result["summary"]["evaluated_folds"] == 2
    assert result["summary"]["positive_excess_folds"] == 2


def test_run_oos_keeps_failed_fold_and_continues(monkeypatch) -> None:
    calls: list[date] = []

    async def fake_run_backtest(algo, start, end, strategy_params=None, bt_params=None, db=None):
        calls.append(start)
        if start == date(2025, 7, 1):
            raise RuntimeError("boom")
        return {"bt_id": f"bt-{start.isoformat()}", "metrics": _metrics()}

    monkeypatch.setattr(oos, "run_backtest", fake_run_backtest)
    folds = [
        oos.OOSFold(1, date(2024, 1, 1), date(2024, 12, 31), date(2025, 1, 1), date(2025, 6, 30)),
        oos.OOSFold(2, date(2024, 1, 1), date(2025, 6, 30), date(2025, 7, 1), date(2025, 12, 31)),
    ]

    result = asyncio.run(oos.run_oos("a3", folds, include_train=False))

    assert len(calls) == 2
    assert result["folds"][0]["test"]["status"] == "success"
    assert result["folds"][1]["test"]["status"] == "failed"
    assert result["summary"]["evaluated_folds"] == 1
    assert result["summary"]["failed_folds"] == 1


def test_single_window_uses_requested_range(monkeypatch) -> None:
    calls: list[tuple[date, date]] = []

    async def fake_run_backtest(algo, start, end, strategy_params=None, bt_params=None, db=None):
        calls.append((start, end))
        return {"bt_id": "bt-single", "metrics": _metrics(0.03)}

    monkeypatch.setattr(oos, "run_backtest", fake_run_backtest)
    result = asyncio.run(
        oos.run_single_window(
            "a4",
            date(2022, 1, 4),
            date(2023, 12, 29),
            strategy_params={"pe_max": 12},
        )
    )

    assert calls == [(date(2022, 1, 4), date(2023, 12, 29))]
    assert result["status"] == "success"
    assert result["strategy_params"] == {"pe_max": 12}
