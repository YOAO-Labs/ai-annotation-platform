"""Strict export ownership and bounded I/O, with no database or S3 access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
from botocore.config import Config
import pytest

from app.config import settings
from app.services import export_storage as owners
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

PROJECT, JOB, OUTPUT = uuid.UUID(int=12), uuid.UUID(int=13), uuid.UUID(int=14)
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
RAW = b"reviewed complete ZIP bytes"
KEY = owners.key_for("image", PROJECT, JOB, OUTPUT)


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(*, bucket="exports-original", output_id=OUTPUT):
    return {
        "schema": 1,
        "purpose": "export",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "object_key": owners.key_for("image", PROJECT, JOB, output_id),
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def record(**changes):
    return SimpleNamespace(
        **{
            "id": OUTPUT,
            "project_id": PROJECT,
            "job_id": JOB,
            "media": "image",
            "format": "coco,yolo-det",
            "file_count": 2,
            "created_at": CREATED,
            "expires_at": CREATED + timedelta(days=7),
            "storage_ref": reference(),
            "sha256": hashlib.sha256(RAW).hexdigest(),
            "size_bytes": len(RAW),
            **changes,
        }
    )


@pytest.fixture
def storage(monkeypatch):
    service = SimpleNamespace(
        export_bucket="exports-current",
        client=SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint())),
    )
    monkeypatch.setattr(owners, "storage_service", service)
    return service


def test_saved_owner_survives_current_bucket_change_and_is_copied(storage):
    row = record()
    owner = owners.resolve(row)
    assert owner.bucket == "exports-original"
    assert owner.object_key == KEY
    row.storage_ref["bucket"] = "changed-after-resolution"
    exposed = owner.ref
    exposed["bucket"] = "changed-returned-copy"
    assert owner.ref == reference()
    with pytest.raises(AttributeError):
        owner.bucket = "forged"


@pytest.mark.parametrize("bad", [None, {}, [], True, "text"])
def test_missing_or_invalid_envelope_is_rejected(storage, bad):
    with pytest.raises(owners.ExportStorageError):
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
        ("object_key", "../export.zip"),
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
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.resolve_ref({**reference(), field: value})
    assert caught.value.code == "export_storage_identity_changed"
    assert "secret" not in str(caught.value)


def test_actual_client_endpoint_and_saved_identity_are_checked(storage):
    storage.client.meta.endpoint_url = "http://another-endpoint.invalid"
    with pytest.raises(owners.ExportStorageError):
        owners.resolve(record())
    storage.client.meta.endpoint_url = endpoint()
    ref = reference()
    ref["bucket"] = "forged-with-old-identity"
    with pytest.raises(owners.ExportStorageError):
        owners.resolve_ref(ref)


def test_saved_key_is_bound_to_all_candidate_identifiers(storage):
    for changes in (
        {"id": uuid.uuid4()},
        {"project_id": uuid.uuid4()},
        {"job_id": uuid.uuid4()},
        {"media": "video"},
    ):
        with pytest.raises(owners.ExportStorageError):
            owners.resolve(record(**changes))
    with pytest.raises(owners.ExportStorageError):
        owners.resolve_ref(reference(), expected_key="unrelated.zip")


@pytest.mark.parametrize(
    "bad", [None, True, 12, "", str(OUTPUT).upper(), OUTPUT.hex, "../x"]
)
def test_key_requires_canonical_uuid(bad):
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.key_for("image", PROJECT, JOB, bad)
    assert caught.value.code == "export_storage_invalid_key"


@pytest.mark.parametrize("media", [None, 1, "Image", "pointcloud", "../image"])
def test_key_requires_exact_supported_media(media):
    with pytest.raises(owners.ExportStorageError):
        owners.key_for(media, PROJECT, JOB, OUTPUT)


def test_each_retry_owns_an_independent_key():
    second = uuid.uuid4()
    assert owners.key_for("image", PROJECT, JOB, second) != KEY
    assert owners.key_for("video", str(PROJECT), str(JOB), str(OUTPUT)).endswith(
        f"/{OUTPUT}.zip"
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
        {"file_count": True},
        {"file_count": -1},
        {"file_count": 1.0},
        {"format": ""},
        {"format": 1},
        {"format": "coco\n"},
        {"created_at": CREATED.replace(tzinfo=None)},
        {"expires_at": CREATED},
        {"expires_at": CREATED + timedelta(days=7, seconds=1)},
    ],
)
def test_receipt_and_fixed_retention_are_strict(storage, changes):
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.resolve(record(**changes))
    assert caught.value.code == "export_storage_invalid_receipt"


def allocation_db(storage):
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    binding = SimpleNamespace(
        location_id=LEGACY_LOCATION_ID, generation=1, bucket=storage.export_bucket
    )

    async def scalar(statement):
        entity = statement.column_descriptions[0]["entity"]
        return location if entity is owners.StorageLocation else binding

    return (
        SimpleNamespace(
            get=AsyncMock(return_value=location), scalar=AsyncMock(side_effect=scalar)
        ),
        location,
        binding,
    )


async def test_allocate_proves_current_binding_without_mutating_candidate(
    storage, monkeypatch
):
    db, _, _ = allocation_db(storage)
    ensure = AsyncMock()
    monkeypatch.setattr(owners, "ensure_legacy_bindings", ensure)
    row = record()
    assert await owners.allocate(db, row) == reference(bucket=storage.export_bucket)
    assert row.storage_ref == reference()
    ensure.assert_awaited_once_with(db)
    statement = db.scalar.await_args.args[0]
    assert statement.compile().params == {"purpose_1": "export"}


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
        ("binding", "generation", 0),
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
    with pytest.raises(owners.ExportStorageError) as caught:
        await owners.allocate(db, record())
    assert caught.value.code == "export_storage_binding_unproven"


@pytest.mark.parametrize("missing", ["location", "binding"])
async def test_allocate_rejects_missing_binding(storage, monkeypatch, missing):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    if missing == "location":
        db.get.return_value = None
    else:
        db.scalar.side_effect = None
        db.scalar.return_value = None
    with pytest.raises(owners.ExportStorageError):
        await owners.allocate(db, record())


async def test_allocate_exposes_no_bootstrap_details(storage, monkeypatch):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(
        owners, "ensure_legacy_bindings", AsyncMock(side_effect=ValueError("secret"))
    )
    with pytest.raises(owners.ExportStorageError) as caught:
        await owners.allocate(db, record())
    assert "secret" not in str(caught.value)
    db.get.assert_not_awaited()


def readable(storage, data=RAW):
    body = io.BytesIO(data)
    metadata = {"ContentLength": len(RAW), "ETag": '"candidate-version"'}
    storage.client.head_object = Mock(return_value=metadata)
    storage.client.get_object = Mock(return_value={**metadata, "Body": body})
    storage.upload_file = Mock()
    storage.delete_object = Mock()
    storage.generate_download_url = Mock(return_value="signed-saved-output")
    return body


@pytest.fixture
def owned_writer(storage, monkeypatch):
    """Only ZIP write tests admit a separate client; reads retain the shared one."""
    from app.services import storage as module

    config = Config(retries={"max_attempts": 3})
    storage.client.meta.config = config
    client = SimpleNamespace(
        head_object=lambda **kw: storage.client.head_object(**kw),
        get_object=lambda **kw: storage.client.get_object(**kw),
        upload_file=lambda path, bucket, key, ExtraArgs: storage.upload_file(
            path, key, bucket=bucket, content_type=ExtraArgs["ContentType"]
        ),
        close=Mock(),
    )
    factory = Mock(return_value=SimpleNamespace(client=client))
    monkeypatch.setattr(module, "StorageService", factory)
    return factory, client, config


def s3_error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret"}}, "GetObject")


def test_verify_reads_full_saved_object_conditionally_and_closes(storage):
    body = readable(storage)
    assert owners.verify(record()) is True
    assert body.closed
    storage.client.get_object.assert_called_once_with(
        Bucket="exports-original", Key=KEY, IfMatch='"candidate-version"'
    )
    assert storage.client.head_object.call_count == 2


@pytest.mark.parametrize("data", [b"x" * len(RAW), RAW[:-1], RAW + b"x"])
def test_corrupt_truncated_or_extra_bytes_are_closed_and_rejected(storage, data):
    body = readable(storage, data)
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.verify(record(), missing_ok=True)
    assert caught.value.code == "export_storage_integrity_mismatch"
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
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.verify(record(), missing_ok=True)
    assert body.closed
    assert caught.value.code == "export_storage_unavailable"
    assert caught.value.storage_operation_unsettled is True
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("missing_ok", [False, True])
def test_only_actual_missing_can_be_safe_absence(storage, missing_ok):
    readable(storage)
    storage.client.head_object.side_effect = s3_error("404")
    if missing_ok:
        assert owners.verify(record(), missing_ok=True) is False
    else:
        with pytest.raises(owners.ExportStorageError) as caught:
            owners.verify(record())
        assert caught.value.code == "export_storage_missing"
    storage.client.get_object.assert_not_called()


@pytest.mark.parametrize("code", ["412", "AccessDenied"])
def test_conditional_conflict_and_access_failure_are_not_missing(storage, code):
    readable(storage)
    storage.client.get_object.side_effect = s3_error(code)
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.verify(record(), missing_ok=True)
    assert caught.value.code == (
        "export_storage_integrity_mismatch"
        if code == "412"
        else "export_storage_unavailable"
    )
    assert "secret" not in str(caught.value)


def test_post_read_object_version_change_is_rejected(storage):
    body = readable(storage)
    storage.client.head_object.side_effect = [
        {"ContentLength": len(RAW), "ETag": '"candidate-version"'},
        {"ContentLength": len(RAW), "ETag": '"changed-version"'},
    ]
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.verify(record())
    assert caught.value.code == "export_storage_integrity_mismatch"
    assert body.closed


def test_deadline_prevents_download_after_slow_head(storage, monkeypatch):
    readable(storage)
    times = iter([0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.verify(record())
    assert caught.value.code == "export_storage_budget"
    storage.client.get_object.assert_not_called()


def test_deadline_during_stream_still_closes_body(storage, monkeypatch):
    body = readable(storage)
    times = iter([0, 0, 0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.verify(record())
    assert caught.value.code == "export_storage_budget"
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
    assert owners.verify(record()) is True
    assert body.closed


def test_file_receipt_and_write_verify_same_saved_bytes(
    storage, tmp_path, owned_writer
):
    readable(storage)
    path = tmp_path / "candidate.zip"
    path.write_bytes(RAW)
    assert owners.file_receipt(path) == (hashlib.sha256(RAW).hexdigest(), len(RAW))
    owners.write(record(), path)
    storage.upload_file.assert_called_once_with(
        str(path), KEY, bucket="exports-original", content_type="application/zip"
    )
    storage.delete_object.assert_not_called()
    owned_writer[0].assert_called_once_with(client_config=owned_writer[2])
    owned_writer[1].close.assert_called_once()


def test_local_file_change_is_rejected_before_upload(storage, tmp_path):
    readable(storage)
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"changed")
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.write(record(), path)
    assert caught.value.code == "export_storage_integrity_mismatch"
    storage.upload_file.assert_not_called()
    storage.delete_object.assert_not_called()


def test_upload_failure_never_deletes_and_has_safe_error(
    storage, tmp_path, owned_writer
):
    readable(storage)
    path = tmp_path / "candidate.zip"
    path.write_bytes(RAW)
    storage.upload_file.side_effect = RuntimeError("secret")
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.write(record(), path)
    assert caught.value.code == "export_storage_unavailable"
    assert "secret" not in str(caught.value)
    storage.delete_object.assert_not_called()


@pytest.mark.parametrize("data", [b"", RAW])
def test_local_receipt_enforces_nonempty_size_budget(tmp_path, monkeypatch, data):
    path = tmp_path / "candidate.zip"
    path.write_bytes(data)
    monkeypatch.setattr(owners, "MAX_BYTES", len(RAW) - 1)
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.file_receipt(path)
    assert caught.value.code == "export_storage_integrity_mismatch"


def test_local_receipt_checks_deadline_after_final_read(tmp_path, monkeypatch):
    path = tmp_path / "candidate.zip"
    path.write_bytes(RAW)
    times = iter([0, 0, 0, 601])
    monkeypatch.setattr(owners.time, "monotonic", lambda: next(times))
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.file_receipt(path)
    assert caught.value.code == "export_storage_budget"


def test_sign_uses_only_remaining_fixed_expiry_and_saved_bucket(storage):
    readable(storage)
    row = record()
    assert (
        owners.sign(
            row, download_name="dataset.zip", now=CREATED + timedelta(days=6, seconds=3)
        )
        == "signed-saved-output"
    )
    storage.generate_download_url.assert_called_once_with(
        KEY,
        expires_in=86400 - 3,
        bucket="exports-original",
        download_name="dataset.zip",
        align=False,
    )
    assert row.expires_at == CREATED + timedelta(days=7)


@pytest.mark.parametrize("elapsed", [timedelta(days=7), timedelta(days=8)])
def test_expired_output_cannot_receive_new_signature(storage, elapsed):
    readable(storage)
    with pytest.raises(owners.ExportStorageError) as caught:
        owners.sign(record(), download_name="dataset.zip", now=CREATED + elapsed)
    assert caught.value.code == "export_storage_expired"
    storage.generate_download_url.assert_not_called()


def test_delete_resolves_receipt_and_exact_saved_owner(storage):
    readable(storage)
    owners.delete(record())
    storage.delete_object.assert_called_once_with(KEY, bucket="exports-original")
    storage.delete_object.reset_mock()
    with pytest.raises(owners.ExportStorageError):
        owners.delete(record(sha256="broken"))
    storage.delete_object.assert_not_called()


def test_forged_owner_is_not_an_output_receipt(storage):
    readable(storage)
    forged = owners.Owner("forged-bucket", KEY, reference())
    with pytest.raises(owners.ExportStorageError):
        owners.delete(forged)
    storage.delete_object.assert_not_called()


def test_code_only_errors_have_safe_default_text():
    error = owners.ExportStorageError("export_storage_missing")
    assert error.code == "export_storage_missing"
    assert error.status_code == 409
    assert str(error)
