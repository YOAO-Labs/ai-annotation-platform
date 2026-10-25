"""Registered dataset original targets and their existing writer capabilities."""

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


def probe_target(target):
    """Exercise empty PUT and nonempty MPU through the production writer."""
    try:
        with target.client(read_timeout=60) as client:
            for payload in (b"", uuid.uuid4().bytes):
                key = target.key_prefix + ".probe-original-" + uuid.uuid4().hex
                receipts = []
                try:
                    prepared = writer.prepare_stream(
                        client,
                        target.bucket,
                        key,
                        io.BytesIO(payload),
                        content_type="application/octet-stream",
                        max_bytes=len(payload),
                    )
                    receipt = writer.complete(
                        client,
                        target.bucket,
                        key,
                        prepared,
                        content_type="application/octet-stream",
                    )
                    receipts.append(receipt)
                    writer.verify(
                        client,
                        target.bucket,
                        key,
                        receipt,
                        sha256=prepared.sha256,
                        size_bytes=prepared.size_bytes,
                        max_bytes=len(payload),
                    )
                    duplicate = writer.prepare_stream(
                        client,
                        target.bucket,
                        key,
                        io.BytesIO(payload),
                        content_type="application/octet-stream",
                        max_bytes=len(payload),
                    )
                    try:
                        unexpected = writer.complete(
                            client,
                            target.bucket,
                            key,
                            duplicate,
                            content_type="application/octet-stream",
                        )
                    except writer.WriterError as exc:
                        if exc.outcome != "conflict":
                            raise
                        writer.abort_prepared(client, target.bucket, key, duplicate)
                    else:
                        receipts.append(unexpected)
                        raise shared.error("datasets", "conditions_unsupported")
                finally:
                    for receipt in reversed(receipts):
                        params = shared.object_params(
                            target.bucket, key, receipt.version_id
                        )
                        try:
                            response = client.delete_object(**params)
                            if response.get("ResponseMetadata", {}).get(
                                "HTTPStatusCode"
                            ) not in {200, 204}:
                                raise shared.error("datasets", "probe_cleanup_unproven")
                            shared._prove_absent(client, params, purpose="datasets")
                        except BaseException as exc:
                            raise storage_access.mark_unsettled(exc)
    except writer.WriterError as exc:
        error = shared.error("datasets", "unavailable")
        if getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from None
    return {
        "conditional_write": True,
        "full_read": True,
        "delete": True,
        "empty_put": True,
        "multipart": True,
    }
