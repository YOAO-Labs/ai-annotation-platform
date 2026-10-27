"""Installer and rollback checks for Scene-mode image manifests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "apps/api/alembic/sql"
SQL = SQL_DIR / "0227_scene_image_input_manifests.sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0227_scene_image_input_manifests.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "scene_image_input_manifest_migration", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(filename: str):
    return [
        statement.strip()
        for statement in (SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
        if statement.strip()
    ]


def test_0227_installer_replaces_only_the_frozen_assets_scope_helper():
    migration = _migration()

    assert migration._UPGRADE_ARTIFACTS == (SQL.name,)

    statements = _statements(SQL.name)
    assert len(statements) == 2
    assert statements[0].lstrip().startswith("--")
    assert "CREATE OR REPLACE FUNCTION " in statements[0]
    assert (
        statements[0]
        .split("CREATE OR REPLACE FUNCTION ", 1)[1]
        .startswith("aap_assets_v1._accepted_image_input_manifest_current_scope(")
    )
    assert statements[1].startswith(
        "REVOKE ALL ON FUNCTION "
        "aap_assets_v1._accepted_image_input_manifest_current_scope(uuid, uuid)"
    )
    assert not any(
        re.match(r"(?is)^\s*(CREATE|ALTER|DROP)\s+TABLE", statement)
        for statement in statements
    )
    helper = statements[0]
    assert "v_project ->> 'data_type' IS DISTINCT FROM 'image'" in helper
    assert "v_project -> 'scene_mode'" not in helper
    assert "read_media_project_access(p_actor_id, p_project_id)" in helper


def test_0227_gate_counts_only_unmanifested_ordinary_scene_image_bulk_jobs():
    migration = _migration()
    query = re.sub(r"\s+", " ", migration._LEGACY_SCENE_IMAGE_JOB_COUNT)
    assert "job.status IN ('pending', 'running')" in query
    assert "project.data_type = 'image'" in query
    assert "project.scene_mode" in query
    assert "COALESCE(job.payload ->> 'execution_scope', 'bulk') = 'bulk'" in query
    assert (
        "job.payload ->> 'accepted_input_manifest_protocol' "
        "IS DISTINCT FROM 'aap-assets-input-manifest/v1'"
    ) in query
    assert "NOT IN ('frame', 'video', 'scene')" in query
    assert "NOT IN ('text', 'tracker')" in query
    assert "stage #>> '{source,execution_unit}' IN ('frame', 'video', 'scene')" in query
    assert "jsonb_array_elements" in query
    assert (
        "Drain or explicitly fail those Scene-image Jobs first."
        in MIGRATION.read_text(encoding="utf-8")
    )


def test_0227_upgrade_fails_before_install_when_legacy_scene_jobs_remain(monkeypatch):
    migration = _migration()

    class Result:
        def scalar_one(self):
            return 2

    class Bind:
        def execute(self, _statement):
            return Result()

    emitted = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(migration.op, "execute", emitted.append)

    with pytest.raises(
        RuntimeError,
        match="active legacy or ambiguous bulk Jobs remain: 2",
    ):
        migration.upgrade()
    assert emitted == []


@pytest.mark.parametrize("count", [1, 0])
def test_0227_downgrade_refuses_manifests_the_old_helper_cannot_read(
    monkeypatch, count
):
    migration = _migration()

    class Result:
        def scalar_one(self):
            return count

    class Bind:
        def execute(self, _statement):
            return Result()

    emitted = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(migration.op, "execute", emitted.append)

    if count:
        with pytest.raises(
            RuntimeError, match="retained Scene-mode image manifests remain: 1"
        ):
            migration.downgrade()
        assert emitted == []
        return

    migration.downgrade()
    frozen = _statements("0222_accepted_input_manifests_control.sql")
    old_definition = next(
        statement
        for statement in frozen
        if statement.startswith(
            "CREATE FUNCTION "
            "aap_assets_v1._accepted_image_input_manifest_current_scope("
        )
    )
    old_revoke = next(
        statement
        for statement in frozen
        if statement.startswith(
            "REVOKE ALL ON FUNCTION "
            "aap_assets_v1._accepted_image_input_manifest_current_scope(uuid, uuid)"
        )
    )
    assert [str(statement) for statement in emitted] == [
        old_definition.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1),
        old_revoke,
    ]
