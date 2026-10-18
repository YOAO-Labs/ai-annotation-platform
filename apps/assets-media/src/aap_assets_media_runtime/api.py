"""Small authenticated control HTTP surface; execution capabilities stay private."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

from aap_assets_media import contract as c
from aap_assets_media.transport import (
    MediaReferences,
    SourceReference,
    SourceReferences,
    SourceReferencesRequest,
    InternalSourceReferenceRequest,
)


def error_status(code: str) -> int:
    if code == "account_not_active":
        return 401
    if code in {"permission_denied", "task_not_visible", "invalid_account_role"}:
        return 403
    if code in {"source_missing", "asset_missing"}:
        return 404
    if code in {"invalid_request", "invalid_frame_range", "resource_budget_exceeded"}:
        return 422
    if code == "invalid_response":
        return 502
    if code in {"metadata_not_ready", "storage_unavailable", "object_missing"}:
        return 503
    return 409


def _reply(data=None, *, error: str | None = None, status: int = 200) -> Response:
    body = {
        "schema": c.MEDIA_SCHEMA,
        "ok": error is None,
        **({"error": {"code": error}} if error else {"data": data}),
    }
    raw = c.json_bytes(body)
    if len(raw) > c.REPLY_BYTES:
        return _reply(error="resource_budget_exceeded", status=422)
    return Response(raw, status_code=status, media_type="application/json")


def create_app(
    control,
    token: str,
    *,
    readiness: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    if (
        len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise ValueError("media control requires a 32+ character ASCII token")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        healthy = readiness is not None and await readiness()
        return Response(
            c.json_bytes({"status": "ok" if healthy else "unavailable"}),
            status_code=200 if healthy else 503,
            media_type="application/json",
        )

    async def invoke(request: Request, model, callback, result_type):
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + token).encode("ascii")
        ):
            return _reply(error="permission_denied", status=401)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > c.REQUEST_BYTES:
                    raise c.MediaFailure("resource_budget_exceeded")
                body.extend(chunk)

            def invalid_constant(_):
                raise ValueError("nonfinite JSON")

            value = model.model_validate(
                json.loads(body, parse_constant=invalid_constant)
            )
        except c.MediaFailure as exc:
            return _reply(error=exc.code, status=error_status(exc.code))
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return _reply(error="invalid_request", status=422)
        try:
            result = await callback(value)
            if not isinstance(result, result_type):
                raise c.MediaFailure("invalid_response")
            return _reply(result)
        except c.MediaFailure as exc:
            return _reply(error=exc.code, status=error_status(exc.code))

    @app.post("/v1/commands/{operation}")
    async def command(operation: str, request: Request):
        definition = c.OPERATIONS.get(operation)
        if definition is None or definition.role == "worker":
            return _reply(error="permission_denied", status=403)
        return await invoke(
            request,
            definition.request,
            lambda value: control.call(operation, value),
            definition.result,
        )

    @app.post("/v1/source-reference")
    async def source_reference(request: Request):
        return await invoke(
            request, c.ReadSourceRequest, control.source_reference, SourceReference
        )

    @app.post("/v1/raw-source-reference")
    async def raw_source_reference(request: Request):
        return await invoke(
            request, c.ReadSourceRequest, control.raw_source_reference, SourceReference
        )

    @app.post("/v1/internal-source-reference")
    async def internal_source_reference(request: Request):
        async def reference(value):
            return await control.internal_source_reference(
                value.source, expires_in=value.expires_in
            )

        return await invoke(
            request, InternalSourceReferenceRequest, reference, SourceReference
        )

    @app.post("/v1/source-references")
    async def source_references(request: Request):
        async def batch(value):
            return SourceReferences(
                references=await control.source_references(
                    value.requests, raw_only=value.raw_only
                )
            )

        return await invoke(request, SourceReferencesRequest, batch, SourceReferences)

    @app.post("/v1/assets-reference")
    async def assets_reference(request: Request):
        return await invoke(
            request, c.AssetsRequest, control.assets_reference, MediaReferences
        )

    @app.post("/v1/internal-frame-reference")
    async def internal_frame_reference(request: Request):
        return await invoke(
            request,
            c.InternalFrameRequest,
            control.internal_frame_reference,
            MediaReferences,
        )

    return app
