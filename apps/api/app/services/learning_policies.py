"""Serialize optional training triggers without changing project publication."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.services.project_access import get_workspace_project
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.db.models.user import User
from app.schemas.learning_policy import LearningPolicySave
from app.schemas.training_dataset import TrainingDatasetSelection
from app.schemas.training_run import TrainingRunCreate
from app.services import training_datasets as datasets, training_runs as runs
from app.services import training_adapters as adapters
from app.services import training_claims as claims
from app.services import async_job as jobs
from app.services.identity_contracts import IdentityActorFacts
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    semantic_partitions,
    semantic_partition_aliases,
)
from app.services import training_initialization as initialization
from app.services import project_task_dataset as task_datasets
from app.services import training_recipes as recipes
from app.services.project_write_guard import lock_project_manager
from app.services.training_dataset_insight_adapters import adapter_for
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.dataset_task_facts import ProjectTaskSourceReader
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.labeling_core_read_relations import batch_row, get_core_row

ACTIVE_TRIGGERS = {"building", "queued", "running"}
log = logging.getLogger(__name__)


async def save(
    db,
    project: Project,
    user: User | IdentityActorFacts,
    body: LearningPolicySave,
    *,
    api_key_id=None,
):
    await claims.project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
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
    template = claims.fixed_policy_template(body.framework, body.base_model)
    configuration = (
        adapters.resolve_configuration(
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
        with db.no_autoflush:
            batch = await get_core_row(db, batch_row, body.batch_id)
        if not batch or batch.project_id != project.id:
            raise HTTPException(status_code=404, detail="批次不属于当前项目")
    if policy is None:
        policy = LearningPolicy(project_id=project.id, revision=0, config={})
        db.add(policy)
        await db.flush()
    await cancel_unstarted(db, policy)
    policy.revision += 1
    policy.enabled, policy.creator_id = body.enabled, user.id
    policy.authorization = {"api_key_id": api_key_id}
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
    return policy


async def eligible(db, policy, *, training_authority: TrainingAuthorityPort):
    await training_authority.assert_authority(
        project_id=policy.project_id,
        actor_id=policy.creator_id,
        api_key_id=policy.authorization.get("api_key_id"),
        required_scopes=runs.SCOPES,
    )
    project = await get_workspace_project(
        db, project_id=policy.project_id, populate_existing=True
    )
    if project.status != "in_progress":
        raise TrainingDatasetError(
            "project_inactive", "项目已停用，自动训练停止", status_code=403
        )
    return project


async def active_run(db, project_id, *, excluding=None):
    stmt = select(TrainingRun.id).where(
        TrainingRun.project_id == project_id,
        TrainingRun.auto_trigger_id.is_not(None),
        TrainingRun.status.in_(runs.ACTIVE),
    )
    if excluding:
        stmt = stmt.where(TrainingRun.id != excluding)
    return await db.scalar(stmt.limit(1))


async def cancel_unstarted(db, policy):
    trigger = await db.scalar(
        select(LearningTrigger)
        .where(
            LearningTrigger.policy_id == policy.id,
            LearningTrigger.status.in_(ACTIVE_TRIGGERS),
        )
        .with_for_update()
    )
    policy.last_checked_at = runs.now()
    if not trigger:
        return
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.auto_trigger_id == trigger.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run:
        if run.status == "queued":
            attempt = await db.get(TrainingAttempt, run.current_attempt_id)
            await runs.mark_terminal(
                db,
                run,
                attempt,
                "cancelled",
                "自动策略关闭或已变化，未启动的训练已取消",
            )
            trigger.status, trigger.completed_at = "cancelled", runs.now()
        return
    dataset = await db.scalar(
        select(TrainingDatasetVersion)
        .where(TrainingDatasetVersion.id == trigger.dataset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dataset.status == "pending":
        dataset.status, dataset.completed_at = "cancelled", runs.now()
        await jobs.mark_cancelled(db, dataset.async_job_id)
    trigger.status, trigger.completed_at = "cancelled", runs.now()
    trigger.message = "策略关闭或已变化，未启动的自动实验已取消"


async def disable(
    db, project_id, user: User | IdentityActorFacts, expected_revision: int
):
    await claims.project_lock(db, project_id)
    await lock_project_manager(db, user.id, project_id)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.project_id == project_id)
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
    await cancel_unstarted(db, policy)
    return policy


async def delete(db, project_id, user: User | IdentityActorFacts) -> bool:
    await claims.project_lock(db, project_id)
    await lock_project_manager(db, user.id, project_id)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.project_id == project_id)
        .with_for_update()
    )
    if not policy:
        return False
    if (
        policy.enabled
        or await db.scalar(
            select(LearningTrigger.id)
            .where(
                LearningTrigger.policy_id == policy.id,
                LearningTrigger.status.in_(ACTIVE_TRIGGERS),
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
        sql_delete(LearningTrigger).where(LearningTrigger.policy_id == policy.id)
    )
    await db.delete(policy)
    return True


def growth(partitions, baseline):
    previous = (
        baseline.get("train", {})
        if baseline.get("classes_digest") == partitions["classes_digest"]
        else {}
    )
    return sum(
        previous.get(sha) != labels for sha, labels in partitions["train"].items()
    )


def terminal_trigger(trigger, status, message):
    trigger.status, trigger.message, trigger.completed_at = status, message, runs.now()


async def reconcile(db, policy, trigger):
    dataset = await db.get(
        TrainingDatasetVersion, trigger.dataset_id, populate_existing=True
    )
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.auto_trigger_id == trigger.id)
        .execution_options(populate_existing=True)
    )
    if run:
        if run.status in runs.ACTIVE:
            trigger.status = "queued" if run.status == "queued" else "running"
            policy.state, policy.message = (
                "active",
                "自动实验进行中，之后的变化会在下一轮检查",
            )
            return
        if run.status == "succeeded":
            policy.baseline = semantic_partitions(dataset.manifest)
            policy.baseline_dataset_id = dataset.id
            policy.blocked_train_digest = None
            # The parent pointer advances with success even if source content
            # does not. Avoid repackaging only for that pointer change.
            snapshot = {
                **dataset.snapshot,
                "selection": {
                    **dataset.snapshot.get("selection", {}),
                    "parent_id": str(dataset.id),
                },
            }
            policy.last_snapshot_digest = content_digest(snapshot)
            terminal_trigger(trigger, "succeeded", "训练完成；模型仍需人工注册和发布")
        else:
            if trigger.policy_revision == policy.revision:
                policy.blocked_train_digest = trigger.train_digest
            terminal_trigger(
                trigger,
                run.status,
                run.error_message or "自动训练未完成，变化尚未计入成功基线",
            )
        policy.state, policy.message = trigger.status, trigger.message
        policy.next_check_at = runs.now()
        return
    if not policy.enabled or trigger.policy_revision != policy.revision:
        await cancel_unstarted(db, policy)
        return
    if dataset.status in {"pending", "running"}:
        policy.state, policy.message = "building", "正在冻结自动候选数据"
        return
    if dataset.status != "ready":
        terminal_trigger(
            trigger,
            "failed",
            dataset.error_message or "自动训练数据未构建成功；修改策略后可重试",
        )
        policy.state, policy.message = "failed", trigger.message
        return
    partitions = semantic_partitions(dataset.manifest)
    trigger.train_digest = partitions["train_digest"]
    trigger.validation_digest = partitions["validation_digest"]
    candidate = content_digest(
        {"train": trigger.train_digest, "val": trigger.validation_digest}
    )
    aliases = semantic_partition_aliases(dataset.manifest)
    candidates = {
        content_digest(
            {"train": value["train_digest"], "val": value["validation_digest"]}
        )
        for value in aliases
    }
    duplicate = await db.scalar(
        select(LearningTrigger.id)
        .where(
            LearningTrigger.project_id == policy.project_id,
            LearningTrigger.policy_revision == policy.revision,
            LearningTrigger.candidate_digest.in_(candidates),
            LearningTrigger.id != trigger.id,
        )
        .limit(1)
    )
    trigger.changed_samples = growth(partitions, policy.baseline)
    reason = None
    if duplicate:
        reason = "相同候选已处理；修改并保存策略后才能明确重试"
    elif policy.blocked_train_digest in {value["train_digest"] for value in aliases}:
        reason = "相同训练内容曾失败；变化仍保留，修改并保存策略后才能重试"
    elif trigger.changed_samples < policy.config["min_changed_samples"]:
        reason = f"训练分区变化 {trigger.changed_samples} 个，未达到门槛 {policy.config['min_changed_samples']} 个"
    if not duplicate:
        trigger.candidate_digest = candidate
    if reason:
        terminal_trigger(trigger, "skipped", reason)
        policy.state, policy.message = "waiting", reason
        return
    if not claims.policy_template_current(policy):
        policy.enabled = False
        terminal_trigger(trigger, "cancelled", "训练目录已变化，请检查并重新保存策略")
        policy.state, policy.message = "suspended", trigger.message
        return
    template = policy.config["template"]
    parameters = {"parameters": policy.config["parameters"]}
    if policy.config.get("configuration"):
        pinned = policy.config["configuration"]
        parameters = {
            "configuration": {
                "schema_id": pinned["identity"]["schema_id"],
                "schema_digest": pinned["identity"]["schema_digest"],
                "values": pinned["user_values"],
            }
        }
    body = TrainingRunCreate(
        name=f"自动训练 · 策略 {policy.revision}",
        dataset_id=dataset.id,
        framework=template["framework"],
        base_model=template["base_model"]["id"],
        **parameters,
        initialization=initialization.reference(policy.config.get("initialization")),
        request_id=trigger.id,
    )
    try:
        config = await runs.frozen_config(db, policy.project_id, body)
    except HTTPException as exc:
        if not policy.config.get("initialization"):
            raise
        terminal_trigger(
            trigger, "failed", "初始化来源校验失败：" + str(exc.detail)[:400]
        )
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            trigger.message,
        )
        return
    if config.get("configuration") != policy.config.get("configuration"):
        raise HTTPException(status_code=409, detail="自动训练参数定义已变化")
    if config.get("initialization") != policy.config.get("initialization"):
        raise HTTPException(status_code=409, detail="自动训练初始化来源已变化")
    if any(config[key] != template[key] for key in template):
        raise HTTPException(status_code=409, detail="自动训练模板已变化")
    await runs.create_policy_triggered_run(
        db,
        project_id=policy.project_id,
        creator_id=policy.creator_id,
        dataset_id=dataset.id,
        initialization_model_id=initialization.model_id(config.get("initialization")),
        trigger_id=trigger.id,
        request_id=trigger.id,
        request_digest=content_digest(body.request_payload()),
        name=body.name,
        config=config,
        origin=policy.config.get("origin"),
        authorization=policy.authorization,
    )
    trigger.status, policy.state = "queued", "active"
    policy.last_started_at, policy.message = (
        runs.now(),
        "自动实验已排队；不会自动发布模型",
    )


async def check_policy(
    db,
    policy_id,
    *,
    source_reader: ProjectTaskSourceReader,
    training_authority: TrainingAuthorityPort,
    force=False,
):
    preliminary = await db.get(LearningPolicy, policy_id, populate_existing=True)
    if preliminary is None or not await claims.project_lock(
        db, preliminary.project_id, wait=False
    ):
        return
    project = None
    authorization_error = None
    try:
        project = await eligible(db, preliminary, training_authority=training_authority)
    except TrainingDatasetError as exc:
        authorization_error = str(exc)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.id == policy_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    trigger = await db.scalar(
        select(LearningTrigger)
        .where(
            LearningTrigger.policy_id == policy.id,
            LearningTrigger.status.in_(ACTIVE_TRIGGERS),
        )
        .with_for_update()
    )
    if authorization_error:
        policy.enabled = False
        if trigger:
            await reconcile(db, policy, trigger)
        await cancel_unstarted(db, policy)
        policy.state, policy.message = "suspended", authorization_error
        return
    if trigger:
        version = await db.get(TrainingDatasetVersion, trigger.dataset_id)
        if version is not None:
            await task_datasets.ensure_membership(db, version, project_locked=True)
        await reconcile(db, policy, trigger)
        return
    if not policy.enabled or (
        not force and policy.next_check_at and policy.next_check_at > runs.now()
    ):
        return
    if await active_run(db, policy.project_id):
        policy.state, policy.message = "active", "已有自动实验运行，请继续或取消该实验"
        return
    policy.last_checked_at = runs.now()
    policy.next_check_at = runs.now() + timedelta(
        minutes=policy.config["check_interval_minutes"]
    )
    if not claims.policy_template_current(policy):
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            "训练目录已变化，请检查并重新保存策略",
        )
        return
    if (
        policy.last_started_at
        and policy.last_started_at
        + timedelta(minutes=policy.config["cooldown_minutes"])
        > runs.now()
    ):
        policy.state, policy.message = (
            "cooldown",
            "冷却时间尚未结束，变化将保留到下一轮",
        )
        return
    selection = TrainingDatasetSelection(
        parent_id=policy.baseline_dataset_id or uuid.UUID(policy.config["parent_id"]),
        batch_id=policy.config.get("batch_id"),
        mode="reviewed",
    )
    parent = await db.get(TrainingDatasetVersion, selection.parent_id)
    try:
        valid_parent = (
            parent is not None and adapter_for(parent.manifest).task == "detect"
        )
    except TrainingDatasetError:
        valid_parent = False
    if not valid_parent:
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            "自动学习策略的父数据版本不是合格检测任务，请重新保存策略",
        )
        return
    snapshot, report = await datasets.freeze_selection(
        db,
        project,
        selection,
        source_reader=source_reader,
        actor_id=policy.creator_id,
        api_key_id=policy.authorization.get("api_key_id"),
    )
    if not report["ready"]:
        policy.state, policy.message = (
            "invalid_data",
            "数据预检未通过：" + "; ".join(i["message"] for i in report["issues"][:5]),
        )
        return
    snapshot["deduplicate"] = True
    digest = content_digest(snapshot)
    if digest == policy.last_snapshot_digest:
        policy.state, policy.message = (
            "waiting",
            "数据快照未变化，等待新增或修正的训练样本",
        )
        return
    version = await datasets.create_policy_candidate_version(
        db,
        project_id=policy.project_id,
        creator_id=policy.creator_id,
        parent_id=selection.parent_id,
        request_digest=digest,
        name=f"自动候选 · 策略 {policy.revision}",
        snapshot=snapshot,
        snapshot_digest=digest,
        preparation_report=datasets.frozen_preparation_report(digest, report),
    )
    db.add(
        LearningTrigger(
            policy_id=policy.id,
            project_id=policy.project_id,
            policy_revision=policy.revision,
            dataset_id=version.id,
            source_digest=digest,
        )
    )
    policy.last_snapshot_digest, policy.state, policy.message = (
        digest,
        "building",
        "正在冻结自动候选数据",
    )


async def check_project(
    db: AsyncSession,
    project: Project,
    user: User | IdentityActorFacts,
    *,
    source_reader: ProjectTaskSourceReader,
    training_authority: TrainingAuthorityPort,
) -> LearningPolicy:
    await claims.project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    policy = await db.scalar(
        select(LearningPolicy).where(LearningPolicy.project_id == project.id)
    )
    if not policy:
        raise HTTPException(status_code=404, detail="自动策略尚未配置")
    await check_policy(
        db,
        policy.id,
        source_reader=source_reader,
        training_authority=training_authority,
        force=True,
    )
    return policy


async def dispatch_pending(db, policy_id, *, dispatcher: TrainingDatasetDispatcher):
    # Durable pending identity closes a crash between commit and broker publish.
    trigger = await db.scalar(
        select(LearningTrigger).where(
            LearningTrigger.policy_id == policy_id, LearningTrigger.status == "building"
        )
    )
    if trigger:
        version = await db.get(TrainingDatasetVersion, trigger.dataset_id)
        if version and version.status == "pending":
            await datasets.dispatch_dataset(db, version, dispatcher=dispatcher)


async def requeue_after_manual_resume(db, trigger_id: uuid.UUID) -> None:
    trigger = await db.get(LearningTrigger, trigger_id)
    trigger.status, trigger.completed_at = "queued", None
