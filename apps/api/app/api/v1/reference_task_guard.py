"""Resolve immutable original authorities at the task route boundary."""

import re
import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.deps import get_current_user, get_db
from app.services.dataset_content import content_http_error
from app.services.dataset_references import reference_snapshot
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    lock_task_for_core_read,
    prediction_row,
    task_row,
)
from app.services.reference_access import reject_reference_scope
from app.services.reference_keys import is_reference, parse_reference


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
            await get_core_row(
                db, task_row, uuid.UUID(str(task_id)), populate_existing=True
            )
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
            return
        task = await db.scalar(
            select(task_row)
            .join(annotation_row, annotation_row.task_id == task_row.id)
            .where(annotation_row.id.in_(ids))
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
        from app.services.ai_mask_receipt import AiMaskAcceptError

        reference = is_reference(task.file_path)
        if reference:
            from app.services.reference_inference import qualify_native_acceptance
        else:
            from app.services.managed_native_inference import qualify_native_acceptance

        try:
            data = AiMaskAcceptRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            return
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
        video_operation = re.fullmatch(
            r"/api/v1/tasks/[^/]+/predictions/[^/]+/(accept|reject)", request.url.path
        )
        if task.file_type == "video" and request.method == "POST" and video_operation:
            from app.services import video_prediction_acceptance

            try:
                prediction_id = uuid.UUID(str(request.path_params["prediction_id"]))
            except (ValueError, TypeError, KeyError):
                return
            try:
                prediction, metadata = await video_prediction_acceptance._candidate(
                    db, task.id, prediction_id
                )
                if video_prediction_acceptance.requires_qualification(
                    prediction, metadata
                ):
                    if video_operation[1] == "accept":
                        request.state.video_prediction_acceptance = (
                            await video_prediction_acceptance.qualify_acceptance(
                                db, task, actor, request, prediction_id
                            )
                        )
                    else:
                        request.state.video_prediction_rejection = (
                            await video_prediction_acceptance.qualify_rejection(
                                db, task, actor, request, prediction_id
                            )
                        )
            except Exception as exc:
                raise content_http_error(exc) from None
            return
        if (
            task.file_type == "image"
            and request.method == "POST"
            and re.fullmatch(
                r"/api/v1/tasks/[^/]+/predictions/[^/]+/accept", request.url.path
            )
        ):
            from app.services.managed_repair_acceptance import qualify_acceptance

            try:
                prediction_id = uuid.UUID(str(request.path_params["prediction_id"]))
            except (ValueError, TypeError):
                return
            prediction = await db.scalar(
                select(prediction_row).where(
                    prediction_row.id == prediction_id,
                    prediction_row.task_id == task.id,
                )
            )
            if prediction is not None and prediction.source == "mask_repair":
                try:
                    request.state.repair_original_acceptance = await qualify_acceptance(
                        db, task, actor, request, prediction_id
                    )
                except Exception as exc:
                    from sqlalchemy.exc import DBAPIError
                    from app.services.project_write_guard import _busy_lock_error

                    if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
                        await db.rollback()
                        raise HTTPException(
                            409, detail={"code": "prediction_acceptance_busy"}
                        ) from None
                    from app.services.managed_mask_repair import repair_http_error

                    raise repair_http_error(exc) from None
        return
    if request.method == "POST" and re.fullmatch(
        r"/api/v1/tasks/[^/]+/predictions/[^/]+/accept", request.url.path
    ):
        from app.services.prediction_acceptance import qualify_acceptance

        try:
            prediction_id = uuid.UUID(str(request.path_params["prediction_id"]))
        except (ValueError, TypeError):
            return

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
    from app.services import task_access
    from app.services.dataset_content import key_authority

    history = request.method == "POST" and re.fullmatch(
        r"/api/v1/tasks/[^/]+/acceptance-history/[^/]+/restore", request.url.path
    )
    scope = (
        "annotations:write"
        if "/annotations" in request.url.path or history
        else "tasks:write"
    )
    key_id = getattr(request.state, "api_key_id", None)
    key_proof = await key_authority(db, actor, key_id, scope)
    await task_access.resolve_task_access(db, task, actor.id, lock_membership=True)
    from app.services.task_lock import TaskLockService

    await TaskLockService(db)._lock_task_scope(task.id)
    task = await lock_task_for_core_read(db, task.id, populate_existing=True)
    if task is None:
        raise HTTPException(404, "Task not found")
    await task_access.assert_task_visible(db, task, actor.id)
    if await key_authority(db, actor, key_id, scope) != key_proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    path = request.url.path
    if request.method == "DELETE" and path.endswith("/lock"):
        return
    manual = re.fullmatch(
        r"/api/v1/tasks/[^/]+/(annotations(?:/[0-9a-f-]{36})?|submit|skip|withdraw|reopen|accept-rejection|lock(?:/heartbeat)?)",
        path,
    )
    bulk = path.endswith("/annotations/bulk-update")
    if not manual and not bulk and not history:
        await reject_reference_scope(db, task_ids=[task.id])
    try:
        item_id, sha = parse_reference(task.file_path)
        scope = (
            "annotations:write" if "/annotations" in path or history else "tasks:write"
        )
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
        from sqlalchemy.exc import DBAPIError

        try:
            async with db.begin_nested():
                await db.execute(
                    text(
                        "SELECT yoao_assets_v1.lock_directory_reference_bindings("
                        "ARRAY[]::uuid[], CAST(:locations AS uuid[]), "
                        "CAST(:sources AS uuid[]), ARRAY[]::uuid[])"
                    ),
                    {
                        "locations": [context["location_id"]],
                        "sources": [context["source_id"]],
                    },
                )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            raise HTTPException(409, detail={"code": "source_busy"}) from None
        await storage_access.check(
            db,
            context["location_id"],
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
