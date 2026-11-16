"""Physical scope, exact-version and prefix proofs for native saved outputs."""

import hashlib
import io
import threading
from uuid import uuid4

from botocore.exceptions import ClientError, EndpointConnectionError
import pytest

from yoao_assets_images.contract import (
    NativePyramidRetirementMember,
    NativePyramidRetirementTarget,
)
from yoao_assets_cleanup.native_gc import (
    NativeStorageError,
    NativeTargetRefused,
    S3NativeRetirementStore,
)


class _Body:
    def __init__(self, payload: bytes):
        self._buffer = io.BytesIO(payload)
        self.closed = False

    def read(self, size: int) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(
        self,
        *,
        heads=(),
        gets=(),
        deletes=(),
        versions=None,
        uploads=None,
        objects=None,
    ):
        self.heads = list(heads)
        self.gets = list(gets)
        self.deletes = list(deletes)
        self.versions = versions if versions is not None else {}
        self.uploads = uploads if uploads is not None else {}
        self.objects = objects if objects is not None else {}
        self.calls = []

    def head_object(self, **parameters):
        self.calls.append(("head_object", parameters))
        return self._next(self.heads)

    def get_object(self, **parameters):
        self.calls.append(("get_object", parameters))
        return self._next(self.gets)

    def delete_object(self, **parameters):
        self.calls.append(("delete_object", parameters))
        return self._next(self.deletes)

    def list_object_versions(self, **parameters):
        self.calls.append(("list_object_versions", parameters))
        return self._next([self.versions])

    def list_multipart_uploads(self, **parameters):
        self.calls.append(("list_multipart_uploads", parameters))
        return self._next([self.uploads])

    def list_objects_v2(self, **parameters):
        self.calls.append(("list_objects_v2", parameters))
        return self._next([self.objects])

    @staticmethod
    def _next(items):
        if not items:
            raise AssertionError("unexpected object-store call")
        item = items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Object",
    )


def _store(client: _Client) -> S3NativeRetirementStore:
    store = object.__new__(S3NativeRetirementStore)
    store.endpoint = "http://s3:9000"
    store.bucket = "media-cache"
    store.client = client
    return store


def _target(
    output_id,
    *,
    endpoint: str = "http://s3:9000",
    bucket: str = "media-cache",
    key_prefix: str = "",
    prefix: str | None = None,
) -> NativePyramidRetirementTarget:
    return NativePyramidRetirementTarget.model_validate(
        {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": str(uuid4()),
            "config_revision": 1,
            "binding_generation": 2,
            "endpoint_url": endpoint,
            "bucket": bucket,
            "key_prefix": key_prefix,
            "prefix": (
                prefix
                if prefix is not None
                else key_prefix + f"image-pyramids/objects/{output_id}/"
            ),
            "storage_identity": "a" * 64,
        }
    )


def _member(
    payload: bytes = b"tile-bytes",
    *,
    relative_key: str = "tiles/0/0/0.webp",
    version_id: str | None = "v1",
    sha256: str | None = None,
    size_bytes: int | None = None,
    etag: str | None = None,
) -> NativePyramidRetirementMember:
    digest = sha256 or hashlib.sha256(payload).hexdigest()
    return NativePyramidRetirementMember(
        operation_id=uuid4(),
        relative_key=relative_key,
        sha256=digest,
        size_bytes=len(payload) if size_bytes is None else size_bytes,
        etag=etag or '"' + hashlib.md5(payload).hexdigest() + '"',
        version_id=version_id,
    )


def _head(member, payload: bytes, *, etag=None, version=None):
    return {
        "ContentLength": len(payload),
        "ETag": member.etag if etag is None else etag,
        "VersionId": member.version_id if version is None else version,
    }


def _get(member, payload: bytes, *, body=None, version=None, etag=None):
    return {
        "ContentLength": len(payload),
        "ETag": member.etag if etag is None else etag,
        "VersionId": member.version_id if version is None else version,
        "Body": body if body is not None else _Body(payload),
    }


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"endpoint": "http://other:9000"}, "native_target_endpoint_mismatch"),
        ({"bucket": "other-bucket"}, "native_target_bucket_mismatch"),
        ({"prefix": "image-pyramids/other/"}, "native_target_prefix_mismatch"),
        ({"key_prefix": "wrong/", "prefix": "image-pyramids/objects/x/"}, (
            "native_target_prefix_mismatch"
        )),
    ],
)
def test_saved_target_is_rejected_before_any_s3_call(kwargs, code):
    output_id = uuid4()
    client = _Client()
    target = _target(output_id, **kwargs)

    with pytest.raises(NativeTargetRefused, match=code):
        _store(client).verify_target(output_id, target)

    assert client.calls == []


def test_exact_versioned_member_is_read_then_conditionally_deleted():
    output_id = uuid4()
    prefix = f"image-pyramids/objects/{output_id}/"
    member = _member()
    body = _Body(b"tile-bytes")
    client = _Client(
        heads=[_head(member, b"tile-bytes"), _error("NoSuchVersion", 404)],
        gets=[_get(member, b"tile-bytes", body=body)],
        deletes=[{}],
    )

    _store(client).delete_member(prefix, member, threading.Event())

    assert [call[0] for call in client.calls] == [
        "head_object",
        "get_object",
        "delete_object",
        "head_object",
    ]
    assert client.calls[1][1]["Key"] == prefix + "tiles/0/0/0.webp"
    assert client.calls[1][1]["IfMatch"] == member.etag
    assert client.calls[1][1]["VersionId"] == "v1"
    assert client.calls[2][1]["IfMatch"] == member.etag
    assert client.calls[2][1]["VersionId"] == "v1"
    assert body.closed is True


def test_unversioned_receipt_keeps_if_match_and_never_guesses_a_version():
    output_id = uuid4()
    prefix = f"image-pyramids/objects/{output_id}/"
    member = _member(version_id=None)
    body = _Body(b"tile-bytes")
    head = _head(member, b"tile-bytes", version="current-version")
    client = _Client(
        heads=[head, _error("404", 404)],
        gets=[_get(member, b"tile-bytes", body=body, version=None)],
        deletes=[{}],
    )

    _store(client).delete_member(prefix, member, threading.Event())

    for name, parameters in client.calls:
        if name in {"head_object", "get_object", "delete_object"}:
            assert "VersionId" not in parameters
    assert client.calls[1][1]["IfMatch"] == member.etag
    assert client.calls[2][1]["IfMatch"] == member.etag
    assert body.closed is True


def test_head_identity_mismatch_never_streams_or_deletes():
    output_id = uuid4()
    member = _member()
    client = _Client(heads=[_head(member, b"tile-bytes", etag='"other"')])

    with pytest.raises(NativeStorageError, match="native_member_identity_mismatch"):
        _store(client).delete_member(
            f"image-pyramids/objects/{output_id}/",
            member,
            threading.Event(),
        )

    assert [call[0] for call in client.calls] == ["head_object"]


def test_streamed_sha_mismatch_closes_the_body_and_never_deletes():
    output_id = uuid4()
    member = _member()
    body = _Body(b"tile-BYTES")
    client = _Client(
        heads=[_head(member, b"tile-bytes")],
        gets=[_get(member, b"tile-bytes", body=body)],
    )

    with pytest.raises(NativeStorageError, match="native_member_identity_mismatch"):
        _store(client).delete_member(
            f"image-pyramids/objects/{output_id}/",
            member,
            threading.Event(),
        )

    assert [call[0] for call in client.calls] == ["head_object", "get_object"]
    assert body.closed is True


def test_missing_exact_version_is_accounted_without_a_delete():
    output_id = uuid4()
    member = _member()
    client = _Client(heads=[_error("NoSuchVersion", 404)])

    _store(client).delete_member(
        f"image-pyramids/objects/{output_id}/",
        member,
        threading.Event(),
    )

    assert [call[0] for call in client.calls] == ["head_object"]


def test_delete_transport_failure_is_reported_as_unsettled():
    output_id = uuid4()
    member = _member()
    client = _Client(
        heads=[_head(member, b"tile-bytes")],
        gets=[_get(member, b"tile-bytes")],
        deletes=[EndpointConnectionError(endpoint_url="http://s3:9000")],
    )

    with pytest.raises(NativeStorageError) as failure:
        _store(client).delete_member(
            f"image-pyramids/objects/{output_id}/",
            member,
            threading.Event(),
        )

    assert failure.value.code == "native_member_delete_unsettled"
    assert failure.value.unsettled is True


def test_delete_precondition_is_not_absence():
    output_id = uuid4()
    member = _member()
    client = _Client(
        heads=[_head(member, b"tile-bytes")],
        gets=[_get(member, b"tile-bytes")],
        deletes=[_error("PreconditionFailed", 412)],
    )

    with pytest.raises(NativeStorageError) as failure:
        _store(client).delete_member(
            f"image-pyramids/objects/{output_id}/",
            member,
            threading.Event(),
        )

    assert failure.value.code == "native_member_identity_changed"
    assert failure.value.unsettled is False


def test_delete_success_still_requires_observed_absence():
    output_id = uuid4()
    member = _member()
    client = _Client(
        heads=[_head(member, b"tile-bytes"), _head(member, b"tile-bytes")],
        gets=[_get(member, b"tile-bytes")],
        deletes=[{}],
    )

    with pytest.raises(
        NativeStorageError, match="native_member_present_after_delete"
    ):
        _store(client).delete_member(
            f"image-pyramids/objects/{output_id}/",
            member,
            threading.Event(),
        )


@pytest.mark.parametrize(
    ("versions", "uploads", "objects", "code"),
    [
        (
            {"Versions": [{"Key": "x", "VersionId": "v1"}], "IsTruncated": False},
            {},
            {},
            "native_prefix_not_empty",
        ),
        (
            {
                "DeleteMarkers": [{"Key": "x", "VersionId": "v2"}],
                "IsTruncated": False,
            },
            {},
            {},
            "native_prefix_not_empty",
        ),
        (
            {},
            {
                "Uploads": [{"Key": "x", "UploadId": "u1"}],
                "IsTruncated": False,
            },
            {},
            "native_prefix_not_empty",
        ),
        (
            {},
            {},
            {"Contents": [{"Key": "x"}], "IsTruncated": False},
            "native_prefix_not_empty",
        ),
        (
            {},
            {},
            {"Contents": [{"Key": "foreign"}], "IsTruncated": False},
            "native_prefix_foreign_material",
        ),
        (
            {},
            {"Uploads": [{"Key": "foreign", "UploadId": "u1"}]},
            {},
            "native_prefix_foreign_material",
        ),
        (
            {},
            {},
            {},
            None,
        ),
    ],
)
def test_prefix_absence_covers_versions_markers_uploads_and_objects(
    versions, uploads, objects, code
):
    output_id = uuid4()
    prefix = f"image-pyramids/objects/{output_id}/"
    client = _Client(
        versions=_page(versions, prefix),
        uploads=_page(uploads, prefix),
        objects=_page(objects, prefix),
    )
    store = _store(client)

    if code is None:
        store.verify_prefix_absent(prefix, threading.Event())
        assert [call[0] for call in client.calls] == [
            "list_object_versions",
            "list_multipart_uploads",
            "list_objects_v2",
        ]
    else:
        with pytest.raises(NativeStorageError, match=code):
            store.verify_prefix_absent(prefix, threading.Event())
    assert all(name != "delete_object" for name, _ in client.calls)


def _page(value: dict, prefix: str) -> dict:
    page = {"IsTruncated": value.get("IsTruncated", False)}
    for group in ("Versions", "DeleteMarkers", "Uploads", "Contents"):
        if group not in value:
            continue
        page[group] = [
            {
                **entry,
                "Key": (
                    prefix + "member" if entry.get("Key") == "x" else entry["Key"]
                ),
            }
            for entry in value[group]
        ]
    return page
