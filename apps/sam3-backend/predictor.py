"""SAM 3 推理封装 (vendor 对齐).

vendor: facebookresearch/sam3 @ 4cbac14, 通过 scripts/sync_vendor.sh 同步.
入口: `from sam3 import build_sam3_image_model` + `from sam3.model.sam3_image_processor import Sam3Processor`.

支持的 prompt (启用 inst_interactivity):
  - text:            processor.set_text_prompt(prompt, state) → 全图所有匹配概念的 mask + box
  - exemplar:        processor.add_geometric_prompt(box, label=True, state) → 全图相似实例 (PCS)
  - point:           model.predict_inst(state, point_coords, point_labels) → 单实例点交互 (SAM-style)
  - interactive_box: model.predict_inst(state, box) → 单框单 mask (SAM-style, ≠ exemplar 的全图相似)
  注: point / interactive_box 与 PCS 共用同一 backbone_out 缓存 (开 inst 后 set_image 同产两路特征).

API 形态关键点 (与 SAM 2 / grounded-sam2 完全不同):
  1. Sam3Processor 是 stateful wrapper, state 是 dict. set_image() 把图像 features 写到
     state["backbone_out"], 后续 prompt 调用是副作用修改 state.
  2. _forward_grounding 写 state["masks" / "boxes" / "scores" / "masks_logits"]:
       - boxes: 像素 xyxy (已转换好)
       - masks: bool tensor (N, 1, H, W), 已 interpolate 到原图分辨率
       - scores: float (N,), 已经 sigmoid 过
  3. confidence_threshold 是 processor 实例属性; 单 worker 串行下可临时修改实现 per-request override.
  4. reset_all_prompts(state) 清掉 language_features + geometric_prompt + boxes/masks/scores,
     但保留 backbone_out 中的图像 features. 缓存命中时只缓存 backbone_out + 原图尺寸.
  5. 没有 label / phrase 输出. 一次 text prompt 是单 phrase, 所有 N 个 mask 共用同一 label.
  6. bbox 输入: 归一化 cxcywh (中心 + 宽高), 用 vendor/sam3/sam3/model/box_ops.box_xywh_to_cxcywh
     转换. 我们对外协议用归一化 xyxy, 转换在 predictor 内部完成.

mask → polygon 简化复用 apps/_shared/mask_utils; 与 grounded-sam2-backend 同源.

Idle unload 集成: __init__ 加载到 self.device; main.py 在 idle 后 del self._predictor +
torch.cuda.empty_cache(); 重建是再调一次 __init__.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

# vendor: container 内由 Dockerfile `pip install -e ./vendor/sam3` 提供; 本地测试通过
# pyproject.toml 的 pythonpath 注入. 显式把 vendor 根加进 sys.path 兜底.
_VENDOR_ROOT = "/app/vendor/sam3"
if os.path.isdir(_VENDOR_ROOT) and _VENDOR_ROOT not in sys.path:
    sys.path.insert(0, _VENDOR_ROOT)

import numpy as np
import torch
from PIL import Image

from aap_backend_runtime import (
    DeviceUnavailableError,
    free_gpu_memory,
    is_device_error,
    require_gpu_device,
)
from aap_protocol_v2 import (
    CocoRlePayload,
    MAX_SCRIBBLE_RASTERIZED_PIXELS,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    decode_low_res_mask,
    encode_low_res_mask,
    native_mask_candidate_id,
)
from embedding_cache import CacheEntry, EmbeddingCache
from mask_utils import (
    MultiPolygonRing,
    PromptAdapterError,
    encode_coco_rle,
    mask_prompt_to_low_res_logits,
    mask_to_multi_polygon,
    mask_to_preview_polygon,
    scribbles_to_point_prompts,
)

logger = logging.getLogger(__name__)

# 与 grounded-sam2-backend 一致的默认 simplify tolerance (像素).
DEFAULT_SIMPLIFY_TOLERANCE = 1.0
VERTEX_COUNT_WARN_THRESHOLD = 200


def _resolve_mask_input(
    mask_input: str | None,
    mask_prompt: Mapping[str, Any] | None,
    *,
    width: int,
    height: int,
    low_res_size: tuple[int, int],
) -> np.ndarray | None:
    if mask_input is not None:
        decoded = decode_low_res_mask(mask_input)
        if decoded.shape[-2:] == low_res_size:
            return decoded
        low_res_height, low_res_width = low_res_size
        ys = np.minimum(
            (
                (np.arange(low_res_height) + 0.5) * decoded.shape[-2] / low_res_height
            ).astype(int),
            decoded.shape[-2] - 1,
        )
        xs = np.minimum(
            (
                (np.arange(low_res_width) + 0.5) * decoded.shape[-1] / low_res_width
            ).astype(int),
            decoded.shape[-1] - 1,
        )
        return decoded[:, ys][:, :, xs]
    if mask_prompt is not None:
        return mask_prompt_to_low_res_logits(
            mask_prompt,
            expected_size=(width, height),
            low_res_size=low_res_size,
        )
    return None


def _maybe_encode_low_res(
    low_res: np.ndarray | None,
    *,
    enable: bool,
) -> str | None:
    """Encode logits only when the single-candidate session is unambiguous."""

    if not enable or low_res is None or len(low_res) < 1:
        return None
    try:
        return encode_low_res_mask(low_res[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to encode mask_input_next: %s", exc)
        return None


CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "/app/checkpoints")
# 图像模型变体名: 当前加载 sam3.pt (facebook/sam3, 3.0) —— 官方 image+inst 路径所用权重。
# (sam3.1_multiplex 是视频模型, 与 image-inst 代码不兼容, 仅留作后续视频追踪, 见 _load_model。)
MODEL_VARIANT = "sam3"

# Sam3Processor 默认 confidence_threshold; per-request 由 context.score_threshold 覆盖.
DEFAULT_SCORE_THRESHOLD = float(os.getenv("SAM3_SCORE_THRESHOLD", "0.5"))
# Sam3Processor 默认推理分辨率 (vendor 默认值, 不暴露 env).
SAM3_RESOLUTION = 1008


class SAM3Predictor:
    """各 prompt (text / exemplar 多正负框 / point / interactive_box) 路由到 Sam3Processor;
    返回归一化 polygon/rect dict 列表."""

    def __init__(
        self,
        *,
        checkpoint_dir: str = CHECKPOINT_DIR,
        embedding_cache: EmbeddingCache | None = None,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    ) -> None:
        self.checkpoint_dir = checkpoint_dir
        self.device = require_gpu_device("cuda")
        self.embedding_cache = embedding_cache
        self.score_threshold = score_threshold

        self._model = self._load_model()
        self._mask_input_size = tuple(
            int(value)
            for value in self._model.inst_interactive_predictor.model.sam_prompt_encoder.mask_input_size
        )
        self._processor = self._build_processor()

    # ---------- 模型加载 ----------

    def _load_model(self):
        """加载 SAM 3 image model.

        启用 inst_interactivity, 解锁 SAM-style point / 单框单 mask 交互
        (model.predict_inst); 同一模型亦提供 PCS (text / exemplar)。

        checkpoint 用 sam3.pt (3.0) 而非 sam3.1_multiplex.pt: 后者是视频模型
        (config.architectures=["Sam3VideoModel"]), 其 inst 权重命名 (tracker.model.* /
        interactive_convs) 与 vendored 代码期望的 image-inst 结构 (inst_interactive_predictor.
        model.* / sam2_convs + convs.3) 不兼容, 会因 key 不匹配静默加载随机权重 → 噪声 mask。
        官方 sam3_for_sam1_task_example.ipynb 即用 sam3.pt 跑 inst。multiplex 保留供后续视频追踪。
        """
        # vendor: facebookresearch/sam3 commit 4cbac14
        from sam3 import build_sam3_image_model  # type: ignore[import-not-found]

        # 优先用本地 checkpoint (容器启动时由 download_checkpoints.py 拉到 /app/checkpoints),
        # fallback 走 vendor 内置 hf_hub_download (`load_from_HF=True`, 默认即 sam3.pt).
        ckpt_path: str | None = None
        candidate = os.path.join(self.checkpoint_dir, "sam3.pt")
        if os.path.isfile(candidate):
            ckpt_path = candidate
            logger.info("using local checkpoint: %s", ckpt_path)

        try:
            return build_sam3_image_model(
                checkpoint_path=ckpt_path,
                load_from_HF=(ckpt_path is None),
                device=self.device,
                enable_segmentation=True,
                enable_inst_interactivity=True,  # 解锁 point / interactive_box
                eval_mode=True,
            )
        except Exception as exc:  # noqa: BLE001
            if is_device_error(exc):
                free_gpu_memory()
                raise DeviceUnavailableError(
                    "SAM3 image model requires a healthy CUDA device; CPU fallback is not supported"
                ) from exc
            raise

    def _build_processor(self):
        from sam3.model.sam3_image_processor import Sam3Processor  # type: ignore[import-not-found]

        try:
            return Sam3Processor(
                self._model,
                resolution=SAM3_RESOLUTION,
                device=self.device,
                confidence_threshold=self.score_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            if is_device_error(exc):
                self._model = None
                free_gpu_memory()
                raise DeviceUnavailableError(
                    "SAM3 image processor requires a healthy CUDA device; CPU fallback is not supported"
                ) from exc
            raise

    # ---------- 缓存辅助 ----------

    def _prime_state(
        self, image: Image.Image | None, cache_key: str | None
    ) -> tuple[dict, int, int, bool]:
        """获取一个干净 state dict (含 backbone_out + 原图尺寸). 命中 cache 跳过 set_image."""
        if cache_key and self.embedding_cache is not None:
            entry = self.embedding_cache.get(cache_key)
            if entry is not None:
                # 复用缓存的 backbone_out (内含 GPU 张量, 同 device, 不需拷贝).
                state = {
                    "backbone_out": dict(
                        entry.features
                    ),  # shallow copy: 外层 dict 隔离, 内层张量共享
                    "original_height": entry.orig_hw[0],
                    "original_width": entry.orig_hw[1],
                }
                return state, entry.wh[0], entry.wh[1], True

        if image is None:
            raise ValueError("image is required when cache miss")
        # set_image 内部会 normalize + resize + backbone.forward_image, 写入 state.
        state = self._processor.set_image(image)
        w, h = image.size
        if cache_key and self.embedding_cache is not None:
            # 缓存的是干净 backbone_out (此时还没跑过任何 prompt, 没有 language_features 污染).
            self.embedding_cache.put(
                cache_key,
                CacheEntry(
                    features=dict(state["backbone_out"]),  # shallow copy
                    orig_hw=(state["original_height"], state["original_width"]),
                    is_batch=False,
                    wh=(w, h),
                ),
            )
        return state, w, h, False

    def _apply_score_threshold(self, score_threshold: float | None) -> None:
        """per-request 阈值覆盖. 单 worker 串行执行下安全; 多 worker 需重新设计."""
        eff = (
            self.score_threshold if score_threshold is None else float(score_threshold)
        )
        self._processor.confidence_threshold = eff

    # ---------- 公开 prompt 接口 ----------

    def predict_text(
        self,
        image: Image.Image | None,
        text: str,
        *,
        output: str = "mask",
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        score_threshold: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """SAM 3 PCS text prompt 单模型一步出 mask.
        - "box":  跳过 mask → polygon 简化, 返回 rectanglelabels
        - "mask": 默认; mask → polygon, 返回 polygonlabels
        - "both": 同 instance 配对返回 [rect, poly]
        """
        self._apply_score_threshold(score_threshold)
        # SAM3.1 multiplex ckpt 部分权重 (vision_backbone.convs.3.*) 缺失, 默认 init 为 fp32,
        # 其余权重以 bf16 加载 → 不包 autocast 会 dtype 冲突. vendor 也是这样用 (见 examples/).
        with torch.autocast(
            self.device, dtype=torch.bfloat16, enabled=(self.device == "cuda")
        ):
            state, w, h, hit = self._prime_state(image, cache_key)
            self._processor.reset_all_prompts(state)
            state = self._processor.set_text_prompt(text.strip(), state)
            boxes, masks, scores = self._extract_outputs(state)
        # cleanup: reset 让 backbone_out 回到干净态, 下次缓存命中可用.
        self._processor.reset_all_prompts(state)

        if masks is None or len(masks) == 0:
            logger.info("SAM 3 returned 0 instances for text prompt")
            return [], hit

        return self._build_results(
            boxes,
            masks,
            scores,
            w,
            h,
            label=text.strip(),
            output=output,
            simplify_tolerance=simplify_tolerance,
            prompt_name="text",
        ), hit

    def predict_bbox(
        self,
        image: Image.Image | None,
        bbox: list[float],
        *,
        output: str = "mask",
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        score_threshold: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """SAM 3 image API 中 bbox prompt 与 exemplar 是同一调用 (add_geometric_prompt),
        语义都是「找全图与 box 内对象相似的所有实例」. 没有 SAM-2-style 的「这个 box 内部出一个 mask」.
        用户期待单框单 mask 的场景请走 grounded-sam2-backend.

        内部转 predict_exemplars 单元素正框 (兼容薄封装).
        """
        return self.predict_exemplars(
            image,
            [{"bbox": bbox, "label": True}],
            output=output,
            cache_key=cache_key,
            simplify_tolerance=simplify_tolerance,
            score_threshold=score_threshold,
        )

    def predict_exemplar(
        self,
        image: Image.Image | None,
        exemplar_bbox: list[float],
        *,
        output: str = "mask",
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        score_threshold: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """单框 exemplar; 内部转 predict_exemplars 单元素正框 (兼容薄封装)."""
        return self.predict_exemplars(
            image,
            [{"bbox": exemplar_bbox, "label": True}],
            output=output,
            cache_key=cache_key,
            simplify_tolerance=simplify_tolerance,
            score_threshold=score_threshold,
        )

    def predict_exemplars(
        self,
        image: Image.Image | None,
        exemplars: list[dict[str, Any]],
        *,
        text: str | None = None,
        output: str = "mask",
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        score_threshold: float | None = None,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """PCS 多正负框 (+ 可选 text) 迭代 refinement (无状态: 每请求重发全量).

        - exemplars: [{bbox:[x1,y1,x2,y2] 归一化 xyxy, label:bool}, ...]; True=正框(扩召回) /
          False=负框(排误检). 顺序累加经 add_geometric_prompt (append_boxes 非覆盖).
        - text: 非空时先 set_text_prompt, 再叠几何框 (text 概念 + 视觉示例组合). 缺省纯几何.
        - score_threshold: per-request 阈值重过滤; backbone 缓存命中下只重跑 grounding head.
        - output: box/mask/both 透传 _build_results (三分支已就绪).

        无 label 的几何 prompt 用 "object" 占位; workbench 按当前 active label 重写.
        """
        self._apply_score_threshold(score_threshold)
        trimmed_text = (text or "").strip()
        with torch.autocast(
            self.device, dtype=torch.bfloat16, enabled=(self.device == "cuda")
        ):
            state, w, h, hit = self._prime_state(image, cache_key)
            self._processor.reset_all_prompts(state)

            if trimmed_text:
                state = self._processor.set_text_prompt(trimmed_text, state)

            for ex in exemplars:
                x1, y1, x2, y2 = ex["bbox"]
                cx = (x1 + x2) / 2.0
                cy = (y1 + y2) / 2.0
                bw = x2 - x1
                bh = y2 - y1
                label = bool(ex.get("label", True))
                # 协议归一化 xyxy → 归一化 cxcywh; True 正框 / False 负框 (vendor add_geometric_prompt).
                state = self._processor.add_geometric_prompt(
                    [cx, cy, bw, bh], label, state
                )

            boxes, masks, scores = self._extract_outputs(state)
        self._processor.reset_all_prompts(state)

        if masks is None or len(masks) == 0:
            logger.info(
                "SAM 3 returned 0 instances for exemplar prompt; exemplars=%d",
                len(exemplars),
            )
            return [], hit

        label = trimmed_text or "object"
        return self._build_results(
            boxes,
            masks,
            scores,
            w,
            h,
            label=label,
            output=output,
            simplify_tolerance=simplify_tolerance,
            prompt_name="exemplar",
            output_geometry=output_geometry,
            prompt_revision=prompt_revision,
        ), hit

    def predict_interactive(
        self,
        image: Image.Image | None,
        *,
        points: list[list[float]] | None = None,
        labels: list[int] | None = None,
        box: list[float] | None = None,
        scribbles: Sequence[Mapping[str, Any]] | None = None,
        multimask_output: bool = False,
        mask_input: str | None = None,
        mask_prompt: Mapping[str, Any] | None = None,
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """SAM-style 单实例点/框交互 (与 grounded-sam2 对齐).

        走 inst_interactive_predictor (model.predict_inst), 复用同一 backbone_out 缓存
        (开 inst 后 set_image 一次同产 PCS + sam2 特征). 与 PCS (text/exemplar) 语义不同:
        这里是「点/框精修出单实例 mask」, 不是「找全图相似」.

        - points: 归一化 [[x,y],...]; labels: 1=正点 / 0=负点 (累加由前端重发全量点).
        - box:    归一化 [x1,y1,x2,y2] 单框单 mask.
        - multimask_output=True: 单点歧义时返回 3 候选 (按 iou 降序), 前端 top-1 + 切换.
        - mask_input: 上一轮 256×256 low-res logits (base64) 回灌, 多点精修提升
          边界稳定性。返回三元组第 3 项 mask_input_next 携带本轮 low-res 供下一次回传,
          仅 points 精修且 multimask_output=False 时返回 (多候选 index 歧义 / 框单发不回灌)。

        返回 (polygonlabels, cache_hit, mask_input_next); geometric 无自然 label, 用
        "object" 占位, workbench 按 active label 重写.
        """
        with torch.autocast(
            self.device, dtype=torch.bfloat16, enabled=(self.device == "cuda")
        ):
            state, w, h, hit = self._prime_state(image, cache_key)

            kwargs: dict[str, Any] = {"multimask_output": multimask_output}
            if scribbles is not None:
                if points or box is not None:
                    raise PromptAdapterError(
                        "points, box, and scribbles are mutually exclusive"
                    )
                points, labels = scribbles_to_point_prompts(
                    scribbles,
                    image_size=(w, h),
                    max_rasterized_pixels=MAX_SCRIBBLE_RASTERIZED_PIXELS,
                )
            if points:
                kwargs["point_coords"] = np.array(
                    [[p[0] * w, p[1] * h] for p in points], dtype=np.float32
                )
                kwargs["point_labels"] = np.array(
                    labels or [1] * len(points), dtype=np.int32
                )
            if box is not None:
                x1, y1, x2, y2 = box
                kwargs["box"] = np.array(
                    [x1 * w, y1 * h, x2 * w, y2 * h], dtype=np.float32
                )
            dense_prompt = _resolve_mask_input(
                mask_input,
                mask_prompt,
                width=w,
                height=h,
                low_res_size=self._mask_input_size,
            )
            if dense_prompt is not None:
                kwargs["mask_input"] = dense_prompt

            # predict_inst 复用 state["backbone_out"]["sam2_backbone_out"], 不重跑 backbone.
            # 返回 (masks CxHxW, iou C, low_res Cx256x256); masks 已 threshold 成 0/1 float.
            masks, ious, low_res = self._model.predict_inst(state, **kwargs)

        results = self._build_interactive_results(
            masks,
            ious,
            w,
            h,
            simplify_tolerance=simplify_tolerance,
            output_geometry=output_geometry,
            prompt_revision=prompt_revision,
        )
        return (
            results,
            hit,
            (
                _maybe_encode_low_res(
                    low_res,
                    enable=bool(points or dense_prompt is not None)
                    and not multimask_output,
                )
                if results
                else None
            ),
        )

    def _build_interactive_results(
        self,
        masks: np.ndarray,
        ious: np.ndarray,
        w: int,
        h: int,
        *,
        simplify_tolerance: float | None,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> list[dict[str, Any]]:
        """inst 输出 (CxHxW masks + C ious) → polygonlabels 列表 (按 iou 降序)."""
        if masks is None or len(masks) == 0:
            return []
        eff_tol = (
            DEFAULT_SIMPLIFY_TOLERANCE
            if simplify_tolerance is None
            else float(simplify_tolerance)
        )
        order = np.argsort(-np.asarray(ious), kind="stable")  # iou 降序; 同分保留原顺序
        results: list[dict[str, Any]] = []
        for i in order:
            mask = np.asarray(masks[i])
            binary = mask > 0
            if binary.shape != (h, w):
                raise ValueError(
                    f"mask shape must be {(h, w)}, got {tuple(binary.shape)}"
                )
            if not bool(binary.any()):
                continue
            if output_geometry == "mask":
                if not prompt_revision:
                    raise ValueError(
                        "prompt_revision is required for native mask output"
                    )
                preview_points = mask_to_preview_polygon(
                    binary,
                    tolerance=eff_tol,
                    normalize_to=(w, h),
                )
                rle = CocoRlePayload.model_validate(
                    encode_coco_rle(binary.reshape(-1), w, h)
                )
                candidate_index = len(results)
                candidate = NativeMaskCandidate(
                    value=NativeMaskCandidateValue(
                        rle=rle,
                        masklabels=["object"],
                        preview={"points": preview_points},
                    ),
                    score=float(ious[i]),
                    candidate_id=native_mask_candidate_id(
                        rle,
                        prompt_revision=prompt_revision,
                        candidate_index=candidate_index,
                    ),
                )
                results.append(candidate.model_dump(mode="json"))
                continue
            rings = mask_to_multi_polygon(
                mask.astype(np.uint8), tolerance=eff_tol, normalize_to=(w, h)
            )
            if not rings:
                continue
            self._maybe_warn_vertex_count(
                rings, eff_tol, int(mask.sum()), prompt="interactive"
            )
            results.append(
                self._rings_to_polygon_label(rings, "object", float(ious[i]))
            )
        return results

    # ---------- 输出处理 ----------

    @staticmethod
    def _extract_outputs(state: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """把 state 中的 GPU 张量提到 numpy."""
        boxes_t = state.get("boxes")
        masks_t = state.get("masks")
        scores_t = state.get("scores")
        if masks_t is None or len(masks_t) == 0:
            return (
                np.empty((0, 4), dtype=np.float32),
                np.empty((0,), dtype=bool),
                np.empty((0,), dtype=np.float32),
            )
        # autocast(bf16) 下 boxes/scores 是 bf16, numpy 不支持 BFloat16 → 先 .float().
        boxes = boxes_t.detach().float().cpu().numpy()
        # masks shape: (N, 1, H, W) bool → (N, H, W); bool 张量不受 autocast 影响.
        if masks_t.ndim == 4:
            masks = masks_t[:, 0].detach().cpu().numpy()
        else:
            masks = masks_t.detach().cpu().numpy()
        scores = (
            scores_t.detach().float().cpu().numpy()
            if scores_t is not None
            else np.zeros(len(boxes))
        )
        return boxes, masks, scores

    def _build_results(
        self,
        boxes: np.ndarray,
        masks: np.ndarray,
        scores: np.ndarray,
        w: int,
        h: int,
        *,
        label: str,
        output: str,
        simplify_tolerance: float | None,
        prompt_name: str,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> list[dict[str, Any]]:
        eff_tol = (
            DEFAULT_SIMPLIFY_TOLERANCE
            if simplify_tolerance is None
            else float(simplify_tolerance)
        )
        results: list[dict[str, Any]] = []

        if output == "box":
            for i in range(len(boxes)):
                results.append(
                    self._box_to_rect_label(boxes[i], w, h, label, float(scores[i]))
                )
            return results

        for i, mask in enumerate(masks):
            score = float(scores[i])
            binary = np.asarray(mask) > 0
            if binary.shape != (h, w):
                raise ValueError(
                    f"mask shape must be {(h, w)}, got {tuple(binary.shape)}"
                )
            if not bool(binary.any()):
                continue
            if output_geometry == "mask":
                if not prompt_revision:
                    raise ValueError(
                        "prompt_revision is required for native mask output"
                    )
                preview_points = mask_to_preview_polygon(
                    binary,
                    tolerance=eff_tol,
                    normalize_to=(w, h),
                )
                rle = CocoRlePayload.model_validate(
                    encode_coco_rle(binary.reshape(-1), w, h)
                )
                if output == "both":
                    results.append(
                        self._box_to_rect_label(boxes[i], w, h, label, score)
                    )
                candidate_index = len(results)
                candidate = NativeMaskCandidate(
                    value=NativeMaskCandidateValue(
                        rle=rle,
                        masklabels=[label],
                        preview={"points": preview_points},
                    ),
                    score=score,
                    candidate_id=native_mask_candidate_id(
                        rle,
                        prompt_revision=prompt_revision,
                        candidate_index=candidate_index,
                    ),
                )
                results.append(candidate.model_dump(mode="json"))
                continue
            rings = mask_to_multi_polygon(
                mask.astype(np.uint8), tolerance=eff_tol, normalize_to=(w, h)
            )
            if not rings:
                continue
            self._maybe_warn_vertex_count(
                rings, eff_tol, int(mask.sum()), prompt=prompt_name
            )
            if output == "both":
                results.append(self._box_to_rect_label(boxes[i], w, h, label, score))
                results.append(self._rings_to_polygon_label(rings, label, score))
            else:
                results.append(self._rings_to_polygon_label(rings, label, score))
        return results

    @staticmethod
    def _box_to_rect_label(
        box_px: np.ndarray | list[float],
        w: int,
        h: int,
        label: str,
        score: float,
    ) -> dict[str, Any]:
        """像素 xyxy → 归一化 [0,1] 的 rectanglelabels 字典 (与 grounded-sam2 同源)."""
        x1, y1, x2, y2 = (
            float(box_px[0]),
            float(box_px[1]),
            float(box_px[2]),
            float(box_px[3]),
        )
        x1, x2 = (max(0.0, min(float(w), x)) for x in (x1, x2))
        y1, y2 = (max(0.0, min(float(h), y)) for y in (y1, y2))
        return {
            "type": "rectanglelabels",
            "value": {
                "x": x1 / w,
                "y": y1 / h,
                "width": max(0.0, (x2 - x1) / w),
                "height": max(0.0, (y2 - y1) / h),
                "rectanglelabels": [label],
            },
            "score": score,
        }

    @staticmethod
    def _rings_to_polygon_label(
        rings: list[MultiPolygonRing], label: str, score: float
    ) -> dict[str, Any]:
        """与 grounded-sam2 一致的 polygonlabels 智能字面。"""
        if len(rings) == 1 and not rings[0]["holes"]:
            return {
                "type": "polygonlabels",
                "value": {
                    "points": rings[0]["exterior"],
                    "polygonlabels": [label],
                },
                "score": score,
            }
        if len(rings) == 1:
            return {
                "type": "polygonlabels",
                "value": {
                    "points": rings[0]["exterior"],
                    "holes": rings[0]["holes"],
                    "polygonlabels": [label],
                },
                "score": score,
            }
        return {
            "type": "polygonlabels",
            "value": {
                "polygons": [
                    {"points": r["exterior"], "holes": r["holes"]}
                    if r["holes"]
                    else {"points": r["exterior"]}
                    for r in rings
                ],
                "polygonlabels": [label],
            },
            "score": score,
        }

    @staticmethod
    def _maybe_warn_vertex_count(
        rings: list[MultiPolygonRing], eff_tol: float, mask_area: int, *, prompt: str
    ) -> None:
        total = sum(len(r["exterior"]) + sum(len(h) for h in r["holes"]) for r in rings)
        if total > VERTEX_COUNT_WARN_THRESHOLD:
            logger.warning(
                "polygon vertex count %d > %d (tolerance=%.2f, mask area=%d, prompt=%s, rings=%d)",
                total,
                VERTEX_COUNT_WARN_THRESHOLD,
                eff_tol,
                mask_area,
                prompt,
                len(rings),
            )
