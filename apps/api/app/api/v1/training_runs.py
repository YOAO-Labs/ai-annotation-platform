"""Project training and the separately authenticated executor protocol."""

import secrets
import uuid
from copy import deepcopy
from datetime import timedelta
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import func, or_, select
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
from app.deps import (
    get_current_user,
    get_current_user_id,
    get_db,
    get_training_authority,
    require_project_owner,
    require_scopes,
)
from app.schemas.training_run import (
    ExecutorClaim,
    ExecutorClaimResponse,
    ExecutorFinish,
    ExecutorHeartbeat,
    ExecutorHeartbeatResponse,
    ExecutorRecovery,
    ExecutorRecoveryResponse,
    ExecutorUpload,
    ExecutorUploadResponse,
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
from app.application import training_control
from app.services.project_write_guard import lock_project_manager
from app.services import training_runs as service
from app.services import training_recipes as recipes
from app.services import training_observability as observations
from app.services import training_initialization as initialization
from app.services import training_adapters as adapters
from app.services import storage_access
from app.services.training_list_filters import (
    ISODate,
    created_date_bounds,
    literal_search_pattern,
    run_task_expression,
)
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.identity_contracts import IdentityActorFacts

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


@router.get("/catalog")
async def catalog(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = adapters.catalog()
    lease = await db.get(TrainingResourceLease, adapters.resource_id())
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
        (
            item
            for item in adapters.catalog()["items"]
            if item["framework"] == framework
        ),
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
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run, created = await service.admit(
        db,
        project.id,
        user,
        body,
        api_key_id=getattr(request.state, "api_key_id", None),
    )
    if created:
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
    if adapters.catalog()["enabled"]:
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
    user: IdentityActorFacts = Depends(get_current_user),
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
    run = await service.load_run(db, project.id, run_id)
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
    run = await service.load_run(db, project.id, run_id)
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
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.training_evaluations import read_evaluation

    await lock_project_manager(db, user.id, project.id)
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.id == run_id, TrainingRun.project_id == project.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    attempt = await db.get(TrainingAttempt, attempt_id)
    if run is None or attempt is None or attempt.run_id != run.id:
        raise HTTPException(status_code=404, detail="实验或执行尝试不存在")
    result = await read_evaluation(run, attempt, offset, limit, db=db)
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
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await service.cancel(db, project.id, run_id, user)
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
    actor_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    run = await training_control.resume_run(
        db, project.id, run_id, actor_id, training_authority=training_authority
    )
    await db.commit()
    return run


def _log_download_http_error(exc):
    failure = HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(failure)
    return failure


async def _download_artifact_context(db, project, user, run_id, attempt_id, name):
    await lock_project_manager(db, user.id, project.id)
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.id == run_id, TrainingRun.project_id == project.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="训练实验不存在")
    attempt = await db.get(
        TrainingAttempt,
        attempt_id,
        populate_existing=True,
        with_for_update={"read": True},
    )
    artifact = (
        (attempt.artifacts or {}).get(name)
        if attempt and attempt.run_id == run.id
        else None
    )
    if not artifact or attempt.status not in service.TERMINAL:
        raise HTTPException(status_code=404, detail="训练产物不存在或尚未校验完成")
    try:
        owner = service.attempt_storage_owner(run, attempt, artifact["key"])
    except TrainingDatasetError as exc:
        if artifact.get("role") == "log":
            raise _log_download_http_error(exc) from exc
        raise
    return run, attempt, deepcopy(artifact), owner


async def _read_log_download(db, project, user, run_id, attempt_id, name, context):
    run, attempt, artifact, owner = context
    if artifact["role"] != "log":
        raise HTTPException(status_code=404, detail="该产物不是训练日志")
    frozen = deepcopy((owner.ref, attempt.status, artifact))
    try:
        result = await service.storage_io.read(
            db,
            owner,
            "training_artifact_download",
            lambda transport: observations.read_redacted_log(
                run, attempt, name, artifact, transport=transport
            ),
            object_key=artifact["key"],
            sha256=artifact["sha256"],
            size_bytes=artifact["size_bytes"],
        )
    except TrainingDatasetError as exc:
        raise _log_download_http_error(exc) from exc
    _, latest_attempt, latest_artifact, latest_owner = await _download_artifact_context(
        db, project, user, run_id, attempt_id, name
    )
    if (latest_owner.ref, latest_attempt.status, latest_artifact) != frozen:
        raise HTTPException(status_code=409, detail="训练日志归属或校验信息已变化")
    return result


@router.get(
    "/{run_id}/attempts/{attempt_id}/artifacts/{name}/content",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def download_log_content(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    name: str,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    context = await _download_artifact_context(
        db, project, user, run_id, attempt_id, name
    )
    result = await _read_log_download(
        db, project, user, run_id, attempt_id, name, context
    )
    return Response(
        content=result["content"],
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{run_id}/attempts/{attempt_id}/artifacts/{name}/download")
async def download_artifact(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    name: str,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    context = await _download_artifact_context(
        db, project, user, run_id, attempt_id, name
    )
    _, _, artifact, owner = context
    if artifact["role"] == "log":
        result = await _read_log_download(
            db, project, user, run_id, attempt_id, name, context
        )
        return {
            **{key: value for key, value in result.items() if key != "content"},
            "url": (
                f"/projects/{project.id}/training-runs/{run_id}/attempts/{attempt_id}"
                f"/artifacts/{quote(name, safe='')}/content"
            ),
            "requires_authentication": True,
        }

    def download(transport):
        if not transport.verify_upload(artifact["key"], bucket=owner.bucket):
            raise HTTPException(
                status_code=409, detail="训练产物已不可用，请联系管理员"
            )
        return {
            "url": transport.generate_download_url(
                artifact["key"],
                expires_in=600,
                bucket=owner.bucket,
                download_name=name,
                align=False,
            ),
            "sha256": artifact["sha256"],
            "size_bytes": artifact["size_bytes"],
        }

    return await service.storage_io.read(
        db,
        owner,
        "training_artifact_download",
        download,
        object_key=artifact["key"],
        sha256=artifact["sha256"],
        size_bytes=artifact["size_bytes"],
    )


@router.delete(
    "/{run_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def delete_run(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await training_control.delete_run(db, project.id, run_id, actor_id)


@executor_router.post(
    "/claim",
    response_model=ExecutorClaimResponse,
    response_model_exclude_unset=True,
)
async def claim(
    body: ExecutorClaim,
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
) -> dict:
    result = await service.claim(db, body, training_authority=training_authority)
    await db.commit()
    return {"assignment": result}


@executor_router.post(
    "/attempts/{attempt_id}/heartbeat", response_model=ExecutorHeartbeatResponse
)
async def heartbeat(
    attempt_id: uuid.UUID,
    body: ExecutorHeartbeat,
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
) -> dict:
    result = await service.heartbeat(
        db, attempt_id, body, training_authority=training_authority
    )
    await db.commit()
    return result


@executor_router.post(
    "/attempts/{attempt_id}/uploads", response_model=ExecutorUploadResponse
)
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
    attempt_id: uuid.UUID,
    body: ExecutorFinish,
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    result = await service.finish(
        db, attempt_id, body, training_authority=training_authority
    )
    await db.commit()
    return result


@executor_router.post(
    "/attempts/{attempt_id}/recover", response_model=ExecutorRecoveryResponse
)
async def recover(
    attempt_id: uuid.UUID, body: ExecutorRecovery, db: AsyncSession = Depends(get_db)
) -> dict:
    await service.recover(db, attempt_id, body)
    await db.commit()
    return {"recovered": True}
