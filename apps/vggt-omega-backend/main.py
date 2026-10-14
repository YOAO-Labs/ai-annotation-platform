"""One-process VGGT-Omega reconstruction service and GPU lifecycle endpoints."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from aap_backend_runtime import (
    BuildArtifact,
    ManagedBuildTimeout,
    ManagedLruPool,
    ManagedPoolBusyError,
    physical_gpu_identity,
    validate_single_gpu_device_set,
    versions_payload,
)
from aap_protocol_v2 import COMPAT_PROTOCOL_VERSIONS, PROTOCOL_VERSION, WarmupResponse
from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    AdmissionScope,
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

from config import (
    CAMERA_COUNT,
    EXECUTION_SHAPE,
    MAX_DIMENSION,
    MAX_IMAGE_BYTES,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    PROFILE,
    SOURCE_COMMIT,
    WEIGHTS_REVISION,
    WEIGHTS_SHA256,
    Config,
)
from gpu_lifecycle import OmegaGpuLifecycle, WorkloadOperation
from predictor import OmegaModel, ProducerUnavailable, strict_gpu_cleanup, validate_vendor
from schemas import ReconstructRequest, ReconstructResponse
from service import ReconstructionError, ReconstructionService

logger = logging.getLogger("vggt-omega-backend")
BACKEND_VERSION = "0.1.0"
MODEL_VERSION = f"vggt-omega:{SOURCE_COMMIT[:12]}:{WEIGHTS_SHA256[:12]}:{PROFILE}"
_config = Config.from_environment()
_pool: ManagedLruPool | None = None
_lifecycle: OmegaGpuLifecycle | None = None
_service: ReconstructionService | None = None


def require_auth(request: Request) -> None:
    """Use the registry's existing bearer authentication contract."""
    if _config.auth_token:
        values = request.headers.getlist("authorization")
        expected = f"Bearer {_config.auth_token}"
        if len(values) != 1 or not hmac.compare_digest(values[0], expected):
            raise HTTPException(
                status_code=401,
                detail="backend authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )


def _build_model(profile: str) -> BuildArtifact:
    if profile != PROFILE:
        raise ProducerUnavailable("unsupported reconstruction profile")
    return BuildArtifact(OmegaModel(_config.weights))


def _preflight(profile: str) -> None:
    if profile != PROFILE or not _config.weights.is_file():
        raise ProducerUnavailable("qualified weights are not provisioned")


async def _idle_watcher() -> None:
    while True:
        await asyncio.sleep(_config.idle_check_interval)
        if _config.idle_unload_seconds > 0 and _lifecycle is not None:
            try:
                await _lifecycle.try_idle_unload(
                    idle_before=time.monotonic() - _config.idle_unload_seconds
                )
            except Exception:
                logger.exception("idle unload failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool, _lifecycle, _service
    import os

    validate_single_gpu_device_set()
    validate_vendor()
    raw_keyring = os.environ.get("GPU_LIFECYCLE_VERIFY_KEYS_JSON", "").strip()
    keyring = load_verify_keyring(raw_keyring) if raw_keyring else {}
    _pool = ManagedLruPool(
        cap=1,
        build_resource=_build_model,
        key_to_str=str,
        strict_cleanup=strict_gpu_cleanup,
        preflight=_preflight,
        build_timeout=_config.build_timeout,
        pool_name="models",
        logger_name="vggt-omega-backend.pool",
    )
    _lifecycle = OmegaGpuLifecycle(
        _pool, verify_keyring=keyring, evictable_verified=_config.managed_verified
    )
    _service = ReconstructionService(_pool, _config)
    idle_task = asyncio.create_task(_idle_watcher())
    try:
        yield
    finally:
        idle_task.cancel()
        await asyncio.gather(idle_task, return_exceptions=True)
        await _lifecycle.shutdown()
        _service.clear_cache()
        _pool = _lifecycle = _service = None


app = FastAPI(
    title="vggt-omega-backend",
    version=BACKEND_VERSION,
    lifespan=lifespan,
    dependencies=[Depends(require_auth)],
)


def _echo_challenge(request: Request, response: Response) -> None:
    challenge = match_gpu_health_challenge(
        request.headers.getlist(GPU_HEALTH_CHALLENGE_HEADER),
        request.query_params.getlist(GPU_HEALTH_CHALLENGE_QUERY_PARAM),
    )
    if challenge is not None:
        response.headers[GPU_HEALTH_CHALLENGE_HEADER] = challenge
        response.headers["Cache-Control"] = "no-store"


@app.get("/health", dependencies=[Depends(_echo_challenge)])
async def health() -> dict[str, Any]:
    if _lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    snapshot, residency = await _lifecycle.snapshot_and_residency()
    return {
        "status": "ok",
        "service": "vggt-omega-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "ready": _config.weights.is_file(),
        "weights_provisioned": _config.weights.is_file(),
        "pool": {
            key: snapshot[key] for key in ("cap", "current_size", "loaded_keys", "last_evict")
        },
        "compute": {
            "configured_device": "cuda",
            "effective_device": snapshot["device"],
            "cpu_fallback_supported": False,
        },
        "gpu_info": physical_gpu_identity() or None,
        "residency": residency.model_dump(mode="json"),
    }


@app.get("/setup")
def setup() -> dict[str, Any]:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
        "name": "vggt-omega-backend",
        "version": BACKEND_VERSION,
        "model_version": MODEL_VERSION,
        "infra": "pytorch",
        "is_interactive": False,
        "supported_prompts": [],
        "supported_geometric_outputs": [],
        "models": [],
        "warmup_endpoint": True,
        "point_cloud_densification": {
            "protocol_version": "1",
            "endpoint": "/reconstruct",
            "profiles": [
                {
                    "id": PROFILE,
                    "camera_count": CAMERA_COUNT,
                    "max_width": MAX_DIMENSION,
                    "max_height": MAX_DIMENSION,
                    "max_image_bytes": MAX_IMAGE_BYTES,
                    "execution_shape": EXECUTION_SHAPE,
                    "outputs": ["depth", "confidence"],
                    "output_width": OUTPUT_WIDTH,
                    "output_height": OUTPUT_HEIGHT,
                    "source_commit": SOURCE_COMMIT,
                    "weights_revision": WEIGHTS_REVISION,
                    "weights_sha256": WEIGHTS_SHA256,
                }
            ],
        },
    }
    if _config.managed_verified:
        payload["managed_lifecycle"] = ManagedLifecycleCapabilities().model_dump(mode="json")
    return payload


@app.get("/versions")
def versions() -> dict[str, Any]:
    return versions_payload(MODEL_VERSION, BACKEND_VERSION)


def _admission_headers(request: Request) -> tuple[str | None, str | None]:
    try:
        return parse_gpu_admission_header_values(
            request.headers.getlist(GPU_GENERATION_HEADER),
            request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
        ) or (None, None)
    except ValueError as exc:
        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc


def _control_token(request: Request) -> str:
    try:
        return parse_gpu_control_token_header_values(
            request.headers.getlist(GPU_GENERATION_HEADER),
            request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
        )
    except ValueError as exc:
        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc


def _ready_lifecycle() -> OmegaGpuLifecycle:
    if _lifecycle is None:
        raise HTTPException(status_code=503, detail="backend not ready")
    return _lifecycle


async def _begin(request: Request, scope: AdmissionScope) -> WorkloadOperation:
    generation, token = _admission_headers(request)
    return await _ready_lifecycle().begin_workload(scope, generation_header=generation, token=token)


async def _body(request: Request, model_type: Any) -> Any:
    # Bound the control envelope; image and tensor bytes never go in this body.
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(status_code=413, detail="request envelope too large")
    try:
        return model_type.model_validate_json(raw)
    except ValidationError as exc:
        # Do not echo signed URLs, auth tokens or raw request values in errors.
        raise HTTPException(status_code=422, detail="invalid request envelope") from exc


@app.post("/reconstruct", response_model=ReconstructResponse)
async def reconstruct(request: Request) -> ReconstructResponse:
    operation = await _begin(request, AdmissionScope.PREDICT)
    try:
        body = await _body(request, ReconstructRequest)
        if _service is None:
            raise HTTPException(status_code=503, detail="backend not ready")
        try:
            return await _service.reconstruct(body, operation)
        except ReconstructionError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"error_code": exc.code, "message": str(exc)},
                headers={"Retry-After": "1"} if exc.status == 503 else None,
            ) from exc
        except ManagedBuildTimeout as exc:
            operation.track_future(exc.builder)
            raise HTTPException(
                status_code=503, detail="model build still active", headers={"Retry-After": "1"}
            ) from exc
        except (ManagedPoolBusyError, ProducerUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.CancelledError:
            if _pool is not None:
                operation.track_future(_pool.builder_for_now(PROFILE))
            raise
        except Exception as exc:
            logger.exception("reconstruction failed for request %s", body.request_id)
            raise HTTPException(status_code=503, detail="model reconstruction failed") from exc
    finally:
        await operation.close()


@app.post("/warmup", response_model=WarmupResponse)
async def warmup(request: Request) -> WarmupResponse:
    operation = await _begin(request, AdmissionScope.WARMUP)
    try:
        if _pool is None:
            raise HTTPException(status_code=503, detail="backend not ready")
        # Existing registry clients may send generic model_id/task fields. This
        # producer has one immutable profile and cannot select another model.
        if (await request.body()).strip():
            try:
                body = await request.json()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid warmup request") from exc
            if not isinstance(body, dict) or body.get("profile", PROFILE) != PROFILE:
                raise HTTPException(status_code=422, detail="unsupported warmup profile")
        try:
            hit, load_ms, evicted = await _pool.warmup(PROFILE)
        except ManagedBuildTimeout as exc:
            operation.track_future(exc.builder)
            raise HTTPException(status_code=503, detail="model build still active") from exc
        except (ManagedPoolBusyError, ProducerUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.CancelledError:
            operation.track_future(_pool.builder_for_now(PROFILE))
            raise
        except Exception as exc:
            logger.exception("warmup failed")
            raise HTTPException(status_code=503, detail="model warmup failed") from exc
        return WarmupResponse(ok=True, cache_hit=hit, model_load_ms=load_ms, evicted=evicted)
    finally:
        await operation.close()


@app.post("/unload")
async def unload(request: Request) -> dict[str, Any]:
    lifecycle = _ready_lifecycle()
    generation, token = _admission_headers(request)
    if not (await request.body()).strip():
        if generation is not None:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        response = await lifecycle.legacy_unload()
    else:
        body = await _body(request, GenerationTransitionRequest)
        response = (
            await lifecycle.managed_unload(
                body.generation, generation_header=generation, token=token
            )
        ).model_dump(mode="json")
    if _service is not None:
        _service.clear_cache()
    return response


@app.post("/drain")
async def drain(request: Request) -> dict[str, Any]:
    generation, token = _admission_headers(request)
    body = await _body(request, GenerationTransitionRequest)
    response = await _ready_lifecycle().drain(
        body.generation, generation_header=generation, token=token
    )
    return response.model_dump(mode="json")


@app.post("/drain/cancel")
async def cancel_drain(request: Request) -> dict[str, Any]:
    generation, token = _admission_headers(request)
    body = await _body(request, GenerationTransitionRequest)
    response = await _ready_lifecycle().cancel_drain(
        body.generation, generation_header=generation, token=token
    )
    return response.model_dump(mode="json")


@app.post("/lifecycle/mode")
async def lifecycle_mode(request: Request) -> dict[str, Any]:
    token = _control_token(request)
    body = await _body(request, LifecycleModeRequest)
    response = await _ready_lifecycle().set_mode(body, token=token)
    return response.model_dump(mode="json")


@app.post("/lifecycle/reset")
async def lifecycle_reset(request: Request) -> dict[str, Any]:
    token = _control_token(request)
    body = await _body(request, LifecycleResetRequest)
    response = await _ready_lifecycle().reset(body, token=token)
    if _service is not None:
        _service.clear_cache()
    return response.model_dump(mode="json")
