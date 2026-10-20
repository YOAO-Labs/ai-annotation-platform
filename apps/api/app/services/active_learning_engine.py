"""Deterministic detection heuristics and an independent same-size random arm."""

import hashlib
import math

from app.services.sample_evaluation_engine import (
    MAX_PREDICTIONS,
    intersection_over_union,
)
from app.services.training_dataset_bridge import content_digest, sample_identities

STRATEGY = {
    "implementation": "aap-detection-uncertainty-exploration",
    "revision": 1,
    "low_confidence": "max_one_minus_observed_box_confidence",
    "ambiguity": "max_iou_between_different_classes",
    "priority": "max_low_confidence_ambiguity",
    "exploration": "ceil_batch_size_times_0.25",
    "control": "uniform_from_score_independent_disjoint_prepartition",
    "zero_detections": "missing_signal_random_fallback",
    "confidence_semantics": "uncalibrated_detection_heuristic",
}

MASK_STRATEGY = {
    "implementation": "aap-instance-mask-uncertainty-exploration",
    "revision": 1,
    "task": "segment",
    "low_confidence": "max_one_minus_observed_instance_confidence",
    "ambiguity": "max_original_pixel_mask_iou_between_different_classes",
    "priority": "max_low_confidence_ambiguity",
    "exploration": STRATEGY["exploration"],
    "control": STRATEGY["control"],
    "zero_instances": "missing_signal_random_fallback",
    "confidence_semantics": "uncalibrated_instance_heuristic_without_pixel_probabilities",
    "maximum_overlap_run_visits": 8_000_000,
}


def strategy_for(ref):
    from aap_task_contract import normalize_task

    return (
        MASK_STRATEGY
        if normalize_task(ref.get("task", "detection")) == "segment"
        else STRATEGY
    )


def mask_features(predictions):
    from app.services.sample_evaluation_masks import mask_iou

    low = max((1 - row["score"] for row in predictions), default=None)
    visits, ambiguity = 0, 0.0
    for index, first in enumerate(predictions):
        for second in predictions[index + 1 :]:
            if first["class_index"] == second["class_index"]:
                continue
            visits += len(first["rle"]["counts"]) + len(second["rle"]["counts"])
            if visits > MASK_STRATEGY["maximum_overlap_run_visits"]:
                raise ValueError(
                    "Mask ambiguity computation exceeds the frozen run budget"
                )
            ambiguity = max(ambiguity, mask_iou(first["rle"], second["rle"]))
    return {
        "low_confidence": low,
        "ambiguity": ambiguity if predictions else None,
        "priority": max(low, ambiguity) if low is not None else None,
        "detections": len(predictions),
        "possible_truncation": len(predictions) == MAX_PREDICTIONS,
        "minimum_instance_pixels": min(
            (row["area_pixels"] for row in predictions), default=None
        ),
        "total_instance_pixels_including_overlap": sum(
            row["area_pixels"] for row in predictions
        ),
        "overlap_run_visits": visits,
    }


def prediction_features(predictions, ref):
    return (
        mask_features(predictions)
        if strategy_for(ref) == MASK_STRATEGY
        else features(predictions)
    )


def shuffled(items, seed, salt):
    return sorted(
        items,
        key=lambda item: (
            hashlib.sha256(
                f"{seed}:{salt}:{item['group_digest']}".encode()
            ).hexdigest(),
            item["task_id"],
        ),
    )


def grouped_pool(samples, excluded):
    """Propagate exclusions through all content/scene members before deduplication."""
    roots = list(range(len(samples)))

    def root(index):
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    seen = {}
    for index, sample in enumerate(samples):
        for identity in sample_identities(sample):
            if identity in seen:
                roots[root(index)] = root(seen[identity])
            seen[identity] = index
    groups = {}
    for index, sample in enumerate(samples):
        groups.setdefault(root(index), []).append(sample)
    representatives, omitted = [], []
    for group in groups.values():
        group = sorted(group, key=lambda sample: sample["task_id"])
        members = sorted(
            {identity for sample in group for identity in sample_identities(sample)}
        )
        hits = sorted(set(members) & set(excluded))
        if hits:
            omitted.extend(
                {
                    "task_id": sample["task_id"],
                    "reason": "historical_membership",
                    "identities": hits,
                }
                for sample in group
            )
            continue
        representatives.append(
            {
                **group[0],
                "group_digest": content_digest(members),
                "group_task_ids": [sample["task_id"] for sample in group],
            }
        )
        omitted.extend(
            {
                "task_id": sample["task_id"],
                "reason": "duplicate_or_scene",
                "representative_task_id": group[0]["task_id"],
            }
            for sample in group[1:]
        )
    return sorted(representatives, key=lambda sample: sample["task_id"]), omitted


def partition(samples, seed, batch_size):
    if len(samples) < 2 * batch_size:
        raise ValueError("排除与去重后须至少保留每组数量两倍的独立候选")
    ordered = shuffled(samples, seed, "partition")
    active_ids = {sample["task_id"] for sample in ordered[: (len(ordered) + 1) // 2]}
    return [
        dict(
            sample,
            ordinal=ordinal,
            arm="active" if sample["task_id"] in active_ids else "random_control",
        )
        for ordinal, sample in enumerate(samples)
    ]


def features(predictions):
    if not predictions:
        return {
            "low_confidence": None,
            "ambiguity": None,
            "priority": None,
            "detections": 0,
            "possible_truncation": False,
        }
    low = max(1 - row["score"] for row in predictions)
    ambiguity = max(
        (
            intersection_over_union(a["xyxy"], b["xyxy"])
            for i, a in enumerate(predictions)
            for b in predictions[i + 1 :]
            if a["class_index"] != b["class_index"]
        ),
        default=0.0,
    )
    return {
        "low_confidence": low,
        "ambiguity": ambiguity,
        "priority": max(low, ambiguity),
        "detections": len(predictions),
        "possible_truncation": len(predictions) == MAX_PREDICTIONS,
    }


def ranking(spec, evidence):
    samples, size, seed = spec["samples"], spec["batch_size"], spec["seed"]
    active = [sample for sample in samples if sample["arm"] == "active"]
    control = [sample for sample in samples if sample["arm"] == "random_control"]
    exploration_count = math.ceil(size * 0.25)
    exploration = shuffled(active, seed, "explore")[:exploration_count]
    chosen = {sample["ordinal"] for sample in exploration}
    scored = sorted(
        (
            sample
            for sample in active
            if sample["ordinal"] not in chosen
            and evidence[sample["ordinal"]]["features"]["priority"] is not None
        ),
        key=lambda sample: (
            -evidence[sample["ordinal"]]["features"]["priority"],
            sample["task_id"],
        ),
    )
    exploit = scored[: size - len(exploration)]
    chosen.update(sample["ordinal"] for sample in exploit)
    fallback = shuffled(
        [sample for sample in active if sample["ordinal"] not in chosen],
        seed,
        "fallback",
    )[: size - len(chosen)]
    chosen.update(sample["ordinal"] for sample in fallback)
    remainder = shuffled(
        [sample for sample in active if sample["ordinal"] not in chosen],
        seed,
        "remainder",
    )
    ordered_active = exploration + exploit + fallback + remainder
    ordered_control = shuffled(control, seed, "control")
    result = []
    for arm, ordered in (
        ("active", ordered_active),
        ("random_control", ordered_control),
    ):
        for index, sample in enumerate(ordered):
            feature = evidence[sample["ordinal"]]["features"]
            reason = (
                "random_control"
                if arm == "random_control"
                else "random_exploration"
                if sample in exploration
                else "random_fallback"
                if sample in fallback or feature["priority"] is None
                else "overlapping_classes"
                if feature["ambiguity"] > feature["low_confidence"]
                else "low_confidence"
            )
            result.append(
                {
                    "ordinal": sample["ordinal"],
                    "task_id": sample["task_id"],
                    "arm": arm,
                    "rank": index + 1,
                    "recommended": index < size,
                    "reason": reason,
                    "features": feature,
                    "evidence_digest": evidence[sample["ordinal"]]["evidence_digest"],
                }
            )
    return result
