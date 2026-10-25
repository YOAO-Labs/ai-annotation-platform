"""Probe only the current training writers and exact-prefix cleanup capabilities."""

import hashlib
import uuid

from botocore.exceptions import ClientError

from app.services import managed_s3_target as shared, storage_access
from app.services.training_dataset_storage import target_for as target_for

PURPOSE = "training"
_DENIED = {
    "AccessDenied",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
    "403",
    "NoSuchBucket",
    "404",
    "InvalidRequest",
    "400",
    "InvalidPart",
    "InvalidPartOrder",
}


def _write(operation, **params):
    try:
        response = operation(**params)
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise storage_access.mark_unsettled(
                shared.error(PURPOSE, "invalid_receipt")
            )
        return response
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _DENIED:
            storage_access.mark_unsettled(exc)
        raise
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)


def _read(client, params, expected):
    response = client.get_object(**params)
    with storage_access.closing_registered(response["Body"]) as body:
        actual = body.read(len(expected) + 1)
        if (
            len(actual) != len(expected)
            or body.read(1)
            or hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest()
        ):
            raise shared.error(PURPOSE, "invalid_receipt")
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ContentRange") is not None
        or response.get("DeleteMarker")
        or (
            params.get("VersionId") is not None
            and not shared.version_matches(
                params["VersionId"], response.get("VersionId")
            )
        )
    ):
        raise shared.error(PURPOSE, "invalid_receipt")


def _listed(client, target, prefix):
    response = client.list_objects_v2(Bucket=target.bucket, Prefix=prefix, MaxKeys=3)
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("IsTruncated")
        or not isinstance(response.get("Contents", []), list)
    ):
        raise shared.error(PURPOSE, "invalid_receipt")
    return response.get("Contents", [])


def _delete(client, target, receipts):
    if not receipts:
        return
    try:
        response = client.delete_objects(
            Bucket=target.bucket,
            Delete={
                "Objects": [
                    {k: v for k, v in item.items() if k != "Bucket"}
                    for item in receipts
                ],
                "Quiet": True,
            },
        )
        if response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        ) != 200 or response.get("Errors"):
            raise shared.error(PURPOSE, "probe_cleanup_unproven")
        for params in receipts:
            shared._prove_absent(client, params, purpose=PURPOSE)
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)


def _probe(client, target):
    prefix = target.key_prefix + ".probe-training/" + uuid.uuid4().hex + "/"
    payloads = {
        prefix + "put": uuid.uuid4().bytes,
        prefix + "multipart": uuid.uuid4().bytes,
    }
    receipts, upload_id, failure = [], None, None
    multipart_key = prefix + "multipart"
    try:
        key = prefix + "put"
        response = _write(
            client.put_object, Bucket=target.bucket, Key=key, Body=payloads[key]
        )
        try:
            version = shared.version_id(response, purpose=PURPOSE)
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        receipts.append(shared.object_params(target.bucket, key, version))
        response = _write(
            client.create_multipart_upload, Bucket=target.bucket, Key=multipart_key
        )
        upload_id = response.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            upload_id = None
            raise storage_access.mark_unsettled(
                shared.error(PURPOSE, "invalid_receipt")
            )
        part = client.upload_part(
            Bucket=target.bucket,
            Key=multipart_key,
            UploadId=upload_id,
            PartNumber=1,
            Body=payloads[multipart_key],
        )
        if not isinstance(part.get("ETag"), str) or not part["ETag"]:
            raise shared.error(PURPOSE, "invalid_receipt")
        response = _write(
            client.complete_multipart_upload,
            Bucket=target.bucket,
            Key=multipart_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]},
        )
        upload_id = None
        try:
            version = shared.version_id(response, purpose=PURPOSE)
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        receipts.append(shared.object_params(target.bucket, multipart_key, version))
        for params in list(receipts):
            head = client.head_object(**params)
            if not shared.version_matches(
                params.get("VersionId"), head.get("VersionId")
            ):
                # No exact write receipt exists for this observed version. Keep
                # it for reconciliation instead of creating a delete marker.
                receipts.remove(params)
                raise storage_access.mark_unsettled(
                    shared.error(PURPOSE, "invalid_receipt")
                )
            _read(client, params, payloads[params["Key"]])
        listing = _listed(client, target, prefix)
        if len(listing) != len(payloads) or {
            item.get("Key"): item.get("Size") for item in listing
        } != {key: len(data) for key, data in payloads.items()}:
            raise shared.error(PURPOSE, "invalid_receipt")
        _delete(client, target, receipts)
        receipts.clear()
        if _listed(client, target, prefix):
            raise storage_access.mark_unsettled(
                shared.error(PURPOSE, "probe_cleanup_unproven")
            )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if upload_id is not None:
            try:
                response = client.abort_multipart_upload(
                    Bucket=target.bucket,
                    Key=multipart_key,
                    UploadId=upload_id,
                )
                if response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {
                    200,
                    204,
                }:
                    raise shared.error(PURPOSE, "probe_cleanup_unproven")
            except BaseException as exc:
                if failure is None:
                    raise storage_access.mark_unsettled(exc)
                storage_access.mark_unsettled(failure)
        if receipts:
            try:
                _delete(client, target, receipts)
            except BaseException:
                if failure is None:
                    raise
                storage_access.mark_unsettled(failure)


def probe_target(target):
    """Ordinary nonempty PUT/MPU, SHA readback, LIST and exact batch deletion."""
    context, failure = target.client(read_timeout=60), None
    try:
        try:
            client = context.__enter__()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        try:
            _probe(client, target)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                context.__exit__(None, None, None)
            except BaseException as exc:
                if failure is None:
                    raise storage_access.mark_unsettled(exc)
                storage_access.mark_unsettled(failure)
    except Exception as exc:
        error = shared.error(PURPOSE, "unavailable")
        if getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from None
    return {
        "write": True,
        "multipart": True,
        "full_read": True,
        "list": True,
        "delete": True,
    }
