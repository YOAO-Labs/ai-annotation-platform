"""rapidocr-backend 推理器：RapidOCR 引擎池 + det/rec/e2e 三能力运行 → 协议 v2 result。

引擎按 ``ResolvedEngine.pool_key``（det+cls+rec 三件套路径）懒加载、LRU 限容。一次调用：
设 use_det/use_cls/use_rec 开关 → 走 RapidOCR 的 load→preprocess→run_ocr_steps→build_final_output。

cls 内化：rec/e2e 内部跑 cls 做 180° 校正，``attributes.orientation`` 取 cls 标签。
build_final_output 会按「空文本 + text_score 阈值」二次过滤 boxes/txts，因此
orientation 必须**在 build_final_output 之后**按 ``final.txts`` ↔ ``rec_res.txts`` 顺序
游标回填（详见 ``_align_orientations``）。

并发：池化的 ``RapidOCR`` 实例不为并发使用设计 —— ``update_params`` 改阈值 + 之后的
``run_ocr_steps`` 必须串行，否则后请求会覆盖前请求的 text_score/box_thresh/unclip_ratio。
动态引擎池以 borrower 防止 LRU 越过正在使用的引擎，并用每 entry 的
``asyncio.Lock`` 覆盖整段 update + run + build。

坐标：RapidOCR 出像素四点框 → 归一化 0-100 百分比的 polygonlabels（与其余 backend 同源）。
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
import time
from functools import partial
from typing import Any

import numpy as np
from aap_backend_runtime import is_device_error
from rapidocr import OCRVersion, RapidOCR

import catalog as catalog_mod
from catalog import RUNTIME_PARAM_DEFAULTS, ResolvedEngine
from engine_pool import EngineBuildArtifact, EnginePool

logger = logging.getLogger("rapidocr-backend.predictor")

POLY_LABEL = "text"  # OCR 文本框无类别，用通用占位 label。


def _session_provider_chain(session: Any) -> list[str] | None:
    """Return one ORT session's complete provider chain, or unknown."""

    if session is None:
        return None
    try:
        providers = session.get_providers()
    except Exception:  # noqa: BLE001
        return None
    if not providers:
        return None
    return [str(provider) for provider in providers]


def _soft_ort_cuda_use() -> bool:
    """Check compiled ORT capability without creating an unowned GPU session."""

    try:
        import onnxruntime  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    try:
        return (
            "CUDAExecutionProvider" in onnxruntime.get_available_providers()
            and onnxruntime.get_device() == "GPU"
        )
    except Exception:  # noqa: BLE001
        return False


def _is_ort_device_error(exc: BaseException) -> bool:
    """Whitelist ORT CUDA-provider initialization failures; never classify OOM."""

    if is_device_error(exc):
        return True
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}".lower())
        current = current.__cause__ or current.__context__
    message = " ".join(parts)
    if "outofmemory" in message or "out of memory" in message:
        return False
    provider_markers = (
        "failed to create cudaexecutionprovider",
        "cuda execution provider is not available",
        "cuda_error_no_device",
        "cuda_error_insufficient_driver",
        "libcudnn.so",
        "libcublas.so",
        "libcudart.so",
    )
    return any(marker in message for marker in provider_markers)


def inspect_engine_sessions(engine: Any) -> dict[str, Any]:
    """Return the three actual business ORT sessions owned by the engine."""
    sessions = {}
    for name, attribute in (
        ("det", "text_det"),
        ("cls", "text_cls"),
        ("rec", "text_rec"),
    ):
        component = getattr(engine, attribute, None)
        wrapper = getattr(component, "session", None)
        sessions[name] = getattr(wrapper, "session", None)
    return sessions


def strict_cuda_engine_error(engine: Any) -> str | None:
    """Reject whole-session CPU fallback while allowing ORT graph partitioning."""
    for name, session in inspect_engine_sessions(engine).items():
        if session is None:
            return f"{name}: missing business session"
        try:
            providers = session.get_providers()
            device_id = (
                session.get_provider_options()
                .get("CUDAExecutionProvider", {})
                .get("device_id")
            )
            if not providers or providers[0] != "CUDAExecutionProvider":
                return f"{name}: CUDA is not the primary session provider"
            if device_id != "0":
                return f"{name}: CUDA session did not select logical device 0"
            session.disable_fallback()
        except Exception:
            return f"{name}: cannot verify device or disable session fallback"
    return None


def inspect_engine_providers(
    engine: Any,
) -> dict[str, list[str] | None] | None:
    """Inspect RapidOCR 3.9.0's three business ORT ownership chains."""

    return {
        name: _session_provider_chain(session)
        for name, session in inspect_engine_sessions(engine).items()
    }


class RapidOCREngineBuildError(RuntimeError):
    """A sanitized engine-construction failure without a partial-session traceback."""


class RapidOCREngineFactory:
    """Process-wide, monotonic CUDA preference used only by admitted builders."""

    def __init__(self, *, strict_cuda: bool = False) -> None:
        self._configured_cuda = (
            os.environ.get("RAPIDOCR_DEVICE", "gpu").lower() == "gpu"
        )
        self._cuda_allowed = self._configured_cuda and _soft_ort_cuda_use()
        self._strict_cuda = strict_cuda
        self._state_lock = threading.Lock()

    def configured_device(self) -> str:
        return "cuda" if self._configured_cuda else "cpu"

    @property
    def use_cuda(self) -> bool:
        with self._state_lock:
            return self._cuda_allowed

    def _latch_cpu(self, reason: str) -> None:
        with self._state_lock:
            changed = self._cuda_allowed
            self._cuda_allowed = False
        if changed:
            logger.warning("RapidOCR future builders latched to CPU: %s", reason)

    def build(self, resolved: ResolvedEngine) -> EngineBuildArtifact:
        prefer_cuda = self.use_cuda
        if self._strict_cuda and not prefer_cuda:
            raise RapidOCREngineBuildError("strict CUDA session provider unavailable")
        try:
            engine = self._construct(resolved, use_cuda=prefer_cuda)
        except Exception as exc:  # noqa: BLE001
            summary = f"{type(exc).__name__}: {exc}"
            device_error = prefer_cuda and _is_ort_device_error(exc)
            exc.__traceback__ = None
            gc.collect()
            if self._strict_cuda or not device_error:
                raise RapidOCREngineBuildError(summary) from None
            try:
                engine = self._construct(resolved, use_cuda=False)
            except Exception as cpu_exc:  # noqa: BLE001
                cpu_summary = f"{type(cpu_exc).__name__}: {cpu_exc}"
                cpu_exc.__traceback__ = None
                gc.collect()
                raise RapidOCREngineBuildError(
                    f"CUDA build failed ({summary}); CPU replacement failed ({cpu_summary})"
                ) from None
            # 只在 CPU replacement 完整构造成功后提交全局降级。权重缺失、
            # 损坏等与 provider 无关的双边失败不得永久污染后续 builder。
            self._latch_cpu(f"CUDA engine construction failed ({summary})")
            # CUDA composite 可能在 det/cls 已构造后才于 rec 失败。即使 CPU
            # replacement 可用，失败构造的私有所有权链也无法直接观测；由池把
            # residency 保持为 Unknown，直到一次成功 force cleanup。
            return EngineBuildArtifact(engine=engine, cleanup_uncertain=True)

        chains = inspect_engine_providers(engine) or {}
        known = [chain for chain in chains.values() if chain]
        if (
            prefer_cuda
            and len(known) == 3
            and all(chain[0] == "CPUExecutionProvider" for chain in known)
        ):
            self._latch_cpu("all constructed business sessions use CPU providers")
        return EngineBuildArtifact(engine=engine)

    @staticmethod
    def _construct(resolved: ResolvedEngine, *, use_cuda: bool) -> RapidOCR:
        params = {
            "Global.use_det": True,
            "Global.use_cls": True,
            "Global.use_rec": True,
            "Det.model_path": resolved.det_path,
            "Det.ocr_version": OCRVersion(resolved.det_meta[0]),
            "Cls.model_path": resolved.cls_path,
            "Cls.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_path": resolved.rec_path,
            "Rec.ocr_version": OCRVersion(resolved.rec_meta[0]),
            "EngineConfig.onnxruntime.use_cuda": use_cuda,
        }
        logger.info(
            "lazy-load RapidOCR engine cuda=%s key=%s",
            use_cuda,
            resolved.pool_key,
        )
        return RapidOCR(params=params)


def _box_to_points(box: np.ndarray, w: int, h: int) -> list[list[float]]:
    """像素四点 (4,2) → 0-100 百分比 [[x,y],...]。"""
    return [[float(p[0]) / w * 100.0, float(p[1]) / h * 100.0] for p in box]


_FULL_CROP_POLY = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]


def _align_orientations(det_res, cls_res, rec_res, final) -> list[str] | None:
    """按 final.txts 与 rec_res.txts 顺序游标回填 orientation。

    RapidOCR 的 ``build_final_output`` 会按「空文本 + text_score 阈值」二次过滤
    boxes/txts;直接按「rec_res.txts 非空」索引快照 cls 标签,在 text_score>0 时会跟
    final.boxes 错位(过滤掉的低分文本对应的方向标签会贴到后一条文本上)。

    本函数在 build_final_output 之后被调用,按 ``final.txts`` 在 ``rec_res.txts``
    中按顺序游标推进 —— build_final_output 保留 rec 原始顺序、不重排,游标推进足以
    把每条 final 文本映射回它在 cls_res.cls_res 里对应的方向标签。

    rec-only 路径(det_res.boxes is None):每 crop 一条 cls 输出,与 final 一一对应。
    """
    cls_labels = getattr(cls_res, "cls_res", None) if cls_res is not None else None
    if cls_labels is None:
        return None

    final_txts = getattr(final, "txts", None)
    if final_txts is None:
        return None

    # rec-only:无 det,每个 crop 单独跑 rec/cls,cls_labels 与 final 同序同长。
    if det_res is None or getattr(det_res, "boxes", None) is None:
        return [str(c[0]) for c in cls_labels]

    rec_txts = getattr(rec_res, "txts", None) if rec_res is not None else None
    if rec_txts is None:
        return None

    orientations: list[str] = []
    cursor = 0
    for ft in final_txts:
        # build_final_output 保留 rec 原始顺序,这里按顺序游标推进:对每条 final 文本,
        # 在 rec_txts 中向前找到第一条与之相等的位置,取该位置的 cls 标签。
        while cursor < len(rec_txts) and rec_txts[cursor] != ft:
            cursor += 1
        if cursor >= len(rec_txts) or cursor >= len(cls_labels):
            return None  # 异常:final 出现 rec 之外的文本,放弃回填避免误标。
        orientations.append(str(cls_labels[cursor][0]))
        cursor += 1
    return orientations


async def _run_blocking_until_complete(call: Any) -> Any:
    """Keep the borrower active until the real executor future has finished."""

    future = asyncio.get_running_loop().run_in_executor(None, call)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        result = future.result()
    except BaseException as exc:
        if cancelled and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError from exc
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class RapidOCRPredictor:
    """Run every composite engine access under a pool borrower and use lock."""

    def __init__(self, engine_pool: EnginePool) -> None:
        self._pool = engine_pool

    async def predict_one(
        self,
        model_id: str,
        resolved: ResolvedEngine,
        file_path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], bool, int | None, int]:
        async with self._pool.borrow(resolved) as lease:
            items, inference_ms = await _run_blocking_until_complete(
                partial(
                    self._predict_sync,
                    lease.engine,
                    model_id,
                    resolved,
                    file_path,
                    params,
                )
            )
            return items, lease.cache_hit, lease.engine_load_ms, inference_ms

    def _run(
        self,
        engine: Any,
        resolved: ResolvedEngine,
        img_content: str,
        *,
        use_det: bool,
        use_cls: bool,
        use_rec: bool,
        params: dict[str, Any] | None = None,
    ):
        """走 RapidOCR 内部步骤，返回 (ori_img, final_output, orientations)。

        **并发安全**：调用方持有 per-key 引擎的 borrower + use lock，
        整段 update_params + load_img + preprocess + run_ocr_steps + build_final_output
        在同一 executor 任务内执行，避免两个并发请求落同一 variant 时，
        后请求覆盖前请求的阈值（text_score / box_thresh / unclip_ratio）。

        **orientation 对齐**：build_final_output 会按「空文本 + text_score 阈值」二次
        过滤 boxes/txts，因此 orientation 必须**在 build_final_output 之后**按
        ``final.txts`` ↔ ``rec_res.txts`` 顺序游标回填，而不是预快照 valid 索引——
        后者只在「仅空文本被过滤」的假设下才对齐，过滤掉低分文本时方向标签会贴错框。

        params（可选，来自 /predict context.params）透传给 update_params，与
        RapidOCR.__call__ 同口径的三个运行时阈值。缺参回落到 RUNTIME_PARAM_DEFAULTS
        并**显式下发**（不传 None）—— det/rec/e2e 同 variant 共享池化引擎，
        update_params 对 None 是跳过不重置，缺参传 None 会让上一次请求的阈值粘在引擎上
        污染后续请求（含跨原子类型、跨项目）。
        """
        p = params or {}

        def _f(key: str) -> float:
            v = p.get(key)
            return float(v) if v is not None else RUNTIME_PARAM_DEFAULTS[key]

        engine.update_params(
            use_det=use_det,
            use_cls=use_cls,
            use_rec=use_rec,
            text_score=_f("text_score"),
            box_thresh=_f("box_thresh"),
            unclip_ratio=_f("unclip_ratio"),
        )
        ori = engine.load_img(img_content)
        img, op = engine.preprocess_img(ori)
        det_res, cls_res, rec_res, crops = engine.run_ocr_steps(img, op)
        final = engine.build_final_output(
            ori,
            det_res,
            cls_res,
            rec_res,
            crops,
            op,
        )

        orientations = _align_orientations(det_res, cls_res, rec_res, final)
        return ori, final, orientations

    def _predict_sync(
        self,
        engine: Any,
        model_id: str,
        resolved: ResolvedEngine,
        file_path: str,
        params: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        if model_id == catalog_mod.DET_MODEL_ID:
            return self._det_sync(engine, resolved, file_path, params)
        if model_id == catalog_mod.REC_MODEL_ID:
            return self._rec_sync(engine, resolved, file_path, params)
        return self._e2e_sync(engine, resolved, file_path, params)

    def _det_sync(
        self,
        engine: Any,
        resolved: ResolvedEngine,
        file_path: str,
        params: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        started = time.monotonic()
        ori, final, _ = self._run(
            engine,
            resolved,
            file_path,
            use_det=True,
            use_cls=False,
            use_rec=False,
            params=params,
        )
        infer_ms = int((time.monotonic() - started) * 1000)
        h, w = ori.shape[:2]
        items: list[dict[str, Any]] = []
        boxes = getattr(final, "boxes", None)
        scores = getattr(final, "scores", None)
        if boxes is not None:
            for i, box in enumerate(boxes):
                items.append(
                    {
                        "type": "polygonlabels",
                        "value": {
                            "points": _box_to_points(box, w, h),
                            "polygonlabels": [POLY_LABEL],
                        },
                        "score": float(scores[i]) if scores is not None else 0.0,
                    }
                )
        return items, infer_ms

    def _rec_sync(
        self,
        engine: Any,
        resolved: ResolvedEngine,
        file_path: str,
        params: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        started = time.monotonic()
        _, final, orientations = self._run(
            engine,
            resolved,
            file_path,
            use_det=False,
            use_cls=True,
            use_rec=True,
            params=params,
        )
        infer_ms = int((time.monotonic() - started) * 1000)
        txts = getattr(final, "txts", None)
        scores = getattr(final, "scores", None)
        if not txts:
            return [], infer_ms
        # rec 原子吃单 crop：取首条识别结果（整 crop = 一行文本）。
        attrs: dict[str, Any] = {"text": txts[0], "language": resolved.lang}
        if orientations:
            attrs["orientation"] = orientations[0]
        item = {
            "type": "polygonlabels",
            "value": {"points": _FULL_CROP_POLY, "polygonlabels": [POLY_LABEL]},
            "score": float(scores[0]) if scores else 0.0,
            "attributes": attrs,
        }
        return [item], infer_ms

    def _e2e_sync(
        self,
        engine: Any,
        resolved: ResolvedEngine,
        file_path: str,
        params: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        started = time.monotonic()
        ori, final, orientations = self._run(
            engine,
            resolved,
            file_path,
            use_det=True,
            use_cls=True,
            use_rec=True,
            params=params,
        )
        infer_ms = int((time.monotonic() - started) * 1000)
        h, w = ori.shape[:2]
        items: list[dict[str, Any]] = []
        boxes = getattr(final, "boxes", None)
        txts = getattr(final, "txts", None)
        scores = getattr(final, "scores", None)
        if boxes is None or txts is None:
            return items, infer_ms
        for i, box in enumerate(boxes):
            attrs: dict[str, Any] = {"text": txts[i], "language": resolved.lang}
            if orientations and i < len(orientations):
                attrs["orientation"] = orientations[i]
            items.append(
                {
                    "type": "polygonlabels",
                    "value": {
                        "points": _box_to_points(box, w, h),
                        "polygonlabels": [POLY_LABEL],
                    },
                    "score": float(scores[i]) if scores is not None else 0.0,
                    "attributes": attrs,
                }
            )
        return items, infer_ms


__all__ = [
    "RapidOCREngineBuildError",
    "RapidOCREngineFactory",
    "RapidOCRPredictor",
    "inspect_engine_providers",
]
