"""Pure ASGI admission counter and authenticated backend routing controller."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hmac
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping
from uuid import UUID, uuid4

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from aap_protocol_v2.routing import (
    BackendRouteControlCommand,
    BackendRouteControlReply,
    MAX_ROUTE_CONTROL_BYTES,
    ROUTE_ADMISSION_HEADER,
    ROUTE_CONTROL_PATH,
    ROUTE_STATUS_PATH,
    RoutingProtocolError,
    decode_backend_route_control_command,
    decode_route_admission_token,
)

_TOKEN_ENV = "AAP_ROUTE_CONTROL_TOKEN"
_TOKEN_FILE_ENV = "AAP_ROUTE_CONTROL_TOKEN_FILE"
_LEGACY_ENV = "AAP_ROUTE_ALLOW_LEGACY_EXECUTION"
_REGISTRY_ENV = "AAP_ROUTE_REGISTRY_ID"
_MAX_SECRET_BYTES = 8192
_REPLAY_SKEW_SECONDS = 30
_JSON_HEADERS = [
    (b"content-type", b"application/json"),
    (b"cache-control", b"no-store"),
]
_READ_ONLY_GET_EXACT = {
    "/health",
    "/setup",
    "/versions",
    "/metrics",
    "/cache/stats",
    "/managed-evaluation/profile",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}
_MANAGEMENT_OPERATIONS = {
    "/unload": "control:unload",
    "/lifecycle/unload": "control:unload",
    "/reload": "control:reload",
}


class RouteConfigurationError(ValueError):
    """Invalid route-control environment; details never include secret values."""


class RouteAuthorizationError(Exception):
    pass


class RouteControlUnavailable(Exception):
    pass


class RouteControlConflict(Exception):
    pass


class StrongWorkCounter:
    """A synchronous counter whose tickets remain held through ASGI task completion."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def acquire(self) -> _WorkTicket:
        with self._lock:
            self._active += 1
        return _WorkTicket(self)

    def _release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("route work counter underflow")
            self._active -= 1


class _WorkTicket:
    def __init__(self, counter: StrongWorkCounter) -> None:
        self._counter = counter
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._counter._release()


class _RequestLease:
    def __init__(
        self,
        controller: BackendRoutingController,
        ticket: _WorkTicket,
        *,
        management: bool,
        attempt_id: str | None = None,
    ) -> None:
        self.controller = controller
        self.ticket = ticket
        self.management = management
        self.attempt_id = attempt_id
        self.lock = threading.Lock()
        self.pending_futures: set[asyncio.Future[Any]] = set()
        self.closing = False
        self.closed = False


_CURRENT_REQUEST: ContextVar[_RequestLease | None] = ContextVar(
    "aap_backend_route_request", default=None
)


class _AcceptedControl:
    def __init__(self, command: BackendRouteControlCommand) -> None:
        self.command_id = command.command_id
        self.registry_id = command.registry_id
        self.authority_epoch = command.authority_epoch
        self.retired_epoch = command.retired_epoch
        self.challenge = command.challenge
        self.phase = "draining"

    def matches(self, command: BackendRouteControlCommand) -> bool:
        return (
            self.command_id == command.command_id
            and self.registry_id == command.registry_id
            and self.authority_epoch == command.authority_epoch
            and self.retired_epoch == command.retired_epoch
            and self.challenge == command.challenge
        )


class BackendRoutingController:
    """Process-local enforcement for tokens and the authenticated drain/bind exchange."""

    def __init__(
        self,
        *,
        registry_id: str | UUID | None = None,
        control_token: bytes | str | None = None,
        allow_legacy_execution: bool = False,
    ) -> None:
        self.registry_id = _parse_optional_registry_id(registry_id)
        self._control_token = _parse_control_token(control_token)
        if type(allow_legacy_execution) is not bool:
            raise RouteConfigurationError("invalid route legacy execution setting")
        self.allow_legacy_execution = allow_legacy_execution
        self.backend_incarnation = str(uuid4())
        self._counter = StrongWorkCounter()
        self._control_lock = threading.RLock()
        self._accepted_control: _AcceptedControl | None = None
        self._signing_key: bytes | None = None
        self._authority_epoch: str | None = None
        self._used_attempts: dict[str, int] = {}
        self._active_attempts: set[str] = set()
        self._management_active = False

    @property
    def management_available(self) -> bool:
        return self._control_token is not None

    @property
    def active_requests(self) -> int:
        return self._counter.active

    @property
    def phase(self) -> str:
        if not self.management_available:
            return "legacy"
        if self._accepted_control is None:
            return "unbound"
        if self._accepted_control.phase == "draining" and self.active_requests == 0:
            return "drained"
        return self._accepted_control.phase

    def authenticate_management(self, authorization_values: list[bytes]) -> bool:
        expected = self._control_token
        if expected is None or len(authorization_values) != 1:
            return False
        value = authorization_values[0]
        prefix = b"bearer "
        if len(value) <= len(prefix) or value[: len(prefix)].lower() != prefix:
            return False
        candidate = value[len(prefix) :]
        if b" " in candidate or b"\t" in candidate:
            return False
        return hmac.compare_digest(candidate, expected)

    async def begin_request(
        self,
        method: str,
        path: str,
        admission_values: list[bytes],
    ) -> _RequestLease:
        with self._control_lock:
            if self._control_token is None:
                return _RequestLease(
                    self,
                    self._counter.acquire(),
                    management=False,
                )

            control = self._accepted_control
            if control is None:
                if self.allow_legacy_execution and not admission_values:
                    return _RequestLease(
                        self,
                        self._counter.acquire(),
                        management=False,
                    )
                raise RouteAuthorizationError
            now = int(time.time())
            self._prune_attempts(now)
            if control.phase != "bound":
                raise RouteAuthorizationError
            if len(admission_values) != 1:
                raise RouteAuthorizationError
            try:
                token = admission_values[0].decode("ascii")
                claims = decode_route_admission_token(token, self._signing_key or b"")
            except (UnicodeDecodeError, RoutingProtocolError):
                raise RouteAuthorizationError from None
            if (
                claims.registry_id != control.registry_id
                or claims.backend_incarnation != self.backend_incarnation
                or claims.authority_epoch != self._authority_epoch
                or claims.method != method
                or claims.path != path
            ):
                raise RouteAuthorizationError

            management_operation = _MANAGEMENT_OPERATIONS.get(path)
            if management_operation is not None:
                if claims.operation != management_operation or self._management_active:
                    raise RouteAuthorizationError
                if self._counter.active != 0:
                    raise RouteControlConflict
                management = True
            else:
                if claims.operation.startswith("control:") or self._management_active:
                    raise RouteAuthorizationError
                management = False

            if claims.attempt_id in self._used_attempts:
                raise RouteAuthorizationError
            self._used_attempts[claims.attempt_id] = (
                claims.expires_at + _REPLAY_SKEW_SECONDS
            )
            self._active_attempts.add(claims.attempt_id)
            if management:
                self._management_active = True
            return _RequestLease(
                self,
                self._counter.acquire(),
                management=management,
                attempt_id=claims.attempt_id,
            )

    def track_background_future(self, future: asyncio.Future[Any] | None) -> bool:
        """Keep the request lease open if tracked backend work outlives its handler."""
        lease = _CURRENT_REQUEST.get()
        if (
            future is None
            or future.done()
            or lease is None
            or lease.controller is not self
        ):
            return False
        with lease.lock:
            if lease.closed:
                ticket = self._counter.acquire()
                future.add_done_callback(lambda _done: ticket.release())
                return True
            lease.pending_futures.add(future)
        return True

    async def finish_request(self, lease: _RequestLease) -> None:
        with self._control_lock:
            with lease.lock:
                if lease.closed:
                    return
                lease.closing = True
        while True:
            with lease.lock:
                pending = [
                    future for future in lease.pending_futures if not future.done()
                ]
                if not pending:
                    lease.closed = True
                    break
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
        with self._control_lock:
            lease.ticket.release()
            if lease.attempt_id is not None:
                self._active_attempts.discard(lease.attempt_id)
            if lease.management:
                self._management_active = False

    async def apply_control(
        self, command: BackendRouteControlCommand
    ) -> BackendRouteControlReply:
        if self._control_token is None:
            raise RouteControlUnavailable
        with self._control_lock:
            if self.registry_id is not None and self.registry_id != command.registry_id:
                raise RouteControlConflict

            accepted = self._accepted_control
            if accepted is None:
                if command.action != "drain":
                    raise RouteControlConflict
                self.registry_id = command.registry_id
                accepted = _AcceptedControl(command)
                self._accepted_control = accepted
                self._signing_key = None
                self._authority_epoch = None
            elif accepted.matches(command):
                pass
            elif (
                command.action == "drain"
                and accepted.phase == "bound"
                and command.retired_epoch == self._authority_epoch
                and command.authority_epoch != self._authority_epoch
            ):
                accepted = _AcceptedControl(command)
                self._accepted_control = accepted
                self._signing_key = None
                self._authority_epoch = None
            else:
                raise RouteControlConflict

            if command.action == "drain":
                if accepted.phase == "draining" and self._counter.active == 0:
                    accepted.phase = "drained"
                return self._control_reply(accepted)

            if accepted.phase == "bound":
                if self._signing_key is None or command.signing_key is None:
                    raise RouteControlConflict
                if not hmac.compare_digest(self._signing_key, command.signing_key):
                    raise RouteControlConflict
                return self._control_reply(accepted)
            if (
                accepted.phase not in {"draining", "drained"}
                or self._counter.active
                or self._active_attempts
            ):
                raise RouteControlConflict
            if command.signing_key is None:
                raise RouteControlConflict
            accepted.phase = "bound"
            self._signing_key = command.signing_key
            self._authority_epoch = accepted.authority_epoch
            self._used_attempts.clear()
            return self._control_reply(accepted)

    async def control_status(self) -> BackendRouteControlReply:
        if self._control_token is None:
            raise RouteControlUnavailable
        with self._control_lock:
            accepted = self._accepted_control
            if accepted is None:
                raise RouteControlUnavailable
            if accepted.phase == "draining" and self._counter.active == 0:
                accepted.phase = "drained"
            return self._control_reply(accepted)

    def _control_reply(self, accepted: _AcceptedControl) -> BackendRouteControlReply:
        phase = accepted.phase
        if phase == "draining" and self._counter.active == 0:
            phase = "drained"
        return BackendRouteControlReply(
            command_id=accepted.command_id,
            registry_id=accepted.registry_id,
            authority_epoch=accepted.authority_epoch,
            backend_incarnation=self.backend_incarnation,
            challenge=accepted.challenge,
            phase=phase,
            active_requests=self._counter.active,
        )

    def _prune_attempts(self, now: int) -> None:
        for attempt_id, expires_at in list(self._used_attempts.items()):
            if expires_at <= now and attempt_id not in self._active_attempts:
                del self._used_attempts[attempt_id]


def _parse_optional_registry_id(value: str | UUID | None) -> str | None:
    if value is None or value == "":
        return None
    rendered = str(value)
    try:
        parsed = UUID(rendered)
    except (ValueError, TypeError, AttributeError):
        raise RouteConfigurationError("invalid route registry ID") from None
    if str(parsed) != rendered:
        raise RouteConfigurationError("invalid route registry ID")
    return rendered


def _parse_control_token(value: bytes | str | None) -> bytes | None:
    if value is None or value == b"" or value == "":
        return None
    try:
        encoded = value if isinstance(value, bytes) else value.encode("ascii")
    except UnicodeEncodeError:
        raise RouteConfigurationError("invalid route control token") from None
    if (
        not encoded
        or len(encoded) > _MAX_SECRET_BYTES
        or any(byte in b" \t\r\n\0" for byte in encoded)
    ):
        raise RouteConfigurationError("invalid route control token")
    return encoded


def _authorization_values(scope: Scope) -> list[bytes]:
    return [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"authorization"
    ]


def _admission_values(scope: Scope) -> list[bytes]:
    return [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == ROUTE_ADMISSION_HEADER.encode("ascii")
    ]


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


def _readonly_get(method: str, path: str) -> bool:
    if method != "GET":
        return False
    if path in _READ_ONLY_GET_EXACT:
        return True
    if path.startswith("/managed-models/") and path.count("/") == 2:
        return True
    return path.startswith("/managed-evaluation/requests/") and path.count("/") == 3


async def _send_json(send: Send, status: int, value: Mapping[str, Any]) -> None:
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    headers = [*_JSON_HEADERS, (b"content-length", str(len(body)).encode("ascii"))]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_control_body(receive: Receive, scope: Scope) -> bytes:
    content_lengths = _header_values(scope, b"content-length")
    if len(content_lengths) > 1:
        raise RoutingProtocolError("invalid control body headers")
    if content_lengths:
        raw_length = content_lengths[0]
        if not raw_length.isdigit() or len(raw_length) > 12:
            raise RoutingProtocolError("invalid control body length")
        if int(raw_length) > MAX_ROUTE_CONTROL_BYTES:
            raise RoutingProtocolError("routing control body is too large")
    encodings = _header_values(scope, b"content-encoding")
    if len(encodings) > 1 or (
        encodings and encodings[0].lower() not in {b"", b"identity"}
    ):
        raise RoutingProtocolError("compressed control bodies are not supported")
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise RoutingProtocolError("incomplete control body")
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > MAX_ROUTE_CONTROL_BYTES:
            raise RoutingProtocolError("routing control body is too large")
        if not message.get("more_body", False):
            break
    if content_lengths and int(content_lengths[0]) != len(body):
        raise RoutingProtocolError("control body length mismatch")
    return bytes(body)


async def _wait_for_task(task: asyncio.Task[Any]) -> bool:
    """Wait until a shielded child really ends, even if its caller is cancelled again."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    return cancelled


async def _finish_uncancelled(
    controller: BackendRoutingController,
    lease: _RequestLease,
) -> None:
    finish_task = asyncio.create_task(controller.finish_request(lease))
    cancelled = await _wait_for_task(finish_task)
    if finish_task.cancelled():
        raise asyncio.CancelledError
    failure = finish_task.exception()
    if failure is not None:
        raise failure
    if cancelled:
        raise asyncio.CancelledError


class BackendRoutingMiddleware:
    """Track and fence every backend mutation until its ASGI task has actually ended."""

    def __init__(self, app: ASGIApp, controller: BackendRoutingController) -> None:
        self.app = app
        self.controller = controller

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        if path in {ROUTE_CONTROL_PATH, ROUTE_STATUS_PATH}:
            await self._management_endpoint(method, path, scope, receive, send)
            return
        if _readonly_get(method, path):
            await self.app(scope, receive, send)
            return

        try:
            lease = await self.controller.begin_request(
                method,
                path,
                _admission_values(scope),
            )
        except RouteControlConflict:
            await _send_json(send, 409, {"detail": "route control is active"})
            return
        except RouteAuthorizationError:
            await _send_json(
                send,
                503 if self.controller.phase != "bound" else 401,
                {"detail": "route admission rejected"},
            )
            return

        app_task: asyncio.Task[Any] | None = None
        response_start = None
        pending_body = None

        async def start_response():
            nonlocal response_start
            if response_start is not None:
                await send(response_start)
                response_start = None

        async def tracked_send(message):
            nonlocal response_start, pending_body
            if message["type"] == "http.response.start":
                response_start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            body = message.get("body", b"")
            if body:
                if pending_body is not None:
                    await start_response()
                    await send({**pending_body, "more_body": True})
                pending_body = message
            elif not message.get("more_body", False):
                # Keep the last nonempty bytes even when the final ASGI message
                # is empty: Content-Length clients otherwise finish too early.
                pending_body = {**(pending_body or message), "more_body": False}

        context_token = _CURRENT_REQUEST.set(lease)
        try:
            app_task = asyncio.create_task(self.app(scope, receive, tracked_send))
            try:
                await asyncio.shield(app_task)
            except asyncio.CancelledError:
                if app_task.done():
                    raise
                # Keep the ticket until handlers, streams, and awaited background work stop.
                await _wait_for_task(app_task)
                raise
            # A complete HTTP response is used as execution settlement evidence.
            # Withhold its last bytes through actual background/future completion.
            await _finish_uncancelled(self.controller, lease)
            await start_response()
            if pending_body is not None:
                await send({**pending_body, "more_body": False})
        finally:
            _CURRENT_REQUEST.reset(context_token)
            if app_task is None or app_task.done():
                await _finish_uncancelled(self.controller, lease)
            else:
                # A cancellation must never publish an idle backend while its ASGI task runs.
                waiter = asyncio.create_task(_wait_for_task(app_task))
                await _finish_after_child(self.controller, lease, waiter)

    async def _management_endpoint(
        self,
        method: str,
        path: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not self.controller.management_available:
            await _send_json(send, 503, {"detail": "route management unavailable"})
            return
        if not self.controller.authenticate_management(_authorization_values(scope)):
            await _send_json(send, 401, {"detail": "route management unauthorized"})
            return
        if scope.get("query_string", b""):
            await _send_json(
                send, 400, {"detail": "route management query is not supported"}
            )
            return
        if path == ROUTE_STATUS_PATH:
            if method != "GET":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 405,
                        "headers": [(b"allow", b"GET"), *_JSON_HEADERS],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b"{}", "more_body": False}
                )
                return
            try:
                reply = await self.controller.control_status()
            except RouteControlUnavailable:
                await _send_json(send, 503, {"detail": "route control is unbound"})
                return
            await _send_json(send, 200, reply.as_dict())
            return
        if method != "POST":
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"allow", b"POST"), *_JSON_HEADERS],
                }
            )
            await send(
                {"type": "http.response.body", "body": b"{}", "more_body": False}
            )
            return
        try:
            raw_body = await _read_control_body(receive, scope)
            command = decode_backend_route_control_command(raw_body)
            reply = await self.controller.apply_control(command)
        except RoutingProtocolError:
            await _send_json(send, 400, {"detail": "invalid route control request"})
            return
        except RouteControlUnavailable:
            await _send_json(send, 503, {"detail": "route management unavailable"})
            return
        except RouteControlConflict:
            await _send_json(send, 409, {"detail": "route control conflict"})
            return
        await _send_json(send, 200, reply.as_dict())


async def _finish_after_child(
    controller: BackendRoutingController,
    lease: _RequestLease,
    child_waiter: asyncio.Task[Any],
) -> None:
    cancelled = await _wait_for_task(child_waiter)
    if child_waiter.cancelled():
        raise asyncio.CancelledError
    failure = child_waiter.exception()
    if failure is not None:
        raise failure
    await _finish_uncancelled(controller, lease)
    if cancelled:
        raise asyncio.CancelledError


def _load_control_token(environ: Mapping[str, str]) -> bytes | None:
    direct = environ.get(_TOKEN_ENV)
    file_path = environ.get(_TOKEN_FILE_ENV)
    has_direct = direct not in (None, "")
    has_file = file_path not in (None, "")
    if has_direct and has_file:
        raise RouteConfigurationError(
            "route control token sources are mutually exclusive"
        )
    if has_direct:
        return _parse_control_token(direct)
    if not has_file:
        return None
    try:
        raw = Path(file_path).read_bytes()
    except OSError:
        raise RouteConfigurationError(
            "route control token file is unavailable"
        ) from None
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    if len(raw) > _MAX_SECRET_BYTES:
        raise RouteConfigurationError("invalid route control token file")
    return _parse_control_token(raw)


def _load_allow_legacy(environ: Mapping[str, str]) -> bool:
    value = environ.get(_LEGACY_ENV)
    if value in (None, "0"):
        return False
    if value == "1":
        return True
    raise RouteConfigurationError("invalid route legacy execution setting")


def install_backend_routing_controller(
    app: FastAPI,
    *,
    registry_id: str | UUID | None = None,
    environ: Mapping[str, str] | None = None,
) -> BackendRoutingController:
    """Install route admission once, before FastAPI starts serving requests."""
    existing = getattr(app.state, "backend_routing_controller", None)
    if existing is not None:
        return existing
    source = os.environ if environ is None else environ
    configured_registry_id = (
        registry_id if registry_id is not None else source.get(_REGISTRY_ENV)
    )
    controller = BackendRoutingController(
        registry_id=configured_registry_id,
        control_token=_load_control_token(source),
        allow_legacy_execution=_load_allow_legacy(source),
    )
    if app.middleware_stack is not None:
        raise RouteConfigurationError(
            "route controller must be installed before serving"
        )
    app.add_middleware(BackendRoutingMiddleware, controller=controller)
    app.state.backend_routing_controller = controller
    return controller


__all__ = [
    "BackendRoutingController",
    "BackendRoutingMiddleware",
    "RouteConfigurationError",
    "StrongWorkCounter",
    "install_backend_routing_controller",
]
