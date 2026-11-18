"""Cold acceptance for Identity-backed image import and scale-seed selection."""

from __future__ import annotations

import asyncio
import builtins
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
from urllib.parse import urlsplit
import zlib

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _required_env,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_image_import_identity_profile import test_db_url as test_db_url  # noqa: F401
from tests.test_workspace_import_cli_live import (
    _IDENTITY_CONSUMER_FUNCTIONS,
    _assert_empty_owned_rows,
)
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_SEED_SELECTION_LIVE") != "1",
        reason="requires the explicitly enabled owned Identity seed-selection verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "apps/api/tests/fixtures/identity_seed_selection_probe.py"
_SCALE_ADMIN = "yoao_identity_v1.read_scale_seed_super_admin()"
_SCALE_ANNOTATOR = "yoao_identity_v1.read_scale_seed_annotator(uuid[])"
_SELECTION_FUNCTIONS = {
    _SCALE_ADMIN,
    _SCALE_ANNOTATOR,
    "yoao_identity_v1.read_first_seed_account()",
}
_IMAGE_IMPORT_FUNCTIONS = {
    "yoao_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)",
    "yoao_assets_v1.append_operator_items(uuid,uuid,jsonb)",
    "yoao_assets_v1.begin_operator_auxiliary(jsonb)",
    "yoao_assets_v1.open_operator_storage_operation(uuid)",
    "yoao_assets_v1.record_operator_original_prepared(uuid,jsonb)",
    "yoao_assets_v1.mark_operator_original_writing(uuid)",
    "yoao_assets_v1.record_operator_original_written(uuid,jsonb)",
    "yoao_assets_v1.record_operator_original_verified(uuid,jsonb)",
    "yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb)",
    "yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)",
    "yoao_assets_v1.publish_operator_item_original(jsonb)",
}
_PRIVATE_SCALE_FIXTURE_FUNCTIONS = (
    "yoao_workspace_v1.create_scale_seed_project(uuid,text)",
    "yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid)",
    "yoao_workspace_v1.guard_scale_seed_cleanup(text)",
    "yoao_workspace_v1.delete_scale_seed_projects(text,uuid[])",
    "yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)",
    "yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])",
    "yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)",
    "yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)",
    "yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)",
)


def _probe_result(run) -> dict:
    prefix = "AAP_IDENTITY_SEED_SELECTION_RESULT="
    matches = [
        line[len(prefix) :]
        for line in run.stdout.splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) == 1, run.evidence
    return json.loads(matches[0])


async def _function_set(db, role: str, schema: str) -> set[str]:
    return set(
        (
            await db.scalars(
                text(
                    "SELECT ns.nspname || '.' || proc.proname || '(' || "
                    "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                    "WHERE ns.nspname=:schema "
                    "AND pg_catalog.has_function_privilege(:role, proc.oid, 'EXECUTE')"
                ),
                {"schema": schema, "role": role},
            )
        ).all()
    )


async def _assert_identity_selection_profile(test_engine) -> None:
    _verify_postgres(test_engine.url)
    runtime_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert runtime_url.drivername == "postgresql+asyncpg"
    assert runtime_url.username == "legacy_runtime"
    assert (runtime_url.host, runtime_url.port, runtime_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert str(runtime_url.database).endswith("_test")
    assert runtime_url.host == "127.0.0.1" and not runtime_url.query
    runtime = create_async_engine(runtime_url, hide_parameters=True)
    try:
        async with runtime.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
            assert await db.scalar(
                text(
                    "SELECT oid::bigint FROM pg_catalog.pg_database "
                    "WHERE datname=current_database()"
                )
            ) == int(_required_env("AAP_MODULE_DATA_PG_DATABASE_OID"))
            flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,"
                        "rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            assert flags == (False, False, False, False, False)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_auth_members "
                        "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                        "WHERE rolname=current_user)"
                    )
                )
                == 0
            )
            assert await _function_set(db, "legacy_runtime", "yoao_identity_v1") == (
                set(_IDENTITY_CONSUMER_FUNCTIONS) | _SELECTION_FUNCTIONS
            )
            for signature in _IMAGE_IMPORT_FUNCTIONS:
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ),
                    {"signature": signature},
                ), signature
            for signature in _PRIVATE_SCALE_FIXTURE_FUNCTIONS:
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ),
                    {"signature": signature},
                ), signature
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'yoao_identity_v1.account_profiles', 'SELECT')"
                )
            )
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user,'public.users',"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                )
            )
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(current_user,'public.users',"
                    "'SELECT,INSERT,UPDATE,REFERENCES')"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'yoao_workspace_v1.project_member_facts', 'SELECT')"
                )
            )
    finally:
        await runtime.dispose()

    async with test_engine.connect() as db:
        assert await _function_set(
            db, "identity_api_consumer", "yoao_identity_v1"
        ) == set(_IDENTITY_CONSUMER_FUNCTIONS)
        for signature in _SELECTION_FUNCTIONS:
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege('identity_api_consumer', "
                    "CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature
        assert not await db.scalar(
            text(
                "SELECT pg_catalog.has_table_privilege('identity_api_consumer', "
                "'public.users','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
            )
        )
        assert not await db.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege('identity_api_consumer',"
                "'public.users','SELECT,INSERT,UPDATE,REFERENCES')"
            )
        )


async def _insert_selection_accounts(test_engine, suffix: str) -> dict[str, uuid.UUID]:
    """Create deterministic actor ordering and project duties for the selectors."""
    base = datetime(2020, 1, 1, tzinfo=UTC)
    accounts = {
        "import_first": ("viewer", True, 0),
        "admin_inactive_oldest": ("super_admin", False, 1),
        "admin_active_later": ("super_admin", True, 2),
        "annotator_inactive_oldest": ("employee", False, 3),
        "annotator_active_viewer": ("viewer", True, 4),
        "annotator_active_employee_later": ("employee", True, 5),
    }
    ids = {key: uuid.uuid4() for key in accounts}
    async with test_engine.begin() as db:
        for key, (role, active, day_offset) in accounts.items():
            await db.execute(
                text(
                    "INSERT INTO public.users "
                    "(id,email,name,password_hash,role,is_active,created_at) "
                    "VALUES (:id,:email,:name,'seed-selection-not-for-login',:role,"
                    ":active,:created_at)"
                ),
                {
                    "id": ids[key],
                    "email": f"selection-{key}-{suffix}@cold.test",
                    "name": key,
                    "role": role,
                    "active": active,
                    "created_at": base.replace(day=base.day + day_offset),
                },
            )

        source_project = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO public.projects "
                "(id,display_id,name,type_label,type_key,data_type,owner_id,total_tasks) "
                "VALUES (:id,:display_id,:name,'Image detection','image_detection',"
                "'image',:owner,0)"
            ),
            {
                "id": source_project,
                "display_id": f"P-SEL-{suffix[:12]}",
                "name": f"identity seed selection source {suffix}",
                "owner": ids["admin_inactive_oldest"],
            },
        )
        for key in (
            "annotator_inactive_oldest",
            "annotator_active_viewer",
            "annotator_active_employee_later",
        ):
            await db.execute(
                text(
                    "INSERT INTO public.project_members "
                    "(id,project_id,user_id,role,assigned_by) "
                    "VALUES (:id,:project,:user,'annotator',:assigned_by)"
                ),
                {
                    "id": uuid.uuid4(),
                    "project": source_project,
                    "user": ids[key],
                    "assigned_by": ids["admin_inactive_oldest"],
                },
            )
    return ids


async def _user_snapshot(test_engine) -> tuple[tuple[object, ...], ...]:
    async with test_engine.connect() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id,email,role,is_active,created_at FROM public.users "
                    "ORDER BY email"
                )
            )
        ).all()
    return tuple(tuple(row) for row in rows)


async def _scale_state(test_engine, run_id: uuid.UUID) -> dict:
    marker = f"scale-seed-{run_id.hex}"
    async with test_engine.connect() as db:
        project = (
            (
                await db.execute(
                    text("SELECT id,owner_id FROM public.projects WHERE name=:marker"),
                    {"marker": marker},
                )
            )
            .mappings()
            .first()
        )
        dataset = (
            (
                await db.execute(
                    text(
                        "SELECT id,created_by FROM public.datasets WHERE name=:marker"
                    ),
                    {"marker": marker},
                )
            )
            .mappings()
            .first()
        )
        project_id = None if project is None else project["id"]
        dataset_id = None if dataset is None else dataset["id"]
        counts = {
            "items": 0
            if dataset_id is None
            else await db.scalar(
                text("SELECT count(*) FROM public.dataset_items WHERE dataset_id=:id"),
                {"id": dataset_id},
            ),
            "batches": 0
            if project_id is None
            else await db.scalar(
                text("SELECT count(*) FROM public.task_batches WHERE project_id=:id"),
                {"id": project_id},
            ),
            "tasks": 0
            if project_id is None
            else await db.scalar(
                text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
                {"id": project_id},
            ),
            "annotations": 0
            if project_id is None
            else await db.scalar(
                text("SELECT count(*) FROM public.annotations WHERE project_id=:id"),
                {"id": project_id},
            ),
        }
        selected_members = []
        if project_id is not None:
            selected_members = list(
                (
                    await db.scalars(
                        text(
                            "SELECT user_id FROM public.project_members "
                            "WHERE project_id=:id AND role='annotator'"
                        ),
                        {"id": project_id},
                    )
                ).all()
            )
    return {
        "project": None
        if project is None
        else {key: project[key] for key in project.keys()},
        "dataset": None
        if dataset is None
        else {key: dataset[key] for key in dataset.keys()},
        **counts,
        "selected_members": selected_members,
    }


async def _scale_activity(test_engine, application_name: str, role: str) -> dict | None:
    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid,usename,application_name,host(client_addr) AS client_host,"
                        "state,wait_event_type,wait_event,"
                        "position('read_scale_seed_annotator' in query)>0 AS selector_call "
                        "FROM pg_catalog.pg_stat_activity WHERE datname=current_database() "
                        "AND usename=:role AND application_name=:tag"
                    ),
                    {"tag": application_name, "role": role},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _wait_for_selector_gate(
    test_engine,
    cli: WorkspaceImportCli,
    run_task: asyncio.Task,
    application_name: str,
) -> tuple[str, dict]:
    deadline = asyncio.get_running_loop().time() + 25
    while cli.container_id is None and not run_task.done():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("scale CLI did not create its owned container")
        await asyncio.sleep(0.02)
    if run_task.done():
        pytest.fail(f"scale CLI exited before its selector gate: {run_task.result()}")

    while asyncio.get_running_loop().time() < deadline:
        activity = await _scale_activity(
            test_engine, application_name, "legacy_runtime"
        )
        if (
            activity is not None
            and activity["state"] == "active"
            and activity["wait_event_type"] == "Timeout"
            and activity["wait_event"] == "PgSleep"
            and activity["selector_call"] is True
        ):
            address = await asyncio.to_thread(cli.running_database_client_address)
            assert activity["client_host"] == address
            return address, activity
        if run_task.done():
            pytest.fail(
                f"scale CLI exited before its selector gate: {run_task.result()}"
            )
        await asyncio.sleep(0.04)
    pytest.fail("scale CLI did not reach the owned Identity selector gate")


async def _wait_for_backend_exit(test_engine, backend_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        async with test_engine.connect() as db:
            alive = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=:pid)"
                ),
                {"pid": backend_pid},
            )
        if not alive:
            return
        await asyncio.sleep(0.05)
    pytest.fail("stopped scale-seed backend remains in pg_stat_activity")


async def _finish_cli_task(task: asyncio.Task) -> None:
    await asyncio.wait_for(asyncio.shield(task), timeout=35)


async def _install_selector_gate(test_engine, application_name: str) -> str:
    suffix = uuid.uuid4().hex[:12]
    original_name = f"read_scale_seed_annotator_original_{suffix}"
    wrapper = "yoao_identity_v1.read_scale_seed_annotator(uuid[])"
    original = f"yoao_identity_v1.{original_name}(uuid[])"
    body = f"""CREATE FUNCTION yoao_identity_v1.read_scale_seed_annotator(p_candidate_ids uuid[])
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $selector_gate$
BEGIN
    PERFORM pg_catalog.set_config('application_name', '{application_name}', true);
    PERFORM pg_catalog.pg_sleep(10);
    RETURN yoao_identity_v1.{original_name}(p_candidate_ids);
END
$selector_gate$"""
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "ALTER FUNCTION yoao_identity_v1.read_scale_seed_annotator(uuid[]) "
                f"RENAME TO {original_name}"
            )
        )
        await db.execute(text(body))
        await db.execute(text(f"REVOKE ALL ON FUNCTION {wrapper} FROM PUBLIC"))
        await db.execute(text(f"GRANT EXECUTE ON FUNCTION {wrapper} TO legacy_runtime"))
    return original


async def _remove_selector_gate(test_engine, original: str) -> None:
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "DROP FUNCTION IF EXISTS "
                "yoao_identity_v1.read_scale_seed_annotator(uuid[]) RESTRICT"
            )
        )
        await db.execute(
            text(f"ALTER FUNCTION {original} RENAME TO read_scale_seed_annotator")
        )


async def _install_late_annotation_failure(
    test_engine, annotator_id: uuid.UUID
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    function = f"public.identity_seed_selection_fail_{suffix}"
    trigger = f"identity_seed_selection_fail_{suffix}"
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE FUNCTION {function}() RETURNS trigger "
                "LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $failure$ "
                "BEGIN IF NEW.class_name='seed' AND NEW.user_id='"
                f"{annotator_id}'::uuid THEN RAISE EXCEPTION USING ERRCODE='P0001', "
                "MESSAGE='controlled scale-seed annotation failure'; END IF; "
                "RETURN NEW; END $failure$"
            )
        )
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.annotations "
                f"FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
        )
    return function, trigger


async def _drop_annotation_failure(test_engine, function: str, trigger: str) -> None:
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"DROP TRIGGER IF EXISTS {trigger.split('.')[-1]} ON public.annotations"
            )
        )
        await db.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))


async def _run_probe(cli: WorkspaceImportCli, operation: str):
    return await asyncio.to_thread(
        cli.run_command,
        ("python", f"/fixture/{PROBE.name}", operation),
    )


async def _run_scale(cli: WorkspaceImportCli, run_id: uuid.UUID, *, purge=False):
    args = ("--purge",) if purge else ("4", "2", "1")
    return await asyncio.to_thread(
        cli.run_command,
        ("python", "-m", "scripts.seed_scale", "--run-id", str(run_id), *args),
    )


async def _cleanup_scale_runs(cli: WorkspaceImportCli, test_engine, run_ids):
    from app.db.models.fixture_run import (
        FixtureRun,
        FixtureRunAdmission,
        FixtureRunResource,
    )

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    for run_id in run_ids:
        async with sessions() as db:
            run = await db.get(FixtureRun, run_id)
            state = None if run is None else run.state
        if state is not None and state != "retired":
            result = await _run_scale(cli, run_id, purge=True)
            if result.timed_out or result.exit_code != 0:
                raise RuntimeError(f"Exact scale cleanup blocked: {result.evidence}")
    async with sessions.begin() as db:
        runs = list(
            await db.scalars(
                select(FixtureRun).where(FixtureRun.id.in_(run_ids)).with_for_update()
            )
        )
        if any(run.state != "retired" for run in runs):
            raise RuntimeError("A scale fixture run is not retired")
        from tests.seed_owner_support import private_seed_owner

        async with private_seed_owner() as owner:
            await owner.shutdown_authority(run_ids[-1])
        await db.execute(
            delete(FixtureRunResource).where(FixtureRunResource.run_id.in_(run_ids))
        )
        await db.execute(
            delete(FixtureRunAdmission).where(FixtureRunAdmission.run_id.in_(run_ids))
        )
        await db.execute(delete(FixtureRun).where(FixtureRun.id.in_(run_ids)))


def _scale_result_ids(stdout: str) -> tuple[uuid.UUID, uuid.UUID]:
    project_match = re.search(r"^\s*project_id\s*=\s*([0-9a-f-]{36})\s*$", stdout, re.M)
    annotator_match = re.search(
        r"^\s*annotator_id\s*=\s*([0-9a-f-]{36})\s*$", stdout, re.M
    )
    assert project_match and annotator_match, stdout
    return uuid.UUID(project_match.group(1)), uuid.UUID(annotator_match.group(1))


async def _image_import(cli: WorkspaceImportCli, image_dir: str, name: str):
    return await asyncio.to_thread(
        cli.run_command,
        (
            "python",
            "-m",
            "scripts.import_images",
            image_dir,
            "--name",
            name,
        ),
    )


async def _import_state(test_engine, name: str) -> dict:
    async with test_engine.connect() as db:
        datasets = (
            (
                await db.execute(
                    text(
                        "SELECT id,name,file_count,created_by FROM public.datasets "
                        "WHERE name=:name ORDER BY created_at,id"
                    ),
                    {"name": name},
                )
            )
            .mappings()
            .all()
        )
        items = (
            (
                await db.execute(
                    text(
                        "SELECT i.dataset_id,i.file_name,i.file_path,i.file_size,i.file_type,"
                        "i.content_hash,i.metadata->>'storage_relative_path' AS relative_path,"
                        "c.id AS creation_id,c.bucket,c.object_key,c.version_id,c.sha256,"
                        "c.status,u.state AS usage_state,u.physical_outcome "
                        "FROM public.dataset_items i "
                        "JOIN public.dataset_assets a ON a.dataset_item_id=i.id "
                        "JOIN public.dataset_asset_locations l ON l.asset_id=a.id "
                        "AND l.role='managed_original' "
                        "JOIN public.managed_original_creations c ON c.id=l.creation_id "
                        "LEFT JOIN public.storage_usage_mutations u ON u.owner_id=c.id "
                        "AND u.producer='managed_original_creation' "
                        "WHERE i.dataset_id=ANY(CAST(:ids AS uuid[])) "
                        "ORDER BY i.dataset_id,i.file_name"
                    ),
                    {"ids": [row["id"] for row in datasets]},
                )
            )
            .mappings()
            .all()
            if datasets
            else []
        )
    return {
        "datasets": [dict(row) for row in datasets],
        "items": [dict(row) for row in items],
    }


def _bucket_versions(owner_s3, bucket: str, prefix: str) -> list[dict]:
    result = []
    for page in owner_s3.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=prefix + "/"
    ):
        result.extend(
            {
                "key": item["Key"],
                "version_id": item["VersionId"],
                "latest": item["IsLatest"],
            }
            for item in page.get("Versions", [])
        )
        result.extend(
            {
                "key": item["Key"],
                "version_id": item["VersionId"],
                "latest": item["IsLatest"],
            }
            for item in page.get("DeleteMarkers", [])
        )
    return result


def _read_object(owner_s3, bucket: str, key: str, version_id: str) -> bytes:
    response = owner_s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    assert response["VersionId"] == version_id
    body = response["Body"]
    try:
        return body.read()
    finally:
        body.close()


def _assert_imported_objects(owner_s3, bucket, dataset_id, items, expected):
    assert {item["file_name"] for item in items} == set(expected)
    assert len(items) == len(expected)
    for item in items:
        filename = item["file_name"]
        content = expected[filename]
        parts = item["object_key"].split("/")
        assert len(parts) == 5
        assert parts[:3] == ["__aap_managed_datasets__", str(dataset_id), "_objects"]
        assert str(uuid.UUID(parts[3])) == parts[3]
        assert parts[3] == str(item["creation_id"])
        assert parts[4] == filename
        assert item["dataset_id"] == dataset_id
        assert item["relative_path"] == filename
        assert item["file_path"] == item["object_key"]
        assert item["file_size"] == len(content) and item["file_type"] == "image"
        assert item["bucket"] == bucket and item["status"] == "verified"
        assert (
            item["usage_state"] == "confirmed" and item["physical_outcome"] == "written"
        )
        assert (
            item["content_hash"]
            == item["sha256"]
            == hashlib.sha256(content).hexdigest()
        )
        assert item["version_id"] not in (None, "", "null")
        assert (
            _read_object(owner_s3, bucket, item["object_key"], item["version_id"])
            == content
        )


def _png_pixel(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        data = kind + payload
        return (
            len(payload).to_bytes(4, "big")
            + data
            + (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    header = (1).to_bytes(4, "big") * 2 + bytes((8, 6, 0, 0, 0))
    pixel = zlib.compress(bytes((0, red, green, blue, 255)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixel)
        + chunk(b"IEND", b"")
    )


async def test_installed_identity_seed_selection_consumers(
    test_engine, tmp_path, record_property, monkeypatch
):
    from app.config import settings

    _verify_postgres(test_engine.url)
    await _assert_identity_selection_profile(test_engine)
    await _assert_empty_owned_rows(test_engine)
    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    assert owner_s3.list_objects_v2(Bucket=bucket).get("Contents", []) == []
    from scripts.prepare_original_catalog import main as prepare_original_catalog

    # Owner provisioning is separate from the installed consumer's environment.
    with monkeypatch.context() as owner_env:
        for key, value in {
            "DATABASE_URL": test_engine.url.render_as_string(hide_password=False),
            "MINIO_ENDPOINT": settings.minio_endpoint,
            "MINIO_USE_SSL": str(settings.minio_use_ssl).lower(),
            "MINIO_DATASETS_BUCKET": bucket,
            "MINIO_ACCESS_KEY": settings.minio_access_key,
            "MINIO_SECRET_KEY": settings.minio_secret_key,
        }.items():
            owner_env.setenv(key, value)
        await prepare_original_catalog()
    assert owner_s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"

    source_root = tmp_path / "identity-seed-selection-fixture"
    source_root.mkdir()
    source_root.chmod(0o755)
    shutil.copyfile(PROBE, source_root / PROBE.name)
    (source_root / PROBE.name).chmod(0o644)
    image_dir = source_root / "images"
    image_dir.mkdir()
    image_dir.chmod(0o755)
    image_bytes = {
        "first.png": _png_pixel(220, 50, 40),
        "second.png": _png_pixel(30, 120, 220),
    }
    for name, content in image_bytes.items():
        (image_dir / name).write_bytes(content)
        (image_dir / name).chmod(0o644)

    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )
    access_key = _required_env("AAP_IMAGE_IMPORT_S3_ACCESS_KEY")
    secret_key = _required_env("AAP_IMAGE_IMPORT_S3_SECRET_KEY")
    assert access_key != settings.minio_access_key
    assert secret_key != settings.minio_secret_key
    writer_s3 = _local_s3_client(settings, access_key, secret_key)
    prefix = "__aap_managed_datasets__"
    cli = None
    owner_cli = None
    run_ids = {
        key: uuid.uuid4()
        for key in (
            "first",
            "second",
            "denied",
            "late",
            "recovered",
            "interrupted",
            "recovery",
        )
    }
    pending_gate = None
    gate_original = None
    failure_trigger = None
    try:
        cli = WorkspaceImportCli(
            test_engine,
            tmp_path,
            source_root=source_root,
            buckets=resources["buckets"],
            minio_authority=f"{authority_host}:{endpoint.port or 9000}",
            minio_use_ssl=endpoint.scheme == "https",
            s3_access_key=access_key,
            s3_secret_key=secret_key,
            managed_original_writer=True,
        )
        await asyncio.to_thread(cli.start)
        await _assert_identity_selection_profile(test_engine)

        missing_name = f"selection-empty-{uuid.uuid4().hex[:10]}"
        missing = await _image_import(cli, "/fixture/images", missing_name)
        record_property("identity_seed_import_without_users", missing.evidence)
        assert not missing.timed_out and missing.exit_code == 0, missing.evidence
        async with test_engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM public.users")) == 0
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.datasets WHERE name=:name"),
                    {"name": missing_name},
                )
                == 0
            )
        assert (
            owner_s3.list_objects_v2(Bucket=bucket, Prefix=prefix + "/").get(
                "Contents", []
            )
            == []
        )

        suffix = uuid.uuid4().hex[:12]
        accounts = await _insert_selection_accounts(test_engine, suffix)
        users_before_scale = await _user_snapshot(test_engine)

        guard_run = await _run_probe(cli, "production_guard")
        record_property("identity_seed_scale_production_guard", guard_run.evidence)
        assert not guard_run.timed_out and guard_run.exit_code == 0, guard_run.evidence
        guard = _probe_result(guard_run)
        assert guard["exit_code"] == 2, guard

        import_name = f"identity seed images {suffix}"
        first_import = await _image_import(cli, "/fixture/images", import_name)
        record_property("identity_seed_import_first", first_import.evidence)
        assert not first_import.timed_out and first_import.exit_code == 0, (
            first_import.evidence
        )
        first_state = await _import_state(test_engine, import_name)
        assert len(first_state["datasets"]) == 1
        first_dataset = first_state["datasets"][0]
        assert first_dataset["file_count"] == 2
        assert first_dataset["created_by"] in set(accounts.values())
        assert len(first_state["items"]) == 2
        _assert_imported_objects(
            writer_s3, bucket, first_dataset["id"], first_state["items"], image_bytes
        )

        old_image_bytes = dict(image_bytes)
        image_bytes = {
            "first.png": _png_pixel(70, 200, 80),
            "second.png": _png_pixel(240, 180, 20),
        }
        for name, content in image_bytes.items():
            (image_dir / name).write_bytes(content)
        second_import = await _image_import(cli, "/fixture/images", import_name)
        record_property("identity_seed_import_repeat", second_import.evidence)
        assert not second_import.timed_out and second_import.exit_code == 0, (
            second_import.evidence
        )
        repeated = await _import_state(test_engine, import_name)
        assert len(repeated["datasets"]) == 2
        assert len({row["id"] for row in repeated["datasets"]}) == 2
        assert all(row["file_count"] == 2 for row in repeated["datasets"])
        assert all(
            row["created_by"] in set(accounts.values()) for row in repeated["datasets"]
        )
        assert len(repeated["items"]) == 4
        original_items = [
            item
            for item in repeated["items"]
            if item["dataset_id"] == first_dataset["id"]
        ]
        assert original_items == first_state["items"]
        second_dataset = next(
            dataset
            for dataset in repeated["datasets"]
            if dataset["id"] != first_dataset["id"]
        )
        second_items = [
            item
            for item in repeated["items"]
            if item["dataset_id"] == second_dataset["id"]
        ]
        _assert_imported_objects(
            writer_s3, bucket, first_dataset["id"], original_items, old_image_bytes
        )
        _assert_imported_objects(
            writer_s3, bucket, second_dataset["id"], second_items, image_bytes
        )
        keys = {item["object_key"] for item in repeated["items"]}
        assert len(keys) == 4
        versions = _bucket_versions(owner_s3, bucket, prefix)
        assert {(row["key"], row["version_id"]) for row in versions} == {
            (item["object_key"], item["version_id"]) for item in repeated["items"]
        }
        assert len(versions) == 4 and all(row["latest"] for row in versions)
        record_property("identity_seed_import_immutable_versions", versions)

        owner_cli = WorkspaceImportCli(
            test_engine,
            tmp_path,
            source_root=source_root,
            buckets=resources["buckets"],
            minio_authority=f"{authority_host}:{endpoint.port or 9000}",
            minio_use_ssl=endpoint.scheme == "https",
            s3_access_key=access_key,
            s3_secret_key=secret_key,
            fixture_family="scale",
            fixture_run_ids=frozenset(run_ids.values()),
        )
        await asyncio.to_thread(owner_cli.start)
        expected_admin = accounts["admin_inactive_oldest"]
        expected_annotator = accounts["annotator_active_viewer"]
        first_scale = await _run_scale(owner_cli, run_ids["first"])
        record_property("identity_seed_scale_first", first_scale.evidence)
        assert not first_scale.timed_out and first_scale.exit_code == 0, (
            first_scale.evidence
        )
        first_project, first_annotator = _scale_result_ids(first_scale.stdout)
        assert first_annotator == expected_annotator
        first_scale_state = await _scale_state(test_engine, run_ids["first"])
        assert first_project == first_scale_state["project"]["id"]
        assert first_scale_state["project"]["owner_id"] == expected_admin
        assert first_scale_state["dataset"]["created_by"] == expected_admin
        assert first_scale_state["selected_members"] == [expected_annotator]
        assert {
            key: first_scale_state[key]
            for key in ("items", "batches", "tasks", "annotations")
        } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 1}
        assert await _user_snapshot(test_engine) == users_before_scale

        repeated_scale = await _run_scale(owner_cli, run_ids["second"])
        record_property("identity_seed_scale_repeat", repeated_scale.evidence)
        assert not repeated_scale.timed_out and repeated_scale.exit_code == 0, (
            repeated_scale.evidence
        )
        second_project, second_annotator = _scale_result_ids(repeated_scale.stdout)
        second_scale_state = await _scale_state(test_engine, run_ids["second"])
        assert (
            second_project == second_scale_state["project"]["id"]
            and second_annotator == expected_annotator
        )
        assert second_scale_state["project"]["owner_id"] == expected_admin
        assert second_scale_state["dataset"]["created_by"] == expected_admin
        assert second_scale_state["project"]["id"] != first_scale_state["project"]["id"]
        assert second_scale_state["dataset"]["id"] != first_scale_state["dataset"]["id"]
        assert {
            key: second_scale_state[key]
            for key in ("items", "batches", "tasks", "annotations")
        } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 1}
        assert await _user_snapshot(test_engine) == users_before_scale
        assert await _scale_state(test_engine, run_ids["first"]) == first_scale_state

        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"REVOKE EXECUTE ON FUNCTION {_SCALE_ANNOTATOR} FROM legacy_runtime"
                )
            )
        try:
            runtime_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
            runtime = create_async_engine(runtime_url, hide_parameters=True)
            try:
                async with runtime.connect() as db:
                    with pytest.raises(DBAPIError) as forbidden:
                        await db.scalar(
                            text(
                                "SELECT yoao_identity_v1.read_scale_seed_annotator(ARRAY[]::uuid[])"
                            )
                        )
                    assert forbidden.value.orig.sqlstate == "42501"
            finally:
                await runtime.dispose()
            denied = await _run_scale(owner_cli, run_ids["denied"])
            record_property("identity_seed_scale_selector_denied", denied.evidence)
            assert not denied.timed_out and denied.exit_code != 0, denied.evidence
            assert await _scale_state(test_engine, run_ids["denied"]) == {
                "project": None,
                "dataset": None,
                "items": 0,
                "batches": 0,
                "tasks": 0,
                "annotations": 0,
                "selected_members": [],
            }
            async with test_engine.connect() as db:
                admissions = (
                    await db.execute(
                        text(
                            "SELECT kind,state FROM yoao_fixture_v1.fixture_run_admissions "
                            "WHERE run_id=:run_id"
                        ),
                        {"run_id": run_ids["denied"]},
                    )
                ).all()
                assert admissions == [("scale_seed", "settled")]
            assert await _user_snapshot(test_engine) == users_before_scale
            assert (
                await _scale_state(test_engine, run_ids["first"]) == first_scale_state
            )
            assert (
                await _scale_state(test_engine, run_ids["second"]) == second_scale_state
            )
        finally:
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION {_SCALE_ANNOTATOR} "
                        "TO legacy_runtime"
                    )
                )

        late_trigger = await _install_late_annotation_failure(
            test_engine, expected_annotator
        )
        failure_trigger = late_trigger
        try:
            late = await _run_scale(owner_cli, run_ids["late"])
            record_property(
                "identity_seed_scale_late_annotation_failure", late.evidence
            )
            assert not late.timed_out and late.exit_code != 0, late.evidence
            partial = await _scale_state(test_engine, run_ids["late"])
            assert partial["project"] is not None and partial["dataset"] is not None
            assert {
                key: partial[key]
                for key in ("items", "batches", "tasks", "annotations")
            } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 0}
            assert await _user_snapshot(test_engine) == users_before_scale
        finally:
            await _drop_annotation_failure(test_engine, *late_trigger)
            failure_trigger = None
        late_cleanup = await _run_scale(owner_cli, run_ids["late"], purge=True)
        assert not late_cleanup.timed_out and late_cleanup.exit_code == 0, (
            late_cleanup.evidence
        )
        assert (await _scale_state(test_engine, run_ids["late"]))["project"] is None
        assert await _scale_state(test_engine, run_ids["first"]) == first_scale_state
        assert await _scale_state(test_engine, run_ids["second"]) == second_scale_state

        recovered = await _run_scale(owner_cli, run_ids["recovered"])
        record_property("identity_seed_scale_late_failure_recovery", recovered.evidence)
        assert not recovered.timed_out and recovered.exit_code == 0, recovered.evidence
        recovered_project, recovered_annotator = _scale_result_ids(recovered.stdout)
        recovered_state = await _scale_state(test_engine, run_ids["recovered"])
        assert (
            recovered_project == recovered_state["project"]["id"]
            and recovered_annotator == expected_annotator
        )
        assert recovered_state["project"]["owner_id"] == expected_admin
        assert recovered_state["dataset"]["created_by"] == expected_admin
        assert {
            key: recovered_state[key]
            for key in ("items", "batches", "tasks", "annotations")
        } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 1}

        application_name = f"aap-scale-seed-{uuid.uuid4().hex[:32]}"
        gate_original = await _install_selector_gate(test_engine, application_name)
        pending_gate = asyncio.create_task(
            _run_scale(owner_cli, run_ids["interrupted"])
        )
        try:
            client_address, activity = await _wait_for_selector_gate(
                test_engine, owner_cli, pending_gate, application_name
            )
            backend_pid = activity["pid"]
            assert activity["usename"] == "legacy_runtime"
            assert activity["application_name"] == application_name
            assert activity["client_host"] == client_address
            assert activity["selector_call"] is True
            record_property(
                "identity_seed_scale_physical_cancellation_gate",
                {
                    "role": activity["usename"],
                    "application_name": activity["application_name"],
                    "client_host": activity["client_host"],
                    "backend_pid": backend_pid,
                    "wait_event_type": activity["wait_event_type"],
                    "wait_event": activity["wait_event"],
                    "selector_call": activity["selector_call"],
                },
            )
            assert await asyncio.to_thread(owner_cli.stop_current_container) is True
            stopped = await asyncio.wait_for(asyncio.shield(pending_gate), timeout=35)
            record_property(
                "identity_seed_scale_physical_cancellation", stopped.evidence
            )
            assert stopped.exit_code in {137, 143}, stopped.evidence
            assert stopped.externally_stopped is True
            assert stopped.timed_out is False
            await _wait_for_backend_exit(test_engine, backend_pid)
            assert await _scale_state(test_engine, run_ids["interrupted"]) == {
                "project": None,
                "dataset": None,
                "items": 0,
                "batches": 0,
                "tasks": 0,
                "annotations": 0,
                "selected_members": [],
            }
            blocked_cleanup = await _run_scale(
                owner_cli, run_ids["interrupted"], purge=True
            )
            assert not blocked_cleanup.timed_out and blocked_cleanup.exit_code != 0, (
                blocked_cleanup.evidence
            )
            assert "fixture_run_unsettled" in blocked_cleanup.stderr
            from app.db.models.fixture_run import FixtureRunAdmission
            from app.services import fixture_run_ownership

            monkeypatch.setattr(settings, "environment", "development")
            monkeypatch.setattr(settings, "e2e_seed_enabled", True)
            async with async_sessionmaker(
                test_engine, expire_on_commit=False
            ).begin() as db:
                admission = await db.scalar(
                    select(FixtureRunAdmission).where(
                        FixtureRunAdmission.run_id == run_ids["interrupted"]
                    )
                )
                assert admission is not None and admission.state == "active"
                await fixture_run_ownership.settle(
                    db, run_ids["interrupted"], admission.id
                )
            cleared = await _run_scale(owner_cli, run_ids["interrupted"], purge=True)
            assert not cleared.timed_out and cleared.exit_code == 0, cleared.evidence
            assert (
                await _scale_state(test_engine, run_ids["first"]) == first_scale_state
            )
            assert (
                await _scale_state(test_engine, run_ids["second"]) == second_scale_state
            )
            assert await _user_snapshot(test_engine) == users_before_scale
        finally:
            await _finish_cli_task(pending_gate)
            if gate_original is not None:
                await _remove_selector_gate(test_engine, gate_original)
                gate_original = None

        recovery = await _run_scale(owner_cli, run_ids["recovery"])
        record_property("identity_seed_scale_cancellation_recovery", recovery.evidence)
        assert not recovery.timed_out and recovery.exit_code == 0, recovery.evidence
        recovery_project, recovery_annotator = _scale_result_ids(recovery.stdout)
        recovery_state = await _scale_state(test_engine, run_ids["recovery"])
        assert (
            recovery_project == recovery_state["project"]["id"]
            and recovery_annotator == expected_annotator
        )
        assert recovery_state["project"]["owner_id"] == expected_admin
        assert recovery_state["dataset"]["created_by"] == expected_admin
        assert {
            key: recovery_state[key]
            for key in ("items", "batches", "tasks", "annotations")
        } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 1}
        assert await _user_snapshot(test_engine) == users_before_scale
        record_property(
            "identity_seed_selection_scope",
            "Qualified installed image import and scale-seed Identity selectors, "
            "including selection ordering, ordinary API denial, database stages, "
            "and owned-process recovery. Broader scale capacity and full seed "
            "workflows remain outside this case.",
        )
    finally:
        original = sys.exception()
        cleanup_failures = []

        async def cleanup(action):
            try:
                await action()
            except BaseException as error:
                cleanup_failures.append(error)

        if pending_gate is not None:
            await cleanup(lambda: _finish_cli_task(pending_gate))
        if gate_original is not None:
            await cleanup(lambda: _remove_selector_gate(test_engine, gate_original))
        if failure_trigger is not None:
            await cleanup(
                lambda: _drop_annotation_failure(test_engine, *failure_trigger)
            )
        if owner_cli is not None:
            if owner_cli.container_id is not None:
                cleanup_failures.append(
                    RuntimeError(
                        "Scale owner process still running; exact run anchors retained"
                    )
                )
            elif owner_cli.network_id is not None:
                owned_ids = list(run_ids.values())
                await cleanup(
                    lambda: _cleanup_scale_runs(owner_cli, test_engine, owned_ids)
                )
            await cleanup(lambda: asyncio.to_thread(owner_cli.close))
        if cli is not None:
            await cleanup(lambda: asyncio.to_thread(cli.close))
        await cleanup(lambda: asyncio.to_thread(writer_s3.close))
        if cleanup_failures:
            if original is not None:
                cleanup_failures.insert(0, original)
            raise builtins.BaseExceptionGroup(
                "Identity seed verifier or its exact cleanup failed", cleanup_failures
            ) from None
