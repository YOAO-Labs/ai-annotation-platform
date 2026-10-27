"""Portable bitmap receipts reject corruption before native dense expansion."""

from copy import deepcopy
import json

import pytest
from training_contract.catalog import digest
from training_contract.dataset import instance_rle_evidence
from training_executor.adapters import validate_instance_rle_sample


def sample(tmp_path):
    rle = {"encoding": "coco_rle", "size": [3, 4], "counts": [0, 4, 1, 4, 2, 1]}
    item = {
        "annotation_id": "annotation",
        "annotation_version": 2,
        "class_index": 0,
        "source_geometry": "raster_mask",
        "source_object_sha256": "0" * 64,
        **instance_rle_evidence(rle),
        "rle": rle,
    }
    label = tmp_path / "labels.json"
    payload = {"schema": 1, "size": [3, 4], "instances": [item]}
    label.write_text(json.dumps(payload))
    receipt = {
        "width": 4,
        "height": 3,
        "semantic_label_sha256": digest([(0, item["pixel_sha256"])]),
        "instances": [{key: value for key, value in item.items() if key != "rle"}],
    }
    return label, payload, receipt


def test_exact_bitmap_receipt_is_accepted(tmp_path):
    label, _, receipt = sample(tmp_path)
    validate_instance_rle_sample(label, receipt, ["shape"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("class_index", True),
        ("annotation_version", 0),
        ("area_pixels", 8),
        ("pixel_sha256", "a" * 64),
        ("bbox_xyxy", [0, 0, 3, 3]),
    ],
)
def test_corrupt_or_misaligned_instance_is_rejected(tmp_path, field, value):
    label, payload, receipt = sample(tmp_path)
    payload["instances"][0][field] = value
    label.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        validate_instance_rle_sample(label, receipt, ["shape"])


def test_dimensions_and_exact_source_mapping_are_not_guessed(tmp_path):
    label, payload, receipt = sample(tmp_path)
    changed = deepcopy(receipt)
    changed["instances"][0]["annotation_version"] += 1
    with pytest.raises(ValueError, match="mapping"):
        validate_instance_rle_sample(label, changed, ["shape"])
    changed = {**receipt, "width": 5}
    with pytest.raises(ValueError, match="dimensions"):
        validate_instance_rle_sample(label, changed, ["shape"])


def test_encoded_budget_rejects_before_json_load(tmp_path, monkeypatch):
    label, _, receipt = sample(tmp_path)
    from training_executor import adapters

    monkeypatch.setitem(adapters.INSTANCE_RLE_LIMITS, "max_sample_label_bytes", 0)
    with pytest.raises(ValueError, match="budget"):
        validate_instance_rle_sample(label, receipt, ["shape"])
