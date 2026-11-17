"""数据源尝试埋点与 7 日健康度聚合（V9 P0-3）。

只记录 ProviderChain 已经产出的 ``attempts``，不改变采集语义：
- 每个 ``(trade_date, provider, task)`` 每天最多 100 行，防止异常源刷爆表；
- 错误只保留异常类名/errcode/HTTP 状态，过滤 URL、token、Webhook；
- 写入失败只记日志，不影响采集主链。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.meta import ProviderHealth

logger = get_logger(__name__)

MAX_ATTEMPTS_PER_KEY_DAY = 100

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(webhook|token|api[_-]?key|secret|password)=([^&\s]+)", re.IGNORECASE
)
_SAFE_RESULTS = {"ok", "partial"}
_DEGRADED_RESULTS = {"failed", "unavailable", "not_implemented", "empty"}


def sanitize_error(value: Any) -> str | None:
    """脱敏错误文本：URL → <url>，密钥键值 → <redacted>，截断 200 字符。"""
    text = str(value or "").strip()
    if not text:
        return None
    text = _URL_RE.sub("<url>", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[:200]


def attempts_to_rows(
    task: str, trade_date: date | None, attempts: Iterable[dict[str, Any]] | None
) -> list[ProviderHealth]:
    """把 ProviderChain 的 attempts 转成 ORM 行。"""
    rows: list[ProviderHealth] = []
    for item in attempts or []:
        provider = str(item.get("source") or "unknown")[:32]
        result = str(item.get("status") or "unknown")[:16]
        duration = item.get("elapsed_ms")
        try:
            duration_ms = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        rows.append(
            ProviderHealth(
                trade_date=trade_date,
                provider=provider,
                task=str(task or "unknown")[:64],
                result=result,
                duration_ms=duration_ms,
                error_code=sanitize_error(item.get("error"))[:64]
                if sanitize_error(item.get("error"))
                else None,
            )
        )
    return rows


async def record_provider_attempts(
    db: AsyncSession | None,
    task: str,
    trade_date: date | None,
    attempts: Iterable[dict[str, Any]] | None,
) -> int:
    """写入 attempts；按 key 日上限截断；失败只记日志。"""
    if db is None:
        return 0
    try:
        rows = attempts_to_rows(task, trade_date, attempts)
        if not rows:
            return 0
        counts: dict[tuple[str, str, date | None], int] = {}
        written = 0
        for row in rows:
            key = (row.provider, row.task, row.trade_date)
            if key not in counts:
                stmt = select(func.count()).select_from(ProviderHealth).where(
                    ProviderHealth.provider == row.provider,
                    ProviderHealth.task == row.task,
                )
                if row.trade_date is None:
                    stmt = stmt.where(ProviderHealth.trade_date.is_(None))
                else:
                    stmt = stmt.where(ProviderHealth.trade_date == row.trade_date)
                counts[key] = int((await db.execute(stmt)).scalar_one() or 0)
            if counts[key] >= MAX_ATTEMPTS_PER_KEY_DAY:
                continue
            counts[key] += 1
            db.add(row)
            written += 1
        if written:
            await db.flush()
        return written
    except Exception:  # noqa: BLE001 - 埋点失败不得影响采集
        logger.warning("写 provider 健康度失败（已隔离）", exc_info=True)
        return 0


async def summarize_provider_health(
    db: AsyncSession, days: int = 7
) -> list[dict[str, Any]]:
    """按 provider × task 聚合窗口内的尝试、成功率与降级次数。"""
    days = max(1, min(int(days), 30))
    since = datetime.combine(date.today() - timedelta(days=days - 1), time.min, tzinfo=timezone.utc)
    rows = (
        await db.execute(
            select(ProviderHealth)
            .where(ProviderHealth.created_at >= since)
            .order_by(ProviderHealth.created_at.desc())
        )
    ).scalars().all()

    buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "provider": "",
            "task": "",
            "total": 0,
            "ok": 0,
            "degraded": 0,
            "last_error": None,
            "last_error_at": None,
            "last_seen_at": None,
        }
    )
    for row in rows:
        bucket = buckets[(row.provider, row.task)]
        bucket["provider"] = row.provider
        bucket["task"] = row.task
        bucket["total"] += 1
        if row.result in _SAFE_RESULTS:
            bucket["ok"] += 1
        if row.result in _DEGRADED_RESULTS:
            bucket["degraded"] += 1
        stamp = row.created_at.isoformat() if row.created_at else None
        if bucket["last_seen_at"] is None:
            bucket["last_seen_at"] = stamp
        if row.error_code and (
            bucket["last_error_at"] is None or (stamp or "") > (bucket["last_error_at"] or "")
        ):
            bucket["last_error"] = row.error_code
            bucket["last_error_at"] = stamp

    items = []
    for bucket in buckets.values():
        total = bucket["total"]
        bucket["success_rate"] = round(bucket["ok"] / total, 4) if total else 0.0
        bucket.pop("last_error_at", None)
        items.append(bucket)
    items.sort(key=lambda item: (item["success_rate"], -item["total"]))
    return items


__all__ = [
    "MAX_ATTEMPTS_PER_KEY_DAY",
    "attempts_to_rows",
    "record_provider_attempts",
    "sanitize_error",
    "summarize_provider_health",
]
