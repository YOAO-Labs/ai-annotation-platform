"""Independent export ownership, verified cache publication and retirement races."""

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import threading
import uuid

from botocore.exceptions import ClientError
from botocore.config import Config
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import export_outputs, export_storage, storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.exporting import cache
from tests.factory import create_project
from tests.test_lidar_export_inputs import input_db as input_db
from tests.test_lidar_export_worker import worker_db as worker_db


class ExportObjects:
    """Real physical helpers operate on independent stored bytes in these tests."""

    def __init__(self):
        scheme = "https" if settings.minio_use_ssl else "http"
        self.client = SimpleNamespace(
            meta=SimpleNamespace(endpoint_url=f"{scheme}://{settings.minio_endpoint}"),
            head_object=self.head,
            get_object=self.get,
        )
        self.export_bucket = settings.minio_export_bucket
        self.objects, self.reads, self.puts, self.deleted, self.bodies = (
            {},
            [],
            [],
            [],
            [],
        )
        self.signatures = []
        self.fail_put = self.fail_after_put = self.fail_delete = False
        self.body_factory = io.BytesIO

    def seed(self, bucket, key, data):
        self.objects[bucket, key] = data

    def head(self, *, Bucket, Key):  # noqa: N803
        try:
            data = self.objects[Bucket, Key]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject") from None
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get(self, *, Bucket, Key, IfMatch):  # noqa: N803
        self.reads.append((Bucket, Key))
        metadata = self.head(Bucket=Bucket, Key=Key)
        if metadata["ETag"] != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        body = self.body_factory(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def upload_file(self, path, key, *, bucket, content_type):
        assert content_type == "application/zip"
        if self.fail_put:
            raise OSError("private storage credential")
        self.seed(bucket, key, Path(path).read_bytes())
        self.puts.append((bucket, key))
        if self.fail_after_put:
            raise OSError("private unknown PUT reply")

    def delete_object(self, key, *, bucket):
        if self.fail_delete:
            raise OSError("private DELETE failure")
        self.objects.pop((bucket, key), None)
        self.deleted.append((bucket, key))

    def generate_download_url(self, key, **kwargs):
        self.signatures.append((key, kwargs))
        return "https://download.invalid/verified.zip"


@pytest.fixture
def export_objects(monkeypatch):
    objects = ExportObjects()
    monkeypatch.setattr(export_storage, "storage_service", objects)
    from app.services import storage as module

    objects.client.meta.config = Config(retries={"max_attempts": 3})

    def owned(**kwargs):
        assert kwargs["client_config"] is objects.client.meta.config
        return SimpleNamespace(
            client=SimpleNamespace(
                head_object=objects.head,
                get_object=objects.get,
                delete_object=lambda **params: objects.delete_object(
                    params["Key"], bucket=params["Bucket"]
                ),
                upload_file=lambda path, bucket, key, ExtraArgs: objects.upload_file(
                    path, key, bucket=bucket, content_type=ExtraArgs["ContentType"]
                ),
                close=lambda: None,
            )
        )

    monkeypatch.setattr(module, "StorageService", owned)
    return objects


@pytest.fixture
async def export_project(db_session, annotator):
    return await create_project(db_session, owner_id=annotator[0].id)


async def seed_output(
    db,
    objects,
    *,
    project_id=None,
    job_id=None,
    state="ready",
    expired=False,
    data=b"independently owned export bytes",
    stored=True,
):
    created = datetime.now(timezone.utc) - timedelta(days=8 if expired else 1)
    row = ExportOutput(
        id=uuid.uuid4(),
        project_id=project_id or uuid.uuid4(),
        job_id=job_id or uuid.uuid4(),
        media="image",
        format="coco",
        file_count=2,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        state=state,
        created_at=created,
        expires_at=created + timedelta(days=7),
        retired_at=datetime.now(timezone.utc) if state == "retired" else None,
    )
    row.storage_ref = await export_storage.allocate(db, row)
    db.add(row)
    await db.flush()
    if stored:
        objects.seed(row.storage_ref["bucket"], row.storage_ref["object_key"], data)
    return row


async def seed_index(db, row, *, cache_key=None, output_id=True, **changes):
    values = dict(
        id=uuid.uuid4(),
        project_id=row.project_id,
        batch_id=None,
        cache_key=cache_key or uuid.uuid4().hex,
        format=row.format,
        object_key=row.storage_ref["object_key"],
        file_count=row.file_count,
        size_bytes=row.size_bytes,
        expires_at=row.expires_at,
        output_id=row.id if output_id else None,
    )
    values.update(changes)
    artifact = ExportArtifact(**values)
    db.add(artifact)
    await db.flush()
    return artifact


async def test_create_commits_receipt_and_never_reuses_a_job_key(
    db_session,
    export_objects,
    tmp_path,
):
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"zip receipt")
    args = dict(
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media="image",
        format="coco",
        file_count=1,
        path=str(path),
        expected_size=11,
    )
    first = await export_outputs.create(db_session, **args)
    second = await export_outputs.create(db_session, **args)
    assert first.id != second.id
    assert export_outputs.object_key(first) != export_outputs.object_key(second)
    assert first.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.expires_at == first.created_at + timedelta(days=7)
    assert not export_objects.puts
    assert (await db_session.get(ExportOutput, first.id)).state == "pending"


@pytest.mark.parametrize("after_put", [False, True])
async def test_unknown_put_retains_pending_bytes_and_cleanup_never_reaps_them(
    after_put,
    db_session,
    export_objects,
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"zip")
    receipt = await export_outputs.create(
        db_session,
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media="image",
        format="coco",
        file_count=1,
        path=str(path),
    )
    export_objects.fail_put, export_objects.fail_after_put = not after_put, after_put
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await export_outputs.write(db_session, receipt, str(path))
    assert "private" not in str(caught.value)
    await db_session.rollback()
    assert (await db_session.get(ExportOutput, receipt.id)).state == "pending"
    monkeypatch.setattr(
        export_outputs, "now", lambda: receipt.expires_at + timedelta(days=1)
    )
    assert (await export_outputs.cleanup(db_session))["outputs_deleted"] == 0
    assert not export_objects.deleted
    assert bool(export_objects.objects) is after_put


async def test_publish_joins_result_transaction_and_rollback_preserves_pending(
    db_session,
    export_objects,
    tmp_path,
):
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"zip")
    receipt = await export_outputs.create(
        db_session,
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media="image",
        format="coco",
        file_count=1,
        path=str(path),
    )
    revision = await export_outputs.write(db_session, receipt, str(path))
    row = await export_outputs.publish(db_session, receipt, revision)
    assert row.state == "ready"
    await db_session.rollback()
    assert (
        await db_session.get(ExportOutput, receipt.id, populate_existing=True)
    ).state == "pending"
    assert export_objects.objects and not export_objects.deleted
    assert all(body.closed for body in export_objects.bodies)


@pytest.mark.parametrize("change", ["receipt", "gate"])
async def test_final_publication_rechecks_receipt_and_gate_revision(
    change,
    db_session,
    export_objects,
    tmp_path,
):
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"zip")
    receipt = await export_outputs.create(
        db_session,
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media="image",
        format="coco",
        file_count=1,
        path=str(path),
    )
    revision = await export_outputs.write(db_session, receipt, str(path))
    if change == "receipt":
        receipt = replace(receipt, sha256="f" * 64)
    else:
        gate = await db_session.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
        gate.revision += 1
        await db_session.commit()
    with pytest.raises((export_storage.ExportStorageError, ValueError)):
        await export_outputs.publish(db_session, receipt, revision)
    await db_session.rollback()
    assert (await db_session.get(ExportOutput, receipt.id)).state == "pending"
    assert not export_objects.deleted


async def test_legacy_null_index_is_zero_io_miss_and_only_db_expiry(
    db_session,
    export_project,
    export_objects,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row, output_id=False)
    assert await cache.lookup(db_session, artifact.cache_key) is None
    assert not export_objects.reads
    artifact.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    identifier = artifact.id
    await db_session.commit()
    result = await export_outputs.cleanup(db_session)
    assert result["removed"] == 1
    assert await db_session.get(ExportArtifact, identifier) is None
    assert not export_objects.deleted and export_objects.objects


async def test_cache_saved_bucket_full_receipt_and_remaining_expiry(
    db_session,
    export_project,
    export_objects,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    saved_bucket, saved_key = row.storage_ref["bucket"], row.storage_ref["object_key"]
    export_objects.export_bucket = "new-export-bucket"
    export_objects.seed(
        export_objects.export_bucket, saved_key, b"wrong-current-bucket"
    )
    hit = await cache.lookup(db_session, artifact.cache_key)
    assert hit.output.id == row.id
    assert export_objects.reads == [(saved_bucket, saved_key)]
    await export_outputs.sign_cached(
        db_session,
        hit,
        project_id=export_project.id,
        download_name="export.zip",
    )
    _, signed = export_objects.signatures[-1]
    assert signed["bucket"] == saved_bucket and signed["align"] is False
    assert (
        0
        < signed["expires_in"]
        <= int((row.expires_at - datetime.now(timezone.utc)).total_seconds()) + 1
    )
    assert signed["expires_in"] < 7 * 24 * 3600
    assert all(body.closed for body in export_objects.bodies)


@pytest.mark.parametrize("change", ["missing", "same_size_corrupt"])
async def test_known_missing_or_corrupt_cache_drops_only_index(
    change,
    db_session,
    export_project,
    export_objects,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    identifier, key = (
        artifact.id,
        (row.storage_ref["bucket"], row.storage_ref["object_key"]),
    )
    if change == "missing":
        export_objects.objects.pop(key)
    else:
        export_objects.objects[key] = b"x" * row.size_bytes
    assert await cache.lookup(db_session, artifact.cache_key) is None
    assert await db_session.get(ExportArtifact, identifier) is None
    assert (await db_session.get(ExportOutput, row.id)).state == "ready"
    assert not export_objects.deleted
    assert (key in export_objects.objects) is (change != "missing")


@pytest.mark.parametrize(
    "field",
    ["project_id", "object_key", "size_bytes", "file_count", "format", "expires_at"],
)
async def test_index_receipt_mismatch_is_refused_before_io(
    field,
    db_session,
    export_project,
    export_objects,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    changed = {
        "project_id": uuid.uuid4(),
        "object_key": "different.zip",
        "size_bytes": 1,
        "file_count": 100,
        "format": "yolo",
        "expires_at": row.expires_at - timedelta(seconds=1),
    }
    setattr(artifact, field, changed[field])
    with db_session.no_autoflush:
        with pytest.raises(export_storage.ExportStorageError):
            await export_outputs.verify_cache(db_session, artifact)
    assert not export_objects.reads and not export_objects.deleted


async def test_endpoint_change_refuses_cache_without_fallback_or_index_deletion(
    db_session,
    export_project,
    export_objects,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    export_objects.client.meta.endpoint_url = "http://different.invalid:9000"
    with pytest.raises(export_storage.ExportStorageError):
        await cache.lookup(db_session, artifact.cache_key)
    assert await db_session.get(ExportArtifact, artifact.id) is artifact
    assert not export_objects.reads and not export_objects.deleted


@pytest.mark.parametrize("mutation", ["index", "project", "expiry", "gate"])
async def test_cache_signing_refuses_changes_after_verification(
    mutation,
    db_session,
    export_project,
    export_objects,
    monkeypatch,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    hit = await cache.lookup(db_session, artifact.cache_key)
    project_id = export_project.id
    if mutation == "index":
        artifact.object_key = "different.zip"
        await db_session.flush()
    elif mutation == "project":
        project_id = uuid.uuid4()
    elif mutation == "expiry":
        monkeypatch.setattr(export_outputs, "now", lambda: row.expires_at)
    else:
        gate = await db_session.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
        gate.revision += 1
        await db_session.commit()
    with pytest.raises(ValueError):
        await export_outputs.sign_cached(
            db_session, hit, project_id=project_id, download_name="export.zip"
        )
    assert not export_objects.signatures


@pytest.mark.parametrize("old_kind", ["expired", "legacy_null", "live"])
async def test_cache_record_replaces_only_stale_indices_without_unique_conflict(
    old_kind,
    db_session,
    export_project,
    export_objects,
):
    old = await seed_output(
        db_session,
        export_objects,
        project_id=export_project.id,
        expired=old_kind == "expired",
    )
    artifact = await seed_index(
        db_session, old, cache_key="same-key", output_id=old_kind != "legacy_null"
    )
    new = await seed_output(db_session, export_objects, project_id=export_project.id)
    result = await cache.record(
        db_session, cache_key="same-key", output=new, batch_id=None
    )
    await db_session.commit()
    assert result.id == artifact.id
    assert result.output_id == (old.id if old_kind == "live" else new.id)
    assert await db_session.get(ExportOutput, old.id) is not None
    assert not export_objects.deleted


async def test_read_receipt_blocks_retirement_and_retired_state_blocks_new_reads(
    db_session,
    export_project,
    export_objects,
    monkeypatch,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    await db_session.commit()
    permit = await storage_access.acquire(
        db_session, LEGACY_LOCATION_ID, export_outputs.read_purpose(row.id)
    )
    monkeypatch.setattr(
        export_outputs, "now", lambda: row.expires_at + timedelta(seconds=1)
    )
    assert (await export_outputs.cleanup(db_session))["outputs_deleted"] == 0
    assert (await db_session.get(ExportOutput, row.id)).state == "ready"
    assert await db_session.get(ExportArtifact, artifact.id) is not None
    assert not export_objects.deleted
    await permit.__aexit__(None, None, None)
    assert (await export_outputs.cleanup(db_session))["outputs_deleted"] == 1
    assert await db_session.get(ExportOutput, row.id) is None
    assert export_objects.deleted


async def test_retirement_commit_precedes_delete_and_failed_delete_retains_responsibility(
    db_session,
    export_project,
    export_objects,
):
    row = await seed_output(
        db_session, export_objects, project_id=export_project.id, expired=True
    )
    artifact = await seed_index(db_session, row)
    identifier, artifact_id = row.id, artifact.id
    export_objects.fail_delete = True
    result = await export_outputs.cleanup(db_session)
    assert result["failed"] == 1
    row = await db_session.get(ExportOutput, identifier, populate_existing=True)
    assert row.state == "retired" and row.retired_at is not None
    assert await db_session.get(ExportArtifact, artifact_id) is None
    export_objects.fail_delete = False
    assert (await export_outputs.cleanup(db_session))["outputs_deleted"] == 1
    assert await db_session.get(ExportOutput, identifier) is None


async def test_cleanup_continues_past_bad_owner_and_uses_exact_saved_bucket(
    db_session,
    export_objects,
):
    bad = await seed_output(db_session, export_objects, expired=True)
    good = await seed_output(db_session, export_objects, expired=True)
    saved = (good.storage_ref["bucket"], good.storage_ref["object_key"])
    # The malformed runtime identity is isolated to this snapshot, without
    # inventing or migrating ownership of any existing object.
    original = export_storage.resolve
    bad_id = bad.id

    def resolve(row):
        if row.id == bad_id:
            raise export_storage.ExportStorageError("export_storage_identity_changed")
        return original(row)

    export_objects.export_bucket = "new-current-bucket"
    export_objects.seed(export_objects.export_bucket, saved[1], b"unrelated")
    from unittest.mock import patch

    with patch.object(export_storage, "resolve", resolve):
        result = await export_outputs.cleanup(db_session)
    assert result["failed"] == 1 and result["outputs_deleted"] == 1
    assert await db_session.get(ExportOutput, bad_id) is not None
    assert export_objects.deleted == [saved]
    assert (export_objects.export_bucket, saved[1]) in export_objects.objects


async def test_project_and_job_removal_do_not_discard_output_deadline(
    db_session,
    export_project,
    annotator,
    export_objects,
):
    job = AsyncJob(
        kind="export",
        project_id=export_project.id,
        user_id=annotator[0].id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(job)
    await db_session.flush()
    row = await seed_output(
        db_session,
        export_objects,
        project_id=export_project.id,
        job_id=job.id,
        expired=True,
    )
    identifier, project_id, job_id, deadline = (
        row.id,
        row.project_id,
        row.job_id,
        row.expires_at,
    )
    await db_session.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
    await db_session.execute(delete(Project).where(Project.id == export_project.id))
    await db_session.commit()
    row = await db_session.get(ExportOutput, identifier, populate_existing=True)
    assert (row.project_id, row.job_id, row.expires_at) == (
        project_id,
        job_id,
        deadline,
    )
    assert (await export_outputs.cleanup(db_session))["outputs_deleted"] == 1


async def test_unknown_body_close_retains_read_receipt_and_defers_cleanup(
    db_session,
    export_project,
    export_objects,
    monkeypatch,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    await db_session.commit()

    class BrokenClose(io.BytesIO):
        def close(self):
            raise OSError("unknown body close")

    export_objects.body_factory = BrokenClose
    identifier, deadline = row.id, row.expires_at
    try:
        with pytest.raises(export_storage.ExportStorageError) as caught:
            await cache.lookup(db_session, artifact.cache_key)
        assert caught.value.storage_operation_unsettled
        await db_session.rollback()
        purpose = export_outputs.read_purpose(identifier)
        assert await db_session.scalar(
            select(StorageOperation.id).where(StorageOperation.purpose == purpose)
        )
        monkeypatch.setattr(
            export_outputs, "now", lambda: deadline + timedelta(seconds=1)
        )
        assert (await export_outputs.cleanup(db_session))["outputs_deleted"] == 0
        assert not export_objects.deleted
    finally:
        # Test-only evidence resolution: close real in-memory bodies and remove
        # this test's independently committed uncertain read permit.
        for body in export_objects.bodies:
            io.BytesIO.close(body)
        async with storage_access.engine(db_session).begin() as connection:
            await connection.execute(
                delete(StorageOperation).where(
                    StorageOperation.purpose == export_outputs.read_purpose(identifier)
                )
            )


async def test_hashing_drains_repeated_cancellation_before_zip_can_be_removed(
    db_session,
    export_objects,
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"zip")
    started, release = threading.Event(), threading.Event()

    def receipt(pathname):
        started.set()
        release.wait(5)
        assert Path(pathname).exists()
        return hashlib.sha256(b"zip").hexdigest(), 3

    monkeypatch.setattr(export_storage, "file_receipt", receipt)
    task = asyncio.create_task(
        export_outputs.create(
            db_session,
            project_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            media="image",
            format="coco",
            file_count=1,
            path=str(path),
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and path.exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not export_objects.puts


async def test_cleanup_winning_before_read_admission_refuses_the_physical_get(
    db_session,
    export_project,
    export_objects,
    monkeypatch,
):
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    artifact = await seed_index(db_session, row)
    deadline = row.expires_at
    original = storage_access.run

    async def raced(db, location, purpose, call, *args, **kwargs):
        if purpose.startswith("export_read_"):
            monkeypatch.setattr(
                export_outputs, "now", lambda: deadline + timedelta(seconds=1)
            )
            assert (await export_outputs.cleanup(db))["outputs_deleted"] == 1
        return await original(db, location, purpose, call, *args, **kwargs)

    monkeypatch.setattr(storage_access, "run", raced)
    with pytest.raises(export_storage.ExportStorageError):
        await cache.lookup(db_session, artifact.cache_key)
    assert not export_objects.reads and not export_objects.signatures
    assert export_objects.deleted


async def test_write_drains_repeated_cancellation_before_zip_unlink(
    db_session,
    export_objects,
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"zip")
    receipt = await export_outputs.create(
        db_session,
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media="image",
        format="coco",
        file_count=1,
        path=str(path),
    )
    started, release = threading.Event(), threading.Event()
    original = export_objects.upload_file

    def upload(pathname, *args, **kwargs):
        started.set()
        release.wait(5)
        assert Path(pathname).exists()
        original(pathname, *args, **kwargs)

    monkeypatch.setattr(export_objects, "upload_file", upload)

    async def owned_zip():
        try:
            await export_outputs.write(db_session, receipt, str(path))
        finally:
            path.unlink()

    task = asyncio.create_task(owned_zip())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and path.exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not path.exists() and export_objects.puts
    assert (await db_session.get(ExportOutput, receipt.id)).state == "pending"
    assert not export_objects.deleted
    assert not await db_session.scalar(
        select(StorageOperation.id).where(
            StorageOperation.purpose == "export_write_" + receipt.id.hex,
        )
    )


@pytest.mark.parametrize("committed", [False, True])
async def test_worker_unknown_final_commit_never_deletes_candidate(
    committed, worker_db, monkeypatch
):
    from unittest.mock import Mock

    from app.workers import lidar_export as worker
    from tests.test_lidar_export_inputs import setup
    from tests.test_lidar_export_worker import Harness, request, state

    # All supported media now share this publication transaction. Keep the
    # original uncertainty test on real Bundle/ZIP rows, not a legacy bypass.
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    delete = Mock()
    monkeypatch.setattr(export_storage, "delete", delete)
    original_commit = db.commit
    fired = False

    async def unknown_commit():
        nonlocal fired
        status = await db.scalar(select(AsyncJob.status).where(AsyncJob.id == job_id))
        if not fired and status == "completed":
            fired = True
            if committed:
                await original_commit()
            raise OSError("commit acknowledgement unknown")
        await original_commit()

    monkeypatch.setattr(db, "commit", unknown_commit)
    with pytest.raises(OSError, match="acknowledgement unknown"):
        await worker.run_owned(db, **args)
    monkeypatch.setattr(db, "commit", original_commit)
    assert fired
    output = await state(db, ExportOutput, receipt.zip_output_id)
    assert output.state == ("ready" if committed else "pending")
    assert output.media_bundle_id == receipt.id
    saved_output = export_outputs.snapshot(output)
    current_job = await state(db, AsyncJob, job_id)
    assert current_job.status == ("completed" if committed else "failed")
    bundle = await state(db, ExportMediaBundle, receipt.id)
    assert bundle.state == ("ready" if committed else "building")
    assert (bundle.published_at is not None) is committed
    assert (
        bool(
            await db.scalar(
                select(ExportArtifact.id).where(
                    ExportArtifact.output_id == saved_output.id
                )
            )
        )
        is committed
    )
    physical_bytes = harness.objects[saved_output.id]
    assert hashlib.sha256(physical_bytes).hexdigest() == saved_output.sha256
    assert len(physical_bytes) == saved_output.size_bytes
    assert harness.built_paths and all(
        not path.exists() for path in harness.built_paths
    )
    delete.assert_not_called()


@pytest.mark.parametrize("path", ["fresh", "cached"])
async def test_key_natural_expiry_during_final_lock_wait_refuses_signing(
    db_session, export_project, export_objects, annotator, monkeypatch, path
):
    from app.db.models.api_key import ApiKey
    from app.core.security import hash_password
    from app.services.exporting import output_authority
    from app.workers.export import _sign_export_output
    from starlette.requests import Request

    actor = annotator[0]
    token = "ak_" + uuid.uuid4().hex
    current = datetime.now(timezone.utc)
    key = ApiKey(
        user_id=actor.id,
        name="expiry-fixture",
        key_prefix=token[:12],
        key_hash=hash_password(token),
        scopes=["*"],
        expires_at=current + timedelta(hours=1),
    )
    db_session.add(key)
    await db_session.flush()
    request = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
    )
    request.state.api_key_id = key.id
    request.state.api_key_scopes = ["*"]
    proof = await output_authority.capture(db_session, export_project, actor, request)
    job = SimpleNamespace(
        user_id=actor.id,
        project_id=export_project.id,
        payload={"output_authority": proof},
    )
    await output_authority.validate(db_session, job, lock=True)
    row = await seed_output(db_session, export_objects, project_id=export_project.id)
    index = await seed_index(db_session, row)
    hit = await cache.lookup(db_session, index.cache_key)

    class Later:
        @staticmethod
        def now(_timezone):
            return current + timedelta(hours=2)

    async def final_validation():
        await output_authority.validate(db_session, job, lock=True)

    if path == "cached":
        original = storage_access.check

        async def waiting_check(*args, **kwargs):
            result = await original(*args, **kwargs)
            monkeypatch.setattr(output_authority, "datetime", Later)
            return result

        monkeypatch.setattr(storage_access, "check", waiting_check)
        with pytest.raises(output_authority.ExportOutputAuthorityError):
            await export_outputs.sign_cached(
                db_session,
                hit,
                project_id=row.project_id,
                download_name="export.zip",
                before_sign=final_validation,
            )
    else:
        monkeypatch.setattr(output_authority, "datetime", Later)
        with pytest.raises(output_authority.ExportOutputAuthorityError):
            await _sign_export_output(db_session, job, hit.output, "export.zip")
    assert not export_objects.signatures
    assert row.state == "ready" and index.output_id == row.id
