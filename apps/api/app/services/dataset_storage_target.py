"""Registered dataset original targets and their existing writer capabilities."""

import hashlib
import io
import uuid

from app.services import managed_s3_target as shared
from app.services import managed_original_writer as writer, storage_access

MANAGED_NAMESPACE = "_aap_managed/datasets/"
MAX_MANAGED_PREFIX_CHARS = 891
MAX_MANAGED_PREFIX_BYTES = 915


def managed_prefix(base):
    return shared.managed_prefix(base, purpose="datasets")


async def target_for(db, location, *, require_enabled=True):
    return await shared.target_for(
        db, location, purpose="datasets", require_enabled=require_enabled
    )


def _probe_writer(client, target, payload):
    key = target.key_prefix + ".probe-original-" + uuid.uuid4().hex
    receipts, prepared = [], None
    completion_pending, unknown_final, failure = False, False, None
    try:
        prepared = writer.prepare_stream(
            client,
            target.bucket,
            key,
            io.BytesIO(payload),
            content_type="application/octet-stream",
            max_bytes=len(payload),
        )
        completion_pending = True
        receipt = writer.complete(
            client,
            target.bucket,
            key,
            prepared,
            content_type="application/octet-stream",
        )
        completion_pending, prepared = False, None
        receipts.append(receipt)
        writer.verify(
            client,
            target.bucket,
            key,
            receipt,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            max_bytes=len(payload),
        )
        prepared = writer.prepare_stream(
            client,
            target.bucket,
            key,
            io.BytesIO(payload),
            content_type="application/octet-stream",
            max_bytes=len(payload),
        )
        completion_pending = True
        try:
            unexpected = writer.complete(
                client,
                target.bucket,
                key,
                prepared,
                content_type="application/octet-stream",
            )
        except writer.WriterError as exc:
            if exc.outcome != "conflict":
                raise
            completion_pending = False
            writer.abort_prepared(client, target.bucket, key, prepared)
            prepared = None
        else:
            completion_pending, prepared = False, None
            receipts.append(unexpected)
            raise shared.error("datasets", "conditions_unsupported")
    except BaseException as exc:
        failure = exc
        unknown_final = (
            completion_pending and getattr(exc, "outcome", None) == "unknown"
        )
        if getattr(exc, "outcome", None) == "unknown":
            storage_access.mark_unsettled(exc)
        if prepared is not None:
            # An exact unfinished MPU can be aborted even when its completion
            # response was lost; this never proves the final object absent.
            try:
                writer.abort_prepared(client, target.bucket, key, prepared)
            except BaseException:
                storage_access.mark_unsettled(exc)
        raise
    finally:
        for receipt in reversed(receipts):
            if unknown_final and receipt.version_id is None:
                # A newer unreceipted write could now own this current key.
                continue
            params = shared.object_params(target.bucket, key, receipt.version_id)
            try:
                response = client.delete_object(**params)
                if response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {
                    200,
                    204,
                }:
                    raise shared.error("datasets", "probe_cleanup_unproven")
                shared._prove_absent(client, params, purpose="datasets")
            except BaseException as exc:
                if failure is not None:
                    storage_access.mark_unsettled(failure)
                else:
                    raise storage_access.mark_unsettled(exc)


def probe_target(target):
    """Exercise empty PUT and nonempty MPU through the production writer."""
    context, failure = target.client(read_timeout=60), None
    try:
        client = context.__enter__()
        try:
            for payload in (b"", uuid.uuid4().bytes):
                _probe_writer(client, target, payload)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                context.__exit__(None, None, None)
            except BaseException as exc:
                if failure is not None:
                    storage_access.mark_unsettled(failure)
                else:
                    raise storage_access.mark_unsettled(exc)
    except writer.WriterError as exc:
        error = shared.error("datasets", "unavailable")
        if exc.outcome == "unknown" or getattr(
            exc, "storage_operation_unsettled", False
        ):
            storage_access.mark_unsettled(error)
        raise error from None
    return {
        "conditional_write": True,
        "full_read": True,
        "delete": True,
        "empty_put": True,
        "multipart": True,
    }
