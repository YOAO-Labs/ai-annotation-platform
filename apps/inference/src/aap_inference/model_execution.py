"""One model-operation owner over the existing backend HTTP transport."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
import httpx

from aap_inference.backend_endpoint import (
    BackendEndpointSnapshot,
    BackendEndpointSource,
)
from aap_inference_contract.backend_endpoint import BackendEndpointSubject
from aap_inference_contract.execution import (
    ExecutionFailure,
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionRouteBinding,
    ModelExecutionError,
    ModelExecutionObservation,
    ModelExecutionPort,
    ModelOperationResult,
    ModelTaskResult,
    tracker_response_digest,
    thaw_json,
)

from .backend_transport import (
    BackendOptions,
    BackendTransport,
    BackendTransportError,
    DispatchContext,
    ErrorFactory,
)
from .request_tracking import RequestContext


class ModelExecutionUnavailable(RuntimeError):
    """Required execution owner composition is incomplete or unavailable."""


class ModelExecutionRequestContextFactory(Protocol):
    def __call__(
        self,
        request: FrozenModelOperation,
        endpoint: BackendEndpointSnapshot,
        route_binding: ModelExecutionRouteBinding,
    ) -> RequestContext: ...


class ModelExecutionDispatchContextFactory(Protocol):
    def __call__(
        self,
        request: FrozenModelOperation,
        endpoint: BackendEndpointSnapshot,
    ) -> DispatchContext: ...


class _ObservationCapture:
    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: ModelExecutionObservation | None = None

    def __call__(self, backend_id: str | None, outcome: str, elapsed: float) -> None:
        if backend_id is None or outcome not in {"success", "error"}:
            return
        if self._value is not None:
            raise RuntimeError("BackendTransport emitted multiple observations")
        self._value = ModelExecutionObservation(
            backend_id=str(backend_id),
            operation="predict",
            outcome=outcome,
            elapsed_seconds=elapsed,
        )

    def operation(self, operation: str) -> ModelExecutionObservation | None:
        if self._value is None:
            return None
        return ModelExecutionObservation(
            backend_id=self._value.backend_id,
            operation=operation,
            outcome=self._value.outcome,
            elapsed_seconds=self._value.elapsed_seconds,
        )


@dataclass(frozen=True, slots=True)
class _TransportBackend:
    id: uuid.UUID
    url: str
    auth_method: str
    auth_token: str | None = field(repr=False)
    gpu_resource_id: uuid.UUID | str | None
    vram_budget_mb: int | None
    extra_params: Mapping[str, Any] | None
    health_meta: Mapping[str, Any] | None


class BackendTransportModelExecutionProvider(ModelExecutionPort):
    """Credential-owning independent provider that executes exactly one request.

    The endpoint subject opens only inside this provider. Routing begin/settle and GPU
    admission are supplied by the concrete runtime composition; they are mandatory and
    used by the existing BackendTransport for each actual HTTP request.
    """

    def __init__(
        self,
        endpoint_source: BackendEndpointSource,
        *,
        options: BackendOptions,
        request_context_factory: ModelExecutionRequestContextFactory,
        dispatch_context_factory: ModelExecutionDispatchContextFactory,
        error_factory: ErrorFactory = BackendTransportError,
    ) -> None:
        if (
            endpoint_source is None
            or not callable(request_context_factory)
            or not callable(dispatch_context_factory)
        ):
            raise ModelExecutionUnavailable()
        self._endpoint_source = endpoint_source
        self._options = options
        self._request_context_factory = request_context_factory
        self._dispatch_context_factory = dispatch_context_factory
        self._error_factory = error_factory

    async def readiness(self) -> bool:
        return await self._endpoint_source.readiness()

    async def execute(self, request: FrozenModelOperation) -> ModelOperationResult:
        if (
            not isinstance(request, FrozenModelOperation)
            or request.endpoint_subject is None
            or request.endpoint_registry_id is None
            or request.route_binding is None
        ):
            raise ModelExecutionError(
                ExecutionFailure(
                    request_id=request.request_id,
                    kind="invalid_request",
                    code="model_execution_authority_required",
                    status_code=400,
                    detail={"reason": "endpoint_and_route_authority_required"},
                )
            )
        subject: BackendEndpointSubject = request.endpoint_subject
        endpoint: BackendEndpointSnapshot | None = None
        capture = _ObservationCapture()
        started = time.perf_counter()
        operation = (
            "predict" if request.operation == "batch_predict" else "predict_interactive"
        )
        try:
            endpoint = await self._endpoint_source.open(
                subject,
                request_id=request.request_id,
                purpose="execution",
                authority=request.route_binding.endpoint_authority(),
            )
            if endpoint.registry_id != request.endpoint_registry_id:
                raise ModelExecutionUnavailable()
            if (
                request.required_instance_id is not None
                and endpoint.registry_id != request.required_instance_id
            ):
                raise ModelExecutionUnavailable()
            transport_backend = _TransportBackend(
                id=endpoint.registry_id,
                url=endpoint.url,
                auth_method=endpoint.auth_method,
                auth_token=endpoint.auth_token,
                gpu_resource_id=endpoint.gpu_resource_id,
                vram_budget_mb=endpoint.vram_budget_mb,
                extra_params=endpoint.extra_params,
                health_meta=request.health_meta,
            )
            request_context = self._request_context_factory(
                request, endpoint, request.route_binding
            )
            dispatch_context = self._dispatch_context_factory(request, endpoint)
            transport = BackendTransport(
                transport_backend,
                options=self._options,
                dispatch_context=dispatch_context,
                observe=capture,
                error_factory=self._error_factory,
                request_context=request_context,
            )
            if request.operation == "batch_predict":
                raw_results = await transport.predict(
                    [thaw_json(item) for item in request.task_inputs],
                    context=None
                    if request.context is None
                    else thaw_json(request.context),
                    max_response_bytes=request.max_response_bytes,
                )
                session_tokens = (None,) * len(raw_results)
            else:
                raw = await transport.predict_interactive(
                    task_data=thaw_json(request.task_inputs[0]),
                    context={}
                    if request.context is None
                    else thaw_json(request.context),
                    max_response_bytes=request.max_response_bytes,
                )
                raw_results = [raw]
                meta = raw.meta if isinstance(raw.meta, Mapping) else {}
                tracker_context = meta.get("tracker_context") or {}
                token = (
                    tracker_context.get("token")
                    if request.operation == "pinned_tracker"
                    and isinstance(tracker_context, Mapping)
                    else None
                )
                session_tokens = (token if isinstance(token, str) else None,)
            expected_task_ids = {item["id"] for item in request.task_inputs}
            seen_task_ids: set[str] = set()
            results_list: list[ModelTaskResult] = []
            for index, raw in enumerate(raw_results):
                task_id = raw.task_id or (
                    request.task_inputs[0]["id"]
                    if len(request.task_inputs) == 1
                    else None
                )
                if (
                    not isinstance(task_id, str)
                    or task_id not in expected_task_ids
                    or task_id in seen_task_ids
                    or not isinstance(raw.result, (list, tuple))
                ):
                    raise self._error_factory(
                        status_code=502,
                        detail={
                            "reason": "invalid_backend_response",
                            "message": "ML backend returned results without valid task identities",
                        },
                    )
                seen_task_ids.add(task_id)
                result_meta = raw.meta
                if isinstance(result_meta, Mapping):
                    tracker_meta = result_meta.get("tracker_context")
                    if isinstance(tracker_meta, Mapping) and "token" in tracker_meta:
                        result_meta = dict(result_meta)
                        safe_tracker_meta = dict(tracker_meta)
                        safe_tracker_meta.pop("token")
                        result_meta["tracker_context"] = safe_tracker_meta
                try:
                    results_list.append(
                        ModelTaskResult(
                            task_id=task_id,
                            result=raw.result,
                            score=raw.score,
                            model_version=raw.model_version,
                            inference_time_ms=raw.inference_time_ms,
                            cache_hit=raw.cache_hit,
                            model_load_ms=raw.model_load_ms,
                            meta=result_meta,
                            mask_input_next=raw.mask_input_next,
                            diagnostic=raw.diagnostic,
                            session_token=session_tokens[index],
                        )
                    )
                except (TypeError, ValueError):
                    raise self._error_factory(
                        status_code=502,
                        detail={
                            "reason": "invalid_backend_response",
                            "message": "ML backend returned an invalid model result",
                        },
                    ) from None
            results = tuple(results_list)
            observation = capture.operation(operation)
            if observation is None:
                raise ModelExecutionUnavailable()
            provenance = self._provenance(request, endpoint)
            digest = (
                tracker_response_digest(results[0], raw_meta=raw_results[0].meta)
                if request.operation == "pinned_tracker"
                else None
            )
            return ModelOperationResult(
                request_id=request.request_id,
                operation=request.operation,
                results=results,
                provenance=provenance,
                observation=observation,
                wall_time_ms=int((time.perf_counter() - started) * 1000),
                response_digest=digest,
            )
        except ModelExecutionError:
            raise
        except Exception as exc:
            provenance = self._provenance(request, endpoint)
            failure = self._failure(
                request,
                exc,
                provenance=provenance,
                observation=capture.operation(operation),
            )
            raise ModelExecutionError(failure) from exc

    @staticmethod
    def _provenance(
        request: FrozenModelOperation,
        endpoint: BackendEndpointSnapshot | None,
    ) -> ExecutionProvenance:
        route = request.route_binding
        if route is None or request.endpoint_registry_id is None:
            raise ModelExecutionUnavailable()
        endpoint_id = (
            endpoint.registry_id
            if endpoint is not None
            else request.endpoint_registry_id
        )
        return ExecutionProvenance(
            requested_registry_id=request.requested_registry_id,
            requested_pool_id=request.requested_pool_id,
            registry_id=endpoint_id,
            pool_id=request.selected_pool_id,
            instance_id=endpoint_id,
            route_scope_id=route.scope_id,
            attempt_id=route.attempt_id,
            topology_generation=request.topology_generation,
            member_id=request.selected_member_id,
            endpoint_sha256=(
                endpoint.endpoint_sha256 if endpoint is not None else None
            ),
        )

    @staticmethod
    def _failure(
        request: FrozenModelOperation,
        exc: Exception,
        *,
        provenance: ExecutionProvenance | None,
        observation: ModelExecutionObservation | None,
    ) -> ExecutionFailure:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if type(status_code) is not int or not 400 <= status_code <= 599:
            status_code = None
        transport_error = (
            exc
            if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
            else exc.__cause__
        )
        error_code = getattr(exc, "error_code", None)
        if not isinstance(error_code, str):
            error_code = getattr(error_code, "value", None)
        uncertain = False
        if isinstance(exc, ModelExecutionUnavailable):
            kind = "unavailable"
            code = "model_execution_unavailable"
            status_code = 503
            detail = "Model execution is unavailable"
        elif isinstance(transport_error, httpx.TimeoutException):
            kind = "transport"
            code = "backend_timeout"
            status_code = status_code or 504
            detail = getattr(exc, "detail", None) or str(transport_error)
            uncertain = True
        elif isinstance(transport_error, httpx.ConnectError):
            kind = "transport"
            code = "backend_unreachable"
            status_code = status_code or 502
            detail = (
                getattr(exc, "detail", None)
                or f"ML backend unreachable: {transport_error}"
            )
        elif isinstance(error_code, str) and error_code:
            kind = "admission"
            code = error_code
            detail = getattr(exc, "detail", None)
        elif status_code is not None:
            kind = "backend"
            code = "backend_execution_failed"
            detail = getattr(exc, "detail", None)
            if detail is None and response is not None:
                try:
                    detail = response.json()
                except Exception:
                    detail = (getattr(response, "text", "") or "")[:512]
            if detail is None:
                detail = str(exc)
        elif isinstance(exc, (ValueError, TypeError)):
            kind = "invalid_request"
            code = "invalid_model_operation"
            detail = str(exc)
        else:
            kind = "uncertain"
            code = "model_execution_uncertain"
            detail = "Model operation did not return a trustworthy response"
            uncertain = True
        headers = getattr(exc, "headers", None)
        if headers is None and response is not None:
            headers = getattr(response, "headers", None)
        retry_after = None
        raw_retry_after = getattr(exc, "retry_after_s", None)
        if type(raw_retry_after) is int and raw_retry_after >= 0:
            retry_after = str(raw_retry_after)
        elif isinstance(headers, Mapping):
            raw_retry_after = headers.get("Retry-After") or headers.get("retry-after")
            if raw_retry_after is not None:
                retry_after = str(raw_retry_after)
        return ExecutionFailure(
            request_id=request.request_id,
            kind=kind,
            code=code,
            status_code=status_code,
            detail=detail,
            retry_after=retry_after,
            uncertain=uncertain,
            provenance=provenance,
            observation=observation,
        )
