"""Export FastAPI OpenAPI schema to a versioned snapshot.

用法：
    cd apps/api && uv run python ../../scripts/export_openapi.py            # 写入 snapshot
    cd apps/api && uv run python ../../scripts/export_openapi.py --check    # CI 用，不一致即 fail

输出：
    apps/api/openapi.snapshot.json    # 仓库内的唯一版本化契约

文档站在构建前由 docs-site/scripts/sync-openapi.mjs 将该 snapshot
复制到 public/openapi.json，不再维护第二份跟踪副本。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="Export FastAPI OpenAPI snapshot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="不写文件，仅比对当前 schema 是否与 snapshot 一致；不一致返回非零退出码",
    )
    args = parser.parse_args()

    from app.main import app  # noqa: E402  延迟导入

    schema = app.openapi()
    schema_str = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    repo_root = Path(__file__).resolve().parents[1]
    snapshot_path = repo_root / "apps" / "api" / "openapi.snapshot.json"

    if args.check:
        if not snapshot_path.exists():
            print(f"::error::snapshot 不存在：{snapshot_path}")
            print(
                "先运行：cd apps/api && uv run python ../../scripts/export_openapi.py"
            )
            return 1
        current = snapshot_path.read_text(encoding="utf-8")
        if current.strip() != schema_str.strip():
            print("::error::OpenAPI snapshot 与当前路由不一致。")
            print(
                "请运行：cd apps/api && uv run python ../../scripts/export_openapi.py"
            )
            print("然后把 apps/api/openapi.snapshot.json 一并提交。")
            return 1
        print("✓ openapi snapshot 与当前路由一致")
        return 0

    snapshot_path.write_text(schema_str, encoding="utf-8")
    print(f"✓ wrote {snapshot_path.relative_to(repo_root)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
