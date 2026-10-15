"""Owner-controlled registration, authenticated deployment, and release CAS."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.training_datasets import lock_owner
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
from app.services import trained_models as service

router = APIRouter(
    prefix="/projects/{project_id}",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


async def serialize(db, project):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
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
    await serialize(db, project)
    await lock_owner(db, user, project)
    result = await service.register(db, project, user, body)
    await db.commit()
    return result


@router.get("/trained-models")
async def list_models(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await db.scalar(
        select(func.count())
        .select_from(TrainedModelVersion)
        .where(TrainedModelVersion.project_id == project.id)
    )
    items = (
        await db.scalars(
            select(TrainedModelVersion)
            .where(
                TrainedModelVersion.project_id == project.id,
            )
            .order_by(TrainedModelVersion.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "total": total,
        "items": [
            TrainedModelOut.model_validate(i).model_dump(mode="json") for i in items
        ],
    }


@router.get("/trained-models/{version_id}")
async def model_detail(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await lock_owner(db, user, project)
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
    return {
        "model": TrainedModelOut.model_validate(version).model_dump(mode="json"),
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
    await serialize(db, project)
    await lock_owner(db, user, project)
    version = await service.load_version(db, project.id, version_id)
    service.validate_bindings(project, version.classes, version.bindings)
    backend = await service.enabled_backend(db, project.id, body.registry_id)
    deployment = await db.scalar(
        select(ModelDeployment)
        .where(
            ModelDeployment.model_version_id == version.id,
            ModelDeployment.registry_id == body.registry_id,
        )
        .with_for_update()
    )
    if not deployment:
        deployment = ModelDeployment(
            model_version_id=version.id, registry_id=body.registry_id
        )
        db.add(deployment)
    elif deployment.status == "ready":
        try:
            receipt = await service.MLBackendClient(backend).trained_model_receipt(
                str(version.id)
            )
            service.check_receipt(version, receipt)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise HTTPException(
                    status_code=503, detail="暂时无法复查推理实例，请稍后重试"
                ) from exc
        except ValueError:
            pass
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="暂时无法复查推理实例，请稍后重试"
            ) from exc
        else:
            return deployment
    check_id = uuid.uuid4()
    deployment.check_id = check_id
    deployment.status = "checking"
    deployment.error_message = None
    await db.flush()
    deployment_id = deployment.id
    await db.commit()
    error = None
    try:
        receipt, trial = await service.install_and_trial(
            db, project, version, deployment
        )
    except Exception as exc:
        receipt, trial = {}, {}
        error = f"加载或样本试用未通过（{type(exc).__name__}）"
    await serialize(db, project)
    await lock_owner(db, user, project)
    deployment = await db.scalar(
        select(ModelDeployment)
        .where(ModelDeployment.id == deployment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if deployment.check_id != check_id:
        raise HTTPException(status_code=409, detail="该部署已有更新的检查，请刷新")
    deployment.status = "failed" if error else "ready"
    deployment.error_message = error
    deployment.receipt = receipt
    deployment.trial = trial
    deployment.checked_at = service.now()
    await db.commit()
    return deployment


@router.get("/trained-models/{version_id}/download")
async def download_model(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await lock_owner(db, user, project)
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
    await serialize(db, project)
    await lock_owner(db, user, project)
    result = await service.publish(db, project, user, body)
    await db.commit()
    return result
