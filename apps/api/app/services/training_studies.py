"""Finite immutable plans, serialized admission and verified final-result groups."""

import itertools
import logging
import math
import uuid
from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from training_contract import definition
from training_contract.budget import POLICY_DIGEST, validate_receipt

from app.db.models.project import Project
from app.db.models.training_run import TrainingAttempt, TrainingEvent, TrainingRun
from app.db.models.training_study import TrainingStudy, TrainingTrial
from app.db.models.user import User
from app.schemas.training_study import TrainingStudyCreate
from app.services import (
    training_adapters as adapters,
    training_claims as claims,
    training_evaluations,
    training_initialization,
    training_recipes,
    training_runs as runs,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.project_access import get_workspace_project
from app.services.project_write_guard import lock_project_manager
from app.services.training_dataset_bridge import content_digest
from app.services.training_authority_contract import TrainingAuthorityPort

log = logging.getLogger(__name__)


async def freeze_plan(db, project_id, body):
    keys = sorted(body.grid)
    cardinality = math.prod(len(body.grid[key]) for key in keys)
    if cardinality > body.budgets.max_trials or cardinality > 20:
        raise HTTPException(
            status_code=422, detail="网格组合超过显式试验数量预算，不会截断执行"
        )
    try:
        schema = definition(body.base.framework, body.base.base_model)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="基础权重不在支持的参数目录中"
        ) from exc
    if schema["task"] != "detect":
        raise HTTPException(
            status_code=422, detail="参数搜索目前仅通过图片检测资格验证"
        )
    editable = {
        field["key"] for field in schema["fields"] if field["status"] == "editable"
    }
    for key in keys:
        if key not in editable:
            raise HTTPException(
                status_code=422, detail=f"{key}：网格只接受当前可编辑的原生参数键"
            )
        values = body.grid[key]
        if any(
            value == previous
            and type(value) is not bool
            and type(previous) is not bool
            or type(value) is type(previous)
            and value == previous
            for index, value in enumerate(values)
            for previous in values[:index]
        ):
            raise HTTPException(status_code=422, detail=f"{key}：网格值不能重复")
    baseline = await runs.frozen_config(db, project_id, body.base)
    origin = await training_recipes.origin_snapshot(
        db, project_id, body.base.origin, baseline
    )
    study_id = uuid.uuid5(project_id, "training-study:" + str(body.base.request_id))
    trials, epochs = [], 0
    for ordinal, values in enumerate(
        itertools.product(*(body.grid[key] for key in keys)), 1
    ):
        overrides = dict(zip(keys, values, strict=True))
        configuration = body.base.configuration.model_copy(
            update={"values": {**body.base.configuration.values, **overrides}}
        )
        resolved = adapters.resolve_configuration(
            body.base.framework, body.base.base_model, configuration.model_dump()
        )
        config = {
            **baseline,
            "configuration": resolved,
            "parameters": {
                key: resolved["effective_values"][key] for key in baseline["parameters"]
            },
            "fixed_options": {
                key: value
                for key, value in resolved["effective_values"].items()
                if resolved["origins"][key] == "platform"
            },
        }
        trial_id = uuid.uuid5(study_id, str(ordinal))
        config["study_budget"] = {
            "policy_digest": POLICY_DIGEST,
            "study_id": str(study_id),
            "trial_id": str(trial_id),
            "max_native_seconds": body.budgets.max_native_seconds,
        }
        epochs += config["parameters"]["epochs"]
        trials.append(
            {
                "id": str(trial_id),
                "ordinal": ordinal,
                "values": overrides,
                "config": config,
                "config_digest": content_digest(config),
            }
        )
    if epochs > body.budgets.max_planned_epochs:
        raise HTTPException(
            status_code=422,
            detail=f"计划总轮数 {epochs} 超过显式预算 {body.budgets.max_planned_epochs}",
        )
    plan = {
        "study_id": str(study_id),
        "policy_digest": POLICY_DIGEST,
        "allocation": "reserve-current-study-balance-before-enqueue",
        "objective": body.objective,
        "direction": body.direction,
        "budgets": body.budgets.model_dump(mode="json"),
        "planned_epochs": epochs,
        "grid": body.grid,
        "trials": trials,
        "origin": origin,
    }
    return plan, content_digest(plan)


async def create(
    db,
    project: Project,
    user: User | IdentityActorFacts,
    body: TrainingStudyCreate,
    *,
    api_key_id=None,
    training_authority: TrainingAuthorityPort,
):
    """Return the study and whether this admission created state to commit."""
    await claims.project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    request_digest = content_digest(body.request_payload())
    existing = await db.scalar(
        select(TrainingStudy).where(
            TrainingStudy.project_id == project.id,
            TrainingStudy.request_id == body.base.request_id,
        )
    )
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(
                status_code=409, detail="同一请求标识不能启动不同参数搜索"
            )
        return existing, False
    if project.status != "in_progress":
        raise HTTPException(status_code=409, detail="项目停用时不能启动参数搜索")
    plan, digest = await freeze_plan(db, project.id, body)
    if body.preview_digest != digest:
        raise HTTPException(
            status_code=409, detail="搜索计划已变化或尚未预览，请重新核对全部组合和预算"
        )
    first = plan["trials"][0]["config"]
    study = TrainingStudy(
        id=uuid.UUID(plan["study_id"]),
        project_id=project.id,
        creator_id=user.id,
        dataset_id=body.base.dataset_id,
        initialization_model_id=training_initialization.model_id(
            first.get("initialization")
        ),
        request_id=body.base.request_id,
        request_digest=request_digest,
        plan_digest=digest,
        name=body.base.name,
        plan=plan,
        origin=plan["origin"],
        authorization={"api_key_id": api_key_id},
    )
    db.add(study)
    await db.flush()
    for trial in plan["trials"]:
        db.add(
            TrainingTrial(
                id=uuid.UUID(trial["id"]),
                study_id=study.id,
                ordinal=trial["ordinal"],
                config=trial["config"],
                config_digest=trial["config_digest"],
                values=trial["values"],
            )
        )
    await db.flush()
    await coordinate(db, study.id, training_authority=training_authority)
    return study, True


async def load(db, project_id, study_id, *, lock=False):
    query = select(TrainingStudy).where(
        TrainingStudy.id == study_id, TrainingStudy.project_id == project_id
    )
    if lock:
        # Serialize non-key Study edits while allowing child FK KEY SHARE checks.
        # FOR UPDATE would invert Study→Run coordination against fenced Run exit.
        query = query.with_for_update(key_share=True).execution_options(
            populate_existing=True
        )
    study = await db.scalar(query)
    if not study:
        raise HTTPException(status_code=404, detail="参数搜索不存在")
    return study


async def children(db, study_id, *, lock=False):
    query = (
        select(TrainingRun)
        .where(TrainingRun.study_id == study_id)
        .order_by(TrainingRun.id)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return list(await db.scalars(query))


async def ledger(db, study, child_runs):
    attempts = (
        list(
            await db.scalars(
                select(TrainingAttempt).where(
                    TrainingAttempt.run_id.in_([run.id for run in child_runs])
                )
            )
        )
        if child_runs
        else []
    )
    consumed, reserved, unknown = 0.0, 0.0, []
    per_run = {}
    for attempt in attempts:
        accounting = attempt.native_accounting
        if accounting is not None:
            try:
                validate_receipt(accounting, attempt.native_budget)
                if accounting["state"] == "unknown":
                    unknown.append(str(attempt.id))
                else:
                    seconds = accounting["native_seconds"]
                    consumed += seconds
                    per_run[attempt.run_id] = per_run.get(attempt.run_id, 0.0) + seconds
            except (ValueError, TypeError, KeyError, AttributeError):
                unknown.append(str(attempt.id))
        elif attempt.status in runs.TERMINAL:
            if attempt.resource_id is not None:
                unknown.append(str(attempt.id))
        elif attempt.native_budget:
            reserved += attempt.native_budget["allowance_seconds"]
        else:
            unknown.append(str(attempt.id))
    maximum = study.plan["budgets"]["max_native_seconds"]
    return {
        "consumed_seconds": consumed,
        "reserved_seconds": reserved,
        "remaining_seconds": max(0.0, maximum - consumed - reserved),
        "overrun_seconds": max(0.0, consumed - maximum),
        "unknown_attempt_ids": unknown,
    }, per_run


async def cancel(
    db,
    project_id,
    study_id,
    user: User | IdentityActorFacts,
    *,
    training_authority: TrainingAuthorityPort,
):
    await claims.project_lock(db, project_id)
    await lock_project_manager(db, user.id, project_id)
    study = await load(db, project_id, study_id, lock=True)
    study.scheduling = False
    study.status = "cancelling"
    study.message = "已停止后续试验；活动进程按普通训练取消协议退出"
    for run in await children(db, study.id, lock=True):
        await runs.cancel_study_child(db, run)
    await coordinate(db, study.id, training_authority=training_authority)
    return study


async def coordinate(db, study_id, *, training_authority: TrainingAuthorityPort):
    preliminary = await db.get(TrainingStudy, study_id)
    if preliminary is None or not await claims.project_lock(
        db, preliminary.project_id, wait=False
    ):
        return
    error = None
    try:
        await runs.authority(
            training_authority,
            project_id=preliminary.project_id,
            actor_id=preliminary.creator_id,
            api_key_id=preliminary.authorization.get("api_key_id"),
        )
        project = await get_workspace_project(
            db,
            actor_id=preliminary.creator_id,
            project_id=preliminary.project_id,
            populate_existing=True,
        )
        if project.status != "in_progress":
            error = "项目已停用"
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        error = "搜索发起者的训练权限已撤销"
    study = await load(db, preliminary.project_id, study_id, lock=True)
    child_runs = await children(db, study.id, lock=True)
    study.last_checked_at = runs.now()
    if error:
        study.scheduling = False
        study.message = error
        for run in child_runs:
            await runs.cancel_study_child(db, run)
    active = [run for run in child_runs if run.status in runs.ACTIVE]
    if active:
        manual = any(
            run.authorization.get("study_manual_resume") and run.status != "cancelling"
            for run in active
        )
        study.status = "active" if study.scheduling or manual else "cancelling"
        if manual and not study.scheduling:
            study.message = "后续组合保持停止；当前子实验由管理员手动续训"
        return
    if not study.scheduling:
        if study.status in {"completed", "budget_exhausted"}:
            return
        study.status, study.completed_at = "cancelled", runs.now()
        return
    account, _ = await ledger(db, study, child_runs)
    if account["unknown_attempt_ids"]:
        study.status, study.message = (
            "blocked",
            "原生执行时间未知，停止后续试验；不能按零结算",
        )
        return
    trial = await db.scalar(
        select(TrainingTrial)
        .where(TrainingTrial.study_id == study.id, TrainingTrial.run_id.is_(None))
        .order_by(TrainingTrial.ordinal)
        .limit(1)
    )
    if trial is None:
        study.scheduling = False
        study.status, study.message, study.completed_at = (
            "completed",
            "全部试验已结束；结果按已验证的兼容评估身份分组",
            runs.now(),
        )
        return
    if account["remaining_seconds"] <= 0:
        study.scheduling = False
        study.status, study.message, study.completed_at = (
            "budget_exhausted",
            "原生计算时间预算已用尽，未启动的组合保留在计划中",
            runs.now(),
        )
        return
    value = {
        key: trial.config[key]
        for key in (
            "framework",
            "framework_version",
            "image_digest",
            "base_model",
            "configuration",
        )
    }
    if trial.config.get("initialization"):
        value["initialization"] = trial.config["initialization"]
    compatible, reason = training_recipes.compatibility(value)
    if not compatible:
        study.status, study.message = "blocked", reason
        return
    run = await runs.create_study_child(
        db,
        project_id=study.project_id,
        creator_id=study.creator_id,
        study_id=study.id,
        dataset_id=study.dataset_id,
        initialization_model_id=study.initialization_model_id,
        request_id=trial.id,
        request_digest=trial.config_digest,
        name=f"{study.name[:100]} · {trial.ordinal}",
        config=trial.config,
        config_digest=trial.config_digest,
        origin=study.origin,
        authorization=study.authorization,
        allowance=account["remaining_seconds"],
    )
    trial.run_id = run.id
    study.status, study.message, study.completed_at = (
        "active",
        "当前试验已进入普通训练队列；结束后再放行下一项",
        None,
    )


async def admit_resume(db, study, run):
    child_runs = await children(db, study.id)
    if any(child.id != run.id and child.status in runs.ACTIVE for child in child_runs):
        raise HTTPException(
            status_code=409, detail="搜索已有其他活动试验，不能同时续训"
        )
    account, per_run = await ledger(db, study, child_runs)
    if account["unknown_attempt_ids"]:
        raise HTTPException(
            status_code=409, detail="搜索存在未知原生执行时间，不能续训"
        )
    child_remaining = run.config["study_budget"]["max_native_seconds"] - per_run.get(
        run.id, 0.0
    )
    allowance = min(child_remaining, account["remaining_seconds"])
    if allowance <= 0:
        raise HTTPException(status_code=409, detail="搜索或试验已无剩余原生计算预算")
    return allowance


async def describe(db, study):
    child_runs = await children(db, study.id)
    by_id = {run.id: run for run in child_runs}
    account, _ = await ledger(db, study, child_runs)
    items, groups = [], {}
    for trial in await db.scalars(
        select(TrainingTrial)
        .where(TrainingTrial.study_id == study.id)
        .order_by(TrainingTrial.ordinal)
    ):
        run = by_id.get(trial.run_id)
        item = {
            "id": str(trial.id),
            "ordinal": trial.ordinal,
            "values": trial.values,
            "config_digest": trial.config_digest,
            "planned_epochs": trial.config["parameters"]["epochs"],
            "run_id": str(run.id) if run else None,
            "status": run.status
            if run
            else "pending"
            if study.scheduling
            else "skipped",
            "result": None,
            "result_error": None,
            "actual_epochs": None,
            "completion": None,
            "queue_seconds": None,
            "lease_seconds": None,
            "native_seconds": 0.0,
            "native_time_unknown": False,
        }
        if run:
            attempts = list(
                await db.scalars(
                    select(TrainingAttempt)
                    .where(TrainingAttempt.run_id == run.id)
                    .order_by(TrainingAttempt.ordinal)
                )
            )
            queue, occupancy = 0.0, 0.0
            for attempt in attempts:
                end = attempt.completed_at or runs.now()
                queue += max(
                    0,
                    ((attempt.started_at or end) - attempt.created_at).total_seconds(),
                )
                if attempt.started_at:
                    occupancy += max(0, (end - attempt.started_at).total_seconds())
                if (
                    attempt.native_accounting
                    and attempt.native_accounting.get("native_seconds") is not None
                ):
                    item["native_seconds"] += attempt.native_accounting[
                        "native_seconds"
                    ]
                if str(attempt.id) in account["unknown_attempt_ids"]:
                    item["native_time_unknown"] = True
            item.update(queue_seconds=queue, lease_seconds=occupancy)
            current = next(
                (a for a in attempts if a.id == run.current_attempt_id), None
            )
            if current:
                item["actual_epochs"] = await db.scalar(
                    select(func.max(TrainingEvent.payload["epoch"].as_integer())).where(
                        TrainingEvent.attempt_id == current.id,
                        TrainingEvent.kind == "epoch",
                    )
                )
            if run.status == "succeeded" and current:
                item["completion"] = (
                    "unknown"
                    if item["actual_epochs"] is None
                    else "target_reached"
                    if item["actual_epochs"] >= item["planned_epochs"]
                    else "early_stopped"
                )
                try:
                    evaluation = await training_evaluations.read_evaluation(
                        run, current, 0, 0
                    )
                    objective = evaluation["summary"].get(study.plan["objective"])
                    if (
                        evaluation["identity_status"] != "verified"
                        or not evaluation["compatibility_digest"]
                        or objective is None
                        or not math.isfinite(objective)
                    ):
                        raise HTTPException(
                            status_code=409, detail="最终报告缺少可排名的已验证目标指标"
                        )
                    item["result"] = {
                        "value": objective,
                        "compatibility_digest": evaluation["compatibility_digest"],
                        "attempt_id": str(current.id),
                        "report_sha256": evaluation["provenance"]["report_sha256"],
                        "weights_sha256": evaluation["provenance"]["weights_sha256"],
                    }
                    groups.setdefault(evaluation["compatibility_digest"], []).append(
                        item
                    )
                except HTTPException as exc:
                    item["result_error"] = str(exc.detail)
        items.append(item)
    rankings = []
    for identity, members in sorted(groups.items()):
        ranked = sorted(
            members, key=lambda item: (-item["result"]["value"], item["ordinal"])
        )
        rankings.append(
            {
                "compatibility_digest": identity,
                "trial_ids": [item["id"] for item in ranked],
            }
        )
    return {
        "id": str(study.id),
        "project_id": str(study.project_id),
        "name": study.name,
        "status": study.status,
        "scheduling": study.scheduling,
        "message": study.message,
        "plan_digest": study.plan_digest,
        "plan": study.plan,
        "accounting": account,
        "trials": items,
        "rankings": rankings,
        "created_at": study.created_at,
    }


async def tick(
    session_factory,
    *,
    training_authority_factory: Callable[[AsyncSession], TrainingAuthorityPort],
):
    async with session_factory() as db:
        ids = list(
            await db.scalars(
                select(TrainingStudy.id)
                .where(
                    or_(
                        TrainingStudy.scheduling.is_(True),
                        TrainingStudy.status.in_(("active", "cancelling")),
                    )
                )
                .order_by(TrainingStudy.last_checked_at.nullsfirst(), TrainingStudy.id)
                .limit(100)
            )
        )
    for identity in ids:
        async with session_factory() as db:
            try:
                await coordinate(
                    db,
                    identity,
                    training_authority=training_authority_factory(db),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                log.exception("Training Study coordination failed: %s", identity)


def stage_manual_resume(study: TrainingStudy) -> None:
    if study.status == "completed":
        study.scheduling = True
    study.status, study.completed_at = "active", None
