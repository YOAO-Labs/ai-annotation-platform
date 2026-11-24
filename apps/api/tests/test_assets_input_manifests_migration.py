"""Capability contracts and PostgreSQL cutover for bulk image input manifests."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import re
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.managed_image_inference_fixtures import _owned_database
from tests.migration_helpers import load

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
        "yoao_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb)"
    )
    for table in TABLES:
        assert f'"{table}"' in native_schema
        assert f"public.{table}" in modules
        assert f"public.{table}" not in media
        assert f"public.{table}" not in worker
    assert workflow_capture in _compact(modules)
    assert all(
        f"yoao_assets_v1.{name}({arguments.replace(', ', ',')})" in _compact(media)
        for name, (role, arguments) in OPERATIONS.items()
        if role == "control"
    )
    assert all(
        f"yoao_assets_v1.{name}({arguments})" not in worker
        for name, (_role, arguments) in OPERATIONS.items()
    )


@pytest.fixture
async def pre_manifest_database(test_db_url):
    admin = create_async_engine(test_db_url, poolclass=NullPool)
    try:
        async with _owned_database(admin) as engine:
            config = Config("alembic.ini")
            url = engine.url.render_as_string(hide_password=False).replace("%", "%%")
            config.set_main_option("sqlalchemy.url", url)
            await asyncio.to_thread(command.upgrade, config, "bo0221")
            yield engine, config
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_0222_upgrade_only_blocks_legacy_active_bulk_image_jobs(
    pre_manifest_database,
):
    engine, config = pre_manifest_database
    migration = load(MIGRATION.name)
    version = text("SELECT version_num FROM public.alembic_version")
    jobs = text("SELECT to_jsonb(job) FROM public.async_jobs AS job ORDER BY job.id")
    insert_job = text(
        "INSERT INTO public.async_jobs(id, kind, status, project_id, user_id, payload) "
        "VALUES (:id, :kind, :status, :project, :actor, :payload)"
    ).bindparams(bindparam("payload", type_=JSONB))

    def run_upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    async with engine.begin() as connection:
        assert await connection.scalar(version) == "bo0221"
        actor = uuid4()
        await connection.execute(
            text(
                "INSERT INTO public.users(id, email, name, password_hash, role) "
                "VALUES (:id, 'manifest-cutover@example.test', 'Cutover actor', "
                "'test-only', 'super_admin')"
            ),
            {"id": actor},
        )
        image, scene, video = (uuid4() for _ in range(3))
        await connection.execute(
            text(
                "INSERT INTO public.projects"
                "(id, display_id, name, type_label, type_key, "
                "owner_id, data_type, scene_mode) "
                "VALUES (:id, :name, :name, :type_key, :type_key, :actor, :data_type, :scene)"
            ),
            [
                {
                    "id": project,
                    "name": name,
                    "type_key": type_key,
                    "actor": actor,
                    "data_type": data_type,
                    "scene": scene_mode,
                }
                for project, name, type_key, data_type, scene_mode in (
                    (image, "P-image", "image-det", "image", False),
                    (scene, "P-scene", "image-det", "image", True),
                    (video, "P-video", "video-track", "video", False),
                )
            ],
        )

        bulk = {"execution_scope": "bulk"}
        for status, payload in (
            ("pending", bulk),
            ("running", bulk),
            ("pending", {}),
            ("running", {}),
            ("running", {**bulk, "accepted_input_manifest_protocol": "v0"}),
        ):
            with pytest.raises(RuntimeError, match="lack frozen input identity"):
                async with connection.begin_nested():
                    await connection.execute(
                        insert_job,
                        {
                            "id": uuid4(),
                            "kind": "batch_predict",
                            "status": status,
                            "project": image,
                            "actor": actor,
                            "payload": payload,
                        },
                    )
                    await connection.run_sync(run_upgrade)

        accepted = (
            ("batch_predict", "completed", image, bulk),
            ("batch_predict", "failed", image, {}),
            ("batch_predict", "cancelled", image, bulk),
            ("batch_predict", "running", image, {"execution_scope": "workbench"}),
            ("batch_predict", "running", scene, bulk),
            ("batch_predict", "running", video, bulk),
            ("export", "pending", image, bulk),
            (
                "batch_predict",
                "running",
                image,
                {
                    **bulk,
                    "accepted_input_manifest_protocol": "aap-assets-input-manifest/v1",
                },
            ),
        )
        await connection.execute(
            insert_job,
            [
                {
                    "id": uuid4(),
                    "kind": kind,
                    "status": status,
                    "project": project,
                    "actor": actor,
                    "payload": payload,
                }
                for kind, status, project, payload in accepted
            ],
        )
        before = (await connection.scalars(jobs)).all()
        assert len(before) == len(accepted)

    await asyncio.to_thread(command.upgrade, config, "bo0222")
    async with engine.connect() as connection:
        assert await connection.scalar(version) == "bo0222"
        assert (await connection.scalars(jobs)).all() == before
        for table in TABLES:
            assert await connection.scalar(
                text("SELECT to_regclass(:relation) IS NOT NULL"),
                {"relation": f"public.{table}"},
            )
