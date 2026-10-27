"""Background native reference results have their own live and durable qualification."""

from copy import deepcopy
from dataclasses import dataclass
import math
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.services import controlled_segmentation, trained_models
from app.services.content_snapshot import ContentError
from app.services.prediction_execution import execution_summary, load_snapshot
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    assert_raster_mask_write_enabled,
    collect_mask_geometries,
    prepare_mask_payload_for_write,
    validate_mask_geometry_for_task,
)
from app.services.training_dataset_bridge import content_digest

PROOF_KEY = "reference_native"
_ISSUER = object()
_VECTOR_TYPES = {
    "bbox",
    "rotated_bbox",
    "polygon",
    "multi_polygon",
    "polyline",
    "keypoint",
}
_STABLE_EXTRA = (
    "reference_input",
    "pipeline",
    "execution_snapshot",
    "trained_model",
    "inference_runtime",
    "segmentation",
)


def has_native_payload(write):
    return (
        (write.get("token_meta") or {}).get("mask_objects") is not None
        or bool(collect_mask_geometries(write["result"]))
        or any(shape.get("type") == "brushlabels" for shape in write["result"])
    )


def write_digest(
    task_id,
    project_id,
    ml_backend_id,
    result,
    *,
    score=None,
    model_version=None,
    inference_time_ms=None,
    token_meta=None,
    source="ml_backend",
    pipeline_extra=None,
    ml_backend_pool_id=None,
    model_provenance=None,
):
    return content_digest(
        {
            "task_id": str(task_id),
            "project_id": str(project_id),
            "ml_backend_id": str(ml_backend_id) if ml_backend_id else None,
            "ml_backend_pool_id": str(ml_backend_pool_id)
            if ml_backend_pool_id
            else None,
            "result": result,
            "score": score,
            "model_version": model_version,
            "inference_time_ms": inference_time_ms,
            "token_meta": token_meta,
            "source": source,
            "pipeline_extra": pipeline_extra,
            "model_provenance": model_provenance,
        }
    )


@dataclass(frozen=True, slots=True)
class _NativeReceipt:
    issuer: object
    owner: object
    anchor: object
    digest: str
    qualification: dict


def _root(payload):
    roots = [s for s in payload["stages"] if s.get("parent_stage") is None]
    if len(roots) != 1:
        raise ContentError("reference_native_model_invalid")
    root = roots[0]
    ref = root.get("frozen_model_ref") or {}
    if (
        ref.get("task") not in {"segment", "segmentation"}
        or root.get("model_id") != f"trained:{ref.get('model_version_id')}"
        or root.get("ml_backend_id") != ref.get("registry_id")
        or (root.get("input") or {}).get("mode", "full_image") != "full_image"
    ):
        raise ContentError("reference_native_model_invalid")
    return root, ref


def _lineage_validator(task_id, payload, extra, result_index, root):
    from aap_inference.execution_pipeline import resolve_input_mode

    stages = {s["stage"]: s for s in payload["stages"]}
    pipeline = extra["pipeline"]
    selections = {s["stage"]: s for s in pipeline["selections"]}
    lineage = pipeline.get("shape_lineage") or []
    records = {s["shape_id"]: s for s in lineage}
    if len(records) != len(lineage):
        raise ContentError("reference_native_lineage_invalid")
    root_ids = {
        str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{extra['execution_snapshot']['id']}:{task_id}:{root['stage']}:{result_index}:{index}",
            )
        )
        for index in range(controlled_segmentation.MAX_INSTANCES)
    }

    def check(origin, *, final=True, seen=None):
        seen = set() if seen is None else seen
        ident = origin["shape_id"]
        stage = stages.get(origin.get("stage"))
        if (
            ident in seen
            or stage is None
            or not selections[stage["stage"]].get("executed")
        ):
            raise ContentError("reference_native_lineage_invalid")
        seen.add(ident)
        if stage["stage"] == root["stage"]:
            if (
                ident not in root_ids
                or origin.get("parent_stage") is not None
                or origin.get("parent_shape_id") is not None
                or origin.get("transform") is not None
            ):
                raise ContentError("reference_native_lineage_invalid")
            return
        target = (stage.get("write") or {}).get("target", "attributes")
        if target not in (
            {"geometry"} if final else {"geometry", "intermediate"}
        ) or origin.get("parent_stage") != stage.get("parent_stage"):
            raise ContentError("reference_native_lineage_invalid")
        transform = origin.get("transform")
        if resolve_input_mode(stage) == "geometry":
            if transform is not None:
                raise ContentError("reference_native_lineage_invalid")
        elif (
            not isinstance(transform, dict)
            or set(transform) != {"ox", "oy", "sx", "sy"}
            or any(
                type(v) not in {int, float} or not math.isfinite(v)
                for v in transform.values()
            )
            or not (
                0 <= transform["ox"] < 1
                and 0 <= transform["oy"] < 1
                and 0 < transform["sx"] <= 1
                and 0 < transform["sy"] <= 1
                and transform["ox"] + transform["sx"] <= 1.000001
                and transform["oy"] + transform["sy"] <= 1.000001
            )
        ):
            raise ContentError("reference_native_lineage_invalid")
        parent_stage = stages.get(stage.get("parent_stage"))
        aliases = set()
        while (
            parent_stage
            and parent_stage["stage"] != root["stage"]
            and (parent_stage.get("write") or {}).get("target", "attributes")
            == "attributes"
        ):
            if parent_stage["stage"] in aliases:
                raise ContentError("reference_native_lineage_invalid")
            aliases.add(parent_stage["stage"])
            parent_stage = stages.get(parent_stage.get("parent_stage"))
        parent = records.get(origin.get("parent_shape_id"))
        if (
            not parent_stage
            or not parent
            or parent.get("stage") != parent_stage["stage"]
        ):
            raise ContentError("reference_native_lineage_invalid")
        check(parent, final=False, seen=seen)

    return check


def _validate_payload(task_id, dimensions, payload, extra, result, meta, result_index):
    """Check the final enriched representation; native classes were mapped at transport."""
    from app.services.prediction import to_internal_shape

    root, ref = _root(payload)
    trained_models.validate_result_runtime(meta, ref)
    if controlled_segmentation.validate_metadata(meta, ref) != dimensions:
        raise ContentError("image_size_mismatch")
    pipeline = extra.get("pipeline") or {}
    selections = pipeline.get("selections") or []
    stages = payload["stages"]
    if len(selections) != len(stages):
        raise ContentError("reference_native_lineage_invalid")
    for stage in stages:
        matches = [s for s in selections if s.get("stage") == stage["stage"]]
        if len(matches) != 1:
            raise ContentError("reference_native_lineage_invalid")
        selected = matches[0]
        if any(
            selected.get(k) != v
            for k, v in {
                "requested_registry_id": stage["ml_backend_id"],
                "model_id": stage.get("model_id"),
                "model_ref": stage.get("frozen_model_ref"),
                "class_metadata": stage.get("class_metadata"),
            }.items()
        ):
            raise ContentError("reference_native_lineage_invalid")
    selected = next(s for s in selections if s["stage"] == root["stage"])
    if (
        not selected.get("executed")
        or selected.get("executed_registry_id") != ref["registry_id"]
    ):
        raise ContentError("reference_native_model_invalid")
    lineage = pipeline.get("shape_lineage") or []
    validate_lineage = _lineage_validator(task_id, payload, extra, result_index, root)
    indices = set()
    for shape in result:
        geometry = to_internal_shape(deepcopy(shape)).get("geometry") or {}
        matches = [
            s for s in lineage if s.get("shape_id") == shape.get("pipeline_shape_id")
        ]
        if len(matches) != 1:
            raise ContentError("reference_native_lineage_invalid")
        origin = matches[0]
        if geometry.get("type") == "raster_mask":
            native, instance, score = (
                shape.get("native_class_index"),
                shape.get("instance_index"),
                shape.get("score"),
            )
            if (
                type(native) is not int
                or not 0 <= native < len(ref["classes"])
                or shape.get("class_name") != ref["bindings"][native]
                or shape.get("tool_unit_id") != "region"
                or type(instance) is not int
                or not 0 <= instance < controlled_segmentation.MAX_INSTANCES
                or instance in indices
                or type(score) not in {float, int}
                or not math.isfinite(score)
                or not 0 <= score <= 1
                or shape.get("confidence") != score
                or geometry["mask"]["size"] != dimensions
                or origin.get("stage") != root["stage"]
                or origin.get("parent_stage") is not None
                or origin.get("parent_shape_id") is not None
                or origin.get("transform") is not None
                or shape.get("pipeline_shape_id")
                != str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{extra['execution_snapshot']['id']}:{task_id}:{root['stage']}:{result_index}:{instance}",
                    )
                )
            ):
                raise ContentError("reference_native_shape_invalid")
            indices.add(instance)
        elif geometry.get("type") not in _VECTOR_TYPES:
            raise ContentError("reference_prediction_geometry_not_supported")
        else:
            if origin.get("stage") == root["stage"]:
                raise ContentError("reference_native_shape_invalid")
            validate_lineage(origin)
    return root, ref, selected


async def _lock_model(db, project_id, ref, selected):
    """No network I/O under these final model/routing locks."""
    from app.db.models.trained_model import (
        ModelDeployment,
        ModelRelease,
        TrainedModelVersion,
    )

    for model, key in [
        (TrainedModelVersion, "model_version_id"),
        (ModelDeployment, "deployment_id"),
        (ModelRelease, "release_id"),
    ]:
        if ref.get(key):
            row = await db.scalar(
                select(model)
                .where(model.id == uuid.UUID(ref[key]))
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise ContentError("reference_native_model_invalid")
    pool_id = uuid.UUID(selected["pool_id"]) if selected.get("pool_id") else None
    pool = await db.scalar(
        select(MLBackendServicePool)
        .where(MLBackendServicePool.id == pool_id)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    assoc = await db.scalar(
        select(ProjectMLBackendPool)
        .where(
            ProjectMLBackendPool.project_id == project_id,
            ProjectMLBackendPool.pool_id == pool_id,
        )
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    registry_id = uuid.UUID(ref["registry_id"])
    member = await db.scalar(
        select(MLBackendPoolMember)
        .where(
            MLBackendPoolMember.pool_id == pool_id,
            MLBackendPoolMember.registry_id == registry_id,
        )
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    backend = await db.scalar(
        select(MLBackendRegistry)
        .where(MLBackendRegistry.id == registry_id)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    if (
        not pool
        or not pool.enabled
        or not assoc
        or not assoc.enabled
        or not member
        or member.traffic_state != "active"
        or backend is None
    ):
        raise ContentError("reference_native_model_invalid")
    from app.services.trained_model_reference_contract import (
        FreezeModelReferenceError,
        ValidateFrozenModelReferenceRequest,
    )

    try:
        await trained_models.bind_frozen_model_reference(db).validate(
            ValidateFrozenModelReferenceRequest(project_id=project_id, reference=ref)
        )
    except FreezeModelReferenceError as exc:
        trained_models.raise_frozen_model_reference_error(exc)


async def qualify_results(owner, task, anchor, writes):
    """Preflight the complete group before any result can materialize an object."""
    if anchor is None or not any(has_native_payload(write) for write in writes):
        return [None] * len(writes)
    from app.services.prediction_references import validate_receipt

    evidence = await validate_receipt(owner.db, task.id, task.project_id, anchor)
    payload, digest = await load_snapshot(
        owner.db, owner.proof["snapshot_id"], task.project_id
    )
    if digest != owner.proof["snapshot_digest"]:
        raise ContentError("prediction_scope_changed")
    project = await owner.db.get(Project, task.project_id, populate_existing=True)
    assert_raster_mask_write_enabled(project)
    ref = owner.bindings[task.id]["reference"]
    qualified = []
    for index, write in enumerate(writes):
        if not has_native_payload(write):
            qualified.append(None)
            continue
        extra, meta = write.get("pipeline_extra") or {}, write.get("token_meta") or {}
        if write.get("source", "ml_backend") != "ml_backend" or extra.get(
            "execution_snapshot"
        ) != {
            "id": owner.proof["snapshot_id"],
            "digest": digest,
            "stages": execution_summary(payload["stages"]),
        }:
            raise ContentError("reference_native_model_invalid")
        _, model, selected = _validate_payload(
            task.id,
            [ref["height"], ref["width"]],
            payload,
            extra,
            write["result"],
            meta,
            index,
        )
        if (
            str(write["ml_backend_id"]) != model["registry_id"]
            or str(write.get("ml_backend_pool_id")) != selected.get("pool_id")
            or write.get("model_provenance") != model
            or write.get("model_version") != f"trained:{model['model_version_id']}"
        ):
            raise ContentError("reference_native_model_invalid")
        controlled_segmentation.validate_portable_masks(write["result"], meta)
        for geometry in collect_mask_geometries(write["result"]):
            await validate_mask_geometry_for_task(owner.db, task, geometry)
        await _lock_model(owner.db, task.project_id, model, selected)
        qualified.append(
            _NativeReceipt(
                _ISSUER,
                owner,
                anchor,
                write_digest(**write),
                {
                    "schema": 1,
                    "kind": "reference_background_native",
                    "execution": {
                        "id": owner.proof["snapshot_id"],
                        "digest": digest,
                        "payload": payload,
                    },
                    "result_index": index,
                    "reference_input": evidence,
                    "registry_id": model["registry_id"],
                    "pool_id": selected.get("pool_id"),
                },
            )
        )
    return qualified


async def validate_receipt(db, task_id, project_id, receipt, write):
    from app.services.prediction_references import validate_receipt as validate_anchor

    if (
        type(receipt) is not _NativeReceipt
        or receipt.issuer is not _ISSUER
        or receipt.owner.db is not db
        or receipt.digest != write_digest(**write)
    ):
        raise ContentError("background_native_reference_receipt_invalid")
    evidence = await validate_anchor(db, task_id, project_id, receipt.anchor)
    task = await db.get(Task, task_id, populate_existing=True)
    receipt.owner.resolve_url(task)
    await receipt.owner.authorize()
    return evidence, deepcopy(receipt.qualification)


def _candidate_digest(prediction, extra):
    # Registry deletion may legitimately SET NULL; preserve original identity in the proof.
    return content_digest(
        {
            "id": str(prediction.id),
            "created_at": str(prediction.created_at),
            "task_id": str(prediction.task_id),
            "project_id": str(prediction.project_id),
            "source": prediction.source,
            "tool_unit_id": prediction.tool_unit_id,
            "model_version": prediction.model_version,
            "score": prediction.score,
            "result": prediction.result,
            "extra": {key: extra.get(key) for key in _STABLE_EXTRA},
        }
    )


def persisted_qualification(prediction, extra, qualification):
    return {**qualification, "candidate_digest": _candidate_digest(prediction, extra)}


async def qualify_candidate(db, task, binding, prediction, metadata, *, final=False):
    try:
        await _qualify_candidate(db, task, binding, prediction, metadata, final=final)
    except (ValueError, KeyError, TypeError, StopIteration, IndexError) as exc:
        raise ContentError("reference_native_candidate_invalid") from exc


async def _qualify_candidate(db, task, binding, prediction, metadata, *, final=False):
    extra = metadata.extra or {}
    proof = extra.get(PROOF_KEY)
    if (
        not isinstance(proof, dict)
        or proof.get("schema") != 1
        or proof.get("kind") != "reference_background_native"
        or prediction.source != "ml_backend"
        or proof.get("candidate_digest") != _candidate_digest(prediction, extra)
        or proof.get("reference_input") != extra.get("reference_input")
        or (
            prediction.ml_backend_id is not None
            and str(prediction.ml_backend_id) != proof.get("registry_id")
        )
        or (
            prediction.ml_backend_pool_id is not None
            and str(prediction.ml_backend_pool_id) != proof.get("pool_id")
        )
    ):
        raise ContentError("reference_native_candidate_invalid")
    if extra.get("superseded_by_prediction_id"):
        raise ContentError("reference_native_candidate_superseded")
    execution = proof.get("execution") or {}
    payload = execution.get("payload") or {}
    if content_digest(payload) != execution.get("digest") or extra.get(
        "execution_snapshot"
    ) != {
        "id": execution.get("id"),
        "digest": execution.get("digest"),
        "stages": execution_summary(payload.get("stages") or []),
    }:
        raise ContentError("reference_native_candidate_invalid")
    reference = binding["reference"]
    _, model, selected = _validate_payload(
        task.id,
        [reference["height"], reference["width"]],
        payload,
        extra,
        prediction.result,
        {
            "trained_model": {
                key: (extra.get("trained_model") or {}).get(key)
                for key in ("model_version_id", "sha256", "classes_digest")
            },
            "segmentation": extra.get("segmentation"),
            **(
                {"inference_runtime": extra["inference_runtime"]}
                if "inference_runtime" in extra
                else {}
            ),
        },
        proof["result_index"],
    )
    if (
        extra.get("trained_model") != model
        or proof.get("registry_id") != model.get("registry_id")
        or proof.get("pool_id") != selected.get("pool_id")
        or prediction.model_version != f"trained:{model['model_version_id']}"
        or extra["reference_input"].get("execution_snapshot_id") != execution.get("id")
        or extra["reference_input"].get("execution_digest") != execution.get("digest")
    ):
        raise ContentError("reference_native_candidate_invalid")
    if final:
        try:
            await prepare_mask_payload_for_write(db, task, prediction.result)
        except RasterMaskContractError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from None
