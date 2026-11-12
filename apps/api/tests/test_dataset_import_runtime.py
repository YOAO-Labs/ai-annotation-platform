"""Real isolated SQL/S3/container acceptance for connector import execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import ClientError
import httpx
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _container_s3_endpoint,
    _docker,
    _inspect_docker,
    _remove_worker_image,
    _required_env,
    _shared_bootstrap_minio,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_postgres,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_assets_source_runtime import _source_origin, _start_source
from tests.test_dataset_upload_native import _cleanup_world, _seed_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires disposable PostgreSQL and scoped import S3 identities",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


def _host_address() -> str:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ipconfig", "getifaddr", "en0"],
            capture_output=True,
            text=True,
            check=False,
        )
        address = result.stdout.strip()
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 9))
            address = connection.getsockname()[0]
    import ipaddress

    value = ipaddress.ip_address(address)
    assert value.is_private and not value.is_loopback and not value.is_reserved
    return address


@pytest.fixture(scope="module")
async def importer_image():
    identity = secrets.token_hex(12)
    image = f"aap-assets-import-acceptance:{identity}"
    source_image = f"aap-assets-import-source-acceptance:{identity}"
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "docker",
            "build",
            "--file",
            "apps/assets-source/Dockerfile.import",
            "--tag",
            image,
            "--label",
            "org.aap.acceptance=module-data-boundary",
            "--label",
            f"org.aap.acceptance-id={identity}",
            ".",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    try:
        source_build = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "build",
                "--file",
                "apps/assets-source/Dockerfile",
                "--tag",
                source_image,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                ".",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        assert source_build.returncode == 0, source_build.stderr[-3000:]
        try:
            yield image, source_image, identity
        finally:
            _remove_worker_image(source_image, identity)
    finally:
        _remove_worker_image(image, identity)


def _start_importer(stack, suffix, *, values=None, mounts=()):
    path = stack.directory / f"worker-{suffix}.env"
    _write_environment(path, values or stack.values)
    command = [
        "run",
        "-d",
        "--name",
        f"aap-assets-import-{stack.identity[:10]}-{suffix}",
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={stack.identity}",
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
        "--env-file",
        str(path),
    ]
    for source, target in mounts:
        command.extend(
            ["--mount", f"type=bind,source={source},target={target},readonly"]
        )
    command.append(stack.image)
    container = _docker(*command)
    stack.containers.append(container)
    return container


async def _facts(stack):
    async with stack.world.maker() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id,file_name,file_path,content_hash FROM public.dataset_items WHERE dataset_id=:id ORDER BY file_name"
                    ),
                    {"id": stack.world.dataset_id},
                )
            )
            .mappings()
            .all()
        )
        jobs = (
            (
                await db.execute(
                    text(
                        "SELECT id,status,payload,result,error_message FROM public.async_jobs WHERE user_id=:actor AND kind='dataset_import' ORDER BY created_at,id"
                    ),
                    {"actor": stack.world.actor_id},
                )
            )
            .mappings()
            .all()
        )
        tasks = await db.scalar(
            text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
            {"id": stack.world.project_id},
        )
        total_tasks = await db.scalar(
            text("SELECT total_tasks FROM public.projects WHERE id=:id"),
            {"id": stack.world.project_id},
        )
        file_count = await db.scalar(
            text("SELECT file_count FROM public.datasets WHERE id=:id"),
            {"id": stack.world.dataset_id},
        )
        deliveries = (
            await db.execute(
                text(
                    "SELECT kind,target_id FROM public.dataset_import_deliveries WHERE job_id=ANY(CAST(:ids AS uuid[])) ORDER BY kind,target_id"
                ),
                {"ids": [row["id"] for row in jobs]},
            )
        ).all()
    return SimpleNamespace(
        items=[dict(row) for row in rows],
        jobs=[dict(row) for row in jobs],
        tasks=tasks,
        total_tasks=total_tasks,
        file_count=file_count,
        deliveries=deliveries,
    )


async def _wait_job(stack, job_id, status="completed", seconds=25):
    async with asyncio.timeout(seconds):
        while True:
            facts = await _facts(stack)
            match = next((job for job in facts.jobs if job["id"] == job_id), None)
            if match and match["status"] == status:
                return facts
            if match and match["status"] in {"completed", "failed", "cancelled"}:
                pytest.fail(
                    f"unexpected import status={match['status']} error={match['error_message']}"
                )
            await asyncio.sleep(0.1)


def _objects(stack):
    result = stack.storage.client.list_objects_v2(
        Bucket=stack.storage.datasets_bucket,
        Prefix=f"dataset-sources/{stack.world.dataset_id}/",
    )
    return [entry["Key"] for entry in result.get("Contents", [])]


async def _wait_copy(stack):
    async with asyncio.timeout(20):
        while True:
            paths = await asyncio.to_thread(_objects, stack)
            if paths:
                return paths
            await asyncio.sleep(0.05)


@asynccontextmanager
async def _import_stack(
    test_engine, app_module, monkeypatch, importer_image, *, sftp=False
):
    from app.config import settings
    from app.core.crypto import encrypt_secret
    from app.core.security import create_access_token
    from app.db.models.storage_connection import StorageConnection
    from app.deps import get_db
    from app.services.managed_original_creation import bootstrap as bootstrap_originals
    from app.services.storage import storage_service
    from app.services.system_settings_service import SystemSettingsService

    role_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"))
    assert (
        role_url.username == "asset_import_client"
        and role_url.database == test_engine.url.database
    )
    _verify_postgres(role_url)
    resources = _verified_resource_manifest(test_engine, role_url)
    if _shared_bootstrap_minio(
        settings, container_id=_required_env("AAP_MODULE_DATA_MINIO_CONTAINER_ID")
    ) is None:
        pytest.fail(
            "import acceptance requires a verified shared bootstrap MinIO gateway"
        )
    source_endpoint = _container_s3_endpoint(settings)
    sftp_host = _host_address() if sftp else None
    allowlist = [f"{urlsplit(source_endpoint).hostname}/32"]
    if sftp_host:
        allowlist.append(f"{sftp_host}/32")
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        await bootstrap_originals(db)
    image, source_image, identity = importer_image
    world = await _seed_world(test_engine)
    connection_id = uuid4()
    prefix = f"connector-import-source/{uuid4()}"
    policy = json.dumps(allowlist)
    connector_config = {
        "endpoint": source_endpoint,
        "bucket": resources["buckets"]["MINIO_IMPORT_BUCKET"],
        "base_prefix": prefix,
        "use_ssl": settings.minio_use_ssl,
    }
    connector_secret = {
        "access_key": _required_env("AAP_MODULE_DATA_IMPORT_SOURCE_S3_ACCESS_KEY"),
        "secret_key": _required_env("AAP_MODULE_DATA_IMPORT_SOURCE_S3_SECRET_KEY"),
    }
    async with world.maker.begin() as db:
        previous_allowlist = (
            (
                await db.execute(
                    text(
                        "SELECT key,value_type,value_json,updated_by,updated_at,value_json IS NULL AS sql_null "
                        "FROM public.system_settings WHERE key='connector_host_allowlist'"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        db.add(
            StorageConnection(
                id=connection_id,
                name=f"import-{connection_id}",
                kind="s3",
                config=connector_config,
                secret_enc=encrypt_secret(connector_secret),
                scope="owner",
                created_by=world.actor_id,
            )
        )
        await db.execute(
            text(
                "INSERT INTO public.system_settings(key,value_type,value_json) VALUES "
                "('connector_host_allowlist','json',CAST(:policy AS jsonb)) "
                "ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"
            ),
            {"policy": policy},
        )
    SystemSettingsService.invalidate("connector_host_allowlist")
    auth_engine = create_async_engine(test_engine.url, hide_parameters=True)

    async def request_db():
        async with async_sessionmaker(auth_engine, expire_on_commit=False)() as db:
            yield db

    previous = dict(app_module.dependency_overrides)
    app_module.dependency_overrides[get_db] = request_db
    stack = None
    source_container = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="assets-import-runtime-", dir=os.environ["TMPDIR"]
        ) as directory:
            source_role = make_url(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
            assert source_role.username == "asset_source_client"
            assert source_role.database == test_engine.url.database
            _verify_postgres(source_role)
            verify_token = secrets.token_urlsafe(32)
            source_values = {
                "ASSETS_SOURCE_DATABASE_URL": _container_database_url(source_role),
                "ASSETS_SOURCE_TOKEN": secrets.token_urlsafe(32),
                "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN": verify_token,
                "ASSETS_SOURCE_S3_ENDPOINT": source_endpoint,
                "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": source_endpoint,
                "ASSETS_SOURCE_S3_ACCESS_KEY": _required_env(
                    "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY"
                ),
                "ASSETS_SOURCE_S3_SECRET_KEY": _required_env(
                    "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY"
                ),
                "ASSETS_SOURCE_DATASETS_BUCKET": resources["buckets"][
                    "MINIO_DATASETS_BUCKET"
                ],
                "ASSETS_SOURCE_IMPORT_BUCKET": resources["buckets"]["MINIO_IMPORT_BUCKET"],
                "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": resources["buckets"][
                    "MINIO_MEDIA_CACHE_BUCKET"
                ],
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": settings.connector_encryption_key,
            }
            source_environment = Path(directory) / "source.env"
            _write_environment(source_environment, source_values)
            source_container = await asyncio.to_thread(
                _start_source, source_image, identity, source_environment
            )
            await _wait_healthy(source_container)
            source_origin = _source_origin(source_container, source_image, identity)
            source_ip = _inspect_docker(source_container)["NetworkSettings"]["Networks"][
                "bridge"
            ]["IPAddress"]
            assert source_ip
            values = {
                "ASSETS_IMPORT_DATABASE_URL": _container_database_url(role_url),
                "ASSETS_IMPORT_VERIFY_URL": f"http://{source_ip}:8767",
                "ASSETS_IMPORT_VERIFY_TOKEN": verify_token,
                "ASSETS_IMPORT_S3_ENDPOINT": source_endpoint,
                "ASSETS_IMPORT_S3_ACCESS_KEY": _required_env(
                    "AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY"
                ),
                "ASSETS_IMPORT_S3_SECRET_KEY": _required_env(
                    "AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY"
                ),
                "ASSETS_IMPORT_DATASETS_BUCKET": resources["buckets"][
                    "MINIO_DATASETS_BUCKET"
                ],
                "CONNECTOR_ENCRYPTION_KEY": settings.connector_encryption_key,
                "CONNECTOR_HOST_ALLOWLIST": policy,
                "ASSETS_IMPORT_LEASE_SECONDS": "30",
                "ASSETS_IMPORT_HEARTBEAT_SECONDS": "2",
                "ASSETS_IMPORT_POLL_SECONDS": "0.1",
                "ASSETS_IMPORT_SQL_TIMEOUT_SECONDS": "60",
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module),
                base_url="http://gateway",
                timeout=30,
            ) as api:
                stack = SimpleNamespace(
                    world=world,
                    connection_id=connection_id,
                    prefix=prefix,
                    sftp_host=sftp_host,
                    source_bucket=resources["buckets"]["MINIO_IMPORT_BUCKET"],
                    resources=resources,
                    source_secret=connector_secret,
                    values=values,
                    directory=Path(directory),
                    image=image,
                    source_image=source_image,
                    source_values=source_values,
                    identity=identity,
                    verifier_origin=source_origin,
                    verifier_token=verify_token,
                    source_gateway_token=source_values["ASSETS_SOURCE_TOKEN"],
                    containers=[],
                    storage=storage_service,
                    api=api,
                    headers={
                        "Authorization": f"Bearer {create_access_token(str(world.actor_id), 'super_admin')}"
                    },
                )
                yield stack
    finally:
        app_module.dependency_overrides.clear()
        app_module.dependency_overrides.update(previous)
        if stack:
            for container in reversed(stack.containers):
                _stop_remove_container(container, identity)
        if source_container:
            _stop_remove_container(source_container, identity)
        await auth_engine.dispose()
        async with world.maker.begin() as db:
            item_ids = list(
                (
                    await db.execute(
                        text(
                            "SELECT id FROM public.dataset_items WHERE dataset_id=:id"
                        ),
                        {"id": world.dataset_id},
                    )
                ).scalars()
            )
            world.item_ids.update(item_ids)
            job_ids = list(
                (
                    await db.execute(
                        text(
                            "SELECT id FROM public.async_jobs WHERE user_id=:id AND kind='dataset_import'"
                        ),
                        {"id": world.actor_id},
                    )
                ).scalars()
            )
            for table in ("dataset_import_deliveries", "dataset_import_entries"):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} WHERE job_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": job_ids},
                )
            await db.execute(
                text(
                    "DELETE FROM public.dataset_import_operations WHERE job_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": job_ids},
            )
            await db.execute(
                text("DELETE FROM public.task_append_operations WHERE dataset_id=:id"),
                {"id": world.dataset_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.dataset_source_deletion_operations WHERE dataset_id=:id"
                ),
                {"id": world.dataset_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.notifications WHERE target_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": job_ids},
            )
            await db.execute(
                text(
                    "DELETE FROM public.async_jobs WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": job_ids},
            )
            await db.execute(
                text("DELETE FROM public.storage_connections WHERE id=:id"),
                {"id": connection_id},
            )
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text("DELETE FROM public.audit_logs WHERE actor_id=:id"),
                {"id": world.actor_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.system_settings WHERE key='connector_host_allowlist'"
                )
            )
            if previous_allowlist is not None:
                await db.execute(
                    text(
                        "INSERT INTO public.system_settings(key,value_type,value_json,updated_by,updated_at) "
                        "VALUES (:key,:value_type,CASE WHEN CAST(:sql_null AS boolean) THEN NULL "
                        "ELSE CAST(:value_json AS jsonb) END,:updated_by,:updated_at)"
                    ),
                    {
                        **previous_allowlist,
                        "value_json": json.dumps(previous_allowlist["value_json"]),
                    },
                )
        SystemSettingsService.invalidate("connector_host_allowlist")
        await _cleanup_world(world)


def _picture(color="white"):
    output = BytesIO()
    Image.new("RGB", (17, 29), color=color).save(output, format="PNG")
    return output.getvalue()


async def _accept(stack, *, operation=None, body=None):
    operation = operation or uuid4()
    response = await stack.api.post(
        f"/api/v1/datasets/{stack.world.dataset_id}/import-from-connection",
        headers={**stack.headers, "Idempotency-Key": str(operation)},
        json=body
        or {
            "connection_id": str(stack.connection_id),
            "source_path": "batch",
            "recursive": True,
            "include_globs": ["*.png"],
        },
    )
    assert response.status_code == 202, response.text
    assert response.headers["Idempotency-Key"] == str(operation)
    assert response.json() == {"job_id": str(operation)}
    return operation


class _CommittedResponseLost(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.transport = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        await response.aread()
        assert response.status_code == 202
        raise httpx.ReadError(
            "acceptance response was lost after commit", request=request
        )

    async def aclose(self):
        await self.transport.aclose()


async def test_import_acceptance_survives_worker_absence_and_replays_once(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        from app.config import settings
        from app.core.crypto import encrypt_secret
        import base64

        ciphertext = encrypt_secret(
            {**stack.source_secret, "compatibility_payload": "x" * 6000}
        )
        assert len(base64.b64encode(ciphertext)) > 8192
        async with stack.world.maker.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.storage_connections SET secret_enc=:secret WHERE id=:id"
                ),
                {"secret": ciphertext, "id": stack.connection_id},
            )
        data = _picture()
        monkeypatch.setattr(settings, "dataset_import_max_files", 7)
        monkeypatch.setattr(settings, "dataset_import_max_total_bytes", 2 * len(data))
        for name in ("a.png", "nested/b.png"):
            stack.storage.client.put_object(
                Bucket=stack.source_bucket,
                Key=f"{stack.prefix}/batch/{name}",
                Body=data,
            )
        job = uuid4()
        async with httpx.AsyncClient(
            transport=_CommittedResponseLost(app_module), base_url="http://gateway"
        ) as disconnected:
            with pytest.raises(httpx.ReadError):
                await disconnected.post(
                    f"/api/v1/datasets/{stack.world.dataset_id}/import-from-connection",
                    headers={**stack.headers, "Idempotency-Key": str(job)},
                    json={
                        "connection_id": str(stack.connection_id),
                        "source_path": "batch",
                        "recursive": True,
                        "include_globs": ["*.png"],
                    },
                )
        await _accept(stack, operation=job)
        monkeypatch.setattr(settings, "dataset_import_max_files", 1)
        monkeypatch.setattr(settings, "dataset_import_max_total_bytes", 1)
        await _accept(stack, operation=job)
        async with stack.world.maker.begin() as owner:
            audit_count = await owner.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE action='dataset.import' AND detail_json->>'operation_id'=:job"
                ),
                {"job": str(job)},
            )
            assert audit_count == 1
            await owner.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": stack.world.actor_id},
            )
        before = await _facts(stack)
        assert (
            len(before.jobs) == 1
            and before.jobs[0]["status"] == "pending"
            and not before.items
        )
        assert before.jobs[0]["payload"]["settings_snapshot"][
            "dataset_import_max_total_bytes"
        ] == 2 * len(data)
        assert (
            before.jobs[0]["payload"]["settings_snapshot"]["dataset_import_max_files"]
            == 7
        )
        worker = _start_importer(stack, "initial")
        await _wait_healthy(worker)
        facts = await _wait_job(stack, job)
        assert facts.file_count == facts.tasks == facts.total_tasks == 1, facts.jobs[0][
            "result"
        ]
        assert facts.jobs[0]["result"]["added"] == 1
        assert facts.jobs[0]["result"]["skipped"] == 1
        assert facts.jobs[0]["result"]["error_count"] == 0
        assert len(facts.items) == 1 and facts.items[0]["file_path"].startswith(
            f"dataset-sources/{stack.world.dataset_id}/"
        )
        assert sorted(kind for kind, _ in facts.deliveries) == [
            "notification",
            "thumbnail",
        ]
        raw = stack.storage.client.get_object(
            Bucket=stack.storage.datasets_bucket, Key=facts.items[0]["file_path"]
        )["Body"].read()
        assert raw == data
        assert facts.items[0]["content_hash"] == hashlib.md5(
            data, usedforsecurity=False
        ).hexdigest()
        async with stack.world.maker() as owner:
            creation = (
                await owner.execute(
                    text(
                        "SELECT status, sha256, size_bytes, requested_by "
                        "FROM public.managed_original_creations "
                        "WHERE dataset_id=:dataset AND object_key=:path"
                    ),
                    {
                        "dataset": stack.world.dataset_id,
                        "path": facts.items[0]["file_path"],
                    },
                )
            ).one()
            assert creation.status == "verified"
            assert creation.sha256 == hashlib.sha256(data).hexdigest()
            assert creation.size_bytes == len(data)
            assert creation.requested_by == stack.world.actor_id
        async with stack.world.maker() as owner:
            entries = (
                await owner.execute(
                    text(
                        "SELECT entry.entry_index, entry.status, entry.publication_id, entry.file_size, "
                        "entry.content_hash, entry.width, entry.height, receipt.operation_id "
                        "FROM public.dataset_import_entries entry "
                        "JOIN aap_assets_v1.import_original_verifications receipt ON receipt.job_id=entry.job_id "
                        "AND receipt.entry_index=entry.entry_index AND receipt.state='completed' "
                        "WHERE entry.job_id=:job ORDER BY entry.entry_index"
                    ),
                    {"job": job},
                )
            ).mappings().all()
            assert [entry["status"] for entry in entries] == ["added", "skipped"]
            for entry in entries:
                completion = {
                    "status": "copied",
                    "verification_operation_id": str(entry["operation_id"]),
                    "file_size": entry["file_size"],
                    "content_hash": entry["content_hash"],
                    "width": entry["width"],
                    "height": entry["height"],
                }
                replay = await owner.scalar(
                    text(
                        "SELECT aap_assets_v1.complete_dataset_import_entry("
                        ":job, :token, :index, :publication, CAST(:completion AS jsonb))"
                    ),
                    {
                        "job": job,
                        "token": uuid4(),
                        "index": entry["entry_index"],
                        "publication": entry["publication_id"],
                        "completion": json.dumps(completion),
                    },
                )
                assert replay["ok"] is True, replay
                assert replay["data"]["cursor"] == 2
                assert replay["data"]["added"] == replay["data"]["skipped"] == 1
            await owner.commit()
        async with stack.world.maker.begin() as owner:
            await owner.execute(
                text("UPDATE public.users SET role='super_admin' WHERE id=:id"),
                {"id": stack.world.actor_id},
            )
        from datetime import datetime, timedelta, timezone
        from app.services.async_job import delete_terminal_jobs_before

        async with stack.world.maker() as owner:
            await owner.execute(
                text(
                    "UPDATE public.async_jobs SET created_at=clock_timestamp()-interval '90 days' WHERE id=:id"
                ),
                {"id": job},
            )
            removed = await delete_terminal_jobs_before(
                owner,
                datetime.now(timezone.utc) - timedelta(days=30),
                statuses={"completed", "failed", "cancelled"},
            )
            assert job not in removed
            assert (
                await owner.scalar(
                    text("SELECT count(*) FROM public.async_jobs WHERE id=:id"),
                    {"id": job},
                )
                == 1
            )
            await owner.rollback()
        await _accept(stack, operation=job)
        after = await _facts(stack)
        assert after.tasks == 1 and len(after.jobs) == 1
        observed = _inspect_docker(worker)
        assert observed["Config"]["User"] == "10004:10004"
        assert not observed["Mounts"]
        assert not any(
            value.startswith(("DATABASE_URL=", "MINIO_", "CELERY_", "REDIS_"))
            for value in observed["Config"]["Env"]
        )
        imported = _docker(
            "exec",
            worker,
            "python",
            "-c",
            "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('app','sqlalchemy','celery','redis'))",
        )
        assert not imported


@pytest.mark.parametrize(
    ("rejection", "expected_error"),
    [("wrong-token", "storage_unavailable"), ("invalid-status", "invalid_response")],
)
async def test_definitive_source_rejection_recovers_unstarted_written_creation(
    test_engine, app_module, monkeypatch, importer_image, rejection, expected_error
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("blue")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        observed = []

        class InvalidReceiptHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = b'{"state":"completed","result":{"sha256":"invalid"}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = None
        server_thread = None
        try:
            values = dict(stack.values)
            if rejection == "wrong-token":
                values["ASSETS_IMPORT_VERIFY_TOKEN"] = "wrong-verification-token-" + "x" * 40
            else:
                server = ThreadingHTTPServer(("0.0.0.0", 0), InvalidReceiptHandler)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                values["ASSETS_IMPORT_VERIFY_URL"] = f"http://host.docker.internal:{server.server_port}"
            job = await _accept(stack)
            worker = _start_importer(stack, f"rejected-{rejection}", values=values)
            await _wait_healthy(worker)
            facts = await _wait_job(stack, job, status="failed")
            assert facts.jobs[0]["error_message"] == expected_error
            assert facts.jobs[0]["result"]["added"] == 0
            assert not facts.items
            async with asyncio.timeout(15):
                while True:
                    async with stack.world.maker() as observer:
                        creation = (
                            await observer.execute(
                                text(
                                    "SELECT creation.id,creation.status,creation.operation_id,creation.object_key,"
                                    "creation.version_id,creation.receipt,creation.error_code,"
                                    "EXISTS(SELECT 1 FROM public.storage_operations permit "
                                    "WHERE permit.id=creation.operation_id) AS permit_retained "
                                    "FROM public.managed_original_creations creation "
                                    "WHERE creation.authority->>'job_id'=:job"
                                ),
                                {"job": str(job)},
                            )
                        ).mappings().one()
                        if creation["status"] == "failed":
                            assert creation["error_code"] == "import_verification_not_started"
                            assert not creation["permit_retained"]
                            mutation = (
                                await observer.execute(
                                    text(
                                        "SELECT physical_outcome,finished_at,evidence FROM public.storage_usage_mutations "
                                        "WHERE owner_id=:creation AND producer='managed_original_creation' "
                                        "AND kind='create'"
                                    ),
                                    {"creation": creation["id"]},
                                )
                            ).mappings().one()
                            assert mutation["physical_outcome"] == "written"
                            assert mutation["finished_at"] is not None
                            assert mutation["evidence"]["receipt"] == creation["receipt"]
                            assert mutation["evidence"]["physical_settled"] is True
                            assert await observer.scalar(
                                text("SELECT count(*) FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"),
                                {"job": job},
                            ) == 0
                            break
                    await asyncio.sleep(0.1)
            get = {"Bucket": stack.storage.datasets_bucket, "Key": creation["object_key"]}
            if creation["version_id"] is not None:
                get["VersionId"] = creation["version_id"]
            assert stack.storage.client.get_object(**get)["Body"].read() == data
            if rejection == "invalid-status":
                assert [path for path, _ in observed] == [
                    "/v1/import/verify", "/v1/import/verify-status"
                ]
                assert observed[0][1] == observed[1][1]
                assert set(observed[0][1]) == {
                    "job_id", "lease_token", "entry_index", "item_id", "creation_id", "operation_id", "mode"
                }
        finally:
            if server is not None:
                await asyncio.to_thread(server.shutdown)
                server_thread.join()
                server.server_close()


async def test_import_accepted_before_account_deactivation_finishes(
    test_engine, app_module, monkeypatch, importer_image
):
    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        data = _picture("blue")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        job = await _accept(stack)
        async with stack.world.maker.begin() as owner:
            await owner.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": stack.world.actor_id},
            )
        worker = _start_importer(stack, "inactive-actor")
        await _wait_healthy(worker)
        facts = await _wait_job(stack, job)
        assert facts.file_count == facts.tasks == facts.total_tasks == 1
        assert facts.jobs[0]["result"]["added"] == 1
        assert stack.storage.client.get_object(
            Bucket=stack.storage.datasets_bucket, Key=facts.items[0]["file_path"]
        )["Body"].read() == data


async def test_import_rejects_unpublishable_entry_and_continues(
    test_engine, app_module, monkeypatch, importer_image
):
    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        data = _picture("red")
        oversized = "/".join(("a" * 200, "a" * 200, "a" * 101 + ".png"))
        for name in (oversized, "z-valid.png"):
            stack.storage.client.put_object(
                Bucket=stack.source_bucket,
                Key=f"{stack.prefix}/batch/{name}",
                Body=data,
            )
        job = await _accept(stack)
        worker = _start_importer(stack, "invalid-entry")
        await _wait_healthy(worker)
        facts = await _wait_job(stack, job)
        result = facts.jobs[0]["result"]
        assert result["error_count"] == 1 and result["added"] == 1
        assert result["skipped"] == 0
        assert len(result["errors"]) == 1
        async with stack.world.maker() as owner:
            entries = (
                await owner.execute(
                    text(
                        "SELECT status, error_code FROM public.dataset_import_entries "
                        "WHERE job_id=:job ORDER BY entry_index"
                    ),
                    {"job": job},
                )
            ).all()
            assert [tuple(entry) for entry in entries] == [
                ("error", "invalid_source_object"),
                ("added", None),
            ]
        assert facts.file_count == facts.tasks == facts.total_tasks == 1
        assert facts.items[0]["file_name"] == "z-valid.png"
        assert stack.storage.client.get_object(
            Bucket=stack.storage.datasets_bucket, Key=facts.items[0]["file_path"]
        )["Body"].read() == data


@asynccontextmanager
async def _publication_gate(stack, test_engine, *, after_settlement=False):
    signature = "uuid,uuid,bigint,uuid,jsonb"
    alias = "_acceptance_import_completion"
    gate = secrets.randbits(60)
    settlement = (
        f"v_reply := aap_assets_v1.{alias}(p_job,p_token,p_index,p_publication,p_completion);"
    )
    gate_lock = f"PERFORM pg_catalog.pg_advisory_xact_lock({gate});"
    steps = (
        f"{settlement} {gate_lock}" if after_settlement else f"{gate_lock} {settlement}"
    )
    async with test_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:gate)"), {"gate": gate})
        await holder.commit()
        async with stack.world.maker.begin() as owner:
            await owner.execute(
                text(
                    f"ALTER FUNCTION aap_assets_v1.complete_dataset_import_entry({signature}) RENAME TO {alias}"
                )
            )
            await owner.execute(
                text(
                    f"REVOKE ALL ON FUNCTION aap_assets_v1.{alias}({signature}) FROM asset_import_client"
                )
            )
            await owner.execute(
                text(f"""
                CREATE FUNCTION aap_assets_v1.complete_dataset_import_entry(
                    p_job uuid,p_token uuid,p_index bigint,p_publication uuid,p_completion jsonb
                ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
                SET search_path=pg_catalog,pg_temp AS $body$
                DECLARE v_reply jsonb;
                BEGIN
                    {steps}
                    RETURN v_reply;
                END;
                $body$;
            """)
            )
            await owner.execute(
                text(
                    f"REVOKE ALL ON FUNCTION aap_assets_v1.complete_dataset_import_entry({signature}) FROM PUBLIC"
                )
            )
            await owner.execute(
                text(
                    f"ALTER FUNCTION aap_assets_v1.complete_dataset_import_entry({signature}) OWNER TO assets_code"
                )
            )
            await owner.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION aap_assets_v1.complete_dataset_import_entry({signature}) TO asset_import_client"
                )
            )
        released = False

        async def release():
            nonlocal released
            if not released:
                await holder.execute(
                    text("SELECT pg_advisory_unlock(:gate)"), {"gate": gate}
                )
                await holder.commit()
                released = True

        async def wait_for_gate():
            async with asyncio.timeout(20):
                async with test_engine.connect() as observer:
                    while not await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks "
                            "WHERE locktype='advisory' AND NOT granted "
                            "AND classid=CAST(:high AS oid) AND objid=CAST(:low AS oid) "
                            "AND objsubid=1)"
                        ),
                        {"high": gate >> 32, "low": gate & 0xFFFFFFFF},
                    ):
                        await asyncio.sleep(0.05)

        try:
            yield release, wait_for_gate
        finally:
            await release()
            for container in stack.containers:
                if _inspect_docker(container)["State"].get("Running"):
                    _docker("stop", "--time", "3", container)
            async with stack.world.maker.begin() as owner:
                await owner.execute(
                    text(
                        f"DROP FUNCTION aap_assets_v1.complete_dataset_import_entry({signature})"
                    )
                )
                await owner.execute(
                    text(
                        f"ALTER FUNCTION aap_assets_v1.{alias}({signature}) RENAME TO complete_dataset_import_entry"
                    )
                )
                await owner.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION aap_assets_v1.complete_dataset_import_entry({signature}) TO asset_import_client"
                    )
                )


@asynccontextmanager
async def _verification_finish_gate(
    stack, test_engine, *, function="finish_import_original_verification", gated_lease=None
):
    assert function in {"finish_import_original_verification", "record_import_original_read"}
    alias = f"_acceptance_{function}"
    gate = secrets.randbits(60)
    gate_statement = f"PERFORM pg_catalog.pg_advisory_xact_lock({gate});"
    if gated_lease is not None:
        gate_statement = (
            f"IF p->>'lease_token'='{gated_lease}' THEN {gate_statement} END IF;"
        )
    role = make_url(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL")).username
    assert role == "asset_source_client"
    async with test_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:gate)"), {"gate": gate})
        await holder.commit()
        async with stack.world.maker.begin() as owner:
            await owner.execute(text(
                f"ALTER FUNCTION aap_assets_v1.{function}(jsonb,jsonb) RENAME TO {alias}"
            ))
            await owner.execute(text(
                f"REVOKE ALL ON FUNCTION aap_assets_v1.{alias}(jsonb,jsonb) "
                "FROM asset_source_client"
            ))
            await owner.execute(text(f"""
                CREATE FUNCTION aap_assets_v1.{function}(p jsonb, r jsonb)
                RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
                SET search_path=pg_catalog,pg_temp AS $body$
                BEGIN
                    {gate_statement}
                    RETURN aap_assets_v1.{alias}(p,r);
                END;
                $body$;
            """))
            await owner.execute(text(
                f"REVOKE ALL ON FUNCTION aap_assets_v1.{function}(jsonb,jsonb) FROM PUBLIC"
            ))
            await owner.execute(text(
                f"ALTER FUNCTION aap_assets_v1.{function}(jsonb,jsonb) OWNER TO assets_code"
            ))
            await owner.execute(text(
                f"GRANT EXECUTE ON FUNCTION aap_assets_v1.{function}(jsonb,jsonb) "
                "TO asset_source_client"
            ))
        released = False

        async def release():
            nonlocal released
            if not released:
                await holder.execute(text("SELECT pg_advisory_unlock(:gate)"), {"gate": gate})
                await holder.commit()
                released = True

        async def wait_for_gate():
            async with asyncio.timeout(20):
                async with test_engine.connect() as observer:
                    while not await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks "
                            "WHERE locktype='advisory' AND NOT granted "
                            "AND classid=CAST(:high AS oid) AND objid=CAST(:low AS oid) AND objsubid=1)"
                        ),
                        {"high": gate >> 32, "low": gate & 0xFFFFFFFF},
                    ):
                        await asyncio.sleep(0.05)
        try:
            yield release, wait_for_gate
        finally:
            await release()
            for container in stack.containers:
                if _inspect_docker(container)["State"].get("Running"):
                    _docker("stop", "--time", "3", container)
            async with stack.world.maker.begin() as owner:
                await owner.execute(text(
                    f"DROP FUNCTION aap_assets_v1.{function}(jsonb,jsonb)"
                ))
                await owner.execute(text(
                    f"ALTER FUNCTION aap_assets_v1.{alias}(jsonb,jsonb) RENAME TO {function}"
                ))
                await owner.execute(text(
                    f"GRANT EXECUTE ON FUNCTION aap_assets_v1.{function}(jsonb,jsonb) "
                    "TO asset_source_client"
                ))


@pytest.mark.parametrize("locked_row", ["dataset", "job", "maintenance", "budget", "inner-timeout"])
async def test_original_source_request_retries_admission_lock_contention(
    test_engine, app_module, monkeypatch, importer_image, locked_row
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("orange")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        accepted = threading.Event()
        forward = threading.Event()
        finished = threading.Event()
        original = []
        origin = stack.verifier_origin
        short_source = None

        class ForwardingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/v1/import/verify":
                    accepted.set()
                    if not forward.wait(15):
                        return
                response = httpx.post(
                    origin + self.path,
                    content=body,
                    headers={
                        "Authorization": self.headers["Authorization"],
                        "Content-Type": "application/json",
                    },
                    timeout=20,
                    trust_env=False,
                )
                if self.path == "/v1/import/verify":
                    original.append((response.status_code, response.json() if response.status_code == 200 else None))
                    finished.set()
                try:
                    self.send_response(response.status_code)
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("0.0.0.0", 0), ForwardingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        gate_id = None
        try:
            if locked_row in {"budget", "inner-timeout"}:
                path = stack.directory / "source-admission-budget.env"
                override = (
                    {"ASSETS_SOURCE_IMPORT_VERIFY_TIMEOUT_SECONDS": "0.5"}
                    if locked_row == "budget" else {"ASSETS_SOURCE_SQL_TIMEOUT_SECONDS": "0.3"}
                )
                _write_environment(path, {**stack.source_values, **override})
                short_source = await asyncio.to_thread(
                    _start_source, stack.source_image, stack.identity, path, "-short-budget"
                )
                await _wait_healthy(short_source)
                origin = _source_origin(short_source, stack.source_image, stack.identity)
            values = {
                **stack.values,
                "ASSETS_IMPORT_VERIFY_URL": f"http://host.docker.internal:{server.server_port}",
            }
            job = await _accept(stack)
            worker = _start_importer(stack, f"admission-{locked_row}", values=values)
            assert await asyncio.to_thread(accepted.wait, 15)
            if locked_row == "maintenance":
                async with stack.world.maker.begin() as owner:
                    gate_id = await owner.scalar(
                        text(
                            "SELECT location_id FROM public.managed_original_creations "
                            "WHERE authority->>'job_id'=:job"
                        ), {"job": str(job)},
                    )
                    gate = await owner.scalar(
                        text("SELECT state FROM public.storage_maintenance_gates WHERE location_id=:id"),
                        {"id": gate_id},
                    )
                    assert gate == "active"
                    await owner.execute(
                        text("UPDATE public.storage_maintenance_gates SET state='draining' WHERE location_id=:id"),
                        {"id": gate_id},
                    )
            async with test_engine.connect() as locker:
                transaction = await locker.begin()
                try:
                    if locked_row == "dataset":
                        await locker.execute(
                            text("SELECT id FROM public.datasets WHERE id=:id FOR NO KEY UPDATE"),
                            {"id": stack.world.dataset_id},
                        )
                    elif locked_row == "job":
                        await locker.execute(
                            text("SELECT id FROM public.async_jobs WHERE id=:id FOR UPDATE"),
                            {"id": job},
                        )
                    elif locked_row in {"budget", "inner-timeout"}:
                        await locker.execute(
                            text(
                                "SELECT gate.location_id FROM public.storage_maintenance_gates gate "
                                "JOIN public.managed_original_creations creation ON creation.location_id=gate.location_id "
                                "WHERE creation.authority->>'job_id'=:job FOR UPDATE OF gate"
                            ), {"job": str(job)},
                        )
                    forward.set()
                    if locked_row == "budget":
                        assert await asyncio.to_thread(finished.wait, 3)
                    else:
                        await asyncio.sleep(1.2 if locked_row == "inner-timeout" else 0.5)
                        assert not finished.is_set()
                    async with stack.world.maker() as observer:
                        assert await observer.scalar(
                            text("SELECT count(*) FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"),
                            {"job": job},
                        ) == 0
                finally:
                    await transaction.rollback()
            if gate_id is not None:
                async with stack.world.maker.begin() as owner:
                    await owner.execute(
                        text("UPDATE public.storage_maintenance_gates SET state='active' WHERE location_id=:id"),
                        {"id": gate_id},
                    )
                gate_id = None
            if locked_row == "budget":
                _docker("kill", "--signal", "KILL", worker)
                async with stack.world.maker() as observer:
                    assert await observer.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM public.managed_original_creations creation "
                            "JOIN public.storage_operations permit ON permit.id=creation.operation_id "
                            "WHERE creation.authority->>'job_id'=:job AND creation.status='written')"
                        ), {"job": str(job)},
                    )
                return
            facts = await _wait_job(stack, job)
            assert facts.jobs[0]["result"]["added"] == 1
            assert len(facts.items) == 1
            assert await asyncio.to_thread(finished.wait, 5)
            assert len(original) == 1 and original[0][0] == 200
            assert original[0][1]["state"] in {"completed", "pending"}
            async with stack.world.maker() as observer:
                receipts = (
                    await observer.execute(
                        text("SELECT state,result FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"),
                        {"job": job},
                    )
                ).mappings().all()
            assert len(receipts) == 1 and receipts[0]["state"] == "completed"
            assert receipts[0]["result"]["sha256"] == hashlib.sha256(data).hexdigest()
        finally:
            if gate_id is not None:
                async with stack.world.maker.begin() as owner:
                    await owner.execute(
                        text("UPDATE public.storage_maintenance_gates SET state='active' WHERE location_id=:id"),
                        {"id": gate_id},
                    )
            forward.set()
            await asyncio.to_thread(server.shutdown)
            thread.join()
            server.server_close()
            if short_source is not None:
                _stop_remove_container(short_source, stack.identity)


@pytest.mark.parametrize("replacement", [False, True, "committed", "inflight", "cancelled", "connection-lost", "timeout-fenced"])
async def test_stale_unstarted_write_is_settled_and_late_source_begin_is_fenced(
    test_engine, app_module, monkeypatch, importer_image, replacement
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("purple")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        accepted = threading.Event()
        forward = threading.Event()
        finished = threading.Event()
        late = []
        captured = []

        class LateRequestHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path != "/v1/import/verify":
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                captured.append(json.loads(body))
                accepted.set()
                if not forward.wait(20):
                    return
                response = httpx.post(
                    stack.verifier_origin + self.path, content=body,
                    headers={
                        "Authorization": self.headers["Authorization"],
                        "Content-Type": "application/json",
                    },
                    timeout=10, trust_env=False,
                )
                late.append((response.status_code, response.json()))
                finished.set()
                try:
                    self.send_response(response.status_code)
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("0.0.0.0", 0), LateRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            values = {
                **stack.values,
                "ASSETS_IMPORT_VERIFY_URL": f"http://host.docker.internal:{server.server_port}",
            }
            job = await _accept(stack)
            worker = _start_importer(stack, "stale-no-source-begin", values=values)
            assert await asyncio.to_thread(accepted.wait, 15)
            if replacement is True:
                # A live current publication is not an orphan.
                await asyncio.sleep(1.2)
                async with stack.world.maker() as observer:
                    assert await observer.scalar(
                        text("SELECT status FROM public.managed_original_creations WHERE authority->>'job_id'=:job"),
                        {"job": str(job)},
                    ) == "written"
            _docker("kill", "--signal", "KILL", worker)
            if replacement == "committed":
                source_login = create_async_engine(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
                try:
                    original = captured[0]
                    nonce = str(uuid4())
                    admission = {**original, "admission_id": nonce}
                    async with source_login.begin() as client:
                        begun = await client.scalar(
                            text("SELECT aap_assets_v1.begin_import_original_verification(CAST(:p AS jsonb))"),
                            {"p": json.dumps(admission)},
                        )
                    assert begun["state"] == "started"
                    async with source_login.begin() as client:
                        replayed = await client.scalar(
                            text("SELECT aap_assets_v1.begin_import_original_verification(CAST(:p AS jsonb))"),
                            {"p": json.dumps(admission)},
                        )
                    assert replayed["state"] == "started"
                    async with source_login.begin() as client:
                        other = await client.scalar(
                            text("SELECT aap_assets_v1.begin_import_original_verification(CAST(:p AS jsonb))"),
                            {"p": json.dumps({**admission, "admission_id": str(uuid4())})},
                        )
                    assert other["state"] == "pending"
                    async with source_login.begin() as client:
                        rejected = await client.scalar(
                            text("SELECT aap_assets_v1.record_import_original_read(CAST(:p AS jsonb),NULL)"),
                            {"p": json.dumps({**admission, "admission_id": str(uuid4())})},
                        )
                    assert rejected["state"] == "fenced"
                    async with source_login.begin() as client:
                        drained = await client.scalar(
                            text("SELECT aap_assets_v1.record_import_original_read(CAST(:p AS jsonb),NULL)"),
                            {"p": json.dumps(admission)},
                        )
                    assert drained["state"] == "drained"
                    async with source_login.begin() as client:
                        settled = await client.scalar(
                            text("SELECT aap_assets_v1.finish_import_original_verification(CAST(:p AS jsonb),NULL)"),
                            {"p": json.dumps(original)},
                        )
                    assert settled["state"] == "failed"
                finally:
                    await source_login.dispose()
            if replacement == "inflight":
                source_login = create_async_engine(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
                original = captured[0]
                admission = {**original, "admission_id": str(uuid4())}
                try:
                    async with source_login.connect() as first:
                        transaction = await first.begin()
                        try:
                            begun = await first.scalar(
                                text("SELECT aap_assets_v1.begin_import_original_verification(CAST(:p AS jsonb))"),
                                {"p": json.dumps(admission)},
                            )
                            assert begun["state"] == "started"

                            async def settle_inflight():
                                async with source_login.begin() as second:
                                    return await second.scalar(
                                        text("SELECT aap_assets_v1.record_import_original_read(CAST(:p AS jsonb),NULL)"),
                                        {"p": json.dumps(admission)},
                                    )

                            pending_settlement = asyncio.create_task(settle_inflight())
                            await asyncio.sleep(0.25)
                            assert not pending_settlement.done()
                            await transaction.commit()
                            assert (await pending_settlement)["state"] == "drained"
                        finally:
                            if transaction.is_active:
                                await transaction.rollback()
                    async with source_login.begin() as client:
                        settled = await client.scalar(
                            text("SELECT aap_assets_v1.finish_import_original_verification(CAST(:p AS jsonb),NULL)"),
                            {"p": json.dumps(original)},
                        )
                    assert settled["state"] == "failed"
                finally:
                    await source_login.dispose()
            if replacement in {"cancelled", "connection-lost", "timeout-fenced"}:
                import asyncpg
                from yoao_assets_source.import_verification_contract import ImportVerificationRequest
                from yoao_assets_source_runtime.database import SourceDatabase, _init_connection
                from yoao_assets_source_runtime.runtime import SourceRuntime

                role = make_url(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL")).set(drivername="postgresql")
                pool = await asyncpg.create_pool(
                    dsn=role.render_as_string(hide_password=False), min_size=1, max_size=2,
                    init=_init_connection,
                )
                config = SimpleNamespace(
                    import_verify_timeout_seconds=15, pool_timeout_seconds=2, max_response_bytes=16_777_216
                )
                native = SourceDatabase(pool, config)
                committed = asyncio.Event()
                cleanup_started = asyncio.Event()
                cleanup_release = asyncio.Event()
                cleanup_attempts = []

                class LostAcknowledgement:
                    async def import_verification(self, operation, request, result=None):
                        if operation == "record" and replacement == "cancelled":
                            cleanup_started.set()
                            await cleanup_release.wait()
                        if operation == "record" and replacement == "connection-lost":
                            cleanup_attempts.append(operation)
                            if len(cleanup_attempts) == 1:
                                raise asyncpg.ConnectionDoesNotExistError("temporary cleanup disconnect")
                        response = await native.import_verification(operation, request, result)
                        if operation == "begin" and not committed.is_set():
                            assert response["state"] == "started"
                            committed.set()
                            if replacement == "cancelled":
                                await asyncio.Event().wait()
                            if replacement == "timeout-fenced":
                                async with stack.world.maker.begin() as owner:
                                    await owner.execute(
                                        text(
                                            "UPDATE public.dataset_import_operations SET "
                                            "lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
                                        ), {"job": job},
                                    )
                                raise TimeoutError("lost admission acknowledgement")
                            raise asyncpg.ConnectionDoesNotExistError("lost commit acknowledgement")
                        return response

                runtime = SourceRuntime(config, LostAcknowledgement(), None)
                try:
                    pending = asyncio.create_task(runtime.verify_import_original(
                        ImportVerificationRequest.model_validate(captured[0])
                    ))
                    if replacement == "cancelled":
                        async with asyncio.timeout(5):
                            await committed.wait()
                        pending.cancel()
                        async with asyncio.timeout(5):
                            await cleanup_started.wait()
                        pending.cancel()
                        cleanup_release.set()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                    elif replacement == "timeout-fenced":
                        assert (await pending)["state"] == "fenced"
                    else:
                        with pytest.raises(asyncpg.ConnectionDoesNotExistError):
                            await pending
                        assert len(cleanup_attempts) == 2
                finally:
                    cleanup_release.set()
                    await pool.close()
            token, publication = uuid4(), uuid4()
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(
                        "UPDATE public.dataset_import_operations SET "
                        "lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
                    ),
                    {"job": job},
                )
                if replacement is True:
                    # Claim and replace the publication before recovery can see
                    # the expired lease, using the real restricted owner API.
                    await owner.execute(text("SET LOCAL ROLE asset_import_client"))
                    claimed = await owner.scalar(
                        text("SELECT aap_assets_v1.claim_dataset_import(:token,30,:job)"),
                        {"token": token, "job": job},
                    )
                    assert claimed["ok"] is True
                    entry = await owner.scalar(
                        text("SELECT aap_assets_v1.next_dataset_import_entry(:job,:token,:publication)"),
                        {"job": job, "token": token, "publication": publication},
                    )
                    assert entry["ok"] is True and entry["data"]["publication_id"] == str(publication)
            async with asyncio.timeout(15):
                while True:
                    async with stack.world.maker() as observer:
                        creation = (
                            await observer.execute(
                                text(
                                    "SELECT id,status,object_key,version_id,operation_id FROM "
                                    "public.managed_original_creations WHERE authority->>'job_id'=:job"
                                ), {"job": str(job)},
                            )
                        ).mappings().one()
                        if creation["status"] == "failed":
                            assert not await observer.scalar(
                                text("SELECT EXISTS(SELECT 1 FROM public.storage_operations WHERE id=:id)"),
                                {"id": creation["operation_id"]},
                            )
                            break
                    await asyncio.sleep(0.1)
            forward.set()
            assert await asyncio.to_thread(finished.wait, 10)
            assert late == [(200, {"state": "fenced", "result": None})]
            async with stack.world.maker() as observer:
                assert await observer.scalar(
                    text("SELECT count(*) FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"),
                    {"job": job},
                ) == (1 if replacement in {"committed", "inflight", "cancelled", "connection-lost", "timeout-fenced"} else 0)
                if replacement in {"committed", "inflight", "cancelled", "connection-lost", "timeout-fenced"}:
                    row = (
                        await observer.execute(
                            text(
                                "SELECT state,read_operation_id FROM aap_assets_v1.import_original_verifications "
                                "WHERE job_id=:job"
                            ), {"job": job},
                        )
                    ).mappings().one()
                    assert row["state"] == ("fenced" if replacement == "timeout-fenced" else "failed")
                    assert not await observer.scalar(
                        text("SELECT EXISTS(SELECT 1 FROM public.storage_operations WHERE id=:id)"),
                        {"id": row["read_operation_id"]},
                    )
                if replacement is True:
                    current = (
                        await observer.execute(
                            text(
                                "SELECT operation.stage,operation.lease_token,"
                                "operation.lease_expires_at>clock_timestamp() AS live,entry.publication_id "
                                "FROM public.dataset_import_operations operation "
                                "JOIN public.dataset_import_entries entry ON entry.job_id=operation.job_id "
                                "AND entry.entry_index=operation.cursor WHERE operation.job_id=:job"
                            ), {"job": job},
                        )
                    ).mappings().one()
                    assert current["stage"] == "importing" and current["live"]
                    assert current["lease_token"] == token and current["publication_id"] == publication
            get = {"Bucket": stack.storage.datasets_bucket, "Key": creation["object_key"]}
            if creation["version_id"] is not None:
                get["VersionId"] = creation["version_id"]
            assert stack.storage.client.get_object(**get)["Body"].read() == data
            assert not (await _facts(stack)).items
        finally:
            forward.set()
            await asyncio.to_thread(server.shutdown)
            thread.join()
            server.server_close()


async def test_stale_started_write_keeps_permits_until_source_records_drain(
    test_engine, app_module, monkeypatch, importer_image
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("orange")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        async with _verification_finish_gate(
            stack, test_engine, function="record_import_original_read"
        ) as (release, wait_for_gate):
            worker = _start_importer(stack, "stale-started-source")
            await _wait_healthy(worker)
            job = await _accept(stack)
            await wait_for_gate()
            async with stack.world.maker() as observer:
                receipt = (
                    await observer.execute(
                        text(
                            "SELECT lease_token,entry_index,item_id,creation_id,operation_id,"
                            "read_operation_id,admission_id,state,mode FROM "
                            "aap_assets_v1.import_original_verifications WHERE job_id=:job"
                        ), {"job": job},
                    )
                ).mappings().one()
            assert receipt["state"] == "pending"
            foreign_admission = {
                "job_id": str(job), "lease_token": str(receipt["lease_token"]),
                "entry_index": receipt["entry_index"], "item_id": str(receipt["item_id"]),
                "creation_id": str(receipt["creation_id"]), "operation_id": str(receipt["operation_id"]),
                "mode": receipt["mode"], "admission_id": str(uuid4()),
            }
            async with stack.world.maker.begin() as owner:
                rejected = await owner.scalar(
                    text(
                        "SELECT aap_assets_v1._acceptance_record_import_original_read("
                        "CAST(:request AS jsonb),NULL)"
                    ), {"request": json.dumps(foreign_admission)},
                )
            assert rejected["state"] == "fenced"
            _docker("kill", "--signal", "KILL", worker)
            async with stack.world.maker.begin() as owner:
                await owner.execute(
                    text(
                        "UPDATE public.dataset_import_operations SET "
                        "lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
                    ), {"job": job},
                )
            # Exercise the Source recovery scan at this creation, rather than
            # spending the record call's finite SQL timeout sleeping.
            cursor = {
                "kind": "creation",
                "creation_id": str(UUID(int=receipt["creation_id"].int - 1)),
            }
            async with stack.world.maker.begin() as owner:
                recovered = await owner.scalar(
                    text(
                        "SELECT aap_assets_v1.recover_import_original_verification("
                        "CAST(:cursor AS jsonb))"
                    ),
                    {"cursor": json.dumps(cursor)},
                )
            assert recovered["cursor"] is None or recovered["cursor"]["creation_id"] != str(
                receipt["creation_id"]
            )
            async with stack.world.maker() as observer:
                assert await observer.scalar(
                    text("SELECT status FROM public.managed_original_creations WHERE id=:id"),
                    {"id": receipt["creation_id"]},
                ) == "written"
                assert await observer.scalar(
                    text("SELECT count(*) FROM public.storage_operations WHERE id IN (:write_id,:read_id)"),
                    {"write_id": receipt["operation_id"], "read_id": receipt["read_operation_id"]},
                ) == 2
            # The gate must still hold the original Source record call, not a
            # request that already timed out and left ownership unsettled.
            async with asyncio.timeout(1):
                await wait_for_gate()
            await release()
            async with asyncio.timeout(15):
                while True:
                    async with stack.world.maker() as observer:
                        state = await observer.scalar(
                            text("SELECT state FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"),
                            {"job": job},
                        )
                        if state == "fenced":
                            assert await observer.scalar(
                                text("SELECT status FROM public.managed_original_creations WHERE id=:id"),
                                {"id": receipt["creation_id"]},
                            ) == "verified"
                            assert await observer.scalar(
                                text("SELECT count(*) FROM public.storage_operations WHERE id IN (:write_id,:read_id)"),
                                {"write_id": receipt["operation_id"], "read_id": receipt["read_operation_id"]},
                            ) == 0
                            break
                    await asyncio.sleep(0.1)
            assert not (await _facts(stack)).items


async def test_source_recovers_drained_original_without_old_publication_authority(
    test_engine, app_module, monkeypatch, importer_image
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("cyan")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        async with _verification_finish_gate(stack, test_engine) as (release, wait_for_gate):
            job = await _accept(stack)
            worker = _start_importer(stack, "orphan-lease")
            await _wait_healthy(worker)
            await wait_for_gate()
            async with stack.world.maker() as observer:
                before = (
                    await observer.execute(
                        text(
                            "SELECT operation_id,read_operation_id,creation_id,evidence,state "
                            "FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"
                        ),
                        {"job": job},
                    )
                ).mappings().one()
            assert before["state"] == "drained"
            assert before["evidence"]["result"]["sha256"] == hashlib.sha256(data).hexdigest()
            async with test_engine.begin() as locker:
                await locker.execute(
                    text("SELECT id FROM public.datasets WHERE id=:id FOR NO KEY UPDATE"),
                    {"id": stack.world.dataset_id},
                )
                _docker("kill", "--signal", "KILL", worker)
                await locker.execute(
                    text(
                        "UPDATE public.dataset_import_operations "
                        "SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
                    ),
                    {"job": job},
                )
                await release()
            async with asyncio.timeout(15):
                while True:
                    async with stack.world.maker() as observer:
                        row = (
                            await observer.execute(
                                text(
                                    "SELECT state,evidence,result FROM aap_assets_v1.import_original_verifications "
                                    "WHERE job_id=:job"
                                ),
                                {"job": job},
                            )
                        ).mappings().one()
                        if row["state"] == "fenced":
                            assert row["result"] is None
                            assert row["evidence"] == before["evidence"]
                            creation = (
                                await observer.execute(
                                    text("SELECT status,object_key FROM public.managed_original_creations WHERE id=:id"),
                                    {"id": before["creation_id"]},
                                )
                            ).mappings().one()
                            assert creation["status"] == "verified"
                            assert not await observer.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM public.storage_operations "
                                    "WHERE id IN (:read_id,:write_id))"
                                ),
                                {"read_id": before["read_operation_id"], "write_id": before["operation_id"]},
                            )
                            break
                    await asyncio.sleep(0.1)
            stored = stack.storage.client.get_object(
                Bucket=stack.storage.datasets_bucket,
                Key=creation["object_key"],
                VersionId=before["evidence"]["result"]["version_id"],
            )["Body"].read()
            assert stored == data
            assert not (await _facts(stack)).items


@pytest.mark.parametrize("first_outcome", ["verified", "failed"])
async def test_same_creation_drained_readbacks_settle_after_lease_reclaim(
    test_engine, app_module, monkeypatch, importer_image, first_outcome
):
    from yoao_assets_source.import_contract import ImportEntryCompletion, ImportFailure
    from yoao_assets_source.import_verification_contract import ImportVerificationResult
    from yoao_assets_source_import.config import load_config
    from yoao_assets_source_import.database import ImportDatabase
    from yoao_assets_source_import.storage import _object_identity_from_verified

    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("green")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        denied_source = None
        restricted = create_async_engine(_required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"))
        try:
            old_origin = stack.verifier_origin
            if first_outcome == "failed":
                denied_environment = stack.directory / "source-read-denied.env"
                _write_environment(denied_environment, {
                    **stack.source_values,
                    "ASSETS_SOURCE_S3_ACCESS_KEY": _required_env("AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY"),
                    "ASSETS_SOURCE_S3_SECRET_KEY": _required_env("AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY"),
                })
                denied_source = await asyncio.to_thread(
                    _start_source, stack.source_image, stack.identity, denied_environment, "-read-denied"
                )
                await _wait_healthy(denied_source)
                old_origin = _source_origin(denied_source, stack.source_image, stack.identity)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable:
                unavailable.bind(("0.0.0.0", 0))
                values = {
                    **stack.values,
                    "ASSETS_IMPORT_VERIFY_URL": f"http://host.docker.internal:{unavailable.getsockname()[1]}",
                }
                job = await _accept(stack)
                worker = _start_importer(stack, f"two-reads-{first_outcome}", values=values)
                await _wait_healthy(worker)
                async with asyncio.timeout(20):
                    while True:
                        async with stack.world.maker() as observer:
                            written = (
                                await observer.execute(
                                    text(
                                        "SELECT creation.id AS creation_id,creation.operation_id,"
                                        "operation.lease_token,entry.entry_index,entry.item_id,entry.publication_id,entry.file_path "
                                        "FROM public.managed_original_creations creation "
                                        "JOIN public.dataset_import_operations operation "
                                        "ON creation.authority->>'job_id'=operation.job_id::text "
                                        "JOIN public.dataset_import_entries entry "
                                        "ON entry.job_id=operation.job_id AND entry.entry_index=operation.cursor "
                                        "WHERE operation.job_id=:job AND creation.status='written'"
                                    ),
                                    {"job": job},
                                )
                            ).mappings().first()
                        if written is not None:
                            break
                        await asyncio.sleep(0.05)
                old_token = written["lease_token"]
                new_token = uuid4()
                old_request = {
                    "job_id": str(job), "lease_token": str(old_token),
                    "entry_index": written["entry_index"], "item_id": str(written["item_id"]),
                    "creation_id": str(written["creation_id"]),
                    "operation_id": str(written["operation_id"]), "mode": "write",
                }
                new_request = {**old_request, "lease_token": str(new_token)}
                async with _verification_finish_gate(
                    stack, test_engine, gated_lease=old_token
                ) as (release_old, wait_old):
                    async with _verification_finish_gate(
                        stack, test_engine, function="record_import_original_read", gated_lease=new_token
                    ) as (release_new, wait_new):
                        async with httpx.AsyncClient(base_url=old_origin, timeout=60) as old_source, \
                                httpx.AsyncClient(base_url=stack.verifier_origin, timeout=60) as new_source:
                            old_read = asyncio.create_task(old_source.post(
                                "/v1/import/verify", json=old_request,
                                headers={"Authorization": f"Bearer {stack.verifier_token}"},
                            ))
                            await wait_old()
                            _docker("kill", "--signal", "KILL", worker)
                            async with stack.world.maker.begin() as owner:
                                await owner.execute(
                                    text(
                                        "UPDATE public.dataset_import_operations SET "
                                        "lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
                                    ),
                                    {"job": job},
                                )
                            async with restricted.begin() as client:
                                claimed = await client.scalar(
                                    text(
                                        "SELECT aap_assets_v1.claim_dataset_import(:token,30,:job)"
                                    ),
                                    {"token": new_token, "job": job},
                                )
                            assert claimed["ok"] is True and claimed["data"]["lease_token"] == str(new_token)
                            new_read = asyncio.create_task(new_source.post(
                                "/v1/import/verify", json=new_request,
                                headers={"Authorization": f"Bearer {stack.verifier_token}"},
                            ))
                            await wait_new()
                            await release_old()
                            old_response = await old_read
                            assert old_response.status_code == 200 and old_response.json()["state"] == "fenced"
                            async with stack.world.maker() as observer:
                                assert await observer.scalar(
                                    text("SELECT status FROM public.managed_original_creations WHERE id=:id"),
                                    {"id": written["creation_id"]},
                                ) == first_outcome
                            source_login = create_async_engine(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
                            try:
                                forged = {**new_request, "operation_id": str(uuid4())}
                                async with source_login.begin() as client:
                                    denied = await client.scalar(
                                        text(
                                            "SELECT aap_assets_v1.finish_import_original_verification("
                                            "CAST(:command AS jsonb),NULL)"
                                        ),
                                        {"command": json.dumps(forged)},
                                    )
                                assert denied["state"] == "fenced"
                            finally:
                                await source_login.dispose()
                            async with stack.world.maker() as observer:
                                pending = (
                                    await observer.execute(
                                        text(
                                            "SELECT state,read_operation_id FROM aap_assets_v1.import_original_verifications "
                                            "WHERE job_id=:job AND lease_token=:token"
                                        ),
                                        {"job": job, "token": new_token},
                                    )
                                ).mappings().one()
                                assert pending["state"] == "pending"
                                assert await observer.scalar(
                                    text("SELECT EXISTS(SELECT 1 FROM public.storage_operations WHERE id=:id)"),
                                    {"id": pending["read_operation_id"]},
                                )
                            await release_new()
                            new_response = await new_read
                            assert new_response.status_code == 200
                            expected_state = "completed" if first_outcome == "verified" else "fenced"
                            assert new_response.json()["state"] == expected_state
                async with stack.world.maker() as observer:
                    receipts = (
                        await observer.execute(
                            text(
                                "SELECT lease_token,creation_id,operation_id,read_operation_id,state,evidence "
                                "FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"
                            ),
                            {"job": job},
                        )
                    ).mappings().all()
                    assert len(receipts) == 2
                    assert {row["creation_id"] for row in receipts} == {written["creation_id"]}
                    assert {row["operation_id"] for row in receipts} == {written["operation_id"]}
                    by_lease = {row["lease_token"]: row for row in receipts}
                    assert by_lease[old_token]["state"] == "fenced"
                    assert by_lease[new_token]["state"] == expected_state
                    if first_outcome == "failed":
                        assert by_lease[old_token]["evidence"]["result"] is None
                    else:
                        assert by_lease[old_token]["evidence"]["result"]["sha256"] == hashlib.sha256(data).hexdigest()
                    assert by_lease[new_token]["evidence"]["result"]["sha256"] == hashlib.sha256(data).hexdigest()
                    assert await observer.scalar(
                        text("SELECT count(*) FROM public.storage_operations WHERE id=ANY(CAST(:ids AS uuid[]))"),
                        {"ids": [
                            written["operation_id"], by_lease[old_token]["read_operation_id"],
                            by_lease[new_token]["read_operation_id"],
                        ]},
                    ) == 0
                result = by_lease[new_token]["evidence"]["result"]
                completion = ImportEntryCompletion(
                    status="copied", verification_operation_id=written["operation_id"],
                    file_size=result["size_bytes"], content_hash=result["md5"],
                    width=result["width"], height=result["height"],
                )
                config = load_config({
                    **stack.values,
                    "ASSETS_IMPORT_DATABASE_URL": _required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"),
                })
                native = await ImportDatabase.connect(config)
                try:
                    if first_outcome == "verified":
                        async with native.transaction() as client:
                            published = await client.complete_entry(
                                job, new_token, written["entry_index"], written["publication_id"], completion
                            )
                            assert published.added == 1
                            verified = ImportVerificationResult.model_validate(result)
                            identity = _object_identity_from_verified(verified, byte_size=verified.size_bytes)
                            await client.image_admission.admit_dataset_item(
                                item_id=written["item_id"],
                                publication_id=written["publication_id"],
                                file_path=written["file_path"],
                                file_size=verified.size_bytes,
                                content_hash=verified.md5,
                                object_identity=identity,
                                missing_is_skipped=True,
                            )
                    else:
                        with pytest.raises(ImportFailure) as failure:
                            async with native.transaction() as client:
                                await client.complete_entry(
                                    job, new_token, written["entry_index"], written["publication_id"], completion
                                )
                finally:
                    await native.close()
                if first_outcome == "verified":
                    facts = await _facts(stack)
                    assert len(facts.items) == 1
                    async with stack.world.maker() as observer:
                        bound = await observer.scalar(
                            text("SELECT media_object_identity FROM public.dataset_items WHERE id=:id"),
                            {"id": written["item_id"]},
                        )
                    assert bound == identity.model_dump(mode="json")
                else:
                    assert failure.value.code == "operation_conflict"
                    assert not (await _facts(stack)).items
        finally:
            await restricted.dispose()
            if denied_source is not None:
                _stop_remove_container(denied_source, stack.identity)


@pytest.mark.parametrize("actor_change", ["deactivate", "demote"])
async def test_accepted_import_finishes_when_actor_changes_during_readback(
    test_engine, app_module, monkeypatch, importer_image, actor_change
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("cyan")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        oversized = "/".join(("z" * 200, "z" * 200, "z" * 101 + ".png"))
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/{oversized}", Body=data
        )
        try:
            async with _verification_finish_gate(stack, test_engine) as (release, wait_for_gate):
                job = await _accept(stack)
                worker = _start_importer(stack, f"actor-{actor_change}")
                await _wait_healthy(worker)
                await wait_for_gate()
                async with stack.world.maker.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE public.users SET is_active=:active,role=:role WHERE id=:actor"
                        ),
                        {
                            "active": actor_change != "deactivate",
                            "role": "employee" if actor_change == "demote" else "super_admin",
                            "actor": stack.world.actor_id,
                        },
                    )
                await release()
                facts = await _wait_job(stack, job)
                assert facts.file_count == facts.tasks == facts.total_tasks == 1
                assert facts.jobs[0]["result"]["added"] == 1
                assert facts.jobs[0]["result"]["error_count"] == 1
                assert stack.storage.client.get_object(
                    Bucket=stack.storage.datasets_bucket, Key=facts.items[0]["file_path"]
                )["Body"].read() == data
        finally:
            async with stack.world.maker.begin() as owner:
                await owner.execute(
                    text("UPDATE public.users SET is_active=true,role='super_admin' WHERE id=:actor"),
                    {"actor": stack.world.actor_id},
                )


async def test_started_reuse_cannot_be_cancelled_while_reader_owns_permits(
    test_engine, app_module, monkeypatch, importer_image
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("blue")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        async with _publication_gate(stack, test_engine) as (_, wait_for_gate):
            job = await _accept(stack)
            worker = _start_importer(stack, "reuse-gate")
            await _wait_healthy(worker)
            await wait_for_gate()
            _docker("kill", "--signal", "KILL", worker)
            async with stack.world.maker() as observer:
                first = (
                    await observer.execute(
                        text(
                            "SELECT lease_token,entry_index,item_id,creation_id FROM "
                            "aap_assets_v1.import_original_verifications WHERE job_id=:job AND state='completed'"
                        ),
                        {"job": job},
                    )
                ).mappings().one()
        restricted = create_async_engine(_required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"))
        try:
            scope = {
                "job_id": str(job), "lease_token": str(first["lease_token"]),
                "entry_index": first["entry_index"], "item_id": str(first["item_id"]),
                "creation_id": str(first["creation_id"]), "reservation_id": str(uuid4()),
            }
            async with restricted.begin() as client:
                reserved = await client.scalar(
                    text("SELECT aap_assets_v1.reserve_import_original_read(CAST(:p AS jsonb))"),
                    {"p": json.dumps(scope)},
                )
            assert reserved["ok"] is True
            command = {
                **{key: value for key, value in scope.items() if key != "reservation_id"},
                "operation_id": reserved["data"]["operation_id"], "mode": "read",
            }
            async with _verification_finish_gate(
                stack, test_engine, function="record_import_original_read"
            ) as (release, wait_for_gate):
                async with httpx.AsyncClient(base_url=stack.verifier_origin, timeout=20) as source_api:
                    source_read = asyncio.create_task(source_api.post(
                        "/v1/import/verify", json=command,
                        headers={"Authorization": f"Bearer {stack.verifier_token}"},
                    ))
                    await wait_for_gate()
                    async with restricted.begin() as client:
                        rejected = await client.scalar(
                            text("SELECT aap_assets_v1.cancel_unstarted_import_original_read(CAST(:p AS jsonb))"),
                            {"p": json.dumps(command)},
                        )
                    assert rejected["state"] == "pending"
                    async with stack.world.maker() as observer:
                        pending = (
                            await observer.execute(
                                text(
                                    "SELECT state,operation_id,read_operation_id FROM "
                                    "aap_assets_v1.import_original_verifications "
                                    "WHERE operation_id=CAST(:id AS uuid)"
                                ),
                                {"id": command["operation_id"]},
                            )
                        ).mappings().one()
                        assert pending["state"] == "pending"
                        assert await observer.scalar(
                            text("SELECT count(*) FROM public.storage_operations WHERE id IN (:first,:second)"),
                            {"first": pending["operation_id"], "second": pending["read_operation_id"]},
                        ) == 2
                    await release()
                    result = await source_read
                    assert result.status_code == 200 and result.json()["state"] == "completed"
                    assert result.json()["result"]["sha256"] == hashlib.sha256(data).hexdigest()
                    async with stack.world.maker() as observer:
                        assert not await observer.scalar(
                            text("SELECT EXISTS (SELECT 1 FROM public.storage_operations WHERE id IN (:first,:second))"),
                            {"first": pending["operation_id"], "second": pending["read_operation_id"]},
                        )
        finally:
            await restricted.dispose()


async def test_drained_read_survives_owner_lock_contention(
    test_engine, app_module, monkeypatch, importer_image
):
    async with _import_stack(test_engine, app_module, monkeypatch, importer_image) as stack:
        data = _picture("orange")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        async with _verification_finish_gate(stack, test_engine) as (release, wait_for_gate):
            job = await _accept(stack)
            worker = _start_importer(stack, "verification-lock")
            await _wait_healthy(worker)
            await wait_for_gate()
            async with stack.world.maker() as observer:
                receipt = (
                    await observer.execute(
                        text(
                            "SELECT * FROM aap_assets_v1.import_original_verifications "
                            "WHERE job_id=:job"
                        ), {"job": job}
                    )
                ).mappings().one()
            assert receipt["state"] == "drained"
            assert receipt["evidence"]["result"]["sha256"] == hashlib.sha256(data).hexdigest()
            command = {
                "job_id": str(job), "lease_token": str(receipt["lease_token"]),
                "entry_index": receipt["entry_index"], "item_id": str(receipt["item_id"]),
                "creation_id": str(receipt["creation_id"]),
                "operation_id": str(receipt["operation_id"]), "mode": receipt["mode"],
            }
            async with test_engine.connect() as locker:
                transaction = await locker.begin()
                try:
                    await locker.execute(
                        text("SELECT id FROM public.datasets WHERE id=:id FOR NO KEY UPDATE"),
                        {"id": stack.world.dataset_id},
                    )
                    await release()
                    async with httpx.AsyncClient(base_url=stack.verifier_origin) as source_api:
                        status = await source_api.post(
                            "/v1/import/verify-status", json=command,
                            headers={"Authorization": f"Bearer {stack.verifier_token}"},
                        )
                    assert status.status_code == 200 and status.json()["state"] == "pending"
                    async with stack.world.maker() as observer:
                        retained = (
                            await observer.execute(
                                text(
                                    "SELECT state,evidence,read_operation_id FROM "
                                    "aap_assets_v1.import_original_verifications WHERE job_id=:job"
                                ), {"job": job}
                            )
                        ).mappings().one()
                        assert retained["state"] == "drained"
                        assert retained["evidence"] == receipt["evidence"]
                        assert await observer.scalar(
                            text("SELECT EXISTS (SELECT 1 FROM public.storage_operations WHERE id=:id)"),
                            {"id": receipt["read_operation_id"]},
                        )
                finally:
                    await transaction.rollback()
            facts = await _wait_job(stack, job)
            assert len(facts.items) == 1
            async with stack.world.maker() as observer:
                receipts = (
                    await observer.execute(
                        text("SELECT state,result,read_operation_id FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"),
                        {"job": job},
                    )
                ).mappings().all()
                assert len(receipts) == 1 and receipts[0]["state"] == "completed"
                assert receipts[0]["result"] == receipt["evidence"]["result"]
                assert not await observer.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM public.storage_operations WHERE id=:id)"),
                    {"id": receipt["read_operation_id"]},
                )


@pytest.mark.parametrize("duplicate", [False, True])
async def test_unstarted_read_cancellation_and_current_lease_publication_fence(
    test_engine, app_module, monkeypatch, importer_image, duplicate
):
    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        data = _picture("purple")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        if duplicate:
            first_job = await _accept(stack)
            worker = _start_importer(stack, "original")
            await _wait_healthy(worker)
            first = await _wait_job(stack, first_job)
            assert len(first.items) == 1
        async with _publication_gate(stack, test_engine) as (_, wait_for_gate):
            job = await _accept(stack)
            if not duplicate:
                worker = _start_importer(stack, "guard")
                await _wait_healthy(worker)
            await wait_for_gate()
            _docker("kill", "--signal", "KILL", worker)
            async with stack.world.maker() as observer:
                receipt = (
                    await observer.execute(
                        text(
                            "SELECT receipt.lease_token, receipt.creation_id, receipt.item_id, receipt.operation_id, "
                            "receipt.entry_index, receipt.result, "
                            "entry.publication_id FROM aap_assets_v1.import_original_verifications receipt "
                            "JOIN public.dataset_import_entries entry ON entry.job_id=receipt.job_id "
                            "AND entry.entry_index=receipt.entry_index "
                            "WHERE receipt.job_id=:job AND receipt.state='completed'"
                        ),
                        {"job": job},
                    )
                ).mappings().one()

        restricted = create_async_engine(_required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"))
        try:
            scope = {
                "job_id": str(job), "lease_token": str(receipt["lease_token"]),
                "entry_index": receipt["entry_index"], "item_id": str(receipt["item_id"]),
                "creation_id": str(receipt["creation_id"]),
                "reservation_id": str(uuid4()),
            }
            async with restricted.begin() as client:
                reservation = await client.scalar(
                    text("SELECT aap_assets_v1.reserve_import_original_read(CAST(:scope AS jsonb))"),
                    {"scope": json.dumps(scope)},
                )
            assert reservation["ok"] is True
            async with restricted.begin() as client:
                replay = await client.scalar(
                    text("SELECT aap_assets_v1.reserve_import_original_read(CAST(:scope AS jsonb))"),
                    {"scope": json.dumps(scope)},
                )
            assert replay == reservation
            async with stack.world.maker() as observer:
                assert await observer.scalar(
                    text(
                        "SELECT count(*) FROM aap_assets_v1.import_original_verifications "
                        "WHERE job_id=:job AND reservation_id=CAST(:reservation AS uuid) AND state='reserved'"
                    ),
                    {"job": job, "reservation": scope["reservation_id"]},
                ) == 1
            operation_id = reservation["data"]["operation_id"]
            request = {**{key: value for key, value in scope.items() if key != "reservation_id"},
                       "operation_id": operation_id, "mode": "read"}
            from yoao_assets_source.managed_original_contract import ManagedOriginalOperation
            from yoao_assets_source_import.config import load_config
            from yoao_assets_source_import.database import ImportDatabase
            from yoao_assets_source_import.verification import ExpectedOriginal
            from yoao_assets_source_import.workflow import ImportWorkflow, LeaseUnavailable

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable:
                unavailable.bind(("127.0.0.1", 0))
                config = load_config({
                    **stack.values,
                    "ASSETS_IMPORT_DATABASE_URL": _required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"),
                    "ASSETS_IMPORT_VERIFY_URL": f"http://127.0.0.1:{unavailable.getsockname()[1]}",
                    "ASSETS_IMPORT_S3_READ_TIMEOUT_SECONDS": "0.2",
                })
                database = await ImportDatabase.connect(config)
                try:
                    entry = SimpleNamespace(
                        job_id=job, lease_token=receipt["lease_token"], index=receipt["entry_index"],
                        item_id=receipt["item_id"],
                    )
                    target = SimpleNamespace(creation_id=receipt["creation_id"])
                    keeper = SimpleNamespace(check=lambda: None, _remaining_lease=lambda: 5)
                    with pytest.raises(LeaseUnavailable):
                        await ImportWorkflow(database, None, config)._verify_original(
                            entry,
                            target,
                            ManagedOriginalOperation.model_validate(reservation["data"]),
                            keeper,
                            ExpectedOriginal(
                                sha256=receipt["result"]["sha256"],
                                size_bytes=receipt["result"]["size_bytes"],
                                etag=receipt["result"]["etag"],
                                version_id=receipt["result"]["version_id"],
                            ),
                        )
                finally:
                    await database.close()
            async with restricted.begin() as client:
                cancelled = await client.scalar(
                    text("SELECT aap_assets_v1.cancel_unstarted_import_original_read(CAST(:scope AS jsonb))"),
                    {"scope": json.dumps(request)},
                )
            assert cancelled["state"] == "cancelled"
            async with stack.world.maker() as observer:
                assert not await observer.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM public.storage_operations WHERE id=CAST(:id AS uuid))"),
                    {"id": operation_id},
                )
            async with httpx.AsyncClient(base_url=stack.verifier_origin) as source_api:
                late = await source_api.post(
                    "/v1/import/verify", json=request,
                    headers={"Authorization": f"Bearer {stack.verifier_token}"},
                )
            assert late.status_code == 200 and late.json()["state"] == "fenced"
            # Simulate a worker that commits a reservation and dies before retaining its reply.
            lost_scope = {**scope, "reservation_id": str(uuid4())}
            async with restricted.begin() as client:
                await client.execute(
                    text("SELECT aap_assets_v1.reserve_import_original_read(CAST(:scope AS jsonb))"),
                    {"scope": json.dumps(lost_scope)},
                )


            replacement_token = uuid4()
            async with stack.world.maker.begin() as owner:
                await owner.execute(
                    text(
                        "UPDATE public.dataset_import_operations SET lease_token=:token, "
                        "lease_expires_at=clock_timestamp()+interval '30 seconds' WHERE job_id=:job"
                    ),
                    {"job": job, "token": replacement_token},
                )
            async with asyncio.timeout(15):
                while True:
                    async with stack.world.maker() as observer:
                        orphan = (
                            await observer.execute(
                                text(
                                    "SELECT state,operation_id FROM aap_assets_v1.import_original_verifications "
                                    "WHERE job_id=:job AND reservation_id=CAST(:reservation AS uuid)"
                                ),
                                {"job": job, "reservation": lost_scope["reservation_id"]},
                            )
                        ).mappings().one()
                        if orphan["state"] == "cancelled":
                            assert not await observer.scalar(
                                text("SELECT EXISTS (SELECT 1 FROM public.storage_operations WHERE id=:id)"),
                                {"id": orphan["operation_id"]},
                            )
                            break
                    await asyncio.sleep(0.1)
            result = receipt["result"]
            completion = {
                "status": "copied", "file_size": result["size_bytes"],
                "verification_operation_id": str(receipt["operation_id"]),
                "content_hash": result["md5"], "width": result["width"], "height": result["height"],
            }
            async with restricted.begin() as client:
                rejected = await client.scalar(
                    text(
                        "SELECT aap_assets_v1.complete_dataset_import_entry("
                        ":job, :token, :index, :publication, CAST(:completion AS jsonb))"
                    ),
                    {
                        "job": job, "token": replacement_token, "index": receipt["entry_index"],
                        "publication": receipt["publication_id"], "completion": json.dumps(completion),
                    },
                )
            assert rejected["ok"] is False and rejected["error"]["code"] == "operation_conflict"
            facts = await _facts(stack)
            assert len(facts.items) == int(duplicate)
            async with stack.world.maker() as observer:
                entry = (
                    await observer.execute(
                        text("SELECT status FROM public.dataset_import_entries WHERE job_id=:job"),
                        {"job": job},
                    )
                ).scalar_one()
            assert entry == "pending"
        finally:
            await restricted.dispose()


async def test_import_sigkill_after_copy_reclaims_without_duplicate_sql_publication(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        data = _picture("green")
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=f"{stack.prefix}/batch/a.png", Body=data
        )
        job = await _accept(stack)
        async with _publication_gate(
            stack, test_engine, after_settlement=True
        ) as (release, wait_for_gate):
            worker = _start_importer(stack, "kill")
            await _wait_healthy(worker)
            old_paths = await _wait_copy(stack)
            await wait_for_gate()
            before_rollback = await _facts(stack)
            assert not before_rollback.items
            assert before_rollback.tasks == before_rollback.total_tasks == 0
            assert before_rollback.file_count == 0 and not before_rollback.deliveries
            async with stack.world.maker() as observer:
                receipt = (
                    await observer.execute(
                        text(
                            "SELECT lease_token,creation_id,operation_id,item_id,entry_index,mode,state,result "
                            "FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"
                        ),
                        {"job": job},
                    )
                ).mappings().one()
            assert receipt["mode"] == "write" and receipt["state"] == "completed"
            assert receipt["result"]["sha256"] == hashlib.sha256(data).hexdigest()
            assert receipt["result"]["size_bytes"] == len(data)
            assert receipt["result"]["version_id"]
            _docker("kill", "--signal", "KILL", worker)
            await release()
        await asyncio.sleep(0.2)
        rolled_back = await _facts(stack)
        assert not rolled_back.items
        assert rolled_back.tasks == rolled_back.total_tasks == 0
        assert rolled_back.file_count == 0 and not rolled_back.deliveries
        async with stack.world.maker.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.dataset_import_operations SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:id"
                ),
                {"id": job},
            )
        restarted = _start_importer(stack, "retry")
        await _wait_healthy(restarted)
        facts = await _wait_job(stack, job)
        assert facts.file_count == facts.tasks == facts.total_tasks == 1, facts.jobs[0][
            "result"
        ]
        assert (
            facts.jobs[0]["result"]["added"] == 1
            and facts.jobs[0]["result"]["error_count"] == 0
        )
        paths = _objects(stack)
        assert len(paths) == 2 and facts.items[0]["file_path"] not in old_paths
        async with stack.world.maker() as observer:
            receipts = (
                await observer.execute(
                    text(
                        "SELECT lease_token,creation_id,state,result "
                        "FROM aap_assets_v1.import_original_verifications WHERE job_id=:job"
                    ),
                    {"job": job},
                )
            ).mappings().all()
        assert len(receipts) == 2
        assert all(row["state"] == "completed" for row in receipts)
        assert len({row["lease_token"] for row in receipts}) == 2
        assert len({row["creation_id"] for row in receipts}) == 2
        async with httpx.AsyncClient(base_url=stack.verifier_origin) as source_api:
            old_status = await source_api.post(
                "/v1/import/verify-status",
                headers={"Authorization": f"Bearer {stack.verifier_token}"},
                json={
                    "job_id": str(job),
                    "lease_token": str(receipt["lease_token"]),
                    "entry_index": receipt["entry_index"],
                    "item_id": str(receipt["item_id"]),
                    "creation_id": str(receipt["creation_id"]),
                    "operation_id": str(receipt["operation_id"]),
                    "mode": receipt["mode"],
                },
            )
            assert old_status.status_code == 200
            assert old_status.json()["state"] == "fenced"
        for path in paths:
            assert (
                stack.storage.client.get_object(
                    Bucket=stack.storage.datasets_bucket, Key=path
                )["Body"].read()
                == data
            )


async def test_cancel_and_delete_fence_a_copied_late_import_result(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        data = _picture("red")
        source = f"{stack.prefix}/batch/a.png"
        stack.storage.client.put_object(
            Bucket=stack.source_bucket, Key=source, Body=data
        )
        job = await _accept(stack)
        async with _publication_gate(stack, test_engine) as (release, _):
            worker = _start_importer(stack, "cancel")
            await _wait_healthy(worker)
            await _wait_copy(stack)
            blocked = await stack.api.delete(
                f"/api/v1/datasets/{stack.world.dataset_id}", headers=stack.headers
            )
            assert blocked.status_code == 409
            cancelled = await stack.api.post(
                f"/api/v1/async-jobs/{job}/cancel", headers=stack.headers
            )
            assert cancelled.status_code == 200, cancelled.text
            deletion = await stack.api.delete(
                f"/api/v1/datasets/{stack.world.dataset_id}", headers=stack.headers
            )
            assert deletion.status_code == 204, deletion.text
            await release()
            await asyncio.sleep(0.3)
            facts = await _facts(stack)
            assert facts.jobs[0]["status"] == "cancelled"
            assert not facts.items and facts.tasks == facts.total_tasks == 0
            assert facts.file_count is None
        assert (
            stack.storage.client.get_object(Bucket=stack.source_bucket, Key=source)[
                "Body"
            ].read()
            == data
        )


async def test_import_storage_identities_cannot_delete_read_destination_or_write_source(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    from app.config import settings

    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        command = {
            "job_id": str(uuid4()), "lease_token": str(uuid4()), "entry_index": 0,
            "item_id": str(uuid4()), "creation_id": str(uuid4()),
            "operation_id": str(uuid4()), "mode": "read",
        }
        async with httpx.AsyncClient(base_url=stack.verifier_origin) as source_api:
            wrong_lane = await source_api.post(
                "/v1/import/verify", json=command,
                headers={"Authorization": f"Bearer {stack.source_gateway_token}"},
            )
            assert wrong_lane.status_code == 401
            gateway = await source_api.post(
                "/v1/source/upload-init",
                json={
                    "item_id": str(uuid4()),
                    "actor_id": str(stack.world.actor_id),
                    "dataset_id": str(stack.world.dataset_id),
                    "file_name": "denied.png",
                    "content_type": "image/png",
                },
                headers={"Authorization": f"Bearer {stack.verifier_token}"},
            )
            assert gateway.status_code == 401
            locator = await source_api.post(
                "/v1/import/verify", json={**command, "bucket": "other"},
                headers={"Authorization": f"Bearer {stack.verifier_token}"},
            )
            assert locator.status_code == 400
            timeout_override = await source_api.post(
                "/v1/import/verify", json={**command, "timeout_seconds": 600},
                headers={"Authorization": f"Bearer {stack.verifier_token}"},
            )
            assert timeout_override.status_code == 400
            absent = await source_api.post(
                "/v1/import/verify-status", json=command,
                headers={"Authorization": f"Bearer {stack.verifier_token}"},
            )
            assert absent.status_code == 200 and absent.json()["state"] == "missing"
        for role_env, statements in (
            (
                "AAP_MODULE_DATA_ASSET_SOURCE_URL",
                ("SELECT * FROM aap_assets_v1.import_original_verifications LIMIT 1",),
            ),
            (
                "AAP_MODULE_DATA_ASSET_IMPORT_URL",
                (
                    "SELECT * FROM aap_assets_v1.import_original_verifications LIMIT 1",
                    "SELECT aap_assets_v1.record_import_original_read(CAST('{}' AS jsonb), NULL)",
                    "SELECT aap_assets_v1.finish_import_original_verification(CAST('{}' AS jsonb), NULL)",
                    "SELECT aap_assets_v1.recover_import_original_verification(NULL::jsonb)",
                    "SELECT aap_assets_v1.record_operator_original_verified(NULL::uuid, CAST('{}' AS jsonb))",
                ),
            ),
        ):
            restricted = create_async_engine(_required_env(role_env))
            try:
                async with restricted.connect() as connection:
                    for statement in statements:
                        with pytest.raises(DBAPIError) as denied:
                            await connection.execute(text(statement))
                        assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                        await connection.rollback()
            finally:
                await restricted.dispose()
        endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        destination = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=stack.values["ASSETS_IMPORT_S3_ACCESS_KEY"],
            aws_secret_access_key=stack.values["ASSETS_IMPORT_S3_SECRET_KEY"],
        )
        source = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=_required_env(
                "AAP_MODULE_DATA_IMPORT_SOURCE_S3_ACCESS_KEY"
            ),
            aws_secret_access_key=_required_env(
                "AAP_MODULE_DATA_IMPORT_SOURCE_S3_SECRET_KEY"
            ),
        )
        key = f"dataset-sources/{stack.world.dataset_id}/{uuid4()}/probe"
        destination.put_object(
            Bucket=stack.storage.datasets_bucket, Key=key, Body=b"owned"
        )
        for method, arguments in (
            (
                destination.get_object,
                {"Bucket": stack.storage.datasets_bucket, "Key": key},
            ),
            (
                destination.delete_object,
                {"Bucket": stack.storage.datasets_bucket, "Key": key},
            ),
            (
                destination.list_objects_v2,
                {"Bucket": stack.storage.datasets_bucket, "Prefix": "dataset-sources/"},
            ),
            (
                destination.put_object,
                {
                    "Bucket": stack.storage.datasets_bucket,
                    "Key": "legacy/probe",
                    "Body": b"denied",
                },
            ),
            (
                source.put_object,
                {
                    "Bucket": stack.source_bucket,
                    "Key": f"{stack.prefix}/probe",
                    "Body": b"denied",
                },
            ),
            (source.get_object, {"Bucket": stack.storage.datasets_bucket, "Key": key}),
        ):
            with pytest.raises(ClientError) as failure:
                method(**arguments)
            assert failure.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
        upload = destination.create_multipart_upload(
            Bucket=stack.storage.datasets_bucket, Key=key + "-multipart"
        )
        destination.abort_multipart_upload(
            Bucket=stack.storage.datasets_bucket,
            Key=key + "-multipart",
            UploadId=upload["UploadId"],
        )


@asynccontextmanager
async def _sftp_server(directory, host):
    import errno
    import posixpath
    import threading
    import paramiko

    root = directory / "sftp"
    (root / "data/good").mkdir(parents=True)
    (root / "data/bad").mkdir()
    data = _picture("blue")
    (root / "data/good/a.png").write_bytes(data)
    (root / "outside.png").write_bytes(_picture("black"))
    (root / "data/bad/escape.png").symlink_to("../../outside.png")
    key = paramiko.RSAKey.generate(2048)
    stop = threading.Event()
    connections = []
    threads = []

    class Auth(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return (
                paramiko.AUTH_SUCCESSFUL
                if (username, password) == ("import", "owned-test")
                else paramiko.AUTH_FAILED
            )

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return (
                paramiko.OPEN_SUCCEEDED
                if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            )

    class Handle(paramiko.SFTPHandle):
        def stat(self):
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))

    class Files(paramiko.SFTPServerInterface):
        def _local(self, path):
            candidate = root / posixpath.normpath("/" + path.lstrip("/")).lstrip("/")
            assert candidate.resolve().is_relative_to(root)
            return candidate

        def canonicalize(self, path):
            try:
                return "/" + str(self._local(path).resolve().relative_to(root))
            except (AssertionError, OSError):
                return "/outside-root"

        def list_folder(self, path):
            try:
                entries = []
                for entry in sorted(self._local(path).iterdir()):
                    attrs = paramiko.SFTPAttributes.from_stat(entry.lstat())
                    attrs.filename = entry.name
                    entries.append(attrs)
                return entries
            except (AssertionError, OSError):
                return paramiko.SFTP_PERMISSION_DENIED

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self._local(path).stat())
            except (AssertionError, OSError):
                return paramiko.SFTP_NO_SUCH_FILE

        def lstat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self._local(path).lstat())
            except (AssertionError, OSError):
                return paramiko.SFTP_NO_SUCH_FILE

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                return paramiko.SFTP_PERMISSION_DENIED
            try:
                handle = Handle(flags)
                handle.readfile = self._local(path).open("rb")
                return handle
            except AssertionError:
                return paramiko.SFTP_PERMISSION_DENIED
            except OSError as error:
                return paramiko.SFTPServer.convert_errno(error.errno or errno.EIO)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, 0))
    listener.listen(4)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]

    def serve(connection):
        transport = paramiko.Transport(connection)
        connections.append(transport)
        transport.add_server_key(key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
        try:
            transport.start_server(server=Auth())
            while transport.is_active() and not stop.wait(0.1):
                pass
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def accept():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=serve, args=(connection,), daemon=True)
            threads.append(thread)
            thread.start()

    server = threading.Thread(target=accept, daemon=True)
    server.start()
    known = directory / "known_hosts"
    known.write_text(f"[{host}]:{port} {key.get_name()} {key.get_base64()}\n")
    known.chmod(0o644)
    try:
        yield SimpleNamespace(host=host, port=port, known_hosts=known, data=data)
    finally:
        stop.set()
        listener.close()
        for transport in connections:
            transport.close()
        await asyncio.to_thread(server.join, 3)
        for thread in threads:
            await asyncio.to_thread(thread.join, 3)
        assert not server.is_alive() and not any(
            thread.is_alive() for thread in threads
        )


async def test_real_sftp_requires_known_host_and_rejects_escape(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    from app.core.crypto import encrypt_secret

    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image, sftp=True
    ) as stack:
        async with _sftp_server(stack.directory, stack.sftp_host) as server:
            async with stack.world.maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.storage_connections SET kind='sftp',config=CAST(:config AS jsonb),secret_enc=:secret WHERE id=:id"
                    ),
                    {
                        "id": stack.connection_id,
                        "config": json.dumps(
                            {
                                "host": server.host,
                                "port": server.port,
                                "username": "import",
                                "base_path": "/data",
                                "auth_type": "password",
                            }
                        ),
                        "secret": encrypt_secret({"password": "owned-test"}),
                    },
                )
            body = {
                "connection_id": str(stack.connection_id),
                "source_path": "good",
                "recursive": True,
                "include_globs": ["*.png"],
            }
            first = await _accept(stack, body=body)
            unknown = _start_importer(stack, "unknown-host")
            await _wait_healthy(unknown)
            facts = await _wait_job(stack, first, status="failed")
            assert not facts.items and facts.tasks == 0
            _docker("stop", "--time", "3", unknown)
            values = {
                **stack.values,
                "ASSETS_IMPORT_SFTP_KNOWN_HOSTS": "/run/secrets/known_hosts",
            }
            second = await _accept(stack, body={**body, "source_path": "/data/good"})
            known = _start_importer(
                stack,
                "known-host",
                values=values,
                mounts=((server.known_hosts, "/run/secrets/known_hosts"),),
            )
            await _wait_healthy(known)
            facts = await _wait_job(stack, second)
            assert facts.file_count == facts.tasks == 1
            assert facts.items[0]["file_name"] == "a.png"
            assert (
                stack.storage.client.get_object(
                    Bucket=stack.storage.datasets_bucket,
                    Key=facts.items[0]["file_path"],
                )["Body"].read()
                == server.data
            )
            third = await _accept(stack, body={**body, "source_path": "bad"})
            rejected = await _wait_job(stack, third)
            result = next(job["result"] for job in rejected.jobs if job["id"] == third)
            assert result["added"] == result["linked_tasks"] == 0
            assert result["total"] == result["error_count"] == 1
            assert result["errors"] == ["bad/escape.png: invalid_source_object"]
            assert rejected.file_count == rejected.tasks == 1
            assert len(_objects(stack)) == 1


async def test_importer_rejects_broad_relation_and_reverse_membership_credentials(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    from tests.test_assets_catalog_runtime import _assert_startup_rejected

    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        broad_url = make_url(_required_env("AAP_MODULE_DATA_LEGACY_RUNTIME_URL"))
        unsafe = _start_importer(
            stack,
            "broad-login",
            values={
                **stack.values,
                "ASSETS_IMPORT_DATABASE_URL": _container_database_url(broad_url),
            },
        )
        await _assert_startup_rejected(unsafe)
        async with test_engine.begin() as owner:
            await owner.execute(
                text("GRANT SELECT ON public.datasets TO asset_import_client")
            )
        try:
            unsafe = _start_importer(stack, "relation-grant")
            await _assert_startup_rejected(unsafe)
        finally:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text("REVOKE SELECT ON public.datasets FROM asset_import_client")
                )
        member = f"import_member_{uuid4().hex[:12]}"
        async with test_engine.begin() as owner:
            await owner.execute(text(f"CREATE ROLE {member} NOLOGIN NOINHERIT"))
            await owner.execute(text(f"GRANT asset_import_client TO {member}"))
        try:
            unsafe = _start_importer(stack, "reverse-member")
            await _assert_startup_rejected(unsafe)
        finally:
            async with test_engine.begin() as owner:
                await owner.execute(text(f"REVOKE asset_import_client FROM {member}"))
                await owner.execute(text(f"DROP ROLE {member}"))


async def test_import_notification_reaches_channel_and_image_work_is_accepted(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    import redis.asyncio as aioredis
    from app.config import settings
    from app.workers.dataset_import_delivery import _relay_pending

    async with _import_stack(
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        broker = urlsplit(settings.celery_broker_url)
        assert broker.hostname in {"127.0.0.1", "localhost"} and broker.port
        observed = _inspect_docker(stack.resources["redis_name"])
        assert (
            observed["Config"]["Labels"]["org.aap.worktree-owner"]
            == stack.resources["owner"]
        )
        assert observed["Config"]["Image"] == "redis:7-alpine"
        assert any(
            binding.get("HostIp") == "127.0.0.1"
            and int(binding["HostPort"]) == broker.port
            for binding in observed["NetworkSettings"]["Ports"]["6379/tcp"]
        )
        stack.storage.client.put_object(
            Bucket=stack.source_bucket,
            Key=f"{stack.prefix}/batch/a.png",
            Body=_picture("yellow"),
        )
        job = await _accept(stack)
        worker = _start_importer(stack, "delivery")
        await _wait_healthy(worker)
        facts = await _wait_job(stack, job)
        notification_id = str(
            next(target for kind, target in facts.deliveries if kind == "notification")
        )
        client = aioredis.from_url(settings.redis_url, socket_timeout=5)
        try:
            async with client.pubsub() as channel:
                await channel.subscribe(f"notify:{stack.world.actor_id}")
                await channel.get_message(timeout=2)
                totals = await _relay_pending()
                assert totals["delivered"] == 1 and totals["deferred"] == 0
                async with asyncio.timeout(5):
                    while True:
                        notification = await channel.get_message(
                            ignore_subscribe_messages=True, timeout=1
                        )
                        if notification is not None:
                            payload = json.loads(notification["data"])
                            assert payload["id"] == notification_id
                            assert payload["target_id"] == str(job)
                            break
            async with stack.world.maker() as db:
                item = facts.items[0]
                identity = await db.scalar(
                    text("SELECT media_object_identity FROM public.dataset_items WHERE id=:id"),
                    {"id": item["id"]},
                )
                head = stack.storage.client.head_object(
                    Bucket=stack.storage.datasets_bucket, Key=item["file_path"]
                )
                assert identity["etag"] == head["ETag"].strip('"')
                assert identity["byte_size"] == head["ContentLength"]
                image_jobs = (
                    await db.execute(
                        text(
                            "SELECT kind, status FROM public.asset_image_jobs "
                            "WHERE owner_kind='dataset_item' AND owner_id=:id"
                        ),
                        {"id": item["id"]},
                    )
                ).all()
                assert len(image_jobs) == 1
                assert image_jobs[0].kind == "thumbnail"
                assert image_jobs[0].status in {"pending", "running", "ready"}
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.dataset_import_deliveries WHERE job_id=:id AND delivered_at IS NOT NULL"
                        ),
                        {"id": job},
                    )
                    == 2
                )
        finally:
            await client.aclose()
