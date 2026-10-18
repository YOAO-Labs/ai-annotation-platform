"""Pure media facts consumed by the point-cloud task manifest composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from aap_assets_catalog.spatial_values import (
    FramePose,
    LidarAxisConvention,
    SensorCalibration,
)


@dataclass(frozen=True, slots=True)
class PointCloudManifestItemFact:
    dataset_item_id: UUID
    file_path: str
    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class PointCloudManifestCalibrationFact:
    calibration: SensorCalibration
    revision: int
    digest: str


@dataclass(frozen=True, slots=True)
class PointCloudManifestPrimaryContextFacts:
    axis_convention: LidarAxisConvention | None
    scene_id: UUID | None
    scene_name: str | None
    frame_index: int | None
    scene_total_frames: int | None
    ego_pose: FramePose | None


class PointCloudManifestAssetsReader(Protocol):
    """Read facts for one manifest in the caller's existing transaction.

    ``read_items`` establishes the item set for subsequent calibration/context
    reads; missing IDs are omitted and an empty request returns no items. These
    reads do not grant access or freeze facts across a transaction boundary.
    """

    async def read_items(
        self, dataset_item_ids: tuple[UUID, ...]
    ) -> tuple[PointCloudManifestItemFact, ...]:
        """Return existing item facts in the query's native order."""
        ...

    async def read_camera_calibration(
        self, dataset_item_id: UUID
    ) -> PointCloudManifestCalibrationFact | None:
        """Return None for unavailable legacy calibration; bad revisions raise."""
        ...

    async def read_primary_context(
        self, primary_item_id: UUID
    ) -> PointCloudManifestPrimaryContextFacts | None:
        """Read axis, scene, frame-count and pose facts for one primary item."""
        ...
