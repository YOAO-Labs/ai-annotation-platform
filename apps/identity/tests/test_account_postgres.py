from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid

import asyncpg
import pytest

from aap_identity.account_contract import (
    NATIVE_ACCOUNT_PROTOCOL,
    AccountFailure,
    AccountResultData,
    NativeAccountReply,
    NativePrepareData,
    NativePrepareReply,
    native_account_apply_command,
    native_account_prepare_command,
)
from aap_identity.account_postgres import AccountPostgresData
from aap_identity.contract import IdentityUnavailable, Principal

from account_helpers import account_record, account_request
from session_helpers import principal


class Connection:
    def __init__(
        self, *, allowed=True, bad_prepare=False, bad_apply=False, failure=None
    ):
        self.allowed = allowed
        self.bad_prepare = bad_prepare
        self.bad_apply = bad_apply
        self.failure = failure
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
        if "account_policy()" in sql:
            return json.dumps(
                {
                    "schema": "aap-identity-accounts-policy/v1",
                    "policy": "aap-identity-accounts/v1",
                }
            )
        if "prepare_account_command" in sql:
            command = json.loads(args[0])
            if self.failure:
                return json.dumps(
                    {
                        "schema": NATIVE_ACCOUNT_PROTOCOL,
                        "ok": False,
                        "error": {
                            "code": self.failure,
                            "status_code": None,
                            "detail": None,
                        },
                    }
                )
            reply = NativePrepareReply(
                schema=NATIVE_ACCOUNT_PROTOCOL,
                ok=True,
                operation="update_name",
                operation_id=uuid.uuid4()
                if self.bad_prepare
                else uuid.UUID(command["operation_id"]),
                principal=Principal.model_validate_json(json.dumps(command["actor"])),
                data=NativePrepareData(
                    account=account_record(), password_hash=None, replay=None
                ),
            )
            return json.dumps(reply.model_dump(mode="json", by_alias=True))
        if "apply_account_command" in sql:
            command = json.loads(args[0])
            reply = NativeAccountReply(
                schema=NATIVE_ACCOUNT_PROTOCOL,
                ok=True,
                operation=command["operation"],
                operation_id=uuid.uuid4()
                if self.bad_apply
                else uuid.UUID(command["operation_id"]),
                principal=Principal.model_validate_json(json.dumps(command["actor"])),
                data=AccountResultData(
                    account=account_record(name=command["new_value"]),
                    preferences=None,
                    previous_avatar_ref=None,
                    changed=True,
                ),
            )
            return json.dumps(reply.model_dump(mode="json", by_alias=True))
        raise AssertionError(sql)

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_account_policy_checks_exact_function_grants(monkeypatch):
    connection = Connection()

    async def connect(*args, **kwargs):
        assert kwargs["server_settings"] == {
            "application_name": "aap-identity-account-commands"
        }
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    policy = await AccountPostgresData("postgresql://account").account_policy()
    assert policy.protocol == "aap-identity-accounts-policy/v1"
    assert connection.calls[1][1][0] == [
        "aap_identity_accounts_v1.account_policy()",
        "aap_identity_accounts_v1.prepare_account_command(jsonb)",
        "aap_identity_accounts_v1.apply_account_command(jsonb)",
    ]
    assert connection.committed and connection.closed


@pytest.mark.asyncio
async def test_account_policy_missing_grant_fails_closed(monkeypatch):
    connection = Connection(allowed=False)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(IdentityUnavailable):
        await AccountPostgresData("postgresql://account").account_policy()
    assert connection.rolled_back and connection.closed


@pytest.mark.asyncio
async def test_prepare_and_apply_validate_before_same_transaction_commit(monkeypatch):
    connection = Connection()

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = AccountPostgresData("postgresql://account")
    request = account_request("update_name", payload={"name": "Changed"})
    command = native_account_prepare_command(
        request,
        actor=principal(),
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
    )
    async with data.transaction() as transaction:
        prepared = await transaction.prepare_account_command(
            command.model_dump(mode="json", by_alias=True)
        )
        assert prepared.data.account.name == "Current account"
        apply = native_account_apply_command(
            command,
            base_value=prepared.data.account.name,
            new_value="Changed",
            response_preferences=None,
        )
        reply = await transaction.apply_account_command(
            apply.model_dump(mode="json", by_alias=True)
        )
        assert reply.data.account.name == "Changed"
    assert ["prepare_account_command" in call[0] for call in connection.calls] == [
        True,
        False,
    ]
    assert ["apply_account_command" in call[0] for call in connection.calls] == [
        False,
        True,
    ]
    assert all(call[2] for call in connection.calls)
    assert connection.committed and connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["operation_conflict", "account_busy"])
async def test_native_business_failure_rolls_back(monkeypatch, failure):
    connection = Connection(failure=failure)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = AccountPostgresData("postgresql://account")
    request = account_request("update_name")
    command = native_account_prepare_command(
        request,
        actor=principal(),
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
    )
    with pytest.raises(AccountFailure) as error:
        async with data.transaction() as transaction:
            await transaction.prepare_account_command(
                command.model_dump(mode="json", by_alias=True)
            )
    assert error.value.code == failure
    assert connection.rolled_back and not connection.committed and connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_prepare,bad_apply", [(True, False), (False, True)])
async def test_wrong_native_operation_binding_rolls_back_before_commit(
    monkeypatch, bad_prepare, bad_apply
):
    connection = Connection(bad_prepare=bad_prepare, bad_apply=bad_apply)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = AccountPostgresData("postgresql://account")
    request = account_request("update_name")
    command = native_account_prepare_command(
        request,
        actor=principal(),
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
    )
    with pytest.raises(IdentityUnavailable):
        async with data.transaction() as transaction:
            prepared = await transaction.prepare_account_command(
                command.model_dump(mode="json", by_alias=True)
            )
            apply = native_account_apply_command(
                command,
                base_value=prepared.data.account.name,
                new_value="Changed",
                response_preferences=None,
            )
            await transaction.apply_account_command(
                apply.model_dump(mode="json", by_alias=True)
            )
    assert connection.rolled_back and not connection.committed and connection.closed
