from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from io import BytesIO
from threading import Event
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from aap_assets_images.contract import ObjectIdentity
from aap_assets_source.managed_original_contract import ManagedOriginalMaterial
from aap_assets_source_runtime.ingest_storage import (
    IngestEntryFailure,
    IngestStorage,
    run_in_thread,
)
from aap_assets_source_runtime.ingest_workflow import IngestWorkflow
from test_ingest_workflow import _Data, _ImageAdmission, _command


class _Paginator:
    def __init__(self, pages, calls) -> None:
        self.pages = pages
        self.calls = calls

    def paginate(self, *, Bucket, Prefix):
        self.calls.append(("list", Bucket, Prefix))
        yield from self.pages


class _S3:
    """Versioned conditional object store covering adopt and managed writes."""

    def __init__(self, *, pages=(), objects=None) -> None:
        self.pages = pages
        self.objects: dict[tuple[str, str, str], dict] = {}
        self.uploads: dict[str, dict] = {}
        self.parts: list[tuple[int, bytes]] = []
        self.calls: list = []
        self.version = 0
        for key, body in (objects or {}).items():
            self._store("datasets", key, body, "application/octet-stream")

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self.pages, self.calls)

    def _error(self, code, status, operation="GetObject"):
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
            operation,
        )

    def _store(self, bucket, key, body, content_type):
        self.version += 1
        version = f"v{self.version}"
        etag = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'
        self.objects[(bucket, key, version)] = {
            "body": body,
            "etag": etag,
            "version": version,
            "content_type": content_type,
            "last_modified": datetime.now(timezone.utc),
        }
        return self.objects[(bucket, key, version)]

    def _current(self, bucket, key):
        for (bucket_name, object_key, _), value in self.objects.items():
            if (bucket_name, object_key) == (bucket, key):
                return value
        return None

    def _object(self, bucket, key, version):
        if version is not None:
            value = self.objects.get((bucket, key, version))
            if value is None:
                raise self._error("NoSuchVersion", 404)
            return value
        value = self._current(bucket, key)
        if value is None:
            raise self._error("NoSuchKey", 404)
        return value

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
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
        self.calls.append(("get", kwargs))
        value = self._object(kwargs["Bucket"], kwargs["Key"], kwargs.get("VersionId"))
        expected = kwargs.get("IfMatch")
        if expected is not None and expected != value["etag"]:
            raise self._error("PreconditionFailed", 412)
        return {
            "Body": BytesIO(value["body"]),
            "ContentLength": len(value["body"]),
            "ETag": value["etag"],
            "VersionId": value["version"],
            "LastModified": value["last_modified"],
            "ContentType": value["content_type"],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create", kwargs))
        upload_id = f"upload-{uuid4()}"
        self.uploads[upload_id] = {"bucket": kwargs["Bucket"], "key": kwargs["Key"]}
        return {"UploadId": upload_id, "ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        self.parts.append((PartNumber, Body))
        return {
            "ETag": f'"part-{PartNumber}"',
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort", kwargs))
        self.uploads.pop(kwargs.get("UploadId"), None)

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete", kwargs))
        if self._current(kwargs["Bucket"], kwargs["Key"]) is not None:
            raise self._error("PreconditionFailed", 412, "CompleteMultipartUpload")
        upload = self.uploads.pop(kwargs["UploadId"])
        body = b"".join(part for _, part in sorted(self.parts))
        value = self._store(
            upload["bucket"], upload["key"], body, "application/octet-stream"
        )
        return {
            "ETag": value["etag"],
            "VersionId": value["version"],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        if kwargs.get("IfNoneMatch") == "*" and self._current(
            kwargs["Bucket"], kwargs["Key"]
        ):
            raise self._error("PreconditionFailed", 412, "PutObject")
        value = self._store(
            kwargs["Bucket"],
            kwargs["Key"],
            kwargs.get("Body", b""),
            kwargs.get("ContentType", "application/octet-stream"),
        )
        return {
            "ETag": value["etag"],
            "VersionId": value["version"],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


def _target(file_path: str, *, max_bytes: int, mode: str = "adopt"):
    return ManagedOriginalMaterial(
        creation_id=uuid4(),
        dataset_id=uuid4(),
        location_id=uuid4(),
        bucket="datasets",
        object_key=file_path,
        key_prefix="",
        endpoint_url="http://s3",
        storage_identity="a" * 64,
        config_revision=1,
        binding_generation=1,
        max_bytes=max_bytes,
        content_type="application/octet-stream",
        origin="directory_copy" if mode == "adopt" else "zip",
        mode=mode,
        file_path=file_path,
    )


@pytest.mark.asyncio
async def test_scan_streams_more_than_one_s3_page_and_adopts_existing_objects() -> None:
    objects = {f"dataset/item-{index:04}.txt": b"x" for index in range(1001)}
    pages = [
        {
            "Contents": [
                {
                    "Key": key,
                    "Size": 1,
                    "ETag": '"' + hashlib.md5(body).hexdigest() + '"',
                }
                for key, body in list(objects.items())[:1000]
            ]
        },
        {
            "Contents": [
                {
                    "Key": key,
                    "Size": 1,
                    "ETag": '"' + hashlib.md5(body).hexdigest() + '"',
                }
                for key, body in list(objects.items())[1000:]
            ]
        },
    ]
    s3 = _S3(pages=pages, objects=objects)
    receipts = await IngestWorkflow(
        data=_Data(),
        storage=IngestStorage(s3, "datasets"),
        image_admission=_ImageAdmission(),
    ).scan(_command())

    assert receipts.added == 1001
    assert [call for call in s3.calls if call[0] == "list"] == [
        ("list", "datasets", "dataset/")
    ]
    assert not any(call[0] in {"create", "complete", "put"} for call in s3.calls)


@pytest.mark.asyncio
async def test_zip_publication_uses_conditional_create_and_full_readback() -> None:
    s3 = _S3()
    storage = IngestStorage(s3, "datasets")
    body = b"point cloud"
    target = _target(
        "dataset-sources/o/item/pub/frame.bin", max_bytes=len(body), mode="write"
    )

    facts, proof = await storage.publish(
        BytesIO(body),
        target,
        content_type="application/octet-stream",
        expected_size=len(body),
        expected_hash=hashlib.md5(body, usedforsecurity=False).hexdigest(),
        image=False,
    )

    assert facts.file_size == len(body)
    assert proof.sha256 == hashlib.sha256(body).hexdigest()
    assert proof.verification["version_id"] == proof.version_id
    assert any(
        call[0] == "complete" and call[1].get("IfNoneMatch") == "*" for call in s3.calls
    )
    assert any(call[0] == "get" and "VersionId" in call[1] for call in s3.calls)


@pytest.mark.asyncio
async def test_zip_publication_conflict_never_overwrites_existing_bytes() -> None:
    key = "dataset-sources/o/item/pub/frame.bin"
    s3 = _S3(objects={key: b"old"})
    storage = IngestStorage(s3, "datasets")
    target = _target(key, max_bytes=4, mode="write")

    with pytest.raises(IngestEntryFailure) as caught:
        await storage.publish(
            BytesIO(b"data"),
            target,
            content_type="application/octet-stream",
            expected_size=4,
            expected_hash=hashlib.md5(b"data", usedforsecurity=False).hexdigest(),
        )
    # A conditional creation conflict means the reserved target is no longer the
    # empty locator the owner allocated; the stable per-entry outcome is a source
    # change, and the pre-existing bytes stay untouched.
    assert caught.value.code == "source_changed"
    assert s3._current("datasets", key)["body"] == b"old"


@pytest.mark.asyncio
async def test_zip_image_identity_comes_from_the_verified_saved_version() -> None:
    s3 = _S3()
    storage = IngestStorage(s3, "datasets")
    body = b"image"
    target = _target(
        "dataset-sources/o/item/pub/pic.jpg", max_bytes=len(body), mode="write"
    )

    facts, proof = await storage.publish(
        BytesIO(body),
        target,
        content_type="image/jpeg",
        expected_size=len(body),
        expected_hash=hashlib.md5(body, usedforsecurity=False).hexdigest(),
        image=True,
    )

    assert facts.object_identity == ObjectIdentity(
        etag=proof.verification["etag"].strip('"'),
        byte_size=len(body),
        version_id=proof.version_id,
    )


@pytest.mark.asyncio
async def test_adopt_reads_and_pins_the_exact_saved_version() -> None:
    key = "dataset/images/pic.png"
    s3 = _S3(objects={key: b"source"})
    storage = IngestStorage(s3, "datasets")

    facts, proof = await storage.adopt(
        _target(key, max_bytes=6), expected_size=6, image=False
    )

    assert facts.file_size == 6
    assert proof.mode == "adopt"
    assert proof.verification["version_id"] == "v1"
    assert not any(call[0] in {"create", "complete", "put"} for call in s3.calls)


@pytest.mark.asyncio
async def test_adoption_rejects_a_changed_size() -> None:
    key = "dataset/images/pic.png"
    s3 = _S3(objects={key: b"source"})
    storage = IngestStorage(s3, "datasets")

    with pytest.raises(IngestEntryFailure, match="source_changed"):
        await storage.adopt(_target(key, max_bytes=99), expected_size=7, image=False)


@pytest.mark.asyncio
async def test_storage_list_failure_is_command_failure() -> None:
    class BrokenS3:
        def get_paginator(self, _name):
            raise OSError("boom")

    from aap_assets_source.ingest_contract import IngestFailure

    workflow = IngestWorkflow(
        data=_Data(),
        storage=IngestStorage(BrokenS3(), "datasets"),
        image_admission=_ImageAdmission(),
    )
    with pytest.raises(IngestFailure) as error:
        await workflow.scan(_command())
    assert error.value.code == "storage_unavailable"


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_blocking_storage_work() -> None:
    started, release = Event(), Event()

    def blocking():
        started.set()
        release.wait(timeout=5)
        return 1

    task = asyncio.create_task(run_in_thread(blocking))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
