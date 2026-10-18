"""Bind point-cloud manifest asset facts to the Assets spatial gateway."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog.spatial_contract import (
    ReadItemsRequest,
    ReadLayoutRequest,
    ReadPosesRequest,
    SpatialFailure,
    SpatialItem,
)
from aap_assets_catalog.spatial_values import FramePose

from app.services.assets_spatial import AssetsSpatialGateway
from app.services.assets_point_cloud_manifest_contract import (
    PointCloudManifestAssetsReader,
    PointCloudManifestCalibrationFact,
    PointCloudManifestItemFact,
    PointCloudManifestPrimaryContextFacts,
)


class SqlAlchemyPointCloudManifestAssetsReader(PointCloudManifestAssetsReader):
    """Read Catalog facts while keeping ORM/provider types behind the gateway."""

    def __init__(self, db: AsyncSession) -> None:
        self._gateway = AssetsSpatialGateway(db)
        self._items: dict[uuid.UUID, SpatialItem] = {}

    async def read_items(
        self, dataset_item_ids: tuple[uuid.UUID, ...]
    ) -> tuple[PointCloudManifestItemFact, ...]:
        if not dataset_item_ids:
            self._items = {}
            return ()
        page = await self._gateway.read_items(
            ReadItemsRequest(item_ids=list(dataset_item_ids))
        )
        self._items = {item.id: item for item in page.items}
        return tuple(
            PointCloudManifestItemFact(
                dataset_item_id=item.id,
                file_path=item.file_path,
                width=item.width,
                height=item.height,
            )
            for item in page.items
        )

    async def read_camera_calibration(
        self, dataset_item_id: uuid.UUID
    ) -> PointCloudManifestCalibrationFact | None:
        if dataset_item_id not in self._items:
            return None
        try:
            state = await self._gateway.read_calibration_state(dataset_item_id)
        except SpatialFailure as exc:
            if exc.code in {"camera_calibration_missing", "camera_calibration_invalid"}:
                return None
            raise
        return PointCloudManifestCalibrationFact(
            calibration=state.calibration,
            revision=state.revision,
            digest=state.digest,
        )

    async def read_primary_context(
        self, primary_item_id: uuid.UUID
    ) -> PointCloudManifestPrimaryContextFacts | None:
        item = self._items.get(primary_item_id)
        if item is None:
            return None
        scene_id = item.scene_id
        scene_name = None
        frame_index = None
        scene_total_frames = None
        ego_pose: FramePose | None = None
        if scene_id is not None:
            layout = await self._gateway.read_layout(
                ReadLayoutRequest(actor_id=None, dataset_id=item.dataset_id)
            )
            scene = next(
                (scene for scene in layout.scenes if scene.id == scene_id), None
            )
            if scene is not None:
                scene_name = scene.name
                frame_index = item.frame_index
                scene_total_frames = len(
                    {
                        row.frame_index
                        for row in layout.items
                        if row.scene_id == scene_id and row.frame_index is not None
                    }
                )
                if frame_index is not None:
                    poses = await self._gateway.read_poses(
                        ReadPosesRequest(
                            actor_id=None,
                            scene_id=scene_id,
                            after_frame_index=frame_index - 1,
                            page_size=1,
                        )
                    )
                    if poses and poses[0].frame_index == frame_index:
                        ego_pose = poses[0]

        return PointCloudManifestPrimaryContextFacts(
            axis_convention=item.axis_convention,
            scene_id=scene_id,
            scene_name=scene_name,
            frame_index=frame_index,
            scene_total_frames=scene_total_frames,
            ego_pose=ego_pose,
        )
