"""V9 P1-4：字段注册表与条件树只读预览。"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.errors import InvalidParamError  # noqa: E402
from app.strategy import condition as cond_dsl  # noqa: E402
from app.strategy import preview as preview_module  # noqa: E402


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["600000", "600519"],
            "name": ["浦发银行", "贵州茅台"],
            "pe_ttm": [8.0, 30.0],
            "close": [10.0, 1500.0],
            "pct_chg": [1.0, -0.5],
            "composite_score": [7.0, 8.0],
            "industry_l1": ["银行", "白酒"],
        }
    )


def test_field_specs_are_consistent() -> None:
    specs = cond_dsl.field_specs()
    keys = [item["key"] for item in specs]
    assert len(keys) == len(set(keys))
    assert "pe_ttm" in keys and "composite_score" in keys
    for item in specs:
        assert item["operators"]
        assert set(item["operators"]).issubset(cond_dsl.OP)


def test_preview_tree_depth_limit() -> None:
    node: dict = {"field": "pe_ttm", "op": "lt", "value": 20}
    for _ in range(6):
        node = {"logic": "AND", "children": [node]}
    tree = cond_dsl.from_dict(node)
    with pytest.raises(InvalidParamError):
        preview_module._validate_tree(tree)  # noqa: SLF001


def test_preview_returns_hits_and_no_writes(monkeypatch) -> None:
    async def fake_fetch(db, trade_date):
        return _frame()

    async def fake_open_dates(db, trade_date):
        return []

    async def fake_latest(db):
        return date(2026, 11, 17)

    monkeypatch.setattr(preview_module, "_fetch_cross_section", fake_fetch)
    monkeypatch.setattr(preview_module, "_load_open_dates", fake_open_dates)
    monkeypatch.setattr(preview_module, "latest_trade_date", fake_latest)
    monkeypatch.setattr(preview_module, "apply_common_filters", lambda frame, d, o: frame)
    monkeypatch.setattr(preview_module, "coerce_numeric_frame", lambda frame: frame)

    tree = cond_dsl.from_dict({"field": "pe_ttm", "op": "lt", "value": 20})
    result = asyncio.run(preview_module.preview_condition_tree(None, tree, limit=20))
    assert result["hit_count"] == 1
    assert result["data"][0]["code"] == "600000"
    assert result["writes"] is False
    assert len(result["param_fingerprint"]) == 16
    assert result["conditions"][0]["passed_count"] == 1
