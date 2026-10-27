"""v0.10.31 · Phase 4 视频导出纯函数底座（计划 §共享底座 / 4.3 / 4.4）。

把「采样网格映射 + MOT/KITTI 文本生成」收成无 DB 纯函数，DB 加载与 zip 组装留给
``export_packaging.build_export_zip`` 的视频分支。

决策对齐：
- D2：geometry.frame_index 永远是源视频帧号；导出 MOT/KITTI 时按采样网格 [0,step,2*step,...]
  重编号（``source_to_grid``）。改采样密度不破坏 geometry。
- 帧集：只取落在采样网格上的帧（``track_grid_rows`` 过滤 ``frame_index % step == 0``）。
- outside：``resolved_track_frames(all_frames)`` 已对 outside 帧返回 None，天然省略。

统一映射约定（计划§统一映射约定）：
- MOT：outside 帧省略；occluded 仍输出（conf=1）；frame 网格序号 **1-based**。
- KITTI：outside 帧省略；occluded 列 ∈{0,1}；frame 网格序号 **0-based**；3D 字段占位 -1。

v0.21.20 · polygon/polyline track（关键帧存 ``points``）导出到 bbox-only 格式
（MOT/KITTI/YOLO-det）时，逐帧降级为顶点外接框（``points_to_bbox_norm``），而非
空框（全 0）。真·segmentation 导出（COCO-seg/YOLO-seg）另行落地。
"""

from __future__ import annotations

import logging

from app.services.mask_formats.image_codecs import compress_coco_rle
from app.services.video_context import derive_sampled_frames
from app.services.video_tracks import resolve_track_at_frame

logger = logging.getLogger("app.services.exporting.video")

# 视频导出的几何白名单（唯一真源，export.py / export_packaging.py 共用）。
#
# 此前导出链路只认 ``video_track_bbox`` / ``video_bbox``，其余几何在打包层与 video_json
# 的类型判别处被**静默丢弃**——连带使 v0.21.20 为 points track 写的外接框降级
# （``points_to_bbox_norm`` / ``_frame_bbox``）在端到端链路上成了死代码，只被纯函数单测
# 覆盖。放宽后 polygon / polyline 的单帧与轨迹几何都能进入导出：bbox-only 格式
# （MOT / KITTI / YOLO-frames-det）降级为顶点外接框，保真格式（video_json / aap_json）
# 保留 ``points``。真·segmentation 格式（保留多边形的 YOLO-seg 等）另行落地。
#
VIDEO_TRACK_GEOMETRY_TYPES = frozenset(
    {
        "video_track_bbox",
        "video_track_polygon",
        "video_track_polyline",
        "video_track_mask",
    }
)
VIDEO_LOSSLESS_SINGLE_FRAME_GEOMETRY_TYPES = frozenset(
    {
        "video_bbox",
        "video_polygon",
        "video_polyline",
        "video_rotated_bbox",
        "video_keypoint",
    }
)
VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES = frozenset(
    {"video_bbox", "video_polygon", "video_polyline"}
)
# 兼容旧 facade；新调用方应按“保真”或“bbox-compatible”显式选择集合。
VIDEO_SINGLE_FRAME_GEOMETRY_TYPES = VIDEO_LOSSLESS_SINGLE_FRAME_GEOMETRY_TYPES

# DatasetItem.width/height 缺失时的回退（与 export.py IMG_W/IMG_H 一致）。
FALLBACK_W, FALLBACK_H = 1920, 1280


def effective_fps(source_fps: float | None, step: int) -> float | None:
    """采样后 fps = 源 fps / step（写进 MOT seqinfo.frameRate）。源 fps 缺失则 None。"""
    if not source_fps or source_fps <= 0:
        return None
    return round(float(source_fps) / max(1, int(step)), 3)


def source_to_grid(
    frame_count: int,
    step: int,
    source_frames: list[int] | None = None,
) -> dict[int, int]:
    """源帧号 → 网格序号(0-based) 映射。网格 = [0, step, 2*step, ...] < frame_count。"""
    frames = (
        derive_sampled_frames(frame_count, step)
        if source_frames is None
        else source_frames
    )
    return {frame: index for index, frame in enumerate(frames)}


def _bbox_px(bbox: dict, img_w: int, img_h: int) -> tuple[float, float, float, float]:
    """归一化 bbox(0..1) → 像素 (left, top, w, h)，保留 2 位小数。"""
    return (
        round(float(bbox.get("x", 0)) * img_w, 2),
        round(float(bbox.get("y", 0)) * img_h, 2),
        round(float(bbox.get("w", 0)) * img_w, 2),
        round(float(bbox.get("h", 0)) * img_h, 2),
    )


def points_to_bbox_norm(points: list) -> dict:
    """归一化 polygon/polyline 顶点 → 归一化外接 bbox ``{x,y,w,h}``。

    v0.21.20 · polygon/polyline track 关键帧存 ``points`` 而非 ``bbox``；导出到
    bbox-only 格式（MOT/KITTI/YOLO-det）时降级为顶点外接框，而非空框（全 0）。
    """
    xs = [float(p[0]) for p in points if len(p) >= 2]
    ys = [float(p[1]) for p in points if len(p) >= 2]
    if not xs or not ys:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _frame_bbox(frame: dict) -> dict:
    """resolved frame → 归一化 bbox：points track 取顶点外接框，bbox track 取 bbox。"""
    points = frame.get("points")
    if points is not None:
        return points_to_bbox_norm(points)
    return frame.get("bbox") or {}


def single_frame_bbox(geometry: dict) -> dict:
    """单帧几何 → 归一化 bbox ``{x,y,w,h}``。

    单帧 bbox 的载荷是**扁平**的（``{type, frame_index, x, y, w, h}``），可直接当 bbox 用；
    而单帧 polygon / polyline 是 ``{type, frame_index, points, ...}``，**没有 x/y/w/h**——
    若直接喂给 ``_yolo_det_line``，``.get("x", 0)`` 会把它导成全 0 空框（比丢弃更坏：下游拿到
    看似合法的空标注）。故此处与 track 侧 ``_frame_bbox`` 对称，降级为顶点外接框。
    """
    points = geometry.get("points")
    if points is not None:
        return points_to_bbox_norm(points)
    return geometry


def track_grid_rows(
    geometry: dict,
    *,
    frame_count: int,
    step: int,
    img_w: int,
    img_h: int,
    source_frames: list[int] | None = None,
    checkpoint=None,
) -> list[dict]:
    """单 track 在采样网格上的逐帧行。

    返回 ``[{grid_index, left, top, w, h, occluded, source}]``（grid_index 0-based）。
    走 ``resolved_track_frames(all_frames)`` 展开插值 + 跳 outside，再筛网格帧重编号。
    """
    step = max(1, int(step))
    grid = source_to_grid(frame_count, step, source_frames)
    rows: list[dict] = []
    for frame_index, grid_index in grid.items():
        if checkpoint is not None:
            checkpoint()
        frame = resolve_track_at_frame(geometry, frame_index)
        if frame is None:
            continue
        bbox = _frame_bbox(frame)
        if float(bbox.get("w", 0)) <= 0 or float(bbox.get("h", 0)) <= 0:
            continue
        left, top, w, h = _bbox_px(bbox, img_w, img_h)
        rows.append(
            {
                "grid_index": grid_index,
                "left": left,
                "top": top,
                "w": w,
                "h": h,
                "occluded": bool(frame.get("occluded", False)),
                "source": frame.get("source", "manual"),
            }
        )
    return rows


# ── YOLO frame detection dataset ──────────────────────────────────────


def yolo_seg_line(class_id: int, ring: list) -> str:
    """YOLO-seg label 行：``cls x1 y1 x2 y2 …``（归一化顶点展平，6 位小数）。

    **图片与视频两条 YOLO-seg 导出链共用**（对应 ``export_packaging`` 的 ``yolo-seg``
    与本模块的 ``build_yolo_frame_seg_labels``），避免各写一份逐渐漂移。放在本模块是因为
    ``export_packaging`` 已依赖本模块，反向 import 会成环。
    """
    flat = " ".join(f"{coord:.6f}" for pt in ring for coord in pt[:2])
    return f"{class_id} {flat}"


def _yolo_det_line(class_id: int, bbox: dict) -> str:
    cx = float(bbox.get("x", 0)) + float(bbox.get("w", 0)) / 2
    cy = float(bbox.get("y", 0)) + float(bbox.get("h", 0)) / 2
    bw = float(bbox.get("w", 0))
    bh = float(bbox.get("h", 0))
    return f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


def build_yolo_frame_det_labels(
    tracks: list[tuple[str | None, dict, dict]],
    bboxes: list[tuple[str | None, dict, dict]],
    cat_map: dict[str, int],
    *,
    frame_count: int,
    step: int,
    frame_start_number: int,
    include_attributes: bool,
    source_frames: list[int] | None = None,
    checkpoint=None,
) -> dict[int, tuple[list[str], list[dict]]]:
    """YOLO det labels for sampled video frames.

    Keys are output frame numbers, matching ``fetch_frames.py`` filenames. Values are
    ``(label_lines, attrs_per_line)``. All sampled frames are present, including
    empty labels, so YOLO has a txt file for every extracted image.
    """
    grid = source_to_grid(frame_count, step, source_frames)
    labels: dict[int, tuple[list[str], list[dict]]] = {
        grid_index + frame_start_number: ([], []) for grid_index in grid.values()
    }

    for class_name, geometry, attributes in bboxes:
        if checkpoint is not None:
            checkpoint()
        source_frame = int(geometry.get("frame_index", 0))
        grid_index = grid.get(source_frame)
        if grid_index is None:
            continue
        out_frame = grid_index + frame_start_number
        lines, attrs = labels[out_frame]
        lines.append(
            _yolo_det_line(
                cat_map.get(class_name or "", 0), single_frame_bbox(geometry)
            )
        )
        if include_attributes:
            attrs.append(attributes or {})

    for class_name, geometry, attributes in tracks:
        if checkpoint is not None:
            checkpoint()
        class_id = cat_map.get(class_name or "", 0)
        for source_frame, grid_index in grid.items():
            if checkpoint is not None:
                checkpoint()
            frame = resolve_track_at_frame(geometry, source_frame)
            if frame is None:
                continue
            out_frame = grid_index + frame_start_number
            lines, attrs = labels[out_frame]
            bbox = _frame_bbox(frame)
            if float(bbox.get("w", 0)) <= 0 or float(bbox.get("h", 0)) <= 0:
                continue
            lines.append(_yolo_det_line(class_id, bbox))
            if include_attributes:
                attrs.append(attributes or {})

    return labels


# ── YOLO frame segmentation dataset ──────────────────────────────────


def build_yolo_frame_seg_labels(
    tracks: list[tuple[str | None, dict, dict]],
    bboxes: list[tuple[str | None, dict, dict]],
    cat_map: dict[str, int],
    *,
    frame_count: int,
    step: int,
    frame_start_number: int,
    include_attributes: bool,
    source_frames: list[int] | None = None,
    checkpoint=None,
) -> dict[int, tuple[list[str], list[dict]]]:
    """YOLO-seg labels：逐帧多边形（**保留顶点，不降级为外接框**）。

    与 ``build_yolo_frame_det_labels`` 同构（同一采样网格 / 帧号基数 / 文件名约定，
    所有采样帧都建 txt，含空文件），但只导**多边形**几何：单帧 ``video_polygon``
    与 ``video_track_polygon``（后者按弧长插值展开到每帧）。

    矩形框与折线不产出 seg 行 —— 对齐图片侧 ``yolo-seg``（``_seg_rings_norm`` 亦只认
    polygon / multi_polygon）：折线不是闭合区域，矩形框请用 ``yolo-frames-det``。
    """
    grid = source_to_grid(frame_count, step, source_frames)
    labels: dict[int, tuple[list[str], list[dict]]] = {
        grid_index + frame_start_number: ([], []) for grid_index in grid.values()
    }

    for class_name, geometry, attributes in bboxes:
        if checkpoint is not None:
            checkpoint()
        if geometry.get("type") != "video_polygon":
            continue
        points = geometry.get("points") or []
        if len(points) < 3:
            continue
        grid_index = grid.get(int(geometry.get("frame_index", 0)))
        if grid_index is None:
            continue
        lines, attrs = labels[grid_index + frame_start_number]
        lines.append(yolo_seg_line(cat_map.get(class_name or "", 0), points))
        if include_attributes:
            attrs.append(attributes or {})

    for class_name, geometry, attributes in tracks:
        if checkpoint is not None:
            checkpoint()
        if geometry.get("type") != "video_track_polygon":
            continue
        class_id = cat_map.get(class_name or "", 0)
        for source_frame, grid_index in grid.items():
            if checkpoint is not None:
                checkpoint()
            frame = resolve_track_at_frame(geometry, source_frame)
            if frame is None:
                continue
            points = frame.get("points") or []
            if len(points) < 3:
                continue
            lines, attrs = labels[grid_index + frame_start_number]
            lines.append(yolo_seg_line(class_id, points))
            if include_attributes:
                attrs.append(attributes or {})

    return labels


# ── COCO frame segmentation dataset ──────────────────────────────────


def _coco_ring_px(points: list, w: int, h: int) -> list[float]:
    """归一化多边形顶点 → COCO segmentation 像素坐标 flat ring（``[x1,y1,x2,y2,…]``，2 位小数）。

    内联于本模块（不 import ``export.py`` 的 ``_flatten_ring``）：``export_packaging`` 已依赖本
    模块，反向 import 会成环。
    """
    out: list[float] = []
    for pt in points:
        if len(pt) >= 2:
            out.append(round(float(pt[0]) * w, 2))
            out.append(round(float(pt[1]) * h, 2))
    return out


def _coco_seg_annotation(
    ann_id: int,
    image_id: int,
    category_id: int,
    points: list,
    img_w: int,
    img_h: int,
    attributes: dict,
    track_id,
    include_attributes: bool,
) -> dict:
    """单个多边形 → COCO annotation 行。

    ``bbox`` = 顶点外接框（像素），``area`` = 外接框面积（对齐图片 ``export_coco``，**不**引入
    shoelace 第二套 area 语义），``segmentation`` = 单外环像素坐标，``iscrowd=0``。
    """
    bbox_norm = points_to_bbox_norm(points)
    x_px = round(bbox_norm["x"] * img_w, 2)
    y_px = round(bbox_norm["y"] * img_h, 2)
    w_px = round(bbox_norm["w"] * img_w, 2)
    h_px = round(bbox_norm["h"] * img_h, 2)
    row: dict = {
        "id": ann_id,
        "image_id": image_id,
        "category_id": category_id,
        "bbox": [x_px, y_px, w_px, h_px],
        "area": round(w_px * h_px, 2),
        "iscrowd": 0,
        "segmentation": [_coco_ring_px(points, img_w, img_h)],
    }
    if include_attributes:
        attrs = dict(attributes or {})
        if track_id is not None:
            attrs["__track_id"] = str(track_id)
        row["attributes"] = attrs
    return row


def build_coco_frames_seg(
    sequences: list[dict],
    cat_map: dict[str, int],
    *,
    frame_start_number: int,
    include_attributes: bool,
    description: str = "",
    checkpoint=None,
) -> dict:
    """视频逐帧 COCO instance segmentation 单文档（纯函数，无时间戳，确定性）。

    ``sequences`` 为有序列表，每项 ``{seq, tracks, bboxes, frame_count, step, img_w, img_h}``；
    ``tracks`` / ``bboxes`` 元素为 ``(class_name, geometry, attributes, track_id)``。image id /
    annotation id 按 sequence 顺序 × 网格帧升序稳定自增（不依赖 ``hash`` / 未排序 DB 结果），
    相同输入两次调用输出字节相等。

    每个采样帧建一条 ``images[]``（含无标注空帧），保证 negative frame 与 ``fetch_frames.py``
    抽帧结果一一对应。只导多边形（单帧 ``video_polygon`` / 轨迹 ``video_track_polygon``，后者按
    弧长插值展开到每帧）；bbox / polyline / ``points < 3`` 跳过，与图片侧 ``_coco_segmentation``
    一致（折线不是闭合区域，矩形请用 ``yolo-frames-det``）。

    class_name 为空/None，或不在 ``cat_map``（已删除的类）时，该 annotation 整条跳过——不再
    静默落到 ``category_id=0``（旧类/新类撞车，且 ``classes_list`` 为空时 0 会指向不存在的
    category，pycocotools ``createIndex`` 直接 KeyError）。跳过计数与类名集合累计进返回的
    ``info.skipped_unknown_class_annotations`` / ``info.skipped_unknown_class_names``，并各记一条
    warning 日志。
    """
    images: list[dict] = []
    annotations: list[dict] = []
    image_id_by_key: dict[tuple[str, int], int] = {}
    next_image_id = 0
    next_ann_id = 0
    skipped_unknown_class = 0
    skipped_class_names: set[str] = set()

    for record in sequences:
        if checkpoint is not None:
            checkpoint()
        seq = record["seq"]
        frame_count = int(record["frame_count"])
        step = max(1, int(record["step"]))
        img_w = int(record["img_w"])
        img_h = int(record["img_h"])
        record_source_frames = record.get("source_frames")
        sampled = (
            derive_sampled_frames(frame_count, step)
            if record_source_frames is None
            else record_source_frames
        )
        grid = {source: idx for idx, source in enumerate(sampled)}

        for grid_index, source_frame in enumerate(sampled):
            if checkpoint is not None:
                checkpoint()
            frame_no = grid_index + frame_start_number
            image_id_by_key[(seq, frame_no)] = next_image_id
            images.append(
                {
                    "id": next_image_id,
                    "file_name": f"images/{seq}/{frame_no:06d}.jpg",
                    "width": img_w,
                    "height": img_h,
                    "source_frame_index": source_frame,
                }
            )
            next_image_id += 1

        for class_name, geometry, attributes, track_id in record["bboxes"]:
            if checkpoint is not None:
                checkpoint()
            if geometry.get("type") != "video_polygon":
                continue
            points = geometry.get("points") or []
            if len(points) < 3:
                continue
            grid_index = grid.get(int(geometry.get("frame_index", 0)))
            if grid_index is None:
                continue
            category_id = cat_map.get(class_name or "")
            if category_id is None:
                skipped_unknown_class += 1
                skipped_class_names.add(class_name or "(empty)")
                continue
            image_id = image_id_by_key[(seq, grid_index + frame_start_number)]
            annotations.append(
                _coco_seg_annotation(
                    next_ann_id,
                    image_id,
                    category_id,
                    points,
                    img_w,
                    img_h,
                    attributes,
                    track_id,
                    include_attributes,
                )
            )
            next_ann_id += 1

        for class_name, geometry, attributes, track_id in record["tracks"]:
            if checkpoint is not None:
                checkpoint()
            if geometry.get("type") not in {"video_track_polygon", "video_track_mask"}:
                continue
            category_id = cat_map.get(class_name or "")
            if category_id is None:
                skipped_unknown_class += 1
                skipped_class_names.add(class_name or "(empty)")
                continue
            for source_frame, grid_index in grid.items():
                if checkpoint is not None:
                    checkpoint()
                frame = resolve_track_at_frame(geometry, source_frame)
                if frame is None:
                    continue
                image_id = image_id_by_key[(seq, grid_index + frame_start_number)]
                if geometry.get("type") == "video_track_mask":
                    rle = frame.get("mask_rle") or {}
                    counts = rle.get("counts") or []
                    bbox_norm = frame.get("bbox") or {}
                    if not counts or float(bbox_norm.get("w", 0)) <= 0:
                        continue
                    bbox = [
                        round(float(bbox_norm["x"]) * img_w, 2),
                        round(float(bbox_norm["y"]) * img_h, 2),
                        round(float(bbox_norm["w"]) * img_w, 2),
                        round(float(bbox_norm["h"]) * img_h, 2),
                    ]
                    row = {
                        "id": next_ann_id,
                        "image_id": image_id,
                        "category_id": category_id,
                        "bbox": bbox,
                        "area": int(sum(int(value) for value in counts[1::2])),
                        "iscrowd": 1,
                        "segmentation": compress_coco_rle(rle),
                    }
                    if include_attributes:
                        attrs = dict(attributes or {})
                        if track_id is not None:
                            attrs["__track_id"] = str(track_id)
                        attrs["__occluded"] = bool(frame.get("occluded"))
                        row["attributes"] = attrs
                    annotations.append(row)
                    next_ann_id += 1
                    continue
                points = frame.get("points") or []
                if len(points) < 3:
                    continue
                annotations.append(
                    _coco_seg_annotation(
                        next_ann_id,
                        image_id,
                        category_id,
                        points,
                        img_w,
                        img_h,
                        attributes,
                        track_id,
                        include_attributes,
                    )
                )
                next_ann_id += 1

    categories = [
        {"id": cid, "name": name}
        for name, cid in sorted(cat_map.items(), key=lambda kv: kv[1])
    ]
    if skipped_unknown_class:
        logger.warning(
            "build_coco_frames_seg: skipped %d annotation(s) with unknown/missing "
            "class (not in cat_map): %s",
            skipped_unknown_class,
            sorted(skipped_class_names),
        )
    return {
        "info": {
            "description": description,
            "skipped_unknown_class_annotations": skipped_unknown_class,
            "skipped_unknown_class_names": sorted(skipped_class_names),
        },
        "images": images,
        "annotations": annotations,
        "categories": categories,
    }


# ── MOT 16/17/20 ─────────────────────────────────────────────────────


def build_mot_gt(
    tracks: list[tuple[int, str | None, dict]],
    *,
    frame_count: int,
    step: int,
    img_w: int,
    img_h: int,
    source_frames: list[int] | None = None,
    checkpoint=None,
) -> str:
    """gt.txt：``frame,id,bb_left,bb_top,bb_w,bb_h,conf,x,y,z``（conf=1，x/y/z=-1）。

    ``tracks`` = ``[(track_number, class_name, geometry)]``。frame=网格序号 1-based。
    按 (frame, id) 排序，便于 trackeval 读取。
    """
    lines: list[tuple[int, int, str]] = []
    for track_number, _class_name, geometry in tracks:
        if checkpoint is not None:
            checkpoint()
        for row in track_grid_rows(
            geometry,
            frame_count=frame_count,
            step=step,
            img_w=img_w,
            img_h=img_h,
            source_frames=source_frames,
            checkpoint=checkpoint,
        ):
            frame = row["grid_index"] + 1  # MOT 1-based
            lines.append(
                (
                    frame,
                    track_number,
                    f"{frame},{track_number},{row['left']},{row['top']},"
                    f"{row['w']},{row['h']},1,-1,-1,-1",
                )
            )
    lines.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(line for _f, _i, line in lines)


def build_mot_seqinfo(
    name: str,
    *,
    source_fps: float | None,
    step: int,
    frame_count: int,
    img_w: int,
    img_h: int,
    source_frames: list[int] | None = None,
) -> str:
    """seqinfo.ini。frameRate=采样后 fps；seqLength=网格帧数。"""
    grid_len = len(
        derive_sampled_frames(frame_count, step)
        if source_frames is None
        else source_frames
    )
    fps = effective_fps(source_fps, step)
    frame_rate = int(round(fps)) if fps else 0
    return (
        "[Sequence]\n"
        f"name={name}\n"
        "imDir=img1\n"
        f"frameRate={frame_rate}\n"
        f"seqLength={grid_len}\n"
        f"imWidth={img_w}\n"
        f"imHeight={img_h}\n"
        "imExt=.jpg\n"
    )


# ── KITTI Tracking 2D ─────────────────────────────────────────────────


def build_kitti_labels(
    tracks: list[tuple[int, str | None, dict]],
    *,
    frame_count: int,
    step: int,
    img_w: int,
    img_h: int,
    source_frames: list[int] | None = None,
    checkpoint=None,
) -> str:
    """KITTI tracking label：每行 17 列。

    ``frame track_id type truncated occluded alpha x1 y1 x2 y2 h w l x y z rotation_y``
    2D 版：truncated=0，occluded∈{0,1}，alpha/3D 字段占位 -1；frame=网格序号 0-based。
    按 (frame, track_id) 排序。
    """
    lines: list[tuple[int, int, str]] = []
    for track_number, class_name, geometry in tracks:
        if checkpoint is not None:
            checkpoint()
        kitti_type = class_name or "DontCare"
        for row in track_grid_rows(
            geometry,
            frame_count=frame_count,
            step=step,
            img_w=img_w,
            img_h=img_h,
            source_frames=source_frames,
            checkpoint=checkpoint,
        ):
            frame = row["grid_index"]  # KITTI 0-based
            x1 = row["left"]
            y1 = row["top"]
            x2 = round(x1 + row["w"], 2)
            y2 = round(y1 + row["h"], 2)
            occluded = 1 if row["occluded"] else 0
            lines.append(
                (
                    frame,
                    track_number,
                    f"{frame} {track_number} {kitti_type} 0 {occluded} -1 "
                    f"{x1} {y1} {x2} {y2} -1 -1 -1 -1 -1 -1 -1",
                )
            )
    lines.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(line for _f, _i, line in lines)
