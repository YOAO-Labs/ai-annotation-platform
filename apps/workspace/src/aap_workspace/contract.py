"""Closed Workspace membership request, result, and native wire contracts."""

from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime
from typing import Literal, TypeAlias

from aap_workspace_contract.wire import (
    ClosedModel as _ClosedModel,
    _json_bytes,
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

MEMBERSHIP_PROTOCOL = "aap-workspace-membership/v1"
MEMBERSHIP_NATIVE_PROTOCOL = "aap-workspace-membership-native/v1"
MEMBERSHIP_NATIVE_TEXT_CODEC = "json-string-ascii/v1"
MEMBERSHIP_NATIVE_TEXT_FIELDS = ("preview_token", "reason")
LABELING_MEMBERSHIP_WORK_PROTOCOL = "aap-labeling-membership-work/v1"


def native_membership_command_json(command: dict) -> str:
    """Carry opaque text through JSONB without changing logical HTTP values."""
    request = command["request"] if "request" in command else command
    if request["operation"] == "change_role":
        payload = dict(request["payload"])
        for field in MEMBERSHIP_NATIVE_TEXT_FIELDS:
            payload[field] = json.dumps(payload[field], ensure_ascii=True)
        request = {**request, "payload": payload}
        command = {**command, "request": request} if "request" in command else request
    return json.dumps(command, ensure_ascii=True, allow_nan=False)


MembershipOperation: TypeAlias = Literal[
    "add_member", "preview_role_change", "change_role", "remove_member"
]
MembershipProjectRole: TypeAlias = Literal["annotator", "reviewer", "viewer"]
JsonObject: TypeAlias = dict[StrictStr, JsonValue]


class _MembershipModel(_ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class _NativeModel(_ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class MembershipAuditContext(_ClosedModel):
    method: StrictStr | None
    path: StrictStr | None
    ip: StrictStr | None
    request_id: StrictStr | None


class AddMemberPayload(_ClosedModel):
    target_user_id: uuid.UUID
    project_role: StrictStr


class PreviewRoleChangePayload(_ClosedModel):
    member_id: uuid.UUID
    target_role: StrictStr
    replacement_annotator_id: uuid.UUID | None
    replacement_reviewer_id: uuid.UUID | None


class ChangeRolePayload(_ClosedModel):
    member_id: uuid.UUID
    target_role: StrictStr
    replacement_annotator_id: uuid.UUID | None
    replacement_reviewer_id: uuid.UUID | None
    expected_version: StrictInt
    preview_token: StrictStr
    reason: StrictStr


class RemoveMemberPayload(_ClosedModel):
    member_id: uuid.UUID


MembershipPayload: TypeAlias = (
    AddMemberPayload
    | PreviewRoleChangePayload
    | ChangeRolePayload
    | RemoveMemberPayload
)


class MembershipRequest(_MembershipModel):
    protocol: Literal["aap-workspace-membership/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    operation: MembershipOperation
    actor_id: uuid.UUID
    project_id: uuid.UUID
    payload: MembershipPayload
    audit_context: MembershipAuditContext

    @model_validator(mode="after")
    def payload_matches_operation(self) -> MembershipRequest:
        expected = {
            "add_member": AddMemberPayload,
            "preview_role_change": PreviewRoleChangePayload,
            "change_role": ChangeRolePayload,
            "remove_member": RemoveMemberPayload,
        }[self.operation]
        if type(self.payload) is not expected:
            raise ValueError("Membership payload does not match its operation")
        return self


class MembershipRecord(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: StrictStr
    assigned_by: uuid.UUID | None
    version: StrictInt
    assigned_at: datetime
    updated_at: datetime
    weekly_target: StrictInt | None

    @field_validator("assigned_at", "updated_at", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid membership timestamp") from None
        return value

    @field_validator("assigned_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Membership timestamps must be timezone-aware")
        return value


class MembershipMemberData(_ClosedModel):
    member: MembershipRecord


class MembershipRemovedData(_ClosedModel):
    member_id: uuid.UUID
    removed: Literal[True]

    @field_validator("removed", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("removed must be a JSON boolean")
        return value


_RESOURCE_SNAPSHOT_FIELDS = frozenset(
    {
        "member",
        "member_account",
        "project",
        "annotation_task_ids",
        "review_task_ids",
        "unassigned_review_task_ids",
        "lock_task_ids",
        "review_claim_task_ids",
        "materialization_annotation_task_ids",
        "materialization_review_task_ids",
        "batch_annotator_ids",
        "batch_reviewer_ids",
        "eligible_reviewer_ids",
        "tasks",
        "batches",
        "receivers",
    }
)


class MembershipPreview(_ClosedModel):
    member_id: uuid.UUID
    user_id: uuid.UUID
    current_role: StrictStr
    current_version: StrictInt
    target_role: StrictStr
    requires_handoff: StrictBool
    blockers: list[StrictStr]
    blocker_details: list[JsonObject]
    resource_snapshot: JsonObject
    preview_token: StrictStr

    @field_validator("resource_snapshot")
    @classmethod
    def exact_snapshot_shape(cls, value: JsonObject) -> JsonObject:
        if set(value) != _RESOURCE_SNAPSHOT_FIELDS:
            raise ValueError("Membership snapshot fields are not exact")
        return value


MembershipReplyData: TypeAlias = (
    MembershipMemberData | MembershipPreview | MembershipRemovedData
)


class MembershipError(_ClosedModel):
    status_code: StrictInt = Field(ge=400, le=599)
    detail: JsonValue
    headers: dict[StrictStr, StrictStr] | None
    rollback: StrictBool


class MembershipSuccess(_MembershipModel):
    protocol: Literal["aap-workspace-membership/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    operation: MembershipOperation
    actor_id: uuid.UUID
    project_id: uuid.UUID
    data: MembershipReplyData

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class MembershipFailure(_MembershipModel):
    protocol: Literal["aap-workspace-membership/v1"] = Field(alias="schema")
    ok: Literal[False]
    operation_id: uuid.UUID
    operation: MembershipOperation
    actor_id: uuid.UUID
    project_id: uuid.UUID
    error: MembershipError

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


MembershipReply: TypeAlias = MembershipSuccess | MembershipFailure


class MembershipPolicyReply(_NativeModel):
    protocol: Literal["aap-workspace-membership-native/v1"] = Field(alias="schema")
    opaque_text_codec: Literal["json-string-ascii/v1"]
    operations: list[MembershipOperation]

    @field_validator("operations")
    @classmethod
    def exact_operations(cls, value: list[MembershipOperation]):
        expected = [
            "add_member",
            "preview_role_change",
            "change_role",
            "remove_member",
        ]
        if value != expected:
            raise ValueError("Native membership capability set is not exact")
        return value


class MembershipActorFacts(_ClosedModel):
    id: uuid.UUID
    name: StrictStr
    email: StrictStr
    role: StrictStr
    is_active: StrictBool


class MembershipProjectFacts(_ClosedModel):
    id: uuid.UUID
    owner_id: uuid.UUID | None


class MembershipAccountFacts(_ClosedModel):
    id: uuid.UUID
    role: StrictStr
    is_active: StrictBool


class MembershipReceiverFacts(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: StrictStr
    version: StrictInt


class MembershipTaskFacts(_ClosedModel):
    id: uuid.UUID
    status: StrictStr
    version: StrictInt
    batch_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    assignee_is_override: StrictBool
    reviewer_id: uuid.UUID | None
    reviewer_is_override: StrictBool
    reviewer_claimed: StrictBool
    review_round_id: uuid.UUID | None
    review_submitter_id: uuid.UUID | None
    annotation_contributor_ids: JsonValue
    review_contributor_ids: JsonValue
    batch_annotator_id: uuid.UUID | None
    batch_reviewer_id: uuid.UUID | None


class MembershipBatchFacts(_ClosedModel):
    id: uuid.UUID
    status: StrictStr
    annotator_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    assigned_user_ids: JsonValue
    admin_locked: StrictBool


class MembershipLabelingFacts(_NativeModel):
    protocol: Literal["aap-labeling-membership-work/v1"] = Field(alias="schema")
    project_id: uuid.UUID
    user_id: uuid.UUID
    annotation_task_ids: list[uuid.UUID]
    review_task_ids: list[uuid.UUID]
    unassigned_review_task_ids: list[uuid.UUID]
    lock_task_ids: list[uuid.UUID]
    review_claim_task_ids: list[uuid.UUID]
    materialization_annotation_task_ids: list[uuid.UUID]
    materialization_review_task_ids: list[uuid.UUID]
    batch_annotator_ids: list[uuid.UUID]
    batch_reviewer_ids: list[uuid.UUID]
    tasks: list[MembershipTaskFacts]
    batches: list[MembershipBatchFacts]
    annotation_receiver_conflict_task_ids: list[uuid.UUID]
    review_receiver_unknown_task_ids: list[uuid.UUID]
    review_receiver_annotator_task_ids: list[uuid.UUID]
    review_receiver_contributor_task_ids: list[uuid.UUID]


class MembershipFactsData(_ClosedModel):
    kind: Literal["facts"]
    operation_id: uuid.UUID
    operation: MembershipOperation
    actor_id: uuid.UUID
    project_id: uuid.UUID
    transaction_id: StrictStr
    actor: MembershipActorFacts
    project: MembershipProjectFacts
    member: MembershipRecord | None
    accounts: list[MembershipAccountFacts]
    receiver_memberships: list[MembershipReceiverFacts]
    eligible_reviewer_ids: list[uuid.UUID]
    labeling: MembershipLabelingFacts | None


MembershipPrepareFacts: TypeAlias = MembershipFactsData


class MembershipReplayData(_ClosedModel):
    kind: Literal["replay"]
    operation_id: uuid.UUID
    operation: MembershipOperation
    actor_id: uuid.UUID
    project_id: uuid.UUID
    reply: MembershipSuccess


MembershipPrepareData: TypeAlias = MembershipReplayData | MembershipFactsData


class NativeMembershipPrepareSuccess(_NativeModel):
    protocol: Literal["aap-workspace-membership-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: MembershipPrepareData

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class NativeMembershipAppliedData(_ClosedModel):
    kind: Literal["applied"]
    operation_id: uuid.UUID
    operation: MembershipOperation
    actor_id: uuid.UUID
    project_id: uuid.UUID
    result: MembershipReplyData


class NativeMembershipApplySuccess(_NativeModel):
    protocol: Literal["aap-workspace-membership-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: NativeMembershipAppliedData

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class NativeMembershipFailure(_NativeModel):
    protocol: Literal["aap-workspace-membership-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: MembershipError

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


NativeMembershipReply: TypeAlias = (
    NativeMembershipPrepareSuccess
    | NativeMembershipApplySuccess
    | NativeMembershipFailure
)


class NativeMembershipApplyRequest(_NativeModel):
    protocol: Literal["aap-workspace-membership-native/v1"] = Field(alias="schema")
    request: MembershipRequest
    transaction_id: StrictStr


class MembershipUnavailable(Exception):
    """A required native membership capability is unavailable or malformed."""

    def __init__(self):
        super().__init__("Workspace membership unavailable")


class MembershipInvalidRequest(ValueError):
    """A caller supplied JSON value does not match the closed request contract."""

    def __init__(self):
        super().__init__("Invalid Workspace membership request")


_REQUEST_ADAPTER = TypeAdapter(MembershipRequest)
_PUBLIC_REPLY_ADAPTER = TypeAdapter(MembershipReply)


def decode_membership_policy(payload: dict | bytes | str) -> MembershipPolicyReply:
    try:
        return MembershipPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MembershipUnavailable() from None


def _validated_request(request: MembershipRequest) -> MembershipRequest:
    if not isinstance(request, MembershipRequest):
        raise MembershipUnavailable()
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(request))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MembershipUnavailable() from None


def decode_membership_request(
    payload: dict | bytes | str | MembershipRequest,
) -> MembershipRequest:
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MembershipInvalidRequest() from None


def _validate_public_reply_binding(
    reply: MembershipReply, request: MembershipRequest
) -> None:
    if (
        reply.operation_id != request.operation_id
        or reply.operation != request.operation
        or reply.actor_id != request.actor_id
        or reply.project_id != request.project_id
    ):
        raise MembershipUnavailable()
    if isinstance(reply, MembershipSuccess):
        expected_data = {
            "add_member": MembershipMemberData,
            "preview_role_change": MembershipPreview,
            "change_role": MembershipMemberData,
            "remove_member": MembershipRemovedData,
        }[request.operation]
        if type(reply.data) is not expected_data:
            raise MembershipUnavailable()
        _validate_reply_data_binding(request, reply.data)


def _validate_reply_data_binding(
    request: MembershipRequest, data: MembershipReplyData
) -> None:
    if isinstance(data, MembershipMemberData):
        member = data.member
        if member.project_id != request.project_id:
            raise MembershipUnavailable()
        if request.operation == "add_member":
            if (
                member.user_id != request.payload.target_user_id
                or member.role != request.payload.project_role
            ):
                raise MembershipUnavailable()
        elif request.operation == "change_role":
            if (
                member.id != request.payload.member_id
                or member.role != request.payload.target_role
            ):
                raise MembershipUnavailable()
        else:
            raise MembershipUnavailable()
        return
    if isinstance(data, MembershipRemovedData):
        if (
            request.operation != "remove_member"
            or data.member_id != request.payload.member_id
            or data.removed is not True
        ):
            raise MembershipUnavailable()
        return
    if isinstance(data, MembershipPreview):
        if request.operation != "preview_role_change":
            raise MembershipUnavailable()
        payload = request.payload
        snapshot = data.resource_snapshot
        member = snapshot.get("member")
        project = snapshot.get("project")
        receivers = snapshot.get("receivers")
        if (
            data.member_id != payload.member_id
            or data.target_role != payload.target_role
            or not isinstance(member, dict)
            or member.get("id") != str(payload.member_id)
            or member.get("user_id") != str(data.user_id)
            or member.get("role") != data.current_role
            or member.get("version") != data.current_version
            or not isinstance(project, dict)
            or project.get("id") != str(request.project_id)
            or not isinstance(receivers, dict)
            or data.preview_token != membership_snapshot_token(snapshot)
            or data.blockers != [item.get("code") for item in data.blocker_details]
            or data.requires_handoff
            != any(code != "same_role" for code in data.blockers)
        ):
            raise MembershipUnavailable()
        for key, replacement_id in (
            ("annotator", payload.replacement_annotator_id),
            ("reviewer", payload.replacement_reviewer_id),
        ):
            receiver = receivers.get(key)
            if replacement_id is None:
                if receiver is not None:
                    raise MembershipUnavailable()
            elif not isinstance(receiver, dict) or receiver.get("user_id") != str(
                replacement_id
            ):
                raise MembershipUnavailable()
        return
    raise MembershipUnavailable()


def membership_snapshot_token(snapshot: dict[str, JsonValue]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decode_membership_reply(
    request: MembershipRequest,
    payload: dict | bytes | str | BaseModel,
) -> MembershipReply:
    request = _validated_request(request)
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid membership result envelope")
        reply = _PUBLIC_REPLY_ADAPTER.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MembershipUnavailable() from None
    _validate_public_reply_binding(reply, request)
    return reply


_PREPARE_REPLY_ADAPTER = TypeAdapter(NativeMembershipPrepareSuccess)
_APPLY_REPLY_ADAPTER = TypeAdapter(NativeMembershipApplySuccess)
_NATIVE_FAILURE_ADAPTER = TypeAdapter(NativeMembershipFailure)


def _assert_native_binding(
    reply: NativeMembershipReply, request: MembershipRequest
) -> None:
    if isinstance(reply, NativeMembershipFailure):
        return
    data = reply.data
    if isinstance(data, MembershipReplayData):
        if (
            data.operation_id != request.operation_id
            or data.operation != request.operation
            or data.actor_id != request.actor_id
            or data.project_id != request.project_id
        ):
            raise MembershipUnavailable()
        _validate_public_reply_binding(data.reply, request)
        return
    if isinstance(data, MembershipFactsData):
        if (
            data.operation_id != request.operation_id
            or data.operation != request.operation
            or data.actor_id != request.actor_id
            or data.project_id != request.project_id
            or data.actor.id != request.actor_id
            or data.project.id != request.project_id
        ):
            raise MembershipUnavailable()
        if request.operation == "add_member":
            if (
                data.member is not None
                or data.labeling is not None
                or data.receiver_memberships
            ):
                raise MembershipUnavailable()
            allowed_account_ids = {
                request.actor_id,
                request.payload.target_user_id,
            }
        else:
            payload = request.payload
            if data.member is None or data.labeling is None:
                raise MembershipUnavailable()
            if (
                data.member.id != payload.member_id
                or data.member.project_id != request.project_id
            ):
                raise MembershipUnavailable()
            if (
                data.labeling.project_id != request.project_id
                or data.labeling.user_id != data.member.user_id
            ):
                raise MembershipUnavailable()
            allowed_account_ids = {data.member.user_id}
            expected_receiver_memberships = set()
            replacement_annotator_id = getattr(
                payload, "replacement_annotator_id", None
            )
            replacement_reviewer_id = getattr(payload, "replacement_reviewer_id", None)
            if replacement_annotator_id is not None:
                allowed_account_ids.add(replacement_annotator_id)
                expected_receiver_memberships.add(
                    (request.project_id, replacement_annotator_id, "annotator")
                )
            if replacement_reviewer_id is not None:
                allowed_account_ids.add(replacement_reviewer_id)
                expected_receiver_memberships.add(
                    (request.project_id, replacement_reviewer_id, "reviewer")
                )
            actual_receiver_memberships = [
                (item.project_id, item.user_id, item.role)
                for item in data.receiver_memberships
            ]
            if (
                len(actual_receiver_memberships)
                != len(set(actual_receiver_memberships))
                or not set(actual_receiver_memberships) <= expected_receiver_memberships
            ):
                raise MembershipUnavailable()
        actual_account_ids = [account.id for account in data.accounts]
        if (
            len(actual_account_ids) != len(set(actual_account_ids))
            or not set(actual_account_ids) <= allowed_account_ids
            or len(data.eligible_reviewer_ids) != len(set(data.eligible_reviewer_ids))
        ):
            raise MembershipUnavailable()
        if isinstance(data.labeling, MembershipLabelingFacts):
            _assert_unique_membership_facts(data.labeling)
        return
    if isinstance(data, NativeMembershipAppliedData):
        if (
            data.operation_id != request.operation_id
            or data.operation != request.operation
            or data.actor_id != request.actor_id
            or data.project_id != request.project_id
        ):
            raise MembershipUnavailable()
        expected_data = {
            "add_member": MembershipMemberData,
            "preview_role_change": MembershipPreview,
            "change_role": MembershipMemberData,
            "remove_member": MembershipRemovedData,
        }[request.operation]
        if type(data.result) is not expected_data:
            raise MembershipUnavailable()
        _validate_reply_data_binding(request, data.result)
        return
    raise MembershipUnavailable()


def _assert_unique_membership_facts(facts: MembershipLabelingFacts) -> None:
    array_fields = (
        "annotation_task_ids",
        "review_task_ids",
        "unassigned_review_task_ids",
        "lock_task_ids",
        "review_claim_task_ids",
        "materialization_annotation_task_ids",
        "materialization_review_task_ids",
        "batch_annotator_ids",
        "batch_reviewer_ids",
        "annotation_receiver_conflict_task_ids",
        "review_receiver_unknown_task_ids",
        "review_receiver_annotator_task_ids",
        "review_receiver_contributor_task_ids",
    )
    for field_name in array_fields:
        values = getattr(facts, field_name)
        if len(values) != len(set(values)):
            raise MembershipUnavailable()
    task_ids = [task.id for task in facts.tasks]
    batch_ids = [batch.id for batch in facts.batches]
    if len(task_ids) != len(set(task_ids)) or len(batch_ids) != len(set(batch_ids)):
        raise MembershipUnavailable()


def decode_native_membership_reply(
    request: MembershipRequest,
    payload: dict | bytes | str | BaseModel,
) -> NativeMembershipReply:
    request = _validated_request(request)
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid native membership result envelope")
        if not raw["ok"]:
            reply: NativeMembershipReply = _NATIVE_FAILURE_ADAPTER.validate_json(
                encoded
            )
        else:
            data = raw.get("data")
            if not isinstance(data, dict):
                raise ValueError("Invalid native membership data")
            if data.get("kind") == "applied":
                reply = _APPLY_REPLY_ADAPTER.validate_json(encoded)
            else:
                reply = _PREPARE_REPLY_ADAPTER.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MembershipUnavailable() from None
    _assert_native_binding(reply, request)
    return reply


def membership_success_reply(
    request: MembershipRequest, data: MembershipReplyData
) -> MembershipSuccess:
    request = _validated_request(request)
    reply = MembershipSuccess(
        schema=MEMBERSHIP_PROTOCOL,
        ok=True,
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=request.actor_id,
        project_id=request.project_id,
        data=data,
    )
    _validate_public_reply_binding(reply, request)
    return reply


def membership_failure_reply(
    request: MembershipRequest, error: MembershipError
) -> MembershipFailure:
    request = _validated_request(request)
    return MembershipFailure(
        schema=MEMBERSHIP_PROTOCOL,
        ok=False,
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=request.actor_id,
        project_id=request.project_id,
        error=error,
    )
