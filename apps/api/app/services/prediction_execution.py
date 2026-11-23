"""Snapshot integrity and task-scope checks shared by initial execution and retry."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from yoao_inference_contract.topology import to_record

from app.services.training_dataset_bridge import content_digest

MAX_SNAPSHOT_BYTES = 1024 * 1024

_CREATE = text(
    "SELECT yoao_inference_pipelines_v1.create_prediction_execution_snapshot("
    "CAST(:project_id AS uuid), CAST(:digest AS text), CAST(:payload AS jsonb))"
).bindparams(bindparam("payload", type_=JSONB))
_READ = text(
    "SELECT payload, digest FROM yoao_inference_pipelines_v1.read_prediction_execution_snapshot("
    "CAST(:snapshot_id AS uuid), CAST(:project_id AS uuid))"
)
_PRUNE = text(
    "SELECT yoao_inference_pipelines_v1.prune_prediction_execution_snapshots("
    "CAST(:cutoff AS timestamptz))"
)


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    id: UUID
    digest: str


async def create_snapshot(db, project_id, payload) -> SnapshotIdentity:
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=422, detail="管线执行配置超过 1 MiB 限制")
    digest = content_digest(payload)
    await db.flush()
    snapshot_id = await db.scalar(
        _CREATE, {"project_id": project_id, "digest": digest, "payload": payload}
    )
    return SnapshotIdentity(snapshot_id, digest)


async def load_snapshot(db, snapshot_id, project_id):
    if db.autoflush:
        await db.flush()
    snapshot = (
        await db.execute(
            _READ, {"snapshot_id": UUID(str(snapshot_id)), "project_id": project_id}
        )
    ).one_or_none()
    if snapshot is None or content_digest(snapshot.payload) != snapshot.digest:
        raise ValueError("原始管线执行快照缺失、版本不支持或摘要无效")
    return snapshot.payload, snapshot.digest


async def prune_retained_snapshots(db, cutoff: datetime) -> None:
    if db.autoflush:
        await db.flush()
    await db.execute(_PRUNE, {"cutoff": cutoff})


async def validate_task_scope(db, project, task, actor, scope, *, lock=False):
    from app.api.v1.projects import (
        _validate_preannotate_task_scope,
        _validate_workbench_task_scope,
    )
    from app.services.labeling_core_read_relations import (
        lock_batch_rows_for_core_read,
        lock_task_for_core_read,
    )

    if lock:
        original_batch_id = task.batch_id
        if task.batch_id:
            await lock_batch_rows_for_core_read(db, [task.batch_id])
        current = await lock_task_for_core_read(db, task.id, populate_existing=True)
        if current is None or current.batch_id != original_batch_id:
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
            if isinstance(item, Mapping) and isinstance(item.get("value"), str)
        ]
        for axis in axes
        if isinstance(axis, Mapping) and isinstance(axis.get("key"), str)
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
        if combos:
            selected = tuple(variants[key] for key in valid_values)
            if not any(
                selected == tuple(row)
                for row in combos
                if isinstance(row, (list, tuple))
            ):
                raise HTTPException(status_code=422, detail="管线模型变体组合无效")
        stage["model_variants"] = {key: variants[key] for key in valid_values}
    properties = (model.get("params") or {}).get("properties") or {}
    defaults = {
        key: to_record(prop["default"])
        for key, prop in properties.items()
        if isinstance(prop, Mapping) and "default" in prop
    }
    if stage.get("parent_stage") is None and project_thresholds:
        defaults.update(
            {key: value for key, value in project_thresholds.items() if key in defaults}
        )
    stage["params"] = {**defaults, **(stage.get("params") or {})}
    stage["identity_guarantee"] = "declared_configuration"


def build_stage_context(stage, payload):
    from yoao_inference.execution_context import build_predict_context

    context = build_predict_context(
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
