from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

from botocore.exceptions import ClientError
import pytest

from aap_assets_mask_content import codec
from aap_assets_mask_content.contract import (
    DeleteRequest,
    GetRequest,
    ListRequest,
    MaskContentFailure,
    PutRequest,
    RleContent,
)
from aap_assets_mask_content.storage import S3MaskContentStore


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.delete_conditions = []
        self.list_limits = []
        self.last_modified = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        self.calls.append(("put", Key))
        if Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[Key]
        return {"Body": BytesIO(body), "ContentLength": len(body)}

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        import hashlib

        return {
            "ETag": '"' + hashlib.md5(self.objects[Key]).hexdigest() + '"',
            "LastModified": self.last_modified.replace(microsecond=0),
            "ContentLength": len(self.objects[Key]),
        }

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, **kwargs):
        self.calls.append(("list", Prefix))
        self.list_limits.append(MaxKeys)
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(kwargs.get("ContinuationToken") or 0)
        page = keys[start : start + MaxKeys]
        import hashlib

        return {
            "Contents": [
                {
                    "Key": key,
                    "ETag": '"' + hashlib.md5(self.objects[key]).hexdigest() + '"',
                    "Size": len(self.objects[key]),
                    "LastModified": self.last_modified,
                }
                for key in page
            ],
            "IsTruncated": start + MaxKeys < len(keys),
            **(
                {"NextContinuationToken": str(start + MaxKeys)}
                if start + MaxKeys < len(keys)
                else {}
            ),
        }

    def delete_object(self, *, Bucket, Key, IfMatch):
        self.calls.append(("delete", Key))
        self.delete_conditions.append(IfMatch)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "DeleteObject")
        import hashlib

        if hashlib.md5(self.objects[Key]).hexdigest() != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "DeleteObject")
        del self.objects[Key]

    def get_bucket_location(self, *, Bucket):
        self.calls.append(("ready", Bucket))
        return {"LocationConstraint": None}


def _rle():
    return RleContent(encoding="coco_rle", size=(2, 2), counts=(1, 3))


def test_put_get_are_idempotent_and_return_the_verified_reference():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    request = PutRequest(content=_rle())

    first = store.put(request)
    second = store.put(request)
    loaded = store.get(GetRequest(reference=first.reference))

    assert second == first
    assert loaded.content == request.content
    assert s3.calls.count(("put", first.reference.object_key)) == 2
    assert first.reference.object_key in s3.objects


def test_put_gzip_falls_back_to_identity_for_existing_expansion_policy():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    content = RleContent(
        encoding="coco_rle", size=(100, 100), counts=(*([1] * 1000), 9000)
    )

    result = store.put(PutRequest(content=content, storage_encoding="gzip"))

    assert result.reference.object_key.endswith(".json")
    assert result.reference.storage_encoding is None
    assert (
        codec.decode_reference(
            result.reference, s3.objects[result.reference.object_key]
        )
        == content
    )


def test_put_refuses_to_overwrite_a_corrupt_existing_content_address():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    request = PutRequest(content=_rle())
    expected = codec.build_rle_reference(request.content)
    s3.objects[expected.object_key] = b"corrupt"

    with pytest.raises(MaskContentFailure) as caught:
        store.put(request)

    assert caught.value.code == "object_corrupt"
    assert s3.objects[expected.object_key] == b"corrupt"


def test_get_validates_reference_before_any_s3_access():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    invalid = {
        "encoding": "coco_rle_ref",
        "size": [1, 1],
        "object_key": "other-bucket-secret",
        "sha256": "a" * 64,
        "runs": 1,
        "bytes": 1,
    }
    with pytest.raises(MaskContentFailure) as caught:
        store.get(GetRequest.model_construct(reference=invalid))

    assert caught.value.code == "invalid_reference"
    assert s3.calls == []


def test_store_revalidates_constructed_requests_before_any_s3_access():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    invalid_content = RleContent.model_construct(
        encoding="coco_rle", size=(2,), counts=(4,)
    )
    put = PutRequest.model_construct(
        content=invalid_content, storage_encoding="identity"
    )
    delete = DeleteRequest.model_construct(
        object_key="other/private-object",
        etag="etag",
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(MaskContentFailure) as caught_put:
        store.put(put)
    with pytest.raises(MaskContentFailure) as caught_delete:
        store.delete(delete)

    assert caught_put.value.code == "invalid_request"
    assert caught_delete.value.code == "invalid_request"
    assert s3.calls == []


def test_get_distinguishes_missing_from_corrupt_stored_objects():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    reference = codec.build_rle_reference(_rle())
    with pytest.raises(MaskContentFailure) as missing:
        store.get(GetRequest(reference=reference))
    assert missing.value.code == "object_missing"

    s3.objects[reference.object_key] = b"not the addressed RLE"
    with pytest.raises(MaskContentFailure) as corrupt:
        store.get(GetRequest(reference=reference))
    assert corrupt.value.code == "object_corrupt"


def test_list_is_prefix_bounded_and_delete_rechecks_candidate_identity():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    ref = codec.build_rle_reference(_rle())
    s3.objects[ref.object_key] = codec.canonical_rle_bytes(_rle())

    page = store.list(ListRequest(limit=5))
    candidate = page.items[0]
    deleted = store.delete(
        DeleteRequest(
            object_key=candidate.key,
            etag=candidate.etag,
            last_modified=candidate.last_modified,
        )
    )

    assert len(page.items) == 1
    assert deleted.deleted is True
    assert ref.object_key not in s3.objects
    assert s3.delete_conditions == [candidate.etag]
    assert all(
        call_key.startswith("raster-masks/sha256/") for _op, call_key in s3.calls
    )


def test_delete_refuses_changed_object_and_missing_delete_is_idempotent():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    ref = codec.build_rle_reference(_rle())
    s3.objects[ref.object_key] = codec.canonical_rle_bytes(_rle())
    candidate = store.list(ListRequest()).items[0]
    changed = DeleteRequest(
        object_key=candidate.key,
        etag="different",
        last_modified=candidate.last_modified,
    )

    with pytest.raises(MaskContentFailure) as caught:
        store.delete(changed)
    assert caught.value.code == "content_changed"
    assert ref.object_key in s3.objects

    stale_time = DeleteRequest(
        object_key=candidate.key,
        etag=candidate.etag,
        last_modified=candidate.last_modified.replace(year=2025),
    )
    with pytest.raises(MaskContentFailure) as caught:
        store.delete(stale_time)
    assert caught.value.code == "content_changed"
    assert ref.object_key in s3.objects

    s3.objects.pop(ref.object_key)
    assert (
        store.delete(
            DeleteRequest(
                object_key=candidate.key,
                etag=candidate.etag,
                last_modified=candidate.last_modified,
            )
        ).deleted
        is False
    )


def test_listing_skips_keys_outside_the_canonical_mask_namespace():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    valid = codec.build_rle_reference(_rle())
    s3.objects[valid.object_key] = codec.canonical_rle_bytes(_rle())
    s3.objects["raster-masks/sha256/aa/bb/not-a-digest.json"] = b"ignored"

    page = store.list(ListRequest(limit=10))

    assert [item.key for item in page.items] == [valid.object_key]


def test_delete_keeps_changed_content_when_replaced_after_head():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    reference = store.put(PutRequest(content=_rle())).reference
    candidate = store.list(ListRequest()).items[0]
    delete = s3.delete_object

    def replace_then_delete(**kwargs):
        s3.objects[reference.object_key] = b"replacement"
        return delete(**kwargs)

    s3.delete_object = replace_then_delete
    with pytest.raises(MaskContentFailure) as caught:
        store.delete(
            DeleteRequest(
                object_key=candidate.key,
                etag=candidate.etag,
                last_modified=candidate.last_modified,
            )
        )
    assert caught.value.code == "content_changed"
    assert s3.objects[reference.object_key] == b"replacement"


def test_delete_accepts_list_timestamp_at_head_http_date_precision():
    s3 = FakeS3()
    s3.last_modified = s3.last_modified.replace(microsecond=123456)
    store = S3MaskContentStore(s3, "masks")
    store.put(PutRequest(content=_rle()))
    candidate = store.list(ListRequest()).items[0]
    assert candidate.last_modified.microsecond == 123456
    assert (
        store.delete(
            DeleteRequest(
                object_key=candidate.key,
                etag=candidate.etag,
                last_modified=candidate.last_modified,
            )
        ).deleted
        is True
    )


def test_listing_uses_bounded_provider_continuation_pages():
    s3 = FakeS3()
    store = S3MaskContentStore(s3, "masks")
    for counts in ((1, 3), (2, 2)):
        content = RleContent(encoding="coco_rle", size=(2, 2), counts=counts)
        reference = codec.build_rle_reference(content)
        s3.objects[reference.object_key] = codec.canonical_rle_bytes(content)

    first = store.list(ListRequest(limit=1))
    second = store.list(ListRequest(cursor=first.cursor, limit=1))

    assert len(first.items) == len(second.items) == 1
    assert first.cursor is not None
    assert second.cursor is None
    assert s3.list_limits == [1, 1]


def test_readiness_uses_bucket_location_without_listing_objects():
    s3 = FakeS3()
    S3MaskContentStore(s3, "masks").check_ready()
    assert s3.calls == [("ready", "masks")]
