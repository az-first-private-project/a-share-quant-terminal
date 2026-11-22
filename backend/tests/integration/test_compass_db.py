"""仓位罗盘 DB 集成测试：真实 Postgres 上生成卡片并走完 T+1 校验。

使用 db_session 外层事务，测试数据不会落库；
卡片日期放在 2026 年已有分区，T+1 校验日期放在 2099 年避免过期。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.compass.paper_backfill import backfill_paper_portfolio
from app.compass.service import apply_check_action, generate_card, run_open_checks
from app.db.models import (
    DimEtfSectorMap, DimSectorMember, FactEtfDaily, FactFeatureDaily,
    FactPaperPortfolio, FactScenarioTrigger, FactSectorDaily, FactStockDaily,
    MarketRegimeDaily, MarketTemperatureDaily, PaperNav, PaperPortfolio, SectorScore,
)

pytestmark = pytest.mark.asyncio

PREV = date(2026, 11, 5)
TRADE_DATE = date(2026, 11, 6)
CHECK_DATE = date(2099, 1, 6)
SECTOR = "TST_COMPASS"
ETF = "TSTETF"


async def _seed(db) -> None:
    db.add(MarketRegimeDaily(
        trade_date=TRADE_DATE,
        regime_code="repair_up",
        regime_label="震荡",
        regime_since=PREV,
        regime_days=2,
    ))
    db.add(MarketTemperatureDaily(trade_date=TRADE_DATE, temp_score=56))

    for day in (PREV, TRADE_DATE):
        db.add(SectorScore(
            trade_date=day,
            sector_code=SECTOR,
            sector_name="测试板块",
            technical_score=8.0,
            sentiment_score=7.0,
            news_score=7.0,
            composite_score=7.4,
        ))
        db.add(FactSectorDaily(
            sector_code=SECTOR,
            trade_date=day,
            sector_name="测试板块",
            sector_type="industry",
            close=100.0,
            pct_chg=1.0,
            up_count=12,
            down_count=8,
            member_count=20,
            main_net_inflow=1.0e8,
            src="test",
        ))

    db.add(DimEtfSectorMap(
        sector_code=SECTOR,
        etf_code=ETF,
        etf_name="测试ETF",
        is_primary=True,
        valid_from=PREV,
        source="test",
    ))
    for day in (PREV, TRADE_DATE):
        db.add(FactEtfDaily(
            trade_date=day,
            etf_code=ETF,
            close=100.0,
            pre_close=99.0,
            amount=1.0e8,
            aum=3.0e8,
            premium_rate=0.1,
            is_suspended=False,
        ))
    db.add(FactEtfDaily(
        trade_date=CHECK_DATE,
        etf_code=ETF,
        open=102.5,
        close=102.5,
        pre_close=100.0,
        amount=1.0e8,
        aum=3.0e8,
        premium_rate=0.1,
        is_suspended=False,
    ))

    # 成分股广度：5 只有效成分，4 只收盘低于 MA20 → 80%
    for index, code in enumerate(("600001", "600002", "600003", "600004", "600005")):
        db.add(DimSectorMember(
            sector_code=SECTOR,
            stock_code=code,
            effective_from=date(2026, 1, 1),
            sector_name="测试板块",
            sector_type="industry",
            is_partial=False,
        ))
        db.add(FactStockDaily(
            code=code,
            trade_date=TRADE_DATE,
            close=10.0 if index < 4 else 20.0,
            is_suspended=False,
            src="test",
        ))
        db.add(FactFeatureDaily(
            code=code,
            trade_date=TRADE_DATE,
            ma20=12.0,
        ))
    await db.flush()


async def test_generate_card_and_delay_check(db_session) -> None:
    await _seed(db_session)

    card = await generate_card(db_session, TRADE_DATE)

    assert card.exec_cap == 40
    assert card.market_cap == 40
    assert card.per_sector_cap == 15
    assert len(card.candidates) == 1
    candidate = card.candidates[0]
    assert candidate["sector_code"] == SECTOR
    assert candidate["etf_code"] == ETF
    assert candidate["reasons"]
    assert candidate["checks"]
    assert candidate["breadth_below_ma20"] == 0.8
    assert candidate["breadth_valid_members"] == 5
    assert any("跌破 MA20 占比 80%" in row for row in candidate["checks"])
    assert card.rejected == []

    trigger = (await db_session.execute(
        select(FactScenarioTrigger).where(
            FactScenarioTrigger.card_id == card.card_id,
            FactScenarioTrigger.scenario_id == "SC-05",
        )
    )).scalar_one()
    assert trigger.status == "TRIGGERED"

    checks = await run_open_checks(db_session, card.card_id, check_date=CHECK_DATE)
    assert len(checks) == 1
    assert checks[0]["result"] == "WARN"
    assert checks[0]["open_gap"] == 2.5

    action = await apply_check_action(
        db_session, checks[0]["check_id"], "DELAY", idempotency_key="test-delay",
    )
    assert action["final_status"] == "DELAYED"
    assert action["idempotent_replay"] is False

    replay = await apply_check_action(
        db_session, checks[0]["check_id"], "DELAY", idempotency_key="test-delay",
    )
    assert replay["idempotent_replay"] is True


async def test_backfill_paper_portfolio_from_paper_nav(db_session) -> None:
    start = date(2099, 3, 1)
    days = 60
    for suffix in ("a", "b"):
        pid = f"paper-{suffix}"
        db_session.add(PaperPortfolio(
            portfolio_id=pid,
            algo_code=f"test{suffix}",
            name=f"测试组合{suffix}",
            status="active",
            start_date=start,
        ))
    await db_session.flush()

    for suffix in ("a", "b"):
        pid = f"paper-{suffix}"
        db_session.add_all([
            PaperNav(
                portfolio_id=pid,
                trade_date=start + timedelta(days=index),
                nav=1.0 + index * 0.01,
                benchmark=1.0 + index * 0.005,
            )
            for index in range(days)
        ])
    await db_session.flush()

    result = await backfill_paper_portfolio(db_session)

    assert result["status"] == "ok"
    assert result["rows"] == days
    assert result["portfolios"] == 2

    count = (await db_session.execute(
        select(func.count()).select_from(FactPaperPortfolio)
        .where(FactPaperPortfolio.trade_date >= start)
    )).scalar()
    assert count == days

    last = (await db_session.execute(
        select(FactPaperPortfolio)
        .where(FactPaperPortfolio.trade_date == start + timedelta(days=days - 1))
    )).scalar_one()
    assert float(last.nav_rule) == pytest.approx(159.0)
    assert float(last.nav_unconstrained) == pytest.approx(129.5)
    assert float(last.drawdown_rule) == pytest.approx(0.0)
