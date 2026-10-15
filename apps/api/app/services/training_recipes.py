"""Resolve reuse against immutable sources without changing execution digests."""

import uuid
from fastapi import HTTPException
from sqlalchemy import select
from app.db.models.training_recipe import TrainingRecipe, TrainingRecipeRevision
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services import training_runs as runs
from app.services.learning_policies import fixed_template
from app.services.training_dataset_bridge import content_digest
from app.services import training_initialization as initialization


def snapshot(framework, base_model, configuration, initialization_pin=None):
    template = fixed_template(framework, base_model)
    value = {
        key: template[key]
        for key in ("framework", "framework_version", "image_digest", "base_model")
    } | {
        "configuration": runs.resolve_configuration(
            framework, base_model, configuration
        )
    }
    if initialization_pin:
        reasons = initialization.compatible_pin(
            initialization_pin, framework, base_model
        )
        if reasons:
            raise HTTPException(status_code=409, detail="；".join(reasons))
        value["initialization"] = initialization_pin
    return value


async def resolve_snapshot(db, project_id, body):
    pin = await initialization.resolve(
        db, project_id, body.initialization, body.framework, body.base_model
    )
    return snapshot(
        body.framework, body.base_model, body.configuration.model_dump(), pin
    )


def from_run(run):
    if not run.config.get("configuration"):
        raise HTTPException(
            status_code=409,
            detail="旧实验只记录六项参数；请在配置工作区明确迁移后另存配方，不能完整克隆未知默认值",
        )
    return {
        key: run.config[key]
        for key in (
            "framework",
            "framework_version",
            "image_digest",
            "base_model",
            "configuration",
        )
    } | (
        {"initialization": run.config["initialization"]}
        if run.config.get("initialization")
        else {}
    )


def compatibility(value):
    try:
        config = value["configuration"]
        current = snapshot(
            value["framework"],
            value["base_model"]["id"],
            {
                "schema_id": config["identity"]["schema_id"],
                "schema_digest": config["identity"]["schema_digest"],
                "values": config["user_values"],
            },
            value.get("initialization"),
        )
        if current != value:
            return (
                False,
                "训练器、原始基础权重或完整生效配置已变化；请明确迁移并保存新修订",
            )
        return True, None
    except (HTTPException, KeyError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return False, f"来源与当前受控目录不兼容：{detail}"


async def recipe_revision(db, project_id, recipe_id, revision_id=None, *, lock=False):
    statement = select(TrainingRecipe).where(
        TrainingRecipe.id == recipe_id, TrainingRecipe.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    recipe = await db.scalar(statement)
    if not recipe:
        raise HTTPException(status_code=404, detail="配方不存在或不属于当前项目")
    query = select(TrainingRecipeRevision).where(
        TrainingRecipeRevision.recipe_id == recipe.id
    )
    query = (
        query.where(TrainingRecipeRevision.id == revision_id)
        if revision_id
        else query.where(TrainingRecipeRevision.ordinal == recipe.head_revision)
    )
    revision = await db.scalar(query)
    if not revision:
        raise HTTPException(status_code=404, detail="配方修订不存在")
    return recipe, revision


async def source_run(db, project_id, run_id, attempt_id=None):
    run = await db.scalar(
        select(TrainingRun).where(
            TrainingRun.id == run_id, TrainingRun.project_id == project_id
        )
    )
    if not run:
        raise HTTPException(
            status_code=404,
            detail="来源实验已删除或不属于当前项目；请明确解除来源后继续",
        )
    if attempt_id and not await db.scalar(
        select(TrainingAttempt.id).where(
            TrainingAttempt.id == attempt_id, TrainingAttempt.run_id == run.id
        )
    ):
        raise HTTPException(status_code=404, detail="来源执行尝试不属于该实验")
    return run


async def clone_candidate(db, project_id, run_id, attempt_id=None):
    run = await source_run(db, project_id, run_id, attempt_id)
    value = from_run(run)
    compatible, reason = compatibility(value)
    if compatible and value.get("initialization"):
        try:
            await initialization.validate_accepted(
                db,
                project_id,
                value["initialization"],
                value["framework"],
                value["base_model"]["id"],
                full=False,
            )
        except HTTPException as exc:
            compatible, reason = False, str(exc.detail)
    return {
        "snapshot": value,
        "snapshot_digest": content_digest(value),
        "origin": {
            "kind": "clone",
            "run_id": str(run.id),
            "attempt_id": str(attempt_id) if attempt_id else None,
            "digest": run.config_digest,
        },
        "label": run.name,
        "compatible": compatible,
        "incompatibility": reason,
        "source_dataset_id": run.dataset_id,
    }


async def describe(db, recipe, revision):
    compatible, reason = compatibility(revision.snapshot)
    origin = revision.origin or {}
    source_available = True
    if origin.get("kind") == "clone":
        source_available = bool(
            await db.scalar(
                select(TrainingRun.id).where(
                    TrainingRun.id == uuid.UUID(origin["run_id"]),
                    TrainingRun.project_id == recipe.project_id,
                )
            )
        )
    if revision.snapshot.get("initialization"):
        pin = revision.snapshot["initialization"]
        try:
            if revision.initialization_model_id != initialization.model_id(pin):
                raise HTTPException(
                    status_code=409, detail="配方初始化引用与冻结身份不符"
                )
            await initialization.validate_accepted(
                db,
                recipe.project_id,
                pin,
                revision.snapshot["framework"],
                revision.snapshot["base_model"]["id"],
                full=False,
            )
        except (HTTPException, ValueError, KeyError, TypeError) as exc:
            compatible, source_available = False, False
            reason = (
                str(exc.detail)
                if isinstance(exc, HTTPException)
                else "配方初始化来源身份无效"
            )
    return {
        "id": recipe.id,
        "project_id": recipe.project_id,
        "head_revision": recipe.head_revision,
        "archived": recipe.archived,
        "revision": revision,
        "compatible": compatible,
        "incompatibility": reason,
        "source_available": source_available,
    }


async def origin_snapshot(
    db, project_id, origin, current, *, retain=None, strict=False
):
    if origin is None:
        return None
    current = {
        key: current[key]
        for key in (
            "framework",
            "framework_version",
            "image_digest",
            "base_model",
            "configuration",
        )
    } | (
        {"initialization": current["initialization"]}
        if current.get("initialization")
        else {}
    )
    reference = origin.model_dump(mode="json", exclude_none=True)
    if origin.kind == "recipe":
        recipe, revision = await recipe_revision(
            db, project_id, origin.recipe_id, origin.revision_id
        )
        same_pin = retain and all(retain.get(k) == v for k, v in reference.items())
        if recipe.archived and not same_pin:
            raise HTTPException(
                status_code=409, detail="配方已归档，不能新采用；既有固定配置继续保留"
            )
        if revision.snapshot_digest != origin.digest:
            raise HTTPException(status_code=409, detail="配方修订摘要不匹配")
        value, label = revision.snapshot, revision.name
        metadata = {"ordinal": revision.ordinal}
    else:
        run = await source_run(db, project_id, origin.run_id, origin.attempt_id)
        if run.config_digest != origin.digest:
            raise HTTPException(status_code=409, detail="来源实验摘要不匹配")
        value, label = from_run(run), run.name
        metadata = {"source_dataset_id": str(run.dataset_id)}
    compatible, reason = compatibility(value)
    if not compatible:
        raise HTTPException(status_code=409, detail=reason)
    if value.get("initialization") and not current.get("initialization"):
        raise HTTPException(
            status_code=409,
            detail="来源固定了项目初始化权重；请保留该来源，或解除配置来源后明确更改初始化",
        )
    if (
        any(
            current[key] != value[key]
            for key in ("framework", "framework_version", "image_digest", "base_model")
        )
        or current["configuration"]["identity"] != value["configuration"]["identity"]
    ):
        raise HTTPException(
            status_code=409, detail="来源基础权重或参数定义已变化，请明确解除来源或迁移"
        )
    modified = current != value
    if strict and modified:
        raise HTTPException(
            status_code=409,
            detail="策略必须采用配方完整配置；修改参数请另存修订或解除配方",
        )
    return (
        reference
        | metadata
        | {
            "label": label,
            "snapshot_digest": content_digest(value),
            "modified": modified,
            "base_model": value["base_model"],
            "identity": value["configuration"]["identity"],
        }
    )
