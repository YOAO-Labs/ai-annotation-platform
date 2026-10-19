"""Install the four authenticated manifest control routes on Media API."""

from __future__ import annotations

import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel

from aap_assets_input_manifests import contract as c


def _reply(data=None, *, error: str | None = None, status: int = 200) -> Response:
    payload = {
        "schema": c.MANIFEST_SCHEMA,
        "ok": error is None,
        **({"error": {"code": error}} if error else {"data": data}),
    }
    try:
        raw = c.json_bytes(payload)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raw = c.json_bytes(
            {
                "schema": c.MANIFEST_SCHEMA,
                "ok": False,
                "error": {"code": "invalid_response"},
            }
        )
        status = 502
    if len(raw) > c.REPLY_BYTES:
        raw = c.json_bytes(
            {
                "schema": c.MANIFEST_SCHEMA,
                "ok": False,
                "error": {"code": "resource_budget_exceeded"},
            }
        )
        status = 413
    return Response(raw, status_code=status, media_type="application/json")


def install_manifest_routes(app: FastAPI, control, token: str) -> FastAPI:
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("manifest control requires a 32+ character ASCII token")

    async def invoke(request: Request, model: type[BaseModel], callback, result_type):
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
        try:
            result = await callback(value)
            if not isinstance(result, result_type):
                raise c.ManifestFailure("invalid_response")
            try:
                c.require_scope(value, result)
            except (ValueError, TypeError, AttributeError):
                raise c.ManifestFailure("invalid_response") from None
            return _reply(result)
        except c.ManifestFailure as exc:
            return _reply(error=exc.code, status=c.http_error_status(exc.code))
        except Exception:
            return _reply(error="storage_unavailable", status=503)

    @app.post("/v1/input-manifests/record")
    async def record(request: Request):
        return await invoke(request, c.ManifestScope, control.record, c.ManifestHeader)

    @app.post("/v1/input-manifests/page")
    async def page(request: Request):
        return await invoke(
            request, c.ManifestPageRequest, control.page, c.ManifestPage
        )

    @app.post("/v1/input-manifests/prepare")
    async def prepare(request: Request):
        return await invoke(
            request,
            c.PrepareManifestMemberRequest,
            control.prepare,
            c.ManifestMemberReceipt,
        )

    @app.post("/v1/input-manifests/seal")
    async def seal(request: Request):
        return await invoke(
            request, c.SealManifestRequest, control.seal, c.ManifestHeader
        )

    return app
