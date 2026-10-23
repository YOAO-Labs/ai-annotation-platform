"""Exact-byte observations never become permission or hide physical uncertainty."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace

from botocore.exceptions import ClientError, ReadTimeoutError
import pytest

from app.services import managed_original_probe as probe
from app.services.filesystem_storage import FilesystemStorageError
from app.services.managed_original_retirement import managed_storage_identity


def provider_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "private provider detail"}}, "GetObject"
    )


class Body(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.close_count = 0
        self.read_hook = None
        self.read_error = None
        self.close_error = None
        self.requested_chunks = []

    def read(self, size):
        self.requested_chunks.append(size)
        if self.read_error:
            raise self.read_error
        value = super().read(size)
        if self.read_hook:
            self.read_hook(value)
        return value

    def close(self):
        self.close_count += 1
        super().close()
        if self.close_error:
            raise self.close_error


class MemoryS3:
    def __init__(self):
        self.data = b"generic original\x00bytes"
        self.headers = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(self.data),
            "ETag": '"opaque-not-a-checksum"',
            "LastModified": datetime(2026, 10, 24, tzinfo=timezone.utc),
            "VersionId": None,
        }
        self.calls, self.factory_calls, self.events = [], [], []
        self.meta = SimpleNamespace(
            endpoint_url="http://owned.test:9000",
            events=SimpleNamespace(register=lambda *args: self.events.append(args)),
        )
        self.body = Body(self.data)
        self.condition_body = Body(self.data)
        self.condition_ignored = False
        self.head_error = None
        self.get_error = None
        self.condition_error = None
        self.read_headers = {}
        self.final_headers = {}
        self.close_error = None
        self.close_hook = None
        self.close_count = 0
        self.head_count = 0

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        self.head_count += 1
        if self.head_error:
            raise self.head_error
        return {
            **self.headers,
            **(self.final_headers if self.head_count > 1 else {}),
        }

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if kwargs["IfMatch"] == '"aap-impossible-etag"':
            if self.condition_error:
                raise self.condition_error
            if not self.condition_ignored:
                raise provider_error("PreconditionFailed")
            return {**self.headers, "Body": self.condition_body}
        if self.get_error:
            raise self.get_error
        return {**self.headers, **self.read_headers, "Body": self.body}

    def close(self):
        self.close_count += 1
        if self.close_hook:
            self.close_hook()
        if self.close_error:
            raise self.close_error


@pytest.fixture
def configured(monkeypatch):
    client = MemoryS3()
    monkeypatch.setattr(probe.settings, "minio_endpoint", "owned.test:9000")
    monkeypatch.setattr(probe.settings, "minio_use_ssl", False)

    def factory(*args, **kwargs):
        client.factory_calls.append((args, kwargs))
        return client

    monkeypatch.setattr(probe.boto3, "client", factory)
    candidate = probe.ProbeInput(
        bucket="owned-originals",
        object_key="exact/a//../图片 +%2F.bin",
        storage_identity=managed_storage_identity(
            "http://owned.test:9000", "owned-originals"
        ),
        sha256=hashlib.sha256(client.data).hexdigest(),
        size_bytes=len(client.data),
    )
    return client, candidate


def test_full_generic_read_is_exact_bounded_and_not_decoded(configured):
    client, candidate = configured
    result = probe.run_probe(candidate)
    assert result.outcome == "matched"
    assert result.error_code is None
    assert result.observed_sha256 == candidate.sha256
    assert result.observed_size == len(client.data)
    assert (
        result.observation["initial"]
        == result.observation["read"]
        == result.observation["final"]
    )
    assert set(client.body.requested_chunks) == {probe.CHUNK_BYTES}
    assert client.body.closed and client.body.close_count == client.close_count == 1
    assert [kind for kind, _ in client.calls] == ["head", "get", "get", "head"]
    assert all(params["Key"] == candidate.object_key for _, params in client.calls)
    assert all(params["Bucket"] == candidate.bucket for _, params in client.calls)
    assert client.calls[1][1]["IfMatch"] == '"aap-impossible-etag"'
    assert client.calls[2][1]["IfMatch"] == client.headers["ETag"]
    assert "VersionId" not in client.calls[2][1]
    assert set(client.calls[-1][1]) == {"Bucket", "Key"}
    config = client.factory_calls[0][1]["config"]
    assert config.connect_timeout == config.read_timeout == 2
    assert config.retries["total_max_attempts"] == 1
    assert config.proxies == {} and config.s3 == {"addressing_style": "path"}
    with pytest.raises(FrozenInstanceError):
        candidate.size_bytes = 12


def test_zero_byte_original_is_a_complete_valid_observation(configured):
    client, candidate = configured
    client.headers["ContentLength"] = 0
    client.body = Body(b"")
    result = probe.run_probe(
        replace(candidate, sha256=hashlib.sha256(b"").hexdigest(), size_bytes=0)
    )
    assert result.outcome == "matched" and result.observed_size == 0
    assert result.observed_sha256 == hashlib.sha256(b"").hexdigest()
    assert client.body.closed and client.close_count == 1


def test_multiple_chunks_are_hashed_without_a_full_temporary_copy(configured):
    client, candidate = configured
    content = b"large generic bytes" * 60_000
    client.headers["ContentLength"] = len(content)
    client.body = Body(content)
    result = probe.run_probe(
        replace(
            candidate,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
    )
    assert result.outcome == "matched"
    assert len(client.body.requested_chunks) > 4
    assert set(client.body.requested_chunks) == {probe.CHUNK_BYTES}
    assert client.body.closed and client.close_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("bucket", ""),
        ("bucket", "bad\nheader"),
        ("object_key", ""),
        ("object_key", "null\x00key"),
        ("object_key", "__aap_reference__/opaque"),
        ("object_key", "图" * 342),
        ("storage_identity", None),
        ("sha256", "unknown"),
        ("sha256", "A" * 64),
        ("size_bytes", None),
        ("size_bytes", True),
        ("size_bytes", -1),
        ("size_bytes", probe.MAX_BYTES + 1),
    ],
)
def test_invalid_or_over_budget_inputs_do_not_construct_client(
    configured, field, value
):
    client, candidate = configured
    result = probe.run_probe(replace(candidate, **{field: value}))
    assert result.outcome == "unverifiable" and result.error_code
    assert client.calls == client.factory_calls == []


def test_changed_storage_identity_is_rejected_before_client_construction(configured):
    client, candidate = configured
    result = probe.run_probe(replace(candidate, storage_identity="0" * 64))
    assert (result.outcome, result.error_code) == (
        "unverifiable",
        "managed_original_storage_configuration_changed",
    )
    assert not client.calls and not client.factory_calls


def test_actual_client_endpoint_is_checked_before_any_request(configured):
    client, candidate = configured
    client.meta.endpoint_url = "http://different.invalid:9000"
    assert probe.run_probe(candidate).outcome == "unverifiable"
    assert not client.calls and client.close_count == 1


@pytest.mark.parametrize(
    "code,outcome,expected",
    [
        ("NoSuchKey", "missing", "managed_original_object_missing"),
        ("NoSuchVersion", "missing", "managed_original_object_missing"),
        ("404", "missing", "managed_original_object_missing"),
        ("NotFound", "missing", "managed_original_object_missing"),
        ("AccessDenied", "failed", "managed_original_access_denied"),
        ("403", "failed", "managed_original_access_denied"),
        ("PreconditionFailed", "changed", "managed_original_object_changed"),
        ("412", "changed", "managed_original_object_changed"),
        ("InternalError", "failed", "managed_original_source_unavailable"),
        ("UnknownMissing", "failed", "managed_original_source_unavailable"),
    ],
)
@pytest.mark.parametrize("stage", ["head", "get"])
def test_provider_errors_have_fixed_safe_outcomes(
    configured, code, outcome, expected, stage
):
    client, candidate = configured
    setattr(client, stage + "_error", provider_error(code))
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (outcome, expected)
    assert "private" not in str(result)
    assert client.close_count == 1


def test_forbidden_response_is_not_called_missing_even_with_a_conflicting_code(
    configured,
):
    client, candidate = configured
    client.head_error = ClientError(
        {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "HeadObject",
    )
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "failed",
        "managed_original_access_denied",
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"ResponseMetadata": {"HTTPStatusCode": 206}},
        {"ContentRange": "bytes 0-1/2"},
        {"DeleteMarker": True},
        {"ContentLength": True},
        {"ETag": ""},
        {"ETag": "\r\nnot-safe"},
        {"LastModified": datetime(2026, 10, 24)},
        {"VersionId": ["wrong shape"]},
    ],
)
@pytest.mark.parametrize("stage", ["head", "get"])
def test_invalid_or_partial_responses_never_match_and_close_handles(
    configured, headers, stage
):
    client, candidate = configured
    (client.headers if stage == "head" else client.read_headers).update(headers)
    result = probe.run_probe(candidate)
    assert result.outcome == "unverifiable"
    assert result.observed_sha256 is None
    assert client.close_count == 1
    if stage == "get":
        assert client.body.closed and client.body.close_count == 1


def test_ignored_condition_is_unverifiable_and_capability_body_is_closed(configured):
    client, candidate = configured
    client.condition_ignored = True
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "unverifiable",
        "managed_original_conditions_unsupported",
    )
    assert client.condition_body.closed and client.condition_body.close_count == 1
    assert len(client.calls) == 2 and client.close_count == 1


def test_version_is_pinned_for_get_but_final_head_observes_current_key(configured):
    client, candidate = configured
    client.headers["VersionId"] = "old-version"
    client.final_headers["VersionId"] = "new-current-version"
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "changed",
        "managed_original_object_changed",
    )
    assert result.observed_sha256 == candidate.sha256
    assert all(
        params["VersionId"] == "old-version"
        for kind, params in client.calls
        if kind == "get"
    )
    assert all(
        set(params) == {"Bucket", "Key"}
        for kind, params in client.calls
        if kind == "head"
    )
    assert client.body.closed and client.close_count == 1


@pytest.mark.parametrize("version", [None, "null", "stable-version"])
def test_stable_versioned_and_unversioned_content_matches(configured, version):
    client, candidate = configured
    client.headers["VersionId"] = version
    assert probe.run_probe(candidate).outcome == "matched"
    if version not in {None, "null"}:
        assert client.calls[2][1]["VersionId"] == version
    else:
        assert "VersionId" not in client.calls[2][1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("ETag", '"new-current-object"'),
        ("ContentLength", 123),
        ("LastModified", datetime(2026, 10, 25, tzinfo=timezone.utc)),
    ],
)
def test_current_metadata_change_after_complete_read_discards_match(
    configured, field, value
):
    client, candidate = configured
    client.final_headers[field] = value
    result = probe.run_probe(candidate)
    assert result.outcome == "changed" and result.observed_sha256 == candidate.sha256


def test_same_size_different_bytes_are_detected_despite_equal_opaque_etag(configured):
    client, candidate = configured
    client.body = Body(b"x" * candidate.size_bytes)
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "changed",
        "managed_original_sha256_mismatch",
    )
    assert result.observed_size == candidate.size_bytes
    assert result.observed_sha256 != candidate.sha256
    assert client.body.closed and client.close_count == 1


@pytest.mark.parametrize("size", [0, 1, 100])
def test_short_and_overlong_streams_never_match_the_announced_size(configured, size):
    client, candidate = configured
    client.body = Body(b"x" * size)
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "changed",
        "managed_original_size_mismatch",
    )
    assert client.body.closed and client.close_count == 1


def test_different_head_size_rejects_before_get(configured):
    client, candidate = configured
    client.headers["ContentLength"] = candidate.size_bytes + 1
    result = probe.run_probe(candidate)
    assert result.outcome == "changed" and result.observed_sha256 is None
    assert [kind for kind, _ in client.calls] == ["head"]


def test_stream_cannot_exceed_the_fixed_byte_budget(configured, monkeypatch):
    client, candidate = configured
    monkeypatch.setattr(probe, "MAX_BYTES", candidate.size_bytes)
    client.body = Body(client.data + b"x")
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "failed",
        "managed_original_byte_budget_exceeded",
    )
    assert client.body.closed and client.close_count == 1


@pytest.mark.parametrize("value", ["not bytes", b"x" * (probe.CHUNK_BYTES + 1)])
def test_invalid_read_shape_is_not_treated_as_verified_bytes(
    configured, monkeypatch, value
):
    client, candidate = configured
    monkeypatch.setattr(client.body, "read", lambda size: value)
    result = probe.run_probe(candidate)
    assert result.outcome == "unverifiable" and result.observed_sha256 is None
    assert client.body.closed and client.close_count == 1


@pytest.mark.parametrize("when", ["data", "eof", "close"])
def test_deadline_is_checked_after_blocking_read_and_after_closure(
    configured, monkeypatch, when
):
    client, candidate = configured
    clock = [0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def read_hook(value):
        if (when == "data" and value) or (when == "eof" and not value):
            clock[0] = probe.TIMEOUT_SECONDS + 1

    client.body.read_hook = read_hook
    if when == "close":
        client.close_hook = lambda: clock.__setitem__(0, probe.TIMEOUT_SECONDS + 1)
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "failed",
        "managed_original_probe_timeout",
    )
    assert client.body.closed and client.close_count == 1


def test_sdk_timeout_is_safe_and_closes_the_stream_and_client(configured):
    client, candidate = configured
    client.body.read_error = ReadTimeoutError(
        endpoint_url="http://private.invalid/secret"
    )
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "failed",
        "managed_original_probe_timeout",
    )
    assert "private" not in str(result)
    assert client.body.closed and client.close_count == 1


@pytest.mark.parametrize("resource", ["body", "client", "condition_body"])
def test_uncertain_close_propagates_instead_of_returning_a_settled_result(
    configured, resource
):
    client, candidate = configured
    target = client if resource == "client" else getattr(client, resource)
    target.close_error = RuntimeError("private close failure")
    if resource == "condition_body":
        client.condition_ignored = True
    with pytest.raises(RuntimeError) as failure:
        probe.run_probe(candidate)
    assert failure.value.storage_operation_unsettled is True
    assert client.close_count == 1


def test_constructor_uncertainty_is_not_hidden(configured, monkeypatch):
    client, candidate = configured

    def fail(*args, **kwargs):
        raise RuntimeError("constructor did not return its private handle")

    monkeypatch.setattr(probe.boto3, "client", fail)
    with pytest.raises(RuntimeError) as failure:
        probe.run_probe(candidate)
    assert failure.value.storage_operation_unsettled is True
    assert not client.calls and not client.close_count


def test_registered_redirect_guard_does_not_follow_a_different_origin(configured):
    client, candidate = configured
    assert probe.run_probe(candidate).outcome == "matched"
    name, guard = client.events[0]
    assert name == "before-send.s3"
    guard(SimpleNamespace(url="http://owned.test:9000/owned-originals/exact%2Fkey"))
    for url in [
        "http://other.invalid/key",
        "https://owned.test:9000/key",
        "http://owned.test:9001/key",
    ]:
        with pytest.raises(FilesystemStorageError, match="s3_redirect_denied"):
            guard(SimpleNamespace(url=url))


def test_only_fixed_error_code_is_returned_for_an_unexpected_provider_failure(
    configured,
):
    client, candidate = configured
    client.body.read_error = RuntimeError("private signed URL and credential detail")
    result = probe.run_probe(candidate)
    assert (result.outcome, result.error_code) == (
        "failed",
        "managed_original_probe_failed",
    )
    assert "private" not in str(result)
    assert client.body.closed and client.close_count == 1
