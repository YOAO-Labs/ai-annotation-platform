from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import re
import time
from dataclasses import dataclass

import httpx
import structlog
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    DrainTransitionResponse,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GenerationTransitionRequest,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetRequest,
    LifecycleResetResponse,
    ManagedUnloadResponse,
    parse_drain_transition_response_json,
    parse_lifecycle_mode_response_json,
    parse_lifecycle_reset_response_json,
    parse_managed_unload_response_json,
)
from fastapi import HTTPException

from app.config import GPUArbiterMode, settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.observability.metrics import observe_ml_backend
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchContextFactory,
    GPUDispatchGrant,
    GPUDispatchOperation,
    GPUDispatchRequest,
    GPUShadowSessionFactory,
)
from app.services.gpu_arbitration.policy import (
    GPUClaimConfigurationError,
    any_gpu_resource_effectively_enforced,
    backend_is_trusted_explicit_cpu,
    effective_gpu_arbiter_mode,
    gpu_shadow_observation_enabled,
    record_gpu_shadow_dispatch,
    validate_gpu_claim,
)
from app.services.gpu_arbitration.rollout_state import (
    GPUArbiterRolloutUnavailable,
    gpu_rollout_boundary_active,
    resolve_gpu_arbiter_rollout,
)

logger = structlog.get_logger(__name__)

# v0.9.12 BUG B-17 · per-backend asyncio.Semaphore 限速. 受 ml_backends.extra_params.max_concurrency
# 控制 (默认 4, 匹配现有 celery worker --concurrency=4 不破坏既有行为). 改 extra_params 后需 worker
# 重启才生效 (每个存活 event loop 按 backend_id 缓存; 见 docs-site/dev/concepts/ai-models.md).
_DEFAULT_MAX_CONCURRENCY = 4
_GPU_SHADOW_OBSERVER_TIMEOUT_SECONDS = 0.25
GPU_HEALTH_CHALLENGE_HEADER = "X-AAP-GPU-Health-Challenge"
GPU_HEALTH_CHALLENGE_QUERY_PARAM = "aap_gpu_health_challenge"
GPU_HEALTH_CHALLENGE_ECHO_MARKER = "_aap_gpu_health_challenge_echo"
_GPU_HEALTH_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")
_SEMAPHORE_CACHE_ATTR = "_aap_ml_backend_semaphores"


_SAFE_BACKEND_DETAIL_KEYS = {
    "reason",
    "message",
    "max_bytes",
    "declared_bytes",
    "received_bytes",
    "model_id",
    "model_ids",
    "requested",
    "supported_prompts",
    "supported_geometric_outputs",
    "candidate_index",
}


def _backend_detail(resp: httpx.Response) -> str | dict:
    """提取上游 backend 的错误说明: 优先 JSON 的 detail/error/message, 回退裁剪后的 text."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("detail", "error", "message"):
                val = data.get(key)
                if val:
                    if isinstance(val, dict) and isinstance(val.get("reason"), str):
                        return {
                            k: v
                            for k, v in val.items()
                            if k in _SAFE_BACKEND_DETAIL_KEYS
                        }
                    return str(val)
    except Exception:
        pass
    return (resp.text or "")[:512]


def _raise_for_backend_status(resp: httpx.Response) -> None:
    """把上游 backend 的非 2xx 映射成对前端友好的 HTTPException:

    - 上游 4xx (如 SAM3 不支持 point 的 400) → 原样透传 4xx, 不再被放大成 500.
    - 上游 5xx → 502 Bad Gateway, 表明是 backend 故障而非平台故障.

    交互式探针 (warmup) 拿到透传的 400 后, 前端全局拦截器 (只对 403/>=500 弹 toast)
    不再刷屏 "服务器错误 HTTP 500".
    """
    if resp.status_code < 400:
        return
    detail = _backend_detail(resp)
    headers: dict[str, str] | None = None
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        headers = {"Retry-After": retry_after}
    client_detail = detail if isinstance(detail, dict) else f"ML backend: {detail}"
    if resp.status_code < 500 or resp.status_code == 503:
        raise HTTPException(
            status_code=resp.status_code,
            detail=client_detail,
            headers=headers,
        )
    if isinstance(detail, dict):
        raise HTTPException(status_code=502, detail=detail)
    raise HTTPException(status_code=502, detail=f"ML backend error: {detail}")


async def _bounded_stream_response(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> httpx.Response:
    declared = response.headers.get("content-length")
    if declared:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "mask_response_too_large",
                    "message": "ML backend response exceeds the byte budget",
                    "max_bytes": max_bytes,
                    "declared_bytes": declared_bytes,
                },
            )
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "mask_response_too_large",
                    "message": "ML backend response exceeds the byte budget",
                    "max_bytes": max_bytes,
                    "received_bytes": len(body),
                },
            )
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=bytes(body),
        request=response.request,
    )


def _get_semaphore(backend_id: str | None, max_cc: int) -> asyncio.Semaphore | None:
    if not backend_id:
        return None
    loop = asyncio.get_running_loop()
    loop_semaphores = getattr(loop, _SEMAPHORE_CACHE_ATTR, None)
    if loop_semaphores is None:
        loop_semaphores = {}
        setattr(loop, _SEMAPHORE_CACHE_ATTR, loop_semaphores)
    sem = loop_semaphores.get(backend_id)
    if sem is None:
        sem = asyncio.Semaphore(max(1, int(max_cc)))
        loop_semaphores[backend_id] = sem
    return sem


@dataclass
class PredictionResult:
    task_id: str
    result: list[dict]
    score: float | None = None
    model_version: str | None = None
    inference_time_ms: int | None = None
    cache_hit: bool | None = None
    model_load_ms: int | None = None
    # v0.9.11 · token / cost 透传 (LLM-backed backend 才有, grounded-sam2 当前留 None).
    # worker 累加到 async_job.result.total_cost, prediction_meta 单条留档.
    meta: dict | None = None
    # v0.18.18 · 交互单实例精修的 low-res logits 回灌 (base64); 平台仅透传, 前端原样回带.
    mask_input_next: str | None = None
    diagnostic: dict | None = None


class MLBackendClient:
    def __init__(
        self,
        backend: MLBackendRegistry,
        *,
        shadow_session_factory: GPUShadowSessionFactory | None = None,
        dispatch_context_factory: GPUDispatchContextFactory | None = None,
    ) -> None:
        self._backend = backend
        self.base_url = backend.url.rstrip("/")
        self.auth_method = backend.auth_method
        self.auth_token = backend.auth_token
        raw_backend_id = getattr(backend, "id", None)
        self.backend_id = str(raw_backend_id) if raw_backend_id else None
        self.gpu_resource_id = getattr(backend, "gpu_resource_id", None)
        self.vram_budget_mb = getattr(backend, "vram_budget_mb", None)
        self._shadow_session_factory = shadow_session_factory
        self._dispatch_context_factory = dispatch_context_factory
        extra = getattr(backend, "extra_params", None) or {}
        self._extra_params = extra
        self.max_concurrency = int(
            extra.get("max_concurrency", _DEFAULT_MAX_CONCURRENCY)
        )

    @property
    def _semaphore(self) -> asyncio.Semaphore | None:
        return _get_semaphore(self.backend_id, self.max_concurrency)

    async def _observe_gpu_dispatch(self, operation: str) -> None:
        """Record P2 shadow decisions and fail open without changing the request."""

        if (
            self.backend_id is None
            or self._shadow_session_factory is None
            or not gpu_shadow_observation_enabled(self.gpu_resource_id)
        ):
            return
        try:
            # API 路径可能仍持有请求事务连接；严格限制旁路查询等待，避免连接池
            # 饥饿把非权威 observe 变成业务派发阻塞点。
            async with asyncio.timeout(_GPU_SHADOW_OBSERVER_TIMEOUT_SECONDS):
                await record_gpu_shadow_dispatch(
                    self.backend_id,
                    operation,
                    self._shadow_session_factory,
                )
        except Exception:  # noqa: BLE001 - observe must never block business traffic
            logger.warning(
                "gpu_arbiter_shadow_observer_failed",
                gpu_arbiter={
                    "backend_id": self.backend_id,
                    "resource_id": self.gpu_resource_id,
                    "operation": operation,
                    "authoritative": False,
                },
                exc_info=True,
            )

    @asynccontextmanager
    async def _gpu_dispatch(
        self,
        operation: GPUDispatchOperation,
        scope: AdmissionScope,
    ):
        """Wrap one residency-changing HTTP call in the mode-specific boundary."""

        resources = settings.gpu_arbiter_resources
        resource_is_configured = self.gpu_resource_id in resources
        unclaimed_explicit_cpu = (
            self.gpu_resource_id is None
            and self.vram_budget_mb is None
            and backend_is_trusted_explicit_cpu(self._backend)
        )
        rollout_boundary_active = False
        if settings.gpu_arbiter_rollout_enabled:
            if unclaimed_explicit_cpu:
                effective_mode = GPUArbiterMode.OFF
            else:
                if self._shadow_session_factory is None:
                    raise GPUArbiterDispatchError(
                        GPUArbiterErrorCode.NOT_READY,
                        message="GPU rollout resolver is not configured",
                    )
                try:
                    if self.gpu_resource_id is not None:
                        rollout = await resolve_gpu_arbiter_rollout(
                            self._shadow_session_factory,
                            self.gpu_resource_id,
                        )
                        if rollout.dispatch_blocked:
                            raise GPUArbiterDispatchError(
                                GPUArbiterErrorCode.NOT_READY,
                                message=rollout.blocked_reason
                                or "GPU rollout transition is not ready",
                            )
                        assert rollout.dispatch_mode is not None
                        effective_mode = rollout.dispatch_mode
                        if not resource_is_configured:
                            rollout_boundary_active = await gpu_rollout_boundary_active(
                                self._shadow_session_factory
                            )
                    else:
                        effective_mode = GPUArbiterMode.OFF
                        rollout_boundary_active = await gpu_rollout_boundary_active(
                            self._shadow_session_factory
                        )
                except GPUArbiterRolloutUnavailable as exc:
                    raise GPUArbiterDispatchError(
                        GPUArbiterErrorCode.UNAVAILABLE,
                        message=str(exc),
                    ) from exc
        else:
            effective_mode = (
                effective_gpu_arbiter_mode(self.gpu_resource_id)
                if self.gpu_resource_id is not None
                else GPUArbiterMode.OFF
            )
        if effective_mode is GPUArbiterMode.ENFORCE:
            try:
                validate_gpu_claim(
                    self.gpu_resource_id,
                    self.vram_budget_mb,
                    extra_params=self._extra_params,
                )
            except GPUClaimConfigurationError as exc:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message=str(exc),
                ) from exc
            if self.backend_id is None:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message="GPU backend registry identity is missing",
                )
            if self._dispatch_context_factory is None:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.NOT_READY,
                    message="GPU arbiter dispatch authority is not configured",
                )
            request = GPUDispatchRequest(
                backend_id=self.backend_id,
                gpu_resource_id=self.gpu_resource_id,
                operation=operation,
                scope=scope,
            )
            boundary_error: BaseException | None = None
            async with self._dispatch_context_factory(request) as grant:
                try:
                    if not isinstance(grant, GPUDispatchGrant):
                        raise GPUArbiterDispatchError(
                            GPUArbiterErrorCode.UNAVAILABLE,
                            message=(
                                "GPU arbiter dispatch authority returned an invalid grant"
                            ),
                        )
                    yield grant
                except BaseException as exc:
                    boundary_error = exc
                    if isinstance(grant, GPUDispatchGrant):
                        grant.report_uncertain_if_missing("request_aborted")
                    raise
                else:
                    grant.report_uncertain_if_missing("response_not_reported")
            if boundary_error is not None:
                # A faulty authority context must not turn backend errors or
                # cancellation into an apparent successful dispatch.
                raise boundary_error
            return

        if (
            not resource_is_configured
            and not unclaimed_explicit_cpu
            and (
                rollout_boundary_active
                if settings.gpu_arbiter_rollout_enabled
                else any_gpu_resource_effectively_enforced()
            )
        ):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CONFIG_INVALID,
                message=(
                    "GPU backend must declare a valid configured resource before "
                    "dispatch while any resource is effectively enforced"
                ),
            )

        await self._observe_gpu_dispatch(operation)
        yield None

    def _headers(self, grant: GPUDispatchGrant | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_method == "token" and self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if grant is not None:
            headers[GPU_GENERATION_HEADER] = grant.generation
            headers[GPU_ADMISSION_TOKEN_HEADER] = grant.admission_token
        return headers

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/health", headers=self._headers()
                )
                return resp.status_code == 200
        except (httpx.RequestError, httpx.TimeoutException):
            return False

    async def health_meta(
        self,
        *,
        gpu_health_challenge: str | None = None,
    ) -> tuple[bool, dict | None]:
        """v0.9.6 · 拉 /health 完整响应; 上层 service 把 gpu_info/cache/model_version 缓存到 ml_backends.health_meta.

        返回 (ok, meta?); meta 仅在 ok=True 且响应 JSON 时返回, 否则 None.
        """
        if gpu_health_challenge is not None and (
            not isinstance(gpu_health_challenge, str)
            or _GPU_HEALTH_CHALLENGE_RE.fullmatch(gpu_health_challenge) is None
        ):
            raise ValueError(
                "gpu_health_challenge must be 64 lowercase hexadecimal characters"
            )
        base_headers = self._headers()
        headers = dict(base_headers)
        params: dict[str, str] | None = None
        if gpu_health_challenge is not None:
            headers[GPU_HEALTH_CHALLENGE_HEADER] = gpu_health_challenge
            headers["Cache-Control"] = "no-cache"
            params = {GPU_HEALTH_CHALLENGE_QUERY_PARAM: gpu_health_challenge}
        try:
            async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/health",
                    headers=headers,
                    params=params,
                )
                response_challenge = gpu_health_challenge
                if gpu_health_challenge is not None and resp.status_code in (400, 422):
                    resp = await client.get(
                        f"{self.base_url}/health",
                        headers=base_headers,
                    )
                    response_challenge = None
                if resp.status_code != 200:
                    return False, None
                try:
                    data = resp.json()
                except Exception:
                    return True, None
                # v0.9.11 · 加 host (PerfHud 容器 CPU/RAM); gpu_info/cache/model_version 保留
                # v0.10.26 · 加 pool (cap / loaded_variants / per_variant_lru_ts), v0.14.14
                # 升级到协议 §4.3 PoolStatus (cap / current_size / loaded_keys / last_evict),
                # 这里整段透传; 模型市场变体面板按字段优先级展示 (backend 无 pool 时静默跳过).
                # v0.10.36 · 加 video_pool (cap / loaded_variants / active_sessions / idle_seconds),
                # 供视频追踪显存池观测 (backend 无该字段时静默跳过; video pool 协议化留下版).
                # v0.22.3 WS4 · 加 compute (configured_device/effective_device/effective_provider),
                # 暴露 backend 是否已静默退回 CPU; platform overview + PerfHud 两路均依赖此处放行.
                meta = {
                    k: data[k]
                    for k in (
                        "gpu_info",
                        "host",
                        "cache",
                        "model_version",
                        "loaded",
                        "idle_unload_seconds",
                        "last_request_age_seconds",
                        "pool",
                        "video_pool",
                        "compute",
                        # ADR-0049 · 全 pool/session 驻留真值，与 compute 配置意图分离。
                        "residency",
                    )
                    if k in data
                }
                if response_challenge is not None and resp.headers.get_list(
                    GPU_HEALTH_CHALLENGE_HEADER
                ) == [response_challenge]:
                    meta[GPU_HEALTH_CHALLENGE_ECHO_MARKER] = response_challenge
                return True, meta or None
        except (httpx.RequestError, httpx.TimeoutException):
            return False, None

    async def _acquire(self):
        """v0.9.12 · per-backend Semaphore 限速 context manager. 无 backend_id 时降级为 noop."""

        class _NullCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        semaphore = self._semaphore
        if semaphore is None:
            return _NullCtx()
        return semaphore

    async def install_trained_model(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.base_url}/managed-models", json=payload, headers=self._headers()
            )
            _raise_for_backend_status(response)
            return response.json()

    async def trained_model_receipt(self, version_id: str) -> dict:
        async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
            response = await client.get(
                f"{self.base_url}/managed-models/{version_id}", headers=self._headers()
            )
            _raise_for_backend_status(response)
            return response.json()

    async def predict(
        self, tasks: list[dict], context: dict | None = None
    ) -> list[PredictionResult]:
        start = time.monotonic()
        outcome = "success"
        payload: dict = {"tasks": tasks}
        if context:
            # v0.9.5 · 批量预标透传 context（含 type=text + prompt + output 三模式 + DINO 阈值）。
            payload["context"] = context
        try:
            async with await self._acquire():
                async with self._gpu_dispatch(
                    "predict", AdmissionScope.PREDICT
                ) as grant:
                    async with httpx.AsyncClient(
                        timeout=settings.ml_predict_timeout
                    ) as client:
                        resp = await client.post(
                            f"{self.base_url}/predict",
                            json=payload,
                            headers=self._headers(grant),
                        )
                        if grant is not None:
                            grant.report_response(resp.status_code)
                        resp.raise_for_status()
                        data = resp.json()
        except Exception:
            outcome = "error"
            observe_ml_backend(self.backend_id, outcome, time.monotonic() - start)
            raise

        wall_ms = int((time.monotonic() - start) * 1000)
        observe_ml_backend(self.backend_id, outcome, wall_ms / 1000)

        results = []
        for item in data.get("results", []):
            # 优先用 backend 自报的 inference_time_ms（去 IO 开销更准），缺失则回退 wall clock。
            results.append(
                PredictionResult(
                    task_id=item.get("task"),
                    result=item.get("result", []),
                    score=item.get("score"),
                    model_version=item.get("model_version"),
                    inference_time_ms=item.get("inference_time_ms") or wall_ms,
                    meta=item.get(
                        "meta"
                    ),  # v0.9.11 · LLM cost/token (grounded-sam2 不返回)
                )
            )
        return results

    async def predict_interactive(
        self,
        task_data: dict,
        context: dict,
        *,
        max_response_bytes: int | None = None,
    ) -> PredictionResult:
        start = time.monotonic()
        outcome = "success"
        try:
            async with await self._acquire():
                try:
                    async with self._gpu_dispatch(
                        "predict_interactive", AdmissionScope.PREDICT
                    ) as grant:
                        async with httpx.AsyncClient(
                            timeout=settings.ml_predict_timeout
                        ) as client:
                            request_kwargs = {
                                "json": {"task": task_data, "context": context},
                                "headers": self._headers(grant),
                            }
                            if max_response_bytes is None:
                                resp = await client.post(
                                    f"{self.base_url}/predict",
                                    **request_kwargs,
                                )
                            else:
                                async with client.stream(
                                    "POST",
                                    f"{self.base_url}/predict",
                                    **request_kwargs,
                                ) as streamed:
                                    if grant is not None:
                                        grant.report_response(streamed.status_code)
                                    resp = await _bounded_stream_response(
                                        streamed,
                                        max_bytes=max_response_bytes,
                                    )
                            if grant is not None and max_response_bytes is None:
                                grant.report_response(resp.status_code)
                            # 上游 4xx 原样透传, 5xx → 502 (见 _raise_for_backend_status)
                            _raise_for_backend_status(resp)
                            try:
                                data = json.loads(resp.content)
                            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                                raise HTTPException(
                                    status_code=502,
                                    detail={
                                        "reason": "invalid_backend_response",
                                        "message": "ML backend returned invalid JSON",
                                    },
                                ) from exc
                            if not isinstance(data, dict):
                                raise HTTPException(
                                    status_code=502,
                                    detail={
                                        "reason": "invalid_backend_response",
                                        "message": "ML backend response must be an object",
                                    },
                                )
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    # 转换发生在 dispatch context 退出后，使 authority 能看到
                    # 原始 timeout/connect 不确定结果。
                    raise HTTPException(
                        status_code=502,
                        detail=f"ML backend unreachable: {exc}",
                    ) from exc
        except Exception:
            # 仍记 error 指标 (HTTPException 也走这里), 再原样抛出, 指标语义不变.
            outcome = "error"
            observe_ml_backend(self.backend_id, outcome, time.monotonic() - start)
            raise

        wall_ms = int((time.monotonic() - start) * 1000)
        observe_ml_backend(self.backend_id, outcome, wall_ms / 1000)

        return PredictionResult(
            task_id=task_data.get("id", ""),
            result=data.get("result", []),
            score=data.get("score"),
            model_version=data.get("model_version"),
            inference_time_ms=data.get("inference_time_ms") or wall_ms,
            cache_hit=data.get("cache_hit"),
            model_load_ms=data.get("model_load_ms"),
            meta=data.get("meta"),
            mask_input_next=data.get("mask_input_next"),
            diagnostic=data.get("diagnostic"),
        )

    async def unload(self) -> dict:
        """B-28+ · 让 backend 卸载模型释放显存. backend 必须实现 POST /unload."""
        async with self._gpu_dispatch("unload", AdmissionScope.UNLOAD) as grant:
            async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
                request_kwargs: dict = {"headers": self._headers(grant)}
                if grant is not None:
                    request_kwargs["json"] = {"generation": grant.generation}
                resp = await client.post(
                    f"{self.base_url}/unload",
                    **request_kwargs,
                )
                if grant is not None:
                    grant.report_response(resp.status_code)
                resp.raise_for_status()
                return resp.json()

    async def reload(
        self,
        sam_variant: str | None = None,
        dino_variant: str | None = None,
        task_type: str | None = None,
    ) -> dict:
        """B-28+ · 让 backend 重新加载模型. 重载耗时可能远高于 health 探活, 用 predict 超时配额.

        v0.10.26 · 可选指定变体预热 (模型市场单变体预热); 缺省时 body 留空, backend 用默认变体.
        v0.10.36 · 可选 task_type="video" 预热独立 video tracker 池 (仅认 sam_variant, 无 dino).
        """
        body: dict[str, str] = {}
        if sam_variant:
            body["sam_variant"] = sam_variant
        if dino_variant:
            body["dino_variant"] = dino_variant
        if task_type:
            body["task_type"] = task_type
        async with self._gpu_dispatch("reload", AdmissionScope.RELOAD) as grant:
            async with httpx.AsyncClient(timeout=settings.ml_predict_timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/reload",
                    json=body or None,
                    headers=self._headers(grant),
                )
                if grant is not None:
                    grant.report_response(resp.status_code)
                resp.raise_for_status()
                return resp.json()

    async def setup(self) -> dict:
        async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
            headers = self._headers()
            headers["Cache-Control"] = "no-cache"
            resp = await client.get(f"{self.base_url}/setup", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def lifecycle_mode(
        self,
        request: LifecycleModeRequest,
        *,
        admission_token: str,
    ) -> LifecycleModeResponse:
        """Send one signed control-plane gate acknowledgement outside dispatch."""

        if (
            not isinstance(admission_token, str)
            or not admission_token
            or admission_token.strip() != admission_token
        ):
            raise ValueError("admission_token must be non-empty and canonical")
        headers = self._headers()
        headers[GPU_ADMISSION_TOKEN_HEADER] = admission_token
        async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
            resp = await client.post(
                f"{self.base_url}/lifecycle/mode",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            if resp.status_code < 200 or resp.status_code >= 300:
                if resp.status_code >= 400:
                    _raise_for_backend_status(resp)
                raise HTTPException(
                    status_code=502,
                    detail="ML backend control endpoint returned a non-success status",
                )
            _raise_for_backend_status(resp)
            return parse_lifecycle_mode_response_json(resp.content)

    async def lifecycle_reset(
        self,
        request: LifecycleResetRequest,
        *,
        admission_token: str,
    ) -> LifecycleResetResponse:
        """Send one signed full-pool reset outside workload dispatch."""

        if (
            not isinstance(admission_token, str)
            or not admission_token
            or admission_token.strip() != admission_token
        ):
            raise ValueError("admission_token must be non-empty and canonical")
        headers = self._headers()
        headers[GPU_ADMISSION_TOKEN_HEADER] = admission_token
        async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
            resp = await client.post(
                f"{self.base_url}/lifecycle/reset",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            if resp.status_code < 200 or resp.status_code >= 300:
                if resp.status_code >= 400:
                    _raise_for_backend_status(resp)
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "ML backend control endpoint returned a non-success status"
                    ),
                )
            _raise_for_backend_status(resp)
            return parse_lifecycle_reset_response_json(resp.content)

    async def _managed_generation_transition(
        self,
        endpoint: str,
        grant: GPUDispatchGrant,
    ) -> httpx.Response:
        if endpoint not in {"/drain", "/drain/cancel", "/unload"}:
            raise ValueError("managed GPU transition endpoint is invalid")
        request = GenerationTransitionRequest(generation=grant.generation)
        try:
            async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    json=request.model_dump(mode="json"),
                    headers=self._headers(grant),
                )
                grant.report_response(response.status_code)
        except BaseException:
            grant.report_uncertain_if_missing("request_aborted")
            raise
        if response.status_code < 200 or response.status_code >= 300:
            if response.status_code >= 400:
                _raise_for_backend_status(response)
            raise HTTPException(
                status_code=502,
                detail="ML backend transition endpoint returned a non-success status",
            )
        return response

    async def lifecycle_drain(
        self,
        grant: GPUDispatchGrant,
    ) -> DrainTransitionResponse:
        """Send one signed generation-fenced drain outside workload dispatch."""

        response = await self._managed_generation_transition("/drain", grant)
        parsed = parse_drain_transition_response_json(response.content)
        if parsed.generation != grant.generation:
            raise ValueError("drain response generation does not match the request")
        return parsed

    async def lifecycle_cancel_drain(
        self,
        grant: GPUDispatchGrant,
    ) -> DrainTransitionResponse:
        """Send one signed newer-generation drain cancellation."""

        response = await self._managed_generation_transition("/drain/cancel", grant)
        parsed = parse_drain_transition_response_json(response.content)
        if parsed.generation != grant.generation:
            raise ValueError(
                "drain cancellation response generation does not match the request"
            )
        return parsed

    async def lifecycle_unload(
        self,
        grant: GPUDispatchGrant,
    ) -> ManagedUnloadResponse:
        """Send one signed full-pool unload outside workload dispatch."""

        response = await self._managed_generation_transition("/unload", grant)
        parsed = parse_managed_unload_response_json(response.content)
        if parsed.generation != grant.generation:
            raise ValueError("unload response generation does not match the request")
        return parsed

    async def warmup(self, body: dict) -> dict:
        """v0.14.14 协议 §4.4 · 把指定 variant 权重加载到 pool, 不跑 forward.

        body 由前端原样传入 (各 backend schema 不同: yolo 要 task+variants{series,size},
        gsam2 要 variants{sam_variant,dino_variant}, sam3 可空或 variants{model_variant}).
        响应统一为 {ok, model_load_ms, cache_hit, evicted}. 用 predict 超时配额 (加载
        可能数秒).
        """
        async with self._gpu_dispatch("warmup", AdmissionScope.WARMUP) as grant:
            async with httpx.AsyncClient(timeout=settings.ml_predict_timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/warmup",
                    json=body or {},
                    headers=self._headers(grant),
                )
                if grant is not None:
                    grant.report_response(resp.status_code)
                resp.raise_for_status()
                return resp.json()

    async def get_versions(self) -> list[str]:
        async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as client:
            resp = await client.get(
                f"{self.base_url}/versions", headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("versions", [])
