"""Portable private HTTP contract for retained video-tracker inputs."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel

from . import tracker as t

CONTROL_SCHEMA = t.CONTROL_SCHEMA
HTTP_PREFIX = t.HTTP_PREFIX


class TrackerControlError(t.Value):
    code: t.TrackerErrorCode


# Route values stay narrower than the native matrix: no work facts or lease calls.
HTTP_OPERATIONS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "input": (t.TrackerInputReadRequest, t.TrackerVideoInputStatus),
    "prepare": (t.TrackerInputRequest, t.TrackerVideoInputStatus),
    "reference": (t.TrackerSourceReferenceRequest, t.TrackerVideoSourceReference),
}


def json_bytes(value: Any) -> bytes:
    def default(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", by_alias=True)
        raise TypeError("not a tracker control JSON value")

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=default,
    ).encode("utf-8")


def _reject_constant(_value: str):
    raise ValueError("nonfinite JSON")


def require_scope(operation: str, request: BaseModel, result: BaseModel) -> None:
    if operation == "input" and isinstance(request, t.TrackerInputReadRequest):
        if not isinstance(result, t.TrackerVideoInputStatus) or (
            result.tracker_job_id != request.tracker_job_id
            or result.task_id != request.task_id
        ):
            raise ValueError("tracker status escaped its requested Job")
        return
    if operation == "prepare" and isinstance(request, t.TrackerInputRequest):
        if not isinstance(result, t.TrackerVideoInputStatus) or (
            result.tracker_job_id != request.tracker_job_id
            or result.input_id != request.input_id
            or result.task_id != request.task_id
            or result.request_digest != request.request_digest
        ):
            raise ValueError("tracker preparation escaped its accepted request")
        return
    if operation == "reference" and isinstance(
        request, t.TrackerSourceReferenceRequest
    ):
        if not isinstance(result, t.TrackerVideoSourceReference) or (
            result.input.tracker_job_id != request.tracker_job_id
            or result.input.input_id != request.input_id
            or result.input.task_id != request.task_id
            or result.input.request_digest != request.request_digest
            or result.input.plan_digest != request.plan_digest
        ):
            raise ValueError("tracker source reference escaped its accepted request")
        parsed = urlsplit(result.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(char.isspace() for char in result.url)
            or parse_qs(parsed.query).get("versionId")
            != [result.source_version.version_id]
        ):
            raise ValueError("tracker source URL is not bound to its exact version")
        return
    raise ValueError("unknown tracker HTTP operation")


def decode_reply(raw: Any, result_type: type[BaseModel]) -> BaseModel:
    """Decode a single bounded, versioned tracker-control response."""
    try:
        if isinstance(raw, (str, bytes)):
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            if len(data) > t.REPLY_BYTES:
                raise ValueError("oversized tracker HTTP reply")
            raw = json.loads(data, parse_constant=_reject_constant)
        elif len(json_bytes(raw)) > t.REPLY_BYTES:
            raise ValueError("oversized tracker HTTP reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != CONTROL_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid tracker HTTP envelope")
        if raw["ok"] is False:
            if set(raw) != {"schema", "ok", "error"}:
                raise ValueError("invalid tracker HTTP failure")
            raise t.TrackerFailure(
                TrackerControlError.model_validate(raw["error"]).code
            )
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid tracker HTTP success")
        return result_type.model_validate(raw["data"])
    except t.TrackerFailure:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise t.TrackerFailure("invalid_response") from None


def http_error_status(code: t.TrackerErrorCode) -> int:
    if code == "account_not_active":
        return 401
    if code in {"permission_denied", "task_not_visible", "invalid_account_role"}:
        return 403
    if code in {"input_missing", "source_missing"}:
        return 404
    if code in {"invalid_request", "not_video", "unsupported_media", "resource_limit"}:
        return 422
    if code == "resource_budget_exceeded":
        return 413
    if code == "invalid_response":
        return 502
    if code in {
        "storage_unavailable",
        "timeout",
        "versioning_required",
        "source_not_ready",
    }:
        return 503
    return 409


def _definitions(models: list[type[BaseModel]], ref_template: str):
    definitions: dict[str, Any] = {}
    for model in models:
        shape = model.model_json_schema(ref_template=ref_template)
        for name, definition in shape.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting tracker HTTP definition")
            definitions[name] = definition
        if model.__name__ in definitions and definitions[model.__name__] != shape:
            raise ValueError("conflicting tracker HTTP model")
        definitions[model.__name__] = shape
    return definitions


def contract_document() -> dict[str, Any]:
    models = [model for pair in HTTP_OPERATIONS.values() for model in pair]
    models.append(TrackerControlError)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONTROL_SCHEMA,
        "$defs": _definitions(models, "#/$defs/{model}"),
        "x-budgets": {
            "request_bytes": t.REQUEST_BYTES,
            "reply_bytes": t.REPLY_BYTES,
            "url_expiry_seconds": t.URL_EXPIRY_SECONDS,
        },
        "x-http": {
            operation: {
                "method": "POST",
                "path": HTTP_PREFIX + "/" + operation,
                "request": request.__name__,
                "response": result.__name__,
                "envelope": CONTROL_SCHEMA,
                "auth": "bearer service token",
            }
            for operation, (request, result) in HTTP_OPERATIONS.items()
        },
    }


def http_document() -> dict[str, Any]:
    models = [model for pair in HTTP_OPERATIONS.values() for model in pair]
    models.append(TrackerControlError)
    definitions = _definitions(models, "#/components/schemas/{model}")
    paths = {}
    for operation, (request, result) in HTTP_OPERATIONS.items():
        success = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "ok", "data"],
            "properties": {
                "schema": {"const": CONTROL_SCHEMA},
                "ok": {"const": True},
                "data": {"$ref": "#/components/schemas/" + result.__name__},
            },
        }
        failure = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "ok", "error"],
            "properties": {
                "schema": {"const": CONTROL_SCHEMA},
                "ok": {"const": False},
                "error": {"$ref": "#/components/schemas/TrackerControlError"},
            },
        }
        paths[HTTP_PREFIX + "/" + operation] = {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/" + request.__name__
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Tracker source control result",
                        "content": {"application/json": {"schema": success}},
                    },
                    "default": {
                        "description": "Tracker source control failure",
                        "content": {"application/json": {"schema": failure}},
                    },
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Retained video-tracker input control", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-control-schema": CONTROL_SCHEMA,
        "x-budgets": contract_document()["x-budgets"],
    }
