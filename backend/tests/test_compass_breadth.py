"""成分股广度 breadth_below_ma20 单元测试（先于实现）。"""
from __future__ import annotations

from app.compass.breadth import compute_breadth_below_ma20


def member(close: float | None, ma20: float | None, suspended: bool = False) -> dict:
    return {"close": close, "ma20": ma20, "is_suspended": suspended}


def test_ratio_counts_strictly_below_ma20() -> None:
    result = compute_breadth_below_ma20([
        member(10, 12),
        member(11, 12),
        member(9, 12),
        member(13, 12),
        member(14, 12),
    ])
    assert result["status"] == "ok"
    assert result["below"] == 3
    assert result["valid"] == 5
    assert result["ratio"] == 0.6


def test_close_equal_ma20_is_not_below() -> None:
    result = compute_breadth_below_ma20([
        member(12, 12),
        member(12, 12),
        member(12, 12),
        member(10, 12),
        member(10, 12),
    ])
    assert result["below"] == 2
    assert result["ratio"] == 0.4


def test_missing_or_invalid_values_are_excluded() -> None:
    result = compute_breadth_below_ma20([
        member(10, 12),
        member(11, 12),
        member(9, 12),
        member(13, 12),
        member(14, 12),
        member(None, 12),
        member(10, None),
        member(10, 0),
    ])
    assert result["valid"] == 5
    assert result["ratio"] == 0.6


def test_suspended_members_are_excluded() -> None:
    result = compute_breadth_below_ma20([
        member(10, 12),
        member(11, 12),
        member(9, 12),
        member(13, 12),
        member(14, 12),
        member(1, 12, suspended=True),
    ])
    assert result["valid"] == 5
    assert result["ratio"] == 0.6


def test_fewer_than_five_valid_members_is_insufficient() -> None:
    result = compute_breadth_below_ma20([
        member(10, 12),
        member(11, 12),
        member(9, 12),
        member(13, 12),
    ])
    assert result["status"] == "insufficient"
    assert result["ratio"] is None
    assert result["valid"] == 4


def test_empty_members_is_insufficient() -> None:
    result = compute_breadth_below_ma20([])
    assert result["status"] == "insufficient"
    assert result["ratio"] is None
