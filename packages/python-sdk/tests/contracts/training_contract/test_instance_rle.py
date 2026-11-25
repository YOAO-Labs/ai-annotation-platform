"""Independent bridge identity, streaming pixel evidence and strict budgets."""

from copy import deepcopy
import hashlib
import struct

import pytest
from training_contract import definition, resolve
from training_contract.dataset import (
    DatasetContractError,
    INSTANCE_RLE_CONTRACT,
    SEGMENTATION_CONTRACT,
    data_contract,
    instance_rle_budget,
    instance_rle_evidence,
)
from training_contract.validation import (
    configuration_for_data_contract,
    verify_frozen,
    CANONICAL,
)


def test_rle_schema_does_not_reinterpret_polygon():
    assert data_contract(INSTANCE_RLE_CONTRACT) == INSTANCE_RLE_CONTRACT
    assert data_contract(SEGMENTATION_CONTRACT) == SEGMENTATION_CONTRACT
    for key in INSTANCE_RLE_CONTRACT:
        altered = {**INSTANCE_RLE_CONTRACT, key: "unknown"}
        with pytest.raises(DatasetContractError):
            data_contract(altered)


def test_pixel_identity_ignores_redundant_zero_runs_and_uses_column_major_pixels():
    a = {"encoding": "coco_rle", "size": [3, 2], "counts": [1, 3, 2]}
    b = {**a, "counts": [1, 1, 0, 2, 2]}
    first, second = instance_rle_evidence(a), instance_rle_evidence(b)
    expected = hashlib.sha256(
        struct.pack(">II", 3, 2) + bytes([0, 1, 1, 1, 0, 0])
    ).hexdigest()
    assert first["pixel_sha256"] == second["pixel_sha256"] == expected
    assert first["rle_sha256"] != second["rle_sha256"]
    assert first["area_pixels"] == 3
    assert first["bbox_xyxy"] == [0, 0, 2, 3]


@pytest.mark.parametrize(
    "args,code",
    [
        ((4001, 4000, 1), "mask_pixel_limit"),
        ((4000, 4000, 9), "mask_decode_limit"),
        ((1, 1, 301), "instance_limit"),
        ((1, 1, 1, 16 * 1024 * 1024 + 1), "mask_encoded_limit"),
        ((True, 1, 1), "mask_dimensions"),
    ],
)
def test_budgets_reject_before_any_decode(args, code):
    with pytest.raises(DatasetContractError) as exc:
        instance_rle_budget(*args)
    assert exc.value.code == code


def test_rle_config_workers_is_additive_and_historical_schema_is_unchanged():
    schema = definition("ultralytics", "yolo11n-seg", version="8.4.177")
    configuration = resolve(
        "ultralytics",
        "yolo11n-seg",
        {
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {"epochs": 2, "warmup_epochs": 0},
        },
    )
    original = deepcopy(configuration)
    raster = configuration_for_data_contract(configuration, INSTANCE_RLE_CONTRACT)
    assert configuration == original
    assert (
        configuration_for_data_contract(configuration, SEGMENTATION_CONTRACT)
        is configuration
    )
    assert raster["effective_values"]["workers"] == 0
    assert raster["identity"] == original["identity"]
    frozen = {
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "base_model": {"id": "yolo11n-seg"},
        "adapter_revision": 2,
        "configuration": raster,
        "data_contract": INSTANCE_RLE_CONTRACT,
        "parameters": {key: raster["effective_values"][key] for key in CANONICAL},
    }
    verify_frozen(frozen)
    with pytest.raises(ValueError, match="Frozen parameter"):
        verify_frozen({**frozen, "configuration": original})
