import base64
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
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
from app.services import task_access
from app.services.project_access import (
    ProjectAccess,
    ProjectCapability,
    resolve_project_access,
)
from app.services.storage import storage_service
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


async def require_task_annotation_write(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """FastAPI dependency: annotation write authority for ``task_id``.

    The membership is acquired ``FOR SHARE`` so a concurrent member role change
    (``FOR UPDATE``) cannot commit while this authorized mutation is in flight.
    Reviewers are admitted only for a review adjustment; the service-layer
    annotation-write guard handles all other capability combinations.
    """

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(db, task, user, lock_membership=True)
    task_access.assert_annotation_write_allowed(task, access)
    await task_access.assert_review_adjustment_evidence(db, task, user, access)
    return access


async def require_task_annotation_write_strict(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """Annotation write authority without the reviewer review-adjustment path."""

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(db, task, user, lock_membership=True)
    task_access.assert_annotation_write_allowed(
        task, access, allow_review_adjustment=False
    )
    await task_access.assert_review_adjustment_evidence(db, task, user, access)
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
    access = await task_access.resolve_task_access(db, task, user, lock_membership=True)
    task_access.assert_annotation_write_allowed(task, access)
    return access


async def require_task_review_write(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """FastAPI dependency: review write authority for ``task_id``."""

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(db, task, user, lock_membership=True)
    if not (
        access.is_manager or ProjectCapability.REVIEW_WRITE.value in access.capabilities
    ):
        raise HTTPException(status_code=403, detail="缺少项目权限: review.write")
    return access


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
        assignee_id = (
            await task_access.resolve_effective_task_assignee_id(db, task) or actor.id
        )
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
