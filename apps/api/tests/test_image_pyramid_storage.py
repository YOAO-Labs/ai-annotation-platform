"""Saved output owner and full provider readback tests without external services."""

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
from app.services import image_pyramid_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

IDENTIFIER = uuid.UUID(int=919)


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(identifier=IDENTIFIER, bucket="saved-pyramid-cache"):
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
        id=IDENTIFIER, storage_ref=reference(), inventory=None, **changes
    )


def provider_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "private provider details"}}, "GetObject"
    )


class Provider:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url=endpoint())
        self.objects = {}
        self.calls = []
        self.bodies = []
        self.corrupt_key = None
        self.body_factory = io.BytesIO
        self.after_get = None
        self.list_extra = None
        self.ignore_delete = False
        self.version = "version-1"

    def metadata(self, data):
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
            "VersionId": self.version,
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
        metadata = self.metadata(data)
        assert kwargs["IfMatch"] == metadata["ETag"]
        assert kwargs.get("VersionId") == self.version
        body = self.body_factory(
            b"X" * len(data) if kwargs["Key"] == self.corrupt_key else data
        )
        self.bodies.append(body)
        if self.after_get:
            self.after_get(kwargs)
        return {**metadata, "Body": body}

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
        if self.list_extra:
            rows.append(self.list_extra)
        return {"Contents": rows, "IsTruncated": False}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if not self.ignore_delete:
            self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


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
    tile = tmp_path / "tile.webp"
    tile.write_bytes(b"tile-bytes")
    overview = tmp_path / "overview.webp"
    overview.write_bytes(b"overview-bytes")
    row = output()
    manifest = json.dumps({"schema": "aap-image-pyramid/v1", "generation": 1}).encode()
    result = storage.upload_and_verify(
        SimpleNamespace(client=provider),
        row,
        [(tile, 0, 0, 0)],
        overview,
        manifest,
        float("inf"),
    )
    row.inventory = result[3]
    return row, result, manifest


def test_saved_owner_ignores_current_bucket_and_returns_copies(provider):
    row = output()
    owner = storage.resolve(row)
    assert owner.bucket == "saved-pyramid-cache"
    assert owner.key("tiles/0/0/0.webp").startswith(owner.prefix)
    ref = owner.ref
    ref["bucket"] = "tampered"
    row.storage_ref["bucket"] = "also-tampered"
    assert owner.ref["bucket"] == "saved-pyramid-cache"


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
        ("prefix", "image-pyramids/old/g1/"),
        ("prefix", storage.prefix_for(uuid.UUID(int=1))),
    ],
)
def test_resolve_rejects_tampered_owner(provider, field, value):
    row = output()
    row.storage_ref[field] = value
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.resolve(row)
    assert provider.calls == []


@pytest.mark.parametrize("value", [None, {}, {**reference(), "unknown": 1}])
def test_unknown_legacy_owner_never_resolves(provider, value):
    row = output()
    row.storage_ref = value
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.resolve(row)
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
    ],
)
def test_owner_rejects_unsafe_relative_keys(provider, key):
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.resolve(output()).key(key)


def test_runtime_and_physical_client_endpoints_must_match(provider):
    row = output()
    wrong = SimpleNamespace(
        client=SimpleNamespace(
            meta=SimpleNamespace(endpoint_url="https://different.invalid")
        )
    )
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.delete(wrong, row)
    provider.meta.endpoint_url = "https://different.invalid"
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.resolve(row)
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
async def test_allocate_does_not_adopt_missing_registration(provider, missing):
    db = SimpleNamespace(
        get=AsyncMock(return_value=None), scalar=AsyncMock(return_value=None)
    )
    if missing == "binding":
        db.get.return_value = SimpleNamespace(
            id=LEGACY_LOCATION_ID,
            kind="s3",
            config_revision=1,
            enabled=True,
            config={
                "endpoint": settings.minio_endpoint,
                "use_ssl": settings.minio_use_ssl,
            },
        )
    with pytest.raises(storage.ImagePyramidStorageError, match="金字塔") as caught:
        await storage.allocate(db, IDENTIFIER)
    assert caught.value.code.endswith("binding_unproven")
    assert provider.calls == []


def test_upload_reads_every_saved_object_and_manifest_last(provider, generated):
    row, result, manifest = generated
    manifest_key, overview_key, retained, inventory = result
    assert manifest_key.endswith("/manifest.json")
    assert overview_key.endswith("/overview.webp")
    assert retained == sum(len(data) for data in provider.objects.values())
    assert (
        inventory["objects"][manifest_key]["sha256"]
        == hashlib.sha256(manifest).hexdigest()
    )
    puts = [params for kind, params in provider.calls if kind == "put"]
    assert puts[-1]["Key"] == manifest_key
    assert all(
        params["Bucket"] == "saved-pyramid-cache" for _, params in provider.calls
    )
    assert all(body.closed for body in provider.bodies)
    assert len([1 for kind, _ in provider.calls if kind == "get"]) == 3
    assert (
        storage.read_manifest(
            SimpleNamespace(client=provider),
            row,
            "sha256:" + hashlib.sha256(manifest).hexdigest(),
        )["generation"]
        == 1
    )
    receipt = storage.verify_object(SimpleNamespace(client=provider), row, overview_key)
    assert receipt == row.inventory["objects"][overview_key]
    receipt["sha256"] = "changed"
    assert row.inventory["objects"][overview_key]["sha256"] != "changed"


def test_same_size_remote_corruption_is_rejected_and_closed(provider, generated):
    row, result, _ = generated
    provider.corrupt_key = result[1]
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert caught.value.code.endswith("integrity_mismatch")
    assert all(body.closed for body in provider.bodies)


def test_changed_object_during_readback_is_rejected(provider, generated):
    row, result, _ = generated
    provider.after_get = lambda _: setattr(provider, "version", "version-2")
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert caught.value.code.endswith("integrity_mismatch")
    assert all(body.closed for body in provider.bodies)


def test_failed_body_close_remains_unsettled(provider, generated):
    row, result, _ = generated

    class Unclosed(io.BytesIO):
        def close(self):
            raise OSError("private close failure")

    provider.body_factory = Unclosed
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert caught.value.storage_operation_unsettled is True
    assert caught.value.code.endswith("unavailable")
    for body in provider.bodies:
        io.BytesIO.close(body)


def test_missing_object_has_stable_error(provider, generated):
    row, result, _ = generated
    del provider.objects[("saved-pyramid-cache", result[1])]
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert caught.value.code.endswith("missing")


def test_foreign_key_cannot_be_verified(provider, generated):
    row, _, _ = generated
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.verify_object(
            SimpleNamespace(client=provider),
            row,
            storage.prefix_for(uuid.UUID(int=2)) + "overview.webp",
        )


@pytest.mark.parametrize("digest", [None, "f" * 64, "sha256:" + "f" * 64])
def test_manifest_requires_the_saved_digest(provider, generated, digest):
    row, _, _ = generated
    before = len(provider.calls)
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.read_manifest(SimpleNamespace(client=provider), row, digest)
    assert len(provider.calls) == before


def test_expired_upload_deadline_rejects_before_io(provider, tmp_path):
    tile = tmp_path / "tile.webp"
    tile.write_bytes(b"tile")
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.upload_and_verify(
            SimpleNamespace(client=provider),
            output(),
            [(tile, 0, 0, 0)],
            tile,
            b"{}",
            0,
        )
    assert caught.value.code.endswith("budget")
    assert provider.calls == []


def test_nonempty_uuid_prefix_is_never_overwritten(provider, generated, tmp_path):
    row, _, _ = generated
    tile = tmp_path / "another.webp"
    tile.write_bytes(b"new")
    before = len([1 for kind, _ in provider.calls if kind == "put"])
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.upload_and_verify(
            SimpleNamespace(client=provider),
            row,
            [(tile, 0, 0, 0)],
            tile,
            b"{}",
            float("inf"),
        )
    assert len([1 for kind, _ in provider.calls if kind == "put"]) == before


def test_delete_only_saved_prefix_and_verify_empty(provider, generated):
    row, _, _ = generated
    foreign = storage.prefix_for(uuid.UUID(int=2)) + "overview.webp"
    provider.objects[("saved-pyramid-cache", foreign)] = b"foreign"
    provider.objects[
        ("new-current-cache", row.storage_ref["prefix"] + "overview.webp")
    ] = b"other-current-bucket"
    assert storage.delete(SimpleNamespace(client=provider), row) == 3
    assert len(provider.objects) == 2
    assert storage.delete(SimpleNamespace(client=provider), row) == 0


def test_delete_does_not_claim_success_if_provider_retains_objects(provider, generated):
    row, _, _ = generated
    provider.ignore_delete = True
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.delete(SimpleNamespace(client=provider), row)
    assert caught.value.code.endswith("integrity_mismatch")


def test_list_never_accepts_keys_outside_saved_prefix(provider):
    provider.list_extra = {"Key": "outside/other", "Size": 1}
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.delete(SimpleNamespace(client=provider), output())
    assert not any(kind == "delete" for kind, _ in provider.calls)


def test_inventory_receipt_tampering_is_rejected_before_io(provider, generated):
    row, result, _ = generated
    row.inventory = deepcopy(row.inventory)
    row.inventory["objects"][result[1]]["size_bytes"] = True
    before = len(provider.calls)
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert len(provider.calls) == before


@pytest.mark.parametrize(
    "kind", ["missing-manifest", "unknown-key", "extra-receipt-field", "bool-schema"]
)
def test_inventory_requires_exact_layout_and_receipts(provider, generated, kind):
    row, result, _ = generated
    row.inventory = deepcopy(row.inventory)
    objects = row.inventory["objects"]
    if kind == "missing-manifest":
        del objects[result[0]]
    elif kind == "unknown-key":
        objects[row.storage_ref["prefix"] + "unexpected.webp"] = objects[result[1]]
    elif kind == "extra-receipt-field":
        objects[result[1]]["version"] = "unproved"
    else:
        row.inventory["schema"] = True
    before = len(provider.calls)
    with pytest.raises(storage.ImagePyramidStorageError):
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert len(provider.calls) == before


@pytest.mark.parametrize("extra", [b"extra", b""])
def test_truncated_or_oversized_readback_is_rejected_and_closed(
    provider, generated, extra
):
    row, result, _ = generated
    provider.body_factory = lambda data: io.BytesIO(
        data + extra if extra else data[:-1]
    )
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert caught.value.code.endswith("integrity_mismatch")
    assert all(body.closed for body in provider.bodies)


def test_read_failure_still_closes_body(provider, generated):
    row, result, _ = generated

    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise OSError("private transport failure")

    provider.body_factory = Broken
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.verify_object(SimpleNamespace(client=provider), row, result[1])
    assert caught.value.code.endswith("unavailable")
    assert all(body.closed for body in provider.bodies)


def test_upload_verifies_remote_sha_before_returning(provider, tmp_path):
    row = output()
    path = tmp_path / "tile.webp"
    path.write_bytes(b"test pixels")
    provider.corrupt_key = storage.resolve(row).key("tiles/0/0/0.webp")
    with pytest.raises(storage.ImagePyramidStorageError) as caught:
        storage.upload_and_verify(
            SimpleNamespace(client=provider),
            row,
            [(path, 0, 0, 0)],
            path,
            b"{}",
            float("inf"),
        )
    assert caught.value.code.endswith("integrity_mismatch")
    assert all(body.closed for body in provider.bodies)


def test_paginated_deletion_verifies_the_whole_prefix(provider):
    row = output()
    prefix = row.storage_ref["prefix"]
    provider.objects[("saved-pyramid-cache", prefix + "first.webp")] = b"one"
    provider.objects[("saved-pyramid-cache", prefix + "second.webp")] = b"two"
    original = provider.list_objects_v2

    def paginated(**kwargs):
        result = original(**kwargs)
        if len(result["Contents"]) <= 1:
            return result
        if kwargs.get("ContinuationToken") == "page-2":
            return {"Contents": result["Contents"][1:], "IsTruncated": False}
        return {
            "Contents": result["Contents"][:1],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        }

    provider.list_objects_v2 = paginated
    assert storage.delete(SimpleNamespace(client=provider), row) == 2
    assert provider.objects == {}
