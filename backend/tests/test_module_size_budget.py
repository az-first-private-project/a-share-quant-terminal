"""模块体积守卫：阻止新的超大单文件，并锁定已知技术债不再增长。

背景：V9 已完成三组拆分：
- ``strategy/strategies.py``（87KB）→ specs/data/market/conditions/ranking/candidates；
- ``ingest/valuation.py``（133KB）→ valuation_math / valuation_store + 门面；
- ``ingest/akshare_provider.py``（103KB）→ akshare_normalize + provider。

剩余 ``ingest/service.py`` 仍在技术债清单中，拆分前用 ceiling 锁住，避免继续膨胀。
"""
from __future__ import annotations

from pathlib import Path

#: 单个 Python 模块的软上限（字节）。超过即视为需要拆分。
MAX_MODULE_BYTES = 72 * 1024

#: 已知待拆分模块的当前上限；拆分后请把对应条目移除或下调。
#: 清单与 docs/wiki/09-dependencies.md 的“技术债与风险清单”保持一致。
KNOWN_OVERSIZED = {
    "app/ingest/service.py": 150 * 1024,
}


def test_python_module_size_budget() -> None:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        relative = path.relative_to(app_dir.parent).as_posix()
        size = path.stat().st_size
        ceiling = KNOWN_OVERSIZED.get(relative)
        if ceiling is None:
            if size > MAX_MODULE_BYTES:
                offenders.append(
                    f"{relative} 为 {size / 1024:.1f}KB，超过 {MAX_MODULE_BYTES / 1024:.0f}KB；"
                    "请按职责拆分，或加入 KNOWN_OVERSIZED 并登记技术债"
                )
        elif size > ceiling:
            offenders.append(
                f"{relative} 为 {size / 1024:.1f}KB，超过已登记上限 "
                f"{ceiling / 1024:.0f}KB；请拆分而不是继续增长"
            )
    assert not offenders, "\n".join(offenders)
