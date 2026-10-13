"""Grounded-SAM-2 推理封装.

vendor 形态: vendor/grounded-sam-2/ 下放上游官方仓库副本 (固定 commit, 通过 scripts/sync_vendor.sh 同步).
本模块只对 vendor 内的 SAM 2.1 image_predictor + GroundingDINO inference utilities 做一层 prompt 适配,
返回平台协议要求的 polygonlabels / rectanglelabels 字典数组.

mask → polygon 简化策略 (实现位于 apps/_shared/mask_utils, 与 sam3-backend 共用):
    cv2.findContours(RETR_CCOMP, CHAIN_APPROX_NONE)  # CCOMP 抓内外环
    → 各连通域外环 / hole 配对（hierarchy parent 索引归属）
    → shapely.simplify(tolerance=DEFAULT_SIMPLIFY_TOLERANCE, preserve_topology=True)
    → 像素坐标归一化到 [0,1] (6 位精度对齐协议)

输出 shape 智能选择:
- 单连通域无 hole → polygonlabels {points}                 // 保持既有字面, 老前端 / 老 fixture 不破
- 单连通域带 hole → polygonlabels {points, holes}          // 新增, 前端 PolygonGeometry.holes 渲染镂空
- 多连通域       → polygonlabels {polygons:[{points, holes}]}  // 新增 multi_polygon, 前端 MultiPolygonGeometry

tolerance 默认值见 DEFAULT_SIMPLIFY_TOLERANCE; 单次请求可由 Context.simplify_tolerance 覆盖.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

# vendor 内 grounding_dino/groundingdino/util/inference.py 用 `import grounding_dino.groundingdino...`
# 把 vendor 根当顶层包名 (上游 demo 依赖 cwd 在 sys.path 隐式提供). 我们显式注入.
_VENDOR_ROOT = "/app/vendor/grounded-sam-2"
if os.path.isdir(_VENDOR_ROOT) and _VENDOR_ROOT not in sys.path:
    sys.path.insert(0, _VENDOR_ROOT)

import numpy as np
import torch
from PIL import Image

from aap_backend_runtime import (
    effective_device,
    free_gpu_memory,
    is_device_error,
    latch_cpu,
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


def _resolve_mask_input(
    mask_input: str | None,
    mask_prompt: Mapping[str, Any] | None,
    *,
    width: int,
    height: int,
) -> np.ndarray | None:
    """Resolve a verified session token first, otherwise adapt the source RLE."""

    if mask_input is not None:
        return decode_low_res_mask(mask_input)
    if mask_prompt is not None:
        return mask_prompt_to_low_res_logits(
            mask_prompt,
            expected_size=(width, height),
        )
    return None


def _maybe_encode_low_res(low_res: np.ndarray | None, *, enable: bool) -> str | None:
    """仅单 mask 精修阶段回灌 low-res logits (多候选 index 歧义不回灌)。"""
    if not enable or low_res is None or len(low_res) < 1:
        return None
    try:
        return encode_low_res_mask(low_res[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to encode mask_input_next: %s", exc)
        return None


# 默认 tolerance (像素). docs/research/13-simplify-tolerance-eval.md
# 跑出来的合理默认 — 50 张 SAM mask 样本 95% 满足 IoU≥0.95, 顶点数中位 ~70.
# 单次请求可由 Context.simplify_tolerance 覆盖.
DEFAULT_SIMPLIFY_TOLERANCE = 1.0

# 顶点数 > 该阈值时 logger.warning, 提示 simplify 没收敛到合理形态 (异常长 contour /
# tolerance 过低). 不影响返回正确性, 仅是运维信号.
VERTEX_COUNT_WARN_THRESHOLD = 200

CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "/app/checkpoints")
# config 路径走 hydra `pkg://sam2` search path, 必须带 configs/sam2.1/ 前缀
# (与 vendor 内 grounded_sam2_local_demo.py:20 一致).
SAM2_CONFIGS = {
    "tiny": ("configs/sam2.1/sam2.1_hiera_t.yaml", "sam2.1_hiera_tiny.pt"),
    "small": ("configs/sam2.1/sam2.1_hiera_s.yaml", "sam2.1_hiera_small.pt"),
    "base_plus": ("configs/sam2.1/sam2.1_hiera_b+.yaml", "sam2.1_hiera_base_plus.pt"),
    "large": ("configs/sam2.1/sam2.1_hiera_l.yaml", "sam2.1_hiera_large.pt"),
}
DINO_CONFIGS = {
    "T": ("GroundingDINO_SwinT_OGC.py", "groundingdino_swint_ogc.pth"),
    # vendor 里 SwinB 的 config 实际命名为 GroundingDINO_SwinB_cfg.py (不是 _cogcoor.py);
    # checkpoint 仍是 groundingdino_swinb_cogcoor.pth. 启用变体热切换前 DINO 只锁 T 变体,
    # 此 config 文件名错配从未被触发.
    "B": ("GroundingDINO_SwinB_cfg.py", "groundingdino_swinb_cogcoor.pth"),
}


class GroundedSAM2Predictor:
    """三种 prompt 路由到 SAM 2.1 / GroundingDINO; 返回归一化 polygon dict 列表."""

    def __init__(
        self,
        sam_variant: str = "tiny",
        dino_variant: str = "T",
        box_threshold: float = 0.35,
        text_threshold: float = 0.25,
        embedding_cache: EmbeddingCache | None = None,
    ) -> None:
        self.sam_variant = sam_variant
        self.dino_variant = dino_variant
        self.box_threshold = box_threshold
        self.text_threshold = text_threshold
        self.device = effective_device("cuda")
        self.embedding_cache = embedding_cache
        self.cleanup_uncertain = False

        self._sam_predictor, self._dino_model = self._load_models()

    # ---------- 模型加载 ----------

    def _build_sam(self, device: str):
        from sam2.build_sam import build_sam2  # type: ignore[import-not-found]
        from sam2.sam2_image_predictor import SAM2ImagePredictor  # type: ignore[import-not-found]

        cfg_name, ckpt_name = SAM2_CONFIGS[self.sam_variant]
        ckpt_path = os.path.join(CHECKPOINT_DIR, ckpt_name)
        sam2_model = build_sam2(cfg_name, ckpt_path, device=device)
        return SAM2ImagePredictor(sam2_model)

    def _build_dino(self, device: str):
        # vendor/grounded-sam-2/ 仓库内 GroundingDINO 通过 grounding_dino 子目录暴露 inference utils.
        from groundingdino.util.inference import load_model  # type: ignore[import-not-found]

        cfg_name, ckpt_name = DINO_CONFIGS[self.dino_variant]
        # 上游 demo 把 config 放到 vendor/grounded-sam-2/grounding_dino/groundingdino/config/ 下;
        # 实际加载路径以 vendor 同步后的目录为准, 通过环境变量可覆盖.
        cfg_path = os.getenv(
            "DINO_CONFIG_PATH",
            f"/app/vendor/grounded-sam-2/grounding_dino/groundingdino/config/{cfg_name}",
        )
        ckpt_path = os.path.join(CHECKPOINT_DIR, ckpt_name)
        return load_model(cfg_path, ckpt_path, device=device)

    def _load_models(self):
        """以 SAM + DINO 整体为一次设备提交，避免混合 GPU/CPU predictor。"""
        if self.device == "cpu":
            return self._build_sam("cpu"), self._build_dino("cpu")

        gpu_sam = None
        try:
            gpu_sam = self._build_sam(self.device)
            gpu_dino = self._build_dino(self.device)
            return gpu_sam, gpu_dino
        except Exception as exc:  # noqa: BLE001
            if not is_device_error(exc):
                raise
            # A device build may allocate hidden CUDA state before raising.  Keep
            # residency Unknown until a later full-pool cleanup has completed.
            self.cleanup_uncertain = True
            # 不在 CPU replacement 完整成功前改写进程 latch；先丢弃可能
            # 已在 GPU 上构建的 SAM，再用同一设备重建两个组件。
            gpu_sam = None
            free_gpu_memory()
            try:
                cpu_sam = self._build_sam("cpu")
                cpu_dino = self._build_dino("cpu")
            except Exception as cpu_exc:  # noqa: BLE001
                raise cpu_exc from exc
            self.device = "cpu"
            latch_cpu(f"GPU model build failed; CPU replacement committed: {exc}")
            return cpu_sam, cpu_dino

    # ---------- SAM 内部状态 snapshot / restore ----------
    #
    # SAM2ImagePredictor.set_image() 把 image embedding 写到几个实例属性上;
    # 缓存命中时把这些字段写回, 等价于 set_image() 但跳过编码器.
    # 字段名跟随 vendor IDEA-Research/Grounded-SAM-2 (commit b7a9c29) 内的
    # sam2/sam2_image_predictor.py; sync_vendor.sh 升级 commit 时务必跑 5-clicks 集成验收.

    def _snapshot_sam(self, w: int, h: int) -> CacheEntry:
        sp = self._sam_predictor
        orig_hw = sp._orig_hw[0] if isinstance(sp._orig_hw, list) else sp._orig_hw
        return CacheEntry(
            features=sp._features,
            orig_hw=tuple(orig_hw),  # type: ignore[arg-type]
            is_batch=getattr(sp, "_is_batch", False),
            wh=(w, h),
        )

    def _restore_sam(self, entry: CacheEntry) -> None:
        sp = self._sam_predictor
        sp._features = entry.features
        sp._orig_hw = [tuple(entry.orig_hw)]
        sp._is_image_set = True
        if hasattr(sp, "_is_batch"):
            sp._is_batch = entry.is_batch

    # ---------- 公开 prompt 接口 ----------

    def predict_point(
        self,
        image: Image.Image | None,
        points: list[list[float]],
        labels: list[int],
        *,
        scribbles: Sequence[Mapping[str, Any]] | None = None,
        multimask_output: bool = False,
        mask_input: str | None = None,
        mask_prompt: Mapping[str, Any] | None = None,
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """返回 (results, cache_hit, mask_input_next). image=None 仅在 cache_key 命中时可省.

        正/负点累加由前端重发全量点 (无状态); multimask_output=True 单点歧义出
        3 候选 (按 iou 降序, 前端 top-1 + 切换).
        mask_input (上一轮 256×256 low-res logits, base64) 回灌; multimask=False
        的单 mask 精修阶段把本轮 low-res 编码回 mask_input_next 供下一次回传。
        """
        w, h, hit = self._prime_sam(image, cache_key)
        if scribbles is not None:
            if points:
                raise PromptAdapterError("points and scribbles are mutually exclusive")
            points, labels = scribbles_to_point_prompts(
                scribbles,
                image_size=(w, h),
                max_rasterized_pixels=MAX_SCRIBBLE_RASTERIZED_PIXELS,
            )
        px = np.array([[p[0] * w, p[1] * h] for p in points], dtype=np.float32)
        lab = np.array(labels, dtype=np.int32)
        kwargs: dict[str, Any] = {}
        dense_prompt = _resolve_mask_input(
            mask_input,
            mask_prompt,
            width=w,
            height=h,
        )
        if dense_prompt is not None:
            kwargs["mask_input"] = dense_prompt
        masks, scores, low_res = self._sam_predictor.predict(
            point_coords=px,
            point_labels=lab,
            multimask_output=multimask_output,
            **kwargs,
        )
        results = self._masks_to_results(
            masks,
            scores,
            w,
            h,
            simplify_tolerance,
            sort_by_score=multimask_output,
            output_geometry=output_geometry,
            prompt_revision=prompt_revision,
        )
        return (
            results,
            hit,
            _maybe_encode_low_res(
                low_res,
                enable=not multimask_output and bool(results),
            ),
        )

    def predict_bbox(
        self,
        image: Image.Image | None,
        bbox: list[float],
        *,
        multimask_output: bool = False,
        mask_input: str | None = None,
        mask_prompt: Mapping[str, Any] | None = None,
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """interactive_box 单框单 mask (协议 type=interactive_box 路由到此).
        multimask_output=True 出 3 候选 (按 iou 降序).

        框是单发 prompt (前端不链式精修), 第 3 项 mask_input_next 恒 None。
        """
        w, h, hit = self._prime_sam(image, cache_key)
        x1, y1, x2, y2 = bbox
        box_px = np.array([x1 * w, y1 * h, x2 * w, y2 * h], dtype=np.float32)
        kwargs: dict[str, Any] = {}
        dense_prompt = _resolve_mask_input(
            mask_input,
            mask_prompt,
            width=w,
            height=h,
        )
        if dense_prompt is not None:
            kwargs["mask_input"] = dense_prompt
        masks, scores, low_res = self._sam_predictor.predict(
            point_coords=None,
            point_labels=None,
            box=box_px[None, :],
            multimask_output=multimask_output,
            **kwargs,
        )
        results = self._masks_to_results(
            masks,
            scores,
            w,
            h,
            simplify_tolerance,
            sort_by_score=multimask_output,
            output_geometry=output_geometry,
            prompt_revision=prompt_revision,
        )
        return (
            results,
            hit,
            _maybe_encode_low_res(
                low_res,
                enable=(
                    dense_prompt is not None and not multimask_output and bool(results)
                ),
            ),
        )

    def predict_mask(
        self,
        image: Image.Image | None,
        mask_prompt: Mapping[str, Any],
        *,
        mask_input: str | None = None,
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """Refine an authorized Mask seed without an additional sparse prompt."""

        w, h, hit = self._prime_sam(image, cache_key)
        dense_prompt = _resolve_mask_input(
            mask_input,
            mask_prompt,
            width=w,
            height=h,
        )
        assert dense_prompt is not None
        masks, scores, low_res = self._sam_predictor.predict(
            point_coords=None,
            point_labels=None,
            mask_input=dense_prompt,
            multimask_output=False,
        )
        results = self._masks_to_results(
            masks,
            scores,
            w,
            h,
            simplify_tolerance,
            sort_by_score=False,
            output_geometry=output_geometry,
            prompt_revision=prompt_revision,
        )
        return (
            results,
            hit,
            _maybe_encode_low_res(
                low_res,
                enable=bool(results),
            ),
        )

    def predict_boxes(
        self,
        image: Image.Image | None,
        boxes: list[tuple[list[float], int]],
        *,
        cache_key: str | None = None,
        simplify_tolerance: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """框→mask 批量分割原子: 一图一次 set_image, N 框共享 image embedding。

        用于多阶段编排的下游 box-seg 阶段——上游检测器已产出 bbox, 这里对每框跑轻量
        SAM decoder 出 polygon。encoder(set_image)成本只付一次, 远优于逐 crop N 次编码。

        Args:
            image: 全图 PIL.Image; cache 命中时可为 None(走 restore_sam)。
            boxes: ``[(bbox, parent_box_idx), ...]``; bbox 为原图归一化 [x1,y1,x2,y2]。
            cache_key: image embedding 缓存键(同图同变体复用)。
            simplify_tolerance: shapely 简化容差(单请求级覆盖)。

        Returns:
            (协议 result 数组, embedding 命中标志)。每条 result 带 ``parent_box_idx``,
            供平台 merge 回对应父框。
        """
        w, h, hit = self._prime_sam(image, cache_key)
        out: list[dict[str, Any]] = []
        for bbox, parent_idx in boxes:
            x1, y1, x2, y2 = bbox
            box_px = np.array([x1 * w, y1 * h, x2 * w, y2 * h], dtype=np.float32)
            masks, scores, _ = self._sam_predictor.predict(
                point_coords=None,
                point_labels=None,
                box=box_px[None, :],
                multimask_output=False,
            )
            for entry in self._masks_to_results(
                masks, scores, w, h, simplify_tolerance
            ):
                entry["parent_box_idx"] = parent_idx
                out.append(entry)
        return out, hit

    def _prime_sam(
        self, image: Image.Image | None, cache_key: str | None
    ) -> tuple[int, int, bool]:
        """命中: restore state, 返回 (w, h, True). 未命中: set_image + put, 返回 (w, h, False).

        cache_key=None 时绕过缓存.
        """
        if cache_key and self.embedding_cache is not None:
            entry = self.embedding_cache.get(cache_key)
            if entry is not None:
                self._restore_sam(entry)
                return entry.wh[0], entry.wh[1], True
        if image is None:
            raise ValueError("image is required when cache miss")
        np_img, w, h = self._to_numpy(image)
        self._sam_predictor.set_image(np_img)
        if cache_key and self.embedding_cache is not None:
            self.embedding_cache.put(cache_key, self._snapshot_sam(w, h))
        return w, h, False

    def predict_text(
        self,
        image: Image.Image,
        text: str,
        *,
        output: str = "mask",
        cache_key: str | None = None,
        box_threshold: float | None = None,
        text_threshold: float | None = None,
        simplify_tolerance: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """output 三分支:
        - "box":  仅 DINO, 跳过 SAM image embedding + mask + 简化, 返回 rectanglelabels
        - "mask": 当前默认行为, DINO + SAM mask → polygon, 返回 polygonlabels
        - "both": 同 instance 配对返回 [rectangle, polygon] 两条 (前端按需消费)

        cache_hit 仅在 mask/both 路径有意义; box 路径恒为 False (不读不写 cache).
        """
        from groundingdino.util.inference import predict as dino_predict  # type: ignore[import-not-found]

        np_img, w, h = self._to_numpy(image)
        # GroundingDINO predict() 期望 caption 以 . 结尾的小写短语.
        caption = text.strip().lower()
        if not caption.endswith("."):
            caption = caption + "."

        image_tensor = self._dino_image_tensor(np_img)
        # 项目级阈值 override；缺省回退到 instance 默认值（来自 backend env）
        eff_box = self.box_threshold if box_threshold is None else float(box_threshold)
        eff_text = (
            self.text_threshold if text_threshold is None else float(text_threshold)
        )
        boxes, dino_logits, phrases = dino_predict(
            model=self._dino_model,
            image=image_tensor,
            caption=caption,
            box_threshold=eff_box,
            text_threshold=eff_text,
            device=self.device,
        )
        if boxes is None or len(boxes) == 0:
            logger.info("DINO returned 0 boxes for text prompt")
            return [], False

        # 归一化 cxcywh → 像素 xyxy
        boxes_xyxy = self._cxcywh_to_xyxy(boxes.cpu().numpy(), w, h)
        default_label = caption.rstrip(".")
        # DINO 每框的 sigmoid 后置信度 (与 box_threshold 同源, 真正的检测置信).
        # 之前丢弃 → box 模式硬编码 score=1.0, mask 模式用 SAM mask 质量分 (恒高).
        # 用户层面表现为「明明 DINO 卡到 0.15 才出框, 显示却是 100%」, 此处统一回填.
        dino_scores = (
            dino_logits.cpu().numpy().tolist()
            if hasattr(dino_logits, "cpu")
            else list(dino_logits)
        )

        def _dino_score(i: int) -> float:
            return float(dino_scores[i]) if i < len(dino_scores) else 0.0

        # box 模式: DINO 直出, 跳过 SAM 全部步骤, cache 不读不写
        if output == "box":
            results: list[dict[str, Any]] = []
            for i, box_px in enumerate(boxes_xyxy):
                label = phrases[i] if i < len(phrases) else default_label
                results.append(
                    self._box_to_rect_label(box_px, w, h, label, _dino_score(i))
                )
            return results, False

        # mask / both 共享 SAM image embedding + mask 推理路径.
        # 注意 text 路径 image 永远不为 None(DINO 要原图), 这里不会触发 ValueError.
        hit = False
        if cache_key and self.embedding_cache is not None:
            entry = self.embedding_cache.get(cache_key)
            if entry is not None:
                self._restore_sam(entry)
                hit = True
        if not hit:
            self._sam_predictor.set_image(np_img)
            if cache_key and self.embedding_cache is not None:
                self.embedding_cache.put(cache_key, self._snapshot_sam(w, h))
        masks, scores, _ = self._sam_predictor.predict(
            point_coords=None, point_labels=None, box=boxes_xyxy, multimask_output=False
        )
        # masks shape: (N, 1, H, W) 或 (N, H, W); 统一展平
        if masks.ndim == 4:
            masks = masks[:, 0]

        results = []
        eff_tol = (
            DEFAULT_SIMPLIFY_TOLERANCE
            if simplify_tolerance is None
            else float(simplify_tolerance)
        )
        for i, mask in enumerate(masks):
            # 用 DINO 检测置信度 (用户语义上的"模型对该目标的把握"), 而非 SAM mask 质量分.
            # SAM scores 仅反映"给定 box prompt 时 mask 切得有多好", 与 detection 强弱无关.
            score = _dino_score(i)
            label = phrases[i] if i < len(phrases) else default_label
            rings = mask_to_multi_polygon(mask, tolerance=eff_tol, normalize_to=(w, h))
            if not rings:
                continue
            self._maybe_warn_vertex_count(
                rings, eff_tol, int(mask.sum()), prompt="text"
            )
            if output == "both":
                # 配对返回: 同 instance 一对 rect + poly (前端按需选).
                results.append(
                    self._box_to_rect_label(boxes_xyxy[i], w, h, label, score)
                )
                results.append(self._rings_to_polygon_label(rings, label, score))
            else:  # mask
                results.append(self._rings_to_polygon_label(rings, label, score))
        return results, hit

    @staticmethod
    def _box_to_rect_label(
        box_px: np.ndarray | list[float],
        w: int,
        h: int,
        label: str,
        score: float,
    ) -> dict[str, Any]:
        """像素 xyxy → 归一化 [0,1] 的 rectanglelabels 字典 (与 polygonlabels 协议同源).

        x/y 是矩形左上, width/height 也都归一化; 与平台 BboxAnnotation 字段一致.
        """
        x1, y1, x2, y2 = (
            float(box_px[0]),
            float(box_px[1]),
            float(box_px[2]),
            float(box_px[3]),
        )
        return {
            "type": "rectanglelabels",
            "value": {
                "x": max(0.0, min(1.0, x1 / w)),
                "y": max(0.0, min(1.0, y1 / h)),
                "width": max(0.0, min(1.0, (x2 - x1) / w)),
                "height": max(0.0, min(1.0, (y2 - y1) / h)),
                "rectanglelabels": [label],
            },
            "score": score,
        }

    @staticmethod
    def _rings_to_polygon_label(
        rings: list[MultiPolygonRing], label: str, score: float
    ) -> dict[str, Any]:
        """mask_to_multi_polygon 输出 → LabelStudio polygonlabels shape.

        智能选择三种字面:
        - 单连通无 hole → {points, polygonlabels}                  (保持既有字面)
        - 单连通带 hole → {points, holes, polygonlabels}            (新, 老前端忽略 holes 字段)
        - 多连通       → {polygons:[{points,holes?},...], polygonlabels}  (新, 老前端忽略 polygons 字段)

        老前端遇到带 holes / polygons 的新字段会 fallback 到 points (老路径) 还是空, 取决于
        前端反序列化实现; 前端 transforms.ts 已适配这些字段, 老前端兼容靠"单连通无 hole
        时不写新字段"这条路径覆盖大多数 mask.
        """
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

    # ---------- 内部工具 ----------

    @staticmethod
    def _to_numpy(image: Image.Image) -> tuple[np.ndarray, int, int]:
        arr = np.array(image)  # RGB
        h, w = arr.shape[:2]
        return arr, w, h

    def _dino_image_tensor(self, np_img: np.ndarray) -> torch.Tensor:
        """GroundingDINO 期望 transform 后的 tensor (3,H,W). 复用 vendor 内 transforms."""
        import groundingdino.datasets.transforms as T  # type: ignore[import-not-found]

        transform = T.Compose(
            [
                T.RandomResize([800], max_size=1333),
                T.ToTensor(),
                T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )
        # 上游 transform 接受 (PIL.Image, target=None)
        pil = Image.fromarray(np_img)
        tensor, _ = transform(pil, None)
        return tensor

    @staticmethod
    def _cxcywh_to_xyxy(boxes: np.ndarray, w: int, h: int) -> np.ndarray:
        cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = (cx - bw / 2) * w
        y1 = (cy - bh / 2) * h
        x2 = (cx + bw / 2) * w
        y2 = (cy + bh / 2) * h
        return np.stack([x1, y1, x2, y2], axis=1).astype(np.float32)

    def _masks_to_results(
        self,
        masks: np.ndarray,
        scores: np.ndarray | None,
        w: int,
        h: int,
        simplify_tolerance: float | None = None,
        *,
        sort_by_score: bool = False,
        output_geometry: str = "polygon",
        prompt_revision: str | None = None,
    ) -> list[dict[str, Any]]:
        if masks.ndim == 4:
            masks = masks[:, 0]
        # multimask 候选按 score 降序, 保证 results[0]=top-1 (与 sam3 对齐);
        # 单 mask / predict_boxes 路径 (sort_by_score=False) 保留原顺序 (parent_box_idx 依赖).
        if sort_by_score and scores is not None and len(scores) > 1:
            order = np.argsort(-np.asarray(scores), kind="stable")
            masks = masks[order]
            scores = np.asarray(scores)[order]
        out: list[dict[str, Any]] = []
        eff_tol = (
            DEFAULT_SIMPLIFY_TOLERANCE
            if simplify_tolerance is None
            else float(simplify_tolerance)
        )
        for i, mask in enumerate(masks):
            score = float(scores[i]) if scores is not None and i < len(scores) else None
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
                candidate_index = len(out)
                candidate = NativeMaskCandidate(
                    value=NativeMaskCandidateValue(
                        rle=rle,
                        masklabels=["object"],
                        preview={"points": preview_points},
                    ),
                    score=score,
                    candidate_id=native_mask_candidate_id(
                        rle,
                        prompt_revision=prompt_revision,
                        candidate_index=candidate_index,
                    ),
                )
                out.append(candidate.model_dump(mode="json"))
                continue
            rings = mask_to_multi_polygon(mask, tolerance=eff_tol, normalize_to=(w, h))
            if not rings:
                continue
            self._maybe_warn_vertex_count(
                rings, eff_tol, int(mask.sum()), prompt="point/bbox"
            )
            entry = self._rings_to_polygon_label(rings, "object", score or 0.0)
            if score is None:
                entry.pop("score", None)
            out.append(entry)
        return out
