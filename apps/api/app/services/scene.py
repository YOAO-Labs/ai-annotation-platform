"""v0.14.0 · Scene service

跨 task 帧序列地基的 service 层:
- create_scene / assign_items_to_scene / list_for_dataset:CRUD
- get_neighbors_for_task:给定 task 找其所在 scene 内的前后 k 个邻居 task

设计要点:
- task 与 dataset_item 有两种关联(单文件 task.dataset_item_id / 点云 link role=primary_lidar);
  neighbors 查询双路径反查,优先 task.dataset_item_id。
- "scene 内主帧 task" = scene_id=X AND frame_index NOT NULL 的 dataset_items 反查到的 task;
  多模态同帧 cam item 也带 frame_index,但 task 唯一(同帧只产 1 个 task)。
- 严格不动 scheduler.get_next_task,本期只读不调度。
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem, Scene
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.scene import NeighborInfo, NeighborsResponse
from app.services.display_id import next_display_id


_PRIMARY_LIDAR_ROLE = "primary_lidar"


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


@dataclass(frozen=True)
class SceneTimelineAnnotationSummary:
    annotation_count: int
    selected_annotation_id: uuid.UUID | None = None
    selected_source: str | None = None
    selected_class_name: str | None = None
    selected_temporal_role: str | None = None


class SceneNameConflict(ValueError):
    """同 dataset 下 scene name 已存在;API 层包成 422。"""


class SceneFrameIndexInconsistent(ValueError):
    """dataset_item 有 scene_id 但 frame_index=NULL;数据状态异常,API 层包成 409。"""


async def create_scene(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    name: str,
    source_format: str | None = None,
    source_metadata: dict | None = None,
    created_by: uuid.UUID | None = None,
) -> Scene:
    """新建 scene。同 dataset 下 name 重复 → SceneNameConflict。"""
    from app.services.dataset_lifecycle import require_writable

    await require_writable(db, dataset_id, lock=True)
    display_id = await next_display_id(db, "scenes")
    scene = Scene(
        id=uuid.uuid4(),
        display_id=display_id,
        dataset_id=dataset_id,
        name=name,
        source_format=source_format,
        source_metadata=source_metadata or {},
        created_by=created_by,
    )
    db.add(scene)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise SceneNameConflict(
            f"scene name {name!r} already exists in dataset {dataset_id}"
        ) from exc
    return scene


async def assign_items_to_scene(
    db: AsyncSession,
    *,
    scene_id: uuid.UUID,
    items_in_order: list[DatasetItem],
    shared_frame_items: dict[uuid.UUID, int] | None = None,
    scene_level_items: Iterable[DatasetItem] | None = None,
) -> int:
    """把 items 挂到 scene 上并写 frame_index。

    - items_in_order:已按帧序排好的"主"item(如 lidar pcd 帧),
      frame_index 自动取 0..N-1。
    - shared_frame_items:`item.id → frame_index` 映射;用于把同帧 cam jpg
      也挂上该 frame_index(主帧 lidar 与 cam 共享)。
    - scene_level_items:仅写 scene_id,frame_index 保持 NULL(典型:calib)。

    返回 update 的行数。本函数 *不* 跳过已有 scene_id 的 item——调用方
    应在外部决定是否覆盖。
    """
    from app.services.dataset_lifecycle import require_writable

    scene = await db.get(Scene, scene_id, populate_existing=True)
    if scene is not None:
        await require_writable(db, scene.dataset_id, lock=True)
    updated = 0
    for idx, item in enumerate(items_in_order):
        item.scene_id = scene_id
        item.frame_index = idx
        updated += 1

    if shared_frame_items:
        for item_id, fi in shared_frame_items.items():
            item = await db.get(DatasetItem, item_id)
            if item is None:
                continue
            item.scene_id = scene_id
            item.frame_index = fi
            updated += 1

    if scene_level_items:
        for item in scene_level_items:
            item.scene_id = scene_id
            item.frame_index = None
            updated += 1

    await db.flush()
    return updated


async def list_for_dataset(db: AsyncSession, dataset_id: uuid.UUID) -> list[Scene]:
    rows = await db.execute(
        select(Scene).where(Scene.dataset_id == dataset_id).order_by(Scene.created_at)
    )
    return list(rows.scalars().all())


async def get_scene(db: AsyncSession, scene_id: uuid.UUID) -> Scene | None:
    return await db.get(Scene, scene_id)


async def resolve_primary_item_id(db: AsyncSession, task: Task) -> uuid.UUID | None:
    """task 关联的"主"dataset_item:
    优先 task.dataset_item_id(2D 单文件路径),否则查 primary_lidar link(3D)。

    公开 API:scheduler / annotation 等跨模块消费方依赖它解析 task 的主 item
    (进而反查 scene),故不加下划线前缀。
    """
    if task.dataset_item_id is not None:
        return task.dataset_item_id
    link_row = await db.execute(
        select(TaskDatasetItemLink.dataset_item_id)
        .where(TaskDatasetItemLink.task_id == task.id)
        .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
        .limit(1)
    )
    return link_row.scalar_one_or_none()


_RESOLVE_CHUNK_SIZE = 5000


async def resolve_task_scene_frames(
    db: AsyncSession, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, TaskSceneFrame]:
    """批量反查 task 的 scene/frame，用于 scheduler/batch 等跨帧逻辑。

    支持两条主 item 关联路径:Task.dataset_item_id 与
    TaskDatasetItemLink(role=primary_lidar)。返回值覆盖所有输入 task_id；
    找不到 scene 的 task 以 None 字段表示。
    """
    if not task_ids:
        return {}

    out = {
        task_id: TaskSceneFrame(scene_id=None, scene_name=None, frame_index=None)
        for task_id in task_ids
    }

    # 按 chunk 反查,避免一次性把全量 task_id 灌进 IN(...) 撞 asyncpg 绑定参数上限
    # (~32767);大数据集 scene 项目分包会传入全量待分包 task。
    for start in range(0, len(task_ids), _RESOLVE_CHUNK_SIZE):
        chunk = task_ids[start : start + _RESOLVE_CHUNK_SIZE]

        direct_rows = (
            await db.execute(
                select(
                    Task.id,
                    DatasetItem.scene_id,
                    Scene.name,
                    DatasetItem.frame_index,
                )
                .join(DatasetItem, Task.dataset_item_id == DatasetItem.id)
                .outerjoin(Scene, DatasetItem.scene_id == Scene.id)
                .where(Task.id.in_(chunk))
            )
        ).all()
        for task_id, scene_id, scene_name, frame_index in direct_rows:
            if scene_id is not None:
                out[task_id] = TaskSceneFrame(
                    scene_id=scene_id,
                    scene_name=scene_name,
                    frame_index=frame_index,
                )

        link_rows = (
            await db.execute(
                select(
                    TaskDatasetItemLink.task_id,
                    DatasetItem.scene_id,
                    Scene.name,
                    DatasetItem.frame_index,
                )
                .join(
                    DatasetItem,
                    TaskDatasetItemLink.dataset_item_id == DatasetItem.id,
                )
                .outerjoin(Scene, DatasetItem.scene_id == Scene.id)
                .where(TaskDatasetItemLink.task_id.in_(chunk))
                .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
            )
        ).all()
        for task_id, scene_id, scene_name, frame_index in link_rows:
            if scene_id is not None and out[task_id].scene_id is None:
                out[task_id] = TaskSceneFrame(
                    scene_id=scene_id,
                    scene_name=scene_name,
                    frame_index=frame_index,
                )

    return out


async def get_scene_frame_task_map(
    db: AsyncSession, scene_id: uuid.UUID
) -> dict[int, uuid.UUID]:
    """scene 内 frame_index → task_id 映射(neighbors / 区间插值共用)。

    双路径反查(task.dataset_item_id / primary_lidar link);同 frame_index
    多模态 item 取首个解析到 task 的(与 neighbors 语义一致)。
    """
    items_rows = (
        await db.execute(
            select(DatasetItem.id, DatasetItem.frame_index)
            .where(DatasetItem.scene_id == scene_id)
            .where(DatasetItem.frame_index.is_not(None))
            .order_by(DatasetItem.frame_index)
        )
    ).all()
    if not items_rows:
        return {}

    item_ids = [row[0] for row in items_rows]

    direct_rows = (
        await db.execute(
            select(Task.id, Task.dataset_item_id).where(
                Task.dataset_item_id.in_(item_ids)
            )
        )
    ).all()
    item_to_task: dict[uuid.UUID, uuid.UUID] = {row[1]: row[0] for row in direct_rows}

    link_rows = (
        await db.execute(
            select(
                TaskDatasetItemLink.dataset_item_id,
                TaskDatasetItemLink.task_id,
            )
            .where(TaskDatasetItemLink.dataset_item_id.in_(item_ids))
            .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
        )
    ).all()
    for item_id_, tid in link_rows:
        item_to_task.setdefault(item_id_, tid)

    # 按 frame_index 排,同 frame_index 取首个有 task 的(多模态去重)
    frame_to_task: dict[int, uuid.UUID] = {}
    for item_id_, fi in items_rows:
        tid = item_to_task.get(item_id_)
        if tid is None:
            continue
        frame_to_task.setdefault(fi, tid)
    return frame_to_task


async def get_scene_timeline_window(
    db: AsyncSession,
    *,
    task: Task,
    start_frame: int,
    end_frame: int,
) -> SceneTimelineWindow | None:
    """解析 task 所在 Scene，并只查询给定闭区间的 frame→task 映射。

    直接 ``Task.dataset_item_id`` 关联优先；点云 ``primary_lidar`` link 仅补缺。
    查询数量不随窗口帧数增长。
    """
    primary_item_id = await resolve_primary_item_id(db, task)
    if primary_item_id is None:
        return None
    primary_item = await db.get(DatasetItem, primary_item_id)
    if (
        primary_item is None
        or primary_item.scene_id is None
        or primary_item.frame_index is None
    ):
        return None
    scene = await db.get(Scene, primary_item.scene_id)
    if scene is None:
        return None

    bounds = (
        await db.execute(
            select(
                func.min(DatasetItem.frame_index),
                func.max(DatasetItem.frame_index),
                func.count(func.distinct(DatasetItem.frame_index)),
            )
            .where(DatasetItem.scene_id == scene.id)
            .where(DatasetItem.frame_index.is_not(None))
        )
    ).one()
    scene_start, scene_end, populated_count = bounds
    if scene_start is None or scene_end is None:
        return None

    direct_rows = (
        await db.execute(
            select(DatasetItem.frame_index, Task.id, Task.status)
            .join(Task, Task.dataset_item_id == DatasetItem.id)
            .where(DatasetItem.scene_id == scene.id)
            .where(Task.project_id == task.project_id)
            .where(DatasetItem.frame_index.between(start_frame, end_frame))
            .order_by(DatasetItem.frame_index, Task.id)
        )
    ).all()
    frame_tasks: dict[int, SceneTimelineTask] = {}
    for frame_index, task_id, status in direct_rows:
        frame_tasks.setdefault(
            frame_index, SceneTimelineTask(task_id=task_id, status=status)
        )

    link_rows = (
        await db.execute(
            select(DatasetItem.frame_index, Task.id, Task.status)
            .join(
                TaskDatasetItemLink,
                TaskDatasetItemLink.dataset_item_id == DatasetItem.id,
            )
            .join(Task, Task.id == TaskDatasetItemLink.task_id)
            .where(DatasetItem.scene_id == scene.id)
            .where(Task.project_id == task.project_id)
            .where(DatasetItem.frame_index.between(start_frame, end_frame))
            .where(TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE)
            .order_by(DatasetItem.frame_index, Task.id)
        )
    ).all()
    for frame_index, task_id, status in link_rows:
        frame_tasks.setdefault(
            frame_index, SceneTimelineTask(task_id=task_id, status=status)
        )

    return SceneTimelineWindow(
        scene_id=scene.id,
        scene_name=scene.name,
        current_frame_index=primary_item.frame_index,
        scene_start_frame=scene_start,
        scene_end_frame=scene_end,
        populated_frame_count=populated_count,
        frame_tasks=frame_tasks,
    )


async def get_scene_timeline_annotation_summaries(
    db: AsyncSession,
    *,
    task_ids: set[uuid.UUID],
    track_id: str | None,
) -> dict[uuid.UUID, SceneTimelineAnnotationSummary]:
    """批量聚合可见 task 的 3D 标注数量与选中轨迹出现位置。"""
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


async def get_neighbors_for_task(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    k: int = 1,
) -> NeighborsResponse | None:
    """返回 task 所在 scene 的前后 k 个邻居。

    返回值:
    - None:task 不存在 / 关联不到 dataset_item / dataset_item 无 scene_id(历史未 backfill);
      API 层据此回 404 或 200 空响应(详 §3.4)。
    - NeighborsResponse:scene 元数据 + prev[]/next[]。
    异常:
    - SceneFrameIndexInconsistent:scene_id 非空但 frame_index NULL,API 层 409。
    """
    task = await db.get(Task, task_id)
    if task is None:
        return None

    primary_item_id = await resolve_primary_item_id(db, task)
    if primary_item_id is None:
        return None

    primary_item = await db.get(DatasetItem, primary_item_id)
    if primary_item is None or primary_item.scene_id is None:
        return None
    if primary_item.frame_index is None:
        raise SceneFrameIndexInconsistent(
            f"dataset_item {primary_item.id} has scene_id but frame_index NULL"
        )

    scene_id = primary_item.scene_id
    cur_frame = primary_item.frame_index
    scene = await db.get(Scene, scene_id)
    if scene is None:
        return None

    # scene 下所有"带 frame_index"的 item 反查 task(当前 task 的主 item 自身
    # 带 scene+frame,故映射至少含当前帧;空 scene 不可达)
    frame_to_task = await get_scene_frame_task_map(db, scene_id)
    if not frame_to_task:
        return None

    ordered_frames = sorted(frame_to_task.keys())
    total = len(ordered_frames)
    try:
        cur_idx = ordered_frames.index(cur_frame)
    except ValueError:
        # cur 本身找不到对应 task(理论不应发生),回空邻居
        return NeighborsResponse(
            scene_id=scene_id,
            scene_name=scene.name,
            frame_index=cur_frame,
            scene_total_frames=total,
            prev=[],
            next=[],
        )

    prev_slice = ordered_frames[max(0, cur_idx - k) : cur_idx][::-1]
    next_slice = ordered_frames[cur_idx + 1 : cur_idx + 1 + k]

    return NeighborsResponse(
        scene_id=scene_id,
        scene_name=scene.name,
        frame_index=cur_frame,
        scene_total_frames=total,
        prev=[
            NeighborInfo(task_id=frame_to_task[fi], frame_index=fi) for fi in prev_slice
        ],
        next=[
            NeighborInfo(task_id=frame_to_task[fi], frame_index=fi) for fi in next_slice
        ],
    )
