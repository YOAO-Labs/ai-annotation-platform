"""Project-manager API for immutable training datasets."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.db.models.training_preview import TrainingPreview
from app.db.models.project_task_dataset import ProjectTaskDataset
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    get_labeling_task_source_reader,
    get_training_dataset_dispatcher,
    get_training_authority,
    require_project_owner,
    require_scopes,
)
from app.schemas.training_dataset import (
    TrainingDatasetCreate,
    TrainingDatasetOut,
    TrainingDatasetPage,
    TrainingDatasetPreflight,
    TrainingDatasetSelection,
    TrainingDatasetSummary,
)
from app.services import training_datasets as service
from app.services.active_learning_references import guard_source
from app.services import project_task_dataset as task_datasets
from app.services import training_storage_cleanup as cleanup
from aap_task_contract import VisualTask
from app.services.project_write_guard import lock_project_manager
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.dataset_task_facts import ProjectTaskSourceReader
from app.services.training_authority_contract import TrainingAuthorityPort

router = APIRouter(
    prefix="/projects/{project_id}/training-datasets",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


def fail(exc: TrainingDatasetError):
    raise HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
    ) from exc


async def load_version(db, project, version_id, *, lock=False):
    stmt = select(TrainingDatasetVersion).where(
        TrainingDatasetVersion.id == version_id,
        TrainingDatasetVersion.project_id == project.id,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    version = (await db.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    return version


@router.post(":preflight", response_model=TrainingDatasetPreflight)
async def preflight(
    body: TrainingDatasetSelection,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    source_reader: ProjectTaskSourceReader = Depends(get_labeling_task_source_reader),
):
    try:
        _, report = await service.freeze_selection(
            db,
            project,
            body,
            source_reader=source_reader,
            actor_id=user.id,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
        return report
    except TrainingDatasetError as exc:
        fail(exc)


@router.post("", response_model=TrainingDatasetOut, status_code=202)
async def create_version(
    body: TrainingDatasetCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dataset_dispatcher: TrainingDatasetDispatcher = Depends(
        get_training_dataset_dispatcher
    ),
    source_reader: ProjectTaskSourceReader = Depends(get_labeling_task_source_reader),
):
    try:
        return await service.create_version(
            db,
            project,
            user,
            body,
            api_key_id=getattr(request.state, "api_key_id", None),
            source_reader=source_reader,
            dispatcher=dataset_dispatcher,
        )
    except TrainingDatasetError as exc:
        fail(exc)


@router.get("", response_model=TrainingDatasetPage)
async def list_versions(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(
        None, pattern="^(pending|running|ready|failed|cancelled)$"
    ),
    task: VisualTask | None = None,
    task_dataset_id: uuid.UUID | None = None,
):
    if task_dataset_id is not None:
        stream = await db.scalar(
            select(ProjectTaskDataset).where(
                ProjectTaskDataset.id == task_dataset_id,
                ProjectTaskDataset.project_id == project.id,
            )
        )
        if stream is None:
            raise HTTPException(status_code=404, detail="任务数据集不存在")
        if task is not None and task != stream.task:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_mismatch", "message": "任务与任务数据集不一致"},
            )
        task = stream.task
    stmt = (
        task_datasets.version_metadata_statement()
        .options(
            defer(TrainingDatasetVersion.snapshot),
            defer(TrainingDatasetVersion.manifest),
            defer(TrainingDatasetVersion.preparation_report),
        )
        .where(TrainingDatasetVersion.project_id == project.id)
    )
    if status is not None:
        stmt = stmt.where(TrainingDatasetVersion.status == status)
    if task is not None:
        # Frozen contract, not membership, is authoritative during rolling deploy.
        stmt = stmt.where(task_datasets.recognized_task_expression() == task)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.execute(
            stmt.order_by(
                TrainingDatasetVersion.created_at.desc(),
                TrainingDatasetVersion.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [task_datasets.summary_metadata(row) for row in rows]
    return {"items": items, "total": total}


@router.get("/{version_id}", response_model=TrainingDatasetOut)
async def get_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    include_manifest: bool = True,
):
    if not include_manifest:
        row = (
            await db.execute(
                task_datasets.version_metadata_statement()
                .options(
                    defer(TrainingDatasetVersion.snapshot),
                    defer(TrainingDatasetVersion.manifest),
                    defer(TrainingDatasetVersion.preparation_report),
                )
                .where(
                    TrainingDatasetVersion.id == version_id,
                    TrainingDatasetVersion.project_id == project.id,
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="训练数据版本不存在")
        version = task_datasets.summary_metadata(row)
        return {
            **TrainingDatasetSummary.model_validate(version).model_dump(),
            "manifest": {},
        }
    version = await load_version(db, project, version_id)
    return await task_datasets.attach_read_metadata(db, version)


@router.post("/{version_id}/retry", response_model=TrainingDatasetOut, status_code=202)
async def retry_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dataset_dispatcher: TrainingDatasetDispatcher = Depends(
        get_training_dataset_dispatcher
    ),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    await lock_project_manager(db, user.id, project.id)
    try:
        await service.lock_dataset_execution(db, version_id)
    except TrainingDatasetError as exc:
        fail(exc)
    version = await load_version(db, project, version_id, lock=True)
    if version.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="仅失败或已取消的版本可重试")
    try:
        await training_authority.assert_authority(
            project_id=version.project_id,
            actor_id=version.creator_id,
            api_key_id=version.snapshot.get("authorization", {}).get("api_key_id"),
        )
    except TrainingDatasetError as exc:
        fail(exc)
    await service.enqueue_dataset(db, version)
    await db.commit()
    await service.dispatch_dataset(db, version, dispatcher=dataset_dispatcher)
    await db.refresh(version)
    return await task_datasets.attach_read_metadata(db, version)


@router.get("/{version_id}/download")
async def download_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    await lock_project_manager(db, user.id, project.id)
    version = await load_version(db, project, version_id)
    if version.status != "ready" or not version.package_key:
        raise HTTPException(status_code=409, detail="训练数据版本尚未就绪")
    if not await asyncio.to_thread(
        storage_service.verify_upload,
        version.package_key,
        bucket=storage_service.training_bucket,
    ):
        raise HTTPException(
            status_code=409, detail="训练包不可用，请联系管理员检查持久存储"
        )
    url = storage_service.generate_download_url(
        version.package_key, bucket=storage_service.training_bucket, expires_in=600
    )
    return {"url": url, "sha256": version.package_sha256}


@router.delete(
    "/{version_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def delete_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_project_manager(db, user.id, project.id)
    try:
        await service.lock_dataset_execution(db, version_id)
    except TrainingDatasetError as exc:
        fail(exc)
    version = await load_version(db, project, version_id, lock=True)

    await guard_source(db, version_id=version.id)
    from app.db.models.learning_policy import LearningPolicy, LearningTrigger
    from app.db.models.sample_evaluation import SampleEvaluationRun
    from app.db.models.training_study import TrainingStudy

    if await db.scalar(
        select(TrainingStudy.id).where(TrainingStudy.dataset_id == version.id).limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="数据版本被参数搜索的冻结计划引用，不能删除"
        )

    if await db.scalar(
        select(SampleEvaluationRun.id)
        .where(SampleEvaluationRun.dataset_id == version.id)
        .limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="数据版本被冻结样本评估引用，请先删除已结束的评估"
        )

    if await db.scalar(
        select(LearningTrigger.id)
        .where(LearningTrigger.dataset_id == version.id)
        .limit(1)
    ) or await db.scalar(
        select(LearningPolicy.id)
        .where(
            (LearningPolicy.baseline_dataset_id == version.id)
            | (LearningPolicy.config["parent_id"].astext == str(version.id))
        )
        .limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="数据被自动策略或触发记录引用，请先移除对应依赖"
        )
    if (
        version.status in {"pending", "running"}
        or await db.scalar(
            select(TrainingDatasetVersion.id)
            .where(TrainingDatasetVersion.parent_id == version.id)
            .limit(1)
        )
        or await db.scalar(
            select(TrainingRun.id).where(TrainingRun.dataset_id == version.id).limit(1)
        )
        or await db.scalar(
            select(TrainingPreview.id)
            .where(TrainingPreview.dataset_id == version.id)
            .limit(1)
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="运行中或被后续版本、训练实验、未清理增强预览引用的数据版本不能删除",
        )
    intent = cleanup.stage_deletion(db, f"projects/{project.id}/datasets/{version.id}/")
    await service.stage_version_deletion(db, version)
    await db.commit()
    await cleanup.purge_committed(db, intent.id)
