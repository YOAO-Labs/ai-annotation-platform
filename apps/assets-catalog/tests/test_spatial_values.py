from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_assets_catalog.spatial_values import (
    FramePose,
    SensorCalibration,
    calibration_digest,
)

_GOLDENS = json.loads(
    (Path(__file__).parent / "fixtures/calibration_digest_golden.json").read_text()
)


@pytest.mark.parametrize("golden", _GOLDENS, ids=lambda golden: golden["name"])
def test_calibration_digest_matches_frozen_json_bytes(golden: dict) -> None:
    calibration = SensorCalibration.model_validate(golden["calibration"])

    assert (
        hashlib.sha256(golden["canonical_json"].encode()).hexdigest()
        == golden["sha256"]
    )
    assert calibration_digest(calibration) == golden["sha256"]


def test_sensor_calibration_preserves_null_rect_and_rejects_extra_fields() -> None:
    calibration = SensorCalibration(
        extrinsic=[0] * 16,
        intrinsic=[0] * 9,
        rect=None,
    )

    assert calibration.model_dump(mode="json")["rect"] is None
    with pytest.raises(ValidationError):
        SensorCalibration(
            extrinsic=[0] * 16,
            intrinsic=[0] * 9,
            vendor_extension=True,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "frame_index": -1,
            "ego_translation": [0, 0, 0],
            "ego_rotation": [1, 0, 0, 0],
        },
        {
            "frame_index": 0,
            "ego_translation": [0, 0],
            "ego_rotation": [1, 0, 0, 0],
        },
        {
            "frame_index": 0,
            "ego_translation": [0, 0, 0],
            "ego_rotation": [1, 0, 0],
        },
    ],
)
def test_frame_pose_rejects_invalid_indices_and_vector_lengths(payload: dict) -> None:
    with pytest.raises(ValidationError):
        FramePose.model_validate(payload)


def test_frame_pose_keeps_current_wire_shape() -> None:
    pose = FramePose(
        frame_index=3,
        timestamp_us=None,
        ego_translation=[1, 2, 3],
        ego_rotation=[1, 0, 0, 0],
    )

    assert pose.model_dump(mode="json") == {
        "frame_index": 3,
        "timestamp_us": None,
        "ego_translation": [1.0, 2.0, 3.0],
        "ego_rotation": [1.0, 0.0, 0.0, 0.0],
        "source_metadata": {},
    }
