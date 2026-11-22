"""T+1 开盘校验状态机。

只负责状态与数值判断，不做数据库读写；服务层负责落库与幂等。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.compass.constants import OPEN_GAP_THRESHOLD

TERMINAL = {
    "PASS", "PASS_AFTER_RECHECK", "DROPPED", "DROPPED_BY_RULE",
    "EXPIRED", "EXECUTED", "CLOSED", "DATA_NOT_READY",
}


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_open_gap(
    prev_close: float | None,
    open_price: float | None,
    *,
    is_suspended: bool = False,
    threshold: float = OPEN_GAP_THRESHOLD,
) -> tuple[str, float | None]:
    """返回 (状态, 偏离百分比)。恰好 ±2.00% 不触发。"""
    if is_suspended:
        return "SUSPENDED", None
    prev = _f(prev_close)
    current = _f(open_price)
    if prev is None or current is None or prev <= 0:
        return "DATA_NOT_READY", None
    gap = round((current / prev - 1.0) * 100.0, 2)
    if abs(gap) > threshold:
        return "WARN", gap
    return "PASS", gap


def apply_action(
    check: dict,
    action: str,
    *,
    now: datetime,
    deadline: datetime,
) -> dict:
    """用户操作：DELAY / DROP / EXECUTED。幂等重放返回 idempotent_replay=True。"""
    act = str(action or "").upper()
    if act not in {"DELAY", "DROP", "EXECUTED"}:
        raise ValueError(f"invalid action: {action!r}")

    if now >= deadline:
        raise ValueError("check expired")

    status = check.get("status")
    if status == "DELAYED" and act == "DELAY":
        replay = dict(check)
        replay["idempotent_replay"] = True
        return replay
    if status != "WARN":
        raise ValueError(f"check not actionable in status {status!r}")

    out = dict(check)
    out["user_action"] = act
    out["action_at"] = now.isoformat()
    out["idempotent_replay"] = False
    if act == "DELAY":
        out["status"] = "DELAYED"
    elif act == "DROP":
        out["status"] = "DROPPED"
        out["final_status"] = "DROPPED"
    else:
        out["status"] = "EXECUTED"
        out["final_status"] = "EXECUTED"
        out["user_override"] = True
    return out


def resolve_recheck(
    check: dict,
    recheck_gap: float | None,
    *,
    threshold: float = OPEN_GAP_THRESHOLD,
) -> dict:
    """10:00 复核：回到阈值内放行，否则按规则放弃。"""
    if check.get("status") != "DELAYED":
        raise ValueError("recheck only applies to DELAYED checks")

    out = dict(check)
    gap = _f(recheck_gap)
    out["recheck_gap"] = gap
    if gap is None:
        out["status"] = "DATA_NOT_READY"
        out["final_status"] = "DATA_NOT_READY"
    elif abs(gap) <= threshold:
        out["status"] = "PASS_AFTER_RECHECK"
        out["final_status"] = "PASS_AFTER_RECHECK"
    else:
        out["status"] = "DROPPED_BY_RULE"
        out["final_status"] = "DROPPED_BY_RULE"
    return out


def expire_if_due(check: dict, *, now: datetime, deadline: datetime) -> dict:
    """10:00 未处理自动收口。已终态的检查不再变化。"""
    if check.get("status") in TERMINAL:
        return dict(check)
    if now >= deadline:
        out = dict(check)
        out["status"] = "EXPIRED"
        out["final_status"] = "EXPIRED"
        return out
    return dict(check)
