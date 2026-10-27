"""Owner-controlled registration, authenticated deployment, and release CAS."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.training_datasets import lock_owner
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
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
from app.services import model_lineage as lineage
from app.schemas.model_lineage import ArchiveFilter
from app.schemas._jsonb_types import CocoRleContent
from app.services.training_list_filters import (
    ISODate,
    created_date_bounds,
    literal_search_pattern,
)

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
    archive: ArchiveFilter = "exclude",
    q: str = Query("", max_length=120),
    framework: Literal["ultralytics", "libreyolo"] | None = None,
    task: Literal["detect", "segment"] | None = None,
    created_from: ISODate | None = Query(
        None, description="创建日期下界，UTC 日期（含）"
    ),
    created_to: ISODate | None = Query(
        None, description="创建日期上界，UTC 日期（含整天）"
    ),
) -> dict:
    start, end = created_date_bounds(created_from, created_to)
    predicates = [
        TrainedModelVersion.project_id == project.id,
        lineage.archive_condition(project.id, archive),
    ]
    pattern = literal_search_pattern(q)
    if pattern is not None:
        predicates.append(TrainedModelVersion.name.ilike(pattern, escape="\\"))
    if framework is not None:
        predicates.append(TrainedModelVersion.framework == framework)
    if task is not None:
        predicates.append(service.model_task_expression() == task)
    if start is not None:
        predicates.append(TrainedModelVersion.created_at >= start)
    if end is not None:
        predicates.append(TrainedModelVersion.created_at <= end)
    total = await db.scalar(
        select(func.count()).select_from(TrainedModelVersion).where(*predicates)
    )
    items = (
        await db.scalars(
            select(TrainedModelVersion)
            .where(*predicates)
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
    dataset = None
    for item in deployments:
        rendered = ModelDeploymentOut.model_validate(item).model_dump(mode="json")
        if item.status == "ready" and item.trial.get("task_id"):
            if dataset is None:
                dataset = await db.get(
                    TrainingDatasetVersion,
                    uuid.UUID(version.provenance["dataset_id"]),
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
                lambda transport: transport.generate_download_url(
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.config import settings
    from app.services.raster_mask_storage import load_coco_rle

    await lock_owner(db, user, project)
    await service.load_version(db, project.id, version_id)
    deployment = await db.get(ModelDeployment, deployment_id)
    if (
        not settings.raster_mask_read_enabled
        or not deployment
        or deployment.model_version_id != version_id
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await serialize(db, project)
    await lock_owner(db, user, project)
    version = await service.load_version(db, project.id, version_id)
    service.validate_bindings(
        project, version.classes, version.bindings, service.model_task(version)
    )
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
            if receipt.get("inference_runtime") == (deployment.receipt or {}).get(
                "inference_runtime"
            ):
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
    owner = await service.owner_for_model(db, version)
    url = await service.storage_io.read(
        db,
        owner,
        "training_model_url",
        lambda transport: transport.generate_download_url(
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
    selection = await db.get(ProjectModelSelection, (project.id, task))
    current = (
        await db.get(ModelRelease, selection.release_id)
        if selection and selection.release_id
        else None
    )
    total = await db.scalar(
        select(func.count())
        .select_from(ModelRelease)
        .where(ModelRelease.project_id == project.id, ModelRelease.task == task)
    )
    items = (
        await db.scalars(
            select(ModelRelease)
            .where(ModelRelease.project_id == project.id, ModelRelease.task == task)
            .order_by(ModelRelease.generation.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await serialize(db, project)
    await lock_owner(db, user, project)
    result = await service.publish(db, project, user, body)
    await db.commit()
    return result
