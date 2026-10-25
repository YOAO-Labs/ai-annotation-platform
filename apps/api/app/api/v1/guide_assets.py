"""Annotation guide image resource endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_workspace.configuration_contract import (
    ConfigurationUnavailable,
    configuration_guide_data,
)
from app.deps import get_current_user, get_db
from app.schemas.guide_asset import (
    GuideAssetEntry,
    GuideAssetSignedUrlResponse,
    GuideAssetUploadInitRequest,
    GuideAssetUploadInitResponse,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_configuration import (
    ConfigurationExecution,
    configuration_manager,
    execute_configuration,
)
from app.services.workspace_configuration_data import WorkspaceConfigurationData
from app.services.workspace_project_query import execute_project_query

router = APIRouter()


async def _require_project_manager(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> UUID:
    access = await execute_project_query(
        db,
        operation="require_project_manager",
        actor=current_user,
        target_id=project_id,
    )
    return access["project_id"]


async def _require_project_visible(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> UUID:
    access = await execute_project_query(
        db,
        operation="require_project_visible",
        actor=current_user,
        target_id=project_id,
    )
    return access["project_id"]


async def _execute_guide_operation(
    db: AsyncSession,
    *,
    operation: str,
    actor: IdentityActorFacts,
    project_id: UUID,
    payload: dict,
) -> ConfigurationExecution:
    execution = await execute_configuration(
        db,
        operation=operation,
        actor_id=actor.id,
        target_id=project_id,
        payload=payload,
    )
    if execution.guide_result is None or execution.guide_request is None:
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用")
    return execution


def _guide_public_data(execution: ConfigurationExecution) -> dict:
    if execution.guide_result is None:
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用")
    try:
        return configuration_guide_data(execution.guide_result).model_dump(
            mode="python"
        )
    except ConfigurationUnavailable:
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用") from None


async def _commit_and_refresh_if_needed(
    db: AsyncSession, execution: ConfigurationExecution
) -> None:
    if execution.guide_result is None or execution.guide_request is None:
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用")
    await db.commit()
    if not execution.guide_result.result.refreshed:
        await configuration_manager(
            WorkspaceConfigurationData(db), db=db
        ).refresh_guide(execution.guide_request)


@router.post(
    "/{project_id}/guide-assets/upload-init",
    response_model=GuideAssetUploadInitResponse,
)
async def guide_asset_upload_init(
    data: GuideAssetUploadInitRequest,
    project_id: UUID = Depends(_require_project_manager),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    execution = await _execute_guide_operation(
        db,
        operation="guide_asset_upload_init",
        actor=current_user,
        project_id=project_id,
        payload={
            "filename": data.filename,
            "content_type": data.content_type,
            "size": data.size,
        },
    )
    return GuideAssetUploadInitResponse(**_guide_public_data(execution))


@router.post(
    "/{project_id}/guide-assets/upload-complete",
    response_model=GuideAssetEntry,
)
async def guide_asset_upload_complete(
    payload: dict,
    project_id: UUID = Depends(_require_project_manager),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    key = payload.get("key")
    original_name = payload.get("original_name")
    content_type = payload.get("content_type")
    if not key or not original_name or not content_type:
        raise HTTPException(status_code=400, detail="Missing required fields")

    execution = await _execute_guide_operation(
        db,
        operation="guide_asset_upload_complete",
        actor=current_user,
        project_id=project_id,
        payload={
            "key": key,
            "original_name": original_name,
            "content_type": content_type,
        },
    )
    public_data = _guide_public_data(execution)
    await _commit_and_refresh_if_needed(db, execution)
    return GuideAssetEntry(**public_data)


@router.delete("/{project_id}/guide-assets")
async def guide_asset_delete(
    key: str,
    project_id: UUID = Depends(_require_project_manager),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    execution = await _execute_guide_operation(
        db,
        operation="guide_asset_delete",
        actor=current_user,
        project_id=project_id,
        payload={"key": key},
    )
    public_data = _guide_public_data(execution)
    await _commit_and_refresh_if_needed(db, execution)
    return public_data


@router.get(
    "/{project_id}/guide-assets/sign-url",
    response_model=GuideAssetSignedUrlResponse,
)
async def guide_asset_sign_url(
    key: str,
    project_id: UUID = Depends(_require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    execution = await _execute_guide_operation(
        db,
        operation="guide_asset_sign",
        actor=current_user,
        project_id=project_id,
        payload={"key": key},
    )
    return GuideAssetSignedUrlResponse(**_guide_public_data(execution))
