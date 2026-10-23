"""Non-executing platform-owned retirement assertion observations."""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.storage_reclamation import ManagedOriginalReclamationPreview
from app.services.managed_original_reclamation import preview
from app.schemas.managed_original_retention import (
    ManagedOriginalRetentionApply,
    ManagedOriginalRetentionChange,
    ManagedOriginalRetentionPreview,
    ManagedOriginalRetentionState,
)
from app.schemas.managed_original_verification import (
    ManagedOriginalVerificationHistory,
    ManagedOriginalVerificationStart,
)
from app.services import managed_original_verification as verification
from app.services import managed_original_retention as retention

router = APIRouter()


@router.get(
    "/managed-original-reclamation", response_model=ManagedOriginalReclamationPreview
)
async def managed_original_reclamation(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1_000_000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    return await preview(
        db,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/managed-original-retirements/{retirement_id}/retention",
    response_model=ManagedOriginalRetentionState,
)
async def managed_original_retention_state(
    retirement_id: uuid.UUID,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1_000_000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    return await retention.state(
        db,
        retirement_id,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/managed-original-retirements/{retirement_id}/retention-preview",
    response_model=ManagedOriginalRetentionPreview,
)
async def managed_original_retention_preview(
    retirement_id: uuid.UUID,
    payload: ManagedOriginalRetentionChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    return await retention.preview(
        db,
        retirement_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )


@router.post(
    "/managed-original-retirements/{retirement_id}/retention",
    response_model=ManagedOriginalRetentionState,
)
async def managed_original_retention_apply(
    retirement_id: uuid.UUID,
    payload: ManagedOriginalRetentionApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    result = await retention.apply(
        db,
        retirement_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        request=request,
    )
    await db.commit()
    return result


@router.get(
    "/managed-original-retirements/{retirement_id}/verifications",
    response_model=ManagedOriginalVerificationHistory,
)
async def managed_original_verification_history(
    retirement_id: uuid.UUID,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1_000_000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    return await verification.history(
        db,
        retirement_id,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/managed-original-retirements/{retirement_id}/verifications",
    response_model=ManagedOriginalVerificationStart,
    status_code=202,
)
async def managed_original_verification_start(
    retirement_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    row, job, existing = await verification.start(
        db,
        retirement_id,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        request=request,
    )
    response = verification.start_response(row, job, existing)
    from app.workers.managed_original_verification import dispatch

    await dispatch(db, job)
    return response
