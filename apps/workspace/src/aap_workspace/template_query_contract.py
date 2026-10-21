"""Closed private contract for Workspace ProjectTemplate reads and projections."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import (
    Field,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_workspace_contract.wire import (
    ClosedModel,
    _json_bytes,
    _reject_constant,
    _unique_object,
)

TEMPLATE_QUERY_PROTOCOL = "aap-workspace-template-query/v1"
TemplateQueryOperation: TypeAlias = Literal[
    "list_templates", "get_template", "get_template_projection"
]
TemplateConfigurationOperation: TypeAlias = Literal[
    "create_template", "update_template", "duplicate_template"
]
TemplateQueryAction: TypeAlias = Literal[
    "organization_ids", "list_rows", "get_row", "projection_row", "creator_name"
]
TemplateQueryStatusCode: TypeAlias = Literal[404]

TEMPLATE_ROW_COLUMNS = (
    "id",
    "display_id",
    "name",
    "description",
    "type_label",
    "type_key",
    "data_type",
    "tool_bindings",
    "label_config",
    "ai_enabled",
    "sampling",
    "maximum_annotations",
    "show_overlap_first",
    "iou_dedup_threshold",
    "box_threshold",
    "text_threshold",
    "rendering_config",
    "annotation_guide",
    "scope",
    "organization_id",
    "created_by",
    "source_project_id",
    "usage_count",
    "created_at",
    "updated_at",
)
TEMPLATE_RESPONSE_COLUMNS = TEMPLATE_ROW_COLUMNS + ("created_by_name",)


class _TemplateQueryModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class TemplateQueryActor(ClosedModel):
    id: uuid.UUID
    role: StrictStr


class TemplateConfigurationReceipt(ClosedModel):
    operation_id: uuid.UUID
    operation: TemplateConfigurationOperation


class TemplateQueryRequest(_TemplateQueryModel):
    protocol: Literal["aap-workspace-template-query/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: TemplateQueryOperation
    admitted_actor: TemplateQueryActor
    target_id: uuid.UUID | None
    configuration_receipt: TemplateConfigurationReceipt | None
    filters_ascii: StrictStr | None

    @field_validator("filters_ascii")
    @classmethod
    def filters_are_ascii_json_text(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.isascii() or any(ord(char) < 0x20 for char in value)
        ):
            raise ValueError("filters_ascii must be ASCII without raw controls")
        return value

    @model_validator(mode="after")
    def operation_fields_match(self) -> TemplateQueryRequest:
        if self.operation == "list_templates":
            if (
                self.target_id is not None
                or self.configuration_receipt is not None
                or self.filters_ascii is None
            ):
                raise ValueError("List requests require only filters_ascii")
        elif self.operation == "get_template":
            if (
                self.target_id is None
                or self.configuration_receipt is not None
                or self.filters_ascii is not None
            ):
                raise ValueError("Detail requests require only target_id")
        elif (
            self.target_id is None
            or self.configuration_receipt is None
            or self.filters_ascii is not None
        ):
            raise ValueError("Projection requests require target and receipt")
        return self


class TemplateQueryFilters(ClosedModel):
    scope: StrictStr | None
    type_key: list[StrictStr] | None
    search: StrictStr | None


class TemplateQueryPolicy(_TemplateQueryModel):
    protocol: Literal["aap-workspace-template-query/v1"] = Field(alias="schema")


class TemplateQueryError(ClosedModel):
    status_code: TemplateQueryStatusCode
    detail: Literal["模板不存在"]
    headers: None


class TemplateQuerySuccess(_TemplateQueryModel):
    protocol: Literal["aap-workspace-template-query/v1"] = Field(alias="schema")
    ok: Literal[True]
    request: TemplateQueryRequest
    data_json: StrictStr

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class TemplateQueryFailure(_TemplateQueryModel):
    protocol: Literal["aap-workspace-template-query/v1"] = Field(alias="schema")
    ok: Literal[False]
    request: TemplateQueryRequest
    error: TemplateQueryError

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


TemplateQueryReply: TypeAlias = TemplateQuerySuccess | TemplateQueryFailure


class TemplateQueryOrganizationIdsFacts(ClosedModel):
    organization_ids: list[uuid.UUID]


class TemplateQueryListRowsFacts(ClosedModel):
    rows_json: StrictStr


class TemplateQueryRowFacts(ClosedModel):
    row_json: StrictStr | None


class TemplateQueryCreatorNameFacts(ClosedModel):
    name: StrictStr | None


class _TemplateQueryStep(_TemplateQueryModel):
    protocol: Literal["aap-workspace-template-query/v1"] = Field(alias="schema")
    request: TemplateQueryRequest


class TemplateQueryOrganizationIdsStep(_TemplateQueryStep):
    action: Literal["organization_ids"]
    facts: TemplateQueryOrganizationIdsFacts


class TemplateQueryListRowsStep(_TemplateQueryStep):
    action: Literal["list_rows"]
    facts: TemplateQueryListRowsFacts


class TemplateQueryGetRowStep(_TemplateQueryStep):
    action: Literal["get_row"]
    facts: TemplateQueryRowFacts


class TemplateQueryProjectionRowStep(_TemplateQueryStep):
    action: Literal["projection_row"]
    facts: TemplateQueryRowFacts


class TemplateQueryCreatorNameStep(_TemplateQueryStep):
    action: Literal["creator_name"]
    facts: TemplateQueryCreatorNameFacts


TemplateQueryStepReply: TypeAlias = (
    TemplateQueryOrganizationIdsStep
    | TemplateQueryListRowsStep
    | TemplateQueryGetRowStep
    | TemplateQueryProjectionRowStep
    | TemplateQueryCreatorNameStep
)

_REQUEST_ADAPTER = TypeAdapter(TemplateQueryRequest)
_POLICY_ADAPTER = TypeAdapter(TemplateQueryPolicy)
_REPLY_ADAPTER = TypeAdapter(TemplateQueryReply)
_STEP_ADAPTER = TypeAdapter(TemplateQueryStepReply)


class TemplateQueryUnavailable(Exception):
    """The selected Workspace template-query capability or its reply is invalid."""

    def __init__(self):
        super().__init__("Workspace template-query owner unavailable")


class TemplateQueryInvalidRequest(ValueError):
    """A private template-query request does not match the closed protocol."""

    def __init__(self):
        super().__init__("Invalid Workspace template-query request")


class TemplateQueryMissing(Exception):
    """A detail request targets a missing or hidden template."""

    def __init__(self):
        super().__init__("Template not found")


def _validated_request(request: TemplateQueryRequest) -> TemplateQueryRequest:
    if not isinstance(request, TemplateQueryRequest):
        raise TemplateQueryUnavailable()
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(request))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def decode_template_query_request(
    payload: dict | bytes | str | TemplateQueryRequest,
) -> TemplateQueryRequest:
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryInvalidRequest() from None


def decode_template_query_policy(payload: dict | bytes | str) -> TemplateQueryPolicy:
    try:
        return _POLICY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def _canonical_filters(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TemplateQueryInvalidRequest() from None


def template_query_request(
    operation: TemplateQueryOperation,
    admitted_actor_id: uuid.UUID,
    admitted_actor_role: str,
    *,
    target_id: uuid.UUID | None = None,
    configuration_receipt: TemplateConfigurationReceipt | dict | None = None,
    filters: Mapping[str, object] | None = None,
    request_id: uuid.UUID | None = None,
) -> TemplateQueryRequest:
    """Build a closed owner request from already admitted API facts."""
    filters_ascii = _canonical_filters(filters) if filters is not None else None
    try:
        receipt = (
            TemplateConfigurationReceipt.model_validate(
                {
                    **configuration_receipt,
                    "operation_id": uuid.UUID(
                        str(configuration_receipt["operation_id"])
                    ),
                }
            )
            if isinstance(configuration_receipt, dict)
            else configuration_receipt
        )
        return TemplateQueryRequest(
            schema=TEMPLATE_QUERY_PROTOCOL,
            request_id=request_id or uuid.uuid4(),
            operation=operation,
            admitted_actor=TemplateQueryActor(
                id=admitted_actor_id, role=admitted_actor_role
            ),
            target_id=target_id,
            configuration_receipt=receipt,
            filters_ascii=filters_ascii,
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryInvalidRequest() from None


def decode_template_query_filters(
    request: TemplateQueryRequest,
) -> TemplateQueryFilters:
    """Decode filters only after the required organization-membership phase."""
    request = _validated_request(request)
    if request.operation != "list_templates" or request.filters_ascii is None:
        raise TemplateQueryUnavailable()
    try:
        decoded = json.loads(
            request.filters_ascii,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if type(decoded) is not dict:
            raise ValueError("List filters must be an object")
        filters = TemplateQueryFilters.model_validate(decoded)
        if _canonical_filters(filters.model_dump(mode="json")) != request.filters_ascii:
            raise ValueError("List filters are not canonical")
        return filters
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def _validate_raw_document(text: str):
    if type(text) is not str:
        raise ValueError("Raw row must be JSON text")
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _parse_row_object(
    value: object,
    *,
    target_id: uuid.UUID | None = None,
    visibility_fields: bool = False,
) -> dict:
    if type(value) is not dict or set(value) != set(TEMPLATE_ROW_COLUMNS):
        raise ValueError("Raw template row columns do not match the fixed projection")
    if type(value["id"]) is not str or type(value["created_by"]) is not str:
        raise ValueError("Raw template row IDs must be UUID strings")
    row_id = uuid.UUID(value["id"])
    uuid.UUID(value["created_by"])
    if target_id is not None and row_id != target_id:
        raise ValueError("Raw template row ID does not match the request")
    if type(value["scope"]) is not str:
        raise ValueError("Raw template scope must be text")
    for field in (
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "created_at",
        "updated_at",
    ):
        if type(value[field]) is not str:
            raise ValueError(f"Raw template {field} must use its text codec")
    if visibility_fields and value["organization_id"] is not None:
        if type(value["organization_id"]) is not str:
            raise ValueError("Raw organization ID must be a UUID string")
        uuid.UUID(value["organization_id"])
    return value


def _validate_materialized_row(
    value: object, *, target_id: uuid.UUID | None = None
) -> dict:
    if type(value) is not dict or set(value) != set(TEMPLATE_RESPONSE_COLUMNS):
        raise ValueError("Template response columns are not exact")
    row = {key: item for key, item in value.items() if key != "created_by_name"}
    name = value["created_by_name"]
    if name is not None and type(name) is not str:
        raise ValueError("Creator name must be text or null")
    return _parse_row_object(row, target_id=target_id)


def _validate_data_root(request: TemplateQueryRequest, data_json: str) -> None:
    value = _validate_raw_document(data_json)
    if request.operation == "list_templates":
        if type(value) is not list:
            raise ValueError("List response data must be an array")
        for row in value:
            _validate_materialized_row(row)
        return
    if request.operation == "get_template":
        _validate_materialized_row(value, target_id=request.target_id)
        return
    if value is None:
        return
    _validate_materialized_row(value, target_id=request.target_id)


def template_query_success_reply(
    request: TemplateQueryRequest, data_json: str
) -> TemplateQuerySuccess:
    request = _validated_request(request)
    try:
        _validate_data_root(request, data_json)
        return TemplateQuerySuccess(
            schema=TEMPLATE_QUERY_PROTOCOL,
            ok=True,
            request=request,
            data_json=data_json,
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def template_query_failure_reply(
    request: TemplateQueryRequest,
) -> TemplateQueryFailure:
    request = _validated_request(request)
    return TemplateQueryFailure(
        schema=TEMPLATE_QUERY_PROTOCOL,
        ok=False,
        request=request,
        error=TemplateQueryError(
            status_code=404,
            detail="模板不存在",
            headers=None,
        ),
    )


def decode_template_query_reply(
    request: TemplateQueryRequest,
    payload: dict | bytes | str | TemplateQueryReply,
) -> TemplateQueryReply:
    request = _validated_request(request)
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(payload))
        if reply.request != request:
            raise ValueError("Template query reply does not echo the request")
        if isinstance(reply, TemplateQuerySuccess):
            _validate_data_root(request, reply.data_json)
        return reply
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def decode_template_query_step_reply(
    request: TemplateQueryRequest,
    action: TemplateQueryAction,
    payload: dict | bytes | str | TemplateQueryStepReply,
) -> TemplateQueryStepReply:
    request = _validated_request(request)
    try:
        reply = _STEP_ADAPTER.validate_json(_json_bytes(payload))
        if reply.request != request or reply.action != action:
            raise ValueError("Template query step reply is not bound to its request")
        return reply
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def materialize_template_query_data(
    reply: TemplateQuerySuccess,
) -> dict | list[dict] | None:
    """Restore the legacy Python scalar types after the public commit boundary."""
    request = _validated_request(reply.request)
    decoded = decode_template_query_reply(request, reply)
    if not isinstance(decoded, TemplateQuerySuccess):
        raise TemplateQueryUnavailable()
    try:
        value = _validate_raw_document(decoded.data_json)
        if request.operation == "list_templates":
            return [_materialize_row(row) for row in value]
        if value is None:
            return None
        return _materialize_row(value)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TemplateQueryUnavailable() from None


def _materialize_row(value: dict) -> dict:
    _validate_materialized_row(value)
    row = dict(value)
    for field in ("iou_dedup_threshold", "box_threshold", "text_threshold"):
        row[field] = float(row[field])
    for field in ("created_at", "updated_at"):
        timestamp = row[field]
        if timestamp == "infinity":
            row[field] = datetime.max
        elif timestamp == "-infinity":
            row[field] = datetime.min
        else:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=UTC)
            row[field] = parsed.astimezone(UTC)
    for field in ("id", "organization_id", "created_by", "source_project_id"):
        if row[field] is not None:
            row[field] = uuid.UUID(row[field])
    return row


__all__ = [
    "TEMPLATE_QUERY_PROTOCOL",
    "TEMPLATE_ROW_COLUMNS",
    "TEMPLATE_RESPONSE_COLUMNS",
    "TemplateConfigurationReceipt",
    "TemplateQueryAction",
    "TemplateQueryActor",
    "TemplateQueryCreatorNameFacts",
    "TemplateQueryCreatorNameStep",
    "TemplateQueryError",
    "TemplateQueryFailure",
    "TemplateQueryFilters",
    "TemplateQueryGetRowStep",
    "TemplateQueryInvalidRequest",
    "TemplateQueryListRowsFacts",
    "TemplateQueryListRowsStep",
    "TemplateQueryMissing",
    "TemplateQueryOperation",
    "TemplateQueryOrganizationIdsFacts",
    "TemplateQueryOrganizationIdsStep",
    "TemplateQueryPolicy",
    "TemplateQueryProjectionRowStep",
    "TemplateQueryReply",
    "TemplateQueryRequest",
    "TemplateQueryRowFacts",
    "TemplateQueryStepReply",
    "TemplateQuerySuccess",
    "TemplateQueryUnavailable",
    "decode_template_query_filters",
    "decode_template_query_policy",
    "decode_template_query_reply",
    "decode_template_query_request",
    "decode_template_query_step_reply",
    "materialize_template_query_data",
    "template_query_failure_reply",
    "template_query_request",
    "template_query_success_reply",
]
