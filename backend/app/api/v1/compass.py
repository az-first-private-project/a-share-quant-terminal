"""仓位罗盘 API（V9）。

权限：
- 内部 beta 默认 professional；客户端可用 `X-Compass-Tier: free` 查看免费视图。
- 生产环境必须替换为服务端订阅权益校验，禁止信任请求头。
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminDep
from app.compass import service
from app.compass.push import send_card_push
from app.compass.constants import ALL_SCENARIO_IDS
from app.core.errors import InvalidParamError, NotFoundError
from app.db.models import DimEtfSectorMap, FactDecisionCard, FactSignalFailure
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/compass", tags=["仓位罗盘"])


async def get_tier(
    x_compass_tier: Annotated[str | None, Header(alias="X-Compass-Tier")] = None,
) -> str:
    return "free" if (x_compass_tier or "").strip().lower() == "free" else "pro"


TierDep = Annotated[str, Depends(get_tier)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


class CheckActionBody(BaseModel):
    action: str = Field(..., description="DELAY / DROP / EXECUTED")
    reason: Optional[str] = None


class FailureBody(BaseModel):
    failure_date: date
    failure_type: str
    impact_pct: Optional[float] = None
    root_cause: str
    fix_version: Optional[str] = None
    status: str = "open"


class EtfMapBody(BaseModel):
    sector_code: str
    etf_code: str
    etf_name: str
    is_primary: bool = True
    tracking_index: Optional[str] = None
    valid_from: date


@router.get("/today")
async def today(
    db: DbDep,
    tier: TierDep,
    trade_date: Optional[date] = Query(None, description="交易日，默认最新"),
) -> dict:
    return await service.get_card(db, trade_date=trade_date, tier=tier)


@router.get("/cards")
async def cards(
    db: DbDep,
    tier: TierDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    if tier != "pro":
        limit = min(limit, 7)
    return await service.list_cards(db, limit=limit, offset=offset, tier=tier)


@router.get("/cards/{card_id}")
async def card_detail(card_id: str, db: DbDep, tier: TierDep) -> dict:
    return await service.get_card_by_id(db, card_id, tier=tier)


@router.get("/scenarios")
async def scenarios(
    db: DbDep,
    tier: TierDep,
    trade_date: Optional[date] = Query(None),
) -> dict:
    return await service.get_scenarios(db, trade_date=trade_date, tier=tier)


@router.post("/checks/{check_id}/action")
async def check_action(
    check_id: str,
    body: CheckActionBody,
    db: DbDep,
    tier: TierDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    if tier != "pro":
        raise InvalidParamError("T+1 操作仅专业版可用", detail={"upgrade": True})
    return await service.apply_check_action(
        db, check_id, body.action, idempotency_key=idempotency_key,
    )


@router.get("/review")
async def review(db: DbDep, tier: TierDep, days: int = Query(252, ge=60, le=500)) -> dict:
    payload = await service.review_payload(db, days=days)
    if tier != "pro":
        payload["failures"] = (payload.get("failures") or [])[:3]
        payload["chart"] = {"dates": [], "drawdown_rule": [], "drawdown_unconstrained": []}
        payload["upgrade_required"] = True
    return payload


@router.get("/rules")
async def rules() -> dict:
    payload = service.rules_payload()
    payload["scenario_ids"] = list(ALL_SCENARIO_IDS)
    payload["tiers"] = {"free": ["风险仪表盘", "SC-01", "SC-03", "月度摘要"],
                        "pro": ["全部情景卡", "T+1 校验", "全量复盘", "历史卡片"]}
    return payload


@router.get("/subscription")
async def subscription(tier: TierDep) -> dict:
    return {
        "tier": tier,
        "beta": True,
        "hard_paywall": False,
        "hint": "内部 beta 默认专业版；付费与权益校验在 P1 接入",
    }


# ============================================================
# 管理接口
# ============================================================

@router.post("/admin/generate")
async def admin_generate(
    db: DbDep,
    _admin: AdminDep = None,
    trade_date: Optional[date] = Query(None),
) -> dict:
    target = trade_date or await service.latest_trade_date(db)
    if target is None:
        raise InvalidParamError("找不到可生成的交易日")
    card = await service.generate_card(db, target)
    return {
        "status": "generated",
        "card_id": card.card_id,
        "trade_date": card.trade_date.isoformat(),
        "exec_cap": card.exec_cap,
    }


@router.post("/admin/etf-sync")
async def admin_etf_sync(
    db: DbDep,
    _admin: AdminDep = None,
    trade_date: Optional[date] = Query(None),
    mode: str = Query("auto", pattern="^(auto|seed|live)$"),
    limit: Optional[int] = Query(None, ge=1, le=200),
) -> dict:
    """同步 ETF 日行情并重建板块映射。"""
    from app.compass.etf_sync import sync_etf_data

    return await sync_etf_data(db, trade_date, mode=mode, limit=limit)


@router.get("/admin/etf-coverage")
async def admin_etf_coverage(
    db: DbDep,
    _admin: AdminDep = None,
    trade_date: Optional[date] = Query(None),
) -> dict:
    """ETF 映射覆盖率、缺失板块与数据源标注。"""
    from app.compass.etf_sync import coverage_report

    return await coverage_report(db, trade_date)


@router.post("/admin/backfill-paper")
async def admin_backfill_paper(
    db: DbDep,
    _admin: AdminDep = None,
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
) -> dict:
    """把 active 纸面组合净值聚合回填到罗盘复盘表。"""
    from app.compass.paper_backfill import backfill_paper_portfolio

    return await backfill_paper_portfolio(db, start_date=start, end_date=end)


@router.post("/admin/check/{card_id}")
async def admin_check(
    card_id: str,
    db: DbDep,
    _admin: AdminDep = None,
    check_date: Optional[date] = Query(None),
) -> dict:
    items = await service.run_open_checks(db, card_id, check_date)
    return {"card_id": card_id, "items": items}


@router.post("/admin/push/{card_id}")
async def admin_push(
    card_id: str,
    db: DbDep,
    _admin: AdminDep = None,
) -> dict:
    return await send_card_push(db, card_id)


@router.post("/admin/failure")
async def admin_failure(
    body: FailureBody,
    db: DbDep,
    _admin: AdminDep = None,
) -> dict:
    row = FactSignalFailure(
        failure_date=body.failure_date,
        failure_type=body.failure_type,
        impact_pct=body.impact_pct,
        root_cause=body.root_cause,
        fix_version=body.fix_version,
        status=body.status,
    )
    db.add(row)
    await db.flush()
    return {"id": row.id, "status": "created"}


@router.post("/admin/etf-map")
async def admin_etf_map(
    body: EtfMapBody,
    db: DbDep,
    _admin: AdminDep = None,
) -> dict:
    row = DimEtfSectorMap(
        sector_code=body.sector_code,
        etf_code=body.etf_code,
        etf_name=body.etf_name,
        is_primary=body.is_primary,
        tracking_index=body.tracking_index,
        valid_from=body.valid_from,
        source="admin",
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise InvalidParamError(
            "ETF 映射冲突：同一板块同一时点只能有一个主映射",
            detail={"sector_code": body.sector_code, "etf_code": body.etf_code},
        ) from exc
    return {"id": row.id, "status": "created"}


@router.get("/admin/cards/{card_id}")
async def admin_card(card_id: str, db: DbDep, _admin: AdminDep = None) -> dict:
    card = (await db.execute(
        select(FactDecisionCard).where(FactDecisionCard.card_id == card_id)
    )).scalar_one_or_none()
    if card is None:
        raise NotFoundError(f"决策卡不存在: {card_id}")
    return await service.card_payload(db, card, tier="pro")
