"""v0.13.1 · 点云 scene 建任务 + 标定写入 service。

scene 文件入库后，DatasetItem.file_path 是 MinIO storage key（保留子目录），形如:
    <dataset_name>/lidar/000970.pcd          file_type=point_cloud
    <dataset_name>/camera/front/000970.jpg   file_type=image
    <dataset_name>/camera/left/000970.jpg
    <dataset_name>/calib/camera/front.json   file_type=other

帧 id = lidar / 相机文件名 stem（如 000970）；相机名 = camera/ 后那一段（如 front）；
标定 calib/camera/<cam>.json 对该相机所有帧通用。

本模块只负责「帧分组 → 逐帧建 Task + link → 写标定」，分流接线由调用方完成，
不改 dataset.py 的 build_tasks_for_link。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import PurePosixPath

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DatasetItem, Project, Task
from app.schemas._jsonb_types import SensorCalibration
from app.services import async_job as async_job_svc
from app.services.dataset_storage_keys import dataset_item_relative_path
from app.services.role_patterns import (
    DEFAULT_ROLE_PATTERNS,
    RolePatterns,
    last_role_index,
    matches_role_part,
)
from app.services.storage import storage_service
from app.services.task_dataset_link import link_items

logger = logging.getLogger(__name__)

# 点云扩展名集合：file_type 未必准（入库可能落 other），故同时按后缀兜底。
_POINT_CLOUD_EXTS = {".pcd", ".bin", ".ply", ".las", ".laz", ".npy"}

# 相机图像直接放在角色目录下(如 camera/000.jpg、<scene>/camera/000.jpg)而无 channel
# 子目录时的默认 channel 名。此前回退到 parts[camera_i](即角色目录名 "camera" 自身),
# 会让 link role 变成畸形的 camera_camera、且 calib/axis_sniffer 反查拿错 sensor。
_DEFAULT_CAMERA_CHANNEL = "default"


def group_frames(
    items: list[DatasetItem],
    patterns: RolePatterns = DEFAULT_ROLE_PATTERNS,
) -> tuple[dict, dict]:
    """把一批 DatasetItem 按帧分组。

    返回 (frames, calib_items):
      frames: {frame_id: {"lidar": item | None, "cameras": {cam: item}}}
      calib_items: {cam: item}

    分组规则（按逻辑相对路径的 PurePosixPath.parts，旧样本回退 file_path）:
      - calib: 末两段是 calib/camera 且后缀 .json → cam = stem(front)，记入 calib_items。
      - lidar: parts 命中 lidar pattern 且 (file_type==point_cloud 或后缀属点云集) → frame=stem。
      - camera: parts 命中 camera pattern → cam = camera 后一段或 camera 段名，frame=stem。
    判定顺序优先 calib，避免 calib/camera 路径被 camera 规则误吞。
    """
    frames: dict[str, dict] = {}
    calib_items: dict[str, DatasetItem] = {}

    for item in items:
        path = PurePosixPath(dataset_item_relative_path(item))
        parts = path.parts
        stem = path.stem
        suffix = path.suffix.lower()

        calib_i = last_role_index(parts, patterns.calib)
        # calib/camera/<cam>.json 或 calibration/<cam>.json。
        if suffix == ".json" and (
            (
                len(parts) >= 3
                and matches_role_part(parts[-2], patterns.camera)
                and calib_i == len(parts) - 3
            )
            or calib_i == len(parts) - 2
        ):
            calib_items[stem] = item
            continue

        lidar_i = last_role_index(parts, patterns.lidar)
        if lidar_i >= 0 and (
            item.file_type == "point_cloud" or suffix in _POINT_CLOUD_EXTS
        ):
            frame = frames.setdefault(stem, {"lidar": None, "cameras": {}})
            frame["lidar"] = item
            continue

        camera_i = last_role_index(parts, patterns.camera)
        if camera_i >= 0 and camera_i + 1 < len(parts):
            # camera/<channel>/<file> → channel(取 channel 子目录名)。
            # 无 channel 子目录(<dir>/<file>)时取角色目录名 <dir> 自身作 channel:
            #   - 携带 channel 的角色名(camera_image_0 / camera_front)→ 保留,即真实 channel;
            #   - 裸通用角色名(camera / image / cam ...)→ 回退默认 channel,
            #     否则 link role 会退化成畸形的 camera_camera 且 calib 反查拿错 sensor。
            role_dir = parts[camera_i]
            if camera_i + 1 < len(parts) - 1:
                cam = parts[camera_i + 1]
            elif role_dir.lower() in {p.lower() for p in patterns.camera}:
                cam = _DEFAULT_CAMERA_CHANNEL
            else:
                cam = role_dir
            frame = frames.setdefault(stem, {"lidar": None, "cameras": {}})
            frame["cameras"][cam] = item
            continue

    return frames, calib_items


async def _load_dataset_items(
    db: AsyncSession, dataset_id: uuid.UUID
) -> list[DatasetItem]:
    result = await db.execute(
        select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
    )
    return list(result.scalars().all())


async def _maybe_infer_single_scene(db: AsyncSession, *, dataset_id: uuid.UUID) -> None:
    """v0.14.0 · 点云 link 接线前的 scene hook。

    若 dataset 已有 scene 直接返回;否则跑 single-mode inference。
    SUSTechPOINTS / wizard 上传走这里 → 自动建出 1 scene + frame_index。
    """
    from app.services import scene as scene_svc
    from app.services import scene_inference

    existing = await scene_svc.list_for_dataset(db, dataset_id)
    if existing:
        return
    await scene_inference.infer_and_apply(db, dataset_id=dataset_id, mode="single")
    await db.flush()


async def build_pointcloud_tasks_for_link(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    chunk_size: int = 5000,
    directory_authority=None,
) -> dict:
    """v0.13.1 · 为点云 scene dataset link 分块建 task（镜像 build_tasks_for_link）。

    单位是「帧」而非 item：先帧分组，按 frame_id 排序后逐帧建一个 Task（dataset_item_id
    指向该帧 primary_lidar item），再用 link_items 关联 lidar + 各相机。

    幂等：跳过该 project 下、dataset_item_id == 本帧 lidar item 已存在 Task 的帧
    （NOT EXISTS，键是 lidar item）。每块独立 commit、累加 project.total_tasks、
    传 job_id 时按 5% 粒度 update_progress。返回 {"created": N, "total": M}。

    幂等粒度限制（已知）：键是「lidar Task 是否已存在」而不是「该 Task 的 link
    数量是否齐备」。正常路径下 chunk 失败整批回滚（Task + link 一起消失），重跑
    能补全；但若进程在「`db.flush()` 拿到 task.id + `link_items` 写完一部分
    role」与「`db.commit()`」之间硬挂（OOM / SIGKILL / OS panic），可能留下
    Task 已存在但 `camera_*` link 残缺的孤儿帧。重跑会被 existing 跳过,缺失
    link 永远不会被补,对应帧 manifest 的 `cameras` 列表少几个相机。补建残缺
    link 是独立入口职责(未实现,见 follow-up)。

    v0.14.0 · 在建 task 之前自动跑 scene_inference(mode="single"):若 dataset
    尚无 scene,推断出 1 个 scene + 给 lidar/cam items 写 frame_index,这样建出
    的 task 通过 dataset_item_id → scene_id 反查能拿到 frame_index。
    """
    from app.services.dataset_source_access import validate_dataset_write

    await validate_dataset_write(
        db, dataset_id, directory_authority, project_id=project_id, lock=True
    )
    # v0.14.0 · scene 推断 hook(放在 _load_dataset_items 之前,避免对已 assign 的 items
    # 重复 inference;infer_and_apply 内部本身也幂等,这层是性能优化)。
    await _maybe_infer_single_scene(db, dataset_id=dataset_id)

    items = await _load_dataset_items(db, dataset_id)

    # v0.14.2 · 按 scene_id 分桶后各 scene 独立 group_frames：upload-zip 不保证
    # 帧 stem 全局唯一（scene_a/lidar/000.pcd 与 scene_b/lidar/000.pcd 都 stem=000），
    # 若在全 dataset 范围 group_frames，同号帧会互相 setdefault 覆盖 → 漏建 task、
    # 跨 scene 串相机。分桶后 group_frames 的 stem key 只需在单 scene 内唯一即可。
    # scene_id=None（无 scene 历史数据）单独成一桶，保持原行为。
    buckets: dict[uuid.UUID | None, list[DatasetItem]] = {}
    for item in items:
        buckets.setdefault(item.scene_id, []).append(item)

    # frame key 复合 (scene_id, stem)，避免跨 scene 同号帧在 frames 字典再次撞键。
    frames: dict[tuple, dict] = {}
    for scene_id, scene_items in buckets.items():
        scene_frames, _ = group_frames(scene_items)
        for stem, frame in scene_frames.items():
            frames[(scene_id, stem)] = frame

    # 缺帧容错：无 lidar 的「帧」跳过（warning）。
    frame_ids: list[tuple] = []
    for frame_id in sorted(frames.keys(), key=lambda k: (str(k[0]), k[1])):
        if frames[frame_id]["lidar"] is None:
            logger.warning(
                "pointcloud frame %r has no lidar item, skipped (dataset=%s)",
                frame_id,
                dataset_id,
            )
            continue
        frame_ids.append(frame_id)

    if not frame_ids:
        return {"created": 0, "total": 0}

    # 幂等去重：该 project 下、dataset_item_id == 本帧 lidar item 已有 Task 的帧跳过。
    lidar_ids = [frames[f]["lidar"].id for f in frame_ids]
    existing_q = select(Task.dataset_item_id).where(
        Task.project_id == project_id,
        Task.dataset_item_id.in_(lidar_ids),
    )
    existing = {row[0] for row in (await db.execute(existing_q)).all()}
    pending_frame_ids = [f for f in frame_ids if frames[f]["lidar"].id not in existing]

    total_pending = len(pending_frame_ids)
    if total_pending == 0:
        return {"created": 0, "total": 0}

    created = 0
    last_pct = 0
    for start in range(0, total_pending, chunk_size):
        await validate_dataset_write(
            db, dataset_id, directory_authority, project_id=project_id, lock=True
        )
        chunk = pending_frame_ids[start : start + chunk_size]
        seq_result = await db.execute(
            text("SELECT nextval('display_seq_tasks') FROM generate_series(1, :n)"),
            {"n": len(chunk)},
        )
        display_nums = [row[0] for row in seq_result.all()]

        for i, frame_id in enumerate(chunk):
            frame = frames[frame_id]
            lidar = frame["lidar"]
            # 逐帧 add + flush 拿 task.id 再 link（scene 帧数远小于 2D item 数，
            # 性能可接受；批插 insert(Task) 不回拿 id 无法建 link）。
            task = Task(
                project_id=project_id,
                dataset_item_id=lidar.id,
                batch_id=None,
                display_id=f"T-{display_nums[i]}",
                file_name=lidar.file_name,
                file_path=lidar.file_path,
                file_type="point_cloud",
                status="pending",
            )
            db.add(task)
            await db.flush()

            link_payload: list[tuple[uuid.UUID, str, str | None]] = [
                (lidar.id, "primary_lidar", None)
            ]
            for cam, cam_item in frame["cameras"].items():
                link_payload.append((cam_item.id, f"camera_{cam}", cam))
            await link_items(db, task.id, link_payload)

        created += len(chunk)

        project = await db.get(Project, project_id)
        if project:
            project.total_tasks = (project.total_tasks or 0) + len(chunk)

        if job_id is not None:
            pct = int(created / total_pending * 100)
            if pct >= last_pct + 5 or created == total_pending:
                await async_job_svc.update_progress(db, job_id, pct)
                last_pct = pct

        await db.commit()

    return {"created": created, "total": total_pending}


async def attach_calibration(db: AsyncSession, *, dataset_id: uuid.UUID) -> int:
    """v0.13.1 · 读 calib/camera/<cam>.json 写入该相机所有帧 DatasetItem 的 metadata。

    对每个 calib item：从 MinIO 读字节 → json.loads → 经 SensorCalibration 归一化后落库。
    归一化做两件事（v0.13.2 起，原先存原始 dict + 仅长度校验）：
      1. 只保留 schema 已建模字段（extrinsic / intrinsic / rect），剥掉上游/厂商夹带的
         杂键（如 SUSTechPOINTS 示例 left.json 的 extrinsic_ok）；
      2. 过 SensorCalibration 校验（extrinsic=16 / intrinsic=9 / rect=16），非法则
         warning 跳过该相机（不抛）。
    入库即「单一真值的干净标定」，读取端（manifest）不会再因杂键被 extra="forbid" 判废。
    返回写入的相机项数（DatasetItem 行数）。
    """
    items = await _load_dataset_items(db, dataset_id)
    frames, calib_items = group_frames(items)

    # cam -> [该相机所有帧的 DatasetItem]
    cam_to_items: dict[str, list[DatasetItem]] = {}
    for frame in frames.values():
        for cam, cam_item in frame["cameras"].items():
            cam_to_items.setdefault(cam, []).append(cam_item)

    known_fields = set(SensorCalibration.model_fields)
    written = 0
    for cam, calib_item in calib_items.items():
        raw = _read_calibration(calib_item.file_path)
        if raw is None:
            continue
        # 先剥未建模键再校验：杂键不应让一份内外参合法的标定整体作废。
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        try:
            calib = SensorCalibration.model_validate(filtered)
        except ValidationError as exc:
            logger.warning("calibration for camera %r invalid, skipped: %s", cam, exc)
            continue
        normalized = calib.model_dump(exclude_none=True)

        for cam_item in cam_to_items.get(cam, []):
            # 整体重新赋值整个 dict 以触发 SQLAlchemy JSONB 变更检测。
            cam_item.metadata_ = {
                **(cam_item.metadata_ or {}),
                "calibration": normalized,
            }
            written += 1

    await db.flush()
    return written


def _read_calibration(key: str) -> dict | None:
    """从 datasets 桶读取 calib JSON 字节并解析。读取 / 解析失败返回 None。

    抽成小函数便于测试注入（monkeypatch storage_service.client.get_object 即可）。
    """
    try:
        body = storage_service.client.get_object(
            Bucket=storage_service.datasets_bucket, Key=key
        )["Body"].read()
        return json.loads(body)
    except Exception:
        logger.warning("failed to read calibration object %r", key, exc_info=True)
        return None
