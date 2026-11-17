"""条件树只读预览（V9 P1-4）。

复用正式选股的横截面取数与统一过滤，但**不写 stg_run**、不进入缓存/共识。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidParamError
from app.strategy import condition as cond_dsl
from app.strategy.data import (
    _fetch_cross_section,
    _load_open_dates,
    apply_common_filters,
    coerce_numeric_frame,
)
from app.strategy.runner import latest_trade_date
from app.strategy.specs import jsonable

MAX_DEPTH = 5
MAX_NODES = 50
DISPLAY_COLUMNS = (
    "code", "name", "close", "pct_chg", "pe_ttm", "pb", "dv_ttm",
    "composite_score", "industry_l1",
)


def _validate_tree(node: Any, depth: int = 1, counter: list[int] | None = None) -> int:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > MAX_DEPTH:
        raise InvalidParamError(
            f"条件树嵌套超过 {MAX_DEPTH} 层",
            detail={"max_depth": MAX_DEPTH},
        )
    if counter[0] > MAX_NODES:
        raise InvalidParamError(
            f"条件树节点超过 {MAX_NODES} 个",
            detail={"max_nodes": MAX_NODES},
        )
    if isinstance(node, cond_dsl.ConditionGroup):
        if not node.children:
            raise InvalidParamError("条件组 children 不能为空")
        for child in node.children:
            _validate_tree(child, depth + 1, counter)
    elif not isinstance(node, cond_dsl.Condition):
        raise InvalidParamError(
            f"未支持的条件节点类型: {type(node).__name__}"
        )
    return counter[0]


def _fingerprint(tree: Any) -> str:
    payload = json.dumps(cond_dsl.to_dict(tree), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def preview_condition_tree(
    db: AsyncSession,
    tree: cond_dsl.Condition | cond_dsl.ConditionGroup,
    trade_date: date | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """执行一次只读预览：命中数、前 N 行、逐条条件通过数。"""
    started = time.perf_counter()
    _validate_tree(tree)
    limit = max(1, min(int(limit), 100))
    resolved = trade_date or await latest_trade_date(db)

    frame = await _fetch_cross_section(db, resolved)
    if len(frame) == 0:
        return {
            "trade_date": resolved.isoformat(),
            "universe_count": 0,
            "hit_count": 0,
            "limit": limit,
            "conditions": [],
            "data": [],
            "param_fingerprint": _fingerprint(tree),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "data_ready": False,
            "warning": "该交易日没有行情数据",
            "writes": False,
        }

    open_dates = await _load_open_dates(db, resolved)
    eligible = apply_common_filters(frame, resolved, open_dates)
    working = coerce_numeric_frame(eligible)
    universe_count = int(len(working))

    warning = None
    if "composite_score" in cond_dsl.flatten_fields(tree):
        if "composite_score" not in working.columns or working["composite_score"].notna().sum() == 0:
            warning = "该交易日尚无个股综合评分，composite_score 条件可能全部不命中"

    mask = cond_dsl.evaluate(working, tree)
    hit_count = int(mask.sum())
    details = cond_dsl.explain(working, tree)
    condition_summary = [
        {
            "key": detail.key,
            "label": detail.label,
            "scope": detail.scope,
            "passed_count": int(detail.passed.sum()),
            "total": int(len(detail.passed)),
        }
        for detail in details
    ]

    columns = [name for name in DISPLAY_COLUMNS if name in working.columns]
    rows = working.loc[mask, columns].head(limit).to_dict("records")
    data = [{key: jsonable(value) for key, value in row.items()} for row in rows]
    return {
        "trade_date": resolved.isoformat(),
        "universe_count": universe_count,
        "hit_count": hit_count,
        "limit": limit,
        "conditions": condition_summary,
        "data": data,
        "param_fingerprint": _fingerprint(tree),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "data_ready": True,
        "warning": warning,
        "writes": False,
    }


__all__ = ["MAX_DEPTH", "MAX_NODES", "preview_condition_tree"]
