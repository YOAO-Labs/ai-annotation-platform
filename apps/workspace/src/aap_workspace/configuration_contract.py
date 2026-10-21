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
from pydantic.json_schema import models_json_schema

from .guide_asset_policy import (
    MAX_GUIDE_ASSET_SIZE_BYTES,
    assert_guide_asset_key,
    assert_guide_asset_upload_target,
    sanitize_guide_asset_filename,
)
from aap_workspace_contract.access import ProjectAccessPolicyFailure, project_manager_access
from .configuration_policy import ConfigurationPolicyError
from aap_workspace_contract.wire import ClosedModel, _json_bytes, _reject_constant
from aap_workspace_contract.project_query_contract import (
    ProjectClassRenameEvidence,
    ProjectQueryInvalidRequest,
    ProjectQuerySuccess,
    ProjectQueryUnavailable,
    decode_project_query_reply,
    project_query_request,
)

CONFIGURATION_PROTOCOL = "aap-workspace-configuration/v1"

LegacyConfigurationOperation: TypeAlias = Literal[
    "create_project",
    "update_project",
    "create_template",
    "update_template",
    "delete_template",
    "duplicate_template",
]
ConfigurationGuideOperation: TypeAlias = Literal[
    "guide_asset_upload_init",
    "guide_asset_upload_complete",
    "guide_asset_delete",
    "guide_asset_sign",
]
ConfigurationOperation: TypeAlias = Literal[
    "create_project",
    "update_project",
    "create_template",
    "update_template",
    "delete_template",
    "duplicate_template",
    "rename_project_class",
    "guide_asset_upload_init",
    "guide_asset_upload_complete",
    "guide_asset_delete",
    "guide_asset_sign",
]
GUIDE_OPERATIONS = frozenset(
    {
        "guide_asset_upload_init",
        "guide_asset_upload_complete",
        "guide_asset_delete",
        "guide_asset_sign",
    }
)
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
    "class_rename_prepare",
    "class_rename_member",
    "class_rename_lock",
    "class_rename_apply",
    "guide_account_exists",
    "guide_read_project",
    "guide_actor_status",
    "guide_read_member",
    "guide_shared",
    "guide_member",
    "guide_lock",
    "guide_apply",
    "guide_refresh",
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
        if self.operation in GUIDE_OPERATIONS:
            selectors = self.selectors
            if self.target_id is None or (
                selectors.source_project_id is not None
                or selectors.template_id is not None
                or selectors.ml_backend_source_id is not None
                or selectors.ml_backend_id is not None
                or selectors.ml_backend_present
                or selectors.scope is not None
                or selectors.scope_present
                or selectors.organization_id is not None
                or selectors.organization_present
            ):
                raise ValueError("Guide operations require a target and null selectors")
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
    operation: LegacyConfigurationOperation
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


class ConfigurationRenameResult(ClosedModel):
    resource_id: uuid.UUID
    changed: StrictBool
    evidence: ProjectClassRenameEvidence
    projection: ProjectQuerySuccess | None


class ConfigurationRenameSuccess(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    operation: Literal["rename_project_class"]
    result: ConfigurationRenameResult

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


GuideExpiresInUpload = Annotated[StrictInt, Field(ge=900, le=900)]
GuideExpiresInDownload = Annotated[StrictInt, Field(ge=3600, le=3600)]
GuideAssetSize = Annotated[
    StrictInt,
    Field(ge=1, le=MAX_GUIDE_ASSET_SIZE_BYTES),
]
GuideAssetUrl = Annotated[StrictStr, Field(min_length=1)]
GuideUploadedAt = Annotated[StrictStr, Field(json_schema_extra={"format": "date-time"})]


class ConfigurationGuideUploadInitData(_ConfigurationModel):
    key: StrictStr
    upload_url: GuideAssetUrl
    expires_in: GuideExpiresInUpload


class ConfigurationGuideUploadCompleteData(_ConfigurationModel):
    key: StrictStr
    original_name: StrictStr
    content_type: StrictStr
    size: GuideAssetSize
    uploaded_at: GuideUploadedAt

    @field_validator("uploaded_at")
    @classmethod
    def uploaded_at_is_aware_iso(cls, value: str) -> str:
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("uploaded_at must be an ISO datetime") from None
        if timestamp.utcoffset() is None:
            raise ValueError("uploaded_at must include a timezone")
        return value


class ConfigurationGuideDeleteData(_ConfigurationModel):
    deleted: StrictStr


class ConfigurationGuideSignData(_ConfigurationModel):
    url: GuideAssetUrl
    expires_in: GuideExpiresInDownload


ConfigurationGuideData: TypeAlias = (
    ConfigurationGuideUploadInitData
    | ConfigurationGuideUploadCompleteData
    | ConfigurationGuideDeleteData
    | ConfigurationGuideSignData
)


class ConfigurationGuideResult(_ConfigurationModel):
    resource_id: uuid.UUID
    data_json: StrictStr
    refreshed: StrictBool

    @field_validator("data_json")
    @classmethod
    def data_json_is_ascii_without_raw_controls(cls, value: str) -> str:
        if not value.isascii() or any(ord(char) < 0x20 for char in value):
            raise ValueError("data_json must contain ASCII JSON without raw controls")
        return value


class ConfigurationGuideSuccess(_ConfigurationModel):
    protocol: Literal["aap-workspace-configuration/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    operation: ConfigurationGuideOperation
    result: ConfigurationGuideResult

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


ConfigurationReply: TypeAlias = (
    ConfigurationSuccess
    | ConfigurationRenameSuccess
    | ConfigurationGuideSuccess
    | ConfigurationFailure
)


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


_GUIDE_PAYLOAD_KEYS = {
    "guide_asset_upload_init": {"filename", "content_type", "size"},
    "guide_asset_upload_complete": {"key", "original_name", "content_type"},
    "guide_asset_delete": {"key"},
    "guide_asset_sign": {"key"},
}
_GUIDE_DATA_MODELS: dict[str, type[_ConfigurationModel]] = {
    "guide_asset_upload_init": ConfigurationGuideUploadInitData,
    "guide_asset_upload_complete": ConfigurationGuideUploadCompleteData,
    "guide_asset_delete": ConfigurationGuideDeleteData,
    "guide_asset_sign": ConfigurationGuideSignData,
}


def configuration_guide_payload(request: ConfigurationRequest) -> dict[str, Any]:
    """Decode exact guide keys after admission, without coercing their values."""
    request = _validate_internal_request(request)
    if request.operation not in GUIDE_OPERATIONS or request.target_id is None:
        raise ConfigurationUnavailable()
    try:
        payload = configuration_payload(request)
    except ConfigurationInvalidRequest:
        raise ConfigurationUnavailable() from None
    if (
        type(payload) is not dict
        or set(payload) != _GUIDE_PAYLOAD_KEYS[request.operation]
    ):
        raise ConfigurationUnavailable()
    return payload


def _canonical_guide_data(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_guide_data(
    operation: ConfigurationGuideOperation, data_json: str
) -> ConfigurationGuideData:
    try:
        value = json.loads(
            data_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if type(value) is not dict or _canonical_guide_data(value) != data_json:
            raise ValueError("Guide data is not canonical JSON")
        return _GUIDE_DATA_MODELS[operation].model_validate(value)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationUnavailable() from None


def _bind_guide_data(
    request: ConfigurationRequest, data: ConfigurationGuideData
) -> None:
    payload = configuration_guide_payload(request)
    target_id = request.target_id
    if target_id is None:
        raise ConfigurationUnavailable()
    try:
        if isinstance(data, ConfigurationGuideUploadInitData):
            assert_guide_asset_key(data.key, target_id)
            assert_guide_asset_upload_target(
                target_id, data.key, payload["content_type"]
            )
            filename = payload["filename"]
            if type(filename) is not str or not data.key.endswith(
                "-" + sanitize_guide_asset_filename(filename)
            ):
                raise ConfigurationUnavailable()
        elif isinstance(data, ConfigurationGuideUploadCompleteData):
            original_name = payload["original_name"]
            assert_guide_asset_upload_target(
                target_id, payload["key"], payload["content_type"]
            )
            if (
                data.key != payload["key"]
                or data.content_type != payload["content_type"]
                or type(original_name) is not str
                or data.original_name != sanitize_guide_asset_filename(original_name)
            ):
                raise ConfigurationUnavailable()
        elif isinstance(data, ConfigurationGuideDeleteData):
            assert_guide_asset_key(payload["key"], target_id)
            if data.deleted != payload["key"]:
                raise ConfigurationUnavailable()
        elif isinstance(data, ConfigurationGuideSignData):
            assert_guide_asset_key(payload["key"], target_id)
        else:
            raise ConfigurationUnavailable()
    except ConfigurationPolicyError:
        raise ConfigurationUnavailable() from None
    except (AttributeError, TypeError, ValueError):
        raise ConfigurationUnavailable() from None


def configuration_guide_data(
    reply: ConfigurationGuideSuccess,
) -> ConfigurationGuideData:
    """Decode the public result payload from a validated guide success envelope."""
    try:
        validated = ConfigurationGuideSuccess.model_validate(
            reply.model_dump(mode="python", by_alias=True)
        )
    except (ValidationError, TypeError, ValueError):
        raise ConfigurationUnavailable() from None
    return _parse_guide_data(validated.operation, validated.result.data_json)


def configuration_rename_payload(
    request: ConfigurationRequest,
) -> tuple[str, str, str | None]:
    request = _validate_internal_request(request)
    if request.operation != "rename_project_class":
        raise ConfigurationUnavailable()
    try:
        payload = configuration_payload(request)
    except ConfigurationInvalidRequest:
        raise ConfigurationUnavailable() from None
    if (
        type(payload) is not dict
        or set(payload) != {"old_name", "new_name", "tool_unit_id"}
        or type(payload.get("old_name")) is not str
        or type(payload.get("new_name")) is not str
        or (
            payload.get("tool_unit_id") is not None
            and type(payload.get("tool_unit_id")) is not str
        )
    ):
        raise ConfigurationUnavailable()
    return (
        payload["old_name"].strip(),
        payload["new_name"].strip(),
        payload["tool_unit_id"],
    )


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


def configuration_guide_success_reply(
    request: ConfigurationRequest,
    *,
    resource_id: uuid.UUID,
    data: dict[str, Any],
    refreshed: bool,
) -> ConfigurationGuideSuccess:
    request = _validate_internal_request(request)
    if (
        request.operation not in GUIDE_OPERATIONS
        or request.target_id is None
        or resource_id != request.target_id
    ):
        raise ConfigurationUnavailable()
    try:
        typed_data = _GUIDE_DATA_MODELS[request.operation].model_validate(data)
        data_json = _canonical_guide_data(typed_data.model_dump(mode="json"))
        reply = ConfigurationGuideSuccess(
            schema=CONFIGURATION_PROTOCOL,
            ok=True,
            operation=request.operation,
            operation_id=request.operation_id,
            result=ConfigurationGuideResult(
                resource_id=resource_id,
                data_json=data_json,
                refreshed=refreshed,
            ),
        )
        _bind_guide_data(request, typed_data)
        if (
            request.operation in {"guide_asset_upload_init", "guide_asset_sign"}
            and reply.result.refreshed
        ):
            raise ConfigurationUnavailable()
        return reply
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationUnavailable() from None


def configuration_rename_success_reply(
    request: ConfigurationRequest,
    *,
    resource_id: uuid.UUID,
    changed: bool,
    evidence: ProjectClassRenameEvidence,
    projection: ProjectQuerySuccess | None,
) -> ConfigurationRenameSuccess:
    request = _validate_internal_request(request)
    if request.operation != "rename_project_class":
        raise ConfigurationUnavailable()
    try:
        return ConfigurationRenameSuccess(
            schema=CONFIGURATION_PROTOCOL,
            ok=True,
            operation_id=request.operation_id,
            operation="rename_project_class",
            result=ConfigurationRenameResult(
                resource_id=resource_id,
                changed=changed,
                evidence=evidence,
                projection=projection,
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
    elif isinstance(reply, ConfigurationGuideSuccess):
        if (
            request.operation not in GUIDE_OPERATIONS
            or request.target_id is None
            or reply.result.resource_id != request.target_id
            or (
                request.operation in {"guide_asset_upload_init", "guide_asset_sign"}
                and reply.result.refreshed
            )
        ):
            raise ConfigurationUnavailable()
        data = configuration_guide_data(reply)
        _bind_guide_data(request, data)
    elif isinstance(reply, ConfigurationRenameSuccess):
        old_name, new_name, _tool_unit_id = configuration_rename_payload(request)
        expected_changed = bool(old_name and new_name and old_name != new_name)
        result = reply.result
        evidence = result.evidence
        if (
            request.operation != "rename_project_class"
            or request.target_id is None
            or result.resource_id != request.target_id
            or evidence.operation_id != request.operation_id
            or evidence.actor_id != request.actor_id
            or evidence.project_id != request.target_id
            or not evidence.is_active
            or not old_name
            or not new_name
            or result.changed != expected_changed
            or (result.changed and result.projection is not None)
        ):
            raise ConfigurationUnavailable()
        try:
            access = project_manager_access(
                actor_id=evidence.actor_id,
                platform_role=evidence.platform_role,
                project_id=evidence.project_id,
                owner_id=evidence.owner_id,
            )
        except ProjectAccessPolicyFailure:
            raise ConfigurationUnavailable() from None
        if access is None or not access.is_manager:
            raise ConfigurationUnavailable()
        if result.projection is not None:
            try:
                expected = project_query_request(
                    "get_project_projection",
                    evidence.actor_id,
                    evidence.platform_role,
                    target_id=evidence.project_id,
                    projection_evidence=evidence,
                    request_id=request.operation_id,
                )
                projection = decode_project_query_reply(expected, result.projection)
            except (ProjectQueryInvalidRequest, ProjectQueryUnavailable):
                raise ConfigurationUnavailable() from None
            if not isinstance(projection, ProjectQuerySuccess):
                raise ConfigurationUnavailable()
            reply = reply.model_copy(
                update={"result": result.model_copy(update={"projection": projection})}
            )
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


def configuration_schema() -> dict[str, Any]:
    """Return the complete closed request, reply and fixed-step JSON Schema."""
    schema = TypeAdapter(
        ConfigurationRequest
        | ConfigurationPolicy
        | ConfigurationReply
        | ConfigurationStepReply
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = CONFIGURATION_PROTOCOL
    _, guide_schema = models_json_schema(
        [
            (model, "validation")
            for model in (
                ConfigurationGuideUploadInitData,
                ConfigurationGuideUploadCompleteData,
                ConfigurationGuideDeleteData,
                ConfigurationGuideSignData,
            )
        ]
    )
    schema["$defs"].update(guide_schema["$defs"])
    schema["x-guide-asset-data-json"] = {
        "canonical_ascii_json": True,
        "operations": {
            "guide_asset_upload_init": {
                "payload_fields": ["filename", "content_type", "size"],
                "data_schema": "#/$defs/ConfigurationGuideUploadInitData",
            },
            "guide_asset_upload_complete": {
                "payload_fields": ["key", "original_name", "content_type"],
                "data_schema": "#/$defs/ConfigurationGuideUploadCompleteData",
            },
            "guide_asset_delete": {
                "payload_fields": ["key"],
                "data_schema": "#/$defs/ConfigurationGuideDeleteData",
            },
            "guide_asset_sign": {
                "payload_fields": ["key"],
                "data_schema": "#/$defs/ConfigurationGuideSignData",
            },
        },
    }
    return schema
