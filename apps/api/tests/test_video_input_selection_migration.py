"""Installer and public-contract checks for migration 0224."""

import importlib.util
import json
from pathlib import Path
import re

from aap_assets_video_inputs import selection as contract

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql/0224_video_input_selection.sql"
SCHEMA = SQL.with_suffix(".schema.json")
MIGRATION = ROOT / "apps/api/alembic/versions/0224_video_input_selection.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "video_selection_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_video_selection_artifact_matches_portable_contract():
    assert json.loads(SCHEMA.read_text()) == contract.contract_document()
    assert (
        json.loads(
            (
                ROOT
                / "apps/assets-media/src/aap_assets_video_inputs/selection.schema.json"
            ).read_text()
        )
        == contract.contract_document()
    )
    assert len(contract.OPERATIONS) == 2
    assert all(op.consumer == "assets_owner" for op in contract.OPERATIONS.values())


def test_video_selection_migration_installs_only_new_labeling_projections():
    module = _migration()
    source = SQL.read_text()
    statements = module._statements()
    created = set(re.findall(r"(?im)^CREATE FUNCTION ([a-z0-9_.]+)\s*\(", source))
    revoked = {signature.split("(", 1)[0] for signature in module._signatures()}

    assert module._UPGRADE_ARTIFACTS == (SQL.name,)
    assert created and created == revoked
    assert all(name.startswith("aap_labeling_v1.") for name in created)
    assert "CREATE OR REPLACE" not in source
    assert not any(
        re.match(r"(?is)^\s*(CREATE|ALTER|DROP)\s+TABLE", statement)
        for statement in statements
    )
    assert "aap_jobs_v1._video_input_selection_valid" not in source
    for name, definition in contract.OPERATIONS.items():
        assert "aap_labeling_v1." + name in created
        assert contract.contract_document()["x-native"][name]["signature"].startswith(
            "aap_labeling_v1."
        )


def test_video_selection_downgrade_uses_exact_restrict_drops():
    module = _migration()
    emitted = []
    original = module.op.execute
    try:
        module.op.execute = emitted.append
        module.downgrade()
    finally:
        module.op.execute = original
    assert emitted == [
        f"DROP FUNCTION IF EXISTS {signature} RESTRICT"
        for signature in reversed(module._signatures())
    ]
