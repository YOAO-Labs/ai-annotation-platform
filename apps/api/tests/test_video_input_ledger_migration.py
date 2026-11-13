"""Frozen video owner artifacts and receipt-preserving rollback guards."""

import importlib.util
import json
from pathlib import Path
import re

import pytest

from yoao_assets_video_inputs import evidence, execution

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"


def _contract_subset(document: dict, frozen: dict) -> dict:
    """The bo0225 artifact freezes only the operations that revision installs.

    Later migrations add owner capabilities additively (bo0432 original reads),
    so the current portable contract is a superset of this snapshot.
    """
    return {
        "$schema": document["$schema"],
        "$id": document["$id"],
        "$defs": {name: document["$defs"][name] for name in frozen["$defs"]},
        "x-stage": document["x-stage"],
        "x-budgets": document["x-budgets"],
        "x-manifest-digest": document["x-manifest-digest"],
        "x-native": {name: document["x-native"][name] for name in frozen["x-native"]},
    }


def _installed_operation_names() -> set[str]:
    pattern = re.compile(
        r"(?im)^CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
        r"aap_assets_v1\.([a-z_][a-z0-9_]*)\s*\("
    )
    names: set[str] = set()
    for artifact in SQL.glob("*.sql"):
        names.update(pattern.findall(artifact.read_text(encoding="utf-8")))
    return names


def _migration():
    spec = importlib.util.spec_from_file_location(
        "retained_video_migration",
        ROOT / "apps/api/alembic/versions/0225_retained_video_inputs.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_video_execution_and_evidence_artifacts_match_portable_contracts():
    document = execution.contract_document()
    frozen = json.loads((SQL / "0225_video_inputs.schema.json").read_text())
    assert frozen == _contract_subset(document, frozen)
    for name, definition in frozen["x-native"].items():
        assert document["x-native"][name] == definition
    assert (
        json.loads((SQL / "0225_video_input_evidence.schema.json").read_text())
        == evidence.contract_document()
    )
    migration = _migration()
    signatures = {signature.replace(" ", "") for signature in migration._signatures()}
    for definition in frozen["x-native"].values():
        assert definition["signature"].replace(" ", "") in signatures
    assert not any(
        signature.split("(")[0] in migration._REPLACED for signature in signatures
    )
    installed = _installed_operation_names()
    for name in execution.OPERATIONS:
        assert name in installed, name


@pytest.mark.parametrize(
    "retained",
    [
        "asset_accepted_video_manifests",
        "annotation_video_input_sources",
        "video_garbage",
    ],
)
def test_video_downgrade_keeps_receipts_and_cleanup_responsibility(
    monkeypatch, retained
):
    migration = _migration()

    class Result:
        def __init__(self, count):
            self.count = count

        def scalar_one(self):
            return self.count

    class Bind:
        def execute(self, statement):
            query = str(statement)
            selected = (
                "asset_accepted_input_garbage"
                if retained == "video_garbage"
                else retained
            )
            return Result(int(f"FROM public.{selected}" in query))

    emitted = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(migration.op, "execute", emitted.append)
    monkeypatch.setattr(
        migration.op, "drop_table", lambda *args, **kwargs: emitted.append(args)
    )
    with pytest.raises(RuntimeError, match="Cannot downgrade retained video receipts"):
        migration.downgrade()
    assert emitted == []
