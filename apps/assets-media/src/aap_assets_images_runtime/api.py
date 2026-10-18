"""Install the private image-control routes onto the shared media FastAPI app."""

from __future__ import annotations

import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel

from aap_assets_images import contract as c
from aap_assets_images.transport import (
    ImageAssetReferences,
    ImageAssetReferencesRequest,
    ImageGenerationReference,
    ImageGenerationReferenceRequest,
    ImageSourceReference,
    ImageSourceReferences,
    ImageSourceReferencesRequest,
    InternalImageSourceReferenceRequest,
    require_asset_reference_scope,
    require_generation_reference_scope,
    require_internal_source_reference_scope,
    require_source_reference_scope,
    require_source_references_scope,
)


def _reply(data=None, *, error: str | None = None, status: int = 200) -> Response:
    body = {
        "schema": c.IMAGE_SCHEMA,
        "ok": error is None,
        **({"error": {"code": error}} if error else {"data": data}),
    }
    try:
        raw = c.json_bytes(body)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        body = {
            "schema": c.IMAGE_SCHEMA,
            "ok": False,
            "error": {"code": "invalid_response"},
        }
        raw = c.json_bytes(body)
        status = 502
    if len(raw) > c.REPLY_BYTES:
        body = {
            "schema": c.IMAGE_SCHEMA,
            "ok": False,
            "error": {"code": "resource_budget_exceeded"},
        }
        raw = c.json_bytes(body)
        status = 413
    return Response(raw, status_code=status, media_type="application/json")


def install_image_routes(app: FastAPI, control, token: str) -> FastAPI:
    """Add image routes to the existing service app; do not create a second app."""
    if (
        len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise ValueError("image control requires a 32+ character ASCII token")

    async def invoke(
        request: Request,
        model: type[BaseModel],
        callback,
        result_type: type[BaseModel],
        scope_check=None,
    ):
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + token).encode("ascii")
        ):
            return _reply(error="permission_denied", status=401)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > c.REQUEST_BYTES:
                    return _reply(error="resource_budget_exceeded", status=413)
                body.extend(chunk)

            def reject_constant(_value):
                raise ValueError("nonfinite JSON")

            value = model.model_validate(
                json.loads(body, parse_constant=reject_constant)
            )
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return _reply(error="invalid_request", status=422)
        except Exception:
            return _reply(error="invalid_request", status=422)
        try:
            result = await callback(value)
            if not isinstance(result, result_type):
                raise c.ImageFailure("invalid_response")
            if scope_check is not None:
                try:
                    scope_check(value, result)
                except (ValueError, TypeError, AttributeError):
                    raise c.ImageFailure("invalid_response") from None
            return _reply(result)
        except c.ImageFailure as exc:
            return _reply(error=exc.code, status=c.http_error_status(exc.code))
        except Exception:
            return _reply(error="storage_unavailable", status=503)

    @app.post("/v1/images/commands/{operation}")
    async def command(operation: str, request: Request):
        definition = c.OPERATIONS.get(operation)
        if definition is None or definition.role in {"worker", "cleanup"}:
            return _reply(error="permission_denied", status=403)
        return await invoke(
            request,
            definition.request,
            lambda value: control.call(operation, value),
            definition.result,
            c.require_scope,
        )

    @app.post("/v1/images/source-reference")
    async def source_reference(request: Request):
        return await invoke(
            request,
            c.ReadImageSourceRequest,
            control.source_reference,
            ImageSourceReference,
            require_source_reference_scope,
        )

    @app.post("/v1/images/raw-source-reference")
    async def raw_source_reference(request: Request):
        return await invoke(
            request,
            c.ReadImageSourceRequest,
            control.raw_source_reference,
            ImageSourceReference,
            lambda req, result: require_source_reference_scope(
                req, result, raw_only=True
            ),
        )

    @app.post("/v1/images/source-references")
    async def source_references(request: Request):
        return await invoke(
            request,
            ImageSourceReferencesRequest,
            control.source_references,
            ImageSourceReferences,
            require_source_references_scope,
        )

    @app.post("/v1/images/internal-source-reference")
    async def internal_source_reference(request: Request):
        return await invoke(
            request,
            InternalImageSourceReferenceRequest,
            control.internal_source_reference,
            ImageSourceReference,
            require_internal_source_reference_scope,
        )

    @app.post("/v1/images/generation-reference")
    async def generation_reference(request: Request):
        return await invoke(
            request,
            ImageGenerationReferenceRequest,
            control.generation_reference,
            ImageGenerationReference,
            require_generation_reference_scope,
        )

    @app.post("/v1/images/asset-references")
    async def asset_references(request: Request):
        return await invoke(
            request,
            ImageAssetReferencesRequest,
            control.asset_references,
            ImageAssetReferences,
            require_asset_reference_scope,
        )

    return app
