"""仓位罗盘推送（企业微信）。

消息只包含预算、方向、风险提示与免责声明，不含收益承诺。
幂等键：card_id + channel。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import PUSH_DRY_RUN, WECOM_WEBHOOK_URL
from app.db.models import CompassPushLog, FactDecisionCard

TZ = ZoneInfo("Asia/Shanghai")
DISCLAIMER = "本消息为风险预算工具输出，不构成投资建议。"


def build_message(payload: dict, tier: str = "pro") -> dict:
    """构造企业微信 markdown。免费版不含 ETF 代码，只给 1 个板块。"""
    date_text = payload.get("trade_date") or ""
    regime = (payload.get("regime") or {}).get("label") or "—"
    budget = payload.get("budget") or {}
    cap = budget.get("exec_cap", 0)
    candidates = payload.get("candidates") or []
    check = payload.get("check") or {}

    lines = [f"## 仓位罗盘 · {date_text}", ""]
    if cap:
        lines.append(f"**结论**：{regime} · 执行仓位上限 **{cap}%**")
    else:
        lines.append(f"**结论**：{regime} · **空仓**")
        reasons = payload.get("empty_reasons") or []
        if reasons:
            lines.append("**原因**：" + "；".join(r.get("text", "") for r in reasons[:3]))

    if cap and candidates:
        lines.append("")
        lines.append("**候选方向**")
        shown = candidates[: 1 if tier != "pro" else 3]
        for item in shown:
            name = item.get("sector_name") or item.get("sector_code")
            pct = item.get("sentiment_percentile")
            pct_text = f" · 情绪分位 {pct}" if pct is not None else ""
            etf = item.get("etf_code")
            etf_text = f" · {etf}" if etf and tier == "pro" else ""
            lines.append(f"- {name}{etf_text}{pct_text}")

    if check.get("status") in {"WARN", "DELAYED", "SUSPENDED"}:
        lines.append("")
        lines.append(f"**执行提示**：{check.get('title', '')}")
        if tier == "pro":
            lines.append(check.get("text", ""))
        else:
            lines.append("存在标的开盘偏离超过阈值，建议延迟至 10:00 复核。")

    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}


async def send_card_push(
    db: AsyncSession,
    card_id: str,
    *,
    channel: str = "wecom",
    tier: str = "pro",
) -> dict:
    card = (await db.execute(
        select(FactDecisionCard).where(FactDecisionCard.card_id == card_id)
    )).scalar_one_or_none()
    if card is None:
        return {"status": "not_found", "card_id": card_id}

    log = (await db.execute(
        select(CompassPushLog).where(
            CompassPushLog.card_id == card_id,
            CompassPushLog.channel == channel,
        )
    )).scalar_one_or_none()
    if log is not None and log.status == "sent":
        return {"status": "skipped", "reason": "already_sent", "card_id": card_id}
    if log is None:
        log = CompassPushLog(card_id=card_id, channel=channel)
        db.add(log)

    from app.compass.service import card_payload  # 延迟导入，避免循环

    payload = await card_payload(db, card, tier=tier)
    message = build_message(payload, tier=tier)

    if PUSH_DRY_RUN or not WECOM_WEBHOOK_URL:
        log.status = "dry_run" if PUSH_DRY_RUN else "skipped"
        log.attempts = 0
        log.error = None if PUSH_DRY_RUN else "WECOM_WEBHOOK_URL 未配置"
        await db.flush()
        return {"status": log.status, "card_id": card_id, "message": message}

    last_error: str | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(WECOM_WEBHOOK_URL, json=message)
                response.raise_for_status()
                body: Any = response.json()
                if isinstance(body, dict) and body.get("errcode", 0) != 0:
                    raise RuntimeError(f"wecom errcode={body.get('errcode')}")
            log.status = "sent"
            log.attempts = attempt
            log.error = None
            log.sent_at = datetime.now(TZ)
            card.status = "published"
            card.published_at = datetime.now(TZ)
            await db.flush()
            return {"status": "sent", "card_id": card_id, "attempts": attempt}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            log.attempts = attempt

    log.status = "failed"
    log.error = (last_error or "")[:2000]
    await db.flush()
    return {"status": "failed", "card_id": card_id, "error": last_error}
