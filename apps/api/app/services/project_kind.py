from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.services.assets_spatial import AssetsSpatialGateway


@dataclass(frozen=True)
class ProjectKind:
    data_type: str
    scene_mode: bool


@dataclass(frozen=True)
class DatasetKind:
    data_type: str
    has_scenes: bool


_LIDAR_DATA_TYPES = ("lidar", "point_cloud", "pointcloud")


def canonical_media_kind(data_type: str | None) -> str:
    if data_type in _LIDAR_DATA_TYPES:
        return "lidar"
    return data_type or "image"


def media_kind_aliases(data_type: str | None) -> tuple[str, ...]:
    """返回与 data_type 同属一个 media kind 的所有原始 data_type 取值。

    用于按 media kind 过滤数据集,使 picker 过滤与 link 硬门(canonical_media_kind)
    口径一致 —— 传入 "lidar" 或 "point_cloud" 都匹配整个 lidar 家族,避免存成
    "lidar" 的数据集被 picker 漏掉却能被 link 门放行的裂缝。
    """
    if canonical_media_kind(data_type) == "lidar":
        return _LIDAR_DATA_TYPES
    return (canonical_media_kind(data_type),)


def project_kind(project: Project) -> ProjectKind:
    return ProjectKind(
        data_type=canonical_media_kind(project.data_type),
        scene_mode=bool(project.scene_mode),
    )


def dataset_kind(dataset: Dataset, *, has_scenes: bool) -> DatasetKind:
    return DatasetKind(
        data_type=canonical_media_kind(dataset.data_type),
        has_scenes=has_scenes,
    )


def kind_matches(project: ProjectKind, dataset: DatasetKind) -> bool:
    return (
        project.data_type == dataset.data_type
        and project.scene_mode == dataset.has_scenes
    )


def scene_mode_allowed(data_type: str | None) -> bool:
    return canonical_media_kind(data_type) in {"image", "lidar"}


def kind_mismatch_detail(project: ProjectKind, dataset: DatasetKind) -> str | None:
    if project.data_type != dataset.data_type:
        return (
            f"data_type 不匹配(项目 {project.data_type} / 数据集 {dataset.data_type})"
        )
    if project.scene_mode != dataset.has_scenes:
        if project.scene_mode:
            return "scene 模式不匹配(scene 项目只能关联 has_scenes=true 的数据集)"
        return "scene 模式不匹配(普通项目只能关联 has_scenes=false 的数据集)"
    return None


async def dataset_has_scenes(db: AsyncSession, dataset_id: uuid.UUID) -> bool:
    return await AssetsSpatialGateway(db).has_scenes(dataset_id)
