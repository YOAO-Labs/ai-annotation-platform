"""OpenAPI 契约测试：snapshot 必须与运行时 schema 完全一致。

工作流：
1. 改动后端路由 / Pydantic schema 后，本地跑：
     cd apps/api && uv run python ../../scripts/export_openapi.py
   会刷新 apps/api/openapi.snapshot.json
2. 把 snapshot 与代码改动一并提交，PR reviewer 能直接看到 API 表面变化
3. CI 会跑这个测试，若忘了刷 snapshot 就 fail，避免静默破坏前端契约
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app

SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "openapi.snapshot.json"


def _normalize(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False).strip()


def test_openapi_snapshot_matches_runtime() -> None:
    assert SNAPSHOT_PATH.exists(), (
        f"openapi.snapshot.json 不存在于 {SNAPSHOT_PATH}。\n"
        "首次运行：cd apps/api && uv run python ../../scripts/export_openapi.py"
    )

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8").strip()
    current = _normalize(app.openapi())

    assert current == expected, (
        "OpenAPI 与 snapshot 不一致。请运行：\n"
        "  cd apps/api && uv run python ../../scripts/export_openapi.py\n"
        "并把改动一并提交。"
    )
