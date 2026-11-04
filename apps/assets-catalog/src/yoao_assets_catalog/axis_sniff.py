"""Portable LiDAR axis inference over detached camera observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from .axis_convention import sniff_convention_from_forward
from .spatial_values import LidarAxisConvention, SensorCalibration

AxisSniffSource = Literal["task_link", "dataset_item"]


@dataclass(frozen=True, slots=True)
class AxisObservation:
    item_id: UUID
    source: AxisSniffSource
    camera_role: str | None
    file_name: str
    file_path: str
    created_at: datetime
    calibration: SensorCalibration | None


@dataclass(frozen=True, slots=True)
class AxisSniffCandidate:
    convention: LidarAxisConvention
    score: float


@dataclass(frozen=True, slots=True)
class AxisSniffCameraResult:
    camera_role: str | None
    best: LidarAxisConvention
    score: float


@dataclass(frozen=True, slots=True)
class AxisSniffResult:
    best: LidarAxisConvention
    score: float
    candidates: tuple[AxisSniffCandidate, ...]
    source: AxisSniffSource
    camera_role: str | None
    camera_item_id: UUID
    per_camera: tuple[AxisSniffCameraResult, ...]
    agreement: float


_SIDE_BACK_TOKENS = ("left", "right", "back", "rear")


def _is_front_role(observation: AxisObservation) -> bool:
    """A canonical front camera excludes all side and rear roles."""
    haystack = " ".join(
        part
        for part in (
            observation.camera_role or "",
            observation.file_name,
            observation.file_path,
        )
        if part
    ).lower()
    if "front" not in haystack and "forward" not in haystack:
        return False
    return not any(token in haystack for token in _SIDE_BACK_TOKENS)


def sniff_axis_convention(
    observations: tuple[AxisObservation, ...] | list[AxisObservation],
) -> AxisSniffResult | None:
    """Infer a Dataset convention, preserving linked-camera and fallback policy."""
    linked = [row for row in observations if row.source == "task_link"]
    selected = linked or [row for row in observations if row.source == "dataset_item"]

    evaluated: list[tuple[AxisObservation, dict]] = []
    for observation in selected:
        calibration = observation.calibration
        if calibration is None:
            continue
        result = sniff_convention_from_forward(
            float(calibration.extrinsic[8]),
            float(calibration.extrinsic[9]),
            float(calibration.extrinsic[10]),
        )
        if result is not None:
            evaluated.append((observation, result))
    if not evaluated:
        return None

    per_camera_entries = _best_per_camera(evaluated)
    per_camera = tuple(
        AxisSniffCameraResult(
            camera_role=observation.camera_role,
            best=result["best"],
            score=float(result["score"]),
        )
        for observation, result in sorted(
            per_camera_entries,
            key=lambda entry: (
                not _is_front_role(entry[0]),
                -float(entry[1]["score"]),
                str(entry[0].camera_role or ""),
            ),
        )
    )

    fronts = [entry for entry in evaluated if _is_front_role(entry[0])]
    if fronts:
        observation, result = min(
            fronts,
            key=lambda entry: (
                -float(entry[1]["score"]),
                *_stable_key(entry[0]),
            ),
        )
        score_scale = 1.0
    else:
        observation, result = _vote(evaluated)
        score_scale = 0.75

    candidates = tuple(
        AxisSniffCandidate(
            convention=candidate["convention"],
            score=float(candidate["score"]) * score_scale,
        )
        for candidate in result["candidates"]
    )
    best = candidates[0]
    agreement = sum(row.best == best.convention for row in per_camera) / len(per_camera)
    return AxisSniffResult(
        best=best.convention,
        score=best.score,
        candidates=candidates,
        source=observation.source,
        camera_role=observation.camera_role,
        camera_item_id=observation.item_id,
        per_camera=per_camera,
        agreement=agreement,
    )


def _stable_key(observation: AxisObservation) -> tuple[datetime, str]:
    return observation.created_at, str(observation.item_id)


def _best_per_camera(
    evaluated: list[tuple[AxisObservation, dict]],
) -> list[tuple[AxisObservation, dict]]:
    grouped: dict[str, tuple[AxisObservation, dict]] = {}
    for observation, result in evaluated:
        key = observation.camera_role or str(observation.item_id)
        current = grouped.get(key)
        if current is None:
            grouped[key] = observation, result
            continue
        current_observation, current_result = current
        current_key = (
            float(current_result["score"]),
            *_stable_key(current_observation),
        )
        next_key = (float(result["score"]), *_stable_key(observation))
        if next_key > current_key:
            grouped[key] = observation, result
    return list(grouped.values())


def _vote(
    evaluated: list[tuple[AxisObservation, dict]],
) -> tuple[AxisObservation, dict]:
    votes: dict[str, int] = {}
    score_sum: dict[str, float] = {}
    for _observation, result in evaluated:
        convention = str(result["best"])
        votes[convention] = votes.get(convention, 0) + 1
        score_sum[convention] = score_sum.get(convention, 0.0) + float(result["score"])
    winner = min(
        votes, key=lambda value: (-votes[value], -round(score_sum[value], 9), value)
    )
    return min(
        (entry for entry in evaluated if str(entry[1]["best"]) == winner),
        key=lambda entry: (-float(entry[1]["score"]), *_stable_key(entry[0])),
    )


__all__ = (
    "AxisObservation",
    "AxisSniffCameraResult",
    "AxisSniffCandidate",
    "AxisSniffResult",
    "AxisSniffSource",
    "sniff_axis_convention",
)
