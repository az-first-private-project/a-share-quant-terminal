"""回测韧性：调仓日数据缺口容错 + 瞬时数据库错误重试。

对应 Docker 场景"回测随机失败、调整调仓周期后恢复"：
调仓日相位变化会绕开数据缺口，旧实现把单日缺数据当整次失败。
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy.exc import OperationalError

from app.api.v1 import backtest as backtest_api
from app.core.errors import DataUnavailableError
from app.strategy.backtest import collect_signal_executions


def _days(*values: str) -> list[date]:
    return [date.fromisoformat(value) for value in values]


def test_collect_signal_executions_skips_missing_signal_day() -> None:
    days = _days("2026-01-05", "2026-01-06", "2026-01-07")
    next_day = {days[0]: days[1], days[1]: days[2]}
    calls: list[date] = []

    async def runner(day: date) -> dict:
        calls.append(day)
        if day == days[1]:
            raise DataUnavailableError(f"{day.isoformat()} 无个股行情数据")
        return {"data": [{"code": "1", "name": "A"}, {"code": "600519", "name": "B"}]}

    executions, skipped, codes = asyncio.run(collect_signal_executions(
        days, next_day, runner=runner, max_positions=2,
    ))

    assert calls == days[:2]  # 最后一个调仓日没有 T+1，不取信号
    assert list(executions) == [days[1]]
    assert len(executions[days[1]]) == 2
    assert [item["date"] for item in skipped] == ["2026-01-06"]
    assert skipped[0]["execution_date"] == "2026-01-07"
    assert "无个股行情数据" in skipped[0]["reason"]
    assert codes == {"000001", "600519"}


def test_collect_signal_executions_reraises_when_session_broken() -> None:
    day = date(2026, 1, 5)

    async def runner(_day: date) -> dict:
        raise DataUnavailableError("缺表：交易事务已失效")

    with pytest.raises(DataUnavailableError):
        asyncio.run(collect_signal_executions(
            [day], {day: date(2026, 1, 6)},
            runner=runner, max_positions=5, can_continue=lambda: False,
        ))


def test_transient_error_classification() -> None:
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    assert backtest_api._is_transient_db_error(
        OperationalError("SELECT 1", {}, Exception("connection reset"))
    )
    assert backtest_api._is_transient_db_error(SATimeoutError("pool timeout"))
    assert not backtest_api._is_transient_db_error(DataUnavailableError("无数据"))
    assert not backtest_api._is_transient_db_error(ValueError("bad param"))


def _execute(monkeypatch, fake_run) -> None:
    monkeypatch.setattr(backtest_api, "run_backtest", fake_run)
    monkeypatch.setattr(backtest_api, "_TRANSIENT_BACKOFF_SEC", 0.0)
    asyncio.run(backtest_api.execute_backtest(
        bt_id="bt-test",
        algo_code="a3",
        start=date(2026, 1, 1),
        end=date(2026, 6, 1),
        strategy_params={"limit": 5},
        bt_params={"rebalance_days": 20},
    ))


def test_execute_backtest_retries_transient_db_errors(monkeypatch) -> None:
    attempts: list[str] = []

    async def fake_run(**kwargs):
        attempts.append(kwargs["bt_id"])
        if len(attempts) == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    _execute(monkeypatch, fake_run)

    assert attempts == ["bt-test", "bt-test"]  # 复用同一 bt_id，不产生重复记录


def test_execute_backtest_does_not_retry_business_errors(monkeypatch) -> None:
    attempts: list[str] = []

    async def fake_run(**kwargs):
        attempts.append(kwargs["bt_id"])
        raise DataUnavailableError("调仓日全部缺数据")

    _execute(monkeypatch, fake_run)

    assert len(attempts) == 1


def test_execute_backtest_gives_up_after_retry_budget(monkeypatch) -> None:
    attempts: list[str] = []

    async def fake_run(**kwargs):
        attempts.append(kwargs["bt_id"])
        raise OperationalError("SELECT 1", {}, Exception("still down"))

    _execute(monkeypatch, fake_run)

    assert len(attempts) == backtest_api._TRANSIENT_RETRIES + 1
