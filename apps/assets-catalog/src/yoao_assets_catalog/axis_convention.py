"""LiDAR axis convention math shared by sniffing and export.

The web workbench keeps the same R_norm table in
``apps/web/src/pages/Workbench/stages/three-d/geometry/axisConvention.ts``.
Keep the two implementations in lockstep; tests cover the shared contract.
"""

from __future__ import annotations

import math
from typing import TypedDict, cast

from .spatial_values import AxisFrame, LidarAxisConvention

Mat3 = tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
]


class PsrDict(TypedDict):
    center: list[float]
    size: list[float]
    rotation: list[float]


class SniffCandidate(TypedDict):
    convention: LidarAxisConvention
    score: float


class SniffResult(TypedDict):
    best: LidarAxisConvention
    score: float
    candidates: list[SniffCandidate]


R_NORM: dict[LidarAxisConvention, Mat3] = {
    "iso_8855": (1, 0, 0, 0, 1, 0, 0, 0, 1),
    "ros_rep103": (1, 0, 0, 0, 1, 0, 0, 0, 1),
    "raw": (1, 0, 0, 0, 1, 0, 0, 0, 1),
    "kitti_camera": (0, 0, 1, -1, 0, 0, 0, -1, 0),
    "opencv_camera": (0, 0, 1, -1, 0, 0, 0, -1, 0),
    "apollo": (0, 1, 0, -1, 0, 0, 0, 0, 1),
    "y_forward": (0, 1, 0, -1, 0, 0, 0, 0, 1),
    "sustechpoints_demo": (0, -1, 0, 1, 0, 0, 0, 0, 1),
}

SNIFF_CONVENTIONS: tuple[LidarAxisConvention, ...] = (
    "iso_8855",
    "ros_rep103",
    "kitti_camera",
    "opencv_camera",
    "apollo",
    "y_forward",
    "sustechpoints_demo",
)


def _mat_vec(m: Mat3, v: tuple[float, float, float]) -> list[float]:
    return [
        m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
        m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
        m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
    ]


def _transpose(m: Mat3) -> Mat3:
    return (m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8])


def _mat_mul(a: Mat3, b: Mat3) -> Mat3:
    return (
        a[0] * b[0] + a[1] * b[3] + a[2] * b[6],
        a[0] * b[1] + a[1] * b[4] + a[2] * b[7],
        a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
        a[3] * b[0] + a[4] * b[3] + a[5] * b[6],
        a[3] * b[1] + a[4] * b[4] + a[5] * b[7],
        a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
        a[6] * b[0] + a[7] * b[3] + a[8] * b[6],
        a[6] * b[1] + a[7] * b[4] + a[8] * b[7],
        a[6] * b[2] + a[7] * b[5] + a[8] * b[8],
    )


def _euler_xyz_to_mat3(rx: float, ry: float, rz: float) -> Mat3:
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return (
        cy * cz,
        -cy * sz,
        sy,
        cx * sz + cz * sx * sy,
        cx * cz - sx * sy * sz,
        -cy * sx,
        sx * sz - cx * cz * sy,
        cz * sx + cx * sy * sz,
        cx * cy,
    )


def _mat3_to_euler_xyz(m: Mat3) -> list[float]:
    sy = max(-1.0, min(1.0, m[2]))
    ry = math.asin(sy)
    cy = math.cos(ry)
    if abs(cy) > 1e-6:
        rx = math.atan2(-m[5], m[8])
        rz = math.atan2(-m[1], m[0])
    else:
        rx = math.atan2(m[7], m[4])
        rz = 0.0
    return [rx, ry, rz]


def apply_to_psr(psr: PsrDict, convention: LidarAxisConvention) -> PsrDict:
    """Map a source-frame PSR into the platform ISO frame."""

    r_norm = R_NORM[convention]
    center = _mat_vec(
        r_norm,
        (
            float(psr["center"][0]),
            float(psr["center"][1]),
            float(psr["center"][2]),
        ),
    )
    box = _euler_xyz_to_mat3(
        float(psr["rotation"][0]),
        float(psr["rotation"][1]),
        float(psr["rotation"][2]),
    )
    box_iso = _mat_mul(_mat_mul(r_norm, box), _transpose(r_norm))
    return {
        "center": center,
        "size": [float(psr["size"][0]), float(psr["size"][1]), float(psr["size"][2])],
        "rotation": _mat3_to_euler_xyz(box_iso),
    }


def unapply_to_psr(psr: PsrDict, convention: LidarAxisConvention) -> PsrDict:
    """Map an ISO-frame PSR back to the dataset source frame."""

    r_norm = R_NORM[convention]
    rt = _transpose(r_norm)
    center = _mat_vec(
        rt,
        (
            float(psr["center"][0]),
            float(psr["center"][1]),
            float(psr["center"][2]),
        ),
    )
    box = _euler_xyz_to_mat3(
        float(psr["rotation"][0]),
        float(psr["rotation"][1]),
        float(psr["rotation"][2]),
    )
    box_src = _mat_mul(_mat_mul(rt, box), r_norm)
    return {
        "center": center,
        "size": [float(psr["size"][0]), float(psr["size"][1]), float(psr["size"][2])],
        "rotation": _mat3_to_euler_xyz(box_src),
    }


def sniff_convention_from_forward(
    fx: float,
    fy: float,
    fz: float = 0.0,
) -> SniffResult | None:
    """Infer the likely source convention from front camera row-2 forward.

    For front camera optical axis, expected source-frame forward is R_norm row 0.
    ``raw`` is intentionally excluded because it is a user override, not a
    physically distinguishable frame.
    """

    norm = math.sqrt(fx * fx + fy * fy + fz * fz)
    if norm < 1e-9:
        return None
    ux, uy, uz = fx / norm, fy / norm, fz / norm
    candidates: list[SniffCandidate] = []
    for convention in SNIFF_CONVENTIONS:
        m = R_NORM[convention]
        ex, ey, ez = m[0], m[1], m[2]
        expected_norm = math.sqrt(ex * ex + ey * ey + ez * ez)
        if expected_norm < 1e-9:
            continue
        score = (
            ux * (ex / expected_norm)
            + uy * (ey / expected_norm)
            + uz * (ez / expected_norm)
        )
        candidates.append({"convention": convention, "score": score})
    candidates.sort(key=lambda c: c["score"], reverse=True)
    if not candidates:
        return None
    best = candidates[0]
    return {
        "best": cast(LidarAxisConvention, best["convention"]),
        "score": best["score"],
        "candidates": candidates,
    }


def transform_box_geometry_axis_frame(
    geometry: dict,
    *,
    dataset_convention: LidarAxisConvention | None,
    axis_frame: AxisFrame,
) -> dict:
    """Return exported geometry, transforming box_3d to source frame when requested."""

    if axis_frame == "iso" or geometry.get("type") != "box_3d":
        return dict(geometry)
    convention = dataset_convention or "iso_8855"
    psr = {
        "center": list(geometry.get("center") or [0, 0, 0]),
        "size": list(geometry.get("size") or [0, 0, 0]),
        "rotation": list(geometry.get("rotation") or [0, 0, 0]),
    }
    out = dict(geometry)
    out.update(unapply_to_psr(psr, convention))
    out["axis_frame"] = "source"
    out["axis_convention"] = convention
    return out
