from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_schemas import (
    all_contract_artifacts,
    canonical_contract_bytes,
    contract_bundle_path,
    contract_schema_sha256,
    load_contract_schema,
)


def test_checked_in_contract_bundles_cover_all_208_artifacts_and_valid_schema_headers():
    artifacts = all_contract_artifacts()
    assert len(artifacts) == 208
    bundles = sorted(
        p.name
        for p in (Path(__file__).resolve().parents[1] / "apps/api/alembic/sql").glob(
            "*.contracts.json"
        )
    )
    assert bundles == [
        "assets.contracts.json",
        "audit.contracts.json",
        "identity.contracts.json",
        "inference.contracts.json",
        "jobs.contracts.json",
        "labeling.contracts.json",
        "notifications.contracts.json",
        "workspace.contracts.json",
    ]
    for name, contract in artifacts.items():
        assert (
            contract.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        ), name
        assert isinstance(contract.get("$id"), str), name
        assert isinstance(contract.get("$defs", {}), dict), name
        assert contract_bundle_path(name).name.endswith(".contracts.json")


def test_contract_loader_returns_isolated_copies_and_deterministic_hashes(tmp_path):
    (tmp_path / "demo.contracts.json").write_text(
        json.dumps(
            {
                "module": "demo",
                "artifacts": {
                    "0001_demo.schema.json": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$id": "demo/v1",
                        "b": 2,
                        "a": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_contract_schema("0001_demo.schema.json", sql_dir=tmp_path)
    loaded["a"] = 999
    assert load_contract_schema("0001_demo.schema.json", sql_dir=tmp_path)["a"] == 1
    assert canonical_contract_bytes("0001_demo.schema.json", sql_dir=tmp_path) == (
        b'{"$id":"demo/v1","$schema":"https://json-schema.org/draft/2020-12/schema","a":1,"b":2}\n'
    )
    assert len(contract_schema_sha256("0001_demo.schema.json", sql_dir=tmp_path)) == 64
    with pytest.raises(FileNotFoundError):
        load_contract_schema("0002_missing.schema.json", sql_dir=tmp_path)


def test_contract_loader_rejects_duplicate_artifact_names(tmp_path):
    doc = {"module": "x", "artifacts": {"dup.schema.json": {"$id": "x"}}}
    (tmp_path / "a.contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "b.contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate contract artifact"):
        load_contract_schema("dup.schema.json", sql_dir=tmp_path)
