from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, or_, and_, false
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProjectRole
from app.services.project_access import get_workspace_project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.project import Project
from app.services.project_access import is_privileged_for_project
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.labeling_scene import read_spatial_item_facts, resolve_primary_item_id
from app.services.task_lock import TaskLockService, assert_task_user_active
from app.services.labeling_core_read_relations import (
    annotation_row,
    batch_row,
    get_core_row,
    prediction_row,
    task_row,
    task_dataset_item_link_row,
    task_lock_row,
)

_PRIMARY_LIDAR_ROLE = "primary_lidar"

_NEXT_TASK_CANDIDATE_WINDOW = 20


def visible_batch_statuses_for_project_role(project_role: str | None) -> list[str]:
    """Batch statuses visible to a project role (project-scoped authority).

    Viewer scope aligns with ``task_access.assert_task_visible``. Missing/unknown roles
    fail closed (empty), never the legacy global role.
    """
    if project_role == ProjectRole.REVIEWER.value:
        return list(REVIEWER_VISIBLE_BATCH_STATUSES)
    if project_role in (ProjectRole.ANNOTATOR.value, ProjectRole.VIEWER.value):
        return list(ANNOTATOR_VISIBLE_BATCH_STATUSES)
    return []


ANNOTATOR_VISIBLE_BATCH_STATUSES = ["active", "annotating", "rejected"]
REVIEWER_VISIBLE_BATCH_STATUSES = ["active", "annotating", "reviewing"]

WORKBENCH_VISIBLE_BATCH_STATUSES = ["active", "annotating"]


def batch_visibility_clause(
    actor_id: uuid.UUID, *, project_role: str | None = None, batch_model=TaskBatch
):
    """返回应用到 TaskBatch 的可见性 WHERE 子句，按**项目职责**分支：

    - reviewer：active / annotating / reviewing，**不**受 annotator 约束
    - annotator / viewer：active / annotating（annotator_id == self 或 IS NULL）
      + rejected 特例（仅当 annotator_id == self）
    - 其它 / 缺失：fail closed（false）

    super_admin / 项目 owner 走 is_privileged_for_project 越权放行，不调本 helper。

    用单值 batch.annotator_id 筛选标注员归属。
    调用方需自行 JOIN TaskBatch。
    """
    if project_role == ProjectRole.REVIEWER.value:
        return batch_model.status.in_(REVIEWER_VISIBLE_BATCH_STATUSES)
    if project_role not in (
        ProjectRole.ANNOTATOR.value,
        ProjectRole.VIEWER.value,
    ):
        return false()

    is_self = batch_model.annotator_id == actor_id
    return or_(
        and_(
            batch_model.status.in_(["active", "annotating"]),
            or_(batch_model.annotator_id.is_(None), is_self),
        ),
        and_(batch_model.status == "rejected", is_self),
    )


def _task_assigned_to_user(
    actor_id: uuid.UUID, *, task_model=Task, batch_model=TaskBatch
):
    """Match an explicit task assignee, falling back to its batch assignee.

    ``Task.assignee_id`` is the task-level override used by Data Manager
    actions.  Legacy/batch-created rows can still leave it NULL, in which
    case the batch annotator remains the effective assignee.  The open-batch
    case is intentionally kept separate because it must not make rejected or
    reviewing work visible to every annotator.
    """
    return or_(
        task_model.assignee_id == actor_id,
        and_(
            task_model.assignee_id.is_(None),
            task_model.batch_id.is_not(None),
            batch_model.annotator_id == actor_id,
        ),
    )


def effective_task_assignee_id(task: Task, batch: TaskBatch | None) -> uuid.UUID | None:
    """Return the effective annotator for an ORM task row."""
    return (
        task.assignee_id
        if task.assignee_id is not None
        else (batch.annotator_id if batch is not None else None)
    )


def effective_task_reviewer_id(task: Task, batch: TaskBatch | None) -> uuid.UUID | None:
    """Return the effective reviewer for an ORM task row."""
    return (
        task.reviewer_id
        if task.reviewer_id is not None
        else (batch.reviewer_id if batch is not None else None)
    )


def effective_task_assignee_expr(task_model=Task, batch_model=TaskBatch):
    """Return the SQL expression matching :func:`effective_task_assignee_id`.

    Callers must outer-join ``TaskBatch`` when the query also includes
    unbatched tasks.
    """
    return func.coalesce(task_model.assignee_id, batch_model.annotator_id)


def effective_task_reviewer_expr(task_model=Task, batch_model=TaskBatch):
    """Return the SQL expression matching the effective reviewer fallback."""
    return func.coalesce(task_model.reviewer_id, batch_model.reviewer_id)


def task_assignment_clause(
    actor_id: uuid.UUID, *, task_model=Task, batch_model=TaskBatch
):
    """Return the SQL scope for an annotator's effective task assignment.

    Explicit task assignments take precedence over a batch assignment.  A
    NULL task assignee preserves the existing open-batch pool semantics, but
    an unbatched task is visible only when it has an explicit assignee.
    Callers must join ``TaskBatch`` with an outer join.
    """
    return or_(
        _task_assigned_to_user(
            actor_id, task_model=task_model, batch_model=batch_model
        ),
        and_(
            task_model.batch_id.is_not(None),
            task_model.assignee_id.is_(None),
            batch_model.annotator_id.is_(None),
        ),
    )


def annotator_can_rework_task(
    actor_id: uuid.UUID,
    batch: TaskBatch,
    task_status: str,
    task_assignee_id: uuid.UUID | None = None,
    *,
    project_role: str | None = None,
) -> bool:
    """A single rejected task may be redone while its peers remain in review.

    ``project_role`` is the resolved membership role; when omitted the legacy
    global annotator value is not consulted (fail closed).
    """
    is_annotator = (
        project_role == ProjectRole.ANNOTATOR.value
        if project_role is not None
        else False
    )
    return (
        is_annotator
        and (task_assignee_id or batch.annotator_id) == actor_id
        and batch.status == "reviewing"
        and task_status in {"rejected", "in_progress"}
    )


def task_visibility_clause(
    actor_id: uuid.UUID,
    *,
    project_role: str | None = None,
    task_model=Task,
    batch_model=TaskBatch,
):
    """Return the canonical visibility scope for a ``Task`` query.

    Branches on the resolved ``project_role`` (missing/unknown fails closed).
    Viewer shares the annotator scope, aligning with ``task_access.assert_task_visible``.
    This is deliberately separate from ``batch_visibility_clause``: the
    latter is also used by reviewer dashboard queries that select only
    ``TaskBatch``.  Task queries must let an explicit task assignment narrow a
    batch without exposing the rest of that batch, and must use an outer join
    so an explicitly assigned unbatched task remains reachable.
    """
    if project_role == ProjectRole.REVIEWER.value:
        return or_(
            and_(task_model.batch_id.is_(None), task_model.reviewer_id == actor_id),
            and_(
                task_model.batch_id.is_not(None),
                batch_visibility_clause(
                    actor_id, project_role=project_role, batch_model=batch_model
                ),
            ),
        )
    if project_role not in (
        ProjectRole.ANNOTATOR.value,
        ProjectRole.VIEWER.value,
    ):
        return false()
    is_annotator = project_role == ProjectRole.ANNOTATOR.value
    assigned = _task_assigned_to_user(
        actor_id, task_model=task_model, batch_model=batch_model
    )
    arms = [
        and_(
            task_model.batch_id.is_not(None),
            batch_model.status.in_(["active", "annotating"]),
            task_assignment_clause(
                actor_id, task_model=task_model, batch_model=batch_model
            ),
        ),
        and_(
            task_model.batch_id.is_not(None),
            batch_model.status == "rejected",
            assigned,
        ),
    ]
    if is_annotator:
        arms.append(
            and_(task_model.batch_id.is_(None), task_model.assignee_id == actor_id)
        )
        arms.append(
            and_(
                task_model.batch_id.is_not(None),
                batch_model.status == "reviewing",
                task_model.status.in_(["rejected", "in_progress"]),
                assigned,
            )
        )
    return or_(*arms)


async def _filter_assignable_task_ids(
    db: AsyncSession,
    actor_id: uuid.UUID,
    project: Project,
    batch_id: uuid.UUID | None,
    task_ids: list[uuid.UUID],
    *,
    project_role: str | None = None,
) -> set[uuid.UUID]:
    """在 task_ids 子集内套用与 get_next_task 主路径一致的可标候选约束,
    额外排除被**他人**持有的未过期锁。返回可分配的 task_id 集合。"""
    already_annotated = (
        select(annotation_row.id)
        .where(
            annotation_row.task_id == task_row.id,
            annotation_row.user_id == actor_id,
            annotation_row.is_active.is_(True),
        )
        .correlate(task_row)
        .exists()
    )
    now = datetime.now(timezone.utc)
    other_lock = (
        select(task_lock_row.id)
        .where(
            task_lock_row.task_id == task_row.id,
            task_lock_row.user_id != actor_id,
            task_lock_row.expire_at > now,
        )
        .correlate(task_row)
        .exists()
    )
    q = (
        select(task_row.id)
        .outerjoin(batch_row, task_row.batch_id == batch_row.id)
        .where(
            task_row.id.in_(task_ids),
            task_row.project_id == project.id,
            task_row.is_labeled.is_(False),
            ~already_annotated,
            ~other_lock,
            or_(
                task_row.batch_id.is_(None),
                and_(
                    batch_row.status.in_(["active", "annotating"]),
                    batch_row.admin_locked.is_(False),
                ),
            ),
        )
    )
    if batch_id:
        q = q.where(task_row.batch_id == batch_id)
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not is_privileged_for_project(actor, project):
        q = q.where(
            task_visibility_clause(
                actor_id,
                project_role=project_role,
                task_model=task_row,
                batch_model=batch_row,
            )
        )
    if project.maximum_annotations > 1:
        q = q.where(task_row.total_annotations < project.maximum_annotations)
    rows = (await db.execute(q)).scalars().all()
    return set(rows)


async def _next_same_scene_task(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project: Project,
    batch_id: uuid.UUID | None,
    project_role: str | None = None,
) -> Task | None:
    """scene 连续标注: 找"用户最近提交 task 的同 scene 下一帧"可标 task。

    - 找用户在 window 内最近创建的 active annotation → 其 task → scene_id + frame_index
    - 在该 scene 内取 frame_index 严格大于当前帧的、按帧升序第一个可分配 task
    - 任何一步缺数据(无 recent / 无 scene_id / 无后续帧 / 全被占)→ None,调用方回退既有策略
    不强制独占 scene(其它帧仍可分配给他人)。
    """
    window_min = project.scene_continuation_window_min or 30
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    recent_task_id = (
        await db.execute(
            select(annotation_row.task_id)
            .where(
                annotation_row.user_id == actor_id,
                annotation_row.project_id == project.id,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= cutoff,
            )
            .order_by(annotation_row.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent_task_id is None:
        return None

    recent_task = await get_core_row(db, task_row, recent_task_id)
    if recent_task is None:
        return None
    primary_item_id = await resolve_primary_item_id(db, recent_task)
    if primary_item_id is None:
        return None
    item_facts, _ = await read_spatial_item_facts(db, [primary_item_id])
    item = item_facts.get(primary_item_id)
    if item is None or item.scene_id is None or item.frame_index is None:
        return None
    direct_rows = (
        await db.execute(
            select(task_row.id, task_row.dataset_item_id)
            .where(task_row.project_id == project.id)
            .where(task_row.dataset_item_id.is_not(None))
        )
    ).all()
    link_rows = (
        await db.execute(
            select(task_row.id, task_dataset_item_link_row.dataset_item_id)
            .join(task_row, task_row.id == task_dataset_item_link_row.task_id)
            .where(task_row.project_id == project.id)
            .where(task_dataset_item_link_row.role == _PRIMARY_LIDAR_ROLE)
        )
    ).all()
    item_facts, _ = await read_spatial_item_facts(
        db,
        [item_id for _, item_id in (*direct_rows, *link_rows) if item_id is not None],
    )
    primary = item_facts.get(primary_item_id)
    if primary is None or primary.scene_id is None or primary.frame_index is None:
        return None
    next_items = sorted(
        (
            (item_id, item.frame_index)
            for item_id, item in item_facts.items()
            if item.scene_id == primary.scene_id
            and item.frame_index is not None
            and item.frame_index > primary.frame_index
        ),
        key=lambda row: (row[1], str(row[0])),
    )
    if not next_items:
        return None

    item_to_task: dict[uuid.UUID, uuid.UUID] = {}
    for tid, diid in direct_rows:
        if diid is not None:
            item_to_task[diid] = tid
    for tid, diid in link_rows:
        item_to_task.setdefault(diid, tid)

    ordered_task_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for diid, _fi in next_items:
        tid = item_to_task.get(diid)
        if tid is not None and tid not in seen:
            seen.add(tid)
            ordered_task_ids.append(tid)
    if not ordered_task_ids:
        return None

    valid = await _filter_assignable_task_ids(
        db,
        actor_id,
        project,
        batch_id,
        ordered_task_ids,
        project_role=project_role,
    )
    for tid in ordered_task_ids:
        if tid in valid:
            return await get_core_row(db, task_row, tid)
    return None


async def get_next_task(
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    db: AsyncSession,
    batch_id: uuid.UUID | None = None,
    *,
    project_role: str | None = None,
) -> Task | None:
    user_id = actor_id
    if not await assert_task_user_active(db, user_id):
        return None
    lock_svc = TaskLockService(db)

    locked_result = await db.execute(
        select(task_lock_row)
        .join(task_row, task_row.id == task_lock_row.task_id)
        .where(task_lock_row.user_id == user_id, task_row.project_id == project_id)
        .order_by(task_lock_row.expire_at.desc())
    )
    existing_lock = locked_result.scalars().first()
    if existing_lock:
        task = await get_core_row(db, task_row, existing_lock.task_id)
        if task and not task.is_labeled:
            return task

    project = await get_workspace_project(db, project_id=project_id)
    if not project:
        return None

    if project.prefer_same_scene_continuation:
        scene_task = await _next_same_scene_task(
            db,
            actor_id=actor_id,
            project=project,
            batch_id=batch_id,
            project_role=project_role,
        )
        if scene_task is not None:
            acquired = await lock_svc.acquire(
                scene_task.id, user_id, ttl=project.task_lock_ttl_seconds
            )
            if acquired is not None:
                return scene_task

    already_annotated = (
        select(annotation_row.id)
        .where(
            annotation_row.task_id == task_row.id,
            annotation_row.user_id == user_id,
            annotation_row.is_active.is_(True),
        )
        .correlate(task_row)
        .exists()
    )

    candidates = (
        select(task_row)
        .outerjoin(batch_row, task_row.batch_id == batch_row.id)
        .where(
            task_row.project_id == project_id,
            task_row.is_labeled.is_(False),
            ~already_annotated,
            or_(
                task_row.batch_id.is_(None),
                and_(
                    batch_row.status.in_(["active", "annotating"]),
                    batch_row.admin_locked.is_(False),
                ),
            ),
        )
    )

    if batch_id:
        candidates = candidates.where(task_row.batch_id == batch_id)

    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not is_privileged_for_project(actor, project):
        candidates = candidates.where(
            task_visibility_clause(
                actor_id,
                project_role=project_role,
                task_model=task_row,
                batch_model=batch_row,
            )
        )

    if project.maximum_annotations > 1:
        candidates = candidates.where(
            task_row.total_annotations < project.maximum_annotations
        )

    if project.sampling == "uncertainty":
        min_pred_score = (
            select(func.min(prediction_row.score))
            .where(prediction_row.task_id == task_row.id)
            .correlate(task_row)
            .scalar_subquery()
        )
        candidates = candidates.order_by(
            batch_row.priority.desc().nullslast(), min_pred_score.asc().nullslast()
        )
    elif project.sampling == "uniform":
        candidates = candidates.order_by(
            batch_row.priority.desc().nullslast(), func.random()
        )
    else:
        candidates = candidates.order_by(
            batch_row.priority.desc().nullslast(),
            task_row.sequence_order.asc().nullslast(),
            task_row.created_at,
        )

    result = await db.execute(candidates.limit(_NEXT_TASK_CANDIDATE_WINDOW))
    for next_task in result.scalars().all():
        acquired = await lock_svc.acquire(
            next_task.id, user_id, ttl=project.task_lock_ttl_seconds
        )
        if acquired is not None:
            return next_task

    return None
