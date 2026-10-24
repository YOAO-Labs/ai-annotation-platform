"""Strict physical reads only: no database, real provider or business fixtures."""

import asyncio
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
import hashlib
import io
import threading
from types import SimpleNamespace

from botocore.exceptions import ClientError
import pytest

from app.services import managed_original_reader as service
from app.services.content_snapshot import ContentError

ENDPOINT = "http://originals.test:9000"
RAW = b"original binary\x00\xff"


def error(code="PreconditionFailed", status=412):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private provider detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "ObjectRead",
    )


class Body(io.BytesIO):
    def __init__(self, provider, raw):
        super().__init__(raw)
        self.provider, self.requests = provider, []

    def read(self, count):
        self.requests.append(count)
        if self.provider.read_hook:
            self.provider.read_hook(self)
        return super().read(count)

    def close(self):
        if not self.closed:
            self.provider.calls.append(("body_close", {}))
            super().close()
            if self.provider.body_close_hook:
                self.provider.body_close_hook()
            if self.provider.body_close_error:
                raise self.provider.body_close_error


class Provider:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url=ENDPOINT)
        self.raw, self.actual = RAW, RAW
        self.etag, self.version = '"target-etag"', None
        self.modified = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.calls, self.bodies, self.closed = [], [], 0
        self.head_changes, self.get_changes = {}, {}
        self.ignore_conditions = False
        self.head_hook = self.read_hook = self.body_close_hook = None
        self.client_close_hook = None
        self.body_close_error = self.client_close_error = None
        self.body_factory = lambda: Body(self, self.actual)
        self.head_error = self.get_error = None

    def metadata(self):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(self.raw),
            "ETag": self.etag,
            "LastModified": self.modified,
            "VersionId": self.version,
            "ContentType": "application/octet-stream",
        }

    def head_object(self, **params):
        self.calls.append(("head", params))
        if self.head_hook:
            self.head_hook()
        if self.head_error:
            raise self.head_error
        return self.metadata() | self.head_changes

    def get_object(self, **params):
        self.calls.append(("get", params))
        if not self.ignore_conditions and params.get("IfMatch") != self.etag:
            raise error()
        assert params.get("VersionId") == (
            None if self.version == "null" else self.version
        )
        if self.get_error:
            raise self.get_error
        body = self.body_factory()
        self.bodies.append(body)
        return self.metadata() | {"Body": body} | self.get_changes

    def close(self):
        self.calls.append(("client_close", {}))
        self.closed += 1
        if self.client_close_hook:
            self.client_close_hook()
        if self.client_close_error:
            raise self.client_close_error


@pytest.fixture
def provider(monkeypatch):
    value = Provider()
    monkeypatch.setattr(service, "create_client", lambda **_: value)
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(4))
    return value


def spec(**changes):
    return service.ObjectSpec(
        **(
            {
                "endpoint_url": ENDPOINT,
                "bucket": "saved-originals",
                "object_key": "images/原件 空格%25.bin",
                "sha256": hashlib.sha256(RAW).hexdigest(),
                "size_bytes": len(RAW),
            }
            | changes
        )
    )


async def capture(value=None, **kwargs):
    return await service.capture(
        value or spec(), **({"max_bytes": len(RAW), "timeout_seconds": 3} | kwargs)
    )


@pytest.mark.parametrize("version", [None, "null", "restored-target-version"])
async def test_full_saved_bucket_read_uses_current_target_and_closes_in_order(
    provider, version
):
    provider.version = version
    stage = await capture()
    try:
        assert stage.file.read() == RAW
        assert stage.sha256 == spec().sha256 and stage.size == len(RAW)
        assert stage.observation["bucket"] == "saved-originals"
        assert stage.observation["version_id"] == (
            None if version == "null" else version
        )
        assert all(body.closed for body in provider.bodies) and provider.closed == 1
        assert [action for action, _ in provider.calls] == [
            "head",
            "get",
            "get",
            "body_close",
            "head",
            "client_close",
        ]
        for action, params in provider.calls:
            if action in {"head", "get"}:
                assert params["Bucket"] == "saved-originals"
                assert params["Key"] == spec().object_key
            if action == "head":
                assert "VersionId" not in params
        assert provider.calls[2][1]["IfMatch"] == '"target-etag"'
        assert service._slots._value == 3
    finally:
        stage.close()
        stage.close()
    assert stage.file.closed and service._slots._value == 4
    assert {field.name for field in fields(service.ObjectSpec)} == {
        "endpoint_url",
        "bucket",
        "object_key",
        "sha256",
        "size_bytes",
    }
    with pytest.raises(FrozenInstanceError):
        spec().bucket = "current-bucket"


async def test_empty_binary_still_reads_eof_and_never_decodes(provider):
    provider.raw = provider.actual = b""
    stage = await capture(
        spec(size_bytes=0, sha256=hashlib.sha256(b"").hexdigest()), max_bytes=0
    )
    try:
        assert stage.file.read() == b"" and stage.size == 0
        assert provider.bodies[0].requests == [1]
    finally:
        stage.close()


async def test_absent_and_null_version_are_one_current_transport_identity(provider):
    provider.get_changes = {"VersionId": "null"}
    stage = await capture()
    try:
        assert stage.observation["version_id"] is None
    finally:
        stage.close()


async def test_capability_probe_condition_is_always_false_for_observed_etag(provider):
    provider.etag = '"aap-impossible-etag"'
    stage = await capture()
    try:
        assert provider.calls[1][1]["IfMatch"] != provider.etag
        assert provider.calls[2][1]["IfMatch"] == provider.etag
    finally:
        stage.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint_url": "http://user:secret@originals.test"},
        {"endpoint_url": "http://originals.test/path"},
        {"endpoint_url": "http://originals.test/"},
        {"endpoint_url": "http://originals.test:0"},
        {"bucket": "bad/path"},
        {"bucket": "bad bucket"},
        {"object_key": "bad\nkey"},
        {"object_key": "x" * 1025},
        {"sha256": "A" * 64},
        {"size_bytes": True},
        {"size_bytes": -1},
        {"size_bytes": 2**63},
    ],
)
def test_object_spec_rejects_ambiguous_or_unsafe_inputs(changes):
    with pytest.raises(ContentError, match="managed_original_read_invalid"):
        spec(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_bytes": True},
        {"max_bytes": -1},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 10**1000},
    ],
)
async def test_invalid_explicit_budgets_do_no_provider_io(provider, changes):
    with pytest.raises(ContentError, match="managed_original_read_invalid"):
        await capture(**changes)
    assert provider.calls == [] and service._slots._value == 4


async def test_declared_byte_budget_rejects_before_constructor(provider):
    with pytest.raises(ContentError, match="content_too_large") as caught:
        await capture(max_bytes=len(RAW) - 1)
    assert caught.value.status == 413 and provider.calls == []


async def test_endpoint_mismatch_closes_only_own_client_before_object_io(provider):
    provider.meta.endpoint_url = "http://other.test:9000"
    with pytest.raises(ContentError, match="managed_original_endpoint_changed"):
        await capture()
    assert [action for action, _ in provider.calls] == ["client_close"]
    assert provider.closed == 1 and service._slots._value == 4


@pytest.mark.parametrize("phase", ["head", "get"])
@pytest.mark.parametrize(
    "changes",
    [
        {"ResponseMetadata": {"HTTPStatusCode": 206}},
        {"ResponseMetadata": {"HTTPStatusCode": 200.0}},
        {"ContentRange": "bytes 0-1/10"},
        {"DeleteMarker": True},
        {"DeleteMarker": "false"},
        {"ContentLength": True},
        {"ContentLength": len(RAW) + 1},
        {"ETag": "bad\nheader"},
        {"ETag": ""},
        {"LastModified": datetime(2026, 1, 1)},
        {"VersionId": True},
        {"VersionId": ""},
        {"Error": {"Code": "NoSuchKey"}},
    ],
)
async def test_invalid_metadata_is_never_a_verified_file(provider, phase, changes):
    setattr(provider, phase + "_changes", changes)
    with pytest.raises(ContentError):
        await capture()
    assert provider.closed == 1 and all(body.closed for body in provider.bodies)
    assert service._slots._value == 4


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"x" * len(RAW), "content_mismatch"),
        (RAW[:-1], "source_changed"),
        (RAW + b"x", "source_changed"),
    ],
)
async def test_corruption_truncation_and_extra_bytes_fail_after_real_close(
    provider, raw, code
):
    provider.actual = raw
    with pytest.raises(ContentError, match=code):
        await capture()
    assert all(body.closed for body in provider.bodies) and provider.closed == 1
    assert service._slots._value == 4


@pytest.mark.parametrize("value", [bytearray(RAW), "text", None])
async def test_body_must_return_bounded_bytes(provider, value):
    class Invalid(Body):
        def read(self, count):
            return value

    provider.body_factory = lambda: Invalid(provider, RAW)
    with pytest.raises(ContentError, match="source_changed"):
        await capture()
    assert provider.bodies[0].closed and provider.closed == 1


async def test_oversized_chunk_rejected_even_when_total_would_fit(
    provider, monkeypatch
):
    monkeypatch.setattr(service, "CHUNK_BYTES", 2)

    class Invalid(Body):
        def read(self, count):
            return b"x" * (count + 1)

    provider.body_factory = lambda: Invalid(provider, RAW)
    with pytest.raises(ContentError, match="source_changed"):
        await capture()
    assert provider.bodies[0].closed


async def test_ignored_conditions_close_probe_and_never_fetch_success(provider):
    provider.ignore_conditions = True
    with pytest.raises(ContentError, match="content_conditions_unsupported"):
        await capture()
    assert len(provider.bodies) == 1 and provider.bodies[0].closed
    assert len([call for call in provider.calls if call[0] == "get"]) == 1
    assert provider.closed == 1 and service._slots._value == 4


async def test_conditions_need_consistent_actual_412_status(provider, monkeypatch):
    def denied(**params):
        raise error("PreconditionFailed", 403)

    monkeypatch.setattr(provider, "get_object", denied)
    with pytest.raises(ContentError, match="content_conditions_unverified"):
        await capture()
    assert provider.closed == 1


async def test_replacement_during_body_close_is_seen_by_current_final_head(provider):
    provider.version = "target-version"

    def replace():
        provider.version = "new-current-version"

    provider.body_close_hook = replace
    with pytest.raises(ContentError, match="source_changed"):
        await capture()
    assert [action for action, _ in provider.calls][-3:] == [
        "body_close",
        "head",
        "client_close",
    ]
    assert "VersionId" not in provider.calls[-2][1]


@pytest.mark.parametrize("where", ["head", "get"])
@pytest.mark.parametrize(
    "failure,status,code",
    [
        (error("NoSuchKey", 404), 404, "content_source_missing"),
        (error("PreconditionFailed", 412), 409, "source_changed"),
        (error("AccessDenied", 403), 503, "content_source_unavailable"),
    ],
)
async def test_known_provider_failures_have_fixed_sanitized_classification(
    provider, where, failure, status, code
):
    setattr(provider, where + "_error", failure)
    with pytest.raises(ContentError, match=code) as caught:
        await capture()
    assert caught.value.status == status and "private" not in str(caught.value)
    assert provider.closed == 1 and not getattr(
        caught.value, "storage_operation_unsettled", False
    )


@pytest.mark.parametrize("where", ["body", "client"])
@pytest.mark.parametrize(
    "failure", [OSError("private close detail"), error("NoSuchKey", 404)]
)
async def test_unknown_close_is_never_missing_or_success(provider, where, failure):
    setattr(provider, where + "_close_error", failure)
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        await capture()
    assert caught.value.status == 503 and caught.value.storage_operation_unsettled
    assert "private" not in str(caught.value)
    assert provider.closed == 1 and service._slots._value == 4


async def test_unknown_client_constructor_keeps_uncertainty(provider, monkeypatch):
    def unknown(**kwargs):
        raise OSError("private constructor detail")

    monkeypatch.setattr(service, "create_client", unknown)
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        await capture()
    assert caught.value.storage_operation_unsettled and service._slots._value == 4


async def test_missing_client_constructor_result_cannot_claim_settlement(
    provider, monkeypatch
):
    monkeypatch.setattr(service, "create_client", lambda **_: None)
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        await capture()
    assert caught.value.storage_operation_unsettled and service._slots._value == 4


async def test_malformed_stream_result_keeps_unknown_resource_responsibility(provider):
    provider.get_changes = {"Body": None}
    try:
        with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
            await capture()
        assert caught.value.storage_operation_unsettled and provider.closed == 1
    finally:
        # The malformed fake hid a real stream from its caller. This cleanup is
        # test-owned; production correctly retains its durable unknown permit.
        for body in provider.bodies:
            body.close()


async def test_ignored_precondition_with_unknown_probe_close_is_unsettled(provider):
    provider.ignore_conditions = True
    provider.body_close_error = error("NoSuchKey", 404)
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        await capture()
    assert caught.value.storage_operation_unsettled and provider.closed == 1


async def test_stage_holds_binary_capacity_until_closed(provider, monkeypatch):
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))
    first = await capture()
    try:
        with pytest.raises(ContentError, match="snapshot_capacity_exceeded") as caught:
            await capture()
        assert caught.value.status == 429 and provider.closed == 1
    finally:
        first.close()
    assert service._slots._value == 1


@pytest.mark.parametrize("phase", ["read", "client_close"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_interruption_drains_thread_before_return_and_releases_own_slot(
    provider, monkeypatch, phase, finish
):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))

    def blocked(*args):
        entered.set()
        assert release.wait(3)

    setattr(provider, phase + "_hook", blocked)
    operation = asyncio.create_task(
        capture(timeout_seconds=0.05 if finish == "timeout" else 3)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if finish == "cancel":
            operation.cancel()
        await asyncio.sleep(0.08)
        assert not operation.done() and service._slots._value == 0
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert provider.closed == 1 and all(body.closed for body in provider.bodies)
    assert service._slots._value == 1


async def test_timeout_is_not_reported_as_success(provider):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(3)

    provider.read_hook = blocked
    operation = asyncio.create_task(capture(timeout_seconds=0.02))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.04)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(ContentError, match="content_read_timeout") as caught:
        await operation
    assert caught.value.status == 504 and provider.closed == 1


async def test_cancelled_late_unknown_close_preserves_unsettled(provider):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(3)

    provider.read_hook = blocked
    provider.body_close_error = OSError("private late close")
    operation = asyncio.create_task(capture())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await operation
    assert caught.value.storage_operation_unsettled and service._slots._value == 4


async def test_late_success_is_closed_after_repeated_cancel(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    returned = []

    def late(spec, maximum, seconds, deadline, stop, release_slot):
        entered.set()
        assert release.wait(3)
        stage = service.BinaryStage(
            io.BytesIO(RAW), spec.sha256, len(RAW), {}, _release=release_slot
        )
        returned.append(stage)
        return stage

    monkeypatch.setattr(service, "_capture", late)
    operation = asyncio.create_task(capture())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert len(returned) == 1 and returned[0].file.closed and service._slots._value == 4


async def test_nonimage_binary_above_image_limit_streams_to_file(provider):
    length, block = 65 * 1024**2, b"x" * service.CHUNK_BYTES
    digest = hashlib.sha256()
    for _ in range(length // len(block)):
        digest.update(block)

    class Repeated(Body):
        def __init__(self):
            super().__init__(provider, b"")
            self.remaining = length

        def read(self, count):
            self.requests.append(count)
            value = block[: min(count, self.remaining)]
            self.remaining -= len(value)
            return value

    provider.head_changes = provider.get_changes = {"ContentLength": length}
    provider.body_factory = Repeated
    stage = await capture(
        spec(size_bytes=length, sha256=digest.hexdigest()),
        max_bytes=length,
        timeout_seconds=10,
    )
    try:
        assert stage.size == length and stage.file.seek(0, 2) == length
        assert max(provider.bodies[0].requests) <= service.CHUNK_BYTES
        assert provider.bodies[0].remaining == 0 and provider.bodies[0].closed
    finally:
        stage.close()
