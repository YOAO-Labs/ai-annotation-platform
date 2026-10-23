"""Preview and confirm immutable bulk quality review scopes."""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.deps import get_current_user, get_db, require_scopes
from app.db.models.dataset_quality_review import (
    DatasetQualityReviewBatch as Batch,
    DatasetQualityReviewMember as Member,
)
from app.schemas.dataset_quality_review import (
    QualityReviewPreview,
    QualityReviewExecute,
    QualityReviewBatchOut,
    QualityReviewBatchList,
    QualityReviewMemberList,
)
from app.services import dataset_quality_review as svc, dataset_quality as quality
from app.services.dataset_content import key_authority

router = APIRouter(
    prefix="/datasets/{dataset_id}/quality-runs/{run_id}/review-batches",
    dependencies=[Depends(require_scopes("datasets:read"))],
)
WRITE = [Depends(require_scopes("datasets:write"))]


@router.post(
    "/preview",
    response_model=QualityReviewBatchOut,
    status_code=201,
    dependencies=WRITE,
)
async def preview(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: QualityReviewPreview,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    await key_authority(
        db, actor, getattr(request.state, "api_key_id", None), "datasets:write"
    )
    batch = await svc.preview(db, dataset_id, run_id, actor, payload)
    await db.commit()
    return batch


@router.get("", response_model=QualityReviewBatchList)
async def list_batches(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    await quality.require_run(db, dataset_id, run_id, actor)
    stmt = select(Batch).where(
        Batch.quality_run_id == run_id, svc.visible_clause(actor)
    )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(
        await db.scalars(
            stmt.order_by(Batch.created_at.desc(), Batch.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return {"items": rows, "total": total}


@router.get("/{batch_id}", response_model=QualityReviewBatchOut)
async def get_batch(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    batch_id: uuid.UUID,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await svc.require_batch(db, dataset_id, run_id, batch_id, actor)


@router.get("/{batch_id}/members", response_model=QualityReviewMemberList)
async def members(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    batch_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    batch = await svc.require_batch(db, dataset_id, run_id, batch_id, actor)
    rows = list(
        await db.scalars(
            select(Member)
            .where(Member.batch_id == batch.id)
            .order_by(Member.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return {"items": rows, "total": batch.total}


@router.post(
    "/{batch_id}/execute", response_model=QualityReviewBatchOut, dependencies=WRITE
)
async def execute(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: QualityReviewExecute,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    key_id = getattr(request.state, "api_key_id", None)
    await key_authority(db, actor, key_id, "datasets:write")
    batch, started = await svc.execute_preview(
        db, dataset_id, run_id, batch_id, actor, payload, key_id
    )
    from app.workers.dataset_quality_review import dispatch

    if started:
        await dispatch(db, batch)
    else:
        await db.commit()
    return batch


@router.post(
    "/{batch_id}/resume", response_model=QualityReviewBatchOut, dependencies=WRITE
)
async def resume(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    batch = await svc.resume(
        db,
        dataset_id,
        run_id,
        batch_id,
        actor,
        getattr(request.state, "api_key_id", None),
    )
    from app.workers.dataset_quality_review import dispatch

    await dispatch(db, batch)
    return batch
