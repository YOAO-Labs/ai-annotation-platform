"""Cold nuScenes CLI acceptance on a fenced operational database caller."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

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
from tests.test_import_nuscenes_lite import _write_fake_nuscenes
from tests.workspace_import_cli_helpers import CliRun, WorkspaceImportCli


pytestmark = pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or os.getenv("AAP_WORKSPACE_IMPORT_CLI_LIVE") != "1",
    reason="requires the explicitly enabled owned nuScenes CLI verifier",
)

ROOT = Path(__file__).resolve().parents[3]
_PROVISIONING_FUNCTIONS = (
    "aap_identity_v1.read_provisioning_account(text)",
    "aap_identity_v1.create_provisioning_account(jsonb)",
)
_IDENTITY_CONSUMER_FUNCTIONS = (
    "aap_identity_v1.read_actor_facts(uuid)",
    "aap_identity_v1.lock_accounts_share(uuid[],boolean)",
    "aap_identity_v1.lock_accounts_update(uuid[],boolean)",
    "aap_identity_v1.lock_accounts_key_share(uuid[],boolean)",
    "aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])",
)
_WORKSPACE_FUNCTIONS = (
    "aap_workspace_v1.read_import_project(text)",
    "aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)",
    "aap_workspace_v1.lock_project_manager(uuid,uuid)",
)
_ASSETS_FUNCTIONS = (
    "aap_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)",
    "aap_assets_v1.append_operator_items(uuid,uuid,jsonb)",
    "aap_assets_v1.patch_operator_items(uuid,uuid,jsonb)",
    "aap_assets_v1.recount_operator_items(uuid,uuid)",
    "aap_assets_v1.read_operator_items(uuid,uuid,integer)",
)
_EMPTY_TABLES = (
    "users",
    "projects",
    "project_members",
    "project_templates",
    "datasets",
    "dataset_items",
    "scenes",
    "scene_frame_poses",
    "project_datasets",
    "tasks",
    "task_dataset_item_links",
    "task_creation_chunks",
    "dataset_operator_operations",
    "dataset_spatial_operations",
    "task_creation_operations",
    "sensor_calibration_revisions",
    "audit_logs",
)


def _create_datasets_storage_scope(resources, owner_s3, bucket: str, settings):
    """Create a worker principal limited to the reserved nuScenes key prefix."""
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
    scope["policy"] = f"aap-nuscenes-{secrets.token_hex(8)}"
    prefix_arn = f"arn:aws:s3:::{bucket}/__aap_trusted_nuscenes__-*/*"
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
                "Resource": [prefix_arn],
            }
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
        return scope, _destroy_cleanup_storage_scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


async def _assert_operational_role(test_engine) -> None:
    _verify_postgres(test_engine.url)
    role_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert role_url.drivername == "postgresql+asyncpg"
    assert role_url.username == "legacy_runtime"
    assert (role_url.host, role_url.port, role_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert str(role_url.database).endswith("_test")
    engine = create_async_engine(role_url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
            role_flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper, rolcreaterole, rolcreatedb "
                        "FROM pg_catalog.pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            assert role_flags == (False, False, False)
            inherited = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_auth_members membership "
                    "JOIN pg_catalog.pg_roles member_role "
                    "ON member_role.oid=membership.member "
                    "WHERE member_role.rolname=current_user"
                )
            )
            assert inherited == 0
            for signature in (
                *_PROVISIONING_FUNCTIONS,
                *_WORKSPACE_FUNCTIONS,
                *_ASSETS_FUNCTIONS,
            ):
                assert (
                    await db.scalar(
                        text(
                            "SELECT pg_catalog.has_function_privilege("
                            "current_user, CAST(:signature AS regprocedure), 'EXECUTE')"
                        ),
                        {"signature": signature},
                    )
                    is True
                ), signature
            identity_functions = set(
                (
                    await db.scalars(
                        text(
                            "SELECT ns.nspname || '.' || proc.proname || '(' || "
                            "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                            "FROM pg_catalog.pg_proc AS proc "
                            "JOIN pg_catalog.pg_namespace AS ns "
                            "ON ns.oid=proc.pronamespace "
                            "WHERE ns.nspname='aap_identity_v1' "
                            "AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')"
                        )
                    )
                ).all()
            )
            assert identity_functions == {
                *_IDENTITY_CONSUMER_FUNCTIONS,
                *_PROVISIONING_FUNCTIONS,
            }
            for table in ("users", "projects", "project_members", "project_templates"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert (
                        await db.scalar(
                            text(
                                "SELECT pg_catalog.has_table_privilege("
                                "current_user, :table_name, :privilege)"
                            ),
                            {"table_name": f"public.{table}", "privilege": privilege},
                        )
                        is False
                    ), f"legacy_runtime unexpectedly has {privilege} on {table}"
    finally:
        await engine.dispose()


async def _assert_empty_owned_rows(test_engine) -> None:
    async with test_engine.connect() as db:
        counts = {}
        for table in _EMPTY_TABLES:
            counts[table] = await db.scalar(
                text(f"SELECT count(*) FROM public.{table}")
            )
    assert all(count == 0 for count in counts.values()), counts


async def _object_keys(owner_s3, bucket: str) -> set[str]:
    page = owner_s3.list_objects_v2(
        Bucket=bucket,
        Prefix="__aap_trusted_nuscenes__-",
    )
    return {item["Key"] for item in page.get("Contents", [])}


def _object_snapshot(owner_s3, bucket: str, prefix: str) -> dict[str, str]:
    page = owner_s3.list_objects_v2(Bucket=bucket, Prefix=prefix.rstrip("/") + "/")
    return {
        item["Key"]: hashlib.sha256(
            _get_object(owner_s3, bucket, item["Key"])
        ).hexdigest()
        for item in page.get("Contents", [])
    }


async def _assert_reimport_transaction_rolled_back(test_engine) -> None:
    await _assert_empty_owned_rows(test_engine)


def _result_ids(result: CliRun) -> tuple[uuid.UUID, uuid.UUID]:
    dataset_match = re.search(r"dataset_id:\s*([0-9a-f-]{36})", result.stdout)
    project_match = re.search(r"project_id:\s*([0-9a-f-]{36})", result.stdout)
    assert dataset_match and project_match, result.stdout
    return uuid.UUID(dataset_match.group(1)), uuid.UUID(project_match.group(1))


async def _read_import_state(test_engine, dataset_id, project_id) -> dict:
    async with test_engine.connect() as db:
        dataset = (
            (
                await db.execute(
                    text(
                        "SELECT id, display_id, name, file_count, created_by, metadata "
                        "FROM public.datasets WHERE id=:id"
                    ),
                    {"id": dataset_id},
                )
            )
            .mappings()
            .one()
        )
        project = (
            (
                await db.execute(
                    text(
                        "SELECT id, display_id, name, owner_id, type_key, data_type, "
                        "tool_bindings FROM public.projects WHERE id=:id"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .one()
        )
        scenes = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, source_format, source_metadata "
                        "FROM public.scenes WHERE dataset_id=:id ORDER BY name"
                    ),
                    {"id": dataset_id},
                )
            )
            .mappings()
            .all()
        )
        items = (
            (
                await db.execute(
                    text(
                        "SELECT id, file_path, file_type, file_size, content_hash, "
                        "scene_id, frame_index, metadata FROM public.dataset_items "
                        "WHERE dataset_id=:id ORDER BY file_path"
                    ),
                    {"id": dataset_id},
                )
            )
            .mappings()
            .all()
        )
        tasks = (
            (
                await db.execute(
                    text(
                        "SELECT id, display_id, file_name, file_path, file_type, "
                        "dataset_item_id FROM public.tasks "
                        "WHERE project_id=:id ORDER BY display_id"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .all()
        )
        poses = (
            (
                await db.execute(
                    text(
                        "SELECT scene_id, frame_index, timestamp_us, ego_translation, "
                        "ego_rotation, source_metadata FROM public.scene_frame_poses "
                        "WHERE scene_id=ANY(CAST(:ids AS uuid[])) "
                        "ORDER BY scene_id, frame_index"
                    ),
                    {"ids": [scene["id"] for scene in scenes]},
                )
            )
            .mappings()
            .all()
        )
        links = await db.scalar(
            text(
                "SELECT count(*) FROM public.project_datasets "
                "WHERE project_id=:project_id AND dataset_id=:dataset_id"
            ),
            {"project_id": project_id, "dataset_id": dataset_id},
        )
        memberships = await db.scalar(
            text("SELECT count(*) FROM public.project_members WHERE project_id=:id"),
            {"id": project_id},
        )
        admin = (
            (
                await db.execute(
                    text(
                        "SELECT id, email, name, role, is_active FROM public.users "
                        "WHERE email='admin'"
                    )
                )
            )
            .mappings()
            .one()
        )
        user_count = await db.scalar(text("SELECT count(*) FROM public.users"))
        dataset_count = await db.scalar(
            text("SELECT count(*) FROM public.datasets WHERE name=:name"),
            {"name": dataset["name"]},
        )
        project_count = await db.scalar(
            text("SELECT count(*) FROM public.projects WHERE name=:name"),
            {"name": project["name"]},
        )
    return {
        "dataset": dict(dataset),
        "project": dict(project),
        "scenes": [dict(scene) for scene in scenes],
        "items": [dict(item) for item in items],
        "tasks": [dict(task) for task in tasks],
        "poses": [dict(pose) for pose in poses],
        "links": links,
        "memberships": memberships,
        "admin": dict(admin),
        "user_count": user_count,
        "dataset_count": dataset_count,
        "project_count": project_count,
    }


def _assert_success_state(state: dict, *, dataset_name: str, project_id, dataset_id):
    dataset = state["dataset"]
    project = state["project"]
    assert dataset["id"] == dataset_id and project["id"] == project_id
    assert state["user_count"] == state["dataset_count"] == state["project_count"] == 1
    assert dataset["name"] == dataset_name and dataset["file_count"] == 10
    assert dataset["metadata"]["source"] == {
        "format": "nuscenes",
        "version": "v1.0-mini",
        "scenes": ["scene-0000", "scene-0001"],
        "pcd_encoding": "binary_xyz_f32",
        "storage_root": f"__aap_trusted_nuscenes__-{dataset_id.hex}",
        "storage_layout": "uuid_v1",
    }
    assert project["type_key"] == project["data_type"] == "lidar"
    assert project["owner_id"] == state["admin"]["id"]
    assert dataset["created_by"] == state["admin"]["id"]
    assert project["tool_bindings"] == {}
    assert state["admin"]["email"] == "admin"
    assert state["admin"]["name"] == "超级管理员"
    assert state["admin"]["role"] == "super_admin" and state["admin"]["is_active"]
    assert state["links"] == 1
    assert state["memberships"] == 0
    assert len(state["scenes"]) == 2
    assert [scene["name"] for scene in state["scenes"]] == ["scene-0000", "scene-0001"]
    assert all(scene["source_format"] == "nuscenes" for scene in state["scenes"])
    assert len(state["items"]) == 10
    assert len(state["tasks"]) == 4
    assert len(state["poses"]) == 4
    assert sum(item["file_type"] == "point_cloud" for item in state["items"]) == 4
    assert sum(item["file_type"] == "image" for item in state["items"]) == 4
    camera_items = [item for item in state["items"] if item["file_type"] == "image"]
    assert all(
        len(item["metadata"]["nuscenes_export"]["platform_calibration_digest"]) == 64
        for item in camera_items
    )
    calibration_items = [
        item for item in state["items"] if item["file_type"] == "other"
    ]
    assert len(calibration_items) == 2
    lidar_item_ids = {
        item["id"] for item in state["items"] if item["file_type"] == "point_cloud"
    }
    assert {task["dataset_item_id"] for task in state["tasks"]} == lidar_item_ids
    assert all(task["file_type"] == "point_cloud" for task in state["tasks"])
    for scene in state["scenes"]:
        scene_items = [
            item for item in state["items"] if item["scene_id"] == scene["id"]
        ]
        assert len(scene_items) == 5
        assert sum(item["file_type"] == "point_cloud" for item in scene_items) == 2
        assert sum(item["file_type"] == "image" for item in scene_items) == 2
        assert sum(item["file_type"] == "other" for item in scene_items) == 1
    assert all(
        item["content_hash"] and len(item["content_hash"]) == 64
        for item in state["items"]
    )
    for scene in state["scenes"]:
        source = scene["source_metadata"]
        assert source["frame"] == "ego" and source["frame_count"] == 2
        assert len(source["nuscenes_export"]["map_sha256"]) == 64
    for item in state["items"]:
        if item["file_type"] in {"point_cloud", "image"}:
            evidence = item["metadata"]["nuscenes_export"]
            assert len(evidence["source_sha256"]) == 64
            assert evidence["source_file_size"] > 0
            assert evidence["source_storage_key"].startswith(
                state["dataset"]["metadata"]["source"]["storage_root"]
            )
    for scene_id in {pose["scene_id"] for pose in state["poses"]}:
        trajectory = [pose for pose in state["poses"] if pose["scene_id"] == scene_id]
        assert [pose["frame_index"] for pose in trajectory] == [0, 1]
        assert trajectory[0]["timestamp_us"] < trajectory[1]["timestamp_us"]
        assert trajectory[1]["ego_translation"][0] - trajectory[0]["ego_translation"][
            0
        ] == pytest.approx(2.0)
        assert all(pose["ego_rotation"] == [1.0, 0.0, 0.0, 0.0] for pose in trajectory)


def _get_object(owner_s3, bucket: str, key: str) -> bytes:
    response = owner_s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        return body.read()
    finally:
        body.close()


def _assert_real_object_bytes(
    owner_s3, bucket: str, source_root: Path, state: dict
) -> None:
    pcd = next(item for item in state["items"] if item["file_type"] == "point_cloud")
    pcd_bytes = _get_object(owner_s3, bucket, pcd["file_path"])
    assert pcd_bytes.startswith(b"# .PCD v0.7")
    assert hashlib.sha256(pcd_bytes).hexdigest() == pcd["content_hash"]
    assert (
        owner_s3.head_object(Bucket=bucket, Key=pcd["file_path"])["ContentLength"]
        == pcd["file_size"]
    )

    source_record = pcd["metadata"]["nuscenes_export"]
    source_bytes = _get_object(owner_s3, bucket, source_record["source_storage_key"])
    expected_lidar = (source_root / "samples/LIDAR_TOP/lidar.pcd.bin").read_bytes()
    assert source_bytes == expected_lidar
    assert hashlib.sha256(source_bytes).hexdigest() == source_record["source_sha256"]

    camera = next(item for item in state["items"] if item["file_type"] == "image")
    camera_bytes = _get_object(owner_s3, bucket, camera["file_path"])
    expected_camera = (source_root / "samples/CAM_FRONT/cam.jpg").read_bytes()
    assert camera_bytes == expected_camera and camera_bytes.startswith(b"\xff\xd8")
    assert hashlib.sha256(camera_bytes).hexdigest() == camera["content_hash"]

    calibration = next(item for item in state["items"] if item["file_type"] == "other")
    calibration_document = json.loads(
        _get_object(owner_s3, bucket, calibration["file_path"])
    )
    assert len(calibration_document["intrinsic"]) == 9
    assert len(calibration_document["extrinsic"]) == 16

    scene = state["scenes"][0]
    export = scene["source_metadata"]["nuscenes_export"]
    map_bytes = _get_object(owner_s3, bucket, export["map_storage_key"])
    assert map_bytes == (source_root / "maps/map-0.png").read_bytes()
    assert hashlib.sha256(map_bytes).hexdigest() == export["map_sha256"]


async def _install_task_trigger(
    test_engine, marker: str | None = None
) -> tuple[str, str]:
    """Fail task insertion, or pause it for the physical CLI cancellation case."""
    suffix = uuid.uuid4().hex[:12]
    function_name = f"workspace_import_cli_fail_{suffix}"
    trigger_name = f"workspace_import_cli_fail_{suffix}_trigger"
    function = f"public.{function_name}"
    trigger = f"public.{trigger_name}"
    # Default PostgreSQL can finish a query before noticing a dead client.
    # Keep this artificial pause bounded; cancellation is not SQL interruption.
    action = (
        "PERFORM pg_sleep(10); "
        if marker is None
        else f"RAISE EXCEPTION '{marker}' USING ERRCODE='P0001'; "
    )
    try:
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"CREATE FUNCTION {function}() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    "IF NEW.file_type='point_cloud' "
                    "AND NEW.file_name='scene-0000_000000.pcd' THEN "
                    + action
                    + "END IF; RETURN NEW; END $$"
                )
            )
            await db.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON public.tasks "
                    f"FOR EACH ROW EXECUTE FUNCTION {function}()"
                )
            )
    except BaseException:
        await _drop_task_trigger(test_engine, function, trigger)
        raise
    return function, trigger


async def _drop_task_trigger(test_engine, function: str, trigger: str) -> None:
    async with test_engine.begin() as db:
        await db.execute(
            text(f"DROP TRIGGER IF EXISTS {trigger.split('.')[-1]} ON public.tasks")
        )
        await db.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))


async def _wait_for_import_pause(test_engine, pending) -> dict:
    async with asyncio.timeout(30):
        while True:
            if pending.done():
                raise AssertionError(pending.result().evidence)
            async with test_engine.connect() as db:
                sessions = (
                    (
                        await db.execute(
                            text(
                                "SELECT pid, host(client_addr) AS client_addr, "
                                "wait_event_type, wait_event "
                                "FROM pg_catalog.pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND usename='legacy_runtime' AND state='active' "
                                "AND wait_event_type='Timeout' AND wait_event='PgSleep'"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            if sessions:
                assert len(sessions) == 1
                return dict(sessions[0])
            await asyncio.sleep(0.1)


async def _wait_for_import_disconnect(test_engine) -> None:
    async with asyncio.timeout(15):
        while True:
            async with test_engine.connect() as db:
                count = await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND usename='legacy_runtime'"
                    )
                )
            if count == 0:
                return
            await asyncio.sleep(0.1)


async def test_actual_nuscenes_cli_uses_fenced_workspace_and_identity_capabilities(
    test_engine, tmp_path, record_property
):
    from app.config import settings

    _verify_postgres(test_engine.url)
    await _assert_operational_role(test_engine)
    await _assert_empty_owned_rows(test_engine)

    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, datasets_bucket, resources["owner"])
    initial_keys = await _object_keys(owner_s3, datasets_bucket)
    assert not initial_keys, (
        "owned dataset bucket must be empty before the CLI acceptance"
    )

    source_root = tmp_path / "workspace-import-nuscenes-fixture"
    _write_fake_nuscenes(source_root, scenes=2, samples_per=2)
    endpoint = urlsplit(endpoint_url())
    assert (
        endpoint.scheme in {"http", "https"}
        and endpoint.hostname
        and endpoint.username is None
        and endpoint.password is None
        and endpoint.path in {"", "/"}
        and not endpoint.query
        and not endpoint.fragment
    )
    scope, destroy_scope = _create_datasets_storage_scope(
        resources, owner_s3, datasets_bucket, settings
    )
    cli = None
    try:
        assert scope["access_key"] != settings.minio_access_key
        assert scope["secret_key"] != settings.minio_secret_key
        authority_host = (
            f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
        )
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
        await _assert_operational_role(test_engine)

        missing_capability_name = "workspace-import-missing-" + uuid.uuid4().hex[:8]
        create_signature = (
            "aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)"
        )
        revoked = False
        try:
            async with test_engine.begin() as owner:
                assert (
                    await owner.scalar(
                        text(
                            "SELECT has_function_privilege('legacy_runtime', CAST(:sig AS regprocedure), 'EXECUTE')"
                        ),
                        {"sig": create_signature},
                    )
                    is True
                )
                await owner.execute(
                    text(
                        f"REVOKE EXECUTE ON FUNCTION {create_signature} FROM legacy_runtime"
                    )
                )
                revoked = True
            missing_capability = await asyncio.to_thread(
                cli.run, dataset_name=missing_capability_name
            )
            record_property(
                "workspace_import_missing_capability", missing_capability.evidence
            )
            assert not missing_capability.timed_out
            assert missing_capability.exit_code != 0, missing_capability.stdout
            assert (
                "permission denied for function create_import_project"
                in missing_capability.stderr.lower()
            ), missing_capability.stderr
            await _assert_reimport_transaction_rolled_back(test_engine)
            missing_keys = await _object_keys(owner_s3, datasets_bucket)
            assert missing_keys, (
                "failed import must retain its staged S3 source objects"
            )
        finally:
            if revoked:
                async with test_engine.begin() as owner:
                    await owner.execute(
                        text(
                            f"GRANT EXECUTE ON FUNCTION {create_signature} TO legacy_runtime"
                        )
                    )

        trigger_name = "workspace-import-trigger-" + uuid.uuid4().hex[:8]
        trigger_marker = "workspace_import_cli_late_task_" + uuid.uuid4().hex[:12]
        function, trigger = await _install_task_trigger(test_engine, trigger_marker)
        keys_before_trigger = await _object_keys(owner_s3, datasets_bucket)
        try:
            late_failure = await asyncio.to_thread(cli.run, dataset_name=trigger_name)
            record_property("workspace_import_late_task_failure", late_failure.evidence)
            assert not late_failure.timed_out
            assert late_failure.exit_code != 0, late_failure.stdout
            assert trigger_marker in late_failure.stderr, late_failure.stderr
            await _assert_reimport_transaction_rolled_back(test_engine)
            keys_after_trigger = await _object_keys(owner_s3, datasets_bucket)
            assert keys_after_trigger - keys_before_trigger, (
                "late task failure should retain newly staged S3 objects"
            )
            assert keys_before_trigger <= keys_after_trigger
        finally:
            await _drop_task_trigger(test_engine, function, trigger)

        function, trigger = await _install_task_trigger(test_engine)
        keys_before_cancel = await _object_keys(owner_s3, datasets_bucket)
        dataset_name = "workspace-import-recovery-" + uuid.uuid4().hex[:8]
        pending = asyncio.create_task(
            asyncio.to_thread(cli.run, dataset_name=dataset_name)
        )
        try:
            paused_session = await _wait_for_import_pause(test_engine, pending)
            assert paused_session["client_addr"] == await asyncio.to_thread(
                cli.running_database_client_address
            )
            record_property("workspace_import_cancel_paused_session", paused_session)
            await _assert_empty_owned_rows(test_engine)
            keys_during_cancel = await _object_keys(owner_s3, datasets_bucket)
            assert keys_during_cancel - keys_before_cancel
            assert await asyncio.to_thread(cli.stop_current_container)
            cancelled = await pending
            record_property("workspace_import_cancelled", cancelled.evidence)
            assert cancelled.exit_code in {137, 143}, cancelled.evidence
            assert cancelled.externally_stopped and not cancelled.timed_out
            await _wait_for_import_disconnect(test_engine)
            await _assert_reimport_transaction_rolled_back(test_engine)
            assert keys_during_cancel == await _object_keys(owner_s3, datasets_bucket)
        finally:
            try:
                if not pending.done():
                    await asyncio.to_thread(cli.stop_current_container)
                await pending
            finally:
                await _drop_task_trigger(test_engine, function, trigger)

        first = await asyncio.to_thread(cli.run, dataset_name=dataset_name)
        record_property("workspace_import_success", first.evidence)
        assert first.exit_code == 0, first.stderr
        assert "total_items: 10" in first.stdout, first.stdout
        dataset_id, project_id = _result_ids(first)
        state = await _read_import_state(test_engine, dataset_id, project_id)
        _assert_success_state(
            state,
            dataset_name=dataset_name,
            project_id=project_id,
            dataset_id=dataset_id,
        )
        _assert_real_object_bytes(owner_s3, datasets_bucket, source_root, state)

        replay = await asyncio.to_thread(cli.run, dataset_name=dataset_name)
        record_property("workspace_import_replay", replay.evidence)
        assert replay.exit_code == 0, replay.stderr
        assert "total_items: 0" in replay.stdout, replay.stdout
        assert replay.stdout.count("(已存在,跳过)") == 2, replay.stdout
        replay_dataset_id, replay_project_id = _result_ids(replay)
        assert (replay_dataset_id, replay_project_id) == (dataset_id, project_id)
        replay_state = await _read_import_state(test_engine, dataset_id, project_id)
        _assert_success_state(
            replay_state,
            dataset_name=dataset_name,
            project_id=project_id,
            dataset_id=dataset_id,
        )
        assert [item["id"] for item in replay_state["items"]] == [
            item["id"] for item in state["items"]
        ]
        assert [task["id"] for task in replay_state["tasks"]] == [
            task["id"] for task in state["tasks"]
        ]
        assert [scene["id"] for scene in replay_state["scenes"]] == [
            scene["id"] for scene in state["scenes"]
        ]
        assert replay_state["poses"] == state["poses"]
        assert [item["content_hash"] for item in replay_state["items"]] == [
            item["content_hash"] for item in state["items"]
        ]
        assert replay_state["admin"] == state["admin"]
        _assert_real_object_bytes(owner_s3, datasets_bucket, source_root, replay_state)

        storage_root = state["dataset"]["metadata"]["source"]["storage_root"]
        objects_before_drift = _object_snapshot(owner_s3, datasets_bucket, storage_root)
        raw_lidar = source_root / "samples/LIDAR_TOP/lidar.pcd.bin"
        original_lidar = raw_lidar.read_bytes()
        raw_lidar.write_bytes(original_lidar + b"\x00source-drift")
        stale_source = await asyncio.to_thread(cli.run, dataset_name=dataset_name)
        record_property("workspace_import_stale_source", stale_source.evidence)
        assert not stale_source.timed_out
        assert stale_source.exit_code != 0, stale_source.stdout
        assert "source drift" in stale_source.stderr.lower(), stale_source.stderr
        stale_state = await _read_import_state(test_engine, dataset_id, project_id)
        assert stale_state["dataset"] == state["dataset"]
        assert stale_state["project"] == state["project"]
        assert stale_state["scenes"] == state["scenes"]
        assert stale_state["items"] == state["items"]
        assert stale_state["tasks"] == state["tasks"]
        assert stale_state["poses"] == state["poses"]
        assert stale_state["admin"] == state["admin"]
        assert (
            _object_snapshot(owner_s3, datasets_bucket, storage_root)
            == objects_before_drift
        )
    finally:
        try:
            if cli is not None:
                await asyncio.to_thread(cli.close)
        finally:
            destroy_scope(scope)
