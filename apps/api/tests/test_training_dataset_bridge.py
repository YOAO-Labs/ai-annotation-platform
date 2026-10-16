"""Behavioral checks for strict packaging and leakage prevention."""

import hashlib
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
        "schema": 1,
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
        "schema": 1,
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
    parent = {
        "schema": 1,
        "samples": [{**items[2], "split": "train"}, {**items[3], "split": "val"}],
    }
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


def test_segmentation_package_binds_task_and_instance_sources_without_semantic_id_noise(
    tmp_path,
):
    import hashlib
    from training_contract.dataset import SEGMENTATION_CONTRACT

    ring = [[0.1, 0.1], [0.8, 0.1], [0.6, 0.3], [0.4, 0.8], [0.1, 0.8]]
    samples, media = [], []
    for i in range(3):
        task_id = str(uuid.uuid4())
        source = tmp_path / f"{i}.png"
        source.write_bytes(f"actual-source-{i}".encode())
        annotations = (
            []
            if i == 0
            else [
                {
                    "id": str(uuid.uuid4()),
                    "version": 4,
                    "tool_unit_id": "region",
                    "class_name": "shape",
                    "geometry": {"type": "polygon", "points": ring},
                }
            ]
        )
        samples.append({"task_id": task_id, "annotations": annotations})
        media.append(
            {
                "task_id": task_id,
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "extension": ".png",
                "width": 160,
                "height": 96,
                "size_bytes": source.stat().st_size,
            }
        )
    snapshot = {
        **SEGMENTATION_CONTRACT,
        "annotation_scope": {
            "task": "segment",
            "tool_unit_ids": ["region"],
            "classes": ["shape"],
        },
        "source": {"media_type": "image", "project_id": "frozen-source"},
        "classes": ["shape"],
        "samples": samples,
        "seed": 42,
        "validation_fraction": 0.2,
    }
    archive = tmp_path / "seg.zip"
    result = build_training_package(snapshot, media, archive)
    assert (
        result["task"] == SEGMENTATION_CONTRACT["task"]
        and result["format"] == "ultralytics-yolo"
    )
    assert result["summary"]["negatives"] == 1
    by_id = {sample["task_id"]: sample for sample in samples}
    with zipfile.ZipFile(archive) as package:
        for sample in result["samples"]:
            receipt = sample["instances"]
            source = by_id[sample["task_id"]]["annotations"]
            assert [entry["annotation_id"] for entry in receipt] == [
                entry["id"] for entry in source
            ]
            assert all(entry["annotation_version"] == 4 for entry in receipt)
            assert len(package.read(sample["label_path"]).splitlines()) == len(receipt)
    for sample in samples:
        for a in sample["annotations"]:
            a["id"] = str(uuid.uuid4())
    changed = build_training_package(snapshot, media, tmp_path / "new-ids.zip")
    assert changed["content_digest"] != result["content_digest"]
    assert changed["semantic_partitions"] == result["semantic_partitions"]
    with pytest.raises(TrainingDatasetError) as error:
        build_training_package(
            snapshot,
            media,
            tmp_path / "wrong-parent.zip",
            parent_manifest={"schema": 1, "samples": []},
        )
    assert error.value.code == "task_mismatch"


@pytest.mark.parametrize(
    "schema,content,validation,semantic_train,semantic_val",
    [
        (
            1,
            "714c6719d0db872a71f7be273ae4dca94e6b51fc11f7d81f3ee454fbd6e8224b",
            "4bafa96a7dd59d8cbed66db7b52592d925a840eacaffbf890a19de1a61be3d8e",
            "7527e0c540fb340d2fcf764a5cd9b2cc9ca905e829eea3bcf1e8c185740d7827",
            "d488615d89a9b185c782b476ae6c0e39dd31e934768e936f466a1cb89a2616ef",
        ),
        (
            2,
            "5fd83c96cca0dcef64571f56ccf0252387beb8601f50f8eeb65e6d812848d761",
            "05c1907fd044e865da2ddd6ce3c0e5e33e450b64bb966de8207ca4039c18cb56",
            "1fadd6d7019f47160f0af42ecadf25aea13b3658afd07161be9f6f2c3c8d8884",
            "093b17e8d320f25c5310634b3787c00954c521f77d80856593291365e1c487d2",
        ),
    ],
)
def test_historical_package_digest_is_unchanged(
    tmp_path, schema, content, validation, semantic_train, semantic_val
):
    # Golden outputs computed from commit 770fb03be, before canonical task metadata.
    samples, media = [], []
    for index in range(3):
        task_id = f"00000000-0000-4000-8000-{index + 1:012d}"
        raw = f"legacy-source-{index}".encode()
        path = tmp_path / f"{index}.png"
        path.write_bytes(raw)
        media.append(
            {
                "task_id": task_id,
                "path": path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "width": 160,
                "height": 96,
                "size_bytes": len(raw),
                "extension": ".png",
            }
        )
        geometry = (
            {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
            if schema == 1
            else {
                "type": "polygon",
                "points": [[0.1, 0.1], [0.8, 0.1], [0.6, 0.3], [0.4, 0.8], [0.1, 0.8]],
            }
        )
        samples.append(
            {
                "task_id": task_id,
                "scene_id": None,
                "annotations": []
                if index == 0
                else [
                    {
                        "id": f"10000000-0000-4000-8000-{index + 1:012d}",
                        "version": 4,
                        "class_name": "vehicle",
                        "tool_unit_id": "bbox" if schema == 1 else "region",
                        "geometry": geometry,
                    }
                ],
            }
        )
    snapshot = (
        {"schema": 1}
        if schema == 1
        else {
            "schema": 2,
            "task": "image-instance-segmentation",
            "geometry": "normalized-simple-polygon",
            "bridge": "ultralytics-simple-polygon-1",
        }
    )
    snapshot.update(
        classes=["vehicle"], seed=42, validation_fraction=0.2, samples=samples
    )
    manifest = build_training_package(snapshot, media, tmp_path / "legacy.zip")
    assert manifest["content_digest"] == content
    assert manifest["validation_digest"] == validation
    assert manifest["semantic_partitions"]["train_digest"] == semantic_train
    assert manifest["semantic_partitions"]["validation_digest"] == semantic_val
