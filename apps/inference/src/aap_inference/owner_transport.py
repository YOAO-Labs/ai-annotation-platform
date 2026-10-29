"""Credential-owning transport composition over durable routing and GPU ports."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import re
import uuid
from urllib.parse import quote
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any, Literal


import httpx
from aap_inference_contract.backend_control import BackendControlRequest
from aap_inference_contract.backend_endpoint import (
    BackendEndpointAuthority,
    BackendEndpointBinding,
)
from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionRouteBinding,
)
from aap_inference_contract.routing import (
    RoutingAttemptView,
    RoutingBeginControlResult,
    RoutingBeginResult,
    RoutingRejectedResult,
    RoutingResult,
    RoutingScopeView,
    RoutingUnavailable,
    make_routing_command,
)
from aap_protocol_v2.lifecycle import (
    DrainTransitionResponse,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetRequest,
    LifecycleResetResponse,
    ManagedUnloadResponse,
)

from .backend_endpoint import BackendEndpointSnapshot
from .backend_transport import (
    BackendTransport,
    BackendTransportError,
    ErrorFactory,
    Observer,
)
from .gpu.contracts import GPUDispatchGrantPort
from .gpu.transport import GPUControlOperation
from .request_tracking import (
    GPU_UNLOAD_ROUTE_EXTENSION,
    RequestContext,
)
from .routing_runtime import RoutingRuntime

_logger = logging.getLogger(__name__)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SHORT_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_GPU_CONTROL_PATHS = {
    "mode": ("POST", "/lifecycle/mode"),
    "reset": ("POST", "/lifecycle/reset"),
    "drain": ("POST", "/drain"),
    "cancel_drain": ("POST", "/drain/cancel"),
    "unload": ("POST", "/unload"),
    "terminal_drain": ("POST", "/lifecycle/terminal-drain-signed"),
}
_BACKEND_CONTROL_PURPOSES = {
    "setup": "setup",
    "health_meta": "health_observation",
    "unload": "lifecycle",
    "terminal_drain": "lifecycle",
    "reload": "lifecycle",
    "warmup": "execution",
    "catalog_fallback": "catalog_fallback",
    "stats": "stats",
    "install_trained_model": "execution",
    "trained_model_receipt": "backend_read",
    "managed_evaluation_profile": "backend_read",
    "managed_evaluation_receipt": "backend_read",
    "managed_evaluation_seal": "execution",
    "versions": "backend_read",
}
_BACKEND_CONTROL_READS = frozenset(
    {
        "setup",
        "health_meta",
        "catalog_fallback",
        "stats",
        "trained_model_receipt",
        "managed_evaluation_profile",
        "managed_evaluation_receipt",
        "versions",
    }
)
_BACKEND_CONTROL_MUTATIONS = frozenset(
    {
        "unload",
        "terminal_drain",
        "reload",
        "warmup",
        "install_trained_model",
        "managed_evaluation_seal",
    }
)


def _json_value(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise RoutingUnavailable()
        return {key: _json_value(child) for key, child in value.items()}
    if type(value) in {list, tuple}:
        return [_json_value(child) for child in value]
    raise RoutingUnavailable()


def _transport_identity(endpoint: BackendEndpointSnapshot) -> dict[str, Any]:
    """Build the exact existing routing identity from the opened private snapshot."""
    auth_method = endpoint.auth_method
    token = endpoint.auth_token
    resource_id = endpoint.gpu_resource_id
    extra_params = endpoint.extra_params
    if (
        type(endpoint.url) is not str
        or type(auth_method) is not str
        or (token is not None and type(token) is not str)
        or (resource_id is not None and type(resource_id) is not str)
        or (extra_params is not None and not isinstance(extra_params, Mapping))
    ):
        raise RoutingUnavailable()
    return {
        "url": endpoint.url,
        "auth_method": auth_method,
        "auth_token": token,
        "gpu_resource_id": resource_id,
        "extra_params": None if extra_params is None else _json_value(extra_params),
    }


async def _execute_routing(
    runtime: RoutingRuntime,
    operation: str,
    body: Mapping[str, Any],
    *,
    command_id: uuid.UUID,
) -> Any:
    command = make_routing_command(operation, body, request_id=command_id)
    result = await runtime.execute(command)
    if (
        not isinstance(result, RoutingResult)
        or result.request_id != command_id
        or result.operation != operation
        or result.state != "committed"
        or isinstance(result.result, RoutingRejectedResult)
    ):
        raise RoutingUnavailable()
    return result.result


class _ReadOnlyGrant:
    __slots__ = ()
    headers = MappingProxyType({})

    async def response(self, _status_code: int) -> None:
        return None

    async def uncertain(self, _reason: str) -> None:
        return None


class _TerminalDrainGrant:
    """No routing headers or route outcome for the signed terminal operation."""

    __slots__ = ()
    headers = MappingProxyType({})

    async def response(self, _status_code: int) -> None:
        return None

    async def uncertain(self, _reason: str) -> None:
        return None


class _RoutingRequestGrant:
    __slots__ = ("_runtime", "_scope_id", "_capability", "_attempt_id", "headers")

    def __init__(
        self,
        runtime: RoutingRuntime,
        scope_id: uuid.UUID,
        capability: str,
        attempt_id: uuid.UUID,
        headers: Mapping[str, str],
    ) -> None:
        self._runtime = runtime
        self._scope_id = scope_id
        self._capability = capability
        self._attempt_id = attempt_id
        self.headers = MappingProxyType(dict(headers))

    async def response(self, status_code: int) -> None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise RoutingUnavailable()
        await self._settle("response", status_code, None)

    async def uncertain(self, reason: str) -> None:
        safe_reason = (
            reason
            if type(reason) is str and _SHORT_CODE_RE.fullmatch(reason)
            else "transport_error"
        )
        await self._settle("uncertain", None, safe_reason)

    async def _settle(
        self,
        kind: Literal["response", "uncertain"],
        status_code: int | None,
        reason: str | None,
    ) -> None:
        command_id = uuid.uuid4()
        attempt = await _execute_routing(
            self._runtime,
            "settle",
            {
                "scope_id": str(self._scope_id),
                "capability": self._capability,
                "attempt_id": str(self._attempt_id),
                "kind": kind,
                "status_code": status_code,
                "reason": reason,
            },
            command_id=command_id,
        )
        if (
            not isinstance(attempt, RoutingAttemptView)
            or attempt.attempt_id != self._attempt_id
            or attempt.state not in {"response", "uncertain"}
        ):
            raise RoutingUnavailable()


class _RoutingControlRequestGrant:
    __slots__ = ("_runtime", "_control_id", "_capability", "_attempt_id", "headers")

    def __init__(
        self,
        runtime: RoutingRuntime,
        control_id: uuid.UUID,
        capability: str,
        attempt_id: uuid.UUID,
        headers: Mapping[str, str],
    ) -> None:
        self._runtime = runtime
        self._control_id = control_id
        self._capability = capability
        self._attempt_id = attempt_id
        self.headers = MappingProxyType(dict(headers))

    async def response(self, status_code: int) -> None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise RoutingUnavailable()
        await self._settle("response", status_code, None)

    async def uncertain(self, reason: str) -> None:
        safe_reason = (
            reason
            if type(reason) is str and _SHORT_CODE_RE.fullmatch(reason)
            else "transport_error"
        )
        await self._settle("uncertain", None, safe_reason)

    async def _settle(
        self,
        kind: Literal["response", "uncertain"],
        status_code: int | None,
        reason: str | None,
    ) -> None:
        settled = await _execute_routing(
            self._runtime,
            "settle_control",
            {
                "control_id": str(self._control_id),
                "capability": self._capability,
                "attempt_id": str(self._attempt_id),
                "kind": kind,
                "status_code": status_code,
                "reason": reason,
            },
            command_id=uuid.uuid4(),
        )
        if (
            not isinstance(settled, RoutingAttemptView)
            or settled.attempt_id != self._attempt_id
            or settled.state not in {"response", "uncertain"}
        ):
            raise RoutingUnavailable()


def _route_request_context(
    runtime: RoutingRuntime,
    endpoint: BackendEndpointSnapshot,
    binding: BackendEndpointBinding,
    *,
    request_id: uuid.UUID,
    operation: GPUControlOperation,
) -> RequestContext:
    if (
        binding.kind != "scope"
        or binding.scope_id is None
        or binding.control_id is not None
        or binding.attempt_id != request_id
        or endpoint.request_id != request_id
        or endpoint.purpose != "gpu_control"
        or endpoint.authority is None
        or not _authority_matches(endpoint.authority, binding)
    ):
        raise RoutingUnavailable()

    @asynccontextmanager
    async def request_context(request):
        if request.method in _READ_METHODS:
            yield _ReadOnlyGrant()
            return
        expected_method, expected_path = _GPU_CONTROL_PATHS[operation]
        expected_raw_path = httpx.URL(
            f"{endpoint.url.rstrip('/')}{expected_path}"
        ).raw_path
        is_gpu_unload = request.extensions.get(GPU_UNLOAD_ROUTE_EXTENSION) is True
        if (
            request.method != expected_method
            or request.url.raw_path != expected_raw_path
            or is_gpu_unload != (operation == "unload")
        ):
            raise RoutingUnavailable()
        issuer_attempt_id = request_id
        async with runtime.transport_subject_issuer(
            parent_kind="scope",
            parent_id=binding.scope_id,
            capability=binding.capability,
            attempt_id=issuer_attempt_id,
            transport_identity=_transport_identity(endpoint),
        ) as subject:
            begun = await _execute_routing(
                runtime,
                "begin",
                {
                    "scope_id": str(binding.scope_id),
                    "capability": binding.capability,
                    "attempt_id": str(issuer_attempt_id),
                    "method": request.method,
                    "path": request.url.path,
                    "transport_subject": subject,
                },
                command_id=issuer_attempt_id,
            )
        if (
            not isinstance(begun, RoutingBeginResult)
            or begun.attempt_id != issuer_attempt_id
        ):
            raise RoutingUnavailable()
        yield _RoutingRequestGrant(
            runtime,
            binding.scope_id,
            binding.capability,
            issuer_attempt_id,
            begun.headers,
        )

    return request_context


def _authority_matches(
    authority: BackendEndpointAuthority, binding: BackendEndpointBinding
) -> bool:
    return (
        authority.kind == binding.kind
        and authority.scope_id == binding.scope_id
        and authority.control_id == binding.control_id
        and authority.attempt_id == binding.attempt_id
    )


class RoutingModelRequestContextFactory:
    """Compose model HTTP sends with fresh issuer and existing durable route calls."""

    def __init__(self, routing_runtime: RoutingRuntime) -> None:
        if routing_runtime is None:
            raise ValueError("routing_runtime is required")
        self._routing_runtime = routing_runtime

    def __call__(
        self,
        request: FrozenModelOperation,
        endpoint: BackendEndpointSnapshot,
        route_binding: ModelExecutionRouteBinding,
    ) -> RequestContext:
        if (
            not isinstance(request, FrozenModelOperation)
            or not isinstance(endpoint, BackendEndpointSnapshot)
            or not isinstance(route_binding, ModelExecutionRouteBinding)
            or request.route_binding != route_binding
            or request.request_id != route_binding.attempt_id
            or request.endpoint_registry_id != endpoint.registry_id
            or request.required_instance_id not in {None, endpoint.registry_id}
            or endpoint.request_id != request.request_id
            or endpoint.purpose != "execution"
            or endpoint.authority != route_binding.endpoint_authority()
        ):
            raise RoutingUnavailable()
        identity = _transport_identity(endpoint)

        @asynccontextmanager
        async def request_context(http_request):
            if http_request.method in _READ_METHODS:
                yield _ReadOnlyGrant()
                return
            attempt_id = request.request_id
            async with self._routing_runtime.transport_subject_issuer(
                parent_kind="scope",
                parent_id=route_binding.scope_id,
                capability=route_binding.capability,
                attempt_id=attempt_id,
                transport_identity=identity,
            ) as subject:
                begun = await _execute_routing(
                    self._routing_runtime,
                    "begin",
                    {
                        "scope_id": str(route_binding.scope_id),
                        "capability": route_binding.capability,
                        "attempt_id": str(attempt_id),
                        "method": http_request.method,
                        "path": http_request.url.path,
                        "transport_subject": subject,
                    },
                    command_id=attempt_id,
                )
            if (
                not isinstance(begun, RoutingBeginResult)
                or begun.attempt_id != attempt_id
            ):
                raise RoutingUnavailable()
            yield _RoutingRequestGrant(
                self._routing_runtime,
                route_binding.scope_id,
                route_binding.capability,
                attempt_id,
                begun.headers,
            )

        return request_context


def _backend_control_target(
    request: BackendControlRequest,
) -> tuple[str, str, dict[str, str] | None]:
    operation = request.operation
    body = request.body or {}
    if operation == "setup":
        return "GET", "/setup", None
    if operation == "health_meta":
        return "GET", "/health", None
    if operation == "catalog_fallback":
        return "GET", "/setup", None
    if operation == "stats":
        return "GET", "/health", None
    if operation == "trained_model_receipt":
        version_id = body.get("version_id")
        if type(version_id) is not str:
            raise RoutingUnavailable()
        return "GET", f"/managed-models/{quote(version_id, safe='')}", None
    if operation == "managed_evaluation_profile":
        return "GET", "/managed-evaluation/profile", None
    if operation == "managed_evaluation_receipt":
        try:
            query = {
                "request_id": body["request_id"],
                "binding_digest": body["binding_digest"],
                "storage_identity": body["storage_identity"],
            }
        except KeyError:
            raise RoutingUnavailable() from None
        if any(type(value) is not str for value in query.values()):
            raise RoutingUnavailable()
        return "GET", "/managed-evaluation/receipt", query
    if operation == "versions":
        return "GET", "/versions", None
    if operation == "unload":
        return "POST", "/unload", None
    if operation == "terminal_drain":
        return "POST", "/lifecycle/terminal-drain", None
    if operation == "reload":
        return "POST", "/reload", None
    if operation == "warmup":
        return "POST", "/warmup", None
    if operation == "install_trained_model":
        return "POST", "/managed-models", None
    if operation == "managed_evaluation_seal":
        return "POST", "/managed-evaluation/seal", None
    raise RoutingUnavailable()


def _matches_backend_control_target(
    http_request,
    target: tuple[str, str, Mapping[str, str] | None],
    *,
    base_url: str,
) -> bool:
    method, path, expected_query = target
    expected_methods = _READ_METHODS if method == "GET" else frozenset({method})
    if http_request.method not in expected_methods:
        return False
    try:
        raw_path = http_request.url.raw_path.partition(b"?")[0]
        expected_path = httpx.URL(f"{base_url.rstrip('/')}{path}").raw_path
        if raw_path != expected_path:
            return False
        actual_query = list(http_request.url.params.multi_items())
    except (AttributeError, UnicodeError, ValueError):
        return False
    if expected_query is None:
        return not actual_query
    return (
        len(actual_query) == len(expected_query)
        and dict(actual_query) == expected_query
    )


class RoutingBackendControlRequestContextFactory:
    """Compose registered control HTTP sends with fresh route subjects and outcomes."""

    def __init__(self, routing_runtime: RoutingRuntime) -> None:
        if routing_runtime is None:
            raise ValueError("routing_runtime is required")
        self._routing_runtime = routing_runtime

    def __call__(
        self,
        request: BackendControlRequest,
        opened_endpoint: BackendEndpointSnapshot,
        endpoint_binding: BackendEndpointBinding | None,
    ) -> RequestContext:
        if (
            not isinstance(request, BackendControlRequest)
            or not isinstance(opened_endpoint, BackendEndpointSnapshot)
            or request.kind != "registered"
            or request.operation not in _BACKEND_CONTROL_PURPOSES
            or request.registry_id != opened_endpoint.registry_id
            or request.request_id != opened_endpoint.request_id
            or request.endpoint_subject.registry_id != request.registry_id
            or request.endpoint_subject.request_id != request.request_id
            or request.endpoint_subject.purpose
            != _BACKEND_CONTROL_PURPOSES[request.operation]
            or opened_endpoint.purpose != _BACKEND_CONTROL_PURPOSES[request.operation]
            or request.endpoint_subject.authority != opened_endpoint.authority
        ):
            raise RoutingUnavailable()

        binding = getattr(request, "binding", None)
        if request.operation in _BACKEND_CONTROL_READS:
            if (
                binding is not None
                or endpoint_binding is not None
                or request.endpoint_subject.authority is not None
                or opened_endpoint.authority is not None
                or (
                    request.operation == "catalog_fallback"
                    and opened_endpoint.auth_method != "none"
                )
            ):
                raise RoutingUnavailable()
        elif request.operation in _BACKEND_CONTROL_MUTATIONS:
            if (
                not isinstance(binding, BackendEndpointBinding)
                or not isinstance(endpoint_binding, BackendEndpointBinding)
                or binding != endpoint_binding
                or binding.attempt_id != request.request_id
                or request.endpoint_subject.authority is None
                or not _authority_matches(request.endpoint_subject.authority, binding)
                or opened_endpoint.authority is None
                or not _authority_matches(opened_endpoint.authority, binding)
                or (
                    request.operation in {"unload", "reload", "terminal_drain"}
                    and binding.kind != "control"
                )
                or (
                    request.operation
                    in {"warmup", "install_trained_model", "managed_evaluation_seal"}
                    and binding.kind != "scope"
                )
            ):
                raise RoutingUnavailable()
        else:
            raise RoutingUnavailable()

        target = _backend_control_target(request)
        identity = (
            None
            if request.operation in _BACKEND_CONTROL_READS
            else _transport_identity(opened_endpoint)
        )
        attempt_id = request.request_id

        @asynccontextmanager
        async def request_context(http_request):
            if not _matches_backend_control_target(
                http_request, target, base_url=opened_endpoint.url
            ):
                raise RoutingUnavailable()
            if request.operation in _BACKEND_CONTROL_READS:
                yield _ReadOnlyGrant()
                return
            if not isinstance(binding, BackendEndpointBinding) or identity is None:
                raise RoutingUnavailable()
            is_control = binding.kind == "control"
            parent_id = binding.control_id if is_control else binding.scope_id
            if parent_id is None:
                raise RoutingUnavailable()
            async with self._routing_runtime.transport_subject_issuer(
                parent_kind=binding.kind,
                parent_id=parent_id,
                capability=binding.capability,
                attempt_id=attempt_id,
                transport_identity=identity,
            ) as subject:
                body = {
                    "control_id" if is_control else "scope_id": str(parent_id),
                    "capability": binding.capability,
                    "attempt_id": str(attempt_id),
                    "method": http_request.method,
                    "path": http_request.url.path,
                    "transport_subject": subject,
                }
                operation = "begin_control" if is_control else "begin"
                begun = await _execute_routing(
                    self._routing_runtime,
                    operation,
                    body,
                    command_id=attempt_id,
                )
            if is_control:
                if (
                    not isinstance(begun, RoutingBeginControlResult)
                    or begun.attempt_id != attempt_id
                ):
                    raise RoutingUnavailable()
                yield _RoutingControlRequestGrant(
                    self._routing_runtime,
                    parent_id,
                    binding.capability,
                    attempt_id,
                    begun.headers,
                )
            else:
                if (
                    not isinstance(begun, RoutingBeginResult)
                    or begun.attempt_id != attempt_id
                ):
                    raise RoutingUnavailable()
                yield _RoutingRequestGrant(
                    self._routing_runtime,
                    parent_id,
                    binding.capability,
                    attempt_id,
                    begun.headers,
                )

        return request_context


@dataclass(frozen=True, slots=True, repr=False)
class _ActiveGPUControl:
    registry_id: uuid.UUID
    request_id: uuid.UUID
    operation: GPUControlOperation
    binding: BackendEndpointBinding = field(repr=False)


class BackendTransportGPUControlAdapter:
    """GPU backend operations over direct per-victim routing scopes and HTTPX."""

    def __init__(
        self,
        routing_runtime: RoutingRuntime,
        *,
        error_factory: ErrorFactory = BackendTransportError,
        observe: Observer | None = None,
    ) -> None:
        if routing_runtime is None:
            raise ValueError("routing_runtime is required")
        self._routing_runtime = routing_runtime
        self._error_factory = error_factory
        self._observe = observe or _ignore_observation
        self._active: contextvars.ContextVar[_ActiveGPUControl | None] = (
            contextvars.ContextVar("aap_gpu_control_binding", default=None)
        )

    @asynccontextmanager
    async def control_binding(
        self,
        registry_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        operation: GPUControlOperation,
    ) -> AsyncIterator[BackendEndpointBinding]:
        if (
            type(registry_id) is not uuid.UUID
            or type(request_id) is not uuid.UUID
            or operation not in _GPU_CONTROL_PATHS
            or operation == "terminal_drain"
        ):
            raise RoutingUnavailable()
        route_operation = "gpu:unload" if operation == "unload" else "backend_http"
        acquired = await _execute_routing(
            self._routing_runtime,
            "acquire",
            {
                "pool_id": None,
                "registry_id": str(registry_id),
                "project_id": None,
                "mode": "direct",
                "required_instance_id": str(registry_id),
                "owner": "inference_gpu_control",
                "operation": route_operation,
            },
            command_id=uuid.uuid4(),
        )
        if not isinstance(acquired, RoutingScopeView):
            raise RoutingUnavailable()
        if (
            acquired.registry_id != registry_id
            or acquired.mode != "direct"
            or acquired.operation != route_operation
            or acquired.closed
        ):
            if not acquired.closed:
                await self._close_scope(acquired, "error")
            raise RoutingUnavailable()
        try:
            binding = BackendEndpointBinding(
                kind="scope",
                scope_id=acquired.id,
                control_id=None,
                attempt_id=request_id,
                capability=acquired.capability,
            )
        except Exception:
            await self._close_scope(acquired, "error")
            raise RoutingUnavailable() from None
        active = _ActiveGPUControl(
            registry_id=registry_id,
            request_id=request_id,
            operation=operation,
            binding=binding,
        )
        token = self._active.set(active)
        primary_failure = False
        outcome: Literal["success", "error", "cancel"] = "error"
        try:
            yield binding
            outcome = "success"
        except asyncio.CancelledError:
            primary_failure = True
            outcome = "cancel"
            raise
        except BaseException:
            primary_failure = True
            outcome = "error"
            raise
        finally:
            self._active.reset(token)
            try:
                await self._close_scope(acquired, outcome)
            except BaseException:
                if primary_failure:
                    _logger.warning("GPU control route close was unacknowledged")
                else:
                    raise

    async def _close_scope(
        self,
        scope: RoutingScopeView,
        outcome: Literal["success", "error", "cancel"],
    ) -> None:
        closed = await _execute_routing(
            self._routing_runtime,
            "close",
            {
                "scope_id": str(scope.id),
                "capability": scope.capability,
                "outcome": outcome,
            },
            command_id=uuid.uuid4(),
        )
        if not isinstance(closed, RoutingScopeView) or closed.id != scope.id:
            raise RoutingUnavailable()

    def _bound_transport(
        self,
        endpoint: BackendEndpointSnapshot,
        *,
        request_id: uuid.UUID,
        operation: GPUControlOperation,
        timeout_seconds: float,
    ) -> BackendTransport:
        active = self._active.get()
        if (
            active is None
            or active.registry_id != endpoint.registry_id
            or active.request_id != request_id
            or active.operation != operation
            or endpoint.request_id != request_id
            or endpoint.purpose != "gpu_control"
            or endpoint.authority is None
            or not _authority_matches(endpoint.authority, active.binding)
        ):
            raise RoutingUnavailable()
        transport = BackendTransport(
            endpoint.transport_configuration(),
            options=_timeout_options(timeout_seconds),
            dispatch_context=_reject_gpu_dispatch,
            observe=self._observe,
            error_factory=self._error_factory,
            request_context=_route_request_context(
                self._routing_runtime,
                endpoint,
                active.binding,
                request_id=request_id,
                operation=operation,
            ),
        )
        return transport

    async def health_meta(
        self,
        endpoint: BackendEndpointSnapshot,
        *,
        request_id: uuid.UUID,
        gpu_health_challenge: str,
        timeout_seconds: float,
    ) -> tuple[bool, Mapping[str, object]]:
        if (
            not isinstance(endpoint, BackendEndpointSnapshot)
            or endpoint.registry_id is None
            or endpoint.request_id != request_id
            or endpoint.purpose != "health_observation"
            or endpoint.authority is not None
        ):
            raise RoutingUnavailable()
        transport = BackendTransport(
            endpoint.transport_configuration(),
            options=_timeout_options(timeout_seconds),
            dispatch_context=_reject_gpu_dispatch,
            observe=self._observe,
            error_factory=self._error_factory,
        )
        healthy, metadata = await transport.health_meta(
            gpu_health_challenge=gpu_health_challenge
        )
        return healthy, {} if metadata is None else metadata

    async def lifecycle_mode(
        self,
        endpoint: BackendEndpointSnapshot,
        request: LifecycleModeRequest,
        admission_token: str,
        *,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> LifecycleModeResponse:
        transport = self._bound_transport(
            endpoint,
            request_id=request_id,
            operation="mode",
            timeout_seconds=timeout_seconds,
        )
        return await transport.lifecycle_mode(request, admission_token=admission_token)

    async def lifecycle_reset(
        self,
        endpoint: BackendEndpointSnapshot,
        request: LifecycleResetRequest,
        admission_token: str,
        *,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> LifecycleResetResponse:
        transport = self._bound_transport(
            endpoint,
            request_id=request_id,
            operation="reset",
            timeout_seconds=timeout_seconds,
        )
        return await transport.lifecycle_reset(request, admission_token=admission_token)

    async def lifecycle_drain(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> DrainTransitionResponse:
        transport = self._bound_transport(
            endpoint,
            request_id=request_id,
            operation="drain",
            timeout_seconds=timeout_seconds,
        )
        return await transport.lifecycle_drain(grant)

    async def lifecycle_terminal_drain(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        registry_id: uuid.UUID,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> dict[str, object]:
        if (
            not isinstance(endpoint, BackendEndpointSnapshot)
            or type(registry_id) is not uuid.UUID
            or type(request_id) is not uuid.UUID
            or endpoint.registry_id != registry_id
            or endpoint.request_id != request_id
            or endpoint.purpose != "terminal_drain"
            or endpoint.authority is not None
        ):
            raise RoutingUnavailable()

        @asynccontextmanager
        async def terminal_context(http_request):
            target_url = httpx.URL(
                f"{endpoint.url.rstrip('/')}/lifecycle/terminal-drain-signed"
            )
            if (
                not _matches_backend_control_target(
                    http_request,
                    ("POST", "/lifecycle/terminal-drain-signed", None),
                    base_url=endpoint.url,
                )
                or http_request.url.scheme != target_url.scheme
                or http_request.url.host != target_url.host
                or http_request.url.port != target_url.port
                or http_request.extensions.get(GPU_UNLOAD_ROUTE_EXTENSION) is True
            ):
                raise RoutingUnavailable()
            yield _TerminalDrainGrant()

        transport = BackendTransport(
            endpoint.transport_configuration(),
            options=_timeout_options(timeout_seconds),
            dispatch_context=_reject_gpu_dispatch,
            observe=self._observe,
            error_factory=self._error_factory,
            request_context=terminal_context,
        )
        return await transport.lifecycle_terminal_drain(grant)

    async def lifecycle_cancel_drain(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> DrainTransitionResponse:
        transport = self._bound_transport(
            endpoint,
            request_id=request_id,
            operation="cancel_drain",
            timeout_seconds=timeout_seconds,
        )
        return await transport.lifecycle_cancel_drain(grant)

    async def lifecycle_unload(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> ManagedUnloadResponse:
        transport = self._bound_transport(
            endpoint,
            request_id=request_id,
            operation="unload",
            timeout_seconds=timeout_seconds,
        )
        return await transport.lifecycle_unload(grant)


@asynccontextmanager
async def _reject_gpu_dispatch(_operation, _scope):
    raise RoutingUnavailable()
    yield None


def _timeout_options(timeout_seconds: float) -> SimpleNamespace:
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive and finite")
    timeout = float(timeout_seconds)
    return SimpleNamespace(
        ml_health_timeout=timeout,
        ml_predict_timeout=timeout,
    )


def _ignore_observation(
    _backend_id: str | None, _outcome: str, _seconds: float
) -> None:
    return None


__all__ = [
    "BackendTransportGPUControlAdapter",
    "RoutingBackendControlRequestContextFactory",
    "RoutingModelRequestContextFactory",
]
