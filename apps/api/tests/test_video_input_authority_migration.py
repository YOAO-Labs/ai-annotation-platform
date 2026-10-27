"""Installer/public-schema checks for the new video owner projections."""

import importlib.util
import json
from pathlib import Path
import re

from aap_assets_video_inputs import authority as c

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql/0223_video_input_authorities.sql"
SCHEMA = SQL.with_suffix(".schema.json")
MIGRATION = ROOT / "apps/api/alembic/versions/0223_video_input_authorities.py"


def migration():
    spec = importlib.util.spec_from_file_location(
        "video_authority_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_video_projection_schema_matches_the_portable_artifact():
    assert json.loads(SCHEMA.read_text()) == c.contract_document()
    assert len(c.OPERATIONS) == 3
    assert all(op.consumer == "assets_owner" for op in c.OPERATIONS.values())


def test_new_authorities_have_reversible_installer_and_no_image_replacements():
    module = migration()

    assert module._UPGRADE_ARTIFACTS == (SQL.name,)
    statements = module._statements()
    source = SQL.read_text()
    created = set(re.findall(r"(?im)^CREATE FUNCTION ([a-z0-9_.]+)\s*\(", source))
    revoked = {signature.split("(", 1)[0] for signature in module._signatures()}
    assert created and created == revoked
    assert all(
        name.startswith(("aap_labeling_v1.", "aap_jobs_v1.")) for name in created
    )
    assert "CREATE OR REPLACE" not in source
    assert not any(
        re.match(r"(?is)^\s*(CREATE|ALTER|DROP)\s+TABLE", s) for s in statements
    )
    for name, definition in c.OPERATIONS.items():
        assert definition.namespace + "." + name in created


def test_rollback_uses_only_exact_restrict_drops():
    module = migration()
    emitted = []
    original = module.op.execute
    try:
        module.op.execute = emitted.append
        module.downgrade()
    finally:
        module.op.execute = original
    assert emitted == [
        f"DROP FUNCTION IF EXISTS {s} RESTRICT" for s in reversed(module._signatures())
    ]
