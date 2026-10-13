"""Grounded-SAM-2 ML Backend — FastAPI 入口.

实现 docs-site/dev/ml-backend-protocol.md 规定的 4 个端点 + 2 个观测端点:
    GET  /health        探活
    GET  /setup         模型配置
    GET  /versions      可用版本
    POST /predict       交互式 / 批量预测（同一端点按 body shape 分流）
    GET  /metrics       Prometheus exposition
    GET  /cache/stats   embedding cache 当前状态

prompt 类型:
    - context.type == "point"           → SAM 直接出 mask (正/负点累加; multimask 候选)
    - context.type == "interactive_box" → SAM 单框单 mask (旧 "bbox" 改名)
    - context.type == "text"            → GroundingDINO 出 boxes → SAM 出 mask（可批量）
    注: "bbox" 已退出交互 prompt 命名空间 (旧 type=bbox 落 422); tracker / box-seg 的 bbox
    是几何输入/追踪种子, 走 geometry-prompt 批量路径, 与此无关.

SAM 2 image embedding LRU 缓存:
    cache_key = sha1(url_path|sam_variant); 同图二次操作跳过 ~1.5s 的 image encoder.
    point/interactive_box 命中可同时跳过 fetch_image; text 仅省 set_image (DINO 仍需原图).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import Any, Callable

import httpx
import torch
from aap_backend_runtime import (
    deployment_verified_flag,
    effective_device_value,
    fetch_image,
    physical_gpu_identity,
    versions_payload,
    validate_single_gpu_device_set,
    TrackerSessionLost,
    TrackerSessionManager,
)
from aap_protocol_v2 import (
    COMPAT_PROTOCOL_VERSIONS,
    CorrectionFramePrompt,
    PROTOCOL_VERSION,
    ModelUnavailableError,
    MaskInteractionDiagnostic,
    PlatformRole,
    VariantNotSupportedError,
    decode_low_res_mask,
    log_deprecated_model_variant_fields,
    normalize_context_model_variants,
    TrackerContextControl,
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
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from embedding_cache import EmbeddingCache, compute_cache_key
from gpu_lifecycle import GroundedSam2GpuLifecycle, WorkloadOperation
from model_pool import (
    ModelBuildTimeout,
    ModelPool,
    ModelPoolBusyError,
)
from observability import (
    init_perfhud_collectors,
    record_cache,
    record_inference,
    record_mask_ai_backend_inference,
    record_video_tracker,
    sample_perfhud,
    shutdown_perfhud_collectors,
    update_cache_size,
)
from mask_utils import PromptAdapterError
from predictor import (
    CHECKPOINT_DIR,
    DEFAULT_SIMPLIFY_TOLERANCE,
    DINO_CONFIGS,
    SAM2_CONFIGS,
    GroundedSAM2Predictor,
)
from pool_domain import GroundedSam2Pools
from schemas import (
    BatchPredictResponse,
    Context,
    PredictionResult,
    WarmupRequest,
    WarmupResponse,
)
from video_pool import VideoBuildTimeout, VideoPool, VideoPoolBusyError
from video_predictor import SAM2VideoTracker
from video_predictor import (
    DEFAULT_MAX_CONTEXT_FRAMES as VIDEO_TRACKER_MAX_CONTEXT_FRAMES,
)

logger = logging.getLogger("grounded-sam2-backend")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

SAM_VARIANT = os.getenv("SAM_VARIANT", "tiny")
DINO_VARIANT = os.getenv("DINO_VARIANT", "T")
BOX_THRESHOLD = float(os.getenv("BOX_THRESHOLD", "0.35"))
TEXT_THRESHOLD = float(os.getenv("TEXT_THRESHOLD", "0.25"))
IMAGE_DOWNLOAD_TIMEOUT = float(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "30"))
EMBEDDING_CACHE_SIZE = int(os.getenv("EMBEDDING_CACHE_SIZE", "16"))
# B-28+ · idle 自动卸载. 0 / 负数 关闭定时卸载, 仍可通过 POST /unload 手动卸载.
IDLE_UNLOAD_SECONDS = float(os.getenv("IDLE_UNLOAD_SECONDS", "600"))
IDLE_CHECK_INTERVAL = float(os.getenv("IDLE_CHECK_INTERVAL", "60"))
# ModelPool 配置. CAP=1 保持"单变体常驻"行为; 大显存卡可调高并存多变体.
MODEL_POOL_CAP = int(os.getenv("MODEL_POOL_CAP", "1"))
MODEL_POOL_BUILD_TIMEOUT = float(os.getenv("MODEL_POOL_BUILD_TIMEOUT", "30"))
# sam2_video tracker 独立显存池 (与图片池预算分离, 互不驱逐).
VIDEO_MODEL_POOL_CAP = int(os.getenv("VIDEO_MODEL_POOL_CAP", "1"))
VIDEO_MODEL_POOL_BUILD_TIMEOUT = float(
    os.getenv("VIDEO_MODEL_POOL_BUILD_TIMEOUT", "60")
)
# 单次 init_state 安全上限 (帧); 超此值的窗口直接拒绝, 防显存灌爆.
VIDEO_TRACKER_MAX_WINDOW_FRAMES = int(
    os.getenv("VIDEO_TRACKER_MAX_WINDOW_FRAMES", "300")
)
# video 池独立 idle 卸载 (与图片池 IDLE_UNLOAD_SECONDS 各自计时, 不连带).
VIDEO_IDLE_UNLOAD_SECONDS = float(os.getenv("VIDEO_IDLE_UNLOAD_SECONDS", "600"))
# Code support and deployment verification are separate.  The backend does not
# advertise or enter enforce mode until real-card load/unload evidence exists.
MANAGED_LIFECYCLE_VERIFIED = deployment_verified_flag(
    "GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED"
)
MAX_PREDICT_REQUEST_BYTES = 6 * 1024 * 1024

# /setup 协议标准化暴露 backend 镜像版本 (与 FastAPI app.version 同源).
BACKEND_VERSION = os.getenv("BACKEND_VERSION", "0.10.1")

app = FastAPI(title="grounded-sam2-backend", version=BACKEND_VERSION)


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
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    return JSONResponse(status_code=400, content={"detail": str(exc)})


_last_request_at: float = time.monotonic()
_idle_task: asyncio.Task | None = None
# 额外变体 checkpoint 后台预拉状态 (主变体已由 entrypoint 阻塞下好).
# status: idle(无额外变体) | downloading | ready | partial(部分失败) | error.
_prefetch_task: asyncio.Task | None = None
_provisioning: dict = {"status": "idle", "detail": ""}


async def _prefetch_extras() -> None:
    """后台 subprocess 跑 download_checkpoints.py prefetch, 把额外变体边服务边补下来."""
    global _provisioning
    _provisioning = {"status": "downloading", "detail": "fetching prefetch variants"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "python",
            os.path.join(
                os.path.dirname(__file__), "scripts", "download_checkpoints.py"
            ),
            "prefetch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        tail = (out or b"").decode(errors="replace").strip().splitlines()
        last = tail[-1] if tail else ""
        if proc.returncode == 0:
            # 脚本对额外变体是 best-effort (失败仅 warn); 有 [warn] 即 partial.
            partial = any("[warn]" in line for line in tail)
            _provisioning = {
                "status": "partial" if partial else "ready",
                "detail": last,
            }
        else:
            _provisioning = {
                "status": "error",
                "detail": last or f"exit {proc.returncode}",
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("prefetch extras failed")
        _provisioning = {"status": "error", "detail": str(exc)}
    logger.info("prefetch extras done: %s", _provisioning)


def _build_predictor(
    sam_variant: str, dino_variant: str, cache: EmbeddingCache
) -> GroundedSAM2Predictor:
    """pool 的 build 回调 (在 executor 内同步执行)."""
    return GroundedSAM2Predictor(
        sam_variant=sam_variant,
        dino_variant=dino_variant,
        box_threshold=BOX_THRESHOLD,
        text_threshold=TEXT_THRESHOLD,
        embedding_cache=cache,
    )


def _strict_free_gpu_memory() -> None:
    """Release managed CUDA allocator state without hiding a failed cleanup."""

    if not torch.cuda.is_available() or not torch.cuda.is_initialized():
        return
    torch.cuda.synchronize()
    clear_cublas_workspaces = getattr(
        torch._C,  # noqa: SLF001 - PyTorch exposes no public equivalent
        "_cuda_clearCublasWorkspaces",
        None,
    )
    if callable(clear_cublas_workspaces):
        clear_cublas_workspaces()
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()


def _require_model_file(path: str, *, role: str) -> None:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{role} not provisioned: {path}")


def _preflight_image_model(sam_variant: str, dino_variant: str) -> None:
    _sam_config, sam_checkpoint = SAM2_CONFIGS[sam_variant]
    dino_config, dino_checkpoint = DINO_CONFIGS[dino_variant]
    dino_config_path = os.getenv(
        "DINO_CONFIG_PATH",
        f"/app/vendor/grounded-sam-2/grounding_dino/groundingdino/config/{dino_config}",
    )
    _require_model_file(
        os.path.join(CHECKPOINT_DIR, sam_checkpoint),
        role="SAM checkpoint",
    )
    _require_model_file(dino_config_path, role="GroundingDINO config")
    _require_model_file(
        os.path.join(CHECKPOINT_DIR, dino_checkpoint),
        role="GroundingDINO checkpoint",
    )


def _preflight_video_model(sam_variant: str) -> None:
    _sam_config, sam_checkpoint = SAM2_CONFIGS[sam_variant]
    _require_model_file(
        os.path.join(CHECKPOINT_DIR, sam_checkpoint),
        role="SAM video checkpoint",
    )


_pool: ModelPool | None = None


def _build_video_tracker(sam_variant: str) -> SAM2VideoTracker:
    """video 池的 build 回调 (在 executor 内同步执行)."""
    return SAM2VideoTracker(
        sam_variant=sam_variant,
        max_window_frames=VIDEO_TRACKER_MAX_WINDOW_FRAMES,
    )


_video_pool: VideoPool | None = None
_video_sessions = TrackerSessionManager(ttl_seconds=300.0)
_pool_domain: GroundedSam2Pools | None = None
_gpu_lifecycle: GroundedSam2GpuLifecycle | None = None
_video_idle_task: asyncio.Task | None = None


def _model_version(sam_variant: str, dino_variant: str) -> str:
    return f"grounded-sam2-dino{dino_variant}-sam2.1{sam_variant}"


# 不含 vram_gb (与 yolo SIZE_META 同理): 粗估占位不准确, 实际 SAM2 .pt
# 加载远低于声称值且推理峰值还受 batch / 分辨率 / FP16 影响. tier (fast / balanced /
# accurate) 作为选购粗粒度档位保留, note 给出语义说明。
SAM2_VARIANT_METADATA = {
    "tiny": {
        "label": "SAM 2.1 Tiny",
        "tier": "fast",
        "note": "最快冷启动，适合快速框选和资源紧张的显卡。",
    },
    "small": {
        "label": "SAM 2.1 Small",
        "tier": "balanced",
        "recommended": True,
        "note": "速度和轮廓质量的默认推荐折中。",
    },
    "base_plus": {
        "label": "SAM 2.1 Base+",
        "tier": "accurate",
        "note": "更稳的细节边界，冷加载和显存占用更高。",
    },
    "large": {
        "label": "SAM 2.1 Large",
        "tier": "accurate",
        "note": "最高精度档，建议大显存环境按需预热。",
    },
}

DINO_VARIANT_METADATA = {
    "T": {
        "label": "GroundingDINO Swin-T",
        "tier": "fast",
        "recommended": True,
        "note": "文本检测默认推荐档，速度优先。",
    },
    "B": {
        "label": "GroundingDINO Swin-B",
        "tier": "accurate",
        "note": "文本检测更准，显存和冷启动成本更高。",
    },
}


def _variant_options(
    configs: dict[str, tuple[str, str]], metadata: dict[str, dict]
) -> list[dict]:
    """Build rich variant options from the same keys used for runtime validation."""
    return [{"value": key, **metadata.get(key, {"label": key})} for key in configs]


def _supported_variants() -> list[dict]:
    return [
        _sam_variant_axis(),
        _dino_variant_axis(),
    ]


def _sam_variant_axis() -> dict:
    return {
        "key": "sam_variant",
        "title": "SAM 2 变体",
        "description": "分割模型尺寸。越大通常越精细，但冷加载更慢、显存占用更高。",
        "variants": _variant_options(SAM2_CONFIGS, SAM2_VARIANT_METADATA),
    }


def _dino_variant_axis() -> dict:
    return {
        "key": "dino_variant",
        "title": "GroundingDINO 变体",
        "description": "文本检测模型尺寸。T 更快，B 更准更吃资源。",
        "variants": _variant_options(DINO_CONFIGS, DINO_VARIANT_METADATA),
    }


# 默认变体的 model_version, 供 /setup / /versions 等"无请求上下文"的端点使用.
MODEL_VERSION = _model_version(SAM_VARIANT, DINO_VARIANT)


def _resolve_variant(ctx: dict) -> tuple[str, str]:
    """从 context 读请求级变体, 缺省回退全局 env 默认; 非法值 422."""
    ctx = _normalize_predict_context(ctx)
    model_variants = ctx.get("model_variants") or {}
    sv = model_variants.get("sam_variant") or SAM_VARIANT
    dv = model_variants.get("dino_variant") or DINO_VARIANT
    if sv not in SAM2_CONFIGS:
        raise VariantNotSupportedError("sam_variant", sv, sorted(SAM2_CONFIGS))
    if dv not in DINO_CONFIGS:
        raise VariantNotSupportedError("dino_variant", dv, sorted(DINO_CONFIGS))
    return sv, dv


def _resolve_video_variant(ctx: dict) -> str:
    ctx = _normalize_predict_context(ctx)
    model_variants = ctx.get("model_variants") or {}
    sv = model_variants.get("sam_variant") or SAM_VARIANT
    if sv not in SAM2_CONFIGS:
        raise VariantNotSupportedError("sam_variant", sv, sorted(SAM2_CONFIGS))
    return sv


def _normalize_predict_context(ctx: dict) -> dict:
    try:
        normalized, deprecated = normalize_context_model_variants(ctx)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 同一请求 ctx 常被图像 (_resolve_variant) 与视频 (_resolve_video_variant)
    # 两条路径各 normalize 一次; deprecation warning 只在首次记 (标记写回输入 ctx),
    # 消除重复日志噪声。私有标记键不影响 normalize 结果与 422 校验。
    if deprecated and not ctx.get("_mv_deprecation_logged"):
        log_deprecated_model_variant_fields(logger, deprecated)
        ctx["_mv_deprecation_logged"] = True
    return normalized


def _model_key(sam_variant: str, dino_variant: str) -> str:
    return f"sam={sam_variant}/dino={dino_variant}"


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


async def _idle_watcher() -> None:
    """Keep the image pool's idle threshold independent from the video pool."""
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if IDLE_UNLOAD_SECONDS <= 0 or _gpu_lifecycle is None:
                continue
            idle_before = time.monotonic() - IDLE_UNLOAD_SECONDS
            count = await _gpu_lifecycle.try_idle_unload(
                "image",
                idle_before=idle_before,
            )
            if count:
                logger.info("idle unloaded %d image variants", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("idle watcher loop error; continuing")


async def _video_idle_watcher() -> None:
    """周期检查 video 池 idle; 独立于图片池 idle watcher, 各自计时不连带清空."""
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            await _video_sessions.expire()
            if VIDEO_IDLE_UNLOAD_SECONDS <= 0 or _gpu_lifecycle is None:
                continue
            idle_before = time.monotonic() - VIDEO_IDLE_UNLOAD_SECONDS
            count = await _gpu_lifecycle.try_idle_unload(
                "video",
                idle_before=idle_before,
            )
            if count:
                logger.info("idle unloaded %d video variants", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("video idle watcher loop error; continuing")


@app.on_event("startup")
async def _load_models() -> None:
    global _gpu_lifecycle, _idle_task, _last_request_at, _pool, _pool_domain
    global _prefetch_task, _video_idle_task, _video_pool
    validate_single_gpu_device_set()
    raw_keyring = os.getenv("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "").strip()
    verify_keyring = load_verify_keyring(raw_keyring) if raw_keyring else {}
    build_serial_lock = asyncio.Lock()
    _pool = ModelPool(
        cap=MODEL_POOL_CAP,
        build_predictor=_build_predictor,
        free_gpu_memory=_strict_free_gpu_memory,
        embedding_cache_size=EMBEDDING_CACHE_SIZE,
        preflight_model=_preflight_image_model,
        build_timeout=MODEL_POOL_BUILD_TIMEOUT,
        build_serial_lock=build_serial_lock,
    )
    _video_pool = VideoPool(
        cap=VIDEO_MODEL_POOL_CAP,
        build_tracker=_build_video_tracker,
        free_gpu_memory=_strict_free_gpu_memory,
        preflight_model=_preflight_video_model,
        build_timeout=VIDEO_MODEL_POOL_BUILD_TIMEOUT,
        idle_unload_seconds=VIDEO_IDLE_UNLOAD_SECONDS,
        build_serial_lock=build_serial_lock,
    )
    _pool_domain = GroundedSam2Pools(_pool, _video_pool)
    _gpu_lifecycle = GroundedSam2GpuLifecycle(
        _pool_domain,
        verify_keyring=verify_keyring,
        evictable_verified=MANAGED_LIFECYCLE_VERIFIED,
    )
    logger.info(
        "loading default variant: dino=%s sam=%s box_th=%.2f text_th=%.2f "
        "cache_size=%d pool_cap=%d idle_unload=%.0fs managed_verified=%s",
        DINO_VARIANT,
        SAM_VARIANT,
        BOX_THRESHOLD,
        TEXT_THRESHOLD,
        EMBEDDING_CACHE_SIZE,
        MODEL_POOL_CAP,
        IDLE_UNLOAD_SECONDS,
        MANAGED_LIFECYCLE_VERIFIED,
    )
    # 不再启动急加载默认变体: 未注册 / 无流量时会白占显存 (与下方 video 池同理).
    # 纯懒加载 — 首个推理请求经 _get_predictor → _pool.get 触发冷启; 需暖启点模型市场「预热默认」。
    _last_request_at = time.monotonic()
    # PerfHud · pynvml + psutil 初始化 (无 GPU 环境会降级, 不阻塞 startup)
    init_perfhud_collectors()
    if IDLE_UNLOAD_SECONDS > 0:
        _idle_task = asyncio.create_task(_idle_watcher())
    # video 池独立 idle watcher (不与图片池连带). 不预热 video 变体:
    # 首个 video_tracker 请求触发冷启, 避免空载常驻额外显存.
    if VIDEO_IDLE_UNLOAD_SECONDS > 0:
        _video_idle_task = asyncio.create_task(_video_idle_watcher())
    # 默认变体走纯懒加载 (首个推理请求才冷启, 见上); 此处仅把额外 PREFETCH 变体的 checkpoint
    # 在后台下载补齐 (_prefetch_extras 只下权重、不加载模型), 不阻塞 uvicorn / /health.
    _prefetch_task = asyncio.create_task(_prefetch_extras())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _gpu_lifecycle, _idle_task, _pool, _pool_domain, _prefetch_task
    global _video_idle_task, _video_pool
    for task_name in ("_idle_task", "_prefetch_task", "_video_idle_task"):
        task = globals()[task_name]
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            globals()[task_name] = None
    await _video_sessions.shutdown()
    if _gpu_lifecycle is not None:
        await _gpu_lifecycle.shutdown()
    _gpu_lifecycle = None
    _pool_domain = None
    _pool = None
    _video_pool = None
    shutdown_perfhud_collectors()


def _legacy_pool_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    loaded_keys = [
        {key: value for key, value in item.items() if key != "borrowers"}
        for item in snapshot["loaded_keys"]
    ]
    return {
        "cap": snapshot["cap"],
        "current_size": snapshot["current_size"],
        "loaded_keys": loaded_keys,
        "last_evict": snapshot["last_evict"],
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
    """GPU 显存 + cache 指标，便于运维实时观察。

    旧前端字段保留：`gpu` 仍是 truthy（True/False），`model_version` / `loaded` 不变；
    新增 `gpu_info` / `cache` 子对象，老前端忽略。
    """
    if _gpu_lifecycle is not None and _pool is not None:
        aggregate, residency = await _gpu_lifecycle.snapshot_and_residency()
        image_snapshot = aggregate["pools"]["image"]
        video_snapshot = aggregate["pools"]["video"]
        cache = await _pool.aggregate_cache_stats()
        residency_payload = residency.model_dump(mode="json")
    else:
        image_snapshot = {
            "cap": MODEL_POOL_CAP,
            "current_size": 0,
            "loaded_keys": [],
            "last_evict": None,
            "device": None,
        }
        video_snapshot = {
            "cap": VIDEO_MODEL_POOL_CAP,
            "current_size": 0,
            "loaded_keys": [],
            "last_evict": None,
            "active_sessions": 0,
            "idle_seconds": 0.0,
            "idle_unload_seconds": VIDEO_IDLE_UNLOAD_SECONDS,
            "device": None,
        }
        cache = {"size": 0, "hits": 0, "misses": 0, "hit_rate": 0.0, "buckets": {}}
        residency_payload = None

    try:
        available = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 — CUDA 运行时损坏不应拖垮 /health
        available = False
    gpu_info: dict | None = None
    # PerfHud · 同步采样 GPU util/温度/功耗 + 容器 CPU/RAM (无 GPU 环境字段为 None)
    perf = sample_perfhud()
    if available:
        try:
            free_b, total_b = torch.cuda.mem_get_info()
            # 显存以 pynvml (sample_perfhud) 的设备全局视角为准, 与 yolo-backend 对齐;
            # torch.cuda.mem_get_info() 只反映当前 CUDA 上下文的 free/total, 多进程共享
            # 同一张卡时会系统性低报已用显存 (观测面板比实际少几百 MB). pynvml 不可用
            # 时才回落 torch。
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
        except Exception:  # noqa: BLE001 — 显存查询失败不阻塞 /health
            gpu_info = None
    if gpu_info is not None:
        gpu_info["gpu_utilization_percent"] = perf["gpu_utilization_percent"]
        gpu_info["gpu_temperature_celsius"] = perf["gpu_temperature_celsius"]
        gpu_info["gpu_power_watts"] = perf["gpu_power_watts"]
    host = {
        "container_cpu_percent": perf["container_cpu_percent"],
        "container_memory_percent": perf["container_memory_percent"],
    }
    return {
        "ok": True,
        "gpu": available,
        "gpu_info": gpu_info,
        "host": host,
        "cache": cache,
        "model_version": MODEL_VERSION,
        "loaded": image_snapshot["current_size"] > 0,
        # 协议 §4.3 PoolStatus (cap/current_size/loaded_keys[]/last_evict).
        "pool": _legacy_pool_status(image_snapshot),
        # video tracker 独立池同样使用 PoolStatus.loaded_keys 协议形态.
        "video_pool": {
            **_legacy_pool_status(video_snapshot),
            "active_sessions": video_snapshot["active_sessions"],
            "idle_seconds": video_snapshot["idle_seconds"],
            "idle_unload_seconds": video_snapshot["idle_unload_seconds"],
        },
        "provisioning": _provisioning,
        # 五镜像统一有效设备观测 (torch 系 effective_device / ORT 系 effective_provider)。
        # configured_device = 环境配置 (本 backend 不读 *_DEVICE env, predictor 锁 "cuda");
        # effective_device = 真实探测生效设备 (None=尚未加载, "cpu"=GPU 配置但已静默退回,
        # 供观测「GPU 静默退化」根因排查)。
        "compute": {
            "configured_device": "cuda",
            "effective_device": effective_device_value(),
            "pool_devices": {
                "image": image_snapshot["device"],
                "video": video_snapshot["device"],
            },
            "cpu_fallback_supported": True,
        },
        "residency": residency_payload,
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "last_request_age_seconds": round(time.monotonic() - _last_request_at, 2),
    }


@app.get("/setup")
def setup() -> dict:
    # /setup 标准化为 JSON Schema 自描述协议 (与 sam3-backend 同构):
    # - name / version / model_version: 必填三元组, 前端用于诊断与兼容判断
    # - supported_prompts: 决定 ToolDock 哪些 AI 工具可用 (M2 ToolDock 重构消费)
    # - params: JSON Schema (Draft-07 子集) — 前端 schema-form 自动渲染参数面板
    base = {
        "protocol_version": PROTOCOL_VERSION,
        "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
        "name": "grounded-sam2",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "labels": [],
        "is_interactive": True,
        # 声明本 backend 支持 POST /warmup (协议 §4.4).
        "warmup_endpoint": True,
        # "bbox" 图像交互单框 prompt 改名 "interactive_box" (统一双 backend 命名).
        # tracker / box-seg 的 "bbox" 是几何输入/追踪种子 (走 geometry-prompt 批量, 非 /predict
        # context.type 路由), 属存活的「几何形状」语义, 各自保留 (见下方 models[])。
        "supported_prompts": [
            "point",
            "interactive_box",
            "mask",
            "scribble",
            "text",
        ],
        # text 路径输出形态选择 (box=DINO 直出, mask=DINO+SAM, both=配对返回).
        "supported_text_outputs": ["box", "mask", "both"],
        # 平台 video_tracker 协议桥据此判断 backend 是否支持视频跟踪.
        "supported_trackers": ["sam2_video"],
        "tracker_context_mode": (
            "session"
            if VIDEO_TRACKER_MAX_CONTEXT_FRAMES > VIDEO_TRACKER_MAX_WINDOW_FRAMES
            else "none"
        ),
        "max_context_frames": VIDEO_TRACKER_MAX_CONTEXT_FRAMES,
        # 富变体元数据: 与 params.*_variant.enum 同源, enum 保留作老前端兼容.
        "supported_variants": _supported_variants(),
        "params": {
            "type": "object",
            "properties": {
                "box_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": BOX_THRESHOLD,
                    "title": "Box 置信度阈值",
                    "x-platform-role": PlatformRole.CONFIDENCE.value,
                    "description": "GroundingDINO 框检测的最低置信度（文本 prompt 路径）。调低=召回更多小物/弱目标但噪声增多；调高=更干净但易漏检。",
                },
                "text_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": TEXT_THRESHOLD,
                    "title": "Text 置信度阈值",
                    "x-platform-role": PlatformRole.TEXT_THRESHOLD.value,
                    "description": "短语与图像区域语义匹配的最低分。调高=匹配更严格、更贴合 prompt 词；调低=更宽松、易误配。",
                },
                "simplify_tolerance": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 10.0,
                    "default": DEFAULT_SIMPLIFY_TOLERANCE,
                    "title": "轮廓简化容差(像素)",
                    "x-platform-role": PlatformRole.SIMPLIFY_TOLERANCE.value,
                    "description": "多边形轮廓抽稀强度（像素）。调大=顶点更少、更轻量；调小=更贴合细节但顶点更多。仅影响 mask 输出。",
                },
                "sam_variant": {
                    "type": "string",
                    "enum": ["tiny", "small", "base_plus", "large"],
                    "default": SAM_VARIANT,
                    "title": "SAM 2 变体",
                    "x-platform-role": PlatformRole.MODEL_VARIANT.value,
                    "description": "SAM 2 分割模型大小。越大越精细但越慢、越吃显存；tiny 最快。切换会触发一次冷加载。",
                },
                "dino_variant": {
                    "type": "string",
                    "enum": ["T", "B"],
                    "default": DINO_VARIANT,
                    "title": "GroundingDINO 变体",
                    "x-platform-role": PlatformRole.MODEL_VARIANT.value,
                    "description": "文本检测模型大小：T(Tiny) 更快，B(Base) 更准更吃资源。切换会触发一次冷加载。",
                },
            },
        },
    }
    # 协议 v2: 顶层 infra + 多模型目录 (models[])。
    # grounded-sam2 的 4 条实际能力拆成独立 model 条目, 让平台
    # 「协议能力目录」按 task 正确归类:
    #   - detection           (text → bbox, DINO 单跑)
    #   - segmentation        (text → mask/polygon, DINO + SAM2)
    #   - interactive_seg     (point/bbox → mask/polygon, SAM2 单跑)
    #   - tracker             (sam2_video, first frame bbox → 跨帧 bbox)
    # `/predict` 协议不变 (依旧由 context.type / supported_prompts 路径自路由),
    # 顶层 supported_prompts / supported_geometric_outputs / supported_trackers
    # 全部保留, 供未迁移平台向后兼容 (合成隐式单 model 路径)。
    base["infra"] = "pytorch"
    # 每个 model 只声明真正用到的 axes (而非全暴露 sam+dino 两轴):
    #   - detection 只用 GroundingDINO 输出 bbox, 不走 SAM;
    #   - interactive_seg / tracker 只用 SAM2 (prompts 是 point/bbox, 与 text 无关);
    #   - segmentation 是 DINO + SAM 组合, 两轴都用。
    # 前端模型市场据此正确聚合: SAM 系列只关联到 seg/iseg/tracker, DINO 系列只到 det/seg。
    # `default_variants`: backend 自报该 task 的默认 variant 组合, 供前端
    # 用户未选时作初值. 与 model 的 supported_variants 轴一一对应:
    #   - detection (DINO 路径) 只声明 dino_variant
    #   - interactive_seg / tracker (SAM2 路径) 只声明 sam_variant
    #   - segmentation (DINO + SAM2 组合) 两轴都声明
    base["models"] = [
        {
            "id": "grounded-sam2-detection",
            "display_name": "Grounded-SAM 2 · 文本检测 (DINO)",
            "task": "detection",
            "model_family": "grounded-sam2",
            "infra": "pytorch",
            "is_interactive": False,
            # 纯 DINO 文本检测,单次推理原子。
            "composition": "atom",
            "supported_prompts": ["text"],
            # 文本检测器: 可跑整图, 也可在父框 crop 上检子物体 (crop-detect 下游)。
            "supported_inputs": ["full_image", "crop"],
            "supported_geometric_outputs": ["bbox"],
            "output_attribute_types": ["class"],
            "resource_profile": {"device": "gpu", "batchable": True},
            "supported_text_outputs": ["box"],
            "supported_variants": [_dino_variant_axis()],
            "variants_shared_across_tasks": True,
            "default_variants": {"dino_variant": DINO_VARIANT},
            "params": base["params"],
        },
        {
            "id": "grounded-sam2-segmentation",
            "display_name": "Grounded-SAM 2 · 文本分割 (DINO + SAM)",
            "task": "segmentation",
            "model_family": "grounded-sam2",
            "infra": "pytorch",
            "is_interactive": False,
            # 一个 model 内部串 DINO(文本→框) + SAM(框→mask),内部编排复合。
            "composition": "composite",
            "supported_prompts": ["text"],
            # 文本→分割: 整图 / 父框 crop 上跑 (文本驱动, 复合内部 DINO+SAM)。
            "supported_inputs": ["full_image", "crop"],
            "supported_geometric_outputs": ["polygon"],
            "output_attribute_types": ["class"],
            "resource_profile": {"device": "gpu", "batchable": True},
            "supported_text_outputs": ["mask", "both"],
            "supported_variants": base["supported_variants"],
            "variants_shared_across_tasks": True,
            "default_variants": {
                "sam_variant": SAM_VARIANT,
                "dino_variant": DINO_VARIANT,
            },
            "params": base["params"],
        },
        {
            "id": "grounded-sam2-interactive-seg",
            "display_name": "Grounded-SAM 2 · 交互分割 (SAM2)",
            "task": "interactive_seg",
            "model_family": "grounded-sam2",
            "infra": "pytorch",
            "is_interactive": True,
            # 单次 SAM 推理(prompt→mask),原子。
            "composition": "atom",
            # bbox→interactive_box (图像交互单框单 mask).
            "supported_prompts": [
                "point",
                "interactive_box",
                "mask",
                "scribble",
            ],
            # 交互分割: 消费点 / 框提示 (不作批量 crop 下游)。
            "supported_inputs": [
                "bbox_prompt",
                "point_prompt",
                "mask_prompt",
                "scribble_prompt",
                "full_image",
            ],
            "supported_geometric_outputs": ["polygon", "mask"],
            # 单实例交互推理, 不作批量。output_attribute_types 留空 (无类别/置信度产出)。
            "resource_profile": {"device": "gpu", "batchable": False},
            "supported_variants": [_sam_variant_axis()],
            "variants_shared_across_tasks": True,
            "default_variants": {"sam_variant": SAM_VARIANT},
            "params": base["params"],
        },
        {
            "id": "grounded-sam2-tracker",
            "display_name": "Grounded-SAM 2 · 视频追踪 (SAM2 Video)",
            "task": "tracker",
            "model_family": "grounded-sam2",
            "infra": "pytorch",
            "is_interactive": True,
            # 跨帧 memory bank 的有状态视频追踪,内部编排复合。
            "composition": "composite",
            "supported_prompts": ["point", "bbox", "correction_frame"],
            # 视频追踪: 以框提示初始化 (有状态视频, 非批量 crop 下游)。
            "supported_inputs": [
                "video",
                "point_prompt",
                "bbox_prompt",
                "mask_prompt",
            ],
            "supported_geometric_outputs": ["bbox", "polygon", "mask"],
            # 有状态视频追踪, 跨帧串行不可批量。output_attribute_types 留空。
            "resource_profile": {"device": "gpu", "batchable": False},
            "supported_trackers": ["sam2_video"],
            "max_window_frames": VIDEO_TRACKER_MAX_WINDOW_FRAMES,
            "tracker_context_mode": (
                "session"
                if VIDEO_TRACKER_MAX_CONTEXT_FRAMES > VIDEO_TRACKER_MAX_WINDOW_FRAMES
                else "none"
            ),
            "max_context_frames": VIDEO_TRACKER_MAX_CONTEXT_FRAMES,
            "supported_variants": [_sam_variant_axis()],
            "variants_shared_across_tasks": True,
            "default_variants": {"sam_variant": SAM_VARIANT},
            "params": base["params"],
        },
        {
            # 框→mask 批量分割原子: public、非交互、下游可编排。
            # 与 interactive-seg 共享底层 SAM(predict_bbox / predict_boxes),
            # 但作为独立 model 暴露——非交互, 供多阶段编排消费上游检测框(geometry-prompt 批量)。
            "id": "grounded-sam2-box-seg",
            "display_name": "Grounded-SAM 2 · 框→分割 (SAM)",
            "task": "segmentation",
            "model_family": "grounded-sam2",
            "infra": "pytorch",
            "is_interactive": False,
            # 单次 SAM 推理(框→mask),原子;DINO 不参与, 故只声明 sam 轴。
            "composition": "atom",
            "supported_prompts": ["bbox"],
            # 框→分割: 消费上游检测框 (geometry-prompt 批量下游)。
            "supported_inputs": ["bbox_prompt", "full_image"],
            "supported_geometric_outputs": ["polygon"],
            # 框→mask 批量细化: 消费上游检测框, 透传其类别, 自身不分类。
            # 故批量可跑但 output_attribute_types 留空 (不自产 class/score)。
            "resource_profile": {"device": "gpu", "batchable": True},
            "supported_variants": [_sam_variant_axis()],
            "variants_shared_across_tasks": True,
            "default_variants": {"sam_variant": SAM_VARIANT},
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
async def metrics() -> Response:
    update_cache_size(await _pool.total_cache_size() if _pool is not None else 0)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/cache/stats")
async def cache_stats() -> dict:
    if _pool is None:
        return {"size": 0, "hits": 0, "misses": 0, "hit_rate": 0.0, "buckets": {}}
    return await _pool.aggregate_cache_stats()


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
    """Bodyless legacy unload is image-only; managed unload clears both pools."""

    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return {"ok": True, "unloaded": False, "loaded": False}
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


class ReloadRequest(BaseModel):
    """可选指定变体预热. 缺省回退 env 默认变体.

    task_type 区分预热目标池: "image" (默认, 图片池) / "video"
    (独立 video tracker 池). 旧调用不传 = "image", 行为完全不变.
    """

    sam_variant: str | None = None
    dino_variant: str | None = None
    task_type: str = "image"


@app.post("/reload")
async def reload(request: Request, req: ReloadRequest | None = None) -> dict:
    """主动 (重新) 加载变体进 pool. 已加载该变体时 reloaded=false.

    接受可选 {sam_variant, dino_variant} 预热指定变体 (模型市场单变体预热);
    缺省回退 env 默认变体. 非法变体 422 (同 predict 的 _resolve_variant 校验).
    task_type="video" 时预热独立 video tracker 池 (不用 dino).
    """
    if _pool is None or _video_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    operation = _request_operation(request)
    task_type = (req.task_type if req else None) or "image"

    # video 分支: 预热独立 video tracker 池 (单维 sam_variant, 无 dino).
    if task_type == "video":
        sv = (req.sam_variant if req else None) or SAM_VARIANT
        if sv not in SAM2_CONFIGS:
            raise VariantNotSupportedError("sam_variant", sv, sorted(SAM2_CONFIGS))
        try:
            cache_hit, _load_ms, _evicted = await _video_pool.warmup(sv)
        except VideoBuildTimeout as exc:
            operation.track_future(exc.builder)
            raise ModelUnavailableError(sv, str(exc)) from exc
        except VideoPoolBusyError as exc:
            raise ModelUnavailableError(sv, str(exc)) from exc
        except FileNotFoundError as exc:
            raise ModelUnavailableError(
                sv, f"video checkpoint not provisioned: {exc}"
            ) from exc
        except asyncio.CancelledError:
            operation.track_future(_video_pool.builder_for_now(sv))
            raise
        return {
            "ok": True,
            "loaded": True,
            "reloaded": not cache_hit,
            "sam_variant": sv,
            "task_type": "video",
        }

    if task_type != "image":
        raise HTTPException(
            status_code=422,
            detail=f"unsupported task_type: {task_type!r}; allowed=['image', 'video']",
        )

    sv = (req.sam_variant if req else None) or SAM_VARIANT
    dv = (req.dino_variant if req else None) or DINO_VARIANT
    if sv not in SAM2_CONFIGS:
        raise VariantNotSupportedError("sam_variant", sv, sorted(SAM2_CONFIGS))
    if dv not in DINO_CONFIGS:
        raise VariantNotSupportedError("dino_variant", dv, sorted(DINO_CONFIGS))
    try:
        cache_hit, _load_ms, _evicted = await _pool.warmup(sv, dv)
    except ModelBuildTimeout as exc:
        operation.track_future(exc.builder)
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except ModelPoolBusyError as exc:
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except FileNotFoundError as exc:
        raise ModelUnavailableError(
            _model_key(sv, dv),
            f"checkpoint not provisioned: {exc}",
        ) from exc
    except asyncio.CancelledError:
        operation.track_future(_pool.builder_for_now(sv, dv))
        raise
    return {
        "ok": True,
        "loaded": True,
        "reloaded": not cache_hit,
        "sam_variant": sv,
        "dino_variant": dv,
        "task_type": "image",
    }


# ---------- POST /warmup (协议 §4.4) ----------


@app.post("/warmup", response_model=WarmupResponse)
async def warmup(request: Request, req: WarmupRequest) -> WarmupResponse:
    """协议 §4.4 · 加载指定 (sam_variant, dino_variant) 权重到 pool, 不跑 forward.

    task 路由 (issue claude[bot] P1, 与 /reload 行为对齐):
    - tracker → 独立 video_pool (单维 sam_variant, 无 dino); 不动图片池, 不强制 DINO.
    - detection / segmentation / interactive_seg / None → 图片池 ModelPool (SAM + DINO).

    缺失的 axis 回退 backend env 默认 (SAM_VARIANT / DINO_VARIANT). 池满按 LRU 淘汰最旧 key,
    evicted 字段回填给前端 toast.
    """
    if _pool is None or _video_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    operation = _request_operation(request)
    variants = req.variants or {}
    sv = variants.get("sam_variant") or SAM_VARIANT
    if sv not in SAM2_CONFIGS:
        raise VariantNotSupportedError("sam_variant", sv, sorted(SAM2_CONFIGS))

    # tracker: 走独立 video_pool, 不复用图片池 / 不校验 DINO (video predictor 不用 DINO)。
    if req.task == "tracker":
        try:
            cache_hit, load_ms, evicted = await _video_pool.warmup(sv)
        except VideoBuildTimeout as exc:
            operation.track_future(exc.builder)
            raise ModelUnavailableError(sv, str(exc)) from exc
        except VideoPoolBusyError as exc:
            raise ModelUnavailableError(sv, str(exc)) from exc
        except FileNotFoundError as exc:
            raise ModelUnavailableError(
                sv, f"video checkpoint not provisioned: {exc}"
            ) from exc
        except asyncio.CancelledError:
            operation.track_future(_video_pool.builder_for_now(sv))
            raise
        return WarmupResponse(
            ok=True,
            model_load_ms=load_ms,
            cache_hit=cache_hit,
            evicted=evicted,
        )

    dv = variants.get("dino_variant") or DINO_VARIANT
    if dv not in DINO_CONFIGS:
        raise VariantNotSupportedError("dino_variant", dv, sorted(DINO_CONFIGS))
    try:
        cache_hit, load_ms, evicted = await _pool.warmup(sv, dv)
    except ModelBuildTimeout as exc:
        operation.track_future(exc.builder)
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except ModelPoolBusyError as exc:
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except FileNotFoundError as exc:
        raise ModelUnavailableError(
            _model_key(sv, dv), f"checkpoint not provisioned: {exc}"
        ) from exc
    except asyncio.CancelledError:
        operation.track_future(_pool.builder_for_now(sv, dv))
        raise
    return WarmupResponse(
        ok=True,
        model_load_ms=load_ms,
        cache_hit=cache_hit,
        evicted=evicted,
    )


async def _run_executor_to_completion(
    call: Callable[[], Any],
    operation: WorkloadOperation | None,
) -> Any:
    """Keep a real executor owner alive across repeated request cancellation."""

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


def _run_prompt_sync(
    file_path: str,
    ctx: dict,
    sv: str,
    dv: str,
    p: GroundedSAM2Predictor,
    cache: EmbeddingCache,
    pool_cache_hit: bool,
    model_load_ms: int | None,
) -> tuple[list[dict], bool, str, str, bool, int | None, str | None]:
    """返回 (results, embedding_hit, sam_variant, dino_variant,
    pool_cache_hit, model_load_ms, mask_input_next).

    - embedding_hit: 图像 embedding 缓存命中 (image fetch / set_image 跳过)
    - pool_cache_hit: model pool 命中 (权重已加载, 不需冷启)
    - model_load_ms: 本次 pool miss 的 build 耗时, 命中时 None
    - mask_input_next: point 精修单 mask 阶段的 low-res logits 回灌, 其余恒 None
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
    cache_key = compute_cache_key(file_path, sv)

    # simplify_tolerance 单次请求级覆盖 (None 时 predictor 用 DEFAULT_SIMPLIFY_TOLERANCE)
    simplify_tol = ctx.get("simplify_tolerance")
    if simplify_tol is not None:
        try:
            simplify_tol = float(simplify_tol)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"context.simplify_tolerance must be float, got {simplify_tol!r}",
            )
        if simplify_tol < 0:
            raise HTTPException(
                status_code=422, detail="context.simplify_tolerance must be >= 0"
            )

    if ptype == "point":
        if mask_context is None or mask_context.points is None:
            raise HTTPException(
                status_code=422, detail="context.points required for type=point"
            )
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        points = [list(point) for point in mask_context.points]
        labels = list(mask_context.labels or [1] * len(points))
        # 正/负点累加由前端重发全量点; multimask 单点歧义出候选.
        multimask = mask_context.multimask_output
        # 上一轮 low-res logits 回灌 (多点精修阶段; 首点 multimask 候选阶段前端不回传).
        mask_input = mask_context.mask_input
        # miss: 拉图 + 让 predictor 内部 set_image + put; hit: 不拉图, 走 restore_sam.
        image = (
            None
            if cache.peek(cache_key)
            else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        )
        try:
            results, hit, mask_input_next = p.predict_point(
                image,
                points,
                labels,
                multimask_output=multimask,
                mask_input=mask_input,
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
        return results, hit, sv, dv, pool_cache_hit, model_load_ms, mask_input_next

    if ptype == "interactive_box":
        if mask_context is None or mask_context.bbox is None:
            raise HTTPException(
                status_code=422,
                detail="context.bbox=[x1,y1,x2,y2] required for type=interactive_box",
            )
        output_geometry, prompt_revision = _coerce_interactive_output(ctx)
        # 单框单 mask (旧 type=bbox 改名; bbox 已退出交互 prompt 命名空间).
        bbox = list(mask_context.bbox)
        multimask = mask_context.multimask_output
        image = (
            None
            if cache.peek(cache_key)
            else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        )
        try:
            results, hit, mask_input_next = p.predict_bbox(
                image,
                bbox,
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
        return results, hit, sv, dv, pool_cache_hit, model_load_ms, mask_input_next

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
            if cache.peek(cache_key)
            else fetch_image(
                file_path,
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
            )
        )
        try:
            results, hit, mask_input_next = p.predict_mask(
                image,
                mask_prompt,
                mask_input=mask_context.mask_input,
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
        return results, hit, sv, dv, pool_cache_hit, model_load_ms, mask_input_next

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
            if cache.peek(cache_key)
            else fetch_image(
                file_path,
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
            )
        )
        try:
            results, hit, mask_input_next = p.predict_point(
                image,
                [],
                [],
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
        return results, hit, sv, dv, pool_cache_hit, model_load_ms, mask_input_next

    if ptype == "text":
        text = (ctx.get("text") or "").strip()
        if not text:
            raise HTTPException(
                status_code=422, detail="context.text required for type=text"
            )
        # text 必须拿原图给 DINO; SAM 端仍走缓存 (mask/both 路径)
        # ctx 上的项目级阈值 override (None 时回退到 backend env 默认值)
        box_th = ctx.get("box_threshold")
        text_th = ctx.get("text_threshold")
        # 输出形态; 默认 mask 兼容老前端.
        output_mode = ctx.get("output", "mask")
        if output_mode not in ("box", "mask", "both"):
            raise HTTPException(
                status_code=422,
                detail=f"context.output must be one of box|mask|both, got {output_mode!r}",
            )
        image = fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        results, hit = p.predict_text(
            image,
            text,
            output=output_mode,
            cache_key=cache_key,
            box_threshold=box_th,
            text_threshold=text_th,
            simplify_tolerance=simplify_tol,
        )
        return results, hit, sv, dv, pool_cache_hit, model_load_ms, None

    raise HTTPException(status_code=422, detail=f"unsupported context.type: {ptype}")


async def _run_prompt(
    file_path: str,
    ctx: dict,
    operation: WorkloadOperation | None = None,
) -> tuple[list[dict], bool, str, str, bool, int | None, str | None]:
    global _last_request_at

    if _pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    sv, dv = _resolve_variant(ctx)
    try:
        async with _pool.borrow(sv, dv) as lease:
            _last_request_at = time.monotonic()
            return await _run_executor_to_completion(
                functools.partial(
                    _run_prompt_sync,
                    file_path,
                    ctx,
                    sv,
                    dv,
                    lease.predictor,
                    lease.cache,
                    lease.cache_hit,
                    lease.model_load_ms,
                ),
                operation,
            )
    except ModelBuildTimeout as exc:
        if operation is not None:
            operation.track_future(exc.builder)
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except ModelPoolBusyError as exc:
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except FileNotFoundError as exc:
        raise ModelUnavailableError(
            _model_key(sv, dv),
            (
                f"checkpoint not provisioned: {exc}; "
                "把该变体加入 PREFETCH_SAM_VARIANTS / PREFETCH_DINO_VARIANTS 后重建容器, "
                "或手动下载 checkpoint 到 CHECKPOINT_DIR."
            ),
        ) from exc
    except asyncio.CancelledError:
        if operation is not None:
            operation.track_future(_pool.builder_for_now(sv, dv))
        raise


def _parse_box_prompts(raw: object) -> list[tuple[list[float], int]]:
    """校验并解析 geometry-prompt 批量入参 ``tasks[].prompts[]``。

    每项 ``{box:[x1,y1,x2,y2], parent_box_idx:int}``; parent_box_idx 缺省按出现序。
    """
    if not isinstance(raw, list) or not raw:
        raise HTTPException(
            status_code=422, detail="tasks[].prompts must be a non-empty list"
        )
    out: list[tuple[list[float], int]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=422, detail="prompts[] item must be an object"
            )
        box = item.get("box")
        if not isinstance(box, list) or len(box) != 4:
            raise HTTPException(
                status_code=422, detail="prompts[].box=[x1,y1,x2,y2] required"
            )
        parent_idx = item.get("parent_box_idx", i)
        out.append(([float(c) for c in box], int(parent_idx)))
    return out


def _run_box_seg_sync(
    file_path: str,
    prompts: list[tuple[list[float], int]],
    ctx: dict,
    sv: str,
    dv: str,
    p: GroundedSAM2Predictor,
    cache: EmbeddingCache,
    pool_cache_hit: bool,
    model_load_ms: int | None,
) -> tuple[list[dict], bool, str, str, bool, int | None, str | None]:
    """框→mask 批量分割: 全图 set_image 一次, N 框共享 embedding。

    返回签名与 :func:`_run_prompt` 对齐(results 各带 parent_box_idx); 末位 mask_input_next 恒 None。
    """
    cache_key = compute_cache_key(file_path, sv)
    simplify_tol = ctx.get("simplify_tolerance")
    if simplify_tol is not None:
        try:
            simplify_tol = float(simplify_tol)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"context.simplify_tolerance must be float, got {simplify_tol!r}",
            )
        if simplify_tol < 0:
            raise HTTPException(
                status_code=422, detail="context.simplify_tolerance must be >= 0"
            )
    image = (
        None
        if cache.peek(cache_key)
        else fetch_image(file_path, timeout=IMAGE_DOWNLOAD_TIMEOUT)
    )
    results, hit = p.predict_boxes(
        image, prompts, cache_key=cache_key, simplify_tolerance=simplify_tol
    )
    return results, hit, sv, dv, pool_cache_hit, model_load_ms, None


async def _run_box_seg(
    file_path: str,
    prompts: list[tuple[list[float], int]],
    ctx: dict,
    operation: WorkloadOperation | None = None,
) -> tuple[list[dict], bool, str, str, bool, int | None, str | None]:
    global _last_request_at

    if _pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    sv, dv = _resolve_variant(ctx)
    try:
        async with _pool.borrow(sv, dv) as lease:
            _last_request_at = time.monotonic()
            return await _run_executor_to_completion(
                functools.partial(
                    _run_box_seg_sync,
                    file_path,
                    prompts,
                    ctx,
                    sv,
                    dv,
                    lease.predictor,
                    lease.cache,
                    lease.cache_hit,
                    lease.model_load_ms,
                ),
                operation,
            )
    except ModelBuildTimeout as exc:
        if operation is not None:
            operation.track_future(exc.builder)
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except ModelPoolBusyError as exc:
        raise ModelUnavailableError(_model_key(sv, dv), str(exc)) from exc
    except FileNotFoundError as exc:
        raise ModelUnavailableError(
            _model_key(sv, dv),
            f"checkpoint not provisioned: {exc}",
        ) from exc
    except asyncio.CancelledError:
        if operation is not None:
            operation.track_future(_pool.builder_for_now(sv, dv))
        raise


async def _observe(prompt_type: str, hit: bool, started: float) -> int:
    elapsed = time.perf_counter() - started
    cache_status = "hit" if hit else "miss"
    record_cache(prompt_type, hit)
    record_inference(prompt_type, cache_status, elapsed)
    update_cache_size(await _pool.total_cache_size() if _pool is not None else 0)
    return int(elapsed * 1000)


def _seed_bbox_from_ctx(ctx: dict) -> dict:
    """从 video_tracker context 取归一化 seed bbox {x,y,w,h}。

    优先 prompt.geometry, 回退 source_geometry (与平台 MockBboxTrackerAdapter
    / _bbox_from_geometry 同一约定: video_track 取首关键帧 bbox, bbox/video_bbox
    取 x/y/w(width)/h(height))。
    """

    def _extract(geometry: dict | None) -> dict | None:
        if not isinstance(geometry, dict):
            return None
        gtype = geometry.get("type")
        if gtype in {"video_track", "video_track_bbox"}:
            keyframes = sorted(
                geometry.get("keyframes") or [],
                key=lambda item: int(item.get("frame_index", 0)),
            )
            if keyframes:
                bbox = keyframes[0].get("bbox") or {}
                return _norm_bbox(bbox)
            return None
        # polygon track: seed = 首关键帧顶点外接框 (SAM2 只吃 bbox seed)。
        if gtype == "video_track_polygon":
            keyframes = sorted(
                geometry.get("keyframes") or [],
                key=lambda item: int(item.get("frame_index", 0)),
            )
            if keyframes:
                return _bbox_from_points(keyframes[0].get("points") or [])
            return None
        if gtype == "polygon":
            return _bbox_from_points(geometry.get("points") or [])
        if gtype in {"bbox", "video_bbox"} or any(
            k in geometry for k in ("x", "y", "w", "width")
        ):
            return _norm_bbox(geometry)
        return None

    def _norm_bbox(bbox: dict) -> dict:
        return {
            "x": float(bbox.get("x", 0.0)),
            "y": float(bbox.get("y", 0.0)),
            "w": float(bbox.get("w", bbox.get("width", 0.0))),
            "h": float(bbox.get("h", bbox.get("height", 0.0))),
        }

    def _bbox_from_points(points: list) -> dict:
        xs = [float(p[0]) for p in points if len(p) >= 2]
        ys = [float(p[1]) for p in points if len(p) >= 2]
        if not xs or not ys:
            return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}

    prompt = ctx.get("prompt")
    if isinstance(prompt, dict):
        seed = _extract(prompt.get("geometry"))
        if seed is not None:
            return seed
    seed = _extract(ctx.get("source_geometry"))
    if seed is not None:
        return seed
    raise HTTPException(
        status_code=422,
        detail="video_tracker requires a seed bbox in prompt.geometry or source_geometry",
    )


def _seeds_from_ctx(ctx: dict) -> list[dict]:
    """从 context 取逐对象 seed(多目标)。

    与 sam3 PVS `_seeds_from_video_ctx` 同款: `seeds[]` 每条
    {obj_id?, prompts?/bbox?/points?/geometry?}——prompts(多帧纠偏)/bbox/points 原样透传;
    geometry(跨窗续种, runner `_continuation_seeds` 下发)取外接框。缺 obj_id 按序补 1..N。
    无 `seeds[]` 时回退单 seed = source_geometry/prompt.geometry(obj_id=1), 与旧 seed-bbox 等价。
    """
    raw = ctx.get("seeds")
    if isinstance(raw, list) and raw:
        seeds: list[dict] = []
        for i, s in enumerate(raw):
            if not isinstance(s, dict):
                continue
            entry: dict = {"obj_id": int(s.get("obj_id", i + 1))}
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
                try:
                    entry["bbox"] = _seed_bbox_from_ctx(
                        {"source_geometry": s["geometry"]}
                    )
                except HTTPException:
                    pass
            if (
                "prompts" in entry
                or "bbox" in entry
                or "points" in entry
                or "mask_prompt" in entry
            ):
                seeds.append(entry)
        if seeds:
            return seeds
    return [{"obj_id": 1, "bbox": _seed_bbox_from_ctx(ctx)}]


def _video_local_path(file_path: str) -> str:
    """video_tracker 的 file_path → OpenCV 可打开的源.

    本地文件直接用; http(s) 先下载到临时文件 (OpenCV 对 presigned URL 的
    HTTP range/seek 支持不稳, 整段拉下来再解码更可靠)。调用方负责清理临时文件。
    """
    if file_path.startswith(("http://", "https://")):
        import tempfile
        from urllib.parse import urlsplit

        suffix = os.path.splitext(urlsplit(file_path).path)[-1] or ".mp4"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="sam2vid_src_")
        try:
            with (
                os.fdopen(fd, "wb") as fh,
                httpx.Client(
                    timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True
                ) as client,
            ):
                with client.stream("GET", file_path) as resp:
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
    tracker: SAM2VideoTracker,
    file_path: str,
    from_frame: int,
    to_frame: int,
    direction: str,
    seeds: list[dict[str, Any]],
    output_geometry: str,
) -> list[dict[str, Any]]:
    """Download, propagate, and remove the source under one real thread owner."""

    local_path = _video_local_path(file_path)
    cleanup = local_path != file_path
    try:
        return tracker.propagate(
            local_path,
            from_frame,
            to_frame,
            direction,
            seeds,
            output_geometry=output_geometry,
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
) -> tuple[list[dict], str, dict | None]:
    """sam2_video tracker: 取 video pool tracker, 窗内传播 seed bbox。

    返回 (result 列表, sam_variant)。OOM / timeout 等不吞, 让 api 落 error_message
    (ADR-0012: predictor 不进 api, 故障外抛)。
    """
    sv = _resolve_video_variant(ctx)
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
    # polygon track 回填: 平台按源几何类型下发 output_geometry, "polygon" 时
    # 每帧保留 mask 矢量化为多边形而非降 bbox; 缺省 "bbox" 维持既有 seed-bbox tracker 行为。
    output_geometry = ctx.get("output_geometry") or "bbox"
    if output_geometry not in ("bbox", "polygon", "mask"):
        raise HTTPException(
            status_code=422,
            detail=f"video_tracker output_geometry must be bbox|polygon|mask, got {output_geometry!r}",
        )

    if _video_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    started = time.perf_counter()
    raw_control = ctx.get("tracker_context")
    if raw_control is not None:
        control = TrackerContextControl.model_validate(raw_control)
        binding = (control.job_id, direction, sv)
        try:
            if control.action == "close":
                await _video_sessions.close(control.token or "", binding)
                return [], sv, {"tracker_context": {"mode": "session", "closed": True}}

            seeds = _seeds_from_ctx(ctx) if control.action == "start" else []
            if control.action == "start":
                local_path = _video_local_path(file_path)
                cleanup = local_path != file_path
                try:
                    token = await _video_sessions.start(
                        binding=binding,
                        borrow=lambda: _video_pool.borrow(sv),
                        open_session=lambda lease: _run_executor_to_completion(
                            functools.partial(
                                lease.tracker.start_session,
                                local_path,
                                control.span_start_frame,
                                control.span_end_frame,
                                direction,
                                seeds,
                            ),
                            operation,
                        ),
                        close_session=lambda lease, state: _run_executor_to_completion(
                            functools.partial(lease.tracker.close_session, state), None
                        ),
                    )
                finally:
                    if cleanup:
                        os.unlink(local_path)
            else:
                token = control.token or ""
            result = await _video_sessions.call(
                token,
                binding,
                lambda lease, state: _run_executor_to_completion(
                    functools.partial(
                        lease.tracker.continue_session,
                        state,
                        from_frame,
                        to_frame,
                        output_geometry=output_geometry,
                    ),
                    operation,
                ),
            )
            record_video_tracker(sv, len(result), time.perf_counter() - started)
            return result, sv, {"tracker_context": {"mode": "session", "token": token}}
        except TrackerSessionLost as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": "tracker_context_lost"},
            ) from exc

    # 多目标: 优先 seeds[] (逐对象点/框/多帧 prompt), 回退单 seed_bbox。
    seeds = _seeds_from_ctx(ctx)
    try:
        async with _video_pool.borrow(sv) as lease:
            result = await _run_executor_to_completion(
                functools.partial(
                    _run_video_tracker_sync,
                    lease.tracker,
                    file_path,
                    from_frame,
                    to_frame,
                    direction,
                    seeds,
                    output_geometry,
                ),
                operation,
            )
    except VideoBuildTimeout as exc:
        if operation is not None:
            operation.track_future(exc.builder)
        raise ModelUnavailableError(sv, str(exc)) from exc
    except VideoPoolBusyError as exc:
        raise ModelUnavailableError(sv, str(exc)) from exc
    except FileNotFoundError as exc:
        raise ModelUnavailableError(
            sv,
            f"video checkpoint not provisioned: {exc}",
        ) from exc
    except asyncio.CancelledError:
        if operation is not None:
            operation.track_future(_video_pool.builder_for_now(sv))
        raise
    record_video_tracker(sv, len(result), time.perf_counter() - started)
    return result, sv, None


@app.post("/predict")
async def predict(request: Request):
    operation = _request_operation(request)
    body = await request.json()
    started = time.perf_counter()

    # 交互式: 单条 task + context
    if isinstance(body, dict) and "task" in body and "context" in body:
        task = body["task"]
        ctx = body.get("context") or {}
        # video_tracker 分支 (走独立 video pool, 不进图片缓存路径).
        if ctx.get("type") == "video_tracker":
            correction = (ctx.get("prompt") or {}).get("correction") or {}
            tracker_started = time.perf_counter()
            try:
                result, sv, tracker_meta = await _run_video_tracker(
                    task["file_path"],
                    ctx,
                    operation,
                )
            except Exception:
                record_mask_ai_backend_inference(
                    model_role="sam2_video",
                    operation="correction" if correction else "tracking",
                    fallback_reason=correction.get("fallback_reason"),
                    candidate_count=0,
                    outcome="error",
                    duration_seconds=time.perf_counter() - tracker_started,
                )
                raise
            record_mask_ai_backend_inference(
                model_role="sam2_video",
                operation="correction" if correction else "tracking",
                fallback_reason=correction.get("fallback_reason"),
                candidate_count=len(result),
                outcome="success",
                duration_seconds=time.perf_counter() - tracker_started,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return PredictionResult(
                result=result,
                model_version=f"sam2_video-2.1{sv}",
                inference_time_ms=elapsed_ms,
                meta=tracker_meta,
            ).model_dump(exclude_none=True)
        # _run_prompt 内部经 pool 取请求级变体 predictor (miss 触发冷启).
        (
            result,
            hit,
            sv,
            dv,
            pool_cache_hit,
            model_load_ms,
            mask_input_next,
        ) = await _run_prompt(task["file_path"], ctx, operation)
        elapsed_ms = await _observe(ctx.get("type") or "unknown", hit, started)
        return PredictionResult(
            result=result,
            score=max((r.get("score") or 0.0) for r in result) if result else None,
            model_version=_model_version(sv, dv),
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
                )
                and ctx.get("output_geometry", "polygon") == "mask"
                and not result
                else None
            ),
        ).model_dump(exclude_none=True)

    # 批量: tasks 数组（M0 仅支持顶层 context.text 时整批同 prompt）
    if isinstance(body, dict) and "tasks" in body:
        tasks = body["tasks"]
        ctx = body.get("context") or {"type": "text", "text": body.get("text", "")}
        # 文本批量按 model_id 路由输出形态 (统一 wire): detection→box(纯 DINO),
        # segmentation→ctx.output||mask(DINO+SAM)。无 model_id 回落 ctx.output (老 wire 兼容)。
        # type 强制 text 以走 _run_prompt 文本分支 (前端可能发 type=task)。box-seg 走 per-task prompts。
        _mid = ctx.get("model_id")
        if _mid == "grounded-sam2-detection":
            ctx = {**ctx, "type": "text", "output": "box"}
        elif _mid == "grounded-sam2-segmentation":
            ctx = {**ctx, "type": "text", "output": ctx.get("output", "mask")}
        results = []
        for t in tasks:
            t_started = time.perf_counter()
            sv, dv = SAM_VARIANT, DINO_VARIANT
            pool_cache_hit: bool | None = None
            model_load_ms: int | None = None
            # geometry-prompt 批量(下游 box-seg stage): task 携带 prompts[] 框列表,
            # 走全图 set_image 一次、N 框共享 embedding 的路径; 否则走文本/context 批量。
            box_prompts = t.get("prompts")
            obs_type = "box_seg" if box_prompts else (ctx.get("type") or "unknown")
            try:
                # 批量路径不回灌 mask_input → 丢弃末位 mask_input_next.
                if box_prompts:
                    prompts = _parse_box_prompts(box_prompts)
                    (
                        result,
                        hit,
                        sv,
                        dv,
                        pool_cache_hit,
                        model_load_ms,
                        _,
                    ) = await _run_box_seg(t["file_path"], prompts, ctx, operation)
                else:
                    (
                        result,
                        hit,
                        sv,
                        dv,
                        pool_cache_hit,
                        model_load_ms,
                        _,
                    ) = await _run_prompt(t["file_path"], ctx, operation)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 — 单图失败降级，不中断整批
                logger.exception("predict failed for task=%s: %s", t.get("id"), exc)
                result, hit = [], False
            elapsed_ms = await _observe(obs_type, hit, t_started)
            results.append(
                PredictionResult(
                    task=t.get("id"),
                    result=result,
                    score=max((r.get("score") or 0.0) for r in result)
                    if result
                    else None,
                    model_version=_model_version(sv, dv),
                    inference_time_ms=elapsed_ms,
                    cache_hit=pool_cache_hit,
                    model_load_ms=model_load_ms,
                ).model_dump(exclude_none=True)
            )
        return BatchPredictResponse(results=results).model_dump(exclude_none=True)

    raise HTTPException(
        status_code=422, detail="body must contain 'task'+'context' or 'tasks'"
    )
