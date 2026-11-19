#!/bin/sh
# ============================================================
# 后端容器入口（V5）
#   1. 引导数据库：建库/建表/年度分区/视图/策略种子（幂等）
#   2. 空库时灌演示数据（SEED_DEMO_ON_EMPTY=1），让新环境打开就有数据
#   3. 前台运行 uvicorn
# 为什么需要第 2 步：只做 create_all 只会得到空表，页面必然空白；
#   这不是 Playwright 的问题（Playwright 只影响新闻爬虫）。
# ============================================================
set -e

# 已有数据卷升级镜像时必须先走增量迁移：create_all 只补“缺失的表”，
# 不会给旧表补列，否则会出现“表在但缺列”（如 margin_buy_amount）导致接口 500。
# 对全新库 pre 阶段直接跳过，bootstrap 建完表后由 post 阶段 stamp head。
echo "[entrypoint] 应用数据库增量迁移（已有版本库）..."
python scripts/migrate_db.py --phase pre

echo "[entrypoint] 引导数据库结构 ..."
python scripts/bootstrap_db.py

echo "[entrypoint] 校验数据库版本 ..."
python scripts/migrate_db.py --phase post

if [ "${SEED_DEMO_ON_EMPTY:-1}" = "1" ]; then
  echo "[entrypoint] 检查是否需要首次灌数据 ..."
  python scripts/first_run.py || echo "[entrypoint] 首次初始化失败（不阻断启动）"
else
  echo "[entrypoint] SEED_DEMO_ON_EMPTY=${SEED_DEMO_ON_EMPTY}，跳过演示数据"
  echo "[entrypoint] 提示：可执行 docker compose exec backend python scripts/pipeline_cli.py all 采集真实数据"
fi

echo "[entrypoint] 启动 FastAPI ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
