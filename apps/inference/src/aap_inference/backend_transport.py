from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Protocol

import httpx
from aap_protocol_v2.predict import normalize_context_task
from aap_task_contract import (
    TASK_ALIASES,
    TASK_VALUES,
    legacy_inference_task,
    normalize_modality_fields,
    normalize_task,
)
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
from .capabilities import resolve_model_task
from .request_tracking import GPU_UNLOAD_ROUTE_EXTENSION, RequestContext, http_client

# BUG B-17 · per-backend asyncio.Semaphore 限速. 受 backend extra_params.max_concurrency
# 控制 (默认 4, 匹配现有 celery worker --concurrency=4 不破坏既有行为). 改 extra_params 后需 worker
# 重启才生效 (每个存活 event loop 按 backend_id 缓存; 见 docs-site/dev/concepts/ai-models.md).
_DEFAULT_MAX_CONCURRENCY = 4
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


class BackendTransportError(Exception):
    """Framework-neutral error carrying the public HTTP error projection."""

    def __init__(
        self,
        status_code: int,
        detail: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(str(detail))


class BackendOptions(Protocol):
    ml_health_timeout: float
    ml_predict_timeout: float


class BackendConfiguration(Protocol):
    url: str
    auth_method: str
    auth_token: str | None
    id: object
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    extra_params: dict | None
    health_meta: dict | None


class BackendDispatchGrant(Protocol):
    generation: str
    admission_token: str

    def report_response(self, status_code: int) -> bool | None: ...

    def report_not_sent(self) -> bool | None: ...

    def request_started(self) -> None: ...

    def report_uncertain_if_missing(self, reason: str) -> bool | None: ...


ErrorFactory = Callable[..., Exception]
DispatchContext = Callable[
    [str, AdmissionScope], AbstractAsyncContextManager[BackendDispatchGrant | None]
]
Observer = Callable[[str | None, str, float], None]


def backend_detail(resp: httpx.Response) -> str | dict:
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


def raise_for_backend_status(
    resp: httpx.Response,
    *,
    error_factory: ErrorFactory = BackendTransportError,
) -> None:
    """把上游 backend 的非 2xx 映射成 framework-neutral error values:

    - 上游 4xx (如 SAM3 不支持 point 的 400) → 原样透传 4xx, 不再被放大成 500.
    - 上游 5xx → 502 Bad Gateway, 表明是 backend 故障而非平台故障.

    交互式探针 (warmup) 拿到透传的 400 后, 前端全局拦截器 (只对 403/>=500 弹 toast)
    不再刷屏 "服务器错误 HTTP 500".
    """
    if resp.status_code < 400:
        return
    detail = backend_detail(resp)
    headers: dict[str, str] | None = None
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        headers = {"Retry-After": retry_after}
    client_detail = detail if isinstance(detail, dict) else f"ML backend: {detail}"
    if resp.status_code < 500 or resp.status_code == 503:
        raise error_factory(
            status_code=resp.status_code, detail=client_detail, headers=headers
        )
    if isinstance(detail, dict):
        raise error_factory(status_code=502, detail=detail)
    raise error_factory(status_code=502, detail=f"ML backend error: {detail}")


async def bounded_stream_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    reason: str = "mask_response_too_large",
    error_factory: ErrorFactory = BackendTransportError,
) -> httpx.Response:
    declared = response.headers.get("content-length")
    if declared:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > max_bytes:
            raise error_factory(
                status_code=413,
                detail={
                    "reason": reason,
                    "message": "ML backend response exceeds the byte budget",
                    "max_bytes": max_bytes,
                    "declared_bytes": declared_bytes,
                },
            )
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=min(max_bytes + 1, 64 * 1024)):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise error_factory(
                status_code=413,
                detail={
                    "reason": reason,
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


def get_semaphore(backend_id: str | None, max_cc: int) -> asyncio.Semaphore | None:
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
    # token / cost 透传 (LLM-backed backend 才有, grounded-sam2 当前留 None).
    # worker 累加到 async_job.result.total_cost, prediction_meta 单条留档.
    meta: dict | None = None
    # 交互单实例精修的 low-res logits 回灌 (base64); 平台仅透传, 前端原样回带.
    mask_input_next: str | None = None
    diagnostic: dict | None = None


class BackendTransport:
    def __init__(
        self,
        backend: BackendConfiguration,
        *,
        options: BackendOptions,
        dispatch_context: DispatchContext,
        observe: Observer,
        error_factory: ErrorFactory = BackendTransportError,
        request_context: RequestContext | None = None,
    ) -> None:
        self._backend = backend
        self.base_url = backend.url.rstrip("/")
        self.auth_method = backend.auth_method
        self.auth_token = backend.auth_token
        raw_backend_id = getattr(backend, "id", None)
        self.backend_id = str(raw_backend_id) if raw_backend_id else None
        self.gpu_resource_id = getattr(backend, "gpu_resource_id", None)
        self.vram_budget_mb = getattr(backend, "vram_budget_mb", None)
        self.options = options
        self._dispatch_context = dispatch_context
        self._observe = observe
        self.error_factory = error_factory
        self._request_context = request_context
        extra = getattr(backend, "extra_params", None) or {}
        self._extra_params = extra
        self.max_concurrency = int(
            extra.get("max_concurrency", _DEFAULT_MAX_CONCURRENCY)
        )

    def _http_client(self, *, dispatch_grant=None, **kwargs):
        return http_client(
            self._request_context,
            on_not_sent=(lambda: dispatch_grant.report_not_sent())
            if dispatch_grant is not None
            else None,
            on_send=getattr(dispatch_grant, "request_started", None),
            **kwargs,
        )

    def _predict_context(self, context: dict) -> dict:
        normalized = normalize_context_task(context)
        task = resolve_model_task(
            self._backend, normalized.get("model_id"), normalized.get("task")
        )
        if task is not None:
            normalized["task"] = task
        meta = getattr(self._backend, "health_meta", None) or {}
        revision = (meta.get("capabilities") or {}).get("task_contract_revision", 0)
        if revision != 1:
            # Compatibility is confined to the transport; stored context is canonical.
            task = normalized.pop("task", None)
            if task is not None and "type" not in normalized:
                normalized["type"] = legacy_inference_task(task)
        return normalized

    @property
    def _semaphore(self) -> asyncio.Semaphore | None:
        return get_semaphore(self.backend_id, self.max_concurrency)

    def _headers(self, grant: BackendDispatchGrant | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_method == "token" and self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if grant is not None:
            headers[GPU_GENERATION_HEADER] = grant.generation
            headers[GPU_ADMISSION_TOKEN_HEADER] = grant.admission_token
        return headers

    def _raise_for_backend_status(self, response: httpx.Response) -> None:
        raise_for_backend_status(response, error_factory=self.error_factory)

    async def _bounded_stream_response(
        self,
        response: httpx.Response,
        *,
        max_bytes: int,
        reason: str = "mask_response_too_large",
    ) -> httpx.Response:
        return await bounded_stream_response(
            response,
            max_bytes=max_bytes,
            reason=reason,
            error_factory=self.error_factory,
        )

    async def health(self) -> bool:
        try:
            async with self._http_client(
                timeout=self.options.ml_health_timeout
            ) as client:
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
        """拉 /health 完整响应; 上层 service 把 gpu_info/cache/model_version 缓存到 ml_backends.health_meta.

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
            async with self._http_client(
                timeout=self.options.ml_health_timeout
            ) as client:
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
                # 加 host (PerfHud 容器 CPU/RAM); gpu_info/cache/model_version 保留
                # 加 pool (cap / loaded_variants / per_variant_lru_ts), # 升级到协议 §4.3 PoolStatus (cap / current_size / loaded_keys / last_evict),
                # 这里整段透传; 模型市场变体面板按字段优先级展示 (backend 无 pool 时静默跳过).
                # 加 video_pool (cap / loaded_variants / active_sessions / idle_seconds),
                # 供视频追踪显存池观测 (backend 无该字段时静默跳过; video pool 协议化留下版).
                # WS4 · 加 compute (configured_device/effective_device/effective_provider),
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
        """per-backend Semaphore 限速 context manager. 无 backend_id 时降级为 noop."""

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
        async with self._http_client(timeout=180) as client:
            response = await client.post(
                f"{self.base_url}/managed-models", json=payload, headers=self._headers()
            )
            self._raise_for_backend_status(response)
            return response.json()

    async def trained_model_receipt(self, version_id: str) -> dict:
        async with self._http_client(timeout=self.options.ml_health_timeout) as client:
            response = await client.get(
                f"{self.base_url}/managed-models/{version_id}", headers=self._headers()
            )
            self._raise_for_backend_status(response)
            return response.json()

    async def managed_evaluation_request(
        self, path: str, *, payload: dict | None = None, params: dict | None = None
    ) -> dict:
        """Read/seal a request without starting native work or GPU admission."""
        async with self._http_client(timeout=self.options.ml_health_timeout) as client:
            async with client.stream(
                "POST" if payload is not None else "GET",
                f"{self.base_url}/managed-evaluation/{path}",
                json=payload,
                params=params,
                headers=self._headers(),
            ) as streamed:
                response = await self._bounded_stream_response(
                    streamed,
                    max_bytes=256 * 1024,
                    reason="evaluation_response_too_large",
                )
            self._raise_for_backend_status(response)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Evaluation backend response must be an object")
            return data

    async def predict(
        self,
        tasks: list[dict],
        context: dict | None = None,
        *,
        max_response_bytes: int | None = None,
    ) -> list[PredictionResult]:
        start = time.monotonic()
        outcome = "success"
        payload: dict = {"tasks": tasks}
        if context:
            # 批量预标透传 context（含 type=text + prompt + output 三模式 + DINO 阈值）。
            payload["context"] = self._predict_context(context)
        try:
            async with await self._acquire():
                async with self._dispatch_context(
                    "predict", AdmissionScope.PREDICT
                ) as grant:
                    async with self._http_client(
                        timeout=self.options.ml_predict_timeout, dispatch_grant=grant
                    ) as client:
                        if max_response_bytes is None:
                            resp = await client.post(
                                f"{self.base_url}/predict",
                                json=payload,
                                headers=self._headers(grant),
                            )
                        else:
                            async with client.stream(
                                "POST",
                                f"{self.base_url}/predict",
                                json=payload,
                                headers=self._headers(grant),
                            ) as streamed:
                                resp = await self._bounded_stream_response(
                                    streamed,
                                    max_bytes=max_response_bytes,
                                    reason="evaluation_response_too_large",
                                )
                        if grant is not None:
                            grant.report_response(resp.status_code)
                        if max_response_bytes is None:
                            resp.raise_for_status()
                        else:
                            self._raise_for_backend_status(resp)
                        data = resp.json()
                        if max_response_bytes is not None and (
                            not isinstance(data, dict)
                            or not isinstance(data.get("results"), list)
                            or any(
                                not isinstance(item, dict) for item in data["results"]
                            )
                        ):
                            raise ValueError("Invalid batch prediction response")
        except Exception:
            outcome = "error"
            self._observe(self.backend_id, outcome, time.monotonic() - start)
            raise

        wall_ms = int((time.monotonic() - start) * 1000)
        self._observe(self.backend_id, outcome, wall_ms / 1000)

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
                    meta=item.get("meta"),  # LLM cost/token (grounded-sam2 不返回)
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
        try:
            context = self._predict_context(context)
        except ValueError as exc:
            raise self.error_factory(status_code=422, detail=str(exc)) from exc
        start = time.monotonic()
        outcome = "success"
        try:
            async with await self._acquire():
                try:
                    async with self._dispatch_context(
                        "predict_interactive", AdmissionScope.PREDICT
                    ) as grant:
                        async with self._http_client(
                            timeout=self.options.ml_predict_timeout,
                            dispatch_grant=grant,
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
                                    resp = await self._bounded_stream_response(
                                        streamed,
                                        max_bytes=max_response_bytes,
                                    )
                            if grant is not None:
                                grant.report_response(resp.status_code)
                            # 上游 4xx 原样透传, 5xx → 502 (见 _raise_for_backend_status)
                            self._raise_for_backend_status(resp)
                            try:
                                data = json.loads(resp.content)
                            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                                raise self.error_factory(
                                    status_code=502,
                                    detail={
                                        "reason": "invalid_backend_response",
                                        "message": "ML backend returned invalid JSON",
                                    },
                                ) from exc
                            if not isinstance(data, dict):
                                raise self.error_factory(
                                    status_code=502,
                                    detail={
                                        "reason": "invalid_backend_response",
                                        "message": "ML backend response must be an object",
                                    },
                                )
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    # 转换发生在 dispatch context 退出后，使 authority 能看到
                    # 原始 timeout/connect 不确定结果。
                    raise self.error_factory(
                        status_code=502,
                        detail=f"ML backend unreachable: {exc}",
                    ) from exc
        except Exception:
            # 仍记 error 指标, 再原样抛出, 指标语义不变.
            outcome = "error"
            self._observe(self.backend_id, outcome, time.monotonic() - start)
            raise

        wall_ms = int((time.monotonic() - start) * 1000)
        self._observe(self.backend_id, outcome, wall_ms / 1000)

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
        async with self._dispatch_context("unload", AdmissionScope.UNLOAD) as grant:
            async with self._http_client(
                timeout=self.options.ml_health_timeout, dispatch_grant=grant
            ) as client:
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
        modality: str | None = None,
    ) -> dict:
        """B-28+ · 让 backend 重新加载模型. 重载耗时可能远高于 health 探活, 用 predict 超时配额.

        可选指定变体预热 (模型市场单变体预热); 缺省时 body 留空, backend 用默认变体.
        可选 task_type="video" 预热独立 video tracker 池 (仅认 sam_variant, 无 dino).
        """
        body: dict[str, str] = {}
        if sam_variant:
            body["sam_variant"] = sam_variant
        if dino_variant:
            body["dino_variant"] = dino_variant
        semantic = normalize_modality_fields(
            {"modality": modality, "task_type": task_type}
        )
        if semantic.get("modality"):
            meta = getattr(self._backend, "health_meta", None) or {}
            key = (
                "modality"
                if (meta.get("capabilities") or {}).get("task_contract_revision") == 1
                else "task_type"
            )
            body[key] = semantic["modality"]
        async with self._dispatch_context("reload", AdmissionScope.RELOAD) as grant:
            async with self._http_client(
                timeout=self.options.ml_predict_timeout, dispatch_grant=grant
            ) as client:
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
        async with self._http_client(timeout=self.options.ml_health_timeout) as client:
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
        async with self._http_client(timeout=self.options.ml_health_timeout) as client:
            resp = await client.post(
                f"{self.base_url}/lifecycle/mode",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            if resp.status_code < 200 or resp.status_code >= 300:
                if resp.status_code >= 400:
                    self._raise_for_backend_status(resp)
                raise self.error_factory(
                    status_code=502,
                    detail="ML backend control endpoint returned a non-success status",
                )
            self._raise_for_backend_status(resp)
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
        async with self._http_client(timeout=self.options.ml_health_timeout) as client:
            resp = await client.post(
                f"{self.base_url}/lifecycle/reset",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            if resp.status_code < 200 or resp.status_code >= 300:
                if resp.status_code >= 400:
                    self._raise_for_backend_status(resp)
                raise self.error_factory(
                    status_code=502,
                    detail=(
                        "ML backend control endpoint returned a non-success status"
                    ),
                )
            self._raise_for_backend_status(resp)
            return parse_lifecycle_reset_response_json(resp.content)

    async def _managed_generation_transition(
        self,
        endpoint: str,
        grant: BackendDispatchGrant,
    ) -> httpx.Response:
        if endpoint not in {"/drain", "/drain/cancel", "/unload"}:
            raise ValueError("managed GPU transition endpoint is invalid")
        request = GenerationTransitionRequest(generation=grant.generation)
        try:
            async with self._http_client(
                timeout=self.options.ml_health_timeout, dispatch_grant=grant
            ) as client:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    json=request.model_dump(mode="json"),
                    headers=self._headers(grant),
                    extensions={GPU_UNLOAD_ROUTE_EXTENSION: True}
                    if endpoint == "/unload"
                    else None,
                )
                grant.report_response(response.status_code)
        except BaseException:
            grant.report_uncertain_if_missing("request_aborted")
            raise
        if response.status_code < 200 or response.status_code >= 300:
            if response.status_code >= 400:
                self._raise_for_backend_status(response)
            raise self.error_factory(
                status_code=502,
                detail="ML backend transition endpoint returned a non-success status",
            )
        return response

    async def lifecycle_drain(
        self,
        grant: BackendDispatchGrant,
    ) -> DrainTransitionResponse:
        """Send one signed generation-fenced drain outside workload dispatch."""

        response = await self._managed_generation_transition("/drain", grant)
        parsed = parse_drain_transition_response_json(response.content)
        if parsed.generation != grant.generation:
            raise ValueError("drain response generation does not match the request")
        return parsed

    async def lifecycle_cancel_drain(
        self,
        grant: BackendDispatchGrant,
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
        grant: BackendDispatchGrant,
    ) -> ManagedUnloadResponse:
        """Send one signed full-pool unload outside workload dispatch."""

        response = await self._managed_generation_transition("/unload", grant)
        parsed = parse_managed_unload_response_json(response.content)
        if parsed.generation != grant.generation:
            raise ValueError("unload response generation does not match the request")
        return parsed

    async def warmup(self, body: dict) -> dict:
        """协议 §4.4 · 把指定 variant 权重加载到 pool, 不跑 forward.

        body 保留各 backend 的结构 (yolo 要 task+variants{series,size},
        gsam2 要 variants{sam_variant,dino_variant}, sam3 可空或 variants{model_variant}).
        响应统一为 {ok, model_load_ms, cache_hit, evicted}. 用 predict 超时配额 (加载
        可能数秒).
        """
        payload = dict(body or {})
        task = payload.get("task")
        if isinstance(task, str) and (task in TASK_VALUES or task in TASK_ALIASES):
            task = normalize_task(task)
            meta = getattr(self._backend, "health_meta", None) or {}
            revision = (meta.get("capabilities") or {}).get("task_contract_revision", 0)
            payload["task"] = task if revision == 1 else legacy_inference_task(task)
        async with self._dispatch_context("warmup", AdmissionScope.WARMUP) as grant:
            async with self._http_client(
                timeout=self.options.ml_predict_timeout, dispatch_grant=grant
            ) as client:
                resp = await client.post(
                    f"{self.base_url}/warmup",
                    json=payload,
                    headers=self._headers(grant),
                )
                if grant is not None:
                    grant.report_response(resp.status_code)
                resp.raise_for_status()
                return resp.json()

    async def get_versions(self) -> list[str]:
        async with self._http_client(timeout=self.options.ml_health_timeout) as client:
            resp = await client.get(
                f"{self.base_url}/versions", headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("versions", [])
