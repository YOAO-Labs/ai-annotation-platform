"""Bounded stdlib HTTP server for independent Inference owner capabilities."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
from typing import Any
from dataclasses import replace

from pydantic import ValidationError

from aap_inference_contract.probe import (
    BACKEND_PROBE_REQUEST_MAX_BYTES,
    BACKEND_PROBE_REPLY_MAX_BYTES,
    BackendProbeInvalidRequest,
    BackendProbeUnavailable,
    backend_probe_reply_bytes,
    decode_backend_probe_reply,
    decode_backend_probe_request,
)
from aap_inference_contract.health_observation import (
    HEALTH_OBSERVATION_REPLY_MAX_BYTES,
    HEALTH_OBSERVATION_REQUEST_MAX_BYTES,
    BackendHealthObservationInvalidRequest,
    BackendHealthObservationUnavailable,
    decode_health_observation_reply,
    decode_health_observation_request,
    health_observation_reply_bytes,
)
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_REPLY_MAX_BYTES,
    REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES,
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_request,
    decode_registry_env_sync_result,
    decode_registry_env_sync_status_request,
    decode_registry_env_sync_status_result,
    registry_env_sync_result_bytes,
)
from aap_inference_contract.routing import (
    ROUTING_REPLY_MAX_BYTES,
    ROUTING_REQUEST_MAX_BYTES,
    RoutingUnavailable,
    decode_routing_command,
    decode_routing_result,
    decode_routing_runtime_request,
    decode_routing_runtime_result,
    decode_routing_status_request,
    decode_routing_status_result,
    routing_result_bytes,
    routing_runtime_result_bytes,
    decode_routing_quiescence_request,
    decode_routing_quiescence_result,
    routing_quiescence_result_bytes,
)
from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_REPLY_MAX_BYTES,
    SCOPE_RETIREMENT_REQUEST_MAX_BYTES,
    ScopeRetirementUnavailable,
    decode_scope_retirement_request,
    decode_scope_retirement_result,
    scope_retirement_result_bytes,
)
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_STATUS_PATH,
    VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES,
    VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES,
    VideoScopeRejected,
    decode_video_execution_status_request,
    decode_video_execution_status_result,
    video_execution_status_result_bytes,
)
from aap_inference_contract.execution import (
    ModelExecutionError,
    decode_model_operation_request,
    model_execution_failure_bytes,
    model_operation_result_bytes,
)
from aap_inference_contract.execution_client import MODEL_EXECUTION_PATH
from aap_inference_contract.backend_control import (
    BACKEND_CONTROL_REQUEST_MAX_BYTES,
    BACKEND_CONTROL_REPLY_MAX_BYTES,
    decode_backend_control_request,
    decode_backend_control_reply,
)
from aap_inference_contract import gpu as gpu_contract
from aap_inference.gpu.contracts import GPUArbiterDispatchError

from .pipeline_contract import (
    PIPELINE_REQUEST_BODY_MAX_BYTES,
    PIPELINE_RESPONSE_BODY_MAX_BYTES,
    PIPELINE_PROTOCOL,
    PipelineErrorDetails,
    PipelineInvalidRequest,
    PipelinePostCommitFailure,
    PipelineRejected,
    PipelineStatusFailure,
    PipelineUnavailable,
    decode_pipeline_status_reply,
    decode_pipeline_status_request,
)

_MAX_HEADER_BYTES = 65_536
_READ_TIMEOUT_SECONDS = 5.0
_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
_ROUTES = {
    "/v1/pipelines/execute": "execute",
    "/v1/pipelines/query": "query",
    "/v1/pipelines/status": "status",
}
_ROUTING_COMMAND_PATH = "/v1/routes/command"
_ROUTING_STATUS_PATH = "/v1/routes/status"
_ROUTING_RUNTIME_PATH = "/v1/routes/runtime"
_ROUTING_RETIRE_PATH = "/v1/routes/retire"
_ROUTING_RETIREMENT_STATUS_PATH = "/v1/routes/retirement-status"
_GPU_PATHS = frozenset(
    {
        gpu_contract.GPU_DISPATCH_ACQUIRE_PATH,
        gpu_contract.GPU_DISPATCH_SETTLE_PATH,
        gpu_contract.GPU_ADMIN_ACTIVATE_PATH,
        gpu_contract.GPU_ADMIN_INSTALLATION_ACTIVATE_PATH,
        gpu_contract.GPU_ADMIN_INSTALLATION_QUIESCE_PATH,
        gpu_contract.GPU_ADMIN_ROLLOUT_PATH,
        gpu_contract.GPU_ADMIN_OBSERVE_PATH,
        gpu_contract.GPU_ADMIN_REPAIR_PATH,
        gpu_contract.GPU_ADMIN_PROBE_PATH,
        gpu_contract.GPU_ADMIN_COLLECT_PATH,
        gpu_contract.GPU_ADMIN_DIAGNOSTICS_PATH,
    }
)


class _BadRequest(Exception):
    pass


class _PayloadTooLarge(Exception):
    pass


class _UnauthorizedRequest(Exception):
    pass


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PipelineUnavailable() from None


def _model_bytes(reply: Any) -> bytes:
    if not hasattr(reply, "model_dump"):
        raise RuntimeError("Invalid Inference pipeline reply")
    try:
        return json.dumps(
            reply.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        # Preserve the owner’s generic 500 phase, including legacy materializer
        # errors such as an integer exceeding Python's JSON conversion limit.
        raise RuntimeError("Invalid Inference pipeline reply") from None


class PipelineHttpServer:
    """Run authenticated Inference owner routes with bounded work."""

    def __init__(
        self,
        manager,
        data,
        control_token: str | None,
        *,
        host: str = "127.0.0.1",
        port: int = 8768,
        max_concurrency: int = 8,
        max_request_bytes: int = PIPELINE_REQUEST_BODY_MAX_BYTES,
        max_response_bytes: int = PIPELINE_RESPONSE_BODY_MAX_BYTES,
        read_timeout_seconds: float = _READ_TIMEOUT_SECONDS,
        backend_probe_manager=None,
        backend_probe_control_token: str | None = None,
        health_observation_manager=None,
        health_observation_control_token: str | None = None,
        registry_ingress_manager=None,
        registry_ingress_control_token: str | None = None,
        routing_manager=None,
        routing_control_token: str | None = None,
        scope_retirement_manager=None,
        model_execution_manager=None,
        model_execution_control_token: str | None = None,
        backend_control_manager=None,
        backend_control_token: str | None = None,
        gpu_manager=None,
        gpu_control_token: str | None = None,
        required_capabilities: tuple[str, ...] = (),
    ) -> None:
        if type(required_capabilities) is not tuple or any(
            type(capability) is not str
            or capability
            not in {
                "pipelines",
                "backend_probe",
                "health_observation",
                "registry_ingress",
                "routing",
                "execution",
                "backend_control",
                "gpu",
            }
            for capability in required_capabilities
        ):
            raise ValueError("Invalid required Inference capabilities")
        if (
            type(host) is not str
            or not host
            or type(port) is not int
            or not 1 <= port <= 65_535
            or type(max_concurrency) is not int
            or not 1 <= max_concurrency <= 64
            or type(max_request_bytes) is not int
            or not 1 <= max_request_bytes <= PIPELINE_REQUEST_BODY_MAX_BYTES
            or type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= PIPELINE_RESPONSE_BODY_MAX_BYTES
            or type(read_timeout_seconds) not in {int, float}
            or not math.isfinite(float(read_timeout_seconds))
            or read_timeout_seconds <= 0
        ):
            raise ValueError("Invalid Inference pipeline HTTP server settings")
        capability_tokens = (
            control_token,
            backend_probe_control_token,
            health_observation_control_token,
            registry_ingress_control_token,
            routing_control_token,
            model_execution_control_token,
            backend_control_token,
            gpu_control_token,
        )
        for index, token in enumerate(capability_tokens):
            if token is None:
                continue
            if (
                type(token) is not str
                or not token.isascii()
                or not token
                or any(character.isspace() for character in token)
            ):
                raise ValueError("Invalid Inference pipeline HTTP server settings")
            for previous in range(index):
                other = capability_tokens[previous]
                if other is not None and hmac.compare_digest(token, other):
                    raise ValueError("Invalid Inference pipeline HTTP server settings")
        self.manager = manager
        self.data = data
        self.control_token = control_token
        self.backend_probe_manager = backend_probe_manager
        self.backend_probe_control_token = backend_probe_control_token
        self.health_observation_manager = health_observation_manager
        self.health_observation_control_token = health_observation_control_token
        self.registry_ingress_manager = registry_ingress_manager
        self.registry_ingress_control_token = registry_ingress_control_token
        self.routing_manager = routing_manager
        self.routing_control_token = routing_control_token
        self.scope_retirement_manager = (
            scope_retirement_manager
            if scope_retirement_manager is not None
            else getattr(routing_manager, "scope_retirement_manager", None)
        )
        self.model_execution_manager = model_execution_manager
        self.model_execution_control_token = model_execution_control_token
        self.backend_control_manager = backend_control_manager
        self.backend_control_token = backend_control_token
        self.gpu_manager = gpu_manager
        self.gpu_control_token = gpu_control_token
        self.required_capabilities = frozenset(required_capabilities)
        self.host = host
        self.port = port
        self.max_concurrency = max_concurrency
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.read_timeout_seconds = float(read_timeout_seconds)
        self._active = 0
        self._server = None

    def _reserve_request(self) -> bool:
        if self._active >= self.max_concurrency:
            return False
        self._active += 1
        return True

    async def start(self):
        if self._server is not None:
            return self._server
        self._server = await asyncio.start_server(
            self._handle,
            self.host,
            self.port,
            limit=_MAX_HEADER_BYTES,
        )
        return self._server

    async def serve_forever(self) -> None:
        server = await self.start()
        async with server:
            await server.serve_forever()

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self._reserve_request():
            await self._write_json(
                writer, 503, {"detail": "Inference pipeline owner busy"}
            )
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        try:
            path = ""
            try:
                method, path, headers, body = await self._read_request(reader)
                status, response_body = await self._dispatch(
                    method, path, headers, body
                )
            except _BadRequest:
                status, response_body = (
                    400,
                    _json_bytes({"detail": "Invalid HTTP request"}),
                )
            except _PayloadTooLarge:
                status, response_body = (
                    413,
                    _json_bytes({"detail": "Request body too large"}),
                )
            except _UnauthorizedRequest:
                status, response_body = (
                    401,
                    _json_bytes({"detail": "Unauthorized"}),
                )
            except PipelineInvalidRequest:
                status, response_body = (
                    400,
                    _json_bytes({"detail": "Invalid Inference pipeline request"}),
                )
            except PipelineUnavailable:
                status, response_body = (
                    503,
                    _json_bytes({"detail": "Inference pipeline owner unavailable"}),
                )
            except Exception:
                # Do not expose driver, request, or credential diagnostics.
                status, response_body = (
                    500,
                    _json_bytes({"detail": "Internal Server Error"}),
                )
            response_limit = self._response_limit(path)
            if response_limit is not None and len(response_body) > response_limit:
                status, response_body = (
                    503,
                    _json_bytes({"detail": "Inference pipeline owner unavailable"}),
                )
            await self._write_bytes(writer, status, response_body)
        finally:
            self._active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _read_line(self, reader: asyncio.StreamReader) -> bytes:
        try:
            value = await asyncio.wait_for(
                reader.readline(), timeout=self.read_timeout_seconds
            )
        except (
            asyncio.TimeoutError,
            asyncio.LimitOverrunError,
            asyncio.IncompleteReadError,
            ValueError,
        ):
            raise _BadRequest() from None
        if not value or len(value) > _MAX_HEADER_BYTES:
            raise _BadRequest()
        return value

    async def _read_request(self, reader: asyncio.StreamReader):
        request_line = await self._read_line(reader)
        if not request_line.endswith(b"\r\n"):
            raise _BadRequest()
        parts = request_line[:-2].split(b" ")
        if len(parts) != 3:
            raise _BadRequest()
        try:
            method = parts[0].decode("ascii")
            target = parts[1].decode("ascii")
            version = parts[2].decode("ascii")
        except UnicodeError:
            raise _BadRequest() from None
        if version != "HTTP/1.1" or not target.startswith("/"):
            raise _BadRequest()
        request_limit = self._request_limit(target)

        headers: dict[str, str] = {}
        header_size = len(request_line)
        while True:
            line = await self._read_line(reader)
            header_size += len(line)
            if header_size > _MAX_HEADER_BYTES:
                raise _BadRequest()
            if line == b"\r\n":
                break
            if not line.endswith(b"\r\n") or b":" not in line:
                raise _BadRequest()
            name, value = line[:-2].split(b":", 1)
            try:
                key = name.decode("ascii").strip().lower()
                text_value = value.decode("latin-1").strip()
            except UnicodeError:
                raise _BadRequest() from None
            if (
                not key
                or any(character.isspace() for character in key)
                or key in headers
            ):
                raise _BadRequest()
            headers[key] = text_value

        if "transfer-encoding" in headers:
            raise _BadRequest()
        model_execution = method == "POST" and target == MODEL_EXECUTION_PATH
        if model_execution:
            if (
                self.model_execution_manager is None
                or self.model_execution_control_token is None
            ):
                raise PipelineUnavailable()
            if not self._model_execution_authorized(headers):
                raise _UnauthorizedRequest()
        content_length = 0
        if "content-length" in headers:
            raw_length = headers["content-length"]
            if not raw_length.isascii() or not raw_length.isdigit():
                raise _BadRequest()
            significant_length = raw_length.lstrip("0") or "0"
            if request_limit is not None and len(significant_length) > len(
                str(request_limit)
            ):
                raise _PayloadTooLarge()
            try:
                content_length = int(significant_length)
            except ValueError:
                raise _BadRequest() from None
        elif method == "POST":
            raise _BadRequest()
        if request_limit is not None and content_length > request_limit:
            raise _PayloadTooLarge()
        if method == "POST":
            content_type = (
                headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if content_type != "application/json":
                raise _BadRequest()
            try:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=None if model_execution else self.read_timeout_seconds,
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raise _BadRequest() from None
        else:
            if content_length:
                raise _BadRequest()
            body = b""
        return method, target, headers, body

    async def _dispatch(self, method: str, path: str, headers, body: bytes):
        if method == "GET" and path == "/health":
            pipeline_configured = (
                self.control_token is not None
                and self.manager is not None
                and self.data is not None
            )
            other_capabilities = (
                (
                    "backend_probe",
                    self.backend_probe_control_token,
                    self.backend_probe_manager,
                ),
                (
                    "health_observation",
                    self.health_observation_control_token,
                    self.health_observation_manager,
                ),
                (
                    "registry_ingress",
                    self.registry_ingress_control_token,
                    self.registry_ingress_manager,
                ),
                ("routing", self.routing_control_token, self.routing_manager),
                (
                    "execution",
                    self.model_execution_control_token,
                    self.model_execution_manager,
                ),
                (
                    "backend_control",
                    self.backend_control_token,
                    self.backend_control_manager,
                ),
                ("gpu", self.gpu_control_token, self.gpu_manager),
            )
            capability_readiness = {name: False for name, _, _ in other_capabilities}
            if not any(
                token is not None and manager is not None
                for _, token, manager in other_capabilities
            ):
                # Preserve the pipeline-only failure phase.
                capability_readiness["pipelines"] = (
                    await self.data.readiness() if pipeline_configured else False
                )
            else:
                try:
                    capability_readiness["pipelines"] = (
                        await self.data.readiness() if pipeline_configured else False
                    )
                except Exception:
                    capability_readiness["pipelines"] = False
                for name, token, manager in other_capabilities:
                    if token is not None and manager is not None:
                        try:
                            capability_readiness[name] = await manager.readiness()
                        except Exception:
                            capability_readiness[name] = False
            if self.required_capabilities:
                ready = all(
                    capability_readiness[capability]
                    for capability in self.required_capabilities
                )
            else:
                ready = any(capability_readiness.values())
            return (200 if ready else 503), _json_bytes(
                {"status": "ready" if ready else "not_ready"}
            )
        if method == "POST" and path == "/v1/backends/probe":
            return await self._dispatch_backend_probe(headers, body)
        if method == "POST" and path == "/v1/backends/health-observation":
            return await self._dispatch_health_observation(headers, body)
        if method == "POST" and path == "/v1/registry/env-sync":
            return await self._dispatch_registry_ingress_sync(headers, body)
        if method == "POST" and path == "/v1/registry/env-sync-status":
            return await self._dispatch_registry_ingress_status(headers, body)
        if method == "POST" and path == _ROUTING_COMMAND_PATH:
            return await self._dispatch_routing_command(headers, body)
        if method == "POST" and path == _ROUTING_STATUS_PATH:
            return await self._dispatch_routing_status(headers, body)
        if method == "POST" and path == _ROUTING_RUNTIME_PATH:
            return await self._dispatch_routing_runtime(headers, body)
        if method == "POST" and path == "/v1/routes/quiescence":
            return await self._dispatch_routing_quiescence(headers, body)
        if method == "POST" and path == _ROUTING_RETIRE_PATH:
            return await self._dispatch_scope_retirement(headers, body)
        if method == "POST" and path == _ROUTING_RETIREMENT_STATUS_PATH:
            return await self._dispatch_scope_retirement_status(headers, body)
        if method == "POST" and path == VIDEO_EXECUTION_STATUS_PATH:
            return await self._dispatch_video_execution_status(headers, body)
        if method == "POST" and path == MODEL_EXECUTION_PATH:
            return await self._dispatch_model_execution(headers, body)
        if method == "POST" and path == "/v1/backends/control":
            return await self._dispatch_backend_control(headers, body)
        if method == "POST" and path in _GPU_PATHS:
            return await self._dispatch_gpu(path, headers, body)
        operation = _ROUTES.get(path)
        if method != "POST" or operation is None:
            return 404, _json_bytes({"detail": "Not Found"})
        if self.control_token is None:
            return 503, _json_bytes({"detail": "Inference pipeline owner unavailable"})
        authorization = headers.get("authorization")
        expected = f"Bearer {self.control_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        ):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if self.manager is None:
            return 503, _json_bytes({"detail": "Inference pipeline owner unavailable"})

        try:
            if operation == "execute":
                reply = await self.manager.execute(body)
            elif operation == "query":
                reply = await self.manager.query(body)
            else:
                reply = await self.manager.status(body)
        except PipelineRejected as error:
            if operation != "status":
                raise
            request = decode_pipeline_status_request(body)
            reply = decode_pipeline_status_reply(
                request,
                PipelineStatusFailure(
                    schema=PIPELINE_PROTOCOL,
                    ok=False,
                    request_id=request.request_id,
                    operation=request.operation,
                    operation_id=request.command.operation_id,
                    command_operation=request.command.operation,
                    actor_id=request.command.admitted_actor.id,
                    target_id=request.command.target_id,
                    error=PipelineErrorDetails(
                        status_code=error.status_code,
                        detail=error.detail,
                        headers=error.headers,
                        rollback=error.rollback,
                    ),
                ),
            )
        except PipelinePostCommitFailure:
            return 500, _json_bytes({"detail": "Internal Server Error"})
        return 200, _model_bytes(reply)

    async def _dispatch_backend_control(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.backend_control_manager is None or self.backend_control_token is None:
            return 503, _json_bytes({"detail": "Inference backend control unavailable"})
        if not self._capability_authorized(headers, self.backend_control_token):
            return 401, _json_bytes({"detail": "Unauthorized"})
        try:
            request = decode_backend_control_request(body)
        except (ValueError, TypeError):
            return 400, _json_bytes({"detail": "Invalid backend control request"})
        reply = await self.backend_control_manager.execute(request)
        return 200, _model_bytes(decode_backend_control_reply(request, reply))

    async def _dispatch_gpu(self, path, headers, body: bytes) -> tuple[int, bytes]:
        if self.gpu_manager is None or self.gpu_control_token is None:
            return 503, _json_bytes({"detail": "Inference GPU authority unavailable"})
        if not self._capability_authorized(headers, self.gpu_control_token):
            return 401, _json_bytes({"detail": "Unauthorized"})
        try:
            if path == gpu_contract.GPU_DISPATCH_ACQUIRE_PATH:
                request = gpu_contract.GPUDispatchAcquireRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.acquire_dispatch(request)
            elif path == gpu_contract.GPU_DISPATCH_SETTLE_PATH:
                request = gpu_contract.GPUDispatchSettleRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.settle_dispatch(request)
            elif path == gpu_contract.GPU_ADMIN_ACTIVATE_PATH:
                request = (
                    gpu_contract.GPUActivateMembershipsRequest.model_validate_json(body)
                )
                reply = await self.gpu_manager.activate_resource_memberships(
                    request.resource_id, pending_only=request.pending_only
                )
            elif path == gpu_contract.GPU_ADMIN_INSTALLATION_ACTIVATE_PATH:
                request = gpu_contract.GPUInstallationControlRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.activate_installation(request)
            elif path == gpu_contract.GPU_ADMIN_INSTALLATION_QUIESCE_PATH:
                request = gpu_contract.GPUInstallationControlRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.quiesce_installation(request)
            elif path == gpu_contract.GPU_ADMIN_ROLLOUT_PATH:
                request = gpu_contract.GPURolloutControlRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.advance_resource_rollout_control(
                    request.resource_id, request.transition_id, request.target_gate
                )
            elif path == gpu_contract.GPU_ADMIN_OBSERVE_PATH:
                request = (
                    gpu_contract.GPUResourceObservationRequest.model_validate_json(body)
                )
                reply = await self.gpu_manager.observe_resource(
                    request.resource_id, allocatable_mb=request.allocatable_mb
                )
            elif path == gpu_contract.GPU_ADMIN_REPAIR_PATH:
                request = gpu_contract.GPUResourceRepairRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.repair_resource(
                    request.resource_id,
                    allocatable_mb=request.allocatable_mb,
                    force_proof_reset=request.force_proof_reset,
                    readiness_blocker=request.readiness_blocker,
                )
            elif path == gpu_contract.GPU_ADMIN_PROBE_PATH:
                request = gpu_contract.GPURetiredMembershipRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.probe_retired_membership(
                    request.backend_id, request.resource_id, request.membership_epoch
                )
            elif path == gpu_contract.GPU_ADMIN_COLLECT_PATH:
                request = gpu_contract.GPURetiredMembershipRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.collect_tombstone(
                    request.backend_id, request.resource_id, request.membership_epoch
                )
            else:
                request = gpu_contract.GPUAdminDiagnosticsRequest.model_validate_json(
                    body
                )
                reply = await self.gpu_manager.get_diagnostics(request.resource_id)
        except GPUArbiterDispatchError as error:
            failure = gpu_contract.GPUAuthorityErrorReply(
                status_code=error.status_code,
                detail=error.detail,
                headers=error.headers or {},
                error_code=error.error_code.value,
                retry_after_s=error.retry_after_s,
            )
            return error.status_code, _model_bytes(failure)
        except ValidationError:
            return 400, _json_bytes({"detail": "Invalid GPU authority request"})
        return 200, _model_bytes(reply)

    @staticmethod
    def _capability_authorized(headers, token) -> bool:
        authorization = headers.get("authorization")
        return authorization is not None and hmac.compare_digest(
            authorization.encode("utf-8"), f"Bearer {token}".encode("utf-8")
        )

    async def _dispatch_model_execution(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if (
            self.model_execution_manager is None
            or self.model_execution_control_token is None
        ):
            return 503, _json_bytes({"detail": "Inference execution owner unavailable"})
        if not self._model_execution_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        try:
            request = decode_model_operation_request(body)
        except ValueError:
            return 400, _json_bytes({"detail": "Invalid model execution request"})
        try:
            result = await self.model_execution_manager.execute(request)
        except ModelExecutionError as error:
            failure = error.failure
            if failure.status_code is None:
                failure = replace(failure, status_code=503)
            return failure.status_code, model_execution_failure_bytes(failure)
        return 200, model_operation_result_bytes(result)

    def _model_execution_authorized(self, headers) -> bool:
        if (
            self.model_execution_manager is None
            or self.model_execution_control_token is None
        ):
            return False
        authorization = headers.get("authorization")
        expected = f"Bearer {self.model_execution_control_token}"
        return authorization is not None and hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        )

    def _request_limit(self, path: str) -> int | None:
        if path == MODEL_EXECUTION_PATH:
            return None
        if path == "/v1/backends/control":
            return BACKEND_CONTROL_REQUEST_MAX_BYTES
        if path in _GPU_PATHS:
            return gpu_contract.GPU_AUTHORITY_REQUEST_MAX_BYTES
        if path == "/v1/backends/health-observation":
            return HEALTH_OBSERVATION_REQUEST_MAX_BYTES
        if path in {"/v1/registry/env-sync", "/v1/registry/env-sync-status"}:
            return REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES
        if path.split("?", 1)[0] in {
            _ROUTING_RETIRE_PATH,
            _ROUTING_RETIREMENT_STATUS_PATH,
        }:
            return SCOPE_RETIREMENT_REQUEST_MAX_BYTES
        if path.split("?", 1)[0] == VIDEO_EXECUTION_STATUS_PATH:
            return VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES
        if path.split("?", 1)[0] in {
            _ROUTING_COMMAND_PATH,
            _ROUTING_STATUS_PATH,
            _ROUTING_RUNTIME_PATH,
            "/v1/routes/quiescence",
        }:
            return ROUTING_REQUEST_MAX_BYTES
        return self.max_request_bytes

    def _response_limit(self, path: str) -> int | None:
        if path == MODEL_EXECUTION_PATH:
            return None
        if path == "/v1/backends/control":
            return BACKEND_CONTROL_REPLY_MAX_BYTES
        if path in _GPU_PATHS:
            return gpu_contract.GPU_AUTHORITY_REPLY_MAX_BYTES
        if path == "/v1/backends/health-observation":
            return HEALTH_OBSERVATION_REPLY_MAX_BYTES
        if path in {"/v1/registry/env-sync", "/v1/registry/env-sync-status"}:
            return REGISTRY_ENV_SYNC_REPLY_MAX_BYTES
        if path.split("?", 1)[0] in {
            _ROUTING_RETIRE_PATH,
            _ROUTING_RETIREMENT_STATUS_PATH,
        }:
            return SCOPE_RETIREMENT_REPLY_MAX_BYTES
        if path.split("?", 1)[0] == VIDEO_EXECUTION_STATUS_PATH:
            return VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES
        if path.split("?", 1)[0] in {
            _ROUTING_COMMAND_PATH,
            _ROUTING_STATUS_PATH,
            _ROUTING_RUNTIME_PATH,
            "/v1/routes/quiescence",
        }:
            return ROUTING_REPLY_MAX_BYTES
        return self.max_response_bytes

    def _registry_ingress_authorized(self, headers) -> bool:
        if self.registry_ingress_manager is None or (
            self.registry_ingress_control_token is None
        ):
            return False
        authorization = headers.get("authorization")
        expected = f"Bearer {self.registry_ingress_control_token}"
        return authorization is not None and hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        )

    async def _dispatch_routing_quiescence(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.routing_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._capability_authorized(headers, self.routing_control_token):
            return 401, _json_bytes({"detail": "Unauthorized"})
        try:
            request = decode_routing_quiescence_request(body)
        except RoutingUnavailable:
            return 400, _json_bytes({"detail": "Invalid routing quiescence request"})
        try:
            reply = await self.routing_manager.quiescence(request)
            result = decode_routing_quiescence_result(request, reply)
            return 200, routing_quiescence_result_bytes(result)
        except RoutingUnavailable:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})

    async def _dispatch_registry_ingress_sync(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.registry_ingress_manager is None or (
            self.registry_ingress_control_token is None
        ):
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        if not self._registry_ingress_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_registry_env_sync_request(body)
        except (RegistryEnvSyncUnavailable, ValueError, TypeError, UnicodeError):
            return 400, _json_bytes(
                {"detail": "Invalid Registry environment sync request"}
            )
        try:
            result = await self.registry_ingress_manager.sync(request)
            encoded = registry_env_sync_result_bytes(result)
            bound = decode_registry_env_sync_result(request, encoded)
        except RegistryEnvSyncUnavailable:
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        if bound.state != "committed":
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        if len(encoded) > REGISTRY_ENV_SYNC_REPLY_MAX_BYTES:
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        return 200, encoded

    async def _dispatch_registry_ingress_status(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.registry_ingress_manager is None or (
            self.registry_ingress_control_token is None
        ):
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        if not self._registry_ingress_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_registry_env_sync_status_request(body)
        except (RegistryEnvSyncUnavailable, ValueError, TypeError, UnicodeError):
            return 400, _json_bytes(
                {"detail": "Invalid Registry environment sync status request"}
            )
        try:
            result = await self.registry_ingress_manager.status(request)
            encoded = registry_env_sync_result_bytes(result)
            decode_registry_env_sync_status_result(request, encoded)
        except RegistryEnvSyncUnavailable:
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        if len(encoded) > REGISTRY_ENV_SYNC_REPLY_MAX_BYTES:
            return 503, _json_bytes(
                {"detail": "Inference Registry ingress owner unavailable"}
            )
        return 200, encoded

    def _routing_authorized(self, headers) -> bool:
        if (
            self.routing_manager is None and self.scope_retirement_manager is None
        ) or self.routing_control_token is None:
            return False
        authorization = headers.get("authorization")
        expected = f"Bearer {self.routing_control_token}"
        return authorization is not None and hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        )

    async def _dispatch_routing_command(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.routing_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._routing_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > ROUTING_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            command = decode_routing_command(body)
        except RoutingUnavailable:
            return 400, _json_bytes({"detail": "Invalid Inference routing command"})
        try:
            result = await self.routing_manager.execute(command)
            encoded = routing_result_bytes(result)
            decode_routing_result(command, encoded)
        except RoutingUnavailable:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if len(encoded) > min(self.max_response_bytes, ROUTING_REPLY_MAX_BYTES):
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        return 200, encoded

    async def _dispatch_routing_status(self, headers, body: bytes) -> tuple[int, bytes]:
        if self.routing_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._routing_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > ROUTING_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_routing_status_request(body)
        except RoutingUnavailable:
            return 400, _json_bytes(
                {"detail": "Invalid Inference routing status request"}
            )
        try:
            result = await self.routing_manager.status(request)
            encoded = routing_result_bytes(result)
            decode_routing_status_result(request, encoded)
        except RoutingUnavailable:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if len(encoded) > min(self.max_response_bytes, ROUTING_REPLY_MAX_BYTES):
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        return 200, encoded

    async def _dispatch_routing_runtime(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.routing_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._routing_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > ROUTING_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_routing_runtime_request(body)
        except RoutingUnavailable:
            return 400, _json_bytes(
                {"detail": "Invalid Inference routing runtime request"}
            )
        try:
            result = await self.routing_manager.runtime(request)
            encoded = routing_runtime_result_bytes(result)
            decode_routing_runtime_result(request, encoded)
        except RoutingUnavailable:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if len(encoded) > min(self.max_response_bytes, ROUTING_REPLY_MAX_BYTES):
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        return 200, encoded

    async def _dispatch_scope_retirement(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.scope_retirement_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._routing_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > SCOPE_RETIREMENT_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_scope_retirement_request(body)
        except ScopeRetirementUnavailable:
            return 400, _json_bytes(
                {"detail": "Invalid Inference scope-retirement request"}
            )
        try:
            result = await self.scope_retirement_manager.execute(request)
            encoded = scope_retirement_result_bytes(result)
            decode_scope_retirement_result(request, encoded)
        except ScopeRetirementUnavailable:
            return 503, _json_bytes(
                {"detail": "Inference scope-retirement owner unavailable"}
            )
        if len(encoded) > min(
            self.max_response_bytes, SCOPE_RETIREMENT_REPLY_MAX_BYTES
        ):
            return 503, _json_bytes(
                {"detail": "Inference scope-retirement owner unavailable"}
            )
        return 200, encoded

    async def _dispatch_scope_retirement_status(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.scope_retirement_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._routing_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > SCOPE_RETIREMENT_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_scope_retirement_request(body)
        except ScopeRetirementUnavailable:
            return 400, _json_bytes(
                {"detail": "Invalid Inference scope-retirement status request"}
            )
        try:
            result = await self.scope_retirement_manager.status(request)
            encoded = scope_retirement_result_bytes(result)
            decode_scope_retirement_result(request, encoded)
        except ScopeRetirementUnavailable:
            return 503, _json_bytes(
                {"detail": "Inference scope-retirement owner unavailable"}
            )
        if len(encoded) > min(
            self.max_response_bytes, SCOPE_RETIREMENT_REPLY_MAX_BYTES
        ):
            return 503, _json_bytes(
                {"detail": "Inference scope-retirement owner unavailable"}
            )
        return 200, encoded

    async def _dispatch_video_execution_status(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if self.routing_manager is None or self.routing_control_token is None:
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if not self._routing_authorized(headers):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            proof = decode_video_execution_status_request(body)
        except RoutingUnavailable:
            return 400, _json_bytes(
                {"detail": "Invalid Inference video execution status request"}
            )
        try:
            data = await self.routing_manager.video_execution_status(proof)
            encoded = video_execution_status_result_bytes(proof, data)
            decode_video_execution_status_result(proof, encoded)
        except (RoutingUnavailable, VideoScopeRejected):
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        if len(encoded) > min(
            self.max_response_bytes, VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES
        ):
            return 503, _json_bytes({"detail": "Inference routing owner unavailable"})
        return 200, encoded

    async def _dispatch_health_observation(
        self, headers, body: bytes
    ) -> tuple[int, bytes]:
        if (
            self.health_observation_control_token is None
            or self.health_observation_manager is None
        ):
            return 503, _json_bytes(
                {"detail": "Inference health observation owner unavailable"}
            )
        authorization = headers.get("authorization")
        expected = f"Bearer {self.health_observation_control_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        ):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > HEALTH_OBSERVATION_REQUEST_MAX_BYTES:
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_health_observation_request(body)
        except (
            BackendHealthObservationInvalidRequest,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            return 400, _json_bytes(
                {"detail": "Invalid Inference health observation request"}
            )
        try:
            reply = await self.health_observation_manager.execute(request)
            encoded = health_observation_reply_bytes(reply)
            decode_health_observation_reply(request, encoded)
        except (
            BackendHealthObservationUnavailable,
            BackendHealthObservationInvalidRequest,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            return 503, _json_bytes(
                {"detail": "Inference health observation owner unavailable"}
            )
        if len(encoded) > HEALTH_OBSERVATION_REPLY_MAX_BYTES:
            return 503, _json_bytes(
                {"detail": "Inference health observation owner unavailable"}
            )
        return 200, encoded

    async def _dispatch_backend_probe(self, headers, body: bytes) -> tuple[int, bytes]:
        if (
            self.backend_probe_control_token is None
            or self.backend_probe_manager is None
        ):
            return 503, _json_bytes(
                {"detail": "Inference backend probe owner unavailable"}
            )
        authorization = headers.get("authorization")
        expected = f"Bearer {self.backend_probe_control_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        ):
            return 401, _json_bytes({"detail": "Unauthorized"})
        if len(body) > min(self.max_request_bytes, BACKEND_PROBE_REQUEST_MAX_BYTES):
            return 413, _json_bytes({"detail": "Request body too large"})
        try:
            request = decode_backend_probe_request(body)
        except (
            BackendProbeInvalidRequest,
            BackendProbeUnavailable,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            return 400, _json_bytes(
                {"detail": "Invalid Inference backend probe request"}
            )
        try:
            reply = await self.backend_probe_manager.execute(request)
            encoded = backend_probe_reply_bytes(reply)
            decode_backend_probe_reply(request, encoded)
        except BackendProbeUnavailable:
            return 503, _json_bytes(
                {"detail": "Inference backend probe owner unavailable"}
            )
        except (
            BackendProbeInvalidRequest,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            return 503, _json_bytes(
                {"detail": "Inference backend probe owner unavailable"}
            )
        if len(encoded) > min(self.max_response_bytes, BACKEND_PROBE_REPLY_MAX_BYTES):
            return 503, _json_bytes(
                {"detail": "Inference backend probe owner unavailable"}
            )
        return 200, encoded

    async def _write_json(
        self, writer: asyncio.StreamWriter, status: int, payload
    ) -> None:
        await self._write_bytes(writer, status, _json_bytes(payload))

    async def _write_bytes(
        self, writer: asyncio.StreamWriter, status: int, body: bytes
    ) -> None:
        reason = _STATUS_TEXT.get(status, "Internal Server Error")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        try:
            writer.write(header + body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass


__all__ = ["PipelineHttpServer"]
