"""数据库增量迁移（容器启动入口调用）
=====================================
背景
----
`bootstrap_db.py` 使用 `create_all`，只会在**表不存在**时建表，不会给旧表补列。
已有数据卷升级镜像后，若数据库版本落后（例如仍停在 0010），就会出现
「表存在但缺列」的结构漂移，典型表现是 `/api/v1/market/dashboard` 500
（`margin_trading.margin_buy_amount does not exist`）。

本脚本负责把「已存在的库」推进到 `alembic head`：
  - 有 `alembic_version`：直接 `alembic upgrade head`（增量迁移，幂等）；
  - 没有 `alembic_version`：
      · 结构完整（全新库刚由 bootstrap create_all 建好）→ `alembic stamp head`；
      · 结构缺表/缺列（未版本化的旧库）→ 拒绝启动并给出修复指引，
        避免把漂移库误标成 head。

用法：
  `python scripts/migrate_db.py --phase pre`   # bootstrap 前：已有版本库先增量升级
  `python scripts/migrate_db.py --phase post`  # bootstrap 后：全新库 stamp head / 校验版本
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import DATABASE_URL_SYNC  # noqa: E402
from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db import models  # noqa: F401,E402  —— 注册全部表

logger = get_logger("migrate")


def _target_url():
    """与 alembic/env.py 保持一致的演示库/测试库覆盖逻辑。"""
    url = make_url(DATABASE_URL_SYNC)
    demo_name = os.getenv("GFNA_DEMO_DATABASE_NAME", "").strip()
    if demo_name:
        if not demo_name.endswith("_demo"):
            raise RuntimeError("演示库名必须以 _demo 结尾")
        return url.set(database=demo_name)
    if os.getenv("GFNA_TEST_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        test_url = os.getenv("GFNA_TEST_DATABASE_URL_SYNC", "").strip()
        if test_url:
            url = make_url(test_url)
        else:
            url = url.set(
                database=os.getenv("GFNA_TEST_DATABASE_NAME", "gfna_test").strip()
            )
        if not (url.database or "").endswith("_test"):
            raise RuntimeError("测试库名必须以 _test 结尾")
    return url


def _missing_objects(engine) -> list[str]:
    """返回 metadata 中缺失的表/列（用于识别未版本化的漂移库）。"""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name == "alembic_version":
            continue
        if table_name not in existing_tables:
            missing.append(f"table:{table_name}")
            continue
        existing_columns = {item["name"] for item in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in existing_columns:
                missing.append(f"{table_name}.{column.name}")
    return missing


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def _upgrade(config: Config, engine) -> int:
    with engine.connect() as conn:
        current = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    logger.info("检测到 alembic_version=%s，执行增量迁移 ...", current)
    command.upgrade(config, "head")
    with engine.connect() as conn:
        updated = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    logger.info("✅ 数据库迁移完成：%s → %s", current, updated)
    print(f"[migrate] database at head: {updated}", flush=True)
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="数据库增量迁移")
    parser.add_argument(
        "--phase", choices=("pre", "post"), default="post",
        help="pre=建表前先升级已有版本库；post=建表后校验/标记版本",
    )
    args = parser.parse_args()

    setup_logging()
    engine = create_engine(
        _target_url().render_as_string(hide_password=False), pool_pre_ping=True
    )
    try:
        has_version = inspect(engine).has_table("alembic_version")
        config = _alembic_config()

        if args.phase == "pre":
            if not has_version:
                logger.info(
                    "未检测到 alembic_version，跳过预迁移（建表后由 --phase post 标记基线）"
                )
                return 0
            return _upgrade(config, engine)

        if has_version:
            return _upgrade(config, engine)

        missing = _missing_objects(engine)
        if missing:
            preview = ", ".join(missing[:12])
            suffix = f" 等 {len(missing)} 项" if len(missing) > 12 else ""
            logger.error(
                "检测到未版本化的旧库结构，缺少：%s%s。"
                "请先备份，然后执行 `python scripts/bootstrap_db.py --recreate` "
                "重建，或手动对齐结构后再启动。",
                preview,
                suffix,
            )
            return 1

        logger.info("未检测到 alembic_version，结构完整，标记为 alembic head ...")
        command.stamp(config, "head")
        logger.info("✅ 已将现有库标记为 head（后续升级走增量迁移）")
        print("[migrate] stamped existing schema as alembic head", flush=True)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
