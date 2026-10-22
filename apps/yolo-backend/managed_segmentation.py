"""Qualified original-image instance masks; contours are never authoritative."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import numpy as np
from aap_protocol_v2.mask_interaction import (
    CocoRlePayload,
    MAX_MASK_DIMENSION,
    MAX_MASK_PIXELS,
    MAX_MASK_RESPONSE_BYTES,
    MAX_MASK_RUNS,
    canonical_rle_bytes,
)
from training_contract.model_return import (
    SEGMENTATION_OUTPUT_CONTRACT,
    segmentation_identity,
    segmentation_runtime,
)

MAX_MASK_ELEMENTS = 128_000_000
MAX_INSTANCES = 300
FIXED_OPTIONS = {
    "retina_masks": True,
    "imgsz": 640,
    "augment": False,
    "half": False,
    "rect": True,
    "agnostic_nms": False,
    "save": False,
}


def verify_runtime(framework_version=None):
    """Check the actual imported code, including letterbox/crop helpers."""
    actual_version = importlib.import_module("ultralytics").__version__
    runtime = segmentation_runtime(framework_version or actual_version)
    for name in ("ultralytics", "torch", "torchvision"):
        if importlib.import_module(name).__version__ != runtime[name]:
            raise ValueError("Unqualified segmentation inference runtime")
    for name, expected in runtime["source_sha256"].items():
        source = Path(importlib.import_module(name).__file__).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise ValueError("Segmentation postprocessing source identity changed")


def validate_receipt(receipt):
    expected = segmentation_identity(
        f"{receipt.get('model_family')}{receipt.get('model_size')}-seg",
        framework_version=receipt.get("framework_version"),
    )
    if (
        receipt.get("task") != "segmentation"
        or receipt.get("framework") != "ultralytics"
        or any(receipt.get(key) != value for key, value in expected.items())
        or receipt.get("artifact_format") is not None
        or receipt.get("artifact_size_bytes") is not None
    ):
        raise ValueError("Unqualified segmentation receipt")


def validate_budget(width, height, max_det):
    # Check the worst case before native postprocessing allocates the NHW stack.
    if (
        type(width) is not int
        or type(height) is not int
        or not 0 < width <= MAX_MASK_DIMENSION
        or not 0 < height <= MAX_MASK_DIMENSION
        or width * height > MAX_MASK_PIXELS
        or type(max_det) is not int
        or not 0 < max_det <= MAX_INSTANCES
        or width * height * max_det > MAX_MASK_ELEMENTS
    ):
        raise ValueError("Original-size segmentation masks exceed the inference budget")


def _numpy(value):
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def _rle(mask):
    # NumPy transition detection avoids a Python loop over every original pixel.
    column_major = np.asarray(mask, dtype=np.bool_).ravel(order="F")
    transitions = np.flatnonzero(column_major[1:] != column_major[:-1]) + 1
    if len(transitions) + 1 + bool(column_major[0]) > MAX_MASK_RUNS:
        raise ValueError("Mask RLE exceeds the run budget")
    boundaries = np.concatenate(([0], transitions, [column_major.size]))
    counts = np.diff(boundaries).astype(int).tolist()
    if column_major[0]:
        counts.insert(0, 0)
    return CocoRlePayload(size=list(mask.shape), counts=counts)


def emit_instances(result, receipt, width, height, max_det):
    """Strictly align native boxes/classes/scores and authoritative binary masks."""
    validate_receipt(receipt)
    validate_budget(width, height, max_det)
    if tuple(result.orig_shape) != (height, width):
        raise ValueError("Segmentation result dimensions differ from the source")
    classes = receipt["classes"]
    names = result.names
    if (
        not isinstance(names, dict)
        or any(type(key) is not int for key in names)
        or set(names) != set(range(len(classes)))
        or [names[index] for index in range(len(classes))] != classes
    ):
        raise ValueError("Segmentation native class order differs from the receipt")
    boxes = result.boxes
    if boxes is None:
        raise ValueError("Segmentation result has no aligned box metadata")
    xyxy, scores, indexes = (
        _numpy(getattr(boxes, key)) for key in ("xyxy", "conf", "cls")
    )
    count = len(scores)
    if (
        xyxy.shape != (count, 4)
        or scores.shape != (count,)
        or indexes.shape != (count,)
        or count > max_det
        or not all(np.isfinite(value).all() for value in (xyxy, scores, indexes))
        or not ((scores >= 0) & (scores <= 1)).all()
        or not ((indexes >= 0) & (indexes < len(classes))).all()
        or not (indexes == np.floor(indexes)).all()
    ):
        raise ValueError(
            "Segmentation boxes, scores and classes are invalid or misaligned"
        )
    if count and (
        not ((xyxy[:, :2] >= 0).all() and (xyxy[:, 2:] > xyxy[:, :2]).all())
        or not (xyxy[:, [0, 2]] <= width).all()
        or not (xyxy[:, [1, 3]] <= height).all()
    ):
        raise ValueError("Segmentation boxes exceed the original image")
    masks = getattr(result, "masks", None)
    raw_masks = None if masks is None else masks.data
    if raw_masks is None:
        if count:
            raise ValueError("Segmentation result is missing instance masks")
    elif tuple(raw_masks.shape) != (count, height, width):
        raise ValueError("Segmentation mask count or dimensions are misaligned")
    items, objects, encoded_bytes = [], {}, 0
    for index in range(count):
        # Copy one mask at a time from GPU; do not duplicate the whole NHW stack.
        mask = _numpy(raw_masks[index])
        if (
            not np.isfinite(mask).all()
            or not ((mask == 0) | (mask == 1)).all()
            or not mask.any()
        ):
            raise ValueError("Segmentation masks must contain finite binary foreground")
        rle = _rle(mask)
        data = canonical_rle_bytes(rle)
        sha = hashlib.sha256(data).hexdigest()
        if sha not in objects:
            encoded_bytes += len(data)
            if encoded_bytes > MAX_MASK_RESPONSE_BYTES:
                raise ValueError("Segmentation RLE response exceeds the byte budget")
            objects[sha] = rle.model_dump(mode="json")
        reference = {
            "encoding": "coco_rle_ref",
            "size": [height, width],
            "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
            "sha256": sha,
            "runs": len(rle.counts),
            "bytes": len(data),
        }
        native_index, score = int(indexes[index]), float(scores[index])
        items.append(
            {
                "tool_unit_id": "region",
                "class_name": classes[native_index],
                "geometry": {"type": "raster_mask", "mask": reference},
                "native_class_index": native_index,
                "instance_index": index,
                "score": score,
                "confidence": score,
                "bbox_xyxy_pixels": xyxy[index].tolist(),
            }
        )
    runtime = receipt.get("inference_runtime", {}).get(
        "qualification", receipt["runtime_qualification"]
    )
    metadata = {
        "mask_objects": objects,
        "segmentation": {
            "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
            "dimensions": [height, width],
            "mask_processing": runtime["mask_processing"],
            "decoder": runtime["decoder"],
            "runtime_qualification": runtime,
        },
    }
    # This includes key/dictionary overhead and source identity, not only RLE bodies.
    if (
        len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_MASK_RESPONSE_BYTES
    ):
        raise ValueError("Segmentation metadata exceeds the byte budget")
    return items, metadata
