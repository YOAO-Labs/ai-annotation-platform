"""Manager-owned automatic training policy and paginated trigger history."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, select
from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.api.v1.training_datasets import lock_owner
from app.schemas.learning_policy import (
    LearningPolicyOut,
    LearningPolicySave,
    LearningTriggerOut,
    LearningTriggerPage,
)
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import learning_policies as service, training_runs as runs
from app.services import training_recipes as recipes
from app.services import training_initialization as initialization

router = APIRouter(
    prefix="/projects/{project_id}/learning-policy",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.get("")
async def get_policy(
    project: Project = Depends(require_project_owner), db=Depends(get_db)
) -> LearningPolicyOut | None:
    return await db.scalar(
        select(LearningPolicy).where(LearningPolicy.project_id == project.id)
    )


@router.put(
    "",
    response_model=LearningPolicyOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def save_policy(
    body: LearningPolicySave,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await service.project_lock(db, project.id)
    await lock_owner(db, user, project)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.project_id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (policy.revision if policy else 0) != body.expected_revision:
        raise HTTPException(status_code=409, detail="策略已被其他操作修改，请刷新")
    if policy and policy.config.get("configuration") and body.configuration is None:
        raise HTTPException(
            status_code=409,
            detail="当前策略使用完整训练配置，请保留 configuration，不能静默降为六参数配置",
        )
    if policy and policy.config.get("initialization") and body.initialization is None:
        raise HTTPException(
            status_code=409,
            detail="当前策略固定了项目初始化权重；请保留来源或明确选择目录初始化",
        )
    parent = await db.get(TrainingDatasetVersion, body.parent_id)
    if not parent or parent.project_id != project.id or parent.status != "ready":
        raise HTTPException(
            status_code=422, detail="请选择当前项目已就绪的数据作为初始划分"
        )
    from app.services.training_dataset_bridge import TrainingDatasetError
    from app.services.training_dataset_insight_adapters import adapter_for

    try:
        parent_task = adapter_for(parent.manifest).task
    except TrainingDatasetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if parent_task != "detect":
        raise HTTPException(status_code=422, detail="自动学习策略仅接受检测父数据版本")
    pin = await initialization.resolve(
        db,
        project.id,
        body.initialization,
        body.framework,
        body.base_model,
        parent.manifest.get("classes"),
    )
    template = service.fixed_template(body.framework, body.base_model)
    configuration = (
        runs.resolve_configuration(
            body.framework, body.base_model, body.configuration.model_dump()
        )
        if body.configuration is not None
        else None
    )
    origin = await recipes.origin_snapshot(
        db,
        project.id,
        body.origin,
        {
            **template,
            "configuration": configuration,
            **({"initialization": pin} if pin else {}),
        },
        retain=policy.config.get("origin") if policy else None,
        strict=True,
    )
    if body.enabled and project.status != "in_progress":
        raise HTTPException(status_code=409, detail="仅进行中的项目可启用自动训练")
    selection = TrainingDatasetSelection(
        parent_id=body.parent_id, batch_id=body.batch_id
    )
    if body.batch_id:
        from app.db.models.task_batch import TaskBatch

        batch = await db.get(TaskBatch, body.batch_id)
        if not batch or batch.project_id != project.id:
            raise HTTPException(status_code=404, detail="批次不属于当前项目")
    if policy is None:
        policy = LearningPolicy(project_id=project.id, revision=0, config={})
        db.add(policy)
        await db.flush()
    await service.cancel_unstarted(db, policy)
    policy.revision += 1
    policy.enabled, policy.creator_id = body.enabled, user.id
    policy.authorization = {"api_key_id": getattr(request.state, "api_key_id", None)}
    policy.initialization_model_id = initialization.model_id(pin)
    policy.config = {
        "parent_id": str(selection.parent_id),
        "batch_id": str(selection.batch_id) if selection.batch_id else None,
        "template": template,
        "parameters": body.parameters.model_dump(),
        "min_changed_samples": body.min_changed_samples,
        "check_interval_minutes": body.check_interval_minutes,
        "cooldown_minutes": body.cooldown_minutes,
    }
    if configuration is not None:
        from training_contract.validation import CANONICAL

        policy.config = {
            **policy.config,
            "configuration": configuration,
            "parameters": {
                key: configuration["effective_values"][key] for key in CANONICAL
            },
        }
    if origin is not None:
        policy.config = {**policy.config, "origin": origin}
    if pin is not None:
        policy.config = {**policy.config, "initialization": pin}
    policy.blocked_train_digest = policy.last_snapshot_digest = None
    policy.next_check_at = runs.now()
    policy.state, policy.message = (
        ("waiting", "策略已启用，等待检查")
        if body.enabled
        else ("disabled", "自动训练已关闭，历史记录保留")
    )
    await db.commit()
    return policy


@router.post(
    "/disable",
    response_model=LearningPolicyOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def disable_policy(
    expected_revision: int = Query(ge=1),
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await service.project_lock(db, project.id)
    await lock_owner(db, user, project)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.project_id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not policy or policy.revision != expected_revision:
        raise HTTPException(status_code=409, detail="策略已变化，请刷新")
    policy.enabled = False
    policy.revision += 1
    policy.state, policy.message = (
        "disabled",
        "自动训练已关闭；已经启动的实验可以继续或显式取消",
    )
    await service.cancel_unstarted(db, policy)
    await db.commit()
    return policy


@router.post(
    "/check",
    response_model=LearningPolicyOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def check_policy(
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await service.project_lock(db, project.id)
    await lock_owner(db, user, project)
    policy = await db.scalar(
        select(LearningPolicy).where(LearningPolicy.project_id == project.id)
    )
    if not policy:
        raise HTTPException(status_code=404, detail="自动策略尚未配置")
    await service.check_policy(db, policy.id, force=True)
    await db.commit()
    await service.dispatch_pending(db, policy.id)
    await db.refresh(policy)
    return policy


@router.get("/triggers", response_model=LearningTriggerPage)
async def list_triggers(
    project: Project = Depends(require_project_owner),
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await db.scalar(
        select(func.count())
        .select_from(LearningTrigger)
        .where(LearningTrigger.project_id == project.id)
    )
    triggers = list(
        await db.scalars(
            select(LearningTrigger)
            .where(LearningTrigger.project_id == project.id)
            .order_by(LearningTrigger.created_at.desc(), LearningTrigger.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    run_ids = (
        {
            r.auto_trigger_id: str(r.id)
            for r in await db.scalars(
                select(TrainingRun).where(
                    TrainingRun.auto_trigger_id.in_([t.id for t in triggers])
                )
            )
        }
        if triggers
        else {}
    )
    return {
        "total": total,
        "items": [
            {
                **LearningTriggerOut.model_validate(t).model_dump(mode="json"),
                "run_id": run_ids.get(t.id),
            }
            for t in triggers
        ],
    }


@router.delete(
    "",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def delete_policy(
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await service.project_lock(db, project.id)
    await lock_owner(db, user, project)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.project_id == project.id)
        .with_for_update()
    )
    if not policy:
        return
    if (
        policy.enabled
        or await db.scalar(
            select(LearningTrigger.id)
            .where(
                LearningTrigger.policy_id == policy.id,
                LearningTrigger.status.in_(service.ACTIVE_TRIGGERS),
            )
            .limit(1)
        )
        or await db.scalar(
            select(TrainingRun.id)
            .join(LearningTrigger, LearningTrigger.id == TrainingRun.auto_trigger_id)
            .where(LearningTrigger.policy_id == policy.id)
            .limit(1)
        )
    ):
        raise HTTPException(
            status_code=409, detail="请先关闭策略并移除被触发记录引用的实验"
        )
    await db.execute(
        delete(LearningTrigger).where(LearningTrigger.policy_id == policy.id)
    )
    await db.delete(policy)
    await db.commit()
