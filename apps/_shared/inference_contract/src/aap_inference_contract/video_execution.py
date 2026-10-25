"""Private Native route bindings for a claimed video execution."""

from __future__ import annotations

from typing import Literal
import json
import uuid

from pydantic import Field, StrictInt, StrictStr, TypeAdapter

from .routing import (
    ROUTING_REPLY_MAX_BYTES,
    ROUTING_REQUEST_MAX_BYTES,
    RoutingScopeView,
    RoutingResult,
    ROUTING_RESULT_PROTOCOL,
    RoutingUnavailable,
    RoutingVideoExecutionProof,
    _RoutingModel,
    _json_input,
)

VIDEO_EXECUTION_DATA_PROTOCOL = "aap-video-execution-data/v1"
VIDEO_EXECUTION_STATUS_RESULT_PROTOCOL = "aap-video-execution-status-result/v1"
VIDEO_EXECUTION_STATUS_PATH = "/v1/routes/video-execution-status"
VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES = ROUTING_REQUEST_MAX_BYTES
VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES = ROUTING_REPLY_MAX_BYTES
VIDEO_EXECUTION_SCOPE_ACTIONS = frozenset(
    {"policy", "prepare", "reserve", "status", "record"}
)
VIDEO_EXECUTION_SCOPE_FUNCTIONS = (
    "aap_inference_routing_v1.routing_policy()",
    "aap_inference_routing_v1.routing_step(text,text)",
    "aap_inference_routing_v1.scope_retirement_step(text,text)",
    "aap_inference_routing_v1.video_execution_scope_step(text,text)",
)


class VideoScopeRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class VideoRouteIntent(_RoutingModel):
    job_id: uuid.UUID
    run_id: uuid.UUID
    generation: StrictInt = Field(ge=1, le=(1 << 63) - 1)
    request_id: uuid.UUID
    command_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: uuid.UUID
    state: Literal["prepared", "bound"]


class VideoExecutionStatusResult(_RoutingModel):
    schema_: Literal["aap-video-execution-status-result/v1"] = Field(alias="schema")
    proof: RoutingVideoExecutionProof = Field(repr=False)
    data: dict


def decode_video_execution_status_request(
    value: object,
) -> RoutingVideoExecutionProof:
    """Decode the exact five-field private status proof request."""
    try:
        _, parsed = _json_input(
            value, max_bytes=VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES
        )
        if set(parsed) != set(RoutingVideoExecutionProof.model_fields):
            raise RoutingUnavailable()
        return TypeAdapter(RoutingVideoExecutionProof).validate_json(
            json.dumps(parsed, allow_nan=False, separators=(",", ":"))
        )
    except RoutingUnavailable:
        raise
    except Exception:
        raise RoutingUnavailable() from None


def video_execution_status_request_bytes(value: object) -> bytes:
    proof = decode_video_execution_status_request(value)
    try:
        encoded = json.dumps(
            proof.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise RoutingUnavailable() from None
    if len(encoded) > VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def decode_video_execution_status_result(
    request: RoutingVideoExecutionProof | object, value: object
) -> dict:
    proof = decode_video_execution_status_request(request)
    try:
        _, parsed = _json_input(value, max_bytes=VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES)
        if set(parsed) != {"schema", "proof", "data"}:
            raise RoutingUnavailable()
        result = VideoExecutionStatusResult.model_validate_json(
            json.dumps(parsed, allow_nan=False, separators=(",", ":"))
        )
        if result.proof != proof:
            raise RoutingUnavailable()
        data = decode_video_scope_data(
            "status",
            proof.model_dump(mode="json"),
            result.data,
        )
        return data
    except (VideoScopeRejected, RoutingUnavailable):
        raise
    except Exception:
        raise RoutingUnavailable() from None


def video_execution_status_result_bytes(
    request: RoutingVideoExecutionProof | object, data: object
) -> bytes:
    proof = decode_video_execution_status_request(request)
    if type(data) is dict and set(data) == {"intent", "scope"}:
        data = {
            "schema": VIDEO_EXECUTION_DATA_PROTOCOL,
            "status": "ok",
            "intent": (
                data["intent"].model_dump(mode="json")
                if isinstance(data["intent"], VideoRouteIntent)
                else data["intent"]
            ),
            "scope": (
                data["scope"].model_dump(mode="json")
                if isinstance(data["scope"], RoutingScopeView)
                else data["scope"]
            ),
        }
    normalized = decode_video_scope_data("status", proof.model_dump(mode="json"), data)
    result = VideoExecutionStatusResult(
        schema=VIDEO_EXECUTION_STATUS_RESULT_PROTOCOL,
        proof=proof,
        data={
            "schema": VIDEO_EXECUTION_DATA_PROTOCOL,
            "status": "ok",
            "intent": (
                normalized["intent"].model_dump(mode="json")
                if normalized["intent"] is not None
                else None
            ),
            "scope": (
                normalized["scope"].model_dump(mode="json")
                if normalized["scope"] is not None
                else None
            ),
        },
    )
    try:
        encoded = json.dumps(
            result.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise RoutingUnavailable() from None
    if len(encoded) > VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES:
        raise RoutingUnavailable()
    decode_video_execution_status_result(proof, encoded)
    return encoded


def decode_video_scope_data(action: str, arguments: dict, value: object) -> dict:
    """Validate identity echoes before the bound Native transaction commits."""
    try:
        _, parsed = _json_input(value, max_bytes=ROUTING_REPLY_MAX_BYTES)
        if (
            type(parsed) is not dict
            or parsed.get("schema") != VIDEO_EXECUTION_DATA_PROTOCOL
        ):
            raise RoutingUnavailable()
        if parsed.get("status") == "rejected":
            if set(parsed) != {"schema", "status", "reason"} or parsed[
                "reason"
            ] not in {
                "invalid_request",
                "invalid_command",
                "job_missing",
                "job_not_active",
                "job_not_writable",
                "job_cancelled",
                "scope_mismatch",
                "lease_lost",
                "execution_busy",
                "recovery_required",
                "legacy_execution_unbound",
                "realm_mismatch",
                "intent_conflict",
                "scope_unavailable",
                "management_busy",
                "pool_unavailable",
                "pool_not_enabled",
                "pool_saturated",
                "generation_mismatch",
                "backend_not_ready",
                "continuity_unverified",
                "transport_changed",
            }:
                raise RoutingUnavailable()
            raise VideoScopeRejected(parsed["reason"])
        if parsed.get("status") != "ok":
            raise RoutingUnavailable()
        if action == "policy":
            if set(parsed) != {"schema", "status", "ready", "functions"} or (
                parsed["ready"] is not True
                or parsed["functions"] != list(VIDEO_EXECUTION_SCOPE_FUNCTIONS)
            ):
                raise RoutingUnavailable()
            return parsed
        proof = TypeAdapter(RoutingVideoExecutionProof).validate_json(
            json.dumps(
                {key: arguments[key] for key in RoutingVideoExecutionProof.model_fields}
            )
        )
        if action == "record":
            if set(parsed) != {"schema", "status", "receipt"}:
                raise RoutingUnavailable()
            receipt = parsed["receipt"]
            if (
                type(receipt) is not dict
                or set(receipt)
                != {"request_id", "command_sha256", "operation", "state", "result"}
                or (
                    receipt["request_id"] != arguments["request_id"]
                    or receipt["command_sha256"] != arguments["command_sha256"]
                    or receipt["operation"] != "video_acquire"
                    or receipt["state"] not in {"caller_pending", "committed"}
                    or receipt["result"] != arguments["result"]
                )
            ):
                raise RoutingUnavailable()
            result = TypeAdapter(RoutingResult).validate_json(
                json.dumps(
                    {
                        "schema": ROUTING_RESULT_PROTOCOL,
                        **receipt,
                    }
                )
            )
            if isinstance(result.result, RoutingScopeView) and (
                result.result.owner != f"tracker:{proof.job_id}"
                or result.result.operation != "tracker"
            ):
                raise RoutingUnavailable()
            return {"receipt": receipt}
        if action not in {"prepare", "reserve", "status"} or set(parsed) != {
            "schema",
            "status",
            "intent",
            "scope",
        }:
            raise RoutingUnavailable()
        if parsed["intent"] is None:
            if action != "status" or parsed["scope"] is not None:
                raise RoutingUnavailable()
            return {"intent": None, "scope": None}
        # JSON-mode validation accepts UUID wire strings while retaining strict
        # integer and scalar validation for every other field.
        intent = TypeAdapter(VideoRouteIntent).validate_json(
            json.dumps(parsed["intent"])
        )
        scope = (
            TypeAdapter(RoutingScopeView).validate_json(json.dumps(parsed["scope"]))
            if parsed["scope"] is not None
            else None
        )
        if intent.job_id != proof.job_id or intent.run_id != proof.run_id:
            raise RoutingUnavailable()
        if action == "status":
            if intent.generation > proof.generation:
                raise RoutingUnavailable()
        elif (
            intent.generation != proof.generation
            or str(intent.request_id) != arguments["request_id"]
            or intent.command_sha256 != arguments["command_sha256"]
            or str(intent.scope_id) != arguments["scope_id"]
        ):
            raise RoutingUnavailable()
        # Status preserves a retired acquisition's intent without exposing a
        # foreign-realm capability. Jobs independently verifies retirement.
        if (intent.state == "bound") != (scope is not None) and not (
            action == "status" and intent.state == "bound" and scope is None
        ):
            raise RoutingUnavailable()
        if scope is not None and (
            scope.id != intent.scope_id
            or scope.owner != f"tracker:{proof.job_id}"
            or scope.operation != "tracker"
        ):
            raise RoutingUnavailable()
        if action == "reserve" and scope is None:
            raise RoutingUnavailable()
        return {"intent": intent, "scope": scope}
    except (VideoScopeRejected, RoutingUnavailable):
        raise
    except Exception:
        raise RoutingUnavailable() from None
