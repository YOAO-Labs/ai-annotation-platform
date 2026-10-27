"""Wire and native contracts for Assets-owned storage connections."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from aap_audit_contract import ProjectDeletionAuditContext
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CONNECTIONS_SCHEMA = "aap-assets-connections/v1"
ConnectionKind = Literal["s3", "sftp"]
ConnectionScope = Literal["global", "owner"]
ConnectionAction = Literal["create", "update", "delete", "test"]
ConnectionAccess = Literal["use", "admin"]
PlatformRole = Literal["super_admin", "project_admin", "employee", "viewer"]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[StrictInt, Field(ge=1)]
PageSize = Annotated[StrictInt, Field(ge=1, le=500)]
Count = Annotated[StrictInt, Field(ge=0)]
SecretEnvelope = Annotated[StrictStr, Field(min_length=1, max_length=16_777_216)]


class _Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    @field_validator("config", "secret", check_fields=False)
    @classmethod
    def require_json_object_values(cls, value):
        if value is not None:
            try:
                json.dumps(value, sort_keys=True, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise ValueError("connection JSON field is invalid") from None
        return value


class ConnectionCursor(_Value):
    created_at: AwareDatetime
    id: UUID


class NativeConnectionFacts(_Value):
    id: UUID
    revision: Revision
    name: StrictStr
    kind: ConnectionKind
    config: dict[str, Any]
    scope: Literal["global", "owner", "project"]
    project_id: UUID | None
    secret_set: StrictBool
    created_by: UUID | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    location_id: UUID | None = None
    location_config_revision: Revision | None = None


class ConnectionRecord(NativeConnectionFacts):
    scope: ConnectionScope

    @model_validator(mode="after")
    def require_public_config(self):
        from .connectors.management import public_config

        if self.config != public_config(self.kind, self.config):
            raise ValueError("connection config contains non-public fields")
        return self


class ListConnectionsRequest(_Value):
    actor_id: UUID
    after: ConnectionCursor | None = None
    page_size: PageSize = 200


class NativeConnectionsPage(_Value):
    actor_id: UUID
    after: ConnectionCursor | None
    next_cursor: ConnectionCursor | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    connections: list[NativeConnectionFacts]


class ConnectionsPage(NativeConnectionsPage):
    connections: list[ConnectionRecord]


class ReadConnectionRequest(_Value):
    actor_id: UUID
    connection_id: UUID


class NativeConnectionRead(_Value):
    actor_id: UUID
    connection_id: UUID
    connection: NativeConnectionFacts


class ConnectionRead(NativeConnectionRead):
    connection: ConnectionRecord


class CreateConnectionRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    kind: ConnectionKind
    config: dict[str, Any]
    secret: dict[str, Any] = Field(repr=False)
    scope: ConnectionScope = "owner"
    audit_context: ProjectDeletionAuditContext = Field(
        default_factory=ProjectDeletionAuditContext
    )


class ConnectionAuthorityProof(_Value):
    scopes: list[StrictStr]
    expires_at: AwareDatetime | None
    revision: Digest


class ConnectionProbeLease(_Value):
    operation_id: UUID
    connection_id: UUID
    location_id: UUID
    revision: Revision
    proof: ConnectionAuthorityProof | None


class ConnectionProbeFinish(_Value):
    settled: StrictBool
    retained: StrictBool
    proof: ConnectionAuthorityProof | None


class BeginConnectionProbeRequest(_Value):
    connection_id: UUID
    actor_id: UUID
    api_key_id: UUID | None = None
    required_scopes: list[StrictStr] = Field(default_factory=list)


class FinishConnectionProbeRequest(_Value):
    operation_id: UUID
    connection_id: UUID
    actor_id: UUID
    api_key_id: UUID | None = None
    required_scopes: list[StrictStr] = Field(default_factory=list)
    expected_proof: ConnectionAuthorityProof | None = None
    settled: StrictBool


class UpdateConnectionRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    connection_id: UUID
    expected_revision: Revision
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)] | None = None
    config: dict[str, Any] | None = None
    secret: dict[str, Any] | None = Field(default=None, repr=False)
    api_key_id: UUID | None = None
    accepted_api_key_proof: ConnectionAuthorityProof | None = None
    audit_context: ProjectDeletionAuditContext = Field(
        default_factory=ProjectDeletionAuditContext
    )


class DeleteConnectionRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    connection_id: UUID
    audit_context: ProjectDeletionAuditContext = Field(
        default_factory=ProjectDeletionAuditContext
    )


class TestConnectionRequest(DeleteConnectionRequest):
    __test__ = False


class NativeConnectionMutation(_Value):
    operation_id: UUID
    actor_id: UUID
    action: Literal["create", "update", "delete"]
    connection_id: UUID
    connection: NativeConnectionFacts | None

    @model_validator(mode="after")
    def require_result_identity(self):
        if self.action == "delete":
            if self.connection is not None:
                raise ValueError("deleted connection must not include a live record")
        elif self.connection is None or self.connection.id != self.connection_id:
            raise ValueError("connection mutation returned another record")
        if self.action == "create" and self.connection_id != self.operation_id:
            raise ValueError("new connection must use the accepted operation identity")
        return self


class ConnectionMutation(NativeConnectionMutation):
    connection: ConnectionRecord | None


class ConnectionProbeResult(_Value):
    ok: StrictBool
    message: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    sample_count: Annotated[StrictInt, Field(ge=0, le=20)] | None

    @model_validator(mode="after")
    def require_probe_outcome(self):
        if self.ok != (self.sample_count is not None):
            raise ValueError("probe count does not match its outcome")
        if self.ok and self.message != "连接成功":
            raise ValueError("unexpected probe success message")
        if not self.ok:
            import re

            if not re.fullmatch(
                r"连接失败: [A-Za-z_][A-Za-z0-9_]{0,127}", self.message
            ):
                raise ValueError("probe failure message contains unsafe details")
        return self


class ConnectionProbeReceipt(ConnectionProbeResult):
    operation_id: UUID
    actor_id: UUID
    connection_id: UUID
    action: Literal["test"] = "test"


class CreateConnectionIntent(_Value):
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    kind: ConnectionKind
    config: dict[str, Any]
    scope: ConnectionScope
    secret_digest: Digest


class UpdateConnectionIntent(_Value):
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)] | None
    config: dict[str, Any] | None
    secret_digest: Digest | None


class EmptyConnectionIntent(_Value):
    pass


class ConnectionReceiptRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    action: ConnectionAction
    connection_id: UUID
    intent: CreateConnectionIntent | UpdateConnectionIntent | EmptyConnectionIntent

    @model_validator(mode="after")
    def require_intent_kind(self):
        expected = {
            "create": CreateConnectionIntent,
            "update": UpdateConnectionIntent,
            "delete": EmptyConnectionIntent,
            "test": EmptyConnectionIntent,
        }[self.action]
        if type(self.intent) is not expected:
            raise ValueError("connection intent does not match the action")
        if self.action == "create" and self.connection_id != self.operation_id:
            raise ValueError("new connection identity must match its operation")
        return self


class ConnectionReceiptLookup(_Value):
    operation_id: UUID
    actor_id: UUID
    action: ConnectionAction
    connection_id: UUID
    result: NativeConnectionMutation | ConnectionProbeReceipt | None


class ConnectionPolicyRequest(_Value):
    actor_id: UUID
    fallback_allowlist: list[StrictStr] = Field(max_length=256)


class ConnectionPolicy(_Value):
    actor_id: UUID
    platform_role: PlatformRole
    allowlist: list[StrictStr] = Field(max_length=256)
    policy_source: Literal["database", "environment"]
    policy_digest: Digest


class CaptureConnectionRequest(_Value):
    actor_id: UUID
    connection_id: UUID
    access: ConnectionAccess


class ConnectionCapture(_Value):
    actor_id: UUID
    platform_role: PlatformRole
    connection_id: UUID
    access: ConnectionAccess
    connection: NativeConnectionFacts
    secret_enc: SecretEnvelope | None = Field(repr=False)
    source_snapshot: Digest


class CreateConnectionNativeRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    intent: CreateConnectionIntent
    config: dict[str, Any]
    secret_enc: SecretEnvelope = Field(repr=False)
    policy_digest: Digest
    fallback_allowlist: list[StrictStr] = Field(max_length=256)
    audit_context: ProjectDeletionAuditContext = Field(
        default_factory=ProjectDeletionAuditContext
    )


class UpdateConnectionNativeRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    connection_id: UUID
    intent: UpdateConnectionIntent
    config: dict[str, Any]
    secret_enc: SecretEnvelope | None = Field(repr=False)
    source_snapshot: Digest
    policy_digest: Digest | None
    fallback_allowlist: list[StrictStr] = Field(max_length=256)
    audit_context: ProjectDeletionAuditContext = Field(
        default_factory=ProjectDeletionAuditContext
    )


class UpdateConnectionAuthorizedRequest(UpdateConnectionNativeRequest):
    api_key_id: UUID | None = None
    required_scopes: list[StrictStr] = Field(default_factory=list)
    expected_proof: ConnectionAuthorityProof | None = None


class RecordConnectionTestRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    connection_id: UUID
    source_snapshot: Digest
    policy_digest: Digest
    fallback_allowlist: list[StrictStr] = Field(max_length=256)
    result: ConnectionProbeResult
    audit_context: ProjectDeletionAuditContext = Field(
        default_factory=ProjectDeletionAuditContext
    )


CONNECTION_NATIVE_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "list_storage_connections": (
        "actor_id",
        "after_created_at",
        "after_id",
        "page_size",
    ),
    "read_storage_connection": ("actor_id", "connection_id"),
    "read_storage_connection_policy": ("actor_id", "fallback_allowlist"),
    "capture_storage_connection": (
        "actor_id",
        "connection_id",
        "access",
    ),
    "read_storage_connection_receipt": (
        "operation_id",
        "actor_id",
        "action",
        "connection_id",
        "intent",
    ),
    "create_storage_connection": (
        "operation_id",
        "actor_id",
        "intent",
        "config",
        "secret_enc",
        "policy_digest",
        "fallback_allowlist",
        "audit_context",
    ),
    "update_storage_connection_authorized": (
        "operation_id",
        "actor_id",
        "connection_id",
        "intent",
        "config",
        "secret_enc",
        "source_snapshot",
        "policy_digest",
        "fallback_allowlist",
        "audit_context",
        "api_key_id",
        "required_scopes",
        "expected_proof",
    ),
    "delete_storage_connection": (
        "operation_id",
        "actor_id",
        "connection_id",
        "audit_context",
    ),
    "record_storage_connection_test": (
        "operation_id",
        "actor_id",
        "connection_id",
        "source_snapshot",
        "policy_digest",
        "fallback_allowlist",
        "result",
        "audit_context",
    ),
}
RUNTIME_NATIVE_SIGNATURES = {
    "begin_connection_probe": "uuid,uuid,uuid,text[]",
    "finish_connection_probe": "uuid,uuid,uuid,uuid,text[],jsonb,boolean",
}
RUNTIME_NATIVE_ARGUMENTS = {
    "begin_connection_probe": (
        "connection_id",
        "actor_id",
        "api_key_id",
        "required_scopes",
    ),
    "finish_connection_probe": (
        "operation_id",
        "connection_id",
        "actor_id",
        "api_key_id",
        "required_scopes",
        "expected_proof",
        "settled",
    ),
}
RUNTIME_NATIVE_OPERATIONS = {
    "begin_connection_probe": (BeginConnectionProbeRequest, ConnectionProbeLease),
    "finish_connection_probe": (FinishConnectionProbeRequest, ConnectionProbeFinish),
}

CONNECTION_NATIVE_SIGNATURES = {
    "list_storage_connections": "uuid,timestamptz,uuid,integer",
    "read_storage_connection": "uuid,uuid",
    "read_storage_connection_policy": "uuid,jsonb",
    "capture_storage_connection": "uuid,uuid,text",
    "read_storage_connection_receipt": "uuid,uuid,text,uuid,jsonb",
    "create_storage_connection": "uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb",
    "update_storage_connection_authorized": "uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb",
    "delete_storage_connection": "uuid,uuid,uuid,jsonb",
    "record_storage_connection_test": "uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb",
}
CONNECTION_NATIVE_OPERATIONS = {
    "list_storage_connections": (ListConnectionsRequest, NativeConnectionsPage),
    "read_storage_connection": (ReadConnectionRequest, NativeConnectionRead),
    "read_storage_connection_policy": (ConnectionPolicyRequest, ConnectionPolicy),
    "capture_storage_connection": (CaptureConnectionRequest, ConnectionCapture),
    "read_storage_connection_receipt": (
        ConnectionReceiptRequest,
        ConnectionReceiptLookup,
    ),
    "create_storage_connection": (
        CreateConnectionNativeRequest,
        NativeConnectionMutation,
    ),
    "update_storage_connection_authorized": (
        UpdateConnectionAuthorizedRequest,
        NativeConnectionMutation,
    ),
    "delete_storage_connection": (DeleteConnectionRequest, NativeConnectionMutation),
    "record_storage_connection_test": (
        RecordConnectionTestRequest,
        ConnectionProbeReceipt,
    ),
}
CONNECTION_OPERATIONS = {
    "list": (ListConnectionsRequest, ConnectionsPage),
    "read": (ReadConnectionRequest, ConnectionRead),
    "create": (CreateConnectionRequest, ConnectionMutation),
    "update": (UpdateConnectionRequest, ConnectionMutation),
    "delete": (DeleteConnectionRequest, ConnectionMutation),
    "test": (TestConnectionRequest, ConnectionProbeReceipt),
}
CONNECTION_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "account_not_active",
        "account_busy",
        "invalid_account_role",
        "manager_required",
        "global_creation_forbidden",
        "global_management_forbidden",
        "connection_management_forbidden",
        "connection_missing",
        "connection_busy",
        "source_changed",
        "policy_changed",
        "policy_busy",
        "policy_unavailable",
        "operation_conflict",
        "validation_failed",
        "host_denied",
        "crypto_not_configured",
        "crypto_error",
        "storage_unavailable",
        "storage_maintenance",
        "connection_revision_conflict",
        "api_key_authority_revoked",
        "operation_missing",
        "storage_maintenance_changed",
        "service_unavailable",
        "invalid_response",
        "resource_budget_exceeded",
    }
)


class ConnectionFailure(Exception):
    def __init__(self, code: str, message: str | None = None):
        if code not in CONNECTION_ERROR_CODES:
            raise ValueError("unknown connection failure")
        if message is not None and (
            not isinstance(message, str) or not 1 <= len(message) <= 2048
        ):
            raise ValueError("invalid connection failure message")
        self.code = code
        self.message = message
        super().__init__(code)

    def payload(self) -> dict[str, Any]:
        error = {"code": self.code}
        if self.message is not None:
            error["message"] = self.message
        return {"schema": CONNECTIONS_SCHEMA, "ok": False, "error": error}


_RESULT = TypeVar("_RESULT", bound=BaseModel)


PROBE_SCHEMA = "aap-assets-connection-probe/v1"


class _Failure(_Value):
    code: Literal[
        "invalid_request",
        "connection_missing",
        "connection_busy",
        "operation_conflict",
        "operation_missing",
        "storage_maintenance",
        "storage_maintenance_changed",
        "api_key_authority_revoked",
    ]


class _ProbeReply(_Value):
    schema_version: Literal["aap-assets-connection-probe/v1"] = Field(alias="schema")
    ok: StrictBool
    data: Any = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("successful probe reply contains only data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("failed probe reply contains only its error")
        return self


def decode_connection_probe_reply(payload: object, model: type[_Value]) -> _Value:
    if isinstance(payload, str):
        payload = json.loads(payload, parse_constant=_reject_constant)
    reply = _ProbeReply.model_validate(payload)
    if reply.schema_version != PROBE_SCHEMA:
        raise ValueError("Unexpected connection probe contract")
    if not reply.ok:
        assert reply.error is not None
        raise ConnectionFailure(reply.error.code)
    return model.model_validate(reply.data)


def decode_connection_reply(payload: object, result_type: type[_RESULT]) -> _RESULT:
    if isinstance(payload, str):
        payload = json.loads(payload, parse_constant=_reject_constant)
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != CONNECTIONS_SCHEMA
        or type(payload.get("ok")) is not bool
    ):
        raise ValueError("invalid connection response envelope")
    if not payload["ok"]:
        error = payload.get("error")
        if (
            set(payload) != {"schema", "ok", "error"}
            or not isinstance(error, dict)
            or not {"code"} <= set(error) <= {"code", "message"}
        ):
            raise ValueError("invalid connection failure envelope")
        raise ConnectionFailure(error["code"], error.get("message"))
    if set(payload) != {"schema", "ok", "data"} or not isinstance(
        payload["data"], dict
    ):
        raise ValueError("invalid connection success envelope")
    return result_type.model_validate(payload["data"])


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def require_connection_scope(request: BaseModel, result: BaseModel) -> None:
    for name in ("actor_id", "operation_id", "connection_id", "action", "access"):
        if hasattr(request, name) and getattr(request, name) != getattr(
            result, name, None
        ):
            raise ValueError("connection response escaped its request scope")
    if isinstance(request, (CreateConnectionRequest, CreateConnectionNativeRequest)):
        if (
            getattr(result, "connection_id", None) != request.operation_id
            or getattr(result, "action", None) != "create"
        ):
            raise ValueError("connection create response escaped its operation")
        intent = (
            request.intent
            if isinstance(request, CreateConnectionNativeRequest)
            else request
        )
        record = result.connection
        if (
            record is None
            or any(
                getattr(record, name) != getattr(intent, name)
                for name in ("name", "kind", "scope")
            )
            or record.created_by != request.actor_id
            or record.project_id is not None
            or not record.secret_set
        ):
            raise ValueError("connection create response changed its accepted values")
        if (
            isinstance(request, CreateConnectionNativeRequest)
            and record.config != request.config
        ):
            raise ValueError("connection create response changed its normalized config")
    elif isinstance(request, (UpdateConnectionRequest, UpdateConnectionNativeRequest)):
        if getattr(result, "action", None) != "update":
            raise ValueError("connection update response has another action")
        intent = (
            request.intent
            if isinstance(request, UpdateConnectionNativeRequest)
            else request
        )
        if intent.name is not None and result.connection.name != intent.name:
            raise ValueError("connection update response changed its accepted name")
        if isinstance(request, UpdateConnectionNativeRequest):
            if result.connection.config != request.config:
                raise ValueError(
                    "connection update response changed its normalized config"
                )
            if request.secret_enc is not None and not result.connection.secret_set:
                raise ValueError(
                    "connection update response lost its supplied credential"
                )
    elif isinstance(request, TestConnectionRequest):
        if getattr(result, "action", None) != "test":
            raise ValueError("connection test response has another action")
    elif isinstance(request, DeleteConnectionRequest):
        if getattr(result, "action", None) != "delete":
            raise ValueError("connection delete response has another action")
    elif isinstance(request, RecordConnectionTestRequest):
        if getattr(result, "action", None) != "test":
            raise ValueError("connection test response has another action")
    if isinstance(result, (NativeConnectionRead, ConnectionCapture)):
        if result.connection.id != result.connection_id:
            raise ValueError("connection record escaped its requested identity")
    if isinstance(result, ConnectionReceiptLookup) and result.result is not None:
        for name in ("operation_id", "actor_id", "connection_id", "action"):
            if getattr(result.result, name) != getattr(result, name):
                raise ValueError("cached connection result escaped its receipt scope")
    if isinstance(request, ListConnectionsRequest):
        if result.after != request.after or len(result.connections) > request.page_size:
            raise ValueError("connection page escaped its requested cursor")
        if len(result.connections) > result.total:
            raise ValueError("connection page exceeds its complete visible scope")
        if result.complete != (result.next_cursor is None):
            raise ValueError("connection page completion does not match its cursor")
        previous = request.after
        for connection in result.connections:
            current = ConnectionCursor(
                created_at=connection.created_at, id=connection.id
            )
            if previous is not None and (current.created_at, current.id) >= (
                previous.created_at,
                previous.id,
            ):
                raise ValueError("connection page did not advance in descending order")
            previous = current
        if result.next_cursor is not None and result.next_cursor != previous:
            raise ValueError("connection page cursor does not match its final record")
        if result.next_cursor is not None and not result.connections:
            raise ValueError("connection page advanced without records")


def connections_contract_document() -> dict[str, Any]:
    from pydantic.json_schema import models_json_schema

    models: dict[str, type[BaseModel]] = {}
    for request, result in (
        *CONNECTION_NATIVE_OPERATIONS.values(),
        *CONNECTION_OPERATIONS.values(),
        *RUNTIME_NATIVE_OPERATIONS.values(),
    ):
        for model in (request, result):
            models[model.__name__] = model
    _, document = models_json_schema(
        [(model, "validation") for model in models.values()]
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:aap:assets-connections:v1",
        "title": CONNECTIONS_SCHEMA,
        "schema": CONNECTIONS_SCHEMA,
        "failure_codes": sorted(CONNECTION_ERROR_CODES),
        "$defs": document["$defs"],
        "runtime": {
            name: {
                "signature": f"aap_assets_v1.{name}("
                + RUNTIME_NATIVE_SIGNATURES[name]
                + ")",
                "arguments": list(RUNTIME_NATIVE_ARGUMENTS[name]),
                "request": request.__name__,
                "result": result.__name__,
            }
            for name, (request, result) in RUNTIME_NATIVE_OPERATIONS.items()
        },
        "native": {
            name: {
                "signature": f"aap_assets_v1.{name}({CONNECTION_NATIVE_SIGNATURES[name]})",
                "arguments": list(CONNECTION_NATIVE_ARGUMENTS[name]),
                "request": pair[0].__name__,
                "result": pair[1].__name__,
            }
            for name, pair in CONNECTION_NATIVE_OPERATIONS.items()
        },
        "operations": {
            name: {
                "method": "POST",
                "path": f"/v1/connections/{name}",
                "request": pair[0].__name__,
                "result": pair[1].__name__,
            }
            for name, pair in CONNECTION_OPERATIONS.items()
        },
    }
