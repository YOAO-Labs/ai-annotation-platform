"""Shared, deterministic 3D cuboid to camera projection primitives."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol

from aap_assets_catalog.spatial_values import SensorCalibration
from aap_assets_catalog.axis_convention import PsrDict, R_NORM


class ProjectionCamera(Protocol):
    calibration: SensorCalibration
    width: int
    height: int


@dataclass(frozen=True)
class ProjectedBox:
    pixel_bbox: tuple[float, float, float, float]
    normalized_bbox: tuple[float, float, float, float]
    truncated: float


@dataclass(frozen=True)
class ProjectionResidual:
    iou: float
    max_edge_residual_px: float
    mean_edge_residual_px: float
    max_edge_residual_ratio: float


BOX_EDGES: tuple[tuple[int, int], ...] = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 0),
    (4, 5),
    (5, 6),
    (6, 7),
    (7, 4),
    (0, 4),
    (1, 5),
    (2, 6),
    (3, 7),
)
NEAR_PLANE = 0.1


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def mat3_vec(
    matrix: tuple[float, ...] | list[float], point: tuple[float, float, float]
) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z,
        matrix[3] * x + matrix[4] * y + matrix[5] * z,
        matrix[6] * x + matrix[7] * y + matrix[8] * z,
    )


def euler_xyz_matrix(rotation: list[float]) -> tuple[float, ...]:
    rx, ry, rz = rotation
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


def iso_to_source(
    point: tuple[float, float, float], axis_convention: str
) -> tuple[float, float, float]:
    matrix = R_NORM[axis_convention]
    transpose = (
        matrix[0],
        matrix[3],
        matrix[6],
        matrix[1],
        matrix[4],
        matrix[7],
        matrix[2],
        matrix[5],
        matrix[8],
    )
    return mat3_vec(transpose, point)


def apply_camera_point(
    point: tuple[float, float, float], calibration: SensorCalibration
) -> tuple[float, float, float]:
    x, y, z = point
    extrinsic = calibration.extrinsic
    camera = (
        extrinsic[0] * x + extrinsic[1] * y + extrinsic[2] * z + extrinsic[3],
        extrinsic[4] * x + extrinsic[5] * y + extrinsic[6] * z + extrinsic[7],
        extrinsic[8] * x + extrinsic[9] * y + extrinsic[10] * z + extrinsic[11],
    )
    if calibration.rect:
        rect = calibration.rect
        cx, cy, cz = camera
        return (
            rect[0] * cx + rect[1] * cy + rect[2] * cz + rect[3],
            rect[4] * cx + rect[5] * cy + rect[6] * cz + rect[7],
            rect[8] * cx + rect[9] * cy + rect[10] * cz + rect[11],
        )
    return camera


def apply_camera_vector(
    vector: tuple[float, float, float], calibration: SensorCalibration
) -> tuple[float, float, float]:
    extrinsic = calibration.extrinsic
    camera = mat3_vec(
        (
            extrinsic[0],
            extrinsic[1],
            extrinsic[2],
            extrinsic[4],
            extrinsic[5],
            extrinsic[6],
            extrinsic[8],
            extrinsic[9],
            extrinsic[10],
        ),
        vector,
    )
    if calibration.rect:
        rect = calibration.rect
        return mat3_vec(
            (
                rect[0],
                rect[1],
                rect[2],
                rect[4],
                rect[5],
                rect[6],
                rect[8],
                rect[9],
                rect[10],
            ),
            camera,
        )
    return camera


def project_camera_point(
    point: tuple[float, float, float], intrinsic: list[float]
) -> tuple[float, float] | None:
    x, y, z = point
    u = intrinsic[0] * x + intrinsic[1] * y + intrinsic[2] * z
    v = intrinsic[3] * x + intrinsic[4] * y + intrinsic[5] * z
    w = intrinsic[6] * x + intrinsic[7] * y + intrinsic[8] * z
    if w <= 0 or not math.isfinite(w):
        return None
    pixel = (u / w, v / w)
    return pixel if all(math.isfinite(value) for value in pixel) else None


def box_iso_geometry(
    psr: PsrDict,
) -> tuple[
    list[tuple[float, float, float]],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    center = tuple(psr["center"])
    length, width, height = psr["size"]
    rotation = euler_xyz_matrix(psr["rotation"])

    def world(local: tuple[float, float, float]) -> tuple[float, float, float]:
        offset = mat3_vec(rotation, local)
        return (
            center[0] + offset[0],
            center[1] + offset[1],
            center[2] + offset[2],
        )

    corners = [
        world((sx * length / 2, sy * width / 2, sz * height / 2))
        for sz in (-1, 1)
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]
    return corners, world((0, 0, -height / 2)), mat3_vec(rotation, (1, 0, 0))


def clipped_projection_bbox(
    corners: list[tuple[float, float, float]], camera: ProjectionCamera
) -> tuple[tuple[float, float, float, float], float] | None:
    candidates = [point for point in corners if point[2] >= NEAR_PLANE]
    for start_index, end_index in BOX_EDGES:
        start = corners[start_index]
        end = corners[end_index]
        start_front = start[2] >= NEAR_PLANE
        end_front = end[2] >= NEAR_PLANE
        if start_front == end_front:
            continue
        ratio = (NEAR_PLANE - start[2]) / (end[2] - start[2])
        candidates.append(
            (
                start[0] + ratio * (end[0] - start[0]),
                start[1] + ratio * (end[1] - start[1]),
                NEAR_PLANE,
            )
        )
    pixels = [
        pixel
        for point in candidates
        if (pixel := project_camera_point(point, camera.calibration.intrinsic))
        is not None
    ]
    if not pixels:
        return None
    raw = (
        min(pixel[0] for pixel in pixels),
        min(pixel[1] for pixel in pixels),
        max(pixel[0] for pixel in pixels),
        max(pixel[1] for pixel in pixels),
    )
    raw_area = max(0.0, raw[2] - raw[0]) * max(0.0, raw[3] - raw[1])
    if raw_area <= 1e-9:
        return None
    image_right = float(camera.width - 1)
    image_bottom = float(camera.height - 1)
    clipped = (
        clamp(raw[0], 0.0, image_right),
        clamp(raw[1], 0.0, image_bottom),
        clamp(raw[2], 0.0, image_right),
        clamp(raw[3], 0.0, image_bottom),
    )
    clipped_area = max(0.0, clipped[2] - clipped[0]) * max(0.0, clipped[3] - clipped[1])
    if clipped_area <= 1e-9:
        return None
    return clipped, clamp(1.0 - clipped_area / raw_area, 0.0, 1.0)


def normalized_bbox(
    pixel_bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = pixel_bbox
    return (x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height)


def pixel_bbox(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    x, y, w, h = bbox
    return (x * width, y * height, (x + w) * width, (y + h) * height)


def project_iso_box(
    psr: PsrDict, *, camera: ProjectionCamera, axis_convention: str
) -> ProjectedBox | None:
    corners_iso, _, _ = box_iso_geometry(psr)
    corners_camera = [
        apply_camera_point(iso_to_source(point, axis_convention), camera.calibration)
        for point in corners_iso
    ]
    result = clipped_projection_bbox(corners_camera, camera)
    if result is None:
        return None
    bbox, truncated = result
    return ProjectedBox(
        pixel_bbox=bbox,
        normalized_bbox=normalized_bbox(bbox, camera.width, camera.height),
        truncated=truncated,
    )


def projection_residual(
    manual_normalized_bbox: tuple[float, float, float, float],
    projected_pixel_bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> ProjectionResidual:
    manual = pixel_bbox(manual_normalized_bbox, width, height)
    x1 = max(manual[0], projected_pixel_bbox[0])
    y1 = max(manual[1], projected_pixel_bbox[1])
    x2 = min(manual[2], projected_pixel_bbox[2])
    y2 = min(manual[3], projected_pixel_bbox[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    manual_area = max(0.0, manual[2] - manual[0]) * max(0.0, manual[3] - manual[1])
    projected_area = max(0.0, projected_pixel_bbox[2] - projected_pixel_bbox[0]) * max(
        0.0, projected_pixel_bbox[3] - projected_pixel_bbox[1]
    )
    union = manual_area + projected_area - intersection
    edges = [abs(a - b) for a, b in zip(manual, projected_pixel_bbox, strict=True)]
    maximum = max(edges)
    return ProjectionResidual(
        iou=intersection / union if union > 1e-9 else 0.0,
        max_edge_residual_px=maximum,
        mean_edge_residual_px=sum(edges) / len(edges),
        max_edge_residual_ratio=maximum / max(math.hypot(width, height), 1.0),
    )
