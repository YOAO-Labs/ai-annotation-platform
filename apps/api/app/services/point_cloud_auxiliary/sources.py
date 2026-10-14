"""Resolve exact task bindings and authoritative per-exposure coordinates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas._jsonb_types import SensorCalibration
from app.schemas.point_cloud_auxiliary import PointCloudEnhancementConfig
from app.services.axis_convention import R_NORM
from app.services.sensor_calibration import resolve_calibration_states
from app.services.storage import storage_service

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_CAMERAS = 6


class AuxiliaryError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def transform(rotation: list[float], translation: list[float]) -> np.ndarray:
    q = np.asarray(rotation, dtype=np.float64)
    t = np.asarray(translation, dtype=np.float64)
    if (
        q.shape != (4,)
        or t.shape != (3,)
        or not np.isfinite(q).all()
        or not np.isfinite(t).all()
        or np.linalg.norm(q) < 1e-12
    ):
        raise AuxiliaryError("coordinate_source_invalid")
    w, x, y, z = q / np.linalg.norm(q)
    result = np.eye(4)
    result[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    result[:3, 3] = t
    return result


def global_from_sensor(source: dict) -> np.ndarray:
    try:
        pose, sensor = source["ego_pose"], source["calibrated_sensor"]
        return transform(pose["rotation"], pose["translation"]) @ transform(
            sensor["rotation"], sensor["translation"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuxiliaryError("coordinate_source_invalid") from exc


def global_from_primary(source: dict, frame: str) -> np.ndarray:
    try:
        pose = source["ego_pose"]
        ego = transform(pose["rotation"], pose["translation"])
        if frame == "ego":
            return ego
        if frame in {"lidar", "sensor"}:
            return global_from_sensor(source)
    except (KeyError, TypeError) as exc:
        raise AuxiliaryError("coordinate_source_invalid") from exc
    raise AuxiliaryError("coordinate_frame_unknown")


def nuscenes_calibration(
    primary_source: dict, camera_source: dict, frame: str
) -> SensorCalibration:
    """Compute camera-from-primary at the two actual acquisition timestamps."""
    try:
        primary_sample = primary_source["sample"]["token"]
        if camera_source["sample"]["token"] != primary_sample:
            raise AuxiliaryError("camera_acquisition_mismatch")
        matrix = np.linalg.inv(global_from_sensor(camera_source)) @ global_from_primary(
            primary_source, frame
        )
        intrinsic = np.asarray(
            camera_source["calibrated_sensor"]["camera_intrinsic"], dtype=float
        )
        return SensorCalibration(
            extrinsic=matrix.reshape(-1).tolist(),
            intrinsic=intrinsic.reshape(-1).tolist(),
        )
    except (KeyError, TypeError, np.linalg.LinAlgError) as exc:
        raise AuxiliaryError("coordinate_source_invalid") from exc


def validate_calibration(calibration: SensorCalibration) -> None:
    k = np.asarray(calibration.intrinsic, dtype=float).reshape(3, 3)
    e = np.asarray(calibration.extrinsic, dtype=float).reshape(4, 4)
    matrices = [e]
    if calibration.rect is not None:
        matrices.append(np.asarray(calibration.rect, dtype=float).reshape(4, 4))
    if (
        not np.isfinite(k).all()
        or k[0, 0] <= 0
        or k[1, 1] <= 0
        or not np.allclose(k[2], [0, 0, 1], atol=1e-6)
    ):
        raise AuxiliaryError("camera_calibration_invalid")
    for matrix in matrices:
        rotation = matrix[:3, :3]
        if (
            not np.isfinite(matrix).all()
            or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-6)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-4)
        ):
            raise AuxiliaryError("camera_calibration_invalid")


def object_fingerprint(item: DatasetItem, storage=storage_service) -> dict:
    """Verify trusted content hashes and freeze storage mutation indicators."""
    content_hash = (item.content_hash or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", content_hash):
        raise AuxiliaryError("source_hash_missing")
    head = storage.verify_upload(item.file_path, bucket=storage.datasets_bucket)
    if not head:
        raise AuxiliaryError("source_object_missing")
    size = int(head.get("ContentLength", -1))
    if (
        size < 1
        or size > MAX_SOURCE_BYTES
        or (item.file_size is not None and size != item.file_size)
    ):
        raise AuxiliaryError("source_size_mismatch")
    metadata_hash = (head.get("Metadata", {}).get("sha256") or "").lower()
    if metadata_hash:
        if metadata_hash != content_hash:
            raise AuxiliaryError("source_hash_mismatch")
    else:
        response = storage.client.get_object(
            Bucket=storage.datasets_bucket, Key=item.file_path
        )
        body = response["Body"]
        try:
            digest, read = hashlib.sha256(), 0
            while chunk := body.read(1024 * 1024):
                read += len(chunk)
                if read > MAX_SOURCE_BYTES:
                    raise AuxiliaryError("source_size_mismatch")
                digest.update(chunk)
            if read != size or digest.hexdigest() != content_hash:
                raise AuxiliaryError("source_hash_mismatch")
        finally:
            body.close()
    return {
        "sha256": content_hash,
        "byte_size": size,
        "etag": str(head.get("ETag") or "").strip('"'),
        "version_id": head.get("VersionId"),
    }


@dataclass(frozen=True)
class FrozenItem:
    id: uuid.UUID
    dataset_id: uuid.UUID
    file_path: str
    content_hash: str
    file_size: int
    fingerprint: dict
    source_context: dict

    def dump_manifest(self) -> dict:
        return {
            "id": str(self.id),
            "dataset_id": str(self.dataset_id),
            "file_path": self.file_path,
            "content_hash": self.content_hash,
            "file_size": self.file_size,
            "fingerprint": self.fingerprint,
            "source_context": self.source_context,
        }


@dataclass(frozen=True)
class FrozenCamera:
    role: str
    name: str
    item: FrozenItem
    width: int
    height: int
    calibration: SensorCalibration
    calibration_revision: int
    calibration_digest: str

    @property
    def file_path(self) -> str:
        return self.item.file_path

    def dump_manifest(self) -> dict:
        return {
            "role": self.role,
            "name": self.name,
            "item": self.item.dump_manifest(),
            "width": self.width,
            "height": self.height,
            "calibration": self.calibration.model_dump(mode="json"),
            "calibration_revision": self.calibration_revision,
            "calibration_digest": self.calibration_digest,
        }


@dataclass(frozen=True)
class FrozenInputs:
    primary: FrozenItem
    cameras: tuple[FrozenCamera, ...]
    axis_convention: str
    profile: str
    binding_digest: str
    input_digest: str
    primary_frame: str | None

    def dump_manifest(self) -> dict:
        return {
            "schema_version": 1,
            "primary": self.primary.dump_manifest(),
            "cameras": [camera.dump_manifest() for camera in self.cameras],
            "axis_convention": self.axis_convention,
            "coordinate_frame": "primary_pointcloud",
            "primary_frame": self.primary_frame,
            "profile": self.profile,
            "binding_digest": self.binding_digest,
            "input_digest": self.input_digest,
        }


async def resolve_task_inputs(
    db: AsyncSession,
    task: Task,
    profile: str | None = None,
    *,
    verify_objects: bool = True,
    lock: bool = False,
    storage=storage_service,
) -> FrozenInputs:
    """No writes or implicit repair. Locking is for a publisher's final fence."""
    if lock:
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            raise AuxiliaryError("task_missing")
    project_query = select(Project).where(Project.id == task.project_id)
    if lock:
        project_query = project_query.with_for_update()
    project = (
        await db.execute(project_query.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if project is None or project.data_type != "lidar":
        raise AuxiliaryError("not_point_cloud")
    config = PointCloudEnhancementConfig.model_validate(
        project.point_cloud_enhancement_config or {}
    )
    profile = profile or config.profile
    if profile not in {"omega-current-mono-512", "omega-current-mono-512-full-lidar"}:
        raise AuxiliaryError("unsupported_profile")
    query = (
        select(TaskDatasetItemLink)
        .where(TaskDatasetItemLink.task_id == task.id)
        .order_by(TaskDatasetItemLink.role)
    )
    if lock:
        query = query.with_for_update()
    links = list(
        (await db.execute(query.execution_options(populate_existing=True))).scalars()
    )
    primary_link = next((link for link in links if link.role == "primary_lidar"), None)
    camera_links = [link for link in links if link.role.startswith("camera_")]
    if primary_link is None or task.dataset_item_id != primary_link.dataset_item_id:
        raise AuxiliaryError("primary_dataset_item_required")
    if not 1 <= len(camera_links) <= MAX_CAMERAS:
        raise AuxiliaryError("camera_binding_unsupported")
    query = (
        select(DatasetItem)
        .where(DatasetItem.id.in_([link.dataset_item_id for link in links]))
        .order_by(DatasetItem.id)
    )
    if lock:
        query = query.with_for_update()
    items = {
        item.id: item
        for item in (
            await db.execute(query.execution_options(populate_existing=True))
        ).scalars()
    }
    primary = items.get(primary_link.dataset_item_id)
    if (
        primary is None
        or primary.file_type != "point_cloud"
        or task.file_path != primary.file_path
    ):
        raise AuxiliaryError("primary_binding_mismatch")
    dataset_query = select(Dataset).where(Dataset.id == primary.dataset_id)
    if lock:
        dataset_query = dataset_query.with_for_update()
    dataset = (
        await db.execute(dataset_query.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    association_query = select(ProjectDataset.id).where(
        ProjectDataset.project_id == task.project_id,
        ProjectDataset.dataset_id == primary.dataset_id,
    )
    if lock:
        association_query = association_query.with_for_update()
    linked = await db.scalar(association_query)
    if dataset is None or linked is None:
        raise AuxiliaryError("dataset_not_linked")
    axis = (dataset.metadata_ or {}).get("axis_convention") or "iso_8855"
    if axis not in R_NORM or axis == "raw":
        raise AuxiliaryError("axis_convention_unsupported")
    scene = None
    if primary.scene_id:
        scene_query = select(Scene).where(Scene.id == primary.scene_id)
        if lock:
            scene_query = scene_query.with_for_update()
        scene = (
            await db.execute(scene_query.execution_options(populate_existing=True))
        ).scalar_one_or_none()
    primary_frame = (scene.source_metadata or {}).get("frame") if scene else None
    if scene and scene.source_format == "nuscenes":
        allowed_axes = (
            {"iso_8855", "ros_rep103"}
            if primary_frame == "ego"
            else {"apollo", "y_forward"}
        )
        if axis not in allowed_axes:
            raise AuxiliaryError("coordinate_axis_mismatch")
    camera_items = []
    for link in camera_links:
        if not re.fullmatch(r"camera_[A-Za-z0-9_-]+", link.role):
            raise AuxiliaryError("camera_binding_unsupported")
        item = items.get(link.dataset_item_id)
        if (
            item is None
            or item.file_type != "image"
            or item.dataset_id != primary.dataset_id
            or item.scene_id != primary.scene_id
            or item.frame_index != primary.frame_index
        ):
            raise AuxiliaryError("camera_binding_mismatch")
        if (
            not item.width
            or not item.height
            or item.width > 32768
            or item.height > 32768
        ):
            raise AuxiliaryError("camera_dimensions_missing")
        camera_items.append(item)
    states = await resolve_calibration_states(db, camera_items)
    frozen: dict[uuid.UUID, FrozenItem] = {}
    for item in [primary, *camera_items]:
        fingerprint = (
            await asyncio.to_thread(object_fingerprint, item, storage)
            if verify_objects
            else {"sha256": item.content_hash, "byte_size": item.file_size}
        )
        frozen[item.id] = FrozenItem(
            item.id,
            item.dataset_id,
            item.file_path,
            item.content_hash or "",
            item.file_size or int(fingerprint["byte_size"]),
            fingerprint,
            json.loads(json.dumps((item.metadata_ or {}).get("nuscenes_export") or {})),
        )
    cameras = []
    for link in camera_links:
        item = items[link.dataset_item_id]
        state = states[item.id]
        validate_calibration(state.calibration)
        if frozen[primary.id].source_context or frozen[item.id].source_context:
            expected = nuscenes_calibration(
                frozen[primary.id].source_context,
                frozen[item.id].source_context,
                primary_frame,
            )
            if (
                not np.allclose(
                    state.calibration.extrinsic, expected.extrinsic, atol=1e-5, rtol=0
                )
                or not np.allclose(
                    state.calibration.intrinsic, expected.intrinsic, atol=1e-5, rtol=0
                )
                or state.calibration.rect is not None
            ):
                raise AuxiliaryError("coordinate_calibration_mismatch")
        cameras.append(
            FrozenCamera(
                link.role,
                link.role[7:],
                frozen[item.id],
                item.width,
                item.height,
                state.calibration,
                state.revision,
                state.digest,
            )
        )
    binding = digest_json(
        [{"role": camera.role, "item_id": str(camera.item.id)} for camera in cameras]
    )
    result = FrozenInputs(
        frozen[primary.id], tuple(cameras), axis, profile, binding, "", primary_frame
    )
    manifest = result.dump_manifest()
    manifest.pop("input_digest")
    return FrozenInputs(
        result.primary,
        result.cameras,
        axis,
        profile,
        binding,
        digest_json(manifest),
        primary_frame,
    )
