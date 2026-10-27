"""Shared project serialization and lock-safe training claim predicates."""

from __future__ import annotations

from aap_task_contract import normalize_task
from fastapi import HTTPException
from sqlalchemy import func, select
from training_contract import definition

from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.db.models.training_study import TrainingStudy
from app.services import training_adapters, training_initialization as initialization
from app.services.project_access import get_workspace_project


async def project_lock(db, project_id, *, wait=True):
    fn = func.pg_advisory_xact_lock if wait else func.pg_try_advisory_xact_lock
    result = await db.scalar(select(fn(func.hashtextextended(str(project_id), 0))))
    return True if wait else bool(result)


def fixed_policy_template(framework, base_model, task="detect"):
    template = training_adapters.controlled_template(framework, base_model)
    if normalize_task(definition(framework, base_model)["task"]) != task or (
        task == "segment"
        and (framework != "ultralytics" or base_model != "yolo11n-seg")
    ):
        raise HTTPException(
            status_code=422, detail="基础模型与自动策略任务或受控规格不匹配"
        )
    return template


def policy_template_current(policy):
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
            fixed_policy_template(
                expected["framework"],
                expected["base_model"]["id"],
                getattr(policy, "task", "detect"),
            )
            == expected
        )
    except (HTTPException, KeyError, ValueError, TypeError):
        return False


async def policy_claim_allowed(db, run):
    if run.auto_trigger_id is None:
        return True
    if run.authorization.get("manual_resume"):
        project = await get_workspace_project(
            db,
            actor_id=run.creator_id,
            project_id=run.project_id,
            populate_existing=True,
        )
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
    project = await get_workspace_project(
        db,
        actor_id=run.creator_id,
        project_id=run.project_id,
        populate_existing=True,
    )
    return bool(
        policy.enabled
        and policy.revision == trigger.policy_revision
        and project.status == "in_progress"
        and policy_template_current(policy)
        and run.config.get("initialization") == policy.config.get("initialization")
    )


async def study_claim_allowed(db, run):
    if run.study_id is None:
        return True
    study = await db.get(TrainingStudy, run.study_id, populate_existing=True)
    project = await get_workspace_project(
        db,
        actor_id=run.creator_id,
        project_id=run.project_id,
        populate_existing=True,
    )
    return bool(
        study
        and project.status == "in_progress"
        and (study.scheduling or run.authorization.get("study_manual_resume"))
    )
