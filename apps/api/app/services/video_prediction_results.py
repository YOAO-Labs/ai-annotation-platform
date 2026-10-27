"""Bound whole-video model returns before any native mask materialization."""

from bisect import bisect_left
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from types import SimpleNamespace
import uuid

from aap_inference_contract.execution import ModelTaskResult
from aap_inference.tracker import TrackerFrameResult

from app.services.content_snapshot import ContentError
from app.services.video_prediction_originals import normalize_probe
from app.services.raster_mask_storage import store_coco_rle
from app.utils.raster_mask_rle import (
    MAX_MASK_RUNS,
    MAX_VIDEO_MASK_DIMENSION,
    MAX_VIDEO_MASK_PIXELS,
    coco_rle_bbox_norm,
)

MAX_INSTANCES = 10_000
MAX_FRAME_RESULTS = 100_000
MAX_VERTICES = 1_000_000
MAX_RETAINED_RUNS = 4_000_000
MAX_MASK_ELEMENTS = 128_000_000
MAX_MASK_TRACK_FRAMES = 3000
MAX_RESPONSE_BYTES = 64 * 1024**2
MAX_RLE_BYTES = 4 * 1024**2
MAX_TOTAL_MASK_BYTES = 64 * 1024**2
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 5_000_000
_TRACK_KINDS = ("video_track_bbox", "video_track_polygon", "video_track_mask")


def reject(code="invalid"):
    raise ContentError("video_prediction_result_" + code, 502)


def _number(value, *, minimum=0, maximum=1):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and minimum <= value <= maximum
        )
    except OverflowError:
        valid = False
    if not valid:
        reject()
    return value


def _identifier(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return str(value)
    if (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return value
    reject()


def _string(value, maximum=1024):
    if not isinstance(value, str) or len(value) > maximum:
        reject()


def _wire(result):
    if isinstance(result, ModelTaskResult):
        return result.to_wire()
    if isinstance(result, dict):
        return result
    value = getattr(result, "__dict__", None)
    if not isinstance(value, dict):
        reject()
    return value


def _json_budget(value, *, maximum=None, digest=None, sort_keys=False):
    """Stream canonical JSON accounting; never allocate another response buffer."""
    maximum = MAX_RESPONSE_BYTES if maximum is None else maximum
    nodes = 0
    active = set()

    def check(member, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            reject("budget_exceeded")
        if type(member) in (dict, list, tuple):
            identity = id(member)
            if identity in active:
                reject()
            active.add(identity)
            if isinstance(member, dict):
                for key, child in member.items():
                    if not isinstance(key, str):
                        reject()
                    check(child, depth + 1)
            else:
                for child in member:
                    check(child, depth + 1)
            active.remove(identity)
        elif member is not None and type(member) not in (str, bool, int, float):
            reject()

    check(value)
    size = 0
    try:
        for fragment in json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=sort_keys,
        ).iterencode(value):
            encoded = fragment.encode()
            size += len(encoded)
            if size > maximum:
                reject("budget_exceeded")
            if digest is not None:
                digest.update(encoded)
    except ContentError:
        raise
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        reject()
    return size


@dataclass
class _Budget:
    width: int
    height: int
    frames: int
    lo: int
    hi: int
    direction: str | None
    rows: int = 0
    vertices: int = 0
    runs: int = 0
    elements: int = 0
    mask_bytes: int = 0
    reference_ids: set = field(default_factory=set)
    geometry_ids: set = field(default_factory=set)

    def frame(self, value):
        if type(value) is not int or not self.lo <= value <= self.hi:
            reject("frame_invalid")
        self.rows += 1
        if self.rows > MAX_FRAME_RESULTS:
            reject("budget_exceeded")
        return value

    def points(self, points, *, outside=False):
        if not isinstance(points, list) or (
            len(points) < 3 and not (outside and not points)
        ):
            reject()
        self.vertices += len(points)
        if self.vertices > MAX_VERTICES:
            reject("budget_exceeded")
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                reject()
            _number(point[0])
            _number(point[1])

    def mask(self, rle, *, outside=False):
        if (
            not isinstance(rle, dict)
            or set(rle) != {"encoding", "size", "counts"}
            or rle["encoding"] != "coco_rle"
        ):
            reject("mask_invalid")
        size, counts = rle["size"], rle["counts"]
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(type(value) is not int for value in size)
            or size != [self.height, self.width]
            or max(size) > MAX_VIDEO_MASK_DIMENSION
            or self.width * self.height > MAX_VIDEO_MASK_PIXELS
            or not isinstance(counts, list)
            or not 0 < len(counts) <= MAX_MASK_RUNS
        ):
            reject("mask_invalid")
        self.runs += len(counts)
        self.elements += self.width * self.height
        if self.runs > MAX_RETAINED_RUNS or self.elements > MAX_MASK_ELEMENTS:
            reject("budget_exceeded")
        total = foreground = 0
        for index, count in enumerate(counts):
            if type(count) is not int or count < 0:
                reject("mask_invalid")
            total += count
            if total > self.width * self.height:
                reject("mask_invalid")
            if index % 2:
                foreground += count
        if total != self.width * self.height or (not outside and foreground == 0):
            reject("mask_invalid")
        return foreground

    def declared_mask(self, reference):
        size = reference["size"]
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(type(value) is not int for value in size)
            or list(size) != [self.height, self.width]
            or max(size) > MAX_VIDEO_MASK_DIMENSION
            or self.width * self.height > MAX_VIDEO_MASK_PIXELS
        ):
            reject("mask_invalid")
        self.runs += reference["runs"]
        self.elements += self.width * self.height
        self.bytes(reference["bytes"])
        if self.runs > MAX_RETAINED_RUNS or self.elements > MAX_MASK_ELEMENTS:
            reject("budget_exceeded")

    def bytes(self, size):
        self.mask_bytes += size
        if self.mask_bytes > MAX_TOTAL_MASK_BYTES:
            reject("budget_exceeded")


def _bbox(value, *, outside=False):
    if not isinstance(value, dict) or not {"x", "y", "w", "h"} <= set(value):
        reject()
    x, y, width, height = (_number(value[key]) for key in ("x", "y", "w", "h"))
    if (
        x + width > 1 + 1e-6
        or y + height > 1 + 1e-6
        or (not outside and (width <= 0 or height <= 0))
    ):
        reject()


def _flags(value, *, outside_ranges=False):
    # Only an unwrapped aggregate track owns an outside range list; its full
    # interval validation belongs to _track, never to per-frame flag handling.
    keys = (
        ("primary", "occluded")
        if outside_ranges
        else ("outside", "primary", "occluded")
    )
    for key in keys:
        if key in value and type(value[key]) is not bool:
            reject()
    for key in ("confidence", "score"):
        if value.get(key) is not None:
            _number(value[key])
    if "class_name" in value:
        _string(value["class_name"])
    if value.get("attributes") is not None and not isinstance(
        value["attributes"], dict
    ):
        reject()
    if value.get("semantic_label") is not None:
        _string(value["semantic_label"])
    if "source" in value and value["source"] != "prediction":
        reject()


def _reference(value, objects, referenced, budget, *, outside=False):
    fields = {
        "encoding",
        "storage_encoding",
        "size",
        "object_key",
        "sha256",
        "runs",
        "bytes",
    }
    if (
        not isinstance(value, dict)
        or set(value) - fields
        or not (fields - {"storage_encoding"}) <= set(value)
        or value["encoding"] not in ("coco_rle_ref", "coco_rle_gzip")
        or value.get("storage_encoding") not in (None, "identity", "gzip")
        or not isinstance(value["sha256"], str)
        or len(value["sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["sha256"])
        or type(value["runs"]) is not int
        or not 0 < value["runs"] <= MAX_MASK_RUNS
        or type(value["bytes"]) is not int
        or not 0 < value["bytes"] <= MAX_RLE_BYTES
    ):
        reject("mask_invalid")
    budget.reference_ids.add(id(value))
    digest = value["sha256"]
    compressed = (
        value.get("storage_encoding") == "gzip" or value["encoding"] == "coco_rle_gzip"
    )
    key = f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json" + (
        ".gz" if compressed else ""
    )
    if value["object_key"] != key or (
        compressed and value.get("storage_encoding") == "identity"
    ):
        reject("mask_invalid")
    if objects is None:
        budget.declared_mask(value)
        referenced.add(digest)
        return
    rle = objects.get(digest)
    budget.mask(rle, outside=outside)
    # Canonical mask JSON has a fixed field order independent of backend key order.
    canonical = {"encoding": "coco_rle", "size": rle["size"], "counts": rle["counts"]}
    checksum = hashlib.sha256()
    size = _json_budget(canonical, maximum=MAX_RLE_BYTES, digest=checksum)
    budget.bytes(size)
    compressed = (
        value.get("storage_encoding") == "gzip" or value["encoding"] == "coco_rle_gzip"
    )
    key = f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json" + (
        ".gz" if compressed else ""
    )
    if (
        value["size"] != [budget.height, budget.width]
        or value["runs"] != len(rle["counts"])
        or value["bytes"] != size
        or value["object_key"] != key
        or checksum.hexdigest() != digest
    ):
        reject("mask_invalid")
    referenced.add(digest)


def _geometry(value, budget, objects, referenced, *, outside=False):
    if not isinstance(value, dict):
        reject()
    budget.geometry_ids.add(id(value))
    kind = value.get("type")
    if kind == "bbox":
        _bbox(value, outside=outside)
    elif kind == "polygon":
        budget.points(value.get("points"), outside=outside)
        if value.get("holes"):
            # Existing tracker polygon keyframes have no holes representation.
            reject()
    elif kind == "mask":
        if "rle" in value and "mask" in value:
            reject("mask_invalid")
        if "rle" in value:
            budget.mask(value["rle"], outside=outside)
            canonical = {
                "encoding": "coco_rle",
                "size": value["rle"]["size"],
                "counts": value["rle"]["counts"],
            }
            budget.bytes(_json_budget(canonical, maximum=MAX_RLE_BYTES))
        elif "mask" in value:
            _reference(value["mask"], objects, referenced, budget, outside=outside)
        else:
            reject("mask_invalid")
    else:
        reject("geometry_unsupported")
    return kind


def _track(item, budget, objects, referenced):
    geometry = item.get("geometry", item)
    if not isinstance(geometry, dict) or geometry.get("type") not in tuple(
        _TRACK_KINDS
    ):
        reject("geometry_unsupported")
    if objects is None and set(geometry) - {
        "type",
        "track_id",
        "semantic_label",
        "keyframes",
        "outside",
    }:
        reject("geometry_unsupported")
    budget.geometry_ids.add(id(geometry))
    kind = geometry["type"]
    if item.get("type") != kind or (
        "geometry" in item
        and "track_id" in item
        and item["track_id"] != geometry.get("track_id")
    ):
        reject("identity_invalid")
    if geometry.get("semantic_label") is not None:
        _string(geometry["semantic_label"])
    identifier = _identifier(geometry.get("track_id"))
    keyframes = geometry.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        reject()
    if kind == "video_track_mask" and len(keyframes) > MAX_MASK_TRACK_FRAMES:
        reject("budget_exceeded")
    previous = -1
    for keyframe in keyframes:
        if not isinstance(keyframe, dict):
            reject()
        frame = budget.frame(keyframe.get("frame_index"))
        if frame <= previous:
            reject("frame_invalid")
        previous = frame
        _flags(keyframe)
        shape_field = {
            "video_track_bbox": "bbox",
            "video_track_polygon": "points",
            "video_track_mask": "mask",
        }[kind]
        if objects is None and set(keyframe) - {
            "frame_index",
            "source",
            "occluded",
            "attributes",
            shape_field,
        }:
            reject("geometry_unsupported")
        if kind == "video_track_bbox":
            _bbox(keyframe.get("bbox"))
            if objects is None and set(keyframe["bbox"]) != {"x", "y", "w", "h"}:
                reject("geometry_unsupported")
        elif kind == "video_track_polygon":
            budget.points(keyframe.get("points"))
        else:
            _reference(keyframe.get("mask"), objects, referenced, budget)
    ranges = geometry.get("outside", [])
    if not isinstance(ranges, list) or len(ranges) > MAX_FRAME_RESULTS:
        reject()
    budget.rows += len(ranges)
    if budget.rows > MAX_FRAME_RESULTS:
        reject("budget_exceeded")
    visible_frames = [keyframe["frame_index"] for keyframe in keyframes]
    previous = -1
    for interval in ranges:
        if not isinstance(interval, dict):
            reject()
        if objects is None and set(interval) - {"from", "to", "source"}:
            reject("geometry_unsupported")
        lo, hi = interval.get("from"), interval.get("to")
        if (
            type(lo) is not int
            or type(hi) is not int
            or not budget.lo <= lo <= hi <= budget.hi
            or lo <= previous
        ):
            reject("frame_invalid")
        visible_index = bisect_left(visible_frames, lo)
        if visible_index < len(visible_frames) and visible_frames[visible_index] <= hi:
            reject("frame_invalid")
        previous = hi
        _flags(interval)
    return identifier


def _reference_scope(value, budget):
    """Shared collectors recurse through attributes too; reject hidden media."""
    if isinstance(value, dict):
        key = value.get("object_key")
        if (
            isinstance(key, str)
            and key.startswith("raster-masks/")
            and id(value) not in budget.reference_ids
        ):
            reject("mask_invalid")
        kind = value.get("type")
        if (
            kind in ("raster_mask", "video_mask", "video_track_mask", "mask")
            and "geometry" not in value
            and id(value) not in budget.geometry_ids
        ):
            reject("mask_invalid")
        for child in value.values():
            _reference_scope(child, budget)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reference_scope(child, budget)


def _validate_results(
    results, task_uuid, probe, *, window=None, direction=None, saved_refs=False
):
    """Validate raw HTTP results; window must come from frozen request context.

    Empty detections inside one matching Task envelope are valid. Both aggregate
    detector tracks and per-frame SAM2/SAM3/PVS rows are supported. No RLE decoder,
    object-store lookup, geometry remapper or persistence helper is called here.
    """
    try:
        task_uuid = uuid.UUID(str(task_uuid))
    except (ValueError, TypeError, AttributeError):
        reject()
    video = normalize_probe(probe)["video"]
    lo, hi = 0, video["frame_count"] - 1
    if window is not None:
        if (
            not isinstance(window, (tuple, list))
            or len(window) != 2
            or any(type(value) is not int for value in window)
        ):
            reject("frame_invalid")
        lo, hi = sorted(window)
        if not 0 <= lo <= hi < video["frame_count"]:
            reject("frame_invalid")
    if direction not in (None, "forward", "backward"):
        reject("frame_invalid")
    if not isinstance(results, (list, tuple)) or len(results) != 1:
        reject("task_invalid")
    for envelope in results:
        wire = _wire(envelope)
        task_id = wire.get("task_id", wire.get("task"))
        try:
            if not isinstance(task_id, str) or uuid.UUID(task_id) != task_uuid:
                reject("task_invalid")
            if "task" in wire and "task_id" in wire and wire["task"] != wire["task_id"]:
                reject("task_invalid")
        except (ValueError, TypeError, AttributeError):
            reject("task_invalid")
        rows = wire.get("result")
        meta = wire.get("meta")
        meta = {} if meta is None else meta
        if not isinstance(rows, list) or not isinstance(meta, dict):
            reject()
        if len(rows) > MAX_FRAME_RESULTS:
            reject("budget_exceeded")
        _flags(wire)
        objects = None if saved_refs else meta.get("mask_objects", {})
        if objects is not None and (
            not isinstance(objects, dict) or len(objects) > MAX_FRAME_RESULTS
        ):
            reject("mask_invalid")
        budget = _Budget(
            video["width"], video["height"], video["frame_count"], lo, hi, direction
        )
        referenced, identities, seen = set(), set(), set()
        mode = None
        previous_frame = None
        ordering = (
            -1 if direction == "backward" else 1 if direction == "forward" else None
        )
        instance_kinds, primary, labels, mask_frames = {}, {}, {}, {}
        for item in rows:
            if not isinstance(item, dict):
                reject()
            aggregate = item.get("type") in _TRACK_KINDS
            _flags(item, outside_ranges=aggregate and "geometry" not in item)
            if mode is not None and mode != aggregate:
                reject("geometry_unsupported")
            mode = aggregate
            if aggregate:
                identifier = _track(item, budget, objects, referenced)
                if identifier in identities:
                    reject("identity_invalid")
                identities.add(identifier)
            else:
                frame = budget.frame(item.get("frame_index"))
                identifier = _identifier(item.get("instance_id"), optional=True)
                identities.add(identifier)
                if (frame, identifier) in seen:
                    reject("identity_invalid")
                seen.add((frame, identifier))
                if previous_frame is not None and frame != previous_frame:
                    difference = 1 if frame > previous_frame else -1
                    if ordering is None:
                        ordering = difference
                    if difference != ordering:
                        reject("frame_invalid")
                previous_frame = frame
                outside = item.get("outside", False)
                geometry = item.get("geometry", item)
                kind = _geometry(geometry, budget, objects, referenced, outside=outside)
                if identifier in instance_kinds and instance_kinds[identifier] != kind:
                    reject("identity_invalid")
                instance_kinds[identifier] = kind
                if "primary" in item:
                    if identifier in primary and primary[identifier] != item["primary"]:
                        reject("identity_invalid")
                    primary[identifier] = item["primary"]
                if "class_name" in item:
                    if (
                        identifier in labels
                        and labels[identifier] != item["class_name"]
                    ):
                        reject("identity_invalid")
                    labels[identifier] = item["class_name"]
                if kind == "mask":
                    mask_frames[identifier] = mask_frames.get(identifier, 0) + 1
                    if mask_frames[identifier] > MAX_MASK_TRACK_FRAMES:
                        reject("budget_exceeded")
            if len(identities) > MAX_INSTANCES:
                reject("budget_exceeded")
        if sum(primary.values()) > 1 or (None in identities and len(identities) > 1):
            reject("identity_invalid")
        if objects is not None and set(objects) != referenced:
            reject("mask_invalid")
        _json_budget(wire)
        _reference_scope(wire, budget)
    return results


def validate_results(results, task_uuid, probe, *, window=None, direction=None):
    """Validate actual raw envelopes, including all portable or inline RLE bytes."""
    return _validate_results(
        results, task_uuid, probe, window=window, direction=direction
    )


def validate_saved_results(value, probe):
    """Preflight persisted shapes before reads; declarations grant no qualification.

    Callers must still load each exact owned reference and validate its actual
    bytes/RLE before issuing any source or result receipt. This helper only
    limits canonical geometry and declared aggregate memory/CPU/object budgets.
    """
    if not isinstance(value, list) or any(
        not isinstance(shape, dict)
        or not isinstance(shape.get("geometry"), dict)
        or shape["geometry"].get("type") not in _TRACK_KINDS
        for shape in value
    ):
        reject("geometry_unsupported")
    sentinel = "00000000-0000-0000-0000-000000000000"
    _validate_results(
        [{"task_id": sentinel, "result": value}], sentinel, probe, saved_refs=True
    )
    return value


_ISSUER = object()


@dataclass(frozen=True, slots=True)
class ResultQualification:
    """An issued in-process correspondence, never a source or write receipt."""

    results: tuple[ModelTaskResult, ...] = field(repr=False)
    _issuer: object = field(repr=False)
    _task_id: str
    _probe_digest: str
    _window: tuple[int, int] | None
    _direction: str
    _raw_digest: str
    _converted_digest: str


def _digest(value):
    digest = hashlib.sha256()
    _json_budget(value, digest=digest, sort_keys=True)
    return digest.hexdigest()


def _results_digest(results):
    return _digest([_wire(result) for result in results])


def _window_value(window):
    if window is not None and (
        not isinstance(window, (tuple, list))
        or len(window) != 2
        or any(type(value) is not int for value in window)
    ):
        reject("qualification_invalid")
    return tuple(sorted(window)) if window is not None else None


def validate_adaptation(
    qualified, results, task_id, probe, *, window=None, direction=None
):
    """Verify the exact server-issued conversion before the caller registers it."""
    if type(qualified) is not ResultQualification or qualified._issuer is not _ISSUER:
        reject("qualification_invalid")
    expected_direction = qualified._direction if direction is None else direction
    if (
        str(task_id) != qualified._task_id
        or _digest(normalize_probe(probe)) != qualified._probe_digest
        or _window_value(window) != qualified._window
        or expected_direction != qualified._direction
        or _results_digest(results) != qualified._converted_digest
        or _results_digest(qualified.results) != qualified._converted_digest
    ):
        reject("qualification_invalid")
    return {
        "schema": 1,
        "task_id": qualified._task_id,
        "probe_digest": qualified._probe_digest,
        "window": list(qualified._window) if qualified._window is not None else None,
        "direction": qualified._direction,
        "raw_digest": qualified._raw_digest,
        "converted_digest": qualified._converted_digest,
    }


def _frame_evidence(row):
    """Preserve observations, never duplicate geometry, RLE or arbitrary metadata."""
    return {
        "frame_index": row["frame_index"],
        "instance_id": row.get("instance_id"),
        "confidence": row.get("confidence", row.get("score")),
        "outside": row.get("outside", False),
        **({"primary": row["primary"]} if "primary" in row else {}),
        **({"class_name": row["class_name"]} if "class_name" in row else {}),
    }


async def adapt_results(results, task_uuid, probe, *, window=None, direction="forward"):
    """Validate first, then adapt real detector/SAM returns to compact tracks.

    Independent raster owners survive a failed/cancelled adaptation. The existing
    store path commits the exact object and applies its 24-hour candidate grace;
    no business transaction or unowned content is introduced here. The caller
    must invoke this before final business locks and consume the issued private
    correspondence when registering the eventual prediction result.
    """
    wires = [_wire(result) for result in results]
    validate_results(wires, task_uuid, probe, window=window, direction=direction)
    from app.services.annotation_propagation import _new_track_id
    from app.services.prediction_originals import settled
    from app.services.video_tracking.runner import apply_tracker_results
    from app.schemas._jsonb_types import (
        VideoTrackGeometry,
        VideoTrackMaskGeometry,
        VideoTrackPolygonGeometry,
    )

    raw_digest = _results_digest(wires)
    converted = [
        wire if isinstance(result, ModelTaskResult) else deepcopy(wire)
        for result, wire in zip(results, wires, strict=True)
    ]
    geometries = {
        "bbox": VideoTrackGeometry,
        "polygon": VideoTrackPolygonGeometry,
        "mask": VideoTrackMaskGeometry,
    }
    for envelope in converted:
        wire = _wire(envelope)
        rows = wire["result"]
        meta = deepcopy(wire.get("meta") or {})
        objects = meta.pop("mask_objects", {})
        groups = []
        if rows and rows[0].get("type") in _TRACK_KINDS:
            for item in rows:
                geometry = item.get("geometry", item)
                kind = {
                    "video_track_bbox": "bbox",
                    "video_track_polygon": "polygon",
                    "video_track_mask": "mask",
                }[geometry["type"]]
                source_rows = []
                for keyframe in geometry["keyframes"]:
                    shape = (
                        {"type": kind, **deepcopy(keyframe.get("bbox") or {})}
                        if kind == "bbox"
                        else {"type": "polygon", "points": deepcopy(keyframe["points"])}
                        if kind == "polygon"
                        else {"type": "mask", "mask": deepcopy(keyframe["mask"])}
                    )
                    source_rows.append(
                        {
                            "frame_index": keyframe["frame_index"],
                            "geometry": shape,
                            "confidence": keyframe.get(
                                "score", item.get("confidence", item.get("score"))
                            ),
                            "outside": False,
                            "instance_id": _identifier(geometry["track_id"]),
                            "class_name": item.get("class_name", ""),
                            "attributes": deepcopy(keyframe.get("attributes")),
                            "occluded": keyframe.get("occluded", False),
                        }
                    )
                groups.append(
                    (source_rows, deepcopy(geometry.get("outside", [])), deepcopy(item))
                )
        else:
            grouped = {}
            for row in rows:
                grouped.setdefault(
                    _identifier(row.get("instance_id"), optional=True), []
                ).append(row)
            groups = [(group, [], {}) for group in grouped.values()]
        shapes, summary = [], []
        for group, outside_ranges, original in groups:
            group.sort(key=lambda row: row["frame_index"])
            first = group[0]
            kind = first.get("geometry", first)["type"]
            instance_id = _identifier(first.get("instance_id"), optional=True)
            track_id = original.get("geometry", original).get("track_id")
            if not isinstance(track_id, str) or not track_id.startswith("trk_"):
                track_id = _new_track_id()
            target = SimpleNamespace(
                geometry={
                    "type": "video_track_" + kind,
                    "track_id": track_id,
                    "keyframes": [],
                    "outside": [],
                },
                track_id=track_id,
                version=0,
            )
            frames = []
            for row in group:
                geometry = deepcopy(row.get("geometry", row))
                frame = TrackerFrameResult(
                    frame_index=row["frame_index"],
                    geometry=geometry,
                    confidence=row.get("confidence", row.get("score")),
                    outside=row.get("outside", False),
                    instance_id=instance_id,
                    primary=row.get("primary", False),
                )
                if kind == "mask" and not frame.outside:
                    if "rle" not in geometry:
                        geometry = {
                            "type": "mask",
                            "rle": objects[geometry["mask"]["sha256"]],
                        }
                        frame = TrackerFrameResult(
                            frame.frame_index,
                            geometry,
                            frame.confidence,
                            frame.outside,
                            frame.instance_id,
                            frame.primary,
                        )
                    rle = geometry["rle"]
                    reference = await settled(store_coco_rle(rle, db=None))
                    bbox = coco_rle_bbox_norm(rle)
                    frame = replace(
                        frame,
                        geometry={"type": "mask", "mask": reference, "bbox": bbox},
                        outside=frame.outside or not bbox,
                    )
                frames.append(frame)
            apply_tracker_results(
                target,
                SimpleNamespace(from_frame=0),
                frames,
                grid_step=1,
                output_geometry=kind,
            )
            target.geometry["outside"] = sorted(
                [*target.geometry["outside"], *outside_ranges],
                key=lambda item: item["from"],
            )
            row_by_frame = {row["frame_index"]: row for row in group}
            for keyframe in target.geometry["keyframes"]:
                source_row = row_by_frame[keyframe["frame_index"]]
                keyframe["occluded"] = source_row.get("occluded", False)
                if source_row.get("attributes") is not None:
                    keyframe["attributes"] = deepcopy(source_row["attributes"])
            evidence = [_frame_evidence(row) for row in group]
            if not target.geometry["keyframes"]:
                summary.append(
                    {
                        "instance_id": instance_id,
                        "all_outside": True,
                        "frames": evidence,
                    }
                )
                continue
            geometry = (
                geometries[kind]
                .model_validate(target.geometry)
                .model_dump(mode="json", by_alias=True)
            )
            scores = [
                row["confidence"] for row in evidence if row["confidence"] is not None
            ]
            confidence = sum(scores) / len(scores) if scores else None
            shape = {
                "type": geometry["type"],
                "track_id": track_id,
                "geometry": geometry,
                "class_name": original.get(
                    "class_name",
                    next(
                        (row["class_name"] for row in group if "class_name" in row), ""
                    ),
                ),
                "confidence": original.get(
                    "confidence", original.get("score", confidence)
                ),
                "attributes": deepcopy(
                    original.get("attributes", first.get("attributes", {}))
                ),
                "tool_unit_id": "bbox" if kind == "bbox" else "region",
                "instance_id": instance_id,
                "primary": any(row.get("primary", False) for row in group),
                "tracker_frames": evidence,
            }
            semantic_label = original.get("semantic_label") or original.get(
                "geometry", {}
            ).get("semantic_label")
            if semantic_label is None and original:
                semantic_label = f"{shape['class_name'] or 'obj'}_{instance_id}"
            if semantic_label is not None:
                shape["geometry"]["semantic_label"] = semantic_label
            shapes.append(shape)
        if summary:
            meta["video_tracker_outside_instances"] = summary
        wire["result"], wire["meta"] = shapes, meta
    _json_budget([_wire(envelope) for envelope in converted])
    adapted = tuple(ModelTaskResult(**envelope) for envelope in converted)
    return ResultQualification(
        adapted,
        _ISSUER,
        str(uuid.UUID(str(task_uuid))),
        _digest(normalize_probe(probe)),
        _window_value(window),
        direction,
        raw_digest,
        _results_digest(adapted),
    )
