from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid

import asyncpg
import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.session_contract import (
    NATIVE_SESSION_PROTOCOL,
    NativeLogin,
    NativeLoginCommand,
    SessionAuditContext,
    SessionPolicy,
)
from aap_identity.session_postgres import SessionPostgresData


def login_command(*, failed_count=1):
    return NativeLoginCommand(
        schema=NATIVE_SESSION_PROTOCOL,
        operation="login",
        operation_id=uuid.uuid4(),
        actor=None,
        expected_key_hash=None,
        login=NativeLogin(
            email="person@example.test",
            actor_id=uuid.uuid4(),
            expected_password_hash="$2b$04$" + "a" * 53,
            password_matches=False,
            failed_count=failed_count,
        ),
        policy=SessionPolicy(
            expire_minutes=1440,
            email_verification_required=False,
            derivation_id="a" * 64,
        ),
        request_fingerprint="b" * 64,
        audit_context=SessionAuditContext(
            method="POST", path="/login", ip="192.0.2.1", request_id=None
        ),
        user_agent="pytest",
    )


class Connection:
    def __init__(self, *, result=None, allowed=True, bad_count=False):
        self.result = result
        self.allowed = allowed
        self.bad_count = bad_count
        self.calls = []
        self.closed = False
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False

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
        if "session_policy" in sql:
            return json.dumps(self.result)
        if "read_login_candidate" in sql:
            return json.dumps(self.result)
        if "execute_session_command" in sql:
            command = json.loads(args[0])
            count = command["login"]["failed_count"]
            return json.dumps(
                {
                    "schema": NATIVE_SESSION_PROTOCOL,
                    "ok": False,
                    "error": {
                        "code": "invalid_credentials",
                        "failed_count": count - 1 if self.bad_count else count,
                    },
                }
            )
        raise AssertionError(sql)

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_session_policy_requires_exact_three_function_grants(monkeypatch):
    connection = Connection(
        result={
            "schema": "aap-identity-sessions-policy/v1",
            "issuer": "aap-identity/v1",
            "algorithm": "HS256",
            "refresh_grace_seconds": 604800,
        }
    )
    connects = []

    async def connect(*args, **kwargs):
        connects.append((args, kwargs))
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    policy = await SessionPostgresData("postgresql://session").session_policy()
    assert policy["issuer"] == "aap-identity/v1"
    assert connection.calls[1][1][0] == [
        "aap_identity_sessions_v1.session_policy()",
        "aap_identity_sessions_v1.read_login_candidate(text)",
        "aap_identity_sessions_v1.execute_session_command(jsonb)",
    ]
    assert connects[0][1]["server_settings"] == {
        "application_name": "aap-identity-session-commands"
    }
    assert connection.committed and connection.closed


@pytest.mark.asyncio
async def test_session_policy_fails_closed_when_a_grant_is_missing(monkeypatch):
    connection = Connection(allowed=False)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(IdentityUnavailable):
        await SessionPostgresData("postgresql://session").session_policy()
    assert connection.rolled_back and connection.closed


@pytest.mark.asyncio
async def test_login_candidate_uses_function_only_query_and_validates_item(monkeypatch):
    actor_id = uuid.uuid4()
    connection = Connection(
        result={
            "schema": "aap-identity-login-candidate/v1",
            "item": {"actor_id": str(actor_id), "password_hash": "bcrypt-hash"},
        }
    )

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    result = await SessionPostgresData("postgresql://session").read_login_candidate(
        "Exact+Tag@example.test"
    )
    assert result["item"]["actor_id"] == str(actor_id)
    assert connection.calls[0][0] == (
        "SELECT aap_identity_sessions_v1.read_login_candidate($1::text)"
    )
    assert connection.calls[0][1] == ("Exact+Tag@example.test",)
    assert connection.closed and connection.committed


@pytest.mark.asyncio
async def test_valid_login_failure_commits_staged_audit_before_manager_raises(
    monkeypatch,
):
    connection = Connection()

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    command = login_command(failed_count=2)
    payload = command.model_dump(mode="json", by_alias=True)
    result = await SessionPostgresData("postgresql://session").execute_session_command(
        payload
    )
    assert result == {
        "schema": NATIVE_SESSION_PROTOCOL,
        "ok": False,
        "error": {"code": "invalid_credentials", "failed_count": 2},
    }
    sql, params, in_transaction = connection.calls[0]
    assert sql == ("SELECT aap_identity_sessions_v1.execute_session_command($1::jsonb)")
    assert json.loads(params[0]) == payload and in_transaction is True
    assert connection.committed and not connection.rolled_back and connection.closed


@pytest.mark.asyncio
async def test_malformed_failure_dto_rolls_back_before_receipt_transaction_commit(
    monkeypatch,
):
    connection = Connection(bad_count=True)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    command = login_command(failed_count=2)
    with pytest.raises(IdentityUnavailable):
        await SessionPostgresData("postgresql://session").execute_session_command(
            command.model_dump(mode="json", by_alias=True)
        )
    assert connection.rolled_back and not connection.committed and connection.closed
