"""Integer admission before source-grid, interpolation, mask or ZIP expansion."""

import math
import re
import json
from types import SimpleNamespace

from app.services.content_snapshot import ContentError
from app.services.raster_mask_storage import collect_mask_references
from app.services.video_frame_service import derive_step
from app.services.video_tracks import frame_is_outside
from app.services.prediction import to_internal_shape

MAX_TASKS = 1000
MAX_SOURCE_FRAMES = 1_000_000
MAX_SELECTED_FRAMES = 1_000_000
MAX_EVALUATIONS = 10_000_000
MAX_RESOLVE_WORK = 100_000_000
MAX_KEYFRAMES = 10_000
MAX_STRUCTURAL_ROWS = 1_000_000
MAX_VERTICES = 4_000_000
MAX_FRAME_VERTICES = 1024
MAX_PORTABLE_RUNS = 4_000_000
MAX_PORTABLE_JSON = 256 * 1024**2
MAX_MASK_BYTES = 4 * 1024**2
MAX_RASTER_PIXELS = 268_435_456
MAX_ZIP_ENTRIES = 200_000
MAX_EXPANDED_BYTES = 256 * 1024**2
SEQUENCE_TARGETS = frozenset(
    {
        "mot",
        "kitti",
        "yolo-frames-det",
        "yolo-frames-seg",
        "coco-frames-seg",
        "davis",
        "youtube-vos",
        "mots",
    }
)


def reject(name="expansion_budget"):
    raise ContentError("video_export_" + name, 422)


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        reject()
    return value


def _rows(geometry, frame_count):
    keys, outside = geometry.get("keyframes") or [], geometry.get("outside") or []
    if not isinstance(keys, list) or not isinstance(outside, list):
        reject()
    if len(keys) > MAX_KEYFRAMES or len(outside) > MAX_KEYFRAMES:
        reject("structure_budget")
    vertices, largest = 0, 0
    for row in [geometry, *keys]:
        if not isinstance(row, dict):
            reject()
        if "frame_index" in row:
            _integer(row["frame_index"], 0, frame_count - 1)
        points = row.get("points") or []
        if not isinstance(points, list) or len(points) > MAX_FRAME_VERTICES:
            reject("structure_budget")
        vertices += len(points)
        largest = max(largest, len(points))
    for row in outside:
        if not isinstance(row, dict):
            reject()
        start = _integer(row.get("from"), 0, frame_count - 1)
        _integer(row.get("to"), start, frame_count - 1)
    return (
        len(keys) + len(outside),
        vertices,
        max(1, len(keys) + len(outside) + largest**2),
    )


def _count_grid(start, end, step):
    return max(0, end // step - (start + step - 1) // step + 1)


def _frame_bytes(geometry):
    maximum, vertices = 0, 0
    for row in geometry.get("keyframes") or []:
        size = 0
        for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(
            row
        ):
            size += len(part.encode("utf-8"))
            if size > MAX_EXPANDED_BYTES:
                reject("expanded_bytes_budget")
        maximum = max(maximum, size)
        vertices = max(vertices, len(row.get("points") or []))
    # Interpolation expands tiny coordinates into full float representations;
    # indentation, field names, provenance and per-frame dictionaries add space.
    return 512 + max(maximum * 4, vertices * 64)


def check(delivery):
    """Inspect detached facts only; do not allocate a frame grid or load masks.

    A receipt for every Task must already exist. A shared physical source is
    inspected once, while the per-Task output and interpolation costs still add.
    """
    if len(delivery.tasks) > MAX_TASKS:
        reject("task_budget")
    targets = set(delivery.targets)
    options = delivery.format_options
    all_frames = options.get("video_frame_mode", "keyframes") == "all_frames"
    attributes = options.get("include_attributes", True)
    totals = dict(
        selected_frames=0,
        evaluations=0,
        resolve_work=0,
        raster_pixels=0,
        zip_entries=4 + len(targets) * 3,
        expanded_bytes=0,
    )
    structural, vertices, mask_runs, mask_json, masks = 0, 0, 0, 2, {}
    exact_work = 0
    raw_by_task = {}
    for row in delivery.annotations:
        raw_by_task.setdefault(row.task_id, []).append(row)
    if "aap_json" in targets:
        for prediction in delivery.predictions:
            for shape in prediction.result or []:
                geometry = to_internal_shape(shape).get("geometry") or {}
                raw_by_task.setdefault(prediction.task_id, []).append(
                    SimpleNamespace(geometry=geometry)
                )
    for task in delivery.tasks:
        info = delivery.video_metadata(task.id)
        count = _integer(info.get("frame_count"), 1, MAX_SOURCE_FRAMES)
        width, height = (
            _integer(info.get(name), 1, 2**31 - 1) for name in ("width", "height")
        )
        fps = info.get("fps")
        if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
            reject("metadata_invalid")
        start, end = 0, count - 1
        scope = delivery.video_scope
        if scope is not None:
            if scope.task_id != task.id:
                reject("scope_changed")
            start = _integer(scope.from_frame, 0, count - 1)
            end = _integer(scope.to_frame, start, count - 1)
        step = derive_step(fps, delivery.project.video_sampling or {})
        selected = _count_grid(start, end, step)
        annotations = delivery.canonical_annotations(task.id)
        tracks = [
            row
            for row in annotations
            if str((row.geometry or {}).get("type", "")).startswith("video_track_")
        ]
        mask_tracks = [
            row for row in tracks if row.geometry["type"] == "video_track_mask"
        ]
        exact = set()
        for row in mask_tracks if "youtube-vos" in targets else []:
            geometry = row.geometry
            exact_work += len(geometry.get("keyframes") or []) * max(
                1, len(geometry.get("outside") or [])
            )
            if exact_work > MAX_RESOLVE_WORK:
                reject("resolve_work_budget")
            for keyframe in geometry.get("keyframes") or []:
                frame = keyframe["frame_index"]
                if start <= frame <= end and not frame_is_outside(geometry, frame):
                    exact.add(frame)
        # The manifest/source contract itself emits the absolute grid. Exact
        # YouTube frames outside that grid add real entries rather than replacing it.
        totals["selected_frames"] += selected + (
            sum(frame % step != 0 for frame in exact) if "youtube-vos" in targets else 0
        )
        for row in raw_by_task.get(task.id, []):
            geometry = row.geometry or {}
            rows, points, _ = _rows(geometry, count)
            structural, vertices = structural + rows, vertices + points
            for mask in collect_mask_references(geometry):
                digest = mask.get("sha256")
                if (
                    not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    reject("mask_budget")
                facts = (
                    mask.get("runs"),
                    mask.get("bytes"),
                    tuple(mask.get("size") or []),
                )
                if digest in masks:
                    if masks[digest] != facts:
                        reject("mask_budget")
                    continue
                masks[digest] = facts
                mask_runs += _integer(mask.get("runs"), 1, 1_000_000)
                _integer(mask.get("bytes"), 1, MAX_MASK_BYTES)
                # A conservative indented uncompressed portable RLE envelope.
                mask_json += mask["runs"] * 32 + 512
                dimensions = mask.get("size")
                if not isinstance(dimensions, (list, tuple)) or len(dimensions) != 2:
                    reject("mask_budget")
                mh, mw = (_integer(value, 1, 4096) for value in dimensions)
                if mh * mw > 16_777_216:
                    reject("mask_budget")
        if structural > MAX_STRUCTURAL_ROWS or vertices > MAX_VERTICES:
            reject("structure_budget")
        if mask_runs > MAX_PORTABLE_RUNS or mask_json > MAX_PORTABLE_JSON:
            reject("portable_mask_budget")
        if targets.intersection({"davis", "youtube-vos"}) and len(mask_tracks) > 254:
            reject("mask_object_budget")
        for row in tracks:
            geometry = row.geometry
            kind = geometry["type"]
            calls = selected * len(
                targets.intersection({"mot", "kitti", "yolo-frames-det"})
            )
            if kind == "video_track_polygon":
                calls += selected * len(
                    targets.intersection({"yolo-frames-seg", "coco-frames-seg"})
                )
            if kind == "video_track_mask":
                calls += selected * len(
                    targets.intersection({"davis", "mots", "coco-frames-seg"})
                )
                if "youtube-vos" in targets:
                    calls += len(exact)
            if "video_json" in targets and all_frames:
                calls += end - start + 1
            _, _, work = _rows(geometry, count)
            if kind == "video_track_mask":
                runs = max(
                    (mask.get("runs", 0) for mask in collect_mask_references(geometry)),
                    default=0,
                )
                work += runs
            else:
                runs = 0
            totals["evaluations"] += calls
            totals["resolve_work"] += calls * work
            if calls:
                frame_bytes = _frame_bytes(geometry)
                expanded = (
                    selected
                    * 768
                    * len(targets.intersection({"mot", "kitti", "yolo-frames-det"}))
                )
                if "video_json" in targets and all_frames:
                    expanded += (end - start + 1) * frame_bytes
                if kind == "video_track_polygon":
                    expanded += (
                        selected
                        * frame_bytes
                        * len(
                            targets.intersection({"yolo-frames-seg", "coco-frames-seg"})
                        )
                    )
                elif kind == "video_track_mask":
                    expanded += (
                        selected
                        * (512 + runs * 32)
                        * len(targets.intersection({"mots", "coco-frames-seg"}))
                    )
                totals["expanded_bytes"] += expanded
        raster_frames = (selected if "davis" in targets else 0) + (
            len(exact) if "youtube-vos" in targets else 0
        )
        if raster_frames:
            if width > 4096 or height > 4096 or width * height > 16_777_216:
                reject("raster_budget")
            totals["raster_pixels"] += (
                raster_frames * width * height * (len(mask_tracks) + 1)
            )
        totals["zip_entries"] += (
            2 * ("mot" in targets) + ("kitti" in targets) + ("mots" in targets)
        )
        totals["zip_entries"] += (
            selected
            * (1 + bool(attributes))
            * len(targets.intersection({"yolo-frames-det", "yolo-frames-seg"}))
        )
        totals["zip_entries"] += raster_frames
        totals["expanded_bytes"] += selected * (
            512 * ("coco-frames-seg" in targets)
            + 128 * len(targets.intersection({"yolo-frames-det", "yolo-frames-seg"}))
        )
        for name, maximum in (
            ("selected_frames", MAX_SELECTED_FRAMES),
            ("evaluations", MAX_EVALUATIONS),
            ("resolve_work", MAX_RESOLVE_WORK),
            ("raster_pixels", MAX_RASTER_PIXELS),
            ("zip_entries", MAX_ZIP_ENTRIES),
            ("expanded_bytes", MAX_EXPANDED_BYTES),
        ):
            if totals[name] > maximum:
                reject(name + "_budget")
    return totals
