from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from yoao_assets_images.contract import ObjectIdentity
from yoao_assets_source import managed_original
from yoao_assets_source.import_verification_contract import ImportVerificationResult
from yoao_assets_source.connectors import SourceChangedError
from yoao_assets_source.import_contract import ImportEntry, ImportSourceObject
from yoao_assets_source.managed_original_contract import ManagedOriginalMaterial
from yoao_assets_source.saved_io import SavedIOError
from yoao_assets_source_import.storage import (
    DestinationStorage,
    DestinationWriteError,
)


class _ShortRead(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 512 * 1024))


class _Store:
    """Minimal versioned conditional S3 used by the managed writer tests."""

    def __init__(self):
        self.objects: dict[tuple[str, str, str], dict] = {}
        self.uploads: dict[str, dict] = {}
        self.parts: list[tuple[int, bytes]] = []
        self.created = False
        self.aborted = False
        self.version = 0

    def _error(self, code, status):
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
            "GetObject",
        )

    def create_multipart_upload(self, **kwargs):
        self.created = True
        upload_id = f"upload-{uuid4()}"
        self.uploads[upload_id] = {
            "bucket": kwargs["Bucket"],
            "key": kwargs["Key"],
            "content_type": kwargs.get("ContentType"),
        }
        return {"UploadId": upload_id, "ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        self.parts.append((PartNumber, Body))
        return {
            "ETag": f"part-{PartNumber}",
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def abort_multipart_upload(self, **kwargs):
        self.aborted = True
        self.uploads.pop(kwargs.get("UploadId"), None)

    def _store(self, bucket, key, body, content_type, etag):
        self.version += 1
        version = f"v{self.version}"
        self.objects[(bucket, key, version)] = {
            "body": body,
            "etag": etag,
            "version": version,
            "content_type": content_type or "application/octet-stream",
            "last_modified": datetime.now(timezone.utc),
        }
        return version

    def _write(self, bucket, key, body, content_type):
        for (bucket_name, object_key, _), value in self.objects.items():
            if (bucket_name, object_key) == (bucket, key):
                return value
        return None

    def complete_multipart_upload(self, **kwargs):
        if self._write(kwargs["Bucket"], kwargs["Key"], b"", None) is not None:
            raise self._error("PreconditionFailed", 412)
        upload = self.uploads.pop(kwargs["UploadId"])
        body = b"".join(part for _, part in sorted(self.parts))
        next_version = f"v{self.version + 1}"
        etag = f'"mpu-{next_version}"'
        version = self._store(
            upload["bucket"],
            upload["key"],
            body,
            upload["content_type"],
            etag,
        )
        return {
            "ETag": etag,
            "VersionId": version,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def put_object(self, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and self._write(
            kwargs["Bucket"], kwargs["Key"], b"", None
        ):
            raise self._error("PreconditionFailed", 412)
        body = kwargs.get("Body", b"")
        etag = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'
        version = self._store(
            kwargs["Bucket"], kwargs["Key"], body, kwargs.get("ContentType"), etag
        )
        return {
            "ETag": etag,
            "VersionId": version,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def _object(self, bucket, key, version):
        if version is not None:
            value = self.objects.get((bucket, key, version))
            if value is None:
                raise self._error("NoSuchVersion", 404)
            return value
        current = self._write(bucket, key, b"", None)
        if current is None:
            raise self._error("NoSuchKey", 404)
        return current

    def head_object(self, **kwargs):
        value = self._object(kwargs["Bucket"], kwargs["Key"], kwargs.get("VersionId"))
        return {
            "ContentLength": len(value["body"]),
            "ETag": value["etag"],
            "VersionId": value["version"],
            "LastModified": value["last_modified"],
            "ContentType": value["content_type"],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def get_object(self, **kwargs):
        value = self._object(kwargs["Bucket"], kwargs["Key"], kwargs.get("VersionId"))
        expected = kwargs.get("IfMatch")
        if expected is not None and expected != value["etag"]:
            raise self._error("PreconditionFailed", 412)
        return {
            "Body": io.BytesIO(value["body"]),
            "ContentLength": len(value["body"]),
            "ETag": value["etag"],
            "VersionId": value["version"],
            "LastModified": value["last_modified"],
            "ContentType": value["content_type"],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


def _config():
    return SimpleNamespace(
        datasets_bucket="datasets",
        s3_read_timeout_seconds=10,
        s3_connect_timeout_seconds=2,
        s3_endpoint="http://s3",
        s3_region="us-east-1",
        s3_access_key="key",
        s3_secret_key="secret",
    )


def _entry(size: int, *, file_type: str = "other") -> ImportEntry:
    item_id = uuid4()
    publication_id = uuid4()
    dataset_id = uuid4()
    name = "source.bin" if file_type == "other" else "source.jpg"
    return ImportEntry(
        job_id=uuid4(),
        lease_token=uuid4(),
        index=0,
        item_id=item_id,
        publication_id=publication_id,
        file_name=name,
        file_path=f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/{name}",
        file_type=file_type,
        source=ImportSourceObject(relpath=name, size=size, mtime=None, etag='"etag"'),
    )


def _target(entry: ImportEntry, *, max_bytes: int | None = None):
    dataset_id = UUID(entry.file_path.split("/")[1])
    return ManagedOriginalMaterial(
        creation_id=uuid4(),
        dataset_id=dataset_id,
        location_id=UUID("4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"),
        bucket="datasets",
        object_key=entry.file_path,
        key_prefix="",
        endpoint_url="http://s3",
        storage_identity="a" * 64,
        config_revision=1,
        binding_generation=1,
        max_bytes=max_bytes if max_bytes is not None else entry.source.size,
        content_type="application/octet-stream",
        origin="connector",
        mode="write",
        publication_id=entry.publication_id,
        file_path=entry.file_path,
    )


def _verify_from_store(store, entry, payload):
    async def verify(_expected):
        obj = store._object("datasets", entry.file_path, None)
        receipt = managed_original.WriteReceipt(obj["etag"], obj["version"], None, {})
        try:
            result = managed_original.verify(
                store,
                "datasets",
                entry.file_path,
                receipt,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                max_bytes=len(payload),
            )
        except managed_original.WriterError as exc:
            raise DestinationWriteError(exc.code) from None
        return ImportVerificationResult(
            sha256=result.sha256,
            md5=result.md5,
            size_bytes=result.size_bytes,
            etag=receipt.etag,
            version_id=receipt.version_id,
            observation=result.observation,
            physical_settled=True,
            current_head_matches=True,
        )

    return verify


@pytest.mark.asyncio
async def test_streaming_aggregates_short_reads_hashes_and_reads_back_exactly() -> None:
    data = b"x" * (8 * 1024 * 1024 + 123)
    store = _Store()
    storage = DestinationStorage(_config(), client=store)
    entry = _entry(len(data))

    result = await storage.transfer(
        _ShortRead(data),
        entry,
        target=_target(entry),
        byte_budget=len(data),
        before_io=lambda: None,
        on_bytes=lambda amount: None,
        on_verify=_verify_from_store(store, entry, data),
    )

    assert result.file_size == len(data)
    assert result.content_hash == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert result.proof.sha256 == hashlib.sha256(data).hexdigest()
    assert result.proof.verification["sha256"] == result.proof.sha256
    assert result.proof.verification["version_id"] == result.proof.version_id
    assert result.dimensions is None
    assert result.object_identity is None
    assert len(store.parts) >= 2
    assert (
        store.objects[("datasets", entry.file_path, result.proof.version_id)]["body"]
        == data
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "expected_part"),
    [
        (200 * 1024**3, 21 * 1024**2),
        (64 * 1024**2 * 10_000, 64 * 1024**2),
        (64 * 1024**2 * 10_000 + 1, None),
    ],
)
async def test_actual_writer_enforces_the_multipart_limit(size, expected_part) -> None:
    reads = []

    class Interrupted:
        def read(self, requested):
            reads.append(requested)
            raise SavedIOError("download_interrupted", unsettled=True)

    store = _Store()
    storage = DestinationStorage(_config(), client=store)
    entry = _entry(size)
    error = (
        pytest.raises(SavedIOError, match="download_interrupted")
        if expected_part is not None
        else pytest.raises(DestinationWriteError, match="supported multipart size")
    )
    with error:
        await storage.transfer(
            Interrupted(),
            entry,
            target=_target(entry),
            byte_budget=size,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
            on_verify=_verify_from_store(store, entry, b""),
        )
    assert reads == ([expected_part] if expected_part is not None else [])
    assert not store.created and not store.objects


@pytest.mark.asyncio
async def test_transfer_rejects_size_larger_than_budget_before_destination_write() -> (
    None
):
    store = _Store()
    storage = DestinationStorage(_config(), client=store)
    entry = _entry(4)

    with pytest.raises(SourceChangedError):
        await storage.transfer(
            io.BytesIO(b"data"),
            entry,
            target=_target(entry, max_bytes=100),
            byte_budget=3,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
            on_verify=_verify_from_store(store, entry, b"data"),
        )
    assert not store.created
    assert not store.objects


@pytest.mark.asyncio
async def test_conditional_creation_conflict_is_never_read_back_or_published() -> None:
    store = _Store()
    entry = _entry(4)
    store.put_object(
        Bucket="datasets", Key=entry.file_path, Body=b"old", ContentType="text/plain"
    )
    storage = DestinationStorage(_config(), client=store)

    with pytest.raises(DestinationWriteError) as caught:
        await storage.transfer(
            io.BytesIO(b"data"),
            entry,
            target=_target(entry),
            byte_budget=4,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
            on_verify=_verify_from_store(store, entry, b"data"),
        )
    assert caught.value.outcome == "conflict"
    assert store.objects[("datasets", entry.file_path, "v1")]["body"] == b"old"


@pytest.mark.asyncio
async def test_readback_mismatch_fails_the_transfer(monkeypatch) -> None:
    store = _Store()
    entry = _entry(4)
    storage = DestinationStorage(_config(), client=store)
    original_get = store.get_object

    def corrupt_if_match(**kwargs):
        response = original_get(**kwargs)
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != '"aap-impossible-etag"':
            response["Body"] = io.BytesIO(b"evil")
        return response

    monkeypatch.setattr(store, "get_object", corrupt_if_match)

    with pytest.raises(DestinationWriteError):
        await storage.transfer(
            io.BytesIO(b"data"),
            entry,
            target=_target(entry),
            byte_budget=4,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
            on_verify=_verify_from_store(store, entry, b"data"),
        )


@pytest.mark.asyncio
async def test_empty_source_uses_conditional_put_and_full_readback() -> None:
    store = _Store()
    entry = _entry(0)
    storage = DestinationStorage(_config(), client=store)

    result = await storage.transfer(
        io.BytesIO(b""),
        entry,
        target=_target(entry),
        byte_budget=0,
        before_io=lambda: None,
        on_bytes=lambda amount: None,
        on_verify=_verify_from_store(store, entry, b""),
    )

    assert result.file_size == 0
    assert result.content_hash == hashlib.md5(b"", usedforsecurity=False).hexdigest()
    assert result.proof.sha256 == hashlib.sha256(b"").hexdigest()
    assert not store.created
    assert (
        store.objects[("datasets", entry.file_path, result.proof.version_id)]["body"]
        == b""
    )


@pytest.mark.asyncio
async def test_image_identity_comes_from_the_verified_saved_version() -> None:
    store = _Store()
    entry = _entry(4, file_type="image")
    storage = DestinationStorage(_config(), client=store)

    result = await storage.transfer(
        io.BytesIO(b"data"),
        entry,
        target=_target(entry),
        byte_budget=4,
        before_io=lambda: None,
        on_bytes=lambda amount: None,
        on_verify=_verify_from_store(store, entry, b"data"),
    )

    assert result.object_identity == ObjectIdentity(
        etag=result.proof.verification["etag"].strip('"'),
        byte_size=4,
        version_id=result.proof.version_id,
    )
