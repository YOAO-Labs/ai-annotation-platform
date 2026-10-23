from __future__ import annotations

import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_v2_contract import (
    MAIL_V2_NATIVE_PROTOCOL,
    MAIL_V2_POLICY,
    MAIL_V2_POLICY_PROTOCOL,
)
from aap_identity.mail_v2_postgres import MailV2PostgresData

WORKER_ID = uuid.UUID("73000000-0000-4000-8000-000000000001")


def _invitation_reply(worker_id: uuid.UUID) -> dict:
    now = datetime.now(UTC)
    return {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {
            "delivery": {
                "id": "73000000-0000-4000-8000-000000000002",
                "purpose": "invitation",
                "issuer_id": "73000000-0000-4000-8000-000000000003",
                "invitation_id": "73000000-0000-4000-8000-000000000004",
                "email": "recipient@example.test",
                "frontend_base_url": "https://labels.example.test/app",
                "token_hash": "a" * 64,
                "token_ciphertext": "encrypted-only",
                "encryption_id": "b" * 64,
                "expires_at": (now + timedelta(days=7)).isoformat(),
                "attempt": 1,
                "worker_id": str(worker_id),
                "lease_token": "opaque-lease-token",
                "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
                "project_name": "Vision project",
                "project_member_role": "annotator",
            }
        },
    }


class FakeConnection:
    def __init__(
        self,
        result=None,
        *,
        grants=(),
        table_access=False,
        column_access=False,
        sequence_access=False,
    ):
        self.result = result
        self.grants = grants
        self.table_access = table_access
        self.column_access = column_access
        self.sequence_access = sequence_access
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
        if "has_sequence_privilege" in sql:
            return self.sequence_access
        if "has_table_privilege" in sql:
            return self.table_access or self.column_access
        return json.dumps(self.result)

    async def fetch(self, sql):
        self.calls.append((sql, (), self.in_transaction))
        return [
            {
                "nspname": schema,
                "proname": name,
                "args": args,
                "prosecdef": prosecdef,
                "prorettype": return_type,
            }
            for schema, name, args, prosecdef, return_type in self.grants
            if (schema.startswith("aap_") or prosecdef)
            and return_type not in {"trigger", "event_trigger"}
        ]

    async def close(self, *, timeout):
        assert timeout == 2
        self.closed = True


def _asyncpg(connection):
    async def connect(*_args, **kwargs):
        assert kwargs["server_settings"] == {"application_name": "aap-identity-mail-v2"}
        return connection

    class PostgresError(Exception):
        pass

    class InterfaceError(Exception):
        pass

    return SimpleNamespace(
        connect=connect,
        PostgresError=PostgresError,
        InterfaceError=InterfaceError,
    )


@pytest.mark.asyncio
async def test_v2_claim_validates_bound_reply_inside_transaction(monkeypatch):
    connection = FakeConnection(_invitation_reply(WORKER_ID))
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    lease = await MailV2PostgresData("postgresql://mail-v2/db").claim_mail(WORKER_ID)
    assert lease.invitation_id == uuid.UUID("73000000-0000-4000-8000-000000000004")
    assert lease.worker_id == WORKER_ID
    assert connection.committed and not connection.rolled_back and connection.closed
    sql, (payload,), in_transaction = connection.calls[0]
    assert sql == "SELECT aap_identity_mail_v2.claim_mail($1::jsonb)"
    assert in_transaction
    assert json.loads(payload) == {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "worker_id": str(WORKER_ID),
        "lease_seconds": 60,
        "delivery_id": None,
    }

    connection = FakeConnection(_invitation_reply(uuid.uuid4()))
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(IdentityUnavailable):
        await MailV2PostgresData("postgresql://mail-v2/db").claim_mail(WORKER_ID)
    assert connection.rolled_back and not connection.committed and connection.closed


@pytest.mark.asyncio
async def test_v2_finish_malformed_reply_rolls_back_and_stale_reply_returns_false(
    monkeypatch,
):
    from aap_identity.mail_v2_contract import decode_mail_v2_claim_reply

    lease = decode_mail_v2_claim_reply(_invitation_reply(WORKER_ID)).data.delivery
    invalid = {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"accepted": 1},
    }
    connection = FakeConnection(invalid)
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(IdentityUnavailable):
        await MailV2PostgresData("postgresql://mail-v2/db").finish_mail(
            lease,
            outcome="delivered",
            reason="sent",
            retry_delay_seconds=None,
        )
    assert connection.rolled_back and not connection.committed

    connection = FakeConnection(
        {
            "schema": MAIL_V2_NATIVE_PROTOCOL,
            "ok": True,
            "data": {"accepted": False},
        }
    )
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    accepted = await MailV2PostgresData("postgresql://mail-v2/db").finish_mail(
        lease,
        outcome="delivered",
        reason="sent",
        retry_delay_seconds=None,
    )
    assert accepted is False
    assert connection.rolled_back and not connection.committed


class PolicyConnection(FakeConnection):
    def __init__(
        self,
        grants,
        *,
        table_access=False,
        column_access=False,
        sequence_access=False,
    ):
        super().__init__(
            {
                "schema": MAIL_V2_POLICY_PROTOCOL,
                "policy": MAIL_V2_POLICY,
            },
            grants=grants,
            table_access=table_access,
            column_access=column_access,
            sequence_access=sequence_access,
        )


@pytest.mark.asyncio
async def test_v2_readiness_requires_exact_functions_and_no_relation_access(
    monkeypatch,
):
    grants = [
        ("aap_identity_mail_v2", "mail_policy", "", True, "jsonb"),
        ("aap_identity_mail_v2", "claim_mail", "jsonb", True, "jsonb"),
        ("aap_identity_mail_v2", "renew_mail", "jsonb", True, "jsonb"),
        ("aap_identity_mail_v2", "finish_mail", "jsonb", True, "jsonb"),
        ("public", "safe_extension_helper", "text", False, "text"),
        ("aap_identity_mail_v2", "trigger_hook", "trigger", True, "trigger"),
        (
            "aap_identity_mail_v2",
            "event_trigger_hook",
            "event_trigger",
            True,
            "event_trigger",
        ),
    ]
    connection = PolicyConnection(grants)
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    policy = await MailV2PostgresData("postgresql://mail-v2/db").mail_policy()
    assert policy.policy == MAIL_V2_POLICY
    assert connection.committed and connection.closed
    queries = [call[0] for call in connection.calls]
    assert any("oidvectortypes(function.proargtypes)" in query for query in queries)
    capability_sql = next(query for query in queries if "pg_proc" in query)
    assert "function.prorettype" in capability_sql
    assert "return_type.typname IN ('trigger', 'event_trigger')" in capability_sql
    assert any("has_any_column_privilege" in query for query in queries)
    assert any("has_sequence_privilege" in query for query in queries)

    for unsafe_grants, table_access, sequence_access in (
        (
            [*grants, ("aap_identity_invitations_v1", "unsafe", "uuid", True, "jsonb")],
            False,
            False,
        ),
        (
            [*grants, ("public", "unsafe_definer", "uuid", True, "jsonb")],
            False,
            False,
        ),
        (grants, True, False),
        (grants, False, True),
    ):
        connection = PolicyConnection(
            unsafe_grants,
            table_access=table_access,
            sequence_access=sequence_access,
        )
        monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
        with pytest.raises(IdentityUnavailable):
            await MailV2PostgresData("postgresql://mail-v2/db").mail_policy()
        assert connection.rolled_back and connection.closed

    connection = PolicyConnection(grants, column_access=True)
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(IdentityUnavailable):
        await MailV2PostgresData("postgresql://mail-v2/db").mail_policy()
    assert connection.rolled_back and connection.closed
