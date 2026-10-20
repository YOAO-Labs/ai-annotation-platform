"""Original-image instance-mask evidence; fixed-threshold counts, never AP."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

from aap_protocol_v2.mask_interaction import CocoRlePayload, canonical_rle_bytes
from training_contract.model_return import (
    SEGMENTATION_OUTPUT_CONTRACT,
    segmentation_runtime,
)

from app.services import sample_evaluation_engine as detection
from app.services.training_dataset_bridge import content_digest

MAX_SAMPLES = detection.MAX_SAMPLES
MAX_TRUTH_PER_IMAGE = detection.MAX_TRUTH_PER_IMAGE
MAX_TRUTH_TOTAL = detection.MAX_TRUTH_TOTAL
MAX_PREDICTIONS = detection.MAX_PREDICTIONS
MAX_RAW_BYTES = detection.MAX_RAW_BYTES
MAX_PIXELS = min(detection.MAX_PIXELS, 128_000_000 // MAX_PREDICTIONS)
MAX_DIMENSION = 4096
MAX_RESPONSE_BYTES = 17 * 1024 * 1024
REPORT_NODE_BUDGET = detection.REPORT_NODE_BUDGET
MAX_MATCH_RUN_OPERATIONS = 20_000_000
MAX_SAMPLE_RLE_BYTES = 2 * 1024 * 1024
PREPROCESSING = detection.PREPROCESSING
EVALUATOR = {
    "implementation": "aap-instance-mask-greedy",
    "revision": 1,
    "coordinates": "original_logical_pixels",
    "decoder": "coco-rle-column-major-1",
    "polygon_rasterizer": "aap-pixel-center-even-odd-1",
    "ordering": "confidence_desc_then_prediction_index",
    "matching": "same_class_max_mask_iou_unused_truth_then_truth_index",
    "threshold_boundary": "inclusive",
    "localization_min_iou": 0.1,
    "metrics": "fixed_threshold_tp_fp_fn_precision_recall_f1_matched_iou_dice_not_ap",
    "area_groups": {"small": [0, 1024], "medium": [1024, 9216], "large": [9216, None]},
    "area_semantics": "original_foreground_pixels_truth_for_tp_fn_prediction_for_fp",
}


def mask_evidence(rle):
    payload = CocoRlePayload.model_validate(rle)
    intervals = _intervals(payload.counts)
    merged = []
    for start, end in intervals:
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    cursor, counts = 0, []
    for start, end in merged:
        counts.extend((start - cursor, end - start))
        cursor = end
    pixels = payload.size[0] * payload.size[1]
    if cursor < pixels:
        counts.append(pixels - cursor)
    payload = CocoRlePayload(size=payload.size, counts=counts or [pixels])
    raw = canonical_rle_bytes(payload)
    area = sum(payload.counts[1::2])
    if area <= 0:
        raise ValueError("Instance-mask evidence must contain foreground")
    return {
        "rle": payload.model_dump(mode="json"),
        "mask_sha256": hashlib.sha256(raw).hexdigest(),
        "area_pixels": area,
    }


def _intervals(counts):
    offset = 0
    intervals = []
    for index, count in enumerate(counts):
        if index % 2 and count:
            intervals.append((offset, offset + count))
        offset += count
    return intervals


def _intersection(left, right):
    i = j = total = 0
    while i < len(left) and j < len(right):
        a, b = left[i], right[j]
        total += max(0, min(a[1], b[1]) - max(a[0], b[0]))
        if a[1] <= b[1]:
            i += 1
        else:
            j += 1
    return total


def mask_iou(first_rle, second_rle):
    """Compare canonical original-image masks without allocating dense bitmaps."""
    first, second = mask_evidence(first_rle), mask_evidence(second_rle)
    if first["rle"]["size"] != second["rle"]["size"]:
        raise ValueError("Masks have different original image dimensions")
    if (
        len(first["rle"]["counts"]) + len(second["rle"]["counts"])
        > MAX_MATCH_RUN_OPERATIONS
    ):
        raise ValueError("Mask comparison exceeds the run-operation budget")
    intersection = _intersection(
        _intervals(first["rle"]["counts"]), _intervals(second["rle"]["counts"])
    )
    return intersection / (first["area_pixels"] + second["area_pixels"] - intersection)


def normalize_predictions(
    raw, native_to_benchmark, *, width, height, runtime_qualification=None
):
    """Freeze managed wire output; confidence filtering belongs to its strategy."""
    return predictions_from_wire(
        raw["result"],
        native_to_benchmark,
        metadata=raw["meta"],
        width=width,
        height=height,
        runtime_qualification=runtime_qualification,
    )


def checked_predictions(items, *, class_count, native_to_benchmark=None):
    """Revalidate persisted mask predictions and their semantic pixel evidence."""
    from app.schemas.sample_evaluation import MaskPrediction

    checked = [
        MaskPrediction.model_validate(item).model_dump(mode="json") for item in items
    ]
    _checked(checked, class_count, True)
    if native_to_benchmark is not None and any(
        item["native_class_index"] >= len(native_to_benchmark)
        or item["class_index"] != native_to_benchmark[item["native_class_index"]]
        for item in checked
    ):
        raise ValueError("Mask prediction native class mapping differs")
    return checked


def _checked(items, class_count, predictions):
    if len(items) > (MAX_PREDICTIONS if predictions else MAX_TRUTH_PER_IMAGE):
        raise ValueError("Instance-mask count exceeds the evaluation budget")
    total_bytes = 0
    for index, item in enumerate(items):
        expected = mask_evidence(item["rle"])
        if (
            type(item.get("index")) is not int
            or item["index"] != index
            or type(item.get("class_index")) is not int
            or not 0 <= item["class_index"] < class_count
            or type(item.get("area_pixels")) is not int
            or any(item.get(key) != value for key, value in expected.items())
        ):
            raise ValueError("Instance-mask evidence identity differs")
        if predictions and (
            type(item.get("score")) not in {int, float}
            or not math.isfinite(item["score"])
            or not 0 <= item["score"] <= 1
        ):
            raise ValueError("Instance-mask confidence is invalid")
        total_bytes += len(
            canonical_rle_bytes(CocoRlePayload.model_validate(item["rle"]))
        )
    if total_bytes > MAX_SAMPLE_RLE_BYTES:
        raise ValueError("Instance-mask evidence exceeds the sample byte budget")


def predictions_from_wire(
    items, mapping, *, metadata, width, height, runtime_qualification=None
):
    runtime = (
        segmentation_runtime("8.4.177")
        if runtime_qualification is None
        else runtime_qualification
    )
    if not isinstance(runtime, dict):
        raise ValueError("Unqualified frozen mask evaluation runtime")
    canonical = segmentation_runtime(runtime.get("ultralytics"))
    if runtime.get("ultralytics") != "8.4.177":
        if type(runtime.get("end2end")) is not bool:
            raise ValueError("Frozen mask evaluation head is unavailable")
        canonical.update(
            end2end=runtime["end2end"], nms=False if runtime["end2end"] else None
        )
    if runtime != canonical:
        raise ValueError("Unqualified frozen mask evaluation runtime")
    segmentation = metadata.get("segmentation") if isinstance(metadata, dict) else None
    if (
        not isinstance(segmentation, dict)
        or segmentation.get("dimensions") != [height, width]
        or segmentation.get("output_contract") != SEGMENTATION_OUTPUT_CONTRACT
        or any(
            segmentation.get(key) != runtime[key]
            for key in ("decoder", "mask_processing")
        )
        or segmentation.get("runtime_qualification") != runtime
    ):
        raise ValueError(
            "Mask prediction processing or dimensions differ from the frozen source"
        )
    if len(json.dumps(metadata, separators=(",", ":")).encode()) > 16 * 1024 * 1024:
        raise ValueError("Mask prediction metadata exceeds its byte budget")
    objects = metadata.get("mask_objects")
    if not isinstance(objects, dict) or len(items) > MAX_PREDICTIONS:
        raise ValueError("Mask prediction objects are unavailable or excessive")
    predictions, used = [], set()
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("geometry"), dict)
            or not isinstance(item["geometry"].get("mask"), dict)
        ):
            raise ValueError("Mask prediction geometry is unavailable")
        native = item.get("native_class_index")
        ref = (item.get("geometry") or {}).get("mask") or {}
        if (
            item.get("tool_unit_id") != "region"
            or item.get("geometry", {}).get("type") != "raster_mask"
            or type(native) is not int
            or not 0 <= native < len(mapping)
            or type(item.get("instance_index")) is not int
            or item.get("instance_index") != index
            or item.get("score") != item.get("confidence")
            or ref.get("size") != [height, width]
        ):
            raise ValueError(
                "Mask prediction class, geometry or instance alignment differs"
            )
        rle = objects.get(ref.get("sha256"))
        evidence = mask_evidence(rle)
        raw = canonical_rle_bytes(CocoRlePayload.model_validate(rle))
        sha = evidence["mask_sha256"]
        if (
            sha != ref.get("sha256")
            or len(raw) != ref.get("bytes")
            or ref.get("runs") != len(rle["counts"])
            or ref.get("object_key")
            != f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json"
            or ref.get("encoding") != "coco_rle_ref"
        ):
            raise ValueError("Mask prediction content reference differs")
        used.add(sha)
        predictions.append(
            {
                "index": index,
                "class_index": mapping[native],
                "native_class_index": native,
                "score": item.get("score"),
                **evidence,
            }
        )
    if used != set(objects):
        raise ValueError("Mask prediction has unreferenced objects")
    return checked_predictions(
        predictions, class_count=len(mapping), native_to_benchmark=mapping
    )


def evaluate_sample(truth, predictions, *, class_count, confidence, matching_iou):
    _checked(truth, class_count, False)
    _checked(predictions, class_count, True)
    for threshold in (confidence, matching_iou):
        if (
            type(threshold) not in {int, float}
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("Mask evaluation threshold is invalid")
    all_items = truth + predictions
    if len({tuple(item["rle"]["size"]) for item in all_items}) > 1:
        raise ValueError("Masks have different original image dimensions")
    if (
        sum(len(item["rle"]["counts"]) for item in truth) * len(predictions)
        + sum(len(item["rle"]["counts"]) for item in predictions) * len(truth)
        > MAX_MATCH_RUN_OPERATIONS
    ):
        raise ValueError("Mask matching exceeds the run-operation budget")
    gt_intervals = [_intervals(item["rle"]["counts"]) for item in truth]
    pred_intervals = [_intervals(item["rle"]["counts"]) for item in predictions]
    overlaps = {}
    for p, prediction in enumerate(predictions):
        for g, target in enumerate(truth):
            intersection = _intersection(pred_intervals[p], gt_intervals[g])
            total = prediction["area_pixels"] + target["area_pixels"]
            overlaps[p, g] = (
                intersection / (total - intersection),
                2 * intersection / total,
            )
    ordered = sorted(
        (p for p in predictions if p["score"] >= confidence),
        key=lambda p: (-p["score"], p["index"]),
    )
    unused, matches, fp = set(range(len(truth))), [], []
    for prediction in ordered:
        eligible = [
            (overlaps[prediction["index"], g][0], g)
            for g in unused
            if truth[g]["class_index"] == prediction["class_index"]
        ]
        best = max(eligible, key=lambda pair: (pair[0], -pair[1]), default=None)
        if best and best[0] >= matching_iou:
            g = best[1]
            unused.remove(g)
            matches.append(
                {
                    "prediction_index": prediction["index"],
                    "truth_index": g,
                    "iou": best[0],
                    "dice": overlaps[prediction["index"], g][1],
                }
            )
        else:
            fp.append(prediction["index"])
    remaining, diagnostics = unused.copy(), []
    for p in fp:
        confused = [
            (overlaps[p, g][0], g)
            for g in remaining
            if truth[g]["class_index"] != predictions[p]["class_index"]
            and overlaps[p, g][0] >= matching_iou
        ]
        localized = [
            (overlaps[p, g][0], g)
            for g in remaining
            if truth[g]["class_index"] == predictions[p]["class_index"]
            and 0.1 <= overlaps[p, g][0] < matching_iou
        ]
        best = max(
            confused or localized, key=lambda pair: (pair[0], -pair[1]), default=None
        )
        if best:
            remaining.remove(best[1])
            diagnostics.append(
                {
                    "kind": "confusion" if confused else "localization",
                    "prediction_index": p,
                    "truth_index": best[1],
                    "iou": best[0],
                    "dice": overlaps[p, best[1]][1],
                }
            )
    return {
        **detection.metrics(len(matches), len(fp), len(unused)),
        "matches": matches,
        "false_positive_indices": fp,
        "false_negative_indices": sorted(unused),
        "excluded_prediction_indices": [
            p["index"] for p in predictions if p["score"] < confidence
        ],
        "diagnostics": diagnostics,
    }


def _area_group(area):
    return "small" if area < 1024 else "medium" if area < 9216 else "large"


def aggregate(samples, classes):
    result = detection.aggregate(samples, classes)
    matches = [pair for sample in samples for pair in sample["matching"]["matches"]]
    result.update(
        matched_mean_iou=sum(p["iou"] for p in matches) / len(matches)
        if matches
        else None,
        matched_mean_dice=sum(p["dice"] for p in matches) / len(matches)
        if matches
        else None,
    )
    groups = {name: Counter(tp=0, fp=0, fn=0) for name in EVALUATOR["area_groups"]}
    for sample in samples:
        for pair in sample["matching"]["matches"]:
            groups[_area_group(sample["truth"][pair["truth_index"]]["area_pixels"])][
                "tp"
            ] += 1
        for key, source, metric in (
            ("false_positive_indices", "predictions", "fp"),
            ("false_negative_indices", "truth", "fn"),
        ):
            for index in sample["matching"][key]:
                groups[_area_group(sample[source][index]["area_pixels"])][metric] += 1
    result["area_groups"] = [
        {"group": name, **detection.metrics(**counts)}
        for name, counts in groups.items()
    ]
    return result


def benchmark_identity(samples, classes):
    return content_digest(
        {
            "schema": 2,
            "classes": classes,
            "evaluator": EVALUATOR,
            "members": sorted(
                (s["sha256"], s["label_sha256"], s["width"], s["height"])
                for s in samples
            ),
        }
    )


comparison_identity = detection.comparison_identity
normalize_image = detection.normalize_image
matches_filter = detection.matches_filter
