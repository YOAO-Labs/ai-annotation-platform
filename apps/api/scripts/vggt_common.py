"""Shared geometry, source fences and I/O for the VGGT reconstruction tools."""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def verify_runtime_sources(output: Path) -> bool:
    """Verify a consolidation fence without rewriting historical inference proof."""
    manifest = output / "code-consolidation.json"
    if not manifest.exists():
        return False
    record = json.loads(manifest.read_text())
    workspace = Path(__file__).resolve().parents[3]
    for root, entries in (
        (workspace, record["runtime_source_hashes"]),
        (output, record["historical_record_hashes"]),
    ):
        for name, expected in entries.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f"consolidation source fence changed: {name}")
    current = ast.parse(Path(__file__).read_text())
    for row in record["extracted_symbols"]:
        historical = ast.parse((output / row["archive"]).read_text())
        trees = []
        for tree in (historical, current):
            node = next(
                n
                for n in tree.body
                if getattr(n, "name", None) == row["symbol"]
                or isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == row["symbol"] for t in n.targets
                )
            )
            trees.append(ast.dump(node, include_attributes=False))
        if trees[0] != trees[1]:
            raise ValueError(f"extracted geometry changed: {row['symbol']}")
    return True


VIEWER_FROM_REFERENCE = np.array([[0, -1, 0], [0, 0, 1], [-1, 0, 0]], dtype=float)


def interpolate_box(track: list[dict], timestamp: int) -> dict:
    """Interpolate keyframe boxes to exposure time; clamp outside track support."""
    times = np.asarray([box["timestamp"] for box in track], dtype=np.int64)
    if np.any(np.diff(times) <= 0):
        raise ValueError("track timestamps must increase")
    clipped = int(np.clip(timestamp, times[0], times[-1]))
    right = min(int(np.searchsorted(times, clipped, side="right")), len(track) - 1)
    left = max(0, right - 1)
    fraction = (
        (clipped - times[left]) / (times[right] - times[left]) if right != left else 0
    )
    a, b = track[left], track[right]
    q = np.asarray([a["rotation"], b["rotation"]], dtype=float)
    if right == left:
        rotation = Rotation.from_quat(q[0, [1, 2, 3, 0]])
    else:
        rotation = Slerp([0, 1], Rotation.from_quat(q[:, [1, 2, 3, 0]]))([fraction])[0]
    xyzw = rotation.as_quat()
    return {
        **a,
        "translation": (
            (1 - fraction) * np.asarray(a["translation"])
            + fraction * np.asarray(b["translation"])
        ).tolist(),
        "size": (
            (1 - fraction) * np.asarray(a["size"]) + fraction * np.asarray(b["size"])
        ).tolist(),
        "rotation": xyzw[[3, 0, 1, 2]].tolist(),
        "rotation_matrix": rotation.as_matrix(),
        "timestamp": timestamp,
        "clamped_timestamp": clipped != timestamp,
    }


def _box_geometry(
    box: dict, padding: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # nuScenes sizes are width, length, height; box axes are length, width, height.
    half = np.asarray(box["size"], dtype=np.float64)[[1, 0, 2]] / 2 + padding
    return np.asarray(box["translation"]), np.asarray(box["rotation_matrix"]), half


def points_in_actors(
    points: np.ndarray, boxes: list[dict], padding: float
) -> np.ndarray:
    result = np.zeros(len(points), dtype=bool)
    for box in boxes:
        center, rotation, half = _box_geometry(box, padding)
        local = (points - center) @ rotation
        result |= np.all(np.abs(local) <= half, axis=1)
    return result


def actor_ray_mask(
    intrinsic: np.ndarray,
    size: tuple[int, int],
    global_from_camera: np.ndarray,
    boxes: list[dict],
    padding: float,
    max_depth: float = 80,
) -> np.ndarray:
    """Mask rays intersecting annotated actor volumes, regardless of model depth.

    This intentionally removes occluded background and parked actor categories.
    Ray/box intersection also handles boxes crossing the camera's near plane.
    """
    width, height = size
    mask = np.zeros((height, width), dtype=bool)
    camera_from_global = np.linalg.inv(global_from_camera)
    corner_signs = np.asarray(list(itertools.product((-1, 1), repeat=3)))
    for box in boxes:
        center, rotation, half = _box_geometry(box, padding)
        corners = (corner_signs * half) @ rotation.T + center
        camera_corners = (
            corners @ camera_from_global[:3, :3].T + camera_from_global[:3, 3]
        )
        if np.max(camera_corners[:, 2]) < 1:
            continue
        left, right, top, bottom = 0, width, 0, height
        if np.min(camera_corners[:, 2]) > 1:
            projected = camera_corners @ intrinsic.T
            uv = projected[:, :2] / projected[:, 2:]
            left = max(0, int(np.floor(uv[:, 0].min())) - 1)
            right = min(width, int(np.ceil(uv[:, 0].max())) + 1)
            top = max(0, int(np.floor(uv[:, 1].min())) - 1)
            bottom = min(height, int(np.ceil(uv[:, 1].max())) + 1)
        if left >= right or top >= bottom:
            continue
        y, x = np.mgrid[top:bottom, left:right]
        camera_rays = (
            np.column_stack((x.ravel(), y.ravel(), np.ones(x.size)))
            @ np.linalg.inv(intrinsic).T
        )
        directions = camera_rays @ global_from_camera[:3, :3].T @ rotation
        origin = (global_from_camera[:3, 3] - center) @ rotation
        parallel = np.abs(directions) < 1e-12
        with np.errstate(divide="ignore", invalid="ignore"):
            first = (-half - origin) / directions
            second = (half - origin) / directions
        low, high = np.minimum(first, second), np.maximum(first, second)
        low[parallel] = -np.inf
        high[parallel] = np.inf
        outside_parallel = np.any(parallel & (np.abs(origin) > half), axis=1)
        hit = ~outside_parallel & (
            np.maximum(1, low.max(axis=1)) <= np.minimum(max_depth, high.min(axis=1))
        )
        mask[top:bottom, left:right] |= hit.reshape(y.shape)
    return mask


def cross_view_support(
    depth: np.ndarray,
    intrinsic: np.ndarray,
    reference_from_camera: np.ndarray,
    references: list[dict],
    absolute_m: float = 0.75,
    relative: float = 0.05,
    pixel_tolerance: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Count cycle-consistent views and visible free-space contradictions.

    A nearer reference surface occludes a candidate, so it supplies no evidence.
    A candidate in front of the reference depth is a visible conflict. Missing
    depths, out-of-view rays and masked actors never count as supporting views.
    """
    support = np.zeros(depth.shape, dtype=np.uint8)
    conflicts = np.zeros(depth.shape, dtype=np.uint8)
    y, x = np.nonzero(np.isfinite(depth) & (depth >= 1) & (depth <= 80))
    if not len(x):
        return support, conflicts
    pixels = np.column_stack((x, y))
    directions = np.column_stack((pixels, np.ones(len(x)))) @ np.linalg.inv(intrinsic).T
    points = directions * depth[y, x, None]
    for reference in references:
        other_from_source = (
            np.linalg.inv(reference["reference_from_camera"]) @ reference_from_camera
        )
        other = points @ other_from_source[:3, :3].T + other_from_source[:3, 3]
        projected = other @ reference["intrinsic"].T
        uv = np.zeros((len(points), 2))
        positive = other[:, 2] >= 1
        uv[positive] = projected[positive, :2] / projected[positive, 2:]
        uv = np.rint(uv).astype(int)
        other_depth = reference["model"]
        visible = (
            positive
            & (uv[:, 0] >= 0)
            & (uv[:, 0] < other_depth.shape[1])
            & (uv[:, 1] >= 0)
            & (uv[:, 1] < other_depth.shape[0])
        )
        indices = np.flatnonzero(visible)
        if not len(indices):
            continue
        samples = other_depth[uv[indices, 1], uv[indices, 0]]
        measured = np.isfinite(samples) & (samples >= 1) & (samples <= 80)
        indices, samples = indices[measured], samples[measured]
        if not len(indices):
            continue
        tolerance = np.maximum(absolute_m, relative * samples)
        conflicts[y[indices], x[indices]] += (
            other[indices, 2] < samples - tolerance
        ).astype(np.uint8)
        other_points = (
            np.column_stack((uv[indices], np.ones(len(indices))))
            @ np.linalg.inv(reference["intrinsic"]).T
            * samples[:, None]
        )
        source_from_other = np.linalg.inv(other_from_source)
        back = other_points @ source_from_other[:3, :3].T + source_from_other[:3, 3]
        back_uv = back @ intrinsic.T
        in_front = back[:, 2] >= 1
        error_px = np.full(len(indices), np.inf)
        error_px[in_front] = np.linalg.norm(
            back_uv[in_front, :2] / back_uv[in_front, 2:] - pixels[indices[in_front]],
            axis=1,
        )
        agrees = (
            (error_px <= pixel_tolerance)
            & (np.abs(back[:, 2] - depth[y[indices], x[indices]]) <= tolerance)
            & (np.abs(other[indices, 2] - samples) <= tolerance)
        )
        support[y[indices], x[indices]] += agrees.astype(np.uint8)
    return support, conflicts


def _scene_samples(scene: dict, samples: dict[str, dict]) -> list[dict]:
    result = []
    token = scene["first_sample_token"]
    seen = set()
    while token:
        if token in seen:
            raise ValueError("sample chain contains a cycle")
        seen.add(token)
        sample = samples[token]
        if sample["scene_token"] != scene["token"]:
            raise ValueError("sample chain crossed the scene boundary")
        result.append(sample)
        token = sample["next"]
    return result


def write_json(path, value):
    def encode(item):
        if isinstance(item, np.ndarray):
            return item.tolist()
        if isinstance(item, np.generic):
            return item.item()
        if isinstance(item, Path):
            return str(item)
        raise TypeError(type(item).__name__)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=encode)
    )
    temporary.replace(path)


class MemoryMonitor:
    """Sample this process and its physical GPU without resetting CUDA counters."""

    def __init__(self, index):
        import pynvml

        self.nvml = pynvml
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        name = pynvml.nvmlDeviceGetName(self.handle)
        self.device = {
            "physical_index": index,
            "name": name.decode() if isinstance(name, bytes) else name,
            "uuid": pynvml.nvmlDeviceGetUUID(self.handle),
            "total_bytes": pynvml.nvmlDeviceGetMemoryInfo(self.handle).total,
        }
        self.samples, self.errors = [], []
        self.stop = threading.Event()
        self.start_s = time.perf_counter()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                memory = self.nvml.nvmlDeviceGetMemoryInfo(self.handle)
                processes = self.nvml.nvmlDeviceGetComputeRunningProcesses(self.handle)
                own = next((p for p in processes if p.pid == os.getpid()), None)
                rss = next(
                    int(line.split()[1]) * 1024
                    for line in Path("/proc/self/status").read_text().splitlines()
                    if line.startswith("VmRSS:")
                )
                self.samples.append(
                    {
                        "elapsed_s": time.perf_counter() - self.start_s,
                        "process_gpu_bytes": int(own.usedGpuMemory) if own else 0,
                        "whole_gpu_bytes": int(memory.used),
                        "process_rss_bytes": rss,
                    }
                )
            except Exception as error:
                self.errors.append(type(error).__name__)
            self.stop.wait(0.2)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)
        self.nvml.nvmlShutdown()

    def summary(self):
        if not self.samples:
            raise RuntimeError("GPU monitor produced no samples")
        return {
            "device": self.device,
            "sample_interval_s": 0.2,
            "sample_count": len(self.samples),
            "process_peak_gpu_bytes": max(s["process_gpu_bytes"] for s in self.samples),
            "whole_device_peak_gpu_bytes": max(
                s["whole_gpu_bytes"] for s in self.samples
            ),
            "process_peak_rss_bytes": max(s["process_rss_bytes"] for s in self.samples),
            "sampling_errors": sorted(set(self.errors)),
            "samples": self.samples,
        }
