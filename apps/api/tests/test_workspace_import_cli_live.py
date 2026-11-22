"""Cold nuScenes CLI acceptance on a fenced operational database caller."""

from __future__ import annotations

import asyncio
from contextlib import closing
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

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
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
_MANAGED_DATASETS = "__aap_managed_datasets__"
_AUXILIARY_ORIGINALS = "__aap_auxiliary_originals__"
_PROVISIONING_FUNCTIONS = (
    "yoao_identity_v1.read_provisioning_account(text)",
    "yoao_identity_v1.create_provisioning_account(jsonb)",
)
_IDENTITY_CONSUMER_FUNCTIONS = (
    "yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid)",
    "yoao_identity_v1.read_actor_facts(uuid)",
    "yoao_identity_v1.lock_accounts_share(uuid[],boolean)",
    "yoao_identity_v1.lock_accounts_update(uuid[],boolean)",
    "yoao_identity_v1.lock_accounts_key_share(uuid[],boolean)",
    "yoao_identity_v1.check_accepted_api_key(uuid,uuid,text[])",
    "yoao_identity_v1.is_jti_revoked(uuid)",
    "yoao_identity_v1.get_credential_generation(uuid)",
)
_WORKSPACE_FUNCTIONS = (
    "yoao_workspace_v1.read_import_project(text)",
    "yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)",
    "yoao_workspace_v1.lock_project_manager(uuid,uuid)",
)
_ASSETS_FUNCTIONS = (
    "yoao_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)",
    "yoao_assets_v1.append_operator_items(uuid,uuid,jsonb)",
    "yoao_assets_v1.patch_operator_items(uuid,uuid,jsonb)",
    "yoao_assets_v1.recount_operator_items(uuid,uuid)",
    "yoao_assets_v1.read_operator_items(uuid,uuid,integer)",
)
_EMPTY_TABLES = (
    "users",
    "projects",
    "project_members",
    "project_templates",
    "datasets",
    "dataset_items",
    "dataset_assets",
    "dataset_asset_locations",
    "dataset_auxiliary_originals",
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
)


def _create_datasets_storage_scope(
    resources,
    owner_s3,
    bucket: str,
    settings,
    *,
    managed_originals: bool = False,
    directory_key: str | None = None,
    retired_key: str | None = None,
):
    """Grant the CLI only its managed writes and any exact legacy seed keys."""
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
    arn = f"arn:aws:s3:::{bucket}"
    statements = []
    if managed_originals:
        statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [
                    f"{arn}/{_MANAGED_DATASETS}/*/_objects/*",
                    f"{arn}/{_AUXILIARY_ORIGINALS}/*/*/payload",
                ],
            }
        )
    if directory_key is not None:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [f"{arn}/{directory_key}"],
            }
        )
    if retired_key is not None:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:DeleteObject"],
                "Resource": [f"{arn}/{retired_key}"],
            }
        )
    policy = {"Version": "2012-10-17", "Statement": statements}
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
        if not statements:
            return scope, _destroy_cleanup_storage_scope
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


def _assert_storage_denied(operation):
    with pytest.raises(ClientError) as denial:
        operation()
    assert denial.value.response["Error"]["Code"] in {"AccessDenied", "403"}


def _assert_managed_writer_scope(settings, resources, owner_s3, scope):
    datasets = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    unrelated = resources["buckets"]["MINIO_BUCKET"]
    assert unrelated != datasets
    _verify_bucket_owner(owner_s3, unrelated, resources["owner"])
    probe_id = uuid.uuid4()
    with closing(
        _local_s3_client(settings, scope["access_key"], scope["secret_key"])
    ) as client:
        assert scope["access_key"] != settings.minio_access_key
        assert scope["secret_key"] != settings.minio_secret_key
        for bucket, key in (
            (datasets, f"unrelated/{probe_id}"),
            (datasets, f"__aap_trusted_nuscenes__-{probe_id}/legacy/old.pcd"),
            (unrelated, f"{_MANAGED_DATASETS}/{probe_id}/_objects/{probe_id}/probe"),
            (unrelated, f"{_AUXILIARY_ORIGINALS}/{probe_id}/{probe_id}/payload"),
        ):
            _assert_storage_denied(
                lambda: client.put_object(Bucket=bucket, Key=key, Body=b"out of scope")
            )
        _assert_storage_denied(lambda: client.list_object_versions(Bucket=datasets))
        _assert_storage_denied(
            lambda: client.delete_object(
                Bucket=datasets,
                Key=f"{_AUXILIARY_ORIGINALS}/{probe_id}/{probe_id}/payload",
            )
        )


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
                            "WHERE ns.nspname='yoao_identity_v1' "
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


async def _assert_empty_owned_rows(test_engine, audit_baseline=None):
    async with test_engine.connect() as db:
        counts = {}
        for table in _EMPTY_TABLES:
            counts[table] = await db.scalar(
                text(f"SELECT count(*) FROM public.{table}")
            )
        audits = (
            await db.execute(
                text(
                    "SELECT action, pg_catalog.to_jsonb(audit)::text "
                    "FROM public.audit_logs AS audit ORDER BY id, created_at"
                )
            )
        ).all()
    assert all(count == 0 for count in counts.values()), counts
    if audit_baseline is None:
        actions = [row[0] for row in audits]
        assert len(actions) == 3
        assert actions.count("audit.append.module-data-verifier") == 1
        assert actions.count("audit.append.conformance") == 1
        assert (
            sum(
                re.fullmatch(r"probe\.audit\.daily\.[0-9a-f]{32}", action) is not None
                for action in actions
            )
            == 1
        )
    else:
        assert audits == audit_baseline
    return audits


def _object_versions(owner_s3, bucket: str) -> dict[tuple[str, str], tuple[bool, bool]]:
    assert owner_s3.get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"
    return {
        (entry["Key"], entry["VersionId"]): (entry["IsLatest"], is_marker)
        for page in owner_s3.get_paginator("list_object_versions").paginate(
            Bucket=bucket
        )
        for group, is_marker in (("Versions", False), ("DeleteMarkers", True))
        for entry in page.get(group, ())
    }


async def _managed_originals(test_engine, bucket: str, keys: set[str]) -> dict:
    async with test_engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id,dataset_id,owner_kind,object_key,logical_path,version_id,"
                        "etag,sha256,size_bytes,status,receipt "
                        "FROM public.managed_original_creations "
                        "WHERE bucket=:bucket AND object_key=ANY(CAST(:keys AS text[])) "
                        "AND status='verified'"
                    ),
                    {"bucket": bucket, "keys": sorted(keys)},
                )
            )
            .mappings()
            .all()
        )
    originals = {row["object_key"]: dict(row) for row in rows}
    assert len(rows) == len(originals) and originals.keys() == keys
    return originals


async def _assert_new_managed_versions(test_engine, owner_s3, bucket, before, after):
    assert all(after.get(identity) == state for identity, state in before.items())
    added = after.keys() - before.keys()
    assert added and all(after[identity] == (True, False) for identity in added)
    originals = await _managed_originals(test_engine, bucket, {key for key, _ in added})
    assert added == {(key, row["version_id"]) for key, row in originals.items()}
    assert {row["owner_kind"] for row in originals.values()} == {
        "dataset",
        "dataset_auxiliary",
    }
    assert len({row["dataset_id"] for row in originals.values()}) == 1
    return {
        key: _read_verified_original(owner_s3, bucket, row)
        for key, row in originals.items()
    }


async def _assert_reimport_transaction_rolled_back(test_engine, audit_baseline) -> None:
    await _assert_empty_owned_rows(test_engine, audit_baseline)


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
                        "SELECT id, name, source_format, source_metadata, nuscenes_map_original_id "
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
                        "SELECT i.id,i.file_path,i.file_type,i.file_size,i.content_hash,"
                        "i.scene_id,i.frame_index,i.metadata,i.nuscenes_raw_original_id,"
                        "l.creation_id,l.bucket AS original_bucket,l.object_key AS original_key "
                        "FROM public.dataset_items AS i "
                        "LEFT JOIN public.dataset_assets AS a ON a.dataset_item_id=i.id "
                        "LEFT JOIN public.dataset_asset_locations AS l ON l.asset_id=a.id "
                        "AND l.role='managed_original' AND l.reference_state='present' "
                        "WHERE i.dataset_id=:id ORDER BY i.file_path"
                    ),
                    {"id": dataset_id},
                )
            )
            .mappings()
            .all()
        )
        auxiliary = (
            (
                await db.execute(
                    text(
                        "SELECT id,kind,bucket,object_key,version_id,sha256,size_bytes "
                        "FROM public.dataset_auxiliary_originals WHERE dataset_id=:id"
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
        "auxiliary": {row["id"]: dict(row) for row in auxiliary},
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
    for scene_id in {pose["scene_id"] for pose in state["poses"]}:
        trajectory = [pose for pose in state["poses"] if pose["scene_id"] == scene_id]
        assert [pose["frame_index"] for pose in trajectory] == [0, 1]
        assert trajectory[0]["timestamp_us"] < trajectory[1]["timestamp_us"]
        assert trajectory[1]["ego_translation"][0] - trajectory[0]["ego_translation"][
            0
        ] == pytest.approx(2.0)
        assert all(pose["ego_rotation"] == [1.0, 0.0, 0.0, 0.0] for pose in trajectory)


def _get_object(owner_s3, bucket: str, key: str, version_id: str) -> bytes:
    assert version_id not in {None, "", "null"}
    response = owner_s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    with closing(response["Body"]) as body:
        assert response["VersionId"] == version_id
        payload = body.read()
    assert response["ContentLength"] == len(payload)
    return payload


def _read_verified_original(owner_s3, bucket: str, row: dict) -> bytes:
    dataset_id, creation_id = row["dataset_id"], row["id"]
    if row["owner_kind"] == "dataset":
        expected_key = f"{_MANAGED_DATASETS}/{dataset_id}/_objects/{creation_id}/{row['logical_path']}"
    else:
        assert row["owner_kind"] == "dataset_auxiliary"
        expected_key = f"{_AUXILIARY_ORIGINALS}/{dataset_id}/{creation_id}/payload"
    assert row["object_key"] == expected_key
    assert row["receipt"]["version_id"] == row["version_id"]
    assert row["receipt"]["etag"] == row["etag"]
    payload = _get_object(owner_s3, bucket, expected_key, row["version_id"])
    assert len(payload) == row["size_bytes"]
    assert hashlib.sha256(payload).hexdigest() == row["sha256"]
    return payload


async def _assert_real_object_bytes(
    test_engine, owner_s3, bucket: str, source_root: Path, state: dict
) -> set[tuple[str, str]]:
    dataset_id = state["dataset"]["id"]
    auxiliary_ids = {
        item["nuscenes_raw_original_id"]
        for item in state["items"]
        if item["file_type"] == "point_cloud"
    } | {scene["nuscenes_map_original_id"] for scene in state["scenes"]}
    assert None not in auxiliary_ids
    auxiliary = {key: state["auxiliary"][key] for key in auxiliary_ids}
    originals = await _managed_originals(
        test_engine,
        bucket,
        {item["file_path"] for item in state["items"]}
        | {row["object_key"] for row in auxiliary.values()},
    )
    assert all(row["dataset_id"] == dataset_id for row in originals.values())
    payloads = {
        key: _read_verified_original(owner_s3, bucket, row)
        for key, row in originals.items()
    }
    for aux in auxiliary.values():
        row = originals[aux["object_key"]]
        assert row["owner_kind"] == "dataset_auxiliary" and aux["bucket"] == bucket
        assert row["id"] == aux["id"] and row["version_id"] == aux["version_id"]
        assert row["sha256"] == aux["sha256"] and row["size_bytes"] == aux["size_bytes"]

    for item in state["items"]:
        key = item["file_path"]
        row = originals[key]
        assert item["original_bucket"] == bucket and item["original_key"] == key
        assert item["creation_id"] == row["id"] and row["owner_kind"] == "dataset"
        assert item["file_size"] == row["size_bytes"]
        assert item["content_hash"] == row["sha256"]
        assert item["metadata"]["storage_relative_path"] == row["logical_path"]
        payload = payloads[key]
        if item["file_type"] == "other":
            calibration = json.loads(payload)
            assert len(calibration["intrinsic"]) == 9
            assert len(calibration["extrinsic"]) == 16
            continue
        source = item["metadata"]["nuscenes_export"]
        raw_key = key
        if item["file_type"] == "point_cloud":
            assert payload.startswith(b"# .PCD v0.7") and b"DATA binary" in payload
            raw = auxiliary[item["nuscenes_raw_original_id"]]
            assert raw["kind"] == "raw_lidar"
            raw_key = raw["object_key"]
        else:
            assert item["file_type"] == "image" and payload.startswith(b"\xff\xd8")
        assert source["source_storage_key"] == raw_key
        assert source["source_sha256"] == originals[raw_key]["sha256"]
        assert source["source_file_size"] == originals[raw_key]["size_bytes"]
        assert (
            payloads[raw_key]
            == (source_root / source["sample_data"]["filename"]).read_bytes()
        )

    for scene in state["scenes"]:
        export = scene["source_metadata"]["nuscenes_export"]
        aux = auxiliary[scene["nuscenes_map_original_id"]]
        assert aux["kind"] == "map" and export["map_storage_key"] == aux["object_key"]
        assert export["map_sha256"] == aux["sha256"]
        assert export["map_file_size"] == aux["size_bytes"]
        assert (
            payloads[aux["object_key"]]
            == (source_root / export["map"]["filename"]).read_bytes()
        )
    return {(key, row["version_id"]) for key, row in originals.items()}


async def _install_task_trigger(
    test_engine, marker: str | None = None
) -> tuple[str, str]:
    """Fail task insertion, or pause it for the physical CLI cancellation case."""
    suffix = uuid.uuid4().hex[:12]
    function_name = f"workspace_import_cli_fail_{suffix}"
    trigger_name = f"workspace_import_cli_fail_{suffix}_trigger"
    function = f"public.{function_name}"
    trigger = f"public.{trigger_name}"
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
    audit_baseline = await _assert_empty_owned_rows(test_engine)
    from app.services import managed_original_creation
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(test_engine) as owner:
        await managed_original_creation.bootstrap(owner)

    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, datasets_bucket, resources["owner"])
    initial_versions = _object_versions(owner_s3, datasets_bucket)
    assert not initial_versions, (
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
        resources, owner_s3, datasets_bucket, settings, managed_originals=True
    )
    cli = None
    try:
        _assert_managed_writer_scope(settings, resources, owner_s3, scope)
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
            managed_original_writer=True,
        )
        await asyncio.to_thread(cli.start)
        await _assert_operational_role(test_engine)

        missing_capability_name = "workspace-import-missing-" + uuid.uuid4().hex[:8]
        create_signature = (
            "yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)"
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
            await _assert_reimport_transaction_rolled_back(test_engine, audit_baseline)
            await _assert_new_managed_versions(
                test_engine,
                owner_s3,
                datasets_bucket,
                initial_versions,
                _object_versions(owner_s3, datasets_bucket),
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
        versions_before_trigger = _object_versions(owner_s3, datasets_bucket)
        try:
            late_failure = await asyncio.to_thread(cli.run, dataset_name=trigger_name)
            record_property("workspace_import_late_task_failure", late_failure.evidence)
            assert not late_failure.timed_out
            assert late_failure.exit_code != 0, late_failure.stdout
            assert trigger_marker in late_failure.stderr, late_failure.stderr
            await _assert_reimport_transaction_rolled_back(test_engine, audit_baseline)
            await _assert_new_managed_versions(
                test_engine,
                owner_s3,
                datasets_bucket,
                versions_before_trigger,
                _object_versions(owner_s3, datasets_bucket),
            )
        finally:
            await _drop_task_trigger(test_engine, function, trigger)

        function, trigger = await _install_task_trigger(test_engine)
        versions_before_cancel = _object_versions(owner_s3, datasets_bucket)
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
            await _assert_empty_owned_rows(test_engine, audit_baseline)
            versions_during_cancel = _object_versions(owner_s3, datasets_bucket)
            assert await asyncio.to_thread(cli.stop_current_container)
            cancelled = await pending
            record_property("workspace_import_cancelled", cancelled.evidence)
            assert cancelled.exit_code in {137, 143}, cancelled.evidence
            assert cancelled.externally_stopped and not cancelled.timed_out
            await _wait_for_import_disconnect(test_engine)
            await _assert_reimport_transaction_rolled_back(test_engine, audit_baseline)
            assert versions_during_cancel == _object_versions(owner_s3, datasets_bucket)
            await _assert_new_managed_versions(
                test_engine,
                owner_s3,
                datasets_bucket,
                versions_before_cancel,
                versions_during_cancel,
            )
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
        published_versions = await _assert_real_object_bytes(
            test_engine, owner_s3, datasets_bucket, source_root, state
        )
        success_versions = _object_versions(owner_s3, datasets_bucket)
        assert success_versions == versions_during_cancel | dict.fromkeys(
            published_versions, (True, False)
        )
        assert published_versions.isdisjoint(versions_during_cancel)

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
        assert replay_state["auxiliary"] == state["auxiliary"]
        assert (
            await _assert_real_object_bytes(
                test_engine, owner_s3, datasets_bucket, source_root, replay_state
            )
            == published_versions
        )
        assert _object_versions(owner_s3, datasets_bucket) == success_versions
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
        assert stale_state["auxiliary"] == state["auxiliary"]
        assert _object_versions(owner_s3, datasets_bucket) == success_versions
    finally:
        try:
            if cli is not None:
                await asyncio.to_thread(cli.close)
        finally:
            destroy_scope(scope)
