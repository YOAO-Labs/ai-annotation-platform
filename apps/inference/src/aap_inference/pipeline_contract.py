"""Closed wire contracts for the independent saved-pipeline owner."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from aap_workspace_contract.project_query_values import (
    decode_postgres_timestamp,
)
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable
from aap_workspace_contract.wire import (
    ClosedModel,
    _json_bytes,
    _reject_constant,
    _unique_object,
)
from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

PIPELINE_PROTOCOL = "aap-inference-pipelines/v1"
PIPELINE_STEP_PROTOCOL = "aap-inference-pipelines-step/v1"
PIPELINE_POLICY_PROTOCOL = "aap-inference-pipelines-policy/v1"
PIPELINE_REQUEST_BODY_MAX_BYTES = 16 * 1024 * 1024
PIPELINE_RESPONSE_BODY_MAX_BYTES = 16 * 1024 * 1024

PipelineOperation: TypeAlias = Literal[
    "create_pipeline",
    "update_pipeline",
    "delete_pipeline",
    "apply_pipeline",
]
PipelineQueryOperation: TypeAlias = Literal[
    "list_pipelines",
    "get_pipeline",
    "get_pipeline_projection",
]
PipelineStatusOperation: TypeAlias = Literal["get_operation_status"]
PipelineAnyOperation: TypeAlias = (
    PipelineOperation | PipelineQueryOperation | PipelineStatusOperation
)
PipelineAction: TypeAlias = Literal[
    "begin",
    "status",
    "organization_ids",
    "pipeline_get",
    "pipeline_list",
    "backend_get",
    "backend_pool",
    "backend_enabled",
    "project_query",
    "project_access",
    "default_clear",
    "pipeline_insert",
    "pipeline_update",
    "pipeline_delete",
    "source_usage_increment",
    "finish",
]
PipelineScope: TypeAlias = Literal["private", "organization", "public"]
PipelineCommitState: TypeAlias = Literal[
    "caller_pending", "committed", "not_started", "rolled_back", "unknown"
]
PipelineStatusState: TypeAlias = Literal["committed", "unknown"]
PipelineJsonObject: TypeAlias = dict[StrictStr, JsonValue]

PIPELINE_ACTIONS = (
    "begin",
    "status",
    "organization_ids",
    "pipeline_get",
    "pipeline_list",
    "backend_get",
    "backend_pool",
    "backend_enabled",
    "project_query",
    "project_access",
    "default_clear",
    "pipeline_insert",
    "pipeline_update",
    "pipeline_delete",
    "source_usage_increment",
    "finish",
)
PIPELINE_ROW_COLUMNS = (
    "id",
    "scope",
    "project_id",
    "organization_id",
    "name",
    "stages",
    "is_default",
    "created_by",
    "usage_count",
    "created_at",
    "updated_at",
)
PIPELINE_INSERT_COLUMNS = (
    "id",
    "scope",
    "project_id",
    "organization_id",
    "name",
    "stages",
    "is_default",
    "created_by",
)
PIPELINE_UPDATE_COLUMNS = (
    "scope",
    "project_id",
    "organization_id",
    "name",
    "stages",
    "is_default",
)
_PIPELINE_TIMESTAMP_EXTREMA = (
    "infinity",
    "-infinity",
    datetime.max.isoformat(),
    datetime.min.isoformat(),
)


def _pipeline_timestamp_json_schema() -> dict:
    return {
        "anyOf": [
            {"type": "string", "format": "date-time"},
            {"type": "string", "enum": list(_PIPELINE_TIMESTAMP_EXTREMA)},
            {"type": "null"},
        ]
    }

_PIPELINE_OPERATIONS = frozenset(
    {"create_pipeline", "update_pipeline", "delete_pipeline", "apply_pipeline"}
)
_PIPELINE_QUERY_OPERATIONS = frozenset(
    {"list_pipelines", "get_pipeline", "get_pipeline_projection"}
)
_PIPELINE_STATUS_CODES = (400, 401, 403, 404, 409, 422, 500, 503)


class PipelineInvalidRequest(ValueError):
    """A caller supplied a value outside the closed Inference pipeline contract."""

    def __init__(self):
        super().__init__("Invalid Inference pipeline request")


class PipelineUnavailable(Exception):
    """A required Inference pipeline capability is unavailable or malformed."""

    def __init__(self):
        super().__init__("Inference pipeline owner unavailable")


class PipelineRejected(Exception):
    """A public pipeline command was rejected with safe response details."""

    def __init__(
        self,
        status_code: int,
        detail: JsonValue,
        headers: Mapping[str, str] | None = None,
        rollback: bool = False,
    ) -> None:
        value = PipelineErrorDetails(
            status_code=status_code,
            detail=detail,
            headers=dict(headers) if headers is not None else None,
            rollback=rollback,
        )
        self.status_code = value.status_code
        self.detail = value.detail
        self.headers = value.headers
        self.rollback = value.rollback
        super().__init__("Inference pipeline operation rejected")


class PipelinePostCommitFailure(Exception):
    """A command committed but its legacy response projection failed."""

    def __init__(self):
        super().__init__("Internal Server Error")


def _json_value(payload) -> JsonValue:
    encoded = _json_bytes(payload)
    return json.loads(
        encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )


def _json_object(payload) -> dict[str, JsonValue]:
    value = _json_value(payload)
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    return value


def _json_ready(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def _decode(model, payload, failure):
    try:
        return TypeAdapter(model).validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise failure() from None


class _PipelineWireModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict):
            if "protocol" in value:
                raise ValueError("Use only the declared wire field schema")
            if "ok" in value and type(value["ok"]) is not bool:
                raise ValueError("ok must be a JSON boolean")
        return value


class PipelineActor(_PipelineWireModel):
    id: uuid.UUID
    role: StrictStr


class PipelineCommandRequest(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    operation: PipelineOperation
    admitted_actor: PipelineActor
    target_id: uuid.UUID | None
    payload_ascii: StrictStr

    @field_validator("payload_ascii")
    @classmethod
    def ascii_json_object(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("Pipeline payload must be ASCII JSON")
        if type(_json_object(value)) is not dict:
            raise ValueError("Pipeline payload must be a JSON object")
        return value

    @model_validator(mode="after")
    def operation_target_and_payload_match(self):
        if self.operation == "create_pipeline":
            if self.target_id is not None:
                raise ValueError("Pipeline create target must be null")
        elif self.target_id is None:
            raise ValueError("Pipeline command target is required")

        payload = _json_object(self.payload_ascii)
        if self.operation == "delete_pipeline" and payload:
            raise ValueError("Pipeline delete payload must be empty")
        if self.operation == "apply_pipeline":
            if set(payload) != {"pipeline_id", "set_default"}:
                raise ValueError("Pipeline apply payload fields do not match")
            try:
                uuid.UUID(payload["pipeline_id"])
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Pipeline apply pipeline_id must be a UUID") from None
            if type(payload["set_default"]) is not bool:
                raise ValueError("Pipeline apply set_default must be a JSON boolean")
        return self


class PipelineFilters(_PipelineWireModel):
    scope: PipelineScope | None
    project_id: uuid.UUID | None
    organization_id: uuid.UUID | None


class PipelineQueryRequest(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: PipelineQueryOperation
    admitted_actor: PipelineActor
    target_id: uuid.UUID | None
    filters: PipelineFilters | None
    evidence: PipelineCommandRequest | None

    @model_validator(mode="after")
    def query_fields_match_operation(self):
        if self.operation == "list_pipelines":
            if self.target_id is not None or self.filters is None or self.evidence is not None:
                raise ValueError("Pipeline list request fields do not match")
            return self
        if self.target_id is None or self.filters is not None:
            raise ValueError("Pipeline read target/filter fields do not match")
        if self.operation == "get_pipeline":
            if self.evidence is not None:
                raise ValueError("Pipeline get request cannot carry command evidence")
            return self

        evidence = self.evidence
        if evidence is None or evidence.operation == "delete_pipeline":
            raise ValueError("Pipeline projection requires non-delete command evidence")
        if evidence.admitted_actor != self.admitted_actor:
            raise ValueError("Pipeline projection actor snapshot does not match command evidence")
        if evidence.operation == "update_pipeline" and evidence.target_id != self.target_id:
            raise ValueError("Pipeline projection target does not match update command")
        return self


class PipelineStatusRequest(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: PipelineStatusOperation
    command: PipelineCommandRequest


class PipelineRow(_PipelineWireModel):
    id: uuid.UUID
    scope: StrictStr
    project_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    name: StrictStr
    stages: JsonValue
    is_default: StrictBool
    created_by: uuid.UUID
    usage_count: StrictInt
    created_at: datetime | None = Field(
        json_schema_extra=_pipeline_timestamp_json_schema()
    )
    updated_at: datetime | None = Field(
        json_schema_extra=_pipeline_timestamp_json_schema()
    )

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def decode_timestamp(cls, value):
        if value is None or isinstance(value, datetime):
            return value
        if type(value) is str:
            # PostgreSQL infinity is normalized to these naive extrema by the
            # shared decoder. Their JSON ISO representation has no UTC offset.
            if value == datetime.max.isoformat():
                return datetime.max
            if value == datetime.min.isoformat():
                return datetime.min
            try:
                return decode_postgres_timestamp(value)
            except ProjectQueryUnavailable:
                raise ValueError("Invalid pipeline timestamp") from None
        raise ValueError("Invalid pipeline timestamp")


class PipelineResult(_PipelineWireModel):
    resource_id: uuid.UUID


class PipelineErrorDetails(_PipelineWireModel):
    status_code: Literal[400, 401, 403, 404, 409, 422, 500, 503]
    detail: JsonValue
    headers: dict[StrictStr, StrictStr] | None
    rollback: StrictBool


class PipelineSuccess(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    operation: PipelineOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    result: PipelineResult
    commit_state: Literal["caller_pending", "committed"]


class PipelineFailure(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    ok: Literal[False]
    operation_id: uuid.UUID
    operation: PipelineOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    error: PipelineErrorDetails
    commit_state: Literal["not_started", "rolled_back", "unknown", "caller_pending"]

    @model_validator(mode="after")
    def uncertain_commit_cannot_claim_rollback(self):
        if self.commit_state == "unknown" and self.error.rollback:
            raise ValueError("Unknown commit state cannot claim rollback")
        return self


PipelineReply: TypeAlias = Annotated[
    PipelineSuccess | PipelineFailure,
    Field(discriminator="ok"),
]


class PipelineQuerySuccess(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    ok: Literal[True]
    request_id: uuid.UUID
    operation: PipelineQueryOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    rows: list[PipelineRow]


class PipelineQueryFailure(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    ok: Literal[False]
    request_id: uuid.UUID
    operation: PipelineQueryOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    error: PipelineErrorDetails


PipelineQueryReply: TypeAlias = Annotated[
    PipelineQuerySuccess | PipelineQueryFailure,
    Field(discriminator="ok"),
]


class PipelineStatusSuccess(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    ok: Literal[True]
    request_id: uuid.UUID
    operation: PipelineStatusOperation
    operation_id: uuid.UUID
    command_operation: PipelineOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    state: PipelineStatusState
    resource_id: uuid.UUID | None

    @model_validator(mode="after")
    def committed_state_has_resource(self):
        if (self.state == "committed") != (self.resource_id is not None):
            raise ValueError("Pipeline status state/resource does not match")
        return self


class PipelineStatusFailure(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines/v1"] = Field(alias="schema")
    ok: Literal[False]
    request_id: uuid.UUID
    operation: PipelineStatusOperation
    operation_id: uuid.UUID
    command_operation: PipelineOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    error: PipelineErrorDetails


PipelineStatusReply: TypeAlias = Annotated[
    PipelineStatusSuccess | PipelineStatusFailure,
    Field(discriminator="ok"),
]


class PipelineBeginReplay(_PipelineWireModel):
    resource_id: uuid.UUID


class PipelineBeginFacts(_PipelineWireModel):
    replay: PipelineBeginReplay | None


class PipelineStatusFacts(_PipelineWireModel):
    state: PipelineStatusState
    resource_id: uuid.UUID | None

    @model_validator(mode="after")
    def committed_state_has_resource(self):
        if (self.state == "committed") != (self.resource_id is not None):
            raise ValueError("Pipeline status state/resource does not match")
        return self


class PipelineOrganizationIdsFacts(_PipelineWireModel):
    organization_ids: list[StrictStr]

    @field_validator("organization_ids")
    @classmethod
    def uuid_strings(cls, values):
        for value in values:
            try:
                uuid.UUID(value)
            except ValueError:
                raise ValueError("Organization IDs must be UUID strings") from None
        return values


class PipelineRowJsonFacts(_PipelineWireModel):
    row_json: StrictStr | None


class PipelineRowsJsonFacts(_PipelineWireModel):
    rows_json: StrictStr


class PipelineWorkspaceReplyFacts(_PipelineWireModel):
    workspace_reply: PipelineJsonObject


class PipelineAffectedCountFacts(_PipelineWireModel):
    affected_count: StrictInt


class PipelineResourceIdFacts(_PipelineWireModel):
    resource_id: uuid.UUID


PipelineStepFacts: TypeAlias = (
    PipelineBeginFacts
    | PipelineStatusFacts
    | PipelineOrganizationIdsFacts
    | PipelineRowJsonFacts
    | PipelineRowsJsonFacts
    | PipelineWorkspaceReplyFacts
    | PipelineAffectedCountFacts
    | PipelineResourceIdFacts
)
_PIPELINE_STEP_FACT_MODELS = {
    "begin": PipelineBeginFacts,
    "status": PipelineStatusFacts,
    "organization_ids": PipelineOrganizationIdsFacts,
    "pipeline_get": PipelineRowJsonFacts,
    "pipeline_list": PipelineRowsJsonFacts,
    "backend_get": PipelineRowJsonFacts,
    "backend_pool": PipelineRowsJsonFacts,
    "backend_enabled": PipelineRowJsonFacts,
    "project_query": PipelineWorkspaceReplyFacts,
    "project_access": PipelineWorkspaceReplyFacts,
    "default_clear": PipelineAffectedCountFacts,
    "pipeline_insert": PipelineResourceIdFacts,
    "pipeline_update": PipelineResourceIdFacts,
    "pipeline_delete": PipelineResourceIdFacts,
    "source_usage_increment": PipelineAffectedCountFacts,
    "finish": PipelineResourceIdFacts,
}


class PipelineStepSuccess(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines-step/v1"] = Field(alias="schema")
    ok: Literal[True]
    request_id: uuid.UUID
    operation: PipelineAnyOperation
    action: PipelineAction
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    facts: PipelineStepFacts

    @model_validator(mode="before")
    @classmethod
    def parse_facts_for_action(cls, value):
        if not isinstance(value, dict):
            return value
        model = _PIPELINE_STEP_FACT_MODELS.get(value.get("action"))
        facts = value.get("facts")
        if model is None or not isinstance(facts, dict):
            return value
        parsed = dict(value)
        parsed["facts"] = TypeAdapter(model).validate_json(_json_bytes(facts))
        return parsed

    @model_validator(mode="after")
    def facts_match_action(self):
        if type(self.facts) is not _PIPELINE_STEP_FACT_MODELS[self.action]:
            raise ValueError("Pipeline step action/facts do not match")
        return self


class PipelineStepFailure(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines-step/v1"] = Field(alias="schema")
    ok: Literal[False]
    request_id: uuid.UUID
    operation: PipelineAnyOperation
    action: PipelineAction
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    error: PipelineErrorDetails


PipelineStepReply: TypeAlias = Annotated[
    PipelineStepSuccess | PipelineStepFailure,
    Field(discriminator="ok"),
]


class PipelineSerialization(_PipelineWireModel):
    timezone: Literal["UTC"]
    date_style: Literal["ISO,YMD"]
    extra_float_digits: StrictInt


class PipelinePolicy(_PipelineWireModel):
    protocol: Literal["aap-inference-pipelines-policy/v1"] = Field(alias="schema")
    request_schema: Literal["aap-inference-pipelines/v1"]
    step_schema: Literal["aap-inference-pipelines-step/v1"]
    request_body_max_bytes: StrictInt
    response_body_max_bytes: StrictInt
    pipeline_columns: list[StrictStr]
    insert_columns: list[StrictStr]
    update_columns: list[StrictStr]
    actions: list[StrictStr]
    serialization: PipelineSerialization


PipelineSchemaDocument: TypeAlias = (
    PipelineCommandRequest
    | PipelineQueryRequest
    | PipelineStatusRequest
    | PipelineReply
    | PipelineQueryReply
    | PipelineStatusReply
    | PipelineStepReply
    | PipelinePolicy
)


def pipeline_policy() -> PipelinePolicy:
    return PipelinePolicy(
        **{
            "schema": PIPELINE_POLICY_PROTOCOL,
            "request_schema": PIPELINE_PROTOCOL,
            "step_schema": PIPELINE_STEP_PROTOCOL,
            "request_body_max_bytes": PIPELINE_REQUEST_BODY_MAX_BYTES,
            "response_body_max_bytes": PIPELINE_RESPONSE_BODY_MAX_BYTES,
            "pipeline_columns": list(PIPELINE_ROW_COLUMNS),
            "insert_columns": list(PIPELINE_INSERT_COLUMNS),
            "update_columns": list(PIPELINE_UPDATE_COLUMNS),
            "actions": list(PIPELINE_ACTIONS),
            "serialization": {
                "timezone": "UTC",
                "date_style": "ISO,YMD",
                "extra_float_digits": 3,
            },
        }
    )


def decode_pipeline_policy(payload) -> PipelinePolicy:
    policy = _decode(PipelinePolicy, payload, PipelineUnavailable)
    if policy != pipeline_policy():
        raise PipelineUnavailable()
    return policy


def pipeline_schema() -> dict:
    schema = TypeAdapter(PipelineSchemaDocument).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PIPELINE_PROTOCOL
    schema["x-pipeline-row-columns"] = list(PIPELINE_ROW_COLUMNS)
    schema["x-pipeline-insert-columns"] = list(PIPELINE_INSERT_COLUMNS)
    schema["x-pipeline-update-columns"] = list(PIPELINE_UPDATE_COLUMNS)
    schema["x-pipeline-actions"] = list(PIPELINE_ACTIONS)
    schema["x-request-body-max-bytes"] = PIPELINE_REQUEST_BODY_MAX_BYTES
    schema["x-response-body-max-bytes"] = PIPELINE_RESPONSE_BODY_MAX_BYTES

    definitions = schema.get("$defs", {})
    step_success = definitions.get("PipelineStepSuccess")
    if step_success is not None:
        step_success["allOf"] = [
            {
                "if": {
                    "properties": {"action": {"const": action}},
                    "required": ["action"],
                },
                "then": {
                    "properties": {
                        "facts": {"$ref": f"#/$defs/{fact_type.__name__}"}
                    }
                },
            }
            for action, fact_type in _PIPELINE_STEP_FACT_MODELS.items()
        ]
    return schema


def decode_pipeline_command_request(payload) -> PipelineCommandRequest:
    return _decode(PipelineCommandRequest, payload, PipelineInvalidRequest)


def decode_pipeline_query_request(payload) -> PipelineQueryRequest:
    return _decode(PipelineQueryRequest, payload, PipelineInvalidRequest)


def decode_pipeline_status_request(payload) -> PipelineStatusRequest:
    return _decode(PipelineStatusRequest, payload, PipelineInvalidRequest)


def pipeline_command_request(
    operation,
    *,
    actor_id,
    actor_role,
    target_id=None,
    payload,
    operation_id=None,
) -> PipelineCommandRequest:
    try:
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(
                mode="json",
                by_alias=True,
                exclude_unset=operation == "update_pipeline",
            )
        value = {
            "schema": PIPELINE_PROTOCOL,
            "operation_id": str(operation_id or uuid.uuid4()),
            "operation": operation,
            "admitted_actor": {"id": str(actor_id), "role": actor_role},
            "target_id": str(target_id) if target_id is not None else None,
            "payload_ascii": _json_bytes(_json_ready(payload)).decode("ascii"),
        }
        return decode_pipeline_command_request(value)
    except PipelineInvalidRequest:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PipelineInvalidRequest() from None


def pipeline_query_request(
    operation,
    *,
    actor_id,
    actor_role,
    target_id=None,
    filters=None,
    evidence=None,
    request_id=None,
) -> PipelineQueryRequest:
    try:
        if isinstance(filters, PipelineFilters):
            filters = filters.model_dump(mode="json", by_alias=True)
        elif filters is not None:
            filters = _json_ready(filters)
        if isinstance(evidence, PipelineCommandRequest):
            evidence = evidence.model_dump(mode="json", by_alias=True)
        elif evidence is not None:
            evidence = _json_ready(evidence)
        return decode_pipeline_query_request(
            {
                "schema": PIPELINE_PROTOCOL,
                "request_id": str(request_id or uuid.uuid4()),
                "operation": operation,
                "admitted_actor": {"id": str(actor_id), "role": actor_role},
                "target_id": str(target_id) if target_id is not None else None,
                "filters": filters,
                "evidence": evidence,
            }
        )
    except PipelineInvalidRequest:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PipelineInvalidRequest() from None


def pipeline_status_request(command, *, request_id=None) -> PipelineStatusRequest:
    try:
        if not isinstance(command, PipelineCommandRequest):
            command = decode_pipeline_command_request(command)
        return decode_pipeline_status_request(
            {
                "schema": PIPELINE_PROTOCOL,
                "request_id": str(request_id or uuid.uuid4()),
                "operation": "get_operation_status",
                "command": command.model_dump(mode="json", by_alias=True),
            }
        )
    except PipelineInvalidRequest:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PipelineInvalidRequest() from None


def _request_binding(request):
    if isinstance(request, PipelineCommandRequest):
        return (
            request.operation_id,
            request.operation,
            request.admitted_actor.id,
            request.target_id,
        )
    if isinstance(request, PipelineQueryRequest):
        return (
            request.request_id,
            request.operation,
            request.admitted_actor.id,
            request.target_id,
        )
    if isinstance(request, PipelineStatusRequest):
        return (
            request.request_id,
            request.operation,
            request.command.admitted_actor.id,
            request.command.target_id,
        )
    raise PipelineUnavailable()


def _decode_request_binding(request):
    if isinstance(
        request, (PipelineCommandRequest, PipelineQueryRequest, PipelineStatusRequest)
    ):
        return request
    try:
        value = _json_object(request)
        operation = value.get("operation")
        if operation in _PIPELINE_OPERATIONS:
            return _decode(PipelineCommandRequest, value, PipelineUnavailable)
        if operation in _PIPELINE_QUERY_OPERATIONS:
            return _decode(PipelineQueryRequest, value, PipelineUnavailable)
        if operation == "get_operation_status":
            return _decode(PipelineStatusRequest, value, PipelineUnavailable)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PipelineUnavailable() from None
    raise PipelineUnavailable()


def decode_pipeline_reply(
    request, raw, *, require_committed: bool = False
) -> PipelineReply:
    request = _decode_request_binding(request)
    reply = _decode(PipelineReply, raw, PipelineUnavailable)
    operation_id, operation, actor_id, target_id = _request_binding(request)
    if (
        reply.operation_id != operation_id
        or reply.operation != operation
        or reply.actor_id != actor_id
        or reply.target_id != target_id
    ):
        raise PipelineUnavailable()
    if require_committed and reply.commit_state == "caller_pending":
        raise PipelineUnavailable()
    if isinstance(reply, PipelineSuccess):
        if operation in {"update_pipeline", "delete_pipeline"}:
            if reply.result.resource_id != target_id:
                raise PipelineUnavailable()
    return reply


def _query_rows_as_models(payload):
    if isinstance(payload, (PipelineQuerySuccess, PipelineQueryFailure)):
        raw = payload.model_dump(mode="python", by_alias=True)
    elif isinstance(payload, BaseModel):
        raw = payload.model_dump(mode="python", by_alias=True)
    elif isinstance(payload, dict):
        raw = dict(payload)
    else:
        raw = _json_object(payload)
    for field in ("request_id", "actor_id", "target_id"):
        identifier = raw.get(field)
        if identifier is None or isinstance(identifier, uuid.UUID):
            continue
        if type(identifier) is not str:
            raise PipelineUnavailable()
        try:
            raw[field] = uuid.UUID(identifier)
        except ValueError:
            raise PipelineUnavailable() from None
    if raw.get("ok") is True:
        rows = raw.get("rows")
        if type(rows) is not list:
            raise PipelineUnavailable()
        materialized = all(
            isinstance(row, PipelineRow)
            or (
                type(row) is dict
                and isinstance(row.get("id"), uuid.UUID)
                and all(
                    row.get(field) is None or isinstance(row.get(field), datetime)
                    for field in ("created_at", "updated_at")
                )
            )
            for row in rows
        )
        if materialized:
            raw["rows"] = [
                row
                if isinstance(row, PipelineRow)
                else PipelineRow.model_validate(row)
                for row in rows
            ]
        else:
            encoded = _json_bytes(rows).decode("ascii")
            from .pipeline_values import decode_pipeline_rows

            raw["rows"] = decode_pipeline_rows(encoded)
    return raw


def decode_pipeline_query_reply(request, raw) -> PipelineQueryReply:
    request = _decode(PipelineQueryRequest, request, PipelineUnavailable)
    try:
        value = _query_rows_as_models(raw)
        reply = TypeAdapter(PipelineQueryReply).validate_python(value)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PipelineUnavailable() from None
    if (
        reply.request_id != request.request_id
        or reply.operation != request.operation
        or reply.actor_id != request.admitted_actor.id
        or reply.target_id != request.target_id
    ):
        raise PipelineUnavailable()
    if isinstance(reply, PipelineQuerySuccess) and request.operation != "list_pipelines":
        if len(reply.rows) != 1 or reply.rows[0].id != request.target_id:
            raise PipelineUnavailable()
    return reply


def decode_pipeline_status_reply(request, raw) -> PipelineStatusReply:
    request = _decode(PipelineStatusRequest, request, PipelineUnavailable)
    reply = _decode(PipelineStatusReply, raw, PipelineUnavailable)
    command = request.command
    if (
        reply.request_id != request.request_id
        or reply.operation != request.operation
        or reply.operation_id != command.operation_id
        or reply.command_operation != command.operation
        or reply.actor_id != command.admitted_actor.id
        or reply.target_id != command.target_id
    ):
        raise PipelineUnavailable()
    if isinstance(reply, PipelineStatusSuccess) and reply.state == "committed":
        if command.operation in {"update_pipeline", "delete_pipeline"}:
            if reply.resource_id != command.target_id:
                raise PipelineUnavailable()
    return reply


def decode_pipeline_step_reply(request, action, raw) -> PipelineStepReply:
    request = _decode_request_binding(request)
    if action not in PIPELINE_ACTIONS:
        raise PipelineUnavailable()
    reply = _decode(PipelineStepReply, raw, PipelineUnavailable)
    request_id, operation, actor_id, target_id = _request_binding(request)
    if (
        reply.request_id != request_id
        or reply.operation != operation
        or reply.action != action
        or reply.actor_id != actor_id
        or reply.target_id != target_id
    ):
        raise PipelineUnavailable()
    if isinstance(reply, PipelineStepSuccess):
        command = request.command if isinstance(request, PipelineStatusRequest) else request
        expected_target = (
            command.target_id
            if isinstance(command, PipelineCommandRequest)
            else request.target_id
        )
        if action == "begin" and isinstance(reply.facts, PipelineBeginFacts):
            replay = reply.facts.replay
            if (
                replay is not None
                and isinstance(command, PipelineCommandRequest)
                and command.operation in {"update_pipeline", "delete_pipeline"}
                and replay.resource_id != expected_target
            ):
                raise PipelineUnavailable()
        elif action == "status" and isinstance(reply.facts, PipelineStatusFacts):
            if (
                reply.facts.state == "committed"
                and isinstance(command, PipelineCommandRequest)
                and command.operation in {"update_pipeline", "delete_pipeline"}
                and reply.facts.resource_id != expected_target
            ):
                raise PipelineUnavailable()
        elif action in {"pipeline_update", "pipeline_delete", "finish"}:
            if (
                isinstance(command, PipelineCommandRequest)
                and command.operation in {"update_pipeline", "delete_pipeline"}
                and isinstance(reply.facts, PipelineResourceIdFacts)
                and reply.facts.resource_id != expected_target
            ):
                raise PipelineUnavailable()
    return reply


__all__ = [
    "PIPELINE_ACTIONS",
    "PIPELINE_INSERT_COLUMNS",
    "PIPELINE_POLICY_PROTOCOL",
    "PIPELINE_PROTOCOL",
    "PIPELINE_REQUEST_BODY_MAX_BYTES",
    "PIPELINE_RESPONSE_BODY_MAX_BYTES",
    "PIPELINE_ROW_COLUMNS",
    "PIPELINE_STEP_PROTOCOL",
    "PIPELINE_UPDATE_COLUMNS",
    "PipelineActor",
    "PipelineBeginFacts",
    "PipelineCommandRequest",
    "PipelineErrorDetails",
    "PipelineFailure",
    "PipelineFilters",
    "PipelineInvalidRequest",
    "PipelineOperation",
    "PipelinePolicy",
    "PipelinePostCommitFailure",
    "PipelineQueryOperation",
    "PipelineQueryFailure",
    "PipelineQueryReply",
    "PipelineQueryRequest",
    "PipelineQuerySuccess",
    "PipelineRejected",
    "PipelineReply",
    "PipelineResult",
    "PipelineRow",
    "PipelineSerialization",
    "PipelineStatusOperation",
    "PipelineStatusReply",
    "PipelineStatusFailure",
    "PipelineStatusFacts",
    "PipelineStatusRequest",
    "PipelineStatusSuccess",
    "PipelineStepFacts",
    "PipelineStepReply",
    "PipelineStepFailure",
    "PipelineStepSuccess",
    "PipelineUnavailable",
    "PipelineSuccess",
    "decode_pipeline_command_request",
    "decode_pipeline_policy",
    "decode_pipeline_query_reply",
    "decode_pipeline_query_request",
    "decode_pipeline_status_reply",
    "decode_pipeline_status_request",
    "decode_pipeline_step_reply",
    "pipeline_command_request",
    "pipeline_policy",
    "pipeline_query_request",
    "pipeline_schema",
    "pipeline_status_request",
]
