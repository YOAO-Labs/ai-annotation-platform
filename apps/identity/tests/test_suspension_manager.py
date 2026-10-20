from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityFailure, IdentityUnavailable
from aap_identity.suspension_contract import (
    NATIVE_SUSPENSION_PROTOCOL,
    SuspensionFailure,
    decode_native_suspension_command,
)
from aap_identity.suspension_manager import (
    SuspensionManager,
    suspension_derivation_id,
    suspension_request_fingerprint,
)
from session_helpers import principal
from suspension_helpers import (
    OPAQUE_KEY_HASH,
    SUSPENSION_SECRET,
    native_success,
    suspension_request,
    suspension_user,
)


class Authenticator:
    def __init__(self, verified=None, error=None):
        self.verified = verified or VerifiedCredential(
            principal(role="super_admin"), None
        )
        self.error = error

    async def authenticate_bound(self, token):
        self.token = token
        if self.error is not None:
            raise self.error
        return self.verified


class Transaction:
    def __init__(self, owner):
        self.owner = owner

    async def execute_suspension_command(self, payload):
        command = decode_native_suspension_command(payload)
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


@pytest.mark.asyncio
async def test_manager_binds_unicode_reason_and_validates_before_commit():
    admitted = principal(role="project_admin")
    current = admitted.model_copy(
        update={"name": "Current manager", "role": "super_admin"}
    )
    request = suspension_request(
        "reactivate_account", token="private-token", reason=" 　恢复 "
    )
    data = Data(
        lambda command: native_success(
            command,
            suspension_user(
                is_active=True,
                disabled_kind=None,
                disabled_at=None,
                disabled_by=None,
                disabled_reason=None,
            ),
            current_principal=current,
        )
    )
    manager = SuspensionManager(
        Authenticator(VerifiedCredential(admitted, None)), data, SUSPENSION_SECRET
    )

    reply = await manager.execute(request)

    command = data.commands[0]
    assert data.events == ["transaction", "execute"]
    assert data.committed and not data.rolled_back
    assert command.actor == admitted
    assert command.expected_key_hash is None
    assert command.derivation_id == suspension_derivation_id(SUSPENSION_SECRET)
    assert command.request_fingerprint == suspension_request_fingerprint(
        SUSPENSION_SECRET, request
    )
    assert command.payload.reason_present is True
    assert "reason" not in command.model_dump(mode="json", by_alias=True)["payload"]
    assert request.token not in json.dumps(
        command.model_dump(mode="json", by_alias=True)
    )
    assert reply.principal.name == "Current manager"
    assert reply.principal.role == "super_admin"
    assert "replayed" not in reply.model_dump(mode="json", by_alias=True)


@pytest.mark.asyncio
async def test_manager_binds_opaque_key_hash_and_keeps_noop_snapshot():
    actor = principal(
        credential_type="api_key",
        role="super_admin",
        generation=None,
        scopes=["users:read", "users:write"],
    )
    request = suspension_request("suspend_account")
    data = Data(
        lambda command: native_success(
            command,
            suspension_user(disabled_kind="deleted"),
            replayed=True,
            current_principal=actor,
        )
    )
    manager = SuspensionManager(
        Authenticator(VerifiedCredential(actor, OPAQUE_KEY_HASH)),
        data,
        SUSPENSION_SECRET,
    )
    reply = await manager.execute(request)
    assert data.commands[0].expected_key_hash == OPAQUE_KEY_HASH
    assert reply.data.user.disabled_kind == "deleted"
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_manager_rolls_back_malformed_native_response_before_commit():
    actor = principal(role="super_admin")
    request = suspension_request("suspend_account")
    data = Data(
        lambda command: native_success(
            command,
            suspension_user(is_active=True),
            current_principal=actor,
        )
    )
    manager = SuspensionManager(
        Authenticator(VerifiedCredential(actor, None)), data, SUSPENSION_SECRET
    )
    with pytest.raises(IdentityUnavailable):
        await manager.execute(request)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_stale_manager_role_failure_is_valid_and_rolls_back():
    actor = principal(role="super_admin")
    request = suspension_request("suspend_account")
    data = Data(
        lambda command: {
            "schema": NATIVE_SUSPENSION_PROTOCOL,
            "ok": False,
            "error": {
                "code": "manager_required",
                "status_code": 403,
                "detail": "当前账号已无管理权限",
            },
        }
    )
    manager = SuspensionManager(
        Authenticator(VerifiedCredential(actor, None)), data, SUSPENSION_SECRET
    )
    with pytest.raises(SuspensionFailure) as failure:
        await manager.execute(request)
    assert failure.value.code == "manager_required"
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_authentication_failure_does_not_open_transaction():
    data = Data(lambda command: {})
    manager = SuspensionManager(
        Authenticator(error=IdentityFailure("credential_invalid")),
        data,
        SUSPENSION_SECRET,
    )
    with pytest.raises(SuspensionFailure) as failure:
        await manager.execute(suspension_request("suspend_account"))
    assert failure.value.code == "credential_invalid"
    assert not data.events and not data.committed and not data.rolled_back
