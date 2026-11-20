"""capability-registry snapshot 契约测试: 必须与当前 registry 一致。

前端 codegen (apps/web/scripts/gen-capability-vocab.mjs) 读此 snapshot 生成受控词表常量。
改 capability_registry.py / schemas/ml_capabilities.py 后本地跑:
    cd apps/api && uv run python ../../scripts/export_capability_registry.py
刷新 apps/api/capability-registry.snapshot.json 并一并提交。镜像 test_openapi_contract。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.api.v1.ml_capabilities import _build_payload

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1] / "capability-registry.snapshot.json"
)


def _normalize(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).strip()


def test_capability_registry_snapshot_matches_runtime() -> None:
    assert SNAPSHOT_PATH.exists(), (
        f"capability-registry.snapshot.json 不存在于 {SNAPSHOT_PATH}。\n"
        "首次运行: cd apps/api && uv run python ../../scripts/export_capability_registry.py"
    )

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8").strip()
    current = _normalize(_build_payload().model_dump(mode="json"))

    assert current == expected, (
        "capability registry 与 snapshot 不一致。请运行:\n"
        "  cd apps/api && uv run python ../../scripts/export_capability_registry.py\n"
        "并把改动一并提交。"
    )
