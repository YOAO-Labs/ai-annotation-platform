"""Build auxiliary samples from verified current images and measured LiDAR only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io

import numpy as np
from PIL import Image, ImageOps
from scipy.spatial import cKDTree

from app.schemas.point_cloud_densification import ReconstructedCamera
from app.services.point_cloud_densification.geometry import (
    local_correction,
    project,
    robust_scale,
    sample_image,
    unproject,
    visible_rows,
    voxel_representatives,
)


@dataclass(frozen=True)
class CameraPrediction:
    role: str
    image: np.ndarray
    depth: np.ndarray
    confidence: np.ndarray
    intrinsic: np.ndarray
    primary_from_camera: np.ndarray


def load_array(payload: bytes, descriptor) -> np.ndarray:
    if (
        len(payload) != descriptor.byte_size
        or hashlib.sha256(payload).hexdigest() != descriptor.sha256
    ):
        raise ValueError("prediction artifact integrity mismatch")
    value = np.load(io.BytesIO(payload), allow_pickle=False)
    if (
        not isinstance(value, np.ndarray)
        or value.dtype.str != "<f4"
        or list(value.shape) != [352, 640]
    ):
        raise ValueError("prediction artifact shape or dtype mismatch")
    if not np.isfinite(value).all():
        raise ValueError("non-finite prediction artifact")
    return value


def prepare_camera(
    source: bytes,
    frozen,
    result: ReconstructedCamera,
    depth: np.ndarray,
    confidence: np.ndarray,
) -> CameraPrediction:
    if hashlib.sha256(source).hexdigest() != result.source_sha256:
        raise ValueError("source image changed")
    with Image.open(io.BytesIO(source)) as opened:
        if opened.size != (result.source_width, result.source_height):
            raise ValueError("source image dimensions changed")
        image = opened.convert("RGB")
    preprocessing = result.preprocessing
    left, top, width, height = preprocessing.crop
    new_width, new_height = preprocessing.resize
    pad_left, pad_top, pad_right, pad_bottom = preprocessing.padding
    if (
        min(left, top, pad_left, pad_top, pad_right, pad_bottom) < 0
        or min(width, height, new_width, new_height) <= 0
        or left + width > image.width
        or top + height > image.height
        or new_width + pad_left + pad_right != 640
        or new_height + pad_top + pad_bottom != 352
    ):
        raise ValueError("invalid source image preprocessing")
    sx, sy = new_width / width, new_height / height
    expected = np.array(
        [
            [sx, 0, (0.5 - left) * sx - 0.5 + pad_left],
            [0, sy, (0.5 - top) * sy - 0.5 + pad_top],
            [0, 0, 1],
        ]
    )
    affine = np.asarray(preprocessing.source_to_output).reshape(3, 3)
    inverse = np.asarray(preprocessing.output_to_source).reshape(3, 3)
    if not np.allclose(affine, expected, atol=1e-7, rtol=1e-7) or not np.allclose(
        affine @ inverse, np.eye(3), atol=1e-7
    ):
        raise ValueError("inconsistent preprocessing transform")
    image = image.crop((left, top, left + width, top + height)).resize(
        (new_width, new_height), Image.Resampling.BICUBIC
    )
    image = ImageOps.expand(image, (pad_left, pad_top, pad_right, pad_bottom), fill=0)
    calibration = frozen.calibration
    camera_from_primary = np.asarray(calibration.extrinsic, dtype=float).reshape(4, 4)
    if calibration.rect:
        camera_from_primary = (
            np.asarray(calibration.rect, dtype=float).reshape(4, 4)
            @ camera_from_primary
        )
    intrinsic = affine @ np.asarray(calibration.intrinsic, dtype=float).reshape(3, 3)
    if not np.isfinite(camera_from_primary).all() or not np.allclose(
        camera_from_primary[3], [0, 0, 0, 1]
    ):
        raise ValueError("invalid camera transform")
    if not np.allclose(
        camera_from_primary[:3, :3] @ camera_from_primary[:3, :3].T,
        np.eye(3),
        atol=1e-4,
    ):
        raise ValueError("camera transform is not rigid")
    return CameraPrediction(
        frozen.role,
        np.asarray(image),
        depth,
        confidence,
        intrinsic,
        np.linalg.inv(camera_from_primary),
    )


def build_clouds(measured: np.ndarray, predictions: list[CameraPrediction]):
    """Keep measured geometry outside the generated arrays and preserve sample lineage."""
    measured = np.asarray(measured, dtype=np.float64)
    if measured.ndim != 2 or measured.shape[1] != 3 or not np.isfinite(measured).all():
        raise ValueError("invalid current measured geometry")
    if len(predictions) != 6 or len({p.role for p in predictions}) != 6:
        raise ValueError("six current independent camera predictions are required")
    projected = []
    for prediction in predictions:
        uv, z = project(measured, prediction.intrinsic, prediction.primary_from_camera)
        rows = visible_rows(uv, z, (352, 640))
        projected.append((uv, z, rows, cKDTree(uv[rows]) if len(rows) else None))
    clouds, diagnostics = {}, {}
    for prediction, (uv, z, rows, _) in zip(predictions, projected):
        scale, scale_info = robust_scale(
            sample_image(prediction.depth, uv[rows]), z[rows], rows
        )
        corrected, local_supported, local_info = local_correction(
            prediction.depth.astype(float) * scale, prediction.image, uv[rows], z[rows]
        )
        valid = (
            np.isfinite(corrected)
            & (corrected >= 1)
            & (corrected <= 80)
            & (prediction.confidence >= np.quantile(prediction.confidence, 0.2))
        )
        valid[int(352 * 0.95) :] = False
        ys, xs = np.nonzero(valid)
        pixel = ys.astype(np.int64) * 640 + xs
        xyz = unproject(
            np.column_stack((xs, ys)),
            corrected[ys, xs],
            prediction.intrinsic,
            prediction.primary_from_camera,
        )
        keep = np.ones(len(xyz), dtype=bool)
        for other, (_, _, _, tree) in zip(predictions, projected):
            if tree is None:
                continue
            candidate_uv, candidate_z = project(
                xyz, other.intrinsic, other.primary_from_camera
            )
            visible = (
                keep
                & np.isfinite(candidate_uv).all(axis=1)
                & (candidate_z >= 1)
                & (candidate_z <= 80)
                & (candidate_uv[:, 0] >= 0)
                & (candidate_uv[:, 0] < 639)
                & (candidate_uv[:, 1] >= 0)
                & (candidate_uv[:, 1] < 352 * 0.95)
            )
            ids = np.flatnonzero(visible)
            distances, _ = tree.query(candidate_uv[ids], distance_upper_bound=3 + 1e-6)
            keep[ids[np.isfinite(distances)]] = False
        xyz, pixel, xs, ys = xyz[keep], pixel[keep], xs[keep], ys[keep]
        representatives = voxel_representatives(xyz, 0.1)
        xyz, pixel, xs, ys = (a[representatives] for a in (xyz, pixel, xs, ys))
        clouds[prediction.role] = {
            "xyz": np.asarray(xyz, dtype=np.float32),
            "rgb": prediction.image[ys, xs].astype(np.uint8),
            "pixel": pixel.astype(np.uint32),
            "confidence": prediction.confidence[ys, xs].astype(np.float32),
        }
        diagnostics[prediction.role] = {
            "scale": scale_info,
            "local_correction": local_info,
            "local_supported_pixels": int(local_supported.sum()),
            "retained_points": len(xyz),
        }
    return clouds, diagnostics
