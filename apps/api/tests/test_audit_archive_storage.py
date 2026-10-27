"""Archive codec and physical I/O tests, with no database or provider access."""

from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import ipaddress
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
import pytest

from app.config import settings
from app.services import audit_archive_storage as owners
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

ARCHIVE, JOB = uuid.UUID(int=11), uuid.UUID(int=12)
CREATED = datetime(2026, 3, 1, tzinfo=timezone.utc)


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(bucket="archive-original"):
    return {
        "schema": 1,
        "purpose": "audit-archive",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "object_key": owners.key_for(2026, 1, ARCHIVE),
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def audit_row(row_id=1, **changes):
    return {
        "id": row_id,
        "actor_id": JOB,
        "actor_email": "审计@example.invalid",
        "actor_role": "superadmin",
        "action": "task.update",
        "target_type": "task",
        "target_id": "12",
        "method": "POST",
        "path": "/tasks/12",
        "status_code": 200,
        "ip": ipaddress.ip_address("127.0.0.1"),
        "detail_json": {"count": 2, "nested": [None, True]},
        "request_id": "request",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        **changes,
    }


def record(**changes):
    return SimpleNamespace(
        **{
            "id": ARCHIVE,
            "job_id": JOB,
            "year": 2026,
            "month": 1,
            "source_schema": "public",
            "source_table": "audit_logs_y2026m01",
            "parent_oid": 100,
            "source_oid": 101,
            "month_start": date(2026, 1, 1),
            "month_end": date(2026, 2, 1),
            "format_revision": 1,
            "row_count": 0,
            "source_size_bytes": 0,
            "source_sha256": hashlib.sha256(b"").hexdigest(),
            "sha256": "a" * 64,
            "size_bytes": 1,
            "storage_ref": reference(),
            "created_at": CREATED,
            "state": "pending",
            "published_at": None,
            **changes,
        }
    )


def archive(rows=None, *, source=None, trailer=None, suffix=b""):
    source = (
        b"".join(owners.canonical_line(item) for item in (rows or []))
        if source is None
        else source
    )
    row = record(
        row_count=len(rows or []),
        source_size_bytes=len(source),
        source_sha256=hashlib.sha256(source).hexdigest(),
    )
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as zipped:
        zipped.write(source)
        zipped.write(owners.encode_meta(row) if trailer is None else trailer)
        zipped.write(suffix)
    raw = output.getvalue()
    row.sha256, row.size_bytes = hashlib.sha256(raw).hexdigest(), len(raw)
    return row, raw


class Client:
    def __init__(self, raw):
        self.raw = raw
        self.meta = SimpleNamespace(endpoint_url=endpoint())
        self.bodies = []
        self.calls = []
        self.delete_object = Mock()
        self.generate_presigned_url = Mock()
        self.list_objects_v2 = Mock()

    def head_object(self, **params):
        self.calls.append(("head", params))
        return {"ETag": '"stable"', "ContentLength": len(self.raw)}

    def get_object(self, **params):
        self.calls.append(("get", params))
        body = io.BytesIO(self.raw)
        self.bodies.append(body)
        return {"ETag": '"stable"', "ContentLength": len(self.raw), "Body": body}

    def put_object(self, **params):
        self.calls.append(("put", params))
        self.raw = params["Body"].read()


@pytest.fixture
def storage(monkeypatch):
    client = Client(b"")
    service = SimpleNamespace(audit_archive_bucket="archive-current", client=client)
    monkeypatch.setattr(owners, "storage_service", service)
    return service


def test_saved_owner_and_snapshot_do_not_follow_current_bucket(storage):
    row, _ = archive()
    owner, original = owners.resolve(row), owners.snapshot(row)
    assert owner.bucket == "archive-original"
    assert original.storage_ref == reference()
    exposed = original.storage_ref
    exposed["bucket"] = "forged"
    assert owners.same(row, original)
    row.storage_ref["bucket"] = "changed"
    assert not owners.same(row, original)
    assert original.storage_ref == reference() == owner.ref
    with pytest.raises(AttributeError):
        original.row_count = 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("config_revision", 1.0),
        ("binding_generation", 2),
        ("purpose", "exports"),
        ("location_id", str(JOB)),
        ("bucket", "bad/path"),
        ("endpoint_url", "http://user:secret@host"),
        ("endpoint_url", "http://host/path"),
        ("storage_identity", "A" * 64),
        ("object_key", "2026/01/../archive.gz"),
        ("extra", "secret"),
    ],
)
def test_strict_ref_rejects_ambiguous_owner(storage, field, value):
    with pytest.raises(owners.AuditArchiveStorageError):
        owners.resolve_ref({**reference(), field: value})


@pytest.mark.parametrize("ref", [None, {}, [], True, "legacy-key"])
def test_legacy_unowned_ref_is_not_adopted(storage, ref):
    with pytest.raises(owners.AuditArchiveStorageError):
        owners.resolve_ref(ref)
    assert storage.client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"year": 1999},
        {"month": True},
        {"month": 13},
        {"year": 9999, "month": 12},
        {"parent_oid": 0},
        {"source_oid": 2**32},
        {"month_start": date(2026, 1, 2)},
        {"source_schema": "other"},
        {"source_table": "other"},
        {"format_revision": 2},
        {"row_count": owners.MAX_ROWS + 1},
        {"source_size_bytes": owners.MAX_SOURCE_BYTES + 1},
        {"size_bytes": owners.MAX_BYTES + 1},
        {"sha256": "A" * 64},
        {"created_at": CREATED.replace(tzinfo=None)},
        {"state": "ready"},
        {"published_at": CREATED},
        {"state": "ready", "published_at": CREATED - timedelta(seconds=1)},
    ],
)
def test_receipt_and_source_identity_are_bounded(storage, changes):
    with pytest.raises(owners.AuditArchiveStorageError):
        owners.resolve(record(**changes))


def test_canonical_line_normalizes_real_database_types():
    row = audit_row(
        created_at=datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    )
    encoded = owners.canonical_line(row)
    parsed = json.loads(encoded)
    assert parsed["created_at"] == "2026-01-01T00:00:00.000000+00:00"
    assert parsed["actor_id"] == str(JOB)
    assert parsed["ip"] == "127.0.0.1"
    assert "审计".encode() in encoded
    assert owners.canonical_line(parsed) == encoded


@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"id": 2**63},
        {"status_code": 32768},
        {"created_at": datetime(2026, 1, 1)},
        {"detail_json": {"secret": object()}},
        {"detail_json": float("nan")},
        {"extra": "field"},
        {"ip": "invalid"},
    ],
)
def test_canonical_line_rejects_lossy_or_unknown_values(changes):
    with pytest.raises(owners.AuditArchiveStorageError):
        owners.canonical_line(audit_row(**changes))


def test_metadata_is_available_before_compressed_receipt():
    row = record()
    for field in (
        "sha256",
        "size_bytes",
        "storage_ref",
        "created_at",
        "state",
        "published_at",
    ):
        delattr(row, field)
    meta = owners.metadata(row)["_partition_meta"]
    assert meta["archive_id"] == str(ARCHIVE)
    assert "sha256" not in meta and "storage_ref" not in meta
    assert json.loads(owners.encode_meta(row)) == owners.metadata(row)


def test_authenticated_full_read_paginates_source_and_literal_meta(storage):
    row, storage.client.raw = archive([audit_row(1), audit_row(2), audit_row(3)])
    result = owners.read(row, offset=2, limit=2)
    assert result["total"] == 4
    assert result["items"][0]["id"] == 3
    assert result["items"][1] == owners.metadata(row)
    assert all(body.closed for body in storage.client.bodies)
    assert storage.client.calls[1][1]["IfMatch"] == '"stable"'
    assert all(call[1]["Bucket"] == "archive-original" for call in storage.client.calls)
    storage.client.list_objects_v2.assert_not_called()
    storage.client.generate_presigned_url.assert_not_called()


def test_empty_archive_has_one_metadata_item(storage):
    row, storage.client.raw = archive()
    assert owners.read(row) == {"items": [owners.metadata(row)], "total": 1}
    assert owners.read(row, offset=1) == {"items": [], "total": 1}


@pytest.mark.parametrize(
    "damage",
    [
        "truncated",
        "extra-row",
        "missing-meta",
        "wrong-meta",
        "unsorted",
        "wrong-month",
        "duplicate-key",
        "noncanonical",
        "source-digest",
        "source-size",
        "gzip-tail",
        "bad-crc",
        "bad-isize",
        "blank-source",
        "blank-after-meta",
        "missing-final-newline",
    ],
)
def test_full_semantic_verification_rejects_corruption_outside_page(storage, damage):
    rows = [audit_row(1), audit_row(2)]
    if damage == "unsorted":
        rows.reverse()
    if damage == "wrong-month":
        rows[1]["created_at"] = datetime(2026, 2, 1, tzinfo=timezone.utc)
    row, raw = archive(rows)
    if damage in {
        "extra-row",
        "missing-meta",
        "wrong-meta",
        "duplicate-key",
        "noncanonical",
        "blank-source",
        "blank-after-meta",
        "missing-final-newline",
    }:
        source = b"".join(owners.canonical_line(item) for item in rows)
        trailer, suffix = None, b""
        if damage == "extra-row":
            suffix = owners.canonical_line(audit_row(3))
        elif damage == "missing-meta":
            trailer = b""
        elif damage == "wrong-meta":
            trailer = owners.encode_meta(record(job_id=uuid.uuid4()))
        elif damage == "duplicate-key":
            source = source.replace(b'"id":2,', b'"id":2,"id":2,')
        elif damage == "noncanonical":
            source = source.replace(b'"id":2,', b'"id": 2,')
        elif damage == "blank-source":
            source = b"\n" + source
        elif damage == "blank-after-meta":
            suffix = b"\n"
        elif damage == "missing-final-newline":
            trailer = owners.encode_meta(row)[:-1]
        row, raw = archive(rows, source=source, trailer=trailer, suffix=suffix)
    if damage == "truncated":
        raw = raw[:-5]
    elif damage == "gzip-tail":
        raw += b"invalid tail"
    elif damage == "bad-crc":
        raw = raw[:-8] + bytes([raw[-8] ^ 1]) + raw[-7:]
    elif damage == "bad-isize":
        raw = raw[:-4] + bytes([raw[-4] ^ 1]) + raw[-3:]
    elif damage == "source-digest":
        row.source_sha256 = "0" * 64
    elif damage == "source-size":
        row.source_size_bytes += 1
    # Even an independent compressed receipt cannot excuse malformed contents.
    row.sha256, row.size_bytes = hashlib.sha256(raw).hexdigest(), len(raw)
    storage.client.raw = raw
    with pytest.raises(owners.AuditArchiveStorageError) as rejected:
        owners.read(row, limit=1)
    assert rejected.value.status_code == 409
    assert all(body.closed for body in storage.client.bodies)


@pytest.mark.parametrize(
    "extra", ["zeros", "empty-tail", "empty-head", "split-line", "split-row"]
)
def test_one_gzip_member_has_no_extra_physical_bytes(storage, extra):
    row, raw = archive([audit_row()])
    plaintext = gzip.decompress(raw)
    if extra == "zeros":
        raw += b"\x00" * 100
    elif extra == "empty-tail":
        raw += gzip.compress(b"", mtime=0)
    elif extra == "empty-head":
        raw = gzip.compress(b"", mtime=0) + raw
    else:
        boundary = 10 if extra == "split-line" else plaintext.index(b"\n") + 1
        raw = gzip.compress(plaintext[:boundary], mtime=0) + gzip.compress(
            plaintext[boundary:], mtime=0
        )
    row.sha256, row.size_bytes = hashlib.sha256(raw).hexdigest(), len(raw)
    storage.client.raw = raw
    with pytest.raises(owners.AuditArchiveStorageError) as rejected:
        owners.read(row)
    assert rejected.value.code == "audit_archive_storage_integrity_mismatch"
    assert all(body.closed for body in storage.client.bodies)
    with io.BytesIO(raw) as source:
        with pytest.raises(owners.AuditArchiveStorageError):
            owners.write(row, source)
        assert source.tell() == 0 and not source.closed
    assert not any(name == "put" for name, _ in storage.client.calls)


def test_valid_source_row_spans_multiple_bounded_decompression_outputs(storage):
    row, storage.client.raw = archive(
        [audit_row(detail_json={"text": "x" * (3 * owners.CHUNK_BYTES)})]
    )
    assert owners.read(row, limit=1, offset=1) == {
        "items": [owners.metadata(row)],
        "total": 2,
    }
    assert all(body.closed for body in storage.client.bodies)


def test_long_gzip_header_obeys_compressed_chunk_deadline(monkeypatch):
    row, raw = archive()
    header = bytearray(raw[:10])
    header[3] |= 8
    raw = bytes(header) + b"x" * (3 * owners.CHUNK_BYTES) + b"\x00" + raw[10:]
    row.sha256, row.size_bytes = hashlib.sha256(raw).hexdigest(), len(raw)

    class ProgressFile(io.BytesIO):
        consumed = 0
        requests = []

        def read(self, size=-1):
            self.requests.append(size)
            chunk = super().read(size)
            self.consumed += len(chunk)
            return chunk

    with ProgressFile(raw) as source:
        # Reading two compressed chunks exhausts the cooperative time window,
        # before the optional filename terminator or any output can be reached.
        monkeypatch.setattr(
            owners.time,
            "monotonic",
            lambda: 601 if source.consumed > owners.CHUNK_BYTES else 0,
        )
        with pytest.raises(owners.AuditArchiveStorageError) as rejected:
            owners._semantic(source, row, 100, 0, 600)
        assert rejected.value.code == "audit_archive_storage_budget"
        assert source.consumed <= 2 * owners.CHUNK_BYTES
        assert all(0 < request <= owners.CHUNK_BYTES for request in source.requests)
        assert source.tell() == 0 and not source.closed


def test_decompression_expansion_is_bounded_before_missing_newline(
    storage, monkeypatch
):
    row, _ = archive()
    raw = gzip.compress(b"x" * (32 * owners.CHUNK_BYTES), mtime=0)
    row.sha256, row.size_bytes = hashlib.sha256(raw).hexdigest(), len(raw)
    storage.client.raw = raw
    original = owners.zlib.decompressobj
    calls = []

    class BoundedDecoder:
        def __init__(self, *args):
            self.decoder = original(*args)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

        def decompress(self, data, maximum):
            assert 0 < maximum <= owners.CHUNK_BYTES
            output = self.decoder.decompress(data, maximum)
            calls.append((len(data), len(output)))
            return output

    monkeypatch.setattr(owners.zlib, "decompressobj", BoundedDecoder)
    with pytest.raises(owners.AuditArchiveStorageError) as rejected:
        owners.read(row)
    assert rejected.value.code == "audit_archive_storage_integrity_mismatch"
    assert calls and all(size <= owners.CHUNK_BYTES for _, size in calls)
    assert all(body.closed for body in storage.client.bodies)


@pytest.mark.parametrize(
    "damage", ["sha", "size", "etag", "get-size", "get-etag", "changed-after"]
)
def test_compressed_receipt_and_conditional_transfer_are_enforced(storage, damage):
    row, raw = archive([audit_row()])
    storage.client.raw = raw
    if damage == "sha":
        row.sha256 = "0" * 64
    elif damage == "size":
        row.size_bytes += 1
    elif damage == "etag":
        storage.client.head_object = Mock(return_value={"ContentLength": len(raw)})
    elif damage == "changed-after":
        storage.client.head_object = Mock(
            side_effect=[
                {"ETag": '"stable"', "ContentLength": len(raw)},
                {"ETag": '"new"', "ContentLength": len(raw)},
            ]
        )
    else:
        original = storage.client.get_object

        def changed(**params):
            result = original(**params)
            result["ContentLength" if damage == "get-size" else "ETag"] = (
                2 if damage == "get-size" else '"new"'
            )
            return result

        storage.client.get_object = changed
    with pytest.raises(owners.AuditArchiveStorageError):
        owners.read(row)
    assert all(body.closed for body in storage.client.bodies)


@pytest.mark.parametrize(
    ("code", "status"),
    [("NoSuchKey", 404), ("PreconditionFailed", 409), ("AccessDenied", 503)],
)
def test_provider_errors_are_fixed_and_sanitized(storage, code, status):
    storage.client.head_object = Mock(
        side_effect=ClientError(
            {"Error": {"Code": code, "Message": "secret provider credential"}},
            "HeadObject",
        )
    )
    row, _ = archive()
    with pytest.raises(owners.AuditArchiveStorageError) as rejected:
        owners.read(row)
    assert rejected.value.status_code == status
    assert "secret" not in str(rejected.value)


def test_unknown_body_close_is_unavailable_and_preserves_admission(storage):
    row, raw = archive()
    body = io.BytesIO(raw)
    body.close = Mock(side_effect=RuntimeError("private handle"))
    storage.client.raw = raw
    storage.client.get_object = Mock(
        return_value={"ETag": '"stable"', "ContentLength": len(raw), "Body": body}
    )
    try:
        with pytest.raises(owners.AuditArchiveStorageError) as rejected:
            owners.read(row)
        assert rejected.value.status_code == 503
        assert rejected.value.storage_operation_unsettled
        assert "private" not in str(rejected.value)
    finally:
        io.BytesIO.close(body)


def test_create_only_write_preserves_bytes_and_caller_file(storage):
    row, raw = archive([audit_row()])
    source = io.BytesIO(raw)
    owners.write(row, source)
    assert source.tell() == 0 and not source.closed
    assert storage.client.raw == raw
    put = next(params for name, params in storage.client.calls if name == "put")
    assert put["IfNoneMatch"] == "*" and put["Body"] is source
    assert put["Bucket"] == "archive-original"
    assert all(body.closed for body in storage.client.bodies)
    storage.client.delete_object.assert_not_called()
    source.close()


def test_write_rejects_bad_local_semantics_before_upload(storage):
    row, raw = archive([audit_row()], trailer=b"{}\n")
    with io.BytesIO(raw) as source:
        with pytest.raises(owners.AuditArchiveStorageError):
            owners.write(row, source)
        assert source.tell() == 0
    assert storage.client.calls == []


def test_write_collision_never_deletes_existing_object(storage):
    row, raw = archive()
    storage.client.put_object = Mock(
        side_effect=ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    )
    with io.BytesIO(raw) as source:
        with pytest.raises(owners.AuditArchiveStorageError) as rejected:
            owners.write(row, source)
        assert rejected.value.status_code == 409
    storage.client.delete_object.assert_not_called()


def test_row_and_time_budgets_are_enforced(storage, monkeypatch):
    row, raw = archive([audit_row()])
    storage.client.raw = raw
    monkeypatch.setattr(owners, "MAX_ROW_BYTES", 10)
    with pytest.raises(owners.AuditArchiveStorageError):
        owners.read(row)
    monkeypatch.setattr(owners, "VERIFY_SECONDS", 0)
    with pytest.raises(owners.AuditArchiveStorageError) as rejected:
        owners.read(row)
    assert rejected.value.code == "audit_archive_storage_budget"


async def test_allocate_alone_checks_current_canonical_binding(storage, monkeypatch):
    row, _ = archive()
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    binding = SimpleNamespace(
        purpose="audit-archive",
        location_id=LEGACY_LOCATION_ID,
        generation=1,
        bucket="archive-current",
    )

    async def scalar(query):
        return location if "storage_locations" in str(query) else binding

    db = SimpleNamespace(
        get=AsyncMock(return_value=location), scalar=AsyncMock(side_effect=scalar)
    )
    ensure = AsyncMock()
    monkeypatch.setattr(owners, "ensure_legacy_bindings", ensure)
    ref = await owners.allocate(db, row)
    assert ref == reference("archive-current")
    ensure.assert_awaited_once_with(db)
    binding.generation = 0
    with pytest.raises(owners.AuditArchiveStorageError) as rejected:
        await owners.allocate(db, row)
    assert rejected.value.code == "audit_archive_storage_binding_unproven"
    assert owners.resolve(row).bucket == "archive-original"
