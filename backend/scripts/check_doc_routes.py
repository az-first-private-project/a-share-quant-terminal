"""API 契约与代码路由一致性校验。

用法（在 backend/ 目录）：
    ..\\.venv\\Scripts\\python.exe scripts/check_doc_routes.py

规则：
- 只比对 `/api/v1/**`；`/` 与 `/health` 不在此契约范围；
- 只解析契约中的表格行（方法 + 路径），避免把正文示例误当接口定义；
- 路径参数统一归一为 `{}`，查询串忽略；
- 实际路由缺失于契约、或契约表格多出代码不存在的路由，均以非零退出。
"""
from __future__ import annotations

import os
import re
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.main import app  # noqa: E402

CONTRACT = os.path.join(BACKEND_DIR, "..", "docs", "V5-API-CONTRACT.md")
WIKI = os.path.join(BACKEND_DIR, "..", "docs", "wiki", "02-backend-api.md")
DOCS = [CONTRACT, WIKI]
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def normalize(path: str) -> str:
    text = path.split("?")[0].strip().strip('`"\'')
    text = text.rstrip('.,;:)]"\'')
    return re.sub(r"\{[^}]+\}", "{}", text)


def actual_routes() -> set[tuple[str, str]]:
    rows: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if not path.startswith("/api/v1") or not methods:
            continue
        for method in methods:
            if method in METHODS:
                rows.add((method, normalize(path)))
    return rows


def documented_routes(path: str) -> set[tuple[str, str]]:
    rows: set[tuple[str, str]] = set()
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        methods = [part.strip() for part in cells[0].split("/")]
        path = cells[1].strip("`").strip()
        if not path.startswith("/api/v1"):
            continue
        for method in methods:
            if method in METHODS:
                rows.add((method, normalize(path)))

    # 兼容正文/代码块中的 `GET /api/v1/...` 写法
    pattern = re.compile(
        r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/api/v1/[^\s`|,)\]]+)"
    )
    for match in pattern.finditer(text):
        rows.add((match.group(1), normalize(match.group(2))))
    return rows


def main() -> int:
    actual = actual_routes()
    failed = False
    for doc in DOCS:
        documented = documented_routes(doc)
        missing = sorted(actual - documented)
        extra = sorted(documented - actual)
        print(f"{os.path.basename(doc)}: actual={len(actual)} documented={len(documented)}")
        if missing:
            print("  MISSING_IN_DOC")
            for method, path in missing:
                print(f"    {method:6} {path}")
            failed = True
        if extra:
            print("  DOC_ONLY")
            for method, path in extra:
                print(f"    {method:6} {path}")
            failed = True
    if failed:
        print("\nFAIL: 契约/Wiki 与代码路由不一致")
        return 1
    print("OK: 契约与 Wiki 均与代码路由一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
