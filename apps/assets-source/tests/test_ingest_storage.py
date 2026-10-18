from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from threading import Event

from PIL import Image
import pytest
from aap_assets_images.contract import ObjectIdentity

from aap_assets_source.ingest_contract import IngestFailure
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
        self.calls.append((Bucket, Prefix))
        yield from self.pages


class _S3:
    def __init__(self, *, pages=(), size=0, etag='"not-md5"', image=b"") -> None:
        self.pages = pages
        self.size = size
        self.etag = etag
        self.image = image
        self.calls = []
        self.part_sizes = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self.pages, self.calls)

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        return {"ContentLength": self.size, "ETag": self.etag}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"Body": BytesIO(self.image)}

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self.part_sizes.append(len(kwargs["Body"]))
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete", kwargs))

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort", kwargs))

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))


class _TrackingReader(BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


@pytest.mark.asyncio
async def test_scan_streams_more_than_one_s3_page_and_never_copies_objects() -> None:
    objects = [
        {"Key": f"dataset/item-{index:04}.txt", "Size": 1, "ETag": '"not-md5"'}
        for index in range(1001)
    ]
    s3 = _S3(
        pages=[{"Contents": objects[:1000]}, {"Contents": objects[1000:]}],
        size=1,
    )
    storage = IngestStorage(s3, "datasets")

    receipt = await IngestWorkflow(
        data=_Data(), storage=storage, image_admission=_ImageAdmission()
    ).scan(_command())

    assert receipt.added == 1001
    assert receipt.linked_tasks == 1001
    assert s3.calls[0] == ("datasets", "dataset/")
    assert [
        call[0] for call in s3.calls if isinstance(call, tuple) and call[0] == "head"
    ] == ["head"] * 1001
    assert not any(
        isinstance(call, tuple) and call[0] in {"put", "create", "complete", "abort"}
        for call in s3.calls
    )


@pytest.mark.asyncio
async def test_legacy_image_observation_is_conditional_and_uses_valid_md5_etag() -> (
    None
):
    image_stream = BytesIO()
    Image.new("RGB", (19, 7), color="red").save(image_stream, format="PNG")
    image_bytes = image_stream.getvalue()
    etag = '"' + hashlib.md5(image_bytes, usedforsecurity=False).hexdigest() + '"'
    s3 = _S3(size=len(image_bytes), etag=etag, image=image_bytes)

    facts = await IngestStorage(s3, "datasets").observe_legacy(
        "dataset/images/pic.png",
        prefix="dataset/",
        etag=etag,
        size=len(image_bytes),
        image=True,
    )

    assert (
        facts.content_hash
        == hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
    )
    assert (facts.width, facts.height) == (19, 7)
    assert facts.object_identity == ObjectIdentity(
        etag=etag, byte_size=len(image_bytes)
    )
    assert s3.calls[0][1]["IfMatch"] == etag
    assert s3.calls[1][1]["Range"] == "bytes=0-262143"
    assert s3.calls[1][1]["IfMatch"] == etag


@pytest.mark.asyncio
async def test_zip_image_publisher_heads_the_final_object_for_sidecar_identity():
    image_stream = BytesIO()
    Image.new("RGB", (13, 9), color="blue").save(image_stream, format="PNG")
    image_bytes = image_stream.getvalue()
    s3 = _S3(size=len(image_bytes), etag='"published-final"')

    facts = await IngestStorage(s3, "datasets").publish(
        BytesIO(image_bytes),
        key="dataset-sources/00000000-0000-0000-0000-000000000001/"
        "00000000-0000-0000-0000-000000000002/"
        "00000000-0000-0000-0000-000000000003/photo.png",
        content_type="image/png",
        expected_size=len(image_bytes),
        expected_hash=hashlib.md5(image_bytes, usedforsecurity=False).hexdigest(),
        image=True,
    )

    assert facts.object_identity == ObjectIdentity(
        etag='"published-final"', byte_size=len(image_bytes)
    )
    assert [call[0] for call in s3.calls] == ["put", "head"]


@pytest.mark.asyncio
async def test_changed_legacy_size_is_a_per_file_failure() -> None:
    s3 = _S3(size=12, etag='"etag"')
    with pytest.raises(IngestEntryFailure, match="source_changed"):
        await IngestStorage(s3, "datasets").observe_legacy(
            "dataset/file.bin",
            prefix="dataset/",
            etag='"etag"',
            size=11,
            image=False,
        )


@pytest.mark.asyncio
async def test_multipart_publication_uses_bounded_parts_and_conditional_completion() -> (
    None
):
    content = b"a" * (8 * 1024 * 1024 + 1)
    source = _TrackingReader(content)
    s3 = _S3()
    facts = await IngestStorage(s3, "datasets").publish(
        source,
        key="dataset-sources/00000000-0000-0000-0000-000000000001/"
        "00000000-0000-0000-0000-000000000002/"
        "00000000-0000-0000-0000-000000000003/path/file.bin",
        content_type="application/octet-stream",
        expected_size=len(content),
        expected_hash=hashlib.md5(content, usedforsecurity=False).hexdigest(),
    )

    assert s3.part_sizes == [8 * 1024 * 1024, 1]
    assert max(source.read_sizes) <= 8 * 1024 * 1024
    complete = next(call[1] for call in s3.calls if call[0] == "complete")
    assert complete["IfNoneMatch"] == "*"
    assert facts.file_size == len(content)
    assert facts.content_hash == hashlib.md5(content, usedforsecurity=False).hexdigest()
    assert not any(call[0] == "delete" for call in s3.calls)


@pytest.mark.asyncio
async def test_storage_list_failure_is_command_failure() -> None:
    class BrokenS3:
        def get_paginator(self, _name):
            raise RuntimeError("network detail must not escape")

    with pytest.raises(IngestFailure) as error:
        async for _item in IngestStorage(BrokenS3(), "datasets").iter_objects(
            "dataset/"
        ):
            pass
    assert error.value.code == "storage_unavailable"


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_blocking_storage_work() -> None:
    started, release = Event(), Event()

    def blocking_call() -> None:
        started.set()
        release.wait(timeout=2)

    task = asyncio.create_task(run_in_thread(blocking_call))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
