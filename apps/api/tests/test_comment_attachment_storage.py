"""Strict attachment ownership and bounded I/O, with no database or S3 access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
import pytest

from app.config import settings
from app.services import comment_attachment_storage as owners
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

OWNER, UPLOAD = uuid.UUID(int=12), uuid.UUID(int=14)
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


ANNOTATION, TASK, PROJECT = uuid.UUID(int=16), uuid.UUID(int=18), uuid.UUID(int=20)
RAW = b"general binary\x00payload\xff"
KEY = owners.key_for(ANNOTATION, UPLOAD)


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(
    *,
    bucket="attachments-original",
    upload_id=UPLOAD,
    annotation_id=ANNOTATION,
):
    return {
        "schema": 1,
        "purpose": "annotations",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "object_key": owners.key_for(annotation_id, upload_id),
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def record(**changes):
    return SimpleNamespace(
        **{
            "id": UPLOAD,
            "owner_id": OWNER,
            "annotation_id": ANNOTATION,
            "task_id": TASK,
            "project_id": PROJECT,
            "bound_comment_id": None,
            "mime_type": "image/png",
            "file_name": "screen.png",
            "created_at": CREATED,
            "expires_at": CREATED + timedelta(days=90),
            "storage_ref": reference(),
            "sha256": hashlib.sha256(RAW).hexdigest(),
            "size_bytes": len(RAW),
            **changes,
        }
    )


@pytest.fixture
def storage(monkeypatch):
    service = SimpleNamespace(
        bucket="attachments-current",
        client=SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint())),
    )
    monkeypatch.setattr(owners, "storage_service", service)
    return service


def test_saved_owner_survives_current_bucket_change_and_is_copied(storage):
    row = record()
    owner = owners.resolve(row)
    assert owner.bucket == "attachments-original"
    assert owner.object_key == KEY
    row.storage_ref["bucket"] = "changed-after-resolution"
    exposed = owner.ref
    exposed["bucket"] = "changed-returned-copy"
    assert owner.ref == reference()
    with pytest.raises(AttributeError):
        owner.bucket = "forged"


@pytest.mark.parametrize("bad", [None, {}, [], True, "text"])
def test_missing_or_invalid_envelope_is_rejected(storage, bad):
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.resolve_ref(bad)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", "1"),
        ("schema", 1.0),
        ("purpose", "training-run"),
        ("location_id", str(uuid.UUID(int=1))),
        ("config_revision", 2),
        ("config_revision", True),
        ("config_revision", 1.0),
        ("binding_generation", "1"),
        ("binding_generation", 1.0),
        ("bucket", ""),
        ("bucket", "bad/path"),
        ("bucket", "bad\\path"),
        ("bucket", "bad bucket"),
        ("bucket", "bad@bucket"),
        ("object_key", "../attachment.png"),
        ("object_key", KEY.upper()),
        ("storage_identity", "A" * 64),
        ("storage_identity", None),
        ("endpoint_url", "http://user:secret@storage.invalid"),
        ("endpoint_url", "http://storage.invalid/path"),
        ("endpoint_url", "http://storage.invalid/"),
        ("endpoint_url", "http://storage.invalid:99999"),
        ("endpoint_url", "http://storage.invalid\\path"),
        ("credential", "secret"),
    ],
)
def test_invalid_physical_locator_never_reaches_io(storage, field, value):
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.resolve_ref({**reference(), field: value})
    assert caught.value.code == "comment_attachment_storage_identity_changed"
    assert "secret" not in str(caught.value)


def test_actual_client_endpoint_and_saved_identity_are_checked(storage):
    storage.client.meta.endpoint_url = "http://another-endpoint.invalid"
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.resolve(record())
    storage.client.meta.endpoint_url = endpoint()
    ref = reference()
    ref["bucket"] = "forged-with-old-identity"
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.resolve_ref(ref)


@pytest.mark.parametrize(
    "bad", [None, True, 12, "", str(UPLOAD).upper(), UPLOAD.hex, "../x"]
)
def test_key_requires_canonical_uuid(bad):
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.key_for(ANNOTATION, bad)
    assert caught.value.code == "comment_attachment_storage_invalid_key"


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": ""},
        {"sha256": "A" * 64},
        {"sha256": 1},
        {"size_bytes": True},
        {"size_bytes": 1.0},
        {"size_bytes": -1},
        {"size_bytes": owners.MAX_BYTES + 1},
        {"file_name": ""},
        {"file_name": "../screen.png"},
        {"file_name": "screen\n.png"},
        {"created_at": CREATED.replace(tzinfo=None)},
        {"expires_at": CREATED},
        {"expires_at": CREATED + timedelta(days=90, seconds=1)},
    ],
)
def test_receipt_and_fixed_retention_are_strict(storage, changes):
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.resolve(record(**changes))
    assert caught.value.code == "comment_attachment_storage_invalid_receipt"


def allocation_db(storage):
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    binding = SimpleNamespace(
        purpose="annotations",
        location_id=LEGACY_LOCATION_ID,
        generation=1,
        bucket=storage.bucket,
    )
    return (
        SimpleNamespace(
            get=AsyncMock(return_value=location), scalar=AsyncMock(return_value=binding)
        ),
        location,
        binding,
    )


async def test_allocate_proves_current_binding_without_mutating_upload(
    storage, monkeypatch
):
    db, _, _ = allocation_db(storage)
    ensure = AsyncMock()
    monkeypatch.setattr(owners, "ensure_legacy_bindings", ensure)
    row = record()
    assert await owners.allocate(db, row) == reference(bucket=storage.bucket)
    assert row.storage_ref == reference()
    ensure.assert_awaited_once_with(db)
    statement = db.scalar.await_args.args[0]
    assert statement.compile().params == {"purpose_1": "annotations"}


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("location", "id", uuid.UUID(int=1)),
        ("location", "kind", "filesystem"),
        ("location", "enabled", False),
        ("location", "enabled", 1),
        ("location", "config_revision", 2),
        ("location", "config_revision", True),
        ("location", "config", {"endpoint": "other.invalid", "use_ssl": False}),
        (
            "location",
            "config",
            {
                "endpoint": settings.minio_endpoint,
                "use_ssl": int(settings.minio_use_ssl),
            },
        ),
        ("binding", "location_id", uuid.UUID(int=2)),
        ("binding", "generation", 2),
        ("binding", "generation", True),
        ("binding", "bucket", "historical-only"),
    ],
)
async def test_allocate_rejects_changed_binding(
    storage, monkeypatch, target, field, value
):
    db, location, binding = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    setattr(location if target == "location" else binding, field, deepcopy(value))
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        await owners.allocate(db, record())
    assert caught.value.code == "comment_attachment_storage_binding_unproven"


@pytest.mark.parametrize("missing", ["location", "binding"])
async def test_allocate_rejects_missing_binding(storage, monkeypatch, missing):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    (db.get if missing == "location" else db.scalar).return_value = None
    with pytest.raises(owners.CommentAttachmentStorageError):
        await owners.allocate(db, record())


async def test_allocate_exposes_no_bootstrap_details(storage, monkeypatch):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(
        owners, "ensure_legacy_bindings", AsyncMock(side_effect=ValueError("secret"))
    )
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        await owners.allocate(db, record())
    assert "secret" not in str(caught.value)
    db.get.assert_not_awaited()


def readable(storage, data=RAW):
    body = io.BytesIO(data)
    metadata = {"ContentLength": len(RAW), "ETag": '"upload-version"'}
    storage.client.head_object = Mock(return_value=metadata)
    storage.client.get_object = Mock(return_value={**metadata, "Body": body})
    storage.client.put_object = Mock()
    storage.client.delete_object = Mock()

    return body


def s3_error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret"}}, "GetObject")


def test_verify_reads_full_saved_object_conditionally_and_closes(storage):
    body = readable(storage)
    assert owners.read(storage.client, record()) == RAW
    assert body.closed
    storage.client.get_object.assert_called_once_with(
        Bucket="attachments-original", Key=KEY, IfMatch='"upload-version"'
    )
    assert storage.client.head_object.call_count == 2


@pytest.mark.parametrize("data", [b"x" * len(RAW), RAW[:-1], RAW + b"x"])
def test_corrupt_truncated_or_extra_bytes_are_closed_and_rejected(storage, data):
    body = readable(storage, data)
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record(), missing_ok=True)
    assert caught.value.code == "comment_attachment_storage_integrity_mismatch"
    assert body.closed


def test_unknown_close_outcome_is_unsettled_and_never_a_cache_miss(storage):
    class FailingClose(io.BytesIO):
        def close(self):
            already_closed = self.closed
            super().close()
            if not already_closed:
                raise s3_error("NoSuchKey")

    readable(storage)
    body = FailingClose(RAW)
    storage.client.get_object.return_value["Body"] = body
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record(), missing_ok=True)
    assert body.closed
    assert caught.value.code == "comment_attachment_storage_unavailable"
    assert caught.value.storage_operation_unsettled is True
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("missing_ok", [False, True])
def test_only_actual_missing_can_be_safe_absence(storage, missing_ok):
    readable(storage)
    storage.client.head_object.side_effect = s3_error("404")
    if missing_ok:
        assert owners.read(storage.client, record(), missing_ok=True) is None
    else:
        with pytest.raises(owners.CommentAttachmentStorageError) as caught:
            owners.read(storage.client, record())
        assert caught.value.code == "comment_attachment_storage_missing"
        assert caught.value.status_code == 404
    storage.client.get_object.assert_not_called()


@pytest.mark.parametrize("code", ["412", "AccessDenied"])
def test_conditional_conflict_and_access_failure_are_not_missing(storage, code):
    readable(storage)
    storage.client.get_object.side_effect = s3_error(code)
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record(), missing_ok=True)
    assert caught.value.code == (
        "comment_attachment_storage_integrity_mismatch"
        if code == "412"
        else "comment_attachment_storage_unavailable"
    )
    assert "secret" not in str(caught.value)


def test_post_read_object_version_change_is_rejected(storage):
    body = readable(storage)
    storage.client.head_object.side_effect = [
        {"ContentLength": len(RAW), "ETag": '"upload-version"'},
        {"ContentLength": len(RAW), "ETag": '"changed-version"'},
    ]
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record())
    assert caught.value.code == "comment_attachment_storage_integrity_mismatch"
    assert body.closed


def test_deadline_prevents_download_after_slow_head(storage, monkeypatch):
    readable(storage)
    times = iter([0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record())
    assert caught.value.code == "comment_attachment_storage_budget"
    storage.client.get_object.assert_not_called()


def test_deadline_during_stream_still_closes_body(storage, monkeypatch):
    body = readable(storage)
    times = iter([0, 0, 0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record())
    assert caught.value.code == "comment_attachment_storage_budget"
    assert body.closed


def test_streaming_never_requests_unbounded_or_full_object_read(storage, monkeypatch):
    class TrackingBody(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 4
            return super().read(size)

    readable(storage)
    body = TrackingBody(RAW)
    storage.client.get_object.return_value["Body"] = body
    monkeypatch.setattr(owners, "CHUNK_BYTES", 4)
    assert owners.read(storage.client, record()) == RAW
    assert body.closed


def test_write_uses_conditional_create_and_full_saved_read(storage):
    body = readable(storage)
    owners.write(storage.client, record(), RAW)
    storage.client.put_object.assert_called_once_with(
        Bucket="attachments-original",
        Key=KEY,
        Body=RAW,
        ContentType="image/png",
        ContentLength=len(RAW),
        IfNoneMatch="*",
    )
    assert body.closed
    assert storage.client.head_object.call_count == 2
    storage.client.delete_object.assert_not_called()


def test_local_content_change_is_rejected_before_upload(storage):
    readable(storage)
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.write(storage.client, record(), b"changed")
    assert caught.value.code == "comment_attachment_storage_integrity_mismatch"
    storage.client.put_object.assert_not_called()
    storage.client.delete_object.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("secret"), s3_error("412")])
def test_upload_failure_never_deletes_or_falls_back(storage, failure):
    readable(storage)
    storage.client.put_object.side_effect = failure
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.write(storage.client, record(), RAW)
    assert caught.value.code == (
        "comment_attachment_storage_integrity_mismatch"
        if isinstance(failure, ClientError)
        else "comment_attachment_storage_unavailable"
    )
    assert "secret" not in str(caught.value)
    storage.client.get_object.assert_not_called()
    storage.client.delete_object.assert_not_called()


def test_delete_resolves_receipt_and_exact_saved_owner(storage):
    readable(storage)
    owners.delete(storage.client, record())
    storage.client.delete_object.assert_called_once_with(
        Bucket="attachments-original", Key=KEY
    )
    storage.client.delete_object.reset_mock()
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.delete(storage.client, record(sha256="broken"))
    storage.client.delete_object.assert_not_called()


def test_forged_owner_is_not_an_output_receipt(storage):
    readable(storage)
    forged = owners.Owner("forged-bucket", KEY, reference())
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.delete(storage.client, forged)
    storage.client.delete_object.assert_not_called()


def test_code_only_errors_have_safe_default_text():
    error = owners.CommentAttachmentStorageError("comment_attachment_storage_missing")
    assert error.code == "comment_attachment_storage_missing"
    assert error.status_code == 409
    assert str(error)


@pytest.mark.parametrize(
    "name",
    [
        None,
        1,
        "",
        ".",
        "..",
        "../screen.png",
        "a/b.png",
        "a\\b.png",
        "screen\n.png",
        "screen\x7f.png",
        "screen\x85.png",
        "x" * 256,
    ],
)
def test_file_names_reject_paths_and_control_characters(name):
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.safe_file_name(name)
    assert caught.value.code == "comment_attachment_storage_invalid_file_name"
    assert caught.value.status_code == 422


def test_safe_unicode_name_and_limit_are_preserved():
    assert owners.safe_file_name("截图 原件.png") == "截图 原件.png"
    assert owners.safe_file_name("图" * 255) == "图" * 255


@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
def test_io_service_does_not_make_workflow_state_decisions(storage, state):
    readable(storage)
    assert owners.read(storage.client, record(state=state)) == RAW


@pytest.mark.parametrize("error_code", ["NoSuchKey", "412"])
def test_unknown_close_cannot_become_missing_or_integrity_fallback(storage, error_code):
    class FailingClose(io.BytesIO):
        def close(self):
            was_closed = self.closed
            super().close()
            if not was_closed:
                raise s3_error(error_code)

    readable(storage)
    storage.client.get_object.return_value["Body"] = FailingClose(b"broken")
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record(), missing_ok=True)
    assert caught.value.code == "comment_attachment_storage_unavailable"
    assert caught.value.storage_operation_unsettled is True


def test_saved_bucket_read_never_touches_same_key_in_current_bucket(storage):
    readable(storage)
    assert owners.read(storage.client, record()) == RAW
    assert all(
        call.kwargs == {"Bucket": "attachments-original", "Key": KEY}
        for call in storage.client.head_object.call_args_list
    )


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (None, "application/octet-stream"),
        ("", "application/octet-stream"),
        ("   ", "application/octet-stream"),
        (" Text/Plain ; charset=utf-8", "text/plain"),
        ("application/vnd.example+json", "application/vnd.example+json"),
        ("image/tiff", "image/tiff"),
        ("video/mp4", "video/mp4"),
        ("x!#$%&'*+.^_`|~-/y!#$%&'*+.^_`|~-", "x!#$%&'*+.^_`|~-/y!#$%&'*+.^_`|~-"),
        ("x/" + "A" * 126, "x/" + "a" * 126),
        ("text/plain; description=" + "x" * 200, "text/plain"),
    ],
)
def test_general_mime_is_normalized_without_an_image_allowlist(raw, normalized):
    assert owners.normalize_mime_type(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        1,
        True,
        b"text/plain",
        "text",
        "/plain",
        "text/",
        "text/plain/extra",
        "text/pl ain",
        "text/plain\n",
        "text/plain; charset=\x85",
        "文本/plain",
        "text/(plain)",
        "; charset=utf-8",
        "x/" + "a" * 127,
    ],
)
def test_mime_rejects_controls_invalid_tokens_and_normalized_overflow(raw):
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.normalize_mime_type(raw)
    assert caught.value.code == "comment_attachment_storage_invalid_mime_type"
    assert caught.value.status_code == 422


@pytest.mark.parametrize("size", [0, owners.MAX_BYTES])
def test_empty_and_twenty_mib_general_bytes_roundtrip_without_transformation(
    storage, size
):
    data = b"\xff" * size
    body = readable(storage, data)
    metadata = {"ContentLength": size, "ETag": '"general-file"'}
    storage.client.head_object.return_value = metadata
    storage.client.get_object.return_value = {**metadata, "Body": body}
    receipt = record(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=size,
        mime_type="application/octet-stream",
        file_name="empty-or-binary.bin",
    )
    owners.write(storage.client, receipt, data)
    assert storage.client.put_object.call_args.kwargs["Body"] is data
    assert storage.client.put_object.call_args.kwargs["ContentLength"] == size
    assert body.closed
    assert storage.client.delete_object.call_count == 0


def test_oversized_receipt_is_rejected_before_any_physical_io(storage):
    readable(storage)
    data = b"x" * (owners.MAX_BYTES + 1)
    row = record(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.write(storage.client, row, data)
    storage.client.put_object.assert_not_called()
    storage.client.head_object.assert_not_called()


def test_key_is_bound_to_annotation_and_independent_upload_id(storage):
    assert owners.key_for(ANNOTATION, uuid.uuid4()) != KEY
    assert owners.key_for(uuid.uuid4(), UPLOAD) != KEY
    for changes in ({"id": uuid.uuid4()}, {"annotation_id": uuid.uuid4()}):
        with pytest.raises(owners.CommentAttachmentStorageError):
            owners.resolve(record(**changes))
    assert (
        owners.resolve(
            record(file_name="different.txt", mime_type="text/plain")
        ).object_key
        == KEY
    )


def test_snapshot_is_immutable_and_ignores_only_mutable_workflow_facts(storage):
    row = record(state="ready", retired_at=None, last_attempted_at=None)
    receipt = owners.snapshot(row)
    assert owners.same(row, receipt)
    row.state, row.retired_at, row.last_attempted_at = "retired", CREATED, CREATED
    assert owners.same(row, receipt)
    assert not hasattr(receipt, "state")
    with pytest.raises(AttributeError):
        receipt.owner_id = uuid.uuid4()
    exposed = receipt.storage_ref
    exposed["bucket"] = "trap"
    assert receipt.storage_ref == reference()
    row.storage_ref["bucket"] = "another-trap"
    assert not owners.same(row, receipt)
    assert receipt.storage_ref == reference()


@pytest.mark.parametrize("name", owners._FACTS)
def test_snapshot_compares_every_independent_fact(storage, name):
    row = record()
    receipt = owners.snapshot(row)
    setattr(row, name, object())
    assert not owners.same(row, receipt)


def test_explicit_client_endpoint_is_checked_before_io(storage):
    readable(storage)
    client = SimpleNamespace(
        meta=SimpleNamespace(endpoint_url="http://another.invalid"),
        head_object=Mock(),
        get_object=Mock(),
        put_object=Mock(),
        delete_object=Mock(),
    )
    for action in (
        lambda: owners.read(client, record()),
        lambda: owners.write(client, record(), RAW),
        lambda: owners.delete(client, record()),
    ):
        with pytest.raises(owners.CommentAttachmentStorageError):
            action()
    client.head_object.assert_not_called()
    client.get_object.assert_not_called()
    client.put_object.assert_not_called()
    client.delete_object.assert_not_called()


def test_real_version_id_is_part_of_conditional_get_and_rechecked(storage):
    body = readable(storage)
    storage.client.head_object.return_value["VersionId"] = "saved-version"
    storage.client.get_object.return_value["VersionId"] = "saved-version"
    assert owners.read(storage.client, record()) == RAW
    assert body.closed
    storage.client.get_object.assert_called_once_with(
        Bucket="attachments-original",
        Key=KEY,
        IfMatch='"upload-version"',
        VersionId="saved-version",
    )


@pytest.mark.parametrize("changed_at", ["response", "final_head"])
def test_version_change_is_closed_and_rejected(storage, changed_at):
    body = readable(storage)
    original = {
        "ContentLength": len(RAW),
        "ETag": '"upload-version"',
        "VersionId": "first",
    }
    changed = {**original, "VersionId": "second"}
    storage.client.head_object.side_effect = [
        original,
        changed if changed_at == "final_head" else original,
    ]
    storage.client.get_object.return_value = {
        **(changed if changed_at == "response" else original),
        "Body": body,
    }
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record())
    assert caught.value.code == "comment_attachment_storage_integrity_mismatch"
    assert body.closed


@pytest.mark.parametrize("version", ["", True, 123, "has space", "x" * 1025])
def test_malformed_version_is_rejected_before_download(storage, version):
    readable(storage)
    storage.client.head_object.return_value["VersionId"] = version
    with pytest.raises(owners.CommentAttachmentStorageError):
        owners.read(storage.client, record())
    storage.client.get_object.assert_not_called()


@pytest.mark.parametrize("bad", [None, "", b"bytes", bytearray(b"x")])
def test_stream_must_return_bytes_including_empty_eof(storage, bad):
    readable(storage)
    body = SimpleNamespace(read=Mock(return_value=bad), close=Mock())
    storage.client.get_object.return_value["Body"] = body
    with pytest.raises(owners.CommentAttachmentStorageError) as caught:
        owners.read(storage.client, record())
    assert caught.value.code == "comment_attachment_storage_integrity_mismatch"
    body.close.assert_called_once()
