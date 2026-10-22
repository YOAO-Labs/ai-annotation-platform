from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
from aap_identity.contract import IdentityUnavailable
from aap_identity.recovery_contract import (
    NATIVE_RECOVERY_PROTOCOL,
    NativeRecoveryApplyReply,
    NativeRecoveryConfig,
    NativeRecoveryResult,
    decode_native_recovery_apply_command,
    decode_native_recovery_prepare_command,
    native_recovery_apply_command,
    native_recovery_prepare_command,
)
from aap_identity.recovery_crypto import request_fingerprint
from aap_identity.recovery_postgres import RecoveryPostgresData
from recovery_helpers import prepare_reply, recovery_request


class Connection:
    def __init__(self, *, allowed=True, bad_prepare=False):
        self.allowed = allowed
        self.bad_prepare = bad_prepare
        self.calls = []
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args, self.in_transaction))
        if "has_function_privilege" in sql:
            return self.allowed
        if "recovery_policy()" in sql:
            return json.dumps(
                {
                    "schema": "aap-identity-recovery-policy/v1",
                    "policy": "aap-identity-recovery/v1",
                }
            )
        if "read_registration_status" in sql:
            return json.dumps(
                {
                    "schema": "aap-identity-registration-status/v1",
                    "operation": "read_status",
                    "ok": True,
                    "data": {"allow_open_registration": True},
                }
            )
        if "prepare_recovery_command" in sql:
            command = decode_native_recovery_prepare_command(json.loads(args[0]))
            reply = prepare_reply(command, eligible=False)
            if self.bad_prepare:
                reply = reply.model_copy(update={"operation_id": uuid.uuid4()})
            return json.dumps(reply.model_dump(mode="json", by_alias=True))
        if "apply_recovery_command" in sql:
            command = decode_native_recovery_apply_command(json.loads(args[0]))
            reply = NativeRecoveryApplyReply(
                schema=NATIVE_RECOVERY_PROTOCOL,
                ok=True,
                operation=command.operation,
                operation_id=command.operation_id,
                data={
                    "result": NativeRecoveryResult(
                        account=None,
                        claims=None,
                        email_verification_required=None,
                        completed=None,
                    ),
                    "actor": None,
                    "replayed": False,
                    "password_changed": False,
                    "email_verified_changed": False,
                },
            )
            return json.dumps(reply.model_dump(mode="json", by_alias=True))
        raise AssertionError(sql)

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_recovery_policy_checks_its_four_native_capabilities(monkeypatch):
    connection = Connection()

    async def connect(*args, **kwargs):
        assert kwargs["server_settings"] == {
            "application_name": "aap-identity-recovery"
        }
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    result = await RecoveryPostgresData("postgresql://recovery").recovery_policy()

    assert result.protocol == "aap-identity-recovery-policy/v1"
    assert connection.calls[1][1][0] == [
        "aap_identity_recovery_v1.recovery_policy()",
        "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
        "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
        "aap_identity_recovery_v1.read_registration_status(jsonb)",
    ]
    assert connection.committed and connection.closed


@pytest.mark.asyncio
async def test_read_registration_status_uses_the_owner_function_in_one_transaction(
    monkeypatch,
):
    connection = Connection()

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    fallbacks = {
        "allow_open_registration": False,
        "frontend_base_url": "https://identity.example.test",
    }
    result = await RecoveryPostgresData(
        "postgresql://recovery"
    ).read_registration_status(fallbacks)

    assert result == {
        "schema": "aap-identity-registration-status/v1",
        "operation": "read_status",
        "ok": True,
        "data": {"allow_open_registration": True},
    }
    query, args, in_transaction = connection.calls[0]
    assert "aap_identity_recovery_v1.read_registration_status($1::jsonb)" in query
    assert json.loads(args[0]) == fallbacks
    assert in_transaction
    assert connection.committed and connection.closed


@pytest.mark.asyncio
async def test_prepare_and_apply_use_one_transaction_and_closed_function_calls(
    monkeypatch,
):
    connection = Connection()

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = RecoveryPostgresData("postgresql://recovery")
    request = recovery_request("request_password_reset")
    fingerprint, epoch = request_fingerprint("recovery-test-secret", request)
    command = native_recovery_prepare_command(
        request,
        request_fingerprint=fingerprint,
        derivation_id=epoch,
        config=NativeRecoveryConfig(
            allow_open_registration=False,
            email_verification_required=False,
            frontend_base_url="http://localhost:3000",
            expire_minutes=1440,
        ),
    )
    async with data.transaction() as transaction:
        prepared = await transaction.prepare_recovery_command(
            command.model_dump(mode="json", by_alias=True)
        )
        assert prepared.operation_id == request.operation_id
        apply = native_recovery_apply_command(
            command,
            password_hash=None,
            token_hash=None,
            token_ciphertext=None,
            encryption_id=None,
        )
        result = await transaction.apply_recovery_command(
            apply.model_dump(mode="json", by_alias=True)
        )
        assert result.operation_id == request.operation_id

    assert "prepare_recovery_command" in connection.calls[0][0]
    assert "apply_recovery_command" in connection.calls[1][0]
    assert all(call[2] for call in connection.calls)
    assert connection.committed and connection.closed


@pytest.mark.asyncio
async def test_recovery_policy_fails_closed_when_capability_is_missing(monkeypatch):
    connection = Connection(allowed=False)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(IdentityUnavailable):
        await RecoveryPostgresData("postgresql://recovery").recovery_policy()
    assert connection.rolled_back and not connection.committed and connection.closed
