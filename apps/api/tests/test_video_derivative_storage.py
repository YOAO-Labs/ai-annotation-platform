"""Pure provider tests for fixed video outputs and complete content verification."""

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
import pytest

from app.config import settings
from app.services import video_derivative_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

IDENTIFIER = uuid.UUID(int=1823)


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(identifier=IDENTIFIER, bucket="saved-video-cache"):
    return {
        "schema": 1,
        "purpose": "media-cache",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "prefix": storage.prefix_for(identifier),
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def output(**changes):
    return SimpleNamespace(
        **{
            "id": IDENTIFIER,
            "storage_ref": reference(),
            "inventory": None,
            "result": None,
            **changes,
        }
    )


def provider_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "private provider detail"}}, "GetObject"
    )


def receipt(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


class Provider:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url=endpoint())
        self.objects = {}
        self.calls = []
        self.bodies = []
        self.body_factory = io.BytesIO
        self.corrupt_key = None
        self.after_get = None
        self.list_extra = None
        self.ignore_delete = False
        self.version = "version-1"
        self.closed = False

    def metadata(self, data):
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
            "VersionId": self.version,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        data = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if data is None:
            raise provider_error("NoSuchKey")
        return self.metadata(data)

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        data = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        meta = self.metadata(data)
        assert kwargs["IfMatch"] == meta["ETag"]
        assert kwargs.get("VersionId") == (
            None if self.version in {None, "null"} else self.version
        )
        body = self.body_factory(
            b"X" * len(data) if kwargs["Key"] == self.corrupt_key else data
        )
        self.bodies.append(body)
        if self.after_get:
            self.after_get(kwargs)
        return {**meta, "Body": body}

    def put_object(self, **kwargs):
        self.calls.append(("put", {k: v for k, v in kwargs.items() if k != "Body"}))
        pair = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") != "*" or pair in self.objects:
            raise provider_error("PreconditionFailed")
        data = kwargs["Body"]
        data = data.read() if hasattr(data, "read") else data
        assert len(data) == kwargs["ContentLength"]
        self.objects[pair] = data
        return self.metadata(data)

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list", kwargs))
        rows = [
            {"Key": key, "Size": len(data)}
            for (bucket, key), data in sorted(self.objects.items())
            if bucket == kwargs["Bucket"] and key.startswith(kwargs["Prefix"])
        ]
        if self.list_extra is not None:
            rows.append(self.list_extra)
        return {
            "Contents": rows,
            "IsTruncated": False,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if not self.ignore_delete:
            self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)

    def close(self):
        self.closed = True


class Storage:
    def __init__(self, client):
        self.client = client

    def generate_download_url(self, key, **kwargs):
        self.client.calls.append(("sign", {"Key": key, **kwargs}))
        return f"{endpoint()}/{kwargs['bucket']}/{key}?expires={kwargs['expires_in']}"


@pytest.fixture
def provider(monkeypatch):
    result = Provider()
    monkeypatch.setattr(
        storage,
        "storage_service",
        SimpleNamespace(client=result, media_cache_bucket="new-current-cache"),
    )
    return result


@pytest.fixture
def generated(tmp_path, provider):
    path = tmp_path / "frame.webp"
    path.write_bytes(b"verified video frame")
    row = output()
    owner = storage.resolve(row)
    frame = storage.upload_file(
        Storage(provider), row, path, "frame.webp", "image/webp"
    )
    result = {"schema": 1, "frame_index": 23, "frame": frame}
    metadata = storage.write_result(Storage(provider), row, result)
    row.result = deepcopy(result)
    row.inventory = {
        "schema": 1,
        "objects": {
            owner.key("frame.webp"): frame,
            owner.key("metadata.json"): metadata,
        },
    }
    return row, path


def test_saved_owner_ignores_current_bucket_and_returns_copies(provider):
    row = output()
    owner = storage.resolve(row)
    assert owner.bucket == "saved-video-cache"
    assert owner.prefix == f"video-derivatives/objects/{IDENTIFIER}/"
    assert not owner.prefix.startswith("videos/")
    assert storage.resolve(vars(row)).ref == owner.ref
    ref = owner.ref
    ref["bucket"] = "tampered"
    row.storage_ref["bucket"] = "also-tampered"
    assert owner.ref["bucket"] == "saved-video-cache"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("config_revision", True),
        ("binding_generation", 2),
        ("purpose", "training"),
        ("location_id", str(uuid.UUID(int=1))),
        ("endpoint_url", "https://user:secret@invalid"),
        ("endpoint_url", "https://invalid/path"),
        ("endpoint_url", "http://invalid/"),
        ("endpoint_url", "file:///tmp/a"),
        ("bucket", "../../other"),
        ("bucket", "space bucket"),
        ("storage_identity", "f" * 64),
        ("prefix", "videos/old/frames/"),
        ("prefix", storage.prefix_for(uuid.UUID(int=1))),
    ],
)
def test_resolve_rejects_tampered_owner_before_io(provider, field, value):
    row = output()
    row.storage_ref[field] = value
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.resolve(row)
    assert provider.calls == []


@pytest.mark.parametrize("value", [None, {}, {**reference(), "unknown": 1}])
def test_unknown_legacy_owner_never_resolves(provider, value):
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.resolve(output(storage_ref=value))
    assert provider.calls == []


@pytest.mark.parametrize(
    "value", [None, True, 123, "", "not-a-uuid", str(IDENTIFIER).upper()]
)
def test_invalid_owner_uuid_never_selects_a_prefix(provider, value):
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.prefix_for(value)
    assert provider.calls == []


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../outside",
        "/absolute",
        "a//b",
        "a/./b",
        "a/%2e%2e/b",
        "a?secret",
        "a\\b",
        "a#x",
        "unknown.bin",
        "frames/1.webp",
    ],
)
def test_owner_only_accepts_exact_known_relative_names(provider, key):
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.resolve(output()).key(key)


def test_runtime_and_physical_client_endpoints_must_match(provider):
    wrong = Storage(
        SimpleNamespace(meta=SimpleNamespace(endpoint_url="https://different.invalid"))
    )
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.delete_output(wrong, output())
    provider.meta.endpoint_url = "https://different.invalid"
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.resolve(output())
    assert provider.calls == []


async def test_allocate_only_reads_registered_binding(provider):
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    binding = SimpleNamespace(
        purpose="media-cache",
        location_id=LEGACY_LOCATION_ID,
        generation=1,
        bucket="new-current-cache",
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=location), scalar=AsyncMock(return_value=binding)
    )
    value = await storage.allocate(db, IDENTIFIER)
    assert value["prefix"] == storage.prefix_for(IDENTIFIER)
    assert value["bucket"] == binding.bucket
    assert not hasattr(db, "commit")
    assert provider.calls == []


@pytest.mark.parametrize("missing", ["location", "binding"])
async def test_allocate_never_bootstraps_missing_registration(provider, missing):
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=None if missing == "location" else location),
        scalar=AsyncMock(return_value=None),
    )
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        await storage.allocate(db, IDENTIFIER)
    assert caught.value.code.endswith("binding_unproven")
    assert provider.calls == []


def test_every_upload_is_exclusive_and_fully_read_back(provider, generated):
    row, path = generated
    assert storage.file_receipt(path) == receipt(path.read_bytes())
    result = storage.verify_inventory(Storage(provider), row, row.inventory)
    assert result == row.inventory
    result["objects"].clear()
    assert len(row.inventory["objects"]) == 2
    puts = [kwargs for kind, kwargs in provider.calls if kind == "put"]
    assert len(puts) == 2
    assert all(kwargs["IfNoneMatch"] == "*" for kwargs in puts)
    assert puts[-1]["Key"].endswith("/metadata.json")
    assert len([1 for kind, _ in provider.calls if kind == "get"]) == 4
    assert all(body.closed for body in provider.bodies)
    assert all(
        kwargs.get("Bucket", kwargs.get("bucket")) == "saved-video-cache"
        for _, kwargs in provider.calls
    )


def test_large_video_verification_never_uses_collecting_read(
    provider, generated, monkeypatch
):
    row, _ = generated
    key = storage.resolve(row).key("frame.webp")
    original = storage._read
    collects = []

    def checked(*args, **kwargs):
        collects.append(kwargs.get("collect", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "_read", checked)
    assert (
        storage.verify_file(Storage(provider), row, key)
        == row.inventory["objects"][key]
    )
    storage.verify_inventory(Storage(provider), row, row.inventory)
    assert collects == [False, False, False]


def test_result_is_canonical_json_and_read_bytes_use_saved_receipt(provider, generated):
    row, _ = generated
    owner = storage.resolve(row)
    data = storage.read_bytes(Storage(provider), row, owner.key("metadata.json"))
    assert json.loads(data) == row.result
    assert (
        data
        == json.dumps(
            row.result, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    assert (
        storage.verify_object(Storage(provider), vars(row), owner.key("frame.webp"))
        == b"verified video frame"
    )


def test_read_bytes_rejects_unknown_or_foreign_key_before_io(provider, generated):
    row, _ = generated
    before = len(provider.calls)
    for key in [
        storage.prefix_for(uuid.UUID(int=2)) + "frame.webp",
        storage.resolve(row).key("chunk.mp4"),
    ]:
        with pytest.raises(storage.VideoDerivativeStorageError):
            storage.read_bytes(Storage(provider), row, key)
    assert len(provider.calls) == before


def test_same_size_corruption_is_rejected_and_body_closed(provider, generated):
    row, _ = generated
    key = storage.resolve(row).key("frame.webp")
    provider.corrupt_key = key
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        storage.verify_file(Storage(provider), row, key)
    assert caught.value.code.endswith("integrity_mismatch")
    assert all(body.closed for body in provider.bodies)


def test_source_changed_during_readback_is_rejected(provider, generated):
    row, _ = generated
    provider.after_get = lambda _: setattr(provider, "version", "version-2")
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert all(body.closed for body in provider.bodies)


@pytest.mark.parametrize("version", [None, "null"])
def test_unversioned_provider_still_uses_conditional_get_and_final_head(
    provider, generated, version
):
    row, _ = generated
    provider.version = version
    storage.verify_file(Storage(provider), row, storage.resolve(row).key("frame.webp"))
    last_get = [kwargs for kind, kwargs in provider.calls if kind == "get"][-1]
    assert "IfMatch" in last_get and "VersionId" not in last_get


@pytest.mark.parametrize("change", ["status", "range", "etag", "size", "version"])
def test_bad_get_metadata_is_rejected_and_closed(provider, generated, change):
    row, _ = generated
    original = provider.get_object

    def broken(**kwargs):
        response = original(**kwargs)
        if change == "status":
            response["ResponseMetadata"]["HTTPStatusCode"] = 206
        if change == "range":
            response["ContentRange"] = "bytes 0-2/23"
        if change == "etag":
            response["ETag"] = '"wrong"'
        if change == "size":
            response["ContentLength"] += 1
        if change == "version":
            response["VersionId"] = "wrong"
        return response

    provider.get_object = broken
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert all(body.closed for body in provider.bodies)


@pytest.mark.parametrize("extra", [b"extra", b""])
def test_truncated_or_oversized_body_is_rejected_and_closed(provider, generated, extra):
    row, _ = generated
    provider.body_factory = lambda data: io.BytesIO(
        data + extra if extra else data[:-1]
    )
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert all(body.closed for body in provider.bodies)


def test_read_failure_closes_body_and_has_stable_error(provider, generated):
    row, _ = generated

    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise OSError("private transport failure")

    provider.body_factory = Broken
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert caught.value.code.endswith("unavailable")
    assert "private" not in str(caught.value)
    assert all(body.closed for body in provider.bodies)


def test_unknown_body_close_preserves_durable_responsibility(provider, generated):
    row, _ = generated

    class Unclosed(io.BytesIO):
        def close(self):
            raise OSError("private close failure")

    provider.body_factory = Unclosed
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert caught.value.storage_operation_unsettled is True
    for body in provider.bodies:
        io.BytesIO.close(body)


def test_open_storage_owns_and_closes_exact_client(provider):
    client = Storage(provider)
    with storage.open_storage(lambda: client) as actual:
        assert actual is client and not provider.closed
    assert provider.closed


def test_client_constructor_failure_is_unsettled(provider):
    def broken():
        raise OSError("private constructor failure")

    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        with storage.open_storage(broken):
            pytest.fail("must not yield")
    assert caught.value.storage_operation_unsettled is True
    assert "private" not in str(caught.value)


def test_client_close_failure_is_unsettled(provider):
    def broken():
        raise OSError("private close failure")

    provider.close = broken
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        with storage.open_storage(lambda: Storage(provider)):
            pass
    assert caught.value.storage_operation_unsettled is True


def test_soft_limit_constructor_and_body_close_are_not_swallowed(provider, generated):
    class Interrupted(BaseException):
        pass

    def broken():
        raise Interrupted()

    with pytest.raises(Interrupted) as caught:
        with storage.open_storage(broken):
            pass
    assert caught.value.storage_operation_unsettled is True
    row, _ = generated

    class Body(io.BytesIO):
        def close(self):
            raise Interrupted()

    provider.body_factory = Body
    with pytest.raises(Interrupted) as caught:
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert caught.value.storage_operation_unsettled is True
    for body in provider.bodies:
        io.BytesIO.close(body)


@pytest.mark.parametrize(
    "code,suffix",
    [
        ("NoSuchKey", "missing"),
        ("PreconditionFailed", "integrity_mismatch"),
        ("AccessDenied", "unavailable"),
    ],
)
def test_provider_errors_are_safe_and_stable(provider, generated, code, suffix):
    row, _ = generated

    def broken(**kwargs):
        raise provider_error(code)

    provider.head_object = broken
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        storage.verify_file(
            Storage(provider), row, storage.resolve(row).key("frame.webp")
        )
    assert caught.value.code.endswith(suffix)
    assert "private" not in str(caught.value)


def test_upload_checks_remote_sha_before_returning(provider, tmp_path):
    row = output()
    key = storage.resolve(row).key("frame.webp")
    provider.corrupt_key = key
    path = tmp_path / "frame.webp"
    path.write_bytes(b"new frame")
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.upload_file(Storage(provider), row, path, "frame.webp", "image/webp")
    assert all(body.closed for body in provider.bodies)


def test_upload_never_overwrites_an_existing_uuid_key(provider, generated):
    row, path = generated
    puts_before = len([1 for kind, _ in provider.calls if kind == "put"])
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.upload_file(Storage(provider), row, path, "frame.webp", "image/webp")
    assert len([1 for kind, _ in provider.calls if kind == "put"]) == puts_before


def test_conditional_put_rejects_a_late_object_without_overwriting(provider, tmp_path):
    row = output()
    owner = storage.resolve(row)
    path = tmp_path / "frame.webp"
    path.write_bytes(b"new frame")
    original = provider.put_object

    def raced(**kwargs):
        provider.objects[(kwargs["Bucket"], kwargs["Key"])] = b"late existing bytes"
        return original(**kwargs)

    provider.put_object = raced
    with pytest.raises(storage.VideoDerivativeStorageError) as caught:
        storage.upload_file(Storage(provider), row, path, "frame.webp", "image/webp")
    assert caught.value.code.endswith("integrity_mismatch")
    assert (
        provider.objects[(owner.bucket, owner.key("frame.webp"))]
        == b"late existing bytes"
    )


def test_local_file_close_uncertainty_is_retained(provider, tmp_path, monkeypatch):
    path = tmp_path / "frame.webp"
    path.write_bytes(b"verified video frame")

    class Unclosed(io.BytesIO):
        def close(self):
            raise OSError("private file close failure")

    stream = Unclosed(path.read_bytes())
    original = type(path).open

    def opened(self, *args, **kwargs):
        return stream if self == path else original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", opened)
    try:
        with pytest.raises(storage.VideoDerivativeStorageError) as caught:
            storage.file_receipt(path)
        assert caught.value.storage_operation_unsettled is True
    finally:
        io.BytesIO.close(stream)


@pytest.mark.parametrize(
    "relative,mime",
    [
        ("frame.webp", "video/mp4"),
        ("metadata.json", "application/json"),
        ("unknown.bin", "application/octet-stream"),
    ],
)
def test_upload_rejects_unknown_name_or_content_type_without_io(
    provider, tmp_path, relative, mime
):
    path = tmp_path / "frame"
    path.write_bytes(b"data")
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.upload_file(Storage(provider), output(), path, relative, mime)
    assert provider.calls == []


@pytest.mark.parametrize(
    "kind",
    [
        "missing-metadata",
        "unknown-key",
        "foreign-key",
        "extra-field",
        "bool-schema",
        "bool-size",
        "bad-sha",
    ],
)
def test_inventory_tampering_is_rejected_before_io(provider, generated, kind):
    row, _ = generated
    row.inventory = deepcopy(row.inventory)
    objects = row.inventory["objects"]
    key = storage.resolve(row).key("frame.webp")
    if kind == "missing-metadata":
        del objects[storage.resolve(row).key("metadata.json")]
    if kind == "unknown-key":
        objects[storage.resolve(row).prefix + "unknown.bin"] = receipt(b"x")
    if kind == "foreign-key":
        objects[storage.prefix_for(uuid.UUID(int=2)) + "frame.webp"] = receipt(b"x")
    if kind == "extra-field":
        objects[key]["version"] = "unproved"
    if kind == "bool-schema":
        row.inventory["schema"] = True
    if kind == "bool-size":
        objects[key]["size_bytes"] = True
    if kind == "bad-sha":
        objects[key]["sha256"] = "sha256:" + "f" * 64
    before = len(provider.calls)
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.validate_inventory(row)
    assert len(provider.calls) == before


def test_inventory_rejects_missing_or_additional_physical_objects(provider, generated):
    row, _ = generated
    owner = storage.resolve(row)
    provider.objects[(owner.bucket, owner.key("chunk.mp4"))] = b"extra"
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.verify_inventory(Storage(provider), row, row.inventory)
    del provider.objects[(owner.bucket, owner.key("chunk.mp4"))]
    del provider.objects[(owner.bucket, owner.key("frame.webp"))]
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.verify_inventory(Storage(provider), row, row.inventory)


def test_inventory_rejects_objects_added_after_readback(provider, generated):
    row, _ = generated
    owner = storage.resolve(row)

    def changed(_):
        provider.objects[(owner.bucket, owner.key("chunk.mp4"))] = b"late extra"

    provider.after_get = changed
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.verify_inventory(Storage(provider), row, row.inventory)


def test_aggregate_and_single_file_budgets_are_checked(
    provider, generated, tmp_path, monkeypatch
):
    row, path = generated
    monkeypatch.setattr(storage, "MAX_OUTPUT_BYTES", 3)
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.file_receipt(path)
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.validate_inventory(row)
    path = tmp_path / "empty"
    path.touch()
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.file_receipt(path)


def test_bounded_read_rejects_before_provider_io(provider, generated):
    row, _ = generated
    key = storage.resolve(row).key("frame.webp")
    before = len(provider.calls)
    for budget in [True, 0, 1, storage.MAX_RESULT_BYTES + 1]:
        with pytest.raises(storage.VideoDerivativeStorageError):
            storage.read_bytes(Storage(provider), row, key, max_bytes=budget)
    assert len(provider.calls) == before


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {"nan": float("nan")},
        {"bad": object()},
        {1: "integer-key"},
        {"tuple": (1, 2)},
    ],
)
def test_non_json_result_cannot_be_uploaded(provider, result):
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.write_result(Storage(provider), output(), result)
    assert provider.calls == []


def test_metadata_and_aggregate_upload_budgets(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_RESULT_BYTES", 3)
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.write_result(Storage(provider), output(), {"long": "metadata"})
    assert provider.calls == []
    monkeypatch.setattr(storage, "MAX_OUTPUT_BYTES", 9)
    owner = storage.resolve(output())
    provider.objects[(owner.bucket, owner.key("poster.webp"))] = b"sixsix"
    path = tmp_path / "frame.webp"
    path.write_bytes(b"four")
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.upload_file(
            Storage(provider), output(), path, "frame.webp", "image/webp"
        )
    assert not any(kind == "put" for kind, _ in provider.calls)


def test_signed_url_only_uses_saved_owner_and_exact_expiry(provider, generated):
    row, _ = generated
    key = storage.resolve(row).key("frame.webp")
    url = storage.signed_url(Storage(provider), row, key, expires=23)
    assert "/saved-video-cache/" in url
    assert provider.calls[-1] == (
        "sign",
        {"Key": key, "bucket": "saved-video-cache", "expires_in": 23, "align": False},
    )
    for invalid in [True, 0, 3601]:
        with pytest.raises(storage.VideoDerivativeStorageError):
            storage.signed_url(Storage(provider), row, key, expires=invalid)


def test_delete_only_saved_prefix_and_prove_empty(provider, generated):
    row, _ = generated
    owner = storage.resolve(row)
    foreign = storage.prefix_for(uuid.UUID(int=2)) + "frame.webp"
    provider.objects[(owner.bucket, foreign)] = b"foreign"
    provider.objects[("new-current-cache", owner.key("frame.webp"))] = (
        b"current bucket trap"
    )
    assert storage.delete_output(Storage(provider), vars(row)) == 2
    assert len(provider.objects) == 2
    assert storage.delete_output(Storage(provider), row) == 0


def test_delete_partial_build_does_not_require_published_inventory(provider):
    row = output()
    owner = storage.resolve(row)
    provider.objects[(owner.bucket, owner.key("frame.webp"))] = b"partial build"
    assert storage.delete_output(Storage(provider), row) == 1


def test_failed_physical_delete_does_not_claim_success(provider, generated):
    row, _ = generated
    provider.ignore_delete = True
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.delete_output(Storage(provider), row)


@pytest.mark.parametrize(
    "key", ["outside/frame.webp", storage.prefix_for(IDENTIFIER) + "unknown.bin"]
)
def test_list_never_deletes_unattributable_keys(provider, key):
    provider.list_extra = {"Key": key, "Size": 1}
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.delete_output(Storage(provider), output())
    assert not any(kind == "delete" for kind, _ in provider.calls)


def test_paginated_listing_checks_all_owned_objects(provider, generated):
    row, _ = generated
    original = provider.list_objects_v2

    def paginated(**kwargs):
        result = original(**kwargs)
        if len(result["Contents"]) <= 1:
            return result
        if kwargs.get("ContinuationToken") == "page-2":
            return {**result, "Contents": result["Contents"][1:]}
        return {
            **result,
            "Contents": result["Contents"][:1],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        }

    provider.list_objects_v2 = paginated
    assert (
        storage.verify_inventory(Storage(provider), row, row.inventory) == row.inventory
    )
    assert storage.delete_output(Storage(provider), row) == 2


def test_listing_rejects_repeated_pagination_tokens(provider):
    def broken(**kwargs):
        return {
            "Contents": [],
            "IsTruncated": True,
            "NextContinuationToken": "again",
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    provider.list_objects_v2 = broken
    with pytest.raises(storage.VideoDerivativeStorageError):
        storage.delete_output(Storage(provider), output())
    assert not any(kind == "delete" for kind, _ in provider.calls)
