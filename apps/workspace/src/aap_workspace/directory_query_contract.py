"""Closed wire contract for the Workspace project directory reads."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes
from aap_workspace_contract.project_query_contract import (
    ProjectQueryActorFacts,
    ProjectQueryMemberFacts,
)
from aap_workspace_contract.member_values import (
    PROJECT_MEMBER_COLUMNS,
    PROJECT_MEMBER_MENTION_COLUMNS,
)

DIRECTORY_QUERY_PROTOCOL = "aap-workspace-directory-query/v1"
DIRECTORY_MEMBER_RESPONSE_COLUMNS = (
    "id",
    "user_id",
    "user_name",
    "user_email",
    "role",
    "platform_role",
    "version",
    "assigned_at",
    "updated_at",
    "avatar_ref",
)
DIRECTORY_MENTION_RESPONSE_COLUMNS = (
    "user_id",
    "user_name",
    "user_email",
    "kind",
)

DirectoryQueryOperation: TypeAlias = Literal["list_members", "mention_candidates"]
DirectoryQueryAction: TypeAlias = Literal[
    "get_row",
    "actor_status",
    "member_access",
    "member_rows",
    "mention_owner",
    "mention_superadmins",
    "mention_members",
]


class _DirectoryQueryModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def schema_alias_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class DirectoryQueryRequest(_DirectoryQueryModel):
    protocol: Literal["aap-workspace-directory-query/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: DirectoryQueryOperation
    actor_id: uuid.UUID
    target_id: uuid.UUID


class DirectoryQueryPolicy(_DirectoryQueryModel):
    protocol: Literal["aap-workspace-directory-query/v1"] = Field(alias="schema")


class DirectoryQueryError(ClosedModel):
    status_code: Literal[401, 403, 404, 503]
    detail: Literal[
        "账号已停用或不存在",
        "账号角色无效，访问被拒绝",
        "项目不存在",
        "项目职责无效，访问被拒绝",
        "账号平台角色与项目职责不兼容",
        "身份资料服务暂不可用",
    ]
    headers: dict[Literal["WWW-Authenticate"], Literal["Bearer"]] | None

    @model_validator(mode="after")
    def valid_error_pair(self):
        expected = {
            "账号已停用或不存在": 401,
            "账号角色无效，访问被拒绝": 403,
            "项目不存在": 404,
            "项目职责无效，访问被拒绝": 403,
            "账号平台角色与项目职责不兼容": 403,
            "身份资料服务暂不可用": 503,
        }
        if self.status_code != expected[self.detail]:
            raise ValueError("Directory query status/detail mismatch")
        if (self.detail == "账号已停用或不存在") != (self.headers is not None):
            raise ValueError("Directory query authentication header mismatch")
        return self


class DirectoryQuerySuccess(_DirectoryQueryModel):
    protocol: Literal["aap-workspace-directory-query/v1"] = Field(alias="schema")
    ok: Literal[True]
    request: DirectoryQueryRequest
    data_json: StrictStr

    @model_validator(mode="before")
    @classmethod
    def exact_ok(cls, value):
        if isinstance(value, dict) and type(value.get("ok")) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class DirectoryQueryFailure(_DirectoryQueryModel):
    protocol: Literal["aap-workspace-directory-query/v1"] = Field(alias="schema")
    ok: Literal[False]
    request: DirectoryQueryRequest
    error: DirectoryQueryError

    @model_validator(mode="before")
    @classmethod
    def exact_ok(cls, value):
        if isinstance(value, dict) and type(value.get("ok")) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


DirectoryQueryReply: TypeAlias = Annotated[
    DirectoryQuerySuccess | DirectoryQueryFailure,
    Field(discriminator="ok"),
]


class DirectoryQueryRowFacts(ClosedModel):
    row_json: StrictStr | None


class DirectoryQueryProjectId(ClosedModel):
    project_id: uuid.UUID


class DirectoryQueryUserId(ClosedModel):
    user_id: uuid.UUID


class DirectoryQueryMemberRowsFacts(ClosedModel):
    rows_json: StrictStr


class DirectoryQueryMentionProfile(ClosedModel):
    id: uuid.UUID
    name: StrictStr
    email: StrictStr | None


class DirectoryQueryMentionOwnerAccount(DirectoryQueryMentionProfile):
    is_active: StrictBool


class DirectoryQueryMentionOwnerFacts(ClosedModel):
    user: DirectoryQueryMentionOwnerAccount | None


class DirectoryQueryMentionSuperadminsFacts(ClosedModel):
    accounts: list[DirectoryQueryMentionProfile]


DirectoryQueryParameters: TypeAlias = (
    DirectoryQueryProjectId | DirectoryQueryUserId | None
)
DirectoryQueryFacts: TypeAlias = (
    DirectoryQueryRowFacts
    | ProjectQueryActorFacts
    | ProjectQueryMemberFacts
    | DirectoryQueryMemberRowsFacts
    | DirectoryQueryMentionOwnerFacts
    | DirectoryQueryMentionSuperadminsFacts
)

DIRECTORY_QUERY_STEPS = {
    "get_row": (type(None), DirectoryQueryRowFacts),
    "actor_status": (type(None), ProjectQueryActorFacts),
    "member_access": (type(None), ProjectQueryMemberFacts),
    "member_rows": (DirectoryQueryProjectId, DirectoryQueryMemberRowsFacts),
    "mention_owner": (DirectoryQueryUserId, DirectoryQueryMentionOwnerFacts),
    "mention_superadmins": (type(None), DirectoryQueryMentionSuperadminsFacts),
    "mention_members": (DirectoryQueryProjectId, DirectoryQueryMemberRowsFacts),
}
DIRECTORY_QUERY_OPERATION_ACTIONS = {
    "list_members": {"get_row", "actor_status", "member_access", "member_rows"},
    "mention_candidates": {
        "get_row",
        "actor_status",
        "member_access",
        "mention_owner",
        "mention_superadmins",
        "mention_members",
    },
}


class DirectoryQueryStep(_DirectoryQueryModel):
    protocol: Literal["aap-workspace-directory-query/v1"] = Field(alias="schema")
    request: DirectoryQueryRequest
    action: DirectoryQueryAction
    parameters: DirectoryQueryParameters
    facts: DirectoryQueryFacts

    @model_validator(mode="after")
    def action_shapes_match(self):
        parameter_type, fact_type = DIRECTORY_QUERY_STEPS[self.action]
        if (
            type(self.parameters) is not parameter_type
            or type(self.facts) is not fact_type
        ):
            raise ValueError("Directory action arguments/facts do not match")
        if self.action not in DIRECTORY_QUERY_OPERATION_ACTIONS[self.request.operation]:
            raise ValueError("Directory action does not match its operation")
        if (
            isinstance(self.facts, ProjectQueryActorFacts)
            and self.facts.actor is not None
            and self.facts.actor.actor_id != self.request.actor_id
        ):
            raise ValueError("Current account does not match the directory actor")
        if (
            isinstance(self.facts, ProjectQueryMemberFacts)
            and self.facts.member is not None
            and (
                self.facts.member.project_id != self.request.target_id
                or self.facts.member.user_id != self.request.actor_id
            )
        ):
            raise ValueError("Current membership does not match the directory request")
        if (
            self.action == "mention_owner"
            and self.facts.user is not None
            and self.facts.user.id != self.parameters.user_id
        ):
            raise ValueError("Owner account does not match the fresh Project owner")
        if self.action == "mention_superadmins":
            ids = [account.id for account in self.facts.accounts]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate super administrator in directory facts")
        return self


DirectoryQueryStepReply: TypeAlias = DirectoryQueryStep

_REQUEST_ADAPTER = TypeAdapter(DirectoryQueryRequest)
_POLICY_ADAPTER = TypeAdapter(DirectoryQueryPolicy)
_REPLY_ADAPTER = TypeAdapter(DirectoryQueryReply)
_STEP_ADAPTER = TypeAdapter(DirectoryQueryStepReply)


class DirectoryQueryUnavailable(Exception):
    """The selected Workspace directory read or its facts are unavailable."""

    def __init__(self):
        super().__init__("Workspace directory query owner unavailable")


class DirectoryQueryInvalidRequest(ValueError):
    """A private Workspace directory request does not match the closed protocol."""

    def __init__(self):
        super().__init__("Invalid Workspace directory query request")


def _decode(model, payload, failure):
    try:
        return TypeAdapter(model).validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise failure() from None


def decode_directory_query_request(payload) -> DirectoryQueryRequest:
    return _decode(DirectoryQueryRequest, payload, DirectoryQueryInvalidRequest)


def decode_directory_query_policy(payload) -> DirectoryQueryPolicy:
    return _decode(DirectoryQueryPolicy, payload, DirectoryQueryUnavailable)


def decode_directory_query_step(request, action, parameters, payload):
    request = _decode(DirectoryQueryRequest, request, DirectoryQueryUnavailable)
    step = _decode(DirectoryQueryStepReply, payload, DirectoryQueryUnavailable)
    expected = (
        parameters.model_dump(mode="json")
        if isinstance(parameters, ClosedModel)
        else parameters
    )
    actual = (
        step.parameters.model_dump(mode="json") if step.parameters is not None else None
    )
    if step.request != request or step.action != action or actual != expected:
        raise DirectoryQueryUnavailable()
    return step


def directory_query_request(
    operation: DirectoryQueryOperation,
    actor_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    request_id: uuid.UUID | None = None,
) -> DirectoryQueryRequest:
    try:
        return decode_directory_query_request(
            {
                "schema": DIRECTORY_QUERY_PROTOCOL,
                "request_id": str(request_id or uuid.uuid4()),
                "operation": operation,
                "actor_id": str(actor_id),
                "target_id": str(target_id),
            }
        )
    except (TypeError, ValueError, UnicodeError):
        raise DirectoryQueryInvalidRequest() from None


def directory_query_schema() -> dict:
    schema = TypeAdapter(
        DirectoryQueryRequest
        | DirectoryQueryPolicy
        | DirectoryQueryReply
        | DirectoryQueryStep
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = DIRECTORY_QUERY_PROTOCOL
    conditions = []
    for action, (parameter_type, fact_type) in DIRECTORY_QUERY_STEPS.items():
        parameters = (
            {"type": "null"}
            if parameter_type is type(None)
            else {"$ref": f"#/$defs/{parameter_type.__name__}"}
        )
        conditions.append(
            {
                "if": {
                    "properties": {"action": {"const": action}},
                    "required": ["action"],
                },
                "then": {
                    "properties": {
                        "parameters": parameters,
                        "facts": {"$ref": f"#/$defs/{fact_type.__name__}"},
                    }
                },
            }
        )
    for operation, actions in DIRECTORY_QUERY_OPERATION_ACTIONS.items():
        conditions.append(
            {
                "if": {
                    "properties": {
                        "request": {
                            "properties": {"operation": {"const": operation}},
                            "required": ["operation"],
                        }
                    },
                    "required": ["request"],
                },
                "then": {"properties": {"action": {"enum": sorted(actions)}}},
            }
        )
    schema["$defs"]["DirectoryQueryStep"]["allOf"] = conditions
    schema["x-project-member-columns"] = list(PROJECT_MEMBER_COLUMNS)
    schema["x-project-member-response-columns"] = list(
        DIRECTORY_MEMBER_RESPONSE_COLUMNS
    )
    schema["x-project-member-mention-columns"] = list(PROJECT_MEMBER_MENTION_COLUMNS)
    schema["x-mention-response-columns"] = list(DIRECTORY_MENTION_RESPONSE_COLUMNS)
    schema["x-directory-query-codecs"] = {
        "timestamp": "aap-workspace-project-query UTC timestamp text codec",
        "rows_json": "fixed-column JSON text; duplicate keys rejected",
    }
    return schema
