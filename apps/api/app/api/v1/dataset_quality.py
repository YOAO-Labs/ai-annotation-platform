"""Dataset-manager quality evidence and review endpoints."""

from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.deps import get_current_user, get_db, require_scopes
from app.db.models.dataset_quality import DatasetQualityRun as Run
from app.schemas.dataset_quality import (
    DatasetQualityConfig,
    DatasetQualityRunOut,
    DatasetQualityRunList,
    DatasetQualityResultList,
    DatasetQualityReview,
)
from app.services import dataset_quality as svc
from app.services.dataset_source_access import assert_dataset_manage
from app.services.dataset_content import key_authority

router = APIRouter(
    prefix="/datasets/{dataset_id}/quality-runs",
    dependencies=[Depends(require_scopes("datasets:read"))],
)


@router.get("", response_model=DatasetQualityRunList)
async def list_runs(
    dataset_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    actor = await assert_dataset_manage(db, dataset_id, actor)
    stmt = select(Run).where(Run.dataset_id == dataset_id, svc.scope_clause(actor))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(
        await db.scalars(
            stmt.order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return {"items": rows, "total": total}


@router.post(
    "",
    response_model=DatasetQualityRunOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def start_run(
    dataset_id: uuid.UUID,
    payload: DatasetQualityConfig,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    run = await svc.start(
        db, dataset_id, actor, payload, getattr(request.state, "api_key_id", None)
    )
    from app.workers.dataset_quality import dispatch

    await dispatch(db, run)
    return run


@router.get("/{run_id}", response_model=DatasetQualityRunOut)
async def get_run(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    return await svc.require_run(db, dataset_id, run_id, actor)


@router.post(
    "/{run_id}/resume",
    response_model=DatasetQualityRunOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def resume_run(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    run = await svc.resume(
        db, dataset_id, run_id, actor, getattr(request.state, "api_key_id", None)
    )
    from app.workers.dataset_quality import dispatch

    await dispatch(db, run)
    return run


@router.get("/{run_id}/results", response_model=DatasetQualityResultList)
async def results(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    finding: Literal[
        "decode_failed", "dimensions_out_of_range", "exact_duplicate", "skipped"
    ]
    | None = None,
    review: Literal["open", "confirmed", "dismissed"] | None = None,
    sha256: str | None = Query(None, pattern="^[0-9a-f]{64}$"),
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    run = await svc.require_run(db, dataset_id, run_id, actor)
    return await svc.result_page(
        db,
        run,
        limit=limit,
        offset=offset,
        finding=finding,
        review=review,
        sha256=sha256,
    )


@router.post(
    "/{run_id}/results/{result_id}/review",
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def review(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    payload: DatasetQualityReview,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    run = await svc.require_run(db, dataset_id, run_id, actor)
    key_id = getattr(request.state, "api_key_id", None)
    proof = await key_authority(db, actor, key_id, "datasets:write")
    value = await svc.review_result(db, run, result_id, actor, payload)
    if await key_authority(db, actor, key_id, "datasets:write") != proof:
        from fastapi import HTTPException

        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await db.commit()
    return value
