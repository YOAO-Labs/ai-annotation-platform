"""Small internal HTTP surface for independent Assets source operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
from tempfile import SpooledTemporaryFile
from typing import Any
from uuid import UUID

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
from aap_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    ZIP_MAX_BYTES,
    IngestCommand,
    IngestFailure,
    IngestReceipt,
    decode_ingest_reply,
)
from aap_assets_source.connections_contract import (
    CONNECTION_OPERATIONS,
    ConnectionFailure,
    ConnectionProbeReceipt,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsRequest,
    ReadConnectionRequest,
    TestConnectionRequest,
    UpdateConnectionRequest,
    decode_connection_reply,
)
from .runtime import SourceRuntime

logger = logging.getLogger("aap_assets_source_runtime")
_SOURCE_SCHEMA = "aap-assets-source/v1"
_ZIP_UPLOAD_PATH = "/v1/source/upload-zip"
_ZIP_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_CONNECTION_PATHS = frozenset(
    f"/v1/connections/{name}" for name in CONNECTION_OPERATIONS
)


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
        is_zip_upload = (
            scope.get("method") == "POST" and scope.get("path") == _ZIP_UPLOAD_PATH
        )
        zip_spool = None
        if is_zip_upload:
            request_state = scope.setdefault("state", {})
            request_state["_source_zip_spool"] = None
            request_state["_source_zip_too_large"] = False
            request_state["_source_zip_body_complete"] = False
            if content_length is not None and content_length.isdecimal():
                if int(content_length) > ZIP_MAX_BYTES:
                    await self._request_too_large(scope, receive, send)
                    return

            received_bytes = 0

            async def streaming_zip_body() -> Message:
                nonlocal received_bytes, zip_spool
                if request_state["_source_zip_body_complete"]:
                    return await receive()
                message = await receive()
                if message["type"] == "http.request":
                    chunk = message.get("body", b"")
                    if received_bytes + len(chunk) > ZIP_MAX_BYTES:
                        request_state["_source_zip_too_large"] = True
                        request_state["_source_zip_body_complete"] = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                    if zip_spool is None:
                        zip_spool = SpooledTemporaryFile(
                            max_size=_ZIP_SPOOL_MEMORY_BYTES, mode="w+b"
                        )
                        request_state["_source_zip_spool"] = zip_spool
                    received_bytes += len(chunk)
                    zip_spool.write(chunk)
                    if not message.get("more_body", False):
                        request_state["_source_zip_body_complete"] = True
                        zip_spool.seek(0)
                return message

            downstream_receive = streaming_zip_body
        else:
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
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            downstream_receive = replay_body

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
            await self.app(scope, downstream_receive, capture_response)
        except Exception as exc:  # noqa: BLE001 - hide internal failures from gateway
            logger.error("Assets source request failed (%s)", type(exc).__name__)
            await self._internal_failure(scope, receive, send)
        finally:
            if zip_spool is not None and not zip_spool.closed:
                zip_spool.close()

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


def _ingest_failure_response(code: str) -> JSONResponse:
    payload = {
        "schema": INGEST_SCHEMA,
        "ok": False,
        "error": {"code": code},
    }
    try:
        decode_ingest_reply(payload, IngestReceipt)
    except IngestFailure as exc:
        if exc.code != code:
            raise RuntimeError("Invalid ingest failure response") from None
    else:
        raise RuntimeError("Ingest failure response decoded as success")
    return JSONResponse(status_code=200, content=payload)


def _connection_failure_response(code: str) -> JSONResponse:
    payload = ConnectionFailure(code).payload()
    try:
        decode_connection_reply(payload, ConnectionProbeReceipt)
    except ConnectionFailure as exc:
        if exc.code != code:
            raise RuntimeError("Invalid connection failure response") from None
    else:
        raise RuntimeError("Connection failure response decoded as success")
    return JSONResponse(status_code=200, content=payload)


async def _run_until_disconnect(request: Request, operation):
    operation_task = asyncio.create_task(operation)
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _pending = await asyncio.wait(
            {operation_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)
            return operation_task.result()
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        return None
    except BaseException:
        operation_task.cancel()
        disconnect_task.cancel()
        await asyncio.gather(operation_task, disconnect_task, return_exceptions=True)
        raise


async def _wait_for_disconnect(request: Request) -> None:
    try:
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        return


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
    app.state.zip_upload_semaphore = asyncio.Semaphore(1)
    app.add_middleware(
        BoundedHttpBodyMiddleware,
        max_request_bytes=actual_config.max_request_bytes,
        max_response_bytes=actual_config.max_response_bytes,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path in _CONNECTION_PATHS:
            return _connection_failure_response("invalid_request")
        if request.url.path in {_ZIP_UPLOAD_PATH, "/v1/source/scan"}:
            return _ingest_failure_response("invalid_request")
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

    @app.post(_ZIP_UPLOAD_PATH, dependencies=[Depends(_require_gateway)])
    async def upload_zip(
        request: Request,
        operation_id: UUID,
        actor_id: UUID,
        dataset_id: UUID,
    ) -> Response:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
            "application/zip"
        ):
            return Response(
                content=_ingest_failure_response("invalid_request").body,
                media_type="application/json",
            )
        semaphore = request.app.state.zip_upload_semaphore
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=request.app.state.config.pool_timeout_seconds,
            )
        except TimeoutError:
            return Response(
                content=_ingest_failure_response("service_unavailable").body,
                media_type="application/json",
            )
        try:
            if request.state._source_zip_too_large:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Assets source request is too large"},
                )
            async for _chunk in request.stream():
                pass
            if request.state._source_zip_too_large:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Assets source request is too large"},
                )
            spool = request.state._source_zip_spool
            if spool is None:
                return _ingest_failure_response("invalid_request")
            spool.seek(0)
            command = IngestCommand(
                operation_id=operation_id,
                actor_id=actor_id,
                dataset_id=dataset_id,
            )
            payload = await _run_until_disconnect(
                request,
                request.app.state.runtime.upload_zip(command, spool),
            )
            if payload is None:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            return Response(content=payload, media_type="application/json")
        finally:
            semaphore.release()

    @app.post("/v1/source/scan", dependencies=[Depends(_require_gateway)])
    async def scan_source(request: Request, body: IngestCommand) -> Response:
        payload = await _run_until_disconnect(
            request,
            request.app.state.runtime.scan(body),
        )
        if payload is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/connections/list", dependencies=[Depends(_require_gateway)])
    async def list_connections(
        request: Request, body: ListConnectionsRequest
    ) -> Response:
        payload = await request.app.state.runtime.manage_connection("list", body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/connections/read", dependencies=[Depends(_require_gateway)])
    async def read_connection(
        request: Request, body: ReadConnectionRequest
    ) -> Response:
        payload = await request.app.state.runtime.manage_connection("read", body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/connections/create", dependencies=[Depends(_require_gateway)])
    async def create_connection(
        request: Request, body: CreateConnectionRequest
    ) -> Response:
        payload = await request.app.state.runtime.manage_connection("create", body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/connections/update", dependencies=[Depends(_require_gateway)])
    async def update_connection(
        request: Request, body: UpdateConnectionRequest
    ) -> Response:
        payload = await request.app.state.runtime.manage_connection("update", body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/connections/delete", dependencies=[Depends(_require_gateway)])
    async def delete_connection(
        request: Request, body: DeleteConnectionRequest
    ) -> Response:
        payload = await request.app.state.runtime.manage_connection("delete", body)
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/connections/test", dependencies=[Depends(_require_gateway)])
    async def test_connection(
        request: Request, body: TestConnectionRequest
    ) -> Response:
        payload = await request.app.state.runtime.manage_connection("test", body)
        return Response(content=payload, media_type="application/json")

    return app
