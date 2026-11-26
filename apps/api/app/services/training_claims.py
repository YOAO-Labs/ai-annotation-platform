"""Shared project serialization and lock-safe training claim predicates."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from yoao_task_contract import normalize_task

from app.services import learning_selection_data as data
from app.services import training_adapters, training_initialization as initialization
from app.services.project_access import get_workspace_project


async def project_lock(db, project_id, *, wait=True):
    fn = func.pg_advisory_xact_lock if wait else func.pg_try_advisory_xact_lock
    result = await db.scalar(select(fn(func.hashtextextended(str(project_id), 0))))
    return True if wait else bool(result)


async def fixed_policy_template(db, framework, base_model, task="detect"):
    template = await training_adapters.plugin_template(db, framework, base_model)
    if normalize_task(template["task"]) != task:
        raise HTTPException(
            status_code=422, detail="基础模型与自动策略任务或受控规格不匹配"
        )
    return {
        key: template[key]
        for key in (
            "framework",
            "framework_version",
            "image_digest",
            "base_model",
            "resource_id",
        )
    }


async def policy_template_current(db, policy):
    try:
        expected = policy.config["template"]
        task = getattr(policy, "task", "detect")
        pin = policy.config.get("initialization")
        if pin:
            if initialization.compatible_pin(
                pin,
                expected["framework"],
                expected["base_model"]["id"],
                expected_task=task,
            ) or getattr(
                policy, "initialization_model_id", None
            ) != initialization.model_id(pin):
                return False
        elif getattr(policy, "initialization_model_id", None) is not None:
            return False
        if policy.config.get("configuration"):
            schema = await training_adapters.plugin_parameter_schema(
                db, expected["framework"], expected["base_model"]["id"]
            )
            identity = policy.config["configuration"]["identity"]
            if (
                identity.get("schema_id") != schema["schema_id"]
                or identity.get("schema_digest") != schema["schema_digest"]
            ):
                return False
        return (
            await fixed_policy_template(
                db,
                expected["framework"],
                expected["base_model"]["id"],
                task,
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
    policy = await data.policy_claim(db, run)
    if policy is None:
        return False
    project = await get_workspace_project(
        db,
        actor_id=run.creator_id,
        project_id=run.project_id,
        populate_existing=True,
    )
    return bool(
        policy.enabled
        and policy.revision == policy.policy_revision
        and project.status == "in_progress"
        and await policy_template_current(db, policy)
        and run.config.get("initialization") == policy.config.get("initialization")
    )


async def study_claim_allowed(db, run):
    if run.study_id is None:
        return True
    study = await data.study_claim(db, run)
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
