"""Append compatible identity mappings; the existing storage service owns I/O."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.services.storage import storage_service

LEGACY_LOCATION_ID = uuid.uuid5(uuid.NAMESPACE_URL, "aap:platform-storage:legacy")
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
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
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
    bindings = {
        row.purpose: row
        for row in await db.scalars(
            select(StorageBinding).execution_options(populate_existing=True)
        )
    }
    for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items():
        binding = bindings.get(purpose)
        if binding is None:
            errors.append(f"missing_binding:{purpose}")
        elif purpose == "avatars" and binding.generation >= 2:
            from app.services.avatar_storage import AvatarStorageError
            from app.services.avatar_storage_target import target_for

            target_location = await db.get(
                StorageLocation, binding.location_id, populate_existing=True
            )
            try:
                target = await target_for(db, target_location, require_enabled=False)
                if binding.bucket != target.bucket:
                    errors.append(f"binding_mismatch:{purpose}")
            except AvatarStorageError:
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
    await db.execute(
        insert(StorageLocation)
        .values(
            id=LEGACY_LOCATION_ID, name="legacy-platform-s3", kind="s3", config=config
        )
        .on_conflict_do_nothing()
    )
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    if location is None or location.config != config:
        raise ValueError(
            "Platform storage location differs from its pinned configuration"
        )
    await db.execute(
        insert(StorageBinding)
        .values(
            [
                {
                    "purpose": purpose,
                    "location_id": LEGACY_LOCATION_ID,
                    "bucket": bucket,
                    "generation": 1,
                }
                for purpose, bucket in buckets.items()
            ]
        )
        .on_conflict_do_nothing()
    )
    bindings = {
        row.purpose: row
        for row in (
            await db.scalars(
                select(StorageBinding).execution_options(populate_existing=True)
            )
        ).all()
    }
    if await validate_legacy_bindings(db):
        raise ValueError(
            "Platform storage binding differs from its pinned configuration"
        )
    return bindings["datasets"]


async def register_item_asset(
    db, item, *, sha256=None, size_bytes=None, etag=None, binding=None
):
    """The item lock serializes backfill and completion, without touching its row."""
    from app.services.reference_keys import reject_reference

    reject_reference(item.file_path)
    binding = binding or await ensure_legacy_bindings(db)
    await db.execute(
        select(DatasetItem.id).where(DatasetItem.id == item.id).with_for_update()
    )
    asset = await db.scalar(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == item.id)
        .execution_options(populate_existing=True)
    )
    if asset is None:
        asset = DatasetAsset(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"aap:dataset-item:{item.id}"),
            dataset_item_id=item.id,
            media_type=item.file_type,
            size_bytes=item.file_size,
            legacy_hash_hint=item.content_hash,
            verification_state="unverified",
        )
        db.add(asset)
        await db.flush()
    if sha256 is not None:
        if asset.verification_state == "verified" and (
            asset.checksum_value != sha256 or asset.size_bytes != size_bytes
        ):
            raise ValueError("Dataset content differs from its verified identity")
        asset.checksum_algorithm = "sha256"
        asset.checksum_value = sha256
        asset.size_bytes = size_bytes
        asset.verification_state = "verified"
    replica = await db.get(
        DatasetAssetLocation, (asset.id, binding.location_id, binding.bucket)
    )
    if replica is None:
        replica = DatasetAssetLocation(
            asset_id=asset.id,
            location_id=binding.location_id,
            bucket=binding.bucket,
            object_key=item.file_path,
            reference_state="unverified",
        )
        db.add(replica)
    elif replica.object_key != item.file_path:
        raise ValueError("Dataset locator differs from its pinned mapping")
    if sha256 is not None:
        replica.reference_state = "present"
        replica.verified_at = datetime.now(timezone.utc)
    if etag is not None:
        replica.etag = etag
    await db.flush()
    return asset


async def find_verified_content(
    db, dataset_id, sha256, *, exclude_item_id=None, legacy_md5=None
):
    from app.services.reference_keys import PREFIX

    stmt = (
        select(DatasetItem)
        .join(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
        .where(
            DatasetItem.dataset_id == dataset_id,
            ~DatasetItem.file_path.startswith(PREFIX, autoescape=True),
            DatasetAsset.verification_state == "verified",
            DatasetAsset.checksum_value == sha256,
        )
    )
    if exclude_item_id:
        stmt = stmt.where(DatasetItem.id != exclude_item_id)
    existing = await db.scalar(stmt.limit(1))
    if existing is not None or not legacy_md5:
        return existing
    # A legacy hash only selects candidates; actual bytes establish duplicates.
    candidates = (
        select(DatasetItem)
        .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
        .where(
            DatasetItem.dataset_id == dataset_id,
            ~DatasetItem.file_path.startswith(PREFIX, autoescape=True),
            DatasetItem.content_hash == legacy_md5,
            (
                DatasetAsset.id.is_(None)
                | (DatasetAsset.verification_state == "unverified")
            ),
        )
        .order_by(DatasetItem.id)
    )
    if exclude_item_id:
        candidates = candidates.where(DatasetItem.id != exclude_item_id)
    for candidate in await db.scalars(candidates):
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
