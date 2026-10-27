import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from aap_assets_media.contract import MediaFailure, ReadSourceRequest
from aap_assets_images.contract import ReadImageSourceRequest

from app.db.enums import UserRole
from app.db.models.user import User
from app.db.models.task import Task
from app.schemas.task import (
    TaskOut,
    VideoMetadata,
)
from app.schemas.image_pyramid import ImagePyramidSummary
from app.deps import get_current_user, get_db
from app.services import task_access
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectCapability,
)
from app.services.project_access import resolve_project_access
from app.services.storage import storage_service
from app.services.assets_media_summary import AssetsTaskMediaSummaryReader
from app.services.assets_media_summary_contract import TaskMediaSummary
from app.services.assets_media import browser_media_url, media_gateway
from app.services.image_source_references import image_source_references
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import get_core_row, task_row

logger = logging.getLogger(__name__)


async def _linked_source_references(
    db: AsyncSession,
    tasks: list[Task],
    *,
    actor_id: uuid.UUID,
) -> dict[uuid.UUID, Any]:
    """Resolve owner references, including direct legacy image Tasks."""
    images = [task for task in tasks if task.file_type == "image"]
    image_references = await image_source_references(
        db,
        [ReadImageSourceRequest(actor_id=actor_id, task_id=task.id) for task in images],
    )
    found = {task.id: ref for task, ref in zip(images, image_references, strict=True)}
    selected = [
        task
        for task in tasks
        if task.dataset_item_id is not None and task.file_type != "image"
    ]
    if not selected:
        return found
    requests = [
        ReadSourceRequest(
            actor_id=actor_id,
            dataset_item_id=task.dataset_item_id,
            task_id=task.id,
        )
        for task in selected
    ]
    try:
        async with media_gateway(db) as gateway:
            references = await gateway.source_references(requests, raw_only=True)
    except MediaFailure:
        return found | {task.id: None for task in selected}
    if len(references) != len(selected) or any(
        reference.source.dataset_item_id != task.dataset_item_id
        for task, reference in zip(selected, references, strict=False)
    ):
        return found | {task.id: None for task in selected}
    return found | {
        task.id: reference for task, reference in zip(selected, references, strict=True)
    }


VIDEO_MANIFEST_URL_EXPIRES_IN = 3600

_ANNOTATORS = (
    UserRole.SUPER_ADMIN,
    UserRole.PROJECT_ADMIN,
    UserRole.REVIEWER,
    UserRole.ANNOTATOR,
)
_REVIEWERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN, UserRole.REVIEWER)


async def _load_task_or_404(db: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await get_core_row(db, task_row, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _assert_current_project_member(db: AsyncSession, project, user: User) -> None:
    """Assert the account has any valid access (manager or member)."""

    await resolve_project_access(
        db, actor_id=user.id, project=project, lock_membership=True
    )


async def require_task_annotation_write(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
) -> ProjectAccess:
    """FastAPI dependency: annotation write authority for ``task_id``.

    The membership is acquired ``FOR SHARE`` so a concurrent member role change
    (``FOR UPDATE``) cannot commit while this authorized mutation is in flight.
    Reviewers are admitted only for a review adjustment; the service-layer
    annotation-write guard handles all other capability combinations.
    """

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(
        db, task, user.id, lock_membership=True
    )
    task_access.assert_annotation_write_allowed(task, access)
    await task_access.assert_review_adjustment_evidence(db, task, user.id, access)
    return access


async def require_task_annotation_write_strict(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
) -> ProjectAccess:
    """Annotation write authority without the reviewer review-adjustment path."""

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(
        db, task, user.id, lock_membership=True
    )
    task_access.assert_annotation_write_allowed(
        task, access, allow_review_adjustment=False
    )
    await task_access.assert_review_adjustment_evidence(db, task, user.id, access)
    return access


async def require_task_annotation_write_lifecycle(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
) -> ProjectAccess:
    """Annotation-phase capability for a task *lifecycle* transition.

    ``submit``/``skip``/``withdraw``/``reopen``/``accept-rejection`` move a task
    between phases; they are not annotation-content edits, so the frozen
    non-self review-evidence guard does not apply.  The capability and the
    ``FOR SHARE`` membership are still required.
    """

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(
        db, task, user.id, lock_membership=True
    )
    task_access.assert_annotation_write_allowed(task, access)
    return access


async def require_task_review_write(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
) -> ProjectAccess:
    """FastAPI dependency: review write authority for ``task_id``."""

    task = await _load_task_or_404(db, task_id)
    access = await task_access.resolve_task_access(
        db, task, user.id, lock_membership=True
    )
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
    owner_source_reference: Any = None,
    owner_source_reference_loaded: bool = False,
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
    owner_source = (
        owner_source_reference.source if owner_source_reference is not None else None
    )
    owner_source_current = False
    bucket = (
        storage_service.datasets_bucket
        if task.dataset_item_id or task.file_type == "point_cloud"
        else storage_service.bucket
    )
    if owner_source_reference_loaded:
        file_url = (
            browser_media_url(owner_source_reference.original_url)
            if owner_source is not None and not owner_source.source_drifted
            else None
        )
        if owner_source is not None:
            if owner_source.file_type == "video":
                owner_source_current = (
                    not owner_source.source_drifted
                    and owner_source.materialized_generation
                    == owner_source.source_generation
                )
                video_metadata = (
                    owner_source.video_metadata if owner_source_current else None
                )
                if owner_source_current:
                    width = owner_source.width or width
                    height = owner_source.height or height
                else:
                    width = None
                    height = None
            else:
                video_metadata = None
                if owner_source.file_type == "image":
                    width, height = owner_source.width, owner_source.height
                    blurhash = (
                        owner_source.blurhash
                        if owner_source.thumbnail_current
                        else None
                    )
        else:
            video_metadata = None
            if task.file_type == "image":
                width, height, blurhash = None, None, None
    else:
        try:
            file_url = storage_service.generate_download_url(
                task.file_path, bucket=bucket
            )
        except Exception:
            file_url = None

    thumbnail_url: str | None = None
    if owner_source_reference_loaded:
        if owner_source is not None and owner_source.file_type == "video":
            thumbnail_url = (
                browser_media_url(owner_source_reference.poster_url)
                if owner_source_current
                else None
            )
        elif owner_source is not None and owner_source.file_type == "image":
            thumbnail_url = browser_media_url(owner_source_reference.thumbnail_url)
        elif owner_source is not None and thumbnail_path:
            thumb_bucket = storage_service.bucket_for_cache_key(
                thumbnail_path, default=bucket
            )
            try:
                thumbnail_url = storage_service.generate_download_url(
                    thumbnail_path, bucket=thumb_bucket
                )
            except Exception:
                pass
    elif thumbnail_path:
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

    if task.file_type == "video":
        checked = getattr(task, "_video_derivative_thumbnail", None)
        if thumbnail_path and checked is not None and checked[0] == thumbnail_path:
            thumbnail_url = checked[1]
        blurhash = None
    elif thumbnail_path:
        # The asynchronous sidecar checks the exact active owner and path.
        for name in ("_image_pyramid_thumbnail", "_media_cache_thumbnail"):
            checked = getattr(task, name, None)
            if checked is not None and checked[0] == thumbnail_path:
                thumbnail_url = checked[1]
                break

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
) -> TaskMediaSummary:
    if task.dataset_item_id:
        summary = await AssetsTaskMediaSummaryReader(db).one(task.dataset_item_id)
        if summary is not None:
            return summary
    return None, None, task.thumbnail_path, task.blurhash, None


async def _video_frame_count(db: AsyncSession, task: Task) -> int | None:
    if not task.dataset_item_id:
        return None
    return await AssetsTaskMediaSummaryReader(db).video_frame_count(
        task.dataset_item_id
    )


async def _attach_dimensions_batch(
    db: AsyncSession,
    tasks: list[Task],
) -> dict[uuid.UUID, TaskMediaSummary]:
    result: dict[uuid.UUID, TaskMediaSummary] = {}

    item_ids = [t.dataset_item_id for t in tasks if t.dataset_item_id]
    if item_ids:
        item_data = await AssetsTaskMediaSummaryReader(db).many(tuple(item_ids))
        for t in tasks:
            if t.dataset_item_id:
                result[t.id] = item_data.get(
                    t.dataset_item_id, (None, None, None, None, None)
                )

    for t in tasks:
        if t.id not in result:
            result[t.id] = (None, None, t.thumbnail_path, t.blurhash, None)

    return result


async def _attach_image_pyramids_batch(
    db: AsyncSession,
    tasks: list[Task],
    dimensions: dict[
        uuid.UUID,
        tuple[int | None, int | None, str | None, str | None, dict | None],
    ],
) -> dict[uuid.UUID, ImagePyramidSummary]:
    from app.services.image_pyramid import pyramid_summary, task_pyramid_assets
    from app.services.image_pyramid_objects import thumbnail_url
    from app.services import thumbnail_outputs

    assets = await task_pyramid_assets(db, tasks)
    result: dict[uuid.UUID, ImagePyramidSummary] = {}
    for task in tasks:
        width, height, path, *_ = dimensions.get(
            task.id, (None, None, None, None, None)
        )
        task._image_pyramid_thumbnail = None
        task._media_cache_thumbnail = None
        task._video_derivative_thumbnail = None
        if task.file_type == "video":
            from app.services import video_derivatives

            checked = await video_derivatives.thumbnail(db, task_id=task.id)
            if checked is not None and checked[0] == path:
                task._video_derivative_thumbnail = checked
        elif path:
            pair = assets.get(task.id)
            if (
                pair is not None and getattr(pair[1], "overview_key", None) == path
            ) or path.startswith("image-pyramids/"):
                task._image_pyramid_thumbnail = (
                    path,
                    await thumbnail_url(db, "task", task.id, path),
                )
            else:
                task._media_cache_thumbnail = (
                    path,
                    await thumbnail_outputs.thumbnail_url(db, "task", task.id, path),
                )
        pair = assets.get(task.id)
        if pair is None:
            continue
        _, generation = pair
        summary = pyramid_summary(generation, width=width, height=height)
        if summary is not None:
            result[task.id] = summary
    return result
