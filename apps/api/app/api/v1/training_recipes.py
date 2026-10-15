"""Manager-owned, project-scoped recipe history and explicit reuse candidates."""

import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from app.db.models.project import Project
from app.db.models.user import User
from app.db.models.training_recipe import TrainingRecipe, TrainingRecipeRevision
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.api.v1.training_datasets import lock_owner
from app.schemas.training_recipe import (
    TrainingConfigurationResolve,
    TrainingRecipeOut,
    TrainingRecipePage,
    TrainingRecipeWrite,
    TrainingReuseCandidate,
)
from app.services import training_recipes as service
from app.services.learning_policies import project_lock
from app.services.training_dataset_bridge import content_digest

router = APIRouter(
    prefix="/projects/{project_id}/training-recipes",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.post(":resolve", dependencies=[Depends(require_scopes("datasets:write"))])
async def resolve_configuration(
    body: TrainingConfigurationResolve,
    project: Project = Depends(require_project_owner),
) -> dict:
    return service.snapshot(
        body.framework, body.base_model, body.configuration.model_dump()
    )


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


async def write_revision(body, project, user, db, recipe_id=None):
    await project_lock(db, project.id)
    await lock_owner(db, user, project)
    digest = content_digest(
        {
            "recipe_id": str(recipe_id) if recipe_id else None,
            "body": body.model_dump(mode="json"),
        }
    )
    previous = await db.scalar(
        select(TrainingRecipeRevision).where(
            TrainingRecipeRevision.project_id == project.id,
            TrainingRecipeRevision.request_id == body.request_id,
        )
    )
    if previous:
        if previous.request_digest != digest:
            raise HTTPException(
                status_code=409, detail="同一请求标识不能保存不同配方修订"
            )
        recipe, _ = await service.recipe_revision(db, project.id, previous.recipe_id)
        return await service.describe(db, recipe, previous)
    if recipe_id:
        recipe, _ = await service.recipe_revision(db, project.id, recipe_id, lock=True)
        if recipe.archived or recipe.head_revision != body.expected_head_revision:
            raise HTTPException(
                status_code=409,
                detail="配方已归档或出现新修订；保留编辑内容并刷新后重试",
            )
    else:
        if body.expected_head_revision != 0:
            raise HTTPException(status_code=409, detail="新配方的预期修订必须为零")
        recipe = TrainingRecipe(project_id=project.id, head_revision=0)
        db.add(recipe)
        await db.flush()
    if body.source_run_id:
        run = await service.source_run(
            db, project.id, body.source_run_id, body.source_attempt_id
        )
        if run.status != "succeeded":
            raise HTTPException(
                status_code=409, detail="仅已验证成功的实验可直接保存为配方"
            )
        value = service.from_run(run)
        compatible, reason = service.compatibility(value)
        if not compatible:
            raise HTTPException(status_code=409, detail=reason)
        origin = {
            "kind": "clone",
            "run_id": str(run.id),
            "attempt_id": str(body.source_attempt_id)
            if body.source_attempt_id
            else None,
            "digest": run.config_digest,
            "label": run.name,
        }
    else:
        source = body.configuration
        value = service.snapshot(
            source.framework, source.base_model, source.configuration.model_dump()
        )
        origin = await service.origin_snapshot(db, project.id, body.origin, value)
    recipe.head_revision += 1
    revision = TrainingRecipeRevision(
        recipe_id=recipe.id,
        project_id=project.id,
        creator_id=user.id,
        ordinal=recipe.head_revision,
        name=body.name,
        description=body.description,
        snapshot=value,
        snapshot_digest=content_digest(value),
        origin=origin,
        request_id=body.request_id,
        request_digest=digest,
    )
    db.add(revision)
    await db.commit()
    return await service.describe(db, recipe, revision)


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
    return await write_revision(body, project, user, db)


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
    return await write_revision(body, project, user, db, recipe_id)


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
    await project_lock(db, project.id)
    await lock_owner(db, user, project)
    recipe, revision = await service.recipe_revision(
        db, project.id, recipe_id, lock=True
    )
    if recipe.head_revision != expected_head_revision:
        raise HTTPException(status_code=409, detail="配方已有新修订，请刷新")
    recipe.archived = True
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
