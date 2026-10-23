from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityFailure, IdentityUnavailable
from aap_identity.directory_contract import (
    NATIVE_DIRECTORY_PROTOCOL,
    decode_native_directory_command,
)
from aap_identity.directory_manager import (
    DirectoryManager,
    directory_request_fingerprint,
)
from directory_helpers import (
    DIRECTORY_SECRET,
    directory_export_user,
    directory_request,
    directory_user,
    native_success,
)
from session_helpers import audit_context, principal


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
    def __init__(self, data):
        self.data = data

    async def execute_directory_command(self, payload):
        self.data.command = decode_native_directory_command(payload)
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
async def test_manager_binds_principal_hmac_and_validates_before_commit():
    token = "manager-private-bearer"
    request = directory_request("list_users", token=token)
    actor = principal(role="project_admin", credential_type="api_key")
    key_hash = "$2b$10$" + ("." * 53)
    verified = VerifiedCredential(actor, key_hash)
    auth = Authenticator(verified)
    user = directory_user(role="employee")

    data = Data(lambda command: native_success(command, {"users": [user]}))
    reply = await DirectoryManager(auth, data, DIRECTORY_SECRET).execute(request)

    assert reply.operation == "list_users"
    assert reply.principal.actor_id == actor.actor_id
    assert data.command.protocol == NATIVE_DIRECTORY_PROTOCOL
    assert data.command.actor == actor
    assert data.command.expected_key_hash == key_hash
    assert data.command.request_fingerprint == directory_request_fingerprint(
        DIRECTORY_SECRET, request
    )
    serialized = json.dumps(data.command.model_dump(mode="json", by_alias=True))
    assert token not in serialized
    assert "replayed" not in reply.model_dump(mode="json", by_alias=True)
    assert data.events == ["execute"]
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_manager_rolls_back_bad_native_data_before_wire_reply():
    request = directory_request("query_users", payload={"page": 2, "page_size": 5})

    def malformed(command):
        return native_success(
            command,
            {"items": [], "total": 0, "page": 1, "page_size": 5, "pages": 0},
        )

    data = Data(malformed)
    with pytest.raises(IdentityUnavailable):
        await DirectoryManager(Authenticator(), data, DIRECTORY_SECRET).execute(request)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_manager_preserves_export_replay_metadata_without_exposing_replayed_flag():
    request = directory_request(
        "export_users", audit=audit_context(request_id="new-request")
    )
    actor = principal(role="super_admin")
    current = actor.model_copy(update={"email": "new@example.test", "name": "New"})
    frozen_data = {
        "users": [directory_export_user(role="employee", status="offline")],
        "exported_by": "old@example.test",
        "exported_at": "2026-10-19T10:00:00Z",
        "request_id": "old-request",
    }
    data = Data(
        lambda command: native_success(
            command, frozen_data, replayed=True, current_principal=current
        )
    )
    reply = await DirectoryManager(
        Authenticator(VerifiedCredential(actor, None)), data, DIRECTORY_SECRET
    ).execute(request)
    assert reply.data.exported_by == "old@example.test"
    assert reply.data.request_id == "old-request"
    assert "replayed" not in reply.model_dump(mode="json", by_alias=True)
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_manager_refuses_fresh_export_with_unbound_metadata_before_commit():
    request = directory_request("export_users")
    actor = principal(role="super_admin")
    data = Data(
        lambda command: native_success(
            command,
            {
                "users": [directory_export_user(role="employee")],
                "exported_by": "different@example.test",
                "exported_at": "2026-10-19T10:00:00Z",
                "request_id": "other-request",
            },
            replayed=False,
        )
    )
    with pytest.raises(IdentityUnavailable):
        await DirectoryManager(
            Authenticator(VerifiedCredential(actor, None)), data, DIRECTORY_SECRET
        ).execute(request)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_authentication_failure_keeps_identity_code_and_never_opens_transaction():
    request = directory_request("user_stats")
    data = Data(lambda command: {})
    with pytest.raises(Exception) as raised:
        await DirectoryManager(
            Authenticator(error=IdentityFailure("credential_invalid")),
            data,
            DIRECTORY_SECRET,
        ).execute(request)
    assert getattr(raised.value, "code") == "credential_invalid"
    assert not data.events and not data.committed and not data.rolled_back
