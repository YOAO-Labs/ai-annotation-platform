"""Project training and the separately authenticated executor protocol."""

import asyncio
import secrets
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import or_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.project import Project
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingEvent,
    TrainingResourceLease,
    TrainingRun,
)
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.training_run import (
    ExecutorClaim,
    ExecutorFinish,
    ExecutorHeartbeat,
    ExecutorRecovery,
    ExecutorUpload,
    TrainingAttemptOut,
    TrainingRunCreate,
    TrainingRunOut,
    TrainingRunDetail,
    TrainingEvaluationOut,
    TrainingRegisteredModel,
)
from app.schemas.training_hub import TrainingParameterSchema
from app.schemas.training_initialization import TrainingInitializationEligibility
from training_contract import definition
from training_contract.model_return import registration_eligibility
from app.api.v1.training_datasets import lock_owner
from app.services import training_runs as service
from app.services import training_recipes as recipes
from app.services import training_observability as observations
from app.services import training_initialization as initialization
from app.services.training_list_filters import (
    ISODate,
    created_date_bounds,
    literal_search_pattern,
    run_task_expression,
)
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest

router = APIRouter(
    prefix="/projects/{project_id}/training-runs",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


async def executor_auth(x_training_executor_token: str = Header(default="")):
    expected = settings.training_executor_token
    if (
        not settings.training_enabled
        or len(expected) < 32
        or not secrets.compare_digest(expected, x_training_executor_token)
    ):
        raise HTTPException(status_code=403, detail="执行器认证失败")


executor_router = APIRouter(
    prefix="/internal/training", dependencies=[Depends(executor_auth)]
)


async def load_run(db, project_id, run_id, *, lock=False):
    statement = select(TrainingRun).where(
        TrainingRun.id == run_id, TrainingRun.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    run = await db.scalar(statement)
    if run is None:
        raise HTTPException(status_code=404, detail="训练实验不存在")
    return run


@router.get("/catalog")
async def catalog(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = service.catalog()
    lease = await db.get(TrainingResourceLease, service.resource_id())
    result["online"] = bool(
        lease
        and lease.last_seen_at
        and lease.last_seen_at > service.now() - timedelta(seconds=30)
    )
    result["busy"] = bool(lease and lease.attempt_id)
    service.annotate_readiness(
        result, lease.runtime if lease else {}, online=result["online"]
    )
    result["initialization_policy_digest"] = (
        lease.runtime.get("initialization_policy_digest") if lease else None
    )
    result["native_budget_policy_digest"] = (
        lease.runtime.get("native_budget_policy_digest") if lease else None
    )
    return result


@router.get(
    "/initialization-sources/{model_id}",
    response_model=TrainingInitializationEligibility,
)
async def initialization_source(
    model_id: uuid.UUID,
    framework: Literal["ultralytics", "libreyolo"],
    base_model: str = Query(min_length=1, max_length=64),
    dataset_id: uuid.UUID | None = None,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await initialization.eligibility(
        db, project.id, model_id, framework, base_model, dataset_id
    )


@router.post(":preflight", dependencies=[Depends(require_scopes("datasets:write"))])
async def preflight(
    body: TrainingRunCreate,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await service.frozen_config(db, project.id, body)
    origin = await recipes.origin_snapshot(db, project.id, body.origin, config)
    return {"config": config, "config_digest": content_digest(config), "origin": origin}


@router.get("/parameter-schema", response_model=TrainingParameterSchema)
async def parameter_schema(
    framework: Literal["ultralytics", "libreyolo"],
    base_model: str = Query(min_length=1, max_length=64),
    project: Project = Depends(require_project_owner),
):
    template = next(
        (item for item in service.catalog()["items"] if item["framework"] == framework),
        None,
    )
    if not template or not any(
        base["id"] == base_model for base in template["base_models"]
    ):
        raise HTTPException(status_code=422, detail="基础权重不在当前受控目录中")
    try:
        return definition(framework, base_model, version=template["version"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "",
    response_model=TrainingRunOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def create_run(
    body: TrainingRunCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_owner(db, user, project)
    request_digest = content_digest(body.request_payload())
    existing = await db.scalar(
        select(TrainingRun).where(
            TrainingRun.project_id == project.id,
            TrainingRun.request_id == body.request_id,
        )
    )
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(status_code=409, detail="同一请求标识不能用于不同实验")
        return existing
    config = await service.frozen_config(db, project.id, body)
    origin = await recipes.origin_snapshot(db, project.id, body.origin, config)
    if body.preflight_digest != content_digest(config):
        raise HTTPException(
            status_code=409, detail="训练配置已变化或尚未检查，请重新预检"
        )
    run = TrainingRun(
        project_id=project.id,
        creator_id=user.id,
        dataset_id=body.dataset_id,
        initialization_model_id=initialization.model_id(config.get("initialization")),
        request_id=body.request_id,
        request_digest=request_digest,
        name=body.name,
        config=config,
        config_digest=content_digest(config),
        origin=origin,
        authorization={"api_key_id": getattr(request.state, "api_key_id", None)},
    )
    db.add(run)
    await db.flush()
    await service.allocate_storage(db, run)
    await service.new_attempt(db, run)
    await db.commit()
    return run


@router.get("")
async def list_runs(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    q: str = Query("", max_length=120),
    status: str | None = Query(
        None,
        pattern="^(queued|preparing|training|evaluating|uploading|cancelling|lost|succeeded|failed|cancelled)$",
    ),
    task: Literal["detect", "segment"] | None = None,
    framework: Literal["ultralytics", "libreyolo"] | None = None,
    created_from: ISODate | None = Query(
        None, description="创建日期下界，UTC 日期（含）"
    ),
    created_to: ISODate | None = Query(
        None, description="创建日期上界，UTC 日期（含整天）"
    ),
) -> dict:
    start, end = created_date_bounds(created_from, created_to)
    predicates = [TrainingRun.project_id == project.id]
    pattern = literal_search_pattern(q)
    if pattern is not None:
        predicates.append(
            or_(
                TrainingRun.name.ilike(pattern, escape="\\"),
                TrainingRun.config["base_model"]["id"]
                .as_string()
                .ilike(pattern, escape="\\"),
            )
        )
    if status is not None:
        predicates.append(TrainingRun.status == status)
    if task is not None:
        predicates.append(run_task_expression(TrainingRun.config) == task)
    if framework is not None:
        predicates.append(TrainingRun.config["framework"].as_string() == framework)
    if start is not None:
        predicates.append(TrainingRun.created_at >= start)
    if end is not None:
        predicates.append(TrainingRun.created_at <= end)
    if service.catalog()["enabled"]:
        lease = await service.lock_resource(db)
        await service.observe_lost(db, lease)
        await db.commit()
    total = await db.scalar(
        select(func.count()).select_from(TrainingRun).where(*predicates)
    )
    items = (
        await db.scalars(
            select(TrainingRun)
            .where(*predicates)
            .order_by(TrainingRun.created_at.desc(), TrainingRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "total": total,
        "items": [
            TrainingRunOut.model_validate(item).model_dump(mode="json")
            for item in items
        ],
    }


@router.post(
    "/{run_id}/storage-pin",
    response_model=TrainingRunOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def pin_legacy_run_storage(
    run_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.training_run_storage_adoption import pin

    return await pin(
        db, run_id, user, project, getattr(request.state, "api_key_id", None)
    )


@router.get("/{run_id}", response_model=TrainingRunDetail)
async def get_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await load_run(db, project.id, run_id)
    attempts = (
        await db.scalars(
            select(TrainingAttempt)
            .where(TrainingAttempt.run_id == run.id)
            .order_by(TrainingAttempt.ordinal.desc())
        )
    ).all()
    registered_models = (
        await db.execute(
            select(
                TrainedModelVersion.id,
                TrainedModelVersion.attempt_id,
                TrainedModelVersion.name,
            )
            .where(
                TrainedModelVersion.project_id == project.id,
                TrainedModelVersion.run_id == run.id,
            )
            .order_by(TrainedModelVersion.created_at, TrainedModelVersion.id)
        )
    ).all()
    return {
        "run": TrainingRunOut.model_validate(run).model_dump(mode="json"),
        "registration_eligibility": registration_eligibility(run.config),
        "registered_models": [
            TrainingRegisteredModel.model_validate(model).model_dump(mode="json")
            for model in registered_models
        ],
        "attempts": [
            TrainingAttemptOut.model_validate(attempt).model_dump(mode="json")
            for attempt in attempts
        ],
    }


@router.get("/{run_id}/events")
async def get_events(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID | None = None,
    after: int = Query(0, ge=0),
    latest: bool = False,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await load_run(db, project.id, run_id)
    attempt = await db.get(TrainingAttempt, attempt_id or run.current_attempt_id)
    if attempt is None or attempt.run_id != run.id:
        raise HTTPException(status_code=404, detail="执行尝试不存在")
    statement = (
        select(TrainingEvent)
        .where(TrainingEvent.attempt_id == attempt.id, TrainingEvent.sequence > after)
        .order_by(TrainingEvent.sequence.desc() if latest else TrainingEvent.sequence)
    )
    events = (await db.scalars(statement.limit(100))).all()
    if latest:
        events.reverse()
    return {
        "attempt_id": str(attempt.id),
        "items": [
            {"sequence": e.sequence, "kind": e.kind, **e.payload} for e in events
        ],
    }


@router.get(
    "/{run_id}/attempts/{attempt_id}/evaluation", response_model=TrainingEvaluationOut
)
async def get_evaluation(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.training_evaluations import read_evaluation

    await lock_owner(db, user, project)
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.id == run_id, TrainingRun.project_id == project.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    attempt = await db.get(TrainingAttempt, attempt_id)
    if run is None or attempt is None or attempt.run_id != run.id:
        raise HTTPException(status_code=404, detail="实验或执行尝试不存在")
    result = await read_evaluation(run, attempt, offset, limit)
    await db.commit()
    return result


@router.post(
    "/{run_id}/cancel",
    response_model=TrainingRunOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def cancel_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    run = await load_run(db, project.id, run_id, lock=True)
    attempt = await db.get(TrainingAttempt, run.current_attempt_id)
    if run.status in service.TERMINAL:
        raise HTTPException(status_code=409, detail="训练实验已结束")
    if run.status == "queued":
        await service.mark_terminal(db, run, attempt, "cancelled")
    else:
        run.status = attempt.status = "cancelling"
    await db.commit()
    return run


@router.post(
    "/{run_id}/resume",
    response_model=TrainingRunOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def resume_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    preliminary = await load_run(db, project.id, run_id)
    eligibility = await observations.recovery(db, preliminary)
    if not eligibility["eligible"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "resume_unavailable",
                "reasons": eligibility["reasons"],
                "message": "当前执行尝试未满足恢复条件，请检查已停止状态、checkpoint 与运行环境",
            },
        )
    if preliminary.auto_trigger_id:
        from app.services import learning_policies

        await learning_policies.project_lock(db, project.id)
        from app.db.models.learning_policy import LearningTrigger

        if await learning_policies.active_run(
            db, project.id, excluding=run_id
        ) or await db.scalar(
            select(LearningTrigger.id)
            .where(
                LearningTrigger.project_id == project.id,
                LearningTrigger.status.in_(learning_policies.ACTIVE_TRIGGERS),
                LearningTrigger.id != preliminary.auto_trigger_id,
            )
            .limit(1)
        ):
            raise HTTPException(
                status_code=409, detail="项目已有其他自动候选或实验，请先结束它"
            )
    study = None
    if preliminary.study_id:
        from app.services import training_studies
        from app.services.learning_policies import project_lock

        await project_lock(db, project.id)
        study = await training_studies.load(
            db, project.id, preliminary.study_id, lock=True
        )
    lease = await service.lock_resource(db, preliminary.config["resource_id"])
    await lock_owner(db, user, project)
    run = await load_run(db, project.id, run_id, lock=True)
    service.storage_owner(run)
    if (
        run.status not in {"failed", "cancelled"}
        or lease.attempt_id == run.current_attempt_id
    ):
        raise HTTPException(status_code=409, detail="旧训练必须确认停止后才可恢复")
    await service.authority(db, run)
    eligibility = await observations.recovery(db, run, check_storage=False, lease=lease)
    if not eligibility["eligible"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "resume_unavailable",
                "reasons": eligibility["reasons"],
                "message": "恢复条件已变化，请刷新后重试",
            },
        )
    parent = await db.get(TrainingAttempt, run.current_attempt_id)
    checkpoint = next(
        (item for item in parent.artifacts.values() if item["role"] == "checkpoint"),
        None,
    )
    if (
        not checkpoint
        or not checkpoint.get("epoch")
        or checkpoint["epoch"] >= run.config["parameters"]["epochs"]
    ):
        raise HTTPException(
            status_code=409, detail="没有可恢复的未完成 checkpoint，请创建新实验"
        )
    allowance = await training_studies.admit_resume(db, study, run) if study else None
    attempt = await service.new_attempt(db, run, resume_from=parent.id)
    if study:
        training_studies.reserve(attempt, run, allowance)
        run.authorization = {**run.authorization, "study_manual_resume": True}
        if study.status == "completed":
            study.scheduling = True
        study.status, study.completed_at = "active", None
    if run.auto_trigger_id:
        run.authorization = {**run.authorization, "manual_resume": True}
        trigger = await db.get(LearningTrigger, run.auto_trigger_id)
        trigger.status, trigger.completed_at = "queued", None
    await db.commit()
    return run


@router.get("/{run_id}/attempts/{attempt_id}/artifacts/{name}/download")
async def download_artifact(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    name: str,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await lock_owner(db, user, project)
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.id == run_id, TrainingRun.project_id == project.id)
        .with_for_update(read=True)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="训练实验不存在")
    attempt = await db.get(TrainingAttempt, attempt_id)
    artifact = (
        (attempt.artifacts or {}).get(name)
        if attempt and attempt.run_id == run.id
        else None
    )
    if not artifact or attempt.status not in service.TERMINAL:
        raise HTTPException(status_code=404, detail="训练产物不存在或尚未校验完成")
    owner = service.attempt_storage_owner(run, attempt, artifact["key"])
    if artifact["role"] == "log":
        derived = await asyncio.to_thread(
            observations.redacted_log, run, attempt, name, artifact
        )
        return {
            **{key: value for key, value in derived.items() if key != "key"},
            "url": storage.generate_download_url(
                derived["key"],
                expires_in=600,
                bucket=owner.bucket,
                download_name=name,
                align=False,
            ),
        }
    if not await asyncio.to_thread(
        storage.verify_upload, artifact["key"], bucket=owner.bucket
    ):
        raise HTTPException(status_code=409, detail="训练产物已不可用，请联系管理员")
    return {
        "url": storage.generate_download_url(
            artifact["key"],
            expires_in=600,
            bucket=owner.bucket,
            download_name=name,
            align=False,
        ),
        "sha256": artifact["sha256"],
        "size_bytes": artifact["size_bytes"],
    }


@router.delete(
    "/{run_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def delete_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    preliminary = await load_run(db, project.id, run_id)
    if preliminary.auto_trigger_id:
        from app.services.learning_policies import project_lock

        await project_lock(db, project.id)
    await lock_owner(db, user, project)
    run = await load_run(db, project.id, run_id, lock=True)
    if run.status not in service.TERMINAL:
        raise HTTPException(status_code=409, detail="训练尚未结束，不能删除")
    if run.study_id:
        raise HTTPException(
            status_code=409,
            detail="实验属于保留的参数搜索，不能单独删除其计划与预算证据",
        )
    if run.auto_trigger_id:
        from app.db.models.learning_policy import LearningTrigger

        trigger = await db.get(LearningTrigger, run.auto_trigger_id)
        if trigger.status in {"building", "queued", "running"}:
            raise HTTPException(
                status_code=409, detail="请先检查自动策略以同步实验终态，再删除实验"
            )
    from app.db.models.trained_model import TrainedModelVersion

    if await db.scalar(
        select(TrainedModelVersion.id)
        .where(
            or_(
                TrainedModelVersion.run_id == run.id,
                TrainedModelVersion.attempt_id.in_(
                    select(TrainingAttempt.id).where(TrainingAttempt.run_id == run.id)
                ),
            )
        )
        .limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="训练产物已被模型版本引用，不能删除"
        )
    from app.services import training_storage_cleanup as cleanup

    try:
        await cleanup.delete_record(db, run, f"projects/{project.id}/runs/{run.id}/")
    except TrainingDatasetError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from None


@executor_router.post("/claim")
async def claim(body: ExecutorClaim, db: AsyncSession = Depends(get_db)) -> dict:
    result = await service.claim(db, body)
    await db.commit()
    return {"assignment": result}


@executor_router.post("/attempts/{attempt_id}/heartbeat")
async def heartbeat(
    attempt_id: uuid.UUID, body: ExecutorHeartbeat, db: AsyncSession = Depends(get_db)
) -> dict:
    result = await service.heartbeat(db, attempt_id, body)
    await db.commit()
    return result


@executor_router.post("/attempts/{attempt_id}/uploads")
async def uploads(
    attempt_id: uuid.UUID, body: ExecutorUpload, db: AsyncSession = Depends(get_db)
) -> dict:
    result = await service.authorize_upload(db, attempt_id, body)
    await db.commit()
    return result


@executor_router.post(
    "/attempts/{attempt_id}/finish", response_model=TrainingAttemptOut
)
async def finish(
    attempt_id: uuid.UUID, body: ExecutorFinish, db: AsyncSession = Depends(get_db)
):
    result = await service.finish(db, attempt_id, body)
    await db.commit()
    return result


@executor_router.post("/attempts/{attempt_id}/recover")
async def recover(
    attempt_id: uuid.UUID, body: ExecutorRecovery, db: AsyncSession = Depends(get_db)
) -> dict:
    if body.new_boot_id == body.boot_id:
        raise HTTPException(status_code=422, detail="恢复需要新的执行器启动身份")
    lease, run, attempt = await service.fenced(
        db, attempt_id, body, allow_terminal=True
    )
    if attempt.status not in service.TERMINAL:
        if lease.attempt_id != attempt.id:
            raise HTTPException(status_code=409, detail="GPU 租约已变化")
        await service.mark_terminal(
            db,
            run,
            attempt,
            "cancelled" if run.status == "cancelling" else "failed",
            "执行器重启，已通过独占资源锁确认旧进程退出",
        )
        from training_contract.observability import failure_receipt

        attempt.failure = failure_receipt(
            "execution_lost",
            "execution",
            run.config_digest,
            "执行器重启，已通过独占资源锁确认旧进程退出",
            source="platform",
        )
        attempt.runtime = {**attempt.runtime, "recovery_boot_id": str(body.new_boot_id)}
        lease.attempt_id = None
    await db.commit()
    return {"recovered": True}
