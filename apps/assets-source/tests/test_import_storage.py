from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_images.contract import ObjectIdentity
from aap_assets_source.import_contract import ImportEntry, ImportSourceObject
from aap_assets_source.connectors import SourceChangedError
from aap_assets_source_import.storage import (
    DestinationStorage,
    DestinationWriteError,
    _multipart_part_bytes,
)


class _ShortRead(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 512 * 1024))


class _Destination:
    def __init__(self):
        self.parts: list[tuple[int, bytes]] = []
        self.completed = None
        self.aborted = False
        self.created = False
        self.empty = None
        self.complete_result = {"ETag": '"multipart-etag"', "VersionId": "v1"}
        self.empty_result = {"ETag": '"single-etag"', "VersionId": "v2"}
        self.head_calls = []
        self.get_calls = []

    def create_multipart_upload(self, **kwargs):
        self.created = True
        return {"UploadId": "upload"}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        self.parts.append((PartNumber, Body))
        return {"ETag": f"part-{PartNumber}"}

    def complete_multipart_upload(self, **kwargs):
        self.completed = kwargs
        return self.complete_result

    def abort_multipart_upload(self, **kwargs):
        self.aborted = True

    def put_object(self, **kwargs):
        self.empty = kwargs
        return self.empty_result

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        raise AssertionError("destination writes must not issue HEAD")

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        raise AssertionError("destination writes must not issue GET")


def _config():
    return SimpleNamespace(
        datasets_bucket="datasets",
        part_bytes=8 * 1024 * 1024,
        image_header_bytes=256 * 1024,
        s3_read_timeout_seconds=10,
    )


def _entry(size: int, *, file_type: str = "other") -> ImportEntry:
    item_id = uuid4()
    publication_id = uuid4()
    dataset_id = uuid4()
    return ImportEntry(
        job_id=uuid4(),
        lease_token=uuid4(),
        index=0,
        item_id=item_id,
        publication_id=publication_id,
        file_name="source.bin" if file_type == "other" else "source.jpg",
        file_path=f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/source.bin"
        if file_type == "other"
        else f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/source.jpg",
        file_type=file_type,
        source=ImportSourceObject(
            relpath="source.bin", size=size, mtime=None, etag="etag"
        ),
    )


@pytest.mark.asyncio
async def test_streaming_aggregates_short_reads_and_hashes_exact_bytes(
    monkeypatch,
) -> None:
    data = b"x" * (8 * 1024 * 1024 + 123)
    client = _Destination()
    storage = DestinationStorage(_config(), client=client)
    monkeypatch.setattr(
        "aap_assets_source_import.storage._image_dimensions",
        lambda head: pytest.fail("non-image dimensions must not be read"),
    )

    size, digest, dimensions, identity = await storage.transfer(
        _ShortRead(data),
        _entry(len(data)),
        byte_budget=len(data),
        before_io=lambda: None,
        on_bytes=lambda amount: None,
    )

    assert size == len(data)
    assert digest == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert dimensions is None
    assert identity is None
    assert len(client.parts) == 2
    assert len(client.parts[0][1]) >= 5 * 1024 * 1024
    assert len(client.parts[1][1]) == 123
    assert client.completed is not None
    assert not client.aborted


def test_part_size_accounts_for_s3_ten_thousand_part_limit() -> None:
    byte_budget = 200 * 1024 * 1024 * 1024
    part_size = _multipart_part_bytes(byte_budget, 8 * 1024 * 1024)
    assert part_size > 8 * 1024 * 1024
    assert (byte_budget + part_size - 1) // part_size <= 10_000
    fixed_capacity = 64 * 1024 * 1024 * 10_000
    assert _multipart_part_bytes(fixed_capacity, 8 * 1024 * 1024) == 64 * 1024 * 1024
    with pytest.raises(DestinationWriteError, match="supported multipart size"):
        _multipart_part_bytes(fixed_capacity + 1, 8 * 1024 * 1024)


@pytest.mark.asyncio
async def test_transfer_rejects_size_larger_than_budget_before_destination_write() -> (
    None
):
    client = _Destination()
    storage = DestinationStorage(_config(), client=client)

    with pytest.raises(SourceChangedError):
        await storage.transfer(
            io.BytesIO(b"data"),
            _entry(4),
            byte_budget=3,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
        )
    assert not client.created


@pytest.mark.asyncio
async def test_transfer_rejects_oversized_object_before_multipart_start() -> None:
    client = _Destination()
    storage = DestinationStorage(_config(), client=client)
    fixed_capacity = 64 * 1024 * 1024 * 10_000

    with pytest.raises(DestinationWriteError, match="supported multipart size"):
        await storage.transfer(
            io.BytesIO(b""),
            _entry(fixed_capacity + 1),
            byte_budget=fixed_capacity + 1,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
        )
    assert not client.created


@pytest.mark.asyncio
async def test_empty_source_uses_bounded_put_object_without_multipart() -> None:
    client = _Destination()
    storage = DestinationStorage(_config(), client=client)

    size, digest, dimensions, identity = await storage.transfer(
        io.BytesIO(b""),
        _entry(0),
        byte_budget=0,
        before_io=lambda: None,
        on_bytes=lambda amount: None,
    )

    assert size == 0
    assert digest == hashlib.md5(b"", usedforsecurity=False).hexdigest()
    assert dimensions is None
    assert identity is None
    assert client.empty["Bucket"] == "datasets"
    assert client.empty["Body"] == b""
    assert not client.created


@pytest.mark.asyncio
async def test_image_identity_uses_multipart_write_receipt_without_destination_read(
    monkeypatch,
) -> None:
    client = _Destination()
    storage = DestinationStorage(_config(), client=client)
    monkeypatch.setattr(
        "aap_assets_source_import.storage._image_dimensions", lambda _head: None
    )

    size, _digest, _dimensions, identity = await storage.transfer(
        io.BytesIO(b"data"),
        _entry(4, file_type="image"),
        byte_budget=4,
        before_io=lambda: None,
        on_bytes=lambda amount: None,
    )

    assert size == 4
    assert identity == ObjectIdentity(
        etag="multipart-etag", byte_size=4, version_id="v1"
    )
    assert client.completed is not None
    assert client.head_calls == []
    assert client.get_calls == []


@pytest.mark.asyncio
async def test_empty_image_identity_uses_single_put_receipt_without_destination_read(
    monkeypatch,
) -> None:
    client = _Destination()
    storage = DestinationStorage(_config(), client=client)
    monkeypatch.setattr(
        "aap_assets_source_import.storage._image_dimensions", lambda _head: None
    )

    size, _digest, _dimensions, identity = await storage.transfer(
        io.BytesIO(b""),
        _entry(0, file_type="image"),
        byte_budget=0,
        before_io=lambda: None,
        on_bytes=lambda amount: None,
    )

    assert size == 0
    assert identity == ObjectIdentity(etag="single-etag", byte_size=0, version_id="v2")
    assert client.empty is not None
    assert client.head_calls == []
    assert client.get_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        {"ETag": None},
        {"ETag": '"good"', "VersionId": ""},
        {"ETag": "bad\x7f"},
    ],
)
async def test_image_transfer_rejects_missing_or_malformed_multipart_receipt(
    monkeypatch, receipt
) -> None:
    client = _Destination()
    client.complete_result = receipt
    storage = DestinationStorage(_config(), client=client)
    monkeypatch.setattr(
        "aap_assets_source_import.storage._image_dimensions", lambda _head: None
    )

    with pytest.raises(DestinationWriteError, match="invalid write receipt"):
        await storage.transfer(
            io.BytesIO(b"data"),
            _entry(4, file_type="image"),
            byte_budget=4,
            before_io=lambda: None,
            on_bytes=lambda amount: None,
        )

    assert client.completed is not None
    assert not client.aborted
    assert client.head_calls == []
    assert client.get_calls == []
