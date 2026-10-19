"""Install only the authenticated public input control operations."""

from __future__ import annotations

import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import Response

from aap_assets_inputs import contract as c
from aap_assets_inputs.client import HTTP_OPERATIONS


def _reply(data=None, *, error: str | None = None, status: int = 200):
    payload = {
        "schema": c.INPUT_SCHEMA,
        "ok": error is None,
        **({"error": {"code": error}} if error else {"data": data}),
    }
    try:
        raw = c.json_bytes(payload)
    except (ValueError, TypeError, RecursionError):
        return _reply(error="invalid_response", status=502)
    if len(raw) > c.REPLY_BYTES:
        return _reply(error="resource_budget_exceeded", status=413)
    return Response(raw, status_code=status, media_type="application/json")


def install_input_routes(app: FastAPI, control, token: str) -> FastAPI:
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("input control requires a 32+ character ASCII token")

    @app.post("/v1/inputs/{operation}")
    async def invoke(operation: str, request: Request):
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            header.encode("utf-8"), ("Bearer " + token).encode("ascii")
        ):
            return _reply(error="permission_denied", status=401)
        definition = HTTP_OPERATIONS.get(operation)
        if definition is None:
            return _reply(error="permission_denied", status=403)
        request_type, result_type = definition
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > c.REQUEST_BYTES:
                    return _reply(error="resource_budget_exceeded", status=413)
                raw.extend(chunk)

            def reject_constant(_):
                raise ValueError("nonfinite JSON")

            value = request_type.model_validate(
                json.loads(raw, parse_constant=reject_constant)
            )
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return _reply(error="invalid_request", status=422)
        try:
            callback = getattr(control, operation.replace("-", "_"))
            result = await callback(value)
            if not isinstance(result, result_type):
                raise c.InputFailure("invalid_response")
            c.require_scope(value, result)
            return _reply(result)
        except c.InputFailure as exc:
            return _reply(error=exc.code, status=c.http_error_status(exc.code))
        except (ValueError, TypeError, AttributeError):
            return _reply(error="invalid_response", status=502)
        except Exception:
            return _reply(error="storage_unavailable", status=503)

    return app
