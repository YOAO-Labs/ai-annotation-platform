"""Prove the cache's conditional PUT, full read, prefix LIST and exact cleanup."""

import hashlib
import uuid

from botocore.exceptions import ClientError

from app.services import managed_s3_target as shared, storage_access
from app.services.media_cache_storage import (
    MANAGED_NAMESPACE as MANAGED_NAMESPACE,
    managed_prefix as managed_prefix,
    target_for as target_for,
)


def _probe(client, target):
    prefix = target.key_prefix + ".probe-media-cache/" + uuid.uuid4().hex + "/"
    key = prefix + "payload"
    payload = hashlib.sha256(uuid.uuid4().bytes).digest()
    receipts = []
    failure = None
    unknown_current = False
    try:
        response = shared._conditional_put(
            client, shared.object_params(target.bucket, key), payload
        )
        try:
            version = shared.version_id(response, purpose="media-cache")
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        params = shared.object_params(target.bucket, key, version)
        receipts.append(params)
        shared._read_probe(client, params, payload, purpose="media-cache")
        listing = client.list_objects_v2(Bucket=target.bucket, Prefix=prefix, MaxKeys=2)
        objects = listing.get("Contents", [])
        if (
            listing.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or listing.get("IsTruncated")
            or not isinstance(objects, list)
            or len(objects) != 1
            or objects[0].get("Key") != key
            or objects[0].get("Size") != len(payload)
        ):
            raise shared.error("media-cache", "invalid_receipt")
        try:
            unexpected = shared._conditional_put(
                client, shared.object_params(target.bucket, key), b"conflict"
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
                "409",
            }:
                unknown_current = bool(
                    getattr(exc, "storage_operation_unsettled", False)
                )
                raise
        except BaseException as exc:
            unknown_current = bool(getattr(exc, "storage_operation_unsettled", False))
            raise
        else:
            try:
                version = shared.version_id(unexpected, purpose="media-cache")
            except BaseException as exc:
                unknown_current = True
                raise storage_access.mark_unsettled(exc)
            receipts.append(shared.object_params(target.bucket, key, version))
            raise shared.error("media-cache", "conditions_unsupported")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        for params in reversed(receipts):
            if unknown_current and params.get("VersionId") in {None, "null"}:
                continue
            try:
                response = client.delete_object(**params)
                if response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {
                    200,
                    204,
                }:
                    raise shared.error("media-cache", "probe_cleanup_unproven")
                shared._prove_absent(client, params, purpose="media-cache")
            except BaseException:
                if failure is None:
                    raise storage_access.mark_unsettled(
                        shared.error("media-cache", "probe_cleanup_unproven")
                    ) from None
                storage_access.mark_unsettled(failure)


def probe_target(target):
    context, failure = target.client(read_timeout=60), None
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
    return {"conditional_write": True, "full_read": True, "delete": True, "list": True}
