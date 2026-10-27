"""Serialize optional training triggers without changing project publication."""

from __future__ import annotations

from aap_task_contract import normalize_task
from copy import deepcopy
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.schemas.training_dataset import TrainingDatasetSelection
from app.schemas.training_run import TrainingRunCreate
from app.services import training_datasets as datasets, training_runs as runs
from app.services import async_job as jobs
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    semantic_partitions,
    semantic_partition_aliases,
)
from app.services import training_initialization as initialization
from app.services import project_task_dataset as task_datasets
from app.services.training_dataset_insight_adapters import adapter_for

ACTIVE_TRIGGERS = {"building", "queued", "running"}
CHECK_BATCH_SIZE = 100
log = logging.getLogger(__name__)


async def project_lock(db, project_id, *, wait=True):
    fn = func.pg_advisory_xact_lock if wait else func.pg_try_advisory_xact_lock
    result = await db.scalar(select(fn(func.hashtextextended(str(project_id), 0))))
    return True if wait else bool(result)


def fixed_template(framework, base_model, task="detect"):
    from training_contract import definition

    template = runs.controlled_template(framework, base_model)
    if normalize_task(definition(framework, base_model)["task"]) != task or (
        task == "segment"
        and (framework != "ultralytics" or base_model != "yolo11n-seg")
    ):
        raise HTTPException(
            status_code=422, detail="基础模型与自动策略任务或受控规格不匹配"
        )
    return template


def template_current(policy):
    try:
        expected = policy.config["template"]
        pin = policy.config.get("initialization")
        if pin:
            if initialization.compatible_pin(
                pin, expected["framework"], expected["base_model"]["id"]
            ) or getattr(
                policy, "initialization_model_id", None
            ) != initialization.model_id(pin):
                return False
        elif getattr(policy, "initialization_model_id", None) is not None:
            return False
        if policy.config.get("configuration"):
            from training_contract.catalog import definition_for_identity

            definition_for_identity(
                expected["framework"],
                expected["base_model"]["id"],
                policy.config["configuration"]["identity"],
            )
        return (
            fixed_template(
                expected["framework"],
                expected["base_model"]["id"],
                getattr(policy, "task", "detect"),
            )
            == expected
        )
    except (HTTPException, KeyError, ValueError, TypeError):
        return False


async def eligible(db, policy):
    await datasets.assert_training_authority(
        db,
        project_id=policy.project_id,
        actor_id=policy.creator_id,
        api_key_id=policy.authorization.get("api_key_id"),
        required_scopes=runs.SCOPES,
    )
    project = await db.get(Project, policy.project_id, populate_existing=True)
    if project.status != "in_progress":
        raise TrainingDatasetError(
            "project_inactive", "项目已停用，自动训练停止", status_code=403
        )
    return project


async def claim_allowed(db, run):
    if run.auto_trigger_id is None:
        return True
    if run.authorization.get("manual_resume"):
        project = await db.get(Project, run.project_id, populate_existing=True)
        return project.status == "in_progress"
    # Claim already owns resource/run locks. Never acquire a policy lock here.
    row = (
        await db.execute(
            select(LearningPolicy, LearningTrigger)
            .join(LearningTrigger, LearningTrigger.policy_id == LearningPolicy.id)
            .where(LearningTrigger.id == run.auto_trigger_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return False
    policy, trigger = row
    project = await db.get(Project, run.project_id, populate_existing=True)
    return bool(
        policy.enabled
        and policy.revision == trigger.policy_revision
        and project.status == "in_progress"
        and template_current(policy)
        and run.config.get("initialization") == policy.config.get("initialization")
    )


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
            LearningTrigger.policy_id == policy.id,
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
    if not template_current(policy):
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
    run = TrainingRun(
        project_id=policy.project_id,
        creator_id=policy.creator_id,
        dataset_id=dataset.id,
        initialization_model_id=initialization.model_id(config.get("initialization")),
        auto_trigger_id=trigger.id,
        request_id=trigger.id,
        request_digest=content_digest(body.request_payload()),
        name=body.name,
        config=config,
        config_digest=content_digest(config),
        origin=policy.config.get("origin"),
        authorization=policy.authorization,
    )
    db.add(run)
    await db.flush()
    await runs.allocate_storage(db, run)
    await runs.new_attempt(db, run)
    trigger.status, policy.state = "queued", "active"
    policy.last_started_at, policy.message = (
        runs.now(),
        "自动实验已排队；不会自动发布模型",
    )


async def check_policy(db, policy_id, *, force=False):
    preliminary = await db.get(LearningPolicy, policy_id, populate_existing=True)
    if preliminary is None or not await project_lock(
        db, preliminary.project_id, wait=False
    ):
        return
    project = None
    authorization_error = None
    try:
        project = await eligible(db, preliminary)
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
    if await db.scalar(
        select(LearningTrigger.id)
        .where(
            LearningTrigger.project_id == policy.project_id,
            LearningTrigger.policy_id != policy.id,
            LearningTrigger.status.in_(ACTIVE_TRIGGERS),
        )
        .limit(1)
    ):
        policy.state, policy.message = (
            "active",
            "项目另一个任务正在构建或运行自动候选，当前任务等待",
        )
        return
    checked_at = runs.now()
    if policy.last_checked_at and checked_at <= policy.last_checked_at:
        checked_at = policy.last_checked_at + timedelta(microseconds=1)
    policy.last_checked_at = checked_at
    policy.next_check_at = runs.now() + timedelta(
        minutes=policy.config["check_interval_minutes"]
    )
    if not template_current(policy):
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
        task=getattr(policy, "task", "detect"),
        **({"bridge": policy.config["bridge"]} if policy.config.get("bridge") else {}),
    )
    parent = await db.get(TrainingDatasetVersion, selection.parent_id)
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
        return
    # This durable scheduling claim releases all business locks before physical
    # reads. Compare every persisted field on return so an editor or a newer
    # checker, including one that found unchanged input, always wins.
    await db.flush()
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

    if not await project_lock(db, context["project_id"], wait=False):
        return
    authorization_error = None
    try:
        await eligible(db, SimpleNamespace(**context))
    except TrainingDatasetError as exc:
        authorization_error = str(exc)
    policy = await db.scalar(
        select(LearningPolicy)
        .where(LearningPolicy.id == policy_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if policy is None or _policy_context(policy) != context:
        return
    trigger = await db.scalar(
        select(LearningTrigger)
        .where(
            LearningTrigger.policy_id == policy.id,
            LearningTrigger.status.in_(ACTIVE_TRIGGERS),
        )
        .with_for_update()
    )
    if trigger is not None:
        return
    if authorization_error or preparation_error:
        policy.enabled, policy.state, policy.message = (
            False,
            "suspended",
            authorization_error or str(preparation_error),
        )
        return
    if not policy.enabled:
        return
    if await active_run(db, policy.project_id):
        policy.state, policy.message = "active", "已有自动实验运行，请继续或取消该实验"
        return
    if await db.scalar(
        select(LearningTrigger.id)
        .where(
            LearningTrigger.project_id == policy.project_id,
            LearningTrigger.policy_id != policy.id,
            LearningTrigger.status.in_(ACTIVE_TRIGGERS),
        )
        .limit(1)
    ):
        policy.state, policy.message = (
            "active",
            "项目另一个任务正在构建或运行自动候选，当前任务等待",
        )
        return
    if not template_current(policy):
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
    snapshot, report = await datasets.finalize_selection(db, prepared)
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
    version = TrainingDatasetVersion(
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
    db.add(version)
    await db.flush()
    from app.services.training_dataset_storage import allocate

    await allocate(db, version)
    await task_datasets.ensure_membership(db, version, project_locked=True)
    await datasets.enqueue_dataset(db, version)
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
    await db.flush()
    datasets.check_prepared_clock(prepared)


def _policy_context(policy):
    return deepcopy(
        {
            column.key: getattr(policy, column.key)
            for column in LearningPolicy.__table__.columns
        }
    )


async def dispatch_pending(db, policy_id):
    # Durable pending identity closes a crash between commit and broker publish.
    trigger = await db.scalar(
        select(LearningTrigger).where(
            LearningTrigger.policy_id == policy_id, LearningTrigger.status == "building"
        )
    )
    if trigger:
        version = await db.get(TrainingDatasetVersion, trigger.dataset_id)
        if version and version.status == "pending":
            await datasets.dispatch_dataset(db, version)


async def tick(session_factory, *, policy_id=None, force=False):
    async with session_factory() as db:
        # Advance a durable fair ordering for active work as well as due scans.
        # A long/lost run must not pin the same first page indefinitely.
        ids = (
            [policy_id]
            if policy_id
            else list(
                await db.scalars(
                    select(LearningPolicy.id)
                    .where(
                        or_(
                            LearningPolicy.enabled.is_(True)
                            & (
                                LearningPolicy.next_check_at.is_(None)
                                | (LearningPolicy.next_check_at <= runs.now())
                            ),
                            LearningPolicy.id.in_(
                                select(LearningTrigger.policy_id).where(
                                    LearningTrigger.status.in_(ACTIVE_TRIGGERS)
                                )
                            ),
                        )
                    )
                    .order_by(
                        LearningPolicy.last_checked_at.nullsfirst(), LearningPolicy.id
                    )
                    .limit(CHECK_BATCH_SIZE)
                )
            )
        )
    for identity in ids:
        async with session_factory() as db:
            try:
                await check_policy(db, identity, force=force)
                await db.commit()
                await dispatch_pending(db, identity)
            except Exception:
                await db.rollback()
                log.exception("Automatic learning policy check failed: %s", identity)
