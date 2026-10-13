"""批量编辑 router.

端点(均要求 ids 属于同一 task, 简化权限校验且符合工作台 UX):
- POST /annotations/bulk-update  批量 patch class_name / attributes / 状态位

v0.21.3 · 标注编组 (group / ungroup) 持久化已删除; 批量编辑退化为前端临时多选。

v0.10.54 · annotations import:
- POST /projects/{project_id}/annotations/import  导入 AAP JSON annotations[] (ADR-0028)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import (
    _assert_review_adjustment_evidence,
    _assert_task_editable,
    _assert_task_visible,
    _resolve_task_access,
    assert_annotation_write_allowed,
)
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    require_active_task_actor,
    require_project_owner,
    require_scopes,
)
from app.schemas.aap_json import AAPImportResult
from app.schemas._jsonb_types import CocoRleContent
from app.schemas.annotation import (
    AnnotationBulkUpdateRequest,
    AnnotationBulkUpdateResponse,
)
from app.services.annotation import AnnotationService
from app.services.audit import AuditAction, AuditService
from app.services.raster_mask_storage import (
    COCO_RLE_GZIP_ENCODING,
    RLE_STORAGE_GZIP,
    RasterMaskContractError,
    assert_raster_mask_write_enabled,
    build_rle_gzip_reference,
    build_rle_reference,
    classify_raster_mask_content_error,
    load_coco_rle,
    lock_raster_mask_references,
    reserve_raster_mask_upload,
    store_coco_rle,
    store_coco_rle_gzip,
    validate_mask_geometry_for_task,
)
from app.services.video_tracks import resolve_track_at_frame
from app.utils.raster_mask_rle import (
    MAX_IMAGE_MASK_DIMENSION,
    MAX_IMAGE_MASK_PIXELS,
    MAX_VIDEO_MASK_DIMENSION,
    MAX_VIDEO_MASK_PIXELS,
)

router = APIRouter(dependencies=[Depends(require_active_task_actor)])
logger = logging.getLogger(__name__)

# v0.23.5 · WS-D · D3 · per-task cap on unclaimed raster-mask uploads.
# The upload endpoint returns an anonymous reference (not yet linked to an
# annotation), so the per-annotation ``is_locked`` guard doesn't apply at
# upload time. Ownership rows bound anonymous objects until an annotation
# transaction claims them; the cap is serialized by a task-level advisory lock
# so concurrent requests cannot race.
MAX_MASK_OBJECTS_PER_TASK = 256


async def _count_task_mask_references(db: AsyncSession, task_id: uuid.UUID) -> int:
    """Count anonymous uploads not yet claimed by an annotation transaction."""
    return int(
        (
            await db.execute(
                select(func.count(RasterMaskUpload.id)).where(
                    RasterMaskUpload.task_id == task_id,
                    RasterMaskUpload.linked_at.is_(None),
                )
            )
        ).scalar()
        or 0
    )


_MASK_CONTENT_RESPONSES = {
    304: {"description": "Not Modified"},
    409: {"description": "Mask reference or task context is invalid"},
    503: {"description": "Mask object storage is unavailable"},
}


def _mask_etag_matches(request: Request, etag: str) -> bool:
    raw = request.headers.get("if-none-match")
    if not raw:
        return False
    normalized = etag.removeprefix("W/")
    return any(
        candidate == "*" or candidate.removeprefix("W/") == normalized
        for candidate in (part.strip() for part in raw.split(","))
    )


async def _load_visible_mask_annotation(
    annotation_id: uuid.UUID,
    db: AsyncSession,
    user: User,
) -> tuple[Annotation, Task, dict]:
    annotation = await db.get(Annotation, annotation_id)
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="annotation not found")
    task = await db.get(Task, annotation.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    await _assert_task_visible(db, task, user)
    geometry = annotation.geometry or {}
    if geometry.get("type") == "raster_mask" and not settings.raster_mask_read_enabled:
        raise HTTPException(
            status_code=404, detail={"reason": "raster_mask_read_disabled"}
        )
    try:
        await validate_mask_geometry_for_task(db, task, geometry)
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "mask_task_context_invalid", "message": str(exc)},
        ) from exc
    return annotation, task, geometry


async def _mask_content_response(
    *,
    annotation_id: uuid.UUID,
    mask_ref: dict,
    request: Request,
) -> Response:
    etag = f'"{mask_ref.get("sha256")}"'
    # Annotation and frame URLs are mutable aliases, including slice restore.
    # Revalidate the current reference before reusing an earlier response body.
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if _mask_etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    try:
        payload = await load_coco_rle(mask_ref)
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": classify_raster_mask_content_error(exc),
                "retryable": True,
                "message": f"mask object is invalid: {exc}",
            },
        ) from exc
    except Exception as exc:
        logger.warning(
            "mask object read failed for annotation %s", annotation_id, exc_info=True
        )
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "mask_storage_unavailable",
                "retryable": True,
                "message": "Mask object storage is unavailable",
            },
        ) from exc
    return JSONResponse(payload, headers=headers)


@router.get(
    "/annotations/{annotation_id}/mask-content",
    response_model=CocoRleContent,
    responses=_MASK_CONTENT_RESPONSES,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_annotation_mask_content(
    annotation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """v0.23.6 · 获取图片掩码内容 (RasterMaskGeometry)。

    支持图片 RasterMaskGeometry 与视频单帧 VideoMaskGeometry 的静态内容服务。
    返回标准 COCO RLE 格式，带 ETag 支持条件请求。
    """
    annotation, _task, geometry = await _load_visible_mask_annotation(
        annotation_id, db, user
    )
    geom_type = geometry.get("type")

    if geom_type in {"raster_mask", "video_mask"}:
        mask_ref = geometry.get("mask")
        if not mask_ref:
            raise HTTPException(status_code=409, detail="mask reference missing")
        return await _mask_content_response(
            annotation_id=annotation.id,
            mask_ref=mask_ref,
            request=request,
        )
    raise HTTPException(
        status_code=422, detail=f"unsupported geometry type: {geom_type}"
    )


@router.get(
    "/annotations/{annotation_id}/mask-content/{frame_index}",
    response_model=CocoRleContent,
    responses=_MASK_CONTENT_RESPONSES,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_annotation_mask_content_frame(
    annotation_id: uuid.UUID,
    frame_index: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    annotation, _task, geometry = await _load_visible_mask_annotation(
        annotation_id, db, user
    )
    if geometry.get("type") in {"raster_mask", "video_mask"}:
        if (
            geometry.get("type") == "video_mask"
            and geometry.get("frame_index") != frame_index
        ):
            raise HTTPException(status_code=404, detail="mask is outside at this frame")
        mask_ref = geometry.get("mask")
        if not mask_ref:
            raise HTTPException(status_code=409, detail="mask reference missing")
        return await _mask_content_response(
            annotation_id=annotation.id,
            mask_ref=mask_ref,
            request=request,
        )
    if geometry.get("type") != "video_track_mask":
        raise HTTPException(
            status_code=422, detail="annotation is not a video mask track"
        )
    resolved = resolve_track_at_frame(geometry, frame_index)
    if resolved is None:
        raise HTTPException(status_code=404, detail="mask is outside at this frame")
    return await _mask_content_response(
        annotation_id=annotation.id,
        mask_ref=resolved["mask"],
        request=request,
    )


@router.post(
    "/tasks/{task_id}/mask-content",
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def upload_task_mask_content(
    task_id: uuid.UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    # Match all Mask writers: the Task row is the first database row lock.
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    assert_annotation_write_allowed(task, access)
    await _assert_review_adjustment_evidence(db, task, user, access)
    await _assert_task_visible(db, task, user, access=access)
    _assert_task_editable(task, user, access=access)
    if (
        access.project_role == "annotator"
        and task.assignee_id is not None
        and task.assignee_id != user.id
    ):
        raise HTTPException(status_code=403, detail="Task belongs to another annotator")
    # v0.23.5 · WS-D · D3 · per-task mask-object quota. The upload returns an
    # anonymous reference (not yet linked to an annotation), so the per-annotation
    # ``is_locked`` guard doesn't apply at upload time; the quota bounds how many
    # orphan-able objects a single task can produce before the caller must link
    # or clean up existing ones. Linking the returned ref marks the reservation
    # claimed in the same annotation transaction.
    try:
        body = dict(payload)
        requested_storage = body.pop("storage_encoding", None)
        # Backward-compatible request shim. The body is normalized immediately:
        # COCO RLE encoding and object-storage encoding are separate contracts.
        if body.get("encoding") == COCO_RLE_GZIP_ENCODING:
            requested_storage = RLE_STORAGE_GZIP
            body["encoding"] = "coco_rle"
        if requested_storage not in (None, "identity", RLE_STORAGE_GZIP):
            raise ValueError("storage_encoding must be 'identity' or 'gzip'")
        size = payload.get("size")
        item = (
            await db.get(DatasetItem, task.dataset_item_id)
            if task.dataset_item_id is not None
            else None
        )
        source_file_type = item.file_type if item is not None else task.file_type
        if source_file_type != "video":
            project = await db.get(Project, task.project_id)
            assert_raster_mask_write_enabled(project)
        if (
            isinstance(size, list)
            and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
        ):
            mask_height, mask_width = size
            max_dimension = (
                MAX_VIDEO_MASK_DIMENSION
                if source_file_type == "video"
                else MAX_IMAGE_MASK_DIMENSION
            )
            max_pixels = (
                MAX_VIDEO_MASK_PIXELS
                if source_file_type == "video"
                else MAX_IMAGE_MASK_PIXELS
            )
            if (
                mask_height > max_dimension
                or mask_width > max_dimension
                or mask_height * mask_width > max_pixels
            ):
                reason = (
                    "video_mask_dimensions_exceeded"
                    if source_file_type == "video"
                    else "raster_mask_dimensions_exceeded"
                )
                raise RasterMaskContractError(
                    status_code=422,
                    reason=reason,
                    message=(
                        "mask content exceeds the media limit "
                        f"({max_dimension}px / {max_pixels} pixels)"
                    ),
                    max_dimension=max_dimension,
                    max_pixels=max_pixels,
                )
        if item is not None:
            video = (item.metadata_ or {}).get("video")
            video = video if isinstance(video, dict) else {}
            width = item.width or video.get("width")
            height = item.height or video.get("height")
            if width and height and size != [int(height), int(width)]:
                raise ValueError(
                    f"mask size must match source media [{height}, {width}]"
                )
        # HTTP Content-Encoding is handled before JSON parsing. This field is
        # only the object-storage preference; reference encoding stays stable.
        if requested_storage == RLE_STORAGE_GZIP:
            try:
                expected = build_rle_gzip_reference(body)
            except ValueError as exc:
                if "expansion ratio" not in str(exc):
                    raise
                expected = build_rle_reference(body)
        else:
            expected = build_rle_reference(body)
        # Hold the same content lock used by GC from reservation through object
        # storage and commit. This closes the window where GC could miss an
        # uncommitted reservation and delete a concurrently uploaded object.
        await lock_raster_mask_references(db, expected, verify=False)
        try:
            await reserve_raster_mask_upload(
                db,
                task_id=task_id,
                object_key=expected["object_key"],
                limit=MAX_MASK_OBJECTS_PER_TASK,
            )
        except ValueError:
            current = await _count_task_mask_references(db, task_id)
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "mask_quota_exceeded",
                    "limit": MAX_MASK_OBJECTS_PER_TASK,
                    "current": current,
                },
            ) from None
        try:
            reference = (
                await store_coco_rle_gzip(body)
                if requested_storage == RLE_STORAGE_GZIP
                else await store_coco_rle(body)
            )
        except Exception:
            # Reservation and quota consumption are transactional. A storage
            # failure must not leave a phantom owner row.
            await db.rollback()
            raise
        if reference["object_key"] != expected["object_key"]:
            raise RuntimeError("mask storage selection changed after reservation")
        await db.commit()
        return reference
    except HTTPException:
        raise
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _load_single_task_for_ids(
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> Task:
    """所有 ids 必须属于同一 task; 否则 422.
    返回该 task (供 _assert_task_editable 与 project_visible 校验)."""
    if not ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")
    rows = (
        await db.execute(
            select(Annotation.id, Annotation.task_id).where(Annotation.id.in_(ids))
        )
    ).all()
    if len(rows) != len(ids):
        missing = set(ids) - {r.id for r in rows}
        raise HTTPException(
            status_code=404,
            detail=f"annotations not found: {sorted(str(m) for m in missing)}",
        )
    task_ids = {r.task_id for r in rows}
    if len(task_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="bulk operation requires all annotations belong to a single task",
        )
    task_id = next(iter(task_ids))
    # Lock the task row before any capability/evidence check so the decision is
    # made on the current row and concurrent writers are serialized (the bulk
    # UPDATE itself also takes this lock later).
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return task


@router.post(
    "/annotations/bulk-update",
    response_model=AnnotationBulkUpdateResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def bulk_update_annotations(
    payload: AnnotationBulkUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """I12 · 批量更新 N 个标注. 失败整体回滚 (单事务).

    设计取舍:
    - 不允许 bulk 改 geometry (同一 geometry 应用到 N 个 shape 无意义)
    - 不允许 bulk 改 tool_unit_id (会破坏 class_name 校验链)
    """
    task = await _load_single_task_for_ids(db, payload.ids)
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    assert_annotation_write_allowed(task, access)
    await _assert_review_adjustment_evidence(db, task, user, access)
    await _assert_task_visible(db, task, user, access=access)
    _assert_task_editable(task, user, access=access)

    from app.services.annotation_evidence import record_annotation_actor

    # A2 · take the task row lock before the bulk annotation UPDATEs.
    await record_annotation_actor(db, task, user.id)
    service = AnnotationService(db)
    updated = await service.bulk_update(
        payload.ids,
        class_name=payload.patch.class_name,
        attributes=payload.patch.attributes,
        z_order=payload.patch.z_order,
        is_locked=payload.patch.is_locked,
        is_hidden=payload.patch.is_hidden,
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.ANNOTATION_BULK_UPDATE,
        target_type="task",
        target_id=task.id,
        request=request,
        status_code=200,
        detail={
            "annotation_ids": [str(i) for i in payload.ids],
            "patch": payload.patch.model_dump(exclude_none=True),
            "count": len(updated),
        },
    )
    await db.commit()
    return AnnotationBulkUpdateResponse(
        updated_ids=[a.id for a in updated],
        updated_count=len(updated),
    )


# v0.21.3 · 标注编组(Ctrl+G)持久化已删除:group/ungroup 端点下线,批量编辑退化为
# 前端临时多选(bulk-update 保留)。跨帧同一对象走 track_id(ADR-0045),不再用 group_id。


# ── v0.10.54 · annotations import (ADR-0028) ──────────────────────────


@router.post(
    "/projects/{project_id}/annotations/import",
    response_model=AAPImportResult,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def import_annotations(
    request: Request,
    file: UploadFile = File(...),
    format: str = Query("aap_json", pattern="^(aap_json)$"),
    dry_run: bool = Query(False),
    overwrite: bool = Form(False),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AAPImportResult:
    """导入 AAP JSON envelope 中的 annotations[] 数组 (v0.10.54, ADR-0028).

    geometry 透传内部格式（无需 LS 转换），user_id 归当前操作者，
    source 保留原值，attributes._imported=true 溯源标记。
    """
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal
    from app.services.annotations_import import import_aap_json_annotations

    max_import_bytes = 64 * 1024 * 1024
    raw = await file.read(max_import_bytes + 1)
    if len(raw) > max_import_bytes:
        raise HTTPException(status_code=413, detail="AAP JSON import must be <= 64 MiB")

    # async_jobs 双写（dry_run 不记录）
    aj_id: uuid.UUID | None = None
    if not dry_run:
        try:
            aj = await async_job_svc.create_job(
                db,
                kind="annotations_import",
                project_id=project.id,
                user_id=current_user.id,
                payload={
                    "format": format,
                    "size_bytes": len(raw),
                    "project_display_id": project.display_id,
                    "overwrite": overwrite,
                },
            )
            await async_job_svc.mark_running(db, aj.id)
            await db.commit()
            aj_id = aj.id
        except Exception:
            await db.rollback()
            aj_id = None

    try:
        result = await import_aap_json_annotations(
            db,
            project.id,
            raw,
            operator_user_id=current_user.id,
            overwrite=overwrite,
            dry_run=dry_run,
        )
    except ValueError as exc:
        if aj_id is not None:
            try:
                await async_job_svc.mark_failed(db, aj_id, error=str(exc))
                await notify_job_terminal(db, job_id=aj_id)
                await db.commit()
            except Exception:
                await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        if aj_id is not None:
            try:
                await async_job_svc.mark_failed(db, aj_id, error=str(exc))
                await notify_job_terminal(db, job_id=aj_id)
                await db.commit()
            except Exception:
                await db.rollback()
        raise

    if not dry_run:
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.ANNOTATION_IMPORT,
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail={
                "format": format,
                "project_display_id": project.display_id,
                "imported": result.imported,
                "skipped": result.skipped,
                "error_count": len(result.errors),
                "overwrite": overwrite,
            },
        )
        if aj_id is not None:
            try:
                await async_job_svc.mark_complete(
                    db,
                    aj_id,
                    result={
                        "imported": result.imported,
                        "skipped": result.skipped,
                        "error_count": len(result.errors),
                    },
                )
                await notify_job_terminal(db, job_id=aj_id)
            except Exception:
                pass
        await db.commit()

    return result
