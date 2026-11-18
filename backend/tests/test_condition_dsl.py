"""T7 条件 DSL 扩展与逐条解释单测。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.errors import InvalidParamError
from app.strategy.condition import (
    Condition,
    ConditionGroup,
    FieldRef,
    describe,
    evaluate,
    explain,
    flatten_fields,
    from_dict,
    to_dict,
    triggered_row,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["600000", "600001", "600002"],
            "name": ["甲", "乙", "丙"],
            "close": [10.0, 9.0, 11.0],
            "open": [9.5, 9.6, 10.0],
            "pre_close": [9.8, 9.2, 10.5],
            "ema20": [9.0, 10.0, 10.0],
            "ma60": [9.5, 9.5, 10.5],
            "volume": [160.0, 140.0, 90.0],
            "vol_ma5": [100.0, 100.0, 100.0],
            "amount_ma20": [3.0e8, None, 2.0e8],
            "roc10": [5.0, -1.0, 2.0],
            "ema_cross_3d": [True, False, True],
            "ema20_up": [True, True, False],
        }
    )


def test_field_ref_comparisons_and_multiplier() -> None:
    frame = _frame()
    above_ema = Condition("close", "gt", FieldRef("ema20"))
    assert list(evaluate(frame, above_ema)) == [True, False, True]

    volume_expansion = Condition("volume", "ge", FieldRef("vol_ma5", 1.5))
    assert list(evaluate(frame, volume_expansion)) == [True, False, False]

    below_ma = Condition("close", "le", FieldRef("ma60"))
    assert list(evaluate(frame, below_ma)) == [False, True, False]


def test_field_ref_missing_column_raises() -> None:
    frame = _frame()
    node = Condition("close", "gt", FieldRef("not_exists"))
    with pytest.raises(InvalidParamError):
        evaluate(frame, node)


def test_notna_isna_and_describe() -> None:
    frame = _frame()
    assert list(evaluate(frame, Condition("amount_ma20", "notna"))) == [True, False, True]
    assert list(evaluate(frame, Condition("amount_ma20", "isna"))) == [False, True, False]
    assert describe(Condition("amount_ma20", "notna")) == "amount_ma20 非空"
    fields = flatten_fields(
        Condition("volume", "ge", FieldRef("vol_ma5", 1.5))
    )
    assert fields == ["volume", "vol_ma5"]


def test_field_ref_roundtrip_with_metadata() -> None:
    node = Condition(
        "close",
        "gt",
        FieldRef("ema20", 1.2),
        key="close_above_ema20",
        label="收盘站上 EMA20",
        detail_fields=("close", "ema20"),
    )
    payload = to_dict(node)
    assert payload["value"] == {"$field": "ema20", "multiplier": 1.2}
    restored = from_dict(payload)
    assert isinstance(restored, Condition)
    assert isinstance(restored.value, FieldRef)
    assert restored.value.multiplier == pytest.approx(1.2)
    assert restored.key == "close_above_ema20"


def test_explain_and_triggered_row_json_safe() -> None:
    frame = _frame()
    group = ConditionGroup(
        children=[
            Condition(
                "volume", "ge", FieldRef("vol_ma5", 1.5),
                key="volume_expansion", label="放量",
                detail_fields=("volume", "vol_ma5"),
            ),
            ConditionGroup(
                logic="OR", key="bullish_bar", label="收阳或收盘不低于昨收",
                detail_fields=("open", "close", "pre_close"),
                children=[
                    Condition("close", "gt", FieldRef("open")),
                    Condition("close", "ge", FieldRef("pre_close")),
                ],
            ),
        ]
    )
    details = explain(frame, group)
    assert [item.key for item in details] == ["volume_expansion", "bullish_bar"]

    triggered = triggered_row(details, frame, 0)
    assert triggered[0]["actual"] == pytest.approx(160.0)
    assert triggered[0]["op"] == "ge"
    assert triggered[0]["threshold"] == pytest.approx(150.0)
    assert triggered[0]["detail"] == "volume=160 vol_ma5=100"
    assert triggered[1]["actual"] is True
    assert triggered[1]["detail"] == "open=9.5 close=10 pre_close=9.8"
    json.dumps(triggered, ensure_ascii=False, allow_nan=False)
