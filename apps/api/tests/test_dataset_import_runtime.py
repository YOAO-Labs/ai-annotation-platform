"""Real isolated SQL/S3/container acceptance for connector import execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
import httpx
from PIL import Image
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _inspect_docker,
    _remove_worker_image,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_postgres,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import _write_environment
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
        yield image, identity
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
    container = _docker(*command, stack.image)
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
async def _import_stack(test_engine, app_module, monkeypatch, importer_image):
    from app.config import settings
    from app.core.crypto import encrypt_secret
    from app.core.security import create_access_token
    from app.db.models.storage_connection import StorageConnection
    from app.deps import get_db
    from app.services.storage import storage_service

    role_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_IMPORT_URL"))
    assert (
        role_url.username == "asset_import_client"
        and role_url.database == test_engine.url.database
    )
    _verify_postgres(role_url)
    resources = _verified_resource_manifest(test_engine, role_url)
    image, identity = importer_image
    world = await _seed_world(test_engine)
    connection_id = uuid4()
    prefix = f"connector-import-source/{uuid4()}"
    minio = urlsplit(
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    source_endpoint = f"{minio.scheme}://{_host_address()}:{minio.port or 9000}"
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
                "('connector_host_allowlist','json','[\"10.0.0.0/8\",\"172.16.0.0/12\",\"192.168.0.0/16\"]'::jsonb) "
                "ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"
            )
        )
    auth_engine = create_async_engine(test_engine.url, hide_parameters=True)

    @event.listens_for(auth_engine.sync_engine, "before_cursor_execute")
    def forbid_private_reads(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if re.search(
            r'\b(?:FROM|JOIN)\s+(?:public\.)?"?(?:datasets|dataset_items|storage_connections|project_datasets|tasks)\b',
            statement,
            re.I,
        ):
            raise AssertionError("import gateway queried provider-private relations")

    async def request_db():
        async with async_sessionmaker(auth_engine, expire_on_commit=False)() as db:
            yield db

    previous = dict(app_module.dependency_overrides)
    app_module.dependency_overrides[get_db] = request_db
    stack = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="assets-import-runtime-", dir=os.environ["TMPDIR"]
        ) as directory:
            values = {
                "ASSETS_IMPORT_DATABASE_URL": _container_database_url(role_url),
                "ASSETS_IMPORT_S3_ENDPOINT": f"{minio.scheme}://host.docker.internal:{minio.port or 9000}",
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
                "CONNECTOR_HOST_ALLOWLIST": '["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"]',
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
                    source_bucket=resources["buckets"]["MINIO_IMPORT_BUCKET"],
                    resources=resources,
                    source_secret=connector_secret,
                    values=values,
                    directory=Path(directory),
                    image=image,
                    identity=identity,
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
        before = await _facts(stack)
        assert (
            len(before.jobs) == 1
            and before.jobs[0]["status"] == "pending"
            and not before.items
        )
        assert before.jobs[0]["payload"]["settings_snapshot"][
            "dataset_import_max_total_bytes"
        ] == 2 * len(data)
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


@asynccontextmanager
async def _publication_gate(stack, test_engine):
    signature = "uuid,uuid,bigint,uuid,jsonb"
    alias = "_acceptance_import_completion"
    gate = secrets.randbits(60)
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
                BEGIN
                    PERFORM pg_catalog.pg_advisory_xact_lock({gate});
                    RETURN aap_assets_v1.{alias}(p_job,p_token,p_index,p_publication,p_completion);
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

        try:
            yield release
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
        async with _publication_gate(stack, test_engine) as release:
            worker = _start_importer(stack, "kill")
            await _wait_healthy(worker)
            old_paths = await _wait_copy(stack)
            assert not (await _facts(stack)).items
            _docker("kill", "--signal", "KILL", worker)
            await release()
        await asyncio.sleep(0.2)
        assert not (await _facts(stack)).items
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
        async with _publication_gate(stack, test_engine) as release:
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
        test_engine, app_module, monkeypatch, importer_image
    ) as stack:
        async with _sftp_server(stack.directory, _host_address()) as server:
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


async def test_import_delivery_reaches_real_owned_broker_and_notification_channel(
    test_engine,
    app_module,
    monkeypatch,
    importer_image,
):
    import base64
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
        own_messages = []
        try:
            async with client.pubsub() as channel:
                await channel.subscribe(f"notify:{stack.world.actor_id}")
                await channel.get_message(timeout=2)
                totals = await _relay_pending()
                assert totals["delivered"] == 2 and totals["deferred"] == 0
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
                for message in await client.lrange("media", 0, -1):
                    payload = json.loads(message)
                    if (
                        payload.get("headers", {}).get("task")
                        != "app.workers.image_pyramid.execute_image_media"
                    ):
                        continue
                    body = json.loads(base64.b64decode(payload["body"]))
                    if body[0] == [None]:
                        own_messages.append(message)
                assert len(own_messages) == 1
            async with stack.world.maker() as db:
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
            for message in own_messages:
                await client.lrem("media", 1, message)
            await client.aclose()
