"""Task and annotation compositions for scene-aware Labeling features."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from app.schemas.scene import NeighborInfo, NeighborsResponse
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.labeling_scene_read import read_scene_data
from aap_assets_catalog.spatial_contract import (
    GetSceneRequest,
    ReadItemsRequest,
    ReadLayoutRequest,
    SpatialItem,
    SpatialScene,
)


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
    """Resolve the primary item through Labeling's fixed scene read."""
    if task.dataset_item_id is not None:
        return task.dataset_item_id
    result = await read_scene_data(db, "resolve_primary_link", task_id=task.id)
    return result.primary_item_id


async def get_scene_timeline_annotation_summaries(
    db: AsyncSession,
    *,
    task_ids: set[uuid.UUID],
    track_id: str | None,
) -> dict[uuid.UUID, SceneTimelineAnnotationSummary]:
    """Batch visible-task annotation summaries for the scene timeline."""
    if not task_ids:
        return {}

    result = await read_scene_data(
        db,
        "timeline_annotation_summaries",
        task_ids=list(task_ids),
        track_id=track_id,
    )
    return {
        row.task_id: SceneTimelineAnnotationSummary(
            annotation_count=row.annotation_count,
            selected_annotation_id=row.selected_annotation_id,
            selected_source=row.selected_source,
            selected_class_name=row.selected_class_name,
            selected_temporal_role=row.selected_temporal_role,
        )
        for row in result.annotation_summaries
    }


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
    resolved: dict[uuid.UUID, TaskSceneFrame] = {}
    for start in range(0, len(task_ids), _RESOLVE_CHUNK_SIZE):
        result = await read_scene_data(
            db,
            "resolve_task_frames",
            task_ids=task_ids[start : start + _RESOLVE_CHUNK_SIZE],
        )
        resolved.update(
            (
                row.task_id,
                TaskSceneFrame(
                    scene_id=row.scene_id,
                    scene_name=row.scene_name,
                    frame_index=row.frame_index,
                ),
            )
            for row in result.frames
        )
    return resolved


async def get_scene_frame_task_map(
    db: AsyncSession, scene_id: uuid.UUID
) -> dict[int, uuid.UUID]:
    """Map populated scene frames to their Labeling tasks, preserving link priority."""
    result = await read_scene_data(db, "scene_frame_tasks", scene_id=scene_id)
    return {row.frame_index: row.task_id for row in result.frame_tasks}


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
        tasks_result = await read_scene_data(
            db,
            "timeline_tasks",
            project_id=task.project_id,
            item_ids=item_ids[offset : offset + _RESOLVE_CHUNK_SIZE],
        )
        for row in tasks_result.timeline_tasks:
            fi = frame_by_item[row.item_id]
            frame_tasks.setdefault(
                fi, SceneTimelineTask(task_id=row.task_id, status=row.status)
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
