"""onnxtools-backend 推理 + 结果映射到协议 v2 result types。

二阶段管道(检测 → 机动车 ROI → 车型/颜色)的输出映射到 LabelStudio 风格 result：

- 每个检测 → ``type=rectanglelabels``，``value={x,y,width,height(百分比), rectanglelabels:[det类]}``
- 机动车额外 → ``attributes={vehicle_type, color}``

坐标统一归一化百分比（与平台 / LabelStudio 一致）。VehicleAttributePipeline 出像素
xyxy，这里 / image(W, H) × 100。

本模块不依赖 onnxtools / 协议包，只做纯映射 + 图像解码，便于隔离单测。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

import cv2
import numpy as np
from aap_backend_runtime import fetch_image

from handle_pool import HANDLE_SESSION_COUNTS


def _class_name_of(names: Any, cls: int) -> str:
    """从检测器 ``class_names`` 取下标 ``cls`` 的类名，兼容 dict / list 两种载体。

    ``BaseORT.class_names`` 读自 ONNX metadata，是 ``{int: str}`` dict；早先经 pipeline
    ``_resolve_class_names`` 转成 list。原子层直接读 ``detector.class_names``（dict），但保留
    list 兼容以防注入端形态不同。
    """
    if isinstance(names, Mapping):
        return names.get(cls) or names.get(str(cls)) or "unknown"
    if isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
        return names[cls] if 0 <= cls < len(names) else "unknown"
    return "unknown"


def load_image_bgr(file_path: str, *, http_timeout: float = 10.0) -> np.ndarray:
    """加载图像为 BGR ndarray（VehicleAttributePipeline 期望 cv2 BGR 输入）。

    下载/解码（``data:`` base64 / ``http(s)://`` presigned URL / 本地绝对路径）复用共享
    :func:`aap_backend_runtime.fetch_image`（出 RGB ``PIL.Image``）, 再转 cv2 BGR。

    Args:
        file_path: 图像来源。
        http_timeout: http 下载超时（秒）。

    Returns:
        BGR 图像 ndarray [H, W, 3]。
    """
    img = fetch_image(file_path, timeout=http_timeout)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def classification_to_result(
    vehicle_type: str,
    color: str,
    *,
    vehicle_type_conf: float = 0.0,
    color_conf: float = 0.0,
) -> dict[str, Any]:
    """把纯分类结果(整张输入图 = 一辆车)映射为单条协议 v2 result。

    用于「检测→分类」多阶段编排的下游分类阶段:上游已裁好单车 ROI,本 backend 只跑
    VehicleAttributeORT,跳过 rtdetr 检测。几何取整图框 ——下游阶段的几何会被平台 merge
    丢弃,只取 ``attributes``;``rectanglelabels`` 填车型,便于单 backend 直接调用时仍可读。

    Args:
        vehicle_type: 车型类别 (VEHICLE_TYPE_MAP 的 value)。
        color: 颜色类别 (VEHICLE_COLOR_MAP 的 value)。
        vehicle_type_conf: 车型分类置信度。
        color_conf: 颜色分类置信度。

    Returns:
        单条协议 result (type=rectanglelabels, 整图框 + attributes)。
    """
    return {
        "type": "rectanglelabels",
        "value": {
            "x": 0.0,
            "y": 0.0,
            "width": 100.0,
            "height": 100.0,
            "rectanglelabels": [vehicle_type],
        },
        "score": float(min(vehicle_type_conf, color_conf)),
        "attributes": {"vehicle_type": vehicle_type, "color": color},
    }


def detections_to_results(
    output: list[dict[str, Any]], img_w: int, img_h: int
) -> list[dict[str, Any]]:
    """把 VehicleAttributePipeline 输出映射为协议 v2 result 数组。

    Args:
        output: pipeline 输出，每项含 ``type`` / ``box2d`` / ``score``，机动车额外含
            ``vehicle_type`` / ``color``。
        img_w: 原图宽（像素），用于归一化。
        img_h: 原图高（像素）。

    Returns:
        协议 result 数组，每项 ``type=rectanglelabels``，机动车带 ``attributes``。
    """
    items: list[dict[str, Any]] = []
    for d in output:
        x1, y1, x2, y2 = d["box2d"]
        item: dict[str, Any] = {
            "type": "rectanglelabels",
            "value": {
                "x": x1 / img_w * 100.0,
                "y": y1 / img_h * 100.0,
                "width": (x2 - x1) / img_w * 100.0,
                "height": (y2 - y1) / img_h * 100.0,
                "rectanglelabels": [d["type"]],
            },
            "score": float(d.get("score", 0.0)),
        }
        if "vehicle_type" in d:
            item["attributes"] = {
                "vehicle_type": d["vehicle_type"],
                "color": d["color"],
            }
        items.append(item)
    return items


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


def inspect_handle_sessions(name: str, handle: Any) -> list[Any] | None:
    """Return the actual ORT session owners in the fixed business topology."""
    if name in {"detector", "va"}:
        return [getattr(handle, "_onnx_session", None)]
    if name == "pipeline":
        detector = getattr(handle, "detector", None)
        classifier = getattr(handle, "va_classifier", None)
        return [
            getattr(detector, "_onnx_session", None),
            getattr(classifier, "_onnx_session", None),
        ]
    return None


def strict_cuda_handle_error(name: str, handle: Any) -> str | None:
    """Reject whole-session CPU fallback while allowing ORT graph partitioning."""
    sessions = inspect_handle_sessions(name, handle)
    if sessions is None or len(sessions) != HANDLE_SESSION_COUNTS.get(name):
        return f"{name}: incomplete business session topology"
    for session in sessions:
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


def inspect_handle_providers(name: str, handle: Any) -> list[list[str]] | None:
    """Read every expected business session owned by one logical handle.

    Missing private fields stay unknown.  Each unknown session is represented by an
    empty provider chain so a known CUDA sibling can still conservatively prove GPU
    residency for a partially introspectable composite handle.
    """

    sessions = inspect_handle_sessions(name, handle)
    if sessions is None:
        return None
    return [_session_provider_chain(session) or [] for session in sessions]


async def _run_blocking_until_complete(call: Any) -> Any:
    """Keep a borrower active until the real executor future has finished."""

    future = asyncio.get_running_loop().run_in_executor(None, call)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
            continue
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result


class VehicleAttributePredictor:
    """Run every ONNXTools handle access under a pool borrower and use lock."""

    def __init__(self, handle_pool: Any) -> None:
        self._pool = handle_pool

    async def predict_one(
        self,
        file_path: str,
        handle_name: str,
    ) -> tuple[list[dict[str, Any]], bool, int | None, int]:
        async with self._pool.borrow(handle_name) as lease:
            if handle_name == "va":
                items, inference_ms = await _run_blocking_until_complete(
                    partial(self._classify_sync, lease.handle, file_path)
                )
            elif handle_name == "detector":
                items, inference_ms = await _run_blocking_until_complete(
                    partial(self._detect_sync, lease.handle, file_path)
                )
            else:
                items, inference_ms = await _run_blocking_until_complete(
                    partial(self._pipeline_sync, lease.handle, file_path)
                )
            return (
                items,
                lease.cache_hit,
                lease.handle_load_ms,
                inference_ms,
            )

    @staticmethod
    def _pipeline_sync(
        pipeline: Any,
        file_path: str,
    ) -> tuple[list[dict[str, Any]], int]:
        img = load_image_bgr(file_path)
        img_h, img_w = img.shape[:2]
        started = time.monotonic()
        output = pipeline(img)
        inference_ms = int((time.monotonic() - started) * 1000)
        return detections_to_results(output, img_w, img_h), inference_ms

    @staticmethod
    def _detect_sync(
        detector: Any,
        file_path: str,
    ) -> tuple[list[dict[str, Any]], int]:
        img = load_image_bgr(file_path)
        img_h, img_w = img.shape[:2]
        started = time.monotonic()
        result = detector(img)
        inference_ms = int((time.monotonic() - started) * 1000)
        names = detector.class_names
        output: list[dict[str, Any]] = []
        for i in range(len(result)):
            xyxy = [float(coordinate) for coordinate in result.boxes[i]]
            class_id = int(result.class_ids[i])
            output.append(
                {
                    "type": _class_name_of(names, class_id),
                    "box2d": xyxy,
                    "score": float(result.scores[i]),
                }
            )
        return detections_to_results(output, img_w, img_h), inference_ms

    @staticmethod
    def _classify_sync(
        classifier: Any,
        file_path: str,
    ) -> tuple[list[dict[str, Any]], int]:
        img = load_image_bgr(file_path)
        started = time.monotonic()
        classified = classifier(img)
        inference_ms = int((time.monotonic() - started) * 1000)
        item = classification_to_result(
            classified.labels[0],
            classified.labels[1],
            vehicle_type_conf=float(classified.confidences[0]),
            color_conf=float(classified.confidences[1]),
        )
        return [item], inference_ms


__all__ = [
    "VehicleAttributePredictor",
    "classification_to_result",
    "detections_to_results",
    "inspect_handle_providers",
    "inspect_handle_sessions",
    "strict_cuda_handle_error",
    "load_image_bgr",
]
