"""Portable multistage prediction, context/ROI routing and result transforms."""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol

from PIL import Image

from aap_assets_mask_content.codec import validate_reference_for_rle
from aap_assets_mask_content.contract import MASK_PREFIX
from aap_inference_contract.execution import (
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionDispatch,
    ModelExecutionError,
    ModelExecutionPort,
    ModelOperationResult,
    ModelTaskResult,
    freeze_json,
    thaw_json,
)

from .roi import (
    CropBatch,
    box_class_name,
    collect_geometry_shapes,
    crop_inputs_from_boxes,
    geometry_prompts_from_boxes,
    merge_classify_attributes,
    remap_geometry_to_image,
)

DispatchCallback = Callable[[int, ModelExecutionDispatch], Awaitable[None]]
ResultValidator = Callable[
    [int, ModelOperationResult, Mapping[str, Any] | None],
    Awaitable[Sequence[ModelTaskResult] | None],
]

logger = logging.getLogger(__name__)
_PAD_BY_DEPTH: dict[int, float] = {1: 0.05, 2: 0.08, 3: 0.12}


class CropPublishPort(Protocol):
    def publish(
        self, task_id: str, box_index: int, jpeg_bytes: bytes
    ) -> str | Awaitable[str]: ...


@dataclass(frozen=True, slots=True)
class TaskPipelineResult:
    results: tuple[ModelTaskResult, ...]
    extra: Mapping[str, Any] | None
    stage_stats: Mapping[int, Mapping[str, int]] | None
    provenance: ExecutionProvenance

    def __post_init__(self) -> None:
        results = tuple(self.results)
        if any(not isinstance(result, ModelTaskResult) for result in results):
            raise ValueError("Pipeline results must be immutable model results")
        object.__setattr__(self, "results", results)
        if self.extra is not None:
            extra = freeze_json(self.extra)
            if not isinstance(extra, Mapping):
                raise ValueError("Pipeline extra must be an object")
            object.__setattr__(self, "extra", extra)
        if self.stage_stats is not None:
            stats: dict[int, Mapping[str, int]] = {}
            for stage, values in self.stage_stats.items():
                if (
                    type(stage) is not int
                    or stage < 0
                    or not isinstance(values, Mapping)
                ):
                    raise ValueError("Invalid pipeline stage statistics")
                frozen_values = dict(values)
                if any(
                    type(key) is not str or type(value) is not int or value < 0
                    for key, value in frozen_values.items()
                ):
                    raise ValueError("Invalid pipeline stage statistics")
                stats[stage] = MappingProxyType(frozen_values)
            object.__setattr__(self, "stage_stats", MappingProxyType(stats))
        if not isinstance(self.provenance, ExecutionProvenance):
            raise ValueError("Pipeline result requires root provenance")


def resolve_input_mode(stage: Mapping[str, Any]) -> str:
    """Resolve the validated crop/geometry delivery mode for one stage."""
    explicit = (stage.get("input") or {}).get("mode")
    if explicit in {"crop", "geometry"}:
        return explicit
    target = (stage.get("write") or {}).get("target", "attributes")
    if target in {"geometry", "intermediate"}:
        return "geometry"
    return "crop"


def resolve_pad(stage: Mapping[str, Any], depth: int) -> float:
    pad = (stage.get("roi") or {}).get("pad")
    if pad is not None:
        return float(pad)
    return _PAD_BY_DEPTH.get(depth, 0.05)


def compute_stage_depths(stages: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    depth: dict[int, int] = {}
    for stage in sorted(stages, key=lambda item: item["stage"]):
        parent = stage.get("parent_stage")
        depth[stage["stage"]] = 1 if parent is None else depth.get(parent, 1) + 1
    return depth


def pipeline_topology(
    stages: Sequence[Mapping[str, Any]], depths: Mapping[int, int]
) -> list[dict[str, Any]]:
    topology = []
    for stage in stages:
        write = stage.get("write") or {}
        topology.append(
            {
                "stage": stage.get("stage"),
                "parent_stage": stage.get("parent_stage"),
                "depth": depths.get(stage.get("stage")),
                "label": stage.get("label"),
                "ml_backend_id": stage.get("ml_backend_id"),
                "model_id": stage.get("model_id"),
                "parent_class_filter": stage.get("parent_class_filter") or None,
                "write_keys": write.get("keys") or None,
                "write_target": write.get("target"),
                "target_stage": write.get("target_stage", "root"),
            }
        )
    return topology


def stage_totals_snapshot(
    totals: Mapping[int, Mapping[str, int]],
) -> list[dict[str, Any]]:
    return [{"stage": stage, **dict(totals[stage])} for stage in sorted(totals)]


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _result_provenance(value: ExecutionProvenance, stage: int) -> dict[str, Any]:
    return {
        "stage": stage,
        "requested_registry_id": str(value.requested_registry_id)
        if value.requested_registry_id
        else None,
        "requested_pool_id": str(value.requested_pool_id)
        if value.requested_pool_id
        else None,
        "registry_id": str(value.registry_id),
        "pool_id": str(value.pool_id) if value.pool_id else None,
        "instance_id": str(value.instance_id),
        "route_scope_id": str(value.route_scope_id),
        "attempt_id": str(value.attempt_id),
        "topology_generation": value.topology_generation,
        "member_id": str(value.member_id) if value.member_id else None,
        "endpoint_sha256": value.endpoint_sha256,
    }


def _failure_fact(exc: Exception, stage: int) -> dict[str, Any] | None:
    if not isinstance(exc, ModelExecutionError):
        return None
    failure = exc.failure
    return {
        "kind": failure.kind,
        "code": failure.code,
        "status_code": failure.status_code,
        "detail": thaw_json(failure.detail),
        "retry_after": failure.retry_after,
        "uncertain": failure.uncertain,
        "provenance": (
            _result_provenance(failure.provenance, stage)
            if failure.provenance is not None
            else None
        ),
        "observation": (
            failure.observation.to_wire() if failure.observation is not None else None
        ),
    }


class NativeMaskChildOutputUnsupported(ValueError):
    """A child stage cannot return pixel masks without a remap ownership contract."""


def lineage_shape_id(
    execution_snapshot_id: str | uuid.UUID,
    task_id: str,
    stage: int,
    result_index: int | str,
    index: int,
) -> str:
    """Return the canonical pipeline shape identity for one result element."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{execution_snapshot_id}:{task_id}:{stage}:{result_index}:{index}",
        )
    )


def _iter_mask_geometries(value: Any):
    if isinstance(value, Mapping):
        if value.get("type") == "raster_mask" and (
            "mask" in value or "geometry" not in value
        ):
            yield value
            return
        for child in value.values():
            yield from _iter_mask_geometries(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_mask_geometries(child)


def _mask_references(value: Any) -> list[str]:
    digests: list[str] = []
    if isinstance(value, Mapping):
        object_key = value.get("object_key")
        digest = value.get("sha256")
        if (
            isinstance(object_key, str)
            and object_key.startswith(f"{MASK_PREFIX}/")
            and isinstance(digest, str)
        ):
            digests.append(digest)
        for child in value.values():
            digests.extend(_mask_references(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            digests.extend(_mask_references(child))
    return digests


def _validate_original_masks(result: Any, meta: Mapping[str, Any]) -> None:
    """Validate the complete portable mask envelope before enrichment or pruning."""
    objects = meta.get("mask_objects")
    if not isinstance(objects, Mapping):
        raise ValueError("Invalid portable image mask envelope")
    referenced: set[str] = set()
    for geometry in _iter_mask_geometries(result):
        mask = geometry.get("mask")
        if isinstance(mask, Mapping):
            mask = dict(mask)
        digest = mask.get("sha256") if isinstance(mask, Mapping) else None
        rle = objects.get(digest) if isinstance(digest, str) else None
        if not isinstance(mask, Mapping) or not isinstance(rle, Mapping):
            raise ValueError("Invalid portable image mask envelope")
        validate_reference_for_rle(mask, rle)
        referenced.add(digest)
    if set(objects) != referenced:
        raise ValueError("Missing or unreferenced segmentation mask objects")


def _validate_child_results(results: Sequence[ModelTaskResult]) -> None:
    if any(
        (result.meta or {}).get("mask_objects") is not None
        or bool(list(_iter_mask_geometries(result.result)))
        or any(
            isinstance(shape, Mapping) and shape.get("type") == "brushlabels"
            for shape in result.result or ()
        )
        for result in results
    ):
        raise NativeMaskChildOutputUnsupported(
            "Native child output requires a qualified pixel remap contract"
        )


async def _publish_crop_inputs(
    image: Image.Image,
    boxes: Sequence[Any],
    *,
    task_id: str,
    publisher: CropPublishPort,
    cache: dict,
    pad: float,
    parent_class_filter: Sequence[str] | None,
    min_crop_side_px: int,
    mask_objects: Mapping[str, Any] | None,
) -> CropBatch:
    """Encode at most one crop at a time; cache only completed published inputs."""
    batch = CropBatch()
    allowed = set(parent_class_filter) if parent_class_filter else None
    for index, box in enumerate(boxes):
        if not isinstance(box, Mapping) or (
            allowed is not None and box_class_name(box) not in allowed
        ):
            continue
        key = (index, round(pad, 4))
        if key in cache:
            cached = cache[key]
            batch.inputs.append(cached["input"])
            batch.transforms[str(index)] = cached["transform"]
            continue
        prepared = crop_inputs_from_boxes(
            image,
            [box],
            pad=pad,
            min_crop_side_px=min_crop_side_px,
            mask_objects=mask_objects,
            delivery="deferred",
        )
        batch.skipped_geometry += prepared.skipped_geometry
        if not prepared.inputs:
            continue
        content = prepared.contents.pop("0")
        uploaded = publisher.publish(task_id, index, content)
        if inspect.isawaitable(uploaded):
            uploaded = await uploaded
        item = {"id": str(index), "file_path": uploaded}
        transform = prepared.transforms["0"]
        batch.inputs.append(item)
        batch.transforms[str(index)] = transform
        cache[key] = {"input": item, "transform": transform}
    return batch


async def execute_task_pipeline(
    task_id: str,
    stages: Sequence[Mapping[str, Any]],
    model_ports: Sequence[ModelExecutionPort],
    stage_contexts: Sequence[Mapping[str, Any] | None],
    *,
    source_url: str,
    project_id: uuid.UUID | None,
    owner_prefix: str,
    route_operation: str,
    stage_modes: Sequence[str] | None = None,
    source_image: Image.Image | None = None,
    crop_publisher: CropPublishPort | None = None,
    correlation: Mapping[str, Any] | None = None,
    input_provenance: Mapping[str, Any] | None = None,
    before_dispatch: DispatchCallback | None = None,
    validate_results: ResultValidator | None = None,
    max_response_bytes: int | None = None,
    execution_snapshot_id: str | uuid.UUID | None = None,
) -> TaskPipelineResult:
    """Execute one task's ordered root and multistage model operations.

    The caller supplies a verified source URL/image and crop-publication adapter. This
    function owns only task/model transformations; it has no ORM, storage, API or ledger
    dependency. Each stage uses the same frozen ``ModelExecutionPort`` contract.
    """
    if (
        not stages
        or len(stages) != len(model_ports)
        or len(stages) != len(stage_contexts)
    ):
        raise ValueError("Pipeline stage inputs are inconsistent")
    if type(task_id) is not str or not task_id:
        raise ValueError("Pipeline task identity is required")
    modes = tuple(stage_modes or ("crop",) * len(stages))
    if len(modes) != len(stages) or any(
        mode not in {"crop", "geometry"} for mode in modes
    ):
        raise ValueError("Pipeline stage modes are invalid")
    frozen_contexts = tuple(
        None if context is None else freeze_json(context) for context in stage_contexts
    )
    frozen_correlation = None if correlation is None else freeze_json(correlation)
    frozen_input_provenance = (
        None if input_provenance is None else freeze_json(input_provenance)
    )
    if execution_snapshot_id is not None and (
        not isinstance(execution_snapshot_id, (str, uuid.UUID))
        or not str(execution_snapshot_id)
    ):
        raise ValueError("Pipeline execution snapshot identity is invalid")
    snapshot_id = None if execution_snapshot_id is None else str(execution_snapshot_id)
    lineage: list[dict[str, Any]] = []
    attempted_stages: set[int] = set()
    stage_provenance: dict[int, ExecutionProvenance] = {}

    def tag_shapes(
        shapes: Sequence[Any],
        stage: Mapping[str, Any],
        parents: Sequence[Any] | None = None,
        transform: Mapping[str, Any] | None = None,
        result_index: int | str = 0,
    ) -> None:
        if snapshot_id is None:
            return
        for index, shape in enumerate(shapes):
            if not isinstance(shape, dict):
                continue
            shape_id = lineage_shape_id(
                snapshot_id, task_id, int(stage["stage"]), result_index, index
            )
            parent_index = shape.get("parent_box_idx")
            parent = (
                parents[parent_index]
                if parents is not None
                and type(parent_index) is int
                and 0 <= parent_index < len(parents)
                else None
            )
            shape["pipeline_shape_id"] = shape_id
            lineage.append(
                {
                    "shape_id": shape_id,
                    "stage": stage["stage"],
                    "parent_stage": stage.get("parent_stage"),
                    "parent_shape_id": (
                        parent.get("pipeline_shape_id")
                        if isinstance(parent, Mapping)
                        else None
                    ),
                    "parent_box_idx": parent_index,
                    "transform": None if transform is None else dict(transform),
                }
            )

    def selections() -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for index, stage in enumerate(stages):
            provenance = stage_provenance.get(index)
            selected.append(
                {
                    "stage": stage.get("stage"),
                    "requested_registry_id": stage.get("ml_backend_id"),
                    "pool_id": (
                        str(provenance.pool_id)
                        if provenance is not None and provenance.pool_id is not None
                        else None
                    ),
                    "executed_registry_id": (
                        str(provenance.instance_id) if provenance is not None else None
                    ),
                    "executed": index in attempted_stages,
                    "model_id": stage.get("model_id"),
                    "model_ref": stage.get("frozen_model_ref"),
                    "class_metadata": stage.get("class_metadata"),
                }
            )
        return selected

    async def call_stage(
        stage_index: int, inputs: Sequence[Mapping[str, Any]]
    ) -> ModelOperationResult:
        attempted_stages.add(stage_index)
        stage = stages[stage_index]
        stage_number = int(stage["stage"])
        requested_registry_id = _uuid_or_none(stage.get("ml_backend_id"))
        context = frozen_contexts[stage_index]
        controlled = bool(
            isinstance(context, Mapping) and context.get("frozen_model_ref")
        )
        operation_request = FrozenModelOperation(
            request_id=uuid.uuid4(),
            operation="batch_predict",
            task_inputs=tuple(inputs),
            context=context,
            project_id=project_id,
            requested_registry_id=requested_registry_id,
            requested_pool_id=None,
            endpoint_registry_id=None,
            required_instance_id=requested_registry_id if controlled else None,
            route_owner=f"{owner_prefix}:stage:{stage_number}",
            route_operation=route_operation,
            correlation={
                **(
                    thaw_json(frozen_correlation)
                    if frozen_correlation is not None
                    else {}
                ),
                "task_id": task_id,
                "stage": stage_number,
                "parent_stage": stage.get("parent_stage"),
            },
            input_provenance=frozen_input_provenance,
            max_response_bytes=max_response_bytes,
        )
        boundary = None
        if before_dispatch is not None:

            async def boundary(dispatch, _stage=stage_number):
                await before_dispatch(_stage, dispatch)

        operation = await model_ports[stage_index].execute(
            operation_request, before_dispatch=boundary
        )
        if validate_results is not None:
            validated = await validate_results(stage_number, operation, context)
            if validated is not None:
                operation = replace(operation, results=tuple(validated))
        return operation

    root_call = await call_stage(0, ({"id": task_id, "file_path": source_url},))
    stage_provenance[0] = root_call.provenance
    for root_result in root_call.results:
        if (root_result.meta or {}).get("mask_objects") is not None:
            _validate_original_masks(root_result.result, root_result.meta)
    if len(stages) == 1:
        if snapshot_id is None:
            return TaskPipelineResult(
                results=root_call.results,
                extra=None,
                stage_stats=None,
                provenance=root_call.provenance,
            )
        tagged_results: list[ModelTaskResult] = []
        for result_index, root_result in enumerate(root_call.results):
            shapes = thaw_json(root_result.result)
            if isinstance(shapes, list):
                tag_shapes(shapes, stages[0], result_index=result_index)
                tagged_results.append(replace(root_result, result=shapes))
            else:
                tagged_results.append(root_result)
        extra = {
            "pipeline": {
                "stage_count": len(stages),
                "stages": pipeline_topology(stages, compute_stage_depths(stages)),
                "shape_lineage": lineage,
                "selections": selections(),
            }
        }
        return TaskPipelineResult(
            results=tuple(tagged_results),
            extra=freeze_json(extra),
            stage_stats=None,
            provenance=root_call.provenance,
        )

    needs_image = any(modes[index] == "crop" for index in range(1, len(stages)))
    if needs_image and source_image is None:
        raise ValueError("Multistage crop input requires the original image")
    native_sizes = [
        geometry["mask"]["size"]
        for root_result in root_call.results
        if (root_result.meta or {}).get("mask_objects") is not None
        for geometry in _iter_mask_geometries(root_result.result)
    ]
    if source_image is not None and any(
        [source_image.height, source_image.width] != list(size) for size in native_sizes
    ):
        raise ValueError("Native root mask size differs from the source image")
    depths = compute_stage_depths(stages)
    root_stage = int(stages[0]["stage"])
    stats: dict[int, dict[str, int]] = {root_stage: {"detected": 0}}
    for stage in stages[1:]:
        stats[int(stage["stage"])] = {
            "targeted": 0,
            "ok": 0,
            "failed": 0,
            "skipped_geometry": 0,
        }
    enriched_keys: set[str] = set()
    gpu_failures: list[dict[str, Any]] = []
    stage_failures: list[dict[str, Any]] = []
    stage_errors: list[dict[str, Any]] = []
    stage_executions = [_result_provenance(root_call.provenance, root_stage)]
    output_results: list[ModelTaskResult] = []
    delivery = "presigned" if crop_publisher is not None else "data_uri"

    for result_index, root_result in enumerate(root_call.results):
        root_boxes = thaw_json(root_result.result)
        if not isinstance(root_boxes, list):
            root_boxes = []
        if not root_boxes:
            output_results.append(
                ModelTaskResult(
                    task_id=root_result.task_id,
                    result=root_boxes,
                    score=root_result.score,
                    model_version=root_result.model_version,
                    inference_time_ms=root_result.inference_time_ms,
                    cache_hit=root_result.cache_hit,
                    model_load_ms=root_result.model_load_ms,
                    meta=root_result.meta,
                    mask_input_next=root_result.mask_input_next,
                    diagnostic=root_result.diagnostic,
                    session_token=root_result.session_token,
                )
            )
            continue
        stats[root_stage]["detected"] += len(root_boxes)
        tag_shapes(root_boxes, stages[0], result_index=result_index)
        stage_outputs: dict[int, list] = {root_stage: root_boxes}
        crop_cache_by_parent: dict[int | None, dict] = {}
        dropped: set[int] = set()
        new_shapes: list[dict] = []

        # Native parents keep their exact RLE; verified portable objects travel
        # with the root prediction so downstream crops/prompts use exact bounds.
        mask_objects = (getattr(root_result, "meta", None) or {}).get("mask_objects")

        for stage_index in range(1, len(stages)):
            stage = stages[stage_index]
            stage_number = int(stage["stage"])
            parent_stage = stage.get("parent_stage")
            parent_boxes = stage_outputs.get(parent_stage)
            if parent_boxes is None:
                continue
            write = stage.get("write") or {}
            target = write.get("target", "attributes")
            write_keys = write.get("keys") or None
            label = stage.get("label")
            parent_filter = stage.get("parent_class_filter") or None

            if modes[stage_index] == "geometry":
                geo = geometry_prompts_from_boxes(
                    parent_boxes,
                    parent_class_filter=parent_filter,
                    mask_objects=mask_objects,
                )
                stats[stage_number]["skipped_geometry"] += geo.skipped_geometry
                if not geo.prompts:
                    stage_outputs[stage_number] = []
                    continue
                stats[stage_number]["targeted"] += len(geo.prompts)
                try:
                    operation = await call_stage(
                        stage_index,
                        (
                            {
                                "id": task_id,
                                "file_path": source_url,
                                "prompts": geo.prompts,
                            },
                        ),
                    )
                    stage_executions.append(
                        _result_provenance(operation.provenance, stage_number)
                    )
                    stage_provenance[stage_index] = operation.provenance
                    _validate_child_results(operation.results)
                    shapes = [
                        thaw_json(shape)
                        for shape in collect_geometry_shapes(
                            operation.results, parent_boxes
                        )
                    ]
                    tag_shapes(shapes, stage, parent_boxes, result_index=result_index)
                    stats[stage_number]["ok"] += len(shapes)
                    stage_outputs[stage_number] = shapes
                    if target == "geometry":
                        new_shapes.extend(shapes)
                except Exception as exc:
                    stats[stage_number]["failed"] += len(geo.prompts)
                    stage_outputs[stage_number] = []
                    stage_errors.append(
                        {
                            "stage": stage_number,
                            "error_type": type(exc).__name__,
                            "on_failure": stage.get("on_failure", "keep_parent"),
                        }
                    )
                    failure = _failure_fact(exc, stage_number)
                    if failure is not None:
                        stage_failures.append({"stage": stage_number, **failure})
                    from .gpu.contracts import gpu_arbiter_failure_record

                    gpu_failure = gpu_arbiter_failure_record(exc)
                    if gpu_failure is not None:
                        gpu_failures.append(
                            {
                                **gpu_failure,
                                "stage": stage_number,
                                "ml_backend_id": str(stage.get("ml_backend_id"))
                                if stage.get("ml_backend_id") is not None
                                else None,
                                "count": 1,
                            }
                        )
                    logger.warning(
                        "pipeline stage %s geometry operation failed: %s: %s",
                        stage_number,
                        type(exc).__name__,
                        exc,
                    )
                continue

            if source_image is None:
                stage_outputs[stage_number] = parent_boxes
                continue
            pad = resolve_pad(stage, depths.get(stage_number, 2))
            crop_cache = crop_cache_by_parent.setdefault(parent_stage, {})
            if crop_publisher is not None:
                crop_batch = await _publish_crop_inputs(
                    source_image,
                    parent_boxes,
                    task_id=task_id,
                    publisher=crop_publisher,
                    cache=crop_cache,
                    pad=pad,
                    parent_class_filter=parent_filter,
                    min_crop_side_px=32,
                    mask_objects=mask_objects,
                )
            else:
                crop_batch = crop_inputs_from_boxes(
                    source_image,
                    parent_boxes,
                    pad=pad,
                    parent_class_filter=parent_filter,
                    delivery=delivery,
                    cache=crop_cache,
                    min_crop_side_px=32,
                    mask_objects=mask_objects,
                )
            stats[stage_number]["skipped_geometry"] += crop_batch.skipped_geometry
            stage_outputs[stage_number] = parent_boxes
            if not crop_batch.inputs:
                if target != "attributes":
                    stage_outputs[stage_number] = []
                continue
            targeted = {int(item["id"]) for item in crop_batch.inputs}
            stats[stage_number]["targeted"] += len(targeted)
            try:
                operation = await call_stage(stage_index, crop_batch.inputs)
                stage_executions.append(
                    _result_provenance(operation.provenance, stage_number)
                )
                stage_provenance[stage_index] = operation.provenance
                _validate_child_results(operation.results)
                if target == "attributes":
                    merged = merge_classify_attributes(
                        parent_boxes,
                        operation.results,
                        write_keys=write_keys,
                        label=label,
                    )
                    stats[stage_number]["ok"] += merged
                    stats[stage_number]["failed"] += len(targeted) - merged
                    if write_keys:
                        prefix = f"{label}_" if label else ""
                        enriched_keys.update(f"{prefix}{key}" for key in write_keys)
                else:
                    produced: list[dict] = []
                    for crop_result in operation.results:
                        transform = crop_batch.transforms.get(str(crop_result.task_id))
                        if transform is None:
                            continue
                        remapped = remap_geometry_to_image(
                            thaw_json(crop_result.result), transform
                        )
                        try:
                            parent_index = int(crop_result.task_id)
                        except (TypeError, ValueError):
                            parent_index = None
                        for shape in remapped:
                            if parent_index is not None:
                                shape["parent_box_idx"] = parent_index
                        tag_shapes(
                            remapped,
                            stage,
                            parent_boxes,
                            transform,
                            result_index=f"{result_index}:{crop_result.task_id}",
                        )
                        produced.extend(remapped)
                    stats[stage_number]["ok"] += len(produced)
                    stage_outputs[stage_number] = produced
                    if target == "geometry":
                        new_shapes.extend(produced)
            except Exception as exc:
                stats[stage_number]["failed"] += len(targeted)
                stage_errors.append(
                    {
                        "stage": stage_number,
                        "error_type": type(exc).__name__,
                        "on_failure": stage.get("on_failure", "keep_parent"),
                    }
                )
                failure = _failure_fact(exc, stage_number)
                if failure is not None:
                    stage_failures.append({"stage": stage_number, **failure})
                from .gpu.contracts import gpu_arbiter_failure_record

                gpu_failure = gpu_arbiter_failure_record(exc)
                if gpu_failure is not None:
                    gpu_failures.append(
                        {
                            **gpu_failure,
                            "stage": stage_number,
                            "ml_backend_id": str(stage.get("ml_backend_id"))
                            if stage.get("ml_backend_id") is not None
                            else None,
                            "count": 1,
                        }
                    )
                if target != "attributes":
                    stage_outputs[stage_number] = []
                if stage.get("on_failure") == "drop_box" and parent_stage == root_stage:
                    dropped |= targeted
                logger.warning(
                    "pipeline stage %s crop operation failed (on_failure=%s): %s: %s",
                    stage_number,
                    stage.get("on_failure", "keep_parent"),
                    type(exc).__name__,
                    exc,
                )

        if dropped:
            root_boxes[:] = [
                shape for index, shape in enumerate(root_boxes) if index not in dropped
            ]
        if new_shapes:
            root_boxes.extend(new_shapes)
        output_meta = root_result.meta
        if (root_result.meta or {}).get("mask_objects") is not None:
            referenced = set(_mask_references(root_boxes))
            output_meta = {
                **(root_result.meta or {}),
                "mask_objects": {
                    digest: value
                    for digest, value in root_result.meta["mask_objects"].items()
                    if digest in referenced
                },
            }
        output_results.append(
            ModelTaskResult(
                task_id=root_result.task_id,
                result=root_boxes,
                score=root_result.score,
                model_version=root_result.model_version,
                inference_time_ms=root_result.inference_time_ms,
                cache_hit=root_result.cache_hit,
                model_load_ms=root_result.model_load_ms,
                meta=output_meta,
                mask_input_next=root_result.mask_input_next,
                diagnostic=root_result.diagnostic,
                session_token=root_result.session_token,
            )
        )

    extra: dict[str, Any] = {
        "pipeline": {
            "stage_count": len(stages),
            "max_depth": max(depths.values()) if depths else 1,
            "enriched_attr_keys": sorted(enriched_keys) or None,
            "stages": pipeline_topology(stages, depths),
            "stage_executions": stage_executions,
        }
    }
    if snapshot_id is not None:
        extra["pipeline"]["shape_lineage"] = lineage
        extra["pipeline"]["selections"] = selections()
    if gpu_failures:
        extra["pipeline"]["gpu_arbiter_failures"] = gpu_failures
    if stage_failures:
        extra["pipeline"]["stage_failures"] = stage_failures
    if stage_errors:
        extra["pipeline"]["stage_errors"] = stage_errors
    if stage_errors or any(values.get("failed", 0) for values in stats.values()):
        extra["pipeline"]["partial"] = True
    immutable_stats = MappingProxyType(
        {stage: MappingProxyType(dict(values)) for stage, values in stats.items()}
    )
    return TaskPipelineResult(
        results=tuple(output_results),
        extra=freeze_json(extra),
        stage_stats=immutable_stats,
        provenance=root_call.provenance,
    )
