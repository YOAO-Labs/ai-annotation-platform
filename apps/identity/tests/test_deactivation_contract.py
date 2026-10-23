from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.deactivation_contract import (
    DEACTIVATION_PROTOCOL,
    DUE_PROTOCOL,
    DeactivationDueFailure,
    DeactivationFailure,
    decode_deactivation_due_reply,
    decode_deactivation_due_request,
    decode_deactivation_reply,
    decode_deactivation_request,
    decode_native_deactivation_due_reply,
    decode_native_deactivation_reply,
    deactivation_due_failure_reply,
    deactivation_failure_reply,
    native_deactivation_command,
    native_deactivation_due_command,
)
from deactivation_helpers import (
    ACTOR_ID,
    SECRET,
    account,
    deactivation_request,
    due_request,
    due_success,
    native_due_success,
    native_user_success,
    requested_account,
    user_success,
)
from aap_identity.deactivation_manager import deactivation_request_fingerprint
from session_helpers import audit_context, principal


def native_command(request, *, actor=None, key_hash=None):
    return native_deactivation_command(
        request,
        actor=actor or principal(role="employee"),
        expected_key_hash=key_hash,
        request_fingerprint=deactivation_request_fingerprint(SECRET, request),
        derivation_id="c" * 64,
    )


def test_wire_request_is_closed_private_and_reason_keeps_legacy_500_limit():
    token = "private-deactivation-bearer"
    request = deactivation_request(
        "request_self_deactivation",
        token=token,
        payload={"reason": " 我已备份数据 "},
    )
    assert request.payload.reason == " 我已备份数据 "
    assert token not in repr(request)

    with pytest.raises(ValidationError):
        deactivation_request(
            "request_self_deactivation",
            payload={"reason": "x" * 501},
        )
    with pytest.raises(ValidationError):
        decode_deactivation_request(
            {
                **request.model_dump(mode="json", by_alias=True),
                "unknown": True,
            }
        )
    raw = json.dumps(request.model_dump(mode="json", by_alias=True)).replace(
        '"schema":', '"schema":"wrong", "schema":', 1
    )
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        decode_deactivation_request(raw.encode())


def test_hmac_fingerprint_excludes_bearer_and_audit_context_but_binds_payload():
    operation_id = uuid.uuid4()
    first = deactivation_request(
        "request_self_deactivation",
        operation_id=operation_id,
        token="first-bearer",
        payload={"reason": "I am leaving"},
        audit=audit_context(path="/first"),
    )
    retry = deactivation_request(
        "request_self_deactivation",
        operation_id=operation_id,
        token="rotated-bearer",
        payload={"reason": "I am leaving"},
        audit=audit_context(path="/retry"),
    )
    changed = deactivation_request(
        "request_self_deactivation",
        operation_id=operation_id,
        payload={"reason": "Different reason"},
    )
    assert deactivation_request_fingerprint(SECRET, first) == (
        deactivation_request_fingerprint(SECRET, retry)
    )
    assert deactivation_request_fingerprint(SECRET, first) != (
        deactivation_request_fingerprint(SECRET, changed)
    )

    command = native_command(first)
    encoded = json.dumps(command.model_dump(mode="json", by_alias=True))
    assert "first-bearer" not in encoded
    assert "rotated-bearer" not in encoded
    assert "I am leaving" in encoded
    assert command.request_fingerprint == deactivation_request_fingerprint(
        SECRET, first
    )


def test_native_api_key_hash_is_required_only_for_api_key_principal():
    request = deactivation_request("cancel_self_deactivation")
    actor = principal(credential_type="api_key", role="viewer")
    command = native_command(request, actor=actor, key_hash="private-api-key-hash")
    assert command.expected_key_hash == "private-api-key-hash"
    assert command.actor.actor_id == ACTOR_ID

    with pytest.raises(IdentityUnavailable):
        native_deactivation_command(
            request,
            actor=actor,
            expected_key_hash=None,
            request_fingerprint="a" * 64,
            derivation_id="b" * 64,
        )


def test_request_reply_binds_current_actor_and_seven_day_schedule():
    request = deactivation_request("request_self_deactivation")
    expected = principal(role="employee")
    fresh = expected.model_copy(update={"role": "project_admin", "name": "Renamed"})
    reply = decode_deactivation_reply(
        request,
        user_success(
            request, requested_account(role="employee"), current_principal=fresh
        ),
        expected_principal=expected,
    )
    assert reply.data.account.id == ACTOR_ID
    assert reply.principal.role == "project_admin"

    wrong_actor = user_success(
        request, requested_account(actor_id=uuid.uuid4()), current_principal=fresh
    )
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_reply(request, wrong_actor, expected_principal=expected)

    wrong_schedule = requested_account().model_copy(
        update={
            "deactivation_scheduled_at": requested_account().deactivation_scheduled_at.replace(
                day=28
            )
        }
    )
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_reply(
            request,
            user_success(request, wrong_schedule, current_principal=fresh),
            expected_principal=expected,
        )


def test_user_profile_reply_keeps_existing_unbounded_preferences():
    request = deactivation_request("cancel_self_deactivation")
    large = account(preferences={"opaque": "x" * 100_000})
    reply = decode_deactivation_reply(request, user_success(request, large))
    assert len(reply.data.account.preferences["opaque"]) == 100_000


def test_cancel_reply_requires_cleared_pending_fields():
    request = deactivation_request("cancel_self_deactivation")
    cleared = account(actor_id=ACTOR_ID)
    assert (
        decode_deactivation_reply(
            request, user_success(request, cleared)
        ).data.account.id
        == ACTOR_ID
    )

    still_pending = requested_account()
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_reply(request, user_success(request, still_pending))


def test_policy_failures_are_operation_bound_and_never_echo_user_reason():
    request = deactivation_request(
        "request_self_deactivation", payload={"reason": "private reason"}
    )
    pending = deactivation_failure_reply(
        DeactivationFailure("policy_rejected", 400, "已有进行中的注销申请")
    )
    with pytest.raises(DeactivationFailure) as failure:
        decode_deactivation_reply(request, pending)
    assert failure.value.status_code == 400
    assert "private reason" not in json.dumps(pending)

    cancel = deactivation_request("cancel_self_deactivation")
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_reply(cancel, pending)

    unsafe = {
        "schema": DEACTIVATION_PROTOCOL,
        "ok": False,
        "error": {
            "code": "policy_rejected",
            "status_code": 400,
            "detail": "private reason",
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_reply(request, unsafe)


def test_native_reply_rejects_operation_and_credential_mismatch():
    request = deactivation_request("cancel_self_deactivation")
    command = native_command(request)
    current = command.actor.model_copy(update={"role": "super_admin", "name": "Fresh"})
    valid = native_user_success(command, account(), current_principal=current)
    assert decode_native_deactivation_reply(command, valid).principal.name == "Fresh"

    wrong = dict(valid)
    wrong["operation_id"] = str(uuid.uuid4())
    with pytest.raises(IdentityUnavailable):
        decode_native_deactivation_reply(command, wrong)

    wrong_principal = command.actor.model_copy(update={"credential_id": uuid.uuid4()})
    wrong = native_user_success(command, account(), current_principal=wrong_principal)
    with pytest.raises(IdentityUnavailable):
        decode_native_deactivation_reply(command, wrong)


def test_due_request_requires_exact_bounded_page_and_no_user_ids():
    request = due_request(limit=500)
    assert request.limit == 500
    with pytest.raises(ValidationError):
        decode_deactivation_due_request(
            {
                "schema": DUE_PROTOCOL,
                "operation_id": str(uuid.uuid4()),
                "limit": True,
            }
        )
    with pytest.raises(ValidationError):
        decode_deactivation_due_request(
            {
                "schema": DUE_PROTOCOL,
                "operation_id": str(uuid.uuid4()),
                "limit": 501,
            }
        )
    with pytest.raises(ValidationError):
        decode_deactivation_due_request(
            {
                "schema": DUE_PROTOCOL,
                "operation_id": str(uuid.uuid4()),
                "limit": 10,
                "user_ids": [str(uuid.uuid4())],
            }
        )


def test_due_success_binds_page_counts_and_has_more():
    request = due_request(limit=4)
    assert decode_deactivation_due_reply(request, due_success(request)).processed == 0
    result = due_success(
        request,
        processed=2,
        examined=3,
        blocked_last_super_admin=1,
        has_more=True,
    )
    reply = decode_deactivation_due_reply(request, result)
    assert reply.processed == 2 and reply.has_more is True

    with pytest.raises(IdentityUnavailable):
        decode_deactivation_due_reply(
            request,
            due_success(
                request,
                processed=2,
                examined=3,
                blocked_last_super_admin=0,
                has_more=True,
            ),
        )
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_due_reply(
            request,
            due_success(
                request,
                processed=4,
                examined=5,
                blocked_last_super_admin=1,
                has_more=True,
            ),
        )
    invalid_boolean = due_success(request, has_more=1)
    with pytest.raises(IdentityUnavailable):
        decode_deactivation_due_reply(request, invalid_boolean)

    with pytest.raises(IdentityUnavailable):
        decode_deactivation_due_reply(
            request, {**result, "user_id": str(uuid.uuid4()), "email": "private"}
        )


def test_due_native_reply_and_operator_failures_are_closed_and_operation_bound():
    request = due_request(limit=8)
    command = native_deactivation_due_command(request)
    reply = decode_native_deactivation_due_reply(
        request,
        native_due_success(
            request,
            processed=1,
            examined=1,
            blocked_last_super_admin=0,
            has_more=False,
        ),
    )
    assert reply.operation_id == command.operation_id

    wrong = native_due_success(request, operation_id=str(uuid.uuid4()))
    with pytest.raises(IdentityUnavailable):
        decode_native_deactivation_due_reply(request, wrong)

    error = deactivation_due_failure_reply("invariant_busy")
    with pytest.raises(DeactivationDueFailure) as failure:
        decode_deactivation_due_reply(request, error)
    assert failure.value.code == "invariant_busy"
