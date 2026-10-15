"""Verify the preserved current-frame experiment before importing copies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
from PIL import Image

from .codec import MAX_DERIVED_BYTES
from .publication import MODEL_SOURCE_COMMIT, WEIGHT_SHA256
from .sources import (
    AuxiliaryError,
    FrozenInputs,
    global_from_primary,
    global_from_sensor,
    transform,
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_npz(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        sizes = [entry.file_size for entry in archive.infolist()]
        if (
            len(sizes) > 16
            or sum(sizes) > MAX_DERIVED_BYTES
            or any(
                entry.is_dir() or not entry.filename.endswith(".npy")
                for entry in archive.infolist()
            )
        ):
            raise AuxiliaryError("prepared_archive_invalid")
    with np.load(path, allow_pickle=False) as cloud:
        return {key: cloud[key] for key in cloud.files}


def prepare_experiment_frame(
    bundle: Path, *, manifest_sha256: str, frame: int, inputs: FrozenInputs
) -> tuple[dict, dict]:
    """Scene/frame labels select files; acquisition tokens/hashes bind them."""
    bundle = bundle.resolve(strict=True)
    manifest_path = bundle / "current-manifest.json"
    if file_sha256(manifest_path) != manifest_sha256:
        raise AuxiliaryError("prepared_manifest_mismatch")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") != "completed":
        raise AuxiliaryError("prepared_bundle_incomplete")
    records = {entry["path"]: entry for entry in manifest["files"]}

    def verified(name: str) -> Path:
        path = (bundle / name).resolve(strict=True)
        if (
            not path.is_relative_to(bundle)
            or name not in records
            or path.stat().st_size != records[name]["bytes"]
            or file_sha256(path) != records[name]["sha256"]
        ):
            raise AuxiliaryError("prepared_artifact_mismatch")
        return path

    protocol_path = verified("current-protocol.json")
    protocol = json.loads(protocol_path.read_text())
    inference = json.loads(verified("current-inference.json").read_text())
    build = json.loads(verified("current-build.json").read_text())
    build_protocol = json.loads(verified("build-protocol.json").read_text())
    verified("protocol-amendments.json")
    if (
        inputs.profile != "omega-current-mono-512"
        or protocol.get("source_commit") != MODEL_SOURCE_COMMIT
        or protocol.get("weight_sha256") != WEIGHT_SHA256
        or inference.get("status") != "completed"
        or inference.get("protocol_sha256") != file_sha256(protocol_path)
        or build.get("status") != "completed"
        or build.get("model_output_cross_time_accumulated") is not False
        or build.get("oracle_boxes_used_for_model_selection") is not False
    ):
        raise AuxiliaryError("prepared_profile_mismatch")
    scans = [scan for scan in protocol["scans"] if scan["frame"] == frame]
    views = [view for view in protocol["views"] if view["frame"] == frame]
    if len(scans) != 1 or len(views) != 6 or len(inputs.cameras) != 6:
        raise AuxiliaryError("prepared_binding_mismatch")
    scan = scans[0]
    source = inputs.primary.source_context
    if scan["token"] != source.get("sample_data", {}).get("token") or scan[
        "sha256"
    ] != source.get("source_sha256"):
        raise AuxiliaryError("prepared_acquisition_mismatch")
    if not np.allclose(
        np.asarray(scan["global_from_sensor"]),
        global_from_sensor(source),
        atol=1e-7,
        rtol=0,
    ):
        raise AuxiliaryError("prepared_transform_mismatch")
    data_root = Path(protocol["data_root"]).resolve(strict=True)

    def original(sensor: dict) -> Path:
        path = Path(sensor["path"]).resolve(strict=True)
        if not path.is_relative_to(data_root) or file_sha256(path) != sensor["sha256"]:
            raise AuxiliaryError("prepared_source_mismatch")
        return path

    raw_path = original(scan)
    # The primary PCD must represent exactly these raw rows in the declared
    # frame, including order. A matching scene name cannot establish this.
    from scripts.import_nuscenes_scene import _lidar_bin_to_binary_pcd

    sensor = source["calibrated_sensor"]
    expected_pcd = _lidar_bin_to_binary_pcd(
        raw_path,
        transform=transform(sensor["rotation"], sensor["translation"])
        if inputs.primary_frame == "ego"
        else None,
    )
    if hashlib.sha256(expected_pcd).hexdigest() != inputs.primary.content_hash:
        raise AuxiliaryError("primary_coordinate_rows_mismatch")
    confidence = _bounded_npz(verified(f"predictions/{frame:02d}-mono.npz"))[
        "confidence"
    ]
    if confidence.shape != (6, 352, 640):
        raise AuxiliaryError("prepared_prediction_shape_invalid")
    inverse = np.linalg.inv(global_from_primary(source, inputs.primary_frame))
    clouds = {}
    source_files = {}
    for camera in inputs.cameras:
        matches = [
            (index, view)
            for index, view in enumerate(views)
            if view["token"]
            == camera.item.source_context.get("sample_data", {}).get("token")
        ]
        if len(matches) != 1:
            raise AuxiliaryError("prepared_acquisition_mismatch")
        index, view = matches[0]
        context = camera.item.source_context
        if (
            view["sha256"] != camera.item.content_hash
            or view["sha256"] != context.get("source_sha256")
            or view["camera"] != context.get("sensor", {}).get("channel")
            or not np.allclose(
                np.asarray(view["global_from_sensor"]),
                global_from_sensor(context),
                atol=1e-7,
                rtol=0,
            )
        ):
            raise AuxiliaryError("prepared_transform_mismatch")
        image_path = original(view)
        cloud_name = f"clouds/{frame:02d}-mono-{index}.npz"
        cloud = _bounded_npz(verified(cloud_name))
        if (
            set(cloud) != {"xyz", "rgb", "pixel", "source", "kind"}
            or cloud["xyz"].ndim != 2
            or cloud["xyz"].shape[1] != 3
            or not np.all(cloud["kind"] == 1)
            or not np.all(cloud["source"] == frame * 6 + index)
        ):
            raise AuxiliaryError("prepared_cloud_invalid")
        pixel = cloud["pixel"]
        if (
            pixel.ndim != 1
            or len(pixel) != len(cloud["xyz"])
            or pixel.dtype.kind not in "iu"
            or np.any(pixel < 0)
            or np.any(pixel >= 352 * 640)
        ):
            raise AuxiliaryError("prepared_pixel_invalid")
        with Image.open(image_path) as image:
            rgb = np.asarray(
                image.convert("RGB").resize((640, 352), Image.Resampling.BICUBIC)
            )
        if not np.array_equal(cloud["rgb"], rgb.reshape(-1, 3)[pixel]):
            raise AuxiliaryError("prepared_rgb_mismatch")
        points = np.asarray(cloud["xyz"], dtype=np.float64)
        cloud["xyz"] = (points @ inverse[:3, :3].T + inverse[:3, 3]).astype(np.float32)
        cloud["confidence"] = confidence[index].reshape(-1)[pixel]
        clouds[camera.role] = cloud
        source_files[cloud_name] = records[cloud_name]["sha256"]
    provenance = {
        "producer": "verified-current-frame-experiment-import",
        "profile": inputs.profile,
        "model_source_commit": MODEL_SOURCE_COMMIT,
        "weight_sha256": WEIGHT_SHA256,
        "preprocessing": protocol["preprocess"],
        "filtering": {
            "candidate_policy": build_protocol["candidate_policy"],
            "ground_truth_used": False,
            "cross_time_model_accumulation": False,
        },
        "calibration_policy": "frozen-current-scan-training-partition-80-percent",
        "bundle_manifest_sha256": manifest_sha256,
        "protocol_sha256": file_sha256(protocol_path),
        "source_files": source_files,
        "input_coordinate_frame": "nuscenes_global",
        "output_coordinate_frame": "primary_pointcloud",
    }
    return clouds, provenance
