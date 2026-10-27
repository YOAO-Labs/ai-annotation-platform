"""Bounded verification and permanent deletion of one immutable S3 version."""

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import re
import sys
import time
from typing import Literal

from botocore.exceptions import ClientError

from app.services import managed_original_probe as probe
from app.services.filesystem_storage import FilesystemStorageError as StorageError
from app.services.managed_original_retirement import managed_storage_identity
from app.services.sources import s3_reference
from app.services.storage_access import close_registered, mark_unsettled

MAX_BYTES = probe.MAX_BYTES
CHUNK_BYTES = probe.CHUNK_BYTES
TIMEOUT_SECONDS = probe.TIMEOUT_SECONDS
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


@dataclass(frozen=True, repr=False)
class ReclaimInput:
    bucket: str
    object_key: str
    version_id: str
    storage_identity: str
    sha256: str
    size_bytes: int
    endpoint_url: str | None = None
    recover_may_delete: bool = False


@dataclass(frozen=True)
class ReclaimResult:
    outcome: Literal[
        "deleted", "already_absent", "missing", "changed", "failed", "unknown"
    ]
    error_code: str | None = None
    delete_started: bool = False
    delete_ack: dict | None = None
    absence_proven: bool = False
    observed_sha256: str | None = None
    observed_size: int | None = None
    observation: dict = field(default_factory=dict)


class _Failure(Exception):
    def __init__(self, code, *, changed=False):
        self.code, self.changed = code, changed
        super().__init__(code)


def _missing(exc):
    if not isinstance(exc, ClientError):
        return False
    response = exc.response if isinstance(exc.response, dict) else {}
    error = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    return (
        isinstance(error, dict)
        and error.get("Code") in {"NoSuchVersion", "NoSuchKey", "NotFound", "404"}
        and isinstance(metadata, dict)
        and metadata.get("HTTPStatusCode") == 404
    )


def _error(exc):
    if isinstance(exc, _Failure):
        return exc.code
    detail = getattr(exc, "detail", None)
    code = (
        detail.get("code") if isinstance(detail, dict) else getattr(exc, "code", None)
    )
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return probe._classify(exc)[1]


def _version(response, candidate):
    observed = probe._observation(response, candidate)
    if observed["version_id"] != candidate.version_id:
        raise _Failure("reclaim_version_mismatch", changed=True)
    if response.get("DeleteMarker"):
        raise _Failure("reclaim_delete_marker_unsupported")
    return observed


def _safe_request_id(value):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise _Failure("reclaim_invalid_delete_ack")
    return value


def _ack(response, candidate):
    if not isinstance(response, dict):
        raise _Failure("reclaim_invalid_delete_ack")
    metadata = response.get("ResponseMetadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("HTTPStatusCode") not in {200, 204}
        or response.get("DeleteMarker") not in {None, False}
        or response.get("VersionId") not in {None, candidate.version_id}
    ):
        raise _Failure("reclaim_invalid_delete_ack")
    return {
        "version_id": candidate.version_id,
        "request_id": _safe_request_id(metadata.get("RequestId")),
        "http_status": metadata["HTTPStatusCode"],
        "delete_marker": False,
    }


def run_reclaim(
    candidate: ReclaimInput,
    *,
    checkpoint: Callable[[str, dict], None],
    client_context=None,
) -> ReclaimResult:
    """The caller owns authorization, reference fencing and durable checkpoints.

    The delete_started callback must commit the durable intent and recheck current
    authority/protection before returning. Later callbacks preserve physical facts
    even if authority or cancellation changes. A callback failure never licenses a
    second delete, and an uncertain close retains the caller's actual permit.
    """
    observed_sha256, observed_size = None, None
    delete_started, absence_proven, delete_ack = False, False, None
    observation = {}
    try:
        if not isinstance(candidate, ReclaimInput):
            raise _Failure("reclaim_invalid_input")
        probe._validate(
            probe.ProbeInput(
                bucket=candidate.bucket,
                object_key=candidate.object_key,
                storage_identity=candidate.storage_identity,
                sha256=candidate.sha256,
                size_bytes=candidate.size_bytes,
                endpoint_url=candidate.endpoint_url,
            )
        )
        if (
            not isinstance(candidate.version_id, str)
            or not candidate.version_id
            or candidate.version_id == "null"
            or len(candidate.version_id) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in candidate.version_id)
            or type(candidate.recover_may_delete) is not bool
            or not callable(checkpoint)
        ):
            raise _Failure("reclaim_exact_version_required")
        endpoint = probe._endpoint(candidate.endpoint_url)
        if (
            managed_storage_identity(endpoint, candidate.bucket)
            != candidate.storage_identity
        ):
            raise _Failure("reclaim_storage_configuration_changed")
        deadline = time.monotonic() + TIMEOUT_SECONDS

        def check_deadline():
            if time.monotonic() >= deadline:
                raise _Failure("reclaim_io_timeout")

        context = client_context() if client_context is not None else None
        client = context.__enter__() if context is not None else probe._client(endpoint)
        try:
            if client is None:
                raise mark_unsettled(StorageError("connection_unavailable"))
            if client.meta.endpoint_url.rstrip("/") != endpoint:
                raise _Failure("reclaim_storage_configuration_changed")
            client.meta.events.register("before-send.s3", probe._guard(endpoint))
            params = {
                "Bucket": candidate.bucket,
                "Key": candidate.object_key,
                "VersionId": candidate.version_id,
            }
            check_deadline()
            try:
                response = client.head_object(**params)
            except ClientError as exc:
                if not _missing(exc):
                    raise
                if not candidate.recover_may_delete:
                    return ReclaimResult("missing", "reclaim_version_missing")
                absence_proven = True
                checkpoint(
                    "version_absent",
                    {"version_id": candidate.version_id, "recovered": True},
                )
                return ReclaimResult("already_absent", absence_proven=True)
            check_deadline()
            before = _version(response, candidate)
            observation["initial"] = before
            if before["size"] != candidate.size_bytes:
                raise _Failure("reclaim_size_mismatch", changed=True)
            conditions = {**params, "IfMatch": before["etag"]}
            s3_reference.require_conditions(client, conditions)
            check_deadline()
            response = client.get_object(**conditions)
            body = response.get("Body")
            try:
                check_deadline()
                current = _version(response, candidate)
                observation["read"] = current
                if current != before:
                    raise _Failure("reclaim_version_changed", changed=True)
                if body is None or not callable(getattr(body, "read", None)):
                    raise _Failure("reclaim_invalid_read_response")
                digest, size = hashlib.sha256(), 0
                while True:
                    check_deadline()
                    chunk = body.read(CHUNK_BYTES)
                    check_deadline()
                    if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                        raise _Failure("reclaim_invalid_read_response")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES or size > candidate.size_bytes:
                        raise _Failure("reclaim_size_mismatch", changed=True)
                    digest.update(chunk)
                observed_sha256, observed_size = digest.hexdigest(), size
                if size != candidate.size_bytes:
                    raise _Failure("reclaim_size_mismatch", changed=True)
                if observed_sha256 != candidate.sha256:
                    raise _Failure("reclaim_sha256_mismatch", changed=True)
            finally:
                if body is not None:
                    close_registered(body)
            check_deadline()
            verified = {
                "version_id": candidate.version_id,
                "sha256": observed_sha256,
                "size_bytes": observed_size,
                "etag": before["etag"],
            }
            checkpoint("read_verified", verified)
            check_deadline()
            # From this callback onward the durable owner conservatively treats
            # DELETE as possibly issued, including a failure before SDK returns.
            checkpoint("delete_started", verified)
            delete_started = True
            check_deadline()
            delete_ack = _ack(client.delete_object(**params), candidate)
            checkpoint("delete_ack", delete_ack)
            check_deadline()
            try:
                client.head_object(**params)
            except ClientError as exc:
                if not _missing(exc):
                    raise
                absence_proven = True
            else:
                raise _Failure("reclaim_version_still_present")
            check_deadline()
            checkpoint(
                "version_absent",
                {"version_id": candidate.version_id, "recovered": False},
            )
        finally:
            try:
                if context is not None:
                    context.__exit__(*sys.exc_info())
                else:
                    close_registered(client)
            except BaseException as exc:
                raise mark_unsettled(exc)
        return ReclaimResult(
            "deleted",
            delete_started=True,
            delete_ack=delete_ack,
            absence_proven=True,
            observed_sha256=observed_sha256,
            observed_size=observed_size,
            observation=observation,
        )
    except BaseException as exc:
        if getattr(exc, "storage_operation_unsettled", False) or not isinstance(
            exc, Exception
        ):
            raise
        code = _error(exc)
        changed = isinstance(exc, _Failure) and exc.changed
        return ReclaimResult(
            "unknown" if delete_started else "changed" if changed else "failed",
            code,
            delete_started,
            delete_ack,
            absence_proven,
            observed_sha256,
            observed_size,
            observation,
        )
