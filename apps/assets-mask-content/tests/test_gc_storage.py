from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import md5
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

from botocore.exceptions import ClientError, EndpointConnectionError
import pytest

from aap_assets_mask_content import codec
from aap_assets_mask_content.gc_contract import (
    DeleteOperationRequest,
    DeleteStatusRequest,
    GcScanRequest,
    MaskGcFailure,
    json_bytes,
)
from aap_assets_mask_content.gc_storage import S3MaskGcStore

_NOW = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)
_OPERATION_ID = UUID("2d639c8a-0fd8-4dcb-9f90-26cb8a752f3a")


def _s3_error(code, operation, status=None):
    response = {"Error": {"Code": code}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return ClientError(response, operation)


class SafeConfig:
    connect_timeout = 3
    read_timeout = 8
    retries = {"total_max_attempts": 1}


class FakeS3:
    def __init__(self):
        self.meta = SimpleNamespace(config=SafeConfig())
        self.objects = {}
        self.calls = []
        self.delete_attempts = 0
        self.delete_conditions = []
        self.payload_head_count = 0
        self.fail_next_delete_reply = False
        self.fail_next_receipt_put = False
        self.lose_next_receipt_reply = False
        self.change_before_delete = False

    def seed(self, key, body, *, etag=None, last_modified=_NOW):
        self.objects[key] = {
            "Body": bytes(body),
            "ETag": etag or md5(body).hexdigest(),
            "LastModified": last_modified,
        }

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        self.calls.append(("put", Key))
        if self.fail_next_receipt_put and Key.endswith("/receipt.json"):
            self.fail_next_receipt_put = False
            raise EndpointConnectionError(endpoint_url="https://storage.invalid")
        if IfNoneMatch != "*":
            raise AssertionError("journal write must be conditional")
        if Key in self.objects:
            raise _s3_error("PreconditionFailed", "PutObject", 412)
        self.seed(Key, Body)
        if self.lose_next_receipt_reply and Key.endswith("/receipt.json"):
            self.lose_next_receipt_reply = False
            raise EndpointConnectionError(endpoint_url="https://storage.invalid")

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        value = self.objects.get(Key)
        if value is None:
            raise _s3_error("NoSuchKey", "GetObject", 404)
        return {"Body": BytesIO(value["Body"])}

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Key))
        if not Key.startswith("raster-mask-gc/"):
            self.payload_head_count += 1
        value = self.objects.get(Key)
        if value is None:
            raise _s3_error("NoSuchKey", "HeadObject", 404)
        return {
            "ETag": f'"{value["ETag"]}"',
            "LastModified": value["LastModified"],
            "ContentLength": len(value["Body"]),
        }

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, **kwargs):
        self.calls.append(("list", kwargs.get("StartAfter")))
        keys = sorted(
            key
            for key in self.objects
            if key.startswith(Prefix) and key > kwargs.get("StartAfter", "")
        )
        page = keys[:MaxKeys]
        contents = []
        for key in page:
            item = self.objects[key]
            contents.append(
                {
                    "Key": key,
                    "ETag": f'"{item["ETag"]}"',
                    "LastModified": item["LastModified"],
                    "Size": len(item["Body"]),
                }
            )
        return {"Contents": contents, "IsTruncated": len(keys) > MaxKeys}

    def delete_object(
        self,
        *,
        Bucket,
        Key,
        IfMatch,
    ):
        self.calls.append(("delete", Key))
        self.delete_attempts += 1
        self.delete_conditions.append(IfMatch)
        if Key not in self.objects:
            raise _s3_error("NoSuchKey", "DeleteObject", 404)
        current = self.objects[Key]
        if self.change_before_delete:
            self.change_before_delete = False
            current["ETag"] = "new-etag"
            current["LastModified"] = current["LastModified"].replace(
                second=current["LastModified"].second + 1
            )
        same_identity = current["ETag"] == IfMatch
        if not same_identity:
            raise _s3_error("PreconditionFailed", "DeleteObject", 412)
        del self.objects[Key]
        if self.fail_next_delete_reply:
            self.fail_next_delete_reply = False
            raise EndpointConnectionError(endpoint_url="https://storage.invalid")
        return {}

    def get_bucket_location(self, *, Bucket):
        self.calls.append(("ready", Bucket))
        return {"LocationConstraint": None}


def _request(s3, *, key=None, operation_id=_OPERATION_ID):
    object_key = key or codec.rle_object_key("a" * 64)
    value = s3.objects[object_key]
    return DeleteOperationRequest(
        operation_id=operation_id,
        object_key=object_key,
        etag=value["ETag"],
        last_modified=value["LastModified"],
    )


def _store(s3):
    return S3MaskGcStore(s3, "masks")


def test_scan_advances_over_invalid_physical_keys():
    s3 = FakeS3()
    invalid = "raster-masks/sha256/aa/aa/old-file.data"
    valid = codec.rle_object_key("b" * 64)
    s3.seed(invalid, b"old")
    s3.seed(valid, b"mask")

    first = _store(s3).scan(GcScanRequest(limit=1))
    second = _store(s3).scan(GcScanRequest(after_key=first.after_key, limit=1))

    assert first.items == ()
    assert first.after_key == invalid
    assert not first.complete
    assert [item.object_key for item in second.items] == [valid]
    assert second.complete
    assert s3.calls[0] == ("list", None)
    assert s3.calls[1] == ("list", invalid)


def test_execute_writes_immutable_receipt_and_replay_never_deletes_twice():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    store = _store(s3)

    completed = store.execute(request)
    replay = store.execute(request)

    assert completed.state == "completed"
    assert completed.outcome == "deleted"
    assert replay == completed
    assert s3.delete_attempts == 1
    assert s3.delete_conditions == [request.etag]
    assert s3.objects == {
        f"raster-mask-gc/v1/{_OPERATION_ID}/request.json": s3.objects[
            f"raster-mask-gc/v1/{_OPERATION_ID}/request.json"
        ],
        f"raster-mask-gc/v1/{_OPERATION_ID}/receipt.json": s3.objects[
            f"raster-mask-gc/v1/{_OPERATION_ID}/receipt.json"
        ],
    }
    assert all(
        key.endswith("request.json") or key.endswith("receipt.json")
        for operation, key in s3.calls
        if operation == "put"
    )


def test_operation_id_conflict_is_closed_and_does_not_delete():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    _store(s3).execute(request)
    conflict = request.model_copy(update={"etag": "different-etag"})

    with pytest.raises(MaskGcFailure) as caught:
        _store(s3).execute(conflict)

    assert caught.value.code == "idempotency_conflict"
    assert s3.delete_attempts == 1


def test_existing_request_marker_is_never_reexecuted():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    request_key = f"raster-mask-gc/v1/{_OPERATION_ID}/request.json"
    s3.seed(request_key, json_bytes(request))

    replay = _store(s3).execute(request)

    assert replay.state == "indeterminate"
    assert replay.request == request
    assert s3.delete_attempts == 0
    assert key in s3.objects


def test_status_reloads_completed_receipt_without_reading_payload():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    expected = _store(s3).execute(request)
    s3.calls.clear()
    s3.payload_head_count = 0

    actual = _store(s3).status(DeleteStatusRequest(operation_id=_OPERATION_ID))

    assert actual == expected
    assert s3.payload_head_count == 0
    assert all(key.startswith("raster-mask-gc/") for operation, key in s3.calls)


def test_ambiguous_delete_is_never_retried_or_settled_by_missing_payload():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    s3.fail_next_delete_reply = True

    with pytest.raises(MaskGcFailure) as caught:
        _store(s3).execute(request)
    assert caught.value.code == "storage_unavailable"
    assert s3.delete_attempts == 1

    replay = _store(s3).execute(request)
    status = _store(s3).status(DeleteStatusRequest(operation_id=_OPERATION_ID))

    assert replay.state == "indeterminate"
    assert replay.outcome is None
    assert status == replay
    assert s3.delete_attempts == 1


def test_changed_payload_gets_terminal_receipt_without_delete_attempt():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload", etag="new-etag")
    request = DeleteOperationRequest(
        operation_id=_OPERATION_ID,
        object_key=key,
        etag="old-etag",
        last_modified=_NOW,
    )

    result = _store(s3).execute(request)

    assert result.state == "completed"
    assert result.outcome == "content_changed"
    assert s3.delete_attempts == 0
    assert key in s3.objects


def test_head_timestamp_mismatch_at_http_date_precision_skips_delete():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload", last_modified=_NOW + timedelta(seconds=1))
    request = DeleteOperationRequest(
        operation_id=_OPERATION_ID,
        object_key=key,
        etag=s3.objects[key]["ETag"],
        last_modified=_NOW,
    )

    result = _store(s3).execute(request)

    assert result.outcome == "content_changed"
    assert s3.delete_attempts == 0
    assert key in s3.objects


def test_conditional_delete_precondition_mismatch_gets_terminal_receipt():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    s3.change_before_delete = True

    result = _store(s3).execute(request)

    assert result.state == "completed"
    assert result.outcome == "content_changed"
    assert s3.delete_attempts == 1
    assert key in s3.objects


def test_missing_payload_gets_terminal_receipt_without_delete_attempt():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    request = DeleteOperationRequest(
        operation_id=_OPERATION_ID,
        object_key=key,
        etag="old-etag",
        last_modified=_NOW,
    )

    result = _store(s3).execute(request)

    assert result.state == "completed"
    assert result.outcome == "missing"
    assert s3.delete_attempts == 0


def test_unsafe_sdk_config_is_rejected_before_claiming_operation():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    s3.meta.config = SimpleNamespace(
        connect_timeout=3,
        read_timeout=8,
        retries={"total_max_attempts": 2},
    )

    with pytest.raises(MaskGcFailure) as caught:
        _store(s3).execute(request)

    assert caught.value.code == "storage_unavailable"
    assert s3.calls == []
    assert s3.delete_attempts == 0


def test_missing_receipt_remains_indeterminate_after_restart():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    s3.fail_next_receipt_put = True

    with pytest.raises(MaskGcFailure) as caught:
        _store(s3).execute(request)

    assert caught.value.code == "storage_unavailable"
    assert s3.delete_attempts == 1
    assert key not in s3.objects
    recovered = _store(s3).status(DeleteStatusRequest(operation_id=_OPERATION_ID))
    assert recovered.state == "indeterminate"
    assert recovered.outcome is None
    assert s3.delete_attempts == 1


def test_durable_receipt_survives_its_lost_put_reply_and_process_restart():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)
    s3.lose_next_receipt_reply = True
    with pytest.raises(MaskGcFailure):
        _store(s3).execute(request)
    s3.payload_head_count = 0
    recovered = _store(s3).status(DeleteStatusRequest(operation_id=_OPERATION_ID))
    assert recovered.state == "completed"
    assert recovered.outcome == "deleted"
    assert s3.delete_attempts == 1
    assert s3.payload_head_count == 0


def test_client_error_outside_definitive_delete_outcomes_stays_unresolved():
    s3 = FakeS3()
    key = codec.rle_object_key("a" * 64)
    s3.seed(key, b"payload")
    request = _request(s3, key=key)

    def fail_without_deleting(**kwargs):
        s3.delete_attempts += 1
        raise _s3_error("InternalError", "DeleteObject", 500)

    s3.delete_object = fail_without_deleting
    with pytest.raises(MaskGcFailure) as caught:
        _store(s3).execute(request)

    assert caught.value.code == "storage_unavailable"
    assert s3.delete_attempts == 1
    assert key in s3.objects
    unresolved = _store(s3).status(DeleteStatusRequest(operation_id=_OPERATION_ID))
    assert unresolved.state == "indeterminate"
    assert unresolved.outcome is None
