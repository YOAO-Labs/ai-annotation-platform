"""Frozen lineage changes raw package identity without changing training semantics."""

from copy import deepcopy
import hashlib
import json
import uuid
import zipfile

from PIL import Image
import pytest

from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    build_training_package,
    content_digest,
)
from training_contract.dataset import INSTANCE_RLE_CONTRACT, SEGMENTATION_CONTRACT
from training_contract.lineage import SOURCE_LINEAGE_CONTRACT


def source_lineage(index):
    return {
        "source_id": str(uuid.UUID(int=100 + index)),
        "location_id": str(uuid.UUID(int=200 + index)),
        "path_digest": hashlib.sha256(f"source-{index}".encode()).hexdigest(),
        "scan_run_id": str(uuid.UUID(int=300 + index)),
        "accepted_revision_id": str(uuid.UUID(int=400 + index)),
        "revision_number": 1,
        "previous_revision_id": None,
        "accepted_at": "2026-10-24T02:00:00+00:00",
        "scan_completed_at": "2026-10-24T01:00:00+00:00",
        "content_verified_at": "2026-10-24T01:30:00+00:00",
        "source_config_revision": 1,
        "location_config_revision": 2,
    }


def inputs(tmp_path, contract):
    media, sources = [], []
    for index in range(1, 4):
        task_id = str(uuid.UUID(int=index))
        path = tmp_path / f"{index}.png"
        with Image.new("RGB", (4, 3), (index * 20, 80, 100)) as image:
            image.save(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        media.append(
            {
                "task_id": task_id,
                "path": str(path),
                "sha256": digest,
                "extension": ".png",
                "width": 4,
                "height": 3,
                "size_bytes": path.stat().st_size,
            }
        )
        sources.append(
            {
                "task_id": task_id,
                "annotations": [],
                "asset_reference": {
                    "schema": 1,
                    "dataset_id": str(uuid.UUID(int=800)),
                    "dataset_item_id": str(uuid.UUID(int=500 + index)),
                    "asset_id": str(uuid.UUID(int=600 + index)),
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                },
            }
        )
    snapshot = {
        **contract,
        "classes": ["car"],
        "seed": 42,
        "validation_fraction": 0.2,
        "source": {"media_type": "image"},
        "annotation_scope": {"tool_unit_ids": ["region"]},
        "asset_source_contract": {
            "schema": 1,
            "reader": "verified-asset-reference-1",
            "freeze": "training-copy",
        },
        "samples": sources,
    }
    return snapshot, media


@pytest.mark.parametrize(
    "contract",
    [{"schema": 1}, SEGMENTATION_CONTRACT, INSTANCE_RLE_CONTRACT],
    ids=["detection", "polygon", "instance-rle"],
)
def test_all_package_bridges_copy_lineage_before_digesting(tmp_path, contract):
    snapshot, media = inputs(tmp_path, contract)
    legacy = build_training_package(snapshot, media, tmp_path / "legacy.zip")
    assert "source_lineage_contract" not in legacy
    assert all("source_lineage" not in member for member in legacy["samples"])
    legacy_copy = deepcopy(legacy)
    assert legacy == build_training_package(
        snapshot, media, tmp_path / "legacy-again.zip"
    )

    snapshot["source_lineage_contract"] = dict(SOURCE_LINEAGE_CONTRACT)
    for index, sample in enumerate(snapshot["samples"], 1):
        sample["source_lineage"] = source_lineage(index)
    path = tmp_path / "lineage.zip"
    manifest = build_training_package(snapshot, media, path)
    assert manifest["source_lineage_contract"] == SOURCE_LINEAGE_CONTRACT
    by_task = {sample["task_id"]: sample for sample in snapshot["samples"]}
    with zipfile.ZipFile(path) as package:
        assert json.loads(package.read("manifest.json")) == manifest
        for member in manifest["samples"]:
            assert (
                member["source_lineage"] == by_task[member["task_id"]]["source_lineage"]
            )
            assert (
                member["asset_reference"]
                == by_task[member["task_id"]]["asset_reference"]
            )
    assert manifest["semantic_partitions"] == legacy["semantic_partitions"]
    assert manifest["validation_digest"] != legacy["validation_digest"]
    assert manifest["source_digest"] == content_digest(snapshot)
    assert manifest["source_digest"] != legacy["source_digest"]
    assert manifest["content_digest"] != legacy["content_digest"]
    assert legacy == legacy_copy

    previous = deepcopy(manifest)
    member = next(member for member in manifest["samples"] if member["split"] == "val")
    by_task[member["task_id"]]["source_lineage"]["location_id"] = str(
        uuid.UUID(int=999)
    )
    updated = build_training_package(snapshot, media, tmp_path / "changed-source.zip")
    assert manifest == previous
    assert updated["semantic_partitions"] == manifest["semantic_partitions"]
    assert updated["content_digest"] != manifest["content_digest"]
    assert updated["validation_digest"] != manifest["validation_digest"]


@pytest.mark.parametrize("failure", ["schema", "unsafe", "orphan", "omitted"])
def test_package_cannot_publish_unknown_or_incomplete_lineage(tmp_path, failure):
    snapshot, media = inputs(tmp_path, {"schema": 1})
    snapshot["source_lineage_contract"] = dict(SOURCE_LINEAGE_CONTRACT)
    for index, sample in enumerate(snapshot["samples"], 1):
        sample["source_lineage"] = source_lineage(index)
    if failure == "schema":
        snapshot["source_lineage_contract"]["schema"] = 2
    elif failure == "unsafe":
        snapshot["samples"][0]["source_lineage"]["root"] = "/host/private"
    elif failure == "orphan":
        del snapshot["source_lineage_contract"]
    else:
        del snapshot["samples"][0]["source_lineage"]
    path = tmp_path / "rejected.zip"
    with pytest.raises(TrainingDatasetError) as error:
        build_training_package(snapshot, media, path)
    assert error.value.code in {
        "unknown_source_lineage_contract",
        "invalid_source_lineage",
    }
    assert not path.exists()
