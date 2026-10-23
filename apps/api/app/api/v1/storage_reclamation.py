"""Non-executing platform-owned retirement assertion observations."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.storage_reclamation import ManagedOriginalReclamationPreview
from app.services.managed_original_reclamation import preview

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
