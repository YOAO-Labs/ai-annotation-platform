"""Authorized one-hop projections; no storage, executor or serving mutations."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, func, select, tuple_

from app.services import learning_models_data as models
from app.services.evaluation_data import evaluation_lineage_row
from app.services.learning_selection_data import (
    recipe_lineage_row,
    recipe_revision_lineage_row,
)
from app.services.training_dataset_data import dataset_lineage_row
from app.services.training_run_data import attempt_lineage_row, run_lineage_row
from app.schemas.model_lineage import LineageNode
from app.services.training_dataset_bridge import content_digest
from app.services.labeling_core_read_relations import (
    alias_core_row,
    prediction_meta_row,
    prediction_row,
)


MODELS = {
    "dataset": dataset_lineage_row,
    "run": run_lineage_row,
    "attempt": attempt_lineage_row,
    "model": models.model_lineage_row,
    "deployment": models.deployment_lineage_row,
    "release": models.release_lineage_row,
    "recipe": recipe_lineage_row,
    "recipe_revision": recipe_revision_lineage_row,
    "evaluation": evaluation_lineage_row,
}
Dataset = dataset_lineage_row.c
Run = run_lineage_row.c
Attempt = attempt_lineage_row.c
Model = models.model_lineage_row.c
Deployment = models.deployment_lineage_row.c
Release = models.release_lineage_row.c
Revision = recipe_revision_lineage_row.c
Evaluation = evaluation_lineage_row.c
GAP = "来源缺失、已删除或归属不一致；未推断替代记录"


def owned_query(kind, project_id):
    relation = MODELS[kind]
    return select(relation).where(relation.c.project_id == project_id)


async def read_node(db, project_id, kind, node_id):
    if db.autoflush:
        await db.flush()
    try:
        node_id = uuid.UUID(str(node_id))
    except (ValueError, TypeError, AttributeError):
        return None
    return (
        (
            await db.execute(
                owned_query(kind, project_id).where(MODELS[kind].c.id == node_id)
            )
        )
        .mappings()
        .first()
    )


def render(kind, row):
    facts = {}
    for key in (
        "run_id",
        "deletion_requested_at",
        "ordinal",
        "generation",
        "snapshot_digest",
        "config_digest",
        "spec_digest",
        "framework",
        "weights_sha256",
        "binding_digest",
        "dataset_digest",
        "validation_digest",
        "checked_at",
        "archived",
        "initialization_model_id",
    ):
        if row.get(key) is not None:
            facts[key] = str(row[key])[:256]
    for key, value in row.items():
        if (
            key.startswith("frozen_") or key.startswith("initialization_")
        ) and value is not None:
            facts[key] = str(value)[:256]
    name = row.get("name") or {
        "attempt": f"执行尝试 {row.get('ordinal', '')}",
        "deployment": "部署检查（最近一次）",
        "release": f"发布代次 {row.get('generation', '')}",
        "recipe": "项目配方",
    }.get(kind, kind)
    return LineageNode(
        kind=kind,
        id=row["id"],
        name=name[:120],
        status=row.get("status"),
        created_at=row["created_at"],
        facts=facts,
    )


def relations(kind, row):
    node_id = row["id"]
    specs = {
        "dataset": {
            "child_datasets": ("dataset", Dataset.parent_id == node_id),
            "runs": ("run", Run.dataset_id == node_id),
            "evaluations": ("evaluation", Evaluation.dataset_id == node_id),
        },
        "run": {
            "attempts": ("attempt", Attempt.run_id == node_id),
            "models": ("model", Model.run_id == node_id),
        },
        "attempt": {
            "resumed_attempts": (
                "attempt",
                and_(
                    Attempt.resume_from_id == node_id,
                    Attempt.run_id == row.get("run_id"),
                    Attempt.ordinal > row.get("ordinal", 0),
                ),
            ),
            "models": (
                "model",
                and_(Model.attempt_id == node_id, Model.run_id == row.get("run_id")),
            ),
        },
        "model": {
            "deployments": ("deployment", Deployment.model_version_id == node_id),
            "releases": ("release", Release.model_version_id == node_id),
            "evaluations": ("evaluation", Evaluation.model_version_id == node_id),
            "initialized_runs": ("run", Run.initialization_model_id == node_id),
            "initialization_recipes": (
                "recipe_revision",
                Revision.initialization_model_id == node_id,
            ),
        },
        "deployment": {
            "releases": (
                "release",
                and_(
                    Release.deployment_id == node_id,
                    Release.model_version_id == row.get("model_version_id"),
                ),
            )
        },
        "release": {
            "subsequent_releases": (
                "release",
                and_(
                    Release.previous_release_id == node_id,
                    Release.generation > row.get("generation", 0),
                ),
            ),
            "rollback_uses": (
                "release",
                and_(
                    Release.rollback_from_id == node_id,
                    Release.model_version_id == row.get("model_version_id"),
                    Release.deployment_id == row.get("deployment_id"),
                    Release.generation > row.get("generation", 0),
                ),
            ),
        },
        "recipe": {"revisions": ("recipe_revision", Revision.recipe_id == node_id)},
    }
    return specs.get(kind, {})


async def input_links(db, project_id, kind, row):
    links = []

    async def edge(
        relation, target_kind, value, valid=True, predicate=None, required=True
    ):
        if value is None and not required:
            return
        target = (
            await read_node(db, project_id, target_kind, value)
            if value is not None and valid
            else None
        )
        if target and (
            (kind == target_kind and row["id"] == target["id"])
            or (predicate and not predicate(target))
        ):
            target = None
        links.append(
            {
                "relation": relation,
                "target": render(target_kind, target) if target else None,
                "gap": None if target else GAP,
            }
        )

    if kind == "dataset":
        await edge("dataset_parent", "dataset", row["parent_id"], required=False)
    elif kind == "run":
        await edge("training_data", "dataset", row["dataset_id"])
        await edge(
            "current_attempt",
            "attempt",
            row["current_attempt_id"],
            predicate=lambda t: t["run_id"] == row["id"],
        )
    elif kind == "attempt":
        await edge("experiment", "run", row["run_id"])
        await edge(
            "checkpoint_resume",
            "attempt",
            row["resume_from_id"],
            predicate=lambda t: (
                t["run_id"] == row["run_id"] and t["ordinal"] < row["ordinal"]
            ),
            required=False,
        )
    elif kind == "model":
        run = await read_node(db, project_id, "run", row["run_id"])
        attempt = await read_node(db, project_id, "attempt", row["attempt_id"])
        valid = bool(run and attempt and attempt["run_id"] == run["id"])
        await edge("experiment", "run", row["run_id"], valid)
        await edge("exact_attempt", "attempt", row["attempt_id"], valid)
        await edge("training_data", "dataset", run["dataset_id"] if valid else None)
        frozen_ok = valid and all(
            row.get(f"frozen_{k}") == str(value)
            for k, value in (
                ("run_id", row["run_id"]),
                ("attempt_id", row["attempt_id"]),
                ("dataset_id", run["dataset_id"]),
            )
        )
        if not frozen_ok:
            links.append(
                {
                    "relation": "frozen_provenance",
                    "gap": "冻结来源不完整或与权威记录不符；摘要仅作历史证据",
                    "target": None,
                }
            )
    elif kind == "deployment":
        await edge("candidate", "model", row["model_version_id"])
    elif kind == "release":
        candidate = row["model_version_id"]
        await edge("published_candidate", "model", candidate, required=False)
        await edge(
            "published_deployment",
            "deployment",
            row["deployment_id"],
            predicate=lambda t: t["model_version_id"] == candidate,
            required=False,
        )
        await edge(
            "previous_selection",
            "release",
            row["previous_release_id"],
            predicate=lambda t: t["generation"] < row["generation"],
            required=False,
        )
        await edge(
            "rollback_target",
            "release",
            row["rollback_from_id"],
            predicate=lambda t: (
                t["generation"] < row["generation"]
                and t["model_version_id"] == candidate
                and t["deployment_id"] == row["deployment_id"]
            ),
            required=False,
        )
        if candidate and (
            row.get("frozen_model_version_id") != str(candidate)
            or row.get("frozen_deployment_id") != str(row["deployment_id"])
        ):
            links.append(
                {
                    "relation": "frozen_release",
                    "target": None,
                    "gap": "历史发布缺少一致的冻结身份",
                }
            )
    elif kind == "recipe_revision":
        await edge("recipe", "recipe", row["recipe_id"])
    elif kind == "evaluation":
        await edge("evaluation_candidate", "model", row["model_version_id"])
        await edge(
            "evaluation_deployment",
            "deployment",
            row["deployment_id"],
            predicate=lambda t: t["model_version_id"] == row["model_version_id"],
        )
        await edge("evaluation_data", "dataset", row["dataset_id"])
        if any(
            row.get(f"frozen_{key}") != str(value)
            for key, value in (
                ("model_version_id", row["model_version_id"]),
                ("deployment_id", row["deployment_id"]),
                ("project_id", project_id),
            )
        ):
            links.append(
                {
                    "relation": "frozen_evaluation",
                    "target": None,
                    "gap": "评估缺少一致的冻结身份；打开报告查看可用性原因",
                }
            )
    if kind in {"run", "recipe_revision"}:
        if row.get("initialization_model_id") or row.get("initialization_kind"):
            await edge(
                "weights_initialization",
                "model",
                row.get("initialization_model_id"),
                valid=row.get("initialization_kind") == "trained_model",
                predicate=lambda target: (
                    str(target["id"]) == row.get("initialization_model_version_id")
                    and str(project_id) == row.get("initialization_project_id")
                    and str(target["run_id"]) == row.get("initialization_run_id")
                    and str(target["attempt_id"])
                    == row.get("initialization_attempt_id")
                ),
            )
        if row.get("origin_kind") == "clone":
            await edge(
                "configuration_clone",
                "run",
                row.get("origin_run_id"),
                predicate=lambda t: row.get("origin_digest") == t["config_digest"],
            )
            if row.get("origin_attempt_id"):
                await edge(
                    "clone_context_attempt",
                    "attempt",
                    row["origin_attempt_id"],
                    predicate=lambda t: str(t["run_id"]) == row.get("origin_run_id"),
                )
        elif row.get("origin_kind") == "recipe":
            await edge(
                "recipe_configuration",
                "recipe_revision",
                row.get("origin_revision_id"),
                predicate=lambda t: (
                    str(t["recipe_id"]) == row.get("origin_recipe_id")
                    and t["snapshot_digest"] == row.get("origin_digest")
                ),
            )
        elif row.get("origin_kind"):
            links.append(
                {
                    "relation": "configuration_origin",
                    "target": None,
                    "gap": "来源操作类型尚不支持解析",
                }
            )
    return links


def encode_cursor(binding, row):
    payload = [*binding, row["created_at"].isoformat(), str(row["id"])]
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def decode_cursor(cursor, binding):
    try:
        if len(cursor) > 1000:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, list) or value[:-2] != list(binding):
            raise ValueError
        timestamp = datetime.fromisoformat(value[-2])
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, uuid.UUID(value[-1])
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise HTTPException(
            status_code=422, detail="谱系分页游标无效或不属于此关系"
        ) from exc


async def prediction_usage(db, project_id, row):
    if any(
        not isinstance(row.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", row[key])
        for key in ("weights_sha256", "binding_digest")
    ):
        return {
            "retained_count": None,
            "truncated": False,
            "unavailable_reason": "candidate_identity_incomplete",
        }
    version = await models.get_version(db, project_id, row["id"])
    classes = version.classes if version else None
    metadata = alias_core_row(prediction_meta_row, name="lineage_prediction_meta")
    prediction = alias_core_row(prediction_row, name="lineage_prediction")
    ref = metadata.extra.op("->")("trained_model")

    def field(key):
        return ref.op("->>")(key)

    q = (
        select(metadata.id)
        .join(
            prediction,
            and_(
                prediction.id == metadata.prediction_id,
                prediction.created_at == metadata.prediction_created_at,
            ),
        )
        .where(
            field("model_version_id") == str(row["id"]),
            field("project_id") == str(project_id),
            field("sha256") == row["weights_sha256"],
            field("binding_digest") == row["binding_digest"],
            field("classes_digest") == content_digest(classes),
            func.length(field("reference_digest")) == 64,
            prediction.project_id == project_id,
        )
        .limit(1001)
    )
    values = (await db.scalars(q)).all()
    return {"retained_count": min(len(values), 1000), "truncated": len(values) > 1000}


async def lineage(db, project_id, kind, node_id, relation=None, cursor=None, limit=20):
    row = await read_node(db, project_id, kind, node_id)
    if not row:
        raise HTTPException(status_code=404, detail="谱系节点不存在")
    specs = relations(kind, row)
    sections = list(specs) + (["predictions"] if kind == "model" else [])
    result = {
        "node": render(kind, row),
        "inputs": await input_links(db, project_id, kind, row),
        "sections": sections,
        "relation": relation,
        "items": [],
    }
    if relation is None:
        if cursor:
            raise HTTPException(status_code=422, detail="分页游标需要指定关系")
        return result
    if relation not in sections:
        raise HTTPException(status_code=422, detail="该节点不支持所选关系")
    if relation == "predictions":
        if cursor:
            raise HTTPException(status_code=422, detail="预测计数不支持分页游标")
        result["usage"] = await prediction_usage(db, project_id, row)
        return result
    target_kind, condition = specs[relation]
    m = MODELS[target_kind].c
    q = owned_query(target_kind, project_id).where(condition)
    binding = (str(project_id), kind, str(node_id), relation)
    if cursor:
        timestamp, last_id = decode_cursor(cursor, binding)
        q = q.where(tuple_(m.created_at, m.id) < tuple_(timestamp, last_id))
    rows = (
        (
            await db.execute(
                q.order_by(m.created_at.desc(), m.id.desc()).limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )
    result["items"] = [render(target_kind, item) for item in rows[:limit]]
    if len(rows) > limit:
        result["next_cursor"] = encode_cursor(binding, rows[limit - 1])
    return result


async def append_decision(db, project_id, model_id, user, body):
    digest = content_digest(body.model_dump(mode="json"))
    item = await models.append_decision(
        db,
        project_id,
        model_id,
        actor_id=user.id,
        actor_label=user.name,
        request_id=body.request_id,
        request_digest=digest,
        disposition=body.disposition,
        note=body.note,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="模型版本不存在")
    if item.request_digest != digest:
        raise HTTPException(status_code=409, detail="决策请求标识已用于其他内容")
    return item


async def decisions(db, project_id, model_id, before=None, limit=20):
    if not await read_node(db, project_id, "model", model_id):
        raise HTTPException(status_code=404, detail="模型版本不存在")
    rows = await models.list_decisions(db, project_id, model_id, before, limit + 1)
    return {
        "items": rows[:limit],
        "next_before": rows[limit - 1].sequence if len(rows) > limit else None,
    }


async def model_metadata(db, project_id, model_ids):
    rows = await models.candidate_metadata(db, [project_id], model_ids)
    return {
        row.id: {
            "disposition": row.disposition,
            "decision_sequence": row.sequence or 0,
            "archived": row.disposition == "archived",
            "is_current_release": bool(row.is_current),
        }
        for row in rows
    }
