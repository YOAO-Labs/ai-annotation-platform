"""Opt-in real worker acceptance for the Assets-owned storage cleanup outbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from celery import Celery
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.asset_storage_deletion import AssetStorageDeletion

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_TASK_NAME = "app.workers.asset_storage_cleanup.retry_asset_storage_deletions"
_PG_IMAGE = "postgres:16-alpine"
_REDIS_IMAGE = "redis:7-alpine"
_CONTAINER_LABEL = "org.aap.acceptance"
_CONTAINER_ID_LABEL = "org.aap.acceptance-id"
_REDIS_OWNER_LABEL = "org.aap.worktree-owner"
_BUCKET_OWNER_TAG = "aap-worktree-owner"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_API_ROOT = _REPO_ROOT / "apps" / "api"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the opted-in module-data verifier did not provide {name}")
    return value


def _inspect_docker(target: str) -> dict:
    result = subprocess.run(
        ["docker", "inspect", target],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("an acceptance-owned container could not be inspected")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid container identity record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("the acceptance container identity was not unique")
    return values[0]


def _verify_postgres(cleanup_url: URL) -> str:
    container_id = _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
    container_name = _required_env("AAP_MODULE_DATA_PG_CONTAINER_NAME")
    identity = _required_env("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
    if (
        re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or re.fullmatch(r"aap-module-data-[0-9a-f]{12}", container_name) is None
        or re.fullmatch(r"[0-9a-f]{24}", identity) is None
    ):
        pytest.fail("the module-data verifier supplied an invalid container identity")
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
    return container_id


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
        and resource.get("redis_name") == f"aap-wt-{resource_id}-test-redis"
        and resource.get("directory") == str(state_dir / "test")
        and manifest.get("postgres", {}).get("host") == "127.0.0.1"
        and manifest.get("postgres", {}).get("port") == cleanup_url.port
    ):
        pytest.fail("the test resources do not match their owned identity")
    expected_buckets = {
        "MINIO_DATASETS_BUCKET": f"aap-wt-{resource_id}-test-datasets",
        "MINIO_MEDIA_CACHE_BUCKET": f"aap-wt-{resource_id}-test-media-cache",
    }
    buckets = resource.get("buckets")
    if not isinstance(buckets, dict) or any(
        buckets.get(name) != value for name, value in expected_buckets.items()
    ):
        pytest.fail("the cleanup buckets do not match the owned resource identity")
    return resource


def _verify_redis(resource: dict, broker_url: str) -> None:
    parsed = urlsplit(broker_url)
    if not (
        parsed.scheme == "redis"
        and parsed.hostname == "127.0.0.1"
        and parsed.port is not None
        and parsed.path == "/0"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    ):
        pytest.fail("Celery must use the owned loopback Redis database 0")
    observed = _inspect_docker(resource["redis_name"])
    labels = observed.get("Config", {}).get("Labels") or {}
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        "6379/tcp"
    ) or []
    if not (
        observed.get("Name") == f"/{resource['redis_name']}"
        and observed.get("Config", {}).get("Image") == _REDIS_IMAGE
        and labels.get(_REDIS_OWNER_LABEL) == resource["owner"]
        and observed.get("State", {}).get("Running") is True
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and int(bindings[0].get("HostPort", 0)) == parsed.port
    ):
        pytest.fail("Celery Redis container ownership or binding did not match")


def _owned_s3_client(settings):
    endpoint = urlsplit(
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("the acceptance MinIO endpoint must be loopback-only")
    if not settings.minio_access_key or not settings.minio_secret_key:
        pytest.fail("the verifier did not configure its owned MinIO credentials")
    return boto3.client(
        "s3",
        endpoint_url=endpoint.geturl(),
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=8,
            retries={"max_attempts": 1},
        ),
    )


def _verify_bucket_owner(client, bucket: str, expected_owner: str) -> None:
    try:
        tags = client.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
    except Exception:
        pytest.fail("the cleanup bucket ownership tag could not be verified")
    if not any(
        item.get("Key") == _BUCKET_OWNER_TAG and item.get("Value") == expected_owner
        for item in tags
    ):
        pytest.fail("refusing to write to a bucket not owned by this acceptance")


def _cleanup_worker_environment(cleanup_url: URL, broker_url: str, settings) -> dict:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP")
        if key in os.environ
    }
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "ENVIRONMENT": settings.environment,
            "AAP_WORKTREE_MODE": "test",
            "DATABASE_URL": cleanup_url.render_as_string(hide_password=False),
            "MIGRATION_DATABASE_URL": "",
            "TEST_DATABASE_URL": "",
            "REDIS_URL": broker_url,
            "CELERY_BROKER_URL": broker_url,
            "CELERY_BROKER_READ_URL": broker_url,
            "CELERY_BROKER_WRITE_URL": broker_url,
            "CELERY_RESULT_BACKEND": broker_url,
            "MINIO_ENDPOINT": settings.minio_endpoint,
            "MINIO_ACCESS_KEY": settings.minio_access_key,
            "MINIO_SECRET_KEY": settings.minio_secret_key,
            "MINIO_USE_SSL": "true" if settings.minio_use_ssl else "false",
            "MINIO_BUCKET": settings.minio_bucket,
            "MINIO_DATASETS_BUCKET": settings.minio_datasets_bucket,
            "MINIO_BUG_REPORTS_BUCKET": settings.minio_bug_reports_bucket,
            "MINIO_MEDIA_CACHE_BUCKET": settings.minio_media_cache_bucket,
            "MINIO_AUDIT_ARCHIVE_BUCKET": settings.minio_audit_archive_bucket,
            "MINIO_IMPORT_BUCKET": settings.minio_import_bucket,
            "MINIO_EXPORT_BUCKET": settings.minio_export_bucket,
            "MINIO_AVATARS_BUCKET": settings.minio_avatars_bucket,
            "MINIO_TRAINING_BUCKET": settings.minio_training_bucket,
        }
    )
    return environment


async def _start_worker(queue: str, environment: dict) -> tuple:
    output = tempfile.TemporaryFile(mode="w+b")
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.workers.celery_app",
            "worker",
            "--pool=solo",
            "--concurrency=1",
            f"--queues={queue}",
            f"--hostname={queue}@%h",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
            cwd=_API_ROOT,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    except BaseException:
        output.close()
        raise
    return process, output


async def _stop_worker(worker: tuple) -> str:
    process, output = worker
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    output.flush()
    output.seek(0)
    log = output.read().decode("utf-8", errors="replace")
    output.close()
    return log


async def _send_task(producer: Celery, queue: str, operation_id: str) -> object:
    return await asyncio.to_thread(
        producer.send_task,
        _TASK_NAME,
        args=[operation_id, 100],
        queue=queue,
        task_id=str(uuid.uuid4()),
    )


async def _wait_success(delivery, worker: tuple) -> dict:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(delivery.get, timeout=50), timeout=60
        )
    except Exception:
        process = worker[0]
        if process.returncode is not None:
            pytest.fail("the owned Celery worker exited before completing cleanup")
        raise
    assert worker[0].returncode is None
    assert isinstance(result, dict)
    return result


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


async def _assert_table_rights_denied(connection, table: str) -> None:
    asset_id = "00000000-0000-0000-0000-000000000001"
    operation_id = "00000000-0000-0000-0000-000000000002"
    prefix = f"image-pyramids/{asset_id}/"
    if table == "asset_storage_deletions":
        statements = (
            (f"SELECT count(*) FROM public.{table}", {}),
            (
                f"INSERT INTO public.{table} (operation_id, asset_id, bucket, prefix) "
                "SELECT CAST(:operation_id AS uuid), CAST(:asset_id AS uuid), "
                "'acceptance-cache', :prefix WHERE false",
                {
                    "operation_id": operation_id,
                    "asset_id": asset_id,
                    "prefix": prefix,
                },
            ),
            (f"UPDATE public.{table} SET completed_at = completed_at WHERE false", {}),
            (f"DELETE FROM public.{table} WHERE false", {}),
        )
    else:
        statements = (
            (f"SELECT count(*) FROM public.{table}", {}),
            (
                f"INSERT INTO public.{table} (id) "
                "SELECT CAST(:operation_id AS uuid) WHERE false",
                {"operation_id": operation_id},
            ),
            (f"UPDATE public.{table} SET status = status WHERE false", {}),
            (f"DELETE FROM public.{table} WHERE false", {}),
        )
    for statement, parameters in statements:
        with pytest.raises(DBAPIError) as denied:
            await connection.execute(text(statement), parameters)
        assert _sqlstate(denied.value) == "42501"
        await connection.rollback()


def _put(client, bucket: str, key: str, owned_objects: list[tuple[str, str]]) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=key.encode("utf-8"))
    owned_objects.append((bucket, key))


def _object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        pytest.fail("MinIO object verification failed")


async def _seed_outbox(test_engine, operation_id: uuid.UUID, asset_ids, bucket: str):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as owner:
        database = await owner.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail("refusing to seed cleanup work outside the disposable test DB")
        owner.add_all(
            AssetStorageDeletion(
                operation_id=operation_id,
                asset_id=asset_id,
                bucket=bucket,
                prefix=f"image-pyramids/{asset_id}/",
            )
            for asset_id in asset_ids
        )


async def _assert_outbox(test_engine, operation_id: uuid.UUID, expected: dict) -> None:
    async with test_engine.connect() as owner:
        rows = (
            (
                await owner.execute(
                    text(
                        "SELECT asset_id, bucket, prefix, completed_at "
                        "FROM public.asset_storage_deletions "
                        "WHERE operation_id=:operation_id ORDER BY asset_id"
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == len(expected)
    assert [row["asset_id"] for row in rows] == sorted(expected)
    for row in rows:
        assert row["bucket"] == expected[row["asset_id"]]["bucket"]
        assert row["prefix"] == expected[row["asset_id"]]["prefix"]
        assert (row["completed_at"] is not None) is expected[row["asset_id"]][
            "completed"
        ]


async def _assert_cleanup_login(test_engine, cleanup_engine, operation_id, asset_ids):
    async with cleanup_engine.connect() as cleanup:
        assert (
            await cleanup.scalar(text("SELECT current_user")) == "asset_cleanup_client"
        )
        assert (
            await cleanup.scalar(text("SELECT session_user")) == "asset_cleanup_client"
        )
        pending = (
            (
                await cleanup.execute(
                    text(
                        "SELECT operation_id, asset_id, bucket, prefix "
                        "FROM aap_assets_v1.pending_storage_deletions(:operation_id, 100)"
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .all()
        )
        assert [row["asset_id"] for row in pending] == sorted(asset_ids)
        await cleanup.rollback()
        await _assert_table_rights_denied(cleanup, "asset_storage_deletions")
        await _assert_table_rights_denied(cleanup, "async_jobs")


def _write_test_objects(
    client,
    media_bucket: str,
    datasets_bucket: str,
    operation_assets: dict[uuid.UUID, dict],
    owned_objects: list[tuple[str, str]],
) -> dict[str, object]:
    for asset_id in operation_assets:
        keys = [
            f"image-pyramids/{asset_id}/manifest.json",
            f"image-pyramids/{asset_id}/0/0/0.jpg",
        ]
        operation_assets[asset_id]["keys"] = keys
        for key in keys:
            _put(client, media_bucket, key, owned_objects)
    adjacent_id = uuid.uuid4()
    adjacent_key = f"image-pyramids/{adjacent_id}/keep.bin"
    sibling_key = f"image-pyramids/{next(iter(operation_assets))}-adjacent/keep.bin"
    source_key = f"task-source/cleanup-acceptance/{uuid.uuid4()}.jpg"
    for key in (adjacent_key, sibling_key):
        _put(client, media_bucket, key, owned_objects)
    _put(client, datasets_bucket, source_key, owned_objects)
    return {
        "adjacent": (media_bucket, adjacent_key),
        "sibling": (media_bucket, sibling_key),
        "source": (datasets_bucket, source_key),
    }


def _verify_expected_objects(client, operation_assets: dict, sentinels: dict, removed):
    for asset_id, info in operation_assets.items():
        for key in info["keys"]:
            assert _object_exists(client, info["bucket"], key) is (
                asset_id not in removed
            )
    for bucket, key in sentinels.values():
        assert _object_exists(client, bucket, key)


async def test_asset_cleanup_celery_worker_recovers_pending_outbox_after_restart(
    test_engine,
):
    if os.name != "posix":
        pytest.skip("the isolated Celery process-group harness requires POSIX")

    from app.config import settings

    raw_cleanup_url = _required_env("AAP_MODULE_DATA_ASSET_CLEANUP_URL")
    cleanup_url = make_url(raw_cleanup_url)
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
    ):
        pytest.fail("cleanup LOGIN URL does not match the disposable test database")
    _verify_postgres(cleanup_url)
    resource = _verified_resource_manifest(test_engine, cleanup_url)

    broker_url = _required_env("CELERY_BROKER_URL")
    result_backend = _required_env("CELERY_RESULT_BACKEND")
    if result_backend != broker_url or settings.effective_celery_broker != broker_url:
        pytest.fail("Celery broker/result backend must use the owned Redis resource")
    _verify_redis(resource, broker_url)

    media_bucket = settings.minio_media_cache_bucket
    datasets_bucket = settings.minio_datasets_bucket
    if (
        media_bucket != resource["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
        or datasets_bucket != resource["buckets"]["MINIO_DATASETS_BUCKET"]
    ):
        pytest.fail("the app storage buckets do not match the verifier-owned buckets")
    owner = resource["owner"]
    client = _owned_s3_client(settings)
    _verify_bucket_owner(client, media_bucket, owner)
    _verify_bucket_owner(client, datasets_bucket, owner)

    cleanup_engine = create_async_engine(
        cleanup_url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_one, operation_two = uuid.uuid4(), uuid.uuid4()
    asset_ids = [uuid.uuid4() for _ in range(3)]
    operation_assets = {
        asset_ids[0]: {
            "operation_id": operation_one,
            "bucket": media_bucket,
            "prefix": f"image-pyramids/{asset_ids[0]}/",
        },
        asset_ids[1]: {
            "operation_id": operation_one,
            "bucket": media_bucket,
            "prefix": f"image-pyramids/{asset_ids[1]}/",
        },
        asset_ids[2]: {
            "operation_id": operation_two,
            "bucket": media_bucket,
            "prefix": f"image-pyramids/{asset_ids[2]}/",
        },
    }
    owned_objects: list[tuple[str, str]] = []
    workers: list[tuple] = []
    producer = None
    queue = f"asset-cleanup-acceptance-{uuid.uuid4().hex}"
    worker_environment = _cleanup_worker_environment(cleanup_url, broker_url, settings)
    completed_logs: list[str] = []

    try:
        sentinels = _write_test_objects(
            client,
            media_bucket,
            datasets_bucket,
            operation_assets,
            owned_objects,
        )
        await _seed_outbox(
            test_engine,
            operation_one,
            [asset_ids[0], asset_ids[1]],
            media_bucket,
        )
        await _seed_outbox(test_engine, operation_two, [asset_ids[2]], media_bucket)
        await _assert_outbox(
            test_engine,
            operation_one,
            {
                asset_id: {**info, "completed": False}
                for asset_id, info in operation_assets.items()
                if info["operation_id"] == operation_one
            },
        )
        await _assert_cleanup_login(
            test_engine,
            cleanup_engine,
            operation_two,
            [asset_ids[2]],
        )

        producer = Celery(
            "asset-cleanup-runtime-acceptance",
            broker=broker_url,
            backend=result_backend,
        )
        delivery_one = await _send_task(producer, queue, str(operation_one))
        worker_one = await _start_worker(queue, worker_environment)
        workers.append(worker_one)
        assert await _wait_success(delivery_one, worker_one) == {
            "completed": 2,
            "failed": 0,
        }
        expected_first = {
            asset_id: {**info, "completed": True}
            for asset_id, info in operation_assets.items()
            if info["operation_id"] == operation_one
        }
        await _assert_outbox(test_engine, operation_one, expected_first)
        await _assert_outbox(
            test_engine,
            operation_two,
            {asset_ids[2]: {**operation_assets[asset_ids[2]], "completed": False}},
        )
        _verify_expected_objects(
            client,
            operation_assets,
            sentinels,
            {asset_ids[0], asset_ids[1]},
        )
        completed_logs.append(await _stop_worker(worker_one))

        # The committed second operation stays queued while no worker is alive.
        delivery_two = await _send_task(producer, queue, str(operation_two))
        assert delivery_two.state == "PENDING"
        worker_two = await _start_worker(queue, worker_environment)
        workers.append(worker_two)
        assert await _wait_success(delivery_two, worker_two) == {
            "completed": 1,
            "failed": 0,
        }
        expected_second = {
            asset_ids[2]: {**operation_assets[asset_ids[2]], "completed": True}
        }
        await _assert_outbox(test_engine, operation_two, expected_second)
        _verify_expected_objects(client, operation_assets, sentinels, set(asset_ids))

        # Failure signal handling must not try to query AsyncJob rows under this LOGIN.
        failed_delivery = await _send_task(producer, queue, "not-a-uuid")
        error = await asyncio.wait_for(
            asyncio.to_thread(failed_delivery.get, timeout=45, propagate=False),
            timeout=55,
        )
        assert failed_delivery.state == "FAILURE"
        assert isinstance(error, ValueError)
        completed_logs.append(await _stop_worker(worker_two))
        assert all(
            "async_jobs signal _on_task_failure failed" not in log
            for log in completed_logs
        )

        async with cleanup_engine.connect() as cleanup:
            for operation_id in (operation_one, operation_two):
                pending = (
                    await cleanup.execute(
                        text(
                            "SELECT * FROM aap_assets_v1.pending_storage_deletions("
                            ":operation_id, 100)"
                        ),
                        {"operation_id": operation_id},
                    )
                ).all()
                assert pending == []
    finally:
        try:
            for worker in reversed(workers):
                if worker[0].returncode is None:
                    completed_logs.append(await _stop_worker(worker))
                elif not worker[1].closed:
                    worker[1].close()
        finally:
            if producer is not None:
                producer.close()
            try:
                async with maker.begin() as owner_session:
                    database = await owner_session.scalar(
                        text("SELECT current_database()")
                    )
                    if database != target_url.database or not database.endswith(
                        "_test"
                    ):
                        pytest.fail(
                            "refusing outbox cleanup outside the owned test database"
                        )
                    await owner_session.execute(
                        delete(AssetStorageDeletion).where(
                            AssetStorageDeletion.operation_id.in_(
                                [operation_one, operation_two]
                            )
                        )
                    )
            finally:
                failures = 0
                try:
                    for bucket, key in owned_objects:
                        try:
                            client.delete_object(Bucket=bucket, Key=key)
                        except Exception:
                            failures += 1
                finally:
                    await cleanup_engine.dispose()
                    client.close()
                if failures:
                    pytest.fail("failed to remove one or more owned MinIO test objects")
