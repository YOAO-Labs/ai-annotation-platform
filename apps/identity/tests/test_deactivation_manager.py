from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityUnavailable
from aap_identity.deactivation_contract import decode_native_deactivation_command
from aap_identity.deactivation_manager import (
    DeactivationManager,
    deactivation_request_fingerprint,
)
from deactivation_helpers import (
    SECRET,
    deactivation_request,
    native_user_success,
    requested_account,
)
from session_helpers import principal


class Authenticator:
    def __init__(self, verified=None):
        self.verified = verified or VerifiedCredential(principal(role="employee"), None)

    async def authenticate_bound(self, token):
        self.token = token
        return self.verified


class Transaction:
    def __init__(self, data):
        self.data = data

    async def execute_deactivation_command(self, payload):
        self.data.command = decode_native_deactivation_command(payload)
        self.data.events.append("execute")
        return self.data.result(self.data.command)


class Data:
    def __init__(self, result):
        self.result = result
        self.events = []
        self.command = None
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield Transaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


@pytest.mark.asyncio
async def test_manager_binds_fingerprint_actor_and_validates_before_commit():
    request = deactivation_request(
        "request_self_deactivation",
        token="private-user-bearer",
        payload={"reason": "personal note"},
    )
    current = principal(
        credential_type="api_key",
        role="project_admin",
        name="Fresh role and name",
    )
    auth = Authenticator(VerifiedCredential(current, "private-key-hash"))

    def result(command):
        return native_user_success(
            command, requested_account(), current_principal=current
        )

    data = Data(result)
    reply = await DeactivationManager(auth, data, SECRET).execute(request)

    assert reply.operation == "request_self_deactivation"
    assert reply.principal.name == "Fresh role and name"
    assert data.command.actor == current
    assert data.command.expected_key_hash == "private-key-hash"
    assert data.command.request_fingerprint == deactivation_request_fingerprint(
        SECRET, request
    )
    serialized = json.dumps(data.command.model_dump(mode="json", by_alias=True))
    assert request.token not in serialized
    assert "personal note" in serialized
    assert data.events == ["execute"]
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_manager_rolls_back_when_native_reply_is_not_operation_bound():
    request = deactivation_request("cancel_self_deactivation")

    def result(command):
        raw = native_user_success(command, requested_account())
        raw["operation_id"] = str(uuid.uuid4())
        return raw

    data = Data(result)
    with pytest.raises(IdentityUnavailable):
        await DeactivationManager(Authenticator(), data, SECRET).execute(request)
    assert data.rolled_back and not data.committed
