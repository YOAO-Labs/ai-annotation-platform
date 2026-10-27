"""Real protocol shapes with no database, SDK client, service or object writes."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import io

from botocore.exceptions import ClientError, ReadTimeoutError
import pytest

from app.services import managed_original_writer as writer


def error(code, status=None):
    metadata = {} if status is None else {"HTTPStatusCode": status}
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private provider detail"},
            "ResponseMetadata": metadata,
        },
        "ObjectOperation",
    )


def response(**values):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request"},
        **values,
    }


class Body(io.BytesIO):
    def __init__(self, value, hook=None):
        super().__init__(value)
        self.hook, self.close_count, self.requests = hook, 0, []
        self.close_failure = False

    def read(self, size):
        self.requests.append(size)
        chunk = super().read(size)
        if self.hook:
            self.hook(self, chunk)
        return chunk

    def close(self):
        self.close_count += 1
        super().close()
        if self.close_failure:
            raise OSError("private close detail")


class MemoryS3:
    def __init__(self):
        self.objects, self.uploads, self.versions = {}, {}, {}
        self.calls, self.bodies = [], []
        self.versioned = False
        self.hook, self.body_hook = None, None
        self.ignore_get_condition = False

    def record(self, operation, params):
        self.calls.append((operation, params.copy()))
        if self.hook:
            self.hook(operation, params)

    def store(
        self, bucket, key, data, *, etag=None, content_type="application/octet-stream"
    ):
        version = f"version-{len(self.versions)}" if self.versioned else None
        value = {
            "data": data,
            "ETag": etag or f'"{hashlib.md5(data).hexdigest()}"',
            "VersionId": version,
            "ContentType": content_type,
        }
        self.objects[bucket, key] = value
        if version:
            self.versions[bucket, key, version] = value
        return value

    def current(self, params):
        if params.get("VersionId") is not None:
            value = self.versions.get(
                (params["Bucket"], params["Key"], params["VersionId"])
            )
        else:
            value = self.objects.get((params["Bucket"], params["Key"]))
        if value is None:
            raise error("NoSuchKey", 404)
        return value

    def headers(self, value):
        return response(
            ETag=value["ETag"],
            VersionId=value["VersionId"],
            ContentLength=len(value["data"]),
            ContentType=value["ContentType"],
            LastModified=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

    def create_multipart_upload(self, **params):
        self.record("create", params)
        upload_id = f"upload-{len(self.calls)}"
        self.uploads[upload_id] = {"parts": {}, "params": params}
        return response(UploadId=upload_id)

    def upload_part(self, **params):
        self.record("part", params)
        self.uploads[params["UploadId"]]["parts"][params["PartNumber"]] = params["Body"]
        return response(ETag=f'"part-{params["PartNumber"]}"')

    def upload_part_copy(self, **params):
        self.record("copy", params)
        value = self.current(params["CopySource"])
        if params["CopySourceIfMatch"] != value["ETag"]:
            raise error("PreconditionFailed", 412)
        self.uploads[params["UploadId"]]["parts"][params["PartNumber"]] = value["data"]
        return response(
            CopyPartResult={"ETag": '"copy-part"'},
            CopySourceVersionId=value["VersionId"],
        )

    def abort_multipart_upload(self, **params):
        self.record("abort", params)
        self.uploads.pop(params["UploadId"], None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def complete_multipart_upload(self, **params):
        self.record("complete", params)
        assert params["IfNoneMatch"] == "*"
        if (params["Bucket"], params["Key"]) in self.objects:
            raise error("PreconditionFailed", 412)
        upload = self.uploads.pop(params["UploadId"])
        data = b"".join(
            upload["parts"][p["PartNumber"]] for p in params["MultipartUpload"]["Parts"]
        )
        value = self.store(
            params["Bucket"],
            params["Key"],
            data,
            content_type=upload["params"]["ContentType"],
        )
        return response(ETag=value["ETag"], VersionId=value["VersionId"])

    def put_object(self, **params):
        self.record("put", params)
        assert params["IfNoneMatch"] == "*" and params["Body"] == b""
        if (params["Bucket"], params["Key"]) in self.objects:
            raise error("PreconditionFailed", 412)
        value = self.store(
            params["Bucket"], params["Key"], b"", content_type=params["ContentType"]
        )
        return response(ETag=value["ETag"], VersionId=value["VersionId"])

    def head_object(self, **params):
        self.record("head", params)
        return self.headers(self.current(params))

    def get_object(self, **params):
        self.record("get", params)
        value = self.current(params)
        if not self.ignore_get_condition and params.get("IfMatch") != value["ETag"]:
            raise error("PreconditionFailed", 412)
        body = Body(value["data"], self.body_hook)
        self.bodies.append(body)
        return {**self.headers(value), "Body": body}


def prepare(client, data=b"original", **kwargs):
    return writer.prepare_stream(
        client,
        "owned",
        "final",
        io.BytesIO(data),
        content_type="image/png",
        max_bytes=kwargs.pop("max_bytes", len(data)),
        **kwargs,
    )


def seal(client, data=b"original"):
    prepared = prepare(client, data)
    return prepared, writer.complete(
        client, "owned", "final", prepared, content_type="image/png"
    )


def check_error(caught, outcome="failed", code=None):
    assert caught.value.outcome == outcome
    assert bool(getattr(caught.value, "storage_operation_unsettled", False)) == (
        outcome == "unknown"
    )
    assert "private" not in str(caught.value)
    if code:
        assert caught.value.code == code


@pytest.mark.parametrize("data", [b"", b"small", b"a" * (writer.PART_BYTES + 9)])
def test_stream_complete_and_full_readback(data):
    client = MemoryS3()
    prepared, receipt = seal(client, data)
    assert ("owned", "final") in client.objects
    assert prepared.sha256 == hashlib.sha256(data).hexdigest()
    assert prepared.md5 == hashlib.md5(data).hexdigest()
    assert prepared.size_bytes == len(data)
    assert prepared.head_bytes == data[: writer.HEAD_BYTES]
    verified = writer.verify(
        client,
        "owned",
        "final",
        receipt,
        sha256=prepared.sha256,
        size_bytes=len(data),
        max_bytes=len(data),
    )
    assert verified.sha256 == prepared.sha256 and verified.md5 == prepared.md5
    assert verified.head_bytes == prepared.head_bytes
    assert all(body.close_count == 1 for body in client.bodies)
    assert all(
        size == writer.CHUNK_BYTES for body in client.bodies for size in body.requests
    )
    assert not any(op == "abort" for op, _ in client.calls)
    with pytest.raises(FrozenInstanceError):
        prepared.sha256 = "other"


def test_prepare_never_writes_final_and_fills_short_stream_reads():
    class Short(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 32768))

    data = b"x" * (writer.PART_BYTES + 3)
    client = MemoryS3()
    prepared = writer.prepare_stream(
        client,
        "owned",
        "final",
        Short(data),
        content_type="x/test",
        max_bytes=len(data),
    )
    parts = [params["Body"] for op, params in client.calls if op == "part"]
    assert [len(part) for part in parts] == [writer.PART_BYTES, 3]
    assert b"".join(parts) == data and not client.objects
    writer.abort_prepared(client, "owned", "final", prepared)
    assert not client.uploads and client.calls[-1][0] == "abort"


@pytest.mark.parametrize("existing", [b"original", b"unrelated"])
def test_existing_final_even_same_sha_is_collision_never_deleted(existing):
    client = MemoryS3()
    client.store("owned", "final", existing)
    prepared = prepare(client)
    with pytest.raises(writer.WriterError) as caught:
        writer.complete(client, "owned", "final", prepared, content_type="image/png")
    check_error(caught, "conflict", "creation_write_conflict")
    assert client.objects["owned", "final"]["data"] == existing
    assert len([1 for op, _ in client.calls if op == "complete"]) == 1
    assert not any(op == "abort" for op, _ in client.calls)


@pytest.mark.parametrize("excess", [1, writer.PART_BYTES + 1])
def test_actual_byte_limit_aborts_only_known_mpu(excess):
    client = MemoryS3()
    maximum = writer.PART_BYTES
    with pytest.raises(writer.WriterError) as caught:
        prepare(client, b"x" * (maximum + excess), max_bytes=maximum)
    check_error(caught, code="creation_byte_budget_exceeded")
    assert [op for op, _ in client.calls] == ["create", "part", "abort"]
    assert not client.objects and not client.uploads


@pytest.mark.parametrize("maximum", [-1, True, None, 1.5])
def test_invalid_budget_has_zero_io(maximum):
    client = MemoryS3()
    with pytest.raises(writer.WriterError):
        prepare(client, max_bytes=maximum)
    assert not client.calls


def test_no_global_five_gib_stream_cap(monkeypatch):
    monkeypatch.setattr(writer, "MAX_COPY_BYTES", 4)
    prepared = prepare(MemoryS3(), b"more than four")
    assert prepared.size_bytes > writer.MAX_COPY_BYTES


@pytest.mark.parametrize("operation", ["create", "part", "abort"])
def test_unknown_preparation_and_abort_keep_responsibility(operation):
    client = MemoryS3()

    def hook(op, params):
        if op == operation or (operation == "abort" and op == "part"):
            raise ReadTimeoutError(endpoint_url="private")

    client.hook = hook
    with pytest.raises(writer.WriterError) as caught:
        prepare(client)
    check_error(caught, "unknown")
    assert not client.objects
    assert not any(op == "complete" for op, _ in client.calls)


def test_stream_local_failure_aborts_then_is_known_failure():
    class Broken(io.BytesIO):
        def read(self, size):
            if self.tell() >= writer.PART_BYTES:
                raise OSError("private source detail")
            return super().read(size)

    client = MemoryS3()
    with pytest.raises(writer.WriterError) as caught:
        writer.prepare_stream(
            client,
            "owned",
            "final",
            Broken(b"a" * writer.PART_BYTES),
            content_type="x/test",
            max_bytes=writer.PART_BYTES * 2,
        )
    check_error(caught)
    assert client.calls[-1][0] == "abort" and not client.uploads


def test_adaptive_parts_allow_large_connector_budget_without_large_test_data():
    class Requests(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.requests = []

        def read(self, size):
            self.requests.append(size)
            return super().read(size)

    client = MemoryS3()
    stream = Requests(b"x" * (writer.PART_BYTES + 1))
    prepared = writer.prepare_stream(
        client, "owned", "final", stream, content_type="x/test", max_bytes=200 * 1024**3
    )
    assert stream.requests[0] == 21 * 1024**2
    assert len(prepared.parts) == 1
    assert prepared.size_bytes == writer.PART_BYTES + 1
    assert not client.objects


def test_protocol_part_limit_rejects_impossible_budget_without_io():
    client = MemoryS3()
    with pytest.raises(writer.WriterError) as caught:
        prepare(client, max_bytes=writer.MAX_PART_BYTES * writer.MAX_PARTS + 1)
    check_error(caught, code="creation_part_budget_exceeded")
    assert not client.calls


@pytest.mark.parametrize("version", [False, True])
@pytest.mark.parametrize("data", [b"", b"tiny", b"x" * (5 * 1024 * 1024 + 1)])
def test_staging_inspection_and_whole_part_copy(version, data):
    client = MemoryS3()
    client.versioned = version
    client.store("stage", "source", data, content_type="image/png")
    source = writer.inspect_staging(client, "stage", "source", max_bytes=len(data))
    assert source.sha256 == hashlib.sha256(data).hexdigest()
    assert source.observation["content_type"] == "image/png"
    prepared = writer.prepare_copy(
        client, "stage", "source", "owned", "final", source, content_type="image/png"
    )
    receipt = writer.complete(
        client, "owned", "final", prepared, content_type="image/png"
    )
    writer.verify(
        client,
        "owned",
        "final",
        receipt,
        sha256=source.sha256,
        size_bytes=len(data),
        max_bytes=len(data),
    )
    copies = [params for op, params in client.calls if op == "copy"]
    if data:
        assert len(copies) == 1 and "CopySourceRange" not in copies[0]
        assert copies[0]["CopySourceIfMatch"] == source.etag
        assert copies[0]["CopySource"].get("VersionId") == source.version_id
    else:
        assert not copies
    assert client.objects["owned", "final"]["data"] == data
    assert not hasattr(client, "copy_object")
    assert all(body.close_count == 1 for body in client.bodies)


def test_copy_source_replaced_after_inspection_aborts_exact_mpu():
    client = MemoryS3()
    client.store("stage", "source", b"original")
    source = writer.inspect_staging(client, "stage", "source", max_bytes=8)
    client.store("stage", "source", b"replaced")
    with pytest.raises(writer.WriterError) as caught:
        writer.prepare_copy(
            client,
            "stage",
            "source",
            "owned",
            "final",
            source,
            content_type="image/png",
        )
    check_error(caught, code="creation_source_changed")
    assert client.calls[-1][0] == "abort" and not client.uploads
    assert ("owned", "final") not in client.objects


@pytest.mark.parametrize("versioned", [False, True])
@pytest.mark.parametrize("data", [b"", b"external original"])
def test_separate_source_client_streams_to_owned_destination(versioned, data):
    reader, destination = MemoryS3(), MemoryS3()
    reader.versioned = versioned
    reader.store("stage", "source", data)
    source = writer.inspect_staging(reader, "stage", "source", max_bytes=len(data))
    prepared = writer.prepare_copy(
        destination,
        "stage",
        "source",
        "owned",
        "final",
        source,
        content_type="application/octet-stream",
        source_client=reader,
    )
    receipt = writer.complete(
        destination, "owned", "final", prepared, content_type="application/octet-stream"
    )
    verified = writer.verify(
        destination,
        "owned",
        "final",
        receipt,
        sha256=source.sha256,
        size_bytes=len(data),
        max_bytes=len(data),
    )
    assert verified.sha256 == hashlib.sha256(data).hexdigest()
    assert destination.objects["owned", "final"]["data"] == data
    assert reader.objects["stage", "source"]["data"] == data
    assert all(operation in {"head", "get"} for operation, _ in reader.calls)
    assert all(
        ("VersionId" in params) is versioned
        for operation, params in reader.calls
        if operation == "get"
    )
    assert not any(operation == "copy" for operation, _ in destination.calls)
    assert not any(params.get("Bucket") == "stage" for _, params in destination.calls)
    assert all(body.close_count == 1 for body in reader.bodies + destination.bodies)


@pytest.mark.parametrize("change", ["replace_current", "corrupt_read", "close_failure"])
def test_cross_client_failure_aborts_only_destination_mpu(change):
    reader, destination = MemoryS3(), MemoryS3()
    reader.versioned = True
    reader.store("stage", "source", b"original")
    source = writer.inspect_staging(reader, "stage", "source", max_bytes=8)
    if change == "replace_current":
        reader.store("stage", "source", b"replaced")
    elif change == "corrupt_read":
        reader.versions["stage", "source", source.version_id]["data"] = b"corrupt!"
    else:
        reader.body_hook = lambda body, chunk: setattr(body, "close_failure", True)
    with pytest.raises(writer.WriterError):
        writer.prepare_copy(
            destination,
            "stage",
            "source",
            "owned",
            "final",
            source,
            content_type="application/octet-stream",
            source_client=reader,
        )
    assert not destination.uploads and ("owned", "final") not in destination.objects
    assert not reader.uploads
    assert ("stage", "source") in reader.objects
    assert all(operation in {"head", "get"} for operation, _ in reader.calls)


def test_copy_pins_old_nonnull_source_version_after_current_replacement():
    client = MemoryS3()
    client.versioned = True
    client.store("stage", "source", b"original")
    source = writer.inspect_staging(client, "stage", "source", max_bytes=8)
    client.store("stage", "source", b"replaced")
    prepared = writer.prepare_copy(
        client, "stage", "source", "owned", "final", source, content_type="image/png"
    )
    writer.complete(client, "owned", "final", prepared, content_type="image/png")
    assert client.objects["owned", "final"]["data"] == b"original"


def test_copy_bound_and_source_locator_validation_have_no_destination_io():
    client = MemoryS3()
    client.store("stage", "source", b"a")
    source = writer.inspect_staging(client, "stage", "source", max_bytes=1)
    client.calls.clear()
    for invalid in [
        replace(source, size_bytes=writer.MAX_COPY_BYTES + 1),
        replace(source, observation={}),
        replace(source, etag="\r\n"),
    ]:
        with pytest.raises(writer.WriterError):
            writer.prepare_copy(
                client,
                "stage",
                "source",
                "owned",
                "final",
                invalid,
                content_type="x/test",
            )
        assert not client.calls


@pytest.mark.parametrize("mutation", ["sha", "version", "etag", "size", "current"])
def test_verify_rejects_receipt_or_content_or_currentness_changes(mutation):
    client = MemoryS3()
    client.versioned = True
    prepared, receipt = seal(client)
    expected_sha = prepared.sha256
    if mutation == "sha":
        expected_sha = "0" * 64
    elif mutation == "version":
        receipt = replace(receipt, version_id=None)
    elif mutation == "etag":
        receipt = replace(receipt, etag='"other"')
    elif mutation == "size":
        client.objects["owned", "final"]["data"] += b"x"
    else:

        def hook(body, chunk):
            client.store("owned", "final", b"different")
            body.hook = None

        client.body_hook = hook
    with pytest.raises(writer.WriterError) as caught:
        writer.verify(
            client,
            "owned",
            "final",
            receipt,
            sha256=expected_sha,
            size_bytes=prepared.size_bytes,
            max_bytes=prepared.size_bytes,
        )
    check_error(caught)
    assert all(body.close_count == 1 for body in client.bodies)
    assert not any(op == "abort" for op, _ in client.calls)


def test_ignored_read_condition_is_not_qualified():
    client = MemoryS3()
    client.store("stage", "source", b"a")
    client.ignore_get_condition = True
    with pytest.raises(writer.WriterError):
        writer.inspect_staging(client, "stage", "source", max_bytes=1)
    assert client.bodies[0].close_count == 1


def test_body_close_failure_is_unknown_not_success():
    client = MemoryS3()
    client.store("stage", "source", b"a")

    def hook(body, chunk):
        body.close_failure = True

    client.body_hook = hook
    with pytest.raises(writer.WriterError) as caught:
        writer.inspect_staging(client, "stage", "source", max_bytes=1)
    check_error(caught, "unknown", "creation_close_unsettled")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        response(),
        response(ETag="\r\n"),
        response(ETag='"ok"', VersionId=""),
        response(ETag='"ok"', Error={"Code": "InternalError"}),
    ],
)
def test_success_response_missing_or_malformed_receipt_is_unknown(monkeypatch, bad):
    client = MemoryS3()
    prepared = prepare(client)
    monkeypatch.setattr(client, "complete_multipart_upload", lambda **kwargs: bad)
    with pytest.raises(writer.WriterError) as caught:
        writer.complete(client, "owned", "final", prepared, content_type="image/png")
    check_error(caught, "unknown")
    assert not any(op == "abort" for op, _ in client.calls)


@pytest.mark.parametrize(
    "failure,outcome",
    [
        (error("AccessDenied", 403), "failed"),
        (error("PreconditionFailed", 412), "conflict"),
        (error("InternalError", 500), "unknown"),
        (error("NoSuchUpload", 404), "unknown"),
        (ReadTimeoutError(endpoint_url="private"), "unknown"),
    ],
)
def test_completion_errors_are_safe_and_never_automatically_retried(
    monkeypatch, failure, outcome
):
    client = MemoryS3()
    prepared = prepare(client)
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise failure

    monkeypatch.setattr(client, "complete_multipart_upload", fail)
    with pytest.raises(writer.WriterError) as caught:
        writer.complete(client, "owned", "final", prepared, content_type="image/png")
    check_error(caught, outcome)
    assert len(calls) == 1 and not any(op == "abort" for op, _ in client.calls)


def test_complete_ack_lost_does_not_infer_success_from_final_digest(monkeypatch):
    client = MemoryS3()
    prepared = prepare(client)
    complete = client.complete_multipart_upload

    def lose_ack(**kwargs):
        complete(**kwargs)
        raise ReadTimeoutError(endpoint_url="private")

    monkeypatch.setattr(client, "complete_multipart_upload", lose_ack)
    with pytest.raises(writer.WriterError) as caught:
        writer.complete(client, "owned", "final", prepared, content_type="image/png")
    check_error(caught, "unknown")
    assert client.objects["owned", "final"]["data"] == b"original"
    assert not any(op in ("head", "get", "abort") for op, _ in client.calls)


@pytest.mark.parametrize("version", [None, "null", "opaque/version+id"])
def test_receipt_retains_actual_version_and_safe_response_only(monkeypatch, version):
    client = MemoryS3()
    prepared = prepare(client)
    monkeypatch.setattr(
        client,
        "complete_multipart_upload",
        lambda **kwargs: response(
            ETag='"not-a-sha"',
            VersionId=version,
            Private="secret",
            Location="private URL",
        ),
    )
    receipt = writer.complete(
        client, "owned", "final", prepared, content_type="image/png"
    )
    assert receipt.version_id == (None if version in (None, "null") else version)
    assert receipt.etag == '"not-a-sha"' and receipt.request_id == "request"
    assert set(receipt.response) == {"ETag", "VersionId", "ResponseMetadata"}
    assert "secret" not in str(receipt.response)


def test_deadline_checks_after_blocking_read_and_closes_body(monkeypatch):
    clock = [0]
    monkeypatch.setattr(writer.time, "monotonic", lambda: clock[0])
    client = MemoryS3()
    client.store("stage", "source", b"a")
    client.body_hook = lambda body, chunk: clock.__setitem__(0, 61)
    with pytest.raises(writer.WriterError) as caught:
        writer.inspect_staging(client, "stage", "source", max_bytes=1)
    check_error(caught, code="creation_timeout")
    assert client.bodies[0].close_count == 1


def test_completion_requires_matching_prepared_locator_and_zero_hash():
    client = MemoryS3()
    prepared = prepare(client, b"")
    for invalid in [
        replace(prepared, object_key="another"),
        replace(prepared, sha256="0" * 64),
        replace(prepared, parts=({"PartNumber": 1, "ETag": '"part"'},)),
    ]:
        with pytest.raises(writer.WriterError) as caught:
            writer.complete(client, "owned", "final", invalid, content_type="image/png")
        check_error(caught)
    assert not client.calls


@pytest.mark.parametrize(
    "failure",
    [
        error("PreconditionFailed", 403),
        error("PreconditionFailed", 500),
        error("AccessDenied", 403),
    ],
)
def test_contradictory_negative_get_condition_cannot_qualify(monkeypatch, failure):
    client = MemoryS3()
    client.store("stage", "source", b"a")
    get = client.get_object

    def inconsistent(**params):
        if params.get("IfMatch") == '"aap-impossible-etag"':
            raise failure
        return get(**params)

    monkeypatch.setattr(client, "get_object", inconsistent)
    with pytest.raises(writer.WriterError) as caught:
        writer.inspect_staging(client, "stage", "source", max_bytes=1)
    check_error(caught, code="creation_conditions_unverified")
    assert not client.bodies


@pytest.mark.parametrize("version", ["unexpected", "", "\r\n"])
def test_copy_response_source_version_disagreement_aborts(monkeypatch, version):
    client = MemoryS3()
    client.store("stage", "source", b"a")
    source = writer.inspect_staging(client, "stage", "source", max_bytes=1)
    monkeypatch.setattr(
        client,
        "upload_part_copy",
        lambda **params: response(
            CopyPartResult={"ETag": '"part"'}, CopySourceVersionId=version
        ),
    )
    with pytest.raises(writer.WriterError) as caught:
        writer.prepare_copy(
            client,
            "stage",
            "source",
            "owned",
            "final",
            source,
            content_type="image/png",
        )
    check_error(caught)
    assert client.calls[-1][0] == "abort" and not client.uploads


@pytest.mark.parametrize("data", [b"", b"short"])
def test_truncated_staging_body_is_never_accepted(monkeypatch, data):
    client = MemoryS3()
    client.store("stage", "source", b"original")
    get = client.get_object

    def truncated(**params):
        result = get(**params)
        result["Body"].close()
        body = Body(data)
        client.bodies.append(body)
        result["Body"] = body
        return result

    monkeypatch.setattr(client, "get_object", truncated)
    with pytest.raises(writer.WriterError) as caught:
        writer.inspect_staging(client, "stage", "source", max_bytes=8)
    check_error(caught, code="creation_source_changed")
    assert all(body.close_count == 1 for body in client.bodies)


def test_staging_head_above_budget_never_opens_body():
    client = MemoryS3()
    client.store("stage", "source", b"too large")
    with pytest.raises(writer.WriterError) as caught:
        writer.inspect_staging(client, "stage", "source", max_bytes=1)
    check_error(caught, code="creation_byte_budget_exceeded")
    assert [op for op, _ in client.calls] == ["head"]


def test_time_expired_after_stream_read_never_starts_mpu(monkeypatch):
    clock = [0]
    monkeypatch.setattr(writer.time, "monotonic", lambda: clock[0])

    class Slow(io.BytesIO):
        def read(self, size):
            data = super().read(size)
            clock[0] = 61
            return data

    client = MemoryS3()
    with pytest.raises(writer.WriterError) as caught:
        writer.prepare_stream(
            client, "owned", "final", Slow(b"a"), content_type="x/test", max_bytes=1
        )
    check_error(caught, code="creation_timeout")
    assert not client.calls


def test_stream_must_obey_requested_byte_bound():
    class Invalid:
        def read(self, size):
            return b"x" * (size + 1)

    client = MemoryS3()
    with pytest.raises(writer.WriterError) as caught:
        writer.prepare_stream(
            client, "owned", "final", Invalid(), content_type="x/test", max_bytes=1
        )
    check_error(caught, code="creation_invalid_stream")
    assert not client.calls


@pytest.mark.parametrize("version", [False, True])
def test_mutable_staging_changed_during_full_read_is_rejected(version):
    client = MemoryS3()
    client.versioned = version
    client.store("stage", "source", b"original")

    def change(body, chunk):
        client.store("stage", "source", b"replaced")
        body.hook = None

    client.body_hook = change
    with pytest.raises(writer.WriterError) as caught:
        writer.inspect_staging(client, "stage", "source", max_bytes=8)
    check_error(caught, code="creation_source_changed")
    assert all(body.close_count == 1 for body in client.bodies)


def test_bad_metadata_error_shapes_still_preserve_unknown_completion(monkeypatch):
    client = MemoryS3()
    prepared = prepare(client)
    failure = error("InternalError", 500)
    failure.response = {"ResponseMetadata": None, "Error": None}

    def fail(**params):
        raise failure

    monkeypatch.setattr(client, "complete_multipart_upload", fail)
    with pytest.raises(writer.WriterError) as caught:
        writer.complete(client, "owned", "final", prepared, content_type="image/png")
    check_error(caught, "unknown")
