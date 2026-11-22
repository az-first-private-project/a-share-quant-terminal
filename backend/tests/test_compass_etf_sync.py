"""ETF 历史归一纯函数测试。DB 集成见 tests/integration/test_compass_etf_sync_db.py。"""
from __future__ import annotations

from app.compass.etf_sync import normalize_history


def test_normalize_history_fills_pre_close_and_pct_chg() -> None:
    rows = normalize_history("512480", [
        {"date": "2026-11-05", "open": 1.0, "high": 1.1, "low": 0.9,
         "close": 1.0, "amount": 1.0e8},
        {"date": "2026-11-06", "open": 1.0, "high": 1.2, "low": 1.0,
         "close": 1.1, "amount": 1.2e8},
    ], aum=3.0e8)

    assert len(rows) == 2
    assert rows[0]["pre_close"] is None
    assert rows[1]["pre_close"] == 1.0
    assert rows[1]["pct_chg"] == 10.0
    assert rows[1]["aum"] == 3.0e8


def test_normalize_history_skips_invalid_rows() -> None:
    rows = normalize_history("512480", [
        {"date": "not-a-date", "close": 1.0},
        {"date": "2026-11-06", "close": None},
        {"date": "2026-11-07", "close": 1.2},
    ], aum=3.0e8)
    assert len(rows) == 1
    assert rows[0]["trade_date"].isoformat() == "2026-11-07"
