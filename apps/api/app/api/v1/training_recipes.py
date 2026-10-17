"""Manager-owned, project-scoped recipe history and explicit reuse candidates."""

import uuid
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from app.db.models.project import Project
from app.db.models.user import User
from app.db.models.training_recipe import TrainingRecipe, TrainingRecipeRevision
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.training_recipe import (
    TrainingConfigurationResolve,
    TrainingRecipeOut,
    TrainingRecipePage,
    TrainingRecipeWrite,
    TrainingReuseCandidate,
)
from app.services import training_recipes as service

router = APIRouter(
    prefix="/projects/{project_id}/training-recipes",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.post(":resolve", dependencies=[Depends(require_scopes("datasets:write"))])
async def resolve_configuration(
    body: TrainingConfigurationResolve,
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
) -> dict:
    return await service.resolve_snapshot(db, project.id, body)


@router.get("", response_model=TrainingRecipePage)
async def list_recipes(
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_archived: bool = False,
):
    query = select(TrainingRecipe).where(TrainingRecipe.project_id == project.id)
    if not include_archived:
        query = query.where(TrainingRecipe.archived.is_(False))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    recipes = await db.scalars(
        query.order_by(TrainingRecipe.created_at.desc(), TrainingRecipe.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = []
    for recipe in recipes:
        _, revision = await service.recipe_revision(db, project.id, recipe.id)
        items.append(await service.describe(db, recipe, revision))
    return {"total": total, "items": items}


@router.post(
    "",
    response_model=TrainingRecipeOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def create_recipe(
    body: TrainingRecipeWrite,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await service.write_revision(body, project, user, db)


@router.post(
    "/{recipe_id}/revisions",
    response_model=TrainingRecipeOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def create_revision(
    recipe_id: uuid.UUID,
    body: TrainingRecipeWrite,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await service.write_revision(body, project, user, db, recipe_id)


@router.get("/{recipe_id}", response_model=TrainingRecipeOut)
async def get_recipe(
    recipe_id: uuid.UUID,
    revision_id: uuid.UUID | None = None,
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
):
    recipe, revision = await service.recipe_revision(
        db, project.id, recipe_id, revision_id
    )
    return await service.describe(db, recipe, revision)


@router.get("/{recipe_id}/revisions", response_model=TrainingRecipePage)
async def list_revisions(
    recipe_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    recipe, _ = await service.recipe_revision(db, project.id, recipe_id)
    where = TrainingRecipeRevision.recipe_id == recipe.id
    total = await db.scalar(
        select(func.count()).select_from(TrainingRecipeRevision).where(where)
    )
    revisions = await db.scalars(
        select(TrainingRecipeRevision)
        .where(where)
        .order_by(TrainingRecipeRevision.ordinal.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": total,
        "items": [
            await service.describe(db, recipe, revision) for revision in revisions
        ],
    }


@router.post(
    "/{recipe_id}/archive",
    response_model=TrainingRecipeOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def archive_recipe(
    recipe_id: uuid.UUID,
    expected_head_revision: int = Query(ge=1),
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    recipe, revision = await service.archive_recipe(
        db, project, user, recipe_id, expected_head_revision
    )
    await db.commit()
    return await service.describe(db, recipe, revision)


@router.get("/clone/{run_id}", response_model=TrainingReuseCandidate)
async def clone_run(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID | None = None,
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
):
    return await service.clone_candidate(db, project.id, run_id, attempt_id)
