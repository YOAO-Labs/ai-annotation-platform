"""Project-authorized, bounded inspection of immutable training data."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from starlette.background import BackgroundTask

from app.api.v1.training_datasets import lock_owner
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_dataset_analysis import (
    TrainingDatasetAnalyzedSample as Sample,
)
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.training_dataset_analysis import (
    TrainingDatasetInsightOverview,
    TrainingDatasetClassPage,
    TrainingDatasetSamplePage,
    TrainingDatasetSampleDetail,
    TrainingDatasetInsightComparison,
    TrainingDatasetFrozenMask,
)
from app.services import training_dataset_analysis as service
from app.services import training_dataset_storage, training_storage_io
from app.services.training_dataset_bridge import TrainingDatasetError

router = APIRouter(
    prefix="/projects/{project_id}/training-datasets/{version_id}/insights",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


async def authorized_analysis(db, project, user, version_id):
    result = await service.ensure_analysis(db, project.id, version_id)
    await lock_owner(db, user, project)
    return result


def comparison_result(baseline, target):
    try:
        return service.compare(baseline.summary, target.summary)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.get("", response_model=TrainingDatasetInsightOverview)
async def overview(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    version, analysis, manifest_digest = await authorized_analysis(
        db, project, user, version_id
    )
    return {
        **analysis.summary["overview"],
        "version_id": version.id,
        "schema_version": service.ANALYSIS_SCHEMA,
        "input_digest": analysis.input_digest,
        "snapshot_digest": version.snapshot_digest,
        "manifest_digest": manifest_digest,
        "media": await service.media_observations(db, version.id),
    }


@router.get("/classes", response_model=TrainingDatasetClassPage)
async def classes(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=2147483647),
    search: str = Query("", max_length=200),
    order_by: Literal["index", "instances", "images"] = "index",
    split: Literal["train", "val"] | None = None,
):
    _, analysis, _ = await authorized_analysis(db, project, user, version_id)
    values = [
        row
        for row in analysis.summary["classes"]
        if search.casefold() in row["name"].casefold()
    ]
    if order_by != "index":
        scopes = [split] if split else ["train", "val"]
        values.sort(
            key=lambda row: (
                -sum(row[f"{scope}_{order_by}"] for scope in scopes),
                row["index"],
            )
        )
    return {
        "items": values[offset : offset + limit],
        "total": len(values),
        "limit": limit,
        "offset": offset,
        "order_by": order_by,
        "split": split,
    }


@router.get("/compare", response_model=TrainingDatasetInsightComparison)
async def comparison(
    version_id: uuid.UUID,
    baseline_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, target, _ = await authorized_analysis(db, project, user, version_id)
    _, baseline, _ = await authorized_analysis(db, project, user, baseline_id)
    result = comparison_result(baseline, target)
    result.pop("changes")
    return {**result, "baseline_id": baseline_id, "version_id": version_id}


@router.get("/samples", response_model=TrainingDatasetSamplePage)
async def samples(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=2147483647),
    split: Literal["train", "val"] | None = None,
    class_index: int | None = Query(None, ge=0),
    content_sha256: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
    kind: Literal["all", "positive", "negative", "excluded", "unavailable"] = "all",
    baseline_id: uuid.UUID | None = None,
    change: Literal[
        "all",
        "added",
        "removed",
        "labels_changed",
        "split_changed",
        "multiplicity_changed",
        "task_ids_changed",
        "reintroduced",
        "unchanged",
        "unknown",
        "source_relation_changed",
        "source_evidence_updated",
        "source_unknown",
    ] = "all",
):
    _, target, _ = await authorized_analysis(db, project, user, version_id)
    source_id, source = version_id, target
    selected_contents = None
    if change != "all":
        if baseline_id is None:
            raise HTTPException(status_code=422, detail="变化筛选需要选择基线版本")
        _, baseline, _ = await authorized_analysis(db, project, user, baseline_id)
        selected_contents = comparison_result(baseline, target)["changes"][change]
        if change == "removed":
            source_id, source = baseline_id, baseline
    elif baseline_id:
        _, baseline, _ = await authorized_analysis(db, project, user, baseline_id)
        comparison_result(baseline, target)
    query = (
        select(Sample)
        .where(Sample.version_id == source_id)
        .options(defer(Sample.labels))
    )
    query = query.where(Sample.included.is_(kind != "excluded"))
    if split:
        query = query.where(Sample.split == split)
    if content_sha256:
        query = query.where(Sample.sha256 == content_sha256)
    if class_index is not None:
        if class_index >= len(source.summary["classes"]):
            raise HTTPException(status_code=422, detail="类别索引不属于当前样本版本")
        query = query.where(Sample.class_ids.contains([class_index]))
    if kind in {"positive", "negative"}:
        query = query.where(Sample.is_negative.is_(kind == "negative"))
    if kind == "unavailable":
        query = query.where(Sample.issue.is_not(None))
    if selected_contents is not None:
        query = query.where(Sample.sha256.in_(sorted(selected_contents)))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    items = (
        await db.scalars(
            query.order_by(Sample.task_id)
            .limit(limit)
            .offset(offset)
            .execution_options(populate_existing=True)
        )
    ).all()
    return {
        "source_version_id": source_id,
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def sample_row(db, version_id, task_id):
    row = await db.scalar(
        select(Sample)
        .where(Sample.version_id == version_id, Sample.task_id == task_id)
        .execution_options(populate_existing=True)
    )
    if not row:
        raise HTTPException(status_code=404, detail="样本不属于该冻结版本")
    return row


@router.get("/samples/{task_id}", response_model=TrainingDatasetSampleDetail)
async def sample_detail(
    version_id: uuid.UUID,
    task_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=2147483647),
):
    version, _, _ = await authorized_analysis(db, project, user, version_id)
    row = await sample_row(db, version_id, task_id)
    manifest = await db.scalar(
        select(TrainingDatasetVersion.manifest).where(
            TrainingDatasetVersion.id == version_id,
            TrainingDatasetVersion.project_id == project.id,
        )
    )
    if manifest is None:
        raise HTTPException(status_code=404, detail="数据版本已删除")
    from app.services.training_dataset_sources import frozen_source_evidence

    member = next(
        (
            member
            for member in manifest.get("samples", [])
            if member["task_id"] == str(task_id)
        ),
        None,
    )
    if member is None and row.included:
        raise HTTPException(status_code=409, detail="冻结清单缺少该成员")
    frozen, source = {}, None
    if member is not None and member.get("asset_reference") is None:
        matched = func.jsonb_path_query_first(
            TrainingDatasetVersion.snapshot,
            literal("$.samples[*] ? (@.task_id == $task_id)").cast(JSONPATH),
            func.jsonb_build_object("task_id", str(task_id)),
        ).cast(JSONB)
        projection = await db.scalar(
            select(
                func.jsonb_build_object(
                    "contract_present",
                    TrainingDatasetVersion.snapshot.has_key(
                        "managed_original_contract"
                    ),
                    "contract",
                    TrainingDatasetVersion.snapshot["managed_original_contract"],
                    "member",
                    func.jsonb_build_object(
                        "task_id", matched["task_id"], "original", matched["original"]
                    ),
                )
            ).where(
                TrainingDatasetVersion.id == version_id,
                TrainingDatasetVersion.project_id == project.id,
            )
        )
        if projection is None:
            raise HTTPException(status_code=404, detail="数据版本已删除")
        if projection["contract_present"]:
            frozen["managed_original_contract"] = projection["contract"]
        source = projection["member"]
    try:
        lineage = (
            frozen_source_evidence(
                frozen, manifest, member, source, version.snapshot_digest
            )
            if member is not None
            else {
                "status": "unrecorded",
                "reasons": ["not_in_package"],
                "evidence": None,
                "kind": "unknown",
                "managed_evidence": None,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="冻结来源证据不可用") from exc
    return {
        "sample": row,
        "labels": row.labels[offset : offset + limit],
        "total": row.instances,
        "limit": limit,
        "offset": offset,
        "source_lineage": lineage,
    }


@router.get(
    "/samples/{task_id}/masks/{annotation_id}", response_model=TrainingDatasetFrozenMask
)
async def sample_mask(
    version_id: uuid.UUID,
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    version, _, _ = await authorized_analysis(db, project, user, version_id)
    retained = await db.scalar(
        select(TrainingDatasetVersion.id)
        .where(
            TrainingDatasetVersion.id == version.id,
            TrainingDatasetVersion.project_id == project.id,
        )
        .with_for_update(read=True)
    )
    if retained is None:
        raise HTTPException(status_code=404, detail="数据版本已删除")
    row = await sample_row(db, version_id, task_id)
    try:
        return await training_storage_io.run(
            db,
            training_dataset_storage.resolve(version),
            "training_dataset_mask",
            lambda bound: service.read_instance_mask(
                project.id, version, row, annotation_id, transport=bound
            ),
        )
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.get(
    "/samples/{task_id}/media",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "image/png": {"schema": {"type": "string", "format": "binary"}},
            }
        }
    },
)
async def sample_media(
    version_id: uuid.UUID,
    task_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    preview: bool = False,
):
    version, _, _ = await authorized_analysis(db, project, user, version_id)
    # Keep deletion from removing members while storage is read and the media
    # observation is committed. Authority locks are acquired first, as in delete.
    retained = await db.scalar(
        select(TrainingDatasetVersion.id)
        .where(
            TrainingDatasetVersion.id == version_id,
            TrainingDatasetVersion.project_id == project.id,
        )
        .with_for_update(read=True)
    )
    if retained is None:
        raise HTTPException(status_code=404, detail="数据版本已删除")
    row = await sample_row(db, version_id, task_id)
    if not row.included:
        raise HTTPException(
            status_code=409, detail="该任务未纳入训练包，没有可读取的冻结成员媒体"
        )
    try:
        source, content_type = await training_storage_io.run(
            db,
            training_dataset_storage.resolve(version),
            "training_dataset_media",
            lambda bound: service.read_media(
                project.id, version, row, preview=preview, transport=bound
            ),
        )
    except TrainingDatasetError as exc:
        row.media_state = (
            "verified" if exc.code == "frozen_preview_unavailable" else "unavailable"
        )
        row.media_checked_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    row.media_state = "verified"
    row.media_checked_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except BaseException:
        source.close()
        raise
    return StreamingResponse(
        service.stream_media(source),
        media_type=content_type,
        headers={"Cache-Control": "no-store", "X-Frozen-Media-SHA256": row.sha256},
        background=BackgroundTask(source.close),
    )
