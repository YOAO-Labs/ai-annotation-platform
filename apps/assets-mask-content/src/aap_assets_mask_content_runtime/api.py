"""Authenticated bounded HTTP routes for Mask object content and cleanup."""

from __future__ import annotations

import asyncio
import hmac
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import Response

from aap_assets_mask_content import contract as c


class MaskContentStore(Protocol):
    def put(self, request: c.PutRequest) -> c.PutResult: ...

    def get(self, request: c.GetRequest) -> c.GetResult: ...

    def list(self, request: c.ListRequest) -> c.CandidatePage: ...

    def delete(self, request: c.DeleteRequest) -> c.DeleteResult: ...

    def check_ready(self) -> None: ...


_ALLOWED_OPERATIONS = {
    "content": frozenset({"put", "get"}),
    "cleanup": frozenset({"list", "delete"}),
}


def _reply(
    data=None, *, error: c.ErrorCode | None = None, status: int = 200
) -> Response:
    payload = c.error_envelope(error) if error is not None else c.success_envelope(data)
    try:
        raw = c.json_bytes(payload)
    except (ValueError, TypeError, RecursionError):
        raw = c.json_bytes(c.error_envelope("invalid_response"))
        status = 502
    if len(raw) > c.MAX_RESPONSE_BYTES:
        raw = c.json_bytes(c.error_envelope("resource_limit"))
        status = 413
    return Response(raw, status_code=status, media_type="application/json")


def _parse_request(raw: bytes, request_type):
    document = c.parse_json(raw)
    return request_type.model_validate(document)


def create_app(store: MaskContentStore, token: str, role: str) -> FastAPI:
    if (
        role not in _ALLOWED_OPERATIONS
        or len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise ValueError("Mask content runtime requires a valid role and token")

    app = FastAPI(
        title="AAP Mask content",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live")
    async def live():
        return Response(b"ok", media_type="text/plain")

    @app.get("/health/ready")
    async def ready():
        try:
            await asyncio.to_thread(store.check_ready)
        except c.MaskContentFailure:
            return Response(b"unavailable", status_code=503, media_type="text/plain")
        except Exception:
            return Response(b"unavailable", status_code=503, media_type="text/plain")
        return Response(b"ok", media_type="text/plain")

    @app.post("/v1/mask-content/{operation}")
    async def invoke(operation: str, request: Request):
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + token).encode("ascii")
        ):
            return _reply(error="permission_denied", status=401)
        if operation not in _ALLOWED_OPERATIONS[role]:
            return _reply(error="permission_denied", status=403)

        request_type = c.REQUEST_TYPES.get(operation)
        result_type = c.RESULT_TYPES.get(operation)
        if request_type is None or result_type is None:
            return _reply(error="permission_denied", status=403)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return _reply(error="invalid_request", status=415)

        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > c.MAX_REQUEST_BYTES:
                    return _reply(error="resource_limit", status=413)
                raw.extend(chunk)
            value = _parse_request(bytes(raw), request_type)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return _reply(error="invalid_request", status=422)

        try:
            callback = getattr(store, operation)
            result = await asyncio.to_thread(callback, value)
            if not isinstance(result, result_type):
                raise c.MaskContentFailure("invalid_response")
            return _reply(result)
        except c.MaskContentFailure as exc:
            return _reply(
                error=exc.code,
                status=c.http_error_status(exc.code),
            )
        except (ValueError, TypeError, AttributeError):
            return _reply(error="invalid_response", status=502)
        except Exception:
            # Storage/provider messages can contain keys or credentials.
            return _reply(error="storage_unavailable", status=503)

    return app
