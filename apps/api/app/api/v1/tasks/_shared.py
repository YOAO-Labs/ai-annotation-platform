import base64
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProjectRole, UserRole
from app.db.models.user import User
from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import DatasetItem
from app.schemas.task import (
    TaskOut,
    VideoMetadata,
)
from app.schemas.image_pyramid import ImagePyramidSummary
from app.deps import get_current_user, get_db
from app.services.project_access import (
    ProjectAccess,
    ProjectCapability,
    resolve_project_access,
)
from app.services.scheduler import (
    effective_task_assignee_id,
    visible_batch_statuses_for_project_role,
    annotator_can_rework_task,
)
from app.services.storage import storage_service
from app.db.models.task_batch import TaskBatch
from app.db.models.project_member import ProjectMember
from app.services.annotation_evidence import freeze_review_contributor_evidence

logger = logging.getLogger(__name__)
VIDEO_MANIFEST_URL_EXPIRES_IN = 3600

_ANNOTATORS = (
    UserRole.SUPER_ADMIN,
    UserRole.PROJECT_ADMIN,
    UserRole.REVIEWER,
    UserRole.ANNOTATOR,
)
_REVIEWERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN, UserRole.REVIEWER)
_LOCKED_STATUSES = {"review", "completed"}


def _start_review_round(task: Task) -> uuid.UUID:
    """Create the stable identifier shared by one submit and its review."""
    task.review_round_id = uuid.uuid4()
    return task.review_round_id


def _ensure_review_round(task: Task) -> uuid.UUID:
    """Return a round id, assigning one for legacy tasks when needed."""
    if task.review_round_id is None:
        task.review_round_id = uuid.uuid4()
    return task.review_round_id


async def _task_contributor_snapshot(
    db: AsyncSession,
    task: Task,
    extra_user_ids: Iterable[uuid.UUID | None] = (),
) -> list[str]:
    """Return stable annotator contributors for workflow audit evidence.

    The task assignee is retained even when no annotation remains.  Active
    annotation authors cover collaborative video/scene tasks and imported
    histories where the task's mutable assignee is no longer sufficient.
    """
    ids = {value for value in extra_user_ids if value is not None}
    if task.assignee_id is not None:
        ids.add(task.assignee_id)
    rows = await db.execute(
        select(Annotation.user_id)
        .where(
            Annotation.task_id == task.id,
            Annotation.user_id.is_not(None),
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .distinct()
    )
    ids.update(value for (value,) in rows if value is not None)
    return sorted(str(value) for value in ids)


def _capture_first_review_contributor_snapshot(
    task: Task,
    contributor_ids: Iterable[str],
) -> None:
    """Persist the first-round contributors before audit retention can run."""

    if task.first_review_eligible is not True or task.first_reviewed_at is not None:
        return
    task.first_review_contributor_ids = sorted(
        {str(value) for value in contributor_ids if value}
    )


async def _review_round_contributor_snapshot(db: AsyncSession, task: Task) -> list[str]:
    """Load the contributor snapshot captured by this task's submit audit or row.

    Approval happens in a later request, after annotations or assignment may
    have changed.  Re-reading those mutable rows would credit the wrong
    people, so a round without a matching submit snapshot stays unknown.
    """
    if task.review_round_id is None:
        return []
    row = (
        await db.execute(
            select(AuditLog.detail_json)
            .where(
                AuditLog.action.in_(("task.submit", "task.skip")),
                AuditLog.target_id == str(task.id),
                AuditLog.status_code == 200,
                AuditLog.detail_json["project_id"].astext == str(task.project_id),
                AuditLog.detail_json["review_round_id"].astext
                == str(task.review_round_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    values = row.get("contributor_ids") if isinstance(row, dict) else None
    if not isinstance(values, list):
        # The submit audit is intentionally retained as an audit trail, but it
        # may already have been archived when a long-running review reaches a
        # decision. New tasks carry the same snapshot on the task row so the
        # first-review fact remains attributable after that retention boundary.
        values = (
            task.first_review_contributor_ids
            if task.first_reviewed_at is None
            else None
        )
    if not isinstance(values, list):
        return []
    return sorted({str(value) for value in values if value})


def _record_first_review_fact(
    task: Task,
    *,
    reviewed_at: datetime,
    result: str,
    contributor_ids: Iterable[str],
) -> bool:
    """Write the first completed review fact once for eligible new tasks."""
    if task.first_review_eligible is not True or task.first_reviewed_at is not None:
        return False
    task.first_reviewed_at = reviewed_at
    task.first_review_result = result
    task.first_review_contributor_ids = sorted(
        {str(value) for value in contributor_ids if value}
    )
    return True


def _assert_access_binding(
    access: ProjectAccess, *, user: User, project_id: uuid.UUID
) -> None:
    """Fail closed when the resolved access does not match user and resource."""

    if access.user_id != user.id or access.project_id != project_id:
        raise HTTPException(
            status_code=409,
            detail={"reason": "access_context_mismatch"},
        )


def _assert_task_editable(
    task: Task,
    user: User | None = None,
    *,
    access: ProjectAccess | None = None,
) -> None:
    """v0.6.5: 已提交质检 / 已通过审核的任务对所有 annotation 写动作锁死。
    标注员要继续编辑必须先 withdraw（review 态）或 reopen（completed 态）。
    M2: 审核员可在 status=review 时直接微调标注（审计记 TASK_REVIEWER_EDIT）。

    ``access`` is the verified project access (B2 routes pass it).  Only
    ``access.is_manager`` is management: a foreign-project administrator
    membership must not bypass task assignment or review checks.  A non-manager
    without a matching access fails closed; the global role is never a fallback.
    """
    if user is not None and access is not None:
        _assert_access_binding(access, user=user, project_id=task.project_id)

    is_manager = bool(access is not None and access.is_manager)
    if user is not None and not is_manager:
        if access is None:
            raise HTTPException(status_code=403, detail="缺少项目权限上下文")
        if (
            access.project_role == ProjectRole.ANNOTATOR.value
            and task.assignee_id is not None
            and task.assignee_id != user.id
        ):
            raise HTTPException(
                status_code=403, detail="Task belongs to another annotator"
            )
    if task.status not in _LOCKED_STATUSES:
        return
    if task.status == "review" and user is not None:
        if is_manager or (
            access is not None and access.project_role == ProjectRole.REVIEWER.value
        ):
            return
    raise HTTPException(
        status_code=409,
        detail={"reason": "task_locked", "status": task.status},
    )


async def _load_task_or_404(db: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _has_current_project_membership(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    lock: bool = False,
) -> bool:
    stmt = (
        select(ProjectMember.id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .limit(1)
    )
    if lock:
        stmt = stmt.with_for_update(read=True)
    return (await db.scalar(stmt)) is not None


async def _project_role_for_user(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    lock: bool = False,
) -> str | None:
    """Resolved project role for one account in one project.

    Project-scoped callers must use this instead of the account's global role.
    ``lock=True`` acquires the membership with ``FOR SHARE`` (conflicts with the
    role-changing ``FOR UPDATE``).  Returns ``None`` when there is no
    membership.
    """

    stmt = (
        select(ProjectMember.role)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .limit(1)
    )
    if lock:
        stmt = stmt.with_for_update(read=True)
    return await db.scalar(stmt)


async def _has_current_project_membership_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    roles: Iterable[str] | None = None,
    lock: bool = False,
) -> bool:
    """True when the current membership exists and its role is in ``roles``."""

    role = await _project_role_for_user(db, project_id, user_id, lock=lock)
    if role is None:
        return False
    if roles is None:
        return True
    return role in set(roles)


async def _assert_current_project_member(db: AsyncSession, project, user: User) -> None:
    """Assert the account has any valid access (manager or member)."""

    await resolve_project_access(db, user=user, project=project, lock_membership=True)


async def _effective_task_assignee_id(db: AsyncSession, task: Task) -> uuid.UUID | None:
    batch = await db.get(TaskBatch, task.batch_id) if task.batch_id else None
    return effective_task_assignee_id(task, batch)


def _assert_effective_task_assignee(
    user: User,
    effective_assignee_id: uuid.UUID | None,
    *,
    action: str,
    project_id: uuid.UUID,
    allow_open_pool: bool = False,
    access: ProjectAccess | None = None,
) -> None:
    """Only the effective annotator may act.

    ``project_id`` is the actual resource project and is required: the binding
    is checked explicitly instead of comparing the access with itself.
    ``access`` is the verified project access (B2 routes pass it).  Only
    ``access.is_manager`` is management; a foreign-project administrator
    membership cannot act as the effective assignee.  A non-manager without a
    matching access fails closed.
    """

    if access is not None:
        _assert_access_binding(access, user=user, project_id=project_id)
        if access.is_manager:
            return
        if access.project_role == ProjectRole.ANNOTATOR.value and (
            effective_assignee_id == user.id
            or (allow_open_pool and effective_assignee_id is None)
        ):
            return
    raise HTTPException(status_code=403, detail=f"only effective assignee can {action}")


def _task_write_allowed(
    task: Task,
    access: ProjectAccess,
    *,
    allow_review_adjustment: bool,
) -> bool:
    """Whether ``access`` may write annotation-phase content on ``task``.

    A manager keeps the existing management path.  An annotator needs
    ``annotation.write``.  A reviewer is admitted only for a *review
    adjustment* (``task.status == "review"``) and only with ``review.write``;
    every other combination fails closed.  No global account role is consulted.
    """

    if access.is_manager:
        return True
    if access.project_role == ProjectRole.ANNOTATOR.value and access.has(
        ProjectCapability.ANNOTATION_WRITE.value
    ):
        return True
    if (
        allow_review_adjustment
        and access.project_role == ProjectRole.REVIEWER.value
        and task.status == "review"
        and access.has(ProjectCapability.REVIEW_WRITE.value)
    ):
        return True
    return False


async def _resolve_task_access(
    db: AsyncSession,
    task: Task,
    user: User,
    *,
    lock_membership: bool = False,
) -> ProjectAccess:
    """Resolve the current project access for the task's actual project."""

    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await resolve_project_access(
        db, user=user, project=project, lock_membership=lock_membership
    )


def assert_annotation_write_allowed(
    task: Task,
    access: ProjectAccess,
    *,
    allow_review_adjustment: bool = True,
) -> None:
    """Fail closed unless ``access`` may write annotation-phase content.

    Reuses the same predicate as the FastAPI write dependency so a mutation
    owner reached outside a task-id dependency (for example a bulk endpoint that
    derives its single task from the payload) enforces the annotation-phase
    capability instead of only task-state editability.
    """

    if not _task_write_allowed(
        task, access, allow_review_adjustment=allow_review_adjustment
    ):
        raise HTTPException(status_code=403, detail="缺少项目权限: annotation.write")


async def _assert_review_adjustment_evidence(
    db: AsyncSession,
    task: Task,
    user: User,
    access: ProjectAccess,
) -> None:
    """Enforce frozen non-self evidence before a review-phase annotation edit.

    An annotator is blocked from a task in ``review`` by ``_assert_task_editable``
    (``task_locked``); a manager or project reviewer reaching this state is a
    *review adjustment* and must not be a contributor, the round submitter or the
    effective annotator.  Unknown legacy evidence fails closed (409).
    """

    if task.status != "review":
        return
    if not (access.is_manager or access.project_role == ProjectRole.REVIEWER.value):
        return
    from app.services.annotation_evidence import assert_review_evidence_current

    effective_annotator_id = await _effective_task_assignee_id(db, task)
    assert_review_evidence_current(
        task, user.id, effective_annotator_id=effective_annotator_id
    )


async def require_task_annotation_write(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """FastAPI dependency: annotation write authority for ``task_id``.

    The membership is acquired ``FOR SHARE`` so a concurrent member role change
    (``FOR UPDATE``) cannot commit while this authorized mutation is in flight.
    Reviewers are admitted only for a review adjustment; annotators, managers
    and unknown roles are handled by :func:`_task_write_allowed`.
    """

    task = await _load_task_or_404(db, task_id)
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    if not _task_write_allowed(task, access, allow_review_adjustment=True):
        raise HTTPException(status_code=403, detail="缺少项目权限: annotation.write")
    await _assert_review_adjustment_evidence(db, task, user, access)
    return access


async def require_task_annotation_write_strict(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """Annotation write authority without the reviewer review-adjustment path."""

    task = await _load_task_or_404(db, task_id)
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    if not _task_write_allowed(task, access, allow_review_adjustment=False):
        raise HTTPException(status_code=403, detail="缺少项目权限: annotation.write")
    await _assert_review_adjustment_evidence(db, task, user, access)
    return access


async def require_task_annotation_write_lifecycle(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """Annotation-phase capability for a task *lifecycle* transition.

    ``submit``/``skip``/``withdraw``/``reopen``/``accept-rejection`` move a task
    between phases; they are not annotation-content edits, so the frozen
    non-self review-evidence guard does not apply.  The capability and the
    ``FOR SHARE`` membership are still required.
    """

    task = await _load_task_or_404(db, task_id)
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    if not _task_write_allowed(task, access, allow_review_adjustment=True):
        raise HTTPException(status_code=403, detail="缺少项目权限: annotation.write")
    return access


async def require_task_review_write(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """FastAPI dependency: review write authority for ``task_id``."""

    task = await _load_task_or_404(db, task_id)
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    if not (
        access.is_manager or ProjectCapability.REVIEW_WRITE.value in access.capabilities
    ):
        raise HTTPException(status_code=403, detail="缺少项目权限: review.write")
    return access


async def _assert_task_visible(
    db: AsyncSession,
    task: Task,
    user: User,
    *,
    access: ProjectAccess | None = None,
) -> None:
    """B-16 + v0.7.0：服务端强制 batch 可见性，按角色分支。
    super_admin / 项目 owner 越权放行；reviewer 见 active/annotating/reviewing；
    annotator 见 active/annotating（assigned）+ rejected（assigned 特例）。
    无 batch 的任务仅对显式分派的标注员可见；批次任务优先使用 task
    级 assignee_id，NULL 时回退到 batch.annotator_id。
    """
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if access is None:
        access = await resolve_project_access(db, user=user, project=project)
    _assert_access_binding(access, user=user, project_id=task.project_id)
    if access.is_manager:
        return
    project_role = access.project_role
    if task.file_type == "video" and bool(
        (project.video_collaboration or {}).get("enabled")
    ):
        return
    if task.batch_id is None:
        if (
            project_role == ProjectRole.ANNOTATOR.value and task.assignee_id == user.id
        ) or (
            project_role == ProjectRole.REVIEWER.value and task.reviewer_id == user.id
        ):
            return
        raise HTTPException(status_code=404, detail="Task not found")
    batch = await db.get(TaskBatch, task.batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Task not found")

    visible_statuses = visible_batch_statuses_for_project_role(project_role)
    if batch.status not in visible_statuses and not annotator_can_rework_task(
        user, batch, task.status, task.assignee_id, project_role=project_role
    ):
        raise HTTPException(status_code=404, detail="Task not found")

    # reviewer 不受 annotator 约束（跨批次审核）
    if project_role == ProjectRole.REVIEWER.value:
        return

    # Task-level assignment takes precedence over the legacy batch assignment.
    is_task_assigned = task.assignee_id is not None and task.assignee_id == user.id
    is_batch_assigned = task.assignee_id is None and batch.annotator_id == user.id
    is_assigned = is_task_assigned or is_batch_assigned
    # rejected 状态特例：仅对被分派的标注员放行
    if batch.status == "rejected" and not is_assigned:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.assignee_id is not None and not is_task_assigned:
        raise HTTPException(status_code=404, detail="Task not found")
    if batch.annotator_id is not None and not is_assigned:
        raise HTTPException(status_code=404, detail="Task not found")


async def _visible_task_ids(
    db: AsyncSession,
    project,
    user: User,
    task_ids: list[uuid.UUID],
    *,
    access: ProjectAccess | None = None,
) -> set[uuid.UUID]:
    """v0.15.26 · `_assert_task_visible` 的批量非抛错版,返回 task_ids 中可见的子集。

    邻帧标注批量端点用它逐邻帧 task 复核 batch 可见性 / 分派状态:该端点替代的
    旧链路(前端对每个邻帧各发一条 getAnnotations)会逐 task 触发 _assert_task_visible,
    所以批量化后必须补回同口径过滤,否则 annotator 凭中心 task 可见即可拿到「分派给
    别人 / 状态不可见」的邻帧框几何(权限漂移)。口径与 _assert_task_visible 完全一致:
    特权放行;否则按 batch 状态白名单 + annotator 单值分派校验。
    """
    if not task_ids:
        return set()
    try:
        if access is None:
            access = await resolve_project_access(db, user=user, project=project)
    except HTTPException:
        return set()
    _assert_access_binding(access, user=user, project_id=project.id)
    if access.is_manager:
        result = await db.execute(
            select(Task.id).where(
                Task.project_id == project.id,
                Task.id.in_(task_ids),
            )
        )
        return set(result.scalars().all())
    project_role = access.project_role

    rows = (
        await db.execute(
            select(
                Task.id,
                Task.batch_id,
                Task.status,
                Task.assignee_id,
                Task.reviewer_id,
            ).where(Task.project_id == project.id, Task.id.in_(task_ids))
        )
    ).all()
    batch_ids = {bid for _, bid, _, _, _ in rows if bid is not None}
    batches: dict[uuid.UUID, TaskBatch] = {}
    if batch_ids:
        result = await db.execute(select(TaskBatch).where(TaskBatch.id.in_(batch_ids)))
        batches = {b.id: b for b in result.scalars()}

    visible_statuses = visible_batch_statuses_for_project_role(project_role)
    is_reviewer = project_role == ProjectRole.REVIEWER.value
    visible: set[uuid.UUID] = set()
    for tid, bid, task_status, task_assignee_id, task_reviewer_id in rows:
        if bid is None:
            if (
                project_role == ProjectRole.ANNOTATOR.value
                and task_assignee_id == user.id
            ) or (
                project_role == ProjectRole.REVIEWER.value
                and task_reviewer_id == user.id
            ):
                visible.add(tid)
            continue
        batch = batches.get(bid)
        if batch is None or (
            batch.status not in visible_statuses
            and not annotator_can_rework_task(
                user, batch, task_status, task_assignee_id, project_role=project_role
            )
        ):
            continue
        if is_reviewer:
            visible.add(tid)
            continue
        is_task_assigned = task_assignee_id is not None and task_assignee_id == user.id
        is_batch_assigned = task_assignee_id is None and batch.annotator_id == user.id
        is_assigned = is_task_assigned or is_batch_assigned
        if batch.status == "rejected" and not is_assigned:
            continue
        if task_assignee_id is not None and not is_task_assigned:
            continue
        if batch.annotator_id is not None and not is_assigned:
            continue
        visible.add(tid)
    return visible


# sequence_order 为 NULL 的非序列任务(图像标注等)用该哨兵参与排序,稳定排在
# 序列任务(点云 scene 帧)之后。取 int32 上限,真实帧序号不可能触及。
_SEQ_NULL_SENTINEL = 2147483647


def _encode_task_cursor(seq_order: int | None, created_at, task_id: uuid.UUID) -> str:
    seq = _SEQ_NULL_SENTINEL if seq_order is None else seq_order
    ts = (
        created_at.astimezone(timezone.utc).isoformat()
        if created_at.tzinfo
        else created_at.isoformat()
    )
    return base64.urlsafe_b64encode(f"{seq}|{ts}|{task_id.hex}".encode()).decode()


def _decode_task_cursor(cursor: str):
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()

    parts = raw.split("|", 2)
    if len(parts) == 3:
        seq_str, ts_str, id_hex = parts
        seq = int(seq_str)
    else:
        # 兼容旧 2 段游标((created_at, id) 时代):seq 取哨兵,降级为按时间/ id 续翻。
        ts_str, id_hex = parts
        seq = _SEQ_NULL_SENTINEL
    ts = datetime.fromisoformat(ts_str)
    return seq, ts, uuid.UUID(id_hex)


def _task_with_url(
    task: Task,
    width: int | None = None,
    height: int | None = None,
    thumbnail_path: str | None = None,
    blurhash: str | None = None,
    video_metadata: dict | None = None,
    image_pyramid: ImagePyramidSummary | None = None,
    briefs: dict | None = None,
) -> TaskOut:
    """v0.8.8 · 由手写 dict 改为 ``TaskOut.model_validate`` + 动态字段注入。

    Schema 漂移防护：DB 直读字段（如 v0.8.7 加的 ``skip_reason`` /
    ``skipped_at``、未来新增列）通过 ``from_attributes`` 自动映射，无需在此手写。
    本 helper 仅负责无法从 ORM 直读的部分：

    * ``file_url`` / ``thumbnail_url`` — MinIO presigned 签发
    * ``image_width`` / ``image_height`` / ``blurhash`` —— 来源可能是
      task 自身或关联的 :class:`DatasetItem`
    * ``assignee`` / ``reviewer`` (UserBrief) —— 调用方批量解析后传入
      ``briefs={str(user_id): UserBrief}``
    """
    bucket = (
        storage_service.datasets_bucket
        if task.dataset_item_id or task.file_type == "point_cloud"
        else storage_service.bucket
    )
    try:
        file_url = storage_service.generate_download_url(task.file_path, bucket=bucket)
    except Exception:
        file_url = None

    thumbnail_url: str | None = None
    if thumbnail_path:
        # v0.10.17 · thumbnails/* 走 media-cache 桶,其它(如旧路径)按 helper 默认走 datasets
        thumb_bucket = storage_service.bucket_for_cache_key(
            thumbnail_path, default=bucket
        )
        try:
            thumbnail_url = storage_service.generate_download_url(
                thumbnail_path, bucket=thumb_bucket
            )
        except Exception:
            pass

    out = TaskOut.model_validate(task, from_attributes=True)
    out.file_url = file_url
    out.thumbnail_url = thumbnail_url
    out.image_width = width
    out.image_height = height
    out.blurhash = blurhash
    out.video_metadata = (
        VideoMetadata.model_validate(video_metadata) if video_metadata else None
    )
    out.image_pyramid = image_pyramid
    if briefs is not None:
        if task.assignee_id is not None:
            out.assignee = briefs.get(str(task.assignee_id))
        if task.reviewer_id is not None:
            out.reviewer = briefs.get(str(task.reviewer_id))
    return out


async def _attach_dimensions(
    db: AsyncSession,
    task: Task,
) -> tuple[int | None, int | None, str | None, str | None, dict | None]:
    if task.dataset_item_id:
        from app.db.models.dataset import DatasetItem

        item = await db.get(DatasetItem, task.dataset_item_id)
        if item:
            video_metadata = (
                dict((item.metadata_ or {}).get("video") or {})
                if item.file_type == "video"
                else None
            )
            return (
                item.width,
                item.height,
                item.thumbnail_path,
                item.blurhash,
                video_metadata,
            )
    return None, None, task.thumbnail_path, task.blurhash, None


async def _video_frame_count(db: AsyncSession, task: Task) -> int | None:
    if not task.dataset_item_id:
        return None
    item = await db.get(DatasetItem, task.dataset_item_id)
    if not item:
        return None
    video = (item.metadata_ or {}).get("video")
    if not isinstance(video, dict):
        return None
    frame_count = video.get("frame_count")
    try:
        return int(frame_count) if frame_count is not None else None
    except (TypeError, ValueError):
        return None


async def _attach_dimensions_batch(
    db: AsyncSession,
    tasks: list[Task],
) -> dict[
    uuid.UUID, tuple[int | None, int | None, str | None, str | None, dict | None]
]:
    result: dict[
        uuid.UUID,
        tuple[int | None, int | None, str | None, str | None, dict | None],
    ] = {}

    item_ids = [t.dataset_item_id for t in tasks if t.dataset_item_id]
    if item_ids:
        from app.db.models.dataset import DatasetItem

        rows = await db.execute(
            select(
                DatasetItem.id,
                DatasetItem.width,
                DatasetItem.height,
                DatasetItem.thumbnail_path,
                DatasetItem.blurhash,
                DatasetItem.file_type,
                DatasetItem.metadata_,
            ).where(DatasetItem.id.in_(item_ids))
        )
        item_data = {
            row[0]: (
                row[1],
                row[2],
                row[3],
                row[4],
                dict((row[6] or {}).get("video") or {}) if row[5] == "video" else None,
            )
            for row in rows
        }
        for t in tasks:
            if t.dataset_item_id:
                result[t.id] = item_data.get(
                    t.dataset_item_id, (None, None, None, None, None)
                )

    for t in tasks:
        if t.id not in result:
            result[t.id] = (None, None, t.thumbnail_path, t.blurhash, None)

    return result


async def _submission_assignment_start(
    db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> datetime:
    """Preserve the actor's current work start before submit/skip releases it."""

    from app.db.models.task_lock import TaskLock

    started_at = await db.scalar(
        select(TaskLock.created_at).where(
            TaskLock.task_id == task_id,
            TaskLock.user_id == user_id,
            TaskLock.expire_at > now,
            TaskLock.created_at <= now,
        )
    )
    return started_at or now


async def perform_task_submit(
    db: AsyncSession,
    task: Task,
    *,
    actor: User,
    now: datetime | None = None,
) -> dict:
    """Apply the pending/in_progress → review transition for one task.

    Shared by the single-task submit endpoint and the batch submit endpoint so a
    bulk send reuses the same review-round, lock, mask-QC and contributor
    semantics as submitting a task from the Workbench. The caller owns task
    validation, the submit audit row, commit and mask-QC dispatch.
    """
    from app.services.task_lock import TaskLockService

    now = now or datetime.now(timezone.utc)
    if task.assignee_id is None:
        # Preserve the effective assignee (task-level, else the batch annotator)
        # instead of overwriting it with an owner/admin actor; fall back to the
        # actor only for genuinely unassigned open-pool tasks.
        assignee_id = await _effective_task_assignee_id(db, task) or actor.id
        task.assignee_id = assignee_id
        task.assigned_at = await _submission_assignment_start(
            db, task.id, assignee_id, now
        )

    review_round_id = _start_review_round(task)
    task.status = "review"
    task.submitted_at = now
    task.reviewer_id = None
    task.reviewer_is_override = False
    task.reviewer_claimed_at = None
    task.reviewed_at = None
    task.reject_reason = None
    task.reject_reason_type = None

    await TaskLockService(db).release(task.id, actor.id)

    mask_qc_run = None
    mask_qc_job = None
    mask_qc_created = False
    mask_qc_status: str | None = None
    has_mask = False
    from app.db.models.project import Project
    from app.services.mask_qc.config import load_mask_qc_config
    from app.services.mask_qc.service import MaskQCError, create_mask_qc_run
    from app.schemas.mask_qc import MaskQCRunRequest

    project = await db.get(Project, task.project_id)
    config = load_mask_qc_config(project.mask_qc_config if project else None)
    has_mask = (
        await db.execute(
            select(Annotation.id)
            .where(
                Annotation.task_id == task.id,
                Annotation.is_active.is_(True),
                Annotation.was_cancelled.is_(False),
                Annotation.geometry["type"].astext.in_(
                    ("raster_mask", "video_track_mask")
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    if (
        project is not None
        and has_mask
        and config.enabled
        and config.auto_run == "on_review_submit"
    ):
        try:
            mask_qc_run, mask_qc_job, mask_qc_created = await create_mask_qc_run(
                db,
                project=project,
                actor_id=actor.id,
                request=MaskQCRunRequest(scope="task_ids", task_ids=[task.id]),
            )
        except MaskQCError as exc:
            mask_qc_status = str(exc.detail.get("reason") or "enqueue_failed")
        else:
            mask_qc_status = mask_qc_run.status

    contributor_ids = await _task_contributor_snapshot(db, task)
    _capture_first_review_contributor_snapshot(task, contributor_ids)
    # A2 · freeze the round's authorization evidence atomically with the new
    # review round. Unknown accumulator stays unknown.
    freeze_review_contributor_evidence(
        task, submitter_id=actor.id, contributor_ids=contributor_ids
    )

    return {
        "review_round_id": review_round_id,
        "contributor_ids": contributor_ids,
        "review_contributor_ids": task.review_contributor_ids,
        "review_submitter_id": task.review_submitter_id,
        "mask_qc_run": mask_qc_run,
        "mask_qc_job": mask_qc_job,
        "mask_qc_created": mask_qc_created,
        "mask_qc_status": mask_qc_status,
        "has_mask": has_mask,
    }


async def _attach_image_pyramids_batch(
    db: AsyncSession,
    tasks: list[Task],
    dimensions: dict[
        uuid.UUID,
        tuple[int | None, int | None, str | None, str | None, dict | None],
    ],
) -> dict[uuid.UUID, ImagePyramidSummary]:
    from app.services.image_pyramid import pyramid_summary, task_pyramid_assets

    assets = await task_pyramid_assets(db, tasks)
    result: dict[uuid.UUID, ImagePyramidSummary] = {}
    for task in tasks:
        pair = assets.get(task.id)
        if pair is None:
            continue
        _, generation = pair
        width, height, *_ = dimensions.get(task.id, (None, None, None, None, None))
        summary = pyramid_summary(generation, width=width, height=height)
        if summary is not None:
            result[task.id] = summary
    return result
