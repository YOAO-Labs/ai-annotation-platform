"""Portable private HTTP contract for retained video input control."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from . import execution as c
from . import values as v

CONTROL_SCHEMA = "aap-assets-video-input-control/v1"
HTTP_PREFIX = "/v1/video-inputs"
ErrorCode = c.ErrorCode


class VideoInputControlError(v.Value):
    code: ErrorCode


# Route names are wire values. Keep this allowlist separate from the larger
# native operation matrix so capture, worker, fencing and cleanup calls cannot
# become HTTP commands by accident.
HTTP_OPERATIONS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "manifest": (c.VideoManifestScope, c.VideoManifestHeader),
    "members": (c.VideoManifestPageRequest, c.VideoManifestPage),
    "prepare": (c.PrepareVideoMemberRequest, c.VideoMemberReceipt),
    "seal": (c.SealVideoManifestRequest, c.VideoManifestHeader),
    "input": (c.ReadVideoInputRequest, c.VideoInputRecord),
    "verify": (c.LockVideoInputRequest, c.VideoInputRecord),
    "times": (v.FramePageRequest, v.FrameTimePage),
    "require-frame": (c.VideoFrameRequest, c.VideoFrameRecord),
    "frame": (c.VideoFrameRequest, c.VideoFrameRecord),
    "reference": (c.VideoFrameRequest, v.FrameReference),
}


def json_bytes(value: Any) -> bytes:
    def default(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        raise TypeError("not a video control JSON value")

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=default,
    ).encode("utf-8")


def _reject_constant(_value: str):
    raise ValueError("nonfinite JSON")


def decode_reply(raw: Any, result_type: type[BaseModel]) -> BaseModel:
    """Decode one bounded control envelope without exposing response details."""

    try:
        if isinstance(raw, (str, bytes)):
            raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
            if len(raw_bytes) > v.REPLY_BYTES:
                raise ValueError("oversized video control reply")
            raw = json.loads(raw_bytes, parse_constant=_reject_constant)
        elif len(json_bytes(raw)) > v.REPLY_BYTES:
            raise ValueError("oversized video control reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != CONTROL_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid video control envelope")
        if raw["ok"] is False:
            if set(raw) != {"schema", "ok", "error"}:
                raise ValueError("invalid video control failure")
            raise c.VideoInputFailure(
                VideoInputControlError.model_validate(raw["error"]).code
            )
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid video control success")
        result = result_type.model_validate(raw["data"])
        header = (
            result.manifest
            if isinstance(result, (c.VideoManifestPage, c.VideoMemberReceipt))
            else result
        )
        if isinstance(header, c.VideoManifestHeader):
            original = raw["data"]["manifest"] if header is not result else raw["data"]
            if original.get("selection") != header.selection.model_dump(mode="json"):
                raise ValueError("video control selector is not canonical")
        return result
    except c.VideoInputFailure:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise c.VideoInputFailure("invalid_response") from None


def require_scope(operation: str, request: BaseModel, result: BaseModel) -> None:
    """Apply the existing native scopes to HTTP replies and add URL scope."""

    if isinstance(result, v.FrameReference):
        if not isinstance(request, c.VideoFrameRequest):
            raise ValueError("frame reference requires a frame request")
        video = result.plan.input
        if (
            video.job_id != request.job_id
            or video.task.task_id != request.task_id
            or video.input_id != request.input_id
            or result.plan.plan_digest != request.plan_digest
            or result.entry.ordinal != request.ordinal
        ):
            raise ValueError("video reference escaped its requested frame")
        # `video.actor_id` records the actor who admitted the Job. HTTP
        # `request.actor_id` is the current reader and may differ by design.
        return
    c.require_scope(operation, request, result)


def http_error_status(code: ErrorCode) -> int:
    if code == "account_not_active":
        return 401
    if code in {"permission_denied", "task_not_visible", "invalid_account_role"}:
        return 403
    if code in {"manifest_missing", "input_missing", "frame_missing", "source_missing"}:
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
        schema = model.model_json_schema(ref_template=ref_template)
        for name, definition in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting video HTTP definition")
            definitions[name] = definition
        if model.__name__ in definitions and definitions[model.__name__] != schema:
            raise ValueError("conflicting video HTTP model")
        definitions[model.__name__] = schema
    return definitions


def contract_document() -> dict[str, Any]:
    models = [model for pair in HTTP_OPERATIONS.values() for model in pair]
    models.append(VideoInputControlError)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONTROL_SCHEMA,
        "$defs": _definitions(models, "#/$defs/{model}"),
        "x-budgets": {
            "request_bytes": v.REQUEST_BYTES,
            "reply_bytes": v.REPLY_BYTES,
            "page_size": v.PAGE_SIZE,
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
    models.append(VideoInputControlError)
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
                "error": {"$ref": "#/components/schemas/VideoInputControlError"},
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
                        "description": "Retained video input control result",
                        "content": {"application/json": {"schema": success}},
                    },
                    "default": {
                        "description": "Video input control failure",
                        "content": {"application/json": {"schema": failure}},
                    },
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Retained video input control", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-control-schema": CONTROL_SCHEMA,
        "x-budgets": contract_document()["x-budgets"],
    }
