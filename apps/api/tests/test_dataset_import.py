from __future__ import annotations

import hashlib
import io
import uuid

from sqlalchemy import func, select

from aap_assets_source.import_contract import ImportAcceptance, ImportContext
from app.db.models.async_job import AsyncJobStatus
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.storage_connection import StorageConnection
from app.services import async_job as async_job_svc
from app.services.assets_import import DatasetImportCapabilities
from app.services.dataset import DatasetService
from app.services.storage import storage_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeDatasetBucketClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.parts: dict[str, list[bytes]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.next_upload_id = 1

    def create_multipart_upload(self, *, Bucket, Key, ContentType):
        upload_id = f"upload-{self.next_upload_id}"
        self.next_upload_id += 1
        self.parts[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, PartNumber, UploadId, Body):
        self.parts[UploadId].append(bytes(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        self.objects[(Bucket, Key)] = b"".join(self.parts.pop(UploadId))

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.parts.pop(UploadId, None)

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = bytes(Body)

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)


class _ChunkOnlyStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        assert size != -1
        return super().read(size)


async def _seed_dataset(db_session, owner_id: uuid.UUID) -> Dataset:
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-IMP-{uuid.uuid4().hex[:6]}",
        name=f"dataset-import-{uuid.uuid4().hex[:6]}",
        data_type="image",
        created_by=owner_id,
    )
    db_session.add(ds)
    await db_session.flush()
    return ds


async def test_ingest_one_streams_upload_and_skips_duplicate(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    fake_client = _FakeDatasetBucketClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions", lambda *a, **k: None)
    monkeypatch.setattr(
        storage_service, "read_image_dimensions_from_bytes", lambda *a, **k: None
    )

    svc = DatasetService(db_session)
    first = await svc.ingest_one(
        ds.id,
        "remote/sample.txt",
        _ChunkOnlyStream(b"same-bytes"),
        size=10,
    )
    assert first.status == "added"
    assert first.file_size == 10
    assert first.content_hash == hashlib.md5(b"same-bytes").hexdigest()
    assert (
        fake_client.objects[(storage_service.datasets_bucket, f"{ds.name}/sample.txt")]
        == b"same-bytes"
    )

    second = await svc.ingest_one(
        ds.id,
        "remote/sample.txt",
        _ChunkOnlyStream(b"same-bytes"),
        size=10,
    )
    assert second.status == "skipped"
    assert second.reason == "content_hash_exists"
    assert (
        storage_service.datasets_bucket,
        f"{ds.name}/sample-1.txt",
    ) in fake_client.deleted

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == ds.id)
        )
    ).scalar_one()
    assert count == 1


async def test_ingest_one_preserves_subdirectory_structure(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    fake_client = _FakeDatasetBucketClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions", lambda *a, **k: None)
    monkeypatch.setattr(
        storage_service, "read_image_dimensions_from_bytes", lambda *a, **k: None
    )

    svc = DatasetService(db_session)
    outcome = await svc.ingest_one(
        ds.id,
        "dataset-A/a/img.jpg",
        _ChunkOnlyStream(b"bytes"),
        size=5,
        dest_relpath="a/img.jpg",
    )
    assert outcome.status == "added"

    item = (
        await db_session.execute(
            select(DatasetItem).where(DatasetItem.dataset_id == ds.id)
        )
    ).scalar_one()
    # 子目录层级保留进 file_name 与 file_path（无多余嵌套）
    assert item.file_name == "a/img.jpg"
    assert item.file_path == f"{ds.name}/a/img.jpg"
    assert (
        storage_service.datasets_bucket,
        f"{ds.name}/a/img.jpg",
    ) in fake_client.objects


async def test_import_from_connection_api_uses_native_idempotent_acceptance(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    ds = await _seed_dataset(db_session, user.id)
    conn = StorageConnection(
        id=uuid.uuid4(),
        name="external-s3",
        kind="s3",
        config={"endpoint": "http://8.8.8.8:9000", "bucket": "incoming"},
        secret_enc=None,
        scope="global",
        created_by=user.id,
    )
    db_session.add(conn)
    await db_session.flush()

    async def allow(_db, _target):
        return None

    monkeypatch.setattr(
        "app.services.connector_guard.assert_connection_target_allowed", allow
    )
    context = ImportContext(
        actor_id=user.id,
        dataset_id=ds.id,
        dataset_display_id=ds.display_id,
        dataset_name=ds.name,
        connection_id=conn.id,
        connection_name=conn.name,
        connection_kind="s3",
        connection_config={
            "endpoint": "https://s3.example.test",
            "bucket": "incoming",
        },
    )
    operation_id = uuid.uuid4()
    prepared: list[tuple] = []
    accepted: list[dict] = []

    async def prepare(self, *, actor_id, dataset_id, connection_id):
        prepared.append((actor_id, dataset_id, connection_id))
        return context

    async def accept(self, **kwargs):
        accepted.append(kwargs)
        return ImportAcceptance(
            job_id=kwargs["operation_id"],
            actor_id=kwargs["actor_id"],
            dataset_id=kwargs["dataset_id"],
            connection_id=kwargs["connection_id"],
        )

    async def snapshot(_db):
        return {
            "dataset_import_max_files": 100,
            "dataset_import_max_total_bytes": 10_000,
            "version": "v1-test",
        }

    monkeypatch.setattr(DatasetImportCapabilities, "prepare", prepare)
    monkeypatch.setattr(DatasetImportCapabilities, "accept", accept)
    monkeypatch.setattr(
        "app.services.system_settings_service.SystemSettingsService.get_import_limits_snapshot",
        staticmethod(snapshot),
    )

    request_body = {
        "connection_id": str(conn.id),
        "source_path": "batch-a",
        "recursive": True,
        "include_globs": [" *.jpg ", ""],
    }
    headers = {
        **_bearer(token),
        "Idempotency-Key": str(operation_id),
    }
    for _ in range(2):
        response = await httpx_client.post(
            f"/api/v1/datasets/{ds.id}/import-from-connection",
            headers=headers,
            json=request_body,
        )
        assert response.status_code == 202, response.text
        assert response.json()["job_id"] == str(operation_id)
        assert response.headers["idempotency-key"] == str(operation_id)

    assert prepared == [(user.id, ds.id, conn.id)] * 2
    assert len(accepted) == 2
    assert [call["operation_id"] for call in accepted] == [operation_id] * 2
    assert accepted[0]["request"] == {
        "source_path": "batch-a",
        "recursive": True,
        "include_globs": ["*.jpg"],
    }
    assert accepted[0]["limits"].version == "v1-test"
    assert accepted[0]["actor_id"] == user.id
    assert accepted[0]["dataset_id"] == ds.id
    assert accepted[0]["connection_id"] == conn.id
    assert "secret" not in str(accepted)
    assert "access_key" not in str(accepted)


async def test_legacy_dataset_import_cancel_is_allowed(
    httpx_client, db_session, annotator
):
    user, token = annotator
    job = await async_job_svc.create_job(
        db_session,
        kind="dataset_import",
        user_id=user.id,
    )
    await async_job_svc.mark_running(db_session, job.id)
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel",
        headers=_bearer(token),
    )

    assert response.status_code == 200
    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.CANCELLED.value
