"""Frozen video owner artifacts and receipt-preserving rollback guards."""

import importlib.util
import json
from pathlib import Path

import pytest

from aap_assets_video_inputs import evidence, execution

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "retained_video_migration",
        ROOT / "apps/api/alembic/versions/0225_retained_video_inputs.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_video_execution_and_evidence_artifacts_match_portable_contracts():
    assert (
        json.loads((SQL / "0225_video_inputs.schema.json").read_text())
        == execution.contract_document()
    )
    assert (
        json.loads((SQL / "0225_video_input_evidence.schema.json").read_text())
        == evidence.contract_document()
    )
    migration = _migration()
    signatures = {signature.replace(" ", "") for signature in migration._signatures()}
    for name, definition in execution.OPERATIONS.items():
        assert f"aap_assets_v1.{name}({','.join(definition.sql_types)})" in signatures
    assert not any(
        signature.split("(")[0] in migration._REPLACED for signature in signatures
    )


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
