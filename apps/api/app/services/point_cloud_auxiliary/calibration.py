"""Explicit, baseline-preserving repair of legacy nuScenes calibration."""

import asyncio
import uuid

from sqlalchemy import select

from app.db.models.dataset import DatasetItem, Scene
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services.sensor_calibration import (
    calibration_digest,
    resolve_calibration_state,
    update_calibration,
)
from app.services.storage import storage_service

from .sources import (
    AuxiliaryError,
    digest_json,
    nuscenes_calibration,
    object_fingerprint,
)


async def plan_calibration_repair(db, task, *, storage=storage_service) -> dict:
    links = list(
        (
            await db.execute(
                select(TaskDatasetItemLink)
                .where(TaskDatasetItemLink.task_id == task.id)
                .order_by(TaskDatasetItemLink.role)
            )
        ).scalars()
    )
    primary_link = next((link for link in links if link.role == "primary_lidar"), None)
    if primary_link is None or task.dataset_item_id != primary_link.dataset_item_id:
        raise AuxiliaryError("primary_dataset_item_required")
    primary = await db.get(DatasetItem, primary_link.dataset_item_id)
    scene = (
        await db.get(Scene, primary.scene_id) if primary and primary.scene_id else None
    )
    if primary is None or scene is None or scene.source_format != "nuscenes":
        raise AuxiliaryError("nuscenes_source_required")
    primary_source = (primary.metadata_ or {}).get("nuscenes_export") or {}
    primary_fingerprint = await asyncio.to_thread(object_fingerprint, primary, storage)
    repairs = []
    for link in links:
        if not link.role.startswith("camera_"):
            continue
        item = await db.get(DatasetItem, link.dataset_item_id)
        if (
            item is None
            or item.scene_id != primary.scene_id
            or item.frame_index != primary.frame_index
        ):
            raise AuxiliaryError("camera_binding_mismatch")
        camera_source = (item.metadata_ or {}).get("nuscenes_export") or {}
        desired = nuscenes_calibration(
            primary_source, camera_source, scene.source_metadata.get("frame")
        )
        current = await resolve_calibration_state(db, item)
        desired_digest = calibration_digest(desired)
        if current.digest != desired_digest and current.digest != camera_source.get(
            "platform_calibration_digest"
        ):
            # No baseline marker is not proof of an unedited import.
            raise AuxiliaryError("manual_calibration_conflict")
        fingerprint = await asyncio.to_thread(object_fingerprint, item, storage)
        repairs.append(
            {
                "role": link.role,
                "dataset_item_id": str(item.id),
                "expected_revision": current.revision,
                "expected_digest": current.digest,
                "baseline_digest": camera_source.get("platform_calibration_digest"),
                "desired_digest": desired_digest,
                "calibration": desired.model_dump(mode="json"),
                "source_digest": digest_json(camera_source),
                "fingerprint": fingerprint,
            }
        )
    if not repairs:
        raise AuxiliaryError("camera_binding_unsupported")
    return {
        "schema_version": 1,
        "task_id": str(task.id),
        "primary_id": str(primary.id),
        "primary_source_digest": digest_json(primary_source),
        "primary_fingerprint": primary_fingerprint,
        "cameras": repairs,
    }


async def apply_calibration_repair(
    db, task, prepared: dict, *, actor_id, storage=storage_service
) -> dict:
    # Lock all affected inputs before rechecking the prepared snapshot. Existing
    # edit APIs lock the same DatasetItem before appending a revision.
    ids = [
        task.dataset_item_id,
        *[uuid.UUID(entry["dataset_item_id"]) for entry in prepared["cameras"]],
    ]
    await db.execute(
        select(DatasetItem)
        .where(DatasetItem.id.in_(ids))
        .order_by(DatasetItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = await plan_calibration_repair(db, task, storage=storage)
    if digest_json(current) != digest_json(prepared):
        raise AuxiliaryError("calibration_repair_snapshot_changed")
    from app.schemas._jsonb_types import SensorCalibration

    results = []
    for camera in current["cameras"]:
        state = await update_calibration(
            db,
            dataset_item_id=uuid.UUID(camera["dataset_item_id"]),
            calibration=SensorCalibration.model_validate(camera["calibration"]),
            expected_revision=camera["expected_revision"],
            expected_digest=camera["expected_digest"],
            actor_id=actor_id,
        )
        results.append(
            {
                "role": camera["role"],
                "revision": state.revision,
                "digest": state.digest,
                "changed": state.digest != camera["expected_digest"],
            }
        )
    return {"task_id": str(task.id), "cameras": results}
