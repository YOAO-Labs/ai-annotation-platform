"""Static checks for the bounded bulk image input-manifest migration."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import re
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from tests.factory import create_project
from tests.test_assets_catalog_native import _seed_actor

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0222_bulk_image_input_manifests.py"
SCHEMA = SQL_DIR / "0222_accepted_input_manifests.schema.json"
ARTIFACTS = (
    "0222_accepted_input_manifests_state.sql",
    "0222_accepted_input_manifests_authorities.sql",
    "0222_accepted_input_manifests_control.sql",
)
OPERATIONS = {
    "capture_accepted_image_input_manifest": (
        "workflow",
        "uuid,uuid,uuid,jsonb",
    ),
    "read_accepted_image_input_manifest": ("control", "uuid,uuid,uuid"),
    "read_accepted_image_input_manifest_page": (
        "control",
        "uuid,uuid,uuid,text,text,bigint,integer",
    ),
    "prepare_accepted_image_input_manifest_member": (
        "control",
        "uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text",
    ),
    "seal_accepted_image_input_manifest": ("control", "uuid,uuid,uuid,text"),
}
TABLES = {
    "asset_accepted_image_input_manifests",
    "asset_accepted_image_input_manifest_members",
}


def _constants(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        continue
    return result


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def test_0222_installer_is_after_frozen_0221_and_lists_all_artifacts():
    migration = _constants(MIGRATION)

    assert migration["_UPGRADE_ARTIFACTS"] == ARTIFACTS
    assert migration["_SCHEMA_ARTIFACTS"] == (
        "0222_accepted_input_manifests.schema.json",
    )
    assert all((SQL_DIR / name).is_file() for name in ARTIFACTS)


def test_0222_schema_freezes_only_the_five_manifest_capabilities():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$id"] == "aap-assets-input-manifest/v1"
    assert set(schema["x-native"]) == set(OPERATIONS)
    assert schema["x-budgets"] == {
        "request_bytes": 64 * 1024,
        "reply_bytes": 2 * 1024 * 1024,
        "page_size": 100,
        "explicit_task_limit": 200,
    }
    for name, (role, arguments) in OPERATIONS.items():
        operation = schema["x-native"][name]
        assert operation["role"] == role
        assert operation["signature"] == f"aap_assets_v1.{name}({arguments})"


def test_0222_revokes_every_new_function_and_streams_without_full_json_aggregation():
    source = "\n".join(
        (SQL_DIR / artifact).read_text(encoding="utf-8") for artifact in ARTIFACTS
    )
    created = set(
        re.findall(
            r"(?im)^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(",
            source,
        )
    )
    revoked = set(
        re.findall(
            r"(?im)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(",
            source,
        )
    )
    assert created <= revoked
    labeling = (SQL_DIR / "0222_accepted_input_manifests_authorities.sql").read_text(
        encoding="utf-8"
    )
    capture = labeling.split(
        "CREATE FUNCTION aap_labeling_v1.open_accepted_image_input_manifest_members(",
        maxsplit=1,
    )[1].split("$function$;", maxsplit=1)[0]
    assert "OPEN v_cursor FOR" in capture
    assert "jsonb_agg" not in capture
    assert "pg_catalog.row_number() OVER (ORDER BY eligible.id)" in capture
    assert "task_lock.expire_at > pg_catalog.clock_timestamp()" in capture
    control = (SQL_DIR / "0222_accepted_input_manifests_control.sql").read_text(
        encoding="utf-8"
    )
    page = control.split(
        "CREATE FUNCTION aap_assets_v1.read_accepted_image_input_manifest_page(",
        maxsplit=1,
    )[1].split("$function$;", maxsplit=1)[0]
    assert "octet_length(v_candidate_reply::text) > 2097152" in page
    assert "resource_budget_exceeded" in page


def test_0222_tables_are_native_managed_and_grants_stay_with_exact_owners():
    native_schema = (ROOT / "apps/api/app/db/native_schema.py").read_text()
    modules = (ROOT / "infra/docker/module-data-grants.sql").read_text()
    media = (ROOT / "infra/docker/assets-media-grants.sql").read_text()
    worker = (ROOT / "infra/docker/assets-image-grants.sql").read_text()
    workflow_capture = (
        "aap_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb)"
    )
    for table in TABLES:
        assert f'"{table}"' in native_schema
        assert f"public.{table}" in modules
        assert f"public.{table}" not in media
        assert f"public.{table}" not in worker
    assert workflow_capture in _compact(modules)
    assert all(
        f"aap_assets_v1.{name}({arguments.replace(', ', ',')})" in _compact(media)
        for name, (role, arguments) in OPERATIONS.items()
        if role == "control"
    )
    assert all(
        f"aap_assets_v1.{name}({arguments})" not in worker
        for name, (_role, arguments) in OPERATIONS.items()
    )


def test_0222_upgrade_fails_closed_on_unmanifested_active_bulk_image_jobs():
    # The pending Job payload does not freeze model recipes, so the cutover
    # conservatively requires draining every active non-scene image-project
    # bulk Job.
    migration = re.sub(r"\s+", " ", MIGRATION.read_text(encoding="utf-8"))
    assert "job.status IN ('pending', 'running')" in migration
    assert "project.data_type = 'image'" in migration
    assert "AND NOT project.scene_mode" in migration
    assert "COALESCE(job.payload ->> 'execution_scope', 'bulk') = 'bulk'" in migration
    assert "active legacy image " in migration
    assert "preannotation Jobs lack frozen input identity" in migration


@pytest.mark.asyncio
async def test_0222_upgrade_gate_rejects_live_legacy_bulk_image_job(test_engine):
    path = ROOT / "apps/api/alembic/versions/0222_bulk_image_input_manifests.py"
    spec = importlib.util.spec_from_file_location("bulk_image_manifest_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                actor_id = await _seed_actor(db)
                project = await create_project(db, owner_id=actor_id)
                job_id = uuid4()
                db.add(
                    AsyncJob(
                        id=job_id,
                        kind="batch_predict",
                        project_id=project.id,
                        user_id=actor_id,
                        status="pending",
                        celery_task_id=str(job_id),
                        payload={"execution_scope": "bulk"},
                        result={},
                    )
                )
                await db.flush()

            def run_upgrade(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.upgrade()

            with pytest.raises(RuntimeError, match="lack frozen input identity"):
                await connection.run_sync(run_upgrade)
        finally:
            await transaction.rollback()
