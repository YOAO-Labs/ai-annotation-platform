"""导出打包编排（2026-05-20 计划 §4 阶段 3）。

在现有 ExportService 之上做"目录镜像"重组：

- `relative_path_from_file_path`：把完整 MinIO key（`{dataset}/animals/cat/001.jpg`）
  剥成数据集内相对路径（`animals/cat/001.jpg`），是导出镜像的核心 helper（导入端可共用）。
- `build_export_zip`：复用 ExportService 加载数据 + 派生类别/属性 schema，但 label 文件
  按 `{project_id}/{dataset_id}/labels/<rel>.txt` 镜像组织（消除 file_name 叶子名同名覆盖），
  并附带 data.yaml / images_manifest.json / fetch_images.py。COCO 像素坐标改用
  DatasetItem.width/height 真值（顺修硬编码 IMG_W/IMG_H bug）。

复用策略（见汇报）：
- YOLO：不复用 export_yolo 的扁平 ZIP 写法（它把 label 路径写死成叶子名），改在此处用
  ExportService 内部加载结果 + geometry helper 自己写镜像目录。
- COCO / AAP JSON：是单文档格式（无 per-image label 文件），直接调 ExportService 现有方法拿
  JSON 字符串落包根，COCO 额外传入 dataset_items 让坐标用真值。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import uuid
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from posixpath import splitext
from typing import Any, AsyncIterator

import numpy as np
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.scene_pose import SceneFramePose
from app.schemas._jsonb_types import SensorCalibration
from app.services.exporting.service import (
    ExportService,
    UnsupportedExportError,
    _bbox_geometry,
)
from app.services.axis_convention import AxisFrame, R_NORM
from app.services.exporting.lidar import (
    LidarCameraExportCtx,
    LidarFrameExportCtx,
    MulticameraCocoImageCtx,
    NuScenesSensorExportCtx,
    build_multicamera_coco,
    build_nuscenes_frame_records,
    build_kitti_lidar_frame,
    build_pointmask_label_bytes,
    category_map_json,
    count_lidar_points_in_boxes,
)
from app.services.exporting.lidar_preflight import calibration_is_valid
from app.services.exporting.video import (
    FALLBACK_H,
    FALLBACK_W,
    VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES,
    VIDEO_TRACK_GEOMETRY_TYPES,
    build_coco_frames_seg,
    build_kitti_labels,
    build_mot_gt,
    build_mot_seqinfo,
    build_yolo_frame_det_labels,
    build_yolo_frame_seg_labels,
    yolo_seg_line,
)
from app.services.exporting.video_scope import VideoExportScope
from app.services.exporting.davis import (
    build_davis_palette_png,
    derive_davis_object_ids,
)
from app.services.exporting.video_mask_formats import (
    build_mots_sequence,
    exact_mask_frames,
    object_manifest,
)
from app.services.project import (
    derive_attribute_schema,
    derive_classes_list,
)
from app.services.sensor_calibration import resolve_calibration_states
from app.services.storage import storage_service
from app.services.mask_formats import registry as mask_format_registry
from app.services.mask_formats.image_export import (
    write_binary_png_export,
    write_indexed_png_export,
    write_label_studio_export,
    yolo_seg_lines_with_masks,
)
from app.services.raster_mask_storage import load_coco_rle
from app.services.point_cloud_quality import parse_pcd_positions
from app.config import settings
from app.utils.raster_mask_rle import coco_rle_bbox_norm
from app.services.video_frame_service import derive_sampled_frames, derive_step
from app.services.video_tracks import derive_track_number

VIDEO_EXPORT_FORMATS = {
    "video_json",
    "aap_json",
    "mot",
    "kitti",
    "yolo-frames-det",
    # 逐帧 YOLO 分割：保留多边形顶点（bbox-only 的 det 只能给外接框）。
    "yolo-frames-seg",
    # 逐帧 COCO 分割：同源多边形，编码为标准 COCO instance segmentation 单文档。
    "coco-frames-seg",
    "davis",
    "youtube-vos",
    "mots",
}


async def _hydrate_mask_geometry_for_export(geometry: dict) -> dict:
    if geometry.get("type") != "video_track_mask":
        return geometry
    hydrated = deepcopy(geometry)
    for keyframe in hydrated.get("keyframes") or []:
        rle = await load_coco_rle(keyframe.get("mask") or {})
        keyframe["mask_rle"] = rle
        keyframe["bbox"] = coco_rle_bbox_norm(rle)
    return hydrated


LIDAR_EXPORT_TARGETS = {
    "aap_json",
    "coco-multicamera",
    "kitti",
    "nuscenes",
    "pointmask",
}

# v0.10.43 · 多目标导出：图像目标（yolo 旧值=yolo-det）+ 视频目标 + voc（仅同步单目标）。
IMAGE_EXPORT_TARGETS = {
    "coco",
    "yolo",
    "yolo-det",
    "yolo-obb",
    "yolo-seg",
    "aap_json",
    "label-studio-brush",
    "binary-png",
    "indexed-png",
}
ALL_EXPORT_TARGETS = (
    IMAGE_EXPORT_TARGETS | VIDEO_EXPORT_FORMATS | LIDAR_EXPORT_TARGETS | {"voc"}
)


def clean_export_targets(targets: list[str], data_type: str | None = None) -> list[str]:
    """去重保序 + 校验目标合法。非法或空抛 ValueError（端点转 400）。

    v0.10.47 · 传入项目 ``data_type`` 时按模态过滤：图像项目只接受图像目标（+ voc），
    视频项目只接受视频目标。否则一个跨模态目标会通过端点校验、派发 job，随后在
    ``build_export_zip`` 抛 ``UnsupportedExportError`` 拖垮整批（含合法目标）。
    ``data_type=None`` 时退回旧行为（接受全集），保持向后兼容。
    """
    allowed = {
        adapter.descriptor.format_id
        for adapter in mask_format_registry.list(
            media_type=data_type,
            direction="export",
        )
    }
    seen: list[str] = []
    for t in targets:
        if t not in allowed:
            scope = f" for {data_type} project" if data_type else ""
            raise ValueError(f"unsupported export target{scope}: {t}")
        if t not in seen:
            seen.append(t)
    if not seen:
        raise ValueError("targets must not be empty")
    return seen


# 预签名 URL / 桶 lifecycle 对齐 7 天。
PRESIGN_EXPIRES_SECONDS = 7 * 24 * 3600


def relative_path_from_file_path(file_path: str, dataset_name: str) -> str:
    """剥掉 `{dataset_name}/` 前缀，得到数据集内相对路径（posix `/`）。

    `file_path` = 完整 MinIO key（如 `mydataset/animals/cat/001.jpg`），叶子名不足以唯一
    定位（同名跨目录会覆盖），故必须用相对路径镜像目录。

    前缀匹配（首段 == dataset_name）才剥；不匹配则原样返回（保守，避免误删层级）。
    """
    path = (file_path or "").lstrip("/")
    if not dataset_name:
        return path
    prefix = f"{dataset_name}/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


_FETCH_IMAGES_TEMPLATE = '''#!/usr/bin/env python3
"""按 images_manifest.json 的预签名 URL 把图片回源到 images/<相对路径>，与 labels/ 平行。

纯标准库，无需配置 MinIO 密钥（URL 已带 7 天签名）。本地已有数据集则无需运行本脚本。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    manifest_path = os.path.join(HERE, "images_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    items = manifest.get("images", [])
    # 过期校验：取最早 expires_at 提示。
    soonest = None
    for it in items:
        exp = it.get("expires_at")
        if exp:
            try:
                dt = datetime.fromisoformat(exp)
            except ValueError:
                continue
            if soonest is None or dt < soonest:
                soonest = dt
    if soonest is not None:
        now = datetime.now(timezone.utc)
        remaining = soonest - now
        if remaining.total_seconds() <= 0:
            print(
                "[!] 预签名链接已于 %s 过期，请回平台重新导出。" % soonest.isoformat()
            )
            return 2
        print(
            "[i] 预签名链接将于 %s 过期（剩约 %d 天），请尽快下载。"
            % (soonest.isoformat(), int(remaining.total_seconds() // 86400))
        )

    ok = 0
    fail = 0
    for it in items:
        rel = it.get("rel_path")
        url = it.get("presigned_url")
        if not rel or not url:
            continue
        dest = os.path.join(HERE, "images", *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            ok += 1
            continue
        try:
            urllib.request.urlretrieve(url, dest)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("[x] 下载失败 %s: %s" % (rel, exc))
            fail += 1
    print("[done] 成功 %d，失败 %d，输出目录 images/" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


_FETCH_POINTCLOUDS_TEMPLATE = '''#!/usr/bin/env python3
"""按 pointclouds_manifest.json 的预签名 URL 把点云回源到 velodyne/<相对路径>。

纯标准库，无需 MinIO 密钥（URL 已带 7 天签名）。label 索引对应这里回源的同一份点云。
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    with open(os.path.join(HERE, "pointclouds_manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    ok = 0
    fail = 0
    for it in manifest.get("pointclouds", []):
        rel = it.get("rel_path")
        url = it.get("presigned_url")
        if not rel or not url:
            continue
        dest = os.path.join(HERE, "velodyne", *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            ok += 1
            continue
        try:
            urllib.request.urlretrieve(url, dest)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("[x] 下载失败 %s: %s" % (rel, exc))
            fail += 1
    print("[done] 点云回源 成功 %d，失败 %d，输出目录 velodyne/" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


_FETCH_TRUSTED_MEDIA_TEMPLATE = '''#!/usr/bin/env python3
"""Materialize the exact trusted media tree from media_manifest.json."""
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def safe_destination(rel_path: str) -> str:
    if (
        not isinstance(rel_path, str)
        or not rel_path
        or "\\\\" in rel_path
        or ":" in rel_path
        or "\\0" in rel_path
    ):
        raise ValueError("unsafe media path")
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("unsafe media path")
    root = os.path.realpath(HERE)
    destination = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath((root, destination)) != root or destination == root:
        raise ValueError("unsafe media path")
    return destination


def file_matches(path: str, expected_size: int, expected_sha256: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) != expected_size:
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def main() -> int:
    with open(os.path.join(HERE, "media_manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    expires_at = manifest.get("expires_at")
    if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
        print("[!] 媒体链接已过期，请回平台重新导出。")
        return 2
    ok = 0
    fail = 0
    seen_destinations = set()
    for item in manifest.get("media", []):
        rel_path = item.get("rel_path")
        url = item.get("presigned_url")
        if not rel_path or not url:
            print("[x] 下载失败 %s: missing media path or URL" % (rel_path,))
            fail += 1
            continue
        temp_path = None
        try:
            expected_size = int(item["source_file_size"])
            expected_sha256 = str(item["source_sha256"]).lower()
            if (
                expected_size < 0
                or len(expected_sha256) != 64
                or any(char not in "0123456789abcdef" for char in expected_sha256)
            ):
                raise ValueError("invalid media fingerprint")
            dest = safe_destination(rel_path)
            if dest in seen_destinations:
                raise ValueError("duplicate media path")
            seen_destinations.add(dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if file_matches(dest, expected_size, expected_sha256):
                ok += 1
                continue
            with tempfile.NamedTemporaryFile(
                prefix=".aap-download-",
                dir=os.path.dirname(dest),
                delete=False,
            ) as temp_file:
                temp_path = temp_file.name
            urllib.request.urlretrieve(url, temp_path)
            if not file_matches(temp_path, expected_size, expected_sha256):
                raise ValueError("downloaded media fingerprint mismatch")
            os.replace(temp_path, dest)
            temp_path = None
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("[x] 下载失败 %s: %s" % (rel_path, exc))
            fail += 1
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
    print("[done] 可信媒体回源 成功 %d，失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''

# Backward-compatible import surface for existing nuScenes script tests.
_FETCH_NUSCENES_MEDIA_TEMPLATE = _FETCH_TRUSTED_MEDIA_TEMPLATE


def _dataset_name_for_task(task: Task, item: DatasetItem | None) -> str:
    """数据集名 = file_path 首段（Web/scan 导入时 file_path = key 含 dataset 前缀）。"""
    path = (task.file_path or "").lstrip("/")
    return path.split("/", 1)[0] if "/" in path else ""


def _label_rel(task: Task, item: DatasetItem | None) -> str:
    """label 相对路径（去 dataset 前缀，去扩展名）。"""
    dataset_name = _dataset_name_for_task(task, item)
    rel = relative_path_from_file_path(task.file_path, dataset_name)
    base, _ext = splitext(rel)
    return base


def _yolo_lines(
    anns: list[Annotation],
    cat_map: dict[str, int],
    include_attributes: bool,
) -> tuple[list[str], list[dict]]:
    lines: list[str] = []
    attrs_per_line: list[dict] = []
    for ann in anns:
        g = _bbox_geometry(ann)
        if g is None:
            continue
        cx = g["x"] + g["w"] / 2
        cy = g["y"] + g["h"] / 2
        cid = cat_map.get(ann.class_name, 0)
        lines.append(f"{cid} {cx:.6f} {cy:.6f} {g['w']:.6f} {g['h']:.6f}")
        if include_attributes:
            attrs_per_line.append(ann.attributes or {})
    return lines, attrs_per_line


# v0.10.43 · YOLO 变体写入器（det/obb/seg）。det=bbox，obb=rotated_bbox 四角，
# seg=polygon/multi_polygon 归一化多边形。坐标均归一化 [0,1]；obb 在像素空间旋转再归一化。
YOLO_TARGETS = {"yolo", "yolo-det", "yolo-obb", "yolo-seg"}


def _rotated_corners_norm(g: dict, w: int, h: int) -> list[float]:
    """rotated_bbox(cx,cy,w,h,angle°顺时针) → 归一化四角 [x1,y1,...,x4,y4]。

    归一化坐标 x、y 尺度不同（图像非正方形），旋转须在像素空间做再归一化。
    """
    cxp, cyp = g["cx"] * w, g["cy"] * h
    bw, bh = g["w"] * w, g["h"] * h
    rad = math.radians(g.get("angle", 0) or 0)
    cos, sin = math.cos(rad), math.sin(rad)
    out: list[float] = []
    for dx, dy in (
        (-bw / 2, -bh / 2),
        (bw / 2, -bh / 2),
        (bw / 2, bh / 2),
        (-bw / 2, bh / 2),
    ):
        rx = dx * cos - dy * sin
        ry = dx * sin + dy * cos
        out.extend([(cxp + rx) / w, (cyp + ry) / h])
    return out


def _seg_rings_norm(g: dict) -> list[list[list[float]]]:
    """polygon / multi_polygon → 归一化外环顶点列表（每环 [[x,y],...]）。"""
    t = g.get("type")
    if t == "polygon":
        pts = g.get("points") or []
        return [pts] if len(pts) >= 3 else []
    if t == "multi_polygon":
        rings = []
        for poly in g.get("polygons") or []:
            pts = poly.get("points") or []
            if len(pts) >= 3:
                rings.append(pts)
        return rings
    return []


def _yolo_target_lines(
    target: str,
    anns: list[Annotation],
    cat_map: dict[str, int],
    *,
    img_w: int,
    img_h: int,
    include_attributes: bool,
) -> tuple[list[str], list[dict]]:
    """按 YOLO 变体生成 label 行 + 对齐的 attrs（每产出一条目标物 append 一次属性）。"""
    if target in ("yolo", "yolo-det"):
        return _yolo_lines(anns, cat_map, include_attributes)
    lines: list[str] = []
    attrs: list[dict] = []
    for ann in anns:
        cid = cat_map.get(ann.class_name, 0)
        g = ann.geometry or {}
        produced = 0
        if target == "yolo-obb":
            if g.get("type") != "rotated_bbox":
                continue
            corners = _rotated_corners_norm(g, img_w, img_h)
            lines.append(f"{cid} " + " ".join(f"{c:.6f}" for c in corners))
            produced = 1
        elif target == "yolo-seg":
            rings = _seg_rings_norm(g)
            # 行格式与视频侧 yolo-frames-seg 共用 yolo_seg_line，避免两份实现漂移。
            lines.extend(yolo_seg_line(cid, ring) for ring in rings)
            produced = len(rings)
        if produced and include_attributes:
            attrs.extend([ann.attributes or {}] * produced)
    return lines, attrs


# v0.12.1 · B6 流式导出：打包函数消费 (tasks, ann_by_task, dataset_items) 的异步分块
# 迭代器（生产由 ExportService.iter_export_chunks 流式产出，测试可喂内存 chunk），ZIP 落盘
# tempfile 而非 io.BytesIO 攒整包，内存与 task 数解耦。
ExportChunkIter = AsyncIterator[
    tuple[list[Task], dict[uuid.UUID, list[Annotation]], dict[uuid.UUID, DatasetItem]]
]


def _new_zip_tempfile() -> str:
    """创建空 tempfile 供 ZipFile 落盘写入，返回路径（调用方负责清理）。"""
    fd, path = tempfile.mkstemp(prefix="aap-export-", suffix=".zip")
    os.close(fd)
    return path


class ExportQuotaError(ValueError):
    pass


class _QuotaZipFile(zipfile.ZipFile):
    def _check_entry(self, size_bytes: int) -> None:
        if len(self.filelist) >= settings.mask_format_max_archive_files:
            raise ExportQuotaError("resource_budget_exceeded:max_archive_files")
        if size_bytes > settings.mask_format_max_entry_bytes:
            raise ExportQuotaError("resource_budget_exceeded:max_entry_bytes")

    def _check_archive(self) -> None:
        if self.fp is not None:
            self.fp.flush()
        if os.path.getsize(self.filename) > settings.mask_format_temp_quota_bytes:
            raise ExportQuotaError("resource_budget_exceeded:temp_quota_bytes")

    def writestr(self, zinfo_or_arcname, data, compress_type=None, compresslevel=None):
        size_bytes = len(data.encode("utf-8") if isinstance(data, str) else data)
        self._check_entry(size_bytes)
        result = super().writestr(
            zinfo_or_arcname,
            data,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )
        self._check_archive()
        return result

    def write(self, filename, arcname=None, compress_type=None, compresslevel=None):
        self._check_entry(os.path.getsize(filename))
        result = super().write(
            filename,
            arcname=arcname,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )
        self._check_archive()
        return result


def _open_export_zip(path: str) -> _QuotaZipFile:
    return _QuotaZipFile(path, "w", zipfile.ZIP_DEFLATED)


def _export_size(path: str) -> int:
    size_bytes = os.path.getsize(path)
    if size_bytes > settings.mask_format_temp_quota_bytes:
        raise ExportQuotaError("resource_budget_exceeded:temp_quota_bytes")
    return size_bytes


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def build_export_zip(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    batch_id: uuid.UUID | None,
    task_ids: list[uuid.UUID] | None = None,
    targets: list[str],
    include_attributes: bool,
    video_frame_mode: str,
    axis_frame: AxisFrame = "iso",
    format_options: dict | None = None,
    video_scope: VideoExportScope | None = None,
) -> tuple[str, int, int]:
    """生成镜像目录 ZIP 到磁盘临时文件，返回 (zip 路径, label 文件数, size_bytes)。

    v0.12.1 · B6 · 落盘 + 流式：ZIP 写 tempfile（不再 io.BytesIO 攒整包驻留 RAM）；
    per-file 格式（YOLO 镜像）按 task 分块流式产出 annotation。COCO/AAP 是单文档 JSON，
    本质要全量物化（流式 JSON 编码不在本版范围），仍由 ExportService 自加载。
    调用方（worker）负责上传后清理返回的 zip 路径；本函数内部异常时清理自身临时文件。

    v0.10.43 · 多目标（方案 B）：单目标落包根（向后兼容旧布局），>1 目标各落 `{target}/` 子目录。
    图像 targets ∈ {coco, yolo-det, yolo-obb, yolo-seg, aap_json}（`yolo` 兼容旧 = yolo-det）。
    VOC 走旧同步路径，不在此处。
    """
    svc = ExportService(db, task_ids=task_ids)
    project = await svc.db.get(Project, project_id)

    tmp_path = _new_zip_tempfile()
    try:
        if project is None:
            with _open_export_zip(tmp_path):
                pass
            return tmp_path, 0, _export_size(tmp_path)

        # v0.10.31 · Phase 4.1 · 视频项目走独立组装（manifest + 视频回源脚本 + 多格式）。
        if project.data_type == "video":
            needs_ann = bool(
                {
                    "mot",
                    "kitti",
                    "yolo-frames-det",
                    "yolo-frames-seg",
                    "coco-frames-seg",
                    "davis",
                    "youtube-vos",
                    "mots",
                }
                & set(targets)
            )
            chunks = svc.iter_export_chunks(
                project_id,
                batch_id,
                with_annotations=needs_ann,
                video_scope=video_scope,
            )
            return await _build_video_export_zip(
                svc,
                project,
                chunks,
                tmp_path=tmp_path,
                batch_id=batch_id,
                targets=targets,
                include_attributes=include_attributes,
                video_frame_mode=video_frame_mode,
                format_options=format_options or {},
                video_scope=video_scope,
            )

        if project.data_type == "lidar":
            chunks = svc.iter_export_chunks(
                project_id,
                batch_id,
                with_annotations=bool(
                    {"kitti", "nuscenes", "pointmask"} & set(targets)
                ),
            )
            return await _build_lidar_export_zip(
                svc,
                project,
                chunks,
                tmp_path=tmp_path,
                batch_id=batch_id,
                targets=targets,
                include_attributes=include_attributes,
                format_options=format_options or {},
            )

        classes_list = derive_classes_list(project.tool_bindings)
        attribute_schema = derive_attribute_schema(project.tool_bindings)
        cat_map = {name: i for i, name in enumerate(classes_list)}

        multi = len(targets) > 1
        yolo_targets = [t for t in targets if t in YOLO_TARGETS]
        other_targets = [t for t in targets if t not in YOLO_TARGETS]
        custom_mask_targets = {
            "label-studio-brush",
            "binary-png",
            "indexed-png",
        }
        for target in other_targets:
            if target not in {"coco", "aap_json", *custom_mask_targets}:
                raise UnsupportedExportError(f"unsupported export target: {target}")
        has_yolo = bool(yolo_targets)
        # COCO 需 DatasetItem 真值尺寸算像素坐标；仅在请求 coco 时累积全量 items
        # （coco 本身单文档全量物化，O(N) items 不额外恶化）。纯 YOLO 不累积，保持流式。
        dataset_items_all: dict[uuid.UUID, DatasetItem] | None = (
            {} if "coco" in targets else None
        )

        file_count = 0
        total_tasks = 0
        now = datetime.now(timezone.utc)
        expires_iso = datetime.fromtimestamp(
            now.timestamp() + PRESIGN_EXPIRES_SECONDS, tz=timezone.utc
        ).isoformat()

        with _open_export_zip(tmp_path) as zf:
            # 共享产物（格式无关）：类别清单 + 属性 schema。
            zf.writestr("classes.txt", "\n".join(classes_list))
            if include_attributes:
                zf.writestr(
                    "attribute_schema.json",
                    json.dumps(attribute_schema, ensure_ascii=False, indent=2),
                )

            # Pass 1（仅 YOLO 目标）：流式写镜像 labels（需 annotation）。
            if yolo_targets:
                async for tasks, ann_by_task, dataset_items in svc.iter_export_chunks(
                    project_id, batch_id, with_annotations=True
                ):
                    for t in tasks:
                        item = (
                            dataset_items.get(t.dataset_item_id)
                            if t.dataset_item_id
                            else None
                        )
                        for target in yolo_targets:
                            prefix = f"{target}/" if multi else ""
                            rel = _label_rel(t, item)
                            img_w = (
                                int(item.width) if item and item.width else FALLBACK_W
                            )
                            img_h = (
                                int(item.height) if item and item.height else FALLBACK_H
                            )
                            if target == "yolo-seg":
                                lines, attrs_per_line = await yolo_seg_lines_with_masks(
                                    ann_by_task.get(t.id, []),
                                    cat_map,
                                    include_attributes=include_attributes,
                                )
                            else:
                                lines, attrs_per_line = _yolo_target_lines(
                                    target,
                                    ann_by_task.get(t.id, []),
                                    cat_map,
                                    img_w=img_w,
                                    img_h=img_h,
                                    include_attributes=include_attributes,
                                )
                            dataset_id = str(item.dataset_id) if item else "unknown"
                            base = f"{prefix}{project_id}/{dataset_id}/labels/{rel}"
                            zf.writestr(f"{base}.txt", "\n".join(lines))
                            file_count += 1
                            if include_attributes and attrs_per_line:
                                zf.writestr(
                                    f"{base}.attrs.json",
                                    json.dumps(
                                        {"attributes": attrs_per_line},
                                        ensure_ascii=False,
                                    ),
                                )
                for target in yolo_targets:
                    prefix = f"{target}/" if multi else ""
                    zf.writestr(f"{prefix}data.yaml", _build_data_yaml(classes_list))

            # Pass 2：流式写 images_manifest.json —— 边遍历边写 zip entry（O(1) 内存，
            # 不把十万条 manifest dict 攒进 RAM，这是 B6「内存与 task 数解耦」的关键），
            # 顺带计 task 数 + 累积 coco 所需 items（不需 annotation）。
            with zf.open("images_manifest.json", "w") as mf:
                mf.write(b'{"images": [')
                first = True
                async for tasks, _ann, dataset_items in svc.iter_export_chunks(
                    project_id, batch_id, with_annotations=False
                ):
                    total_tasks += len(tasks)
                    if dataset_items_all is not None:
                        dataset_items_all.update(dataset_items)
                    for t in tasks:
                        item = (
                            dataset_items.get(t.dataset_item_id)
                            if t.dataset_item_id
                            else None
                        )
                        dataset_name = _dataset_name_for_task(t, item)
                        img_rel = relative_path_from_file_path(
                            t.file_path, dataset_name
                        )
                        presigned = storage_service.generate_download_url(
                            t.file_path,
                            expires_in=PRESIGN_EXPIRES_SECONDS,
                            bucket=storage_service.datasets_bucket,
                        )
                        entry = {
                            "rel_path": img_rel,
                            "dataset_id": str(item.dataset_id) if item else None,
                            "presigned_url": presigned,
                            "expires_at": expires_iso,
                        }
                        mf.write(b"" if first else b", ")
                        mf.write(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
                        first = False
                mf.write(b'], "expires_at": ')
                mf.write(json.dumps(expires_iso).encode("utf-8"))
                mf.write(b"}")

            # 单文档目标（coco/aap_json）：本质全量物化，svc 自加载落包根/子目录。
            for target in other_targets:
                if target in custom_mask_targets:
                    continue
                prefix = f"{target}/" if multi else ""
                if target == "coco":
                    content = await svc.export_coco(
                        project_id,
                        batch_id=batch_id,
                        include_attributes=include_attributes,
                        video_frame_mode=video_frame_mode,
                        dataset_items=dataset_items_all or {},
                        axis_frame=axis_frame,
                    )
                else:  # aap_json
                    content = await svc.export_aap_json(
                        project_id,
                        batch_id=batch_id,
                        axis_frame=axis_frame,
                    )
                zf.writestr(f"{prefix}annotations.json", content)
                file_count += total_tasks

            export_options = format_options or {}
            for target in sorted(custom_mask_targets & set(targets)):
                prefix = f"{target}/" if multi else ""
                chunks = svc.iter_export_chunks(
                    project_id, batch_id, with_annotations=True
                )
                if target == "binary-png":
                    file_count += await write_binary_png_export(
                        zf,
                        prefix=prefix,
                        project=project,
                        chunks=chunks,
                    )
                elif target == "indexed-png":
                    file_count += await write_indexed_png_export(
                        zf,
                        prefix=prefix,
                        project=project,
                        chunks=chunks,
                        overlap_policy=str(
                            export_options.get("indexed_overlap_policy") or "error"
                        ),
                    )
                else:
                    file_count += await write_label_studio_export(
                        zf,
                        prefix=prefix,
                        chunks=chunks,
                        data_key=str(export_options.get("ls_data_key") or "image"),
                        from_name=str(export_options.get("ls_from_name") or "brush"),
                        to_name=str(export_options.get("ls_to_name") or "image"),
                    )

            zf.writestr("fetch_images.py", _FETCH_IMAGES_TEMPLATE)
            # 单 YOLO 目标根 data.yaml 已在上面写；纯 COCO/AAP 单目标补一份（兼容旧包结构）。
            if not multi and not has_yolo:
                zf.writestr("data.yaml", _build_data_yaml(classes_list))

        return tmp_path, file_count, _export_size(tmp_path)
    except BaseException:
        _safe_unlink(tmp_path)
        raise


def _build_data_yaml(classes_list: list[str]) -> str:
    """YOLO 训练入口：images/ 与 labels/ 平行（fetch_images.py 把图拉到 images/）。"""
    names = "\n".join(f"  {i}: {name}" for i, name in enumerate(classes_list))
    return (
        "# YOLO 数据集入口（由 AAP 导出生成）\n"
        "# images/ 由 fetch_images.py 按 images_manifest.json 回源；labels/ 已在包内。\n"
        "path: .\n"
        "train: images\n"
        "val: images\n"
        f"nc: {len(classes_list)}\n"
        "names:\n"
        f"{names}\n"
    )


def _build_video_yolo_data_yaml(classes_list: list[str]) -> str:
    """视频逐帧 YOLO 训练入口：fetch_frames.py 把帧抽到 images/。"""
    names = "\n".join(f"  {i}: {name}" for i, name in enumerate(classes_list))
    return (
        "# 视频逐帧 YOLO 数据集入口（由 AAP 导出生成）\n"
        "# images/ 由 fetch_frames.py 按 manifest.json 抽帧；labels/ 已在包内。\n"
        "path: .\n"
        "train: images\n"
        "val: images\n"
        f"nc: {len(classes_list)}\n"
        "names:\n"
        f"{names}\n"
    )


# ── v0.14.7 · LiDAR 标准训练格式导出 ─────────────────────────────────


def _lidar_frame_key(task: Task, item: DatasetItem | None) -> str:
    rel = _label_rel(task, item)
    return rel or task.display_id


def _calibration_for_item(item: DatasetItem | None) -> SensorCalibration | None:
    if item is None:
        return None
    raw = (item.metadata_ or {}).get("calibration")
    if raw is None:
        return None
    if not calibration_is_valid(raw):
        return None
    try:
        return SensorCalibration.model_validate(raw)
    except ValueError:
        return None


def _camera_name(link: TaskDatasetItemLink) -> str:
    if link.sensor_name:
        return link.sensor_name
    if link.role.startswith("camera_"):
        return link.role[len("camera_") :]
    return link.role


async def _load_lidar_link_items(
    svc: ExportService,
    tasks: list[Task],
) -> dict[uuid.UUID, dict[str, tuple[TaskDatasetItemLink, DatasetItem]]]:
    task_ids = [task.id for task in tasks]
    if not task_ids:
        return {}
    result = await svc.db.execute(
        select(TaskDatasetItemLink, DatasetItem)
        .join(DatasetItem, DatasetItem.id == TaskDatasetItemLink.dataset_item_id)
        .where(TaskDatasetItemLink.task_id.in_(task_ids))
        .order_by(TaskDatasetItemLink.role)
    )
    out: dict[uuid.UUID, dict[str, tuple[TaskDatasetItemLink, DatasetItem]]] = {}
    for link, item in result.all():
        out.setdefault(link.task_id, {})[link.role] = (link, item)
    return out


def _primary_lidar_item(
    task: Task,
    links_by_task: dict[uuid.UUID, dict[str, tuple[TaskDatasetItemLink, DatasetItem]]],
    dataset_items: dict[uuid.UUID, DatasetItem],
) -> DatasetItem | None:
    """v0.24 · chunk 内解析 task 的主点云 item(与导出循环 primary_item 同规则)。

    优先 primary_lidar link,回退 task.dataset_item_id;nuScenes pose 按
    (scene, frame) 精确查询时用它收集本 chunk 实际需要的 pose 对。
    """

    primary_pair = links_by_task.get(task.id, {}).get("primary_lidar")
    if primary_pair is not None:
        return primary_pair[1]
    if task.dataset_item_id:
        return dataset_items.get(task.dataset_item_id)
    return None


def _kitti_calib_text(calib: SensorCalibration) -> str:
    k = [float(v) for v in calib.intrinsic]
    p2 = [k[0], k[1], k[2], 0.0, k[3], k[4], k[5], 0.0, k[6], k[7], k[8], 0.0]
    if calib.rect:
        r = [float(v) for v in calib.rect]
        r0 = [r[0], r[1], r[2], r[4], r[5], r[6], r[8], r[9], r[10]]
    else:
        r0 = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0]
    e = [float(v) for v in calib.extrinsic]
    tr = [e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7], e[8], e[9], e[10], e[11]]
    return "\n".join(
        [
            "P2: " + " ".join(f"{v:.12g}" for v in p2),
            "R0_rect: " + " ".join(f"{v:.12g}" for v in r0),
            "Tr_velo_to_cam: " + " ".join(f"{v:.12g}" for v in tr),
            "",
        ]
    )


def _lidar_readme(target: str) -> str:
    common = (
        "AAP LiDAR export\n"
        "Images and point clouds are referenced by presigned manifests; run the "
        "fetch scripts before training if local media is needed.\n"
        "point_mask_3d labels index the point order of the fetched point cloud.\n"
    )
    if target == "kitti":
        return (
            common + "\nKITTI label_2 and calib use the explicitly selected camera. "
            "A persistent manual camera bbox is preferred over the derived projection; "
            "export_report.json records both source counts. Fully invisible cuboids are "
            "listed there as skipped.\n"
        )
    if target == "pointmask":
        return common + "\nPointmask labels are little-endian uint32 class ids.\n"
    if target == "nuscenes":
        return (
            "AAP trusted nuScenes keyframe export\n\n"
            "This package is compatible with the official nuscenes-devkit load/query "
            "contract after running fetch_nuscenes_media.py. It is not an official "
            "detection/tracking benchmark split and does not map project categories to "
            "the official ontology. JSON tables live in v1.0-aap/; media keeps its exact "
            "source-relative path. Presigned media links expire at the time recorded in "
            "media_manifest.json.\n"
        )
    if target == "coco-multicamera":
        return (
            "AAP trusted multi-camera COCO export\n\n"
            "annotations.json contains only persistent manual camera bounding boxes; "
            "it never fills missing labels from 3D projections. Every linked camera "
            "image is included, so images without annotations remain valid negative "
            "samples. Run fetch_media.py before training to materialize the exact "
            "fingerprinted images at their COCO file_name paths.\n"
        )
    return common


def _read_dataset_object(key: str) -> bytes:
    response = storage_service.client.get_object(
        Bucket=storage_service.datasets_bucket,
        Key=key,
    )
    body = response["Body"]
    try:
        return body.read()
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()


def _hash_dataset_object(key: str) -> tuple[int, str]:
    response = storage_service.client.get_object(
        Bucket=storage_service.datasets_bucket,
        Key=key,
    )
    body = response["Body"]
    size = 0
    digest = hashlib.sha256()
    try:
        while chunk := body.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    return size, digest.hexdigest()


def _trusted_media_entry(
    *,
    rel_path: str,
    storage_key: str,
    kind: str,
    source_file_size: int,
    source_sha256: str,
    expires_iso: str,
) -> dict[str, Any]:
    return {
        "rel_path": rel_path,
        "kind": kind,
        "source_file_size": source_file_size,
        "source_sha256": source_sha256,
        "presigned_url": storage_service.generate_download_url(
            storage_key,
            expires_in=PRESIGN_EXPIRES_SECONDS,
            bucket=storage_service.datasets_bucket,
        ),
        "expires_at": expires_iso,
    }


async def _build_lidar_export_zip(
    svc: ExportService,
    project: Project,
    chunks: ExportChunkIter,
    *,
    tmp_path: str,
    batch_id: uuid.UUID | None,
    targets: list[str],
    include_attributes: bool,
    format_options: dict[str, Any],
) -> tuple[str, int, int]:
    for target in targets:
        if target not in LIDAR_EXPORT_TARGETS:
            raise UnsupportedExportError(f"unsupported lidar export target: {target}")
    lidar_options = format_options.get("lidar") or {}
    selected_camera_role = lidar_options.get("kitti_camera_role")
    if "kitti" in targets and not selected_camera_role:
        raise UnsupportedExportError("kitti_camera_required")

    classes_list = derive_classes_list(project.tool_bindings)
    cat_map = {name: i + 1 for i, name in enumerate(classes_list)}
    attribute_schema = derive_attribute_schema(project.tool_bindings)
    allowed_attribute_keys = {
        field["key"]
        for field in attribute_schema.get("fields") or []
        if isinstance(field, dict) and field.get("key")
    }
    multi = len(targets) > 1
    needs_legacy_lidar_manifests = bool({"kitti", "pointmask"} & set(targets))
    file_count = 0
    frame_count = 0
    kitti_skipped: list[dict[str, str]] = []
    kitti_exported_count = 0
    kitti_manual_bbox_count = 0
    kitti_derived_bbox_count = 0
    image_manifest_all: list[dict] = []
    image_manifest_selected: list[dict] = []
    pointcloud_manifest: list[dict] = []
    nuscenes_frames: list[LidarFrameExportCtx] = []
    nuscenes_media: dict[str, dict[str, Any]] = {}
    multicamera_coco_images: list[MulticameraCocoImageCtx] = []
    multicamera_coco_media: dict[str, dict[str, Any]] = {}
    multicamera_media_fingerprints: dict[uuid.UUID, tuple[int, str]] = {}
    now = datetime.now(timezone.utc)
    expires_iso = datetime.fromtimestamp(
        now.timestamp() + PRESIGN_EXPIRES_SECONDS, tz=timezone.utc
    ).isoformat()

    with _open_export_zip(tmp_path) as zf:
        zf.writestr("classes.txt", "\n".join(classes_list))
        if include_attributes:
            zf.writestr(
                "attribute_schema.json",
                json.dumps(attribute_schema, ensure_ascii=False, indent=2),
            )

        async for tasks, ann_by_task, dataset_items in chunks:
            task_ids = [task.id for task in tasks]
            camera_members_by_task: dict[uuid.UUID, list[Annotation]] = {}
            if task_ids:
                camera_members = list(
                    (
                        await svc.db.execute(
                            select(Annotation)
                            .where(Annotation.task_id.in_(task_ids))
                            .where(Annotation.sensor_role.is_not(None))
                            .where(Annotation.geometry["type"].as_string() == "bbox")
                            .where(Annotation.is_active.is_(True))
                            .where(Annotation.was_cancelled.is_(False))
                            .order_by(Annotation.task_id, Annotation.id)
                        )
                    ).scalars()
                )
                for member in camera_members:
                    camera_members_by_task.setdefault(member.task_id, []).append(member)
            links_by_task = await _load_lidar_link_items(svc, tasks)
            multicamera_calibration_states = {}
            if "coco-multicamera" in targets:
                camera_items_by_id = {
                    item.id: item
                    for linked in links_by_task.values()
                    for role, (_link, item) in linked.items()
                    if role.startswith("camera_")
                }
                new_media_items = [
                    item
                    for item_id, item in camera_items_by_id.items()
                    if item_id not in multicamera_media_fingerprints
                ]

                async def hash_camera_item(
                    item: DatasetItem,
                ) -> tuple[DatasetItem, tuple[int, str]]:
                    return item, await asyncio.to_thread(
                        _hash_dataset_object, item.file_path
                    )

                for start in range(0, len(new_media_items), 4):
                    hashed = await asyncio.gather(
                        *[
                            hash_camera_item(item)
                            for item in new_media_items[start : start + 4]
                        ]
                    )
                    for item, fingerprint in hashed:
                        if fingerprint[0] != int(item.file_size or -1):
                            raise UnsupportedExportError(
                                f"multicamera_coco_media_drift:{item.id}"
                            )
                        multicamera_media_fingerprints[item.id] = fingerprint

                member_item_ids = {
                    member.sensor_dataset_item_id
                    for members in camera_members_by_task.values()
                    for member in members
                    if member.source == "manual"
                    and (member.geometry or {}).get("type") == "bbox"
                    and member.sensor_dataset_item_id in camera_items_by_id
                }
                if member_item_ids:
                    multicamera_calibration_states = await resolve_calibration_states(
                        svc.db,
                        [camera_items_by_id[item_id] for item_id in member_item_ids],
                    )
            axis_by_task = (
                await svc._axis_convention_by_task(tasks)
                if {"kitti", "nuscenes"} & set(targets)
                else {}
            )
            scenes_by_id: dict[uuid.UUID, Scene] = {}
            poses_by_frame: dict[tuple[uuid.UUID, int], SceneFramePose] = {}
            if "nuscenes" in targets:
                # v0.24 · pose 查询按 chunk 内主点云的 (scene, frame) 对过滤:
                # 大 scene 跨多个 1000-task chunk 时,逐 chunk 全量拉 scene pose
                # 会造成 ~N²/1000 行的重复加载;精确对过滤让每帧 pose 只取一次。
                chunk_pose_pairs: set[tuple[uuid.UUID, int]] = set()
                chunk_scene_ids: set[uuid.UUID] = set()
                for task in tasks:
                    chunk_primary = _primary_lidar_item(
                        task, links_by_task, dataset_items
                    )
                    if chunk_primary is None or chunk_primary.scene_id is None:
                        continue
                    chunk_scene_ids.add(chunk_primary.scene_id)
                    if chunk_primary.frame_index is not None:
                        chunk_pose_pairs.add(
                            (chunk_primary.scene_id, chunk_primary.frame_index)
                        )
                if chunk_scene_ids:
                    scenes_by_id = {
                        scene.id: scene
                        for scene in (
                            await svc.db.execute(
                                select(Scene).where(Scene.id.in_(chunk_scene_ids))
                            )
                        ).scalars()
                    }
                if chunk_pose_pairs:
                    poses_by_frame = {
                        (pose.scene_id, pose.frame_index): pose
                        for pose in (
                            await svc.db.execute(
                                select(SceneFramePose).where(
                                    tuple_(
                                        SceneFramePose.scene_id,
                                        SceneFramePose.frame_index,
                                    ).in_(chunk_pose_pairs)
                                )
                            )
                        ).scalars()
                    }
            for task in tasks:
                linked = links_by_task.get(task.id, {})
                primary_item = _primary_lidar_item(task, links_by_task, dataset_items)
                frame_key = _lidar_frame_key(task, primary_item)
                cameras: dict[str, LidarCameraExportCtx] = {}
                if needs_legacy_lidar_manifests:
                    for role, (link, item) in linked.items():
                        if not role.startswith("camera_"):
                            continue
                        cam = _camera_name(link)
                        if "kitti" in targets:
                            calib = _calibration_for_item(item)
                            if (
                                calib is not None
                                and item.width is not None
                                and item.width > 0
                                and item.height is not None
                                and item.height > 0
                            ):
                                cameras[role] = LidarCameraExportCtx(
                                    role=role,
                                    name=cam,
                                    calibration=calib,
                                    width=item.width,
                                    height=item.height,
                                )
                        manifest_item = {
                            "camera": cam,
                            "camera_role": role,
                            "frame": frame_key,
                            "width": item.width,
                            "height": item.height,
                            "rel_path": f"{cam}/{os.path.basename(item.file_path)}",
                            "dataset_id": str(item.dataset_id),
                            "presigned_url": storage_service.generate_download_url(
                                item.file_path,
                                expires_in=PRESIGN_EXPIRES_SECONDS,
                                bucket=storage_service.datasets_bucket,
                            ),
                            "expires_at": expires_iso,
                        }
                        image_manifest_all.append(manifest_item)
                        if role == selected_camera_role:
                            image_manifest_selected.append(manifest_item)
                        for target in targets:
                            if target == "kitti" and role != selected_camera_role:
                                continue
                            if target not in {"kitti", "pointmask"}:
                                continue
                            prefix = f"{target}/" if multi else ""
                            zf.writestr(f"{prefix}images/{cam}/", "")
                            zf.writestr(
                                f"{prefix}calib_raw/{cam}/{frame_key}.json",
                                json.dumps(
                                    (item.metadata_ or {}).get("calibration") or {},
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                            )
                if needs_legacy_lidar_manifests and primary_item is not None:
                    pointcloud_manifest.append(
                        {
                            "frame": frame_key,
                            "rel_path": os.path.basename(primary_item.file_path),
                            "dataset_id": str(primary_item.dataset_id),
                            "presigned_url": storage_service.generate_download_url(
                                primary_item.file_path,
                                expires_in=PRESIGN_EXPIRES_SECONDS,
                                bucket=storage_service.datasets_bucket,
                            ),
                            "expires_at": expires_iso,
                        }
                    )
                anns = ann_by_task.get(task.id, [])
                if "coco-multicamera" in targets:
                    task_members = camera_members_by_task.get(task.id, [])
                    for role, (_link, item) in sorted(linked.items()):
                        if not role.startswith("camera_"):
                            continue
                        members = [
                            member
                            for member in task_members
                            if member.source == "manual"
                            and member.sensor_role == role
                            and member.sensor_dataset_item_id == item.id
                            and (member.geometry or {}).get("type") == "bbox"
                        ]
                        source_basename = os.path.basename(item.file_path)
                        rel_path = f"images/{role}/{task.id}/{source_basename}"
                        size, sha256 = multicamera_media_fingerprints[item.id]
                        entry = _trusted_media_entry(
                            rel_path=rel_path,
                            storage_key=item.file_path,
                            kind="image",
                            source_file_size=size,
                            source_sha256=sha256,
                            expires_iso=expires_iso,
                        )
                        entry.update(
                            {
                                "task_id": str(task.id),
                                "dataset_item_id": str(item.id),
                                "sensor_role": role,
                            }
                        )
                        previous = multicamera_coco_media.setdefault(rel_path, entry)
                        if previous["source_sha256"] != entry["source_sha256"]:
                            raise UnsupportedExportError(
                                f"multicamera_coco_media_path_collision:{rel_path}"
                            )
                        state = multicamera_calibration_states.get(item.id)
                        scene_id = item.scene_id
                        frame_index = item.frame_index
                        if primary_item is not None:
                            scene_id = scene_id or primary_item.scene_id
                            if frame_index is None:
                                frame_index = primary_item.frame_index
                        multicamera_coco_images.append(
                            MulticameraCocoImageCtx(
                                task_id=task.id,
                                dataset_item_id=item.id,
                                sensor_role=role,
                                file_name=rel_path,
                                width=int(item.width),
                                height=int(item.height),
                                members=members,
                                scene_id=scene_id,
                                frame_index=frame_index,
                                current_calibration_revision=(
                                    state.revision if state is not None else None
                                ),
                                current_calibration_digest=(
                                    state.digest if state is not None else None
                                ),
                            )
                        )
                if "nuscenes" in targets:
                    if (
                        primary_item is None
                        or primary_item.scene_id is None
                        or primary_item.frame_index is None
                    ):
                        raise UnsupportedExportError(
                            f"nuscenes_scene_frame_missing:{task.id}"
                        )
                    scene = scenes_by_id.get(primary_item.scene_id)
                    pose = poses_by_frame.get(
                        (primary_item.scene_id, primary_item.frame_index)
                    )
                    if scene is None or pose is None or pose.timestamp_us is None:
                        raise UnsupportedExportError(
                            f"nuscenes_frame_pose_missing:{task.id}"
                        )
                    source_primary = (primary_item.metadata_ or {}).get(
                        "nuscenes_export"
                    )
                    if not isinstance(source_primary, dict):
                        raise UnsupportedExportError(
                            f"nuscenes_sensor_contract_missing:{task.id}"
                        )
                    sensor_items = {
                        role: item for role, (_link, item) in linked.items()
                    }
                    sensor_items.setdefault("primary_lidar", primary_item)
                    sensors: dict[str, NuScenesSensorExportCtx] = {}
                    for role, item in sorted(sensor_items.items()):
                        if role != "primary_lidar" and not role.startswith("camera_"):
                            continue
                        source = (item.metadata_ or {}).get("nuscenes_export")
                        if not isinstance(source, dict):
                            raise UnsupportedExportError(
                                f"nuscenes_sensor_contract_missing:{task.id}:{role}"
                            )
                        sensors[role] = NuScenesSensorExportCtx(
                            role=role,
                            dataset_item_id=item.id,
                            sample_data=dict(source["sample_data"]),
                            calibrated_sensor=dict(source["calibrated_sensor"]),
                            sensor=dict(source["sensor"]),
                            ego_pose=dict(source["ego_pose"]),
                            source_storage_key=str(source["source_storage_key"]),
                        )
                        rel_path = str(source["sample_data"]["filename"])
                        entry = _trusted_media_entry(
                            rel_path=rel_path,
                            storage_key=str(source["source_storage_key"]),
                            kind=str(source["sensor"]["modality"]),
                            source_file_size=int(source["source_file_size"]),
                            source_sha256=str(source["source_sha256"]),
                            expires_iso=expires_iso,
                        )
                        previous = nuscenes_media.setdefault(rel_path, entry)
                        if previous["source_sha256"] != entry["source_sha256"]:
                            raise UnsupportedExportError(
                                f"nuscenes_media_path_collision:{rel_path}"
                            )
                    scene_source = (scene.source_metadata or {}).get("nuscenes_export")
                    if not isinstance(scene_source, dict):
                        raise UnsupportedExportError(
                            f"nuscenes_scene_contract_missing:{scene.id}"
                        )
                    map_rel = str(scene_source["map"]["filename"])
                    map_entry = _trusted_media_entry(
                        rel_path=map_rel,
                        storage_key=str(scene_source["map_storage_key"]),
                        kind="map",
                        source_file_size=int(scene_source["map_file_size"]),
                        source_sha256=str(scene_source["map_sha256"]),
                        expires_iso=expires_iso,
                    )
                    previous_map = nuscenes_media.setdefault(map_rel, map_entry)
                    if previous_map["source_sha256"] != map_entry["source_sha256"]:
                        raise UnsupportedExportError(
                            f"nuscenes_media_path_collision:{map_rel}"
                        )
                    axis_convention = axis_by_task.get(task.id)
                    if axis_convention not in R_NORM:
                        raise UnsupportedExportError(
                            f"axis_convention_untrusted:{task.id}"
                        )
                    pcd_bytes = _read_dataset_object(primary_item.file_path)
                    if (
                        len(pcd_bytes) != primary_item.file_size
                        or hashlib.sha256(pcd_bytes).hexdigest()
                        != primary_item.content_hash
                    ):
                        raise UnsupportedExportError(
                            f"nuscenes_platform_pcd_drift:{task.id}"
                        )
                    points = parse_pcd_positions(pcd_bytes)
                    raw_lidar_points = int(source_primary["source_file_size"]) // 20
                    if len(points) != raw_lidar_points:
                        raise UnsupportedExportError(
                            f"nuscenes_lidar_point_count_drift:{task.id}"
                        )
                    norm = np.asarray(
                        R_NORM[axis_convention], dtype=np.float64
                    ).reshape(3, 3)
                    points_iso = np.asarray(points, dtype=np.float64) @ norm.T
                    nuscenes_frames.append(
                        LidarFrameExportCtx(
                            task_id=task.id,
                            frame_key=frame_key,
                            annotations=anns,
                            axis_convention=axis_convention,
                            scene_id=scene.id,
                            scene_name=scene.name,
                            scene_source_metadata=dict(scene.source_metadata or {}),
                            source_sample=dict(source_primary["sample"]),
                            frame_index=primary_item.frame_index,
                            timestamp_us=pose.timestamp_us,
                            ego_translation=list(pose.ego_translation),
                            ego_rotation=list(pose.ego_rotation),
                            sensors=sensors,
                            point_counts=count_lidar_points_in_boxes(points_iso, anns),
                        )
                    )
                frame_count += 1
                for target in targets:
                    prefix = f"{target}/" if multi else ""
                    if target == "kitti":
                        selected_camera = cameras.get(str(selected_camera_role))
                        if selected_camera is None:
                            raise UnsupportedExportError(
                                f"camera_frame_missing:{selected_camera_role}:{task.id}"
                            )
                        axis_convention = axis_by_task.get(task.id)
                        if (
                            not axis_convention
                            or axis_convention == "raw"
                            or axis_convention not in R_NORM
                        ):
                            raise UnsupportedExportError(
                                f"axis_convention_untrusted:{task.id}"
                            )
                        result = build_kitti_lidar_frame(
                            anns,
                            camera=selected_camera,
                            axis_convention=axis_convention,
                            camera_members=camera_members_by_task.get(task.id, []),
                        )
                        zf.writestr(
                            f"{prefix}label_2/{frame_key}.txt",
                            "\n".join(result.lines),
                        )
                        zf.writestr(
                            f"{prefix}calib/{frame_key}.txt",
                            _kitti_calib_text(selected_camera.calibration),
                        )
                        kitti_exported_count += len(result.lines)
                        kitti_manual_bbox_count += result.manual_bbox_count
                        kitti_derived_bbox_count += result.derived_bbox_count
                        kitti_skipped.extend(
                            {
                                "frame": frame_key,
                                "annotation_id": skipped.annotation_id,
                                "class_name": skipped.class_name,
                                "reason": skipped.reason,
                            }
                            for skipped in result.skipped
                        )
                        zf.writestr(f"{prefix}velodyne/", "")
                        file_count += 1
                    elif target == "pointmask":
                        source_point_count = None
                        if primary_item is not None:
                            source_point_count = (primary_item.metadata_ or {}).get(
                                "point_count"
                            )
                        zf.writestr(
                            f"{prefix}segmentation/{frame_key}.label",
                            build_pointmask_label_bytes(
                                anns,
                                source_point_count=source_point_count,
                                category_map=cat_map,
                            ),
                        )
                        zf.writestr(f"{prefix}velodyne/", "")
                        file_count += 1

        for target in targets:
            prefix = f"{target}/" if multi else ""
            if target == "aap_json":
                zf.writestr(
                    f"{prefix}annotations.json",
                    await svc.export_aap_json(project.id, batch_id=batch_id),
                )
                file_count += frame_count
                continue
            if target == "nuscenes":
                records = build_nuscenes_frame_records(nuscenes_frames)
                for table_name, rows in records.items():
                    zf.writestr(
                        f"{prefix}v1.0-aap/{table_name}.json",
                        json.dumps(
                            rows,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    )
                zf.writestr(f"{prefix}README.txt", _lidar_readme(target))
                zf.writestr(
                    f"{prefix}media_manifest.json",
                    json.dumps(
                        {
                            "contract": "nuscenes-devkit-load-query-keyframes-v1",
                            "media": [
                                nuscenes_media[path] for path in sorted(nuscenes_media)
                            ],
                            "expires_at": expires_iso,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                )
                zf.writestr(
                    f"{prefix}fetch_nuscenes_media.py",
                    _FETCH_NUSCENES_MEDIA_TEMPLATE,
                )
                file_count += len(nuscenes_frames)
                continue
            if target == "coco-multicamera":
                result = build_multicamera_coco(
                    multicamera_coco_images,
                    classes=classes_list,
                    include_attributes=include_attributes,
                    allowed_attribute_keys=allowed_attribute_keys,
                )
                zf.writestr(
                    f"{prefix}annotations.json",
                    json.dumps(
                        result.document,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
                zf.writestr(
                    f"{prefix}media_manifest.json",
                    json.dumps(
                        {
                            "contract": "multicamera-coco-media-v1",
                            "media": [
                                multicamera_coco_media[path]
                                for path in sorted(multicamera_coco_media)
                            ],
                            "expires_at": expires_iso,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                )
                zf.writestr(f"{prefix}fetch_media.py", _FETCH_TRUSTED_MEDIA_TEMPLATE)
                annotated_image_ids = {
                    annotation["image_id"]
                    for annotation in result.document["annotations"]
                }
                zf.writestr(
                    f"{prefix}export_report.json",
                    json.dumps(
                        {
                            "images": result.image_count,
                            "negative_images": (
                                result.image_count - len(annotated_image_ids)
                            ),
                            "manual_bbox_count": result.annotation_count,
                            "derived_bbox_count": 0,
                            "stale_relation_count": result.stale_relation_count,
                            "images_by_role": result.images_by_role,
                            "annotations_by_role": result.annotations_by_role,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                )
                zf.writestr(f"{prefix}README.txt", _lidar_readme(target))
                file_count += result.image_count
                continue
            zf.writestr(f"{prefix}README.txt", _lidar_readme(target))
            zf.writestr(
                f"{prefix}images_manifest.json",
                json.dumps(
                    {
                        "images": (
                            image_manifest_selected
                            if target == "kitti"
                            else image_manifest_all
                        ),
                        "expires_at": expires_iso,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            zf.writestr(
                f"{prefix}pointclouds_manifest.json",
                json.dumps(
                    {"pointclouds": pointcloud_manifest, "expires_at": expires_iso},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            zf.writestr(f"{prefix}fetch_images.py", _FETCH_IMAGES_TEMPLATE)
            zf.writestr(f"{prefix}fetch_pointclouds.py", _FETCH_POINTCLOUDS_TEMPLATE)
            if target == "kitti":
                zf.writestr(
                    f"{prefix}export_report.json",
                    json.dumps(
                        {
                            "camera_role": selected_camera_role,
                            "frames": frame_count,
                            "exported_annotations": kitti_exported_count,
                            "manual_bbox_count": kitti_manual_bbox_count,
                            "derived_bbox_count": kitti_derived_bbox_count,
                            "skipped_annotations": kitti_skipped,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            elif target == "pointmask":
                zf.writestr(
                    f"{prefix}category_map.json", category_map_json(classes_list)
                )

    return tmp_path, file_count, _export_size(tmp_path)


# ── v0.10.31 · Phase 4 视频导出组装 ──────────────────────────────────

_FETCH_VIDEOS_TEMPLATE = '''#!/usr/bin/env python3
"""按 manifest.json 的预签名 URL 把视频回源到 videos/<相对路径>。

纯标准库，无需 MinIO 密钥（URL 已带 7 天签名）。本地已有视频则跳过。
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    with open(os.path.join(HERE, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    ok = 0
    fail = 0
    for it in manifest.get("videos", []):
        rel = it.get("rel_path")
        url = it.get("presigned_url")
        if not rel or not url:
            continue
        dest = os.path.join(HERE, "videos", *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            ok += 1
            continue
        try:
            urllib.request.urlretrieve(url, dest)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("[x] 下载失败 %s: %s" % (rel, exc))
            fail += 1
    print("[done] 视频回源 成功 %d，失败 %d，输出目录 videos/" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


_FETCH_FRAMES_TEMPLATE = '''#!/usr/bin/env python3
"""按 manifest.json 的采样网格帧号，用本地 ffmpeg 从回源视频抽帧序列。

遵循 D1（不物理打包帧）：导出包只带标注 + 网格帧号，帧由本脚本就地抽取。
依赖：先跑 fetch_videos.py 回源视频；系统需安装 ffmpeg。
帧号语义（D2）：grid_source_frames 是源视频帧号；输出按抽取顺序编号，
start_number=1（MOT/YOLO，1-based）或 0（KITTI-only，0-based）。
输出目录由 frame_output_dirs 指定；旧 manifest 缺失时回退 {sequence}/img1。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    with open(os.path.join(HERE, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    ok = 0
    fail = 0
    for it in manifest.get("videos", []):
        rel = it.get("rel_path")
        seq = it.get("sequence")
        frames = it.get("grid_source_frames") or []
        start = int(it.get("frame_start_number", 1))
        if not rel or not seq or not frames:
            continue
        video_path = os.path.join(HERE, "videos", *rel.split("/"))
        if not os.path.exists(video_path):
            print("[x] 视频缺失（先跑 fetch_videos.py）: %s" % rel)
            fail += 1
            continue
        raw_specs = it.get("frame_outputs")
        if not raw_specs:
            raw_outputs = it.get("frame_output_dirs") or ["%s/img1" % seq]
            raw_specs = [
                {
                    "dir": rel_out,
                    "start_number": start,
                    "padding": 6,
                    "extension": "jpg",
                }
                for rel_out in raw_outputs
            ]
        output_specs = []
        seen_dirs = set()
        for spec in raw_specs:
            rel_out = spec.get("dir") if isinstance(spec, dict) else None
            if not rel_out or rel_out in seen_dirs:
                continue
            seen_dirs.add(rel_out)
            output_specs.append(spec)
        if not output_specs:
            continue
        try:
            for spec in output_specs:
                rel_out = spec["dir"]
                output_start = int(spec.get("start_number", start))
                padding = max(1, int(spec.get("padding", 6)))
                extension = str(spec.get("extension", "jpg")).lstrip(".") or "jpg"
                selected_frames = (
                    frames
                    if spec.get("source_frames") is None
                    else spec["source_frames"]
                )
                if not selected_frames:
                    continue
                select_expr = "+".join(
                    "eq(n\\\\,%d)" % fr for fr in selected_frames
                )
                out_dir = os.path.join(HERE, *rel_out.split("/"))
                os.makedirs(out_dir, exist_ok=True)
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                    "-vf", "select='%s'" % select_expr, "-fps_mode", "passthrough",
                    "-start_number", str(output_start),
                    os.path.join(out_dir, "%%0%dd.%s" % (padding, extension)),
                ]
                subprocess.run(cmd, check=True)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("[x] 抽帧失败 %s: %s" % (seq, exc))
            fail += 1
    print("[done] 抽帧 成功 %d 序列，失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def _video_seq_name(task: Task, item: DatasetItem | None) -> str:
    """sequence 名 = 数据集内相对路径去扩展名（保留层级，去重靠相对路径）。"""
    return _label_rel(task, item)


def _video_meta(item: DatasetItem | None) -> dict:
    if not item:
        return {}
    v = (item.metadata_ or {}).get("video")
    return v if isinstance(v, dict) else {}


async def _build_video_export_zip(
    svc: ExportService,
    project,
    chunks: ExportChunkIter,
    *,
    tmp_path: str,
    batch_id: uuid.UUID | None,
    targets: list[str],
    include_attributes: bool,
    video_frame_mode: str,
    format_options: dict[str, Any] | None = None,
    video_scope: VideoExportScope | None = None,
) -> tuple[str, int, int]:
    """视频项目 zip（v0.10.43 多目标）：单目标落根、>1 目标各落 `{target}/`；
    manifest.json + fetch_videos.py 共享落根（MOT/KITTI 另带 fetch_frames.py）。

    v0.12.1 · B6 · 落盘 + 按 task 分块流式：逐序列文件（MOT/KITTI/yolo-frames）边遍历
    chunk 边写盘；video_json/aap_json 单文档由 svc 自加载。ZIP 写入调用方给的 tmp_path，
    临时文件清理由调用方负责，返回 (zip 路径, 文件数, size_bytes)。
    """
    for tg in targets:
        if tg not in VIDEO_EXPORT_FORMATS:
            raise UnsupportedExportError(f"unsupported video export format: {tg}")

    format_options = format_options or {}
    sampling = project.video_sampling or {}
    multi = len(targets) > 1
    has_mot = "mot" in targets
    has_kitti = "kitti" in targets
    has_yolo_frames = "yolo-frames-det" in targets
    has_yolo_frames_seg = "yolo-frames-seg" in targets
    has_coco_frames_seg = "coco-frames-seg" in targets
    has_davis = "davis" in targets
    has_youtube_vos = "youtube-vos" in targets
    has_mots = "mots" in targets
    video_overlap_policy = str(format_options.get("video_overlap_policy") or "error")
    if video_overlap_policy not in {"error", "z_order", "larger_area", "smaller_area"}:
        raise ValueError("invalid video mask overlap policy")
    mots_frame_base = int(format_options.get("mots_frame_base", 0))
    if mots_frame_base not in {0, 1}:
        raise ValueError("MOTS frame base must be 0 or 1")
    has_frame_sequences = (
        has_mot
        or has_kitti
        or has_yolo_frames
        or has_yolo_frames_seg
        or has_coco_frames_seg
        or has_davis
        or has_youtube_vos
        or has_mots
    )
    frame_start_number = (
        1
        if has_mot or has_yolo_frames or has_yolo_frames_seg or has_coco_frames_seg
        else 0
    )

    classes_list = derive_classes_list(project.tool_bindings)
    attribute_schema = derive_attribute_schema(project.tool_bindings)
    cat_map = {name: i for i, name in enumerate(classes_list)}
    # coco-frames-seg 是单文档：跨 task 累积每序列的帧/几何，chunk 循环后一次性写 annotations.json。
    coco_seq_records: list[dict] = []
    davis_sequences: list[str] = []
    davis_sequence_owners: dict[str, uuid.UUID] = {}
    davis_manifest_sequences: dict[str, Any] = {}
    youtube_videos: dict[str, Any] = {}
    youtube_source_frames: dict[str, dict[str, int]] = {}
    mots_sequences: dict[str, Any] = {}

    manifest_videos: list[dict] = []
    now = datetime.now(timezone.utc)
    expires_iso = datetime.fromtimestamp(
        now.timestamp() + PRESIGN_EXPIRES_SECONDS, tz=timezone.utc
    ).isoformat()

    with _open_export_zip(tmp_path) as zf:
        for target in targets:
            prefix = f"{target}/" if multi else ""
            if target == "video_json":
                zf.writestr(
                    f"{prefix}annotations.json",
                    await svc.export_video_tracks(
                        project.id,
                        batch_id=batch_id,
                        include_attributes=include_attributes,
                        video_frame_mode=video_frame_mode,
                        video_scope=video_scope,
                    ),
                )
            elif target == "aap_json":
                zf.writestr(
                    f"{prefix}annotations.json",
                    await svc.export_aap_json(
                        project.id,
                        batch_id=batch_id,
                        video_scope=video_scope,
                    ),
                )
            elif target in ("yolo-frames-det", "yolo-frames-seg"):
                zf.writestr(f"{prefix}classes.txt", "\n".join(classes_list))
                if include_attributes:
                    zf.writestr(
                        f"{prefix}attribute_schema.json",
                        json.dumps(attribute_schema, ensure_ascii=False, indent=2),
                    )
                zf.writestr(
                    f"{prefix}data.yaml", _build_video_yolo_data_yaml(classes_list)
                )

        # 逐 task = sequence（按 chunk 流式）：写 MOT/KITTI/yolo-frames 逐序列文件 + 收集 manifest。
        file_count = 0
        total_tasks = 0
        async for tasks, ann_by_task, dataset_items in chunks:
            total_tasks += len(tasks)
            # 本块内按 task 分组：轨迹几何 → tracks，单帧几何 → bboxes。
            tracks_by_task: dict[uuid.UUID, list[Annotation]] = {}
            bboxes_by_task: dict[uuid.UUID, list[Annotation]] = {}
            for anns in ann_by_task.values():
                for ann in anns:
                    geometry_type = (ann.geometry or {}).get("type")
                    if geometry_type in VIDEO_TRACK_GEOMETRY_TYPES:
                        tracks_by_task.setdefault(ann.task_id, []).append(ann)
                    elif (
                        geometry_type
                        in VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES
                    ):
                        bboxes_by_task.setdefault(ann.task_id, []).append(ann)
            for t in tasks:
                item = (
                    dataset_items.get(t.dataset_item_id) if t.dataset_item_id else None
                )
                seq = _video_seq_name(t, item)
                vmeta = _video_meta(item)
                source_fps = vmeta.get("fps")
                img_w = int(
                    (item.width if item else None) or vmeta.get("width") or FALLBACK_W
                )
                img_h = int(
                    (item.height if item else None) or vmeta.get("height") or FALLBACK_H
                )
                step = derive_step(source_fps, sampling)

                track_anns = tracks_by_task.get(t.id, [])
                bbox_anns = bboxes_by_task.get(t.id, [])
                track_geometry_by_id = {
                    ann.id: await _hydrate_mask_geometry_for_export(ann.geometry or {})
                    for ann in track_anns
                }
                mask_track_annotations = [
                    ann
                    for ann in track_anns
                    if (ann.geometry or {}).get("type") == "video_track_mask"
                ]
                mask_tracks = [
                    (
                        ann.id,
                        int(getattr(ann, "z_order", 0) or 0),
                        track_geometry_by_id[ann.id],
                    )
                    for ann in mask_track_annotations
                ]
                youtube_sparse_frames: list[int] = []
                # frame_count：元数据优先，缺失回退最大标注帧 + 1。
                max_kf = 0
                for ann in track_anns:
                    for kf in (ann.geometry or {}).get("keyframes") or []:
                        max_kf = max(max_kf, int(kf.get("frame_index", 0)))
                for ann in bbox_anns:
                    max_kf = max(
                        max_kf, int((ann.geometry or {}).get("frame_index", 0))
                    )
                frame_count = int(vmeta.get("frame_count") or (max_kf + 1))
                frame_count = max(frame_count, max_kf + 1)
                selected_source_frames = derive_sampled_frames(frame_count, step)
                if video_scope is not None:
                    selected_source_frames = video_scope.filter_frames(
                        selected_source_frames
                    )

                if has_mot or has_kitti:
                    numbers = derive_track_number(
                        [(ann.id, track_geometry_by_id[ann.id]) for ann in track_anns]
                    )
                    track_args = [
                        (numbers[ann.id], ann.class_name, track_geometry_by_id[ann.id])
                        for ann in track_anns
                    ]
                    if has_mot:
                        mp = "mot/" if multi else ""
                        zf.writestr(
                            f"{mp}{seq}/gt/gt.txt",
                            build_mot_gt(
                                track_args,
                                frame_count=frame_count,
                                step=step,
                                img_w=img_w,
                                img_h=img_h,
                                source_frames=selected_source_frames,
                            ),
                        )
                        zf.writestr(
                            f"{mp}{seq}/seqinfo.ini",
                            build_mot_seqinfo(
                                seq.split("/")[-1],
                                source_fps=source_fps,
                                step=step,
                                frame_count=frame_count,
                                img_w=img_w,
                                img_h=img_h,
                                source_frames=selected_source_frames,
                            ),
                        )
                    if has_kitti:
                        kp = "kitti/" if multi else ""
                        zf.writestr(
                            f"{kp}labels/{seq}.txt",
                            build_kitti_labels(
                                track_args,
                                frame_count=frame_count,
                                step=step,
                                img_w=img_w,
                                img_h=img_h,
                                source_frames=selected_source_frames,
                            ),
                        )

                if has_yolo_frames:
                    yp = "yolo-frames-det/" if multi else ""
                    labels = build_yolo_frame_det_labels(
                        [
                            (
                                ann.class_name,
                                track_geometry_by_id[ann.id],
                                ann.attributes or {},
                            )
                            for ann in track_anns
                        ],
                        [
                            (ann.class_name, ann.geometry or {}, ann.attributes or {})
                            for ann in bbox_anns
                        ],
                        cat_map,
                        frame_count=frame_count,
                        step=step,
                        frame_start_number=frame_start_number,
                        include_attributes=include_attributes,
                        source_frames=selected_source_frames,
                    )
                    for frame_no, (lines, attrs_per_line) in sorted(labels.items()):
                        base = f"{yp}labels/{seq}/{frame_no:06d}"
                        zf.writestr(f"{base}.txt", "\n".join(lines))
                        file_count += 1
                        if include_attributes and attrs_per_line:
                            zf.writestr(
                                f"{base}.attrs.json",
                                json.dumps(
                                    {"attributes": attrs_per_line}, ensure_ascii=False
                                ),
                            )

                if has_yolo_frames_seg:
                    yp = "yolo-frames-seg/" if multi else ""
                    seg_labels = build_yolo_frame_seg_labels(
                        [
                            (ann.class_name, ann.geometry or {}, ann.attributes or {})
                            for ann in track_anns
                        ],
                        [
                            (ann.class_name, ann.geometry or {}, ann.attributes or {})
                            for ann in bbox_anns
                        ],
                        cat_map,
                        frame_count=frame_count,
                        step=step,
                        frame_start_number=frame_start_number,
                        include_attributes=include_attributes,
                        source_frames=selected_source_frames,
                    )
                    for frame_no, (lines, attrs_per_line) in sorted(seg_labels.items()):
                        base = f"{yp}labels/{seq}/{frame_no:06d}"
                        zf.writestr(f"{base}.txt", "\n".join(lines))
                        file_count += 1
                        if include_attributes and attrs_per_line:
                            zf.writestr(
                                f"{base}.attrs.json",
                                json.dumps(
                                    {"attributes": attrs_per_line}, ensure_ascii=False
                                ),
                            )

                if has_coco_frames_seg:
                    coco_seq_records.append(
                        {
                            "seq": seq,
                            "tracks": [
                                (
                                    ann.class_name,
                                    track_geometry_by_id[ann.id],
                                    ann.attributes or {},
                                    getattr(ann, "track_id", None)
                                    or track_geometry_by_id[ann.id].get("track_id"),
                                )
                                for ann in track_anns
                            ],
                            "bboxes": [
                                (
                                    ann.class_name,
                                    ann.geometry or {},
                                    ann.attributes or {},
                                    getattr(ann, "track_id", None)
                                    or (ann.geometry or {}).get("track_id"),
                                )
                                for ann in bbox_anns
                            ],
                            "frame_count": frame_count,
                            "step": step,
                            "img_w": img_w,
                            "img_h": img_h,
                            "source_frames": selected_source_frames,
                        }
                    )

                if has_davis:
                    previous_owner = davis_sequence_owners.get(seq)
                    if previous_owner is not None and previous_owner != t.id:
                        raise ValueError(
                            f"DAVIS sequence name collision after extension removal: {seq}"
                        )
                    davis_sequence_owners[seq] = t.id
                    davis_prefix = "davis/" if multi else ""
                    object_ids = derive_davis_object_ids(mask_tracks)
                    for output_index, source_frame in enumerate(selected_source_frames):
                        zf.writestr(
                            f"{davis_prefix}Annotations/Full-Resolution/{seq}/{output_index:05d}.png",
                            build_davis_palette_png(
                                mask_tracks,
                                frame_index=source_frame,
                                width=img_w,
                                height=img_h,
                                overlap_policy=video_overlap_policy,
                            ),
                        )
                        file_count += 1
                    davis_sequences.append(seq)
                    davis_manifest_sequences[seq] = {
                        "task_display_id": t.display_id,
                        "objects": object_manifest(
                            [
                                (
                                    ann.id,
                                    ann.class_name,
                                    track_geometry_by_id[ann.id],
                                )
                                for ann in mask_track_annotations
                            ],
                            object_ids,
                        ),
                    }

                if has_youtube_vos:
                    youtube_prefix = "youtube-vos/" if multi else ""
                    object_ids = derive_davis_object_ids(mask_tracks)
                    youtube_sparse_frames = sorted(
                        {
                            frame
                            for _annotation_id, _z_order, geometry in mask_tracks
                            for frame in exact_mask_frames(geometry)
                            if video_scope is None or video_scope.contains(frame)
                        }
                    )
                    frame_name_by_source = {
                        source_frame: f"{output_index:05d}"
                        for output_index, source_frame in enumerate(
                            youtube_sparse_frames
                        )
                    }
                    for source_frame in youtube_sparse_frames:
                        zf.writestr(
                            f"{youtube_prefix}Annotations/{seq}/"
                            f"{frame_name_by_source[source_frame]}.png",
                            build_davis_palette_png(
                                mask_tracks,
                                frame_index=source_frame,
                                width=img_w,
                                height=img_h,
                                overlap_policy=video_overlap_policy,
                                exact_keyframes_only=True,
                            ),
                        )
                        file_count += 1
                    youtube_objects = object_manifest(
                        [
                            (
                                ann.id,
                                ann.class_name,
                                track_geometry_by_id[ann.id],
                            )
                            for ann in mask_track_annotations
                        ],
                        object_ids,
                    )
                    for object_row in youtube_objects.values():
                        object_row["frames"] = [
                            frame_name_by_source[int(frame)]
                            for frame in object_row["frames"]
                        ]
                    youtube_videos[seq] = {"objects": youtube_objects}
                    youtube_source_frames[seq] = {
                        frame_name_by_source[frame]: frame
                        for frame in youtube_sparse_frames
                    }

                if has_mots:
                    mots_prefix = "mots/" if multi else ""
                    mots_path = f"{mots_prefix}instances/{seq}.txt"
                    mots_text, mots_meta = build_mots_sequence(
                        [
                            (
                                ann.id,
                                ann.class_name,
                                track_geometry_by_id[ann.id],
                            )
                            for ann in mask_track_annotations
                        ],
                        class_ids={
                            name: index + 1 for index, name in enumerate(classes_list)
                        },
                        source_frames=selected_source_frames,
                        frame_base=mots_frame_base,
                    )
                    zf.writestr(mots_path, mots_text)
                    file_count += 1
                    mots_sequences[seq] = {
                        "task_display_id": t.display_id,
                        "annotations_path": mots_path,
                        **mots_meta,
                    }

                # manifest 视频条目（含网格帧号供 fetch_frames.py 抽帧）。
                dataset_name = _dataset_name_for_task(t, item)
                video_rel = relative_path_from_file_path(t.file_path, dataset_name)
                presigned = storage_service.generate_download_url(
                    t.file_path,
                    expires_in=PRESIGN_EXPIRES_SECONDS,
                    bucket=storage_service.datasets_bucket,
                )
                frame_output_dirs: list[str] = []
                if has_mot or has_kitti:
                    frame_output_dirs.append(f"{seq}/img1")
                if has_yolo_frames:
                    yp = "yolo-frames-det/" if multi else ""
                    frame_output_dirs.append(f"{yp}images/{seq}")
                if has_yolo_frames_seg:
                    yp = "yolo-frames-seg/" if multi else ""
                    frame_output_dirs.append(f"{yp}images/{seq}")
                if has_coco_frames_seg:
                    cp = "coco-frames-seg/" if multi else ""
                    frame_output_dirs.append(f"{cp}images/{seq}")
                frame_outputs = [
                    {
                        "dir": output_dir,
                        "start_number": frame_start_number,
                        "padding": 6,
                        "extension": "jpg",
                    }
                    for output_dir in frame_output_dirs
                ]
                if has_davis:
                    davis_prefix = "davis/" if multi else ""
                    davis_images = f"{davis_prefix}JPEGImages/Full-Resolution/{seq}"
                    frame_output_dirs.append(davis_images)
                    frame_outputs.append(
                        {
                            "dir": davis_images,
                            "start_number": 0,
                            "padding": 5,
                            "extension": "jpg",
                        }
                    )
                if has_youtube_vos:
                    youtube_prefix = "youtube-vos/" if multi else ""
                    youtube_images = f"{youtube_prefix}JPEGImages/{seq}"
                    frame_output_dirs.append(youtube_images)
                    frame_outputs.append(
                        {
                            "dir": youtube_images,
                            "start_number": 0,
                            "padding": 5,
                            "extension": "jpg",
                            "source_frames": youtube_sparse_frames,
                        }
                    )
                if has_mots:
                    mots_prefix = "mots/" if multi else ""
                    mots_images = f"{mots_prefix}images/{seq}"
                    frame_output_dirs.append(mots_images)
                    frame_outputs.append(
                        {
                            "dir": mots_images,
                            "start_number": mots_frame_base,
                            "padding": 6,
                            "extension": "jpg",
                        }
                    )
                manifest_videos.append(
                    {
                        "sequence": seq,
                        "task_display_id": t.display_id,
                        "rel_path": video_rel,
                        "presigned_url": presigned,
                        "expires_at": expires_iso,
                        "video_metadata": vmeta,
                        "sampling": sampling,
                        "step": step,
                        "grid_source_frames": selected_source_frames,
                        # 帧号 base：MOT/YOLO 取 1（1-based），KITTI-only 取 0。
                        "frame_start_number": frame_start_number,
                        "frame_output_dirs": frame_output_dirs,
                        "frame_outputs": frame_outputs,
                    }
                )
                if video_scope is not None:
                    manifest_videos[-1]["export_scope"] = video_scope.as_dict()

        if has_coco_frames_seg:
            cp = "coco-frames-seg/" if multi else ""
            zf.writestr(
                f"{cp}annotations.json",
                json.dumps(
                    build_coco_frames_seg(
                        coco_seq_records,
                        cat_map,
                        frame_start_number=frame_start_number,
                        include_attributes=include_attributes,
                        description=project.name,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            file_count += 1

        if has_davis:
            davis_prefix = "davis/" if multi else ""
            sequence_lines = sorted(set(davis_sequences))
            zf.writestr(
                f"{davis_prefix}ImageSets/2017/val.txt",
                "\n".join(sequence_lines) + ("\n" if sequence_lines else ""),
            )
            zf.writestr(
                f"{davis_prefix}davis_manifest.json",
                json.dumps(
                    {
                        "format_id": "davis",
                        "manifest_version": "1",
                        "sequences": davis_manifest_sequences,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        if has_youtube_vos:
            youtube_prefix = "youtube-vos/" if multi else ""
            zf.writestr(
                f"{youtube_prefix}meta.json",
                json.dumps(
                    {
                        "videos": youtube_videos,
                        "_aap": {
                            "manifest_version": "1",
                            "source_frames": youtube_source_frames,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        if has_mots:
            mots_prefix = "mots/" if multi else ""
            zf.writestr(
                f"{mots_prefix}mots_manifest.json",
                json.dumps(
                    {
                        "format_id": "mots",
                        "manifest_version": "1",
                        "sequences": mots_sequences,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "targets": targets,
                    "videos": manifest_videos,
                    "expires_at": expires_iso,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        zf.writestr("fetch_videos.py", _FETCH_VIDEOS_TEMPLATE)
        if has_frame_sequences:
            zf.writestr("fetch_frames.py", _FETCH_FRAMES_TEMPLATE)

    return tmp_path, file_count or total_tasks, _export_size(tmp_path)
