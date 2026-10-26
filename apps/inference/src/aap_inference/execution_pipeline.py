"""Portable multistage prediction, context/ROI routing and result transforms."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from PIL import Image

from aap_inference_contract.execution import (
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionError,
    ModelExecutionPort,
    ModelOperationResult,
    ModelTaskResult,
    freeze_json,
    thaw_json,
)

from .roi import (
    collect_geometry_shapes,
    crop_inputs_from_boxes,
    geometry_prompts_from_boxes,
    merge_classify_attributes,
    remap_geometry_to_image,
)

logger = logging.getLogger(__name__)
_PAD_BY_DEPTH: dict[int, float] = {1: 0.05, 2: 0.08, 3: 0.12}


class CropPublishPort(Protocol):
    def publish(self, task_id: str, box_index: int, jpeg_bytes: bytes) -> str: ...


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

    async def call_stage(
        stage_index: int, inputs: Sequence[Mapping[str, Any]]
    ) -> ModelOperationResult:
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
        )
        return await model_ports[stage_index].execute(operation_request)

    root_call = await call_stage(0, ({"id": task_id, "file_path": source_url},))
    if len(stages) == 1:
        return TaskPipelineResult(
            results=root_call.results,
            extra=None,
            stage_stats=None,
            provenance=root_call.provenance,
        )

    needs_image = any(modes[index] == "crop" for index in range(1, len(stages)))
    if needs_image and source_image is None:
        raise ValueError("Multistage crop input requires the original image")
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
    stage_executions = [_result_provenance(root_call.provenance, root_stage)]
    output_results: list[ModelTaskResult] = []
    delivery = "presigned" if crop_publisher is not None else "data_uri"

    for root_result in root_call.results:
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
        stage_outputs: dict[int, list] = {root_stage: root_boxes}
        crop_cache_by_parent: dict[int | None, dict] = {}
        dropped: set[int] = set()
        new_shapes: list[dict] = []

        def publish_crop(index: int, jpeg_bytes: bytes) -> str:
            if crop_publisher is None:
                raise ValueError("Crop publisher is unavailable")
            return crop_publisher.publish(task_id, index, jpeg_bytes)

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
                    parent_boxes, parent_class_filter=parent_filter
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
                    shapes = collect_geometry_shapes(operation.results, parent_boxes)
                    stats[stage_number]["ok"] += len(shapes)
                    stage_outputs[stage_number] = shapes
                    if target == "geometry":
                        new_shapes.extend(shapes)
                except Exception as exc:
                    stats[stage_number]["failed"] += len(geo.prompts)
                    stage_outputs[stage_number] = []
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
            crop_batch = crop_inputs_from_boxes(
                source_image,
                parent_boxes,
                pad=pad,
                parent_class_filter=parent_filter,
                delivery=delivery,
                upload_fn=publish_crop if crop_publisher is not None else None,
                cache=crop_cache,
                min_crop_side_px=32,
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
                        produced.extend(remapped)
                    stats[stage_number]["ok"] += len(produced)
                    stage_outputs[stage_number] = produced
                    if target == "geometry":
                        new_shapes.extend(produced)
            except Exception as exc:
                stats[stage_number]["failed"] += len(targeted)
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

    extra: dict[str, Any] = {
        "pipeline": {
            "stage_count": len(stages),
            "max_depth": max(depths.values()) if depths else 1,
            "enriched_attr_keys": sorted(enriched_keys) or None,
            "stages": pipeline_topology(stages, depths),
            "stage_executions": stage_executions,
        }
    }
    if gpu_failures:
        extra["pipeline"]["gpu_arbiter_failures"] = gpu_failures
    if stage_failures:
        extra["pipeline"]["stage_failures"] = stage_failures
    immutable_stats = MappingProxyType(
        {stage: MappingProxyType(dict(values)) for stage, values in stats.items()}
    )
    return TaskPipelineResult(
        results=tuple(output_results),
        extra=freeze_json(extra),
        stage_stats=immutable_stats,
        provenance=root_call.provenance,
    )
