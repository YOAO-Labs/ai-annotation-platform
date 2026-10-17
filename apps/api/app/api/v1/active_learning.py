"""Explicit manager-owned selection, human batch decisions and factual feedback."""

import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.project_write_guard import lock_project_manager
from app.db.models.active_learning import (
    ActiveLearningAttempt as Attempt,
    ActiveLearningDecision as Decision,
    ActiveLearningFeedback as Feedback,
    ActiveLearningRound as Round,
)
from app.db.models.project import Project
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    get_training_authority,
    require_project_owner,
    require_scopes,
)
from app.schemas.active_learning import (
    DecisionCreate,
    FeedbackCreate,
    RoundCreate,
    RoundOut,
    Selection,
)
from app.services import (
    active_learning as service,
    active_learning_feedback as feedback,
    active_learning_pool as pool,
)
from app.services.assets_active_learning_fence import (
    SqlAlchemyAssetsActiveLearningFence,
)
from app.services.labeling_active_learning_source_contract import CandidatePageRead
from app.services.labeling_active_learning_sources import (
    SqlAlchemyLabelingActiveLearningSources,
)
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services.training_authority_contract import TrainingAuthorityPort

router = APIRouter(
    prefix="/projects/{project_id}/active-learning-rounds",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)
WRITE = [Depends(require_scopes("datasets:write"))]


async def attempt_for(db, round, attempt_id):
    attempt = await db.get(Attempt, attempt_id)
    if not attempt or attempt.round_id != round.id:
        raise service.reject("attempt_missing", "选样执行尝试不存在", 404)
    return attempt


@router.get("/pool")
async def candidate_pool(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(32, ge=2, le=32),
    offset: int = Query(0, ge=0),
) -> dict:
    sources = SqlAlchemyLabelingActiveLearningSources(
        db,
        dataset_bucket=storage_service.datasets_bucket,
        task_bucket=storage_service.bucket,
    )
    return await sources.list_candidates(
        CandidatePageRead(project_id=project.id, limit=limit, offset=offset)
    )


@router.post(":preview", dependencies=WRITE)
async def preview(
    body: Selection,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
) -> dict:
    candidate_sources = SqlAlchemyLabelingActiveLearningSources(
        db,
        dataset_bucket=storage_service.datasets_bucket,
        task_bucket=storage_service.bucket,
    )
    try:
        prepared = await pool.prepare(
            db,
            project,
            body,
            user.id,
            getattr(request.state, "api_key_id", None),
            candidate_sources=candidate_sources,
            training_authority=training_authority,
        )
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    except ValueError as exc:
        raise service.reject("pool_insufficient", str(exc), 422) from exc
    return {
        "preview_digest": prepared["preview_digest"],
        "pool_size": len(prepared["pool"]),
        "candidate_count": len(prepared["samples"]),
        "omitted": prepared["omitted"],
        "samples": [
            {key: value for key, value in sample.items() if key != "snapshot"}
            for sample in prepared["samples"]
        ],
        "model_ref": prepared["model_ref"],
        "profile": prepared["profile"],
        "strategy": prepared["strategy"],
        "exclusion_reference_count": len(prepared["exclusions"]["references"]),
        "batch_size": body.batch_size,
        "max_inference_dispatches": body.max_inference_dispatches,
        "max_review_tasks": body.max_review_tasks,
        "claim": "workflow_correctness_only",
    }


@router.post("", status_code=202, response_model=RoundOut, dependencies=WRITE)
async def create(
    body: RoundCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    digest = content_digest(body.model_dump(mode="json"))
    existing = await db.scalar(
        select(Round).where(
            Round.project_id == project.id, Round.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise service.reject("request_conflict", "同一请求标识不能用于不同选样轮次")
        return await service.output(db, existing)
    candidate_sources = SqlAlchemyLabelingActiveLearningSources(
        db,
        dataset_bucket=storage_service.datasets_bucket,
        task_bucket=storage_service.bucket,
    )
    assets_fence = SqlAlchemyAssetsActiveLearningFence(db)
    try:
        selection = Selection.model_validate(
            body.model_dump(exclude={"request_id", "name", "preview_digest"})
        )
        prepared = await pool.prepare(
            db,
            project,
            selection,
            user.id,
            getattr(request.state, "api_key_id", None),
            candidate_sources=candidate_sources,
            training_authority=training_authority,
        )
        round = await service.create_round(
            db,
            project,
            user,
            body,
            prepared,
            getattr(request.state, "api_key_id", None),
            candidate_sources=candidate_sources,
            assets_fence=assets_fence,
            training_authority=training_authority,
        )
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    except ValueError as exc:
        raise service.reject("pool_invalid", str(exc), 422) from exc
    await db.commit()
    from app.workers.active_learning import dispatch

    await dispatch(db, round)
    return await service.output(db, round)


@router.get("")
async def list_rounds(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await db.scalar(
        select(func.count()).select_from(Round).where(Round.project_id == project.id)
    )
    rows = (
        await db.scalars(
            select(Round)
            .where(Round.project_id == project.id)
            .order_by(Round.created_at.desc(), Round.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {"total": total, "items": [await service.output(db, row) for row in rows]}


@router.get("/{round_id}", response_model=RoundOut)
async def detail(
    round_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await service.output(db, await service.load_round(db, project.id, round_id))


@router.post("/{round_id}/cancel", response_model=RoundOut, dependencies=WRITE)
async def cancel(
    round_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_project_manager(db, user.id, project.id)
    round = await service.load_round(db, project.id, round_id, lock=True)
    await service.request_cancel(db, round)
    await db.commit()
    return await service.output(db, round)


@router.post(
    "/{round_id}/retry", status_code=202, response_model=RoundOut, dependencies=WRITE
)
async def retry(
    round_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    await lock_project_manager(db, user.id, project.id)
    round = await service.load_round(db, project.id, round_id, lock=True)
    rows = await service.candidate_rows(db, round)
    if (
        round.status not in {"lost", "failed"}
        or time.time() >= round.spec["expires_at"]
        or round.status == "failed"
        and any(row.request for row in rows)
    ):
        raise service.reject(
            "retry_unavailable",
            "仅原窗口内的失联执行或尚未推理的失败可恢复；取消、封存或过期须新建轮次",
        )
    try:
        await training_authority.assert_authority(
            project_id=round.project_id,
            actor_id=round.creator_id,
            api_key_id=round.authorization.get("api_key_id"),
            required_scopes=pool.SCOPES,
        )
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    previous = await attempt_for(db, round, round.current_attempt_id)
    await service.retry_round(db, round, previous, rows)
    await db.commit()
    from app.workers.active_learning import dispatch

    await dispatch(db, round)
    return await service.output(db, round)


@router.get("/{round_id}/attempts/{attempt_id}/report")
async def report(
    round_id: uuid.UUID,
    attempt_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    round = await service.load_round(db, project.id, round_id)
    return await service.read_report(
        db, round, await attempt_for(db, round, attempt_id)
    )


@router.get("/{round_id}/candidates")
async def candidates(
    round_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    round = await service.load_round(db, project.id, round_id)
    service.validate_spec(round)
    rows = await service.candidate_rows(db, round)
    if [row.ordinal for row in rows] != list(range(len(round.spec["samples"]))):
        raise service.reject("evidence_corrupt", "候选序号与冻结窗口不一致")
    return {
        "items": [
            {
                "ordinal": row.ordinal,
                "task_id": row.task_id,
                "status": row.status,
                "disposition": row.disposition,
                "decision_id": row.decision_id,
                "has_image": row.media is not None,
                "error_code": row.error_code,
                "evidence": service.verified_evidence(round, row)
                if row.status == "succeeded"
                else None,
            }
            for row in rows
        ]
    }


@router.get("/{round_id}/candidates/{ordinal}/image", response_class=Response)
async def image(
    round_id: uuid.UUID,
    ordinal: int,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    round = await service.load_round(db, project.id, round_id)
    spec = service.validate_spec(round)
    if not 0 <= ordinal < len(spec["samples"]):
        raise service.reject("candidate_missing", "冻结候选不存在", 404)
    await db.commit()
    try:
        png = await asyncio.to_thread(
            service.read_retained_png, round, spec["samples"][ordinal]
        )
    except Exception as exc:
        raise service.reject(
            "frozen_image_unavailable", "冻结图片尚未就绪或未通过完整性检查"
        ) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{round_id}/decisions")
async def decisions(
    round_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    round = await service.load_round(db, project.id, round_id)
    rows = (
        await db.scalars(
            select(Decision)
            .where(Decision.round_id == round.id)
            .order_by(Decision.created_at.desc(), Decision.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {"items": [service.decision_output(row) for row in rows]}


@router.post("/{round_id}/decisions", dependencies=WRITE)
async def decide(
    round_id: uuid.UUID,
    body: DecisionCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
) -> dict:
    candidate_sources = SqlAlchemyLabelingActiveLearningSources(
        db,
        dataset_bucket=storage_service.datasets_bucket,
        task_bucket=storage_service.bucket,
    )
    assets_fence = SqlAlchemyAssetsActiveLearningFence(db)
    try:
        row = await service.decide(
            db,
            project,
            user,
            round_id,
            body,
            getattr(request.state, "api_key_id", None),
            candidate_sources=candidate_sources,
            assets_fence=assets_fence,
            training_authority=training_authority,
        )
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    await db.commit()
    return service.decision_output(row)


@router.get("/{round_id}/feedback")
async def feedback_list(
    round_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    round = await service.load_round(db, project.id, round_id)
    rows = (
        await db.scalars(
            select(Feedback)
            .where(Feedback.round_id == round.id)
            .order_by(Feedback.created_at.desc(), Feedback.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {"items": [feedback.output(row) for row in rows]}


@router.post("/{round_id}/feedback", dependencies=WRITE)
async def record_feedback(
    round_id: uuid.UUID,
    body: FeedbackCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
) -> dict:
    try:
        row = await feedback.snapshot(
            db,
            project,
            user,
            round_id,
            body,
            getattr(request.state, "api_key_id", None),
            training_authority=training_authority,
        )
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    await db.commit()
    return feedback.output(row)
