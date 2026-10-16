"""v0.18.1 · 多阶段预标注 ROI 裁剪 (路径 B M1).

平台按上游检测框 bbox 裁 crop, 以 data URI (base64) 内联喂下游分类 backend——backend
无感, 只收到一张小图跑它本就支持的分类, 把结果塞 attributes 返回 (协议 §3 已有能力)。
这样任意现成 classify backend 零改造即可作下游 (ROADMAP §3.4 决议)。

本期 (M1) 只处理 rectanglelabels (轴对齐 bbox); 旋转框 / 多边形等几何留 M2。
crop 经 data URI 内存传递, 不落临时对象 (依赖下游 load_image_bgr 支持 ``data:`` 前缀)。
"""

from __future__ import annotations

import base64
import io
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image


@dataclass
class CropBatch:
    """v0.18.4 · crop_inputs_from_boxes 的返回: 喂下游的 inputs + 几何跳过统计。

    skipped_geometry: 命中本阶段路由 (类别匹配) 但因几何不支持 (非 bbox / 旋转框 / 退化框)
    无法裁 crop 而被跳过的父框数。供逐阶段统计暴露「N 框因几何不支持未富集」, 不再静默。

    transforms: v0.18.15 · ``{box_idx_str: {ox, oy, sx, sy}}`` —— 每个裁出的 crop 在原图的
        仿射变换 (归一化图像空间: crop 左上角 + 宽高占图比例), 供 :func:`remap_geometry_to_image`
        把下游在 crop 上检出的几何反投影回原图坐标。不写进 ``inputs`` (线格式零变化, 不泄漏给 backend)。
    """

    inputs: list[dict] = field(default_factory=list)
    skipped_geometry: int = 0
    transforms: dict[str, dict] = field(default_factory=dict)


def box_class_name(box: dict) -> str | None:
    """从 LS 标准 shape 提取 class_name (rectanglelabels / polygonlabels / labels[0])。

    v0.18.2 · parent_class_filter 类别路由用。v0.18.15 · 兼容 polygon 父框。取不到返回 None。
    """
    value = box.get("value") or {}
    labels = (
        value.get("rectanglelabels")
        or value.get("polygonlabels")
        or value.get("labels")
    )
    if isinstance(labels, list) and labels:
        return labels[0]
    return None


def _polygon_bbox_pct(value: dict) -> tuple[float, float, float, float] | None:
    """polygon value.points ([[x,y],...] 百分比 0-100) → 外接框 (x, y, w, h) 百分比。

    v0.18.15 · polygon 父框取外接框作 ROI。点不足 / 非法返回 None。
    """
    pts = value.get("points")
    if not isinstance(pts, list) or len(pts) < 3:
        return None
    try:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
    except (TypeError, ValueError, IndexError):
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return x0, y0, x1 - x0, y1 - y0


def _box_bbox_pct(box: dict) -> tuple[float, float, float, float] | None:
    """父框 → 外接框 (x, y, w, h) 百分比 (0-100)。

    v0.18.15 · 统一 rectanglelabels (轴对齐 bbox, 旋转框跳过) 与 polygonlabels (取外接框)。
    其余几何 / 非法返回 None (调用方计入 skipped_geometry)。
    """
    btype = box.get("type")
    value = box.get("value") or {}
    if btype == "rectanglelabels":
        if value.get("rotation"):
            return None
        raw = (value.get("x"), value.get("y"), value.get("width"), value.get("height"))
        if any(v is None for v in raw):
            return None
        try:
            x, y, w, h = (float(v) for v in raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return x, y, w, h
    if btype == "polygonlabels":
        return _polygon_bbox_pct(value)
    return None


def _box_bbox_pixels(
    box: dict, img_w: int, img_h: int
) -> tuple[float, float, float, float] | None:
    """父框 → 像素外接框 (left, top, right, bottom); 委托 :func:`_box_bbox_pct`。"""
    pct = _box_bbox_pct(box)
    if pct is None:
        return None
    x, y, w, h = pct
    return (
        x / 100.0 * img_w,
        y / 100.0 * img_h,
        (x + w) / 100.0 * img_w,
        (y + h) / 100.0 * img_h,
    )


def crop_inputs_from_boxes(
    image: Image.Image,
    boxes: list[dict],
    *,
    pad: float = 0.05,
    jpeg_quality: int = 90,
    parent_class_filter: list[str] | None = None,
    delivery: str = "data_uri",
    upload_fn: Callable[[int, bytes], str] | None = None,
    cache: dict | None = None,
    min_crop_side_px: int = 0,
) -> CropBatch:
    """对 boxes 里的 bbox 框逐个裁 ROI crop, 返回喂下游 /predict 的 inputs + 几何跳过统计。

    Args:
        image: 任务原图 (PIL Image)。
        boxes: 上游 stage 返回的 LS 标准 result 列表 (每项 ``{type, value, ...}``)。
        pad: 按框宽高比例外扩的边界 padding (0.05 = 各边外扩 5%)。
        jpeg_quality: crop 重编码 JPEG 质量。
        parent_class_filter: v0.18.2 · 只对 class_name 在此集合的父框裁 crop (空/None=全部)。
            类别路由: 不同下游阶段设不相交类别集 = 不同类走不同模型。
        delivery: v0.18.4 · crop 投递方式。``"data_uri"`` (默认, 纯函数自足, 单测/已知支持
            ``data:`` 的后端) 内联 base64; ``"presigned"`` (worker 生产默认) 经 ``upload_fn``
            上传对象存储回 URL——对所有走 ``httpx.get`` 的下游后端 (gsam2/sam3) 通用。
        upload_fn: v0.18.4 · ``delivery="presigned"`` 时必传, ``(box_idx, jpeg_bytes) -> url``。
        cache: v0.18.4 · 可选 ``{cache_key: input}``。并行兄弟阶段 target 同一批父框时复用
            已裁/已上传 crop, 不重复裁剪 + 重编码 + 重上传。**调用方需为每个父阶段传独立 cache**
            (或在 cache key 里包 parent_stage), 否则 depth-3 阶段的子下标 (中间几何) 会与
            root_boxes 的 (idx, pad) 撞键, 喂错图 (claude[bot] P1)。

    Returns:
        :class:`CropBatch` —— ``inputs`` 每项 ``{"id": "<box_idx>", "file_path": <data uri | url>}``,
        ``id`` 即 boxes 中的下标 (字符串, **保留原下标**即使被类别过滤), 供下游结果回写到对应父框;
        ``skipped_geometry`` 为命中路由但几何不支持被跳过的父框数。类别不匹配的父框是路由跳过, 不计入。
    """
    img_w, img_h = image.size
    filter_set = set(parent_class_filter) if parent_class_filter else None
    inputs: list[dict] = []
    skipped_geometry = 0
    transforms: dict[str, dict] = {}
    for idx, box in enumerate(boxes):
        if not isinstance(box, dict):
            continue
        # 类别路由: 不匹配目标类的父框是路由跳过 (非几何), 不计入 skipped_geometry。
        if filter_set is not None and box_class_name(box) not in filter_set:
            continue
        cache_key = (idx, round(pad, 4))
        if cache is not None and cache_key in cache:
            cached = cache[cache_key]
            inputs.append(cached["input"])
            transforms[str(idx)] = cached["transform"]
            continue
        # 几何门控: 非 bbox/polygon / 旋转框 / 退化框 → 无法裁 crop, 计入 skipped_geometry。
        px = _box_bbox_pixels(box, img_w, img_h)
        if px is None:
            skipped_geometry += 1
            continue
        left, top, right, bottom = px
        bw = right - left
        bh = bottom - top
        if bw <= 0 or bh <= 0:
            skipped_geometry += 1
            continue
        left = max(0.0, left - bw * pad)
        top = max(0.0, top - bh * pad)
        right = min(float(img_w), right + bw * pad)
        bottom = min(float(img_h), bottom + bh * pad)
        left_int, top_int = int(left), int(top)
        right_int, bottom_int = int(round(right)), int(round(bottom))
        crop = image.crop((left_int, top_int, right_int, bottom_int))
        if crop.width <= 0 or crop.height <= 0:
            skipped_geometry += 1
            continue
        # v0.18.14 · 嵌套裁剪越深裁得越小, 短边过小的 crop 上采样后画质崩坏; 低于阈值跳过
        # (计 skipped_geometry), 父框靠 on_failure=keep_parent 保留。0=不守卫 (向后兼容)。
        if min_crop_side_px and (
            crop.width < min_crop_side_px or crop.height < min_crop_side_px
        ):
            skipped_geometry += 1
            continue
        buf = io.BytesIO()
        crop.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality)
        jpeg_bytes = buf.getvalue()
        if delivery == "presigned":
            if upload_fn is None:
                raise ValueError("delivery='presigned' 需要 upload_fn")
            file_path = upload_fn(idx, jpeg_bytes)
        else:
            file_path = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode(
                "ascii"
            )
        inp = {"id": str(idx), "file_path": file_path}
        # v0.18.15 · crop→原图仿射变换 (归一化图像空间), 供 remap_geometry_to_image 反投影。
        transform = {
            "ox": left_int / img_w,
            "oy": top_int / img_h,
            "sx": (right_int - left_int) / img_w,
            "sy": (bottom_int - top_int) / img_h,
        }
        transforms[str(idx)] = transform
        if cache is not None:
            cache[cache_key] = {"input": inp, "transform": transform}
        inputs.append(inp)
    return CropBatch(
        inputs=inputs, skipped_geometry=skipped_geometry, transforms=transforms
    )


@dataclass
class GeometryPromptBatch:
    """v0.18.12 · geometry-prompt 投递 (下游 box-seg stage) 的返回。

    prompts: 喂下游 ``tasks[].prompts[]`` 的框列表, 每项 ``{box:[x1,y1,x2,y2], parent_box_idx}``,
        box 为原图归一化坐标 [0,1]。skipped_geometry: 命中类别路由但几何不支持被跳过的父框数。
    """

    prompts: list[dict] = field(default_factory=list)
    skipped_geometry: int = 0


def geometry_prompts_from_boxes(
    boxes: list[dict],
    *,
    parent_class_filter: list[str] | None = None,
) -> GeometryPromptBatch:
    """把上游父框转成 geometry-prompt 列表 (全图 + 归一化框), 喂下游 box-seg 原子。

    与 :func:`crop_inputs_from_boxes` 的区别: **不裁图、不上传**——下游收全图 URL + 框列表,
    backend 对一张图只 set_image 一次、N 框共享 embedding (协议 §2.1.1)。坐标纯由 LS
    百分比换算, 无需加载原图。几何门控与 crop 路径一致 (仅轴对齐 bbox; 旋转/退化框跳过)。

    Args:
        boxes: 上游 stage 的 LS 标准 result 列表。
        parent_class_filter: 只对 class_name 在此集合的父框生成 prompt (空/None=全部)。

    Returns:
        :class:`GeometryPromptBatch` —— ``prompts`` 每项 ``{"box": [x1,y1,x2,y2], "parent_box_idx": idx}``,
        ``parent_box_idx`` 即 boxes 下标 (保留原下标), 供下游结果回写到对应父框。
    """
    filter_set = set(parent_class_filter) if parent_class_filter else None
    prompts: list[dict] = []
    skipped_geometry = 0
    for idx, box in enumerate(boxes):
        if not isinstance(box, dict):
            continue
        if filter_set is not None and box_class_name(box) not in filter_set:
            continue
        # v0.18.15 · 统一支持 rectanglelabels (旋转框跳过) 与 polygonlabels (取外接框)。
        pct = _box_bbox_pct(box)
        if pct is None:
            skipped_geometry += 1
            continue
        x, y, w, h = pct
        if w <= 0 or h <= 0:
            skipped_geometry += 1
            continue
        # LS 百分比 0-100 → 归一化 [0,1]; 钳制到边界 (下游按图宽高还原像素)。
        x1 = min(max(x / 100.0, 0.0), 1.0)
        y1 = min(max(y / 100.0, 0.0), 1.0)
        x2 = min(max((x + w) / 100.0, 0.0), 1.0)
        y2 = min(max((y + h) / 100.0, 0.0), 1.0)
        prompts.append({"box": [x1, y1, x2, y2], "parent_box_idx": idx})
    return GeometryPromptBatch(prompts=prompts, skipped_geometry=skipped_geometry)


def collect_geometry_shapes(seg_results: list, boxes: list[dict]) -> list[dict]:
    """把下游 box-seg 返回的 polygon shapes 收集成可追加到预测的 LS result 列表。

    下游单图返回一条 PredictionResult, 其 ``result`` 列表里每个 polygon 带 ``parent_box_idx``
    (原图坐标, 无需回映)。只保留 parent_box_idx 落在父框范围内的有效 shape。

    Args:
        seg_results: 下游 ``client.predict`` 返回的 PredictionResult 列表。
        boxes: 父框列表 (仅用于校验 parent_box_idx 合法范围)。

    Returns:
        待追加到 ``pred_result.result`` 的新 shape 列表 (保留 parent_box_idx 供溯源)。
    """
    out: list[dict] = []
    for cr in seg_results:
        for s in cr.result or []:
            if not isinstance(s, dict):
                continue
            pidx = s.get("parent_box_idx")
            if pidx is not None and not (0 <= int(pidx) < len(boxes)):
                continue
            out.append(s)
    return out


def compose_transforms(outer: dict, inner: dict) -> dict:
    """v0.18.15 · 链式 crop 变换合成 (depth-3): inner 相对 outer crop, 返回 inner 相对原图的变换。

    归一化图像空间下: ``inner`` 的偏移先按 ``outer`` 缩放再叠加 ``outer`` 偏移, 缩放相乘。
    """
    return {
        "ox": outer["ox"] + inner["ox"] * outer["sx"],
        "oy": outer["oy"] + inner["oy"] * outer["sy"],
        "sx": outer["sx"] * inner["sx"],
        "sy": outer["sy"] * inner["sy"],
    }


def _crop_coord_scale(values: list[float]) -> float:
    """crop 上检出几何的坐标口径: 返回把它归一化到 [0,1] 的除数。

    协议允许 backend 返回 [0,100] (yolo / onnxtools) 或 [0,1] (grounded-sam2 / sam3);
    判据与 ``services.prediction._percent_scale`` 同款 (任一分量 >1 即视作百分比)。
    早期只有百分比口径的 backend 走 ROI 下游, 此处曾硬当百分比, 归一化口径的 backend
    检出框被缩小 100 倍并塌到 crop 左上角。
    """
    return 100.0 if any(abs(v) > 1.0 for v in values) else 1.0


def _polygon_rings(value: dict) -> list[list]:
    """取 polygonlabels 的全部环 (外环 + 洞), 供口径判定; 空 / 形态不认识时返回 []。"""
    polys = value.get("polygons")
    if polys:
        rings: list[list] = []
        for poly in polys:
            pts = poly.get("points")
            # 外环空 = 退化多边形 (只有洞无外环): 整体跳过, 否则会产出 points:[] 的鬼影
            # shape, 下游 ingest 422 或渲染出「只有洞」的异常几何。
            if not (isinstance(pts, list) and pts):
                continue
            rings.append(pts)
            rings.extend(
                h for h in (poly.get("holes") or []) if isinstance(h, list) and h
            )
        return rings
    pts = value.get("points")
    if not isinstance(pts, list) or not pts:
        return []
    return [pts] + [h for h in (value.get("holes") or []) if isinstance(h, list) and h]


def remap_geometry_to_image(shapes: list[dict], transform: dict) -> list[dict]:
    """把下游在 crop 上检出的几何反投影回原图百分比坐标。

    v0.18.15 · ``transform={ox,oy,sx,sy}`` 为 crop 在原图的归一化偏移 + 缩放
    (见 :class:`CropBatch`)。rectanglelabels (x/y/width/height) 与 polygonlabels (points)
    都按 ``img_pct = (offset + crop_norm * scale) * 100`` 反投影, 其中 ``crop_norm`` 由
    :func:`_crop_coord_scale` 按 backend 实际口径归一。不改入参, 返回新列表;
    非 bbox/polygon 或缺坐标的 shape 丢弃。
    """
    ox, oy = transform["ox"], transform["oy"]
    sx, sy = transform["sx"], transform["sy"]
    # 口径 (0-1 vs 0-100) 按整批 shape 一次判定: 同一 crop 的所有检出来自同一 backend、
    # 口径一致。逐 shape 判会让"百分比口径下各分量恰好 <1 的近原点小目标"被误判成归一化
    # (与 prediction.py:227 video_track_bbox 拒绝自动探测同因); 整批含任一 >1 分量即判
    # 百分比, 显著降低单个小目标被带偏的概率。
    batch_values: list[float] = []
    for s in shapes:
        if not isinstance(s, dict):
            continue
        value = s.get("value") or {}
        btype = s.get("type")
        if btype == "rectanglelabels":
            batch_values.extend(
                float(value[k])
                for k in ("x", "y", "width", "height")
                if value.get(k) is not None
            )
        elif btype == "polygonlabels":
            batch_values.extend(
                float(c) for ring in _polygon_rings(value) for p in ring for c in p
            )
    batch_scale = _crop_coord_scale(batch_values)
    out: list[dict] = []
    for s in shapes:
        if not isinstance(s, dict):
            continue
        value = dict(s.get("value") or {})
        btype = s.get("type")
        if btype == "rectanglelabels":
            if any(value.get(k) is None for k in ("x", "y", "width", "height")):
                continue
            xywh = [float(value[k]) for k in ("x", "y", "width", "height")]
            scale = batch_scale
            x, y, w, h = (v / scale for v in xywh)
            value["x"] = (ox + x * sx) * 100.0
            value["y"] = (oy + y * sy) * 100.0
            value["width"] = w * sx * 100.0
            value["height"] = h * sy * 100.0
        elif btype == "polygonlabels":
            # LS polygon 三形态 (见 services.prediction.to_internal_shape):
            #   ① {points}  ② {points, holes}  ③ {polygons: [{points, holes?}]} (多连通)
            # 环坐标口径按整个 shape 一次判定 (逐环判会让小环误判成归一化)。
            rings = _polygon_rings(value)
            if not rings:
                continue
            scale = batch_scale

            def _remap_ring(ring: list) -> list[list[float]]:
                return [
                    [
                        (ox + float(p[0]) / scale * sx) * 100.0,
                        (oy + float(p[1]) / scale * sy) * 100.0,
                    ]
                    for p in ring
                ]

            if value.get("polygons"):
                value["polygons"] = [
                    {
                        **poly,
                        "points": _remap_ring(poly["points"]),
                        **(
                            {"holes": [_remap_ring(h) for h in poly["holes"]]}
                            if poly.get("holes")
                            else {}
                        ),
                    }
                    for poly in value["polygons"]
                    # 退化多边形 (外环空) 一并丢弃, 与 _polygon_rings 口径判定保持一致。
                    if isinstance(poly.get("points"), list) and poly.get("points")
                ]
            else:
                value["points"] = _remap_ring(value["points"])
                if value.get("holes"):
                    value["holes"] = [_remap_ring(h) for h in value["holes"]]
        else:
            continue
        out.append({**s, "value": value})
    return out


def merge_classify_attributes(
    boxes: list[dict],
    classify_results: list,
    *,
    write_keys: list[str] | None = None,
    label: str | None = None,
) -> int:
    """把下游分类结果的 attributes 合并 (union) 进对应父框的 attributes。

    下游每个结果的 ``task_id`` 是 crop 输入的 id (= 父框在 boxes 中的下标字符串)。
    取该结果 ``result`` 列表里**置信度最高**项的 ``attributes`` (下游若返回几何一并丢弃,
    本期只取属性, ROADMAP §3.3 write=attributes)。若声明 write_keys 则只取这些键。

    Args:
        boxes: 父框 LS result 列表 (原地修改, 写入 box["attributes"])。
        classify_results: 下游 ``client.predict`` 返回的 PredictionResult 列表。
        write_keys: 本阶段声明写哪些属性键 (原始键); None=全取下游返回的键。
        label: v0.18.14 · 设了则写回键加 f"{label}_" 前缀 (子物体命名空间, 如 hat_color);
            缺省写原始键 (双阶段零退化)。前缀在 write_keys 过滤之后施加。

    Returns:
        成功合并属性的父框数量 (供统计)。
    """
    merged = 0
    for cr in classify_results:
        try:
            box_idx = int(cr.task_id)
        except (TypeError, ValueError):
            continue
        if not (0 <= box_idx < len(boxes)):
            continue
        shapes = cr.result or []
        # 取置信度最高的结果项的 attributes
        best_attrs: dict = {}
        best_score = -1.0
        for s in shapes:
            if not isinstance(s, dict):
                continue
            attrs = s.get("attributes")
            if not isinstance(attrs, dict) or not attrs:
                continue
            score = float(s.get("score") or s.get("confidence") or 0.0)
            if score >= best_score:
                best_score = score
                best_attrs = attrs
        if not best_attrs:
            continue
        if write_keys:
            best_attrs = {k: v for k, v in best_attrs.items() if k in write_keys}
            if not best_attrs:
                continue
        if label:
            best_attrs = {f"{label}_{k}": v for k, v in best_attrs.items()}
        box = boxes[box_idx]
        existing = box.get("attributes")
        box["attributes"] = (
            {**existing, **best_attrs}
            if isinstance(existing, dict)
            else dict(best_attrs)
        )
        merged += 1
    return merged
