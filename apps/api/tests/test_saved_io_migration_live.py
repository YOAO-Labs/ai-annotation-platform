"""Disposable real-role, Source HTTP and MinIO migration qualification."""

import asyncio
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import hashlib
import multiprocessing
import os
from pathlib import Path
import re
import signal
from urllib.parse import parse_qs, urlsplit
import uuid

import asyncpg
import httpx
from fastapi import HTTPException
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yoao_assets_source.saved_io import SavedIOClient, SavedIOError

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset_asset import StorageBinding
from app.db.models.export_output import ExportOutput
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.storage_migration_copy import StorageMigrationCopyDelta
from app.db.models.storage_migration_finalization import StorageMigrationFinalization
from app.db.models.storage_migration_read_publication import (
    StorageMigrationReadPublication,
    StorageMigrationReadRoute,
)
from app.db.models.storage_migration_window import StorageMigrationWindow
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyAttemptRequest,
    StorageMigrationCopyRequest,
)
from app.schemas.storage_migration_inventory import StorageMigrationInventoryRequest
from app.services import (
    export_outputs,
    export_storage,
    managed_s3_target,
    storage_access,
)
from app.services import storage_migration_artifact_reads as artifacts
from app.services import storage_migration_copies as copies
from app.services import storage_migration_inventories as inventories
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    PURPOSE_BUCKET_ATTRIBUTES,
    ensure_legacy_bindings,
)
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.storage_location import register_connection
from app.workers import storage_migration_copy as worker
from app.workers import storage_migration_inventory as inventory_worker
from tests.factory import create_user
from tests.saved_io_live import real_source, registered_sink

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires cold named-role verifier and owned disposable PG/MinIO",
    ),
]

_APP = "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"
_SOURCE = "AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"
_OPERATOR = "AAP_MODULE_DATA_STORAGE_MIGRATION_CONTROL_URL"
_BYTES = b"source version remains byte-for-byte retained after physical copy" * (
    256 * 1024
)


def _url(key, owner):
    url = make_url(os.environ[key])
    assert (url.host, url.port, url.database) == (
        owner.host,
        owner.port,
        owner.database,
    )
    assert url.username != owner.username
    return url


async def _permissions(url, *, source=False):
    conn = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    try:
        assert await conn.fetchval("SELECT current_user") == url.username
        for statement in (
            "SELECT secret_enc FROM public.storage_connections LIMIT 1",
            "SELECT * FROM public.storage_migration_io_reads LIMIT 1",
            "SELECT * FROM public.storage_migration_io_writes LIMIT 1",
            "SELECT * FROM public.storage_migration_io_probes LIMIT 1",
            "SELECT * FROM public.storage_migration_io_flights LIMIT 1",
            "SELECT * FROM public.storage_migration_copy_deltas LIMIT 1",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(statement)
        if source:
            assert (
                await conn.fetchval("""
                SELECT yoao_assets_v1.migration_io_copy(
                    gen_random_uuid(), gen_random_uuid(), 'absent', gen_random_uuid(), 'run', 1, 1, NULL)
            """)
                is None
            )
            denied = "SELECT yoao_assets_v1.migration_io_bind_read(gen_random_uuid(), gen_random_uuid(), 1, '{}'::jsonb)"
        else:
            denied = "SELECT yoao_assets_v1.migration_io_copy(gen_random_uuid(), gen_random_uuid(), 'absent', gen_random_uuid(), 'run', 1, 1, NULL)"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(denied)
    finally:
        await conn.close()


def _object(storage, bucket, key, version):
    with closing(
        storage.get_object(Bucket=bucket, Key=key, VersionId=version)["Body"]
    ) as body:
        return body.read()


def _rejected(call, *args, **kwargs):
    with pytest.raises(SavedIOError) as caught:
        call(*args, **kwargs)
    assert caught.value.status_code in {400, 401, 403, 404, 409}
    assert not caught.value.unsettled
    return caught.value


async def _copy_attempt(db, actor, copy_id):
    _, job, reused = await copies.attempt(
        db, copy_id, actor, StorageMigrationCopyAttemptRequest(operation="run")
    )
    assert not reused
    await db.commit()
    return job.id, job.celery_task_id


def _configure_inventory_bucket(bucket, assign=setattr):
    for attr in PURPOSE_BUCKET_ATTRIBUTES.values():
        assign(settings, f"minio_{attr}", bucket)
        assign(storage_service, attr, bucket)


def _safe_child_code(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value)
        else None
    )


def _child_failure(stage, exc):
    failure = {"stage": stage, "type": type(exc).__name__}
    if isinstance(exc, SavedIOError):
        failure.update(
            status=exc.status_code,
            code=_safe_child_code(exc.code),
            storage_code=exc.storage_code,
            unsettled=exc.unsettled,
        )
    elif isinstance(exc, HTTPException):
        failure["status"] = exc.status_code
        if isinstance(exc.detail, dict):
            failure["code"] = _safe_child_code(exc.detail.get("code"))
    elif isinstance(exc, DBAPIError):
        state = getattr(exc.orig, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
            failure["sqlstate"] = state
    return failure


def _hold_verified_body(
    db_url, endpoint, bucket, origin, tokens, job_id, task_id, row, ready
):
    """A separate worker PID starts the real Source read, then stops consuming it."""
    stage = "configure"

    async def start():
        nonlocal stage
        stage = "connect"
        engine = create_async_engine(db_url)
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            async with maker() as db:
                stage = "claim"
                attempt = await copies.claim(db, job_id, task_id)
                if attempt is None:
                    outcome = await copies.by_job(db, job_id)
                    await db.commit()
                    ready.send(
                        (
                            {
                                "stage": stage,
                                "type": "NoClaim",
                                "job_status": _safe_child_code(
                                    outcome and outcome.status
                                ),
                                "code": _safe_child_code(
                                    outcome and outcome.error_code
                                ),
                            },
                            None,
                        )
                    )
                    return
                assert attempt.operation == "verify"
                await db.commit()
                stage = "acquire_permits"
                target = uuid.UUID(attempt.scope["target_location_id"])
                ids = {}
                for location in sorted((LEGACY_LOCATION_ID, target)):
                    ids[location] = await storage_access.acquire(
                        db,
                        location,
                        copies.KIND,
                        expected_revision=attempt.scope["maintenance"][str(location)],
                        node_id=f"migration:{os.getpid()}",
                    )
                stage = "attach_permits"
                await copies.acquired(db, job_id, task_id, tuple(ids.values()))
                source_id, target_id = ids[LEGACY_LOCATION_ID].id, ids[target].id
                subject = {
                    "copy_id": str(attempt.copy_id),
                    "attempt_id": str(attempt.id),
                    "operation_id": str(target_id),
                    "task_id": task_id,
                    "phase": "verify",
                    "location_revision": attempt.scope["target"]["location"],
                    "connection_revision": attempt.scope["target"]["connection"],
                    "object_id": row["id"],
                    "obligation": hashlib.sha256(
                        row["target_key"].encode()
                    ).hexdigest(),
                    "version": row["receipt"]["version_id"],
                }
                for scope in ("read", "cleanup"):
                    stage = f"deny_{scope}"
                    client = SavedIOClient(origin, tokens[scope])
                    with pytest.raises(SavedIOError) as denied:
                        with client.download("migration", "copy_get", subject):
                            pytest.fail("non-copy bearer admitted a valid copy proof")
                    assert denied.value.status_code in {401, 403, 404}
                stage = "open_copy_body"
                client = SavedIOClient(origin, tokens["write"])
                with client.download("migration", "copy_get", subject) as transfer:
                    assert transfer.length == len(_BYTES)
                    ready.send((None, (attempt.id, source_id, target_id)))
                    ready.recv()
        finally:
            await engine.dispose()

    with ready:
        try:
            settings.connector_host_allowlist = [urlsplit(endpoint).hostname]
            _configure_inventory_bucket(bucket)
            asyncio.run(start())
        except BaseException as exc:
            try:
                ready.send((_child_failure(stage, exc), None))
            except (OSError, EOFError):
                pass


async def _restart_during_worker_recovery(
    owner, runtime, app_url, provider, restart, copy_id, actor, source_bucket, row
):
    verify = StorageMigrationCopyAttemptRequest(operation="verify")
    recover = StorageMigrationCopyAttemptRequest(operation="recover")
    async with runtime() as db:
        _, job, reused = await copies.attempt(db, copy_id, actor, verify)
        assert not reused
        await db.commit()
        job_id, task_id = job.id, job.celery_task_id

    process_context = multiprocessing.get_context("spawn")
    receipt, owned = process_context.Pipe()
    process = process_context.Process(
        target=_hold_verified_body,
        args=(
            app_url.set(drivername="postgresql+asyncpg"),
            provider.storage_origin,
            source_bucket,
            provider.origin,
            provider.tokens,
            job_id,
            task_id,
            row,
            owned,
        ),
    )
    process.start()
    owned.close()
    paused = False
    original_process = provider.process
    try:
        assert await asyncio.to_thread(receipt.poll, 30), (
            "no real Source response headers"
        )
        try:
            failure, permits = receipt.recv()
        except EOFError:
            await asyncio.to_thread(process.join, 5)
            pytest.fail(
                f"migration fixture child exited before reporting (exit={process.exitcode})"
            )
        assert failure is None, failure
        attempt_id, source_id, target_id = permits
        provider.process.send_signal(signal.SIGSTOP)
        paused = True
        async with owner() as db:
            assert (
                await db.scalar(
                    text("""
                SELECT parent_operation_id FROM public.storage_migration_io_flights
                WHERE operation_id = :permit
            """),
                    {"permit": target_id},
                )
                == source_id
            )
        async with runtime() as db:
            _, existing, reused = await copies.attempt(db, copy_id, actor, verify)
            assert (
                reused and existing.id == job_id and existing.celery_task_id == task_id
            )
            await db.rollback()
        process.kill()
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode is not None
        async with runtime() as db:
            with pytest.raises(HTTPException) as denied:
                await copies.attempt(db, copy_id, actor, recover)
            assert denied.value.detail == {"code": "copy_execution_unsettled"}
            await db.rollback()
        async with owner() as db:
            assert await db.get(StorageOperation, source_id)
            assert await db.get(StorageOperation, target_id)
        provider.process.kill()
        await asyncio.to_thread(provider.process.wait, 10)
        provider = restart()
        async with runtime() as db:
            _, replacement, reused = await copies.attempt(db, copy_id, actor, recover)
            assert not reused and replacement.id != job_id
            replacement_id, replacement_task = (
                replacement.id,
                replacement.celery_task_id,
            )
            await db.commit()
            ended = await copies.by_job(db, job_id)
            assert ended.id == attempt_id and ended.error_code == "copy_process_ended"
            await db.rollback()
            assert await worker.execute(db, replacement_id, replacement_task)
            assert (await copies.by_job(db, replacement_id)).status == "completed"
        async with owner() as db:
            assert await db.get(StorageOperation, source_id) is None
            assert await db.get(StorageOperation, target_id) is None
        return provider
    finally:
        if paused and original_process.poll() is None:
            original_process.send_signal(signal.SIGCONT)
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 10)
        receipt.close()


async def _publish_exact_fixture(owner, copy, row, revisions, actor, output):
    """Seed the immutable route from real verified version/creation facts."""
    publication_id, window_id = uuid.uuid4(), uuid.uuid4()
    async with owner() as db:
        gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
        assert gate.state == "active"
        gate.state = "stopped"
        gate.revision += 1
        source_revision = copy.source["location_revision"]
        db.add(
            StorageMigrationWindow(
                id=window_id,
                source_location_id=LEGACY_LOCATION_ID,
                source=copy.source,
                source_config_revision=source_revision,
                parent_copy_id=copy.id,
                target_location_id=copy.target_location_id,
                target=copy.target,
                state="stopped",
                revision=1,
                source_gate_revision=gate.revision,
                created_by=actor.id,
            )
        )
        await db.flush()
        db.add(
            StorageMigrationFinalization(
                id=publication_id,
                window_id=window_id,
                state="sealed",
                revision=1,
                window_revision=1,
                source_location_id=LEGACY_LOCATION_ID,
                source=copy.source,
                source_gate_revision=gate.revision,
                source_config_revision=source_revision,
                parent_copy_id=copy.id,
                target_location_id=copy.target_location_id,
                target=copy.target,
                claim={},
                native={},
                copy_id=copy.id,
                token={},
                evidence={},
                created_by=actor.id,
                sealed_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()
        db.add(
            StorageMigrationReadPublication(
                id=publication_id,
                window_id=window_id,
                copy_id=copy.id,
                source_location_id=LEGACY_LOCATION_ID,
                target_location_id=copy.target_location_id,
                state="active",
                revision=1,
                finalization_revision=1,
                window_revision=1,
                source=copy.source,
                target=copy.target,
                target_revisions=revisions,
                token={},
                evidence={},
                native={},
                mapping_sha256=copies.sha(row["id"].encode()),
                route_count=1,
                created_by=actor.id,
            )
        )
        await db.flush()
        db.add(
            StorageMigrationReadRoute(
                publication_id=publication_id,
                source_object_id=row["id"],
                endpoint_url=row["source"]["endpoint_url"],
                bucket=row["source"]["bucket"],
                object_key=row["source"]["key"],
                source_version_id=row["source"]["version_id"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                source_etag=row["etag"],
                is_latest=True,
                target_key=row["target_key"],
                target_version_id=row["receipt"]["version_id"],
                target_etag=row["receipt"]["etag"],
                target_content_sha256=row["receipt"]["content_sha256"],
                nonce=row["nonce"],
                creation_plan_uuid=row["target_key"]
                .removeprefix(copy.target["key_prefix"])
                .split("/")[0],
            )
        )
        db.add(output)
        await db.commit()
    return publication_id, window_id


async def _withdraw_fixture(owner, publication_id, window_id):
    """Leave immutable history for teardown of the verifier's owned database."""
    async with owner() as db:
        publication = await db.get(StorageMigrationReadPublication, publication_id)
        window = await db.get(StorageMigrationWindow, window_id)
        gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
        publication.state = "withdrawing"
        publication.revision += 1
        publication.withdrawing_at = datetime.now(timezone.utc)
        await db.flush()
        publication.state = "withdrawn"
        publication.revision += 1
        publication.withdrawn_at = datetime.now(timezone.utc)
        window.state = "closed"
        window.revision += 1
        gate.state = "active"
        gate.revision += 1
        window.source_gate_revision = gate.revision
        await db.commit()


async def _read_and_sign(runtime, provider, output, row, publication_id):
    reader = SavedIOClient(provider.origin, provider.tokens["read"])
    async with runtime() as db:
        route = await artifacts.resolve(
            db, output.storage_ref, row["source"]["key"], row["sha256"]
        )
        assert route is not None and route.publication_id == publication_id
        await db.commit()
        async with await storage_access.acquire(
            db,
            route.target_location_id,
            route.purpose,
            expected_revision=route.maintenance_revision,
        ) as permit:
            await artifacts.bind_routes(db, permit, (route,))
            subject = {
                "publication_id": str(publication_id),
                "publication_revision": route.publication_revision,
                "object_id": route.source_object_id,
                "version": route.target_version_id,
                "operation_id": str(permit.id),
                "location_revision": route.target_location_revision,
                "connection_revision": route.target_connection_revision,
            }

            def ranged():
                for forged in (
                    {"operation_id": str(uuid.uuid4())},
                    {"object_id": "f" * 64},
                    {"version": "crossed-version"},
                    {"key": "caller-supplied"},
                ):
                    _rejected(
                        reader.command, "migration", "retained_head", subject | forged
                    )
                req = subject | {"range": "bytes=1-4", "condition": "match"}
                writer = SavedIOClient(provider.origin, provider.tokens["write"])
                with pytest.raises(SavedIOError) as denied:
                    with writer.download("migration", "retained_get", req):
                        pytest.fail("copy bearer admitted a retained artifact")
                assert denied.value.status_code in {401, 403, 404}
                with reader.download("migration", "retained_get", req) as result:
                    assert result.metadata["VersionId"] == route.target_version_id
                    assert result.metadata["ContentRange"] == f"bytes 1-4/{len(_BYTES)}"
                    assert result.read(4) == _BYTES[1:5]
                    assert not result.read(1)
                with pytest.raises(SavedIOError):
                    with reader.download(
                        "migration",
                        "retained_get",
                        req | {"range": f"bytes=0-{len(_BYTES)}"},
                    ):
                        pytest.fail("unbounded range was admitted")
                _rejected(
                    reader.command,
                    "migration",
                    "retained_sign",
                    subject
                    | {"expires_in": 30, "disposition": None, "content_type": None},
                )

            await asyncio.to_thread(ranged)

        def full_read(client):
            with closing(
                client.get_object(
                    Bucket=route.source_bucket, Key=route.source_object_key
                )["Body"]
            ) as stream:
                return stream.read(len(_BYTES) + 1)

        assert (
            await artifacts.run(db, route, "migration_fixture_full_read", full_read)
            == _BYTES
        )
        ready = await export_outputs.prepare_download(
            db,
            export_outputs.snapshot(output),
            download_name="retained.zip",
        )
        assert ready.route == route

    def signed_get():
        with httpx.Client(
            trust_env=False, timeout=10, follow_redirects=False
        ) as client:
            result = client.get(ready.url)
        assert result.status_code == 200 and result.content == _BYTES
        query = parse_qs(urlsplit(ready.url).query)
        assert query["versionId"] == [route.target_version_id]
        assert 0 < int(query["X-Amz-Expires"][0]) <= 600

    await asyncio.to_thread(signed_get)


async def _operator_probe(
    owner, control, provider, publication_id, window_id, location_id, actor, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from cutover_storage_migration import probe_targets

    monkeypatch.setenv("ASSETS_SOURCE_URL", provider.origin)
    monkeypatch.setenv(
        "ASSETS_SOURCE_SAVED_IO_CUTOVER_TOKEN", provider.tokens["cutover"]
    )
    async with owner() as db:
        location = await db.get(StorageMaintenanceGate, location_id)
        assert location.state == "active"
        raw = await db.get(StorageMigrationReadPublication, publication_id)
        bindings = {
            row.purpose: {"generation": row.generation}
            for row in await db.scalars(select(StorageBinding))
        }
        revisions = raw.target_revisions
        targets = {
            purpose: {
                "location_id": str(location_id),
                "config_revision": revisions["location"],
                "connection_revision": revisions["connection"],
                "key_prefix": managed_s3_target.managed_prefix(
                    raw.target["base_prefix"], purpose=purpose
                ),
            }
            for purpose in PURPOSE_BUCKET_ATTRIBUTES
        }
        context = {
            "id": str(publication_id),
            "window_id": str(window_id),
            "publication_revision": 1,
            "finalization_revision": 1,
            "window_revision": 1,
            "actor_id": str(actor.id),
            "target_location_id": str(location_id),
            "target_gate_revision": location.revision,
            "before_bindings": bindings,
            "targets": targets,
        }
    results = await probe_targets(control, context)
    assert set(results) == set(PURPOSE_BUCKET_ATTRIBUTES) and all(results.values())
    for access in ("read", "write", "cleanup"):
        denied = SavedIOClient(provider.origin, provider.tokens[access])
        error = await asyncio.to_thread(
            _rejected, denied.command, "migration", "probe_cutover_target", {}
        )
        assert error.status_code in {401, 403, 404}


async def test_registered_copy_and_retained_route_use_exact_live_receipts(
    test_engine, tmp_path, monkeypatch
):
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    app_url, source_url, op_url = (
        _url(key, test_engine.url) for key in (_APP, _SOURCE, _OPERATOR)
    )
    assert len({app_url.username, source_url.username, op_url.username}) == 3
    app_engine = create_async_engine(app_url.set(drivername="postgresql+asyncpg"))
    op_engine = create_async_engine(op_url.set(drivername="postgresql+asyncpg"))
    runtime = async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False)
    control = async_sessionmaker(op_engine, expire_on_commit=False, autoflush=False)
    try:
        await _permissions(app_url)
        await _permissions(source_url, source=True)
        async with owner() as db:
            await ensure_legacy_bindings(db)
            before = {
                row.purpose: row.bucket
                for row in await db.scalars(select(StorageBinding))
            }
            await db.commit()
        with (
            registered_sink(test_engine) as source,
            registered_sink(test_engine) as target,
            ExitStack() as restarts,
            real_source(
                test_engine,
                tmp_path,
                s3_credentials=(settings.minio_access_key, settings.minio_secret_key),
            ) as provider,
        ):

            def select_provider(selected):
                monkeypatch.setattr(settings, "assets_source_url", selected.origin)
                for access in ("read", "write", "cleanup"):
                    monkeypatch.setattr(
                        settings,
                        f"assets_source_saved_io_{access}_token",
                        selected.tokens[access],
                    )

            def restart():
                selected = restarts.enter_context(
                    real_source(
                        test_engine,
                        tmp_path,
                        encryption_key=provider.encryption_key,
                        s3_credentials=(
                            settings.minio_access_key,
                            settings.minio_secret_key,
                        ),
                    )
                )
                select_provider(selected)
                return selected

            select_provider(provider)
            monkeypatch.setattr(
                settings, "connector_encryption_key", provider.encryption_key
            )
            monkeypatch.setattr(
                settings,
                "connector_host_allowlist",
                [urlsplit(provider.storage_origin).hostname],
            )
            _configure_inventory_bucket(source.bucket, monkeypatch.setattr)

            project, job, output_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            key = export_storage.key_for("image", project, job, output_id)
            source_put = source.storage.put_object(
                Bucket=source.bucket,
                Key=key,
                Body=_BYTES,
                ContentType="application/zip",
                Metadata={"fixture": "migration-source-retained"},
            )
            now = datetime.now(timezone.utc)
            output = ExportOutput(
                id=output_id,
                project_id=project,
                job_id=job,
                media="image",
                format="fixture.zip",
                file_count=1,
                sha256=hashlib.sha256(_BYTES).hexdigest(),
                size_bytes=len(_BYTES),
                storage_ref={
                    "schema": 1,
                    "purpose": "export",
                    "location_id": str(LEGACY_LOCATION_ID),
                    "config_revision": 1,
                    "binding_generation": 1,
                    "endpoint_url": provider.storage_origin,
                    "bucket": source.bucket,
                    "object_key": key,
                    "storage_identity": managed_storage_identity(
                        provider.storage_origin, source.bucket
                    ),
                },
                state="ready",
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )
            async with owner() as db:
                actor = await create_user(
                    db,
                    "super_admin",
                    f"saved-io-{uuid.uuid4()}@test.local",
                    "Migration",
                )
                checked = StorageConnection(
                    name=f"Migration {target.bucket}",
                    kind="s3",
                    scope="global",
                    config={
                        "endpoint": provider.storage_origin,
                        "bucket": target.bucket,
                        "base_prefix": target.prefix,
                    },
                    secret_enc=encrypt_secret(
                        {
                            "access_key": target.credentials[0],
                            "secret_key": target.credentials[1],
                        }
                    ),
                )
                db.add(checked)
                await db.flush()
                location = await register_connection(db, checked)
                assert (
                    await db.scalar(
                        select(StorageBinding).where(
                            StorageBinding.location_id != LEGACY_LOCATION_ID
                        )
                    )
                    is None
                )
                await db.execute(update(StorageBinding).values(bucket=source.bucket))
                await db.commit()
            publication = window = None
            try:
                handling = dict.fromkeys(PURPOSE_BUCKET_ATTRIBUTES, "migrate")
                async with runtime() as db:
                    inventory, job, reused = await inventories.start(
                        db, actor, StorageMigrationInventoryRequest(handling=handling)
                    )
                    assert not reused
                    inventory_job_id = job.id
                    await db.commit()
                    assert await inventory_worker.execute(
                        db, inventory_job_id, inventory.task_id
                    )
                    sealed = await inventories.by_job(db, inventory_job_id)
                    assert sealed is not None
                    assert sealed.report_sha256 is not None, (
                        sealed.status,
                        sealed.error_code,
                    )
                    assert sealed.status == "completed" and sealed.complete, (
                        sealed.status,
                        sealed.error_code,
                        sealed.counts,
                    )
                    assert sealed.counts["verified_objects"] == 1
                    copy, prep, reused = await copies.start(
                        db,
                        actor,
                        StorageMigrationCopyRequest(
                            inventory_job_id=inventory_job_id,
                            target_location_id=location.id,
                            handling=handling,
                        ),
                    )
                    assert not reused
                    await db.commit()
                    assert await worker.execute(db, prep.id, prep.celery_task_id)
                    assert (await copies.last(db, copy)).status == "completed"
                    await db.rollback()
                    target.storage.put_bucket_versioning(
                        Bucket=target.bucket,
                        VersioningConfiguration={"Status": "Suspended"},
                    )
                    failed_id, failed_task = await _copy_attempt(db, actor, copy.id)
                    assert await worker.execute(db, failed_id, failed_task)
                    failed = await copies.by_job(db, failed_id)
                    assert (
                        failed.status == "failed"
                        and failed.error_code == "target_versioning_required"
                    )
                    assert not target.storage.list_object_versions(
                        Bucket=target.bucket
                    ).get("Versions")
                    await db.rollback()
                    target.storage.put_bucket_versioning(
                        Bucket=target.bucket,
                        VersioningConfiguration={"Status": "Enabled"},
                    )
                    run_id, run_task = await _copy_attempt(db, actor, copy.id)
                    channel = SavedIOClient(provider.origin, provider.tokens["write"])
                    loop = asyncio.get_running_loop()
                    events = []

                    async def claimed():
                        async with runtime() as observed:
                            return await copies.by_job(observed, run_id)

                    def observe(phase, row):
                        if phase not in {"before_put", "after_put"}:
                            return
                        attempt = asyncio.run_coroutine_threadsafe(
                            claimed(), loop
                        ).result()
                        subject = {
                            "copy_id": str(copy.id),
                            "attempt_id": str(attempt.id),
                            "task_id": run_task,
                            "operation_id": str(attempt.target_operation_id),
                            "phase": "run",
                            "location_revision": attempt.scope["target"]["location"],
                            "connection_revision": attempt.scope["target"][
                                "connection"
                            ],
                            "object_id": row["id"],
                            "obligation": hashlib.sha256(
                                row["target_key"].encode()
                            ).hexdigest(),
                            "nonce": row["nonce"],
                        }

                        def put(proof, data):
                            return channel.upload(
                                "migration", "copy_write", proof, data, length=len(data)
                            )

                        if phase == "before_put":
                            for forged in (
                                {"bucket": source.bucket},
                                {"obligation": "f" * 64},
                                {"operation_id": str(attempt.source_operation_id)},
                                {"nonce": "0" * 48},
                            ):
                                _rejected(put, subject | forged, _BYTES)
                            _rejected(put, subject, b"x" * len(_BYTES))
                        else:
                            assert (
                                _rejected(put, subject, _BYTES).code
                                == "copy_write_replayed"
                            )
                        events.append(phase)

                    execute_copy = worker.copy_execute

                    def observed_execute(*args, **kwargs):
                        return execute_copy(*args, **kwargs, event=observe)

                    monkeypatch.setattr(worker, "copy_execute", observed_execute)
                    assert await worker.execute(db, run_id, run_task)
                    assert events == ["before_put", "after_put"]
                    attempt = await copies.by_job(db, run_id)
                    assert attempt.status == "completed"
                async with owner() as db:
                    delta = (
                        await db.scalars(
                            select(StorageMigrationCopyDelta).where(
                                StorageMigrationCopyDelta.copy_id == copy.id
                            )
                        )
                    ).one()
                    row = delta.data
                assert row["state"] == "verified" and row["source"]["key"] == key
                assert (
                    _object(
                        target.storage,
                        target.bucket,
                        row["target_key"],
                        row["receipt"]["version_id"],
                    )
                    == _BYTES
                )
                assert (
                    len(
                        target.storage.list_object_versions(Bucket=target.bucket).get(
                            "Versions", []
                        )
                    )
                    == 1
                )
                provider = await _restart_during_worker_recovery(
                    owner,
                    runtime,
                    app_url,
                    provider,
                    restart,
                    copy.id,
                    actor,
                    source.bucket,
                    row,
                )
                publication, window = await _publish_exact_fixture(
                    owner, copy, row, attempt.scope["target"], actor, output
                )
                await _read_and_sign(runtime, provider, output, row, publication)
                await _operator_probe(
                    owner,
                    control,
                    provider,
                    publication,
                    window,
                    location.id,
                    actor,
                    monkeypatch,
                )
                assert (
                    _object(source.storage, source.bucket, key, source_put["VersionId"])
                    == _BYTES
                )
                assert not source.storage.list_object_versions(
                    Bucket=source.bucket
                ).get("DeleteMarkers")
            finally:
                try:
                    if publication is not None:
                        await _withdraw_fixture(owner, publication, window)
                finally:
                    async with owner() as db:
                        for purpose, bucket in before.items():
                            await db.execute(
                                update(StorageBinding)
                                .where(StorageBinding.purpose == purpose)
                                .values(bucket=bucket)
                            )
                        await db.commit()
    finally:
        await app_engine.dispose()
        await op_engine.dispose()
