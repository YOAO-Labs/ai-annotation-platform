"""Bounded stdlib HTTP server for independent Inference owner capabilities."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
from typing import Any

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


class _BadRequest(Exception):
    pass


class _PayloadTooLarge(Exception):
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
    ) -> None:
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
            or (
                control_token is not None
                and (
                    type(control_token) is not str
                    or not control_token.isascii()
                    or not control_token
                    or any(character.isspace() for character in control_token)
                )
            )
            or (
                backend_probe_control_token is not None
                and (
                    type(backend_probe_control_token) is not str
                    or not backend_probe_control_token.isascii()
                    or not backend_probe_control_token
                    or any(
                        character.isspace() for character in backend_probe_control_token
                    )
                )
            )
            or (
                health_observation_control_token is not None
                and (
                    type(health_observation_control_token) is not str
                    or not health_observation_control_token.isascii()
                    or not health_observation_control_token
                    or any(
                        character.isspace()
                        for character in health_observation_control_token
                    )
                )
            )
            or (
                registry_ingress_control_token is not None
                and (
                    type(registry_ingress_control_token) is not str
                    or not registry_ingress_control_token.isascii()
                    or not registry_ingress_control_token
                    or any(
                        character.isspace()
                        for character in registry_ingress_control_token
                    )
                )
            )
            or (
                routing_control_token is not None
                and (
                    type(routing_control_token) is not str
                    or not routing_control_token.isascii()
                    or not routing_control_token
                    or any(character.isspace() for character in routing_control_token)
                )
            )
            or (
                control_token is not None
                and backend_probe_control_token is not None
                and hmac.compare_digest(control_token, backend_probe_control_token)
            )
            or (
                control_token is not None
                and health_observation_control_token is not None
                and hmac.compare_digest(control_token, health_observation_control_token)
            )
            or (
                backend_probe_control_token is not None
                and health_observation_control_token is not None
                and hmac.compare_digest(
                    backend_probe_control_token, health_observation_control_token
                )
            )
            or (
                control_token is not None
                and registry_ingress_control_token is not None
                and hmac.compare_digest(control_token, registry_ingress_control_token)
            )
            or (
                backend_probe_control_token is not None
                and registry_ingress_control_token is not None
                and hmac.compare_digest(
                    backend_probe_control_token, registry_ingress_control_token
                )
            )
            or (
                health_observation_control_token is not None
                and registry_ingress_control_token is not None
                and hmac.compare_digest(
                    health_observation_control_token, registry_ingress_control_token
                )
            )
            or (
                control_token is not None
                and routing_control_token is not None
                and hmac.compare_digest(control_token, routing_control_token)
            )
            or (
                backend_probe_control_token is not None
                and routing_control_token is not None
                and hmac.compare_digest(
                    backend_probe_control_token, routing_control_token
                )
            )
            or (
                health_observation_control_token is not None
                and routing_control_token is not None
                and hmac.compare_digest(
                    health_observation_control_token, routing_control_token
                )
            )
            or (
                registry_ingress_control_token is not None
                and routing_control_token is not None
                and hmac.compare_digest(
                    registry_ingress_control_token, routing_control_token
                )
            )
        ):
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
            if len(response_body) > self._response_limit(path):
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
        content_length = 0
        if "content-length" in headers:
            raw_length = headers["content-length"]
            if not raw_length.isascii() or not raw_length.isdigit():
                raise _BadRequest()
            significant_length = raw_length.lstrip("0") or "0"
            if len(significant_length) > len(str(request_limit)):
                raise _PayloadTooLarge()
            content_length = int(significant_length)
        elif method == "POST":
            raise _BadRequest()
        if content_length > request_limit:
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
                    timeout=self.read_timeout_seconds,
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
            probe_configured = (
                self.backend_probe_control_token is not None
                and self.backend_probe_manager is not None
            )
            observation_configured = (
                self.health_observation_control_token is not None
                and self.health_observation_manager is not None
            )
            registry_configured = (
                self.registry_ingress_control_token is not None
                and self.registry_ingress_manager is not None
            )
            routing_configured = (
                self.routing_control_token is not None
                and self.routing_manager is not None
            )
            if (
                not probe_configured
                and not observation_configured
                and not registry_configured
                and not routing_configured
            ):
                # Preserve the pipeline-only readiness phase and response.
                pipeline_ready = (
                    await self.data.readiness() if pipeline_configured else False
                )
                probe_ready = False
                observation_ready = False
                registry_ready = False
                routing_ready = False
            else:
                pipeline_ready = False
                if pipeline_configured:
                    try:
                        pipeline_ready = await self.data.readiness()
                    except Exception:
                        pipeline_ready = False
                try:
                    probe_ready = (
                        await self.backend_probe_manager.readiness()
                        if probe_configured
                        else False
                    )
                except Exception:
                    probe_ready = False
                try:
                    observation_ready = (
                        await self.health_observation_manager.readiness()
                        if observation_configured
                        else False
                    )
                except Exception:
                    observation_ready = False
                try:
                    registry_ready = (
                        await self.registry_ingress_manager.readiness()
                        if registry_configured
                        else False
                    )
                except Exception:
                    registry_ready = False
                try:
                    routing_ready = (
                        await self.routing_manager.readiness()
                        if routing_configured
                        else False
                    )
                except Exception:
                    routing_ready = False
            ready = (
                pipeline_ready
                or probe_ready
                or observation_ready
                or registry_ready
                or routing_ready
            )
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
        if method == "POST" and path == _ROUTING_RETIRE_PATH:
            return await self._dispatch_scope_retirement(headers, body)
        if method == "POST" and path == _ROUTING_RETIREMENT_STATUS_PATH:
            return await self._dispatch_scope_retirement_status(headers, body)
        if method == "POST" and path == VIDEO_EXECUTION_STATUS_PATH:
            return await self._dispatch_video_execution_status(headers, body)
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

    def _request_limit(self, path: str) -> int:
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
        }:
            return ROUTING_REQUEST_MAX_BYTES
        return self.max_request_bytes

    def _response_limit(self, path: str) -> int:
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
