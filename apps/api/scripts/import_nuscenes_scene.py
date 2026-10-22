"""v0.14.2 · nuScenes-mini → 本平台原生目录结构 + 入库(MinIO + DB)一次性转换脚本。

把 nuScenes 数据集的一个或多个 scene 转成本平台点云原生目录结构。Dataset/Item 写入
由 `AssetsOperatorGateway` 通过原生能力持有，Scene / frame 与 pose 写入由各自 service
持有；`NuScenesImportWorkflow` 组合 workspace、项目关联、标定和 labeling tasks。

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

平台原生目录约定(storage key);<storage_root> 由 Dataset UUID 派生，
帧 stem 用 <scene_name>_<idx 6位> 保证跨 scene 的 storage key/file name 全局唯一:
    <storage_root>/<scene_name>/lidar/<scene_name>_<idx 6位>.pcd            file_type=point_cloud
    <storage_root>/<scene_name>/camera/<CHANNEL>/<scene_name>_<idx 6位>.jpg  file_type=image
    <storage_root>/<scene_name>/calib/camera/<CHANNEL>.json                 file_type=other

标定:本版用 **每个 scene 第 1 帧** 的标定对全 scene 通用(metadata 记
timestamp_delta_us 备查;前端不消费;逐帧真补偿留 v0.15+)。

幂等:dataset 按派生 display_id 复用(短名为 DS-NU-<name>,超长名加 hash 截断到
DB display_id 长度限制内);scene 按 (dataset_id, name) 已存在则跳过该 scene;
入口最终提交同一个 SQL transaction。

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
    with path.open(encoding="utf-8") as f:
        return json.load(f)


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
    pts = np.fromfile(str(bin_path), dtype=np.float32).reshape(-1, 5)[:, :3]
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
    """把 nuScenes 的若干 scene 转换并入库到一个共用 dataset。

    流程(每个 scene 一个顶层子目录,显式 create_scene + assign_items_to_scene):
      1. ensure/复用 dataset(display_id 由 dataset_name 稳定派生且不超过 20 字符,
         data_type=point_cloud, metadata_={axis_convention: iso_8855|apollo})。
      2. 逐 scene:沿 sample.next 链取有序 sample;每帧转 lidar PCD + 各 CAM jpg +
         (首帧)算 calib;建 DatasetItem;create_scene + assign(lidar 有序、cam 同帧、
         calib scene-level)。已存在同名 scene 则跳过(幂等)。
      3. 建/复用 lidar Project + ProjectDataset，并由应用工作流创建标注任务。

    调用方负责最终 commit;素材、Scene、workspace、标定、任务及关联写入只
    flush 到调用方事务。返回 {"dataset_id", "scenes": [{"name", "frames"}...],
    "total_items"}。
    """
    from aap_assets_source.operator_contract import (
        EnsureOperatorNuScenesDatasetRequest,
        NuScenesDatasetSpec,
        OperatorDimensions,
        OperatorSourceEntry,
        OperatorSourcePatch,
    )
    from app.application.nuscenes_import import NuScenesImportWorkflow
    from aap_assets_catalog.spatial_contract import (
        AssignItemsRequest,
        CreateSceneRequest,
        ListScenesRequest,
        SceneAssignment,
        SetDatasetAxisRequest,
        SetSceneSourceRequest,
        UpsertPosesRequest,
    )
    from aap_assets_catalog.spatial_values import FramePose
    from app.services.assets_spatial import AssetsSpatialGateway
    from app.services.assets_operator import AssetsOperatorGateway
    from app.services.storage import storage_service

    meta_dir = _resolve_source_path(nuscenes_root, version)
    if not meta_dir.is_dir():
        raise FileNotFoundError(f"nuScenes 元数据目录缺失: {meta_dir}")

    # 读 nuScenes 表 + 建索引
    scenes_tbl = _load_table(meta_dir, "scene")
    samples_by_token = _index_by_token(_load_table(meta_dir, "sample"))
    sample_data = _load_table(meta_dir, "sample_data")
    cs_by_token = _index_by_token(_load_table(meta_dir, "calibrated_sensor"))
    sensor_by_token = _index_by_token(_load_table(meta_dir, "sensor"))
    ego_by_token = _index_by_token(_load_table(meta_dir, "ego_pose"))
    log_by_token = _index_by_token(_load_table(meta_dir, "log"))
    maps_tbl = _load_table(meta_dir, "map")

    scene_by_name = {s["name"]: s for s in scenes_tbl}
    axis_convention = "iso_8855" if frame == "ego" else "apollo"

    # 1. ensure/复用 dataset(派生固定 display_id → 幂等)
    display_id = dataset_display_id or _derived_display_id("DS-NU-", dataset_name)
    assets = AssetsOperatorGateway(db)
    dataset_mutation = await assets.ensure_operator_nuscenes_dataset(
        EnsureOperatorNuScenesDatasetRequest(
            operation_id=uuid.uuid4(),
            owner_id=owner_id,
            spec=NuScenesDatasetSpec(
                display_id=display_id,
                name=dataset_name,
                version=version,
                scene_names=scene_names,
                axis_convention=axis_convention,
                frame=frame,
                pcd_encoding=PCD_ENCODING,
            ),
        )
    )
    ds = dataset_mutation.dataset
    storage_root = dataset_mutation.storage_root
    needs_pcd_upgrade = dataset_mutation.needs_pcd_upgrade
    if storage_root is None or needs_pcd_upgrade is None:
        raise ValueError("operator returned an incomplete nuScenes Dataset result")
    _safe_source_path(storage_root)

    bucket = storage_service.datasets_bucket
    spatial = AssetsSpatialGateway(db)
    await spatial.set_dataset_axis(
        SetDatasetAxisRequest(
            operation_id=uuid.uuid4(),
            dataset_id=ds.id,
            axis_convention=axis_convention,
        ),
        same_session_native=True,
        commit=False,
    )
    existing_scenes = {
        scene.name: scene
        for scene in await spatial.list_scenes(
            ListScenesRequest(actor_id=None, dataset_id=ds.id),
            same_session_native=True,
        )
    }
    existing_items = await assets.read_all_items(ds.id)

    report_scenes: list[dict] = []
    total_items = 0

    # 2. 逐 scene 转换 + 入库
    for scene_name in scene_names:
        scene_row = scene_by_name.get(scene_name)
        if scene_row is None:
            raise ValueError(
                f"scene name {scene_name!r} 在 {meta_dir}/scene.json 中不存在"
            )

        if scene_name in existing_scenes:
            # 幂等:同名 scene 已建过时保留 task / annotation；旧 ASCII PCD
            # 只原位覆盖对象并同步 DB 大小与哈希；同时为旧相机条目
            # 回填 nuScenes sample_data 中的像素尺寸，让投影与 2D 成员有可靠坐标基准。
            samples = _ordered_samples(scene_row, samples_by_token)
            scene = existing_scenes[scene_name]
            log_row = log_by_token.get(scene_row.get("log_token"))
            if log_row is None:
                raise ValueError(
                    f"scene {scene_name!r} references missing log {scene_row.get('log_token')!r}"
                )
            map_row = _map_for_log(maps_tbl, log_row["token"])
            map_storage_key = _source_storage_key(storage_root, map_row["filename"])
            map_bytes = _resolve_source_path(
                nuscenes_root, map_row["filename"]
            ).read_bytes()
            map_sha256 = hashlib.sha256(map_bytes).hexdigest()
            camera_items = [
                item
                for item in existing_items
                if item.scene_id == scene.id and item.file_type == "image"
            ]
            camera_by_path = {item.file_path: item for item in camera_items}
            lidar_items = [
                item
                for item in existing_items
                if item.scene_id == scene.id and item.file_type == "point_cloud"
            ]
            lidar_by_frame = {item.frame_index: item for item in lidar_items}
            existing_scene_meta = scene.source_metadata or {}
            if (
                existing_scene_meta.get("scene_token") != scene_row["token"]
                or existing_scene_meta.get("first_sample_token")
                != scene_row["first_sample_token"]
                or int(existing_scene_meta.get("frame_count") or -1) != len(samples)
                or existing_scene_meta.get("frame") != frame
            ):
                raise ValueError(
                    f"existing scene {scene_name!r} does not match the requested nuScenes source"
                )
            existing_scene_export = existing_scene_meta.get("nuscenes_export")
            if existing_scene_export is not None:
                existing_map = (
                    existing_scene_export.get("map")
                    if isinstance(existing_scene_export, dict)
                    else None
                )
                existing_source_scene = (
                    existing_scene_export.get("scene")
                    if isinstance(existing_scene_export, dict)
                    else None
                )
                existing_log = (
                    existing_scene_export.get("log")
                    if isinstance(existing_scene_export, dict)
                    else None
                )
                existing_map_sha256 = (
                    existing_scene_export.get("map_sha256")
                    if isinstance(existing_scene_export, dict)
                    else None
                )
                if (
                    existing_map != map_row
                    or existing_source_scene != scene_row
                    or existing_log != log_row
                    or existing_map_sha256 != map_sha256
                ):
                    raise ValueError(
                        f"existing scene {scene_name!r} map or source contract drifted"
                    )
            if len(lidar_by_frame) != len(samples):
                raise ValueError(
                    f"scene {scene_name!r} 已存在的 lidar 帧数与 nuScenes 源不一致"
                )
            for frame_idx, sample in enumerate(samples):
                frame_stem = f"{scene_name}_{frame_idx:06d}"
                by_channel = _key_sample_data_by_channel(
                    sample["token"], sample_data, cs_by_token, sensor_by_token
                )
                lidar_sd = next(
                    (
                        sd
                        for sd in by_channel.values()
                        if sensor_by_token[
                            cs_by_token[sd["calibrated_sensor_token"]]["sensor_token"]
                        ]["modality"]
                        == "lidar"
                    ),
                    None,
                )
                lidar_item = lidar_by_frame.get(frame_idx)
                if lidar_sd is None or lidar_item is None:
                    raise ValueError(
                        f"existing scene {scene_name!r} is missing lidar frame {frame_idx}"
                    )
                lidar_cs = cs_by_token[lidar_sd["calibrated_sensor_token"]]
                lidar_sensor = sensor_by_token[lidar_cs["sensor_token"]]
                lidar_ego = ego_by_token[lidar_sd["ego_pose_token"]]
                raw_lidar_bytes = _resolve_source_path(
                    nuscenes_root, lidar_sd["filename"]
                ).read_bytes()
                _assert_source_context_compatible(
                    lidar_item.metadata.get("nuscenes_export"),
                    sample=sample,
                    sample_data=lidar_sd,
                    calibrated_sensor=lidar_cs,
                    sensor=lidar_sensor,
                    ego_pose=lidar_ego,
                    source_bytes=raw_lidar_bytes,
                )
                for channel, sd in by_channel.items():
                    cs = cs_by_token[sd["calibrated_sensor_token"]]
                    sensor = sensor_by_token[cs["sensor_token"]]
                    if sensor["modality"] != "camera":
                        continue
                    camera_key = (
                        f"{storage_root}/{scene_name}/camera/{channel}/{frame_stem}.jpg"
                    )
                    item = camera_by_path.get(camera_key)
                    if item is None:
                        raise ValueError(
                            f"existing scene {scene_name!r} is missing camera {channel} "
                            f"at frame {frame_idx}"
                        )
                    camera_ego = ego_by_token[sd["ego_pose_token"]]
                    source_camera_bytes = _resolve_source_path(
                        nuscenes_root, sd["filename"]
                    ).read_bytes()
                    _assert_source_context_compatible(
                        item.metadata.get("nuscenes_export"),
                        sample=sample,
                        sample_data=sd,
                        calibrated_sensor=cs,
                        sensor=sensor,
                        ego_pose=camera_ego,
                        source_bytes=source_camera_bytes,
                    )

            storage_service.client.put_object(
                Bucket=bucket,
                Key=map_storage_key,
                Body=map_bytes,
                Metadata={"sha256": map_sha256},
            )
            await spatial.set_scene_source(
                SetSceneSourceRequest(
                    operation_id=uuid.uuid4(),
                    scene_id=scene.id,
                    source_format="nuscenes",
                    source_metadata={
                        **existing_scene_meta,
                        "scene_token": scene_row["token"],
                        "first_sample_token": scene_row["first_sample_token"],
                        "frame_count": len(samples),
                        "nuscenes_version": version,
                        "frame": frame,
                        "nuscenes_export": {
                            "version": version,
                            "scene": scene_row,
                            "log": log_row,
                            "map": map_row,
                            "map_storage_key": map_storage_key,
                            "map_file_size": len(map_bytes),
                            "map_sha256": map_sha256,
                        },
                    },
                ),
                same_session_native=True,
            )
            needs_pcd_refresh = True
            report_pcd_upgrade = needs_pcd_upgrade
            patch_rows: dict[uuid.UUID, dict] = {}

            def stage_item_patch(item, **values) -> None:
                patch = patch_rows.setdefault(
                    item.id,
                    {"id": item.id, "source_snapshot": item.source_snapshot},
                )
                patch.update(values)

            frame_poses: list[FramePose] = []
            for frame_idx, sample in enumerate(samples):
                frame_stem = f"{scene_name}_{frame_idx:06d}"
                by_channel = _key_sample_data_by_channel(
                    sample["token"], sample_data, cs_by_token, sensor_by_token
                )
                lidar_sd = next(
                    (
                        sd
                        for sd in by_channel.values()
                        if sensor_by_token[
                            cs_by_token[sd["calibrated_sensor_token"]]["sensor_token"]
                        ]["modality"]
                        == "lidar"
                    ),
                    None,
                )
                if lidar_sd is None:
                    raise ValueError(
                        f"scene {scene_name!r} sample {sample['token']!r} has no lidar keyframe"
                    )
                lidar_cs = cs_by_token[lidar_sd["calibrated_sensor_token"]]
                lidar_sensor = sensor_by_token[lidar_cs["sensor_token"]]
                lidar_ego = ego_by_token[lidar_sd["ego_pose_token"]]
                raw_lidar_key = _source_storage_key(storage_root, lidar_sd["filename"])
                raw_lidar_bytes = _resolve_source_path(
                    nuscenes_root, lidar_sd["filename"]
                ).read_bytes()
                storage_service.client.put_object(
                    Bucket=bucket,
                    Key=raw_lidar_key,
                    Body=raw_lidar_bytes,
                    Metadata={"sha256": hashlib.sha256(raw_lidar_bytes).hexdigest()},
                )
                lidar_item = lidar_by_frame.get(frame_idx)
                if lidar_item is None:
                    raise ValueError(
                        f"existing scene {scene_name!r} is missing lidar frame {frame_idx}"
                    )
                stage_item_patch(
                    lidar_item,
                    nuscenes_export=_source_context(
                        sample=sample,
                        sample_data=lidar_sd,
                        calibrated_sensor=lidar_cs,
                        sensor=lidar_sensor,
                        ego_pose=lidar_ego,
                        storage_key=raw_lidar_key,
                        source_bytes=raw_lidar_bytes,
                    ),
                )
                frame_poses.append(
                    FramePose(
                        frame_index=frame_idx,
                        timestamp_us=int(lidar_sd["timestamp"]),
                        ego_translation=[float(v) for v in lidar_ego["translation"]],
                        ego_rotation=[float(v) for v in lidar_ego["rotation"]],
                        source_metadata={"ego_pose_token": lidar_sd["ego_pose_token"]},
                    )
                )
                for channel, sd in by_channel.items():
                    cs = cs_by_token[sd["calibrated_sensor_token"]]
                    sensor = sensor_by_token[cs["sensor_token"]]
                    if sensor["modality"] != "camera":
                        continue
                    camera_key = (
                        f"{storage_root}/{scene_name}/camera/{channel}/{frame_stem}.jpg"
                    )
                    item = camera_by_path.get(camera_key)
                    if item is None:
                        continue
                    source_camera_bytes = _resolve_source_path(
                        nuscenes_root, sd["filename"]
                    ).read_bytes()
                    storage_service.client.put_object(
                        Bucket=bucket,
                        Key=item.file_path,
                        Body=source_camera_bytes,
                        Metadata={
                            "sha256": hashlib.sha256(source_camera_bytes).hexdigest()
                        },
                    )
                    camera_width, camera_height = _camera_dimensions(sd)
                    item_patch = {
                        "nuscenes_export": _source_context(
                            sample=sample,
                            sample_data=sd,
                            calibrated_sensor=cs,
                            sensor=sensor,
                            ego_pose=ego_by_token[sd["ego_pose_token"]],
                            storage_key=item.file_path,
                            source_bytes=source_camera_bytes,
                        )
                    }
                    if not item.width or not item.height:
                        item_patch["dimensions"] = OperatorDimensions(
                            width=camera_width, height=camera_height
                        )
                    stage_item_patch(item, **item_patch)

            await spatial.upsert_poses(
                UpsertPosesRequest(
                    operation_id=uuid.uuid4(), scene_id=scene.id, poses=frame_poses
                ),
                same_session_native=True,
            )

            upgraded_point_clouds = 0
            if needs_pcd_refresh:
                lidar_items = [
                    item
                    for item in existing_items
                    if item.scene_id == scene.id and item.file_type == "point_cloud"
                ]
                lidar_by_frame = {item.frame_index: item for item in lidar_items}
                if len(lidar_by_frame) != len(samples):
                    raise ValueError(
                        f"scene {scene_name!r} 已存在的 lidar 帧数与 nuScenes 源不一致"
                    )
                for frame_idx, sample in enumerate(samples):
                    by_channel = _key_sample_data_by_channel(
                        sample["token"], sample_data, cs_by_token, sensor_by_token
                    )
                    lidar_sd = next(
                        (
                            sd
                            for sd in by_channel.values()
                            if sensor_by_token[
                                cs_by_token[sd["calibrated_sensor_token"]][
                                    "sensor_token"
                                ]
                            ]["modality"]
                            == "lidar"
                        ),
                        None,
                    )
                    if lidar_sd is None:
                        raise ValueError(
                            f"scene {scene_name!r} sample {sample['token']!r} 无 lidar keyframe"
                        )
                    item = lidar_by_frame.get(frame_idx)
                    if item is None:
                        raise ValueError(
                            f"scene {scene_name!r} 缺少 frame_index={frame_idx} 的 lidar item"
                        )
                    cs_lidar = cs_by_token[lidar_sd["calibrated_sensor_token"]]
                    transform = _transform(
                        cs_lidar["rotation"], cs_lidar["translation"]
                    )
                    pcd_bytes = _lidar_bin_to_binary_pcd(
                        _resolve_source_path(nuscenes_root, lidar_sd["filename"]),
                        transform=transform if frame == "ego" else None,
                    )
                    pcd_sha256 = hashlib.sha256(pcd_bytes).hexdigest()
                    storage_service.client.put_object(
                        Bucket=bucket,
                        Key=item.file_path,
                        Body=pcd_bytes,
                        Metadata={"sha256": pcd_sha256},
                    )
                    stage_item_patch(
                        item,
                        file_size=len(pcd_bytes),
                        content_hash=pcd_sha256,
                    )
                    if report_pcd_upgrade:
                        upgraded_point_clouds += 1
            backfilled_camera_dimensions = await assets.patch_all_items(
                ds.id,
                [OperatorSourcePatch(**patch) for patch in patch_rows.values()],
                observed_items={item.id: item for item in existing_items},
            )
            report = {"name": scene_name, "frames": len(samples), "skipped": True}
            if upgraded_point_clouds:
                report["upgraded_point_clouds"] = upgraded_point_clouds
            if backfilled_camera_dimensions:
                report["backfilled_camera_dimensions"] = backfilled_camera_dimensions
            report_scenes.append(report)
            continue

        samples = _ordered_samples(scene_row, samples_by_token)
        if not samples:
            raise ValueError(f"scene {scene_name!r} 无 sample(数据损坏?)")

        scene_token = scene_row["token"]
        log_row = log_by_token.get(scene_row.get("log_token"))
        if log_row is None:
            raise ValueError(
                f"scene {scene_name!r} references missing log {scene_row.get('log_token')!r}"
            )
        map_row = _map_for_log(maps_tbl, log_row["token"])
        map_storage_key = _source_storage_key(storage_root, map_row["filename"])
        map_bytes = _resolve_source_path(
            nuscenes_root, map_row["filename"]
        ).read_bytes()
        storage_service.client.put_object(
            Bucket=bucket,
            Key=map_storage_key,
            Body=map_bytes,
            Metadata={"sha256": hashlib.sha256(map_bytes).hexdigest()},
        )
        first_ts = samples[0]["timestamp"]
        last_ts = samples[-1]["timestamp"]

        append_entries: list[OperatorSourceEntry] = []
        lidar_item_ids: list[uuid.UUID] = []  # 主帧,按帧序
        cam_frame_index: dict[uuid.UUID, int] = {}  # cam item.id -> frame_index
        calib_item_ids: list[uuid.UUID] = []  # scene-level(frame_index=NULL)
        frame_poses: list[FramePose] = []  # v0.15.0 · 逐帧 ego pose(LIDAR_TOP 时钟)
        calib_written = False

        for frame_idx, sample in enumerate(samples):
            idx6 = f"{frame_idx:06d}"
            # 多 scene 共用 dataset 时保留带 scene 前缀的全局唯一 storage key/file name。
            # Task preparation 按 scene 分组,允许各 scene 内自然使用重复的 frame stem;
            # 这里仍保留前缀以兼容既有 nuScenes 导入路径与输出名称。
            frame_stem = f"{scene_name}_{idx6}"
            by_channel = _key_sample_data_by_channel(
                sample["token"], sample_data, cs_by_token, sensor_by_token
            )

            # ---- LIDAR_TOP ----
            lidar_sd = None
            for channel, sd in by_channel.items():
                cs = cs_by_token[sd["calibrated_sensor_token"]]
                if sensor_by_token[cs["sensor_token"]]["modality"] == "lidar":
                    lidar_sd = sd
                    break
            if lidar_sd is None:
                raise ValueError(
                    f"scene {scene_name!r} sample {sample['token']!r} 无 lidar keyframe"
                )

            cs_lidar = cs_by_token[lidar_sd["calibrated_sensor_token"]]
            ego_lidar = ego_by_token[lidar_sd["ego_pose_token"]]
            # v0.15.0 · 逐帧 ego pose(ego→global)+ LIDAR_TOP 时间戳,落 scene_frame_poses
            frame_poses.append(
                FramePose(
                    frame_index=frame_idx,
                    timestamp_us=int(lidar_sd["timestamp"]),
                    ego_translation=[float(v) for v in ego_lidar["translation"]],
                    ego_rotation=[float(v) for v in ego_lidar["rotation"]],
                    source_metadata={"ego_pose_token": lidar_sd["ego_pose_token"]},
                )
            )
            T_ego_from_lidar = _transform(
                cs_lidar["rotation"],
                cs_lidar["translation"],
            )
            pcd_bytes = _lidar_bin_to_binary_pcd(
                _resolve_source_path(nuscenes_root, lidar_sd["filename"]),
                transform=T_ego_from_lidar if frame == "ego" else None,
            )
            raw_lidar_key = _source_storage_key(storage_root, lidar_sd["filename"])
            raw_lidar_bytes = _resolve_source_path(
                nuscenes_root, lidar_sd["filename"]
            ).read_bytes()
            storage_service.client.put_object(
                Bucket=bucket,
                Key=raw_lidar_key,
                Body=raw_lidar_bytes,
                Metadata={"sha256": hashlib.sha256(raw_lidar_bytes).hexdigest()},
            )
            lidar_key = f"{storage_root}/{scene_name}/lidar/{frame_stem}.pcd"
            pcd_sha256 = hashlib.sha256(pcd_bytes).hexdigest()
            storage_service.client.put_object(
                Bucket=bucket,
                Key=lidar_key,
                Body=pcd_bytes,
                Metadata={"sha256": pcd_sha256},
            )
            lidar_item_id = uuid.uuid4()
            append_entries.append(
                OperatorSourceEntry(
                    id=lidar_item_id,
                    file_name=f"{frame_stem}.pcd",
                    file_path=lidar_key,
                    file_type="point_cloud",
                    file_size=len(pcd_bytes),
                    content_hash=pcd_sha256,
                    metadata={
                        "nuscenes_export": _source_context(
                            sample=sample,
                            sample_data=lidar_sd,
                            calibrated_sensor=cs_lidar,
                            sensor=sensor_by_token[cs_lidar["sensor_token"]],
                            ego_pose=ego_lidar,
                            storage_key=raw_lidar_key,
                            source_bytes=raw_lidar_bytes,
                        )
                    },
                )
            )
            lidar_item_ids.append(lidar_item_id)

            # ---- 各 CAM(modality==camera)----
            for channel, sd in by_channel.items():
                cs = cs_by_token[sd["calibrated_sensor_token"]]
                if sensor_by_token[cs["sensor_token"]]["modality"] != "camera":
                    continue

                jpg_bytes = _resolve_source_path(
                    nuscenes_root, sd["filename"]
                ).read_bytes()
                cam_key = (
                    f"{storage_root}/{scene_name}/camera/{channel}/{frame_stem}.jpg"
                )
                storage_service.client.put_object(
                    Bucket=bucket,
                    Key=cam_key,
                    Body=jpg_bytes,
                    Metadata={"sha256": hashlib.sha256(jpg_bytes).hexdigest()},
                )
                camera_width, camera_height = _camera_dimensions(sd)
                cam_item_id = uuid.uuid4()
                append_entries.append(
                    OperatorSourceEntry(
                        id=cam_item_id,
                        file_name=f"{frame_stem}.jpg",
                        file_path=cam_key,
                        file_type="image",
                        file_size=len(jpg_bytes),
                        content_hash=hashlib.sha256(jpg_bytes).hexdigest(),
                        width=camera_width,
                        height=camera_height,
                        metadata={
                            "nuscenes_export": _source_context(
                                sample=sample,
                                sample_data=sd,
                                calibrated_sensor=cs,
                                sensor=sensor_by_token[cs["sensor_token"]],
                                ego_pose=ego_by_token[sd["ego_pose_token"]],
                                storage_key=cam_key,
                                source_bytes=jpg_bytes,
                            )
                        },
                    )
                )
                cam_frame_index[cam_item_id] = frame_idx

                # 首帧:算该相机外参 + 内参,写 calib/camera/<channel>.json
                if not calib_written:
                    ego_cam = ego_by_token[sd["ego_pose_token"]]
                    intrinsic_3x3 = cs["camera_intrinsic"]
                    if not intrinsic_3x3:
                        # 无内参的相机(理论不应出现在 CAM_*),跳过 calib
                        continue
                    if frame == "ego":
                        extrinsic = _compute_ego_to_cam_extrinsic(
                            ego_lidar=ego_lidar,
                            cs_cam=cs,
                            ego_cam=ego_cam,
                        )
                    else:
                        extrinsic = _compute_lidar_to_cam_extrinsic(
                            cs_lidar=cs_lidar,
                            ego_lidar=ego_lidar,
                            cs_cam=cs,
                            ego_cam=ego_cam,
                        )
                    intrinsic = [float(v) for row in intrinsic_3x3 for v in row]
                    calib_payload = {"extrinsic": extrinsic, "intrinsic": intrinsic}
                    calib_key = (
                        f"{storage_root}/{scene_name}/calib/camera/{channel}.json"
                    )
                    calib_bytes = json.dumps(calib_payload).encode("utf-8")
                    storage_service.client.put_object(
                        Bucket=bucket, Key=calib_key, Body=calib_bytes
                    )
                    calib_item_id = uuid.uuid4()
                    append_entries.append(
                        OperatorSourceEntry(
                            id=calib_item_id,
                            file_name=f"{channel}.json",
                            file_path=calib_key,
                            file_type="other",
                            file_size=len(calib_bytes),
                            content_hash=hashlib.sha256(calib_bytes).hexdigest(),
                        )
                    )
                    calib_item_ids.append(calib_item_id)

            calib_written = True

        await assets.append_all_items(ds.id, append_entries)
        total_items += len(append_entries)

        # 显式建 scene + 赋 frame_index(不依赖目录启发式 inference)
        scene_result = await spatial.create_scene(
            CreateSceneRequest(
                operation_id=uuid.uuid4(),
                dataset_id=ds.id,
                name=scene_name,
                source_format="nuscenes",
                source_metadata={
                    "scene_token": scene_token,
                    "first_sample_token": scene_row["first_sample_token"],
                    "frame_count": len(samples),
                    "duration_s": (last_ts - first_ts) / 1e6,
                    "nuscenes_version": version,
                    "frame": frame,
                    "timestamp_delta_us": last_ts - first_ts,
                    "nuscenes_export": {
                        "version": version,
                        "scene": scene_row,
                        "log": log_row,
                        "map": map_row,
                        "map_storage_key": map_storage_key,
                        "map_file_size": len(map_bytes),
                        "map_sha256": hashlib.sha256(map_bytes).hexdigest(),
                    },
                },
                created_by=owner_id,
            ),
            same_session_native=True,
        )
        scene = scene_result.scene
        assignments = [
            SceneAssignment(item_id=item_id, scene_id=scene.id, frame_index=index)
            for index, item_id in enumerate(lidar_item_ids)
        ]
        assignments.extend(
            SceneAssignment(item_id=item_id, scene_id=scene.id, frame_index=frame_index)
            for item_id, frame_index in cam_frame_index.items()
        )
        assignments.extend(
            SceneAssignment(item_id=item_id, scene_id=scene.id, frame_index=None)
            for item_id in calib_item_ids
        )
        await spatial.assign_items(
            AssignItemsRequest(
                operation_id=uuid.uuid4(),
                dataset_id=ds.id,
                assignments=assignments,
            ),
            same_session_native=True,
        )
        # v0.15.0 · 逐帧 ego pose 落库(幂等 upsert);已存在同名 scene 走上方
        # skip 分支不会到这里,补历史 scene 用 scripts/backfill_frame_poses.py
        await spatial.upsert_poses(
            UpsertPosesRequest(
                operation_id=uuid.uuid4(), scene_id=scene.id, poses=frame_poses
            ),
            same_session_native=True,
        )
        report_scenes.append({"name": scene_name, "frames": len(samples)})

    # 3. Build/reuse the lidar workspace, associate the dataset, and create tasks.
    project_display_id = project_display_id or _derived_display_id(
        "P-NU-", dataset_name
    )
    project_id = await NuScenesImportWorkflow(db).create_project_and_tasks(
        dataset_id=ds.id,
        project_display_id=project_display_id,
        project_name=project_name or f"nuScenes {dataset_name}",
        owner_id=owner_id,
        tool_bindings=tool_bindings,
    )

    return {
        "dataset_id": ds.id,
        "project_id": project_id,
        "scenes": report_scenes,
        "total_items": total_items,
    }


# --------------------------------------------------------------------------- #
# admin / CLI
# --------------------------------------------------------------------------- #


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

    from app.config import settings
    from app.db.base import async_session
    from app.services.identity_commands import IdentityCommandService

    async with async_session() as db:
        owner_id = await IdentityCommandService(db).ensure_import_admin(
            environment=settings.environment
        )
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
