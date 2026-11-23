"""Real Source HTTP, restricted PostgreSQL LOGIN and disposable attached MinIO."""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import os
from uuid import uuid4
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

import boto3
from botocore.config import Config
import httpx
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yoao_assets_source.saved_io import SavedIOError

from app.core.crypto import encrypt_secret
from app.db.models.async_job import AsyncJob
from app.config import settings
from app.db.models.storage_connection import StorageConnection
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.storage_location import TrainingBindingChange
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingResourceLease,
    TrainingRun,
)
from app.schemas.training_run import (
    ExecutorFinish,
    ExecutorUpload,
    TrainingArtifactSpec,
)
from app.services import (
    storage_access,
    training_binding_change,
    training_dataset_data as datasets,
    training_dataset_originals as originals,
    training_dataset_storage as owners,
    training_storage_cleanup,
    training_storage_io as io,
    training_run_storage,
    training_runs,
)
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.saved_io import saved_io_client
from app.services.storage_location import register_connection
from app.services.training_datasets import SqlTrainingAuthority
from app.workers.training_dataset import _upload_output, file_digest
from tests.factory import create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_TRAINING_SAVED_IO_LIVE") != "1"
        or os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires an isolated Source process, disposable PostgreSQL and versioned MinIO",
    ),
]


@asynccontextmanager
async def _attached_bucket():
    endpoint = os.environ["AAP_REFERENCE_IO_S3_ENDPOINT"]
    access = os.environ["AAP_REFERENCE_IO_S3_ACCESS_KEY"]
    secret = os.environ["AAP_REFERENCE_IO_S3_SECRET_KEY"]
    bucket = f"m2b-training-{uuid4().hex}"
    provider = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )
    await asyncio.to_thread(provider.create_bucket, Bucket=bucket)
    try:
        await asyncio.to_thread(
            provider.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        yield provider, bucket, endpoint, {"access_key": access, "secret_key": secret}
    finally:

        def remove_fixture():
            while True:
                page = provider.list_object_versions(Bucket=bucket)
                entries = page.get("Versions", []) + page.get("DeleteMarkers", [])
                if not entries:
                    break
                reply = provider.delete_objects(
                    Bucket=bucket,
                    Delete={
                        "Objects": [
                            {"Key": item["Key"], "VersionId": item["VersionId"]}
                            for item in entries
                        ]
                    },
                )
                assert not reply.get("Errors")
            provider.delete_bucket(Bucket=bucket)
            provider.close()

        await asyncio.to_thread(remove_fixture)


@pytest.fixture
async def training_source(monkeypatch):
    from tests.saved_io_live_fixture import real_saved_io_source

    host = urlsplit(os.environ["AAP_REFERENCE_IO_S3_ENDPOINT"]).hostname
    encryption_key = Fernet.generate_key().decode()
    async with real_saved_io_source(
        allowlist=[host], encryption_key=encryption_key
    ) as (origin, tokens):
        monkeypatch.setattr(settings, "connector_encryption_key", encryption_key)
        monkeypatch.setattr(settings, "assets_source_url", origin)
        for scope, token in tokens.items():
            name = f"assets_source_saved_io_{scope}_token"
            monkeypatch.setattr(settings, name, token)
            monkeypatch.setattr(settings, f"{name}_file", "")
        yield


@pytest.fixture
async def embedded_db(test_engine):
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"])
    assert url.username == "identity_embedded_api_consumer"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            assert await db.scalar(text("SELECT session_user")) == url.username
            yield db
    finally:
        await engine.dispose()


async def _register(db, actor, bucket, endpoint, keys, base):
    connection = StorageConnection(
        id=uuid4(),
        name=f"registered training {uuid4().hex}",
        kind="s3",
        scope="global",
        config={
            "endpoint": endpoint,
            "bucket": bucket,
            "base_prefix": base,
            "use_ssl": endpoint.startswith("https:"),
        },
        secret_enc=encrypt_secret(keys),
        created_by=actor.id,
    )
    db.add(connection)
    await db.flush()
    return await register_connection(db, connection)


async def _assert_denied_app_privileges(db):
    for sql in (
        "SELECT secret_enc FROM public.storage_connections LIMIT 0",
        "SELECT * FROM public.training_saved_objects LIMIT 0",
        "SELECT yoao_assets_v1.capture_training_saved_io(NULL::uuid,NULL::text,NULL::uuid,NULL::uuid,NULL::text,NULL::text,NULL::jsonb)",
    ):
        with pytest.raises(DBAPIError) as error:
            async with db.begin_nested():
                await db.execute(text(sql))
        assert error.value.orig.sqlstate == "42501"


async def _head_denied(client, proof):
    with pytest.raises(SavedIOError) as denied:
        await asyncio.to_thread(client.command, "training", "head", proof)
    assert denied.value.status_code == 409
    assert denied.value.storage_code is None


async def test_training_attached_archive_publication_and_retirement(
    test_engine, embedded_db, tmp_path, training_source
):
    seed = async_sessionmaker(test_engine, expire_on_commit=False)
    async with _attached_bucket() as (s3, bucket, endpoint, keys):
        async with seed.begin() as db:
            await ensure_legacy_bindings(db)
            actor = await create_user(
                db,
                "super_admin",
                f"training-{uuid4().hex}@example.test",
                "Training admin",
            )
            project = await create_project(db, owner_id=actor.id)
            location = await _register(
                db, actor, bucket, endpoint, keys, "training-world"
            )
            other = await _register(
                db, actor, bucket, endpoint, keys, "counterfeit-world"
            )
            generation = await db.scalar(
                text(
                    "SELECT generation FROM public.storage_bindings WHERE purpose='training'"
                )
            )

        db = embedded_db
        reviewed = await training_binding_change.preview(
            db,
            TrainingBindingChange(
                expected_generation=generation,
                target_location_id=location.id,
                target_config_revision=location.config_revision,
            ),
            actor,
        )
        assert reviewed["capabilities"] == {
            "write": True,
            "multipart": True,
            "full_read": True,
            "list": True,
            "delete": True,
        }
        await db.rollback()
        await _assert_denied_app_privileges(db)
        await db.rollback()

        version_id, attempt, job_id = uuid4(), uuid4(), uuid4()
        snapshot_digest = sha256(b"frozen training fixture").hexdigest()
        target = await owners.target_for(db, location)
        ref = owners.make_ref(
            target, max(generation + 1, 2), owners.prefix_for(project.id, version_id)
        )
        owner = owners.resolve_ref(ref)
        await db.rollback()
        async with seed.begin() as db_seed:
            db_seed.add(
                AsyncJob(
                    id=job_id,
                    project_id=project.id,
                    user_id=actor.id,
                    kind="training_dataset",
                    status="running",
                    payload={"training_dataset_id": str(version_id)},
                )
            )
            await db_seed.flush()
            db_seed.add(
                TrainingDatasetVersion(
                    id=version_id,
                    project_id=project.id,
                    creator_id=actor.id,
                    async_job_id=job_id,
                    request_id=uuid4(),
                    request_digest=snapshot_digest,
                    name="Frozen archive",
                    status="running",
                    snapshot={"samples": [], "classes": [], "task": "detect"},
                    snapshot_digest=snapshot_digest,
                    attempt_id=attempt,
                    storage_ref=ref,
                )
            )

        path = tmp_path / "archive.zip"
        with ZipFile(path, "w", compression=ZIP_STORED) as archive:
            archive.writestr("manifest.json", '{"contract": "frozen"}')
            with archive.open("images/archive-fixture", "w") as image:
                for _ in range(9):
                    image.write(b"a" * 1024**2)
        digest, size = file_digest(path), path.stat().st_size
        key = owner.key(f"attempts/{attempt}/dataset.zip")

        def upload(bound):
            def local_upload():
                pytest.fail("registered upload must not open the ordinary client")

            _upload_output(
                bound,
                bucket,
                key,
                digest,
                size,
                local_upload,
                path=path,
                content_type="application/zip",
            )

        await io.run(
            db,
            owner,
            originals.output_purpose(version_id),
            upload,
            source_attempt=attempt,
        )
        actual = await asyncio.to_thread(s3.head_object, Bucket=bucket, Key=key)
        saved_version = actual["VersionId"]
        assert saved_version != "null" and actual["ContentLength"] == size
        assert "-" in actual["ETag"]
        version = await datasets.read_version(db, version_id)
        assert await datasets.publish_version(
            db,
            version,
            manifest={"source_digest": snapshot_digest, "samples": []},
            package_key=key,
            package_sha256=digest,
            size_bytes=size,
        )
        await db.commit()

        await asyncio.to_thread(
            s3.put_object, Bucket=bucket, Key=key, Body=b"wrong current bytes"
        )
        foreign_key = f"counterfeit-world/_aap_managed/training/{owner.logical_prefix}attempts/{attempt}/dataset.zip"
        await asyncio.to_thread(
            s3.put_object, Bucket=bucket, Key=foreign_key, Body=b"do not retire"
        )

        def reread(bound):
            response = io.get_object(bound, bucket, key)
            assert response["VersionId"] == saved_version
            result = sha256()
            with storage_access.closing_registered(response["Body"]) as body:
                for block in iter(lambda: body.read(1024 * 1024), b""):
                    result.update(block)
            return result.hexdigest()

        assert (
            await io.read(
                db,
                owner,
                "training_dataset_download",
                reread,
                object_key=key,
                sha256=digest,
                size_bytes=size,
            )
            == digest
        )
        signed = await io.read(
            db,
            owner,
            "training_dataset_download",
            lambda transport: io.sign_download(
                transport, key, bucket=bucket, expires_in=60
            ),
            object_key=key,
            sha256=digest,
            size_bytes=size,
        )
        async with httpx.AsyncClient(trust_env=False) as web:
            async with web.stream("GET", signed) as reply:
                assert reply.status_code == 200
                signed_digest = sha256()
                async for block in reply.aiter_bytes():
                    signed_digest.update(block)
                assert signed_digest.hexdigest() == digest

        reader = saved_io_client("read")
        proof = {
            "scope": "dataset",
            "owner": str(version_id),
            "attempt": str(attempt),
            "member": "archive",
            "evidence": {},
        }
        counterfeit = await storage_access.acquire(
            db, other.id, "training_dataset_download"
        )
        async with counterfeit:
            await _head_denied(reader, {**proof, "permit": str(counterfeit.id)})

        permit = await storage_access.acquire(
            db, location.id, "training_dataset_download"
        )
        async with permit:
            request = {**proof, "permit": str(permit.id)}
            assert (
                await asyncio.to_thread(reader.command, "training", "head", request)
            )["VersionId"] == saved_version
            await _head_denied(reader, {**request, "attempt": str(uuid4())})
            async with seed.begin() as db_seed:
                await db_seed.execute(
                    text(
                        "UPDATE public.storage_maintenance_gates SET revision=revision+1 WHERE location_id=:id"
                    ),
                    {"id": location.id},
                )
            await _head_denied(reader, request)

        version = await datasets.read_version(db, version_id)
        intent = await training_storage_cleanup.stage_deletion(
            db, version, owner.logical_prefix
        )
        await datasets.delete_version(db, version)
        await db.commit()
        assert await training_storage_cleanup.purge_requested(db, intent)
        assert not await training_storage_cleanup.purge_requested(db, intent)
        seen = await asyncio.to_thread(
            s3.list_object_versions, Bucket=bucket, Prefix=owner.prefix
        )
        assert not seen.get("Versions") and not seen.get("DeleteMarkers")
        intact = await asyncio.to_thread(s3.head_object, Bucket=bucket, Key=foreign_key)
        assert intact["ContentLength"] == len(b"do not retire")


async def test_training_attached_executor_put_and_finish(
    test_engine, embedded_db, training_source
):
    seed = async_sessionmaker(test_engine, expire_on_commit=False)
    run_id, attempt_id, dataset_id, job_id, boot_id = (uuid4() for _ in range(5))
    executor_id = f"training-executor-{uuid4().hex}"
    token = uuid4().hex
    resource = f"gpu:{uuid4()}"
    digest = sha256(b"frozen fixture").hexdigest()
    async with _attached_bucket() as (s3, bucket, endpoint, keys):
        async with seed.begin() as db:
            actor = await create_user(
                db, "project_admin", f"executor-{uuid4().hex}@example.test", "Owner"
            )
            project = await create_project(db, owner_id=actor.id)
            location = await _register(
                db, actor, bucket, endpoint, keys, "executor-world"
            )
        db = embedded_db
        target = await owners.target_for(db, location)
        owner = training_run_storage.resolve_ref(
            owners.make_ref(
                target,
                2,
                training_run_storage.prefix_for(project.id, run_id),
                purpose="training-run",
            )
        )
        await db.rollback()
        async with seed.begin() as db_seed:
            db_seed.add(
                TrainingDatasetVersion(
                    id=dataset_id,
                    project_id=project.id,
                    creator_id=actor.id,
                    request_id=uuid4(),
                    request_digest=digest,
                    name="Training inputs",
                    status="failed",
                    snapshot={"classes": [], "samples": [], "task": "detect"},
                    snapshot_digest=digest,
                )
            )
            db_seed.add(
                AsyncJob(
                    id=job_id,
                    project_id=project.id,
                    user_id=actor.id,
                    kind="training_run",
                    status="running",
                    payload={"training_run_id": str(run_id)},
                )
            )
            await db_seed.flush()
            db_seed.add(
                TrainingRun(
                    id=run_id,
                    project_id=project.id,
                    creator_id=actor.id,
                    dataset_id=dataset_id,
                    async_job_id=job_id,
                    request_id=uuid4(),
                    request_digest=digest,
                    name="Interrupted training",
                    status="uploading",
                    config={"resource_id": resource},
                    config_digest=digest,
                    current_attempt_id=attempt_id,
                    storage_ref=owner.ref,
                )
            )
            await db_seed.flush()
            db_seed.add(
                TrainingAttempt(
                    id=attempt_id,
                    run_id=run_id,
                    ordinal=1,
                    status="uploading",
                    executor_id=executor_id,
                    boot_id=boot_id,
                    claim_id=uuid4(),
                    lease_digest=sha256(token.encode()).hexdigest(),
                    resource_id=resource,
                )
            )
            await db_seed.flush()
            db_seed.add(
                TrainingResourceLease(
                    resource_id=resource,
                    attempt_id=attempt_id,
                    executor_id=executor_id,
                    boot_id=boot_id,
                )
            )

        raw = b"completed checkpoint from interrupted training attempt"
        spec = TrainingArtifactSpec(
            name="last.pt",
            role="checkpoint",
            sha256=sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )
        fence = {"executor_id": executor_id, "boot_id": boot_id, "lease_token": token}
        grants = await training_runs.authorize_upload(
            db, attempt_id, ExecutorUpload(artifacts=[spec], **fence)
        )
        grant = grants[spec.name]
        assert grant["headers"]["If-None-Match"] == "*"
        async with httpx.AsyncClient(trust_env=False) as web:
            response = await web.put(
                grant["url"], content=raw, headers=grant["headers"]
            )
            assert response.status_code == 200
            duplicate = await web.put(
                grant["url"], content=raw, headers=grant["headers"]
            )
            assert duplicate.status_code == 412
        finished = await training_runs.finish(
            db,
            attempt_id,
            ExecutorFinish(status="failed", process_exited=True, **fence),
            training_authority=SqlTrainingAuthority(db),
        )
        await db.commit()
        assert finished.status == "failed"
        key = owner.key(f"attempts/{attempt_id}/{spec.name}")
        assert finished.artifacts[spec.name]["key"] == key
        physical = await asyncio.to_thread(s3.head_object, Bucket=bucket, Key=key)
        assert physical["ContentLength"] == len(raw) and physical["VersionId"] != "null"
        async with seed() as db_seed:
            version_id = await db_seed.scalar(
                text(
                    "SELECT version_id FROM public.training_saved_objects WHERE location_id=:location AND object_key=:key"
                ),
                {"location": location.id, "key": key},
            )
            assert version_id == physical["VersionId"]
