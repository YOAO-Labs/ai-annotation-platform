"""rapidocr-backend · RapidOCR OCR 任务族 + 受管 GPU 生命周期。

三个 det/rec/e2e 对外 model 共享按权重三件套分组的动态引擎池。每个池条目完整拥有
det/cls/rec 三条 ORT session；single-flight builder、borrower、per-entry use lock 与
取消安全清理共同守住容量与驻留真值。

端点：
    GET  /health、/setup、/versions
    POST /predict、/warmup、/unload
    POST /drain、/drain/cancel、/lifecycle/mode、/lifecycle/reset

端口 8005（compose profile gpu-rapidocr）。
"""

from __future__ import annotations

from aap_protocol_v2.predict import normalize_context_task
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from aap_task_contract import normalize_task

import asyncio
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

import catalog
from engine_pool import EngineBuildTimeout, EnginePool, EnginePoolBusyError
from gpu_lifecycle import RapidOcrGpuLifecycle, WorkloadOperation
from predictor import (
    RapidOCREngineBuildError,
    RapidOCREngineFactory,
    RapidOCRPredictor,
    inspect_engine_providers,
)
from schemas import BatchPredictRequest

logger = logging.getLogger("rapidocr-backend")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

BACKEND_VERSION = "0.1.0"
MODEL_VERSION = "rapidocr-v3.9.0"

POOL_CAP = int(os.environ.get("RAPIDOCR_POOL_CAP", "3"))
BUILD_TIMEOUT = float(os.environ.get("RAPIDOCR_BUILD_TIMEOUT", "30"))
IDLE_UNLOAD_SECONDS = float(os.environ.get("RAPIDOCR_IDLE_UNLOAD_SECONDS", "600"))
IDLE_CHECK_INTERVAL = float(os.environ.get("RAPIDOCR_IDLE_CHECK_INTERVAL", "60"))
# 受管代码路径与部署物理验证解耦：在满池实卡 load→unload 回落完成前，
# 不对外声明 managed_lifecycle，也不允许进入 enforce gate。
MANAGED_LIFECYCLE_VERIFIED = deployment_verified_flag(
    "RAPIDOCR_MANAGED_LIFECYCLE_VERIFIED"
)

_engine_factory: RapidOCREngineFactory | None = None
_engine_pool: EnginePool | None = None
_predictor: RapidOCRPredictor | None = None
_gpu_lifecycle: RapidOcrGpuLifecycle | None = None
_idle_task: asyncio.Task[None] | None = None


async def _idle_watcher() -> None:
    """整池空闲超时时经同一 lifecycle/pool 所有权边界卸载。"""

    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if IDLE_UNLOAD_SECONDS <= 0 or _gpu_lifecycle is None:
                continue
            idle_before = time.monotonic() - IDLE_UNLOAD_SECONDS
            count = await _gpu_lifecycle.try_idle_unload(idle_before=idle_before)
            if count:
                logger.info("idle unloaded %d RapidOCR engines", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("idle_watcher error: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _engine_factory, _engine_pool, _predictor, _gpu_lifecycle, _idle_task

    validate_single_gpu_device_set()
    raw_keyring = os.environ.get("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "").strip()
    verify_keyring = load_verify_keyring(raw_keyring) if raw_keyring else {}
    _engine_factory = RapidOCREngineFactory()
    _engine_pool = EnginePool(
        POOL_CAP,
        _engine_factory.build,
        inspect_engine_providers,
        build_timeout=BUILD_TIMEOUT,
    )
    _predictor = RapidOCRPredictor(_engine_pool)
    _gpu_lifecycle = RapidOcrGpuLifecycle(
        _engine_pool,
        verify_keyring=verify_keyring,
        evictable_verified=MANAGED_LIFECYCLE_VERIFIED,
    )
    _idle_task = asyncio.create_task(_idle_watcher())
    logger.info(
        "rapidocr-backend ready (lazy): model_dir=%s cap=%d configured_device=%s "
        "idle_unload=%.0fs managed_verified=%s",
        catalog.MODELS_DIR,
        POOL_CAP,
        _engine_factory.configured_device(),
        IDLE_UNLOAD_SECONDS,
        MANAGED_LIFECYCLE_VERIFIED,
    )
    try:
        yield
    finally:
        if _idle_task is not None:
            _idle_task.cancel()
            try:
                await _idle_task
            except BaseException:
                pass
        if _gpu_lifecycle is not None:
            await _gpu_lifecycle.shutdown()


app = FastAPI(title="rapidocr-backend", version=BACKEND_VERSION, lifespan=lifespan)
install_backend_routing_controller(app)


def _echo_gpu_health_challenge(request: Request, response: Response) -> None:
    challenge = match_gpu_health_challenge(
        request.headers.getlist(GPU_HEALTH_CHALLENGE_HEADER),
        request.query_params.getlist(GPU_HEALTH_CHALLENGE_QUERY_PARAM),
    )
    if challenge is not None:
        response.headers[GPU_HEALTH_CHALLENGE_HEADER] = challenge
        response.headers["Cache-Control"] = "no-store"


def _pool_status(snapshot: dict[str, Any]) -> dict[str, Any]:
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
            "cap": POOL_CAP,
            "current_size": 0,
            "loaded_keys": [],
            "last_evict": None,
            "provider": None,
        }
        residency_payload = None
    return {
        "status": "ok",
        "service": "rapidocr-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "ready": _engine_pool is not None,
        "loaded_engines": pool_snapshot["current_size"],
        "pool": _pool_status(pool_snapshot),
        # effective_provider 只来自已加载引擎的 det/cls/rec 业务 session；
        # 空池、私有所有权链不可读或 provider 混合时为 None。
        "compute": {
            "configured_device": (
                _engine_factory.configured_device()
                if _engine_factory is not None
                else "cpu"
            ),
            "effective_provider": pool_snapshot["provider"],
            "cpu_fallback_supported": True,
        },
        "gpu_info": physical_gpu_identity() or None,
        "residency": residency_payload,
    }


@app.get("/setup")
def setup() -> dict[str, Any]:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "task_contract_revision": 1,
        "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
        "name": "rapidocr-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "is_interactive": False,
        "supported_prompts": ["none"],
        "supported_geometric_outputs": ["polygon"],
        "infra": "onnx",
        "warmup_endpoint": True,
        "models": catalog.model_entries(),
    }
    if MANAGED_LIFECYCLE_VERIFIED:
        payload["managed_lifecycle"] = ManagedLifecycleCapabilities().model_dump(
            mode="json"
        )
    return payload


@app.get("/versions")
def versions() -> dict[str, Any]:
    return versions_payload(MODEL_VERSION, BACKEND_VERSION)


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
            status_code=422,
            detail="invalid JSON request body",
        ) from exc


async def _optional_request_json(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    if not raw_body.strip():
        return {}
    body = await _request_json(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be an object")
    return body


def _warmup_target(body: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    """同时接受旧 ``context`` wire 与模型市场的 ``task/variants`` wire。"""

    context = body.get("context")
    if context is not None and not isinstance(context, dict):
        raise HTTPException(status_code=422, detail="context must be an object")
    source = context if isinstance(context, dict) else body
    model_id = source.get("model_id")
    variants = source.get("model_variants")
    if variants is None:
        variants = body.get("variants")
    if model_id is None:
        try:
            task = (
                normalize_task(body["task"]) if body.get("task") is not None else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if task in (None, "ocr"):
            model_id = catalog.E2E_MODEL_ID
        elif task == "detect":
            model_id = catalog.DET_MODEL_ID
        else:
            raise HTTPException(
                status_code=422, detail=f"unsupported warmup task: {task}"
            )
    try:
        normalize_context_task(
            source, model_task="detect" if model_id == catalog.DET_MODEL_ID else "ocr"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not isinstance(model_id, str):
        raise HTTPException(status_code=422, detail="model_id must be a string")
    if variants is not None:
        if not isinstance(variants, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in variants.items()
        ):
            raise HTTPException(
                status_code=422,
                detail="model variants must be a string-to-string object",
            )
    return model_id, variants


def _extract_params(context: dict[str, Any]) -> dict[str, Any]:
    """读取结构化 ``context.params``，并兼容历史 flat 阈值。"""

    params = context.get("params")
    out: dict[str, Any] = dict(params) if isinstance(params, dict) else {}
    for key in ("text_score", "box_thresh", "unclip_ratio"):
        if key not in out and key in context:
            out[key] = context[key]
    return out


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
    operation = await _gpu_lifecycle.begin_workload(
        scope,
        generation_header=generation_header,
        token=token,
    )
    operation._backend_routing_controller = getattr(
        request.app.state, "backend_routing_controller", None
    )
    return operation


def _track_operation_future(operation: WorkloadOperation, future: Any) -> None:
    operation.track_future(future)
    controller = getattr(operation, "_backend_routing_controller", None)
    if controller is not None:
        controller.track_background_future(future)


@app.post("/warmup", response_model=WarmupResponse)
async def warmup(request: Request) -> WarmupResponse:
    """在 lifecycle admission 之后预加载一个 composite engine。"""

    if _engine_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    operation = await _begin_workload(request, AdmissionScope.WARMUP)
    resolved = None
    try:
        model_id, variants = _warmup_target(await _optional_request_json(request))
        try:
            resolved = catalog.resolve(model_id, variants)
            cache_hit, load_ms, evicted = await _engine_pool.warmup(resolved)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except EngineBuildTimeout as exc:
            _track_operation_future(operation, exc.builder)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except EnginePoolBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RapidOCREngineBuildError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.CancelledError:
            if resolved is not None:
                _track_operation_future(
                    operation, _engine_pool.builder_for_now(resolved)
                )
            raise
        return WarmupResponse(
            ok=True,
            model_load_ms=load_ms,
            cache_hit=cache_hit,
            evicted=evicted,
        )
    finally:
        await operation.close()


async def _run_predict(
    req: BatchPredictRequest,
    *,
    operation: WorkloadOperation,
) -> BatchPredictResponse:
    if _predictor is None or _engine_pool is None:
        raise HTTPException(status_code=503, detail="backend not ready")

    model_id = req.context.get("model_id", catalog.E2E_MODEL_ID)
    try:
        req.context = normalize_context_task(
            req.context,
            model_task="detect" if model_id == catalog.DET_MODEL_ID else "ocr",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    variants = req.context.get("model_variants")
    params = _extract_params(req.context)
    if variants is not None and (
        not isinstance(variants, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in variants.items()
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="model_variants must be a string-to-string object",
        )
    try:
        resolved = catalog.resolve(model_id, variants)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    results: list[PredictionResult] = []
    for task in req.tasks:
        try:
            items, cache_hit, load_ms, inference_ms = await _predictor.predict_one(
                model_id,
                resolved,
                task.file_path,
                params,
            )
            score = max((item.get("score", 0.0) for item in items), default=0.0)
            results.append(
                PredictionResult(
                    task=task.id,
                    result=items,
                    score=score,
                    model_version=f"{MODEL_VERSION}/{model_id}",
                    inference_time_ms=inference_ms,
                    cache_hit=cache_hit,
                    model_load_ms=load_ms,
                )
            )
        except EngineBuildTimeout as exc:
            _track_operation_future(operation, exc.builder)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except EnginePoolBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RapidOCREngineBuildError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.CancelledError:
            _track_operation_future(operation, _engine_pool.builder_for_now(resolved))
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
    """无 body 保持 legacy wire；generation body 进入受管全池卸载。"""

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
