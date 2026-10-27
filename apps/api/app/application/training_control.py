"""Compose cross-owner training commands around learning state owners."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.learning_policy import LearningTrigger
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services import (
    learning_policies,
    training_claims,
    training_observability,
    training_runs,
    training_studies,
    training_storage_cleanup,
)
from app.services.project_write_guard import lock_project_manager
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.training_dataset_bridge import TrainingDatasetError


async def resume_run(
    db: AsyncSession,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    training_authority: TrainingAuthorityPort,
) -> TrainingRun:
    preliminary = await training_runs.load_run(db, project_id, run_id)
    eligibility = await training_observability.recovery(
        db, preliminary, training_authority=training_authority
    )
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
        await training_claims.project_lock(db, project_id)
        if await learning_policies.active_run(
            db, project_id, excluding=run_id
        ) or await db.scalar(
            select(LearningTrigger.id)
            .where(
                LearningTrigger.project_id == project_id,
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
        await training_claims.project_lock(db, project_id)
        study = await training_studies.load(
            db, project_id, preliminary.study_id, lock=True
        )
    lease = await training_runs.lock_resource(db, preliminary.config["resource_id"])
    await lock_project_manager(db, actor_id, project_id)
    run = await training_runs.load_run(db, project_id, run_id, lock=True)
    if (
        run.status not in {"failed", "cancelled"}
        or lease.attempt_id == run.current_attempt_id
    ):
        raise HTTPException(status_code=409, detail="旧训练必须确认停止后才可恢复")
    await training_runs.authority(
        training_authority,
        project_id=run.project_id,
        actor_id=run.creator_id,
        api_key_id=run.authorization.get("api_key_id"),
    )
    eligibility = await training_observability.recovery(
        db,
        run,
        check_storage=False,
        lease=lease,
        training_authority=training_authority,
    )
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
    attempt = await training_runs.new_attempt(db, run, resume_from=parent.id)
    if study:
        training_runs.reserve_study_budget(attempt, run, allowance)
        training_runs.mark_study_manual_resume(run)
        training_studies.stage_manual_resume(study)
    if run.auto_trigger_id:
        training_runs.mark_policy_manual_resume(run)
        await learning_policies.requeue_after_manual_resume(db, run.auto_trigger_id)
    return run


async def delete_run(
    db: AsyncSession, project_id: uuid.UUID, run_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    preliminary = await training_runs.load_run(db, project_id, run_id)
    if preliminary.auto_trigger_id:
        await training_claims.project_lock(db, project_id)
    await lock_project_manager(db, actor_id, project_id)
    run = await training_runs.load_run(db, project_id, run_id, lock=True)
    if run.status not in training_runs.TERMINAL:
        raise HTTPException(status_code=409, detail="训练尚未结束，不能删除")
    if run.study_id:
        raise HTTPException(
            status_code=409,
            detail="实验属于保留的参数搜索，不能单独删除其计划与预算证据",
        )
    if run.auto_trigger_id:
        trigger = await db.get(LearningTrigger, run.auto_trigger_id)
        if trigger.status in {"building", "queued", "running"}:
            raise HTTPException(
                status_code=409, detail="请先检查自动策略以同步实验终态，再删除实验"
            )
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
    try:
        intent = training_storage_cleanup.stage_deletion(
            db, run, f"projects/{project_id}/runs/{run.id}/"
        )
    except TrainingDatasetError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from None
    await training_runs.stage_run_deletion(db, run)
    await db.commit()
    await training_storage_cleanup.purge_committed(db, intent.id)
