from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityFailure, IdentityUnavailable
from aap_identity.deletion_contract import (
    NATIVE_DELETION_PROTOCOL,
    DeletionFailure,
    decode_native_deletion_command,
    deletion_failure_reply,
)
from aap_identity.deletion_manager import (
    DeletionManager,
    deletion_derivation_id,
    deletion_request_fingerprint,
)
from deletion_helpers import (
    DELETION_SECRET,
    OPAQUE_KEY_HASH,
    deletion_request,
    deletion_user,
    native_success,
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

    async def execute_deletion_command(self, payload):
        command = decode_native_deletion_command(payload)
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
async def test_manager_binds_command_and_validates_frozen_success_before_commit():
    admitted = principal(role="project_admin")
    current = admitted.model_copy(
        update={"name": "Current admin", "role": "super_admin"}
    )
    request = deletion_request(token="private-token")
    data = Data(
        lambda command: native_success(
            command,
            deletion_user(disabled_kind="suspended", disabled_reason="old reason"),
            replayed=True,
            current_principal=current,
        )
    )
    manager = DeletionManager(
        Authenticator(VerifiedCredential(admitted, None)), data, DELETION_SECRET
    )

    reply = await manager.execute(request)

    command = data.commands[0]
    assert data.events == ["transaction", "execute"]
    assert data.committed and not data.rolled_back
    assert command.actor == admitted
    assert command.expected_key_hash is None
    assert command.derivation_id == deletion_derivation_id(DELETION_SECRET)
    assert command.request_fingerprint == deletion_request_fingerprint(
        DELETION_SECRET, request
    )
    assert command.protocol == NATIVE_DELETION_PROTOCOL
    assert "token" not in command.model_dump(mode="json", by_alias=True)
    assert request.token not in json.dumps(
        command.model_dump(mode="json", by_alias=True)
    )
    assert reply.principal.name == "Current admin"
    assert reply.data.user.disabled_kind == "suspended"
    assert "replayed" not in reply.model_dump(mode="json", by_alias=True)


@pytest.mark.asyncio
async def test_manager_binds_opaque_api_key_hash_and_receiver():
    actor = principal(
        credential_type="api_key",
        role="super_admin",
        generation=None,
        scopes=["users:read", "users:write"],
    )
    request = deletion_request(transfer_to_user_id=principal(role="employee").actor_id)
    data = Data(
        lambda command: native_success(
            command, deletion_user(), current_principal=actor
        )
    )
    manager = DeletionManager(
        Authenticator(VerifiedCredential(actor, OPAQUE_KEY_HASH)),
        data,
        DELETION_SECRET,
    )
    await manager.execute(request)
    assert data.commands[0].expected_key_hash == OPAQUE_KEY_HASH
    assert (
        data.commands[0].payload.transfer_to_user_id
        == request.payload.transfer_to_user_id
    )


@pytest.mark.asyncio
async def test_manager_rolls_back_malformed_native_reply_before_commit():
    actor = principal(role="super_admin")
    request = deletion_request()
    data = Data(
        lambda command: native_success(
            command, deletion_user(is_active=True), current_principal=actor
        )
    )
    manager = DeletionManager(
        Authenticator(VerifiedCredential(actor, None)), data, DELETION_SECRET
    )
    with pytest.raises(IdentityUnavailable):
        await manager.execute(request)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_manager_accepts_only_closed_native_failure_then_rolls_back():
    actor = principal(role="super_admin")
    request = deletion_request()
    data = Data(
        lambda command: {
            "schema": NATIVE_DELETION_PROTOCOL,
            "ok": False,
            "error": {
                "code": "manager_required",
                "status_code": 403,
                "detail": "当前账号已无管理权限",
            },
        }
    )
    manager = DeletionManager(
        Authenticator(VerifiedCredential(actor, None)), data, DELETION_SECRET
    )
    with pytest.raises(DeletionFailure) as failure:
        await manager.execute(request)
    assert failure.value.code == "manager_required"
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_manager_preserves_pending_409_sample_and_rolls_back_without_echo():
    actor = principal(role="super_admin")
    token = "sensitive-delete-bearer"
    request = deletion_request(token=token)
    sample_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    data = Data(
        lambda command: {
            "schema": NATIVE_DELETION_PROTOCOL,
            "ok": False,
            "error": {
                "code": "has_pending_tasks",
                "status_code": 409,
                "detail": {
                    "reason": "has_pending_tasks",
                    "pending_task_count": 1,
                    "locked_task_count": 2,
                    "sample_task_ids": [sample_id],
                    "message": ("该用户当前有 1 个未完成任务、2 个锁定任务，需先转交"),
                },
            },
        }
    )
    manager = DeletionManager(
        Authenticator(VerifiedCredential(actor, None)), data, DELETION_SECRET
    )

    with pytest.raises(DeletionFailure) as failure:
        await manager.execute(request)

    assert failure.value.code == "has_pending_tasks"
    assert failure.value.status_code == 409
    assert failure.value.detail["sample_task_ids"] == [sample_id]
    public = deletion_failure_reply(failure.value)
    assert token not in json.dumps(public)
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_auth_failure_does_not_open_transaction():
    data = Data(lambda command: {})
    manager = DeletionManager(
        Authenticator(error=IdentityFailure("credential_invalid")),
        data,
        DELETION_SECRET,
    )
    with pytest.raises(DeletionFailure) as failure:
        await manager.execute(deletion_request())
    assert failure.value.code == "credential_invalid"
    assert not data.events and not data.committed and not data.rolled_back
