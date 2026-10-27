"""Pure receipt and provider tests; no database or external storage is used."""

from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
import pytest

from app.config import settings
from app.services import raster_object_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
IDENTIFIER = uuid.UUID(int=144)
RLE = {"encoding": "coco_rle", "size": [4, 4], "counts": [0, 5, 11]}
RAW = json.dumps(RLE, separators=(",", ":")).encode()
SHA = hashlib.sha256(RAW).hexdigest()


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def portable(encoding="identity"):
    result = {
        "encoding": "coco_rle_ref",
        "size": [4, 4],
        "object_key": f"raster-masks/sha256/{SHA[:2]}/{SHA[2:4]}/{SHA}.json",
        "sha256": SHA,
        "runs": 3,
        "bytes": len(RAW),
    }
    if encoding == "gzip":
        result["object_key"] += ".gz"
        result["storage_encoding"] = "gzip"
    return result


def locator(identifier=IDENTIFIER, encoding="identity", bucket="rle-original"):
    return {
        "schema": 1,
        "purpose": "annotations",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "object_key": storage.key_for(identifier, encoding),
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def record(data=RAW, encoding="identity", **changes):
    values = {
        "id": IDENTIFIER,
        "created_at": CREATED,
        **storage.content(portable(encoding), data),
        "storage_ref": locator(encoding=encoding),
        **changes,
    }
    return SimpleNamespace(**values)


def error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "secret signed URL"}}, "GetObject"
    )


class Provider:
    """An in-memory provider with actual conditional requests and closeable bodies."""

    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url=endpoint())
        self.objects = {}
        self.calls = []
        self.bodies = []
        self.body_factory = io.BytesIO
        self.version = None
        self.head_failure = None
        self.get_failure = None
        self.put_failure = None
        self.delete_failure = None
        self.after_get = None

    def metadata(self, data):
        result = {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }
        if self.version is not None:
            result["VersionId"] = self.version
        return result

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if self.head_failure:
            raise self.head_failure
        data = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if data is None:
            raise error("NoSuchKey")
        return self.metadata(data)

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.get_failure:
            raise self.get_failure
        data = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if data is None:
            raise error("NoSuchKey")
        metadata = self.metadata(data)
        if kwargs["IfMatch"] != metadata["ETag"] or kwargs.get("VersionId") != (
            None if self.version == "null" else self.version
        ):
            raise error("PreconditionFailed")
        body = self.body_factory(data)
        self.bodies.append(body)
        if self.after_get:
            self.after_get()
        return {**metadata, "Body": body}

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.put_failure:
            raise self.put_failure
        assert kwargs["IfNoneMatch"] == "*"
        target = (kwargs["Bucket"], kwargs["Key"])
        if target in self.objects:
            raise error("PreconditionFailed")
        assert kwargs["ContentLength"] == len(kwargs["Body"])
        self.objects[target] = kwargs["Body"]

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.delete_failure:
            raise self.delete_failure
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


@pytest.fixture
def provider(monkeypatch):
    client = Provider()
    monkeypatch.setattr(
        storage, "storage_service", SimpleNamespace(bucket="rle-current", client=client)
    )
    return client


def seed(provider, row=None, data=RAW):
    row = row or record()
    provider.objects[(row.storage_ref["bucket"], row.storage_ref["object_key"])] = data
    return row


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_content_keeps_portable_identity_separate_from_uuid_locator(provider, encoding):
    data = RAW if encoding == "identity" else gzip.compress(RAW, mtime=1)
    row = record(data, encoding)
    assert storage.reference(row) == portable(encoding)
    assert row.logical_key != row.storage_ref["object_key"]
    seed(provider, row, data)
    assert storage.read(provider, storage.snapshot(row)) == RLE
    assert provider.bodies[-1].closed
    assert row.stored_sha256 == hashlib.sha256(data).hexdigest()
    assert row.canonical_sha256 == SHA


def test_snapshot_and_owner_return_no_mutable_locator_alias(provider):
    row = record()
    receipt, owner = storage.snapshot(row), storage.resolve(row)
    receipt.storage_ref["bucket"] = "trap"
    owner.ref["bucket"] = "trap"
    assert receipt.storage_ref == locator() == owner.ref
    assert storage.same(row, receipt)
    row.storage_ref = locator(bucket="rle-other")
    assert not storage.same(row, receipt)
    with pytest.raises(AttributeError):
        receipt.logical_key = "forged"


@pytest.mark.parametrize("bad", [None, {}, [], True, "text"])
def test_missing_or_invalid_locator_is_rejected_before_io(provider, bad):
    with pytest.raises(storage.RasterObjectStorageError):
        storage.read(provider, record(storage_ref=bad))
    assert provider.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", "1"),
        ("schema", 1.0),
        ("purpose", "export"),
        ("location_id", str(uuid.uuid4())),
        ("config_revision", True),
        ("binding_generation", 1.0),
        ("bucket", "bad/path"),
        ("bucket", "bad bucket"),
        ("bucket", "bad\\bucket"),
        ("endpoint_url", "http://user:secret@host.invalid"),
        ("endpoint_url", "http://host.invalid/path"),
        ("endpoint_url", "http://host.invalid/"),
        ("endpoint_url", "http://host.invalid:99999"),
        ("object_key", portable()["object_key"]),
        ("object_key", "raster-masks/objects/../x.json"),
        ("object_key", storage.key_for(uuid.uuid4())),
        ("storage_identity", "A" * 64),
        ("credentials", "secret"),
    ],
)
def test_locator_is_strict_and_uuid_bound(provider, field, value):
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, record(storage_ref={**locator(), field: value}))
    assert provider.calls == []
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_sha256": "A" * 64},
        {"canonical_sha256": True},
        {"stored_sha256": "b" * 64},
        {"canonical_size_bytes": True},
        {"stored_size_bytes": 1.0},
        {"canonical_size_bytes": storage.MAX_BYTES + 1},
        {"stored_size_bytes": storage.MAX_COMPRESSED_BYTES + 1},
        {"height": True},
        {"height": 8193},
        {"width": 0},
        {"runs": 0},
        {"runs": 1000001},
        {"logical_key": "raster-masks/sha256/wrong.json"},
        {"storage_encoding": "other"},
        {"created_at": CREATED.replace(tzinfo=None)},
    ],
)
def test_invalid_receipt_never_reaches_provider(provider, changes):
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, record(**changes))
    assert caught.value.code == "raster_object_storage_invalid_receipt"
    assert provider.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "b" * 64),
        ("bytes", True),
        ("runs", 3.0),
        ("size", [True, 4]),
        ("object_key", "raster-masks/sha256/invalid.json"),
        ("storage_encoding", "gzip"),
        ("encoding", "other"),
        ("arbitrary", 1),
    ],
)
def test_portable_metadata_must_exactly_match_canonical_bytes(field, value):
    with pytest.raises(storage.RasterObjectStorageError):
        storage.content({**portable(), field: value}, RAW)


@pytest.mark.parametrize(
    "data",
    [
        RAW + b" ",
        RAW.replace(b'"size"', b'"unexpected"'),
        b"[]",
        b"null",
        b"{}",
        b"\xff",
        RAW.replace(b"[0,5,11]", b"[0,true,15]"),
    ],
)
def test_noncanonical_or_invalid_rle_is_rejected(data):
    with pytest.raises(storage.RasterObjectStorageError):
        storage.content(portable(), data)


@pytest.mark.parametrize(
    "data",
    [
        gzip.compress(RAW, mtime=1)[:-2],
        gzip.compress(RAW, mtime=1) + b"trailing",
        gzip.compress(RAW, mtime=1) * 2,
        gzip.compress(b"x" * (storage.MAX_BYTES + 1), mtime=0),
    ],
)
def test_invalid_gzip_never_becomes_a_receipt(data):
    with pytest.raises(storage.RasterObjectStorageError):
        storage.content(portable("gzip"), data)


def test_gzip_original_stored_hash_is_preserved_not_recompressed(provider):
    original = gzip.compress(RAW, mtime=1)
    replacement = gzip.compress(RAW, mtime=2)
    row = record(original, "gzip")
    seed(provider, row, original)
    assert storage.read(provider, row) == RLE
    seed(provider, row, replacement)
    with pytest.raises(storage.RasterObjectStorageError, match="字节"):
        storage.read(provider, row)
    assert (
        provider.objects[(row.storage_ref["bucket"], row.storage_ref["object_key"])]
        == replacement
    )


@pytest.mark.parametrize("version", [None, "null", "real-version"])
def test_full_saved_bucket_read_is_conditional_and_closes(provider, version):
    row = seed(provider)
    provider.version = version
    provider.objects[("rle-current", row.storage_ref["object_key"])] = b"trap"
    assert storage.read(provider, row) == RLE
    get = next(params for action, params in provider.calls if action == "get")
    assert (
        get["Bucket"] == "rle-original" and get["Key"] == row.storage_ref["object_key"]
    )
    assert get["IfMatch"] == provider.metadata(RAW)["ETag"]
    assert get.get("VersionId") == (None if version == "null" else version)
    assert provider.bodies[-1].closed
    assert len([1 for action, _ in provider.calls if action == "head"]) == 2


def test_client_endpoint_mismatch_is_zero_io(provider):
    provider.meta.endpoint_url = "https://other.invalid"
    with pytest.raises(storage.RasterObjectStorageError):
        storage.read(provider, record())
    assert provider.calls == []


@pytest.mark.parametrize("data", [RAW[:-1], RAW + b"x", b"x" * len(RAW)])
def test_corrupt_bytes_are_retained_and_never_deleted(provider, data):
    row = seed(provider, data=data)
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.delete(provider, row)
    assert caught.value.code == "raster_object_storage_integrity_mismatch"
    assert not any(action == "delete" for action, _ in provider.calls)
    assert provider.objects[("rle-original", row.storage_ref["object_key"])] == data
    assert all(body.closed for body in provider.bodies)


def test_post_close_replacement_is_detected(provider):
    row = seed(provider)
    provider.after_get = lambda: seed(provider, row, b"x" * len(RAW))
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, row)
    assert caught.value.code == "raster_object_storage_integrity_mismatch"
    assert provider.bodies[-1].closed


@pytest.mark.parametrize("data", [RAW[:-1], RAW + b"x"])
def test_stream_length_mismatch_is_detected_even_with_consistent_headers(
    provider, data
):
    class ChangedBody(io.BytesIO):
        def __init__(self, ignored):
            super().__init__(data)

    seed(provider)
    provider.body_factory = ChangedBody
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, record())
    assert caught.value.code == "raster_object_storage_integrity_mismatch"
    assert provider.bodies[-1].closed


@pytest.mark.parametrize("version", ["", True, 1, "bad\nversion"])
def test_invalid_version_is_rejected_before_get(provider, version):
    seed(provider)
    provider.version = version
    with pytest.raises(storage.RasterObjectStorageError):
        storage.read(provider, record())
    assert not any(action == "get" for action, _ in provider.calls)


@pytest.mark.parametrize(
    "code,expected,status",
    [
        ("NoSuchKey", "missing", 404),
        ("412", "integrity_mismatch", 409),
        ("403", "unavailable", 503),
        ("500", "unavailable", 503),
    ],
)
def test_provider_failures_use_safe_fixed_codes(provider, code, expected, status):
    seed(provider)
    provider.get_failure = error(code)
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, record())
    assert caught.value.code == "raster_object_storage_" + expected
    assert caught.value.status_code == status and "secret" not in str(caught.value)


def test_missing_is_idempotent_only_when_explicitly_allowed(provider):
    assert storage.read(provider, record(), missing_ok=True) is None
    storage.delete(provider, record())
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.delete(provider, record(), missing_ok=False)
    assert caught.value.status_code == 404
    assert not any(action == "delete" for action, _ in provider.calls)


def test_unknown_close_is_unsettled_even_when_it_looks_like_missing(provider):
    class UnknownClose(io.BytesIO):
        def close(self):
            first = not self.closed
            super().close()
            if first:
                raise error("NoSuchKey")

    seed(provider)
    provider.body_factory = UnknownClose
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, record(), missing_ok=True)
    assert caught.value.code == "raster_object_storage_unavailable"
    assert caught.value.storage_operation_unsettled is True
    assert provider.bodies[-1].closed


def test_deadline_after_head_prevents_get(provider, monkeypatch):
    seed(provider)
    ticks = iter([0, storage.VERIFY_SECONDS + 1])
    monkeypatch.setattr(storage.time, "monotonic", lambda: next(ticks))
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        storage.read(provider, record())
    assert caught.value.code == "raster_object_storage_budget"
    assert not any(action == "get" for action, _ in provider.calls)


def test_bounded_chunks_reach_eof_and_close(provider, monkeypatch):
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 5
            return super().read(size)

    seed(provider)
    provider.body_factory = Bounded
    monkeypatch.setattr(storage, "CHUNK_BYTES", 5)
    assert storage.read(provider, record()) == RLE
    assert provider.bodies[-1].closed


@pytest.mark.parametrize("existing", [False, True])
def test_conditional_create_and_412_both_require_complete_readback(provider, existing):
    row = record()
    if existing:
        seed(provider, row)
    storage.write(provider, row, RAW)
    assert provider.bodies[-1].closed
    assert [action for action, _ in provider.calls] == ["put", "head", "get", "head"]
    assert provider.objects[("rle-original", row.storage_ref["object_key"])] == RAW
    assert not any(action == "delete" for action, _ in provider.calls)


def test_preexisting_corrupt_uuid_candidate_is_never_overwritten(provider):
    row = seed(provider, data=b"x" * len(RAW))
    with pytest.raises(storage.RasterObjectStorageError):
        storage.write(provider, row, RAW)
    assert provider.objects[
        ("rle-original", row.storage_ref["object_key"])
    ] == b"x" * len(RAW)
    assert not any(action == "delete" for action, _ in provider.calls)


def test_local_write_mismatch_is_zero_io(provider):
    with pytest.raises(storage.RasterObjectStorageError):
        storage.write(provider, record(), RAW + b" ")
    assert provider.calls == []


@pytest.mark.parametrize(
    "failure", [error("409"), error("AccessDenied"), RuntimeError("secret")]
)
def test_write_failure_never_compensates_or_reads_another_bucket(provider, failure):
    provider.put_failure = failure
    with pytest.raises(storage.RasterObjectStorageError):
        storage.write(provider, record(), RAW)
    assert [action for action, _ in provider.calls] == ["put"]


def test_delete_verifies_and_removes_only_saved_uuid_not_current_trap(provider):
    row = seed(provider)
    trap = ("rle-current", row.storage_ref["object_key"])
    provider.objects[trap] = b"trap"
    storage.delete(provider, row)
    assert provider.objects == {trap: b"trap"}
    assert [action for action, _ in provider.calls] == ["head", "get", "head", "delete"]


async def test_allocate_uses_shared_target_without_mutating_saved_candidate(
    provider, monkeypatch
):
    saved = {**locator(bucket="rle-current"), "schema": 2, "key_prefix": ""}
    allocate = AsyncMock(return_value=saved)
    monkeypatch.setattr(storage.annotations_storage, "allocate", allocate)
    db, row = object(), record()
    assert await storage.allocate(db, row) == saved
    assert row.storage_ref == locator()
    allocate.assert_awaited_once_with(db, storage.key_for(row.id, row.storage_encoding))
    assert provider.calls == []
    allocate.side_effect = ValueError("private target metadata")
    with pytest.raises(storage.RasterObjectStorageError) as caught:
        await storage.allocate(db, row)
    assert caught.value.code == "raster_object_storage_binding_unproven"


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_new_candidate_never_reuses_physical_key_for_same_logical_content(
    provider, encoding
):
    data = RAW if encoding == "identity" else gzip.compress(RAW, mtime=0)
    first = record(data, encoding)
    identifier = uuid.uuid4()
    second = record(
        data, encoding, id=identifier, storage_ref=locator(identifier, encoding)
    )
    assert first.logical_key == second.logical_key
    assert storage.resolve(first).object_key != storage.resolve(second).object_key


def test_storage_does_not_choose_lifecycle_state(provider):
    for state in ("pending", "ready", "retired"):
        row = seed(provider, record(state=state))
        assert storage.read(provider, row) == RLE


def test_reference_and_content_copies_cannot_mutate_original():
    ref = portable()
    original = deepcopy(ref)
    storage.content(ref, RAW)
    assert ref == original
