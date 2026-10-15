"""Fixed-threshold detection evidence, independent of live project annotations."""

from __future__ import annotations

import hashlib
import io
import math
from collections import Counter

from PIL import Image, ImageOps

from app.services.training_dataset_bridge import content_digest

MAX_SAMPLES = 32
MAX_TRUTH_PER_IMAGE = 200
MAX_TRUTH_TOTAL = 2000
MAX_PREDICTIONS = 100
MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_PNG_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
REPORT_NODE_BUDGET = 500_000
EVALUATOR = {
    "implementation": "aap-detection-greedy",
    "revision": 1,
    "ordering": "confidence_desc_then_prediction_index",
    "matching": "same_class_max_iou_unused_truth_then_truth_index",
    "threshold_boundary": "inclusive",
    "diagnostics": "remaining_predictions_confidence_order_confusion_then_localization",
    "localization_min_iou": 0.1,
    "metrics": "fixed_threshold_counts_precision_recall_f1",
    "coordinate_units": "normalized_xyxy",
}
PREPROCESSING = {
    "implementation": "aap-frozen-image-rgb-png",
    "revision": 1,
    "orientation": "exif_transpose_once",
    "color": "RGB",
    "output": "PNG_without_metadata",
    "coordinates": "frozen_logical_image",
}


def _finite(value):
    try:
        valid = type(value) in {float, int} and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Detection coordinates and scores must be finite numbers")
    return float(value)


def checked_box(box):
    """Accept only normalized xyxy; clamp the protocol's documented edge tolerance."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("Detection box must contain four coordinates")
    x1, y1, x2, y2 = map(_finite, box)
    if not (-1e-7 <= x1 < x2 <= 1 + 1e-7 and -1e-7 <= y1 < y2 <= 1 + 1e-7):
        raise ValueError("Detection box is outside its logical image")
    result = [min(1.0, max(0.0, v)) for v in (x1, y1, x2, y2)]
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError("Detection box has zero area")
    return result


def truth_from_yolo(text, class_count):
    rows = []
    if type(class_count) is not int or not 1 <= class_count <= 1000:
        raise ValueError("Frozen class count is invalid")
    if not isinstance(text, str) or len(text.encode()) > 256 * 1024:
        raise ValueError("Frozen labels exceed the evaluation budget")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 5 or len(rows) >= MAX_TRUTH_PER_IMAGE:
            raise ValueError("Frozen labels have an invalid shape or too many boxes")
        try:
            index = int(parts[0])
            cx, cy, width, height = map(float, parts[1:])
        except (ValueError, OverflowError) as exc:
            raise ValueError("Frozen label values are invalid") from exc
        if str(index) != parts[0] or not 0 <= index < class_count:
            raise ValueError("Frozen label class is invalid")
        if width <= 0 or height <= 0:
            raise ValueError("Frozen label dimensions are invalid")
        rows.append(
            {
                "index": len(rows),
                "class_index": index,
                "xyxy": checked_box(
                    [cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2]
                ),
            }
        )
    return rows


def predictions_from_wire(items, native_to_benchmark):
    if not isinstance(items, list) or len(items) > MAX_PREDICTIONS:
        raise ValueError("Evaluation returned too many detections")
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "rectanglelabels":
            raise ValueError("Evaluation returned unsupported detection geometry")
        native = item.get("native_class_index")
        if type(native) is not int or not 0 <= native < len(native_to_benchmark):
            raise ValueError("Evaluation returned an invalid native class index")
        value = item.get("value", {})
        if not isinstance(value, dict):
            raise ValueError("Evaluation returned invalid detection geometry")
        x, y, width, height = [
            _finite(value.get(k)) / 100 for k in ("x", "y", "width", "height")
        ]
        score = _finite(item.get("score"))
        if not 0 <= score <= 1:
            raise ValueError("Evaluation returned an invalid confidence")
        result.append(
            {
                "index": len(result),
                "class_index": native_to_benchmark[native],
                "native_class_index": native,
                "xyxy": checked_box([x, y, x + width, y + height]),
                "score": score,
            }
        )
    return result


def intersection_over_union(a, b):
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union > 0 else 0.0


def metrics(tp, fp, fn):
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
    }


def _check_detections(items, *, predictions, class_count):
    limit = MAX_PREDICTIONS if predictions else MAX_TRUTH_PER_IMAGE
    if not isinstance(items, list) or len(items) > limit:
        raise ValueError("Detection count exceeds the evaluation budget")
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or type(item.get("index")) is not int
            or item["index"] != index
            or type(item.get("class_index")) is not int
            or not 0 <= item["class_index"] < class_count
            or checked_box(item.get("xyxy")) != item["xyxy"]
        ):
            raise ValueError("Detection evidence identity or geometry is invalid")
        if predictions and not 0 <= _finite(item.get("score")) <= 1:
            raise ValueError("Detection confidence is invalid")


def evaluate_sample(truth, predictions, *, class_count, confidence, matching_iou):
    for threshold in (confidence, matching_iou):
        if not 0 <= _finite(threshold) <= 1:
            raise ValueError("Evaluation threshold is invalid")
    _check_detections(truth, predictions=False, class_count=class_count)
    _check_detections(predictions, predictions=True, class_count=class_count)
    ordered = sorted(
        (p for p in predictions if p["score"] >= confidence),
        key=lambda p: (-p["score"], p["index"]),
    )
    unused_truth = set(range(len(truth)))
    matches, false_positives = [], []
    for prediction in ordered:
        eligible = [
            (intersection_over_union(prediction["xyxy"], truth[g]["xyxy"]), g)
            for g in unused_truth
            if truth[g]["class_index"] == prediction["class_index"]
        ]
        best = max(eligible, key=lambda pair: (pair[0], -pair[1]), default=None)
        if best and best[0] >= matching_iou:
            overlap, ground_truth = best
            unused_truth.remove(ground_truth)
            matches.append(
                {
                    "prediction_index": prediction["index"],
                    "truth_index": ground_truth,
                    "iou": overlap,
                }
            )
        else:
            false_positives.append(prediction["index"])

    diagnostic_truth = unused_truth.copy()
    diagnostics = []
    for prediction_index in false_positives:
        prediction = predictions[prediction_index]
        overlaps = [
            (intersection_over_union(prediction["xyxy"], truth[g]["xyxy"]), g)
            for g in diagnostic_truth
        ]
        confused = [
            pair
            for pair in overlaps
            if pair[0] >= matching_iou
            and truth[pair[1]]["class_index"] != prediction["class_index"]
        ]
        localized = [
            pair
            for pair in overlaps
            if 0.1 <= pair[0] < matching_iou
            and truth[pair[1]]["class_index"] == prediction["class_index"]
        ]
        eligible = confused or localized
        if eligible:
            overlap, ground_truth = max(eligible, key=lambda pair: (pair[0], -pair[1]))
            diagnostic_truth.remove(ground_truth)
            diagnostics.append(
                {
                    "kind": "confusion" if confused else "localization",
                    "prediction_index": prediction_index,
                    "truth_index": ground_truth,
                    "iou": overlap,
                }
            )
    return {
        **metrics(len(matches), len(false_positives), len(unused_truth)),
        "matches": matches,
        "false_positive_indices": false_positives,
        "false_negative_indices": sorted(unused_truth),
        "excluded_prediction_indices": [
            p["index"] for p in predictions if p["score"] < confidence
        ],
        "diagnostics": diagnostics,
    }


def aggregate(samples, classes):
    counters = [Counter(tp=0, fp=0, fn=0) for _ in classes]
    confusion = Counter()
    diagnostic_counts = Counter(confusion=0, localization=0)
    for sample in samples:
        if sample["status"] != "succeeded" or not sample.get("matching"):
            raise ValueError("Incomplete evidence cannot produce a qualified aggregate")
        truth, predictions, matching = (
            sample["truth"],
            sample["predictions"],
            sample["matching"],
        )
        for pair in matching["matches"]:
            counters[truth[pair["truth_index"]]["class_index"]]["tp"] += 1
        for index in matching["false_positive_indices"]:
            counters[predictions[index]["class_index"]]["fp"] += 1
        for index in matching["false_negative_indices"]:
            counters[truth[index]["class_index"]]["fn"] += 1
        for pair in matching["diagnostics"]:
            diagnostic_counts[pair["kind"]] += 1
            if pair["kind"] == "confusion":
                confusion[
                    (
                        truth[pair["truth_index"]]["class_index"],
                        predictions[pair["prediction_index"]]["class_index"],
                    )
                ] += 1
    total = sum(counters, Counter())
    return {
        **metrics(total["tp"], total["fp"], total["fn"]),
        "samples": len(samples),
        "per_class": [
            {"class_index": i, "name": name, **metrics(**counters[i])}
            for i, name in enumerate(classes)
        ],
        "diagnostics": dict(diagnostic_counts),
        "confusion": [
            {
                "truth_class_index": truth,
                "prediction_class_index": prediction,
                "count": count,
            }
            for (truth, prediction), count in sorted(confusion.items())
        ],
    }


def matches_filter(
    sample, *, class_index=None, error=None, confidence_min=None, confidence_max=None
):
    """Filter evidence without changing its frozen matching decisions."""
    if sample["status"] != "succeeded":
        return (
            (error is None or error == "failed" and sample["status"] == "failed")
            and class_index is None
            and confidence_min is None
            and confidence_max is None
        )
    if error == "failed":
        return False
    truth, predictions, matching = (
        sample["truth"],
        sample["predictions"],
        sample["matching"],
    )

    def prediction_ok(p):
        return (
            (class_index is None or p["class_index"] == class_index)
            and (confidence_min is None or p["score"] >= confidence_min)
            and (confidence_max is None or p["score"] <= confidence_max)
        )

    def truth_ok(g):
        return class_index is None or g["class_index"] == class_index

    if error == "false_positive":
        return any(
            prediction_ok(predictions[i]) for i in matching["false_positive_indices"]
        )
    if error == "false_negative":
        return any(truth_ok(truth[i]) for i in matching["false_negative_indices"])
    if error in {"confusion", "localization"}:
        return any(
            pair["kind"] == error
            and (
                class_index is None
                or class_index
                in {
                    truth[pair["truth_index"]]["class_index"],
                    predictions[pair["prediction_index"]]["class_index"],
                }
            )
            and (
                confidence_min is None
                or predictions[pair["prediction_index"]]["score"] >= confidence_min
            )
            and (
                confidence_max is None
                or predictions[pair["prediction_index"]]["score"] <= confidence_max
            )
            for pair in matching["diagnostics"]
        )
    if error is not None:
        raise ValueError("Unknown evaluation error filter")
    if class_index is None and confidence_min is None and confidence_max is None:
        return True
    if confidence_min is not None or confidence_max is not None:
        return any(prediction_ok(p) for p in predictions)
    return any(truth_ok(g) for g in truth) or any(prediction_ok(p) for p in predictions)


class _BoundedPNG(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_PNG_BYTES:
            raise ValueError("Normalized evaluation image exceeds its byte budget")
        return super().write(data)


def normalize_image(raw, *, width, height):
    """Derive a metadata-free image in the exact frozen logical coordinate domain."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW_BYTES:
        raise ValueError("Frozen image exceeds the evaluation byte budget")
    with Image.open(io.BytesIO(raw)) as original:
        if (
            original.width * original.height > MAX_PIXELS
            or getattr(original, "n_frames", 1) != 1
        ):
            raise ValueError("Frozen image exceeds the evaluation pixel/frame budget")
        orientation = original.getexif().get(274, 1)
        if type(orientation) is not int or not 1 <= orientation <= 8:
            raise ValueError("Frozen image orientation is invalid")
        original.load()
        with ImageOps.exif_transpose(original) as oriented:
            if oriented.size != (width, height):
                raise ValueError("Frozen logical dimensions differ from decoded image")
            with (
                oriented.convert("RGB") as rgb,
                Image.new("RGB", oriented.size) as image,
            ):
                image.paste(rgb)
                with _BoundedPNG() as target:
                    image.save(target, format="PNG", compress_level=6)
                    normalized = target.getvalue()
    return normalized, {
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "derived_sha256": hashlib.sha256(normalized).hexdigest(),
        "width": width,
        "height": height,
        "exif_orientation": orientation,
        "preprocessing_digest": content_digest(PREPROCESSING),
        "size_bytes": len(normalized),
    }


def benchmark_identity(samples, classes):
    members = Counter(
        (s["sha256"], s["label_sha256"], s["width"], s["height"]) for s in samples
    )
    return content_digest(
        {
            "schema": 1,
            "classes": classes,
            "members": [[*member, count] for member, count in sorted(members.items())],
        }
    )


def comparison_identity(spec):
    identity = {
        key: spec[key]
        for key in (
            "benchmark_digest",
            "classes",
            "role",
            "evaluator",
            "preprocessing",
            "confidence",
            "matching_iou",
            "nms_iou",
        )
    }
    identity["profile"] = {
        key: value
        for key, value in spec["profile"].items()
        if key not in {"profile_digest", "storage_identity"}
    }
    return content_digest(identity)
