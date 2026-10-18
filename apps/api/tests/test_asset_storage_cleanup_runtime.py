"""Opt-in real-container acceptance for the independent Assets cleanup worker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_PG_IMAGE = "postgres:16-alpine"
_CONTAINER_LABEL = "org.aap.acceptance"
_CONTAINER_ID_LABEL = "org.aap.acceptance-id"
_BUCKET_OWNER_TAG = "aap-worktree-owner"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PENDING_QUERY = """
SELECT operation_id, asset_id, bucket, prefix, created_at
FROM aap_assets_v1.pending_storage_deletions_v2(
    :bucket, NULL::timestamptz, NULL::uuid, NULL::uuid, 100
)
"""
_ACK_QUERY = """
SELECT aap_assets_v1.complete_storage_deletion(
    CAST(:operation_id AS uuid), CAST(:asset_id AS uuid)
)
"""
_UNLINK_QUERY = """
SELECT aap_workspace_v1.unlink_dataset(
    CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
    CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), :bucket,
    CAST(:audit_context AS jsonb)
)
"""


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the module-data verifier did not provide {name}")
    return value


def _docker(*arguments: str, timeout: int = 90) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"owned cleanup Docker command failed: {arguments[0]}")
    return result.stdout.strip()


def _inspect_docker(target: str) -> dict:
    try:
        values = json.loads(_docker("inspect", target, timeout=15))
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid cleanup identity record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("cleanup Docker identity was not unique")
    return values[0]


def _verify_postgres(cleanup_url: URL) -> None:
    container_id = _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
    container_name = _required_env("AAP_MODULE_DATA_PG_CONTAINER_NAME")
    identity = _required_env("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
    if (
        re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or re.fullmatch(r"aap-module-data-[0-9a-f]{12}", container_name) is None
        or re.fullmatch(r"[0-9a-f]{24}", identity) is None
    ):
        pytest.fail("the module-data verifier supplied an invalid PostgreSQL identity")
    observed = _inspect_docker(container_id)
    labels = observed.get("Config", {}).get("Labels") or {}
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        "5432/tcp"
    ) or []
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{container_name}"
        and observed.get("Config", {}).get("Image") == _PG_IMAGE
        and labels.get(_CONTAINER_LABEL) == "module-data-boundary"
        and labels.get(_CONTAINER_ID_LABEL) == identity
        and observed.get("State", {}).get("Running") is True
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and int(bindings[0].get("HostPort", 0)) == cleanup_url.port
    ):
        pytest.fail("PostgreSQL container ownership or loopback binding did not match")


def _verified_resource_manifest(test_engine, cleanup_url: URL) -> dict:
    tmpdir = Path(_required_env("TMPDIR")).resolve()
    if (
        tmpdir.name != "tmp"
        or tmpdir.parent.name != "test"
        or tmpdir.parent.parent.name != ".worktree"
    ):
        pytest.fail("the verifier did not provide its owned test resource topology")
    resource_root = tmpdir.parents[2]
    state_dir = resource_root / ".worktree"
    identity_path = state_dir / "identity.json"
    manifest_path = state_dir / "test" / "resources.json"
    if identity_path.is_symlink() or manifest_path.is_symlink():
        pytest.fail("the owned resource manifest must not be a symlink")
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail("the verifier's owned resource identity is unavailable")
    resource = manifest.get("resources") if isinstance(manifest, dict) else None
    if not isinstance(identity, dict) or not isinstance(resource, dict):
        pytest.fail("the verifier's resource manifest has an invalid shape")
    resource_id = identity.get("id")
    checkout = hashlib.sha256(str(resource_root).encode()).hexdigest()[:16]
    owner = f"aap-worktree:{resource_id}:test:{checkout}"
    if not (
        identity.get("schema") == 1
        and re.fullmatch(r"[0-9a-f]{16}", str(resource_id))
        and identity.get("root") == str(resource_root)
        and manifest.get("schema") == 1
        and resource.get("id") == resource_id
        and resource.get("root") == str(resource_root)
        and resource.get("mode") == "test"
        and resource.get("owner") == owner
        and resource.get("database") == test_engine.url.database
        and resource.get("database") == cleanup_url.database
        and resource.get("directory") == str(state_dir / "test")
        and manifest.get("postgres", {}).get("host") == "127.0.0.1"
        and manifest.get("postgres", {}).get("port") == cleanup_url.port
    ):
        pytest.fail("test resources do not match their owned identity")
    expected_buckets = {
        "MINIO_DATASETS_BUCKET": f"aap-wt-{resource_id}-test-datasets",
        "MINIO_MEDIA_CACHE_BUCKET": f"aap-wt-{resource_id}-test-media-cache",
    }
    buckets = resource.get("buckets")
    if not isinstance(buckets, dict) or any(
        buckets.get(name) != value for name, value in expected_buckets.items()
    ):
        pytest.fail("cleanup buckets do not match the owned resource identity")
    return resource


def _local_s3_client(settings, access_key: str, secret_key: str):
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = urlsplit(f"{scheme}://{settings.minio_endpoint}")
    if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("acceptance MinIO endpoint must be loopback-only")
    if not access_key or not secret_key:
        pytest.fail("the verifier did not provide scoped MinIO credentials")
    return boto3.client(
        "s3",
        endpoint_url=endpoint.geturl(),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=8,
            retries={"total_max_attempts": 1},
        ),
    )


def _container_s3_endpoint(settings) -> str:
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = urlsplit(f"{scheme}://{settings.minio_endpoint}")
    if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("acceptance MinIO endpoint must be loopback-only")
    return f"{scheme}://host.docker.internal:{endpoint.port or 9000}"


def _verify_bucket_owner(client, bucket: str, expected_owner: str) -> None:
    try:
        tags = client.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
    except Exception:
        pytest.fail("a cleanup bucket ownership tag could not be verified")
    if not any(
        item.get("Key") == _BUCKET_OWNER_TAG and item.get("Value") == expected_owner
        for item in tags
    ):
        pytest.fail("refusing to use a bucket not owned by this acceptance")


def _expect_s3_denied(operation) -> None:
    try:
        result = operation()
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code not in {"AccessDenied", "403", "Forbidden"}:
            pytest.fail("scoped MinIO credential failed with an unexpected response")
        return
    body = result.get("Body") if isinstance(result, dict) else None
    if body is not None:
        body.close()
    pytest.fail("scoped MinIO credential performed a forbidden operation")


def _object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        pytest.fail("MinIO object verification failed")


def _put(client, bucket: str, key: str, owned_objects: list[tuple[str, str]]) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=key.encode("utf-8"))
    owned_objects.append((bucket, key))


def _verify_scoped_s3_permissions(
    client,
    owner_client,
    *,
    media_bucket: str,
    datasets_bucket: str,
    target_prefix: str,
    target_key: str,
    source_key: str,
    video_key: str,
    owned_objects: list[tuple[str, str]],
) -> None:
    listed = client.list_objects_v2(
        Bucket=media_bucket,
        Prefix=target_prefix,
        MaxKeys=1,
    )
    assert listed.get("KeyCount") == 1
    _expect_s3_denied(lambda: client.list_objects_v2(Bucket=media_bucket, MaxKeys=1))
    _expect_s3_denied(
        lambda: client.list_objects_v2(Bucket=media_bucket, Prefix="videos/", MaxKeys=1)
    )
    _expect_s3_denied(lambda: client.get_object(Bucket=media_bucket, Key=target_key))
    _expect_s3_denied(lambda: client.get_object(Bucket=datasets_bucket, Key=source_key))
    _expect_s3_denied(lambda: client.delete_object(Bucket=media_bucket, Key=video_key))
    _expect_s3_denied(
        lambda: client.delete_object(Bucket=datasets_bucket, Key=source_key)
    )

    forbidden_put = target_prefix + "forbidden-put.webp"
    owned_objects.append((media_bucket, forbidden_put))
    _expect_s3_denied(
        lambda: client.put_object(
            Bucket=media_bucket, Key=forbidden_put, Body=b"must be denied"
        )
    )

    probe_prefix = f"image-pyramids/{uuid.uuid4()}/"
    probe_key = probe_prefix + "delete-permission-probe"
    _put(owner_client, media_bucket, probe_key, owned_objects)
    client.delete_object(Bucket=media_bucket, Key=probe_key)


def _sqlstate(error: DBAPIError) -> str | None:
    pending = [error.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        pending.extend(
            nested
            for nested in (
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
            if nested is not None
        )
    return None


async def _assert_denied(connection, statement: str, parameters: dict | None = None):
    with pytest.raises(DBAPIError) as denied:
        await connection.execute(text(statement), parameters or {})
    assert _sqlstate(denied.value) == "42501"
    await connection.rollback()


async def _assert_cleanup_login(test_engine, cleanup_engine, media_bucket: str):
    async with cleanup_engine.connect() as cleanup:
        assert (
            await cleanup.scalar(text("SELECT current_user")) == "asset_cleanup_client"
        )
        assert (
            await cleanup.scalar(text("SELECT session_user")) == "asset_cleanup_client"
        )
        assert await cleanup.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', "
                "'EXECUTE')"
            )
        )
        assert await cleanup.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'aap_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')"
            )
        )
        assert await cleanup.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'aap_assets_v1.claim_image_garbage(uuid,integer,integer)', 'EXECUTE')"
            )
        )
        assert await cleanup.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'aap_assets_v1.complete_image_garbage(uuid,uuid)', 'EXECUTE')"
            )
        )
        assert not await cleanup.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'aap_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')"
            )
        )
        assert not await cleanup.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'aap_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')"
            )
        )
        pending = (
            (await cleanup.execute(text(_PENDING_QUERY), {"bucket": media_bucket}))
            .mappings()
            .all()
        )
        assert pending == []
        await _assert_denied(
            cleanup, "SELECT count(*) FROM public.asset_storage_deletions"
        )
        await _assert_denied(
            cleanup, "SELECT count(*) FROM public.dataset_source_deletion_operations"
        )
        await _assert_denied(cleanup, "SELECT count(*) FROM public.dataset_items")


def _container_database_url(cleanup_url: URL) -> str:
    return cleanup_url.set(
        drivername="postgresql", host="host.docker.internal"
    ).render_as_string(hide_password=False)


def _write_worker_environment(
    path: Path,
    *,
    database_url: str,
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
) -> None:
    values = {
        "ASSET_CLEANUP_DATABASE_URL": database_url,
        "ASSET_CLEANUP_S3_ENDPOINT": endpoint,
        "ASSET_CLEANUP_S3_REGION": "us-east-1",
        "ASSET_CLEANUP_BUCKET": bucket,
        "ASSET_CLEANUP_S3_ACCESS_KEY": access_key,
        "ASSET_CLEANUP_S3_SECRET_KEY": secret_key,
        "ASSET_CLEANUP_POLL_SECONDS": "0.1",
        "ASSET_CLEANUP_PAGE_SIZE": "1",
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        pytest.fail("cleanup worker environment contains a line break")
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _build_worker_image(tag: str, identity: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            "apps/assets-cleanup/Dockerfile",
            "--tag",
            tag,
            "--label",
            f"{_CONTAINER_LABEL}=module-data-boundary",
            "--label",
            f"{_CONTAINER_ID_LABEL}={identity}",
            ".",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("standalone Assets cleanup image build failed")


def _start_worker(name: str, image: str, identity: str, environment_file: Path) -> str:
    container_id = _docker(
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"{_CONTAINER_LABEL}=module-data-boundary",
        "--label",
        f"{_CONTAINER_ID_LABEL}={identity}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--health-interval",
        "1s",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--stop-timeout",
        "45",
        "--env-file",
        str(environment_file),
        image,
        timeout=120,
    )
    return container_id


def _verify_worker_container(container_id: str, name: str, image: str, identity: str):
    observed = _inspect_docker(container_id)
    labels = observed.get("Config", {}).get("Labels") or {}
    config = observed.get("Config", {})
    environment = config.get("Env") or []
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{name}"
        and config.get("Image") == image
        and config.get("User") == "10001:10001"
        and labels.get(_CONTAINER_LABEL) == "module-data-boundary"
        and labels.get(_CONTAINER_ID_LABEL) == identity
        and not observed.get("Mounts")
        and not (observed.get("NetworkSettings", {}).get("Ports") or {})
        and config.get("Cmd") == ["python", "-m", "aap_assets_cleanup"]
        and not any(
            value.startswith(
                (
                    "DATABASE_URL=",
                    "REDIS_URL=",
                    "CELERY_",
                    "MINIO_",
                    "AWS_ACCESS_KEY_ID=",
                    "AWS_SECRET_ACCESS_KEY=",
                )
            )
            for value in environment
        )
    ):
        pytest.fail(
            "cleanup container is not isolated to its standalone runtime contract"
        )


async def _wait_healthy(container_id: str, timeout: float = 100) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = await asyncio.to_thread(_inspect_docker, container_id)
        state = observed.get("State", {})
        if state.get("Running") is not True:
            pytest.fail("standalone cleanup container exited before becoming ready")
        health = state.get("Health", {}).get("Status")
        if health == "healthy":
            return
        if health == "unhealthy":
            pytest.fail("standalone cleanup container failed its readiness check")
        await asyncio.sleep(0.2)
    pytest.fail("standalone cleanup container did not become ready")


def _stop_remove_container(container_id: str, identity: str) -> None:
    observed = _inspect_docker(container_id)
    labels = observed.get("Config", {}).get("Labels") or {}
    if not (
        observed.get("Id") == container_id
        and labels.get(_CONTAINER_LABEL) == "module-data-boundary"
        and labels.get(_CONTAINER_ID_LABEL) == identity
    ):
        pytest.fail("refusing to stop a container outside this acceptance")
    if observed.get("State", {}).get("Running") is True:
        _docker("stop", "--time", "45", container_id, timeout=60)
    stopped = _inspect_docker(container_id)
    stopped_labels = stopped.get("Config", {}).get("Labels") or {}
    if (
        stopped.get("Id") == container_id
        and stopped_labels.get(_CONTAINER_LABEL) == "module-data-boundary"
        and stopped_labels.get(_CONTAINER_ID_LABEL) == identity
    ):
        _docker("rm", container_id, timeout=30)


def _remove_worker_image(tag: str, identity: str) -> None:
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid cleanup image identity")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("cleanup image identity was not unique")
    labels = values[0].get("Config", {}).get("Labels") or {}
    if not (
        labels.get(_CONTAINER_LABEL) == "module-data-boundary"
        and labels.get(_CONTAINER_ID_LABEL) == identity
        and tag in (values[0].get("RepoTags") or [])
    ):
        pytest.fail("refusing to remove an image outside this acceptance")
    _docker("image", "rm", tag, timeout=30)


def _put_objects(
    client,
    media_bucket,
    dataset_bucket,
    target_prefix,
    sibling_prefix,
    task_prefix,
    source_keys,
    owned,
):
    target_keys = [
        target_prefix + "manifest.json",
        target_prefix + "g1/0/0/0.webp",
    ]
    sibling_keys = [
        sibling_prefix + "manifest.json",
        sibling_prefix + "g1/0/0/0.webp",
    ]
    task_keys = [
        task_prefix + "manifest.json",
        task_prefix + "g1/0/0/0.webp",
    ]
    video_key = f"videos/cleanup-acceptance/{uuid.uuid4()}/frame.webp"
    adjacent_prefix = f"image-pyramids/{uuid.uuid4()}/"
    adjacent_key = adjacent_prefix + "keep.webp"
    for key in target_keys + sibling_keys + task_keys + [video_key, adjacent_key]:
        _put(client, media_bucket, key, owned)
    _put(client, dataset_bucket, source_keys[0], owned)
    _put(client, dataset_bucket, source_keys[1], owned)
    return {
        "target": target_keys,
        "late": target_prefix + "g1/late-crash-write.webp",
        "sibling": sibling_keys,
        "task": task_keys,
        "task_late": task_prefix + "g1/late-crash-write.webp",
        "video": video_key,
        "adjacent": adjacent_key,
        "sources": source_keys,
    }


async def _seed_dataset_case(test_engine, *, lease_seconds: int) -> dict:
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User
    from tests.factory import make_user_dict

    namespace = uuid.uuid4()
    actor = User(
        **make_user_dict(
            "super_admin",
            f"cleanup-{namespace.hex}@local.test",
            "Cleanup Acceptance",
        )
    )
    dataset_id = uuid.uuid4()
    project_id = uuid.uuid4()
    association_id = uuid.uuid4()
    target_item_id, sibling_item_id = uuid.uuid4(), uuid.uuid4()
    target_asset_id, sibling_asset_id = uuid.uuid4(), uuid.uuid4()
    task_id, task_asset_id = uuid.uuid4(), uuid.uuid4()
    target_source_key = f"cleanup-runtime/{dataset_id}/{target_item_id}.jpg"
    sibling_source_key = f"cleanup-runtime/{dataset_id}/{sibling_item_id}.jpg"
    now = datetime.now(timezone.utc)
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    dataset = Dataset(
        id=dataset_id,
        display_id=f"DSC{namespace.hex[:12]}",
        name=f"cleanup-{namespace.hex}",
        description="",
        data_type="image",
        is_temporal=False,
        file_count=2,
        created_by=actor.id,
    )
    project = Project(
        id=project_id,
        display_id=f"CP{namespace.hex[:12]}",
        name=f"cleanup-project-{namespace.hex}",
        type_label="Image",
        type_key="image",
        data_type="image",
        owner_id=actor.id,
        total_tasks=1,
    )
    target_item = DatasetItem(
        id=target_item_id,
        dataset_id=dataset_id,
        file_name=f"target-{target_item_id}.jpg",
        file_path=target_source_key,
        file_type="image",
        file_size=128,
    )
    sibling_item = DatasetItem(
        id=sibling_item_id,
        dataset_id=dataset_id,
        file_name=f"sibling-{sibling_item_id}.jpg",
        file_path=sibling_source_key,
        file_type="image",
        file_size=128,
    )
    association = ProjectDataset(
        id=association_id,
        project_id=project_id,
        dataset_id=dataset_id,
    )
    task = Task(
        id=task_id,
        project_id=project_id,
        dataset_item_id=sibling_item_id,
        display_id=f"CT{namespace.hex[:12]}",
        file_name=sibling_item.file_name,
        file_path=sibling_item.file_path,
        file_type="image",
        status="pending",
    )
    target_asset = ImagePyramidAsset(
        id=target_asset_id,
        dataset_item_id=target_item_id,
        task_id=None,
        profile_version="cleanup-runtime-v1",
        building_generation=1,
    )
    sibling_asset = ImagePyramidAsset(
        id=sibling_asset_id,
        dataset_item_id=sibling_item_id,
        task_id=None,
        profile_version="cleanup-runtime-v1",
        active_generation=1,
    )
    task_asset = ImagePyramidAsset(
        id=task_asset_id,
        task_id=task_id,
        profile_version="cleanup-runtime-v1",
        building_generation=1,
    )
    target_generation = ImagePyramidGeneration(
        asset_id=target_asset_id,
        generation=1,
        source_identity=target_source_key,
        status="building",
        lease_token=uuid.uuid4(),
        lease_expires_at=lease_expires_at,
        normalization_version="cleanup-runtime-v1",
    )
    sibling_generation = ImagePyramidGeneration(
        asset_id=sibling_asset_id,
        generation=1,
        source_identity=sibling_source_key,
        status="ready",
        normalization_version="cleanup-runtime-v1",
    )
    task_generation = ImagePyramidGeneration(
        asset_id=task_asset_id,
        generation=1,
        source_identity=sibling_source_key,
        status="building",
        lease_token=uuid.uuid4(),
        lease_expires_at=lease_expires_at,
        normalization_version="cleanup-runtime-v1",
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as owner:
        database = await owner.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail(
                "refusing to seed source deletion outside the disposable test DB"
            )
        owner.add(actor)
        await owner.flush()
        owner.add_all((dataset, project))
        await owner.flush()
        owner.add_all((target_item, sibling_item))
        await owner.flush()
        owner.add_all((association, task))
        await owner.flush()
        owner.add_all((target_asset, sibling_asset, task_asset))
        await owner.flush()
        owner.add_all((target_generation, sibling_generation, task_generation))

    return {
        "actor_id": actor.id,
        "dataset_id": dataset_id,
        "project_id": project_id,
        "association_id": association_id,
        "target_item_id": target_item_id,
        "sibling_item_id": sibling_item_id,
        "target_asset_id": target_asset_id,
        "sibling_asset_id": sibling_asset_id,
        "task_id": task_id,
        "task_asset_id": task_asset_id,
        "target_source_key": target_source_key,
        "sibling_source_key": sibling_source_key,
        "target_prefix": f"image-pyramids/{target_asset_id}/",
        "sibling_prefix": f"image-pyramids/{sibling_asset_id}/",
        "task_prefix": f"image-pyramids/{task_asset_id}/",
        "lease_expires_at": lease_expires_at,
    }


async def _call_native_item_delete(
    append_engine,
    *,
    operation_id,
    actor_id,
    dataset_id,
    item_id,
    bucket,
) -> dict:
    async with append_engine.begin() as workflow:
        if await workflow.scalar(text("SELECT current_user")) != "append_client":
            pytest.fail("native source deletion did not use the workflow LOGIN")
        result = await workflow.scalar(
            text(
                "SELECT aap_assets_v1.delete_dataset_item("
                "CAST(:operation AS uuid), CAST(:actor AS uuid), "
                "CAST(:dataset AS uuid), CAST(:item AS uuid), :bucket, NULL::jsonb)"
            ),
            {
                "operation": operation_id,
                "actor": actor_id,
                "dataset": dataset_id,
                "item": item_id,
                "bucket": bucket,
            },
        )
    if not isinstance(result, dict):
        pytest.fail("Assets returned an invalid native source deletion receipt")
    return result


async def _call_native_dataset_unlink(
    append_engine,
    *,
    operation_id: uuid.UUID,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    bucket: str,
) -> dict:
    audit_context = json.dumps(
        {
            "method": "DELETE",
            "path": f"/api/v1/projects/{project_id}/datasets/{dataset_id}",
            "ip": "127.0.0.1",
            "request_id": str(operation_id),
        }
    )
    async with append_engine.begin() as workflow:
        if await workflow.scalar(text("SELECT current_user")) != "append_client":
            pytest.fail("native dataset unlink did not use the workflow LOGIN")
        result = await workflow.scalar(
            text(_UNLINK_QUERY),
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project_id,
                "bucket": bucket,
                "audit_context": audit_context,
            },
        )
    if not isinstance(result, dict):
        pytest.fail("Workspace returned an invalid native dataset unlink receipt")
    return result


async def _outbox_row(test_engine, operation_id: uuid.UUID) -> dict | None:
    async with test_engine.connect() as owner:
        result = await owner.execute(
            text(
                "SELECT operation_id, asset_id, bucket, prefix, not_before, completed_at "
                "FROM public.asset_storage_deletions WHERE operation_id=:operation_id"
            ),
            {"operation_id": operation_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _pending_count(test_engine, bucket: str) -> int:
    async with test_engine.connect() as owner:
        return await owner.scalar(
            text(
                "SELECT count(*) FROM public.asset_storage_deletions "
                "WHERE bucket=:bucket AND completed_at IS NULL"
            ),
            {"bucket": bucket},
        )


async def _assert_native_source_state(test_engine, fixture: dict) -> None:
    async with test_engine.connect() as owner:
        target = await owner.scalar(
            text("SELECT id FROM public.dataset_items WHERE id=:id"),
            {"id": fixture["target_item_id"]},
        )
        sibling = await owner.scalar(
            text("SELECT id FROM public.dataset_items WHERE id=:id"),
            {"id": fixture["sibling_item_id"]},
        )
        dataset_count = await owner.scalar(
            text("SELECT file_count FROM public.datasets WHERE id=:id"),
            {"id": fixture["dataset_id"]},
        )
        target_asset = await owner.scalar(
            text("SELECT id FROM public.image_pyramid_assets WHERE id=:id"),
            {"id": fixture["target_asset_id"]},
        )
        sibling_asset = await owner.scalar(
            text("SELECT id FROM public.image_pyramid_assets WHERE id=:id"),
            {"id": fixture["sibling_asset_id"]},
        )
        task = await owner.scalar(
            text("SELECT id FROM public.tasks WHERE id=:id"),
            {"id": fixture["task_id"]},
        )
        task_asset = await owner.scalar(
            text("SELECT id FROM public.image_pyramid_assets WHERE id=:id"),
            {"id": fixture["task_asset_id"]},
        )
        association = await owner.scalar(
            text("SELECT id FROM public.project_datasets WHERE id=:id"),
            {"id": fixture["association_id"]},
        )
    assert target is None
    assert sibling == fixture["sibling_item_id"]
    assert dataset_count == 1
    assert target_asset is None
    assert sibling_asset == fixture["sibling_asset_id"]
    assert task == fixture["task_id"]
    assert task_asset == fixture["task_asset_id"]
    assert association == fixture["association_id"]


async def _assert_native_unlink_state(test_engine, fixture: dict) -> None:
    async with test_engine.connect() as owner:
        task = await owner.scalar(
            text("SELECT id FROM public.tasks WHERE id=:id"),
            {"id": fixture["task_id"]},
        )
        association = await owner.scalar(
            text("SELECT id FROM public.project_datasets WHERE id=:id"),
            {"id": fixture["association_id"]},
        )
        project_total = await owner.scalar(
            text("SELECT total_tasks FROM public.projects WHERE id=:id"),
            {"id": fixture["project_id"]},
        )
        task_asset = await owner.scalar(
            text("SELECT id FROM public.image_pyramid_assets WHERE id=:id"),
            {"id": fixture["task_asset_id"]},
        )
        sibling_item = await owner.scalar(
            text("SELECT id FROM public.dataset_items WHERE id=:id"),
            {"id": fixture["sibling_item_id"]},
        )
    assert task is None
    assert association is None
    assert project_total == 0
    assert task_asset is None
    assert sibling_item == fixture["sibling_item_id"]


async def _set_complete_permission(test_engine, *, enabled: bool) -> None:
    statement = (
        "GRANT EXECUTE ON FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid) "
        "TO asset_cleanup_client"
        if enabled
        else "REVOKE EXECUTE ON FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid) "
        "FROM asset_cleanup_client"
    )
    async with test_engine.begin() as owner:
        await owner.exec_driver_sql(statement)


async def _wait_until(check, *, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.1)
    pytest.fail(message)


async def _wait_target_prefix_gone(client, bucket: str, keys: list[str]) -> None:
    async def all_gone():
        statuses = await asyncio.gather(
            *(asyncio.to_thread(_object_exists, client, bucket, key) for key in keys)
        )
        return not any(statuses)

    await _wait_until(
        all_gone,
        timeout=45,
        message="cleanup container did not delete the eligible target prefix",
    )


async def _wait_outbox_complete(test_engine, operation_id: uuid.UUID) -> dict:
    async def completed():
        row = await _outbox_row(test_engine, operation_id)
        return row if row and row["completed_at"] is not None else None

    return await _wait_until(
        completed,
        timeout=45,
        message="cleanup restart did not acknowledge the durable outbox row",
    )


async def _insert_poison_page_row(
    test_engine,
    *,
    operation_id: uuid.UUID,
    asset_id: uuid.UUID,
    bucket: str,
) -> None:
    async with test_engine.begin() as owner:
        database = await owner.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail("refusing to seed cleanup work outside the disposable test DB")
        await owner.execute(
            text(
                "INSERT INTO public.asset_storage_deletions "
                "(operation_id, asset_id, bucket, prefix, created_at, not_before) "
                "VALUES (:operation_id, :asset_id, :bucket, :prefix, "
                "pg_catalog.statement_timestamp() - interval '1 minute', "
                "pg_catalog.statement_timestamp())"
            ),
            {
                "operation_id": operation_id,
                "asset_id": asset_id,
                "bucket": bucket,
                "prefix": f"image-pyramids/{asset_id}/",
            },
        )


async def _cleanup_database_rows(
    test_engine,
    fixture: dict,
    operation_id: uuid.UUID,
    unlink_operation_id: uuid.UUID,
    poison_operation_id: uuid.UUID | None,
):
    async with test_engine.begin() as owner:
        database = await owner.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail("refusing cleanup outside the disposable test DB")
        await owner.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
        await owner.execute(
            text(
                "DELETE FROM public.audit_logs "
                "WHERE detail_json ->> 'operation_id' IN "
                "(:operation_id, :unlink_operation_id)"
            ),
            {
                "operation_id": str(operation_id),
                "unlink_operation_id": str(unlink_operation_id),
            },
        )
        await owner.execute(
            text(
                "DELETE FROM public.dataset_source_deletion_operations "
                "WHERE operation_id=:operation_id"
            ),
            {"operation_id": operation_id},
        )
        await owner.execute(
            text(
                "DELETE FROM public.dataset_unlink_operations "
                "WHERE operation_id=:operation_id"
            ),
            {"operation_id": unlink_operation_id},
        )
        await owner.execute(
            text(
                "DELETE FROM public.asset_storage_deletions WHERE operation_id IN "
                "(:operation_id, :unlink_operation_id)"
            ),
            {
                "operation_id": operation_id,
                "unlink_operation_id": unlink_operation_id,
            },
        )
        if poison_operation_id is not None:
            await owner.execute(
                text(
                    "DELETE FROM public.asset_storage_deletions "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": poison_operation_id},
            )
        await owner.execute(
            text("DELETE FROM public.tasks WHERE id=:task_id"),
            {"task_id": fixture["task_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.project_datasets WHERE id=:association_id"),
            {"association_id": fixture["association_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.datasets WHERE id=:dataset_id"),
            {"dataset_id": fixture["dataset_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.projects WHERE id=:project_id"),
            {"project_id": fixture["project_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.users WHERE id=:actor_id"),
            {"actor_id": fixture["actor_id"]},
        )


async def test_standalone_cleanup_container_respects_source_lease_and_recovers_lost_ack(
    test_engine,
):
    from app.config import settings

    raw_cleanup_url = _required_env("AAP_MODULE_DATA_ASSET_CLEANUP_URL")
    cleanup_url = make_url(raw_cleanup_url)
    raw_append_url = _required_env("AAP_MODULE_DATA_APPEND_URL")
    append_url = make_url(raw_append_url)
    target_url = test_engine.url
    if not (
        cleanup_url.drivername == "postgresql+asyncpg"
        and cleanup_url.username == "asset_cleanup_client"
        and cleanup_url.host == "127.0.0.1"
        and cleanup_url.port == target_url.port
        and cleanup_url.database == target_url.database
        and cleanup_url.database
        and cleanup_url.database.endswith("_test")
        and cleanup_url.password
        and not cleanup_url.query
        and append_url.drivername == "postgresql+asyncpg"
        and append_url.username == "append_client"
        and append_url.host == "127.0.0.1"
        and append_url.port == target_url.port
        and append_url.database == target_url.database
        and append_url.password
        and not append_url.query
    ):
        pytest.fail("module LOGIN URLs do not match the disposable test database")
    _verify_postgres(cleanup_url)
    resource = _verified_resource_manifest(test_engine, cleanup_url)

    media_bucket = settings.minio_media_cache_bucket
    datasets_bucket = settings.minio_datasets_bucket
    if (
        media_bucket != resource["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
        or datasets_bucket != resource["buckets"]["MINIO_DATASETS_BUCKET"]
    ):
        pytest.fail("app buckets do not match the verifier-owned resources")
    owner_client = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    _verify_bucket_owner(owner_client, media_bucket, resource["owner"])
    _verify_bucket_owner(owner_client, datasets_bucket, resource["owner"])
    cleanup_s3_key = _required_env("AAP_MODULE_DATA_ASSET_CLEANUP_S3_ACCESS_KEY")
    cleanup_s3_secret = _required_env("AAP_MODULE_DATA_ASSET_CLEANUP_S3_SECRET_KEY")
    if (
        cleanup_s3_key == settings.minio_access_key
        or cleanup_s3_secret == settings.minio_secret_key
    ):
        pytest.fail("cleanup worker must not reuse the general MinIO principal")
    scoped_client = _local_s3_client(settings, cleanup_s3_key, cleanup_s3_secret)
    cleanup_engine = create_async_engine(
        cleanup_url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    append_engine = create_async_engine(
        append_url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )

    image_identity = uuid.uuid4().hex[:24]
    image_tag = f"aap-assets-cleanup:acceptance-{image_identity[:12]}"
    container_name = f"aap-assets-cleanup-{image_identity[:12]}"
    container_id: str | None = None
    image_built = False
    ack_revoked = False
    fixture: dict | None = None
    operation_id = uuid.uuid4()
    unlink_operation_id = uuid.uuid4()
    poison_operation_id = uuid.uuid4()
    poison_asset_id = uuid.uuid4()
    poison_inserted = False
    owned_objects: list[tuple[str, str]] = []
    tmpdir = Path(_required_env("TMPDIR")).resolve()

    try:
        if await _pending_count(test_engine, media_bucket) != 0:
            pytest.fail("owned cleanup bucket already has pending work")
        _build_worker_image(image_tag, image_identity)
        image_built = True
        image = _inspect_docker(image_tag)
        image_labels = image.get("Config", {}).get("Labels") or {}
        if not (
            image_labels.get(_CONTAINER_LABEL) == "module-data-boundary"
            and image_labels.get(_CONTAINER_ID_LABEL) == image_identity
        ):
            pytest.fail("standalone worker image ownership could not be verified")

        fixture = await _seed_dataset_case(test_engine, lease_seconds=20)
        target_prefix = fixture["target_prefix"]
        sibling_prefix = fixture["sibling_prefix"]
        keys = _put_objects(
            owner_client,
            media_bucket,
            datasets_bucket,
            target_prefix,
            sibling_prefix,
            fixture["task_prefix"],
            [fixture["target_source_key"], fixture["sibling_source_key"]],
            owned_objects,
        )
        _verify_scoped_s3_permissions(
            scoped_client,
            owner_client,
            media_bucket=media_bucket,
            datasets_bucket=datasets_bucket,
            target_prefix=target_prefix,
            target_key=keys["target"][0],
            source_key=fixture["target_source_key"],
            video_key=keys["video"],
            owned_objects=owned_objects,
        )

        secret_directory = tempfile.TemporaryDirectory(
            dir=tmpdir, prefix="asset-cleanup-"
        )
        try:
            environment_file = Path(secret_directory.name) / "worker.env"
            _write_worker_environment(
                environment_file,
                database_url=_container_database_url(cleanup_url),
                endpoint=_container_s3_endpoint(settings),
                bucket=media_bucket,
                access_key=cleanup_s3_key,
                secret_key=cleanup_s3_secret,
            )
            container_id = _start_worker(
                container_name, image_tag, image_identity, environment_file
            )
            if not container_id:
                pytest.fail("cleanup container ID was unavailable")
            _verify_worker_container(
                container_id, container_name, image_tag, image_identity
            )
            await _wait_healthy(container_id)
            await _assert_cleanup_login(test_engine, cleanup_engine, media_bucket)

            reply = await _call_native_item_delete(
                append_engine,
                operation_id=operation_id,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                item_id=fixture["target_item_id"],
                bucket=media_bucket,
            )
            assert reply["schema"] == "aap-dataset-source-delete/v1"
            assert reply["ok"] is True
            assert reply["data"]["operation_id"] == str(operation_id)
            assert reply["data"]["queued_storage_deletions"] == 1
            await _assert_native_source_state(test_engine, fixture)

            unlink_reply = await _call_native_dataset_unlink(
                append_engine,
                operation_id=unlink_operation_id,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                bucket=media_bucket,
            )
            assert unlink_reply["schema"] == "aap-dataset-unlink/v1"
            assert unlink_reply["ok"] is True
            assert unlink_reply["data"]["operation_id"] == str(unlink_operation_id)
            assert unlink_reply["data"]["deleted_tasks"] == 1
            await _assert_native_unlink_state(test_engine, fixture)

            outbox = await _outbox_row(test_engine, operation_id)
            assert outbox is not None
            assert outbox["asset_id"] == fixture["target_asset_id"]
            assert outbox["bucket"] == media_bucket
            assert outbox["prefix"] == target_prefix
            assert outbox["completed_at"] is None
            assert outbox["not_before"] >= fixture["lease_expires_at"]
            assert outbox["not_before"] > datetime.now(timezone.utc) + timedelta(
                seconds=2
            )
            task_outbox = await _outbox_row(test_engine, unlink_operation_id)
            assert task_outbox is not None
            assert task_outbox["asset_id"] == fixture["task_asset_id"]
            assert task_outbox["bucket"] == media_bucket
            assert task_outbox["prefix"] == fixture["task_prefix"]
            assert task_outbox["completed_at"] is None
            assert task_outbox["not_before"] >= fixture["lease_expires_at"]
            assert task_outbox["not_before"] > datetime.now(timezone.utc) + timedelta(
                seconds=2
            )

            async with cleanup_engine.connect() as cleanup:
                early_ack = await cleanup.scalar(
                    text(_ACK_QUERY),
                    {
                        "operation_id": operation_id,
                        "asset_id": fixture["target_asset_id"],
                    },
                )
                early_task_ack = await cleanup.scalar(
                    text(_ACK_QUERY),
                    {
                        "operation_id": unlink_operation_id,
                        "asset_id": fixture["task_asset_id"],
                    },
                )
            assert early_ack is False
            assert early_task_ack is False
            assert (await _outbox_row(test_engine, operation_id))[
                "completed_at"
            ] is None
            assert (await _outbox_row(test_engine, unlink_operation_id))[
                "completed_at"
            ] is None

            await _set_complete_permission(test_engine, enabled=False)
            ack_revoked = True
            await _insert_poison_page_row(
                test_engine,
                operation_id=poison_operation_id,
                asset_id=poison_asset_id,
                bucket=media_bucket,
            )
            poison_inserted = True

            late_key = keys["late"]
            _put(owner_client, media_bucket, late_key, owned_objects)
            task_late_key = keys["task_late"]
            _put(owner_client, media_bucket, task_late_key, owned_objects)
            async with cleanup_engine.connect() as cleanup:
                scoped_pending = (
                    (
                        await cleanup.execute(
                            text(_PENDING_QUERY), {"bucket": media_bucket}
                        )
                    )
                    .mappings()
                    .all()
                )
            assert all(row["operation_id"] != operation_id for row in scoped_pending)
            assert all(
                row["operation_id"] != unlink_operation_id for row in scoped_pending
            )
            assert any(
                row["operation_id"] == poison_operation_id for row in scoped_pending
            )
            assert _object_exists(owner_client, media_bucket, late_key)
            assert _object_exists(owner_client, media_bucket, task_late_key)
            assert _object_exists(
                owner_client, datasets_bucket, fixture["target_source_key"]
            )
            assert _object_exists(owner_client, media_bucket, keys["sibling"][0])

            await _wait_target_prefix_gone(
                owner_client,
                media_bucket,
                keys["target"] + [late_key] + keys["task"] + [task_late_key],
            )
            pending_after_lost_ack = await _outbox_row(test_engine, operation_id)
            assert pending_after_lost_ack is not None
            assert pending_after_lost_ack["completed_at"] is None
            task_pending_after_lost_ack = await _outbox_row(
                test_engine, unlink_operation_id
            )
            assert task_pending_after_lost_ack is not None
            assert task_pending_after_lost_ack["completed_at"] is None
            poison_after_lost_ack = await _outbox_row(test_engine, poison_operation_id)
            assert poison_after_lost_ack is not None
            assert poison_after_lost_ack["completed_at"] is None
            assert _object_exists(owner_client, media_bucket, keys["sibling"][0])
            assert _object_exists(
                owner_client, datasets_bucket, fixture["target_source_key"]
            )
            await asyncio.to_thread(_docker, "stop", "--time", "45", container_id)
            stopped = _inspect_docker(container_id)
            assert stopped.get("State", {}).get("Running") is False

            await _set_complete_permission(test_engine, enabled=True)
            ack_revoked = False
            _docker("start", container_id, timeout=30)
            await _wait_healthy(container_id)
            completed = await _wait_outbox_complete(test_engine, operation_id)
            assert completed["completed_at"] is not None
            task_completed = await _wait_outbox_complete(
                test_engine, unlink_operation_id
            )
            assert task_completed["completed_at"] is not None
            if poison_inserted:
                poison = await _wait_outbox_complete(test_engine, poison_operation_id)
                assert poison["completed_at"] is not None
            _verify_expected_objects(
                owner_client, media_bucket, datasets_bucket, fixture, keys
            )
        finally:
            secret_directory.cleanup()
    finally:
        cleanup_errors: list[str] = []
        if container_id is not None:
            try:
                _stop_remove_container(container_id, image_identity)
            except Exception:
                cleanup_errors.append("worker container")
            container_id = None
        if ack_revoked:
            try:
                await _set_complete_permission(test_engine, enabled=True)
            except Exception:
                cleanup_errors.append("cleanup-role permission")
        if fixture is not None:
            try:
                await _cleanup_database_rows(
                    test_engine,
                    fixture,
                    operation_id,
                    unlink_operation_id,
                    poison_operation_id if poison_inserted else None,
                )
            except Exception:
                cleanup_errors.append("owned database rows")
        for bucket, key in owned_objects:
            try:
                await asyncio.to_thread(
                    owner_client.delete_object, Bucket=bucket, Key=key
                )
            except Exception:
                cleanup_errors.append("owned MinIO objects")
        for engine in (append_engine, cleanup_engine):
            try:
                await engine.dispose()
            except Exception:
                cleanup_errors.append("database engine")
        if image_built:
            try:
                _remove_worker_image(image_tag, image_identity)
            except Exception:
                cleanup_errors.append("acceptance worker image")
        for client in (owner_client, scoped_client):
            try:
                client.close()
            except Exception:
                cleanup_errors.append("MinIO client")
        if cleanup_errors:
            pytest.fail(
                "failed to clean one or more owned acceptance resources: "
                + ", ".join(sorted(set(cleanup_errors)))
            )


def _verify_expected_objects(client, media_bucket, datasets_bucket, fixture, keys):
    for key in keys["target"] + [keys["late"]] + keys["task"] + [keys["task_late"]]:
        assert not _object_exists(client, media_bucket, key)
    for key in keys["sibling"] + [keys["adjacent"], keys["video"]]:
        assert _object_exists(client, media_bucket, key)
    for key in keys["sources"]:
        assert _object_exists(client, datasets_bucket, key)
    assert fixture["target_item_id"] != fixture["sibling_item_id"]
