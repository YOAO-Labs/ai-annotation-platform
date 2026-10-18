from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from aap_assets_catalog.axis_convention import R_NORM
from aap_assets_catalog.axis_sniff import AxisObservation, sniff_axis_convention
from aap_assets_catalog.spatial_values import SensorCalibration

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _calibration(convention: str) -> SensorCalibration:
    forward = R_NORM[convention][0:3]
    return SensorCalibration(
        extrinsic=[1, 0, 0, 0, 0, 1, 0, 0, *forward, 0, 0, 0, 0, 1],
        intrinsic=[1, 0, 0, 0, 1, 0, 0, 0, 1],
    )


def _observation(
    convention: str | None,
    role: str | None,
    *,
    source: str = "task_link",
    index: int,
    created_at: datetime | None = None,
) -> AxisObservation:
    camera = role.removeprefix("camera_") if role else "unknown"
    return AxisObservation(
        item_id=UUID(int=index),
        source=source,
        camera_role=role,
        file_name="000001.jpg",
        file_path=f"scene/camera/{camera}/000001.jpg",
        created_at=created_at or (_CREATED + timedelta(seconds=index)),
        calibration=_calibration(convention) if convention else None,
    )


def _observation_forward(
    forward: tuple[float, float, float], role: str, index: int
) -> AxisObservation:
    return AxisObservation(
        item_id=UUID(int=index),
        source="task_link",
        camera_role=role,
        file_name="000001.jpg",
        file_path=f"scene/camera/{role}/000001.jpg",
        created_at=_CREATED + timedelta(seconds=index),
        calibration=SensorCalibration(
            extrinsic=[1, 0, 0, 0, 0, 1, 0, 0, *forward, 0, 0, 0, 0, 1],
            intrinsic=[1, 0, 0, 0, 1, 0, 0, 0, 1],
        ),
    )


def test_linked_observations_prefer_canonical_front_over_front_side() -> None:
    front_right = _observation("iso_8855", "camera_CAM_FRONT_RIGHT", index=1)
    front = _observation("apollo", "camera_CAM_FRONT", index=2)
    back_left = _observation("sustechpoints_demo", "camera_CAM_BACK_LEFT", index=3)

    result = sniff_axis_convention((front_right, back_left, front))

    assert result is not None
    assert result.best == "apollo"
    assert result.score == 1.0
    assert result.camera_role == "camera_CAM_FRONT"
    assert result.camera_item_id == front.item_id
    assert result.agreement == 1 / 3
    assert [row.camera_role for row in result.per_camera] == [
        "camera_CAM_FRONT",
        "camera_CAM_BACK_LEFT",
        "camera_CAM_FRONT_RIGHT",
    ]


def test_task_links_suppress_fallback_even_when_the_linked_calibration_is_missing() -> (
    None
):
    linked_without_calibration = _observation(None, "camera_front", index=1)
    dataset_fallback = _observation(
        "apollo", "camera_left", source="dataset_item", index=2
    )

    assert sniff_axis_convention((linked_without_calibration, dataset_fallback)) is None


def test_dataset_item_fallback_votes_and_applies_confidence_discount() -> None:
    observations = (
        _observation("apollo", "camera_left", source="dataset_item", index=1),
        _observation("apollo", "camera_back", source="dataset_item", index=2),
        _observation("iso_8855", "camera_rear", source="dataset_item", index=3),
    )

    result = sniff_axis_convention(observations)

    assert result is not None
    assert result.best == "apollo"
    assert result.score == 0.75
    assert result.source == "dataset_item"
    assert result.agreement == 2 / 3


def test_vote_ties_use_lexical_convention_order_independent_of_input_order() -> None:
    apollo = _observation("apollo", "camera_left", index=1)
    iso = _observation("iso_8855", "camera_back", index=2)

    forward = sniff_axis_convention((iso, apollo))
    reverse = sniff_axis_convention((apollo, iso))

    assert forward is not None and reverse is not None
    assert forward.best == reverse.best == "apollo"
    assert forward.score == reverse.score == 0.75


def test_vote_rounds_score_sums_before_lexical_tiebreak() -> None:
    close_apollo = _observation_forward(
        (0.0, (1.0 - 8e-10) ** 0.5, (8e-10) ** 0.5),
        "camera_back",
        1,
    )
    exact_iso = _observation("iso_8855", "camera_left", index=2)

    result = sniff_axis_convention((exact_iso, close_apollo))

    assert result is not None
    assert result.best == "apollo"


def test_camera_and_representative_ties_keep_their_distinct_time_order() -> None:
    earlier = _observation("apollo", "camera_front", index=1, created_at=_CREATED)
    later = _observation(
        "iso_8855", "camera_front", index=2, created_at=_CREATED + timedelta(seconds=1)
    )

    result = sniff_axis_convention((later, earlier))

    assert result is not None
    assert result.best == "apollo"
    assert result.camera_item_id == earlier.item_id
    assert result.per_camera[0].camera_role == "camera_front"
    assert result.per_camera[0].best == "iso_8855"
