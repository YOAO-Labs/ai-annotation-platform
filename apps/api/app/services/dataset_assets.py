"""Append compatible identity mappings; the existing storage service owns I/O."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.services import storage_registry_data
from app.services.storage import storage_service
from app.services.storage_identity import LEGACY_LOCATION_ID

MAX_DIRECT_UPLOAD_BYTES = 5 * 1024**3
log = logging.getLogger(__name__)
PURPOSE_BUCKET_ATTRIBUTES = {
    "annotations": "bucket",
    "datasets": "datasets_bucket",
    "bug-reports": "bug_reports_bucket",
    "media-cache": "media_cache_bucket",
    "audit-archive": "audit_archive_bucket",
    "import": "import_bucket",
    "export": "export_bucket",
    "avatars": "avatars_bucket",
    "training": "training_bucket",
}


async def validate_legacy_bindings(db):
    """Read-only validation, including all purposes rather than just datasets."""
    location = await storage_registry_data.get_location(db, LEGACY_LOCATION_ID)
    errors = []
    config = {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    if location is None:
        errors.append("missing_platform_location")
    elif (
        location.config != config
        or location.kind != "s3"
        or location.config_revision != 1
    ):
        errors.append("platform_location_mismatch")
    bindings = {row.purpose: row for row in await storage_registry_data.bindings(db)}
    for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items():
        binding = bindings.get(purpose)
        if binding is None:
            errors.append(f"missing_binding:{purpose}")
        elif (
            purpose
            in {
                "avatars",
                "bug-reports",
                "audit-archive",
                "export",
                "import",
                "datasets",
                "media-cache",
                "annotations",
                "training",
            }
            and binding.generation >= 2
        ):
            from app.services.managed_s3_target import target_origin_for
            from app.services.storage_errors import (
                AnnotationsStorageError,
                AuditArchiveStorageError,
                AvatarStorageError,
                BugAttachmentStorageError,
                DatasetStorageError,
                ExportStorageError,
                ImportStorageError,
                MediaCacheStorageError,
                TrainingDatasetError,
            )

            target_location = await storage_registry_data.get_location(
                db, binding.location_id
            )
            try:
                target = await target_origin_for(
                    db,
                    target_location,
                    purpose=purpose,
                    require_enabled=False,
                    historical_export=purpose == "export",
                )
                if binding.bucket != target.bucket:
                    errors.append(f"binding_mismatch:{purpose}")
            except (
                AvatarStorageError,
                BugAttachmentStorageError,
                AuditArchiveStorageError,
                ExportStorageError,
                ImportStorageError,
                DatasetStorageError,
                MediaCacheStorageError,
                AnnotationsStorageError,
                TrainingDatasetError,
            ):
                errors.append(f"binding_mismatch:{purpose}")
        elif (
            binding.location_id != LEGACY_LOCATION_ID
            or binding.bucket != getattr(storage_service, attr)
            or binding.generation != 1
        ):
            errors.append(f"binding_mismatch:{purpose}")
    return errors


async def ensure_legacy_bindings(db):
    config = {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    buckets = {
        purpose: getattr(storage_service, attr)
        for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items()
    }
    if any(not bucket for bucket in buckets.values()):
        raise ValueError("Platform storage purpose has no configured bucket")
    if not await storage_registry_data.ensure_embedded_targets(db, config, buckets):
        raise ValueError(
            "Platform storage location differs from its pinned configuration"
        )
    if await validate_legacy_bindings(db):
        raise ValueError(
            "Platform storage binding differs from its pinned configuration"
        )
    return await storage_registry_data.get_binding(db, "datasets")


async def register_item_asset(
    db, item, *, sha256=None, size_bytes=None, etag=None, binding=None, storage_ref=None
):
    """The item lock serializes backfill and completion, without touching its row."""
    from app.services.reference_keys import reject_reference

    reject_reference(item.file_path)
    if storage_ref is not None:
        from app.services.dataset_storage import resolve_ref

        saved = resolve_ref(storage_ref)
        if saved.object_key != item.file_path or (
            binding is not None
            and (binding.location_id, binding.bucket)
            != (saved.location_id, saved.bucket)
        ):
            raise ValueError("Dataset locator differs from its saved address")
        binding = saved
    elif binding is None:
        await ensure_legacy_bindings(db)
        binding = legacy_dataset_binding()
    if db.autoflush:
        await db.flush()
    if not await db.scalar(
        text(
            "SELECT yoao_assets_v1.lock_managed_item_asset_slot("
            "CAST(:item_id AS uuid), :file_path)"
        ),
        {"item_id": item.id, "file_path": item.file_path},
    ):
        raise ValueError("Dataset locator differs from its pinned Item")
    from app.services.managed_original_reclaim_fence import shared

    await shared(db, binding.bucket, item.file_path)
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.register_managed_item_asset("
                "CAST(:item_id AS uuid), CAST(:asset_id AS uuid), :file_path, "
                "CAST(:location_id AS uuid), :bucket, CAST(:storage_ref AS jsonb), "
                ":sha256, CAST(:size_bytes AS bigint), :etag, :verified_at)"
            ),
            {
                "item_id": item.id,
                "asset_id": uuid.uuid5(
                    uuid.NAMESPACE_URL, f"aap:dataset-item:{item.id}"
                ),
                "file_path": item.file_path,
                "location_id": binding.location_id,
                "bucket": binding.bucket,
                "storage_ref": json.dumps(storage_ref, separators=(",", ":"))
                if storage_ref is not None
                else None,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "etag": etag,
                "verified_at": datetime.now(timezone.utc),
            },
        )
    ).one()


def legacy_dataset_binding():
    """Known deployment objects retain A independently of the current binding."""
    return SimpleNamespace(
        purpose="datasets",
        location_id=LEGACY_LOCATION_ID,
        bucket=storage_service.datasets_bucket,
        generation=1,
    )


async def find_verified_content(
    db, dataset_id, sha256, *, exclude_item_id=None, legacy_md5=None
):
    if db.autoflush:
        await db.flush()
    candidates = await db.execute(
        text(
            "SELECT * FROM yoao_assets_v1.dataset_managed_duplicate_candidates("
            "CAST(:dataset_id AS uuid), CAST(:exclude AS uuid), :sha256, :legacy_md5)"
        ),
        {
            "dataset_id": dataset_id,
            "exclude": exclude_item_id,
            "sha256": sha256,
            "legacy_md5": legacy_md5,
        },
    )
    for candidate in candidates:
        if candidate.verified:
            return candidate
        try:
            actual_md5, actual_sha256, size, etag = await run_in_threadpool(
                hash_stored_object, candidate.file_path
            )
        except (BotoCoreError, ClientError, OSError, ValueError):
            log.warning(
                "Legacy content candidate could not be verified: item=%s", candidate.id
            )
            continue
        if actual_md5 == legacy_md5 and actual_sha256 == sha256:
            await register_item_asset(
                db, candidate, sha256=actual_sha256, size_bytes=size, etag=etag
            )
            return candidate
    return None


def hash_stored_object(key, expected=None):
    """Read actual bytes in bounded chunks; an ETag never establishes a digest."""
    expected = expected or {}
    condition = {}
    if expected.get("VersionId"):
        condition["VersionId"] = expected["VersionId"]
    if expected.get("ETag"):
        condition["IfMatch"] = expected["ETag"]
    response = storage_service.client.get_object(
        Bucket=storage_service.datasets_bucket, Key=key, **condition
    )
    body = response["Body"]
    md5, sha256, size = hashlib.md5(), hashlib.sha256(), 0
    try:
        if expected.get("ETag") and response.get("ETag") != expected["ETag"]:
            raise ValueError("Object changed during content verification")
        while chunk := body.read(8 * 1024 * 1024):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
            if size > MAX_DIRECT_UPLOAD_BYTES:
                raise ValueError("Object exceeds direct-upload verification budget")
        if (
            expected.get("ContentLength") is not None
            and size != expected["ContentLength"]
        ):
            raise ValueError("Object size changed during content verification")
    finally:
        body.close()
    return (
        md5.hexdigest(),
        sha256.hexdigest(),
        size,
        (response.get("ETag") or "").strip('"'),
    )
