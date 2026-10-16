"""Hand-checkable heuristics and deterministic independent controls."""

import uuid

import pytest

from app.services import active_learning_engine as engine


def members(count=12):
    return [
        {
            "task_id": str(uuid.UUID(int=index + 1)),
            "sha256": f"{index:064x}",
            "scene_id": None,
        }
        for index in range(count)
    ]


def test_connected_content_scene_groups_propagate_history_exclusions():
    samples = members(5)
    samples[1]["sha256"] = samples[0]["sha256"]
    samples[1]["scene_id"] = samples[2]["scene_id"] = str(uuid.uuid4())
    samples[3]["scene_id"] = samples[4]["scene_id"] = None
    chosen, omitted = engine.grouped_pool(samples, {f"task:{samples[2]['task_id']}"})
    assert [sample["task_id"] for sample in chosen] == [
        samples[3]["task_id"],
        samples[4]["task_id"],
    ]
    assert len(omitted) == 3 and all(
        row["reason"] == "historical_membership" for row in omitted
    )


def test_detection_confidence_is_missing_for_empty_and_ambiguity_is_geometric():
    assert engine.features([])["priority"] is None
    boxes = [
        {"class_index": 0, "xyxy": [0, 0, 1, 1], "score": 0.8},
        {"class_index": 1, "xyxy": [0, 0, 1, 1], "score": 0.9},
    ]
    assert engine.features(boxes)["ambiguity"] == 1
    assert engine.features(boxes)["low_confidence"] == pytest.approx(0.2)
    assert engine.features(boxes)["priority"] == 1
    assert engine.features([boxes[0]] * 100)["possible_truncation"]


def test_partition_precedes_scoring_and_control_is_disjoint_score_independent():
    samples, _ = engine.grouped_pool(members(), [])
    samples = engine.partition(samples, 42, 3)
    spec = {"samples": samples, "seed": 42, "batch_size": 3}
    evidence = {
        sample["ordinal"]: {
            "features": engine.features([]),
            "evidence_digest": "a" * 64,
        }
        for sample in samples
    }
    first = engine.ranking(spec, evidence)
    assert first == engine.ranking(spec, evidence)
    active = [row for row in first if row["arm"] == "active" and row["recommended"]]
    control = [
        row for row in first if row["arm"] == "random_control" and row["recommended"]
    ]
    assert len(active) == len(control) == 3
    assert not {row["task_id"] for row in active} & {row["task_id"] for row in control}
    assert (
        active[0]["reason"] == "random_exploration"
        and active[1]["reason"] == "random_fallback"
    )
    for item in evidence.values():
        item["features"] = engine.features(
            [{"class_index": 0, "score": 0.01, "xyxy": [0, 0, 1, 1]}]
        )
    second = engine.ranking(spec, evidence)
    assert [
        (row["ordinal"], row["rank"])
        for row in second
        if row["arm"] == "random_control"
    ] == [
        (row["ordinal"], row["rank"]) for row in first if row["arm"] == "random_control"
    ]
    with pytest.raises(ValueError, match="两倍"):
        engine.partition(samples[:5], 42, 3)
