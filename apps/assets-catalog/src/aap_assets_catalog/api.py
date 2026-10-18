"""Private HTTP transport for the independent Assets catalog control plane."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .catalog import CatalogRuntime
from .config import CatalogConfig, load_config
from .contract import (
    CATALOG_SCHEMA,
    CatalogFailure,
    CreateDatasetRequest,
    DatasetMutationResult,
    DatasetPage,
    DatasetResult,
    ItemPage,
    GetDatasetRequest,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
)
from .associations_contract import (
    ASSOCIATIONS_SCHEMA,
    AssociationFailure,
    DatasetProjectsResult,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
    ProjectDatasetsResult,
)

logger = logging.getLogger("aap_assets_catalog")


class BoundedHttpBodyMiddleware:
    """Bound internal JSON request and response bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        max_response_bytes: int,
    ):
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
                await self.app(scope, receive, send)
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
        except Exception as exc:
            logger.error("Assets catalog request failed (%s)", type(exc).__name__)
            await self._internal_failure(scope, receive, send)

    @staticmethod
    async def _request_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Assets catalog request is too large"},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _response_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Assets catalog response is too large"},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _internal_failure(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Assets catalog unavailable"},
        )
        await response(scope, receive, send)


def _success(data: Any) -> dict[str, Any]:
    return {
        "schema": CATALOG_SCHEMA,
        "ok": True,
        "data": data.model_dump(mode="json"),
    }


def _failure(code: str) -> dict[str, Any]:
    return {
        "schema": CATALOG_SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


def _association_success(data: Any) -> dict[str, Any]:
    return {
        "schema": ASSOCIATIONS_SCHEMA,
        "ok": True,
        "data": data.model_dump(mode="json"),
    }


def _association_failure(code: str) -> dict[str, Any]:
    return {
        "schema": ASSOCIATIONS_SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


async def _require_gateway(request: Request) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    expected = request.app.state.config.gateway_token
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
    config: CatalogConfig | None = None,
    runtime: CatalogRuntime | None = None,
) -> FastAPI:
    actual_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = actual_config
        owned_runtime = runtime is None
        try:
            app.state.runtime = runtime or await CatalogRuntime.start(actual_config)
        except Exception as exc:
            logger.error("Assets catalog startup failed (%s)", type(exc).__name__)
            raise RuntimeError("Assets catalog startup failed") from None
        try:
            yield
        finally:
            if owned_runtime:
                await app.state.runtime.close()

    app = FastAPI(
        title="Assets Catalog Internal API",
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

    @app.exception_handler(CatalogFailure)
    async def catalog_failure_handler(
        _request: Request, exc: CatalogFailure
    ) -> JSONResponse:
        return JSONResponse(status_code=200, content=_failure(exc.code))

    @app.exception_handler(AssociationFailure)
    async def association_failure_handler(
        _request: Request, exc: AssociationFailure
    ) -> JSONResponse:
        return JSONResponse(status_code=200, content=_association_failure(exc.code))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid Assets catalog request"},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        try:
            await request.app.state.runtime.ready()
        except Exception as exc:
            logger.error("Assets catalog readiness failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Assets catalog is not ready",
            ) from None
        return {"status": "ready"}

    @app.post("/v1/catalog/list", dependencies=[Depends(_require_gateway)])
    async def list_datasets(request: Request, body: ListDatasetsRequest):
        result: DatasetPage = await request.app.state.runtime.catalog.list_datasets(
            body
        )
        return _success(result)

    @app.post("/v1/catalog/get", dependencies=[Depends(_require_gateway)])
    async def get_dataset(request: Request, body: GetDatasetRequest):
        result: DatasetResult = await request.app.state.runtime.catalog.get_dataset(
            body
        )
        return _success(result)

    @app.post("/v1/catalog/items", dependencies=[Depends(_require_gateway)])
    async def list_items(request: Request, body: ListItemsRequest):
        result: ItemPage = await request.app.state.runtime.catalog.list_items(body)
        return _success(result)

    @app.post("/v1/catalog/create", dependencies=[Depends(_require_gateway)])
    async def create_dataset(request: Request, body: CreateDatasetRequest):
        result: DatasetMutationResult = (
            await request.app.state.runtime.catalog.create_dataset(body)
        )
        return _success(result)

    @app.post("/v1/catalog/update", dependencies=[Depends(_require_gateway)])
    async def update_dataset(request: Request, body: UpdateDatasetRequest):
        result: DatasetMutationResult = (
            await request.app.state.runtime.catalog.update_dataset(body)
        )
        return _success(result)

    @app.post("/v1/catalog/dataset-projects", dependencies=[Depends(_require_gateway)])
    async def list_dataset_projects(request: Request, body: ListDatasetProjectsRequest):
        result: DatasetProjectsResult = (
            await request.app.state.runtime.catalog.list_dataset_projects(body)
        )
        return _association_success(result)

    @app.post("/v1/catalog/project-datasets", dependencies=[Depends(_require_gateway)])
    async def list_project_datasets(request: Request, body: ListProjectDatasetsRequest):
        result: ProjectDatasetsResult = (
            await request.app.state.runtime.catalog.list_project_datasets(body)
        )
        return _association_success(result)

    return app
