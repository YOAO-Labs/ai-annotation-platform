"""Exact-version deletion never infers SHA ownership from an ETag or key."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace

from botocore.exceptions import ClientError, ReadTimeoutError
from fastapi import HTTPException
import pytest

from app.services import managed_original_reclaim_io as reclaim
from app.services.managed_original_retirement import managed_storage_identity


def error(code, status=404):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private provider details"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


class Body(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.requests = []
        self.close_error = None

    def read(self, size):
        self.requests.append(size)
        return super().read(size)

    def close(self):
        super().close()
        if self.close_error:
            raise self.close_error


class Versions:
    def __init__(self):
        self.data = b"retired generic bytes\x00"
        self.versions = {"retired-v1": self.data, "later-v2": b"unrelated replacement"}
        self.calls, self.phases = [], []
        self.headers = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(self.data),
            "ETag": '"opaque"',
            "LastModified": datetime(2026, 10, 26, tzinfo=timezone.utc),
            "VersionId": "retired-v1",
        }
        self.meta = SimpleNamespace(
            endpoint_url="http://owned.test:9000",
            events=SimpleNamespace(register=lambda *args: None),
        )
        self.body = Body(self.data)
        self.ignored_body = Body(self.data)
        self.before_error = self.after_error = self.get_error = None
        self.delete_error = self.close_error = None
        self.get_headers = {}
        self.ignored_condition = False
        self.delete_ignored = False
        self.ack = {
            "ResponseMetadata": {"HTTPStatusCode": 204, "RequestId": "ack-id"},
            "VersionId": "retired-v1",
        }
        self.closed = False

    def head_object(self, **params):
        self.calls.append(("head", params))
        if self.before_error and not self.phases:
            raise self.before_error
        if self.after_error and "delete_ack" in self.phases:
            raise self.after_error
        if params["VersionId"] not in self.versions:
            raise error("NoSuchVersion")
        return dict(self.headers)

    def get_object(self, **params):
        self.calls.append(("get", params))
        if params.get("IfMatch") == '"aap-impossible-etag"':
            if self.ignored_condition:
                return {**self.headers, "Body": self.ignored_body}
            raise error("PreconditionFailed", 412)
        if self.get_error:
            raise self.get_error
        return {**self.headers, **self.get_headers, "Body": self.body}

    def delete_object(self, **params):
        self.calls.append(("delete", params))
        if not self.delete_ignored:
            self.versions.pop(params["VersionId"], None)
        if self.delete_error:
            raise self.delete_error
        return self.ack

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


@pytest.fixture
def configured(monkeypatch):
    client = Versions()
    monkeypatch.setattr(reclaim.probe, "_client", lambda endpoint: client)
    candidate = reclaim.ReclaimInput(
        bucket="owned-originals",
        object_key="exact/图片 +%2F.bin",
        version_id="retired-v1",
        storage_identity=managed_storage_identity(
            client.meta.endpoint_url, "owned-originals"
        ),
        sha256=hashlib.sha256(client.data).hexdigest(),
        size_bytes=len(client.data),
        endpoint_url=client.meta.endpoint_url,
    )

    def checkpoint(phase, payload):
        assert payload["version_id"] == candidate.version_id
        if phase == "delete_started":
            assert not any(kind == "delete" for kind, _ in client.calls)
        if phase == "delete_ack":
            assert any(kind == "delete" for kind, _ in client.calls)
        client.phases.append(phase)

    return client, candidate, checkpoint


def test_exact_immutable_version_verified_deleted_and_later_version_preserved(
    configured,
):
    client, candidate, checkpoint = configured
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert (
        result.outcome == "deleted" and result.absence_proven and result.delete_started
    )
    assert result.observed_sha256 == candidate.sha256
    assert result.observed_size == len(client.data)
    assert result.delete_ack["request_id"] == "ack-id"
    assert client.phases == [
        "read_verified",
        "delete_started",
        "delete_ack",
        "version_absent",
    ]
    assert client.versions == {"later-v2": b"unrelated replacement"}
    assert [kind for kind, _ in client.calls] == [
        "head",
        "get",
        "get",
        "delete",
        "head",
    ]
    assert all(
        p["Bucket"] == candidate.bucket
        and p["Key"] == candidate.object_key
        and p["VersionId"] == candidate.version_id
        for _, p in client.calls
    )
    assert set(client.calls[3][1]) == {"Bucket", "Key", "VersionId"}
    assert set(client.body.requests) == {reclaim.CHUNK_BYTES}
    assert client.body.closed and client.closed


@pytest.mark.parametrize(
    "version", [None, "", "null", "bad\rversion", "bad\x00version", "x" * 1025]
)
def test_no_key_only_or_mutable_null_version_delete(configured, version):
    client, candidate, checkpoint = configured
    result = reclaim.run_reclaim(
        replace(candidate, version_id=version), checkpoint=checkpoint
    )
    assert (
        result.outcome == "failed"
        and result.error_code == "reclaim_exact_version_required"
    )
    assert client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "x" * 64},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"size_bytes": reclaim.MAX_BYTES + 1},
        {"bucket": ""},
        {"object_key": ""},
        {"storage_identity": "a" * 64},
        {"endpoint_url": "http://user:private@owned.test:9000"},
        {"recover_may_delete": 1},
    ],
)
def test_invalid_identity_budget_and_shape_do_no_io(configured, changes):
    client, candidate, checkpoint = configured
    result = reclaim.run_reclaim(replace(candidate, **changes), checkpoint=checkpoint)
    assert result.outcome == "failed" and client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"VersionId": "later-v2"},
        {"VersionId": None},
        {"ContentLength": 1},
        {"ETag": ""},
        {"LastModified": None},
        {"DeleteMarker": True},
        {"ContentRange": "bytes 0-1/21"},
    ],
)
def test_inaccurate_head_is_not_delete_permission(configured, changes):
    client, candidate, checkpoint = configured
    client.headers.update(changes)
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome in {"failed", "changed"}
    assert not result.delete_started and not any(k == "delete" for k, _ in client.calls)
    assert client.closed


@pytest.mark.parametrize(
    "changes",
    [
        {"VersionId": "later-v2"},
        {"VersionId": None},
        {"ContentLength": 1},
        {"ETag": '"different"'},
    ],
)
def test_inaccurate_get_is_not_delete_permission(configured, changes):
    client, candidate, checkpoint = configured
    client.get_headers.update(changes)
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome in {"failed", "changed"} and not result.delete_started
    assert client.body.closed and client.closed
    assert not any(k == "delete" for k, _ in client.calls)


@pytest.mark.parametrize("data", [b"same-size-wrong-data!!", b"short", b"x" * 25])
def test_full_sha_and_size_checked_not_etag(configured, data):
    client, candidate, checkpoint = configured
    client.body = Body(data)
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome == "changed" and not result.delete_started
    assert client.body.closed and client.closed
    assert not any(k == "delete" for k, _ in client.calls)


def test_condition_ignored_is_rejected_and_body_closed(configured):
    client, candidate, checkpoint = configured
    client.ignored_condition = True
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome == "failed" and not result.delete_started
    assert client.ignored_body.closed and client.closed


@pytest.mark.parametrize("phase", ["read_verified", "delete_started"])
def test_reference_or_cancel_recheck_rejection_prevents_delete(configured, phase):
    client, candidate, checkpoint = configured

    def rejected(current, payload):
        checkpoint(current, payload)
        if current == phase:
            raise HTTPException(409, detail={"code": "reclaim_reference_changed"})

    result = reclaim.run_reclaim(candidate, checkpoint=rejected)
    assert (
        result.outcome == "failed" and result.error_code == "reclaim_reference_changed"
    )
    assert not any(k == "delete" for k, _ in client.calls)
    assert client.closed


@pytest.mark.parametrize("phase", ["delete_ack", "version_absent"])
def test_late_checkpoint_failure_retains_actual_ack_and_tombstone_requirement(
    configured, phase
):
    client, candidate, checkpoint = configured

    def rejected(current, payload):
        checkpoint(current, payload)
        if current == phase:
            raise RuntimeError("private database failure")

    result = reclaim.run_reclaim(candidate, checkpoint=rejected)
    assert result.outcome == "unknown" and result.delete_started
    assert result.delete_ack["request_id"] == "ack-id"
    assert "retired-v1" not in client.versions and client.closed
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "code,status",
    [("AccessDenied", 403), ("NoSuchBucket", 404), ("InternalError", 500)],
)
def test_provider_failure_does_not_mean_version_absent(configured, code, status):
    client, candidate, checkpoint = configured
    client.before_error = error(code, status)
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome == "failed" and not result.absence_proven
    assert not result.delete_started and client.closed


@pytest.mark.parametrize("recover", [False, True])
def test_missing_counts_as_recovery_only_after_durable_prior_intent(
    configured, recover
):
    client, candidate, checkpoint = configured
    client.versions.pop("retired-v1")
    result = reclaim.run_reclaim(
        replace(candidate, recover_may_delete=recover), checkpoint=checkpoint
    )
    assert result.outcome == ("already_absent" if recover else "missing")
    assert result.absence_proven is recover
    assert not result.delete_started and client.closed
    assert client.phases == (["version_absent"] if recover else [])


@pytest.mark.parametrize("mode", ["ignored", "lost_ack", "after_403", "bad_ack"])
def test_issued_delete_without_verified_absence_is_unknown(configured, mode):
    client, candidate, checkpoint = configured
    if mode == "ignored":
        client.delete_ignored = True
    elif mode == "lost_ack":
        client.delete_error = ReadTimeoutError(endpoint_url=client.meta.endpoint_url)
    elif mode == "after_403":
        client.after_error = error("AccessDenied", 403)
    else:
        client.ack["VersionId"] = "another-version"
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome == "unknown" and result.delete_started
    assert not result.absence_proven and client.closed
    assert sum(k == "delete" for k, _ in client.calls) == 1


@pytest.mark.parametrize("handle", ["body", "client"])
def test_uncertain_close_retains_actual_operation_responsibility(configured, handle):
    client, candidate, checkpoint = configured
    if handle == "body":
        client.body.close_error = OSError("private close error")
    else:
        client.close_error = OSError("private close error")
    with pytest.raises(BaseException) as raised:
        reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert raised.value.storage_operation_unsettled is True
    assert client.closed


def test_undeletable_zero_byte_version_remains_valid_exact_object(configured):
    client, candidate, checkpoint = configured
    client.body = Body(b"")
    client.headers["ContentLength"] = 0
    candidate = replace(candidate, sha256=hashlib.sha256(b"").hexdigest(), size_bytes=0)
    result = reclaim.run_reclaim(candidate, checkpoint=checkpoint)
    assert result.outcome == "deleted" and result.observed_size == 0


def test_budget_expiry_after_intent_never_issues_an_unbounded_delete(
    configured, monkeypatch
):
    client, candidate, checkpoint = configured
    ticks = [0.0]
    monkeypatch.setattr(reclaim.time, "monotonic", lambda: ticks[0])

    def expires(current, payload):
        checkpoint(current, payload)
        if current == "delete_started":
            ticks[0] = reclaim.TIMEOUT_SECONDS

    result = reclaim.run_reclaim(candidate, checkpoint=expires)
    assert result.outcome == "unknown" and result.delete_started
    assert result.error_code == "reclaim_io_timeout"
    assert not any(k == "delete" for k, _ in client.calls) and client.closed
