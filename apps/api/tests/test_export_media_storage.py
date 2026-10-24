"""Pure transport tests: fake S3 clients and owned temporary file descriptors."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from botocore.exceptions import ClientError
import pytest

from app.services import export_media_storage as physical

BUNDLE = uuid.UUID(int=101)
RELATIVE = str(uuid.UUID(int=102)) + ".bin"
RAW = b"exact original PCD and image bytes\x00\xff"


def ok(**values):
    return {"ResponseMetadata": {"HTTPStatusCode": 200}, **values}


def receipt(data=RAW):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def record(*, count=1, **changes):
    created = datetime.now(timezone.utc)
    ref = physical.make_ref(BUNDLE, bucket="saved-export-bucket")
    objects = {
        physical.prefix_for(BUNDLE) + str(uuid.UUID(int=102 + i)) + ".bin": receipt()
        for i in range(count)
    }
    return SimpleNamespace(
        **{
            "id": BUNDLE,
            "storage_ref": ref,
            "inventory": {"schema": 1, "objects": objects},
            "created_at": created,
            "expires_at": created + timedelta(days=7),
            **changes,
        }
    )


class Body(io.BytesIO):
    def __init__(self, value, failure=None):
        super().__init__(value)
        self.close_count, self.failure = 0, failure

    def close(self):
        self.close_count += 1
        if self.failure:
            raise self.failure
        super().close()


class Client:
    def __init__(self, *, page_size=1000, versioned=True):
        self.meta = SimpleNamespace(
            endpoint_url=physical._runtime_endpoint(), events=Mock()
        )
        self.rows, self.bodies, self.calls = {}, [], []
        self.close_count, self.page_size, self.versioned = 0, page_size, versioned
        self.mutate_get, self.mutate_head, self.close_failure = None, None, None
        self.body_close_failure = None

    def close(self):
        self.close_count += 1
        if self.close_failure:
            raise self.close_failure

    def add(self, key, data=RAW, *, marker=False):
        rows = self.rows.setdefault(key, [])
        version = str(len(rows) + 1) if self.versioned else "null"
        row = {
            "Key": key,
            "data": data,
            "VersionId": version,
            "ETag": '"' + hashlib.md5(data).hexdigest() + '"',
            "marker": marker,
        }
        if not self.versioned:
            rows.clear()
        rows.append(row)
        return row

    def current(self, key):
        rows = self.rows.get(key) or []
        if not rows or rows[-1]["marker"]:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GET")
        return rows[-1]

    def head_object(self, **params):
        self.calls.append(("head", params))
        row = self.current(params["Key"])
        value = ok(
            ETag=row["ETag"], VersionId=row["VersionId"], ContentLength=len(row["data"])
        )
        return self.mutate_head(value) if self.mutate_head else value

    def get_object(self, **params):
        self.calls.append(("get", params))
        row = self.current(params["Key"])
        assert params["IfMatch"] == row["ETag"]
        if self.versioned:
            assert params["VersionId"] == row["VersionId"]
        body = Body(row["data"], self.body_close_failure)
        self.bodies.append(body)
        value = ok(
            ETag=row["ETag"],
            VersionId=row["VersionId"],
            ContentLength=len(row["data"]),
            Body=body,
        )
        return self.mutate_get(value) if self.mutate_get else value

    def put_object(self, **params):
        self.calls.append(("put", params))
        assert params["IfNoneMatch"] == "*"
        if self.rows.get(params["Key"]) and not self.rows[params["Key"]][-1]["marker"]:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PUT")
        data = params["Body"].read()
        assert len(data) == params["ContentLength"]
        row = self.add(params["Key"], data)
        return ok(ETag=row["ETag"], VersionId=row["VersionId"])

    def list_objects_v2(self, **params):
        self.calls.append(("list", params))
        rows = [
            {"Key": key, "Size": len(values[-1]["data"])}
            for key, values in sorted(self.rows.items())
            if key.startswith(params["Prefix"]) and values and not values[-1]["marker"]
        ]
        start = int(params.get("ContinuationToken", 0))
        page = rows[start : start + self.page_size]
        more = start + self.page_size < len(rows)
        return ok(
            Contents=page,
            IsTruncated=more,
            **({"NextContinuationToken": str(start + self.page_size)} if more else {}),
        )

    def list_object_versions(self, **params):
        self.calls.append(("versions", params))
        rows = [
            row
            for key, values in sorted(self.rows.items())
            if key.startswith(params["Prefix"])
            for row in values
        ]
        marker = (params.get("KeyMarker"), params.get("VersionIdMarker"))
        start = next(
            (
                i + 1
                for i, row in enumerate(rows)
                if (row["Key"], row["VersionId"]) == marker
            ),
            0,
        )
        page = rows[start : start + self.page_size]
        more = start + self.page_size < len(rows)
        return ok(
            Versions=[row for row in page if not row["marker"]],
            DeleteMarkers=[row for row in page if row["marker"]],
            IsTruncated=more,
            **(
                {
                    "NextKeyMarker": page[-1]["Key"],
                    "NextVersionIdMarker": page[-1]["VersionId"],
                }
                if more
                else {}
            ),
        )

    def delete_object(self, **params):
        self.calls.append(("delete", params))
        rows = self.rows.get(params["Key"], [])
        self.rows[params["Key"]] = [
            row for row in rows if row["VersionId"] != params["VersionId"]
        ]
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def generate_presigned_url(self, operation, **kwargs):
        self.calls.append(("sign", {"operation": operation, **kwargs}))
        return "signed-saved-object"


def populate(client, owner):
    for key in owner.inventory["objects"]:
        client.add(key)


def test_owner_uses_saved_bucket_and_is_immutable_without_constructing_client(
    monkeypatch,
):
    factory = Mock(side_effect=AssertionError("constructor outside durable permit"))
    monkeypatch.setattr(physical, "create_client", factory)
    row = record()
    monkeypatch.setattr(physical.settings, "minio_export_bucket", "current-bucket-trap")
    owner = physical.resolve(row)
    assert owner.bucket == "saved-export-bucket"
    row.storage_ref["bucket"] = "mutated"
    owner.ref["bucket"] = "mutated-again"
    assert owner.bucket == owner.ref["bucket"] == "saved-export-bucket"
    with pytest.raises(AttributeError):
        owner.bucket = "forged"
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", 1.0),
        ("purpose", "datasets"),
        ("location_id", str(uuid.UUID(int=1))),
        ("config_revision", True),
        ("binding_generation", 2),
        ("bucket", "bad/path"),
        ("bucket", "bad bucket"),
        ("endpoint_url", "http://user:secret@host"),
        ("endpoint_url", "http://host/path"),
        ("endpoint_url", "http://host:0"),
        ("endpoint_url", "http://different-host"),
        ("key_prefix", physical.prefix_for(uuid.UUID(int=333))),
        ("storage_identity", "a" * 64),
        ("credential", "must not be accepted"),
    ],
)
def test_invalid_saved_owner_is_refused_before_io(field, value):
    row = record()
    row.storage_ref[field] = value
    with pytest.raises(physical.ExportMediaStorageError):
        physical.resolve(row)


@pytest.mark.parametrize(
    "relative",
    [
        "../a.bin",
        "/a.bin",
        RELATIVE.upper(),
        "nested/" + RELATIVE,
        RELATIVE + "?x=1",
        RELATIVE + "%2f",
        "not-a-uuid.bin",
        "",
        str(uuid.UUID(int=500)) + ".bin",
    ],
)
def test_unknown_or_unsafe_relative_never_signs(relative):
    client, row = Client(), record()
    with pytest.raises(physical.ExportMediaStorageError):
        physical.signed_url(client, row, relative)
    assert not client.calls


def test_inventory_checks_limits_exact_shapes_and_independent_copy(monkeypatch):
    row = record(count=2)
    values = physical.validate_inventory(row)
    values.clear()
    assert len(row.inventory["objects"]) == 2
    monkeypatch.setattr(physical, "MAX_OBJECTS", 1)
    with pytest.raises(physical.ExportMediaStorageError):
        physical.validate_inventory(row)
    monkeypatch.setattr(physical, "MAX_OBJECTS", 2)
    monkeypatch.setattr(physical, "MAX_TOTAL_BYTES", len(RAW))
    with pytest.raises(physical.ExportMediaStorageError, match="核验") as failure:
        physical.validate_inventory(row)
    assert failure.value.code.endswith("budget")
    row.inventory["objects"].popitem()
    entry = next(iter(row.inventory["objects"].values()))
    for changes in (
        {"size_bytes": True},
        {"size_bytes": physical.MAX_OBJECT_BYTES + 1},
        {"sha256": "A" * 64},
        {"extra": 1},
    ):
        original = dict(entry)
        entry.update(changes)
        with pytest.raises(physical.ExportMediaStorageError):
            physical.validate_inventory(row)
        entry.clear()
        entry.update(original)


@pytest.mark.parametrize("versioned", [True, False])
def test_exact_fd_put_is_exclusive_preserves_cursor_and_verifies_full_saved_object(
    monkeypatch, versioned
):
    client, row = Client(versioned=versioned), record()
    monkeypatch.setattr(
        physical,
        "validate_inventory",
        Mock(side_effect=AssertionError("per-PUT full inventory scan")),
    )
    with tempfile.TemporaryFile() as source:
        source.write(RAW)
        source.seek(3)
        assert (
            physical.write_object(client, row, RELATIVE, source.fileno()) == receipt()
        )
        assert source.tell() == 3
        assert source.read() == RAW[3:]
        put = [params for method, params in client.calls if method == "put"][0]
        assert put["Body"].closed
        assert put["Bucket"] == "saved-export-bucket"
        with pytest.raises(physical.ExportMediaStorageError) as failure:
            physical.write_object(client, row, RELATIVE, source.fileno())
        assert failure.value.code.endswith("integrity_mismatch")
        assert not source.closed
    assert all(body.closed and body.close_count == 1 for body in client.bodies)
    assert not any(method in {"list", "versions"} for method, _ in client.calls)


def test_wrong_source_content_never_puts_or_closes_borrowed_fd():
    row, client = record(), Client()
    with tempfile.TemporaryFile() as source:
        source.write(b"x" * len(RAW))
        source.seek(2)
        with pytest.raises(physical.ExportMediaStorageError):
            physical.write_object(client, row, RELATIVE, source.fileno())
        assert source.tell() == 2 and not source.closed
    assert not client.calls


@pytest.mark.parametrize(
    "fault",
    [
        "short",
        "extra",
        "sha",
        "etag",
        "version",
        "length",
        "partial",
        "range",
        "after-version",
    ],
)
def test_readback_detects_tampering_and_closes_body(fault):
    row, client = record(), Client()
    populate(client, row)

    def mutate(value):
        if fault in {"short", "extra", "sha"}:
            body = value["Body"]
            body.seek(0)
            body.truncate(0)
            body.write(
                RAW[:-1]
                if fault == "short"
                else RAW + b"x"
                if fault == "extra"
                else b"x" * len(RAW)
            )
            body.seek(0)
        elif fault == "etag":
            value["ETag"] = '"changed"'
        elif fault == "version":
            value["VersionId"] = "other"
        elif fault == "length":
            value["ContentLength"] = True
        elif fault == "partial":
            value["ResponseMetadata"]["HTTPStatusCode"] = 206
        elif fault == "range":
            value["ContentRange"] = "bytes 0-2/10"
        return value

    client.mutate_get = mutate
    if fault == "after-version":
        heads = 0

        def mutate_head(value):
            nonlocal heads
            heads += 1
            if heads == 2:
                value["VersionId"] = "later"
            return value

        client.mutate_head = mutate_head
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        physical.verify_object(client, row, RELATIVE)
    assert failure.value.code.endswith("integrity_mismatch")
    assert all(body.closed and body.close_count == 1 for body in client.bodies)


def test_write_requires_readback_version_to_equal_the_put_receipt():
    client, row = Client(), record()
    original_put = client.put_object

    def put(**params):
        response = original_put(**params)
        client.add(params["Key"])
        return response

    client.put_object = put
    with tempfile.TemporaryFile() as source:
        source.write(RAW)
        source.flush()
        with pytest.raises(physical.ExportMediaStorageError) as failure:
            physical.write_object(client, row, RELATIVE, source.fileno())
    assert failure.value.code.endswith("integrity_mismatch")
    assert not any(method == "get" for method, _ in client.calls)


def test_inventory_verifies_each_object_once_and_lists_linearly():
    client, row = Client(page_size=2), record(count=5)
    populate(client, row)
    assert physical.verify_inventory(client, row) == row.inventory
    assert sum(method == "get" for method, _ in client.calls) == 5
    assert sum(method == "head" for method, _ in client.calls) == 10
    assert sum(method == "list" for method, _ in client.calls) == 6
    assert all(body.closed for body in client.bodies)


@pytest.mark.parametrize("fault", ["missing", "extra", "unsafe", "list-size"])
def test_inventory_set_must_be_exact(fault):
    client, row = Client(), record(count=2)
    populate(client, row)
    key = next(iter(row.inventory["objects"]))
    if fault == "missing":
        client.rows.pop(key)
    elif fault == "extra":
        client.add(physical.prefix_for(BUNDLE) + str(uuid.UUID(int=900)) + ".bin")
    elif fault == "unsafe":
        client.add(physical.prefix_for(BUNDLE) + "../other.bin")
    else:
        client.rows[key][-1]["data"] = RAW + b"x"
    with pytest.raises(physical.ExportMediaStorageError):
        physical.verify_inventory(client, row)
    assert not any(method == "delete" for method, _ in client.calls)


def test_context_defers_constructor_closes_on_body_failure_and_rejects_other_endpoint():
    client, row = Client(), record()
    factory = Mock(return_value=client)
    context = physical.open_storage(row, factory)
    factory.assert_not_called()
    with pytest.raises(RuntimeError):
        with context as opened:
            assert opened is client
            raise RuntimeError("caller failure")
    assert client.close_count == 1
    client.meta.endpoint_url = "http://different-host"
    with pytest.raises(physical.ExportMediaStorageError):
        with physical.open_storage(row, factory):
            pass
    assert client.close_count == 2


class Cancelled(BaseException):
    pass


@pytest.mark.parametrize(
    "exception", [RuntimeError("constructor failure"), Cancelled()]
)
def test_uncertain_constructor_is_marked_even_for_base_exception(exception):
    with pytest.raises((physical.ExportMediaStorageError, Cancelled)) as failure:
        with physical.open_storage(record(), Mock(side_effect=exception)):
            pass
    assert failure.value.storage_operation_unsettled


@pytest.mark.parametrize("where", ["body", "client", "missing-body"])
def test_unknown_close_or_unowned_response_stays_unsettled(where):
    row, client = record(), Client()
    populate(client, row)
    if where == "body":
        client.body_close_failure = OSError("body close uncertain")
    elif where == "client":
        client.close_failure = OSError("client close uncertain")
    else:
        client.mutate_get = lambda value: {
            key: val for key, val in value.items() if key != "Body"
        }
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        with physical.open_storage(row, lambda: client):
            physical.verify_object(client, row, RELATIVE)
    assert failure.value.storage_operation_unsettled
    assert client.close_count == 1


def test_default_constructor_uses_same_origin_guard_and_owns_actual_client(monkeypatch):
    import boto3

    client, row = Client(), record()
    create = Mock(return_value=client)
    monkeypatch.setattr(boto3, "client", create)
    with physical.open_storage(row):
        parameters = create.call_args.kwargs
        assert parameters["endpoint_url"] == row.storage_ref["endpoint_url"]
        assert parameters["config"].s3 == {"addressing_style": "path"}
        event, guard = client.meta.events.register.call_args.args
        assert event == "before-send.s3"
        guard(SimpleNamespace(url=row.storage_ref["endpoint_url"] + "/bucket/key"))
        with pytest.raises(physical.ExportMediaStorageError):
            guard(SimpleNamespace(url="https://another-host/key"))
    assert client.close_count == 1


def test_signing_uses_only_remaining_fixed_deadline_and_saved_bucket(monkeypatch):
    row, client = record(), Client()
    monkeypatch.setattr(physical.settings, "minio_export_bucket", "current-bucket-trap")
    assert (
        physical.signed_url(
            client, row, RELATIVE, now=row.expires_at - timedelta(seconds=1.9)
        )
        == "signed-saved-object"
    )
    _, params = client.calls[-1]
    assert params == {
        "operation": "get_object",
        "Params": {
            "Bucket": "saved-export-bucket",
            "Key": physical.prefix_for(BUNDLE) + RELATIVE,
        },
        "ExpiresIn": 1,
    }
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        physical.signed_url(client, row, RELATIVE, now=row.expires_at)
    assert failure.value.code.endswith("expired")


@pytest.mark.parametrize("versioned", [True, False])
def test_gc_removes_only_saved_known_subset_and_all_versions_even_after_expiry(
    versioned,
):
    row, client = record(count=2), Client(page_size=1, versioned=versioned)
    row.created_at -= timedelta(days=8)
    row.expires_at -= timedelta(days=8)
    key = next(iter(row.inventory["objects"]))
    client.add(key)
    if versioned:
        client.add(key, marker=True)
    unrelated = physical.prefix_for(uuid.UUID(int=800)) + RELATIVE
    client.add(unrelated)
    assert physical.delete_inventory(client, row) == (2 if versioned else 1)
    assert client.current(unrelated)["data"] == RAW
    assert not client.rows[key]
    for method, params in client.calls:
        assert params.get("Bucket") == "saved-export-bucket"
        if method in {"versions", "list"}:
            assert params["Prefix"] == physical.prefix_for(BUNDLE)


@pytest.mark.parametrize("hidden", [False, True])
def test_gc_refuses_unknown_object_or_hidden_version_before_any_delete(hidden):
    row, client = record(), Client()
    populate(client, row)
    unknown = physical.prefix_for(BUNDLE) + str(uuid.UUID(int=600)) + ".bin"
    client.add(unknown)
    if hidden:
        client.add(unknown, marker=True)
    with pytest.raises(physical.ExportMediaStorageError):
        physical.delete_inventory(client, row)
    assert not any(method == "delete" for method, _ in client.calls)


def test_gc_failed_delete_keeps_saved_responsibility_and_reports_failure():
    row, client = record(), Client()
    populate(client, row)
    before = deepcopy(row.inventory)
    client.delete_object = Mock(side_effect=OSError("delete failed"))
    with pytest.raises(physical.ExportMediaStorageError):
        physical.delete_inventory(client, row)
    assert row.inventory == before
    assert client.current(next(iter(row.inventory["objects"])))["data"] == RAW


@pytest.mark.parametrize("operation", ["read", "write"])
def test_cancelled_io_closes_all_owned_handles_and_preserves_borrowed_fd(operation):
    row, client = record(), Client()
    with tempfile.TemporaryFile() as source:
        source.write(RAW)
        source.seek(2)
        if operation == "read":
            populate(client, row)

            def fail_read(response):
                response["Body"].read = Mock(side_effect=Cancelled())
                return response

            client.mutate_get = fail_read
        else:
            original_put = client.put_object

            def fail_put(**params):
                original_put(**params)
                raise Cancelled()

            client.put_object = fail_put
        with pytest.raises(Cancelled):
            with physical.open_storage(row, lambda: client):
                if operation == "read":
                    physical.verify_object(client, row, RELATIVE)
                else:
                    physical.write_object(client, row, RELATIVE, source.fileno())
        assert source.tell() == 2 and not source.closed
        assert client.close_count == 1
        assert all(body.closed and body.close_count == 1 for body in client.bodies)
        if operation == "write":
            put = next(params for method, params in client.calls if method == "put")
            assert put["Body"].closed


@pytest.mark.parametrize("resource", ["body", "client"])
def test_base_exception_during_close_is_unsettled_and_client_still_closes(resource):
    row, client = record(), Client()
    populate(client, row)
    if resource == "body":
        client.body_close_failure = Cancelled()
    else:
        client.close_failure = Cancelled()
    with pytest.raises(Cancelled) as failure:
        with physical.open_storage(row, lambda: client):
            physical.verify_object(client, row, RELATIVE)
    assert failure.value.storage_operation_unsettled
    assert client.close_count == 1


def test_constructor_guard_failure_closes_the_created_client(monkeypatch):
    import boto3

    row, client = record(), Client()
    client.meta.events.register.side_effect = RuntimeError("guard failed")
    monkeypatch.setattr(boto3, "client", Mock(return_value=client))
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        with physical.open_storage(row):
            pytest.fail("a client without the endpoint guard must never escape")
    assert failure.value.storage_operation_unsettled
    assert client.close_count == 1


def test_write_rechecks_expiry_after_source_verification_before_put(monkeypatch):
    row, client = record(), Client()
    check_live, calls = physical._live, 0

    def expire_after_source(record):
        nonlocal calls
        calls += 1
        if calls == 2:
            record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        return check_live(record)

    monkeypatch.setattr(physical, "_live", expire_after_source)
    with tempfile.TemporaryFile() as source:
        source.write(RAW)
        source.seek(2)
        with pytest.raises(physical.ExportMediaStorageError) as failure:
            physical.write_object(client, row, RELATIVE, source.fileno())
        assert source.tell() == 2 and not source.closed
    assert failure.value.code.endswith("expired")
    assert not client.calls


@pytest.mark.parametrize("operation", ["list", "versions"])
def test_pagination_cycles_are_rejected_before_any_delete(operation):
    row, client = record(), Client()
    key = next(iter(row.inventory["objects"]))
    if operation == "list":
        client.list_objects_v2 = Mock(
            return_value=ok(
                Contents=[], IsTruncated=True, NextContinuationToken="cycle"
            )
        )
    else:
        client.list_object_versions = Mock(
            return_value=ok(
                Versions=[],
                DeleteMarkers=[],
                IsTruncated=True,
                NextKeyMarker=key,
                NextVersionIdMarker="cycle",
            )
        )
    with pytest.raises(physical.ExportMediaStorageError):
        physical.delete_inventory(client, row)
    assert not any(method == "delete" for method, _ in client.calls)


def test_gc_post_delete_unknown_object_is_not_deleted_or_reported_clean():
    row, client = record(), Client()
    populate(client, row)
    unknown = physical.prefix_for(BUNDLE) + str(uuid.UUID(int=987)) + ".bin"
    original_delete = client.delete_object

    def delete_and_introduce_unknown(**params):
        response = original_delete(**params)
        client.add(unknown)
        return response

    client.delete_object = delete_and_introduce_unknown
    with pytest.raises(physical.ExportMediaStorageError):
        physical.delete_inventory(client, row)
    assert client.current(unknown)["data"] == RAW
    assert all(
        params["Key"] != unknown
        for method, params in client.calls
        if method == "delete"
    )


@pytest.mark.parametrize("public", ["", "/minio", "https://media.example.test/minio"])
def test_saved_media_signing_honors_public_gateway_without_changing_signature(
    monkeypatch, public
):
    row, client = record(), Client()
    raw = (
        physical._runtime_endpoint()
        + "/saved-export-bucket/"
        + physical.prefix_for(BUNDLE)
        + RELATIVE
        + "?signature=a%2Fb&expires=123"
    )
    monkeypatch.setattr(physical.settings, "minio_public_url", public)
    monkeypatch.setattr(client, "generate_presigned_url", lambda *a, **kw: raw)
    expected = raw if not public else public + raw[len(physical._runtime_endpoint()) :]
    assert physical.signed_url(client, row, RELATIVE) == expected


def test_empty_object_put_readback_uses_saved_owner_exact_hash_eof_and_real_closes(
    monkeypatch,
):
    client, row = Client(), record()
    key = physical.prefix_for(BUNDLE) + RELATIVE
    row.inventory["objects"][key] = receipt(b"")
    monkeypatch.setattr(physical, "create_client", lambda: client)
    reads = []

    def track(value):
        body = value["Body"]
        original = body.read

        def read(size):
            reads.append(size)
            return original(size)

        body.read = read
        return value

    client.mutate_get = track
    with tempfile.TemporaryFile() as source:
        with physical.open_storage(row) as owned:
            assert physical.write_object(
                owned, row, RELATIVE, source.fileno()
            ) == receipt(b"")
            assert physical.verify_object(owned, row, RELATIVE) == receipt(b"")
            assert physical.verify_inventory(owned, row) == row.inventory
            assert physical.list_objects(owned, row) == {key: 0}
        assert not source.closed and source.tell() == 0
    assert reads == [1, 1, 1]
    assert all(body.closed and body.close_count == 1 for body in client.bodies)
    assert client.close_count == 1
    put = next(params for method, params in client.calls if method == "put")
    assert put["Bucket"] == "saved-export-bucket" and put["ContentLength"] == 0
    assert put["IfNoneMatch"] == "*" and client.current(key)["data"] == b""


@pytest.mark.parametrize("damage", ["hash", "extra_body", "nonbytes_body", "length"])
def test_empty_readback_does_not_confuse_zero_length_with_verified_eof(damage):
    row, client = record(), Client()
    key = physical.prefix_for(BUNDLE) + RELATIVE
    row.inventory["objects"][key] = receipt(b"")
    client.add(key, b"")
    if damage == "hash":
        row.inventory["objects"][key]["sha256"] = "a" * 64
    else:

        def change(value):
            if damage == "extra_body":
                value["Body"].write(b"x")
                value["Body"].seek(0)
            elif damage == "nonbytes_body":
                value["Body"].read = lambda size: None
            else:
                value["ContentLength"] = 1
            return value

        client.mutate_get = change
    with pytest.raises(physical.ExportMediaStorageError, match="核验") as error:
        physical.verify_object(client, row, RELATIVE)
    assert error.value.code.endswith("integrity_mismatch")
    assert all(body.closed and body.close_count == 1 for body in client.bodies)


def test_empty_put_cannot_publish_a_nonempty_hash_without_a_write():
    row, client = record(), Client()
    row.inventory["objects"][physical.prefix_for(BUNDLE) + RELATIVE] = {
        "sha256": hashlib.sha256(b"nonempty").hexdigest(),
        "size_bytes": 0,
    }
    with tempfile.TemporaryFile() as source:
        with pytest.raises(physical.ExportMediaStorageError):
            physical.write_object(client, row, RELATIVE, source.fileno())
        assert not source.closed
    assert not client.calls
