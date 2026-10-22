"""Run the real point-cloud seeds through the fenced API image and S3 scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
import uuid

import pytest
from sqlalchemy import text

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import (
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_import_nuscenes_lite import _write_fake_nuscenes
from tests.test_workspace_import_cli_live import (
    _assert_empty_owned_rows,
    _assert_operational_role,
    _assert_real_object_bytes,
    _create_datasets_storage_scope,
    _get_object,
    _object_keys,
    _object_snapshot,
    _read_import_state,
)
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_SEED_OPERATOR_LIVE") != "1",
        reason="requires the explicitly enabled owned Workspace seed verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_SEED_FUNCTIONS = (
    "aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
    "aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
)
_LEGACY_PROJECT_NAME = "点云联合标注 (dev)"
_LEGACY_DATASET_NAME = "pc-scene-dev"
_SEED_TABLES = (
    "users",
    "projects",
    "project_members",
    "datasets",
    "dataset_items",
    "scenes",
    "scene_frame_poses",
    "project_datasets",
    "tasks",
    "task_batches",
    "task_dataset_item_links",
    "task_creation_chunks",
    "task_creation_operations",
    "project_deletion_operations",
    "dataset_operator_operations",
    "dataset_spatial_operations",
    "sensor_calibration_revisions",
    "audit_logs",
)
_OLD_PCD = (
    b"# .PCD v0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
    b"WIDTH 1\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS 1\nDATA ascii\n0 0 0\n"
)


def _probe_result(run) -> dict:
    prefix = "AAP_WORKSPACE_SEED_RESULT="
    matches = [
        line[len(prefix) :]
        for line in run.stdout.splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) == 1, run.evidence
    return json.loads(matches[0])


async def _assert_seed_privileges(test_engine) -> None:
    async with test_engine.connect() as db:
        for signature in _SEED_FUNCTIONS:
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'legacy_runtime', CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'identity_api_consumer', CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature


async def _legacy_snapshot(test_engine) -> dict:
    async with test_engine.connect() as db:
        project = (
            (
                await db.execute(
                    text(
                        "SELECT id, display_id, name, owner_id, type_label, type_key, "
                        "data_type, scene_mode, prefer_same_scene_continuation "
                        "FROM public.projects WHERE display_id='P-PC-DEV' ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
        dataset = (
            (
                await db.execute(
                    text(
                        "SELECT id, display_id, name, data_type, file_count, created_by, metadata "
                        "FROM public.datasets WHERE display_id='DS-PC-DEV' ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
        project_ids = [row["id"] for row in project]
        dataset_ids = [row["id"] for row in dataset]
        links = (
            (
                await db.execute(
                    text(
                        "SELECT id, project_id, dataset_id FROM public.project_datasets "
                        "WHERE project_id=ANY(CAST(:project_ids AS uuid[])) "
                        "OR dataset_id=ANY(CAST(:dataset_ids AS uuid[])) ORDER BY id"
                    ),
                    {"project_ids": project_ids, "dataset_ids": dataset_ids},
                )
            )
            .mappings()
            .all()
        )
        items = (
            (
                await db.execute(
                    text(
                        "SELECT id, dataset_id, file_name, file_path, file_type, file_size, content_hash "
                        "FROM public.dataset_items WHERE dataset_id=ANY(CAST(:ids AS uuid[])) "
                        "ORDER BY id"
                    ),
                    {"ids": dataset_ids},
                )
            )
            .mappings()
            .all()
        )
        tasks = (
            (
                await db.execute(
                    text(
                        "SELECT id, project_id, display_id FROM public.tasks "
                        "WHERE project_id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
                    ),
                    {"ids": project_ids},
                )
            )
            .mappings()
            .all()
        )
    return {
        "projects": [dict(row) for row in project],
        "datasets": [dict(row) for row in dataset],
        "links": [dict(row) for row in links],
        "items": [dict(row) for row in items],
        "tasks": [dict(row) for row in tasks],
    }


async def _seed_table_counts(test_engine) -> dict[str, int]:
    async with test_engine.connect() as db:
        return {
            table: await db.scalar(text(f"SELECT count(*) FROM public.{table}"))
            for table in _SEED_TABLES
        }


async def _insert_legacy_seed(test_engine, owner_s3, bucket: str, owner_id) -> dict:
    project_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    item_id = uuid.uuid4()
    link_id = uuid.uuid4()
    old_key = f"__aap_trusted_nuscenes__-{uuid.uuid4().hex}/legacy/old.pcd"
    async with test_engine.begin() as db:
        admin_id = await db.scalar(
            text("SELECT id FROM public.users WHERE email='admin'")
        )
        assert admin_id == owner_id
        await db.execute(
            text(
                "INSERT INTO public.projects "
                "(id,display_id,name,owner_id,type_label,type_key,data_type) "
                "VALUES (:id,'P-PC-DEV',:name,:owner,'点云检测','lidar','lidar')"
            ),
            {
                "id": project_id,
                "name": _LEGACY_PROJECT_NAME,
                "owner": owner_id,
            },
        )
        await db.execute(
            text(
                "INSERT INTO public.datasets "
                "(id,display_id,name,data_type,file_count,created_by,metadata) "
                "VALUES (:id,'DS-PC-DEV',:name,'point_cloud',1,:owner,'{}'::jsonb)"
            ),
            {"id": dataset_id, "name": _LEGACY_DATASET_NAME, "owner": owner_id},
        )
        await db.execute(
            text(
                "INSERT INTO public.project_datasets(id,project_id,dataset_id) "
                "VALUES (:id,:project_id,:dataset_id)"
            ),
            {"id": link_id, "project_id": project_id, "dataset_id": dataset_id},
        )
        await db.execute(
            text(
                "INSERT INTO public.dataset_items "
                "(id,dataset_id,file_name,file_path,file_type,file_size,content_hash,metadata) "
                "VALUES (:id,:dataset_id,'old.pcd',:file_path,'point_cloud',:file_size,:sha256,'{}'::jsonb)"
            ),
            {
                "id": item_id,
                "dataset_id": dataset_id,
                "file_path": old_key,
                "file_size": len(_OLD_PCD),
                "sha256": hashlib.sha256(_OLD_PCD).hexdigest(),
            },
        )
    owner_s3.put_object(Bucket=bucket, Key=old_key, Body=_OLD_PCD)
    return {
        "project_id": project_id,
        "dataset_id": dataset_id,
        "item_id": item_id,
        "old_key": old_key,
    }


async def _seed_rows(test_engine) -> dict:
    async with test_engine.connect() as db:
        project = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, owner_id, scene_mode, prefer_same_scene_continuation, "
                        "tool_bindings, updated_at FROM public.projects "
                        "WHERE display_id='P-PC-DEV'"
                    )
                )
            )
            .mappings()
            .one()
        )
        dataset = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, file_count FROM public.datasets "
                        "WHERE display_id='DS-PC-DEV'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return {"project": dict(project), "dataset": dict(dataset)}


async def _batch_rows(test_engine, project_id) -> list[dict]:
    async with test_engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, total_tasks FROM public.task_batches "
                        "WHERE project_id=:project_id ORDER BY id"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def test_seed_entrypoints_run_through_the_fenced_api_image(
    test_engine, tmp_path, record_property
):
    from app.config import settings
    from scripts.seed_pointcloud import NUSCENES_PROJECT_NAME, POINTCLOUD_TOOL_BINDINGS

    _verify_postgres(test_engine.url)
    await _assert_operational_role(test_engine)
    await _assert_seed_privileges(test_engine)
    await _assert_empty_owned_rows(test_engine)

    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    assert not await _object_keys(owner_s3, bucket)

    source_root = tmp_path / "workspace-seed-source"
    source_root.mkdir()
    directory_fixture = source_root / "directory"
    (directory_fixture / "lidar").mkdir(parents=True)
    (directory_fixture / "lidar" / "000000.pcd").write_bytes(_OLD_PCD)
    cache = source_root / "nu-cache"
    _write_fake_nuscenes(cache / "content", scenes=1, samples_per=1)
    scene_path = cache / "content" / "v1.0-mini" / "scene.json"
    scenes = json.loads(scene_path.read_text(encoding="utf-8"))
    scenes[0]["name"] = "scene-0061"
    scene_path.write_text(json.dumps(scenes), encoding="utf-8")
    probe = ROOT / "apps/api/tests/fixtures/workspace_seed_operator_probe.py"
    shutil.copyfile(probe, source_root / probe.name)

    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )
    scope, destroy_scope = _create_datasets_storage_scope(
        resources, owner_s3, bucket, settings
    )
    cli = None
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
        await _assert_operational_role(test_engine)

        async def run_probe(operation: str, argument: str | None = None):
            command = ("python", f"/fixture/{probe.name}", operation)
            if argument is not None:
                command = (*command, argument)
            return await asyncio.to_thread(cli.run_command, command)

        directory_name = f"__aap_trusted_nuscenes__-seed-{uuid.uuid4().hex[:8]}"
        directory_run = await run_probe("directory", directory_name)
        record_property("workspace_seed_directory", directory_run.evidence)
        assert directory_run.exit_code == 0, directory_run.stderr
        directory = _probe_result(directory_run)
        assert directory["owner_id"]
        assert directory["result"]["project"] == "P-PC-MULTI"
        assert directory["result"]["tasks"] == {"created": 1, "total": 1}
        async with test_engine.connect() as db:
            project = (
                (
                    await db.execute(
                        text(
                            "SELECT id, scene_mode, prefer_same_scene_continuation "
                            "FROM public.projects WHERE display_id='P-PC-MULTI'"
                        )
                    )
                )
                .mappings()
                .one()
            )
            dataset_id = await db.scalar(
                text("SELECT id FROM public.datasets WHERE display_id='DS-PC-MULTI'")
            )
            tasks = await db.scalar(
                text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
                {"id": project["id"]},
            )
        assert project["scene_mode"] is False
        assert project["prefer_same_scene_continuation"] is False
        assert dataset_id is not None and tasks == 1
        directory_key = f"{directory_name}/lidar/000000.pcd"
        assert directory_key in await _object_keys(owner_s3, bucket)
        assert (
            hashlib.sha256(_get_object(owner_s3, bucket, directory_key)).hexdigest()
            == hashlib.sha256(_OLD_PCD).hexdigest()
        )

        legacy = await _insert_legacy_seed(
            test_engine, owner_s3, bucket, uuid.UUID(directory["owner_id"])
        )
        legacy_before = await _legacy_snapshot(test_engine)
        rows_before_failure = await _seed_table_counts(test_engine)
        keys_before_failure = await _object_keys(owner_s3, bucket)
        signature = _SEED_FUNCTIONS[1]
        async with test_engine.begin() as owner:
            await owner.execute(
                text(f"REVOKE EXECUTE ON FUNCTION {signature} FROM legacy_runtime")
            )
        try:
            missing = await run_probe("rollback", legacy["old_key"])
            record_property(
                "workspace_seed_missing_defaults_capability", missing.evidence
            )
            assert missing.exit_code != 0, missing.stdout
            assert (
                "permission denied for function write_nuscenes_seed_defaults"
                in missing.stderr.lower()
            ), missing.stderr
            assert await _legacy_snapshot(test_engine) == legacy_before
            assert await _seed_table_counts(test_engine) == rows_before_failure
            keys_after_failure = await _object_keys(owner_s3, bucket)
            assert legacy["old_key"] in keys_after_failure
            assert _get_object(owner_s3, bucket, legacy["old_key"]) == _OLD_PCD
            assert keys_after_failure - keys_before_failure, (
                "late failure should retain staged S3 objects"
            )
        finally:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(f"GRANT EXECUTE ON FUNCTION {signature} TO legacy_runtime")
                )
        await _assert_seed_privileges(test_engine)

        keys_before_rollback = await _object_keys(owner_s3, bucket)
        rollback = await run_probe("rollback", legacy["old_key"])
        record_property("workspace_seed_outer_rollback", rollback.evidence)
        assert rollback.exit_code == 0, rollback.stderr
        rolled_back = _probe_result(rollback)
        assert rolled_back["info"]["total_items"] == 3
        assert rolled_back["old_keys"] == [legacy["old_key"]]
        assert rolled_back["old_keys_before_rollback"] == [True]
        assert rolled_back["old_keys_after_rollback"] == [True]
        assert rolled_back["cleanup_called"] is False
        assert await _legacy_snapshot(test_engine) == legacy_before
        assert await _seed_table_counts(test_engine) == rows_before_failure
        keys_after_rollback = await _object_keys(owner_s3, bucket)
        assert keys_after_rollback >= keys_before_rollback
        assert _get_object(owner_s3, bucket, legacy["old_key"]) == _OLD_PCD
        source_key = next(
            key
            for key in keys_after_rollback
            if "/_nuscenes/source/samples/LIDAR_TOP/lidar.pcd.bin" in key
        )
        assert (
            _get_object(owner_s3, bucket, source_key)
            == (cache / "content" / "samples/LIDAR_TOP/lidar.pcd.bin").read_bytes()
        )
        assert any(
            (payload := _get_object(owner_s3, bucket, key)).startswith(b"# .PCD v0.7")
            and b"DATA binary" in payload
            for key in keys_after_rollback
            if key not in {legacy["old_key"], directory_key}
        )

        success = await run_probe("replace", legacy["old_key"])
        record_property("workspace_seed_legacy_replacement", success.evidence)
        assert success.exit_code == 0, success.stderr
        replaced = _probe_result(success)
        info = replaced["info"]
        assert info["total_items"] == 3 and info["batches"] == 1
        assert info["replaced_legacy"] is True
        assert replaced["old_keys"] == [legacy["old_key"]]
        assert replaced["old_keys_before_commit"] == [True]
        assert replaced["old_keys_after_commit_before_cleanup"] == [True]
        assert replaced["old_keys_after_cleanup"] == [False]
        assert replaced["cleanup_called"] is True
        assert legacy["old_key"] not in await _object_keys(owner_s3, bucket)

        project_id = uuid.UUID(info["project_id"])
        dataset_id = uuid.UUID(info["dataset_id"])
        state = await _read_import_state(test_engine, dataset_id, project_id)
        _assert_real_object_bytes(owner_s3, bucket, cache / "content", state)
        assert state["project"]["display_id"] == "P-PC-DEV"
        assert state["project"]["name"] == NUSCENES_PROJECT_NAME
        assert state["project"]["tool_bindings"] == POINTCLOUD_TOOL_BINDINGS
        assert len(state["items"]) == 3 and len(state["tasks"]) == 1
        assert len(state["scenes"]) == 1
        assert state["scenes"][0]["name"] == "scene-0061"
        assert state["dataset"]["display_id"] == "DS-PC-DEV"
        assert state["dataset"]["name"] == "nuscenes-mini"
        assert state["links"] == 1
        current_rows = await _seed_rows(test_engine)
        assert current_rows["project"]["scene_mode"] is True
        assert current_rows["project"]["prefer_same_scene_continuation"] is True
        assert current_rows["project"]["tool_bindings"] == POINTCLOUD_TOOL_BINDINGS
        assert current_rows["dataset"]["file_count"] == 3
        batches = await _batch_rows(test_engine, project_id)
        assert len(batches) == 1 and batches[0]["total_tasks"] == 1

        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.projects SET name='drifted seed', scene_mode=false, "
                    "prefer_same_scene_continuation=false, tool_bindings='{}'::jsonb, "
                    "updated_at='2000-01-01T00:00:00Z' WHERE id=:id"
                ),
                {"id": project_id},
            )
        before_replay_keys = _object_snapshot(
            owner_s3, bucket, "__aap_trusted_nuscenes__-"
        )
        replay = await run_probe("replay")
        record_property("workspace_seed_existing_project_replay", replay.evidence)
        assert replay.exit_code == 0, replay.stderr
        replayed = _probe_result(replay)
        assert replayed["info"]["total_items"] == 0
        assert replayed["info"]["batches"] == 0
        assert replayed["old_keys"] == []
        current = await _read_import_state(test_engine, dataset_id, project_id)
        current_rows = await _seed_rows(test_engine)
        assert current["project"]["id"] == state["project"]["id"]
        assert current["dataset"]["id"] == state["dataset"]["id"]
        assert current["project"]["name"] == NUSCENES_PROJECT_NAME
        assert current["project"]["tool_bindings"] == POINTCLOUD_TOOL_BINDINGS
        assert current_rows["project"]["scene_mode"] is True
        assert current_rows["project"]["prefer_same_scene_continuation"] is True
        assert current_rows["project"]["tool_bindings"] == POINTCLOUD_TOOL_BINDINGS
        assert current_rows["project"]["updated_at"].year > 2000
        assert [row["id"] for row in current["items"]] == [
            row["id"] for row in state["items"]
        ]
        assert [row["id"] for row in current["tasks"]] == [
            row["id"] for row in state["tasks"]
        ]
        assert [row["id"] for row in current["scenes"]] == [
            row["id"] for row in state["scenes"]
        ]
        assert await _batch_rows(test_engine, project_id) == batches
        assert (
            _object_snapshot(owner_s3, bucket, "__aap_trusted_nuscenes__-")
            == before_replay_keys
        )
        await _assert_seed_privileges(test_engine)
    finally:
        try:
            if cli is not None:
                await asyncio.to_thread(cli.close)
        finally:
            destroy_scope(scope)
