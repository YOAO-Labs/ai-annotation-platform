from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityFailure, IdentityUnavailable
from aap_identity.platform_role_contract import (
    NATIVE_PLATFORM_ROLE_PROTOCOL,
    PlatformRoleFailure,
    decode_native_platform_role_command,
)
from aap_identity.platform_role_manager import (
    PlatformRoleManager,
    platform_role_derivation_id,
    platform_role_request_fingerprint,
)
from platform_role_helpers import (
    OPAQUE_KEY_HASH,
    PLATFORM_ROLE_SECRET,
    native_success,
    platform_role_request,
    role_preview_data,
    role_user,
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
        if self.error is not None:
            raise self.error
        return self.verified


class Transaction:
    def __init__(self, owner):
        self.owner = owner

    async def execute_platform_role_command(self, payload):
        command = decode_native_platform_role_command(payload)
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
async def test_manager_binds_hmac_command_and_validates_preview_before_commit():
    actor = principal(role="super_admin")
    request = platform_role_request("preview_role_change", token="private-bearer")
    data = Data(
        lambda command: native_success(
            command, role_preview_data(), current_principal=actor
        )
    )
    manager = PlatformRoleManager(
        Authenticator(VerifiedCredential(actor, None)), data, PLATFORM_ROLE_SECRET
    )

    reply = await manager.execute(request)

    command = data.commands[0]
    assert data.events == ["transaction", "execute"]
    assert data.committed and not data.rolled_back
    assert command.actor == actor
    assert command.expected_key_hash is None
    assert command.derivation_id == platform_role_derivation_id(PLATFORM_ROLE_SECRET)
    assert command.request_fingerprint == platform_role_request_fingerprint(
        PLATFORM_ROLE_SECRET, request
    )
    serialized = json.dumps(command.model_dump(mode="json", by_alias=True))
    assert request.token not in serialized
    assert reply.data.user_id == request.payload.target_user_id


@pytest.mark.asyncio
async def test_manager_binds_opaque_api_key_hash_and_current_principal_snapshot():
    admitted = principal(
        credential_type="api_key",
        role="super_admin",
        generation=None,
        scopes=["users:read", "users:write"],
    )
    current = admitted.model_copy(
        update={"name": "Current SA", "email": "fresh@example.test"}
    )
    request = platform_role_request("change_platform_role")
    data = Data(
        lambda command: native_success(
            command,
            {"user": role_user()},
            current_principal=current,
        )
    )
    manager = PlatformRoleManager(
        Authenticator(VerifiedCredential(admitted, OPAQUE_KEY_HASH)),
        data,
        PLATFORM_ROLE_SECRET,
    )
    reply = await manager.execute(request)
    assert data.commands[0].expected_key_hash == OPAQUE_KEY_HASH
    assert reply.principal.name == "Current SA"
    assert reply.data.user.role == "employee"
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_manager_preserves_historical_change_replay_without_reapplying():
    actor = principal(role="super_admin")
    request = platform_role_request("change_platform_role", role="employee")
    data = Data(
        lambda command: native_success(
            command,
            {"user": role_user(role="employee", email="frozen@example.test")},
            replayed=True,
            current_principal=actor,
        )
    )
    manager = PlatformRoleManager(
        Authenticator(VerifiedCredential(actor, None)), data, PLATFORM_ROLE_SECRET
    )
    reply = await manager.execute(request)
    assert reply.data.user.email == "frozen@example.test"
    assert reply.data.user.role == "employee"
    assert "replayed" not in reply.model_dump(mode="json", by_alias=True)
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_manager_rejects_misbound_reply_before_commit():
    actor = principal(role="super_admin")
    request = platform_role_request("change_platform_role")

    def wrong_target(command):
        return native_success(
            command,
            {"user": role_user(id=actor.actor_id)},
            current_principal=actor,
        )

    data = Data(wrong_target)
    manager = PlatformRoleManager(
        Authenticator(VerifiedCredential(actor, None)), data, PLATFORM_ROLE_SECRET
    )
    with pytest.raises(IdentityUnavailable):
        await manager.execute(request)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_manager_rejects_native_error_that_would_echo_bearer():
    token = "secret-value-in-invalid-role"
    request = platform_role_request("change_platform_role", role=token, token=token)
    actor = principal(role="super_admin")
    data = Data(
        lambda command: {
            "schema": NATIVE_PLATFORM_ROLE_PROTOCOL,
            "ok": False,
            "error": {
                "code": "invalid_platform_role",
                "status_code": 400,
                "detail": f"非法平台角色: {token}",
            },
        }
    )
    manager = PlatformRoleManager(
        Authenticator(VerifiedCredential(actor, None)), data, PLATFORM_ROLE_SECRET
    )
    with pytest.raises(IdentityUnavailable):
        await manager.execute(request)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_stale_admitted_super_admin_gets_closed_current_role_failure():
    actor = principal(role="super_admin")
    request = platform_role_request("change_platform_role")
    data = Data(
        lambda command: {
            "schema": NATIVE_PLATFORM_ROLE_PROTOCOL,
            "ok": False,
            "error": {
                "code": "super_admin_required",
                "status_code": 403,
                "detail": "当前账号已无管理权限",
            },
        }
    )
    manager = PlatformRoleManager(
        Authenticator(VerifiedCredential(actor, None)), data, PLATFORM_ROLE_SECRET
    )
    with pytest.raises(PlatformRoleFailure) as failure:
        await manager.execute(request)
    assert failure.value.code == "super_admin_required"
    assert failure.value.status_code == 403
    assert failure.value.detail == "当前账号已无管理权限"
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_authentication_failure_does_not_open_transaction():
    data = Data(lambda command: {})
    manager = PlatformRoleManager(
        Authenticator(error=IdentityFailure("credential_invalid")),
        data,
        PLATFORM_ROLE_SECRET,
    )
    with pytest.raises(PlatformRoleFailure) as failure:
        await manager.execute(platform_role_request("preview_role_change"))
    assert failure.value.code == "credential_invalid"
    assert not data.events and not data.committed and not data.rolled_back
