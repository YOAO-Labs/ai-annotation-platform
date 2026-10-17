"""Compose a task's point-cloud manifest from labeling and assets facts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from app.schemas.task import PointCloudCameraOut, TaskPointCloudManifestResponse
from app.services.assets_point_cloud_manifest_contract import (
    PointCloudManifestAssetsReader,
)
from app.services.labeling_point_cloud_manifest_contract import (
    TaskDatasetItemLinksReader,
)

logger = logging.getLogger(__name__)


class PointCloudManifestFileUnavailable(Exception):
    """The task has no linked primary point cloud or fallback file path."""


async def build_point_cloud_manifest(
    task_id: UUID,
    task_file_path: str | None,
    *,
    link_reader: TaskDatasetItemLinksReader,
    assets_reader: PointCloudManifestAssetsReader,
    presign: Callable[[str], str],
    expires_in: int,
) -> TaskPointCloudManifestResponse:
    """Read only manifest facts and preserve existing link/signing order."""
    links = await link_reader.read_task_dataset_item_links(task_id)
    items = await assets_reader.read_items(
        tuple(link.dataset_item_id for link in links)
    )
    items_by_id = {item.dataset_item_id: item for item in items}

    point_cloud_url: str | None = None
    primary_link = next((link for link in links if link.role == "primary_lidar"), None)
    primary_item = (
        items_by_id.get(primary_link.dataset_item_id)
        if primary_link is not None
        else None
    )
    if primary_item is not None:
        point_cloud_url = presign(primary_item.file_path)
    if point_cloud_url is None:
        if not task_file_path:
            raise PointCloudManifestFileUnavailable("Point cloud file not available")
        point_cloud_url = presign(task_file_path)

    cameras: list[PointCloudCameraOut] = []
    for link in links:
        if not link.role.startswith("camera_"):
            continue
        item = items_by_id.get(link.dataset_item_id)
        if item is None:
            continue
        calibration = await assets_reader.read_camera_calibration(item.dataset_item_id)
        if calibration is None:
            logger.warning(
                "task %s %s: stored calibration unavailable, returning null",
                task_id,
                link.role,
            )
        cameras.append(
            PointCloudCameraOut(
                name=link.role[len("camera_") :],
                role=link.role,
                dataset_item_id=item.dataset_item_id,
                image_url=presign(item.file_path),
                width=item.width,
                height=item.height,
                calibration=calibration.calibration if calibration else None,
                calibration_revision=calibration.revision if calibration else None,
                calibration_digest=calibration.digest if calibration else None,
            )
        )

    cameras.sort(key=lambda camera: camera.name)

    primary_context = (
        await assets_reader.read_primary_context(primary_item.dataset_item_id)
        if primary_item is not None
        else None
    )

    return TaskPointCloudManifestResponse(
        task_id=task_id,
        point_cloud_url=point_cloud_url,
        cameras=cameras,
        expires_in=expires_in,
        axis_convention=(
            primary_context.axis_convention if primary_context is not None else None
        ),
        scene_id=primary_context.scene_id if primary_context is not None else None,
        scene_name=primary_context.scene_name if primary_context is not None else None,
        frame_index=(
            primary_context.frame_index if primary_context is not None else None
        ),
        scene_total_frames=(
            primary_context.scene_total_frames if primary_context is not None else None
        ),
        ego_pose=primary_context.ego_pose if primary_context is not None else None,
    )
