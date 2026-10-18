"""Task and annotation compositions for scene-aware Labeling features."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.scene import NeighborInfo, NeighborsResponse
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.assets_spatial_views import spatial_item_references
from aap_assets_catalog.spatial_contract import (
    GetSceneRequest,
    ReadItemsRequest,
    ReadLayoutRequest,
    SpatialItem,
    SpatialScene,
)


_PRIMARY_LIDAR_ROLE = "primary_lidar"
_RESOLVE_CHUNK_SIZE = 5000


@dataclass(frozen=True)
class TaskSceneFrame:
    scene_id: uuid.UUID | None
    scene_name: str | None
    frame_index: int | None


@dataclass(frozen=True)
class SceneTimelineTask:
    task_id: uuid.UUID
    status: str


@dataclass(frozen=True)
class SceneTimelineWindow:
    scene_id: uuid.UUID
    scene_name: str
    current_frame_index: int
    scene_start_frame: int
    scene_end_frame: int
    populated_frame_count: int
    frame_tasks: dict[int, SceneTimelineTask]


class SceneFrameIndexInconsistent(ValueError):
    """A Labeling task points at a Scene item without a frame index."""


@dataclass(frozen=True)
class SceneTimelineAnnotationSummary:
    annotation_count: int
    selected_annotation_id: uuid.UUID | None = None
    selected_source: str | None = None
    selected_class_name: str | None = None
    selected_temporal_role: str | None = None


async def resolve_primary_item_id(db: AsyncSession, task: Task) -> uuid.UUID | None:
    """Resolve the primary item from Labeling-owned task and link rows."""
    if task.dataset_item_id is not None:
        return task.dataset_item_id
    link_row = await db.execute(
        select(TaskDatasetItemLink.dataset_item_id)
        .where(TaskDatasetItemLink.task_id == task.id)
        .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
        .limit(1)
    )
    return link_row.scalar_one_or_none()


async def get_scene_timeline_annotation_summaries(
    db: AsyncSession,
    *,
    task_ids: set[uuid.UUID],
    track_id: str | None,
) -> dict[uuid.UUID, SceneTimelineAnnotationSummary]:
    """Batch visible-task annotation summaries for the scene timeline."""
    if not task_ids:
        return {}

    active_3d = (
        Annotation.task_id.in_(task_ids),
        Annotation.annotation_type.in_(("box_3d", "point_mask_3d")),
        Annotation.is_active.is_(True),
        Annotation.was_cancelled.is_(False),
    )
    count_rows = (
        await db.execute(
            select(Annotation.task_id, func.count(Annotation.id))
            .where(*active_3d)
            .group_by(Annotation.task_id)
        )
    ).all()
    summaries = {
        task_id: SceneTimelineAnnotationSummary(annotation_count=count)
        for task_id, count in count_rows
    }

    if track_id:
        track_rows = (
            await db.execute(
                select(
                    Annotation.task_id,
                    Annotation.id,
                    Annotation.source,
                    Annotation.class_name,
                    Annotation.temporal_role,
                )
                .where(*active_3d)
                .where(Annotation.track_id == track_id)
                .order_by(Annotation.task_id, Annotation.created_at, Annotation.id)
            )
        ).all()
        selected_by_task: dict[uuid.UUID, tuple[uuid.UUID, str, str, str]] = {}
        for task_id, annotation_id, source, class_name, temporal_role in track_rows:
            selected_by_task.setdefault(
                task_id, (annotation_id, source, class_name, temporal_role)
            )
        for task_id, selected in selected_by_task.items():
            current = summaries.get(
                task_id, SceneTimelineAnnotationSummary(annotation_count=0)
            )
            summaries[task_id] = SceneTimelineAnnotationSummary(
                annotation_count=current.annotation_count,
                selected_annotation_id=selected[0],
                selected_source=selected[1],
                selected_class_name=selected[2],
                selected_temporal_role=selected[3],
            )
    return summaries


async def read_spatial_item_facts(
    db: AsyncSession,
    item_ids: list[uuid.UUID],
    *,
    same_session_native: bool = False,
) -> tuple[dict[uuid.UUID, SpatialItem], dict[uuid.UUID, SpatialScene]]:
    gateway = AssetsSpatialGateway(db)
    items: dict[uuid.UUID, SpatialItem] = {}
    scenes: dict[uuid.UUID, SpatialScene] = {}
    unique_ids = list(dict.fromkeys(item_ids))
    for start in range(0, len(unique_ids), _RESOLVE_CHUNK_SIZE):
        page = await gateway.read_items(
            ReadItemsRequest(item_ids=unique_ids[start : start + _RESOLVE_CHUNK_SIZE]),
            same_session_native=same_session_native,
        )
        items.update((item.id, item) for item in page.items)
        scenes.update((scene.id, scene) for scene in page.scenes)
    return items, scenes


async def resolve_task_scene_frames(
    db: AsyncSession, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, TaskSceneFrame]:
    """Compose Task and camera-link rows with owner-returned Scene item facts."""
    if not task_ids:
        return {}
    out = {
        task_id: TaskSceneFrame(scene_id=None, scene_name=None, frame_index=None)
        for task_id in task_ids
    }
    direct: dict[uuid.UUID, uuid.UUID] = {}
    linked: dict[uuid.UUID, uuid.UUID] = {}
    for start in range(0, len(task_ids), _RESOLVE_CHUNK_SIZE):
        chunk = task_ids[start : start + _RESOLVE_CHUNK_SIZE]
        direct_rows = await db.execute(
            select(Task.id, Task.dataset_item_id)
            .where(Task.id.in_(chunk))
            .where(Task.dataset_item_id.is_not(None))
        )
        direct.update((task_id, item_id) for task_id, item_id in direct_rows.all())
        linked_rows = await db.execute(
            select(TaskDatasetItemLink.task_id, TaskDatasetItemLink.dataset_item_id)
            .where(TaskDatasetItemLink.task_id.in_(chunk))
            .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
        )
        for task_id, item_id in linked_rows.all():
            linked.setdefault(task_id, item_id)

    item_facts, scene_facts = await read_spatial_item_facts(
        db, [*direct.values(), *linked.values()]
    )
    for task_id, item_id in direct.items():
        item = item_facts.get(item_id)
        if item is not None and item.scene_id is not None:
            scene = scene_facts.get(item.scene_id)
            out[task_id] = TaskSceneFrame(
                scene_id=item.scene_id,
                scene_name=scene.name if scene else None,
                frame_index=item.frame_index,
            )
    for task_id, item_id in linked.items():
        if out[task_id].scene_id is not None:
            continue
        item = item_facts.get(item_id)
        if item is not None and item.scene_id is not None:
            scene = scene_facts.get(item.scene_id)
            out[task_id] = TaskSceneFrame(
                scene_id=item.scene_id,
                scene_name=scene.name if scene else None,
                frame_index=item.frame_index,
            )
    return out


async def get_scene_frame_task_map(
    db: AsyncSession, scene_id: uuid.UUID
) -> dict[int, uuid.UUID]:
    """Map populated scene frames to their Labeling tasks, preserving link priority."""
    facts = spatial_item_references.c
    item_rows = (
        await db.execute(
            select(facts.item_id, facts.frame_index)
            .where(facts.scene_id == scene_id)
            .where(facts.frame_index.is_not(None))
            .order_by(facts.frame_index)
        )
    ).all()
    if not item_rows:
        return {}
    direct_rows = (
        await db.execute(
            select(Task.id, Task.dataset_item_id)
            .join(spatial_item_references, facts.item_id == Task.dataset_item_id)
            .where(facts.scene_id == scene_id)
        )
    ).all()
    item_to_task = {item_id: task_id for task_id, item_id in direct_rows}
    link_rows = (
        await db.execute(
            select(TaskDatasetItemLink.dataset_item_id, TaskDatasetItemLink.task_id)
            .join(
                spatial_item_references,
                facts.item_id == TaskDatasetItemLink.dataset_item_id,
            )
            .where(facts.scene_id == scene_id)
            .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
        )
    ).all()
    for item_id, task_id in link_rows:
        item_to_task.setdefault(item_id, task_id)
    frame_to_task: dict[int, uuid.UUID] = {}
    for item_id, frame_index in item_rows:
        task_id = item_to_task.get(item_id)
        if task_id is not None:
            frame_to_task.setdefault(frame_index, task_id)
    return frame_to_task


async def get_scene_timeline_window(
    db: AsyncSession,
    *,
    task: Task,
    start_frame: int,
    end_frame: int,
) -> SceneTimelineWindow | None:
    primary_item_id = await resolve_primary_item_id(db, task)
    if primary_item_id is None:
        return None
    item_facts, _ = await read_spatial_item_facts(db, [primary_item_id])
    primary_item = item_facts.get(primary_item_id)
    if (
        primary_item is None
        or primary_item.scene_id is None
        or primary_item.frame_index is None
    ):
        return None
    scene_result = await AssetsSpatialGateway(db).get_scene(
        GetSceneRequest(actor_id=None, scene_id=primary_item.scene_id)
    )
    scene = scene_result.scene
    layout = await AssetsSpatialGateway(db).read_layout(
        ReadLayoutRequest(actor_id=None, dataset_id=scene.dataset_id)
    )
    frame_indexes = sorted(
        {
            item.frame_index
            for item in layout.items
            if item.scene_id == scene.id and item.frame_index is not None
        }
    )
    if not frame_indexes:
        return None
    scene_items = [
        item
        for item in layout.items
        if item.scene_id == scene.id
        and item.frame_index is not None
        and start_frame <= item.frame_index <= end_frame
    ]
    item_ids = list(dict.fromkeys(item.id for item in scene_items))
    frame_by_item = {item.id: item.frame_index for item in scene_items}
    frame_tasks: dict[int, SceneTimelineTask] = {}
    for offset in range(0, len(item_ids), _RESOLVE_CHUNK_SIZE):
        chunk = item_ids[offset : offset + _RESOLVE_CHUNK_SIZE]
        direct_rows = await db.execute(
            select(Task.dataset_item_id, Task.id, Task.status)
            .where(Task.project_id == task.project_id)
            .where(Task.dataset_item_id.in_(chunk))
            .order_by(Task.dataset_item_id, Task.id)
        )
        for item_id, task_id, status in direct_rows.all():
            fi = frame_by_item[item_id]
            frame_tasks.setdefault(
                fi, SceneTimelineTask(task_id=task_id, status=status)
            )
        link_rows = await db.execute(
            select(TaskDatasetItemLink.dataset_item_id, Task.id, Task.status)
            .join(Task, Task.id == TaskDatasetItemLink.task_id)
            .where(Task.project_id == task.project_id)
            .where(TaskDatasetItemLink.dataset_item_id.in_(chunk))
            .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
            .order_by(TaskDatasetItemLink.dataset_item_id, Task.id)
        )
        for item_id, task_id, status in link_rows.all():
            fi = frame_by_item[item_id]
            frame_tasks.setdefault(
                fi, SceneTimelineTask(task_id=task_id, status=status)
            )
    return SceneTimelineWindow(
        scene_id=scene.id,
        scene_name=scene.name,
        current_frame_index=primary_item.frame_index,
        scene_start_frame=frame_indexes[0],
        scene_end_frame=frame_indexes[-1],
        populated_frame_count=len(frame_indexes),
        frame_tasks=frame_tasks,
    )


async def get_neighbors_for_task(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    k: int = 1,
) -> NeighborsResponse | None:
    task = await db.get(Task, task_id)
    if task is None:
        return None
    primary_item_id = await resolve_primary_item_id(db, task)
    if primary_item_id is None:
        return None
    item_facts, _ = await read_spatial_item_facts(db, [primary_item_id])
    item = item_facts.get(primary_item_id)
    if item is None or item.scene_id is None:
        return None
    if item.frame_index is None:
        raise SceneFrameIndexInconsistent(
            f"dataset_item {item.id} has scene_id but frame_index NULL"
        )
    scene_result = await AssetsSpatialGateway(db).get_scene(
        GetSceneRequest(actor_id=None, scene_id=item.scene_id)
    )
    frame_to_task = await get_scene_frame_task_map(db, item.scene_id)
    if not frame_to_task:
        return None
    frames = sorted(frame_to_task)
    try:
        current_index = frames.index(item.frame_index)
    except ValueError:
        return NeighborsResponse(
            scene_id=item.scene_id,
            scene_name=scene_result.scene.name,
            frame_index=item.frame_index,
            scene_total_frames=len(frames),
            prev=[],
            next=[],
        )
    previous = frames[max(0, current_index - k) : current_index][::-1]
    following = frames[current_index + 1 : current_index + 1 + k]
    return NeighborsResponse(
        scene_id=item.scene_id,
        scene_name=scene_result.scene.name,
        frame_index=item.frame_index,
        scene_total_frames=len(frames),
        prev=[
            NeighborInfo(task_id=frame_to_task[fi], frame_index=fi) for fi in previous
        ],
        next=[
            NeighborInfo(task_id=frame_to_task[fi], frame_index=fi) for fi in following
        ],
    )
