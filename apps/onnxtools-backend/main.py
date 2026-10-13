"""onnxtools-backend FastAPI 入口 —— 二阶段车辆属性预标注（ml-backend 协议 v2.2）。

端点：
    GET  /health    健康检查 + 句柄池状态 (pool)
    GET  /setup     协议 v2.2 model 目录（三个 model，自报 output_attribute_schema）
    GET  /versions  版本
    POST /predict   批量预测（按 context.model_id 路由一锅端 / 纯检测 / 纯分类）
    POST /warmup    预加载句柄到显存（ModelMarket 预热按钮，协议 §4.4）
    POST /unload    legacy / 受管全池卸载
    POST /drain     受管 generation drain
    POST /drain/cancel、/lifecycle/mode、/lifecycle/reset

三个 model 分别架在各自单模型推理类上、按需懒加载：``vehicle-detect`` 直跑独立
``RtdetrORT`` 只做检测（多阶段编排上游，composition=atom）；``vehicle-attr-classify``
直跑独立 ``VehicleAttributeORT`` 只做分类（多阶段下游，atom）；``vehicle-attr`` 跑完整
``VehicleAttributePipeline``（一锅端检测+属性，composition=composite，过渡保留）。
detect-only 部署只加载检测器、classify-only 只加载分类器。无 variant 多轴；固定句柄池提供
single-flight builder、borrower/use-lock、受管全池清理与取消安全 executor。
`/setup.supported_prompts=["none"]`：纯批量，平台只走「批量预标」入口。
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from aap_backend_runtime import (
    deployment_verified_flag,
    physical_gpu_identity,
    validate_single_gpu_device_set,
    versions_payload,
)
from aap_protocol_v2 import (
    COMPAT_PROTOCOL_VERSIONS,
    PROTOCOL_VERSION,
    BatchPredictResponse,
    PredictionResult,
    WarmupResponse,
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
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import ValidationError

from attribute_schema import OUTPUT_ATTRIBUTE_SCHEMA
from gpu_lifecycle import OnnxToolsGpuLifecycle, WorkloadOperation
from handle_pool import (
    HandleBuildTimeout,
    HandlePool,
    HandlePoolBusyError,
)
from predictor import VehicleAttributePredictor, inspect_handle_providers
from schemas import BatchPredictRequest

logger = logging.getLogger("onnxtools-backend")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

BACKEND_VERSION = "0.4.0"
MODEL_VERSION = "onnxtools-rtdetr+va"
# 纯分类(跳过 rtdetr)的 model_version,与完整 pipeline 区分,便于历史 job 溯源。
VA_MODEL_VERSION = "onnxtools-va"
# 纯检测(只 rtdetr,跳过 va)的 model_version。
DET_ONLY_MODEL_VERSION = "onnxtools-rtdetr"

# 协议 v2 多模型路由:平台把下游阶段卡的 model_id 写进 context["model_id"]。
DETECT_MODEL_ID = "vehicle-attr"  # 一锅端:rtdetr 检测 + va 分类(internal,不对外选用)
DETECT_ONLY_MODEL_ID = (
    "vehicle-detect"  # 纯检测:只 rtdetr 出框,属性交下游(public,多阶段上游)
)
CLASSIFY_MODEL_ID = (
    "vehicle-attr-classify"  # 纯分类:整图当一辆车,跳过 rtdetr(public,多阶段下游)
)

# rtdetr 检测器 onnx metadata 为空，车辆类别按域知识静态自报（与 va 分类器 vehicle_type 取值域一致）。
VEHICLE_TYPES = [
    "car",
    "truck",
    "bus",
    "tanker",
    "slagcar",
    "fire engine",
    "mixer",
    "ambulance",
    "police car",
    "engineering truck",
    "hazardous_goods_vehicle",
    "manned_sweeping_vehicle",
    "school_bus",
]

MODEL_DIR = os.environ.get("ONNXTOOLS_MODEL_DIR", "/app/models")
DET_MODEL = os.environ.get("ONNXTOOLS_DET_MODEL", "rtdetr-2024080100.onnx")
VA_MODEL = os.environ.get("ONNXTOOLS_VA_MODEL", "va_260612.onnx")
CONF_THRES = float(os.environ.get("ONNXTOOLS_CONF_THRES", "0.5"))

# idle-unload: 末次推理后空闲超 IDLE_UNLOAD_SECONDS 自动卸载(<=0 关闭)。与 yolo 对齐。
IDLE_UNLOAD_SECONDS = float(os.environ.get("ONNXTOOLS_IDLE_UNLOAD_SECONDS", "600"))
IDLE_CHECK_INTERVAL = float(os.environ.get("ONNXTOOLS_IDLE_CHECK_INTERVAL", "60"))
BUILD_TIMEOUT = float(os.environ.get("ONNXTOOLS_BUILD_TIMEOUT", "30"))
# The code path is available before deployment calibration, but it may not advertise
# or report itself evictable until a real four-session GPU unload returns to baseline.
MANAGED_LIFECYCLE_VERIFIED = deployment_verified_flag(
    "ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED"
)


# ORT provider 构造偏好 (None=未探测)。一旦探过即缓存，启动后不再重复试 CUDA。
_provider_preference: list[str] | None = None


def _available_providers() -> list[str]:
    """ORT 构建列出的 provider 列表 (可用性, 非功能性)。

    供 /health.configured_device 判断「镜像意图」——构建列出 CUDAExecutionProvider 即视作
    配置 cuda。与 ``_probe_providers`` (功能性探测) 区分：列出 ≠ 可用, 驱动损坏时列出仍有 CUDA
    但功能探测退回 CPU, 两者漂移即「静默退回 CPU」信号。
    """
    try:
        import onnxruntime  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ["CPUExecutionProvider"]
    return list(onnxruntime.get_available_providers())


def _probe_providers() -> list[str]:
    """功能探测 ORT provider 构造优先级。

    ORT 列出 ``CUDAExecutionProvider`` 不代表可用 —— 驱动 / cuDNN 损坏时
    ``InferenceSession(providers=['CUDAExecutionProvider'])`` 会抛错。这里用真实 det 模型文件
    开一次 CUDA session 探测：能开起来返回 ``['CUDAExecutionProvider', 'CPUExecutionProvider']``
    (ORT 自身在 CUDA 初始化失败时按 list 顺序落 CPU)，否则返回 ``['CPUExecutionProvider']``。
    探测 session 构造成功后仍必须检查 ``get_providers()[0]``；ORT 可能不报错但
    已静默使用 CPU。该结果只供后续句柄构造，/health 会读取已加载业务 session
    的实际 primary provider。

    模型文件不存在 (启动期未落盘) 时，仍返回含 CUDA 的优先级列表 —— 此时退回 ORT 自身
    行为 (list 含 CPU fallback)，由 BaseORT 构造时的 ORT fallback 兜底，不阻塞启动。
    """
    global _provider_preference
    if _provider_preference is not None:
        return _provider_preference
    try:
        import onnxruntime  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.warning("onnxruntime 不可用; provider 探测退回 CPU")
        _provider_preference = ["CPUExecutionProvider"]
        return _provider_preference
    available = onnxruntime.get_available_providers()
    if "CUDAExecutionProvider" not in available:
        _provider_preference = ["CPUExecutionProvider"]
        return _provider_preference
    # 用真实 det 模型文件做 CUDA 功能探测。
    det_path = os.path.join(MODEL_DIR, DET_MODEL)
    probe_session: Any = None
    try:
        if os.path.exists(det_path):
            probe_session = onnxruntime.InferenceSession(
                det_path, providers=["CUDAExecutionProvider"]
            )
            actual = probe_session.get_providers()
            if actual and actual[0] == "CUDAExecutionProvider":
                _provider_preference = [
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ]
            else:
                logger.warning(
                    "ORT CUDA 探测 session 已静默退回 %s；后续句柄改用 CPU",
                    actual[0] if actual else "unknown provider",
                )
                _provider_preference = ["CPUExecutionProvider"]
        else:
            # 模型尚未落盘：保留 CUDA 优先 (ORT 构造时按 list 自动落 CPU fallback)，不阻塞。
            _provider_preference = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ORT CUDA 探测失败 (%s) — CUDA 已列出但不可用 (驱动/cuDNN 不匹配?)，"
            "onnxtools backend 全部句柄退回 CPUExecutionProvider。",
            exc,
        )
        _provider_preference = ["CPUExecutionProvider"]
    finally:
        probe_session = None
        gc.collect()
    return _provider_preference


_handle_pool: HandlePool | None = None
_predictor: VehicleAttributePredictor | None = None
_gpu_lifecycle: OnnxToolsGpuLifecycle | None = None
_idle_task: asyncio.Task | None = None


def _handle_for(model_id: str | None) -> str:
    """model_id → handle 名 (与 VehicleAttributePredictor.warm/predict 内部路由一致)。"""
    if model_id == "vehicle-attr-classify":
        return "va"
    if model_id == "vehicle-detect":
        return "detector"
    return "pipeline"


def _make_detector(providers: list[str] | None = None) -> Any:
    """构造独立 rtdetr 检测器(RtdetrORT),只给检测权重。

    ``providers`` 由启动期功能探测决定 (``_probe_providers``)：CUDA 列出但损坏时已降级为
    纯 CPU，避免 RtdetrORT 构造时硬抛 CUDA error。create_detector 把 kwargs 透传给
    RtdetrORT → BaseORT (providers list 含 CPU fallback，ORT 自身按序降级)。
    """
    from onnxtools import create_detector

    return create_detector(
        model_type="rtdetr",
        onnx_path=os.path.join(MODEL_DIR, DET_MODEL),
        conf_thres=CONF_THRES,
        providers=providers if providers is not None else _probe_providers(),
    )


def _make_va_classifier(providers: list[str] | None = None) -> Any:
    """构造独立车辆属性分类器(VehicleAttributeORT),只给分类权重(type/color map 用包内默认)。"""
    from onnxtools import VehicleAttributeORT

    return VehicleAttributeORT(
        os.path.join(MODEL_DIR, VA_MODEL),
        conf_thres=CONF_THRES,
        providers=providers if providers is not None else _probe_providers(),
    )


def _make_pipeline() -> Any:
    """构造一锅端 composite(VehicleAttributePipeline,内部自建 detector + va)。

    当前上游构造器虽接受 ``providers``，但只传给属性分类器，检测器仍使用
    默认 provider。这里跳过该构造器，用两个已经过功能探测的原子工厂注入
    实例，确保 composite 的两个业务 session 使用同一 provider 偏好。
    """
    from onnxtools.pipeline import VehicleAttributePipeline

    providers = _probe_providers()
    pipeline = VehicleAttributePipeline.__new__(VehicleAttributePipeline)
    pipeline.roi_pad_ratio = 0.1
    pipeline.detector = _make_detector(providers)
    pipeline.va_classifier = _make_va_classifier(providers)
    pipeline.class_names = pipeline._resolve_class_names(None)
    return pipeline


async def _idle_watcher() -> None:
    """周期检查:末次推理后空闲超 IDLE_UNLOAD_SECONDS 则卸载全部句柄。"""
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if IDLE_UNLOAD_SECONDS <= 0 or _gpu_lifecycle is None:
                continue
            idle_before = time.monotonic() - IDLE_UNLOAD_SECONDS
            count = await _gpu_lifecycle.try_idle_unload(idle_before=idle_before)
            if count:
                logger.info("idle unloaded %d handles", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("idle_watcher error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _handle_pool, _predictor, _gpu_lifecycle, _idle_task
    validate_single_gpu_device_set()
    raw_keyring = os.environ.get("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "").strip()
    verify_keyring = load_verify_keyring(raw_keyring) if raw_keyring else {}
    # 懒加载:不在启动时加载模型,首次 predict 按 model_id 构造对应句柄。
    _handle_pool = HandlePool(
        {
            "detector": _make_detector,
            "va": _make_va_classifier,
            "pipeline": _make_pipeline,
        },
        inspect_handle_providers,
        build_timeout=BUILD_TIMEOUT,
    )
    _predictor = VehicleAttributePredictor(_handle_pool)
    _gpu_lifecycle = OnnxToolsGpuLifecycle(
        _handle_pool,
        verify_keyring=verify_keyring,
        evictable_verified=MANAGED_LIFECYCLE_VERIFIED,
    )
    # 启动期预热 provider 构造偏好；业务 session 的实际 provider 另由 /health 实时读取。
    probed = _probe_providers()
    _idle_task = asyncio.create_task(_idle_watcher())
    logger.info(
        "onnxtools-backend ready (lazy): model_dir=%s det=%s va=%s conf=%.2f idle_unload=%.0fs "
        "provider_preference=%s managed_verified=%s",
        MODEL_DIR,
        DET_MODEL,
        VA_MODEL,
        CONF_THRES,
        IDLE_UNLOAD_SECONDS,
        probed[0],
        MANAGED_LIFECYCLE_VERIFIED,
    )
    try:
        yield
    finally:
        if _idle_task is not None:
            _idle_task.cancel()
            try:
                await _idle_task
            except (asyncio.CancelledError, BaseException):
                pass
        if _gpu_lifecycle is not None:
            await _gpu_lifecycle.shutdown()


app = FastAPI(title="onnxtools-backend", version=BACKEND_VERSION, lifespan=lifespan)


def _echo_gpu_health_challenge(request: Request, response: Response) -> None:
    challenge = match_gpu_health_challenge(
        request.headers.getlist(GPU_HEALTH_CHALLENGE_HEADER),
        request.query_params.getlist(GPU_HEALTH_CHALLENGE_QUERY_PARAM),
    )
    if challenge is not None:
        response.headers[GPU_HEALTH_CHALLENGE_HEADER] = challenge
        response.headers["Cache-Control"] = "no-store"


def _pool_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    """协议 §4.3 PoolStatus: 句柄池状态, 供模型市场展示已加载 / 预热状态。

    onnxtools 无显存 LRU, 句柄按需懒加载 (≤3: pipeline/detector/va); cap=3,
    current_size=已加载句柄数, loaded_keys 列出各句柄名 (last_used 取末次推理时间)。
    """
    return {
        key: snapshot[key]
        for key in ("cap", "current_size", "loaded_keys", "last_evict")
    }


@app.get("/health", dependencies=[Depends(_echo_gpu_health_challenge)])
async def health() -> dict[str, Any]:
    if _gpu_lifecycle is not None:
        pool_snapshot, residency = await _gpu_lifecycle.snapshot_and_residency()
        residency_payload = residency.model_dump(mode="json")
    else:
        pool_snapshot = {
            "cap": 3,
            "current_size": 0,
            "loaded_keys": [],
            "last_evict": None,
            "provider": None,
        }
        residency_payload = None
    return {
        "status": "ok",
        "service": "onnxtools-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "ready": _handle_pool is not None,
        "loaded_handles": pool_snapshot["current_size"],
        # 句柄池状态, 供模型市场「已加载 / 预热」展示 (此前缺 pool 字段)。
        "pool": _pool_status(pool_snapshot),
        # effective_provider 只来自当前已加载业务 session；空池、无法完整检查
        # composite，或多 session provider 不一致时为 None。
        "compute": {
            "configured_device": "cuda"
            if "CUDAExecutionProvider" in _available_providers()
            else "cpu",
            "effective_provider": pool_snapshot["provider"],
            "cpu_fallback_supported": True,
        },
        "gpu_info": physical_gpu_identity() or None,
        "residency": residency_payload,
    }


def _detect_model_entry() -> dict[str, Any]:
    """一锅端 model:rtdetr 检测 + va 车辆属性（自报 output_attribute_schema）。

    单 backend「一锅端」场景:既出检测框又写车型/颜色属性。

    composition=composite:一个 model 内部串 rtdetr(检测)+ va(属性分类),内部编排复合。
    平台据此把它挡在「编排下游 stage」选择器外(编排只组合 atom),但单阶段可直接选用(开箱即用)。
    """
    return {
        "id": DETECT_MODEL_ID,
        "display_name": "[专用]车辆检测+属性",
        "task": "detection",
        "model_family": "rtdetr-v1",
        "infra": "onnx",
        # 一个 model 内部串 rtdetr(检测)+ va(属性分类),内部编排复合。
        "composition": "composite",
        "is_interactive": False,
        "supported_prompts": ["none"],
        # 一锅端检测+属性: 整图 / 父框 crop 上跑。
        "supported_inputs": ["full_image", "crop"],
        "supported_geometric_outputs": ["bbox"],
        "classes": [{"index": i, "name": n} for i, n in enumerate(VEHICLE_TYPES)],
        # 协议③：输出属性类型 + 取值域自描述，供平台一键导入项目 attribute_schema
        "output_attribute_types": ["class"],
        "output_attribute_schema": OUTPUT_ATTRIBUTE_SCHEMA,
        "default_thresholds": {"conf": CONF_THRES},
        "resource_profile": {"device": "gpu", "batchable": True},
    }


def _detect_only_model_entry() -> dict[str, Any]:
    """纯检测 model:只跑 rtdetr 检测出框,跳过 va 属性分类。

    多阶段编排的上游阶段——只产 bbox,车型/颜色属性交给下游纯分类原子。
    与一锅端 vehicle-attr 区别:不写 attributes,故不声明 output_attribute_schema。
    """
    return {
        "id": DETECT_ONLY_MODEL_ID,
        "display_name": "[专用]车辆检测",
        "task": "detection",
        "model_family": "rtdetr-v1",
        "infra": "onnx",
        # 单跑 rtdetr 检测,原子。多阶段编排上游检测阶段直接选用。
        "composition": "atom",
        "is_interactive": False,
        "supported_prompts": ["none"],
        # 纯检测: 整图 / 父框 crop 上检子物体 (crop-detect 下游)。
        "supported_inputs": ["full_image", "crop"],
        "supported_geometric_outputs": ["bbox"],
        "classes": [{"index": i, "name": n} for i, n in enumerate(VEHICLE_TYPES)],
        # 纯检测不写属性,不声明 output_attribute_*（属性交下游纯分类原子）。
        "default_thresholds": {"conf": CONF_THRES},
        "resource_profile": {"device": "gpu", "batchable": True},
    }


def _classify_model_entry() -> dict[str, Any]:
    """纯分类 model:只跑 va 车辆属性,跳过 rtdetr 检测。

    用于「检测→分类」多阶段编排的下游阶段——上游检测器(如 gsam2)裁好单车 ROI 后,
    本 model 直接对整张 ROI 分类,免去冗余检测、不受 rtdetr 紧 crop 域偏移漏检影响。
    """
    return {
        "id": CLASSIFY_MODEL_ID,
        "display_name": "[专用]车辆属性分类",
        "task": "classification",
        "model_family": "PP-lcnet",
        "infra": "onnx",
        # 单跑 va 属性分类,原子。多阶段编排下游分类阶段直接选用。
        "composition": "atom",
        "is_interactive": False,
        "supported_prompts": ["none"],
        # 纯分类: 对裁好的 ROI(crop)分类, 也可整图。
        "supported_inputs": ["full_image", "crop"],
        # 纯分类不产几何(整图框仅占位,平台 merge 丢弃),不声明几何输出能力。
        "supported_geometric_outputs": [],
        "output_attribute_types": ["class"],
        "output_attribute_schema": OUTPUT_ATTRIBUTE_SCHEMA,
        "default_thresholds": {"conf": CONF_THRES},
        "resource_profile": {"device": "gpu", "batchable": True},
    }


@app.get("/setup")
def setup() -> dict[str, Any]:
    """协议 v2.2 模型目录:广播三个 model —— 一锅端检测+属性(composite,单阶段可选)
    + 纯检测原子(atom,多阶段上游)+ 纯分类原子(atom,多阶段下游)。predict 对三者路由均支持。"""
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
        "name": "onnxtools-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "is_interactive": False,
        "supported_prompts": ["none"],
        "supported_geometric_outputs": ["bbox"],
        "infra": "onnx",
        # 声明支持 POST /warmup (协议 §4.4), 让模型市场「预热默认」按钮可用。
        "warmup_endpoint": True,
        "models": [
            _detect_model_entry(),
            _detect_only_model_entry(),
            _classify_model_entry(),
        ],
    }
    if MANAGED_LIFECYCLE_VERIFIED:
        payload["managed_lifecycle"] = ManagedLifecycleCapabilities().model_dump(
            mode="json"
        )
    return payload


@app.get("/versions")
def versions() -> dict[str, Any]:
    return versions_payload(MODEL_VERSION, BACKEND_VERSION)


def _validate_body(model_type, body):
    try:
        return model_type.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False)
        ) from exc


async def _request_json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="invalid JSON request body"
        ) from exc


async def _optional_request_json(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    if not raw_body.strip():
        return {}
    body = await _request_json(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be an object")
    return body


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


@app.post("/warmup", response_model=WarmupResponse)
async def warmup(request: Request) -> WarmupResponse:
    """Preload one fixed handle after lifecycle admission, without inference."""

    if _handle_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    operation = await _begin_workload(request, AdmissionScope.WARMUP)
    handle_name = "pipeline"
    try:
        body = await _optional_request_json(request)
        handle_name = _handle_for(body.get("model_id"))
        try:
            cache_hit, load_ms = await _handle_pool.warmup(handle_name)
        except HandleBuildTimeout as exc:
            operation.track_future(exc.builder)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except HandlePoolBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.CancelledError:
            operation.track_future(_handle_pool.builder_for_now(handle_name))
            raise
        return WarmupResponse(
            ok=True,
            model_load_ms=load_ms,
            cache_hit=cache_hit,
            evicted=None,
        )
    finally:
        await operation.close()


async def _run_predict(
    req: BatchPredictRequest,
    *,
    operation: WorkloadOperation,
) -> BatchPredictResponse:
    if _predictor is None or _handle_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")

    model_id = req.context.get("model_id")
    handle_name = _handle_for(model_id)
    if handle_name == "va":
        model_version = VA_MODEL_VERSION
    elif handle_name == "detector":
        model_version = DET_ONLY_MODEL_VERSION
    else:
        model_version = MODEL_VERSION

    results: list[PredictionResult] = []
    for task in req.tasks:
        try:
            items, cache_hit, load_ms, inference_ms = await _predictor.predict_one(
                task.file_path,
                handle_name,
            )
            score = max((item.get("score", 0.0) for item in items), default=0.0)
            results.append(
                PredictionResult(
                    task=task.id,
                    result=items,
                    score=score,
                    model_version=model_version,
                    inference_time_ms=inference_ms,
                    cache_hit=cache_hit,
                    model_load_ms=load_ms,
                )
            )
        except HandleBuildTimeout as exc:
            operation.track_future(exc.builder)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except HandlePoolBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.CancelledError:
            operation.track_future(_handle_pool.builder_for_now(handle_name))
            raise
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("predict failed for task %s", task.id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return BatchPredictResponse(results=results)


@app.post("/predict", response_model=BatchPredictResponse)
async def predict(request: Request) -> BatchPredictResponse:
    operation = await _begin_workload(request, AdmissionScope.PREDICT)
    try:
        req = _validate_body(BatchPredictRequest, await _request_json(request))
        return await _run_predict(req, operation=operation)
    finally:
        await operation.close()


@app.post("/unload")
async def unload(request: Request) -> dict[str, Any]:
    """Keep the bodyless legacy wire; a generation body invokes managed unload."""

    generation_header, token = _managed_lifecycle_headers(request)
    if _gpu_lifecycle is None:
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return {"ok": True, "unloaded": 0}
    raw_body = await request.body()
    if not raw_body.strip():
        if generation_header is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return await _gpu_lifecycle.legacy_unload()
    body = _validate_body(GenerationTransitionRequest, await _request_json(request))
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
    body = _validate_body(GenerationTransitionRequest, await _request_json(request))
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
    body = _validate_body(GenerationTransitionRequest, await _request_json(request))
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
