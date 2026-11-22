"""仓位罗盘 · T+1 开盘校验状态机单元测试（先于实现）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.compass.checks import (
    apply_action,
    evaluate_open_gap,
    expire_if_due,
    resolve_recheck,
)

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 23, 9, 41, tzinfo=TZ)
DEADLINE = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)


def check(status: str = "WARN", **overrides) -> dict:
    base = {
        "check_id": "chk-1",
        "etf_code": "159992",
        "status": status,
        "user_action": None,
        "final_status": None,
        "open_gap": -2.3,
        "recheck_gap": None,
        "user_override": False,
        "idempotent_replay": False,
    }
    base.update(overrides)
    return base


def test_gap_boundary_200_is_pass() -> None:
    assert evaluate_open_gap(100.0, 102.0) == ("PASS", 2.0)
    assert evaluate_open_gap(100.0, 98.0) == ("PASS", -2.0)


def test_gap_above_threshold_is_warn_both_directions() -> None:
    assert evaluate_open_gap(100.0, 102.01) == ("WARN", 2.01)
    assert evaluate_open_gap(100.0, 97.99)[0] == "WARN"


def test_suspended_and_missing_open_price() -> None:
    assert evaluate_open_gap(100.0, None, is_suspended=True)[0] == "SUSPENDED"
    assert evaluate_open_gap(100.0, None)[0] == "DATA_NOT_READY"
    assert evaluate_open_gap(None, 100.0)[0] == "DATA_NOT_READY"


def test_delay_action_transitions_and_is_idempotent() -> None:
    delayed = apply_action(check(), "DELAY", now=NOW, deadline=DEADLINE)
    assert delayed["status"] == "DELAYED"
    assert delayed["user_action"] == "DELAY"
    assert delayed["idempotent_replay"] is False

    replay = apply_action(delayed, "DELAY", now=NOW, deadline=DEADLINE)
    assert replay["status"] == "DELAYED"
    assert replay["idempotent_replay"] is True


def test_drop_action_from_warn() -> None:
    dropped = apply_action(check(), "DROP", now=NOW, deadline=DEADLINE)
    assert dropped["status"] == "DROPPED"


def test_execute_after_warning_marks_override() -> None:
    executed = apply_action(check(), "EXECUTED", now=NOW, deadline=DEADLINE)
    assert executed["status"] == "EXECUTED"
    assert executed["user_override"] is True


def test_action_on_pass_is_rejected() -> None:
    with pytest.raises(ValueError):
        apply_action(check(status="PASS"), "DELAY", now=NOW, deadline=DEADLINE)


def test_action_after_deadline_expires() -> None:
    late = NOW + timedelta(minutes=30)
    with pytest.raises(ValueError):
        apply_action(check(), "DELAY", now=late, deadline=DEADLINE)


def test_recheck_pass_and_fail() -> None:
    delayed = apply_action(check(), "DELAY", now=NOW, deadline=DEADLINE)
    passed = resolve_recheck(delayed, 1.5)
    assert passed["final_status"] == "PASS_AFTER_RECHECK"

    failed = resolve_recheck(delayed, -2.5)
    assert failed["final_status"] == "DROPPED_BY_RULE"


def test_expire_unresolved_before_deadline_is_noop() -> None:
    current = check()
    assert expire_if_due(current, now=NOW, deadline=DEADLINE)["status"] == "WARN"

    expired = expire_if_due(current, now=DEADLINE, deadline=DEADLINE)
    assert expired["status"] == "EXPIRED"
