from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.deps import (
    get_quality_job_dispatcher,
    get_current_user,
    get_db,
    require_project_owner,
    require_project_visible,
    require_scopes,
    require_project_capability,
)
from app.schemas.mask_format import (
    MaskFormatDescriptorOut,
    MaskFormatExportPreflightRequest,
    MaskFormatExportPreflightResponse,
    MaskFormatImportBatchOut,
    MaskFormatImportExecuteRequest,
    MaskFormatImportPreflightRequest,
    MaskFormatImportPreflightResponse,
    MaskFormatUploadInitRequest,
    MaskFormatUploadInitResponse,
)
from app.services.audit import AuditAction, AuditService
from app.services.mask_formats.catalog import registry
from app.services.mask_formats.contracts import canonical_digest
from app.services.mask_formats.service import (
    MaskFormatError,
    batch_out,
    dispatch_import,
    execute_import,
    fail_import_dispatch,
    preflight_import,
    read_import,
    record_import_dispatch_task,
    resume_import,
    staged_prefix,
)
from app.services.storage import storage_service
from aap_workspace_contract.access import ProjectAccess, ProjectCapability
from app.services.identity_contracts import IdentityActorFacts


router = APIRouter()


def _raise_format_error(exc: MaskFormatError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"reason": exc.reason, "message": exc.message, **exc.detail},
    ) from exc


@router.get(
    "/projects/{project_id}/mask-formats",
    response_model=list[MaskFormatDescriptorOut],
)
async def list_mask_formats(
    project: Project = Depends(require_project_visible),
) -> list[MaskFormatDescriptorOut]:
    return [
        adapter.descriptor.to_out()
        for adapter in registry.list(media_type=project.data_type or "image")
    ]


@router.post(
    "/projects/{project_id}/mask-formats/exports:preflight",
    response_model=MaskFormatExportPreflightResponse,
)
async def preflight_mask_format_export(
    body: MaskFormatExportPreflightRequest,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
) -> MaskFormatExportPreflightResponse:
    from app.services.exporting.video_scope import normalize_video_export_scope

    try:
        video_scope = await normalize_video_export_scope(
            db,
            actor_id=current_user.id,
            project=project,
            request=body.scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scope = {
        "project_id": str(project.id),
        "video_export_scope": video_scope.as_dict() if video_scope else None,
    }
    plans = []
    options = {
        "include_attributes": body.include_attributes,
        "video_frame_mode": body.video_frame_mode,
        "axis_frame": body.axis_frame,
        **body.options,
    }
    for format_id in sorted(set(body.targets)):
        try:
            adapter = registry.get(format_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        descriptor = adapter.descriptor
        if (
            not descriptor.export_capability.supported
            or (project.data_type or "image") not in descriptor.media_types
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "format_export_unsupported",
                    "format_id": format_id,
                },
            )
        try:
            plans.append(
                await adapter.preflight_export(
                    db,
                    project=project,
                    scope=scope,
                    options=options,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    classes = [plan.loss_class for plan in plans]
    loss_class = (
        "unsupported"
        if "unsupported" in classes
        else "lossy"
        if "lossy" in classes
        else "lossless"
    )
    losses = [loss for plan in plans for loss in plan.losses]
    warnings = [warning for plan in plans for warning in plan.warnings]
    preflight_digest = canonical_digest(
        [plan.model_dump(mode="json") for plan in plans]
    )
    return MaskFormatExportPreflightResponse(
        plans=plans,
        loss_class=loss_class,
        estimated_objects=sum(plan.estimated_objects for plan in plans),
        estimated_files=sum(plan.estimated_files for plan in plans),
        estimated_bytes=sum(plan.estimated_bytes for plan in plans),
        losses=losses,
        warnings=warnings,
        preflight_digest=preflight_digest,
    )


@router.post(
    "/projects/{project_id}/mask-formats/imports:upload-init",
    response_model=MaskFormatUploadInitResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def init_mask_format_upload(
    body: MaskFormatUploadInitRequest,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
) -> MaskFormatUploadInitResponse:
    suffix = re.sub(r"[^A-Za-z0-9._-]+", "_", body.file_name).strip("._") or "import"
    object_key = f"{staged_prefix(project.id, user.id)}{uuid.uuid4()}/{suffix}"
    upload_url = storage_service.generate_upload_url(
        object_key,
        content_type=body.content_type,
        expires_in=900,
        bucket=storage_service.import_bucket,
    )
    return MaskFormatUploadInitResponse(
        object_key=object_key,
        upload_url=upload_url,
        expires_in=900,
    )


@router.post(
    "/projects/{project_id}/mask-formats/imports:preflight",
    response_model=MaskFormatImportPreflightResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def preflight_mask_format_import(
    body: MaskFormatImportPreflightRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MaskFormatImportPreflightResponse:
    try:
        response = await preflight_import(
            db,
            project=project,
            user_id=user.id,
            format_id=body.format_id,
            staged_object_key=body.staged_object_key,
            staged_sha256=body.staged_sha256,
            mapping=body.mapping,
            options=body.options,
        )
    except MaskFormatError as exc:
        _raise_format_error(exc)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.MASK_FORMAT_IMPORT_PREFLIGHT,
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=200,
        detail={
            "format_id": body.format_id,
            "staged_sha256": body.staged_sha256,
            "plan_digest": response.plan.plan_digest,
            "loss_class": response.plan.loss_class,
        },
    )
    await db.commit()
    return response


async def _dispatch_or_fail(
    db: AsyncSession,
    *,
    batch,
    dispatcher: QualityJobDispatcher,
) -> None:
    batch_id = batch.id
    async_job_id = batch.async_job_id
    assert async_job_id is not None
    try:
        celery_task_id = await dispatch_import(batch_id, dispatcher=dispatcher)
    except MaskFormatError as exc:
        await db.rollback()
        await fail_import_dispatch(
            db,
            batch_id=batch_id,
            async_job_id=async_job_id,
            error=exc.message,
        )
        await db.commit()
        _raise_format_error(exc)
    await record_import_dispatch_task(
        db,
        batch_id=batch_id,
        async_job_id=async_job_id,
        celery_task_id=celery_task_id,
    )
    await db.commit()


@router.post(
    "/projects/{project_id}/mask-formats/imports",
    response_model=MaskFormatImportBatchOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def execute_mask_format_import(
    body: MaskFormatImportExecuteRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
) -> MaskFormatImportBatchOut:
    try:
        batch, should_dispatch = await execute_import(
            db,
            project=project,
            user_id=user.id,
            receipt=body.receipt,
            plan_digest=body.plan_digest,
            confirm_lossy=body.confirm_lossy,
        )
    except MaskFormatError as exc:
        await db.rollback()
        _raise_format_error(exc)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.MASK_FORMAT_IMPORT_EXECUTE,
        target_type="mask_format_import",
        target_id=str(batch.id),
        request=request,
        status_code=202,
        detail={"format_id": batch.format_id, "plan_digest": batch.plan_digest},
    )
    await db.commit()
    if should_dispatch:
        await _dispatch_or_fail(db, batch=batch, dispatcher=quality_dispatcher)
    return batch_out(batch)


@router.get(
    "/projects/{project_id}/mask-formats/imports/{import_id}",
    response_model=MaskFormatImportBatchOut,
)
async def get_mask_format_import(
    import_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> MaskFormatImportBatchOut:
    batch = await read_import(db, import_id=import_id, project_id=project.id)
    if batch is None or batch.project_id != project.id:
        raise HTTPException(status_code=404, detail="mask format import not found")
    return batch_out(batch)


@router.post(
    "/projects/{project_id}/mask-formats/imports/{import_id}/resume",
    response_model=MaskFormatImportBatchOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def resume_mask_format_import(
    import_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
) -> MaskFormatImportBatchOut:
    try:
        batch = await resume_import(
            db,
            batch_id=import_id,
            project_id=project.id,
            user_id=user.id,
        )
    except MaskFormatError as exc:
        await db.rollback()
        _raise_format_error(exc)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.MASK_FORMAT_IMPORT_RESUME,
        target_type="mask_format_import",
        target_id=str(batch.id),
        request=request,
        status_code=202,
        detail={"format_id": batch.format_id, "plan_digest": batch.plan_digest},
    )
    await db.commit()
    await _dispatch_or_fail(db, batch=batch, dispatcher=quality_dispatcher)
    return batch_out(batch)
