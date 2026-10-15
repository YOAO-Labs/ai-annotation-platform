"""Bounded cleanup of expired attempts and objects whose source owner vanished."""

import asyncio
from datetime import timedelta
import uuid

from sqlalchemy import select

from app.db.models.point_cloud_auxiliary import (
    PointCloudAuxiliaryAsset,
    PointCloudAuxiliaryGeneration,
)
from app.services.storage import storage_service

from .publication import AUXILIARY_PREFIX, generation_prefix, utcnow


async def reconcile_auxiliary(
    db,
    *,
    apply: bool = False,
    limit: int = 100,
    grace_hours: int = 24,
    cursor: str | None = None,
    storage=storage_service,
) -> dict:
    """Caller commits. Ready generations are retained while their owner exists."""
    now = utcnow()
    result = {
        "expired": [],
        "partial_prefixes": [],
        "orphan_prefixes": [],
        "next_cursor": None,
    }
    expired = list(
        (
            await db.execute(
                select(PointCloudAuxiliaryGeneration)
                .where(
                    PointCloudAuxiliaryGeneration.status == "building",
                    PointCloudAuxiliaryGeneration.lease_expires_at < now,
                )
                .order_by(PointCloudAuxiliaryGeneration.updated_at)
                .limit(limit)
            )
        ).scalars()
    )
    for candidate in expired:
        asset = (
            await db.execute(
                select(PointCloudAuxiliaryAsset)
                .where(PointCloudAuxiliaryAsset.id == candidate.asset_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        row = (
            await db.execute(
                select(PointCloudAuxiliaryGeneration)
                .where(
                    PointCloudAuxiliaryGeneration.asset_id == candidate.asset_id,
                    PointCloudAuxiliaryGeneration.generation == candidate.generation,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            asset is None
            or row is None
            or row.status != "building"
            or not row.lease_expires_at
            or row.lease_expires_at >= now
        ):
            continue
        result["expired"].append(
            {"asset_id": str(row.asset_id), "generation": row.generation}
        )
        if apply:
            row.status, row.error_code = "failed", "publication_lease_expired"
            row.lease_token = None
            row.lease_expires_at = None
            if asset.building_generation == row.generation:
                asset.building_generation = None
    await db.flush()
    failed = list(
        (
            await db.execute(
                select(PointCloudAuxiliaryGeneration)
                .where(
                    PointCloudAuxiliaryGeneration.status == "failed",
                    PointCloudAuxiliaryGeneration.provenance[
                        "objects_removed_at"
                    ].astext.is_(None),
                    PointCloudAuxiliaryGeneration.updated_at
                    < now - timedelta(hours=grace_hours),
                )
                .order_by(PointCloudAuxiliaryGeneration.updated_at)
                .limit(limit)
            )
        ).scalars()
    )
    for row in failed:
        asset = await db.get(PointCloudAuxiliaryAsset, row.asset_id)
        if asset is None or row.generation in {
            asset.active_generation,
            asset.building_generation,
        }:
            continue
        prefix = generation_prefix(row.asset_id, row.generation)
        result["partial_prefixes"].append(prefix)
        if apply:
            await asyncio.to_thread(
                storage.delete_prefix, prefix, bucket=storage.datasets_bucket
            )
            row.provenance = {
                **(row.provenance or {}),
                "objects_removed_at": now.isoformat(),
            }
    # Enumerate asset directory names, not every point object, with a hard page
    # limit. The next invocation can repeat idempotently after cleanup.
    page_args = dict(
        Bucket=storage.datasets_bucket,
        Prefix=f"{AUXILIARY_PREFIX}/",
        Delimiter="/",
        MaxKeys=limit,
    )
    if cursor:
        page_args["ContinuationToken"] = cursor
    page = await asyncio.to_thread(storage.client.list_objects_v2, **page_args)
    result["next_cursor"] = page.get("NextContinuationToken")
    for entry in page.get("CommonPrefixes", []):
        prefix = entry["Prefix"].rstrip("/")
        try:
            asset_id = uuid.UUID(prefix.rsplit("/", 1)[1])
        except ValueError:
            continue
        if await db.get(PointCloudAuxiliaryAsset, asset_id) is not None:
            continue
        objects_page = await asyncio.to_thread(
            storage.client.list_objects_v2,
            Bucket=storage.datasets_bucket,
            Prefix=prefix + "/",
            MaxKeys=1000,
        )
        objects = objects_page.get("Contents", [])
        if (
            objects_page.get("IsTruncated")
            or not objects
            or any(
                obj["LastModified"] >= now - timedelta(hours=grace_hours)
                for obj in objects
            )
        ):
            continue
        result["orphan_prefixes"].append(prefix)
        if apply:
            await asyncio.to_thread(
                storage.delete_prefix, prefix, bucket=storage.datasets_bucket
            )
    await db.flush()
    return result
