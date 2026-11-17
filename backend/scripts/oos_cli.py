"""V9 P0-2：样本外 / walk-forward 验证 CLI。

用法（在 backend/ 下执行）：

    # A3：数据允许范围内自动降级为 expanding window（4 折）
    python scripts/oos_cli.py --algo a3 --start 2024-01-02 --end 2026-11-16

    # A4：对 pe_max=12 做 2022–2023 单窗口样本外对照
    python scripts/oos_cli.py --algo a4 --start 2022-01-04 --end 2023-12-29 \
        --single-window --param pe_max=12 --output artifacts/oos_a4_pe12.json

    # A3：只看测试窗，不跑训练窗
    python scripts/oos_cli.py --algo a3 --start 2024-01-02 --end 2026-11-16 --no-train

输出：默认打印完整 JSON；``--output`` 同时写入文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from app.core.config import BACKTEST  # noqa: E402
from app.core.errors import DataUnavailableError, InvalidParamError  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.session import AsyncSessionLocal, dispose_engines  # noqa: E402
from app.strategy.oos_validation import (  # noqa: E402
    build_folds,
    run_oos,
    run_single_window,
)


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到: {value}") from exc


def _cast(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_params(items: list[str] | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise InvalidParamError(f"--param 需要 key=value 形式，收到: {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise InvalidParamError(f"--param 的 key 不能为空: {item!r}")
        params[key] = _cast(raw.strip())
    return params


async def _data_audit(
    db, algo_code: str, requested_start: date, requested_end: date
) -> dict[str, Any]:
    """检查策略所需数据的最早/最晚可用日，并把区间夹到真实数据范围。"""
    score_row = (
        await db.execute(
            text(
                "select min(trade_date), max(trade_date), count(distinct trade_date) "
                "from fact_stock_score_daily where composite_score is not null"
            )
        )
    ).first()
    a4_row = (
        await db.execute(
            text(
                "select min(trade_date), max(trade_date), count(distinct trade_date) "
                "from fact_stock_daily where pe_ttm is not null and dv_ttm is not null"
            )
        )
    ).first()
    feature_row = (
        await db.execute(
            text(
                "select min(trade_date), max(trade_date), count(distinct trade_date) "
                "from fact_feature_daily where momentum_20 is not null"
            )
        )
    ).first()
    regime_row = (
        await db.execute(
            text(
                "select min(trade_date), max(trade_date), count(*) "
                "from market_regime_daily"
            )
        )
    ).first()

    def _range(row) -> dict[str, Any]:
        if row is None or row[0] is None or row[1] is None:
            return {"start": None, "end": None, "days": 0}
        return {
            "start": row[0].isoformat(),
            "end": row[1].isoformat(),
            "days": int(row[2] or 0),
        }

    score_range = _range(score_row)
    a4_range = _range(a4_row)
    feature_range = _range(feature_row)
    regime_range = _range(regime_row)

    if algo_code == "a3":
        required = score_range
    elif algo_code == "a4":
        required = a4_range
    else:
        required = feature_range

    if not required["start"]:
        raise DataUnavailableError(
            f"策略 {algo_code} 所需的行情/评分数据为空，无法做样本外验证",
            next_action="先运行 pipeline_cli.py 完成对应数据回补与评分",
        )

    effective_start = max(requested_start, date.fromisoformat(required["start"]))
    effective_end = min(requested_end, date.fromisoformat(required["end"]))
    if effective_start > effective_end:
        raise DataUnavailableError(
            f"请求区间 {requested_start}~{requested_end} 与可用数据 "
            f"{required['start']}~{required['end']} 没有交集",
            detail={
                "algo": algo_code,
                "required_range": required,
            },
        )

    notes: list[str] = []
    if effective_start > requested_start:
        notes.append(
            f"请求起点 {requested_start} 早于 {algo_code} 可用起点 "
            f"{required['start']}，已夹到 {effective_start}"
        )
    if effective_end < requested_end:
        notes.append(
            f"请求终点 {requested_end} 晚于数据终点 {required['end']}，已夹到 {effective_end}"
        )
    if algo_code == "a4" and regime_range["start"]:
        notes.append(
            "a4 防御门槛依赖 market_regime_daily；该表从 "
            f"{regime_range['start']} 开始，之前按“阶段缺失放行”处理"
        )

    return {
        "algo": algo_code,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "effective_start": effective_start.isoformat(),
        "effective_end": effective_end.isoformat(),
        "data_clamped": bool(notes),
        "score_range": score_range,
        "a4_input_range": a4_range,
        "feature_range": feature_range,
        "regime_range": regime_range,
        "notes": notes,
    }


def _bt_params() -> dict[str, Any]:
    return {
        "rebalance_days": int(BACKTEST.rebalance_days),
        "max_positions": int(BACKTEST.max_positions),
        "benchmark": str(BACKTEST.benchmark_code),
        "commission": float(BACKTEST.commission_rate),
        "stamp_tax": float(BACKTEST.stamp_tax_rate),
        "slippage": float(BACKTEST.slippage_rate),
    }


async def _run(args) -> int:
    setup_logging()
    requested_start = args.start or date(date.today().year - 2, 1, 1)
    requested_end = args.end or date.today()
    params = _parse_params(args.param)
    bt_params = _bt_params()

    async with AsyncSessionLocal() as db:
        audit = await _data_audit(db, args.algo, requested_start, requested_end)
        start = date.fromisoformat(audit["effective_start"])
        end = date.fromisoformat(audit["effective_end"])

        if args.single_window:
            result = await run_single_window(
                args.algo,
                start,
                end,
                strategy_params=params,
                bt_params=bt_params,
            )
            payload = {
                "mode": "single_window",
                "data_audit": audit,
                "window": result,
            }
        else:
            folds = build_folds(
                start,
                end,
                train_months=args.train_months,
                test_months=args.test_months,
                step_months=args.step_months,
                min_folds=args.min_folds,
            )

            def progress(row: dict[str, Any]) -> None:
                test = row.get("test") or {}
                metrics = test.get("metrics") or {}
                print(
                    f"[fold {row['fold_index']}] {row['window_type']} "
                    f"train {row['train_start']}~{row['train_end']} "
                    f"test {row['test_start']}~{row['test_end']} "
                    f"annual={metrics.get('annual_return')} "
                    f"excess={metrics.get('excess_return')} "
                    f"sharpe={metrics.get('sharpe')}",
                    file=sys.stderr,
                )

            result = await run_oos(
                args.algo,
                folds,
                strategy_params=params,
                bt_params=bt_params,
                include_train=not args.no_train,
                progress=progress,
            )
            payload = {
                "mode": "walk_forward",
                "data_audit": audit,
                **result,
            }

    await dispose_engines()
    output = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(output)
    if args.output:
        target = Path(args.output)
        if not target.is_absolute():
            target = BACKEND_DIR / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
        print(f"\nwritten: {target}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V9 样本外 / walk-forward 验证")
    parser.add_argument("--algo", required=True, help="策略代码，如 a3 / a4")
    parser.add_argument("--start", type=_parse_date, help="请求起点 YYYY-MM-DD")
    parser.add_argument("--end", type=_parse_date, help="请求终点 YYYY-MM-DD")
    parser.add_argument("--train-months", type=int, default=24)
    parser.add_argument("--test-months", type=int, default=6)
    parser.add_argument("--step-months", type=int, default=6)
    parser.add_argument("--min-folds", type=int, default=4)
    parser.add_argument(
        "--param",
        action="append",
        help="策略参数覆盖，可重复：--param pe_max=12",
    )
    parser.add_argument("--no-train", action="store_true", help="只跑测试窗")
    parser.add_argument(
        "--single-window",
        action="store_true",
        help="单窗口模式：不做折切分，直接回测 --start~--end（用于参数对照）",
    )
    parser.add_argument("--output", help="额外把 JSON 写入文件（相对 backend/）")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
