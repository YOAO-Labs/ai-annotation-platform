"""Manager-owned frozen sample evaluation and verified error evidence."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.training_datasets import lock_owner
from app.db.models.project import Project
from app.db.models.sample_evaluation import (
    SampleEvaluationAttempt as Attempt,
    SampleEvaluationRun as Run,
    SampleEvaluationSample as Sample,
)
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.sample_evaluation import (
    ErrorFilter,
    EvaluationReport,
    EvaluationSamplePage,
    EvaluationSourceOut,
    SampleEvaluationCreate,
    SampleEvaluationOut,
)
from app.services import sample_evaluations as service, trained_models
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest

router = APIRouter(
    prefix="/projects/{project_id}/sample-evaluations",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)
WRITE = [Depends(require_scopes("datasets:write"))]


async def attempt_for(db, run, attempt_id):
    attempt = await db.get(Attempt, attempt_id)
    if not attempt or attempt.run_id != run.id:
        raise service.reject("attempt_missing", "评估执行尝试不存在", 404)
    return attempt


@router.get("/qualification")
async def qualification(
    model_version_id: uuid.UUID,
    deployment_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ref, profile = await service.qualification(
        db, project, model_version_id, deployment_id
    )
    selected_engine = service.engine_for(ref)
    return {
        "model_ref": ref,
        "profile": profile,
        "evaluator": selected_engine.EVALUATOR,
        "preprocessing": service.engine.PREPROCESSING,
        "limits": {
            "samples": service.engine.MAX_SAMPLES,
            "truth_per_image": service.engine.MAX_TRUTH_PER_IMAGE,
            "truth_total": service.engine.MAX_TRUTH_TOTAL,
            "predictions_per_image": service.engine.MAX_PREDICTIONS,
            "raw_bytes": service.engine.MAX_RAW_BYTES,
            "pixels": selected_engine.MAX_PIXELS,
            **(
                {
                    "max_dimension": selected_engine.MAX_DIMENSION,
                    "mask_evidence_bytes_per_sample": selected_engine.MAX_SAMPLE_RLE_BYTES,
                }
                if selected_engine is not service.engine
                else {}
            ),
        },
    }


@router.post(
    "", status_code=202, response_model=SampleEvaluationOut, dependencies=WRITE
)
async def create(
    body: SampleEvaluationCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    digest = content_digest(body.model_dump(mode="json"))
    existing = await db.scalar(
        select(Run).where(
            Run.project_id == project.id, Run.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise service.reject("request_conflict", "同一请求标识不能用于不同评估")
        return await service.output(db, existing)
    # Qualification performs HTTP before taking any domain/authority lock.
    ref, profile = await service.qualification(
        db, project, body.model_version_id, body.deployment_id
    )
    await db.commit()
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_owner(db, user, project)
    existing = await db.scalar(
        select(Run).where(
            Run.project_id == project.id, Run.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise service.reject("request_conflict", "同一请求标识不能用于不同评估")
        return await service.output(db, existing)
    for model, identity in (
        (TrainingDatasetVersion, body.dataset_id),
        (TrainedModelVersion, body.model_version_id),
        (ModelDeployment, body.deployment_id),
    ):
        if (
            await db.scalar(
                select(model).where(model.id == identity).with_for_update(read=True)
            )
            is None
        ):
            raise service.reject("reference_missing", "冻结引用已不可用", 404)
    await trained_models.validate_frozen_reference(db, project.id, ref)
    identity = uuid.uuid4()
    try:
        spec = await service.frozen_spec(db, project, body, identity, ref, profile)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise service.reject(
            "invalid_frozen_sample",
            str(exc)
            if isinstance(exc, TrainingDatasetError)
            else "冻结样本、类别或真值超过支持范围",
            422,
        ) from exc
    run = Run(
        id=identity,
        project_id=project.id,
        creator_id=user.id,
        dataset_id=body.dataset_id,
        model_version_id=body.model_version_id,
        deployment_id=body.deployment_id,
        request_id=body.request_id,
        request_digest=digest,
        name=body.name,
        spec=spec,
        spec_digest=content_digest(spec),
        authorization={"api_key_id": getattr(request.state, "api_key_id", None)},
        generation=0,
    )
    db.add(run)
    await db.flush()
    try:
        await service.owners.allocate(db, run)
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    for frozen in spec["samples"]:
        db.add(
            Sample(
                run_id=run.id,
                ordinal=frozen["ordinal"],
                request_id=uuid.uuid5(run.id, f"sample:{frozen['ordinal']}"),
            )
        )
    await service.new_attempt(db, run)
    await db.commit()
    from app.workers.sample_evaluations import dispatch

    await dispatch(db, run)
    return await service.output(db, run)


@router.get("")
async def list_evaluations(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await db.scalar(
        select(func.count()).select_from(Run).where(Run.project_id == project.id)
    )
    runs = (
        await db.scalars(
            select(Run)
            .where(Run.project_id == project.id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "total": total,
        "items": [
            (await service.output(db, run)).model_dump(mode="json") for run in runs
        ],
    }


@router.get("/compare")
async def compare(
    left_id: uuid.UUID,
    right_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if left_id == right_id:
        raise service.reject("comparison_candidates", "须明确选择两个不同评估", 422)
    runs = [
        await service.load_run(db, project.id, identity)
        for identity in (left_id, right_id)
    ]
    reports = []
    reasons = []
    for run in runs:
        if run.status != "succeeded":
            reasons.append(f"{run.name} 尚无完整成功报告")
            reports.append(None)
        else:
            reports.append(
                await service.read_report(
                    db, run, await attempt_for(db, run, run.current_attempt_id)
                )
            )
    if runs[0].spec["comparison_digest"] != runs[1].spec["comparison_digest"]:
        reasons.append("冻结基准、类别、用途、阈值或推理环境不同")
    compatible = not reasons
    delta = None
    pairs = []
    if compatible:
        left, right = reports
        delta = {
            key: None
            if left["aggregate"][key] is None or right["aggregate"][key] is None
            else right["aggregate"][key] - left["aggregate"][key]
            for key in ("tp", "fp", "fn", "precision", "recall", "f1")
            + (
                ("matched_mean_iou", "matched_mean_dice")
                if runs[0].spec["schema"] == 2
                else ()
            )
        }
        available = {}
        for sample in right["samples"]:
            key = tuple(
                sample[k] for k in ("sha256", "label_sha256", "width", "height")
            )
            available.setdefault(key, []).append(sample["ordinal"])
        for sample in left["samples"]:
            key = tuple(
                sample[k] for k in ("sha256", "label_sha256", "width", "height")
            )
            pairs.append(
                {
                    "left_ordinal": sample["ordinal"],
                    "right_ordinal": available[key].pop(0),
                }
            )
    return {
        "compatible": compatible,
        "reasons": reasons,
        "candidates": [
            {
                "evaluation_id": str(run.id),
                "name": run.name,
                "status": run.status,
                "attempt_id": str(run.current_attempt_id),
                "model_ref": run.spec["model_ref"],
                "spec_digest": run.spec_digest,
                "comparison_digest": run.spec["comparison_digest"],
                "aggregate": report["aggregate"] if report else None,
            }
            for run, report in zip(runs, reports, strict=True)
        ],
        "delta_right_minus_left": delta,
        "sample_pairs": pairs,
    }


@router.get("/{evaluation_id}", response_model=SampleEvaluationOut)
async def detail(
    evaluation_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await service.output(
        db, await service.load_run(db, project.id, evaluation_id)
    )


@router.post(
    "/{evaluation_id}/cancel", response_model=SampleEvaluationOut, dependencies=WRITE
)
async def cancel(
    evaluation_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    run = await service.load_run(db, project.id, evaluation_id, lock=True)
    await service.request_cancel(db, run)
    await db.commit()
    return await service.output(db, run)


@router.post(
    "/{evaluation_id}/retry",
    status_code=202,
    response_model=SampleEvaluationOut,
    dependencies=WRITE,
)
async def retry(
    evaluation_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    run = await service.load_run(db, project.id, evaluation_id, lock=True)
    if (
        run.deletion_requested_at
        or run.status not in {"lost", "failed"}
        or run.spec["expires_at"] <= int(service.time.time())
    ):
        raise service.reject(
            "retry_unavailable",
            "仅失联或失败且请求未过期的评估可恢复；取消或过期须新建评估",
        )
    try:
        await service.authority(db, run)
        service.owners.resolve(run)
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    previous = await db.get(Attempt, run.current_attempt_id)
    previous.claim_id = None
    if previous.status == "lost":
        previous.completed_at = service.now()
        await service.jobs.mark_failed(
            db,
            previous.async_job_id,
            error="旧执行已被新尝试接管；后端请求仍通过同一把锁防止重叠",
        )
    for row in await service.sample_rows(db, run):
        if row.status == "failed":
            row.status, row.error_code, row.error_message = "pending", None, None
    await service.new_attempt(db, run)
    await db.commit()
    from app.workers.sample_evaluations import dispatch

    await dispatch(db, run)
    return await service.output(db, run)


@router.get(
    "/{evaluation_id}/attempts/{attempt_id}/report", response_model=EvaluationReport
)
async def report(
    evaluation_id: uuid.UUID,
    attempt_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    run = await service.load_run(db, project.id, evaluation_id)
    return await service.read_report(db, run, await attempt_for(db, run, attempt_id))


@router.get(
    "/{evaluation_id}/attempts/{attempt_id}/samples",
    response_model=EvaluationSamplePage,
)
async def samples(
    evaluation_id: uuid.UUID,
    attempt_id: uuid.UUID,
    class_index: int | None = Query(None, ge=0),
    error: ErrorFilter | None = None,
    confidence_min: float | None = Query(None, ge=0, le=1),
    confidence_max: float | None = Query(None, ge=0, le=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(12, ge=1, le=100),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    run = await service.load_run(db, project.id, evaluation_id)
    attempt = await attempt_for(db, run, attempt_id)
    verified = await service.read_report(db, run, attempt)
    if (
        class_index is not None
        and class_index >= len(run.spec["classes"])
        or confidence_min is not None
        and confidence_max is not None
        and confidence_min > confidence_max
    ):
        raise service.reject("filter_invalid", "筛选类别或置信度范围无效", 422)
    selected = [
        sample
        for sample in verified["samples"]
        if service.engine.matches_filter(
            sample,
            class_index=class_index,
            error=error,
            confidence_min=confidence_min,
            confidence_max=confidence_max,
        )
    ]
    return {
        "evaluation_id": run.id,
        "attempt_id": attempt.id,
        "report_sha256": attempt.report["sha256"],
        "spec_digest": run.spec_digest,
        "total": len(selected),
        "offset": offset,
        "limit": limit,
        "items": selected[offset : offset + limit],
    }


@router.get(
    "/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/image",
    response_class=Response,
)
async def image(
    evaluation_id: uuid.UUID,
    attempt_id: uuid.UUID,
    ordinal: int,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    run = await service.load_run(db, project.id, evaluation_id)
    await attempt_for(db, run, attempt_id)
    spec = service.validate_spec(run)
    if not 0 <= ordinal < len(spec["samples"]):
        raise service.reject("sample_missing", "冻结样本不存在", 404)
    version = await db.get(TrainingDatasetVersion, run.dataset_id)
    try:
        png, preprocessing = await asyncio.to_thread(
            service.prepare_input, run, version, spec["samples"][ordinal]
        )
        row = await db.get(Sample, (run.id, ordinal))
        if row.request and row.request["preprocessing"] != preprocessing:
            raise ValueError("Frozen preprocessing differs")
    except Exception as exc:
        raise service.reject(
            "frozen_image_unavailable", "冻结图片未通过完整性或解码检查"
        ) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/source",
    response_model=EvaluationSourceOut,
)
async def source(
    evaluation_id: uuid.UUID,
    attempt_id: uuid.UUID,
    ordinal: int,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await service.load_run(db, project.id, evaluation_id)
    await attempt_for(db, run, attempt_id)
    spec = service.validate_spec(run)
    if not 0 <= ordinal < len(spec["samples"]):
        raise service.reject("sample_missing", "冻结样本不存在", 404)
    from app.services.sample_evaluation_sources import resolve_source

    try:
        return await resolve_source(
            db,
            run,
            attempt_id,
            ordinal,
            user,
            getattr(request.state, "api_key_id", None),
        )
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc


@router.delete(
    "/{evaluation_id}", status_code=204, response_class=Response, dependencies=WRITE
)
async def delete(
    evaluation_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    run = await service.load_run(db, project.id, evaluation_id, lock=True)
    from app.services.active_learning_pool import guard_source

    await guard_source(db, evaluation_id=run.id)
    if run.status not in service.TERMINAL:
        raise service.reject("evaluation_active", "评估尚未确认结束，不能删除")
    try:
        service.owners.resolve(run)
    except TrainingDatasetError as exc:
        raise service.reject(exc.code, str(exc), exc.status_code) from exc
    run.deletion_requested_at = run.deletion_requested_at or service.now()
    await db.commit()
    try:
        await service.purge_requested(db, run.id)
    except Exception as exc:
        raise service.reject(
            "cleanup_pending", "删除意图已保存；存储清理暂不可用，后台会继续重试", 503
        ) from exc
