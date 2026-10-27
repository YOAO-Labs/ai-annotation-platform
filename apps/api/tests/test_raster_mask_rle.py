import json
from pathlib import Path

import pytest

from app.utils.raster_mask_rle import (
    MAX_IMAGE_MASK_DIMENSION,
    MAX_IMAGE_MASK_PIXELS,
    decode_coco_rle,
    encode_coco_rle,
    validate_coco_rle,
    coco_rle_area,
    coco_rle_bbox_norm,
    coco_rle_bbox_pixels,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "apps/web/src/__fixtures__/rasterMaskRle.json"
)
FIXTURE = json.loads(FIXTURE_PATH.read_text())


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda case: case["name"])
def test_api_coco_rle_golden_cases(case):
    pixels = case["pixels_row_major"]
    rle = encode_coco_rle(pixels, case["width"], case["height"])
    assert rle["counts"] == case["counts"]
    assert list(decode_coco_rle(rle)) == pixels


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda case: case["name"])
def test_pixel_bounds_match_dense_golden_foreground(case):
    width, height = case["width"], case["height"]
    pixels = case["pixels_row_major"]
    points = [
        (index % width, index // width) for index, pixel in enumerate(pixels) if pixel
    ]
    expected = (
        (
            min(x for x, _ in points),
            min(y for _, y in points),
            max(x for x, _ in points) + 1,
            max(y for _, y in points) + 1,
        )
        if points
        else None
    )
    assert coco_rle_bbox_pixels(encode_coco_rle(pixels, width, height)) == expected


@pytest.mark.parametrize("case", FIXTURE["invalid"], ids=lambda case: case["name"])
def test_api_coco_rle_rejects_invalid_cases(case):
    with pytest.raises(ValueError):
        validate_coco_rle(
            {"encoding": "coco_rle", "size": case["size"], "counts": case["counts"]}
        )


def test_coco_rle_bbox_norm_is_tight_and_empty_safe():
    # row-major pixels: [0,1,0, 0,1,0] -> vertical 1x2 column at x=1.
    rle = encode_coco_rle([0, 1, 0, 0, 1, 0], 3, 2)
    assert coco_rle_bbox_norm(rle) == {"x": 1 / 3, "y": 0, "w": 1 / 3, "h": 1}
    assert coco_rle_bbox_norm(encode_coco_rle([0] * 6, 3, 2)) == {}
    assert coco_rle_area(rle) == 2
    assert coco_rle_area(encode_coco_rle([0] * 6, 3, 2)) == 0


def test_canonical_rle_accepts_8k_image_envelope_without_materializing_pixels():
    rle = {
        "encoding": "coco_rle",
        "size": [MAX_IMAGE_MASK_DIMENSION, MAX_IMAGE_MASK_DIMENSION],
        "counts": [MAX_IMAGE_MASK_PIXELS],
    }

    assert validate_coco_rle(rle) == (
        MAX_IMAGE_MASK_DIMENSION,
        MAX_IMAGE_MASK_DIMENSION,
        [MAX_IMAGE_MASK_PIXELS],
    )
    with pytest.raises(ValueError, match="dimensions"):
        validate_coco_rle(
            {
                "encoding": "coco_rle",
                "size": [MAX_IMAGE_MASK_DIMENSION + 1, 1],
                "counts": [MAX_IMAGE_MASK_DIMENSION + 1],
            }
        )
