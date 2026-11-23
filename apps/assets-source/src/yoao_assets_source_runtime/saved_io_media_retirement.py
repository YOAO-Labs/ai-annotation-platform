"""Read and delete only the retired original's currently claimed version."""

import hashlib
import time

from botocore.exceptions import ClientError
from yoao_assets_source import managed_original as writer
from yoao_assets_source.saved_io import SavedIOError

from .saved_io_media import _FAMILY, _denied, _fields, _identity, _pin, _receipt, _uuid
from .saved_io_storage import physical_error

_SQL = "yoao_assets_v1.media_io_retired_original"
_MISSING = {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}


def _params(pin):
    return {"Bucket": pin.bucket, "Key": pin.key} | (
        {"VersionId": pin.version} if pin.version else {}
    )


def _read(client, pin, expected):
    params = _params(pin)
    size, digest = _receipt(expected)
    observation = {}
    outcome, error, observed_digest, observed_size = "matched", None, None, None
    limit = time.monotonic() + 60
    try:
        before = writer._observation(client.head_object(**params), pin.bucket, pin.key)
        observation["initial"] = before
        if before["size"] != size:
            return {
                "outcome": "changed",
                "error_code": "managed_original_size_mismatch",
                "observation": observation,
            }
        if pin.version is not None and before["version_id"] != pin.version:
            return {
                "outcome": "changed",
                "error_code": "reclaim_version_mismatch",
                "observation": observation,
            }
        conditions = {**params, "IfMatch": before["etag"]}
        if before["version_id"] is not None:
            conditions["VersionId"] = before["version_id"]
        writer._require_conditions(client, conditions)
        response = client.get_object(**conditions)
        body = response["Body"]
        try:
            current = writer._observation(response, pin.bucket, pin.key)
            observation["read"] = current
            if current != before:
                return {
                    "outcome": "changed",
                    "error_code": "managed_original_object_changed",
                    "observation": observation,
                }
            checksum, count = hashlib.sha256(), 0
            while True:
                if time.monotonic() >= limit:
                    return {
                        "outcome": "failed",
                        "error_code": "managed_original_probe_timeout",
                        "observation": observation,
                    }
                chunk = body.read(min(256 * 1024, size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > size:
                    return {
                        "outcome": "changed",
                        "error_code": "managed_original_size_mismatch",
                        "observation": observation,
                    }
                checksum.update(chunk)
            observed_digest, observed_size = checksum.hexdigest(), count
        finally:
            try:
                body.close()
            except Exception:
                raise SavedIOError(
                    "source_close_unsettled", status_code=503, unsettled=True
                ) from None
        after = writer._observation(client.head_object(**params), pin.bucket, pin.key)
        observation["final"] = after
        if after != before:
            outcome, error = "changed", "managed_original_object_changed"
        elif observed_size != size:
            outcome, error = "changed", "managed_original_size_mismatch"
        elif observed_digest != digest:
            outcome, error = "changed", "managed_original_sha256_mismatch"
    except ClientError as exc:
        failure = physical_error(exc)
        if failure.storage_code in _MISSING:
            outcome, error = "missing", "managed_original_object_missing"
        elif failure.status_code == 412:
            outcome, error = "changed", "managed_original_object_changed"
        elif failure.status_code == 403:
            outcome, error = "failed", "managed_original_access_denied"
        else:
            outcome, error = "failed", "managed_original_source_unavailable"
    except writer.WriterError as exc:
        if exc.outcome == "unknown":
            raise SavedIOError(exc.code, status_code=503, unsettled=True) from None
        outcome, error = (
            "unverifiable",
            (
                "managed_original_conditions_unsupported"
                if exc.code.startswith("creation_conditions_")
                else "managed_original_invalid_response"
            ),
        )
    return {
        "outcome": outcome,
        "error_code": error,
        "observation": observation,
        "observed_sha256": observed_digest,
        "observed_size": observed_size,
    }


def _delete(client, pin, expected):
    params = _params(pin)
    _identity(
        client.head_object(**params),
        expected["size"],
        etag=expected["etag"],
        version=pin.version,
    )
    try:
        response = client.delete_object(**params)
        meta = response.get("ResponseMetadata", {})
        request_id = meta.get("RequestId")
        if (
            meta.get("HTTPStatusCode") not in {200, 204}
            or response.get("DeleteMarker") not in (None, False)
            or response.get("VersionId") not in (None, pin.version)
            or request_id is not None
            and (
                type(request_id) is not str
                or not 0 < len(request_id) <= 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in request_id)
            )
        ):
            raise SavedIOError(
                "reclaim_invalid_delete_ack", status_code=503, unsettled=True
            )
    except Exception as exc:
        raise physical_error(exc, mutation=True) from None
    return {
        "version_id": pin.version,
        "request_id": request_id,
        "http_status": meta["HTTPStatusCode"],
        "delete_marker": False,
    }


def _handle(action):
    async def handle(context, subject, body=None):
        _fields(subject, "id", "permit")
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

        async def proof(claim=False):
            return await context.fetch(_SQL, identifier, permit, action, claim)

        expected = await proof()
        pin = await _pin(
            context, expected, version=expected.get("version") if expected else None
        )
        if action == "reclaim-absence":
            try:
                await pin.head()
            except SavedIOError as exc:
                if (
                    not exc.unsettled
                    and exc.storage_code in _MISSING
                    and await proof() == expected
                ):
                    return {"absent": True}
                raise
            raise SavedIOError("reclaim_version_still_present")
        if action == "reclaim-delete" and await proof(claim=True) != expected:
            raise _denied()
        try:
            async with pin.client() as client:
                result = await context.run(
                    _delete if action == "reclaim-delete" else _read,
                    client,
                    pin,
                    expected,
                )
        except Exception as exc:
            raise physical_error(exc) from None
        if action != "reclaim-delete" and await proof() != expected:
            raise _denied()
        return result

    return handle


def register(registry):
    registry.allow(_SQL + "(uuid,uuid,text,boolean)")
    registry.add(
        _FAMILY, "retired-original-verify", "command", _handle("verify"), access="read"
    )
    for action in ("reclaim-probe", "reclaim-delete", "reclaim-absence"):
        registry.add(
            _FAMILY,
            "retired-original-" + action,
            "command",
            _handle(action),
            access="cleanup",
        )
