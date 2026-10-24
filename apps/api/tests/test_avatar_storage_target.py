"""Registered target capture and exact provider receipts without network or DB I/O."""

from contextlib import contextmanager
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
import pytest

from app.db.models.dataset_asset import StorageLocation
from app.services import avatar, avatar_storage, avatar_storage_target as targets
from tests.test_avatar_storage import TOKEN, external_reference


def missing():
    return ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


class Objects:
    def __init__(self, version=None):
        self.version = version
        self.objects = {}
        self.current = {}
        self.calls = []
        self.bodies = []
        self.closed = 0
        self.deny_write = self.deny_delete = self.ignore_conditions = False
        self.corrupt_read = False
        self.counter = 0

    def seed(self, bucket, key, data, version=None):
        self.objects[bucket, key, version] = data
        self.current[bucket, key] = version

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.deny_write:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        if (bucket, key) in self.current and not self.ignore_conditions:
            assert kwargs["IfNoneMatch"] == "*"
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.counter += 1
        version = (
            f"{self.version}-{self.counter}"
            if self.version not in {None, "null"}
            else self.version
        )
        self.seed(bucket, key, kwargs["Body"], version)
        return {"VersionId": version} if version is not None else {}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        if (bucket, key) not in self.current and "VersionId" not in kwargs:
            raise missing()
        version = kwargs.get("VersionId", self.current.get((bucket, key)))
        try:
            data = self.objects[bucket, key, version]
        except KeyError:
            raise missing() from None
        body = io.BytesIO(b"corrupted" if self.corrupt_read else data)
        self.bodies.append(body)
        return {
            "Body": body,
            "VersionId": version,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.deny_delete:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        version = kwargs.get("VersionId", self.current.get((bucket, key)))
        self.objects.pop((bucket, key, version), None)
        if self.current.get((bucket, key)) == version:
            self.current.pop((bucket, key), None)
        return {}

    @contextmanager
    def client(self):
        try:
            yield self
        finally:
            self.closed += 1


def target(objects, ref=None):
    ref = ref or external_reference()
    return targets.Target(
        uuid.UUID(ref["location_id"]),
        ref["config_revision"],
        4,
        ref["endpoint_url"],
        ref["bucket"],
        ref["object_key"][:-37],
        _access=objects,
    )


@pytest.mark.parametrize("version", [None, "null", "version"])
def test_probe_proves_conditions_full_bytes_and_exact_cleanup(version):
    objects = Objects(version)
    result = targets.probe_target(target(objects))
    assert result == {"conditional_write": True, "full_read": True, "delete": True}
    assert not objects.objects and objects.closed == 1
    assert len([call for call in objects.calls if call[0] == "put"]) == 2
    assert all(body.closed for body in objects.bodies)
    for kind, params in objects.calls:
        if kind in {"get", "delete"}:
            assert ("VersionId" in params) is (version is not None)


def test_known_readonly_probe_refusal_does_not_claim_unknown_put():
    objects = Objects()
    objects.deny_write = True
    with pytest.raises(ClientError) as caught:
        targets.probe_target(target(objects))
    assert not getattr(caught.value, "storage_operation_unsettled", False)
    assert not objects.objects and objects.closed == 1


def test_probe_corruption_is_refused_after_known_cleanup():
    objects = Objects("version")
    objects.corrupt_read = True
    with pytest.raises(avatar_storage.AvatarStorageError):
        targets.probe_target(target(objects))
    assert not objects.objects and all(body.closed for body in objects.bodies)


def test_probe_cleanup_failure_keeps_unsettled_responsibility():
    objects = Objects("version")
    objects.deny_delete = True
    with pytest.raises(ClientError) as caught:
        targets.probe_target(target(objects))
    assert caught.value.storage_operation_unsettled
    assert objects.objects and objects.closed == 1


@pytest.mark.parametrize("version", [None, "null", "version"])
def test_probe_rejects_ignored_create_condition_without_leaking_known_objects(version):
    objects = Objects(version)
    objects.ignore_conditions = True
    with pytest.raises(avatar_storage.AvatarStorageError) as caught:
        targets.probe_target(target(objects))
    assert caught.value.code == "avatar_storage_conditions_unsupported"
    assert not objects.objects


@pytest.mark.parametrize("version", [None, "null", "version"])
def test_write_returns_real_receipt_and_all_reads_and_deletes_pin_it(version):
    objects = Objects(version)
    ref, data = external_reference(), b"full normalized avatar bytes"
    row = SimpleNamespace(
        token=TOKEN,
        storage_ref=ref,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        object_version_id=None,
    )
    chosen = target(objects, ref)
    row.object_version_id = avatar.write_avatar_object(row, data, target=chosen)
    assert row.object_version_id == ("version-1" if version == "version" else version)
    assert avatar.read_avatar_object(row, target=chosen) == (data, f'"{row.sha256}"')
    avatar.delete_avatar_object(row, target=chosen)
    assert not objects.objects and all(body.closed for body in objects.bodies)
    for kind, params in objects.calls:
        if kind in {"get", "delete"}:
            assert params.get("VersionId") == row.object_version_id


def test_saved_version_does_not_read_or_delete_newer_same_key():
    objects = Objects("version")
    ref, data = external_reference(), b"saved version bytes"
    row = SimpleNamespace(
        token=TOKEN,
        storage_ref=ref,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    chosen = target(objects, ref)
    row.object_version_id = avatar.write_avatar_object(row, data, target=chosen)
    objects.seed(ref["bucket"], ref["object_key"], b"other actor replacement", "newer")
    assert avatar.read_avatar_object(row, target=chosen)[0] == data
    avatar.delete_avatar_object(row, target=chosen)
    assert objects.objects == {
        (ref["bucket"], ref["object_key"], "newer"): b"other actor replacement"
    }


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_external_owner_never_falls_back_to_global_client(operation):
    data = b"saved avatar"
    row = SimpleNamespace(
        token=TOKEN,
        storage_ref=external_reference(),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    with pytest.raises(avatar_storage.AvatarStorageError):
        if operation == "write":
            avatar.write_avatar_object(row, data)
        else:
            getattr(avatar, f"{operation}_avatar_object")(row)


def test_wrong_captured_target_is_rejected_before_physical_access():
    objects, ref = Objects(), external_reference()
    row = SimpleNamespace(token=TOKEN, storage_ref=ref, sha256="a" * 64, size_bytes=1)
    wrong = target(objects, {**ref, "location_id": str(uuid.uuid4())})
    with pytest.raises(avatar_storage.AvatarStorageError):
        avatar.read_avatar_object(row, target=wrong)
    assert not objects.calls


def test_unversioned_retirement_checks_full_saved_bytes_before_delete():
    objects = Objects()
    ref, data = external_reference(), b"saved avatar bytes"
    row = SimpleNamespace(
        token=TOKEN,
        storage_ref=ref,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    chosen = target(objects, ref)
    avatar.write_avatar_object(row, data, target=chosen)
    objects.seed(ref["bucket"], ref["object_key"], b"replaced", None)
    with pytest.raises(avatar_storage.AvatarStorageError):
        avatar.delete_avatar_object(row, target=chosen)
    assert not any(kind == "delete" for kind, _ in objects.calls)


def connection_target(*, enabled=True):
    location = SimpleNamespace(
        id=uuid.uuid4(),
        config_revision=3,
        connection_id=uuid.uuid4(),
        kind="s3",
        enabled=enabled,
    )
    connection = SimpleNamespace(
        kind="s3",
        scope="global",
        project_id=None,
        config={
            "endpoint": "https://registered.example.test",
            "bucket": "avatars-target",
            "base_prefix": "team/图片/",
        },
        revision=4,
        secret_enc=b"private-encrypted-secret",
    )
    return location, connection


async def test_capture_contains_no_network_and_keeps_credentials_private(monkeypatch):
    location, connection = connection_target()
    db = SimpleNamespace(get=AsyncMock(return_value=connection))
    monkeypatch.setattr(
        targets.connector_guard,
        "get_allowlist",
        AsyncMock(return_value=["registered.example.test"]),
    )
    monkeypatch.setattr(
        targets.connector_guard,
        "assert_host_allowed",
        lambda *_: pytest.fail("DNS/network guard belongs inside I/O"),
    )
    captured = await targets.target_for(db, location)
    assert captured.key_prefix == "team/图片/_aap_managed/avatars/"
    assert captured.facts == {
        "location_id": str(location.id),
        "config_revision": 3,
        "connection_revision": 4,
        "endpoint_url": "https://registered.example.test",
        "bucket": "avatars-target",
        "key_prefix": captured.key_prefix,
    }
    assert "private" not in repr(captured) and "secret" not in str(captured.facts)


async def test_disabled_target_can_be_validated_as_mapping_but_not_allocated(
    monkeypatch,
):
    location, connection = connection_target(enabled=False)
    db = SimpleNamespace(get=AsyncMock(return_value=connection))
    monkeypatch.setattr(
        targets.connector_guard, "get_allowlist", AsyncMock(return_value=[])
    )
    assert (
        await targets.target_for(db, location, require_enabled=False)
    ).location_id == location.id
    with pytest.raises(avatar_storage.AvatarStorageError):
        await targets.target_for(db, location)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "sftp"),
        ("scope", "owner"),
        ("project_id", uuid.uuid4()),
        ("secret_enc", None),
    ],
)
async def test_only_existing_global_credentialed_s3_is_a_target(
    monkeypatch, field, value
):
    location, connection = connection_target()
    setattr(connection, field, value)
    db = SimpleNamespace(get=AsyncMock(return_value=connection))
    monkeypatch.setattr(
        targets.connector_guard, "get_allowlist", AsyncMock(return_value=[])
    )
    with pytest.raises(avatar_storage.AvatarStorageError):
        await targets.target_for(db, location)


async def test_io_target_uses_rotated_credentials_without_rewriting_saved_location(
    monkeypatch,
):
    ref = external_reference()
    location, connection = connection_target()
    location.id = uuid.UUID(ref["location_id"])
    location.config_revision = 99
    connection.revision = 100
    connection.config.update(
        endpoint=ref["endpoint_url"], bucket=ref["bucket"], base_prefix="team/图片/"
    )
    connection.secret_enc = b"rotated-encrypted-secret"

    async def get(model, _identifier, **_kwargs):
        return location if model is StorageLocation else connection

    db = SimpleNamespace(get=AsyncMock(side_effect=get))
    monkeypatch.setattr(
        targets.connector_guard, "get_allowlist", AsyncMock(return_value=[])
    )
    row = SimpleNamespace(token=TOKEN, storage_ref=ref, sha256="a" * 64, size_bytes=1)
    captured = await avatar_storage.io_target(db, row)
    assert captured._access.secret_enc == b"rotated-encrypted-secret"
    assert captured.config_revision == 99 and row.storage_ref == ref


@pytest.mark.parametrize(
    "base", ["../escape", "/root", "one//two", "one\\two", "one\x00two", "x" * 1000]
)
def test_managed_namespace_rejects_unsafe_or_oversize_prefix(base):
    with pytest.raises(avatar_storage.AvatarStorageError):
        targets.managed_prefix(base)
