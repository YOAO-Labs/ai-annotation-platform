"""Strict attachment ownership and bounded I/O, with no database or S3 access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
from PIL import Image
import pytest

from app.config import settings
from app.services import bug_attachment_storage as owners
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

OWNER, UPLOAD = uuid.UUID(int=12), uuid.UUID(int=14)
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def image_bytes(image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format=image_format)
    return output.getvalue()


RAW = image_bytes()
KEY = owners.key_for(OWNER, UPLOAD, "image/png")


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(
    *,
    bucket="attachments-original",
    upload_id=UPLOAD,
    owner_id=OWNER,
    mime_type="image/png",
):
    return {
        "schema": 1,
        "purpose": "bug-reports",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "object_key": owners.key_for(owner_id, upload_id, mime_type),
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def record(**changes):
    return SimpleNamespace(
        **{
            "id": UPLOAD,
            "owner_id": OWNER,
            "mime_type": "image/png",
            "file_name": "screen.png",
            "created_at": CREATED,
            "expires_at": CREATED + timedelta(days=180),
            "storage_ref": reference(),
            "sha256": hashlib.sha256(RAW).hexdigest(),
            "size_bytes": len(RAW),
            **changes,
        }
    )


@pytest.fixture
def storage(monkeypatch):
    service = SimpleNamespace(
        bug_reports_bucket="attachments-current",
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
    with pytest.raises(owners.BugAttachmentStorageError):
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
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.resolve_ref({**reference(), field: value})
    assert caught.value.code == "bug_attachment_storage_identity_changed"
    assert "secret" not in str(caught.value)


def test_actual_client_endpoint_and_saved_identity_are_checked(storage):
    storage.client.meta.endpoint_url = "http://another-endpoint.invalid"
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.resolve(record())
    storage.client.meta.endpoint_url = endpoint()
    ref = reference()
    ref["bucket"] = "forged-with-old-identity"
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.resolve_ref(ref)


def test_saved_key_is_bound_to_all_upload_identifiers(storage):
    for changes in (
        {"id": uuid.uuid4()},
        {"owner_id": uuid.uuid4()},
        {"mime_type": "image/webp"},
    ):
        with pytest.raises(owners.BugAttachmentStorageError):
            owners.resolve(record(**changes))
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.resolve_ref(reference(), expected_key="unrelated.png")


@pytest.mark.parametrize(
    "bad", [None, True, 12, "", str(UPLOAD).upper(), UPLOAD.hex, "../x"]
)
def test_key_requires_canonical_uuid(bad):
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.key_for(OWNER, bad, "image/png")
    assert caught.value.code == "bug_attachment_storage_invalid_key"


@pytest.mark.parametrize("mime_type", [None, 1, "image/jpg", "IMAGE/PNG", "../image"])
def test_key_requires_exact_supported_mime_type(mime_type):
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.key_for(OWNER, UPLOAD, mime_type)


def test_each_retry_owns_an_independent_key():
    assert owners.key_for(OWNER, uuid.uuid4(), "image/png") != KEY
    assert owners.key_for(str(OWNER), str(UPLOAD), "image/jpeg").endswith(
        f"/{UPLOAD}.jpg"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": ""},
        {"sha256": "A" * 64},
        {"sha256": 1},
        {"size_bytes": True},
        {"size_bytes": 1.0},
        {"size_bytes": 0},
        {"size_bytes": owners.MAX_BYTES + 1},
        {"file_name": ""},
        {"file_name": "../screen.png"},
        {"file_name": "screen\n.png"},
        {"created_at": CREATED.replace(tzinfo=None)},
        {"expires_at": CREATED},
        {"expires_at": CREATED + timedelta(days=180, seconds=1)},
    ],
)
def test_receipt_and_fixed_retention_are_strict(storage, changes):
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.resolve(record(**changes))
    assert caught.value.code == "bug_attachment_storage_invalid_receipt"


def allocation_db(storage):
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    binding = SimpleNamespace(
        purpose="bug-reports",
        location_id=LEGACY_LOCATION_ID,
        generation=1,
        bucket=storage.bug_reports_bucket,
    )

    async def scalar(statement):
        from app.db.models.dataset_asset import StorageLocation

        return (
            location
            if statement.column_descriptions[0]["entity"] is StorageLocation
            else binding
        )

    return (
        SimpleNamespace(
            get=AsyncMock(return_value=location), scalar=AsyncMock(side_effect=scalar)
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
    assert await owners.allocate(db, row) == reference(
        bucket=storage.bug_reports_bucket
    )
    assert row.storage_ref == reference()
    ensure.assert_awaited_once_with(db)
    statement = db.scalar.await_args.args[0]
    assert statement.compile().params == {"purpose_1": "bug-reports"}


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
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        await owners.allocate(db, record())
    assert caught.value.code == "bug_attachment_storage_binding_unproven"


@pytest.mark.parametrize("missing", ["location", "binding"])
async def test_allocate_rejects_missing_binding(storage, monkeypatch, missing):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    missing_call = db.get if missing == "location" else db.scalar
    missing_call.side_effect = None
    missing_call.return_value = None
    with pytest.raises(owners.BugAttachmentStorageError):
        await owners.allocate(db, record())


async def test_allocate_exposes_no_bootstrap_details(storage, monkeypatch):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(
        owners, "ensure_legacy_bindings", AsyncMock(side_effect=ValueError("secret"))
    )
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        await owners.allocate(db, record())
    assert "secret" not in str(caught.value)
    db.get.assert_not_awaited()


def readable(storage, data=RAW):
    body = io.BytesIO(data)
    metadata = {"ContentLength": len(RAW), "ETag": '"upload-version"'}
    storage.client.head_object = Mock(return_value=metadata)
    storage.client.get_object = Mock(return_value={**metadata, "Body": body})
    storage.client.put_object = Mock()
    storage.delete_object = Mock()

    return body


def s3_error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret"}}, "GetObject")


def test_verify_reads_full_saved_object_conditionally_and_closes(storage):
    body = readable(storage)
    assert owners.read(record()) == RAW
    assert body.closed
    storage.client.get_object.assert_called_once_with(
        Bucket="attachments-original", Key=KEY, IfMatch='"upload-version"'
    )
    assert storage.client.head_object.call_count == 2


@pytest.mark.parametrize("data", [b"x" * len(RAW), RAW[:-1], RAW + b"x"])
def test_corrupt_truncated_or_extra_bytes_are_closed_and_rejected(storage, data):
    body = readable(storage, data)
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record(), missing_ok=True)
    assert caught.value.code == "bug_attachment_storage_integrity_mismatch"
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
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record(), missing_ok=True)
    assert body.closed
    assert caught.value.code == "bug_attachment_storage_unavailable"
    assert caught.value.storage_operation_unsettled is True
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("missing_ok", [False, True])
def test_only_actual_missing_can_be_safe_absence(storage, missing_ok):
    readable(storage)
    storage.client.head_object.side_effect = s3_error("404")
    if missing_ok:
        assert owners.read(record(), missing_ok=True) is None
    else:
        with pytest.raises(owners.BugAttachmentStorageError) as caught:
            owners.read(record())
        assert caught.value.code == "bug_attachment_storage_missing"
    storage.client.get_object.assert_not_called()


@pytest.mark.parametrize("code", ["412", "AccessDenied"])
def test_conditional_conflict_and_access_failure_are_not_missing(storage, code):
    readable(storage)
    storage.client.get_object.side_effect = s3_error(code)
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record(), missing_ok=True)
    assert caught.value.code == (
        "bug_attachment_storage_integrity_mismatch"
        if code == "412"
        else "bug_attachment_storage_unavailable"
    )
    assert "secret" not in str(caught.value)


def test_post_read_object_version_change_is_rejected(storage):
    body = readable(storage)
    storage.client.head_object.side_effect = [
        {"ContentLength": len(RAW), "ETag": '"upload-version"'},
        {"ContentLength": len(RAW), "ETag": '"changed-version"'},
    ]
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record())
    assert caught.value.code == "bug_attachment_storage_integrity_mismatch"
    assert body.closed


def test_deadline_prevents_download_after_slow_head(storage, monkeypatch):
    readable(storage)
    times = iter([0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record())
    assert caught.value.code == "bug_attachment_storage_budget"
    storage.client.get_object.assert_not_called()


def test_deadline_during_stream_still_closes_body(storage, monkeypatch):
    body = readable(storage)
    times = iter([0, 0, 0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record())
    assert caught.value.code == "bug_attachment_storage_budget"
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
    assert owners.read(record()) == RAW
    assert body.closed


def test_write_uses_conditional_create_and_full_saved_read(storage):
    body = readable(storage)
    storage.client.put_object.return_value = {}
    owners.write(record(), RAW)
    storage.client.put_object.assert_called_once_with(
        Bucket="attachments-original",
        Key=KEY,
        Body=RAW,
        ContentType="image/png",
        IfNoneMatch="*",
    )
    assert body.closed
    assert storage.client.head_object.call_count == 2
    storage.delete_object.assert_not_called()


def test_local_content_change_is_rejected_before_upload(storage):
    readable(storage)
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.write(record(), b"changed")
    assert caught.value.code == "bug_attachment_storage_integrity_mismatch"
    storage.client.put_object.assert_not_called()
    storage.delete_object.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("secret"), s3_error("412")])
def test_upload_failure_never_deletes_or_falls_back(storage, failure):
    readable(storage)
    storage.client.put_object.side_effect = failure
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.write(record(), RAW)
    assert caught.value.code == (
        "bug_attachment_storage_integrity_mismatch"
        if isinstance(failure, ClientError)
        else "bug_attachment_storage_unavailable"
    )
    assert "secret" not in str(caught.value)
    storage.client.get_object.assert_not_called()
    storage.delete_object.assert_not_called()


def test_delete_resolves_receipt_and_exact_saved_owner(storage):
    readable(storage)
    owners.delete(record())
    storage.delete_object.assert_called_once_with(KEY, bucket="attachments-original")
    storage.delete_object.reset_mock()
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.delete(record(sha256="broken"))
    storage.delete_object.assert_not_called()


def test_forged_owner_is_not_an_output_receipt(storage):
    readable(storage)
    forged = owners.Owner("forged-bucket", KEY, reference())
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.delete(forged)
    storage.delete_object.assert_not_called()


def test_code_only_errors_have_safe_default_text():
    error = owners.BugAttachmentStorageError("bug_attachment_storage_missing")
    assert error.code == "bug_attachment_storage_missing"
    assert error.status_code == 409
    assert str(error)


@pytest.mark.parametrize(
    ("mime_type", "image_format"),
    [("image/png", "PNG"), ("image/jpeg", "JPEG"), ("image/webp", "WEBP")],
)
def test_real_images_verify_without_reencoding(mime_type, image_format):
    raw = image_bytes(image_format)
    original = bytes(raw)
    assert owners.validate_image(raw, mime_type) is None
    assert raw == original


@pytest.mark.parametrize(
    ("data", "mime_type"),
    [
        (b"", "image/png"),
        (b"not an image", "image/png"),
        (RAW, "image/jpeg"),
        (RAW, "image/jpg"),
        (RAW, None),
        (bytearray(RAW), "image/png"),
        (RAW[:-12], "image/png"),
    ],
)
def test_invalid_or_misdeclared_images_have_safe_errors(data, mime_type):
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.validate_image(data, mime_type)
    assert caught.value.code == "bug_attachment_storage_invalid_image"
    assert caught.value.status_code == 422


def test_image_budget_rejects_before_put(storage, monkeypatch):
    readable(storage)
    monkeypatch.setattr(owners, "MAX_PIXELS", 63)
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.write(record(), RAW)
    assert caught.value.code == "bug_attachment_storage_invalid_image"
    storage.client.put_object.assert_not_called()


def test_webp_frame_decode_eof_is_a_safe_invalid_image(monkeypatch):
    image = Mock()
    image.__enter__ = Mock(return_value=image)
    image.__exit__ = Mock(return_value=False)
    image.size = (8, 8)
    image.format = "WEBP"
    image.load.side_effect = EOFError("private decoder details")
    monkeypatch.setattr(owners.Image, "open", Mock(return_value=image))
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.validate_image(image_bytes("WEBP"), "image/webp")
    assert caught.value.code == "bug_attachment_storage_invalid_image"
    assert caught.value.status_code == 422
    assert "private" not in str(caught.value)


def test_image_size_budget_is_applied_before_decode(monkeypatch):
    monkeypatch.setattr(owners, "MAX_BYTES", len(RAW) - 1)
    opened = Mock()
    monkeypatch.setattr(owners.Image, "open", opened)
    with pytest.raises(owners.BugAttachmentStorageError):
        owners.validate_image(RAW, "image/png")
    opened.assert_not_called()


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
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.safe_file_name(name)
    assert caught.value.code == "bug_attachment_storage_invalid_file_name"
    assert caught.value.status_code == 422


def test_safe_unicode_name_and_limit_are_preserved():
    assert owners.safe_file_name("截图 原件.png") == "截图 原件.png"
    assert owners.safe_file_name("图" * 255) == "图" * 255


@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
def test_io_service_does_not_make_workflow_state_decisions(storage, state):
    readable(storage)
    assert owners.read(record(state=state)) == RAW


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
    with pytest.raises(owners.BugAttachmentStorageError) as caught:
        owners.read(record(), missing_ok=True)
    assert caught.value.code == "bug_attachment_storage_unavailable"
    assert caught.value.storage_operation_unsettled is True


def test_saved_bucket_read_never_touches_same_key_in_current_bucket(storage):
    readable(storage)
    assert owners.read(record()) == RAW
    assert all(
        call.kwargs == {"Bucket": "attachments-original", "Key": KEY}
        for call in storage.client.head_object.call_args_list
    )


@pytest.mark.parametrize(
    "version,accepted", [("null", True), ("non-null-provider-version", False)]
)
def test_explicit_null_version_can_omit_header_but_non_null_must_match(
    storage, version, accepted
):
    readable(storage)
    row = record(object_version_id=version)
    if accepted:
        assert owners.read(row) == RAW
        assert storage.client.get_object.call_args.kwargs["VersionId"] == version
    else:
        with pytest.raises(owners.BugAttachmentStorageError):
            owners.read(row)
        storage.client.get_object.assert_not_called()


@pytest.mark.parametrize("replaced", [False, True])
def test_null_slot_cleanup_verifies_saved_content_before_delete(storage, replaced):
    body = readable(storage, b"x" * len(RAW) if replaced else RAW)
    storage.client.delete_object = Mock()
    row = record(object_version_id="null")
    if replaced:
        with pytest.raises(owners.BugAttachmentStorageError) as caught:
            owners.delete(row)
        assert caught.value.code == "bug_attachment_storage_integrity_mismatch"
        storage.client.delete_object.assert_not_called()
    else:
        owners.delete(row)
        storage.client.delete_object.assert_called_once_with(
            Bucket="attachments-original", Key=KEY, VersionId="null"
        )
        assert storage.client.head_object.call_count == 2
    assert body.closed
    assert storage.client.get_object.call_args.kwargs["VersionId"] == "null"
    storage.delete_object.assert_not_called()


def test_immutable_version_cleanup_keeps_exact_saved_version(storage):
    readable(storage)
    storage.client.delete_object = Mock()
    owners.delete(record(object_version_id="saved-provider-version"))
    storage.client.delete_object.assert_called_once_with(
        Bucket="attachments-original", Key=KEY, VersionId="saved-provider-version"
    )
    storage.client.head_object.assert_not_called()
    storage.client.get_object.assert_not_called()
    storage.delete_object.assert_not_called()
