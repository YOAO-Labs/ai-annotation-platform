"""Load frozen contract JSON Schemas from per-module contracts.json bundles."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_SQL_DIR = Path(__file__).resolve().parents[1] / "apps/api/alembic/sql"


@lru_cache(maxsize=8)
def _load_index(sql_dir: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    index: dict[str, tuple[Path, dict[str, Any]]] = {}
    for bundle_path in sorted(sql_dir.glob("*.contracts.json")):
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        artifacts = bundle.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ValueError(f"Invalid contracts bundle: {bundle_path}")
        for name, schema in artifacts.items():
            if name in index:
                raise ValueError(
                    f"Duplicate contract artifact {name!r} in {bundle_path} and {index[name][0]}"
                )
            index[name] = (bundle_path, schema)
    return index


def load_contract_schema(
    artifact_name: str, *, sql_dir: Path | None = None
) -> dict[str, Any]:
    directory = (sql_dir or DEFAULT_SQL_DIR).resolve()
    index = _load_index(directory)
    entry = index.get(artifact_name)
    if entry is None:
        raise FileNotFoundError(
            f"Contract schema artifact {artifact_name!r} not found in {directory}"
        )
    return json.loads(json.dumps(entry[1]))


def canonical_contract_bytes(
    artifact_name: str, *, sql_dir: Path | None = None
) -> bytes:
    schema = load_contract_schema(artifact_name, sql_dir=sql_dir)
    return (
        json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def contract_schema_sha256(artifact_name: str, *, sql_dir: Path | None = None) -> str:
    return hashlib.sha256(
        canonical_contract_bytes(artifact_name, sql_dir=sql_dir)
    ).hexdigest()


def contract_bundle_path(artifact_name: str, *, sql_dir: Path | None = None) -> Path:
    directory = (sql_dir or DEFAULT_SQL_DIR).resolve()
    index = _load_index(directory)
    entry = index.get(artifact_name)
    if entry is None:
        raise FileNotFoundError(
            f"Contract schema artifact {artifact_name!r} not found in {directory}"
        )
    return entry[0]


def all_contract_artifacts(*, sql_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    directory = (sql_dir or DEFAULT_SQL_DIR).resolve()
    return {
        name: json.loads(json.dumps(schema))
        for name, (_, schema) in _load_index(directory).items()
    }
