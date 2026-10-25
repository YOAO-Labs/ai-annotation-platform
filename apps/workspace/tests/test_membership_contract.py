from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_workspace.contract import (
    MEMBERSHIP_NATIVE_PROTOCOL,
    MEMBERSHIP_PROTOCOL,
    AddMemberPayload,
    ChangeRolePayload,
    MembershipActorFacts,
    MembershipAuditContext,
    MembershipError,
    MembershipFactsData,
    MembershipFailure,
    MembershipInvalidRequest,
    MembershipMemberData,
    MembershipPolicyReply,
    MembershipProjectFacts,
    MembershipRecord,
    MembershipRequest,
    MembershipSuccess,
    MembershipUnavailable,
    NativeMembershipAppliedData,
    NativeMembershipApplyRequest,
    MembershipReplayData,
    decode_membership_reply,
    decode_membership_request,
    decode_native_membership_reply,
    native_membership_command_json,
)

_ACTOR = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_MEMBER = uuid.UUID("423ad7f4-5aa8-4baf-9d75-69317fec493d")
_USER = uuid.UUID("be846de2-ab3d-426e-b118-0f82d3affd06")
_OPERATION = uuid.UUID("5bde90d2-dd64-4b22-818f-62462351ec6a")
_TIME = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _request(**overrides) -> MembershipRequest:
    values = {
        "schema": MEMBERSHIP_PROTOCOL,
        "operation_id": _OPERATION,
        "operation": "change_role",
        "actor_id": _ACTOR,
        "project_id": _PROJECT,
        "payload": ChangeRolePayload(
            member_id=_MEMBER,
            target_role="viewer",
            replacement_annotator_id=None,
            replacement_reviewer_id=None,
            expected_version=2,
            preview_token="legacy-digest",
            reason="reason from caller",
        ),
        "audit_context": MembershipAuditContext(
            method="PATCH",
            path="/projects/project/members/member/role",
            ip=None,
            request_id=None,
        ),
    }
    values.update(overrides)
    return MembershipRequest(**values)


def _member_record() -> MembershipRecord:
    return MembershipRecord(
        id=_MEMBER,
        project_id=_PROJECT,
        user_id=_USER,
        role="viewer",
        assigned_by=_ACTOR,
        version=3,
        assigned_at=_TIME,
        updated_at=_TIME,
        weekly_target=None,
    )


def _success(request: MembershipRequest | None = None) -> MembershipSuccess:
    request = request or _request()
    return MembershipSuccess(
        schema=MEMBERSHIP_PROTOCOL,
        ok=True,
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=request.actor_id,
        project_id=request.project_id,
        data=MembershipMemberData(member=_member_record()),
    )


def _native_facts(request: MembershipRequest):
    return MembershipFactsData(
        kind="facts",
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=request.actor_id,
        project_id=request.project_id,
        transaction_id="transaction-1",
        actor=MembershipActorFacts(
            id=request.actor_id,
            name="owner",
            email="owner@example.test",
            role="project_admin",
            is_active=True,
        ),
        project=MembershipProjectFacts(
            id=request.project_id, owner_id=request.actor_id
        ),
        member=None,
        accounts=[],
        receiver_memberships=[],
        eligible_reviewer_ids=[],
        labeling=None,
    )


def test_request_round_trip_is_closed_and_keeps_unbounded_text_verbatim():
    oversized = "角色 " + "x" * 10000
    request = _request(
        payload=ChangeRolePayload(
            member_id=_MEMBER,
            target_role=oversized,
            replacement_annotator_id=None,
            replacement_reviewer_id=None,
            expected_version=-7,
            preview_token="",
            reason="r" * 10000,
        )
    )
    wire = request.model_dump(mode="json", by_alias=True)
    assert wire["schema"] == MEMBERSHIP_PROTOCOL
    assert wire["payload"]["target_role"] == oversized
    assert wire["payload"]["reason"] == "r" * 10000
    assert decode_membership_request(wire) == request


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": MEMBERSHIP_PROTOCOL},
        {"schema": MEMBERSHIP_PROTOCOL, "extra": True},
        b'{"schema":"aap-workspace-membership/v1","schema":"other"}',
        b'{"schema":"aap-workspace-membership/v1","extra":NaN}',
        b'{"schema":"aap-workspace-membership/v1","extra":'
        + b"[" * 300
        + b"0"
        + b"]" * 300
        + b"}",
    ],
)
def test_malformed_duplicate_nonfinite_extra_and_deep_requests_are_rejected(payload):
    with pytest.raises(MembershipInvalidRequest):
        decode_membership_request(payload)


def test_required_nullable_fields_and_strict_scalars_are_enforced():
    wire = _request().model_dump(mode="json", by_alias=True)
    payload = dict(wire["payload"])
    del payload["replacement_reviewer_id"]
    with pytest.raises(MembershipInvalidRequest):
        decode_membership_request({**wire, "payload": payload})

    with pytest.raises(MembershipInvalidRequest):
        decode_membership_request(
            {
                **wire,
                "payload": {**wire["payload"], "expected_version": True},
            }
        )

    audit_context = dict(wire["audit_context"])
    del audit_context["ip"]
    with pytest.raises(MembershipInvalidRequest):
        decode_membership_request({**wire, "audit_context": audit_context})


def test_forged_model_instances_are_revalidated_before_public_reply_binding():
    valid = _request()
    forged = valid.model_copy(update={"protocol": "aap-other/v1"})
    with pytest.raises(MembershipUnavailable):
        decode_membership_reply(
            forged, _success(valid).model_dump(mode="json", by_alias=True)
        )

    constructed = MembershipRequest.model_construct(
        protocol="aap-other/v1",
        operation_id=valid.operation_id,
        operation=valid.operation,
        actor_id=valid.actor_id,
        project_id=valid.project_id,
        payload=valid.payload,
        audit_context=valid.audit_context,
    )
    with pytest.raises(MembershipInvalidRequest):
        decode_membership_request(constructed)


def test_public_success_and_failure_replies_bind_all_request_identifiers():
    request = _request()
    success = _success(request).model_dump(mode="json", by_alias=True)
    decoded = decode_membership_reply(request, success)
    assert decoded.ok is True
    assert decoded.data.member.id == _MEMBER

    failure = MembershipFailure(
        schema=MEMBERSHIP_PROTOCOL,
        ok=False,
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=request.actor_id,
        project_id=request.project_id,
        error=MembershipError(
            status_code=409,
            detail={"reason": "resource_busy"},
            headers=None,
            rollback=True,
        ),
    )
    decoded_failure = decode_membership_reply(
        request, failure.model_dump(mode="json", by_alias=True)
    )
    assert decoded_failure.ok is False
    assert decoded_failure.error.rollback is True

    for mismatched in (
        {**success, "operation_id": str(uuid.uuid4())},
        {**success, "actor_id": str(uuid.uuid4())},
        {**success, "project_id": str(uuid.uuid4())},
        {**success, "operation": "remove_member"},
        {**success, "ok": 1},
    ):
        with pytest.raises(MembershipUnavailable):
            decode_membership_reply(request, mismatched)


def test_native_prepare_facts_are_flat_and_bound_to_request():
    request = MembershipRequest(
        schema=MEMBERSHIP_PROTOCOL,
        operation_id=_OPERATION,
        operation="add_member",
        actor_id=_ACTOR,
        project_id=_PROJECT,
        payload=AddMemberPayload(
            target_user_id=_USER,
            project_role="annotator",
        ),
        audit_context=MembershipAuditContext(
            method="POST",
            path="/projects/project/members",
            ip=None,
            request_id=None,
        ),
    )
    facts = _native_facts(request)
    native = {
        "schema": MEMBERSHIP_NATIVE_PROTOCOL,
        "ok": True,
        "data": facts.model_dump(mode="json", by_alias=True),
    }
    decoded = decode_native_membership_reply(request, native)
    assert decoded.data.kind == "facts"
    assert decoded.data.transaction_id == "transaction-1"

    for key, value in (
        ("operation_id", str(uuid.uuid4())),
        ("actor_id", str(uuid.uuid4())),
        ("project_id", str(uuid.uuid4())),
        ("operation", "remove_member"),
    ):
        corrupted = json.loads(json.dumps(native))
        corrupted["data"][key] = value
        with pytest.raises(MembershipUnavailable):
            decode_native_membership_reply(request, corrupted)

    leaked_account = json.loads(json.dumps(native))
    leaked_account["data"]["accounts"] = [
        {
            "id": str(uuid.uuid4()),
            "role": "employee",
            "is_active": True,
        }
    ]
    with pytest.raises(MembershipUnavailable):
        decode_native_membership_reply(request, leaked_account)

    duplicate_reviewers = json.loads(json.dumps(native))
    reviewer_id = str(uuid.uuid4())
    duplicate_reviewers["data"]["eligible_reviewer_ids"] = [reviewer_id, reviewer_id]
    with pytest.raises(MembershipUnavailable):
        decode_native_membership_reply(request, duplicate_reviewers)


def test_native_replay_and_apply_results_are_closed_and_bound():
    request = _request()
    replay = {
        "schema": MEMBERSHIP_NATIVE_PROTOCOL,
        "ok": True,
        "data": MembershipReplayData(
            kind="replay",
            operation_id=request.operation_id,
            operation=request.operation,
            actor_id=request.actor_id,
            project_id=request.project_id,
            reply=_success(request),
        ).model_dump(mode="json", by_alias=True),
    }
    assert decode_native_membership_reply(request, replay).data.kind == "replay"

    applied = {
        "schema": MEMBERSHIP_NATIVE_PROTOCOL,
        "ok": True,
        "data": NativeMembershipAppliedData(
            kind="applied",
            operation_id=request.operation_id,
            operation=request.operation,
            actor_id=request.actor_id,
            project_id=request.project_id,
            result=MembershipMemberData(member=_member_record()),
        ).model_dump(mode="json", by_alias=True),
    }
    decoded = decode_native_membership_reply(request, applied)
    assert decoded.data.kind == "applied"

    bad_apply = json.loads(json.dumps(applied))
    bad_apply["data"]["result"]["member"]["role"] = "reviewer"
    with pytest.raises(MembershipUnavailable):
        decode_native_membership_reply(request, bad_apply)


def test_policy_reply_capability_names_are_exact():
    reply = MembershipPolicyReply(
        schema=MEMBERSHIP_NATIVE_PROTOCOL,
        opaque_text_codec="json-string-ascii/v1",
        operations=[
            "add_member",
            "preview_role_change",
            "change_role",
            "remove_member",
        ],
    )
    assert len(reply.operations) == 4
    with pytest.raises(ValidationError):
        MembershipPolicyReply(
            schema=MEMBERSHIP_NATIVE_PROTOCOL,
            opaque_text_codec="json-string-ascii/v1",
            operations=["add_member", "preview_role_change", "change_role", "unknown"],
        )
    for codec in (None, "unknown"):
        values = reply.model_dump(mode="json", by_alias=True)
        if codec is None:
            values.pop("opaque_text_codec")
        else:
            values["opaque_text_codec"] = codec
        with pytest.raises(ValidationError):
            MembershipPolicyReply.model_validate(values)


@pytest.mark.parametrize(
    "value", ["x\u0000y", "\\u0000", "\u007f", "ß 中 😀", ' "\\\n', "null"]
)
@pytest.mark.parametrize("apply", [False, True])
def test_native_opaque_text_survives_jsonb_transport_without_changing_logical_input(
    value, apply
):
    request = _request().model_dump(mode="json", by_alias=True)
    request["payload"].update(preview_token=value, reason=value)
    original = json.dumps(request, ensure_ascii=True)
    command = (
        {
            "schema": MEMBERSHIP_NATIVE_PROTOCOL,
            "request": request,
            "transaction_id": "1",
        }
        if apply
        else request
    )
    carried = json.loads(native_membership_command_json(command))
    payload = (carried["request"] if apply else carried)["payload"]
    for field in ("reason", "preview_token"):
        assert payload[field].isascii() and "\u0000" not in payload[field]
        assert json.loads(payload[field]) == value
    assert json.dumps(request, ensure_ascii=True) == original


def test_native_apply_request_uses_exact_private_envelope():
    request = _request()
    command = NativeMembershipApplyRequest(
        schema=MEMBERSHIP_NATIVE_PROTOCOL,
        request=request,
        transaction_id="opaque-tx-id",
    ).model_dump(mode="json", by_alias=True)
    assert set(command) == {"schema", "request", "transaction_id"}
    assert command["request"]["operation_id"] == str(_OPERATION)
