"""Small internal HTTP surface for independent Assets source operations."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import SourceConfig, load_config
from aap_assets_source.contract import (
    SourceDeletionRequest,
    SourceFailure,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadOutcome,
    decode_source_reply,
)
from .runtime import SourceRuntime

logger = logging.getLogger("aap_assets_source_runtime")
_SOURCE_SCHEMA = "aap-assets-source/v1"


class BoundedHttpBodyMiddleware:
    """Bound internal JSON request/response bodies before they reach callers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        max_response_bytes: int,
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (
                value.decode("ascii", errors="ignore")
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ),
            None,
        )
        if content_length is not None and content_length.isdecimal():
            if int(content_length) > self.max_request_bytes:
                await self._request_too_large(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_bytes:
                await self._request_too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_body() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        response_start: Message | None = None
        response_body = bytearray()
        response_too_large = False

        async def capture_response(message: Message) -> None:
            nonlocal response_start, response_too_large
            if message["type"] == "http.response.start":
                response_start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunk = message.get("body", b"")
            if len(response_body) + len(chunk) > self.max_response_bytes:
                response_too_large = True
                response_body.clear()
            elif not response_too_large:
                response_body.extend(chunk)
            if message.get("more_body", False):
                return
            if response_too_large:
                await self._response_too_large(scope, receive, send)
                return
            if response_start is None:
                raise RuntimeError("HTTP response body arrived before response start")
            headers = [
                (name, value)
                for name, value in response_start.get("headers", [])
                if name.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(response_body)).encode("ascii")))
            await send({**response_start, "headers": headers})
            await send({"type": "http.response.body", "body": bytes(response_body)})

        try:
            await self.app(scope, replay_body, capture_response)
        except Exception as exc:  # noqa: BLE001 - hide internal failures from gateway
            logger.error("Assets source request failed (%s)", type(exc).__name__)
            await self._internal_failure(scope, receive, send)

    @staticmethod
    async def _request_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Assets source request is too large"},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _response_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Assets source response is too large"},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _internal_failure(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Assets source unavailable"},
        )
        await response(scope, receive, send)


def _failure(code: str) -> dict[str, Any]:
    return {"schema": _SOURCE_SCHEMA, "ok": False, "error": {"code": code}}


def _failure_response(code: str) -> JSONResponse:
    payload = _failure(code)
    # Keep even validation failures on the same strict source wire contract.
    try:
        decode_source_reply(payload, UploadOutcome)
    except SourceFailure as exc:
        if exc.code != code:
            raise RuntimeError("Invalid source failure response") from None
    else:
        raise RuntimeError("Source failure response decoded as success")
    return JSONResponse(status_code=200, content=payload)


async def _require_gateway(request: Request) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    expected = request.app.state.config.service_token
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not token
        or not token.isascii()
        or not hmac.compare_digest(token, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(
    *,
    config: SourceConfig | None = None,
    runtime: SourceRuntime | None = None,
) -> FastAPI:
    actual_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = actual_config
        owns_runtime = runtime is None
        try:
            app.state.runtime = runtime or await SourceRuntime.start(actual_config)
        except Exception as exc:
            logger.error("Assets source startup failed (%s)", type(exc).__name__)
            raise RuntimeError("Assets source startup failed") from None
        try:
            yield
        finally:
            if owns_runtime:
                await app.state.runtime.close()

    app = FastAPI(
        title="Assets Source Internal API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        BoundedHttpBodyMiddleware,
        max_request_bytes=actual_config.max_request_bytes,
        max_response_bytes=actual_config.max_response_bytes,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _failure_response("invalid_request")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        try:
            await request.app.state.runtime.ready()
        except Exception as exc:
            logger.error("Assets source readiness failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Assets source is not ready",
            ) from None
        return {"status": "ready"}

    @app.post("/v1/source/upload-init", dependencies=[Depends(_require_gateway)])
    async def upload_init(request: Request, body: UploadInitRequest) -> Response:
        payload = await request.app.state.runtime.init_upload(body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/source/upload-complete", dependencies=[Depends(_require_gateway)])
    async def upload_complete(
        request: Request, body: UploadCompleteRequest
    ) -> Response:
        payload = await request.app.state.runtime.complete_upload(body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/source/delete", dependencies=[Depends(_require_gateway)])
    async def delete_source(request: Request, body: SourceDeletionRequest) -> Response:
        payload = await request.app.state.runtime.delete_source(body)
        return Response(content=payload, media_type="application/json")

    return app
