"""V9 P1-5：推送自选过滤（真实 PostgreSQL）。"""
from __future__ import annotations

from datetime import date

import pytest

from app.db.models import StgRun, StgSignal
from app.notify import push

pytestmark = pytest.mark.asyncio


async def test_watchlist_only_filters_sections(db_session, monkeypatch) -> None:
    run_id = "run-watchlist-only"
    db_session.add(
        StgRun(
            run_id=run_id,
            algo_code="a3",
            algo_name="央企 × 综合评分TOP",
            trade_date=date(2026, 11, 17),
            params={"min_score": 6.0},
            signal_count=2,
            status="success",
            signal_schema=3,
        )
    )
    db_session.add_all(
        [
            StgSignal(run_id=run_id, code="601668", name="中国建筑", rank=1, score=8.1),
            StgSignal(run_id=run_id, code="600028", name="中国石化", rank=2, score=7.9),
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(push, "PUSH_WATCHLIST_ONLY", True)

    sections = await push._load_sections(  # noqa: SLF001
        db_session, date(2026, 11, 17), ["a3"], {"601668"}
    )
    assert sections[0]["signal_count"] == 1
    assert [item["code"] for item in sections[0]["items"]] == ["601668"]
