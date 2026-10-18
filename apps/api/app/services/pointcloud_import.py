"""Compose accepted Assets facts into Labeling task-creation frame inputs."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.frame_layout import (
    PreparedDatasetItem,
    PreparedPointCloudFrame,
    group_frames,
    source_ingest_logical_path,
)
from aap_assets_catalog.spatial_contract import ReadPreparationRequest

from app.services.assets_spatial import AssetsSpatialGateway

logger = logging.getLogger(__name__)


async def prepare_pointcloud_frames(
    db: AsyncSession,
    *,
    creation_operation_id: uuid.UUID,
    same_session_native: bool = False,
) -> tuple[PreparedPointCloudFrame, ...]:
    """Read the accepted layout snapshot and group frames for Labeling."""
    layout = await AssetsSpatialGateway(db).read_layout_for_operation(
        ReadPreparationRequest(creation_operation_id=creation_operation_id),
        same_session_native=same_session_native,
    )
    items = tuple(
        PreparedDatasetItem(
            id=item.id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type=item.file_type,
            scene_id=item.scene_id,
            frame_index=item.frame_index,
            logical_path=source_ingest_logical_path(item.metadata),
        )
        for item in layout.items
    )
    buckets: dict[uuid.UUID | None, list[PreparedDatasetItem]] = {}
    for item in items:
        buckets.setdefault(item.scene_id, []).append(item)

    frames = {}
    for scene_id, scene_items in buckets.items():
        scene_frames, _ = group_frames(scene_items)
        for frame_id, frame in scene_frames.items():
            frames[(scene_id, frame_id)] = frame

    prepared: list[PreparedPointCloudFrame] = []
    for frame_key in sorted(frames, key=lambda key: (str(key[0]), key[1])):
        frame = frames[frame_key]
        if frame.lidar is None:
            logger.warning(
                "pointcloud frame %r has no lidar item, skipped (operation=%s)",
                frame_key,
                creation_operation_id,
            )
            continue
        prepared.append(
            PreparedPointCloudFrame(
                scene_id=frame_key[0],
                frame_id=frame_key[1],
                lidar=frame.lidar,
                cameras=frame.cameras,
            )
        )
    return tuple(prepared)
