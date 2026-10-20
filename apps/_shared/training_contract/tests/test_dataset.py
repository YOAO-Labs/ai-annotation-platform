"""Lossless polygon identity, bounded native conversion and task separation."""

from copy import deepcopy
import math

import pytest

from training_contract import definition, resolve
from training_contract.catalog import supported_identities
from training_contract.dataset import (
    DatasetContractError,
    SEGMENTATION,
    SEGMENTATION_CONTRACT,
    data_contract,
    polygon_points,
    segmentation_labels,
)
from training_contract.model_return import registration_eligibility

SQUARE = [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]]
CONCAVE = [[0.1, 0.1], [0.8, 0.1], [0.8, 0.3], [0.4, 0.3], [0.4, 0.8], [0.1, 0.8]]


def instance(points=None, **values):
    return {
        "id": "annotation-1",
        "version": 7,
        "class_name": "shape",
        "tool_unit_id": "region",
        "geometry": {
            "type": "polygon",
            "points": deepcopy(SQUARE if points is None else points),
        },
        **values,
    }


def test_closure_rotation_direction_and_vertex_precision_are_lossless():
    original = CONCAVE + [[0.1, 0.1]]
    canonical, native = polygon_points(original)
    assert len(canonical) == 6 and len(native) == 6
    assert set(canonical) == {tuple(p) for p in CONCAVE}
    for ring in [CONCAVE, CONCAVE[2:] + CONCAVE[:2], list(reversed(CONCAVE))]:
        assert polygon_points(ring)[0] == canonical
    precise = [[0.12345678901234567, 0.2], [0.8, 0.2], [0.8, 0.7]]
    text, mapping = segmentation_labels([instance(precise)], ["shape"])
    numbers = list(map(float, text.split()[1:]))
    assert set(zip(numbers[::2], numbers[1::2], strict=True)) == {
        tuple(p) for p in precise
    }
    assert mapping[0]["annotation_id"] == "annotation-1"
    assert mapping[0]["annotation_version"] == 7
    assert mapping[0]["line"] == 1


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [1, 1], [0, 1], [1, 0]],
        [[0, 0], [0.5, 0.5], [1, 1]],
        [[0, 0], [1, 0], [0.5, 0], [0.5, 1]],
        [[0, 0], [1, 0], [1, 1], [0.5, 0], [0, 1]],
        [[0, 0], [1, 0], [1, 1], [1, 0], [0, 1]],
        [[0, 0], [1, 0]],
        [[0.1, 0.1], [0.10000000001, 0.1], [0.8, 0.8]],
    ],
)
def test_invalid_or_native_collapsed_geometry_rejects(points):
    with pytest.raises(DatasetContractError):
        segmentation_labels([instance(points)], ["shape"])


@pytest.mark.parametrize(
    "value", [True, None, "0.1", float("nan"), float("inf"), -0.001, 1.001, 10**1000]
)
def test_coordinates_reject_nonfinite_or_outside_values(value):
    points = deepcopy(SQUARE)
    points[0][0] = value
    with pytest.raises(DatasetContractError):
        polygon_points(points)


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "polygon", "points": SQUARE, "holes": [SQUARE]},
        {"type": "multi_polygon", "polygons": [{"points": SQUARE}]},
        {"type": "raster_mask", "mask": {}},
        {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.7, "h": 0.7},
        {"type": "polygon", "points": SQUARE, "approximate": True},
    ],
)
def test_no_approximate_geometry_or_silent_negative(geometry):
    with pytest.raises(DatasetContractError) as error:
        segmentation_labels([instance(geometry=geometry)], ["shape"])
    assert error.value.code == "unsupported_annotation"


def test_same_box_different_polygons_keep_instances_but_duplicate_native_rings_reject():
    text, mapping = segmentation_labels(
        [instance(), instance(CONCAVE, id="annotation-2")], ["shape"]
    )
    assert len(text.splitlines()) == len(mapping) == 2
    assert {row["annotation_id"] for row in mapping} == {"annotation-1", "annotation-2"}
    for ring in [SQUARE, SQUARE[1:] + SQUARE[:1], list(reversed(SQUARE))]:
        with pytest.raises(DatasetContractError) as error:
            segmentation_labels([instance(), instance(ring, id="other")], ["shape"])
        assert error.value.code == "duplicate_instance"
    shifted = [[x + 1e-10, y + 1e-10] for x, y in SQUARE]
    with pytest.raises(DatasetContractError) as error:
        segmentation_labels([instance(), instance(shifted, id="other")], ["shape"])
    assert error.value.code == "duplicate_instance"


def test_empty_labels_are_explicit_and_mixed_tools_and_unknown_classes_reject():
    assert segmentation_labels([], ["shape"]) == ("", [])
    for changed in [
        instance(class_name="missing"),
        instance(tool_unit_id="bbox"),
        instance(id=None),
        instance(version=True),
    ]:
        with pytest.raises(DatasetContractError):
            segmentation_labels([changed], ["shape"])


def test_polygon_limits_are_checked_before_unbounded_work():
    ring = [
        [
            0.5 + 0.4 * math.cos(i / 513 * 2 * math.pi),
            0.5 + 0.4 * math.sin(i / 513 * 2 * math.pi),
        ]
        for i in range(513)
    ]
    with pytest.raises(DatasetContractError, match="512"):
        polygon_points(ring)
    with pytest.raises(DatasetContractError, match="300"):
        segmentation_labels([instance(id=str(i)) for i in range(301)], ["shape"])


def test_data_contract_cannot_reinterpret_legacy_or_unknown_identity():
    assert data_contract({"schema": 1}) == {"schema": 1, "task": "image-detection"}
    assert data_contract(SEGMENTATION_CONTRACT) == SEGMENTATION_CONTRACT
    for changed in [
        {"schema": 1, "task": SEGMENTATION},
        {"schema": 1, "format": "ultralytics-segmentation"},
        {**SEGMENTATION_CONTRACT, "bridge": "other"},
        {**SEGMENTATION_CONTRACT, "geometry": "raster"},
        {**SEGMENTATION_CONTRACT, "schema": True},
        {"schema": 3},
    ]:
        with pytest.raises(DatasetContractError):
            data_contract(changed)


def test_detection_schema_digests_are_unchanged_and_segmentation_has_no_legacy_alias():
    assert (
        definition("ultralytics", legacy=True)["schema_digest"]
        == "4230ae95a885793c7db77ee4daa69b6d5c32c33d28ff37ea908b077cb0c54d7d"
    )
    assert (
        definition("libreyolo", legacy=True)["schema_digest"]
        == "fa3a9697401df6ebb6b34929b40b620a6274ce1a50e76e56b5337fdcf22cb5ed"
    )
    schema = definition("ultralytics", "yolo11n-seg")
    assert schema["task"] == SEGMENTATION
    identities = [
        i
        for i in supported_identities({"ultralytics": "8.4.177"})
        if i["task"] == SEGMENTATION
    ]
    assert len(identities) == 2 and all(
        identity["adapter_revision"] == 2 for identity in identities
    )
    with pytest.raises(ValueError):
        definition("ultralytics", "yolo11s-seg", version="8.4.177")
    request = {
        "schema_id": schema["schema_id"],
        "schema_digest": schema["schema_digest"],
        "values": {},
    }
    resolved = resolve("ultralytics", "yolo11n-seg", request)
    effective = resolved["effective_values"]
    assert effective["task"] == "segment" and effective["mask_ratio"] == 1
    assert effective["overlap_mask"] is False and effective["copy_paste"] == 0
    assert effective["mosaic"] == effective["translate"] == 0
    for key in [
        "overlap_mask",
        "mask_ratio",
        "copy_paste",
        "mosaic",
        "translate",
        "flipud",
    ]:
        with pytest.raises(ValueError):
            resolve(
                "ultralytics",
                "yolo11n-seg",
                {**request, "values": {key: effective[key]}},
            )
    assert not registration_eligibility(
        {
            "framework": "ultralytics",
            "configuration": resolved,
            "base_model": {"id": "yolo11n-seg"},
        }
    )["qualified"]
