"""SAM 3 ML Backend — FastAPI 入口.

实现 docs-site/dev/reference/ml-backend-protocol.md 规定的 4 个端点 + 2 个观测端点 +
2 个运维端点 (与 grounded-sam2-backend 对齐):
    GET  /health        探活 (含 GPU / cache / PerfHud / idle 状态)
    GET  /setup         模型配置 (supported_prompts: point/interactive_box/text/exemplar)
    GET  /versions      可用版本
    POST /predict       交互式 / 批量预测 (懒加载: idle unload 后自动重建)
    GET  /metrics       Prometheus exposition (sam3_* 指标)
    GET  /cache/stats   embedding cache 当前状态
    POST /unload        主动卸载模型释放显存
    POST /reload        主动重载模型

prompt 类型 (启用 inst_interactivity):
    - context.type == "text"            → Sam3Processor.set_text_prompt → 全图所有匹配概念的 masks
    - context.type == "exemplar"        → Sam3Processor.add_geometric_prompt → 全图相似实例 (PCS)
    - context.type == "point"           → model.predict_inst(point_coords) → 单实例点交互 (SAM-style)
    - context.type == "interactive_box" → model.predict_inst(box) → 单框单 mask (SAM-style)

"point" / "interactive_box" 与 "exemplar" 语义不同: 前两者是「点/框精修出单实例 mask」,
后者是 PCS「找全图与示例框相似的所有实例」. "bbox" 已退出交互 prompt 命名空间
(仅作几何形状), 旧 type=bbox 请求落到 422.

Idle Unload (双 backend 并存场景的显存让渡机制):
    SAM 3.1 FP16 ~6-7GB 常驻显存; 3090 单卡若同时常驻 grounded-sam2 (~2GB) + sam3 (~7GB),
    与平台其他 GPU 任务争用容易紧张. SAM3_IDLE_UNLOAD_SECONDS 触发自动卸载 (默认 600s
    无 /predict 即卸); 下次请求懒重载 (冷启动 ~8-12s). 端到端运维侧可通过 POST /unload
    /reload 显式控制.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
import tempfile
import time
from typing import Any, Callable

import httpx
import torch
from aap_backend_runtime import (
    BuildArtifact,
    DeviceUnavailableError,
    ManagedBuildTimeout,
    ManagedLruPool,
    ManagedPoolBusyError,
    TrackerSessionLost,
    TrackerSessionManager,
    deployment_verified_flag,
    fetch_image,
    physical_gpu_identity,
    versions_payload,
    validate_single_gpu_device_set,
)
from aap_protocol_v2 import (
    COMPAT_PROTOCOL_VERSIONS,
    CorrectionFramePrompt,
    PROTOCOL_VERSION,
    ModelUnavailableError,
    MaskInteractionDiagnostic,
    PlatformRole,
    TrackerContextControl,
    VariantNotSupportedError,
    decode_low_res_mask,
    log_deprecated_model_variant_fields,
    normalize_context_model_variants,
)
from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    GenerationTransitionRequest,
    LifecycleModeRequest,
    LifecycleResetRequest,
    ManagedLifecycleCapabilities,
    load_verify_keyring,
    match_gpu_health_challenge,
    parse_gpu_admission_header_values,
    parse_gpu_control_token_header_values,
)
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError

from embedding_cache import EmbeddingCache, compute_cache_key
from gpu_lifecycle import Sam3GpuLifecycle, WorkloadOperation
from observability import (
    init_perfhud_collectors,
    record_cache,
    record_inference,
    record_mask_ai_backend_inference,
    sample_perfhud,
    shutdown_perfhud_collectors,
    update_cache_size,
)
from mask_utils import PromptAdapterError
from predictor import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_SIMPLIFY_TOLERANCE,
    MODEL_VARIANT,
    SAM3Predictor,
)
from video_predictor import (
    DEFAULT_MAX_WINDOW_FRAMES as MULTIPLEX_MAX_WINDOW_FRAMES,
    SAM3MultiplexVideoTracker,
)
from pvs_video_predictor import (
    DEFAULT_MAX_CONTEXT_FRAMES as PVS_MAX_CONTEXT_FRAMES,
    DEFAULT_MAX_WINDOW_FRAMES as PVS_MAX_WINDOW_FRAMES,
    SAM3PVSVideoTracker,
)
from pool_domain import Sam3Pools
from schemas import BatchPredictResponse, Context, PredictionResult, WarmupResponse

logger = logging.getLogger("sam3-backend")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

MODEL_VERSION = MODEL_VARIANT  # "sam3" (图像模型即 facebook/sam3 单档)
# 视频追踪权重 (sam3.1_multiplex) 的展示名, 供前端「视频权重」条目 (与图像「SAM 3」对称)。
_VIDEO_MODEL_VERSION = "SAM 3.1"
# /setup 协议标准化暴露 backend 镜像版本 (与 FastAPI app.version 同源).
BACKEND_VERSION = os.getenv("BACKEND_VERSION", "0.10.1")
IMAGE_DOWNLOAD_TIMEOUT = float(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "30"))
EMBEDDING_CACHE_SIZE = int(os.getenv("SAM3_EMBEDDING_CACHE_SIZE", "32"))

# 与 grounded-sam2-backend 的 IDLE_UNLOAD_SECONDS 区分开, 让两个 backend 可独立调.
# sam3 默认与 sam2 一致 (600s/60s); sam3 显存占用大, 默认值偏积极也可由用户改更短.
# 0 / 负数 关闭定时卸载, 仍可通过 POST /unload 手动卸载.
IDLE_UNLOAD_SECONDS = float(os.getenv("SAM3_IDLE_UNLOAD_SECONDS", "600"))
IDLE_CHECK_INTERVAL = float(os.getenv("SAM3_IDLE_CHECK_INTERVAL", "60"))
MODEL_POOL_BUILD_TIMEOUT = float(os.getenv("SAM3_MODEL_POOL_BUILD_TIMEOUT", "120"))
MANAGED_LIFECYCLE_VERIFIED = deployment_verified_flag("SAM3_MANAGED_LIFECYCLE_VERIFIED")
MAX_PREDICT_REQUEST_BYTES = 6 * 1024 * 1024

app = FastAPI(title="sam3-backend", version=BACKEND_VERSION)


async def _buffer_bounded_predict_body(request: Request) -> None:
    """Buffer one JSON predict body before acquiring GPU admission."""

    content_encoding = request.headers.get("content-encoding", "identity").lower()
    if content_encoding not in {"", "identity"}:
        raise HTTPException(
            status_code=415, detail="compressed predict bodies are not supported"
        )
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="invalid Content-Length"
            ) from exc
        if declared > MAX_PREDICT_REQUEST_BYTES:
            raise HTTPException(
                status_code=413, detail="predict request body is too large"
            )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PREDICT_REQUEST_BYTES:
            raise HTTPException(
                status_code=413, detail="predict request body is too large"
            )
    request._body = bytes(body)  # noqa: SLF001 - Starlette body cache after bounded stream


class _ManagedAdmissionRoute(APIRoute):
    """Admit workload routes before FastAPI reads or validates their bodies."""

    def get_route_handler(self):
        original = super().get_route_handler()
        scope = {
            "/predict": AdmissionScope.PREDICT,
            "/warmup": AdmissionScope.WARMUP,
            "/reload": AdmissionScope.RELOAD,
        }.get(self.path)
        if scope is None:
            return original

        async def admitted(request: Request):
            if scope == AdmissionScope.PREDICT:
                await _buffer_bounded_predict_body(request)
            operation = await _begin_workload(request, scope)
            request.state.gpu_workload = operation
            try:
                return await original(request)
            finally:
                await operation.close()

        return admitted


app.router.route_class = _ManagedAdmissionRoute


@app.exception_handler(ValueError)
async def _value_error_to_400(_request: Request, exc: ValueError):
    # aap_backend_runtime.fetch_image 对 unsupported scheme 抛 ValueError;此 handler
    # 把它包成 HTTPException(400) 的等价响应,恢复抽取前 _fetch_image 的 400 语义,并防止
    # 原生 traceback / 内部路径泄露到响应体。
    return JSONResponse(status_code=400, content={"detail": str(exc)})


_cache = EmbeddingCache(capacity=EMBEDDING_CACHE_SIZE, sam_variant=MODEL_VERSION)
_last_request_at: float = time.monotonic()
_idle_task: asyncio.Task | None = None
_POOL_KEY: str = MODEL_VERSION  # opaque key, 协议 §4.3 sam3 用 model_variant 字符串
_MULTIPLEX_KEY = "sam3_video"
_PVS_KEY = "sam3_video_interactive"

_image_pool: ManagedLruPool[str, SAM3Predictor] | None = None
_multiplex_pool: ManagedLruPool[str, SAM3MultiplexVideoTracker] | None = None
_pvs_pool: ManagedLruPool[str, SAM3PVSVideoTracker] | None = None
_pvs_sessions = TrackerSessionManager(ttl_seconds=300.0)
_pool_domain: Sam3Pools | None = None
_gpu_lifecycle: Sam3GpuLifecycle | None = None


def _build_predictor(_key: str = _POOL_KEY) -> SAM3Predictor:
    return SAM3Predictor(embedding_cache=_cache)


def _build_video_tracker(_key: str = _MULTIPLEX_KEY) -> SAM3MultiplexVideoTracker:
    return SAM3MultiplexVideoTracker()


def _build_pvs_tracker(_key: str = _PVS_KEY) -> SAM3PVSVideoTracker:
    return SAM3PVSVideoTracker()


def _strict_free_gpu_memory() -> None:
    """Release managed CUDA allocator state without hiding cleanup failure."""

    if not torch.cuda.is_available():
        return
    torch.clear_autocast_cache()
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()


def _cleanup_image_attachments(attachments: tuple[Any, ...]) -> None:
    for attachment in attachments:
        if isinstance(attachment, EmbeddingCache):
            attachment.clear()


def _image_artifact(key: str) -> BuildArtifact[SAM3Predictor]:
    return BuildArtifact(
        resource=_build_predictor(key),
        attachments=(_cache,),
    )


def _multiplex_artifact(key: str) -> BuildArtifact[SAM3MultiplexVideoTracker]:
    return BuildArtifact(resource=_build_video_tracker(key))


def _pvs_artifact(key: str) -> BuildArtifact[SAM3PVSVideoTracker]:
    return BuildArtifact(resource=_build_pvs_tracker(key))


def _require_checkpoint(filename: str) -> None:
    path = os.path.join(os.getenv("CHECKPOINT_DIR", "/app/checkpoints"), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"checkpoint not provisioned: {path}")


def _preflight_image(_key: str) -> None:
    _require_checkpoint("sam3.pt")


def _preflight_multiplex(_key: str) -> None:
    _require_checkpoint("sam3.1_multiplex.pt")


def _preflight_pvs(_key: str) -> None:
    _require_checkpoint("sam3.pt")


def _legacy_image_pool_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "cap": snapshot["cap"],
        "current_size": snapshot["current_size"],
        "loaded_keys": [
            {key: value for key, value in item.items() if key != "borrowers"}
            for item in snapshot["loaded_keys"]
        ],
        "last_evict": snapshot["last_evict"],
    }


def _legacy_video_pool_status(
    multiplex: dict[str, Any],
    pvs: dict[str, Any],
) -> dict[str, Any]:
    loaded_variants = [
        item["key"] for snapshot in (multiplex, pvs) for item in snapshot["loaded_keys"]
    ]
    return {
        "cap": multiplex["cap"] + pvs["cap"],
        "current_size": multiplex["current_size"] + pvs["current_size"],
        "loaded_keys": [{"key": key} for key in loaded_variants],
        "loaded_variants": loaded_variants,
        "active_sessions": (
            multiplex.get("active_sessions", 0) + pvs.get("active_sessions", 0)
        ),
    }


def _effective_compute_device(snapshot: dict[str, Any]) -> str | None:
    devices = {
        str(pool["device"]).strip().lower()
        for pool in snapshot["pools"].values()
        if pool.get("gpu_resident") is True and pool.get("device") is not None
    }
    if not devices or not all(device.startswith("cuda") for device in devices):
        return None
    return next(iter(devices)) if len(devices) == 1 else "cuda"


def _managed_lifecycle_headers(request: Request) -> tuple[str | None, str | None]:
    try:
        headers = parse_gpu_admission_header_values(
            request.headers.getlist(GPU_GENERATION_HEADER),
            request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
        )
    except ValueError as exc:
        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc
    return headers if headers is not None else (None, None)


def _managed_control_token(request: Request) -> str:
    try:
        return parse_gpu_control_token_header_values(
            request.headers.getlist(GPU_GENERATION_HEADER),
            request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
        )
    except ValueError as exc:
        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc


async def _begin_workload(
    request: Request,
    scope: AdmissionScope,
) -> WorkloadOperation:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    return await _gpu_lifecycle.begin_workload(
        scope,
        generation_header=generation_header,
        token=token,
    )


def _request_operation(request: Request) -> WorkloadOperation:
    operation = getattr(request.state, "gpu_workload", None)
    if operation is None:
        raise RuntimeError("workload admission operation is missing")
    return operation


async def _warm_pool(
    pool: ManagedLruPool[str, Any],
    key: str,
    operation: WorkloadOperation,
) -> tuple[bool, int | None, str | None]:
    try:
        return await pool.warmup(key)
    except ManagedBuildTimeout as exc:
        operation.track_future(exc.builder)
        raise ModelUnavailableError(key, str(exc)) from exc
    except (ManagedPoolBusyError, DeviceUnavailableError, FileNotFoundError) as exc:
        raise ModelUnavailableError(key, str(exc)) from exc
    except asyncio.CancelledError:
        operation.track_future(pool.builder_for_now(key))
        raise


def _normalize_predict_context(ctx: dict) -> dict:
    try:
        normalized, deprecated = normalize_context_model_variants(ctx)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_deprecated_model_variant_fields(logger, deprecated)
    model_variants = normalized.get("model_variants") or {}
    model_variant = model_variants.get("model_variant")
    if model_variant is not None and model_variant != MODEL_VERSION:
        raise VariantNotSupportedError("model_variant", model_variant, [MODEL_VERSION])
    return normalized


async def _idle_watcher() -> None:
    """Unload each independently idle pool without crossing a real owner."""
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            await _pvs_sessions.expire()
            if IDLE_UNLOAD_SECONDS <= 0 or _gpu_lifecycle is None:
                continue
            idle_before = time.monotonic() - IDLE_UNLOAD_SECONDS
            for pool_id in ("image", "multiplex_video", "pvs_video"):
                count = await _gpu_lifecycle.try_idle_unload(
                    pool_id,
                    idle_before=idle_before,
                )
                if count:
                    logger.info("idle unloaded SAM3 pool %s", pool_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("idle watcher loop error; continuing")


@app.on_event("startup")
async def _load_models() -> None:
    global _gpu_lifecycle, _idle_task, _image_pool, _last_request_at
    global _multiplex_pool, _pool_domain, _pvs_pool
    validate_single_gpu_device_set()
    raw_keyring = os.getenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "").strip()
    verify_keyring = load_verify_keyring(raw_keyring) if raw_keyring else {}
    build_serial_lock = asyncio.Lock()
    _image_pool = ManagedLruPool(
        cap=1,
        build_resource=_image_artifact,
        key_to_str=str,
        strict_cleanup=_strict_free_gpu_memory,
        cleanup_attachments=_cleanup_image_attachments,
        preflight=_preflight_image,
        build_timeout=MODEL_POOL_BUILD_TIMEOUT,
        build_serial_lock=build_serial_lock,
        pool_name="SAM3 image models",
        logger_name="sam3-backend.managed-pool",
    )
    _multiplex_pool = ManagedLruPool(
        cap=1,
        build_resource=_multiplex_artifact,
        key_to_str=str,
        strict_cleanup=_strict_free_gpu_memory,
        preflight=_preflight_multiplex,
        build_timeout=MODEL_POOL_BUILD_TIMEOUT,
        build_serial_lock=build_serial_lock,
        pool_name="SAM3 multiplex video models",
        logger_name="sam3-backend.managed-pool",
    )
    _pvs_pool = ManagedLruPool(
        cap=1,
        build_resource=_pvs_artifact,
        key_to_str=str,
        strict_cleanup=_strict_free_gpu_memory,
        preflight=_preflight_pvs,
        build_timeout=MODEL_POOL_BUILD_TIMEOUT,
        build_serial_lock=build_serial_lock,
        pool_name="SAM3 PVS video models",
        logger_name="sam3-backend.managed-pool",
    )
    _pool_domain = Sam3Pools(_image_pool, _multiplex_pool, _pvs_pool)
    _gpu_lifecycle = Sam3GpuLifecycle(
        _pool_domain,
        verify_keyring=verify_keyring,
        evictable_verified=MANAGED_LIFECYCLE_VERIFIED,
    )
    # 不再启动急加载: 未注册 / 无流量时白占 ~3.6GB 显存。改纯懒加载——首个推理 /
    # 预热请求通过对应受管 pool 冷启；需暖启时由模型市场发起「预热默认」。
    logger.info(
        "SAM 3 backend ready (lazy load; variant=%s, cache_size=%d, "
        "idle_unload=%.0fs managed_verified=%s)",
        MODEL_VERSION,
        EMBEDDING_CACHE_SIZE,
        IDLE_UNLOAD_SECONDS,
        MANAGED_LIFECYCLE_VERIFIED,
    )
    _last_request_at = time.monotonic()
    init_perfhud_collectors()
    if IDLE_UNLOAD_SECONDS > 0:
        _idle_task = asyncio.create_task(_idle_watcher())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _gpu_lifecycle, _idle_task, _image_pool
    global _multiplex_pool, _pool_domain, _pvs_pool
    if _idle_task is not None:
        _idle_task.cancel()
        try:
            await _idle_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _idle_task = None
    await _pvs_sessions.shutdown()
    if _gpu_lifecycle is not None:
        await _gpu_lifecycle.shutdown()
    _gpu_lifecycle = None
    _pool_domain = None
    _image_pool = None
    _multiplex_pool = None
    _pvs_pool = None
    shutdown_perfhud_collectors()


def _empty_pool_snapshot() -> dict[str, Any]:
    return {
        "cap": 1,
        "current_size": 0,
        "loaded_keys": [],
        "last_evict": None,
        "builders": 0,
        "reserved_build_slots": 0,
        "borrowers": 0,
        "waiters": 0,
        "cleanup_in_progress": False,
        "cleanup_failed": False,
        "gpu_resident": False,
        "device": None,
        "active_sessions": 0,
    }


def _echo_gpu_health_challenge(request: Request, response: Response) -> None:
    challenge = match_gpu_health_challenge(
        request.headers.getlist(GPU_HEALTH_CHALLENGE_HEADER),
        request.query_params.getlist(GPU_HEALTH_CHALLENGE_QUERY_PARAM),
    )
    if challenge is not None:
        response.headers[GPU_HEALTH_CHALLENGE_HEADER] = challenge
        response.headers["Cache-Control"] = "no-store"


@app.get("/health", dependencies=[Depends(_echo_gpu_health_challenge)])
async def health() -> dict:
    """与 grounded-sam2 /health 字段对齐, 让 AdminDashboard 卡片直接复用渲染."""
    if _gpu_lifecycle is not None:
        aggregate, residency = await _gpu_lifecycle.snapshot_and_residency()
        residency_payload = residency.model_dump(mode="json")
    else:
        aggregate = {
            "pools": {
                "image": _empty_pool_snapshot(),
                "multiplex_video": _empty_pool_snapshot(),
                "pvs_video": _empty_pool_snapshot(),
            }
        }
        residency_payload = None
    image_snapshot = aggregate["pools"]["image"]
    multiplex_snapshot = aggregate["pools"]["multiplex_video"]
    pvs_snapshot = aggregate["pools"]["pvs_video"]

    try:
        available = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        available = False
    gpu_info: dict | None = None
    perf = sample_perfhud()
    if available:
        try:
            free_b, total_b = torch.cuda.mem_get_info()
            # 显存以 pynvml (sample_perfhud) 的设备全局视角为准, 与 yolo-backend 对齐;
            # torch.cuda.mem_get_info() 只反映当前 CUDA 上下文的 free/total, 多进程共享
            # 同一张卡时会系统性低报已用显存. pynvml 不可用时才回落 torch。
            used_mb = perf.get("gpu_memory_used_mb")
            total_mb = perf.get("gpu_memory_total_mb")
            if used_mb is None or total_mb is None:
                used_mb = int((total_b - free_b) / 1024**2)
                total_mb = int(total_b / 1024**2)
            # 本容器自身视角: 单张物理卡身份 + 本进程 torch 已保留显存
            # (caching allocator，不含 ~数百 MB CUDA 上下文)。memory_used_mb 仍是整卡全局。
            gpu_info = {
                "device_name": torch.cuda.get_device_name(0),
                "memory_used_mb": used_mb,
                "memory_total_mb": total_mb,
                "memory_free_mb": max(total_mb - used_mb, 0),
                "process_memory_mb": int(torch.cuda.memory_reserved() / 1024**2),
            }
            gpu_info.update(
                physical_gpu_identity() or {"device_index": torch.cuda.current_device()}
            )
        except Exception:  # noqa: BLE001
            gpu_info = None
    if gpu_info is not None:
        gpu_info["gpu_utilization_percent"] = perf.get("gpu_utilization_percent")
        gpu_info["gpu_temperature_celsius"] = perf.get("gpu_temperature_celsius")
        gpu_info["gpu_power_watts"] = perf.get("gpu_power_watts")
    host = {
        "container_cpu_percent": perf.get("container_cpu_percent"),
        "container_memory_percent": perf.get("container_memory_percent"),
    }
    return {
        "ok": True,
        "gpu": available,
        "gpu_info": gpu_info,
        "host": host,
        "cache": _cache.stats(),
        "model_version": MODEL_VERSION,
        "loaded": image_snapshot["current_size"] > 0,
        # 协议 §4.3 PoolStatus 统一格式; sam3 cap 永远 1.
        "pool": _legacy_image_pool_status(image_snapshot),
        # 视频追踪池 (与图像池并存常驻); 前端据此显示视频追踪已加载 / 预热。
        "video_pool": _legacy_video_pool_status(
            multiplex_snapshot,
            pvs_snapshot,
        ),
        # SAM3 是 GPU-only：effective_device 仅反映已成功加载池的实际设备。
        # 未加载或 GPU 不可用时为 None，不会伪报 CPU fallback。
        "compute": {
            "configured_device": "cuda",
            "effective_device": _effective_compute_device(aggregate),
            "cpu_fallback_supported": False,
        },
        "residency": residency_payload,
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "last_request_age_seconds": round(time.monotonic() - _last_request_at, 2),
    }


@app.get("/setup")
def setup() -> dict:
    # /setup 标准化为 JSON Schema 自描述协议:
    # - name / version / model_version: 必填三元组, 前端用于诊断与兼容判断
    # - supported_prompts: 决定 ToolDock 哪些 AI 工具可用 (M2 ToolDock 重构消费)
    # - params: JSON Schema (Draft-07 子集) — 前端 schema-form 自动渲染参数面板
    base = {
        "protocol_version": PROTOCOL_VERSION,
        "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
        "name": "sam3-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        # 视频追踪权重展示名 (sam3.1_multiplex → "SAM 3.1"), 前端「视频权重」条目用。
        "video_model_version": _VIDEO_MODEL_VERSION,
        # 声明本 backend 支持 POST /warmup (协议 §4.4).
        "warmup_endpoint": True,
        "labels": [],
        "is_interactive": True,
        # 开 inst_interactivity 后宣称 point + interactive_box (SAM-style 单实例
        # 点/框交互, 走 model.predict_inst). "bbox" 已退出交互 prompt 命名空间 (仅几何形状);
        # PCS「找全图相似」统一走 "exemplar" (add_geometric_prompt). text = PCS 文本概念.
        "supported_prompts": [
            "point",
            "interactive_box",
            "mask",
            "scribble",
            "text",
            "exemplar",
        ],
        # exemplar 支持多正负框 + text 组合 + per-request 阈值重过滤的迭代会话.
        # 前端据此把 exemplar 工具从「一发」升级为 refine 会话 (加正框/负框/拖阈值/叠 text).
        "exemplar_capabilities": {
            "multi_box": True,
            "negative_box": True,
            "text_combination": True,
            "threshold_refilter": True,
        },
        "supported_text_outputs": ["box", "mask", "both"],
        # exemplar 走 add_geometric_prompt; state 同时产出 boxes/masks, 三档都支持.
        "supported_geometric_outputs": ["bbox", "polygon", "mask"],
        # sam3.1_multiplex 视频文本追踪 (text-driven, 每帧按文本检测目标)。
        # 平台据 supported_trackers 判 backend 支持视频追踪; text_driven_trackers 让其区分
        # seed-bbox tracker(sam2) 与文本驱动 tracker(sam3, 需 text)。
        "supported_trackers": ["sam3_video", "sam3_video_interactive"],
        "text_driven_trackers": ["sam3_video"],
        # 显式暴露单档 variant, 让模型市场能展示该具体权重 (此前 [] 导致
        # 卡片/列表无法显示「该 backend 加载的是 sam3」). 三个 task 共享同一份权重,
        # variants_shared_across_tasks 在每个 model 上设 True 让列表合并到 1 行。
        "supported_variants": [
            {
                "key": "model_variant",
                "title": "模型版本",
                "description": "SAM 3 图像模型权重 (facebook/sam3, 即 image + inst 交互所用).",
                "variants": [
                    {
                        "value": MODEL_VARIANT,
                        "label": "SAM 3" if MODEL_VARIANT == "sam3" else MODEL_VARIANT,
                        "recommended": True,
                    },
                ],
            },
        ],
        "params": {
            "type": "object",
            "properties": {
                # 可调: PCS 置信度阈值 (text / exemplar 路径)。前端工作台 AI 面板据此渲染滑块,
                # 每位标注员可独立调整; per-request 经 context.score_threshold 覆盖 backend 默认值。
                "score_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": DEFAULT_SCORE_THRESHOLD,
                    "title": "置信度阈值",
                    "x-platform-role": PlatformRole.CONFIDENCE.value,
                    "description": "只保留置信度高于此值的实例。调高=更少更准的框，调低=更多但可能含误检。",
                },
                "simplify_tolerance": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 10.0,
                    "default": DEFAULT_SIMPLIFY_TOLERANCE,
                    "title": "轮廓简化容差(像素)",
                    "x-platform-role": PlatformRole.SIMPLIFY_TOLERANCE.value,
                    "description": "多边形轮廓抽稀强度（像素）。调大=顶点更少、边更直更轻量；调小=更贴合细节但顶点更多。仅影响 mask 输出。",
                },
                "model_variant": {
                    "type": "string",
                    "default": MODEL_VERSION,
                    "title": "模型版本",
                    "x-platform-role": PlatformRole.MODEL_VARIANT.value,
                    "readOnly": True,
                    "description": "当前部署的 SAM 3 模型版本，由后端固定，不可在前端切换。",
                },
                "embedding_cache_size": {
                    "type": "integer",
                    "minimum": 0,
                    "default": EMBEDDING_CACHE_SIZE,
                    "title": "图像缓存容量",
                    "readOnly": True,
                    "description": "后端缓存的图像 embedding 数量上限，启动时设定。命中缓存可跳过重复编码、加速同图多次交互。",
                },
            },
        },
    }
    # 协议 v2: 顶层 infra + 多模型目录 (models[])。
    # SAM 3 的 3 条实际能力 (PCS 路径) 拆成独立 model 条目, 让平台
    # 「协议能力目录」按 task 正确归类:
    #   - detection       (text → bbox, PCS 全图找类相似实例)
    #   - segmentation    (text → mask/polygon, PCS 出 mask 转 polygon)
    #   - interactive_seg (exemplar → bbox/polygon, 示例框 PCS 同类实例)
    # `/predict` 协议不变 (依旧由 context.type / supported_prompts 路径自路由),
    # 顶层 supported_prompts / supported_geometric_outputs 全部保留, 供未迁移平台
    # 向后兼容 (合成隐式单 model 路径)。
    base["infra"] = "pytorch"
    # `default_variants`: 跨 backend 对称声明 (sam3 图像模型只有单档).
    # 即便单值, 前端 VariantSelector 仍按统一规则消费 model.default_variants 拿初值,
    # 避免对"单档 backend"再走特殊分支.
    _default_variants = {"model_variant": MODEL_VARIANT}
    base["models"] = [
        {
            "id": "sam3-detection",
            "display_name": "SAM 3 · 文本检测 (PCS)",
            "task": "detection",
            "model_family": "sam3",
            "infra": "pytorch",
            "is_interactive": False,
            # 纯文本检测, 原子单元。
            "composition": "atom",
            "supported_prompts": ["text"],
            # 文本检测器: 整图 / 父框 crop 上检子物体 (crop-detect 下游)。
            "supported_inputs": ["full_image", "crop"],
            "supported_geometric_outputs": ["bbox"],
            "output_attribute_types": ["class"],
            "resource_profile": {"device": "gpu", "batchable": True},
            "supported_text_outputs": ["box"],
            "supported_variants": base["supported_variants"],
            "variants_shared_across_tasks": True,
            "default_variants": _default_variants,
            "params": base["params"],
        },
        {
            "id": "sam3-segmentation",
            "display_name": "SAM 3 · 文本分割 (PCS)",
            "task": "segmentation",
            "model_family": "sam3",
            "infra": "pytorch",
            "is_interactive": False,
            # 文本→检测→分割一体的内置流程, 非原子。
            "composition": "composite",
            "supported_prompts": ["text"],
            # 文本→分割: 整图 / 父框 crop 上跑 (文本驱动, 内置流程)。
            "supported_inputs": ["full_image", "crop"],
            "supported_geometric_outputs": ["bbox", "polygon"],
            "output_attribute_types": ["class"],
            "resource_profile": {"device": "gpu", "batchable": True},
            "supported_text_outputs": ["mask", "both"],
            "supported_variants": base["supported_variants"],
            "variants_shared_across_tasks": True,
            "default_variants": _default_variants,
            "params": base["params"],
        },
        {
            "id": "sam3-interactive-seg",
            "display_name": "SAM 3 · 交互分割 (点/框/Exemplar)",
            "task": "interactive_seg",
            "model_family": "sam3",
            "infra": "pytorch",
            "is_interactive": True,
            # 单步交互分割, 原子单元。
            # 开 inst 后并入 SAM-style point / interactive_box 单实例交互 (与 exemplar 的
            # PCS 全图相似并列; 三者均走整图、出 polygon)。
            "composition": "atom",
            "supported_prompts": [
                "point",
                "interactive_box",
                "mask",
                "scribble",
                "exemplar",
            ],
            # 交互分割: 点/框/exemplar 提示驱动, 整图 (不作批量 crop/框下游)。
            "supported_inputs": [
                "point_prompt",
                "bbox_prompt",
                "mask_prompt",
                "scribble_prompt",
                "full_image",
            ],
            "supported_geometric_outputs": ["polygon", "mask"],
            # 交互分割: 单实例逐次推理, 不作批量。output_attribute_types 留空 (无类别/置信度产出)。
            "resource_profile": {"device": "gpu", "batchable": False},
            "supported_variants": base["supported_variants"],
            "variants_shared_across_tasks": True,
            "default_variants": _default_variants,
            "params": base["params"],
        },
        {
            # 文本驱动视频追踪 (sam3.1_multiplex)。
            "id": "sam3-video-tracker",
            "display_name": "SAM 3.1 · 视频文本追踪 (Multiplex)",
            "task": "tracker",
            "model_family": "sam3",
            "infra": "pytorch",
            "is_interactive": True,
            # 跨帧有状态 + 文本每帧检测, 内部编排复合。
            "composition": "composite",
            # text-driven: 以文本 query 初始化 (非 bbox 种子)。
            "supported_prompts": ["text"],
            "supported_inputs": ["video", "bbox_prompt"],
            "supported_geometric_outputs": ["bbox", "polygon", "mask"],
            "output_attribute_types": ["class"],
            "resource_profile": {"device": "gpu", "batchable": False},
            "supported_trackers": ["sam3_video"],
            "text_driven_trackers": ["sam3_video"],
            "max_window_frames": MULTIPLEX_MAX_WINDOW_FRAMES,
            "supported_variants": base["supported_variants"],
            "variants_shared_across_tasks": True,
            "default_variants": _default_variants,
            "params": base["params"],
        },
        {
            "id": "sam3-video-interactive-tracker",
            "display_name": "SAM 3 · 视频交互追踪 (PVS)",
            "task": "tracker",
            "model_family": "sam3",
            "infra": "pytorch",
            "is_interactive": True,
            "composition": "composite",
            "supported_prompts": ["point", "interactive_box", "correction_frame"],
            "supported_inputs": [
                "video",
                "point_prompt",
                "bbox_prompt",
                "mask_prompt",
            ],
            "supported_geometric_outputs": ["bbox", "polygon", "mask"],
            "resource_profile": {"device": "gpu", "batchable": False},
            "supported_trackers": ["sam3_video_interactive"],
            "max_window_frames": PVS_MAX_WINDOW_FRAMES,
            "tracker_context_mode": (
                "session" if PVS_MAX_CONTEXT_FRAMES > PVS_MAX_WINDOW_FRAMES else "none"
            ),
            "max_context_frames": PVS_MAX_CONTEXT_FRAMES,
            "supported_variants": base["supported_variants"],
            "variants_shared_across_tasks": True,
            "default_variants": _default_variants,
            "params": base["params"],
        },
    ]
    if MANAGED_LIFECYCLE_VERIFIED:
        base["managed_lifecycle"] = ManagedLifecycleCapabilities().model_dump(
            mode="json"
        )
    return base


@app.get("/versions")
def versions() -> dict:
    return versions_payload(MODEL_VERSION, BACKEND_VERSION)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    update_cache_size(_cache.size())
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/cache/stats")
def cache_stats() -> dict:
    return _cache.stats()


def _validate_body(model_type: Any, body: Any) -> Any:
    try:
        return model_type.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False),
        ) from exc


async def _request_json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="invalid JSON request body"
        ) from exc


@app.post("/unload")
async def unload(request: Request) -> dict[str, Any]:
    """Bodyless legacy unload and signed managed full-pool unload."""

    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return {
            "ok": True,
            "unloaded": False,
            "loaded": False,
            "video_loaded": False,
        }
    raw_body = await request.body()
    if not raw_body.strip():
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return await _gpu_lifecycle.legacy_unload()
    body = _validate_body(
        GenerationTransitionRequest,
        await _request_json(request),
    )
    response = await _gpu_lifecycle.managed_unload(
        body.generation,
        generation_header=generation_header,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/drain")
async def drain(request: Request) -> dict[str, Any]:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(
        GenerationTransitionRequest,
        await _request_json(request),
    )
    response = await _gpu_lifecycle.drain(
        body.generation,
        generation_header=generation_header,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/drain/cancel")
async def cancel_drain(request: Request) -> dict[str, Any]:
    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(
        GenerationTransitionRequest,
        await _request_json(request),
    )
    response = await _gpu_lifecycle.cancel_drain(
        body.generation,
        generation_header=generation_header,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/lifecycle/mode")
async def lifecycle_mode(request: Request) -> dict[str, Any]:
    token = _managed_control_token(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(LifecycleModeRequest, await _request_json(request))
    response = await _gpu_lifecycle.set_mode(
        body,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/lifecycle/reset")
async def lifecycle_reset(request: Request) -> dict[str, Any]:
    token = _managed_control_token(request)
    if _gpu_lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    body = _validate_body(LifecycleResetRequest, await _request_json(request))
    response = await _gpu_lifecycle.reset(
        body,
        token=token,
    )
    return response.model_dump(mode="json")


@app.post("/reload")
async def reload(request: Request) -> dict:
    """主动 (重新) 加载模型. 已加载时是 noop."""
    if _image_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    operation = _request_operation(request)
    cache_hit, _load_ms, _evicted = await _warm_pool(
        _image_pool,
        _POOL_KEY,
        operation,
    )
    return {"ok": True, "loaded": True, "reloaded": not cache_hit}


# 协议 §4.4 · /warmup 端点 (sam3 单档, body 可空).


class WarmupRequest(BaseModel):
    """图像: variants 可选 {model_variant: "sam3"}, 仅校验。
    视频: task="tracker" 预热视频追踪模型 (sam3.1_multiplex, 单档无变体)。"""

    variants: dict[str, str] = {}
    # 平台按 taskType=video 下发 {task:"tracker"} → 预热 multiplex 文本追踪;
    # {task:"interactive"} → 预热 PVS 交互 (点/框) 追踪。
    task: str | None = None


@app.post("/warmup", response_model=WarmupResponse)
async def warmup(
    request: Request,
    req: WarmupRequest | None = None,
) -> WarmupResponse:
    """加载权重到 GPU 不跑 forward。

    默认预热图像模型 (SAM 3 单档, variants.model_variant 必须等于 sam3 或缺省);
    task="tracker" 预热视频追踪模型 (sam3.1_multiplex)。图像 / 视频可并存,
    预热其一不再卸另一。重复预热返回 cache_hit=true。
    """
    operation = _request_operation(request)
    if _image_pool is None or _multiplex_pool is None or _pvs_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    if req is not None and req.task == "tracker":
        pool: ManagedLruPool[str, Any] = _multiplex_pool
        key = _MULTIPLEX_KEY
    elif req is not None and req.task == "interactive":
        pool = _pvs_pool
        key = _PVS_KEY
    else:
        pool = _image_pool
        key = _POOL_KEY
    if req is not None and req.variants:
        mv = req.variants.get("model_variant")
        if mv is not None and mv != MODEL_VERSION:
            raise VariantNotSupportedError("model_variant", mv, [MODEL_VERSION])
    cache_hit, load_ms, evicted = await _warm_pool(pool, key, operation)
    return WarmupResponse(
        ok=True,
        model_load_ms=load_ms,
        cache_hit=cache_hit,
        evicted=evicted,
    )


def _coerce_simplify_tolerance(ctx: dict) -> float | None:
    raw = ctx.get("simplify_tolerance")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"context.simplify_tolerance must be float, got {raw!r}",
        )
    if val < 0:
        raise HTTPException(
            status_code=422, detail="context.simplify_tolerance must be >= 0"
        )
    return val


def _coerce_output(ctx: dict) -> str:
    mode = ctx.get("output", "mask")
    if mode not in ("box", "mask", "both"):
        raise HTTPException(
            status_code=422,
            detail=f"context.output must be one of box|mask|both, got {mode!r}",
        )
    if (
        ctx.get("type") == "exemplar"
        and mode == "both"
        and ctx.get("output_geometry") == "mask"
        and ctx.get("native_mask_companion_boxes") is not True
    ):
        # Older platform APIs require an all-Mask result array. Paired output is
        # enabled only when the caller explicitly accepts companion boxes.
        return "mask"
    return mode


def _coerce_interactive_output(ctx: dict) -> tuple[str, str | None]:
    output_geometry = ctx.get("output_geometry", "polygon")
    if output_geometry not in ("polygon", "mask"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"context.output_geometry must be polygon|mask, got {output_geometry!r}"
            ),
        )
    prompt_revision = ctx.get("prompt_revision")
    if output_geometry == "mask" and (
        not isinstance(prompt_revision, str)
        or not prompt_revision
        or len(prompt_revision) > 256
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "context.prompt_revision must contain 1..256 characters "
                "for output_geometry=mask"
            ),
        )
    return output_geometry, prompt_revision


def _validate_mask_context(ctx: dict) -> Context | None:
    if ctx.get("type") not in {"point", "interactive_box", "mask", "scribble"}:
        return None
    try:
        validated = Context.model_validate(ctx)
        if validated.mask_input is not None:
            decode_low_res_mask(validated.mask_input)
        return validated
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_mask_prompt",
                "message": "interactive Mask prompt failed schema validation",
            },
        ) from exc


def _mask_prompt_payload(context: Context | None) -> dict[str, Any] | None:
    if context is None or context.mask_prompt is None:
        return None
    return context.mask_prompt.model_dump(mode="json")


def _coerce_exemplars(ctx: dict) -> list[dict]:
    """归一 type=exemplar 的几何输入为 [{bbox, label}] 列表。

    优先读多框 `exemplars[]`; 缺省退化单 `bbox` 正框 (旧路径兼容)。每框 bbox 必须长度 4。
    """
    raw = ctx.get("exemplars")
    if raw:
        out: list[dict] = []
        for i, ex in enumerate(raw):
            bbox = ex.get("bbox") if isinstance(ex, dict) else None
            if not bbox or len(bbox) != 4:
                raise HTTPException(
                    status_code=422,
                    detail=f"context.exemplars[{i}].bbox=[x1,y1,x2,y2] required (length 4)",
                )
            out.append({"bbox": bbox, "label": bool(ex.get("label", True))})
        return out

    bbox = ctx.get("bbox")
    if not bbox or len(bbox) != 4:
        raise HTTPException(
            status_code=422,
            detail="type=exemplar requires context.exemplars[] or context.bbox=[x1,y1,x2,y2]",
        )
    return [{"bbox": bbox, "label": True}]


async def _run_executor_to_completion(
    call: Callable[[], Any],
    operation: WorkloadOperation | None,
) -> Any:
    """Keep the real executor owner active across request cancellation."""

    future = asyncio.get_running_loop().run_in_executor(None, call)
    if operation is not None:
        operation.track_future(future)
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
        if cancelled:
            raise asyncio.CancelledError from exc
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _run_prompt_sync(
    p: SAM3Predictor,
    file_path: str,
    ctx: dict,
) -> tuple[list[dict], bool, str | None]:
    """返回 (results, cache_hit, mask_input_next). 命中时 point/bbox/exemplar 跳过 image fetch.

    mask_input_next 仅 point 精修单 mask 阶段非空, 其余 prompt 恒 None。
    """
    ptype = ctx.get("type")
    mask_context = _validate_mask_context(ctx)
    if (
        ptype in {"point", "interactive_box", "mask", "scribble"}
        and mask_context is None
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_mask_prompt",
                "message": "interactive prompt is invalid",
            },
        )
    mask_prompt = _mask_prompt_payload(mask_context)
    cache_key = compute_cache_key(file_path, MODEL_VERSION)
    simplify_tol = _coerce_simplify_tolerance(ctx)
    score_th = ctx.get("score_threshold")

    if ptype == "point":
        if mask_context is None or mask_context.points is None:
            raise HTTPException(
                status_code=422, detail="context.points required for type=point"
            )
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        # inst 单实例点交互 (累加正负点; multimask 候选).
        points = [list(point) for point in mask_context.points]
        labels = list(mask_context.labels or [1] * len(points))
        multimask = mask_context.multimask_output
        image = (
            None
            if _cache.peek(cache_key)
            else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        )
        try:
            return p.predict_interactive(
                image,
                points=points,
                labels=labels,
                multimask_output=multimask,
                mask_input=mask_context.mask_input,
                mask_prompt=mask_prompt,
                cache_key=cache_key,
                simplify_tolerance=simplify_tol,
                output_geometry=output_geometry,
                prompt_revision=prompt_revision,
            )
        except PromptAdapterError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": "invalid_mask_prompt", "message": str(exc)},
            ) from exc

    if ptype == "interactive_box":
        if mask_context is None or mask_context.bbox is None:
            raise HTTPException(
                status_code=422,
                detail="context.bbox=[x1,y1,x2,y2] required for type=interactive_box",
            )
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        # inst 单框单 mask (≠ exemplar 的全图相似; bbox prompt 已退役).
        box = list(mask_context.bbox)
        multimask = mask_context.multimask_output
        image = (
            None
            if _cache.peek(cache_key)
            else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        )
        try:
            return p.predict_interactive(
                image,
                box=box,
                multimask_output=multimask,
                mask_input=mask_context.mask_input,
                mask_prompt=mask_prompt,
                cache_key=cache_key,
                simplify_tolerance=simplify_tol,
                output_geometry=output_geometry,
                prompt_revision=prompt_revision,
            )
        except PromptAdapterError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": "invalid_mask_prompt", "message": str(exc)},
            ) from exc

    if ptype == "mask":
        if mask_context is None or mask_prompt is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "invalid_mask_prompt",
                    "message": "mask prompt is required",
                },
            )
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        image = (
            None
            if _cache.peek(cache_key)
            else fetch_image(
                file_path,
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
            )
        )
        try:
            return p.predict_interactive(
                image,
                mask_input=mask_context.mask_input,
                mask_prompt=mask_prompt,
                cache_key=cache_key,
                simplify_tolerance=simplify_tol,
                output_geometry=output_geometry,
                prompt_revision=prompt_revision,
            )
        except PromptAdapterError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": "invalid_mask_prompt", "message": str(exc)},
            ) from exc

    if ptype == "scribble":
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        if mask_context is None or mask_context.scribbles is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "invalid_scribble_prompt",
                    "message": "scribbles are required",
                },
            )
        image = (
            None
            if _cache.peek(cache_key)
            else fetch_image(
                file_path,
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
            )
        )
        try:
            return p.predict_interactive(
                image,
                scribbles=[
                    stroke.model_dump(mode="json") for stroke in mask_context.scribbles
                ],
                multimask_output=False,
                mask_input=mask_context.mask_input,
                mask_prompt=mask_prompt,
                cache_key=cache_key,
                simplify_tolerance=simplify_tol,
                output_geometry=output_geometry,
                prompt_revision=prompt_revision,
            )
        except PromptAdapterError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": "invalid_scribble_prompt", "message": str(exc)},
            ) from exc

    if ptype == "text":
        text = (ctx.get("text") or "").strip()
        if not text:
            raise HTTPException(
                status_code=422, detail="context.text required for type=text"
            )
        output_mode = _coerce_output(ctx)
        # SAM 3 PCS text 走 image predictor + 缓存; 与 grounded-sam2 (DINO 原图必拉) 不同,
        # 缓存命中时可省 fetch_image. text/exemplar 不回灌 mask_input → 第 3 项恒 None.
        image = (
            None
            if _cache.peek(cache_key)
            else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        )
        results, hit = p.predict_text(
            image,
            text,
            output=output_mode,
            cache_key=cache_key,
            simplify_tolerance=simplify_tol,
            score_threshold=score_th,
        )
        return results, hit, None

    if ptype == "exemplar":
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        # 多正负框 exemplars[] (+ 可选 text 组合) 优先; 缺省退化单 bbox 正框.
        exemplars = _coerce_exemplars(ctx)
        text = (ctx.get("text") or "").strip() or None
        output_mode = _coerce_output(ctx)
        image = (
            None
            if _cache.peek(cache_key)
            else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        )
        results, hit = p.predict_exemplars(
            image,
            exemplars,
            text=text,
            output=output_mode,
            cache_key=cache_key,
            simplify_tolerance=simplify_tol,
            score_threshold=score_th,
            output_geometry=output_geometry,
            prompt_revision=prompt_revision,
        )
        return results, hit, None

    raise HTTPException(status_code=422, detail=f"unsupported context.type: {ptype}")


async def _run_prompt(
    file_path: str,
    ctx: dict,
    operation: WorkloadOperation | None = None,
) -> tuple[list[dict], bool, str | None, bool, int | None]:
    global _last_request_at

    if _image_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    try:
        async with _image_pool.borrow(_POOL_KEY) as lease:
            _last_request_at = time.monotonic()
            result, hit, mask_input_next = await _run_executor_to_completion(
                functools.partial(
                    _run_prompt_sync,
                    lease.resource,
                    file_path,
                    ctx,
                ),
                operation,
            )
            return (
                result,
                hit,
                mask_input_next,
                lease.cache_hit,
                lease.load_ms,
            )
    except ManagedBuildTimeout as exc:
        if operation is not None:
            operation.track_future(exc.builder)
        raise ModelUnavailableError(_POOL_KEY, str(exc)) from exc
    except (ManagedPoolBusyError, DeviceUnavailableError, FileNotFoundError) as exc:
        raise ModelUnavailableError(_POOL_KEY, str(exc)) from exc
    except asyncio.CancelledError:
        if operation is not None:
            operation.track_future(_image_pool.builder_for_now(_POOL_KEY))
        raise


def _observe(prompt_type: str, hit: bool, started: float) -> int:
    elapsed = time.perf_counter() - started
    cache_status = "hit" if hit else "miss"
    record_cache(prompt_type, hit)
    record_inference(prompt_type, cache_status, elapsed)
    update_cache_size(_cache.size())
    return int(elapsed * 1000)


# ── video_tracker 分支 (text-driven sam3_video) ─────────────────────────


def _seed_bbox_from_geometry(geom: Any) -> dict[str, float] | None:
    """把 video tracker 支持的几何统一成有效的归一化 xywh。"""

    def _from_points(points: list) -> dict[str, float] | None:
        xs = [float(p[0]) for p in points if len(p) >= 2]
        ys = [float(p[1]) for p in points if len(p) >= 2]
        if not xs or not ys:
            return None
        return {
            "x": min(xs),
            "y": min(ys),
            "w": max(xs) - min(xs),
            "h": max(ys) - min(ys),
        }

    def _valid(bbox: dict[str, float] | None) -> dict[str, float] | None:
        if bbox is None:
            return None
        values = tuple(bbox.values())
        if not all(math.isfinite(value) for value in values):
            return None
        if bbox["w"] <= 0 or bbox["h"] <= 0:
            return None
        return bbox

    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    if gtype in {"video_track_bbox", "video_track_polygon"}:
        kfs = sorted(
            geom.get("keyframes") or [],
            key=lambda k: int(k.get("frame_index", 0)),
        )
        if not kfs:
            return None
        first = kfs[0]
        if gtype == "video_track_polygon":
            return _valid(_from_points(first.get("points") or []))
        geom = first.get("bbox") or {}
        gtype = "bbox"
    if gtype == "polygon":
        return _valid(_from_points(geom.get("points") or []))
    if gtype == "mask" and isinstance(geom.get("bbox"), dict):
        geom = geom["bbox"]
        gtype = "bbox"
    if gtype in {"bbox", "video_bbox"} or any(
        key in geom for key in ("x", "y", "w", "width")
    ):
        return _valid(
            {
                "x": float(geom.get("x", 0)),
                "y": float(geom.get("y", 0)),
                "w": float(geom.get("w", geom.get("width", 0))),
                "h": float(geom.get("h", geom.get("height", 0))),
            }
        )
    return None


def _seed_bboxes_from_video_ctx(ctx: dict) -> list[dict[str, float]]:
    """收集 multiplex 的续追框；多实例优先使用平台传入的 ``seeds[]``。"""

    raw_seeds = ctx.get("seeds")
    if isinstance(raw_seeds, list):
        bboxes: list[dict[str, float]] = []
        for seed in raw_seeds:
            if not isinstance(seed, dict):
                continue
            bbox = _seed_bbox_from_geometry(seed.get("bbox"))
            if bbox is None:
                bbox = _seed_bbox_from_geometry(seed.get("geometry"))
            if bbox is not None:
                bboxes.append(bbox)
        if bboxes:
            return bboxes

    prompt = ctx.get("prompt")
    if isinstance(prompt, dict):
        seed = _seed_bbox_from_geometry(prompt.get("geometry"))
        if seed is not None:
            return [seed]
    seed = _seed_bbox_from_geometry(ctx.get("source_geometry"))
    return [seed] if seed is not None else []


def _seed_bbox_from_video_ctx(ctx: dict) -> dict[str, float] | None:
    """兼容单 seed 调用者：返回首个有效续追框。"""

    bboxes = _seed_bboxes_from_video_ctx(ctx)
    return bboxes[0] if bboxes else None


def _video_local_path(file_path: str) -> str:
    """video_tracker 的 file_path → OpenCV 可打开的源 (http(s) 先整段下载到临时文件)。"""
    if file_path.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        suffix = os.path.splitext(urlsplit(file_path).path)[-1] or ".mp4"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="sam3vid_src_")
        try:
            with (
                os.fdopen(fd, "wb") as fh,
                httpx.Client(
                    timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True
                ) as client,
                client.stream("GET", file_path) as resp,
            ):
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return tmp_path
    if os.path.isfile(file_path):
        return file_path
    raise HTTPException(
        status_code=400, detail=f"unsupported video file_path: {file_path[:64]}"
    )


def _run_video_tracker_sync(
    tracker: SAM3MultiplexVideoTracker,
    file_path: str,
    from_frame: int,
    to_frame: int,
    direction: str,
    text: str,
    seed_bbox: dict[str, float] | None,
    output_geometry: str,
    seed_bboxes: list[dict[str, float]],
) -> list[dict]:
    local_path = _video_local_path(file_path)
    cleanup = local_path != file_path
    try:
        return tracker.propagate(
            local_path,
            from_frame,
            to_frame,
            direction,
            text,
            seed_bbox,
            output_geometry,
            seed_bboxes,
        )
    finally:
        if cleanup:
            try:
                os.unlink(local_path)
            except OSError:
                pass


async def _run_video_tracker(
    file_path: str,
    ctx: dict,
    operation: WorkloadOperation | None = None,
) -> list[dict]:
    """sam3_video: 按 text 在窗内检测+追踪目标, 返回逐帧几何 (polygon/bbox)。"""
    try:
        from_frame = int(ctx["from_frame"])
        to_frame = int(ctx["to_frame"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="video_tracker requires integer from_frame / to_frame",
        ) from exc
    direction = ctx.get("direction") or "forward"
    if direction not in ("forward", "backward"):
        raise HTTPException(
            status_code=422,
            detail=f"video_tracker direction must be forward|backward, got {direction!r}",
        )
    text = (ctx.get("text") or "").strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail="sam3_video tracker requires context.text (text-driven detection)",
        )
    output_geometry = ctx.get("output_geometry") or "bbox"
    if output_geometry not in ("bbox", "polygon", "mask"):
        raise HTTPException(
            status_code=422,
            detail=f"output_geometry must be bbox|polygon|mask, got {output_geometry!r}",
        )
    seed_bboxes = _seed_bboxes_from_video_ctx(ctx)
    seed_bbox = seed_bboxes[0] if seed_bboxes else None

    global _last_request_at
    if _multiplex_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    try:
        async with _multiplex_pool.borrow(_MULTIPLEX_KEY) as lease:
            _last_request_at = time.monotonic()
            return await _run_executor_to_completion(
                functools.partial(
                    _run_video_tracker_sync,
                    lease.resource,
                    file_path,
                    from_frame,
                    to_frame,
                    direction,
                    text,
                    seed_bbox,
                    output_geometry,
                    seed_bboxes,
                ),
                operation,
            )
    except ManagedBuildTimeout as exc:
        if operation is not None:
            operation.track_future(exc.builder)
        raise ModelUnavailableError(_MULTIPLEX_KEY, str(exc)) from exc
    except (ManagedPoolBusyError, DeviceUnavailableError, FileNotFoundError) as exc:
        raise ModelUnavailableError(_MULTIPLEX_KEY, str(exc)) from exc
    except asyncio.CancelledError:
        if operation is not None:
            operation.track_future(_multiplex_pool.builder_for_now(_MULTIPLEX_KEY))
        raise


def _seeds_from_video_ctx(ctx: dict) -> list[dict]:
    """PVS: 从 context 取逐对象 seed。优先 `seeds[]`(多目标 / 模式 b), 否则退单 seed
    = source_geometry/prompt.geometry(obj_id=1), 与 sam2_video 单目标种子等价。

    seeds[] 每条: {obj_id?, prompts?/bbox?/points?/geometry?}; geometry 走
    _seed_bbox_from_video_ctx 取外接框。points 直接透传 [[x,y,label],...]。缺 obj_id 时按序补
    1..N。prompts = 多帧 [{frame_index, points?/bbox?}], 原样透传给
    wrapper 逐帧播种; 优先于单帧 bbox/points。
    """
    if ctx.get("type") == "correction_frame":
        return [
            {
                "obj_id": int(ctx.get("obj_id", 1)),
                "prompts": [ctx],
            }
        ]

    raw = ctx.get("seeds")
    if isinstance(raw, list) and raw:
        seeds: list[dict] = []
        for i, s in enumerate(raw):
            if not isinstance(s, dict):
                continue
            entry: dict[str, Any] = {"obj_id": int(s.get("obj_id", i + 1))}
            if isinstance(s.get("prompts"), list) and s["prompts"]:
                entry["prompts"] = [
                    CorrectionFramePrompt.model_validate(prompt).model_dump(mode="json")
                    if isinstance(prompt, dict)
                    and prompt.get("type") == "correction_frame"
                    else prompt
                    for prompt in s["prompts"]
                ]
            elif isinstance(s.get("bbox"), dict):
                entry["bbox"] = s["bbox"]
            elif isinstance(s.get("points"), list) and s["points"]:
                entry["points"] = s["points"]
            elif isinstance(s.get("mask_prompt"), dict):
                entry["mask_prompt"] = s["mask_prompt"]
            elif s.get("geometry") is not None:
                b = _seed_bbox_from_video_ctx({"source_geometry": s["geometry"]})
                if b is not None:
                    entry["bbox"] = b
            if (
                "prompts" in entry
                or "bbox" in entry
                or "points" in entry
                or "mask_prompt" in entry
            ):
                seeds.append(entry)
        if seeds:
            return seeds
    seed = _seed_bbox_from_video_ctx(ctx)
    if seed is None:
        return []
    return [{"obj_id": 1, "bbox": seed}]


def _run_pvs_video_tracker_sync(
    tracker: SAM3PVSVideoTracker,
    file_path: str,
    from_frame: int,
    to_frame: int,
    direction: str,
    seeds: list[dict],
    output_geometry: str,
) -> list[dict]:
    local_path = _video_local_path(file_path)
    cleanup = local_path != file_path
    try:
        return tracker.propagate(
            local_path,
            from_frame,
            to_frame,
            direction,
            seeds,
            output_geometry,
        )
    finally:
        if cleanup:
            try:
                os.unlink(local_path)
            except OSError:
                pass


async def _run_pvs_video_tracker(
    file_path: str,
    ctx: dict,
    operation: WorkloadOperation | None = None,
) -> tuple[list[dict], dict | None]:
    """sam3_video_interactive: 点/框 seed + memory 逐对象跨帧追踪, 返回逐帧逐对象几何。"""
    try:
        from_frame = int(ctx["from_frame"])
        to_frame = int(ctx["to_frame"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="video_tracker requires integer from_frame / to_frame",
        ) from exc
    direction = ctx.get("direction") or "forward"
    if direction not in ("forward", "backward"):
        raise HTTPException(
            status_code=422,
            detail=f"video_tracker direction must be forward|backward, got {direction!r}",
        )
    output_geometry = ctx.get("output_geometry") or "bbox"
    if output_geometry not in ("bbox", "polygon", "mask"):
        raise HTTPException(
            status_code=422,
            detail=f"output_geometry must be bbox|polygon|mask, got {output_geometry!r}",
        )
    global _last_request_at
    if _pvs_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    raw_control = ctx.get("tracker_context")
    if raw_control is not None:
        control = TrackerContextControl.model_validate(raw_control)
        binding = (control.job_id, direction, _PVS_KEY)
        try:
            if control.action == "close":
                await _pvs_sessions.close(control.token or "", binding)
                return [], {"tracker_context": {"mode": "session", "closed": True}}

            seeds = _seeds_from_video_ctx(ctx) if control.action == "start" else []
            if control.action == "start":
                if not seeds:
                    raise HTTPException(
                        status_code=422,
                        detail="sam3_video_interactive tracker requires a seed",
                    )
                local_path = _video_local_path(file_path)
                cleanup = local_path != file_path
                try:
                    token = await _pvs_sessions.start(
                        binding=binding,
                        borrow=lambda: _pvs_pool.borrow(_PVS_KEY),
                        open_session=lambda lease: _run_executor_to_completion(
                            functools.partial(
                                lease.resource.start_session,
                                local_path,
                                control.span_start_frame,
                                control.span_end_frame,
                                direction,
                                seeds,
                            ),
                            operation,
                        ),
                        close_session=lambda lease, state: _run_executor_to_completion(
                            functools.partial(lease.resource.close_session, state), None
                        ),
                    )
                finally:
                    if cleanup:
                        os.unlink(local_path)
            else:
                token = control.token or ""
            result = await _pvs_sessions.call(
                token,
                binding,
                lambda lease, state: _run_executor_to_completion(
                    functools.partial(
                        lease.resource.continue_session,
                        state,
                        from_frame,
                        to_frame,
                        output_geometry=output_geometry,
                    ),
                    operation,
                ),
            )
            return result, {"tracker_context": {"mode": "session", "token": token}}
        except TrackerSessionLost as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": "tracker_context_lost"},
            ) from exc

    seeds = _seeds_from_video_ctx(ctx)
    if not seeds:
        raise HTTPException(
            status_code=422,
            detail="sam3_video_interactive tracker requires a seed (source_geometry / seeds[])",
        )
    try:
        async with _pvs_pool.borrow(_PVS_KEY) as lease:
            _last_request_at = time.monotonic()
            result = await _run_executor_to_completion(
                functools.partial(
                    _run_pvs_video_tracker_sync,
                    lease.resource,
                    file_path,
                    from_frame,
                    to_frame,
                    direction,
                    seeds,
                    output_geometry,
                ),
                operation,
            )
            return result, None
    except ManagedBuildTimeout as exc:
        if operation is not None:
            operation.track_future(exc.builder)
        raise ModelUnavailableError(_PVS_KEY, str(exc)) from exc
    except (ManagedPoolBusyError, DeviceUnavailableError, FileNotFoundError) as exc:
        raise ModelUnavailableError(_PVS_KEY, str(exc)) from exc
    except asyncio.CancelledError:
        if operation is not None:
            operation.track_future(_pvs_pool.builder_for_now(_PVS_KEY))
        raise


@app.post("/predict")
async def predict(request: Request):
    operation = _request_operation(request)
    body = await request.json()
    started = time.perf_counter()

    if isinstance(body, dict) and "task" in body and "context" in body:
        task = body["task"]
        ctx = _normalize_predict_context(body.get("context") or {})
        # video_tracker 走独立视频模型分支 (与图像 prompt 路径分流)。
        # 按 model_key 分派: sam3_video_interactive → PVS 点/框 memory 追踪,
        # 否则 (sam3_video) → multiplex 文本追踪。
        if ctx.get("type") == "video_tracker":
            correction = (ctx.get("prompt") or {}).get("correction") or {}
            model_role = (
                "sam3_pvs"
                if ctx.get("model_key") == "sam3_video_interactive"
                else "sam3_multiplex"
            )
            tracker_started = time.perf_counter()
            try:
                if ctx.get("model_key") == "sam3_video_interactive":
                    result, tracker_meta = await _run_pvs_video_tracker(
                        task["file_path"],
                        ctx,
                        operation,
                    )
                else:
                    result = await _run_video_tracker(
                        task["file_path"],
                        ctx,
                        operation,
                    )
            except Exception:
                record_mask_ai_backend_inference(
                    model_role=model_role,
                    operation="correction" if correction else "tracking",
                    fallback_reason=correction.get("fallback_reason"),
                    candidate_count=0,
                    outcome="error",
                    duration_seconds=time.perf_counter() - tracker_started,
                )
                raise
            record_mask_ai_backend_inference(
                model_role=model_role,
                operation="correction" if correction else "tracking",
                fallback_reason=correction.get("fallback_reason"),
                candidate_count=len(result),
                outcome="success",
                duration_seconds=time.perf_counter() - tracker_started,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return PredictionResult(
                result=result,
                score=None,
                model_version=MODEL_VERSION,
                inference_time_ms=elapsed_ms,
                meta=tracker_meta
                if ctx.get("model_key") == "sam3_video_interactive"
                else None,
                cache_hit=False,
                model_load_ms=None,
            ).model_dump(exclude_none=True)
        result, hit, mask_input_next, pool_cache_hit, model_load_ms = await _run_prompt(
            task["file_path"], ctx, operation
        )
        elapsed_ms = _observe(ctx.get("type") or "unknown", hit, started)
        return PredictionResult(
            result=result,
            score=max((r.get("score") or 0.0) for r in result) if result else None,
            model_version=MODEL_VERSION,
            inference_time_ms=elapsed_ms,
            cache_hit=pool_cache_hit,
            model_load_ms=model_load_ms,
            mask_input_next=mask_input_next,
            diagnostic=(
                MaskInteractionDiagnostic(reason="empty_mask")
                if ctx.get("type")
                in (
                    "point",
                    "interactive_box",
                    "mask",
                    "scribble",
                    "exemplar",
                )
                and ctx.get("output_geometry", "polygon") == "mask"
                and not result
                else None
            ),
        ).model_dump(exclude_none=True)

    if isinstance(body, dict) and "tasks" in body:
        tasks = body["tasks"]
        ctx = _normalize_predict_context(
            body.get("context") or {"type": "text", "text": body.get("text", "")}
        )
        # 文本批量按 model_id 路由输出形态 (统一 wire): detection→box, segmentation→
        # ctx.output||mask。无 model_id 回落 ctx.output (老 wire 兼容)。type 强制 text 走文本分支。
        _mid = ctx.get("model_id")
        if _mid == "sam3-detection":
            ctx = {**ctx, "type": "text", "output": "box"}
        elif _mid == "sam3-segmentation":
            ctx = {**ctx, "type": "text", "output": ctx.get("output", "mask")}
        if _image_pool is None:
            raise HTTPException(status_code=503, detail="backend not ready")
        pool_cache_hit, model_load_ms, _evicted = await _warm_pool(
            _image_pool,
            _POOL_KEY,
            operation,
        )
        results = []
        for t in tasks:
            t_started = time.perf_counter()
            try:
                # 文本批量不回灌 mask_input → 丢弃 mask_input_next.
                result, hit, _, _entry_hit, _entry_load_ms = await _run_prompt(
                    t["file_path"],
                    ctx,
                    operation,
                )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("predict failed for task=%s: %s", t.get("id"), exc)
                result, hit = [], False
            elapsed_ms = _observe(ctx.get("type") or "unknown", hit, t_started)
            results.append(
                PredictionResult(
                    task=t.get("id"),
                    result=result,
                    score=max((r.get("score") or 0.0) for r in result)
                    if result
                    else None,
                    model_version=MODEL_VERSION,
                    inference_time_ms=elapsed_ms,
                    # 整批共享同一懒加载结果: 第一条 task 触发, 后续都是 True/None.
                    cache_hit=pool_cache_hit,
                    model_load_ms=model_load_ms,
                ).model_dump(exclude_none=True)
            )
        return BatchPredictResponse(results=results).model_dump(exclude_none=True)

    raise HTTPException(
        status_code=422, detail="body must contain 'task'+'context' or 'tasks'"
    )
