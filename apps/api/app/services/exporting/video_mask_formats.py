from __future__ import annotations

from typing import Any

from app.services.mask_formats.image_codecs import compress_coco_rle
from app.services.video_tracks import (
    derive_track_number,
    frame_is_outside,
    resolve_track_at_frame,
)
from app.utils.raster_mask_rle import validate_coco_rle


def exact_mask_frames(geometry: dict[str, Any]) -> list[int]:
    return sorted(
        {
            int(keyframe.get("frame_index", 0))
            for keyframe in geometry.get("keyframes") or []
            if not frame_is_outside(
                geometry,
                int(keyframe.get("frame_index", 0)),
            )
        }
    )


def object_manifest(
    tracks: list[tuple[Any, str | None, dict[str, Any]]],
    object_ids: dict[Any, int],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for annotation_id, class_name, geometry in tracks:
        object_id = object_ids[annotation_id]
        keyframes = [
            keyframe
            for keyframe in geometry.get("keyframes") or []
            if not frame_is_outside(
                geometry,
                int(keyframe.get("frame_index", 0)),
            )
        ]
        result[str(object_id)] = {
            "category": class_name or "__unknown",
            "track_id": str(geometry.get("track_id") or object_id),
            "source_annotation_id": str(annotation_id),
            "frames": [f"{int(row.get('frame_index', 0)):05d}" for row in keyframes],
            "occluded_frames": [
                int(row.get("frame_index", 0))
                for row in keyframes
                if bool(row.get("occluded"))
            ],
        }
    return result


def build_mots_sequence(
    tracks: list[tuple[Any, str | None, dict[str, Any]]],
    *,
    class_ids: dict[str, int],
    source_frames: list[int],
    frame_base: int,
    checkpoint=None,
) -> tuple[str, dict[str, Any]]:
    if frame_base not in {0, 1}:
        raise ValueError("MOTS frame base must be 0 or 1")
    track_numbers = derive_track_number(
        [(annotation_id, geometry) for annotation_id, _class_name, geometry in tracks]
    )
    rows: list[tuple[int, int, str]] = []
    track_manifest: dict[str, dict[str, Any]] = {}
    for annotation_id, class_name, geometry in tracks:
        if checkpoint is not None:
            checkpoint()
        if not class_name or class_name not in class_ids:
            continue
        track_number = track_numbers[annotation_id]
        class_id = class_ids[class_name]
        track_manifest[str(track_number)] = {
            "class_id": class_id,
            "class_name": class_name,
            "track_id": str(geometry.get("track_id") or track_number),
            "source_annotation_id": str(annotation_id),
        }
        for output_index, source_frame in enumerate(source_frames):
            if checkpoint is not None:
                checkpoint()
            resolved = resolve_track_at_frame(geometry, source_frame)
            if resolved is None:
                continue
            rle = resolved.get("mask_rle")
            if not isinstance(rle, dict):
                raise ValueError("MOTS export requires hydrated mask RLE content")
            height, width, _counts = validate_coco_rle(rle)
            compressed = compress_coco_rle(rle)
            frame_number = output_index + frame_base
            line = (
                f"{frame_number} {track_number} {class_id} {height} {width} "
                f"{compressed['counts']}"
            )
            rows.append((frame_number, track_number, line))
    rows.sort(key=lambda row: (row[0], row[1]))
    return "\n".join(row[2] for row in rows), {
        "frame_base": frame_base,
        "source_frames": {
            str(index + frame_base): source_frame
            for index, source_frame in enumerate(source_frames)
        },
        "classes": {str(value): key for key, value in sorted(class_ids.items())},
        "tracks": track_manifest,
    }
