"""Early reference gates for supported manual task operations."""

import re
import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_current_user, get_db
from app.services.dataset_content import content_http_error
from app.services.dataset_references import reference_snapshot
from app.services.reference_keys import is_reference, parse_reference, PREFIX


async def reject_reference_scope(
    db, *, project_id=None, task_ids=None, item_ids=None, dataset_id=None
):
    from app.db.models.dataset import DatasetItem

    if item_ids is not None or dataset_id is not None:
        stmt = select(DatasetItem.id).where(
            DatasetItem.file_path.startswith(PREFIX, autoescape=True)
        )
        if item_ids is not None:
            stmt = stmt.where(DatasetItem.id.in_(item_ids))
        if dataset_id is not None:
            stmt = stmt.where(DatasetItem.dataset_id == dataset_id)
    else:
        stmt = select(Task.id).where(Task.file_path.startswith(PREFIX, autoescape=True))
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if task_ids is not None:
            stmt = stmt.where(Task.id.in_(task_ids))
    if await db.scalar(stmt.limit(1)):
        raise HTTPException(
            409,
            detail={
                "code": "reference_operation_not_supported",
                "message": "此操作尚未接入原地引用图片，请使用手动标注与严格原图读取",
            },
        )


async def reference_task_guard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    task_id = request.path_params.get("task_id")
    try:
        task = (
            await db.get(Task, uuid.UUID(str(task_id)), populate_existing=True)
            if task_id
            else None
        )
    except ValueError:
        return
    if task is None and request.url.path.endswith("/annotations/bulk-update"):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return
            ids = [uuid.UUID(str(v)) for v in payload.get("ids", [])]
        except (TypeError, ValueError):
            return  # The endpoint's schema owns validation errors.
        task = await db.scalar(
            select(Task)
            .join(Annotation, Annotation.task_id == Task.id)
            .where(Annotation.id.in_(ids))
        )
    if task is None:
        return
    if (
        request.method == "POST"
        and request.url.path.endswith("/ai-mask-candidates/accept")
        and (is_reference(task.file_path) or task.file_type == "image")
    ):
        from pydantic import ValidationError
        from app.schemas.ai_mask import AiMaskAcceptRequest
        from app.services.ai_mask_accept import AiMaskAcceptError

        reference = is_reference(task.file_path)
        if reference:
            from app.services.reference_inference import qualify_native_acceptance
        else:
            from app.services.managed_native_inference import qualify_native_acceptance

        try:
            data = AiMaskAcceptRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            return  # The endpoint owns request-schema errors.
        try:
            setattr(
                request.state,
                "reference_native_acceptance"
                if reference
                else "original_native_acceptance",
                await qualify_native_acceptance(db, task, actor, request, data),
            )
        except AiMaskAcceptError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from None
        except Exception as exc:
            from sqlalchemy.exc import DBAPIError
            from app.services.project_write_guard import _busy_lock_error

            if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
                await db.rollback()
                raise HTTPException(
                    409,
                    detail={
                        "code": "reference_inference_busy"
                        if reference
                        else "managed_inference_busy"
                    },
                ) from None
            if not reference:
                from app.services.managed_image_inference import (
                    managed_inference_http_error,
                )

                raise managed_inference_http_error(exc) from None
            raise content_http_error(exc) from None
        return
    if not is_reference(task.file_path):
        return
    if request.method == "POST" and re.fullmatch(
        r"/api/v1/tasks/[^/]+/predictions/[^/]+/accept", request.url.path
    ):
        from app.services.prediction_acceptance import qualify_acceptance

        try:
            prediction_id = uuid.UUID(str(request.path_params["prediction_id"]))
        except (ValueError, TypeError):
            return  # Route parameter validation owns malformed UUIDs.

        try:
            request.state.reference_acceptance = await qualify_acceptance(
                db,
                task,
                actor,
                request,
                prediction_id,
            )
        except Exception as exc:
            from sqlalchemy.exc import DBAPIError
            from app.services.project_write_guard import _busy_lock_error

            if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
                await db.rollback()
                raise HTTPException(
                    409, detail={"code": "prediction_acceptance_busy"}
                ) from None
            raise content_http_error(exc) from None
        return
    if request.method == "POST" and re.fullmatch(
        r"/api/v1/tasks/[^/]+/predictions/[^/]+/reject", request.url.path
    ):
        from app.services.prediction_acceptance import qualify_rejection

        try:
            prediction_id = uuid.UUID(str(request.path_params["prediction_id"]))
        except (ValueError, TypeError):
            return
        try:
            await qualify_rejection(db, task, actor, request, prediction_id)
        except Exception as exc:
            from sqlalchemy.exc import DBAPIError
            from app.services.project_write_guard import _busy_lock_error

            if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
                await db.rollback()
                raise HTTPException(
                    409, detail={"code": "prediction_acceptance_busy"}
                ) from None
            raise content_http_error(exc) from None
        return
    from app.api.v1.tasks._shared import _assert_task_visible, _resolve_task_access
    from app.services.dataset_content import key_authority

    scope = "annotations:write" if "/annotations" in request.url.path else "tasks:write"
    key_id = getattr(request.state, "api_key_id", None)
    key_proof = await key_authority(db, actor, key_id, scope)
    await _resolve_task_access(db, task, actor, lock_membership=True)
    from app.services.task_lock import TaskLockService

    # Dispatch also takes this advisory lock before TaskLock's Task FK lock.
    await TaskLockService(db)._lock_task_scope(task.id)
    task = await db.scalar(
        select(Task)
        .where(Task.id == task.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise HTTPException(404, "Task not found")
    await _assert_task_visible(db, task, actor)
    if await key_authority(db, actor, key_id, scope) != key_proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    path = request.url.path
    # Releasing an edit lease must remain possible when its source goes offline.
    if request.method == "DELETE" and path.endswith("/lock"):
        return
    manual = re.fullmatch(
        r"/api/v1/tasks/[^/]+/(annotations(?:/[0-9a-f-]{36})?|submit|skip|withdraw|reopen|accept-rejection|lock(?:/heartbeat)?)",
        path,
    )
    bulk = path.endswith("/annotations/bulk-update")
    if not manual and not bulk:
        await reject_reference_scope(db, task_ids=[task.id])
    try:
        item_id, sha = parse_reference(task.file_path)
        scope = "annotations:write" if "/annotations" in path else "tasks:write"
        from app.services.dataset_references import reference_context
        from app.services import storage_access

        context = await reference_context(db, item_id, actor, task_id=task.id)
        snapshot = await reference_snapshot(
            db,
            item_id,
            actor,
            expected_sha256=sha,
            task_id=task.id,
            api_key_id=getattr(request.state, "api_key_id", None),
            scope=scope,
        )
        snapshot.close()
        from app.db.models.dataset_source import DatasetReference, DatasetSource
        from app.db.models.dataset_asset import StorageLocation
        from sqlalchemy.exc import DBAPIError

        ref = await db.get(DatasetReference, item_id)
        source = await db.get(DatasetSource, ref.source_id)
        # A lifecycle commit cannot overtake an already verified write. Never
        # wait while holding Task: source writers may be creating linked tasks.
        try:
            async with db.begin_nested():
                await db.execute(
                    select(StorageLocation.id)
                    .where(StorageLocation.id == source.location_id)
                    .with_for_update(read=True, nowait=True)
                )
                await db.execute(
                    select(DatasetSource.id)
                    .where(DatasetSource.id == source.id)
                    .with_for_update(read=True, nowait=True)
                )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            raise HTTPException(409, detail={"code": "source_busy"}) from None
        await storage_access.check(
            db,
            source.location_id,
            expected_revision=context["maintenance_revision"],
            lock=True,
        )
        if await reference_context(db, item_id, actor, task_id=task.id) != context:
            raise HTTPException(409, detail={"code": "content_binding_changed"})
        if await key_authority(db, actor, key_id, scope) != key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        request.state.reference_verified_task_id = task.id
    except Exception as exc:
        raise content_http_error(exc) from None
