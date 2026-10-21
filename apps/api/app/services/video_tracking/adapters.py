from __future__ import annotations

import uuid
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.config import settings
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.ml_client import MLBackendClient

if TYPE_CHECKING:
    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend


@dataclass(frozen=True)
class TrackerFrameResult:
    frame_index: int
    geometry: dict
    confidence: float | None = None
    outside: bool = False
    # v0.21.26 · 阶段 0 · 多目标: backend 一帧可返回多实例。instance_id 是该 job 内
    # 全局稳定的对象标识 (backend 负责跨窗保持稳定, 见阶段 B); None = 单实例老 backend
    # (归主实例, 回填源 annotation), 与既有单 track 行为等价。
    instance_id: str | None = None
    # primary=True 标记与用户种子对应的那个实例 (backend 按 IoU 挑定)。其逐帧结果回填源
    # annotation; 非 primary 的每个 instance_id 各落一条新 annotation。老 backend 不发此
    # 键 → runner 按「无 instance_id 即主实例」兜底。见 video_tracker_runner 分组落库。
    primary: bool = False


@dataclass
class TrackerSession:
    action: str = "start"
    token: str | None = None
    span_start_frame: int | None = None
    span_end_frame: int | None = None


@dataclass(frozen=True)
class TrackerContext:
    job_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    dataset_item_id: uuid.UUID
    annotation_id: uuid.UUID
    from_frame: int
    to_frame: int
    direction: str
    prompt: dict
    source_geometry: dict
    task_data: dict
    ml_backend: "MLBackend | None" = None
    shadow_session_factory: GPUShadowSessionFactory | None = None
    dispatch_context_factory: GPUDispatchContextFactory | None = None
    sam_variant: str | None = (
        None  # v0.10.36 · 透传到 backend /predict video_tracker 分支
    )
    # v0.21.19 · text-driven 追踪 (sam3_video): 文本 query + 视觉示例框, 显式透传到
    # backend context 顶层 (而非仅塞进自由 prompt), 与 seed-bbox tracker 区分。
    text: str | None = None
    exemplars: list[dict] | None = None
    # v0.21.20 · polygon track 回填: "polygon" 时 backend 每帧保留 mask 矢量化的多边形
    # 而非降 bbox; 缺省 "bbox" 维持既有 seed-bbox tracker 行为。由 runner 按源几何类型定。
    output_geometry: str = "bbox"
    # v0.21.27 · U-pvs-1 · PVS 逐对象种子 (含 points / 多目标), 每条
    # {obj_id?, bbox?/points?}。仅在种子窗 (首窗) 有值 —— points 锚在原始种子帧, 后续窗靠
    # source_geometry 续追。缺省 None → backend 退 source_geometry 单框兜底 (与 sam2_video /
    # 已发 B-pvs 框种子等价, 零回归)。其它 backend 无此键不受影响。
    seeds: list[dict] | None = None
    session: TrackerSession | None = None
    response_digest: list[str] | None = None
    model_id: str | None = None
    outside_threshold: float | None = None


def _response_digest(body) -> str:
    digest, size = hashlib.sha256(), 0
    for chunk in json.JSONEncoder(
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).iterencode(body):
        content = chunk.encode("utf-8")
        size += len(content)
        if size > 64 * 1024 * 1024:
            raise ValueError("tracker_response_too_large")
        digest.update(content)
    return digest.hexdigest()


class TrackerAdapter(Protocol):
    model_key: str

    def propagate(self, ctx: TrackerContext) -> AsyncIterator[TrackerFrameResult]: ...


def _bbox_from_points(points: list) -> dict:
    """归一化多边形/折线顶点 → 外接 bbox {x,y,w,h}; 空/退化 → 零框。

    v0.21.20 · SAM2 只吃 bbox seed, polygon track 的种子 = 多边形外接框; 跨窗续追时
    上一窗结果几何也是多边形, 同样取外接框喂下一窗。
    """
    xs = [float(p[0]) for p in points if len(p) >= 2]
    ys = [float(p[1]) for p in points if len(p) >= 2]
    if not xs or not ys:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _bbox_from_geometry(geometry: dict) -> dict:
    if geometry.get("type") == "video_track_bbox":
        keyframes = sorted(
            geometry.get("keyframes") or [],
            key=lambda item: int(item.get("frame_index", 0)),
        )
        if keyframes:
            return dict(keyframes[0].get("bbox") or {})

    # v0.21.20 · polygon track: 取首关键帧顶点外接框 / 单帧 polygon 结果取其顶点外接框。
    if geometry.get("type") == "video_track_polygon":
        keyframes = sorted(
            geometry.get("keyframes") or [],
            key=lambda item: int(item.get("frame_index", 0)),
        )
        if keyframes:
            return _bbox_from_points(keyframes[0].get("points") or [])

    if geometry.get("type") == "polygon":
        return _bbox_from_points(geometry.get("points") or [])

    if geometry.get("type") == "mask":
        return dict(geometry.get("bbox") or {})

    if geometry.get("type") in {"bbox", "video_bbox"}:
        return {
            "x": float(geometry.get("x", 0)),
            "y": float(geometry.get("y", 0)),
            "w": float(geometry.get("w", geometry.get("width", 0))),
            "h": float(geometry.get("h", geometry.get("height", 0))),
        }

    return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}


class MockBboxTrackerAdapter:
    model_key = "mock_bbox"

    async def propagate(self, ctx: TrackerContext) -> AsyncIterator[TrackerFrameResult]:
        prompt_geometry = ctx.prompt.get("geometry")
        bbox = _bbox_from_geometry(
            prompt_geometry
            if isinstance(prompt_geometry, dict)
            else ctx.source_geometry
        )
        frames = range(ctx.from_frame, ctx.to_frame + 1)
        if ctx.direction == "backward":
            frames = range(ctx.to_frame, ctx.from_frame - 1, -1)

        observed = hashlib.sha256(b"[")
        for index, frame_index in enumerate(frames):
            if index:
                observed.update(b",")
            observed.update(
                json.dumps(
                    {
                        "frame_index": frame_index,
                        "geometry": {"type": "bbox", **bbox},
                        "confidence": 1.0,
                        "outside": False,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            yield TrackerFrameResult(
                frame_index=frame_index,
                geometry={"type": "bbox", **bbox},
                confidence=1.0,
                outside=False,
            )
        observed.update(b"]")
        if ctx.response_digest is not None:
            ctx.response_digest.append(observed.hexdigest())


class MLBackendVideoTrackerAdapter:
    def __init__(self, model_key: str) -> None:
        self.model_key = model_key

    async def propagate(self, ctx: TrackerContext) -> AsyncIterator[TrackerFrameResult]:
        backend = ctx.ml_backend
        if backend is None:
            raise ValueError(
                f"{self.model_key} requires a connected project ML backend"
            )
        if backend.state != "connected":
            raise ValueError(
                f"{self.model_key} requires a connected project ML backend"
            )

        client = MLBackendClient(
            backend,
            shadow_session_factory=ctx.shadow_session_factory,
            dispatch_context_factory=ctx.dispatch_context_factory,
        )
        context: dict = {
            "type": "video_tracker",
            "model_key": self.model_key,
            "job_id": str(ctx.job_id),
            "task_id": str(ctx.task_id),
            "project_id": str(ctx.project_id),
            "dataset_item_id": str(ctx.dataset_item_id),
            "annotation_id": str(ctx.annotation_id),
            "from_frame": ctx.from_frame,
            "to_frame": ctx.to_frame,
            "direction": ctx.direction,
            "prompt": ctx.prompt,
            "source_geometry": ctx.source_geometry,
        }
        if ctx.model_id is not None:
            context["model_id"] = ctx.model_id
        # v0.14.15 · 仅当显式指定时透传模型变体; 缺省让后端回退默认 tiny.
        if ctx.sam_variant:
            context["model_variants"] = {"sam_variant": ctx.sam_variant}
        # v0.21.19 · text-driven 追踪的 text/exemplars 显式提到 context 顶层 (seed-bbox
        # tracker 无此二键), 让 backend /predict video_tracker 分支按 text 重检测。
        if ctx.text:
            context["text"] = ctx.text
        if ctx.exemplars:
            context["exemplars"] = ctx.exemplars
        # v0.21.20 · polygon track 回填: 显式下发期望输出几何, backend 据此保留 mask
        # 矢量化的多边形; 缺省 bbox 不下发, 老 backend 无此键也不受影响。
        if ctx.output_geometry and ctx.output_geometry != "bbox":
            context["output_geometry"] = ctx.output_geometry
        # v0.21.27 · U-pvs-1 · PVS 点/多目标种子: 仅种子窗有值。backend _seeds_from_video_ctx
        # 优先 seeds[] (含 points) 于 source_geometry 兜底; 其它 backend 无此键不受影响。
        if ctx.seeds:
            context["seeds"] = ctx.seeds
        if ctx.session is not None:
            context["tracker_context"] = {
                "action": ctx.session.action,
                "job_id": str(ctx.job_id),
                "direction": ctx.direction,
            }
            if ctx.session.token:
                context["tracker_context"]["token"] = ctx.session.token
            if ctx.session.action == "start":
                context["tracker_context"].update(
                    {
                        "span_start_frame": ctx.session.span_start_frame,
                        "span_end_frame": ctx.session.span_end_frame,
                    }
                )
        result = await client.predict_interactive(
            task_data=ctx.task_data,
            context=context,
        )
        if ctx.response_digest is not None:
            ctx.response_digest.append(
                _response_digest({"result": result.result, "meta": result.meta})
            )
        if ctx.session is not None:
            session_meta = (result.meta or {}).get("tracker_context") or {}
            token = session_meta.get("token")
            if ctx.session.action != "close" and not isinstance(token, str):
                raise ValueError("tracker_context_lost")
            ctx.session.token = token if isinstance(token, str) else None
            ctx.session.action = "continue"

        for item in result.result:
            if isinstance(item, dict):
                yield _frame_result_from_payload(
                    item, outside_threshold=ctx.outside_threshold
                )


def _frame_result_from_payload(
    payload: dict, *, outside_threshold: float | None = None
) -> TrackerFrameResult:
    confidence = payload.get("confidence")
    if confidence is not None:
        confidence = float(confidence)
    outside = bool(payload.get("outside", False))
    if confidence is not None and confidence < (
        settings.video_tracker_low_confidence_outside_threshold
        if outside_threshold is None
        else outside_threshold
    ):
        outside = True

    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        geometry = {k: payload[k] for k in ("type", "x", "y", "w", "h") if k in payload}
    if not geometry:
        geometry = {"type": "bbox", "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

    instance_id = payload.get("instance_id")
    if instance_id is not None:
        instance_id = str(instance_id)

    return TrackerFrameResult(
        frame_index=int(payload["frame_index"]),
        geometry=geometry,
        confidence=confidence,
        outside=outside,
        instance_id=instance_id,
        primary=bool(payload.get("primary", False)),
    )


_REGISTRY: dict[str, TrackerAdapter] = {
    MockBboxTrackerAdapter.model_key: MockBboxTrackerAdapter(),
    "sam2_video": MLBackendVideoTrackerAdapter("sam2_video"),
    "sam3_video": MLBackendVideoTrackerAdapter("sam3_video"),
    # v0.21.26 · 阶段 B-pvs · SAM3 PVS 交互追踪 (点/框 seed + memory 传播)。与 sam3_video
    # (text 开集) 同后端不同模型; adapter 与 sam2_video 同为 seed 驱动 (透传 source_geometry)。
    "sam3_video_interactive": MLBackendVideoTrackerAdapter("sam3_video_interactive"),
}


def get_tracker_adapter(model_key: str) -> TrackerAdapter:
    try:
        return _REGISTRY[model_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported tracker model: {model_key}") from exc


def registered_tracker_models() -> list[str]:
    return sorted(_REGISTRY)
