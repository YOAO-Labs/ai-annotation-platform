from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.deletion_contract import (
    DELETION_PROTOCOL,
    NATIVE_DELETION_PROTOCOL,
    DeletionFailure,
    decode_deletion_reply,
    decode_deletion_request,
    decode_native_deletion_command,
    decode_native_deletion_reply,
    deletion_failure_reply,
)
from aap_identity.deletion_manager import (
    deletion_derivation_id,
    deletion_request_fingerprint,
)
from session_helpers import principal
from deletion_helpers import (
    DELETION_SECRET,
    OPAQUE_KEY_HASH,
    RECEIVER_ID,
    TARGET_ID,
    deletion_request,
    deletion_user,
    native_command,
    native_success,
    wire_success,
)


def test_request_has_exact_payload_and_required_nullable_receiver():
    request = deletion_request()
    assert set(request.model_dump(mode="json", by_alias=True)) == {
        "schema",
        "operation",
        "operation_id",
        "token",
        "audit_context",
        "payload",
    }
    assert request.payload.transfer_to_user_id is None
    assert set(request.model_dump(mode="json", by_alias=True)["payload"]) == {
        "target_user_id",
        "transfer_to_user_id",
    }
    with pytest.raises(ValidationError):
        decode_deletion_request(
            {
                "schema": DELETION_PROTOCOL,
                "operation": "delete_account",
                "operation_id": str(uuid.uuid4()),
                "token": "private-token",
                "audit_context": {
                    "method": "DELETE",
                    "path": "/api/v1/users/target",
                    "ip": None,
                    "request_id": None,
                },
                "payload": {"target_user_id": str(TARGET_ID)},
            }
        )
    with pytest.raises(ValidationError):
        missing_receiver = deletion_request().model_dump(mode="json", by_alias=True)
        missing_receiver["payload"].pop("transfer_to_user_id")
        decode_deletion_request(missing_receiver)


def test_request_rejects_alias_duplicate_fields_and_extra_payload_fields():
    request = deletion_request()
    raw = request.model_dump(mode="json", by_alias=True)
    with pytest.raises(ValueError, match="declared wire field"):
        decode_deletion_request({**raw, "protocol": DELETION_PROTOCOL})
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        decode_deletion_request(
            b'{"schema":"aap-identity-deletions/v1","schema":"bad"}'
        )
    raw["payload"]["unexpected"] = True
    with pytest.raises(ValidationError):
        decode_deletion_request(raw)


def test_fingerprint_binds_operation_and_receiver_but_not_token_or_audit():
    request = deletion_request(transfer_to_user_id=RECEIVER_ID)
    same = deletion_request(
        transfer_to_user_id=RECEIVER_ID,
        token="different-private-bearer",
        audit={
            "method": "DELETE",
            "path": "/other/path",
            "ip": None,
            "request_id": "other-request",
        },
    )
    without_receiver = deletion_request()
    assert deletion_request_fingerprint(DELETION_SECRET, request) == (
        deletion_request_fingerprint(DELETION_SECRET, same)
    )
    assert deletion_request_fingerprint(DELETION_SECRET, request) != (
        deletion_request_fingerprint(DELETION_SECRET, without_receiver)
    )
    assert len(deletion_derivation_id(DELETION_SECRET)) == 64
    assert request.token not in repr(request)

    vector = json.loads(
        (Path(__file__).parent / "fixtures/deletion_vector.json").read_text()
    )
    vector_request = deletion_request(
        operation_id=uuid.UUID(vector["operation_id"]),
        target_user_id=uuid.UUID(vector["target_user_id"]),
        transfer_to_user_id=vector["transfer_to_user_id"],
    )
    canonical = json.dumps(
        {
            "operation": vector_request.operation,
            "payload": vector_request.payload.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    assert canonical == vector["canonical_request"]
    assert (
        deletion_request_fingerprint(vector["signing_secret"], vector_request)
        == vector["request_fingerprint"]
    )
    assert deletion_derivation_id(vector["signing_secret"]) == vector["derivation_id"]


def test_native_command_uses_exact_shape_and_opaque_api_key_binding():
    actor = principal(
        credential_type="api_key",
        role="super_admin",
        generation=None,
        scopes=["users:read", "users:write"],
    )
    command = native_command(actor=actor, key_hash=OPAQUE_KEY_HASH)
    raw = command.model_dump(mode="json", by_alias=True)
    assert set(raw) == {
        "schema",
        "operation_id",
        "operation",
        "payload",
        "actor",
        "expected_key_hash",
        "request_fingerprint",
        "derivation_id",
        "audit_context",
    }
    assert command.expected_key_hash == OPAQUE_KEY_HASH
    assert "token" not in raw
    assert raw["schema"] == NATIVE_DELETION_PROTOCOL
    assert decode_native_deletion_command(raw) == command
    raw["expected_key_hash"] = None
    with pytest.raises(IdentityUnavailable):
        decode_native_deletion_command(raw)


def test_native_and_wire_success_accept_frozen_inactive_noop_snapshot():
    request = deletion_request()
    command = native_command(request)
    historical = deletion_user(
        disabled_kind="suspended",
        disabled_reason="管理员停用账号",
        deactivation_requested_at="2026-10-18T00:00:00Z",
    )
    native = decode_native_deletion_reply(
        command, native_success(command, historical, replayed=True)
    )
    assert native.replayed is True
    assert native.data.user.disabled_kind == "suspended"
    wire = decode_deletion_reply(
        request,
        wire_success(request, historical, current_principal=command.actor),
        expected_principal=command.actor,
    )
    assert wire.data.user.disabled_kind == "suspended"
    assert "replayed" not in wire.model_dump(mode="json", by_alias=True)


def test_native_replay_flag_is_strict_and_reply_principal_stays_credential_bound():
    request = deletion_request()
    command = native_command(request)
    malformed_flag = native_success(command, deletion_user())
    malformed_flag["replayed"] = 1
    with pytest.raises(IdentityUnavailable):
        decode_native_deletion_reply(command, malformed_flag)

    wrong_credential = command.actor.model_copy(update={"credential_id": uuid.uuid4()})
    with pytest.raises(IdentityUnavailable):
        decode_native_deletion_reply(
            command,
            native_success(
                command, deletion_user(), current_principal=wrong_credential
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"is_active": True},
        {"id": uuid.uuid4()},
    ],
)
def test_success_rejects_active_or_unrelated_target(changes):
    request = deletion_request()
    actor = principal(role="super_admin")
    payload = wire_success(request, deletion_user(**changes))
    with pytest.raises(IdentityUnavailable):
        decode_deletion_reply(request, payload, expected_principal=actor)


def test_pending_detail_is_count_bound_and_strict():
    request = deletion_request()
    actor = principal(role="super_admin")
    detail = {
        "reason": "has_pending_tasks",
        "pending_task_count": 2,
        "locked_task_count": 1,
        "sample_task_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        "message": "该用户当前有 2 个未完成任务、1 个锁定任务，需先转交",
    }
    failure = {
        "schema": DELETION_PROTOCOL,
        "ok": False,
        "error": {
            "code": "has_pending_tasks",
            "status_code": 409,
            "detail": detail,
        },
    }
    with pytest.raises(DeletionFailure) as raised:
        decode_deletion_reply(request, failure, expected_principal=actor)
    assert raised.value.code == "has_pending_tasks"

    detail["pending_task_count"] = True
    with pytest.raises(IdentityUnavailable):
        decode_deletion_reply(request, failure, expected_principal=actor)
    detail["pending_task_count"] = 2
    detail["message"] = "driver error"
    with pytest.raises(IdentityUnavailable):
        decode_deletion_reply(request, failure, expected_principal=actor)


def test_failure_builder_revalidates_json_uuid_samples():
    sample_id = uuid.uuid4()
    failure = DeletionFailure(
        "has_pending_tasks",
        409,
        {
            "reason": "has_pending_tasks",
            "pending_task_count": 1,
            "locked_task_count": 0,
            "sample_task_ids": [str(sample_id)],
            "message": "该用户当前有 1 个未完成任务、0 个锁定任务，需先转交",
        },
    )
    reply = deletion_failure_reply(failure)
    assert reply["error"]["detail"]["sample_task_ids"] == [str(sample_id)]
    assert reply["error"]["code"] == "has_pending_tasks"
    assert "private-deletion-bearer" not in json.dumps(reply)


def test_failure_code_details_are_operation_and_receiver_bound():
    request = deletion_request()
    actor = principal(role="super_admin")
    error = deletion_failure_reply("receiver_unavailable")
    with pytest.raises(IdentityUnavailable):
        decode_deletion_reply(request, error, expected_principal=actor)
    pa_request = deletion_request()
    pa = actor.model_copy(update={"role": "project_admin"})
    wrong_scope = deletion_failure_reply("delete_scope_forbidden")
    with pytest.raises(DeletionFailure):
        decode_deletion_reply(pa_request, wrong_scope, expected_principal=pa)


@pytest.mark.parametrize(
    ("admitted_role", "failure_code", "with_receiver"),
    [
        ("super_admin", "delete_role_forbidden", False),
        ("super_admin", "delete_scope_forbidden", False),
        ("super_admin", "receiver_scope_forbidden", True),
        ("employee", "project_busy", False),
    ],
)
def test_mutable_role_snapshot_does_not_reject_owner_failures(
    admitted_role, failure_code, with_receiver
):
    actor = principal(role=admitted_role)
    request = deletion_request(
        transfer_to_user_id=RECEIVER_ID if with_receiver else None
    )
    with pytest.raises(DeletionFailure) as failure:
        decode_deletion_reply(
            request,
            deletion_failure_reply(failure_code),
            expected_principal=actor,
        )
    assert failure.value.code == failure_code


@pytest.mark.parametrize(
    ("actor_role", "target_is_actor", "failure_code", "detail"),
    [
        (
            "project_admin",
            False,
            "receiver_scope_forbidden",
            "转交目标必须是待转交任务所在项目的成员",
        ),
        (
            "super_admin",
            True,
            "receiver_is_target",
            "转交目标不能与被删用户相同",
        ),
        (
            "super_admin",
            False,
            "receiver_unavailable",
            "转交目标用户不存在或已禁用",
        ),
    ],
)
def test_receiver_failures_reject_unreachable_legacy_branches(
    actor_role, target_is_actor, failure_code, detail
):
    actor = principal(role=actor_role)
    target_id = actor.actor_id if target_is_actor else TARGET_ID
    request = deletion_request(
        target_user_id=target_id,
        transfer_to_user_id=actor.actor_id,
    )
    command = native_command(request, actor=actor)
    wire_error = {
        "schema": DELETION_PROTOCOL,
        "ok": False,
        "error": {
            "code": failure_code,
            "status_code": 400 if failure_code != "receiver_scope_forbidden" else 403,
            "detail": detail,
        },
    }
    native_error = {
        **wire_error,
        "schema": NATIVE_DELETION_PROTOCOL,
    }
    with pytest.raises(IdentityUnavailable):
        decode_deletion_reply(request, wire_error, expected_principal=actor)
    with pytest.raises(IdentityUnavailable):
        decode_native_deletion_reply(command, native_error)


def test_schema_artifacts_have_expected_ids_and_closed_models():
    root = Path(__file__).resolve().parents[1] / "src" / "aap_identity"
    for filename, expected_id in (
        ("deletion_protocol.schema.json", DELETION_PROTOCOL),
        ("native_deletion_protocol.schema.json", NATIVE_DELETION_PROTOCOL),
    ):
        schema = json.loads((root / filename).read_text(encoding="utf-8"))
        assert schema["$id"] == expected_id
