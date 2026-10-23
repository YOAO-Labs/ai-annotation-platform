from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import bcrypt
import pytest

from aap_identity.authenticator import Authenticator, VerifiedCredential
from aap_identity.contract import IdentityUnavailable, Principal, principal_reply
from aap_identity.key_contract import (
    KEY_DERIVATION,
    KEY_PROTOCOL,
    NATIVE_KEY_PROTOCOL,
    CreateKeyReply,
    KeyFailure,
    KeyRecord,
    decode_key_request,
)
from aap_identity.key_manager import KeyManager, derive_key_material
from aap_identity.security import create_access_token

SECRET = "runtime-key-derivation-secret"
ACTOR_ID = uuid.uuid4()
KEY_ID = uuid.uuid4()
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
        "generation": 2 if credential_type == "jwt" else None,
        "scopes": None if credential_type == "jwt" else ["datasets:read"],
        "expires_at": "2026-10-20T01:00:00Z" if credential_type == "jwt" else None,
    }
    value.update(changes)
    return Principal.model_validate_json(json.dumps(value))


def request(
    operation="create_api_key", payload=None, operation_id=None, token="private-token"
):
    return decode_key_request(
        {
            "schema": KEY_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "payload": payload or {"name": "automation"},
        }
    )


def api_record(*, key_id, name, prefix, scopes):
    return KeyRecord.model_validate_json(
        json.dumps(
            {
                "id": str(key_id),
                "name": name,
                "key_prefix": prefix,
                "scopes": scopes,
                "expires_at": None,
                "last_used_at": None,
                "revoked_at": None,
                "created_at": NOW.isoformat(),
            }
        )
    )


class BoundAuth:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def authenticate_bound(self, token):
        self.calls.append(token)
        return self.result


class KeyData:
    def __init__(self, current_principal=None):
        self.current_principal = current_principal or principal()
        self.commands = []
        self.created_record = None
        self.rotated_record = None
        self.failure = None

    async def key_policy(self):
        return {
            "schema": "aap-identity-keys-policy/v1",
            "derivation": KEY_DERIVATION,
        }

    async def execute_key_command(self, payload):
        self.commands.append(payload)
        if self.failure is not None:
            return {
                "schema": NATIVE_KEY_PROTOCOL,
                "ok": False,
                "error": {"code": self.failure},
            }
        actor = Principal.model_validate_json(json.dumps(payload["actor"]))
        operation = payload["operation"]
        shared = {
            "schema": NATIVE_KEY_PROTOCOL,
            "ok": True,
            "operation": operation,
            "operation_id": payload["operation_id"],
            "principal": actor.model_dump(mode="json"),
        }
        if operation == "create_api_key":
            new_key = payload["new_key"]
            if self.created_record is None:
                self.created_record = api_record(
                    key_id=new_key["key_id"],
                    name=payload["payload"]["name"],
                    prefix=new_key["key_prefix"],
                    scopes=payload["payload"]["scopes"],
                )
            return {
                **shared,
                "data": {"key": self.created_record.model_dump(mode="json")},
            }
        if operation == "rotate_api_key":
            target = payload["payload"]["key_id"]
            self.rotated_record = api_record(
                key_id=target,
                name="automation",
                prefix=payload["new_key"]["key_prefix"],
                scopes=["datasets:read"],
            )
            return {
                **shared,
                "data": {"key": self.rotated_record.model_dump(mode="json")},
            }
        raise AssertionError(operation)


@pytest.mark.asyncio
async def test_revoke_translates_native_result_into_the_wire_model():
    actor = principal()
    command = request("revoke_api_key", {"key_id": str(KEY_ID)})

    class RevokeData:
        async def execute_key_command(self, native):
            return {
                "schema": NATIVE_KEY_PROTOCOL,
                "ok": True,
                "operation": "revoke_api_key",
                "operation_id": native["operation_id"],
                "principal": actor.model_dump(mode="json"),
                "data": {"key_id": str(KEY_ID), "revoked": True},
            }

    reply = await KeyManager(
        BoundAuth(VerifiedCredential(actor, None)), RevokeData(), SECRET
    ).execute(command)
    assert reply.operation == "revoke_api_key" and reply.data.key_id == KEY_ID
    assert reply.data.revoked is True


@pytest.mark.asyncio
async def test_authenticate_bound_returns_only_the_verified_api_key_hash():
    token = "ak_" + "q" * 32
    hashed = bcrypt.hashpw(token.encode(), bcrypt.gensalt(rounds=4)).decode()
    key_id = uuid.uuid4()
    key_principal = principal(
        "api_key", credential_id=str(key_id), scopes=["datasets:read"]
    )

    class AdmissionData:
        async def read_api_key_candidates(self, prefix, after_id, limit):
            return {
                "schema": "aap-identity-candidates/v1",
                "items": [{"id": str(key_id), "key_hash": hashed}],
                "next_after": None,
            }

        async def confirm_api_key_principal(self, candidate_id, expected_hash):
            assert candidate_id == key_id and expected_hash == hashed
            return principal_reply(key_principal)

    verified = await Authenticator(AdmissionData(), SECRET).authenticate_bound(token)
    assert verified.principal == key_principal and verified.key_hash == hashed
    assert (
        await Authenticator(AdmissionData(), SECRET).authenticate(token)
    ) == key_principal


def test_verified_credential_repr_hides_the_bound_hash():
    historical_hash = "$2b$04$" + "a" * 53
    bound = VerifiedCredential(
        principal("api_key", credential_id=str(KEY_ID)), historical_hash
    )
    assert historical_hash not in repr(bound)


@pytest.mark.asyncio
async def test_jwt_bound_authentication_keeps_the_public_authenticate_result():
    token = create_access_token(
        str(ACTOR_ID), "viewer", secret=SECRET, expire_minutes=5, gen=4
    )

    class AdmissionData:
        async def read_jwt_principal(
            self, actor_id, jti, generation, expires_at, for_refresh=False
        ):
            current = principal(
                actor_id=str(actor_id),
                credential_id=str(jti),
                generation=generation,
                expires_at=expires_at.isoformat(),
            )
            return principal_reply(current)

    auth = Authenticator(AdmissionData(), SECRET)
    bound = await auth.authenticate_bound(token)
    assert bound.principal.actor_id == ACTOR_ID and bound.key_hash is None
    assert await auth.authenticate(token) == bound.principal


def test_derived_key_material_matches_domain_separated_hmac_definition():
    operation_id = uuid.uuid4()
    master = hmac.new(
        SECRET.encode(), b"aap-identity-api-keys/v1", hashlib.sha256
    ).digest()
    body = (
        b"actor:" + str(ACTOR_ID).encode() + b":operation:" + str(operation_id).encode()
    )
    expected = "ak_" + base64.urlsafe_b64encode(
        hmac.new(master, body, hashlib.sha256).digest()[:24]
    ).decode().rstrip("=")
    plaintext, derivation_id = derive_key_material(SECRET, ACTOR_ID, operation_id)
    assert plaintext == expected and len(plaintext) == 35
    assert derivation_id == hashlib.sha256(master).hexdigest()
    assert derive_key_material(SECRET, ACTOR_ID, operation_id) == (
        plaintext,
        derivation_id,
    )
    assert derive_key_material(SECRET, uuid.uuid4(), operation_id)[0] != plaintext
    assert derive_key_material(SECRET, ACTOR_ID, uuid.uuid4())[0] != plaintext
    assert derive_key_material(SECRET + "2", ACTOR_ID, operation_id)[0] != plaintext
    assert plaintext[:12].startswith("ak_")


@pytest.mark.asyncio
async def test_create_replay_rederives_plaintext_but_never_sends_it_native():
    actor = principal()
    auth = BoundAuth(VerifiedCredential(actor, None))
    data = KeyData(actor)
    command = request(operation_id=uuid.uuid4())
    manager = KeyManager(auth, data, SECRET)

    first = await manager.execute(command)
    second = await manager.execute(command)
    assert isinstance(first, CreateKeyReply) and isinstance(second, CreateKeyReply)
    assert first.data.plaintext == second.data.plaintext
    assert bcrypt.checkpw(
        first.data.plaintext.encode(), data.commands[0]["new_key"]["key_hash"].encode()
    )
    assert data.commands[0]["new_key"]["key_hash"].startswith("$2b$12$")
    assert (
        data.commands[0]["new_key"]["key_hash"]
        != data.commands[1]["new_key"]["key_hash"]
    )
    for native in data.commands:
        assert "token" not in native and "plaintext" not in json.dumps(native)
        assert (
            native["new_key"]["derivation_id"]
            == derive_key_material(SECRET, actor.actor_id, command.operation_id)[1]
        )
    assert auth.calls == ["private-token", "private-token"]


@pytest.mark.asyncio
async def test_self_rotation_returns_fenced_pre_write_principal_without_reauth():
    old_hash = "$2b$12$" + "a" * 53
    actor = principal("api_key", credential_id=str(KEY_ID))
    auth = BoundAuth(VerifiedCredential(actor, old_hash))
    data = KeyData(actor)
    rotate = request(
        "rotate_api_key",
        {"key_id": str(KEY_ID)},
        token="previous-key",
    )
    reply = await KeyManager(auth, data, SECRET).execute(rotate)
    assert reply.operation == "rotate_api_key"
    assert reply.data.key.id == KEY_ID and len(auth.calls) == 1
    native = data.commands[0]
    assert native["expected_key_hash"] == old_hash
    assert native["new_key"]["key_id"] == str(KEY_ID)


@pytest.mark.asyncio
async def test_native_failures_are_typed_and_bad_success_replies_fail_closed():
    actor = principal()
    auth = BoundAuth(VerifiedCredential(actor, None))
    data = KeyData(actor)
    command = request(operation_id=uuid.uuid4())
    data.failure = "operation_conflict"
    with pytest.raises(KeyFailure) as failure:
        await KeyManager(auth, data, SECRET).execute(command)
    assert failure.value.code == "operation_conflict"
    data.failure = None

    async def wrong_target(payload):
        key = api_record(
            key_id=uuid.uuid4(),
            name="automation",
            prefix=payload["new_key"]["key_prefix"],
            scopes=[],
        )
        return {
            "schema": NATIVE_KEY_PROTOCOL,
            "ok": True,
            "operation": "create_api_key",
            "operation_id": payload["operation_id"],
            "principal": payload["actor"],
            "data": {"key": key.model_dump(mode="json")},
        }

    data.execute_key_command = wrong_target
    with pytest.raises(IdentityUnavailable):
        await KeyManager(auth, data, SECRET).execute(command)
