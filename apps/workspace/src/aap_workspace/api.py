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
from .owner_transfer import OwnerTransferManager
from .owner_transfer_contract import (
    OwnerTransferInvalidRequest,
    OwnerTransferUnavailable,
    decode_owner_transfer_request,
)
from .owner_transfer_postgres import OwnerTransferPostgresData


def create_app(
    settings: RuntimeSettings | None = None, *, data=None, owner_transfer_data=None
) -> FastAPI:
    settings = settings or RuntimeSettings()
    control = settings.control_token().encode("utf-8")
    transfer_dsn = settings.transfer_database_url()
    if owner_transfer_data is None and transfer_dsn:
        owner_transfer_data = OwnerTransferPostgresData(
            transfer_dsn, command_timeout=settings.timeout_seconds
        )
    if data is None and (
        settings.membership_database_url
        or settings.membership_database_url_file
        or owner_transfer_data is None
    ):
        data = MembershipPostgresData(
            settings.database_url(), command_timeout=settings.timeout_seconds
        )
    gate = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(
        title="Workspace owner",
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
                    if data is not None:
                        await data.membership_policy()
                    if owner_transfer_data is not None:
                        await owner_transfer_data.owner_transfer_policy()
            return {"status": "ready"}
        except (MembershipUnavailable, OwnerTransferUnavailable, TimeoutError):
            return JSONResponse({"detail": "Workspace unavailable"}, status_code=503)

    def register_command(path, decoder, manager, binding, invalid, unavailable, label):
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
                command = decoder(await request.body())
            except (
                invalid,
                unavailable,
                ValueError,
                TypeError,
                UnicodeError,
                RecursionError,
            ):
                return JSONResponse(
                    {"detail": f"Invalid Workspace {label} request"}, status_code=400
                )
            try:
                if binding is None:
                    raise unavailable()
                async with asyncio.timeout(settings.timeout_seconds):
                    async with gate:
                        reply = await manager(binding).execute(command)
                return JSONResponse(reply.model_dump(mode="json", by_alias=True))
            except (unavailable, TimeoutError):
                return JSONResponse(
                    {"detail": f"Workspace {label} unavailable"}, status_code=503
                )

        app.add_api_route(path, execute, methods=["POST"])

    register_command(
        "/v1/workspace/membership/execute",
        decode_membership_request,
        MembershipManager,
        data,
        MembershipInvalidRequest,
        MembershipUnavailable,
        "membership",
    )
    register_command(
        "/v1/workspace/owner-transfer/execute",
        decode_owner_transfer_request,
        OwnerTransferManager,
        owner_transfer_data,
        OwnerTransferInvalidRequest,
        OwnerTransferUnavailable,
        "owner transfer",
    )
    return app
