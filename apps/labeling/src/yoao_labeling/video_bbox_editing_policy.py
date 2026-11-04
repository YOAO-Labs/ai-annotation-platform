"""Portable compact-video bbox conversion and composition policy."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from .video_bbox_editing_contract import (
    AggregateBBoxes,
    AnnotationFence,
    ConvertTrack,
    JoinTracks,
    MergeTracks,
    SplitTrack,
    VideoBBoxEditingCommand,
    VideoBBoxFrame,
)
from .video_tracks import (
    frame_is_outside,
    normalize_outside_ranges,
    resolved_track_frames,
    resolve_track_at_frame,
    sorted_keyframes,
)


class _Facts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VideoBBoxTaskFacts(_Facts):
    id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    status: StrictStr
    review_round_id: uuid.UUID | None


class VideoBBoxAnnotationFacts(_Facts):
    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    version: StrictInt
    is_active: bool | None
    was_cancelled: bool | None
    annotation_type: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
    video_segment_id: uuid.UUID | None
    source: StrictStr
    confidence: float | None
    attributes: dict[str, object]
    track_id: StrictStr | None


def new_track_id() -> str:
    return f"trk_{uuid.uuid4().hex}"


def clean_bbox_geometry(geometry: dict) -> dict:
    return {
        "x": float(geometry.get("x", 0)),
        "y": float(geometry.get("y", 0)),
        "w": float(geometry.get("w", 0)),
        "h": float(geometry.get("h", 0)),
    }


def composition_keyframe(
    frame_index: int, bbox: dict, *, source: str = "manual"
) -> dict:
    return {
        "frame_index": int(frame_index),
        "bbox": clean_bbox_geometry(bbox),
        "source": "prediction" if source == "prediction" else "manual",
        "occluded": False,
    }


def track_visible_keyframes(geometry: dict) -> list[dict]:
    return [
        keyframe
        for keyframe in sorted_keyframes(geometry)
        if not frame_is_outside(geometry, int(keyframe.get("frame_index", 0)))
    ]


def clip_outside_ranges(
    geometry: dict, *, start: int | None, end: int | None
) -> list[dict]:
    clipped = []
    for range_ in geometry.get("outside") or []:
        from_frame = int(range_.get("from", 0))
        to_frame = int(range_.get("to", 0))
        if start is not None:
            from_frame = max(from_frame, start)
        if end is not None:
            to_frame = min(to_frame, end)
        if from_frame <= to_frame:
            clipped.append(
                {
                    "from": from_frame,
                    "to": to_frame,
                    "source": (
                        "prediction"
                        if range_.get("source") == "prediction"
                        else "manual"
                    ),
                }
            )
    return normalize_outside_ranges(clipped)


def _common(task: VideoBBoxTaskFacts, actor_id: uuid.UUID) -> dict[str, object]:
    return {
        "schema": "aap-labeling-video-bbox-editing/v1",
        "task_id": task.id,
        "project_id": task.project_id,
        "actor_id": actor_id,
        "batch_id": task.batch_id,
        "expected_status": task.status,
        "expected_review_round_id": task.review_round_id,
    }


def _fence(annotation: VideoBBoxAnnotationFacts) -> AnnotationFence:
    return AnnotationFence(
        annotation_id=annotation.id,
        expected_version=annotation.version,
    )


def _ensure_source(
    task: VideoBBoxTaskFacts, annotation: VideoBBoxAnnotationFacts
) -> None:
    if annotation.task_id != task.id or annotation.project_id != task.project_id:
        raise ValueError("Annotation does not belong to this task")
    if not annotation.is_active or annotation.was_cancelled is True:
        raise ValueError("Annotation not found")


def convert_video_track_to_bboxes_command(
    *,
    task: VideoBBoxTaskFacts,
    annotation: VideoBBoxAnnotationFacts,
    actor_id: uuid.UUID,
    operation: Literal["copy", "split"],
    scope: Literal["frame", "track"],
    frame_index: int | None = None,
    frame_mode: Literal["keyframes", "all_frames"] = "keyframes",
    frame_count: int | None = None,
    max_created: int = 5000,
) -> ConvertTrack:
    _ensure_source(task, annotation)
    geometry = annotation.geometry or {}
    if geometry.get("type") != "video_track_bbox":
        raise ValueError("annotation must be a video_track_bbox")

    removed_frame_indexes: list[int] = []
    if scope == "frame":
        if frame_index is None:
            raise ValueError("frame_index is required for frame scope")
        keyframes = sorted_keyframes(geometry)
        if operation == "split":
            exact = next(
                (
                    keyframe
                    for keyframe in keyframes
                    if int(keyframe.get("frame_index", 0)) == frame_index
                ),
                None,
            )
            if not exact or frame_is_outside(geometry, frame_index):
                raise ValueError("frame split requires an exact visible keyframe")
            frames = [
                {
                    "frame_index": frame_index,
                    "bbox": exact.get("bbox") or {},
                }
            ]
            removed_frame_indexes = [frame_index]
        else:
            resolved = resolve_track_at_frame(geometry, frame_index)
            if not resolved:
                raise ValueError("track has no bbox at the requested frame")
            frames = [resolved]
    elif scope == "track":
        frames = resolved_track_frames(
            geometry,
            frame_mode=frame_mode,
            frame_count=frame_count,
        )
        if operation == "split":
            removed_frame_indexes = [int(frame["frame_index"]) for frame in frames]
    else:
        raise ValueError("scope must be one of: frame, track")

    if not frames:
        raise ValueError("track conversion produced no frames")
    if len(frames) > max_created:
        raise ValueError(
            f"track conversion would create more than {max_created} bboxes"
        )

    created_frames = [
        VideoBBoxFrame(
            annotation_id=uuid.uuid4(),
            frame_index=int(frame.get("frame_index", 0)),
            x=(frame.get("bbox") or {}).get("x", 0),
            y=(frame.get("bbox") or {}).get("y", 0),
            w=(frame.get("bbox") or {}).get("w", 0),
            h=(frame.get("bbox") or {}).get("h", 0),
        )
        for frame in frames
    ]

    return ConvertTrack(
        **_common(task, actor_id),
        operation="convert_track",
        source_fences=[_fence(annotation)],
        conversion_operation=operation,
        scope=scope,
        frame_index=frame_index,
        frame_mode=frame_mode,
        frame_count=frame_count,
        max_created=max_created,
        created_frames=created_frames,
        removed_frame_indexes=removed_frame_indexes,
    )


def compose_video_tracks_command(
    *,
    task: VideoBBoxTaskFacts,
    annotations: list[VideoBBoxAnnotationFacts],
    actor_id: uuid.UUID,
    operation: Literal[
        "aggregate_bboxes", "split_track", "merge_tracks", "join_tracks"
    ],
    frame_index: int | None = None,
    delete_sources: bool = True,
    gap_mode: Literal["interpolate", "outside"] = "interpolate",
) -> VideoBBoxEditingCommand:
    for annotation in annotations:
        _ensure_source(task, annotation)

    common = _common(task, actor_id)
    if operation == "aggregate_bboxes":
        if not annotations:
            raise ValueError("annotation_ids is required")
        if any(
            (annotation.geometry or {}).get("type") != "video_bbox"
            for annotation in annotations
        ):
            raise ValueError("aggregate_bboxes only accepts video_bbox annotations")
        class_names = {annotation.class_name for annotation in annotations}
        if len(class_names) != 1:
            raise ValueError("video_bbox annotations must share one class")

        frames: dict[int, VideoBBoxAnnotationFacts] = {}
        for annotation in annotations:
            frame = int((annotation.geometry or {}).get("frame_index", 0))
            if frame in frames:
                raise ValueError("video_bbox annotations must not share a frame_index")
            frames[frame] = annotation

        ordered = [frames[frame] for frame in sorted(frames)]
        track_id = new_track_id()
        geometry = {
            "type": "video_track_bbox",
            "track_id": track_id,
            "keyframes": [
                composition_keyframe(
                    int(annotation.geometry.get("frame_index", 0)),
                    annotation.geometry,
                    source="manual",
                )
                for annotation in ordered
            ],
            "outside": [],
        }
        return AggregateBBoxes(
            **common,
            operation="aggregate_bboxes",
            source_fences=[_fence(annotation) for annotation in ordered],
            created_annotation_id=uuid.uuid4(),
            created_track_id=track_id,
            created_geometry=geometry,
            delete_sources=delete_sources,
        )

    if operation == "split_track":
        if frame_index is None:
            raise ValueError("frame_index is required for split_track")
        if len(annotations) != 1:
            raise ValueError("split_track requires exactly one annotation")
        source = annotations[0]
        geometry = source.geometry or {}
        if geometry.get("type") != "video_track_bbox":
            raise ValueError("split_track only accepts a video_track_bbox annotation")
        if not resolve_track_at_frame(geometry, frame_index):
            raise ValueError("split_track requires a visible frame")

        keyframes = sorted_keyframes(geometry)
        before_keyframes = [
            dict(keyframe)
            for keyframe in keyframes
            if int(keyframe.get("frame_index", 0)) <= frame_index
        ]
        if not any(
            int(keyframe.get("frame_index", 0)) == frame_index
            for keyframe in before_keyframes
        ):
            resolved = resolve_track_at_frame(geometry, frame_index)
            if not resolved:
                raise ValueError("split_track requires a visible frame")
            before_keyframes.append(
                composition_keyframe(frame_index, resolved.get("bbox") or {})
            )
        before_keyframes.sort(key=lambda keyframe: int(keyframe.get("frame_index", 0)))

        next_frame = frame_index + 1
        next_resolved = resolve_track_at_frame(geometry, next_frame)
        after_keyframes = [
            dict(keyframe)
            for keyframe in keyframes
            if int(keyframe.get("frame_index", 0)) > frame_index
        ]
        if next_resolved and not any(
            int(keyframe.get("frame_index", 0)) == next_frame
            for keyframe in after_keyframes
        ):
            after_keyframes.insert(
                0,
                composition_keyframe(next_frame, next_resolved.get("bbox") or {}),
            )
        after_keyframes.sort(key=lambda keyframe: int(keyframe.get("frame_index", 0)))
        if not track_visible_keyframes(
            {"type": "video_track_bbox", "keyframes": after_keyframes}
        ):
            raise ValueError("split_track requires a visible tail segment")

        track_id = new_track_id()
        updated_geometry = {
            **geometry,
            "keyframes": before_keyframes,
            "outside": clip_outside_ranges(geometry, start=None, end=frame_index),
        }
        created_geometry = {
            "type": "video_track_bbox",
            "track_id": track_id,
            "semantic_label": geometry.get("semantic_label"),
            "keyframes": after_keyframes,
            "outside": clip_outside_ranges(geometry, start=next_frame, end=None),
        }
        return SplitTrack(
            **common,
            operation="split_track",
            source_fences=[_fence(source)],
            frame_index=frame_index,
            created_annotation_id=uuid.uuid4(),
            created_track_id=track_id,
            updated_geometry=updated_geometry,
            created_geometry=created_geometry,
        )

    if operation not in {"merge_tracks", "join_tracks"}:
        raise ValueError("unsupported composition operation")
    if len(annotations) != 2:
        raise ValueError(f"{operation} requires exactly two annotations")
    if any(
        (annotation.geometry or {}).get("type") != "video_track_bbox"
        for annotation in annotations
    ):
        raise ValueError(f"{operation} only accepts video_track_bbox annotations")
    if annotations[0].class_name != annotations[1].class_name:
        raise ValueError(f"{operation} requires tracks with the same class")

    ranges: list[tuple[int, int, VideoBBoxAnnotationFacts]] = []
    for annotation in annotations:
        visible = track_visible_keyframes(annotation.geometry or {})
        if not visible:
            raise ValueError(f"{operation} requires visible keyframes")
        frames = [int(keyframe.get("frame_index", 0)) for keyframe in visible]
        ranges.append((min(frames), max(frames), annotation))
    ranges.sort(key=lambda item: item[0])
    _first_start, first_end, _first = ranges[0]
    second_start, _second_end, _second = ranges[1]
    if first_end >= second_start:
        raise ValueError(f"{operation} requires non-overlapping tracks")

    survivor, removed = annotations
    keyframes = [
        dict(keyframe)
        for annotation in annotations
        for keyframe in sorted_keyframes(annotation.geometry or {})
    ]
    frame_counts: dict[int, int] = {}
    for keyframe in keyframes:
        frame = int(keyframe.get("frame_index", 0))
        frame_counts[frame] = frame_counts.get(frame, 0) + 1
    if any(count > 1 for count in frame_counts.values()):
        raise ValueError(f"{operation} requires unique keyframe frame_index values")
    keyframes.sort(key=lambda keyframe: int(keyframe.get("frame_index", 0)))

    outside = [
        range_
        for annotation in annotations
        for range_ in ((annotation.geometry or {}).get("outside") or [])
    ]
    fill_gap_outside = operation == "merge_tracks" or gap_mode == "outside"
    if fill_gap_outside and first_end + 1 <= second_start - 1:
        outside.append(
            {
                "from": first_end + 1,
                "to": second_start - 1,
                "source": "manual",
            }
        )

    track_id = (survivor.geometry or {}).get("track_id") or new_track_id()
    survivor_geometry = {
        **(survivor.geometry or {}),
        "type": "video_track_bbox",
        "track_id": track_id,
        "keyframes": keyframes,
        "outside": normalize_outside_ranges(outside),
    }
    source_fences = [_fence(survivor), _fence(removed)]
    common_fields = {
        **common,
        "source_fences": source_fences,
        "survivor_id": survivor.id,
        "survivor_track_id": track_id,
        "survivor_geometry": survivor_geometry,
    }
    if operation == "merge_tracks":
        return MergeTracks(operation=operation, **common_fields)
    return JoinTracks(operation=operation, gap_mode=gap_mode, **common_fields)


__all__ = [
    "VideoBBoxAnnotationFacts",
    "VideoBBoxTaskFacts",
    "clip_outside_ranges",
    "composition_keyframe",
    "compose_video_tracks_command",
    "convert_video_track_to_bboxes_command",
    "new_track_id",
    "track_visible_keyframes",
]
