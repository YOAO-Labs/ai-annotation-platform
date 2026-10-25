"""Cold acceptance for Identity-backed image import and scale-seed selection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import urlsplit
import zlib

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import (
    _verify_bucket_owner,
    _verify_postgres,
)
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
_SCALE_ADMIN = "aap_identity_v1.read_scale_seed_super_admin()"
_SCALE_ANNOTATOR = "aap_identity_v1.read_scale_seed_annotator(uuid[])"
_SELECTION_FUNCTIONS = {_SCALE_ADMIN, _SCALE_ANNOTATOR}
_IMAGE_IMPORT_FUNCTIONS = {
    "aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)",
    "aap_assets_v1.append_operator_items(uuid,uuid,jsonb)",
}
_SCALE_MARKER = "scale-seed"
_IMPORT_IMAGE_NAMES = ("first.png", "second.png")


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
    runtime = create_async_engine(runtime_url, hide_parameters=True)
    try:
        async with runtime.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
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
            assert await _function_set(db, "legacy_runtime", "aap_identity_v1") == (
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
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'aap_identity_v1.account_profiles', 'SELECT')"
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
                    "'aap_workspace_v1.project_member_facts', 'SELECT')"
                )
            )
    finally:
        await runtime.dispose()

    async with test_engine.connect() as db:
        assert await _function_set(
            db, "identity_api_consumer", "aap_identity_v1"
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


def _create_dataset_storage_scope(resources, owner_s3, bucket: str, settings):
    """Reuse the owned disposable principal with bucket-head and prefix access."""
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=owner_s3)
    )
    scope["bucket"] = bucket
    prefix = f"__aap_trusted_nuscenes__-{uuid.uuid4().hex}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:DeleteObject",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
        ],
    }
    try:
        _cleanup_mc(
            scope,
            [
                "admin",
                "user",
                "add",
                "scoped",
                scope["access_key"],
                scope["secret_key"],
            ],
        )
        scope["user_created"] = True
        _cleanup_mc(
            scope,
            ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
            payload=json.dumps(policy),
        )
        scope["policy_created"] = True
        _cleanup_mc(
            scope,
            [
                "admin",
                "policy",
                "attach",
                "scoped",
                scope["policy"],
                "--user",
                scope["access_key"],
            ],
        )
        return scope, _destroy_cleanup_storage_scope, prefix
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


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


async def _scale_state(test_engine) -> dict:
    async with test_engine.connect() as db:
        project = (
            (
                await db.execute(
                    text("SELECT id,owner_id FROM public.projects WHERE name=:marker"),
                    {"marker": _SCALE_MARKER},
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
                    {"marker": _SCALE_MARKER},
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


async def _scale_activity(test_engine, application_name: str) -> dict | None:
    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid,usename,application_name,host(client_addr) AS client_host,"
                        "state,wait_event_type,wait_event,"
                        "position('read_scale_seed_annotator' in query)>0 AS selector_call "
                        "FROM pg_catalog.pg_stat_activity WHERE datname=current_database() "
                        "AND usename='legacy_runtime' AND application_name=:tag"
                    ),
                    {"tag": application_name},
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
        activity = await _scale_activity(test_engine, application_name)
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
    wrapper = "aap_identity_v1.read_scale_seed_annotator(uuid[])"
    original = f"aap_identity_v1.{original_name}(uuid[])"
    body = f"""CREATE FUNCTION aap_identity_v1.read_scale_seed_annotator(p_candidate_ids uuid[])
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $selector_gate$
BEGIN
    PERFORM pg_catalog.set_config('application_name', '{application_name}', true);
    PERFORM pg_catalog.pg_sleep(10);
    RETURN aap_identity_v1.{original_name}(p_candidate_ids);
END
$selector_gate$"""
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "ALTER FUNCTION aap_identity_v1.read_scale_seed_annotator(uuid[]) "
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
                "aap_identity_v1.read_scale_seed_annotator(uuid[]) RESTRICT"
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


async def _run_scale(cli: WorkspaceImportCli):
    return await asyncio.to_thread(
        cli.run_command,
        ("python", "-m", "scripts.seed_scale", "4", "2", "1"),
    )


def _scale_result_ids(stdout: str) -> tuple[uuid.UUID, uuid.UUID]:
    project_match = re.search(r"^\s*project_id\s*=\s*([0-9a-f-]{36})\s*$", stdout, re.M)
    annotator_match = re.search(
        r"^\s*annotator_id\s*=\s*([0-9a-f-]{36})\s*$", stdout, re.M
    )
    assert project_match and annotator_match, stdout
    return uuid.UUID(project_match.group(1)), uuid.UUID(annotator_match.group(1))


async def _image_import(
    cli: WorkspaceImportCli, image_dir: str, name: str, prefix: str
):
    return await asyncio.to_thread(
        cli.run_command,
        (
            "python",
            "-m",
            "scripts.import_images",
            image_dir,
            "--name",
            name,
            "--folder",
            prefix,
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
                        "SELECT dataset_id,file_name,file_path,file_size,file_type "
                        "FROM public.dataset_items WHERE dataset_id=ANY(CAST(:ids AS uuid[])) "
                        "ORDER BY dataset_id,file_name"
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


def _read_object(owner_s3, bucket: str, key: str) -> bytes:
    response = owner_s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        return body.read()
    finally:
        body.close()


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
    test_engine, tmp_path, record_property
):
    from app.config import settings

    _verify_postgres(test_engine.url)
    await _assert_identity_selection_profile(test_engine)
    await _assert_empty_owned_rows(test_engine)
    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    assert owner_s3.list_objects_v2(Bucket=bucket).get("Contents", []) == []

    source_root = tmp_path / "identity-seed-selection-fixture"
    source_root.mkdir()
    shutil.copyfile(PROBE, source_root / PROBE.name)
    image_dir = source_root / "images"
    image_dir.mkdir()
    image_bytes = {
        "first.png": _png_pixel(220, 50, 40),
        "second.png": _png_pixel(30, 120, 220),
    }
    for name, content in image_bytes.items():
        (image_dir / name).write_bytes(content)

    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )
    scope, destroy_scope, prefix = _create_dataset_storage_scope(
        resources, owner_s3, bucket, settings
    )
    assert scope["access_key"] != settings.minio_access_key
    assert scope["secret_key"] != settings.minio_secret_key
    cli = None
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
            s3_access_key=scope["access_key"],
            s3_secret_key=scope["secret_key"],
        )
        await asyncio.to_thread(cli.start)
        await _assert_identity_selection_profile(test_engine)

        missing_name = f"selection-empty-{uuid.uuid4().hex[:10]}"
        missing = await _image_import(cli, "/fixture/images", missing_name, prefix)
        record_property("identity_seed_import_without_users", missing.evidence)
        assert not missing.timed_out and missing.exit_code == 0, missing.evidence
        assert "数据库中没有用户" in missing.stdout
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
        assert "refusing to run with environment=production" in guard["stderr"]

        import_name = f"identity seed images {suffix}"
        first_import = await _image_import(cli, "/fixture/images", import_name, prefix)
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
        assert {
            item["file_name"]: (
                item["file_path"],
                item["file_size"],
                item["file_type"],
            )
            for item in first_state["items"]
        } == {
            name: (f"{prefix}/{name}", len(content), "image")
            for name, content in image_bytes.items()
        }
        for name, expected in image_bytes.items():
            assert _read_object(owner_s3, bucket, f"{prefix}/{name}") == expected

        old_image_bytes = dict(image_bytes)
        image_bytes = {
            "first.png": _png_pixel(70, 200, 80),
            "second.png": _png_pixel(240, 180, 20),
        }
        for name, content in image_bytes.items():
            (image_dir / name).write_bytes(content)
        second_import = await _image_import(cli, "/fixture/images", import_name, prefix)
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
        for name, expected in image_bytes.items():
            assert _read_object(owner_s3, bucket, f"{prefix}/{name}") == expected
        versioning = owner_s3.get_bucket_versioning(Bucket=bucket).get("Status")
        versions = _bucket_versions(owner_s3, bucket, prefix)
        if versioning == "Enabled":
            assert len(versions) == 4, versions
            for name in _IMPORT_IMAGE_NAMES:
                matching = [row for row in versions if row["key"] == f"{prefix}/{name}"]
                assert (
                    len(matching) == 2 and sum(row["latest"] for row in matching) == 1
                )
        assert all(
            _read_object(owner_s3, bucket, f"{prefix}/{name}") != old_image_bytes[name]
            for name in _IMPORT_IMAGE_NAMES
        )
        record_property(
            "identity_seed_import_object_rewrite",
            {"bucket_versioning": versioning, "versions": versions},
        )

        expected_admin = accounts["admin_inactive_oldest"]
        expected_annotator = accounts["annotator_active_viewer"]
        first_scale = await _run_scale(cli)
        record_property("identity_seed_scale_first", first_scale.evidence)
        assert not first_scale.timed_out and first_scale.exit_code == 0, (
            first_scale.evidence
        )
        first_project, first_annotator = _scale_result_ids(first_scale.stdout)
        assert first_annotator == expected_annotator
        first_scale_state = await _scale_state(test_engine)
        assert first_project == first_scale_state["project"]["id"]
        assert first_scale_state["project"]["owner_id"] == expected_admin
        assert first_scale_state["dataset"]["created_by"] == expected_admin
        assert first_scale_state["selected_members"] == [expected_annotator]
        assert {
            key: first_scale_state[key]
            for key in ("items", "batches", "tasks", "annotations")
        } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 1}
        assert await _user_snapshot(test_engine) == users_before_scale

        repeated_scale = await _run_scale(cli)
        record_property("identity_seed_scale_repeat", repeated_scale.evidence)
        assert not repeated_scale.timed_out and repeated_scale.exit_code == 0, (
            repeated_scale.evidence
        )
        second_project, second_annotator = _scale_result_ids(repeated_scale.stdout)
        second_scale_state = await _scale_state(test_engine)
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

        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"REVOKE EXECUTE ON FUNCTION {_SCALE_ANNOTATOR} FROM legacy_runtime"
                )
            )
        try:
            denied = await _run_scale(cli)
            record_property("identity_seed_scale_selector_denied", denied.evidence)
            assert not denied.timed_out and denied.exit_code != 0, denied.evidence
            assert "permission denied for function read_scale_seed_annotator" in (
                denied.stderr.lower()
            ), denied.stderr
            assert await _scale_state(test_engine) == {
                "project": None,
                "dataset": None,
                "items": 0,
                "batches": 0,
                "tasks": 0,
                "annotations": 0,
                "selected_members": [],
            }
            assert await _user_snapshot(test_engine) == users_before_scale
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
            late = await _run_scale(cli)
            record_property(
                "identity_seed_scale_late_annotation_failure", late.evidence
            )
            assert not late.timed_out and late.exit_code != 0, late.evidence
            assert "controlled scale-seed annotation failure" in late.stderr
            partial = await _scale_state(test_engine)
            assert partial["project"] is not None and partial["dataset"] is not None
            assert {
                key: partial[key]
                for key in ("items", "batches", "tasks", "annotations")
            } == {"items": 4, "batches": 2, "tasks": 4, "annotations": 0}
            assert await _user_snapshot(test_engine) == users_before_scale
        finally:
            await _drop_annotation_failure(test_engine, *late_trigger)
            failure_trigger = None

        recovered = await _run_scale(cli)
        record_property("identity_seed_scale_late_failure_recovery", recovered.evidence)
        assert not recovered.timed_out and recovered.exit_code == 0, recovered.evidence
        recovered_project, recovered_annotator = _scale_result_ids(recovered.stdout)
        recovered_state = await _scale_state(test_engine)
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
            asyncio.to_thread(
                cli.run_command,
                ("python", "-m", "scripts.seed_scale", "4", "2", "1"),
            )
        )
        try:
            client_address, activity = await _wait_for_selector_gate(
                test_engine, cli, pending_gate, application_name
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
            assert await asyncio.to_thread(cli.stop_current_container) is True
            stopped = await asyncio.wait_for(asyncio.shield(pending_gate), timeout=35)
            record_property(
                "identity_seed_scale_physical_cancellation", stopped.evidence
            )
            assert stopped.exit_code in {137, 143}, stopped.evidence
            assert stopped.externally_stopped is True
            assert stopped.timed_out is False
            await _wait_for_backend_exit(test_engine, backend_pid)
            assert await _scale_state(test_engine) == {
                "project": None,
                "dataset": None,
                "items": 0,
                "batches": 0,
                "tasks": 0,
                "annotations": 0,
                "selected_members": [],
            }
            assert await _user_snapshot(test_engine) == users_before_scale
        finally:
            await _finish_cli_task(pending_gate)
            if gate_original is not None:
                await _remove_selector_gate(test_engine, gate_original)
                gate_original = None

        recovery = await _run_scale(cli)
        record_property("identity_seed_scale_cancellation_recovery", recovery.evidence)
        assert not recovery.timed_out and recovery.exit_code == 0, recovery.evidence
        recovery_project, recovery_annotator = _scale_result_ids(recovery.stdout)
        recovery_state = await _scale_state(test_engine)
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
        try:
            if pending_gate is not None:
                await _finish_cli_task(pending_gate)
        finally:
            try:
                if gate_original is not None:
                    await _remove_selector_gate(test_engine, gate_original)
            finally:
                try:
                    if failure_trigger is not None:
                        await _drop_annotation_failure(test_engine, *failure_trigger)
                finally:
                    try:
                        if cli is not None:
                            await asyncio.to_thread(cli.close)
                    finally:
                        destroy_scope(scope)
