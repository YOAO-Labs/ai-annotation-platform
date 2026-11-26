"""Owner-controlled registration, authenticated deployment, and release CAS."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.project_write_guard import lock_project_manager
from app.services.training_claims import project_lock
from app.db.models.project import Project
from app.services import learning_models_data as data
from app.services.training_dataset_data import read_version
from app.deps import (
    get_current_user,
    get_current_user_id,
    get_db,
    require_project_owner,
    require_scopes,
)
from app.schemas.training_run import FrameworkId
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
from app.services import training_adapters as adapters
from app.services import model_lineage as lineage
from app.schemas.model_lineage import ArchiveFilter
from app.schemas._jsonb_types import CocoRleContent
from app.services.training_list_filters import (
    ISODate,
    created_date_bounds,
    literal_search_pattern,
)
from app.services.identity_contracts import IdentityActorFacts

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
    user: IdentityActorFacts = Depends(get_current_user),
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
    q: str = Query("", max_length=120),
    framework: FrameworkId | None = None,
    task: Literal["detect", "segment"] | None = None,
    created_from: ISODate | None = Query(
        None, description="创建日期下界，UTC 日期（含）"
    ),
    created_to: ISODate | None = Query(
        None, description="创建日期上界，UTC 日期（含整天）"
    ),
) -> dict:
    if framework is not None:
        await adapters.ensure_known_framework(db, framework)
    start, end = created_date_bounds(created_from, created_to)
    total, items = await data.list_models(
        db,
        project.id,
        archive=archive,
        pattern=literal_search_pattern(q),
        framework=framework,
        task=task,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
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
    user: IdentityActorFacts = Depends(get_current_user),
) -> dict:
    await lock_project_manager(db, user.id, project.id)
    version = await service.load_version(db, project.id, version_id)
    deployments = await data.deployments_for_versions(db, [project.id], [version.id])
    rendered_deployments = []
    dataset = None
    for item in deployments:
        rendered = ModelDeploymentOut.model_validate(item).model_dump(mode="json")
        if item.status == "ready" and item.trial.get("task_id"):
            if dataset is None:
                dataset = await read_version(
                    db,
                    uuid.UUID(version.provenance["dataset_id"]),
                    project_id=project.id,
                )
            owner, key = service.frozen_sample_location(
                project.id, dataset, item.trial["task_id"]
            )
            sample = next(
                (
                    member
                    for member in dataset.manifest.get("samples", [])
                    if member.get("task_id") == item.trial["task_id"]
                ),
                {},
            )
            url = await service.storage_io.read(
                db,
                owner,
                "training_model_url",
                lambda transport: service.storage_io.sign_download(
                    transport,
                    key,
                    expires_in=600,
                    bucket=owner.bucket,
                    align=False,
                ),
                object_key=key,
                sha256=sample.get("sha256"),
                size_bytes=sample.get("size_bytes"),
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


@router.get(
    "/trained-models/{version_id}/deployments/{deployment_id}/mask-content/{shape_index}",
    response_model=CocoRleContent,
)
async def trial_mask_content(
    version_id: uuid.UUID,
    deployment_id: uuid.UUID,
    shape_index: int,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.config import settings
    from app.services.raster_mask_storage import load_coco_rle

    await lock_project_manager(db, user.id, project.id)
    await service.load_version(db, project.id, version_id)
    deployment = await data.get_deployment(db, project.id, version_id, deployment_id)
    if (
        not settings.raster_mask_read_enabled
        or not deployment
        or deployment.status != "ready"
    ):
        raise HTTPException(status_code=404, detail="分割试用掩码不存在或未开放读取")
    shapes = deployment.trial.get("result", [])
    if not 0 <= shape_index < len(shapes):
        raise HTTPException(status_code=404, detail="分割试用实例不存在")
    geometry = shapes[shape_index].get("geometry", {})
    if geometry.get("type") != "raster_mask":
        raise HTTPException(status_code=404, detail="分割试用实例不是掩码")
    return await load_coco_rle(geometry["mask"])


@router.post(
    "/trained-models/{version_id}/deploy",
    response_model=ModelDeploymentOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def deploy_model(
    version_id: uuid.UUID,
    body: ModelDeploymentCreate,
    project: Project = Depends(require_project_owner),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return await trained_model_deployment.deploy_model(
        db, project, actor_id, version_id, body.registry_id
    )


@router.post(
    "/trained-models/{version_id}/deployments/{deployment_id}/stop-references",
    response_model=ModelDeploymentOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def stop_deployment_references(
    version_id: uuid.UUID,
    deployment_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await project_lock(db, project.id)
    await lock_project_manager(db, actor_id, project.id)
    result = await service.retire_deployment(
        db, project.id, version_id, deployment_id, terminal=False
    )
    await db.commit()
    return result


@router.post(
    "/trained-models/{version_id}/deployments/{deployment_id}/retire",
    response_model=ModelDeploymentOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def retire_deployment(
    version_id: uuid.UUID,
    deployment_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await project_lock(db, project.id)
    await lock_project_manager(db, actor_id, project.id)
    result = await service.retire_deployment(
        db, project.id, version_id, deployment_id, terminal=True
    )
    await db.commit()
    return result


@router.get("/trained-models/{version_id}/download")
async def download_model(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await lock_project_manager(db, user.id, project.id)
    version = await service.load_version(db, project.id, version_id)
    owner = await service.owner_for_model(db, version)
    url = await service.storage_io.read(
        db,
        owner,
        "training_model_url",
        lambda transport: service.storage_io.sign_download(
            transport,
            version.artifact["key"],
            expires_in=600,
            bucket=owner.bucket,
            download_name="best.pt",
            align=False,
        ),
        object_key=version.artifact["key"],
        sha256=version.artifact["sha256"],
        size_bytes=version.artifact["size_bytes"],
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
    task: Literal["detect", "segment"] = "detect",
) -> dict:
    selection = await data.get_selection(db, project.id, task)
    current = (
        await data.get_release(db, project.id, selection.release_id)
        if selection and selection.release_id
        else None
    )
    total, items = await data.list_releases(db, project.id, task, limit, offset)
    return {
        "task": task,
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
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    result = await service.publish(db, project, user, body)
    await db.commit()
    return result
