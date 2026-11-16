"""股票池源瞬时不可用时沿用旧池的降级测试。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date

import pandas as pd

from app.ingest import service
from app.ingest.base import FetchResult


class _FakeJob:
    def __init__(self, *args, **kwargs) -> None:
        self.finishes: list[tuple[str, str | None]] = []

    async def begin(self, *args, **kwargs) -> None:
        return None

    async def update(self, *args, **kwargs) -> None:
        return None

    async def finish(self, status: str, error: str | None = None, **kwargs) -> None:
        self.finishes.append((status, error))


class _FakeChain:
    async def fetch_stock_universe(self) -> FetchResult:
        frame = pd.DataFrame(
            {
                "code": [f"{600000 + index:06d}" for index in range(3862)],
                "name": [f"股票{index}" for index in range(3862)],
            }
        )
        return FetchResult(rows=len(frame), total=len(frame), detail={"data": frame})


def test_universe_source_incomplete_reuses_existing_pool(monkeypatch) -> None:
    job = _FakeJob()
    quality_calls: list[tuple] = []
    isolated_calls: list[tuple] = []

    @asynccontextmanager
    async def fake_lock(task):
        yield True

    @asynccontextmanager
    async def fake_scope(db):
        yield object()

    async def fake_existing(db):
        return {
            f"{600000 + index:06d}": {
                "status": 1,
                "delist_date": None,
            }
            for index in range(5225)
        }

    async def fake_record_quality(session, dataset, report, trade_date, status):
        quality_calls.append((dataset, report, status))

    async def fake_isolated(dataset, report, trade_date, status):
        isolated_calls.append((dataset, report, status))

    monkeypatch.setattr(service, "_advisory_lock", fake_lock)
    monkeypatch.setattr(service, "_JobTracker", lambda *a, **k: job)
    monkeypatch.setattr(service, "get_provider_chain", lambda: _FakeChain())
    monkeypatch.setattr(service, "_filter_pool_frame", lambda frame: frame)
    monkeypatch.setattr(service, "_load_existing_stocks", fake_existing)
    monkeypatch.setattr(service, "is_pool_stock_code", lambda code: True)
    monkeypatch.setattr(service, "_db_scope", fake_scope)
    monkeypatch.setattr(service, "record_quality", fake_record_quality)
    monkeypatch.setattr(service, "_record_quality_isolated", fake_isolated)
    monkeypatch.setattr(service, "_today_cn", lambda: date(2026, 11, 16))
    monkeypatch.setattr(service.config, "QUALITY_MIN_UNIVERSE", 4000)

    result = asyncio.run(service.sync_stock_universe())

    assert result.detail["source"] == "existing"
    assert result.detail["existing"] == 5225
    assert result.detail["fetched"] == 3862
    assert result.detail["skipped"] == "universe_source_incomplete"
    assert job.finishes == [("skipped", "universe_source_incomplete")]
    assert isolated_calls == []
    assert len(quality_calls) == 1
    _dataset, report, status = quality_calls[0]
    assert status == "ok"
    assert report.rows == 5225
    assert report.ok is True
    assert {item["type"] for item in report.anomalies} == {"universe_source_incomplete"}
