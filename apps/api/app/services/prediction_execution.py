"""Snapshot integrity and task-scope checks shared by initial execution and retry."""

from copy import deepcopy
import json
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.services.training_dataset_bridge import content_digest

MAX_SNAPSHOT_BYTES = 1024 * 1024


async def create_snapshot(db, project_id, payload):
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=422, detail="管线执行配置超过 1 MiB 限制")
    snapshot = PredictionExecutionSnapshot(
        project_id=project_id,
        schema_version=1,
        digest=content_digest(payload),
        payload=deepcopy(payload),
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def load_snapshot(db, snapshot_id, project_id):
    snapshot = await db.get(
        PredictionExecutionSnapshot, UUID(str(snapshot_id)), populate_existing=True
    )
    if (
        not snapshot
        or snapshot.project_id != project_id
        or snapshot.schema_version != 1
        or content_digest(snapshot.payload) != snapshot.digest
    ):
        raise ValueError("原始管线执行快照缺失、版本不支持或摘要无效")
    return deepcopy(snapshot.payload), snapshot.digest


async def validate_task_scope(db, project, task, actor, scope, *, lock=False):
    from app.api.v1.projects import (
        _validate_preannotate_task_scope,
        _validate_workbench_task_scope,
    )
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch

    if lock:
        original_batch_id = task.batch_id
        # Assignment/batch edits lock these owners; hold them until prediction commit.
        if task.batch_id:
            await db.execute(
                select(TaskBatch).where(TaskBatch.id == task.batch_id).with_for_update()
            )
        await db.execute(select(Task).where(Task.id == task.id).with_for_update())
        await db.refresh(task)
        if task.batch_id != original_batch_id:
            raise HTTPException(
                status_code=409, detail="推理期间任务批次已变更，请重新提交"
            )
    if scope == "workbench":
        await _validate_workbench_task_scope(
            db, project=project, task_ids=[task.id], actor=actor
        )
    else:
        await _validate_preannotate_task_scope(
            db, project=project, task_ids=[task.id], batch_id=None
        )


def freeze_builtin_defaults(backend, model, stage, *, project_thresholds=None):
    """Resolve advertised defaults once; legacy undeclared families stay explicit."""
    model = model or {}
    axes = model.get("supported_variants") or []
    variants = {
        **((backend.health_meta or {}).get("capabilities") or {}).get(
            "default_variants", {}
        ),
        **(model.get("default_variants") or {}),
        **(stage.get("model_variants") or {}),
    }
    valid_values = {
        axis["key"]: [
            item["value"]
            for item in axis.get("variants", [])
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        ]
        for axis in axes
        if isinstance(axis, dict) and isinstance(axis.get("key"), str)
    }
    combos = model.get("variant_combinations") or []
    if valid_values:
        if any(
            key not in variants or variants[key] not in values
            for key, values in valid_values.items()
        ):
            raise HTTPException(
                status_code=422,
                detail="管线模型缺少可冻结的完整变体，请明确选择模型规格",
            )
        if combos and [variants[key] for key in valid_values] not in combos:
            raise HTTPException(status_code=422, detail="管线模型变体组合无效")
        stage["model_variants"] = {key: variants[key] for key in valid_values}
    properties = (model.get("params") or {}).get("properties") or {}
    defaults = {
        key: prop["default"]
        for key, prop in properties.items()
        if isinstance(prop, dict) and "default" in prop
    }
    if stage.get("parent_stage") is None and project_thresholds:
        defaults.update(
            {key: value for key, value in project_thresholds.items() if key in defaults}
        )
    stage["params"] = {**defaults, **(stage.get("params") or {})}
    # The read model never invents a physical checkpoint identity for legacy declarations.
    stage["identity_guarantee"] = "declared_configuration"


def build_stage_context(stage, payload):
    from app.workers.tasks import _build_predict_context

    context = _build_predict_context(
        prompt=payload["prompt"] if stage.get("parent_stage") is None else None,
        output_mode=payload["output_mode"],
        params=stage.get("params"),
        model_id=stage.get("model_id"),
        task=stage.get("task"),
        prompt_type=stage.get("prompt_type"),
        model_variants=stage.get("model_variants"),
        class_filter=stage.get("class_filter"),
        frozen_model_ref=stage.get("frozen_model_ref"),
        box_threshold=payload["box_threshold"],
        text_threshold=payload["text_threshold"],
    )
    metadata = stage.get("class_metadata") or {}
    if (
        context is not None
        and metadata.get("verification_status") == "verified"
        and metadata.get("loader_version")
    ):
        context["builtin_model_ref"] = {
            key: metadata[key]
            for key in ("artifact_sha256", "classes_digest", "loader_version")
        }
    return context


def execution_summary(stages):
    """Self-contained identity/provenance without repeating unbounded parameter payloads."""
    return [
        {
            key: stage.get(key)
            for key in (
                "stage",
                "parent_stage",
                "ml_backend_id",
                "model_id",
                "model_variants",
                "frozen_model_ref",
                "class_metadata",
                "identity_guarantee",
                "class_filter",
                "parent_class_filter",
                "roi",
                "input",
                "write",
                "on_failure",
            )
        }
        for stage in stages
    ]
