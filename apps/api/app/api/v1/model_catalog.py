"""Discovery does not load models, probe services, or authorize execution."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.deps import get_current_user, get_db, require_roles
from app.schemas.model_catalog import ModelCatalogResponse
from app.services.model_catalog import list_catalog
from app.services.project_access import resolve_project_access_by_id

router = APIRouter()


@router.get("/model-catalog", response_model=ModelCatalogResponse)
async def global_catalog(
    response: Response,
    project_id: UUID | None = None,
    task: str | None = None,
    source: str | None = None,
    status: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles("super_admin", "project_admin")),
):
    from fastapi import HTTPException

    project = access = None
    if project_id:
        project, access = await resolve_project_access_by_id(
            db, actor_id=user.id, project_id=project_id
        )
        if not access.is_manager:
            raise HTTPException(status_code=404, detail="项目不存在")
    response.headers["Cache-Control"] = "private, no-store"
    return await list_catalog(
        db,
        user,
        project=project,
        access=access,
        task=task,
        source=source,
        status=status,
        q=q,
        page=page,
        page_size=page_size,
    )


@router.get("/projects/{project_id}/model-catalog", response_model=ModelCatalogResponse)
async def project_catalog(
    project_id: UUID,
    response: Response,
    task: str | None = None,
    source: str | None = None,
    status: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project, access = await resolve_project_access_by_id(
        db, actor_id=user.id, project_id=project_id
    )
    response.headers["Cache-Control"] = "private, no-store"
    return await list_catalog(
        db,
        user,
        project=project,
        access=access,
        task=task,
        source=source,
        status=status,
        q=q,
        page=page,
        page_size=page_size,
    )
