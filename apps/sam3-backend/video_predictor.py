"""SAM 3.1 multiplex video tracker 推理封装.

平行于 grounded-sam2 的 video_predictor.py, 但走 **text-driven** 视频追踪:
    build_sam3_multiplex_video_predictor
      → handle_request(start_session) / handle_request(add_prompt, text=...)
      → handle_stream_request(propagate_in_video) → 每帧多目标 mask
      → handle_request(close_session)

与 grounded-sam2 的 seed-bbox 传播不同: SAM3 按**文本 query** 在每帧检测目标
(multiplex 多目标), 平台首切片按单目标消费——用 seed_bbox 在种子帧挑重叠最大的
目标 obj_id, 跨帧跟随该 obj_id。mask 复用图片栈 mask_to_polygon 矢量化。

协议契约 (apps/api video_tracker_adapters + runner):
    请求 context: {type:"video_tracker", from_frame, to_frame, direction, text,
                   source_geometry(种子几何), output_geometry, ...}
    响应 result 每条: {frame_index(源帧号), geometry:{type:"polygon"|"bbox", ...},
                        confidence, outside}, 坐标归一化到 [0,1]。

显存: 图像 sam3.pt(~5.8GB) + 视频 multiplex(~3.2GB) 约 9GB。二者可并存常驻
(24GB 卡容得下并存), 各自独立懒加载 / idle 卸载; 小显存部署若不需视频设 SAM3_DOWNLOAD_VIDEO=0。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from typing import Any

_VENDOR_ROOT = "/app/vendor/sam3"
if os.path.isdir(_VENDOR_ROOT) and _VENDOR_ROOT not in sys.path:
    sys.path.insert(0, _VENDOR_ROOT)

import cv2  # opencv-python-headless, 已在镜像内
import numpy as np
import torch

from aap_backend_runtime import (
    DeviceUnavailableError,
    free_gpu_memory,
    is_device_error,
    require_gpu_device,
)
from mask_utils.polygon import mask_to_polygon  # 与图片栈共用的 mask→polygon 矢量化
from mask_utils.rle import encode_coco_rle

logger = logging.getLogger("sam3-backend.video")

CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "/app/checkpoints")
_VIDEO_CKPT = os.path.join(CHECKPOINT_DIR, "sam3.1_multiplex.pt")
_BPE_PATH = os.path.join(_VENDOR_ROOT, "sam3/assets/bpe_simple_vocab_16e6.txt.gz")
# FlashAttention-3 仅 Hopper(SM90) 优化; 消费级 Ada(4060, SM89) 默认关。可用 env 覆盖。
_USE_FA3 = os.getenv("SAM3_VIDEO_USE_FA3", "0") == "1"
# 单次窗口安全上限 (帧), 防超长窗口灌爆显存。视频前向显存随窗口近似线性增长
# (实测 ~0.85GB/帧, 基线 ~5.4GB, 16 帧 ~18.9GB, 41 帧即 OOM@24GB), 故默认取 16
# 作硬上限; runner 侧 video_tracker_sam3_window_size_frames 会先把请求切到该量级,
# 本 env 是 backend 侧兜底 (直连 /predict 或配置漂移时拒绝超限窗口而非 OOM)。
DEFAULT_MAX_WINDOW_FRAMES = int(os.getenv("SAM3_VIDEO_MAX_WINDOW_FRAMES", "16"))
# mask→polygon 简化容差 (像素), 与图片栈同源默认。
_POLYGON_TOLERANCE = 1.0


class SAM3MultiplexVideoTracker:
    """SAM 3.1 multiplex video predictor 封装 (单模型, 无变体维)。

    权重在 __init__ build (冷启数十秒), 之后每个 job 起独立 session; session 结束即
    close_session 释放, 模型权重留给下个 job 复用 (由 main.py 持有/idle 卸载)。
    """

    def __init__(
        self,
        *,
        use_fa3: bool = _USE_FA3,
        max_window_frames: int = DEFAULT_MAX_WINDOW_FRAMES,
    ) -> None:
        self.max_window_frames = max_window_frames
        self.device = require_gpu_device("cuda")
        self.active_sessions = 0
        self.cleanup_uncertain = False
        self._predictor = self._load_predictor(use_fa3)

    def _load_predictor(self, use_fa3: bool):
        from sam3.model_builder import (  # type: ignore[import-not-found]
            build_sam3_multiplex_video_predictor,
        )

        if not os.path.isfile(_VIDEO_CKPT):
            raise FileNotFoundError(_VIDEO_CKPT)
        logger.info(
            "building sam3 multiplex video predictor ckpt=%s use_fa3=%s",
            _VIDEO_CKPT,
            use_fa3,
        )
        # max_num_objects / multiplex_count 锁定 16 (checkpoint 按此训练, 调小会
        # state_dict 形状不匹配加载失败)——不暴露为可调项。
        # vendor builder 无 device= 形参且内部硬绑 CUDA；SAM3 本期明确为 GPU-only。
        try:
            predictor = build_sam3_multiplex_video_predictor(
                checkpoint_path=_VIDEO_CKPT,
                bpe_path=_BPE_PATH if os.path.isfile(_BPE_PATH) else None,
                use_fa3=use_fa3,
                warm_up=False,
            )
        except Exception as exc:  # noqa: BLE001
            if is_device_error(exc):
                free_gpu_memory()
                raise DeviceUnavailableError(
                    "SAM3 multiplex video model requires a healthy CUDA device; "
                    "CPU fallback is not supported"
                ) from exc
            raise
        _patch_init_state_kwargs(predictor)
        return predictor

    # ---------- 公开接口 ----------

    def propagate(
        self,
        video_path: str,
        from_frame: int,
        to_frame: int,
        direction: str,
        text: str,
        seed_bbox: dict[str, float] | None = None,
        output_geometry: str = "bbox",
        seed_bboxes: list[dict[str, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run one multiplex window under a bounded BF16 autocast scope."""

        device_type = self.device.split(":", 1)[0]
        with torch.autocast(
            device_type=device_type,
            dtype=torch.bfloat16,
            enabled=device_type == "cuda",
        ):
            return self._propagate(
                video_path,
                from_frame,
                to_frame,
                direction,
                text,
                seed_bbox,
                output_geometry,
                seed_bboxes,
            )

    def _propagate(
        self,
        video_path: str,
        from_frame: int,
        to_frame: int,
        direction: str,
        text: str,
        seed_bbox: dict[str, float] | None = None,
        output_geometry: str = "bbox",
        seed_bboxes: list[dict[str, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """在 [from_frame, to_frame] 窗内按 text 检测+追踪目标, 返回逐帧几何。

        text: 文本 query (必填; SAM3 每帧按此检测目标)。
        seed_bbox: 首个归一化 {x,y,w,h}; 用于选择主实例。
        seed_bboxes: 上一分窗逐实例续追框；与 text 一起作为种子帧正框提示。
        output_geometry: "mask"→原生 RLE；"polygon"→矢量化多边形；否则取外接框 bbox。
        """
        if not text or not text.strip():
            raise ValueError("sam3_video tracker requires non-empty text query")
        lo, hi = int(min(from_frame, to_frame)), int(max(from_frame, to_frame))
        span = hi - lo + 1
        if span > self.max_window_frames:
            raise ValueError(
                f"video tracker window {span} frames exceeds "
                f"SAM3_VIDEO_MAX_WINDOW_FRAMES={self.max_window_frames}"
            )
        reverse = direction == "backward"
        seed_src_frame = hi if reverse else lo

        tmp_dir: str | None = None
        session_id = None
        self.active_sessions += 1
        try:
            tmp_dir = tempfile.mkdtemp(prefix="sam3vid_")
            # 窗口不完整只 warn (见 _extract_window_jpegs); 但下面两种截断必须硬失败:
            _fw, _fh, local_count = self._extract_window_jpegs(
                video_path, lo, hi, tmp_dir
            )
            if local_count == 0:
                raise ValueError(
                    f"no frames decoded from {video_path[:80]} for window [{lo},{hi}]"
                )
            # 种子帧落进未解码区 (常见于 backward 追踪、种子在窗尾 hi): 若放行, 下面的
            # clamp 会把它静默挪到实际最后一帧, 追踪从一个完全错误的种子帧开始且无提示。
            if seed_src_frame - lo >= local_count:
                raise ValueError(
                    f"seed frame {seed_src_frame} not in decodable range "
                    f"[{lo},{lo + local_count - 1}] (window [{lo},{hi}] decoded only "
                    f"{local_count} frames) from {video_path[:80]}"
                )
            # 上面已校验 seed 在 [lo, lo+local_count-1] 内, clamp 对合法输入是无害的边界保护。
            local_seed = max(0, min(seed_src_frame - lo, local_count - 1))

            session_id = self._predictor.handle_request(
                dict(type="start_session", resource_path=tmp_dir)
            )["session_id"]

            # 首窗只传 text 做开放目标发现；后续窗同时传入上一窗逐实例末框，避免新 session
            # 在窗首文本暂时未检出时整窗变空。
            prompt_request: dict[str, Any] = dict(
                type="add_prompt",
                session_id=session_id,
                frame_index=local_seed,
                text=text.strip(),
            )
            if seed_bboxes:
                prompt_request["bounding_boxes"] = [
                    [bbox["x"], bbox["y"], bbox["w"], bbox["h"]] for bbox in seed_bboxes
                ]
                prompt_request["bounding_box_labels"] = [1] * len(seed_bboxes)
            seed_out = self._predictor.handle_request(prompt_request)["outputs"]
            # 多目标批量吐: 不收敛单目标, 逐帧把**全部**
            # 检测对象各出一条结果 (instance_id = 窗内 obj_id)。仅在种子帧挑出与 seed_bbox /
            # 最高分对应的**主实例** (primary), 供平台回填源轨迹; 其余各成新轨迹。窗内 obj_id
            # 稳定, 跨窗身份由平台按边界帧 IoU 关联 (见 video_tracker_runner)。
            primary_obj_id = self._pick_target_obj(seed_out, seed_bbox)

            # 收集逐帧输出 (含种子帧)。
            per_frame: dict[int, dict] = {local_seed: seed_out}
            for response in self._predictor.handle_stream_request(
                dict(
                    type="propagate_in_video",
                    session_id=session_id,
                    propagation_direction=("backward" if reverse else "forward"),
                    start_frame_index=local_seed,
                    max_frame_num_to_track=local_count,
                )
            ):
                per_frame[int(response["frame_index"])] = response["outputs"]

            results: list[dict[str, Any]] = []
            geom_empty, _ = self._mask_geometry(
                np.zeros((_fh, _fw), dtype=bool),
                output_geometry,
            )
            seed_obj_ids = _to_numpy(seed_out.get("out_obj_ids"))
            known_obj_ids = (
                {int(oid) for oid in seed_obj_ids.tolist()}
                if seed_obj_ids is not None
                else set()
            )
            for local_idx in sorted(per_frame, reverse=reverse):
                if local_idx < 0 or local_idx >= local_count:
                    continue
                src_idx = local_idx + lo
                outputs = per_frame[local_idx]
                obj_ids = _to_numpy(outputs.get("out_obj_ids"))
                current_obj_ids = (
                    {int(oid) for oid in obj_ids.tolist()}
                    if obj_ids is not None
                    else set()
                )
                known_obj_ids.update(current_obj_ids)
                for oid in sorted(known_obj_ids):
                    is_primary = oid == primary_obj_id
                    mask = (
                        self._obj_mask(
                            outputs,
                            oid,
                            frame_index=src_idx,
                            preserve_pixels=output_geometry == "mask",
                        )
                        if oid in current_obj_ids
                        else None
                    )
                    if mask is None or not mask.any():
                        results.append(
                            {
                                "frame_index": src_idx,
                                "instance_id": str(oid),
                                "geometry": dict(geom_empty),
                                "confidence": 0.0,
                                "outside": True,
                                "primary": is_primary,
                            }
                        )
                        continue
                    geometry, outside = self._mask_geometry(mask, output_geometry)
                    results.append(
                        {
                            "frame_index": src_idx,
                            "instance_id": str(oid),
                            "geometry": geometry,
                            "confidence": 0.0 if outside else 1.0,
                            "outside": outside,
                            "primary": is_primary,
                        }
                    )
            return results
        finally:
            if session_id is not None:
                try:
                    self._predictor.handle_request(
                        dict(type="close_session", session_id=session_id)
                    )
                except Exception:  # noqa: BLE001
                    self.cleanup_uncertain = True
                    logger.exception("close_session failed")
            if tmp_dir is not None:
                self._cleanup_tmp(tmp_dir)
            free_gpu_memory()
            self.active_sessions = max(0, self.active_sessions - 1)

    # ---------- 内部工具 ----------

    @staticmethod
    def _extract_window_jpegs(
        video_path: str, lo: int, hi: int, out_dir: str
    ) -> tuple[int, int, int]:
        """OpenCV 把源帧 [lo,hi] 解码为 out_dir/<local_idx>.jpg (窗内重编号 0..N-1)。"""
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"OpenCV cannot open video: {video_path[:80]}")
        try:
            frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
            frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
            cap.set(cv2.CAP_PROP_POS_FRAMES, lo)
            written = 0
            for src in range(lo, hi + 1):
                ok, frame = cap.read()
                if not ok:
                    # 中途解码失败 (损坏帧 / 容器元数据帧数虚报 / VFR): 停在此处但不静默 —
                    # 尾部少解码几帧通常无害 (forward 追踪的种子帧在窗首)。是否需硬失败
                    # (种子帧落进未解码区 / 一帧都没解出) 由调用方 propagate 判定。
                    logger.warning(
                        "sam3_video window [%d,%d] decode truncated: requested %d frames "
                        "but only %d decoded from %s",
                        lo,
                        hi,
                        hi - lo + 1,
                        written,
                        video_path[:80],
                    )
                    break
                cv2.imwrite(os.path.join(out_dir, f"{src - lo}.jpg"), frame)
                if frame_w == 0 or frame_h == 0:
                    frame_h, frame_w = frame.shape[:2]
                written += 1
            return frame_w, frame_h, written
        finally:
            cap.release()

    @staticmethod
    def _obj_mask(
        outputs: dict,
        obj_id: int,
        *,
        frame_index: int,
        preserve_pixels: bool = False,
    ) -> np.ndarray | None:
        """从某帧 multiplex 输出取指定 obj_id 的二值 mask (HxW bool); 无则 None。"""
        obj_ids = _to_numpy(outputs.get("out_obj_ids"))
        masks = _to_numpy(outputs.get("out_binary_masks"))
        if obj_ids is None or masks is None:
            return None
        _assert_masks_align(masks, obj_ids, frame_index=frame_index)
        for idx, oid in enumerate(obj_ids.tolist()):
            if int(oid) == obj_id:
                raw = masks[idx] > 0
                # bbox / polygon 保留旧的追踪稳定化；原生 Mask 必须逐像素透传。
                return raw if preserve_pixels else _largest_blob(raw)
        return None

    @staticmethod
    def _pick_target_obj(
        outputs: dict, seed_bbox: dict[str, float] | None
    ) -> int | None:
        """种子帧从 multiplex 多目标里挑目标 obj_id。

        有 seed_bbox: 取归一化外接框与 seed_bbox IoU 最大的目标; 无: 取最高分 (或首个)。
        """
        obj_ids = _to_numpy(outputs.get("out_obj_ids"))
        masks = _to_numpy(outputs.get("out_binary_masks"))
        if obj_ids is None or masks is None or len(obj_ids) == 0:
            return None
        _assert_masks_align(masks, obj_ids)
        probs = _to_numpy(outputs.get("out_probs"))
        if seed_bbox is None or not any(seed_bbox.get(k) for k in ("w", "h")):
            # 无种子: 最高分 / 首个
            if probs is not None and len(probs) == len(obj_ids):
                return int(obj_ids[int(np.argmax(probs))])
            return int(obj_ids[0])
        best_id, best_iou = None, -1.0
        for idx, oid in enumerate(obj_ids.tolist()):
            m = masks[idx] > 0
            if not m.any():
                continue
            iou = _bbox_iou(_mask_bbox_norm(m), seed_bbox)
            if iou > best_iou:
                best_iou, best_id = iou, int(oid)
        if best_id is None:  # 全空 mask, 退最高分
            if probs is not None and len(probs) == len(obj_ids):
                return int(obj_ids[int(np.argmax(probs))])
            return int(obj_ids[0])
        return best_id

    @staticmethod
    def _mask_geometry(mask: np.ndarray, output_geometry: str) -> tuple[dict, bool]:
        """mask → geometry; 空/退化 → outside。"""
        h, w = mask.shape[:2]
        if output_geometry == "mask":
            return {
                "type": "mask",
                "rle": encode_coco_rle(mask.reshape(-1), w, h),
            }, not bool(mask.any())
        if output_geometry == "polygon":
            points = mask_to_polygon(mask, _POLYGON_TOLERANCE, normalize_to=(w, h))
            if len(points) < 3:
                return {"type": "polygon", "points": []}, True
            return {"type": "polygon", "points": points}, False
        bbox = _mask_bbox_norm(mask)
        if bbox["w"] <= 0 or bbox["h"] <= 0:
            return {"type": "bbox", **bbox}, True
        return {"type": "bbox", **bbox}, False

    @staticmethod
    def _cleanup_tmp(tmp_dir: str) -> None:
        import shutil

        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.debug("failed to remove tmp dir %s", tmp_dir)


def _patch_init_state_kwargs(predictor: Any) -> None:
    """兼容垫片: vendor Sam3BasePredictor.start_session 无条件给 init_state 传
    offload_state_to_cpu / video_loader_type, 但 multiplex 模型的 init_state 签名不收
    (sam3_multiplex_tracking.init_state 只认 resource_path/offload_video_to_cpu/
    async_loading_frames/use_*), 直接调用会 TypeError。包一层按真实签名过滤 kwargs。
    """
    import inspect

    model = getattr(predictor, "model", None)
    if model is None or not hasattr(model, "init_state"):
        return
    orig = model.init_state
    try:
        accepted = set(inspect.signature(orig).parameters)
    except (TypeError, ValueError):
        return
    if any(p in accepted for p in ("kwargs", "args")):
        return  # 已收 **kwargs, 无需过滤

    def _filtered(*args: Any, **kwargs: Any) -> Any:
        return orig(*args, **{k: v for k, v in kwargs.items() if k in accepted})

    model.init_state = _filtered


def output_geometry_type(output_geometry: str) -> str:
    if output_geometry == "mask":
        return "mask"
    return "polygon" if output_geometry == "polygon" else "bbox"


def _assert_masks_align(
    masks: np.ndarray, obj_ids: np.ndarray, *, frame_index: int | None = None
) -> None:
    """校验 vendor multiplex 输出的 masks 与 obj_ids 长度一致。

    不一致时抛带上下文的 ValueError (经 main.py 的 ValueError handler → 400 + detail),
    避免下游按 obj_ids 索引 masks 时裸抛 IndexError → unhandled 500。种子帧无帧号
    (frame_index=None), 逐帧传播时带源帧号定位是哪一帧的 vendor 输出坏了。
    """
    if len(masks) != len(obj_ids):
        where = "seed frame" if frame_index is None else f"frame {frame_index}"
        raise ValueError(
            f"sam3_video {where} shape mismatch: "
            f"{len(obj_ids)} obj_ids vs {len(masks)} masks"
        )


def _to_numpy(x: Any) -> np.ndarray | None:
    if x is None:
        return None
    if hasattr(x, "detach"):
        x = x.detach()
    if hasattr(x, "cpu"):
        x = x.cpu()
    return np.asarray(x)


_OPEN_KERNEL = np.ones((3, 3), np.uint8)


def _largest_blob(mask: np.ndarray) -> np.ndarray:
    """形态学开运算断细带 + 取最大连通域。

    multiplex 偶发对目标输出「紧凑车身 + 沿细带 8-连通拉出去的毛刺」的 mask:
    像素数与正常帧相当, 却令朴素 min/max 外接框虚高 (实测 frame 4/10/14 的归一化
    宽度从 ~0.03 飙到 0.26-0.31, 把车 + 远处结构并进一个又宽又稀的框)。3x3 开运算
    断掉 ≤2px 细带、再取最大连通域即恢复紧凑框, 正常帧不受影响。开运算把 mask 抹空
    (极小目标) 或只剩一个域时保守退回原 mask。
    """
    u8 = mask.astype(np.uint8)
    opened = cv2.morphologyEx(u8, cv2.MORPH_OPEN, _OPEN_KERNEL)
    if not opened.any():
        return mask
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        opened, connectivity=8
    )
    if n_labels <= 1:
        return mask
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return labels == largest


def _mask_bbox_norm(mask: np.ndarray) -> dict[str, float]:
    """二值 mask → 归一化外接框 {x,y,w,h}; 空 mask → 零框。"""
    ys, xs = np.where(mask)
    if xs.size == 0 or ys.size == 0:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    h, w = mask.shape[:2]
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())

    def _clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    return {
        "x": _clamp(x1 / w),
        "y": _clamp(y1 / h),
        "w": _clamp((x2 - x1 + 1) / w),
        "h": _clamp((y2 - y1 + 1) / h),
    }


def _bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1 = b.get("x", 0.0), b.get("y", 0.0)
    bx2 = b.get("x", 0.0) + b.get("w", 0.0)
    by2 = b.get("y", 0.0) + b.get("h", 0.0)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = a["w"] * a["h"] + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0
