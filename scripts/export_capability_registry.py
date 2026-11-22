"""Export capability registry (受控词表) to a versioned snapshot.

镜像 export_openapi.py 那套 snapshot-based 链路, 但数据源是 `capability_registry` 纯模块
常量 (task/infra/modality/geometry/prompt), 经 `GET /protocol` 的同一 `_build_payload()`
序列化。前端 codegen (scripts/gen-capability-vocab.mjs) 读此 snapshot 生成 ts 常量,
不依赖运行后端。

用法:
    cd apps/api && uv run python ../../scripts/export_capability_registry.py            # 写入 snapshot
    cd apps/api && uv run python ../../scripts/export_capability_registry.py --check    # CI 用, 不一致即 fail

输出:
    apps/api/capability-registry.snapshot.json    # 仓库内版本化契约 (提交)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))


def _render() -> str:
    from app.api.v1.ml_capabilities import _build_payload

    schema = _build_payload().model_dump(mode="json")
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export capability registry snapshot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="不写文件, 仅比对当前 registry 是否与 snapshot 一致; 不一致返回非零退出码",
    )
    args = parser.parse_args()

    rendered = _render()
    repo_root = Path(__file__).resolve().parents[1]
    snapshot_path = repo_root / "apps" / "api" / "capability-registry.snapshot.json"

    if args.check:
        if not snapshot_path.exists():
            print(f"::error::snapshot 不存在: {snapshot_path}")
            print(
                "先运行: cd apps/api && uv run python "
                "../../scripts/export_capability_registry.py"
            )
            return 1
        if snapshot_path.read_text(encoding="utf-8").strip() != rendered.strip():
            print("::error::capability-registry snapshot 与当前 registry 不一致。")
            print(
                "请运行: cd apps/api && uv run python "
                "../../scripts/export_capability_registry.py 并提交。"
            )
            return 1
        print("✓ capability-registry snapshot 与当前 registry 一致")
        return 0

    snapshot_path.write_text(rendered, encoding="utf-8")
    print(f"✓ wrote {snapshot_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
