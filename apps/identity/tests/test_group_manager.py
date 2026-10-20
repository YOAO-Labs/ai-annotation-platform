from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityUnavailable
from aap_identity.group_contract import (
    NATIVE_GROUP_PROTOCOL,
    decode_native_group_command,
)
from aap_identity.group_manager import GroupManager, group_request_fingerprint
from group_helpers import SECRET, group_request, native_success
from session_helpers import principal


class Authenticator:
    def __init__(self, verified=None):
        self.verified = verified or VerifiedCredential(
            principal(role="super_admin"), None
        )

    async def authenticate_bound(self, token):
        self.token = token
        return self.verified


class Transaction:
    def __init__(self, data):
        self.data = data

    async def execute_group_command(self, payload):
        self.data.command = decode_native_group_command(payload)
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
async def test_manager_binds_hmac_principal_and_validates_before_commit():
    request = group_request("list_groups", token="request-secret")
    current = principal(
        credential_type="api_key",
        role="project_admin",
        name="Current display name",
    )
    auth = Authenticator(VerifiedCredential(current, "api-key-hash"))

    def result(command):
        return native_success(command, {"items": []}, current_principal=current)

    data = Data(result)
    reply = await GroupManager(auth, data, SECRET).execute(request)

    assert reply.operation == "list_groups"
    assert reply.principal.actor_id == current.actor_id
    assert data.command.protocol == NATIVE_GROUP_PROTOCOL
    assert data.command.actor == current
    assert data.command.expected_key_hash == "api-key-hash"
    assert data.command.request_fingerprint == group_request_fingerprint(
        SECRET, request
    )
    serialized = json.dumps(data.command.model_dump(mode="json", by_alias=True))
    assert request.token not in serialized
    assert data.events == ["execute"]
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_invalid_native_target_rolls_back_before_reply_or_commit():
    request = group_request("delete_group")

    def result(command):
        return native_success(
            command,
            {"group_id": str(request.operation_id), "deleted": True},
        )

    data = Data(result)
    with pytest.raises(IdentityUnavailable):
        await GroupManager(Authenticator(), data, SECRET).execute(request)
    assert data.rolled_back and not data.committed
