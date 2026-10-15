"""Behavioral checks for strict packaging and leakage prevention."""

import json
import uuid
import zipfile

import pytest

from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    assign_splits,
    build_training_package,
    detection_labels,
    split_history,
)


def annotation(**geometry):
    return {
        "class_name": "car",
        "tool_unit_id": "bbox",
        "geometry": {
            "type": "bbox",
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
            **geometry,
        },
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, True, None])
def test_rejects_invalid_bbox(value):
    with pytest.raises(TrainingDatasetError, match="矩形框"):
        detection_labels([annotation(w=value)], ["car"])


def test_does_not_drop_unknown_class_or_geometry():
    for item in [{**annotation(), "class_name": "unknown"}, annotation(type="polygon")]:
        with pytest.raises(TrainingDatasetError) as error:
            detection_labels([item], ["car"])
        assert error.value.code == "unsupported_annotation"


def sample(name, sha=None, scene=None, labels="0 0.25 0.4 0.3 0.4\n"):
    return {"task_id": name, "sha256": sha or name, "scene_id": scene, "labels": labels}


def test_content_and_scene_groups_do_not_leak_and_inheritance_is_stable():
    items = [
        sample("a", "same"),
        sample("b", "same", "scene"),
        sample("c", scene="scene"),
        sample("d"),
        sample("e"),
    ]
    splits = assign_splits(items, seed=42, validation_fraction=0.2)
    assert splits["a"] == splits["b"] == splits["c"]
    assert set(splits.values()) == {"train", "val"}
    parent = {"samples": [{**s, "split": splits[s["task_id"]]} for s in items]}
    next_splits = assign_splits(
        items + [sample("f")], seed=99, validation_fraction=0.4, parent_manifest=parent
    )
    assert all(next_splits[key] == split for key, split in splits.items())


def test_duplicate_annotations_and_historical_partition_conflicts_fail():
    with pytest.raises(TrainingDatasetError) as error:
        assign_splits(
            [sample("a", "same"), sample("b", "same", labels="")],
            seed=42,
            validation_fraction=0.2,
        )
    assert error.value.code == "duplicate_labels_conflict"
    items = [sample("a", scene="merged"), sample("b", scene="merged"), sample("c")]
    with pytest.raises(TrainingDatasetError) as error:
        assign_splits(
            items,
            seed=42,
            validation_fraction=0.2,
            parent_manifest={
                "samples": [
                    {**items[0], "split": "train"},
                    {**items[1], "split": "val"},
                ]
            },
        )
    assert error.value.code == "inherited_split_conflict"


def test_ancestral_task_content_and_scene_assignments_survive_omission():
    ancestor = {
        "samples": [
            {**sample("a", "old-content", "old-scene"), "split": "val"},
            {**sample("b"), "split": "train"},
        ]
    }
    intermediate = {"samples": [], "split_history": split_history(ancestor)}
    for returning in [
        sample("a", "changed"),
        sample("new", "old-content"),
        sample("new", scene="old-scene"),
    ]:
        splits = assign_splits(
            [returning, sample("b")],
            seed=0,
            validation_fraction=0.2,
            parent_manifest=intermediate,
        )
        assert splits[returning["task_id"]] == "val"
        assert splits["b"] == "train"


def test_training_zip_has_actual_media_and_separate_paths(tmp_path):
    ids = [str(uuid.uuid4()) for _ in range(3)]
    snapshot = {
        "classes": ["car"],
        "seed": 42,
        "validation_fraction": 0.2,
        "samples": [
            {"task_id": key, "annotations": [annotation()] if index else []}
            for index, key in enumerate(ids)
        ],
    }
    media = []
    for index, key in enumerate(ids):
        image = tmp_path / f"{key}.jpg"
        image.write_bytes(f"image {index}".encode())
        media.append(
            {
                "task_id": key,
                "path": str(image),
                "sha256": str(index),
                "width": 100,
                "height": 100,
                "size_bytes": image.stat().st_size,
                "extension": ".jpg",
            }
        )
    archive = tmp_path / "training.zip"
    manifest = build_training_package(snapshot, media, archive)
    with zipfile.ZipFile(archive) as package:
        assert "train: images/train" in package.read("data.yaml").decode()
        assert "val: images/val" in package.read("data.yaml").decode()
        assert json.loads(package.read("manifest.json")) == manifest
        for item in manifest["samples"]:
            assert package.read(item["image_path"]).startswith(b"image ")
            labels = package.read(item["label_path"])
            assert (labels == b"") == (item["task_id"] == ids[0])
    assert manifest["summary"]["negatives"] == 1
    again = build_training_package(snapshot, media, tmp_path / "again.zip")
    assert manifest == again


def test_incomplete_media_and_single_group_rejected(tmp_path):
    with pytest.raises(TrainingDatasetError) as error:
        build_training_package(
            {"classes": ["car"], "samples": [{"task_id": "a"}]}, [], tmp_path / "x.zip"
        )
    assert error.value.code == "incomplete_media"
    with pytest.raises(TrainingDatasetError) as error:
        assign_splits(
            [sample("a", "same"), sample("b", "same")], seed=42, validation_fraction=0.2
        )
    assert error.value.code == "insufficient_groups"


def test_automatic_dedup_keeps_scene_grouping_and_all_split_history(tmp_path):
    items = [
        sample("a", "same"),
        sample("b", "same", "linked"),
        sample("c", "other", "linked"),
        sample("d", "val"),
    ]
    media = []
    for s in items:
        p = tmp_path / (s["task_id"] + ".png")
        p.write_bytes(s["sha256"].encode())
        media.append(
            {
                **s,
                "path": str(p),
                "extension": ".png",
                "width": 10,
                "height": 10,
                "size_bytes": p.stat().st_size,
            }
        )
    snapshot = {
        "classes": ["car"],
        "seed": 42,
        "validation_fraction": 0.2,
        "deduplicate": True,
        "samples": [
            {
                "task_id": s["task_id"],
                "scene_id": s["scene_id"],
                "annotations": [annotation()],
            }
            for s in items
        ],
    }
    parent = {"samples": [{**items[2], "split": "train"}, {**items[3], "split": "val"}]}
    m = build_training_package(
        snapshot, media, tmp_path / "auto.zip", parent_manifest=parent
    )
    assert len(m["samples"]) == 3
    assert {s["split"] for s in m["samples"] if s["sha256"] in {"same", "other"}} == {
        "train"
    }
    assert (
        m["split_history"]["task:b"] == "train"
        and m["split_history"]["scene:linked"] == "train"
    )
    with zipfile.ZipFile(tmp_path / "auto.zip") as z:
        assert len([n for n in z.namelist() if n.startswith("images/")]) == 3
