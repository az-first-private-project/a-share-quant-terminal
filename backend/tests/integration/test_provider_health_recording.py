"""V9 P0-3：Provider 健康度聚合（真实 PostgreSQL）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.db.models import ProviderHealth
from app.ingest.base import FetchResult, ProviderChain
from app.ingest.provider_health import summarize_provider_health
from app.ingest.service import _RecordingChain

pytestmark = pytest.mark.asyncio


class _FakeProvider:
    name = "fake"

    def available(self) -> bool:
        return True

    def fetch_daily_snapshot(self, trade_date) -> FetchResult:
        return FetchResult(rows=1, total=1, detail={"source": self.name})


async def test_recording_chain_writes_attempts(db_session) -> None:
    chain = _RecordingChain(
        ProviderChain([_FakeProvider()], name="fake-chain", max_retries=0),
        "snapshot",
        date(2026, 11, 17),
        db_session,
    )
    result = await chain.fetch_daily_snapshot(date(2026, 11, 17))
    assert result.rows == 1
    count = (
        await db_session.execute(select(func.count()).select_from(ProviderHealth))
    ).scalar_one()
    assert count == 1


async def test_summarize_provider_health(db_session) -> None:
    day = date(2026, 11, 17)
    rows = [
        ProviderHealth(provider="akshare", task="snapshot", result="ok", trade_date=day),
        ProviderHealth(provider="akshare", task="snapshot", result="failed", trade_date=day,
                       error_code="ProviderError: boom"),
        ProviderHealth(provider="akshare", task="snapshot", result="ok", trade_date=day),
        ProviderHealth(provider="baostock", task="snapshot", result="ok", trade_date=day),
        ProviderHealth(provider="sina", task="sector_members", result="empty", trade_date=day),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    items = await summarize_provider_health(db_session, days=7)
    by_key = {(item["provider"], item["task"]): item for item in items}
    assert by_key[("akshare", "snapshot")]["total"] == 3
    assert by_key[("akshare", "snapshot")]["degraded"] == 1
    assert by_key[("akshare", "snapshot")]["success_rate"] == 0.6667
    assert by_key[("sina", "sector_members")]["degraded"] == 1
    assert by_key[("sina", "sector_members")]["success_rate"] == 0.0
