"""Conditional original creation with bounded bytes and explicit uncertainty.

The caller owns its one-attempt SDK client, durable allocation and maintenance
permit. These functions never delete a final object or infer creation from an
existing object's digest. A returned write receipt must be persisted before
verification and business publication.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
import time

from botocore.exceptions import ClientError

from app.services.source_readers import s3 as s3_reference

CHUNK_BYTES = 256 * 1024
PART_BYTES = 8 * 1024 * 1024
HEAD_BYTES = 256 * 1024
MAX_COPY_BYTES = 5 * 1024**3
MAX_PART_BYTES = 5 * 1024**3
MAX_PARTS = 10000
TIMEOUT_SECONDS = 60


@dataclass(frozen=True, repr=False)
class Prepared:
    sha256: str
    md5: str
    size_bytes: int
    head_bytes: bytes
    upload_id: str | None
    parts: tuple[dict, ...]
    bucket: str
    object_key: str


@dataclass(frozen=True, repr=False)
class Source:
    sha256: str
    md5: str
    size_bytes: int
    head_bytes: bytes
    etag: str
    version_id: str | None
    observation: dict


@dataclass(frozen=True, repr=False)
class WriteReceipt:
    etag: str
    version_id: str | None
    request_id: str | None
    response: dict


@dataclass(frozen=True, repr=False)
class Verified:
    sha256: str
    md5: str
    size_bytes: int
    head_bytes: bytes
    observation: dict


class WriterError(Exception):
    def __init__(self, code, outcome="failed"):
        self.code, self.outcome = code, outcome
        if outcome == "unknown":
            self.storage_operation_unsettled = True
        super().__init__(code)


def _safe(value, *, maximum=1024):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= maximum and not any(
            ord(c) < 32 or ord(c) == 127 for c in value
        )
    except UnicodeError:
        return False


def _locator(bucket, key):
    if not _safe(bucket, maximum=255) or not _safe(key):
        raise WriterError("creation_invalid_input")
    from app.services.reference_keys import is_reference

    if is_reference(key):
        raise WriterError("creation_invalid_input")


def _budget(maximum):
    if type(maximum) is not int or maximum < 0:
        raise WriterError("creation_invalid_budget")


def _content_type(value):
    if not _safe(value):
        raise WriterError("creation_invalid_content_type")
    return value


def _clock(seconds):
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not (0 < seconds <= 86400)
    ):
        raise WriterError("creation_invalid_budget")
    deadline = time.monotonic() + seconds

    def check():
        if time.monotonic() >= deadline:
            raise WriterError("creation_timeout")

    return check


def _version(value):
    if value is not None and not _safe(value):
        raise WriterError("creation_invalid_response")
    return None if value in (None, "null") else value


def _status(response, allowed=(200,)):
    if (
        not isinstance(response, dict)
        or response.get("Error") is not None
        or response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in allowed
    ):
        raise WriterError("creation_invalid_response")


def _failure(exc, code="creation_io_failed"):
    if isinstance(exc, WriterError):
        return exc
    if getattr(exc, "storage_operation_unsettled", False):
        return WriterError(code, "unknown")
    return WriterError(code)


def _close(body):
    try:
        body.close()
    except BaseException:
        raise WriterError("creation_close_unsettled", "unknown") from None


def _abort(client, bucket, key, upload_id):
    try:
        response = client.abort_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id
        )
        _status(response, (200, 204))
    except BaseException:
        raise WriterError("creation_abort_unsettled", "unknown") from None


def abort_prepared(client, bucket, key, prepared):
    """Abort only before complete has been attempted, never as final rollback."""
    _prepared(prepared, bucket, key)
    if prepared.upload_id is not None:
        _abort(client, bucket, key, prepared.upload_id)


def _start(client, bucket, key, content_type):
    try:
        response = client.create_multipart_upload(
            Bucket=bucket, Key=key, ContentType=content_type
        )
        _status(response)
        upload_id = response.get("UploadId")
        if not _safe(upload_id):
            raise WriterError("creation_invalid_response")
        return upload_id
    except ClientError as exc:
        if _denied(exc):
            raise WriterError("creation_access_denied") from None
        raise WriterError("creation_prepare_unknown", "unknown") from None
    except BaseException:
        # A constructor response lost before UploadId is retained cannot be
        # reconciled by inventing an ID or scanning another writer's prefix.
        raise WriterError("creation_prepare_unknown", "unknown") from None


def _error(exc):
    response = exc.response if isinstance(exc.response, dict) else {}
    metadata, detail = response.get("ResponseMetadata"), response.get("Error")
    return (
        detail.get("Code") if isinstance(detail, dict) else None,
        metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None,
    )


def _denied(exc):
    code, status = _error(exc)
    return status in (401, 403) or code in ("AccessDenied", "403")


def _part(call, **params):
    try:
        response = call(**params)
        _status(response)
        return response
    except ClientError as exc:
        if _denied(exc):
            raise WriterError("creation_access_denied") from None
        code, status = _error(exc)
        if code in ("PreconditionFailed", "412") and status in (None, 412):
            raise WriterError("creation_source_changed") from None
        raise WriterError("creation_part_unknown", "unknown") from None
    except BaseException:
        raise WriterError("creation_part_unknown", "unknown") from None


def prepare_stream(
    client,
    bucket,
    key,
    stream,
    *,
    content_type,
    max_bytes,
    timeout_seconds=TIMEOUT_SECONDS,
    _check=None,
):
    """Prepare parts while hashing actual bytes; completion is a separate step."""
    _locator(bucket, key)
    _budget(max_bytes)
    _content_type(content_type)
    # Size parts from the actual allocation budget instead of imposing an
    # accidental 10000 * 8MiB file limit on larger connector streams.
    mib = 1024**2
    part_bytes = max(
        PART_BYTES, ((max_bytes + MAX_PARTS - 1) // MAX_PARTS + mib - 1) // mib * mib
    )
    if part_bytes > MAX_PART_BYTES:
        raise WriterError("creation_part_budget_exceeded")
    check = _check if _check is not None else _clock(timeout_seconds)
    upload_id, parts, head = None, [], bytearray()
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    try:
        eof = False
        while not eof:
            buffer = bytearray()
            while len(buffer) < part_bytes:
                check()
                requested = min(part_bytes - len(buffer), max_bytes - size + 1)
                chunk = stream.read(requested)
                check()
                if not isinstance(chunk, bytes) or len(chunk) > requested:
                    raise WriterError("creation_invalid_stream")
                if not chunk:
                    eof = True
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise WriterError("creation_byte_budget_exceeded")
                sha.update(chunk)
                md5.update(chunk)
                head.extend(chunk[: max(0, HEAD_BYTES - len(head))])
                buffer.extend(chunk)
            if not buffer:
                break
            if len(parts) >= MAX_PARTS:
                raise WriterError("creation_part_budget_exceeded")
            if upload_id is None:
                check()
                upload_id = _start(client, bucket, key, content_type)
            check()
            response = _part(
                client.upload_part,
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=len(parts) + 1,
                Body=bytes(buffer),
            )
            if not _safe(response.get("ETag")):
                raise WriterError("creation_part_unknown", "unknown")
            parts.append({"PartNumber": len(parts) + 1, "ETag": response["ETag"]})
            check()
        return Prepared(
            sha.hexdigest(),
            md5.hexdigest(),
            size,
            bytes(head),
            upload_id,
            tuple(parts),
            bucket,
            key,
        )
    except BaseException as exc:
        if upload_id is not None:
            _abort(client, bucket, key, upload_id)
        raise _failure(exc) from None


def _observation(response, bucket, key):
    try:
        observed = s3_reference.read_observation(response, bucket, key)
        modified = response.get("LastModified")
        if (
            type(observed["size"]) is not int
            or not isinstance(modified, datetime)
            or modified.utcoffset() is None
            or not _safe(observed["etag"])
        ):
            raise WriterError("creation_invalid_response")
        _version(observed["version_id"])
        observed["content_type"] = _content_type(
            response.get("ContentType", "application/octet-stream")
        )
        return observed
    except BaseException as exc:
        raise _failure(exc, "creation_invalid_response") from None


def _read(client, bucket, key, before, max_bytes, check):
    if before["size"] > max_bytes:
        raise WriterError("creation_byte_budget_exceeded")
    params = {"Bucket": bucket, "Key": key, "IfMatch": before["etag"]}
    if _version(before["version_id"]) is not None:
        params["VersionId"] = before["version_id"]
    body = None
    try:
        check()
        _require_conditions(client, params)
        check()
        response = client.get_object(**params)
        body = response.get("Body")
        check()
        if _observation(response, bucket, key) != before:
            raise WriterError("creation_source_changed")
        if not callable(getattr(body, "read", None)):
            raise WriterError("creation_invalid_response")
        sha, md5, size, head = hashlib.sha256(), hashlib.md5(), 0, bytearray()
        while True:
            check()
            chunk = body.read(CHUNK_BYTES)
            check()
            if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                raise WriterError("creation_invalid_response")
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise WriterError("creation_byte_budget_exceeded")
            if size > before["size"]:
                raise WriterError("creation_source_changed")
            sha.update(chunk)
            md5.update(chunk)
            head.extend(chunk[: max(0, HEAD_BYTES - len(head))])
        if size != before["size"]:
            raise WriterError("creation_source_changed")
        # Also check currentness for versioned reads: consumers of these new
        # locators have not universally adopted immutable VersionId reads.
        check()
        final = _observation(client.head_object(Bucket=bucket, Key=key), bucket, key)
        check()
        if final != before:
            raise WriterError("creation_source_changed")
        return Verified(sha.hexdigest(), md5.hexdigest(), size, bytes(head), final)
    except BaseException as exc:
        raise _failure(exc, "creation_read_failed") from None
    finally:
        if body is not None:
            _close(body)


def _require_conditions(client, params):
    """Only an explicit, consistent precondition rejection qualifies the read."""
    try:
        response = client.get_object(**{**params, "IfMatch": '"aap-impossible-etag"'})
    except ClientError as exc:
        code, status = _error(exc)
        if (
            not _denied(exc)
            and code in ("PreconditionFailed", "412")
            and status in (None, 412)
        ):
            return
        raise WriterError("creation_conditions_unverified") from None
    else:
        body = response.get("Body") if isinstance(response, dict) else None
        if body is not None:
            _close(body)
        raise WriterError("creation_conditions_unsupported")


def inspect_staging(client, bucket, key, *, max_bytes, timeout_seconds=TIMEOUT_SECONDS):
    _locator(bucket, key)
    _budget(max_bytes)
    check = _clock(timeout_seconds)
    try:
        check()
        before = _observation(client.head_object(Bucket=bucket, Key=key), bucket, key)
        check()
        content = _read(client, bucket, key, before, max_bytes, check)
        return Source(
            content.sha256,
            content.md5,
            content.size_bytes,
            content.head_bytes,
            before["etag"],
            _version(before["version_id"]),
            before,
        )
    except BaseException as exc:
        raise _failure(exc, "creation_read_failed") from None


def _proof(value):
    if (
        not isinstance(value.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", value.sha256) is None
        or not isinstance(value.md5, str)
        or re.fullmatch(r"[0-9a-f]{32}", value.md5) is None
        or type(value.size_bytes) is not int
        or value.size_bytes < 0
        or not isinstance(value.head_bytes, bytes)
        or len(value.head_bytes) > min(HEAD_BYTES, value.size_bytes)
    ):
        raise WriterError("creation_invalid_input")


def _prepare_pinned_stream(
    client,
    source_bucket,
    source_key,
    bucket,
    key,
    source,
    content_type,
    seconds,
    check,
    *,
    source_client=None,
):
    """Replace an unqualified server copy with a fully verified bounded stream."""
    reader = client if source_client is None else source_client
    body, prepared = None, None
    params = {
        "Bucket": source_bucket,
        "Key": source_key,
        "IfMatch": source.etag,
        **({"VersionId": source.version_id} if source.version_id is not None else {}),
    }
    try:
        try:
            check()
            _require_conditions(reader, params)
            check()
            response = reader.get_object(**params)
            body = response.get("Body")
            check()
            if _observation(response, source_bucket, source_key) != source.observation:
                raise WriterError("creation_source_changed")
            if not callable(getattr(body, "read", None)):
                raise WriterError("creation_invalid_response")

            prepared = prepare_stream(
                client,
                bucket,
                key,
                body,
                content_type=content_type,
                max_bytes=source.size_bytes,
                timeout_seconds=seconds,
                _check=check,
            )
            if any(
                getattr(prepared, field) != getattr(source, field)
                for field in ("sha256", "md5", "size_bytes", "head_bytes")
            ):
                raise WriterError("creation_source_changed")
        finally:
            if body is not None:
                _close(body)
        # A version-pinned read cannot adopt a source which stopped being
        # current while its bytes or handle were still being consumed.
        check()
        current = reader.head_object(Bucket=source_bucket, Key=source_key)
        check()
        if _observation(current, source_bucket, source_key) != source.observation:
            raise WriterError("creation_source_changed")
        return prepared
    except BaseException as exc:
        if prepared is not None and prepared.upload_id is not None:
            _abort(client, bucket, key, prepared.upload_id)
        raise _failure(exc) from None


def prepare_copy(
    client,
    source_bucket,
    source_key,
    bucket,
    key,
    source,
    *,
    content_type,
    timeout_seconds=TIMEOUT_SECONDS,
    source_client=None,
):
    _locator(source_bucket, source_key)
    _locator(bucket, key)
    _content_type(content_type)
    if not isinstance(source, Source):
        raise WriterError("creation_invalid_input")
    _proof(source)
    if source.size_bytes > MAX_COPY_BYTES:
        raise WriterError("creation_byte_budget_exceeded")
    if (
        source.observation.get("bucket") != source_bucket
        or source.observation.get("key") != source_key
        or source.observation.get("size") != source.size_bytes
        or source.observation.get("etag") != source.etag
        or _version(source.observation.get("version_id")) != source.version_id
        or not _safe(source.etag)
    ):
        raise WriterError("creation_invalid_input")
    if source_client is not None:
        return _prepare_pinned_stream(
            client,
            source_bucket,
            source_key,
            bucket,
            key,
            source,
            content_type,
            timeout_seconds,
            _clock(timeout_seconds),
            source_client=source_client,
        )
    if not source.size_bytes:
        return Prepared(
            source.sha256, source.md5, 0, source.head_bytes, None, (), bucket, key
        )
    check = _clock(timeout_seconds)
    upload_id, abort_started = None, False
    try:
        check()
        upload_id = _start(client, bucket, key, content_type)
        copy = {"Bucket": source_bucket, "Key": source_key}
        if source.version_id is not None:
            copy["VersionId"] = source.version_id
        check()
        response = _part(
            client.upload_part_copy,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=1,
            CopySource=copy,
            CopySourceIfMatch=source.etag,
        )
        if source.version_id is not None and "CopySourceVersionId" not in response:
            # Some S3-compatible services omit the copy-source version echo.
            # Do not infer that they copied the requested immutable version.
            # Settle this exact MPU before preparing an independently hashed
            # pinned stream, under the same original deadline and owner.
            abort_started = True
            _abort(client, bucket, key, upload_id)
            upload_id = None
            check()
            return _prepare_pinned_stream(
                client,
                source_bucket,
                source_key,
                bucket,
                key,
                source,
                content_type,
                timeout_seconds,
                check,
            )
        if _version(response.get("CopySourceVersionId")) != source.version_id:
            raise WriterError("creation_source_changed")
        etag = response.get("CopyPartResult", {}).get("ETag")
        if not _safe(etag):
            raise WriterError("creation_part_unknown", "unknown")
        check()
        return Prepared(
            source.sha256,
            source.md5,
            source.size_bytes,
            source.head_bytes,
            upload_id,
            ({"PartNumber": 1, "ETag": etag},),
            bucket,
            key,
        )
    except BaseException as exc:
        if upload_id is not None and not abort_started:
            _abort(client, bucket, key, upload_id)
        raise _failure(exc) from None


def _prepared(value, bucket, key):
    _locator(bucket, key)
    if not isinstance(value, Prepared) or (value.bucket, value.object_key) != (
        bucket,
        key,
    ):
        raise WriterError("creation_invalid_input")
    _proof(value)
    if value.size_bytes == 0:
        if (
            value.upload_id is not None
            or value.parts
            or value.sha256 != hashlib.sha256(b"").hexdigest()
            or value.md5 != hashlib.md5(b"").hexdigest()
        ):
            raise WriterError("creation_invalid_input")
        return
    if not _safe(value.upload_id) or not 0 < len(value.parts) <= MAX_PARTS:
        raise WriterError("creation_invalid_input")
    for number, part in enumerate(value.parts, 1):
        if (
            not isinstance(part, dict)
            or type(part.get("PartNumber")) is not int
            or part.get("PartNumber") != number
            or not _safe(part.get("ETag"))
        ):
            raise WriterError("creation_invalid_input")


def complete(client, bucket, key, prepared, *, content_type):
    """One attempt only. Never repeat completion or delete a possible final."""
    _prepared(prepared, bucket, key)
    _content_type(content_type)
    try:
        params = {"Bucket": bucket, "Key": key, "IfNoneMatch": "*"}
        if prepared.upload_id is None:
            response = client.put_object(**params, Body=b"", ContentType=content_type)
        else:
            response = client.complete_multipart_upload(
                **params,
                UploadId=prepared.upload_id,
                MultipartUpload={"Parts": [dict(part) for part in prepared.parts]},
            )
        _status(response)
        etag = response.get("ETag")
        if not _safe(etag):
            raise WriterError("creation_invalid_receipt")
        version = _version(response.get("VersionId"))
        request_id = response.get("ResponseMetadata", {}).get("RequestId")
        if request_id is not None and not _safe(request_id):
            raise WriterError("creation_invalid_receipt")
        safe_response = {
            "ETag": etag,
            "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": request_id},
        }
        if "VersionId" in response:
            safe_response["VersionId"] = response["VersionId"]
        return WriteReceipt(etag, version, request_id, safe_response)
    except ClientError as exc:
        if _denied(exc):
            raise WriterError("creation_access_denied") from None
        code, status = _error(exc)
        if code in ("PreconditionFailed", "412") and status in (None, 412):
            raise WriterError("creation_write_conflict", "conflict") from None
        raise WriterError("creation_write_unknown", "unknown") from None
    except BaseException:
        raise WriterError("creation_write_unknown", "unknown") from None


def verify(
    client,
    bucket,
    key,
    receipt,
    *,
    sha256,
    size_bytes,
    max_bytes,
    timeout_seconds=TIMEOUT_SECONDS,
):
    _locator(bucket, key)
    _budget(max_bytes)
    if (
        not isinstance(receipt, WriteReceipt)
        or not _safe(receipt.etag)
        or _version(receipt.version_id) != receipt.version_id
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or type(size_bytes) is not int
        or not 0 <= size_bytes <= max_bytes
    ):
        raise WriterError("creation_invalid_input")
    check = _clock(timeout_seconds)
    try:
        params = {"Bucket": bucket, "Key": key}
        if receipt.version_id is not None:
            params["VersionId"] = receipt.version_id
        check()
        before = _observation(client.head_object(**params), bucket, key)
        check()
        if (
            before["etag"] != receipt.etag
            or _version(before["version_id"]) != receipt.version_id
            or before["size"] != size_bytes
        ):
            raise WriterError("creation_source_changed")
        content = _read(client, bucket, key, before, max_bytes, check)
        if content.sha256 != sha256 or content.size_bytes != size_bytes:
            raise WriterError("creation_content_mismatch")
        return content
    except BaseException as exc:
        raise _failure(exc, "creation_verify_failed") from None
