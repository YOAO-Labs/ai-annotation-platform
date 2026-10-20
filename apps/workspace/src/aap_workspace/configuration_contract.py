"""Closed private wire contracts for Workspace configuration ownership."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
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

from .contract import ClosedModel, _json_bytes, _reject_constant

CONFIGURATION_PROTOCOL = "aap-workspace-configuration/v1"

ConfigurationOperation: TypeAlias = Literal[
    "create_project",
    "update_project",
    "create_template",
    "update_template",
    "delete_template",
    "duplicate_template",
]
ConfigurationAction: TypeAlias = Literal[
    "begin",
    "backend",
    "source_pool",
    "video_annotations",
    "video_trackers",
    "video_items",
    "video_media",
    "task_count",
    "lock_config",
    "datasets",
    "dataset_scenes",
    "authority",
    "allocate",
    "insert",
    "enable_backend",
    "binding",
    "update",
    "delete",
    "finish",
]
ConfigurationStatusCode: TypeAlias = Annotated[StrictInt, Field(ge=400, le=599)]


class _ConfigurationModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class ConfigurationSelectors(ClosedModel):
    source_project_id: uuid.UUID | None
    template_id: uuid.UUID | None
    ml_backend_source_id: uuid.UUID | None
    ml_backend_id: uuid.UUID | None
    ml_backend_present: StrictBool
    scope: StrictStr | None
    scope_present: StrictBool
    organization_id: uuid.UUID | None
    organization_present: StrictBool


class ConfigurationRequest(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    operation: ConfigurationOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID | None
    selectors: ConfigurationSelectors
    payload_ascii: StrictStr

    @field_validator("payload_ascii")
    @classmethod
    def payload_is_ascii_without_raw_controls(cls, value: str) -> str:
        if not value.isascii() or any(ord(char) < 0x20 for char in value):
            raise ValueError(
                "payload_ascii must contain ASCII JSON without raw controls"
            )
        return value

    @model_validator(mode="after")
    def create_targets_are_null(self) -> ConfigurationRequest:
        is_create = self.operation in {"create_project", "create_template"}
        if is_create != (self.target_id is None):
            raise ValueError("target_id must be null only for create operations")
        return self


class ConfigurationPolicy(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")


class ConfigurationErrorDetails(ClosedModel):
    status_code: ConfigurationStatusCode
    detail: JsonValue
    headers: dict[StrictStr, StrictStr] | None
    rollback: StrictBool


class ConfigurationResult(ClosedModel):
    resource_id: uuid.UUID
    template_id: uuid.UUID | None


class ConfigurationSuccess(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    operation: ConfigurationOperation
    result: ConfigurationResult

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class ConfigurationFailure(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    ok: Literal[False]
    operation_id: uuid.UUID
    operation: ConfigurationOperation
    error: ConfigurationErrorDetails

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


ConfigurationReply: TypeAlias = ConfigurationSuccess | ConfigurationFailure


class ConfigurationStepSuccess(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    operation: ConfigurationOperation
    action: ConfigurationAction
    facts: dict[StrictStr, Any]

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class ConfigurationStepFailure(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    ok: Literal[False]
    operation_id: uuid.UUID
    operation: ConfigurationOperation
    action: ConfigurationAction
    error: ConfigurationErrorDetails

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


ConfigurationStepReply: TypeAlias = ConfigurationStepSuccess | ConfigurationStepFailure


class ConfigurationError(Exception):
    """A portable public error with the legacy HTTP response semantics."""

    def __init__(
        self,
        status_code: int,
        detail: JsonValue,
        headers: dict[str, str] | None = None,
        rollback: bool = False,
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        self.rollback = rollback


class ConfigurationUnavailable(Exception):
    """The native Workspace configuration capability is unavailable."""

    def __init__(self):
        super().__init__("Workspace configuration unavailable")


class ConfigurationInvalidRequest(ValueError):
    """A request or opaque payload does not match the frozen wire contract."""

    def __init__(self):
        super().__init__("Invalid Workspace configuration request")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _allow_json_constant(value: str) -> float:
    return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}[value]


def _legacy_json_ready(value: Any) -> Any:
    """Convert post-validation Python values as a Pydantic JSON dump would."""
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _legacy_json_ready(value.model_dump(mode="python"))
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _legacy_json_ready(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            result[key] = _legacy_json_ready(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_legacy_json_ready(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Invalid legacy JSON value")


def _legacy_json(value: Any) -> str:
    return json.dumps(
        _legacy_json_ready(value),
        ensure_ascii=True,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _selector_uuid(payload: dict[str, Any], name: str) -> uuid.UUID | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    if type(value) is str:
        try:
            return uuid.UUID(value)
        except ValueError:
            pass
    raise ValueError(f"Invalid {name} selector")


def _selectors_for_payload(payload: dict[str, Any]) -> ConfigurationSelectors:
    try:
        return ConfigurationSelectors(
            source_project_id=_selector_uuid(payload, "source_project_id"),
            template_id=_selector_uuid(payload, "template_id"),
            ml_backend_source_id=_selector_uuid(payload, "ml_backend_source_id"),
            ml_backend_id=_selector_uuid(payload, "ml_backend_id"),
            ml_backend_present="ml_backend_id" in payload,
            scope=payload.get("scope"),
            scope_present="scope" in payload,
            organization_id=_selector_uuid(payload, "organization_id"),
            organization_present="organization_id" in payload,
        )
    except (ValidationError, TypeError, ValueError):
        raise ConfigurationInvalidRequest() from None


_REQUEST_ADAPTER = TypeAdapter(ConfigurationRequest)
_POLICY_ADAPTER = TypeAdapter(ConfigurationPolicy)
_REPLY_ADAPTER = TypeAdapter(ConfigurationReply)
_STEP_REPLY_ADAPTER = TypeAdapter(ConfigurationStepReply)


def _validate_internal_request(request: ConfigurationRequest) -> ConfigurationRequest:
    if not isinstance(request, ConfigurationRequest):
        raise ConfigurationUnavailable()
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(request))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationUnavailable() from None


def decode_configuration_request(
    payload: dict | bytes | str | ConfigurationRequest,
) -> ConfigurationRequest:
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationInvalidRequest() from None


def decode_configuration_policy(payload: dict | bytes | str) -> ConfigurationPolicy:
    try:
        return _POLICY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationUnavailable() from None


def configuration_request(
    operation: ConfigurationOperation,
    actor_id: uuid.UUID,
    target_id: uuid.UUID | None,
    payload: dict[str, Any],
    operation_id: uuid.UUID | None = None,
) -> ConfigurationRequest:
    """Build the wire request from an already public-validated payload mapping."""
    if not isinstance(payload, dict):
        raise ConfigurationInvalidRequest()
    try:
        return ConfigurationRequest(
            schema=CONFIGURATION_PROTOCOL,
            operation_id=operation_id or uuid.uuid4(),
            operation=operation,
            actor_id=actor_id,
            target_id=target_id,
            selectors=_selectors_for_payload(payload),
            payload_ascii=_legacy_json(payload),
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationInvalidRequest() from None


def configuration_payload(request: ConfigurationRequest) -> dict[str, Any]:
    """Decode and cross-check the opaque payload after admission has succeeded."""
    request = _validate_internal_request(request)
    try:
        decoded = json.loads(
            request.payload_ascii,
            object_pairs_hook=_unique_object,
            parse_constant=_allow_json_constant,
        )
        if type(decoded) is not dict or _legacy_json(decoded) != request.payload_ascii:
            raise ValueError("Payload is not canonical legacy JSON")
        if _selectors_for_payload(decoded) != request.selectors:
            raise ValueError("Payload selectors do not match request selectors")
        return decoded
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationInvalidRequest() from None


def configuration_failure_reply(
    request: ConfigurationRequest,
    error: ConfigurationError,
) -> ConfigurationFailure:
    request = _validate_internal_request(request)
    try:
        detail = ConfigurationErrorDetails(
            status_code=error.status_code,
            detail=error.detail,
            headers=error.headers,
            rollback=error.rollback,
        )
        return ConfigurationFailure(
            schema=CONFIGURATION_PROTOCOL,
            ok=False,
            operation_id=request.operation_id,
            operation=request.operation,
            error=detail,
        )
    except (ValidationError, TypeError, ValueError):
        raise ConfigurationUnavailable() from None


def configuration_success_reply(
    request: ConfigurationRequest,
    resource_id: uuid.UUID,
    template_id: uuid.UUID | None = None,
) -> ConfigurationSuccess:
    request = _validate_internal_request(request)
    try:
        return ConfigurationSuccess(
            schema=CONFIGURATION_PROTOCOL,
            ok=True,
            operation_id=request.operation_id,
            operation=request.operation,
            result=ConfigurationResult(
                resource_id=resource_id,
                template_id=template_id,
            ),
        )
    except (ValidationError, TypeError, ValueError):
        raise ConfigurationUnavailable() from None


def decode_configuration_reply(
    request: ConfigurationRequest,
    payload: dict | bytes | str | ConfigurationReply,
) -> ConfigurationReply:
    request = _validate_internal_request(request)
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationUnavailable() from None
    if (
        reply.protocol != CONFIGURATION_PROTOCOL
        or reply.operation_id != request.operation_id
        or reply.operation != request.operation
    ):
        raise ConfigurationUnavailable()
    if isinstance(reply, ConfigurationSuccess):
        operation = request.operation
        if operation in {"update_project", "update_template", "delete_template"}:
            if reply.result.resource_id != request.target_id:
                raise ConfigurationUnavailable()
        elif operation == "duplicate_template":
            if reply.result.resource_id == request.target_id:
                raise ConfigurationUnavailable()

        expected_template_id = (
            request.selectors.template_id if operation == "create_project" else None
        )
        if reply.result.template_id != expected_template_id:
            raise ConfigurationUnavailable()
    return reply


def decode_configuration_step_reply(
    request: ConfigurationRequest,
    action: ConfigurationAction,
    payload: dict | bytes | str | ConfigurationStepReply,
) -> ConfigurationStepReply:
    request = _validate_internal_request(request)
    try:
        if isinstance(payload, (ConfigurationStepSuccess, ConfigurationStepFailure)):
            raw = payload.model_dump(mode="json", by_alias=True, exclude={"facts"})
            if isinstance(payload, ConfigurationStepSuccess):
                raw["facts"] = payload.facts
        elif isinstance(payload, (str, bytes)):
            raw = json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        elif isinstance(payload, dict):
            raw = dict(payload)
        else:
            raise ValueError("Configuration reply must be a JSON object")
        if type(raw) is not dict:
            raise ValueError("Configuration reply must be a JSON object")
        facts = None
        if raw.get("ok") is True and "facts" in raw:
            facts = raw["facts"]
            if type(facts) is not dict:
                raise ValueError("Configuration facts must be a JSON object")
            # Stored configuration has the existing JSON limits. Do not apply
            # the small envelope's depth cap to arbitrary nested config values.
            json.dumps(facts, ensure_ascii=True, allow_nan=False)
            pending = [facts]
            while pending:
                value = pending.pop()
                if type(value) is dict:
                    if any(type(key) is not str for key in value):
                        raise ValueError("JSON object keys must be strings")
                    pending.extend(value.values())
                elif type(value) is list:
                    pending.extend(value)
                elif value is not None and type(value) not in (str, bool, int, float):
                    raise ValueError("Invalid JSON configuration fact")
            raw = {**raw, "facts": {}}
        reply = _STEP_REPLY_ADAPTER.validate_json(_json_bytes(raw))
        if facts is not None:
            reply = reply.model_copy(update={"facts": facts})
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationUnavailable() from None
    if (
        reply.protocol != CONFIGURATION_PROTOCOL
        or reply.operation_id != request.operation_id
        or reply.operation != request.operation
        or reply.action != action
    ):
        raise ConfigurationUnavailable()
    return reply
