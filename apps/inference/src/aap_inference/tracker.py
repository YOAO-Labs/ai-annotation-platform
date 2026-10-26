"""Portable tracker request, session, and response transformations."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelOperationResult,
    freeze_json,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class TrackerSessionState:
    action: Literal["start", "continue", "close"] = "start"
    token: str | None = field(default=None, repr=False)
    span_start_frame: int | None = None
    span_end_frame: int | None = None

    def __post_init__(self) -> None:
        if self.action not in {"start", "continue", "close"}:
            raise ValueError("Invalid tracker session action")
        if self.token is not None and type(self.token) is not str:
            raise ValueError("Invalid tracker session token")
        for name in ("span_start_frame", "span_end_frame"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid tracker session span")
        if (
            self.span_start_frame is not None
            and self.span_end_frame is not None
            and self.span_end_frame < self.span_start_frame
        ):
            raise ValueError("Invalid tracker session span")


@dataclass(frozen=True, slots=True)
class TrackerFrameResult:
    frame_index: int
    geometry: Mapping[str, Any]
    confidence: float | None = None
    outside: bool = False
    instance_id: str | None = None
    primary: bool = False

    def __post_init__(self) -> None:
        if type(self.frame_index) is not int or self.frame_index < 0:
            raise ValueError("Tracker frame index must be a nonnegative integer")
        if self.confidence is not None and (
            type(self.confidence) not in {int, float}
            or not math.isfinite(float(self.confidence))
        ):
            raise ValueError("Tracker confidence must be finite and numeric")
        if type(self.outside) is not bool or type(self.primary) is not bool:
            raise ValueError("Tracker frame flags must be booleans")
        object.__setattr__(self, "geometry", freeze_json(self.geometry))


def build_tracker_context(
    *,
    model_key: str,
    job_id: uuid.UUID | str,
    task_id: uuid.UUID | str,
    project_id: uuid.UUID | str,
    dataset_item_id: uuid.UUID | str,
    annotation_id: uuid.UUID | str,
    from_frame: int,
    to_frame: int,
    direction: str,
    prompt: Mapping[str, Any],
    source_geometry: Mapping[str, Any],
    model_id: str | None = None,
    sam_variant: str | None = None,
    text: str | None = None,
    exemplars: list[dict[str, Any]] | None = None,
    output_geometry: str = "bbox",
    seeds: list[dict[str, Any]] | None = None,
    session: TrackerSessionState | None = None,
) -> dict[str, Any]:
    """Build the exact per-window video-tracker backend context."""
    context: dict[str, Any] = {
        "type": "video_tracker",
        "model_key": model_key,
        "job_id": str(job_id),
        "task_id": str(task_id),
        "project_id": str(project_id),
        "dataset_item_id": str(dataset_item_id),
        "annotation_id": str(annotation_id),
        "from_frame": from_frame,
        "to_frame": to_frame,
        "direction": direction,
        "prompt": dict(prompt),
        "source_geometry": dict(source_geometry),
    }
    if model_id is not None:
        context["model_id"] = model_id
    if sam_variant:
        context["model_variants"] = {"sam_variant": sam_variant}
    if text:
        context["text"] = text
    if exemplars:
        context["exemplars"] = list(exemplars)
    if output_geometry and output_geometry != "bbox":
        context["output_geometry"] = output_geometry
    if seeds:
        context["seeds"] = list(seeds)
    if session is not None:
        tracker_context: dict[str, Any] = {
            "action": session.action,
            "job_id": str(job_id),
            "direction": direction,
        }
        if session.token:
            tracker_context["token"] = session.token
        if session.action == "start":
            tracker_context.update(
                {
                    "span_start_frame": session.span_start_frame,
                    "span_end_frame": session.span_end_frame,
                }
            )
        context["tracker_context"] = tracker_context
    return context


def build_tracker_operation(
    *,
    request_id: uuid.UUID,
    task_data: Mapping[str, Any],
    context: Mapping[str, Any],
    project_id: uuid.UUID,
    requested_registry_id: uuid.UUID,
    required_instance_id: uuid.UUID,
    route_owner: str,
    route_operation: str,
    correlation: Mapping[str, Any] | None = None,
) -> FrozenModelOperation:
    """Freeze one window/close call against the caller's already-pinned instance."""
    return FrozenModelOperation(
        request_id=request_id,
        operation="pinned_tracker",
        task_inputs=(task_data,),
        context=context,
        project_id=project_id,
        requested_registry_id=requested_registry_id,
        requested_pool_id=None,
        endpoint_registry_id=None,
        required_instance_id=required_instance_id,
        route_owner=route_owner,
        route_operation=route_operation,
        correlation=correlation,
    )


def continue_tracker_session(
    session: TrackerSessionState, result: ModelOperationResult
) -> TrackerSessionState:
    """Return the immutable token state for the next window; never retain output in-place."""
    if result.operation != "pinned_tracker" or len(result.results) != 1:
        raise ValueError("Tracker operation must return one pinned response")
    token = result.results[0].session_token
    if session.action != "close" and not isinstance(token, str):
        raise ValueError("tracker_context_lost")
    return TrackerSessionState(action="continue", token=token)


def bbox_from_points(points: list[Any]) -> dict[str, float]:
    xs = [
        float(point[0])
        for point in points
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    ys = [
        float(point[1])
        for point in points
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not xs or not ys:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def bbox_from_geometry(geometry: Mapping[str, Any]) -> dict[str, float]:
    kind = geometry.get("type")
    if kind in {"video_track_bbox", "video_track_polygon"}:
        keyframes = sorted(
            geometry.get("keyframes") or (),
            key=lambda item: int(item.get("frame_index", 0)),
        )
        if keyframes:
            first = keyframes[0]
            if kind == "video_track_bbox":
                return dict(first.get("bbox") or {})
            return bbox_from_points(first.get("points") or [])
    if kind == "polygon":
        return bbox_from_points(geometry.get("points") or [])
    if kind == "mask":
        return dict(geometry.get("bbox") or {})
    if kind in {"bbox", "video_bbox"}:
        return {
            "x": float(geometry.get("x", 0)),
            "y": float(geometry.get("y", 0)),
            "w": float(geometry.get("w", geometry.get("width", 0))),
            "h": float(geometry.get("h", geometry.get("height", 0))),
        }
    return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}


def frame_result_from_payload(
    payload: Mapping[str, Any], *, outside_threshold: float
) -> TrackerFrameResult:
    confidence = payload.get("confidence")
    if confidence is not None:
        confidence = float(confidence)
    outside = bool(payload.get("outside", False))
    if confidence is not None and confidence < outside_threshold:
        outside = True
    geometry = payload.get("geometry")
    if not isinstance(geometry, Mapping):
        geometry = {
            key: payload[key] for key in ("type", "x", "y", "w", "h") if key in payload
        }
    if not geometry:
        geometry = {"type": "bbox", "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    instance_id = payload.get("instance_id")
    return TrackerFrameResult(
        frame_index=int(payload["frame_index"]),
        geometry=geometry,
        confidence=confidence,
        outside=outside,
        instance_id=None if instance_id is None else str(instance_id),
        primary=bool(payload.get("primary", False)),
    )


def tracker_frame_results(
    result: ModelOperationResult, *, outside_threshold: float
) -> tuple[TrackerFrameResult, ...]:
    if result.operation != "pinned_tracker" or len(result.results) != 1:
        raise ValueError("Tracker operation must return one pinned response")
    return tuple(
        frame_result_from_payload(item, outside_threshold=outside_threshold)
        for item in thaw_json(result.results[0].result)
        if isinstance(item, Mapping)
    )
