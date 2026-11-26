"""Serialize optional training triggers without changing project publication."""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.project_access import get_workspace_project
from app.services.learning_context import LearningActorContext, LearningProjectContext
from app.schemas.learning_policy import LearningPolicySave
from app.schemas.training_dataset import TrainingDatasetSelection
from app.schemas.training_run import TrainingRunCreate
from app.services import training_datasets as datasets, training_runs as runs
from app.services import (
    learning_selection_data as data,
    training_dataset_data,
    training_run_data,
)
from app.services import training_adapters as adapters
from app.services import training_claims as claims
from app.services import async_job as jobs
from app.services.async_job_terminal import JobTerminal
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
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.labeling_core_read_relations import batch_row, get_core_row

log = logging.getLogger(__name__)


async def save(
    db,
    project: LearningProjectContext,
    user: LearningActorContext | IdentityActorFacts,
    body: LearningPolicySave,
    *,
    api_key_id=None,
):
    task = body.task
    await claims.project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    policy = await data.get_policy(db, project.id, task, lock=True)
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
    parent = await training_dataset_data.read_version(
        db, body.parent_id, project_id=project.id
    )
    if not parent or parent.project_id != project.id or parent.status != "ready":
        raise HTTPException(
            status_code=422, detail="请选择当前项目已就绪的数据作为初始划分"
        )
    try:
        parent_task = adapter_for(parent.manifest).task
    except TrainingDatasetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if parent_task != task:
        raise HTTPException(
            status_code=422, detail="自动学习策略任务与父数据版本不匹配"
        )
    if task == "segment" and body.configuration is None:
        raise HTTPException(status_code=422, detail="分割策略须提交完整训练配置")
    pin = await initialization.resolve(
        db,
        project.id,
        body.initialization,
        body.framework,
        body.base_model,
        parent.manifest.get("classes"),
    )
    template = await claims.fixed_policy_template(
        db, body.framework, body.base_model, task
    )
    if body.configuration is not None:
        from app.services.training_dataset_bridge import contract_of

        configuration = await adapters.resolve_plugin_configuration(
            db,
            body.framework,
            body.base_model,
            body.configuration.model_dump(),
            data_contract=contract_of(parent.manifest),
        )
    else:
        configuration = None
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
        parent_id=body.parent_id,
        batch_id=body.batch_id,
        task=task,
        **({"bridge": parent.manifest["bridge"]} if task == "segment" else {}),
    )
    if body.batch_id:
        with db.no_autoflush:
            batch = await get_core_row(db, batch_row, body.batch_id)
        if not batch or batch.project_id != project.id:
            raise HTTPException(status_code=404, detail="批次不属于当前项目")
    if policy is None:
        policy = SimpleNamespace(
            id=None, project_id=project.id, task=task, revision=0, config={}
        )
    await cancel_unstarted(db, policy)
    revision = policy.revision
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
    if task == "segment":
        policy.config = {**policy.config, "bridge": parent.manifest["bridge"]}
    if configuration is not None:
        from training_contract.validation import CANONICAL

        policy.config = {
            **policy.config,
            "configuration": configuration,
            "parameters": {
                **policy.config["parameters"],
                **{
                    key: configuration["effective_values"][key]
                    for key in CANONICAL
                    if key in configuration["effective_values"]
                    and configuration["effective_values"][key] is not None
                },
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
    saved = await data.save_policy(db, policy, revision)
    if saved is None:
        raise HTTPException(status_code=409, detail="策略已被其他操作修改，请刷新")
    return saved


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
    return await training_run_data.active_auto_run(db, project_id, excluding=excluding)


async def cancel_unstarted(db, policy):
    trigger = (
        await data.lock_active_trigger(db, policy.id) if policy.id is not None else None
    )
    policy.last_checked_at = runs.now()
    if trigger:
        await _cancel_trigger(db, trigger)
        await _persist_trigger(db, trigger)


async def _cancel_trigger(db, trigger):
    run = await training_run_data.lock_trigger_run(db, trigger.id)
    if run:
        if run.status == "queued":
            attempt = await training_run_data.get_attempt(db, run.current_attempt_id)
            await runs.mark_terminal(
                db,
                run,
                attempt,
                "cancelled",
                "自动策略关闭或已变化，未启动的训练已取消",
            )
            trigger.status, trigger.completed_at = "cancelled", runs.now()
        return
    dataset = await training_dataset_data.read_version(
        db, trigger.dataset_id, project_id=trigger.project_id, lock=True
    )
    if dataset.status == "pending":
        await jobs.mark_cancelled(db, dataset.async_job_id)
        await datasets.reconcile_job_terminal(
            db, JobTerminal(dataset.async_job_id, "training_dataset", cancelled=True)
        )
    trigger.status, trigger.completed_at = "cancelled", runs.now()
    trigger.message = "策略关闭或已变化，未启动的自动实验已取消"


async def disable(
    db,
    project_id,
    user: LearningActorContext | IdentityActorFacts,
    expected_revision: int,
    *,
    task="detect",
):
    await claims.project_lock(db, project_id)
    await lock_project_manager(db, user.id, project_id)
    policy = await data.get_policy(db, project_id, task, lock=True)
    if not policy or policy.revision != expected_revision:
        raise HTTPException(status_code=409, detail="策略已变化，请刷新")
    await cancel_unstarted(db, policy)
    saved = await data.disable_policy(
        db, policy, "自动训练已关闭；已经启动的实验可以继续或显式取消"
    )
    if saved is None:
        raise HTTPException(status_code=409, detail="策略已变化，请刷新")
    return saved


async def delete(
    db,
    project_id,
    user: LearningActorContext | IdentityActorFacts,
    *,
    task="detect",
) -> bool:
    await claims.project_lock(db, project_id)
    await lock_project_manager(db, user.id, project_id)
    policy = await data.get_policy(db, project_id, task, lock=True)
    if not policy:
        return False
    if not await data.delete_policy(db, policy):
        raise HTTPException(
            status_code=409, detail="请先关闭策略并移除被触发记录引用的实验"
        )
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


async def _persist_trigger(db, trigger):
    if not await data.trigger_progress(db, trigger):
        raise HTTPException(status_code=409, detail="自动触发记录已变化，请刷新")


async def _persist_policy(db, policy):
    if not await data.policy_check_state(db, policy):
        raise HTTPException(status_code=409, detail="策略已变化，请刷新")
    return policy


async def reconcile(db, policy, trigger):
    await _reconcile(db, policy, trigger)
    await _persist_trigger(db, trigger)


async def _reconcile(db, policy, trigger):
    dataset = await training_dataset_data.read_version(
        db, trigger.dataset_id, project_id=trigger.project_id
    )
    run = await training_run_data.get_trigger_run(db, trigger.id)
    if run:
        if run.status in runs.ACTIVE:
            trigger.status = "queued" if run.status == "queued" else "running"
            policy.state, policy.message = (
                "active",
                "自动实验进行中，之后的变化会在下一轮检查",
            )
            return
        if run.status == "succeeded":
            baseline = semantic_partitions(dataset.manifest)
            if not await data.advance_baseline(db, policy, trigger, dataset, baseline):
                raise HTTPException(status_code=409, detail="自动实验的基线来源已变化")
            policy.baseline, policy.baseline_dataset_id = baseline, dataset.id
            policy.blocked_train_digest = None
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
        policy.last_checked_at = runs.now()
        await _cancel_trigger(db, trigger)
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
    duplicate = await data.duplicate_trigger(db, policy, trigger, candidates)
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
    if not await claims.policy_template_current(db, policy):
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
    await runs.create_policy_child(
        db,
        project_id=policy.project_id,
        creator_id=policy.creator_id,
        dataset_id=dataset.id,
        initialization_model_id=initialization.model_id(config.get("initialization")),
        auto_trigger_id=trigger.id,
        request_digest=content_digest(body.request_payload()),
        name=body.name,
        config=config,
        config_digest=content_digest(config),
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
    training_authority: TrainingAuthorityPort,
    force=False,
):
    preliminary = await data.policy_by_id(db, policy_id)
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
    policy = await data.policy_by_id(db, policy_id, lock=True)
    if policy is None:
        return
    trigger = await data.lock_active_trigger(db, policy.id)
    if authorization_error:
        policy.enabled = False
        if trigger:
            await reconcile(db, policy, trigger)
        await cancel_unstarted(db, policy)
        policy.state, policy.message = "suspended", authorization_error
        return await _persist_policy(db, policy)
    if trigger:
        version = await training_dataset_data.read_version(
            db, trigger.dataset_id, project_id=policy.project_id
        )
        if version is not None:
            await task_datasets.ensure_membership(db, version)
        await reconcile(db, policy, trigger)
        return await _persist_policy(db, policy)
    if not policy.enabled or (
        not force and policy.next_check_at and policy.next_check_at > runs.now()
    ):
        return
    if await active_run(db, policy.project_id):
        policy.state, policy.message = "active", "已有自动实验运行，请继续或取消该实验"
        return await _persist_policy(db, policy)
    if await data.other_active_trigger(
        db, policy.project_id, excluding_policy=policy.id
    ):
        policy.state, policy.message = (
            "active",
            "项目另一个任务正在构建或运行自动候选，当前任务等待",
        )
        return await _persist_policy(db, policy)
    checked_at = runs.now()
    if policy.last_checked_at and checked_at <= policy.last_checked_at:
        checked_at = policy.last_checked_at + timedelta(microseconds=1)
    policy.last_checked_at = checked_at
    policy.next_check_at = runs.now() + timedelta(
        minutes=policy.config["check_interval_minutes"]
    )
    if not await claims.policy_template_current(db, policy):
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            "训练目录已变化，请检查并重新保存策略",
        )
        return await _persist_policy(db, policy)
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
        return await _persist_policy(db, policy)
    selection = TrainingDatasetSelection(
        parent_id=policy.baseline_dataset_id or uuid.UUID(policy.config["parent_id"]),
        batch_id=policy.config.get("batch_id"),
        mode="reviewed",
        task=getattr(policy, "task", "detect"),
        **({"bridge": policy.config["bridge"]} if policy.config.get("bridge") else {}),
    )
    parent = await training_dataset_data.read_version(
        db, selection.parent_id, project_id=policy.project_id
    )
    try:
        valid_parent = (
            parent is not None
            and adapter_for(parent.manifest).task == getattr(policy, "task", "detect")
            and parent.manifest.get("bridge")
            == policy.config.get("bridge", parent.manifest.get("bridge"))
        )
    except TrainingDatasetError:
        valid_parent = False
    if not valid_parent:
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            "自动学习策略的父数据版本任务或桥接不匹配，请重新保存策略",
        )
        return await _persist_policy(db, policy)
    await _persist_policy(db, policy)
    context = _policy_context(policy)
    await db.commit()
    preparation_error = None
    try:
        prepared = await datasets.prepare_selection(
            db,
            project,
            selection,
            actor_id=context["creator_id"],
            api_key_id=context["authorization"].get("api_key_id"),
            trusted_actor=True,
        )
    except TrainingDatasetError as exc:
        if exc.status_code != 403:
            raise
        preparation_error = exc

    if not await claims.project_lock(db, context["project_id"], wait=False):
        return
    authorization_error = None
    try:
        await eligible(
            db,
            SimpleNamespace(**context),
            training_authority=training_authority,
        )
    except TrainingDatasetError as exc:
        authorization_error = str(exc)
    policy = await data.policy_by_id(db, policy_id, lock=True)
    if policy is None or _policy_context(policy) != context:
        return
    trigger = await data.lock_active_trigger(db, policy.id)
    if trigger is not None:
        return
    if authorization_error or preparation_error:
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            authorization_error or str(preparation_error),
        )
        return await _persist_policy(db, policy)
    if not policy.enabled:
        return
    if await active_run(db, policy.project_id):
        policy.state, policy.message = "active", "已有自动实验运行，请继续或取消该实验"
        return await _persist_policy(db, policy)
    if await data.other_active_trigger(
        db, policy.project_id, excluding_policy=policy.id
    ):
        policy.state, policy.message = (
            "active",
            "项目另一个任务正在构建或运行自动候选，当前任务等待",
        )
        return await _persist_policy(db, policy)
    if not await claims.policy_template_current(db, policy):
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            "训练目录已变化，请检查并重新保存策略",
        )
        return await _persist_policy(db, policy)
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
        return await _persist_policy(db, policy)
    snapshot, report = await datasets.finalize_selection(db, prepared)
    if not report["ready"]:
        policy.state, policy.message = (
            "invalid_data",
            "数据预检未通过：" + "; ".join(i["message"] for i in report["issues"][:5]),
        )
        return await _persist_policy(db, policy)
    snapshot["deduplicate"] = True
    digest = content_digest(snapshot)
    if digest == policy.last_snapshot_digest:
        policy.state, policy.message = (
            "waiting",
            "数据快照未变化，等待新增或修正的训练样本",
        )
        return await _persist_policy(db, policy)
    version = training_dataset_data.NewVersion(
        project_id=policy.project_id,
        creator_id=policy.creator_id,
        parent_id=selection.parent_id,
        request_id=uuid.uuid4(),
        request_digest=digest,
        name=f"自动候选 · 策略 {policy.revision}",
        snapshot=snapshot,
        snapshot_digest=digest,
        preparation_report=datasets.frozen_preparation_report(digest, report),
    )
    version = await training_dataset_data.insert_version(db, version)
    from app.services.training_dataset_storage import allocate

    await allocate(db, version)
    await task_datasets.ensure_membership(db, version)
    version = await datasets.enqueue_dataset(db, version)
    if not await data.build_trigger(db, policy, version, digest):
        raise HTTPException(status_code=409, detail="策略已变化，请刷新")
    policy.last_snapshot_digest, policy.state, policy.message = (
        digest,
        "building",
        "正在冻结自动候选数据",
    )
    await _persist_policy(db, policy)
    datasets.check_prepared_clock(prepared)


def _policy_context(policy):
    return deepcopy(vars(policy))


async def check_project(
    db: AsyncSession,
    project: LearningProjectContext,
    user: LearningActorContext | IdentityActorFacts,
    *,
    task="detect",
    training_authority: TrainingAuthorityPort,
):
    await claims.project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    policy = await data.get_policy(db, project.id, task)
    if not policy:
        raise HTTPException(status_code=404, detail="自动策略尚未配置")
    await check_policy(
        db,
        policy.id,
        training_authority=training_authority,
        force=True,
    )
    return await data.policy_by_id(db, policy.id)


async def dispatch_pending(db, policy_id, *, dispatcher: TrainingDatasetDispatcher):
    trigger = await data.building_trigger(db, policy_id)
    if trigger:
        version = await training_dataset_data.read_version(
            db, trigger.dataset_id, project_id=trigger.project_id
        )
        if version and version.status == "pending":
            await datasets.dispatch_dataset(db, version, dispatcher=dispatcher)


async def requeue_after_manual_resume(db, trigger_id: uuid.UUID) -> None:
    if not await data.requeue_trigger(db, trigger_id):
        raise HTTPException(status_code=409, detail="自动触发记录已变化，请刷新")
