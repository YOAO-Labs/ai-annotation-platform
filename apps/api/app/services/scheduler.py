from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, or_, and_, false
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProjectRole
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_lock import TaskLock
from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.user import User
from app.services.project_access import is_privileged_for_project
from app.services.scene import resolve_primary_item_id
from app.services.task_lock import TaskLockService, assert_task_user_active

_PRIMARY_LIDAR_ROLE = "primary_lidar"

# 经典派题路径在「候选查询」与「acquire 上锁」之间存在 TOCTOU 窗口:他人可能抢先
# 上锁。逐个候选尝试 acquire,失败即跳到下一个,最多看这么多个候选(高竞争下兜底,
# 全被占则本次返回 None,客户端重试)。
_NEXT_TASK_CANDIDATE_WINDOW = 20


def visible_batch_statuses_for_project_role(project_role: str | None) -> list[str]:
    """Batch statuses visible to a project role (project-scoped authority).

    Viewer scope aligns with ``_assert_task_visible``.  Missing/unknown roles
    fail closed (empty), never the legacy global role.
    """
    if project_role == ProjectRole.REVIEWER.value:
        return list(REVIEWER_VISIBLE_BATCH_STATUSES)
    if project_role in (ProjectRole.ANNOTATOR.value, ProjectRole.VIEWER.value):
        return list(ANNOTATOR_VISIBLE_BATCH_STATUSES)
    return []


# v0.7.0：按角色分拆可见性集合。
# 标注员能看到 active / annotating（自己分派的批次）+ rejected（特例：让他们看到 reviewer 留言并重做）。
# Reviewer 能看到 active / annotating / reviewing（跨批次审核），不受 assigned_user_ids 约束。
ANNOTATOR_VISIBLE_BATCH_STATUSES = ["active", "annotating", "rejected"]
REVIEWER_VISIBLE_BATCH_STATUSES = ["active", "annotating", "reviewing"]

# 兼容别名（保留 B-16 时引入的常量名；指向 annotator 集合的核心三态）。
WORKBENCH_VISIBLE_BATCH_STATUSES = ["active", "annotating"]


def batch_visibility_clause(user: User, *, project_role: str | None = None):
    """返回应用到 TaskBatch 的可见性 WHERE 子句，按**项目职责**分支：

    - reviewer：active / annotating / reviewing，**不**受 annotator 约束
    - annotator / viewer：active / annotating（annotator_id == self 或 IS NULL）
      + rejected 特例（仅当 annotator_id == self）
    - 其它 / 缺失：fail closed（false）

    super_admin / 项目 owner 走 is_privileged_for_project 越权放行，不调本 helper。

    v0.7.2：从 list 语义切换为单值（batch.annotator_id）。
    调用方需自行 JOIN TaskBatch。
    """
    if project_role == ProjectRole.REVIEWER.value:
        return TaskBatch.status.in_(REVIEWER_VISIBLE_BATCH_STATUSES)
    if project_role not in (
        ProjectRole.ANNOTATOR.value,
        ProjectRole.VIEWER.value,
    ):
        return false()

    is_self = TaskBatch.annotator_id == user.id
    return or_(
        and_(
            TaskBatch.status.in_(["active", "annotating"]),
            or_(TaskBatch.annotator_id.is_(None), is_self),
        ),
        # rejected 对**被分派**的标注员可见（看 reviewer 留言 + 重做）
        and_(TaskBatch.status == "rejected", is_self),
    )


def _task_assigned_to_user(user: User):
    """Match an explicit task assignee, falling back to its batch assignee.

    ``Task.assignee_id`` is the task-level override used by Data Manager
    actions.  Legacy/batch-created rows can still leave it NULL, in which
    case the batch annotator remains the effective assignee.  The open-batch
    case is intentionally kept separate because it must not make rejected or
    reviewing work visible to every annotator.
    """
    return or_(
        Task.assignee_id == user.id,
        and_(
            Task.assignee_id.is_(None),
            Task.batch_id.is_not(None),
            TaskBatch.annotator_id == user.id,
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


def effective_task_assignee_expr():
    """Return the SQL expression matching :func:`effective_task_assignee_id`.

    Callers must outer-join ``TaskBatch`` when the query also includes
    unbatched tasks.
    """
    return func.coalesce(Task.assignee_id, TaskBatch.annotator_id)


def effective_task_reviewer_expr():
    """Return the SQL expression matching the effective reviewer fallback."""
    return func.coalesce(Task.reviewer_id, TaskBatch.reviewer_id)


def task_assignment_clause(user: User):
    """Return the SQL scope for an annotator's effective task assignment.

    Explicit task assignments take precedence over a batch assignment.  A
    NULL task assignee preserves the existing open-batch pool semantics, but
    an unbatched task is visible only when it has an explicit assignee.
    Callers must join ``TaskBatch`` with an outer join.
    """
    return or_(
        _task_assigned_to_user(user),
        and_(
            Task.batch_id.is_not(None),
            Task.assignee_id.is_(None),
            TaskBatch.annotator_id.is_(None),
        ),
    )


def visible_batch_statuses_for(
    user: User, *, project_role: str | None = None
) -> list[str]:
    """非 SQL 路径用：给定**项目职责**返回扁平 status 白名单。

    ``project_role`` is the resolved membership role; missing/unknown fails
    closed.  ``user`` is kept for call-site compatibility but plays no part in
    the decision.
    """
    return visible_batch_statuses_for_project_role(project_role)


def annotator_can_rework_task(
    user: User,
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
        and (task_assignee_id or batch.annotator_id) == user.id
        and batch.status == "reviewing"
        and task_status in {"rejected", "in_progress"}
    )


def task_visibility_clause(user: User, *, project_role: str | None = None):
    """Return the canonical visibility scope for a ``Task`` query.

    Branches on the resolved ``project_role`` (missing/unknown fails closed).
    Viewer shares the annotator scope, aligning with ``_assert_task_visible``.
    This is deliberately separate from ``batch_visibility_clause``: the
    latter is also used by reviewer dashboard queries that select only
    ``TaskBatch``.  Task queries must let an explicit task assignment narrow a
    batch without exposing the rest of that batch, and must use an outer join
    so an explicitly assigned unbatched task remains reachable.
    """
    if project_role == ProjectRole.REVIEWER.value:
        return or_(
            and_(Task.batch_id.is_(None), Task.reviewer_id == user.id),
            and_(
                Task.batch_id.is_not(None),
                batch_visibility_clause(user, project_role=project_role),
            ),
        )
    if project_role not in (
        ProjectRole.ANNOTATOR.value,
        ProjectRole.VIEWER.value,
    ):
        return false()
    is_annotator = project_role == ProjectRole.ANNOTATOR.value
    assigned = _task_assigned_to_user(user)
    arms = [
        # Keep the existing active/annotating open-pool behavior, while an
        # explicit task assignee narrows a normally batch-scoped task.  This
        # task-level override filtering applies to viewer too.
        and_(
            Task.batch_id.is_not(None),
            TaskBatch.status.in_(["active", "annotating"]),
            task_assignment_clause(user),
        ),
        # Rejected work is visible only to its effective assignee.
        and_(
            Task.batch_id.is_not(None),
            TaskBatch.status == "rejected",
            assigned,
        ),
    ]
    if is_annotator:
        arms.append(
            # An explicitly assigned unbatched task has no batch status to gate
            # it; only an annotator may see it.
            and_(Task.batch_id.is_(None), Task.assignee_id == user.id)
        )
        arms.append(
            # A rejected/in-progress task can be resumed during batch review by
            # its annotator assignee; a viewer must not enter this rework arm.
            and_(
                Task.batch_id.is_not(None),
                TaskBatch.status == "reviewing",
                Task.status.in_(["rejected", "in_progress"]),
                assigned,
            )
        )
    return or_(*arms)


# 兼容别名
assigned_user_ids_clause = task_visibility_clause


async def _filter_assignable_task_ids(
    db: AsyncSession,
    user: User,
    project: Project,
    batch_id: uuid.UUID | None,
    task_ids: list[uuid.UUID],
    *,
    project_role: str | None = None,
) -> set[uuid.UUID]:
    """v0.14.1 · 在 task_ids 子集内套用与 get_next_task 主路径一致的可标候选约束,
    额外排除被**他人**持有的未过期锁。返回可分配的 task_id 集合。"""
    already_annotated = (
        select(Annotation.id)
        .where(
            Annotation.task_id == Task.id,
            Annotation.user_id == user.id,
            Annotation.is_active.is_(True),
        )
        .correlate(Task)
        .exists()
    )
    now = datetime.now(timezone.utc)
    other_lock = (
        select(TaskLock.id)
        .where(
            TaskLock.task_id == Task.id,
            TaskLock.user_id != user.id,
            TaskLock.expire_at > now,
        )
        .correlate(Task)
        .exists()
    )
    q = (
        select(Task.id)
        .outerjoin(TaskBatch, Task.batch_id == TaskBatch.id)
        .where(
            Task.id.in_(task_ids),
            Task.project_id == project.id,
            Task.is_labeled.is_(False),
            ~already_annotated,
            ~other_lock,
            or_(
                Task.batch_id.is_(None),
                and_(
                    TaskBatch.status.in_(["active", "annotating"]),
                    TaskBatch.admin_locked.is_(False),
                ),
            ),
        )
    )
    if batch_id:
        q = q.where(Task.batch_id == batch_id)
    if not is_privileged_for_project(user, project):
        q = q.where(assigned_user_ids_clause(user, project_role=project_role))
    if project.maximum_annotations > 1:
        q = q.where(Task.total_annotations < project.maximum_annotations)
    rows = (await db.execute(q)).scalars().all()
    return set(rows)


async def _next_same_scene_task(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    batch_id: uuid.UUID | None,
    project_role: str | None = None,
) -> Task | None:
    """v0.14.1 · scene 连续标注: 找"用户最近提交 task 的同 scene 下一帧"可标 task。

    - 找用户在 window 内最近创建的 active annotation → 其 task → scene_id + frame_index
    - 在该 scene 内取 frame_index 严格大于当前帧的、按帧升序第一个可分配 task
    - 任何一步缺数据(无 recent / 无 scene_id / 无后续帧 / 全被占)→ None,调用方回退既有策略
    不强制独占 scene(其它帧仍可分配给他人)。
    """
    window_min = project.scene_continuation_window_min or 30
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    recent_task_id = (
        await db.execute(
            select(Annotation.task_id)
            .where(
                Annotation.user_id == user.id,
                Annotation.project_id == project.id,
                Annotation.is_active.is_(True),
                Annotation.created_at >= cutoff,
            )
            .order_by(Annotation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent_task_id is None:
        return None

    recent_task = await db.get(Task, recent_task_id)
    if recent_task is None:
        return None
    primary_item_id = await resolve_primary_item_id(db, recent_task)
    if primary_item_id is None:
        return None
    item = await db.get(DatasetItem, primary_item_id)
    if item is None or item.scene_id is None or item.frame_index is None:
        return None

    next_items = (
        await db.execute(
            select(DatasetItem.id, DatasetItem.frame_index)
            .where(
                DatasetItem.scene_id == item.scene_id,
                DatasetItem.frame_index.is_not(None),
                DatasetItem.frame_index > item.frame_index,
            )
            .order_by(DatasetItem.frame_index)
        )
    ).all()
    if not next_items:
        return None
    next_item_ids = [r[0] for r in next_items]

    # 双路径反查 item → task(直挂 dataset_item_id / primary_lidar link)
    item_to_task: dict[uuid.UUID, uuid.UUID] = {}
    for tid, diid in (
        await db.execute(
            select(Task.id, Task.dataset_item_id).where(
                Task.dataset_item_id.in_(next_item_ids)
            )
        )
    ).all():
        if diid is not None:
            item_to_task[diid] = tid
    for diid, tid in (
        await db.execute(
            select(
                TaskDatasetItemLink.dataset_item_id, TaskDatasetItemLink.task_id
            ).where(
                TaskDatasetItemLink.dataset_item_id.in_(next_item_ids),
                TaskDatasetItemLink.role == _PRIMARY_LIDAR_ROLE,
            )
        )
    ).all():
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
        user,
        project,
        batch_id,
        ordered_task_ids,
        project_role=project_role,
    )
    for tid in ordered_task_ids:
        if tid in valid:
            return await db.get(Task, tid)
    return None


async def get_next_task(
    user: User,
    project_id: uuid.UUID,
    db: AsyncSession,
    batch_id: uuid.UUID | None = None,
    *,
    project_role: str | None = None,
) -> Task | None:
    user_id = user.id
    # Authentication may have completed before an administrator suspended the
    # account. Take the same User share lock used by TaskLockService before
    # returning an already-held lock or attempting to claim a new task.
    if not await assert_task_user_active(db, user_id):
        return None
    lock_svc = TaskLockService(db)

    # 1. Check if user already has a locked task in this project
    # B-6 修复：用户在同一项目下可能因切换任务残留多把锁，scalar_one_or_none() 会抛 500。
    # 改用 .first() 取最新一把作为"当前任务"。
    locked_result = await db.execute(
        select(TaskLock)
        .join(Task, Task.id == TaskLock.task_id)
        .where(TaskLock.user_id == user_id, Task.project_id == project_id)
        .order_by(TaskLock.expire_at.desc())
    )
    existing_lock = locked_result.scalars().first()
    if existing_lock:
        task = await db.get(Task, existing_lock.task_id)
        if task and not task.is_labeled:
            return task

    # 2. Get project config
    project = await db.get(Project, project_id)
    if not project:
        return None

    # v0.14.1 · scene 连续标注优先(默认 OFF, 既有项目零回归): 显式打开后, 在套用既有
    # sampling 策略前, 优先返回"用户最近提交 task 的同 scene 下一帧"。找不到则回退。
    if project.prefer_same_scene_continuation:
        scene_task = await _next_same_scene_task(
            db,
            user=user,
            project=project,
            batch_id=batch_id,
            project_role=project_role,
        )
        if scene_task is not None:
            # TOCTOU: _filter_assignable_task_ids 已排除他人锁, 但与此处 acquire 之间存在
            # 竞态窗口——他人可能在两步之间抢先上锁。acquire 返回 None 表示未拿到锁, 此时
            # 不能返回这个未上锁的 task(否则多人同拉同一 scene next frame), 回退到下方既有
            # 经典分配逻辑(经典路径同样以 acquire 结果决定是否返回)。
            acquired = await lock_svc.acquire(
                scene_task.id, user_id, ttl=project.task_lock_ttl_seconds
            )
            if acquired is not None:
                return scene_task

    # 3. Build candidate query: unlabeled, not already annotated by this user.
    # v0.11.30 · 相关 NOT EXISTS 取代 NOT IN(子查询)：标注员标注量大时 NOT IN 会让
    # Postgres 物化整个 task_id 集合（大表热路径放大）；NOT EXISTS 可直接走索引短路。
    already_annotated = (
        select(Annotation.id)
        .where(
            Annotation.task_id == Task.id,
            Annotation.user_id == user_id,
            Annotation.is_active.is_(True),
        )
        .correlate(Task)
        .exists()
    )

    candidates = (
        select(Task)
        .outerjoin(TaskBatch, Task.batch_id == TaskBatch.id)
        .where(
            Task.project_id == project_id,
            Task.is_labeled.is_(False),
            ~already_annotated,
            or_(
                Task.batch_id.is_(None),
                and_(
                    TaskBatch.status.in_(["active", "annotating"]),
                    TaskBatch.admin_locked.is_(False),  # v0.9.15 · ADR-0008
                ),
            ),
        )
    )

    # Batch filtering
    if batch_id:
        candidates = candidates.where(Task.batch_id == batch_id)

    # Assignment filtering: super_admin / 项目 owner 越权放行（可代标注员补刀），
    # 其他角色无论是否显式指定 batch_id，都必须命中 assigned_user_ids（或批次未分派）。
    if not is_privileged_for_project(user, project):
        candidates = candidates.where(
            assigned_user_ids_clause(user, project_role=project_role)
        )

    # 4. Multi-annotator overlap
    if project.maximum_annotations > 1:
        candidates = candidates.where(
            Task.total_annotations < project.maximum_annotations
        )

    # 5. Apply sampling strategy (batch priority as primary sort)
    if project.sampling == "uncertainty":
        # v0.11.30 · 相关标量子查询取每 task 最低预测分，取代 outerjoin Prediction：
        # outerjoin 会按预测条数行扇出（一 task 多 prediction）再对扇出行排序，大表代价高。
        min_pred_score = (
            select(func.min(Prediction.score))
            .where(Prediction.task_id == Task.id)
            .correlate(Task)
            .scalar_subquery()
        )
        candidates = candidates.order_by(
            TaskBatch.priority.desc().nullslast(), min_pred_score.asc().nullslast()
        )
    elif project.sampling == "uniform":
        candidates = candidates.order_by(
            TaskBatch.priority.desc().nullslast(), func.random()
        )
    else:
        candidates = candidates.order_by(
            TaskBatch.priority.desc().nullslast(),
            Task.sequence_order.asc().nullslast(),
            Task.created_at,
        )

    # 6. Pick and lock — 逐个候选尝试 acquire,跳过他人在 TOCTOU 窗口抢先上锁的 task,
    #    返回首个成功上锁者;候选窗口内全被占则返回 None(本次无可派 task,客户端重试)。
    result = await db.execute(candidates.limit(_NEXT_TASK_CANDIDATE_WINDOW))
    for next_task in result.scalars().all():
        acquired = await lock_svc.acquire(
            next_task.id, user_id, ttl=project.task_lock_ttl_seconds
        )
        if acquired is not None:
            return next_task

    return None
