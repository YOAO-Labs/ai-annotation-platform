"""Authenticated bounded HTTP facade for the independent Labeling Mask worker."""

from __future__ import annotations

import asyncio
import hmac
import inspect
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel

from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc.coordinator import run_cycle

from .database import MaskGcDatabase

_HTTP_ERROR_CODES = frozenset((*c.ErrorCode.__args__, "permission_denied"))


def _status_for(code: str) -> int:
    return {
        "invalid_request": 422,
        "invalid_response": 502,
        "content_deleting": 409,
        "content_epoch_changed": 409,
        "lease_lost": 409,
        "cursor_conflict": 409,
        "idempotency_conflict": 409,
        "storage_unavailable": 503,
        "permission_denied": 401,
    }.get(code, 502)


def _reply(
    data: BaseModel | dict | None = None, *, error: str | None = None, status: int = 200
):
    if error is None:
        payload = {
            "schema": c.SCHEMA,
            "ok": True,
            "data": data.model_dump(mode="json", by_alias=True)
            if isinstance(data, BaseModel)
            else data,
        }
    else:
        safe_code = error if error in _HTTP_ERROR_CODES else "invalid_response"
        payload = {
            "schema": c.SCHEMA,
            "ok": False,
            "error": {"code": safe_code},
        }
    try:
        raw = c.json_bytes(payload)
    except (ValueError, TypeError, RecursionError):
        raw = c.json_bytes(
            {
                "schema": c.SCHEMA,
                "ok": False,
                "error": {"code": "invalid_response"},
            }
        )
        status = 502
    if len(raw) > c.MAX_RESPONSE_BYTES:
        raw = c.json_bytes(
            {
                "schema": c.SCHEMA,
                "ok": False,
                "error": {"code": "invalid_response"},
            }
        )
        status = 502
    return Response(raw, status_code=status, media_type="application/json")


async def _call_data(data: MaskGcDatabase, operation: str, request: BaseModel):
    result = data.call(operation, request)
    return await result if inspect.isawaitable(result) else result


async def _check_storage(storage) -> None:
    result = await asyncio.to_thread(storage.check_ready)
    if inspect.isawaitable(result):
        await result


def create_app(
    data: MaskGcDatabase,
    storage,
    token: str,
    *,
    worker_id: UUID | None = None,
    run_lock: asyncio.Lock | None = None,
) -> FastAPI:
    if (
        not isinstance(token, str)
        or len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise ValueError("Labeling Mask GC runtime requires a valid service token")
    process_worker_id = worker_id or uuid4()
    admission = run_lock or asyncio.Lock()
    app = FastAPI(
        title="AAP Labeling Mask GC",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return Response(b"ok", media_type="text/plain")

    @app.get("/health/ready", include_in_schema=False)
    async def ready():
        try:
            await _call_data(
                data, "read_mask_gc_progress", c.ReadMaskGcProgressRequest()
            )
            await _check_storage(storage)
        except Exception:
            return Response(b"unavailable", status_code=503, media_type="text/plain")
        return Response(b"ok", media_type="text/plain")

    @app.post("/v1/mask-gc/run")
    async def run(request: Request):
        if not _authorized(request, token):
            return _reply(error="permission_denied", status=401)
        if admission.locked():
            return _reply(error="storage_unavailable", status=503)
        if _json_content_type(request) is False:
            return _reply(error="invalid_request", status=415)
        async with admission:
            try:
                raw = await _read_bounded(request)
            except Exception:
                return _reply(error="invalid_request", status=400)
            if raw is None:
                return _reply(error="invalid_request", status=413)
            try:
                body = c.RunRequest.model_validate(c.parse_json(raw))
            except (ValueError, TypeError, RecursionError, UnicodeError):
                return _reply(error="invalid_request", status=422)
            try:
                result = await run_cycle(
                    data,
                    storage,
                    worker_id=process_worker_id,
                    scan_pages=body.scan_pages,
                    delete_limit=body.delete_limit,
                    dry_run=body.dry_run,
                    after_key=body.after_key,
                )
                c.validate_run_stats(body, result)
                return _reply(result)
            except c.MaskGcFailure as exc:
                return _reply(error=exc.code, status=_status_for(exc.code))
            except Exception:
                # Provider and database exceptions may contain credentials or keys.
                return _reply(error="storage_unavailable", status=503)

    @app.post("/v1/mask-gc/progress")
    async def progress(request: Request):
        if not _authorized(request, token):
            return _reply(error="permission_denied", status=401)
        if _json_content_type(request) is False:
            return _reply(error="invalid_request", status=415)
        try:
            raw = await _read_bounded(request)
        except Exception:
            return _reply(error="invalid_request", status=400)
        if raw is None:
            return _reply(error="invalid_request", status=413)
        try:
            c.ProgressRequest.model_validate(c.parse_json(raw))
            result = await _call_data(
                data, "read_mask_gc_progress", c.ReadMaskGcProgressRequest()
            )
            if not isinstance(result, c.ReadMaskGcProgressResult):
                result = c.ReadMaskGcProgressResult.model_validate(result)
            return _reply(result)
        except c.MaskGcFailure as exc:
            return _reply(error=exc.code, status=_status_for(exc.code))
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return _reply(error="invalid_request", status=422)
        except Exception:
            return _reply(error="storage_unavailable", status=503)

    return app


def _authorized(request: Request, token: str) -> bool:
    supplied = request.headers.get("authorization", "").encode("utf-8")
    expected = ("Bearer " + token).encode("ascii")
    return hmac.compare_digest(supplied, expected)


def _json_content_type(request: Request) -> bool:
    return (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "application/json"
    )


async def _read_bounded(request: Request) -> bytes | None:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > c.MAX_REQUEST_BYTES:
            return None
        raw.extend(chunk)
    return bytes(raw)


def openapi_document() -> dict[str, Any]:
    def schema(model: type[BaseModel]) -> dict:
        return model.model_json_schema(ref_template="#/components/schemas/{model}")

    request = schema(c.RunRequest)
    progress_result = schema(c.ReadMaskGcProgressResult)
    cycle_result = schema(c.CycleStats)
    definitions = {}
    for model_schema in (request, progress_result, cycle_result):
        definitions.update(model_schema.pop("$defs", {}))
        definitions[model_schema.get("title", "")] = model_schema
    error = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "error"],
        "properties": {
            "schema": {"const": c.SCHEMA},
            "ok": {"const": False},
            "error": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code"],
                "properties": {
                    "code": {
                        "enum": sorted(_HTTP_ERROR_CODES),
                        "type": "string",
                    }
                },
            },
        },
    }

    def success(result_name: str) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "ok", "data"],
            "properties": {
                "schema": {"const": c.SCHEMA},
                "ok": {"const": True},
                "data": {"$ref": f"#/components/schemas/{result_name}"},
            },
        }

    paths = {}
    for name, path_request, result_name in (
        ("run", "RunRequest", "CycleStats"),
        ("progress", "ProgressRequest", "ReadMaskGcProgressResult"),
    ):
        paths[f"/v1/mask-gc/{name}"] = {
            "post": {
                "operationId": "runMaskGcCycle"
                if name == "run"
                else "readMaskGcProgress",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{path_request}"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Labeling Mask GC result",
                        "content": {
                            "application/json": {"schema": success(result_name)}
                        },
                    },
                    "default": {
                        "description": "Closed Labeling Mask GC error",
                        "content": {"application/json": {"schema": error}},
                    },
                },
                "security": [{"serviceToken": []}],
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "AAP Labeling Mask GC", "version": "1"},
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                **definitions,
                "RunRequest": request,
                "ProgressRequest": {
                    "type": "object",
                    "additionalProperties": False,
                    "title": "ProgressRequest",
                },
                "CycleStats": cycle_result,
                "ReadMaskGcProgressResult": progress_result,
            },
        },
        "x-aap-schema": c.SCHEMA,
        "x-budgets": {
            "request_bytes": c.MAX_REQUEST_BYTES,
            "response_bytes": c.MAX_RESPONSE_BYTES,
            "scan_pages_per_cycle": c.MAX_SCAN_PAGES,
            "delete_claims_per_cycle": c.MAX_DELETE_LIMIT,
        },
    }
