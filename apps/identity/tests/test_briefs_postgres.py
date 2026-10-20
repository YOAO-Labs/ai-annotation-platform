from __future__ import annotations

import json

import pytest

from aap_identity.briefs_postgres import (
    BriefsPostgresData,
    BriefsPostgresTransaction,
)
from aap_identity.contract import IdentityUnavailable
from briefs_helpers import (
    BRIEFS_POLICY,
    briefs_request,
    native_briefs_success,
)
from aap_identity.briefs_contract import native_briefs_command


class Connection:
    def __init__(
        self,
        value=None,
        *,
        extra_functions=(),
        relation_access=False,
        sequence_access=False,
        schema_access=False,
        database_access=False,
    ):
        self.value = value
        self.extra_functions = list(extra_functions)
        self.relation_access = relation_access
        self.sequence_access = sequence_access
        self.schema_access = schema_access
        self.database_access = database_access
        self.queries = []
        self.closed = False
        self.readonly = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "execute_briefs_command" in query:
            return json.dumps(self.value)
        if "briefs_policy()" in query:
            return json.dumps(BRIEFS_POLICY)
        if "has_database_privilege" in query or "has_schema_privilege" in query:
            return self.database_access or self.schema_access
        if "has_table_privilege" in query:
            return self.relation_access
        if "has_sequence_privilege" in query:
            return self.sequence_access
        raise AssertionError("Unexpected query")

    async def fetch(self, query, *args):
        self.queries.append(query)
        return [
            {
                "nspname": "aap_identity_briefs_v1",
                "proname": "briefs_policy",
                "args": "",
            },
            {
                "nspname": "aap_identity_briefs_v1",
                "proname": "execute_briefs_command",
                "args": "jsonb",
            },
        ] + self.extra_functions

    def transaction(self, *, readonly=False):
        self.readonly.append(readonly)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def close(self, *, timeout):
        self.closed = True


@pytest.mark.asyncio
async def test_transaction_calls_only_versioned_function_and_validates_reply():
    command = native_briefs_command(briefs_request("get_user_briefs", []))
    connection = Connection(native_briefs_success(command))

    result = await BriefsPostgresTransaction(connection).execute_briefs_command(
        command.model_dump(mode="json", by_alias=True)
    )

    assert result == native_briefs_success(command)
    assert len(connection.queries) == 1
    assert (
        "aap_identity_briefs_v1.execute_briefs_command($1::jsonb)"
        in connection.queries[0]
    )


@pytest.mark.asyncio
async def test_policy_and_read_transactions_are_readonly_and_close_connections(
    monkeypatch,
):
    import asyncpg

    policy_connection = Connection()
    command = native_briefs_command(briefs_request("get_user_briefs", []))
    execute_connection = Connection(native_briefs_success(command))
    connections = iter((policy_connection, execute_connection))

    async def connect(*_args, **_kwargs):
        return next(connections)

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = BriefsPostgresData("postgresql://briefs.invalid/identity")

    await data.briefs_policy()
    async with data.transaction() as transaction:
        await transaction.execute_briefs_command(
            command.model_dump(mode="json", by_alias=True)
        )

    assert policy_connection.readonly == [True]
    assert execute_connection.readonly == [True]
    assert policy_connection.closed and execute_connection.closed


@pytest.mark.asyncio
async def test_readiness_requires_exact_two_functions_and_no_direct_access():
    expected = {
        "aap_identity_briefs_v1.briefs_policy()",
        "aap_identity_briefs_v1.execute_briefs_command(jsonb)",
    }
    connection = Connection()

    await BriefsPostgresData._assert_capabilities(connection, expected)

    assert "oidvectortypes" in connection.queries[0]
    assert "pg_depend" in connection.queries[0]
    assert "function.prosecdef" in connection.queries[0]
    assert "pg_catalog.aclexplode" in connection.queries[0]
    assert "'CREATE,TEMP'" in connection.queries[1]
    assert "has_any_column_privilege" in connection.queries[2]
    assert "has_sequence_privilege" in connection.queries[3]

    for invalid in (
        Connection(
            extra_functions=[
                {
                    "nspname": "aap_workspace_v1",
                    "proname": "unrelated_capability",
                    "args": "uuid",
                }
            ]
        ),
        Connection(relation_access=True),
        Connection(sequence_access=True),
        Connection(schema_access=True),
        Connection(database_access=True),
    ):
        with pytest.raises(IdentityUnavailable):
            await BriefsPostgresData._assert_capabilities(invalid, expected)
