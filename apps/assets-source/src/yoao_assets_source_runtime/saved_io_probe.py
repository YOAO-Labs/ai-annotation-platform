"""Finite, purpose-specific qualification of a candidate registered target."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from io import BytesIO
from uuid import uuid4

from yoao_assets_source import managed_original as writer
from yoao_assets_source.saved_io import SavedIOError
from .saved_io_storage import (
    _CONFLICT,
    _NOT_FOUND,
    _multipart,
    physical_error,
    safe_text,
)


def _unsettled(code="probe_cleanup_unproven"):
    return SavedIOError(code, status_code=503, unsettled=True)


def _status(response, wanted):
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode") in wanted


def _version(response, *, required=False):
    if not _status(response, {200}):
        raise _unsettled("storage_invalid_receipt")
    version = response.get("VersionId")
    if version is not None and not safe_text(version, 1024):
        raise _unsettled("storage_invalid_receipt")
    if required and version in {None, "null"}:
        raise _unsettled("versioning_required")
    return version


def _params(bucket, key, version=None):
    params = {"Bucket": bucket, "Key": key}
    if version is not None:
        params["VersionId"] = version
    return params


def _read(client, params, payload):
    head = client.head_object(**params)
    expected = params.get("VersionId")
    if (
        not _status(head, {200})
        or head.get("VersionId") != expected
        and not (expected == "null" and head.get("VersionId") is None)
    ):
        raise _unsettled("storage_invalid_receipt")
    result = client.get_object(**params)
    with _body(result) as body:
        if body.read(len(payload) + 1) != payload or body.read(1):
            raise SavedIOError("storage_invalid_receipt")
    if (
        not _status(result, {200})
        or result.get("ContentRange") is not None
        or result.get("DeleteMarker")
        or result.get("VersionId") != head.get("VersionId")
        or result.get("ContentLength") != len(payload)
    ):
        raise SavedIOError("storage_invalid_receipt")


@contextmanager
def _body(response):
    body = response["Body"]
    try:
        yield body
    finally:
        try:
            body.close()
        except Exception:
            raise _unsettled() from None


def _absent(client, params):
    try:
        result = client.get_object(**params)
    except Exception as exc:
        if physical_error(exc).storage_code in _NOT_FOUND:
            return
        raise _unsettled() from None
    with _body(result):
        raise _unsettled()


def _delete(client, receipts, *, batch=False):
    if not receipts:
        return
    try:
        if batch:
            response = client.delete_objects(
                Bucket=receipts[0]["Bucket"],
                Delete={
                    "Objects": [
                        {k: v for k, v in row.items() if k != "Bucket"}
                        for row in receipts
                    ],
                    "Quiet": True,
                },
            )
            if not _status(response, {200}) or response.get("Errors"):
                raise _unsettled()
        else:
            for params in receipts:
                if not _status(client.delete_object(**params), {200, 204}):
                    raise _unsettled()
        for params in receipts:
            _absent(client, params)
    except Exception:
        raise _unsettled() from None


def _write(operation, **params):
    try:
        return operation(**params)
    except Exception as exc:
        raise physical_error(exc, mutation=True) from None


def _list(client, bucket, prefix, limit):
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=limit)
    entries = response.get("Contents", [])
    if (
        not _status(response, {200})
        or response.get("IsTruncated")
        or not isinstance(entries, list)
    ):
        raise SavedIOError("storage_invalid_receipt")
    return entries


def _version_lists(client, bucket, prefix):
    if _list(client, bucket, prefix, 1):
        raise _unsettled()
    versions = client.list_object_versions(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    if (
        not _status(versions, {200})
        or versions.get("IsTruncated")
        or any(versions.get(name) for name in ("Versions", "DeleteMarkers"))
    ):
        raise _unsettled()


def _checked(client, bucket, key, payload, *, versioned, list_prefix=None):
    receipts, unknown = [], False
    current = _params(bucket, key)
    try:
        written = _write(client.put_object, **current, Body=payload, IfNoneMatch="*")
        receipts.append(_params(bucket, key, _version(written, required=versioned)))
        _read(client, receipts[-1], payload)
        if list_prefix is not None:
            entries = _list(client, bucket, list_prefix, 2)
            if (
                len(entries) != 1
                or entries[0].get("Key") != key
                or entries[0].get("Size") != len(payload)
            ):
                raise SavedIOError("storage_invalid_receipt")
        try:
            written = _write(
                client.put_object, **current, Body=b"conflict", IfNoneMatch="*"
            )
            receipts.append(_params(bucket, key, _version(written, required=versioned)))
        except SavedIOError as exc:
            if exc.storage_code not in _CONFLICT:
                unknown = exc.unsettled
                raise
        else:
            raise SavedIOError("conditions_unsupported")
    finally:
        _delete(
            client,
            [
                p
                for p in reversed(receipts)
                if not unknown or p.get("VersionId") not in {None, "null"}
            ],
        )


def _condition_writer(client, bucket, key, payload):
    receipts, unknown = [], False
    prepared = None
    args = client, bucket, key
    content_type = "application/octet-stream"

    def prepare():
        return writer.prepare_stream(
            *args, BytesIO(payload), content_type=content_type, max_bytes=len(payload)
        )

    try:
        prepared = prepare()
        receipt = writer.complete(*args, prepared, content_type=content_type)
        prepared = None
        receipts.append(_params(bucket, key, receipt.version_id))
        writer.verify(
            *args,
            receipt,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            max_bytes=len(payload),
        )
        prepared = prepare()
        try:
            unexpected = writer.complete(*args, prepared, content_type=content_type)
        except writer.WriterError as exc:
            if exc.outcome != "conflict":
                raise
            writer.abort_prepared(*args, prepared)
            prepared = None
        else:
            prepared = None
            receipts.append(_params(bucket, key, unexpected.version_id))
            raise SavedIOError("conditions_unsupported")
    except writer.WriterError as exc:
        unknown = exc.outcome == "unknown" or bool(
            getattr(exc, "storage_operation_unsettled", False)
        )
        if prepared is not None and not unknown:
            writer.abort_prepared(*args, prepared)
        raise physical_error(exc) from None
    finally:
        _delete(
            client,
            [
                p
                for p in reversed(receipts)
                if not unknown or p.get("VersionId") not in {None, "null"}
            ],
        )


def _training(client, bucket, prefix):
    receipts, payloads = [], {}
    try:
        for multipart, name in ((False, "put"), (True, "multipart")):
            key = prefix + name
            data = payloads[key] = uuid4().bytes
            params = _params(bucket, key)
            if multipart:
                result = _write(
                    _multipart,
                    client=client,
                    params=params,
                    body=data,
                    length=len(data),
                    fields={},
                    condition={},
                )
            else:
                result = _write(client.put_object, **params, Body=data)
            receipts.append(_params(bucket, key, _version(result)))
        for params in list(receipts):
            try:
                _read(client, params, payloads[params["Key"]])
            except SavedIOError as exc:
                if exc.unsettled and params.get("VersionId") in {None, "null"}:
                    receipts.remove(params)
                raise
        entries = _list(client, bucket, prefix, 3)
        if len(entries) != 2 or {
            row.get("Key"): row.get("Size") for row in entries
        } != {key: len(data) for key, data in payloads.items()}:
            raise SavedIOError("storage_invalid_receipt")
    finally:
        _delete(client, receipts, batch=True)
    if _list(client, bucket, prefix, 3):
        raise _unsettled()
    return {
        "write": True,
        "multipart": True,
        "full_read": True,
        "list": True,
        "delete": True,
    }


def probe(client, bucket, root, purpose):
    prefix = root + ".probe-" + uuid4().hex
    if purpose in {"datasets", "annotations"}:
        for key, payload in (
            (prefix + "-empty", b""),
            (prefix + "-mpu", uuid4().bytes),
        ):
            _condition_writer(client, bucket, key, payload)
        return {
            "conditional_write": True,
            "full_read": True,
            "delete": True,
            "empty_put": True,
            "multipart": True,
        }
    if purpose == "training":
        return _training(client, bucket, prefix + "/")
    versioned = purpose == "import"
    if versioned:
        versioning = client.get_bucket_versioning(Bucket=bucket)
        if not _status(versioning, {200}) or versioning.get("Status") != "Enabled":
            raise SavedIOError("versioning_required")
    listing = prefix + "/" if purpose == "media-cache" else None
    key = listing + "payload" if listing else prefix
    _checked(
        client, bucket, key, uuid4().bytes, versioned=versioned, list_prefix=listing
    )
    result = {"conditional_write": True, "full_read": True, "delete": True}
    if listing:
        result["list"] = True
    if purpose in {"export", "import"}:
        _version_lists(client, bucket, prefix + "/")
        result.update(list=True, list_versions=True)
    if versioned:
        result["versioned"] = True
    return result
