"""Private Labeling projection; the trusted API peer holds public authorization."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .class_queries import LabelingClassQueryManager
from .class_query_contract import (
    LabelingClassQueryInvalidRequest,
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_request,
)
from .class_query_postgres import LabelingClassQueryPostgresData
from .config import RuntimeSettings

_REQUEST_LIMIT = 16_384


def create_app(settings: RuntimeSettings | None = None, *, data=None) -> FastAPI:
    settings = settings or RuntimeSettings()
    control = settings.read_control_token().encode("utf-8")
    if data is None:
        data = LabelingClassQueryPostgresData(
            settings.read_database_url(), command_timeout=settings.timeout_seconds
        )
    gate = asyncio.Semaphore(settings.max_concurrency)
    manager = LabelingClassQueryManager(data)
    app = FastAPI(
        title="Labeling class-query owner",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    await data.class_query_policy()
            return {"status": "ready"}
        except (LabelingClassQueryUnavailable, OSError, TimeoutError):
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/v1/labeling/class-queries/execute")
    async def execute(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(
            authorization[7:].encode("utf-8"), control
        ):
            return JSONResponse(
                {"detail": "Invalid Labeling class-query control credential"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _REQUEST_LIMIT:
                return JSONResponse({"detail": "Request is too large"}, status_code=413)
        try:
            payload = decode_labeling_class_query_request(bytes(body))
        except LabelingClassQueryInvalidRequest:
            return JSONResponse(
                {"detail": "Invalid class-query request"}, status_code=422
            )
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    reply = await manager.execute(payload)
            return JSONResponse(reply.model_dump(mode="json", by_alias=True))
        except (LabelingClassQueryUnavailable, OSError, TimeoutError):
            return JSONResponse(
                {"detail": "Class-query owner unavailable"}, status_code=503
            )

    return app
