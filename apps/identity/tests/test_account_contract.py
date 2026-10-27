from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_identity.account_contract import (
    ACCOUNT_PROTOCOL,
    AccountFailure,
    AccountFailureReply,
    AccountResultData,
    NativeAccountReply,
    decode_account_reply,
    decode_native_account_reply,
    decode_native_apply_command,
    decode_native_prepare_reply,
    decode_native_prepare_command,
    native_account_apply_command,
    native_account_prepare_command,
    account_failure_reply,
)
from aap_identity.contract import IdentityUnavailable

from account_helpers import NATIVE_ACCOUNT_PROTOCOL as HELPER_NATIVE_PROTOCOL
from account_helpers import account_record, account_request
from session_helpers import ACTOR_ID, principal

CURRENT_PRINCIPAL = principal()


def account_success(
    request, *, account=None, preferences=None, changed=None, previous=None
):
    return {
        "schema": ACCOUNT_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": CURRENT_PRINCIPAL.model_dump(mode="json"),
        "data": {
            "account": account.model_dump(mode="json") if account is not None else None,
            "preferences": preferences,
            "previous_avatar_ref": previous,
            "changed": changed,
        },
    }


def test_wire_request_hides_token_and_passwords_and_preserves_large_preferences():
    token = "private-account-bearer"
    old = "Current1Password"
    new = "NewPassword!2026"
    request = account_request(
        "change_password",
        token=token,
        payload={"old_password": old, "new_password": new},
    )
    assert token not in repr(request)
    assert old not in repr(request) and new not in repr(request)
    assert request.model_dump(mode="json", by_alias=True)["token"] == token

    large = account_request(
        "patch_preferences",
        payload={"patch": {"ai": {"opaque": "x" * 100_000}}},
    )
    assert len(json.dumps(large.model_dump(mode="json", by_alias=True))) > 65_536


def test_wire_request_rejects_duplicate_unknown_and_wrong_avatar_fields():
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        from aap_identity.account_contract import decode_account_request

        decode_account_request(
            b'{"schema":"aap-identity-accounts/v1","schema":"aap-identity-accounts/v1"}'
        )

    with pytest.raises(ValidationError):
        account_request(
            "set_avatar_ref",
            payload={"avatar_ref": "upload:" + "a" * 32, "kind": "preset_or_clear"},
        )
    with pytest.raises(ValidationError):
        account_request(
            "set_avatar_ref",
            payload={
                "avatar_ref": "preset:blue",
                "kind": "upload",
                "bytes": True,
            },
        )


def _avatar_receipt(token="a" * 32, *, size=9):
    return {
        "token": token,
        "sha256": "b" * 64,
        "size_bytes": size,
        "object_version_id": "version-1",
        "maintenance_revision": 1,
        "storage_ref": {
            "schema": 1,
            "purpose": "avatars",
            "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": "http://minio:9000",
            "bucket": "avatars",
            "object_key": token + ".webp",
            "storage_identity": "c" * 64,
        },
    }


def test_upload_avatar_requires_an_owned_verified_receipt():
    token = "a" * 32
    request = account_request(
        "set_avatar_ref",
        payload={
            "avatar_ref": "upload:" + token,
            "kind": "upload",
            "bytes": 9,
            "expected_previous_avatar_ref": "preset:blue",
            "receipt": _avatar_receipt(token),
        },
    )
    payload = request.model_dump(mode="json")["payload"]
    assert payload["expected_previous_avatar_ref"] == "preset:blue"
    assert payload["receipt"]["storage_ref"]["purpose"] == "avatars"

    base = {
        "avatar_ref": "upload:" + token,
        "kind": "upload",
        "bytes": 9,
        "receipt": _avatar_receipt(token),
    }
    with pytest.raises(ValidationError):
        account_request("set_avatar_ref", payload=base)
    for broken in (
        {**base, "expected_previous_avatar_ref": None, "receipt": None},
        {
            **base,
            "expected_previous_avatar_ref": None,
            "receipt": {**_avatar_receipt(token), "size_bytes": 10},
        },
        {
            **base,
            "expected_previous_avatar_ref": None,
            "receipt": _avatar_receipt("d" * 32),
        },
        {
            **base,
            "expected_previous_avatar_ref": None,
            "receipt": {
                key: value
                for key, value in _avatar_receipt(token).items()
                if key != "maintenance_revision"
            },
        },
        {
            **base,
            "expected_previous_avatar_ref": None,
            "receipt": {**_avatar_receipt(token), "maintenance_revision": 0},
        },
        {
            **base,
            "expected_previous_avatar_ref": None,
            "receipt": {
                **(_avatar_receipt(token)),
                "storage_ref": {
                    **_avatar_receipt(token)["storage_ref"],
                    "object_key": "other.webp",
                },
            },
        },
    ):
        with pytest.raises(ValidationError):
            account_request("set_avatar_ref", payload=broken)

    with pytest.raises(ValidationError):
        account_request(
            "set_avatar_ref",
            payload={
                "avatar_ref": "preset:blue",
                "kind": "preset_or_clear",
                "bytes": None,
                "receipt": _avatar_receipt(token),
            },
        )


def test_wire_reply_binds_operation_id_actor_and_credential_identity():
    request = account_request("read_self_account")
    reply = decode_account_reply(
        request,
        account_success(request, account=account_record()),
        expected_principal=CURRENT_PRINCIPAL,
    )
    assert reply.operation == "read_self_account"
    assert reply.data.account.id == ACTOR_ID

    wrong = account_success(request, account=account_record(id=uuid.uuid4()))
    with pytest.raises(IdentityUnavailable):
        decode_account_reply(request, wrong, expected_principal=CURRENT_PRINCIPAL)

    wrong_operation = account_success(request, account=account_record())
    wrong_operation["operation"] = "heartbeat"
    with pytest.raises(IdentityUnavailable):
        decode_account_reply(request, wrong_operation, expected_principal=principal())

    wrong_principal = principal(credential_id=uuid.uuid4())
    with pytest.raises(IdentityUnavailable):
        decode_account_reply(
            request,
            account_success(request, account=account_record()),
            expected_principal=wrong_principal,
        )


def test_wire_success_enforces_operation_specific_data_and_avatar_noop_shape():
    request = account_request("get_preferences")
    with pytest.raises(IdentityUnavailable):
        decode_account_reply(
            request,
            account_success(request, account=account_record()),
            expected_principal=CURRENT_PRINCIPAL,
        )

    avatar = account_request(
        "set_avatar_ref",
        payload={"avatar_ref": "preset:blue", "kind": "preset_or_clear"},
    )
    with pytest.raises(IdentityUnavailable):
        decode_account_reply(
            avatar,
            account_success(
                avatar,
                account=account_record(avatar_ref="preset:blue"),
                changed=False,
                previous="upload:" + "a" * 32,
            ),
            expected_principal=CURRENT_PRINCIPAL,
        )


def test_account_policy_failure_is_closed_and_never_echoes_secrets():
    request = account_request(
        "change_password",
        token="secret-token-value",
        payload={
            "old_password": "old-password-secret",
            "new_password": "lowercaseonly",
        },
    )
    detail = [
        {
            "type": "value_error",
            "loc": ["body", "new_password"],
            "msg": "Value error, 密码需包含大写字母; 密码需包含数字",
        }
    ]
    failure = account_failure_reply(AccountFailure("policy_rejected", 422, detail))
    with pytest.raises(AccountFailure) as error:
        decode_account_reply(request, failure)
    assert error.value.status_code == 422
    assert error.value.detail == detail
    assert "old-password-secret" not in json.dumps(failure)
    assert "secret-token-value" not in json.dumps(failure)

    echoed = account_failure_reply(
        AccountFailure(
            "policy_rejected",
            422,
            [{"type": "value_error", "loc": ["body"], "msg": "old-password-secret"}],
        )
    )
    with pytest.raises(IdentityUnavailable):
        decode_account_reply(request, echoed)

    with pytest.raises(ValidationError):
        AccountFailureReply.model_validate(
            {
                "schema": ACCOUNT_PROTOCOL,
                "ok": False,
                "error": {"code": "account_busy"},
            }
        )


def test_account_failure_nonpolicy_fields_are_required_and_null():
    request = account_request("heartbeat")
    failure = account_failure_reply("account_busy")
    assert failure["error"] == {
        "code": "account_busy",
        "status_code": None,
        "detail": None,
    }
    with pytest.raises(AccountFailure) as caught:
        decode_account_reply(request, failure)
    assert caught.value.code == "account_busy"


def test_native_prepare_and_apply_decoders_bind_actor_operation_and_receipt_data():
    request = account_request("patch_preferences", payload={"patch": {"ui": {"x": 1}}})
    actor = principal()
    command = native_account_prepare_command(
        request,
        actor=actor,
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
    )
    encoded = command.model_dump(mode="json", by_alias=True)
    assert "token" not in encoded
    decoded_command = decode_native_prepare_command(encoded)
    assert decoded_command.request_fingerprint == "a" * 64

    prepared = {
        "schema": HELPER_NATIVE_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": actor.model_dump(mode="json"),
        "data": {
            "account": account_record(preferences={"ui": {"x": 1}}).model_dump(
                mode="json"
            ),
            "password_hash": None,
            "replay": None,
        },
    }
    result = decode_native_prepare_reply(decoded_command, prepared)
    assert result.data.account.preferences["ui"]["x"] == 1

    apply = native_account_apply_command(
        decoded_command,
        base_value={"ui": {"x": 1}},
        new_value={"ui": {"x": 2}},
        response_preferences={"ui": {"x": 2}},
    )
    decoded_apply = decode_native_apply_command(
        apply.model_dump(mode="json", by_alias=True)
    )
    applied = NativeAccountReply(
        schema=HELPER_NATIVE_PROTOCOL,
        ok=True,
        operation="patch_preferences",
        operation_id=request.operation_id,
        principal=actor,
        data=AccountResultData(
            account=None,
            preferences={"ui": {"x": 2}},
            previous_avatar_ref=None,
            changed=None,
        ),
    )
    assert (
        decode_native_account_reply(decoded_apply, applied).data.preferences["ui"]["x"]
        == 2
    )

    malformed = dict(prepared)
    malformed["operation_id"] = str(uuid.uuid4())
    with pytest.raises(IdentityUnavailable):
        decode_native_prepare_reply(decoded_command, malformed)
