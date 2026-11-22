"""ETF 数据同步：种子/实时 ETF 池 → Sina 历史行情 → 映射与日行情落库。

数据依赖链：
1. ETF 池：优先实时现货接口；失败时回退到 SEED_ETF_UNIVERSE；
2. 历史行情：Sina `fund_etf_hist_sina`（提供 OHLC/成交额，不提供规模与溢价）；
3. 规模：种子/实时池提供，缺失时记 0 并由卡片门槛拦截；
4. 溢价：当前数据源不提供，按 0 处理并在覆盖率报告中标注；
5. 板块映射：由 `etf_mapping.build_mapping` 按名称/别名生成。
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.compass.etf_mapping import SEED_ETF_UNIVERSE, build_mapping
from app.core.logging import get_logger
from app.db.models import DimEtfSectorMap, FactEtfDaily, FactSectorDaily, SectorScore

logger = get_logger(__name__)

PREMIUM_SOURCE = "unavailable_assumed_zero"
AUM_SOURCE = "seed_or_live_pool"
HISTORY_SOURCE = "sina"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sina_symbol(code: str) -> str:
    text = str(code).strip()
    if text.startswith("5"):
        return f"sh{text}"
    if text.startswith("1"):
        return f"sz{text}"
    return text


def fetch_sina_history(code: str) -> list[dict]:
    """同步拉取单只 ETF 的 Sina 日线；失败由调用方隔离。"""
    import akshare as ak

    frame = ak.fund_etf_hist_sina(symbol=sina_symbol(code))
    if frame is None or len(frame) == 0:
        return []
    return frame.tail(80).to_dict("records")


def normalize_history(code: str, rows: list[dict], aum: float) -> list[dict]:
    """把 Sina 行归一为 fact_etf_daily 字段，并补 pre_close/pct_chg。"""
    parsed: list[dict] = []
    for row in rows:
        raw_date = row.get("date")
        close = _f(row.get("close"))
        if raw_date is None or close is None:
            continue
        try:
            day = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        parsed.append({
            "trade_date": day,
            "etf_code": code,
            "open": _f(row.get("open")),
            "high": _f(row.get("high")),
            "low": _f(row.get("low")),
            "close": close,
            "amount": _f(row.get("amount")),
        })
    parsed.sort(key=lambda item: item["trade_date"])

    result: list[dict] = []
    previous_close: float | None = None
    for item in parsed:
        pre_close = previous_close
        pct_chg = (
            round((item["close"] / pre_close - 1.0) * 100.0, 3)
            if pre_close and pre_close > 0 else None
        )
        result.append({
            **item,
            "pre_close": pre_close,
            "pct_chg": pct_chg,
            "aum": aum,
            "premium_rate": 0.0,
            "is_suspended": False,
            "updated_at": datetime.now(),
        })
        previous_close = item["close"]
    return result


async def _fetch_many(
    pool: list[dict],
    fetcher: Callable[[str], list[dict]],
    *,
    concurrency: int = 1,
) -> dict[str, list[dict]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, list[dict]] = {}

    async def one(item: dict) -> None:
        code = str(item.get("etf_code"))
        async with semaphore:
            try:
                rows = await asyncio.to_thread(fetcher, code)
            except Exception as exc:  # noqa: BLE001 - 单只 ETF 失败不影响整体
                logger.warning("ETF 历史拉取失败 code=%s: %s", code, type(exc).__name__)
                rows = []
            results[code] = rows or []

    await asyncio.gather(*(one(item) for item in pool))
    return results


def _live_pool() -> list[dict]:
    try:
        import akshare as ak

        frame = ak.fund_etf_spot_em()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ETF 实时现货接口不可用: %s", type(exc).__name__)
        return []
    rows: list[dict] = []
    for record in frame.to_dict("records"):
        code = str(record.get("代码") or "").zfill(6)
        name = str(record.get("名称") or "")
        aum = _f(record.get("总市值")) or _f(record.get("流通市值")) or 0.0
        if code and name:
            rows.append({
                "etf_code": code,
                "etf_name": name,
                "sector_hint": "",
                "aum": aum,
            })
    return rows


async def _target_date(db: AsyncSession, trade_date: date | None) -> date | None:
    if trade_date is not None:
        return trade_date
    return (await db.execute(
        select(func.max(FactSectorDaily.trade_date))
    )).scalar()


async def _industry_sectors(db: AsyncSession, trade_date: date) -> list[dict]:
    rows = (await db.execute(
        select(FactSectorDaily.sector_code, FactSectorDaily.sector_name)
        .where(
            FactSectorDaily.trade_date == trade_date,
            FactSectorDaily.sector_type == "industry",
        )
        .distinct()
        .order_by(FactSectorDaily.sector_code)
    )).all()
    return [{"sector_code": code, "sector_name": name} for code, name in rows]


async def _upsert_etf_daily(db: AsyncSession, values: list[dict]) -> None:
    if not values:
        return
    # asyncpg 单条语句参数上限 32767；按行分批写入
    batch_size = 500
    for start in range(0, len(values), batch_size):
        chunk = values[start:start + batch_size]
        stmt = pg_insert(FactEtfDaily).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "etf_code"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "pre_close": stmt.excluded.pre_close,
                "pct_chg": stmt.excluded.pct_chg,
                "amount": stmt.excluded.amount,
                "aum": stmt.excluded.aum,
                "premium_rate": stmt.excluded.premium_rate,
                "is_suspended": stmt.excluded.is_suspended,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.execute(stmt)


async def _upsert_mapping(
    db: AsyncSession,
    mapping: list[dict],
    trade_date: date,
    source: str,
) -> None:
    by_sector = {item["sector_code"]: item for item in mapping}
    if not by_sector:
        return
    existing = (await db.execute(
        select(DimEtfSectorMap).where(
            DimEtfSectorMap.sector_code.in_(list(by_sector)),
            DimEtfSectorMap.valid_to.is_(None),
        )
    )).scalars().all()
    existing_by_sector = {row.sector_code: row for row in existing}

    # 同日重复同步可能把同一 (sector, etf, valid_from) 先关闭再重新打开；
    # 先查历史行，存在则复用，避免唯一约束冲突。
    history_rows = (await db.execute(
        select(DimEtfSectorMap).where(
            DimEtfSectorMap.sector_code.in_(list(by_sector)),
            DimEtfSectorMap.valid_from == trade_date,
        )
    )).scalars().all()
    history_by_key = {
        (row.sector_code, row.etf_code): row for row in history_rows
    }

    close_rows = [
        row for row in existing
        if by_sector[row.sector_code]["etf_code"] != row.etf_code
    ]
    for row in close_rows:
        row.valid_to = trade_date
    if close_rows:
        await db.flush()

    for sector_code, item in by_sector.items():
        old = existing_by_sector.get(sector_code)
        if old is not None and old.etf_code == item["etf_code"]:
            continue
        historical = history_by_key.get((sector_code, item["etf_code"]))
        if historical is not None:
            historical.valid_to = None
            historical.etf_name = item["etf_name"]
            historical.source = source
            historical.is_primary = True
            continue
        db.add(DimEtfSectorMap(
            sector_code=sector_code,
            etf_code=item["etf_code"],
            etf_name=item["etf_name"],
            is_primary=True,
            valid_from=trade_date,
            source=source,
        ))
    await db.flush()


async def sync_etf_data(
    db: AsyncSession,
    trade_date: date | None = None,
    *,
    mode: str = "auto",
    fetcher: Callable[[str], list[dict]] | None = None,
    limit: int | None = None,
) -> dict:
    """同步 ETF 日行情并重建板块映射。mode: auto / seed / live。"""
    target = await _target_date(db, trade_date)
    if target is None:
        return {"status": "no_sector_data", "reason": "no trade date"}

    sectors = await _industry_sectors(db, target)
    if not sectors:
        return {"status": "no_sector_data", "trade_date": target.isoformat()}

    pool: list[dict] = []
    source = mode
    if mode in {"auto", "live"}:
        pool = _live_pool()
        if not pool and mode == "auto":
            source = "seed_fallback"
    if not pool:
        if mode == "live":
            return {"status": "provider_failed", "trade_date": target.isoformat()}
        pool = [dict(item) for item in SEED_ETF_UNIVERSE]
        source = "seed" if source == "seed_fallback" else mode

    if limit is not None:
        pool = pool[:max(1, limit)]

    histories = await _fetch_many(pool, fetcher or fetch_sina_history)
    available = [item for item in pool if histories.get(str(item.get("etf_code")))]
    available_codes = {str(item["etf_code"]) for item in available}

    values: list[dict] = []
    for item in available:
        code = str(item["etf_code"])
        values.extend(normalize_history(code, histories[code], float(item.get("aum") or 0.0)))
    await _upsert_etf_daily(db, values)

    mapping = build_mapping(sectors, available)
    await _upsert_mapping(db, mapping["items"], target, source)

    result = {
        "status": "ok",
        "trade_date": target.isoformat(),
        "mode": source,
        "sectors": mapping["total"],
        "mapped": mapping["mapped"],
        "coverage": mapping["coverage"],
        "missing": mapping["missing"],
        "etf_fetched": len(available_codes),
        "etf_rows": len(values),
        "premium_source": PREMIUM_SOURCE,
        "aum_source": AUM_SOURCE,
        "history_source": HISTORY_SOURCE,
    }
    logger.info(
        "ETF 同步完成 date=%s mode=%s sectors=%d mapped=%d coverage=%.2f etfs=%d rows=%d",
        target, source, result["sectors"], result["mapped"], result["coverage"],
        result["etf_fetched"], result["etf_rows"],
    )
    return result


async def coverage_report(
    db: AsyncSession,
    trade_date: date | None = None,
) -> dict:
    target = await _target_date(db, trade_date)
    if target is None:
        return {"status": "no_sector_data"}
    sectors = await _industry_sectors(db, target)
    sector_codes = [item["sector_code"] for item in sectors]

    mapping_rows = (await db.execute(
        select(DimEtfSectorMap).where(
            DimEtfSectorMap.sector_code.in_(sector_codes),
            DimEtfSectorMap.valid_from <= target,
            DimEtfSectorMap.valid_to.is_(None) | (DimEtfSectorMap.valid_to > target),
        )
    )).scalars().all()
    mapped_codes = {row.sector_code for row in mapping_rows}
    missing = [
        item for item in sectors
        if item["sector_code"] not in mapped_codes
    ]
    etf_codes = [row.etf_code for row in mapping_rows]
    covered = 0
    if etf_codes:
        covered = (await db.execute(
            select(func.count(func.distinct(FactEtfDaily.etf_code))).where(
                FactEtfDaily.trade_date == target,
                FactEtfDaily.etf_code.in_(etf_codes),
            )
        )).scalar() or 0

    mapped = len(mapped_codes)
    total = len(sectors)

    industry_codes = {item["sector_code"] for item in sectors}
    score_rows = (await db.execute(
        select(SectorScore.sector_code, SectorScore.sector_name)
        .where(SectorScore.trade_date == target)
        .order_by(SectorScore.composite_score.desc().nullslast())
        .limit(30)
    )).all()
    pool = [row for row in score_rows if row[0] in industry_codes][:8]
    pool_mapped = [row for row in pool if row[0] in mapped_codes]
    pool_coverage = round(len(pool_mapped) / len(pool), 4) if pool else 0.0

    return {
        "status": "ok",
        "trade_date": target.isoformat(),
        "total": total,
        "mapped": mapped,
        "coverage": round(mapped / total, 4) if total else 0.0,
        "pool_total": len(pool),
        "pool_mapped": len(pool_mapped),
        "pool_coverage": pool_coverage,
        "pool_missing": [
            {"sector_code": code, "sector_name": name}
            for code, name in pool if code not in mapped_codes
        ],
        "etf_daily_covered": covered,
        "missing": missing,
        "mappings": [{
            "sector_code": row.sector_code,
            "etf_code": row.etf_code,
            "etf_name": row.etf_name,
            "is_primary": row.is_primary,
            "source": row.source,
            "valid_from": row.valid_from.isoformat(),
        } for row in mapping_rows],
        "premium_source": PREMIUM_SOURCE,
        "aum_source": AUM_SOURCE,
        "history_source": HISTORY_SOURCE,
    }
