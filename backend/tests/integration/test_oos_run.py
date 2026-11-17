"""V9 P0-2：样本外结果表读写（真实 PostgreSQL）。"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.db.models import OOSRun
from app.strategy.oos_validation import load_oos_rows

pytestmark = pytest.mark.asyncio


def _metrics(annual: float = 0.1, excess: float = 0.02) -> dict:
    return {
        "annual_return": annual,
        "excess_return": excess,
        "sharpe": 1.0,
        "max_drawdown": -0.05,
        "trade_count": 10,
    }


async def test_save_and_load_oos_rows(db_session) -> None:
    oos_id = uuid.uuid4()
    for index in (1, 2):
        db_session.add(
            OOSRun(
                id=oos_id,
                fold_index=index,
                algo_code="a3",
                train_start=date(2024, 1, 1),
                train_end=date(2024, 12, 31),
                test_start=date(2025, index, 1),
                test_end=date(2025, index + 5, 1),
                window_type="expanding",
                degraded=True,
                metrics={"test": _metrics(0.1 * index), "train": _metrics(0.2)},
                params={"min_score": 6.0},
                param_fingerprint="abc123",
                signal_schema=3,
                status="success",
            )
        )
    await db_session.flush()

    payload = await load_oos_rows(db_session, "a3", str(oos_id))
    assert payload is not None
    assert payload["algo"] == "a3"
    assert payload["oos_id"] == str(oos_id)
    assert payload["status"] == "success"
    assert payload["degraded"] is True
    assert len(payload["folds"]) == 2
    assert payload["folds"][0]["test"]["metrics"]["annual_return"] == 0.1
    assert payload["summary"]["n_folds"] == 2
    assert payload["summary"]["positive_excess_folds"] == 2


async def test_load_latest_oos_group(db_session) -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    for group, annual in ((first, 0.1), (second, 0.2)):
        db_session.add(
            OOSRun(
                id=group,
                fold_index=1,
                algo_code="a4",
                train_start=date(2024, 1, 1),
                train_end=date(2024, 12, 31),
                test_start=date(2025, 1, 1),
                test_end=date(2025, 6, 30),
                window_type="expanding",
                degraded=True,
                metrics={"test": _metrics(annual), "train": None},
                params={"pe_max": 15.0},
                param_fingerprint="xyz",
                signal_schema=3,
                status="success",
            )
        )
    await db_session.flush()

    payload = await load_oos_rows(db_session, "a4")
    assert payload is not None
    assert payload["oos_id"] in {str(first), str(second)}
    assert len(payload["folds"]) >= 1
