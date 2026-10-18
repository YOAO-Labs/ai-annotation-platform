from __future__ import annotations

import math

import pytest

from aap_assets_catalog.axis_convention import (
    R_NORM,
    apply_to_psr,
    sniff_convention_from_forward,
    transform_box_geometry_axis_frame,
    unapply_to_psr,
)


@pytest.mark.parametrize(
    ("convention", "expected_best"),
    [
        ("iso_8855", "iso_8855"),
        ("ros_rep103", "iso_8855"),
        ("kitti_camera", "kitti_camera"),
        ("opencv_camera", "kitti_camera"),
        ("apollo", "apollo"),
        ("y_forward", "apollo"),
        ("sustechpoints_demo", "sustechpoints_demo"),
    ],
)
def test_sniff_convention_from_forward_matches_r_norm_row0(
    convention,
    expected_best,
):
    matrix = R_NORM[convention]

    result = sniff_convention_from_forward(matrix[0], matrix[1], matrix[2])

    assert result is not None
    assert result["best"] == expected_best
    assert result["score"] == pytest.approx(1.0)
    assert any(
        candidate["convention"] == convention
        and candidate["score"] == pytest.approx(1.0)
        for candidate in result["candidates"]
    )


def test_apply_unapply_psr_round_trip():
    source = {
        "center": [1.5, -2.0, 3.25],
        "size": [4.0, 1.8, 1.6],
        "rotation": [0.1, -0.2, math.pi / 4],
    }

    iso = apply_to_psr(source, "apollo")
    output = unapply_to_psr(iso, "apollo")

    assert output["center"] == pytest.approx(source["center"])
    assert output["size"] == pytest.approx(source["size"])
    assert output["rotation"] == pytest.approx(source["rotation"])


def test_transform_box_geometry_axis_frame_source_keeps_extra_fields():
    geometry = {
        "type": "box_3d",
        "center": [0, 1, 0],
        "size": [4, 2, 1],
        "rotation": [0, 0, 0],
        "convention_at_create": "apollo",
    }

    output = transform_box_geometry_axis_frame(
        geometry,
        dataset_convention="apollo",
        axis_frame="source",
    )

    assert output["center"] == pytest.approx([-1, 0, 0])
    assert output["size"] == pytest.approx([4, 2, 1])
    assert output["axis_frame"] == "source"
    assert output["axis_convention"] == "apollo"
    assert output["convention_at_create"] == "apollo"


def test_transform_box_geometry_axis_frame_iso_is_noop_copy():
    geometry = {
        "type": "box_3d",
        "center": [0, 1, 0],
        "size": [1, 1, 1],
        "rotation": [0, 0, 0],
    }

    output = transform_box_geometry_axis_frame(
        geometry,
        dataset_convention="apollo",
        axis_frame="iso",
    )

    assert output == geometry
    assert output is not geometry
