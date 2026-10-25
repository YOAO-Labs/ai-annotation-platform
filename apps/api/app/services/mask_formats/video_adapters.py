from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.mask_format import MaskFormatPlan, MaskFormatPlanItem
from app.services.annotations_import import import_aap_json_annotations
from app.services.mask_formats.contracts import (
    MaskFormatDescriptor,
    StagedObject,
    canonical_digest,
)
from app.services.mask_formats.image_codecs import (
    indexed_png_to_coco,
    normalize_coco_segmentation_rle,
    rasterize_coco_polygons,
)
from aap_assets_source.archive import ArchiveLimits, SafeZipArchive
from aap_workspace.configuration_policy import lookup_classes_for_tool_unit
from app.services.raster_mask_storage import build_rle_reference, load_coco_rle
from app.services.task_matcher import (
    resolve_task,
    resolve_task_by_file_stem,
    resolve_task_by_video_sequence,
)
from app.services.video_tracks import resolve_track_at_frame
from app.services.exporting.video_scope import (
    VideoExportScope,
    clip_video_geometry,
)
from app.utils.raster_mask_rle import coco_rle_area, validate_coco_rle

from .adapters import LegacyPackagingAdapter
from .png_contract import validate_png_contract
from .planning import _code, _plan, _worst_loss


@dataclass
class ParsedVideoTrack:
    class_name: str
    track_id: str
    keyframes: list[dict[str, Any]]
    outside: list[dict[str, Any]] = field(default_factory=list)
    source_id: str | None = None


@dataclass
class ParsedVideoItem:
    sequence: str
    width: int
    height: int
    tracks: list[ParsedVideoTrack]
    task: Task | None = None
    frame_count: int | None = None
    losses: list[str] = field(default_factory=list)
    warnings: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    frame_mapping: dict[str, int] = field(default_factory=dict)
    id_mapping: dict[str, Any] = field(default_factory=dict)


def _archive_limits() -> ArchiveLimits:
    return ArchiveLimits(
        max_files=settings.mask_format_max_archive_files,
        max_entry_bytes=settings.mask_format_max_entry_bytes,
        max_total_bytes=settings.mask_format_temp_quota_bytes,
        max_compression_ratio=settings.mask_format_max_compression_ratio,
    )


def _mapped_label(raw: str, mapping: dict[str, Any]) -> str:
    labels = (
        mapping.get("labels") if isinstance(mapping.get("labels"), dict) else mapping
    )
    value = labels.get(raw, raw) if isinstance(labels, dict) else raw
    return str(value).strip()


def _read_json_bytes(data: bytes, *, source: str) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {source}") from exc


def _read_archive_json(archive: SafeZipArchive, path: str) -> Any:
    with archive.open(path) as source:
        return _read_json_bytes(source.read(), source=path)


def _find_archive_path(
    archive: SafeZipArchive,
    *,
    names: set[str],
) -> str | None:
    candidates = sorted(
        entry.normalized_path
        for entry in archive.entries
        if entry.normalized_path.rsplit("/", 1)[-1] in names
    )
    return candidates[0] if candidates else None


def _safe_track_id(value: Any, *, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    if len(text) <= 64:
        return text
    return canonical_digest(text)[:64]


async def _resolve_video_task(
    db: AsyncSession,
    project_id: uuid.UUID,
    sequence: str,
) -> Task | None:
    task = await resolve_task(db, project_id, {"file_path": sequence})
    if task is not None:
        return task
    task, _reason = await resolve_task_by_file_stem(db, project_id, sequence)
    if task is not None:
        return task
    return await resolve_task_by_video_sequence(db, project_id, sequence)


async def _video_dimensions(
    db: AsyncSession,
    task: Task | None,
) -> tuple[int, int, int | None] | None:
    if task is None or task.dataset_item_id is None:
        return None
    from app.services.assets_media_summary import AssetsTaskMediaSummaryReader

    summary = await AssetsTaskMediaSummaryReader(db).current_video_summary(
        task.dataset_item_id
    )
    if summary is None:
        return None
    width, height, video = summary
    if not width or not height:
        return None
    frame_count = video.get("frame_count")
    return int(width), int(height), int(frame_count) if frame_count else None


def _ranges_for_missing(missing: list[int]) -> list[dict[str, Any]]:
    if not missing:
        return []
    ranges: list[dict[str, Any]] = []
    start = previous = missing[0]
    for frame in missing[1:]:
        if frame == previous + 1:
            previous = frame
            continue
        ranges.append({"from": start, "to": previous, "source": "manual"})
        start = previous = frame
    ranges.append({"from": start, "to": previous, "source": "manual"})
    return ranges


def _outside_ranges(
    visible_frames: set[int], frame_count: int | None
) -> list[dict[str, Any]]:
    if frame_count is None or frame_count <= 0:
        return []
    return _ranges_for_missing(
        [frame for frame in range(frame_count) if frame not in visible_frames]
    )


def _outside_known_frames(
    visible_frames: set[int], known_frames: set[int]
) -> list[dict[str, Any]]:
    return _ranges_for_missing(sorted(known_frames - visible_frames))


def _item_identity(item: ParsedVideoItem) -> str:
    return canonical_digest(
        {
            "sequence": item.sequence,
            "width": item.width,
            "height": item.height,
            "tracks": [
                {
                    "class_name": track.class_name,
                    "track_id": track.track_id,
                    "keyframes": track.keyframes,
                    "outside": track.outside,
                    "source_id": track.source_id,
                }
                for track in item.tracks
            ],
            "losses": item.losses,
            "frame_mapping": item.frame_mapping,
        }
    )


def _sequence_from_coco_image(file_name: str) -> str:
    path = PurePosixPath(file_name)
    parts = list(path.parts)
    if "images" in parts:
        parts = parts[parts.index("images") + 1 :]
    if len(parts) < 2:
        return path.parent.as_posix().strip("./") or path.stem
    return PurePosixPath(*parts[:-1]).as_posix()


def _coco_frame_mapping(image: dict[str, Any], frame_base: int) -> tuple[int, int]:
    source_frame = image.get("source_frame_index")
    try:
        external_frame = int(PurePosixPath(str(image.get("file_name") or "")).stem)
    except ValueError:
        if type(source_frame) is not int:
            raise ValueError(
                "COCO Frames image requires source_frame_index or a numeric frame name"
            ) from None
        external_frame = source_frame + frame_base
    if type(source_frame) is not int:
        source_frame = external_frame - frame_base
    if source_frame < 0:
        raise ValueError("COCO Frames source frame must be non-negative")
    return external_frame, source_frame


async def _parse_coco_frames(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    options: dict[str, Any],
) -> list[ParsedVideoItem]:
    if zipfile.is_zipfile(staged.local_path):
        with SafeZipArchive(staged.local_path, _archive_limits()) as archive:
            json_paths = [
                entry.normalized_path
                for entry in archive.entries
                if entry.normalized_path.endswith("annotations.json")
            ]
            if not json_paths:
                raise ValueError("COCO Frames archive requires annotations.json")
            raw = _read_archive_json(archive, sorted(json_paths)[0])
    else:
        data = Path(staged.local_path).read_bytes()
        if len(data) > settings.mask_format_max_entry_bytes:
            raise ValueError("resource_budget_exceeded:max_entry_bytes")
        raw = _read_json_bytes(data, source=staged.local_path)
    if not isinstance(raw, dict):
        raise ValueError("COCO Frames root must be an object")

    categories = {
        row.get("id"): str(row.get("name"))
        for row in raw.get("categories") or []
        if isinstance(row, dict)
        and isinstance(row.get("id"), int)
        and isinstance(row.get("name"), str)
    }
    image_rows = {
        row.get("id"): row
        for row in raw.get("images") or []
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }
    sequence_images: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for image in image_rows.values():
        sequence_images[
            _sequence_from_coco_image(str(image.get("file_name") or ""))
        ].append(image)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    track_identity_lost: set[str] = set()
    frame_base = int(options.get("frame_base", 1))
    if frame_base not in {0, 1}:
        raise ValueError("COCO Frames frame_base must be 0 or 1")
    for annotation in raw.get("annotations") or []:
        if not isinstance(annotation, dict):
            continue
        image = image_rows.get(annotation.get("image_id"))
        if image is None:
            raise ValueError("COCO Frames annotation references an unknown image")
        width, height = image.get("width"), image.get("height")
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
        ):
            raise ValueError("COCO Frames image dimensions must be positive integers")
        sequence = _sequence_from_coco_image(str(image.get("file_name") or ""))
        _external_frame, raw_frame = _coco_frame_mapping(image, frame_base)
        attributes = (
            annotation.get("attributes")
            if isinstance(annotation.get("attributes"), dict)
            else {}
        )
        external_track = annotation.get("track_id") or attributes.get("__track_id")
        if external_track is None:
            external_track = f"frame-{raw_frame}-annotation-{annotation.get('id')}"
            track_identity_lost.add(sequence)
        key = (sequence, str(external_track))
        raw_label = categories.get(annotation.get("category_id"))
        if not raw_label:
            raise ValueError(
                f"unknown COCO Frames category_id: {annotation.get('category_id')!r}"
            )
        segmentation = annotation.get("segmentation")
        if isinstance(segmentation, dict):
            rle = normalize_coco_segmentation_rle(
                segmentation,
                expected_width=width,
                expected_height=height,
            )
        elif isinstance(segmentation, list):
            rle = rasterize_coco_polygons(segmentation, width=width, height=height)
        else:
            continue
        if coco_rle_area(rle) == 0:
            raise ValueError("COCO Frames mask must contain foreground pixels")
        group = grouped.setdefault(
            key,
            {
                "class_name": _mapped_label(raw_label, mapping),
                "width": width,
                "height": height,
                "frames": {},
                "source_id": str(external_track),
            },
        )
        if group["class_name"] != _mapped_label(raw_label, mapping):
            raise ValueError("COCO Frames track changes category across frames")
        if raw_frame in group["frames"]:
            raise ValueError("COCO Frames track has duplicate annotations in one frame")
        group["frames"][raw_frame] = {
            "frame_index": raw_frame,
            "rle": rle,
            "occluded": bool(attributes.get("__occluded", False)),
        }

    items: list[ParsedVideoItem] = []
    for sequence, images in sorted(sequence_images.items()):
        task = await _resolve_video_task(db, project.id, sequence)
        dims = await _video_dimensions(db, task)
        width = int(images[0].get("width") or 0)
        height = int(images[0].get("height") or 0)
        frame_count = dims[2] if dims else None
        if frame_count is None:
            frame_count = (
                max([int(image.get("source_frame_index", 0)) for image in images] + [0])
                + 1
            )
        tracks: list[ParsedVideoTrack] = []
        external_source_frames = {
            external: source
            for external, source in (
                _coco_frame_mapping(image, frame_base) for image in images
            )
        }
        known_frames = set(external_source_frames.values())
        for (track_sequence, track_key), group in sorted(grouped.items()):
            if track_sequence != sequence:
                continue
            frames = sorted(
                group["frames"].values(), key=lambda row: row["frame_index"]
            )
            visible = {int(row["frame_index"]) for row in frames}
            tracks.append(
                ParsedVideoTrack(
                    class_name=group["class_name"],
                    track_id=_safe_track_id(
                        track_key, fallback=canonical_digest(group)[:16]
                    ),
                    keyframes=frames,
                    outside=_outside_known_frames(visible, known_frames),
                    source_id=group["source_id"],
                )
            )
        items.append(
            ParsedVideoItem(
                sequence=sequence,
                width=width,
                height=height,
                tracks=tracks,
                task=task,
                frame_count=frame_count,
                losses=["track_identity_lost"]
                if sequence in track_identity_lost
                else [],
                frame_mapping={
                    str(external): source
                    for external, source in sorted(external_source_frames.items())
                },
            )
        )
    return items


def _palette_values(data: bytes, *, width: int, height: int) -> list[int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.mode != "P" or image.size != (width, height):
                raise ValueError("video mask PNG must be an 8-bit palette image")
            return list(image.getdata())
    except OSError as exc:
        raise ValueError("video mask PNG is invalid") from exc


def _root_video_manifest(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(row.get("sequence")): row
        for row in raw.get("videos") or []
        if isinstance(row, dict) and row.get("sequence")
    }


async def _parse_davis(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    _options: dict[str, Any],
) -> list[ParsedVideoItem]:
    with SafeZipArchive(staged.local_path, _archive_limits()) as archive:
        root_manifest: dict[str, Any] = {}
        if any(entry.normalized_path == "manifest.json" for entry in archive.entries):
            candidate = _read_archive_json(archive, "manifest.json")
            root_manifest = candidate if isinstance(candidate, dict) else {}
        format_manifest_path = next(
            (
                entry.normalized_path
                for entry in archive.entries
                if entry.normalized_path.endswith("davis_manifest.json")
            ),
            None,
        )
        format_manifest = (
            _read_archive_json(archive, format_manifest_path)
            if format_manifest_path
            else {}
        )
        object_meta = (
            format_manifest.get("sequences", {})
            if isinstance(format_manifest, dict)
            else {}
        )
        root_videos = _root_video_manifest(root_manifest)
        frame_rows: dict[str, list[tuple[int, bytes]]] = defaultdict(list)
        for entry in archive.entries:
            parts = PurePosixPath(entry.normalized_path).parts
            if "Annotations" not in parts or not entry.normalized_path.lower().endswith(
                ".png"
            ):
                continue
            index = parts.index("Annotations")
            if len(parts) < index + 4:
                continue
            sequence = PurePosixPath(*parts[index + 2 : -1]).as_posix()
            try:
                frame = int(PurePosixPath(parts[-1]).stem)
            except ValueError as exc:
                raise ValueError("DAVIS frame names must be numeric") from exc
            with archive.open(entry.normalized_path) as source:
                data = source.read()
            frame_rows[sequence].append((frame, data))

        items: list[ParsedVideoItem] = []
        for sequence, rows in sorted(frame_rows.items()):
            task = await _resolve_video_task(db, project.id, sequence)
            dims = await _video_dimensions(db, task)
            source_frames = list(
                root_videos.get(sequence, {}).get("grid_source_frames") or []
            )
            per_object: dict[int, list[dict[str, Any]]] = defaultdict(list)
            known_frames: set[int] = set()
            width = height = 0
            for output_frame, png in sorted(rows):
                with Image.open(io.BytesIO(png)) as image:
                    width, height = image.size
                validate_source = io.BytesIO(png)
                validate_png_contract(
                    validate_source,
                    expected_width=width,
                    expected_height=height,
                    allowed_color_types=frozenset({3}),
                )
                source_frame = (
                    int(source_frames[output_frame])
                    if output_frame < len(source_frames)
                    else output_frame
                )
                known_frames.add(source_frame)
                values = _palette_values(png, width=width, height=height)
                for pixel_id in sorted(set(values) - {0, 255}):
                    rle = indexed_png_to_coco(
                        png,
                        width=width,
                        height=height,
                        pixel_id=pixel_id,
                    )
                    per_object[pixel_id].append(
                        {"frame_index": source_frame, "rle": rle, "occluded": False}
                    )
            frame_count = (
                dims[2] if dims else (max(source_frames, default=len(rows) - 1) + 1)
            )
            sequence_meta = (
                object_meta.get(sequence, {}) if isinstance(object_meta, dict) else {}
            )
            objects = (
                sequence_meta.get("objects", {})
                if isinstance(sequence_meta, dict)
                else {}
            )
            tracks: list[ParsedVideoTrack] = []
            for pixel_id, keyframes in sorted(per_object.items()):
                meta = (
                    objects.get(str(pixel_id), {}) if isinstance(objects, dict) else {}
                )
                occluded = {int(value) for value in meta.get("occluded_frames", [])}
                for keyframe in keyframes:
                    keyframe["occluded"] = keyframe["frame_index"] in occluded
                visible = {int(row["frame_index"]) for row in keyframes}
                raw_label = str(meta.get("category") or f"object_{pixel_id}")
                tracks.append(
                    ParsedVideoTrack(
                        class_name=_mapped_label(raw_label, mapping),
                        track_id=_safe_track_id(
                            meta.get("track_id"), fallback=f"davis-{pixel_id}"
                        ),
                        keyframes=sorted(keyframes, key=lambda row: row["frame_index"]),
                        outside=_outside_known_frames(visible, known_frames),
                        source_id=str(meta.get("source_annotation_id") or pixel_id),
                    )
                )
            items.append(
                ParsedVideoItem(
                    sequence=sequence,
                    width=width,
                    height=height,
                    tracks=tracks,
                    task=task,
                    frame_count=frame_count,
                    losses=["occlusion_lost"] if not objects else [],
                    frame_mapping={
                        str(index): int(value)
                        for index, value in enumerate(source_frames)
                    },
                    id_mapping={str(key): value for key, value in objects.items()}
                    if isinstance(objects, dict)
                    else {},
                )
            )
        return items


async def _parse_youtube_vos(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    options: dict[str, Any],
) -> list[ParsedVideoItem]:
    gap_policy = str(options.get("sparse_gap_policy") or "")
    if gap_policy not in {"outside_gaps", "nearest_hold"}:
        raise ValueError("YouTube-VOS import requires sparse_gap_policy")
    with SafeZipArchive(staged.local_path, _archive_limits()) as archive:
        meta_path = _find_archive_path(archive, names={"meta.json"})
        if meta_path is None:
            raise ValueError("YouTube-VOS archive requires meta.json")
        meta = _read_archive_json(archive, meta_path)
        if not isinstance(meta, dict) or not isinstance(meta.get("videos"), dict):
            raise ValueError("YouTube-VOS meta.json requires videos")
        prefix = meta_path[: -len("meta.json")]
        aap_meta = meta.get("_aap") if isinstance(meta.get("_aap"), dict) else {}
        frame_maps = (
            aap_meta.get("source_frames")
            if isinstance(aap_meta.get("source_frames"), dict)
            else {}
        )
        items: list[ParsedVideoItem] = []
        for sequence, video in sorted(meta["videos"].items()):
            if not isinstance(video, dict) or not isinstance(
                video.get("objects"), dict
            ):
                continue
            task = await _resolve_video_task(db, project.id, sequence)
            dims = await _video_dimensions(db, task)
            tracks: list[ParsedVideoTrack] = []
            width = height = 0
            sequence_frame_map = (
                frame_maps.get(sequence, {}) if isinstance(frame_maps, dict) else {}
            )
            for object_id, object_row in sorted(video["objects"].items()):
                if not isinstance(object_row, dict):
                    continue
                keyframes: list[dict[str, Any]] = []
                for frame_name in object_row.get("frames") or []:
                    png_path = f"{prefix}Annotations/{sequence}/{frame_name}.png"
                    archive.require_paths([png_path])
                    with archive.open(png_path) as source:
                        png = source.read()
                    with Image.open(io.BytesIO(png)) as image:
                        width, height = image.size
                    source_frame = int(
                        sequence_frame_map.get(str(frame_name), int(frame_name))
                    )
                    rle = indexed_png_to_coco(
                        png,
                        width=width,
                        height=height,
                        pixel_id=int(object_id),
                    )
                    if coco_rle_area(rle):
                        keyframes.append(
                            {
                                "frame_index": source_frame,
                                "rle": rle,
                                "occluded": source_frame
                                in set(object_row.get("occluded_frames") or []),
                            }
                        )
                if not keyframes:
                    continue
                frame_count = (
                    dims[2]
                    if dims
                    else max(row["frame_index"] for row in keyframes) + 1
                )
                visible = {int(row["frame_index"]) for row in keyframes}
                raw_label = str(object_row.get("category") or f"object_{object_id}")
                tracks.append(
                    ParsedVideoTrack(
                        class_name=_mapped_label(raw_label, mapping),
                        track_id=_safe_track_id(
                            object_row.get("track_id"), fallback=f"ytvos-{object_id}"
                        ),
                        keyframes=sorted(keyframes, key=lambda row: row["frame_index"]),
                        outside=_outside_ranges(visible, frame_count)
                        if gap_policy == "outside_gaps"
                        else [],
                        source_id=str(
                            object_row.get("source_annotation_id") or object_id
                        ),
                    )
                )
            frame_count = (
                dims[2]
                if dims
                else max(
                    [row["frame_index"] for track in tracks for row in track.keyframes]
                    + [0]
                )
                + 1
            )
            items.append(
                ParsedVideoItem(
                    sequence=sequence,
                    width=width,
                    height=height,
                    tracks=tracks,
                    task=task,
                    frame_count=frame_count,
                    losses=["sparse_frames_collapsed"],
                    frame_mapping={
                        str(key): int(value)
                        for key, value in sequence_frame_map.items()
                    },
                )
            )
        return items


async def _parse_mots(
    db: AsyncSession,
    project: Project,
    staged: StagedObject,
    mapping: dict[str, Any],
    options: dict[str, Any],
) -> list[ParsedVideoItem]:
    with SafeZipArchive(staged.local_path, _archive_limits()) as archive:
        manifest_path = _find_archive_path(archive, names={"mots_manifest.json"})
        if manifest_path is None:
            raise ValueError("MOTS archive requires mots_manifest.json")
        manifest = _read_archive_json(archive, manifest_path)
        if not isinstance(manifest, dict) or manifest.get("format_id") != "mots":
            raise ValueError("invalid MOTS manifest")
        sequences = manifest.get("sequences")
        if not isinstance(sequences, dict):
            raise ValueError("MOTS manifest requires sequences")
        items: list[ParsedVideoItem] = []
        for sequence, sequence_meta in sorted(sequences.items()):
            if not isinstance(sequence_meta, dict):
                continue
            text_path = str(
                sequence_meta.get("annotations_path") or f"instances/{sequence}.txt"
            )
            archive.require_paths([text_path])
            with archive.open(text_path) as source:
                lines = source.read().decode("utf-8-sig").splitlines()
            frame_base = int(
                options.get("frame_base", sequence_meta.get("frame_base", 0))
            )
            if frame_base not in {0, 1}:
                raise ValueError("MOTS frame base must be 0 or 1")
            classes = (
                sequence_meta.get("classes")
                if isinstance(sequence_meta.get("classes"), dict)
                else {}
            )
            tracks_meta = (
                sequence_meta.get("tracks")
                if isinstance(sequence_meta.get("tracks"), dict)
                else {}
            )
            source_frames = (
                sequence_meta.get("source_frames")
                if isinstance(sequence_meta.get("source_frames"), dict)
                else {}
            )
            grouped: dict[str, dict[str, Any]] = {}
            width = height = 0
            for line_number, line in enumerate(lines, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split(maxsplit=5)
                if len(parts) != 6:
                    raise ValueError(f"invalid MOTS row at line {line_number}")
                try:
                    frame_number, track_number, class_id, height, width = map(
                        int, parts[:5]
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"invalid MOTS integer at line {line_number}"
                    ) from exc
                source_frame = int(
                    source_frames.get(str(frame_number), frame_number - frame_base)
                )
                rle = normalize_coco_segmentation_rle(
                    {"size": [height, width], "counts": parts[5]},
                    expected_width=width,
                    expected_height=height,
                )
                meta = (
                    tracks_meta.get(str(track_number), {})
                    if isinstance(tracks_meta, dict)
                    else {}
                )
                raw_label = str(
                    meta.get("class_name")
                    or classes.get(str(class_id))
                    or f"class_{class_id}"
                )
                group = grouped.setdefault(
                    str(track_number),
                    {
                        "class_name": _mapped_label(raw_label, mapping),
                        "track_id": meta.get("track_id"),
                        "source_id": meta.get("source_annotation_id"),
                        "frames": {},
                    },
                )
                if source_frame in group["frames"]:
                    raise ValueError("MOTS track has duplicate rows in one frame")
                group["frames"][source_frame] = {
                    "frame_index": source_frame,
                    "rle": rle,
                    "occluded": False,
                }
            task = await _resolve_video_task(db, project.id, sequence)
            dims = await _video_dimensions(db, task)
            frame_count = (
                dims[2]
                if dims
                else max(
                    [frame for group in grouped.values() for frame in group["frames"]]
                    + [0]
                )
                + 1
            )
            tracks: list[ParsedVideoTrack] = []
            known_frames = {int(value) for value in source_frames.values()}
            for track_number, group in sorted(
                grouped.items(), key=lambda row: int(row[0])
            ):
                keyframes = sorted(
                    group["frames"].values(), key=lambda row: row["frame_index"]
                )
                visible = {int(row["frame_index"]) for row in keyframes}
                tracks.append(
                    ParsedVideoTrack(
                        class_name=group["class_name"],
                        track_id=_safe_track_id(
                            group["track_id"], fallback=f"mots-{track_number}"
                        ),
                        keyframes=keyframes,
                        outside=_outside_known_frames(visible, known_frames),
                        source_id=str(group["source_id"] or track_number),
                    )
                )
            items.append(
                ParsedVideoItem(
                    sequence=sequence,
                    width=width,
                    height=height,
                    tracks=tracks,
                    task=task,
                    frame_count=frame_count,
                    losses=["occlusion_lost", "frame_base_changed"],
                    frame_mapping={
                        str(key): int(value) for key, value in source_frames.items()
                    },
                    id_mapping={str(key): value for key, value in tracks_meta.items()}
                    if isinstance(tracks_meta, dict)
                    else {},
                )
            )
        return items


Parser = Any


class VideoMaskFormatAdapter:
    def __init__(self, descriptor: MaskFormatDescriptor, parser: Parser) -> None:
        self.descriptor = descriptor
        self._parser = parser

    async def _items(
        self,
        db: AsyncSession,
        project: Project,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> list[ParsedVideoItem]:
        items = await self._parser(db, project, staged, mapping, options)
        if len(items) > 100_000:
            raise ValueError("resource_budget_exceeded:max_import_items")
        return items

    async def preflight_import(
        self,
        db: AsyncSession,
        *,
        project: Project,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        items = await self._items(db, project, staged, mapping, options)
        allowed_classes = lookup_classes_for_tool_unit(
            project.tool_bindings or {}, "region"
        )
        unknown_labels: set[str] = set()
        size_conflicts: list[dict[str, Any]] = []
        plan_items: list[MaskFormatPlanItem] = []
        frame_mapping: dict[str, Any] = {}
        id_mapping: dict[str, Any] = {}
        for index, item in enumerate(items):
            skips = []
            item_unknown = sorted(
                {
                    track.class_name
                    for track in item.tracks
                    if allowed_classes and track.class_name not in allowed_classes
                }
            )
            unknown_labels.update(item_unknown)
            if item.task is None:
                skips.append(_code("task_not_found", sequence=item.sequence))
            dims = await _video_dimensions(db, item.task)
            if item.task is not None and dims is None:
                skips.append(
                    _code("image_size_mismatch", reason="video dimensions unavailable")
                )
            elif dims is not None and dims[:2] != (item.width, item.height):
                conflict = {
                    "sequence": item.sequence,
                    "expected": list(dims[:2]),
                    "observed": [item.width, item.height],
                }
                size_conflicts.append(conflict)
                skips.append(_code("image_size_mismatch", **conflict))
            if item_unknown:
                skips.append(_code("unknown_label", labels=item_unknown))
            if not item.tracks:
                skips.append(_code("not_selected"))
            for track in item.tracks:
                for keyframe in track.keyframes:
                    validate_coco_rle(keyframe["rle"])
                    frame_index = int(keyframe["frame_index"])
                    if frame_index < 0 or (
                        item.frame_count is not None and frame_index >= item.frame_count
                    ):
                        skips.append(
                            _code(
                                "frame_index_out_of_range",
                                track_id=track.track_id,
                                frame_index=keyframe["frame_index"],
                                frame_count=item.frame_count,
                            )
                        )
            loss_class = (
                "unsupported" if skips else ("lossy" if item.losses else "lossless")
            )
            losses = [_code(code) for code in item.losses]
            warnings = [_code(code, **detail) for code, detail in item.warnings]
            plan_items.append(
                MaskFormatPlanItem(
                    item_id=_item_identity(item),
                    task_id=item.task.id if item.task else None,
                    media_path=item.sequence,
                    source_index=index,
                    loss_class=loss_class,
                    estimated_objects=len(item.tracks),
                    estimated_files=sum(len(track.keyframes) for track in item.tracks),
                    estimated_bytes=sum(
                        len(keyframe["rle"]["counts"]) * 4
                        for track in item.tracks
                        for keyframe in track.keyframes
                    ),
                    losses=losses,
                    skips=skips,
                    warnings=warnings,
                )
            )
            frame_mapping[item.sequence] = item.frame_mapping
            id_mapping[item.sequence] = item.id_mapping
        return _plan(
            {
                "format_id": self.descriptor.format_id,
                "direction": "import",
                "adapter_version": self.descriptor.adapter_version,
                "manifest_version": self.descriptor.manifest_version,
                "media_type": "video",
                "staged_object_key": staged.object_key,
                "staged_sha256": staged.sha256,
                "mapping_digest": canonical_digest(mapping),
                "options_digest": canonical_digest(options),
                "items": [item.model_dump(mode="json") for item in plan_items],
                "loss_class": _worst_loss([item.loss_class for item in plan_items]),
                "unknown_labels": sorted(unknown_labels),
                "size_conflicts": size_conflicts,
                "id_mapping": id_mapping,
                "frame_mapping": frame_mapping,
                "estimated_objects": sum(item.estimated_objects for item in plan_items),
                "estimated_files": sum(item.estimated_files for item in plan_items),
                "estimated_bytes": staged.size_bytes,
                "losses": [
                    loss.model_dump(mode="json")
                    for item in plan_items
                    for loss in item.losses
                ],
                "skips": [
                    skip.model_dump(mode="json")
                    for item in plan_items
                    for skip in item.skips
                ],
                "warnings": [
                    warning.model_dump(mode="json")
                    for item in plan_items
                    for warning in item.warnings
                ],
            }
        )

    async def execute_import_item(
        self,
        db: AsyncSession,
        *,
        project: Project,
        staged: StagedObject,
        plan: MaskFormatPlan,
        item_index: int,
        operator_user_id: uuid.UUID,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        plan_item = plan.items[item_index]
        if plan_item.loss_class == "unsupported" or plan_item.task_id is None:
            return {
                "status": "skipped",
                "skip_code": plan_item.skips[0].code
                if plan_item.skips
                else "not_selected",
            }
        items = await self._items(db, project, staged, mapping, options)
        if plan_item.source_index is None or plan_item.source_index >= len(items):
            raise ValueError("format_plan_source_index_conflict")
        item = items[plan_item.source_index]
        if _item_identity(item) != plan_item.item_id:
            raise ValueError("format_plan_item_digest_conflict")
        if item.task is None or item.task.id != plan_item.task_id:
            raise ValueError("format_plan_task_conflict")
        mask_objects: dict[str, dict[str, Any]] = {}
        annotations: list[dict[str, Any]] = []
        for track in item.tracks:
            keyframes: list[dict[str, Any]] = []
            for keyframe in track.keyframes:
                reference = build_rle_reference(keyframe["rle"])
                mask_objects[reference["sha256"]] = keyframe["rle"]
                keyframes.append(
                    {
                        "frame_index": int(keyframe["frame_index"]),
                        "mask": reference,
                        "source": "manual",
                        "occluded": bool(keyframe.get("occluded")),
                    }
                )
            annotations.append(
                {
                    "geometry": {
                        "type": "video_track_mask",
                        "track_id": track.track_id,
                        "semantic_label": track.class_name,
                        "keyframes": keyframes,
                        "outside": track.outside,
                    },
                    "class_name": track.class_name,
                    "tool_unit_id": "region",
                    "source": "manual",
                    "attributes": {
                        "_import_format": self.descriptor.format_id,
                        "_import_source_id": track.source_id,
                    },
                }
            )
        payload = {
            "schema_version": "1.3",
            "mask_objects": mask_objects,
            "tasks": [
                {
                    "task_match": {"display_id": item.task.display_id},
                    "file_path": item.task.file_path,
                    "media_type": "video",
                    "annotations": annotations,
                }
            ],
        }
        result = await import_aap_json_annotations(
            db,
            project.id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            operator_user_id=operator_user_id,
            overwrite=bool(options.get("overwrite")),
            dry_run=False,
        )
        if result.errors or result.skipped:
            raise ValueError("format_execute_diverged_from_preflight")
        return {
            "status": "committed",
            "task_id": str(item.task.id),
            "imported": result.imported,
            "skipped": 0,
        }

    async def preflight_export(
        self,
        db: AsyncSession,
        *,
        project: Project,
        scope: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        base = await LegacyPackagingAdapter(self.descriptor).preflight_export(
            db,
            project=project,
            scope=scope,
            options=options,
        )
        if self.descriptor.format_id not in {"davis", "youtube-vos"}:
            return base
        video_scope = VideoExportScope.from_dict(scope.get("video_export_scope"))
        overlap_policy = str(options.get("video_overlap_policy") or "error")
        if overlap_policy not in {"error", "z_order", "larger_area", "smaller_area"}:
            raise ValueError("invalid video mask overlap policy")
        task_ids = [item.task_id for item in base.items if item.task_id is not None]
        rows = (
            (
                await db.execute(
                    select(Annotation)
                    .where(
                        Annotation.task_id.in_(task_ids),
                        Annotation.is_active.is_(True),
                        Annotation.was_cancelled.is_(False),
                        Annotation.geometry["type"].astext == "video_track_mask",
                    )
                    .order_by(Annotation.task_id, Annotation.id)
                )
            )
            .scalars()
            .all()
            if task_ids
            else []
        )
        grouped: dict[uuid.UUID, list[Annotation]] = defaultdict(list)
        for annotation in rows:
            grouped[annotation.task_id].append(annotation)
        overlap_conflicts: list[dict[str, Any]] = []
        for task_id, task_annotations in grouped.items():
            hydrated: list[tuple[int, dict[str, Any], int]] = []
            for pixel_id, annotation in enumerate(task_annotations, start=1):
                geometry = clip_video_geometry(annotation.geometry, video_scope)
                if geometry is None:
                    continue
                for keyframe in geometry.get("keyframes") or []:
                    keyframe["mask_rle"] = await load_coco_rle(
                        keyframe.get("mask") or {}
                    )
                hydrated.append((pixel_id, geometry, int(annotation.z_order or 0)))
            max_frame = max(
                (
                    int(keyframe.get("frame_index", 0))
                    for _pixel_id, geometry, _z_order in hydrated
                    for keyframe in geometry.get("keyframes") or []
                ),
                default=0,
            )
            frames = (
                range(video_scope.from_frame, video_scope.to_frame + 1)
                if video_scope is not None
                else range(max_frame + 1)
            )
            if len(frames) * max(1, len(hydrated)) > 200_000:
                raise ValueError("resource_budget_exceeded:video_overlap_frames")
            for frame in frames:
                occupied: set[int] = set()
                overlaps = 0
                for _pixel_id, geometry, _z_order in hydrated:
                    resolved = resolve_track_at_frame(geometry, frame)
                    if resolved is None or not isinstance(
                        resolved.get("mask_rle"), dict
                    ):
                        continue
                    pixels = resolved["mask_rle"]
                    _height, _width, _counts = validate_coco_rle(pixels)
                    from app.utils.raster_mask_rle import decode_coco_rle

                    current = {
                        index
                        for index, value in enumerate(decode_coco_rle(pixels))
                        if value
                    }
                    overlaps += len(occupied & current)
                    occupied.update(current)
                if overlaps:
                    overlap_conflicts.append(
                        {
                            "task_id": str(task_id),
                            "frame_index": frame,
                            "covered_pixels": overlaps,
                        }
                    )
        if not overlap_conflicts:
            return base
        payload = base.model_dump(mode="json", exclude={"plan_digest"})
        losses = list(base.losses)
        warnings = list(base.warnings)
        loss_class = base.loss_class
        if overlap_policy == "error":
            loss_class = "unsupported"
            warnings.append(
                _code("overlap_policy_required", conflicts=len(overlap_conflicts))
            )
        else:
            loss_class = "lossy"
            losses.append(
                _code(
                    "overlap_resolved",
                    overlap_policy=overlap_policy,
                    conflicts=len(overlap_conflicts),
                )
            )
        payload.update(
            {
                "loss_class": loss_class,
                "overlap_conflicts": overlap_conflicts,
                "losses": [row.model_dump(mode="json") for row in losses],
                "warnings": [row.model_dump(mode="json") for row in warnings],
            }
        )
        return _plan(payload)


VIDEO_IMPORT_PARSERS = {
    "coco-frames-seg": _parse_coco_frames,
    "davis": _parse_davis,
    "youtube-vos": _parse_youtube_vos,
    "mots": _parse_mots,
}
