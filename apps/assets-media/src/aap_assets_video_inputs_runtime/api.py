"""Install authenticated private HTTP routes for retained video inputs."""

from __future__ import annotations

import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import Response

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import http_contract as h
from aap_assets_video_inputs import values as v


def _reply(data=None, *, error: str | None = None, status: int = 200) -> Response:
    payload = {
        "schema": h.CONTROL_SCHEMA,
        "ok": error is None,
        **({"error": {"code": error}} if error else {"data": data}),
    }
    try:
        raw = h.json_bytes(payload)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raw = h.json_bytes(
            {
                "schema": h.CONTROL_SCHEMA,
                "ok": False,
                "error": {"code": "invalid_response"},
            }
        )
        status = 502
    if len(raw) > v.REPLY_BYTES:
        raw = h.json_bytes(
            {
                "schema": h.CONTROL_SCHEMA,
                "ok": False,
                "error": {"code": "resource_budget_exceeded"},
            }
        )
        status = 413
    return Response(raw, status_code=status, media_type="application/json")


def install_video_input_routes(app: FastAPI, control, token: str) -> FastAPI:
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("video input control requires a 32+ character ASCII token")

    async def invoke(operation: str, request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + token).encode("ascii")
        ):
            return _reply(error="permission_denied", status=401)
        request_type, result_type = h.HTTP_OPERATIONS[operation]
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > v.REQUEST_BYTES:
                    return _reply(error="resource_budget_exceeded", status=413)

                body.extend(chunk)

            value = request_type.model_validate(
                json.loads(body, parse_constant=h._reject_constant)
            )
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return _reply(error="invalid_request", status=422)

        try:
            callback = getattr(control, operation.replace("-", "_"))
            result = await callback(value)
            if not isinstance(result, result_type):
                raise c.VideoInputFailure("invalid_response")
            try:
                h.require_scope(operation, value, result)
            except (ValueError, TypeError, AttributeError):
                raise c.VideoInputFailure("invalid_response") from None
            return _reply(result)
        except c.VideoInputFailure as exc:
            return _reply(error=exc.code, status=h.http_error_status(exc.code))
        except (ValueError, TypeError, AttributeError, RecursionError):
            return _reply(error="invalid_response", status=502)
        except Exception:
            return _reply(error="storage_unavailable", status=503)

    for operation in h.HTTP_OPERATIONS:

        def route(operation_name: str):
            async def endpoint(request: Request):
                return await invoke(operation_name, request)

            return endpoint

        app.add_api_route(
            f"{h.HTTP_PREFIX}/{operation}",
            route(operation),
            methods=["POST"],
            name="video_input_" + operation.replace("-", "_"),
        )
    return app
