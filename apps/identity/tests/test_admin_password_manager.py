from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC

import bcrypt
import pytest

from aap_identity.admin_password_contract import decode_native_admin_password_command
from aap_identity.admin_password_manager import (
    AdminPasswordManager,
    admin_password_derivation_id,
    admin_password_request_fingerprint,
    derive_admin_password,
)
from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityFailure, IdentityUnavailable
from admin_password_helpers import (
    SECRET,
    admin_password_data,
    admin_password_request,
    native_admin_password_success,
)
from session_helpers import principal


class Authenticator:
    def __init__(self, verified=None, error=None):
        self.verified = verified or VerifiedCredential(
            principal(role="super_admin"), None
        )
        self.error = error

    async def authenticate_bound(self, token):
        self.token = token
        if self.error:
            raise self.error
        return self.verified


class Transaction:
    def __init__(self, owner):
        self.owner = owner

    async def execute_admin_password_command(self, payload):
        command = decode_native_admin_password_command(payload)
        self.owner.commands.append(command)
        self.owner.events.append("execute")
        return self.owner.result(command)


class Data:
    def __init__(self, result):
        self.result = result
        self.events = []
        self.commands = []
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        try:
            yield Transaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


def successful_data():
    values = admin_password_data()
    values.pop("temp_password")
    values["password_admin_reset_at"] = (
        values["password_admin_reset_at"].astimezone(UTC).isoformat()
    )
    return values


@pytest.mark.asyncio
async def test_manager_derives_and_hashes_before_transaction_and_validates_reply(
    monkeypatch,
):
    actor = principal(role="super_admin")
    original_hash = __import__(
        "aap_identity.admin_password_manager", fromlist=["_hash_password_cost12"]
    )._hash_password_cost12

    def hash_and_record(password):
        data.events.append("hash")
        return original_hash(password)

    monkeypatch.setattr(
        "aap_identity.admin_password_manager._hash_password_cost12", hash_and_record
    )
    data = Data(
        lambda command: native_admin_password_success(
            command, successful_data(), current_principal=actor
        )
    )
    auth = Authenticator(VerifiedCredential(actor, None))
    manager = AdminPasswordManager(auth, data, SECRET)
    request = admin_password_request(token="private-bearer")

    reply = await manager.execute(request)

    assert data.events == ["hash", "transaction", "execute"]
    assert data.committed and not data.rolled_back
    command = data.commands[0]
    assert command.actor == actor
    assert command.expected_key_hash is None
    assert command.derivation_id == admin_password_derivation_id(SECRET)
    assert command.request_fingerprint == admin_password_request_fingerprint(
        SECRET, request
    )
    assert bcrypt.checkpw(
        reply.data.temp_password.encode("ascii"), command.new_password_hash.encode()
    )
    assert command.new_password_hash.startswith("$2b$12$")
    assert "private-bearer" not in json.dumps(
        command.model_dump(mode="json", by_alias=True)
    )


@pytest.mark.asyncio
async def test_response_loss_replay_keeps_password_stable_but_rehashes():
    actor = principal(role="super_admin")
    data = Data(
        lambda command: native_admin_password_success(
            command, successful_data(), replayed=True, current_principal=actor
        )
    )
    manager = AdminPasswordManager(
        Authenticator(VerifiedCredential(actor, None)), data, SECRET
    )
    first = await manager.execute(admin_password_request(token="first-token"))
    first_hash = data.commands[-1].new_password_hash
    second = await manager.execute(
        admin_password_request(
            token="second-token",
            audit={
                "method": "POST",
                "path": "/api/v1/users/admin-reset",
                "ip": "192.0.2.99",
                "request_id": "changed-request-id",
            },
        )
    )
    second_hash = data.commands[-1].new_password_hash
    assert first.data.temp_password == second.data.temp_password
    assert first_hash != second_hash
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_bad_native_success_rolls_back_before_commit():
    actor = principal(role="super_admin")

    def wrong_target(command):
        raw = native_admin_password_success(
            command, successful_data(), current_principal=actor
        )
        raw["data"]["target_user_id"] = str(actor.actor_id)
        return raw

    data = Data(wrong_target)
    manager = AdminPasswordManager(
        Authenticator(VerifiedCredential(actor, None)), data, SECRET
    )
    with pytest.raises(IdentityUnavailable):
        await manager.execute(admin_password_request())
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_authentication_failure_does_not_open_transaction():
    data = Data(lambda command: {})
    manager = AdminPasswordManager(
        Authenticator(error=IdentityFailure("credential_invalid")), data, SECRET
    )
    from aap_identity.admin_password_contract import AdminPasswordFailure

    with pytest.raises(AdminPasswordFailure) as failure:
        await manager.execute(admin_password_request())
    assert failure.value.code == "credential_invalid"
    assert not data.events and not data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_authentication_invalid_request_maps_to_closed_http_400():
    data = Data(lambda command: {})
    manager = AdminPasswordManager(
        Authenticator(error=IdentityFailure("invalid_request")), data, SECRET
    )
    from aap_identity.admin_password_contract import AdminPasswordFailure

    with pytest.raises(AdminPasswordFailure) as failure:
        await manager.execute(admin_password_request())
    assert failure.value.code == "invalid_request"
    assert failure.value.status_code == 400
    assert failure.value.detail is None
    assert not data.events and not data.committed and not data.rolled_back


def test_derivation_rejects_noncanonical_fingerprint():
    request = admin_password_request()
    fingerprint = admin_password_request_fingerprint(SECRET, request).upper()
    with pytest.raises(ValueError):
        derive_admin_password(
            SECRET,
            principal(role="super_admin").actor_id,
            request.operation_id,
            fingerprint,
        )
