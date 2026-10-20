"""Private Workspace HTTP assembly; public admission stays with the caller."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import RuntimeSettings
from .contract import (
    MembershipInvalidRequest,
    MembershipUnavailable,
    decode_membership_request,
)
from .manager import MembershipManager
from .postgres import MembershipPostgresData


def create_app(settings: RuntimeSettings | None = None, *, data=None) -> FastAPI:
    settings = settings or RuntimeSettings()
    control = settings.control_token().encode("utf-8")
    data = data or MembershipPostgresData(
        settings.database_url(), command_timeout=settings.timeout_seconds
    )
    gate = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(
        title="Workspace membership owner",
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
                    await data.membership_policy()
            return {"status": "ready"}
        except (MembershipUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Workspace membership unavailable"}, status_code=503
            )

    @app.post("/v1/workspace/membership/execute")
    async def execute(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(
            authorization[7:].encode("utf-8"), control
        ):
            return JSONResponse(
                {"detail": "Invalid internal credential"}, status_code=401
            )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                {"detail": "Expected application/json"}, status_code=400
            )
        try:
            command = decode_membership_request(await request.body())
        except (
            MembershipInvalidRequest,
            MembershipUnavailable,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            return JSONResponse(
                {"detail": "Invalid Workspace membership request"}, status_code=400
            )
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    reply = await MembershipManager(data).execute(command)
            return JSONResponse(reply.model_dump(mode="json", by_alias=True))
        except (MembershipUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Workspace membership unavailable"}, status_code=503
            )

    return app
