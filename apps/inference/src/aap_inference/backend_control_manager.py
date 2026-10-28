"""Credential-owning fixed backend-control operations for the Inference owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, AsyncContextManager, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx
from aap_inference_contract.backend_control import (
    BACKEND_CONTROL_PROTOCOL,
    BackendControlFailure,
    BackendControlObservation,
    BackendControlReply,
    BackendControlRequest,
    BackendRequestOnlyEndpoint,
    BackendRequestOnlyProbeFact,
    BackendRequestOnlyProbeRequest,
    BackendSmokeRequest,
    BackendStatsSnapshot,
    BackendStatsSnapshotBinding,
    BackendStatsSnapshotsRequest,
    decode_backend_control_request,
)
from aap_inference_contract.backend_endpoint import (
    BackendEndpointBinding,
    BackendEndpointPurpose,
)
from aap_inference_contract.plugin import PluginInstallation, plugin_is_active

from .backend_control_stats_postgres import (
    BackendControlStatsUnavailable,
    BackendStatsEndpoint,
)
from .backend_endpoint import (
    BackendEndpointManager,
    BackendEndpointSnapshot,
    BackendEndpointUnavailable,
)
from .backend_transport import (
    BackendOptions,
    BackendTransport,
    BackendTransportError,
    DispatchContext,
    ErrorFactory,
    backend_detail,
)
from .request_tracking import RequestContext

_HEALTH_META_KEYS = (
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
    "residency",
)
_MANUAL_HEALTH_KEYS = ("gpu_info", "cache", "model_version")
_VARIANT_KEYS = ("sam_variant", "dino_variant")
_RELOAD_KEYS = ("sam_variant", "dino_variant", "modality", "task_type")
_SMOKE_RELOAD_RESULT_KEYS = ("sam_variant", "dino_variant", "reloaded")


class BackendStatsData(Protocol):
    async def snapshots(self, request_id: UUID) -> tuple[BackendStatsEndpoint, ...]: ...

    async def readiness(self) -> bool: ...


class BackendControlRequestContextFactory(Protocol):
    def __call__(
        self,
        request: BackendControlRequest,
        opened_endpoint: BackendEndpointSnapshot,
        endpoint_binding: BackendEndpointBinding | None,
    ) -> RequestContext: ...


class BackendControlDispatchContextFactory(Protocol):
    def __call__(
        self,
        request: BackendControlRequest,
        opened_endpoint: BackendEndpointSnapshot,
    ) -> DispatchContext: ...


class UnregisteredSmokeDispatch(Protocol):
    def __call__(self, url: str, operation: str) -> AsyncContextManager[None]: ...


class _NoAuthConfiguration:
    """Preserve the catalog's existing unauthenticated live fallback behavior."""

    def __init__(self, endpoint: BackendEndpointSnapshot) -> None:
        self.id = endpoint.registry_id
        self.url = endpoint.url
        self.auth_method = "none"
        self.auth_token = None
        self.gpu_resource_id = endpoint.gpu_resource_id
        self.vram_budget_mb = endpoint.vram_budget_mb
        self.extra_params = None
        self.health_meta = None


class BackendControlManager:
    """Strict one-request-in/one-reply owner manager with no arbitrary HTTP proxy."""

    def __init__(
        self,
        endpoint_openers: Mapping[BackendEndpointPurpose, BackendEndpointManager],
        options: BackendOptions,
        *,
        request_context_factory: BackendControlRequestContextFactory,
        dispatch_context_factory: BackendControlDispatchContextFactory,
        unregistered_smoke_dispatch: UnregisteredSmokeDispatch,
        stats_data: BackendStatsData,
        error_factory: ErrorFactory = BackendTransportError,
    ) -> None:
        required = {
            "setup",
            "health_observation",
            "catalog_fallback",
            "stats",
            "backend_read",
            "lifecycle",
            "execution",
        }
        if (
            not isinstance(endpoint_openers, Mapping)
            or any(purpose not in endpoint_openers for purpose in required)
            or any(
                not callable(getattr(endpoint_openers[purpose], "open", None))
                for purpose in required
            )
            or not callable(request_context_factory)
            or not callable(dispatch_context_factory)
            or not callable(unregistered_smoke_dispatch)
            or not callable(getattr(stats_data, "snapshots", None))
            or not callable(getattr(stats_data, "readiness", None))
            or not callable(error_factory)
        ):
            raise ValueError("Invalid backend-control owner composition")
        self.endpoint_openers = dict(endpoint_openers)
        self.options = options
        self.request_context_factory = request_context_factory
        self.dispatch_context_factory = dispatch_context_factory
        self.unregistered_smoke_dispatch = unregistered_smoke_dispatch
        self.stats_data = stats_data
        self.error_factory = error_factory

    async def readiness(self) -> bool:
        try:
            if not await self.stats_data.readiness():
                return False
            for purpose in (
                "setup",
                "health_observation",
                "catalog_fallback",
                "stats",
                "backend_read",
                "lifecycle",
                "execution",
            ):
                if not await self.endpoint_openers[purpose].readiness():
                    return False
        except (BackendEndpointUnavailable, BackendControlStatsUnavailable):
            return False
        return True

    async def execute(self, request: object) -> BackendControlReply:
        typed = decode_backend_control_request(request)
        if isinstance(typed, BackendRequestOnlyProbeRequest):
            return await self._execute_request_only(typed)
        if isinstance(typed, BackendSmokeRequest):
            return await self._execute_unregistered_smoke(typed)
        if isinstance(typed, BackendStatsSnapshotsRequest):
            return await self._execute_stats_snapshots(typed)
        if isinstance(typed, BackendControlRequest):
            return await self._execute_registered(typed)
        raise ValueError("unsupported backend-control request")

    async def _execute_registered(
        self, request: BackendControlRequest
    ) -> BackendControlReply:
        observations: list[BackendControlObservation] = []

        def record(
            backend_id: str | None, outcome: str, duration_seconds: float
        ) -> None:
            observations.append(
                BackendControlObservation(
                    backend_id=backend_id,
                    outcome=outcome,
                    duration_seconds=duration_seconds,
                )
            )

        try:
            purpose = request.endpoint_subject.purpose
            opener = self.endpoint_openers[purpose]
            endpoint = await opener.open(
                request.endpoint_subject,
                request_id=request.request_id,
                purpose=purpose,
                authority=request.endpoint_subject.authority,
            )
            if request.operation == "terminal_drain":
                try:
                    installation = PluginInstallation.model_validate(
                        endpoint.transport_configuration().extra_params["plugin"]
                    )
                except (KeyError, TypeError, ValueError):
                    raise BackendEndpointUnavailable() from None
                if (
                    installation.installation_id != request.registry_id
                    or installation.manifest.resource.device != "cpu"
                    or installation.resource.gpu_resource_id is not None
                    or endpoint.gpu_resource_id is not None
                    or installation.status not in {"prepared", "disabled"}
                ):
                    raise BackendEndpointUnavailable()
            if request.operation in {
                "reload",
                "warmup",
                "install_trained_model",
                "managed_evaluation_seal",
            } and not plugin_is_active(endpoint.extra_params):
                raise BackendEndpointUnavailable()
            configuration = (
                _NoAuthConfiguration(endpoint)
                if request.operation == "catalog_fallback"
                else endpoint.transport_configuration()
            )
            dispatch_context = self.dispatch_context_factory(request, endpoint)
            request_context = self.request_context_factory(
                request, endpoint, request.binding
            )
            transport = BackendTransport(
                configuration,
                options=self.options,
                dispatch_context=dispatch_context,
                observe=record,
                error_factory=self.error_factory,
                request_context=request_context,
            )
            result = await self._registered_operation(request, transport)
            return BackendControlReply(
                schema=BACKEND_CONTROL_PROTOCOL,
                request_id=request.request_id,
                result=result,
                observations=tuple(observations),
            )
        except Exception as error:
            return self._error_reply(request.request_id, error, observations)

    async def _registered_operation(
        self, request: BackendControlRequest, transport: BackendTransport
    ) -> dict[str, Any] | list[Any]:
        operation = request.operation
        body = request.body
        if operation == "setup":
            return _require_object(await transport.setup())
        if operation == "health_meta":
            healthy, meta = await transport.health_meta()
            return {"healthy": healthy, "meta": meta}
        if operation == "unload":
            return _require_object(await transport.unload())
        if operation == "terminal_drain":
            return _require_object(await transport.terminal_drain(_require_object(body)))
        if operation == "reload":
            payload = _reload_body(body)
            return _require_object(
                await transport.reload(
                    sam_variant=payload.get("sam_variant"),
                    dino_variant=payload.get("dino_variant"),
                    modality=payload.get("modality"),
                    task_type=payload.get("task_type"),
                )
            )
        if operation == "warmup":
            return _require_object(await transport.warmup(_require_object(body)))
        if operation == "catalog_fallback":
            return _require_object(await transport.setup())
        if operation == "stats":
            healthy, meta = await transport.health_meta()
            return {"healthy": healthy, "meta": meta}
        if operation == "install_trained_model":
            return _require_object(
                await transport.install_trained_model(_require_object(body))
            )
        if operation == "trained_model_receipt":
            payload = _require_object(body)
            return _require_object(
                await transport.trained_model_receipt(payload["version_id"])
            )
        if operation == "managed_evaluation_profile":
            return _require_object(
                await transport.managed_evaluation_request("profile")
            )
        if operation == "managed_evaluation_receipt":
            payload = _require_object(body)
            return _require_object(
                await transport.managed_evaluation_request(
                    "receipt",
                    params={
                        "request_id": payload["request_id"],
                        "binding_digest": payload["binding_digest"],
                        "storage_identity": payload["storage_identity"],
                    },
                )
            )
        if operation == "managed_evaluation_seal":
            return _require_object(
                await transport.managed_evaluation_request(
                    "seal", payload=_require_object(body)
                )
            )
        if operation == "versions":
            result = await transport.get_versions()
            if type(result) is not list or any(
                type(value) is not str for value in result
            ):
                raise ValueError("Invalid versions response")
            return result
        raise ValueError("unsupported backend-control operation")

    async def _execute_request_only(
        self, request: BackendRequestOnlyProbeRequest
    ) -> BackendControlReply:
        if request.operation == "manual_probe":
            facts = [
                await self._manual_probe(request.endpoints[0], request.timeout_seconds)
            ]
        elif request.operation == "environment_observation":
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                facts = list(
                    await asyncio.gather(
                        *(
                            self._environment_probe(client, endpoint)
                            for endpoint in request.endpoints
                        )
                    )
                )
        else:
            raise ValueError("unsupported request-only operation")
        return BackendControlReply(
            schema=BACKEND_CONTROL_PROTOCOL,
            request_id=request.request_id,
            result={"results": [fact.model_dump(mode="json") for fact in facts]},
            observations=(),
        )

    async def _manual_probe(
        self, endpoint: BackendRequestOnlyEndpoint, timeout_seconds: float
    ) -> BackendRequestOnlyProbeFact:
        base = endpoint.url.rstrip("/")
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.get(
                    f"{base}/health", headers=_endpoint_headers(endpoint)
                )
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            if response.status_code != 200:
                return BackendRequestOnlyProbeFact(
                    url=base,
                    ok=False,
                    latency_ms=latency_ms,
                    status_code=response.status_code,
                    error=f"HTTP {response.status_code}",
                )
            try:
                data = response.json()
            except (ValueError, UnicodeError):
                return BackendRequestOnlyProbeFact(
                    url=base,
                    ok=False,
                    latency_ms=latency_ms,
                    status_code=response.status_code,
                    error="响应非 JSON",
                )
            if type(data) is not dict:
                return BackendRequestOnlyProbeFact(
                    url=base,
                    ok=False,
                    latency_ms=latency_ms,
                    status_code=response.status_code,
                    error="响应 JSON 格式无效",
                )
            return BackendRequestOnlyProbeFact(
                url=base,
                ok=bool(data.get("ok", True)),
                latency_ms=latency_ms,
                status_code=response.status_code,
                health=_project_health(data, _MANUAL_HEALTH_KEYS),
            )
        except (httpx.TimeoutException, httpx.RequestError, ValueError) as error:
            return BackendRequestOnlyProbeFact(
                url=base,
                ok=False,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=_safe_probe_error(error),
            )

    async def _environment_probe(
        self,
        client: httpx.AsyncClient,
        endpoint: BackendRequestOnlyEndpoint,
    ) -> BackendRequestOnlyProbeFact:
        base = endpoint.url.rstrip("/")
        started = time.monotonic()
        try:
            response = await client.get(f"{base}/health")
        except (httpx.TimeoutException, httpx.RequestError) as error:
            return BackendRequestOnlyProbeFact(
                url=base,
                ok=False,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=_safe_probe_error(error),
            )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        if response.status_code != 200:
            return BackendRequestOnlyProbeFact(
                url=base,
                ok=False,
                latency_ms=latency_ms,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}",
            )
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            return BackendRequestOnlyProbeFact(
                url=base,
                ok=False,
                latency_ms=latency_ms,
                status_code=response.status_code,
                error="响应解析失败",
            )
        if type(data) is not dict:
            return BackendRequestOnlyProbeFact(
                url=base,
                ok=False,
                latency_ms=latency_ms,
                status_code=response.status_code,
                error="响应 JSON 格式无效",
            )
        setup = None
        try:
            setup_response = await client.get(f"{base}/setup")
            if setup_response.status_code == 200:
                setup_data = setup_response.json()
                if type(setup_data) is dict:
                    setup = _project_setup(setup_data)
        except (httpx.RequestError, ValueError, UnicodeError):
            setup = None
        return BackendRequestOnlyProbeFact(
            url=base,
            ok=bool(data.get("ok", True)),
            latency_ms=latency_ms,
            status_code=200,
            health=_project_health(data, _HEALTH_META_KEYS),
            setup=setup,
        )

    async def _execute_unregistered_smoke(
        self, request: BackendSmokeRequest
    ) -> BackendControlReply:
        endpoint = request.endpoint
        base = endpoint.url.rstrip("/")
        payload = request.body.model_dump(exclude_none=True)
        headers = _endpoint_headers(endpoint)
        started = time.monotonic()
        try:
            async with self.unregistered_smoke_dispatch(base, "reload"):
                async with httpx.AsyncClient(
                    timeout=self.options.ml_predict_timeout
                ) as client:
                    response = await client.post(
                        f"{base}/reload", json=payload or None, headers=headers
                    )
                    response.raise_for_status()
                    data = response.json()
            reload_result = _require_object(data)
            load_latency_ms = max(0, int((time.monotonic() - started) * 1000))
        except Exception as error:
            return self._error_reply(request.request_id, error, [])
        auto_unloaded = False
        try:
            async with self.unregistered_smoke_dispatch(base, "unload"):
                async with httpx.AsyncClient(
                    timeout=self.options.ml_health_timeout
                ) as client:
                    response = await client.post(f"{base}/unload", headers=headers)
                    auto_unloaded = response.status_code == 200
        except Exception:
            auto_unloaded = False
        return BackendControlReply(
            schema=BACKEND_CONTROL_PROTOCOL,
            request_id=request.request_id,
            result={
                "reload": {
                    key: reload_result[key]
                    for key in _SMOKE_RELOAD_RESULT_KEYS
                    if key in reload_result
                },
                "auto_unloaded": auto_unloaded,
                "load_latency_ms": load_latency_ms,
            },
            observations=(),
        )

    async def _execute_stats_snapshots(
        self, request: BackendStatsSnapshotsRequest
    ) -> BackendControlReply:
        observations: list[BackendControlObservation] = []

        def record(
            backend_id: str | None, outcome: str, duration_seconds: float
        ) -> None:
            observations.append(
                BackendControlObservation(
                    backend_id=backend_id,
                    outcome=outcome,
                    duration_seconds=duration_seconds,
                )
            )

        try:
            endpoints = await self.stats_data.snapshots(request.request_id)
            groups = _group_stats_endpoints(endpoints)
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshots: list[BackendStatsSnapshot] = []
            for group in groups:
                backend = group["backend"]
                meta = None
                state = "error"
                try:
                    stats_request = BackendControlRequest(
                        kind="registered",
                        schema=BACKEND_CONTROL_PROTOCOL,
                        operation="stats",
                        request_id=request.request_id,
                        registry_id=backend.fact.registry_id,
                        endpoint_subject=backend.subject,
                        binding=None,
                        body=None,
                    )
                    request_context = self.request_context_factory(
                        stats_request, backend.endpoint, None
                    )
                    transport = BackendTransport(
                        backend.endpoint.transport_configuration(),
                        options=self.options,
                        dispatch_context=_readonly_dispatch,
                        observe=record,
                        error_factory=self.error_factory,
                        request_context=request_context,
                    )
                    healthy, meta = await transport.health_meta()
                    state = "ok" if healthy else "error"
                except Exception:
                    state = "error"
                    meta = None
                snapshots.append(
                    BackendStatsSnapshot(
                        physical_key=group["physical_key"],
                        url_host=group["url_host"],
                        backend_id=backend.fact.registry_id,
                        backend_name=backend.fact.name,
                        bindings=tuple(group["bindings"]),
                        state=state,
                        timestamp=timestamp,
                        meta=meta,
                    )
                )
            return BackendControlReply(
                schema=BACKEND_CONTROL_PROTOCOL,
                request_id=request.request_id,
                result={
                    "snapshots": [
                        snapshot.model_dump(mode="json") for snapshot in snapshots
                    ]
                },
                observations=tuple(observations),
            )
        except Exception as error:
            return self._error_reply(request.request_id, error, observations)

    def _error_reply(
        self,
        request_id: UUID,
        error: Exception,
        observations: list[BackendControlObservation],
    ) -> BackendControlReply:
        failure = _failure(error)
        return BackendControlReply(
            schema=BACKEND_CONTROL_PROTOCOL,
            request_id=request_id,
            observations=tuple(observations),
            error=failure,
        )


def _group_stats_endpoints(
    endpoints: tuple[BackendStatsEndpoint, ...],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for backend in endpoints:
        endpoint = backend.endpoint
        registry_id = backend.fact.registry_id
        identity = _endpoint_identity(endpoint.url)
        if identity is None:
            private_key = (f"backend:{registry_id}", "none", "")
            physical_key = f"backend:{registry_id}"
            url_host = None
        else:
            endpoint_key, url_host = identity
            auth_method = endpoint.auth_method or "none"
            auth_token = endpoint.auth_token or ""
            private_key = (endpoint_key, auth_method, auth_token)
            if auth_method == "none" and not auth_token:
                physical_key = endpoint_key
            else:
                fingerprint = hashlib.sha256(
                    f"{auth_method}:{auth_token}".encode("utf-8")
                ).hexdigest()[:8]
                physical_key = f"{endpoint_key}|auth:{fingerprint}"
        group = grouped.setdefault(
            private_key,
            {
                "backend": backend,
                "physical_key": physical_key,
                "url_host": url_host,
                "bindings": [],
            },
        )
        group["bindings"].append(
            BackendStatsSnapshotBinding(
                backend_id=registry_id,
                backend_name=backend.fact.name,
                project_id=None,
                project_display_id=None,
                project_name=None,
            )
        )
    return list(grouped.values())


def _endpoint_identity(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url if "://" in url else f"//{url}")
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    scheme = parsed.scheme or "http"
    hostport = f"{host}:{parsed.port}" if parsed.port else host
    return f"{scheme}://{hostport}", hostport


def _endpoint_headers(endpoint: BackendRequestOnlyEndpoint) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if endpoint.auth_method == "token" and endpoint.auth_token:
        headers["Authorization"] = f"Bearer {endpoint.auth_token}"
    return headers


def _project_health(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data}


def _project_setup(data: dict[str, Any]) -> dict[str, Any]:
    params = data.get("params")
    properties = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(properties, dict):
        properties = {}
    variant_properties = {}
    for key in _VARIANT_KEYS:
        descriptor = properties.get(key)
        if isinstance(descriptor, dict):
            enum = descriptor.get("enum")
            variant_properties[key] = {
                "enum": [value for value in enum if type(value) is str]
                if isinstance(enum, list)
                else []
            }
    models = data.get("models")
    projected_models = (
        [
            {
                "supported_variants": _project_variant_groups(
                    model.get("supported_variants")
                )
            }
            for model in models
            if isinstance(model, dict)
        ]
        if isinstance(models, list)
        else []
    )
    trackers = data.get("supported_trackers")
    return {
        "params": {"properties": variant_properties},
        "supported_variants": _project_variant_groups(data.get("supported_variants")),
        "models": projected_models,
        "supported_trackers": [tracker for tracker in trackers if type(tracker) is str]
        if isinstance(trackers, list)
        else [],
    }


def _project_variant_groups(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    groups: list[dict[str, Any]] = []
    for group in value:
        if not isinstance(group, dict) or type(group.get("key")) is not str:
            continue
        projected: dict[str, Any] = {"key": group["key"]}
        for key in ("title", "description"):
            if type(group.get(key)) is str:
                projected[key] = group[key]
        variants = group.get("variants")
        projected_variants = []
        if isinstance(variants, list):
            for variant in variants:
                if (
                    not isinstance(variant, dict)
                    or type(variant.get("value")) is not str
                ):
                    continue
                item: dict[str, Any] = {"value": variant["value"]}
                for key in ("label", "tier", "note"):
                    if type(variant.get(key)) is str:
                        item[key] = variant[key]
                vram = variant.get("vram_gb")
                if type(vram) in {int, float} and math.isfinite(float(vram)):
                    item["vram_gb"] = vram
                if type(variant.get("recommended")) is bool:
                    item["recommended"] = variant["recommended"]
                projected_variants.append(item)
        projected["variants"] = projected_variants
        groups.append(projected)
    return groups


def _reload_body(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if type(value) is not dict or any(key not in _RELOAD_KEYS for key in value):
        raise ValueError("invalid reload body")
    if any(type(child) is not str or not child for child in value.values()):
        raise ValueError("invalid reload body")
    return value


def _require_object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("backend response must be an object")
    return value


def _safe_probe_error(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "连接超时"
    if isinstance(error, httpx.ConnectError):
        return "连接失败"
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.RequestError):
        return "网络请求失败"
    if isinstance(error, ValueError):
        return "响应格式无效"
    return "探测失败"


def _failure(error: Exception) -> BackendControlFailure:
    if isinstance(error, BackendEndpointUnavailable):
        return BackendControlFailure(
            code="backend_endpoint_unavailable",
            status_code=503,
            detail={"reason": "backend_endpoint_unavailable"},
        )
    if isinstance(error, BackendControlStatsUnavailable):
        return BackendControlFailure(
            code="backend_stats_unavailable",
            status_code=503,
            detail={"reason": "backend_stats_unavailable"},
        )

    status_code = getattr(error, "status_code", None)
    detail = getattr(error, "detail", None)
    headers = getattr(error, "headers", None)
    retry_after = None
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        status_code = response.status_code
        detail = backend_detail(response)
        headers = response.headers
    elif isinstance(error, (httpx.TimeoutException, httpx.RequestError)):
        status_code = 502
        detail = {"reason": "backend_unreachable"}
    error_code = getattr(error, "error_code", None)
    reason = detail.get("reason") if type(detail) is dict else None
    if not isinstance(reason, str) and isinstance(error_code, str):
        reason = error_code
    code = reason if isinstance(reason, str) else "backend_operation_failed"
    if (
        not code.isascii()
        or not code.replace("_", "").isalnum()
        or code.lower() != code
    ):
        code = "backend_operation_failed"
    if headers is not None:
        retry_after = headers.get("Retry-After")
    if retry_after is None:
        retry_after_s = getattr(error, "retry_after_s", None)
        if type(retry_after_s) is int:
            retry_after = str(retry_after_s)
    if status_code is not None and (
        type(status_code) is not int or not 100 <= status_code <= 599
    ):
        status_code = None
    try:
        json.dumps(detail, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        detail = {"reason": code}
    return BackendControlFailure(
        code=code,
        status_code=status_code,
        detail=detail,
        retry_after=retry_after if type(retry_after) is str else None,
    )


class _ReadonlyDispatch:
    async def __aenter__(self):
        raise BackendTransportError(
            status_code=500,
            detail={"reason": "readonly_dispatch_invariant"},
        )

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


def _readonly_dispatch(_operation: str, _scope: Any) -> _ReadonlyDispatch:
    return _ReadonlyDispatch()


__all__ = [
    "BackendControlDispatchContextFactory",
    "BackendControlManager",
    "BackendControlRequestContextFactory",
    "BackendStatsData",
    "UnregisteredSmokeDispatch",
]
