"""Dataset-level LiDAR axis convention sniffing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog.axis_sniff import (
    AxisObservation,
    AxisSniffSource,
    sniff_axis_convention,
)
from aap_assets_catalog.spatial_values import SensorCalibration

from app.db.models.dataset import DatasetItem
from app.db.models.task_dataset_item_link import TaskDatasetItemLink


@dataclass(frozen=True)
class AxisSniffResult:
    best: str
    score: float
    candidates: list[dict]
    source: AxisSniffSource
    camera_role: str | None
    camera_item_id: uuid.UUID
    per_camera: list[dict]
    agreement: float


def _camera_name_from_path(item: DatasetItem) -> str | None:
    parts = PurePosixPath(item.file_path or item.file_name).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "camera" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _calibration_for(item: DatasetItem) -> SensorCalibration | None:
    raw = (item.metadata_ or {}).get("calibration")
    if not raw:
        return None
    try:
        return SensorCalibration.model_validate(raw)
    except Exception:
        return None


class AxisSnifferService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def sniff_dataset(self, dataset_id: uuid.UUID) -> AxisSniffResult | None:
        observations = await self._linked_camera_observations(dataset_id)
        if not observations:
            observations = await self._dataset_item_observations(dataset_id)
        result = sniff_axis_convention(tuple(observations))
        if result is None:
            return None
        return AxisSniffResult(
            best=result.best,
            score=result.score,
            candidates=[
                {"convention": candidate.convention, "score": candidate.score}
                for candidate in result.candidates
            ],
            source=result.source,
            camera_role=result.camera_role,
            camera_item_id=result.camera_item_id,
            per_camera=[
                {
                    "camera_role": camera.camera_role,
                    "best": camera.best,
                    "score": camera.score,
                }
                for camera in result.per_camera
            ],
            agreement=result.agreement,
        )

    async def _linked_camera_observations(
        self,
        dataset_id: uuid.UUID,
    ) -> list[AxisObservation]:
        rows = (
            await self.db.execute(
                select(TaskDatasetItemLink, DatasetItem)
                .join(
                    DatasetItem,
                    DatasetItem.id == TaskDatasetItemLink.dataset_item_id,
                )
                .where(DatasetItem.dataset_id == dataset_id)
                .where(TaskDatasetItemLink.role.startswith("camera_"))
            )
        ).all()
        return [
            _observation(item, source="task_link", camera_role=link.role)
            for link, item in rows
        ]

    async def _dataset_item_observations(
        self,
        dataset_id: uuid.UUID,
    ) -> list[AxisObservation]:
        # 仅看相机图（file_type=="image"）。点云项可能在 metadata 里带 lidar→ego
        # 外参，若混进来会被 sniff_convention_from_forward 当成相机光轴污染推断。
        # 与 _linked_camera_observations 仅取 role.startswith("camera_") 对齐。
        items = (
            (
                await self.db.execute(
                    select(DatasetItem)
                    .where(DatasetItem.dataset_id == dataset_id)
                    .where(DatasetItem.file_type == "image")
                    .order_by(DatasetItem.created_at)
                )
            )
            .scalars()
            .all()
        )
        out: list[AxisObservation] = []
        for item in items:
            observation = _observation(
                item,
                source="dataset_item",
                camera_role=(
                    f"camera_{name}" if (name := _camera_name_from_path(item)) else None
                ),
            )
            if observation.calibration is None:
                continue
            out.append(observation)
        return out


def _observation(
    item: DatasetItem,
    *,
    source: AxisSniffSource,
    camera_role: str | None,
) -> AxisObservation:
    return AxisObservation(
        item_id=item.id,
        source=source,
        camera_role=camera_role,
        file_name=item.file_name or "",
        file_path=item.file_path or "",
        created_at=item.created_at,
        calibration=_calibration_for(item),
    )
