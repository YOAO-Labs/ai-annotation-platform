from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from aap_identity.contract import IdentityUnavailable
from aap_identity.recovery_contract import (
    NATIVE_RECOVERY_PROTOCOL,
    RECOVERY_PROTOCOL,
    NativeRecoveryConfig,
    RecoveryFailure,
    decode_native_recovery_apply_command,
    decode_native_recovery_prepare_command,
    decode_native_recovery_prepare_reply,
    decode_recovery_reply,
    decode_recovery_request,
    native_recovery_apply_command,
    native_recovery_prepare_command,
    recovery_failure_reply,
)
from aap_identity.recovery_crypto import request_fingerprint, token_hash
from aap_identity.session_manager import sign_session_claims
from pydantic import ValidationError
from recovery_helpers import SECRET, account, claims, policy, recovery_request


def _native_prepare(request):
    fingerprint, epoch = request_fingerprint(SECRET, request)
    config = NativeRecoveryConfig(
        allow_open_registration=True,
        email_verification_required=True,
        frontend_base_url="https://anno.example.test",
        expire_minutes=1440,
    )
    return native_recovery_prepare_command(
        request,
        request_fingerprint=fingerprint,
        derivation_id=epoch,
        config=config,
    )


def test_recovery_request_is_closed_normalizes_email_and_hides_credentials():
    request = recovery_request(
        "register_open",
        payload={
            "email": "  Person@Example.Test ",
            "name": "  Keep this name  ",
            "password": "StrongPassword2026",
            "captcha_verified": True,
        },
    )
    assert request.payload.email == "person@example.test"
    assert request.payload.name == "  Keep this name  "
    assert "StrongPassword2026" not in repr(request)

    with pytest.raises((ValidationError, ValueError)):
        decode_recovery_request(
            {
                "schema": RECOVERY_PROTOCOL,
                "protocol": RECOVERY_PROTOCOL,
                "operation": "consume_email_verification",
                "operation_id": str(uuid.uuid4()),
                "audit_context": {
                    "method": "POST",
                    "path": "/api/v1/auth/verify-email",
                    "ip": None,
                    "request_id": None,
                },
                "payload": {"token": "opaque"},
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        recovery_request("consume_email_verification", payload={"token": "x" * 65})


def test_json_decoder_rejects_duplicate_fields_and_non_json_numbers():
    raw = b'{"schema":"aap-identity-recovery/v1","schema":"aap-identity-recovery/v1"}'
    with pytest.raises((ValidationError, ValueError)):
        decode_recovery_request(raw)
    raw = b'{"schema":"aap-identity-recovery/v1","operation":"register_open","operation_id":"00000000-0000-0000-0000-000000000001","audit_context":{"method":null,"path":null,"ip":null,"request_id":null},"payload":{"email":"a@b","name":"n","password":"StrongPassword2026","captcha_verified":NaN}}'
    with pytest.raises((ValidationError, ValueError)):
        decode_recovery_request(raw)


def test_native_commands_never_include_raw_password_or_token():
    request = recovery_request("consume_password_reset")
    prepare = _native_prepare(request)
    encoded = json.dumps(prepare.model_dump(mode="json", by_alias=True))
    assert request.payload.token not in encoded
    assert request.payload.new_password not in encoded
    assert prepare.args.token_hash == token_hash(request.payload.token)
    assert (
        decode_native_recovery_prepare_command(
            prepare.model_dump(mode="json", by_alias=True)
        )
        == prepare
    )

    apply = native_recovery_apply_command(
        prepare,
        password_hash="$2b$12$" + "a" * 53,
        token_hash=prepare.args.token_hash,
        token_ciphertext=None,
        encryption_id=None,
    )
    apply_json = json.dumps(apply.model_dump(mode="json", by_alias=True))
    assert request.payload.token not in apply_json
    assert request.payload.new_password not in apply_json
    assert (
        decode_native_recovery_apply_command(
            apply.model_dump(mode="json", by_alias=True)
        )
        == apply
    )


def test_native_command_rejects_raw_secrets_unknown_fields_and_aliases():
    request = recovery_request("consume_email_verification")
    payload = _native_prepare(request).model_dump(mode="json", by_alias=True)
    payload["args"] = {"token": request.payload.token}
    with pytest.raises(IdentityUnavailable):
        decode_native_recovery_prepare_command(payload)
    payload = _native_prepare(request).model_dump(mode="json", by_alias=True)
    payload["protocol"] = payload.pop("schema")
    with pytest.raises(IdentityUnavailable):
        decode_native_recovery_prepare_command(payload)


def test_native_failure_is_code_only_and_maps_to_a_safe_public_status():
    request = recovery_request("consume_email_verification")
    command = _native_prepare(request)
    failure = {
        "schema": NATIVE_RECOVERY_PROTOCOL,
        "ok": False,
        "error": {"code": "invalid_request"},
    }
    with pytest.raises(RecoveryFailure) as caught:
        decode_native_recovery_prepare_reply(command, failure)
    assert caught.value.code == "invalid_request"
    assert caught.value.status_code == 400
    assert caught.value.detail is None

    failure["error"]["detail"] = "legacy token leaked"
    with pytest.raises(IdentityUnavailable):
        decode_native_recovery_prepare_reply(command, failure)


def test_registration_result_binds_user_and_frozen_session_claims():
    request = recovery_request("register_open")
    issued = claims(request.operation_id, actor_id=request.operation_id)
    result = {
        "schema": RECOVERY_PROTOCOL,
        "ok": True,
        "operation": "register_open",
        "operation_id": str(request.operation_id),
        "data": {
            "account": account(
                actor_id=request.operation_id, verified_at=datetime.now(UTC)
            ).model_dump(mode="json"),
            "claims": issued.model_dump(mode="json"),
            "access_token": "not-a-jwt",
            "email_verification_required": False,
            "completed": None,
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_recovery_reply(request, result)


def test_success_does_not_treat_opaque_account_strings_as_secret_echoes():
    password = "StrongPassword2026"
    request = recovery_request(
        "register_open",
        payload={
            "email": "strongpassword2026@example.test",
            "name": password,
            "password": password,
            "captcha_verified": True,
        },
    )
    issued = claims(request.operation_id, actor_id=request.operation_id)
    result = {
        "schema": RECOVERY_PROTOCOL,
        "ok": True,
        "operation": "register_open",
        "operation_id": str(request.operation_id),
        "data": {
            "account": account(
                actor_id=request.operation_id,
                email=request.payload.email,
                verified_at=datetime.now(UTC),
            )
            .model_copy(
                update={
                    "name": password,
                    "preferences": {
                        "token": password,
                        "password": password,
                        "protocol": password,
                    },
                }
            )
            .model_dump(mode="json"),
            "claims": issued.model_dump(mode="json"),
            "access_token": sign_session_claims(issued, SECRET),
            "email_verification_required": False,
            "completed": None,
        },
    }

    reply = decode_recovery_reply(request, result)

    assert reply.data.account.name == password
    assert reply.data.account.preferences["password"] == password
    assert reply.data.account.preferences["protocol"] == password


def test_failure_reply_only_exposes_operation_safe_details():
    request = recovery_request("consume_email_verification")
    failure = RecoveryFailure("invalid_request", 400, "验证链接无效或已过期")
    with pytest.raises(RecoveryFailure) as caught:
        decode_recovery_reply(request, recovery_failure_reply(failure))
    assert caught.value.code == "invalid_request"

    with pytest.raises(IdentityUnavailable):
        recovery_failure_reply(RecoveryFailure("invalid_request", 400, "secret-token"))


def test_password_policy_failure_uses_only_the_request_specific_safe_message():
    request = recovery_request(
        "register_open",
        payload={
            "email": "person@example.test",
            "name": "New account",
            "password": "lowercase123",
            "captcha_verified": True,
        },
    )
    detail = [
        {
            "type": "value_error",
            "loc": ["body", "password"],
            "msg": "Value error, 密码需包含大写字母",
        }
    ]
    with pytest.raises(RecoveryFailure) as caught:
        decode_recovery_reply(
            request,
            recovery_failure_reply(RecoveryFailure("policy_rejected", 422, detail)),
        )
    assert caught.value.code == "policy_rejected"

    unsafe = [
        {
            "type": "value_error",
            "loc": ["body", "password"],
            "msg": "Value error, " + request.payload.password,
        }
    ]
    with pytest.raises(IdentityUnavailable):
        decode_recovery_reply(
            request,
            recovery_failure_reply(RecoveryFailure("policy_rejected", 422, unsafe)),
        )


def test_policy_reply_values_are_closed_and_exact():
    request = recovery_request("register_open")
    assert request.operation_id
    assert policy().email_verification_required is True
