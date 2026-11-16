"""V8 W3：手动触发/调试盘后推送。

用法：
    python scripts/push_cli.py --dry-run
    python scripts/push_cli.py --date 2026-11-13
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.logging import setup_logging  # noqa: E402
from app.db.session import AsyncSessionLocal, dispose_engines  # noqa: E402
from app.notify.push import push_daily_results  # noqa: E402


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def main() -> int:
    parser = argparse.ArgumentParser(description="企业微信盘后推送")
    parser.add_argument("--date", help="交易日 YYYY-MM-DD；默认最近一次成功选股日")
    parser.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    parser.add_argument("--channel", default=None, help="推送通道，默认 wecom")
    args = parser.parse_args()

    setup_logging()
    async with AsyncSessionLocal() as db:
        result = await push_daily_results(
            db,
            _parse_date(args.date),
            dry_run=args.dry_run,
            channel=args.channel,
        )
        await db.commit()
    await dispose_engines()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
