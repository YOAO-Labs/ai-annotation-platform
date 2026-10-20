from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import bcrypt
import pytest
from aap_identity.recovery_contract import (
    NativeRecoveryResult,
    RecoveryFailure,
    decode_native_recovery_apply_command,
    decode_native_recovery_prepare_command,
)
from aap_identity.recovery_crypto import decrypt_mail_token, token_hash
from aap_identity.recovery_manager import RecoveryManager
from recovery_helpers import (
    MAIL_KEY,
    SECRET,
    account,
    actor,
    apply_reply,
    claims,
    policy,
    prepare_reply,
    recovery_request,
    token_record,
)


class Transaction:
    def __init__(self, data):
        self.data = data

    async def prepare_recovery_command(self, payload):
        assert self.data.in_transaction
        command = decode_native_recovery_prepare_command(payload)
        self.data.prepare_command = command
        self.data.events.append("prepare")
        return self.data.prepare(command)

    async def apply_recovery_command(self, payload):
        assert self.data.in_transaction
        command = decode_native_recovery_apply_command(payload)
        self.data.apply_command = command
        self.data.events.append("apply")
        return self.data.apply(command)


class Data:
    def __init__(self, prepare, apply):
        self.prepare = prepare
        self.apply = apply
        self.events = []
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False
        self.prepare_command = None
        self.apply_command = None

    @asynccontextmanager
    async def transaction(self):
        assert not self.in_transaction
        self.in_transaction = True
        try:
            yield Transaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False


def manager(data, *, verify=True):
    return RecoveryManager(
        data,
        SECRET,
        MAIL_KEY,
        email_verification_required=verify,
        allow_open_registration=True,
        frontend_base_url="https://fallback.example.test",
    )


def _empty_result():
    return NativeRecoveryResult(
        account=None,
        claims=None,
        email_verification_required=None,
        completed=None,
    )


@pytest.mark.asyncio
async def test_no_target_reset_is_committed_as_a_no_target_receipt():
    request = recovery_request("request_password_reset")
    data = Data(
        lambda command: prepare_reply(command, eligible=False, actor_value=None),
        lambda command: apply_reply(command, _empty_result()),
    )

    reply = await manager(data).execute(request)

    assert reply.data.model_dump() == {
        "account": None,
        "claims": None,
        "access_token": None,
        "email_verification_required": None,
        "completed": None,
    }
    assert data.events == ["prepare", "apply"]
    assert data.apply_command.token_hash is None
    assert data.apply_command.token_ciphertext is None
    assert data.committed and not data.rolled_back


@pytest.mark.asyncio
async def test_resend_encrypts_random_token_without_crossing_native_boundary():
    request = recovery_request("resend_email_verification")
    data = Data(
        lambda command: prepare_reply(
            command,
            actor_value=actor(),
            eligible=True,
            resolved_policy=policy(),
        ),
        lambda command: apply_reply(command, _empty_result(), actor_value=actor()),
    )

    await manager(data).execute(request)

    command = data.apply_command
    raw_token = decrypt_mail_token(
        command.token_ciphertext, MAIL_KEY, command.encryption_id
    )
    assert len(raw_token) == 64
    assert token_hash(raw_token) == command.token_hash
    serialized = json.dumps(command.model_dump(mode="json", by_alias=True))
    assert raw_token not in serialized
    assert request.payload.email in serialized
    assert data.events == ["prepare", "apply"]


@pytest.mark.asyncio
async def test_registration_with_verification_atomically_returns_no_jwt():
    request = recovery_request("register_open")
    data = Data(
        lambda command: prepare_reply(
            command,
            eligible=True,
            resolved_policy=policy(verify=True),
        ),
        lambda command: apply_reply(
            command,
            NativeRecoveryResult(
                account=account(actor_id=request.operation_id),
                claims=None,
                email_verification_required=True,
                completed=None,
            ),
            actor_value=actor(actor_id=request.operation_id),
        ),
    )

    reply = await manager(data).execute(request)

    assert reply.data.account.email == request.payload.email
    assert reply.data.email_verification_required is True
    assert reply.data.access_token is None and reply.data.claims is None
    assert data.apply_command.password_hash.startswith("$2b$12$")
    assert data.apply_command.token_hash is not None
    assert data.apply_command.token_ciphertext is not None


@pytest.mark.asyncio
async def test_immediate_registration_signs_native_claims_and_replay_reuses_hash():
    request = recovery_request("register_open")
    frozen_claims = claims(request.operation_id, actor_id=request.operation_id)
    frozen = NativeRecoveryResult(
        account=account(actor_id=request.operation_id, verified_at=datetime.now(UTC)),
        claims=frozen_claims,
        email_verification_required=False,
        completed=None,
    )
    current_hash = bcrypt.hashpw(
        request.payload.password.encode(), bcrypt.gensalt(rounds=12)
    ).decode()

    initial = Data(
        lambda command: prepare_reply(
            command,
            eligible=True,
            resolved_policy=policy(verify=False),
        ),
        lambda command: apply_reply(
            command, frozen, actor_value=actor(actor_id=request.operation_id)
        ),
    )
    initial_reply = await manager(initial, verify=False).execute(request)
    assert initial_reply.data.claims == frozen_claims
    assert initial_reply.data.access_token
    assert initial.apply_command.password_hash.startswith("$2b$12$")
    assert initial.apply_command.token_hash is None
    assert initial.apply_command.token_ciphertext is None

    replay = Data(
        lambda command: prepare_reply(
            command,
            actor_value=actor(actor_id=request.operation_id),
            eligible=False,
            replay=frozen,
            password_hash=current_hash,
            resolved_policy=policy(verify=False),
        ),
        lambda command: apply_reply(
            command,
            frozen,
            actor_value=actor(actor_id=request.operation_id),
            replayed=True,
        ),
    )
    replay_reply = await manager(replay, verify=False).execute(request)
    assert replay_reply.data.claims == initial_reply.data.claims
    assert replay_reply.data.access_token == initial_reply.data.access_token
    assert replay.apply_command.password_hash == current_hash
    assert replay.events == ["prepare", "apply"]

    changed_hash = bcrypt.hashpw(
        b"DifferentPassword2026", bcrypt.gensalt(rounds=12)
    ).decode()
    denied = Data(
        lambda command: prepare_reply(
            command,
            actor_value=actor(actor_id=request.operation_id),
            replay=frozen,
            password_hash=changed_hash,
            resolved_policy=policy(verify=False),
        ),
        lambda command: pytest.fail("incorrect current password must not apply"),
    )
    with pytest.raises(RecoveryFailure) as failure:
        await manager(denied, verify=False).execute(request)
    assert failure.value.code == "operation_conflict"
    assert denied.events == ["prepare"] and denied.rolled_back


@pytest.mark.asyncio
async def test_reset_consumes_inactive_target_without_hashing_new_password(monkeypatch):
    request = recovery_request("consume_password_reset")
    digest = token_hash(request.payload.token)
    target = actor(active=False)
    data = Data(
        lambda command: prepare_reply(
            command,
            actor_value=target,
            token=token_record(digest),
            eligible=False,
        ),
        lambda command: apply_reply(
            command,
            NativeRecoveryResult(
                account=None,
                claims=None,
                email_verification_required=None,
                completed=False,
            ),
            actor_value=target,
        ),
    )
    monkeypatch.setattr(
        "aap_identity.recovery_manager.hash_password",
        lambda _password: pytest.fail("inactive reset must not hash a new password"),
    )

    reply = await manager(data).execute(request)

    assert reply.data.completed is False
    assert data.apply_command.password_hash is None
    assert data.apply_command.token_hash == digest
    assert data.events == ["prepare", "apply"]


@pytest.mark.asyncio
async def test_invalid_token_does_not_apply_or_create_a_receipt():
    request = recovery_request("consume_email_verification")
    data = Data(
        lambda command: prepare_reply(command, eligible=False, token=None),
        lambda command: pytest.fail("invalid challenge must not be applied"),
    )

    with pytest.raises(RecoveryFailure) as failure:
        await manager(data).execute(request)

    assert failure.value.code == "invalid_request"
    assert failure.value.status_code == 400
    assert data.events == ["prepare"] and data.rolled_back
