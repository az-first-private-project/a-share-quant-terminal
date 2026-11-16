"""资金流日更降级：已有数据跳过与新浪缺口补齐。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date

from app.ingest import bulk_backfill, service


class _FakeJob:
    def __init__(self, *args, **kwargs) -> None:
        self.finishes: list[tuple[str, str | None]] = []

    async def begin(self, *args, **kwargs) -> None:
        return None

    async def update(self, *args, **kwargs) -> None:
        return None

    async def finish(self, status: str, error: str | None = None, **kwargs) -> None:
        self.finishes.append((status, error))


def test_moneyflow_sina_fill_only_missing_codes(monkeypatch) -> None:
    quoted = {"000001", "000002", "600000"}
    captured: dict = {}
    existing_calls = 0

    async def fake_quoted(db, trade_date):
        return quoted

    async def fake_existing(db, trade_date):
        nonlocal existing_calls
        existing_calls += 1
        return 2, {"000001", "000002"}

    async def fake_backfill(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(service, "_quoted_codes", fake_quoted)
    monkeypatch.setattr(service, "_moneyflow_existing", fake_existing)
    monkeypatch.setattr(bulk_backfill, "backfill_moneyflow", fake_backfill)

    count = asyncio.run(
        service._moneyflow_sina_fill(None, date(2026, 11, 13), {"000001"})
    )

    assert count == 2
    assert existing_calls == 1
    assert captured["codes"] == ["000002", "600000"]
    assert captured["start"] == date(2026, 11, 13)
    assert captured["end"] == date(2026, 11, 13)
    assert captured["force"] is True


def test_collect_moneyflow_skips_when_existing_coverage_ok(monkeypatch) -> None:
    job = _FakeJob()
    quality_calls: list[tuple] = []

    @asynccontextmanager
    async def fake_lock(task):
        yield True

    @asynccontextmanager
    async def fake_scope(db):
        yield object()

    async def fake_trade_day(db, day):
        return True

    async def fake_readiness(day, db):
        return None

    async def fake_quoted_count(db, day):
        return 100

    async def fake_existing(db, day):
        return 95, {f"{index:06d}" for index in range(95)}

    async def fake_record_quality(session, dataset, report, trade_date, status):
        quality_calls.append((dataset, report.rows, report.coverage, status))

    monkeypatch.setattr(service, "_advisory_lock", fake_lock)
    monkeypatch.setattr(service, "_JobTracker", lambda *a, **k: job)
    monkeypatch.setattr(service, "_is_trade_day", fake_trade_day)
    monkeypatch.setattr(service, "_snapshot_readiness", fake_readiness)
    monkeypatch.setattr(service, "_fact_rows_on", fake_quoted_count)
    monkeypatch.setattr(service, "_moneyflow_existing", fake_existing)
    monkeypatch.setattr(service, "_db_scope", fake_scope)
    monkeypatch.setattr(service, "record_quality", fake_record_quality)
    monkeypatch.setattr(service.config, "QUALITY_MIN_SNAPSHOT_ROWS", 100)

    result = asyncio.run(service.collect_moneyflow(date(2026, 11, 13)))

    assert result.detail["already_collected"] is True
    assert result.detail["coverage"] == 0.95
    assert result.quality_status == "ok"
    assert job.finishes == [("skipped", "already_collected")]
    assert quality_calls == [("moneyflow", 95, 0.95, "ok")]


def test_collect_moneyflow_falls_back_to_sina_when_chain_unavailable(monkeypatch) -> None:
    job = _FakeJob()
    quality_calls: list[tuple] = []

    class _Chain:
        async def fetch_moneyflow(self, *args, **kwargs):
            raise ConnectionError("东财重置")

    @asynccontextmanager
    async def fake_lock(task):
        yield True

    @asynccontextmanager
    async def fake_scope(db):
        yield object()

    async def fake_trade_day(db, day):
        return True

    async def fake_readiness(day, db):
        return None

    async def fake_quoted_count(db, day):
        return 100

    async def fake_existing(db, day):
        return 0, set()

    async def fake_fill(db, day, existing_codes):
        return 95

    async def fake_record_quality(session, dataset, report, trade_date, status):
        quality_calls.append((dataset, report.rows, report.coverage, status))

    monkeypatch.setattr(service, "_advisory_lock", fake_lock)
    monkeypatch.setattr(service, "_JobTracker", lambda *a, **k: job)
    monkeypatch.setattr(service, "_is_trade_day", fake_trade_day)
    monkeypatch.setattr(service, "_snapshot_readiness", fake_readiness)
    monkeypatch.setattr(service, "_fact_rows_on", fake_quoted_count)
    monkeypatch.setattr(service, "_moneyflow_existing", fake_existing)
    monkeypatch.setattr(service, "_moneyflow_sina_fill", fake_fill)
    monkeypatch.setattr(service, "_db_scope", fake_scope)
    monkeypatch.setattr(service, "get_provider_chain", lambda: _Chain())
    monkeypatch.setattr(service, "record_quality", fake_record_quality)
    monkeypatch.setattr(service.config, "QUALITY_MIN_SNAPSHOT_ROWS", 100)

    result = asyncio.run(service.collect_moneyflow(date(2026, 11, 16)))

    assert result.detail == {"source": "sina-fallback", "coverage": 0.95}
    assert result.quality_status == "ok"
    assert job.finishes == [("success", None)]
    assert quality_calls == [("moneyflow", 95, 0.95, "ok")]
