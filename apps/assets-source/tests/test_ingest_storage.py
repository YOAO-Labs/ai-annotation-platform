from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from yoao_assets_images.contract import ObjectIdentity
from yoao_assets_source.managed_original_contract import ManagedOriginalMaterial
from yoao_assets_source_runtime.ingest_storage import (
    IngestEntryFailure,
    IngestStorage,
    run_in_thread,
)
from yoao_assets_source_runtime.ingest_workflow import IngestWorkflow
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
        self.uploads[upload_id] = {
            "bucket": kwargs["Bucket"],
            "key": kwargs["Key"],
            "parts": {},
        }
        return {"UploadId": upload_id, "ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        self.parts.append((PartNumber, Body))
        self.uploads[kwargs["UploadId"]]["parts"][PartNumber] = Body
        return {
            "ETag": f'"part-{PartNumber}"',
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort", kwargs))
        self.uploads.pop(kwargs.get("UploadId"), None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete", kwargs))
        if self._current(kwargs["Bucket"], kwargs["Key"]) is not None:
            raise self._error("PreconditionFailed", 412, "CompleteMultipartUpload")
        upload = self.uploads.pop(kwargs["UploadId"])
        body = b"".join(part for _, part in sorted(upload["parts"].items()))
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
async def test_scan_admits_verified_images_across_batches_without_publication_ids():
    from PIL import Image

    objects = {
        f"dataset/item-{index:04}.dat": f"point-{index}".encode()
        for index in range(199)
    }
    for name, color in (("first.png", "red"), ("second.png", "blue")):
        output = BytesIO()
        with Image.new("RGB", (19, 11), color) as picture:
            picture.save(output, "PNG")
        objects["dataset/nested/" + name] = output.getvalue()
    listed = [
        {
            "Key": key,
            "Size": len(body),
            "ETag": '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"',
        }
        for key, body in objects.items()
    ]
    s3 = _S3(
        objects=objects,
        pages=[{"Contents": listed[:200]}, {"Contents": listed[200:]}],
    )
    verified = []

    class VerifiedData(_Data):
        async def original_verified(self, creation_id, verification):
            verified.append(verification)
            await super().original_verified(creation_id, verification)

    data, admission = VerifiedData(), _ImageAdmission()
    receipt = await IngestWorkflow(
        data=data,
        storage=IngestStorage(s3, "datasets"),
        image_admission=admission,
    ).scan(_command())

    assert receipt.added == receipt.linked_tasks == 201
    assert receipt.error_count == 0
    assert data.calls.count("prepare") == data.calls.count("complete") == 2
    assert len(verified) == 201
    assert {proof["sha256"] for proof in verified} == {
        hashlib.sha256(body).hexdigest() for body in objects.values()
    }
    assert all(proof["physical_settled"] for proof in verified)
    planned_images = {
        entry.item_id: entry
        for entry in data.planned.values()
        if entry.file_type == "image"
    }
    assert len(admission.calls) == len(planned_images) == 2
    for request in admission.calls:
        planned = planned_images[request["item_id"]]
        body = objects[planned.file_path]
        assert planned.publication_id is None
        assert request["operation_namespace"] == planned.entry_id
        assert request["file_path"] == planned.source_key == planned.file_path
        assert request["file_size"] == len(body)
        assert (
            request["content_hash"]
            == hashlib.md5(body, usedforsecurity=False).hexdigest()
        )
        assert request["object_identity"] == ObjectIdentity(
            etag=request["content_hash"],
            byte_size=len(body),
            version_id=s3._current("datasets", planned.file_path)["version"],
        )
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

    from yoao_assets_source.ingest_contract import IngestFailure

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


@pytest.mark.asyncio
async def test_copy_uses_the_source_identity_for_reads_and_target_identity_for_writes():
    from yoao_assets_source_runtime.managed_storage import ManagedStorage

    body = b"separately authorized staging bytes"
    source = _S3(objects={"incoming.dat": body})
    destination = _S3()
    target = _target("published.dat", max_bytes=len(body), mode="write")
    written = await ManagedStorage(destination).publish_object(
        target,
        source_client=source,
        source_bucket="datasets",
        source_key="incoming.dat",
        source_etag=source._current("datasets", "incoming.dat")["etag"],
        source_version_id="v1",
        content_type="application/octet-stream",
        max_bytes=len(body),
        image=False,
    )

    assert written.proof.sha256 == hashlib.sha256(body).hexdigest()
    assert destination._current("datasets", "published.dat")["body"] == body
    assert not source.uploads and not source.parts
    assert all(
        call[1]["Key"] == "published.dat"
        for call in destination.calls
        if call[0] in {"head", "get"}
    )
    assert all(
        call[1].get("VersionId") == "v1" for call in source.calls if call[0] == "get"
    )


@pytest.mark.asyncio
async def test_lost_completion_acknowledgement_keeps_unknown_physical_ownership():
    class LostAck(_S3):
        def complete_multipart_upload(self, **kwargs):
            super().complete_multipart_upload(**kwargs)
            raise TimeoutError("completion acknowledgement lost")

    body = b"possibly published"
    s3 = LostAck()
    target = _target("published.dat", max_bytes=len(body), mode="write")
    with pytest.raises(IngestEntryFailure) as caught:
        await IngestStorage(s3, "datasets").publish(
            BytesIO(body),
            target,
            content_type="application/octet-stream",
            expected_size=len(body),
        )

    from yoao_assets_source_runtime.ingest_workflow import _failure_evidence

    evidence = _failure_evidence("write", caught.value)
    assert evidence["physical_outcome"] == "unknown"
    assert evidence["physical_settled"] is False
    assert s3._current("datasets", "published.dat")["body"] == body
    assert not any(call[0] == "abort" for call in s3.calls)


@pytest.mark.asyncio
async def test_readback_failure_preserves_the_observed_write_receipt():
    class MissingReadback(_S3):
        def head_object(self, **kwargs):
            raise self._error("NoSuchKey", 404, "HeadObject")

    body = b"published before readback failed"
    s3 = MissingReadback()
    target = _target("published.dat", max_bytes=len(body), mode="write")
    with pytest.raises(IngestEntryFailure) as caught:
        await IngestStorage(s3, "datasets").publish(
            BytesIO(body),
            target,
            content_type="application/octet-stream",
            expected_size=len(body),
        )

    from yoao_assets_source_runtime.ingest_workflow import _failure_evidence

    evidence = _failure_evidence("verify", caught.value)
    assert evidence["physical_outcome"] == "written"
    assert evidence["physical_settled"] is True
    assert evidence["receipt"]["version_id"] == "v1"
    assert s3._current("datasets", "published.dat")["body"] == body
    assert not any(call[0] == "abort" for call in s3.calls)


@pytest.mark.asyncio
async def test_cancelled_part_upload_drains_before_aborting_its_exact_multipart():
    started, release = Event(), Event()

    class HeldPart(_S3):
        def upload_part(self, **kwargs):
            started.set()
            assert release.wait(5)
            return super().upload_part(**kwargs)

    s3 = HeldPart()
    body = BytesIO(b"held upload bytes")
    target = _target("published.dat", max_bytes=len(body.getvalue()), mode="write")
    task = asyncio.create_task(
        IngestStorage(s3, "datasets").publish(
            body,
            target,
            content_type="application/octet-stream",
            expected_size=len(body.getvalue()),
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not body.closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not s3.uploads and not s3.objects
    aborts = [call for call in s3.calls if call[0] == "abort"]
    assert len(aborts) == 1 and aborts[0][1]["Key"] == target.object_key


@pytest.mark.asyncio
async def test_managed_readback_uses_its_operation_budget_not_the_socket_timeout(
    monkeypatch,
):
    from yoao_assets_source import managed_original
    from yoao_assets_source_runtime.storage import SourceStorage
    from test_storage import _config, _Signer

    elapsed = [0]
    monkeypatch.setattr(
        managed_original, "time", SimpleNamespace(monotonic=lambda: elapsed[0])
    )

    class SlowHead(_S3):
        def head_object(self, **kwargs):
            response = super().head_object(**kwargs)
            elapsed[0] += 11
            return response

    body = b"bounded bytes"
    storage = SourceStorage(
        _config(), internal_client=SlowHead(), public_signer=_Signer()
    )
    written = await storage.managed.publish_stream(
        BytesIO(body),
        _target("published.dat", max_bytes=len(body), mode="write"),
        content_type="application/octet-stream",
        expected_size=len(body),
        expected_hash=None,
        image=False,
    )
    assert written.proof.sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_prepublication_failure_aborts_only_its_prepared_upload():
    s3 = _S3()
    target = _target("published.dat", max_bytes=4, mode="write")

    async def refused(_prepared):
        raise RuntimeError("publication fence refused")

    with pytest.raises(RuntimeError, match="publication fence refused"):
        await IngestStorage(s3, "datasets").publish(
            BytesIO(b"data"),
            target,
            content_type="application/octet-stream",
            expected_size=4,
            on_prepared=refused,
        )
    assert not s3.uploads and not s3.objects
    assert len([call for call in s3.calls if call[0] == "abort"]) == 1
    assert not any(call[0] == "complete" for call in s3.calls)


@pytest.mark.asyncio
async def test_ingest_receipt_persistence_gap_retains_the_actual_written_evidence():
    from yoao_assets_source.managed_original_contract import ManagedOriginalFailure
    from test_ingest_workflow import _zip_bytes

    failures = []

    class ReceiptPersistenceGap(_Data):
        async def original_written(self, creation_id, receipt):
            raise ManagedOriginalFailure("creation_evidence_mismatch")

        async def fail_original(self, creation_id, outcome, error_code, evidence):
            failures.append((creation_id, outcome, evidence))

    body = b"bytes whose completion receipt has not been persisted"
    s3 = _S3()
    result = await IngestWorkflow(
        data=ReceiptPersistenceGap(),
        storage=IngestStorage(s3, "datasets"),
        image_admission=_ImageAdmission(),
    ).upload_zip(_command(), BytesIO(_zip_bytes({"frame.bin": body})))

    assert result.added == 0 and len(result.errors) == 1
    assert len(failures) == 1
    _, outcome, evidence = failures[0]
    assert outcome == "failed"
    assert evidence["physical_settled"] is True
    assert evidence["physical_outcome"] == "written"
    stored = list(s3.objects.values())
    assert len(stored) == 1 and stored[0]["body"] == body
    assert evidence["receipt"]["etag"] == stored[0]["etag"]
    assert evidence["receipt"]["version_id"] == stored[0]["version"]
    assert not any(call[0] == "abort" for call in s3.calls)


@pytest.mark.asyncio
async def test_scan_accepts_a_32hex_etag_that_is_not_the_content_md5():
    objects = {
        "dataset/opaque.dat": b"opaque etags are not checksums",
        "dataset/good.dat": b"unrelated accepted bytes",
    }
    s3 = _S3(objects=objects)
    opaque = s3._current("datasets", "dataset/opaque.dat")
    opaque["etag"] = '"0123456789abcdef0123456789abcdef"'
    s3.pages = [
        {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(body),
                    "ETag": s3._current("datasets", key)["etag"],
                }
                for key, body in objects.items()
            ]
        }
    ]
    verified = []

    class VerifiedData(_Data):
        async def original_verified(self, creation_id, verification):
            verified.append(verification)
            await super().original_verified(creation_id, verification)

    result = await IngestWorkflow(
        data=VerifiedData(),
        storage=IngestStorage(s3, "datasets"),
        image_admission=_ImageAdmission(),
    ).scan(_command())

    assert result.added == 2 and result.error_count == 0
    assert {proof["sha256"] for proof in verified} == {
        hashlib.sha256(body).hexdigest() for body in objects.values()
    }
    assert {proof["md5"] for proof in verified} == {
        hashlib.md5(body, usedforsecurity=False).hexdigest()
        for body in objects.values()
    }
    assert not any(call[0] in {"create", "complete", "put"} for call in s3.calls)


@pytest.mark.asyncio
async def test_postcopy_size_drift_keeps_its_receipt_and_isolates_the_bad_entry():
    from yoao_assets_source.managed_original_contract import ManagedOriginalFailure

    objects = {"dataset/bad.dat": b"bad", "dataset/good.dat": b"good"}
    s3 = _S3(
        objects=objects,
        pages=[
            {
                "Contents": [
                    {"Key": "dataset/bad.dat", "Size": 4},
                    {"Key": "dataset/good.dat", "Size": 4},
                ]
            }
        ],
    )
    written, failures = {}, []

    class CopyData(_Data):
        async def allocate(self, request):
            target = await super().allocate(request)
            return target.model_copy(
                update={
                    "mode": "write",
                    "object_key": "copy/" + target.file_path,
                    "key_prefix": "copy/",
                }
            )

        async def original_written(self, creation_id, receipt):
            written[creation_id] = receipt
            await super().original_written(creation_id, receipt)

        async def fail_original(self, creation_id, outcome, error_code, evidence):
            if evidence["physical_outcome"] == "not_written" and creation_id in written:
                raise ManagedOriginalFailure("creation_evidence_mismatch")
            failures.append((creation_id, outcome, evidence))

    data = CopyData()
    result = await IngestWorkflow(
        data=data,
        storage=IngestStorage(s3, "datasets"),
        image_admission=_ImageAdmission(),
    ).scan(_command())

    assert result.added == result.linked_tasks == 1
    assert result.error_count == 1
    assert [item.file_path for item in data.items] == ["dataset/good.dat"]
    assert len(failures) == 1
    creation_id, outcome, evidence = failures[0]
    assert outcome == "failed"
    assert evidence["physical_settled"] is True
    assert evidence["physical_outcome"] == "written"
    assert evidence["receipt"] == written[creation_id]
    for key, body in objects.items():
        assert s3._current("datasets", "copy/" + key)["body"] == body
