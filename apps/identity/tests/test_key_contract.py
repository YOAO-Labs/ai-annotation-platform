from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aap_identity.contract import (
    AuthenticateRequest,
    Candidate,
    IdentityUnavailable,
    Principal,
)
from aap_identity.key_contract import (
    KEY_PROTOCOL,
    NATIVE_KEY_PROTOCOL,
    CreateKeyReply,
    CreateKeyRequest,
    EmptyPayload,
    KeyCreatedData,
    KeyRecord,
    NativeCreateReply,
    NativeListCommand,
    NativeKeyRecordData,
    NativeListReply,
    NativeKeyListData,
    NativeUpdateCommand,
    NewKey,
    UpdateKeyRequest,
    decode_key_reply,
    decode_key_request,
    decode_native_key_reply,
    native_key_command,
)

ACTOR_ID = uuid.uuid4()
KEY_ID = uuid.uuid4()
OPERATION_ID = uuid.uuid4()
SECRET_HASH = "$2b$12$" + "a" * 53
DERIVATION_ID = "b" * 64
PLAINTEXT = "ak_" + "c" * 32
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def principal(credential_type="jwt", **changes):
    value = {
        "actor_id": str(ACTOR_ID),
        "name": "Current account",
        "email": "current@example.test",
        "role": "employee",
        "is_active": True,
        "credential_type": credential_type,
        "credential_id": str(uuid.uuid4()),
        "generation": 3 if credential_type == "jwt" else None,
        "scopes": None if credential_type == "jwt" else ["datasets:read"],
        "expires_at": "2026-10-20T01:00:00Z" if credential_type == "jwt" else None,
    }
    value.update(changes)
    return Principal.model_validate_json(json.dumps(value))


def key_record(**changes):
    value = {
        "id": str(KEY_ID),
        "name": "automation",
        "key_prefix": PLAINTEXT[:12],
        "scopes": ["datasets:read"],
        "expires_at": None,
        "last_used_at": None,
        "revoked_at": None,
        "created_at": NOW.isoformat(),
    }
    value.update(changes)
    return KeyRecord.model_validate_json(json.dumps(value))


def wire_request(operation="list_api_keys", payload=None, **changes):
    value = {
        "schema": KEY_PROTOCOL,
        "operation": operation,
        "operation_id": str(OPERATION_ID),
        "token": "private-bearer-value",
        "payload": {} if payload is None else payload,
    }
    value.update(changes)
    return value


def native_reply(operation="list_api_keys", data=None, **changes):
    value = {
        "schema": NATIVE_KEY_PROTOCOL,
        "ok": True,
        "operation": operation,
        "operation_id": str(OPERATION_ID),
        "principal": principal().model_dump(mode="json"),
        "data": {"items": []} if data is None else data,
    }
    value.update(changes)
    return value


def test_request_union_is_strict_and_preserves_patch_presence():
    update = decode_key_request(
        wire_request(
            "update_api_key",
            {
                "key_id": str(KEY_ID),
                "patch": {"expires_in_days": None},
            },
        )
    )
    assert isinstance(update, UpdateKeyRequest)
    assert update.payload.patch.model_fields_set == {"expires_in_days"}
    command = native_key_command(
        update,
        actor=principal(),
        expected_key_hash=None,
        new_key=None,
    )
    assert command["payload"] == {
        "key_id": str(KEY_ID),
        "patch": {"expires_in_days": None},
    }

    for invalid in (
        wire_request("update_api_key", {"key_id": str(KEY_ID)}),
        wire_request(
            "update_api_key",
            {"key_id": str(KEY_ID), "patch": {"name": None}},
        ),
        wire_request(
            "create_api_key",
            {"name": "new", "expires_in_days": True},
        ),
        wire_request("list_api_keys", {"key_id": str(KEY_ID)}),
        {**wire_request(), "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            decode_key_request(invalid)


def test_create_defaults_are_canonicalized_for_native_receipt_scope():
    request = decode_key_request(wire_request("create_api_key", {"name": "new"}))
    assert isinstance(request, CreateKeyRequest)
    assert request.payload.scopes == [] and request.payload.expires_in_days is None
    command = native_key_command(
        request,
        actor=principal(),
        expected_key_hash=None,
        new_key=NewKey(
            key_id=OPERATION_ID,
            key_prefix=PLAINTEXT[:12],
            key_hash=SECRET_HASH,
            derivation_id=DERIVATION_ID,
        ),
    )
    assert command["payload"] == {
        "name": "new",
        "scopes": [],
        "expires_in_days": None,
    }
    assert "token" not in command and "plaintext" not in command
    assert command["new_key"]["key_hash"] == SECRET_HASH


def test_native_command_binds_api_key_hash_and_rejects_wrong_hash_shape():
    actor = principal(
        "api_key",
        credential_id=str(KEY_ID),
        scopes=["keys:manage"],
    )
    request = decode_key_request(wire_request())
    historical_hash = "$2b$04$" + "a" * 53
    command = native_key_command(
        request,
        actor=actor,
        expected_key_hash=historical_hash,
        new_key=None,
    )
    assert command["expected_key_hash"] == historical_hash
    assert command["actor"]["credential_id"] == str(KEY_ID)
    with pytest.raises(ValidationError):
        NativeUpdateCommand.model_validate(
            {
                "schema": NATIVE_KEY_PROTOCOL,
                "operation": "update_api_key",
                "operation_id": OPERATION_ID,
                "actor": actor,
                "expected_key_hash": "",
                "payload": {"key_id": KEY_ID, "patch": {}},
                "new_key": None,
            }
        )
    with pytest.raises(ValidationError):
        NativeUpdateCommand.model_validate(
            {
                "schema": NATIVE_KEY_PROTOCOL,
                "operation": "update_api_key",
                "operation_id": OPERATION_ID,
                "actor": actor,
                "expected_key_hash": "x" * 256,
                "payload": {"key_id": KEY_ID, "patch": {}},
                "new_key": None,
            }
        )
    with pytest.raises(ValidationError):
        NewKey(
            key_id=KEY_ID,
            key_prefix=PLAINTEXT[:12],
            key_hash=historical_hash,
            derivation_id=DERIVATION_ID,
        )


def test_public_reply_decoder_binds_operation_id_target_and_secret_shape():
    request = decode_key_request(wire_request("create_api_key", {"name": "automation"}))
    record = key_record(id=str(OPERATION_ID))
    reply = {
        "schema": KEY_PROTOCOL,
        "ok": True,
        "operation": "create_api_key",
        "operation_id": str(OPERATION_ID),
        "principal": principal().model_dump(mode="json"),
        "data": {"key": record.model_dump(mode="json"), "plaintext": PLAINTEXT},
    }
    parsed = decode_key_reply(request, reply)
    assert isinstance(parsed, CreateKeyReply)
    assert parsed.data.plaintext == PLAINTEXT
    assert "key_hash" not in parsed.model_dump(mode="json")

    for invalid in (
        {**reply, "operation": "update_api_key"},
        {**reply, "operation_id": str(uuid.uuid4())},
        {
            **reply,
            "data": {
                "key": {**reply["data"]["key"], "id": str(uuid.uuid4())},
                "plaintext": PLAINTEXT,
            },
        },
        {
            **reply,
            "data": {
                "key": {**reply["data"]["key"], "key_hash": SECRET_HASH},
                "plaintext": PLAINTEXT,
            },
        },
        {**reply, "data": {**reply["data"], "plaintext": "secret-token"}},
    ):
        with pytest.raises(IdentityUnavailable):
            decode_key_reply(request, invalid)


def test_targeted_update_and_revoke_replies_require_the_requested_key_id():
    update_request = decode_key_request(
        wire_request(
            "update_api_key",
            {"key_id": str(KEY_ID), "patch": {}},
        )
    )
    update_reply = {
        "schema": KEY_PROTOCOL,
        "ok": True,
        "operation": "update_api_key",
        "operation_id": str(OPERATION_ID),
        "principal": principal().model_dump(mode="json"),
        "data": {"key": key_record(id=str(KEY_ID)).model_dump(mode="json")},
    }
    assert decode_key_reply(update_request, update_reply).operation == "update_api_key"
    with pytest.raises(IdentityUnavailable):
        decode_key_reply(
            update_request,
            {
                **update_reply,
                "data": {
                    "key": key_record(id=str(uuid.uuid4())).model_dump(mode="json")
                },
            },
        )

    revoke_request = decode_key_request(
        wire_request("revoke_api_key", {"key_id": str(KEY_ID)})
    )
    revoke_reply = {
        "schema": KEY_PROTOCOL,
        "ok": True,
        "operation": "revoke_api_key",
        "operation_id": str(OPERATION_ID),
        "principal": principal().model_dump(mode="json"),
        "data": {"key_id": str(KEY_ID), "revoked": True},
    }
    assert decode_key_reply(revoke_request, revoke_reply).operation == "revoke_api_key"
    with pytest.raises(IdentityUnavailable):
        decode_key_reply(
            revoke_request,
            {**revoke_reply, "data": {"key_id": str(uuid.uuid4()), "revoked": True}},
        )


def test_native_reply_binds_actor_credential_but_accepts_current_account_fields():
    request = decode_key_request(wire_request())
    expected = principal("api_key", credential_id=str(KEY_ID))
    refreshed = expected.model_copy(
        update={
            "name": "Renamed account",
            "email": "new@example.test",
            "role": "project_admin",
            "scopes": ["datasets:read", "keys:manage"],
            "expires_at": datetime(2026, 10, 20, 2, tzinfo=UTC),
        }
    )
    reply = NativeListReply(
        schema=NATIVE_KEY_PROTOCOL,
        ok=True,
        operation="list_api_keys",
        operation_id=OPERATION_ID,
        principal=refreshed,
        data=NativeKeyListData(items=[]),
    )
    decoded = decode_native_key_reply(
        request,
        reply.model_dump(mode="json", by_alias=True),
        expected_principal=expected,
        new_key=None,
    )
    assert decoded.principal.name == "Renamed account"

    bad = reply.model_copy(
        update={
            "principal": refreshed.model_copy(update={"credential_id": uuid.uuid4()})
        }
    )
    with pytest.raises(IdentityUnavailable):
        decode_native_key_reply(
            request,
            bad.model_dump(mode="json", by_alias=True),
            expected_principal=expected,
            new_key=None,
        )

    jwt_actor = principal()
    changed_jwt_expiry = NativeListReply(
        schema=NATIVE_KEY_PROTOCOL,
        ok=True,
        operation="list_api_keys",
        operation_id=OPERATION_ID,
        principal=jwt_actor.model_copy(
            update={"expires_at": jwt_actor.expires_at + timedelta(seconds=1)}
        ),
        data=NativeKeyListData(items=[]),
    )
    with pytest.raises(IdentityUnavailable):
        decode_native_key_reply(
            request,
            changed_jwt_expiry.model_dump(mode="json", by_alias=True),
            expected_principal=jwt_actor,
            new_key=None,
        )


def test_secrets_are_hidden_from_repr_but_remain_in_wire_serialization():
    token = "private-bearer-value"
    auth_request = AuthenticateRequest(token=token)
    request = decode_key_request(wire_request(token=token))
    assert token not in repr(auth_request) and token not in repr(request)
    assert auth_request.model_dump()["token"] == token
    assert request.model_dump(mode="json")["token"] == token

    created = KeyCreatedData(key=key_record(), plaintext=PLAINTEXT)
    assert PLAINTEXT not in repr(created)
    assert created.model_dump()["plaintext"] == PLAINTEXT

    historical_hash = "$2b$04$" + "a" * 53
    candidate = Candidate(id=KEY_ID, key_hash=historical_hash)
    issued = NewKey(
        key_id=KEY_ID,
        key_prefix=PLAINTEXT[:12],
        key_hash=SECRET_HASH,
        derivation_id=DERIVATION_ID,
    )
    actor = principal("api_key", credential_id=str(KEY_ID))
    command = NativeListCommand(
        schema=NATIVE_KEY_PROTOCOL,
        operation="list_api_keys",
        operation_id=OPERATION_ID,
        actor=actor,
        expected_key_hash=historical_hash,
        payload=EmptyPayload(),
        new_key=None,
    )
    assert historical_hash not in repr(candidate)
    assert historical_hash not in repr(command)
    assert SECRET_HASH not in repr(issued)
    assert candidate.model_dump()["key_hash"] == historical_hash
    assert command.model_dump(by_alias=True)["expected_key_hash"] == historical_hash
    assert issued.model_dump()["key_hash"] == SECRET_HASH


def test_native_create_reply_has_no_hash_field_and_binds_prefix():
    request = decode_key_request(wire_request("create_api_key", {"name": "automation"}))
    assert isinstance(request, CreateKeyRequest)
    new_key = NewKey(
        key_id=OPERATION_ID,
        key_prefix=PLAINTEXT[:12],
        key_hash=SECRET_HASH,
        derivation_id=DERIVATION_ID,
    )
    record = key_record(id=str(OPERATION_ID))
    current_principal = principal()
    native = NativeCreateReply(
        schema=NATIVE_KEY_PROTOCOL,
        ok=True,
        operation="create_api_key",
        operation_id=OPERATION_ID,
        principal=current_principal,
        data=NativeKeyRecordData(key=record),
    )
    decoded = decode_native_key_reply(
        request,
        native.model_dump(mode="json", by_alias=True),
        expected_principal=current_principal,
        new_key=new_key,
    )
    assert isinstance(decoded, NativeCreateReply)

    wrong_prefix = native.model_copy(
        update={
            "data": NativeKeyRecordData(
                key=key_record(id=str(OPERATION_ID), key_prefix="ak_123456789")
            )
        }
    )
    with pytest.raises(IdentityUnavailable):
        decode_native_key_reply(
            request,
            wrong_prefix.model_dump(mode="json", by_alias=True),
            expected_principal=current_principal,
            new_key=new_key,
        )
    for mismatch in (
        native.model_copy(update={"operation": "rotate_api_key"}),
        native.model_copy(update={"operation_id": uuid.uuid4()}),
        native.model_copy(
            update={"data": NativeKeyRecordData(key=key_record(id=str(uuid.uuid4())))}
        ),
    ):
        with pytest.raises(IdentityUnavailable):
            decode_native_key_reply(
                request,
                mismatch.model_dump(mode="json", by_alias=True),
                expected_principal=current_principal,
                new_key=new_key,
            )


def test_naive_public_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        KeyRecord.model_validate(
            {
                "id": KEY_ID,
                "name": "key",
                "key_prefix": PLAINTEXT[:12],
                "scopes": [],
                "expires_at": None,
                "last_used_at": None,
                "revoked_at": None,
                "created_at": datetime(2026, 10, 20),
            }
        )
