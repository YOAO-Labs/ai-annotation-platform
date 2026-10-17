"""Owner-controlled registration, authenticated deployment, and release CAS."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.project_write_guard import lock_project_manager
from app.services.training_claims import project_lock
from app.db.models.project import Project
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.trained_model import (
    ModelDeploymentCreate,
    ModelDeploymentOut,
    ModelReleaseCreate,
    ModelReleaseOut,
    TrainedModelCreate,
    TrainedModelOut,
)
from app.application import trained_model_deployment
from app.services import trained_models as service
from app.services import model_lineage as lineage
from app.schemas.model_lineage import ArchiveFilter

router = APIRouter(
    prefix="/projects/{project_id}",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.post(
    "/trained-models",
    response_model=TrainedModelOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def register_model(
    body: TrainedModelCreate,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    result = await service.register(db, project, user, body)
    await db.commit()
    return result


@router.get("/trained-models")
async def list_models(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    archive: ArchiveFilter = "exclude",
) -> dict:
    condition = lineage.archive_condition(project.id, archive)
    total = await db.scalar(
        select(func.count())
        .select_from(TrainedModelVersion)
        .where(TrainedModelVersion.project_id == project.id, condition)
    )
    items = (
        await db.scalars(
            select(TrainedModelVersion)
            .where(
                TrainedModelVersion.project_id == project.id,
                condition,
            )
            .order_by(
                TrainedModelVersion.created_at.desc(), TrainedModelVersion.id.desc()
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    metadata = await lineage.model_metadata(db, project.id, [i.id for i in items])
    return {
        "total": total,
        "items": [
            {
                **TrainedModelOut.model_validate(i).model_dump(mode="json"),
                **metadata[i.id],
            }
            for i in items
        ],
    }


@router.get("/trained-models/{version_id}")
async def model_detail(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await lock_project_manager(db, user.id, project.id)
    version = await service.load_version(db, project.id, version_id)
    deployments = (
        await db.scalars(
            select(ModelDeployment).where(
                ModelDeployment.model_version_id == version.id
            )
        )
    ).all()
    rendered_deployments = []
    for item in deployments:
        rendered = ModelDeploymentOut.model_validate(item).model_dump(mode="json")
        if item.status == "ready" and item.trial.get("task_id"):
            from app.services.storage import storage_service

            key = f"projects/{project.id}/datasets/{version.provenance['dataset_id']}/media/{item.trial['task_id']}"
            url = await asyncio.to_thread(
                storage_service.generate_download_url,
                key,
                expires_in=600,
                bucket=storage_service.training_bucket,
                align=False,
            )
            rendered["trial"] = {**rendered["trial"], "sample_url": url}
        rendered_deployments.append(rendered)
    metadata = await lineage.model_metadata(db, project.id, [version.id])
    return {
        "model": {
            **TrainedModelOut.model_validate(version).model_dump(mode="json"),
            **metadata[version.id],
        },
        "deployments": rendered_deployments,
    }


@router.post(
    "/trained-models/{version_id}/deploy",
    response_model=ModelDeploymentOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def deploy_model(
    version_id: uuid.UUID,
    body: ModelDeploymentCreate,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await trained_model_deployment.deploy_model(
        db, project, user, version_id, body.registry_id
    )


@router.get("/trained-models/{version_id}/download")
async def download_model(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await lock_project_manager(db, user.id, project.id)
    version = await service.load_version(db, project.id, version_id)
    from app.services.storage import storage_service

    url = await asyncio.to_thread(
        storage_service.generate_download_url,
        version.artifact["key"],
        expires_in=600,
        bucket=storage_service.training_bucket,
        download_name="best.pt",
        align=False,
    )
    return {
        "url": url,
        "sha256": version.artifact["sha256"],
        "size_bytes": version.artifact["size_bytes"],
    }


@router.get("/model-releases")
async def list_releases(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    selection = await db.get(ProjectModelSelection, project.id)
    current = (
        await db.get(ModelRelease, selection.release_id)
        if selection and selection.release_id
        else None
    )
    total = await db.scalar(
        select(func.count())
        .select_from(ModelRelease)
        .where(ModelRelease.project_id == project.id)
    )
    items = (
        await db.scalars(
            select(ModelRelease)
            .where(ModelRelease.project_id == project.id)
            .order_by(ModelRelease.generation.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "current": ModelReleaseOut.model_validate(current).model_dump(mode="json")
        if current
        else None,
        "generation": selection.generation if selection else 0,
        "current_release_id": str(selection.release_id)
        if selection and selection.release_id
        else None,
        "total": total,
        "items": [
            ModelReleaseOut.model_validate(i).model_dump(mode="json") for i in items
        ],
    }


@router.post(
    "/model-releases",
    response_model=ModelReleaseOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def publish_model(
    body: ModelReleaseCreate,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    result = await service.publish(db, project, user, body)
    await db.commit()
    return result
