"""Pinned B6S1 producer. Metric calibration remains in the platform worker."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from config import (
    CAMERA_COUNT,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    PREPROCESS_RESOLUTION,
    SOURCE_COMMIT,
    WEIGHTS_SHA256,
)
from schemas import Preprocessing

VENDOR = Path(__file__).parent / "vendor"


class ProducerUnavailable(RuntimeError):
    """Missing/incompatible model prerequisites; callers may retry provisioned service."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_vendor() -> None:
    manifest = json.loads((VENDOR / "SOURCE.json").read_text())
    if manifest["commit"] != SOURCE_COMMIT:
        raise ProducerUnavailable("model source commit mismatch")
    for name, expected in manifest["files"].items():
        if sha256_file(VENDOR / name) != expected:
            raise ProducerUnavailable("model source integrity check failed")


def validate_weights(path: Path) -> None:
    if not path.is_file():
        raise ProducerUnavailable("weights are not provisioned")
    if sha256_file(path) != WEIGHTS_SHA256:
        raise ProducerUnavailable("weights integrity check failed")


def preprocessing_for(width: int, height: int) -> Preprocessing:
    """Mirror pinned load_fn crop/resize/pad, with exact pixel-center transforms."""
    left = top = 0
    crop_width, crop_height = width, height
    ratio = height / width
    if ratio < 0.5:
        crop_width = min(width, max(1, int(round(height / 0.5))))
        left = max((width - crop_width) // 2, 0)
    elif ratio > 2.0:
        crop_height = min(height, max(1, int(round(width * 2.0))))
        top = max((height - crop_height) // 2, 0)
    ratio = crop_height / crop_width
    tokens = (PREPROCESS_RESOLUTION // 16) ** 2
    w_patches = np.sqrt(tokens / ratio)
    h_patches = tokens / w_patches
    target_width = max(1, int(np.round(w_patches))) * 16
    target_height = max(1, int(np.round(h_patches))) * 16
    if (target_height, target_width) != (OUTPUT_HEIGHT, OUTPUT_WIDTH):
        raise ValueError("input aspect ratio is not qualified for the 640x352 profile")
    sx, sy = target_width / crop_width, target_height / crop_height
    tx, ty = (0.5 - left) * sx - 0.5, (0.5 - top) * sy - 0.5
    return Preprocessing(
        source_to_output=[sx, 0, tx, 0, sy, ty, 0, 0, 1],
        output_to_source=[1 / sx, 0, -tx / sx, 0, 1 / sy, -ty / sy, 0, 0, 1],
        output_width=target_width,
        output_height=target_height,
        crop=(left, top, crop_width, crop_height),
        resize=(target_width, target_height),
        padding=(0, 0, 0, 0),
    )


@dataclass(frozen=True)
class PredictionArrays:
    depth: np.ndarray
    confidence: np.ndarray
    inference_ms: int


class OmegaModel:
    device = "cuda:0"

    def __init__(self, weights: Path) -> None:
        validate_vendor()
        validate_weights(weights)
        sys.path.insert(0, str(VENDOR))
        import torch
        from vggt_omega.models import VGGTOmega

        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise ProducerUnavailable("exactly one CUDA GPU is required")
        if not torch.cuda.is_bf16_supported():
            raise ProducerUnavailable("this profile requires qualified BF16 CUDA support")
        if torch.__version__ != "2.6.0+cu124":
            raise ProducerUnavailable("this profile requires torch 2.6.0+cu124")
        torch.set_num_threads(8)
        torch.manual_seed(42)
        model: Any = None
        try:
            model = VGGTOmega().eval()
            model.load_state_dict(
                torch.load(weights, map_location="cpu", weights_only=True), strict=True
            )
            self.model = model.cuda()
        except BaseException:
            model = None
            strict_gpu_cleanup()
            raise

    def predict(self, paths: list[Path]) -> PredictionArrays:
        import torch
        from torch.nn.attention import SDPBackend, sdpa_kernel
        from vggt_omega.utils.load_fn import load_and_preprocess_images

        if len(paths) != CAMERA_COUNT:
            raise ValueError("six camera images are required")
        images = load_and_preprocess_images(
            paths, image_resolution=PREPROCESS_RESOLUTION, mode="balanced"
        ).cuda()
        if tuple(images.shape) != (CAMERA_COUNT, 3, OUTPUT_HEIGHT, OUTPUT_WIDTH):
            raise ValueError("unexpected preprocessing tensor shape")
        # The six batch elements remain independent; do not change to B1 or B1S6.
        inputs = images[:, None]
        torch.cuda.synchronize()
        started = time.perf_counter()
        with (
            torch.inference_mode(),
            sdpa_kernel([SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION]),
        ):
            output = self.model(inputs)
        torch.cuda.synchronize()
        inference_ms = int((time.perf_counter() - started) * 1000)
        depth = (
            output["depth"].reshape(CAMERA_COUNT, OUTPUT_HEIGHT, OUTPUT_WIDTH).float().cpu().numpy()
        )
        confidence = (
            output["depth_conf"]
            .reshape(CAMERA_COUNT, OUTPUT_HEIGHT, OUTPUT_WIDTH)
            .float()
            .cpu()
            .numpy()
        )
        if not np.isfinite(depth).all() or not np.isfinite(confidence).all():
            raise RuntimeError("nonfinite model output")
        return PredictionArrays(depth=depth, confidence=confidence, inference_ms=inference_ms)


def strict_gpu_cleanup() -> None:
    """Do not claim successful unload if CUDA cleanup raises."""
    import gc

    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if not torch.cuda.is_available() or not torch.cuda.is_initialized():
        return
    torch.cuda.synchronize()
    torch.clear_autocast_cache()
    clear_workspaces = getattr(torch._C, "_cuda_clearCublasWorkspaces", None)
    if callable(clear_workspaces):
        clear_workspaces()
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()
