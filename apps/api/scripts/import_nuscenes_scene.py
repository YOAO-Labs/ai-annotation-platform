"""v0.14.2 · nuScenes-mini → 本平台原生目录结构 + 入库(MinIO + DB)一次性转换脚本。

把 nuScenes 数据集的一个或多个 scene 转成本平台点云原生目录结构,并 **显式调用
v0.14.0 的 scene service** 建 scene + 给 items 赋 frame_index(不依赖
build_pointcloud_tasks_for_link 的目录启发式 single-scene inference)。

不引入 nuscenes-devkit 依赖——自己读 JSON。只依赖 numpy + Pillow(均已是项目依赖)。

数据下载:https://www.nuscenes.org/nuscenes#download (取 "Mini" split,解压后
根目录形如 <root>/v1.0-mini/*.json + <root>/samples/<CHANNEL>/*.{jpg,pcd.bin})。

用法示例:
    cd apps/api

    # 单 scene(注意 PYTHONPATH=. 才能 import app.*,与其他 seed 脚本一致)
    PYTHONPATH=. uv run python scripts/import_nuscenes_scene.py \
        --nuscenes-root /data/nuscenes-mini \
        --scene-tokens scene-0061 \
        --dataset-name nu-scene-0061 \
        --frame ego

    # 多 scene 共用 dataset(验证 v0.14.0 多 scene 隔离)
    PYTHONPATH=. uv run python scripts/import_nuscenes_scene.py \
        --nuscenes-root /data/nuscenes-mini \
        --scene-tokens scene-0061,scene-0103,scene-0553 \
        --dataset-name nu-mini-multi

注意 `--scene-tokens` 实际匹配 scene.json 的 `name` 字段(如 scene-0061),不是 token。

平台原生逻辑目录约定(保存于 storage_relative_path)；实际对象由受控 Creation 分配，
帧 stem 用 <scene_name>_<idx 6位> 保证跨 scene 全局唯一
(group_frames 以文件名 stem 作帧键,多 scene 同号帧会撞键):
    <storage_root>/<scene_name>/lidar/<scene_name>_<idx 6位>.pcd            file_type=point_cloud
    <storage_root>/<scene_name>/camera/<CHANNEL>/<scene_name>_<idx 6位>.jpg  file_type=image
    <storage_root>/<scene_name>/calib/camera/<CHANNEL>.json                 file_type=other

标定:本版用 **每个 scene 第 1 帧** 的标定对全 scene 通用(metadata 记
timestamp_delta_us 备查;前端不消费;逐帧真补偿留 v0.15+)。

幂等:dataset 按派生 display_id 复用；Scene、原始表和原件证明必须完全一致。
已有 canonical 原件完整回读后复用，历史未注册数据只能通过显式本地重导创建新证明。

================================ 手动测试 checklist ================================
跑真实 nuScenes-mini 后人工验证(浏览器 + curl,token 自备):
  1. 脚本 stdout 报告:每个 scene 的 frames 数 = nbr_samples,total_items 合理。
  2. 浏览器打开任一 scene 第 1 帧 BEV:默认 --frame ego 时车头朝上
     (点已变换到 ego/ISO 系,axis_convention=iso_8855)。
  3. 6 路相机投影:点云投到各 CAM 图像上大致对齐(用第 1 帧标定,首帧最准)。
  4. curl /scenes?dataset_id=<id> 返回 N 个 scene(= 传入的 scene 数)。
  5. curl /tasks/<某 scene 末帧 task>/neighbors:next 为空,不串到下一个 scene 首帧。
  6. curl -X POST /datasets/<id>/sniff-axis-convention 命中 iso_8855。
     若显式 --frame sensor,则保留 v0.14.2 raw LIDAR_TOP/apollo 行为。
  7. (若已有 v0.14.1)工作台 Shift+→ 翻到 scene 末帧后不跳到下一 scene。
====================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
import uuid
from pathlib import Path, PurePosixPath
from typing import Literal

import numpy as np

# nuScenes 默认 6 相机通道(modality=camera 全收,这里仅作文档参考)。
_NUSCENES_CAM_CHANNELS = (
    "CAM_FRONT",
    "CAM_FRONT_RIGHT",
    "CAM_BACK_RIGHT",
    "CAM_BACK",
    "CAM_BACK_LEFT",
    "CAM_FRONT_LEFT",
)

_DISPLAY_ID_MAX_LENGTH = 20
_DISPLAY_ID_HASH_LENGTH = 8


def _derived_display_id(prefix: str, name: str) -> str:
    """Return a deterministic display_id that fits the current DB varchar(20)."""
    display_id = f"{prefix}{name}"
    if len(display_id) <= _DISPLAY_ID_MAX_LENGTH:
        return display_id

    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_DISPLAY_ID_HASH_LENGTH]
    head_len = _DISPLAY_ID_MAX_LENGTH - len(prefix) - 1 - len(digest)
    if head_len <= 0:
        raise ValueError(f"display_id prefix {prefix!r} is too long")
    return f"{prefix}{name[:head_len]}-{digest}"


# --------------------------------------------------------------------------- #
# nuScenes JSON 读取 + 索引
# --------------------------------------------------------------------------- #
def _load_table(meta_dir: Path, name: str) -> list[dict]:
    """读 <meta_dir>/<name>.json(nuScenes 表都是 list[dict])。"""
    root = meta_dir.resolve(strict=True)
    path = (root / f"{name}.json").resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"nuScenes metadata table escapes its root: {name}.json"
        ) from exc
    if not path.is_file():
        raise FileNotFoundError(f"nuScenes 元数据表缺失: {path}")
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size > 256 * 1024**2:
            raise ValueError("nuScenes metadata table exceeds byte budget")
        payload = stream.read(256 * 1024**2 + 1)
        if len(payload) > 256 * 1024**2:
            raise ValueError("nuScenes metadata table exceeds byte budget")
    rows = json.loads(payload)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("nuScenes metadata table must contain records")
    return rows


def _index_by_token(rows: list[dict]) -> dict[str, dict]:
    return {row["token"]: row for row in rows}


def _camera_dimensions(sample_data: dict) -> tuple[int, int]:
    width = int(sample_data.get("width") or 0)
    height = int(sample_data.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError(
            f"nuScenes camera sample_data {sample_data.get('token')!r} "
            "has no valid width/height"
        )
    return width, height


def _safe_source_path(filename: str) -> PurePosixPath:
    raw = str(filename)
    raw_parts = raw.split("/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or "\\" in raw
        or ":" in raw
        or "\0" in raw
    ):
        raise ValueError(f"nuScenes source filename is unsafe: {filename!r}")
    return path


def _resolve_source_path(nuscenes_root: Path, filename: str) -> Path:
    root = nuscenes_root.resolve(strict=True)
    candidate = (root / _safe_source_path(filename)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"nuScenes source path escapes its root: {filename!r}"
        ) from exc
    return candidate


def _source_storage_key(storage_root: str, filename: str) -> str:
    root = _safe_source_path(storage_root)
    path = _safe_source_path(filename)
    return "/".join((*root.parts, "_nuscenes", "source", *path.parts))


def _map_for_log(rows: list[dict], log_token: str) -> dict:
    matches = [row for row in rows if log_token in (row.get("log_tokens") or [])]
    if len(matches) != 1:
        raise ValueError(
            f"nuScenes log {log_token!r} must reference exactly one map, got {len(matches)}"
        )
    return matches[0]


def _source_context(
    *,
    sample: dict,
    sample_data: dict,
    calibrated_sensor: dict,
    sensor: dict,
    ego_pose: dict,
    storage_key: str,
    source_bytes: bytes,
) -> dict:
    return {
        "sample": sample,
        "sample_data": sample_data,
        "calibrated_sensor": calibrated_sensor,
        "sensor": sensor,
        "ego_pose": ego_pose,
        "source_storage_key": storage_key,
        "source_file_size": len(source_bytes),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
    }


def _assert_source_context_compatible(
    existing: object,
    *,
    sample: dict,
    sample_data: dict,
    calibrated_sensor: dict,
    sensor: dict,
    ego_pose: dict,
    source_bytes: bytes,
) -> None:
    if existing is None:
        return
    if not isinstance(existing, dict):
        raise ValueError("existing nuScenes source context is invalid")
    existing_sample = existing.get("sample")
    existing_sample_data = existing.get("sample_data")
    if (
        not isinstance(existing_sample, dict)
        or not isinstance(existing_sample_data, dict)
        or existing_sample != sample
        or existing_sample_data != sample_data
        or existing.get("calibrated_sensor") != calibrated_sensor
        or existing.get("sensor") != sensor
        or existing.get("ego_pose") != ego_pose
        or existing.get("source_sha256") != hashlib.sha256(source_bytes).hexdigest()
    ):
        raise ValueError(
            f"existing scene source drift for sample_data {sample_data.get('token')!r}"
        )


# --------------------------------------------------------------------------- #
# 几何:四元数 wxyz → 旋转矩阵;变换矩阵组装;lidar→camera 外参链
# --------------------------------------------------------------------------- #
def _quat_wxyz_to_rot(q: list[float]) -> np.ndarray:
    """nuScenes rotation 是 [w, x, y, z] 四元数 → 3x3 旋转矩阵。"""
    try:
        values = [float(value) for value in q]
        norm = math.hypot(*values)
    except (TypeError, ValueError) as exc:
        raise ValueError("nuScenes quaternion is invalid") from exc
    if len(values) != 4 or not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("nuScenes quaternion is invalid")
    w, x, y, z = (value / norm for value in values)
    s = 2.0
    wx, wy, wz = s * w * x, s * w * y, s * w * z
    xx, xy, xz = s * x * x, s * x * y, s * x * z
    yy, yz, zz = s * y * y, s * y * z, s * z * z
    return np.array(
        [
            [1.0 - (yy + zz), xy - wz, xz + wy],
            [xy + wz, 1.0 - (xx + zz), yz - wx],
            [xz - wy, yz + wx, 1.0 - (xx + yy)],
        ]
    )


def _transform(rotation: list[float], translation: list[float]) -> np.ndarray:
    """由四元数(wxyz)+ 平移构造 4x4 齐次变换矩阵(source→target)。"""
    T = np.eye(4)
    T[:3, :3] = _quat_wxyz_to_rot(rotation)
    T[:3, 3] = np.asarray(translation, dtype=float)
    return T


def _compute_lidar_to_cam_extrinsic(
    *,
    cs_lidar: dict,
    ego_lidar: dict,
    cs_cam: dict,
    ego_cam: dict,
) -> list[float]:
    """标准 nuScenes 投影链:lidar 帧 → camera 帧的 4x4 外参,拍平成 row-major 16-float。

        T_cam_from_lidar =
            inv(T_ego_from_cam) @ inv(T_global_from_egoCam)
            @ T_global_from_egoLidar @ T_ego_from_lidar

    calibrated_sensor 的 (rotation, translation) 表示 sensor→ego;
    ego_pose 的 (rotation, translation) 表示 ego→global。
    """
    T_ego_from_lidar = _transform(cs_lidar["rotation"], cs_lidar["translation"])
    T_global_from_egoLidar = _transform(ego_lidar["rotation"], ego_lidar["translation"])
    T_ego_from_cam = _transform(cs_cam["rotation"], cs_cam["translation"])
    T_global_from_egoCam = _transform(ego_cam["rotation"], ego_cam["translation"])

    T_cam_from_lidar = (
        np.linalg.inv(T_ego_from_cam)
        @ np.linalg.inv(T_global_from_egoCam)
        @ T_global_from_egoLidar
        @ T_ego_from_lidar
    )
    return [float(v) for v in T_cam_from_lidar.reshape(-1)]


def _compute_ego_to_cam_extrinsic(
    *,
    ego_lidar: dict,
    cs_cam: dict,
    ego_cam: dict,
) -> list[float]:
    """nuScenes 投影链:lidar timestamp ego 帧 → camera 帧的 4x4 外参。

    ego 模式下点已先从 LIDAR_TOP sensor 系变换到 lidar sample 的 ego 系,所以
    外参链不再包含 T_ego_from_lidar。
    """
    T_global_from_egoLidar = _transform(ego_lidar["rotation"], ego_lidar["translation"])
    T_ego_from_cam = _transform(cs_cam["rotation"], cs_cam["translation"])
    T_global_from_egoCam = _transform(ego_cam["rotation"], ego_cam["translation"])

    T_cam_from_ego = (
        np.linalg.inv(T_ego_from_cam)
        @ np.linalg.inv(T_global_from_egoCam)
        @ T_global_from_egoLidar
    )
    return [float(v) for v in T_cam_from_ego.reshape(-1)]


# --------------------------------------------------------------------------- #
# .pcd.bin → binary PCD
# --------------------------------------------------------------------------- #
PCD_ENCODING = "binary_xyz_f32"


def _lidar_bin_to_binary_pcd(
    bin_path: Path,
    *,
    transform: np.ndarray | None = None,
) -> bytes:
    """nuScenes LIDAR_TOP .pcd.bin 是 float32 数组,每点 5 个 float (x y z intensity ring)。
    只取前 3 列写成标准 little-endian binary PCD(x y z)。"""
    pts = np.fromfile(
        bin_path if hasattr(bin_path, "read") else str(bin_path), dtype="<f4"
    ).reshape(-1, 5)[:, :3]
    if transform is not None and pts.size:
        pts = pts @ transform[:3, :3].T + transform[:3, 3]
    with np.errstate(over="ignore", invalid="ignore"):
        pts = np.ascontiguousarray(pts, dtype="<f4")
    if not np.isfinite(pts).all():
        raise ValueError(f"nuScenes lidar contains non-finite points: {bin_path}")
    n = pts.shape[0]
    header = (
        "# .PCD v0.7 - Point Cloud Data file format\n"
        "VERSION 0.7\n"
        "FIELDS x y z\n"
        "SIZE 4 4 4\n"
        "TYPE F F F\n"
        "COUNT 1 1 1\n"
        f"WIDTH {n}\n"
        "HEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {n}\n"
        "DATA binary\n"
    )
    return header.encode("ascii") + pts.tobytes(order="C")


# --------------------------------------------------------------------------- #
# scene 遍历:沿 sample.next 链得到有序 sample 列表
# --------------------------------------------------------------------------- #
def _ordered_samples(scene: dict, samples_by_token: dict[str, dict]) -> list[dict]:
    """从 first_sample_token 沿 next 链得到该 scene 全部 sample(按时间顺序)。"""
    out: list[dict] = []
    token = scene["first_sample_token"]
    visited: set[str] = set()
    while token:
        if token in visited:
            raise ValueError(f"nuScenes sample chain contains a cycle at {token!r}")
        visited.add(token)
        sample = samples_by_token.get(token)
        if sample is None:
            raise ValueError(
                f"nuScenes sample chain references missing token {token!r}"
            )
        out.append(sample)
        token = sample.get("next") or ""
    try:
        expected_count = int(scene["nbr_samples"])
    except (KeyError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError("nuScenes scene nbr_samples is invalid") from exc
    if expected_count < 1 or len(out) != expected_count:
        raise ValueError(
            f"nuScenes sample chain length {len(out)} != nbr_samples {expected_count}"
        )
    return out


def _key_sample_data_by_channel(
    sample_token: str,
    sample_data: list[dict],
    cs_by_token: dict[str, dict],
    sensor_by_token: dict[str, dict],
) -> dict[str, dict]:
    """该 sample 下、is_key_frame==True 的 sample_data,按 sensor channel 归集。
    返回 {channel: sample_data_row}(每 channel 一条 keyframe)。"""
    out: dict[str, dict] = {}
    for sd in sample_data:
        if sd["sample_token"] != sample_token or not sd.get("is_key_frame"):
            continue
        cs = cs_by_token[sd["calibrated_sensor_token"]]
        sensor = sensor_by_token[cs["sensor_token"]]
        out[sensor["channel"]] = sd
    return out


@contextmanager
def _local_stage(root, filename):
    from app.services.managed_original_reader import BinaryStage
    from app.services.storage_access import close_registered

    path = _resolve_source_path(root, filename)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    stage = None
    temporary = None
    try:
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 <= before.st_size <= 256 * 1024**2
            ):
                raise ValueError("nuScenes source exceeds regular file/byte budget")
            temporary = tempfile.TemporaryFile(mode="w+b", prefix="aap-nuscenes-")
            digest, size = hashlib.sha256(), 0
            while block := source.read(min(1024**2, before.st_size - size + 1)):
                size += len(block)
                if size > before.st_size or size > 256 * 1024**2:
                    raise ValueError("nuScenes source changed while staging")
                if temporary.write(block) != len(block):
                    raise OSError("nuScenes stage short write")
                digest.update(block)
            after = os.fstat(source.fileno())
            if size != before.st_size or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("nuScenes source changed while staging")
        temporary.seek(0)
        stage = BinaryStage(temporary, digest.hexdigest(), size, {})
        yield stage
    finally:
        if stage is not None:
            stage.close()
        elif temporary is not None:
            close_registered(temporary)


@contextmanager
def _bytes_stage(payload):
    from app.services.managed_original_reader import BinaryStage

    if len(payload) > 256 * 1024**2:
        raise ValueError("nuScenes generated media exceeds byte budget")
    with tempfile.TemporaryFile(
        mode="w+b", prefix="aap-nuscenes-generated-"
    ) as temporary:
        temporary.write(payload)
        temporary.seek(0)
        stage = BinaryStage(
            temporary, hashlib.sha256(payload).hexdigest(), len(payload), {}
        )
        try:
            yield stage
        finally:
            stage.close()


# --------------------------------------------------------------------------- #
# 核心:可被 pytest 直接驱动的 async 入口
# --------------------------------------------------------------------------- #
async def import_nuscenes(
    db,
    *,
    nuscenes_root: Path,
    scene_names: list[str],
    dataset_name: str,
    owner_id: uuid.UUID,
    version: str = "v1.0-mini",
    frame: Literal["sensor", "ego"] = "ego",
    dataset_display_id: str | None = None,
    project_display_id: str | None = None,
    project_name: str | None = None,
    tool_bindings: dict | None = None,
) -> dict:
    """Import explicit local NuScenes originals through controlled server creation.

    This is an authorized internal CLI/seed service. It stages complete scene
    bindings in its caller's transaction; only the caller commits business rows.
    Source files stay borrowed until all real creation work and close have drained.
    """
    from sqlalchemy import delete, func, select
    from app.db.enums import UserRole
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.schemas._jsonb_types import SensorCalibration
    from app.schemas.scene_pose import FramePose
    from app.services import auxiliary_originals as auxiliary
    from app.services import managed_original_creation as creation
    from app.services import (
        original_sources,
        scene as scene_svc,
        scene_pose as scene_pose_svc,
    )
    from app.services import managed_original_reader as reader, storage_access
    from app.services.dataset_assets import (
        validate_legacy_bindings,
        register_item_asset,
    )
    from app.services.dataset_lifecycle import require_writable
    from app.services.dataset_storage_keys import dataset_item_relative_path
    from app.services.sensor_calibration import calibration_digest
    from app.services.storage import TRUSTED_NUSCENES_PREFIX

    if (
        frame not in {"ego", "sensor"}
        or not scene_names
        or len(set(scene_names)) != len(scene_names)
    ):
        raise ValueError("invalid nuScenes scene/frame selection")
    meta_dir = _resolve_source_path(nuscenes_root, version)
    scenes_by_name = {row["name"]: row for row in _load_table(meta_dir, "scene")}
    samples_by_token = _index_by_token(_load_table(meta_dir, "sample"))
    sample_data = _load_table(meta_dir, "sample_data")
    cs_by_token = _index_by_token(_load_table(meta_dir, "calibrated_sensor"))
    sensor_by_token = _index_by_token(_load_table(meta_dir, "sensor"))
    ego_by_token = _index_by_token(_load_table(meta_dir, "ego_pose"))
    log_by_token = _index_by_token(_load_table(meta_dir, "log"))
    maps = _load_table(meta_dir, "map")
    chosen = []
    for name in scene_names:
        if name not in scenes_by_name:
            raise ValueError(f"scene name {name!r} is absent from the supplied source")
        row = scenes_by_name[name]
        chosen.append((name, row, _ordered_samples(row, samples_by_token)))
    if sum(len(samples) for _, _, samples in chosen) > 1000:
        raise ValueError("nuScenes import exceeds 1000 selected frames")
    axis = "iso_8855" if frame == "ego" else "apollo"
    display_id = dataset_display_id or _derived_display_id("DS-NU-", dataset_name)
    dataset = await db.scalar(select(Dataset).where(Dataset.display_id == display_id))
    actor = await db.get(User, owner_id, populate_existing=True)
    if (
        actor is None
        or not actor.is_active
        or actor.role not in {UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN}
    ):
        raise ValueError("nuScenes CLI import requires an active administrative owner")
    if dataset is None:
        dataset = Dataset(
            id=uuid.uuid4(),
            display_id=display_id,
            name=dataset_name,
            data_type="point_cloud",
            is_temporal=True,
            created_by=owner_id,
            metadata_={"axis_convention": axis},
        )
        db.add(dataset)
        await db.flush()
    if actor.role != UserRole.SUPER_ADMIN and dataset.created_by != owner_id:
        raise ValueError("nuScenes CLI owner does not own this dataset")
    await require_writable(db, dataset.id)
    meta = dict(dataset.metadata_ or {})
    if meta.get("axis_convention") not in (None, axis):
        raise ValueError(
            f"dataset {dataset.display_id} axis_convention cannot accept --frame {frame}"
        )
    prior_source = meta.get("source") or {}
    expected_root = f"{TRUSTED_NUSCENES_PREFIX}-{dataset.id.hex}"
    storage_root = prior_source.get("storage_root") or expected_root
    if (
        prior_source.get("storage_layout") != "legacy_name"
        and storage_root != expected_root
    ):
        raise ValueError("nuScenes dataset storage_root does not match its UUID")
    revision = dataset.lifecycle_revision
    dataset_id = dataset.id
    prior_dataset = (
        dataset.created_by,
        dataset.name,
        dataset.data_type,
        dataset.is_temporal,
        json.dumps(dataset.metadata_ or {}, sort_keys=True, allow_nan=False),
    )
    if await validate_legacy_bindings(db):
        raise ValueError(
            "Initialize canonical platform storage before NuScenes CLI import"
        )
    authority = {"policy": "nuscenes-cli-import", "actor_id": str(owner_id)}
    total_items, report, total_local_bytes = 0, [], 0

    async def authorize(*, lock=False):
        current = await require_writable(
            db, dataset_id, expected_revision=revision, lock=lock
        )
        query = (
            select(User)
            .where(User.id == owner_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update(read=True)
        current_actor = await db.scalar(query)
        if (
            current_actor is None
            or not current_actor.is_active
            or current_actor.role not in {UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN}
            or (
                current_actor.role != UserRole.SUPER_ADMIN
                and current.created_by != owner_id
            )
        ):
            raise ValueError("nuScenes CLI import authority changed")
        return current

    def source_context(
        sample, sample_data, calibrated_sensor, sensor, ego_pose, *, key, sha, size
    ):
        return {
            "sample": sample,
            "sample_data": sample_data,
            "calibrated_sensor": calibrated_sensor,
            "sensor": sensor,
            "ego_pose": ego_pose,
            "source_storage_key": key,
            "source_sha256": sha,
            "source_file_size": size,
        }

    async def verify_owner(owner):
        if hasattr(owner, "owner_kind"):
            spec = reader.ObjectSpec(
                owner.endpoint_url,
                owner.bucket,
                owner.object_key,
                owner.sha256,
                owner.size_bytes,
            )
            location_id, gate_revision = owner.location_id, owner.maintenance_revision
        else:
            where = auxiliary.resolve(owner)
            spec = reader.ObjectSpec(
                where.endpoint_url,
                where.bucket,
                where.object_key,
                owner.sha256,
                owner.size_bytes,
            )
            location_id = where.location_id
            gate_revision = await storage_access.check(db, location_id)
        from app.services.export_media_transfer import Admission

        async with await storage_access.acquire(
            db, location_id, "nuscenes_import_verify", expected_revision=gate_revision
        ):
            admission, stage, failure = Admission(), None, None
            physical = creation._Physical()
            try:
                stage = await physical.run(
                    reader._capture,
                    spec,
                    auxiliary.MAX_BYTES,
                    1800,
                    time.monotonic() + 1800,
                    threading.Event(),
                    admission.release,
                    transport_timeout_seconds=60,
                    retain_on_close_error=True,
                )
            except BaseException as exc:
                failure = exc
            finally:
                if stage is not None:
                    try:
                        await physical.run(stage.close)
                    except BaseException as closing:
                        if failure is not None and getattr(
                            failure, "storage_operation_unsettled", False
                        ):
                            storage_access.mark_unsettled(closing)
                        failure = closing
                admission.finish(getattr(failure, "storage_operation_unsettled", False))
            if failure is not None:
                raise failure
            if physical.interrupted is not None:
                raise physical.interrupted
        await authorize()

    def check_legacy_receipt(metadata, staged, prefix="source"):
        if not isinstance(metadata, dict):
            raise ValueError("existing scene source contract is invalid")
        expected_sha, expected_size = (
            metadata.get(prefix + "_sha256"),
            metadata.get(prefix + "_file_size"),
        )
        if (expected_sha is not None and expected_sha != staged.sha256) or (
            expected_size is not None
            and (type(expected_size) is not int or expected_size != staged.size)
        ):
            raise ValueError("existing scene source drift for original bytes")

    async def auxiliary_candidate(kind, frozen, stage, existing_id=None):
        if kind != "raw_lidar" and stage.size == 0:
            raise ValueError("nuScenes map original is empty")
        await authorize()
        owner = (
            await db.get(DatasetAuxiliaryOriginal, existing_id, populate_existing=True)
            if existing_id
            else None
        )
        if owner is None and existing_id:
            raise ValueError("registered nuScenes auxiliary original is missing")
        if owner is None and kind == "map":
            owner = await db.scalar(
                select(DatasetAuxiliaryOriginal)
                .where(
                    DatasetAuxiliaryOriginal.dataset_id == dataset_id,
                    DatasetAuxiliaryOriginal.kind == "map",
                    DatasetAuxiliaryOriginal.source_contract == frozen,
                )
                .order_by(
                    DatasetAuxiliaryOriginal.created_at, DatasetAuxiliaryOriginal.id
                )
                .limit(1)
            )
        if owner is not None:
            if (
                owner.dataset_id != dataset_id
                or owner.kind != kind
                or owner.source_contract != frozen
                or owner.sha256 != stage.sha256
                or owner.size_bytes != stage.size
            ):
                raise ValueError("existing scene source drift for auxiliary original")
            await verify_owner(owner)
            return owner
        allocation = await creation.allocate_auxiliary(
            db,
            dataset_id=dataset_id,
            kind=kind,
            source_contract=frozen,
            logical_path=f"nuscenes/{kind}/payload",
            max_bytes=auxiliary.MAX_BYTES,
            authority=authority,
            requested_by=owner_id,
        )
        stage.file.seek(0)
        created = await creation.create_stream(
            db, allocation, stage.file, content_type=allocation["content_type"]
        )
        if created.sha256 != stage.sha256 or created.size_bytes != stage.size:
            raise ValueError("nuScenes auxiliary source changed during creation")
        await authorize()
        return await auxiliary.publish(
            db, allocation, created, kind=kind, source_contract=frozen
        )

    def item_state(item):
        if item is None:
            return None
        return (
            item.dataset_id,
            item.file_path,
            item.file_type,
            item.file_size,
            item.content_hash,
            item.width,
            item.height,
            item.scene_id,
            item.frame_index,
            item.nuscenes_raw_original_id,
            json.dumps(item.metadata_ or {}, sort_keys=True, allow_nan=False),
        )

    async def media_candidate(
        logical, file_type, stage, existing=None, width=None, height=None
    ):
        if stage.size == 0:
            raise ValueError("nuScenes media original is empty")
        await authorize()
        prior = item_state(existing)
        asset = (
            await db.scalar(
                select(DatasetAsset).where(DatasetAsset.dataset_item_id == existing.id)
            )
            if existing
            else None
        )
        if asset is not None and asset.verification_state == "verified":
            source = await original_sources._item(db, existing.id, False, bounded=True)
            if source.sha256 != stage.sha256 or source.size_bytes != stage.size:
                raise ValueError("existing scene source drift for canonical media")
            await verify_owner(source)
            return dict(
                item=existing,
                prior=prior,
                logical=logical,
                file_type=file_type,
                source=source,
                created=None,
                width=width,
                height=height,
            )
        allocation = await creation.allocate(
            db,
            dataset_id=dataset_id,
            logical_path=logical,
            origin="connector",
            max_bytes=auxiliary.MAX_BYTES,
            authority=authority,
            requested_by=owner_id,
            content_type="image/jpeg"
            if file_type == "image"
            else "application/octet-stream",
        )
        stage.file.seek(0)
        created = await creation.create_stream(
            db, allocation, stage.file, content_type=allocation["content_type"]
        )
        if created.sha256 != stage.sha256 or created.size_bytes != stage.size:
            raise ValueError("nuScenes media changed during controlled creation")
        await authorize()
        return dict(
            item=existing,
            prior=prior,
            logical=logical,
            file_type=file_type,
            source=None,
            allocation=allocation,
            created=created,
            width=width,
            height=height,
        )

    async def bind_media(candidate, scene, index, metadata):
        item, created = candidate["item"], candidate["created"]
        if item is None:
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset_id,
                file_name=Path(candidate["logical"]).name,
                file_path=candidate["allocation"]["object_key"],
                file_type=candidate["file_type"],
            )
            db.add(item)
        elif created:
            # Historical unverified locators never become write receipts. Retain
            # their conservative assertions before replacing only unverified maps.
            from app.services.managed_original_retirement import (
                capture_managed_original_retirements,
            )

            asset = await db.scalar(
                select(DatasetAsset)
                .where(DatasetAsset.dataset_item_id == item.id)
                .with_for_update()
            )
            if asset and asset.verification_state == "verified":
                raise ValueError(
                    "nuScenes canonical media changed before final binding"
                )
            await capture_managed_original_retirements(
                db, dataset_id=dataset_id, item_id=item.id
            )
            if asset:
                await db.execute(
                    delete(DatasetAssetLocation).where(
                        DatasetAssetLocation.asset_id == asset.id
                    )
                )
            item.file_path = candidate["allocation"]["object_key"]
        if created:
            item.file_size, item.content_hash = created.size_bytes, created.md5
        item.width, item.height = candidate["width"], candidate["height"]
        item.scene_id, item.frame_index = scene.id, index
        item.metadata_ = {
            **(item.metadata_ or {}),
            **metadata,
            "storage_relative_path": candidate["logical"],
        }
        await db.flush()
        if created:
            await register_item_asset(
                db,
                item,
                sha256=created.sha256,
                size_bytes=created.size_bytes,
                etag=created.etag,
            )
            await creation.attach(db, item, created.creation_id)
        return item

    staged_scenes = []
    for scene_name, scene_row, samples in chosen:
        scene = await db.scalar(
            select(Scene).where(
                Scene.dataset_id == dataset_id, Scene.name == scene_name
            )
        )
        prior_scene = json.loads(json.dumps(scene.source_metadata)) if scene else None
        prior_scene_state = (
            (
                scene.id,
                scene.dataset_id,
                scene.source_format,
                scene.nuscenes_map_original_id,
                json.dumps(scene.source_metadata, sort_keys=True, allow_nan=False),
            )
            if scene
            else None
        )
        prior_items = (
            list(
                await db.scalars(
                    select(DatasetItem).where(DatasetItem.scene_id == scene.id)
                )
            )
            if scene
            else []
        )
        by_logical = {}
        for item in prior_items:
            logical = dataset_item_relative_path(item)
            for prefix in (storage_root + "/", dataset.name + "/"):
                if logical.startswith(prefix):
                    logical = logical[len(prefix) :]
                    break
            by_logical[logical] = item
        by_frame = {
            item.frame_index: item
            for item in prior_items
            if item.file_type == "point_cloud"
        }
        log = log_by_token.get(scene_row.get("log_token"))
        if log is None:
            raise ValueError("nuScenes scene references missing log")
        map_row = _map_for_log(maps, log["token"])
        if scene:
            source = (scene.source_metadata or {}).get("nuscenes_export") or {}
            if (
                scene.source_metadata.get("frame") != frame
                or len(by_frame) != len(samples)
                or (
                    source
                    and (
                        source.get("scene") != scene_row
                        or source.get("log") != log
                        or source.get("map") != map_row
                    )
                )
            ):
                raise ValueError(
                    "existing scene source drift or frame contract changed"
                )
            for index, sample in enumerate(samples):
                channels = _key_sample_data_by_channel(
                    sample["token"], sample_data, cs_by_token, sensor_by_token
                )
                for channel, data in channels.items():
                    cs, sensor = (
                        cs_by_token[data["calibrated_sensor_token"]],
                        sensor_by_token[
                            cs_by_token[data["calibrated_sensor_token"]]["sensor_token"]
                        ],
                    )
                    logical = (
                        f"{scene_name}/camera/{channel}/{scene_name}_{index:06d}.jpg"
                    )
                    item = (
                        by_frame.get(index)
                        if sensor["modality"] == "lidar"
                        else by_logical.get(logical)
                        if sensor["modality"] == "camera"
                        else None
                    )
                    if sensor["modality"] not in {"lidar", "camera"}:
                        continue
                    if item is None:
                        raise ValueError(
                            "existing scene is missing original sensor frame"
                        )
                    old = (item.metadata_ or {}).get("nuscenes_export")
                    expected = dict(
                        sample=sample,
                        sample_data=data,
                        calibrated_sensor=cs,
                        sensor=sensor,
                        ego_pose=ego_by_token[data["ego_pose_token"]],
                    )
                    if old is not None and any(
                        old.get(key) != value for key, value in expected.items()
                    ):
                        raise ValueError("existing scene source drift")
        with _local_stage(nuscenes_root, map_row["filename"]) as staged:
            total_local_bytes += staged.size
            if prior_scene and isinstance(prior_scene.get("nuscenes_export"), dict):
                check_legacy_receipt(prior_scene["nuscenes_export"], staged, "map")
            map_owner = await auxiliary_candidate(
                "map",
                auxiliary.contract("map", map_row),
                staged,
                scene.nuscenes_map_original_id if scene else None,
            )
        candidates, calibrations = [], {}
        for index, sample in enumerate(samples):
            channels = _key_sample_data_by_channel(
                sample["token"], sample_data, cs_by_token, sensor_by_token
            )
            lidars = [
                data
                for data in channels.values()
                if sensor_by_token[
                    cs_by_token[data["calibrated_sensor_token"]]["sensor_token"]
                ]["modality"]
                == "lidar"
            ]
            if len(lidars) != 1:
                raise ValueError("nuScenes sample requires exactly one lidar keyframe")
            data = lidars[0]
            cs, sensor, pose = (
                cs_by_token[data["calibrated_sensor_token"]],
                sensor_by_token[
                    cs_by_token[data["calibrated_sensor_token"]]["sensor_token"]
                ],
                ego_by_token[data["ego_pose_token"]],
            )
            raw_facts = dict(
                sample=sample,
                sample_data=data,
                calibrated_sensor=cs,
                sensor=sensor,
                ego_pose=pose,
            )
            old_item = by_frame.get(index)
            with _local_stage(nuscenes_root, data["filename"]) as staged:
                total_local_bytes += staged.size
                if total_local_bytes > 24 * 1024**3:
                    raise ValueError("nuScenes import exceeds total source byte budget")
                if (
                    old_item
                    and (old_item.metadata_ or {}).get("nuscenes_export") is not None
                ):
                    check_legacy_receipt(old_item.metadata_["nuscenes_export"], staged)
                raw_owner = await auxiliary_candidate(
                    "raw_lidar",
                    auxiliary.contract("raw_lidar", raw_facts),
                    staged,
                    old_item.nuscenes_raw_original_id if old_item else None,
                )
                staged.file.seek(0)
                pcd = _lidar_bin_to_binary_pcd(
                    staged.file,
                    transform=_transform(cs["rotation"], cs["translation"])
                    if frame == "ego"
                    else None,
                )
                with _bytes_stage(pcd) as pcd_stage:
                    candidate = await media_candidate(
                        f"{scene_name}/lidar/{scene_name}_{index:06d}.pcd",
                        "point_cloud",
                        pcd_stage,
                        old_item,
                    )
            candidate.update(
                index=index,
                raw=raw_owner,
                metadata={
                    "nuscenes_export": source_context(
                        **raw_facts,
                        key=raw_owner.object_key,
                        sha=raw_owner.sha256,
                        size=raw_owner.size_bytes,
                    )
                },
            )
            candidates.append(candidate)
            for channel, camera in channels.items():
                camera_cs = cs_by_token[camera["calibrated_sensor_token"]]
                camera_sensor = sensor_by_token[camera_cs["sensor_token"]]
                if camera_sensor["modality"] != "camera":
                    continue
                logical = f"{scene_name}/camera/{channel}/{scene_name}_{index:06d}.jpg"
                camera_pose = ego_by_token[camera["ego_pose_token"]]
                if index == 0 and camera_cs["camera_intrinsic"]:
                    extrinsic = (
                        _compute_ego_to_cam_extrinsic(
                            ego_lidar=pose, cs_cam=camera_cs, ego_cam=camera_pose
                        )
                        if frame == "ego"
                        else _compute_lidar_to_cam_extrinsic(
                            cs_lidar=cs,
                            ego_lidar=pose,
                            cs_cam=camera_cs,
                            ego_cam=camera_pose,
                        )
                    )
                    calibrations[channel] = SensorCalibration.model_validate(
                        {
                            "extrinsic": extrinsic,
                            "intrinsic": [
                                float(v)
                                for row in camera_cs["camera_intrinsic"]
                                for v in row
                            ],
                        }
                    ).model_dump(exclude_none=True)
                with _local_stage(nuscenes_root, camera["filename"]) as staged:
                    total_local_bytes += staged.size
                    if total_local_bytes > 24 * 1024**3:
                        raise ValueError(
                            "nuScenes import exceeds total source byte budget"
                        )
                    width, height = _camera_dimensions(camera)
                    prior_camera = by_logical.get(logical)
                    if (
                        prior_camera
                        and (prior_camera.metadata_ or {}).get("nuscenes_export")
                        is not None
                    ):
                        check_legacy_receipt(
                            prior_camera.metadata_["nuscenes_export"], staged
                        )
                    candidate = await media_candidate(
                        logical, "image", staged, by_logical.get(logical), width, height
                    )
                    key = (
                        candidate["source"].object_key
                        if candidate["source"]
                        else candidate["allocation"]["object_key"]
                    )
                    source = source_context(
                        sample,
                        camera,
                        camera_cs,
                        camera_sensor,
                        camera_pose,
                        key=key,
                        sha=staged.sha256,
                        size=staged.size,
                    )
                metadata = {"nuscenes_export": source}
                if channel in calibrations:
                    metadata["calibration"] = calibrations[channel]
                    source["platform_calibration_digest"] = calibration_digest(
                        SensorCalibration.model_validate(calibrations[channel])
                    )
                candidate.update(index=index, raw=None, metadata=metadata)
                candidates.append(candidate)
        for channel, calibration in calibrations.items():
            logical = f"{scene_name}/calib/camera/{channel}.json"
            with _bytes_stage(json.dumps(calibration).encode()) as staged:
                candidate = await media_candidate(
                    logical, "other", staged, by_logical.get(logical)
                )
            candidate.update(index=None, raw=None, metadata={})
            candidates.append(candidate)
        staged_scenes.append(
            (
                scene_name,
                scene_row,
                samples,
                scene,
                prior_scene,
                prior_scene_state,
                map_row,
                map_owner,
                log,
                candidates,
            )
        )

    # All originals and existing-object verification have finished. No physical
    # work or caller commit follows these final existing-parent write fences.
    bound_items = []
    current_dataset = await authorize(lock=True)
    if (
        current_dataset.created_by,
        current_dataset.name,
        current_dataset.data_type,
        current_dataset.is_temporal,
        json.dumps(current_dataset.metadata_ or {}, sort_keys=True, allow_nan=False),
    ) != prior_dataset:
        raise ValueError("nuScenes dataset changed before final binding")
    for (
        scene_name,
        scene_row,
        samples,
        scene,
        prior_scene,
        prior_scene_state,
        map_row,
        map_owner,
        log,
        candidates,
    ) in staged_scenes:
        await authorize(lock=True)
        current_scene = await db.scalar(
            select(Scene)
            .where(Scene.dataset_id == dataset_id, Scene.name == scene_name)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_scene_state = (
            (
                current_scene.id,
                current_scene.dataset_id,
                current_scene.source_format,
                current_scene.nuscenes_map_original_id,
                json.dumps(
                    current_scene.source_metadata, sort_keys=True, allow_nan=False
                ),
            )
            if current_scene
            else None
        )
        if current_scene_state != prior_scene_state:
            raise ValueError("nuScenes scene changed before final binding")
        scene = current_scene
        metadata = {
            "scene_token": scene_row["token"],
            "first_sample_token": scene_row["first_sample_token"],
            "frame_count": len(samples),
            "duration_s": (samples[-1]["timestamp"] - samples[0]["timestamp"]) / 1e6,
            "nuscenes_version": version,
            "frame": frame,
            "timestamp_delta_us": samples[-1]["timestamp"] - samples[0]["timestamp"],
            "nuscenes_export": {
                "version": version,
                "scene": scene_row,
                "log": log,
                "map": map_row,
                "map_storage_key": map_owner.object_key,
                "map_file_size": map_owner.size_bytes,
                "map_sha256": map_owner.sha256,
            },
        }
        if scene is None:
            scene = await scene_svc.create_scene(
                db,
                dataset_id=dataset_id,
                name=scene_name,
                source_format="nuscenes",
                source_metadata=metadata,
                created_by=owner_id,
            )
        else:
            scene.source_format, scene.source_metadata = "nuscenes", metadata
        await auxiliary.bind_map(db, scene, map_owner)
        for candidate in candidates:
            prior = candidate["item"]
            if prior is not None:
                locked = await db.get(
                    DatasetItem, prior.id, with_for_update=True, populate_existing=True
                )
                if locked is None or item_state(locked) != candidate["prior"]:
                    raise ValueError("nuScenes item changed before final binding")
                candidate["item"] = locked
                if candidate["source"] is not None:
                    async with original_sources._reads(db, True):
                        current_source = await original_sources._item(
                            db, locked.id, True, bounded=True
                        )
                    if current_source != candidate["source"]:
                        raise ValueError(
                            "nuScenes canonical source changed before final binding"
                        )
            item = await bind_media(
                candidate, scene, candidate["index"], candidate["metadata"]
            )
            if candidate["raw"] is not None:
                await auxiliary.bind_raw(db, item, candidate["raw"])
            bound_items.append(item)
            if prior is None:
                total_items += 1
        poses = []
        for index, sample in enumerate(samples):
            data = next(
                data
                for data in _key_sample_data_by_channel(
                    sample["token"], sample_data, cs_by_token, sensor_by_token
                ).values()
                if sensor_by_token[
                    cs_by_token[data["calibrated_sensor_token"]]["sensor_token"]
                ]["modality"]
                == "lidar"
            )
            pose = ego_by_token[data["ego_pose_token"]]
            poses.append(
                FramePose(
                    frame_index=index,
                    timestamp_us=int(data["timestamp"]),
                    ego_translation=[float(v) for v in pose["translation"]],
                    ego_rotation=[float(v) for v in pose["rotation"]],
                    source_metadata={"ego_pose_token": data["ego_pose_token"]},
                )
            )
        await scene_pose_svc.upsert_frame_poses(db, scene_id=scene.id, poses=poses)
        dataset = await db.get(Dataset, dataset_id)
        dataset.metadata_ = {
            **meta,
            "axis_convention": axis,
            "source": {
                "format": "nuscenes",
                "version": version,
                "scenes": sorted(scene_names),
                "pcd_encoding": PCD_ENCODING,
                "storage_root": storage_root,
                "storage_layout": prior_source.get("storage_layout") or "uuid_v1",
            },
        }
        dataset.file_count = int(
            await db.scalar(
                select(func.count(DatasetItem.id)).where(
                    DatasetItem.dataset_id == dataset_id
                )
            )
            or 0
        )
        await authorize(lock=True)
        report.append(
            {
                "name": scene_name,
                "frames": len(samples),
                **({"skipped": True} if prior_scene is not None else {}),
            }
        )
    project_display_id = project_display_id or _derived_display_id(
        "P-NU-", dataset_name
    )
    project = await db.scalar(
        select(Project)
        .where(Project.display_id == project_display_id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if project is None:
        project = Project(
            id=uuid.uuid4(),
            display_id=project_display_id,
            name=project_name or f"nuScenes {dataset_name}",
            type_label="点云检测",
            type_key="lidar",
            data_type="lidar",
            scene_mode=True,
            prefer_same_scene_continuation=True,
            owner_id=owner_id,
            tool_bindings=tool_bindings or {},
            ai_enabled=False,
        )
        db.add(project)
        await db.flush()
    if (
        await db.scalar(
            select(ProjectDataset).where(
                ProjectDataset.project_id == project.id,
                ProjectDataset.dataset_id == dataset_id,
            )
        )
        is None
    ):
        db.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
        await db.flush()
    # Use the same frame grouping, display IDs and link writer as the existing
    # point-cloud builder, without its calibration GETs and internal commits.
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.services.display_id import next_display_id
    from app.services.pointcloud_import import group_frames
    from app.services.task_dataset_link import link_items

    buckets = {}
    for item in bound_items:
        buckets.setdefault(item.scene_id, []).append(item)
    created_tasks = 0
    for items in buckets.values():
        frames, _ = group_frames(items)
        for _stem, group in sorted(frames.items()):
            primary = group["lidar"]
            if primary is None:
                continue
            task = await db.scalar(
                select(Task)
                .where(
                    Task.project_id == project.id, Task.dataset_item_id == primary.id
                )
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if task is None:
                task = Task(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    dataset_item_id=primary.id,
                    display_id=await next_display_id(db, "tasks"),
                    file_name=primary.file_name,
                    file_path=primary.file_path,
                    file_type="point_cloud",
                    status="pending",
                )
                db.add(task)
                await db.flush()
                created_tasks += 1
            else:
                task.file_path = primary.file_path
            existing_links = {
                row.role: row
                for row in await db.scalars(
                    select(TaskDatasetItemLink).where(
                        TaskDatasetItemLink.task_id == task.id
                    )
                )
            }
            expected = [(primary.id, "primary_lidar", None)] + [
                (item.id, f"camera_{channel}", channel)
                for channel, item in group["cameras"].items()
            ]
            missing = []
            for identifier, role, sensor_name in expected:
                current = existing_links.get(role)
                if current is None:
                    missing.append((identifier, role, sensor_name))
                elif (
                    current.dataset_item_id != identifier
                    or current.sensor_name != sensor_name
                ):
                    raise ValueError("nuScenes task sensor relation changed")
            if missing:
                await link_items(db, task.id, missing)
    project.total_tasks = (project.total_tasks or 0) + created_tasks
    await authorize(lock=True)
    await db.flush()
    return {
        "dataset_id": dataset_id,
        "project_id": project.id,
        "scenes": report,
        "total_items": total_items,
    }


# --------------------------------------------------------------------------- #
# admin / CLI
# --------------------------------------------------------------------------- #
async def _ensure_admin(db) -> uuid.UUID:
    """取标准 admin 用户;库里没有则按 admin/123456 建一个(与 seed_pointcloud 一致)。

    弱口令默认 admin 仅用于 dev/test。生产环境(settings.environment == "production")
    下若无 admin,拒绝自动创建——避免误对生产库植入弱口令 super_admin 后门。
    """
    from sqlalchemy import select

    from app.config import settings
    from app.core.security import hash_password
    from app.db.models.user import User

    admin = await db.scalar(select(User).where(User.email == "admin"))
    if admin:
        return admin.id
    if settings.environment == "production":
        raise RuntimeError(
            "生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。"
            "请先手动创建管理员账号再运行本导入脚本。"
        )
    admin = User(
        id=uuid.uuid4(),
        email="admin",
        name="超级管理员",
        password_hash=hash_password("123456"),
        role="super_admin",
        is_active=True,
    )
    db.add(admin)
    await db.flush()
    return admin.id


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 nuScenes-mini 的一个/多个 scene 转换并入库到本平台(MinIO + DB)。"
    )
    parser.add_argument(
        "--nuscenes-root",
        required=True,
        help="nuScenes 根目录(含 <version>/*.json 与 samples/<CHANNEL>/...)",
    )
    parser.add_argument(
        "--scene-tokens",
        required=True,
        help="逗号分隔的 scene name 列表(如 scene-0061,scene-0103)",
    )
    parser.add_argument("--dataset-name", required=True, help="目标 dataset 名(共用)")
    parser.add_argument("--version", default="v1.0-mini", help="nuScenes 版本子目录")
    parser.add_argument(
        "--frame",
        choices=["ego", "sensor"],
        default="ego",
        help="点云输出坐标系: ego=默认,点变换到 ego/ISO; sensor=保留 LIDAR_TOP/apollo",
    )
    args = parser.parse_args()

    scene_names = [s.strip() for s in args.scene_tokens.split(",") if s.strip()]

    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await import_nuscenes(
            db,
            nuscenes_root=Path(args.nuscenes_root),
            scene_names=scene_names,
            dataset_name=args.dataset_name,
            owner_id=owner_id,
            version=args.version,
            frame=args.frame,
        )
        await db.commit()

    print("=== nuScenes 导入完成 ===")
    print(f"dataset_id: {info['dataset_id']}  project_id: {info['project_id']}")
    print(f"total_items: {info['total_items']}")
    for s in info["scenes"]:
        tag = " (已存在,跳过)" if s.get("skipped") else ""
        print(f"  scene {s['name']}: {s['frames']} frames{tag}")


if __name__ == "__main__":
    asyncio.run(main())
