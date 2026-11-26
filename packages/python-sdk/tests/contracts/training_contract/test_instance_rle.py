"""Independent bridge identity, streaming pixel evidence and strict budgets."""

import hashlib
import struct

import pytest

from training_contract.dataset import (
    DatasetContractError,
    INSTANCE_RLE_CONTRACT,
    SEGMENTATION_CONTRACT,
    data_contract,
    instance_rle_budget,
    instance_rle_evidence,
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
