"""LiDAR standard training-format serializers.

Pure serializer helpers for point-cloud exports. They intentionally do not
depend on routers or database sessions so packaging code can test and reuse
them directly.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.db.models.annotation import Annotation
from aap_assets_catalog.spatial_values import SensorCalibration
from aap_assets_catalog.axis_convention import PsrDict, _euler_xyz_to_mat3
from app.services.pointcloud_projection import (
    NEAR_PLANE as _NEAR_PLANE,
    apply_camera_point as _apply_camera_point,
    apply_camera_vector as _apply_camera_vector,
    box_iso_geometry as _box_iso_geometry,
    clamp as _clamp,
    clipped_projection_bbox as _clipped_projection_bbox,
    iso_to_source as _iso_to_source,
    pixel_bbox,
)


@dataclass(frozen=True)
class BoxExportAttrs:
    occluded: int = 0


@dataclass(frozen=True)
class LidarCameraExportCtx:
    role: str
    name: str
    calibration: SensorCalibration
    width: int
    height: int


@dataclass(frozen=True)
class NuScenesSensorExportCtx:
    role: str
    dataset_item_id: uuid.UUID
    sample_data: dict[str, Any]
    calibrated_sensor: dict[str, Any]
    sensor: dict[str, Any]
    ego_pose: dict[str, Any]
    source_storage_key: str


@dataclass(frozen=True)
class LidarFrameExportCtx:
    task_id: uuid.UUID
    frame_key: str
    annotations: list[Annotation]
    axis_convention: str
    cameras: dict[str, LidarCameraExportCtx] = field(default_factory=dict)
    scene_id: uuid.UUID | None = None
    scene_name: str | None = None
    scene_source_metadata: dict[str, Any] = field(default_factory=dict)
    source_sample: dict[str, Any] = field(default_factory=dict)
    frame_index: int | None = None
    timestamp_us: int | None = None
    ego_translation: list[float] | None = None
    ego_rotation: list[float] | None = None
    sensors: dict[str, NuScenesSensorExportCtx] = field(default_factory=dict)
    point_counts: dict[uuid.UUID, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MulticameraCocoImageCtx:
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID
    sensor_role: str
    file_name: str
    width: int
    height: int
    members: list[Annotation] = field(default_factory=list)
    scene_id: uuid.UUID | None = None
    frame_index: int | None = None
    current_calibration_revision: int | None = None
    current_calibration_digest: str | None = None


@dataclass(frozen=True)
class MulticameraCocoExportResult:
    document: dict[str, Any]
    image_count: int
    annotation_count: int
    stale_relation_count: int
    images_by_role: dict[str, int]
    annotations_by_role: dict[str, int]


@dataclass(frozen=True)
class KittiSkippedAnnotation:
    annotation_id: str
    class_name: str
    reason: str


@dataclass(frozen=True)
class KittiFrameExportResult:
    lines: list[str]
    skipped: list[KittiSkippedAnnotation]
    manual_bbox_count: int = 0
    derived_bbox_count: int = 0


_JS_SAFE_INTEGER_MAX = (1 << 53) - 1


def _stable_coco_id(kind: str, identity: str, seen: dict[int, str]) -> int:
    digest = hashlib.sha256(f"{kind}\0{identity}".encode()).digest()
    value = int.from_bytes(digest[:7], "big") >> 3
    value = value or 1
    if value > _JS_SAFE_INTEGER_MAX:
        raise ValueError("coco_id_not_javascript_safe")
    previous = seen.setdefault(value, identity)
    if previous != identity:
        raise ValueError(f"coco_id_collision:{kind}:{value}")
    return value


def build_multicamera_coco(
    images: list[MulticameraCocoImageCtx],
    *,
    classes: list[str],
    include_attributes: bool,
    allowed_attribute_keys: set[str],
) -> MulticameraCocoExportResult:
    """Build one deterministic COCO Instances document from manual camera members."""

    category_ids = {name: index + 1 for index, name in enumerate(classes)}
    if len(category_ids) != len(classes):
        raise ValueError("coco_category_duplicate")
    seen_image_ids: dict[int, str] = {}
    seen_annotation_ids: dict[int, str] = {}
    coco_images: list[dict[str, Any]] = []
    coco_annotations: list[dict[str, Any]] = []
    images_by_role: dict[str, int] = {}
    annotations_by_role: dict[str, int] = {}
    stale_relation_count = 0

    ordered_images = sorted(
        images,
        key=lambda image: (
            image.sensor_role,
            str(image.task_id),
            str(image.dataset_item_id),
        ),
    )
    for image in ordered_images:
        if image.width <= 0 or image.height <= 0:
            raise ValueError(
                f"multicamera_coco_image_size_invalid:{image.dataset_item_id}"
            )
        image_identity = f"{image.task_id}:{image.dataset_item_id}:{image.sensor_role}"
        image_id = _stable_coco_id("image", image_identity, seen_image_ids)
        image_row: dict[str, Any] = {
            "id": image_id,
            "file_name": image.file_name,
            "width": image.width,
            "height": image.height,
            "task_id": str(image.task_id),
            "dataset_item_id": str(image.dataset_item_id),
            "sensor_role": image.sensor_role,
        }
        if image.scene_id is not None:
            image_row["scene_id"] = str(image.scene_id)
        if image.frame_index is not None:
            image_row["frame_index"] = image.frame_index
        coco_images.append(image_row)
        images_by_role[image.sensor_role] = images_by_role.get(image.sensor_role, 0) + 1

        for member in sorted(image.members, key=lambda annotation: str(annotation.id)):
            geometry = member.geometry or {}
            if geometry.get("type") != "bbox":
                raise ValueError(f"multicamera_coco_bbox_invalid:{member.id}")
            try:
                normalized_x = float(geometry["x"])
                normalized_y = float(geometry["y"])
                normalized_width = float(geometry["w"])
                normalized_height = float(geometry["h"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"multicamera_coco_bbox_invalid:{member.id}") from exc
            if (
                not all(
                    math.isfinite(value)
                    for value in (
                        normalized_x,
                        normalized_y,
                        normalized_width,
                        normalized_height,
                    )
                )
                or normalized_x < 0
                or normalized_y < 0
                or normalized_width <= 0
                or normalized_height <= 0
                or normalized_x + normalized_width > 1 + 1e-9
                or normalized_y + normalized_height > 1 + 1e-9
            ):
                raise ValueError(f"multicamera_coco_bbox_invalid:{member.id}")
            if (
                member.task_id != image.task_id
                or member.sensor_role != image.sensor_role
                or member.sensor_dataset_item_id != image.dataset_item_id
                or member.scene_track_id is None
                or not member.track_id
                or member.sensor_visibility is None
                or member.calibration_revision is None
                or member.calibration_revision < 1
                or member.calibration_digest is None
                or len(member.calibration_digest) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in member.calibration_digest.lower()
                )
            ):
                raise ValueError(f"multicamera_coco_member_context_invalid:{member.id}")
            x = normalized_x * image.width
            y = normalized_y * image.height
            width = normalized_width * image.width
            height = normalized_height * image.height
            category_id = category_ids.get(str(member.class_name))
            if category_id is None:
                raise ValueError(f"multicamera_coco_category_unknown:{member.id}")
            annotation_identity = f"{member.id}:{member.version}"
            annotation_id = _stable_coco_id(
                "annotation", annotation_identity, seen_annotation_ids
            )
            relation_status = (
                "current"
                if image.current_calibration_digest is not None
                and member.calibration_digest == image.current_calibration_digest
                else "stale"
            )
            if relation_status == "stale":
                stale_relation_count += 1
            annotation_row: dict[str, Any] = {
                "id": annotation_id,
                "image_id": image_id,
                "category_id": category_id,
                "bbox": [x, y, width, height],
                "area": width * height,
                "iscrowd": 0,
                "annotation_id": str(member.id),
                "scene_track_id": str(member.scene_track_id),
                "track_id": member.track_id,
                "sensor_role": image.sensor_role,
                "sensor_visibility": member.sensor_visibility,
                "calibration_revision": member.calibration_revision,
                "calibration_digest": member.calibration_digest,
                "current_calibration_revision": image.current_calibration_revision,
                "current_calibration_digest": image.current_calibration_digest,
                "relation_status": relation_status,
            }
            if include_attributes:
                annotation_row["attributes"] = {
                    key: value
                    for key, value in (member.attributes or {}).items()
                    if key in allowed_attribute_keys
                }
            coco_annotations.append(annotation_row)
            annotations_by_role[image.sensor_role] = (
                annotations_by_role.get(image.sensor_role, 0) + 1
            )

    document = {
        "info": {
            "description": "AAP trusted multi-camera manual bbox export",
            "version": "1",
        },
        "licenses": [],
        "images": coco_images,
        "annotations": coco_annotations,
        "categories": [
            {"id": category_ids[name], "name": name, "supercategory": ""}
            for name in classes
        ],
        "aap": {
            "contract": "multicamera-coco-manual-bbox-v1",
            "annotation_source": "persistent_manual_camera_bbox",
            "derived_projection_fallback": False,
        },
    }
    return MulticameraCocoExportResult(
        document=document,
        image_count=len(coco_images),
        annotation_count=len(coco_annotations),
        stale_relation_count=stale_relation_count,
        images_by_role=dict(sorted(images_by_role.items())),
        annotations_by_role=dict(sorted(annotations_by_role.items())),
    )


def _wrap_pi(value: float) -> float:
    while value > math.pi:
        value -= math.tau
    while value <= -math.pi:
        value += math.tau
    return value


def _map_box_attributes(attrs: dict | None) -> BoxExportAttrs:
    raw = attrs or {}
    try:
        occluded = int(_clamp(float(raw.get("occluded", 0) or 0), 0, 3))
    except (TypeError, ValueError):
        occluded = 0
    return BoxExportAttrs(occluded=occluded)


_KITTI_OCCLUSION_BY_SENSOR_VISIBILITY = {
    "visible": 0,
    "occluded": 1,
    "truncated": 0,
    "unknown": 3,
}


def _box_psr(geometry: dict) -> PsrDict:
    return {
        "center": [float(v) for v in (geometry.get("center") or [0, 0, 0])[:3]],
        "size": [float(v) for v in (geometry.get("size") or [0, 0, 0])[:3]],
        "rotation": [float(v) for v in (geometry.get("rotation") or [0, 0, 0])[:3]],
    }


def _kitti_alpha(ry: float, x: float, z: float) -> float:
    if abs(x) < 1e-9 and abs(z) < 1e-9:
        return _wrap_pi(ry)
    return _wrap_pi(ry - math.atan2(x, z))


def build_kitti_lidar_frame(
    annotations: list[Annotation],
    *,
    camera: LidarCameraExportCtx,
    axis_convention: str,
    camera_members: list[Annotation] | None = None,
) -> KittiFrameExportResult:
    """Project ISO-frame cuboids into one trusted KITTI camera frame."""

    lines: list[str] = []
    skipped: list[KittiSkippedAnnotation] = []
    manual_bbox_count = 0
    derived_bbox_count = 0
    manual_by_track = {
        member.scene_track_id: member
        for member in (camera_members or [])
        if member.is_active
        and not member.was_cancelled
        and member.sensor_role == camera.role
        and member.scene_track_id is not None
        and (member.geometry or {}).get("type") == "bbox"
    }
    for ann in annotations:
        geometry = ann.geometry or {}
        if geometry.get("type") != "box_3d":
            continue
        psr = _box_psr(geometry)
        length, width, height = psr["size"]
        if not all(
            math.isfinite(value) and value > 0 for value in (length, width, height)
        ):
            skipped.append(
                KittiSkippedAnnotation(
                    str(ann.id), str(ann.class_name), "invalid_geometry"
                )
            )
            continue
        corners_iso, bottom_iso, forward_iso = _box_iso_geometry(psr)
        corners_camera = [
            _apply_camera_point(
                _iso_to_source(point, axis_convention), camera.calibration
            )
            for point in corners_iso
        ]
        manual_member = manual_by_track.get(ann.scene_track_id)
        projection = _clipped_projection_bbox(corners_camera, camera)
        if projection is None:
            reason = (
                "behind_camera"
                if all(point[2] < _NEAR_PLANE for point in corners_camera)
                else "outside_image_or_degenerate"
            )
            skipped.append(
                KittiSkippedAnnotation(str(ann.id), str(ann.class_name), reason)
            )
            continue
        derived_bbox, truncated = projection
        if manual_member is not None:
            manual = manual_member.geometry or {}
            bbox = pixel_bbox(
                tuple(float(manual[key]) for key in ("x", "y", "w", "h")),
                camera.width,
                camera.height,
            )
            truncated = 0.0
            manual_bbox_count += 1
        else:
            bbox = derived_bbox
            derived_bbox_count += 1
        x, y, z = _apply_camera_point(
            _iso_to_source(bottom_iso, axis_convention), camera.calibration
        )
        forward_camera = _apply_camera_vector(
            _iso_to_source(forward_iso, axis_convention), camera.calibration
        )
        if math.hypot(forward_camera[0], forward_camera[2]) <= 1e-9:
            skipped.append(
                KittiSkippedAnnotation(
                    str(ann.id), str(ann.class_name), "degenerate_orientation"
                )
            )
            continue
        ry = _wrap_pi(math.atan2(-forward_camera[2], forward_camera[0]))
        alpha = _kitti_alpha(ry, x, z)
        attrs = _map_box_attributes(ann.attributes)
        occluded = (
            _KITTI_OCCLUSION_BY_SENSOR_VISIBILITY.get(
                manual_member.sensor_visibility or "unknown", 3
            )
            if manual_member is not None
            else attrs.occluded
        )
        x1, y1, x2, y2 = bbox
        lines.append(
            " ".join(
                [
                    str(ann.class_name),
                    f"{truncated:.6f}",
                    str(occluded),
                    f"{alpha:.6f}",
                    f"{x1:.2f}",
                    f"{y1:.2f}",
                    f"{x2:.2f}",
                    f"{y2:.2f}",
                    f"{height:.6f}",
                    f"{width:.6f}",
                    f"{length:.6f}",
                    f"{x:.6f}",
                    f"{y:.6f}",
                    f"{z:.6f}",
                    f"{ry:.6f}",
                ]
            )
        )
    return KittiFrameExportResult(
        lines=lines,
        skipped=skipped,
        manual_bbox_count=manual_bbox_count,
        derived_bbox_count=derived_bbox_count,
    )


def build_kitti_lidar_label_lines(
    annotations: list[Annotation],
    *,
    camera: LidarCameraExportCtx,
    axis_convention: str,
) -> list[str]:
    return build_kitti_lidar_frame(
        annotations,
        camera=camera,
        axis_convention=axis_convention,
    ).lines


def build_nuscenes_frame_records(
    frames: list[LidarFrameExportCtx],
) -> dict[str, list[dict[str, Any]]]:
    if not frames:
        raise ValueError("nuscenes_export_empty")

    ordered = sorted(
        frames,
        key=lambda frame: (
            str(frame.scene_id or ""),
            -1 if frame.frame_index is None else frame.frame_index,
            str(frame.task_id),
        ),
    )
    records: dict[str, list[dict[str, Any]]] = {
        "attribute": [],
        "calibrated_sensor": [],
        "category": [],
        "ego_pose": [],
        "instance": [],
        "log": [],
        "map": [],
        "sample": [],
        "sample_annotation": [],
        "sample_data": [],
        "scene": [],
        "sensor": [],
        "visibility": [],
    }

    scene_groups: dict[uuid.UUID, list[LidarFrameExportCtx]] = {}
    for frame in ordered:
        if (
            frame.scene_id is None
            or frame.frame_index is None
            or frame.timestamp_us is None
            or frame.ego_translation is None
            or frame.ego_rotation is None
            or not frame.source_sample
        ):
            raise ValueError(f"nuscenes_frame_contract_missing:{frame.task_id}")
        if not frame.sensors:
            raise ValueError(f"nuscenes_sensor_contract_missing:{frame.task_id}")
        scene_groups.setdefault(frame.scene_id, []).append(frame)

    category_names = sorted(
        {
            str(ann.class_name)
            for frame in ordered
            for ann in frame.annotations
            if (ann.geometry or {}).get("type") == "box_3d"
        }
    )
    category_tokens = {
        name: _nuscenes_token("category", name) for name in category_names
    }
    records["category"] = [
        {
            "token": category_tokens[name],
            "name": name,
            "description": f"AAP project category {name}",
            "index": index,
        }
        for index, name in enumerate(category_names, start=1)
    ]

    log_by_token: dict[str, dict[str, Any]] = {}
    map_by_token: dict[str, dict[str, Any]] = {}
    sensor_by_token: dict[str, dict[str, Any]] = {}
    calibrated_by_token: dict[str, dict[str, Any]] = {}
    ego_by_token: dict[str, dict[str, Any]] = {}
    sample_token_by_frame: dict[tuple[uuid.UUID, int], str] = {}
    sample_data_groups: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}
    annotation_groups: dict[str, list[dict[str, Any]]] = {}
    instance_categories: dict[str, str] = {}

    for scene_id, scene_frames in scene_groups.items():
        source = (scene_frames[0].scene_source_metadata or {}).get("nuscenes_export")
        if not isinstance(source, dict):
            raise ValueError(f"nuscenes_scene_source_missing:{scene_id}")
        source_scene = source.get("scene")
        source_log = source.get("log")
        source_map = source.get("map")
        if not all(
            isinstance(value, dict) for value in (source_scene, source_log, source_map)
        ):
            raise ValueError(f"nuscenes_scene_metadata_incomplete:{scene_id}")

        for field_name in ("token", "logfile", "vehicle", "date_captured", "location"):
            _required_source_value(source_log, field_name, "log")
        for field_name in ("token", "category", "filename"):
            _required_source_value(source_map, field_name, "map")
        for field_name in (
            "token",
            "name",
            "description",
            "log_token",
            "nbr_samples",
            "first_sample_token",
            "last_sample_token",
        ):
            _required_source_value(
                source_scene,
                field_name,
                "scene",
                allow_empty=field_name == "description",
            )
        if str(source_scene["log_token"]) != str(source_log["token"]) or str(
            source_log["token"]
        ) not in source_map.get("log_tokens", []):
            raise ValueError(f"nuscenes_scene_log_map_reference_invalid:{scene_id}")

        log_token = _nuscenes_token("log", _canonical_json(source_log))
        log_by_token.setdefault(
            log_token,
            {
                "token": log_token,
                "logfile": str(source_log["logfile"]),
                "vehicle": str(source_log["vehicle"]),
                "date_captured": str(source_log["date_captured"]),
                "location": str(source_log["location"]),
            },
        )
        source_map_token = str(source_map["token"])
        map_token = _nuscenes_token("map", source_map_token)
        map_record = map_by_token.setdefault(
            map_token,
            {
                "token": map_token,
                "log_tokens": [],
                "category": str(source_map["category"]),
                "filename": str(source_map["filename"]),
            },
        )
        if log_token not in map_record["log_tokens"]:
            map_record["log_tokens"].append(log_token)

        scene_frames.sort(key=lambda frame: int(frame.frame_index or 0))
        frame_indices = [int(frame.frame_index or 0) for frame in scene_frames]
        try:
            expected_count = int(source_scene["nbr_samples"])
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"nuscenes_scene_frame_gap:{scene_id}") from exc
        if len(frame_indices) != expected_count or any(
            frame_index != index for index, frame_index in enumerate(frame_indices)
        ):
            raise ValueError(f"nuscenes_scene_frame_gap:{scene_id}")
        source_samples = [frame.source_sample for frame in scene_frames]
        source_tokens = [str(sample.get("token") or "") for sample in source_samples]
        if (
            not all(source_tokens)
            or source_tokens[0] != str(source_scene["first_sample_token"])
            or source_tokens[-1] != str(source_scene["last_sample_token"])
        ):
            raise ValueError(f"nuscenes_scene_sample_boundary_invalid:{scene_id}")
        for index, source_sample in enumerate(source_samples):
            expected_prev = source_tokens[index - 1] if index else ""
            expected_next = (
                source_tokens[index + 1] if index + 1 < len(source_tokens) else ""
            )
            if (
                str(source_sample.get("prev") or "") != expected_prev
                or str(source_sample.get("next") or "") != expected_next
                or source_sample.get("timestamp") is None
            ):
                raise ValueError(f"nuscenes_scene_sample_chain_invalid:{scene_id}")
        sample_tokens = [
            _nuscenes_token("sample", scene_id, frame.frame_index)
            for frame in scene_frames
        ]
        for index, (frame, sample_token) in enumerate(
            zip(scene_frames, sample_tokens, strict=True)
        ):
            sample_token_by_frame[(scene_id, int(frame.frame_index or 0))] = (
                sample_token
            )
            records["sample"].append(
                {
                    "token": sample_token,
                    "timestamp": int(frame.source_sample["timestamp"]),
                    "scene_token": _nuscenes_token("scene", scene_id),
                    "next": sample_tokens[index + 1]
                    if index + 1 < len(sample_tokens)
                    else "",
                    "prev": sample_tokens[index - 1] if index else "",
                }
            )
        records["scene"].append(
            {
                "token": _nuscenes_token("scene", scene_id),
                "name": str(source_scene["name"]),
                "description": str(source_scene["description"]),
                "log_token": log_token,
                "nbr_samples": expected_count,
                "first_sample_token": sample_tokens[0],
                "last_sample_token": sample_tokens[-1],
            }
        )

        for frame in scene_frames:
            sample_token = sample_token_by_frame[
                (scene_id, int(frame.frame_index or 0))
            ]
            lidar_sensor_count = 0
            for role, source_sensor in sorted(frame.sensors.items()):
                sensor = source_sensor.sensor
                calibrated = source_sensor.calibrated_sensor
                ego_pose = source_sensor.ego_pose
                sample_data = source_sensor.sample_data
                if not source_sensor.source_storage_key:
                    raise ValueError("nuscenes_source_storage_key_missing")
                for field_name in ("token", "channel", "modality"):
                    _required_source_value(sensor, field_name, "sensor")
                for field_name in (
                    "token",
                    "sensor_token",
                    "translation",
                    "rotation",
                    "camera_intrinsic",
                ):
                    _required_source_value(
                        calibrated,
                        field_name,
                        "calibrated_sensor",
                        allow_empty=field_name == "camera_intrinsic",
                    )
                for field_name in ("token", "translation", "rotation", "timestamp"):
                    _required_source_value(ego_pose, field_name, "ego_pose")
                for field_name in (
                    "token",
                    "sample_token",
                    "ego_pose_token",
                    "calibrated_sensor_token",
                    "filename",
                    "fileformat",
                    "width",
                    "height",
                    "timestamp",
                    "is_key_frame",
                ):
                    _required_source_value(
                        sample_data,
                        field_name,
                        "sample_data",
                        allow_empty=field_name in {"width", "height"},
                    )
                if (
                    str(calibrated["sensor_token"]) != str(sensor["token"])
                    or str(sample_data["calibrated_sensor_token"])
                    != str(calibrated["token"])
                    or str(sample_data["ego_pose_token"]) != str(ego_pose["token"])
                    or str(sample_data["sample_token"])
                    != str(frame.source_sample["token"])
                    or sample_data["is_key_frame"] is not True
                ):
                    raise ValueError(
                        f"nuscenes_sensor_reference_invalid:{frame.task_id}:{role}"
                    )
                if str(sensor["modality"]) == "lidar":
                    lidar_sensor_count += 1
                sensor_token = _nuscenes_token("sensor", _canonical_json(sensor))
                calibrated_token = _nuscenes_token(
                    "calibrated_sensor", _canonical_json(calibrated)
                )
                ego_token = _nuscenes_token("ego_pose", _canonical_json(ego_pose))
                sample_data_token = _nuscenes_token(
                    "sample_data",
                    source_sensor.dataset_item_id,
                    sample_data.get("token"),
                )
                sensor_by_token.setdefault(
                    sensor_token,
                    {
                        "token": sensor_token,
                        "channel": str(sensor["channel"]),
                        "modality": str(sensor["modality"]),
                    },
                )
                calibrated_by_token.setdefault(
                    calibrated_token,
                    {
                        "token": calibrated_token,
                        "sensor_token": sensor_token,
                        "translation": [
                            float(v) for v in calibrated.get("translation") or []
                        ],
                        "rotation": _normalized_quaternion(calibrated.get("rotation")),
                        "camera_intrinsic": [
                            [float(v) for v in row]
                            for row in (calibrated.get("camera_intrinsic") or [])
                        ],
                    },
                )
                ego_by_token.setdefault(
                    ego_token,
                    {
                        "token": ego_token,
                        "translation": [
                            float(v) for v in ego_pose.get("translation") or []
                        ],
                        "rotation": _normalized_quaternion(ego_pose.get("rotation")),
                        "timestamp": int(
                            ego_pose.get("timestamp")
                            or sample_data.get("timestamp")
                            or 0
                        ),
                    },
                )
                data_record = {
                    "token": sample_data_token,
                    "sample_token": sample_token,
                    "ego_pose_token": ego_token,
                    "calibrated_sensor_token": calibrated_token,
                    "filename": str(sample_data["filename"]),
                    "fileformat": str(sample_data["fileformat"]),
                    "width": int(sample_data["width"]),
                    "height": int(sample_data["height"]),
                    "timestamp": int(sample_data["timestamp"]),
                    "is_key_frame": bool(sample_data["is_key_frame"]),
                    "next": "",
                    "prev": "",
                }
                records["sample_data"].append(data_record)
                sample_data_groups.setdefault(
                    (scene_id, str(sensor["channel"])), []
                ).append(data_record)

            if lidar_sensor_count != 1:
                raise ValueError(f"nuscenes_lidar_sensor_count_invalid:{frame.task_id}")

            for ann in frame.annotations:
                geometry = ann.geometry or {}
                if geometry.get("type") != "box_3d":
                    continue
                raw_size = geometry.get("size")
                raw_center = geometry.get("center")
                raw_rotation = geometry.get("rotation")
                if not all(
                    isinstance(value, list) and len(value) == 3
                    for value in (raw_size, raw_center, raw_rotation)
                ):
                    raise ValueError(f"nuscenes_box_geometry_invalid:{ann.id}")
                length, width, height = [float(v) for v in raw_size]
                local_center = [float(v) for v in raw_center]
                local_rotation = [float(v) for v in raw_rotation]
                if (
                    not all(
                        math.isfinite(value)
                        for value in (
                            length,
                            width,
                            height,
                            *local_center,
                            *local_rotation,
                        )
                    )
                    or min(length, width, height) <= 0
                ):
                    raise ValueError(f"nuscenes_box_geometry_invalid:{ann.id}")
                ego_rotation = _normalized_quaternion(frame.ego_rotation)
                global_center = [
                    left + right
                    for left, right in zip(
                        _quaternion_rotate(ego_rotation, local_center),
                        [float(v) for v in frame.ego_translation or []],
                        strict=True,
                    )
                ]
                global_rotation = _normalized_quaternion(
                    _quaternion_multiply(
                        ego_rotation, _euler_xyz_quaternion(local_rotation)
                    )
                )
                instance_key = str(ann.scene_track_id or ann.track_id or ann.id)
                instance_token = _nuscenes_token("instance", scene_id, instance_key)
                annotation_record = {
                    "token": _nuscenes_token("sample_annotation", ann.id, ann.version),
                    "sample_token": sample_token,
                    "instance_token": instance_token,
                    "attribute_tokens": [],
                    "visibility_token": "",
                    "translation": global_center,
                    "size": [width, length, height],
                    "rotation": global_rotation,
                    "num_lidar_pts": int(frame.point_counts[ann.id]),
                    "num_radar_pts": 0,
                    "next": "",
                    "prev": "",
                    "_frame_index": int(frame.frame_index or 0),
                }
                records["sample_annotation"].append(annotation_record)
                annotation_groups.setdefault(instance_token, []).append(
                    annotation_record
                )
                previous_category = instance_categories.setdefault(
                    instance_token, str(ann.class_name)
                )
                if previous_category != str(ann.class_name):
                    raise ValueError(
                        f"nuscenes_instance_category_drift:{instance_token}"
                    )

    for group in sample_data_groups.values():
        group.sort(key=lambda row: (row["timestamp"], row["token"]))
        for index, row in enumerate(group):
            row["prev"] = group[index - 1]["token"] if index else ""
            row["next"] = group[index + 1]["token"] if index + 1 < len(group) else ""

    for instance_token, group in annotation_groups.items():
        group.sort(key=lambda row: (row["_frame_index"], row["token"]))
        for index, row in enumerate(group):
            row["prev"] = group[index - 1]["token"] if index else ""
            row["next"] = group[index + 1]["token"] if index + 1 < len(group) else ""
            row.pop("_frame_index", None)
        records["instance"].append(
            {
                "token": instance_token,
                "category_token": category_tokens[instance_categories[instance_token]],
                "nbr_annotations": len(group),
                "first_annotation_token": group[0]["token"],
                "last_annotation_token": group[-1]["token"],
            }
        )

    records["log"] = sorted(log_by_token.values(), key=lambda row: row["token"])
    records["map"] = sorted(map_by_token.values(), key=lambda row: row["token"])
    records["sensor"] = sorted(sensor_by_token.values(), key=lambda row: row["token"])
    records["calibrated_sensor"] = sorted(
        calibrated_by_token.values(), key=lambda row: row["token"]
    )
    records["ego_pose"] = sorted(ego_by_token.values(), key=lambda row: row["token"])
    records["scene"].sort(key=lambda row: row["token"])
    records["sample"].sort(key=lambda row: (row["timestamp"], row["token"]))
    records["sample_data"].sort(key=lambda row: (row["timestamp"], row["token"]))
    records["sample_annotation"].sort(
        key=lambda row: (row["sample_token"], row["token"])
    )
    records["instance"].sort(key=lambda row: row["token"])
    validate_nuscenes_records(records)
    return records


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _required_source_value(
    row: dict[str, Any],
    field_name: str,
    table: str,
    *,
    allow_empty: bool = False,
) -> object:
    if field_name not in row or row[field_name] is None:
        raise ValueError(f"nuscenes_source_field_missing:{table}:{field_name}")
    value = row[field_name]
    if not allow_empty and value == "":
        raise ValueError(f"nuscenes_source_field_empty:{table}:{field_name}")
    return value


def _nuscenes_token(kind: str, *parts: object) -> str:
    payload = "\x1f".join((kind, *[str(part) for part in parts]))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _normalized_quaternion(raw: object) -> list[float]:
    values = [float(v) for v in (raw or [])]
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError("nuscenes_quaternion_invalid")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("nuscenes_quaternion_invalid")
    return [v / norm for v in values]


def _quaternion_multiply(left: list[float], right: list[float]) -> list[float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return [
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    ]


def _quaternion_rotate(quaternion: list[float], point: list[float]) -> list[float]:
    _, x, y, z = _quaternion_multiply(
        _quaternion_multiply(quaternion, [0.0, *point]),
        [quaternion[0], -quaternion[1], -quaternion[2], -quaternion[3]],
    )
    return [x, y, z]


def _euler_xyz_quaternion(rotation: list[float]) -> list[float]:
    rx, ry, rz = rotation
    c1, c2, c3 = math.cos(rx / 2), math.cos(ry / 2), math.cos(rz / 2)
    s1, s2, s3 = math.sin(rx / 2), math.sin(ry / 2), math.sin(rz / 2)
    return [
        c1 * c2 * c3 - s1 * s2 * s3,
        s1 * c2 * c3 + c1 * s2 * s3,
        c1 * s2 * c3 - s1 * c2 * s3,
        c1 * c2 * s3 + s1 * s2 * c3,
    ]


def validate_nuscenes_records(records: dict[str, list[dict[str, Any]]]) -> None:
    expected = {
        "attribute",
        "calibrated_sensor",
        "category",
        "ego_pose",
        "instance",
        "log",
        "map",
        "sample",
        "sample_annotation",
        "sample_data",
        "scene",
        "sensor",
        "visibility",
    }
    if set(records) != expected:
        raise ValueError("nuscenes_table_set_invalid")
    if not records["log"] or not records["map"] or not records["scene"]:
        raise ValueError("nuscenes_required_table_empty")
    tokens: dict[str, set[str]] = {}
    for table, rows in records.items():
        table_tokens = {str(row.get("token") or "") for row in rows}
        if "" in table_tokens or len(table_tokens) != len(rows):
            raise ValueError(f"nuscenes_token_invalid:{table}")
        tokens[table] = table_tokens

    foreign_keys = {
        "calibrated_sensor": (("sensor_token", "sensor"),),
        "instance": (("category_token", "category"),),
        "sample": (("scene_token", "scene"),),
        "sample_annotation": (
            ("sample_token", "sample"),
            ("instance_token", "instance"),
        ),
        "sample_data": (
            ("sample_token", "sample"),
            ("ego_pose_token", "ego_pose"),
            ("calibrated_sensor_token", "calibrated_sensor"),
        ),
        "scene": (("log_token", "log"),),
    }
    for table, relations in foreign_keys.items():
        for row in records[table]:
            for field_name, target in relations:
                if row.get(field_name) not in tokens[target]:
                    raise ValueError(
                        f"nuscenes_foreign_key_invalid:{table}:{field_name}"
                    )
    for row in records["map"]:
        if not row.get("filename") or not row.get("log_tokens"):
            raise ValueError("nuscenes_map_invalid")
        if any(token not in tokens["log"] for token in row["log_tokens"]):
            raise ValueError("nuscenes_map_log_invalid")
    for row in records["sample_data"]:
        if (
            not row.get("filename")
            or not row.get("fileformat")
            or row.get("timestamp") is None
            or row.get("is_key_frame") is not True
        ):
            raise ValueError("nuscenes_sample_data_invalid")
    for row in records["sample_annotation"]:
        if int(row.get("num_lidar_pts", -1)) < 0:
            raise ValueError("nuscenes_annotation_point_count_invalid")
    for table in ("sample", "sample_data", "sample_annotation"):
        by_token = {row["token"]: row for row in records[table]}
        for row in records[table]:
            for direction, inverse in (("next", "prev"), ("prev", "next")):
                target = row.get(direction) or ""
                if target and (
                    target not in by_token
                    or by_token[target].get(inverse) != row["token"]
                ):
                    raise ValueError(f"nuscenes_chain_invalid:{table}:{direction}")
    sample_count_by_scene: dict[str, int] = {}
    for sample in records["sample"]:
        scene_token = str(sample["scene_token"])
        sample_count_by_scene[scene_token] = (
            sample_count_by_scene.get(scene_token, 0) + 1
        )
    annotations_by_instance: dict[str, list[dict[str, Any]]] = {}
    for annotation in records["sample_annotation"]:
        annotations_by_instance.setdefault(
            str(annotation["instance_token"]), []
        ).append(annotation)
    for row in records["scene"]:
        if (
            row["first_sample_token"] not in tokens["sample"]
            or row["last_sample_token"] not in tokens["sample"]
        ):
            raise ValueError("nuscenes_scene_sample_invalid")
        if sample_count_by_scene.get(str(row["token"]), 0) != row["nbr_samples"]:
            raise ValueError("nuscenes_scene_count_invalid")
    for row in records["instance"]:
        group = annotations_by_instance.get(str(row["token"]), [])
        group_tokens = {annotation["token"] for annotation in group}
        if (
            len(group) != row["nbr_annotations"]
            or row["first_annotation_token"] not in group_tokens
            or row["last_annotation_token"] not in group_tokens
        ):
            raise ValueError("nuscenes_instance_count_invalid")


def count_lidar_points_in_boxes(
    points: np.ndarray, annotations: list[Annotation]
) -> dict[uuid.UUID, int]:
    finite = np.asarray(points, dtype=np.float64)
    finite = finite[np.isfinite(finite).all(axis=1)]
    counts: dict[uuid.UUID, int] = {}
    for ann in annotations:
        geometry = ann.geometry or {}
        if geometry.get("type") != "box_3d":
            continue
        center = np.asarray(geometry.get("center") or [], dtype=np.float64)
        size = np.asarray(geometry.get("size") or [], dtype=np.float64)
        rotation = [float(v) for v in geometry.get("rotation") or []]
        if center.shape != (3,) or size.shape != (3,) or len(rotation) != 3:
            raise ValueError(f"nuscenes_box_geometry_invalid:{ann.id}")
        matrix = np.asarray(
            _euler_xyz_to_mat3(*rotation),
            dtype=np.float64,
        ).reshape(3, 3)
        local = (finite - center) @ matrix
        counts[ann.id] = int(
            np.count_nonzero(np.all(np.abs(local) <= size / 2 + 1e-6, axis=1))
        )
    return counts


def build_pointmask_label_bytes(
    annotations: list[Annotation],
    *,
    source_point_count: int | None,
    category_map: dict[str, int],
) -> bytes:
    """Label the verified original record space, preserving annotation order."""
    from app.services.exporting.lidar_pcd import MAX_PCD_POINTS, MAX_POINTMASK_INDICES

    if source_point_count is None:
        raise ValueError("pointmask_point_count_required")
    if type(source_point_count) is not int or source_point_count < 0:
        raise ValueError("pointmask_point_count_invalid")
    if source_point_count > MAX_PCD_POINTS:
        raise ValueError("pointmask_point_budget_exceeded")
    masks = []
    total_indices = 0
    for ann in annotations:
        geometry = ann.geometry if ann.geometry is not None else {}
        if not isinstance(geometry, dict):
            raise ValueError("pointmask_geometry_invalid")
        if geometry.get("type") != "point_mask_3d":
            continue
        declared = geometry.get("source_point_count")
        if declared is not None and (
            type(declared) is not int or declared != source_point_count
        ):
            raise ValueError("pointmask_point_count_mismatch")
        indices = geometry.get("point_indices", [])
        if not isinstance(indices, list):
            raise ValueError("pointmask_indices_invalid")
        total_indices += len(indices)
        if total_indices > MAX_POINTMASK_INDICES:
            raise ValueError("pointmask_index_budget_exceeded")
        if not isinstance(ann.class_name, str) or ann.class_name not in category_map:
            raise ValueError("pointmask_category_unknown")
        class_id = category_map[ann.class_name]
        if type(class_id) is not int or not 1 <= class_id <= 0xFFFFFFFF:
            raise ValueError("pointmask_category_invalid")
        for index in indices:
            if type(index) is not int:
                raise ValueError("pointmask_index_invalid")
            if not 0 <= index < source_point_count:
                raise ValueError("pointmask_index_out_of_range")
        masks.append((indices, class_id))
    labels = bytearray(4 * source_point_count)
    for indices, class_id in masks:
        for index in indices:
            struct.pack_into("<I", labels, 4 * index, class_id)
    return bytes(labels)


def category_map_json(classes_list: list[str]) -> str:
    return json.dumps(
        {name: idx + 1 for idx, name in enumerate(classes_list)},
        ensure_ascii=False,
        indent=2,
    )
