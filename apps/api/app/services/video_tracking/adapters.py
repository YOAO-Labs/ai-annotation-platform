from __future__ import annotations

import uuid
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from aap_inference.tracker import (
    TrackerFrameResult as _TrackerFrameResult,
    TrackerSessionState as _TrackerSessionState,
    bbox_from_geometry,
    build_tracker_context,
    build_tracker_operation,
    continue_tracker_session,
    tracker_frame_results,
)
from aap_inference_contract.execution import ExecutionProvenance
from app.config import settings
from app.services.inference_model_execution import model_execution_port


@dataclass(frozen=True)
class TrackerContext:
    job_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    dataset_item_id: uuid.UUID
    annotation_id: uuid.UUID
    from_frame: int
    to_frame: int
    direction: str
    prompt: dict
    source_geometry: dict
    task_data: dict
    db: Any | None = None
    bound_route: Any | None = None
    requested_registry_id: uuid.UUID | None = None
    required_instance_id: uuid.UUID | None = None
    sam_variant: str | None = None
    text: str | None = None
    exemplars: list[dict] | None = None
    output_geometry: str = "bbox"
    seeds: list[dict] | None = None
    session: _TrackerSessionState | None = None
    model_id: str | None = None
    outside_threshold: float | None = None
    correlation: dict | None = None


@dataclass(frozen=True)
class TrackerPropagationResult:
    frames: tuple[_TrackerFrameResult, ...]
    session: _TrackerSessionState | None
    response_digest: str
    provenance: ExecutionProvenance | None


def _response_digest(body) -> str:
    digest, size = hashlib.sha256(), 0
    for chunk in json.JSONEncoder(
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).iterencode(body):
        content = chunk.encode("utf-8")
        size += len(content)
        if size > 64 * 1024 * 1024:
            raise ValueError("tracker_response_too_large")
        digest.update(content)
    return digest.hexdigest()


class TrackerAdapter(Protocol):
    model_key: str

    async def propagate(self, ctx: TrackerContext) -> TrackerPropagationResult: ...


class MockBboxTrackerAdapter:
    model_key = "mock_bbox"

    async def propagate(self, ctx: TrackerContext) -> TrackerPropagationResult:
        prompt_geometry = ctx.prompt.get("geometry")
        bbox = bbox_from_geometry(
            prompt_geometry
            if isinstance(prompt_geometry, dict)
            else ctx.source_geometry
        )
        frames = range(ctx.from_frame, ctx.to_frame + 1)
        if ctx.direction == "backward":
            frames = range(ctx.to_frame, ctx.from_frame - 1, -1)

        rows = []
        frame_results = []
        for frame_index in frames:
            geometry = {"type": "bbox", **bbox}
            rows.append(
                {
                    "frame_index": frame_index,
                    "geometry": geometry,
                    "confidence": 1.0,
                    "outside": False,
                }
            )
            frame_results.append(
                _TrackerFrameResult(
                    frame_index=frame_index,
                    geometry=geometry,
                    confidence=1.0,
                    outside=False,
                )
            )
        return TrackerPropagationResult(
            frames=tuple(frame_results),
            session=ctx.session,
            response_digest=_response_digest(rows),
            provenance=None,
        )


class MLBackendVideoTrackerAdapter:
    def __init__(self, model_key: str) -> None:
        self.model_key = model_key

    async def propagate(self, ctx: TrackerContext) -> TrackerPropagationResult:
        request_id = uuid.uuid4()
        if (
            ctx.db is None
            or ctx.bound_route is None
            or ctx.requested_registry_id is None
            or ctx.required_instance_id is None
        ):
            from aap_inference_contract.execution import (
                ExecutionFailure,
                ModelExecutionError,
            )

            raise ModelExecutionError(
                ExecutionFailure(
                    request_id=request_id,
                    kind="unavailable",
                    code="routing_unavailable",
                    status_code=503,
                    detail={"reason": "pinned_tracker_route_unavailable"},
                )
            )

        context = build_tracker_context(
            model_key=self.model_key,
            job_id=ctx.job_id,
            task_id=ctx.task_id,
            project_id=ctx.project_id,
            dataset_item_id=ctx.dataset_item_id,
            annotation_id=ctx.annotation_id,
            from_frame=ctx.from_frame,
            to_frame=ctx.to_frame,
            direction=ctx.direction,
            prompt=ctx.prompt,
            source_geometry=ctx.source_geometry,
            model_id=ctx.model_id,
            sam_variant=ctx.sam_variant,
            text=ctx.text,
            exemplars=ctx.exemplars,
            output_geometry=ctx.output_geometry,
            seeds=ctx.seeds,
            session=ctx.session,
        )
        operation = build_tracker_operation(
            request_id=request_id,
            task_data=ctx.task_data,
            context=context,
            project_id=ctx.project_id,
            requested_registry_id=ctx.requested_registry_id,
            required_instance_id=ctx.required_instance_id,
            route_owner=f"tracker:{ctx.job_id}",
            route_operation="tracker",
            correlation=ctx.correlation,
        )
        result = await model_execution_port(
            ctx.db,
            bound_route=ctx.bound_route,
        ).execute(operation)
        session = (
            continue_tracker_session(ctx.session, result)
            if ctx.session is not None
            else None
        )
        frames = tracker_frame_results(
            result,
            outside_threshold=(
                settings.video_tracker_low_confidence_outside_threshold
                if ctx.outside_threshold is None
                else ctx.outside_threshold
            ),
        )
        if result.response_digest is None:
            raise ValueError("Tracker execution result is missing its digest")
        return TrackerPropagationResult(
            frames=frames,
            session=session,
            response_digest=result.response_digest,
            provenance=result.provenance,
        )


_REGISTRY: dict[str, TrackerAdapter] = {
    MockBboxTrackerAdapter.model_key: MockBboxTrackerAdapter(),
    "sam2_video": MLBackendVideoTrackerAdapter("sam2_video"),
    "sam3_video": MLBackendVideoTrackerAdapter("sam3_video"),
    # v0.21.26 · 阶段 B-pvs · SAM3 PVS 交互追踪 (点/框 seed + memory 传播)。与 sam3_video
    # (text 开集) 同后端不同模型; adapter 与 sam2_video 同为 seed 驱动 (透传 source_geometry)。
    "sam3_video_interactive": MLBackendVideoTrackerAdapter("sam3_video_interactive"),
}


def get_tracker_adapter(model_key: str) -> TrackerAdapter:
    try:
        return _REGISTRY[model_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported tracker model: {model_key}") from exc


def registered_tracker_models() -> list[str]:
    return sorted(_REGISTRY)
