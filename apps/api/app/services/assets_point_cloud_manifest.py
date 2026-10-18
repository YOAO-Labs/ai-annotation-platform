"""SQL adapter for Assets facts used by the point-cloud manifest."""

from __future__ import annotations

import uuid
from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, DatasetItem, Scene
from aap_assets_catalog.spatial_values import FramePose
from app.services import scene_pose
from app.services.assets_point_cloud_manifest_contract import (
    PointCloudManifestAssetsReader,
    PointCloudManifestCalibrationFact,
    PointCloudManifestItemFact,
    PointCloudManifestPrimaryContextFacts,
)
from app.services.sensor_calibration import (
    SensorCalibrationError,
    resolve_calibration_state,
)


class SqlAlchemyPointCloudManifestAssetsReader(PointCloudManifestAssetsReader):
    """Bind manifest asset reads to the route's existing AsyncSession.

    Keep loaded DatasetItem rows private: current calibration resolution consumes
    its ORM item, while the application boundary receives only scalar facts.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._items: dict[uuid.UUID, DatasetItem] = {}

    async def read_items(
        self, dataset_item_ids: tuple[uuid.UUID, ...]
    ) -> tuple[PointCloudManifestItemFact, ...]:
        if not dataset_item_ids:
            self._items = {}
            return ()
        rows = (
            (
                await self._db.execute(
                    select(DatasetItem).where(DatasetItem.id.in_(dataset_item_ids))
                )
            )
            .scalars()
            .all()
        )
        self._items = {item.id: item for item in rows}
        return tuple(
            PointCloudManifestItemFact(
                dataset_item_id=item.id,
                file_path=item.file_path,
                width=item.width,
                height=item.height,
            )
            for item in rows
        )

    async def read_camera_calibration(
        self, dataset_item_id: uuid.UUID
    ) -> PointCloudManifestCalibrationFact | None:
        item = self._items.get(dataset_item_id)
        if item is None:
            return None
        try:
            state = await resolve_calibration_state(self._db, item)
        except SensorCalibrationError:
            return None
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

        axis_convention = None
        dataset = await self._db.get(Dataset, item.dataset_id)
        if dataset is not None:
            axis_convention = (dataset.metadata_ or {}).get("axis_convention")

        scene_id_out = None
        scene_name_out = None
        frame_index_out = None
        scene_total_frames_out = None
        ego_pose_out: FramePose | None = None
        if item.scene_id is not None:
            scene = await self._db.get(Scene, item.scene_id)
            if scene is not None:
                scene_id_out = scene.id
                scene_name_out = scene.name
                frame_index_out = item.frame_index
                total_row = await self._db.execute(
                    select(func.count(func.distinct(DatasetItem.frame_index)))
                    .where(DatasetItem.scene_id == scene.id)
                    .where(DatasetItem.frame_index.is_not(None))
                )
                scene_total_frames_out = total_row.scalar() or 0
                if item.frame_index is not None:
                    ego_pose_out = await scene_pose.get_frame_pose(
                        self._db,
                        scene_id=scene.id,
                        frame_index=item.frame_index,
                    )
                    if ego_pose_out is not None:
                        ego_pose_out.source_metadata = deepcopy(
                            ego_pose_out.source_metadata
                        )

        return PointCloudManifestPrimaryContextFacts(
            axis_convention=axis_convention,
            scene_id=scene_id_out,
            scene_name=scene_name_out,
            frame_index=frame_index_out,
            scene_total_frames=scene_total_frames_out,
            ego_pose=ego_pose_out,
        )
