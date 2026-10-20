from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.invitation_contract import (
    NativeInvitationConfig,
    NativeInvitationPrepareReply,
    native_invitation_prepare_command,
)
from aap_identity.invitation_crypto import invitation_request_fingerprint
from aap_identity.invitation_postgres import (
    InvitationPostgresData,
    InvitationPostgresTransaction,
)
from invitation_helpers import (
    INVITATION_SECRET,
    actor_record,
    invitation_request,
    prepare_reply,
    result,
)
from session_helpers import principal


class Connection:
    def __init__(
        self,
        reply=None,
        *,
        functions=None,
        table_access=False,
        column_access=False,
        sequence_access=False,
    ):
        self.reply = reply
        self.functions = functions or [
            ("aap_identity_invitations_v1", "invitation_policy", ""),
            ("aap_identity_invitations_v1", "prepare_invitation_command", "jsonb"),
            ("aap_identity_invitations_v1", "apply_invitation_command", "jsonb"),
        ]
        self.table_access = table_access
        self.column_access = column_access
        self.sequence_access = sequence_access
        self.calls = []
        self.fetch_query = None
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        self.calls.append(("begin", None))
        try:
            yield
        except Exception:
            self.calls.append(("rollback", None))
            raise
        else:
            self.calls.append(("commit", None))

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "has_table_privilege" in sql:
            return self.table_access or self.column_access
        if "has_sequence_privilege" in sql:
            return self.sequence_access
        return self.reply

    async def fetch(self, sql):
        self.fetch_query = sql
        return [
            {"nspname": namespace, "proname": name, "args": args, "prosecdef": False}
            for namespace, name, args in self.functions
        ]

    async def close(self, timeout):
        assert timeout == 2
        self.closed = True


@pytest.mark.asyncio
async def test_invitation_prepare_calls_only_owner_capability_and_validates_reply():
    request = invitation_request("list_invitations")
    actor = principal(
        actor_id=actor_record().id,
        role="super_admin",
        name="Inviter",
        email="inviter@example.test",
    )
    command = native_invitation_prepare_command(
        request,
        actor=actor,
        expected_key_hash=None,
        request_fingerprint=invitation_request_fingerprint(INVITATION_SECRET, request)[
            0
        ],
        derivation_id="a" * 64,
        config=NativeInvitationConfig(
            invitation_ttl_days=7,
            max_invitations_per_day=30,
            frontend_base_url="https://frontend.example.test",
            expire_minutes=1440,
        ),
    )
    raw_reply = prepare_reply(
        command,
        actor_value=actor_record(),
        native_result=result(invitations=[]),
    )
    connection = Connection(
        json.dumps(raw_reply.model_dump(mode="json", by_alias=True))
    )
    returned = await InvitationPostgresTransaction(
        connection
    ).prepare_invitation_command(command.model_dump(mode="json", by_alias=True))
    assert isinstance(returned, NativeInvitationPrepareReply)
    assert returned.data.result.invitations == []
    assert len(connection.calls) == 1
    assert connection.calls[0][0] == (
        "SELECT aap_identity_invitations_v1.prepare_invitation_command($1::jsonb)"
    )
    payload = json.loads(connection.calls[0][1][0])
    assert "token" not in payload
    assert payload["schema"] == "aap-identity-invitations-native/v1"


@pytest.mark.asyncio
async def test_invitation_policy_requires_exact_functions_and_no_direct_privileges(
    monkeypatch,
):
    connection = Connection(
        {
            "schema": "aap-identity-invitations-policy/v1",
            "policy": "aap-identity-invitations/v1",
        }
    )

    async def connect(*_args, **_kwargs):
        return connection

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=connect,
            PostgresError=Exception,
            InterfaceError=Exception,
        ),
    )
    reply = await InvitationPostgresData(
        "postgresql://invitations.invalid/db"
    ).invitation_policy()
    assert reply.policy == "aap-identity-invitations/v1"
    assert "oidvectortypes(function.proargtypes)" in connection.fetch_query
    assert any("has_any_column_privilege" in sql for sql, _ in connection.calls)
    assert connection.closed

    for invalid in (
        Connection(
            {
                "schema": "aap-identity-invitations-policy/v1",
                "policy": "aap-identity-invitations/v1",
            },
            functions=[
                ("aap_identity_invitations_v1", "invitation_policy", ""),
                ("aap_identity_invitations_v1", "prepare_invitation_command", "jsonb"),
                ("aap_identity_invitations_v1", "apply_invitation_command", "jsonb"),
                ("aap_workspace_v1", "unsafe_capability", "uuid"),
            ],
        ),
        Connection(
            {
                "schema": "aap-identity-invitations-policy/v1",
                "policy": "aap-identity-invitations/v1",
            },
            table_access=True,
        ),
        Connection(
            {
                "schema": "aap-identity-invitations-policy/v1",
                "policy": "aap-identity-invitations/v1",
            },
            column_access=True,
        ),
        Connection(
            {
                "schema": "aap-identity-invitations-policy/v1",
                "policy": "aap-identity-invitations/v1",
            },
            sequence_access=True,
        ),
    ):

        async def connect_invalid(*_args, _connection=invalid, **_kwargs):
            return _connection

        monkeypatch.setitem(
            sys.modules,
            "asyncpg",
            SimpleNamespace(
                connect=connect_invalid,
                PostgresError=Exception,
                InterfaceError=Exception,
            ),
        )
        with pytest.raises(IdentityUnavailable):
            await InvitationPostgresData(
                "postgresql://invitations.invalid/db"
            ).invitation_policy()
        assert invalid.closed
