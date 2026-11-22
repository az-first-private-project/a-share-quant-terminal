"""仓位罗盘服务层：读现有事实表，生成决策卡与 T+1 校验。

约定：
- 只读本地库；请求路径不访问外部数据源。
- 关键数据缺失抛 DataUnavailableError，不生成“看起来正常”的空卡。
- 卡片生成后核心字段不可更新，新规则版本写新行。
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.compass.breadth import load_breadth_below_ma20
from app.compass.checks import apply_action, evaluate_open_gap
from app.compass.constants import (
    ALL_SCENARIO_IDS,
    FREE_SCENARIO_IDS,
    RULE_VERSION,
    SENTIMENT_MIN_SAMPLES,
    SENTIMENT_WINDOW,
    TIER_LABEL,
)
from app.compass.pipeline import build_decision, rank_pool
from app.compass.review import build_review, redact_card
from app.compass.scenarios import (
    SCENARIO_DEFS, evaluate_scenarios, resolve_action_priority,
)
from app.core.errors import (
    AppError, DataUnavailableError, ErrorCode, NotFoundError, QualityBlockedError,
)
from app.db.models import (
    DimEtfSectorMap, DimSectorMember, FactDecisionCard, FactDecisionCheck, FactEtfDaily,
    FactPaperPortfolio, FactScenarioTrigger, FactSectorDaily, FactSectorSentimentPercentile,
    FactSignalFailure, MarketIndex, MarketRegimeDaily, MarketTemperatureDaily, SectorScore,
    TradeCalendar,
)
from app.services.freshness import get_freshness_items

TZ = ZoneInfo("Asia/Shanghai")
COMPASS_DATASETS = [
    "sector_daily", "sector_members", "moneyflow", "margin",
    "market_temperature", "market_regime", "etf_daily", "etf_sector_map",
]


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _band(temp: float | None) -> str:
    if temp is None:
        return "样本不足"
    if temp < 20:
        return "冰点"
    if temp < 40:
        return "清冷"
    if temp < 60:
        return "平稳"
    if temp < 80:
        return "温热"
    return "沸腾"


def _crowding(pct: int | None) -> str:
    if pct is None:
        return "样本不足"
    if pct >= 90:
        return "过热"
    if pct >= 80:
        return "偏热"
    if pct >= 30:
        return "正常"
    return "偏冷"


async def latest_trade_date(db: AsyncSession) -> date | None:
    for column in (FactSectorDaily.trade_date, SectorScore.trade_date,
                   MarketRegimeDaily.trade_date):
        value = (await db.execute(select(column).order_by(column.desc()).limit(1))).scalar()
        if value:
            return value
    return None


async def previous_trade_date(db: AsyncSession, trade_date: date) -> date | None:
    return (await db.execute(
        select(FactSectorDaily.trade_date)
        .where(FactSectorDaily.trade_date < trade_date)
        .order_by(FactSectorDaily.trade_date.desc())
        .limit(1)
    )).scalar()


async def next_trading_day(db: AsyncSession, after: date) -> date | None:
    return (await db.execute(
        select(TradeCalendar.cal_date)
        .where(and_(TradeCalendar.cal_date > after, TradeCalendar.is_open.is_(True)))
        .order_by(TradeCalendar.cal_date.asc())
        .limit(1)
    )).scalar()


def _percentile(values: list[float], current: float) -> tuple[int | None, int]:
    window = [v for v in values[-SENTIMENT_WINDOW:] if v is not None]
    if len(window) < SENTIMENT_MIN_SAMPLES:
        return None, len(window)
    hits = sum(1 for v in window if v <= current)
    return int(round(hits / len(window) * 100)), len(window)


def _pearson(a: list[float], b: list[float]) -> float | None:
    size = min(len(a), len(b))
    if size < 40:
        return None
    xs = a[-size:]
    ys = b[-size:]
    mean_x = sum(xs) / size
    mean_y = sum(ys) / size
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _aligned_pearson(
    history: dict[str, list[dict]], left: str, right: str, *, window: int = 60
) -> float | None:
    """按交易日对齐后再算相关系数，避免缺日导致错位。"""
    left_map = {
        item["trade_date"]: item["pct_chg"]
        for item in (history.get(left) or [])
        if item["pct_chg"] is not None
    }
    right_map = {
        item["trade_date"]: item["pct_chg"]
        for item in (history.get(right) or [])
        if item["pct_chg"] is not None
    }
    dates = sorted(set(left_map) & set(right_map))[-window:]
    if len(dates) < 40:
        return None
    return _pearson([left_map[d] for d in dates], [right_map[d] for d in dates])


async def _daily_history(db: AsyncSession, trade_date: date, days: int = 80) -> dict[str, list[dict]]:
    dates = (await db.execute(
        select(FactSectorDaily.trade_date)
        .where(FactSectorDaily.trade_date <= trade_date)
        .distinct()
        .order_by(FactSectorDaily.trade_date.desc())
        .limit(days)
    )).scalars().all()
    if not dates:
        return {}
    rows = (await db.execute(
        select(FactSectorDaily).where(FactSectorDaily.trade_date.in_(dates))
    )).scalars().all()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.sector_code, []).append({
            "trade_date": row.trade_date,
            "close": _num(row.close),
            "pct_chg": _num(row.pct_chg),
            "up_count": row.up_count,
            "down_count": row.down_count,
            "member_count": row.member_count,
            "main_net_inflow": _num(row.main_net_inflow),
            "sector_type": row.sector_type,
        })
    for values in grouped.values():
        values.sort(key=lambda item: item["trade_date"])
    return grouped


async def _sentiment_history(
    db: AsyncSession, trade_date: date, codes: list[str]
) -> dict[str, list[float]]:
    if not codes:
        return {}
    start = trade_date - timedelta(days=420)
    rows = (await db.execute(
        select(SectorScore.sector_code, SectorScore.sentiment_score)
        .where(and_(
            SectorScore.sector_code.in_(codes),
            SectorScore.trade_date <= trade_date,
            SectorScore.trade_date >= start,
        ))
        .order_by(SectorScore.sector_code, SectorScore.trade_date)
    )).all()
    grouped: dict[str, list[float]] = {}
    for code, value in rows:
        number = _num(value)
        if number is not None:
            grouped.setdefault(code, []).append(number)
    return grouped


async def _etf_snapshot(
    db: AsyncSession, trade_date: date, codes: list[str]
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    if not codes:
        return {}, {}
    mapping_rows = (await db.execute(
        select(DimEtfSectorMap).where(and_(
            DimEtfSectorMap.sector_code.in_(codes),
            DimEtfSectorMap.valid_from <= trade_date,
            or_(DimEtfSectorMap.valid_to.is_(None),
                DimEtfSectorMap.valid_to > trade_date),
        ))
    )).scalars().all()

    etf_codes = [row.etf_code for row in mapping_rows]
    daily_map: dict[str, dict] = {}
    adv_map: dict[str, float] = {}
    if etf_codes:
        dates = (await db.execute(
            select(FactEtfDaily.trade_date)
            .where(FactEtfDaily.trade_date <= trade_date)
            .distinct()
            .order_by(FactEtfDaily.trade_date.desc())
            .limit(21)
        )).scalars().all()
        etf_rows = (await db.execute(
            select(FactEtfDaily).where(and_(
                FactEtfDaily.etf_code.in_(etf_codes),
                FactEtfDaily.trade_date.in_(dates),
            ))
        )).scalars().all()
        amount_group: dict[str, list[float]] = {}
        for row in etf_rows:
            amount = _num(row.amount)
            if amount is not None:
                amount_group.setdefault(row.etf_code, []).append(amount)
            if row.trade_date == trade_date:
                daily_map[row.etf_code] = {
                    "etf_code": row.etf_code,
                    "close": _num(row.close),
                    "open": _num(row.open),
                    "premium_rate": _num(row.premium_rate) or 0.0,
                    "is_suspended": bool(row.is_suspended),
                }
        for code, amounts in amount_group.items():
            adv_map[code] = sum(amounts) / len(amounts)

    mapping: dict[str, list[dict]] = {}
    for row in mapping_rows:
        daily = daily_map.get(row.etf_code, {})
        mapping.setdefault(row.sector_code, []).append({
            "etf_code": row.etf_code,
            "etf_name": row.etf_name,
            "is_primary": bool(row.is_primary),
            "listed": True,
            "suspended": daily.get("is_suspended", False),
            "aum": _num(
                (await db.execute(
                    select(FactEtfDaily.aum).where(and_(
                        FactEtfDaily.etf_code == row.etf_code,
                        FactEtfDaily.trade_date == trade_date,
                    ))
                )).scalar()
            ) or 0.0,
            "adv20": adv_map.get(row.etf_code, 0.0),
            "premium_rate": daily.get("premium_rate", 0.0),
        })
    return mapping, daily_map


def _derivation(decision: dict, regime_label: str, temp: float | None) -> list[dict]:
    output_count = len(decision["output"])
    return [
        {"step": 1, "key": "市场温度", "value": f"{temp:.0f} / 100" if temp is not None else "—",
         "note": f"{_band(temp)}区间，作为背景指标"},
        {"step": 2, "key": "市场状态", "value": regime_label,
         "note": f"读取状态机输出 {decision['regime_state']}，叠加 3 日迟滞"},
        {"step": 3, "key": "初筛池", "value": f"{decision['initial_pool_size']} 个",
         "note": f"通过五道门槛 {decision['qualified_count']} 个"},
        {"step": 4, "key": "单板块上限",
         "value": f"min(20%, {decision['market_cap']}% × 40%) = {decision['per_sector_cap']}%",
         "note": "按 5% 步长向下取整"},
        {"step": 5, "key": "执行结论",
         "value": f"预算 {decision['exec_cap']}%，候选 {output_count} 个" if output_count else "空仓",
         "note": "空仓是主动输出，不是数据故障" if not output_count else "开盘后由用户自行分配",
         },
    ]


async def generate_card(
    db: AsyncSession,
    trade_date: date,
    *,
    rule_version: str = RULE_VERSION,
) -> FactDecisionCard:
    existing = (await db.execute(
        select(FactDecisionCard).where(and_(
            FactDecisionCard.trade_date == trade_date,
            FactDecisionCard.rule_version == rule_version,
        ))
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    regime = (await db.execute(
        select(MarketRegimeDaily).where(MarketRegimeDaily.trade_date == trade_date)
    )).scalar_one_or_none()
    if regime is None:
        raise DataUnavailableError(
            f"{trade_date} 的市场状态尚未生成",
            next_action="先运行市场状态任务（market_regime）",
        )
    temperature = (await db.execute(
        select(MarketTemperatureDaily).where(MarketTemperatureDaily.trade_date == trade_date)
    )).scalar_one_or_none()
    temp = _num(temperature.temp_score) if temperature is not None else None
    if temp is None:
        raise DataUnavailableError(
            f"{trade_date} 的市场温度缺失",
            next_action="先运行市场温度任务（temperature）",
        )

    scores = (await db.execute(
        select(SectorScore).where(SectorScore.trade_date == trade_date)
    )).scalars().all()
    if not scores:
        raise DataUnavailableError(
            f"{trade_date} 的板块评分尚未生成",
            next_action="先运行板块评分任务（sector_scores）",
        )

    prev_date = await previous_trade_date(db, trade_date)
    prev_codes: set[str] = set()
    if prev_date:
        prev_scores = (await db.execute(
            select(SectorScore).where(SectorScore.trade_date == prev_date)
        )).scalars().all()
        prev_rows = [
            {"sector_code": row.sector_code, "composite": _num(row.composite_score) or 0.0,
             "technical": _num(row.technical_score) or 0.0,
             "sentiment": _num(row.sentiment_score) or 0.0}
            for row in prev_scores
        ]
        prev_codes = {row["sector_code"] for row in rank_pool(prev_rows)}

    history = await _daily_history(db, trade_date)
    codes = [row.sector_code for row in scores]
    sentiment = await _sentiment_history(db, trade_date, codes)

    partial_codes = set((await db.execute(
        select(DimSectorMember.sector_code).where(and_(
            DimSectorMember.sector_code.in_(codes),
            DimSectorMember.effective_from <= trade_date,
            or_(DimSectorMember.effective_to.is_(None),
                DimSectorMember.effective_to > trade_date),
            DimSectorMember.is_partial.is_(True),
        )).distinct()
    )).scalars().all())

    candidates: list[dict] = []
    for row in scores:
        daily = history.get(row.sector_code) or []
        current = next((item for item in reversed(daily) if item["trade_date"] == trade_date), None)
        # MVP 仅纳入行业板块：概念板块重叠度高、ETF 映射覆盖率低
        if ((current or {}).get("sector_type") or "industry") != "industry":
            continue
        closes = [item["close"] for item in daily if item["close"] is not None]
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        pct_values = sentiment.get(row.sector_code) or []
        technical = _num(row.technical_score)
        sentiment_score = _num(row.sentiment_score)
        news = _num(row.news_score)
        composite = _num(row.composite_score)
        percentile, samples = (None, 0)
        if sentiment_score is not None and pct_values:
            percentile, samples = _percentile(pct_values, sentiment_score)
        candidates.append({
            "sector_code": row.sector_code,
            "sector_name": row.sector_name or row.sector_code,
            "sector_type": (current or {}).get("sector_type", "industry"),
            "composite": composite,
            "technical": technical,
            "sentiment": sentiment_score,
            "news": news,
            "sentiment_percentile": percentile,
            "sentiment_samples": samples,
            "crowding_level": _crowding(percentile),
            "member_count": (current or {}).get("member_count") or 0,
            "is_partial": row.sector_code in partial_codes,
            "data_complete": technical is not None and sentiment_score is not None,
            "up_count": (current or {}).get("up_count"),
            "down_count": (current or {}).get("down_count"),
            "main_net_inflow": (current or {}).get("main_net_inflow"),
            "close": (current or {}).get("close"),
            "ma20": ma20,
            "breadth_below_ma20": None,
        })

    pool = rank_pool(candidates)
    pool_codes = [c["sector_code"] for c in pool]

    breadth = await load_breadth_below_ma20(db, trade_date, pool_codes)
    for cand in candidates:
        stats = breadth.get(cand["sector_code"])
        if stats:
            cand["breadth_below_ma20"] = stats["ratio"]
            cand["breadth_below_count"] = stats["below"]
            cand["breadth_valid_members"] = stats["valid"]

    correlations: dict[tuple[str, str], float] = {}
    for index, left in enumerate(pool_codes):
        for right in pool_codes[index + 1:]:
            value = _aligned_pearson(history, left, right)
            if value is not None:
                correlations[(left, right)] = value

    # 情绪分位落库，供复盘与历史查询复用
    if candidates:
        now = datetime.now(TZ)
        values = [{
            "trade_date": trade_date,
            "sector_code": c["sector_code"],
            "window_days": SENTIMENT_WINDOW,
            "valid_samples": c["sentiment_samples"],
            "percentile": c["sentiment_percentile"],
            "crowding_level": c["crowding_level"],
            "computed_at": now,
        } for c in candidates]
        stmt = pg_insert(FactSectorSentimentPercentile).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "sector_code", "window_days"],
            set_={
                "valid_samples": stmt.excluded.valid_samples,
                "percentile": stmt.excluded.percentile,
                "crowding_level": stmt.excluded.crowding_level,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        await db.execute(stmt)

    etf_mapping, _ = await _etf_snapshot(db, trade_date, pool_codes)
    try:
        decision = build_decision(
            regime_state=regime.regime_code,
            temp_score=temp,
            candidates=candidates,
            etf_map=etf_mapping,
            correlations=correlations,
            prev_rank_pool=prev_codes,
            rule_version=rule_version,
        )
    except ValueError as exc:
        raise QualityBlockedError(
            f"市场状态无法映射到预算档位: {regime.regime_code}",
            detail={"regime_code": regime.regime_code},
        ) from exc

    output_by_code = {c["sector_code"]: c for c in decision["output"]}
    for cand in candidates:
        if cand["sector_code"] not in output_by_code:
            continue
        etf_info = next(
            (e for e in etf_mapping.get(cand["sector_code"], [])
             if e.get("is_primary", True)), None
        )
        cand["etf_code"] = etf_info.get("etf_code") if etf_info else None
        cand["etf_name"] = etf_info.get("etf_name") if etf_info else None
        cand["etf_aum"] = etf_info.get("aum") if etf_info else None
        cand["etf_adv20"] = etf_info.get("adv20") if etf_info else None

        up = int(_num(cand.get("up_count")) or 0)
        down = int(_num(cand.get("down_count")) or 0)
        ratio = (up / (up + down)) if (up + down) else 0.0
        inflow = _num(cand.get("main_net_inflow")) or 0.0
        pct = cand.get("sentiment_percentile")
        cand["reasons"] = [
            f"技术面：技术分 {(_num(cand.get('technical')) or 0):.1f} / 10，趋势与量能同步",
            f"情绪面：情绪分 {(_num(cand.get('sentiment')) or 0):.1f} / 10，情绪分位 "
            + (f"{pct}" if pct is not None else "样本不足"),
            f"广度：上涨占比 {ratio:.0%}，主力净流入 {inflow / 1e8:.1f} 亿",
        ]
        etf_txt = (
            f"{cand.get('etf_code')} · 规模 {(cand.get('etf_aum') or 0) / 1e8:.0f} 亿 / "
            f"日成交 {(cand.get('etf_adv20') or 0) / 1e8:.1f} 亿"
            if cand.get("etf_code") else "—"
        )
        cand["checks"] = [
            f"绝对门槛：综合 {(_num(cand.get('composite')) or 0):.1f} ≥ 6.5",
            "连续性：2/2 个交易日",
            f"广度一致性：上涨占比 {ratio:.0%} ≥ 55%",
            "相关性去重：通过",
            f"ETF 可交易：{etf_txt}",
            f"成分股广度：跌破 MA20 占比 {((cand.get('breadth_below_ma20') or 0) * 100):.0f}%"
            if cand.get("breadth_below_ma20") is not None else "成分股广度：样本不足",
        ]

    card_id = uuid.uuid4().hex
    card = FactDecisionCard(
        card_id=card_id,
        trade_date=trade_date,
        rule_version=rule_version,
        regime_state=regime.regime_code,
        regime_tier=decision["regime_tier"],
        temp_score=temp,
        market_cap=decision["market_cap"],
        exec_cap=decision["exec_cap"],
        per_sector_cap=decision["per_sector_cap"],
        candidates=[
            {**c, "rank": index + 1, "tag": "关注"}
            for index, c in enumerate(decision["output"])
        ],
        rejected=decision["rejected"],
        empty_reasons=decision["empty_reasons"],
        derivation=_derivation(decision, regime.regime_label, temp),
        status="generated",
    )
    db.add(card)
    await db.flush()

    scenario_rows = evaluate_scenarios({
        "regime_state": regime.regime_code,
        "regime_tier": decision["regime_tier"],
        "output_count": len(decision["output"]),
        "candidates": candidates,
        "checks": [],
        "rolling_excess_60d": None,
        "breadth_below_ma20": None,
    })
    for row in scenario_rows:
        db.add(FactScenarioTrigger(
            trigger_id=uuid.uuid4().hex,
            card_id=card_id,
            scenario_id=row["id"],
            status=row["status"],
            evidence=row.get("evidence") or {},
            triggered_at=datetime.now(TZ) if row["status"] == "TRIGGERED" else None,
        ))
    await db.flush()
    return card


async def _card_scenarios(db: AsyncSession, card_id: str) -> list[dict]:
    rows = (await db.execute(
        select(FactScenarioTrigger).where(FactScenarioTrigger.card_id == card_id)
    )).scalars().all()
    by_id = {row.scenario_id: row for row in rows}
    result = []
    for sid in ALL_SCENARIO_IDS:
        row = by_id.get(sid)
        if row is None:
            continue
        result.append({
            "id": sid,
            **SCENARIO_DEFS[sid],
            "status": row.status,
            "evidence": row.evidence or {},
        })
    return result


async def _card_checks(db: AsyncSession, card_id: str) -> list[dict]:
    rows = (await db.execute(
        select(FactDecisionCheck).where(FactDecisionCheck.card_id == card_id)
        .order_by(FactDecisionCheck.etf_code)
    )).scalars().all()
    return [{
        "check_id": row.check_id,
        "etf_code": row.etf_code,
        "open_gap": _num(row.open_gap),
        "recheck_gap": _num(row.recheck_gap),
        "result": row.result,
        "user_action": row.user_action,
        "final_status": row.final_status,
        "user_override": row.user_override,
    } for row in rows]


def _check_summary(checks: list[dict], exec_cap: int) -> dict:
    if not checks:
        if exec_cap == 0:
            return {"status": "IDLE", "title": "空仓日 · 无买入计划",
                    "text": "不进行开盘校验。候选池为空，今日不新开仓、不补仓。"}
        return {"status": "PENDING", "title": "等待 T+1 09:35 校验",
                "text": "开盘后自动比对候选 ETF 的开盘偏离。"}
    if any(c["result"] == "SUSPENDED" for c in checks):
        return {"status": "SUSPENDED", "title": "T+1 校验 · ETF 停牌",
                "text": "存在停牌标的，禁止执行该标的。", "items": checks}
    if any(c.get("final_status") == "DELAYED" for c in checks):
        return {"status": "DELAYED", "title": "已标记：延迟执行",
                "text": "10:00 复核前不新开仓；复核结果会自动写入复盘。", "items": checks}
    if any(c.get("final_status") in {"DROPPED", "EXPIRED", "DROPPED_BY_RULE"} for c in checks):
        return {"status": "DROPPED", "title": "本次执行已放弃",
                "text": "已记录到复盘；下一次机会需等待新卡片。", "items": checks}
    risky = [c for c in checks if c["result"] == "WARN"]
    if risky:
        return {"status": "WARN", "title": f"T+1 校验 · {len(risky)} 个标的需要延迟",
                "text": "开盘偏离超过 2% 阈值，建议延迟至 10:00 复核。", "items": checks}
    return {"status": "PASS", "title": "T+1 校验通过",
            "text": "候选 ETF 开盘偏离均在 2% 以内。", "items": checks}


async def _idle_days(db: AsyncSession, trade_date: date) -> int:
    rows = (await db.execute(
        select(FactDecisionCard.exec_cap)
        .where(FactDecisionCard.trade_date <= trade_date)
        .order_by(FactDecisionCard.trade_date.desc())
        .limit(30)
    )).scalars().all()
    count = 0
    for value in rows:
        if value == 0:
            count += 1
        else:
            break
    return count


async def card_payload(
    db: AsyncSession, card: FactDecisionCard, *, tier: str = "pro"
) -> dict:
    checks = await _card_checks(db, card.card_id)
    scenarios = await _card_scenarios(db, card.card_id)
    freshness = await get_freshness_items(db, COMPASS_DATASETS)
    payload = {
        "card_id": card.card_id,
        "trade_date": card.trade_date.isoformat(),
        "rule_version": card.rule_version,
        "signal_version": card.signal_version,
        "regime": {
            "state": card.regime_state,
            "tier": card.regime_tier,
            "label": TIER_LABEL.get(card.regime_tier, card.regime_tier),
        },
        "temperature": {"score": _num(card.temp_score), "band": _band(_num(card.temp_score))},
        "budget": {
            "market_cap": card.market_cap,
            "exec_cap": card.exec_cap,
            "per_sector_cap": card.per_sector_cap,
        },
        "candidates": card.candidates or [],
        "rejected": card.rejected or [],
        "empty_reasons": card.empty_reasons or [],
        "derivation": card.derivation or [],
        "check": _check_summary(checks, card.exec_cap),
        "scenarios": scenarios,
        "idle_days": await _idle_days(db, card.trade_date),
        "freshness": freshness,
    }
    return redact_card(payload, tier)


async def get_card(
    db: AsyncSession, *, trade_date: date | None = None, tier: str = "pro"
) -> dict:
    target = trade_date or await latest_trade_date(db)
    if target is None:
        raise DataUnavailableError("数据库中还没有板块数据", next_action="先运行日行情采集")
    card = (await db.execute(
        select(FactDecisionCard)
        .where(FactDecisionCard.trade_date <= target)
        .order_by(FactDecisionCard.trade_date.desc(),
                 FactDecisionCard.generated_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if card is None:
        raise DataUnavailableError(
            f"{target} 尚未生成决策卡",
            next_action="POST /api/v1/admin/compass/generate",
        )
    return await card_payload(db, card, tier=tier)


async def get_card_by_id(
    db: AsyncSession, card_id: str, *, tier: str = "pro"
) -> dict:
    card = (await db.execute(
        select(FactDecisionCard).where(FactDecisionCard.card_id == card_id)
    )).scalar_one_or_none()
    if card is None:
        raise NotFoundError(f"决策卡不存在: {card_id}")
    return await card_payload(db, card, tier=tier)


async def list_cards(
    db: AsyncSession, *, limit: int = 20, offset: int = 0, tier: str = "pro"
) -> dict:
    total = (await db.execute(select(func.count()).select_from(FactDecisionCard))).scalar() or 0
    rows = (await db.execute(
        select(FactDecisionCard)
        .order_by(FactDecisionCard.trade_date.desc(),
                 FactDecisionCard.generated_at.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return {
        "items": [{
            "card_id": row.card_id,
            "trade_date": row.trade_date.isoformat(),
            "regime_tier": row.regime_tier,
            "exec_cap": row.exec_cap,
            "status": row.status,
            "rule_version": row.rule_version,
        } for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


async def get_scenarios(
    db: AsyncSession, *, trade_date: date | None = None, tier: str = "pro"
) -> dict:
    target = trade_date or await latest_trade_date(db)
    card = (await db.execute(
        select(FactDecisionCard)
        .where(FactDecisionCard.trade_date <= (target or date.today()))
        .order_by(FactDecisionCard.trade_date.desc())
        .limit(1)
    )).scalar_one_or_none()
    if card is None:
        raise DataUnavailableError("尚未生成决策卡", next_action="先生成当日卡片")
    scenarios = await _card_scenarios(db, card.card_id)
    action = resolve_action_priority(scenarios)
    locked: list[str] = []
    if tier != "pro":
        locked = [sid for sid in ALL_SCENARIO_IDS if sid not in FREE_SCENARIO_IDS]
        scenarios = [s for s in scenarios if s["id"] in FREE_SCENARIO_IDS]
    return {
        "trade_date": card.trade_date.isoformat(),
        "card_id": card.card_id,
        "scenarios": scenarios,
        "locked_scenario_ids": locked,
        "action_priority": action,
    }


async def run_open_checks(
    db: AsyncSession, card_id: str, check_date: date | None = None
) -> list[dict]:
    card = (await db.execute(
        select(FactDecisionCard).where(FactDecisionCard.card_id == card_id)
    )).scalar_one_or_none()
    if card is None:
        raise NotFoundError(f"决策卡不存在: {card_id}")

    target = check_date or await next_trading_day(db, card.trade_date)
    if target is None:
        raise DataUnavailableError("找不到下一交易日", next_action="检查交易日历")

    results: list[dict] = []
    for cand in card.candidates or []:
        etf_code = cand.get("etf_code")
        if not etf_code:
            continue
        prev_row = (await db.execute(
            select(FactEtfDaily).where(and_(
                FactEtfDaily.etf_code == etf_code,
                FactEtfDaily.trade_date == card.trade_date,
            ))
        )).scalar_one_or_none()
        open_row = (await db.execute(
            select(FactEtfDaily).where(and_(
                FactEtfDaily.etf_code == etf_code,
                FactEtfDaily.trade_date == target,
            ))
        )).scalar_one_or_none()

        status, gap = evaluate_open_gap(
            _num(prev_row.close) if prev_row else None,
            _num(open_row.open) if open_row else None,
            is_suspended=bool(open_row.is_suspended) if open_row else False,
        )
        existing = (await db.execute(
            select(FactDecisionCheck).where(and_(
                FactDecisionCheck.card_id == card_id,
                FactDecisionCheck.etf_code == etf_code,
            ))
        )).scalar_one_or_none()
        if existing is None:
            existing = FactDecisionCheck(
                check_id=uuid.uuid4().hex,
                card_id=card_id,
                trade_date=target,
                etf_code=etf_code,
            )
            db.add(existing)
        existing.prev_close = _num(prev_row.close) if prev_row else None
        existing.open_price = _num(open_row.open) if open_row else None
        existing.open_gap = gap
        existing.result = status
        results.append({
            "check_id": existing.check_id,
            "etf_code": etf_code,
            "result": status,
            "open_gap": gap,
        })
    await db.flush()
    return results


async def apply_check_action(
    db: AsyncSession,
    check_id: str,
    action: str,
    *,
    idempotency_key: str | None = None,
) -> dict:
    row = (await db.execute(
        select(FactDecisionCheck).where(FactDecisionCheck.check_id == check_id)
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"校验记录不存在: {check_id}")

    if idempotency_key and row.idempotency_key == idempotency_key:
        return {
            "check_id": row.check_id,
            "final_status": row.final_status or row.result,
            "action_at": row.action_at.isoformat() if row.action_at else None,
            "idempotent_replay": True,
        }

    deadline = datetime.combine(row.trade_date, time(10, 0), tzinfo=TZ)
    now = datetime.now(TZ)
    state = {
        "check_id": row.check_id,
        "etf_code": row.etf_code,
        "status": row.final_status or row.result,
        "user_action": row.user_action,
        "final_status": row.final_status,
        "open_gap": _num(row.open_gap),
        "user_override": row.user_override,
    }
    try:
        updated = apply_action(state, action, now=now, deadline=deadline)
    except ValueError as exc:
        raise AppError(
            str(exc),
            code=ErrorCode.BAD_REQUEST,
            status_code=400,
            detail={"reason": "CHECK_EXPIRED" if "expired" in str(exc) else "CHECK_NOT_ACTIONABLE"},
        ) from exc

    row.user_action = updated.get("user_action")
    row.action_at = now
    row.final_status = updated.get("final_status") or updated.get("status")
    row.user_override = bool(updated.get("user_override"))
    if idempotency_key:
        row.idempotency_key = idempotency_key
    await db.flush()
    return {
        "check_id": row.check_id,
        "final_status": row.final_status,
        "action_at": row.action_at.isoformat(),
        "recheck_at": datetime.combine(row.trade_date, time(10, 0), tzinfo=TZ).isoformat(),
        "idempotent_replay": False,
    }


async def review_payload(db: AsyncSession, *, days: int = 252) -> dict:
    rows = (await db.execute(
        select(FactPaperPortfolio)
        .order_by(FactPaperPortfolio.trade_date.desc())
        .limit(days)
    )).scalars().all()
    rows = list(reversed(rows))

    failures = (await db.execute(
        select(FactSignalFailure)
        .order_by(FactSignalFailure.failure_date.desc())
        .limit(50)
    )).scalars().all()
    failure_items = [{
        "date": row.failure_date.isoformat(),
        "type": row.failure_type,
        "impact_pct": _num(row.impact_pct),
        "root_cause": row.root_cause,
        "fix_version": row.fix_version,
        "status": row.status,
    } for row in failures]

    nav_rule = [_num(row.nav_rule) for row in rows if _num(row.nav_rule) is not None]
    nav_loose = [_num(row.nav_unconstrained) for row in rows
                 if _num(row.nav_unconstrained) is not None]
    if len(nav_rule) < 60 or len(nav_loose) < 60:
        return {
            "sample_insufficient": True,
            "source": "paper_portfolio",
            "violations_available": False,
            "label_rule": "纸面组合综合",
            "label_unconstrained": "组合基准",
            "method_note": "回填自 active 纸面组合净值，等权归一；违规次数待罗盘情景卡历史积累后启用",
            "stats": {
                "max_drawdown_rule": None,
                "max_drawdown_unconstrained": None,
                "violations_rule": rows[-1].violations_rule if rows else 0,
                "violations_unconstrained": rows[-1].violations_unconstrained if rows else 0,
                "empty_avoidance": None,
                "missed_upside": None,
                "net_avoidance": None,
                "empty_days": None,
            },
            "chart": {"dates": [], "drawdown_rule": [], "drawdown_unconstrained": []},
            "failures": failure_items,
        }

    benchmark_code = rows[-1].benchmark_code or "sh000905"
    benchmark_rows = (await db.execute(
        select(MarketIndex)
        .where(and_(
            MarketIndex.index_code == benchmark_code,
            MarketIndex.trade_date >= rows[0].trade_date,
            MarketIndex.trade_date <= rows[-1].trade_date,
        ))
        .order_by(MarketIndex.trade_date.asc())
    )).scalars().all()
    benchmark_returns = [_num(row.pct_chg) or 0.0 for row in benchmark_rows]

    empty_dates = set((await db.execute(
        select(FactDecisionCard.trade_date).where(and_(
            FactDecisionCard.trade_date >= rows[0].trade_date,
            FactDecisionCard.trade_date <= rows[-1].trade_date,
            FactDecisionCard.exec_cap == 0,
        ))
    )).scalars().all())
    empty_flags = [row.trade_date in empty_dates for row in rows]

    review = build_review(
        nav_rule=nav_rule,
        nav_unconstrained=nav_loose,
        benchmark_returns=benchmark_returns,
        empty_flags=empty_flags,
        violations_rule=rows[-1].violations_rule,
        violations_unconstrained=rows[-1].violations_unconstrained,
        dates=[row.trade_date.isoformat() for row in rows],
    )
    review["source"] = "paper_portfolio"
    review["violations_available"] = False
    review["label_rule"] = "纸面组合综合"
    review["label_unconstrained"] = "组合基准"
    review["method_note"] = "回填自 active 纸面组合净值，等权归一；违规次数待罗盘情景卡历史积累后启用"
    review["failures"] = failure_items
    return review


def rules_payload() -> dict:
    return {
        "rule_version": RULE_VERSION,
        "tier_labels": TIER_LABEL,
        "promises": [
            "公开样本外区间的全部结果，包括失效月份",
            "每条信号的失效记录与归因，不删改",
            "只统计回撤、错误次数、空仓避损，不以收益率做卖点",
            "规则变更保留版本号，历史决策卡不可覆写",
            "数据缺失时不出信号，不用中性分掩盖",
        ],
        "not_do": ["不推荐个股", "不给单一买卖结论", "不做盘中实时", "不承诺收益", "不代客操作"],
        "criterion": "如果这个产品必须靠“这个月赚了多少”来卖，那它就是伪需求。",
    }
