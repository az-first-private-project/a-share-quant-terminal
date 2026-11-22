"""仓位罗盘推送文案测试（不触网）。"""
from __future__ import annotations

from app.compass.push import DISCLAIMER, build_message


def payload(**overrides) -> dict:
    base = {
        "trade_date": "2026-11-20",
        "regime": {"label": "震荡"},
        "budget": {"exec_cap": 40},
        "candidates": [
            {"sector_name": "半导体", "etf_code": "512480", "sentiment_percentile": 78},
            {"sector_name": "创新药", "etf_code": "159992", "sentiment_percentile": 64},
        ],
        "check": {"status": "WARN", "title": "T+1 校验 · 1 个标的需要延迟", "text": "创新药 ETF 开盘 -2.3%"},
        "empty_reasons": [],
    }
    base.update(overrides)
    return base


def content(message: dict) -> str:
    return message["markdown"]["content"]


def test_pro_message_contains_etf_and_disclaimer() -> None:
    text = content(build_message(payload(), "pro"))
    assert "512480" in text
    assert "159992" in text
    assert "40%" in text
    assert DISCLAIMER in text


def test_free_message_hides_etf_and_limits_candidates() -> None:
    text = content(build_message(payload(), "free"))
    assert "512480" not in text
    assert "半导体" in text
    assert "创新药" not in text


def test_empty_card_message_explains_short_position() -> None:
    text = content(build_message(payload(
        budget={"exec_cap": 0},
        candidates=[],
        empty_reasons=[{"text": "市场状态为防守，预算上限仅 10%"}],
    ), "pro"))
    assert "空仓" in text
    assert "防守" in text
    assert DISCLAIMER in text
