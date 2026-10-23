from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.suspension_contract import (
    SuspensionFailure,
    decode_native_suspension_command,
    decode_native_suspension_reply,
    decode_suspension_reply,
    decode_suspension_request,
    suspension_failure_reply,
)
from aap_identity.suspension_manager import (
    suspension_derivation_id,
    suspension_request_fingerprint,
)
from session_helpers import principal
from suspension_helpers import (
    OPAQUE_KEY_HASH,
    SUSPENSION_SECRET,
    TARGET_ID,
    native_command,
    native_success,
    suspension_request,
    suspension_user,
    wire_success,
)


def test_request_fingerprint_binds_exact_unicode_reason_but_not_token_or_audit():
    reason = " \u00a0交接　 "
    request = suspension_request("reactivate_account", reason=reason)
    same = suspension_request(
        "reactivate_account",
        reason=reason,
        token="different-bearer",
        audit={
            "method": "POST",
            "path": "/other/audit/path",
            "ip": None,
            "request_id": "different-request",
        },
    )
    trimmed = suspension_request("reactivate_account", reason=reason.strip())
    assert request.payload.reason == reason
    assert suspension_request_fingerprint(SUSPENSION_SECRET, request) == (
        suspension_request_fingerprint(SUSPENSION_SECRET, same)
    )
    assert suspension_request_fingerprint(SUSPENSION_SECRET, request) != (
        suspension_request_fingerprint(SUSPENSION_SECRET, trimmed)
    )
    assert len(suspension_derivation_id(SUSPENSION_SECRET)) == 64
    assert request.token not in repr(request)

    vector = json.loads(
        (Path(__file__).parent / "fixtures/suspension_vector.json").read_text()
    )
    vector_request = suspension_request(
        "reactivate_account",
        operation_id=uuid.UUID(vector["operation_id"]),
        target_user_id=uuid.UUID(vector["target_user_id"]),
        reason=vector["reason"],
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
        suspension_request_fingerprint(vector["signing_secret"], vector_request)
        == vector["request_fingerprint"]
    )
    assert suspension_derivation_id(vector["signing_secret"]) == vector["derivation_id"]


def test_exact_wire_payloads_and_duplicate_alias_rejection():
    suspend = suspension_request("suspend_account")
    reactivate = suspension_request("reactivate_account", reason=None)
    assert set(suspend.model_dump(mode="json", by_alias=True)["payload"]) == {
        "target_user_id"
    }
    assert set(reactivate.model_dump(mode="json", by_alias=True)["payload"]) == {
        "target_user_id",
        "reason",
    }
    with pytest.raises(Exception):
        decode_suspension_request(
            {
                **suspend.model_dump(mode="json", by_alias=True),
                "protocol": "unexpected-alias",
            }
        )
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        decode_suspension_request(
            b'{"schema":"aap-identity-suspensions/v1","schema":"bad"}'
        )
    with pytest.raises(Exception):
        suspension_request("reactivate_account", reason="x" * 501)


def test_native_command_is_exact_and_hides_bearer_and_api_key_hash():
    request = suspension_request(
        "reactivate_account", token="private-suspension-token", reason="reason"
    )
    api_key_actor = principal(
        credential_type="api_key",
        role="super_admin",
        generation=None,
        scopes=["users:read", "users:write"],
    )
    command = native_command(request, actor=api_key_actor, key_hash=OPAQUE_KEY_HASH)
    raw = command.model_dump(mode="json", by_alias=True)
    assert set(raw) == {
        "schema",
        "operation",
        "operation_id",
        "actor",
        "expected_key_hash",
        "request_fingerprint",
        "derivation_id",
        "audit_context",
        "payload",
    }
    assert "private-suspension-token" not in repr(request)
    assert "private-suspension-token" not in repr(command)
    assert OPAQUE_KEY_HASH not in repr(command)
    assert "private-suspension-token" not in json.dumps(raw)
    assert raw["payload"] == {
        "target_user_id": str(TARGET_ID),
        "reason_present": True,
    }
    assert "reason" not in raw["payload"]
    assert decode_native_suspension_command(raw) == command
    with pytest.raises(IdentityUnavailable):
        decode_native_suspension_command(
            {**raw, "payload": {**raw["payload"], "reason_present": 1}}
        )
    with pytest.raises(IdentityUnavailable):
        decode_native_suspension_command({**raw, "unexpected": True})


def test_nul_and_unicode_whitespace_stay_wire_only_and_derive_legacy_truthiness():
    nul_request = suspension_request("reactivate_account", reason="\x00")
    empty_request = suspension_request("reactivate_account", reason="")
    nul_command = native_command(nul_request)
    empty_command = native_command(empty_request)
    assert nul_command.payload.reason_present is True
    assert empty_command.payload.reason_present is False
    assert nul_command.payload.reason_present != empty_command.payload.reason_present
    assert suspension_request_fingerprint(SUSPENSION_SECRET, nul_request) != (
        suspension_request_fingerprint(SUSPENSION_SECRET, empty_request)
    )
    native_json = json.dumps(nul_command.model_dump(mode="json", by_alias=True))
    assert "\\u0000" not in native_json
    assert "reason" not in nul_command.model_dump(mode="json", by_alias=True)["payload"]

    whitespace_request = suspension_request("reactivate_account", reason="\u00a0\u3000")
    null_request = suspension_request("reactivate_account", reason=None)
    whitespace_command = native_command(whitespace_request)
    null_command = native_command(null_request)
    assert whitespace_request.payload.reason == "\u00a0\u3000"
    assert whitespace_command.payload.reason_present is False
    assert null_command.payload.reason_present is False
    assert suspension_request_fingerprint(SUSPENSION_SECRET, whitespace_request) != (
        suspension_request_fingerprint(SUSPENSION_SECRET, null_request)
    )
    assert (
        "reason"
        not in whitespace_command.model_dump(mode="json", by_alias=True)["payload"]
    )


def test_noop_suspend_preserves_historical_disabled_kind_and_replay_flag_is_private():
    request = suspension_request("suspend_account")
    actor = principal(role="super_admin")
    command = native_command(request, actor=actor)
    frozen = suspension_user(
        disabled_kind="deleted", disabled_at=None, disabled_by=None
    )
    native = decode_native_suspension_reply(
        command,
        native_success(command, frozen, replayed=True, current_principal=actor),
    )
    assert native.replayed is True
    assert native.data.user.disabled_kind == "deleted"
    wire = decode_suspension_reply(
        request,
        wire_success(request, frozen, current_principal=actor),
        expected_principal=actor,
    )
    assert wire.data.user.disabled_kind == "deleted"
    assert "replayed" not in wire.model_dump(mode="json", by_alias=True)


def test_reactivate_success_binds_target_and_active_state_and_preserves_opaque_fields():
    request = suspension_request("reactivate_account", reason="  restore  ")
    actor = principal(role="project_admin")
    user = suspension_user(
        is_active=True,
        status="offline",
        disabled_kind=None,
        disabled_at=None,
        disabled_by=None,
        disabled_reason=None,
    )
    reply = decode_suspension_reply(
        request,
        wire_success(request, user, current_principal=actor),
        expected_principal=actor,
    )
    assert reply.data.user.preferences == {"opaque": {"numeric": 9007199254740993}}
    direct_value = decode_suspension_reply(
        request,
        {
            "schema": "aap-identity-suspensions/v1",
            "ok": True,
            "operation": request.operation,
            "operation_id": request.operation_id,
            "principal": actor,
            "data": {"user": user},
        },
        expected_principal=actor,
    )
    assert direct_value.data.user.created_at == reply.data.user.created_at
    with pytest.raises(IdentityUnavailable):
        decode_suspension_reply(
            request,
            wire_success(request, suspension_user(), current_principal=actor),
            expected_principal=actor,
        )
    with pytest.raises(IdentityUnavailable):
        decode_suspension_reply(
            request,
            wire_success(
                request,
                {**user, "id": str(uuid.uuid4())},
                current_principal=actor,
            ),
            expected_principal=actor,
        )


def test_failure_codes_are_closed_operation_bound_and_stale_manager_roles_are_valid():
    admitted = principal(role="super_admin")
    request = suspension_request("suspend_account")
    scope_failure = SuspensionFailure(
        "suspend_scope_forbidden",
        403,
        "该用户跨多个项目，须由超级管理员处理",
    )
    with pytest.raises(SuspensionFailure) as raised:
        decode_suspension_reply(
            request,
            suspension_failure_reply(scope_failure),
            expected_principal=admitted,
        )
    assert raised.value.code == "suspend_scope_forbidden"

    with pytest.raises(IdentityUnavailable):
        decode_suspension_reply(
            request,
            suspension_failure_reply(
                SuspensionFailure("already_active", 409, "账号当前已启用")
            ),
            expected_principal=admitted,
        )
    with pytest.raises(IdentityUnavailable):
        decode_suspension_reply(
            request,
            {
                "schema": "aap-identity-suspensions/v1",
                "ok": False,
                "error": {
                    "code": "suspend_scope_forbidden",
                    "status_code": 403,
                    "detail": "driver detail",
                },
            },
            expected_principal=admitted,
        )
    with pytest.raises(IdentityUnavailable):
        decode_suspension_reply(
            request,
            {
                "schema": "aap-identity-suspensions/v1",
                "ok": False,
                "error": {
                    "code": "project_busy",
                    "status_code": 409,
                    "detail": {"reason": "secret"},
                },
            },
            expected_principal=admitted,
        )


def test_project_busy_is_closed_and_suspend_only():
    actor = principal(role="project_admin")
    suspend = suspension_request("suspend_account")
    failure = SuspensionFailure("project_busy", 409, {"reason": "project_busy"})
    with pytest.raises(SuspensionFailure) as raised:
        decode_suspension_reply(
            suspend,
            suspension_failure_reply(failure),
            expected_principal=actor,
        )
    assert raised.value.detail == {"reason": "project_busy"}

    reactivate = suspension_request("reactivate_account")
    with pytest.raises(IdentityUnavailable):
        decode_suspension_reply(
            reactivate,
            suspension_failure_reply(failure),
            expected_principal=actor,
        )


def test_schema_artifacts_are_generated_from_closed_models():
    import json as json_module

    root = Path(__file__).resolve().parents[1] / "src" / "aap_identity"
    for filename, protocol in (
        ("suspension_protocol.schema.json", "aap-identity-suspensions/v1"),
        (
            "native_suspension_protocol.schema.json",
            "aap-identity-suspensions-native/v1",
        ),
    ):
        schema = json_module.loads((root / filename).read_text(encoding="utf-8"))
        assert schema["$id"] == protocol
