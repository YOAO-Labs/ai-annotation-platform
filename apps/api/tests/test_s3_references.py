"""Persistent external identities use exact keys and fenced page commits."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import uuid

from botocore.exceptions import ClientError
from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_source import DatasetScanEntry, DatasetScanRun
from app.db.models.storage_connection import StorageConnection
from app.schemas.dataset_source import DatasetSourceCreate
from app.services import dataset_scans as scans, dataset_content as content
from app.services import storage_location as locations, connector_guard
from app.services.content_snapshot import ContentError
from app.services.dataset_references import register_entry, reference_snapshot
from app.services.filesystem_storage import FilesystemStorageError
from app.services.sources import s3_reference as backend
from tests.test_dataset_registration import start, drain, begin_confirmed


class MemoryS3:
    def __init__(self):
        output = io.BytesIO()
        Image.new("RGB", (48, 32), "navy").save(output, format="PNG")
        self.data = output.getvalue()
        self.objects = {"root/collection/a//图片 +%2F.png": self.data}
        self.calls = []
        self.version = None
        self.get_hook = None
        self.status = 200
        self.ignore_condition = False

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list", kwargs))
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        offset = int(kwargs.get("ContinuationToken", "0").split("+")[0])
        limit = kwargs["MaxKeys"]
        selected = keys[offset : offset + limit]
        return {
            "EncodingType": "url",
            "IsTruncated": len(keys) > offset + limit,
            "NextContinuationToken": f"{offset + limit}+%2F",
            "Contents": [
                {
                    "Key": quote(k, safe=""),
                    "Size": len(self.objects[k]),
                    "ETag": '"opaque-etag"',
                    "LastModified": datetime(2026, 10, 23, tzinfo=timezone.utc),
                }
                for k in selected
            ],
        }

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        if (
            kwargs.get("IfMatch", '"opaque-etag"') != '"opaque-etag"'
            and not self.ignore_condition
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "HeadObject")
        return {
            "ContentLength": len(self.objects[kwargs["Key"]]),
            "ETag": '"opaque-etag"',
            "LastModified": datetime(2026, 10, 23, tzinfo=timezone.utc),
            "VersionId": self.version,
            "ResponseMetadata": {"HTTPStatusCode": self.status},
        }

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.get_hook:
            self.get_hook()
        return {
            **self.head_object(**kwargs),
            "Body": io.BytesIO(self.objects[kwargs["Key"]]),
        }


@pytest.fixture
async def s3_world(db_session, super_admin, monkeypatch):
    memory = MemoryS3()

    @contextmanager
    def client(self):
        yield memory

    async def allowed(*args):
        return None

    monkeypatch.setattr(backend.S3Access, "client", client)
    monkeypatch.setattr(connector_guard, "assert_connection_target_allowed", allowed)
    actor = super_admin[0]
    connection = StorageConnection(
        name=f"s3-{uuid.uuid4()}",
        kind="s3",
        config={
            "endpoint": "http://10.1.1.1:9000",
            "bucket": "external",
            "base_prefix": "root",
        },
        secret_enc=b"sealed",
        scope="owner",
        created_by=actor.id,
    )
    dataset = Dataset(
        display_id=f"s3-{uuid.uuid4().hex[:10]}",
        name="S3 reference",
        created_by=actor.id,
    )
    db_session.add_all([connection, dataset])
    await db_session.flush()
    location = await locations.register_connection(db_session, connection)
    for node in ["worker:content@test", locations.api_node_id()]:
        result = await locations.perform_probe(
            db_session, location, node, "collection/a//图片 +%2F.png"
        )
        assert result["ok"], result
        await locations.store_probe(
            db_session, location, node, result, datetime.now(timezone.utc)
        )
    source = await scans.create_source(
        db_session,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name="S3",
            relative_path="collection",
            node_id="content@test",
            cache_policy="none",
        ),
        actor,
    )
    return memory, actor, dataset, location, source


async def scan(db, world):
    run, _ = await scans.start_scan(db, world[-1].id, world[1])
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    while True:
        work = await scans.next_directory(db, run.id, job.celery_task_id, fence)
        await db.commit()
        if work is None:
            break
        page = work["access"].list_page(work["path"], work["cursor"])
        await scans.apply_directory(db, run.id, job.celery_task_id, fence, work, page)
        await db.commit()
    return run


async def test_exact_keys_register_without_original_copies(db_session, s3_world):
    memory, actor, dataset, _, _ = s3_world
    keys = ["a//b.png", "a/./b.png", "a/../b.png", "a\\b.png", " edge +%2F.png "]
    memory.objects = {f"root/collection/{k}": memory.data for k in keys}
    memory.objects["root/collection-adjacent/secret.png"] = memory.data
    run = await scan(db_session, s3_world)
    entries = list(
        await db_session.scalars(
            select(DatasetScanEntry).where(
                DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
            )
        )
    )
    assert {e.relative_path for e in entries} == set(keys)
    for entry in entries:
        result = await register_entry(
            db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
        )
        snapshot = await reference_snapshot(db_session, result["item_id"], actor)
        assert snapshot.file.read() == memory.data
        snapshot.close()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == dataset.id)
        )
        == len(keys)
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetAssetLocation)
            .where(
                DatasetAssetLocation.asset_id.in_(
                    select(DatasetAsset.id)
                    .join(DatasetItem, DatasetItem.id == DatasetAsset.dataset_item_id)
                    .where(DatasetItem.dataset_id == dataset.id)
                )
            )
        )
        == 0
    )
    assert {
        args["Key"]
        for method, args in memory.calls
        if method == "get" and args["IfMatch"] == '"opaque-etag"'
    } >= {f"root/collection/{k}" for k in keys}


async def test_pagination_cancel_resume_preserves_committed_page(
    db_session, s3_world, monkeypatch
):
    monkeypatch.setattr(backend, "PAGE_SIZE", 1)
    memory, actor, _, _, source = s3_world
    memory.objects["root/collection/z.png"] = memory.data
    run, _ = await scans.start_scan(db_session, source.id, actor)
    await db_session.commit()
    job = await db_session.get(AsyncJob, run.async_job_id)
    old_task_id = job.celery_task_id
    fence = await scans.claim(db_session, run.id, old_task_id, "content@test")
    await db_session.commit()
    work = await scans.next_directory(db_session, run.id, old_task_id, fence)
    page = work["access"].list_page(work["path"], work["cursor"])
    await scans.apply_directory(db_session, run.id, old_task_id, fence, work, page)
    await db_session.commit()
    await scans.cancel_job(db_session, job.id, actor)
    await db_session.commit()
    await scans.resume_scan(db_session, run.id, actor)
    await db_session.commit()
    run_id = run.id
    with pytest.raises(scans.LostScanLease):
        await scans.apply_directory(db_session, run.id, old_task_id, fence, work, page)
    await db_session.rollback()
    run = await db_session.get(DatasetScanRun, run_id)
    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db_session, run.id, job.celery_task_id, "content@test")
    work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
    assert work["cursor"] == "1+%2F"
    page = work["access"].list_page(work["path"], work["cursor"])
    await scans.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, page
    )
    assert (
        await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
        is None
    )
    assert run.complete and run.counts["file_count"] == 2


async def test_same_etag_changed_bytes_cannot_replace_registered_identity(
    db_session, s3_world
):
    memory, actor, *_ = s3_world
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    result = await register_entry(
        db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    key = next(iter(memory.objects))
    memory.objects[key] = memory.data.replace(b"IDAT", b"JDAT", 1)
    with pytest.raises(ContentError, match="content_mismatch"):
        await reference_snapshot(db_session, result["item_id"], actor)


async def test_preflight_registration_and_copy_use_same_backend(db_session, s3_world):
    memory, actor, dataset, location, source = s3_world
    source.ownership_mode = "managed_copy"
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    # Reuse the existing durable registration acceptance helpers.
    world = (memory, memory.data, actor, dataset, location, source, run, entry)
    preflight, task_id, fence = await start(db_session, world)
    await drain(db_session, preflight, task_id, fence)
    assert preflight.counts["ready_count"] == 1
    task_id, fence = await begin_confirmed(db_session, preflight, world)
    await drain(db_session, preflight, task_id, fence)
    assert preflight.counts["registered_count"] == 1
    replica = await db_session.scalar(select(DatasetAssetLocation))
    from app.services.storage import storage_service

    body = storage_service.client.get_object(
        Bucket=replica.bucket, Key=replica.object_key
    )["Body"]
    try:
        assert (
            hashlib.sha256(body.read()).hexdigest()
            == hashlib.sha256(memory.data).hexdigest()
        )
    finally:
        body.close()
    assert "a//" not in replica.object_key


async def test_credential_revision_change_during_read_cannot_publish_receipt(
    db_session, s3_world, monkeypatch
):
    _, actor, _, location, _ = s3_world
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    real_capture = content.snapshots.capture

    async def rotated(*args, **kwargs):
        snapshot = await real_capture(*args, **kwargs)
        location.config_revision += 1
        await db_session.commit()
        return snapshot

    monkeypatch.setattr(content.snapshots, "capture", rotated)
    entry_key = (run.id, entry.path_digest)
    with pytest.raises(FilesystemStorageError, match="configuration_changed"):
        await content.scan_snapshot(
            db_session,
            run.id,
            entry.path_digest,
            actor,
            verify=True,
            node_id="worker:content@test",
        )
    await db_session.rollback()
    current = await db_session.get(DatasetScanEntry, entry_key, populate_existing=True)
    assert current.verified_content is None


@pytest.mark.parametrize("status", [206, 204])
async def test_non_full_response_is_not_a_verified_original(
    db_session, s3_world, status
):
    memory, actor, *_ = s3_world
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    memory.status = status
    with pytest.raises(FilesystemStorageError, match="s3_invalid_response"):
        await content.scan_snapshot(
            db_session,
            run.id,
            entry.path_digest,
            actor,
            verify=True,
            node_id="worker:content@test",
        )


async def test_ignored_conditions_fail_sample_probe(db_session, s3_world):
    memory, _, _, location, _ = s3_world
    memory.ignore_condition = True
    result = await locations.perform_probe(
        db_session, location, locations.api_node_id(), "collection/a//图片 +%2F.png"
    )
    assert not result["ok"] and result["code"] == "s3_conditions_not_supported"


async def test_scan_rejects_repeated_continuation_token(
    db_session, s3_world, monkeypatch
):
    monkeypatch.setattr(backend, "PAGE_SIZE", 1)
    memory, actor, _, _, source = s3_world
    memory.objects["root/collection/z.png"] = memory.data
    run, _ = await scans.start_scan(db_session, source.id, actor)
    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db_session, run.id, job.celery_task_id, "content@test")
    work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
    page = work["access"].list_page(work["path"], work["cursor"])
    await scans.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, page
    )
    work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
    with pytest.raises(FilesystemStorageError, match="s3_invalid_cursor"):
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, page
        )
    assert not run.complete


def test_client_guards_actual_destination_and_disables_proxy(monkeypatch):
    monkeypatch.setattr(
        backend,
        "decrypt_secret",
        lambda value: {"access_key": "test", "secret_key": "test"},
    )
    monkeypatch.setattr(connector_guard, "assert_host_allowed", lambda *args: None)
    access = backend.S3Access(
        {"endpoint": "http://10.1.1.1:9000", "bucket": "external"}, b"sealed", ()
    )
    with access.client() as client:
        assert client.meta.config.proxies == {}
        client.meta.events.emit(
            "before-send.s3.GetObject",
            request=SimpleNamespace(url="http://10.1.1.1:9000/external/a//../b"),
        )
        with pytest.raises(FilesystemStorageError, match="s3_redirect_denied"):
            client.meta.events.emit(
                "before-send.s3.GetObject",
                request=SimpleNamespace(url="http://10.1.1.2:9000/external/key"),
            )


@pytest.mark.parametrize(
    "key", ["root/a//b", "root/./b", "root/a\\b", "root/ 中文 +%2F "]
)
def test_encoded_key_is_decoded_exactly_once(key):
    assert backend.decode_key(quote(key, safe="")) == key
    assert backend.decode_key(quote_plus(key, safe="")) == key


async def test_ignored_conditions_cannot_start_or_bypass_preflight(
    db_session, s3_world
):
    from app.services import dataset_registration as registration

    memory, actor, dataset, _, _ = s3_world
    run = await scan(db_session, s3_world)
    memory.ignore_condition = True
    with pytest.raises(HTTPException) as failure:
        await registration.start_preflight(db_session, run.id, actor)
    assert (
        getattr(failure.value, "detail", {}).get("reason")
        == "s3_conditions_not_supported"
    )
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    with pytest.raises(FilesystemStorageError, match="s3_conditions_not_supported"):
        await register_entry(
            db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
        )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == dataset.id)
        )
        == 0
    )


async def test_version_receipt_pins_every_subsequent_read(db_session, s3_world):
    memory, actor, *_ = s3_world
    memory.version = "immutable-v1"
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    result = await register_entry(
        db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    memory.calls.clear()
    snapshot = await reference_snapshot(db_session, result["item_id"], actor)
    snapshot.close()
    assert all(
        args.get("VersionId") == "immutable-v1"
        for method, args in memory.calls
        if method in {"head", "get"}
    )


async def test_fractional_listing_time_matches_http_date(
    db_session, s3_world, monkeypatch
):
    memory, actor, *_ = s3_world
    original = memory.list_objects_v2

    def fractional(**kwargs):
        response = original(**kwargs)
        for obj in response["Contents"]:
            obj["LastModified"] = obj["LastModified"].replace(microsecond=123000)
        return response

    monkeypatch.setattr(memory, "list_objects_v2", fractional)
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    result = await register_entry(
        db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    assert result["created"]


async def test_platform_location_cannot_be_mutated_or_used_as_external_source(
    db_session, httpx_client, super_admin
):
    from app.services.dataset_assets import (
        ensure_legacy_bindings,
        validate_legacy_bindings,
        LEGACY_LOCATION_ID,
    )

    await ensure_legacy_bindings(db_session)
    dataset = Dataset(
        display_id=f"guard-{uuid.uuid4().hex[:8]}",
        name="Guard",
        created_by=super_admin[0].id,
    )
    db_session.add(dataset)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {super_admin[1]}"}
    response = await httpx_client.post(
        f"/api/v1/storage-locations/{LEGACY_LOCATION_ID}/change-preview",
        json={"expected_revision": 1, "cache_policy": "none"},
        headers=headers,
    )
    assert response.status_code == 409
    response = await httpx_client.post(
        "/api/v1/dataset-sources",
        json={
            "dataset_id": str(dataset.id),
            "location_id": str(LEGACY_LOCATION_ID),
            "name": "Invalid",
            "node_id": "content@test",
        },
        headers=headers,
    )
    assert response.status_code == 409
    assert await validate_legacy_bindings(db_session) == []


async def test_source_outage_stops_worker_without_consuming_remaining_candidates(
    db_session, s3_world, monkeypatch
):
    from contextlib import asynccontextmanager
    from app.services import dataset_registration as registration
    from app.db.models.dataset_source import (
        DatasetRegistrationEntry,
        DatasetRegistrationRun,
    )
    from app.workers import dataset_registration as worker

    memory, actor, *_ = s3_world
    memory.objects["root/collection/z.png"] = memory.data
    scan_run = await scan(db_session, s3_world)
    run, _ = await registration.start_preflight(db_session, scan_run.id, actor)
    await db_session.commit()
    run_id = run.id
    job = await db_session.get(AsyncJob, run.async_job_id)
    task_id = job.celery_task_id

    @asynccontextmanager
    async def session():
        yield db_session

    async def dispose():
        pass

    async def offline(*args):
        raise FilesystemStorageError("content_source_unavailable")

    monkeypatch.setattr(
        worker, "create_async_engine", lambda *args: SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: session)
    monkeypatch.setattr(worker, "process_one", offline)
    await worker.execute_registration(run_id, task_id, "content@test")
    run = await db_session.get(DatasetRegistrationRun, run_id, populate_existing=True)
    assert run.status == "failed" and run.error_code == "content_source_unavailable"
    entries = list(
        await db_session.scalars(
            select(DatasetRegistrationEntry).where(
                DatasetRegistrationEntry.run_id == run_id
            )
        )
    )
    assert len(entries) == 2 and all(e.state == "pending" for e in entries)
    assert run.counts["error_count"] == 0
    resumed = await registration.resume(db_session, run_id, actor)
    assert resumed.status == "pending"
