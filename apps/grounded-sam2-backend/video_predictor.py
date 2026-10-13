"""SAM 2.1 video tracker 推理封装.

平行于 predictor.py 的图片栈, 但走 **video** 入口:
    build_sam2_video_predictor (vendor/grounded-sam-2/sam2/build_sam.py:100)
      → SAM2VideoPredictor.init_state / add_new_points_or_box / propagate_in_video

权重独立: 与图片 `build_sam2` + `SAM2ImagePredictor` 不共享实例 (各自一份显存),
由 video_pool.py 管理 (平行于 model_pool.py, 不挤占图片池预算)。

协议契约 (apps/api video_tracker_adapters.MLBackendVideoTrackerAdapter):
    请求 context: {type:"video_tracker", from_frame, to_frame, direction,
                   prompt, source_geometry, model_key, ...}
    seed bbox 取自 prompt.geometry / source_geometry 的归一化 {x,y,w,h} (0-1)。
    响应 result 每条: {frame_index(源帧号), geometry:{type:"bbox",x,y,w,h},
                        confidence, outside}, x/y/w/h 归一化到 [0,1]。

vendor SAM2 video API 关键事实 (以实际 vendor 代码为准, commit 见 sync_vendor.sh):
  - init_state(video_path, ...): video_path 可为 .mp4 文件 (走 decord) 或
    "<frame_index>.jpg" JPEG 目录 (走 PIL)。一次性把整段加载进显存, **无窗口参数**。
    → 为把单次 init_state 显存/帧数压到 VIDEO_TRACKER_MAX_WINDOW_FRAMES 安全上限,
      我们用 OpenCV (已在镜像内, 不引 decord) 只解码窗内帧到临时 JPEG 目录,
      窗内重编号 0..N-1, 传播完再映射回源帧号。
  - add_new_points_or_box(state, frame_idx, obj_id, box=[x1,y1,x2,y2], normalize_coords=True):
    box 为**像素** xyxy (normalize_coords=True 时内部按 video_W/video_H 归一化)。
  - propagate_in_video(state, start_frame_idx, max_frame_num_to_track, reverse):
    yield (frame_idx, obj_ids, video_res_masks); masks 是原始视频分辨率的 logits
    (shape [num_obj, 1, H, W]), >0 为前景。
  - 会话清理: reset_state(state) 反向清理 + del state + 共享 best-effort CUDA 缓存清理。

坐标归一化约定照搬 predictor.py: 像素 / (w 或 h), clamp 到 [0,1], 与 _box_to_rect_label 同源。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

# 与 predictor.py 一致: 把 vendor 根注入 sys.path 供上游隐式包名解析。
_VENDOR_ROOT = "/app/vendor/grounded-sam-2"
if os.path.isdir(_VENDOR_ROOT) and _VENDOR_ROOT not in sys.path:
    sys.path.insert(0, _VENDOR_ROOT)

import cv2  # opencv-python-headless, 已在镜像内
import numpy as np
import torch

from aap_backend_runtime import (
    effective_device,
    free_gpu_memory,
    is_device_error,
    latch_cpu,
)
from aap_protocol_v2 import MaskPromptPayload
from mask_utils import decode_coco_rle
from mask_utils.polygon import mask_to_polygon  # 与图片栈共用的 mask→polygon 矢量化
from mask_utils.rle import encode_coco_rle

logger = logging.getLogger("grounded-sam2-backend.video")

# mask→polygon 简化容差 (像素); 与 predictor 单环路径同源默认。
_POLYGON_TOLERANCE = 1.0

CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "/app/checkpoints")

# 复用 predictor.py 的 SAM2_CONFIGS (同一 checkpoint 路径; video predictor 走
# build_sam2_video_predictor 这个不同入口, 但 config / 权重文件名相同)。
from predictor import SAM2_CONFIGS  # noqa: E402

# 单次 init_state 安全上限 (帧). 防止超长窗口一次性灌爆显存; main.py 解析 env 注入。
DEFAULT_MAX_WINDOW_FRAMES = int(os.getenv("VIDEO_TRACKER_MAX_WINDOW_FRAMES", "300"))
DEFAULT_MAX_CONTEXT_FRAMES = int(
    os.getenv("VIDEO_TRACKER_MAX_CONTEXT_FRAMES", str(DEFAULT_MAX_WINDOW_FRAMES))
)

# 该 obj 的固定 id (单目标传播; 平台一次只跟一个 annotation)。
_OBJ_ID = 1


@dataclass
class SAM2VideoSession:
    lo: int
    hi: int
    direction: str
    tmp_dir: str
    state: Any
    frame_w: int
    frame_h: int
    local_count: int


class SAM2VideoTracker:
    """单容器内 sam_variant 维度的 SAM 2.1 video predictor 封装。

    权重在 __init__ 时 build (冷启 1-3s), 之后每个 job 调 propagate(); 会话状态
    (init_state) 按 job 结束即释放, 模型权重留给下个 job 复用 (由 video_pool 管 LRU)。
    """

    def __init__(
        self,
        sam_variant: str = "tiny",
        *,
        max_window_frames: int = DEFAULT_MAX_WINDOW_FRAMES,
        max_context_frames: int = DEFAULT_MAX_CONTEXT_FRAMES,
    ) -> None:
        self.sam_variant = sam_variant
        self.max_window_frames = max_window_frames
        self.max_context_frames = max_context_frames
        self.device = effective_device("cuda")
        self.cleanup_uncertain = False
        self._predictor = self._load_video_predictor()
        # 当前活跃会话数 (0/1; 单 worker 串行, 仅用于 /health 观测)。
        self.active_sessions = 0

    def _load_video_predictor(self):
        if self.device == "cpu":
            return self._build_for_device("cpu")
        try:
            predictor = self._build_for_device(self.device)
        except Exception as exc:  # noqa: BLE001
            if not is_device_error(exc):
                raise
            self.cleanup_uncertain = True
            free_gpu_memory()
            predictor = self._build_for_device("cpu")
            self.device = "cpu"
            latch_cpu(
                f"GPU video predictor build failed; CPU replacement committed: {exc}"
            )
        return predictor

    def _build_for_device(self, device: str):
        from sam2.build_sam import build_sam2_video_predictor  # type: ignore[import-not-found]

        cfg_name, ckpt_name = SAM2_CONFIGS[self.sam_variant]
        ckpt_path = os.path.join(CHECKPOINT_DIR, ckpt_name)
        if not os.path.isfile(ckpt_path):
            # 与图片池一致: checkpoint 未预置交给 main.py 翻成 503。
            raise FileNotFoundError(ckpt_path)
        logger.info(
            "building video predictor variant=%s ckpt=%s", self.sam_variant, ckpt_name
        )
        return build_sam2_video_predictor(cfg_name, ckpt_path, device=device)

    # ---------- 公开接口 ----------

    def propagate(
        self,
        video_path: str,
        from_frame: int,
        to_frame: int,
        direction: str,
        seeds: list[dict[str, Any]],
        output_geometry: str = "bbox",
    ) -> list[dict[str, Any]]:
        """在 [from_frame, to_frame] 窗内按逐对象 seed(点/框)传播, 返回逐帧逐对象几何。

        seeds 每条 (与 sam3 PVS 同款协议, 多目标 = 多条 seed):
          - 单帧: {obj_id, bbox:{x,y,w,h}} | {obj_id, points:[[x,y,label],...]}, 锚在窗种子帧
            (forward=from_frame, backward=to_frame; 与平台 _tracker_windows 窗口方向一致);
          - 多帧 (纠偏): {obj_id, prompts:[{frame_index, points?/bbox?}, ...]}, frame_index=绝对源帧。
        坐标归一化 [0,1]。单目标(平台单 annotation 追踪)= 长度 1 的 seeds, instance_id="1"
        → 平台回填源 annotation, 与旧 seed-bbox 行为等价。vendor SAM2VideoPredictor 原生支持
        任意 obj_id + points/box (支持多目标, 不再硬编码单目标)。

        output_geometry: "bbox"(默认) 每帧降外接框; "polygon" 矢量化为多边形顶点。
        返回: [{frame_index(源帧号), instance_id, geometry, confidence, outside}], 含 seed 帧在内。
        空 mask / 退化多边形(顶点<3) → outside=True。
        """
        span = abs(to_frame - from_frame) + 1
        if span > self.max_window_frames:
            raise ValueError(
                f"video tracker window {span} frames exceeds "
                f"VIDEO_TRACKER_MAX_WINDOW_FRAMES={self.max_window_frames}"
            )
        session = self.start_session(video_path, from_frame, to_frame, direction, seeds)
        try:
            return self.continue_session(
                session, from_frame, to_frame, output_geometry=output_geometry
            )
        finally:
            self.close_session(session)

    def start_session(
        self,
        video_path: str,
        span_start_frame: int,
        span_end_frame: int,
        direction: str,
        seeds: list[dict[str, Any]],
    ) -> SAM2VideoSession:
        if not seeds:
            raise ValueError("video tracker requires at least one seed")
        lo, hi = (
            min(span_start_frame, span_end_frame),
            max(span_start_frame, span_end_frame),
        )
        span = hi - lo + 1
        if span > self.max_context_frames:
            raise ValueError(
                f"video tracker context {span} frames exceeds "
                f"VIDEO_TRACKER_MAX_CONTEXT_FRAMES={self.max_context_frames}"
            )
        if direction not in {"forward", "backward"}:
            raise ValueError(f"invalid tracker direction: {direction}")

        tmp_dir = tempfile.mkdtemp(prefix="sam2vid_")
        state = None
        self.active_sessions += 1
        try:
            frame_w, frame_h, local_count = self._extract_window_jpegs(
                video_path, lo, hi, tmp_dir
            )
            if local_count != span:
                raise ValueError(
                    f"decoded {local_count}/{span} tracker context frames [{lo},{hi}]"
                )
            state = self._predictor.init_state(
                video_path=tmp_dir,
                offload_video_to_cpu=True,
                offload_state_to_cpu=True,
            )
            local_seed = hi - lo if direction == "backward" else 0
            for seed in seeds:
                self._add_seed(
                    state, local_seed, seed, lo, local_count, frame_w, frame_h
                )
            return SAM2VideoSession(
                lo, hi, direction, tmp_dir, state, frame_w, frame_h, local_count
            )
        except BaseException:
            if state is None and self.device != "cpu":
                self.cleanup_uncertain = True
            if state is not None:
                self._predictor.reset_state(state)
            self._cleanup_tmp(tmp_dir)
            free_gpu_memory()
            self.active_sessions = max(0, self.active_sessions - 1)
            raise

    def continue_session(
        self,
        session: SAM2VideoSession,
        from_frame: int,
        to_frame: int,
        *,
        output_geometry: str = "bbox",
    ) -> list[dict[str, Any]]:
        lo, hi = min(from_frame, to_frame), max(from_frame, to_frame)
        if lo < session.lo or hi > session.hi:
            raise ValueError("tracker window is outside context span")
        local_start = (hi if session.direction == "backward" else lo) - session.lo
        results: list[dict[str, Any]] = []
        for local_idx, obj_ids, video_res_masks in self._predictor.propagate_in_video(
            session.state,
            start_frame_idx=local_start,
            max_frame_num_to_track=hi - lo,
            reverse=session.direction == "backward",
        ):
            src_idx = int(local_idx) + session.lo
            if not lo <= src_idx <= hi:
                continue
            masks = self._masks_to_bool(video_res_masks)
            for i, oid in enumerate(self._obj_ids_to_list(obj_ids)):
                mask = masks[i] if i < masks.shape[0] else masks[0]
                if output_geometry == "mask":
                    outside = not bool(mask.any())
                    geometry = {
                        "type": "mask",
                        "rle": encode_coco_rle(
                            mask.reshape(-1), session.frame_w, session.frame_h
                        ),
                    }
                elif output_geometry == "polygon":
                    geometry, outside = self._mask_to_polygon_geometry(
                        mask, session.frame_w, session.frame_h
                    )
                else:
                    bbox_norm, outside = self._mask_to_bbox_norm(
                        mask, session.frame_w, session.frame_h
                    )
                    geometry = {"type": "bbox", **bbox_norm}
                score = self._object_score(session.state, int(oid), int(local_idx))
                results.append(
                    {
                        "frame_index": src_idx,
                        "instance_id": str(int(oid)),
                        "geometry": geometry,
                        "confidence": 0.0
                        if outside
                        else score
                        if score is not None
                        else 1.0,
                        "outside": outside,
                    }
                )
        results.sort(
            key=lambda row: (row["frame_index"], row["instance_id"]),
            reverse=session.direction == "backward",
        )
        return results

    def close_session(self, session: SAM2VideoSession) -> None:
        try:
            self._predictor.reset_state(session.state)
        except Exception:  # noqa: BLE001
            logger.exception("reset_state failed; dropping inference_state")
            self.cleanup_uncertain = True
        session.state = None
        self._cleanup_tmp(session.tmp_dir)
        free_gpu_memory()
        self.active_sessions = max(0, self.active_sessions - 1)

    # ---------- 内部工具 ----------

    @staticmethod
    def _extract_window_jpegs(
        video_path: str, lo: int, hi: int, out_dir: str
    ) -> tuple[int, int, int]:
        """用 OpenCV 把源帧 [lo, hi] 解码为 out_dir/<local_idx>.jpg。

        返回 (frame_w, frame_h, 实际写出的帧数)。窗内重编号从 0 起 (vendor JPEG
        loader 要求文件名为 "<int>.jpg" 且按数值排序)。
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"OpenCV cannot open video: {video_path[:80]}")
        try:
            frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
            frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
            # 直接 seek 到窗首帧 (源帧号), 然后顺序读 span 帧。
            cap.set(cv2.CAP_PROP_POS_FRAMES, lo)
            written = 0
            for src in range(lo, hi + 1):
                ok, frame = cap.read()
                if not ok:
                    break  # 视频实际帧数不足, 窗被截断。
                local_idx = src - lo
                # OpenCV 读出 BGR; cv2.imwrite 也期望 BGR, 写盘正确。vendor JPEG
                # loader 再用 PIL.convert("RGB") 读回, 通道顺序由 PIL 处理。
                cv2.imwrite(os.path.join(out_dir, f"{local_idx}.jpg"), frame)
                if frame_w == 0 or frame_h == 0:
                    frame_h, frame_w = frame.shape[:2]
                written += 1
            return frame_w, frame_h, written
        finally:
            cap.release()

    @staticmethod
    def _seed_to_pixel_xyxy(seed_bbox: dict[str, float], w: int, h: int) -> list[float]:
        """归一化 {x,y,w,h} (左上+宽高) → 像素 xyxy。

        与 predictor.predict_bbox 的"归一化 * 宽高"约定同源。
        """
        x = float(seed_bbox.get("x", 0.0))
        y = float(seed_bbox.get("y", 0.0))
        bw = float(seed_bbox.get("w", seed_bbox.get("width", 0.0)))
        bh = float(seed_bbox.get("h", seed_bbox.get("height", 0.0)))
        x1 = x * w
        y1 = y * h
        x2 = (x + bw) * w
        y2 = (y + bh) * h
        return [x1, y1, x2, y2]

    # ---------- 多目标逐对象播种 (与 sam3 PVS wrapper 同款) ----------

    def _add_prompt(
        self,
        state: Any,
        frame_idx: int,
        obj_id: int,
        prompt: dict[str, Any],
        frame_w: int,
        frame_h: int,
    ) -> bool:
        """在指定(局部)帧对 obj_id 加一条 prompt(框/点)。归一化输入 → 像素
        (normalize_coords=True 时 vendor 内部再按 video 宽高归一化)。无框无点返回 False。"""
        mask_prompt = prompt.get("mask_prompt")
        if mask_prompt is not None:
            payload = MaskPromptPayload.model_validate(mask_prompt)
            height, width = payload.rle.size
            if (height, width) != (frame_h, frame_w):
                raise ValueError(
                    "mask_prompt size must match video frame "
                    f"{frame_h}x{frame_w}, got {height}x{width}"
                )
            decoded = decode_coco_rle(payload.rle.model_dump(mode="json"))
            mask = torch.from_numpy(
                np.frombuffer(decoded, dtype=np.uint8).reshape(height, width).copy()
            ).to(dtype=torch.bool)
            self._predictor.add_new_mask(
                inference_state=state,
                frame_idx=frame_idx,
                obj_id=obj_id,
                mask=mask,
            )
            return True

        bbox = prompt.get("bbox")
        pts = prompt.get("points")
        if not bbox and not pts:
            return False
        box_px = self._seed_to_pixel_xyxy(bbox, frame_w, frame_h) if bbox else None
        points_px = None
        labels = None
        if pts:
            points_px = np.array(
                [[float(p[0]) * frame_w, float(p[1]) * frame_h] for p in pts],
                dtype=np.float32,
            )
            labels = np.array(
                [int(p[2]) if len(p) > 2 else 1 for p in pts], dtype=np.int32
            )
        self._predictor.add_new_points_or_box(
            inference_state=state,
            frame_idx=frame_idx,
            obj_id=obj_id,
            points=points_px,
            labels=labels,
            box=box_px,
            normalize_coords=True,
        )
        return True

    def _add_seed(
        self,
        state: Any,
        local_seed: int,
        seed: dict[str, Any],
        lo: int,
        local_count: int,
        frame_w: int,
        frame_h: int,
    ) -> None:
        """把一条 seed 的 prompt(s) 按 obj_id 加进 state。

        - 多帧 (纠偏): seed["prompts"]=[{frame_index?, points?/bbox?}], 各在其局部帧
          (frame_index-lo) 播种，越界稳定拒绝; 缺 frame_index 的落窗种子帧。
        - 单帧: seed 直接带 points/bbox/mask_prompt, 落窗种子帧 local_seed。
        """
        obj_id = int(seed["obj_id"])
        prompts = seed.get("prompts")
        if isinstance(prompts, list) and prompts:
            added = 0
            for p in prompts:
                if not isinstance(p, dict):
                    continue
                fi = p.get("frame_index")
                if fi is None:
                    local_f = local_seed
                else:
                    local_f = int(fi) - lo
                    if local_f < 0 or local_f >= local_count:
                        raise ValueError(
                            f"seed frame {fi} outside decoded window "
                            f"[{lo},{lo + local_count - 1}]"
                        )
                if self._add_prompt(state, local_f, obj_id, p, frame_w, frame_h):
                    added += 1
            if added == 0:
                raise ValueError(f"seed obj_id={obj_id} 的 prompts 无有效框/点/Mask")
        elif not self._add_prompt(state, local_seed, obj_id, seed, frame_w, frame_h):
            raise ValueError(
                f"seed for obj_id={obj_id} has neither bbox, points nor mask_prompt"
            )

    @staticmethod
    def _masks_to_bool(video_res_masks: Any) -> np.ndarray:
        """propagate_in_video 的 video_res_masks: [num_obj, 1, H, W] logits →
        [num_obj, H, W] bool (>0 前景)。单对象无 num_obj 维时补上。"""
        m = video_res_masks
        if hasattr(m, "detach"):
            m = m.detach()
        if hasattr(m, "cpu"):
            m = m.cpu()
        arr = np.asarray(m)
        if arr.ndim == 4:  # (num_obj, 1, H, W)
            arr = arr[:, 0]
        if arr.ndim == 2:  # (H, W) — 单对象无 num_obj 维
            arr = arr[None]
        return arr > 0.0

    @staticmethod
    def _obj_ids_to_list(obj_ids: Any) -> list[int]:
        """propagate_in_video 的 obj_ids (list / tensor) → [int, ...]。缺省回退 [_OBJ_ID]。"""
        if obj_ids is None:
            return [_OBJ_ID]
        if hasattr(obj_ids, "tolist"):
            return [int(x) for x in obj_ids.tolist()]
        return [int(x) for x in obj_ids]

    @staticmethod
    def _object_score(
        inference_state: Any, obj_id: int, local_frame_idx: int
    ) -> float | None:
        """取该帧该对象的 object_score_logits → sigmoid 概率 [0,1]。

        SAM2 传播时每帧自评一个 object score (目标是否存在 / 是否被遮挡), 存在
        ``inference_state["output_dict_per_obj"][obj_idx][storage_key][frame_idx]``
        (storage_key: 种子帧=cond_frame_outputs、传播帧=non_cond_frame_outputs),
        由 vendor 在 yield 前的 _add_output_per_object 写入。vendor 是 pinned 版本、
        结构稳定; 仍包一层兜底——键缺失 / 形状异常 (未来 sync 漂移) 时返回 None,
        调用方回退旧的「有 mask=1.0」, 不因内部结构变动丢结果或崩。
        """
        try:
            obj_idx = inference_state["obj_id_to_idx"][obj_id]
            per_obj = inference_state["output_dict_per_obj"][obj_idx]
            out = per_obj["cond_frame_outputs"].get(local_frame_idx) or per_obj[
                "non_cond_frame_outputs"
            ].get(local_frame_idx)
            if out is None:
                return None
            logit = torch.as_tensor(out["object_score_logits"]).flatten()
            if logit.numel() == 0:
                return None
            return float(torch.sigmoid(logit[0].float()).item())
        except (KeyError, TypeError, IndexError, RuntimeError):
            return None

    @staticmethod
    def _mask_to_bbox_norm(
        mask: np.ndarray, w: int, h: int
    ) -> tuple[dict[str, float], bool]:
        """mask 外接框 → 归一化 {x,y,w,h}; 空 mask → outside=True (零框)。

        归一化照搬 predictor._box_to_rect_label: 像素 / 宽高, clamp 到 [0,1]。
        """
        ys, xs = np.where(mask)
        if xs.size == 0 or ys.size == 0:
            return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}, True
        x1, x2 = int(xs.min()), int(xs.max())
        y1, y2 = int(ys.min()), int(ys.max())
        # x2/y2 是包含端点的像素索引, 宽高 +1 像素。
        bw = (x2 - x1 + 1) / w
        bh = (y2 - y1 + 1) / h

        def _clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        return {
            "x": _clamp(x1 / w),
            "y": _clamp(y1 / h),
            "w": _clamp(bw),
            "h": _clamp(bh),
        }, False

    @staticmethod
    def _mask_to_polygon_geometry(
        mask: np.ndarray, w: int, h: int
    ) -> tuple[dict[str, Any], bool]:
        """mask → 归一化多边形 geometry; 空 mask / 顶点<3 → outside=True (空 points)。

        复用图片栈 ``mask_to_polygon`` (cv2.findContours 取最大外环 → shapely.simplify →
        归一化)。polygon track keyframe 要求 points 至少 3 个, 退化时标 outside 让平台跳过。
        """
        points = mask_to_polygon(mask, _POLYGON_TOLERANCE, normalize_to=(w, h))
        if len(points) < 3:
            return {"type": "polygon", "points": []}, True
        return {"type": "polygon", "points": points}, False

    @staticmethod
    def _cleanup_tmp(tmp_dir: str) -> None:
        import shutil

        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.debug("failed to remove tmp dir %s", tmp_dir)
