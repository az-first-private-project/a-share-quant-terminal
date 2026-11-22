"""ETF 同步 DB 集成测试：种子池 → 历史 → 映射 → 覆盖率。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.compass.etf_sync import coverage_report, sync_etf_data
from app.db.models import DimEtfSectorMap, FactEtfDaily, FactSectorDaily, SectorScore

pytestmark = pytest.mark.asyncio

TRADE_DATE = date(2026, 11, 6)


def fake_fetch(code: str) -> list[dict]:
    if code not in {"512480", "512760", "515790"}:
        return []
    return [
        {"date": "2026-11-05", "open": 1.0, "high": 1.1, "low": 0.9,
         "close": 1.0, "volume": 100, "amount": 1.0e8},
        {"date": "2026-11-06", "open": 1.0, "high": 1.2, "low": 1.0,
         "close": 1.1, "volume": 100, "amount": 1.2e8},
    ]


async def test_sync_etf_data_generates_mapping_and_daily_rows(db_session) -> None:
    for code, name, composite in (
        ("TST_SEMI", "半导体", 8.0),
        ("TST_PV", "光伏设备", 7.0),
        ("TST_MISC", "综合", 6.0),
    ):
        db_session.add(FactSectorDaily(
            sector_code=code,
            trade_date=TRADE_DATE,
            sector_name=name,
            sector_type="industry",
            src="test",
        ))
        db_session.add(SectorScore(
            trade_date=TRADE_DATE,
            sector_code=code,
            sector_name=name,
            composite_score=composite,
        ))
    await db_session.flush()

    result = await sync_etf_data(
        db_session, TRADE_DATE, mode="seed", fetcher=fake_fetch, limit=3,
    )

    assert result["status"] == "ok"
    assert result["sectors"] == 3
    assert result["mapped"] == 2
    assert result["coverage"] == 0.6667
    assert result["etf_fetched"] == 3
    assert result["etf_rows"] == 6

    daily_count = (await db_session.execute(
        select(func.count()).select_from(FactEtfDaily)
        .where(FactEtfDaily.trade_date == TRADE_DATE)
    )).scalar()
    assert daily_count == 3

    mapping_rows = (await db_session.execute(
        select(DimEtfSectorMap).where(DimEtfSectorMap.valid_to.is_(None))
    )).scalars().all()
    assert len(mapping_rows) == 2
    semi = next(row for row in mapping_rows if row.sector_code == "TST_SEMI")
    assert semi.etf_code == "512480"

    report = await coverage_report(db_session, TRADE_DATE)
    assert report["total"] == 3
    assert report["mapped"] == 2
    assert report["coverage"] == 0.6667
    assert report["pool_total"] == 3
    assert report["pool_mapped"] == 2
    assert report["pool_coverage"] == 0.6667
    assert report["etf_daily_covered"] == 2  # 只统计被映射的 ETF，未映射的 512760 不计入
    assert report["missing"][0]["sector_name"] == "综合"

    # 同日重复同步必须幂等：不新增映射行、不触发唯一约束
    again = await sync_etf_data(
        db_session, TRADE_DATE, mode="seed", fetcher=fake_fetch, limit=3,
    )
    assert again["status"] == "ok"
    assert again["mapped"] == 2
    mapping_again = (await db_session.execute(
        select(DimEtfSectorMap).where(DimEtfSectorMap.valid_to.is_(None))
    )).scalars().all()
    assert len(mapping_again) == 2
