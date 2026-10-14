"""Leased publication with immutable keys and an atomic source-fenced pointer.

Functions flush but never commit. Reserve and commit before inference/upload to
retain interrupted-attempt evidence, then publish and commit in a new transaction.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.point_cloud_auxiliary import (
    PointCloudAuxiliaryAsset,
    PointCloudAuxiliaryGeneration,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.point_cloud_auxiliary import PointCloudEnhancementConfig
from app.services.storage import storage_service

from .codec import MAX_DERIVED_BYTES, encode_cloud
from .sources import AuxiliaryError, FrozenInputs, resolve_task_inputs

AUXILIARY_PREFIX = "point-cloud-auxiliary"
LEASE_SECONDS = 3600
MODEL_SOURCE_COMMIT = "48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6"
WEIGHT_SHA256 = "c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generation_prefix(asset_id: uuid.UUID, generation: int) -> str:
    return f"{AUXILIARY_PREFIX}/{asset_id}/{generation}"


async def assert_enabled(
    db: AsyncSession, task: Task, profile: str, *, lock: bool = False
) -> None:
    if not settings.point_cloud_ai_enhancement_enabled:
        raise AuxiliaryError("deployment_disabled")
    query = select(Project).where(Project.id == task.project_id)
    if lock:
        query = query.with_for_update()
    project = (
        await db.execute(query.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if project is None or project.data_type != "lidar":
        raise AuxiliaryError("not_point_cloud")
    config = PointCloudEnhancementConfig.model_validate(
        project.point_cloud_enhancement_config or {}
    )
    if not config.enabled:
        raise AuxiliaryError("project_disabled")
    if config.profile != profile:
        raise AuxiliaryError("profile_changed")


async def find_asset(
    db: AsyncSession, inputs: FrozenInputs, *, lock: bool = False
) -> PointCloudAuxiliaryAsset | None:
    query = select(PointCloudAuxiliaryAsset).where(
        PointCloudAuxiliaryAsset.dataset_item_id == inputs.primary.id,
        PointCloudAuxiliaryAsset.binding_digest == inputs.binding_digest,
        PointCloudAuxiliaryAsset.profile == inputs.profile,
    )
    if lock:
        query = query.with_for_update()
    return (
        await db.execute(query.execution_options(populate_existing=True))
    ).scalar_one_or_none()


def verify_descriptor(descriptor: dict, storage=storage_service) -> bool:
    head = storage.verify_upload(descriptor["key"], bucket=storage.datasets_bucket)
    return bool(
        head
        and head.get("ContentLength") == descriptor["byte_size"]
        and head.get("Metadata", {}).get("sha256") == descriptor["sha256"]
        and str(head.get("ETag") or "").strip('"') == descriptor.get("etag", "")
        and head.get("VersionId") == descriptor.get("version_id")
    )


async def get_ready_generation(
    db: AsyncSession, task: Task, profile: str | None = None, *, storage=storage_service
):
    inputs = await resolve_task_inputs(db, task, profile, storage=storage)
    asset = await find_asset(db, inputs)
    active = (
        await db.get(
            PointCloudAuxiliaryGeneration,
            (asset.id, asset.active_generation),
            populate_existing=True,
        )
        if asset and asset.active_generation
        else None
    )
    if (
        active is None
        or active.status != "ready"
        or active.input_digest != inputs.input_digest
        or not active.cameras
        or not all(
            await asyncio.gather(
                *(
                    asyncio.to_thread(verify_descriptor, camera, storage)
                    for camera in active.cameras
                )
            )
        )
    ):
        active = None
    return inputs, asset, active


async def reserve_generation(
    db: AsyncSession, task: Task, inputs: FrozenInputs, *, storage=storage_service
) -> PointCloudAuxiliaryGeneration:
    await assert_enabled(db, task, inputs.profile)
    current = await resolve_task_inputs(db, task, inputs.profile, storage=storage)
    if current.input_digest != inputs.input_digest:
        raise AuxiliaryError("source_changed")
    await db.execute(
        insert(PointCloudAuxiliaryAsset)
        .values(
            id=uuid.uuid4(),
            dataset_item_id=inputs.primary.id,
            binding_digest=inputs.binding_digest,
            profile=inputs.profile,
        )
        .on_conflict_do_nothing(
            constraint="uq_point_cloud_auxiliary_owner_binding_profile"
        )
    )
    asset = await find_asset(db, inputs, lock=True)
    assert asset is not None
    if asset.active_generation is not None:
        active = await db.get(
            PointCloudAuxiliaryGeneration,
            (asset.id, asset.active_generation),
            populate_existing=True,
        )
        if (
            active
            and active.status == "ready"
            and active.input_digest == inputs.input_digest
            and active.cameras
            and all(
                await asyncio.gather(
                    *(
                        asyncio.to_thread(verify_descriptor, camera, storage)
                        for camera in active.cameras
                    )
                )
            )
        ):
            return active
    if asset.building_generation is not None:
        building = await db.get(
            PointCloudAuxiliaryGeneration,
            (asset.id, asset.building_generation),
            populate_existing=True,
        )
        if building and building.status == "building":
            if building.lease_expires_at and building.lease_expires_at > utcnow():
                raise AuxiliaryError("publication_in_progress")
            building.status = "failed"
            building.error_code = "publication_lease_expired"
            building.lease_token = None
            building.lease_expires_at = None
            await db.flush()
    last = await db.scalar(
        select(func.max(PointCloudAuxiliaryGeneration.generation)).where(
            PointCloudAuxiliaryGeneration.asset_id == asset.id
        )
    )
    row = PointCloudAuxiliaryGeneration(
        asset_id=asset.id,
        generation=(last or 0) + 1,
        input_digest=inputs.input_digest,
        source_manifest=inputs.dump_manifest(),
        status="building",
        lease_token=uuid.uuid4(),
        lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS),
    )
    db.add(row)
    asset.building_generation = row.generation
    await db.flush()
    return row


def validate_provenance(provenance: dict, profile: str) -> dict:
    result = json.loads(json.dumps(provenance, allow_nan=False))
    if (
        result.get("profile") != profile
        or not result.get("producer")
        or not result.get("calibration_policy")
        or not isinstance(result.get("preprocessing"), dict)
        or not isinstance(result.get("filtering"), dict)
        or not re.fullmatch(r"[a-f0-9]{40}", str(result.get("model_source_commit", "")))
        or not re.fullmatch(r"[a-f0-9]{64}", str(result.get("weight_sha256", "")))
    ):
        raise AuxiliaryError("producer_provenance_invalid")
    if len(json.dumps(result)) > 256 * 1024:
        raise AuxiliaryError("producer_provenance_too_large")
    if (
        result["model_source_commit"] != MODEL_SOURCE_COMMIT
        or result["weight_sha256"] != WEIGHT_SHA256
    ):
        raise AuxiliaryError("producer_profile_mismatch")
    return result


def _put_verified(
    storage, key: str, body: bytes, sha256: str, content_type: str
) -> dict:
    storage.client.put_object(
        Bucket=storage.datasets_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        CacheControl="private, max-age=31536000, immutable",
        Metadata={"sha256": sha256},
    )
    head = storage.verify_upload(key, bucket=storage.datasets_bucket)
    if (
        not head
        or head.get("ContentLength") != len(body)
        or head.get("Metadata", {}).get("sha256") != sha256
    ):
        raise AuxiliaryError("published_object_invalid")
    return {
        "etag": str(head.get("ETag") or "").strip('"'),
        "version_id": head.get("VersionId"),
    }


async def publish_auxiliary(
    db: AsyncSession,
    task: Task,
    inputs: FrozenInputs,
    camera_clouds: dict[str, dict],
    provenance: dict,
    *,
    generation: PointCloudAuxiliaryGeneration | None = None,
    storage=storage_service,
) -> PointCloudAuxiliaryGeneration:
    """Publish raw-primary XYZ; caller owns commit and retains any prior active result."""
    provenance = validate_provenance(provenance, inputs.profile)
    roles = {camera.role: camera for camera in inputs.cameras}
    if not camera_clouds or not set(camera_clouds).issubset(roles):
        raise AuxiliaryError("camera_binding_mismatch")
    serialized, total = {}, 0
    for role, cloud in sorted(camera_clouds.items()):
        pcd, audit, count = encode_cloud(cloud)
        total += len(pcd) + len(audit)
        if total > MAX_DERIVED_BYTES:
            raise AuxiliaryError("derived_bytes_exceeded")
        serialized[role] = (pcd, audit, count)
    generation = generation or await reserve_generation(
        db, task, inputs, storage=storage
    )
    if generation.status == "ready":
        if generation.input_digest != inputs.input_digest:
            raise AuxiliaryError("generation_source_mismatch")
        await assert_enabled(db, task, inputs.profile)
        current = await resolve_task_inputs(db, task, inputs.profile, storage=storage)
        if current.input_digest != inputs.input_digest:
            raise AuxiliaryError("source_changed")
        return generation
    identity, lease = (
        (generation.asset_id, generation.generation),
        generation.lease_token,
    )
    if lease is None:
        raise AuxiliaryError("publication_lease_lost")
    prefix = generation_prefix(*identity)
    descriptors = []
    for role, (pcd, audit, count) in serialized.items():
        sha, audit_sha = (
            hashlib.sha256(pcd).hexdigest(),
            hashlib.sha256(audit).hexdigest(),
        )
        key = f"{prefix}/{lease}/{role}-{sha}.pcd"
        audit_key = f"{prefix}/{lease}/{role}-{audit_sha}.npz"
        object_identity = await asyncio.to_thread(
            _put_verified, storage, key, pcd, sha, "application/octet-stream"
        )
        await asyncio.to_thread(
            _put_verified,
            storage,
            audit_key,
            audit,
            audit_sha,
            "application/octet-stream",
        )
        descriptors.append(
            {
                "role": role,
                "name": roles[role].name,
                "point_count": count,
                "byte_size": len(pcd),
                "sha256": sha,
                "key": key,
                **object_identity,
                "audit_key": audit_key,
                "audit_sha256": audit_sha,
                "audit_byte_size": len(audit),
            }
        )
    # Lock source owners in one deterministic order before the publication CAS.
    current = await resolve_task_inputs(
        db, task, inputs.profile, lock=True, storage=storage
    )
    await assert_enabled(db, task, inputs.profile, lock=True)
    asset = await find_asset(db, inputs, lock=True)
    if asset is None:
        raise AuxiliaryError("asset_owner_missing")
    row = (
        await db.execute(
            select(PointCloudAuxiliaryGeneration)
            .where(
                PointCloudAuxiliaryGeneration.asset_id == identity[0],
                PointCloudAuxiliaryGeneration.generation == identity[1],
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.status != "building"
        or row.lease_token != lease
        or not row.lease_expires_at
        or row.lease_expires_at <= utcnow()
        or asset.building_generation != row.generation
    ):
        raise AuxiliaryError("publication_lease_lost")
    if (
        current.input_digest != inputs.input_digest
        or row.input_digest != inputs.input_digest
    ):
        raise AuxiliaryError("source_changed")
    row.cameras = descriptors
    row.provenance = provenance
    row.status = "ready"
    row.published_at = utcnow()
    row.lease_token = None
    row.lease_expires_at = None
    asset.active_generation = row.generation
    asset.building_generation = None
    await db.flush()
    return row


async def fail_generation(
    db: AsyncSession,
    asset_id: uuid.UUID,
    generation: int,
    *,
    lease_token: uuid.UUID,
    reason: str,
) -> bool:
    asset = (
        await db.execute(
            select(PointCloudAuxiliaryAsset)
            .where(PointCloudAuxiliaryAsset.id == asset_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if asset is None:
        return False
    row = (
        await db.execute(
            select(PointCloudAuxiliaryGeneration)
            .where(
                PointCloudAuxiliaryGeneration.asset_id == asset_id,
                PointCloudAuxiliaryGeneration.generation == generation,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.status != "building" or row.lease_token != lease_token:
        return False
    row.status, row.error_code = "failed", reason[:64]
    row.lease_token = None
    row.lease_expires_at = None
    if asset.building_generation == generation:
        asset.building_generation = None
    await db.flush()
    return True
