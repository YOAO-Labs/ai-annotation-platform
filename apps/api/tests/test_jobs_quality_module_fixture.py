from __future__ import annotations

import ast
import math

from tests.jobs_quality_module_helpers import (
    _API_DISPATCH_PROBE,
    _QUALITY_TASKS,
    _point_cloud_pcd,
)


def test_quality_dispatch_probe_compiles_and_covers_six_variants():
    ast.parse(_API_DISPATCH_PROBE)
    assert [item["operation"] for item in _QUALITY_TASKS] == [
        "run",
        "run",
        "run",
        "repair",
        "repair",
        "import",
    ]
    assert len({item["task"] for item in _QUALITY_TASKS}) == 6


def test_minimal_ascii_pcd_has_36_ground_points_in_expected_extent():
    lines = _point_cloud_pcd().decode("ascii").splitlines()
    data_start = lines.index("DATA ascii") + 1
    points = [tuple(map(float, line.split())) for line in lines[data_start:]]

    assert "POINTS 36" in lines
    assert len(points) == 36
    assert math.isclose(min(point[0] for point in points), 97.0)
    assert math.isclose(max(point[0] for point in points), 103.0)
    assert {point[1] for point in points} == {-1.0, 0.0, 1.0}
    assert {point[2] for point in points} == {0.0}
